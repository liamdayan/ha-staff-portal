"""Photos on tasks made from Connecteam form submissions, fetched on the server for the staff app.

A submission's image answers (and image files) come as links (ctdata._photos). Connecteam's docs
don't say where those live or for how long they work (its own file downloads come from
cdn.connecteam.com, and its upload links are signed and expire), and the staff app's CSP only
shows images from itself. So the staff server fetches a photo itself, for someone who may see
the task (main.api_task_photo checks), and only:
- from HOSTS (Connecteam's own: connecteam.com and its subdomains), over https on port 443, no
  user name in the link, following at most MAX_REDIRECTS redirects that stay on those hosts, and
  only to public addresses (PublicResolver: a name that points inside the network is refused);
- an image (image/*, not SVG), at most MAX_BYTES, that Pillow can read as a photo;
and sends back a smaller JPEG (at most MAX_SIDE pixels a side), kept on disk for KEEP_SECONDS so a
photo isn't fetched again on every look (and still shows after its link expires). A photo that
can't be had says "Photo unavailable".
"""

import asyncio
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import socket
import time
import urllib.parse

import aiohttp
import yarl

log = logging.getLogger("staff_portal")

HOSTS = ("connecteam.com",)   # and their subdomains (cdn.connecteam.com)
SCHEMES = ("https",)
PORTS = (443,)
MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 50_000_000     # a photo bigger than this (in pixels) isn't opened
MAX_SIDE = 1600
MAX_REDIRECTS = 3
TIMEOUT = 20
KEEP_SECONDS = 14 * 86400   # copies kept this long...
MAX_KEPT = 200 * 1024 * 1024   # ...and at most this much of them (oldest go first)
UNAVAILABLE = "Photo unavailable."
FORMATS = ("JPEG", "PNG", "GIF", "WEBP", "MPO", "BMP", "TIFF")
HOST_RE = re.compile(r"^[a-z0-9.-]{1,253}$")
FAILED_SECONDS = 600        # a photo that couldn't be had isn't asked for again for this long
MAX_FAILED = 500
_failed: dict[str, float] = {}


class Unavailable(Exception):
    pass


def _on_hosts(host) -> bool:
    host = str(host or "").lower().rstrip(".")
    return bool(HOST_RE.match(host)) and any(host == h or host.endswith("." + h) for h in HOSTS)


def host_ok(url: str) -> bool:
    """Whether a link is on Connecteam's own hosts (HOSTS), over https on 443, with no user name
    and nothing two URL parsers could read differently (spaces, backslashes, odd host letters)."""
    if not isinstance(url, str) or re.search(r"[\x00-\x20\x7f\\]", url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
        other = yarl.URL(url)   # what aiohttp will ask, which must say the same
    except ValueError:
        return False
    return (parts.scheme in SCHEMES and "@" not in parts.netloc and port in (None, other.port)
            and other.port in PORTS and _on_hosts(parts.hostname) and other.scheme == parts.scheme
            and (other.host or "").lower().rstrip(".") == parts.hostname.lower().rstrip(".")
            and not other.user and not other.password)


def public_ip(address: str) -> bool:
    """Whether an address is on the public internet (not this computer, the local network, or
    any reserved range; IPv4 inside IPv6 is checked as IPv4)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class PublicResolver(aiohttp.ThreadedResolver):
    """Looks names up as usual, then drops addresses that aren't public, so a Connecteam name
    pointed inside the network (or rebound between checks) is never connected to."""

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        found = [r for r in await super().resolve(host, port, family) if public_ip(r["host"])]
        if not found:
            raise OSError(f"{host} has no public address")
        return found


def _cache_file(folder: str, task_id: int, index: int, url: str) -> str:
    # The link without its query: a signed link's signature changes, the photo doesn't.
    plain = url.split("?", 1)[0]
    key = hashlib.sha256(f"{task_id}\n{index}\n{plain}".encode()).hexdigest()[:40]
    return os.path.join(folder, f"{key}.jpg")


async def photo(task: dict, index: int, folder: str) -> bytes:
    """A task's photo (index in its list) as a smaller JPEG. Raises Unavailable."""
    try:
        photos = json.loads(task.get("photos") or "[]")
        url = photos[index]["url"]
    except (ValueError, IndexError, KeyError, TypeError):
        raise Unavailable(UNAVAILABLE) from None
    if not isinstance(url, str) or not host_ok(url):
        raise Unavailable(UNAVAILABLE)
    path = _cache_file(folder, task["id"], index, url)
    try:
        if time.time() - os.path.getmtime(path) < KEEP_SECONDS:
            with open(path, "rb") as f:
                return f.read()
    except OSError:
        pass
    # One that failed lately isn't fetched again for every look (or every tap on it).
    now = time.time()
    if now - _failed.get(path, 0) < FAILED_SECONDS:
        raise Unavailable(UNAVAILABLE)
    try:
        small = await asyncio.to_thread(_shrink, await _fetch(url))
    except Unavailable:
        if len(_failed) >= MAX_FAILED:
            for key in [k for k, at in _failed.items() if now - at >= FAILED_SECONDS] or list(_failed):
                del _failed[key]
        _failed[path] = now
        raise
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path + ".part", "wb") as f:
            f.write(small)
        os.replace(path + ".part", path)
    except OSError as err:
        log.info("Photos: couldn't keep a copy: %s", err)
    return small


async def _fetch(url: str) -> bytes:
    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    try:
        connector = aiohttp.TCPConnector(resolver=PublicResolver())
        # No proxy from the environment (trust_env off), and redirects followed here, one by one.
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, trust_env=False) as session:
            for _ in range(MAX_REDIRECTS + 1):
                if not host_ok(url):
                    raise Unavailable(UNAVAILABLE)
                async with session.get(yarl.URL(url), allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                        url = urllib.parse.urljoin(url, resp.headers["Location"])
                        continue
                    kind = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                    if resp.status != 200 or not kind.startswith("image/") or "svg" in kind:
                        raise Unavailable(UNAVAILABLE)
                    if (resp.content_length or 0) > MAX_BYTES:
                        raise Unavailable(UNAVAILABLE)
                    body = bytearray()
                    async for chunk in resp.content.iter_chunked(65536):
                        body += chunk
                        if len(body) > MAX_BYTES:
                            raise Unavailable(UNAVAILABLE)
                    return bytes(body)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError):
        raise Unavailable(UNAVAILABLE) from None
    raise Unavailable(UNAVAILABLE)


def _shrink(raw: bytes) -> bytes:
    """A photo read and written again as a JPEG at most MAX_SIDE a side (turned upright), so
    only a plain picture ever reaches the page."""
    from PIL import Image, ImageOps
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.format not in FORMATS or im.width * im.height > MAX_PIXELS:
                raise Unavailable(UNAVAILABLE)
            im = ImageOps.exif_transpose(im)
            im.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            im.convert("RGB").save(out, "JPEG", quality=82, optimize=True)
            return out.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise Unavailable(UNAVAILABLE) from None


def tidy(folder: str) -> None:
    """Drop copies older than KEEP_SECONDS, and the oldest while there's more than MAX_KEPT."""
    try:
        files = [(e.stat().st_mtime, e.stat().st_size, e.path) for e in os.scandir(folder) if e.is_file()]
    except OSError:
        return
    total = sum(size for _, size, _ in files)
    for mtime, size, path in sorted(files):
        if time.time() - mtime > KEEP_SECONDS or total > MAX_KEPT:
            try:
                os.remove(path)
                total -= size
            except OSError:
                pass
