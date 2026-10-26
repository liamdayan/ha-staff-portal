"""The HR portal's own SQLite database, DATA_DIR/hr/hr.db, kept apart from Staff Access's so a
Staff Access backup or restore never touches it, and a damaged HR database only affects HR.

Everyone is keyed by their Connecteam user ID (Staff Access's own staff numbers change on a
restore). One connection, used only on the event loop: never await inside a write. Other parts
of the HR portal add their tables with add_schema(). Text that must stay private is encrypted
(crypto.py) before it gets here; names, dates and statuses stay readable so lists can be sorted.
"""

import json
import os
import sqlite3
import time
import urllib.parse

DB_FILE = "hr.db"
BUSY_SECONDS = 5
WAL_LIMIT = 16 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Everyone in Connecteam, active or not (former employees keep their files).
CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY,                 -- Connecteam user ID
    name TEXT NOT NULL,
    mobile TEXT,                         -- encrypted
    email TEXT,                          -- encrypted
    signin_hash TEXT,                    -- keyed hash of the mobile number, to find them at sign-in
    signin_name TEXT,                    -- encrypted: a sign-in name HR set (no mobile number)
    signin_name_hash TEXT,
    groups TEXT NOT NULL DEFAULT '[]',   -- Connecteam smart group IDs (departments), JSON
    start_date TEXT,                     -- YYYY-MM-DD
    start_estimated INTEGER NOT NULL DEFAULT 0,   -- 1: the day they were added to Connecteam
    start_confirmed INTEGER NOT NULL DEFAULT 0,   -- 1: HR typed or confirmed it
    managers TEXT NOT NULL DEFAULT '[]', -- their direct manager(s) in Connecteam, user IDs, JSON
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at REAL,
    added_at REAL NOT NULL,
    synced_at REAL,
    kiosk_hash TEXT,                     -- slow salted hash of their kiosk code, never the code
    totp_secret TEXT,                    -- encrypted
    totp_on INTEGER NOT NULL DEFAULT 0,
    totp_step INTEGER NOT NULL DEFAULT 0,          -- each authenticator code works once
    totp_redo INTEGER NOT NULL DEFAULT 0,          -- 1: used a recovery code, should set up again
    failed INTEGER NOT NULL DEFAULT 0,             -- wrong tries in a row
    failed_at REAL,
    locked_until REAL,
    day_failed INTEGER NOT NULL DEFAULT 0,         -- wrong tries in the last day
    day_since REAL,
    hard_locked INTEGER NOT NULL DEFAULT 0,        -- 1: locked until an HR admin unlocks them
    access_ended INTEGER NOT NULL DEFAULT 0,       -- 1: HR ended access, or they were archived
    last_signin_at REAL,
    last_signin_ip TEXT,
    prev_signin_at REAL,
    prev_signin_ip TEXT
);
CREATE INDEX IF NOT EXISTS people_signin ON people (signin_hash);
CREATE INDEX IF NOT EXISTS people_signin_name ON people (signin_name_hash);
-- HR admins (given by the owner), directors and managers (given by HR admins).
CREATE TABLE IF NOT EXISTS roles (
    person_id TEXT NOT NULL,
    role TEXT NOT NULL,
    granted_by TEXT,
    granted_at REAL NOT NULL,
    PRIMARY KEY (person_id, role)
);
-- The departments (smart groups) HR assigned each manager, and which one they're primary for.
CREATE TABLE IF NOT EXISTS manager_departments (
    person_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (person_id, group_id)
);
CREATE TABLE IF NOT EXISTS recovery_codes (
    person_id TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    used_at REAL,
    PRIMARY KEY (person_id, code_hash)
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    person_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_used REAL NOT NULL,
    confirmed_at REAL,                   -- last time they typed their code or PIN
    ip TEXT,
    agent TEXT,
    via_recovery INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_person ON sessions (person_id);
-- One-time setup codes (QR codes and links) for setting up an authenticator.
CREATE TABLE IF NOT EXISTS setup_tokens (
    token_hash TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_by TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    pin_ok_at REAL,                      -- their PIN was typed right on this setup
    pending_secret TEXT                  -- encrypted: the authenticator being set up
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor_id TEXT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT,
    target TEXT,
    detail TEXT,
    ip TEXT,
    security INTEGER NOT NULL DEFAULT 0,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_security ON audit (security, id);
CREATE INDEX IF NOT EXISTS audit_target ON audit (target_id, id);
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    link TEXT,
    urgent INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    read_at REAL,
    summarized INTEGER NOT NULL DEFAULT 0,
    once TEXT                            -- the same event never makes a second entry
);
CREATE INDEX IF NOT EXISTS inbox_person ON inbox (person_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS inbox_once ON inbox (person_id, once) WHERE once IS NOT NULL;
-- Connecteam chat messages waiting to go (they survive restarts and updates).
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,
    text TEXT NOT NULL,
    urgent INTEGER NOT NULL DEFAULT 0,
    day TEXT NOT NULL,
    created_at REAL NOT NULL,
    due_at REAL NOT NULL,
    tries INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    sent_at REAL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox (status, due_at);
"""

# Columns added after a table was first made: {table: {column: SQL type}}.
MIGRATIONS: dict[str, dict[str, str]] = {}
INDEXES = ""

DEFAULTS = {
    # Who must use an authenticator code: everyone, staff (HR admins, directors and managers),
    # hr (HR admins only) or nobody.
    "totp_policy": "staff",
    "session_minutes": "15",              # signed out after this long without use (5 to 30)
    "time_zone": "America/Los_Angeles",   # every date and quiet hour is in this time zone
    "quiet_start": "21:00",               # employees' messages wait from here...
    "quiet_end": "07:00",                 # ...until here
    "summary_time": "08:00",              # managers' and HR's daily summary
    "start_field": "",                    # the Connecteam profile field holding the start date
    "manager_field": "",                  # ...and the direct manager ("" finds it by its name)
    "date_order": "mdy",                  # how Connecteam writes dates: mdy (US) or dmy
    "retention_years": "5",               # records of people who left are listed after this
}
PEOPLE_FIELDS = {"name", "mobile", "email", "signin_hash", "signin_name", "signin_name_hash",
                 "groups", "start_date", "start_estimated", "start_confirmed", "managers",
                 "archived", "archived_at", "synced_at", "kiosk_hash", "totp_secret", "totp_on",
                 "totp_step", "totp_redo", "failed", "failed_at", "locked_until", "day_failed",
                 "day_since", "hard_locked", "access_ended", "last_signin_at", "last_signin_ip",
                 "prev_signin_at", "prev_signin_ip"}
ROLES = ("hr_admin", "director", "manager")

current = None   # the open HRDatabase while the HR portal runs (service.py sets it)


class HRDatabase:
    def __init__(self, folder: str):
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, DB_FILE)
        self.conn = sqlite3.connect(self.path, timeout=BUSY_SECONDS)
        self.conn.row_factory = sqlite3.Row
        self._settings = None
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute(f"PRAGMA journal_size_limit = {WAL_LIMIT}")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for column, kind in columns.items():
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        self.conn.executescript(INDEXES)
        self.append_only("audit")
        for key, value in DEFAULTS.items():
            self.conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                              (key, value))
        self.conn.commit()

    def add_schema(self, sql: str) -> None:
        """Tables (and indexes) of another part of the HR portal: CREATE ... IF NOT EXISTS."""
        self.conn.executescript(sql)
        self.conn.commit()

    def append_only(self, table: str) -> None:
        """Rows of this table can be added but never changed or deleted (the audit log, and
        records once issued): corrections are new rows."""
        for event in ("UPDATE", "DELETE"):
            self.conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{event.lower()} BEFORE {event} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} rows are never changed'); END")
        self.conn.commit()

    def reader(self) -> sqlite3.Connection:
        """A read-only connection of its own, for reading a lot (checking the audit log, a
        migration file) in a worker thread: make it there, and close it when done."""
        conn = sqlite3.connect(f"file:{urllib.parse.quote(self.path)}?mode=ro", uri=True,
                               timeout=BUSY_SECONDS)
        conn.row_factory = sqlite3.Row
        return conn

    def close(self) -> None:
        try:
            self.conn.execute("PRAGMA optimize")
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self.conn.close()

    # ------------------------------------------------------------ settings

    def settings(self) -> dict:
        if self._settings is None:
            self._settings = {r["key"]: r["value"] for r in self.conn.execute(
                "SELECT key, value FROM settings")}
        return dict(self._settings)

    def setting(self, key: str, default: str = "") -> str:
        return self.settings().get(key, DEFAULTS.get(key, default))

    def setting_int(self, key: str, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(self.setting(key))))
        except ValueError:
            return max(low, min(high, int(DEFAULTS.get(key, low))))

    def set_settings(self, values: dict) -> None:
        self._settings = None
        for key, value in values.items():
            self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                              (key, str(value)))
        self.conn.commit()

    def setting_json(self, key: str, default=None):
        try:
            return json.loads(self.setting(key)) if self.setting(key) else default
        except ValueError:
            return default

    # ------------------------------------------------------------ people

    def person(self, person_id) -> dict | None:
        row = self.conn.execute("SELECT * FROM people WHERE id = ?", (str(person_id),)).fetchone()
        return dict(row) if row else None

    def people(self, archived: bool | None = False) -> list[dict]:
        where = "" if archived is None else f"WHERE archived = {1 if archived else 0}"
        return [dict(r) for r in self.conn.execute(
            f"SELECT * FROM people {where} ORDER BY name COLLATE NOCASE")]

    def update_person(self, person_id, **fields) -> None:
        bad = set(fields) - PEOPLE_FIELDS
        if bad:
            raise ValueError(f"unknown fields: {bad}")
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.conn.execute(f"UPDATE people SET {sets} WHERE id = ?",
                              (*fields.values(), str(person_id)))
            self.conn.commit()

    # ------------------------------------------------------------ roles

    def roles_of(self, person_id) -> set[str]:
        return {r[0] for r in self.conn.execute(
            "SELECT role FROM roles WHERE person_id = ?", (str(person_id),))}

    def all_roles(self) -> dict[str, set[str]]:
        found: dict[str, set[str]] = {}
        for pid, role in self.conn.execute("SELECT person_id, role FROM roles"):
            found.setdefault(pid, set()).add(role)
        return found

    def with_role(self, role: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT person_id FROM roles WHERE role = ? ORDER BY granted_at", (role,))]

    def set_role(self, person_id, role: str, on: bool, by: str | None = None) -> bool:
        """Give or take away a role. Returns whether anything changed."""
        if role not in ROLES:
            raise ValueError(role)
        if on:
            changed = self.conn.execute(
                "INSERT OR IGNORE INTO roles (person_id, role, granted_by, granted_at) "
                "VALUES (?, ?, ?, ?)", (str(person_id), role, by, time.time())).rowcount
        else:
            changed = self.conn.execute("DELETE FROM roles WHERE person_id = ? AND role = ?",
                                        (str(person_id), role)).rowcount
            if role == "manager":
                self.conn.execute("DELETE FROM manager_departments WHERE person_id = ?",
                                  (str(person_id),))
        self.conn.commit()
        return bool(changed)

    def manager_groups(self, person_id) -> dict[str, bool]:
        """{smart group ID: primary} for the departments HR assigned this manager."""
        return {g: bool(p) for g, p in self.conn.execute(
            "SELECT group_id, is_primary FROM manager_departments WHERE person_id = ?",
            (str(person_id),))}

    def all_manager_groups(self) -> dict[str, dict[str, bool]]:
        found: dict[str, dict[str, bool]] = {}
        for pid, g, p in self.conn.execute(
                "SELECT person_id, group_id, is_primary FROM manager_departments"):
            found.setdefault(pid, {})[g] = bool(p)
        return found

    def set_manager_groups(self, person_id, groups: dict[str, bool]) -> None:
        """Replace a manager's departments. A department has one primary manager: marking this
        one primary takes it from whoever had it."""
        pid = str(person_id)
        self.conn.execute("DELETE FROM manager_departments WHERE person_id = ?", (pid,))
        for group, primary in groups.items():
            if primary:
                self.conn.execute("UPDATE manager_departments SET is_primary = 0 WHERE group_id = ?",
                                  (group,))
            self.conn.execute("INSERT INTO manager_departments (person_id, group_id, is_primary) "
                              "VALUES (?, ?, ?)", (pid, group, 1 if primary else 0))
        self.conn.commit()
