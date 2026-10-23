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
                    clean_phone_pages, layout_overrides, visible_layout)
from ha import HomeAssistant, HomeAssistantError, LinkError
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


def record_failure(ip: str) -> None:
    count, since = failures.get(ip, (0, time.time()))
    failures[ip] = (count + 1, since)


def bad_request(message: str, status: int = 400, **extra) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


async def read_json(request: web.Request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
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


def phone_access(device: dict, layout: AreaLayout) -> dict[str, str]:
    """What one phone may see and control: its role, adjusted by any access overrides on the
    layout items it shows."""
    access = role_access(device["role_id"], layout)
    for eid, level in layout_overrides(db.layout(), layout, device_pages(device)).items():
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

def render_staff_index(enroll_code: str | None = None) -> web.Response:
    s = db.settings()
    with open(os.path.join(HERE, "staff", "index.html"), encoding="utf-8") as f:
        html = f.read()
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
    csp = (f"default-src 'self'; img-src 'self' data:; style-src 'self' 'nonce-{nonce}'; "
           f"script-src 'self' 'nonce-{nonce}'; connect-src 'self'; frame-ancestors 'none'; "
           "base-uri 'none'; form-action 'none'")
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store", "Content-Security-Policy": csp})


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
                 active: bool = False) -> tuple[dict, dict | None]:
    """Return (phone, signed-in staff member or None), or raise the error the app should show.

    `active` marks a request someone actually made (a control, saving an order). Only those,
    and polls sent right after the person touched the screen (X-Active), keep a PIN session
    alive; background refreshes don't, so an unattended phone still signs out when idle."""
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
recent_actions: dict[int, list[float]] = {}
last_entity_action: dict[tuple[int, str], float] = {}


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
    if too_many_failures(ip):
        return bad_request("Too many attempts. Try again later.", 429)
    data = await read_json(request)
    claimed = db.claim_enroll_code(str(data.get("code", "")))
    if not claimed:
        record_failure(ip)
        return bad_request("This setup code is invalid, already used, or expired.", 404)
    device, token = claimed
    db.log(device["name"], "Phone set up", ip=ip)
    return web.json_response({"token": token})


async def api_me(request: web.Request):
    device, _ = authenticate(request, need_unlock=False)
    s = db.settings()
    shared = device["signin_mode"] == "staff"
    return web.json_response({
        "device": device["name"],
        "role": device["role_name"],
        "shared": shared,
        "staff": db.staff_for_device(device) if shared else [],
        "pin_required": shared or bool(device["pin_hash"]),
        "property_name": s["property_name"],
        "app_name": s["app_name"],
        "accent_color": s["accent_color"],
        "has_logo": branding.has_logo(),
        "pin_timeout_minutes": db.setting_int("pin_timeout_minutes"),
    })


async def api_unlock(request: web.Request):
    device, _ = authenticate(request, need_unlock=False)
    data = await read_json(request)
    pin = str(data.get("pin", ""))
    ip = client_ip(request)
    max_attempts = db.setting_int("max_pin_attempts")

    if device["signin_mode"] != "staff":
        if not device["pin_hash"]:
            return web.json_response({"session": ""})
        if check_pin(pin, device["pin_hash"]):
            db.update_device(device["id"], failed_pins=0)
            return web.json_response({"session": new_session(device, None)})
        attempts = device["failed_pins"] + 1
        if attempts >= max_attempts:
            db.update_device(device["id"], failed_pins=attempts, status="locked")
            db.log(device["name"], "Phone locked after wrong PINs", role=device["role_name"],
                   ok=False, ip=ip)
            return bad_request("locked", 423)
        db.update_device(device["id"], failed_pins=attempts)
        return bad_request(f"Wrong PIN. {max_attempts - attempts} tries left.", 403)

    # Shared phone: a staff member signs in with their own PIN.
    try:
        member = db.staff(int(data.get("staff_id")))
    except (TypeError, ValueError):
        member = None
    if not member or member["status"] == "disabled" or not db.staff_allowed_on(member["id"], device):
        return bad_request("Pick your name again.", 404)
    now = time.time()
    locked_msg = (f"{member['name']} is locked for a while after too many wrong PINs. "
                  "Try again later, or ask your manager to unlock you.")
    if member["status"] == "locked" or (member["locked_until"] or 0) > now:
        return bad_request(locked_msg, 403, staff_locked=True)
    if check_pin(pin, member["pin_hash"]):
        # Only this person's own count is cleared. The phone's count isn't, so signing in as
        # yourself can't be used to keep guessing other people's PINs.
        db.update_staff(member["id"], failed_pins=0, locked_until=None, last_seen=now)
        db.log(actor_name(device, member), "Signed in", role=device["role_name"], ip=ip)
        return web.json_response({"session": new_session(device, member),
                                  "staff_name": member["name"]})

    attempts = member["failed_pins"] + 1
    # Also count per phone, so cycling through names can't be used to guess PINs. The count
    # is forgotten after a quiet spell with no wrong PINs.
    quiet = now - (device["failed_at"] or 0) > PIN_FAILURE_MEMORY
    phone_attempts = (0 if quiet else device["failed_pins"]) + 1
    if phone_attempts >= max_attempts * 3:
        db.update_device(device["id"], failed_pins=phone_attempts, failed_at=now, status="locked")
        db.log(device["name"], "Phone locked after wrong PINs", role=device["role_name"],
               ok=False, ip=ip)
        return bad_request("locked", 423)
    db.update_device(device["id"], failed_pins=phone_attempts, failed_at=now)
    if attempts >= max_attempts:
        # A temporary lock that doesn't end their sessions on other phones, so wrong guesses
        # can't be used to shut a colleague out.
        db.update_staff(member["id"], failed_pins=0, locked_until=now + STAFF_LOCK_SECONDS)
        db.log(actor_name(device, member), "Locked for a while after wrong PINs",
               role=device["role_name"], ok=False, ip=ip)
        return bad_request(locked_msg, 403, staff_locked=True)
    db.update_staff(member["id"], failed_pins=attempts)
    return bad_request(f"Wrong PIN. {max_attempts - attempts} tries left.", 403)


def new_session(device: dict, member: dict | None) -> str:
    session = secrets.token_urlsafe(24)
    pin_sessions[session] = (device["id"], time.time(), member["id"] if member else None)
    return session


async def api_signout(request: web.Request):
    device, member = authenticate(request)
    pin_sessions.pop(request.headers.get("X-Session", ""), None)
    if member:
        db.log(actor_name(device, member), "Signed out", role=device["role_name"])
    return web.json_response({"ok": True})


async def api_entities(request: web.Request):
    device, member = authenticate(request)
    try:
        layout = await area_layout()
        allowed = phone_access(device, layout)
        states = await ha.states() if allowed else {}
    except HomeAssistantError as err:
        log.warning("Home Assistant unavailable: %s", err)
        return bad_request("Can't reach the system right now.", 503)
    entities = [entity_view(eid, states.get(eid), layout, lvl) for eid, lvl in allowed.items()]
    entities.sort(key=lambda e: (e["group_rank"], e["area"].lower(), e["name"].lower()))
    can_reorder = db.settings().get("allow_phone_reorder") == "1"
    order = db.phone_order(order_owner(device, member)) if can_reorder else None
    phone_layout = visible_layout(db.layout(), allowed, layout, device_pages(device))
    if phone_layout:
        phone_layout = apply_phone_order(phone_layout, order)
    else:
        entities = apply_auto_order(entities, order)
    return web.json_response({
        "entities": entities,
        "layout": phone_layout,
        "staff_name": member and member["name"],
        "can_reorder": can_reorder,
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


async def api_action(request: web.Request):
    device, member = authenticate(request, active=True)
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
        # Only commands that would really reach the device count towards the per-device pause.
        if entity_too_soon(device["id"], entity_id):
            return bad_request("That was changed a moment ago. Wait a second and try again.", 429)
        last_entity_action[(device["id"], entity_id)] = time.time()
        as_user = await send_command(device, member, domain, service, call)
    except ActionRejected as err:
        return bad_request(str(err))
    except HomeAssistantError as err:
        log.warning("Service call failed: %s", err)
        db.log(actor, action, role=device["role_name"], entity_id=entity_id,
               detail=str(err)[:200], ok=False, ip=ip)
        return bad_request("The system didn't accept that. Try again.", 502)
    detail = ", ".join(f"{k}={v}" for k, v in call.items() if k != "entity_id") or None
    db.log(actor, action, role=device["role_name"], entity_id=entity_id, detail=detail, ip=ip)
    # Commands sent as a linked user are already credited to them in Home Assistant's logbook.
    if not as_user and db.settings().get("ha_logbook") == "1":
        name = (states.get(entity_id) or {}).get("attributes", {}).get("friendly_name", entity_id)
        who = f"{member['name']} ({device['name']})" if member else device["name"]
        try:
            await ha.logbook(name, f"{describe_action(entity_id, action, call)} by {who} "
                                   "via Staff Portal", entity_id)
        except HomeAssistantError as err:
            log.warning("Couldn't write logbook entry: %s", err)
    return web.json_response({"ok": True})


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


@web.middleware
async def staff_headers(request: web.Request, handler):
    note_tunnel_source(request)
    if (db.settings().get("phones_via_tunnel_only") == "1"
            and not from_tunnel(request.remote)):
        # Direct connections from the local network are refused; only the Cloudflare Tunnel
        # (the add-on, or a tunnel computer listed in Settings) may pass phones through.
        response = web.Response(status=403, text="Staff Portal is only available through the "
                                                 "property's secure address.")
    else:
        response = await handler(request)
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"  # always check for a newer version
    return response


def staff_app() -> web.Application:
    app = web.Application(middlewares=[staff_headers], client_max_size=64 * 1024)
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


def device_pages(device: dict) -> list[str] | None:
    """Layout pages chosen for a phone, or None for every page."""
    raw = device.get("layout_pages")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return [str(i) for i in value] if isinstance(value, list) else None


def device_payload() -> list[dict]:
    devices = db.devices()
    owners = db.phone_order_owners()
    for d in devices:
        d["setup_url"] = setup_url(d["enroll_code"])
        d["staff_ids"] = db.device_staff_ids(d["id"])
        d["layout_pages"] = device_pages(d)
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
        "settings": db.settings(),
        "tunnel_sources": tunnel_sources(),
        "roles": db.roles(),
        "devices": device_payload(),
        "staff": [{**m, "custom_order": f"s:{m['id']}" in owners,
                   "locked": m["status"] == "locked" or (m["locked_until"] or 0) > time.time()}
                  for m in db.staff_list()],
        "areas": db.areas(),
        "layout": db.layout(),
        "has_logo": branding.has_logo(),
    })


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
    db.set_layout(layout)
    items = sum(len(p["items"]) for p in layout["pages"])
    db.log(admin_name(request), "Updated phone layout",
           detail=f"{len(layout['pages'])} pages, {items} items")
    return web.json_response({"ok": True, "layout": layout})


async def admin_create_area(request: web.Request):
    data = await read_json(request)
    name = clean_name(data.get("name"), 40)
    if not name:
        return bad_request("Give the area a name.")
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
    if not name:
        return bad_request("Give the phone a name.")
    shared = data.get("signin_mode") == "staff"
    # Check everything before creating the phone, so a rejected request creates nothing.
    role_id = parse_role(data.get("role_id"))
    pin = None if shared else parse_pin(data.get("pin"))
    pages = clean_phone_pages(data.get("layout_pages"))
    device_id = db.create_device(name, role_id, pin)
    if pages is not None:
        db.update_device(device_id, layout_pages=json.dumps(pages))
    if shared:
        db.update_device(device_id, signin_mode="staff", staff_all=1 if data.get("staff_all", True) else 0)
        if isinstance(data.get("staff_ids"), list):
            known = {m["id"] for m in db.staff_list()}
            db.set_device_staff(device_id, [int(i) for i in data["staff_ids"] if int(i) in known])
    db.log(admin_name(request), f"Added phone {name}")
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
    if data.get("signin_mode") in ("phone", "staff"):
        fields["signin_mode"] = data["signin_mode"]
        end_sessions = end_sessions or fields["signin_mode"] != device["signin_mode"]
    if "staff_all" in data:
        fields["staff_all"] = 1 if data["staff_all"] else 0
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
        db.log(who, f"Revoked phone {device['name']}")
    elif action == "reenroll":
        db.new_enroll_code(device["id"])
        db.log(who, f"New setup code for {device['name']}")
    elif action == "unlock":
        db.update_device(device["id"], status="active", failed_pins=0)
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
    try:
        staff_id = db.create_staff(name, require_pin(data.get("pin")))
    except sqlite3.IntegrityError:
        return bad_request("Someone with that name already exists.")
    db.log(admin_name(request), f"Added staff member {name}")
    return web.json_response({"id": staff_id})


async def admin_update_staff(request: web.Request):
    member = db.staff(int(request.match_info["id"]))
    if not member:
        raise web.HTTPNotFound()
    data = await read_json(request)
    fields = {}
    if "name" in data and clean_name(data["name"], 40):
        fields["name"] = clean_name(data["name"], 40)
    if data.get("pin"):
        fields["pin_hash"] = hash_pin(require_pin(data["pin"]))
        fields["failed_pins"] = 0
    if "active" in data:
        if not data["active"]:
            fields["status"] = "disabled"
        elif member["status"] == "disabled":
            fields["status"] = "active"
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


async def admin_update_settings(request: web.Request):
    data = await read_json(request)
    values = {}
    for key in ("property_name", "app_name"):
        if key in data:
            values[key] = clean_name(data[key], 40) or db.settings()[key]
    for key in ("accent_color", "icon_background"):
        if key in data:
            if not COLOR_RE.match(str(data[key])):
                return bad_request("Colors must look like #2f6f4f.")
            values[key] = data[key]
    if "public_url" in data:
        url = str(data["public_url"]).strip().rstrip("/")
        if url and not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?", url):
            return bad_request("The staff app address must start with https:// (no path).")
        values["public_url"] = url
    for key in ("ha_logbook", "allow_phone_reorder", "phones_via_tunnel_only", "require_pin"):
        if key in data:
            values[key] = "1" if data[key] in (True, "1", "true", "on") else "0"
    if "tunnel_addresses" in data:
        try:
            values["tunnel_addresses"] = ", ".join(parse_tunnel_addresses(data["tunnel_addresses"]))
        except ValueError as err:
            return bad_request(str(err))
    current = db.settings()
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
    for key, lo, hi in (("pin_timeout_minutes", 1, 1440), ("max_pin_attempts", 3, 20),
                        ("enroll_hours", 1, 720)):
        if key in data:
            try:
                values[key] = max(lo, min(hi, int(data[key])))
            except (TypeError, ValueError):
                return bad_request(f"{key} must be a number.")
    db.update_settings(values)
    branding.invalidate()
    db.log(admin_name(request), "Updated settings")
    return web.json_response({"ok": True})


async def admin_upload_logo(request: web.Request):
    reader = await request.multipart()
    field = await reader.next()
    if not field or field.name != "logo":
        return bad_request("No file received.")
    raw = bytearray()
    while chunk := await field.read_chunk(64 * 1024):
        raw.extend(chunk)
        if len(raw) > MAX_LOGO_BYTES:
            return bad_request("That file is too big. Use an image under 5 MB.", 413)
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
        # Changes must come from the dashboard itself: it sends this header, which another
        # website can't add to a request without the browser blocking it.
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
    app = web.Application(middlewares=[admin_guard], client_max_size=8 * 1024 * 1024)
    app.router.add_get("/", admin_index)
    app.router.add_static("/static/", os.path.join(HERE, "admin"))
    app.router.add_get("/logo.png", logo)
    app.router.add_get(r"/icon-{size:\d+}.png", staff_icon)
    app.router.add_get("/api/state", admin_state)
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


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await ha.start()
    runners = []
    for app, port in ((admin_app(), ADMIN_PORT), (staff_app(), STAFF_PORT)):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        runners.append(runner)
    log.info("Staff Portal %s: admin on :%s (ingress), staff app on :%s",
             VERSION, ADMIN_PORT, STAFF_PORT)
    try:
        await asyncio.Event().wait()
    finally:
        for runner in runners:
            await runner.cleanup()
        await ha.close()


if __name__ == "__main__":
    asyncio.run(main())
