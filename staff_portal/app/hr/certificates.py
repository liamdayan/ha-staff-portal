"""Certificates in the HR portal: Oregon Food Handler Cards, OLCC Service Permits, food manager
certificates and any type HR adds; who needs each, the cards people hold, and what's due.

Types (HR settings): a name, how long it lasts, the days a new hire has to get one, the heads-up
days before it runs out, the message shown while it's lapsed ("May not serve alcohol"), the
departments (Connecteam smart groups) and people who need it, other types accepted in its place
(a food manager certificate for a Food Handler Card). The first setup suggests departments by
their names ("Pub", "Front Desk"); nothing is required until HR confirms them. HR can waive a type
for someone, with a reason and an end date.

Adding or renewing a card: the employee on their phone, a manager or director for someone on their
team, or HR. The photo (or the course's PDF) is cleaned (files.py), a back photo is put under the
front, and it's kept here encrypted while it waits for HR. A reduced copy (2 MB or less) is read by
Google (svc.google().ocr) and ocr_dates suggests the dates. HR checks the photo against the text
read, then confirms (or corrects) the date or rejects the card with a reason. Only a confirmed card
becomes a document (documents.add), so a rejected upload never reaches the person's folder; it's
deleted after 30 days, and so is the text read from a card. While Google isn't set up, HR types
the date. The documents part's delivery queue takes
a confirmed card to Drive, and opens its photo from then on (photo_url). Nothing is copied to
Connecteam (the HR portal only reads from it): Google Drive keeps every card.

Status, one per type someone needs (status_of): valid, expiring (inside the first heads-up), expired,
due (a new hire still inside their days), missing, waiting (for HR) or waived. A type can flag the
person ("May not serve alcohol"): while it's lapsed, or until it's valid.

Every night (daily_jobs, 2:15 AM Pacific; running it again the same day does nothing more):
heads-ups before cards run out; reminders every N days (an HR setting, 2 by default) about lapsed
or missing cards, to the person, their managers and HR; and tidying up.
"""

import asyncio
import csv
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
import weakref
from datetime import date, timedelta

import aiohttp
from aiohttp import web

from . import (access, audit, crypto, documents, files, gdrive, hrdb, notify, ocr_dates,
               people, server, service)
from .access import rule
from .server import PERSON, RELATION, error, read_json, svc_of, viewer_of

log = logging.getLogger("staff_portal")

DAY = 86400
FILE_PURPOSE = "file.certificate"
KEEP_DAYS = 30           # a rejected or withdrawn upload, and the text read from a card, go after this
READ_LIMIT = 2 * 1024 * 1024   # the copy Google reads: it reads files up to about 2 MB best
READ_SIDE = 2000         # ...at most this many pixels on its long side
READ_TRIES = 3           # Google busy or down: tried again after READ_RETRY, then HR types it
READ_RETRY = 300
MAX_TEXT = 20_000        # characters of read text kept
STACK_WIDTH = 2000       # a front and back photo, one above the other, at most this wide
UPLOAD_SECONDS = 120     # a phone on a slow connection sending a 15 MB photo
ADDS_A_DAY = 12          # cards added for one person in a day, at most
MAX_TYPES = 30
REMIND_EVERY = 2         # lapsed or missing: reminded every this many days (HR: 1 to 14)
STATUS_WORDS = {"valid": "Valid", "expiring": "Expiring", "expired": "Expired", "due": "Due",
                "missing": "Missing", "waiting": "Waiting for HR", "waived": "Waived"}
LAPSED = ("expired", "missing")
ORDER = ("expired", "missing", "waiting", "due", "expiring", "valid", "waived")
CARD_WORDS = {"waiting": "Waiting for HR", "confirmed": "Confirmed", "replaced": "Replaced",
              "rejected": "Not accepted", "withdrawn": "Replaced by a newer photo"}
READERS = {"food_handler": "As an Oregon Food Handler Card (3 years)",
           "olcc": "As an OLCC Service Permit (5 years)", "generic": "As another kind of card"}
HOW_WORDS = {"read": "Read from card", "calculated": "Calculated", "not_found": "Not found"}
ADDERS = ("self", "manager", "director", "hr")    # who may add a card for someone
WITHIN = (60, 30, 14, 7)
TIPS = ("Put the card flat on a table.", "Avoid glare on the lamination.",
        "Fill the frame, about 6 to 8 inches (15 to 20 cm) away.")
NO_GOOGLE = "Google Drive isn't set up yet, so the date wasn't read. Type it from the photo."
TOO_BIG = "That file is too big for Google to read. Type the date from it."
LOST = "The photo isn't on this computer any more. Ask them to add the card again."
PDF_BACK = "A PDF can't have a back photo. Put both sides in one PDF instead."
BUSY = "Someone is already deciding on this card."

# The types a new HR portal starts with: key, name, how its dates are read (ocr_dates), years it
# lasts (0: as printed), days a new hire has, heads-up days, the message while it's lapsed,
# flagged until valid (even while due), OLCC extras (temporary permit, under 21), other types
# accepted in its place, and words that suggest the departments needing it (HR confirms).
BUILT_IN = (
    ("food_handler", "Oregon Food Handler Card", "food_handler", 3, 30, [60, 30, 14, 7],
     "May not handle food", 0, 0, ["food_manager"], ["pub"]),
    ("olcc", "OLCC Service Permit", "olcc", 5, 7, [182, 60, 30, 14, 7], "May not serve alcohol",
     1, 1, [], ["pub", "front desk"]),
    ("food_manager", "Food manager certificate", "generic", 0, 30, [60, 30, 14, 7], "", 0, 0, [],
     []),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cert_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    reader TEXT NOT NULL DEFAULT 'generic',   -- how dates are read: food_handler, olcc, generic
    lasts_years INTEGER NOT NULL DEFAULT 0,   -- 0: as printed on the card
    due_days INTEGER NOT NULL DEFAULT 30,     -- days after starting to have one
    remind TEXT NOT NULL DEFAULT '[60, 30, 14, 7]',   -- heads-up days before it runs out, JSON
    lapse_message TEXT NOT NULL DEFAULT '',   -- shown while it's lapsed: "May not serve alcohol"
    flag_until_valid INTEGER NOT NULL DEFAULT 0,   -- ...and while it's due too
    olcc INTEGER NOT NULL DEFAULT 0,          -- asks about a temporary permit and under 21
    accepts TEXT NOT NULL DEFAULT '[]',       -- other types accepted in its place, JSON keys
    groups TEXT NOT NULL DEFAULT '[]',        -- departments (smart group IDs) that need it, JSON
    people TEXT NOT NULL DEFAULT '[]',        -- people who need it too, JSON user IDs
    suggest TEXT NOT NULL DEFAULT '[]',       -- words in department names suggested at first
    groups_confirmed INTEGER NOT NULL DEFAULT 0,   -- 1: HR saved its departments
    active INTEGER NOT NULL DEFAULT 1,        -- 0: no longer tracked (its cards stay)
    position INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
-- Every card added: waiting for HR, then confirmed (a document from then on), replaced by a
-- newer one, rejected, or withdrawn (a newer photo came before HR decided).
CREATE TABLE IF NOT EXISTS certificates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,                  -- Connecteam user ID
    type_key TEXT NOT NULL,
    status TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    upload_id TEXT NOT NULL UNIQUE,           -- its encrypted photo here, until HR decides
    stored INTEGER NOT NULL DEFAULT 1,
    added_by TEXT,
    added_by_name TEXT,
    added_rel TEXT,                           -- self, manager, director or hr
    added_at REAL NOT NULL,
    read_state TEXT NOT NULL,                 -- pending, done, failed or skipped
    read_tries INTEGER NOT NULL DEFAULT 0,
    read_due REAL,
    read_note TEXT,                           -- why it wasn't read, in plain words
    read_text TEXT,                           -- encrypted: the text Google read
    read_expires TEXT,
    read_issued TEXT,
    read_how TEXT,                            -- read, calculated or not_found
    read_warnings TEXT NOT NULL DEFAULT '[]',
    expires TEXT,                             -- YYYY-MM-DD, as HR confirmed it
    issued TEXT,
    number TEXT,                              -- encrypted: the card or permit number
    temporary INTEGER NOT NULL DEFAULT 0,     -- an OLCC temporary permit
    under_21 INTEGER NOT NULL DEFAULT 0,      -- may not mix drinks or bartend
    checked_issuer INTEGER NOT NULL DEFAULT 0,   -- HR checked it with OLCC or the issuer
    corrected INTEGER NOT NULL DEFAULT 0,     -- HR's date differs from the one read
    decided_by TEXT,
    decided_by_name TEXT,
    decided_at REAL,
    reason TEXT,                              -- encrypted: why it wasn't accepted
    document_id INTEGER,                      -- documents.py, once confirmed
    replaced_by INTEGER,
    replaced_at REAL
);
CREATE INDEX IF NOT EXISTS certificates_person ON certificates (person_id, status);
CREATE INDEX IF NOT EXISTS certificates_status ON certificates (status, read_state);
CREATE TABLE IF NOT EXISTS cert_waivers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,
    type_key TEXT NOT NULL,
    reason TEXT NOT NULL,                     -- encrypted
    ends TEXT NOT NULL,                       -- YYYY-MM-DD, the last day it's waived
    set_by TEXT,
    set_by_name TEXT,
    set_at REAL NOT NULL,
    lifted_at REAL,
    lifted_by TEXT
);
CREATE INDEX IF NOT EXISTS cert_waivers_person ON cert_waivers (person_id);
-- Reminders already sent: a heads-up stage per card, the due notice, the last lapse reminder.
CREATE TABLE IF NOT EXISTS cert_reminders (
    person_id TEXT NOT NULL,
    type_key TEXT NOT NULL,
    stage TEXT NOT NULL,
    sent_day TEXT NOT NULL,
    PRIMARY KEY (person_id, type_key, stage)
);
-- Gone since 0.10.4 (the HR portal only reads from Connecteam): cards added in Connecteam's own
-- app, found by a daily look at Connecteam's documents.
DROP TABLE IF EXISTS cert_ct_notes;
"""

notify.register_kind("cert_uploaded", "A card is waiting for you to check.")
notify.register_kind("cert_confirmed", "Your card was confirmed.")
notify.register_kind("cert_rejected", "Your card wasn't accepted. Open it to see why.")
notify.register_kind("cert_due", "You need a card soon.")
notify.register_kind("cert_expiring", "A card runs out soon.")
notify.register_kind("cert_lapsed", "A card has expired or is missing.")
notify.register_kind("cert_team", "Cards need attention.")


# ---------------------------------------------------------------- dates and small helpers

def today(db, now: float | None = None) -> date:
    """Today in HR's time zone (Pacific)."""
    return date.fromisoformat(notify.local_day(db, now))


def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:   # February 29th
        return d.replace(year=d.year + years, day=28)


# Initialisms said letter by letter that start with a vowel sound ("an OLCC permit", "an MSDS").
_VOWEL_LETTERS = set("AEFHILMNORSX")
# Words spelt with a vowel that start with a consonant sound, and the reverse.
_A_WORDS = ("uni", "use", "usu", "uti", "eu", "one", "once", "ure", "uro")
_AN_WORDS = ("hour", "honest", "honor", "honour", "heir")


def a_or_an(name: str) -> str:
    """The name with "a" or "an" before it, by how it sounds: "an OLCC Service Permit", "an
    Oregon Food Handler Card", "a Food Handler Card", "a Universal permit", "an HACCP plan"."""
    name = str(name or "").strip()
    word = re.sub(r"[^A-Za-z0-9]", "", name.split(" ")[0]) if name else ""
    if not word:
        return f"a {name}".strip()
    if word.upper() in ("US", "USA", "UK", "UN", "EU"):    # "you-ess": a consonant sound
        article = "a"
    elif len(word) > 1 and word.isupper() and word.isalpha():
        # An initialism (OLCC, HACCP, MSDS): the first letter's name decides. Real words in
        # capitals (CARD) are rare in certificate names, so they're read as initialisms too.
        article = "an" if word[0] in _VOWEL_LETTERS else "a"
    else:
        low = word.lower()
        if low.startswith(_AN_WORDS):
            article = "an"
        elif low.startswith(_A_WORDS):
            article = "a"
        else:
            article = "an" if low[0] in "aeiou" else "a"
    return f"{article} {name}"


def us(value) -> str:
    """6/14/2029, for messages."""
    d = date.fromisoformat(value) if isinstance(value, str) else value
    return f"{d.month}/{d.day}/{d.year}"


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _list(value) -> list:
    try:
        found = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return []
    return found if isinstance(found, list) else []


def _plain(value, limit: int) -> str:
    """One line of plain text as someone typed it (no control characters)."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).split())[:limit]


def _unseal(value, purpose: str) -> str | None:
    try:
        return crypto.unseal_text(value, purpose)
    except crypto.Damaged:
        return "(damaged)"


def _plural(name: str, n: int) -> str:
    return name if n == 1 or name.endswith("s") else name + "s"


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text[1:2].islower() else text


# ---------------------------------------------------------------- types

def setup(svc) -> None:
    """On start: the tables, and the built-in types the first time."""
    db = svc.db
    db.add_schema(SCHEMA)   # the documents part (started first) has made the "certificates" kind
    now = time.time()
    for position, (key, name, reader, lasts, due, remind, message, until_valid, olcc, accepts,
                   suggest) in enumerate(BUILT_IN):
        db.conn.execute(
            "INSERT OR IGNORE INTO cert_types (key, name, reader, lasts_years, due_days, remind, "
            "lapse_message, flag_until_valid, olcc, accepts, suggest, groups_confirmed, position, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, name, reader, lasts, due, json.dumps(remind), message, until_valid, olcc,
             json.dumps(accepts), json.dumps(suggest), 0 if suggest else 1, position * 10, now))
    db.conn.commit()


def _type(row) -> dict:
    t = dict(row)
    for key in ("remind", "accepts", "groups", "people", "suggest"):
        t[key] = [str(v) if key != "remind" else v for v in _list(t[key])]
    t["remind"] = sorted({v for v in t["remind"] if isinstance(v, int) and not isinstance(v, bool)},
                         reverse=True)
    return t


def types(db, active: bool = True) -> list[dict]:
    where = "WHERE active = 1" if active else ""
    return [_type(r) for r in db.conn.execute(f"SELECT * FROM cert_types {where} ORDER BY position, id")]


def type_by_key(db, key) -> dict | None:
    row = db.conn.execute("SELECT * FROM cert_types WHERE key = ?", (str(key or ""),)).fetchone()
    return _type(row) if row else None


def type_by_id(db, type_id) -> dict | None:
    row = db.conn.execute("SELECT * FROM cert_types WHERE id = ?", (type_id,)).fetchone()
    return _type(row) if row else None


def needed(t: dict, person: dict) -> bool:
    """Whether this person needs this type: one of its departments (once HR confirmed them), or
    named on it."""
    return bool(t["active"]) and (person["id"] in t["people"] or (
        bool(t["groups_confirmed"]) and bool(set(people.groups(person)) & set(t["groups"]))))


def suggested_groups(t: dict, departments: list[dict]) -> list[str]:
    """The departments whose names suggest they need this type ("Pub", "Front Desk")."""
    return [d["group"] for d in departments
            if any(word in str(d["name"]).casefold() for word in t["suggest"])]


def remind_every(db) -> int:
    try:
        return max(1, min(14, int(db.setting("cert_remind_every", str(REMIND_EVERY)))))
    except ValueError:
        return REMIND_EVERY


def clean_type(db, data: dict, current: dict | None, groups: set, person_ids: set) -> dict:
    """A type's fields as HR typed them, checked. Raises ValueError (plain words)."""
    values = {}
    if "name" in data or current is None:
        name = _plain(data.get("name"), 80)
        if not 2 <= len(name) <= 60:
            raise ValueError("A certificate's name is 2 to 60 characters.")
        if db.conn.execute("SELECT 1 FROM cert_types WHERE name = ? COLLATE NOCASE AND id != ?",
                           (name, current["id"] if current else -1)).fetchone():
            raise ValueError("There's already a certificate with that name.")
        values["name"] = name
    if "reader" in data:
        if data["reader"] not in READERS:
            raise ValueError("Choose how its dates are read.")
        values["reader"] = data["reader"]
    for key, lo, hi, what in (("lasts_years", 0, 20, "How long it lasts"),
                              ("due_days", 0, 365, "The days a new hire has")):
        if key in data:
            try:
                number = int(data[key])
            except (TypeError, ValueError):
                raise ValueError(f"{what} must be a number.") from None
            if not lo <= number <= hi:
                raise ValueError(f"{what} must be {lo} to {hi}.")
            values[key] = number
    if "remind" in data:
        raw = data["remind"]
        raw = re.split(r"[,\s]+", raw) if isinstance(raw, str) else raw if isinstance(raw, list) else None
        try:
            days = sorted({int(v) for v in raw if str(v).strip()}, reverse=True) if raw is not None else None
        except (TypeError, ValueError):
            days = None
        if days is None or len(days) > 8 or any(not 1 <= d <= 730 for d in days):
            raise ValueError("Heads-up days are up to 8 numbers from 1 to 730, like 60, 30, 14, 7.")
        values["remind"] = json.dumps(days)
    if "lapse_message" in data:
        values["lapse_message"] = _plain(data["lapse_message"], 80)
    for key in ("flag_until_valid", "olcc", "active"):
        if key in data:
            values[key] = 1 if data[key] in (True, 1, "1") else 0
    if "accepts" in data:
        keys = {r[0] for r in db.conn.execute("SELECT key FROM cert_types")} - (
            {current["key"]} if current else set())
        accepts = data["accepts"] if isinstance(data["accepts"], list) else None
        if accepts is None or not {str(a) for a in accepts} <= keys:
            raise ValueError("Choose the certificates accepted in its place from the list.")
        values["accepts"] = json.dumps(sorted({str(a) for a in accepts}))
    if "groups" in data:
        chosen = data["groups"] if isinstance(data["groups"], list) else None
        if chosen is None or len(chosen) > 200 or not {str(g) for g in chosen} <= groups:
            raise ValueError("Choose departments from the list.")
        values["groups"] = json.dumps(sorted({str(g) for g in chosen}))
        values["groups_confirmed"] = 1
    if "people" in data:
        chosen = data["people"] if isinstance(data["people"], list) else None
        if chosen is None or len(chosen) > 300 or not {str(p) for p in chosen} <= person_ids:
            raise ValueError("Choose people from the list.")
        values["people"] = json.dumps(sorted({str(p) for p in chosen}))
    return values


def add_type(db, values: dict) -> int:
    if db.conn.execute("SELECT COUNT(*) FROM cert_types").fetchone()[0] >= MAX_TYPES:
        raise ValueError(f"There can be at most {MAX_TYPES} kinds of certificate.")
    base = re.sub(r"[^a-z0-9]+", "_", values["name"].lower()).strip("_")[:30] or "certificate"
    key, n = base, 1
    while db.conn.execute("SELECT 1 FROM cert_types WHERE key = ?", (key,)).fetchone():
        n += 1
        key = f"{base}_{n}"
    last = db.conn.execute("SELECT COALESCE(MAX(position), 0) FROM cert_types").fetchone()[0]
    fields = {"reader": "generic", "lasts_years": 0, "due_days": 30, "remind": "[60, 30, 14, 7]",
              "groups_confirmed": 1, **values, "key": key, "position": last + 10,
              "created_at": time.time()}
    cur = db.conn.execute(f"INSERT INTO cert_types ({', '.join(fields)}) VALUES "
                          f"({', '.join('?' * len(fields))})", tuple(fields.values()))
    db.conn.commit()
    return cur.lastrowid


def update_type(db, type_id: int, values: dict) -> None:
    if values:
        sets = ", ".join(f"{k} = ?" for k in values)
        db.conn.execute(f"UPDATE cert_types SET {sets} WHERE id = ?", (*values.values(), type_id))
        db.conn.commit()


# ---------------------------------------------------------------- statuses

def _context(db, person_id=None) -> dict:
    """What statuses are worked out from, read once (for one person, or everyone)."""
    where, params = ("AND person_id = ?", (str(person_id),)) if person_id is not None else ("", ())
    cards, waivers = {}, {}
    for r in db.conn.execute(f"SELECT * FROM certificates WHERE status IN ('waiting', 'confirmed') "
                             f"{where} ORDER BY id", params):
        cards.setdefault(r["person_id"], []).append(dict(r))
    for r in db.conn.execute(f"SELECT * FROM cert_waivers WHERE lifted_at IS NULL {where} "
                             "ORDER BY ends, id", params):
        waivers.setdefault(r["person_id"], []).append(dict(r))
    return {"types": types(db), "cards": cards, "waivers": waivers}


def _statuses(person: dict, on: date, ctx: dict) -> list[dict]:
    cards = ctx["cards"].get(person["id"], [])
    waivers = {w["type_key"]: w for w in ctx["waivers"].get(person["id"], [])
               if w["ends"] >= on.isoformat()}
    found = []
    for t in ctx["types"]:
        required = needed(t, person)
        if required or any(c["type_key"] == t["key"] for c in cards):
            found.append(_status(t, person, cards, waivers.get(t["key"]), on, required))
    return found


def _status(t: dict, person: dict, cards: list[dict], waiver: dict | None, on: date,
            required: bool) -> dict:
    keys = {t["key"], *t["accepts"]}
    held = [c for c in cards if c["status"] == "confirmed" and c["type_key"] in keys and c["expires"]]
    best = max(held, key=lambda c: (c["expires"], c["id"]), default=None)
    waiting = [c for c in cards if c["status"] == "waiting" and c["type_key"] in keys]
    expires = _day(best["expires"]) if best else None
    start = _day(person.get("start_date"))
    due = start + timedelta(days=t["due_days"]) if start else None
    left = (expires - on).days if expires else None
    if expires and left >= 0:
        base = "expiring" if left <= max(t["remind"], default=0) else "valid"
    elif expires:
        base = "expired"
    elif due and on <= due:
        base = "due"
    else:
        base = "missing"
    status = base
    if base not in ("valid", "expiring"):
        if required and waiver:
            status = "waived"
        elif waiting:
            status = "waiting"
    lapsed = base in LAPSED or (t["flag_until_valid"] and base == "due")
    flag = t["lapse_message"] if required and lapsed and status != "waived" else None
    usable = best and base in ("valid", "expiring")
    return {"type_key": t["key"], "name": t["name"], "status": status,
            "status_word": STATUS_WORDS[status], "expires": best["expires"] if best else None,
            "flag": flag or None, "required": required,
            "due": due.isoformat() if due and not expires else None, "days_left": left,
            "card_id": best["id"] if best else None,
            "via": best["type_key"] if best and best["type_key"] != t["key"] else None,
            "waiting_id": waiting[-1]["id"] if waiting else None,
            "temporary": bool(usable and best["temporary"]),
            "under_21": bool(usable and best["under_21"]),
            "waived_until": waiver["ends"] if status == "waived" else None}


def status_of(db, person_id, on: date | None = None) -> list[dict]:
    """The person's certificates: [{type_key, name, status, expires, flag, ...}], one per type
    they need (or hold). status: valid, expiring, expired, due, missing, waiting or waived. flag:
    the type's message while it's lapsed, such as "May not serve alcohol", else None. Also:
    status_word, required, due (a new hire's deadline), days_left, card_id, via (an accepted
    stand-in, such as food_manager), waiting_id, temporary, under_21, waived_until."""
    person = db.person(person_id)
    if not person:
        return []
    return _statuses(person, on or today(db), _context(db, person["id"]))


def _rank(row: dict) -> tuple:
    return (ORDER.index(row["status"]), row["days_left"] if row["days_left"] is not None else 99999,
            row["name"].casefold())


def board(db, viewer: access.Viewer, filters: dict | None = None) -> dict:
    """The compliance board as this viewer may see it (HR: everyone; directors and managers:
    the people they see): {"rows", "banners", "counts", "total", "on"}. A row is a person and a
    type they need: status_of's fields plus person_id, name (theirs) and groups. filters: group,
    type, status (one of them, or "problems": lapsed, waiting or flagged), within (days), on."""
    f = filters or {}
    on = f.get("on") or today(db)
    ctx = _context(db)
    rows = []
    for p, rel in access.visible_people(db, viewer):
        for s in _statuses(p, on, ctx):
            if s["required"]:
                rows.append({**s, "type": s["name"], "person_id": p["id"], "name": p["name"],
                             "groups": people.groups(p)})
    shown = sorted((r for r in rows if _matches(r, f)), key=_rank)
    return {"rows": shown, "banners": banners(rows, ctx["types"]), "total": len(rows),
            "counts": {k: sum(1 for r in rows if r["status"] == k) for k in STATUS_WORDS},
            "on": on.isoformat()}


def _matches(row: dict, f: dict) -> bool:
    if f.get("group") and str(f["group"]) not in row["groups"]:
        return False
    if f.get("type") and row["type_key"] != f["type"]:
        return False
    status = f.get("status")
    if status == "problems" and not (row["flag"] or row["status"] in (*LAPSED, "waiting")):
        return False
    if status in STATUS_WORDS and row["status"] != status:
        return False
    within = f.get("within")
    if within and not (row["days_left"] is not None and 0 <= row["days_left"] <= within):
        return False
    return True


def banners(rows: list[dict], types_: list[dict]) -> list[str]:
    """For a team or the whole place: "2 OLCC Service Permits expired: may not serve alcohol"
    (expired first, then missing, waiting for HR and due)."""
    found = []
    for status, word in (("expired", "expired"), ("missing", "missing"),
                         ("waiting", "waiting for HR"), ("due", "not in yet")):
        for t in types_:
            flagged = [r for r in rows if r["type_key"] == t["key"] and r["status"] == status and r["flag"]]
            if flagged:
                n = len(flagged)
                found.append(f"{n} {_plural(t['name'], n)} {word}: {_lower_first(flagged[0]['flag'])}")
    return found


def board_csv(rows: list[dict], names: dict) -> str:
    """The board as a spreadsheet (CSV). Cells that a spreadsheet would run as a formula are
    made plain text."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Name", "Departments", "Certificate", "Status", "Expires", "Due by", "Flag",
                "Temporary permit", "Under 21", "Waived until"])
    for r in rows:
        w.writerow([_cell(v) for v in (
            r["name"], ", ".join(names.get(g, f"Group {g}") for g in r["groups"]), r["type"],
            r["status_word"], r["expires"] or "", r["due"] or "", r["flag"] or "",
            "Yes" if r["temporary"] else "", "Yes" if r["under_21"] else "",
            r["waived_until"] or "")])
    return out.getvalue()


def _cell(value) -> str:
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


# ---------------------------------------------------------------- the photos kept here

def store_dir(folder: str) -> str:
    return os.path.join(folder, "certificates")


def _path(folder: str, card: dict) -> str:
    return os.path.join(store_dir(folder), f"{card['upload_id']}.bin")


async def _save(folder: str, upload_id: str, data: bytes) -> None:
    sealed = await crypto.seal_async(data, FILE_PURPOSE)
    await asyncio.to_thread(crypto.write_private, os.path.join(store_dir(folder), f"{upload_id}.bin"),
                            sealed)


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def _load(folder: str, card: dict) -> bytes | None:
    if not card["stored"]:
        return None
    try:
        return await crypto.unseal_async(await asyncio.to_thread(_read_file, _path(folder, card)),
                                         FILE_PURPOSE)
    except (OSError, crypto.Damaged):
        return None


def _drop(db, folder: str, card: dict) -> None:
    try:
        os.remove(_path(folder, card))
    except FileNotFoundError:
        pass
    db.conn.execute("UPDATE certificates SET stored = 0 WHERE id = ?", (card["id"],))
    db.conn.commit()


async def prepare(front: bytes, back: bytes | None = None) -> tuple[bytes, str]:
    """The card made safe (files.clean_upload): a photo re-encoded without metadata, or the PDF
    byte for byte. A back photo goes under the front, so the card is one picture everywhere
    (Drive, and the copy Google reads). Raises files.FileRefused (Apple's HEIC:
    retake the photo)."""
    data, mime = await files.clean_upload(front, files.PHOTO_LIMIT)
    if back is None:
        return data, mime
    other, other_mime = await files.clean_upload(back, files.PHOTO_LIMIT)
    if files.MIME["pdf"] in (mime, other_mime):
        raise files.FileRefused(PDF_BACK)
    async with files.image_slots():
        return await asyncio.to_thread(_stack, data, other)


def _stack(front: bytes, back: bytes) -> tuple[bytes, str]:
    from PIL import Image
    with Image.open(io.BytesIO(front)) as a, Image.open(io.BytesIO(back)) as b:
        width = min(max(a.width, b.width), STACK_WIDTH)
        parts = [im.convert("RGB").resize((width, max(1, round(im.height * width / im.width))))
                 for im in (a, b)]
    gap = 24
    sheet = Image.new("RGB", (width, parts[0].height + gap + parts[1].height), "white")
    sheet.paste(parts[0], (0, 0))
    sheet.paste(parts[1], (0, parts[0].height + gap))
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=90)
    return files.clean_image(out.getvalue())


def reduced(data: bytes, mime: str) -> tuple[bytes, str] | None:
    """The copy Google reads, 2 MB or less: a smaller JPEG, or the PDF as it is when it's small
    enough (None: too big to read)."""
    if mime == files.MIME["pdf"]:
        return (data, mime) if len(data) <= READ_LIMIT else None
    from PIL import Image
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
    side, quality = READ_SIDE, 85
    for _ in range(6):
        copy = img.copy()
        copy.thumbnail((side, side))
        out = io.BytesIO()
        copy.save(out, "JPEG", quality=quality)
        if out.tell() <= READ_LIMIT:
            return out.getvalue(), files.MIME["jpeg"]
        side, quality = int(side * 0.75), max(60, quality - 8)
    return None


def _turned(data: bytes, turn: int) -> bytes:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB").rotate(-turn, expand=True)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue()


# ---------------------------------------------------------------- cards

def get(db, card_id) -> dict | None:
    try:
        cid = int(card_id)
    except (TypeError, ValueError):
        return None
    row = db.conn.execute("SELECT * FROM certificates WHERE id = ?", (cid,)).fetchone()
    return dict(row) if row else None


def _google(svc):
    """The Google Drive bridge, when it's set up with a drive chosen (else None)."""
    bridge = svc.google()
    return bridge if bridge is not None and svc.main_settings().get("hr_google_drive") else None


def _hr_admins(db, but=None) -> list[str]:
    found = []
    for pid in db.with_role(access.HR_ADMIN):
        p = db.person(pid)
        if p and not p["archived"] and pid != str(but):
            found.append(pid)
    return found


async def add_card(svc, person: dict, t: dict, data: bytes, mime: str, viewer: access.Viewer,
                   rel: str) -> dict:
    """A cleaned card (prepare) for someone, waiting for HR. Its date is read in the background
    when Google is set up. An older card of this type still waiting is withdrawn: the newest
    counts. Returns the card."""
    upload_id = secrets.token_urlsafe(24)
    await _save(svc.folder, upload_id, data)
    db, now = svc.db, time.time()
    if _google(svc) is None:
        state, note = "skipped", NO_GOOGLE
    elif mime == files.MIME["pdf"] and len(data) > READ_LIMIT:
        state, note = "skipped", TOO_BIG
    else:
        state, note = "pending", None
    deciding = _busy.get(svc, set())   # a card HR is confirming right now stays as it is
    older = [dict(r) for r in db.conn.execute(
        "SELECT * FROM certificates WHERE person_id = ? AND type_key = ? AND status = 'waiting'",
        (person["id"], t["key"])) if r["id"] not in deciding]
    cur = db.conn.execute(
        "INSERT INTO certificates (person_id, type_key, status, mime, size, upload_id, added_by, "
        "added_by_name, added_rel, added_at, read_state, read_due, read_note) VALUES (?, ?, "
        "'waiting', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (person["id"], t["key"], mime, len(data), upload_id, viewer.id, viewer.name, rel, now,
         state, now if state == "pending" else None, note))
    db.conn.commit()
    card_id = cur.lastrowid
    for old in older:
        db.conn.execute("UPDATE certificates SET status = 'withdrawn', read_text = NULL, "
                        "replaced_by = ?, replaced_at = ? WHERE id = ?", (card_id, now, old["id"]))
        db.conn.commit()
        if not documents.on_hold(db, person["id"]):   # a legal hold keeps every photo
            _drop(db, svc.folder, old)
    audit.record(db, "Added a certificate", actor_id=viewer.id, actor=viewer.name,
                 target_id=person["id"], target=person["name"], detail=t["name"])
    if rel != "hr":
        for admin in _hr_admins(db, but=person["id"]):
            notify.notify(admin, "cert_uploaded", db=db, once=f"cert-added-{card_id}",
                          title=f"{person['name']}'s {t['name']} is waiting for you to check.",
                          link=f"#/certificates/check/{person['id']}/{card_id}")
    if state == "pending":
        kick(svc)
    return get(db, card_id)


def pick(text: str, t: dict, on: date, name: str | None) -> dict:
    """The dates on a card (ocr_dates), with the expiry worked out from the issue date for types
    that last a set time but aren't read as one of the known cards."""
    found = ocr_dates.pick_dates(text, t["reader"], on, name)
    if not found["expires"] and found["issued"] and t["lasts_years"] and t["reader"] == "generic":
        found["expires"] = add_years(found["issued"], t["lasts_years"])
        found["how"] = "calculated"
    return found


async def read_card(svc, card: dict, now: float | None = None) -> str:
    """Read one card's dates with Google. Returns its read state (done, pending again, failed or
    skipped). Never raises for Google's problems: HR types the date instead."""
    db = svc.db
    now = time.time() if now is None else now
    tries = card["read_tries"] + 1

    def finish(state, note=None, **fields):
        fields.update(read_state=state, read_note=note, read_tries=tries,
                      read_due=now + READ_RETRY if state == "pending" else None)
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.conn.execute(f"UPDATE certificates SET {sets} WHERE id = ? AND status = 'waiting'",
                        (*fields.values(), card["id"]))
        db.conn.commit()
        return state

    bridge = _google(svc)
    if bridge is None:
        return finish("skipped", NO_GOOGLE)
    data = await _load(svc.folder, card)
    if data is None:
        return finish("failed", LOST)
    try:
        async with files.image_slots():
            small = await asyncio.to_thread(reduced, data, card["mime"])
    except Exception:  # noqa: BLE001 - a picture that can't be shrunk isn't read
        small = None
    if small is None:
        return finish("skipped", TOO_BIG)
    try:
        text = await bridge.ocr(*small)
    except gdrive.BridgeError as err:
        if err.code in ("unavailable", "quota") and tries < READ_TRIES:
            return finish("pending", "Google is busy: it will be read again in a few minutes.")
        return finish("failed", f"Google couldn't read it: {err.message} Type the date from the photo.")
    except ValueError as err:
        return finish("failed", f"Google couldn't read it: {err} Type the date from the photo.")
    if not svc.running:
        return "pending"
    t = type_by_key(db, card["type_key"]) or {"reader": "generic", "lasts_years": 0}
    person = db.person(card["person_id"])
    found = pick(text, t, today(db, now), person["name"] if person else None)
    return finish("done", None, read_text=crypto.seal_text(text[:MAX_TEXT], "certificates.read"),
                  read_expires=found["expires"] and found["expires"].isoformat(),
                  read_issued=found["issued"] and found["issued"].isoformat(),
                  read_how=found["how"], read_warnings=json.dumps(found["warnings"][:10]))


_readers: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def kick(svc) -> None:
    """Read waiting cards now, in the background (one reader per running HR portal)."""
    if not svc.running:
        return
    state = _readers.setdefault(svc, {"task": None, "again": False})
    if state["task"] is not None and not state["task"].done():
        state["again"] = True
        return
    state["again"] = False
    state["task"] = asyncio.get_running_loop().create_task(_reader(svc, state))


async def _reader(svc, state) -> None:
    try:
        while svc.running:
            row = svc.db.conn.execute(
                "SELECT * FROM certificates WHERE status = 'waiting' AND read_state = 'pending' AND "
                "read_due <= ? ORDER BY read_due, id LIMIT 1", (time.time(),)).fetchone()
            if row is None:
                if not state["again"]:
                    break
                state["again"] = False
                continue
            await read_card(svc, dict(row))
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001 - looked at again next tick
        log.warning("HR portal: reading a card failed: %r", err)


async def settle(svc) -> None:
    """Wait until the reader is done (tests)."""
    state = _readers.get(svc)
    while state and state["task"] is not None and not state["task"].done():
        await asyncio.shield(state["task"])


async def _stop(svc) -> None:
    state = _readers.pop(svc, None)
    if state and state["task"] is not None and not state["task"].done():
        state["task"].cancel()
        try:
            await state["task"]
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - stopping regardless
            pass
    _busy.pop(svc, None)


_busy: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()   # cards being decided


def clean_confirm(data: dict, t: dict, on: date) -> dict:
    """What HR confirms: the expiry date (required), issue date, number and the ticks. Raises
    ValueError (plain words)."""
    expires = _day(data.get("expires"))
    if not expires:
        raise ValueError("Type the expiry date, like 2029-06-14.")
    if not add_years(on, -10) <= expires <= add_years(on, (t["lasts_years"] or 10) + 1):
        raise ValueError("That expiry date doesn't look right. Check it.")
    issued = None
    if str(data.get("issued") or "").strip():
        issued = _day(data.get("issued"))
        if not issued or issued > on or issued >= expires:
            raise ValueError("The issue date must be a date before the expiry date, and not in "
                             "the future.")
    number = _plain(data.get("number"), 40)
    if number and not re.fullmatch(r"[A-Za-z0-9 #./-]{1,40}", number):
        raise ValueError("A card number is letters, numbers and dashes.")
    tick = lambda k: data.get(k) in (True, 1, "1")   # noqa: E731
    return {"expires": expires, "issued": issued, "number": number or None,
            "temporary": tick("temporary") and bool(t["olcc"]),
            "under_21": tick("under_21") and bool(t["olcc"]), "checked_issuer": tick("checked_issuer")}


async def confirm(svc, card: dict, values: dict, viewer: access.Viewer) -> dict:
    """HR confirmed a waiting card (clean_confirm's values): it becomes a document on its way to
    Google Drive, the card it replaces is marked replaced, reminders start again, and the person is told.
    Raises ValueError."""
    db = svc.db
    busy = _busy.setdefault(svc, set())
    if card["id"] in busy:
        raise ValueError(BUSY)
    busy.add(card["id"])
    try:
        person = db.person(card["person_id"])
        t = type_by_key(db, card["type_key"])
        kind = documents.kind_by_key(db, documents.CERTIFICATES)
        if not person or not t or not kind:
            raise ValueError("That card can't be confirmed any more.")
        data = await _load(svc.folder, card)
        if data is None:
            raise ValueError(LOST)
        on = today(db)
        expires = values["expires"]
        doc = await documents.add(svc, person, kind, data, card["mime"],
                                  f"{t['name']} - expires {expires.isoformat()}", on.isoformat(),
                                  viewer, "hr")
        now = time.time()
        db.conn.execute("UPDATE certificates SET status = 'replaced', replaced_by = ?, replaced_at = ? "
                        "WHERE person_id = ? AND type_key = ? AND status = 'confirmed'",
                        (card["id"], now, person["id"], t["key"]))
        db.conn.execute(
            "UPDATE certificates SET status = 'confirmed', expires = ?, issued = ?, number = ?, "
            "temporary = ?, under_21 = ?, checked_issuer = ?, corrected = ?, decided_by = ?, "
            "decided_by_name = ?, decided_at = ?, document_id = ?, read_text = NULL WHERE id = ?",
            (expires.isoformat(), values["issued"] and values["issued"].isoformat(),
             crypto.seal_text(values["number"], "certificates.number"),
             int(values["temporary"]), int(values["under_21"]), int(values["checked_issuer"]),
             int(bool(card["read_expires"]) and card["read_expires"] != expires.isoformat()),
             viewer.id, viewer.name, now, doc["id"], card["id"]))
        # Reminders start again: the next lapse is told at once.
        db.conn.execute("DELETE FROM cert_reminders WHERE person_id = ? AND type_key = ? AND "
                        "stage IN ('lapse', 'due')", (person["id"], t["key"]))
        db.conn.commit()
        _drop(db, svc.folder, card)   # the document keeps its own encrypted copy
        detail = f"{t['name']}: expires {expires.isoformat()}"
        if card["read_expires"] and card["read_expires"] != expires.isoformat():
            detail += " (corrected)"
        if values["checked_issuer"]:
            detail += ", checked with the issuer"
        audit.record(db, "Confirmed a certificate", actor_id=viewer.id, actor=viewer.name,
                     target_id=person["id"], target=person["name"], detail=detail)
        notify.notify(person["id"], "cert_confirmed", db=db, once=f"cert-confirmed-{card['id']}",
                      title=f"Your {t['name']} was confirmed. It expires on {us(expires)}.",
                      link="#/my-certificates")
        return get(db, card["id"])
    finally:
        busy.discard(card["id"])


def reject(svc, card: dict, reason: str, viewer: access.Viewer) -> dict:
    """HR didn't accept a waiting card: the person (and whoever added it for them) is told why,
    and the photo goes after 30 days. It never reaches their folder."""
    db = svc.db
    if card["id"] in _busy.get(svc, set()):
        raise ValueError(BUSY)
    now = time.time()
    db.conn.execute("UPDATE certificates SET status = 'rejected', reason = ?, decided_by = ?, "
                    "decided_by_name = ?, decided_at = ?, read_text = NULL WHERE id = ? AND "
                    "status = 'waiting'", (crypto.seal_text(reason, "certificates.reason"), viewer.id,
                                           viewer.name, now, card["id"]))
    db.conn.commit()
    person = db.person(card["person_id"])
    t = type_by_key(db, card["type_key"])
    name = t["name"] if t else "card"
    audit.record(db, "Didn't accept a certificate", actor_id=viewer.id, actor=viewer.name,
                 target_id=card["person_id"], target=person and person["name"], detail=name)
    notify.notify(card["person_id"], "cert_rejected", db=db, once=f"cert-rejected-{card['id']}",
                  title=f"Your {name} wasn't accepted. Open it to see why.", link="#/my-certificates")
    if card["added_rel"] in ("manager", "director") and card["added_by"] != card["person_id"]:
        notify.notify(card["added_by"], "cert_rejected", db=db, once=f"cert-rejected-{card['id']}",
                      title=f"The {name} you added for {person['name'] if person else 'someone'} "
                            "wasn't accepted.",
                      link=f"#/people/{card['person_id']}")
    return get(db, card["id"])


def add_waiver(db, person: dict, t: dict, reason: str, ends: date, viewer: access.Viewer) -> int:
    cur = db.conn.execute(
        "INSERT INTO cert_waivers (person_id, type_key, reason, ends, set_by, set_by_name, set_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", (person["id"], t["key"], crypto.seal_text(reason, "certificates.waiver"),
                                         ends.isoformat(), viewer.id, viewer.name, time.time()))
    db.conn.commit()
    audit.record(db, "Waived a certificate", actor_id=viewer.id, actor=viewer.name,
                 target_id=person["id"], target=person["name"],
                 detail=f"{t['name']} until {ends.isoformat()}")
    return cur.lastrowid


def end_waiver(db, person: dict, waiver_id: int, viewer: access.Viewer) -> bool:
    row = db.conn.execute("SELECT * FROM cert_waivers WHERE id = ? AND person_id = ? AND "
                          "lifted_at IS NULL", (waiver_id, person["id"])).fetchone()
    if not row:
        return False
    db.conn.execute("UPDATE cert_waivers SET lifted_at = ?, lifted_by = ? WHERE id = ?",
                    (time.time(), viewer.id, waiver_id))
    db.conn.commit()
    t = type_by_key(db, row["type_key"])
    audit.record(db, "Ended a certificate waiver", actor_id=viewer.id, actor=viewer.name,
                 target_id=person["id"], target=person["name"], detail=t["name"] if t else row["type_key"])
    return True


# ---------------------------------------------------------------- the nightly run

def _managers(db) -> list[access.Viewer]:
    found = []
    for pid in db.with_role(access.MANAGER):
        p = db.person(pid)
        if p and not p["archived"]:
            found.append(access.viewer_for(db, p))
    return found


def reminders(db, on: date) -> dict:
    """Heads-ups before cards run out (the most recent stage only, once per card), reminders
    every N days about lapsed or missing cards, and a new hire's due notice. The person is told
    about their own cards; managers and HR get one message each for all of theirs. Running it
    again the same day sends nothing more. Returns counts."""
    every, ctx = remind_every(db), _context(db)
    by_key = {t["key"]: t for t in ctx["types"]}
    roles, managers, admins = db.all_roles(), _managers(db), _hr_admins(db)
    lapsed_for, ahead_for = {}, {}
    counts = {"lapsed": 0, "ahead": 0, "due": 0}
    day = on.isoformat()

    def sent(pid, key, stage):
        return db.conn.execute("SELECT sent_day FROM cert_reminders WHERE person_id = ? AND "
                               "type_key = ? AND stage = ?", (pid, key, stage)).fetchone()

    def mark(pid, key, stage):
        db.conn.execute("INSERT OR REPLACE INTO cert_reminders (person_id, type_key, stage, sent_day) "
                        "VALUES (?, ?, ?, ?)", (pid, key, stage, day))

    for p in db.people(archived=False):
        bosses = [m.id for m in managers if m.id != p["id"] and
                  access.relation(m, p, roles.get(p["id"], set())) == "manager"]
        staff = bosses + [a for a in admins if a != p["id"]]
        for s in _statuses(p, on, ctx):
            if not s["required"]:
                continue
            t, key = by_key[s["type_key"]], s["type_key"]
            if s["status"] in LAPSED:
                last = sent(p["id"], key, "lapse")
                if last is None or (on - date.fromisoformat(last[0])).days >= every:
                    mark(p["id"], key, "lapse")
                    counts["lapsed"] += 1
                    title = (f"Your {t['name']} expired on {us(s['expires'])}. Add your new one in "
                             "the HR portal." if s["status"] == "expired" else
                             f"You need {a_or_an(t['name'])}. Add it in the HR portal.")
                    notify.notify(p["id"], "cert_lapsed", urgent=True, db=db, title=title,
                                  link="#/my-certificates", once=f"cert-lapse-{key}-{day}")
                    for pid in staff:
                        lapsed_for[pid] = lapsed_for.get(pid, 0) + 1
            elif s["status"] not in ("waiting", "waived"):
                db.conn.execute("DELETE FROM cert_reminders WHERE person_id = ? AND type_key = ? "
                                "AND stage = 'lapse'", (p["id"], key))
            if s["status"] in ("valid", "expiring") and s["card_id"]:
                stage = min((d for d in t["remind"] if s["days_left"] <= d), default=None)
                if stage is not None and not sent(p["id"], key, f"ahead{stage}:{s['card_id']}"):
                    mark(p["id"], key, f"ahead{stage}:{s['card_id']}")
                    counts["ahead"] += 1
                    notify.notify(p["id"], "cert_expiring", db=db, link="#/my-certificates",
                                  title=f"Your {t['name']} runs out on {us(s['expires'])}.",
                                  once=f"cert-ahead-{s['card_id']}-{stage}")
                    for pid in staff:
                        ahead_for[pid] = ahead_for.get(pid, 0) + 1
            if s["status"] == "due" and s["due"] and not sent(p["id"], key, "due"):
                mark(p["id"], key, "due")
                counts["due"] += 1
                notify.notify(p["id"], "cert_due", db=db, link="#/my-certificates",
                              title=f"You need {a_or_an(t['name'])} by {us(s['due'])}. Add it in the "
                                    "HR portal.", once=f"cert-due-{key}-{s['due']}")
    db.conn.commit()
    for pid, n in lapsed_for.items():
        notify.notify(pid, "cert_team", urgent=True, db=db, once=f"cert-lapsed-{day}",
                      link="#/certificates?status=problems",
                      title=f"{n} certificate{'s are' if n != 1 else ' is'} expired or missing. "
                            "See the certificate board.")
    for pid, n in ahead_for.items():
        notify.notify(pid, "cert_expiring", db=db, once=f"cert-ahead-{day}",
                      link="#/certificates?within=60",
                      title=f"{n} certificate{'s' if n != 1 else ''} run{'' if n != 1 else 's'} out "
                            "soon. See the certificate board.")
    return counts


def tidy(svc, now: float | None = None) -> dict:
    """Photos of rejected or withdrawn cards go after 30 days (not while the person is under a
    legal hold), and so does the text read from a card still waiting; files no card points to go
    after a day. Returns counts."""
    db, folder = svc.db, svc.folder
    now = time.time() if now is None else now
    counts = {"photos": 0, "texts": 0, "strays": 0}
    for card in [dict(r) for r in db.conn.execute(
            "SELECT * FROM certificates WHERE status IN ('rejected', 'withdrawn') AND stored = 1 AND "
            "COALESCE(decided_at, replaced_at, added_at) < ?", (now - KEEP_DAYS * DAY,))]:
        if documents.on_hold(db, card["person_id"]):
            continue   # kept while a legal hold is on, and tidied once it's lifted
        _drop(db, folder, card)
        counts["photos"] += 1
    counts["texts"] = db.conn.execute(
        "UPDATE certificates SET read_text = NULL WHERE read_text IS NOT NULL AND added_at < ?",
        (now - KEEP_DAYS * DAY,)).rowcount
    db.conn.commit()
    try:
        names = os.listdir(store_dir(folder))
    except FileNotFoundError:
        names = []
    known = {f"{r[0]}.bin" for r in db.conn.execute("SELECT upload_id FROM certificates WHERE stored = 1")}
    for name in names:
        path = os.path.join(store_dir(folder), name)
        try:
            if name not in known and now - os.path.getmtime(path) > DAY:
                os.remove(path)
                counts["strays"] += 1
        except OSError:
            pass
    return counts


async def nightly(svc, now: float | None = None) -> None:
    """The nightly run's part for certificates (daily_jobs)."""
    db = svc.db
    reminders(db, today(db, now))
    tidy(svc, now)


async def tick(svc, now: float) -> None:
    """Every half minute: read cards waiting to be read."""
    if svc.db.conn.execute("SELECT 1 FROM certificates WHERE status = 'waiting' AND read_state = "
                           "'pending' AND read_due <= ? LIMIT 1", (now,)).fetchone():
        kick(svc)


# ---------------------------------------------------------------- migration files

def migration_blocker(svc) -> str | None:
    """Cards waiting for HR live only on this computer (a migration file doesn't carry them):
    a migration waits until HR has decided on them. Checked while HR is off too."""
    if svc.running:
        return _blocker(svc.db.conn)
    path = os.path.join(svc.folder, hrdb.DB_FILE)
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{urllib.parse.quote(path)}?mode=ro", uri=True,
                               timeout=hrdb.BUSY_SECONDS)
        try:
            return _blocker(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _blocker(conn) -> str | None:
    try:
        n = conn.execute("SELECT COUNT(*) FROM certificates WHERE status = 'waiting' AND stored = 1"
                         ).fetchone()[0]
    except sqlite3.OperationalError:   # this part never ran here
        return None
    if not n:
        return None
    return (f"{n} certificate{' is' if n == 1 else 's are'} waiting for HR to confirm "
            f"{'it' if n == 1 else 'them'} in the HR portal, and {'its photo is' if n == 1 else 'their photos are'} "
            "only on this computer. Once HR has, try again.")


def _part(svc) -> None:
    async def daily(s):
        await nightly(s)
    svc.on_start.append(setup)
    svc.daily_jobs.append(daily)
    svc.tick_jobs.append(tick)
    svc.on_stop.append(_stop)
    svc.migration_blockers.append(migration_blocker)


service.PARTS.append(_part)
documents.OWNED[documents.CERTIFICATES] = "certificates"


# ---------------------------------------------------------------- what people see

def photo_url(db, c: dict, rel: str, viewer=None) -> str | None:
    """Where the person or HR opens a card's photo: here while it's only here (waiting for HR,
    or turned down), then the document it became (documents.view_url: the documents part
    fetches it from Drive when needed, and logs every look)."""
    if c["stored"]:
        return f"/api/people/{c['person_id']}/certificates/{c['id']}/photo"
    doc = documents.get(db, c["document_id"]) if c["document_id"] else None
    k = documents.kind(db, doc["kind_id"]) if doc else None
    return documents.view_url(doc) if doc and documents.may_open(viewer, rel, doc, k) else None


def card_view(db, c: dict, rel: str, viewer=None) -> dict:
    """A card as this relation may see it: the photo, number and read text only for the person
    and HR (managers and directors see statuses and dates)."""
    t = type_by_key(db, c["type_key"])
    out = {"id": c["id"], "type_key": c["type_key"], "type": t["name"] if t else c["type_key"],
           "status": c["status"], "status_word": CARD_WORDS.get(c["status"], c["status"]),
           "added_at": round(c["added_at"]), "added_by": c["added_by_name"],
           "expires": c["expires"], "issued": c["issued"], "temporary": bool(c["temporary"]),
           "under_21": bool(c["under_21"]), "read_state": c["read_state"],
           "read_expires": c["read_expires"], "read_how": c["read_how"],
           "how_word": HOW_WORDS.get(c["read_how"] or ""), "read_note": c["read_note"],
           "decided_at": c["decided_at"] and round(c["decided_at"])}
    if c["status"] == "rejected":
        out["reason"] = _unseal(c["reason"], "certificates.reason")
    if rel in ("self", "hr"):
        out["number"] = _unseal(c["number"], "certificates.number")
        if photo := photo_url(db, c, rel, viewer):
            out.update(photo=photo, pdf=c["mime"] == files.MIME["pdf"])
    return out


def person_view(svc, v: access.Viewer, p: dict, rel: str) -> dict:
    """A person's certificates as the viewer may see them: statuses, recent cards, waivers, and
    what they can do."""
    db = svc.db
    statuses = status_of(db, p["id"])
    cards = [card_view(db, dict(r), rel, v) for r in db.conn.execute(
        "SELECT * FROM certificates WHERE person_id = ? AND status != 'withdrawn' ORDER BY id DESC "
        "LIMIT 20", (p["id"],))]
    waivers = []
    for w in db.conn.execute("SELECT * FROM cert_waivers WHERE person_id = ? AND lifted_at IS NULL "
                             "AND ends >= ? ORDER BY ends", (p["id"], today(db).isoformat())):
        t = type_by_key(db, w["type_key"])
        item = {"id": w["id"], "type": t["name"] if t else w["type_key"], "ends": w["ends"]}
        if rel == "hr":   # why: HR only (it may be personal)
            item.update(reason=_unseal(w["reason"], "certificates.waiver"), set_by=w["set_by_name"])
        waivers.append(item)
    return {"person": {"id": p["id"], "name": p["name"]}, "relation": rel, "statuses": statuses,
            "cards": cards, "waivers": waivers,
            "types": [{"key": t["key"], "name": t["name"], "olcc": bool(t["olcc"])} for t in types(db)],
            "can_add": rel in ADDERS and not p["archived"], "can_check": rel == "hr",
            "can_waive": rel == "hr", "reading": _google(svc) is not None, "tips": list(TIPS)}


@server.section("certificates", 30)
def certificates_section(svc, v: access.Viewer, p: dict, rel: str) -> dict | None:
    """On a person's page, when they need a certificate or have one on file."""
    view = person_view(svc, v, p, rel)
    if not view["statuses"] and not view["cards"]:
        return None
    rows = [{"label": s["name"], "value": s["status_word"] + (f", expires {us(s['expires'])}"
                                                              if s["expires"] else "")}
            for s in view["statuses"]]
    notes = sorted({s["flag"] for s in view["statuses"] if s["flag"]})
    return {"title": "Certificates", "rows": rows, "notes": notes, "certs": view}


@server.todo
def certificate_todos(svc, v: access.Viewer) -> list[dict]:
    db = svc.db
    found = []
    for s in status_of(db, v.id):
        if s["required"] and s["status"] in LAPSED:
            found.append({"text": f"Your {s['name']} {'expired' if s['status'] == 'expired' else 'is missing'}"
                                  ". Add your new one.", "link": "#/my-certificates"})
        elif s["required"] and s["status"] == "due":
            found.append({"text": f"You need {a_or_an(s['name'])} by {us(s['due'])}.", "link": "#/my-certificates"})
    if v.is_hr:
        waiting = db.conn.execute("SELECT COUNT(*) FROM certificates WHERE status = 'waiting' AND "
                                  "person_id != ?", (v.id,)).fetchone()[0]
        if waiting:
            found.append({"text": f"{waiting} certificate{'s' if waiting != 1 else ''} waiting for "
                                  "you to check.", "link": "#/certificates/check"})
        open_types = [t["name"] for t in types(db) if t["suggest"] and not t["groups_confirmed"]]
        if open_types:
            found.append({"text": "Confirm which departments need the " + " and the ".join(open_types)
                                  + " (HR settings, Certificates).", "link": "#/settings"})
    if v.is_staff:
        problems = [r for r in board(db, v)["rows"] if r["person_id"] != v.id and
                    (r["status"] in LAPSED or r["flag"])]
        if problems:
            n = len(problems)
            found.append({"text": f"{n} certificate{'s' if n != 1 else ''} expired, missing or "
                                  "flagged" + (" on your team" if not v.is_hr and not v.is_director
                                               else "") + ".",
                          "link": "#/certificates?status=problems"})
    return found


@server.settings_section("certificates", 30)
def certificates_settings(svc, v: access.Viewer) -> dict:
    db = svc.db
    departments = server.departments_list(svc)
    shown = []
    for t in types(db, active=False):
        shown.append({k: t[k] for k in (
            "id", "key", "name", "reader", "lasts_years", "due_days", "remind", "lapse_message",
            "flag_until_valid", "olcc", "accepts", "groups", "people", "groups_confirmed",
            "active")} | {"suggested": suggested_groups(t, departments)
                                         if not t["groups_confirmed"] else []})
    return {"types": shown, "departments": departments, "readers": READERS,
            "people": [{"id": p["id"], "name": p["name"]} for p in db.people(archived=False)],
            "remind_every": remind_every(db)}


# ---------------------------------------------------------------- routes

async def _read_part(part, limit: int) -> bytes:
    data = bytearray()
    while chunk := await part.read_chunk(64 * 1024):
        data.extend(chunk)
        if len(data) > limit:
            raise files.FileRefused(f"That file is too big (more than {limit // 2**20} MB).")
    return bytes(data)


async def _read_form(request: web.Request) -> dict:
    """An added card's form: type, front and an optional back, each capped."""
    if not (request.content_type or "").startswith("multipart/"):
        raise files.FileRefused("Choose a photo or PDF of the card.")
    found = {}
    try:
        async with asyncio.timeout(UPLOAD_SECONDS):
            reader = await request.multipart()
            while (part := await reader.next()) is not None:
                if not isinstance(part, aiohttp.BodyPartReader) or len(found) > 3:
                    raise files.FileRefused("That upload couldn't be read.")
                if part.name in ("front", "back") and part.name not in found:
                    found[part.name] = await _read_part(part, files.PHOTO_LIMIT)
                elif part.name == "type" and "type" not in found:
                    found["type"] = (await _read_part(part, 100)).decode("utf-8", "replace").strip()
                else:
                    await _read_part(part, 100)
    except TimeoutError:
        raise web.HTTPRequestTimeout(text="The photo took too long to arrive. Try again.") from None
    except ValueError as err:
        if isinstance(err, files.FileRefused):
            raise
        raise files.FileRefused("That upload couldn't be read.") from None
    if not found.get("front"):
        raise files.FileRefused("Choose a photo or PDF of the card.")
    return found


def _card_of(request: web.Request) -> dict | None:
    card = get(svc_of(request).db, request.match_info["cid"])
    return card if card and card["person_id"] == request[PERSON]["id"] else None


@rule("signed_in", person=True)
async def api_person_certificates(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    p, rel = request[PERSON], request[RELATION]
    audit.record(svc.db, "Viewed certificates", actor_id=v.id, actor=v.name, target_id=p["id"],
                 target=p["name"], ip=svc.client_ip(request))
    return web.json_response(person_view(svc, v, p, rel))


@rule("signed_in", person=True)
async def api_add(request: web.Request):
    """Add or renew a card: the person, their manager or director, or HR."""
    svc, v = svc_of(request), viewer_of(request)
    db = svc.db
    p, rel = request[PERSON], request[RELATION]
    if rel not in ADDERS or p["archived"]:
        return error("You can't add a card for them.", 403)
    since = time.time() - DAY
    if db.conn.execute("SELECT COUNT(*) FROM certificates WHERE person_id = ? AND added_at > ?",
                       (p["id"], since)).fetchone()[0] >= ADDS_A_DAY:
        return error("That's a lot of cards today. Try again tomorrow, or ask HR.", 429)
    try:
        form = await _read_form(request)
        t = type_by_key(db, form.get("type"))
        if not t or not t["active"]:
            return error("Choose which card it is.")
        data, mime = await prepare(form["front"], form.get("back"))
    except files.FileRefused as err:
        return error(str(err))
    card = await add_card(svc, p, t, data, mime, v, rel)
    return web.json_response({"card": card_view(db, card, rel, v)})


@rule("signed_in", person=True)
async def api_reading(request: web.Request):
    """What was read from a card just added ("We read: expires 6/14/2029. HR will confirm.")."""
    card = _card_of(request)
    if not card or request[RELATION] not in ADDERS:
        return error(server.NOT_FOUND, 404)
    return web.json_response({"card": card_view(svc_of(request).db, card, request[RELATION],
                                                viewer_of(request))})


@rule("signed_in", person=True)
async def api_photo(request: web.Request):
    """A card's photo or PDF while it's only here (waiting for HR, or turned down): the person
    and HR only (managers see its status and dates). A confirmed card's opens as its document
    (photo_url)."""
    svc, v = svc_of(request), viewer_of(request)
    card, rel = _card_of(request), request[RELATION]
    if not card or rel not in ("self", "hr"):
        return error(server.NOT_FOUND, 404)
    data, mime = await _load(svc.folder, card), card["mime"]
    if data is None:
        return error("The photo isn't kept here any more.", 404)
    turn = request.query.get("turn", "0")
    if rel == "hr" and turn in ("90", "180", "270") and mime != files.MIME["pdf"]:
        async with files.image_slots():
            data = await asyncio.to_thread(_turned, data, int(turn))
        mime = files.MIME["jpeg"]
    p = request[PERSON]
    audit.record(svc.db, "Viewed a certificate photo", actor_id=v.id, actor=v.name,
                 target_id=p["id"], target=p["name"], ip=svc.client_ip(request))
    t = type_by_key(svc.db, card["type_key"])
    return files.file_response(data, mime, f"{t['name'] if t else 'Certificate'} {p['name']}")


def _segments(text: str) -> list[dict]:
    """The read text in pieces, dates and labels marked ("d", "l"), for HR's check screen."""
    spans = []
    for regex, mark in ((ocr_dates.ISO_RE, "d"), (ocr_dates.NUMERIC_RE, "d"),
                        (ocr_dates.MONTH_FIRST_RE, "d"), (ocr_dates.DAY_FIRST_RE, "d"),
                        (ocr_dates.MONTH_YEAR_RE, "d"), (ocr_dates.LABEL_RE, "l")):
        for m in regex.finditer(text):
            if not any(s < m.end() and m.start() < e for s, e, _ in spans):
                spans.append((m.start(), m.end(), mark))
    out, at = [], 0
    for start, end, mark in sorted(spans):
        if start > at:
            out.append({"t": text[at:start]})
        out.append({"t": text[start:end], mark: 1})
        at = end
    if at < len(text):
        out.append({"t": text[at:]})
    return out


@rule("hr", person=True, not_self=True)
async def api_card(request: web.Request):
    """HR's check screen: the card, the text read with its dates marked, the dates suggested, and
    the card it would replace."""
    svc, v = svc_of(request), viewer_of(request)
    db = svc.db
    card, p = _card_of(request), request[PERSON]
    if not card:
        return error(server.NOT_FOUND, 404)
    t = type_by_key(db, card["type_key"]) or {"name": card["type_key"], "olcc": 0, "key": card["type_key"]}
    text = _unseal(card["read_text"], "certificates.read") or ""
    current = db.conn.execute("SELECT * FROM certificates WHERE person_id = ? AND type_key = ? AND "
                              "status = 'confirmed' ORDER BY id DESC LIMIT 1", (p["id"], card["type_key"])).fetchone()
    following = db.conn.execute("SELECT id, person_id FROM certificates WHERE status = 'waiting' AND "
                                "person_id != ? AND id != ? ORDER BY added_at LIMIT 1",
                                (v.id, card["id"])).fetchone()
    audit.record(db, "Opened a certificate to check", actor_id=v.id, actor=v.name,
                 target_id=p["id"], target=p["name"], detail=t["name"], ip=svc.client_ip(request))
    warnings = _list(card["read_warnings"])
    return web.json_response({
        "card": card_view(db, card, "hr", v), "person": {"id": p["id"], "name": p["name"]},
        "type": {"key": t["key"], "name": t["name"], "olcc": bool(t["olcc"])},
        "segments": _segments(text), "warnings": warnings,
        "suggest": {"expires": card["read_expires"], "issued": card["read_issued"],
                    "temporary": any("temporary permit" in w for w in warnings),
                    "under_21": any("under 21" in w for w in warnings)},
        "current": card_view(db, dict(current), "hr", v) if current else None,
        "next": f"#/certificates/check/{following['person_id']}/{following['id']}" if following else None,
        "own": card["added_by"] == v.id})


@rule("hr", person=True, not_self=True)
async def api_confirm(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    card = _card_of(request)
    if not card:
        return error(server.NOT_FOUND, 404)
    if card["status"] != "waiting":
        return error("That card was already decided.", 409)
    data = await read_json(request)
    t = type_by_key(svc.db, card["type_key"])
    if not t:
        return error("That kind of certificate is gone.", 409)
    try:
        values = clean_confirm(data, t, today(svc.db))
        done = await confirm(svc, card, values, v)
    except ValueError as err:
        return error(str(err))
    return web.json_response({"ok": True, "card": card_view(svc.db, done, "hr", v),
                              "message": f"Confirmed. {request[PERSON]['name']} is told."})


@rule("hr", person=True, not_self=True)
async def api_reject(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    card = _card_of(request)
    if not card:
        return error(server.NOT_FOUND, 404)
    if card["status"] != "waiting":
        return error("That card was already decided.", 409)
    data = await read_json(request)
    reason = _plain(data.get("reason"), 300)
    if len(reason) < 3:
        return error("Say why, so they know what to fix (for example: the photo is blurry).")
    try:
        done = reject(svc, card, reason, v)
    except ValueError as err:
        return error(str(err))
    return web.json_response({"ok": True, "card": card_view(svc.db, done, "hr", v),
                              "message": f"Not accepted. {request[PERSON]['name']} is told why."})


@rule("hr", person=True, not_self=True, sensitive=True)
async def api_waive(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    db, p = svc.db, request[PERSON]
    data = await read_json(request)
    t = type_by_key(db, data.get("type"))
    if not t or not t["active"]:
        return error("Choose which certificate to waive.")
    reason = _plain(data.get("reason"), 300)
    if len(reason) < 3:
        return error("Say why it's waived.")
    ends, on = _day(data.get("ends")), today(db)
    if not ends or not on <= ends <= add_years(on, 5):
        return error("Choose when the waiver ends: from today up to 5 years ahead.")
    add_waiver(db, p, t, reason, ends, v)
    return web.json_response({"ok": True, "message": f"{t['name']} waived until {us(ends)}."})


@rule("hr", person=True, not_self=True, sensitive=True)
async def api_end_waiver(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    if not end_waiver(svc.db, request[PERSON], int(request.match_info["wid"]), v):
        return error(server.NOT_FOUND, 404)
    return web.json_response({"ok": True, "message": "The waiver has ended."})


def _filters(query) -> dict:
    f = {k: str(query.get(k) or "")[:60] for k in ("group", "type", "status")}
    within = str(query.get("within") or "")
    f["within"] = int(within) if within.isdigit() and int(within) in WITHIN else None
    return f


@rule("staff")
async def api_board(request: web.Request):
    """HR's compliance board (everyone), or a manager's or director's team board."""
    svc, v = svc_of(request), viewer_of(request)
    db = svc.db
    f = _filters(request.query)
    found = board(db, v, f)
    names = server.group_names(svc)
    for r in found["rows"]:
        r["departments"] = [names.get(g, f"Group {g}") for g in r.pop("groups")]
    seen = {g for p, _ in access.visible_people(db, v) for g in people.groups(p)}
    audit.record(db, "Viewed the certificate board", actor_id=v.id, actor=v.name,
                 ip=svc.client_ip(request))
    extra = {}
    if v.is_hr:
        extra["waiting"] = db.conn.execute("SELECT COUNT(*) FROM certificates WHERE status = "
                                           "'waiting' AND person_id != ?", (v.id,)).fetchone()[0]
    return web.json_response({
        **found, **extra, "filters": f, "hr": v.is_hr, "within": list(WITHIN),
        "statuses": STATUS_WORDS, "types": [{"key": t["key"], "name": t["name"]} for t in types(db)],
        "departments": [{"group": g, "name": names.get(g, f"Group {g}")} for g in sorted(
            seen, key=lambda g: names.get(g, g).casefold())]})


@rule("hr", sensitive=True)
async def api_export(request: web.Request):
    """The compliance board as a spreadsheet (a bulk download: a fresh code first)."""
    svc, v = svc_of(request), viewer_of(request)
    data = await read_json(request)
    f = _filters(data)
    rows = board(svc.db, v, f)["rows"]
    audit.record(svc.db, "Downloaded the certificate board", actor_id=v.id, actor=v.name,
                 detail=f"{len(rows)} rows", ip=svc.client_ip(request), security=True)
    return web.json_response({"name": f"Certificates {today(svc.db).isoformat()}.csv",
                              "csv": board_csv(rows, server.group_names(svc))})


@rule("hr")
async def api_waiting(request: web.Request):
    """Cards waiting for HR to check, oldest first (never HR's own: another HR admin checks
    those)."""
    svc, v = svc_of(request), viewer_of(request)
    db = svc.db
    found = []
    for r in db.conn.execute("SELECT * FROM certificates WHERE status = 'waiting' AND person_id != ? "
                             "ORDER BY added_at LIMIT 200", (v.id,)):
        p = db.person(r["person_id"])
        found.append({**card_view(db, dict(r), "hr", v), "person_id": r["person_id"],
                      "name": p["name"] if p else r["person_id"]})
    return web.json_response({"cards": found})


def _choices(svc) -> tuple[set, set]:
    return ({d["group"] for d in server.departments_list(svc)},
            {p["id"] for p in svc.db.people(archived=False)})


@rule("hr", sensitive=True)
async def api_settings_save(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    data = await read_json(request)
    try:
        every = int(data.get("remind_every"))
    except (TypeError, ValueError):
        return error("Remind every 1 to 14 days.")
    if not 1 <= every <= 14:
        return error("Remind every 1 to 14 days.")
    svc.db.set_settings({"cert_remind_every": every})
    audit.record(svc.db, "Changed HR settings", actor_id=v.id, actor=v.name,
                 detail=f"Certificates: remind every {every} days", ip=svc.client_ip(request),
                 security=True)
    return web.json_response({"ok": True})


@rule("hr", sensitive=True)
async def api_type_add(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    data = await read_json(request)
    try:
        values = clean_type(svc.db, data, None, *_choices(svc))
        type_id = add_type(svc.db, values)
    except ValueError as err:
        return error(str(err))
    audit.record(svc.db, "Added a kind of certificate", actor_id=v.id, actor=v.name,
                 detail=values["name"], ip=svc.client_ip(request), security=True)
    return web.json_response({"ok": True, "id": type_id})


@rule("hr", sensitive=True)
async def api_type_save(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    t = type_by_id(svc.db, int(request.match_info["tid"]))
    if not t:
        return error(server.NOT_FOUND, 404)
    data = await read_json(request)
    try:
        values = clean_type(svc.db, data, t, *_choices(svc))
    except ValueError as err:
        return error(str(err))
    update_type(svc.db, t["id"], values)
    audit.record(svc.db, "Changed a kind of certificate", actor_id=v.id, actor=v.name,
                 detail=f"{t['name']}: {', '.join(sorted(values)) or 'nothing'}",
                 ip=svc.client_ip(request), security=True)
    return web.json_response({"ok": True})


@server.add_routes
def routes(app: web.Application) -> None:
    r = app.router
    pid = r"/api/people/{pid:[A-Za-z0-9_-]{1,40}}/certificates"
    cid = pid + r"/{cid:\d{1,12}}"
    r.add_get(pid, api_person_certificates)
    r.add_post(pid, api_add)
    r.add_post(pid + "/waivers", api_waive)
    r.add_delete(pid + r"/waivers/{wid:\d{1,12}}", api_end_waiver)
    r.add_get(cid, api_card)
    r.add_get(cid + "/reading", api_reading)
    r.add_get(cid + "/photo", api_photo)
    r.add_post(cid + "/confirm", api_confirm)
    r.add_post(cid + "/reject", api_reject)
    r.add_get("/api/certificates/board", api_board)
    r.add_post("/api/certificates/export", api_export)
    r.add_get("/api/certificates/waiting", api_waiting)
    r.add_put("/api/settings/certificates", api_settings_save)
    r.add_post("/api/settings/certificates/types", api_type_add)
    r.add_put(r"/api/settings/certificates/types/{tid:\d{1,6}}", api_type_save)
