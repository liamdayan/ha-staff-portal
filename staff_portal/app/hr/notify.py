"""Telling people something is waiting in the HR portal.

- The portal inbox always works, for everyone.
- Connecteam chat messages, from the custom publisher "Hot Lake HR", when the owner set up the HR
  Connecteam connection with chat (hr/ctapi.py sends them). They never hold details: only that
  something is waiting, and the portal's address.
- Employees' messages wait out quiet hours (9 PM to 7 AM by default, in HR's time zone), and
  they get at most one message a day that isn't urgent.
- Managers, directors and HR get urgent things straight away, and everything else in one daily
  summary.
- Messages wait in a durable outbox (it survives restarts and updates) and are sent inside HR's
  Connecteam lane. A failed one is tried again after 1 minute, 5, 30, 2 hours, 6 hours, then
  daily; after 8 tries it goes on HR's to-do list. A refusal by the budget isn't a try: it waits.
"""

import inspect
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import hrdb

GENERIC = "You have something new in the HR portal."
CHAT_TEXT = "You have something new in the HR portal. Open it here: {url}"
SUMMARY_TEXT = "You have {n} new item{s} in the HR portal. Open it here: {url}"
RETRY_AFTER = (60, 300, 1800, 7200, 21600)   # then once a day
MAX_TRIES = 8
SEND_AT_ONCE = 6                             # HR's lane allows 6 a minute

# Titles shown in the inbox, by kind. Other parts of the HR portal add theirs (register_kind).
KINDS = {
    "recovery_code_used": "A recovery code was used to sign in to your account.",
    "signin_reset": "Your HR sign-in was reset. Ask HR for a new setup code.",
    "start_date": "A start date needs confirming.",
    "message_stuck": "A Connecteam message couldn't be sent.",
    "welcome": "Welcome to the HR portal.",
}

# Set by service.py: chat(person_id, text) sends one Connecteam chat message (None: inbox only),
# and url() is the portal's address for the message.
chat = None
url = lambda: ""   # noqa: E731


def register_kind(kind: str, title: str) -> None:
    KINDS[kind] = title


def _zone(db) -> ZoneInfo:
    try:
        return ZoneInfo(db.setting("time_zone"))
    except (ValueError, KeyError):
        return ZoneInfo("America/Los_Angeles")


def _minutes(text: str, default: int) -> int:
    try:
        hours, minutes = str(text).split(":")
        return int(hours) * 60 + int(minutes)
    except ValueError:
        return default


def quiet_until(db, now: float | None = None) -> float | None:
    """When quiet hours end, if it's quiet hours now (else None)."""
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now, _zone(db))
    start, end = _minutes(db.setting("quiet_start"), 21 * 60), _minutes(db.setting("quiet_end"), 7 * 60)
    minute = local.hour * 60 + local.minute
    quiet = (start <= minute or minute < end) if start > end else (start <= minute < end)
    if not quiet or start == end:
        return None
    ends = local.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if ends <= local:
        ends += timedelta(days=1)
    return ends.timestamp()


def local_day(db, now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, _zone(db)).date().isoformat()


def is_staff(db, person_id) -> bool:
    return bool(db.roles_of(person_id) & {"hr_admin", "director", "manager"})


def notify(person_id, kind: str, urgent: bool = False, *, link: str | None = None,
           title: str | None = None, once: str | None = None, db=None) -> int | None:
    """Put an entry in someone's inbox and, when chat is on, queue a Connecteam message (see the
    timing rules above). once: a key for this event, so a repeated run never adds it twice.
    Returns the inbox entry's ID (None when it was already there)."""
    db = db or hrdb.current
    if db is None:
        return None
    now = time.time()
    pid = str(person_id)
    cur = db.conn.execute(
        "INSERT OR IGNORE INTO inbox (person_id, kind, title, link, urgent, created_at, once) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pid, kind[:40], (title or KINDS.get(kind, GENERIC))[:200], link, 1 if urgent else 0,
         now, once))
    db.conn.commit()
    if not cur.rowcount:
        return None
    entry = cur.lastrowid
    if chat is not None:
        staff = is_staff(db, pid)
        if staff and not urgent:
            return entry   # in tomorrow's daily summary
        day = local_day(db, now)
        if not urgent and db.conn.execute(
                "SELECT 1 FROM outbox WHERE person_id = ? AND day = ? AND urgent = 0",
                (pid, day)).fetchone():
            return entry   # one a day: the inbox has the rest
        due = now if staff else (quiet_until(db, now) or now)
        db.conn.execute("INSERT INTO outbox (person_id, text, urgent, day, created_at, due_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (pid, CHAT_TEXT.format(url=url()), 1 if urgent else 0, day, now, due))
        db.conn.commit()
    return entry


def summaries(db, now: float | None = None) -> int:
    """The daily summary for managers, directors and HR: one message each for what came in since
    the last one and isn't urgent. Returns how many were queued."""
    now = time.time() if now is None else now
    if chat is None:
        return 0
    day, queued = local_day(db, now), 0
    staff = {r[0] for r in db.conn.execute("SELECT DISTINCT person_id FROM roles")}
    for pid in sorted(staff):
        person = db.person(pid)
        if not person or person["archived"]:
            continue
        n = db.conn.execute("SELECT COUNT(*) FROM inbox WHERE person_id = ? AND urgent = 0 AND "
                            "summarized = 0 AND read_at IS NULL", (pid,)).fetchone()[0]
        db.conn.execute("UPDATE inbox SET summarized = 1 WHERE person_id = ? AND summarized = 0",
                        (pid,))
        if n:
            db.conn.execute("INSERT INTO outbox (person_id, text, urgent, day, created_at, due_at) "
                            "VALUES (?, ?, 0, ?, ?, ?)",
                            (pid, SUMMARY_TEXT.format(n=n, s="" if n == 1 else "s", url=url()),
                             day, now, now))
            queued += 1
    db.conn.commit()
    return queued


def retry_after(tries: int) -> int:
    return RETRY_AFTER[tries - 1] if tries <= len(RETRY_AFTER) else 86400


async def send_due(db, now: float | None = None) -> int:
    """Send the messages that are due, a few at a time. Returns how many went."""
    if chat is None:
        return 0
    now = time.time() if now is None else now
    sent = 0
    rows = db.conn.execute("SELECT * FROM outbox WHERE status = 'queued' AND due_at <= ? "
                           "ORDER BY urgent DESC, due_at LIMIT ?", (now, SEND_AT_ONCE)).fetchall()
    for row in rows:
        try:
            result = chat(row["person_id"], row["text"])
            if inspect.isawaitable(result):
                await result
        except Exception as err:  # noqa: BLE001 - every failure is recorded and tried again
            if getattr(err, "budget", False) or type(err).__name__ == "HRBusy":
                # HR's Connecteam lane is used up for now: wait, without counting a try.
                db.conn.execute("UPDATE outbox SET due_at = ?, error = ? WHERE id = ?",
                                (now + 300, str(err)[:300], row["id"]))
                db.conn.commit()
                break
            tries = row["tries"] + 1
            stuck = tries >= MAX_TRIES
            db.conn.execute("UPDATE outbox SET tries = ?, due_at = ?, status = ?, error = ? "
                            "WHERE id = ?", (tries, now + retry_after(tries),
                                             "needs_attention" if stuck else "queued",
                                             str(err)[:300] or type(err).__name__, row["id"]))
            db.conn.commit()
            continue
        db.conn.execute("UPDATE outbox SET status = 'sent', sent_at = ?, tries = tries + 1, "
                        "error = NULL WHERE id = ?", (time.time(), row["id"]))
        db.conn.commit()
        sent += 1
    return sent


def retry(db, message_id: int) -> bool:
    """HR's Retry on a message that needs attention."""
    changed = db.conn.execute("UPDATE outbox SET status = 'queued', tries = 0, due_at = ? "
                              "WHERE id = ? AND status = 'needs_attention'",
                              (time.time(), message_id)).rowcount
    db.conn.commit()
    return bool(changed)


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
