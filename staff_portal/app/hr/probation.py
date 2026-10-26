"""Probation (design 7.8), which runs by its own rules.

Two kinds:
- Introductory (new hires): starts on the start date from Connecteam and lasts the length its
  policy sets for the person's department (90 days unless the published point system says
  otherwise). It's made by itself, only for people whose period hasn't already ended, so long-time
  staff never get one. A rehire (a later start date) gets a new one when the policy says so; a
  transfer between departments never does.
- Disciplinary: HR places an existing employee on it, with an end date and a reason.

Until a point system is published, the defaults below apply, so probation works from day one.

How one runs:
- Extending needs HR, a reason and a new end date: at most the policy's number of extensions (1),
  each up to its days (30), and never past its maximum length (120 days). A leave of absence is
  HR extending with "leave", which doesn't count against those limits. Shortening is HR moving
  the end earlier. The employee is told and asked to acknowledge it.
- The end-of-probation review is due some days before the end (14). The reviews part opens it
  (end_review_due) and passes on the manager's recommendation: pass, extend (with a date) or end
  employment (record_recommendation). A manager can also recommend here. HR decides (decide):
  "end employment" becomes a to-do for HR, done by hand (archiving in Connecteam).
- If the end passes with no decision, it stays open and managers and HR are reminded daily, or it
  passes by itself when the policy says so.
- Which rules applied to an incident depends on the day it happened (current(db, person, on)).

Reasons and notes are sealed; dates and statuses stay readable.
"""

import logging
import time
import weakref
from datetime import date, timedelta

from aiohttp import web

from . import access, audit, crypto, notify, policy, server, service
from .access import rule
from .server import error, read_json, svc_of, viewer_of

log = logging.getLogger("staff_portal")

INTRODUCTORY, DISCIPLINARY = "introductory", "disciplinary"
OUTCOMES = {"pass": "Pass", "extend": "Extend", "end": "End employment"}
DECISIONS = {**OUTCOMES, "waive": "Waive (it no longer applies)"}
STATUS_WORDS = {"open": "Running", "passed": "Passed", "ended": "Employment ending",
                "waived": "Waived"}
MAX_DAYS = 730                        # no probation runs longer than two years
WORDS = {"introductory period": "Introductory period", "probation": "Probation"}
STAFF_WORDS = {"extended": "extended", "shortened": "end date changed", "passed": "passed",
               "placed": "started (placed by HR)", "ended": "decided: employment ends (HR's to-do)",
               "waived": "waived"}

# Used until a point system is published, and for anything a published policy leaves out
# (the recommended answers to questions 35 to 37).
DEFAULT_POLICY = {"days": 90, "department_days": {}, "departments": [], "extensions": {"count": 1,
                  "days": 30, "max_days": 120}, "review_lead_days": 14, "when_end_passes": "remind",
                  "on_pass": "keep", "rehires": True, "transfers": False}
DEFAULT_POLICIES = [
    {**DEFAULT_POLICY, "key": INTRODUCTORY, "name": "Introductory period", "kind": INTRODUCTORY},
    {**DEFAULT_POLICY, "key": DISCIPLINARY, "name": "Disciplinary probation", "kind": DISCIPLINARY},
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS probations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,             -- Connecteam user ID
    kind TEXT NOT NULL,                  -- introductory or disciplinary
    policy_key TEXT NOT NULL,            -- the probation policy whose rules apply
    starts TEXT NOT NULL,                -- YYYY-MM-DD: its first day
    first_ends TEXT NOT NULL,            -- the end it started with
    ends TEXT NOT NULL,                  -- its last day now, after extensions
    status TEXT NOT NULL DEFAULT 'open', -- open, passed, ended (employment ending), waived
    extensions INTEGER NOT NULL DEFAULT 0,   -- extensions used (a leave doesn't count)
    reason TEXT,                         -- sealed: why HR placed them on it (HR only)
    placed_by TEXT,
    created_at REAL NOT NULL,
    recommendation TEXT,                 -- pass, extend or end: from the review or a manager
    recommend_to TEXT,                   -- ...with the date for "extend"
    recommend_review TEXT,               -- the review it came from
    recommend_note TEXT,                 -- sealed (managers and HR)
    recommended_by TEXT,
    recommended_at REAL,
    decided_on TEXT,                     -- the day HR decided (or it passed by itself)
    decided_by TEXT,
    decided_at REAL,
    outcome_note TEXT,                   -- sealed: HR's reason (managers and HR)
    acknowledged_at REAL,                -- the employee acknowledged the last change
    told_due TEXT,                       -- the end date the "review due" message was sent for
    todo_done_at REAL,                   -- "end employment": HR's to-do, done
    todo_done_by TEXT
);
CREATE INDEX IF NOT EXISTS probations_person ON probations (person_id, starts);
CREATE UNIQUE INDEX IF NOT EXISTS probations_intro ON probations (person_id, starts)
    WHERE kind = 'introductory';
-- What happened to each probation: placed, extended, shortened, recommended, decided. Never changed.
CREATE TABLE IF NOT EXISTS probation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    probation_id INTEGER NOT NULL,
    person_id TEXT NOT NULL,
    ts REAL NOT NULL,
    actor_id TEXT,
    actor TEXT,
    what TEXT NOT NULL,                  -- plain words, no details
    old_ends TEXT,
    new_ends TEXT,
    note TEXT,                           -- sealed
    visibility TEXT NOT NULL DEFAULT 'managers'
);
CREATE INDEX IF NOT EXISTS probation_events_of ON probation_events (probation_id, id);
"""

notify.register_kind("probation_due", "An end-of-probation review is due.")
notify.register_kind("probation_overdue", "A probation has ended without a decision.")
notify.register_kind("probation_changed", "A probation was extended or decided.")
notify.register_kind("probation_recommended", "A probation recommendation is waiting for HR.")


def setup(db) -> None:
    db.add_schema(SCHEMA)
    db.append_only("probation_events")


def _part(svc) -> None:
    svc.on_start.append(lambda s: setup(s.db))
    svc.daily_jobs.append(nightly)
    svc.tick_jobs.append(hourly)


service.PARTS.append(_part)


# ---------------------------------------------------------------- small helpers

def today_of(db) -> date:
    """Today in HR's time zone (Pacific)."""
    return date.fromisoformat(notify.local_day(db))


def as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def unseal(value, purpose: str) -> str | None:
    try:
        return crypto.unseal_text(value, purpose)
    except crypto.Damaged:
        return "(damaged)"


def clean_text(value, limit: int = 2000) -> str:
    """Plain text people type: control characters out (new lines kept), trimmed."""
    text = "".join(c for c in str(value or "") if c in "\n\t" or (c >= " " and c != "\x7f"))
    return text.strip()[:limit]


def word(db) -> str:
    """What screens call introductory probation (HR setting; question 38)."""
    value = db.setting("probation_word") or "introductory period"
    return value if value in WORDS else "introductory period"


def shows_end(db) -> bool:
    """Whether employees see their end date (HR setting; yes by default)."""
    return db.setting("probation_show_end") != "0"


def kind_word(db, kind: str) -> str:
    return WORDS[word(db)] if kind == INTRODUCTORY else "Disciplinary probation"


def hr_admins(db, *, exclude=()) -> list[str]:
    """Active HR admins, leaving out anyone named (the person whose file it is, the actor)."""
    skip = {str(x) for x in exclude if x}
    found = []
    for pid in db.with_role(access.HR_ADMIN):
        p = db.person(pid)
        if p and not p["archived"] and pid not in skip:
            found.append(pid)
    return found


def team_managers(db, person: dict) -> list[str]:
    """The managers who see this person's file (their departments, or their direct reports)."""
    roles = db.all_roles()
    found = []
    for pid in sorted(db.all_manager_groups()) + sorted(access.managers_of(person)):
        if pid == person["id"] or pid in found or access.MANAGER not in roles.get(pid, set()):
            continue
        manager = db.person(pid)
        if not manager or manager["archived"]:
            continue
        viewer = access.viewer_for(db, manager)
        if access.relation(viewer, person, roles.get(person["id"], set())) == "manager":
            found.append(pid)
    return found


def tell(db, ids, kind: str, *, title: str, link: str, urgent: bool = False,
         once: str | None = None) -> None:
    for pid in dict.fromkeys(str(i) for i in ids if i):
        notify.notify(pid, kind, urgent, link=link, title=title, once=once, db=db)


# ---------------------------------------------------------------- policies

def policies(db, on: date | None = None) -> list[dict]:
    """The probation policies in force: the published point system's (policy.py), each filled in
    from the defaults, or the defaults themselves."""
    version = policy.current(db, on or today_of(db))
    found = []
    for p in (version["body"].get("probation") or []) if version else []:
        ext = {**DEFAULT_POLICY["extensions"], **(p.get("extensions") or {})}
        found.append({**DEFAULT_POLICY, **p, "extensions": ext,
                      "kind": DISCIPLINARY if p.get("kind") == DISCIPLINARY else INTRODUCTORY})
    for default in DEFAULT_POLICIES:
        if not any(p["kind"] == default["kind"] for p in found):
            found.append(dict(default))
    return found


def policy_for(db, key: str, kind: str | None = None, groups=None) -> dict:
    """A probation policy by key, else the first of its kind. groups (a new hire's departments):
    the introductory policy that covers them (its departments; none listed covers everyone)."""
    listed = policies(db)
    if groups is not None:
        covers = [p for p in listed if p["kind"] == INTRODUCTORY and (
            not p["departments"] or set(map(str, groups)) & set(map(str, p["departments"])))]
        if covers:
            return covers[0]
    return next((p for p in listed if p["key"] == key), None) or next(
        p for p in listed if p["kind"] == (kind or INTRODUCTORY))


def _int(value, default: int, low: int = 0, high: int = MAX_DAYS) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def days_for(pol: dict, person: dict) -> int:
    """Its length for this person: their department's, the longest when they're in several."""
    by_department = {str(g): d for g, d in (pol.get("department_days") or {}).items()}
    found = [_int(by_department[g], 0, 1) for g in access.groups_of(person) if g in by_department]
    return max(found) if found else _int(pol.get("days"), 90, 1)


# ---------------------------------------------------------------- reading

def get(db, probation_id) -> dict | None:
    try:
        pid = int(probation_id)
    except (TypeError, ValueError):
        return None
    row = db.conn.execute("SELECT * FROM probations WHERE id = ?", (pid,)).fetchone()
    return dict(row) if row else None


def of_person(db, person_id) -> list[dict]:
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM probations WHERE person_id = ? ORDER BY starts DESC, id DESC",
        (str(person_id),))]


def last_day(row: dict) -> str:
    """The last day it was (or will be) in force."""
    if row["status"] != "open" and row["decided_on"]:
        return min(row["ends"], row["decided_on"])
    return row["ends"]


def public(row: dict) -> dict:
    return {"id": row["id"], "policy_key": row["policy_key"], "kind": row["kind"],
            "start": row["starts"], "end": row["ends"], "status": row["status"],
            "extensions": row["extensions"]}


def current(db, person_id, on: date) -> dict | None:
    """The probation in force for this person on that day: {id, policy_key, kind, start, end,
    status, extensions} (dates as YYYY-MM-DD; extensions: how many were used), or None."""
    day = (as_date(on) or today_of(db)).isoformat()
    for row in of_person(db, person_id):
        if row["starts"] <= day <= last_day(row):
            return public(row)
    return None


def end_review_due(db, on: date) -> list[dict]:
    """Open probations whose end-of-probation review is due by that day (or overdue):
    [{person_id, probation_id, kind, start, end, due}], dates as YYYY-MM-DD."""
    day = as_date(on) or today_of(db)
    found = []
    for row in db.conn.execute("SELECT * FROM probations WHERE status = 'open' ORDER BY ends"):
        pol = policy_for(db, row["policy_key"], row["kind"])
        due = date.fromisoformat(row["ends"]) - timedelta(days=_int(pol["review_lead_days"], 14))
        if due <= day:
            found.append({"person_id": row["person_id"], "probation_id": row["id"],
                          "kind": row["kind"], "start": row["starts"], "end": row["ends"],
                          "due": due.isoformat()})
    return found


def events(db, probation_id) -> list[dict]:
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM probation_events WHERE probation_id = ? ORDER BY id", (probation_id,))]


def _event(db, row: dict, what: str, actor_id, actor: str, *, old=None, new=None, note=None,
           visibility: str = "managers") -> None:
    db.conn.execute(
        "INSERT INTO probation_events (probation_id, person_id, ts, actor_id, actor, what, "
        "old_ends, new_ends, note, visibility) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["id"], row["person_id"], time.time(), actor_id, actor, what, old, new,
         crypto.seal_text(note, "probation.event") if note else None, visibility))


# ---------------------------------------------------------------- introductory probation

def ensure_introductory(db, person: dict, today: date | None = None) -> dict | None:
    """Make (or follow) a new hire's introductory probation from their start date. Returns it."""
    today = today or today_of(db)
    start = as_date(person.get("start_date"))
    if person.get("archived") or start is None or start > today + timedelta(days=366):
        return None
    pol = policy_for(db, INTRODUCTORY, groups=access.groups_of(person))
    ends = start + timedelta(days=days_for(pol, person) - 1)    # its last day: 90 days in all
    mine = [r for r in of_person(db, person["id"]) if r["kind"] == INTRODUCTORY]
    if any(r["starts"] == start.isoformat() for r in mine):
        return next(r for r in mine if r["starts"] == start.isoformat())
    untouched = [r for r in mine if r["status"] == "open" and not r["extensions"]
                 and not r["recommendation"] and r["ends"] == r["first_ends"]]
    if untouched:
        # The start date moved (HR confirmed it, or Connecteam's changed): it follows.
        row = untouched[0]
        db.conn.execute("UPDATE probations SET starts = ?, first_ends = ?, ends = ? WHERE id = ?",
                        (start.isoformat(), ends.isoformat(), ends.isoformat(), row["id"]))
        _event(db, row, "Start date changed", None, "HR portal", old=row["ends"],
               new=ends.isoformat())
        db.conn.commit()
        return get(db, row["id"])
    if ends < today:
        return None   # their period was over before the HR portal knew them
    if mine and (not pol.get("rehires", True) or any(r["status"] == "open" for r in mine)):
        return None
    cur = db.conn.execute(
        "INSERT OR IGNORE INTO probations (person_id, kind, policy_key, starts, first_ends, ends, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (person["id"], INTRODUCTORY, pol["key"], start.isoformat(), ends.isoformat(),
         ends.isoformat(), time.time()))
    if cur.rowcount:
        row = get(db, cur.lastrowid)
        _event(db, row, "Started on the start date", None, "HR portal", new=ends.isoformat(),
               visibility="employee")
    db.conn.commit()
    return get(db, cur.lastrowid) if cur.rowcount else None


def ensure_all(db, today: date | None = None) -> int:
    made = 0
    for person in db.people(archived=False):
        before = db.conn.total_changes
        ensure_introductory(db, person, today)
        made += db.conn.total_changes != before
    return made


# ---------------------------------------------------------------- HR's actions

def place(db, person: dict, ends: date, reason: str, actor_id, actor: str,
          policy_key: str | None = None, today: date | None = None) -> dict:
    """HR places someone on disciplinary probation until `ends`. Raises ValueError."""
    today = today or today_of(db)
    pol = policy_for(db, policy_key or DISCIPLINARY, DISCIPLINARY)
    if pol["kind"] != DISCIPLINARY:
        raise ValueError("Choose a disciplinary probation policy.")
    if not reason:
        raise ValueError("Say why.")
    if ends <= today or ends > today + timedelta(days=MAX_DAYS):
        raise ValueError("Choose an end date after today, within two years.")
    if any(r["status"] == "open" and r["kind"] == DISCIPLINARY for r in of_person(db, person["id"])):
        raise ValueError("They're already on disciplinary probation.")
    cur = db.conn.execute(
        "INSERT INTO probations (person_id, kind, policy_key, starts, first_ends, ends, reason, "
        "placed_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (person["id"], DISCIPLINARY, pol["key"], today.isoformat(), ends.isoformat(),
         ends.isoformat(), crypto.seal_text(reason, "probation.reason"), actor_id, time.time()))
    row = get(db, cur.lastrowid)
    _event(db, row, "Placed on disciplinary probation", actor_id, actor, new=ends.isoformat(),
           note=reason, visibility="hr")
    db.conn.commit()
    _told_of_change(db, row, "placed", actor_id)
    return get(db, row["id"])


def change_end(db, row: dict, new_end: date, reason: str, actor_id, actor: str, *,
               leave: bool = False, today: date | None = None) -> dict:
    """HR extends (or shortens) a running probation. Extensions keep to the policy's limits,
    except for a leave of absence. Raises ValueError."""
    today = today or today_of(db)
    if row["status"] != "open":
        raise ValueError("It's already decided.")
    if not reason:
        raise ValueError("Say why.")
    old = date.fromisoformat(row["ends"])
    starts = date.fromisoformat(row["starts"])
    if new_end == old:
        raise ValueError("That's the end date it already has.")
    if new_end <= starts or new_end > starts + timedelta(days=MAX_DAYS):
        raise ValueError("That end date doesn't look right.")
    pol = policy_for(db, row["policy_key"], row["kind"])
    ext = pol["extensions"]
    longer = new_end > old
    if longer and not leave:
        if row["extensions"] >= _int(ext.get("count"), 1):
            raise ValueError(f"It can be extended at most {_int(ext.get('count'), 1)} time(s), "
                             "except for a leave of absence.")
        if (new_end - old).days > _int(ext.get("days"), 30):
            raise ValueError(f"An extension is at most {_int(ext.get('days'), 30)} days.")
        if (new_end - starts).days + 1 > _int(ext.get("max_days"), 120):     # both days count
            raise ValueError(f"It can't run longer than {_int(ext.get('max_days'), 120)} days in all.")
    if not longer and new_end < today:
        raise ValueError("Choose today or a later day. To end it now, decide it instead.")
    # Extending is HR's answer to any recommendation waiting: it leaves HR's queue (the
    # recommendation stays in the probation's history).
    db.conn.execute("UPDATE probations SET ends = ?, extensions = extensions + ?, "
                    "acknowledged_at = NULL WHERE id = ?",
                    (new_end.isoformat(), 1 if longer and not leave else 0, row["id"]))
    if longer:
        db.conn.execute("UPDATE probations SET recommendation = NULL, recommend_to = NULL, "
                        "recommend_review = NULL, recommend_note = NULL, recommended_by = NULL, "
                        "recommended_at = NULL WHERE id = ?", (row["id"],))
    what = ("Extended for a leave of absence" if leave else "Extended") if longer else "Shortened"
    _event(db, row, what, actor_id, actor, old=row["ends"], new=new_end.isoformat(), note=reason)
    db.conn.commit()
    _told_of_change(db, row, "extended" if longer else "shortened", actor_id)
    return get(db, row["id"])


def record_recommendation(db, probation_id, outcome: str, extend_to, review_id, actor_id,
                          note: str | None = None) -> dict:
    """The manager's recommendation at the end-of-probation review: "pass", "extend" (with the
    date in extend_to) or "end". HR then decides (decide). Raises ValueError."""
    row = get(db, probation_id)
    if row is None:
        raise ValueError("That probation isn't here.")
    if row["status"] != "open":
        raise ValueError("It's already decided.")
    if outcome not in OUTCOMES:
        raise ValueError("Choose pass, extend or end employment.")
    to = as_date(extend_to) if outcome == "extend" else None
    if outcome == "extend" and (to is None or to <= date.fromisoformat(row["ends"])):
        raise ValueError("Choose the new end date, after the current one.")
    db.conn.execute(
        "UPDATE probations SET recommendation = ?, recommend_to = ?, recommend_review = ?, "
        "recommend_note = ?, recommended_by = ?, recommended_at = ? WHERE id = ?",
        (outcome, to.isoformat() if to else None, str(review_id) if review_id else None,
         crypto.seal_text(note, "probation.recommend_note") if note else None,
         str(actor_id) if actor_id else None, time.time(), row["id"]))
    who = db.person(actor_id) if actor_id else None
    _event(db, row, f"Recommended: {OUTCOMES[outcome]}" + (f" to {to.isoformat()}" if to else ""),
           actor_id, who["name"] if who else "Review", note=note)
    db.conn.commit()
    person = db.person(row["person_id"]) or {"id": row["person_id"], "name": row["person_id"]}
    tell(db, hr_admins(db, exclude=(row["person_id"], actor_id)), "probation_recommended",
         title=f"{person['name']}: a probation recommendation is waiting for you.",
         link=f"#/people/{row['person_id']}", once=f"prob-rec-{row['id']}-{time.time():.0f}")
    return get(db, row["id"])


def decide(db, probation_id, outcome: str, extend_to=None, *, actor_id, actor: str = "HR",
           reason: str = "", today: date | None = None) -> dict:
    """HR's decision: "pass", "extend" (to extend_to), "end" (employment: a to-do for HR, done by
    hand) or "waive". Raises ValueError."""
    today = today or today_of(db)
    row = get(db, probation_id)
    if row is None:
        raise ValueError("That probation isn't here.")
    if row["status"] != "open":
        raise ValueError("It's already decided.")
    if outcome not in DECISIONS:
        raise ValueError("Choose pass, extend, end employment or waive.")
    if not reason:
        raise ValueError("Say why.")
    if outcome == "extend":
        to = as_date(extend_to)
        if to is None:
            raise ValueError("Choose the new end date.")
        return change_end(db, row, to, reason, actor_id, actor, today=today)
    status = {"pass": "passed", "end": "ended", "waive": "waived"}[outcome]
    db.conn.execute("UPDATE probations SET status = ?, decided_on = ?, decided_by = ?, "
                    "decided_at = ?, outcome_note = ?, acknowledged_at = NULL WHERE id = ?",
                    (status, today.isoformat(), str(actor_id), time.time(),
                     crypto.seal_text(reason, "probation.outcome_note"), row["id"]))
    _event(db, row, DECISIONS[outcome].split(" (")[0], actor_id, actor, note=reason,
           visibility="employee" if outcome == "pass" else "managers")
    db.conn.commit()
    if outcome == "pass" and policy_for(db, row["policy_key"], row["kind"]).get("on_pass") == "clear":
        for hook in on_pass_clear:
            try:
                hook(db, get(db, row["id"]), actor_id)
            except Exception as err:  # noqa: BLE001 - the decision stands either way
                log.warning("HR portal: clearing probation points failed: %r", err)
    _told_of_change(db, row, status, actor_id)
    return get(db, row["id"])


# Called when someone passes a probation whose policy clears its points: fn(db, row, actor_id).
# discipline.py adds the one that writes the points line.
on_pass_clear: list = []
# Whether an end-of-probation review is already reminding people: fn(db, row) -> bool.
# reviews.py adds one, so its writer isn't told twice that the review is due ("probation_due"
# here, and the review's own reminders, with HR told when it's late).
review_reminds: list = []


def acknowledge(db, row: dict, person_id) -> None:
    if row["person_id"] != str(person_id):
        raise ValueError("That isn't yours.")
    db.conn.execute("UPDATE probations SET acknowledged_at = ? WHERE id = ?", (time.time(), row["id"]))
    _event(db, row, "Acknowledged by the employee", person_id, None, visibility="employee")
    db.conn.commit()


def _told_of_change(db, row: dict, what: str, actor_id) -> None:
    """Extended, shortened, passed or placed: the employee (except an ending), their managers and HR."""
    person = db.person(row["person_id"]) or {"id": row["person_id"], "name": row["person_id"]}
    words = kind_word(db, row["kind"]).lower()
    link = f"#/people/{row['person_id']}"
    once = f"prob-{row['id']}-{what}-{time.time():.0f}"
    if what in ("extended", "shortened", "passed", "placed"):
        title = {"extended": f"Your {words} was extended. Open it to acknowledge.",
                 "shortened": f"Your {words} end date changed. Open it to acknowledge.",
                 "passed": f"You passed your {words}.",
                 "placed": "You were placed on disciplinary probation. Open it to acknowledge."}[what]
        tell(db, [row["person_id"]], "probation_changed", title=title, link=link, once=once)
    staff = [m for m in team_managers(db, person) if m != actor_id]
    staff += hr_admins(db, exclude=(row["person_id"], actor_id))
    tell(db, staff, "probation_changed", link=link, once=once,
         title=f"{person['name']}: {words} {STAFF_WORDS[what]}.")


# ---------------------------------------------------------------- background work

async def nightly(svc) -> None:
    daily(svc.db, today_of(svc.db))


async def hourly(svc, now: float) -> None:
    """New hires' introductory probation soon after they appear (people are read hourly)."""
    if now - _last_ensure.get(svc, 0.0) >= 3600:
        _last_ensure[svc] = now
        ensure_all(svc.db)


_last_ensure: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def daily(db, today: date) -> dict:
    """New hires' probation; "review due" once for each end date; and once the end has passed
    undecided, a daily reminder (or, where the policy says so, it passes by itself)."""
    counts = {"made": ensure_all(db, today), "due": 0, "reminded": 0, "passed": 0}
    for row in [dict(r) for r in db.conn.execute("SELECT * FROM probations WHERE status = 'open'")]:
        person = db.person(row["person_id"])
        if not person or person["archived"]:
            continue   # former employees: HR decides from their file, without reminders
        pol = policy_for(db, row["policy_key"], row["kind"])
        ends = date.fromisoformat(row["ends"])
        due = ends - timedelta(days=_int(pol["review_lead_days"], 14))
        words = kind_word(db, row["kind"]).lower()
        staff = team_managers(db, person) + hr_admins(db, exclude=(person["id"],))
        link = f"#/people/{person['id']}"
        if today > ends:
            # Passing by itself never overrules a recommendation HR hasn't confirmed (7.8).
            if pol.get("when_end_passes") == "pass" and row["recommendation"] in (None, "pass"):
                decide(db, row["id"], "pass", actor_id="policy", actor="HR portal",
                       reason="The end date passed with no decision; the policy passes it.",
                       today=today)
                counts["passed"] += 1
            else:
                tell(db, staff, "probation_overdue", link=link, once=f"prob-over-{row['id']}-{today}",
                     title=f"{person['name']}: the {words} ended on {row['ends']} with no "
                           "decision. HR decides.")
                counts["reminded"] += 1
        elif today >= due and row["told_due"] != row["ends"]:
            if not any(check(db, row) for check in review_reminds):
                tell(db, staff, "probation_due", link=link,
                     once=f"prob-due-{row['id']}-{row['ends']}",
                     title=f"{person['name']}: the end-of-{words} review is due. It ends on "
                           f"{row['ends']}.")
                counts["due"] += 1
            db.conn.execute("UPDATE probations SET told_due = ? WHERE id = ?", (row["ends"], row["id"]))
            db.conn.commit()
    return counts


# ---------------------------------------------------------------- what each person sees

def view(db, row: dict, rel: str) -> dict:
    """A probation as this relation may see it (reasons and notes stay with managers and HR,
    HR's own placing reason with HR)."""
    pol = policy_for(db, row["policy_key"], row["kind"])
    ext = pol["extensions"]
    found = {"id": row["id"], "kind": row["kind"], "title": kind_word(db, row["kind"]),
             "starts": row["starts"], "status": row["status"],
             "status_word": STATUS_WORDS.get(row["status"], row["status"]),
             "decided_on": row["decided_on"], "needs_ack": False}
    if rel == "self":
        found["ends"] = row["ends"] if shows_end(db) else None
        found["needs_ack"] = (row["acknowledged_at"] is None and (
            row["ends"] != row["first_ends"] or row["kind"] == DISCIPLINARY)
            and row["status"] == "open")
        return found
    found.update(ends=row["ends"], first_ends=row["first_ends"], extensions=row["extensions"],
                 extension_limits={"count": _int(ext.get("count"), 1),
                                   "days": _int(ext.get("days"), 30),
                                   "maximum": _int(ext.get("max_days"), 120)},
                 review_due=(date.fromisoformat(row["ends"]) - timedelta(
                     days=_int(pol["review_lead_days"], 14))).isoformat(),
                 recommendation=row["recommendation"] and {
                     "outcome": row["recommendation"], "word": OUTCOMES[row["recommendation"]],
                     "to": row["recommend_to"], "from_review": bool(row["recommend_review"]),
                     "note": unseal(row["recommend_note"], "probation.recommend_note")},
                 acknowledged=row["acknowledged_at"] is not None,
                 outcome_note=unseal(row["outcome_note"], "probation.outcome_note"),
                 separation_todo=row["status"] == "ended" and row["todo_done_at"] is None)
    if rel == "hr":
        found["reason"] = unseal(row["reason"], "probation.reason")
    found["history"] = [
        {"when": round(e["ts"]), "what": e["what"], "actor": e["actor"], "old_ends": e["old_ends"],
         "new_ends": e["new_ends"], "note": unseal(e["note"], "probation.event")}
        for e in access.visible(events(db, row["id"]), rel)]
    return found


@server.section("probation", 40)
def probation_section(svc, v, p: dict, rel: str) -> dict | None:
    db = svc.db
    try:
        ensure_introductory(db, p)
    except Exception as err:  # noqa: BLE001 - the page still shows
        log.warning("HR portal: checking probation failed: %r", err)
    rows = of_person(db, p["id"])
    if rel == "self":
        rows = [r for r in rows if r["status"] == "open"]
    if not rows and rel != "hr":
        return None
    shown = [view(db, r, rel) for r in rows[:6]]
    return {"title": shown[0]["title"] if shown else WORDS[word(db)],
            "rows": [{"label": s["title"], "value": f"{s['status_word']}, {s['starts']} to "
                      f"{s.get('ends') or '(not shown)'}"} for s in shown],
            "probations": shown, "word": word(db),
            "can": {"place": rel == "hr" and not p["archived"], "decide": rel == "hr",
                    "recommend": rel in ("manager", "director")},
            "policies": [{"key": x["key"], "name": x.get("name") or x["key"]}
                         for x in policies(db) if x["kind"] == DISCIPLINARY] if rel == "hr" else []}


# ---------------------------------------------------------------- routes

def _probation_of(request, *, staff: bool, hr: bool = False) -> tuple:
    """The probation a route names, with its person, when the viewer may act on it."""
    svc = svc_of(request)
    v = viewer_of(request)
    row = get(svc.db, request.match_info["prid"])
    person = svc.db.person(row["person_id"]) if row else None
    rel = access.person_relation(svc.db, v, person)
    if row is None or rel is None or (hr and rel != "hr") or (
            staff and rel not in ("manager", "director", "hr")):
        raise access.Refused(server.NOT_FOUND, 404)
    return svc, v, row, person, rel


def _reply(svc, row: dict, rel: str, message: str) -> web.Response:
    return web.json_response({"ok": True, "message": message, "probation": view(svc.db, row, rel)})


@rule("hr", person=True, not_self=True, sensitive=True)
async def api_place(request: web.Request):
    svc, p, aid, name, ip = server.hr_action(request)
    data = await read_json(request)
    ends = as_date(data.get("ends"))
    if ends is None:
        return error("Choose the end date.")
    try:
        row = place(svc.db, p, ends, clean_text(data.get("reason")), aid, name,
                    str(data.get("policy_key") or "") or None)
    except ValueError as err:
        return error(str(err))
    audit.record(svc.db, "Placed on disciplinary probation", actor_id=aid, actor=name,
                 target_id=p["id"], target=p["name"], detail=f"Until {row['ends']}", ip=ip)
    return _reply(svc, row, "hr", f"{p['name']} is on disciplinary probation until {row['ends']}.")


@rule("hr", sensitive=True)
async def api_change(request: web.Request):
    svc, v, row, person, rel = _probation_of(request, staff=True, hr=True)
    access.not_own_file(v, person["id"])
    data = await read_json(request)
    ends = as_date(data.get("ends"))
    if ends is None:
        return error("Choose the new end date.")
    try:
        row = change_end(svc.db, row, ends, clean_text(data.get("reason")), v.id, v.name,
                         leave=data.get("leave") is True)
    except ValueError as err:
        return error(str(err))
    audit.record(svc.db, "Changed a probation's end date", actor_id=v.id, actor=v.name,
                 target_id=person["id"], target=person["name"], detail=f"Now ends {row['ends']}",
                 ip=svc.client_ip(request))
    return _reply(svc, row, rel, f"It now ends on {row['ends']}.")


@rule("staff")
async def api_recommend(request: web.Request):
    svc, v, row, person, rel = _probation_of(request, staff=True)
    access.not_own_file(v, person["id"])
    data = await read_json(request)
    try:
        row = record_recommendation(svc.db, row["id"], str(data.get("outcome") or ""),
                                    data.get("extend_to"), None, v.id,
                                    clean_text(data.get("note")) or None)
    except ValueError as err:
        return error(str(err))
    audit.record(svc.db, "Recommended a probation outcome", actor_id=v.id, actor=v.name,
                 target_id=person["id"], target=person["name"], detail=OUTCOMES[row["recommendation"]],
                 ip=svc.client_ip(request))
    return _reply(svc, row, rel, "Sent to HR to decide.")


@rule("hr", sensitive=True)
async def api_decide(request: web.Request):
    svc, v, row, person, rel = _probation_of(request, staff=True, hr=True)
    access.not_own_file(v, person["id"])
    data = await read_json(request)
    outcome = str(data.get("outcome") or "")
    try:
        row = decide(svc.db, row["id"], outcome, data.get("extend_to"), actor_id=v.id,
                     actor=v.name, reason=clean_text(data.get("reason")))
    except ValueError as err:
        return error(str(err))
    audit.record(svc.db, "Decided a probation", actor_id=v.id, actor=v.name, target_id=person["id"],
                 target=person["name"], detail=DECISIONS[outcome].split(" (")[0],
                 ip=svc.client_ip(request))
    message = {"pass": "Passed.", "extend": f"Extended to {row['ends']}.",
               "end": "Recorded. Archive them in Connecteam by hand: it's on your to-do list.",
               "waive": "Waived."}[outcome]
    return _reply(svc, row, rel, message)


@rule("signed_in")
async def api_acknowledge(request: web.Request):
    svc = svc_of(request)
    v = viewer_of(request)
    row = get(svc.db, request.match_info["prid"])
    if row is None or row["person_id"] != v.id:
        return error(server.NOT_FOUND, 404)
    acknowledge(svc.db, row, v.id)
    audit.record(svc.db, "Acknowledged a probation change", actor_id=v.id, actor=v.name,
                 target_id=v.id, target=v.name, ip=svc.client_ip(request))
    return _reply(svc, get(svc.db, row["id"]), "self", "Thank you.")


@rule("hr")
async def api_todo_done(request: web.Request):
    svc, v, row, person, rel = _probation_of(request, staff=True, hr=True)
    access.not_own_file(v, person["id"])
    if row["status"] != "ended" or row["todo_done_at"]:
        return error("There's nothing to do for this one.", 409)
    svc.db.conn.execute("UPDATE probations SET todo_done_at = ?, todo_done_by = ? WHERE id = ?",
                        (time.time(), v.id, row["id"]))
    svc.db.conn.commit()
    audit.record(svc.db, "Separation done (probation)", actor_id=v.id, actor=v.name,
                 target_id=person["id"], target=person["name"], ip=svc.client_ip(request))
    return _reply(svc, get(svc.db, row["id"]), rel, "Done.")


@server.add_routes
def routes(app: web.Application) -> None:
    r = app.router
    r.add_post(r"/api/people/{pid:[A-Za-z0-9_-]{1,40}}/probation", api_place)
    prid = r"/api/probations/{prid:\d{1,12}}"
    r.add_post(prid + "/change", api_change)
    r.add_post(prid + "/recommend", api_recommend)
    r.add_post(prid + "/decide", api_decide)
    r.add_post(prid + "/acknowledge", api_acknowledge)
    r.add_post(prid + "/separation-done", api_todo_done)


# ---------------------------------------------------------------- to-dos

def hr_queue(db, viewer) -> dict:
    """Probation work for HR: recommendations waiting, ends passed with no decision, and
    separations to do by hand (never an HR admin's own file)."""
    today = today_of(db).isoformat()
    found = {"recommended": [], "overdue": [], "separations": [], "due": []}
    due_ids = {d["probation_id"]: d["due"] for d in end_review_due(db, today_of(db))}
    for row in [dict(r) for r in db.conn.execute(
            "SELECT * FROM probations WHERE status = 'open' OR (status = 'ended' AND "
            "todo_done_at IS NULL) ORDER BY ends")]:
        if row["person_id"] == viewer.id:
            continue
        person = db.person(row["person_id"])
        item = {"id": row["id"], "person_id": row["person_id"],
                "name": person["name"] if person else row["person_id"],
                "kind": kind_word(db, row["kind"]), "ends": row["ends"]}
        if row["status"] == "ended":
            found["separations"].append(item)
        elif row["recommendation"]:
            found["recommended"].append({**item, "recommendation": OUTCOMES[row["recommendation"]]})
        elif row["ends"] < today:
            found["overdue"].append(item)
        elif row["id"] in due_ids:
            found["due"].append({**item, "due": due_ids[row["id"]]})
    return found


@server.todo
def probation_todos(svc, v) -> list[dict]:
    db = svc.db
    found = []
    for row in db.conn.execute("SELECT * FROM probations WHERE person_id = ? AND status = 'open' "
                               "AND acknowledged_at IS NULL", (v.id,)):
        if row["ends"] != row["first_ends"] or row["kind"] == DISCIPLINARY:
            found.append({"text": f"Your {kind_word(db, row['kind']).lower()} changed. Read it and "
                                  "acknowledge it.", "link": f"#/people/{v.id}"})
    if v.is_hr:
        q = hr_queue(db, v)
        for key, text in (("recommended", "probation recommendation{s} waiting for your decision"),
                          ("overdue", "probation{s} ended with no decision"),
                          ("separations", "employment ending{s} to finish by hand (archive in "
                                          "Connecteam)")):
            n = len(q[key])
            if n:
                found.append({"text": f"{n} {text.format(s='' if n == 1 else 's')}.",
                              "link": "#/discipline"})
    return found
