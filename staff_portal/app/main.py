"""Staff Portal: a locked-down, branded staff web app in front of Home Assistant.

Three web servers run in this process:
  * Admin (port 8099) is only reachable through Home Assistant ingress, so only HA admins see it.
  * Staff (port 8787) is the phone app; expose it through your Cloudflare Tunnel.
  * HR (port 8788) is the HR portal (hr/), at its own address through the tunnel. It's off until
    the owner turns it on in the dashboard; until then it only says so.
Staff phones never receive Home Assistant credentials. Every request is checked against the
phone's role before it is forwarded.

Front-desk computers can also make TT Hotel key cards (Wristbands). The add-on, not the
browser, talks to TT Hotel, so the TT Hotel account is never reachable from a phone or browser.
It reaches the card encoder on the desk either over the network, or (This computer (USB))
through the staff app open on the computer the encoder is plugged into.
"""

import asyncio
import collections
import dataclasses
import functools
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import segno
from aiohttp import web

from branding import Branding
from db import (KEY_CARD_FILTERS, PRIVATE_SETTINGS, STAFF_ON, WB_ACTIVE, Database, built_in_group,
                check_pin, department_list, hash_pin, page_choice, pin_length, staff_off)
from layout import (apply_auto_order, apply_phone_order, clean_layout, clean_phone_order,
                    clean_phone_pages, expand_page_choice, keep_removed_groups, layout_overrides,
                    visible_layout)
from ha import HomeAssistant, HomeAssistantError, LinkError
import ctdata
import ctlink
import ctphotos
import ctsync
import migrate
import reports as reports_kit
import transfer
import connecteam as connecteam_module
from connecteam import USERS_TTL, Connecteam, ConnecteamError
from permissions import (ACTIONS, ENTITY_ID_RE, ActionRejected, build_service_call,
                         controllable, describe_action, domain_of, offered)
import doorlog
import tasks
import wristbands
from wristbands import PlanError, clean_permissions, no_access, permission_choice
from tthotel import TTHotel, TTHotelError
from encoder import (DASHBOARD_PREFIX, NO_LINK, NO_REPLY, UNREACHABLE, EncoderError, Encoders,
                     dashboard_user, local_address, local_device)
from discovery import (SN_WEB_SOCKET_PORT, Discovery, DiscoveryError, describe, ha_networks,
                       parse_networks)
from hr import access as hr_access, audit as hr_audit, crypto as hr_crypto, signin as hr_signin
from hr import gdrive, server as hr_server
from hr import parts as hr_parts  # noqa: F401 - every part of the HR portal, before hr_service
from hr.gdrive import BridgeError
from hr.service import HRService

DATA_DIR = os.environ.get("DATA_DIR", "/data")
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8099"))
STAFF_PORT = int(os.environ.get("STAFF_PORT", "8787"))
HR_PORT = int(os.environ.get("HR_PORT", "8788"))
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
    rules = db.department_clock_ins()
    return any(db.clock_in_rule(dict(r), rules)[0] for r in db.conn.execute(
        "SELECT require_clock_in, clock_in_set, department_ids FROM staff "
        f"WHERE connecteam_user_id IS NOT NULL AND {STAFF_ON}"))


def load_connecteam_usage() -> dict:
    try:
        usage = json.loads(db.settings().get("connecteam_usage") or "{}")
    except ValueError:
        return {}
    return usage if isinstance(usage, dict) and isinstance(usage.get("count"), int) else {}


def load_hr_connecteam_usage() -> dict:
    try:
        usage = json.loads(db.settings().get("hr_connecteam_usage") or "{}")
    except ValueError:
        return {}
    return usage if isinstance(usage, dict) and isinstance(usage.get("count"), int) else {}


def load_reports_connecteam_usage() -> dict:
    try:
        usage = json.loads(db.settings().get("reports_connecteam_usage") or "{}")
    except ValueError:
        return {}
    return usage if isinstance(usage, dict) and isinstance(usage.get("count"), int) else {}


USER_AGENT = f"HomeAssistant-StaffPortal/{VERSION} (+https://github.com/liamdayan/ha-staff-portal)"
connecteam = Connecteam(
    connecteam_settings, connecteam_wanted,
    (load_connecteam_usage, lambda u: db.update_settings({"connecteam_usage": json.dumps(u)})),
    USER_AGENT,
    hr_usage_store=(load_hr_connecteam_usage,
                    lambda u: db.update_settings({"hr_connecteam_usage": json.dumps(u)})),
    reports_usage_store=(load_reports_connecteam_usage,
                         lambda u: db.update_settings({"reports_connecteam_usage": json.dumps(u)})))
# Reports and tasks read Connecteam through ctdata, in their own lane of the same budget; the
# task sync (ctsync) reads and writes through that lane too.
ctdata.configure(connecteam, lambda: report_zone())
ctsync.configure(lambda: db, connecteam, lambda: report_zone())


def tthotel_settings() -> tuple[bool, str, str, str, str, str, int]:
    s = db.settings()
    return (s.get("wristbands_enabled") == "1", s.get("tthotel_region", "global"),
            s.get("tthotel_client_id", ""), s.get("tthotel_client_secret", ""),
            s.get("tthotel_username", ""), s.get("tthotel_password_md5", ""),
            db.setting_int("tthotel_sync_hours"))


DOORS_IN_USE_DAYS = 90   # a key card made from the dashboard this recently, or door activity
                         # (TT Hotel's lock records), means the door list is still in use


def tthotel_wanted() -> bool:
    """Whether anything needs TT Hotel's door list (so TT Hotel isn't asked for nothing):
    Wristbands is on, and some computer has a card encoder (typed in, This computer (USB) or
    Automatic), or the dashboard's own encoder (This computer, in its card dialogs) is linked or
    made a key card lately, or TT Hotel sends door activity (whose records are only kept for
    doors on the list)."""
    if db.settings().get("wristbands_enabled") != "1":
        return False
    if db.conn.execute(
            "SELECT 1 FROM devices WHERE (encoder_url IS NOT NULL OR encoder_local = 1 "
            "OR encoder_auto = 1) AND status != 'revoked' LIMIT 1").fetchone() is not None:
        return True
    if dashboard_links or door_delivery["at"] is not None:
        return True
    since = time.time() - DOORS_IN_USE_DAYS * 86400
    last_record = db.last_lock_record()
    if last_record is not None and last_record > since:
        return True
    # A key card made on a dashboard's own encoder, still valid or ended lately (on the index
    # for key cards, by end date).
    return db.conn.execute(
        "SELECT 1 FROM wb_jobs WHERE card_type = 'staff' AND kind = 'write' AND expires_at > ? "
        "AND encoder_url LIKE ? ESCAPE '\\' LIMIT 1",
        (since, DASHBOARD_PREFIX.replace("_", "\\_") + "%")).fetchone() is not None


def load_setting_object(key: str) -> dict:
    """A JSON object kept in settings (TT Hotel's request count or sign-in); {} if damaged."""
    try:
        value = json.loads(db.settings().get(key) or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def load_tthotel_usage() -> dict:
    usage = load_setting_object("tthotel_usage")
    return usage if isinstance(usage.get("count"), int) else {}


tthotel = TTHotel(
    tthotel_settings, tthotel_wanted,
    (load_tthotel_usage, lambda u: db.update_settings({"tthotel_usage": json.dumps(u)})),
    (lambda: load_setting_object("tthotel_token"),
     lambda t: db.update_settings({"tthotel_token": json.dumps(t)})),
    USER_AGENT, on_doors=lambda locks: db.sync_wb_doors(locks),
    last_sync=lambda: db.wb_last_sync())


# One runner per encoder address; each makes one job's cards at a time. The add-on reaches the
# encoder through TT Hotel's SN Web Socket helper on the desk computer: over the network, or
# (This computer (USB)) through the staff app open on that computer, whose runner is at
# "local:<device ID>".
LINGERING_CARD_SECONDS = 3600   # a card made this recently may still be on the encoder after a
                                # restart, so it isn't overwritten until it's been taken off


def computer_name(device_id: int) -> str | None:
    device = db.device(device_id)
    return device["name"] if device else None


encoders = Encoders(tthotel.hotel_info, lambda *update: save_job_progress(*update),
                    hotel_info_expired=tthotel.hotel_info_expired,
                    last_card=lambda url: db.wb_last_card(url, time.time() - LINGERING_CARD_SECONDS),
                    local_name=computer_name)

# A computer whose card encoder is set to Automatic uses the SN Web Socket helper found on the
# network (discovery.py) instead of a typed address: the staff app comes through the tunnel, so
# the desk computer's address can't be learned from its requests.
NETWORK_UNKNOWN = ("Couldn't read Home Assistant's network setting. Try again, or enter the "
                   "network to search in Wristbands settings.")


async def encoder_networks() -> list:
    """The networks to look for card encoders on: the ones typed in Wristbands settings, else
    the ones in Home Assistant's network setting (Settings > System > Network)."""
    typed = db.settings().get("wb_scan_subnets", "")
    if typed.strip():
        try:
            return parse_networks(typed)
        except ValueError as err:
            raise DiscoveryError(str(err)) from None
    try:
        answer = await ha.ws_command({"type": "network"})
    except HomeAssistantError as err:
        log.info("Couldn't read Home Assistant's network setting: %s", err)
        raise DiscoveryError(NETWORK_UNKNOWN) from None
    return ha_networks(answer)


def encoder_auto_wanted() -> bool:
    """Whether encoders may be looked for without an admin asking: Wristbands is on and some
    computer's card encoder is set to Automatic."""
    return db.settings().get("wristbands_enabled") == "1" and db.conn.execute(
        "SELECT 1 FROM devices WHERE encoder_auto = 1 AND encoder_url IS NULL "
        "AND encoder_local = 0 AND status != 'revoked' LIMIT 1").fetchone() is not None


discovery = Discovery(lambda: encoder_networks(), lambda: encoder_auto_wanted())


def needs_clock_in(member: dict | None, rules: dict | None = None) -> bool:
    """Whether this person may only sign in while clocked in (and Connecteam is turned on): their
    own rule if set by hand, else their departments' (the first that sets one), else their own
    (db.clock_in_rule).
    rules: db.department_clock_ins(), when checking many people."""
    return bool(member and member.get("connecteam_user_id") and connecteam_settings()[0]
                and db.clock_in_rule(member, rules)[0])


def clock_block(member: dict | None, rules: dict | None = None) -> str | None:
    """None if this person may be signed in now, else 'clocked_out' or 'clock_unknown'."""
    if not needs_clock_in(member, rules):
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


async def read_upload(request: web.Request, limits: dict[str, int]) -> dict[str, bytes]:
    """The fields of a multipart upload named in `limits`, each read up to its limit in bytes
    (other fields are skipped). Raises ValueError for one that's too big or a body that isn't an
    upload, and HTTPRequestTimeout like read_json."""
    fields: dict[str, bytes] = {}
    try:
        async with asyncio.timeout(request.app.get(BODY_SECONDS, 10)):
            try:
                reader = await request.multipart()
            except (AssertionError, KeyError, ValueError):
                raise ValueError("Choose a file to upload.") from None   # not an upload
            while (part := await reader.next()) is not None:
                name = getattr(part, "name", None)
                if name not in limits or name in fields:
                    await part.release()
                    continue
                data = bytearray()
                while chunk := await part.read_chunk(64 * 1024):
                    data.extend(chunk)
                    if len(data) > limits[name]:
                        raise ValueError(f"That file is too big (more than "
                                         f"{limits[name] // 2**20} MB)." if limits[name] >= 2**20
                                         else f"The {name} is too long.")
                fields[name] = bytes(data)
    except TimeoutError:
        raise web.HTTPRequestTimeout(text="The upload took too long to arrive.") from None
    return fields


async def read_options(request: web.Request) -> dict:
    """A JSON object of options for a button's request, or {} when it sent no body."""
    return await read_json(request) if request.can_read_body else {}


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


def set_level(access: dict[str, str], entity_id: str, level: str) -> None:
    """Put one device at a level in an access map ('none' takes it away). Control is only
    kept for devices staff can operate."""
    if level == "none":
        access.pop(entity_id, None)
    elif offered(entity_id):
        access[entity_id] = "control" if level == "control" and controllable(entity_id) else "view"


def phone_access(device: dict, layout: AreaLayout, layout_data: dict | None = None) -> dict[str, str]:
    """What one phone may see and control: its role, adjusted by any access overrides on the
    layout items it shows. layout_data: the layout to use (the saved one unless previewing)."""
    access = role_access(device["role_id"], layout)
    data = db.layout() if layout_data is None else layout_data
    for eid, level in layout_overrides(data, layout, device_pages(device, data)).items():
        set_level(access, eid, level)
    return access


# ---------------------------------------------------------------- role-based devices
# On a role-based device each person who signs in gets their own access and pages: their
# role's, then the layout's own access settings on what they're shown, then the changes made
# for them personally (whole areas, then single devices), each more specific than the last.
# Kiosk devices ('phone' and 'staff' sign-in) use the device's own role and pages instead.

SIGNIN_MODES = ("phone", "staff", "role")
SHARED_MODES = ("staff", "role")   # each person signs in with their own PIN
OVERRIDE_RANK = {"none": 0, "view": 1, "control": 2}


def is_shared(device: dict) -> bool:
    return device["signin_mode"] in SHARED_MODES


def role_based(device: dict) -> bool:
    return device["signin_mode"] == "role"


def pin_timeout(device: dict) -> int:
    """Seconds without use before a device asks for a PIN again: phones and computers each have
    their own setting (a front desk computer is walked away from more often than a phone)."""
    key = ("pin_timeout_computer_minutes" if device.get("kind") == "computer"
           else "pin_timeout_minutes")
    return db.setting_int(key) * 60


def device_pin_length(device: dict) -> int | None:
    """How many digits every PIN that could sign in on this device has, when they all have the
    same known length, so the PIN pad can sign in as soon as the last digit is typed. Only a
    length shared by everyone allowed on the device is given out (never one person's own), and
    None whenever any of them is unknown or they differ. Never the PIN itself."""
    if not is_shared(device):
        return pin_length(device["pin_length"]) if device["pin_hash"] else None
    lengths = set(db.staff_pin_lengths(device))
    return lengths.pop() if len(lengths) == 1 else None   # {None} gives None too


def person_page_choice(member: dict) -> list[str] | None:
    """A person's pages on role-based devices: their own choice, else their role's (None:
    every page)."""
    own = page_choice(member.get("layout_pages"))
    if own is not None:
        return own
    role = db.role(member["role_id"]) if member.get("role_id") else None
    return role["layout_pages"] if role else None


def person_pages(member: dict, layout_data: dict | None = None) -> list[str] | None:
    return expand_page_choice(db.layout() if layout_data is None else layout_data,
                              person_page_choice(member))


def person_access(member: dict, layout: AreaLayout, layout_data: dict | None = None) -> dict[str, str]:
    """What one person may see and control on a role-based device. `member` may also be a
    stand-in with just a role_id (previewing a role), which has no changes of its own."""
    access = role_access(member.get("role_id"), layout)
    data = db.layout() if layout_data is None else layout_data
    for eid, level in layout_overrides(data, layout, person_pages(member, data)).items():
        set_level(access, eid, level)
    entities, areas = db.staff_overrides(member["id"]) if member.get("id") else ({}, {})
    if areas:
        for eid, area_ids in layout.membership.items():
            level = max((areas[a] for a in area_ids if a in areas), key=OVERRIDE_RANK.get, default=None)
            if level:
                set_level(access, eid, level)
    for eid, level in entities.items():
        set_level(access, eid, level)
    return access


def access_for(device: dict, member: dict | None, layout: AreaLayout,
               layout_data: dict | None = None) -> dict[str, str]:
    """What may be seen and controlled on this device right now: on a role-based device the
    signed-in person's own access (nothing until someone signs in), otherwise the device's."""
    if role_based(device):
        return person_access(member, layout, layout_data) if member else {}
    return phone_access(device, layout, layout_data)


def pages_for(device: dict, member: dict | None, layout_data: dict) -> list[str] | None:
    if role_based(device):
        return person_pages(member, layout_data) if member else []
    return device_pages(device, layout_data)


def role_label(device: dict, member: dict | None) -> str | None:
    """The role behind what's on screen, for the activity log and the app's header."""
    if not role_based(device):
        return device["role_name"]
    role = db.role(member["role_id"]) if member and member.get("role_id") else None
    return role["name"] if role else None


# ---------------------------------------------------------------- wristband permissions
# Which wristbands and cards someone may make, and for which doors, is worked out the same way
# as access, again on every request: on a role-based device the person's own permissions (else
# their role's), on a shared kiosk device the device's role's. Every card needs a named maker,
# so a device nobody signs in to with their own PIN never makes any.

def person_wristbands(member: dict) -> dict:
    """A person's wristband permissions on role-based devices: their own, else their role's."""
    own = permission_choice(member.get("wristbands"))
    if own is not None:
        return own
    role = db.role(member["role_id"]) if member.get("role_id") else None
    return (role and role["wristbands"]) or no_access()


def wristband_permissions(device: dict, member: dict | None) -> dict:
    """What the person signed in on this device may make now (wristbands.NO_ACCESS: nothing).
    Nothing unless Wristbands is turned on, the device has a card encoder, and someone signed
    in with their own PIN on a shared or role-based device."""
    if (not member or not is_shared(device) or not encoder_address(device)
            or db.settings().get("wristbands_enabled") != "1"):
        return no_access()
    if role_based(device):
        return person_wristbands(member)
    role = db.role(device["role_id"]) if device.get("role_id") else None
    return (role and role["wristbands"]) or no_access()


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

# This computer (USB): the staff app reaches SN Web Socket on the computer it's open on. Only the
# staff app's own pages may (the dashboard and its preview run on Home Assistant's address).
LOCAL_HELPERS = f"ws://127.0.0.1:{SN_WEB_SOCKET_PORT} ws://localhost:{SN_WEB_SOCKET_PORT}"


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
    connect = "'self'" if preview else f"'self' {LOCAL_HELPERS}"   # 'self' takes same-origin wss
    csp = (f"default-src 'self'; img-src 'self' data:; style-src 'self' 'nonce-{nonce}'; "
           f"script-src 'self' 'nonce-{nonce}'; connect-src {connect}; "
           f"frame-ancestors {framed_by}; base-uri 'none'; form-action 'none'")
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
    shared = is_shared(device)
    if not shared and not device["pin_hash"] and db.settings().get("require_pin") == "1":
        raise json_error(web.HTTPForbidden, "pin_not_set")
    if not need_unlock or not (shared or device["pin_hash"]):
        return device, None

    key = request.headers.get("X-Session", "")
    session = pin_sessions.get(key)
    timeout = pin_timeout(device)
    if not session or session[0] != device["id"] or time.time() - session[1] > timeout:
        pin_sessions.pop(key, None)
        raise json_error(web.HTTPForbidden, "pin_required")
    member = None
    if shared:
        member = db.staff(session[2]) if session[2] else None
        if (staff_off(member) or member["status"] != "active"
                or not db.staff_allowed_on(member["id"], device)):
            pin_sessions.pop(key, None)
            raise json_error(web.HTTPForbidden, "pin_required")
        blocked = clock_block(member) if control else None
        if blocked:
            # Clocked out (or the time clock can't be checked): this person's session ends.
            pin_sessions.pop(key, None)
            db.log(actor_name(device, member),
                   "Signed out: clocked out in Connecteam" if blocked == "clocked_out"
                   else "Signed out: Connecteam time clock unavailable",
                   role=role_label(device, member), ok=False)
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
    shared = is_shared(device)
    pin_only = shared and bool(device["pin_only"])
    rules = db.department_clock_ins()
    return web.json_response({
        "device": device["name"],
        "kind": device["kind"],
        # On a role-based device the role is the signed-in person's (sent with the readings).
        "role": None if role_based(device) else device["role_name"],
        "role_based": role_based(device),
        "shared": shared,
        # PIN-only devices never get the list of names.
        "pin_only": pin_only,
        "staff": [{"id": m["id"], "name": m["name"],
                   "off_clock": clock_block(m, rules) is not None}
                  for m in db.staff_for_device(device)] if shared and not pin_only else [],
        "pin_required": shared or bool(device["pin_hash"]),
        "property_name": s["property_name"],
        "app_name": s["app_name"],
        "accent_color": s["accent_color"],
        "has_logo": branding.has_logo(),
        # This device's own: phones and computers each have their own setting.
        "pin_timeout_minutes": pin_timeout(device) // 60,
        # So the PIN pad can sign in without OK: only when every PIN that could sign in here has
        # this many digits (None: it waits for a pause instead).
        "pin_length": device_pin_length(device),
        # ...and how long it waits for more digits when it doesn't know (Settings).
        "pin_pause_ms": max(PIN_PAUSE_MIN_MS, min(PIN_PAUSE_MAX_MS, db.setting_int("pin_pause_ms"))),
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
        if "length" in data and data["length"] != device_pin_length(device):
            # The PIN pad signed in by itself at a length that's out of date (someone with a
            # longer PIN was added since it loaded, say). Nothing is checked or counted: the pad
            # takes the new length and lets them finish typing.
            return web.json_response({"error": "pin_length_changed",
                                      "pin_length": device_pin_length(device)}, status=409)
        if not is_shared(device):
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
        db.log(device["name"], "Phone locked after wrong PINs", role=role_label(device, None),
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
               role=role_label(device, None), ok=False, ip=ip)
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
    if staff_off(member) or not db.staff_allowed_on(member["id"], device):
        return bad_request("Pick your name again.", 404)
    locked_msg = (f"{member['name']} is locked for a while after too many wrong PINs. "
                  "Try again later, or ask your manager to unlock you.")
    if member["status"] == "locked" or (member["locked_until"] or 0) > time.time():
        return bad_request(locked_msg, 403, staff_locked=True)
    ok = await pin_matches(str(data.get("pin", "")), member["pin_hash"])
    # Fresh: an attempt on another device may have changed this person's count meanwhile.
    member = db.staff(member["id"])
    if staff_off(member):
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
               role=role_label(device, member), ok=False, ip=ip)
        return bad_request(locked_msg, 403, staff_locked=True)
    db.update_staff(member["id"], failed_pins=attempts)
    return bad_request(f"Wrong PIN. {max_attempts - attempts} tries left.", 403)


def staff_signed_in(device: dict, member: dict, ip: str) -> web.Response:
    """The right PIN for a staff member on a shared device: sign them in, unless they must be
    clocked in and aren't. Only this person's own wrong-PIN count is cleared. The device's
    count isn't, so signing in as yourself can't be used to keep guessing other people's PINs."""
    if staff_off(member):   # turned off or archived while their PIN was being checked
        return bad_request("Pick your name again.", 404)
    blocked = clock_block(member)
    if blocked:
        db.update_staff(member["id"], failed_pins=0)
        db.log(actor_name(device, member), "Blocked sign-in: not clocked in"
               if blocked == "clocked_out" else "Blocked sign-in: time clock unavailable",
               role=role_label(device, member), ok=False, ip=ip)
        return bad_request(CLOCK_MESSAGES[blocked].format(name=member["name"]), 403,
                           clock=blocked)
    db.update_staff(member["id"], failed_pins=0, locked_until=None, last_seen=time.time())
    db.log(actor_name(device, member), "Signed in", role=role_label(device, member), ip=ip)
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
               role=role_label(device, None), ok=False, ip=ip)
        return bad_request("That PIN belongs to more than one person, so it can't be used here. "
                           "Ask your manager to give each person their own PIN.", 403)
    member = db.staff(matches[0]) if matches else None
    if not staff_off(member):
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
    PIN-only devices the PIN alone says who someone is. PINs given meanwhile (the Connecteam
    sync) are checked too, so the caller can save it straight after."""
    checked: set[tuple[int, str]] = set()
    for _ in range(STAFF_SYNC_RECHECKS):
        others = {(sid, stored) for sid, stored in db.staff_pins() if sid != except_id} - checked
        if not others:
            return False
        if await asyncio.to_thread(lambda: any(check_pin(pin, stored) for _, stored in others)):
            return True
        checked |= others
    return True   # PINs kept changing: safer to ask for another one


PIN_TAKEN = "Someone else already uses that PIN. Give each person their own PIN."


def new_session(device: dict, member: dict | None) -> str:
    session = secrets.token_urlsafe(24)
    now = time.time()
    if len(pin_sessions) >= MAX_SESSIONS:
        # Forget sessions that have timed out; if there are still too many, the oldest go.
        timeout = max(db.setting_int("pin_timeout_minutes"),
                      db.setting_int("pin_timeout_computer_minutes")) * 60
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
    recheck_links(device["id"])   # this session's encoder link closes with it
    if member:
        db.log(actor_name(device, member), "Signed out", role=role_label(device, member))
    return web.json_response({"ok": True})


async def api_entities(request: web.Request):
    device, member = authenticate(request)
    layout_data = db.layout()
    try:
        layout = await area_layout()
        allowed = access_for(device, member, layout, layout_data)
        states = await ha.states() if allowed else {}
    except HomeAssistantError as err:
        log.warning("Home Assistant unavailable: %s", err)
        return bad_request("Can't reach the system right now.", 503)
    entities = [entity_view(eid, states.get(eid), layout, lvl) for eid, lvl in allowed.items()]
    entities.sort(key=lambda e: (e["group_rank"], e["area"].lower(), e["name"].lower()))
    can_reorder = db.settings().get("allow_phone_reorder") == "1"
    order = db.phone_order(order_owner(device, member)) if can_reorder else None
    offer_wristbands = bool(wristband_permissions(device, member)["types"])
    phone_layout = visible_layout(layout_data, allowed, layout, pages_for(device, member, layout_data))
    if phone_layout:
        phone_layout = apply_phone_order(phone_layout, order)
    else:
        entities = apply_auto_order(entities, order)
    offer_reports = bool(visible_reports(member))
    return web.json_response({
        "entities": entities,
        "layout": phone_layout,
        "staff_name": member and member["name"],
        "role": role_label(device, member),
        "can_reorder": can_reorder,
        "control_lock": db.settings().get("control_lock") == "1",
        "control_lock_minutes": db.setting_int("control_lock_minutes"),
        "custom_order": bool(order),
        "order": order or {"pages": [], "items": {}},
        # Whether to offer Wristbands (the page itself checks again on every request), and
        # whether this computer's encoder is This computer (USB), reached through this app.
        "wristbands": offer_wristbands,
        "encoder_local": offer_wristbands and encoder_mode(device) == "local",
        # Whether to offer Reports: someone signed in may see at least one (each report checks
        # again on every request).
        "reports": offer_reports,
        # Tasks: for anyone signed in with their own PIN (each request checks again).
        "tasks": bool(member),
        # Their department's start page, for the app to open at sign-in (unless something was
        # already chosen in this sign-in).
        "start": start_for(member, phone_layout, {"wristbands": offer_wristbands,
                                                  "reports": offer_reports, "tasks": bool(member)}),
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
    db.log(actor_name(device, member), "Rearranged the phone", role=role_label(device, member))
    return web.json_response({"ok": True})


async def api_reset_order(request: web.Request):
    device, member = authenticate(request, active=True)
    db.set_phone_order(order_owner(device, member), None)
    db.log(actor_name(device, member), "Reset the phone's arrangement", role=role_label(device, member))
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
    actor, role = actor_name(device, member), role_label(device, member)
    if phone_too_busy(device["id"]):
        return bad_request("Too many changes at once. Wait a moment and try again.", 429)
    if not ENTITY_ID_RE.match(entity_id) or action not in ACTIONS.get(domain_of(entity_id), {}):
        # Never store junk a phone sends; just note that something invalid was refused.
        db.log(actor, "Blocked: invalid request", role=role, ok=False, ip=ip)
        return bad_request("You don't have access to control that.", 403)
    try:
        allowed = access_for(device, member, await area_layout())
    except HomeAssistantError as err:
        log.warning("Home Assistant unavailable: %s", err)
        return bad_request("Can't reach the system right now.", 503)
    if allowed.get(entity_id) != "control" or not isinstance(params, dict):
        db.log(actor, f"Blocked: {action}", role=role,
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
        db.log(actor, action, role=role, entity_id=entity_id,
               detail=str(err)[:200], ok=False, ip=ip)
        return bad_request("The system didn't accept that. Try again.", 502)
    detail = ", ".join(f"{k}={v}" for k, v in call.items() if k != "entity_id") or None
    db.log(actor, action, role=role, entity_id=entity_id, detail=detail, ip=ip)
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
    if member and staff_off(db.staff(member["id"])):
        # Turned off or archived while this was on its way: nothing is sent, as them or not.
        raise ActionRejected("You're signed out. Sign in again.")
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
                       role=role_label(device, member), detail=str(err), ok=False)
    await ha.call_service(domain, service, call)
    return False


# ---------------------------------------------------------------- wristbands (staff app)
# A front-desk computer with a card encoder makes TT Hotel key cards: day passes, overnight
# wristbands, contractor badges and staff cards. wristbands.py turns a request into a plan
# (checking it against the person's permissions), it's recorded as a card job, and the encoder's
# runner (encoder.py) writes the cards through TT Hotel's SN Web Socket helper on the desk
# computer. Screens that poll (a job's progress, a look-up) only read the database and the
# runner's own state: they never cost a TT Hotel request or wait for the encoder.

WRISTBAND_REQUESTS_PER_MINUTE = 60   # previews while typing, new jobs, their buttons, look-ups
WRISTBAND_LOG_PAGE = 50
HA_ZONE_SECONDS = 6 * 3600           # Home Assistant's time zone is read again after this
HA_ZONE_RETRY = 60                   # after a failed read, how long before asking again
NO_WRISTBANDS = "You don't have access to make wristbands on this computer."
NO_TIME_ZONE = ("Can't tell the property's time zone right now, so wristbands can't be made. "
                "Try again in a minute, or ask your manager to set it in Wristbands settings.")
ENCODER_BUSY = ("The card encoder is busy with another job. Wait for it to finish, then try "
                "again.")
recent_wristband_requests: dict[int, list[float]] = {}
ha_zone = {"name": None, "at": float("-inf"), "tried": float("-inf")}


def wristbands_too_busy(device_id: int) -> bool:
    now = time.time()
    times = [t for t in recent_wristband_requests.get(device_id, []) if now - t < 60]
    busy = len(times) >= WRISTBAND_REQUESTS_PER_MINUTE
    if not busy:
        times.append(now)
    recent_wristband_requests[device_id] = times
    return busy


async def ha_time_zone() -> str | None:
    """Home Assistant's own time zone (Settings > System > General). Read now and then; while
    Home Assistant can't be reached, the last one read is used."""
    now = time.monotonic()
    if ha_zone["name"] and now - ha_zone["at"] < HA_ZONE_SECONDS:
        return ha_zone["name"]
    if now - ha_zone["tried"] < HA_ZONE_RETRY:
        return ha_zone["name"]
    ha_zone["tried"] = now
    try:
        name = (await ha.config()).get("time_zone")
        ZoneInfo(str(name))
    except (HomeAssistantError, ValueError, ZoneInfoNotFoundError) as err:
        log.warning("Couldn't read Home Assistant's time zone: %s", err)
        return ha_zone["name"]
    ha_zone.update(name=name, at=now)
    return name


async def wristband_zone() -> ZoneInfo | None:
    """The property's time zone for when cards stop working: the one chosen in Wristbands
    settings, else Home Assistant's. None if neither is known: a card must never get a guessed
    time."""
    name = db.settings().get("wb_time_zone") or await ha_time_zone()
    try:
        return ZoneInfo(name) if name else None
    except (ValueError, ZoneInfoNotFoundError):
        return None


async def wristband_caller(request: web.Request, change: bool = False,
                           limit: bool = True) -> tuple[dict, dict, dict]:
    """(device, signed-in person, their wristband permissions) for a Wristbands request, or
    raise the error the app should show. `change`: making or stopping cards, which counts as
    using a control (a clocked-in-only person must be on the clock). `limit`: counted towards the
    device's requests per minute (polls aren't)."""
    if change:
        device, member = authenticate(request, active=True)
        if member and clock_block(member):
            await recheck_clock(member["id"])
        device, member = authenticate(request, active=True, control=True)
    else:
        device, member = authenticate(request)
    perms = wristband_permissions(device, member)
    address = encoder_address(device, rescan=False)
    if not perms["types"] or not address:
        if change:
            db.log(actor_name(device, member), "Blocked: wristbands", role=role_label(device, member),
                   ok=False, ip=client_ip(request))
        raise json_error(web.HTTPForbidden, NO_WRISTBANDS)
    if limit and wristbands_too_busy(device["id"]):
        raise json_error(web.HTTPTooManyRequests,
                         "Too many requests at once. Wait a moment and try again.")
    # From here on the device's encoder_url is the encoder it uses now, typed in, found, or
    # "local:<ID>" for This computer (USB) (local_device()), so encoder_mode() no longer applies.
    return {**device, "encoder_url": address}, member, perms


def door_name(door: dict) -> str:
    return door["name"] or door["alias"] or door["tt_name"] or f"Door {door['lock_id']}"


def wristband_doors() -> dict[int, dict]:
    """Every door TT Hotel has listed, by lock ID, as wristbands.py takes them (it leaves out
    hidden and no-longer-listed ones where it should)."""
    return {d["lock_id"]: d for d in db.wb_doors()}


def card_noun(card_type: str, count: int = 1) -> str:
    noun = {"day_pass": "day pass", "overnight": "overnight wristband",
            "contractor": "contractor badge"}.get(card_type, "staff card")
    if count == 1:
        return f"an {noun}" if noun[0] in "aeiou" else f"a {noun}"
    return f"{count} {noun}es" if noun.endswith("s") else f"{count} {noun}s"


def made_for(card_type: str, name: str, room_ids) -> str:
    """Who or what cards were made for, as the activity log says it: the room for overnight
    cards, otherwise the name typed."""
    if card_type == "overnight" and isinstance(room_ids, list):
        doors = wristband_doors()
        rooms = [door_name(doors[i]) for i in room_ids[:10] if isinstance(i, int) and i in doors]
        if rooms:
            return ", ".join(rooms)
    return name


def finished_message(job: dict) -> str:
    """What a finished job says, if the encoder runner left no message of its own."""
    noun = {"staff": "card", "contractor": "badge"}.get(job["card_type"], "wristband")
    total = len(job["cards"])
    written = sum(1 for c in job["cards"] if c["status"] in ("written", "cleared"))
    if job.get("kind") == "revoke":
        return (f"Card {job['target_card']} is revoked and cleared" if written
                else f"Card {job['target_card']} wasn't revoked")
    if job.get("kind") == "notice":
        return ("The lost card notice is ready. Tap it on each door the lost card opens"
                if written else "No lost card notice was made")
    if job["status"] == "cancelled":
        return (f"Stopped before any {noun}s were made" if not written
                else f"Stopped after making {written} of {total} {noun}s")
    if written == total:
        return f"The {noun} is ready" if total == 1 else f"All {total} {noun}s are ready"
    return f"{written} of {total} {noun}s made"


def job_view(job: dict, zone: ZoneInfo | None) -> dict:
    """A card job as the app shows it. While it runs, its progress comes from the encoder's
    runner (in memory); afterwards from the record."""
    live = job_progress(job) if job["status"] in WB_ACTIVE else None
    if live:
        phase, current, message = live["phase"], live.get("current"), live.get("message") or ""
    elif job["status"] == "needs_attention":
        failed = next((c for c in job["cards"] if c["status"] == "failed"), None)
        phase, current = "needs_attention", failed and failed["seq"]
        message = (failed and failed["error"]) or job["message"] or "That didn't work. Try again."
    elif job["status"] in ("done", "cancelled"):
        phase, current, message = job["status"], None, job["message"] or finished_message(job)
    else:
        phase, current, message = "queued", None, "Waiting for the encoder…"
    used = db.cards_last_used([c.get("id") for c in job["cards"] if c["status"] == "written"])
    return {"id": job["id"], "type": job["card_type"], "name": job["name"], "count": job["count"],
            "summary": job["summary"], "expires_at": job["expires_at"],
            "expires_label": (wristbands.format_expiry(job["expires_at"], time.time(), zone)
                              if zone else ""),
            "status": job["status"], "phase": phase, "current": current, "message": message,
            "made_by": job["actor"], "created_at": job["ts"],
            # 'write', 'revoke' or 'notice'; the card revoked, noticed or replaced.
            "kind": job.get("kind") or "write", "target_card": job.get("target_card"),
            "holder_staff_id": job.get("holder_staff_id"), "source": job.get("source"),
            # A staff card's id and state (active, revoked or lost), for the card log.
            "cards": [{"id": c.get("id"), "seq": c["seq"], "status": c["status"],
                       "card_number": c["card_number"], "error": c["error"],
                       "state": c.get("state", "active"), "state_by": c.get("state_by"),
                       "cancelled_card": c.get("cancelled_card"),
                       # When and where it was last used (door activity), or None.
                       "last_used": last_used_view(used.get(c.get("id")), zone)}
                      for c in job["cards"]]}


def visible_job(request: web.Request, member: dict, perms: dict) -> dict:
    """A card job this person may see: their own, or anyone's if they may see every card.
    Anyone else's is "not found", so job numbers can't be probed."""
    try:
        job = db.wb_job(int(request.match_info["id"]))
    except (ValueError, OverflowError):
        job = None
    if not job or (job["staff_id"] != member["id"] and not perms["see_all_cards"]):
        raise json_error(web.HTTPNotFound, "That card job wasn't found.")
    return job


async def wristband_plan(request: web.Request, device: dict, member: dict, perms: dict):
    """(plan, request body, time zone) for the card request in this request's body, or raise
    the error to show. Refusals (as opposed to typing mistakes) are logged."""
    data = await read_json(request)
    zone = await wristband_zone()
    if zone is None:
        raise json_error(web.HTTPServiceUnavailable, NO_TIME_ZONE)
    try:
        plan = wristbands.plan(data, perms, wristband_doors(), db.settings(), time.time(), zone)
    except PlanError as err:
        if err.status == 403:
            db.log(actor_name(device, member), "Blocked: wristbands", role=role_label(device, member),
                   detail=err.message, ok=False, ip=client_ip(request))
        raise json_error(web.HTTPForbidden if err.status == 403 else web.HTTPBadRequest,
                         err.message, field=err.field) from None
    holder = staff_named(plan.name) if plan.type == "staff" else None
    if holder and holder["status"] == "archived":
        message = ARCHIVED_HOLDER.format(name=holder["name"])
        if request.path == "/api/wristbands/jobs":   # making it, not the preview as it's typed
            db.log(actor_name(device, member), "Blocked: wristbands", role=role_label(device, member),
                   detail=message, ok=False, ip=client_ip(request))
        raise json_error(web.HTTPForbidden, message, field="name")
    return plan, data, zone


ARCHIVED_HOLDER = "{name} is archived, so no key card can be made for them."


def archived_holder_refusal(job: dict) -> str | None:
    """Why a card job can't be tried again or go on to its next card: it writes a key card for
    someone archived since it started (archiving stops those, so this is one that slipped
    through). Stopping it is always allowed. None if it may go on."""
    if (job.get("kind") or "write") != "write" or not job.get("holder_staff_id"):
        return None
    holder = db.staff(job["holder_staff_id"])
    if holder and holder["status"] == "archived":
        return f"{ARCHIVED_HOLDER.format(name=holder['name'])} Stop this job."
    return None


async def api_wristbands(request: web.Request):
    """What this person may make here, with the defaults to start from."""
    device, member, perms = await wristband_caller(request, limit=False)
    status = encoder_status(device["encoder_url"])
    # This computer (USB): the app opens this computer's encoder link (POST /api/encoder/ticket,
    # then GET /api/encoder/link) while Wristbands is open.
    local = local_device(device["encoder_url"]) is not None
    zone = await wristband_zone()
    if zone is None:
        return web.json_response({"enabled": False, "reason": NO_TIME_ZONE, "encoder": status,
                                  "encoder_local": local, "types": {}, "doors": [],
                                  "see_all_cards": perms["see_all_cards"]})
    options = wristbands.options(perms, wristband_doors(), db.settings(), time.time(), zone)
    return web.json_response({**options, "encoder": status, "encoder_local": local})


async def api_preview_wristbands(request: web.Request):
    """The summary and end time of a card request, as it's being filled in."""
    device, member, perms = await wristband_caller(request)
    plan, _, _ = await wristband_plan(request, device, member, perms)
    return web.json_response({"plan": plan.public()})


async def api_make_wristbands(request: web.Request):
    device, member, perms = await wristband_caller(request, change=True)
    plan, data, zone = await wristband_plan(request, device, member, perms)
    actor, role, ip = actor_name(device, member), role_label(device, member), client_ip(request)
    url = device["encoder_url"]
    # Nothing is awaited from here until the job is handed over, so two requests can't both
    # find the encoder free, and nobody archived or turned off meanwhile (archiving stops their
    # jobs) starts a new one.
    if staff_off(db.staff(member["id"])):   # as authenticate() answers it
        pin_sessions.pop(request.headers.get("X-Session", ""), None)
        raise json_error(web.HTTPForbidden, "pin_required")
    if encoder_busy(url):
        return bad_request(ENCODER_BUSY, 409)
    holder = staff_named(plan.name) if plan.type == "staff" else None
    job_id = db.add_wb_job(
        card_type=plan.type, name=plan.name, count=plan.count,
        records=[r.to_dict() for r in plan.records], summary=plan.summary,
        expires_at=plan.expires_at, allow_lockout=plan.allow_lockout, staff_id=member["id"],
        device_id=device["id"], actor=actor, role=role, ip=ip, encoder_url=url,
        holder_staff_id=holder and holder["id"],
        department_id=holder and (db.giving_department(holder, "card_access") or {}).get("id"),
        source="staff_app")
    try:
        start_job(db.wb_job(job_id), device)
    except ValueError as err:   # the runner can't write what was planned: a bug, never staff's
        log.warning("Card job %s couldn't be started: %s", job_id, err)
        db.cancel_wb_job(job_id, STOPPED_UNEXPECTEDLY)
        return bad_request("That card couldn't be started. Try again, or ask your manager to "
                           "check the doors.", 500)
    action = (f"Made {card_noun(plan.type, plan.count)} for "
              f"{made_for(plan.type, plan.name, data.get('room_ids'))}")
    db.log(actor, action, role=role, detail=f"{plan.summary} · until {plan.expires_label}", ip=ip)
    if db.settings().get("ha_logbook") == "1":
        background(ha.logbook("Wristbands", f"{action} by {member['name']} ({device['name']}) "
                                            "via Staff Portal"), "write logbook entry")
    return web.json_response({"job": job_view(db.wb_job(job_id), zone)})


def staff_named(name: str) -> dict | None:
    """The person a staff card was made for, when the name typed is exactly one person's
    (ignoring capitals and extra spaces)."""
    wanted = " ".join(str(name or "").split()).lower()
    found = [dict(r) for r in db.conn.execute(
        "SELECT id, name, department_ids, status FROM staff WHERE name = ? COLLATE NOCASE",
        (wanted,))]
    return found[0] if len(found) == 1 else None


async def api_revoke_card(request: web.Request):
    """Revoke a staff card (card in hand) on this computer's encoder: only for people who may
    see every card. Reporting a card lost is done in the dashboard."""
    device, member, perms = await wristband_caller(request, change=True)
    try:
        card = db.key_card(int(request.match_info["id"]))
    except (ValueError, OverflowError):
        card = None
    if not perms["see_all_cards"]:
        db.log(actor_name(device, member), "Blocked: revoke a key card",
               role=role_label(device, member), ok=False, ip=client_ip(request))
        raise json_error(web.HTTPForbidden, "You don't have access to revoke cards. Ask a "
                                            "manager.")
    if not card:
        raise json_error(web.HTTPNotFound, "That card wasn't found.")
    if card["state"] != "active":
        return bad_request(f"This card is already {card['state']}.", 409)
    if encoder_busy(device["encoder_url"]):
        return bad_request(ENCODER_BUSY, 409)
    actor, role, ip = actor_name(device, member), role_label(device, member), client_ip(request)
    job_id = start_revoke_job(card, device, actor=actor, staff_id=member["id"], role=role, ip=ip,
                              source="staff_app")
    db.log(actor, f"Started revoking key {card_label(card)}", role=role, ip=ip)
    return web.json_response({"job": job_view(db.wb_job(job_id), await wristband_zone())})


async def api_wristband_job(request: web.Request):
    device, member, perms = await wristband_caller(request, limit=False)
    return web.json_response({"job": job_view(visible_job(request, member, perms),
                                              await wristband_zone())})


async def api_wristband_job_action(request: web.Request):
    """Try again, skip this card, or stop: what to do after a card couldn't be made."""
    action = request.match_info["action"]
    if action == "cancel":
        # Anyone signed in may stop their own job, even after clocking out or losing wristband
        # access: otherwise it would go on writing cards for whoever puts one on the encoder.
        device, member = authenticate(request, active=True)
        if not member:
            raise json_error(web.HTTPForbidden, NO_WRISTBANDS)
        perms = wristband_permissions(device, member)
        if wristbands_too_busy(device["id"]):
            raise json_error(web.HTTPTooManyRequests,
                             "Too many requests at once. Wait a moment and try again.")
    else:
        device, member, perms = await wristband_caller(request, change=True)
    job = visible_job(request, member, perms)
    if action != "cancel" and job["status"] != "needs_attention":
        return bad_request("This job isn't waiting for a decision any more.", 409)
    if action != "cancel" and dashboard_user(job["encoder_url"]) is not None:
        # Made on the encoder of a computer with the dashboard open: only that page goes on.
        return bad_request("This card is being made from the dashboard: try it again there.", 409)
    refusal = archived_holder_refusal(job) if action != "cancel" else None
    if refusal:
        return bad_request(refusal, 409)
    if job["status"] in WB_ACTIVE:
        decide_job(job, action)   # a job that ends short is logged when it ends
    return web.json_response({"job": job_view(db.wb_job(job["id"]), await wristband_zone())})


def parse_before(value) -> float | None:
    """A ?before= paging time, or None for the newest."""
    if value in (None, ""):
        return None
    before = float(value)
    if not 0 < before < 1e11:   # also refuses NaN and infinity
        raise ValueError("before")
    return before


async def api_wristband_log(request: web.Request):
    """Cards made: this person's own, or everyone's if they may see every card."""
    device, member, perms = await wristband_caller(request, limit=False)
    jobs = db.wb_jobs(WRISTBAND_LOG_PAGE, parse_before(request.query.get("before")),
                      None if perms["see_all_cards"] else member["id"],
                      request.query.get("q", "")[:60])
    zone = await wristband_zone()
    return web.json_response({"jobs": [job_view(job, zone) for job in jobs]})


async def api_reconnect_encoder(request: web.Request):
    """Reconnect (Wristbands screen): connect to this computer's card encoder now, read it and
    let it go, so its status says how it is now rather than how it was when it was last used.
    Refused while it's making cards, and at most every WRISTBAND_TEST_GAP seconds (shared with
    the dashboard's Test encoder)."""
    device, member, _perms = await wristband_caller(request)
    url = device["encoder_url"]
    if encoder_busy(url):
        return bad_request(ENCODER_BUSY, 409)
    now = time.time()
    if now - encoders_tested.get(device["id"], 0) < WRISTBAND_TEST_GAP:
        return bad_request("The card encoder was checked a moment ago. Wait a few seconds, then "
                           "try again.", 429)
    if len(encoders_tested) > 1000:
        encoders_tested.clear()
    encoders_tested[device["id"]] = now
    actor, role = actor_name(device, member), role_label(device, member)
    try:
        version = await test_encoder(device)
    except EncoderError as err:
        message = encoders.get(url).explain(err.code)
        db.log(actor, "Reconnected the card encoder", detail=message, role=role, ok=False,
               ip=client_ip(request))
        return bad_request(message, 502)
    db.log(actor, "Reconnected the card encoder", detail=str(version)[:200], role=role,
           ip=client_ip(request))
    return web.json_response({"ok": True, "version": version})


async def api_start_lookup(request: web.Request):
    """Read the wristband on this computer's encoder: its number and what's written on it."""
    device, member, perms = await wristband_caller(request, change=True)
    if encoder_busy(device["encoder_url"], lookup=False):   # a new look-up replaces one waiting
        return bad_request(ENCODER_BUSY, 409)
    start_lookup(device, member)
    return web.json_response({"state": "waiting", "card_number": None, "card": [], "job": None,
                              "message": "Place the wristband on the encoder"})


def card_view(records: list[dict], zone: ZoneInfo | None) -> list[dict]:
    """What a wristband read off the encoder opens, in words (lock MACs stay here)."""
    doors = db.wb_doors()
    by_mac = {d["mac"]: d for d in doors}
    buildings, floors = {}, {}
    for d in doors:
        b, f = d["building_number"], d["floor_number"]
        if b is not None and (d["building_name"] or b not in buildings):
            buildings[b] = d["building_name"] or f"Building {b}"
        if b is not None and f is not None and (d["floor_name"] or (b, f) not in floors):
            floors[(b, f)] = d["floor_name"] or f"Floor {f}"
    view = []
    for record in records:
        level, b, f = record.get("level"), record.get("building"), record.get("floor")
        if level == "door":
            door = by_mac.get(record.get("mac"))
            label = door_name(door) if door else "A door TT Hotel doesn't list"
        elif level == "floor":
            label = f"{buildings.get(b, f'Building {b}')}, {floors.get((b, f), f'Floor {f}')}"
        elif level == "building":
            label = buildings.get(b, f"Building {b}")
        elif level == "all":
            label = "All doors"
        else:
            label = "Something the encoder couldn't read"
        expires = record.get("expires_at")
        view.append({"level": level, "label": label, "building": b, "floor": f,
                     "expires_at": expires,
                     "expires_label": (wristbands.format_expiry(expires, time.time(), zone)
                                       if expires and zone else "")})
    return view


def lookup_empty(found: dict) -> bool:
    """Whether a card read off the encoder has nothing on it: blank (or another hotel's), or read
    with no doors and no lost card notice, as after it's cleared or cancelled in TT Hotel."""
    if found.get("state") != "done":
        return False
    return bool(found.get("blank")) or bool(found.get("read") and not found.get("card")
                                            and not found.get("cancellations"))


def lookup_card_entry(job: dict | None, number: str) -> dict | None:
    """Our record of this very card in a job (the job may have made several)."""
    return next((c for c in (job or {}).get("cards", [])
                 if c["status"] == "written" and c["card_number"] == number), None)


# Who a record says revoked a card that was read back with nothing on it.
FOUND_CLEARED = "Found cleared on the encoder"


def note_cleared_card(found: dict, job: dict | None, device: dict, member: dict) -> bool:
    """A card our record says is active read back with no doors (cancelled or cleared in TT
    Hotel's own program, say): it opens nothing now, so the record says so too. Only after the
    doors were actually read: a card the encoder can't read as this hotel's (a sector setting
    changed, say) may still open doors, so it's left alone. Returns whether a record changed."""
    entry = lookup_card_entry(job, found.get("card_number"))
    if (not entry or entry["state"] != "active" or found.get("blank")
            or not lookup_empty(found)):
        return False
    db.set_card_state(entry["id"], "revoked", FOUND_CLEARED, cleared=True)
    db.log(actor_name(device, member),
           f"Card {entry['card_number']} ({job['name']}) found cleared on the encoder",
           role=role_label(device, member), detail="Marked revoked: it has no doors on it")
    return True


def lookup_wipeable(found: dict) -> bool:
    """Whether the card just looked up has something on it to wipe (doors, or a lost card
    notice). A card that couldn't be read isn't offered: clearing it would fail the same way."""
    return bool(found.get("state") == "done" and found.get("card_number") and found.get("read")
                and (found.get("card") or found.get("cancellations")))


def lookup_may_revoke(found: dict, job: dict | None, member: dict, perms: dict) -> bool:
    """Whether this person may wipe the card they just looked up: people who may see every card,
    or whoever made it."""
    return lookup_wipeable(found) and bool(
        perms["see_all_cards"] or (job and job["staff_id"] == member["id"]))


async def api_lookup(request: web.Request):
    """The look-up this person started on this computer's encoder: the card's number, what's
    on it, and our record of it (if they may see it)."""
    device, member, perms = await wristband_caller(request, limit=False)
    found = lookup_result(device, member)
    if found is None:
        return web.json_response({"state": "error", "card_number": None, "card": [], "job": None,
                                  "message": "Start the look-up again."})
    zone = await wristband_zone()
    job = db.wb_card_job(found["card_number"]) if found.get("card_number") else None
    if job and note_cleared_card(found, job, device, member):
        job = db.wb_card_job(found["card_number"])
    may_revoke = lookup_may_revoke(found, job, member, perms)
    # A blank card's own message is said by "blank" (the app explains it next to "no doors").
    message = None if found.get("blank") else found.get("message")
    if job and job["staff_id"] != member["id"] and not perms["see_all_cards"]:
        job = None
        message = message or "Someone else made this wristband, so its details aren't shown."
    elif found["state"] == "done" and not job:
        message = message or "There's no record of this wristband being made here."
    return web.json_response({"state": found["state"], "card_number": found.get("card_number"),
                              "card": card_view(found.get("card") or [], zone),
                              # Nothing on the card: blank, or cleared (in TT Hotel, say).
                              "empty": lookup_empty(found), "blank": bool(found.get("blank")),
                              # A lost card notice (or replacement) read back: the card numbers
                              # it cancels [{"card_number", "expires_at"}] (None: not read).
                              "cancellations": found.get("cancellations"),
                              "can_revoke": may_revoke,
                              "job": job and job_view(job, zone), "message": message})


async def api_lookup_revoke(request: web.Request):
    """Wipe the card just looked up, which is still on this computer's encoder: the usual revoke
    job, for that card number. For people who may see every card, or who made this one. The
    card number comes from the look-up, never from the request."""
    device, member, perms = await wristband_caller(request, change=True)
    found = lookup_result(device, member)
    if not found or found["state"] != "done" or not found.get("card_number"):
        return bad_request("Look the card up again first.", 409)
    number = found["card_number"]
    job = db.wb_card_job(number)
    actor, role, ip = actor_name(device, member), role_label(device, member), client_ip(request)
    if not lookup_wipeable(found):
        return bad_request("This card has no doors on it, so there's nothing to wipe.", 409)
    if not lookup_may_revoke(found, job, member, perms):
        db.log(actor, "Blocked: revoke a card", role=role, detail=f"card {number}", ok=False, ip=ip)
        raise json_error(web.HTTPForbidden, "You can only revoke cards you made. Ask a manager.")
    if encoder_busy(device["encoder_url"]):
        return bad_request(ENCODER_BUSY, 409)
    entry = lookup_card_entry(job, number)
    ends = [r["expires_at"] for r in found.get("card") or [] if r.get("expires_at")]
    card = {"id": entry and entry["id"], "card_number": number, "holder_name": None,
            "name": job["name"] if job else f"Card {number}",
            "expires_at": job["expires_at"] if job else max(ends, default=time.time()),
            "holder_staff_id": job and job.get("holder_staff_id"),
            "department_id": job and job.get("department_id")}
    job_id = start_revoke_job(card, device, actor=actor, staff_id=member["id"], role=role, ip=ip,
                              source="staff_app", card_type=job["card_type"] if job else "staff")
    db.log(actor, f"Started revoking card {number} ({card['name']})", role=role, ip=ip)
    return web.json_response({"job": job_view(db.wb_job(job_id), await wristband_zone())})


# Which encoder a computer uses: Off, This computer (USB) (plugged into it, reached through
# the staff app open on it), Automatic (found on the network, once an admin confirms it) or a
# typed address.

def encoder_mode(device: dict) -> str:
    """"address" (typed in; it always wins), "local" (This computer (USB)), "auto" or "off"."""
    if device.get("encoder_url"):
        return "address"
    if device.get("encoder_local"):
        return "local"
    return "auto" if device.get("encoder_auto") else "off"


def encoder_address(device: dict, rescan: bool = True) -> str | None:
    """Where this computer's card encoder is now: the address typed in; "local:<its ID>" for
    This computer (USB) (whether or not its link is connected now: a job waits for it); for
    Automatic the SN Web Socket helper an admin confirmed for it, if it's found; else None.
    Spelled as transfer.encoder_url spells a typed one, so one encoder still gets one runner.
    With `rescan`, an Automatic encoder that wasn't found, or has stopped answering, has the
    network searched again (at most every 10 minutes, and only while something uses
    Automatic)."""
    mode = encoder_mode(device)
    if mode == "local":
        return local_address(device["id"])
    if mode != "auto":
        return device.get("encoder_url") if mode == "address" else None
    helper, why = discovery.resolve(device.get("encoder_auto_ip"))
    url = transfer.encoder_url(helper["url"]) if helper else None
    if rescan and why != "choose":
        runner = encoders.find(url)
        if url is None or (runner is not None and runner.reachable is False):
            discovery.scan_soon()
    return url


# This computer (USB) with its link open: SN Web Socket there as its browser last said, in the
# dashboard's words (anything else: still connecting).
LINK_WORDS = {"ready": "connected", "unreachable": "SN Web Socket isn't answering",
              "blocked": "blocked by the browser"}


def encoder_link(device: dict) -> dict | None:
    """This computer (USB): the computer's browser link, {"link", "connected_at", "last_seen",
    "helper", "helper_version", "message"} (from memory). None in other modes."""
    if encoder_mode(device) != "local":
        return None
    relay = encoders.relay(device["id"])
    return {**relay.state(), "message": relay.summary()}


def encoder_text(device: dict) -> str | None:
    """How a computer's encoder is set and what it resolves to, for the dashboard: "This
    computer (USB): connected", "Automatic: 192.168.1.50", "Automatic: 2 found, choose one", ...
    None when it has none."""
    mode = encoder_mode(device)
    if mode == "local":
        relay = encoders.find_relay(device["id"])
        state = ("not connected" if relay is None or not relay.connected
                 else LINK_WORDS.get(relay.helper, "connecting"))
        return f"This computer (USB): {state}"
    if mode != "auto":
        return device["encoder_url"] if mode == "address" else None
    helper, why = discovery.resolve(device.get("encoder_auto_ip"))
    if helper:
        return f"Automatic: {helper['ip']}"
    if discovery.scanning:
        return "Automatic: looking for encoders…"
    # Found but not confirmed yet: the ones found are kept after a search that didn't work.
    if why == "choose" and len(discovery.helpers) == 1:
        return (f"Automatic: found {discovery.helpers[0]['ip']}. Click Use this encoder to "
                "confirm it.")
    if why == "choose":
        return f"Automatic: {len(discovery.helpers)} found, choose one"
    if discovery.error:
        return f"Automatic: {discovery.error}"
    if discovery.scanned_at is None:
        return "Automatic: not looked for yet. Click Find encoders."
    if why == "missing":
        return (f"Automatic: {device['encoder_auto_ip']} not found on "
                f"{describe(discovery.networks)}")
    return f"Automatic: no encoder found on {describe(discovery.networks)}"


def wristbands_note(device: dict) -> str | None:
    """Why Wristbands won't show on this computer, for its card in the dashboard (None if it
    can, as far as the computer goes: on a role-based one it still depends on each person)."""
    if device.get("kind") != "computer" or device.get("status") == "revoked":
        return None
    mode = encoder_mode(device)
    if db.settings().get("wristbands_enabled") != "1":
        # This computer (USB) is every new computer's default, so it says nothing either: a
        # property that doesn't use Wristbands would see this on every computer.
        return ("Wristbands won't show here: Wristbands is turned off (Wristbands tab)."
                if mode not in ("off", "local") else None)
    if mode == "off":
        return "Wristbands won't show here: it has no card encoder (Edit → Card encoder)."
    if not encoder_address(device, rescan=False):
        if discovery.resolve(device.get("encoder_auto_ip"))[1] == "choose" and discovery.helpers:
            return ("Wristbands won't show here: confirm its card encoder first (Edit → Card "
                    "encoder → Use this encoder).")
        return "Wristbands won't show here: its card encoder hasn't been found yet."
    if not is_shared(device):
        return ("Wristbands won't show here: in Kiosk mode for one person or team, no one signs "
                "in. Edit it and choose Role-based, or Shared.")
    if not role_based(device):
        role = db.role(device["role_id"]) if device.get("role_id") else None
        if not role or not (role["wristbands"] or {}).get("types"):
            return ("Wristbands won't show here: it's Shared, and its role can't make any "
                    "wristbands (Roles tab).")
    link = encoder_link(device)
    if link and not link["link"]:
        # Wristbands shows (the encoder connects through it): say what's missing.
        return ("Card encoder not connected: it connects while someone signed in here has "
                "Wristbands open, with SN Web Socket running on this computer.")
    if link and link["helper"] == "blocked":
        return ("Card encoder: the browser on this computer is blocking it. Choose Allow when the "
                "browser asks, or allow Apps on device for the staff app (the icon at the left of "
                "the address bar).")
    if link and link["helper"] == "unreachable":
        return ("Card encoder: Wristbands is open on this computer, but SN Web Socket isn't "
                "answering there. Check that it's running.")
    # Still connecting, or the browser there is asking whether the page may reach SN Web Socket:
    # the card says "checking SN Web Socket", which is all that's known yet.
    return None


# The encoder runners (encoder.py, one per encoder address) are only used through these.

lookups: dict[str, tuple[int, int]] = {}   # encoder address -> (device, person) looking up there
STOPPED_UNEXPECTEDLY = "Something went wrong, so this job was stopped. Start it again."


def encoder_status(url: str) -> dict:
    """{"connected", "busy", "error", "checked_at"} for an encoder, from its runner's memory
    (at most every half minute the runner checks in the background that the desk computer
    still answers)."""
    return encoders.status(url)


def encoder_job(url: str) -> dict | None:
    """The unfinished card job on an encoder, if any. One the records say is unfinished but no
    runner knows about (it can't happen, short of a bug) is closed, so it can't block the
    encoder until the next restart."""
    job = db.active_wb_job(url)
    if job and encoders.live(url, job["id"]) is None:
        log.warning("Card job %s was unfinished with nothing working on it; closing it", job["id"])
        db.cancel_wb_job(job["id"], STOPPED_UNEXPECTEDLY)
        return None
    return job


def encoder_busy(url: str, lookup: bool = True) -> bool:
    """Whether an encoder already has a card job (or, with `lookup`, a look-up) under way. A new
    one is refused rather than queued behind it, so a double click or a second computer sharing
    the encoder can't start cards nobody is standing at the encoder for."""
    runner = encoders.find(url)
    state = runner.lookup_state() if runner and lookup else None
    return encoder_job(url) is not None or bool(state and state["state"] == "waiting")


def job_progress(job: dict) -> dict | None:
    """{"phase", "current", "message", ...} of an unfinished job, from its runner's memory."""
    return encoders.live(job["encoder_url"], job["id"])


def runner_job(job: dict) -> dict:
    """A wb_jobs row as the encoder runner takes it: a revoke names the card to clear
    ("card_number"); a lost card notice, or a replacement card, carries the cancellation of
    "cancel_card", valid until "cancel_until" (the lost card's own expiry)."""
    extra = {"kind": job.get("kind") or "write"}
    if extra["kind"] == "revoke":
        extra["card_number"] = job.get("target_card")
    elif job.get("target_card"):
        extra.update(cancel_card=job["target_card"],
                     cancel_until=int(job.get("cancel_until") or job["expires_at"]))
    return {**job, **extra}


def start_job(job: dict, device: dict) -> None:
    settings = db.settings()
    encoders.get(job["encoder_url"]).submit(runner_job(job), port=device["encoder_port"],
                                            sectors=settings.get("wb_sectors", ""),
                                            card_wait=db.setting_int("wb_card_wait"))


def decide_job(job: dict, action: str) -> None:
    """Try again, skip or stop. Stopping a job no runner knows about any more just closes it."""
    runner = encoders.find(job["encoder_url"])
    if runner and runner.live(job["id"]) is not None:
        getattr(runner, action)(job["id"])
    elif action == "cancel":
        db.cancel_wb_job(job["id"], STOPPED_UNEXPECTEDLY)


def start_lookup(device: dict, member: dict) -> None:
    lookups[device["encoder_url"]] = (device["id"], member["id"])
    encoders.get(device["encoder_url"]).start_lookup(port=device["encoder_port"],
                                                     card_wait=db.setting_int("wb_card_wait"))


def lookup_result(device: dict, member: dict) -> dict | None:
    """The look-up on this computer's encoder, if this person started it."""
    runner = encoders.find(device["encoder_url"])
    if not runner or lookups.get(device["encoder_url"]) != (device["id"], member["id"]):
        return None
    return runner.lookup_state()


async def test_encoder(device: dict) -> str:
    """Connect to a computer's encoder, read its version and let it go (raises EncoderError)."""
    return await encoders.get(device["encoder_url"]).test(device["encoder_port"])


def save_job_progress(job_id: int, job_fields: dict, card_fields: dict | None) -> None:
    """How a runner records a job's progress (the runners never touch the database themselves).
    A job that ends with cards missing gets its own activity entry."""
    if card_fields:
        fields = dict(card_fields)
        seq = fields.pop("seq")
        db.update_wb_card(job_id, seq, **fields)
        if fields.get("status") in ("written", "cleared"):   # cleared: a revoke's card
            card_done(job_id, seq)
    if job_fields:
        db.update_wb_job(job_id, **job_fields)
    if job_fields.get("status") not in ("done", "cancelled"):
        return
    job = db.wb_job(job_id)
    written = sum(1 for c in job["cards"] if c["status"] in ("written", "cleared")) if job else 0
    if job and written < job["count"] and (job.get("kind") or "write") != "write":
        what = "revoke" if job["kind"] == "revoke" else "make a lost card notice for"
        db.log(job["actor"], f"Couldn't {what} card {job['target_card']}", role=job["role"],
               detail=job["message"] or finished_message(job), ok=False, ip=job["ip"])
    elif job and written < job["count"]:
        what = card_noun(job["card_type"], job["count"])
        db.log(job["actor"], f"Made only {written} of {what} for {job['name']}" if written
               else f"Couldn't make {what} for {job['name']}", role=job["role"],
               detail=job["message"] or finished_message(job), ok=False, ip=job["ip"])


def card_done(job_id: int, seq: int) -> None:
    """A card in a job was written (or, for a revoke, cleared): keep the key card records right.
    A card number written again retires its older records; a revoke marks its card revoked; a
    replacement carries the old card's cancellation (the old card is revoked unless it's lost);
    a lost card notice lists the doors to tap it on."""
    job = db.wb_job(job_id)
    card = next((c for c in job["cards"] if c["seq"] == seq), None) if job else None
    if not card:
        return
    kind = job.get("kind") or "write"
    target = db.key_card(job["target_card_id"]) if job.get("target_card_id") else None
    if kind == "revoke":
        # The card itself is cleared (a lost one found again stays lost): a lock record from
        # later on is whoever it's given to next, not this card (db.match_card).
        if target and target["state"] == "active":
            db.set_card_state(target["id"], "revoked", job["actor"], cleared=True)
            db.log(job["actor"], f"Revoked key {card_label(target)}", role=job["role"],
                   ip=job["ip"])
        elif target:
            db.mark_card_cleared(target["id"])
        else:
            # A wristband (only staff cards are key cards), or a card with no record here.
            other = db.wb_card(job["target_card_id"]) if job.get("target_card_id") else None
            if other and other["state"] != "active":
                db.mark_card_cleared(other["id"])
                return
            if other:
                db.set_card_state(other["id"], "revoked", job["actor"], cleared=True)
            db.log(job["actor"], f"Revoked card {job['target_card']}" + (
                f" ({other['name']})" if other else ""), role=job["role"], ip=job["ip"])
        return
    if card["card_number"]:
        db.retire_card_number(card["card_number"], card["id"], f"Written over by {job['actor']}")
    if not target:
        return
    doors = [d["lock_id"] for d in card_doors(target["records"])]
    if kind == "notice":
        db.add_lockouts(target["id"], doors, "notice")
        for lock_id in doors:
            if not any(lo["lock_id"] == lock_id and lo["method"] == "notice" and lo["status"] == "done"
                       for lo in target["lockouts"]):
                db.set_lockout(target["id"], lock_id, "notice", "pending",
                               f"Tap notice card {card['card_number']} on this door")
        db.log(job["actor"], f"Made a lost card notice for key {card_label(target)}",
               detail=f"notice card {card['card_number']}, {len(doors)} doors to tap")
        return
    db.update_wb_card(job_id, seq, cancelled_card=target["card_number"])
    db.add_lockouts(target["id"], doors, "replacement")
    if target["state"] == "active":
        db.set_card_state(target["id"], "revoked", f"Replaced by card {card['card_number']}")


def recover_wristband_jobs() -> None:
    """Card jobs left unfinished when the add-on stopped can't carry on (nothing is writing them
    any more), so they're closed, and the person can start them again. So are remote lock-outs."""
    stopped = db.cancel_unfinished_wb_jobs("Stopped because Staff Portal restarted. Start it again.")
    if db.stop_remote_lockouts("Stopped because Staff Portal restarted. Lock it out again."):
        log.info("Stopped remote lock-outs that were running when Staff Portal restarted")
    if stopped:
        db.log("Staff Portal", f"Stopped {len(stopped)} unfinished card job(s) after a restart",
               ok=False)


# ---------------------------------------------------------------- reports (staff app)
# Reports (app/reports/<key>/, see REPORTS.md): each one's on/off, and which departments may view
# and manage it, are set in the dashboard's Reports tab and checked here on every request. Only
# someone signed in with their own PIN (on a shared or role-based device) has departments, so a
# kiosk device nobody signs in to shows none. Every department they're in counts (hidden ones
# give nothing). Their Connecteam data comes through ctdata (its own
# lane of the budget, asked only when a report is opened), and files a manager uploads are kept
# under DATA_DIR/reports/<key>/uploads/.

REPORTS = reports_kit.discover()
REPORT_REQUESTS_PER_MINUTE = 30      # per device: opening reports, changing weeks, uploads
REPORT_TICKET_SECONDS = 3600         # how long a report's view.js address works
REPORT_TICKET_KEY = secrets.token_bytes(32)   # new at each start: older addresses stop working
NO_REPORT = "You don't have access to that report."
NO_PERSON = "Sign in with your own PIN to see reports."
recent_report_requests: dict[int, list[float]] = {}


def report_zone() -> ZoneInfo:
    """The property's time zone for reports and tasks: the one in Wristbands settings, else Home
    Assistant's as last read, else UTC. Never waits for Home Assistant."""
    for name in (db.settings().get("wb_time_zone"), ha_zone["name"]):
        try:
            if name:
                return ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError):
            continue
    return ZoneInfo("UTC")


def report_rights(member: dict | None, key: str, access: dict | None = None) -> tuple[bool, bool]:
    """(may view, may manage) a report: it exists, it's on, and one of the person's departments
    is one the owner chose (manage includes view)."""
    report = REPORTS.get(key)
    if not member or not report or report.hidden or staff_off(member):
        return False, False
    access = access or db.report(key)
    mine = set(db.member_department_ids(member))
    if not access["enabled"] or not mine:
        return False, False
    manage = bool(mine & set(access["manage"]))
    return manage or bool(mine & set(access["view"])), manage


def visible_reports(member: dict | None) -> list[str]:
    """The reports this person may see, in the order they're listed."""
    if not member:
        return []
    access = db.reports()
    return sorted((k for k in REPORTS if k in access and report_rights(member, k, access[k])[0]),
                  key=lambda k: REPORTS[k].name.lower())


def reports_too_busy(device_id: int) -> bool:
    now = time.time()
    times = [t for t in recent_report_requests.get(device_id, []) if now - t < 60]
    busy = len(times) >= REPORT_REQUESTS_PER_MINUTE
    if not busy:
        times.append(now)
    recent_report_requests[device_id] = times
    return busy


def report_caller(request: web.Request, manage: bool = False,
                  limit: bool = True) -> tuple[dict, dict, bool]:
    """(device, signed-in person, whether they may manage it) for a request about the report in
    the address, or raise the error the app should show. manage: uploading or changing settings,
    which only people allowed to manage the report may do."""
    device, member = authenticate(request, active=manage)
    if not member:
        raise json_error(web.HTTPForbidden, NO_PERSON)
    if limit and reports_too_busy(device["id"]):
        raise json_error(web.HTTPTooManyRequests, "Too many requests at once. Wait a moment and "
                                                  "try again.")
    view, can_manage = report_rights(member, request.match_info["key"])
    if not view or (manage and not can_manage):
        if manage:
            db.log(actor_name(device, member), "Blocked: report",
                   role=role_label(device, member), detail=request.match_info["key"][:40],
                   ok=False, ip=client_ip(request))
        raise json_error(web.HTTPForbidden, NO_REPORT)
    return device, member, can_manage


def report_ticket(key: str, device_id: int, staff_id: int, now: float | None = None) -> str:
    """A view.js or view.css address's proof that this person on this device may see the report
    (a script tag can't send the app's sign-in): signed here, and checked again when used."""
    expires = int((time.time() if now is None else now) + REPORT_TICKET_SECONDS)
    body = f"{device_id}.{staff_id}.{expires}"
    sig = hmac.new(REPORT_TICKET_KEY, f"{key}|{body}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def ticket_allows(key: str, ticket: str) -> bool:
    parts = ticket.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts[:3]) or len(parts[0] + parts[1]) > 24:
        return False
    device_id, staff_id, expires = (int(p) for p in parts[:3])
    good = report_ticket(key, device_id, staff_id, expires - REPORT_TICKET_SECONDS)
    if not hmac.compare_digest(good, ticket) or expires < time.time():
        return False
    device, member = db.device(device_id), db.staff(staff_id)
    return bool(device and device["status"] == "active" and is_shared(device) and member
                and member["status"] == "active" and db.staff_allowed_on(staff_id, device)
                and report_rights(member, key)[0])


def report_view(report, device: dict, member: dict, manage: bool) -> dict:
    """A report as the staff app lists it: where its view is, and its uploads' newest files."""
    ticket = report_ticket(report.key, device["id"], member["id"])
    uploads = []
    for u in report.uploads:
        latest = reports_kit.uploads(DATA_DIR, report.key, u["key"], text=False)[:1]
        uploads.append({k: u[k] for k in ("key", "name", "help", "accept")} | {
            "max_bytes": u["max_bytes"], "latest": latest[0] if latest else None})
    return {**report.public(), "manage": manage, "uploads": uploads,
            "view_js": f"/reports/{report.key}/view.js?a={ticket}",
            "view_css": f"/reports/{report.key}/view.css?a={ticket}" if report.has_css else None}


async def api_reports(request: web.Request):
    device, member = authenticate(request)
    if not member:
        raise json_error(web.HTTPForbidden, NO_PERSON)
    access = db.reports()
    return web.json_response({"reports": [
        report_view(REPORTS[key], device, member, report_rights(member, key, access[key])[1])
        for key in visible_reports(member)]})


def report_context(report, member: dict, manage: bool) -> "reports_kit.Context":
    """Departments here are the shown ones, in the Staff tab's order: hidden ones give nothing."""
    shown = db.shown_departments()
    names = {d["id"]: d["name"] for d in shown}
    staff = []
    for r in db.conn.execute("SELECT connecteam_user_id, department_ids FROM staff "
                             "WHERE connecteam_user_id IS NOT NULL"):
        ids = db.member_department_ids(dict(r), shown)
        if ids:
            staff.append({"connecteam_user_id": r["connecteam_user_id"], "department_ids": ids,
                          "department_id": ids[0]})
    return reports_kit.Context(
        ct=ctdata, report=report, data_dir=DATA_DIR,
        settings=reports_kit.stored_settings(report, db.report(report.key)["settings"]),
        person={"id": member["id"], "name": member["name"],
                "connecteam_user_id": member.get("connecteam_user_id"),
                "departments": [{"id": i, "name": names[i]}
                                for i in db.member_department_ids(member, shown)]},
        departments=[{"id": d["id"], "name": d["name"], "smart_group_id": d["smart_group_id"]}
                     for d in shown],
        staff=staff, tz=report_zone(), manage=manage)


REPORT_WAIT = 20         # seconds a request waits for a report before saying "still working on it"
REPORT_KEEP = 120        # seconds a finished report waits to be picked up
report_jobs: dict[tuple, dict] = {}   # (report, person, manage, params) -> {"task", "done"}


async def run_report(report, ctx, params: dict, manage: bool) -> tuple[int, str]:
    """A report's data(ctx, params), as (HTTP status, JSON body)."""
    try:
        data = await report.data(ctx, params)
        if not isinstance(data, dict):
            raise TypeError("data() must return a dict")
        # Someone who can't manage the report gets the settings data() sent (trimmed to what
        # they may see, when it trims them), not all that's stored.
        shown = data["settings"] if not manage and isinstance(data.get("settings"), dict) else ctx.settings
        return 200, json.dumps({"data": data, "settings": shown, "manage": manage, "params": params})
    except ctdata.Unavailable as err:
        return 503, json.dumps({"error": str(err), "unavailable": True})
    except ValueError as err:
        return 400, json.dumps({"error": str(err)[:300]})
    except Exception:  # noqa: BLE001 - a report's own bug mustn't take the app down
        log.exception("Report %s failed", report.key)
        return 500, json.dumps({"error": "This report couldn't be made. Try again later."})


async def api_report_data(request: web.Request):
    """Run a report's data(ctx, params) for the person signed in. A first look can need more
    Connecteam requests than its lane allows in a minute, longer than the tunnel waits (Cloudflare
    gives up after 100 seconds), so after REPORT_WAIT it carries on in the background and the
    answer is 202 {"pending": true}: the staff app asks again with the same params and gets it
    once it's ready."""
    device, member, manage = report_caller(request)
    report = REPORTS[request.match_info["key"]]
    params = reports_kit.clean_params(report, request.query)
    now = time.monotonic()
    for k, job in list(report_jobs.items()):
        if job["done"] and now - job["done"] > REPORT_KEEP:
            del report_jobs[k]
    key = (report.key, member["id"], manage, json.dumps(params, sort_keys=True))
    job = report_jobs.get(key)
    if job is None:
        if len(report_jobs) >= 50:
            return bad_request("Reports are busy. Try again in a minute.", 503)
        if not db.settings().get("wb_time_zone"):
            await ha_time_zone()   # so report_zone() knows Home Assistant's zone
        task = asyncio.create_task(run_report(report, report_context(report, member, manage),
                                              params, manage))
        job = report_jobs[key] = {"task": task, "done": None}
        task.add_done_callback(lambda _t, job=job: job.update(done=time.monotonic()))
    try:
        status, body = await asyncio.wait_for(asyncio.shield(job["task"]), REPORT_WAIT)
    except TimeoutError:
        return web.json_response({"pending": True}, status=202)
    report_jobs.pop(key, None)
    return web.Response(text=body, status=status, content_type="application/json")


async def read_upload_file(request: web.Request, limit: int) -> tuple[str, bytes]:
    """The "file" field of a multipart upload: (its file name, its bytes, read up to limit).
    Raises ValueError, and HTTPRequestTimeout like read_json."""
    try:
        async with asyncio.timeout(request.app.get(BODY_SECONDS, 10)):
            try:
                reader = await request.multipart()
            except (AssertionError, KeyError, ValueError):
                raise ValueError("Choose a file to upload.") from None
            while (part := await reader.next()) is not None:
                if getattr(part, "name", None) != "file":
                    await part.release()
                    continue
                data = bytearray()
                while chunk := await part.read_chunk(64 * 1024):
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ValueError(f"That file is too big (at most "
                                         f"{reports_kit._size(limit)}).")
                return part.filename or "", bytes(data)
    except TimeoutError:
        raise web.HTTPRequestTimeout(text="The upload took too long to arrive.") from None
    raise ValueError("Choose a file to upload.")


async def api_report_upload(request: web.Request):
    """A file for a report (a Gusto export, say), from someone who may manage it."""
    device, member, _ = report_caller(request, manage=True)
    report = REPORTS[request.match_info["key"]]
    upload = next((u for u in report.uploads if u["key"] == request.match_info["upload"]), None)
    if not upload:
        return bad_request("This report has no such upload.", 404)
    try:
        filename, raw = await read_upload_file(request, upload["max_bytes"])
        name, _ = reports_kit.check_file(upload, filename, raw)
        meta = await asyncio.to_thread(reports_kit.save_upload, DATA_DIR, report.key, upload,
                                       name, raw, member["name"])
    except ValueError as err:
        return bad_request(str(err))
    except OSError as err:
        log.warning("Couldn't save an upload for report %s: %s", report.key, err)
        return bad_request("The file couldn't be saved. Try again.", 500)
    db.log(actor_name(device, member), f"Uploaded a file to {report.name}",
           role=role_label(device, member), detail=f"{upload['name']}: {name}",
           ip=client_ip(request))
    return web.json_response({"ok": True, "latest": meta})


async def api_report_settings(request: web.Request):
    """A report's own settings, from someone who may manage it."""
    device, member, _ = report_caller(request, manage=True)
    report = REPORTS[request.match_info["key"]]
    try:
        settings = reports_kit.clean_settings(report, await read_json(request))
    except ValueError as err:
        return bad_request(str(err)[:300])
    db.set_report(report.key, actor_name(device, member), settings=settings)
    db.log(actor_name(device, member), f"Changed the settings of {report.name}",
           role=role_label(device, member), ip=client_ip(request))
    return web.json_response({"settings": reports_kit.stored_settings(report, settings)})


async def report_file(request: web.Request):
    """A report's view.js or view.css, for an address api_reports gave this person."""
    key, name = request.match_info["key"], request.match_info["file"]
    report = REPORTS.get(key)
    if (not report or (name == "view.css" and not report.has_css)
            or not ticket_allows(key, request.query.get("a", ""))):
        raise web.HTTPNotFound()
    return web.FileResponse(os.path.join(report.folder, name), headers={
        "Content-Type": "text/javascript" if name == "view.js" else "text/css",
        "Cache-Control": "no-store"})


# ---------------------------------------------------------------- encoder links (staff app)
# A computer whose card encoder is This computer (USB) is never connected to by the add-on.
# The staff app open on it (someone signed in who may make wristbands, Wristbands open) keeps a
# WebSocket to the add-on instead, GET /api/encoder/link, through the tunnel like any other
# request, and passes each SN Web Socket request it's sent on to ws://127.0.0.1:8102 on the same
# computer, and the answers back (encoder.LocalRelay has the messages). Browsers can't add the
# device token to a WebSocket, so the app first gets a ticket from POST /api/encoder/ticket (with
# its token and PIN session): good once, for LINK_TICKET_SECONDS, for that device and session.
# One link per computer: a newer one replaces the old. A link closes when its PIN session ends
# (signed out, idle, ended by an admin), or when the computer or person may no longer use it.

LINK_TICKET_SECONDS = 30          # a ticket is used at once, so it needn't last
LINK_TICKETS_PER_MINUTE = 12      # per device: reconnecting with backoff needs a few at most
MAX_LINK_TICKETS = 1000           # tickets kept at once (the oldest go first)
LINK_CHECK_SECONDS = 5.0          # an open link checks its session and access this often
LINK_HEARTBEAT = 30.0             # WebSocket pings, so a link to a computer that's gone closes
LINK_CLOSE_SECONDS = 5.0          # closing waits this long for the browser to agree
MAX_LINK_MESSAGE = 256 * 1024     # a card's records with the hotel key fit many times over
LINK_MESSAGES_PER_MINUTE = 1200   # polling for a card every 0.3 s is about 200 a minute
# Close codes the app can act on: replaced by a newer link (don't reconnect on your own), the
# PIN session ended (sign in again first), no longer allowed, too many messages, and the add-on
# stopping (reconnect, with the usual backoff).
LINK_REPLACED, LINK_SIGNED_OUT, LINK_NOT_ALLOWED, LINK_TOO_BUSY = 4000, 4001, 4003, 4008
LINK_STOPPING = 1001
NO_LINK_ACCESS = "This computer's card encoder can't be connected here."

link_tickets: dict[str, tuple[int, str, int, float]] = {}   # ticket -> (device, session, person,
                                                            #            expires at)
recent_link_tickets: dict[int, list[float]] = {}


@dataclasses.dataclass
class EncoderLink:
    """An open link: its number at the relay, its WebSocket, and the PIN session it's bound to."""
    number: int
    ws: web.WebSocketResponse
    session: str
    staff_id: int


open_links: dict[int, EncoderLink] = {}   # device -> its link


def link_refusal(device_id: int, session: str, staff_id: int) -> str | None:
    """Why a computer's encoder link may not be (or stay) open: "signed_out" once the PIN
    session it belongs to has ended (signed out, idle, ended by an admin), "not_allowed" when the
    computer or the person may no longer use it (revoked or locked, not This computer (USB) any
    more, no wristbands). None: it may."""
    device = db.device(device_id)
    if not device or device["status"] != "active":
        return "not_allowed"
    held = pin_sessions.get(session)
    if (not held or held[0] != device_id or held[2] != staff_id
            or time.time() - held[1] > pin_timeout(device)):
        return "signed_out"
    member = db.staff(staff_id)
    if (not is_shared(device) or staff_off(member) or member["status"] != "active"
            or not db.staff_allowed_on(member["id"], device)):
        return "signed_out"   # the app is asked for a PIN again too
    if encoder_mode(device) != "local" or not wristband_permissions(device, member)["types"]:
        return "not_allowed"
    return None


def link_tickets_too_busy(device_id: int) -> bool:
    now = time.time()
    times = [t for t in recent_link_tickets.get(device_id, []) if now - t < 60]
    busy = len(times) >= LINK_TICKETS_PER_MINUTE
    if not busy:
        times.append(now)
    recent_link_tickets[device_id] = times
    return busy


async def api_encoder_ticket(request: web.Request):
    """A ticket to open this computer's encoder link with: {"ticket", "expires_in", "path"}."""
    device, member = authenticate(request)
    session = request.headers.get("X-Session", "")
    if not member or link_refusal(device["id"], session, member["id"]):
        raise json_error(web.HTTPForbidden, NO_LINK_ACCESS)
    if link_tickets_too_busy(device["id"]):
        raise json_error(web.HTTPTooManyRequests,
                         "Too many requests at once. Wait a moment and try again.")
    now = time.time()
    if len(link_tickets) >= MAX_LINK_TICKETS:
        for key, entry in list(link_tickets.items()):
            if entry[3] < now:
                del link_tickets[key]
        for key in sorted(link_tickets, key=lambda k: link_tickets[k][3])[
                :len(link_tickets) - MAX_LINK_TICKETS + 1]:
            del link_tickets[key]
    ticket = secrets.token_urlsafe(24)
    link_tickets[ticket] = (device["id"], session, member["id"], now + LINK_TICKET_SECONDS)
    return web.json_response({"ticket": ticket, "expires_in": LINK_TICKET_SECONDS,
                              "path": "/api/encoder/link"})


async def api_encoder_link(request: web.Request):
    """This computer's encoder link (GET /api/encoder/link?ticket=...): a WebSocket for as long
    as Wristbands is open. Only this computer's own card jobs, look-ups and tests use it."""
    entry = link_tickets.pop(request.query.get("ticket", "")[:64], None)
    if not entry or entry[3] < time.time():
        raise json_error(web.HTTPUnauthorized, "Sign in again to connect the card encoder.")
    device_id, session, staff_id, _ = entry
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        # Browsers can't send one on a WebSocket; anything that does must be this very device.
        other = db.device_by_token(auth[7:])
        if not other or other["id"] != device_id:
            raise json_error(web.HTTPForbidden, NO_LINK_ACCESS)
    if link_refusal(device_id, session, staff_id):
        raise json_error(web.HTTPForbidden, NO_LINK_ACCESS)
    # No compression: messages carry the hotel key, and they're small anyway.
    ws = web.WebSocketResponse(heartbeat=LINK_HEARTBEAT, max_msg_size=MAX_LINK_MESSAGE,
                               timeout=LINK_CLOSE_SECONDS, compress=False)
    if not ws.can_prepare(request).ok:
        return bad_request("Open this address as a WebSocket.")
    await ws.prepare(request)
    # Again: a sign-out or a change to the computer while the handshake went out isn't seen by
    # recheck_links (it only knows open links). Nothing waits from here until the link is taken,
    # so one that may not stay never replaces the computer's link in another window.
    refusal = link_refusal(device_id, session, staff_id)
    if refusal:
        await ws.close(code=LINK_SIGNED_OUT if refusal == "signed_out" else LINK_NOT_ALLOWED,
                       message=refusal.encode())
        return ws
    relay = encoders.relay(device_id)
    number = relay.attach(ws.send_json)
    link = EncoderLink(number, ws, session, staff_id)
    old, open_links[device_id] = open_links.get(device_id), link
    if old is not None:
        close_link(old, LINK_REPLACED, "replaced")
    log.info("Card encoder link opened on %s", relay.name())
    try:
        await serve_link(link, relay, device_id)
    except Exception as err:  # noqa: BLE001 - a link that breaks just closes
        log.warning("Card encoder link on %s failed: %r", relay.name(), err)
    finally:
        relay.detach(number)
        if open_links.get(device_id) is link:
            del open_links[device_id]
        if not ws.closed:
            await ws.close()
        log.info("Card encoder link closed on %s", relay.name())
    return ws


async def serve_link(link: EncoderLink, relay, device_id: int, check=None) -> None:
    """Hand the browser's messages to the relay until the link closes, checking its session and
    access every LINK_CHECK_SECONDS. Messages are never logged: answers carry the hotel key.
    check: a coroutine function saying why the link may not stay open, instead of link_refusal
    (the dashboard's own link)."""
    ws = link.ws
    loop = asyncio.get_running_loop()
    checked = window = loop.time()
    count = 0
    while True:
        try:
            msg = await ws.receive(timeout=LINK_CHECK_SECONDS)
        except asyncio.TimeoutError:
            msg = None
        if msg is not None:
            if msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSING, web.WSMsgType.CLOSED,
                            web.WSMsgType.ERROR):
                return
            if loop.time() - window >= 60:
                window, count = loop.time(), 0
            count += 1
            if count > LINK_MESSAGES_PER_MINUTE:
                log.warning("Card encoder link on %s sent too many messages; closing it",
                            relay.name())
                await ws.close(code=LINK_TOO_BUSY, message=b"too many messages")
                return
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except (ValueError, RecursionError):
                    data = None
                relay.feed(link.number, data)
        if msg is None or loop.time() - checked >= LINK_CHECK_SECONDS:
            checked = loop.time()
            refusal = (await check() if check else
                       link_refusal(device_id, link.session, link.staff_id))
            if refusal:
                await ws.close(code=LINK_SIGNED_OUT if refusal == "signed_out"
                               else LINK_NOT_ALLOWED, message=refusal.encode())
                return


def close_link(link: EncoderLink, code: int, why: str) -> None:
    """Close a link in the background (its own handler then lets the relay go)."""
    if not link.ws.closed:
        background(link.ws.close(code=code, message=why.encode()), "close an encoder link")


def recheck_links(device_id: int | None = None) -> None:
    """Close the encoder links (of one computer, or all) that may not stay open, now rather
    than at their next check: after a sign-out, or a change to a computer."""
    for linked_id, link in list(open_links.items()):
        if device_id is not None and linked_id != device_id:
            continue
        refusal = link_refusal(linked_id, link.session, link.staff_id)
        if refusal:
            close_link(link, LINK_SIGNED_OUT if refusal == "signed_out" else LINK_NOT_ALLOWED,
                       refusal)


async def close_all_links(app: web.Application) -> None:
    """When the add-on stops: close every link, so stopping doesn't wait for them."""
    links = list(open_links.values())
    await asyncio.gather(*(link.ws.close(code=LINK_STOPPING, message=b"stopping")
                           for link in links if not link.ws.closed), return_exceptions=True)


# ---------------------------------------------------------------- encoder links (dashboard)
# This computer (the one you're using): the dashboard's card dialogs (Make key card, Revoke, a
# lost card notice or replacement) can use the card encoder plugged into the computer the
# dashboard is open on, with nothing set up. The dashboard page then does what the staff app does
# in This computer (USB) mode: it keeps a WebSocket to the admin server, GET /api/encoder/link
# (through Home Assistant's ingress, which admin_guard checks like any dashboard request), and
# passes each request it's sent on to SN Web Socket on its own computer. A ticket first, POST
# /api/encoder/ticket (refused like any change the dashboard itself didn't send): good once, for
# LINK_TICKET_SECONDS, for the Home Assistant user it was given to. Each admin has one link and
# one encoder address, dashboard_address(their user ID); a newer link of theirs (another tab, or
# another computer) replaces the older one. A link closes when Wristbands is turned off or its
# user is no longer an administrator. Through ingress a close code may not reach the page, so a
# {"type": "closing", "why"} message goes first, and {"type": "linked"} says it was taken.
#
# One account may be signed in on several computers (a shared front-desk login), so the page
# sends an ID of its own ("page": made once per browser tab, kept when it reloads) with its
# ticket, its card jobs and their Try again: a job runs only through the link of the page that
# started it (DashboardRelay.bound_page), and only that page may try it again.

DASHBOARD_LOCAL_USER = "local"   # the dashboard opened on this computer itself (no ingress)
NO_DASHBOARD_LINK = "Open this card dialog again to connect the card encoder on this computer."
DASHBOARD_NOT_LINKED = ("The card encoder on this computer isn't connected. Wait for it to "
                        "connect, then try again.")
DASHBOARD_WRISTBANDS_OFF = "Wristbands is turned off. Turn it on in the Wristbands tab first."
DASHBOARD_ELSEWHERE = ("Another window (or another computer signed in as you) has the card "
                       "encoder link now. Click Use it here, then try again.")
DASHBOARD_NOT_YOURS = ("This card job was started in another window or on another computer: "
                       "only that window can try it again. Stop it to start again here.")
PAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
dashboard_tickets: dict[str, tuple[str, float, str | None]] = {}   # ticket -> (user, expires at,
                                                                   # page)
recent_dashboard_tickets: dict[str, list[float]] = {}
dashboard_links: dict[str, EncoderLink] = {}           # user -> their link


def from_loopback(request: web.Request) -> bool:
    try:
        return ipaddress.ip_address(request.remote or "").is_loopback
    except ValueError:
        return False


def admin_user(request: web.Request) -> str | None:
    """Who's using the dashboard: their Home Assistant user ID (ingress sends it, and admin_guard
    has checked they're an administrator), DASHBOARD_LOCAL_USER on this computer itself, else
    None."""
    user = request.headers.get("X-Remote-User-Id", "")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user):
        return user
    return DASHBOARD_LOCAL_USER if from_loopback(request) else None


async def dashboard_link_refusal(user: str, ingress: bool) -> str | None:
    """Why an admin's dashboard link may not stay open: "not_allowed" once Wristbands is turned
    off, or (through ingress) they're no longer an administrator. None: it may. Home Assistant
    not answering leaves it open (it was checked when it opened)."""
    if db.settings().get("wristbands_enabled") != "1":
        return "not_allowed"
    if ingress:
        try:
            if not await ha.is_admin(user):
                return "not_allowed"
        except (HomeAssistantError, OSError, asyncio.TimeoutError) as err:
            log.info("Couldn't check Home Assistant user %s: %s", user, err)
    return None


def page_id(data: dict) -> str | None:
    """The dashboard page's own ID from a request body, or None (a page from before 0.9.0)."""
    page = data.get("page") if isinstance(data, dict) else None
    return page if isinstance(page, str) and PAGE_ID_RE.match(page) else None


async def optional_json(request: web.Request) -> dict:
    """A request's JSON body, or {} when it has none."""
    return await read_json(request) if request.body_exists else {}


def dashboard_tickets_too_busy(user: str) -> bool:
    now = time.time()
    times = [t for t in recent_dashboard_tickets.get(user, []) if now - t < 60]
    busy = len(times) >= LINK_TICKETS_PER_MINUTE
    if not busy:
        times.append(now)
    recent_dashboard_tickets[user] = times
    return busy


async def admin_encoder_ticket(request: web.Request):
    """A ticket to open this admin's dashboard link with: {"ticket", "expires_in", "path"}."""
    user = admin_user(request)
    if user is None:
        raise json_error(web.HTTPForbidden, NO_DASHBOARD_LINK)
    if db.settings().get("wristbands_enabled") != "1":
        return bad_request(DASHBOARD_WRISTBANDS_OFF, 409)
    if dashboard_tickets_too_busy(user):
        return bad_request("Too many requests at once. Wait a moment and try again.", 429)
    page = page_id(await optional_json(request))
    now = time.time()
    if len(dashboard_tickets) >= MAX_LINK_TICKETS:   # the oldest go first
        for key in sorted(dashboard_tickets, key=lambda k: dashboard_tickets[k][1])[
                :len(dashboard_tickets) - MAX_LINK_TICKETS + 1]:
            del dashboard_tickets[key]
    ticket = secrets.token_urlsafe(24)
    dashboard_tickets[ticket] = (user, now + LINK_TICKET_SECONDS, page)
    return web.json_response({"ticket": ticket, "expires_in": LINK_TICKET_SECONDS,
                              "path": "api/encoder/link"})


async def admin_encoder_link(request: web.Request):
    """This admin's dashboard link (GET /api/encoder/link?ticket=...): a WebSocket for as long as
    a card dialog uses This computer. Only card jobs this admin starts there use it."""
    user = admin_user(request)
    entry = dashboard_tickets.pop(request.query.get("ticket", "")[:64], None)
    if not entry or entry[1] < time.time() or entry[0] != user:
        raise json_error(web.HTTPUnauthorized, NO_DASHBOARD_LINK)
    ingress = not from_loopback(request)
    if await dashboard_link_refusal(user, ingress):
        raise json_error(web.HTTPForbidden, DASHBOARD_WRISTBANDS_OFF)
    # No compression: messages carry the hotel key, and they're small anyway.
    ws = web.WebSocketResponse(heartbeat=LINK_HEARTBEAT, max_msg_size=MAX_LINK_MESSAGE,
                               timeout=LINK_CLOSE_SECONDS, compress=False)
    if not ws.can_prepare(request).ok:
        return bad_request("Open this address as a WebSocket.")
    await ws.prepare(request)
    who = admin_name(request)

    async def check():
        refusal = await dashboard_link_refusal(user, ingress)
        if refusal:
            await say_closing(ws, refusal)
        return refusal
    relay = encoders.dashboard_relay(user)
    number = relay.attach(ws.send_json, entry[2])
    link = EncoderLink(number, ws, user, 0)
    old, dashboard_links[user] = dashboard_links.get(user), link
    if old is not None:
        background(close_dashboard_link(old, LINK_REPLACED, "replaced"), "close an encoder link")
    log.info("Card encoder link opened from the dashboard (%s)", who)
    try:
        await ws.send_json({"type": "linked"})
        await serve_link(link, relay, 0, check)
    except Exception as err:  # noqa: BLE001 - a link that breaks just closes
        log.warning("Card encoder link from the dashboard (%s) failed: %r", who, err)
    finally:
        relay.detach(number)
        if dashboard_links.get(user) is link:
            del dashboard_links[user]
        if not ws.closed:
            await ws.close()
        log.info("Card encoder link closed from the dashboard (%s)", who)
    return ws


async def say_closing(ws: web.WebSocketResponse, why: str) -> None:
    """Tell the dashboard why its link is closing (its close code may not get through ingress)."""
    try:
        await ws.send_json({"type": "closing", "why": why})
    except Exception:  # noqa: BLE001 - closing anyway
        pass


async def close_dashboard_link(link: EncoderLink, code: int, why: str) -> None:
    if not link.ws.closed:
        await say_closing(link.ws, why)
        await link.ws.close(code=code, message=why.encode())


async def close_dashboard_links(app: web.Application) -> None:
    """When the add-on stops: close every dashboard link, so stopping doesn't wait for them."""
    await asyncio.gather(*(close_dashboard_link(link, LINK_STOPPING, "stopping")
                           for link in list(dashboard_links.values())), return_exceptions=True)


def dashboard_encoder(request: web.Request, data: dict) -> dict:
    """This computer: the encoder on the computer this admin has the dashboard open on. Its link
    must be open, from this very page (data's "page": the dialog opens it before starting a
    job), and a job of theirs still unfinished there comes back ("job") to finish or stop first.
    The job about to start is bound to this page (DashboardRelay.bound_page). Returned like
    chosen_encoder."""
    user = admin_user(request)
    relay = encoders.find_dashboard_relay(user) if user else None
    if relay is None or not relay.connected:
        raise json_error(web.HTTPConflict, DASHBOARD_NOT_LINKED)
    if relay.page != page_id(data):
        raise json_error(web.HTTPConflict, DASHBOARD_ELSEWHERE)
    job = encoder_job(relay.url)
    if job:
        raise json_error(web.HTTPConflict, "A card job on this computer isn't finished yet. "
                         "Finish it or stop it first.", job=job_view(job, None))
    relay.bound_page = relay.page
    return {"id": None, "name": "This computer", "encoder_url": relay.url, "encoder_port": None}


def dashboard_job_refusal(job: dict, data: dict) -> str | None:
    """Why this page may not try a card job on a dashboard's own encoder again (or skip its
    card), or None: only the page that started it may, while its link is there."""
    user = dashboard_user(job["encoder_url"])
    if user is None:
        return None
    relay = encoders.find_dashboard_relay(user)
    if relay is None or relay.bound_page != page_id(data):
        return DASHBOARD_NOT_YOURS
    if relay.connected and relay.page != relay.bound_page:
        return DASHBOARD_ELSEWHERE
    return None


STAFF_SECURITY_HEADERS = {
    "Content-Security-Policy":
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        f"connect-src 'self' {LOCAL_HELPERS}; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'none'",
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


# ---------------------------------------------------------------- tasks (staff app)
# Each department's to-do list (tasks.py has the rules). Only someone signed in with their own
# PIN has tasks; every request checks who they are, their departments (every one they're in that
# isn't hidden) and their role again, and nothing the page sends about them is trusted.

NO_TASKS = "Sign in with your own PIN to see tasks."
TASK_CHANGES_PER_MINUTE = 60
recent_task_changes: dict[int, list[float]] = {}


def manages_tasks(device: dict, member: dict) -> bool:
    """Whether this person may edit and delete anyone's tasks: their role's Manage tasks (on a
    kiosk device, the device's role when they have none of their own)."""
    role_id = member.get("role_id") or (None if role_based(device) else device.get("role_id"))
    role = db.role(role_id) if role_id else None
    return bool(role and role.get("manage_tasks"))


def task_caller(request: web.Request, change: bool = False) -> tuple[dict, dict, bool]:
    """(device, signed-in person, whether they manage tasks) for a Tasks request, or the error."""
    device, member = authenticate(request, active=change)
    if not member:
        raise json_error(web.HTTPForbidden, NO_TASKS)
    member = {**member, "department_ids": db.member_department_ids(member)}   # shown, in order
    if change:
        now = time.time()
        times = [t for t in recent_task_changes.get(member["id"], []) if now - t < 60]
        if len(times) >= TASK_CHANGES_PER_MINUTE:
            raise json_error(web.HTTPTooManyRequests, "Too many changes at once. Wait a moment.")
        recent_task_changes[member["id"]] = [*times, now]
    return device, member, manages_tasks(device, member)


def task_departments() -> dict[int, str]:
    """The departments tasks can be for: the shown ones (a hidden one's tasks are seen as if it
    were gone: by the person given it, its maker and managers, to move it on)."""
    return {d["id"]: d["name"] for d in db.shown_departments()}


def task_people() -> dict[int, dict]:
    """People who may be given tasks: everyone not turned off or archived, with their shown
    departments."""
    ordered = db.departments()
    return {r["id"]: {"id": r["id"], "name": r["name"],
                      "department_ids": db.member_department_ids(dict(r), ordered)}
            for r in db.conn.execute(f"SELECT id, name, department_ids FROM staff WHERE {STAFF_ON} "
                                     "ORDER BY name COLLATE NOCASE")}


def task_view(t: dict, member: dict, manager: bool, departments: dict[int, str]) -> dict:
    """A task as the staff app shows it. Text is sent as text; the page never treats it as HTML."""
    dept = t["department_id"]
    return {"id": t["id"], "title": t["title"], "notes": t["notes"],
            "department_id": dept,
            "department": "Everyone" if dept is None else departments.get(dept)
            or db.department_name(dept) or "Old department",
            "assigned_id": t["assigned_id"], "assigned_name": t["assigned_name"],
            "mine": t["assigned_id"] == member["id"],
            "due_date": t["due_date"], "due_time": t["due_time"], "priority": t["priority"],
            "status": t["status"], "status_at": t["status_at"], "status_by": t["status_by"],
            "created_at": t["created_at"], "created_by": t["created_by_name"],
            "made_by_me": t["created_by_id"] == member["id"],
            "source": t["source"], "form_name": t["form_name"], "source_url": t["source_url"],
            "deleted_at": t["deleted_at"], "deleted_by": t["deleted_by"],
            "can_edit": tasks.can_edit(t, member, manager, departments) and not ctsync.recurring(t),
            "ct_recurring": ctsync.recurring(t),
            # The Connecteam sync: in Connecteam (as a Quick Task, or a followed submission),
            # or why not; a form's photos (fetched through /photos/) and manager notes.
            "connecteam": bool(t["ct_task_id"]) or bool(t["source"] == "form" and t["ct_status"] is not None),
            "ct_note": t["ct_note"], "manager_note": t["manager_note"],
            "photos": [p.get("name") or "" for p in json.loads(t["photos"] or "[]")]}


def visible_task(task_id: str, member: dict, manager: bool) -> dict:
    """The task, if this person may see it (else Not found, the same as a task that isn't)."""
    task = db.task(int(task_id))
    if not task or not tasks.can_see(task, member, manager, task_departments()):
        raise json_error(web.HTTPNotFound, "That task isn't there any more.")
    return task


async def api_tasks(request: web.Request):
    """Everything on the Tasks screen. ?v= the version the page has: unchanged, it gets just
    that back, so a poll costs almost nothing."""
    device, member, manager = task_caller(request)
    departments = task_departments()
    since = time.time() - tasks.LIST_FINISHED_DAYS * 86400
    body = {"tasks": [task_view(t, member, manager, departments)
                      for t in db.tasks_for(member, since, manager)
                      if tasks.can_see(t, member, manager, departments)],
            "departments": [{"id": k, "name": v} for k, v in departments.items()],
            "me": {"id": member["id"], "name": member["name"],
                   "department_ids": [i for i in member["department_ids"] if i in departments],
                   "manager": manager},
            "bin": len(db.tasks_in_bin(member, manager))}
    version = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    if request.query.get("v") == version:
        return web.json_response({"v": version, "unchanged": True})
    return web.json_response({**body, "v": version})


async def api_task_people(request: web.Request):
    """Who a task can be given to (names and departments only), for the task form."""
    task_caller(request)
    return web.json_response({"people": list(task_people().values())})


async def api_task(request: web.Request):
    device, member, manager = task_caller(request)
    task = db.task(int(request.match_info["id"]))
    live = task_departments()
    if not task or not (tasks.can_see(task, member, manager, live)
                        or tasks.can_restore(task, member, manager, live)):
        raise json_error(web.HTTPNotFound, "That task isn't there any more.")
    return web.json_response({"task": task_view(task, member, manager, task_departments()),
                              "history": db.task_history(task["id"])})


async def api_add_task(request: web.Request):
    device, member, manager = task_caller(request, change=True)
    data = await read_json(request)
    if "department_id" not in data:   # their first (none: Everyone)
        data["department_id"] = next(iter(member["department_ids"]), None)
    try:
        fields = tasks.clean_task(data, task_departments(),
                                  {k: p["name"] for k, p in task_people().items()})
    except tasks.TaskError as err:
        return bad_request(str(err))
    task_id = db.add_task(fields, member, time.time())
    ctsync.touch(task_id, new=True)
    db.log(actor_name(device, member), "Added a task", role=role_label(device, member),
           detail=fields["title"])
    return web.json_response({"task": task_view(db.task(task_id), member, manager,
                                                task_departments())})


async def api_edit_task(request: web.Request):
    device, member, manager = task_caller(request, change=True)
    task = visible_task(request.match_info["id"], member, manager)
    if ctsync.recurring(task):
        return bad_request(ctsync.RECURRING, 409)
    if not tasks.can_edit(task, member, manager, task_departments()):
        return bad_request("Only the person who made this task, or a manager, can change it.", 403)
    data = await read_json(request)
    try:
        fields = tasks.clean_task(data, task_departments(),
                                  {k: p["name"] for k, p in task_people().items()}, task)
    except tasks.TaskError as err:
        return bad_request(str(err))
    db.edit_task(task["id"], fields, member, time.time())
    ctsync.touch(task["id"])
    db.log(actor_name(device, member), "Changed a task", role=role_label(device, member),
           detail=fields["title"])
    return web.json_response({"task": task_view(db.task(task["id"]), member, manager,
                                                task_departments())})


async def api_task_status(request: web.Request):
    """Anyone who sees a task may change its status; it's saved at once, in its history."""
    device, member, manager = task_caller(request, change=True)
    task = visible_task(request.match_info["id"], member, manager)
    if ctsync.recurring(task):
        return bad_request(ctsync.RECURRING, 409, task=task_view(task, member, manager, task_departments()))
    data = await read_json(request)
    try:
        status = tasks.clean_status(data, task)
    except tasks.TaskError as err:
        return bad_request(str(err), 409, task=task_view(task, member, manager, task_departments()))
    if db.set_task_status(task["id"], status, member, time.time()):
        ctsync.touch(task["id"])
    return web.json_response({"task": task_view(db.task(task["id"]), member, manager,
                                                task_departments())})


async def api_delete_task(request: web.Request):
    device, member, manager = task_caller(request, change=True)
    task = visible_task(request.match_info["id"], member, manager)
    if ctsync.recurring(task):
        return bad_request(ctsync.RECURRING, 409)
    if not tasks.can_edit(task, member, manager, task_departments()):
        return bad_request("Only the person who made this task, or a manager, can delete it.", 403)
    db.bin_task(task["id"], member, time.time())
    ctsync.touch(task["id"])
    db.log(actor_name(device, member), "Deleted a task", role=role_label(device, member),
           detail=task["title"])
    return web.json_response({"ok": True})


def photo_dir() -> str:
    return os.path.join(os.path.dirname(db.path), "task-photos")


async def api_task_photo(request: web.Request):
    """One of a task's photos, for someone who may see the task: fetched from Connecteam's own
    hosts on the server (ctphotos), as a smaller copy kept a while, so the page needs no other
    site (the staff app's CSP allows images from itself only)."""
    device, member, manager = task_caller(request)
    task = visible_task(request.match_info["id"], member, manager)
    try:
        body = await ctphotos.photo(task, int(request.match_info["n"]), photo_dir())
    except ctphotos.Unavailable as err:
        return bad_request(str(err), 404)
    return web.Response(body=body, content_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


async def api_task_bin(request: web.Request):
    device, member, manager = task_caller(request)
    departments = task_departments()
    return web.json_response({"tasks": [task_view(t, member, manager, departments)
                                        for t in db.tasks_in_bin(member, manager)],
                              "days": tasks.BIN_DAYS})


async def api_restore_task(request: web.Request):
    device, member, manager = task_caller(request, change=True)
    task = db.task(int(request.match_info["id"]))
    if not task or not tasks.can_restore(task, member, manager, task_departments()):
        raise json_error(web.HTTPNotFound, "That task isn't in the bin any more.")
    db.bin_task(task["id"], member, time.time(), restore=True)
    ctsync.touch(task["id"])
    db.log(actor_name(device, member), "Put back a task", role=role_label(device, member),
           detail=task["title"])
    return web.json_response({"ok": True})


# Tasks from Connecteam forms: every form chosen on the dashboard is checked every 10 minutes, or
# less often as more are chosen (tasks.form_interval), through ctdata in the reports lane of the
# Connecteam budget. Nothing is asked while no form is chosen.
tasks_purged = [0.0]


async def check_task_forms(force: bool = False) -> int:
    """Turn new submissions of the chosen forms into tasks. Returns how many were made."""
    now = time.time()
    chosen = db.task_forms()
    weight = ctsync.poll_weight(chosen)
    forms = [f for f in chosen if force or tasks.form_due(f, now, weight)]
    if not forms:
        return 0
    if not connecteam_settings()[0]:
        for form in forms:
            db.task_form_checked(form["form_id"], now, "Connecteam is turned off in Settings.")
        return 0
    if not db.settings().get("wb_time_zone"):
        await ha_time_zone()   # so report_zone() knows Home Assistant's zone
    zone = report_zone()   # the zone ctdata gives times in
    staff_by_ct = {r["connecteam_user_id"]: dict(r) for r in db.conn.execute(
        f"SELECT id, name, connecteam_user_id FROM staff WHERE connecteam_user_id IS NOT NULL "
        f"AND {STAFF_ON}")}
    made = 0
    for form in forms:
        start, end = tasks.form_range(form, now, zone)
        follow = tasks.follow_of(form)
        if follow:   # a followed form reads back FORM_LOOKBACK_DAYS, a month at a time, afresh
            start = datetime.fromtimestamp(now - tasks.FORM_LOOKBACK_DAYS * 86400, zone).date()
            ctdata.forget_submissions(form["form_id"])
        read_at = time.time()
        try:
            subs = await ctdata.form_submissions(form["form_id"], start, end)
        except (ctdata.Unavailable, ValueError) as err:
            db.task_form_checked(form["form_id"], time.time(), str(err)[:300])
            continue
        form = db.task_form(form["form_id"])   # it may have been changed or removed meanwhile
        if not form:
            continue
        if follow and tasks.follow_of(form):
            made += await ctsync.follow_submissions(form, subs, staff_by_ct, zone, time.time(), read_at)
            db.task_form_checked(form["form_id"], time.time(), None)
            continue
        ids = [str(sub.get("id"))[:80] for sub in subs
               if isinstance(sub, dict) and sub.get("id") not in (None, "")]
        seen, marked, new = db.task_seen(form["form_id"], ids), [], []
        for sub in subs:
            sid = str(sub.get("id"))[:80] if isinstance(sub, dict) and sub.get("id") not in (None, "") else ""
            if not sid or sid in seen or sid in marked:
                continue
            marked.append(sid)
            # ctdata gives times to the minute: one sent in the minute it was chosen counts.
            when = tasks.submitted_time(sub.get("submitted_at"), zone)
            if when is not None and when >= form["picked_at"] - 60:
                new.append(tasks.task_from_submission(form, sub, staff_by_ct, when, zone))
        made += db.add_form_tasks(form["form_id"], marked, new, time.time())
        db.task_form_checked(form["form_id"], time.time(), None)
    return made


async def tasks_loop() -> None:
    """Background loop: forms that are due, the Connecteam task sync, and once an hour the bin,
    old finished tasks and old photo copies."""
    async def tidy():
        if time.time() - tasks_purged[0] > 3600:
            tasks_purged[0] = time.time()
            db.purge_tasks(time.time(), tasks.BIN_DAYS, tasks.KEEP_FINISHED_DAYS)
            ctphotos.tidy(photo_dir())

    # Each step on its own: one that fails (a form Connecteam answers oddly) never stops the
    # others, so changes made here are still sent.
    steps = (("Tasks: tidying up", tidy), ("Tasks from forms", check_task_forms),
             ("Task sync: sending changes", ctsync.push_pending),
             ("Task sync: reading boards", lambda: ctsync.poll_due(ctsync.poll_weight(db.task_forms()))))
    ctsync.retry_refused()
    while True:
        for what, step in steps:
            try:
                await step()
            except Exception:  # noqa: BLE001 - the loop must keep running
                log.exception("%s failed", what)
        await asyncio.sleep(60)


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
    app.router.add_get("/api/wristbands", api_wristbands)
    app.router.add_post("/api/wristbands/preview", api_preview_wristbands)
    app.router.add_post("/api/wristbands/jobs", api_make_wristbands)
    app.router.add_get(r"/api/wristbands/jobs/{id:\d{1,12}}", api_wristband_job)
    app.router.add_post(r"/api/wristbands/jobs/{id:\d{1,12}}/{action:retry|skip|cancel}",
                        api_wristband_job_action)
    app.router.add_get("/api/wristbands/log", api_wristband_log)
    app.router.add_post("/api/wristbands/lookup", api_start_lookup)
    app.router.add_post("/api/wristbands/encoder/reconnect", api_reconnect_encoder)
    app.router.add_get("/api/wristbands/lookup", api_lookup)
    app.router.add_post(r"/api/wristbands/cards/{id:\d{1,12}}/revoke", api_revoke_card)
    app.router.add_post("/api/wristbands/lookup/revoke", api_lookup_revoke)
    app.router.add_post("/api/encoder/ticket", api_encoder_ticket)
    app.router.add_get("/api/encoder/link", api_encoder_link)
    app.router.add_get("/api/reports", api_reports)
    app.router.add_get(r"/api/reports/{key:[a-z][a-z0-9_]{0,39}}", api_report_data)
    app.router.add_post(r"/api/reports/{key:[a-z][a-z0-9_]{0,39}}/uploads/{upload:[a-z][a-z0-9_]{0,39}}",
                        api_report_upload)
    app.router.add_put(r"/api/reports/{key:[a-z][a-z0-9_]{0,39}}/settings", api_report_settings)
    app.router.add_get(r"/reports/{key:[a-z][a-z0-9_]{0,39}}/{file:view\.js|view\.css}", report_file)
    app.router.add_get("/api/tasks", api_tasks)
    app.router.add_post("/api/tasks", api_add_task)
    app.router.add_get("/api/tasks/people", api_task_people)
    app.router.add_get("/api/tasks/bin", api_task_bin)
    app.router.add_get(r"/api/tasks/{id:\d{1,12}}", api_task)
    app.router.add_put(r"/api/tasks/{id:\d{1,12}}", api_edit_task)
    app.router.add_delete(r"/api/tasks/{id:\d{1,12}}", api_delete_task)
    app.router.add_post(r"/api/tasks/{id:\d{1,12}}/status", api_task_status)
    app.router.add_post(r"/api/tasks/{id:\d{1,12}}/restore", api_restore_task)
    app.router.add_get(r"/api/tasks/{id:\d{1,12}}/photos/{n:\d{1,2}}", api_task_photo)
    # TT Hotel's lock records (door activity): the secret address is the only way in.
    app.router.add_post(r"/tthotel/records/{token:[A-Za-z0-9_-]{1,100}}", tthotel_records)
    app.on_shutdown.append(close_all_links)
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
    return page_choice(device.get("layout_pages"))


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
        d["encoder_mode"] = encoder_mode(d)
        d["encoder_address"] = encoder_address(d, rescan=False)   # the one it uses now
        d["encoder_text"] = encoder_text(d)
        d["encoder_link"] = encoder_link(d)   # This computer (USB): its browser link
        d["wristbands_note"] = wristbands_note(d)
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
    # connect-src: SN Web Socket on the admin's own computer too (This computer, in the card
    # dialogs); 'self' takes the dashboard's own link through ingress (same-origin ws or wss).
    csp = (f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'nonce-{nonce}'; "
           f"img-src 'self' data:; connect-src 'self' {LOCAL_HELPERS}; object-src 'none'; "
           "base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
    return web.Response(text=html, content_type="text/html", headers={
        "Cache-Control": "no-store", "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin"})


def departments_view(ordered: list[dict] | None = None) -> list[dict]:
    """Departments for the dashboard, with their smart group's name and how many people
    Connecteam has in it, when known (from the last smart group list read; this never asks
    Connecteam)."""
    groups = connecteam.known_groups()
    ids = layout_page_ids()
    return [{**d, "smart_group_name": (groups.get(d["smart_group_id"]) or {}).get("name"),
             "smart_group_users": (groups.get(d["smart_group_id"]) or {}).get("users"),
             # Its start page was a layout page that's been deleted: it's left alone now.
             "start_page_removed": bool(d["start_page"]) and not start_page_alive(d["start_page"], ids)}
            for d in (db.departments() if ordered is None else ordered)]


def clock_in_view(m: dict, rules: dict) -> dict:
    """A person's clock-in rule for the Staff tab: whether they must be clocked in (clock_in),
    where that comes from (clock_in_from: person, department or default; clock_in_department:
    the department it comes from) and, if so, whether they're on the clock now (clock)."""
    required, source = db.clock_in_rule(m, rules)
    return {"clock_in": required, "clock_in_from": source,
            "clock_in_department": db.clock_in_department(m, rules),
            "clock": connecteam.status(m["connecteam_user_id"]) if needs_clock_in(m, rules) else None}


def gives_view(m: dict, ordered: list[dict]) -> dict:
    """Which of a person's departments gives them their role (when it's the role they have) and
    their key card (None: none does), for the Staff tab."""
    role = db.giving_department(m, "role_id", ordered)
    card = db.giving_department(m, "card_access", ordered)
    return {"role_department": role["id"] if role and role["role_id"] == m["role_id"] else None,
            "card_department": card and card["id"]}


async def admin_state(request: web.Request):
    owners = db.phone_order_owners()
    key_counts = db.active_key_card_counts()
    rules = db.department_clock_ins()
    ordered = db.departments()
    return web.json_response({
        "settings": public_settings(),
        "connecteam": connecteam_state(),
        "hr_google": google_view(),
        "wristbands": await wristbands_state(),
        "tunnel_sources": tunnel_sources(),
        "roles": db.roles(),
        "departments": departments_view(ordered),
        "staff_sync": staff_sync_state(),
        "key_card_totals": db.key_card_totals(),
        "devices": device_payload(),
        "staff": [{**m, "custom_order": f"s:{m['id']}" in owners,
                   "locked": m["status"] == "locked" or (m["locked_until"] or 0) > time.time(),
                   **clock_in_view(m, rules), **gives_view(m, ordered),
                   "key_cards": key_counts.get(m["id"], 0)}
                  for m in db.staff_list()],
        "areas": db.areas(),
        "layout": db.layout(),
        "has_logo": branding.has_logo(),
        # Why a migration file would be refused here (None on a fresh install), so the Migrate
        # dialog says so early, by the add-on's own rule.
        "not_fresh": not_fresh(),
        "hr": hr_brief(),
    })


def public_settings() -> dict:
    """Settings as the dashboard may see them: secrets are only ever "set or not", and request
    counts and TT Hotel's sign-in aren't shown at all."""
    s = db.settings()
    s["connecteam_secret_set"] = "1" if s.get("connecteam_client_secret") else "0"
    s["tthotel_client_secret_set"] = "1" if s.get("tthotel_client_secret") else "0"
    s["tthotel_password_md5_set"] = "1" if s.get("tthotel_password_md5") else "0"
    for key in PRIVATE_SETTINGS:
        s.pop(key, None)
    return s


def connecteam_state() -> dict:
    now = time.time()
    return {"enabled": connecteam_settings()[0], "checked_at": connecteam.checked_at,
            "last_error": connecteam.last_error, "clocked_in": len(connecteam.clocked_in),
            "halted": connecteam.stop_reason(), "retry_at": connecteam.retry_at(),
            "needed": connecteam_wanted(), "hr": connecteam.hr_status(),
            "paused": connecteam.pause_reason if connecteam.paused_until > now else None,
            "paused_until": connecteam.paused_until if connecteam.paused_until > now else None,
            "interval": connecteam.interval(), "used_today": connecteam.usage()["count"],
            "daily_cap": connecteam.daily_cap(),
            "account_left": connecteam.limits.get("day_left"),
            "account_limit": connecteam.limits.get("day_limit")}


async def admin_connecteam_users(request: web.Request):
    try:
        users = await connecteam.users()
    except ConnecteamError as err:
        return bad_request(str(err), 502)
    linked = {r["connecteam_user_id"]: r for r in db.conn.execute(
        "SELECT id, name, status, connecteam_user_id FROM staff WHERE connecteam_user_id IS NOT NULL")}
    # Kiosk codes stay on the server: the dashboard only learns whether there is one.
    return web.json_response({"users": [
        {"id": u["id"], "name": u["name"], "has_kiosk_code": bool(u["kiosk_code"]),
         "linked_to": linked[u["id"]]["name"] if u["id"] in linked else None,
         "linked_id": linked[u["id"]]["id"] if u["id"] in linked else None,
         "linked_archived": u["id"] in linked and linked[u["id"]]["status"] == "archived"}
        for u in users]})


connecteam_tested = [0.0]


async def admin_connecteam_test(request: web.Request):
    if not connecteam_settings()[0]:
        return bad_request("Turn on Connecteam and save the settings first.")
    if time.time() - connecteam_tested[0] < 10:
        return bad_request("Wait a few seconds before testing again.", 429)
    connecteam_tested[0] = time.time()
    checks = await connecteam.diagnose()   # each step in Connecteam's own words; lifts a stop
    failed = [c for c in checks if not c["ok"]]
    db.log(admin_name(request), "Tested the Connecteam connection", ok=not failed,
           detail="; ".join(f"{c['check']}: {c['detail']}" for c in failed) or None)
    return web.json_response({"ok": not failed, "checks": checks})


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


# ---------------------------------------------------------------- Link existing staff
# People added by hand (before Connecteam was connected, say) linked to their Connecteam users
# in one go: suggestions by name (ctlink), checked and confirmed by the owner. Relink everyone
# offers the same for people already linked. Uses the cached user list, like Add from
# Connecteam. Linking never changes a PIN or role, and never turns on a clock-in requirement
# unless asked; their departments follow their smart groups unless the owner keeps theirs.

def cached_user_name(uid: str | None) -> str | None:
    """A Connecteam user's name from the last user list read (never asks Connecteam)."""
    found = connecteam._users
    return next((u["name"] for u in found[0] if u["id"] == uid), None) if uid and found else None


def link_plan(users: list[dict]) -> dict:
    """What Link existing staff shows: the active Connecteam users, and each person here (not
    archived) with their link and the suggested one, for linking the unlinked ("suggested", among
    users no one is linked to) and for Relink everyone ("relink", among all users but those of
    archived people)."""
    staff = [dict(r) for r in db.conn.execute(
        "SELECT id, name, status, connecteam_user_id, connecteam_auto, department_ids, "
        "department_set FROM staff ORDER BY name COLLATE NOCASE")]
    by_id = {u["id"]: u for u in users}
    owner = {m["connecteam_user_id"]: m for m in staff if m["connecteam_user_id"]}
    here = [m for m in staff if m["status"] != "archived"]
    free = [u for u in users if u["id"] not in owner]
    suggested = ctlink.suggest([m for m in here if not m["connecteam_user_id"]], free)
    relink = ctlink.suggest(here, [u for u in users if owner.get(u["id"], {}).get("status")
                                   != "archived"])
    ordered = db.departments()

    def pick(found):
        return {"id": found[0]["id"], "name": found[0]["name"],
                "why": ctlink.WHY[found[1]]} if found else None
    return {
        "users": [{"id": u["id"], "name": u["name"], "has_kiosk_code": bool(u["kiosk_code"]),
                   "linked_to": owner[u["id"]]["id"] if u["id"] in owner else None,
                   "linked_archived": owner.get(u["id"], {}).get("status") == "archived",
                   "department_ids": [d["id"] for d in smart_group_departments(u, ordered)]}
                  for u in users],
        "staff": [{"id": m["id"], "name": m["name"], "connecteam_user_id": m["connecteam_user_id"],
                   "link_name": by_id[m["connecteam_user_id"]]["name"]
                   if m["connecteam_user_id"] in by_id else None,
                   # Linked to someone Connecteam no longer lists (archived or removed there).
                   "broken": bool(m["connecteam_user_id"]) and m["connecteam_user_id"] not in by_id,
                   "auto": bool(m["connecteam_auto"]), "suggested": pick(suggested.get(m["id"])),
                   "relink": pick(relink.get(m["id"]))} for m in here],
        # People the sync added stay linked to their own user while it runs.
        "auto_locked": staff_sync_enabled(),
    }


async def admin_connecteam_links(request: web.Request):
    if not connecteam_settings()[0]:
        return bad_request("Turn on Connecteam and save the settings first.")
    try:
        users = await connecteam.users()
    except ConnecteamError as err:
        return bad_request(str(err), 502)
    return web.json_response(link_plan(users))


def link_log_text(name: str, old: str | None, new: str | None) -> tuple[str, str]:
    """The Activity line for one person's Connecteam link changing (names or IDs)."""
    if not old:
        return f"Linked {name} to Connecteam", f"Connecteam user {new}"
    if not new:
        return f"Unlinked {name} from Connecteam", f"Was Connecteam user {old}"
    return f"Changed {name}'s Connecteam link", f"From {old} to {new}"


def link_fields(member: dict, uid: str | None, user: dict | None, *, follow_department: bool,
                clock_in: bool, departments: list[dict], rules: dict) -> dict:
    """The changes linking (or unlinking) one person makes, apart from their PIN. Newly linked:
    their smart groups' departments (follow_department, else the ones they have are kept as set
    by hand), and a clock-in requirement only if asked (clock_in): it never just switches on."""
    fields = {"connecteam_user_id": uid}
    if member["connecteam_auto"]:
        fields["connecteam_auto"] = 0   # from now on, a person added by hand
    if not uid:
        return {**fields, "pin_from_connecteam": 0, "department_set": 0}
    if member["connecteam_user_id"]:
        return fields   # relinked: their departments and clock-in rule stay as they were
    if follow_department and user:
        ids = followed_departments(member, user, departments)
        if set(ids) != set(department_list(member["department_ids"])):
            fields["department_ids"] = ids
    fields["department_set"] = 0 if follow_department else 1
    after = {**member, **fields}
    if db.clock_in_rule(after, rules)[0] != clock_in:
        fields["require_clock_in"] = 1 if clock_in else 0
        if after.get("clock_in_set") or db.clock_in_department(after, rules) is not None:
            fields["clock_in_set"] = 1   # theirs, beating their departments'
    return fields


async def admin_save_links(request: web.Request):
    """Link selected: {"links": [{"staff_id", "connecteam_user_id" (None: unlink)}],
    "use_kiosk_code", "follow_department", "require_clock_in"}. All or nothing, in one
    transaction, so two people's links can be swapped. Links already as asked are left alone."""
    if not connecteam_settings()[0]:
        return bad_request("Turn on Connecteam and save the settings first.")
    data = await read_json(request)
    rows = data.get("links")
    if not isinstance(rows, list) or not rows or len(rows) > 2000:
        return bad_request("Choose who to link.")
    wanted: dict[int, str | None] = {}
    for row in rows:
        if not isinstance(row, dict) or not re.fullmatch(r"\d{1,12}", str(row.get("staff_id"))):
            return bad_request("links must be a list of staff and Connecteam users.")
        uid = row.get("connecteam_user_id")
        uid = None if uid in (None, "") else str(uid).strip()
        if uid is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", uid):
            return bad_request("That Connecteam user ID doesn't look right.")
        if int(row["staff_id"]) in wanted:
            return bad_request("Each person can only be linked once.")
        wanted[int(row["staff_id"])] = uid
    flag = lambda key: data.get(key) in (True, 1, "1", "on")
    use_kiosk, follow, clock_in = (flag("use_kiosk_code"), data.get("follow_department", True)
                                   not in (False, 0, "0"), flag("require_clock_in"))
    try:
        users = await connecteam.users()   # cached: the list the dashboard just showed
    except ConnecteamError as err:
        return bad_request(str(err), 502)
    by_id = {u["id"]: u for u in users}
    async with staff_sync_lock:   # the sync never adds or moves anyone meanwhile
        def check() -> tuple[dict, list]:
            """Who changes, checked against the database as it is now."""
            members = {}
            for staff_id, uid in wanted.items():
                m = db.staff(staff_id)
                if not m:
                    raise web.HTTPBadRequest(text="Someone in the list was deleted meanwhile. "
                                                  "Open Link existing staff again.")
                if m["status"] == "archived":
                    raise web.HTTPBadRequest(text=f"{m['name']} is archived. Restore them first.")
                if uid == m["connecteam_user_id"]:
                    continue   # already so
                if uid and uid not in by_id:
                    raise web.HTTPBadRequest(text=f"The Connecteam user chosen for {m['name']} "
                                                  "isn't an active Connecteam user.")
                if m["connecteam_auto"] and staff_sync_enabled():
                    raise web.HTTPBadRequest(text=f"{m['name']} was added from Connecteam, so they "
                                                  "stay linked while Add everyone from "
                                                  "Connecteam is on.")
                members[staff_id] = m
            # One Connecteam user, one person, after every change (archived people included).
            after = {r["id"]: r["connecteam_user_id"] for r in db.conn.execute(
                "SELECT id, connecteam_user_id FROM staff WHERE connecteam_user_id IS NOT NULL")}
            after.update({sid: wanted[sid] for sid in members})
            holders = collections.Counter(uid for uid in after.values() if uid)
            for staff_id in members:
                uid = wanted[staff_id]
                if uid and holders[uid] > 1:
                    other = next(sid for sid, u in after.items() if u == uid and sid != staff_id)
                    raise web.HTTPBadRequest(
                        text=f"{by_id[uid]['name']} would be linked to both {members[staff_id]['name']} "
                             f"and {(db.staff(other) or {}).get('name', 'someone else')}. "
                             "One Connecteam user can be linked to one person.")
            return members, [sid for sid in members if wanted[sid]
                             and not members[sid]["connecteam_user_id"]]
        try:
            members, newly = check()
        except web.HTTPBadRequest as err:
            return bad_request(err.text)
        if not members:
            return web.json_response({"changed": 0, "pins": 0, "pins_kept": []})
        # Kiosk codes as PINs (newly linked people only): one each, used by no one else.
        pins, kept = {}, []
        if use_kiosk:
            in_use = collections.Counter(u["kiosk_code"] for u in users if u["kiosk_code"])
            codes = {sid: by_id[wanted[sid]]["kiosk_code"] for sid in newly}
            stored = db.staff_pins()

            def owners() -> dict:
                return {c: {sid for sid, h in stored if check_pin(c, h)}
                        for c in set(filter(None, codes.values()))}
            found = await asyncio.to_thread(owners)
            for sid, code in codes.items():
                if code and in_use[code] == 1 and not (found[code] - {sid}):
                    pins[sid] = code
                else:
                    kept.append(members[sid]["name"])
            hashes = dict(zip(pins, await asyncio.to_thread(
                lambda: [hash_pin(c) for c in pins.values()])))
            # PINs given meanwhile may be one of these codes.
            if any(pair not in stored for pair in db.staff_pins()):
                return bad_request("Staff PINs changed while linking. Try again.", 409)
        try:
            members, newly = check()   # nothing is awaited from here on
        except web.HTTPBadRequest as err:
            return bad_request(err.text)
        departments, rules = db.departments(), db.department_clock_ins()
        changes, logs = [], []
        for staff_id, m in members.items():
            uid = wanted[staff_id]
            fields = link_fields(m, uid, by_id.get(uid), follow_department=follow,
                                 clock_in=clock_in, departments=departments, rules=rules)
            extra = []
            if staff_id in pins and staff_id in newly:
                fields.update(pin_hash=hashes[staff_id], pin_length=len(pins[staff_id]),
                              pin_from_connecteam=1, failed_pins=0)
                extra.append("PIN set to their kiosk code")
            if "department_ids" in fields:
                extra.append("departments: " + (department_names(fields["department_ids"], departments)
                                                or "none"))
            if "require_clock_in" in fields:
                extra.append("must be clocked in to sign in" if fields["require_clock_in"]
                             else "may sign in without clocking in")
            changes.append((staff_id, fields))
            action, detail = link_log_text(
                m["name"], m["connecteam_user_id"] and (cached_user_name(m["connecteam_user_id"])
                                                        or f"{m['connecteam_user_id']} (no longer in Connecteam)"),
                uid and by_id[uid]["name"])
            logs.append((action, "; ".join([detail, *extra])))
        db.update_staff_many(changes)
    for action, detail in logs:
        db.log(admin_name(request), action, detail=detail)
    for staff_id, fields in changes:
        if "pin_hash" in fields:
            staff_sessions_end(staff_id)
    return web.json_response({"changed": len(changes), "pins": len(pins), "pins_kept": kept})


# ---------------------------------------------------------------- Connecteam staff sync
# With "Add everyone from Connecteam" on, every active Connecteam user becomes a person here,
# in every department whose smart group they're in. It uses Connecteam's cached user list (one
# request, at most hourly, inside the same budget as the clock checks) and never runs while
# Connecteam is stopped or paused. People added by hand are never touched, and neither is
# anything an admin changed by hand (role, clock-in rule, PIN, turned on or off).

STAFF_SYNC_EVERY = 3600     # the regular sync, while it's on
STAFF_SYNC_GAP = 60         # Save or Sync staff now: at most this often
NO_KIOSK_CODE = "No usable kiosk code in Connecteam"
LEFT_CONNECTEAM = "Archived or removed in Connecteam"
MAX_KIOSK_MEMORY = 10_000   # people whose PIN is known to match their kiosk code
STAFF_SYNC_RECHECKS = 4     # PINs changed during a sync are checked again this often at most
staff_sync = {"at": None, "result": None, "error": None, "tried": 0.0, "soon": False}
staff_sync_wake = asyncio.Event()
staff_sync_lock = asyncio.Lock()
# staff ID -> fingerprint of (their PIN hash, kiosk code) last found to match, so the hourly sync
# doesn't re-check every PIN (each check is deliberately slow). In memory only.
kiosk_matches: dict[int, str] = {}
# (person id -> fingerprint of PIN hash and kiosk code) pairs already tried for a missing PIN
# length, so a kiosk code that isn't someone's PIN costs one slow check, not one every sync.
pin_length_tried: dict[int, str] = {}


def staff_sync_enabled() -> bool:
    """Add everyone from Connecteam is on."""
    s = db.settings()
    return s.get("connecteam_enabled") == "1" and s.get("connecteam_auto_staff") == "1"


def departments_sync_enabled() -> bool:
    """Make a department for each smart group is on."""
    s = db.settings()
    return s.get("connecteam_enabled") == "1" and s.get("connecteam_auto_departments") == "1"


def connecteam_sync_wanted() -> bool:
    """Whether the regular sync has anything to do: adding everyone, making departments, or
    keeping linked people in their smart groups' departments."""
    if staff_sync_enabled() or departments_sync_enabled():
        return True
    return connecteam_settings()[0] and db.conn.execute(
        "SELECT 1 FROM staff WHERE connecteam_user_id IS NOT NULL AND department_set = 0 "
        "AND status != 'archived' AND EXISTS (SELECT 1 FROM departments "
        "WHERE smart_group_id IS NOT NULL) LIMIT 1").fetchone() is not None


def sync_staff_soon() -> None:
    """Sync within a minute (after Save), at most every STAFF_SYNC_GAP seconds."""
    staff_sync["soon"] = True
    staff_sync_wake.set()


def staff_sync_state() -> dict:
    return {"enabled": staff_sync_enabled(), "departments": departments_sync_enabled(),
            "running": connecteam_sync_wanted(), "synced_at": staff_sync["at"],
            "result": staff_sync["result"], "error": staff_sync["error"]}


def kiosk_fingerprint(pin_hash: str, code: str) -> str:
    return hashlib.sha256(f"{pin_hash}:{code}".encode()).hexdigest()


def unique_staff_name(name: str, taken: set[str], fallback: str = "Connecteam user") -> str:
    """name, or "name (2)", "name (3)"... whichever nobody here has (ignoring capitals)."""
    name = clean_name(name, 40) or fallback
    candidate, n = name, 1
    while candidate.lower() in taken:
        n += 1
        suffix = f" ({n})"
        candidate = name[:40 - len(suffix)].rstrip() + suffix
    return candidate


def summarize_sync(counts: dict) -> str:
    parts = [f"{counts['added']} added", f"{counts['updated']} updated",
             f"{counts['turned_off']} turned off", f"{counts['turned_on']} turned on",
             f"{counts['no_code']} without a kiosk code",
             f"{counts.get('waiting', 0)} waiting to be linked (same name as someone here)",
             plural_text(counts.get("departments_added", 0), "department made", "departments made"),
             plural_text(counts.get("departments_renamed", 0), "department renamed",
                         "departments renamed")]
    return ", ".join(p for p in parts if not p.startswith("0 ")) or "No changes"


def plural_text(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def sync_changed(counts: dict) -> bool:
    """Whether a sync changed anything (people waiting to be linked are no change)."""
    return any(v for k, v in counts.items() if k != "waiting")


async def sync_connecteam_staff(fresh: bool = False, actor: str = "Staff Portal") -> dict:
    """Bring departments and linked people in line with Connecteam: a department for each smart
    group (if that's on), the people added from Connecteam in line with its active users (if
    that's on) and everyone linked in their smart groups' departments. Returns counts {"added",
    "updated", "turned_off", "turned_on", "no_code", "waiting", "departments_added",
    "departments_renamed"}; raises ConnecteamError. fresh: Sync staff now (the user list may be
    at most a minute old). Both lists are the cached ones (users: minutes, smart groups: hours)."""
    users = await connecteam.users(max_age=STAFF_SYNC_GAP if fresh else USERS_TTL)
    if not users:
        # Never take an empty answer to mean everyone left.
        raise ConnecteamError("Connecteam listed no active users, so no one was changed.")
    groups = await connecteam.smart_groups() if departments_sync_enabled() else None
    async with staff_sync_lock:
        counts = sync_departments(groups, actor) if groups is not None else {
            "departments_added": 0, "departments_renamed": 0}
        return {**await apply_staff_sync(users, actor), **counts}


GROUP_GONE = "Smart group removed in Connecteam"


def smart_group_departments(user: dict, departments: list[dict]) -> list[dict]:
    """The departments a Connecteam user is in: every one whose smart group they're in, in the
    Staff tab's order (hidden ones too: they give nothing, and Show brings them back as they
    are)."""
    groups = set(user.get("groups") or ())
    return [d for d in departments if d["smart_group_id"] and d["smart_group_id"] in groups]


def followed_departments(member: dict, user: dict, departments: list[dict]) -> list[int]:
    """A person's departments when they follow Connecteam: their smart groups' departments, and
    the ones they're in that the sync can't know about (made by hand, with no smart group, or
    whose smart group was removed in Connecteam), kept. In the Staff tab's order. In none of the
    smart groups departments follow, they're left where they are."""
    theirs = {d["id"] for d in smart_group_departments(user, departments)}
    mine = set(department_list(member.get("department_ids")))
    if not theirs:
        return [d["id"] for d in departments if d["id"] in mine]
    return [d["id"] for d in departments if d["id"] in theirs or (
        d["id"] in mine and (not d["smart_group_id"] or d["group_gone"]))]


def department_names(ids, departments: list[dict]) -> str:
    """Departments by name, in the Staff tab's order ("" for none)."""
    mine = set(department_list(ids))
    return ", ".join(d["name"] for d in departments if d["id"] in mine)


def sync_departments(groups: list[dict], actor: str) -> dict:
    """A department for each smart group that has none, made with no role, no key card and each
    person's own clock-in rule (Connecteam's groups for everyone, BUILT_IN_GROUPS, made hidden);
    a department of the same name made by hand (with no smart group) follows the group instead.
    A hidden department still follows its group, so it's never made again. Names follow a
    renamed group unless renamed here. A group no longer listed leaves its department marked
    GROUP_GONE, never deleted. Nothing is awaited."""
    counts = {"departments_added": 0, "departments_renamed": 0}
    if not groups:
        return counts   # never take an empty list to mean every group went
    listed = {g["id"]: g for g in groups}
    depts = db.departments()
    taken = {d["name"].lower(): d for d in depts}
    followed = set()
    for d in depts:
        if not d["smart_group_id"]:
            continue
        followed.add(d["smart_group_id"])
        g = listed.get(d["smart_group_id"])
        if d["group_gone"] != (0 if g else 1):
            db.update_department(d["id"], group_gone=0 if g else 1)
            if not g:
                db.log(actor, f"Department {d['name']}: {GROUP_GONE.lower()}",
                       detail="The department and its people are kept.")
        name = clean_name(g["name"], 40) if g and d["group_named"] else ""
        if name and name != d["name"] and taken.get(name.lower(), d)["id"] == d["id"]:
            db.update_department(d["id"], name=name)
            taken.pop(d["name"].lower(), None)
            taken[name.lower()] = d
            db.log(actor, f"Renamed department {d['name']} to {name}",
                   detail="Its smart group was renamed in Connecteam")
            counts["departments_renamed"] += 1
    for g in groups:
        if g["id"] in followed:
            continue
        name = clean_name(g["name"], 40) or f"Group {g['id']}"
        same = taken.get(name.lower())
        if same and not same["smart_group_id"]:
            db.update_department(same["id"], smart_group_id=g["id"])
            db.log(actor, f"Department {same['name']} now follows the smart group {g['name']}",
                   detail="Same name as the smart group")
            continue
        name = unique_staff_name(name, set(taken), "Smart group")
        hidden = built_in_group(g["name"])
        dept_id = db.create_department(name, smart_group_id=g["id"], group_named=1,
                                       hidden=1 if hidden else 0)
        taken[name.lower()] = db.department(dept_id)
        db.log(actor, f"Made department {name} from a Connecteam smart group",
               detail="Hidden: Connecteam's group for everyone, so it gives nothing" if hidden
               else "No role, no key card; clock-in: each person's own setting")
        counts["departments_added"] += 1
    return counts


def department_move_detail(m: dict, ids: list[int], rules: dict,
                           departments: list[dict]) -> str | None:
    """What a change to someone's departments does to their clock-in rule, so it's never changed
    quietly (None: nothing)."""
    before = db.clock_in_rule(m, rules)[0]
    after_member = {**m, "department_ids": ids}
    after, source = db.clock_in_rule(after_member, rules)
    if before == after or not m.get("connecteam_user_id"):
        return None
    dept = db.clock_in_department(after_member, rules)
    name = next((d["name"] for d in departments if d["id"] == dept), "")
    return (f"now must be clocked in to sign in ({name}'s rule)" if after
            else f"no longer needs to clock in ({name}'s rule)" if source == "department"
            else "no longer needs to clock in")


def department_change_log(actor: str, m: dict, ids: list[int], rules: dict,
                          departments: list[dict]) -> None:
    db.log(actor, f"{m['name']}'s departments: {department_names(ids, departments) or 'none'}",
           detail="; ".join(filter(None, ["their Connecteam smart groups",
                                          department_move_detail(m, ids, rules, departments)])))


async def backfill_pin_lengths(users: list[dict], linked: dict) -> None:
    """Record the PIN length of linked people whose PIN is their kiosk code but whose length
    isn't known (PINs set before lengths were recorded), so the PIN pad can sign them in as soon
    as the last digit is typed. Checking PINs is slow, so each pair is tried once, off the loop."""
    todo = []
    for u in users:
        m = linked.get(u["id"])
        code = u["kiosk_code"]
        if not m or m["pin_length"] is not None or not code or pin_length(len(code)) is None:
            continue
        mark = kiosk_fingerprint(m["pin_hash"], code)
        if pin_length_tried.get(m["id"]) != mark:
            todo.append((m, code, mark))
    if not todo:
        return
    same = await asyncio.to_thread(lambda: [check_pin(code, m["pin_hash"]) for m, code, _ in todo])
    for (m, code, mark), ok in zip(todo, same):
        pin_length_tried[m["id"]] = mark
        current = db.staff(m["id"])
        if ok and current and current["pin_hash"] == m["pin_hash"] and current["pin_length"] is None:
            db.update_staff(m["id"], pin_length=len(code))


async def apply_staff_sync(users: list[dict], actor: str = "Staff Portal") -> dict:
    counts = {"added": 0, "updated": 0, "turned_off": 0, "turned_on": 0, "no_code": 0,
              "waiting": 0}
    adding = staff_sync_enabled()
    clock = 1 if db.settings().get("connecteam_auto_clock_in") == "1" else 0
    ordered = db.departments()
    linked = {r["connecteam_user_id"]: dict(r) for r in db.conn.execute(
        "SELECT * FROM staff WHERE connecteam_user_id IS NOT NULL")}
    new = [u for u in users if u["id"] not in linked] if adding else []
    # Someone here (not archived) with the same name and no link is probably them: never a
    # second person, but a suggestion in Link existing staff, for the owner to confirm.
    unlinked = [r["name"] for r in db.conn.execute(
        "SELECT name FROM staff WHERE connecteam_user_id IS NULL AND status != 'archived'")]
    waiting = [u for u in new if any(ctlink.same_name(n, u["name"]) for n in unlinked)]
    new = [u for u in new if u not in waiting]
    counts["waiting"] = len(waiting)
    # Archived people stay linked (so they're never added again) but are left alone.
    ours = [(u, linked[u["id"]]) for u in users
            if adding and u["id"] in linked and linked[u["id"]]["connecteam_auto"]
            and linked[u["id"]]["status"] != "archived"]
    await backfill_pin_lengths(users, linked)

    # Kiosk codes: whose PIN each one already is. Checking PINs is slow, so it runs off the
    # event loop, and as little as it can: someone already added is only checked when the sync
    # may still set their PIN (it came from their kiosk code, or they have none yet), against
    # their own PIN first. Everyone's PINs are checked only for codes that would be new PINs.
    may_set = [(u, m) for u, m in ours if u["kiosk_code"]
               and (m["pin_from_connecteam"] or m["connecteam_note"] == NO_KIOSK_CODE)
               and kiosk_matches.get(m["id"]) != kiosk_fingerprint(m["pin_hash"],
                                                                   u["kiosk_code"])]
    theirs = await asyncio.to_thread(
        lambda: [check_pin(u["kiosk_code"], m["pin_hash"]) for u, m in may_set])
    changing = []
    for (u, m), same in zip(may_set, theirs):
        if same:
            kiosk_matches[m["id"]] = kiosk_fingerprint(m["pin_hash"], u["kiosk_code"])
        else:
            changing.append((u, m))
    codes = {u["kiosk_code"] for u in new if u["kiosk_code"]} | {u["kiosk_code"]
                                                                  for u, _ in changing}
    in_use = collections.Counter(u["kiosk_code"] for u in users if u["kiosk_code"])
    shared = {c for c, n in in_use.items() if n > 1}   # two Connecteam users, one code
    pins = set(db.staff_pins())
    owners: dict[str, set[int]] = {c: set() for c in codes}

    async def find_owners(checking: set) -> None:
        found = await asyncio.to_thread(
            lambda: {c: {sid for sid, stored in checking if check_pin(c, stored)} for c in codes})
        for c, sids in found.items():
            owners[c] |= sids
    await find_owners(pins)

    def free(code, staff_id=None) -> bool:
        return bool(code) and code not in shared and not (owners.get(code, set()) - {staff_id})

    # New people: their kiosk code as PIN if it's theirs alone, else turned off until an admin
    # gives them one (a random PIN nobody knows keeps the record valid meanwhile).
    plans = [(u, u["kiosk_code"] if free(u["kiosk_code"]) else None) for u in new]
    taken_codes: set[str] = set()
    for i, (u, code) in enumerate(plans):
        if code in taken_codes:
            plans[i] = (u, None)
        elif code:
            taken_codes.add(code)
    hashes = await asyncio.to_thread(
        lambda: [hash_pin(code or f"{secrets.randbelow(10**8):08d}") for _, code in plans])
    changed_pins = {}
    for u, m in changing:
        code = u["kiosk_code"]
        if free(code, m["id"]) and code not in taken_codes:
            changed_pins[m["id"]] = code
            taken_codes.add(code)
    new_hashes = dict(zip(changed_pins, await asyncio.to_thread(
        lambda: [hash_pin(c) for c in changed_pins.values()])))
    if len(kiosk_matches) > MAX_KIOSK_MEMORY:
        kiosk_matches.clear()
    # A PIN given meanwhile (by an admin, while this was checking) may be one of these codes:
    # check the PINs that changed, until none did since the last check.
    for _ in range(STAFF_SYNC_RECHECKS):
        changed = set(db.staff_pins()) - pins
        if not changed:
            break
        pins |= changed
        await find_owners(changed)
    else:
        raise ConnecteamError("Staff PINs kept changing while staff were synced, so no one was "
                              "changed. It will try again.")

    # From here on, nothing is awaited: the database is read fresh and written in one go.
    taken = {r[0].lower() for r in db.conn.execute("SELECT name FROM staff")}
    for (u, code), pin_hash in zip(plans, hashes):
        if db.staff_by_connecteam(u["id"]) or (code and not free(code)):
            continue   # linked meanwhile, or their code became someone's PIN: next time
        ids = [d["id"] for d in smart_group_departments(u, ordered)]
        role = db.giving_department({"department_ids": ids}, "role_id", ordered)
        name = unique_staff_name(u["name"], taken)
        taken.add(name.lower())
        staff_id = db.conn.execute(
            """INSERT INTO staff (name, pin_hash, pin_length, status, created_at,
               connecteam_user_id, require_clock_in, connecteam_auto, pin_from_connecteam,
               connecteam_note, department_ids, role_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
            (name, pin_hash, len(code) if code else None, "active" if code else "disabled",
             time.time(), u["id"], clock,
             1 if code else 0, None if code else NO_KIOSK_CODE, json.dumps(ids),
             role and role["role_id"])).lastrowid
        if code:
            kiosk_matches[staff_id] = kiosk_fingerprint(pin_hash, code)
        counts["added"] += 1
        counts["no_code"] += 0 if code else 1
    db.conn.commit()

    signed_out = set()
    rules = db.department_clock_ins()
    for u, old in ours:
        m = db.staff(old["id"])
        if (not m or not m["connecteam_auto"] or m["connecteam_user_id"] != u["id"]
                or m["status"] == "archived"):   # archived meanwhile
            continue
        fields = {}
        # Their smart groups' departments, unless chosen by hand.
        was = department_list(m["department_ids"])
        ids = was if m["department_set"] else followed_departments(m, u, ordered)
        if set(ids) != set(was):
            fields["department_ids"] = ids
            department_change_log(actor, m, ids, rules, ordered)
        # Their role: the first of their departments that gives one, unless set by hand. None
        # does: a role that came with a department (left, hidden or no longer giving it) goes.
        role = db.giving_department({"department_ids": ids}, "role_id", ordered)
        if not m["role_set"] and role and m["role_id"] != role["role_id"]:
            fields["role_id"] = role["role_id"]
        elif (not m["role_set"] and not role and m["role_id"] is not None
              and any(d["role_id"] == m["role_id"] for d in ordered if d["id"] in {*was, *ids})):
            fields["role_id"] = None
        if not m["clock_in_set"] and m["require_clock_in"] != clock:
            fields["require_clock_in"] = clock
        if (m["id"] in new_hashes and m["pin_hash"] == old["pin_hash"]
                and free(u["kiosk_code"], m["id"])):
            fields.update(pin_hash=new_hashes[m["id"]], pin_length=len(u["kiosk_code"]),
                          pin_from_connecteam=1, failed_pins=0)
            kiosk_matches[m["id"]] = kiosk_fingerprint(new_hashes[m["id"]], u["kiosk_code"])
            signed_out.add(m["id"])
        turn_on = m["status"] == "disabled" and (
            m["connecteam_note"] == LEFT_CONNECTEAM
            or (m["connecteam_note"] == NO_KIOSK_CODE and "pin_hash" in fields))
        if turn_on:
            fields.update(status="active", connecteam_note=None)
            counts["turned_on"] += 1
        elif fields:
            counts["updated"] += 1
        if fields:
            db.update_staff(m["id"], **fields)

    # People added by hand and linked: only their departments follow their smart groups (unless
    # chosen by hand). Their role, PIN, clock-in rule and status are theirs.
    for u in users:
        m = db.staff(linked[u["id"]]["id"]) if u["id"] in linked else None
        if (not m or m["connecteam_auto"] or m["connecteam_user_id"] != u["id"]
                or m["status"] == "archived" or m["department_set"]):
            continue
        ids = followed_departments(m, u, ordered)
        if set(ids) != set(department_list(m["department_ids"])):
            db.update_staff(m["id"], department_ids=ids)
            department_change_log(actor, m, ids, rules, ordered)
            counts["updated"] += 1

    # Archived in Connecteam, or no longer listed: turned off, never deleted.
    active = {u["id"] for u in users}
    for staff_id, uid in db.conn.execute(
            "SELECT id, connecteam_user_id FROM staff WHERE connecteam_auto = 1 "
            f"AND {STAFF_ON} AND connecteam_user_id IS NOT NULL").fetchall():
        if adding and uid not in active:
            db.update_staff(staff_id, status="disabled", connecteam_note=LEFT_CONNECTEAM)
            signed_out.add(staff_id)
            counts["turned_off"] += 1
    for staff_id in signed_out:
        staff_sessions_end(staff_id)   # a new PIN, or turned off: sign in again
    return counts


async def run_staff_sync(actor: str = "Staff Portal", fresh: bool = False) -> dict:
    """Sync and record how it went (state for the dashboard, and the activity log). Raises
    ConnecteamError for the Sync staff now button; the regular sync catches it."""
    staff_sync["tried"] = time.time()
    try:
        counts = await sync_connecteam_staff(fresh, actor)
    except ConnecteamError as err:
        if actor != "Staff Portal" or staff_sync["error"] != str(err):
            db.log(actor, "Synced staff from Connecteam", detail=str(err), ok=False)
        staff_sync["error"] = str(err)
        raise
    staff_sync.update(at=time.time(), result=counts, error=None)
    # The hourly sync only writes to the log when it changed something.
    if actor != "Staff Portal" or sync_changed(counts):
        db.log(actor, "Synced staff from Connecteam", detail=summarize_sync(counts))
    return counts


def staff_sync_due() -> bool:
    """Whether the regular sync should run now: it's on, Connecteam isn't stopped or paused,
    and the last try was an hour ago (a minute, after Save)."""
    gap = STAFF_SYNC_GAP if staff_sync["soon"] else STAFF_SYNC_EVERY
    now = time.time()
    return (connecteam_sync_wanted() and not connecteam.stop_reason() and now >= connecteam.paused_until
            and now - staff_sync["tried"] >= gap)


async def staff_sync_loop() -> None:
    """Background loop: the regular sync while it's on (and soon after Save). Never raises."""
    while True:
        staff_sync_wake.clear()
        try:
            if staff_sync_due():
                staff_sync["soon"] = False
                await run_staff_sync()
        except ConnecteamError:
            pass   # kept in staff_sync["error"] for the dashboard
        except Exception as err:  # noqa: BLE001 - the loop must keep running
            log.warning("Connecteam staff sync failed: %r", err)
            staff_sync["error"] = "Something went wrong while syncing staff. It will try again."
        try:
            await asyncio.wait_for(staff_sync_wake.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass


async def admin_sync_staff(request: web.Request):
    """Sync staff now (Staff tab)."""
    if not connecteam_sync_wanted():
        return bad_request("Turn on Connecteam and Add everyone from Connecteam (or Make a "
                           "department for each smart group), and save the settings first.")
    if time.time() - staff_sync["tried"] < STAFF_SYNC_GAP:
        return bad_request("Staff were synced less than a minute ago. Wait a moment and try "
                           "again.", 429)
    try:
        counts = await run_staff_sync(admin_name(request), fresh=True)
    except ConnecteamError as err:
        return bad_request(str(err), 502)
    return web.json_response({**counts, "detail": summarize_sync(counts)})


departments_synced = [0.0]


async def admin_sync_departments(request: web.Request):
    """Add departments now: a department for each smart group, as the sync makes them."""
    if not departments_sync_enabled():
        return bad_request("Turn on Connecteam and Make a department for each smart group, and "
                           "save the settings first.")
    if time.time() - departments_synced[0] < STAFF_SYNC_GAP:
        return bad_request("Departments were added less than a minute ago. Wait a moment and "
                           "try again.", 429)
    departments_synced[0] = time.time()
    try:
        groups = await connecteam.smart_groups(max_age=STAFF_SYNC_GAP)
    except ConnecteamError as err:
        return bad_request(str(err), 502)
    if not groups:
        return bad_request("Connecteam listed no smart groups, so no department was made.")
    async with staff_sync_lock:
        counts = sync_departments(groups, admin_name(request))
    sync_staff_soon()   # people join their group's department (the user list is cached)
    return web.json_response(counts)


async def admin_smart_groups(request: web.Request):
    """Connecteam's smart groups, for choosing a department's group (cached for hours)."""
    if not connecteam_settings()[0]:
        return bad_request("Turn on Connecteam and save the settings first.")
    try:
        groups = await connecteam.smart_groups()
    except ConnecteamError as err:
        return bad_request(str(err), 502)
    return web.json_response({"groups": groups})


# ---------------------------------------------------------------- departments

def parse_departments(value, member: dict | None = None) -> list[int]:
    """Departments picked for a person (a list of IDs; one ID or none, from older pages), in
    the Staff tab's order. Hidden ones can't be picked, so the hidden ones they're in stay."""
    if value in (None, ""):
        value = []
    elif not isinstance(value, list):
        value = [value]
    ordered = db.departments()
    known = {d["id"]: d for d in ordered}
    picked = set()
    for v in value[:1000]:
        try:
            dept = known.get(int(v)) if not isinstance(v, bool) else None
        except (TypeError, ValueError, OverflowError):
            dept = None
        if not dept or dept["hidden"]:
            raise web.HTTPBadRequest(text="unknown department")
        picked.add(dept["id"])
    kept = set(department_list(member["department_ids"])) if member else set()
    return [d["id"] for d in ordered if d["id"] in picked or (d["hidden"] and d["id"] in kept)]


def clean_department(data: dict, current: dict | None) -> dict:
    """A department from the dashboard, checked: name, smart_group_id (Connecteam), role_id,
    card_access (the key card template, as a staff card's "access"; None = no key card),
    card_days and card_lockout. Fields left out keep what's saved. Raises ValueError."""
    fields = {}
    if "name" in data or current is None:
        name = clean_name(data.get("name"), 40)
        if not name:
            raise ValueError("Give the department a name.")
        if db.name_taken("departments", name, current and current["id"]):
            raise ValueError("A department with that name already exists.")
        fields["name"] = name
        if current and name != current["name"]:
            fields["group_named"] = 0   # renamed here: no longer follows its smart group's name
    if "smart_group_id" in data:
        group = transfer.smart_group_id(data["smart_group_id"])
        if data["smart_group_id"] not in (None, "") and group is None:
            raise ValueError("Choose a Connecteam smart group from the list.")
        other = next((d for d in db.departments() if group and d["smart_group_id"] == group
                      and (current is None or d["id"] != current["id"])), None)
        if other:
            raise ValueError(f"{other['name']} already follows that smart group.")
        fields["smart_group_id"] = group
        if current and group != current["smart_group_id"]:
            fields.update(group_named=0, group_gone=0)
    if "role_id" in data:
        fields["role_id"] = parse_role(data["role_id"])
    if "card_access" in data:
        fields["card_access"] = transfer.clean_card_access(data["card_access"])
        if fields["card_access"] == []:
            fields["card_access"] = None   # nothing chosen: no key card
    if "card_days" in data:
        value = data["card_days"]
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip().isdigit():
            raise ValueError("Key card days must be a whole number.")
        fields["card_days"] = transfer.card_days(int(value))
    if "card_lockout" in data:
        fields["card_lockout"] = 1 if data["card_lockout"] in (True, 1, "1", "on") else 0
    if "clock_in" in data:
        fields["clock_in"] = transfer.clock_in_rule(data["clock_in"])
    if "start_page" in data:
        fields["start_page"] = transfer.start_page(data["start_page"])
        if (fields["start_page"] or "").startswith("page:") and fields["start_page"] != (
                current or {}).get("start_page") and not start_page_alive(fields["start_page"]):
            raise ValueError("That page isn't in the layout any more. Choose another.")
    return fields


def layout_page_ids() -> set[str]:
    return {str(p.get("id")) for p in db.layout().get("pages", []) if isinstance(p, dict)}


def start_page_alive(value: str | None, page_ids: set[str] | None = None) -> bool:
    """Whether a department's start page is still there: a tool always is; a layout page until
    it's deleted (then the department leaves the start page alone)."""
    if not value or not value.startswith("page:"):
        return bool(value)
    return value[5:] in (layout_page_ids() if page_ids is None else page_ids)


def start_for(member: dict | None, phone_layout: dict | None, offered: dict) -> dict | None:
    """Where the staff app opens for this person ({"page": id} or {"tool": name}): the start
    page of the first of their departments that sets one (hidden ones and removed pages left
    out). None when none does, or it's a page or tool they can't see here: the app opens as
    it always has. offered: {tool: whether it's offered}."""
    if not member:
        return None
    ids = layout_page_ids()
    dept = db.giving_department(member, "start_page", keep=lambda v: start_page_alive(v, ids))
    if not dept:
        return None
    kind, _, name = dept["start_page"].partition(":")
    if kind == "tool":
        return {"tool": name} if offered.get(name) else None
    pages = (phone_layout or {}).get("pages") or []
    return {"page": name} if any(p.get("id") == name for p in pages) else None


# What a department's clock-in rule says, for the activity log.
DEPARTMENT_CLOCK_IN = {None: "clock-in: each person's own setting",
                       1: "clock-in: everyone must be clocked in to sign in",
                       0: "clock-in: no one needs to clock in"}


async def admin_create_department(request: web.Request):
    data = await read_json(request)
    try:
        fields = clean_department(data, None)
    except ValueError as err:
        return bad_request(str(err))
    try:
        dept_id = db.create_department(**fields)
    except sqlite3.IntegrityError:
        return bad_request("A department with that name already exists.")
    clock_in = fields.get("clock_in")
    db.log(admin_name(request), f"Created department {fields['name']}",
           detail=None if clock_in is None else DEPARTMENT_CLOCK_IN[clock_in])
    return web.json_response({"id": dept_id})


async def admin_update_department(request: web.Request):
    dept = db.department(int(request.match_info["id"]))
    if not dept:
        raise web.HTTPNotFound()
    data = await read_json(request)
    try:
        fields = clean_department(data, dept)
    except ValueError as err:
        return bad_request(str(err))
    try:
        db.update_department(dept["id"], **fields)
    except sqlite3.IntegrityError:
        return bad_request("A department with that name already exists.")
    clock_in = fields.get("clock_in", dept["clock_in"])
    db.log(admin_name(request), f"Updated department {fields.get('name', dept['name'])}",
           detail=None if clock_in == dept["clock_in"] else DEPARTMENT_CLOCK_IN[clock_in])
    if "smart_group_id" in fields or "role_id" in fields:
        if connecteam_sync_wanted():
            sync_staff_soon()   # people move in or out (the user list is cached)
    return web.json_response({"ok": True})


async def admin_reorder_departments(request: web.Request):
    data = await read_json(request)
    order = data.get("order")
    if not isinstance(order, list) or len(order) > 1000:
        return bad_request("order must be a list")
    known = {d["id"] for d in db.departments()}
    ids = [int(i) for i in order if re.fullmatch(r"\d{1,9}", str(i))]
    db.reorder_departments([i for i in dict.fromkeys(ids) if i in known])
    return web.json_response({"ok": True})


def department_forms_refusal(dept: dict) -> web.Response | None:
    """Deleting or hiding a department a task form (or a Connecteam board) sends its tasks to: they'd go nowhere, so
    another department is picked for the form first (409). None: nothing in the way."""
    forms = [f["name"] for f in db.task_forms() if f["department_id"] == dept["id"]]
    forms += [f"The Connecteam board {b['name']}" for b in ctsync.boards()
              if b["department_id"] == dept["id"]]
    if not forms:
        return None
    return bad_request(f"{', '.join(forms)} still sends its tasks to {dept['name']}. On the "
                       "Tasks tab, pick another department for it (or Everyone) first.", 409)


async def admin_delete_department(request: web.Request):
    dept = db.department(int(request.match_info["id"]))
    if not dept:
        raise web.HTTPNotFound()
    refusal = department_forms_refusal(dept)
    if refusal:
        return refusal
    db.delete_department(dept["id"])
    db.log(admin_name(request), f"Deleted department {dept['name']}")
    return web.json_response({"ok": True})


async def admin_hide_department(request: web.Request):
    """Hide (or Show) a department that follows a smart group: deleting it would only have the
    sync make it again. Hidden, it gives nothing and is offered nowhere; its people stay in it."""
    dept = db.department(int(request.match_info["id"]))
    if not dept:
        raise web.HTTPNotFound()
    hide = request.match_info["action"] == "hide"
    if hide and not dept["smart_group_id"]:
        return bad_request(f"{dept['name']} doesn't follow a smart group. Delete it instead.")
    refusal = department_forms_refusal(dept) if hide else None
    if refusal:
        return refusal
    if bool(dept["hidden"]) != hide:
        db.update_department(dept["id"], hidden=1 if hide else 0)
        people = f"{dept['staff_count']} {'person' if dept['staff_count'] == 1 else 'people'}"
        db.log(admin_name(request), f"{'Hid' if hide else 'Showed'} department {dept['name']}",
               detail=f"{people} in it; it gives nothing while hidden" if hide
               else f"{people} in it get what it sets again")
        if connecteam_sync_wanted():
            sync_staff_soon()   # roles that came with it follow (the user list is cached)
    return web.json_response({"ok": True})


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
    cleaned = clean_entity_levels(entities, ("view", "control"))
    areas = clean_area_levels(data["areas"], ("view", "control")) if isinstance(data.get("areas"), dict) else None
    try:
        pages = clean_phone_pages(data["layout_pages"]) if "layout_pages" in data else KEEP
        # None: people with this role make no wristbands.
        wristband_perms = clean_permissions(data["wristbands"]) if "wristbands" in data else KEEP
    except ValueError as err:
        return bad_request(str(err))
    if db.name_taken("roles", name, role_id):
        return bad_request("A role with that name already exists.")
    try:
        db.update_role(role_id, name, cleaned, areas)
    except sqlite3.IntegrityError:
        return bad_request("A role with that name already exists.")
    if pages is not KEEP:
        db.set_role_pages(role_id, pages)
    if wristband_perms is not KEEP:
        db.set_role_wristbands(role_id, wristband_perms)
    manage = bool(data["manage_tasks"]) if "manage_tasks" in data else KEEP
    if manage is not KEEP:
        db.set_role_manage_tasks(role_id, manage)
    detail = f"{len(cleaned)} devices" + (f", {len(areas)} areas" if areas else "")
    if wristband_perms is not KEEP and wristband_perms != role["wristbands"]:
        detail += ", wristband rules changed" if wristband_perms else ", no wristbands"
    if manage is not KEEP and manage != bool(role["manage_tasks"]):
        detail += ", manages tasks" if manage else ", doesn't manage tasks"
    db.log(admin_name(request), f"Updated role {name}", role=name, detail=detail)
    return web.json_response({"ok": True})


async def admin_role_pages(request: web.Request):
    """Only a role's pages (from the Layout tab), leaving its access as it is."""
    role = db.role(int(request.match_info["id"]))
    if not role:
        raise web.HTTPNotFound()
    data = await read_json(request)
    if "layout_pages" not in data:
        return bad_request("layout_pages is missing")
    try:
        pages = clean_phone_pages(data["layout_pages"])
    except ValueError as err:
        return bad_request(str(err))
    db.set_role_pages(role["id"], pages)
    db.log(admin_name(request), f"Changed the pages of role {role['name']}", role=role["name"])
    return web.json_response({"ok": True})


KEEP = object()   # a field left out of a request: keep what's saved
MAX_LEVELS = 5000


def clean_entity_levels(raw: dict, levels: tuple[str, ...]) -> dict[str, str]:
    """{entity_id: level} from the dashboard. Control is only kept for devices staff can
    operate (otherwise View); devices staff can't be given are dropped."""
    cleaned = {}
    for eid, lvl in list(raw.items())[:MAX_LEVELS]:
        if isinstance(eid, str) and offered(eid) and lvl in levels:
            cleaned[eid] = "view" if lvl == "control" and not controllable(eid) else lvl
    return cleaned


def clean_area_levels(raw: dict, levels: tuple[str, ...]) -> dict[int, str]:
    """{area_id: level} from the dashboard, for areas that exist."""
    known = {a["id"] for a in db.areas()}
    return {int(aid): lvl for aid, lvl in list(raw.items())[:MAX_LEVELS]
            if re.fullmatch(r"\d{1,9}", str(aid)) and int(aid) in known and lvl in levels}


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
    # A page group that's gone: phones, roles and people that showed it keep its pages, now as
    # single pages.
    for device in db.devices():
        choice = device_page_choice(device)
        kept = keep_removed_groups(choice, old, layout)
        if kept != choice:
            db.update_device(device["id"], layout_pages=json.dumps(kept))
    for role in db.roles():
        kept = keep_removed_groups(role["layout_pages"], old, layout)
        if kept != role["layout_pages"]:
            db.set_role_pages(role["id"], kept)
    for member in db.staff_list():
        kept = keep_removed_groups(member["layout_pages"], old, layout)
        if kept != member["layout_pages"]:
            db.update_staff(member["id"], layout_pages=json.dumps(kept))
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
    """A kiosk device to preview as. A role-based device shows each person their own view, so
    it's previewed by role or by person instead."""
    try:
        device = db.device(int(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return device if device and device["status"] != "revoked" and not role_based(device) else None


def preview_person(role_value, staff_value) -> dict | None:
    """A person to preview as (what they'd see on a role-based device), or a stand-in for
    someone with a role and no changes of their own."""
    try:
        if staff_value not in (None, ""):
            return db.staff(int(staff_value))
        if role_value not in (None, ""):
            role = db.role(int(role_value))
            return {"id": None, "name": None, "role_id": role["id"], "layout_pages": None} if role else None
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def preview_role_name(device: dict | None, person: dict | None) -> str:
    if device:
        role_id = device["role_id"]
    elif person:
        role_id = person.get("role_id")
    else:
        return "Everything on the layout"
    role = db.role(role_id) if role_id else None
    return role["name"] if role else "No role assigned"


async def admin_preview_me(request: web.Request):
    s = db.settings()
    device = preview_device(request.query.get("device"))
    person = None if device else preview_person(request.query.get("role"), request.query.get("staff"))
    return web.json_response({
        "device": device["name"] if device else "Preview",
        "kind": "computer" if request.query.get("view") == "desktop" else "phone",
        "role": preview_role_name(device, person),
        # A real person's preview reads like their own sign-in on a role-based device.
        "role_based": bool(person and person.get("id")),
        "shared": False, "pin_only": False, "staff": [], "pin_required": False,
        "property_name": s["property_name"], "app_name": s["app_name"],
        "accent_color": s["accent_color"], "has_logo": branding.has_logo(),
        "pin_timeout_minutes": db.setting_int(
            "pin_timeout_computer_minutes" if request.query.get("view") == "desktop"
            else "pin_timeout_minutes"),
        "pin_length": None,
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
    person = None if device else preview_person(data.get("role_id"), data.get("staff_id"))
    try:
        areas = await area_layout()
        states = await ha.states()
    except HomeAssistantError as err:
        log.warning("Home Assistant unavailable: %s", err)
        return bad_request("Can't reach the system right now.", 503)
    if device:
        allowed = phone_access(device, areas, draft)
        pages = device_pages(device, draft)
    elif person:
        allowed = person_access(person, areas, draft)
        pages = person_pages(person, draft)
    else:
        allowed, pages = layout_access(draft, areas), None
    entities = [entity_view(eid, states.get(eid), areas, lvl) for eid, lvl in allowed.items()]
    entities.sort(key=lambda e: (e["group_rank"], e["area"].lower(), e["name"].lower()))
    return web.json_response({
        "entities": entities,
        "layout": visible_layout(draft, allowed, areas, pages),
        "staff_name": person["name"] if person else None,
        "role": preview_role_name(device, person),
        "can_reorder": False,
        "control_lock": db.settings().get("control_lock") == "1",
        "control_lock_minutes": db.setting_int("control_lock_minutes"),
        "custom_order": False,
        "order": {"pages": [], "items": {}},
        "wristbands": False,   # the preview never makes cards
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
    try:
        role = db.role(int(value))
    except (TypeError, ValueError, OverflowError):
        role = None
    if not role:
        raise web.HTTPBadRequest(text="unknown role")
    return role["id"]


def parse_pin(value) -> str | None:
    pin = str(value or "").strip()
    if pin and not re.fullmatch(r"\d{4,8}", pin):
        raise web.HTTPBadRequest(text="PIN must be 4 to 8 digits")
    return pin or None


ENCODER_MODES = ("off", "local", "auto", "address")
MAX_ENCODER_URL = 300   # characters: ws://, a host name (at most 253) and a port


def clean_encoder_fields(data: dict) -> dict:
    """The card encoder from a device form, checked: encoder_mode ("off", "local" for This
    computer (USB), "auto" or "address"; left out, a typed encoder_url alone decides, as before
    Automatic), encoder_url, encoder_auto_ip (Automatic: the helper an admin confirmed with Use
    this encoder; blank = none yet, so none is used) and encoder_port. The address may be typed
    as just the desk computer's IP address; blank means no encoder. A blank or "auto" port lets
    the helper find the encoder itself."""
    fields = {}
    mode = data.get("encoder_mode")
    if mode is not None and mode not in ENCODER_MODES:
        raise ValueError("Choose Off, This computer (USB), Automatic or Address for the card "
                         "encoder.")
    if mode is not None:
        fields.update(encoder_url=None, encoder_auto=1 if mode == "auto" else 0,
                      encoder_local=1 if mode == "local" else 0)
        if mode != "auto":
            fields["encoder_auto_ip"] = None
    if "encoder_auto_ip" in data and mode in (None, "auto"):
        raw = str(data["encoder_auto_ip"] or "").strip()
        if raw and (len(raw) > 64 or not transfer.encoder_ip(raw)):
            raise ValueError("Choose one of the encoders found.")
        fields["encoder_auto_ip"] = transfer.encoder_ip(raw)
    if mode == "address" and not str(data.get("encoder_url") or "").strip():
        raise ValueError("Enter the encoder's address, or choose Automatic.")
    if "encoder_url" in data and mode in (None, "address"):
        url = str(data["encoder_url"] or "").strip().rstrip("/")
        if url and "://" not in url:
            url = "ws://" + url
        if url and not re.search(r":\d+$", url.split("://", 1)[1]):
            url += f":{SN_WEB_SOCKET_PORT}"
        if url and (len(url) > MAX_ENCODER_URL or not transfer.encoder_url(url)):
            raise ValueError("The encoder address must look like ws://192.168.1.50:8102, using "
                             "the address of the computer the encoder is plugged into.")
        fields["encoder_url"] = transfer.encoder_url(url) if url else None
    if "encoder_port" in data:
        port = str(data["encoder_port"] or "").strip()
        port = port.upper() if port.upper().startswith("COM") else port
        if port and port.lower() != "auto" and not transfer.encoder_port_ok(port):
            raise ValueError("The encoder port must be Automatic, a COM port like COM3, or a "
                             "/dev/ path.")
        fields["encoder_port"] = None if not port or port.lower() == "auto" else port
    return fields


async def admin_create_device(request: web.Request):
    data = await read_json(request)
    name = clean_name(data.get("name"))
    kind = "computer" if data.get("kind") == "computer" else "phone"
    if not name:
        return bad_request(f"Give the {kind} a name.")
    mode = data.get("signin_mode") if data.get("signin_mode") in SIGNIN_MODES else "phone"
    shared = mode in SHARED_MODES
    # Check everything before creating the phone, so a rejected request creates nothing.
    role_id = parse_role(data.get("role_id"))
    pin = None if shared else parse_pin(data.get("pin"))
    pages = clean_phone_pages(data.get("layout_pages"))
    try:
        encoder = clean_encoder_fields(data)
    except ValueError as err:
        return bad_request(str(err))
    if (kind == "computer" and data.get("encoder_mode") is None
            and not str(data.get("encoder_url") or "").strip()):
        # A new computer's encoder is plugged into it, unless the form says otherwise. Phones
        # have none.
        encoder.update(encoder_local=1, encoder_auto=0, encoder_auto_ip=None)
    device_id = db.create_device(name, role_id, pin)
    if kind != "phone":
        db.update_device(device_id, kind=kind)
    if encoder:
        db.update_device(device_id, **encoder)
        if encoder.get("encoder_auto"):
            discovery.scan_soon()   # only while Wristbands is on
    if pages is not None:
        db.update_device(device_id, layout_pages=json.dumps(pages))
    if shared:
        # A new shared computer hides names unless asked not to: a front desk screen is seen by
        # guests too. Phones show names unless asked to hide them.
        pin_only = data["pin_only"] if "pin_only" in data else kind == "computer"
        db.update_device(device_id, signin_mode=mode, staff_all=1 if data.get("staff_all", True) else 0,
                         pin_only=1 if pin_only else 0)
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
        fields["pin_length"] = len(pin) if pin else None
        end_sessions = True
    if data.get("kind") in ("phone", "computer"):
        fields["kind"] = data["kind"]
    if data.get("signin_mode") in SIGNIN_MODES:
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
    try:
        fields.update(clean_encoder_fields(data))
    except ValueError as err:
        return bad_request(str(err))
    if isinstance(data.get("staff_ids"), list):
        known = {m["id"] for m in db.staff_list()}
        archived = db.archived_staff_ids()   # not offered in the dashboard: kept for Restore
        db.set_device_staff(device["id"], [int(i) for i in data["staff_ids"] if int(i) in known]
                            + [i for i in db.device_staff_ids(device["id"]) if i in archived])
    if end_sessions:
        end_device_sessions(device["id"])
    db.update_device(device["id"], **fields)
    recheck_links(device["id"])   # its encoder link, if it may no longer be open
    if fields.get("encoder_auto"):
        discovery.scan_soon()   # not looked for yet, say (at most every 10 minutes)
    db.log(admin_name(request), f"Updated phone {fields.get('name', device['name'])}")
    return web.json_response({"ok": True})


def end_device_sessions(device_id: int) -> None:
    """Sign out everyone signed in on a phone or computer (its PIN sessions end)."""
    for token, (dev_id, *_) in list(pin_sessions.items()):
        if dev_id == device_id:
            pin_sessions.pop(token, None)


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
        recheck_links(device["id"])
        db.log(who, f"Revoked phone {device['name']}")
    elif action == "reenroll":
        db.new_enroll_code(device["id"])
        end_device_sessions(device["id"])   # signed out, as the old token no longer works
        recheck_links(device["id"])
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
    recheck_links(device["id"])
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
    if request.match_info["kind"] == "staff" and target["status"] == "archived":
        return bad_request(f"{target['name']} is archived. Restore them first.", 409)
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
    recheck_links()   # and their encoder links


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


def name_taken_message(name: str) -> str:
    """Why a new person can't have this name: someone has it, perhaps someone archived."""
    row = db.conn.execute("SELECT name FROM staff WHERE name = ? COLLATE NOCASE "
                          "AND status = 'archived'", (name,)).fetchone()
    return (f"{row[0]} is archived under that name. Restore them (Show archived on the Staff "
            "tab), or use another name." if row else "Someone with that name already exists.")


async def admin_create_staff(request: web.Request):
    data = await read_json(request)
    name = clean_name(data.get("name"), 40)
    if not name:
        return bad_request("Enter the staff member's name.")
    if db.name_taken("staff", name):
        return bad_request(name_taken_message(name))
    link = parse_connecteam_link(data)
    if link.get("connecteam_user_id") and "require_clock_in" not in link:
        link["require_clock_in"] = 1  # clocked-in only unless the admin says otherwise
    role_id = parse_role(data.get("role_id"))
    department_ids = parse_departments(data.get("department_ids", data.get("department_id")))
    if link.get("connecteam_user_id"):
        # Their smart groups' departments (from the user list just shown), unless others were
        # chosen: those are kept, and the sync leaves them alone.
        user = next((u for u in (connecteam._users or ([], 0))[0]
                     if u["id"] == link["connecteam_user_id"]), None)
        groups = [d["id"] for d in smart_group_departments(user, db.departments())] if user else []
        if not department_ids:
            department_ids = groups
        link["department_set"] = 1 if department_ids and set(department_ids) != set(groups) else 0
    if "require_clock_in" in data and link.get("connecteam_user_id"):
        # Not what their departments say: theirs, set by hand, which beats the departments'.
        rules = db.department_clock_ins()
        dept = db.clock_in_department({"department_ids": department_ids}, rules)
        if dept is not None and rules[dept] != link["require_clock_in"]:
            link["clock_in_set"] = 1
    pin = (await kiosk_pin(link.get("connecteam_user_id")) if data.get("use_kiosk_code")
           else data.get("pin"))
    if await pin_taken(require_pin(pin)):
        return bad_request(PIN_TAKEN)
    try:
        staff_id = db.create_staff(name, require_pin(pin))
    except sqlite3.IntegrityError:
        return bad_request("Someone with that name already exists.")
    if link or role_id or department_ids:
        db.update_staff(staff_id, role_id=role_id, department_ids=department_ids,
                        pin_from_connecteam=1 if data.get("use_kiosk_code") else 0, **link)
    db.log(admin_name(request), f"Added staff member {name}" +
           (" from Connecteam" if link.get("connecteam_user_id") else ""))
    return web.json_response({"id": staff_id})


async def admin_update_staff(request: web.Request):
    member = db.staff(int(request.match_info["id"]))
    if not member:
        raise web.HTTPNotFound()
    if member["status"] == "archived":
        return bad_request(f"{member['name']} is archived. Restore them first.", 409)
    data = await read_json(request)
    fields = {}
    if "name" in data and clean_name(data["name"], 40):
        fields["name"] = clean_name(data["name"], 40)
    link = parse_connecteam_link(data, member["id"])
    newly_linked = bool(link.get("connecteam_user_id")) and not member["connecteam_user_id"]
    if newly_linked and "require_clock_in" not in link:
        link["require_clock_in"] = 0   # linking never switches it on by itself
    if member["connecteam_auto"] and link.get("connecteam_user_id",
                                              member["connecteam_user_id"]) != member[
                                                  "connecteam_user_id"]:
        if staff_sync_enabled():
            # The sync would only add them again, as a second person.
            return bad_request(f"{member['name']} was added from Connecteam, so they stay "
                               "linked while Add everyone from Connecteam is on. Archive them in "
                               "Connecteam to turn them off here.")
        fields["connecteam_auto"] = 0   # from now on, a person added by hand
    pin = (await kiosk_pin(link.get("connecteam_user_id", member["connecteam_user_id"]))
           if data.get("use_kiosk_code") else data.get("pin"))
    if pin:
        if await pin_taken(require_pin(pin), member["id"]):
            return bad_request(PIN_TAKEN)
        fields["pin_hash"] = hash_pin(require_pin(pin))
        fields["pin_length"] = len(require_pin(pin))
        fields["failed_pins"] = 0
        # A typed PIN is the admin's: the Connecteam sync never changes it.
        fields["pin_from_connecteam"] = 1 if data.get("use_kiosk_code") else 0
    # Nothing is awaited from here on: read them again, as another admin may have archived (or
    # changed) them while the PIN was being checked.
    member = db.staff(member["id"])
    if not member:
        raise web.HTTPNotFound()
    if member["status"] == "archived":
        return bad_request(f"{member['name']} is archived. Restore them first.", 409)
    if "active" in data:
        if not data["active"]:
            fields["status"] = "disabled"
        elif member["status"] == "disabled":
            if member["connecteam_note"] == NO_KIOSK_CODE and not pin:
                return bad_request(f"Give {member['name']} a PIN first: Connecteam has no kiosk "
                                   "code of 4 to 8 digits for them.")
            fields["status"] = "active"
    if fields.get("status", member["status"]) != member["status"] or (
            "pin_hash" in fields and member["connecteam_note"] == NO_KIOSK_CODE):
        # Turned on or off by hand, or given a PIN: the sync leaves it so. Saving the Edit
        # dialog unchanged keeps the note, so the sync can still turn them back on.
        fields["connecteam_note"] = None
    fields.update(link)
    if "role_id" in data:
        fields["role_id"] = parse_role(data["role_id"])
        if fields["role_id"] != member["role_id"]:
            fields["role_set"] = 1
    if "department_ids" in data or "department_id" in data:
        fields["department_ids"] = parse_departments(data.get("department_ids", data.get("department_id")),
                                                     member)
    department_link_fields(member, data, fields)
    clock_in_fields(member, data, fields)
    try:
        if "layout_pages" in data:
            pages = clean_phone_pages(data["layout_pages"])   # None: their role's pages
            fields["layout_pages"] = None if pages is None else json.dumps(pages)
        if "wristbands" in data:
            perms = clean_permissions(data["wristbands"])     # None: the same as their role
            fields["wristbands"] = None if perms is None else json.dumps(perms)
        overrides = staff_overrides_from(data, member["id"])
    except ValueError as err:
        return bad_request(str(err))
    if "name" in fields and db.name_taken("staff", fields["name"], member["id"]):
        return bad_request("Someone with that name already exists.")
    try:
        db.update_staff(member["id"], **fields)
    except sqlite3.IntegrityError:
        return bad_request("Someone with that name already exists.")
    if overrides is not None:
        db.set_staff_overrides(member["id"], *overrides)
    if "pin_hash" in fields or fields.get("status") == "disabled":
        staff_sessions_end(member["id"])
    detail = None
    if fields.get("clock_in_set"):   # the per-person clock-in override changed
        detail = ("must be clocked in to sign in" if fields["require_clock_in"]
                  else "may sign in without clocking in")
    elif "clock_in_set" in fields:
        detail = "clock-in rule from their departments"
    if "department_ids" in fields:
        detail = "; ".join(filter(None, [
            "departments: " + (department_names(fields["department_ids"], db.departments()) or "none"),
            detail]))
    db.log(admin_name(request), f"Updated staff member {fields.get('name', member['name'])}",
           detail=detail)
    if fields.get("connecteam_user_id", member["connecteam_user_id"]) != member["connecteam_user_id"]:
        action, detail = link_log_text(
            fields.get("name", member["name"]),
            member["connecteam_user_id"] and (cached_user_name(member["connecteam_user_id"])
                                              or member["connecteam_user_id"]),
            fields["connecteam_user_id"] and (cached_user_name(fields["connecteam_user_id"])
                                              or fields["connecteam_user_id"]))
        db.log(admin_name(request), action, detail=detail)
    if "department_set" in fields and not fields["department_set"] and member["department_set"]:
        db.log(admin_name(request), f"{member['name']}'s departments follow Connecteam again")
    return web.json_response({"ok": True})


def department_link_fields(member: dict, data: dict, fields: dict) -> None:
    """A linked person's departments: chosen by hand (they differ from theirs), they're kept so
    and the sync leaves them alone; department_set: false (Follow Connecteam) undoes that. Newly
    linked, or following again, they go to their smart groups' departments straight away, when
    the cached user list knows them. Unlinked: nothing is kept for Connecteam."""
    uid = fields.get("connecteam_user_id", member["connecteam_user_id"])
    if "department_ids" in fields and set(fields["department_ids"]) == set(
            department_list(member["department_ids"])):
        fields.pop("department_ids")   # sent unchanged
    if not uid:
        if member["department_set"]:
            fields["department_set"] = 0
        return
    if "department_ids" in fields:
        fields["department_set"] = 1
        return
    follow = data.get("department_set") is False or (
        not member["connecteam_user_id"] and data.get("department_set") is not True)
    if not follow:
        return
    fields["department_set"] = 0
    user = next((u for u in (connecteam._users or ([], 0))[0] if u["id"] == uid), None)
    if user:
        ids = followed_departments(member, user, db.departments())
        if set(ids) != set(department_list(member["department_ids"])):
            fields["department_ids"] = ids


def clock_in_fields(member: dict, data: dict, fields: dict) -> None:
    """The clock-in rule from a staff form, into fields. The switch (on their row, or in Edit)
    sets this person's own rule by hand, which beats their departments' and which the Connecteam
    sync leaves alone, but only when it differs from the rule they'd have anyway (with the
    departments they're moving to): sending the switch unchanged changes nothing. clock_in_set:
    false is Use the department's setting."""
    wanted = fields.pop("require_clock_in", None)
    if "clock_in_set" in data and not data["clock_in_set"]:
        if member["clock_in_set"]:
            fields["clock_in_set"] = 0
        return
    after = {**member, "department_ids": fields.get("department_ids", member["department_ids"])}
    if wanted is not None and bool(wanted) != db.clock_in_rule(after)[0]:
        fields.update(require_clock_in=wanted, clock_in_set=1)


def staff_overrides_from(data: dict, staff_id: int) -> tuple[dict, dict] | None:
    """A person's own changes to their role from the dashboard ("entities" and/or "areas",
    each {id: 'none'|'view'|'control'}), or None if the request doesn't change them. A part
    left out stays as it is."""
    if "entities" not in data and "areas" not in data:
        return None
    entities, areas = db.staff_overrides(staff_id)
    if "entities" in data:
        if not isinstance(data["entities"], dict):
            raise ValueError("entities must be an object")
        entities = clean_entity_levels(data["entities"], OVERRIDE_LEVELS)
    if "areas" in data:
        if not isinstance(data["areas"], dict):
            raise ValueError("areas must be an object")
        areas = clean_area_levels(data["areas"], OVERRIDE_LEVELS)
    return entities, areas


OVERRIDE_LEVELS = ("none", "view", "control")


async def admin_unlock_staff(request: web.Request):
    member = db.staff(int(request.match_info["id"]))
    if not member:
        raise web.HTTPNotFound()
    db.update_staff(member["id"], failed_pins=0, locked_until=None,
                    **({"status": "active"} if member["status"] == "locked" else {}))
    db.log(admin_name(request), f"Unlocked staff member {member['name']}")
    return web.json_response({"ok": True})


async def admin_archive_staff(request: web.Request):
    """Archive a person (POST /api/staff/{id}/archive) or restore them (.../restore). Archiving
    ends their access everywhere at once and keeps everything of theirs. The answer lists their
    key cards that still open doors: locks check cards on their own, so those keep working until
    they expire, unless they're revoked or locked out."""
    member = db.staff(int(request.match_info["id"]))
    if not member:
        raise web.HTTPNotFound()
    who = admin_name(request)
    if request.match_info["action"] == "restore":
        if member["status"] != "archived":
            return bad_request(f"{member['name']} isn't archived.", 409)
        db.restore_staff(member["id"])
        db.log(who, f"Restored {member['name']}")
        return web.json_response({"ok": True, "status": db.staff(member["id"])["status"]})
    if member["status"] == "archived":
        return bad_request(f"{member['name']} is already archived.", 409)
    db.archive_staff(member["id"], who)
    staff_sessions_end(member["id"])   # signed out everywhere, and their encoder links close
    # No more cards from a job of theirs, and no key card for them from anyone's (a revoke or a
    # lost card notice for one of their cards goes on).
    for job_id in dict.fromkeys(db.unfinished_wb_job_ids(member["id"])
                                + db.unfinished_key_card_ids_for(member["id"])):
        decide_job(db.wb_job(job_id), "cancel")
    cards = db.key_cards("active", holder_staff_id=member["id"], limit=KEY_CARD_PAGE)
    one = len(cards) == 1
    db.log(who, f"Archived {member['name']}", detail=(
        f"{len(cards)} key card{'' if one else 's'} still open{'s' if one else ''} doors until "
        "revoked, locked out or expired" if cards else None))
    zone = await wristband_zone()
    return web.json_response({"ok": True, "cards": [key_card_view(c, zone) for c in cards]})


async def admin_delete_staff(request: web.Request):
    member = db.staff(int(request.match_info["id"]))
    if not member:
        raise web.HTTPNotFound()
    if member["status"] != "archived":
        return bad_request(f"Archive {member['name']} first. Only archived people can be deleted.",
                           409)
    cards = db.key_cards("active", holder_staff_id=member["id"], limit=KEY_CARD_PAGE)
    if cards:
        # Their cards would go on opening doors with nobody to tie them to, so Door activity
        # could no longer flag them as an archived person's.
        one = len(cards) == 1
        return bad_request(
            f"{member['name']} still has {len(cards)} key card{'' if one else 's'} that "
            f"open{'s' if one else ''} doors. Revoke {'it' if one else 'them'} or report "
            f"{'it' if one else 'them'} lost under Key cards first, or delete {member['name']} "
            f"once {'it has' if one else 'they have'} expired.", 409, key_cards=len(cards))
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
                 "control_lock", "connecteam_enabled", "wristbands_enabled", "wb_lockout_daypass",
                 "wb_lockout_overnight", "wb_lockout_staff", "wb_lockout_contractor",
                 "connecteam_auto_staff", "connecteam_auto_clock_in", "connecteam_auto_departments",
                 "wb_remote_lockout", "wb_lost_card_alert")
PIN_PAUSE_MIN_MS, PIN_PAUSE_MAX_MS = 200, 5000
REMOTE_LOCKOUT_APIS = ("reversed", "plain")   # TT Hotel's two calls for enrolling a card number
NUMBER_SETTINGS = (("pin_timeout_minutes", 1, 1440), ("pin_timeout_computer_minutes", 1, 1440),
                   # Any shorter and someone tapping a 6-digit PIN at an ordinary pace is signed
                   # in (and told it's wrong) after 4 digits.
                   ("pin_pause_ms", PIN_PAUSE_MIN_MS, PIN_PAUSE_MAX_MS),
                   ("max_pin_attempts", 3, 20),
                   ("enroll_hours", 1, 720), ("control_lock_minutes", 1, 120),
                   ("connecteam_poll_minutes", 30, 240), ("tthotel_sync_hours", 1, 48),
                   ("wb_daypass_max", 1, 50), ("wb_overnight_max", 1, 50),
                   ("wb_staff_days", 1, 3650), ("wb_staff_max", 1, 20), ("wb_card_wait", 10, 600),
                   ("wb_max_records", 1, 64), ("wb_contractor_max", 1, 20))
# Times of day for wristbands: when day passes stop working, and checkout time.
TIME_SETTINGS = {"wb_daypass_end": "The time day passes stop working",
                 "wb_checkout_time": "The checkout time"}
DOOR_SETTINGS = ("wb_daypass_doors", "wb_overnight_doors")
MAX_SETTING_DOORS = 200   # doors on a day pass or every overnight card (a card holds far fewer)


def wristband_setting(key: str) -> bool:
    return key == "wristbands_enabled" or key.startswith(("tthotel_", "wb_"))


def clean_time_of_day(value, what: str) -> str:
    """"20:00" from the dashboard's time picker ("8:00" and "20:00:00" will do too)."""
    match = re.fullmatch(r"(\d{1,2}):([0-5]\d)(:00)?", str(value or "").strip())
    if not match or int(match[1]) > 23:
        raise ValueError(f"{what} must be a time like 20:00 (24-hour clock).")
    return f"{int(match[1]):02d}:{match[2]}"


def clean_time_zone(value) -> str:
    """A time zone name like America/Denver, or "" for Home Assistant's own."""
    name = str(value or "").strip()
    if not name:
        return ""
    try:
        if len(name) > 64 or not re.fullmatch(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*", name):
            raise ValueError
        ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("Enter a time zone like America/Denver, or leave it blank to use Home "
                         "Assistant's.") from None
    return name


def clean_lock_ids(raw) -> list[int]:
    """TT Hotel lock IDs chosen in the dashboard (a list, or JSON text of one)."""
    try:
        ids = json.loads(raw or "[]") if isinstance(raw, str) else raw
    except ValueError:
        ids = None
    if not isinstance(ids, list) or len(ids) > MAX_SETTING_DOORS:
        raise ValueError("Pick the doors from the list.")
    cleaned = []
    for lock_id in ids:
        if isinstance(lock_id, str) and re.fullmatch(r"\d{1,16}", lock_id):
            lock_id = int(lock_id)
        if isinstance(lock_id, bool) or not isinstance(lock_id, int) or not 0 < lock_id <= 2**53:
            raise ValueError("Pick the doors from the list.")
        if lock_id not in cleaned:
            cleaned.append(lock_id)
    return cleaned


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
    values.update(clean_wristband_settings(data))
    for key, lo, hi in NUMBER_SETTINGS:
        if key in data:
            try:
                values[key] = max(lo, min(hi, int(data[key])))
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number.") from None
    return values


def clean_wristband_settings(data: dict) -> dict:
    """The Wristbands part of clean_settings (TT Hotel account, times, doors, card details)."""
    values = {}
    if "tthotel_region" in data:
        if data["tthotel_region"] not in ("global", "china"):
            raise ValueError("Choose where your TT Hotel account is hosted.")
        values["tthotel_region"] = data["tthotel_region"]
    if "tthotel_client_id" in data:
        client_id = str(data["tthotel_client_id"] or "").strip()
        if len(client_id) > 200 or not re.fullmatch(r"[\x21-\x7e]*", client_id):
            raise ValueError("The TT Hotel Client ID doesn't look right.")
        values["tthotel_client_id"] = client_id
    if "tthotel_username" in data:
        username = str(data["tthotel_username"] or "").strip()
        if len(username) > 200 or not username.isprintable():
            raise ValueError("The TT Hotel username doesn't look right.")
        values["tthotel_username"] = username
    # The secret and the password are write-only: blank keeps the saved ones, "clear" removes both.
    secret = str(data.get("tthotel_client_secret") or "").strip()
    if secret:
        if len(secret) > 500 or not re.fullmatch(r"[\x21-\x7e]+", secret):
            raise ValueError("The TT Hotel Client Secret doesn't look right.")
        values["tthotel_client_secret"] = secret
    password = str(data.get("tthotel_password_md5") or "").strip()
    if password:
        if not re.fullmatch(r"[0-9a-fA-F]{32}", password):
            raise ValueError("Enter the password exactly as TT Hotel shows it under Settings > "
                             "Integration: 32 letters and numbers, not your real password.")
        values["tthotel_password_md5"] = password.lower()
    if data.get("tthotel_clear_secret") in (True, "1", "true", "on"):
        values["tthotel_client_secret"] = values["tthotel_password_md5"] = ""
    if "wb_time_zone" in data:
        values["wb_time_zone"] = clean_time_zone(data["wb_time_zone"])
    for key, what in TIME_SETTINGS.items():
        if key in data:
            values[key] = clean_time_of_day(data[key], what)
    for key in DOOR_SETTINGS:
        if key in data:
            values[key] = json.dumps(clean_lock_ids(data[key]))
    if "wb_sectors" in data:
        sectors = str(data["wb_sectors"] or "").strip()
        if not re.fullmatch(r"[01]{16}", sectors) or "1" not in sectors:
            raise ValueError("Sectors must be 16 digits, each 0 or 1, as set in TT Hotel "
                             "(its default is 0000000000011111).")
        values["wb_sectors"] = sectors
    if "wb_scan_subnets" in data:
        values["wb_scan_subnets"] = describe(parse_networks(data["wb_scan_subnets"]))
    if "wb_remote_lockout_api" in data:
        if data["wb_remote_lockout_api"] not in REMOTE_LOCKOUT_APIS:
            raise ValueError("Choose how card numbers are sent to TT Hotel for remote lock-out.")
        values["wb_remote_lockout_api"] = data["wb_remote_lockout_api"]
    # wb_remote_lockout_tested is never taken from a form or a backup: only the test helper sets
    # it, after the owner saw a locked-out card refused at a door.
    return values


def refuse_staff_doors(values: dict, current: dict) -> None:
    """Staff doors never go on day passes or overnight wristbands, so one newly ticked as a
    day-pass or overnight shared door is refused (ValueError). One still listed from before (a
    restored backup can bring one back: marking a door a staff door takes it off) goes on no
    card already, and is dropped from what's saved."""
    staff = {door["lock_id"]: door for door in db.wb_doors()
             if wristbands.door_kind(door["kind"]) == "staff"}
    for key in DOOR_SETTINGS:
        if key not in values:
            continue
        picked = json.loads(values[key])
        before = set(wristbands.setting_doors(current, key))
        for lock_id in picked:
            if lock_id in staff and lock_id not in before:
                raise ValueError(f"{door_name(staff[lock_id])} is a staff door, so it can't go on "
                                 "day passes or overnight wristbands. Untick it, or mark it as a "
                                 "public door.")
        values[key] = json.dumps([lock_id for lock_id in picked if lock_id not in staff])


def unshared_text(unshared: dict) -> str | None:
    """"taken off the day-pass doors and the overnight shared doors" (db.set_wb_door's
    unshared), or None when a new staff door wasn't one of them."""
    where = [{"wb_daypass_doors": "the day-pass doors",
              "wb_overnight_doors": "the overnight shared doors"}[key]
             for key in DOOR_SETTINGS if unshared.get(key)]
    return f"taken off {' and '.join(where)}" if where else None


async def admin_update_settings(request: web.Request):
    data = await read_json(request)
    current = db.settings()
    try:
        values = clean_settings(data, current)
        refuse_staff_doors(values, current)
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
    ct_before, tt_before = connecteam_settings(), tthotel_settings()
    if values.get("wb_remote_lockout_api", current.get("wb_remote_lockout_api")) != current.get(
            "wb_remote_lockout_api"):
        values["wb_remote_lockout_tested"] = ""   # the other format hasn't been tried at a door
    sync_before = connecteam_sync_wanted()
    db.update_settings(values)
    branding.invalidate()
    if connecteam_settings() != ct_before:
        connecteam.forget()   # also wakes the clock check, which runs only if someone needs it
    if connecteam_sync_wanted() and (not sync_before or connecteam_settings() != ct_before
                                     or any(k in values for k in SYNC_SETTINGS)):
        sync_staff_soon()     # at most once a minute
    if tthotel_settings() != tt_before:
        tthotel.forget()      # another account (or Wristbands turned on): a fresh start with it
    db.log(admin_name(request), settings_log_text(values))
    return web.json_response({"ok": True})


# Saving any of these changes what the sync does: it runs again soon.
SYNC_SETTINGS = ("connecteam_auto_clock_in", "connecteam_auto_staff", "connecteam_auto_departments")


def settings_log_text(values: dict) -> str:
    """The Activity line for a settings save. TT Hotel and Connecteam are each saved on their own
    (Settings > Connections), and so are the Wristbands tab's settings."""
    for prefix, text in (("tthotel_", "Updated TT Hotel settings"),
                         ("connecteam_", "Updated Connecteam settings")):
        if values and all(k.startswith(prefix) for k in values):
            return text
    if values and all(wristband_setting(k) for k in values):
        return "Updated wristband settings"
    return "Updated settings"


# ---------------------------------------------------------------- Google Drive (HR files)
# The HR portal keeps its files in a Google shared drive, through a small script the owner
# deploys in their own Google account (hr/gdrive.py, hr/bridge.gs): no Cloud project or key file.
# Settings > Connections sets it up: copy the script (with this install's secret in it), paste
# its Web app URL, pick the drives, test. The secret only ever leaves here inside that script.
# Google is only asked when the owner checks, saves or tests the connection here, or when the HR
# portal works with files.

GOOGLE_CHECK_GAP = 5      # seconds between checks from the dashboard
GOOGLE_TEST_GAP = 10      # ...and tests (each makes and removes a folder and reads an image)
google_asked = {"check": 0.0, "test": 0.0}
GOOGLE_NEW_SECRET = ("The script in Google still has the old secret. Paste the new script there "
                     "and deploy a new version.")


def google_status() -> dict:
    """What the last check found: checked_at, email, version, code and error (None when it
    worked), and drives (the shared drives the script could see)."""
    return load_setting_object("hr_google_status")


def save_google_status(**fields) -> None:
    db.update_settings({"hr_google_status": json.dumps({**google_status(), **fields})})


def google_view(full: bool = False) -> dict:
    """The Google Drive connection as the dashboard sees it. Never the secret; the folded line
    (in /api/state) doesn't get the address or the drives either."""
    s = db.settings()
    status = google_status()
    view = {"set_up": gdrive.bridge_from_settings(s) is not None,
            "script_made": bool(s.get("hr_google_secret")), "drive_set": bool(s.get("hr_google_drive")),
            "script_version": gdrive.SCRIPT_VERSION,
            **{k: status.get(k) for k in ("checked_at", "email", "version", "code", "error")}}
    if full:
        view.update(url=s.get("hr_google_url", ""), drive=s.get("hr_google_drive", ""), manifest=gdrive.MANIFEST,
                    restricted_drive=s.get("hr_google_restricted_drive", ""),
                    drives=status.get("drives") or [])
    return view


async def google_check(bridge: gdrive.Bridge) -> tuple[bool, BridgeError | None]:
    """Asks the script who it runs as, which shared drives it sees and its version, and lets it
    use the chosen drives if it doesn't yet (a copy deployed afresh starts with none). Returns
    (whether the script answered, the problem or None), and keeps what it found."""
    now = time.time()
    try:
        info = await bridge.ping()
    except BridgeError as err:
        save_google_status(checked_at=now, version=bridge.last_version, code=err.code, error=err.message)
        return False, err
    found = {"checked_at": now, "email": info["email"], "version": info["version"],
             "drives": info["drives"]}
    wanted = gdrive.drive_ids(db.settings())
    if wanted and info["allowed"] != wanted:
        try:
            await bridge.setup(wanted)
        except BridgeError as err:
            save_google_status(**found, code=err.code, error=err.message)
            return True, err
    save_google_status(**found, code=None, error=None)
    return True, None


async def admin_google(request: web.Request):
    return web.json_response(google_view(full=True))


async def admin_google_script(request: web.Request):
    """Copy the script: its text, with this install's secret (made the first time, or anew with
    {"new_secret": true}, after which the script must be pasted and deployed again)."""
    if refusal := hr_key_locked():
        return refusal
    data = await optional_json(request)
    s = db.settings()
    secret = s.get("hr_google_secret") or ""
    new = data.get("new_secret") in (True, "1") or not gdrive.SECRET_RE.fullmatch(secret)
    if new:
        replacing = bool(secret)
        secret = gdrive.new_secret()
        db.update_settings({"hr_google_secret": secret})
        if replacing and s.get("hr_google_url"):
            save_google_status(code="auth", error=GOOGLE_NEW_SECRET)
        hr_connection_log(request, "Made a new secret for the Google Drive script" if replacing
                          else "Made the Google Drive script")
    else:
        db.log(admin_name(request), "Copied the Google Drive script")
    return web.json_response({"script": gdrive.script_source(secret), "new_secret": new},
                             headers={"Cache-Control": "no-store"})


async def admin_save_google(request: web.Request):
    """Save: the Web app URL and the drives. A new address or other drives are checked straight
    away (and the script is told which drives it may use)."""
    if refusal := hr_key_locked():
        return refusal
    data = await read_json(request)
    s = db.settings()
    try:
        url = gdrive.clean_url(data["url"]) if "url" in data else s.get("hr_google_url", "")
        drive, restricted = (
            gdrive.clean_drive_id(data[key]) if key in data else s.get(f"hr_google_{key}", "")
            for key in ("drive", "restricted_drive"))
    except ValueError as err:
        return bad_request(str(err))
    if restricted and (restricted == drive or not drive):
        return bad_request("Pick the HR drive, and a different shared drive for restricted files "
                           "(or keep them in a folder in the HR drive).")
    values = {"hr_google_url": url, "hr_google_drive": drive, "hr_google_restricted_drive": restricted}
    changed = {k for k, v in values.items() if s.get(k, "") != v}
    if "hr_google_url" in changed:
        values["hr_google_status"] = "{}"   # another script: nothing known about it yet
    db.update_settings(values)
    if changed:
        hr_connection_log(request, "Updated the Google Drive settings (HR files)")
    bridge = gdrive.bridge_from_settings(db.settings())
    problem = None
    if bridge and changed:
        google_asked["check"] = time.time()
        _, problem = await google_check(bridge)
    return web.json_response({"ok": True, "problem": problem.message if problem else None,
                              "google": google_view(full=True)})


async def admin_check_google(request: web.Request):
    """Check: the account, the drives the script sees, its version (opening the connection on
    the dashboard does this, at most every few seconds)."""
    bridge = gdrive.bridge_from_settings(db.settings())
    if not bridge:
        return bad_request("Copy the script and paste its Web app URL first.")
    problem = None
    if time.time() - google_asked["check"] >= GOOGLE_CHECK_GAP:
        google_asked["check"] = time.time()
        _, problem = await google_check(bridge)
    return web.json_response({"problem": problem.message if problem else None,
                              "google": google_view(full=True)})


async def admin_test_google(request: web.Request):
    """Test connection: signing in, the drive, making and removing a test folder, and reading text
    from a test image, each with a plain result."""
    bridge = gdrive.bridge_from_settings(db.settings())
    if not bridge:
        return bad_request("Copy the script and paste its Web app URL first.")
    if time.time() - google_asked["test"] < GOOGLE_TEST_GAP:
        return bad_request("Wait a few seconds before testing again.", 429)
    google_asked["test"] = google_asked["check"] = time.time()
    answered, problem = await google_check(bridge)
    if answered:
        checks = await bridge.self_test()
    else:
        what = {"auth": "Sign-in", "outdated": "Script version"}.get(problem.code, "Reaching the script")
        checks = [{"check": what, "ok": False, "detail": problem.message}]
    failed = [c for c in checks if not c["ok"]]
    if answered and failed:
        save_google_status(code="outdated" if failed[0]["check"] == "Script version" else "test",
                           error=failed[0]["detail"])
    elif answered:
        save_google_status(code=None, error=None, tested_at=time.time())
    hr_connection_log(request, "Tested the Google Drive connection (HR files)",
                      detail="Every check passed." if not failed else
                      " ".join(f"{c['check']}: {c['detail']}" for c in failed), ok=not failed)
    return web.json_response({"checks": checks, "google": google_view(full=True)})


# ---------------------------------------------------------------- wristbands (dashboard)

WRISTBAND_TEST_GAP = 10    # seconds between TT Hotel or encoder tests
DOOR_SYNC_GAP = 60         # seconds between door syncs started from the dashboard
wristbands_tested = [0.0]
doors_synced = [0.0]
encoders_tested: dict[int, float] = {}


async def wristbands_state() -> dict:
    """TT Hotel's connection and request counts, each encoder's status (all from memory), and
    Home Assistant's time zone, so the dashboard can say which zone cards use (read at most every
    few hours, and only while Wristbands is on)."""
    urls = sorted({encoder_address(d, rescan=False) for d in db.devices()} - {None})
    zone = await ha_time_zone() if tthotel_settings()[0] else ha_zone["name"]
    return {**tthotel.state(), "encoders": {url: encoder_status(url) for url in urls},
            "discovery": discovery.state(), "ha_time_zone": zone}


def door_view(door: dict) -> dict:
    return {"id": door["lock_id"], "tt_name": door["tt_name"], "alias": door["alias"],
            "name": door["name"], "mac_last4": door["mac"][-4:],
            "building": door["building_number"], "building_name": door["building_name"],
            "floor": door["floor_number"], "floor_name": door["floor_name"],
            "battery": door["battery"], "has_gateway": bool(door["has_gateway"]),
            "hidden": bool(door["hidden"]), "active": bool(door["active"]),
            "kind": wristbands.door_kind(door.get("kind"))}


async def admin_wristband_doors(request: web.Request):
    return web.json_response({"doors": [door_view(d) for d in db.wb_doors()]})


async def admin_update_door(request: web.Request):
    """A door's name for staff ('' = TT Hotel's), whether staff see it in door lists, and its
    kind (a public door, a guest room, or not marked)."""
    door = db.wb_door(int(request.match_info["id"]))
    if not door:
        raise web.HTTPNotFound()
    data = await read_json(request)
    fields = {}
    if "name" in data:
        fields["name"] = clean_name(data["name"])
    if "hidden" in data:
        fields["hidden"] = 1 if data["hidden"] in (True, 1, "1") else 0
    if "kind" in data:
        try:
            fields["kind"] = clean_door_kind(data["kind"])
        except ValueError as err:
            return bad_request(str(err))
    # A new staff door is taken off the day-pass and overnight shared doors at the same time.
    unshared = db.set_wb_door(door["lock_id"], **fields)
    detail = "hidden from staff" if fields.get("hidden") else None
    if "kind" in fields:
        detail = f"marked as {DOOR_KIND_WORDS[fields['kind']]}" if fields["kind"] else "not marked"
        if unshared_text(unshared):
            detail += f", {unshared_text(unshared)}"
    db.log(admin_name(request), f"Updated door {fields.get('name') or door_name(door)}",
           detail=detail)
    return web.json_response({"ok": True, **({"unshared": unshared} if unshared else {})})


# ---------------------------------------------------------------- door kinds
# Each door is a public door (shared doors on day passes and overnight wristbands), a guest
# room (rooms on overnight wristbands) or a staff door (only on staff cards and contractor
# badges). One not marked yet is offered as a public door and a room, so nothing disappears
# from staff's lists until the owner has marked it.

DOOR_KIND_WORDS = wristbands.KIND_WORDS
DOOR_KIND_PLURALS = {"public": "public doors", "room": "guest rooms", "staff": "staff doors"}
MAX_DOOR_BATCH = 5000   # doors marked at once (a whole building)


def clean_door_kind(value) -> str | None:
    """'public', 'room', 'staff', or None (not marked) from the dashboard."""
    if value is None or value == "":
        return None
    if value not in wristbands.DOOR_KINDS:
        raise ValueError("Choose Public door, Guest room, Staff door or Not marked.")
    return value


async def admin_mark_doors(request: web.Request):
    """Mark many doors at once (a whole floor or building): {"door_ids": [...], "kind": ...}."""
    data = await read_json(request)
    ids = data.get("door_ids")
    if (not isinstance(ids, list) or not ids or len(ids) > MAX_DOOR_BATCH
            or not all(isinstance(i, int) and not isinstance(i, bool) and 0 < i <= 2**53
                       for i in ids)):
        return bad_request("Pick the doors to mark.")
    try:
        kind = clean_door_kind(data.get("kind"))
    except ValueError as err:
        return bad_request(str(err))
    # New staff doors are taken off the day-pass and overnight shared doors at the same time.
    changed, unshared = db.mark_wb_doors(list(dict.fromkeys(ids)), kind)
    noun = f"{changed} door{'' if changed == 1 else 's'}"
    words = DOOR_KIND_WORDS if changed == 1 else DOOR_KIND_PLURALS
    detail = None
    if unshared_text(unshared):
        names = {d["lock_id"]: door_name(d) for d in db.wb_doors()}
        taken = list(dict.fromkeys(i for key in DOOR_SETTINGS for i in unshared.get(key, [])))
        detail = f"{', '.join(names.get(i, f'Door {i}') for i in taken)} {unshared_text(unshared)}"
    db.log(admin_name(request), f"Marked {noun} as {words[kind]}" if kind else f"Unmarked {noun}",
           detail=detail)
    return web.json_response({"changed": changed, **({"unshared": unshared} if unshared else {})})


async def admin_wristbands_test(request: web.Request):
    """Sign in to TT Hotel, read its door list, and get the key the encoder needs."""
    if not tthotel_settings()[0]:
        return bad_request("Turn on Wristbands and save the settings first.")
    if time.time() - wristbands_tested[0] < WRISTBAND_TEST_GAP:
        return bad_request("Wait a few seconds before testing again.", 429)
    wristbands_tested[0] = time.time()
    try:
        result = await tthotel.sync(admin=True)   # also lifts a stop after rejected details
        await tthotel.hotel_info(fresh=True)
    except TTHotelError as err:
        db.log(admin_name(request), "Tested the TT Hotel connection", detail=str(err), ok=False)
        return bad_request(str(err), 502)
    db.log(admin_name(request), "Tested the TT Hotel connection",
           detail=f"{result['total']} doors in {result['buildings']} buildings")
    return web.json_response({"doors": result["total"], "buildings": result["buildings"]})


async def admin_sync_doors(request: web.Request):
    if not tthotel_settings()[0]:
        return bad_request("Turn on Wristbands and save the settings first.")
    if time.time() - doors_synced[0] < DOOR_SYNC_GAP:
        return bad_request("The doors were synced less than a minute ago. Wait a moment and try "
                           "again.", 429)
    doors_synced[0] = time.time()
    try:
        result = await tthotel.sync(admin=True)
    except TTHotelError as err:
        db.log(admin_name(request), "Synced doors from TT Hotel", detail=str(err), ok=False)
        return bad_request(str(err), 502)
    db.log(admin_name(request), "Synced doors from TT Hotel",
           detail=f"{result['added']} added, {result['updated']} updated, "
                  f"{result['removed']} removed, {result['total']} in all")
    return web.json_response({k: result[k] for k in ("added", "updated", "removed", "total")})


async def admin_encoder_test(request: web.Request):
    """Connect to a computer's card encoder, read its version, and let it go again. This
    computer (USB) goes through the computer's browser link, so Wristbands must be open there."""
    device = db.device(int(request.match_info["id"]))
    if not device:
        raise web.HTTPNotFound()
    url = encoder_address(device, rescan=False)
    if not url:
        return bad_request(
            f"No encoder has been found for this computer ({encoder_text(device)}). Click Find "
            "encoders, or enter its address." if encoder_mode(device) == "auto" else
            "Choose this computer's card encoder (This computer, Automatic or its address) and "
            "save it first.")
    local = encoder_mode(device) == "local"
    device = {**device, "encoder_url": url}
    if local and not encoders.relay(device["id"]).connected:
        return bad_request(f"{encoders.relay(device['id']).missing_message()}, then test it "
                           "again.", 409)
    now = time.time()
    if now - encoders_tested.get(device["id"], 0) < WRISTBAND_TEST_GAP:
        return bad_request("Wait a few seconds before testing again.", 429)
    if len(encoders_tested) > 1000:
        encoders_tested.clear()
    encoders_tested[device["id"]] = now
    if encoder_busy(url):
        return bad_request("The encoder is making cards right now. Test it when it's done.", 409)
    try:
        version = await test_encoder(device)
    except EncoderError as err:
        # Through a browser link, say which computer to check (not "on the network").
        message = (encoders.get(url).explain(err.code) if local and err.code in (
            NO_LINK, UNREACHABLE, NO_REPLY) else err.message)
        db.log(admin_name(request), f"Tested the encoder on {device['name']}", detail=message,
               ok=False)
        return bad_request(message, 502)
    db.log(admin_name(request), f"Tested the encoder on {device['name']}", detail=str(version)[:200])
    return web.json_response({"version": version})


async def admin_find_encoders(request: web.Request):
    """Find encoders: look for SN Web Socket helpers on the network now, and say what was found.
    Works whether or not anything uses Automatic yet."""
    wait = discovery.admin_wait()
    if wait > 0:
        return bad_request(f"Encoders were looked for a moment ago. Try again in {int(wait) + 1} "
                           "seconds.", 429)
    found = await discovery.find()
    detail = (f"{len(found['helpers'])} found on {describe(found['networks'])}"
              if not found["error"] else found["error"])
    db.log(admin_name(request), "Looked for card encoders", detail=detail[:200],
           ok=not found["error"])
    return web.json_response({"discovery": found, "devices": device_payload()})


async def admin_wristband_jobs(request: web.Request):
    """Every card job, newest first."""
    jobs = db.wb_jobs(WRISTBAND_LOG_PAGE, parse_before(request.query.get("before")))
    zone = await wristband_zone()
    return web.json_response({"jobs": [job_view(job, zone) for job in jobs]})


# ---------------------------------------------------------------- door activity (TT Hotel)
# TT Hotel pushes lock records for doors on a gateway to its "Callback URL" (Settings >
# Integration): <staff app address>/tthotel/records/<token>, on the staff app's server, since
# that's what the Cloudflare Tunnel reaches. The secret token is the only way in (no device or
# dashboard sign-in). Records are stored, matched to cards made here (a card's "Last used"), and
# a lost or revoked card tried at a door is noted in the activity log and Home Assistant. Nothing
# else ever happens because of them: no TT Hotel request, no card job, no command.

# Limits. The right address has budgets of its own, shared by every caller (TT Hotel's servers
# vary): requests with a wrong one never use them up, so they can't get TT Hotel's deliveries
# refused. Over a budget, TT Hotel is answered 429 rather than "success", so it sends those
# records again later and nothing real is lost.
CALLBACK_PER_MINUTE = 300     # deliveries to the right address (TT Hotel sends one per few records)
RECORDS_PER_MINUTE = 1000     # ...and records stored from them, so even a leaked address can't
                              # push the door history out (LOCK_RECORDS_MAX_ROWS) in minutes
WRONG_ADDRESS_PER_MINUTE = 30  # requests with a wrong address, per caller
CARD_ALERT_GAP = 600          # one alert per card this often at most, however often it's tried
ALERTS_PER_MINUTE = 20        # and this many alerts in all, so even a leaked address can't flood
                              # the activity log or Home Assistant (Door activity lists them all)
DOOR_ACTIVITY_PAGE = 50
DOOR_ALERTS = {"lost": ("Lost card", "was reported lost"),
               "revoked": ("Revoked card", "was revoked"),
               "deleted": ("Deleted card", "was deleted in TT Hotel"),
               "archived": ("Archived person's card", "belongs to someone who was archived")}
callback_requests: dict[str, collections.deque] = {}   # caller -> times of its wrong addresses
callback_deliveries: collections.deque = collections.deque()  # times of right-address deliveries
callback_records: collections.deque = collections.deque()     # (time, records stored) of those
card_alerts: dict[str, float] = {}                      # card -> when it last alerted
recent_alerts: collections.deque = collections.deque()  # when the last alerts were
# The last delivery with the right address, for the dashboard (since the add-on started).
door_delivery = {"at": None, "kept": 0, "dropped": 0, "lock_id": None, "known": True}


def callback_token() -> str:
    """The secret part of the Door activity address, made the first time it's needed."""
    token = db.settings().get("tthotel_callback_token", "")
    if not doorlog.TOKEN_RE.match(token):
        token = doorlog.new_token()
        db.update_settings({"tthotel_callback_token": token})
    return token


def wrong_addresses_too_busy(ip: str) -> bool:
    """Whether this caller tried too many wrong addresses this minute (counting this one)."""
    now = time.time()
    if len(callback_requests) > 1000:   # callers can be many: forget the quiet ones
        for key in [k for k, times in callback_requests.items() if not times or now - times[-1] > 60]:
            del callback_requests[key]
        if len(callback_requests) > 1000:
            callback_requests.clear()
    times = callback_requests.setdefault(ip, collections.deque())
    while times and now - times[0] > 60:
        times.popleft()
    if len(times) >= WRONG_ADDRESS_PER_MINUTE:
        return True
    times.append(now)
    return False


def deliveries_too_busy(now: float) -> bool:
    """Whether the right address had CALLBACK_PER_MINUTE deliveries this minute (else this one
    counts)."""
    while callback_deliveries and now - callback_deliveries[0] > 60:
        callback_deliveries.popleft()
    if len(callback_deliveries) >= CALLBACK_PER_MINUTE:
        return True
    callback_deliveries.append(now)
    return False


def records_too_many(now: float, count: int) -> bool:
    """Whether storing `count` more records would go past RECORDS_PER_MINUTE (else they count).
    A delivery is stored whole or not at all (doorlog.MAX_RECORDS fits in an empty minute)."""
    while callback_records and now - callback_records[0][0] > 60:
        callback_records.popleft()
    if count and sum(n for _, n in callback_records) + count > RECORDS_PER_MINUTE:
        return True
    if count:
        callback_records.append((now, count))
    return False


async def tthotel_records(request: web.Request):
    """Lock records from TT Hotel. The right address gets "success", whatever was sent, so TT
    Hotel never sends the same thing again and again; what can't be read is dropped, and so are
    records for doors TT Hotel hasn't listed here (sync the doors). Only over the right
    address's own limits is it 429, and TT Hotel sends those again later."""
    token = db.settings().get("tthotel_callback_token", "")
    if not token or not secrets.compare_digest(request.match_info["token"].encode(), token.encode()):
        if wrong_addresses_too_busy(client_ip(request)):
            return web.Response(status=429, text="Too many requests.")
        raise web.HTTPNotFound()
    if deliveries_too_busy(time.time()):
        return web.Response(status=429, text="Too many requests.")
    try:
        async with asyncio.timeout(request.app.get(BODY_SECONDS, 10)):
            body = await request.read()   # at most the staff app's client_max_size
    except TimeoutError:
        raise web.HTTPRequestTimeout(text="The request took too long to arrive.") from None
    except web.HTTPRequestEntityTooLarge:
        log.warning("TT Hotel sent more lock records at once than Staff Access reads; dropped")
        body = b""
    now = time.time()
    lock_id, records, dropped = doorlog.read_delivery(body, request.content_type, now)
    known = lock_id is not None and db.wb_door(lock_id) is not None
    if known and records_too_many(now, len(records)):
        log.warning("TT Hotel sent more lock records this minute than Staff Access stores; it "
                    "will send them again")
        return web.Response(status=429, text="Too many records this minute.")
    added = db.add_lock_records(lock_id, records, now) if known and records else []
    door_delivery.update(at=now, kept=len(added), lock_id=lock_id, known=known or lock_id is None,
                         dropped=dropped + (0 if known else len(records)))
    if any(r["alert"] for r in added):   # TT Hotel isn't kept waiting for them
        background(door_alerts(lock_id, added, now), "note a lost card tried at a door")
    return web.Response(text="success")


async def door_alerts(lock_id: int, added: list[dict], now: float) -> None:
    """A lost, revoked or deleted card, or an archived person's, tried at a door: a line in the
    activity log and (if that's on) a Home Assistant notification, one per card, replaced by the
    next, at most every CARD_ALERT_GAP seconds per card (and ALERTS_PER_MINUTE in all)."""
    door = door_name(db.wb_door(lock_id) or {"name": "", "alias": "", "tt_name": "",
                                             "lock_id": lock_id})
    zone = await wristband_zone()
    if len(card_alerts) > 1000:
        for key in [k for k, at in card_alerts.items() if now - at > CARD_ALERT_GAP]:
            del card_alerts[key]
    for record in added:
        if not record["alert"]:
            continue
        card = record["card"]
        number = card["card_number"] if card else record["card_number"]
        key = f"card {card['id']}" if card else f"number {number or record['username']}"
        while recent_alerts and now - recent_alerts[0] > 60:
            recent_alerts.popleft()
        if (now - card_alerts.get(key, float("-inf")) < CARD_ALERT_GAP
                or len(recent_alerts) >= ALERTS_PER_MINUTE):
            continue
        card_alerts[key] = now
        recent_alerts.append(now)
        what, why = DOOR_ALERTS[record["alert"]]
        action = f"{what} {'opened' if record['success'] else 'tried at'} {door}"
        name = card["name"] if card else record["username"]
        noun = alert_noun(card)
        label = f"{noun} {number}" if number else f"A {noun.lower()}"
        label += f" ({name})" if name else ""
        when = doorlog.when_label(record["at"], now, zone) if zone else ""
        db.log("Door activity", action, ok=False,
               detail=f"{label} · {'opened' if record['success'] else 'refused'}"
                      f"{f' · {when}' if when else ''}")
        if db.settings().get("wb_lost_card_alert") != "1":
            continue
        # Home Assistant shows the message as Markdown, and whoever has the address can send
        # any text: so only a card number that is one (digits) and the name of a card made here,
        # never TT Hotel's own text, and every mark that could make a link or formatting is
        # escaped.
        shown = number if number and number.isascii() and number.isdigit() else ""
        named = f"{noun} {shown}" if shown else f"A {noun.lower()}"
        named += f" ({markdown_text(card['name'])})" if card and card["name"] else ""
        message = (f"{named} {why}. It was tried at {markdown_text(door)}"
                   f"{f' {when}' if when else ''}, and "
                   f"{'the door opened' if record['success'] else 'the door refused it'}. "
                   "See Staff Access, Wristbands tab, Door activity.")
        tag = re.sub(r"[^0-9A-Za-z]", "", str(card["id"] if card else number or record["username"]))
        background(ha.call_service("persistent_notification", "create", {
            "title": action, "message": message,
            "notification_id": f"staff_access_card_{tag[:40] or lock_id}"}),
            "tell Home Assistant a lost card was tried")


def alert_noun(card: dict | None) -> str:
    """What a door alert calls the card: "Contractor badge" for a contractor badge made here,
    else "Card"."""
    return "Contractor badge" if card and card.get("card_type") == "contractor" else "Card"


def markdown_text(text: str) -> str:
    """Text shown as itself in Markdown (a Home Assistant notification): every ASCII punctuation
    mark escaped, so it can't make a link, an image, an address that turns into a link, or
    formatting."""
    return re.sub(r"([!-/:-@\[-`{-~])", r"\\\1", " ".join(str(text).split()))


def last_used_view(row: dict | None, zone: ZoneInfo | None) -> dict | None:
    """A card's newest lock record (db.cards_last_used) as the apps show it, or None."""
    if not row:
        return None
    door = door_name({"name": row["door_name"] or "", "alias": row["door_alias"] or "",
                      "tt_name": row["door_tt_name"] or "", "lock_id": row["lock_id"]})
    return {"at": row["at"], "door": door, "ok": bool(row["success"]),
            "label": doorlog.used_label(door, row["at"], bool(row["success"]), time.time(), zone)}


def lock_record_view(row: dict) -> dict:
    door = door_name({"name": row["door_name"] or "", "alias": row["door_alias"] or "",
                      "tt_name": row["door_tt_name"] or "", "lock_id": row["lock_id"]})
    return {"id": row["id"], "at": row["at"], "lock_id": row["lock_id"], "door": door,
            # Who: the card made here it matched, else TT Hotel's name for it (a card's or
            # passcode's name, the app user).
            "who": row["card_name"] or row["username"] or "",
            "card_id": row["card_id"], "card_number": row["card_card_number"] or row["card_number"],
            # The kind of card made here it matched (day_pass, contractor...), else None.
            "card_type": row.get("card_type"),
            "how": doorlog.HOW.get(row["record_type"], "Other"), "ok": bool(row["success"]),
            "alarm": row["record_type"] in doorlog.ALARM_TYPES, "alert": row["alert"],
            "problem": bool(row["problem"]), "battery": row["battery"]}


async def admin_door_activity(request: web.Request):
    """The Door activity section: the address for TT Hotel, the last delivery, and the records,
    newest first (one door's, or problems only)."""
    door, before = request.query.get("door", ""), request.query.get("before", "")
    if not re.fullmatch(r"(\d{1,16})?", door) or not re.fullmatch(r"(\d{1,12})?", before):
        return bad_request("invalid value")
    rows = db.lock_records(DOOR_ACTIVITY_PAGE, int(before) if before else None,
                           int(door) if door else None, request.query.get("problems") == "1")
    s = db.settings()
    return web.json_response({
        "address": doorlog.callback_address(s.get("public_url", ""), callback_token()),
        "alert_ha": s.get("wb_lost_card_alert") == "1",
        "last_record": db.last_lock_record(), "delivery": door_delivery,
        "records": [lock_record_view(r) for r in rows], "more": len(rows) == DOOR_ACTIVITY_PAGE})


async def admin_new_door_address(request: web.Request):
    """Make a new address: the old one stops working at once (TT Hotel needs the new one)."""
    db.update_settings({"tthotel_callback_token": doorlog.new_token()})
    db.log(admin_name(request), "Made a new Door activity address",
           detail="Paste it into TT Hotel's Callback URL")
    return web.json_response({"address": doorlog.callback_address(
        db.settings().get("public_url", ""), callback_token())})


# ---------------------------------------------------------------- staff key cards (dashboard)
# Every staff card made (from the staff app or the Staff tab) is listed on the Staff tab, and
# can be revoked (cleared on an encoder, card in hand), reported lost, and then locked out of
# its doors: with a lost card notice tapped on each door, a replacement card that carries the
# old card's cancellation, or (experimental, gateway doors only) a remote lock-out through TT
# Hotel. Key cards made from the Staff tab use the person's department's template.

KEY_CARD_PAGE = 100
DASHBOARD_CARD_PERMS = {"types": ["staff"], "staff_levels": list(wristbands.LEVELS),
                        "doors": {"all": True}, "edit_doors": True, "edit_end_time": False,
                        "max_nights": 0, "max_staff_days": 0, "see_all_cards": True}
REMOTE_TRIES = 3              # a door whose gateway is busy is tried this often at most...
REMOTE_RETRY_SECONDS = 300    # ...this far apart, so a busy gateway never gets a storm of calls
REMOTE_PACE_SECONDS = 6       # doors one at a time, at most ~10 a minute: 20 of TT Hotel's 30
                              # calls a minute, leaving room for wristbands and key cards
REMOTE_MINUTE_WAIT = 60       # held back by our own per-minute cap: wait this long and go on...
REMOTE_MAX_WAITS = 5          # ...this often per door at most (not counted as tries)
REMOTE_TEST_NAME = "Remote lock-out test"
NO_ENCODER = ("No computer has a card encoder set up. Edit a computer and choose its card "
              "encoder first.")
remote_running: set[int] = set()   # cards being locked out remotely right now
# The Test remote lock-out helper's progress. locked_out: the number format the test card was
# locked out with; only then can the admin say it was refused at the door.
remote_test = {"job_id": None, "lock_id": None, "door": {}, "locked_out": None}


def dashboard_encoders() -> list[dict]:
    """The card encoders computers use now, one per address: [{"device_id", "name", "url",
    "status"}] (a computer's own address, typed in or found automatically)."""
    found: dict[str, dict] = {}
    for d in db.devices():
        if d["status"] == "revoked":
            continue
        url = encoder_address(d, rescan=False)
        if not url:
            continue
        if url in found:
            found[url]["name"] = f"{found[url]['name']}, {d['name']}"[:120]
            continue
        found[url] = {"device_id": d["id"], "name": d["name"], "url": url,
                      "port": d["encoder_port"]}
    return [{**e, "status": encoder_status(e["url"])} for e in found.values()]


def chosen_encoder(data: dict, request: web.Request | None = None) -> dict:
    """The encoder a dashboard card job uses: This computer ("encoder": "here", the one the
    dashboard is open on: dashboard_encoder), the computer chosen ("device_id"), or the only
    one there is. Returned like a device row (id, name, encoder_url, encoder_port)."""
    if data.get("encoder") == "here" and request is not None:
        return dashboard_encoder(request, data)
    encoders = dashboard_encoders()
    if not encoders:
        raise json_error(web.HTTPBadRequest, NO_ENCODER)
    wanted = data.get("device_id")
    if wanted in (None, "") and len(encoders) == 1:
        pick = encoders[0]
    else:
        try:
            device = db.device(int(wanted))
        except (TypeError, ValueError, OverflowError):
            device = None
        url = encoder_address(device, rescan=False) if device else None
        pick = next((e for e in encoders if e["url"] == url), None)
        if pick is None:
            raise json_error(web.HTTPBadRequest, "Choose which card encoder to use.",
                             encoders=[{"device_id": e["device_id"], "name": e["name"]}
                                       for e in encoders])
    if encoder_busy(pick["url"]):
        raise json_error(web.HTTPConflict, ENCODER_BUSY)
    return {"id": pick["device_id"], "name": pick["name"], "encoder_url": pick["url"],
            "encoder_port": pick["port"]}


def card_doors(records: list, doors: list[dict] | None = None) -> list[dict]:
    """The doors a card opens (wb_doors rows): building, floor and all-door records expanded to
    the doors TT Hotel lists there now. doors: db.wb_doors(), when already read."""
    doors = db.wb_doors() if doors is None else doors
    found: dict[int, dict] = {}
    for r in records if isinstance(records, list) else []:
        if not isinstance(r, dict):
            continue
        level = r.get("level")
        for d in doors:
            if level == "door":
                hit = d["lock_id"] == r.get("door") or (r.get("door") is None and
                                                       d["mac"] == r.get("mac"))
            elif not d["active"]:
                hit = False
            elif level == "floor":
                hit = d["building_number"] == r.get("building") and d["floor_number"] == r.get("floor")
            elif level == "building":
                hit = d["building_number"] == r.get("building")
            else:
                hit = level == "all"
            if hit:
                found[d["lock_id"]] = d
    return list(found.values())


def key_card_view(card: dict, zone: ZoneInfo | None, doors: bool = False,
                  door_rows: list[dict] | None = None, used: dict | None = None) -> dict:
    """A staff card as the Key cards list shows it. "state" is active, expired, revoked or lost.
    doors: also every door it opens (for a lost card, the doors to lock it out of). door_rows:
    db.wb_doors(), when already read. used: db.cards_last_used() for these cards, when already
    read."""
    now = time.time()
    state = "expired" if card["state"] == "active" and card["expires_at"] <= now else card["state"]
    door_rows = db.wb_doors() if door_rows is None else door_rows
    names = {d["lock_id"]: d for d in door_rows}
    view = {"id": card["id"], "job_id": card["job_id"], "card_number": card["card_number"],
            "holder": card["holder_name"] or card["name"], "name": card["name"],
            "holder_staff_id": card["holder_staff_id"], "department": card["department_name"],
            "holder_archived": card.get("holder_status") == "archived",
            "summary": card["summary"], "made_by": card["actor"],
            "made_at": card["written_at"] or card["made_at"], "source": card["source"] or "staff_app",
            "expires_at": card["expires_at"],
            "expires_label": (wristbands.format_expiry(card["expires_at"], now, zone)
                              if zone else ""),
            "state": state, "state_at": card["state_at"], "state_by": card["state_by"],
            "cancelled_card": card["cancelled_card"], "allow_lockout": bool(card["allow_lockout"]),
            "remote_running": card["id"] in remote_running,
            "last_used": last_used_view((db.cards_last_used([card["id"]]) if used is None
                                         else used).get(card["id"]), zone),
            "lockouts": [{"lock_id": lo["lock_id"],
                          "door": door_name(names[lo["lock_id"]]) if lo["lock_id"] in names
                          else f"Door {lo['lock_id']}",
                          "method": lo["method"], "status": lo["status"], "detail": lo["detail"],
                          "tries": lo["tries"], "at": lo["at"]} for lo in card["lockouts"]]}
    if doors:
        view["doors"] = [{"id": d["lock_id"], "name": door_name(d),
                          "has_gateway": bool(d["has_gateway"])}
                         for d in card_doors(card["records"], door_rows)]
    return view


def remote_state() -> dict:
    """Remote lock-out's settings, and why it can't be used yet (None when it can)."""
    s = db.settings()
    enabled, tested = s.get("wb_remote_lockout") == "1", s.get("wb_remote_lockout_tested", "")
    why = None
    if not enabled:
        why = "Remote lock-out is turned off (Wristbands settings). It's experimental."
    elif not tested:
        why = ("Test remote lock-out on one door first: until it's been seen to work at a door, "
               "it can't be used on lost cards.")
    return {"enabled": enabled, "api": s.get("wb_remote_lockout_api", "reversed"),
            "tested_at": float(tested) if re.fullmatch(r"\d{1,11}(\.\d+)?", tested) else None,
            "ready": why is None, "why": why}


def key_card_or_404(request: web.Request) -> dict:
    try:
        card = db.key_card(int(request.match_info["id"]))
    except (ValueError, OverflowError):
        card = None
    if not card:
        raise json_error(web.HTTPNotFound, "That key card wasn't found.")
    return card


def card_label(card: dict) -> str:
    return f"card {card['card_number']} ({card['holder_name'] or card['name']})"


async def admin_key_cards(request: web.Request):
    """The Key cards list: filter by state, search by name or number, newest first."""
    state = request.query.get("state", "active")
    if state not in KEY_CARD_FILTERS:
        return bad_request("Choose Active, Expired, Revoked, Lost or All.")
    before, holder = request.query.get("before", ""), request.query.get("holder", "")
    if not re.fullmatch(r"(\d{1,12})?", before) or not re.fullmatch(r"(\d{1,12})?", holder):
        return bad_request("invalid value")
    cards = db.key_cards(state, request.query.get("q", "")[:60], KEY_CARD_PAGE,
                         int(before) if before else None, int(holder) if holder else None)
    zone = await wristband_zone()
    door_rows = db.wb_doors()
    used = db.cards_last_used([c["id"] for c in cards])
    return web.json_response({"cards": [key_card_view(c, zone, c["state"] == "lost", door_rows,
                                                      used) for c in cards],
                              "more": len(cards) == KEY_CARD_PAGE,
                              "totals": db.key_card_totals(),
                              "encoders": [{k: e[k] for k in ("device_id", "name", "status")}
                                           for e in dashboard_encoders()],
                              "remote": remote_state()})


async def admin_key_card(request: web.Request):
    card = key_card_or_404(request)
    return web.json_response({"card": key_card_view(card, await wristband_zone(), doors=True)})


async def dashboard_plan(name: str, access: list, days: int, lockout: bool, field_name: str):
    """A staff card plan for the dashboard (every door is in scope, as for an admin), or raise
    the error to show."""
    zone = await wristband_zone()
    if zone is None:
        raise json_error(web.HTTPServiceUnavailable, NO_TIME_ZONE)
    expires_on = (wristbands.local_today(time.time(), zone) + timedelta(days=days)).isoformat()
    request = {"type": "staff", "name": name, "count": 1, "access": access,
               "expires_on": expires_on}
    try:
        plan = wristbands.plan(request, DASHBOARD_CARD_PERMS, wristband_doors(), db.settings(),
                               time.time(), zone)
    except PlanError as err:
        raise json_error(web.HTTPBadRequest, f"{field_name}: {err.message}"
                         if field_name else err.message) from None
    return dataclasses.replace(plan, allow_lockout=bool(lockout)), zone


def start_dashboard_job(request: web.Request, device: dict, **job) -> int:
    """Record a card job from the dashboard and hand it to the encoder. Returns its ID."""
    job_id = db.add_wb_job(staff_id=None, device_id=device["id"], actor=admin_name(request),
                           role=None, ip=request.remote, encoder_url=device["encoder_url"],
                           source="dashboard", **job)
    try:
        start_job(db.wb_job(job_id), device)
    except ValueError as err:   # the runner can't write what was planned: a bug
        log.warning("Card job %s couldn't be started: %s", job_id, err)
        db.cancel_wb_job(job_id, STOPPED_UNEXPECTEDLY)
        raise json_error(web.HTTPInternalServerError,
                         "That card job couldn't be started. Try again.") from None
    return job_id


def access_from_records(records: list) -> list[dict]:
    """A card's records as a staff card request's "access" (to make its replacement)."""
    access = []
    for r in records if isinstance(records, list) else []:
        level = r.get("level") if isinstance(r, dict) else None
        if level == "all":
            access.append({"level": "all"})
        elif level == "building":
            access.append({"level": "building", "building": r.get("building")})
        elif level == "floor":
            access.append({"level": "floor", "building": r.get("building"), "floor": r.get("floor")})
        elif level == "door" and r.get("door") is not None:
            access.append({"level": "door", "door": r.get("door")})
    return access


async def admin_make_key_card(request: web.Request):
    """Make key card (Staff tab): a staff card for this person from their department's template
    (the first of their departments that has one).
    mode: "replace" (their current card is revoked, and the new one carries its cancellation)
    or "add"; asked for when they already have an active key card."""
    member = db.staff(int(request.match_info["id"]))
    if not member:
        raise web.HTTPNotFound()
    data = await read_options(request)
    if db.settings().get("wristbands_enabled") != "1":
        return bad_request(wristbands.TURNED_OFF)
    if member["status"] == "archived":
        return bad_request(ARCHIVED_HOLDER.format(name=member["name"]))
    if member["status"] == "disabled":
        return bad_request(f"{member['name']} is turned off. Turn them on first.")
    dept = db.giving_department(member, "card_access")
    if not dept:
        return bad_request(f"Give {member['name']} a department with a key card template first "
                           "(Staff tab, Departments).")
    if not dept["card_access"]:
        return bad_request(f"The {dept['name']} key card template is damaged. Choose its doors "
                           "again.")
    mode = data.get("mode")
    current = db.key_cards("active", holder_staff_id=member["id"], limit=10)
    zone = await wristband_zone()
    if current and mode not in ("replace", "add"):
        return bad_request(f"{member['name']} already has a key card. Replace it, or add "
                           "another card?", 409, needs_choice=True,
                           current=[key_card_view(c, zone) for c in current])
    old = current[0] if current and mode == "replace" else None
    plan, zone = await dashboard_plan(member["name"], dept["card_access"], dept["card_days"],
                                      dept["card_lockout"], f"{dept['name']} key card")
    if (db.staff(member["id"]) or {"status": "archived"})["status"] == "archived":
        # Archived while the card was being planned (nothing is awaited from here on).
        return bad_request(ARCHIVED_HOLDER.format(name=member["name"]), 409)
    device = chosen_encoder(data, request)
    job_id = start_dashboard_job(
        request, device, card_type="staff", name=plan.name, count=1,
        records=[r.to_dict() for r in plan.records], summary=plan.summary,
        expires_at=plan.expires_at, allow_lockout=plan.allow_lockout,
        holder_staff_id=member["id"], department_id=dept["id"],
        target_card_id=old and old["id"], target_card=old and old["card_number"],
        cancel_until=old and old["expires_at"])
    db.log(admin_name(request), f"Made a key card for {member['name']}",
           detail=f"{plan.summary} · until {plan.expires_label}"
                  + (f" · replaces card {old['card_number']}" if old else ""))
    return web.json_response({"job": job_view(db.wb_job(job_id), zone)})


async def admin_key_card_action(request: web.Request):
    """revoke (card in hand, on an encoder), lost (report it lost), notice (write a lost card
    notice), replace (a replacement card carrying its cancellation) or lockout (remote)."""
    card = key_card_or_404(request)
    action = request.match_info["action"]
    data = await read_options(request)
    who = admin_name(request)
    zone = await wristband_zone()
    expired = card["expires_at"] <= time.time()
    if action == "lost":
        if card["state"] != "active":
            return bad_request(f"This card is already {card['state']}.", 409)
        db.set_card_state(card["id"], "lost", who)
        db.log(who, f"Reported key {card_label(card)} lost")
        return web.json_response({"card": key_card_view(db.key_card(card["id"]), zone, doors=True)})
    if action == "lockout":
        return await remote_lockout_request(request, card, who, zone)
    if db.settings().get("wristbands_enabled") != "1":
        return bad_request(wristbands.TURNED_OFF)
    if action == "revoke":
        if card["state"] != "active":
            return bad_request(f"This card is already {card['state']}.", 409)
        device = chosen_encoder(data, request)
        job_id = start_revoke_job(card, device, actor=who, staff_id=None, role=None,
                                  ip=request.remote, source="dashboard")
        db.log(who, f"Started revoking key {card_label(card)}",
               detail="Place the card on the encoder to clear it")
        return web.json_response({"job": job_view(db.wb_job(job_id), zone)})
    if card["state"] != "lost":
        return bad_request("Report the card lost first.", 409)
    if action == "notice":
        if expired:
            return bad_request("This card has already expired, so it can't open any door.", 409)
        device = chosen_encoder(data, request)
        job_id = start_dashboard_job(
            request, device, card_type="staff", name=card["holder_name"] or card["name"],
            count=1, records=[], summary=f"Lost card notice for card {card['card_number']}",
            expires_at=card["expires_at"], allow_lockout=False,
            holder_staff_id=card["holder_staff_id"], department_id=card["department_id"],
            kind="notice", target_card_id=card["id"], target_card=card["card_number"],
            cancel_until=card["expires_at"])
        db.log(who, f"Started a lost card notice for {card_label(card)}")
        return web.json_response({"job": job_view(db.wb_job(job_id), zone)})
    # replace: the holder's department template if there is one (the first of their departments
    # with one), else what the old card opened.
    member = db.staff(card["holder_staff_id"]) if card["holder_staff_id"] else None
    if member and member["status"] == "archived":
        return bad_request(f"{ARCHIVED_HOLDER.format(name=member['name'])} Write a notice card "
                           "instead.", 409)
    dept = db.giving_department(member, "card_access") if member else None
    if dept and dept["card_access"]:
        access, days, lockout = dept["card_access"], dept["card_days"], dept["card_lockout"]
    else:
        access, lockout = access_from_records(card["records"]), card["allow_lockout"]
        days = max(1, int((card["expires_at"] - time.time()) // 86400)) if not expired else 30
    plan, zone = await dashboard_plan(card["holder_name"] or card["name"], access, days,
                                      lockout, "Replacement card")
    if member and (db.staff(member["id"]) or {"status": "archived"})["status"] == "archived":
        # Archived while the card was being planned (nothing is awaited from here on).
        return bad_request(f"{ARCHIVED_HOLDER.format(name=member['name'])} Write a notice card "
                           "instead.", 409)
    device = chosen_encoder(data, request)
    job_id = start_dashboard_job(
        request, device, card_type="staff", name=plan.name, count=1,
        records=[r.to_dict() for r in plan.records], summary=plan.summary,
        expires_at=plan.expires_at, allow_lockout=plan.allow_lockout,
        holder_staff_id=card["holder_staff_id"], department_id=dept and dept["id"],
        target_card_id=card["id"], target_card=card["card_number"],
        cancel_until=card["expires_at"])
    db.log(who, f"Made a replacement for lost key {card_label(card)}",
           detail=f"{plan.summary} · until {plan.expires_label}")
    return web.json_response({"job": job_view(db.wb_job(job_id), zone)})


def start_revoke_job(card: dict, device: dict, *, actor: str, staff_id: int | None,
                     role: str | None, ip: str | None, source: str,
                     card_type: str = "staff") -> int:
    """Clear one card on an encoder: the runner waits for that card number, clears it, and the
    card (if it's one made here) is marked revoked when it's done."""
    job_id = db.add_wb_job(
        card_type=card_type, name=card["holder_name"] or card["name"], count=1, records=[],
        summary=f"Revoke card {card['card_number']}", expires_at=card["expires_at"],
        allow_lockout=False, staff_id=staff_id, device_id=device["id"], actor=actor, role=role,
        ip=ip, encoder_url=device["encoder_url"], holder_staff_id=card["holder_staff_id"],
        department_id=card["department_id"], source=source, kind="revoke",
        target_card_id=card["id"], target_card=card["card_number"])
    try:
        start_job(db.wb_job(job_id), device)
    except ValueError as err:
        log.warning("Revoke job %s couldn't be started: %s", job_id, err)
        db.cancel_wb_job(job_id, STOPPED_UNEXPECTEDLY)
        raise json_error(web.HTTPInternalServerError,
                         "That card couldn't be revoked. Try again.") from None
    return job_id


async def admin_key_card_lockout(request: web.Request):
    """Tick a door off (a lost card notice or replacement card was tapped on it), or undo it."""
    card = key_card_or_404(request)
    data = await read_json(request)
    try:
        lock_id = int(request.match_info["lock_id"])
    except (ValueError, OverflowError):
        raise web.HTTPNotFound() from None
    method = data.get("method", "notice")
    row = next((lo for lo in card["lockouts"] if lo["lock_id"] == lock_id
                and lo["method"] == method), None)
    if row is None or method not in ("notice", "replacement"):
        return bad_request("That door isn't on this card's lock-out list.", 404)
    done = data.get("done") in (True, 1, "1")
    db.set_lockout(card["id"], lock_id, method, "done" if done else "pending",
                   f"Ticked off by {admin_name(request)}" if done else None)
    door = db.wb_door(lock_id)
    db.log(admin_name(request), f"{'Locked' if done else 'Unmarked'} key {card_label(card)} "
                                f"{'out of' if done else 'at'} {door_name(door) if door else lock_id}",
           detail=f"{method} card" if done else None)
    return web.json_response({"card": key_card_view(db.key_card(card["id"]),
                                                    await wristband_zone(), doors=True)})


# ---- remote lock-out (experimental)

async def lock_out_try(card_id: int, card_number: str, lock_id: int, api: str, attempt: int,
                       last_try: bool, door: dict, may_wait: bool = False) -> str:
    """One try at locking a card number out of one gateway door (tthotel.lock_out_card: two
    calls through its caps). door carries what earlier tries learned: "enrolled" (the card TT
    Hotel added, so only its delete is sent next) and "unsure" (an add may have happened
    unseen, so look for it first). Records progress in wb_lockouts and returns "done", "retry"
    (later may work: try again in REMOTE_RETRY_SECONDS) or "failed"; door["why"] says why not.
    may_wait: when our own per-minute cap held the try back (TT Hotel wasn't asked), return
    "wait" instead, without counting it as a try."""
    db.set_lockout(card_id, lock_id, "remote", "pending", "Asking TT Hotel…", tries=attempt)
    result = await tthotel.lock_out_card(lock_id, card_number, api=api,
                                         card_id=door.get("enrolled"),
                                         find_first=bool(door.get("unsure")))
    result = result if isinstance(result, dict) else {}
    door["enrolled"] = result.get("card_id") or door.get("enrolled")
    door["unsure"] = bool(result.get("unsure")) and door["enrolled"] is None
    if may_wait and result.get("wait") and result.get("status") == "retry":
        db.set_lockout(card_id, lock_id, "remote", "pending",
                       "Waiting a minute: Staff Access has made enough TT Hotel requests for "
                       "this minute.", tries=attempt if door["enrolled"] else attempt - 1)
        return "wait"
    if result.get("status") == "done":
        db.set_lockout(card_id, lock_id, "remote", "done",
                       "Locked out: this card number is retired on this door for good.",
                       tries=attempt)
        return "done"
    door["why"] = str(result.get("message") or "TT Hotel couldn't lock the card out.")
    door["later"] = result.get("status") == "retry"   # trying again later may work
    if door["later"] and not last_try:
        db.set_lockout(card_id, lock_id, "remote", "pending",
                       f"{door['why']} Trying again in {max(1, REMOTE_RETRY_SECONDS // 60)} min.",
                       tries=attempt)
        return "retry"
    db.set_lockout(card_id, lock_id, "remote", "failed", door["why"], tries=attempt)
    return "failed"


async def remote_lockout_run(card: dict, lock_ids: list[int], actor: str) -> None:
    """Lock a lost card out of its gateway doors (background; never raises). Doors go one at a
    time; a door whose gateway is busy is tried again after the others, in another round
    REMOTE_RETRY_SECONDS later, at most REMOTE_TRIES times, so one busy gateway neither holds
    up the rest nor gets a storm of calls."""
    api = db.settings().get("wb_remote_lockout_api", "reversed")
    # A door tried before (a run that failed or was cut short by a restart) may already have the
    # card on it, unseen: look for it there first (one more call) rather than adding it twice.
    tried = {lo["lock_id"] for lo in card.get("lockouts") or []
             if lo["method"] == "remote" and lo["status"] != "done" and (lo.get("tries") or 0) > 0}
    doors = {lock_id: {"unsure": lock_id in tried} for lock_id in lock_ids}
    failed = 0
    try:
        todo = list(lock_ids)
        for attempt in range(1, REMOTE_TRIES + 1):
            again = []
            for i, lock_id in enumerate(todo):
                if i:
                    await asyncio.sleep(REMOTE_PACE_SECONDS)
                for waits in range(REMOTE_MAX_WAITS + 1):
                    outcome = await lock_out_try(card["id"], card["card_number"], lock_id, api,
                                                 attempt, attempt == REMOTE_TRIES,
                                                 doors[lock_id], waits < REMOTE_MAX_WAITS)
                    if outcome != "wait":
                        break
                    await asyncio.sleep(REMOTE_MINUTE_WAIT)
                if outcome == "retry":
                    again.append(lock_id)
                elif outcome == "failed":
                    failed += 1
            if not again:
                break
            await asyncio.sleep(REMOTE_RETRY_SECONDS)
            todo = again
    except Exception as err:  # noqa: BLE001 - a background task never raises
        log.warning("Remote lock-out of card %s stopped: %r", card["card_number"], err)
        failed = len(lock_ids)
    finally:
        remote_running.discard(card["id"])
    db.log(actor, f"Locked key {card_label(card)} out remotely",
           detail=f"{len(lock_ids) - failed} of {len(lock_ids)} gateway doors", ok=not failed)


async def remote_lockout_request(request: web.Request, card: dict, who: str, zone) -> web.Response:
    state = remote_state()
    if not state["ready"]:
        return bad_request(state["why"], 409)
    if card["state"] != "lost":
        return bad_request("Report the card lost first.", 409)
    if card["expires_at"] <= time.time():
        return bad_request("This card has already expired, so it can't open any door.", 409)
    if card["id"] in remote_running:
        return bad_request("This card is being locked out already.", 409)
    done = {lo["lock_id"] for lo in card["lockouts"]
            if lo["method"] == "remote" and lo["status"] == "done"}
    doors = [d["lock_id"] for d in card_doors(card["records"])
             if d["has_gateway"] and d["active"] and d["lock_id"] not in done]
    if not doors:
        return bad_request("None of this card's doors has a gateway still to lock it out of. "
                           "Use a lost card notice or a replacement card instead.")
    db.add_lockouts(card["id"], doors, "remote")
    remote_running.add(card["id"])
    background(remote_lockout_run(card, doors, who), "lock a card out remotely")
    db.log(who, f"Started remote lock-out of key {card_label(card)}",
           detail=f"{len(doors)} gateway doors; the number is retired on them for good")
    return web.json_response({"card": key_card_view(db.key_card(card["id"]), zone, doors=True)})


async def admin_remote_test(request: web.Request):
    """The Test remote lock-out helper: start (make a test card for one gateway door), lockout
    (lock that card out remotely), passed (it was refused at the door: remote lock-out may be
    used) or failed (it still opened: switch the number format, then try again)."""
    action = request.match_info["action"]
    data = await read_options(request)
    who = admin_name(request)
    s = db.settings()
    if s.get("wb_remote_lockout") != "1":
        return bad_request("Turn on remote lock-out in Wristbands settings first.")
    if action == "start":
        door = None
        try:
            door = db.wb_door(int(data.get("door_id")))
        except (TypeError, ValueError, OverflowError):
            pass
        if not door or not door["active"] or not door["has_gateway"]:
            return bad_request("Choose a door that has a gateway.")
        plan, zone = await dashboard_plan(REMOTE_TEST_NAME, [{"level": "door",
                                                              "door": door["lock_id"]}],
                                          1, False, "")
        device = chosen_encoder(data, request)
        job_id = start_dashboard_job(
            request, device, card_type="staff", name=REMOTE_TEST_NAME, count=1,
            records=[r.to_dict() for r in plan.records], summary=plan.summary,
            expires_at=plan.expires_at, allow_lockout=False)
        remote_test.update(job_id=job_id, lock_id=door["lock_id"], door={}, locked_out=None)
        db.log(who, "Started a remote lock-out test", detail=door_name(door))
        return web.json_response({"job": job_view(db.wb_job(job_id), zone)})
    if action == "lockout":
        job = db.wb_job(remote_test["job_id"]) if remote_test["job_id"] else None
        made = next((c for c in (job or {}).get("cards", []) if c["status"] == "written"), None)
        if not made:
            return bad_request("Make the test card first, and check that it opens the door.", 409)
        card = db.key_card(made["id"])
        if card is None or card["id"] in remote_running:
            return bad_request("Make the test card again.", 409)
        db.add_lockouts(card["id"], [remote_test["lock_id"]], "remote")
        remote_running.add(card["id"])
        # One try while the admin waits; a busy gateway means clicking again a bit later (what
        # the try learned is kept, so a card already added is only deleted next time).
        door = remote_test.setdefault("door", {})
        api = s.get("wb_remote_lockout_api", "reversed")
        try:
            outcome = await lock_out_try(card["id"], card["card_number"], remote_test["lock_id"],
                                         api, 1, True, door)
        finally:
            remote_running.discard(card["id"])
        problem = None if outcome == "done" else door.get("why")
        db.log(who, "Remote lock-out test: locked the test card out", detail=problem,
               ok=problem is None)
        if problem:
            return bad_request(problem + (" Wait a few minutes, then click it again."
                                          if door.get("later") else ""), 502)
        db.set_card_state(card["id"], "revoked", who)   # its holder says it was the test
        remote_test["locked_out"] = api
        return web.json_response({"ok": True, "card_number": card["card_number"]})
    if action == "passed":
        if remote_test.get("locked_out") != s.get("wb_remote_lockout_api", "reversed"):
            return bad_request("Lock the test card out first, then check that the door refuses "
                               "it.", 409)
        remote_test.update(job_id=None, lock_id=None, door={}, locked_out=None)
        db.update_settings({"wb_remote_lockout_tested": str(int(time.time()))})
        db.log(who, "Remote lock-out tested: the card was refused at the door",
               detail=f"number format: {s.get('wb_remote_lockout_api', 'reversed')}")
        return web.json_response({"remote": remote_state()})
    # failed: the card still opened the door, so try the other way of sending the number.
    api = "plain" if s.get("wb_remote_lockout_api") == "reversed" else "reversed"
    db.update_settings({"wb_remote_lockout_api": api, "wb_remote_lockout_tested": ""})
    remote_test.update(job_id=None, lock_id=None, door={}, locked_out=None)
    db.log(who, "Remote lock-out test: the card still opened the door",
           detail=f"switched the number format to {api}; make a new test card", ok=False)
    return web.json_response({"remote": remote_state()})


async def admin_key_card_job(request: web.Request):
    """A card job's live progress, for the dashboard's Make key card, Revoke and notice dialogs."""
    try:
        job = db.wb_job(int(request.match_info["id"]))
    except (ValueError, OverflowError):
        job = None
    if not job:
        raise json_error(web.HTTPNotFound, "That card job wasn't found.")
    return web.json_response({"job": job_view(job, await wristband_zone())})


async def admin_key_card_job_action(request: web.Request):
    """Try again, skip this card, or stop (any card job: admins may stop anything)."""
    try:
        job = db.wb_job(int(request.match_info["id"]))
    except (ValueError, OverflowError):
        job = None
    if not job:
        raise json_error(web.HTTPNotFound, "That card job wasn't found.")
    action = request.match_info["action"]
    data = await optional_json(request)
    if action != "cancel" and job["status"] != "needs_attention":
        return bad_request("This job isn't waiting for a decision any more.", 409)
    refusal = (dashboard_job_refusal(job, data) or archived_holder_refusal(job)
               if action != "cancel" else None)
    if refusal:
        return bad_request(refusal, 409)
    if job["status"] in WB_ACTIVE:
        decide_job(job, action)
    return web.json_response({"job": job_view(db.wb_job(job["id"]), await wristband_zone())})


# ---------------------------------------------------------------- HR portal (dashboard)
# The HR portal (hr/) runs in this process on its own port and address. From the dashboard the
# owner turns it on or off, sets its address, chooses the HR admins (and shows an HR admin's
# setup QR code), uses the emergency controls, unlocks it with the recovery key, and sees its
# security events (never who looked at what). Its events go to its own audit log, never to
# Staff Access's Activity log or the Home Assistant logbook.

def hr_departments() -> list[dict]:
    return [{"smart_group_id": d["smart_group_id"], "name": d["name"], "hidden": bool(d["hidden"])}
            for d in db.departments()]


hr_service = HRService(
    DATA_DIR, main_settings=lambda: db.settings(), connecteam=connecteam,
    departments=hr_departments, group_names=lambda: connecteam.known_group_names(),
    from_tunnel=lambda remote: from_tunnel(remote), client_ip=lambda request: client_ip(request),
    property_name=lambda: db.settings().get("property_name", ""),
    icon=lambda: branding.icon(192, db.settings()), version=VERSION,
    reseal=lambda: db.reseal_settings())


class HRSealer:
    """Database.sealer: the HR portal's key as it is now. While it's kept out of backups, the
    Google script's Web app URL and secret are stored sealed with it (hr/crypto.py), so a
    stolen Home Assistant backup alone can't reach HR's files."""

    def open_settings(self, values: dict) -> dict:
        return hr_service.keyring.open_settings(values)

    def seal_settings(self, values: dict) -> dict:
        return hr_service.keyring.seal_settings(values)

    def resealed(self, stored: dict) -> dict:
        return hr_service.keyring.resealed(stored)


Database.sealer = HRSealer()
db.reseal_settings()   # read again, opened (and sealed, if an older version left them plain)


def hr_key_locked() -> web.Response | None:
    """A refusal while the HR key is locked (after a restore, until the recovery key is typed):
    the HR connections' secrets are sealed with it, so they can't be read or changed yet."""
    if hr_service.keyring.status() == "locked":
        return bad_request(hr_crypto.LOCKED, 409)
    return None


HOST_RE = re.compile(r"^(?=.{4,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
                     r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
HR_OFF = "Turn the HR portal on first."
HR_SYNC_GAP = 60
MAX_HR_ADMINS = 10
hr_synced = [0.0]


def owner_name(request: web.Request) -> str:
    return f"Owner ({admin_name(request)})"[:120]


def clean_hr_host(value) -> str:
    """The HR portal's address as typed: a host name like hr.yourdomain.com ("" for none)."""
    text = re.sub(r"^https?://", "", str(value or "").strip().lower()).rstrip("/")
    if text and not HOST_RE.match(text):
        raise ValueError("Type the HR address like hr.yourdomain.com (no https:// and no path).")
    staff = re.sub(r"^https?://", "", db.settings().get("public_url", "").lower()).split(":")[0]
    if text and text == staff.rstrip("/"):
        raise ValueError("The HR portal needs its own address, not the staff app's.")
    return text


def hr_refusal() -> web.Response | None:
    """None while HR runs; otherwise why the dashboard can't do an HR action now."""
    status = hr_service.status()
    if status == "on":
        return None
    if status in ("locked", "lost"):
        return bad_request(hr_crypto.LOCKED if status == "locked" else hr_crypto.LOST, 409)
    return bad_request(HR_OFF, 409)


def hr_person_view(p: dict, roles: set[str]) -> dict:
    return {"id": p["id"], "name": p["name"], "totp_on": bool(p["totp_on"]),
            "locked": hr_signin.locked(p), "access_ended": bool(p["access_ended"]),
            "archived": bool(p["archived"]), "has_code": bool(p["kiosk_hash"]),
            "last_signin_at": p["last_signin_at"], "roles": sorted(roles)}


def hr_brief() -> dict:
    """What /api/state says about HR: whether it's on, and its Connecteam reads today (it uses
    Staff Access's connection, in a small lane of its own)."""
    return {"status": hr_service.status(), "enabled": db.settings().get("hr_enabled") == "1",
            "connecteam": connecteam.hr_status()}


def hr_connection_log(request: web.Request, action: str, detail: str | None = None,
                      ok: bool = True) -> None:
    """A change to an HR connection (Settings > Connections): in the activity log like the other
    connections (it says nothing about anyone's records), and among the HR portal's security
    events while it runs."""
    db.log(admin_name(request), action, detail=detail, ok=ok)
    if hr_service.running:
        hr_audit.record(hr_service.db, action, actor_id="owner", actor=owner_name(request),
                        detail=detail, security=True)


async def admin_hr(request: web.Request):
    """The HR portal tab."""
    s = db.settings()
    state = {**hr_brief(), "host": s.get("hr_host", ""), "url": hr_service.url(),
             "port": HR_PORT, "connecteam_on": s.get("connecteam_enabled") == "1",
             "key": {"kept_out": hr_service.keyring.kept_out(),
                     "state": hr_service.keyring.status()},
             "admins": [], "people": 0, "sync": None, "events": []}
    if hr_service.running:
        hdb = hr_service.db
        roles = hdb.all_roles()
        state["admins"] = [hr_person_view(p, roles.get(p["id"], set()))
                           for p in map(hdb.person, hdb.with_role(hr_access.HR_ADMIN)) if p]
        state["people"] = hdb.conn.execute(
            "SELECT COUNT(*) FROM people WHERE archived = 0").fetchone()[0]
        sync = hr_service.sync_state
        state["sync"] = {"at": sync["at"], "error": sync["error"], "result": sync["result"],
                         "warning": hdb.setting("sync_warning") or None}
        state["events"] = hr_audit.lines(hdb, 50, security=True)
    return web.json_response(state)


async def admin_hr_settings(request: web.Request):
    """Turn the HR portal on or off, and set its address."""
    data = await read_json(request)
    current = db.settings()
    try:
        host = clean_hr_host(data["hr_host"]) if "hr_host" in data else current.get("hr_host", "")
    except ValueError as err:
        return bad_request(str(err))
    want = (data.get("hr_enabled") in (True, "1", "on", "true") if "hr_enabled" in data
            else current.get("hr_enabled") == "1")
    if want and not host:
        return bad_request("Type the HR portal's address first (the Cloudflare Tunnel hostname "
                           "pointing at port 8788).")
    if want and current.get("connecteam_enabled") != "1":
        return bad_request("Turn on Connecteam first (Settings → Connections): the HR portal "
                           "takes its people from it.")
    host_changed = host != current.get("hr_host", "")
    db.update_settings({"hr_host": host, "hr_enabled": "1" if want else "0"})
    who = owner_name(request)
    if want and not hr_service.running:
        if hr_service.start() == "on":
            hr_audit.record(hr_service.db, "Turned the HR portal on", actor_id="owner", actor=who,
                            detail=host, security=True)
    elif not want and hr_service.running:
        hr_audit.record(hr_service.db, "Turned the HR portal off", actor_id="owner", actor=who,
                        security=True)
        await hr_service.stop()
    elif hr_service.running and host_changed:
        hr_audit.record(hr_service.db, "Changed the HR address", actor_id="owner", actor=who,
                        detail=host, security=True)
    return web.json_response({"ok": True, "status": hr_service.status()})


async def admin_hr_people(request: web.Request):
    """People to choose HR admins from (from Connecteam, as HR last synced them)."""
    if refusal := hr_refusal():
        return refusal
    q = " ".join(request.query.get("q", "").split()).casefold()[:60]
    hdb = hr_service.db
    roles = hdb.all_roles()
    return web.json_response({"people": [
        hr_person_view(p, roles.get(p["id"], set())) for p in hdb.people(archived=False)
        if not q or q in p["name"].casefold()][:500]})


async def admin_hr_admins(request: web.Request):
    if refusal := hr_refusal():
        return refusal
    data = await read_json(request)
    ids = data.get("person_ids")
    hdb = hr_service.db
    if (not isinstance(ids, list) or len(ids) > MAX_HR_ADMINS
            or not all(isinstance(i, str) and (p := hdb.person(i)) and not p["archived"]
                       for i in ids)):
        return bad_request(f"Choose up to {MAX_HR_ADMINS} people from the list.")
    added, removed = hr_service.set_admins(ids, owner_name(request))
    return web.json_response({"ok": True, "added": added, "removed": removed})


async def admin_hr_setup_code(request: web.Request):
    """An HR admin's setup QR code, shown on the dashboard (the first HR admin has no one else to
    make it). It works once, for 15 minutes."""
    if refusal := hr_refusal():
        return refusal
    hdb = hr_service.db
    pid = request.match_info["pid"]
    person = hdb.person(pid)
    if not person or hr_access.HR_ADMIN not in hdb.roles_of(pid) or person["archived"]:
        return bad_request("Only HR admins get a setup code here.", 404)
    if not hr_service.url():
        return bad_request("Save the HR portal's address first.")
    token, expires = hr_signin.new_setup_token(hdb, pid, "owner", "owner")
    hdb.update_person(pid, access_ended=0)
    url = f"{hr_service.url()}/setup/{token}"
    hr_audit.record(hdb, "Made a setup QR code", actor_id="owner", actor=owner_name(request),
                    target_id=pid, target=person["name"], security=True)
    return web.json_response({"url": url, "qr": hr_server.qr_data_uri(url), "expires_at": expires})


async def admin_hr_person_action(request: web.Request):
    """Emergency controls: reset or unlock anyone's HR sign-in."""
    if refusal := hr_refusal():
        return refusal
    pid = request.match_info["pid"]
    if not hr_service.db.person(pid):
        return bad_request("That person isn't in the HR portal.", 404)
    if request.match_info["action"] == "reset":
        hr_service.reset_signin(pid, "owner", owner_name(request))
    else:
        hr_service.unlock_person(pid, "owner", owner_name(request))
    return web.json_response({"ok": True})


async def admin_hr_sign_out_everyone(request: web.Request):
    if refusal := hr_refusal():
        return refusal
    return web.json_response({"ended": hr_service.sign_out_everyone("owner", owner_name(request))})


async def admin_hr_unlock_key(request: web.Request):
    data = await read_json(request)
    if hr_service.keyring.status() != "locked":
        return bad_request("The HR portal isn't locked.", 409)
    try:
        await hr_service.unlock_key(str(data.get("recovery_key") or ""))
    except ValueError as err:
        return bad_request(str(err))
    return web.json_response({"ok": True, "status": hr_service.status()})


async def admin_hr_sync(request: web.Request):
    if refusal := hr_refusal():
        return refusal
    if time.time() - hr_synced[0] < HR_SYNC_GAP:
        return bad_request("People were synced less than a minute ago. Wait a moment.", 429)
    hr_synced[0] = time.time()
    result = await hr_service.sync_people(fresh=True)
    if result is None:
        return bad_request(hr_service.sync_state["error"] or "Couldn't sync people.", 502)
    return web.json_response(result)


# ---------------------------------------------------------------- reports (dashboard)

def reports_lane_state() -> dict:
    """The reports lane of the Connecteam budget, for the Reports tab."""
    now = time.time()
    return {"used_today": connecteam.reports_usage()["count"],
            "daily_cap": connecteam_module.REPORTS_DAILY_CAP,
            "minute_cap": connecteam_module.REPORTS_MINUTE_CAP,
            "paused": (connecteam.reports_pause_reason
                       if connecteam.reports_paused_until > now else None)}


async def admin_reports(request: web.Request):
    access = db.reports()
    departments = {d["id"] for d in db.departments()}
    listed = []
    for key, report in sorted(REPORTS.items(), key=lambda kv: kv[1].name.lower()):
        if report.hidden:
            continue
        a = access.get(key) or db.report(key)
        listed.append({**report.public(), "enabled": a["enabled"],
                       "view": [i for i in a["view"] if i in departments],
                       "manage": [i for i in a["manage"] if i in departments],
                       "uploads": [{"key": u["key"], "name": u["name"],
                                    "files": reports_kit.uploads(DATA_DIR, key, u["key"], text=False)}
                                   for u in report.uploads]})
    return web.json_response({"reports": listed, "lane": reports_lane_state()})


def department_ids(raw, known: set[int]) -> list[int]:
    if not isinstance(raw, list) or len(raw) > 1000:
        raise ValueError("Pick departments from the list.")
    ids = sorted({i for i in raw if isinstance(i, int) and not isinstance(i, bool)})
    if any(i not in known for i in ids):
        raise ValueError("One of those departments doesn't exist any more. Reload the page.")
    return ids


async def admin_update_report(request: web.Request):
    report = REPORTS.get(request.match_info["key"])
    if not report or report.hidden:
        raise web.HTTPNotFound()
    data = await read_json(request)
    ordered = db.departments()
    known = {d["id"] for d in ordered if not d["hidden"]}
    hidden = {d["id"] for d in ordered if d["hidden"]}
    before = db.report(report.key)
    fields = {}
    try:
        if "enabled" in data:
            fields["enabled"] = data["enabled"] is True
        for name in ("view", "manage"):
            if name in data:
                # Hidden departments aren't offered: the ones chosen before hiding are kept
                # (they give nothing until shown again).
                fields[name] = sorted({*department_ids(data[name], known),
                                       *(i for i in before[name] if i in hidden)})
    except ValueError as err:
        return bad_request(str(err))
    db.set_report(report.key, admin_name(request), **fields)
    after = db.report(report.key)
    names = {d["id"]: d["name"] for d in db.departments()}
    said = lambda ids: ", ".join(names[i] for i in ids) or "nobody"
    detail = (f"{'On' if after['enabled'] else 'Off'}. View: {said(after['view'])}. "
              f"Manage: {said(after['manage'])}.")
    db.log(admin_name(request), f"Changed report {report.name}",
           detail=detail if after != before else None)
    return web.json_response({"ok": True})


# ---------------------------------------------------------------- export, import, backup

def download(data: dict, kind: str) -> web.Response:
    name = f"staff-portal-{kind}-{time.strftime('%Y-%m-%d')}.json"
    return web.Response(text=json.dumps(data, indent=1), content_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


async def admin_export(request: web.Request):
    kind = request.match_info["kind"]
    maker = {"layout": transfer.export_layout, "roles": transfer.export_roles,
             "staff": transfer.export_staff, "departments": transfer.export_departments}[kind]
    db.log(admin_name(request), f"Exported {kind}")
    return download(maker(db, VERSION), kind)


async def admin_import(request: web.Request):
    kind = request.match_info["kind"]
    data = await read_json(request)
    importer = {"roles": transfer.import_roles, "staff": transfer.import_staff,
                "departments": transfer.import_departments}[kind]
    try:
        result = importer(db, data.get("file"), str(data.get("mode") or ""))
    except ValueError as err:
        return bad_request(str(err))
    for staff_id in result.pop("signed_out", []):
        staff_sessions_end(staff_id)   # their PIN changed, or they're turned off
    db.log(admin_name(request), f"Imported {kind}",
           detail=f"{result['added']} added, {result['replaced']} replaced, {result['skipped']} skipped")
    if kind == "departments" and staff_sync_enabled():
        sync_staff_soon()
    return web.json_response(result)


# Room for a full backup with its logs, as the dashboard uploads it: the file as picked. As
# downloaded (gzip-compressed) that's a few megabytes. An older or unpacked backup is plain JSON:
# with a full activity log and the most card jobs the add-on keeps, about 70 MB with 2 doors on
# each card, more with more doors (transfer.MAX_BACKUP_BYTES), so a bigger one goes up packed.
MAX_ADMIN_BODY = 96 * 1024 * 1024
BUSY = "Staff Access is busy with another backup or restore. Try again in a moment."
transfer_running = [False]


def one_at_a_time(handler):
    """For backups, restores and migration files: each holds a whole backup (with its logs,
    possibly tens of megabytes) in memory, so only one runs at a time; another is refused."""
    @functools.wraps(handler)
    async def run(request: web.Request):
        if transfer_running[0]:
            return bad_request(BUSY, 409)
        transfer_running[0] = True
        try:
            return await handler(request)
        finally:
            transfer_running[0] = False
    return run


def attachment(body: bytes, name: str, content_type: str) -> web.Response:
    return web.Response(body=body, content_type=content_type, headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


def read_everything(build):
    """build(database) on a read-only connection of its own, as one moment: for a worker thread,
    so reading everything (logs and all, which takes seconds on a big install) doesn't hold up
    the add-on."""
    reader = db.reader()
    try:
        return build(reader)
    finally:
        reader.close()


def backup_of(database) -> dict:
    return transfer.make_backup(database, branding, VERSION)


@one_at_a_time
async def admin_backup(request: web.Request):
    db.log(admin_name(request), "Downloaded a full backup")
    body = await asyncio.to_thread(lambda: transfer.pack_backup(read_everything(backup_of)))
    return attachment(body, f"staff-portal-backup-{time.strftime('%Y-%m-%d')}.json.gz",
                      "application/gzip")


async def backup_from_request(request: web.Request):
    """The backup a restore (or its preview) is about, read but not yet checked. Raises
    ValueError with a message for the admin. It comes as:
    - an upload (multipart, field "file"): a backup file as downloaded, gzip-compressed or plain;
    - {"safety_backup": name}: a copy saved here before an earlier restore;
    - {"backup": {...}}: the backup itself, already read as JSON (unpacked, if it was)."""
    if request.content_type == "multipart/form-data":
        raw = (await read_upload(request, {"file": MAX_ADMIN_BODY})).get("file")
        if not raw:
            raise ValueError("Choose a backup file.")
        return await asyncio.to_thread(transfer.read_backup_file, raw)
    body = await read_json(request)
    if "safety_backup" in body:
        path = transfer.safety_backup_path(DATA_DIR, str(body["safety_backup"] or ""))
        try:
            if not path:
                raise OSError
            return await asyncio.to_thread(transfer.read_backup_path, path)
        except OSError:
            raise ValueError("That saved copy couldn't be read.") from None
    return body.get("backup")


async def check_backup(data) -> dict:
    # Checking is pure Python work on what was sent (and decoding the logo is image work), so
    # keep it off the event loop; what it needs from the database is read here first.
    return await asyncio.to_thread(transfer.clean_backup, data, clean_settings, db.settings(),
                                   branding.clean_logo, db.known_tokens())


async def save_safety_backup() -> str:
    """Save a copy of everything here (logs too) before a restore or migration replaces it, so
    it can be undone. Read, packed and written off the event loop."""
    return await asyncio.to_thread(
        lambda: transfer.save_safety_backup(DATA_DIR, read_everything(backup_of)))


async def unlink_dropped(tokens: list[str]) -> None:
    for token in tokens:
        # A link that didn't survive the restore would otherwise stay valid in Home Assistant.
        try:
            await ha.unlink_user(token)
        except HomeAssistantError as err:
            log.warning("Couldn't revoke a Home Assistant sign-in after restore: %s", err)


def added_note(added: dict) -> str:
    """For the activity log: " Added 120 activity log lines and 4 card jobs from it.", or ""
    when a restore added none (db.replace_everything's counts)."""
    parts = [f"{added[key]} {word if added[key] != 1 else word[:-1]}"
             for key, word in (("activity", "activity log lines"), ("wb_jobs", "card jobs"))
             if added.get(key)]
    return f" Added {' and '.join(parts)} from it." if parts else ""


def archived_here() -> list[dict]:
    """The people archived here (name, archived_at, archived_by), for transfer.keep_archived."""
    return [dict(r) for r in db.conn.execute(
        "SELECT name, archived_at, archived_by FROM staff WHERE status = 'archived'")]


@one_at_a_time
async def admin_restore_preview(request: web.Request):
    try:
        data = await backup_from_request(request)
        snap = await check_backup(data)
    except ValueError as err:
        return bad_request(str(err))
    # People archived here since the backup was made stay archived (kept_archived).
    return web.json_response({**transfer.backup_summary(data), "needs_setup": snap["needs_setup"],
                              "kept_archived": transfer.keep_archived(snap, archived_here())})


@one_at_a_time
async def admin_restore(request: web.Request):
    try:
        data = await backup_from_request(request)
        snap = await check_backup(data)
    except ValueError as err:
        return bad_request(str(err))
    del data   # it can be big; only the checked copy is needed from here
    # Keep what's being replaced, so a restore can be undone by restoring this copy.
    safety = await save_safety_backup()
    api = db.settings().get("wb_remote_lockout_api", "reversed")
    # Nothing is awaited from here until it's replaced: people archived here (since the backup
    # was made) stay archived, as a staff import keeps them.
    kept_archived = transfer.keep_archived(snap, archived_here())
    dropped_links, added = db.replace_everything(snap)
    if db.settings().get("wb_remote_lockout_api", "reversed") != api:
        # Remote lock-out was tested with the other number format, not this one.
        db.update_settings({"wb_remote_lockout_tested": ""})
    remote_test.update(job_id=None, lock_id=None, door={}, locked_out=None)
    await unlink_dropped(dropped_links)
    for device in snap["devices"]:
        if device["status"] == "pending":
            db.new_enroll_code(device["id"])  # setup codes never travel in backups
    if snap["logo"]:
        branding.write_logo(snap["logo"])
    else:
        branding.remove_logo()
    pin_sessions.clear()  # everyone with a PIN signs in again
    recheck_links()       # so every encoder link closes too
    connecteam.forget()   # the restored settings may point somewhere else
    tthotel.forget()
    made = time.strftime("%b %d, %Y", time.localtime(snap["exported_at"]))
    db.log(admin_name(request), "Restored a backup",
           detail=f"Backup from {made}.{added_note(added)} What it replaced was saved as {safety}." +
           (f" Kept archived: {', '.join(kept_archived)}." if kept_archived else "") +
           (f" Revoked {len(dropped_links)} Home Assistant sign-in(s) no longer linked." if dropped_links else ""))
    return web.json_response({"ok": True, "safety_backup": safety, "added": added,
                              "kept_archived": kept_archived})


async def admin_safety_backups(request: web.Request):
    return web.json_response({"backups": transfer.safety_backups(DATA_DIR)})


async def admin_safety_backup(request: web.Request):
    path = transfer.safety_backup_path(DATA_DIR, request.match_info["name"])
    if not path:
        raise web.HTTPNotFound()
    with open(path, "rb") as f:
        body = f.read()
    return attachment(body, os.path.basename(path),
                      "application/gzip" if path.endswith(".gz") else "application/json")


# ---------------------------------------------------------------- migrate to another install

SET_UP_NOUNS = {"staff": ("staff member", "staff members"),
                "devices": ("phone or computer", "phones and computers"),
                "roles": ("role", "roles"), "areas": ("area", "areas"),
                "departments": ("department", "departments"),
                "wb_jobs": ("card job", "card jobs"), "pages": ("layout page", "layout pages")}


def not_fresh() -> str | None:
    """Why this install can't take a migration (it's been set up), or None if it's fresh."""
    found = db.setup_found()
    if not found:
        return None
    listed = [f"{n} {SET_UP_NOUNS[key][n != 1]}" for key, n in found.items()]
    what = listed[0] if len(listed) == 1 else f"{', '.join(listed[:-1])} and {listed[-1]}"
    return (f"This install already has {what}. A migration replaces everything, so it only goes "
            "onto a fresh install of the add-on: uninstall this one and install it again, then "
            "restore the migration file.")


@one_at_a_time
async def admin_migrate_create(request: web.Request):
    """Create migration file (Backup tab > Migrate): everything here, device tokens and secrets
    included, encrypted under a one-time code (migrate.py). The file comes back in this answer
    with the code, for the dashboard to save; the code is never stored, logged or put in an
    address, and nothing of either is kept here."""
    if refusal := hr_service.migration_blocker():
        return bad_request(refusal, 409)   # HR uploads still on their way to Google Drive

    def build(database):
        snap = migrate.make_snapshot(database, branding, VERSION)
        snap["migrate"]["hr"] = hr_service.export()   # encrypted already; None if HR was never on
        # Files uploaded to reports: never in backups (staff data that changes weekly), but a
        # migration moves everything.
        snap["migrate"]["report_uploads"] = reports_kit.all_uploads(DATA_DIR, REPORTS)
        return snap
    db.log(admin_name(request), "Created a migration file",
           detail="It opens for 24 hours, only with the code shown when it was made.")
    code = migrate.new_code()
    try:
        file = await asyncio.to_thread(lambda: migrate.seal(read_everything(build), code, VERSION))
    except ValueError as err:   # the HR key is missing, or HR's records are too big to carry
        return bad_request(str(err), 409)
    return web.json_response(
        {"code": code, "file": file, "expires_at": file["expires_at"],
         "filename": f"staff-portal-migration-{time.strftime('%Y-%m-%d-%H%M')}.json"},
        headers={"Cache-Control": "no-store"})


async def migration_from_request(request: web.Request) -> tuple:
    """(the migration file as sent, the code typed), from an upload (multipart: "file" and
    "code") or JSON ({"file": the file read as JSON, or its text, "code": ...}). Raises
    ValueError with a message for the admin."""
    if request.content_type == "multipart/form-data":
        fields = await read_upload(request, {"file": MAX_ADMIN_BODY, "code": 200})
        file, code = fields.get("file"), fields.get("code", b"").decode("utf-8", "replace")
    else:
        body = await read_json(request)
        file, code = body.get("file"), body.get("code")
    if not file:
        raise ValueError("Choose the migration file.")
    return file, migrate.clean_code(code)


HR_NOT_RESTORED = ("The HR portal's records couldn't be written here (is the disk full?), so the HR "
                   "portal is off. Keep the old install: its HR records are still there.")


@one_at_a_time
async def admin_migrate_restore(request: web.Request):
    """Restore from migration file: the file and its code (migration_from_request). Everything
    here is replaced by what the file holds, device tokens and secrets included, so phones and
    computers stay signed in. Only onto a fresh install (409 otherwise), within the file's 24
    hours."""
    try:
        file, code = await migration_from_request(request)
    except ValueError as err:
        return bad_request(str(err))
    if refusal := not_fresh():
        return bad_request(refusal, 409)
    try:
        data, header = await asyncio.to_thread(migrate.open_file, file, code, VERSION)
        del file
        snap = await asyncio.to_thread(migrate.clean_snapshot, data, clean_settings, db.settings(),
                                       branding.clean_logo)
        report_uploads = reports_kit.clean_uploads(data["migrate"].get("report_uploads"), REPORTS)
    except ValueError as err:
        return bad_request(str(err))
    summary = transfer.backup_summary(data)
    del data   # it can be big; only the checked copy is needed from here
    safety = await save_safety_backup()
    if refusal := not_fresh():   # something was set up while the file was being opened
        return bad_request(refusal, 409)
    dropped_links, added = db.replace_everything(snap)
    remote_test.update(job_id=None, lock_id=None, door={}, locked_out=None)
    await unlink_dropped(dropped_links)
    await hr_service.stop()
    hr_problem = None
    if snap["migrate"].get("hr"):
        try:
            hr_service.take_migration(snap["migrate"]["hr"])   # the HR database and its key
        except (ValueError, OSError) as err:
            # Checked already (migrate._hr_part), so only the disk can fail here. HR stays off
            # rather than starting empty with a new key, and the owner is told.
            log.warning("Couldn't restore the HR portal from the migration file: %s", err)
            db.update_settings({"hr_enabled": "0"})
            hr_problem = HR_NOT_RESTORED
    if hr_service.enabled():
        hr_service.start()   # locked until the recovery key is typed, if it was kept out
    try:
        await asyncio.to_thread(reports_kit.restore_uploads, DATA_DIR, report_uploads, REPORTS)
    except OSError as err:
        log.warning("Couldn't restore the reports' uploaded files: %s", err)
    for device in snap["devices"]:
        if device["status"] == "pending" and device["id"] not in snap["migrate"]["setup_codes"]:
            db.new_enroll_code(device["id"])
    if snap["logo"]:
        branding.write_logo(snap["logo"])
    else:
        branding.remove_logo()
    pin_sessions.clear()  # devices stay signed in; people type their PIN again
    connecteam.forget()   # new settings and secrets: nothing cached from before applies
    tthotel.forget(saved_stops=True)   # but a pause or stop TT Hotel asked for still holds
    signed_in = sum(1 for d in snap["devices"] if d["token_hash"])
    made = time.strftime("%b %d, %Y %H:%M", time.localtime(header["created_at"]))
    db.log(admin_name(request), "Migrated from another install",
           detail=f"Migration file made {made} by Staff Portal {header['app_version'][:20]}. "
                  f"{signed_in} phone(s) and computer(s) stay signed in.{added_note(added)} "
                  f"What it replaced was saved as {safety}.{' ' + hr_problem if hr_problem else ''}")
    return web.json_response({"ok": True, "summary": {
        **summary, "made_at": header["created_at"], "app_version": header["app_version"][:20],
        "signed_in": signed_in, "needs_setup": snap["needs_setup"], "added": added,
        "safety_backup": safety, "hr_problem": hr_problem}})


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


# ---------------------------------------------------------------- tasks from forms (dashboard)

def task_forms_view() -> list[dict]:
    departments = task_departments()
    return [{**f, "department": "Everyone" if f["department_id"] is None
             else departments.get(f["department_id"], "Old department")}
            for f in db.task_forms()]


def task_forms_body() -> dict:
    """The chosen forms, and what checking them and the Connecteam boards costs: how often, and
    about how many of the reports lane's Connecteam requests a day (they share
    tasks.FORM_DAILY_REQUESTS), and the task sync's state."""
    weight = ctsync.poll_weight(db.task_forms())
    departments = task_departments()
    sync = ctsync.status()
    sync["boards"] = [{**b, "department": "Everyone" if b["department_id"] is None
                       else departments.get(b["department_id"], "Old department")}
                      for b in sync["boards"]]
    return {"forms": task_forms_view(), "check_minutes": tasks.form_interval(weight) // 60,
            "requests_a_day": tasks.form_requests_a_day(weight),
            "checks_a_day": tasks.FORM_DAILY_REQUESTS,
            "lane_a_day": connecteam_module.REPORTS_DAILY_CAP, "sync": sync}


async def admin_task_forms(request: web.Request):
    return web.json_response({**task_forms_body(), "connecteam": connecteam_settings()[0]})


async def admin_connecteam_forms(request: web.Request):
    """Connecteam's forms, to choose from (one request in the reports lane, then cached)."""
    try:
        forms = await ctdata.forms()
    except ctdata.Unavailable as err:
        return bad_request(str(err), 502)
    return web.json_response({"forms": [{"id": str(f["id"]), "name": str(f.get("name") or "")}
                                        for f in forms]})


async def admin_form_questions(request: web.Request):
    """A form's questions, to pick the one whose answer is the title (from ctdata's list of
    forms: one request in the reports lane, then cached)."""
    form_id = request.match_info["form_id"]
    try:
        forms = await ctdata.forms()
    except ctdata.Unavailable as err:
        return bad_request(str(err), 502)
    form = next((f for f in forms if str(f["id"]) == form_id), None)
    if not form:
        return bad_request("That form isn't in Connecteam any more.", 404)
    questions = []
    for q in form.get("questions") or []:
        text = tasks.clean_text(q.get("title"), tasks.MAX_QUESTION)
        if text and text not in questions and q.get("type") != "description":
            questions.append(text)
    return web.json_response({"questions": questions[:100]})


async def admin_form_fields(request: web.Request):
    """A form's status manager fields with their values, to follow one (ctdata: from the list
    of forms, or one request for the form)."""
    try:
        fields = await ctdata.form_fields(request.match_info["form_id"])
    except ctdata.Unavailable as err:
        return bad_request(str(err), 502)
    except ValueError as err:
        return bad_request(str(err), 404)
    return web.json_response({"fields": [f for f in fields if f["type"] == "status"],
                              "notes": [f["name"] for f in fields if f["type"] == "note"]})


async def admin_task_sync(request: web.Request):
    return web.json_response(task_forms_body())


async def admin_save_task_sync(request: web.Request):
    """Turn the Connecteam task sync on or off, and choose its boards and their departments."""
    data = await read_json(request)
    try:
        ctsync.save(data.get("enabled") is True, data.get("boards", []), task_departments())
    except tasks.TaskError as err:
        return bad_request(str(err))
    db.log(admin_name(request), "Turned on the Connecteam task sync" if data.get("enabled") is True
           else "Turned off the Connecteam task sync",
           detail=", ".join(b["name"] or b["board_id"] for b in ctsync.boards()))
    return web.json_response(task_forms_body())


async def admin_task_sync_boards(request: web.Request):
    """Connecteam's Quick Tasks boards, to choose from (one request, when the owner asks)."""
    try:
        found = await ctsync.ct_boards()
    except (ctsync.Paused, ctsync.Busy, ConnecteamError) as err:
        return bad_request(str(err), 502)
    return web.json_response({"boards": found})


async def admin_test_task_sync(request: web.Request):
    return web.json_response(await ctsync.test())


async def admin_save_task_form(request: web.Request):
    form_id = request.match_info["form_id"]
    data = await read_json(request)
    old = db.task_form(form_id)
    if not old and len(db.task_forms()) >= tasks.MAX_FORMS:
        return bad_request(f"At most {tasks.MAX_FORMS} forms can make tasks.")
    try:
        # It may keep the department it has, even once that's hidden (so its other settings
        # can still be saved).
        kept = {old["department_id"]: ""} if old and old["department_id"] is not None else {}
        fields = tasks.clean_form(data, {**kept, **task_departments()})
    except tasks.TaskError as err:
        return bad_request(str(err))
    fields["name"] = fields["name"] or (old or {}).get("name") or f"Form {form_id}"
    if "follow" not in data:
        fields["follow"] = (old or {}).get("follow")   # left as it was
    db.set_task_form(form_id, fields, time.time())
    db.log(admin_name(request), "Changed a form that makes tasks" if old
           else "Chose a form to make tasks", detail=fields["name"])
    return web.json_response(task_forms_body())


async def admin_delete_task_form(request: web.Request):
    form = db.task_form(request.match_info["form_id"])
    if form:
        db.delete_task_form(form["form_id"])
        db.log(admin_name(request), "Stopped a form making tasks", detail=form["name"])
    return web.json_response(task_forms_body())


def admin_app() -> web.Application:
    app = web.Application(middlewares=[admin_guard], client_max_size=MAX_ADMIN_BODY)
    app[BODY_SECONDS] = 300.0   # a big backup over a slow connection
    app.router.add_get("/", admin_index)
    app.router.add_get("/preview", admin_preview)
    app.router.add_get("/api/preview/me", admin_preview_me)
    app.router.add_post("/api/preview/entities", admin_preview_entities)
    app.router.add_static("/static/", os.path.join(HERE, "admin"))
    app.router.add_get("/logo.png", logo)
    app.router.add_get(r"/icon-{size:\d+}.png", staff_icon)
    app.router.add_get("/api/state", admin_state)
    app.router.add_get(r"/api/export/{kind:layout|roles|staff|departments}", admin_export)
    app.router.add_post(r"/api/import/{kind:roles|staff|departments}", admin_import)
    app.router.add_get("/api/backup", admin_backup)
    app.router.add_get("/api/connecteam/users", admin_connecteam_users)
    app.router.add_get("/api/connecteam/forms", admin_connecteam_forms)
    app.router.add_get("/api/tasks/forms", admin_task_forms)
    app.router.add_get(r"/api/tasks/forms/{form_id:[A-Za-z0-9_-]{1,40}}/questions", admin_form_questions)
    app.router.add_put(r"/api/tasks/forms/{form_id:[A-Za-z0-9_-]{1,40}}", admin_save_task_form)
    app.router.add_delete(r"/api/tasks/forms/{form_id:[A-Za-z0-9_-]{1,40}}", admin_delete_task_form)
    app.router.add_get(r"/api/tasks/forms/{form_id:[A-Za-z0-9_-]{1,40}}/fields", admin_form_fields)
    app.router.add_get("/api/tasks/sync", admin_task_sync)
    app.router.add_put("/api/tasks/sync", admin_save_task_sync)
    app.router.add_get("/api/tasks/sync/boards", admin_task_sync_boards)
    app.router.add_post("/api/tasks/sync/test", admin_test_task_sync)
    app.router.add_post("/api/connecteam/test", admin_connecteam_test)
    app.router.add_get("/api/connecteam/smart-groups", admin_smart_groups)
    app.router.add_post("/api/connecteam/sync-staff", admin_sync_staff)
    app.router.add_post("/api/connecteam/departments", admin_sync_departments)
    app.router.add_get("/api/connecteam/links", admin_connecteam_links)
    app.router.add_post("/api/connecteam/links", admin_save_links)
    app.router.add_get("/api/hr-google", admin_google)
    app.router.add_put("/api/hr-google", admin_save_google)
    app.router.add_post("/api/hr-google/script", admin_google_script)
    app.router.add_post("/api/hr-google/check", admin_check_google)
    app.router.add_post("/api/hr-google/test", admin_test_google)
    app.router.add_post("/api/departments", admin_create_department)
    app.router.add_put("/api/departments/order", admin_reorder_departments)
    app.router.add_put(r"/api/departments/{id:\d{1,12}}", admin_update_department)
    app.router.add_delete(r"/api/departments/{id:\d{1,12}}", admin_delete_department)
    app.router.add_post(r"/api/departments/{id:\d{1,12}}/{action:hide|show}", admin_hide_department)
    app.router.add_get("/api/keycards", admin_key_cards)
    app.router.add_get(r"/api/keycards/{id:\d{1,12}}", admin_key_card)
    app.router.add_post(r"/api/keycards/{id:\d{1,12}}/{action:revoke|lost|notice|replace|lockout}",
                        admin_key_card_action)
    app.router.add_patch(r"/api/keycards/{id:\d{1,12}}/lockouts/{lock_id:\d{1,16}}",
                         admin_key_card_lockout)
    app.router.add_get(r"/api/keycards/jobs/{id:\d{1,12}}", admin_key_card_job)
    app.router.add_post(r"/api/keycards/jobs/{id:\d{1,12}}/{action:retry|skip|cancel}",
                        admin_key_card_job_action)
    app.router.add_post(r"/api/keycards/remote-test/{action:start|lockout|passed|failed}",
                        admin_remote_test)
    app.router.add_post(r"/api/staff/{id:\d{1,12}}/keycard", admin_make_key_card)
    app.router.add_post("/api/wristbands/test", admin_wristbands_test)
    app.router.add_post("/api/wristbands/doors/sync", admin_sync_doors)
    app.router.add_get("/api/wristbands/doors", admin_wristband_doors)
    app.router.add_patch(r"/api/wristbands/doors/{id:\d{1,16}}", admin_update_door)
    app.router.add_post("/api/wristbands/doors/kind", admin_mark_doors)
    app.router.add_get("/api/wristbands/jobs", admin_wristband_jobs)
    app.router.add_get("/api/door-activity", admin_door_activity)
    app.router.add_post("/api/door-activity/address", admin_new_door_address)
    app.router.add_post(r"/api/devices/{id:\d{1,12}}/encoder-test", admin_encoder_test)
    app.router.add_post("/api/wristbands/encoders/find", admin_find_encoders)
    app.router.add_post("/api/encoder/ticket", admin_encoder_ticket)   # This computer's link
    app.router.add_get("/api/encoder/link", admin_encoder_link)
    app.on_shutdown.append(close_dashboard_links)
    app.router.add_post("/api/restore/preview", admin_restore_preview)
    app.router.add_post("/api/restore", admin_restore)
    app.router.add_get("/api/safety-backups", admin_safety_backups)
    app.router.add_get(r"/api/safety-backups/{name}", admin_safety_backup)
    app.router.add_post("/api/migrate/create", admin_migrate_create)
    app.router.add_post("/api/migrate/restore", admin_migrate_restore)
    app.router.add_get("/api/entities", admin_entities)
    app.router.add_post("/api/roles", admin_create_role)
    app.router.add_put(r"/api/roles/{id:\d+}", admin_update_role)
    app.router.add_patch(r"/api/roles/{id:\d+}", admin_role_pages)
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
    app.router.add_post(r"/api/staff/{id:\d{1,12}}/{action:archive|restore}", admin_archive_staff)
    app.router.add_get(r"/api/devices/{id:\d+}/qr.svg", admin_device_qr)
    app.router.add_get("/api/activity", admin_activity)
    app.router.add_put("/api/settings", admin_update_settings)
    app.router.add_post("/api/logo", admin_upload_logo)
    app.router.add_delete("/api/logo", admin_delete_logo)
    app.router.add_get("/api/hr", admin_hr)
    app.router.add_put("/api/hr/settings", admin_hr_settings)
    app.router.add_get("/api/hr/people", admin_hr_people)
    app.router.add_put("/api/hr/admins", admin_hr_admins)
    app.router.add_post(r"/api/hr/admins/{pid:[A-Za-z0-9_-]{1,40}}/setup", admin_hr_setup_code)
    app.router.add_post(r"/api/hr/people/{pid:[A-Za-z0-9_-]{1,40}}/{action:reset|unlock}",
                        admin_hr_person_action)
    app.router.add_post("/api/hr/sign-out-everyone", admin_hr_sign_out_everyone)
    app.router.add_post("/api/hr/unlock-key", admin_hr_unlock_key)
    app.router.add_post("/api/hr/sync", admin_hr_sync)
    app.router.add_get("/api/reports", admin_reports)
    app.router.add_put(r"/api/reports/{key:[a-z][a-z0-9_]{0,39}}", admin_update_report)
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
    await tthotel.start()
    recover_wristband_jobs()
    discovery.scan_soon()   # only while Wristbands is on and a computer's encoder is Automatic
    clock_task = asyncio.create_task(connecteam.run())
    sync_task = asyncio.create_task(staff_sync_loop())   # only while it has something to do
    doors_task = asyncio.create_task(tthotel.run())
    tasks_task = asyncio.create_task(tasks_loop())   # forms only once one is chosen
    raise_file_limit()
    if hr_service.enabled():
        hr_service.start()   # locked until the recovery key is typed, if its key is missing
    runners = []
    # The HR server always listens: while HR is off it only says so, so turning it on needs no
    # restart.
    for app, port in ((admin_app(), ADMIN_PORT), (staff_app(), STAFF_PORT),
                      (hr_server.make_app(hr_service), HR_PORT)):
        runner = web.AppRunner(app, access_log=None, keepalive_timeout=IDLE_SECONDS)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        runners.append(runner)
    log.info("Staff Portal %s: admin on :%s (ingress), staff app on :%s, HR portal on :%s (%s)",
             VERSION, ADMIN_PORT, STAFF_PORT, HR_PORT, hr_service.status())
    # Home Assistant stops the add-on with SIGTERM (on update, restart or shutdown): treat it as a
    # normal exit so the cleanup below runs, which also lets go of every card encoder.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
        log.info("Stopping Staff Portal")
    finally:
        clock_task.cancel()
        sync_task.cancel()
        doors_task.cancel()
        tasks_task.cancel()
        for runner in runners:
            await runner.cleanup()
        await hr_service.stop()
        await discovery.close()
        await encoders.close_all()   # lets go of every encoder
        await tthotel.close()
        await connecteam.close()
        await gdrive.close()
        await ha.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
