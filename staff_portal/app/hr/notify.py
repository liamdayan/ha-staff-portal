"""Telling people something is waiting in the HR portal: an entry in their inbox there. Nothing
goes outside the portal (the HR portal only reads from Connecteam, so it sends no chat messages).
"""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import hrdb

GENERIC = "You have something new in the HR portal."

# Titles shown in the inbox, by kind. Other parts of the HR portal add theirs (register_kind).
KINDS = {
    "recovery_code_used": "A recovery code was used to sign in to your account.",
    "signin_reset": "Your HR sign-in was reset. Ask HR for a new setup code.",
    "start_date": "A start date needs confirming.",
    "welcome": "Welcome to the HR portal.",
}


def register_kind(kind: str, title: str) -> None:
    KINDS[kind] = title


def _zone(db) -> ZoneInfo:
    try:
        return ZoneInfo(db.setting("time_zone"))
    except (ValueError, KeyError):
        return ZoneInfo("America/Los_Angeles")


def local_day(db, now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, _zone(db)).date().isoformat()


def notify(person_id, kind: str, urgent: bool = False, *, link: str | None = None,
           title: str | None = None, once: str | None = None, db=None) -> int | None:
    """Put an entry in someone's inbox. once: a key for this event, so a repeated run never adds
    it twice. Returns the inbox entry's ID (None when it was already there)."""
    db = db or hrdb.current
    if db is None:
        return None
    cur = db.conn.execute(
        "INSERT OR IGNORE INTO inbox (person_id, kind, title, link, urgent, created_at, once) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(person_id), kind[:40], (title or KINDS.get(kind, GENERIC))[:200], link,
         1 if urgent else 0, time.time(), once))
    db.conn.commit()
    return cur.lastrowid if cur.rowcount else None


def inbox(db, person_id, limit: int = 50) -> list[dict]:
    return [dict(r) for r in db.conn.execute(
        "SELECT id, kind, title, link, urgent, created_at, read_at FROM inbox WHERE person_id = ? "
        "ORDER BY id DESC LIMIT ?", (str(person_id), limit))]


def unread(db, person_id) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM inbox WHERE person_id = ? AND read_at IS NULL",
                           (str(person_id),)).fetchone()[0]


def mark_read(db, person_id, entry_id: int | None = None) -> None:
    if entry_id is None:
        db.conn.execute("UPDATE inbox SET read_at = ? WHERE person_id = ? AND read_at IS NULL",
                        (time.time(), str(person_id)))
    else:
        db.conn.execute("UPDATE inbox SET read_at = ? WHERE person_id = ? AND id = ? AND "
                        "read_at IS NULL", (time.time(), str(person_id), entry_id))
    db.conn.commit()
