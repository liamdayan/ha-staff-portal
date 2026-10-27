"""Photos on tasks made from Connecteam form submissions, fetched on the server for the staff app.

A submission's image answers (and image files) come as links (ctdata._photos). Connecteam's docs
don't say where those live or for how long they work (its own file downloads come from
cdn.connecteam.com, and its upload links are signed and expire), and the staff app's CSP only
shows images from itself. So the staff server fetches a photo itself, for someone who may see
the task (main.api_task_photo checks), and only:
- from HOSTS (Connecteam's own: connecteam.com and its subdomains), over https, following at
  most MAX_REDIRECTS redirects that stay on those hosts;
- an image (image/*, not SVG), at most MAX_BYTES, that Pillow can read as a photo;
and sends back a smaller JPEG (at most MAX_SIDE pixels a side), kept on disk for KEEP_SECONDS so a
photo isn't fetched again on every look (and still shows after its link expires). A photo that
can't be had says "Photo unavailable".
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import time
import urllib.parse

import aiohttp

log = logging.getLogger("staff_portal")

HOSTS = ("connecteam.com",)   # and their subdomains (cdn.connecteam.com)
SCHEMES = ("https",)
MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 50_000_000     # a photo bigger than this (in pixels) isn't opened
MAX_SIDE = 1600
MAX_REDIRECTS = 3
TIMEOUT = 20
KEEP_SECONDS = 14 * 86400   # copies kept this long...
MAX_KEPT = 200 * 1024 * 1024   # ...and at most this much of them (oldest go first)
UNAVAILABLE = "Photo unavailable."
FORMATS = ("JPEG", "PNG", "GIF", "WEBP", "MPO", "BMP", "TIFF")


class Unavailable(Exception):
    pass


def host_ok(url: str) -> bool:
    """Whether a link is on Connecteam's own hosts (HOSTS), over https."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    return parts.scheme in SCHEMES and not parts.username and not parts.password and any(
        host == h or host.endswith("." + h) for h in HOSTS)


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
    raw = await _fetch(url)
    small = await asyncio.to_thread(_shrink, raw)
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
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for _ in range(MAX_REDIRECTS + 1):
                if not host_ok(url):
                    raise Unavailable(UNAVAILABLE)
                async with session.get(url, allow_redirects=False) as resp:
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
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
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
