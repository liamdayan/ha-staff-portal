"""Exporting and importing layouts, roles, departments and staff, and whole backups of Staff
Access.

Files are JSON with a small envelope ({"format": "staff-portal", "kind": ...}) so the wrong file
is refused with a clear message. Everything read from a file is validated as strictly as the
same data typed into the dashboard. Full backups are downloaded gzip-compressed (the logs in them
shrink about tenfold); a restore takes them either way, and plain JSON files too.

What files never contain:
- Home Assistant sign-in tokens for linked users. After restoring onto the same system, links
  are kept for phones and staff that still match; otherwise they need relinking.
- Pending one-time setup codes. Restored phones still waiting for setup get new QR codes.
- The Connecteam and TT Hotel secrets (and TT Hotel's sign-in). A restore keeps the ones saved.
- The door list (it comes from TT Hotel). Only the door names and hiding chosen here travel, and
  land on doors the system already has.
(A migration file, migrate.py, carries all of these, encrypted.)
Departments (with their key card templates and clock-in rules) are in backups; a backup from
before departments leaves the ones here as they are. The activity log and the record of
wristbands and key cards made are in backups from 0.8.6 (schema 4), and a restore adds them to
the ones here.
Staff PINs are only ever stored scrambled (scrypt), and that is what exports carry, with how
many digits each has when that's known (files from before 0.8.3 don't say: unknown).
"""

import base64
import binascii
import gzip
import io
import ipaddress
import json
import os
import re
import time
import zlib

from db import (ACTIVITY_MAX_ROWS, CARD_STATES, LOCKOUT_METHODS, LOCKOUT_STATUSES, PRIVATE_SETTINGS,
                WB_ACTIVE, WB_CARD_STATUSES, WB_JOB_KINDS, WB_JOB_STATUSES, WB_JOBS_MAX_ROWS,
                ActivityRow, CardJobRow, CardRow, LockoutRow, card_access_choice, page_choice,
                pin_length)
from layout import (GROUP_PREFIX, MAX_PHONE_PAGES, MORE_PAGE_ID, clean_layout, clean_phone_order,
                    clean_phone_pages)
from permissions import controllable, offered
from wristbands import (MAC_RE, MAX_BUILDING, MAX_FLOOR, NO_ACCESS, TYPES, clean_permissions,
                        permission_choice)

FORMAT = "staff-portal"
MIGRATE_FORMAT = "staff-portal-migrate"   # a migration file's header (migrate.py)
BACKUP_SCHEMA = 4          # 4: the activity log and card records (0.8.6)
                           # 3: wristbands and card encoders (0.8.0)
                           # 2: role pages, people's roles, pages and own changes (0.7.0)
READABLE_SCHEMAS = (1, 2, 3, 4)
KINDS = {"layout": "a layout", "roles": "roles", "staff": "staff members",
         "departments": "departments", "backup": "a full backup"}
PIN_HASH_RE = re.compile(r"^[0-9a-f]{32}\$[0-9a-f]{128}$")
TOKEN_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
OWNER_RE = re.compile(r"^([ds]):(\d{1,9})$")
LEVELS = ("view", "control")
OVERRIDE_LEVELS = ("none", "view", "control")   # a person's own changes ('none' = Hidden)
DEVICE_STATUSES = ("pending", "active", "locked", "revoked")
STAFF_STATUSES = ("active", "disabled", "locked")
IMPORT_MODES = ("replace", "copy", "skip")
MAX_ROWS = 5000          # per table in a backup, or entries in an export
# Tables that link things (a role's devices, a person's own settings...) grow with everything
# else multiplied together: a few dozen roles or people with hundreds of settings each.
MAX_LINK_ROWS = 200_000
MAX_LOGO_BYTES = 6 * 1024 * 1024
KEEP_SAFETY_BACKUPS = 5
# Saved copies are gzip-compressed since 0.8.6 (.json.gz); older ones are plain .json.
SAFETY_NAME_RE = re.compile(r"^before-restore-\d{8}-\d{6}\.json(\.gz)?$")
NOT_OURS = "That isn't a Staff Portal export or backup file."
# Logs make backups big, so they're downloaded gzip-compressed, which shrinks them about tenfold
# or more. A restore holds the whole backup in memory, several times its unpacked size, hence a
# limit on that too. The most the add-on keeps, 50,000 activity lines and 100,000 card jobs of
# 2 cards, is about 70 MB unpacked with 2 doors on each card and 160 MB with 10 (each door adds
# about 120 bytes to a job); only cards with many more doors, made all day for two years, go past.
GZIP_MAGIC = b"\x1f\x8b"
MAX_BACKUP_BYTES = 192 * 1024 * 1024
# The log sections of a backup, each {"columns": [...], "rows": [[...], ...]} (columns rather
# than a dict per row: a third smaller, and far less memory to read). The most rows each may have
# is what the add-on keeps itself; cards and lock-outs have no limit of their own, so theirs are
# simply far above anything real.
LOG_ROWS = {"activity": ActivityRow, "wb_jobs": CardJobRow, "wb_cards": CardRow,
            "wb_lockouts": LockoutRow}
LOG_NAMES = {"activity": "activity log lines", "wb_jobs": "card jobs", "wb_cards": "cards",
             "wb_lockouts": "lost card lock-outs"}
MAX_LOG_ROWS = {"activity": ACTIVITY_MAX_ROWS, "wb_jobs": WB_JOBS_MAX_ROWS, "wb_cards": 500_000,
                "wb_lockouts": 200_000}
FUTURE_SLACK = 86400     # a log time later than this from now is damaged, not a clock a bit off
MAX_JOB_RECORDS = 256    # records written on one job's cards (an encoder fits at most 64)
STOPPED_IN_BACKUP = "Stopped: it was still running when the backup was made."
SMART_GROUP_RE = re.compile(r"^\d{1,20}$")   # a Connecteam smart group ID
MAX_CARD_DAYS = 3650     # how long a key card made from the Staff tab may last
MAX_CARD_ACCESS = 256    # entries in a department's key card template (as in a card request)
MAX_PLACE = {"building": 254, "floor": 255, "door": 2**53}   # the encoder's (and TT Hotel's) ranges
# A computer's card encoder: the address of TT Hotel's SN Web Socket helper on the desk PC, and
# the USB port it's on (Windows COM ports, or a Mac/Linux device path).
ENCODER_URL_RE = re.compile(r"^wss?://[A-Za-z0-9.-]+(:\d{1,5})?/?$")
ENCODER_PORT_RE = re.compile(r"^(COM(25[0-6]|2[0-4]\d|1\d\d|[1-9]\d?)"
                             r"|/dev/[A-Za-z0-9_][A-Za-z0-9._/-]{0,59})$")


# ---------------------------------------------------------------- helpers

def _envelope(kind: str, version: str, **data) -> dict:
    return {"format": FORMAT, "kind": kind, "app_version": version,
            "exported_at": int(time.time()), **data}


def check_file(data, kind: str) -> None:
    if isinstance(data, dict) and data.get("format") == MIGRATE_FORMAT:
        raise ValueError("That's a migration file. Open it with Restore from migration file, under "
                         "Migrate, and the code shown when it was made.")
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError(NOT_OURS)
    if data.get("kind") != kind:
        found = KINDS.get(data.get("kind"), "something else")
        raise ValueError(f"That file holds {found}, not {KINDS[kind]}.")


def _name(value, limit: int = 60) -> str:
    return str(value or "").strip()[:limit]


def _rows(data: dict, key: str, limit: int = MAX_ROWS) -> list:
    value = data.get(key) or []
    if not isinstance(value, list):
        raise ValueError(f"The backup's {key} section is damaged.")
    if len(value) > limit:
        raise ValueError(f"The backup has too many {key} (more than {limit}).")
    return [row for row in value if isinstance(row, dict)]


def _int(value, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 10**9:
        raise ValueError(f"The backup has a damaged {what}.")
    return value


def _time(value, default: float | None = None) -> float | None:
    # Positive and before the year 5000, so it can always be shown as a date.
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 1e11
    return float(value) if ok else default


def _level(entity_id: str, level: str) -> str:
    return "control" if level == "control" and controllable(entity_id) else "view"


def _unique(name: str, taken: set[str], limit: int) -> str:
    """name (imported), name (imported 2)... whichever isn't taken (case-insensitively), cut so
    the whole thing, suffix included, fits in `limit` characters."""
    n = 1
    while True:
        suffix = " (imported)" if n == 1 else f" (imported {n})"
        candidate = name[:limit - len(suffix)].rstrip() + suffix
        if candidate.lower() not in taken:
            return candidate
        n += 1


def _connecteam_id(value) -> str | None:
    value = str(value).strip() if value not in (None, "") else ""
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value) else None


def smart_group_id(value) -> str | None:
    """A Connecteam smart group ID (digits), or None."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    value = str(value).strip()
    return value if SMART_GROUP_RE.match(value) else None


def clean_card_access(raw) -> list[dict] | None:
    """A department's key card template: None (no key card), or a list of access entries in the
    shape of a staff card request: {"level": "all"}, {"level": "building", "building": n},
    {"level": "floor", "building": n, "floor": n} or {"level": "door", "door": lock ID}.
    Numbers start at 1 (to the encoder, building 0 means every door). Raises ValueError."""
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > MAX_CARD_ACCESS:
        raise ValueError("Choose what the key card opens from the list.")
    cleaned = []
    for item in raw:
        level = item.get("level") if isinstance(item, dict) else None
        keys = {"all": (), "building": ("building",), "floor": ("building", "floor"),
                "door": ("door",)}.get(level)
        if keys is None:
            raise ValueError("Choose what the key card opens from the list.")
        entry = {"level": level}
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.isdigit() and len(value) <= 16:
                value = int(value)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PLACE[key]:
                raise ValueError("Choose what the key card opens from the list.")
            entry[key] = value
        if entry not in cleaned:
            cleaned.append(entry)
    return cleaned


def card_days(value, default: int = 30) -> int:
    """How many days a key card made from the Staff tab lasts (1 to MAX_CARD_DAYS)."""
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(1, min(MAX_CARD_DAYS, value))


def clock_in_rule(value) -> int | None:
    """A department's clock-in rule: 1 (everyone in it must be clocked in to sign in), 0 (no one
    in it needs to) or None (each person's own setting). Raises ValueError for anything else."""
    if value is None or value == "":
        return None
    if value in (True, "1"):
        return 1
    if value in (False, "0"):
        return 0
    raise ValueError("Choose a clock-in rule from the list.")


def _file_clock_in(value) -> int | None:
    """A department's clock-in rule from a file: damaged means each person's own setting."""
    try:
        return clock_in_rule(value)
    except ValueError:
        return None


def _stored_card_access(raw) -> str | None:
    """A key card template from a file (JSON text or a list) as it's stored here. Damaged means
    an empty template, which makes no card, never a wider one."""
    choice = card_access_choice(raw)
    if choice is None:
        return None
    try:
        return json.dumps(clean_card_access(choice))
    except ValueError:
        return "[]"


def encoder_url_ok(url: str) -> bool:
    match = ENCODER_URL_RE.match(url)
    return bool(match) and (not match[1] or 0 < int(match[1][1:]) <= 65535)


def encoder_url(url: str) -> str | None:
    """An encoder address spelled one way (lower case, no trailing slash or leading zeros in the
    port), or None if it isn't one. Each address gets its own runner, so two computers sharing
    an encoder must spell it the same to take turns."""
    url = url.strip().rstrip("/").lower()
    if not encoder_url_ok(url):
        return None
    base, _, port = url.rpartition(":")
    return f"{base}:{int(port)}" if base.count(":") == 1 and port.isdigit() else url


def encoder_ip(value) -> str | None:
    """The address of the SN Web Socket helper chosen for an Automatic encoder (an IPv4 address,
    spelled one way), or None."""
    try:
        return str(ipaddress.IPv4Address(str(value).strip())) if value else None
    except ValueError:
        return None


def encoder_port_ok(port: str) -> bool:
    return bool(ENCODER_PORT_RE.match(port)) and ".." not in port


def _stored_wristbands(raw) -> str | None:
    """Wristband permissions from a file (stored JSON text, or already an object) as they're
    stored here. None means none of their own: no wristbands for a role, the same as their role
    for a person. Damaged means no wristbands, never more."""
    if raw is None or raw == "":
        return None
    try:
        cleaned = clean_permissions(json.loads(raw) if isinstance(raw, str) else raw)
    except (TypeError, ValueError, RecursionError):
        cleaned = NO_ACCESS
    return None if cleaned is None else json.dumps(cleaned)


def _page_names(layout: dict, choice: list[str] | None) -> list[dict] | None:
    """A page choice by name, for a file that may be opened on another system (page IDs
    differ between systems): [{"page": name} | {"group": name} | {"more": True}], or None for
    "not chosen" (every page, or a person's role's pages)."""
    if choice is None:
        return None
    pages = {p["id"]: p["name"] for p in layout.get("pages", [])}
    groups = {g["id"]: g["name"] for g in layout.get("page_groups", [])}
    named = []
    for entry in choice:
        if entry == MORE_PAGE_ID:
            named.append({"more": True})
        elif entry.startswith(GROUP_PREFIX) and entry[len(GROUP_PREFIX):] in groups:
            named.append({"group": groups[entry[len(GROUP_PREFIX):]]})
        elif entry in pages:
            named.append({"page": pages[entry]})
    return named


def _page_ids(layout: dict, named, missing: set[str]) -> list[str] | None:
    """A page choice from a file (by name) for this system's layout. Names with no match here
    are left out and noted in `missing`. A damaged value means no pages, never every page,
    because pages can carry extra access (an item's own access setting)."""
    if named is None:
        return None
    if not isinstance(named, list):
        return []
    pages = {p["name"].lower(): p["id"] for p in layout.get("pages", [])}
    groups = {g["name"].lower(): g["id"] for g in layout.get("page_groups", [])}
    ids: list[str] = []
    for entry in named[:MAX_PHONE_PAGES]:
        if not isinstance(entry, dict):
            continue
        if entry.get("more") is True:
            found = MORE_PAGE_ID
        elif isinstance(entry.get("group"), str):
            group_id = groups.get(entry["group"].strip().lower())
            found = f"{GROUP_PREFIX}{group_id}" if group_id else None
            if not found:
                missing.add(_name(entry["group"], 30))
        elif isinstance(entry.get("page"), str):
            found = pages.get(entry["page"].strip().lower())
            if not found:
                missing.add(_name(entry["page"], 30))
        else:
            continue
        if found and found not in ids:
            ids.append(found)
    return ids


def _override_levels(raw, area_ids: dict[str, int], missing_areas: set[str]) -> tuple[dict, dict]:
    """A person's own changes from a file: ({entity_id: level}, {area_id: level}), with areas
    matched by name."""
    raw = raw if isinstance(raw, dict) else {}
    entities_in = raw.get("entities") if isinstance(raw.get("entities"), dict) else {}
    areas_in = raw.get("areas") if isinstance(raw.get("areas"), dict) else {}
    entities = {eid: ("view" if lvl == "control" and not controllable(eid) else lvl)
                for eid, lvl in list(entities_in.items())[:MAX_ROWS]
                if isinstance(eid, str) and offered(eid) and lvl in OVERRIDE_LEVELS}
    areas = {}
    for area_name, lvl in list(areas_in.items())[:MAX_ROWS]:
        if lvl not in OVERRIDE_LEVELS:
            continue
        area_id = area_ids.get(str(area_name).lower())
        if area_id:
            areas[area_id] = lvl
        else:
            missing_areas.add(_name(area_name, 40))
    return entities, areas


def _write_overrides(db, staff_id: int, entities: dict, areas: dict) -> None:
    """Replace a person's own changes, without committing."""
    c = db.conn
    c.execute("DELETE FROM staff_entities WHERE staff_id = ?", (staff_id,))
    c.execute("DELETE FROM staff_areas WHERE staff_id = ?", (staff_id,))
    c.executemany("INSERT INTO staff_entities (staff_id, entity_id, level) VALUES (?, ?, ?)",
                  [(staff_id, eid, lvl) for eid, lvl in entities.items()])
    c.executemany("INSERT INTO staff_areas (staff_id, area_id, level) VALUES (?, ?, ?)",
                  [(staff_id, aid, lvl) for aid, lvl in areas.items()])


def _all_or_nothing(db, work) -> dict:
    """Run an import in one transaction: either every row lands or none does."""
    try:
        result = work()
        db.conn.commit()
        return result
    except Exception:
        db.conn.rollback()
        raise


def _write_role(db, role_id, name: str, entities: dict, areas: dict) -> int:
    """Create (role_id None) or overwrite a role's access, without committing."""
    c = db.conn
    if role_id is None:
        role_id = c.execute("INSERT INTO roles (name, created_at) VALUES (?, ?)",
                            (name, time.time())).lastrowid
    c.execute("DELETE FROM role_entities WHERE role_id = ?", (role_id,))
    c.execute("DELETE FROM role_areas WHERE role_id = ?", (role_id,))
    c.executemany("INSERT INTO role_entities (role_id, entity_id, level) VALUES (?, ?, ?)",
                  [(role_id, eid, lvl) for eid, lvl in entities.items()])
    c.executemany("INSERT INTO role_areas (role_id, area_id, level) VALUES (?, ?, ?)",
                  [(role_id, aid, lvl) for aid, lvl in areas.items()])
    return role_id


# ---------------------------------------------------------------- exports

def export_layout(db, version: str) -> dict:
    # Area items point at area IDs, which differ between systems; the names travel with them.
    return _envelope("layout", version, layout=db.layout(),
                     areas=[{"id": a["id"], "name": a["name"]} for a in db.areas()])


def export_roles(db, version: str) -> dict:
    area_names = {a["id"]: a["name"] for a in db.areas()}
    layout = db.layout()
    return _envelope("roles", version, roles=[
        {"name": r["name"], "entities": r["entities"],
         "areas": {area_names[a]: lvl for a, lvl in r["areas"].items() if a in area_names},
         "pages": _page_names(layout, r["layout_pages"]),
         # Door IDs are TT Hotel's own, the same on any system using the same TT Hotel account.
         "wristbands": r["wristbands"]}
        for r in db.roles()])


def export_staff(db, version: str) -> dict:
    phones = {r["id"]: r["name"] for r in db.conn.execute(
        "SELECT id, name FROM devices WHERE status != 'revoked'")}
    allowed: dict[int, list[str]] = {}
    for staff_id, device_id in db.conn.execute("SELECT staff_id, device_id FROM staff_devices"):
        if device_id in phones:
            allowed.setdefault(staff_id, []).append(phones[device_id])
    roles = {r["id"]: r["name"] for r in db.conn.execute("SELECT id, name FROM roles")}
    departments = {d["id"]: d["name"] for d in db.departments()}
    area_names = {a["id"]: a["name"] for a in db.areas()}
    layout = db.layout()

    def overrides(staff_id):
        entities, areas = db.staff_overrides(staff_id)
        return {"entities": entities,
                "areas": {area_names[a]: lvl for a, lvl in areas.items() if a in area_names}}
    return _envelope("staff", version, staff=[
        {"name": r["name"], "pin_hash": r["pin_hash"], "pin_length": pin_length(r["pin_length"]),
         "enabled": r["status"] != "disabled",
         "phones": sorted(allowed.get(r["id"], [])),
         "connecteam_user_id": r["connecteam_user_id"], "require_clock_in": bool(r["require_clock_in"]),
         "clock_in_set": bool(r["clock_in_set"]),
         "role": roles.get(r["role_id"]), "pages": _page_names(layout, page_choice(r["layout_pages"])),
         "overrides": overrides(r["id"]), "wristbands": permission_choice(r["wristbands"]),
         "department": departments.get(r["department_id"])}
        for r in db.conn.execute("""SELECT id, name, pin_hash, pin_length, status,
                                    connecteam_user_id, require_clock_in, clock_in_set, role_id,
                                    layout_pages, wristbands, department_id FROM staff
                                    ORDER BY name""")])


def export_departments(db, version: str) -> dict:
    roles = {r["id"]: r["name"] for r in db.conn.execute("SELECT id, name FROM roles")}
    # Door IDs in a key card template are TT Hotel's own, the same on any system using the same
    # TT Hotel account; smart group IDs are Connecteam's.
    return _envelope("departments", version, departments=[
        {"name": d["name"], "smart_group_id": d["smart_group_id"], "role": roles.get(d["role_id"]),
         "card_access": d["card_access"], "card_days": d["card_days"],
         "card_lockout": bool(d["card_lockout"]),
         "clock_in": None if d["clock_in"] is None else bool(d["clock_in"])}
        for d in db.departments()])


# ---------------------------------------------------------------- imports (merge into what's here)

def import_roles(db, data, mode: str) -> dict:
    check_file(data, "roles")
    if mode not in IMPORT_MODES:
        raise ValueError("Choose what to do with roles that already exist.")
    rows = _rows(data, "roles")
    return _all_or_nothing(db, lambda: _import_roles(db, rows, mode))


def _import_roles(db, rows: list, mode: str) -> dict:
    area_ids = {a["name"].lower(): a["id"] for a in db.areas()}
    existing = {r["name"].lower(): r for r in db.roles()}
    layout = db.layout()
    result = {"added": 0, "replaced": 0, "skipped": 0, "missing_areas": set(), "missing_pages": set()}
    for raw in rows:
        name = _name(raw.get("name"))
        entities_in = raw.get("entities") if isinstance(raw.get("entities"), dict) else {}
        areas_in = raw.get("areas") if isinstance(raw.get("areas"), dict) else {}
        if not name:
            continue
        entities = {eid: _level(eid, lvl) for eid, lvl in list(entities_in.items())[:MAX_ROWS]
                    if isinstance(eid, str) and offered(eid) and lvl in LEVELS}
        areas = {}
        for area_name, lvl in areas_in.items():
            area_id = area_ids.get(str(area_name).lower())
            if area_id and lvl in LEVELS:
                areas[area_id] = lvl
            elif lvl in LEVELS:
                result["missing_areas"].add(_name(area_name, 40))
        match = existing.get(name.lower())
        if match and mode == "skip":
            result["skipped"] += 1
            continue
        # Files from before roles had pages (or wristbands) leave a replaced role's as they are.
        pages = _page_ids(layout, raw["pages"], result["missing_pages"]) if "pages" in raw else _KEEP
        wristbands = _stored_wristbands(raw["wristbands"]) if "wristbands" in raw else _KEEP
        if match and mode == "replace":
            _write_role(db, match["id"], match["name"], entities, areas)
            _write_pages(db, "roles", match["id"], pages)
            _write_wristbands(db, "roles", match["id"], wristbands)
            result["replaced"] += 1
            continue
        if match:
            name = _unique(name, set(existing), 60)
        role_id = _write_role(db, None, name, entities, areas)
        _write_pages(db, "roles", role_id, pages)
        _write_wristbands(db, "roles", role_id, wristbands)
        existing[name.lower()] = {"id": role_id, "name": name}
        result["added"] += 1
    result["missing_areas"] = sorted(result["missing_areas"])
    result["missing_pages"] = sorted(result["missing_pages"])
    return result


_KEEP = object()   # left out of the file: keep what's here


def _write_pages(db, table: str, row_id: int, pages) -> None:
    """Set a role's or person's page choice, without committing (_KEEP: leave it)."""
    assert table in ("roles", "staff")
    if pages is not _KEEP:
        db.conn.execute(f"UPDATE {table} SET layout_pages = ? WHERE id = ?",
                        (None if pages is None else json.dumps(pages), row_id))


def _write_wristbands(db, table: str, row_id: int, stored) -> None:
    """Set a role's or person's wristband permissions (as _stored_wristbands gives them),
    without committing (_KEEP: leave them)."""
    assert table in ("roles", "staff")
    if stored is not _KEEP:
        db.conn.execute(f"UPDATE {table} SET wristbands = ? WHERE id = ?", (stored, row_id))


def import_staff(db, data, mode: str) -> dict:
    check_file(data, "staff")
    if mode not in IMPORT_MODES:
        raise ValueError("Choose what to do with staff members who already exist.")
    rows = _rows(data, "staff")
    return _all_or_nothing(db, lambda: _import_staff(db, rows, mode))


def _import_staff(db, rows: list, mode: str) -> dict:
    phones = {r["name"].lower(): r["id"] for r in db.conn.execute(
        "SELECT id, name FROM devices WHERE status != 'revoked'")}
    existing = {r["name"].lower(): r["id"] for r in db.conn.execute("SELECT id, name FROM staff")}
    current = {r["id"]: r for r in db.conn.execute(
        "SELECT id, status, pin_hash, pin_length FROM staff")}
    roles = {r["name"].lower(): r["id"] for r in db.conn.execute("SELECT id, name FROM roles")}
    departments = {d["name"].lower(): d["id"] for d in db.departments()}
    area_ids = {a["name"].lower(): a["id"] for a in db.areas()}
    layout = db.layout()
    result = {"added": 0, "replaced": 0, "skipped": 0, "bad_pin": 0, "missing_phones": set(),
              "kept_off": [], "signed_out": [], "missing_roles": set(), "missing_pages": set(),
              "missing_areas": set(), "missing_departments": set()}
    for raw in rows:
        name = _name(raw.get("name"), 40)
        pin_hash = str(raw.get("pin_hash") or "")
        if not name:
            continue
        if not PIN_HASH_RE.match(pin_hash):
            result["bad_pin"] += 1
            continue
        status = "active" if raw.get("enabled", True) is not False else "disabled"
        length = pin_length(raw.get("pin_length"))   # unknown in older files, or if damaged
        device_ids = []
        for phone in raw.get("phones") if isinstance(raw.get("phones"), list) else []:
            device_id = phones.get(str(phone).lower())
            if device_id:
                device_ids.append(device_id)
            else:
                result["missing_phones"].add(_name(phone))
        staff_id = existing.get(name.lower())
        if staff_id and mode == "skip":
            result["skipped"] += 1
            continue
        if staff_id and mode == "replace":
            old = current[staff_id]
            if old["status"] == "disabled" and status != "disabled":
                # Someone you turned off stays off: an older file (or one from another
                # property) mustn't quietly let them back in.
                status = "disabled"
                result["kept_off"].append(name)
            if length is None and pin_hash == old["pin_hash"]:
                length = pin_length(old["pin_length"])   # the same PIN: its length is known here
            db.conn.execute(
                """UPDATE staff SET pin_hash = ?, pin_length = ?, status = ?, failed_pins = 0,
                   locked_until = NULL WHERE id = ?""", (pin_hash, length, status, staff_id))
            if pin_hash != old["pin_hash"] or status == "disabled":
                result["signed_out"].append(staff_id)   # a new PIN means signing in again
            result["replaced"] += 1
        else:
            if staff_id:
                name = _unique(name, set(existing), 40)
            staff_id = db.conn.execute(
                """INSERT INTO staff (name, pin_hash, pin_length, status, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, pin_hash, length, status, time.time())).lastrowid
            link = _connecteam_id(raw.get("connecteam_user_id"))
            if link and not db.conn.execute("SELECT 1 FROM staff WHERE connecteam_user_id = ?",
                                            (link,)).fetchone():
                db.conn.execute("""UPDATE staff SET connecteam_user_id = ?, require_clock_in = ?,
                                   clock_in_set = ? WHERE id = ?""",
                                (link, 0 if raw.get("require_clock_in") is False else 1,
                                 1 if raw.get("clock_in_set") is True else 0, staff_id))
            existing[name.lower()] = staff_id
            result["added"] += 1
        db.conn.executemany(
            "INSERT OR IGNORE INTO staff_devices (staff_id, device_id) VALUES (?, ?)",
            [(staff_id, d) for d in device_ids])
        # Their role (matched by name), own pages and own changes, for role-based devices.
        # Files from before these existed leave a replaced person's as they are, and a role
        # with no match here leaves their current role.
        role_name = _name(raw.get("role")) if isinstance(raw.get("role"), str) else ""
        if role_name and role_name.lower() in roles:
            db.conn.execute("UPDATE staff SET role_id = ? WHERE id = ?", (roles[role_name.lower()], staff_id))
        elif role_name:
            result["missing_roles"].add(role_name)
        elif "role" in raw:
            db.conn.execute("UPDATE staff SET role_id = NULL WHERE id = ?", (staff_id,))
        # Their department, matched by name, the same way (files from before departments leave
        # it as it is).
        dept_name = _name(raw.get("department"), 40) if isinstance(raw.get("department"), str) else ""
        if dept_name and dept_name.lower() in departments:
            db.conn.execute("UPDATE staff SET department_id = ? WHERE id = ?",
                            (departments[dept_name.lower()], staff_id))
        elif dept_name:
            result["missing_departments"].add(dept_name)
        elif "department" in raw:
            db.conn.execute("UPDATE staff SET department_id = NULL WHERE id = ?", (staff_id,))
        _write_pages(db, "staff", staff_id,
                     _page_ids(layout, raw["pages"], result["missing_pages"]) if "pages" in raw else _KEEP)
        if "overrides" in raw:
            _write_overrides(db, staff_id, *_override_levels(raw["overrides"], area_ids, result["missing_areas"]))
        _write_wristbands(db, "staff", staff_id,
                          _stored_wristbands(raw["wristbands"]) if "wristbands" in raw else _KEEP)
    for key in ("missing_phones", "missing_roles", "missing_pages", "missing_areas",
                "missing_departments"):
        result[key] = sorted(result[key])
    return result


def import_departments(db, data, mode: str) -> dict:
    check_file(data, "departments")
    if mode not in IMPORT_MODES:
        raise ValueError("Choose what to do with departments that already exist.")
    rows = _rows(data, "departments")
    return _all_or_nothing(db, lambda: _import_departments(db, rows, mode))


def _import_departments(db, rows: list, mode: str) -> dict:
    """Departments matched by name. Their role is matched by name too; a smart group already
    followed by another department here is left off (one smart group, one department)."""
    roles = {r["name"].lower(): r["id"] for r in db.conn.execute("SELECT id, name FROM roles")}
    existing = {d["name"].lower(): d for d in db.departments()}
    groups = {d["smart_group_id"]: d["id"] for d in existing.values() if d["smart_group_id"]}
    result = {"added": 0, "replaced": 0, "skipped": 0, "missing_roles": set()}
    for raw in rows:
        name = _name(raw.get("name"), 40)
        if not name:
            continue
        match = existing.get(name.lower())
        if match and mode == "skip":
            result["skipped"] += 1
            continue
        role_name = _name(raw.get("role")) if isinstance(raw.get("role"), str) else ""
        role_id = roles.get(role_name.lower()) if role_name else None
        if role_name and role_id is None:
            result["missing_roles"].add(role_name)
        group = smart_group_id(raw.get("smart_group_id"))
        own = match["id"] if match and mode == "replace" else None
        if group and groups.get(group, own) != own:
            group = None
        values = (group, role_id, _stored_card_access(raw.get("card_access")),
                  card_days(raw.get("card_days")), 0 if raw.get("card_lockout") is False else 1)
        # Its clock-in rule (files from before it leave a replaced department's as it is).
        clock_in = _file_clock_in(raw.get("clock_in"))
        if match and mode == "replace":
            db.conn.execute("""UPDATE departments SET smart_group_id = ?, role_id = ?, card_access = ?,
                               card_days = ?, card_lockout = ? WHERE id = ?""", (*values, match["id"]))
            if "clock_in" in raw:
                db.conn.execute("UPDATE departments SET clock_in = ? WHERE id = ?",
                                (clock_in, match["id"]))
            dept_id = match["id"]
            result["replaced"] += 1
        else:
            if match:
                name = _unique(name, set(existing), 40)
            order = db.conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM departments").fetchone()[0]
            dept_id = db.conn.execute(
                """INSERT INTO departments (name, sort_order, smart_group_id, role_id, card_access,
                   card_days, card_lockout, created_at, clock_in)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, order, *values, time.time(), clock_in)).lastrowid
            existing[name.lower()] = {"id": dept_id, "name": name, "smart_group_id": group}
            result["added"] += 1
        if group:
            groups[group] = dept_id
    result["missing_roles"] = sorted(result["missing_roles"])
    return result


# ---------------------------------------------------------------- full backup and restore

def _stored_pages(raw) -> str | None:
    """A page choice from a backup (stored JSON text, or a list) as it's stored here. Damaged
    means no pages, never every page."""
    try:
        pages = clean_phone_pages(page_choice(raw))
    except ValueError:
        pages = []   # more than a phone can have: damaged
    return None if pages is None else json.dumps(pages)


def make_backup(db, branding, version: str) -> dict:
    rows = lambda sql: [dict(r) for r in db.conn.execute(sql)]
    logo = branding.logo_png()
    return _envelope(
        "backup", version, schema=BACKUP_SCHEMA,
        # Secrets are credentials: they never go into a file (a restore keeps the ones already
        # saved). Neither do request counts or TT Hotel's sign-in.
        settings={k: v for k, v in db.settings().items() if k not in PRIVATE_SETTINGS},
        layout=db.layout(),
        logo=base64.b64encode(logo).decode() if logo else None,
        areas=rows("SELECT id, name, sort_order, created_at FROM areas"),
        area_entities=rows("SELECT area_id, entity_id FROM area_entities"),
        area_ha_areas=rows("SELECT area_id, ha_area_id FROM area_ha_areas"),
        roles=rows("SELECT id, name, created_at, layout_pages, wristbands FROM roles"),
        role_entities=rows("SELECT role_id, entity_id, level FROM role_entities"),
        role_areas=rows("SELECT role_id, area_id, level FROM role_areas"),
        devices=rows("""SELECT id, name, kind, role_id, status, token_hash, pin_hash, signin_mode,
                        staff_all, pin_only, layout_pages, created_at, last_seen, encoder_url,
                        encoder_port, encoder_auto, encoder_auto_ip, pin_length, encoder_local
                        FROM devices WHERE status != 'deleted'"""),
        staff=rows("""SELECT id, name, pin_hash, status, created_at, last_seen, connecteam_user_id,
                      require_clock_in, role_id, layout_pages, wristbands, department_id,
                      connecteam_auto, clock_in_set, role_set, pin_from_connecteam,
                      connecteam_note, pin_length FROM staff"""),
        departments=rows("""SELECT id, name, sort_order, smart_group_id, role_id, card_access,
                            card_days, card_lockout, created_at, clock_in FROM departments"""),
        staff_devices=rows("SELECT staff_id, device_id FROM staff_devices"),
        staff_entities=rows("SELECT staff_id, entity_id, level FROM staff_entities"),
        staff_areas=rows("SELECT staff_id, area_id, level FROM staff_areas"),
        phone_orders=rows("SELECT owner, data FROM phone_orders"),
        wristband_doors=rows("SELECT lock_id, name, hidden FROM wb_doors"),
        **log_sections(db),
    )


def log_sections(db) -> dict:
    """The activity log and the card records, as a backup carries them (see LOG_ROWS), oldest
    first: at most what the add-on keeps, newest first if there's ever more."""
    newest_jobs = "SELECT id FROM wb_jobs ORDER BY ts DESC, id DESC LIMIT ?"
    newest_cards = (f"SELECT id FROM wb_cards WHERE job_id IN ({newest_jobs}) "
                    "ORDER BY id DESC LIMIT ?")
    queries = {
        "activity": ("SELECT {} FROM (SELECT * FROM activity ORDER BY ts DESC, id DESC LIMIT ?) "
                     "ORDER BY ts, id", (ACTIVITY_MAX_ROWS,)),
        "wb_jobs": (f"SELECT {{}} FROM wb_jobs WHERE id IN ({newest_jobs}) ORDER BY ts, id",
                    (WB_JOBS_MAX_ROWS,)),
        "wb_cards": (f"SELECT {{}} FROM wb_cards WHERE id IN ({newest_cards}) ORDER BY id",
                     (WB_JOBS_MAX_ROWS, MAX_LOG_ROWS["wb_cards"])),
        "wb_lockouts": (f"SELECT {{}} FROM wb_lockouts WHERE card_id IN ({newest_cards}) "
                        "ORDER BY id LIMIT ?",
                        (WB_JOBS_MAX_ROWS, MAX_LOG_ROWS["wb_cards"], MAX_LOG_ROWS["wb_lockouts"])),
    }
    return {key: {"columns": list(LOG_ROWS[key]._fields),
                  "rows": [list(r) for r in db.conn.execute(
                      sql.format(", ".join(LOG_ROWS[key]._fields)), params)]}
            for key, (sql, params) in queries.items()}


def _log_count(data: dict, key: str) -> int:
    section = data.get(key)
    rows = section.get("rows") if isinstance(section, dict) else None
    return len(rows) if isinstance(rows, list) else 0


def backup_summary(data: dict) -> dict:
    return {"exported_at": _time(data.get("exported_at")), "app_version": str(data.get("app_version") or "")[:20],
            "property_name": (data.get("settings") or {}).get("property_name"),
            **{k: len(data.get(k) or []) if isinstance(data.get(k) or [], list) else 0
               for k in ("roles", "areas", "devices", "staff", "departments")},
            "pages": len((data.get("layout") or {}).get("pages") or []),
            # Logs (backups from 0.8.6 on); a restore adds them to the ones here.
            "has_logs": any(key in data for key in LOG_ROWS),
            **{key: _log_count(data, key) for key in ("activity", "wb_jobs", "wb_cards")}}


def clean_backup(data, clean_settings, current_settings: dict, clean_logo,
                 known_tokens: set[tuple[int, str]]) -> dict:
    """Validate a whole backup before anything is replaced. Raises ValueError.

    known_tokens: (device ID, token hash) pairs this system issued and hasn't revoked. A phone
    only stays signed in if its pair is one of them; any other phone in the file has to be set
    up again, so a tampered backup can't plant a working phone and an old one can't bring back
    a revoked phone."""
    check_file(data, "backup")
    if data.get("schema") not in READABLE_SCHEMAS:
        raise ValueError("That backup was made by a newer version of Staff Portal. Update the add-on first.")

    settings_in = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    # Secrets are write-only and never in a backup: a file can't set or clear them, nor touch
    # the request counts or TT Hotel's sign-in.
    settings_in = {k: v for k, v in settings_in.items()
                   if k not in (*PRIVATE_SETTINGS, "connecteam_clear_secret", "tthotel_clear_secret")}
    settings = clean_settings(settings_in, current_settings)
    layout = clean_layout(data.get("layout") or {"pages": []})

    logo = None
    if data.get("logo"):
        if not isinstance(data["logo"], str) or len(data["logo"]) > MAX_LOGO_BYTES * 4 // 3 + 4:
            raise ValueError("The backup's logo is too big.")
        try:
            logo = clean_logo(base64.b64decode(data["logo"], validate=True))
        except (binascii.Error, ValueError, OSError):
            raise ValueError("The backup's logo is damaged.") from None

    areas, names = [], set()
    for row in _rows(data, "areas"):
        area_id, name = _int(row.get("id"), "area"), _name(row.get("name"), 40)
        if not name or name in names or any(a["id"] == area_id for a in areas):
            raise ValueError("The backup has a damaged or repeated area.")
        names.add(name)
        order = row.get("sort_order")
        areas.append({"id": area_id, "name": name,
                      "sort_order": order if isinstance(order, int) and not isinstance(order, bool) else 0,
                      "created_at": _time(row.get("created_at"), time.time())})
    area_ids = {a["id"] for a in areas}
    area_entities = {(r["area_id"], r["entity_id"]) for r in _rows(data, "area_entities", MAX_LINK_ROWS)
                     if r.get("area_id") in area_ids and offered(str(r.get("entity_id") or ""))}
    area_ha_areas = {(r["area_id"], r["ha_area_id"]) for r in _rows(data, "area_ha_areas", MAX_LINK_ROWS)
                     if r.get("area_id") in area_ids and isinstance(r.get("ha_area_id"), str)
                     and 0 < len(r["ha_area_id"]) <= 100}

    roles, names = [], set()
    for row in _rows(data, "roles"):
        role_id, name = _int(row.get("id"), "role"), _name(row.get("name"))
        if not name or name in names or any(r["id"] == role_id for r in roles):
            raise ValueError("The backup has a damaged or repeated role.")
        names.add(name)
        # Backups from before wristbands have none: roles make none, people follow their role.
        roles.append({"id": role_id, "name": name, "created_at": _time(row.get("created_at"), time.time()),
                      "layout_pages": _stored_pages(row.get("layout_pages")),
                      "wristbands": _stored_wristbands(row.get("wristbands"))})
    role_ids = {r["id"] for r in roles}
    role_entities = {(r["role_id"], r["entity_id"]): _level(r["entity_id"], r["level"])
                     for r in _rows(data, "role_entities", MAX_LINK_ROWS)
                     if r.get("role_id") in role_ids and offered(str(r.get("entity_id") or ""))
                     and r.get("level") in LEVELS}
    role_areas = {(r["role_id"], r["area_id"]): r["level"] for r in _rows(data, "role_areas", MAX_LINK_ROWS)
                  if r.get("role_id") in role_ids and r.get("area_id") in area_ids
                  and r.get("level") in LEVELS}

    # Departments (backups from before them have none: None leaves the ones here as they are).
    departments, names, groups = None, set(), set()
    if data.get("departments") is not None:
        departments = []
        for row in _rows(data, "departments"):
            dept_id, name = _int(row.get("id"), "department"), _name(row.get("name"), 40)
            if not name or name.lower() in names or any(d["id"] == dept_id for d in departments):
                raise ValueError("The backup has a damaged or repeated department.")
            names.add(name.lower())
            group = smart_group_id(row.get("smart_group_id"))
            if group in groups:
                group = None   # one smart group, one department
            groups.add(group)
            order = row.get("sort_order")
            departments.append({
                "id": dept_id, "name": name,
                "sort_order": order if isinstance(order, int) and not isinstance(order, bool) else 0,
                "smart_group_id": group,
                "role_id": row.get("role_id") if row.get("role_id") in role_ids else None,
                "card_access": _stored_card_access(row.get("card_access")),
                "card_days": card_days(row.get("card_days")),
                "card_lockout": 0 if row.get("card_lockout") in (0, False) else 1,
                "created_at": _time(row.get("created_at"), time.time()),
                # Backups from before the department clock-in rule: each person's own setting.
                "clock_in": _file_clock_in(row.get("clock_in"))})
    department_ids = {d["id"] for d in departments or []}
    flag = lambda value: 1 if value in (1, True) else 0

    staff, names = [], set()
    for row in _rows(data, "staff"):
        staff_id, name = _int(row.get("id"), "staff member"), _name(row.get("name"), 40)
        pin_hash = str(row.get("pin_hash") or "")
        if not name or name in names or any(m["id"] == staff_id for m in staff):
            raise ValueError("The backup has a damaged or repeated staff member.")
        if not PIN_HASH_RE.match(pin_hash):
            raise ValueError(f"The backup has a damaged PIN for {name}.")
        names.add(name)
        link = _connecteam_id(row.get("connecteam_user_id"))
        if link and any(m["connecteam_user_id"] == link for m in staff):
            raise ValueError(f"The backup links two staff members to the same Connecteam user ({name}).")
        staff.append({"id": staff_id, "name": name, "pin_hash": pin_hash,
                      "status": row.get("status") if row.get("status") in STAFF_STATUSES else "active",
                      "created_at": _time(row.get("created_at"), time.time()),
                      "last_seen": _time(row.get("last_seen")),
                      "connecteam_user_id": link,
                      "require_clock_in": 1 if link and row.get("require_clock_in") in (1, True) else 0,
                      "role_id": row.get("role_id") if row.get("role_id") in role_ids else None,
                      "layout_pages": _stored_pages(row.get("layout_pages")),
                      "wristbands": _stored_wristbands(row.get("wristbands")),
                      # Backups from before departments and the Connecteam sync: added by hand.
                      "department_id": (row.get("department_id")
                                        if row.get("department_id") in department_ids else None),
                      "connecteam_auto": flag(row.get("connecteam_auto")) if link else 0,
                      "clock_in_set": flag(row.get("clock_in_set")),
                      "role_set": flag(row.get("role_set")),
                      "pin_from_connecteam": flag(row.get("pin_from_connecteam")) if link else 0,
                      "connecteam_note": (_name(row.get("connecteam_note"), 120) or None
                                          if isinstance(row.get("connecteam_note"), str) else None),
                      # Backups from before 0.8.3 don't know it: the PIN pad waits for a pause.
                      "pin_length": pin_length(row.get("pin_length"))})
    staff_ids = {m["id"] for m in staff}
    staff_entities = {(r["staff_id"], r["entity_id"]): ("view" if r["level"] == "control" and not controllable(r["entity_id"])
                                                         else r["level"])
                      for r in _rows(data, "staff_entities", MAX_LINK_ROWS)
                      if r.get("staff_id") in staff_ids and offered(str(r.get("entity_id") or ""))
                      and r.get("level") in OVERRIDE_LEVELS}
    staff_areas = {(r["staff_id"], r["area_id"]): r["level"] for r in _rows(data, "staff_areas", MAX_LINK_ROWS)
                   if r.get("staff_id") in staff_ids and r.get("area_id") in area_ids
                   and r.get("level") in OVERRIDE_LEVELS}

    devices, needs_setup = [], []
    for row in _rows(data, "devices"):
        device_id, name = _int(row.get("id"), "phone"), _name(row.get("name"))
        if not name or any(d["id"] == device_id for d in devices):
            raise ValueError("The backup has a damaged or repeated phone.")
        token_hash = row.get("token_hash") if TOKEN_HASH_RE.match(str(row.get("token_hash") or "")) else None
        if token_hash and (device_id, token_hash) not in known_tokens:
            token_hash = None   # not issued by this system, or revoked since
        pin_hash = row.get("pin_hash")
        if pin_hash is not None and not PIN_HASH_RE.match(str(pin_hash)):
            raise ValueError(f"The backup has a damaged PIN for the phone {name}.")
        status = row.get("status") if row.get("status") in DEVICE_STATUSES else "pending"
        if status in ("active", "locked") and not token_hash:
            status = "pending"  # nothing to sign in with: set it up again
            needs_setup.append(name)
        if status in ("pending", "revoked"):
            token_hash = None
        role_id = row.get("role_id") if row.get("role_id") in role_ids else None
        url, port = str(row.get("encoder_url") or ""), str(row.get("encoder_port") or "")
        devices.append({"id": device_id, "name": name, "role_id": role_id, "status": status,
                        "kind": "computer" if row.get("kind") == "computer" else "phone",
                        "token_hash": token_hash, "pin_hash": pin_hash,
                        "pin_length": pin_length(row.get("pin_length")) if pin_hash else None,
                        "signin_mode": row.get("signin_mode") if row.get("signin_mode") in ("staff", "role") else "phone",
                        "staff_all": 0 if row.get("staff_all") in (0, False) else 1,
                        "pin_only": 1 if row.get("pin_only") in (1, True) else 0,
                        "layout_pages": _stored_pages(row.get("layout_pages")),
                        "created_at": _time(row.get("created_at"), time.time()),
                        "last_seen": _time(row.get("last_seen")),
                        "encoder_url": encoder_url(url),
                        "encoder_port": port if encoder_port_ok(port) else None,
                        # Backups from before Automatic encoders have neither: Off.
                        "encoder_auto": 1 if row.get("encoder_auto") in (1, True) else 0,
                        "encoder_auto_ip": encoder_ip(row.get("encoder_auto_ip")),
                        # From before This computer (USB): the mode they had (Off, Automatic or
                        # an address), as before.
                        "encoder_local": 1 if row.get("encoder_local") in (1, True) else 0})
    device_ids = {d["id"] for d in devices}
    staff_devices = {(r["staff_id"], r["device_id"]) for r in _rows(data, "staff_devices", MAX_LINK_ROWS)
                     if r.get("staff_id") in staff_ids and r.get("device_id") in device_ids}

    phone_orders = {}
    for row in _rows(data, "phone_orders", MAX_LINK_ROWS):
        match = OWNER_RE.match(str(row.get("owner") or ""))
        if not match or int(match[2]) not in (device_ids if match[1] == "d" else staff_ids):
            continue
        raw = row.get("data")
        try:
            order = clean_phone_order(json.loads(raw) if isinstance(raw, str) else raw)
        except ValueError:
            continue
        phone_orders[row["owner"]] = json.dumps(order)

    # Names and hiding chosen for TT Hotel's doors (by TT Hotel's lock ID).
    wristband_doors, lock_ids = [], set()
    for row in _rows(data, "wristband_doors"):
        lock_id = row.get("lock_id")
        if isinstance(lock_id, bool) or not isinstance(lock_id, int) or not 0 < lock_id <= 2**53:
            continue
        if lock_id not in lock_ids:
            lock_ids.add(lock_id)
            wristband_doors.append({"lock_id": lock_id, "name": _name(row.get("name")),
                                    "hidden": 1 if row.get("hidden") in (1, True) else 0})

    return {"settings": settings, "layout": layout, "logo": logo, "areas": areas,
            "area_entities": sorted(area_entities), "area_ha_areas": sorted(area_ha_areas),
            "roles": roles, "role_entities": role_entities, "role_areas": role_areas,
            "staff": staff, "devices": devices, "staff_devices": sorted(staff_devices),
            "staff_entities": staff_entities, "staff_areas": staff_areas,
            "phone_orders": phone_orders, "wristband_doors": wristband_doors,
            "departments": departments, "needs_setup": needs_setup,
            "logs": clean_logs(data),
            "exported_at": _time(data.get("exported_at"), time.time())}


# ---------------------------------------------------------------- logs in backups

def _text(value, limit: int) -> str | None:
    """A text field of a log row, cut to the length the add-on stores (None if it isn't text)."""
    return value[:limit] if isinstance(value, str) else None


def _whole(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _log_id(value) -> int | None:
    return value if _whole(value, 1, 2**53) else None


def _log_time(value, now: float) -> float | None:
    """When something in a log happened. A time more than a day ahead is damaged: it would sit
    at the top of the log for good."""
    found = _time(value)
    return found if found is not None and found <= now + FUTURE_SLACK else None


def _log_rows(data: dict, key: str):
    """The rows of one log section, each as {column: value} (one at a time, so a big section
    never exists twice). Raises ValueError for a damaged or oversized section."""
    section = data.get(key)
    if section is None:
        return
    columns, rows = ((section.get("columns"), section.get("rows")) if isinstance(section, dict)
                     else (None, None))
    if (not isinstance(columns, list) or not all(isinstance(c, str) for c in columns)
            or not isinstance(rows, list)):
        raise ValueError(f"The backup's {LOG_NAMES[key]} are damaged.")
    if len(rows) > MAX_LOG_ROWS[key]:
        raise ValueError(f"The backup has too many {LOG_NAMES[key]} (more than "
                         f"{MAX_LOG_ROWS[key]:,}).")
    for row in rows:
        if isinstance(row, list) and len(row) == len(columns):
            yield dict(zip(columns, row))


def _stored_records(raw) -> str:
    """What a card job wrote on its cards, from a backup (JSON text or a list), as it's stored
    here. Records that are damaged are left out: they only describe cards already made, and a
    replacement card is checked again from scratch."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, RecursionError):
        return "[]"
    cleaned = []
    for r in value[:MAX_JOB_RECORDS] if isinstance(value, list) else []:
        if not isinstance(r, dict) or r.get("level") not in ("door", "floor", "building", "all"):
            continue
        door, mac = r.get("door"), r.get("mac")
        if (not _whole(r.get("building"), 0, MAX_BUILDING)
                or not _whole(r.get("floor"), 0, MAX_FLOOR)
                or not isinstance(mac, str) or not MAC_RE.match(mac)
                or (door is not None and not _whole(door, 1, 2**53))):
            continue
        cleaned.append({"level": r["level"], "building": r["building"], "floor": r["floor"],
                        "mac": r["mac"], "door": door, "label": _name(r.get("label"), 100)})
    return json.dumps(cleaned)


def clean_logs(data: dict, now: float | None = None) -> dict | None:
    """The activity log and card records in a backup, row by row, ready for
    db.replace_everything to add to the ones here: {"activity": [ActivityRow], "wb_jobs":
    [CardJobRow], "wb_cards": [CardRow], "wb_lockouts": [LockoutRow]}. None for a backup from
    before logs were in backups (nothing is added). A damaged row is left out, since it only
    records something that already happened; a damaged section is refused like any other.

    Card jobs that were still running when the backup was made are stopped (nothing is working
    on them here), and so are remote lock-outs; they can be started again."""
    if not any(key in data for key in LOG_ROWS):
        return None
    now = time.time() if now is None else now
    activity = []
    for row in _log_rows(data, "activity"):
        ts, actor, action = (_log_time(row.get("ts"), now), _text(row.get("actor"), 120),
                             _text(row.get("action"), 120))
        if ts is None or not actor or not action:
            continue
        activity.append(ActivityRow(
            ts=ts, actor=actor, role=_text(row.get("role"), 80),
            entity_id=_text(row.get("entity_id"), 255), action=action,
            detail=_text(row.get("detail"), 300), ok=0 if row.get("ok") in (0, False) else 1,
            ip=_text(row.get("ip"), 64)))

    jobs, job_ids, stopped = [], set(), set()
    for row in _log_rows(data, "wb_jobs"):
        job_id, ts, expires = (_log_id(row.get("id")), _log_time(row.get("ts"), now),
                               _time(row.get("expires_at")))
        status, message = row.get("status"), _text(row.get("message"), 300)
        actor = _text(row.get("actor"), 120)
        if (job_id is None or job_id in job_ids or ts is None or expires is None or not actor
                or row.get("kind") not in WB_JOB_KINDS or row.get("card_type") not in TYPES
                or not _whole(row.get("count"), 1, 1000) or status not in WB_JOB_STATUSES):
            continue
        finished = _time(row.get("finished_at"))
        if status in WB_ACTIVE:
            stopped.add(job_id)
            status, message, finished = "cancelled", STOPPED_IN_BACKUP, finished or ts
        job_ids.add(job_id)
        jobs.append(CardJobRow(
            id=job_id, ts=ts, card_type=row["card_type"], name=_text(row.get("name"), 80) or "",
            count=row["count"], records=_stored_records(row.get("records")),
            summary=_text(row.get("summary"), 300) or "", expires_at=expires,
            allow_lockout=1 if row.get("allow_lockout") in (1, True) else 0,
            staff_id=_log_id(row.get("staff_id")), device_id=_log_id(row.get("device_id")),
            actor=actor, role=_text(row.get("role"), 80), ip=_text(row.get("ip"), 64),
            encoder_url=_text(row.get("encoder_url"), 200) or "", status=status, message=message,
            finished_at=finished, holder_staff_id=_log_id(row.get("holder_staff_id")),
            department_id=_log_id(row.get("department_id")), source=_text(row.get("source"), 20),
            kind=row["kind"], target_card_id=_log_id(row.get("target_card_id")),
            target_card=_text(row.get("target_card"), 40),
            cancel_until=_time(row.get("cancel_until"))))

    cards, card_ids, places = [], set(), set()
    for row in _log_rows(data, "wb_cards"):
        card_id, job_id, seq, status = (_log_id(row.get("id")), _log_id(row.get("job_id")),
                                        row.get("seq"), row.get("status"))
        if (card_id is None or card_id in card_ids or job_id not in job_ids
                or not _whole(seq, 1, 1000) or (job_id, seq) in places
                or status not in WB_CARD_STATUSES):
            continue
        if job_id in stopped and status in ("pending", "failed"):
            status = "cancelled"
        card_ids.add(card_id)
        places.add((job_id, seq))
        cards.append(CardRow(
            id=card_id, job_id=job_id, seq=seq, status=status,
            card_number=_text(row.get("card_number"), 40), error=_text(row.get("error"), 300),
            written_at=_time(row.get("written_at")),
            state=row.get("state") if row.get("state") in CARD_STATES else "active",
            state_at=_time(row.get("state_at")), state_by=_text(row.get("state_by"), 120),
            cancelled_card=_text(row.get("cancelled_card"), 40)))

    lockouts, seen = [], set()
    for row in _log_rows(data, "wb_lockouts"):
        card_id, lock_id = _log_id(row.get("card_id")), row.get("lock_id")
        method = row.get("method")
        status, detail, at = row.get("status"), _text(row.get("detail"), 300), _time(row.get("at"))
        if (card_id not in card_ids or not _whole(lock_id, 1, 2**53) or at is None
                or method not in LOCKOUT_METHODS or status not in LOCKOUT_STATUSES
                or (card_id, lock_id, method) in seen):
            continue
        if method == "remote" and status == "pending":
            status, detail = "failed", STOPPED_IN_BACKUP + " Lock it out again."
        seen.add((card_id, lock_id, method))
        lockouts.append(LockoutRow(card_id=card_id, lock_id=lock_id, method=method, status=status,
                                   detail=detail, at=at,
                                   tries=row["tries"] if _whole(row.get("tries"), 0, 1000) else 0))
    return {"activity": activity, "wb_jobs": jobs, "wb_cards": cards, "wb_lockouts": lockouts}


# ---------------------------------------------------------------- backup files

def pack_backup(data: dict) -> bytes:
    """A backup as it's downloaded and saved: compact JSON, gzip-compressed. The log sections
    are written a few thousand rows at a time, so the whole JSON text (tens of megabytes) never
    exists at once. Works only on data already read, so it can run off the event loop."""
    dump = lambda value: json.dumps(value, separators=(",", ":")).encode()
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=6, mtime=0) as packed:
        packed.write(b"{")
        for n, (key, value) in enumerate(data.items()):
            packed.write(b"," * bool(n) + dump(key) + b":")
            rows = value.get("rows") if key in LOG_ROWS and isinstance(value, dict) else None
            if not isinstance(rows, list):
                packed.write(dump(value))
                continue
            packed.write(b'{"columns":' + dump(value["columns"]) + b',"rows":[')
            for start in range(0, len(rows), 5000):
                packed.write(b"," * bool(start) + dump(rows[start:start + 5000])[1:-1])
            packed.write(b"]}")
        packed.write(b"}")
    return out.getvalue()


def gunzip(raw: bytes, limit: int | None = None) -> bytes:
    """Unpack gzip-compressed bytes, refusing more than `limit` bytes unpacked (MAX_BACKUP_BYTES
    unless given: a small file could otherwise unpack into gigabytes). Raises ValueError."""
    limit = MAX_BACKUP_BYTES if limit is None else limit
    unpacker = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    try:
        data = unpacker.decompress(raw, limit + 1)
    except zlib.error:
        raise ValueError("That file is damaged: it couldn't be unpacked.") from None
    if len(data) > limit:
        raise ValueError(f"That file is too big to restore (more than {limit // 2**20} MB "
                         "unpacked).")
    if not unpacker.eof:
        raise ValueError("That file is damaged: it ends too soon (was the download cut off?).")
    return data


def read_backup_file(raw: bytes) -> dict:
    """A file uploaded for a restore: a backup gzip-compressed (as downloaded since 0.8.6) or
    plain JSON (older backups, or one someone unpacked), told apart by its first bytes. Raises
    ValueError. Works only on bytes, so it can run off the event loop."""
    if raw[:2] == GZIP_MAGIC:
        raw = gunzip(raw)
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):   # not JSON, or not UTF-8
        raise ValueError(NOT_OURS) from None


def read_backup_path(path: str) -> dict:
    """A backup file saved here (a copy from before a restore), gzip-compressed or not."""
    with open(path, "rb") as f:
        return read_backup_file(f.read())


# ---------------------------------------------------------------- automatic safety copies

def save_safety_backup(data_dir: str, backup: dict) -> str:
    """Keep a copy of what a restore is about to replace, so it can be undone: a backup like a
    downloaded one (from make_backup, gzip-compressed). Works only on data already read, and the
    files, so it can run off the event loop."""
    folder = os.path.join(data_dir, "backups")
    os.makedirs(folder, exist_ok=True)
    name = time.strftime("before-restore-%Y%m%d-%H%M%S.json.gz")
    path = os.path.join(folder, name)
    with open(path + ".part", "wb") as f:
        f.write(pack_backup(backup))
    os.replace(path + ".part", path)   # never half a copy under the real name
    for old in safety_backups(data_dir)[KEEP_SAFETY_BACKUPS:]:
        os.remove(os.path.join(folder, old["name"]))
    return name


def safety_backups(data_dir: str) -> list[dict]:
    folder = os.path.join(data_dir, "backups")
    if not os.path.isdir(folder):
        return []
    found = [{"name": n, "size": os.path.getsize(os.path.join(folder, n)),
              "saved_at": os.path.getmtime(os.path.join(folder, n))}
             for n in os.listdir(folder) if SAFETY_NAME_RE.match(n)]
    return sorted(found, key=lambda b: b["name"], reverse=True)


def safety_backup_path(data_dir: str, name: str) -> str | None:
    if not SAFETY_NAME_RE.match(name or ""):
        return None
    path = os.path.join(data_dir, "backups", name)
    return path if os.path.isfile(path) else None
