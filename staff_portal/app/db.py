"""SQLite storage for roles, phones, settings, the activity log and wristband records."""

import collections
import hashlib
import json
import os
import secrets
import sqlite3
import time
import urllib.parse

import doorlog
from wristbands import door_kind, permission_choice

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
-- Door activity: lock records TT Hotel pushes for doors on a gateway (doorlog.py). No foreign
-- keys either; card_id is the wb_cards row the card number matched, when it did.
CREATE TABLE IF NOT EXISTS wb_lock_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lock_id INTEGER NOT NULL,
    record_type INTEGER NOT NULL,        -- TT Hotel's record type (doorlog.HOW)
    success INTEGER NOT NULL,            -- 1 = opened, 0 = refused
    card_number TEXT NOT NULL DEFAULT '',  -- card records only ('' otherwise: no passcodes)
    username TEXT NOT NULL DEFAULT '',   -- TT Hotel's name for it: the card's, the app user's...
    lock_date REAL,                      -- by the lock's clock
    server_date REAL,                    -- when TT Hotel got it
    at REAL NOT NULL,                    -- when it happened, as listed
    battery INTEGER,
    card_id INTEGER,
    alert TEXT,                          -- lost|revoked|deleted|archived: not to be used
    problem INTEGER NOT NULL DEFAULT 0,  -- refused, an alarm, or an alert ("Problems only")
    received_at REAL NOT NULL,
    UNIQUE (lock_id, at, record_type, success, card_number, username)   -- TT Hotel sending again
);
CREATE INDEX IF NOT EXISTS wb_lock_records_at ON wb_lock_records (at);
CREATE INDEX IF NOT EXISTS wb_lock_records_card ON wb_lock_records (card_id, at);
CREATE INDEX IF NOT EXISTS wb_lock_records_problems ON wb_lock_records (at) WHERE problem = 1;
-- Card numbers without leading zeros, as a lock record's number is matched to a card made here.
CREATE INDEX IF NOT EXISTS wb_cards_number_digits ON wb_cards (ltrim(card_number, '0'));

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

-- Reports (app/reports/): whether each is on, which departments may view it and which may
-- manage it (upload files, change its settings), and its own settings. One row per report
-- key, made when the owner first changes it; no row = off. In backups.
CREATE TABLE IF NOT EXISTS reports (
    key TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    view_departments TEXT NOT NULL DEFAULT '[]',    -- JSON list of department IDs
    manage_departments TEXT NOT NULL DEFAULT '[]',
    settings TEXT NOT NULL DEFAULT '{}',            -- JSON object: the report's own settings
    updated_at REAL,
    updated_by TEXT
);
-- Tasks (tasks.py): each department's to-do list in the staff app. No foreign keys, like the
-- wristband tables: people, roles and departments are deleted and re-added by a restore. The
-- names are kept beside the IDs, so a task still says who made it after they've gone.
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    department_id INTEGER,               -- NULL = Everyone
    assigned_id INTEGER,                 -- staff.id, or NULL
    assigned_name TEXT,
    due_date TEXT,                       -- YYYY-MM-DD, the property's own date
    due_time TEXT,                       -- HH:MM, only with a date
    priority TEXT NOT NULL DEFAULT 'normal',   -- normal|urgent
    status TEXT NOT NULL DEFAULT 'todo',       -- todo|doing|done|wont
    status_at REAL NOT NULL,
    status_by TEXT,
    created_at REAL NOT NULL,
    created_by_id INTEGER,               -- NULL for tasks from forms
    created_by_name TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'quick',      -- quick|form
    form_id TEXT,
    form_name TEXT,
    submission_id TEXT,
    source_url TEXT,                     -- the submission in Connecteam, when it gives one
    updated_at REAL NOT NULL,
    deleted_at REAL,                     -- in the bin since (emptied after tasks.BIN_DAYS)
    deleted_by TEXT
);
CREATE INDEX IF NOT EXISTS tasks_open ON tasks (status, status_at);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_submission ON tasks (form_id, submission_id)
    WHERE submission_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS task_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    at REAL NOT NULL,
    by_id INTEGER,
    by_name TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,                -- created|status|edited|deleted|restored
    from_status TEXT,
    to_status TEXT
);
CREATE INDEX IF NOT EXISTS task_history_task ON task_history (task_id, at);
-- Connecteam forms whose submissions become tasks, and every submission already seen (so none
-- becomes a task twice, even after its task was deleted).
CREATE TABLE IF NOT EXISTS task_forms (
    form_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    department_id INTEGER,               -- NULL = Everyone
    title_question TEXT NOT NULL DEFAULT '',   -- '' = "Form name: submitter"
    assign_submitter INTEGER NOT NULL DEFAULT 0,
    picked_at REAL NOT NULL,             -- only submissions sent after this become tasks
    last_check REAL,
    last_ok REAL,
    last_error TEXT,
    made INTEGER NOT NULL DEFAULT 0      -- tasks made from it
);
CREATE TABLE IF NOT EXISTS task_form_seen (
    form_id TEXT NOT NULL,
    submission_id TEXT NOT NULL,
    seen_at REAL NOT NULL,
    PRIMARY KEY (form_id, submission_id)
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
        # Archived (status 'archived'): when, by whom, and the status Restore puts back
        # ('active' or 'disabled'). Their rows, cards and activity are all kept.
        "archived_at": "REAL",
        "archived_by": "TEXT",
        "archived_from": "TEXT",
        # 1 once an admin chose a linked person's department by hand (or kept it when linking):
        # the Connecteam sync no longer moves them to their smart group's department.
        "department_set": "INTEGER NOT NULL DEFAULT 0",
    },
    "wb_doors": {
        # 'public' (a shared door, such as the pool gate), 'room' (a guest room) or 'staff' (a
        # staff door: only on staff cards and contractor badges); NULL = not marked yet, offered
        # as a public door and a room. TT Hotel's door sync never changes it.
        "kind": "TEXT",
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
        # When the card itself was cleared: by a revoke job, found blank on an encoder, or its
        # number written again. It opens nothing from then on and may be given out again (in
        # TT Hotel's own program, too), so a lock record from later on isn't this card.
        "cleared_at": "REAL",
    },
    "roles": {
        # Pages for people with this role on role-based devices (JSON like a phone's choice;
        # NULL = every page). Kiosk devices choose their own pages.
        "layout_pages": "TEXT",
        # Which wristbands and cards people with this role may make (JSON, see wristbands.py;
        # NULL = none).
        "wristbands": "TEXT",
        # 1 = people with this role may edit and delete anyone's tasks (Manage tasks).
        "manage_tasks": "INTEGER NOT NULL DEFAULT 0",
    },
    "departments": {
        # Whether people in it must be clocked in on Connecteam to sign in: 1 = everyone, 0 = no
        # one, NULL = each person's own setting. A person's own, set by hand, still wins
        # (Database.clock_in_rule).
        "clock_in": "INTEGER",
        # 1 = made from a Connecteam smart group and not renamed here since, so its name follows
        # the group's. group_gone: 1 once its smart group is no longer listed (never deleted).
        "group_named": "INTEGER NOT NULL DEFAULT 0",
        "group_gone": "INTEGER NOT NULL DEFAULT 0",
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
    # Where PINs differ in length, the PIN pad signs in once typing stops this long (ms).
    "pin_pause_ms": "500",
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
    "wb_contractor_max": "5",        # contractor badges: most at once, and whether they open
    "wb_lockout_contractor": "0",    # deadbolted doors (they last at most 7 days)
    "wb_sectors": "0000000000011111",  # card sectors TT Hotel uses (its default: 12 to 16)
    "wb_card_wait": "60",            # seconds to wait for a wristband on the encoder
    "wb_max_records": "20",          # most doors written onto one card
    "wb_scan_subnets": "",           # networks searched for Automatic encoders (blank: the ones
                                     # in Home Assistant's network setting)
    # Connecteam staff sync: add every active Connecteam user as a person, and whether people it
    # adds must be clocked in (each person's own setting wins once an admin changes it).
    "connecteam_auto_staff": "0",
    "connecteam_auto_clock_in": "1",
    # Make a department for each smart group (the sync). On for new setups only: see __init__.
    "connecteam_auto_departments": "1",
    # Remote lock-out of lost cards through door gateways (experimental, off until tested):
    # which TT Hotel call enrolls the card number, and when the owner confirmed it works.
    "wb_remote_lockout": "0",
    "wb_remote_lockout_api": "reversed",
    "wb_remote_lockout_tested": "",  # set only by the test helper, never from a form or backup
    # Door activity: the secret part of the address TT Hotel sends lock records to (made when the
    # dashboard first shows it), and whether a lost or revoked card tried at a door is also a
    # Home Assistant notification (it's always in the activity log).
    "tthotel_callback_token": "",
    "wb_lost_card_alert": "1",
    # The HR portal (hr/): off until the owner turns it on, and the address it answers on.
    "hr_enabled": "0",
    "hr_host": "",
    # HR portal: Google Drive through the owner's Apps Script (hr/gdrive.py). The Web app URL, the
    # secret written into the script, the HR shared drive and the restricted one ("": a folder in
    # the HR drive), and what the last check found (internal).
    "hr_google_url": "",
    "hr_google_secret": "",
    "hr_google_drive": "",
    "hr_google_restricted_drive": "",
    "hr_google_status": "{}",
    # The HR portal's Connecteam reads today, in its own lane (internal: connecteam.hr_spend).
    # All the hr_ settings are private: never in Staff Access's own backups or /api/state.
    "hr_connecteam_usage": "{}",
    # Reports' and tasks' Connecteam requests today, in their own lane (internal: ctdata.py).
    "reports_connecteam_usage": "{}",
}

# Settings that never leave the add-on: not shown in the dashboard (only whether they're set),
# not put in backups, and not taken from one. Only a migration file carries them, encrypted
# (migrate.py), to another install of the add-on.
PRIVATE_SETTINGS = ("connecteam_client_secret", "connecteam_usage", "tthotel_client_secret",
                    "tthotel_password_md5", "tthotel_token", "tthotel_usage",
                    "tthotel_callback_token", "hr_enabled", "hr_host", "hr_connecteam_usage",
                    "hr_google_url", "hr_google_secret", "hr_google_drive",
                    "hr_google_restricted_drive", "hr_google_status", "reports_connecteam_usage")

# Settings of older versions, removed when the add-on starts: the HR portal's own Connecteam app,
# chat and certificate copies (0.10.0 to 0.10.3; it only reads, through the one connection, now).
RETIRED_SETTINGS = ("hr_connecteam_client_id", "hr_connecteam_client_secret", "hr_connecteam_chat",
                    "hr_connecteam_documents")

ENROLL_TTL_DEFAULT = 72 * 3600
ACTIVITY_KEEP_DAYS = 180
ACTIVITY_MAX_ROWS = 50_000
WB_JOBS_KEEP_DAYS = 2 * 365    # card records outlive the activity log: wristbands can be asked
WB_JOBS_MAX_ROWS = 100_000     # about long after they were made
LOCK_RECORDS_KEEP_DAYS = 180   # door activity, like the activity log (TT Hotel keeps six months)
LOCK_RECORDS_MAX_ROWS = 50_000
WB_ACTIVE = ("queued", "running", "needs_attention")   # card jobs that haven't finished
# The same as SQL, spelled out: SQLite only uses the partial index below for a query that spells
# out the same statuses (not one that passes them as parameters).
# The day-pass and overnight shared doors: never staff doors (see _unshare_doors).
SHARED_DOOR_SETTINGS = ("wb_daypass_doors", "wb_overnight_doors")
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
                  "cancelled_card", "cleared_at"}
WB_JOB_KINDS = ("write", "revoke", "notice")
CARD_STATES = ("active", "revoked", "lost")
LOCKOUT_METHODS = ("remote", "notice", "replacement")
LOCKOUT_STATUSES = ("pending", "done", "failed")
KEY_CARD_FILTERS = ("active", "expired", "revoked", "lost", "all")
STAFF_FIELDS = {"name", "pin_hash", "status", "failed_pins", "last_seen", "locked_until",
                "ha_user", "ha_refresh_token", "ha_link_error", "connecteam_user_id",
                "require_clock_in", "role_id", "layout_pages", "wristbands", "department_id",
                "connecteam_auto", "clock_in_set", "role_set", "pin_from_connecteam",
                "connecteam_note", "pin_length", "archived_at", "archived_by", "archived_from",
                "department_set"}
# A person's status: 'active', 'disabled' (turned off, by an admin or by the Connecteam sync) or
# 'archived' (gone for good, but kept with everything they did). Turned off and archived people
# have no access anywhere: every way in asks staff_off(), or uses STAFF_ON in SQL.
STAFF_OFF = ("disabled", "archived")
STAFF_ON = "status NOT IN ('disabled', 'archived')"
DEPARTMENT_FIELDS = {"name", "smart_group_id", "role_id", "card_access", "card_days",
                     "card_lockout", "clock_in", "group_named", "group_gone"}

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
               "cancelled_card cleared_at")
LockoutRow = collections.namedtuple("LockoutRow", "card_id lock_id method status detail tries at")
# What a migration brings besides a backup's contents (see migrate.py): every column of a door.
DOOR_COLUMNS = ("lock_id", "tt_name", "alias", "mac", "lock_type", "building_number",
                "building_name", "floor_number", "floor_name", "battery", "has_gateway", "name",
                "hidden", "active", "synced_at", "kind")
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


def staff_off(member) -> bool:
    """Whether this person may not sign in, make cards or be offered anywhere: there's no such
    person, or they're turned off or archived."""
    return not member or member["status"] in STAFF_OFF


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


def _report_row(row) -> dict:
    def ids(raw):
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return [i for i in value if isinstance(i, int) and not isinstance(i, bool)] if isinstance(
            value, list) else []
    try:
        settings = json.loads(row["settings"])
    except (TypeError, ValueError):
        settings = {}
    return {"enabled": bool(row["enabled"]), "view": ids(row["view_departments"]),
            "manage": ids(row["manage_departments"]),
            "settings": settings if isinstance(settings, dict) else {},
            "updated_at": row["updated_at"], "updated_by": row["updated_by"]}


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
    # The HR key (main.py sets it): while it's kept out of backups, a few HR settings are stored
    # sealed with it (hr/crypto.py SEALED_SETTINGS). settings() opens them; update_settings()
    # seals them.
    sealer = None

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
        if self.conn.execute("SELECT 1 FROM settings WHERE key = 'connecteam_enabled'").fetchone():
            # Set up before smart group departments existed: they stay off until turned on.
            self.conn.execute("INSERT OR IGNORE INTO settings (key, value) "
                              "VALUES ('connecteam_auto_departments', '0')")
        for key, value in DEFAULT_SETTINGS.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )
        self.conn.executemany("DELETE FROM settings WHERE key = ?", [(k,) for k in RETIRED_SETTINGS])
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
        other.sealer = None           # as stored: a migration carries sealed settings as they are
        other.conn.execute("BEGIN")   # one moment: from the first read to close()
        return other

    # Settings

    def settings(self) -> dict:
        # Read on nearly every request, several times: kept, and read again after a change.
        # A copy each time, since callers add to it.
        if self._settings is None:
            found = {r["key"]: r["value"] for r in self.conn.execute(
                "SELECT key, value FROM settings WHERE key != 'layout'")}
            self._settings = self.sealer.open_settings(found) if self.sealer else found
        return dict(self._settings)

    def update_settings(self, values: dict) -> None:
        if self.sealer:
            values = self.sealer.seal_settings(values)   # raises Locked if it can't
        self._settings = None
        for key, value in values.items():
            if key in DEFAULT_SETTINGS:
                self.conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value))
                )
        self.conn.commit()

    def reseal_settings(self) -> None:
        """The HR key came back, or moved in or out of backups: its sealed settings follow."""
        self._settings = None
        if self.sealer:
            stored = {r["key"]: r["value"]
                      for r in self.conn.execute("SELECT key, value FROM settings")}
            for key, value in self.sealer.resealed(stored).items():
                self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                                  (key, value))
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
                f"SELECT COUNT(*) FROM staff WHERE role_id = ? AND {STAFF_ON}", (role["id"],),
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

    def set_role_manage_tasks(self, role_id: int, manage: bool) -> None:
        self.conn.execute("UPDATE roles SET manage_tasks = ? WHERE id = ?", (1 if manage else 0, role_id))
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
        """A new role with the same device, area, page, wristband and task settings. Phones and
        people stay on the original."""
        new_id = self.create_role(name)
        self.update_role(new_id, name, self.role_entities(role_id), self.role_areas(role_id))
        self.set_role_pages(new_id, self.role(role_id)["layout_pages"])
        # Copied as stored, so a damaged value stays damaged (no wristbands) in the copy too.
        self.conn.execute("UPDATE roles SET wristbands = (SELECT wristbands FROM roles WHERE id = ?), "
                          "manage_tasks = (SELECT manage_tasks FROM roles WHERE id = ?) "
                          "WHERE id = ?", (role_id, role_id, new_id))
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
            "AND status != 'archived' GROUP BY department_id").fetchall())
        for dept in found:
            dept["card_access"] = card_access_choice(dept["card_access"])
            dept["staff_count"] = counts.get(dept["id"], 0)
        return found

    def department(self, department_id) -> dict | None:
        return next((d for d in self.departments() if d["id"] == department_id), None)

    def department_clock_ins(self) -> dict[int, int]:
        """{department ID: its clock-in rule} for the departments that set one (1 or 0)."""
        return dict(self.conn.execute(
            "SELECT id, clock_in FROM departments WHERE clock_in IS NOT NULL").fetchall())

    def clock_in_rule(self, member: dict, rules: dict | None = None) -> tuple[bool, str]:
        """Whether this person must be clocked in on Connecteam to sign in, and where that comes
        from: "person" (an admin set theirs by hand, which beats their department), "department"
        (their department sets it for everyone in it) or "default" (their own setting, which the
        Connecteam sync keeps in line with "People added this way must be clocked in"). The one
        place the rule is worked out. rules: department_clock_ins(), when checking many people."""
        if member.get("clock_in_set"):
            return bool(member.get("require_clock_in")), "person"
        rule = (self.department_clock_ins() if rules is None else rules).get(
            member.get("department_id"))
        if rule is not None:
            return bool(rule), "department"
        return bool(member.get("require_clock_in")), "default"

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

    # Reports

    def reports(self) -> dict[str, dict]:
        """Every report's access and settings, by key: {enabled, view, manage, settings}."""
        return {r["key"]: _report_row(r) for r in self.conn.execute("SELECT * FROM reports")}

    def report(self, key: str) -> dict:
        row = self.conn.execute("SELECT * FROM reports WHERE key = ?", (key,)).fetchone()
        return _report_row(row) if row else {"enabled": False, "view": [], "manage": [],
                                             "settings": {}, "updated_at": None,
                                             "updated_by": None}

    def set_report(self, key: str, by: str, **fields) -> None:
        """Change a report's enabled, view, manage (lists of department IDs) or settings."""
        current = self.report(key)
        current.update(fields)
        self.conn.execute(
            """INSERT OR REPLACE INTO reports (key, enabled, view_departments, manage_departments,
               settings, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (key, int(bool(current["enabled"])), json.dumps(current["view"]),
             json.dumps(current["manage"]), json.dumps(current["settings"]), time.time(),
             str(by)[:120]))
        self.conn.commit()

    # Tasks (tasks.py has the rules: who sees and edits what)

    def task(self, task_id) -> dict | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    # Who sees a task (tasks.can_see): its department's people, everyone for Everyone, the
    # person it's given to and the one who made it, and managers when its department is gone.
    TASK_SEEN = """(department_id IS NULL OR department_id = ? OR assigned_id = ? OR created_by_id = ?
                    OR (? AND department_id NOT IN (SELECT id FROM departments)))"""

    def tasks_for(self, member: dict, since: float, manager: bool = False) -> list[dict]:
        """The tasks someone may see (tasks.can_see), not in the bin: every open one, and finished
        ones from `since` on. Urgent first, then by due date (none last), then newest."""
        rows = self.conn.execute(
            f"""SELECT * FROM tasks WHERE deleted_at IS NULL AND {self.TASK_SEEN}
               AND (status IN ('todo', 'doing') OR status_at >= ?)
               ORDER BY priority = 'urgent' DESC, due_date IS NULL, due_date, due_time IS NULL,
                        due_time, created_at DESC, id DESC""",
            (member.get("department_id"), member["id"], member["id"], 1 if manager else 0, since))
        return [dict(r) for r in rows]

    def tasks_in_bin(self, member: dict, manager: bool) -> list[dict]:
        """Deleted tasks this person may put back (tasks.can_restore): their own, and for a
        manager the ones they could see if they weren't deleted."""
        seen = self.TASK_SEEN if manager else "created_by_id = ?"
        args = (member.get("department_id"), member["id"], member["id"], 1) if manager else (member["id"],)
        rows = self.conn.execute(
            f"SELECT * FROM tasks WHERE deleted_at IS NOT NULL AND {seen} "
            "ORDER BY deleted_at DESC LIMIT 200", args)
        return [dict(r) for r in rows]

    def task_history(self, task_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT at, by_name, action, from_status, to_status FROM task_history "
            "WHERE task_id = ? ORDER BY at, id", (task_id,))]

    def _task_event(self, task_id: int, at: float, by: dict | None, action: str,
                    from_status: str | None = None, to_status: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO task_history (task_id, at, by_id, by_name, action, from_status, to_status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_id, at, by and by.get("id"), (by or {}).get("name", "")[:60], action,
             from_status, to_status))

    def add_task(self, fields: dict, by: dict | None, at: float, commit: bool = True) -> int:
        """A new task (fields from tasks.clean_task or tasks.task_from_submission). by: the
        person who made it ({"id", "name"}), or {"name": "Connecteam form"} for a form."""
        row = {"source": "quick", "form_id": None, "form_name": None, "submission_id": None,
               "source_url": None, **fields}
        cur = self.conn.execute(
            """INSERT INTO tasks (title, notes, department_id, assigned_id, assigned_name, due_date,
               due_time, priority, status, status_at, created_at, created_by_id, created_by_name,
               source, form_id, form_name, submission_id, source_url, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'todo', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["title"], row["notes"], row["department_id"], row["assigned_id"],
             row["assigned_name"], row["due_date"], row["due_time"], row["priority"], at, at,
             by and by.get("id"), (by or {}).get("name", "")[:60], row["source"], row["form_id"],
             row["form_name"], row["submission_id"], row["source_url"], at))
        self._task_event(cur.lastrowid, at, by, "created", None, "todo")
        if commit:
            self.conn.commit()
        return cur.lastrowid

    def edit_task(self, task_id: int, fields: dict, by: dict, at: float) -> None:
        keys = ("title", "notes", "department_id", "assigned_id", "assigned_name", "due_date",
                "due_time", "priority")
        self.conn.execute(f"UPDATE tasks SET {', '.join(f'{k} = ?' for k in keys)}, updated_at = ? "
                          "WHERE id = ?", (*(fields[k] for k in keys), at, task_id))
        self._task_event(task_id, at, by, "edited")
        self.conn.commit()

    def set_task_status(self, task_id: int, status: str, by: dict, at: float) -> bool:
        """Move a task to a status. False when it was already there (nothing recorded)."""
        old = self.task(task_id)
        if not old or old["status"] == status:
            return False
        self.conn.execute("UPDATE tasks SET status = ?, status_at = ?, status_by = ?, updated_at = ? "
                          "WHERE id = ?", (status, at, by["name"][:60], at, task_id))
        self._task_event(task_id, at, by, "status", old["status"], status)
        self.conn.commit()
        return True

    def bin_task(self, task_id: int, by: dict, at: float, restore: bool = False) -> None:
        """Put a task in the bin (restore: take it out again)."""
        self.conn.execute("UPDATE tasks SET deleted_at = ?, deleted_by = ?, updated_at = ? WHERE id = ?",
                          (None if restore else at, None if restore else by["name"][:60], at, task_id))
        self._task_event(task_id, at, by, "restored" if restore else "deleted")
        self.conn.commit()

    def purge_tasks(self, now: float, bin_days: int, keep_days: int) -> int:
        """Empty the bin of tasks deleted more than bin_days ago, and drop tasks finished more
        than keep_days ago. Returns how many went."""
        gone = [r[0] for r in self.conn.execute(
            """SELECT id FROM tasks WHERE deleted_at < ?
               OR (status IN ('done', 'wont') AND status_at < ?)""",
            (now - bin_days * 86400, now - keep_days * 86400))]
        for start in range(0, len(gone), 500):
            part = gone[start:start + 500]
            marks = ", ".join("?" * len(part))
            self.conn.execute(f"DELETE FROM task_history WHERE task_id IN ({marks})", part)
            self.conn.execute(f"DELETE FROM tasks WHERE id IN ({marks})", part)
        # Submissions seen long ago can't come back (a check reaches back a month at most).
        self.conn.execute("DELETE FROM task_form_seen WHERE seen_at < ?", (now - keep_days * 86400,))
        self.conn.commit()
        return len(gone)

    def task_forms(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM task_forms ORDER BY name, form_id")]

    def task_form(self, form_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM task_forms WHERE form_id = ?", (form_id,)).fetchone()
        return dict(row) if row else None

    def set_task_form(self, form_id: str, fields: dict, now: float) -> None:
        """Choose a form, or change its settings. A form chosen now only makes tasks from
        submissions sent from now on; changing its settings doesn't move that."""
        self.conn.execute(
            """INSERT INTO task_forms (form_id, name, department_id, title_question,
               assign_submitter, picked_at) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (form_id) DO UPDATE SET name = excluded.name,
               department_id = excluded.department_id, title_question = excluded.title_question,
               assign_submitter = excluded.assign_submitter""",
            (form_id, fields["name"], fields["department_id"], fields["title_question"],
             fields["assign_submitter"], now))
        self.conn.commit()

    def delete_task_form(self, form_id: str) -> None:
        self.conn.execute("DELETE FROM task_forms WHERE form_id = ?", (form_id,))
        self.conn.commit()

    def task_form_checked(self, form_id: str, now: float, error: str | None) -> None:
        self.conn.execute(
            "UPDATE task_forms SET last_check = ?, last_error = ?, last_ok = CASE WHEN ? IS NULL "
            "THEN ? ELSE last_ok END WHERE form_id = ?", (now, error, error, now, form_id))
        self.conn.commit()

    def task_seen(self, form_id: str, submission_ids: list[str]) -> set[str]:
        """Which of these submissions of a form have been seen before (or have a task)."""
        seen = set()
        for start in range(0, len(submission_ids), 400):
            part = submission_ids[start:start + 400]
            marks = ", ".join("?" * len(part))
            seen |= {r[0] for r in self.conn.execute(
                f"""SELECT submission_id FROM task_form_seen WHERE form_id = ? AND submission_id IN ({marks})
                    UNION SELECT submission_id FROM tasks WHERE form_id = ? AND submission_id IN ({marks})""",
                (form_id, *part, form_id, *part))}
        return seen

    def add_form_tasks(self, form_id: str, seen: list[str], new: list[dict], at: float) -> int:
        """Record a check's new submissions as seen and add their tasks, all at once."""
        try:
            self.conn.executemany(
                "INSERT OR IGNORE INTO task_form_seen (form_id, submission_id, seen_at) VALUES (?, ?, ?)",
                [(form_id, sid, at) for sid in seen])
            for fields in new:
                self.add_task(fields, {"name": "Connecteam form"}, at, commit=False)
            self.conn.execute("UPDATE task_forms SET made = made + ? WHERE form_id = ?",
                              (len(new), form_id))
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return len(new)

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
        cols = ("s.id, s.name, s.connecteam_user_id, s.require_clock_in, s.clock_in_set, "
                "s.department_id")
        if device["staff_all"]:
            rows = self.conn.execute(
                f"SELECT {cols} FROM staff s WHERE {STAFF_ON} ORDER BY name COLLATE NOCASE")
        else:
            rows = self.conn.execute(
                f"""SELECT {cols} FROM staff s JOIN staff_devices sd ON sd.staff_id = s.id
                   WHERE sd.device_id = ? AND s.{STAFF_ON}
                   ORDER BY s.name COLLATE NOCASE""", (device["id"],))
        return [dict(r) for r in rows]

    def staff_pins(self, device: dict | None = None) -> list[tuple[int, str]]:
        """(id, pin_hash) of the active staff allowed on a shared device (or of everyone,
        archived people too: their PINs stay taken, so Restore gives them back a PIN of their
        own)."""
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

    def update_staff_many(self, changes: list[tuple[int, dict]]) -> None:
        """update_staff for several people in one transaction (all or none), so links can be
        swapped between two people without either being linked twice."""
        try:
            for staff_id, fields in changes:
                sets = {k: v for k, v in fields.items() if k in STAFF_FIELDS}
                if sets:
                    cols = ", ".join(f"{k} = ?" for k in sets)
                    self.conn.execute(f"UPDATE staff SET {cols} WHERE id = ?",
                                      (*sets.values(), staff_id))
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()

    def archive_staff(self, staff_id: int, by: str) -> None:
        """Archive a person: no access anywhere from now on (staff_off). Nothing of theirs is
        deleted, and the status they had ('active' or 'disabled') is kept for Restore."""
        self.conn.execute(
            """UPDATE staff SET status = 'archived', archived_at = ?, archived_by = ?,
               archived_from = CASE WHEN status = 'disabled' THEN 'disabled' ELSE 'active' END
               WHERE id = ? AND status != 'archived'""", (time.time(), str(by)[:120], staff_id))
        self.conn.commit()

    def restore_staff(self, staff_id: int) -> None:
        """Back to how they were before they were archived (signing in with their PIN again,
        unless they'd been turned off), with any wrong-PIN lock cleared."""
        self.conn.execute(
            """UPDATE staff SET status = CASE WHEN archived_from = 'disabled' THEN 'disabled'
               ELSE 'active' END, archived_at = NULL, archived_by = NULL, archived_from = NULL,
               failed_pins = 0, locked_until = NULL WHERE id = ? AND status = 'archived'""",
            (staff_id,))
        self.conn.commit()

    def archived_staff_ids(self) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT id FROM staff WHERE status = 'archived'")}

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

    def _restore_tasks(self, section: dict | None) -> None:
        """Part of replace_everything: the backup's tasks (tasks.clean_backup_section), or, for a
        backup from before tasks, the ones here kept. Kept tasks stay with the people who made
        them or have them only while those people are here under the same ID and name."""
        c = self.conn
        if section is None:
            for column, name in (("assigned_id", "assigned_name"), ("created_by_id", "created_by_name")):
                c.execute(f"""UPDATE tasks SET {column} = NULL WHERE {column} IS NOT NULL AND NOT EXISTS
                              (SELECT 1 FROM staff s WHERE s.id = tasks.{column} AND s.name = tasks.{name})""")
            return
        for table in ("tasks", "task_history", "task_forms", "task_form_seen"):
            c.execute(f"DELETE FROM {table}")
        columns = ("id", "title", "notes", "department_id", "assigned_id", "assigned_name", "due_date",
                   "due_time", "priority", "status", "status_at", "status_by", "created_at",
                   "created_by_id", "created_by_name", "source", "form_id", "form_name",
                   "submission_id", "source_url", "updated_at", "deleted_at", "deleted_by")
        c.executemany(f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                      [tuple(t[k] for k in columns) for t in section["tasks"]])
        c.executemany("""INSERT INTO task_history (task_id, at, by_id, by_name, action, from_status,
                         to_status) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                      [(h["task_id"], h["at"], h["by_id"], h["by_name"], h["action"],
                        h["from_status"], h["to_status"]) for h in section["history"]])
        c.executemany("""INSERT INTO task_forms (form_id, name, department_id, title_question,
                         assign_submitter, picked_at, last_ok) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                      [(f["form_id"], f["name"], f["department_id"], f["title_question"],
                        f["assign_submitter"], f["picked_at"], f["last_ok"]) for f in section["forms"]])
        c.executemany("INSERT INTO task_form_seen (form_id, submission_id, seen_at) VALUES (?, ?, ?)",
                      [(form_id, sid, time.time()) for form_id, sid in section["seen"]])

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
            c.executemany("INSERT INTO roles (id, name, created_at, layout_pages, wristbands, "
                          "manage_tasks) VALUES (?, ?, ?, ?, ?, ?)",
                          [(r["id"], r["name"], r["created_at"], r["layout_pages"], r["wristbands"],
                            r.get("manage_tasks", 0)) for r in snap["roles"]])
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
                       card_access, card_days, card_lockout, created_at, clock_in, group_named,
                       group_gone) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(d["id"], d["name"], d["sort_order"], d["smart_group_id"], d["role_id"],
                      d["card_access"], d["card_days"], d["card_lockout"], d["created_at"],
                      d.get("clock_in"), d.get("group_named", 0), d.get("group_gone", 0))
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
                   connecteam_note, pin_length, archived_at, archived_by, archived_from,
                   department_set)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(m["id"], m["name"], m["pin_hash"], m["status"], m["created_at"], m["last_seen"],
                  m["connecteam_user_id"], m["require_clock_in"], m["role_id"], m["layout_pages"],
                  m["wristbands"], m.get("department_id"), m.get("connecteam_auto", 0),
                  m.get("clock_in_set", 0), m.get("role_set", 0), m.get("pin_from_connecteam", 0),
                  m.get("connecteam_note"), m.get("pin_length"), m.get("archived_at"),
                  m.get("archived_by"), m.get("archived_from"), m.get("department_set", 0))
                 for m in snap["staff"]])
            c.execute("UPDATE staff SET department_id = NULL WHERE department_id NOT IN "
                      "(SELECT id FROM departments)")
            self._restore_tasks(snap.get("tasks"))
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
            # Report access and settings (backups from before reports have none: None leaves
            # the ones here as they are).
            if snap.get("reports") is not None:
                c.execute("DELETE FROM reports")
                c.executemany(
                    """INSERT INTO reports (key, enabled, view_departments, manage_departments,
                       settings, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    [(r["key"], r["enabled"], json.dumps(r["view"]), json.dumps(r["manage"]),
                      json.dumps(r["settings"]), r["updated_at"], r["updated_by"])
                     for r in snap["reports"]])
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
            # list itself comes from TT Hotel, and isn't replaced). Kinds too, from backups that
            # have them (older ones leave them as they are).
            c.executemany("UPDATE wb_doors SET name = ?, hidden = ? WHERE lock_id = ?",
                          [(d["name"], d["hidden"], d["lock_id"]) for d in snap["wristband_doors"]])
            c.executemany("UPDATE wb_doors SET kind = ? WHERE lock_id = ?",
                          [(d["kind"], d["lock_id"]) for d in snap["wristband_doors"]
                           if "kind" in d])
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

    def set_wb_door(self, lock_id: int, **fields) -> dict[str, list[int]]:
        """The name staff see for a door ('' = TT Hotel's), whether it's hidden from them, and
        its kind ('public', 'room', 'staff' or None). A door made a staff door is taken off the
        day-pass and overnight shared doors in the same transaction: returns what was taken off
        ({setting key: [lock ID]}, see _unshare_doors)."""
        sets = {k: v for k, v in fields.items() if k in ("name", "hidden", "kind")}
        if "kind" in sets:
            sets["kind"] = door_kind(sets["kind"])
        if not sets:
            return {}
        cols = ", ".join(f"{k} = ?" for k in sets)
        try:
            self.conn.execute(f"UPDATE wb_doors SET {cols} WHERE lock_id = ?",
                              (*sets.values(), lock_id))
            unshared = self._unshare_doors([lock_id]) if sets.get("kind") == "staff" else {}
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._settings = None   # read again, whichever way it went
        return unshared

    def set_wb_door_kinds(self, lock_ids: list[int], kind: str | None) -> int:
        """Mark many doors at once (a whole floor or building) as 'public', 'room', 'staff' or
        None. Returns how many doors there were."""
        return self.mark_wb_doors(lock_ids, kind)[0]

    def mark_wb_doors(self, lock_ids: list[int], kind: str | None) -> tuple[int, dict]:
        """set_wb_door_kinds, also returning the staff doors taken off the day-pass and
        overnight shared doors in the same transaction ({setting key: [lock ID]})."""
        rows = [(door_kind(kind), lock_id) for lock_id in lock_ids]
        try:
            changed = self.conn.executemany("UPDATE wb_doors SET kind = ? WHERE lock_id = ?", rows)
            unshared = self._unshare_doors(lock_ids) if door_kind(kind) == "staff" else {}
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._settings = None   # read again, whichever way it went
        return changed.rowcount, unshared

    def _unshare_doors(self, lock_ids: list[int]) -> dict[str, list[int]]:
        """Take doors (just made staff doors) off the day-pass and overnight shared-door
        settings, without committing: staff doors never go on a guest's card. Returns
        {setting key: [lock IDs taken off]} for the settings that had any."""
        drop, taken = set(lock_ids), {}
        number = lambda v: (int(v) if isinstance(v, str) and v.isdigit() else v
                            if isinstance(v, int) and not isinstance(v, bool) else None)
        for key in SHARED_DOOR_SETTINGS:
            row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            try:
                ids = json.loads(row[0]) if row else []
            except (TypeError, ValueError):
                continue
            if not isinstance(ids, list):
                continue
            gone = [number(v) for v in ids if number(v) in drop]
            if gone:
                self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                                  (key, json.dumps([v for v in ids if number(v) not in drop])))
                taken[key] = gone
        return taken

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

    def unfinished_wb_job_ids(self, staff_id: int) -> list[int]:
        """The card jobs this person started that haven't finished."""
        return [r[0] for r in self.conn.execute(
            f"SELECT id FROM wb_jobs WHERE staff_id = ? AND {UNFINISHED}", (staff_id,))]

    def unfinished_key_card_ids_for(self, staff_id: int) -> list[int]:
        """The unfinished card jobs writing a key card for this person (whoever started them,
        from the dashboard or the staff app). Revokes and lost card notices aren't among them."""
        return [r[0] for r in self.conn.execute(
            f"""SELECT id FROM wb_jobs WHERE holder_staff_id = ? AND kind = 'write'
                AND {UNFINISHED}""", (staff_id,))]

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
                s.status AS holder_status, dp.name AS department_name
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

    def set_card_state(self, card_id: int, state: str, by: str | None, *,
                       cleared: bool = False) -> None:
        """cleared: the card itself was cleared just now (see mark_card_cleared)."""
        assert state in CARD_STATES
        now = time.time()
        self.conn.execute(
            """UPDATE wb_cards SET state = ?, state_at = ?, state_by = ?,
               cleared_at = CASE WHEN ? THEN ? ELSE cleared_at END WHERE id = ?""",
            (state, now, None if by is None else str(by)[:120], int(cleared), now, card_id))
        self.conn.commit()

    def mark_card_cleared(self, card_id: int) -> None:
        """The card itself was just cleared (it keeps its state: a lost card found and cleared
        stays lost). From then on it opens nothing and may be given out again, so lock records
        from later on are no longer matched to it (match_card)."""
        self.conn.execute("UPDATE wb_cards SET cleared_at = ? WHERE id = ? AND cleared_at IS NULL",
                          (time.time(), card_id))
        self.conn.commit()

    def retire_card_number(self, card_number: str, keep_card_id: int, by: str) -> list[int]:
        """A card number was just written again (a card is reusable once cleared): every older
        record of it that still counts as active is marked revoked, so the list never shows two
        live cards with one number. Returns their IDs."""
        number, now = str(card_number)[:40], time.time()
        ids = [r[0] for r in self.conn.execute(
            """SELECT id FROM wb_cards WHERE card_number = ? AND id != ? AND status = 'written'
               AND state = 'active'""", (number, keep_card_id))]
        self.conn.executemany(
            "UPDATE wb_cards SET state = 'revoked', state_at = ?, state_by = ? WHERE id = ?",
            [(now, str(by)[:120], i) for i in ids])
        # Whatever was on the card before (in any state) is gone now.
        self.conn.execute(
            """UPDATE wb_cards SET cleared_at = ? WHERE card_number = ? AND id != ?
               AND status = 'written' AND cleared_at IS NULL""", (now, number, keep_card_id))
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

    def key_card_totals(self) -> dict[str, int]:
        """How many staff key cards still open doors (not expired): {"active", "lost"}, for the
        Key cards section's closed line."""
        row = self.conn.execute(
            """SELECT SUM(c.state = 'active'), SUM(c.state = 'lost')
               FROM wb_cards c JOIN wb_jobs j ON j.id = c.job_id
               WHERE j.card_type = 'staff' AND j.kind = 'write' AND c.status = 'written'
               AND c.card_number IS NOT NULL AND j.expires_at > ?""", (time.time(),)).fetchone()
        return {"active": row[0] or 0, "lost": row[1] or 0}

    def active_key_card_counts(self) -> dict[int, int]:
        """{person: how many active, unexpired key cards are theirs}, for the Staff tab."""
        return dict(self.conn.execute(
            """SELECT j.holder_staff_id, COUNT(*) FROM wb_cards c JOIN wb_jobs j ON j.id = c.job_id
               WHERE j.card_type = 'staff' AND j.kind = 'write' AND c.status = 'written'
               AND c.state = 'active' AND j.expires_at > ? AND j.holder_staff_id IS NOT NULL
               GROUP BY j.holder_staff_id""", (time.time(),)).fetchall())

    # Door activity: lock records TT Hotel pushes for doors on a gateway (doorlog.py)

    def match_card(self, number: str, at: float) -> dict | None:
        """The card made here that a lock record's card number is, or None: compared without
        leading zeros, as written, then with its bytes reversed (doorlog.card_keys). A number
        written again after its card was cleared is the card it was at the time: the last one
        written by then (a few minutes either way, for the lock's clock). A card cleared before
        the record (cleared_at) is never it: a cleared card goes back in the stack and may have
        been given out again in TT Hotel's own program, so the record is left unmatched."""
        slack = doorlog.CLOCK_SLACK
        for key in doorlog.card_keys(number):
            row = self.conn.execute(
                """SELECT c.id, c.card_number, c.state, c.state_at, c.cleared_at, j.name,
                   j.card_type, j.expires_at, s.status AS holder_status,
                   s.archived_at AS holder_archived_at
                   FROM wb_cards c JOIN wb_jobs j ON j.id = c.job_id
                   LEFT JOIN staff s ON s.id = j.holder_staff_id
                   WHERE ltrim(c.card_number, '0') = ? AND c.status = 'written' AND j.kind = 'write'
                   AND (c.cleared_at IS NULL OR ? <= c.cleared_at + ?)
                   ORDER BY COALESCE(c.written_at, j.ts) <= ? DESC, COALESCE(c.written_at, j.ts) DESC,
                   c.id DESC LIMIT 1""", (key, at, slack, at + slack)).fetchone()
            if row:
                return dict(row)
        return None

    def add_lock_records(self, lock_id: int, records: list[dict], now: float) -> list[dict]:
        """Store records for one door (doorlog.read_record), each card number matched to a card
        made here. Records already stored (TT Hotel sending them again) are skipped. The door's
        battery follows the newest record. Then the usual limits apply (LOCK_RECORDS_KEEP_DAYS,
        LOCK_RECORDS_MAX_ROWS). Returns the records added, each with its "id", "card" (the
        matched card or None) and "alert" (doorlog.alert_for)."""
        c = self.conn
        added = []
        try:
            newest = c.execute("SELECT MAX(at) FROM wb_lock_records WHERE lock_id = ?",
                               (lock_id,)).fetchone()[0]
            for record in records:
                card = self.match_card(record["card_number"], record["at"]) if record[
                    "card_number"] else None
                alert = doorlog.alert_for(record, card)
                problem = (not record["success"] or alert is not None
                           or record["record_type"] in doorlog.ALARM_TYPES)
                cur = c.execute(
                    """INSERT OR IGNORE INTO wb_lock_records (lock_id, record_type, success,
                       card_number, username, lock_date, server_date, at, battery, card_id, alert,
                       problem, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (lock_id, record["record_type"], record["success"], record["card_number"],
                     record["username"], record["lock_date"], record["server_date"], record["at"],
                     record["battery"], card and card["id"], alert, int(problem), now))
                if cur.rowcount:
                    added.append({**record, "id": cur.lastrowid, "card": card, "alert": alert})
            levels = [r for r in added if r["battery"] is not None]
            latest = max(levels, key=lambda r: r["at"], default=None)
            if latest and (newest is None or latest["at"] >= newest):
                c.execute("UPDATE wb_doors SET battery = ? WHERE lock_id = ?",
                          (latest["battery"], lock_id))
            c.execute("DELETE FROM wb_lock_records WHERE at < ? OR id <= "
                      "(SELECT MAX(id) FROM wb_lock_records) - ?",
                      (now - LOCK_RECORDS_KEEP_DAYS * 86400, LOCK_RECORDS_MAX_ROWS))
            c.commit()
        except Exception:
            c.rollback()
            raise
        return added

    def lock_records(self, limit: int = 50, before: int | None = None, lock_id: int | None = None,
                     problems: bool = False) -> list[dict]:
        """Door activity, newest first (by when it happened): every door's or one door's, all of
        it or only problems. Each row has the matched card's name, number and type ("card_name",
        "card_card_number", "card_type") and the door's names ("door_name", "door_alias",
        "door_tt_name").
        before: a record ID, for the next page (the records listed after it)."""
        where, params = [], []
        if lock_id is not None:
            where.append("r.lock_id = ?")
            params.append(lock_id)
        if problems:
            where.append("r.problem = 1")
        if before is not None:
            row = self.conn.execute("SELECT at FROM wb_lock_records WHERE id = ?",
                                    (before,)).fetchone()
            if row:
                where.append("(r.at < ? OR (r.at = ? AND r.id < ?))")
                params += [row[0], row[0], before]
            else:   # trimmed since: go on by ID
                where.append("r.id < ?")
                params.append(before)
        return [dict(r) for r in self.conn.execute(
            f"""SELECT r.*, c.card_number AS card_card_number, j.name AS card_name,
                j.card_type AS card_type, d.name AS door_name, d.alias AS door_alias, d.tt_name AS door_tt_name
                FROM wb_lock_records r LEFT JOIN wb_cards c ON c.id = r.card_id
                LEFT JOIN wb_jobs j ON j.id = c.job_id
                LEFT JOIN wb_doors d ON d.lock_id = r.lock_id
                WHERE {' AND '.join(where or ['1'])} ORDER BY r.at DESC, r.id DESC LIMIT ?""",
            (*params, max(1, min(limit, 500))))]

    def last_lock_record(self) -> float | None:
        """When the last record was stored (the newest row, not the newest event)."""
        row = self.conn.execute(
            "SELECT received_at FROM wb_lock_records ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def cards_last_used(self, card_ids) -> dict[int, dict]:
        """{card ID: its newest lock record ("at", "success", "lock_id" and the door's names)},
        for the cards that have one."""
        ids = sorted({i for i in card_ids if isinstance(i, int) and not isinstance(i, bool)})
        found = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            for r in self.conn.execute(
                    f"""SELECT r.card_id, MAX(r.at) AS at, r.success, r.lock_id,
                        d.name AS door_name, d.alias AS door_alias, d.tt_name AS door_tt_name
                        FROM wb_lock_records r LEFT JOIN wb_doors d ON d.lock_id = r.lock_id
                        WHERE r.card_id IN ({', '.join('?' * len(chunk))}) GROUP BY r.card_id""",
                    chunk):
                found[r["card_id"]] = dict(r)
        return found
