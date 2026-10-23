"""SQLite storage for roles, phones, settings and the activity log."""

import hashlib
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
"""

# Columns added after the first release, applied to existing databases on startup.
MIGRATIONS = {
    "devices": {
        "ha_user": "TEXT",
        "ha_refresh_token": "TEXT",
        "ha_link_error": "TEXT",
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
}

ENROLL_TTL_DEFAULT = 72 * 3600
ACTIVITY_KEEP_DAYS = 180


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
        self.conn.commit()

    # Settings

    def settings(self) -> dict:
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM settings")}

    def update_settings(self, values: dict) -> None:
        for key, value in values.items():
            if key in DEFAULT_SETTINGS:
                self.conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value))
                )
        self.conn.commit()

    def setting_int(self, key: str) -> int:
        try:
            return int(self.settings().get(key, DEFAULT_SETTINGS[key]))
        except ValueError:
            return int(DEFAULT_SETTINGS[key])

    # Roles

    def roles(self) -> list[dict]:
        roles = [dict(r) for r in self.conn.execute("SELECT * FROM roles ORDER BY name")]
        for role in roles:
            role["entities"] = self.role_entities(role["id"])
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
        return role

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

    def update_role(self, role_id: int, name: str, entities: dict[str, str]) -> None:
        self.conn.execute("UPDATE roles SET name = ? WHERE id = ?", (name, role_id))
        self.conn.execute("DELETE FROM role_entities WHERE role_id = ?", (role_id,))
        self.conn.executemany(
            "INSERT INTO role_entities (role_id, entity_id, level) VALUES (?, ?, ?)",
            [(role_id, eid, lvl) for eid, lvl in entities.items() if lvl in ("view", "control")],
        )
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
        allowed = {"name", "role_id", "status", "pin_hash", "failed_pins",
                   "ha_user", "ha_refresh_token", "ha_link_error"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k} = ?" for k in sets)
        self.conn.execute(f"UPDATE devices SET {cols} WHERE id = ?", (*sets.values(), device_id))
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
        self.conn.commit()
        return dict(row), token

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
        self.conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        self.conn.commit()

    # Activity log

    def log(self, actor, action, *, role=None, entity_id=None, detail=None, ok=True, ip=None):
        self.conn.execute(
            """INSERT INTO activity (ts, actor, role, entity_id, action, detail, ok, ip)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), actor, role, entity_id, action, detail, int(ok), ip),
        )
        self.conn.execute(
            "DELETE FROM activity WHERE ts < ?", (time.time() - ACTIVITY_KEEP_DAYS * 86400,)
        )
        self.conn.commit()

    def activity(self, limit: int = 200, before: float | None = None) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM activity WHERE ts < ? ORDER BY ts DESC LIMIT ?",
            (before or time.time() + 1, limit),
        )
        return [dict(r) for r in rows]
