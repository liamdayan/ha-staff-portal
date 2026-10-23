"""Staff Portal: a locked-down, branded staff web app in front of Home Assistant.

Two web servers run in this process:
  * Admin (port 8099) is only reachable through Home Assistant ingress, so only HA admins see it.
  * Staff (port 8787) is the phone app; expose it through your Cloudflare Tunnel.
Staff phones never receive Home Assistant credentials. Every request is checked against the
phone's role before it is forwarded.
"""

import asyncio
import io
import json
import logging
import os
import re
import secrets
import time

import segno
from aiohttp import web

from branding import Branding
from db import Database, check_pin, hash_pin
from ha import HomeAssistant, HomeAssistantError
from permissions import ActionRejected, build_service_call, controllable, domain_of, offered

DATA_DIR = os.environ.get("DATA_DIR", "/data")
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8099"))
STAFF_PORT = int(os.environ.get("STAFF_PORT", "8787"))
# Supervisor ingress always connects from 172.30.32.2.
ADMIN_ALLOWED_IPS = set(os.environ.get("ADMIN_ALLOWED_IPS", "172.30.32.2,127.0.0.1").split(","))
HERE = os.path.dirname(os.path.abspath(__file__))
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

log = logging.getLogger("staff_portal")
db = Database(DATA_DIR)
ha = HomeAssistant()
branding = Branding(DATA_DIR)

# PIN unlock sessions live in memory: an add-on restart just asks for the PIN again.
pin_sessions: dict[str, tuple[int, float]] = {}
failures: dict[str, tuple[int, float]] = {}


class HTTPLocked(web.HTTPClientError):
    status_code = 423


def client_ip(request: web.Request) -> str:
    return (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote
        or ""
    )


def too_many_failures(ip: str, limit: int = 20, window: float = 600) -> bool:
    count, since = failures.get(ip, (0, time.time()))
    if time.time() - since > window:
        failures.pop(ip, None)
        return False
    return count >= limit


def record_failure(ip: str) -> None:
    count, since = failures.get(ip, (0, time.time()))
    failures[ip] = (count + 1, since)


def bad_request(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


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


def entity_view(entity_id: str, state: dict | None, areas: dict, level: str | None = None) -> dict:
    attrs = (state or {}).get("attributes", {})
    keep = (
        "unit_of_measurement", "device_class", "brightness", "current_temperature", "temperature",
        "target_temp_high", "target_temp_low", "hvac_modes", "hvac_action", "min_temp",
        "max_temp", "operation_list", "operation_mode", "current_position", "icon",
    )
    return {
        "entity_id": entity_id,
        "domain": domain_of(entity_id),
        "name": attrs.get("friendly_name") or entity_id,
        "area": areas.get(entity_id, ""),
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
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


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


def authenticate(request: web.Request, need_unlock: bool = True) -> dict:
    """Return the calling phone's record or raise the right error for the app to show."""
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    device = db.device_by_token(token) if token else None
    if not device or device["status"] in ("revoked", "pending"):
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "signed_out"}), content_type="application/json"
        )
    if device["status"] == "locked":
        raise HTTPLocked(text=json.dumps({"error": "locked"}), content_type="application/json")
    if need_unlock and device["pin_hash"]:
        session = pin_sessions.get(request.headers.get("X-Session", ""))
        timeout = db.setting_int("pin_timeout_minutes") * 60
        if not session or session[0] != device["id"] or time.time() - session[1] > timeout:
            raise web.HTTPForbidden(
                text=json.dumps({"error": "pin_required"}), content_type="application/json"
            )
        pin_sessions[request.headers["X-Session"]] = (device["id"], time.time())
    if not device["last_seen"] or time.time() - device["last_seen"] > 60:
        db.touch_device(device["id"])
    return device


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
    return web.json_response({"token": token, "pin_required": bool(device["pin_hash"])})


async def api_me(request: web.Request):
    device = authenticate(request, need_unlock=False)
    s = db.settings()
    return web.json_response({
        "device": device["name"],
        "role": device["role_name"],
        "pin_required": bool(device["pin_hash"]),
        "property_name": s["property_name"],
        "app_name": s["app_name"],
        "accent_color": s["accent_color"],
        "has_logo": branding.has_logo(),
        "pin_timeout_minutes": db.setting_int("pin_timeout_minutes"),
    })


async def api_unlock(request: web.Request):
    device = authenticate(request, need_unlock=False)
    data = await read_json(request)
    pin = str(data.get("pin", ""))
    if not device["pin_hash"]:
        return web.json_response({"session": ""})
    if check_pin(pin, device["pin_hash"]):
        db.update_device(device["id"], failed_pins=0)
        session = secrets.token_urlsafe(24)
        pin_sessions[session] = (device["id"], time.time())
        return web.json_response({"session": session})
    attempts = device["failed_pins"] + 1
    max_attempts = db.setting_int("max_pin_attempts")
    if attempts >= max_attempts:
        db.update_device(device["id"], failed_pins=attempts, status="locked")
        db.log(device["name"], "Phone locked after wrong PINs", role=device["role_name"],
               ok=False, ip=client_ip(request))
        return bad_request("locked", 423)
    db.update_device(device["id"], failed_pins=attempts)
    return bad_request(f"Wrong PIN. {max_attempts - attempts} tries left.", 403)


async def api_entities(request: web.Request):
    device = authenticate(request)
    allowed = db.role_entities(device["role_id"]) if device["role_id"] else {}
    if not allowed:
        return web.json_response({"entities": []})
    try:
        states, areas = await ha.states(), await ha.areas()
    except HomeAssistantError as err:
        log.warning("Home Assistant unavailable: %s", err)
        return bad_request("Can't reach the system right now.", 503)
    entities = [entity_view(eid, states.get(eid), areas, lvl) for eid, lvl in allowed.items()]
    entities.sort(key=lambda e: (e["area"] or "~", e["name"].lower()))
    return web.json_response({"entities": entities})


async def api_action(request: web.Request):
    device = authenticate(request)
    data = await read_json(request)
    entity_id = str(data.get("entity_id", ""))
    action = str(data.get("action", ""))
    params = data.get("params") or {}
    ip = client_ip(request)
    allowed = db.role_entities(device["role_id"]) if device["role_id"] else {}
    if allowed.get(entity_id) != "control" or not isinstance(params, dict):
        db.log(device["name"], f"Blocked: {action}", role=device["role_name"],
               entity_id=entity_id, ok=False, ip=ip)
        return bad_request("You don't have access to control that.", 403)
    try:
        states = await ha.states()
        domain, service, call = build_service_call(entity_id, action, params, states.get(entity_id))
        await ha.call_service(domain, service, call)
    except ActionRejected as err:
        return bad_request(str(err))
    except HomeAssistantError as err:
        log.warning("Service call failed: %s", err)
        db.log(device["name"], action, role=device["role_name"], entity_id=entity_id,
               detail=str(err)[:200], ok=False, ip=ip)
        return bad_request("The system didn't accept that. Try again.", 502)
    detail = ", ".join(f"{k}={v}" for k, v in params.items()) or None
    db.log(device["name"], action, role=device["role_name"], entity_id=entity_id,
           detail=detail, ip=ip)
    return web.json_response({"ok": True})


@web.middleware
async def staff_headers(request: web.Request, handler):
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
    app.router.add_get("/api/entities", api_entities)
    app.router.add_post("/api/action", api_action)
    return app


# ---------------------------------------------------------------- admin dashboard

def admin_name(request: web.Request) -> str:
    return request.headers.get("X-Remote-User-Display-Name") or "Admin"


def setup_url(code: str | None) -> str | None:
    base = db.settings()["public_url"].rstrip("/")
    return f"{base}/e/{code}" if base and code else None


def device_payload() -> list[dict]:
    devices = db.devices()
    for d in devices:
        d["setup_url"] = setup_url(d["enroll_code"])
    return devices


async def admin_index(request: web.Request):
    return web.FileResponse(os.path.join(HERE, "admin", "index.html"),
                            headers={"Cache-Control": "no-cache"})


async def admin_state(request: web.Request):
    return web.json_response({
        "settings": db.settings(),
        "roles": db.roles(),
        "devices": device_payload(),
        "has_logo": branding.has_logo(),
    })


async def admin_entities(request: web.Request):
    try:
        states, areas = await ha.states(max_age=0), await ha.areas(max_age=0)
    except HomeAssistantError as err:
        return bad_request(f"Can't reach Home Assistant: {err}", 503)
    entities = [entity_view(eid, st, areas) for eid, st in states.items() if offered(eid)]
    entities.sort(key=lambda e: (e["area"] or "~", e["domain"], e["name"].lower()))
    return web.json_response({"entities": entities})


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
    try:
        db.update_role(role_id, name, cleaned)
    except Exception:
        return bad_request("A role with that name already exists.")
    db.log(admin_name(request), f"Updated role {name}", role=name,
           detail=f"{len(cleaned)} devices")
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
    device_id = db.create_device(name, parse_role(data.get("role_id")), parse_pin(data.get("pin")))
    db.log(admin_name(request), f"Added phone {name}")
    return web.json_response({"id": device_id})


async def admin_update_device(request: web.Request):
    device = db.device(int(request.match_info["id"]))
    if not device:
        raise web.HTTPNotFound()
    data = await read_json(request)
    fields = {}
    if "name" in data and clean_name(data["name"]):
        fields["name"] = clean_name(data["name"])
    if "role_id" in data:
        fields["role_id"] = parse_role(data["role_id"])
    if "pin" in data:
        pin = parse_pin(data["pin"])
        fields["pin_hash"] = hash_pin(pin) if pin else None
        for token, (dev_id, _) in list(pin_sessions.items()):
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
    db.delete_device(device["id"])
    db.log(admin_name(request), f"Deleted phone {device['name']}")
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
    raw = await field.read(decode=True)
    try:
        await asyncio.to_thread(branding.save_logo, raw)
    except Exception:
        return bad_request("That file isn't an image we can use. Try a PNG or JPG.")
    db.log(admin_name(request), "Uploaded logo")
    return web.json_response({"ok": True})


async def admin_delete_logo(request: web.Request):
    branding.remove_logo()
    return web.json_response({"ok": True})


@web.middleware
async def admin_guard(request: web.Request, handler):
    if request.remote not in ADMIN_ALLOWED_IPS:
        return web.Response(status=403, text="Open Staff Access from the Home Assistant sidebar.")
    try:
        return await handler(request)
    except ValueError:
        return bad_request("invalid value")


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
    app.router.add_delete(r"/api/roles/{id:\d+}", admin_delete_role)
    app.router.add_post("/api/devices", admin_create_device)
    app.router.add_patch(r"/api/devices/{id:\d+}", admin_update_device)
    app.router.add_delete(r"/api/devices/{id:\d+}", admin_delete_device)
    app.router.add_post(r"/api/devices/{id:\d+}/{action:revoke|reenroll|unlock}", admin_device_action)
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
    log.info("Admin on :%s (ingress), staff app on :%s", ADMIN_PORT, STAFF_PORT)
    try:
        await asyncio.Event().wait()
    finally:
        for runner in runners:
            await runner.cleanup()
        await ha.close()


if __name__ == "__main__":
    asyncio.run(main())
