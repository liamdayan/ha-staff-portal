"""SQLite storage for roles, phones, settings and the activity log."""

import hashlib
import json
import os
import secrets
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS role_entities (
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('view', 'control')),
    PRIMARY KEY (role_id, entity_id)
);
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role_id INTEGER REFERENCES roles(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    token_hash TEXT UNIQUE,
    enroll_code TEXT UNIQUE,
    enroll_expires REAL,
    pin_hash TEXT,
    failed_pins INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    role TEXT,
    entity_id TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    ok INTEGER NOT NULL DEFAULT 1,
    ip TEXT
);
CREATE INDEX IF NOT EXISTS activity_ts ON activity (ts DESC);
CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    pin_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    failed_pins INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_seen REAL,
    ha_user TEXT,
    ha_refresh_token TEXT,
    ha_link_error TEXT
);
CREATE TABLE IF NOT EXISTS staff_devices (
    staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    PRIMARY KEY (staff_id, device_id)
);
-- Areas are groupings defined in Staff Access (such as "Soaking Area"). An area holds
-- individual devices and/or whole Home Assistant areas, which stay in sync automatically.
CREATE TABLE IF NOT EXISTS areas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS area_entities (
    area_id INTEGER NOT NULL REFERENCES areas(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL,
    PRIMARY KEY (area_id, entity_id)
);
CREATE TABLE IF NOT EXISTS area_ha_areas (
    area_id INTEGER NOT NULL REFERENCES areas(id) ON DELETE CASCADE,
    ha_area_id TEXT NOT NULL,
    PRIMARY KEY (area_id, ha_area_id)
);
CREATE TABLE IF NOT EXISTS role_areas (
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    area_id INTEGER NOT NULL REFERENCES areas(id) ON DELETE CASCADE,
    level TEXT NOT NULL CHECK (level IN ('view', 'control')),
    PRIMARY KEY (role_id, area_id)
);
-- Every device token this system has issued and not since revoked (by hash). A restore only
-- keeps a phone signed in if its token is here, so a tampered backup can't add a working phone
-- and an old backup can't bring back a phone that was revoked. Survives restores, so undoing
-- one keeps phones signed in.
CREATE TABLE IF NOT EXISTS device_tokens (
    token_hash TEXT PRIMARY KEY,
    device_id INTEGER NOT NULL,
    issued_at REAL
);

-- A phone's own arrangement (or, on shared phones, one staff member's): JSON
-- {"pages": [page IDs], "items": {page ID: [item IDs]}}. Owner is "d:<device id>" or "s:<staff id>".
CREATE TABLE IF NOT EXISTS phone_orders (
    owner TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
"""

# Columns added after the first release, applied to existing databases on startup.
MIGRATIONS = {
    "devices": {
        "ha_user": "TEXT",
        "ha_refresh_token": "TEXT",
        "ha_link_error": "TEXT",
        # 'phone': the phone itself is the user (optional phone PIN).
        # 'staff': shared phone, each staff member signs in with their own PIN.
        "signin_mode": "TEXT NOT NULL DEFAULT 'phone'",
        # In 'staff' mode: 1 = every active staff member may sign in, 0 = only staff_devices.
        "staff_all": "INTEGER NOT NULL DEFAULT 1",
        # In 'staff' mode: 1 = no list of names; people just type their PIN.
        "pin_only": "INTEGER NOT NULL DEFAULT 0",
        # JSON list of layout page IDs this phone shows ("more" = the More page).
        # NULL means every page, including pages added later.
        "layout_pages": "TEXT",
        # When this phone's shared-sign-in failure count was last increased; the count is
        # forgotten after a quiet spell rather than reset by someone else signing in.
        "failed_at": "REAL",
        # Wrong PINs on a shared device in the current 24-hour window, and when it started.
        # Unlike failed_pins this isn't forgotten after a quiet spell.
        "failed_day_count": "INTEGER NOT NULL DEFAULT 0",
        "failed_day_since": "REAL",
        # 'phone' or 'computer' (a front desk PC, say). Same app and rules; it only changes how
        # the device is set up and described.
        "kind": "TEXT NOT NULL DEFAULT 'phone'",
    },
    "staff": {
        # Too many wrong PINs locks a person out for a while (not permanently), so one
        # colleague can't lock everyone out. An admin can unlock sooner.
        "locked_until": "REAL",
        # Linked Connecteam user (their user ID there), and whether they may only sign in while
        # clocked in on Connecteam's time clock.
        "connecteam_user_id": "TEXT",
        "require_clock_in": "INTEGER NOT NULL DEFAULT 0",
    },
}

DEFAULT_SETTINGS = {
    "property_name": "Staff Portal",
    "app_name": "Staff",
    "accent_color": "#2f6f4f",
    "icon_background": "#ffffff",
    "public_url": "",
    "pin_timeout_minutes": "15",
    "max_pin_attempts": "5",
    "enroll_hours": "72",
    "ha_logbook": "1",
    "allow_phone_reorder": "1",
    "control_lock": "1",  # controls need a hold-to-unlock, and lock again after a quiet spell
    "control_lock_minutes": "10",
    "phones_via_tunnel_only": "0",
    "tunnel_addresses": "",  # where cloudflared connects from, when it isn't the add-on
    "connecteam_enabled": "0",
    "connecteam_client_id": "",
    "connecteam_client_secret": "",  # never sent to the browser or put in backups
    "connecteam_region": "global",   # "au" for Australian-hosted accounts
    "connecteam_poll_minutes": "60",
    "connecteam_usage": "{}",        # requests made today (internal: never shown or backed up)
    "require_pin": "0",
}

ENROLL_TTL_DEFAULT = 72 * 3600
ACTIVITY_KEEP_DAYS = 180
ACTIVITY_MAX_ROWS = 50_000


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}${digest.hex()}"


def check_pin(pin: str, stored: str) -> bool:
    salt_hex, digest_hex = stored.split("$", 1)
    digest = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return secrets.compare_digest(digest.hex(), digest_hex)


class Database:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.conn = sqlite3.connect(os.path.join(data_dir, "staff_portal.db"))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for column, kind in columns.items():
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        for key, value in DEFAULT_SETTINGS.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )
        # Phones signed in before device_tokens existed (or set up by an older version).
        self.conn.execute(
            """INSERT OR IGNORE INTO device_tokens (token_hash, device_id, issued_at)
               SELECT token_hash, id, ? FROM devices WHERE token_hash IS NOT NULL""", (time.time(),))
        self.conn.commit()

    # Settings

    def settings(self) -> dict:
        return {r["key"]: r["value"] for r in self.conn.execute(
            "SELECT key, value FROM settings WHERE key != 'layout'")}

    def update_settings(self, values: dict) -> None:
        for key, value in values.items():
            if key in DEFAULT_SETTINGS:
                self.conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value))
                )
        self.conn.commit()

    def layout(self) -> dict:
        row = self.conn.execute("SELECT value FROM settings WHERE key = 'layout'").fetchone()
        try:
            return json.loads(row[0]) if row else {"pages": [], "show_unplaced": True}
        except ValueError:
            return {"pages": [], "show_unplaced": True}

    def set_layout(self, layout: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('layout', ?)",
                          (json.dumps(layout),))
        self.conn.commit()

    def setting_int(self, key: str) -> int:
        try:
            return int(self.settings().get(key, DEFAULT_SETTINGS[key]))
        except ValueError:
            return int(DEFAULT_SETTINGS[key])

    def name_taken(self, table: str, name: str, except_id: int | None = None) -> bool:
        """Whether another role, area or staff member already has this name, ignoring capitals
        ("Bob" and "bob" would be confusing on a phone, and import and restore match names
        that way)."""
        assert table in ("roles", "areas", "staff")
        row = self.conn.execute(
            f"SELECT 1 FROM {table} WHERE name = ? COLLATE NOCASE AND id != ? LIMIT 1",
            (name, except_id if except_id is not None else -1)).fetchone()
        return row is not None

    # Roles

    def roles(self) -> list[dict]:
        roles = [dict(r) for r in self.conn.execute("SELECT * FROM roles ORDER BY name")]
        for role in roles:
            role["entities"] = self.role_entities(role["id"])
            role["areas"] = self.role_areas(role["id"])
            role["device_count"] = self.conn.execute(
                "SELECT COUNT(*) FROM devices WHERE role_id = ? AND status != 'revoked'",
                (role["id"],),
            ).fetchone()[0]
        return roles

    def role(self, role_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
        if not row:
            return None
        role = dict(row)
        role["entities"] = self.role_entities(role_id)
        role["areas"] = self.role_areas(role_id)
        return role

    def role_areas(self, role_id: int) -> dict[int, str]:
        rows = self.conn.execute("SELECT area_id, level FROM role_areas WHERE role_id = ?", (role_id,))
        return {r["area_id"]: r["level"] for r in rows}

    def role_entities(self, role_id: int) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT entity_id, level FROM role_entities WHERE role_id = ?", (role_id,)
        )
        return {r["entity_id"]: r["level"] for r in rows}

    def create_role(self, name: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO roles (name, created_at) VALUES (?, ?)", (name, time.time())
        )
        self.conn.commit()
        return cur.lastrowid

    def duplicate_role(self, role_id: int, name: str) -> int:
        """A new role with the same device and area settings. Phones stay on the original."""
        new_id = self.create_role(name)
        self.update_role(new_id, name, self.role_entities(role_id), self.role_areas(role_id))
        return new_id

    def update_role(self, role_id: int, name: str, entities: dict[str, str],
                    areas: dict[int, str] | None = None) -> None:
        self.conn.execute("UPDATE roles SET name = ? WHERE id = ?", (name, role_id))
        self.conn.execute("DELETE FROM role_entities WHERE role_id = ?", (role_id,))
        self.conn.executemany(
            "INSERT INTO role_entities (role_id, entity_id, level) VALUES (?, ?, ?)",
            [(role_id, eid, lvl) for eid, lvl in entities.items() if lvl in ("view", "control")],
        )
        if areas is not None:
            self.conn.execute("DELETE FROM role_areas WHERE role_id = ?", (role_id,))
            self.conn.executemany(
                "INSERT INTO role_areas (role_id, area_id, level) VALUES (?, ?, ?)",
                [(role_id, aid, lvl) for aid, lvl in areas.items() if lvl in ("view", "control")],
            )
        self.conn.commit()

    # Areas (groupings defined in Staff Access)

    def areas(self) -> list[dict]:
        areas = [dict(r) for r in self.conn.execute(
            "SELECT id, name, sort_order FROM areas ORDER BY sort_order, id")]
        for area in areas:
            area["entities"] = [r[0] for r in self.conn.execute(
                "SELECT entity_id FROM area_entities WHERE area_id = ? ORDER BY entity_id",
                (area["id"],))]
            area["ha_areas"] = [r[0] for r in self.conn.execute(
                "SELECT ha_area_id FROM area_ha_areas WHERE area_id = ? ORDER BY ha_area_id",
                (area["id"],))]
        return areas

    def area(self, area_id: int) -> dict | None:
        return next((a for a in self.areas() if a["id"] == area_id), None)

    def create_area(self, name: str) -> int:
        next_order = self.conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM areas").fetchone()[0]
        cur = self.conn.execute(
            "INSERT INTO areas (name, sort_order, created_at) VALUES (?, ?, ?)",
            (name, next_order, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_area(self, area_id: int, name: str, entities: list[str], ha_areas: list[str]) -> None:
        self.conn.execute("UPDATE areas SET name = ? WHERE id = ?", (name, area_id))
        self.conn.execute("DELETE FROM area_entities WHERE area_id = ?", (area_id,))
        self.conn.executemany("INSERT OR IGNORE INTO area_entities (area_id, entity_id) VALUES (?, ?)",
                              [(area_id, e) for e in entities])
        self.conn.execute("DELETE FROM area_ha_areas WHERE area_id = ?", (area_id,))
        self.conn.executemany("INSERT OR IGNORE INTO area_ha_areas (area_id, ha_area_id) VALUES (?, ?)",
                              [(area_id, a) for a in ha_areas])
        self.conn.commit()

    def reorder_areas(self, area_ids: list[int]) -> None:
        self.conn.executemany("UPDATE areas SET sort_order = ? WHERE id = ?",
                              [(i, aid) for i, aid in enumerate(area_ids, 1)])
        self.conn.commit()

    def delete_area(self, area_id: int) -> None:
        self.conn.execute("DELETE FROM areas WHERE id = ?", (area_id,))
        self.conn.commit()

    def delete_role(self, role_id: int) -> None:
        self.conn.execute("DELETE FROM roles WHERE id = ?", (role_id,))
        self.conn.commit()

    # Devices (staff phones)

    def devices(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT d.*, r.name AS role_name FROM devices d
               LEFT JOIN roles r ON r.id = d.role_id
               WHERE d.status != 'deleted' ORDER BY d.name"""
        )
        result = []
        for r in rows:
            d = dict(r)
            d["has_pin"] = bool(d.pop("pin_hash"))
            d.pop("token_hash")
            d.pop("ha_refresh_token")
            if d["status"] != "pending" or (d["enroll_expires"] or 0) < time.time():
                d["enroll_code"] = None
            result.append(d)
        return result

    def device(self, device_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        return dict(row) if row else None

    def create_device(self, name: str, role_id: int | None, pin: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO devices (name, role_id, pin_hash, created_at) VALUES (?, ?, ?, ?)",
            (name, role_id, hash_pin(pin) if pin else None, time.time()),
        )
        self.conn.commit()
        self.new_enroll_code(cur.lastrowid)
        return cur.lastrowid

    def update_device(self, device_id: int, **fields) -> None:
        allowed = {"name", "role_id", "status", "pin_hash", "failed_pins", "kind",
                   "ha_user", "ha_refresh_token", "ha_link_error", "signin_mode", "staff_all",
                   "layout_pages", "failed_at", "pin_only", "failed_day_count", "failed_day_since"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k} = ?" for k in sets)
        self.conn.execute(f"UPDATE devices SET {cols} WHERE id = ?", (*sets.values(), device_id))
        self.conn.commit()

    def set_device_staff(self, device_id: int, staff_ids: list[int]) -> None:
        self.conn.execute("DELETE FROM staff_devices WHERE device_id = ?", (device_id,))
        self.conn.executemany(
            "INSERT OR IGNORE INTO staff_devices (staff_id, device_id) VALUES (?, ?)",
            [(sid, device_id) for sid in staff_ids],
        )
        self.conn.commit()

    def device_staff_ids(self, device_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT staff_id FROM staff_devices WHERE device_id = ?", (device_id,))
        return [r[0] for r in rows]

    # Staff members (people who sign in on shared phones)

    def staff_list(self) -> list[dict]:
        result = []
        for r in self.conn.execute("SELECT * FROM staff ORDER BY name COLLATE NOCASE"):
            m = dict(r)
            m.pop("pin_hash")
            m.pop("ha_refresh_token")
            result.append(m)
        return result

    def staff(self, staff_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()
        return dict(row) if row else None

    def staff_for_device(self, device: dict) -> list[dict]:
        """Active staff members allowed to sign in on a shared phone."""
        cols = "s.id, s.name, s.connecteam_user_id, s.require_clock_in"
        if device["staff_all"]:
            rows = self.conn.execute(
                f"SELECT {cols} FROM staff s WHERE status != 'disabled' ORDER BY name COLLATE NOCASE")
        else:
            rows = self.conn.execute(
                f"""SELECT {cols} FROM staff s JOIN staff_devices sd ON sd.staff_id = s.id
                   WHERE sd.device_id = ? AND s.status != 'disabled'
                   ORDER BY s.name COLLATE NOCASE""", (device["id"],))
        return [dict(r) for r in rows]

    def staff_pins(self, device: dict | None = None) -> list[tuple[int, str]]:
        """(id, pin_hash) of the active staff allowed on a shared device (or of everyone)."""
        ids = {m["id"] for m in self.staff_for_device(device)} if device else None
        return [(r["id"], r["pin_hash"]) for r in self.conn.execute("SELECT id, pin_hash FROM staff")
                if ids is None or r["id"] in ids]

    def staff_allowed_on(self, staff_id: int, device: dict) -> bool:
        return any(m["id"] == staff_id for m in self.staff_for_device(device))

    def create_staff(self, name: str, pin: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO staff (name, pin_hash, created_at) VALUES (?, ?, ?)",
            (name, hash_pin(pin), time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def staff_by_connecteam(self, user_id: str) -> dict | None:
        row = self.conn.execute("SELECT id, name FROM staff WHERE connecteam_user_id = ?",
                                (str(user_id),)).fetchone()
        return dict(row) if row else None

    def update_staff(self, staff_id: int, **fields) -> None:
        allowed = {"name", "pin_hash", "status", "failed_pins", "last_seen", "locked_until",
                   "ha_user", "ha_refresh_token", "ha_link_error", "connecteam_user_id",
                   "require_clock_in"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k} = ?" for k in sets)
        self.conn.execute(f"UPDATE staff SET {cols} WHERE id = ?", (*sets.values(), staff_id))
        self.conn.commit()

    def delete_staff(self, staff_id: int) -> None:
        self.conn.execute("DELETE FROM phone_orders WHERE owner = ?", (f"s:{staff_id}",))
        self.conn.execute("DELETE FROM staff WHERE id = ?", (staff_id,))
        self.conn.commit()

    def new_enroll_code(self, device_id: int) -> str:
        """Issue a fresh one-time setup code. Any phone already using this entry is signed out."""
        code = secrets.token_urlsafe(24)
        ttl = self.setting_int("enroll_hours") * 3600 or ENROLL_TTL_DEFAULT
        self.conn.execute(
            """UPDATE devices SET enroll_code = ?, enroll_expires = ?, token_hash = NULL,
               status = 'pending', failed_pins = 0 WHERE id = ?""",
            (code, time.time() + ttl, device_id),
        )
        self.conn.execute("DELETE FROM device_tokens WHERE device_id = ?", (device_id,))
        self.conn.commit()
        return code

    def claim_enroll_code(self, code: str) -> tuple[dict, str] | None:
        row = self.conn.execute(
            "SELECT * FROM devices WHERE enroll_code = ? AND status = 'pending'", (code,)
        ).fetchone()
        if not row or (row["enroll_expires"] or 0) < time.time():
            return None
        token = secrets.token_urlsafe(32)
        self.conn.execute(
            """UPDATE devices SET token_hash = ?, enroll_code = NULL, enroll_expires = NULL,
               status = 'active', last_seen = ? WHERE id = ?""",
            (hash_token(token), time.time(), row["id"]),
        )
        self.conn.execute("INSERT OR REPLACE INTO device_tokens (token_hash, device_id, issued_at) "
                          "VALUES (?, ?, ?)", (hash_token(token), row["id"], time.time()))
        self.conn.commit()
        return dict(row), token

    def revoke_tokens(self, device_id: int) -> None:
        """Forget every token issued to a device, so no backup can bring it back."""
        self.conn.execute("DELETE FROM device_tokens WHERE device_id = ?", (device_id,))
        self.conn.commit()

    def known_tokens(self) -> set[tuple[int, str]]:
        """(device ID, token hash) for every token this system issued and hasn't revoked."""
        return {(r["device_id"], r["token_hash"])
                for r in self.conn.execute("SELECT device_id, token_hash FROM device_tokens")}

    def device_by_token(self, token: str) -> dict | None:
        row = self.conn.execute(
            """SELECT d.*, r.name AS role_name FROM devices d
               LEFT JOIN roles r ON r.id = d.role_id WHERE d.token_hash = ?""",
            (hash_token(token),),
        ).fetchone()
        return dict(row) if row else None

    def touch_device(self, device_id: int) -> None:
        self.conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (time.time(), device_id))
        self.conn.commit()

    def delete_device(self, device_id: int) -> None:
        self.conn.execute("DELETE FROM phone_orders WHERE owner = ?", (f"d:{device_id}",))
        self.conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        self.conn.execute("DELETE FROM device_tokens WHERE device_id = ?", (device_id,))
        self.conn.commit()

    # Restoring a backup

    def replace_everything(self, snap: dict) -> list[str]:
        """Swap in a validated backup (from transfer.clean_backup) in one transaction: either all
        of it lands or none of it does. The activity log is kept. Home Assistant links are
        kept for phones and staff that still exist with the same ID and name. Returns the Home
        Assistant refresh tokens of links that didn't carry over, so the caller can revoke them."""
        c = self.conn
        links = {
            table: {r["id"]: r for r in c.execute(
                f"SELECT id, name, ha_user, ha_refresh_token, ha_link_error"
                f"{', token_hash' if table == 'devices' else ''} FROM {table}")}
            for table in ("devices", "staff")
        }
        try:
            for table in ("phone_orders", "staff_devices", "role_areas", "role_entities",
                          "area_entities", "area_ha_areas", "devices", "staff", "roles", "areas"):
                c.execute(f"DELETE FROM {table}")
            c.executemany("INSERT INTO areas (id, name, sort_order, created_at) VALUES (?, ?, ?, ?)",
                          [(a["id"], a["name"], a["sort_order"], a["created_at"]) for a in snap["areas"]])
            c.executemany("INSERT INTO area_entities (area_id, entity_id) VALUES (?, ?)", snap["area_entities"])
            c.executemany("INSERT INTO area_ha_areas (area_id, ha_area_id) VALUES (?, ?)", snap["area_ha_areas"])
            c.executemany("INSERT INTO roles (id, name, created_at) VALUES (?, ?, ?)",
                          [(r["id"], r["name"], r["created_at"]) for r in snap["roles"]])
            c.executemany("INSERT INTO role_entities (role_id, entity_id, level) VALUES (?, ?, ?)",
                          [(*k, v) for k, v in snap["role_entities"].items()])
            c.executemany("INSERT INTO role_areas (role_id, area_id, level) VALUES (?, ?, ?)",
                          [(*k, v) for k, v in snap["role_areas"].items()])
            c.executemany(
                """INSERT INTO staff (id, name, pin_hash, status, created_at, last_seen,
                   connecteam_user_id, require_clock_in) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(m["id"], m["name"], m["pin_hash"], m["status"], m["created_at"], m["last_seen"],
                  m["connecteam_user_id"], m["require_clock_in"]) for m in snap["staff"]])
            c.executemany(
                """INSERT INTO devices (id, name, role_id, status, token_hash, pin_hash, signin_mode,
                   staff_all, layout_pages, created_at, last_seen, kind, pin_only)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(d["id"], d["name"], d["role_id"], d["status"], d["token_hash"], d["pin_hash"],
                  d["signin_mode"], d["staff_all"], d["layout_pages"], d["created_at"], d["last_seen"],
                  d["kind"], d["pin_only"]) for d in snap["devices"]])
            c.executemany("INSERT INTO staff_devices (staff_id, device_id) VALUES (?, ?)", snap["staff_devices"])
            c.executemany("INSERT INTO phone_orders (owner, data) VALUES (?, ?)", snap["phone_orders"].items())
            kept = set()
            for table, rows in (("devices", snap["devices"]), ("staff", snap["staff"])):
                for row in rows:
                    old = links[table].get(row["id"])
                    if table == "devices" and old and old["token_hash"] != row["token_hash"]:
                        continue   # not the same signed-in phone, so not its Home Assistant user
                    if old and old["name"] == row["name"] and old["ha_refresh_token"]:
                        kept.add(old["ha_refresh_token"])
                        c.execute(f"""UPDATE {table} SET ha_user = ?, ha_refresh_token = ?,
                                      ha_link_error = ? WHERE id = ?""",
                                  (old["ha_user"], old["ha_refresh_token"], old["ha_link_error"], row["id"]))
            for key, value in snap["settings"].items():
                if key in DEFAULT_SETTINGS:
                    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('layout', ?)",
                      (json.dumps(snap["layout"]),))
            c.commit()
        except Exception:
            c.rollback()
            raise
        return [r["ha_refresh_token"] for table in links.values() for r in table.values()
                if r["ha_refresh_token"] and r["ha_refresh_token"] not in kept]

    # Personal arrangements set on the phone

    def phone_order(self, owner: str) -> dict | None:
        row = self.conn.execute("SELECT data FROM phone_orders WHERE owner = ?", (owner,)).fetchone()
        try:
            return json.loads(row[0]) if row else None
        except ValueError:
            return None

    def set_phone_order(self, owner: str, order: dict | None) -> None:
        if order is None:
            self.conn.execute("DELETE FROM phone_orders WHERE owner = ?", (owner,))
        else:
            self.conn.execute("INSERT OR REPLACE INTO phone_orders (owner, data) VALUES (?, ?)",
                              (owner, json.dumps(order)))
        self.conn.commit()

    def phone_order_owners(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT owner FROM phone_orders")}

    # Activity log

    def log(self, actor, action, *, role=None, entity_id=None, detail=None, ok=True, ip=None):
        # Every field is capped, and the log is capped in rows, so nothing sent from a phone
        # can grow the database (and Home Assistant's disk) without limit.
        cap = lambda value, n: None if value is None else str(value)[:n]
        self.conn.execute(
            """INSERT INTO activity (ts, actor, role, entity_id, action, detail, ok, ip)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), cap(actor, 120), cap(role, 80), cap(entity_id, 255), cap(action, 120),
             cap(detail, 300), int(ok), cap(ip, 64)),
        )
        self.conn.execute(
            "DELETE FROM activity WHERE ts < ? OR id <= (SELECT MAX(id) FROM activity) - ?",
            (time.time() - ACTIVITY_KEEP_DAYS * 86400, ACTIVITY_MAX_ROWS),
        )
        self.conn.commit()

    def activity(self, limit: int = 200, before: float | None = None) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM activity WHERE ts < ? ORDER BY ts DESC LIMIT ?",
            (before or time.time() + 1, limit),
        )
        return [dict(r) for r in rows]
