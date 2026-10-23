"""Exporting and importing layouts, roles and staff, and whole backups of Staff Access.

Files are JSON with a small envelope ({"format": "staff-portal", "kind": ...}) so the wrong file
is refused with a clear message. Everything read from a file is validated as strictly as the
same data typed into the dashboard.

What files never contain:
- Home Assistant sign-in tokens for linked users. After restoring onto the same system, links
  are kept for phones and staff that still match; otherwise they need relinking.
- Pending one-time setup codes. Restored phones still waiting for setup get new QR codes.
Staff PINs are only ever stored scrambled (scrypt), and that is what exports carry.
"""

import base64
import binascii
import json
import os
import re
import time

from layout import clean_layout, clean_phone_order, clean_phone_pages
from permissions import controllable, offered

FORMAT = "staff-portal"
BACKUP_SCHEMA = 1
KINDS = {"layout": "a layout", "roles": "roles", "staff": "staff members", "backup": "a full backup"}
PIN_HASH_RE = re.compile(r"^[0-9a-f]{32}\$[0-9a-f]{128}$")
TOKEN_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
OWNER_RE = re.compile(r"^([ds]):(\d{1,9})$")
LEVELS = ("view", "control")
DEVICE_STATUSES = ("pending", "active", "locked", "revoked")
STAFF_STATUSES = ("active", "disabled", "locked")
IMPORT_MODES = ("replace", "copy", "skip")
MAX_ROWS = 5000          # per table in a backup, or entries in an export
MAX_LOGO_BYTES = 6 * 1024 * 1024
KEEP_SAFETY_BACKUPS = 5
SAFETY_NAME_RE = re.compile(r"^before-restore-\d{8}-\d{6}\.json$")


# ---------------------------------------------------------------- helpers

def _envelope(kind: str, version: str, **data) -> dict:
    return {"format": FORMAT, "kind": kind, "app_version": version,
            "exported_at": int(time.time()), **data}


def check_file(data, kind: str) -> None:
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("That isn't a Staff Portal export or backup file.")
    if data.get("kind") != kind:
        found = KINDS.get(data.get("kind"), "something else")
        raise ValueError(f"That file holds {found}, not {KINDS[kind]}.")


def _name(value, limit: int = 60) -> str:
    return str(value or "").strip()[:limit]


def _rows(data: dict, key: str) -> list:
    value = data.get(key) or []
    if not isinstance(value, list):
        raise ValueError(f"The backup's {key} section is damaged.")
    if len(value) > MAX_ROWS:
        raise ValueError(f"The backup has too many {key} (more than {MAX_ROWS}).")
    return [row for row in value if isinstance(row, dict)]


def _int(value, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 10**9:
        raise ValueError(f"The backup has a damaged {what}.")
    return value


def _time(value, default: float | None = None) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else default


def _level(entity_id: str, level: str) -> str:
    return "control" if level == "control" and controllable(entity_id) else "view"


def _unique(name: str, taken: set[str]) -> str:
    """name (imported), name (imported 2)... whichever isn't taken (case-insensitively)."""
    base = f"{name[:46]} (imported"
    candidate, n = f"{base})", 2
    while candidate.lower() in taken:
        candidate, n = f"{base} {n})", n + 1
    return candidate


# ---------------------------------------------------------------- exports

def export_layout(db, version: str) -> dict:
    # Area items point at area IDs, which differ between systems; the names travel with them.
    return _envelope("layout", version, layout=db.layout(),
                     areas=[{"id": a["id"], "name": a["name"]} for a in db.areas()])


def export_roles(db, version: str) -> dict:
    area_names = {a["id"]: a["name"] for a in db.areas()}
    return _envelope("roles", version, roles=[
        {"name": r["name"], "entities": r["entities"],
         "areas": {area_names[a]: lvl for a, lvl in r["areas"].items() if a in area_names}}
        for r in db.roles()])


def export_staff(db, version: str) -> dict:
    phones = {r["id"]: r["name"] for r in db.conn.execute(
        "SELECT id, name FROM devices WHERE status != 'revoked'")}
    allowed: dict[int, list[str]] = {}
    for staff_id, device_id in db.conn.execute("SELECT staff_id, device_id FROM staff_devices"):
        if device_id in phones:
            allowed.setdefault(staff_id, []).append(phones[device_id])
    return _envelope("staff", version, staff=[
        {"name": r["name"], "pin_hash": r["pin_hash"], "enabled": r["status"] != "disabled",
         "phones": sorted(allowed.get(r["id"], []))}
        for r in db.conn.execute("SELECT id, name, pin_hash, status FROM staff ORDER BY name")])


# ---------------------------------------------------------------- imports (merge into what's here)

def import_roles(db, data, mode: str) -> dict:
    check_file(data, "roles")
    if mode not in IMPORT_MODES:
        raise ValueError("Choose what to do with roles that already exist.")
    area_ids = {a["name"].lower(): a["id"] for a in db.areas()}
    existing = {r["name"].lower(): r for r in db.roles()}
    result = {"added": 0, "replaced": 0, "skipped": 0, "missing_areas": set()}
    for raw in _rows(data, "roles"):
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
        if match and mode == "replace":
            db.update_role(match["id"], match["name"], entities, areas)
            result["replaced"] += 1
            continue
        if match:
            name = _unique(name, set(existing))
        role_id = db.create_role(name)
        db.update_role(role_id, name, entities, areas)
        existing[name.lower()] = {"id": role_id, "name": name}
        result["added"] += 1
    result["missing_areas"] = sorted(result["missing_areas"])
    return result


def import_staff(db, data, mode: str) -> dict:
    check_file(data, "staff")
    if mode not in IMPORT_MODES:
        raise ValueError("Choose what to do with staff members who already exist.")
    phones = {r["name"].lower(): r["id"] for r in db.conn.execute(
        "SELECT id, name FROM devices WHERE status != 'revoked'")}
    existing = {r["name"].lower(): r["id"] for r in db.conn.execute("SELECT id, name FROM staff")}
    result = {"added": 0, "replaced": 0, "skipped": 0, "bad_pin": 0, "missing_phones": set()}
    for raw in _rows(data, "staff"):
        name = _name(raw.get("name"), 40)
        pin_hash = str(raw.get("pin_hash") or "")
        if not name:
            continue
        if not PIN_HASH_RE.match(pin_hash):
            result["bad_pin"] += 1
            continue
        status = "active" if raw.get("enabled", True) is not False else "disabled"
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
            db.conn.execute(
                """UPDATE staff SET pin_hash = ?, status = ?, failed_pins = 0, locked_until = NULL
                   WHERE id = ?""", (pin_hash, status, staff_id))
            result["replaced"] += 1
        else:
            if staff_id:
                name = _unique(name, set(existing))[:40]
            staff_id = db.conn.execute(
                "INSERT INTO staff (name, pin_hash, status, created_at) VALUES (?, ?, ?, ?)",
                (name, pin_hash, status, time.time())).lastrowid
            existing[name.lower()] = staff_id
            result["added"] += 1
        db.conn.executemany(
            "INSERT OR IGNORE INTO staff_devices (staff_id, device_id) VALUES (?, ?)",
            [(staff_id, d) for d in device_ids])
        db.conn.commit()
    result["missing_phones"] = sorted(result["missing_phones"])
    return result


# ---------------------------------------------------------------- full backup and restore

def make_backup(db, branding, version: str) -> dict:
    rows = lambda sql: [dict(r) for r in db.conn.execute(sql)]
    logo = branding.logo_png()
    return _envelope(
        "backup", version, schema=BACKUP_SCHEMA,
        settings=db.settings(),
        layout=db.layout(),
        logo=base64.b64encode(logo).decode() if logo else None,
        areas=rows("SELECT id, name, sort_order, created_at FROM areas"),
        area_entities=rows("SELECT area_id, entity_id FROM area_entities"),
        area_ha_areas=rows("SELECT area_id, ha_area_id FROM area_ha_areas"),
        roles=rows("SELECT id, name, created_at FROM roles"),
        role_entities=rows("SELECT role_id, entity_id, level FROM role_entities"),
        role_areas=rows("SELECT role_id, area_id, level FROM role_areas"),
        devices=rows("""SELECT id, name, role_id, status, token_hash, pin_hash, signin_mode,
                        staff_all, layout_pages, created_at, last_seen
                        FROM devices WHERE status != 'deleted'"""),
        staff=rows("SELECT id, name, pin_hash, status, created_at, last_seen FROM staff"),
        staff_devices=rows("SELECT staff_id, device_id FROM staff_devices"),
        phone_orders=rows("SELECT owner, data FROM phone_orders"),
    )


def backup_summary(data: dict) -> dict:
    return {"exported_at": data.get("exported_at"), "app_version": data.get("app_version"),
            "property_name": (data.get("settings") or {}).get("property_name"),
            **{k: len(data.get(k) or []) for k in ("roles", "areas", "devices", "staff")},
            "pages": len((data.get("layout") or {}).get("pages") or [])}


def clean_backup(data, clean_settings, current_settings: dict, clean_logo) -> dict:
    """Validate a whole backup before anything is replaced. Raises ValueError."""
    check_file(data, "backup")
    if data.get("schema") != BACKUP_SCHEMA:
        raise ValueError("That backup was made by a newer version of Staff Portal. Update the add-on first.")

    settings_in = data.get("settings") if isinstance(data.get("settings"), dict) else {}
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
        if not name or name.lower() in names or any(a["id"] == area_id for a in areas):
            raise ValueError("The backup has a damaged or repeated area.")
        names.add(name.lower())
        order = row.get("sort_order")
        areas.append({"id": area_id, "name": name,
                      "sort_order": order if isinstance(order, int) and not isinstance(order, bool) else 0,
                      "created_at": _time(row.get("created_at"), time.time())})
    area_ids = {a["id"] for a in areas}
    area_entities = {(r["area_id"], r["entity_id"]) for r in _rows(data, "area_entities")
                     if r.get("area_id") in area_ids and offered(str(r.get("entity_id") or ""))}
    area_ha_areas = {(r["area_id"], r["ha_area_id"]) for r in _rows(data, "area_ha_areas")
                     if r.get("area_id") in area_ids and isinstance(r.get("ha_area_id"), str)
                     and 0 < len(r["ha_area_id"]) <= 100}

    roles, names = [], set()
    for row in _rows(data, "roles"):
        role_id, name = _int(row.get("id"), "role"), _name(row.get("name"))
        if not name or name.lower() in names or any(r["id"] == role_id for r in roles):
            raise ValueError("The backup has a damaged or repeated role.")
        names.add(name.lower())
        roles.append({"id": role_id, "name": name, "created_at": _time(row.get("created_at"), time.time())})
    role_ids = {r["id"] for r in roles}
    role_entities = {(r["role_id"], r["entity_id"]): _level(r["entity_id"], r["level"])
                     for r in _rows(data, "role_entities")
                     if r.get("role_id") in role_ids and offered(str(r.get("entity_id") or ""))
                     and r.get("level") in LEVELS}
    role_areas = {(r["role_id"], r["area_id"]): r["level"] for r in _rows(data, "role_areas")
                  if r.get("role_id") in role_ids and r.get("area_id") in area_ids
                  and r.get("level") in LEVELS}

    staff, names = [], set()
    for row in _rows(data, "staff"):
        staff_id, name = _int(row.get("id"), "staff member"), _name(row.get("name"), 40)
        pin_hash = str(row.get("pin_hash") or "")
        if not name or name.lower() in names or any(m["id"] == staff_id for m in staff):
            raise ValueError("The backup has a damaged or repeated staff member.")
        if not PIN_HASH_RE.match(pin_hash):
            raise ValueError(f"The backup has a damaged PIN for {name}.")
        names.add(name.lower())
        staff.append({"id": staff_id, "name": name, "pin_hash": pin_hash,
                      "status": row.get("status") if row.get("status") in STAFF_STATUSES else "active",
                      "created_at": _time(row.get("created_at"), time.time()),
                      "last_seen": _time(row.get("last_seen"))})
    staff_ids = {m["id"] for m in staff}

    devices = []
    for row in _rows(data, "devices"):
        device_id, name = _int(row.get("id"), "phone"), _name(row.get("name"))
        if not name or any(d["id"] == device_id for d in devices):
            raise ValueError("The backup has a damaged or repeated phone.")
        token_hash = row.get("token_hash") if TOKEN_HASH_RE.match(str(row.get("token_hash") or "")) else None
        pin_hash = row.get("pin_hash")
        if pin_hash is not None and not PIN_HASH_RE.match(str(pin_hash)):
            raise ValueError(f"The backup has a damaged PIN for the phone {name}.")
        status = row.get("status") if row.get("status") in DEVICE_STATUSES else "pending"
        if status in ("active", "locked") and not token_hash:
            status = "pending"  # nothing to sign in with: set it up again
        if status in ("pending", "revoked"):
            token_hash = None
        pages = row.get("layout_pages")
        if isinstance(pages, str):
            try:
                pages = json.loads(pages)
            except ValueError:
                pages = None
        pages = clean_phone_pages(pages if isinstance(pages, list) else None)
        role_id = row.get("role_id") if row.get("role_id") in role_ids else None
        devices.append({"id": device_id, "name": name, "role_id": role_id, "status": status,
                        "token_hash": token_hash, "pin_hash": pin_hash,
                        "signin_mode": "staff" if row.get("signin_mode") == "staff" else "phone",
                        "staff_all": 0 if row.get("staff_all") in (0, False) else 1,
                        "layout_pages": json.dumps(pages) if pages is not None else None,
                        "created_at": _time(row.get("created_at"), time.time()),
                        "last_seen": _time(row.get("last_seen"))})
    device_ids = {d["id"] for d in devices}
    staff_devices = {(r["staff_id"], r["device_id"]) for r in _rows(data, "staff_devices")
                     if r.get("staff_id") in staff_ids and r.get("device_id") in device_ids}

    phone_orders = {}
    for row in _rows(data, "phone_orders"):
        match = OWNER_RE.match(str(row.get("owner") or ""))
        if not match or int(match[2]) not in (device_ids if match[1] == "d" else staff_ids):
            continue
        raw = row.get("data")
        try:
            order = clean_phone_order(json.loads(raw) if isinstance(raw, str) else raw)
        except ValueError:
            continue
        phone_orders[row["owner"]] = json.dumps(order)

    return {"settings": settings, "layout": layout, "logo": logo, "areas": areas,
            "area_entities": sorted(area_entities), "area_ha_areas": sorted(area_ha_areas),
            "roles": roles, "role_entities": role_entities, "role_areas": role_areas,
            "staff": staff, "devices": devices, "staff_devices": sorted(staff_devices),
            "phone_orders": phone_orders}


# ---------------------------------------------------------------- automatic safety copies

def save_safety_backup(data_dir: str, backup: dict) -> str:
    """Keep a copy of what a restore is about to replace, so it can be undone."""
    folder = os.path.join(data_dir, "backups")
    os.makedirs(folder, exist_ok=True)
    name = time.strftime("before-restore-%Y%m%d-%H%M%S.json")
    with open(os.path.join(folder, name), "w") as f:
        json.dump(backup, f)
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
