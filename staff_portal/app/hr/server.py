"""The HR portal's web server (HR_PORT, 8788): its pages and API.

Every request, in this order:
1. must come through the Cloudflare Tunnel (always: there's no setting to turn this off);
2. gets a plain "turned off" page while HR is off, and a "locked" page while its key is missing;
3. must be for the HR address (the Host header);
4. a change (anything but GET or HEAD) must carry the HR address as its Origin, the header
   X-Requested-With: hr-portal, and Sec-Fetch-Site: same-origin, which other websites can't send;
5. is checked against its route's rule (access.py): signed in, role, the person it names, and a
   fresh code (or PIN) for sensitive actions. A route without a rule is never served, and
   make_app refuses to start with one.

Pages are static files (web/: index.html, hr.js, hr.css, and any hr-*.js other parts add) under a
strict Content-Security-Policy: no inline script or style, nothing from other sites. Text is
always plain text. Nothing is cached.

Other parts of the HR portal add routes (add_routes), sections of a person's page (section),
to-dos (todo) and HR settings (settings_section).
"""

import asyncio
import hashlib
import json
import os
import re
import time

import segno
from aiohttp import web

from . import access, audit, crypto, notify, people, signin, totp
from .access import Refused, rule
from .service import HRService

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
SERVICE = web.AppKey("hr_service", HRService)
VIEWER = web.RequestKey("hr_viewer", object)       # access.Viewer, or None when signed out
PERSON = web.RequestKey("hr_person", dict)         # the person a route names (rule person=True)
RELATION = web.RequestKey("hr_relation", str)      # ...and how the viewer sees their file
# For trying the portal on this computer over plain http (never set in the add-on).
TEST_ORIGIN = os.environ.get("HR_TEST_ORIGIN", "")
MAX_JSON = 64 * 1024
MAX_BODY = 32 * 1024 * 1024      # uploads (other parts) check their own, smaller limits
BODY_SECONDS = 30
STATIC_NAME = re.compile(r"^[a-z0-9-]{1,40}\.(js|css|svg|png)$")
STATIC_TYPES = {"js": "text/javascript", "css": "text/css", "svg": "image/svg+xml",
                "png": "image/png"}

OFF_PAGE = "The HR portal is turned off."
LOCKED_PAGE = ("The HR portal is locked. The owner can unlock it with the HR recovery key in "
               "Home Assistant (Staff Access, HR portal tab).")
TUNNEL_ONLY = "The HR portal is only available through the property's secure address."
NOT_FOUND = "That isn't here, or you can't see it."
SETUP_GONE = "This setup code has expired or was already used. Ask HR for a new one."
CODE_WRONG = "That code didn't work. Type the newest code your authenticator app shows."
PIN_WRONG = "That PIN didn't work. Type your Connecteam kiosk code."
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                               "img-src 'self' data:; connect-src 'self'; manifest-src 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=(), payment=(), usb=()",
    "Strict-Transport-Security": "max-age=31536000",
    "Cache-Control": "no-store",
}

ROUTE_ADDERS: list = []     # fn(app) -> None: other parts' routes (each with a rule)
SECTIONS: list = []         # (order, name, build(svc, viewer, person, relation) -> dict | None)
TODOS: list = []            # fn(svc, viewer) -> [{"text", "link"}]
SETTINGS_SECTIONS: list = []   # (order, name, build(svc, viewer) -> dict | None)


def add_routes(fn):
    ROUTE_ADDERS.append(fn)
    return fn


def section(name: str, order: int):
    """A section of a person's page: build(svc, viewer, person, relation) returns
    {"title", "rows": [{"label", "value"}], "notes": [...], "actions": [...]}, or None to leave
    it out for this viewer. Text in it is plain text."""
    def add(build):
        SECTIONS[:] = [s for s in SECTIONS if s[1] != name] + [(order, name, build)]
        SECTIONS.sort(key=lambda s: s[0])
        return build
    return add


def todo(fn):
    TODOS.append(fn)
    return fn


def settings_section(name: str, order: int):
    def add(build):
        SETTINGS_SECTIONS[:] = [s for s in SETTINGS_SECTIONS if s[1] != name] + [(order, name, build)]
        SETTINGS_SECTIONS.sort(key=lambda s: s[0])
        return build
    return add


# ---------------------------------------------------------------- helpers

def svc_of(request: web.Request) -> HRService:
    return request.app[SERVICE]


def error(message: str, status: int = 400, **extra) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


def plain(status: int, text: str) -> web.Response:
    """A page with nothing but a sentence (readable on a phone, light or dark)."""
    body = ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width, initial-scale=1"><meta name="color-scheme" '
            f'content="light dark"><title>HR portal</title><p>{_esc(text)}</p></html>')
    return web.Response(status=status, text=body, content_type="text/html")


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


async def read_json(request: web.Request) -> dict:
    if request.content_length is None or request.content_length > MAX_JSON:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_JSON, actual_size=request.content_length or 0)
    try:
        async with asyncio.timeout(10):
            data = await request.json()
    except TimeoutError:
        raise web.HTTPRequestTimeout(text="The request took too long to arrive.") from None
    except (ValueError, RecursionError):
        raise web.HTTPBadRequest(text="That request couldn't be read.") from None
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="That request couldn't be read.")
    return data


async def read_optional(request: web.Request) -> dict:
    return await read_json(request) if request.can_read_body else {}


def viewer_of(request: web.Request) -> access.Viewer:
    return request[VIEWER]


def actor(request: web.Request) -> tuple[str, str]:
    v = viewer_of(request)
    return v.id, v.name


def qr_data_uri(text: str) -> str:
    return segno.make(text, error="m").svg_data_uri(scale=5, border=2)


def host_of(request: web.Request) -> str:
    host = (request.host or "").strip().lower()
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def expected_origin(svc: HRService) -> str:
    return TEST_ORIGIN or f"https://{svc.host()}"


def same_origin(request: web.Request, svc: HRService) -> bool:
    return (request.headers.get("Origin") == expected_origin(svc)
            and request.headers.get("X-Requested-With") == "hr-portal"
            and request.headers.get("Sec-Fetch-Site") == "same-origin")


def may_sign_in(person: dict | None) -> bool:
    return bool(person) and not person["archived"] and not person["access_ended"] \
        and bool(person["kiosk_hash"])


# ---------------------------------------------------------------- the guard

@web.middleware
async def guard(request: web.Request, handler):
    try:
        response = await checked(request, handler)
    except web.HTTPException as exc:
        for key, value in SECURITY_HEADERS.items():
            exc.headers.setdefault(key, value)
        raise
    except Refused as err:
        response = error(str(err), err.status)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


async def checked(request: web.Request, handler):
    svc = svc_of(request)
    if not svc.from_tunnel(request.remote):
        return plain(403, TUNNEL_ONLY)
    state = svc.status()
    if state == "off":
        return plain(503, OFF_PAGE)
    if state != "on":
        return plain(503, LOCKED_PAGE)
    if not svc.host() or host_of(request) != svc.host().lower():
        return plain(421, "This address isn't the HR portal's.")
    if request.method not in ("GET", "HEAD") and not same_origin(request, svc):
        return error("Request blocked.", 403)
    if request.path.startswith("/api/") and request.headers.get(
            "Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
        return error("Request blocked.", 403)
    r = access.rule_of(request.match_info.route.handler)
    if r is None:
        return error(NOT_FOUND, 404)
    db = svc.db
    session = signin.session_for(db, request.cookies.get(signin.COOKIE),
                                 touch=request.headers.get("X-Background") != "1")
    viewer = None
    if session:
        person = db.person(session["person_id"])
        if may_sign_in(person):
            viewer = access.viewer_for(db, person, session)
        else:
            signin.end_session(db, session["id"])
    request[VIEWER] = viewer
    if not access.allows(r, viewer):
        if viewer is None:
            return error("signed_out", 401)
        return error("Only HR admins can do this." if r.who == "hr" else
                     "Only managers, directors and HR can do this.", 403)
    if r.person:
        pid = request.match_info.get("pid", "")
        person = db.person(pid)
        if r.not_self and person and person["id"] == viewer.id:
            return error(access.NOT_YOURS, 403)
        rel = access.person_relation(db, viewer, person)
        if rel is None or (r.who == "hr" and rel != "hr"):
            return error(NOT_FOUND, 404)
        request[PERSON], request[RELATION] = person, rel
    if r.sensitive and not signin.confirmed(session):
        return error("reconfirm", 401, method="code" if db.person(viewer.id)["totp_on"] else "pin")
    return await handler(request)


# ---------------------------------------------------------------- pages

def fingerprint() -> str:
    digest = hashlib.sha1()
    for name in sorted(os.listdir(WEB)):
        with open(os.path.join(WEB, name), "rb") as f:
            digest.update(name.encode() + f.read())
    return digest.hexdigest()[:10]


ASSETS = [""]


def extra_scripts() -> list[str]:
    """Scripts other parts of the HR portal add (web/hr-*.js), after hr.js."""
    return sorted(n for n in os.listdir(WEB) if re.fullmatch(r"hr-[a-z0-9-]+\.js", n))


@rule("public")
async def index(request: web.Request):
    svc = svc_of(request)
    if not ASSETS[0]:
        ASSETS[0] = fingerprint()
    with open(os.path.join(WEB, "index.html"), encoding="utf-8") as f:
        html = f.read()
    scripts = "\n".join(f'<script src="/static/{name}?v={ASSETS[0]}" defer></script>'
                        for name in ["hr.js", *extra_scripts()])
    html = (html.replace("{{TITLE}}", _esc(f"{svc.property_name() or 'Staff Access'} HR"))
            .replace("{{VERSION}}", ASSETS[0]).replace("{{SCRIPTS}}", scripts))
    return web.Response(text=html, content_type="text/html")


@rule("public")
async def static_file(request: web.Request):
    name = request.match_info["name"]
    if not STATIC_NAME.match(name) or not os.path.isfile(os.path.join(WEB, name)):
        return error(NOT_FOUND, 404)
    with open(os.path.join(WEB, name), "rb") as f:
        body = f.read()
    return web.Response(body=body, content_type=STATIC_TYPES[name.rsplit(".", 1)[1]])


@rule("public")
async def icon(request: web.Request):
    png = svc_of(request).icon()
    if not png:
        return error(NOT_FOUND, 404)
    return web.Response(body=png, content_type="image/png")


# ---------------------------------------------------------------- signing in

def failed(svc: HRService, ip: str, person: dict | None, detail: str) -> web.Response:
    svc.throttle.failed(ip)
    audit.record(svc.db, "Sign-in failed", target_id=person and person["id"],
                 target=person and person["name"], detail=detail, ip=ip, security=True)
    return error(signin.FAILED, 401)


def wrong(svc: HRService, person: dict, ip: str) -> None:
    """A wrong PIN or code for a known person: counted, and their lock noted in the audit log."""
    lock = signin.wrong_try(svc.db, person)
    if lock:
        audit.record(svc.db, "Locked until HR unlocks it" if lock == "hard_locked"
                     else "Locked for 15 minutes", target_id=person["id"], target=person["name"],
                     detail="Too many wrong tries", ip=ip, security=True)


def signed_in(svc: HRService, request: web.Request, person: dict, ip: str, via_recovery: bool,
              reply: dict | None = None) -> web.Response:
    """Start a session (the cookie goes with the reply), clearing their wrong tries."""
    db = svc.db
    signin.unlock(db, person["id"])
    db.update_person(person["id"], prev_signin_at=person["last_signin_at"],
                     prev_signin_ip=person["last_signin_ip"], last_signin_at=time.time(),
                     last_signin_ip=ip[:64])
    token = signin.new_session(db, person["id"], ip, request.headers.get("User-Agent", ""),
                               via_recovery)
    audit.record(db, "Signed in" + (" with a recovery code" if via_recovery else ""),
                 actor_id=person["id"], actor=person["name"], ip=ip, security=True)
    response = web.json_response({"ok": True, **(reply or {})})
    response.headers.add("Set-Cookie", signin.cookie(token))
    return response


@rule("public")
async def api_signin(request: web.Request):
    svc = svc_of(request)
    db = svc.db
    ip = svc.client_ip(request)
    refusal = svc.throttle.refusal(ip)
    if refusal:
        return error(refusal, 429)
    data = await read_json(request)
    typed = str(data.get("mobile") or "")[:80]
    pin = signin.clean_pin(data.get("pin"))
    code = str(data.get("code") or "").strip()[:20]
    try:
        person = signin.find_person(db, typed) if typed else None
        if person is None:
            if signin.normalize_mobile(typed):
                svc.sync_soon()   # someone just added in Connecteam can sign in a few minutes later
            await signin.pin_matches(pin or "0000", None)   # as slow as a real check
            return failed(svc, ip, None, "No one with that number or name")
        if signin.locked(person) or not may_sign_in(person) or pin is None:
            await signin.pin_matches(pin or "0000", None)
            why = ("Locked" if signin.locked(person) else "Access ended" if person["access_ended"]
                   or person["archived"] else people.NO_KIOSK_CODE if not person["kiosk_hash"]
                   else "No PIN typed")
            return failed(svc, ip, person, why)
        recovery = False
        if person["totp_on"]:
            # The code first. A wrong one still waits as long as a PIN check, so timing doesn't
            # tell who has an authenticator (staff roles, under the usual setting).
            if not signin.check_totp(db, person, code):
                if not signin.clean_recovery_code(code):
                    await signin.pin_matches(pin, None)
                    wrong(svc, person, ip)
                    return failed(svc, ip, person, "Wrong authenticator code")
                recovery = True
        ok = await signin.pin_matches(pin, person["kiosk_hash"])
    except signin.Busy as err:
        return error(str(err), 503)
    person = db.person(person["id"])   # fresh: another try may have locked them meanwhile
    if not person or signin.locked(person) or not may_sign_in(person):
        return failed(svc, ip, person, "Locked")
    if not ok:
        wrong(svc, person, ip)
        return failed(svc, ip, person, "Wrong PIN")
    if recovery and not signin.use_recovery_code(db, person["id"], code):
        wrong(svc, person, ip)
        return failed(svc, ip, person, "Wrong authenticator code")
    if signin.needs_setup_code(db, person):
        # Their role needs an authenticator, and only a setup code from HR can add one.
        return failed(svc, ip, person, "Needs an authenticator: no setup code used yet")
    if recovery:
        db.update_person(person["id"], totp_redo=1)
        for admin in db.with_role(access.HR_ADMIN):
            if admin != person["id"]:
                notify.notify(admin, "recovery_code_used", db=db,
                              title=f"{person['name']} signed in with a recovery code.",
                              link=f"#/people/{person['id']}")
    return signed_in(svc, request, person, ip, recovery)


@rule("signed_in")
async def api_signout(request: web.Request):
    svc = svc_of(request)
    v = viewer_of(request)
    signin.end_session(svc.db, v.session["id"])
    audit.record(svc.db, "Signed out", actor_id=v.id, actor=v.name, ip=svc.client_ip(request),
                 security=True)
    response = web.json_response({"ok": True})
    response.headers.add("Set-Cookie", signin.cleared_cookie())
    return response


@rule("signed_in")
async def api_signout_everywhere(request: web.Request):
    svc = svc_of(request)
    v = viewer_of(request)
    count = signin.end_sessions(svc.db, v.id)
    audit.record(svc.db, "Signed out everywhere", actor_id=v.id, actor=v.name,
                 detail=f"{count} device(s)", ip=svc.client_ip(request), security=True)
    response = web.json_response({"ok": True, "ended": count})
    response.headers.add("Set-Cookie", signin.cleared_cookie())
    return response


@rule("signed_in")
async def api_reconfirm(request: web.Request):
    """A fresh authenticator code (or the PIN, without one) before a sensitive action."""
    svc = svc_of(request)
    db = svc.db
    v = viewer_of(request)
    ip = svc.client_ip(request)
    data = await read_json(request)
    person = db.person(v.id)
    if signin.locked(person):
        return error("Too many wrong tries. Wait 15 minutes, or ask another HR admin.", 403)
    if person["totp_on"]:
        ok, message = signin.check_totp(db, person, data.get("code")), CODE_WRONG
    else:
        pin = signin.clean_pin(data.get("pin"))
        try:
            ok = bool(pin) and await signin.pin_matches(pin, person["kiosk_hash"])
        except signin.Busy as err:
            return error(str(err), 503)
        message = PIN_WRONG
    if not ok:
        wrong(svc, db.person(v.id), ip)
        audit.record(db, "Confirmation failed", actor_id=v.id, actor=v.name, ip=ip, security=True)
        return error(message, 403)
    signin.mark_confirmed(db, v.session["id"])
    return web.json_response({"ok": True})


# ---------------------------------------------------------------- setting up an authenticator

def setup_row(request: web.Request) -> tuple[str, dict, dict] | None:
    svc = svc_of(request)
    token = request.match_info["token"]
    row = signin.setup_token(svc.db, token)
    person = svc.db.person(row["person_id"]) if row else None
    # Ended access needs HR to allow it again (or a new setup code, which does). A code someone
    # made for themselves in My account is finished there, not here.
    if (not row or not person or person["archived"] or person["access_ended"]
            or row["created_by"] == person["id"]):
        svc.throttle.failed(svc.client_ip(request))
        return None
    return token, row, person


def account_label(svc: HRService, person: dict) -> tuple[str, str]:
    issuer = f"{svc.property_name() or 'Staff Access'} HR"[:40].replace(":", "")
    return issuer, person["name"][:40].replace(":", "")


@rule("public")
async def api_setup_info(request: web.Request):
    svc = svc_of(request)
    if refusal := svc.throttle.refusal(svc.client_ip(request)):
        return error(refusal, 429)
    found = setup_row(request)
    if not found:
        return error(SETUP_GONE, 404)
    _, row, person = found
    return web.json_response({"name": person["name"], "expires_at": row["expires_at"],
                              "pin_ok": bool(row["pin_ok_at"]),
                              "has_code": bool(person["kiosk_hash"])})


@rule("public")
async def api_setup_pin(request: web.Request):
    """Step 2 of setup: the person proves who they are with their PIN (kiosk code), then gets
    the authenticator's QR code."""
    svc = svc_of(request)
    db = svc.db
    ip = svc.client_ip(request)
    if refusal := svc.throttle.refusal(ip):
        return error(refusal, 429)
    data = await read_json(request)
    found = setup_row(request)
    if not found:
        return error(SETUP_GONE, 404)
    token, row, person = found
    if not person["kiosk_hash"]:
        return error("Connecteam has no kiosk code for you, so you can't set this up yet. Ask HR.")
    if signin.locked(person):
        return error("Too many wrong tries. Wait 15 minutes, or ask HR to unlock you.", 403)
    pin = signin.clean_pin(data.get("pin"))
    try:
        ok = bool(pin) and await signin.pin_matches(pin, person["kiosk_hash"])
    except signin.Busy as err:
        return error(str(err), 503)
    if not ok:
        svc.throttle.failed(ip)
        wrong(svc, db.person(person["id"]), ip)
        audit.record(db, "Setup: wrong PIN", target_id=person["id"], target=person["name"], ip=ip,
                     security=True)
        return error(PIN_WRONG, 403)
    secret = totp.new_secret()
    signin.update_setup_token(db, token, pin_ok_at=time.time(),
                              pending_secret=crypto.seal_text(secret, "setup.totp"))
    issuer, label = account_label(svc, person)
    return web.json_response({"secret": totp.grouped(secret),
                              "qr": qr_data_uri(totp.uri(secret, label, issuer)),
                              "issuer": issuer, "account": label})


@rule("public")
async def api_setup_finish(request: web.Request):
    """Step 3: the first code from the app proves it worked. The authenticator is turned on, the
    recovery codes are shown once, and the person is signed in."""
    svc = svc_of(request)
    db = svc.db
    ip = svc.client_ip(request)
    if refusal := svc.throttle.refusal(ip):
        return error(refusal, 429)
    data = await read_json(request)
    found = setup_row(request)
    if not found:
        return error(SETUP_GONE, 404)
    token, row, person = found
    if not row["pin_ok_at"] or not row["pending_secret"]:
        return error("Type your PIN first.", 409)
    secret = crypto.unseal_text(row["pending_secret"], "setup.totp")
    step = totp.check(secret, data.get("code"))
    if step is None:
        svc.throttle.failed(ip)
        return error(CODE_WRONG, 403)
    signin.update_setup_token(db, token, used_at=time.time(), pending_secret=None)
    codes = signin.enable_totp(db, person["id"], secret, step,
                               "owner" if row["created_by"] == "owner" else "hr")
    signin.end_sessions(db, person["id"])   # the old phone, if any, stops working
    audit.record(db, "Set up an authenticator", actor_id=person["id"], actor=person["name"],
                 ip=ip, security=True)
    return signed_in(svc, request, db.person(person["id"]), ip, False, {"recovery_codes": codes})


# ---------------------------------------------------------------- me, home and inbox

def group_names(svc: HRService) -> dict[str, str]:
    names = dict(svc.group_names())
    for d in svc.departments():
        if d.get("smart_group_id"):
            names[str(d["smart_group_id"])] = d["name"]
    return names


def when_text(ts: float | None) -> float | None:
    return round(ts) if ts else None


@rule("signed_in")
async def api_me(request: web.Request):
    svc = svc_of(request)
    db = svc.db
    v = viewer_of(request)
    me = db.person(v.id)
    return web.json_response({
        "id": v.id, "name": v.name, "role": v.role, "role_word": access.ROLE_WORDS[v.role],
        "hr": v.is_hr, "staff": v.is_staff, "totp_on": bool(me["totp_on"]),
        "totp_required": signin.totp_required(db, me, set(v.roles)),
        "totp_redo": bool(me["totp_redo"]), "unread": notify.unread(db, v.id),
        "last_signin": {"at": when_text(me["prev_signin_at"]), "ip": me["prev_signin_ip"]},
        "idle_minutes": signin.idle_seconds(db) // 60, "property": svc.property_name(),
    })


@todo
def own_todos(svc: HRService, v: access.Viewer) -> list[dict]:
    me = svc.db.person(v.id)
    found = []
    if me["totp_redo"]:
        found.append({"text": "You signed in with a recovery code. Set up your authenticator "
                              "again in My account.", "link": "#/account"})
    elif me["totp_on"] and signin.recovery_codes_left(svc.db, v.id) <= 2:
        found.append({"text": "You have few recovery codes left. Make new ones in My account.",
                      "link": "#/account"})
    return found


@todo
def hr_todos(svc: HRService, v: access.Viewer) -> list[dict]:
    if not v.is_hr:
        return []
    db, found = svc.db, []
    if warning := db.setting("sync_warning"):
        found.append({"text": warning, "link": "#/people"})
    if svc.sync_state.get("error"):
        found.append({"text": f"People couldn't be read from Connecteam: {svc.sync_state['error']}",
                      "link": "#/people"})
    counts = db.conn.execute(
        "SELECT SUM(start_estimated = 1 AND start_confirmed = 0), SUM(kiosk_hash IS NULL), "
        "SUM(access_ended = 1) FROM people WHERE archived = 0").fetchone()
    if counts[0]:
        found.append({"text": f"{counts[0]} start date{'s' if counts[0] != 1 else ''} need "
                              "confirming (Connecteam's start date field is empty).",
                      "link": "#/people?todo=start"})
    if counts[1]:
        found.append({"text": f"{counts[1]} {'people have' if counts[1] != 1 else 'person has'} "
                              "no kiosk code in Connecteam, so they can't sign in.",
                      "link": "#/people?todo=code"})
    if counts[2]:
        found.append({"text": f"{counts[2]} {'people' if counts[2] != 1 else 'person'} back in "
                              "Connecteam or with access ended: allow sign-in again if they "
                              "should have it.", "link": "#/people?todo=access"})
    if db.setting("audit_check") == "failed":
        found.append({"text": "The last audit log check found a problem.", "link": "#/audit"})
    return found


@rule("signed_in")
async def api_home(request: web.Request):
    svc = svc_of(request)
    v = viewer_of(request)
    todos = []
    for fn in TODOS:
        try:
            todos.extend(fn(svc, v) or [])
        except Exception as err:  # noqa: BLE001 - one broken list mustn't hide the rest
            todos.append({"text": f"Couldn't check something: {type(err).__name__}", "link": ""})
    return web.json_response({"todos": todos, "inbox": notify.inbox(svc.db, v.id)})


@rule("signed_in")
async def api_inbox_read(request: web.Request):
    svc = svc_of(request)
    data = await read_optional(request)
    entry = data.get("id")
    notify.mark_read(svc.db, viewer_of(request).id,
                     entry if isinstance(entry, int) and not isinstance(entry, bool) else None)
    return web.json_response({"ok": True, "unread": notify.unread(svc.db, viewer_of(request).id)})


# ---------------------------------------------------------------- my account

@rule("signed_in")
async def api_account(request: web.Request):
    svc = svc_of(request)
    db = svc.db
    v = viewer_of(request)
    me = db.person(v.id)
    sessions = [{"id": r["id"], "created_at": when_text(r["created_at"]),
                 "last_used": when_text(r["last_used"]), "ip": r["ip"], "agent": r["agent"],
                 "current": r["id"] == v.session["id"]}
                for r in db.conn.execute("SELECT * FROM sessions WHERE person_id = ? "
                                         "ORDER BY last_used DESC", (v.id,))]
    return web.json_response({
        "totp_on": bool(me["totp_on"]), "totp_required": signin.totp_required(db, me, set(v.roles)),
        "recovery_left": signin.recovery_codes_left(db, v.id) if me["totp_on"] else 0,
        "sessions": sessions, "confirmed": signin.confirmed(v.session),
    })


@rule("signed_in")
async def api_totp_start(request: web.Request):
    """Turn on an authenticator, or move it to a new phone. Someone without one re-types their
    PIN; someone with one confirms with a code first. People who must use one but don't have one
    need a setup code from HR (so a kiosk code alone can't add a phone to a manager's account)."""
    svc = svc_of(request)
    db = svc.db
    v = viewer_of(request)
    me = db.person(v.id)
    ip = svc.client_ip(request)
    data = await read_json(request)
    if me["totp_on"]:
        if not signin.confirmed(v.session):
            return error("reconfirm", 401, method="code")
    else:
        if signin.totp_required(db, me, set(v.roles)):
            return error("Your role needs an authenticator set up with a setup code from HR. Ask "
                         "an HR admin.", 403)
        pin = signin.clean_pin(data.get("pin"))
        try:
            ok = bool(pin) and await signin.pin_matches(pin, me["kiosk_hash"])
        except signin.Busy as err:
            return error(str(err), 503)
        if not ok:
            wrong(svc, me, ip)
            return error(PIN_WRONG, 403)
    token, expires = signin.new_setup_token(db, v.id, "qr", v.id)
    secret = totp.new_secret()
    signin.update_setup_token(db, token, pin_ok_at=time.time(),
                              pending_secret=crypto.seal_text(secret, "setup.totp"))
    issuer, label = account_label(svc, me)
    return web.json_response({"token": token, "secret": totp.grouped(secret),
                              "qr": qr_data_uri(totp.uri(secret, label, issuer)),
                              "expires_at": expires})


@rule("signed_in")
async def api_totp_finish(request: web.Request):
    svc = svc_of(request)
    db = svc.db
    v = viewer_of(request)
    data = await read_json(request)
    token = str(data.get("token") or "")
    row = signin.setup_token(db, token)
    if not row or row["person_id"] != v.id or not row["pending_secret"]:
        return error("That setup has expired. Start again.", 409)
    secret = crypto.unseal_text(row["pending_secret"], "setup.totp")
    step = totp.check(secret, data.get("code"))
    if step is None:
        return error(CODE_WRONG, 403)
    me = db.person(v.id)
    had = bool(me["totp_on"])
    signin.update_setup_token(db, token, used_at=time.time(), pending_secret=None)
    # A new phone keeps how the old one was set up (a fresh code from it came first).
    codes = signin.enable_totp(db, v.id, secret, step, me["totp_source"] if had else "self")
    # Every other device signs in again with the new phone.
    db.conn.execute("DELETE FROM sessions WHERE person_id = ? AND id != ?", (v.id, v.session["id"]))
    db.conn.commit()
    audit.record(db, "Set up a new phone" if had else "Turned on an authenticator", actor_id=v.id,
                 actor=v.name, ip=svc.client_ip(request), security=True)
    return web.json_response({"ok": True, "recovery_codes": codes})


@rule("signed_in", sensitive=True)
async def api_totp_off(request: web.Request):
    svc = svc_of(request)
    db = svc.db
    v = viewer_of(request)
    me = db.person(v.id)
    if signin.totp_required(db, me, set(v.roles)):
        return error("Your role needs an authenticator, so it can't be turned off.", 403)
    signin.disable_totp(db, v.id)
    audit.record(db, "Turned off the authenticator", actor_id=v.id, actor=v.name,
                 ip=svc.client_ip(request), security=True)
    return web.json_response({"ok": True})


@rule("signed_in", sensitive=True)
async def api_recovery_codes(request: web.Request):
    svc = svc_of(request)
    v = viewer_of(request)
    if not svc.db.person(v.id)["totp_on"]:
        return error("Turn on an authenticator first.", 409)
    codes = signin.new_recovery_codes(svc.db, v.id)
    audit.record(svc.db, "Made new recovery codes", actor_id=v.id, actor=v.name,
                 ip=svc.client_ip(request), security=True)
    return web.json_response({"recovery_codes": codes})


@rule("signed_in")
async def api_end_device(request: web.Request):
    svc = svc_of(request)
    v = viewer_of(request)
    sid = int(request.match_info["sid"])
    gone = svc.db.conn.execute("DELETE FROM sessions WHERE id = ? AND person_id = ?",
                               (sid, v.id)).rowcount
    svc.db.conn.commit()
    if not gone:
        return error(NOT_FOUND, 404)
    audit.record(svc.db, "Signed out a device", actor_id=v.id, actor=v.name,
                 ip=svc.client_ip(request), security=True)
    return web.json_response({"ok": True, "current": sid == v.session["id"]})


# ---------------------------------------------------------------- people

def person_summary(svc: HRService, p: dict, rel: str, names: dict, roles: set[str]) -> dict:
    found = {"id": p["id"], "name": p["name"],
             "departments": [names.get(g, f"Group {g}") for g in people.groups(p)],
             "archived": bool(p["archived"]), "relation": rel,
             "role": access.ROLE_WORDS[next((r for r in access.STAFF_ROLES if r in roles),
                                            access.EMPLOYEE)]}
    if rel == "hr":
        flags = []
        if not p["kiosk_hash"]:
            flags.append(people.NO_KIOSK_CODE)
        if signin.locked(p):
            flags.append("Locked")
        if p["access_ended"] and not p["archived"]:
            flags.append("Access ended")
        if p["start_estimated"] and not p["start_confirmed"]:
            flags.append("Start date to confirm")
        found["flags"] = flags
    return found


@rule("signed_in")
async def api_people(request: web.Request):
    svc = svc_of(request)
    db = svc.db
    v = viewer_of(request)
    former = request.query.get("former") == "1" and v.is_hr
    q = " ".join(request.query.get("q", "").split()).casefold()[:60]
    names = group_names(svc)
    roles = db.all_roles()
    found = [person_summary(svc, p, rel, names, roles.get(p["id"], set()))
             for p, rel in access.visible_people(db, v, archived=former)
             if not q or q in p["name"].casefold()]
    audit.record(db, "Viewed the people list" + (" (former employees)" if former else ""),
                 actor_id=v.id, actor=v.name, ip=svc.client_ip(request))
    return web.json_response({"people": found[:2000], "former": former})


@section("details", 10)
def details_section(svc: HRService, v: access.Viewer, p: dict, rel: str) -> dict:
    names = group_names(svc)
    rows = [{"label": "Departments",
             "value": ", ".join(names.get(g, f"Group {g}") for g in people.groups(p)) or "None"}]
    start = p["start_date"] or "Not known"
    if p["start_date"] and p["start_estimated"] and not p["start_confirmed"]:
        start += " (estimated: the day they were added to Connecteam)"
    rows.append({"label": "Start date", "value": start})
    managers = [svc.person_name(m) for m in access.managers_of(p)]
    if managers:
        rows.append({"label": "Direct manager", "value": ", ".join(managers)})
    if rel in ("self", "hr"):
        rows.append({"label": "Mobile", "value": people.mobile_of(p) or "None in Connecteam"})
        rows.append({"label": "Email", "value": people.email_of(p) or "None in Connecteam"})
    rows.append({"label": "Status", "value": "Former employee" if p["archived"] else "Active"})
    actions = []
    if rel == "hr" and p["start_estimated"] and not p["start_confirmed"] and p["start_date"]:
        actions.append({"label": "Confirm start date", "method": "PUT",
                        "path": f"/api/people/{p['id']}/start-date", "ask_date": p["start_date"]})
    elif rel == "hr":
        actions.append({"label": "Change start date", "method": "PUT",
                        "path": f"/api/people/{p['id']}/start-date", "ask_date": p["start_date"] or ""})
    return {"title": "Details", "rows": rows, "actions": actions}


@section("signin", 90)
def signin_section(svc: HRService, v: access.Viewer, p: dict, rel: str) -> dict | None:
    if rel != "hr":
        return None
    db = svc.db
    roles = db.roles_of(p["id"])
    role_words = [access.ROLE_WORDS[r] for r in access.STAFF_ROLES if r in roles] or ["Employee"]
    base = f"/api/people/{p['id']}"
    rows = [{"label": "Role", "value": ", ".join(role_words)},
            {"label": "PIN", "value": "Their Connecteam kiosk code" if p["kiosk_hash"]
             else people.NO_KIOSK_CODE},
            {"label": "Authenticator", "value": "Needed, not set up yet"
             if signin.needs_setup_code(db, p, roles) else "On" if p["totp_on"] else "Off"},
            {"label": "Last sign-in", "value": p["last_signin_at"] and time.strftime(
                "%b %d, %Y %I:%M %p", time.localtime(p["last_signin_at"])) or "Never",
             "time": when_text(p["last_signin_at"])}]
    if p["signin_name_hash"]:
        rows.append({"label": "Sign-in name", "value": crypto.unseal_text(
            p["signin_name"], "people.signin_name") or ""})
    notes, actions = [], []
    if p["archived"]:
        notes.append("Archived in Connecteam: they can't sign in.")
    elif p["access_ended"]:
        notes.append("Their HR access is ended.")
        actions.append({"label": "Allow sign-in again", "method": "POST",
                        "path": f"{base}/allow-access"})
    if signin.locked(p):
        notes.append("Locked after too many wrong tries.")
        actions.append({"label": "Unlock", "method": "POST", "path": f"{base}/unlock"})
    if not p["archived"]:
        actions += [
            {"label": "Setup QR code", "method": "POST", "path": f"{base}/setup-code",
             "body": {"kind": "qr"}},
            {"label": "Setup link (72 hours)", "method": "POST", "path": f"{base}/setup-code",
             "body": {"kind": "link"}},
            {"label": "Sign-in name", "method": "PUT", "path": f"{base}/signin-name",
             "ask_text": "A sign-in name for someone without a mobile number (blank removes it)"},
            {"label": "Reset sign-in", "method": "POST", "path": f"{base}/reset-signin",
             "confirm": f"Reset {p['name']}'s HR sign-in? Their authenticator and recovery codes "
                        "are removed and they're signed out everywhere.", "danger": True}]
        if not p["access_ended"]:
            actions.append({"label": "End HR access now", "method": "POST",
                            "path": f"{base}/end-access", "danger": True,
                            "confirm": f"End {p['name']}'s HR access now? They're signed out at "
                                       "once and can't sign in until HR allows it again."})
    return {"title": "HR sign-in", "rows": rows, "notes": notes, "actions": actions}


@rule("signed_in", person=True)
async def api_person(request: web.Request):
    svc = svc_of(request)
    v = viewer_of(request)
    p, rel = request[PERSON], request[RELATION]
    sections = []
    for _, name, build in SECTIONS:
        try:
            built = build(svc, v, p, rel)
        except Refused:
            built = None
        if built:
            sections.append({"name": name, **built})
    names = group_names(svc)
    audit.record(svc.db, "Viewed a file", actor_id=v.id, actor=v.name, target_id=p["id"],
                 target=p["name"], ip=svc.client_ip(request))
    return web.json_response({"person": person_summary(svc, p, rel, names, svc.db.roles_of(p["id"])),
                              "sections": sections})


def hr_action(request: web.Request) -> tuple[HRService, dict, str, str, str]:
    svc = svc_of(request)
    v = viewer_of(request)
    return svc, request[PERSON], v.id, v.name, svc.client_ip(request)


@rule("hr", person=True, not_self=True, sensitive=True)
async def api_setup_code(request: web.Request):
    svc, p, aid, name, ip = hr_action(request)
    data = await read_optional(request)
    kind = "link" if data.get("kind") == "link" else "qr"
    if p["archived"]:
        return error("They're archived in Connecteam.", 409)
    token, expires = signin.new_setup_token(svc.db, p["id"], kind, aid)
    svc.db.update_person(p["id"], access_ended=0)
    url = f"{svc.url()}/setup/{token}"
    audit.record(svc.db, "Made a setup " + ("link" if kind == "link" else "QR code"),
                 actor_id=aid, actor=name, target_id=p["id"], target=p["name"], ip=ip, security=True)
    return web.json_response({"url": url, "qr": qr_data_uri(url), "expires_at": expires,
                              "message": "Send this link by hand in a Connecteam chat. It works "
                                         "once, for 72 hours." if kind == "link" else
                                         "Let them scan this with their phone's camera. It works "
                                         "once, for 15 minutes."})


@rule("hr", person=True, not_self=True, sensitive=True)
async def api_reset_signin(request: web.Request):
    svc, p, aid, name, ip = hr_action(request)
    svc.reset_signin(p["id"], aid, name, ip)
    return web.json_response({"ok": True, "message": f"{p['name']}'s sign-in was reset."})


@rule("hr", person=True, not_self=True, sensitive=True)
async def api_unlock(request: web.Request):
    svc, p, aid, name, ip = hr_action(request)
    svc.unlock_person(p["id"], aid, name, ip)
    return web.json_response({"ok": True, "message": f"{p['name']} is unlocked."})


@rule("hr", person=True, not_self=True, sensitive=True)
async def api_end_access(request: web.Request):
    svc, p, aid, name, ip = hr_action(request)
    svc.end_access(p["id"], aid, name, ip)
    return web.json_response({"ok": True, "message": f"{p['name']}'s HR access has ended."})


@rule("hr", person=True, not_self=True, sensitive=True)
async def api_allow_access(request: web.Request):
    svc, p, aid, name, ip = hr_action(request)
    if p["archived"]:
        return error("They're archived in Connecteam, so they can't sign in.", 409)
    svc.allow_access(p["id"], aid, name, ip)
    return web.json_response({"ok": True, "message": f"{p['name']} can sign in again."})


@rule("hr", person=True, not_self=True, sensitive=True)
async def api_signin_name(request: web.Request):
    svc, p, aid, name, ip = hr_action(request)
    data = await read_json(request)
    text = " ".join(str(data.get("value") or "").split())[:60]
    if not text:
        svc.db.update_person(p["id"], signin_name=None, signin_name_hash=None)
    else:
        key = signin.name_key(text)
        if not key:
            return error("A sign-in name is 3 to 60 characters, and can't be a phone number.")
        if svc.db.conn.execute("SELECT 1 FROM people WHERE signin_name_hash = ? AND id != ?",
                               (key, p["id"])).fetchone():
            return error("Someone else already has that sign-in name.")
        svc.db.update_person(p["id"], signin_name=crypto.seal_text(text, "people.signin_name"),
                             signin_name_hash=key)
    audit.record(svc.db, "Set a sign-in name" if text else "Removed the sign-in name", actor_id=aid,
                 actor=name, target_id=p["id"], target=p["name"], ip=ip, security=True)
    return web.json_response({"ok": True})


@rule("hr", person=True, not_self=True)
async def api_start_date(request: web.Request):
    svc, p, aid, name, ip = hr_action(request)
    data = await read_json(request)
    value = people.parse_date(str(data.get("value") or "").strip())
    if not value:
        return error("Type the date as YYYY-MM-DD, like 2026-10-25.")
    svc.db.update_person(p["id"], start_date=value, start_estimated=0, start_confirmed=1)
    audit.record(svc.db, "Confirmed the start date", actor_id=aid, actor=name, target_id=p["id"],
                 target=p["name"], detail=value, ip=ip)
    return web.json_response({"ok": True})


# ---------------------------------------------------------------- HR settings

POLICIES = {"everyone": "Everyone", "staff": "HR admins, directors and managers",
            "hr": "HR admins only", "nobody": "Nobody"}


def people_choices(svc: HRService) -> list[dict]:
    return [{"id": p["id"], "name": p["name"]} for p in svc.db.people(archived=False)]


def departments_list(svc: HRService) -> list[dict]:
    """HR's departments: Staff Access's departments with a smart group, and other smart groups
    people are in (by name when Connecteam's list is known). Smart groups whose department is
    hidden in Staff Access (Connecteam's groups for everyone, say) aren't offered."""
    names = group_names(svc)
    seen, found = set(), []
    for d in svc.departments():
        g = str(d.get("smart_group_id") or "")
        if g and g not in seen:
            seen.add(g)
            if not d.get("hidden"):
                found.append({"group": g, "name": d["name"]})
    for p in svc.db.people(archived=False):
        for g in people.groups(p):
            if g not in seen:
                seen.add(g)
                found.append({"group": g, "name": names.get(g, f"Group {g}")})
    return found


@settings_section("roles", 10)
def roles_settings(svc: HRService, v: access.Viewer) -> dict:
    db = svc.db
    roles = db.all_roles()
    groups = db.all_manager_groups()
    return {"hr_admins": [{"id": pid, "name": svc.person_name(pid)} for pid in db.with_role("hr_admin")],
            "directors": [{"id": pid, "name": svc.person_name(pid)} for pid in db.with_role("director")],
            "managers": [{"id": pid, "name": svc.person_name(pid), "groups": groups.get(pid, {}),
                          "hr_admin": "hr_admin" in roles.get(pid, set())}
                         for pid in db.with_role("manager")]}


@rule("hr")
async def api_settings(request: web.Request):
    svc = svc_of(request)
    db = svc.db
    v = viewer_of(request)
    general = {k: db.setting(k) for k in hr_general_keys()}
    extra = {}
    for _, name, build in SETTINGS_SECTIONS:
        built = build(svc, v)
        if built is not None:
            extra[name] = built
    return web.json_response({
        "general": general, "policies": POLICIES,
        "fields": db.setting_json("ct_fields", []) or [],
        "departments": departments_list(svc), "people": people_choices(svc),
        "key": {"kept_out": svc.keyring.kept_out()},
        "sections": extra, "me": v.id})


def hr_general_keys() -> tuple[str, ...]:
    return ("totp_policy", "session_minutes", "start_field", "manager_field", "date_order",
            "retention_years")


@rule("hr", sensitive=True)
async def api_settings_general(request: web.Request):
    svc = svc_of(request)
    v = viewer_of(request)
    data = await read_json(request)
    values = {}
    if "totp_policy" in data:
        if data["totp_policy"] not in POLICIES:
            return error("Choose who must use an authenticator code.")
        values["totp_policy"] = data["totp_policy"]
    for key, lo, hi, what in (("session_minutes", 5, 30, "The sign-out time"),
                              ("retention_years", 1, 30, "How long records are kept")):
        if key in data:
            try:
                values[key] = max(lo, min(hi, int(data[key])))
            except (TypeError, ValueError):
                return error(f"{what} must be a number.")
    field_ids = {str(f.get("id")) for f in svc.db.setting_json("ct_fields", []) or []}
    for key in ("start_field", "manager_field"):
        if key in data:
            value = str(data[key] or "")
            if value and value not in field_ids:
                return error("Choose a field from the list.")
            values[key] = value
    if "date_order" in data:
        if data["date_order"] not in ("mdy", "dmy"):
            return error("Choose how Connecteam writes dates.")
        values["date_order"] = data["date_order"]
    policy_changed = "totp_policy" in values and values["totp_policy"] != svc.db.setting(
        "totp_policy")
    svc.db.set_settings(values)
    audit.record(svc.db, "Changed HR settings", actor_id=v.id, actor=v.name,
                 detail=", ".join(sorted(values)), ip=svc.client_ip(request), security=True)
    if policy_changed:
        svc.check_authenticators(None, v.id, v.name, svc.client_ip(request))
    if {"start_field", "manager_field", "date_order"} & set(values):
        svc.sync_soon()
    return web.json_response({"ok": True})


@rule("hr", sensitive=True)
async def api_settings_role(request: web.Request):
    """Make someone a director or a manager (or neither). HR admins are the owner's to give."""
    svc = svc_of(request)
    db = svc.db
    v = viewer_of(request)
    data = await read_json(request)
    pid, role = str(data.get("person_id") or ""), data.get("role")
    person = db.person(pid)
    if not person or person["archived"]:
        return error("Choose someone from the list.")
    access.not_own_file(v, pid)
    if role not in ("director", "manager", "none"):
        return error("Choose Director, Manager or neither.")
    before = db.roles_of(pid)
    for r in ("director", "manager"):
        db.set_role(pid, r, r == role, by=v.id)
    after = db.roles_of(pid)
    if before != after:
        audit.record(db, "Changed role", actor_id=v.id, actor=v.name, target_id=pid,
                     target=person["name"], detail=access.ROLE_WORDS.get(role, "Employee"),
                     ip=svc.client_ip(request), security=True)
        svc.check_authenticators([pid], v.id, v.name, svc.client_ip(request))
    return web.json_response({"ok": True})


@rule("hr", person=True, not_self=True, sensitive=True)
async def api_manager_groups(request: web.Request):
    svc, p, aid, name, ip = hr_action(request)
    data = await read_json(request)
    if "manager" not in svc.db.roles_of(p["id"]):
        return error("Make them a manager first.", 409)
    known = {d["group"] for d in departments_list(svc)}
    raw = data.get("groups")
    if not isinstance(raw, dict) or len(raw) > 200 or not set(map(str, raw)) <= known:
        return error("Choose departments from the list.")
    svc.db.set_manager_groups(p["id"], {str(g): bool(primary) for g, primary in raw.items()})
    names = {d["group"]: d["name"] for d in departments_list(svc)}
    audit.record(svc.db, "Changed a manager's departments", actor_id=aid, actor=name,
                 target_id=p["id"], target=p["name"],
                 detail=", ".join(names[g] + (" (primary)" if pr else "") for g, pr in raw.items())
                 or "none", ip=ip, security=True)
    return web.json_response({"ok": True})


@rule("hr", sensitive=True)
async def api_settings_key(request: web.Request):
    svc = svc_of(request)
    v = viewer_of(request)
    data = await read_json(request)
    on = data.get("keep_out") is True
    if on == svc.keyring.kept_out():
        return web.json_response({"ok": True, "recovery_key": None})
    code = await svc.keep_key_out(on, v.id, v.name)
    return web.json_response({"ok": True, "recovery_key": code})


# ---------------------------------------------------------------- audit log

@rule("hr")
async def api_audit(request: web.Request):
    svc = svc_of(request)
    before = request.query.get("before", "")
    target = request.query.get("person") or None
    lines = audit.lines(svc.db, 100, int(before) if before.isdigit() else None,
                        target_id=target)
    return web.json_response({"lines": lines, "last_check": svc.db.setting_json("audit_result")})


@rule("hr")
async def api_audit_check(request: web.Request):
    svc = svc_of(request)
    v = viewer_of(request)

    def run():
        conn = svc.db.reader()
        try:
            return audit.check(conn)
        finally:
            conn.close()
    result = await asyncio.to_thread(run)
    svc.db.set_settings({"audit_check": "ok" if result["ok"] else "failed",
                         "audit_result": json.dumps(result)})
    audit.record(svc.db, "Checked the audit log", actor_id=v.id, actor=v.name,
                 detail="No changes found" if result["ok"] else "Lines are missing from the end"
                 if result.get("missing_end") else f"Line {result['first_bad']} doesn't match",
                 ip=svc.client_ip(request), security=True)
    return web.json_response(result)


# ---------------------------------------------------------------- the app

def make_app(svc: HRService) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=MAX_BODY)
    app[SERVICE] = svc
    r = app.router
    r.add_get("/", index)
    r.add_get(r"/setup/{token:[A-Za-z0-9_-]{20,64}}", index)
    r.add_get(r"/static/{name}", static_file)
    r.add_get("/icon.png", icon)
    r.add_post("/api/signin", api_signin)
    r.add_post("/api/signout", api_signout)
    r.add_post("/api/signout-everywhere", api_signout_everywhere)
    r.add_post("/api/reconfirm", api_reconfirm)
    r.add_get(r"/api/setup/{token:[A-Za-z0-9_-]{20,64}}", api_setup_info)
    r.add_post(r"/api/setup/{token:[A-Za-z0-9_-]{20,64}}/pin", api_setup_pin)
    r.add_post(r"/api/setup/{token:[A-Za-z0-9_-]{20,64}}/finish", api_setup_finish)
    r.add_get("/api/me", api_me)
    r.add_get("/api/home", api_home)
    r.add_post("/api/inbox/read", api_inbox_read)
    r.add_get("/api/account", api_account)
    r.add_post("/api/account/authenticator", api_totp_start)
    r.add_post("/api/account/authenticator/finish", api_totp_finish)
    r.add_delete("/api/account/authenticator", api_totp_off)
    r.add_post("/api/account/recovery-codes", api_recovery_codes)
    r.add_delete(r"/api/account/sessions/{sid:\d{1,12}}", api_end_device)
    r.add_get("/api/people", api_people)
    pid = r"/api/people/{pid:[A-Za-z0-9_-]{1,40}}"
    r.add_get(pid, api_person)
    r.add_post(pid + "/setup-code", api_setup_code)
    r.add_post(pid + "/reset-signin", api_reset_signin)
    r.add_post(pid + "/unlock", api_unlock)
    r.add_post(pid + "/end-access", api_end_access)
    r.add_post(pid + "/allow-access", api_allow_access)
    r.add_put(pid + "/signin-name", api_signin_name)
    r.add_put(pid + "/start-date", api_start_date)
    r.add_put(pid + "/departments", api_manager_groups)
    r.add_get("/api/settings", api_settings)
    r.add_put("/api/settings/general", api_settings_general)
    r.add_put("/api/settings/role", api_settings_role)
    r.add_post("/api/settings/key", api_settings_key)
    r.add_get("/api/audit", api_audit)
    r.add_post("/api/audit/check", api_audit_check)
    for adder in ROUTE_ADDERS:
        adder(app)
    access.check_routes(app)
    return app
