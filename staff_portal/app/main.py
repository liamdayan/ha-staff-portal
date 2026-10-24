"""Staff Portal: a locked-down, branded staff web app in front of Home Assistant.

Two web servers run in this process:
  * Admin (port 8099) is only reachable through Home Assistant ingress, so only HA admins see it.
  * Staff (port 8787) is the phone app; expose it through your Cloudflare Tunnel.
Staff phones never receive Home Assistant credentials. Every request is checked against the
phone's role before it is forwarded.
"""

import asyncio
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import time

import segno
from aiohttp import web

from branding import Branding
from db import Database, check_pin, hash_pin
from layout import (apply_auto_order, apply_phone_order, clean_layout, clean_phone_order,
                    clean_phone_pages, expand_page_choice, keep_removed_groups, layout_overrides,
                    visible_layout)
from ha import HomeAssistant, HomeAssistantError, LinkError
import transfer
from connecteam import Connecteam, ConnecteamError
from permissions import (ACTIONS, ENTITY_ID_RE, ActionRejected, build_service_call,
                         controllable, describe_action, domain_of, offered)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8099"))
STAFF_PORT = int(os.environ.get("STAFF_PORT", "8787"))
# Supervisor ingress always connects from 172.30.32.2.
ADMIN_ALLOWED_IPS = set(os.environ.get("ADMIN_ALLOWED_IPS", "172.30.32.2,127.0.0.1").split(","))
HERE = os.path.dirname(os.path.abspath(__file__))
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_LOGO_BYTES = 5 * 1024 * 1024
PIN_FAILURE_MEMORY = 15 * 60  # a phone forgets wrong-PIN attempts after this quiet time
STAFF_LOCK_SECONDS = 15 * 60  # how long a person is locked out after too many wrong PINs
DAILY_WRONG_PINS = 20         # this many wrong PINs on a shared device within DAILY_WINDOW lock it
DAILY_WINDOW = 24 * 3600
UNLOCK_PER_MINUTE = 15        # sign-in attempts per device per minute
MAX_SESSIONS = 5000           # PIN sessions kept in memory at most

log = logging.getLogger("staff_portal")


def read_version() -> str:
    """Add-on version from config.yaml (copied into the image; one level up in development)."""
    for path in (os.path.join(HERE, "config.yaml"), os.path.join(HERE, "..", "config.yaml")):
        try:
            with open(path, encoding="utf-8") as f:
                match = re.search(r'^version:\s*"?([^"\n]+)"?', f.read(), re.M)
        except OSError:
            continue
        if match:
            return match.group(1).strip()
    return "dev"


def asset_fingerprint(folder: str) -> str:
    """Short hash of a folder's files. Added to script/style URLs so every update makes
    browsers fetch the new files instead of reusing old cached copies."""
    digest = hashlib.sha1()
    root = os.path.join(HERE, folder)
    for name in sorted(os.listdir(root)):
        with open(os.path.join(root, name), "rb") as f:
            digest.update(name.encode() + f.read())
    return digest.hexdigest()[:10]


VERSION = read_version()
ADMIN_ASSETS = asset_fingerprint("admin")
STAFF_ASSETS = asset_fingerprint("staff")
db = Database(DATA_DIR)
ha = HomeAssistant()
branding = Branding(DATA_DIR)


def connecteam_settings() -> tuple[bool, str, str, str, int]:
    s = db.settings()
    return (s.get("connecteam_enabled") == "1", s.get("connecteam_client_id", ""),
            s.get("connecteam_client_secret", ""), s.get("connecteam_region", "global"),
            max(30, db.setting_int("connecteam_poll_minutes")))


def connecteam_wanted() -> bool:
    """Whether anyone needs the time clock checked (so Connecteam isn't asked for nothing)."""
    return db.conn.execute(
        "SELECT 1 FROM staff WHERE connecteam_user_id IS NOT NULL AND require_clock_in = 1 "
        "AND status != 'disabled' LIMIT 1").fetchone() is not None


def load_connecteam_usage() -> dict:
    try:
        usage = json.loads(db.settings().get("connecteam_usage") or "{}")
    except ValueError:
        return {}
    return usage if isinstance(usage, dict) and isinstance(usage.get("count"), int) else {}


connecteam = Connecteam(
    connecteam_settings, connecteam_wanted,
    (load_connecteam_usage, lambda u: db.update_settings({"connecteam_usage": json.dumps(u)})),
    f"HomeAssistant-StaffPortal/{VERSION} (+https://github.com/liamdayan/ha-staff-portal)")


def needs_clock_in(member: dict | None) -> bool:
    """Whether this person may only sign in while clocked in (and Connecteam is turned on)."""
    return bool(member and member.get("connecteam_user_id") and member.get("require_clock_in")
                and connecteam_settings()[0])


def clock_block(member: dict | None) -> str | None:
    """None if this person may be signed in now, else 'clocked_out' or 'clock_unknown'."""
    if not needs_clock_in(member):
        return None
    status = connecteam.status(member["connecteam_user_id"])
    return None if status == "in" else ("clocked_out" if status == "out" else "clock_unknown")


CLOCK_MESSAGES = {
    "clocked_out": "{name} isn't clocked in. Clock in on Connecteam, then sign in again.",
    "clock_unknown": "Can't check the Connecteam time clock right now, so {name} can't sign in. "
                     "Try again in a few minutes, or ask your manager.",
}

# PIN unlock sessions live in memory: an add-on restart just asks for the PIN again.
pin_sessions: dict[str, tuple[int, float, int | None]] = {}  # device, last used, staff
failures: dict[str, tuple[int, float]] = {}


class HTTPLocked(web.HTTPClientError):
    status_code = 423


# Home Assistant's internal network: Supervisor (ingress) and other add-ons such as Cloudflared.
TRUSTED_NETWORKS = [ipaddress.ip_network(n) for n in ("172.30.32.0/23", "127.0.0.0/8", "::1/128")]


MAX_TUNNEL_ADDRESSES = 10


def from_trusted_network(remote: str | None) -> bool:
    try:
        address = ipaddress.ip_address(remote or "")
    except ValueError:
        return False
    return any(address in network for network in TRUSTED_NETWORKS)


def parse_tunnel_addresses(text) -> list[str]:
    """The tunnel addresses typed in Settings: single IPs, separated by commas or spaces."""
    found = []
    for part in re.split(r"[\s,]+", str(text or "").strip()):
        if not part:
            continue
        try:
            address = str(ipaddress.ip_address(part))
        except ValueError:
            raise ValueError(f"{part[:40]} isn't an IP address. Enter the address of the computer "
                             "that runs cloudflared, such as 192.168.1.40.") from None
        if address not in found:
            found.append(address)
    if len(found) > MAX_TUNNEL_ADDRESSES:
        raise ValueError(f"Enter at most {MAX_TUNNEL_ADDRESSES} tunnel addresses.")
    return found


def from_tunnel(remote: str | None, addresses: list[str] | None = None) -> bool:
    """Whether a connection comes from the Cloudflare Tunnel: the Cloudflared add-on (Home
    Assistant's own network) or a tunnel computer listed in Settings."""
    if from_trusted_network(remote):
        return True
    if addresses is None:
        addresses = parse_tunnel_addresses(db.settings().get("tunnel_addresses"))
    try:
        return str(ipaddress.ip_address(remote or "")) in addresses
    except ValueError:
        return False


# Where requests carrying Cloudflare's headers have come from lately: {address: (last seen, count)}.
# Shown in Settings so the admin can tell which address the tunnel connects from.
tunnel_seen: dict[str, tuple[float, int]] = {}


def note_tunnel_source(request: web.Request) -> None:
    if not request.headers.get("CF-Connecting-IP"):
        return
    remote = (request.remote or "")[:64]
    _, count = tunnel_seen.get(remote, (0.0, 0))
    tunnel_seen[remote] = (time.time(), count + 1)
    if len(tunnel_seen) > 20:
        oldest = min(tunnel_seen, key=lambda k: tunnel_seen[k][0])
        tunnel_seen.pop(oldest)


def tunnel_sources() -> list[dict]:
    addresses = parse_tunnel_addresses(db.settings().get("tunnel_addresses"))
    return [{"address": addr, "last_seen": seen, "count": count,
             "allowed": from_tunnel(addr, addresses),
             "ha_network": from_trusted_network(addr)}
            for addr, (seen, count) in sorted(tunnel_seen.items(), key=lambda kv: -kv[1][0])]


def client_ip(request: web.Request) -> str:
    """The caller's address. Forwarding headers are only believed when the connection comes
    from the tunnel (the Cloudflared add-on, or a tunnel computer listed in Settings);
    anyone else could write whatever they like in them."""
    remote = request.remote or ""
    if from_tunnel(remote):
        forwarded = (request.headers.get("CF-Connecting-IP")
                     or request.headers.get("X-Forwarded-For", "").split(",")[0].strip())
        if forwarded:
            return forwarded[:64]
    return remote


def too_many_failures(ip: str, limit: int = 20, window: float = 600) -> bool:
    count, since = failures.get(ip, (0, time.time()))
    if time.time() - since > window:
        failures.pop(ip, None)
        return False
    return count >= limit


def record_failure(ip: str, window: float = 600) -> None:
    now = time.time()
    if len(failures) > 10_000:
        # Addresses can be made up (another add-on can claim any forwarded address), so the
        # table is kept bounded: old entries go first, and if that isn't enough, all of them.
        for key, (_, since) in list(failures.items()):
            if now - since > window:
                del failures[key]
        if len(failures) > 10_000:
            failures.clear()
    count, since = failures.get(ip, (0, now))
    failures[ip] = (count + 1, since)


ANY_ADDRESS = "*"           # failures counted across every address
ENROLL_FAILURES_ANYWHERE = 200


def bad_request(message: str, status: int = 400, **extra) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


# How long a request body may take to arrive. A connection that sends its headers and then
# trickles (or never sends) the body would otherwise be held open for good.
BODY_SECONDS = web.AppKey("body_seconds", float)
IDLE_SECONDS = 15   # idle connections (and ones that never finish their headers) are closed


async def read_json(request: web.Request) -> dict:
    try:
        async with asyncio.timeout(request.app.get(BODY_SECONDS, 10)):
            data = await request.json()
    except TimeoutError:
        raise web.HTTPRequestTimeout(text="The request took too long to arrive.") from None
    except (ValueError, RecursionError):
        # Not JSON, not UTF-8, absurdly long numbers or deep nesting: all just a bad request.
        raise web.HTTPBadRequest(text="invalid JSON") from None
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="expected an object")
    return data


def clean_name(value, limit: int = 60) -> str:
    return str(value or "").strip()[:limit]


class AreaLayout:
    """Which Staff Access areas each device is in, and which heading it's shown under.

    A device is in an area if it was added to it directly or sits in one of the Home Assistant
    areas the area includes. It's shown under its first area (in the order set in Settings),
    otherwise under its Home Assistant area, otherwise under "Other".
    """

    def __init__(self, areas: list[dict], entity_ha_area: dict[str, str], ha_names: dict[str, str],
                 tucked_away: set[str] | frozenset = frozenset()):
        self.areas = areas
        self.entity_ha_area = entity_ha_area
        self.ha_names = ha_names
        order = {a["id"]: i for i, a in enumerate(areas)}
        by_ha_area: dict[str, list[int]] = {}
        self.membership: dict[str, list[int]] = {}
        for area in areas:
            for eid in area["entities"]:
                self.membership.setdefault(eid, []).append(area["id"])
            for ha_area in area["ha_areas"]:
                by_ha_area.setdefault(ha_area, []).append(area["id"])
        for eid, ha_area in entity_ha_area.items():
            # Config/diagnostic/hidden entities (restart buttons, child locks, calibration)
            # never come along with a whole Home Assistant area; add them to an area
            # individually if staff really need them.
            if eid in tucked_away:
                continue
            for area_id in by_ha_area.get(ha_area, []):
                if area_id not in self.membership.setdefault(eid, []):
                    self.membership[eid].append(area_id)
        for ids in self.membership.values():
            ids.sort(key=order.get)
        self.names = {a["id"]: a["name"] for a in areas}
        self.order = order

    def ha_area_name(self, entity_id: str) -> str:
        ha_area = self.entity_ha_area.get(entity_id)
        return self.ha_names.get(ha_area, ha_area or "")

    def group(self, entity_id: str) -> tuple[int, str]:
        """(sort rank, heading) for the section a device is listed under."""
        ids = self.membership.get(entity_id)
        if ids:
            return self.order[ids[0]], self.names[ids[0]]
        ha_name = self.ha_area_name(entity_id)
        if ha_name:
            return len(self.areas), ha_name
        return len(self.areas) + 1, ""


async def area_layout(max_age: float = 300.0) -> AreaLayout:
    entity_ha_area, ha_names = await ha.area_info(max_age)
    return AreaLayout(db.areas(), entity_ha_area, ha_names, ha.tucked_away())


LEVEL_RANK = {"view": 1, "control": 2}


def role_access(role_id: int | None, layout: AreaLayout) -> dict[str, str]:
    """Effective {entity_id: 'view'|'control'} for a role: the higher of the device's own
    setting and the settings of every area it's in."""
    if not role_id:
        return {}
    access = dict(db.role_entities(role_id))
    area_levels = db.role_areas(role_id)
    if area_levels:
        for eid, area_ids in layout.membership.items():
            best = max((area_levels[a] for a in area_ids if a in area_levels),
                       key=LEVEL_RANK.get, default=None)
            if best and LEVEL_RANK[best] > LEVEL_RANK.get(access.get(eid), 0):
                access[eid] = best
    return {eid: "control" if lvl == "control" and controllable(eid) else "view"
            for eid, lvl in access.items() if offered(eid)}


def phone_access(device: dict, layout: AreaLayout, layout_data: dict | None = None) -> dict[str, str]:
    """What one phone may see and control: its role, adjusted by any access overrides on the
    layout items it shows. layout_data: the layout to use (the saved one unless previewing)."""
    access = role_access(device["role_id"], layout)
    data = db.layout() if layout_data is None else layout_data
    for eid, level in layout_overrides(data, layout, device_pages(device, data)).items():
        if level == "none":
            access.pop(eid, None)
        elif offered(eid):
            access[eid] = "control" if level == "control" and controllable(eid) else "view"
    return access


def entity_view(entity_id: str, state: dict | None, layout: AreaLayout,
                level: str | None = None) -> dict:
    attrs = (state or {}).get("attributes", {})
    keep = (
        "unit_of_measurement", "device_class", "brightness", "current_temperature", "temperature",
        "target_temp_high", "target_temp_low", "hvac_modes", "hvac_action", "min_temp",
        "max_temp", "operation_list", "operation_mode", "current_position", "icon",
        "min", "max", "step", "options",
    )
    return {
        "entity_id": entity_id,
        "domain": domain_of(entity_id),
        "name": attrs.get("friendly_name") or entity_id,
        "area": layout.group(entity_id)[1],
        "group_rank": layout.group(entity_id)[0],
        "ha_area": layout.ha_area_name(entity_id),
        "area_ids": layout.membership.get(entity_id, []),
        "state": (state or {}).get("state", "unavailable"),
        "attributes": {k: attrs[k] for k in keep if k in attrs},
        "last_changed": (state or {}).get("last_changed"),
        "level": level,
        "controllable": controllable(entity_id),
    }


# ---------------------------------------------------------------- staff app

def render_staff_index(enroll_code: str | None = None, preview: bool = False) -> web.Response:
    """The staff app's page. preview: the copy shown in the dashboard's layout preview, served
    from the dashboard's address (so its links are relative) and framed by the dashboard."""
    s = db.settings()
    with open(os.path.join(HERE, "staff", "index.html"), encoding="utf-8") as f:
        html = f.read()
    if preview:
        html = (html.replace('<link rel="manifest" href="{{MANIFEST}}">\n', "")
                .replace('href="/icon-', 'href="icon-').replace('src="/icon-', 'src="icon-')
                .replace("</head>", '<meta name="preview" content="1">\n</head>'))
    manifest = "/manifest.webmanifest" + (f"?e={enroll_code}" if enroll_code else "")
    replacements = {
        "{{APP_NAME}}": s["app_name"],
        "{{PROPERTY_NAME}}": s["property_name"],
        "{{ACCENT}}": s["accent_color"],
        "{{MANIFEST}}": manifest,
    }
    for key, value in replacements.items():
        html = html.replace(key, _esc(value))
    # Script and styles are built into the page (which is never cached) so a phone can't end up
    # running an old script after an update. A per-response nonce lets only these run.
    nonce = secrets.token_urlsafe(16)
    with open(os.path.join(HERE, "staff", "app.css"), encoding="utf-8") as f:
        css = f.read()
    with open(os.path.join(HERE, "staff", "app.js"), encoding="utf-8") as f:
        script = f.read()
    html = html.replace('<link rel="stylesheet" href="/static/app.css">',
                        f'<style nonce="{nonce}">\n{css}\n</style>')
    html = html.replace('<script src="/static/app.js"></script>',
                        f'<script nonce="{nonce}">\n{script}\n</script>')
    framed_by = "'self'" if preview else "'none'"   # only the dashboard may frame the preview
    csp = (f"default-src 'self'; img-src 'self' data:; style-src 'self' 'nonce-{nonce}'; "
           f"script-src 'self' 'nonce-{nonce}'; connect-src 'self'; frame-ancestors {framed_by}; "
           "base-uri 'none'; form-action 'none'")
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store", "Content-Security-Policy": csp,
                                 "X-Content-Type-Options": "nosniff"})


def _esc(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


async def staff_index(request: web.Request):
    return render_staff_index()


async def staff_enroll_page(request: web.Request):
    code = request.match_info["code"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", code):
        code = None
    return render_staff_index(code)


async def staff_manifest(request: web.Request):
    s = db.settings()
    code = request.query.get("e", "")
    start = f"/e/{code}" if re.fullmatch(r"[A-Za-z0-9_-]{16,64}", code) else "/"
    manifest = {
        "name": s["property_name"],
        "short_name": s["app_name"],
        "start_url": start,
        "scope": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": s["accent_color"],
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return web.json_response(
        manifest, content_type="application/manifest+json", headers={"Cache-Control": "no-store"}
    )


async def staff_icon(request: web.Request):
    size = int(request.match_info["size"])
    if size not in (180, 192, 512):
        raise web.HTTPNotFound()
    png = branding.icon(size, db.settings())
    return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "max-age=300"})


async def logo(request: web.Request):
    png = branding.logo_png()
    if png is None:
        raise web.HTTPNotFound()
    return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "max-age=300"})


async def service_worker(request: web.Request):
    return web.FileResponse(
        os.path.join(HERE, "staff", "sw.js"),
        headers={"Content-Type": "text/javascript", "Cache-Control": "no-cache"},
    )


def json_error(exc_class, error: str, **extra):
    return exc_class(text=json.dumps({"error": error, **extra}), content_type="application/json")


def authenticate(request: web.Request, need_unlock: bool = True,
                 active: bool = False, control: bool = False) -> tuple[dict, dict | None]:
    """Return (phone, signed-in staff member or None), or raise the error the app should show.

    `active` marks a request someone actually made (a control, saving an order). Only those,
    and polls sent right after the person touched the screen (X-Active), keep a PIN session
    alive; background refreshes don't, so an unattended phone still signs out when idle.
    `control` marks a control being used: only then (and at sign-in) must a clocked-in-only
    person be on the clock. Reading doesn't check, so it never costs a Connecteam request."""
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    device = db.device_by_token(token) if token else None
    if not device or device["status"] in ("revoked", "pending"):
        raise json_error(web.HTTPUnauthorized, "signed_out")
    if device["status"] == "locked":
        raise json_error(HTTPLocked, "locked")
    if not device["last_seen"] or time.time() - device["last_seen"] > 60:
        db.touch_device(device["id"])
    shared = device["signin_mode"] == "staff"
    if not shared and not device["pin_hash"] and db.settings().get("require_pin") == "1":
        raise json_error(web.HTTPForbidden, "pin_not_set")
    if not need_unlock or not (shared or device["pin_hash"]):
        return device, None

    key = request.headers.get("X-Session", "")
    session = pin_sessions.get(key)
    timeout = db.setting_int("pin_timeout_minutes") * 60
    if not session or session[0] != device["id"] or time.time() - session[1] > timeout:
        pin_sessions.pop(key, None)
        raise json_error(web.HTTPForbidden, "pin_required")
    member = None
    if shared:
        member = db.staff(session[2]) if session[2] else None
        if not member or member["status"] != "active" or not db.staff_allowed_on(member["id"], device):
            pin_sessions.pop(key, None)
            raise json_error(web.HTTPForbidden, "pin_required")
        blocked = clock_block(member) if control else None
        if blocked:
            # Clocked out (or the time clock can't be checked): this person's session ends.
            pin_sessions.pop(key, None)
            db.log(actor_name(device, member),
                   "Signed out: clocked out in Connecteam" if blocked == "clocked_out"
                   else "Signed out: Connecteam time clock unavailable",
                   role=device["role_name"], ok=False)
            raise json_error(web.HTTPForbidden, blocked,
                             message=CLOCK_MESSAGES[blocked].format(name=member["name"]))
        if not member["last_seen"] or time.time() - member["last_seen"] > 60:
            db.update_staff(member["id"], last_seen=time.time())
    elif session[2] is not None:
        # Phone was switched out of shared mode while someone was signed in.
        pin_sessions.pop(key, None)
        raise json_error(web.HTTPForbidden, "pin_required")
    if active or request.headers.get("X-Active") == "1":
        pin_sessions[key] = (device["id"], time.time(), session[2])
    return device, member


# Speed limits on controls: real relays, pumps and lock motors shouldn't be cycled faster
# than a person could tap.
ACTIONS_PER_MINUTE = 60
ENTITY_MIN_INTERVAL = 1.0
REPEAT_WINDOW = 3.0          # the same command again this soon is a double tap: ignored
recent_actions: dict[int, list[float]] = {}
last_entity_action: dict[tuple[int, str], float] = {}
last_command: dict[tuple[int, str], tuple[str, str, float]] = {}   # (service, call, when)

# On/off commands for something already in that state do nothing, so they aren't sent or logged.
ALREADY = {"turn_on": "on", "turn_off": "off"}


def repeated(device_id: int, entity_id: str, service: str, call: dict, state: dict | None) -> bool:
    """Whether this command would change nothing: the same one from this device a moment ago,
    or switching something on/off that already is."""
    now = time.time()
    if len(last_command) > 5000:
        for key, (_, _, t) in list(last_command.items()):
            if now - t > 60:
                del last_command[key]
    signature = json.dumps(call, sort_keys=True, default=str)
    prev = last_command.get((device_id, entity_id))
    if prev and prev[0] == service and prev[1] == signature and now - prev[2] < REPEAT_WINDOW:
        return True
    if ALREADY.get(service) and len(call) == 1 and (state or {}).get("state") == ALREADY[service]:
        return True
    last_command[(device_id, entity_id)] = (service, signature, now)
    return False


def phone_too_busy(device_id: int) -> bool:
    now = time.time()
    times = [t for t in recent_actions.get(device_id, []) if now - t < 60]
    busy = len(times) >= ACTIONS_PER_MINUTE
    if not busy:
        times.append(now)
    recent_actions[device_id] = times
    return busy


def entity_too_soon(device_id: int, entity_id: str) -> bool:
    now = time.time()
    if len(last_entity_action) > 5000:
        for key, t in list(last_entity_action.items()):
            if now - t > 60:
                del last_entity_action[key]
    return now - last_entity_action.get((device_id, entity_id), 0) < ENTITY_MIN_INTERVAL


def actor_name(device: dict, member: dict | None) -> str:
    return f"{member['name']} on {device['name']}" if member else device["name"]


async def api_enroll(request: web.Request):
    ip = client_ip(request)
    # Per address, and across all addresses (an address can be faked from Home Assistant's
    # own network, so the per-address limit alone isn't enough).
    if too_many_failures(ip) or too_many_failures(ANY_ADDRESS, ENROLL_FAILURES_ANYWHERE):
        return bad_request("Too many attempts. Try again later.", 429)
    data = await read_json(request)
    claimed = db.claim_enroll_code(str(data.get("code", "")))
    if not claimed:
        record_failure(ip)
        record_failure(ANY_ADDRESS)
        return bad_request("This setup code is invalid, already used, or expired.", 404)
    device, token = claimed
    db.log(device["name"], "Phone set up", ip=ip)
    return web.json_response({"token": token})


async def api_me(request: web.Request):
    device, _ = authenticate(request, need_unlock=False)
    s = db.settings()
    shared = device["signin_mode"] == "staff"
    pin_only = shared and bool(device["pin_only"])
    return web.json_response({
        "device": device["name"],
        "kind": device["kind"],
        "role": device["role_name"],
        "shared": shared,
        # PIN-only devices never get the list of names.
        "pin_only": pin_only,
        "staff": [{"id": m["id"], "name": m["name"],
                   "off_clock": clock_block(m) is not None}
                  for m in db.staff_for_device(device)] if shared and not pin_only else [],
        "pin_required": shared or bool(device["pin_hash"]),
        "property_name": s["property_name"],
        "app_name": s["app_name"],
        "accent_color": s["accent_color"],
        "has_logo": branding.has_logo(),
        "pin_timeout_minutes": db.setting_int("pin_timeout_minutes"),
    })


async def api_unlock(request: web.Request):
    # Read the body, then wait for this device's turn: sign-in attempts on one device run one at a
    # time, so each sees the wrong-PIN count the previous one left (thousands of held-open
    # requests once shared one stale count and never triggered the lock). The slow PIN checks run
    # off the main thread, so a stream of sign-ins can't stall the rest of the add-on.
    data = await read_json(request)
    device, _ = authenticate(request, need_unlock=False)
    if unlock_too_busy(device["id"]):
        return bad_request("Too many sign-in attempts. Wait a minute and try again.", 429)
    async with unlock_locks.setdefault(device["id"], asyncio.Lock()):
        device, _ = authenticate(request, need_unlock=False)   # fresh, now that it's our turn
        if device["signin_mode"] != "staff":
            return await unlock_device(request, device, data)
        if device["pin_only"]:
            return await unlock_by_pin(request, device, data)
        return await unlock_named(request, device, data)


unlock_locks: dict[int, asyncio.Lock] = {}
recent_unlocks: dict[int, list[float]] = {}


def unlock_too_busy(device_id: int) -> bool:
    """More sign-in attempts from one device in a minute than people really make."""
    now = time.time()
    times = [t for t in recent_unlocks.get(device_id, []) if now - t < 60]
    busy = len(times) >= UNLOCK_PER_MINUTE
    if not busy:
        times.append(now)
    recent_unlocks[device_id] = times
    return busy


async def pin_matches(pin: str, stored: str | None) -> bool:
    """check_pin off the main thread (it's deliberately slow). Malformed PINs never match."""
    if not stored or not parse_pin(pin):
        return False
    return await asyncio.to_thread(check_pin, pin, stored)


async def unlock_device(request: web.Request, device: dict, data: dict) -> web.Response:
    """A phone or computer with its own PIN. Wrong PINs add up until it locks."""
    if not device["pin_hash"]:
        return web.json_response({"session": ""})
    if await pin_matches(str(data.get("pin", "")), device["pin_hash"]):
        db.update_device(device["id"], failed_pins=0)
        return web.json_response({"session": new_session(device, None)})
    max_attempts = db.setting_int("max_pin_attempts")
    attempts = device["failed_pins"] + 1
    if attempts >= max_attempts:
        db.update_device(device["id"], failed_pins=attempts, status="locked")
        db.log(device["name"], "Phone locked after wrong PINs", role=device["role_name"],
               ok=False, ip=client_ip(request))
        return bad_request("locked", 423)
    db.update_device(device["id"], failed_pins=attempts)
    return bad_request(f"Wrong PIN. {max_attempts - attempts} tries left.", 403)


def shared_wrong_pin(device: dict, ip: str, limit: int) -> tuple[int, web.Response | None]:
    """Count a wrong PIN on a shared device. Returns (wrong PINs in a row, the "locked" response
    if this one locked the device).

    Two limits. `limit` wrong in a row locks it; that count is forgotten after a quiet spell,
    so a day of ordinary typos doesn't. And DAILY_WRONG_PINS within DAILY_WINDOW lock it however
    slowly they come, so guessing can't be paced to stay under the first limit forever."""
    now = time.time()
    quiet = now - (device["failed_at"] or 0) > PIN_FAILURE_MEMORY
    attempts = (0 if quiet else device["failed_pins"]) + 1
    since = device["failed_day_since"]
    if not since or now - since > DAILY_WINDOW:
        since, today = now, 1
    else:
        today = device["failed_day_count"] + 1
    fields = {"failed_pins": attempts, "failed_at": now,
              "failed_day_count": today, "failed_day_since": since}
    if attempts >= limit or today >= DAILY_WRONG_PINS:
        db.update_device(device["id"], status="locked", **fields)
        db.log(device["name"], "Phone locked after wrong PINs" if attempts >= limit
               else f"Phone locked after {today} wrong PINs in a day",
               role=device["role_name"], ok=False, ip=ip)
        return attempts, bad_request("locked", 423)
    db.update_device(device["id"], **fields)
    return attempts, None


async def unlock_named(request: web.Request, device: dict, data: dict) -> web.Response:
    """Shared device with a list of names: the person picked their name, then typed their PIN."""
    ip = client_ip(request)
    max_attempts = db.setting_int("max_pin_attempts")
    try:
        member = db.staff(int(data.get("staff_id")))
    except (TypeError, ValueError):
        member = None
    if not member or member["status"] == "disabled" or not db.staff_allowed_on(member["id"], device):
        return bad_request("Pick your name again.", 404)
    locked_msg = (f"{member['name']} is locked for a while after too many wrong PINs. "
                  "Try again later, or ask your manager to unlock you.")
    if member["status"] == "locked" or (member["locked_until"] or 0) > time.time():
        return bad_request(locked_msg, 403, staff_locked=True)
    ok = await pin_matches(str(data.get("pin", "")), member["pin_hash"])
    # Fresh: an attempt on another device may have changed this person's count meanwhile.
    member = db.staff(member["id"])
    if not member or member["status"] == "disabled":
        return bad_request("Pick your name again.", 404)
    if member["status"] == "locked" or (member["locked_until"] or 0) > time.time():
        return bad_request(locked_msg, 403, staff_locked=True)
    if ok:
        # Only a proven PIN may cost a Connecteam request (someone who just clocked in).
        await recheck_clock(member["id"])
        return staff_signed_in(device, db.staff(member["id"]), ip)

    attempts = member["failed_pins"] + 1
    # Also counted per device, so cycling through names can't be used to guess PINs.
    _, locked = shared_wrong_pin(device, ip, max_attempts * 3)
    if locked:
        return locked
    if attempts >= max_attempts:
        # A temporary lock that doesn't end their sessions on other phones, so wrong guesses
        # can't be used to shut a colleague out.
        db.update_staff(member["id"], failed_pins=0, locked_until=time.time() + STAFF_LOCK_SECONDS)
        db.log(actor_name(device, member), "Locked for a while after wrong PINs",
               role=device["role_name"], ok=False, ip=ip)
        return bad_request(locked_msg, 403, staff_locked=True)
    db.update_staff(member["id"], failed_pins=attempts)
    return bad_request(f"Wrong PIN. {max_attempts - attempts} tries left.", 403)


def staff_signed_in(device: dict, member: dict, ip: str) -> web.Response:
    """The right PIN for a staff member on a shared device: sign them in, unless they must be
    clocked in and aren't. Only this person's own wrong-PIN count is cleared. The device's
    count isn't, so signing in as yourself can't be used to keep guessing other people's PINs."""
    blocked = clock_block(member)
    if blocked:
        db.update_staff(member["id"], failed_pins=0)
        db.log(actor_name(device, member), "Blocked sign-in: not clocked in"
               if blocked == "clocked_out" else "Blocked sign-in: time clock unavailable",
               role=device["role_name"], ok=False, ip=ip)
        return bad_request(CLOCK_MESSAGES[blocked].format(name=member["name"]), 403,
                           clock=blocked)
    db.update_staff(member["id"], failed_pins=0, locked_until=None, last_seen=time.time())
    db.log(actor_name(device, member), "Signed in", role=device["role_name"], ip=ip)
    return web.json_response({"session": new_session(device, member),
                              "staff_name": member["name"]})


async def unlock_by_pin(request: web.Request, device: dict, data: dict) -> web.Response:
    """Sign-in on a PIN-only shared device: whoever's PIN it is. With no name chosen, a guess
    could match anyone, so wrong PINs count against the device, which locks after the normal
    limit (not three times it, as when people pick their name first)."""
    pin = str(data.get("pin", ""))
    ip = client_ip(request)
    max_attempts = db.setting_int("max_pin_attempts")
    candidates = db.staff_pins(device) if parse_pin(pin) else []
    matches = await asyncio.to_thread(
        lambda: [sid for sid, stored in candidates if check_pin(pin, stored)])
    if len(matches) > 1:
        db.log(device["name"], "Blocked sign-in: PIN used by more than one person",
               role=device["role_name"], ok=False, ip=ip)
        return bad_request("That PIN belongs to more than one person, so it can't be used here. "
                           "Ask your manager to give each person their own PIN.", 403)
    member = db.staff(matches[0]) if matches else None
    if member and member["status"] != "disabled":
        if member["status"] == "locked" or (member["locked_until"] or 0) > time.time():
            return bad_request(f"{member['name']} is locked for a while after too many wrong PINs. "
                               "Try again later, or ask your manager to unlock you.", 403,
                               staff_locked=True)
        # Only a proven PIN may cost a Connecteam request (someone who just clocked in).
        await recheck_clock(member["id"])
        return staff_signed_in(device, db.staff(member["id"]), ip)
    attempts, locked = shared_wrong_pin(device, ip, max_attempts)
    if locked:
        return locked
    return bad_request(f"Wrong PIN. {max_attempts - attempts} tries left.", 403)


async def pin_taken(pin: str, except_id: int | None = None) -> bool:
    """Whether another staff member already has this PIN. PINs must be unique, because on
    PIN-only devices the PIN alone says who someone is."""
    others = [(sid, stored) for sid, stored in db.staff_pins() if sid != except_id]
    return await asyncio.to_thread(lambda: any(check_pin(pin, stored) for _, stored in others))


PIN_TAKEN = "Someone else already uses that PIN. Give each person their own PIN."


def new_session(device: dict, member: dict | None) -> str:
    session = secrets.token_urlsafe(24)
    now = time.time()
    if len(pin_sessions) >= MAX_SESSIONS:
        # Forget sessions that have timed out; if there are still too many, the oldest go.
        timeout = db.setting_int("pin_timeout_minutes") * 60
        for key, (_, used, _) in list(pin_sessions.items()):
            if now - used > timeout:
                del pin_sessions[key]
        oldest = sorted(pin_sessions, key=lambda k: pin_sessions[k][1])
        for key in oldest[:len(pin_sessions) - MAX_SESSIONS + 1]:
            del pin_sessions[key]
    pin_sessions[session] = (device["id"], now, member["id"] if member else None)
    return session


async def api_signout(request: web.Request):
    device, member = authenticate(request)
    pin_sessions.pop(request.headers.get("X-Session", ""), None)
    if member:
        db.log(actor_name(device, member), "Signed out", role=device["role_name"])
    return web.json_response({"ok": True})


async def api_entities(request: web.Request):
    device, member = authenticate(request)
    layout_data = db.layout()
    try:
        layout = await area_layout()
        allowed = phone_access(device, layout, layout_data)
        states = await ha.states() if allowed else {}
    except HomeAssistantError as err:
        log.warning("Home Assistant unavailable: %s", err)
        return bad_request("Can't reach the system right now.", 503)
    entities = [entity_view(eid, states.get(eid), layout, lvl) for eid, lvl in allowed.items()]
    entities.sort(key=lambda e: (e["group_rank"], e["area"].lower(), e["name"].lower()))
    can_reorder = db.settings().get("allow_phone_reorder") == "1"
    order = db.phone_order(order_owner(device, member)) if can_reorder else None
    phone_layout = visible_layout(layout_data, allowed, layout, device_pages(device, layout_data))
    if phone_layout:
        phone_layout = apply_phone_order(phone_layout, order)
    else:
        entities = apply_auto_order(entities, order)
    return web.json_response({
        "entities": entities,
        "layout": phone_layout,
        "staff_name": member and member["name"],
        "can_reorder": can_reorder,
        "control_lock": db.settings().get("control_lock") == "1",
        "control_lock_minutes": db.setting_int("control_lock_minutes"),
        "custom_order": bool(order),
        "order": order or {"pages": [], "items": {}},
    })


def order_owner(device: dict, member: dict | None) -> str:
    """Whose arrangement this is: the signed-in person on a shared phone, otherwise the phone."""
    return f"s:{member['id']}" if member else f"d:{device['id']}"


async def api_save_order(request: web.Request):
    device, member = authenticate(request, active=True)
    if db.settings().get("allow_phone_reorder") != "1":
        return bad_request("Rearranging is turned off for this phone.", 403)
    try:
        order = clean_phone_order(await read_json(request))
    except ValueError as err:
        return bad_request(str(err))
    db.set_phone_order(order_owner(device, member), order)
    db.log(actor_name(device, member), "Rearranged the phone", role=device["role_name"])
    return web.json_response({"ok": True})


async def api_reset_order(request: web.Request):
    device, member = authenticate(request, active=True)
    db.set_phone_order(order_owner(device, member), None)
    db.log(actor_name(device, member), "Reset the phone's arrangement", role=device["role_name"])
    return web.json_response({"ok": True})


async def recheck_clock(staff_id) -> None:
    """Before a sign-in or a control: if this person must be clocked in and the last check didn't
    find them on the clock, ask Connecteam now (one request updates everyone). Someone the last
    check found clocked in costs nothing."""
    try:
        member = db.staff(int(staff_id))
    except (TypeError, ValueError):
        return
    if needs_clock_in(member) and clock_block(member):
        await connecteam.check(force=True)


async def api_action(request: web.Request):
    device, member = authenticate(request, active=True)
    if member and clock_block(member):
        # Signed in, but the last check didn't find them on the clock: they may have just
        # clocked in, so ask Connecteam (at most every 30 seconds) before refusing.
        await recheck_clock(member["id"])
    device, member = authenticate(request, active=True, control=True)
    data = await read_json(request)
    entity_id = str(data.get("entity_id", ""))
    action = str(data.get("action", ""))
    params = data.get("params") or {}
    ip = client_ip(request)
    actor = actor_name(device, member)
    if phone_too_busy(device["id"]):
        return bad_request("Too many changes at once. Wait a moment and try again.", 429)
    if not ENTITY_ID_RE.match(entity_id) or action not in ACTIONS.get(domain_of(entity_id), {}):
        # Never store junk a phone sends; just note that something invalid was refused.
        db.log(actor, "Blocked: invalid request", role=device["role_name"], ok=False, ip=ip)
        return bad_request("You don't have access to control that.", 403)
    try:
        allowed = phone_access(device, await area_layout())
    except HomeAssistantError as err:
        log.warning("Home Assistant unavailable: %s", err)
        return bad_request("Can't reach the system right now.", 503)
    if allowed.get(entity_id) != "control" or not isinstance(params, dict):
        db.log(actor, f"Blocked: {action}", role=device["role_name"],
               entity_id=entity_id, ok=False, ip=ip)
        return bad_request("You don't have access to control that.", 403)
    try:
        states = await ha.states()
        domain, service, call = build_service_call(entity_id, action, params, states.get(entity_id))
        if repeated(device["id"], entity_id, service, call, states.get(entity_id)):
            return web.json_response({"ok": True, "unchanged": True})
        # Only commands that would really reach the device count towards the per-device pause.
        if entity_too_soon(device["id"], entity_id):
            return bad_request("That was changed a moment ago. Wait a second and try again.", 429)
        last_entity_action[(device["id"], entity_id)] = time.time()
        await send_command(device, member, domain, service, call)
    except ActionRejected as err:
        return bad_request(str(err))
    except HomeAssistantError as err:
        log.warning("Service call failed: %s", err)
        # It never reached the device, so trying again straight away is a retry, not a double
        # tap: it must be sent (and not refused as too soon).
        last_command.pop((device["id"], entity_id), None)
        last_entity_action.pop((device["id"], entity_id), None)
        db.log(actor, action, role=device["role_name"], entity_id=entity_id,
               detail=str(err)[:200], ok=False, ip=ip)
        return bad_request("The system didn't accept that. Try again.", 502)
    detail = ", ".join(f"{k}={v}" for k, v in call.items() if k != "entity_id") or None
    db.log(actor, action, role=device["role_name"], entity_id=entity_id, detail=detail, ip=ip)
    # Written for linked users too: Home Assistant credits them, but this entry also says
    # which device and what exactly was set.
    if db.settings().get("ha_logbook") == "1":
        name = (states.get(entity_id) or {}).get("attributes", {}).get("friendly_name", entity_id)
        who = f"{member['name']} ({device['name']})" if member else device["name"]
        # The command already worked: write the logbook entry in the background so a slow or
        # failing logbook can never turn that into an error (and a second press) on the phone.
        background(ha.logbook(name, f"{describe_action(entity_id, action, call)} by {who} "
                                    "via Staff Portal", entity_id), "write logbook entry")
    return web.json_response({"ok": True})


background_tasks: set[asyncio.Task] = set()


def background(coro, what: str) -> None:
    """Run a coroutine without waiting for it; failures are logged, never raised."""
    async def run():
        try:
            await coro
        except Exception as err:  # noqa: BLE001 - nothing may escape a background task
            log.warning("Couldn't %s: %s", what, err)
    task = asyncio.create_task(run())
    background_tasks.add(task)  # keep a reference until it finishes
    task.add_done_callback(background_tasks.discard)


async def send_command(device: dict, member: dict | None, domain: str, service: str,
                       call: dict) -> bool:
    """Send as the linked HA user when there is one. Returns True if it did.

    On a shared phone the signed-in staff member's own link is used. The phone's link is
    ignored there, so Home Assistant never credits the wrong person.
    """
    linked, update = (member, db.update_staff) if member else (device, db.update_device)
    if linked.get("ha_refresh_token"):
        try:
            await ha.call_service(domain, service, call, refresh_token=linked["ha_refresh_token"])
            if linked.get("ha_link_error"):
                update(linked["id"], ha_link_error=None)
            return True
        except LinkError as err:
            # Keep staff working: fall back to the add-on's own access and flag it for the admin.
            log.warning("Linked user for %s failed: %s", linked["name"], err)
            if linked.get("ha_link_error") != str(err):
                update(linked["id"], ha_link_error=str(err))
                db.log(actor_name(device, member), "Home Assistant user link stopped working",
                       role=device["role_name"], detail=str(err), ok=False)
    await ha.call_service(domain, service, call)
    return False


STAFF_SECURITY_HEADERS = {
    "Content-Security-Policy":
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def add_staff_headers(request: web.Request, headers) -> None:
    for key, value in STAFF_SECURITY_HEADERS.items():
        headers.setdefault(key, value)
    if request.path.startswith("/api/"):
        headers["Cache-Control"] = "no-store"
    elif request.path.startswith("/static/"):
        headers["Cache-Control"] = "no-cache"  # always check for a newer version


@web.middleware
async def staff_headers(request: web.Request, handler):
    note_tunnel_source(request)
    try:
        if (db.settings().get("phones_via_tunnel_only") == "1"
                and not from_tunnel(request.remote)):
            # Direct connections from the local network are refused; only the Cloudflare Tunnel
            # (the add-on, or a tunnel computer listed in Settings) may pass phones through.
            response = web.Response(status=403, text="Staff Portal is only available through the "
                                                     "property's secure address.")
        else:
            response = await handler(request)
    except web.HTTPException as exc:
        # Errors (signed out, locked, bad request...) get the same protective headers.
        add_staff_headers(request, exc.headers)
        raise
    except (ValueError, UnicodeError):
        response = bad_request("That request couldn't be read.")
    add_staff_headers(request, response.headers)
    return response


def staff_app() -> web.Application:
    app = web.Application(middlewares=[staff_headers], client_max_size=64 * 1024)
    app[BODY_SECONDS] = 10.0
    app.router.add_get("/", staff_index)
    app.router.add_get("/e/{code}", staff_enroll_page)
    app.router.add_get("/manifest.webmanifest", staff_manifest)
    app.router.add_get(r"/icon-{size:\d+}.png", staff_icon)
    app.router.add_get("/logo.png", logo)
    app.router.add_get("/sw.js", service_worker)
    app.router.add_static("/static/", os.path.join(HERE, "staff"))
    app.router.add_post("/api/enroll", api_enroll)
    app.router.add_get("/api/me", api_me)
    app.router.add_post("/api/unlock", api_unlock)
    app.router.add_post("/api/signout", api_signout)
    app.router.add_put("/api/order", api_save_order)
    app.router.add_delete("/api/order", api_reset_order)
    app.router.add_get("/api/entities", api_entities)
    app.router.add_post("/api/action", api_action)
    return app


# ---------------------------------------------------------------- admin dashboard

def admin_name(request: web.Request) -> str:
    return request.headers.get("X-Remote-User-Display-Name") or "Admin"


def setup_url(code: str | None) -> str | None:
    base = db.settings()["public_url"].rstrip("/")
    return f"{base}/e/{code}" if base and code else None


def device_page_choice(device: dict) -> list[str] | None:
    """A phone's page choice as stored: None for every page, otherwise page IDs and page
    groups ("group:<id>"). A damaged value means no pages rather than every page, so it can
    never widen what a phone sees."""
    raw = device.get("layout_pages")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return [str(i) for i in value] if isinstance(value, list) else []


def device_pages(device: dict, layout_data: dict | None = None) -> list[str] | None:
    """The layout pages a phone shows (its groups turned into their pages), or None for every
    page. layout_data: the layout to use (the saved one unless previewing a draft)."""
    return expand_page_choice(db.layout() if layout_data is None else layout_data,
                              device_page_choice(device))


def device_payload() -> list[dict]:
    devices = db.devices()
    owners = db.phone_order_owners()
    for d in devices:
        d["setup_url"] = setup_url(d["enroll_code"])
        d["staff_ids"] = db.device_staff_ids(d["id"])
        d["layout_pages"] = device_page_choice(d)
        d["custom_order"] = f"d:{d['id']}" in owners
    return devices


async def admin_index(request: web.Request):
    # The script and styles are built into the page rather than linked. The page is never
    # cached, so a browser or proxy (such as Cloudflare) can't pair it with an old script.
    def read(name: str) -> str:
        with open(os.path.join(HERE, "admin", name), encoding="utf-8") as f:
            return f.read()
    html = read("index.html").replace("{{VERSION}}", _esc(VERSION))
    script = read("admin.js").replace("{{SCRIPT_VERSION}}", VERSION)
    # This page runs on Home Assistant's own web address, so a script injected here could
    # reach the admin's Home Assistant session. Only the page's own script and styles may run.
    nonce = secrets.token_urlsafe(16)
    html = html.replace('<link rel="stylesheet" href="static/admin.css">',
                        f'<style nonce="{nonce}">\n{read("admin.css")}\n</style>')
    html = html.replace('<script src="static/admin.js"></script>',
                        f'<script nonce="{nonce}">\n{script}\n</script>')
    csp = (f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'nonce-{nonce}'; "
           "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
           "form-action 'none'; frame-ancestors 'self'")
    return web.Response(text=html, content_type="text/html", headers={
        "Cache-Control": "no-store", "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin"})


async def admin_state(request: web.Request):
    owners = db.phone_order_owners()
    return web.json_response({
        "settings": public_settings(),
        "connecteam": connecteam_state(),
        "tunnel_sources": tunnel_sources(),
        "roles": db.roles(),
        "devices": device_payload(),
        "staff": [{**m, "custom_order": f"s:{m['id']}" in owners,
                   "locked": m["status"] == "locked" or (m["locked_until"] or 0) > time.time(),
                   "clock": connecteam.status(m["connecteam_user_id"]) if needs_clock_in(m) else None}
                  for m in db.staff_list()],
        "areas": db.areas(),
        "layout": db.layout(),
        "has_logo": branding.has_logo(),
    })


def public_settings() -> dict:
    """Settings as the dashboard may see them: the Connecteam secret is only ever "set or not"."""
    s = db.settings()
    s["connecteam_secret_set"] = "1" if s.pop("connecteam_client_secret", "") else "0"
    s.pop("connecteam_usage", None)
    return s


def connecteam_state() -> dict:
    now = time.time()
    return {"enabled": connecteam_settings()[0], "checked_at": connecteam.checked_at,
            "last_error": connecteam.last_error, "clocked_in": len(connecteam.clocked_in),
            "halted": connecteam.halted, "needed": connecteam_wanted(),
            "paused": connecteam.pause_reason if connecteam.paused_until > now else None,
            "interval": connecteam.interval(), "used_today": connecteam.usage()["count"],
            "daily_cap": connecteam.daily_cap(),
            "account_left": connecteam.limits.get("day_left"),
            "account_limit": connecteam.limits.get("day_limit")}


async def admin_connecteam_users(request: web.Request):
    try:
        users = await connecteam.users()
    except ConnecteamError as err:
        return bad_request(str(err), 502)
    linked = {r["connecteam_user_id"]: r["name"] for r in db.conn.execute(
        "SELECT name, connecteam_user_id FROM staff WHERE connecteam_user_id IS NOT NULL")}
    # Kiosk codes stay on the server: the dashboard only learns whether there is one.
    return web.json_response({"users": [
        {"id": u["id"], "name": u["name"], "has_kiosk_code": bool(u["kiosk_code"]),
         "linked_to": linked.get(u["id"])} for u in users]})


connecteam_tested = [0.0]


async def admin_connecteam_test(request: web.Request):
    if not connecteam_settings()[0]:
        return bad_request("Turn on Connecteam and save the settings first.")
    if time.time() - connecteam_tested[0] < 10:
        return bad_request("Wait a few seconds before testing again.", 429)
    connecteam_tested[0] = time.time()
    await connecteam.check(admin=True)   # also lifts a stop after rejected keys
    if connecteam.last_error:
        return bad_request(connecteam.last_error, 502)
    try:
        users = await connecteam.users()          # reused for 5 minutes
        clocks = await connecteam.time_clock_ids()  # cached by the check above
    except ConnecteamError as err:
        return bad_request(str(err), 502)
    db.log(admin_name(request), "Tested the Connecteam connection")
    return web.json_response({"users": len(users), "time_clocks": len(clocks),
                              "clocked_in": len(connecteam.clocked_in)})


async def kiosk_pin(uid: str | None) -> str:
    """A linked person's Connecteam kiosk code, to use as their PIN."""
    if not uid:
        raise web.HTTPBadRequest(text="Only people linked to Connecteam have a kiosk code.")
    try:
        code = await connecteam.kiosk_code(uid)
    except ConnecteamError as err:
        raise web.HTTPBadRequest(text=str(err))
    if not code:
        raise web.HTTPBadRequest(text="Connecteam has no kiosk code of 4 to 8 digits for them. "
                                      "Type a PIN instead.")
    return code


def parse_connecteam_link(data: dict, staff_id: int | None = None) -> dict:
    """connecteam_user_id / require_clock_in from a staff form, validated."""
    fields = {}
    if "connecteam_user_id" in data:
        uid = data["connecteam_user_id"]
        if uid in (None, ""):
            fields["connecteam_user_id"] = None
        else:
            uid = str(uid).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", uid):
                raise web.HTTPBadRequest(text="That Connecteam user ID doesn't look right.")
            owner = db.staff_by_connecteam(uid)
            if owner and owner["id"] != staff_id:
                raise web.HTTPBadRequest(text=f"That Connecteam user is already linked to {owner['name']}.")
            fields["connecteam_user_id"] = uid
    if "require_clock_in" in data:
        fields["require_clock_in"] = 1 if data["require_clock_in"] in (True, "1", 1, "on") else 0
    return fields


async def admin_entities(request: web.Request):
    try:
        states, layout = await ha.states(max_age=0), await area_layout(max_age=0)
    except HomeAssistantError as err:
        return bad_request(f"Can't reach Home Assistant: {err}", 503)
    entities = [entity_view(eid, st, layout) for eid, st in states.items() if offered(eid)]
    entities.sort(key=lambda e: (e["group_rank"], e["area"].lower(), e["domain"], e["name"].lower()))
    ha_areas = sorted(({"id": aid, "name": name} for aid, name in layout.ha_names.items()),
                      key=lambda a: a["name"].lower())
    return web.json_response({"entities": entities, "ha_areas": ha_areas})


async def admin_create_role(request: web.Request):
    data = await read_json(request)
    name = clean_name(data.get("name"))
    if not name:
        return bad_request("Give the role a name.")
    if db.name_taken("roles", name):
        return bad_request("A role with that name already exists.")
    try:
        role_id = db.create_role(name)
    except Exception:
        return bad_request("A role with that name already exists.")
    db.log(admin_name(request), f"Created role {name}", role=name)
    return web.json_response({"id": role_id})


async def admin_duplicate_role(request: web.Request):
    role = db.role(int(request.match_info["id"]))
    if not role:
        raise web.HTTPNotFound()
    data = await read_json(request)
    name = clean_name(data.get("name"))
    if not name:
        return bad_request("Give the new role a name.")
    if db.name_taken("roles", name):
        return bad_request("A role with that name already exists.")
    try:
        role_id = db.duplicate_role(role["id"], name)
    except sqlite3.IntegrityError:
        return bad_request("A role with that name already exists.")
    db.log(admin_name(request), f"Created role {name} as a copy of {role['name']}", role=name)
    return web.json_response({"id": role_id})


async def admin_update_role(request: web.Request):
    role_id = int(request.match_info["id"])
    role = db.role(role_id)
    if not role:
        raise web.HTTPNotFound()
    data = await read_json(request)
    name = clean_name(data.get("name")) or role["name"]
    entities = data.get("entities") or {}
    if not isinstance(entities, dict):
        return bad_request("entities must be an object")
    # "control" is only kept for device types staff can actually operate.
    cleaned = {}
    for eid, lvl in entities.items():
        if not isinstance(eid, str) or not offered(eid) or lvl not in ("view", "control"):
            continue
        cleaned[eid] = "control" if lvl == "control" and controllable(eid) else "view"
    areas = None
    if isinstance(data.get("areas"), dict):
        known = {a["id"] for a in db.areas()}
        areas = {int(aid): lvl for aid, lvl in data["areas"].items()
                 if str(aid).isdigit() and int(aid) in known and lvl in ("view", "control")}
    if db.name_taken("roles", name, role_id):
        return bad_request("A role with that name already exists.")
    try:
        db.update_role(role_id, name, cleaned, areas)
    except sqlite3.IntegrityError:
        return bad_request("A role with that name already exists.")
    detail = f"{len(cleaned)} devices" + (f", {len(areas)} areas" if areas else "")
    db.log(admin_name(request), f"Updated role {name}", role=name, detail=detail)
    return web.json_response({"ok": True})


async def admin_client_error(request: web.Request):
    """Errors from the admin dashboard, written to the add-on log for troubleshooting."""
    data = await read_json(request)
    now = time.time()
    recent = [t for t in client_error_times if now - t < 60]
    client_error_times[:] = recent
    if len(recent) >= 20:
        return web.json_response({"ok": False}, status=429)
    client_error_times.append(now)
    # Flatten to one line so nothing sent here can fake extra lines in the add-on's log.
    one_line = lambda value, n: " | ".join(str(value or "").splitlines())[:n]
    log.warning("Dashboard error (%s): %s", one_line(data.get("agent"), 200),
                one_line(data.get("detail"), 2000))
    return web.json_response({"ok": True})


client_error_times: list[float] = []


async def admin_save_layout(request: web.Request):
    try:
        layout = clean_layout(await read_json(request))
    except ValueError as err:
        return bad_request(str(err))
    old = db.layout()
    db.set_layout(layout)
    # A page group that's gone: phones that showed it keep its pages, now as single pages.
    for device in db.devices():
        choice = device_page_choice(device)
        kept = keep_removed_groups(choice, old, layout)
        if kept != choice:
            db.update_device(device["id"], layout_pages=json.dumps(kept))
    items = sum(len(p["items"]) for p in layout["pages"])
    db.log(admin_name(request), "Updated phone layout",
           detail=f"{len(layout['pages'])} pages, {items} items")
    return web.json_response({"ok": True, "layout": layout})


# ---------------------------------------------------------------- layout preview
# The Layout tab can show the staff app with the layout being edited, as a phone or a computer
# would. The preview runs on the dashboard's own address (admins only), takes the unsaved draft
# from the dashboard, shows live readings, and never sends a command.

async def admin_preview(request: web.Request):
    return render_staff_index(preview=True)


def preview_device(value) -> dict | None:
    try:
        device = db.device(int(value))
    except (TypeError, ValueError):
        return None
    return device if device and device["status"] != "revoked" else None


async def admin_preview_me(request: web.Request):
    s = db.settings()
    device = preview_device(request.query.get("device"))
    role = next((r["name"] for r in db.roles() if device and r["id"] == device["role_id"]),
                "No role assigned" if device else "Everything on the layout")
    return web.json_response({
        "device": device["name"] if device else "Preview",
        "kind": "computer" if request.query.get("view") == "desktop" else "phone",
        "role": role, "shared": False, "pin_only": False, "staff": [], "pin_required": False,
        "property_name": s["property_name"], "app_name": s["app_name"],
        "accent_color": s["accent_color"], "has_logo": branding.has_logo(),
        "pin_timeout_minutes": db.setting_int("pin_timeout_minutes"),
    })


def layout_access(layout_data: dict, areas: AreaLayout) -> dict[str, str]:
    """Everything a layout shows, at the most any phone could get, with the items' own access
    settings applied: what the preview shows when it isn't previewing one particular device."""
    eids: set[str] = set()
    for page in layout_data["pages"]:
        for item in page["items"]:
            if item["type"] == "device":
                eids.add(item["entity_id"])
            elif item["type"] == "group":
                eids.update(m["entity_id"] for m in item["entities"] if "entity_id" in m)
            elif item["type"] == "area":
                eids.update(eid for eid, ids in areas.membership.items() if item["area_id"] in ids)
    access = {eid: "control" if controllable(eid) else "view" for eid in eids if offered(eid)}
    for eid, level in layout_overrides(layout_data, areas).items():
        if level == "none":
            access.pop(eid, None)
        elif level == "view" and eid in access:
            access[eid] = "view"
    return access


async def admin_preview_entities(request: web.Request):
    data = await read_json(request)
    try:
        draft = clean_layout(data["layout"]) if data.get("layout") is not None else db.layout()
    except ValueError as err:
        return bad_request(str(err))
    device = preview_device(data.get("device_id"))
    try:
        areas = await area_layout()
        states = await ha.states()
    except HomeAssistantError as err:
        log.warning("Home Assistant unavailable: %s", err)
        return bad_request("Can't reach the system right now.", 503)
    if device:
        allowed = phone_access(device, areas, draft)
        pages = device_pages(device, draft)
    else:
        allowed, pages = layout_access(draft, areas), None
    entities = [entity_view(eid, states.get(eid), areas, lvl) for eid, lvl in allowed.items()]
    entities.sort(key=lambda e: (e["group_rank"], e["area"].lower(), e["name"].lower()))
    return web.json_response({
        "entities": entities,
        "layout": visible_layout(draft, allowed, areas, pages),
        "staff_name": None,
        "can_reorder": False,
        "control_lock": db.settings().get("control_lock") == "1",
        "control_lock_minutes": db.setting_int("control_lock_minutes"),
        "custom_order": False,
        "order": {"pages": [], "items": {}},
    })


async def admin_create_area(request: web.Request):
    data = await read_json(request)
    name = clean_name(data.get("name"), 40)
    if not name:
        return bad_request("Give the area a name.")
    if db.name_taken("areas", name):
        return bad_request("An area with that name already exists.")
    try:
        area_id = db.create_area(name)
    except sqlite3.IntegrityError:
        return bad_request("An area with that name already exists.")
    db.log(admin_name(request), f"Created area {name}")
    return web.json_response({"id": area_id})


async def admin_update_area(request: web.Request):
    area = db.area(int(request.match_info["id"]))
    if not area:
        raise web.HTTPNotFound()
    data = await read_json(request)
    name = clean_name(data.get("name"), 40) or area["name"]
    entities = [e for e in data.get("entities") or [] if isinstance(e, str) and offered(e)]
    ha_areas = [a for a in data.get("ha_areas") or [] if isinstance(a, str) and a]
    if db.name_taken("areas", name, area["id"]):
        return bad_request("An area with that name already exists.")
    try:
        db.update_area(area["id"], name, entities, ha_areas)
    except sqlite3.IntegrityError:
        return bad_request("An area with that name already exists.")
    db.log(admin_name(request), f"Updated area {name}",
           detail=f"{len(ha_areas)} Home Assistant areas, {len(entities)} devices")
    return web.json_response({"ok": True})


async def admin_reorder_areas(request: web.Request):
    data = await read_json(request)
    ids = [int(i) for i in data.get("order") or []]
    known = {a["id"] for a in db.areas()}
    db.reorder_areas([i for i in ids if i in known])
    return web.json_response({"ok": True})


async def admin_delete_area(request: web.Request):
    area = db.area(int(request.match_info["id"]))
    if not area:
        raise web.HTTPNotFound()
    db.delete_area(area["id"])
    db.log(admin_name(request), f"Deleted area {area['name']}")
    return web.json_response({"ok": True})


async def admin_delete_role(request: web.Request):
    role = db.role(int(request.match_info["id"]))
    if not role:
        raise web.HTTPNotFound()
    db.delete_role(role["id"])
    db.log(admin_name(request), f"Deleted role {role['name']}")
    return web.json_response({"ok": True})


def parse_role(value) -> int | None:
    if value in (None, ""):
        return None
    role = db.role(int(value))
    if not role:
        raise web.HTTPBadRequest(text="unknown role")
    return role["id"]


def parse_pin(value) -> str | None:
    pin = str(value or "").strip()
    if pin and not re.fullmatch(r"\d{4,8}", pin):
        raise web.HTTPBadRequest(text="PIN must be 4 to 8 digits")
    return pin or None


async def admin_create_device(request: web.Request):
    data = await read_json(request)
    name = clean_name(data.get("name"))
    kind = "computer" if data.get("kind") == "computer" else "phone"
    if not name:
        return bad_request(f"Give the {kind} a name.")
    shared = data.get("signin_mode") == "staff"
    # Check everything before creating the phone, so a rejected request creates nothing.
    role_id = parse_role(data.get("role_id"))
    pin = None if shared else parse_pin(data.get("pin"))
    pages = clean_phone_pages(data.get("layout_pages"))
    device_id = db.create_device(name, role_id, pin)
    if kind != "phone":
        db.update_device(device_id, kind=kind)
    if pages is not None:
        db.update_device(device_id, layout_pages=json.dumps(pages))
    if shared:
        db.update_device(device_id, signin_mode="staff", staff_all=1 if data.get("staff_all", True) else 0,
                         pin_only=1 if data.get("pin_only") else 0)
        if isinstance(data.get("staff_ids"), list):
            known = {m["id"] for m in db.staff_list()}
            db.set_device_staff(device_id, [int(i) for i in data["staff_ids"] if int(i) in known])
    db.log(admin_name(request), f"Added {kind} {name}")
    return web.json_response({"id": device_id})


async def admin_update_device(request: web.Request):
    device = db.device(int(request.match_info["id"]))
    if not device:
        raise web.HTTPNotFound()
    data = await read_json(request)
    fields = {}
    end_sessions = False
    if "name" in data and clean_name(data["name"]):
        fields["name"] = clean_name(data["name"])
    if "role_id" in data:
        fields["role_id"] = parse_role(data["role_id"])
    if "pin" in data:
        pin = parse_pin(data["pin"])
        fields["pin_hash"] = hash_pin(pin) if pin else None
        end_sessions = True
    if data.get("kind") in ("phone", "computer"):
        fields["kind"] = data["kind"]
    if data.get("signin_mode") in ("phone", "staff"):
        fields["signin_mode"] = data["signin_mode"]
        end_sessions = end_sessions or fields["signin_mode"] != device["signin_mode"]
    if "staff_all" in data:
        fields["staff_all"] = 1 if data["staff_all"] else 0
    if "pin_only" in data:
        fields["pin_only"] = 1 if data["pin_only"] else 0
        end_sessions = end_sessions or fields["pin_only"] != device["pin_only"]
    if "layout_pages" in data:
        pages = clean_phone_pages(data["layout_pages"])
        fields["layout_pages"] = None if pages is None else json.dumps(pages)
    if isinstance(data.get("staff_ids"), list):
        known = {m["id"] for m in db.staff_list()}
        db.set_device_staff(device["id"], [int(i) for i in data["staff_ids"] if int(i) in known])
    if end_sessions:
        for token, (dev_id, *_) in list(pin_sessions.items()):
            if dev_id == device["id"]:
                pin_sessions.pop(token, None)
    db.update_device(device["id"], **fields)
    db.log(admin_name(request), f"Updated phone {fields.get('name', device['name'])}")
    return web.json_response({"ok": True})


async def admin_device_action(request: web.Request):
    device = db.device(int(request.match_info["id"]))
    if not device:
        raise web.HTTPNotFound()
    action = request.match_info["action"]
    who = admin_name(request)
    if action == "revoke":
        db.conn.execute(
            """UPDATE devices SET status = 'revoked', token_hash = NULL, enroll_code = NULL
               WHERE id = ?""", (device["id"],))
        db.conn.commit()
        db.revoke_tokens(device["id"])   # and no backup can bring it back
        db.log(who, f"Revoked phone {device['name']}")
    elif action == "reenroll":
        db.new_enroll_code(device["id"])
        db.log(who, f"New setup code for {device['name']}")
    elif action == "unlock":
        db.update_device(device["id"], status="active", failed_pins=0, failed_day_count=0,
                         failed_day_since=None)
        db.log(who, f"Unlocked phone {device['name']}")
    else:
        raise web.HTTPNotFound()
    return web.json_response({"ok": True})


async def admin_delete_device(request: web.Request):
    device = db.device(int(request.match_info["id"]))
    if not device:
        raise web.HTTPNotFound()
    if device["ha_refresh_token"]:
        await ha.unlink_user(device["ha_refresh_token"])
    db.delete_device(device["id"])
    db.log(admin_name(request), f"Deleted phone {device['name']}")
    return web.json_response({"ok": True})


def link_target(request: web.Request):
    """Phone or staff member whose Home Assistant user link is being changed."""
    item_id = int(request.match_info["id"])
    if request.match_info["kind"] == "staff":
        return db.staff(item_id), db.update_staff
    return db.device(item_id), db.update_device


async def admin_link_user(request: web.Request):
    target, update = link_target(request)
    if not target:
        raise web.HTTPNotFound()
    data = await read_json(request)
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if not username or not password:
        return bad_request("Enter the Home Assistant username and password.")
    try:
        refresh = await ha.link_user(username, password)
    except LinkError as err:
        return bad_request(str(err))
    except HomeAssistantError as err:
        log.warning("Linking %s failed: %s", username, err)
        return bad_request("Couldn't reach Home Assistant's login. Try again in a moment.", 502)
    if target["ha_refresh_token"]:
        await ha.unlink_user(target["ha_refresh_token"])
    update(target["id"], ha_user=username, ha_refresh_token=refresh, ha_link_error=None)
    db.log(admin_name(request), f"Linked {target['name']} to Home Assistant user {username}")
    return web.json_response({"ok": True})


async def admin_unlink_user(request: web.Request):
    target, update = link_target(request)
    if not target:
        raise web.HTTPNotFound()
    if target["ha_refresh_token"]:
        await ha.unlink_user(target["ha_refresh_token"])
    update(target["id"], ha_user=None, ha_refresh_token=None, ha_link_error=None)
    db.log(admin_name(request), f"Unlinked {target['name']} from Home Assistant user "
                                f"{target['ha_user']}")
    return web.json_response({"ok": True})


# Staff members

def staff_sessions_end(staff_id: int) -> None:
    for token, (_, _, sid) in list(pin_sessions.items()):
        if sid == staff_id:
            pin_sessions.pop(token, None)


def require_pin(value) -> str:
    pin = parse_pin(value)
    if not pin:
        raise web.HTTPBadRequest(text="Each staff member needs a PIN of 4 to 8 digits.")
    return pin


async def admin_reset_order(request: web.Request):
    """Put a phone (or a staff member's) arrangement back to the admin's layout."""
    kind, item_id = request.match_info["kind"], int(request.match_info["id"])
    target = db.staff(item_id) if kind == "staff" else db.device(item_id)
    if not target:
        raise web.HTTPNotFound()
    db.set_phone_order(f"{'s' if kind == 'staff' else 'd'}:{item_id}", None)
    db.log(admin_name(request), f"Reset {target['name']}'s arrangement to the layout")
    return web.json_response({"ok": True})


async def admin_create_staff(request: web.Request):
    data = await read_json(request)
    name = clean_name(data.get("name"), 40)
    if not name:
        return bad_request("Enter the staff member's name.")
    if db.name_taken("staff", name):
        return bad_request("Someone with that name already exists.")
    link = parse_connecteam_link(data)
    if link.get("connecteam_user_id") and "require_clock_in" not in link:
        link["require_clock_in"] = 1  # clocked-in only unless the admin says otherwise
    pin = (await kiosk_pin(link.get("connecteam_user_id")) if data.get("use_kiosk_code")
           else data.get("pin"))
    if await pin_taken(require_pin(pin)):
        return bad_request(PIN_TAKEN)
    try:
        staff_id = db.create_staff(name, require_pin(pin))
    except sqlite3.IntegrityError:
        return bad_request("Someone with that name already exists.")
    if link:
        db.update_staff(staff_id, **link)
    db.log(admin_name(request), f"Added staff member {name}" +
           (" from Connecteam" if link.get("connecteam_user_id") else ""))
    return web.json_response({"id": staff_id})


async def admin_update_staff(request: web.Request):
    member = db.staff(int(request.match_info["id"]))
    if not member:
        raise web.HTTPNotFound()
    data = await read_json(request)
    fields = {}
    if "name" in data and clean_name(data["name"], 40):
        fields["name"] = clean_name(data["name"], 40)
    link = parse_connecteam_link(data, member["id"])
    pin = (await kiosk_pin(link.get("connecteam_user_id", member["connecteam_user_id"]))
           if data.get("use_kiosk_code") else data.get("pin"))
    if pin:
        if await pin_taken(require_pin(pin), member["id"]):
            return bad_request(PIN_TAKEN)
        fields["pin_hash"] = hash_pin(require_pin(pin))
        fields["failed_pins"] = 0
    if "active" in data:
        if not data["active"]:
            fields["status"] = "disabled"
        elif member["status"] == "disabled":
            fields["status"] = "active"
    fields.update(link)
    if "name" in fields and db.name_taken("staff", fields["name"], member["id"]):
        return bad_request("Someone with that name already exists.")
    try:
        db.update_staff(member["id"], **fields)
    except sqlite3.IntegrityError:
        return bad_request("Someone with that name already exists.")
    if "pin_hash" in fields or fields.get("status") == "disabled":
        staff_sessions_end(member["id"])
    db.log(admin_name(request), f"Updated staff member {fields.get('name', member['name'])}")
    return web.json_response({"ok": True})


async def admin_unlock_staff(request: web.Request):
    member = db.staff(int(request.match_info["id"]))
    if not member:
        raise web.HTTPNotFound()
    db.update_staff(member["id"], status="active", failed_pins=0, locked_until=None)
    db.log(admin_name(request), f"Unlocked staff member {member['name']}")
    return web.json_response({"ok": True})


async def admin_delete_staff(request: web.Request):
    member = db.staff(int(request.match_info["id"]))
    if not member:
        raise web.HTTPNotFound()
    if member["ha_refresh_token"]:
        await ha.unlink_user(member["ha_refresh_token"])
    staff_sessions_end(member["id"])
    db.delete_staff(member["id"])
    db.log(admin_name(request), f"Deleted staff member {member['name']}")
    return web.json_response({"ok": True})


async def admin_device_qr(request: web.Request):
    device = db.device(int(request.match_info["id"]))
    url = setup_url(device and device["enroll_code"])
    if not url:
        raise web.HTTPNotFound()
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2)
    return web.Response(body=buf.getvalue(), content_type="image/svg+xml",
                        headers={"Cache-Control": "no-store"})


async def admin_activity(request: web.Request):
    before = request.query.get("before")
    return web.json_response({"activity": db.activity(200, float(before) if before else None)})


BOOL_SETTINGS = ("ha_logbook", "allow_phone_reorder", "phones_via_tunnel_only", "require_pin",
                 "control_lock", "connecteam_enabled")
NUMBER_SETTINGS = (("pin_timeout_minutes", 1, 1440), ("max_pin_attempts", 3, 20),
                   ("enroll_hours", 1, 720), ("control_lock_minutes", 1, 120),
                   ("connecteam_poll_minutes", 30, 240))


def clean_settings(data: dict, current: dict) -> dict:
    """The settings in `data` that are valid, ready to store. Raises ValueError with a message
    for the admin. Used by the Settings form and when restoring a backup."""
    values = {}
    for key in ("property_name", "app_name"):
        if key in data:
            values[key] = clean_name(data[key], 40) or current[key]
    for key in ("accent_color", "icon_background"):
        if key in data:
            if not COLOR_RE.match(str(data[key])):
                raise ValueError("Colors must look like #2f6f4f.")
            values[key] = data[key]
    if "public_url" in data:
        url = str(data["public_url"]).strip().rstrip("/")
        if url and not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?", url):
            raise ValueError("The staff app address must start with https:// (no path).")
        values["public_url"] = url
    for key in BOOL_SETTINGS:
        if key in data:
            values[key] = "1" if data[key] in (True, "1", "true", "on") else "0"
    if "tunnel_addresses" in data:
        values["tunnel_addresses"] = ", ".join(parse_tunnel_addresses(data["tunnel_addresses"]))
    if "connecteam_region" in data:
        if data["connecteam_region"] not in ("global", "au"):
            raise ValueError("Choose where your Connecteam account is hosted.")
        values["connecteam_region"] = data["connecteam_region"]
    if "connecteam_client_id" in data:
        client_id = str(data["connecteam_client_id"] or "").strip()
        if len(client_id) > 200 or not re.fullmatch(r"[\x21-\x7e]*", client_id):
            raise ValueError("The Connecteam Client ID doesn't look right.")
        values["connecteam_client_id"] = client_id
    # The secret is write-only: blank keeps the saved one, "clear" removes it.
    secret = str(data.get("connecteam_client_secret") or "").strip()
    if secret:
        if len(secret) > 500 or not re.fullmatch(r"[\x21-\x7e]+", secret):
            raise ValueError("The Connecteam Client Secret doesn't look right.")
        values["connecteam_client_secret"] = secret
    if data.get("connecteam_clear_secret") in (True, "1", "true", "on"):
        values["connecteam_client_secret"] = ""
    for key, lo, hi in NUMBER_SETTINGS:
        if key in data:
            try:
                values[key] = max(lo, min(hi, int(data[key])))
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number.") from None
    return values


async def admin_update_settings(request: web.Request):
    data = await read_json(request)
    current = db.settings()
    try:
        values = clean_settings(data, current)
    except ValueError as err:
        return bad_request(str(err))
    if values.get("phones_via_tunnel_only") == "1" and current.get("phones_via_tunnel_only") != "1":
        # Only switch on once tunnel traffic has been seen from an allowed address, so the
        # setting can't cut every phone off.
        addresses = parse_tunnel_addresses(values.get("tunnel_addresses",
                                                      current.get("tunnel_addresses")))
        recent = [addr for addr, (seen, _) in tunnel_seen.items()
                  if time.time() - seen < 24 * 3600 and from_tunnel(addr, addresses)]
        if not recent:
            seen = [addr for addr, (when, _) in tunnel_seen.items() if time.time() - when < 24 * 3600]
            hint = (f" Cloudflare traffic recently came from {', '.join(seen)}; add that as the "
                    "tunnel address if it's the computer running cloudflared." if seen else
                    " Open the staff app through your https address once (on any phone or "
                    "browser), then try again.")
            return bad_request("Staff Access hasn't seen any traffic from your tunnel yet, so "
                               "turning this on would block every phone." + hint)
    ct_before = connecteam_settings()
    db.update_settings(values)
    branding.invalidate()
    if connecteam_settings() != ct_before:
        connecteam.forget()   # also wakes the clock check, which runs only if someone needs it
    db.log(admin_name(request), "Updated settings")
    return web.json_response({"ok": True})


# ---------------------------------------------------------------- export, import, backup

def download(data: dict, kind: str) -> web.Response:
    name = f"staff-portal-{kind}-{time.strftime('%Y-%m-%d')}.json"
    return web.Response(text=json.dumps(data, indent=1), content_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


async def admin_export(request: web.Request):
    kind = request.match_info["kind"]
    maker = {"layout": transfer.export_layout, "roles": transfer.export_roles,
             "staff": transfer.export_staff}[kind]
    db.log(admin_name(request), f"Exported {kind}")
    return download(maker(db, VERSION), kind)


async def admin_import(request: web.Request):
    kind = request.match_info["kind"]
    data = await read_json(request)
    importer = {"roles": transfer.import_roles, "staff": transfer.import_staff}[kind]
    try:
        result = importer(db, data.get("file"), str(data.get("mode") or ""))
    except ValueError as err:
        return bad_request(str(err))
    for staff_id in result.pop("signed_out", []):
        staff_sessions_end(staff_id)   # their PIN changed, or they're turned off
    db.log(admin_name(request), f"Imported {kind}",
           detail=f"{result['added']} added, {result['replaced']} replaced, {result['skipped']} skipped")
    return web.json_response(result)


async def admin_backup(request: web.Request):
    db.log(admin_name(request), "Downloaded a full backup")
    return download(transfer.make_backup(db, branding, VERSION), "backup")


async def check_backup(data) -> dict:
    # Decoding the logo is image work, so keep it off the event loop.
    return await asyncio.to_thread(transfer.clean_backup, data, clean_settings, db.settings(),
                                   branding.clean_logo, db.known_tokens())


async def admin_restore_preview(request: web.Request):
    data = (await read_json(request)).get("backup")
    try:
        snap = await check_backup(data)
    except ValueError as err:
        return bad_request(str(err))
    return web.json_response({**transfer.backup_summary(data), "needs_setup": snap["needs_setup"]})


async def admin_restore(request: web.Request):
    data = (await read_json(request)).get("backup")
    try:
        snap = await check_backup(data)
    except ValueError as err:
        return bad_request(str(err))
    # Keep what's being replaced, so a restore can be undone by restoring this copy.
    safety = transfer.save_safety_backup(DATA_DIR, transfer.make_backup(db, branding, VERSION))
    dropped_links = db.replace_everything(snap)
    for token in dropped_links:
        # A link that didn't survive the restore would otherwise stay valid in Home Assistant.
        try:
            await ha.unlink_user(token)
        except HomeAssistantError as err:
            log.warning("Couldn't revoke a Home Assistant sign-in after restore: %s", err)
    for device in snap["devices"]:
        if device["status"] == "pending":
            db.new_enroll_code(device["id"])  # setup codes never travel in backups
    if snap["logo"]:
        branding.write_logo(snap["logo"])
    else:
        branding.remove_logo()
    pin_sessions.clear()  # everyone with a PIN signs in again
    connecteam.forget()   # the restored settings may point somewhere else
    made = time.strftime("%b %d, %Y", time.localtime(snap["exported_at"]))
    db.log(admin_name(request), "Restored a backup",
           detail=f"Backup from {made}. What it replaced was saved as {safety}." +
           (f" Revoked {len(dropped_links)} Home Assistant sign-in(s) no longer linked." if dropped_links else ""))
    return web.json_response({"ok": True, "safety_backup": safety})


async def admin_safety_backups(request: web.Request):
    return web.json_response({"backups": transfer.safety_backups(DATA_DIR)})


async def admin_safety_backup(request: web.Request):
    path = transfer.safety_backup_path(DATA_DIR, request.match_info["name"])
    if not path:
        raise web.HTTPNotFound()
    with open(path) as f:
        text = f.read()
    return web.Response(text=text, content_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="{os.path.basename(path)}"',
        "Cache-Control": "no-store"})


async def admin_upload_logo(request: web.Request):
    raw = bytearray()
    try:
        async with asyncio.timeout(60):
            reader = await request.multipart()
            field = await reader.next()
            if not field or field.name != "logo":
                return bad_request("No file received.")
            while chunk := await field.read_chunk(64 * 1024):
                raw.extend(chunk)
                if len(raw) > MAX_LOGO_BYTES:
                    return bad_request("That file is too big. Use an image under 5 MB.", 413)
    except TimeoutError:
        return bad_request("The upload took too long. Try again.", 408)
    try:
        await asyncio.to_thread(branding.save_logo, bytes(raw))
    except Exception:
        return bad_request("That file isn't an image we can use. Try a PNG, JPG or WebP "
                           "under 5000 \u00d7 5000 pixels.")
    db.log(admin_name(request), "Uploaded logo")
    return web.json_response({"ok": True})


async def admin_delete_logo(request: web.Request):
    branding.remove_logo()
    return web.json_response({"ok": True})


@web.middleware
async def admin_guard(request: web.Request, handler):
    remote = request.remote or ""
    if remote not in ADMIN_ALLOWED_IPS:
        return web.Response(status=403, text="Open Staff Access from the Home Assistant sidebar.")
    if not ipaddress.ip_address(remote).is_loopback:
        # Through ingress. Home Assistant lets any signed-in user open an add-on's page if
        # they know the address, so check that this user is an administrator.
        user_id = request.headers.get("X-Remote-User-Id", "")
        try:
            admin = bool(user_id) and await ha.is_admin(user_id)
        except HomeAssistantError as err:
            log.warning("Couldn't check Home Assistant user %s: %s", user_id, err)
            return web.Response(status=503, text="Couldn't check your Home Assistant account. "
                                                 "Try again in a moment.")
        if not admin:
            return web.Response(status=403, text="Only Home Assistant administrators can open "
                                                 "Staff Access.")
    # Changes must come from the dashboard itself: it sends this header, which another website
    # can't add to a request without the browser blocking it. Checked for local connections
    # too, in case the dashboard is put behind a proxy on the same computer.
    if request.method not in ("GET", "HEAD") and request.headers.get(
            "X-Requested-With") != "staff-portal":
        return web.Response(status=403, text="Request blocked.")
    try:
        response = await handler(request)
    except ValueError:
        return bad_request("invalid value")
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"  # always check for a newer version
    return response


def admin_app() -> web.Application:
    # Room for a full backup with its logo.
    app = web.Application(middlewares=[admin_guard], client_max_size=16 * 1024 * 1024)
    app[BODY_SECONDS] = 120.0   # a 16 MB backup over a slow connection
    app.router.add_get("/", admin_index)
    app.router.add_get("/preview", admin_preview)
    app.router.add_get("/api/preview/me", admin_preview_me)
    app.router.add_post("/api/preview/entities", admin_preview_entities)
    app.router.add_static("/static/", os.path.join(HERE, "admin"))
    app.router.add_get("/logo.png", logo)
    app.router.add_get(r"/icon-{size:\d+}.png", staff_icon)
    app.router.add_get("/api/state", admin_state)
    app.router.add_get(r"/api/export/{kind:layout|roles|staff}", admin_export)
    app.router.add_post(r"/api/import/{kind:roles|staff}", admin_import)
    app.router.add_get("/api/backup", admin_backup)
    app.router.add_get("/api/connecteam/users", admin_connecteam_users)
    app.router.add_post("/api/connecteam/test", admin_connecteam_test)
    app.router.add_post("/api/restore/preview", admin_restore_preview)
    app.router.add_post("/api/restore", admin_restore)
    app.router.add_get("/api/safety-backups", admin_safety_backups)
    app.router.add_get(r"/api/safety-backups/{name}", admin_safety_backup)
    app.router.add_get("/api/entities", admin_entities)
    app.router.add_post("/api/roles", admin_create_role)
    app.router.add_put(r"/api/roles/{id:\d+}", admin_update_role)
    app.router.add_post(r"/api/roles/{id:\d+}/duplicate", admin_duplicate_role)
    app.router.add_delete(r"/api/roles/{id:\d+}", admin_delete_role)
    app.router.add_put("/api/layout", admin_save_layout)
    app.router.add_post("/api/client-error", admin_client_error)
    app.router.add_post("/api/areas", admin_create_area)
    app.router.add_put("/api/areas/order", admin_reorder_areas)
    app.router.add_put(r"/api/areas/{id:\d+}", admin_update_area)
    app.router.add_delete(r"/api/areas/{id:\d+}", admin_delete_area)
    app.router.add_post("/api/devices", admin_create_device)
    app.router.add_patch(r"/api/devices/{id:\d+}", admin_update_device)
    app.router.add_delete(r"/api/devices/{id:\d+}", admin_delete_device)
    app.router.add_post(r"/api/devices/{id:\d+}/{action:revoke|reenroll|unlock}", admin_device_action)
    app.router.add_post(r"/api/{kind:devices|staff}/{id:\d+}/link", admin_link_user)
    app.router.add_delete(r"/api/{kind:devices|staff}/{id:\d+}/link", admin_unlink_user)
    app.router.add_delete(r"/api/{kind:devices|staff}/{id:\d+}/order", admin_reset_order)
    app.router.add_post("/api/staff", admin_create_staff)
    app.router.add_patch(r"/api/staff/{id:\d+}", admin_update_staff)
    app.router.add_delete(r"/api/staff/{id:\d+}", admin_delete_staff)
    app.router.add_post(r"/api/staff/{id:\d+}/unlock", admin_unlock_staff)
    app.router.add_get(r"/api/devices/{id:\d+}/qr.svg", admin_device_qr)
    app.router.add_get("/api/activity", admin_activity)
    app.router.add_put("/api/settings", admin_update_settings)
    app.router.add_post("/api/logo", admin_upload_logo)
    app.router.add_delete("/api/logo", admin_delete_logo)
    return app


def raise_file_limit() -> None:
    """Allow as many open connections as the system lets us (often 1024 by default), so it takes
    far more connections to run out."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = hard if hard != resource.RLIM_INFINITY else max(soft, 65536)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ImportError, ValueError, OSError) as err:
        log.info("Couldn't raise the open-file limit: %s", err)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await ha.start()
    await connecteam.start()
    clock_task = asyncio.create_task(connecteam.run())
    raise_file_limit()
    runners = []
    for app, port in ((admin_app(), ADMIN_PORT), (staff_app(), STAFF_PORT)):
        runner = web.AppRunner(app, access_log=None, keepalive_timeout=IDLE_SECONDS)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        runners.append(runner)
    log.info("Staff Portal %s: admin on :%s (ingress), staff app on :%s",
             VERSION, ADMIN_PORT, STAFF_PORT)
    try:
        await asyncio.Event().wait()
    finally:
        clock_task.cancel()
        for runner in runners:
            await runner.cleanup()
        await connecteam.close()
        await ha.close()


if __name__ == "__main__":
    asyncio.run(main())
