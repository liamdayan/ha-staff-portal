"""SQLite storage for roles, phones, settings, the activity log and wristband records."""

import collections
import hashlib
import json
import os
import secrets
import sqlite3
import time
import urllib.parse

from wristbands import permission_choice

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

-- One person's own changes to what their role gives them, used on role-based devices: a level
-- for a device, or for every device in an area. 'none' takes the device away (Hidden).
CREATE TABLE IF NOT EXISTS staff_entities (
    staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('none', 'view', 'control')),
    PRIMARY KEY (staff_id, entity_id)
);
CREATE TABLE IF NOT EXISTS staff_areas (
    staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    area_id INTEGER NOT NULL REFERENCES areas(id) ON DELETE CASCADE,
    level TEXT NOT NULL CHECK (level IN ('none', 'view', 'control')),
    PRIMARY KEY (staff_id, area_id)
);

-- Wristbands. None of these tables refer to people, devices or roles with foreign keys: a
-- restore deletes and re-adds those, and must never wipe the door list or the card records.
-- The doors TT Hotel lists for the property, plus the names and hiding chosen here.
CREATE TABLE IF NOT EXISTS wb_doors (
    lock_id INTEGER PRIMARY KEY,
    tt_name TEXT NOT NULL DEFAULT '',
    alias TEXT NOT NULL DEFAULT '',
    mac TEXT NOT NULL,
    lock_type INTEGER NOT NULL DEFAULT 1,
    building_number INTEGER,
    building_name TEXT NOT NULL DEFAULT '',
    floor_number INTEGER,
    floor_name TEXT NOT NULL DEFAULT '',
    battery INTEGER,
    has_gateway INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL DEFAULT '',       -- name shown to staff; '' = TT Hotel's alias or name
    hidden INTEGER NOT NULL DEFAULT 0,   -- left out of the staff app's door lists
    active INTEGER NOT NULL DEFAULT 1,   -- 0 once TT Hotel stops listing it (kept for old cards)
    synced_at REAL NOT NULL
);
-- Every card job: who asked for which cards, on which computer, and how it went.
CREATE TABLE IF NOT EXISTS wb_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    card_type TEXT NOT NULL,
    name TEXT NOT NULL,
    count INTEGER NOT NULL,
    records TEXT NOT NULL,               -- JSON list of what's written on each card
    summary TEXT NOT NULL,
    expires_at REAL NOT NULL,
    allow_lockout INTEGER NOT NULL DEFAULT 0,
    staff_id INTEGER,
    device_id INTEGER,
    actor TEXT NOT NULL,
    role TEXT,
    ip TEXT,
    encoder_url TEXT NOT NULL,
    status TEXT NOT NULL,                -- queued|running|needs_attention|done|cancelled
    message TEXT,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS wb_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    status TEXT NOT NULL,                -- pending|written|failed|skipped|cancelled
    card_number TEXT,
    error TEXT,
    written_at REAL,
    UNIQUE (job_id, seq)
);
CREATE INDEX IF NOT EXISTS wb_jobs_ts ON wb_jobs (ts DESC);
CREATE INDEX IF NOT EXISTS wb_cards_number ON wb_cards (card_number);
-- Locking a lost staff card out, door by door: a lost card notice tapped on the door, a
-- replacement card carrying its cancellation, or a remote lock-out through the door's gateway.
CREATE TABLE IF NOT EXISTS wb_lockouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER NOT NULL,            -- wb_cards.id of the lost (or replaced) card
    lock_id INTEGER NOT NULL,
    method TEXT NOT NULL,                -- remote|notice|replacement
    status TEXT NOT NULL,                -- pending|done|failed
    detail TEXT,
    tries INTEGER NOT NULL DEFAULT 0,
    at REAL NOT NULL,
    UNIQUE (card_id, lock_id, method)
);
CREATE INDEX IF NOT EXISTS wb_lockouts_card ON wb_lockouts (card_id);

-- Departments: which Connecteam smart group people come from, the role they get, and the key
-- card made for them from the Staff tab. No foreign keys, like the wristband tables: people and
-- roles are deleted and re-added by a restore. Departments are in backups.
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    smart_group_id TEXT,                 -- Connecteam smart group it follows (NULL = by hand only)
    role_id INTEGER,                     -- role given to people in it (NULL = leave roles alone)
    card_access TEXT,                    -- JSON list like a staff card's "access"; NULL = no key card
    card_days INTEGER NOT NULL DEFAULT 30,
    card_lockout INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
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
        # 'role': role-based, each person signs in and gets their own role's access and pages
        # (and their own overrides) instead of the device's. 'phone' and 'staff' are kiosk modes.
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
        # The card encoder at this computer's desk: the address of TT Hotel's SN Web Socket
        # helper on the desk PC (ws://192.168.1.50:8102), and which USB port it's on (NULL =
        # found automatically). NULL address and not encoder_auto = no encoder, so no Wristbands
        # on this device.
        "encoder_url": "TEXT",
        "encoder_port": "TEXT",
        # 1 = Automatic: no address typed, so the add-on finds SN Web Socket on the network
        # (discovery.py). A typed encoder_url always wins over it. 0 with no address = no encoder.
        "encoder_auto": "INTEGER NOT NULL DEFAULT 0",
        # Automatic: the address of the helper an admin confirmed for this computer (Use this
        # encoder). NULL = none confirmed yet, so none is used. Matched again after every search.
        "encoder_auto_ip": "TEXT",
        # How many digits the device's own PIN has, recorded when it's set (PINs are stored
        # scrambled, so it can't be worked out later). NULL = not known: the PIN pad then waits
        # for a pause instead of signing in as soon as the last digit is typed.
        "pin_length": "INTEGER",
        # 1 = This computer (USB): the encoder is plugged into this computer, and the staff app
        # open on it passes requests to SN Web Socket on 127.0.0.1 (encoder.LocalRelay), so the
        # add-on never connects to it. A typed encoder_url still wins; Automatic doesn't.
        "encoder_local": "INTEGER NOT NULL DEFAULT 0",
    },
    "staff": {
        # Too many wrong PINs locks a person out for a while (not permanently), so one
        # colleague can't lock everyone out. An admin can unlock sooner.
        "locked_until": "REAL",
        # Linked Connecteam user (their user ID there), and whether they may only sign in while
        # clocked in on Connecteam's time clock.
        "connecteam_user_id": "TEXT",
        "require_clock_in": "INTEGER NOT NULL DEFAULT 0",
        # Their role on role-based devices, and their own page choice there (JSON like a
        # phone's; NULL = their role's pages).
        "role_id": "INTEGER REFERENCES roles(id) ON DELETE SET NULL",
        "layout_pages": "TEXT",
        # Which wristbands and cards they may make, and for which doors (JSON, see
        # wristbands.py; NULL = the same as their role).
        "wristbands": "TEXT",
        # Their department (no foreign key: departments are replaced by a restore).
        "department_id": "INTEGER",
        # 1 = added by the Connecteam staff sync, which keeps them up to date. People added by
        # hand are never touched by it.
        "connecteam_auto": "INTEGER NOT NULL DEFAULT 0",
        # 1 once an admin changed require_clock_in (or their role) by hand: the sync leaves that
        # alone from then on.
        "clock_in_set": "INTEGER NOT NULL DEFAULT 0",
        "role_set": "INTEGER NOT NULL DEFAULT 0",
        # 1 = their PIN is their Connecteam kiosk code, so it follows when the code changes.
        # An admin typing a PIN sets it back to 0.
        "pin_from_connecteam": "INTEGER NOT NULL DEFAULT 0",
        # Why the sync turned them off ("No usable kiosk code in Connecteam"); NULL otherwise.
        # Only people the sync turned off are turned on again by it.
        "connecteam_note": "TEXT",
        # How many digits their PIN has, as for devices (NULL = not known).
        "pin_length": "INTEGER",
    },
    "wb_jobs": {
        # Who a staff card is for (made from the Staff tab, or matched by name), their department
        # and where it was made ('staff_app' or 'dashboard'). No foreign keys.
        "holder_staff_id": "INTEGER",
        "department_id": "INTEGER",
        "source": "TEXT",
        # 'write' makes cards; 'revoke' clears one particular card; 'notice' writes a lost card
        # notice. target_card(_id) is the card revoked, noticed or replaced; a write or notice
        # with target_card also carries its cancellation, valid until cancel_until.
        "kind": "TEXT NOT NULL DEFAULT 'write'",
        "target_card_id": "INTEGER",
        "target_card": "TEXT",
        "cancel_until": "REAL",
    },
    "wb_cards": {
        # A staff card's state: active, revoked (cleared, or replaced) or lost; when, and by whom.
        "state": "TEXT NOT NULL DEFAULT 'active'",
        "state_at": "REAL",
        "state_by": "TEXT",
        # The number of a lost or replaced card this card also carries a cancellation for.
        "cancelled_card": "TEXT",
    },
    "roles": {
        # Pages for people with this role on role-based devices (JSON like a phone's choice;
        # NULL = every page). Kiosk devices choose their own pages.
        "layout_pages": "TEXT",
        # Which wristbands and cards people with this role may make (JSON, see wristbands.py;
        # NULL = none).
        "wristbands": "TEXT",
    },
}

DEFAULT_SETTINGS = {
    "property_name": "Staff Portal",
    "app_name": "Staff",
    "accent_color": "#2f6f4f",
    "icon_background": "#ffffff",
    "public_url": "",
    "pin_timeout_minutes": "15",            # phones (the key is older than computers)
    "pin_timeout_computer_minutes": "5",    # computers: a desk is left unattended more often
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
    # Wristbands (TT Hotel key cards made on a desk's card encoder)
    "wristbands_enabled": "0",
    "tthotel_region": "global",      # "china" for accounts on TT Hotel's China servers
    "tthotel_client_id": "",
    "tthotel_client_secret": "",     # never sent to the browser or put in backups
    "tthotel_username": "",
    "tthotel_password_md5": "",      # as TT Hotel shows it (already MD5); never sent or backed up
    "tthotel_sync_hours": "6",
    "tthotel_token": "{}",           # TT Hotel sign-in, kept for its 90 days (internal)
    "tthotel_usage": "{}",           # requests made this month (internal: never shown or backed up)
    "wb_time_zone": "",              # blank: Home Assistant's time zone
    "wb_daypass_end": "20:00",       # day passes stop working at this time today
    "wb_daypass_doors": "[]",        # TT Hotel lock IDs a day pass opens
    "wb_daypass_max": "20",
    "wb_checkout_time": "11:00",     # overnight cards stop working at this time on checkout day
    "wb_overnight_doors": "[]",      # shared doors every overnight card opens, besides the room
    "wb_overnight_max": "20",
    "wb_staff_days": "90",           # how far ahead a staff card's end date may be
    "wb_staff_max": "5",
    "wb_lockout_daypass": "0",       # 1: the card also opens a door that's been deadbolted inside
    "wb_lockout_overnight": "0",
    "wb_lockout_staff": "1",
    "wb_sectors": "0000000000011111",  # card sectors TT Hotel uses (its default: 12 to 16)
    "wb_card_wait": "60",            # seconds to wait for a wristband on the encoder
    "wb_max_records": "20",          # most doors written onto one card
    "wb_scan_subnets": "",           # networks searched for Automatic encoders (blank: the ones
                                     # in Home Assistant's network setting)
    # Connecteam staff sync: add every active Connecteam user as a person, and whether people it
    # adds must be clocked in (each person's own setting wins once an admin changes it).
    "connecteam_auto_staff": "0",
    "connecteam_auto_clock_in": "1",
    # Remote lock-out of lost cards through door gateways (experimental, off until tested):
    # which TT Hotel call enrolls the card number, and when the owner confirmed it works.
    "wb_remote_lockout": "0",
    "wb_remote_lockout_api": "reversed",
    "wb_remote_lockout_tested": "",  # set only by the test helper, never from a form or backup
}

# Settings that never leave the add-on: not shown in the dashboard (only whether they're set),
# not put in backups, and not taken from one. Only a migration file carries them, encrypted
# (migrate.py), to another install of the add-on.
PRIVATE_SETTINGS = ("connecteam_client_secret", "connecteam_usage", "tthotel_client_secret",
                    "tthotel_password_md5", "tthotel_token", "tthotel_usage")

ENROLL_TTL_DEFAULT = 72 * 3600
ACTIVITY_KEEP_DAYS = 180
ACTIVITY_MAX_ROWS = 50_000
WB_JOBS_KEEP_DAYS = 2 * 365    # card records outlive the activity log: wristbands can be asked
WB_JOBS_MAX_ROWS = 100_000     # about long after they were made
WB_ACTIVE = ("queued", "running", "needs_attention")   # card jobs that haven't finished
# The same as SQL, spelled out: SQLite only uses the partial index below for a query that spells
# out the same statuses (not one that passes them as parameters).
UNFINISHED = f"status IN ({', '.join(repr(status) for status in WB_ACTIVE)})"
WB_JOB_STATUSES = (*WB_ACTIVE, "done", "cancelled")
WB_CARD_STATUSES = ("pending", "written", "failed", "skipped", "cancelled")
WB_JOB_FIELDS = {"status", "message", "finished_at"}

# Indexes on columns MIGRATIONS may have added, so they're made after it.
INDEXES = f"""
-- Each encoder's unfinished job: looked for whenever a job starts.
CREATE INDEX IF NOT EXISTS wb_jobs_unfinished ON wb_jobs (encoder_url) WHERE {UNFINISHED};
-- One person's card log.
CREATE INDEX IF NOT EXISTS wb_jobs_staff ON wb_jobs (staff_id, ts);
-- Staff key cards: the Key cards list, and the count of each person's (on every dashboard load).
CREATE INDEX IF NOT EXISTS wb_jobs_key_cards ON wb_jobs (card_type, expires_at)
    WHERE kind = 'write';
"""
DB_FILE = "staff_portal.db"
CARD_NUMBER_DIGITS = 6         # a card log search this many digits or longer is a card number
BUSY_SECONDS = 5               # how long a write waits for another connection's (a backup's)
WAL_LIMIT = 16 * 1024 * 1024   # the write-ahead log is cut back to this after a checkpoint
WB_CARD_FIELDS = {"status", "card_number", "error", "written_at", "state", "state_at", "state_by",
                  "cancelled_card"}
WB_JOB_KINDS = ("write", "revoke", "notice")
CARD_STATES = ("active", "revoked", "lost")
LOCKOUT_METHODS = ("remote", "notice", "replacement")
LOCKOUT_STATUSES = ("pending", "done", "failed")
KEY_CARD_FILTERS = ("active", "expired", "revoked", "lost", "all")
STAFF_FIELDS = {"name", "pin_hash", "status", "failed_pins", "last_seen", "locked_until",
                "ha_user", "ha_refresh_token", "ha_link_error", "connecteam_user_id",
                "require_clock_in", "role_id", "layout_pages", "wristbands", "department_id",
                "connecteam_auto", "clock_in_set", "role_set", "pin_from_connecteam",
                "connecteam_note", "pin_length"}
DEPARTMENT_FIELDS = {"name", "smart_group_id", "role_id", "card_access", "card_days",
                     "card_lockout"}

# One row of each log table as backups carry it: a section's "columns" are these fields, in this
# order. Tuples rather than dicts because a full log is tens of thousands of rows, and a backup
# being restored is held in memory whole.
ActivityRow = collections.namedtuple(
    "ActivityRow", "ts actor role entity_id action detail ok ip")
CardJobRow = collections.namedtuple(
    "CardJobRow", "id ts card_type name count records summary expires_at allow_lockout staff_id "
                  "device_id actor role ip encoder_url status message finished_at holder_staff_id "
                  "department_id source kind target_card_id target_card cancel_until")
CardRow = collections.namedtuple(
    "CardRow", "id job_id seq status card_number error written_at state state_at state_by "
               "cancelled_card")
LockoutRow = collections.namedtuple("LockoutRow", "card_id lock_id method status detail tries at")
# What a migration brings besides a backup's contents (see migrate.py): every column of a door.
DOOR_COLUMNS = ("lock_id", "tt_name", "alias", "mac", "lock_type", "building_number",
                "building_name", "floor_number", "floor_name", "battery", "has_gateway", "name",
                "hidden", "active", "synced_at")
# What makes an install "set up", for Migrate (which only restores onto a fresh install): a
# fresh one has none of these, only default settings and perhaps a few activity log lines.
SET_UP_TABLES = ("staff", "devices", "roles", "areas", "departments", "wb_jobs")


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


def pin_length(value) -> int | None:
    """A stored PIN length: 4 to 8, or None when it isn't known (or is damaged), which only
    means the PIN pad waits for a pause before signing in."""
    if isinstance(value, bool) or not isinstance(value, int) or not 4 <= value <= 8:
        return None
    return value


def card_access_choice(raw) -> list[dict] | None:
    """A department's stored key card template: None when there is none, otherwise the list of
    access entries ({"level": "all"}, {"level": "door", "door": lock ID}, ...). Damaged means an
    empty list, which makes no card, never a wider one; wristbands.plan checks every entry again
    whenever a card is made."""
    if raw is None or raw == "":
        return None
    try:
        value = raw if isinstance(raw, list) else json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return []
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def page_choice(raw) -> list[str] | None:
    """A stored page choice (a phone's, a role's or a person's): None when nothing is stored,
    otherwise page IDs and page groups ("group:<id>"). A damaged value means no pages rather
    than every page, so it can never widen what anyone sees. Takes the stored JSON text or an
    already-read list."""
    if raw is None or raw == "":
        return None
    try:
        value = raw if isinstance(raw, list) else json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(i) for i in value] if isinstance(value, list) else []


class Database:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, DB_FILE)
        self.conn = sqlite3.connect(self.path, timeout=BUSY_SECONDS)
        self.conn.row_factory = sqlite3.Row
        self.read_only = False
        self._settings = None   # settings(), read once and kept until they change
        # A write-ahead log: a commit is added to staff_portal.db-wal rather than rewriting the
        # file, and with synchronous=NORMAL it doesn't wait for the disk (only checkpoints do).
        # Waiting four times on every change froze the whole add-on for tens of milliseconds a
        # time on an SD card or eMMC. A power cut can lose the last few changes, never the file.
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
        for key, value in DEFAULT_SETTINGS.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )
        # Phones signed in before device_tokens existed (or set up by an older version).
        self.conn.execute(
            """INSERT OR IGNORE INTO device_tokens (token_hash, device_id, issued_at)
               SELECT token_hash, id, ? FROM devices WHERE token_hash IS NOT NULL""", (time.time(),))
        self.conn.commit()
        # Statistics for choosing indexes (a quick sample of each table, not a full read): all of
        # them the first time, then for tables much changed since (all tables without them too,
        # from SQLite 3.46).
        self.conn.execute("PRAGMA analysis_limit = 1000")
        analyzed = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'").fetchone()
        self.conn.execute("PRAGMA optimize = 0x10002" if analyzed else "ANALYZE")
        self.conn.commit()

    def close(self) -> None:
        """On the way out: the write-ahead log goes back into the file, so /data holds one
        up-to-date file while the add-on is stopped."""
        try:
            if not self.read_only:
                self.conn.execute("PRAGMA optimize")
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self.conn.close()

    def reader(self) -> "Database":
        """This database, read-only, on a connection of its own: for reading a lot at once (a
        backup) in a worker thread, made and used there, while the add-on carries on. Everything
        read through it is one moment's (the write-ahead log keeps that moment while the add-on
        writes). close() it when done."""
        other = object.__new__(Database)
        other.path = self.path
        other.conn = sqlite3.connect(f"file:{urllib.parse.quote(self.path)}?mode=ro", uri=True,
                                     timeout=BUSY_SECONDS)
        other.conn.row_factory = sqlite3.Row
        other.read_only = True
        other._settings = None
        other.conn.execute("BEGIN")   # one moment: from the first read to close()
        return other

    # Settings

    def settings(self) -> dict:
        # Read on nearly every request, several times: kept, and read again after a change.
        # A copy each time, since callers add to it.
        if self._settings is None:
            self._settings = {r["key"]: r["value"] for r in self.conn.execute(
                "SELECT key, value FROM settings WHERE key != 'layout'")}
        return dict(self._settings)

    def update_settings(self, values: dict) -> None:
        self._settings = None
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
        """Whether another role, area, department or staff member already has this name, ignoring capitals
        ("Bob" and "bob" would be confusing on a phone, and import and restore match names
        that way)."""
        assert table in ("roles", "areas", "staff", "departments")
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
            role["layout_pages"] = page_choice(role["layout_pages"])
            role["wristbands"] = permission_choice(role["wristbands"])
            # Devices that use the role themselves: role-based devices take each person's.
            role["device_count"] = self.conn.execute(
                "SELECT COUNT(*) FROM devices WHERE role_id = ? AND status != 'revoked' AND signin_mode != 'role'",
                (role["id"],),
            ).fetchone()[0]
            role["staff_count"] = self.conn.execute(
                "SELECT COUNT(*) FROM staff WHERE role_id = ? AND status != 'disabled'", (role["id"],),
            ).fetchone()[0]
        return roles

    def role(self, role_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
        if not row:
            return None
        role = dict(row)
        role["entities"] = self.role_entities(role_id)
        role["areas"] = self.role_areas(role_id)
        role["layout_pages"] = page_choice(role["layout_pages"])
        role["wristbands"] = permission_choice(role["wristbands"])
        return role

    def set_role_pages(self, role_id: int, pages: list[str] | None) -> None:
        self.conn.execute("UPDATE roles SET layout_pages = ? WHERE id = ?",
                          (None if pages is None else json.dumps(pages), role_id))
        self.conn.commit()

    def set_role_wristbands(self, role_id: int, permissions: dict | None) -> None:
        """A role's wristband permissions, already checked (wristbands.clean_permissions)."""
        self.conn.execute("UPDATE roles SET wristbands = ? WHERE id = ?",
                          (None if permissions is None else json.dumps(permissions), role_id))
        self.conn.commit()

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
        """A new role with the same device, area, page and wristband settings. Phones and people
        stay on the original."""
        new_id = self.create_role(name)
        self.update_role(new_id, name, self.role_entities(role_id), self.role_areas(role_id))
        self.set_role_pages(new_id, self.role(role_id)["layout_pages"])
        # Copied as stored, so a damaged value stays damaged (no wristbands) in the copy too.
        self.conn.execute("UPDATE roles SET wristbands = (SELECT wristbands FROM roles WHERE id = ?) "
                          "WHERE id = ?", (role_id, new_id))
        self.conn.commit()
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

    # Departments (people's teams: their Connecteam smart group, role and key card template)

    def departments(self) -> list[dict]:
        """Every department, in order (a person in several mapped smart groups gets the first),
        with card_access as a list (None: no key card template; damaged: an empty list, which
        makes no card) and how many people are in each."""
        found = [dict(r) for r in self.conn.execute(
            "SELECT * FROM departments ORDER BY sort_order, id")]
        counts = dict(self.conn.execute(
            "SELECT department_id, COUNT(*) FROM staff WHERE department_id IS NOT NULL "
            "GROUP BY department_id").fetchall())
        for dept in found:
            dept["card_access"] = card_access_choice(dept["card_access"])
            dept["staff_count"] = counts.get(dept["id"], 0)
        return found

    def department(self, department_id) -> dict | None:
        return next((d for d in self.departments() if d["id"] == department_id), None)

    def create_department(self, name: str, **fields) -> int:
        next_order = self.conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM departments").fetchone()[0]
        cur = self.conn.execute(
            "INSERT INTO departments (name, sort_order, created_at) VALUES (?, ?, ?)",
            (name, next_order, time.time()))
        self.conn.commit()
        self.update_department(cur.lastrowid, **fields)
        return cur.lastrowid

    def update_department(self, department_id: int, **fields) -> None:
        """Change a department; card_access is a list (stored as JSON) or None."""
        sets = {k: v for k, v in fields.items() if k in DEPARTMENT_FIELDS}
        if "card_access" in sets and sets["card_access"] is not None:
            sets["card_access"] = json.dumps(sets["card_access"])
        if not sets:
            return
        cols = ", ".join(f"{k} = ?" for k in sets)
        self.conn.execute(f"UPDATE departments SET {cols} WHERE id = ?",
                          (*sets.values(), department_id))
        self.conn.commit()

    def reorder_departments(self, department_ids: list[int]) -> None:
        self.conn.executemany("UPDATE departments SET sort_order = ? WHERE id = ?",
                              [(i, did) for i, did in enumerate(department_ids, 1)])
        self.conn.commit()

    def delete_department(self, department_id: int) -> None:
        """Delete a department. Its people stay, with no department."""
        self.conn.execute("UPDATE staff SET department_id = NULL WHERE department_id = ?",
                          (department_id,))
        self.conn.execute("DELETE FROM departments WHERE id = ?", (department_id,))
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
            """INSERT INTO devices (name, role_id, pin_hash, pin_length, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (name, role_id, hash_pin(pin) if pin else None, len(pin) if pin else None,
             time.time()),
        )
        self.conn.commit()
        self.new_enroll_code(cur.lastrowid)
        return cur.lastrowid

    def update_device(self, device_id: int, **fields) -> None:
        allowed = {"name", "role_id", "status", "pin_hash", "failed_pins", "kind",
                   "ha_user", "ha_refresh_token", "ha_link_error", "signin_mode", "staff_all",
                   "layout_pages", "failed_at", "pin_only", "failed_day_count", "failed_day_since",
                   "encoder_url", "encoder_port", "encoder_auto", "encoder_auto_ip", "pin_length",
                   "encoder_local"}
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
        for r in self.conn.execute(
                """SELECT s.*, r.name AS role_name, dp.name AS department_name FROM staff s
                   LEFT JOIN roles r ON r.id = s.role_id
                   LEFT JOIN departments dp ON dp.id = s.department_id
                   ORDER BY s.name COLLATE NOCASE"""):
            m = dict(r)
            m.pop("pin_hash")
            m.pop("ha_refresh_token")
            m["layout_pages"] = page_choice(m["layout_pages"])
            m["wristbands"] = permission_choice(m["wristbands"])
            m["entities"], m["areas"] = self.staff_overrides(m["id"])
            result.append(m)
        return result

    def staff_overrides(self, staff_id: int) -> tuple[dict[str, str], dict[int, str]]:
        """One person's own changes to their role: ({entity_id: level}, {area_id: level})."""
        entities = {r["entity_id"]: r["level"] for r in self.conn.execute(
            "SELECT entity_id, level FROM staff_entities WHERE staff_id = ?", (staff_id,))}
        areas = {r["area_id"]: r["level"] for r in self.conn.execute(
            "SELECT area_id, level FROM staff_areas WHERE staff_id = ?", (staff_id,))}
        return entities, areas

    def set_staff_overrides(self, staff_id: int, entities: dict[str, str], areas: dict[int, str]) -> None:
        self.conn.execute("DELETE FROM staff_entities WHERE staff_id = ?", (staff_id,))
        self.conn.execute("DELETE FROM staff_areas WHERE staff_id = ?", (staff_id,))
        self.conn.executemany("INSERT INTO staff_entities (staff_id, entity_id, level) VALUES (?, ?, ?)",
                              [(staff_id, eid, lvl) for eid, lvl in entities.items()])
        self.conn.executemany("INSERT INTO staff_areas (staff_id, area_id, level) VALUES (?, ?, ?)",
                              [(staff_id, aid, lvl) for aid, lvl in areas.items()])
        self.conn.commit()

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

    def staff_pin_lengths(self, device: dict) -> list[int | None]:
        """The PIN length of each active person allowed on a shared device (None: not known)."""
        ids = {m["id"] for m in self.staff_for_device(device)}
        return [pin_length(r["pin_length"]) for r in self.conn.execute(
            "SELECT id, pin_length FROM staff") if r["id"] in ids]

    def staff_allowed_on(self, staff_id: int, device: dict) -> bool:
        return any(m["id"] == staff_id for m in self.staff_for_device(device))

    def create_staff(self, name: str, pin: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO staff (name, pin_hash, pin_length, created_at) VALUES (?, ?, ?, ?)",
            (name, hash_pin(pin), len(pin), time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def staff_by_connecteam(self, user_id: str) -> dict | None:
        row = self.conn.execute("SELECT id, name FROM staff WHERE connecteam_user_id = ?",
                                (str(user_id),)).fetchone()
        return dict(row) if row else None

    def update_staff(self, staff_id: int, **fields) -> None:
        sets = {k: v for k, v in fields.items() if k in STAFF_FIELDS}
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

    def setup_found(self) -> dict[str, int]:
        """What has been set up here, as {table: how many} (layout pages as "pages"), leaving out
        what there's none of. Empty means a fresh install."""
        found = {table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 for table in SET_UP_TABLES}
        found["pages"] = len(self.layout().get("pages") or [])
        return {k: v for k, v in found.items() if v}

    def replace_everything(self, snap: dict) -> tuple[list[str], dict[str, int]]:
        """Swap in a validated backup (from transfer.clean_backup) in one transaction: either all
        of it lands or none of it does. The door list is kept, and so are the activity log and
        the card records, with the backup's own added to them (departments come from the backup
        when it has them). Home Assistant links are kept for phones and staff that still exist
        with the same ID and name.

        A migration (migrate.clean_snapshot, which adds "migrate") also brings the device tokens,
        secrets, door list, Home Assistant links and unexpired setup codes of the install it came
        from.

        Returns the Home Assistant refresh tokens of links that didn't carry over, so the caller
        can revoke them, and how many log rows came from the backup ({"activity", "wb_jobs",
        "wb_cards"}; empty for a backup from before logs were in backups)."""
        c = self.conn
        migrate = snap.get("migrate")
        links = {
            table: {r["id"]: r for r in c.execute(
                f"SELECT id, name, ha_user, ha_refresh_token, ha_link_error"
                f"{', token_hash' if table == 'devices' else ''} FROM {table}")}
            for table in ("devices", "staff")
        }
        role_names = {r[0]: r[1] for r in c.execute("SELECT id, name FROM roles")}
        try:
            for table in ("phone_orders", "staff_devices", "staff_entities", "staff_areas",
                          "role_areas", "role_entities", "area_entities", "area_ha_areas",
                          "devices", "staff", "roles", "areas"):
                c.execute(f"DELETE FROM {table}")
            c.executemany("INSERT INTO areas (id, name, sort_order, created_at) VALUES (?, ?, ?, ?)",
                          [(a["id"], a["name"], a["sort_order"], a["created_at"]) for a in snap["areas"]])
            c.executemany("INSERT INTO area_entities (area_id, entity_id) VALUES (?, ?)", snap["area_entities"])
            c.executemany("INSERT INTO area_ha_areas (area_id, ha_area_id) VALUES (?, ?)", snap["area_ha_areas"])
            c.executemany("INSERT INTO roles (id, name, created_at, layout_pages, wristbands) "
                          "VALUES (?, ?, ?, ?, ?)",
                          [(r["id"], r["name"], r["created_at"], r["layout_pages"], r["wristbands"])
                           for r in snap["roles"]])
            c.executemany("INSERT INTO role_entities (role_id, entity_id, level) VALUES (?, ?, ?)",
                          [(*k, v) for k, v in snap["role_entities"].items()])
            c.executemany("INSERT INTO role_areas (role_id, area_id, level) VALUES (?, ?, ?)",
                          [(*k, v) for k, v in snap["role_areas"].items()])
            # Departments travel in backups. A backup from before departments has none in it,
            # and leaves the ones here as they are, each keeping its role by name: the backup's
            # role IDs may belong to other roles (none of that name: no role).
            if snap.get("departments") is not None:
                c.execute("DELETE FROM departments")
                c.executemany(
                    """INSERT INTO departments (id, name, sort_order, smart_group_id, role_id,
                       card_access, card_days, card_lockout, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(d["id"], d["name"], d["sort_order"], d["smart_group_id"], d["role_id"],
                      d["card_access"], d["card_days"], d["card_lockout"], d["created_at"])
                     for d in snap["departments"]])
            else:
                by_name = {r["name"].casefold(): r["id"] for r in snap["roles"]}
                c.executemany("UPDATE departments SET role_id = ? WHERE id = ?", [
                    (by_name.get(str(role_names.get(role_id, "")).casefold()), dept_id)
                    for dept_id, role_id in c.execute(
                        "SELECT id, role_id FROM departments WHERE role_id IS NOT NULL"
                    ).fetchall()])
            c.executemany(
                """INSERT INTO staff (id, name, pin_hash, status, created_at, last_seen,
                   connecteam_user_id, require_clock_in, role_id, layout_pages, wristbands,
                   department_id, connecteam_auto, clock_in_set, role_set, pin_from_connecteam,
                   connecteam_note, pin_length)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(m["id"], m["name"], m["pin_hash"], m["status"], m["created_at"], m["last_seen"],
                  m["connecteam_user_id"], m["require_clock_in"], m["role_id"], m["layout_pages"],
                  m["wristbands"], m.get("department_id"), m.get("connecteam_auto", 0),
                  m.get("clock_in_set", 0), m.get("role_set", 0), m.get("pin_from_connecteam", 0),
                  m.get("connecteam_note"), m.get("pin_length")) for m in snap["staff"]])
            c.execute("UPDATE staff SET department_id = NULL WHERE department_id NOT IN "
                      "(SELECT id FROM departments)")
            c.executemany("INSERT INTO staff_entities (staff_id, entity_id, level) VALUES (?, ?, ?)",
                          [(*k, v) for k, v in snap["staff_entities"].items()])
            c.executemany("INSERT INTO staff_areas (staff_id, area_id, level) VALUES (?, ?, ?)",
                          [(*k, v) for k, v in snap["staff_areas"].items()])
            c.executemany(
                """INSERT INTO devices (id, name, role_id, status, token_hash, pin_hash, signin_mode,
                   staff_all, layout_pages, created_at, last_seen, kind, pin_only, encoder_url,
                   encoder_port, encoder_auto, encoder_auto_ip, pin_length, encoder_local)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(d["id"], d["name"], d["role_id"], d["status"], d["token_hash"], d["pin_hash"],
                  d["signin_mode"], d["staff_all"], d["layout_pages"], d["created_at"], d["last_seen"],
                  d["kind"], d["pin_only"], d["encoder_url"], d["encoder_port"], d["encoder_auto"],
                  d["encoder_auto_ip"], d.get("pin_length"), d.get("encoder_local", 0))
                 for d in snap["devices"]])
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
            if migrate:
                self._apply_migration(migrate, kept)
            # Card jobs stay with the people who made them, by the same rule: a job whose maker
            # isn't here any more under the same ID and name belongs to no one (people who may
            # see every card still see it), so it can't turn up in someone else's card log.
            same = {r["id"] for r in snap["staff"]
                    if (old := links["staff"].get(r["id"])) and old["name"] == r["name"]}
            makers = c.execute("SELECT DISTINCT staff_id FROM wb_jobs WHERE staff_id IS NOT NULL")
            c.executemany("UPDATE wb_jobs SET staff_id = NULL WHERE staff_id = ?",
                          [(r[0],) for r in makers.fetchall() if r[0] not in same])
            # The same for whom a key card is for (the card's name stays on the record).
            holders = c.execute(
                "SELECT DISTINCT holder_staff_id FROM wb_jobs WHERE holder_staff_id IS NOT NULL")
            c.executemany("UPDATE wb_jobs SET holder_staff_id = NULL WHERE holder_staff_id = ?",
                          [(r[0],) for r in holders.fetchall() if r[0] not in same])
            # Door names and hiding chosen here, for doors this system already knows (the door
            # list itself comes from TT Hotel, and isn't replaced).
            c.executemany("UPDATE wb_doors SET name = ?, hidden = ? WHERE lock_id = ?",
                          [(d["name"], d["hidden"], d["lock_id"]) for d in snap["wristband_doors"]])
            for key, value in snap["settings"].items():
                if key in DEFAULT_SETTINGS:
                    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('layout', ?)",
                      (json.dumps(snap["layout"]),))
            merged = self._merge_logs(snap["logs"]) if snap.get("logs") else {}
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            self._settings = None   # read again, whichever way it went
        return [r["ha_refresh_token"] for table in links.values() for r in table.values()
                if r["ha_refresh_token"] and r["ha_refresh_token"] not in kept], merged

    def _apply_migration(self, extra: dict, kept: set[str]) -> None:
        """The parts of a migration a backup never carries (from migrate.clean_snapshot), without
        committing. Only ever used on a fresh install, so there is nothing here to keep."""
        c = self.conn
        c.executemany("INSERT OR REPLACE INTO device_tokens (token_hash, device_id, issued_at) "
                      "VALUES (?, ?, ?)", extra["device_tokens"])
        for key, value in extra["secrets"].items():
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        if extra["doors"] is not None:
            c.execute("DELETE FROM wb_doors")
            c.executemany(f"INSERT INTO wb_doors ({', '.join(DOOR_COLUMNS)}) "
                          f"VALUES ({', '.join('?' * len(DOOR_COLUMNS))})",
                          [tuple(d[k] for k in DOOR_COLUMNS) for d in extra["doors"]])
        for table in ("devices", "staff"):
            for row_id, (user, token, error) in extra["links"][table].items():
                kept.add(token)
                c.execute(f"UPDATE {table} SET ha_user = ?, ha_refresh_token = ?, ha_link_error = ? "
                          "WHERE id = ?", (user, token, error, row_id))
        c.executemany("UPDATE devices SET enroll_code = ?, enroll_expires = ? "
                      "WHERE id = ? AND status = 'pending'",
                      [(code, expires, device_id)
                       for device_id, (code, expires) in extra["setup_codes"].items()])

    # Logs from a backup: added to the ones here, never replacing them

    def _merge_logs(self, logs: dict) -> dict[str, int]:
        """Add a backup's activity log and card records (transfer.clean_logs) to the ones here,
        without committing. Rows already here are skipped and nothing here is removed; then the
        usual limits apply. Card jobs from the backup get new IDs here, and their cards (and
        those cards' lock-outs) follow them. Returns how many rows were added and kept (when a
        log is full, older rows from the backup are trimmed straight away, so they don't count,
        and restoring the same backup again never counts them again)."""
        c = self.conn
        now = time.time()
        # Activity: the same moment, person, action, detail and device is the same entry. Rows
        # the log would drop straight away (too old) aren't added at all.
        cutoff = now - ACTIVITY_KEEP_DAYS * 86400
        seen = {tuple(r) for r in c.execute(
            "SELECT ts, actor, action, detail, entity_id FROM activity")}
        added = []
        for row in logs["activity"]:
            key = (row.ts, row.actor, row.action, row.detail, row.entity_id)
            if row.ts >= cutoff and key not in seen:
                seen.add(key)
                added.append(row)
        activity_added = 0
        if added:
            c.executemany(f"INSERT INTO activity ({', '.join(ActivityRow._fields)}) "
                          f"VALUES ({', '.join('?' * len(ActivityRow._fields))})", added)
            self._renumber_activity(now)
            seen.clear()
            kept = {tuple(r) for r in c.execute(
                "SELECT ts, actor, action, detail, entity_id FROM activity")}
            activity_added = sum((row.ts, row.actor, row.action, row.detail, row.entity_id) in kept
                                 for row in added)

        # Card jobs: the same moment, kind, card type, name, count and maker is the same job.
        # One that's here already keeps its own cards and progress (it may have moved on since
        # the backup was made); the backup's lock-outs for its cards are still added.
        staff = {r[0] for r in c.execute("SELECT id FROM staff")}
        devices = {r[0] for r in c.execute("SELECT id FROM devices")}
        departments = {r[0] for r in c.execute("SELECT id FROM departments")}
        jobs_here = {tuple(r)[1:]: r[0] for r in c.execute(
            "SELECT id, ts, kind, card_type, name, count, actor FROM wb_jobs")}
        cards_here = {(r[0], r[1]): r[2] for r in c.execute("SELECT job_id, seq, id FROM wb_cards")}
        job_cutoff = now - WB_JOBS_KEEP_DAYS * 86400
        job_ids, new_jobs, targets = {}, set(), []
        columns = CardJobRow._fields[1:]   # everything but the ID, which is new here
        for job in sorted(logs["wb_jobs"], key=lambda j: (j.ts, j.id)):
            key = (job.ts, job.kind, job.card_type, job.name, job.count, job.actor)
            if key in jobs_here:
                job_ids[job.id] = jobs_here[key]
                continue
            if job.ts < job_cutoff:
                continue
            target = job.target_card_id
            # People, devices and departments are the backup's own, now restored here; any it
            # no longer had belong to no one (so a job never lands in someone else's card log).
            job = job._replace(
                staff_id=job.staff_id if job.staff_id in staff else None,
                holder_staff_id=job.holder_staff_id if job.holder_staff_id in staff else None,
                device_id=job.device_id if job.device_id in devices else None,
                department_id=job.department_id if job.department_id in departments else None,
                target_card_id=None)
            new_id = c.execute(f"INSERT INTO wb_jobs ({', '.join(columns)}) "
                               f"VALUES ({', '.join('?' * len(columns))})", job[1:]).lastrowid
            jobs_here[key] = job_ids[job.id] = new_id
            new_jobs.add(new_id)
            if target is not None:
                targets.append((new_id, target))
        card_ids, cards_added = {}, collections.Counter()   # cards added, by their new job
        columns = CardRow._fields[1:]
        for card in sorted(logs["wb_cards"], key=lambda r: r.id):
            job_id = job_ids.get(card.job_id)
            if job_id is None:
                continue   # its job was left out
            here = cards_here.get((job_id, card.seq))
            if here is not None:
                card_ids[card.id] = here
                continue
            if job_id not in new_jobs:
                continue   # a job that was here already keeps its own cards
            cards_here[(job_id, card.seq)] = card_ids[card.id] = c.execute(
                f"INSERT INTO wb_cards ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' * len(columns))})", (job_id, *card[2:])).lastrowid
            cards_added[job_id] += 1
        # A revoke, notice or replacement points at the card it was for, by that card's new ID.
        c.executemany("UPDATE wb_jobs SET target_card_id = ? WHERE id = ?",
                      [(card_ids.get(old), job_id) for job_id, old in targets])
        c.executemany(
            f"INSERT OR IGNORE INTO wb_lockouts ({', '.join(LockoutRow._fields)}) "
            f"VALUES ({', '.join('?' * len(LockoutRow._fields))})",
            [(card_ids[lo.card_id], *lo[1:]) for lo in logs["wb_lockouts"]
             if lo.card_id in card_ids])
        self._prune_wb_jobs(now)
        new_jobs &= {r[0] for r in c.execute("SELECT id FROM wb_jobs")}   # those the limit kept
        return {"activity": activity_added, "wb_jobs": len(new_jobs),
                "wb_cards": sum(cards_added[job_id] for job_id in new_jobs)}

    def _renumber_activity(self, now: float) -> None:
        """Rewrite the activity log oldest first, within its limits, without committing. Rows
        added from a backup get the newest IDs whatever their time, and log() trims by ID (it
        runs on every action, so it has to be quick); after this, ID order is time order."""
        c = self.conn
        columns = ", ".join(ActivityRow._fields)
        keep = c.execute(f"SELECT {columns} FROM activity WHERE ts >= ? ORDER BY ts DESC, id DESC "
                         "LIMIT ?", (now - ACTIVITY_KEEP_DAYS * 86400, ACTIVITY_MAX_ROWS)).fetchall()
        c.execute("DELETE FROM activity")
        c.executemany(f"INSERT INTO activity ({columns}) "
                      f"VALUES ({', '.join('?' * len(ActivityRow._fields))})",
                      [tuple(r) for r in reversed(keep)])

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

    # Wristbands: the door list (mirrors TT Hotel's)

    def wb_doors(self) -> list[dict]:
        """Every door TT Hotel has listed, hidden and no-longer-listed ones too, by building,
        floor and name."""
        return [dict(r) for r in self.conn.execute(
            """SELECT * FROM wb_doors ORDER BY building_number IS NULL, building_number,
               floor_number IS NULL, floor_number,
               COALESCE(NULLIF(name, ''), NULLIF(alias, ''), tt_name) COLLATE NOCASE, lock_id""")]

    def wb_door(self, lock_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM wb_doors WHERE lock_id = ?", (lock_id,)).fetchone()
        return dict(row) if row else None

    def set_wb_door(self, lock_id: int, **fields) -> None:
        """The name staff see for a door ('' = TT Hotel's), and whether it's hidden from them."""
        sets = {k: v for k, v in fields.items() if k in ("name", "hidden")}
        if not sets:
            return
        cols = ", ".join(f"{k} = ?" for k in sets)
        self.conn.execute(f"UPDATE wb_doors SET {cols} WHERE lock_id = ?", (*sets.values(), lock_id))
        self.conn.commit()

    def wb_last_sync(self) -> tuple[float | None, int]:
        """(when TT Hotel's door list was last synced, how many doors it listed then)."""
        row = self.conn.execute(
            "SELECT MAX(synced_at), COUNT(*) FROM wb_doors WHERE active = 1").fetchone()
        return row[0], row[1]

    def sync_wb_doors(self, locks: list[dict]) -> dict[str, int]:
        """Bring the door list in line with TT Hotel's (tthotel.hotel_locks()). New doors are
        added and changed ones updated; names and hiding chosen here stay. A door TT Hotel no
        longer lists is kept (old cards and records still name it) but can't be put on new cards.
        Returns {"added", "updated", "removed", "total"}."""
        c = self.conn
        now = time.time()
        existing = {r["lock_id"]: dict(r) for r in c.execute("SELECT * FROM wb_doors")}
        cap = lambda value, n: str(value or "")[:n]
        number = lambda v: v if isinstance(v, int) and not isinstance(v, bool) else None
        seen: set[int] = set()
        added = updated = 0
        try:
            for lock in locks:
                lock_id, mac = lock.get("lock_id"), str(lock.get("mac") or "")
                if (number(lock_id) is None or lock_id <= 0 or lock_id in seen
                        or len(mac) != 12 or any(ch not in "0123456789ABCDEF" for ch in mac)):
                    continue   # unusable: a card can't be written for it
                seen.add(lock_id)
                values = {"tt_name": cap(lock.get("name"), 100), "alias": cap(lock.get("alias"), 100),
                          "mac": mac, "lock_type": number(lock.get("lock_type")) or 1,
                          "building_number": number(lock.get("building_number")),
                          "building_name": cap(lock.get("building_name"), 100),
                          "floor_number": number(lock.get("floor_number")),
                          "floor_name": cap(lock.get("floor_name"), 100),
                          "battery": number(lock.get("battery")),
                          "has_gateway": 1 if lock.get("has_gateway") else 0}
                old = existing.get(lock_id)
                if old is None:
                    added += 1
                    c.execute(f"""INSERT INTO wb_doors (lock_id, {", ".join(values)}, active,
                                  synced_at) VALUES (?, {", ".join("?" * len(values))}, 1, ?)""",
                              (lock_id, *values.values(), now))
                    continue
                # Battery levels change all the time; they don't count as the door changing.
                if not old["active"] or any(old[k] != v for k, v in values.items() if k != "battery"):
                    updated += 1
                cols = ", ".join(f"{k} = ?" for k in values)
                c.execute(f"UPDATE wb_doors SET {cols}, active = 1, synced_at = ? WHERE lock_id = ?",
                          (*values.values(), now, lock_id))
            gone = [i for i, row in existing.items() if i not in seen and row["active"]]
            c.executemany("UPDATE wb_doors SET active = 0 WHERE lock_id = ?", [(i,) for i in gone])
            c.commit()
        except Exception:
            c.rollback()
            raise
        return {"added": added, "updated": updated, "removed": len(gone), "total": len(seen)}

    # Wristbands: card jobs and the cards in them

    def add_wb_job(self, *, card_type: str, name: str, count: int, records: list[dict],
                   summary: str, expires_at: float, allow_lockout: bool, staff_id: int | None,
                   device_id: int | None, actor: str, role: str | None, ip: str | None,
                   encoder_url: str, holder_staff_id: int | None = None,
                   department_id: int | None = None, source: str | None = None,
                   kind: str = "write", target_card_id: int | None = None,
                   target_card: str | None = None, cancel_until: float | None = None) -> int:
        """Record a new card job (queued) with one pending card per wristband. Returns its ID.
        kind: 'write' (make cards), 'revoke' (clear target_card) or 'notice' (a lost card
        notice for target_card); a 'write' with target_card is a replacement that also carries
        the old card's cancellation."""
        assert kind in WB_JOB_KINDS
        cap = lambda value, n: None if value is None else str(value)[:n]
        c = self.conn
        now = time.time()
        try:
            job_id = c.execute(
                """INSERT INTO wb_jobs (ts, card_type, name, count, records, summary, expires_at,
                   allow_lockout, staff_id, device_id, actor, role, ip, encoder_url, status,
                   holder_staff_id, department_id, source, kind, target_card_id, target_card,
                   cancel_until)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)""",
                (now, card_type, cap(name, 80), count, json.dumps(records), cap(summary, 300),
                 expires_at, int(bool(allow_lockout)), staff_id, device_id, cap(actor, 120),
                 cap(role, 80), cap(ip, 64), encoder_url, holder_staff_id, department_id,
                 cap(source, 20), kind, target_card_id, cap(target_card, 40),
                 cancel_until)).lastrowid
            c.executemany("INSERT INTO wb_cards (job_id, seq, status) VALUES (?, ?, 'pending')",
                          [(job_id, seq) for seq in range(1, count + 1)])
            self._prune_wb_jobs(now)
            c.commit()
        except Exception:
            c.rollback()
            raise
        return job_id

    def _prune_wb_jobs(self, now: float) -> None:
        """Keep WB_JOBS_KEEP_DAYS or WB_JOBS_MAX_ROWS of card jobs, whichever is less, like the
        activity log, with their cards and lock-outs; unfinished jobs always stay. Counted by
        when jobs were made, not by ID: jobs added from a backup get new IDs whatever their age.
        Doesn't commit."""
        c = self.conn
        old = [r[0] for r in c.execute(
            f"""SELECT id FROM wb_jobs WHERE status NOT IN ({", ".join("?" * len(WB_ACTIVE))})
                AND (ts < ? OR id IN (SELECT id FROM wb_jobs ORDER BY ts DESC
                                      LIMIT -1 OFFSET ?))""",
            (*WB_ACTIVE, now - WB_JOBS_KEEP_DAYS * 86400, WB_JOBS_MAX_ROWS))]
        c.executemany("DELETE FROM wb_lockouts WHERE card_id IN "
                      "(SELECT id FROM wb_cards WHERE job_id = ?)", [(i,) for i in old])
        c.executemany("DELETE FROM wb_cards WHERE job_id = ?", [(i,) for i in old])
        c.executemany("DELETE FROM wb_jobs WHERE id = ?", [(i,) for i in old])

    def _wb_job_rows(self, rows) -> list[dict]:
        jobs = [dict(r) for r in rows]
        for job in jobs:
            try:
                job["records"] = json.loads(job["records"])
            except ValueError:
                job["records"] = []
            job["cards"] = [dict(r) for r in self.conn.execute(
                """SELECT id, seq, status, card_number, error, written_at, state, state_at,
                   state_by, cancelled_card FROM wb_cards WHERE job_id = ? ORDER BY seq""",
                (job["id"],))]
        return jobs

    def wb_job(self, job_id: int) -> dict | None:
        """One card job with its cards (and the records written on them as a list)."""
        return next(iter(self._wb_job_rows(
            self.conn.execute("SELECT * FROM wb_jobs WHERE id = ?", (job_id,)))), None)

    def wb_jobs(self, limit: int = 50, before: float | None = None, staff_id: int | None = None,
                q: str = "", kinds: tuple[str, ...] | None = None) -> list[dict]:
        """Card jobs, newest first: everyone's, or one person's. q matches part of the name the
        cards were made for, or the start of a card number. kinds: only these job kinds (None:
        all)."""
        where, params = ["ts < ?"], [before or time.time() + 1]
        if kinds is not None:
            where.append(f"kind IN ({', '.join('?' * len(kinds))})")
            params += list(kinds)
        if staff_id is not None:
            where.append("staff_id = ?")
            params.append(staff_id)
        q = str(q or "").strip()[:60]
        # Card numbers by how they start, through their index (a number anywhere in one read
        # every card ever made).
        number = "id IN (SELECT job_id FROM wb_cards WHERE card_number >= ? AND card_number < ?)"
        if q.isascii() and q.isdigit() and len(q) >= CARD_NUMBER_DIGITS:
            # Long enough to be a card number, and no name holds that many digits: only card
            # numbers, which spares reading every name ever used.
            where.append(number)
            params += [q, q + "\x7f"]
        elif q:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(f"(name LIKE ? ESCAPE '\\' OR {number})")
            params += [like, q, q + "\x7f"]
        return self._wb_job_rows(self.conn.execute(
            f"SELECT * FROM wb_jobs WHERE {' AND '.join(where)} ORDER BY ts DESC, id DESC LIMIT ?",
            (*params, limit)))

    def active_wb_job(self, encoder_url: str) -> dict | None:
        """The unfinished card job on an encoder, if there is one."""
        row = self.conn.execute(
            f"SELECT id FROM wb_jobs WHERE encoder_url = ? AND {UNFINISHED} ORDER BY id LIMIT 1",
            (encoder_url,)).fetchone()
        return self.wb_job(row[0]) if row else None

    def wb_card_job(self, card_number: str) -> dict | None:
        """The most recent job that wrote this card number (a card made, not one cleared or a
        notice)."""
        row = self.conn.execute(
            """SELECT c.job_id FROM wb_cards c JOIN wb_jobs j ON j.id = c.job_id
               WHERE c.card_number = ? AND c.status = 'written' AND j.kind = 'write'
               ORDER BY c.written_at DESC, c.id DESC LIMIT 1""",
            (str(card_number)[:40],)).fetchone()
        return self.wb_job(row[0]) if row else None

    def wb_last_card(self, encoder_url: str, since: float) -> str | None:
        """The last card written on this encoder since then, if any (from jobs started up to a
        day before, so only recent jobs are looked at)."""
        row = self.conn.execute(
            """SELECT c.card_number FROM wb_cards c JOIN wb_jobs j ON j.id = c.job_id
               WHERE j.ts >= ? AND j.encoder_url = ? AND c.status = 'written'
               AND c.card_number IS NOT NULL
               AND c.written_at >= ? ORDER BY c.written_at DESC, c.id DESC LIMIT 1""",
            (since - 86400, encoder_url, since)).fetchone()
        return row[0] if row else None

    def update_wb_job(self, job_id: int, **fields) -> None:
        sets = {k: v for k, v in fields.items() if k in WB_JOB_FIELDS}
        if sets:
            cols = ", ".join(f"{k} = ?" for k in sets)
            self.conn.execute(f"UPDATE wb_jobs SET {cols} WHERE id = ?", (*sets.values(), job_id))
            self.conn.commit()

    def update_wb_card(self, job_id: int, seq: int, **fields) -> None:
        sets = {k: v for k, v in fields.items() if k in WB_CARD_FIELDS}
        if sets:
            cols = ", ".join(f"{k} = ?" for k in sets)
            self.conn.execute(f"UPDATE wb_cards SET {cols} WHERE job_id = ? AND seq = ?",
                              (*sets.values(), job_id, seq))
            self.conn.commit()

    def cancel_wb_job(self, job_id: int, message: str) -> None:
        """Close an unfinished card job that nothing is working on any more. Cards not yet
        written are marked cancelled."""
        self.conn.execute("""UPDATE wb_jobs SET status = 'cancelled', message = ?, finished_at = ?
                             WHERE id = ?""", (message, time.time(), job_id))
        self.conn.execute("""UPDATE wb_cards SET status = 'cancelled'
                             WHERE job_id = ? AND status IN ('pending', 'failed')""", (job_id,))
        self.conn.commit()

    def cancel_unfinished_wb_jobs(self, message: str) -> list[int]:
        """Close every unfinished card job (the add-on restarted while they ran, so nothing is
        working on them any more). Returns their IDs."""
        ids = [r[0] for r in self.conn.execute(f"SELECT id FROM wb_jobs WHERE {UNFINISHED}")]
        for job_id in ids:
            self.cancel_wb_job(job_id, message)
        return ids

    # Staff key cards: every staff card made, its state, and locking out lost ones

    def _key_card_rows(self, where: list[str], params: list, limit: int) -> list[dict]:
        rows = [dict(r) for r in self.conn.execute(
            f"""SELECT c.id, c.job_id, c.seq, c.card_number, c.written_at, c.state, c.state_at,
                c.state_by, c.cancelled_card, j.name, j.summary, j.records, j.expires_at,
                j.allow_lockout, j.actor, j.ts AS made_at, j.source, j.holder_staff_id,
                j.department_id, j.staff_id, j.encoder_url, s.name AS holder_name,
                dp.name AS department_name
                FROM wb_cards c JOIN wb_jobs j ON j.id = c.job_id
                LEFT JOIN staff s ON s.id = j.holder_staff_id
                LEFT JOIN departments dp ON dp.id = j.department_id
                WHERE j.card_type = 'staff' AND j.kind = 'write' AND c.status = 'written'
                AND c.card_number IS NOT NULL AND {' AND '.join(where or ['1'])}
                ORDER BY j.ts DESC, c.id DESC LIMIT ?""", (*params, limit))]
        for row in rows:
            try:
                row["records"] = json.loads(row["records"])
            except ValueError:
                row["records"] = []
            row["lockouts"] = self.lockouts(row["id"])
        return rows

    def key_cards(self, state: str = "all", q: str = "", limit: int = 100,
                  before: int | None = None, holder_staff_id: int | None = None) -> list[dict]:
        """Staff cards made (one row per card written, whoever made it), newest first: by when
        their job was made, since cards added from a backup get new IDs whatever their age.
        state: active (not expired), expired, revoked, lost or all. q matches part of the name
        the card was made for, its holder's name, or its number. before: a card ID, for the next
        page (the cards listed after it)."""
        assert state in KEY_CARD_FILTERS
        now = time.time()
        where, params = [], []
        if state == "active":
            where += ["c.state = 'active'", "j.expires_at > ?"]
            params.append(now)
        elif state == "expired":
            where += ["c.state = 'active'", "j.expires_at <= ?"]
            params.append(now)
        elif state != "all":
            where.append("c.state = ?")
            params.append(state)
        if before is not None:
            made = self.conn.execute("SELECT j.ts FROM wb_cards c JOIN wb_jobs j ON j.id = c.job_id "
                                     "WHERE c.id = ?", (before,)).fetchone()
            if made:
                where.append("(j.ts < ? OR (j.ts = ? AND c.id < ?))")
                params += [made[0], made[0], before]
            else:   # gone since (trimmed with its job): go on by ID
                where.append("c.id < ?")
                params.append(before)
        if holder_staff_id is not None:
            where.append("j.holder_staff_id = ?")
            params.append(holder_staff_id)
        q = str(q or "").strip()[:60]
        if q:
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(j.name LIKE ? ESCAPE '\\' OR s.name LIKE ? ESCAPE '\\' "
                         "OR (c.card_number >= ? AND c.card_number < ?))")   # how it starts
            params += [like, like, q, q + "\x7f"]
        return self._key_card_rows(where, params, max(1, min(limit, 500)))

    def key_card(self, card_id: int) -> dict | None:
        """One staff card made (by its wb_cards ID), in any state."""
        return next(iter(self._key_card_rows(["c.id = ?"], [card_id], 1)), None)

    def wb_card(self, card_id: int) -> dict | None:
        """One card written by any job (a wristband or a staff card), by its wb_cards ID, with
        the name it was made for."""
        row = self.conn.execute(
            """SELECT c.id, c.job_id, c.status, c.card_number, c.state, j.name, j.card_type
               FROM wb_cards c JOIN wb_jobs j ON j.id = c.job_id WHERE c.id = ?""",
            (card_id,)).fetchone()
        return dict(row) if row else None

    def set_card_state(self, card_id: int, state: str, by: str | None) -> None:
        assert state in CARD_STATES
        self.conn.execute("UPDATE wb_cards SET state = ?, state_at = ?, state_by = ? WHERE id = ?",
                          (state, time.time(), None if by is None else str(by)[:120], card_id))
        self.conn.commit()

    def retire_card_number(self, card_number: str, keep_card_id: int, by: str) -> list[int]:
        """A card number was just written again (a card is reusable once cleared): every older
        record of it that still counts as active is marked revoked, so the list never shows two
        live cards with one number. Returns their IDs."""
        ids = [r[0] for r in self.conn.execute(
            """SELECT id FROM wb_cards WHERE card_number = ? AND id != ? AND status = 'written'
               AND state = 'active'""", (str(card_number)[:40], keep_card_id))]
        self.conn.executemany(
            "UPDATE wb_cards SET state = 'revoked', state_at = ?, state_by = ? WHERE id = ?",
            [(time.time(), str(by)[:120], i) for i in ids])
        self.conn.commit()
        return ids

    def lockouts(self, card_id: int) -> list[dict]:
        """How locking out one card is going, door by door."""
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM wb_lockouts WHERE card_id = ? ORDER BY method, lock_id", (card_id,))]

    def add_lockouts(self, card_id: int, lock_ids: list[int], method: str) -> None:
        """Doors to lock a card out of by one method, as pending (doors already listed for that
        method keep their progress)."""
        assert method in LOCKOUT_METHODS
        now = time.time()
        self.conn.executemany(
            """INSERT OR IGNORE INTO wb_lockouts (card_id, lock_id, method, status, at)
               VALUES (?, ?, ?, 'pending', ?)""", [(card_id, i, method, now) for i in lock_ids])
        self.conn.commit()

    def set_lockout(self, card_id: int, lock_id: int, method: str, status: str,
                    detail: str | None = None, tries: int | None = None) -> None:
        assert status in LOCKOUT_STATUSES
        self.conn.execute(
            """UPDATE wb_lockouts SET status = ?, detail = ?, at = ?, tries = COALESCE(?, tries)
               WHERE card_id = ? AND lock_id = ? AND method = ?""",
            (status, None if detail is None else str(detail)[:300], time.time(), tries, card_id,
             lock_id, method))
        self.conn.commit()

    def stop_remote_lockouts(self, detail: str) -> int:
        """Remote lock-outs still pending (the add-on restarted while they ran) become failed, so
        they can be started again. Returns how many."""
        cur = self.conn.execute(
            """UPDATE wb_lockouts SET status = 'failed', detail = ?, at = ?
               WHERE method = 'remote' AND status = 'pending'""", (detail[:300], time.time()))
        self.conn.commit()
        return cur.rowcount

    def active_key_card_counts(self) -> dict[int, int]:
        """{person: how many active, unexpired key cards are theirs}, for the Staff tab."""
        return dict(self.conn.execute(
            """SELECT j.holder_staff_id, COUNT(*) FROM wb_cards c JOIN wb_jobs j ON j.id = c.job_id
               WHERE j.card_type = 'staff' AND j.kind = 'write' AND c.status = 'written'
               AND c.state = 'active' AND j.expires_at > ? AND j.holder_staff_id IS NOT NULL
               GROUP BY j.holder_staff_id""", (time.time(),)).fetchall())
