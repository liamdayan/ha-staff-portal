"""Finding TT Hotel's SN Web Socket helper on the local network, for computers whose card encoder
is set to Automatic.

Staff reach the app through a Cloudflare Tunnel, so the add-on can't learn a desk computer's
address from its requests. Instead it looks: Home Assistant's network setting (Settings > System
> Network) says which networks it's on, and every address there is tried on SN Web Socket's port.
A port that opens only counts when it greets like SN Web Socket ("you are connection"); nothing
is sent to it, so the encoder itself is never touched.

Only private networks (10.x, 172.16-31.x, 192.168.x) are searched, and at most a /24 (254
addresses) of each: a wider network is narrowed to the /24 around Home Assistant's own address.
An admin can name the networks instead (the wb_scan_subnets setting). What's found is kept in
memory, with when it was found.

A helper found this way is only ever used once an admin has confirmed it for the computer (Use
this encoder, which saves its address): anything on the network could greet like SN Web Socket,
and the encoder is sent the hotel key. A confirmed helper that isn't found is "not found", never
swapped for whatever else answered.

Searches only run when asked: at startup and when an Automatic computer's encoder isn't found or
stops answering (both only while something uses Automatic; the second at most every
AUTO_SCAN_GAP), and from the dashboard's Find encoders button (at most every ADMIN_SCAN_GAP).
The network source and "does anything use Automatic" are injected, so this module never touches
the database or Home Assistant itself.
"""

import asyncio
import ipaddress
import json
import logging
import re
import time

import aiohttp

log = logging.getLogger("staff_portal")

SN_WEB_SOCKET_PORT = 8102    # where SN Web Socket listens on the desk computer
PROBE_TIMEOUT = 0.5          # seconds to open a connection; a computer on the LAN answers in ms
GREETING_TIMEOUT = 2.0       # seconds an open port gets to greet like SN Web Socket
CONCURRENCY = 32             # addresses tried at once (a /24 takes about 4 seconds)
AUTO_SCAN_GAP = 10 * 60      # automatic searches (encoder not found or not answering) at most this
                             # often, so a desk computer that's switched off isn't hunted for
ADMIN_SCAN_GAP = 30          # the dashboard's Find encoders, at most this often
MAX_NETWORKS = 4             # networks searched at once (each at most a /24)
WIDEST_PREFIX = 24           # a wider network is narrowed to the /24 around Home Assistant
MAX_GREETING = 64 * 1024     # largest greeting read
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n)
                         for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
GREETING_TEXT = "you are connection"   # what SN Web Socket 2.5.1 says when a client connects

NO_NETWORKS = ("Home Assistant's network setting has no private IPv4 network to search. Enter "
               "the network in Wristbands settings, or enter the encoder's address instead.")
FAILED = "Looking for encoders didn't work. Try again in a minute."


class DiscoveryError(Exception):
    """Why encoders couldn't be looked for, in words for the admin."""


def private(address: ipaddress.IPv4Address) -> bool:
    return any(address in network for network in PRIVATE_NETWORKS)


def ha_networks(answer) -> list[ipaddress.IPv4Network]:
    """The networks to search, from Home Assistant's {"type": "network"} WebSocket answer:
    {"adapters": [{"name", "enabled", "ipv4": [{"address", "network_prefix"}], ...}], ...}.
    Enabled adapters' private IPv4 networks, each at most the /24 around Home Assistant's own
    address. Anything that doesn't look like that is left out."""
    adapters = answer.get("adapters") if isinstance(answer, dict) else None
    found: list[ipaddress.IPv4Network] = []
    for adapter in adapters if isinstance(adapters, list) else []:
        if not isinstance(adapter, dict) or adapter.get("enabled") is not True:
            continue
        entries = adapter.get("ipv4")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            prefix = entry.get("network_prefix")
            if isinstance(prefix, bool) or not isinstance(prefix, int) or not 0 <= prefix <= 32:
                continue
            try:
                interface = ipaddress.IPv4Interface(f"{entry.get('address')}/{prefix}")
            except ValueError:
                continue
            if not private(interface.ip):
                continue
            if prefix < WIDEST_PREFIX:
                interface = ipaddress.IPv4Interface(f"{interface.ip}/{WIDEST_PREFIX}")
            if interface.network not in found:
                found.append(interface.network)
    return found[:MAX_NETWORKS]


def parse_networks(text: str) -> list[ipaddress.IPv4Network]:
    """Networks an admin typed to search instead of Home Assistant's (commas or spaces between
    them): 192.168.1.0/24, or one address. Raises ValueError with a message for the admin."""
    found: list[ipaddress.IPv4Network] = []
    for item in re.split(r"[\s,;]+", str(text or "").strip()):
        if not item:
            continue
        try:
            network = ipaddress.IPv4Network(item, strict=False)
        except ValueError:
            raise ValueError(f"{item[:40]} isn't a network. Enter it like 192.168.1.0/24.") from None
        if not private(network.network_address) or not private(network.broadcast_address):
            raise ValueError(f"{network} isn't a private network (192.168.x.x, 10.x.x.x or "
                             "172.16-31.x.x), so it can't be searched.")
        if network.prefixlen < WIDEST_PREFIX:
            raise ValueError(f"{network} is too big to search. Use /24 (256 addresses) or smaller, "
                             "such as 192.168.1.0/24.")
        if network not in found:
            found.append(network)
    if len(found) > MAX_NETWORKS:
        raise ValueError(f"Enter at most {MAX_NETWORKS} networks to search.")
    return found


def describe(networks) -> str:
    """192.168.1.0/24, 10.0.0.0/24"""
    return ", ".join(str(n) for n in networks)


def addresses(network: ipaddress.IPv4Network) -> list[ipaddress.IPv4Address]:
    """Every address worth trying on a network (not its network or broadcast address)."""
    return list(network) if network.prefixlen >= 31 else list(network.hosts())


def greeting(text) -> str | None:
    """SN Web Socket's version ("" if it didn't say) if this is its greeting, else None."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("errorCode") != 0 or isinstance(
            data.get("errorCode"), bool):
        return None
    description = data.get("description")
    if not isinstance(description, str) or GREETING_TEXT not in description.lower():
        return None
    version = data.get("snWebSocketVersion")
    return version[:40] if isinstance(version, str) and version.isprintable() else ""


def resolve(helpers: list[dict], chosen_ip: str | None) -> tuple[dict | None, str]:
    """Which helper an Automatic computer uses, and why: (helper, "ok") only for the one at the
    address an admin confirmed for it; (None, "missing") when that one wasn't found (whatever
    else was); with none confirmed yet, (None, "choose") when any were found (an admin picks
    one) and (None, "none") when nothing was."""
    if chosen_ip:
        for helper in helpers:
            if helper["ip"] == chosen_ip:
                return helper, "ok"
        return None, "missing"
    return None, "choose" if helpers else "none"


class Discovery:
    """The SN Web Socket helpers found on the network, and the searches that find them.

    networks() is a coroutine returning the networks to search (raising DiscoveryError with a
    message for the admin when it can't tell); wanted() says whether any computer uses Automatic
    now, so automatic searches never run for nothing. Call from the event loop."""

    def __init__(self, networks, wanted, *, port: int = SN_WEB_SOCKET_PORT,
                 probe_timeout: float = PROBE_TIMEOUT, greeting_timeout: float = GREETING_TIMEOUT,
                 concurrency: int = CONCURRENCY):
        self._networks = networks
        self._wanted = wanted
        self.port = port
        self._probe_timeout = probe_timeout
        self._greeting_timeout = greeting_timeout
        self._concurrency = concurrency
        self._task: asyncio.Task | None = None
        self._auto_at = float("-inf")    # when the last search of any kind started (monotonic)
        self._admin_at = float("-inf")   # when the dashboard last asked for one (monotonic)
        self.helpers: list[dict] = []    # [{"ip", "url", "version", "found_at"}], by address
        self.networks: list[str] = []    # what the last search covered
        self.scanned_at: float | None = None
        self.error: str | None = None
        self.scans = 0                   # searches started (for tests and the log)

    # ------------------------------------------------------------ public

    @property
    def scanning(self) -> bool:
        return self._task is not None and not self._task.done()

    def state(self) -> dict:
        """What the dashboard shows: the helpers found, the networks searched and when."""
        return {"helpers": [dict(h) for h in self.helpers], "networks": list(self.networks),
                "scanned_at": self.scanned_at, "scanning": self.scanning, "error": self.error}

    def resolve(self, chosen_ip: str | None) -> tuple[dict | None, str]:
        """resolve() against what was found last."""
        return resolve(self.helpers, chosen_ip)

    def scan_soon(self) -> bool:
        """Search in the background if a computer uses Automatic and no search started in the
        last AUTO_SCAN_GAP. True if one was started. Never raises."""
        try:
            if self.scanning or time.monotonic() - self._auto_at < AUTO_SCAN_GAP:
                return False
            loop = asyncio.get_running_loop()
            if not self._wanted():
                return False
        except RuntimeError:   # no event loop: nothing to search from
            return False
        except Exception as err:  # noqa: BLE001 - a search is never worth an error
            log.warning("Couldn't tell whether to look for card encoders: %r", err)
            return False
        self._start(loop)
        return True

    def admin_wait(self) -> float:
        """Seconds until the dashboard may search again (0: now)."""
        return max(0.0, ADMIN_SCAN_GAP - (time.monotonic() - self._admin_at))

    async def find(self) -> dict:
        """The dashboard's Find encoders: search now (or join the search under way) and return
        state(). The caller checks admin_wait() first."""
        self._admin_at = time.monotonic()
        if not self.scanning:
            self._start(asyncio.get_running_loop())
        await asyncio.shield(self._task)
        return self.state()

    async def close(self) -> None:
        if self.scanning:
            self._task.cancel()
            try:
                await self._task
            except BaseException:  # noqa: BLE001 - it's stopping
                pass

    # ------------------------------------------------------------ searching

    def _start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._auto_at = time.monotonic()
        self.scans += 1
        self._task = loop.create_task(self._run())

    async def _run(self) -> None:
        """One search. Never raises: problems end up in self.error and the log."""
        try:
            networks = await self._networks()
            if not networks:
                raise DiscoveryError(NO_NETWORKS)
            started = time.time()
            found = await self._search(networks[:MAX_NETWORKS])
        except DiscoveryError as err:
            self.error = str(err)
            log.info("Not looking for card encoders: %s", err)
            return
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - background tasks never raise
            self.error = FAILED
            log.warning("Looking for card encoders failed: %r", err)
            return
        self.helpers = [{"ip": str(ip), "url": f"ws://{ip}:{self.port}", "version": version,
                         "found_at": started} for ip, version in sorted(found)]
        self.networks = [str(n) for n in networks[:MAX_NETWORKS]]
        self.scanned_at = started
        self.error = None
        log.info("Looked for card encoders on %s: %s", describe(self.networks),
                 ", ".join(h["ip"] for h in self.helpers) or "none found")

    async def _search(self, networks) -> list[tuple[ipaddress.IPv4Address, str]]:
        limit = asyncio.Semaphore(self._concurrency)
        timeout = aiohttp.ClientTimeout(total=self._probe_timeout + self._greeting_timeout + 1,
                                        sock_connect=self._probe_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async def probe(ip):
                async with limit:
                    return ip, await self._probe(session, ip)
            results = await asyncio.gather(*(probe(ip) for network in networks
                                             for ip in addresses(network)))
        return [(ip, version) for ip, version in results if version is not None]

    async def _probe(self, session: aiohttp.ClientSession, ip) -> str | None:
        """SN Web Socket's version if it's listening at this address, else None. First a plain
        connection (most addresses stop here), then the WebSocket and its greeting."""
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(str(ip), self.port),
                                               self._probe_timeout)
        except (OSError, asyncio.TimeoutError):
            return None
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        try:
            async with asyncio.timeout(self._probe_timeout + self._greeting_timeout):
                async with session.ws_connect(f"ws://{ip}:{self.port}",
                                              timeout=aiohttp.ClientWSTimeout(ws_close=1),
                                              max_msg_size=MAX_GREETING) as ws:
                    msg = await ws.receive()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            return None
        if msg.type != aiohttp.WSMsgType.TEXT:
            return None
        return greeting(msg.data)
