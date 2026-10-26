"""Discipline (design 7.3 to 7.10, and section 9's discipline rows): incidents, write-ups, HR's
approvals, signatures, appeals, voids and adjustments, the tasks a step makes, and termination
reviews. The point system itself (offences, steps, ladders, expiry) is policy.py, and the points
ledger and its arithmetic are points.py: this part only calls them by name.

The flow:
- A manager (or a director, or HR) records an incident on one form: the offence, the day, what
  happened, for attendance offences whether it was protected leave (7.10: any answer but "no"
  scores 0 and never leads to a step), and points within the offence's range, with a reason when
  they changed them. A live preview says what it means ("Maria: 4 → 6 points. Reaches Written
  warning. A write-up is needed."), and when a step is reached the same form takes the write-up.
  Until a point system is published, nothing can be recorded and the section says so.
- HR approves it (every incident at launch; the policy's approval rules can relax that), sets
  other points with a reason, or returns it to the manager with a note. Points count from HR's
  approval, and expire counting from the day it happened.
- Approving works out the step it calls for (points.required_step). Higher than the person's
  current step (or one already waiting): a task for the manager and HR, with a pre-filled
  write-up. Suspensions and termination reviews go to HR only, and a termination review is
  never automatic: HR chooses terminate (then archives them in Connecteam by hand), a last-chance
  agreement, or no action, each with a reason.
- A write-up: Draft → With HR (HR can send it back with a note) → Approved → Delivered → Active
  (signed, refused with a witness, or signed on paper with a photo) → Expired. It can be voided,
  or be under appeal. Delivery is in person (the employee reads it on the manager's device, may
  comment, types their name and confirms with their kiosk PIN, and their authenticator code if
  they use one) or through the portal (they sign there). Signing confirms receipt, not
  agreement. An unsigned one reminds the manager; it counts either way. The signed PDF is made
  through the documents part's PDF queue (queue_pdf: Google Drive makes it, retried and shown on
  HR's Needs attention page like any upload) and goes to the employee's Discipline folder in
  Google Drive (documents' "writeups" kind), as does the photo of a page signed on paper.
- Appeals within 7 days (an HR setting): upheld, amended (through adjustment lines) or voided.
  After any void or reduction, later write-ups are checked again, and HR gets a "check this"
  task where a later step would now be lower. Nothing is undone by itself.
- HR's adjustments (a person's points, one incident's points), voids, and changes to when a
  write-up stops counting each need a reason and a fresh code, and are audited.

Who sees what: HR everything; managers and directors their people's records without HR-only
notes, and nothing of suspensions or termination reviews until HR acts; employees their own
write-ups once delivered, without manager or HR notes, and their points and expired history
when the policy's switches allow. Voided records are HR's: an employee (or manager) who had the
write-up delivered sees only "Withdrawn on [date]". Text people type is sealed; dates, points
and statuses stay readable. Issued records are never rewritten: the database refuses it.
"""

import logging
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiohttp import web

from . import (access, audit, crypto, documents, files, hrdb, notify, pdfdoc, points, policy,
               probation, server, service, signin)
from .access import Refused, rule
from .probation import as_date, clean_text, hr_admins, team_managers, tell, today_of
from .server import error, read_json, read_optional, svc_of, viewer_of


log = logging.getLogger("staff_portal")

WRITEUPS = "writeups"             # documents' kind for signed write-ups (the Discipline folder)
STAFF = ("manager", "director", "hr")
NOT_SET_UP = ("The point system isn't set up yet, so incidents can't be recorded. HR loads it, "
              "tests it in the simulator and publishes it in HR settings.")
SIGN_STATEMENT = ("Signing confirms you received this write-up. It doesn't mean you agree with it. "
                  "You can add a comment, and you can appeal it within {days} days.")
SIGN_FAILED = "That PIN or code didn't work."
INCIDENT_WORDS = {"with_hr": "Waiting for HR", "returned": "Returned to the manager",
                  "approved": "Approved", "voided": "Voided"}
WRITEUP_WORDS = {"draft": "Draft", "with_hr": "With HR", "approved": "Approved: ready to deliver",
                 "delivered": "Delivered: waiting for signature", "active": "Active",
                 "expired": "Expired", "voided": "Voided"}
METHODS = {"portal": "Signed in the HR portal", "in_person": "Signed in person",
           "paper": "Signed on paper", "refused": "Refused to sign (witnessed)"}
APPEAL_OUTCOMES = {"upheld": "Upheld: no change", "amended": "Amended", "voided": "Voided"}
TERMINATION_OUTCOMES = {"terminate": "Terminate", "last_chance": "Last-chance agreement",
                        "no_action": "No action"}
TASK_WORDS = {"step": "A step is due", "suspension": "A suspension to decide",
              "termination": "Termination review", "check": "Check a later write-up",
              "separation": "Employment ending: archive them in Connecteam by hand"}
# HR settings of this part: (default, lowest, highest).
SETTINGS = {"dis_appeal_days": (7, 1, 60), "dis_sign_days": (3, 1, 30)}
TEXT_LIMIT = 4000
MAX_POINTS = 100
MAX_BACK_DAYS = 365               # an incident is recorded within a year of happening
WRITEUP_TEXTS = ("what", "policy", "improvement", "consequences", "support")

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,             -- Connecteam user ID
    offence_key TEXT NOT NULL,
    offence_name TEXT NOT NULL,
    category TEXT,
    track TEXT NOT NULL,
    happened TEXT NOT NULL,              -- YYYY-MM-DD
    what TEXT,                           -- sealed: what happened (managers and HR)
    protected TEXT NOT NULL DEFAULT 'no',    -- no, or the point system's protected leave key
    default_points REAL NOT NULL,
    manager_points REAL,                 -- the manager's choice within the range...
    manager_reason TEXT,                 -- ...and why (sealed)
    final_points REAL NOT NULL,          -- what counts
    hr_points REAL,                      -- HR's value at approval...
    hr_reason TEXT,                      -- ...and why (sealed, HR only)
    version_id TEXT,                     -- the point system version it was scored under
    probation_key TEXT,                  -- the probation policy in force that day
    status TEXT NOT NULL,                -- with_hr, returned, approved, voided
    recorded_by TEXT,
    recorded_by_name TEXT,
    recorded_at REAL NOT NULL,
    return_note TEXT,                    -- sealed (managers and HR)
    decided_by TEXT,
    decided_by_name TEXT,
    decided_at REAL,
    approved_on TEXT,                    -- the day it started to count
    entry_id TEXT,                       -- its line in the points ledger
    step_key TEXT,                       -- the step HR's approval made due
    step_reason TEXT,
    voided_by TEXT,
    voided_at REAL,
    void_reason TEXT                     -- sealed (HR only)
);
CREATE INDEX IF NOT EXISTS incidents_person ON incidents (person_id, happened);
CREATE INDEX IF NOT EXISTS incidents_status ON incidents (status);
CREATE TABLE IF NOT EXISTS writeups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,
    incident_id INTEGER,
    version_id TEXT,
    step_key TEXT,
    step_name TEXT,
    recommended_key TEXT,                -- the step the point system called for
    downgrade_reason TEXT,               -- sealed: why a lower step (managers and HR)
    amended_key TEXT,                    -- a lower step after an appeal
    amended_name TEXT,
    hr_only INTEGER NOT NULL DEFAULT 0,  -- a suspension: HR's alone until HR approves it
    status TEXT NOT NULL,                -- draft, with_hr, approved, delivered, active, voided
    what TEXT, policy TEXT, improvement TEXT, consequences TEXT, support TEXT,   -- sealed
    follow_up TEXT,                      -- YYYY-MM-DD
    suspension_from TEXT,
    suspension_to TEXT,
    suspension_paid INTEGER,
    manager_note TEXT,                   -- sealed: managers and HR
    hr_note TEXT,                        -- sealed: HR only
    return_note TEXT,                    -- sealed: HR's note when it sent it back
    created_by TEXT,
    created_by_name TEXT,
    created_at REAL NOT NULL,
    edited_by TEXT,
    edited_at REAL,
    submitted_at REAL,
    approved_by TEXT,
    approved_by_name TEXT,
    approved_at REAL,
    delivered_by TEXT,
    delivered_by_name TEXT,
    delivered_at REAL,
    method TEXT,                         -- portal, in_person, paper, refused
    issued_on TEXT,                      -- the day it was delivered
    ends_on TEXT,                        -- the last day it counts
    signed_at REAL,
    signed_name TEXT,                    -- sealed: the name the employee typed
    comment TEXT,                        -- sealed: the employee's comment
    witness_id TEXT,
    witness_name TEXT,                   -- sealed
    photo_doc_id INTEGER,                -- the signed paper page (documents)
    pdf_doc_id INTEGER,                  -- the signed PDF (documents; made through its queue)
    reminded_at REAL,
    voided_by TEXT,
    voided_at REAL,
    void_reason TEXT                     -- sealed (HR only)
);
CREATE INDEX IF NOT EXISTS writeups_person ON writeups (person_id, issued_on);
CREATE INDEX IF NOT EXISTS writeups_status ON writeups (status);
CREATE TABLE IF NOT EXISTS appeals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    writeup_id INTEGER NOT NULL,
    person_id TEXT NOT NULL,
    filed_at REAL NOT NULL,
    text TEXT,                           -- sealed: the employee's appeal
    status TEXT NOT NULL DEFAULT 'open', -- open, upheld, amended, voided
    decided_by TEXT,
    decided_by_name TEXT,
    decided_at REAL,
    note TEXT                            -- sealed: HR's decision (the employee sees it)
);
CREATE UNIQUE INDEX IF NOT EXISTS appeals_writeup ON appeals (writeup_id);
-- Work a step makes: a write-up due (managers and HR), suspensions and termination reviews (HR
-- only), "check this" after a void or reduction, and separations to do by hand.
CREATE TABLE IF NOT EXISTS discipline_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,
    kind TEXT NOT NULL,                  -- step, suspension, termination, check, separation
    step_key TEXT,
    reason TEXT,                         -- the point system's reason ("Points 5.5 ≥ 5 → Written")
    incident_id INTEGER,
    writeup_id INTEGER,
    hr_only INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open', -- open, done
    created_at REAL NOT NULL,
    done_by TEXT,
    done_at REAL,
    outcome TEXT,                        -- a termination review's outcome
    outcome_note TEXT                    -- sealed (HR only)
);
CREATE INDEX IF NOT EXISTS discipline_tasks_open ON discipline_tasks (status, person_id);
-- What happened to each incident and write-up, and who did it. Never changed.
CREATE TABLE IF NOT EXISTS discipline_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    person_id TEXT NOT NULL,
    incident_id INTEGER,
    writeup_id INTEGER,
    actor_id TEXT,
    actor TEXT,
    what TEXT NOT NULL,                  -- plain words
    points REAL,                         -- a change to an incident's points
    note TEXT,                           -- sealed
    visibility TEXT NOT NULL DEFAULT 'managers'
);
CREATE INDEX IF NOT EXISTS discipline_events_incident ON discipline_events (incident_id);
CREATE INDEX IF NOT EXISTS discipline_events_writeup ON discipline_events (writeup_id);
-- Issued records are never rewritten (design rule 5): corrections are new lines.
CREATE TRIGGER IF NOT EXISTS incidents_issued BEFORE UPDATE OF person_id, offence_key, track,
    happened, what, protected, final_points, version_id ON incidents
    WHEN OLD.status IN ('approved', 'voided')
    BEGIN SELECT RAISE(ABORT, 'approved incidents are never rewritten'); END;
CREATE TRIGGER IF NOT EXISTS writeups_issued BEFORE UPDATE OF person_id, incident_id, step_key,
    what, policy, improvement, consequences, support, follow_up, suspension_from, suspension_to,
    suspension_paid ON writeups WHEN OLD.status NOT IN ('draft', 'with_hr')
    BEGIN SELECT RAISE(ABORT, 'issued write-ups are never rewritten'); END;
CREATE TRIGGER IF NOT EXISTS incidents_kept BEFORE DELETE ON incidents
    BEGIN SELECT RAISE(ABORT, 'incidents are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS writeups_kept BEFORE DELETE ON writeups
    BEGIN SELECT RAISE(ABORT, 'write-ups are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS appeals_kept BEFORE DELETE ON appeals
    BEGIN SELECT RAISE(ABORT, 'appeals are never deleted'); END;
"""

for _kind, _title in (
        ("incident_submitted", "An incident is waiting for HR's approval."),
        ("incident_returned", "An incident was returned to you with a note."),
        ("step_due", "A discipline step is due."),
        ("termination_review", "A termination review is needed."),
        ("writeup_submitted", "A write-up is waiting for HR."),
        ("writeup_returned", "A write-up was returned to you with a note."),
        ("writeup_ready", "A write-up is ready to deliver."),
        ("writeup_to_sign", "You have a write-up to read and sign."),
        ("writeup_overdue", "A write-up hasn't been signed yet."),
        ("appeal_submitted", "An appeal is waiting for HR."),
        ("appeal_decided", "An appeal was decided."),
        ("discipline_check", "A later write-up needs checking.")):
    notify.register_kind(_kind, _title)


def setup(db) -> None:
    db.add_schema(SCHEMA)
    db.append_only("discipline_events")


def _part(svc) -> None:
    svc.on_start.append(lambda s: setup(s.db))
    svc.daily_jobs.append(nightly)


service.PARTS.append(_part)


# ---------------------------------------------------------------- small helpers

def num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fmt(value) -> str:
    """Points as people write them: 4, 5.5, 4.25."""
    return f"{round(num(value, 0.0), 2):g}"


def clean_points(value, low: float = 0, high: float = MAX_POINTS) -> float | None:
    """Points typed: whole, half or quarter points. None when blank. Raises ValueError."""
    if value is None or str(value).strip() == "":
        return None
    found = num(str(value).strip())
    if found is None or not low <= found <= high or round(found * 4) != found * 4:
        raise ValueError("Points are whole, half or quarter points, like 1, 1.5 or 0.25.")
    return found


def seal(text, column: str) -> str | None:
    return crypto.seal_text(text, f"discipline.{column}") if text else None


def opened(value, column: str) -> str | None:
    try:
        return crypto.unseal_text(value, f"discipline.{column}")
    except crypto.Damaged:
        return "(damaged)"


def setting(db, key: str) -> int:
    default, low, high = SETTINGS[key]
    try:
        return max(low, min(high, int(db.setting(key) or default)))
    except ValueError:
        return default


def first_name(person: dict) -> str:
    return (person.get("name") or "").split(" ")[0] or "They"


# ---------------------------------------------------------------- the point system (policy.py)

def version_on(db, on: date) -> tuple[str, dict] | None:
    """(version id, body) of the point system published and in force that day, or None."""
    found = policy.current(db, on)
    return (str(found["id"]), found["body"]) if found else None


def body_of(db, version_id, on: date) -> dict:
    """The body an incident was scored under (else the one in force that day)."""
    found = policy.get(db, version_id) if version_id else None
    if found:
        return found["body"]
    found = version_on(db, on)
    return found[1] if found else {}


def offences(body: dict) -> list[dict]:
    return (body or {}).get("offences") or []


def offence(body: dict, key) -> dict | None:
    return policy.offence(body or {}, key)


def steps(body: dict) -> list[dict]:
    return policy.steps(body or {})


def step(body: dict, key) -> dict | None:
    return policy.step(body or {}, key) if key else None


def rank(body: dict, key) -> int:
    return policy.rank(body or {}, key) if key else 0


def step_name(body: dict, key, fallback: str = "") -> str:
    found = step(body, key)
    return found["name"] if found else (fallback or key or "")


def is_termination(s: dict | None) -> bool:
    return bool(s and s.get("termination"))


def is_suspension(s: dict | None) -> bool:
    """An HR-only step short of a termination review (the sample's Suspension)."""
    return bool(s and s.get("hr_only") and not s.get("termination"))


def hr_only_step(s: dict | None) -> bool:
    return bool(s and (s.get("hr_only") or s.get("termination")))


def ends_on(body: dict, key, issued: date) -> str | None:
    """The last day a write-up at this step, delivered that day, counts: the day before its step
    stops being active (policy.step_end). None: it doesn't end by itself."""
    found = policy.step_end(body or {}, key, issued) if key else None
    return (found - timedelta(days=1)).isoformat() if found else None


def points_range(o: dict) -> tuple[float, float]:
    """What a manager may choose for this offence."""
    low, high = o.get("min"), o.get("max")
    return (o["points"] if low is None else low, o["points"] if high is None else high)


def track_of(body: dict, o: dict) -> str:
    if o.get("track"):
        return str(o["track"])
    tracks = (body or {}).get("tracks") or []
    return tracks[0]["key"] if tracks else "discipline"


def is_attendance(o: dict) -> bool:
    return bool(o.get("attendance"))


def needs_hr(body: dict, o: dict) -> bool:
    """HR approves before it counts (every incident at launch; the policy can relax it)."""
    return policy.needs_hr_approval(body, o["key"])


def switches(body: dict) -> tuple[bool, bool]:
    """(employees see their total, employees see expired history): the policy's switches."""
    seen = (body or {}).get("visibility") or {}
    return seen.get("employee_sees_total", True) is not False, \
        seen.get("employee_sees_expired", True) is not False


def wording(o: dict | None) -> str:
    return str((o or {}).get("wording") or "")


def leaves(body: dict) -> dict:
    """Was it protected leave (design 7.10)? "No", then the point system's list."""
    return {"no": "No", **{x["key"]: x["name"] for x in (body or {}).get("protected_leave") or []}}


def next_step_name(body: dict, key) -> str | None:
    later = [s for s in steps(body) if s["rank"] > rank(body, key)]
    return later[0]["name"] if later else None


# ---------------------------------------------------------------- points (points.py)

def totals(db, person_id, on: date) -> dict[str, float]:
    return points.totals(db, str(person_id), on)


def next_drop(db, person_id, on: date) -> dict | None:
    found = points.next_drop(db, str(person_id), on)
    return {"date": found["date"], "points": found["points"]} if found else None


def context(db, person_id, happened: date, leave: str | None = None, repeat=None) -> dict:
    """What points.py needs to know about that day: the day (for repeats), probation in force,
    protected leave, and which repeat this is (for a repeat's step)."""
    on = probation.current(db, person_id, happened)
    return {"happened": happened, "on_probation": on["policy_key"] if on else None,
            "protected": leave if leave and leave != "no" else None, "repeat": repeat}


def score(db, body: dict, person_id, offence_key: str, happened: date, leave: str,
          manager_points=None) -> dict:
    """incident_points for one incident, against the person's incidents already counting."""
    ctx = context(db, person_id, happened, leave)
    found = points.incident_points(body, offence_key, ctx, points.past_incidents(db, str(person_id)),
                                   manager_points=manager_points)
    return {"default": found["default"], "final": found["final"], "repeat": found["repeat"],
            "errors": [str(e) for e in found["errors"]], "ctx": ctx, "result": found}


def current_step(db, body: dict, person_id, on: date, *, delivered_only: bool = False,
                 leave_out=None, instead: dict | None = None) -> tuple[str | None, float]:
    """The highest step the person is on that day: delivered write-ups still counting, and
    approved ones not delivered yet (delivered_only: only those delivered by then, as escalation
    needs). leave_out: a write-up's id to leave out; instead: {write-up id: its row as it was}."""
    best, best_rank, day = None, 0.0, on.isoformat()
    rows = {w["id"]: dict(w) for w in db.conn.execute(
        "SELECT id, step_key, amended_key, status, issued_on, ends_on FROM writeups WHERE "
        "person_id = ?", (str(person_id),))}
    rows.update(instead or {})
    for w in rows.values():
        if w["id"] == leave_out or w["status"] not in ("approved", "delivered", "active"):
            continue
        if w["status"] == "approved":
            if delivered_only:
                continue
        elif not (w["issued_on"] <= day <= (w["ends_on"] or day)):
            continue
        key = w["amended_key"] or w["step_key"]
        if rank(body, key) > best_rank:
            best, best_rank = key, rank(body, key)
    return best, best_rank


def waiting_rank(db, body: dict, person_id) -> float:
    """The highest step already waiting in an open task (so one step makes one task)."""
    return max((rank(body, r[0]) for r in db.conn.execute(
        "SELECT step_key FROM discipline_tasks WHERE person_id = ? AND status = 'open' AND kind IN "
        "('step', 'suspension', 'termination')", (str(person_id),))), default=0.0)


def required(db, body: dict, person_id, offence_key: str, happened: date, on: date,
             counted: dict | None = None, repeat=None) -> tuple[str | None, str]:
    """The step this incident makes due (design 7.5), when it's higher than the person's current
    step and any already waiting: (step key or None, the point system's reason). Escalation ("a
    new incident while on Final written") goes from the step they had been given by the day it
    happened, never from a later write-up or one not delivered yet."""
    active, active_rank = current_step(db, body, person_id, on)
    then, _ = current_step(db, body, person_id, happened, delivered_only=True)
    found = points.required_step(body, context(db, person_id, happened, repeat=repeat),
                                 counted if counted is not None else totals(db, person_id, on),
                                 offence_key, active, escalate_from=then)
    key, reason = found["step"], str(found["reason"] or "")
    higher = max(active_rank, waiting_rank(db, body, person_id))
    if key and step(body, key) and rank(body, key) > higher:
        return key, reason
    return None, reason


def ends_to_points(db, w: dict, actor_id, reason: str) -> list[int]:
    """A write-up's end date, told to the points ledger (points.set_ends): under the expiry
    rules "writeup" and "first", the points that led to its step stop counting the day after its
    last. Those are its incident's, and every other point counting the day HR approved that
    incident that no other write-up covers yet (as the simulator works it out). HR changing the
    end date moves it for the same points. Returns the ledger lines added."""
    inc = get_incident(db, w["incident_id"]) if w["incident_id"] else None
    if not (inc and inc["status"] == "approved" and w["ends_on"]):
        return []
    stops = date.fromisoformat(w["ends_on"]) + timedelta(days=1)
    c = points.calc(db, w["person_id"])
    ends = [ln for ln in c.lines if ln["end_kind"] == "writeup" and not c.voided(ln, c.known)]
    mine = {ln["target_id"] for ln in ends if ln["scoring"].get("writeup") == w["id"]}
    if not mine:
        on = as_date(inc["approved_on"]) or c.known
        covered = {ln["target_id"] for ln in ends}
        mine = {ln["id"] for ln in c.bases if not c.voided(ln, c.known) and (
            ln["incident_id"] == str(inc["id"]) or (c.alive(ln, on) and ln["id"] not in covered))}
    found = []
    for target in sorted(mine):
        found += points.set_ends(db, w["person_id"], stops, actor_id, reason, target_id=target,
                                 writeup=True, scoring={"writeup": w["id"]})
    return found


# ---------------------------------------------------------------- records

def get_incident(db, incident_id) -> dict | None:
    try:
        iid = int(incident_id)
    except (TypeError, ValueError):
        return None
    row = db.conn.execute("SELECT * FROM incidents WHERE id = ?", (iid,)).fetchone()
    return dict(row) if row else None


def get_writeup(db, writeup_id) -> dict | None:
    try:
        wid = int(writeup_id)
    except (TypeError, ValueError):
        return None
    row = db.conn.execute("SELECT * FROM writeups WHERE id = ?", (wid,)).fetchone()
    return dict(row) if row else None


def get_task(db, task_id) -> dict | None:
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return None
    row = db.conn.execute("SELECT * FROM discipline_tasks WHERE id = ?", (tid,)).fetchone()
    return dict(row) if row else None


def get_appeal(db, appeal_id=None, writeup_id=None) -> dict | None:
    try:
        row = db.conn.execute("SELECT * FROM appeals WHERE id = ?", (int(appeal_id),)).fetchone() \
            if appeal_id is not None else db.conn.execute(
                "SELECT * FROM appeals WHERE writeup_id = ?", (int(writeup_id),)).fetchone()
    except (TypeError, ValueError):
        return None
    return dict(row) if row else None


def attached_writeup(db, incident_id) -> dict | None:
    """The write-up being drafted with an incident (not issued yet)."""
    row = db.conn.execute("SELECT * FROM writeups WHERE incident_id = ? AND status IN "
                          "('draft', 'with_hr') ORDER BY id DESC LIMIT 1", (incident_id,)).fetchone()
    return dict(row) if row else None


def event(db, person_id, what: str, actor_id, actor: str | None, *, incident_id=None,
          writeup_id=None, note: str | None = None, points_change=None,
          visibility: str = "managers") -> None:
    db.conn.execute(
        "INSERT INTO discipline_events (ts, person_id, incident_id, writeup_id, actor_id, actor, "
        "what, points, note, visibility) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (time.time(), str(person_id), incident_id, writeup_id, actor_id and str(actor_id), actor,
         what[:200], points_change, seal(note, "event.note"), visibility))


def events_of(db, *, incident_id=None, writeup_id=None) -> list[dict]:
    column, value = (("incident_id", incident_id) if incident_id is not None
                     else ("writeup_id", writeup_id))
    return [dict(r) for r in db.conn.execute(
        f"SELECT * FROM discipline_events WHERE {column} = ? ORDER BY id", (value,))]


def hr_only_note(e: dict) -> bool:
    """HR's reasons for changing an incident's points or a write-up's end date are HR's only, as
    in the points statement: others see what changed, not why."""
    return e["points"] is not None or e["what"].startswith("HR changed when it stops counting")


def history_view(db, rows: list[dict], rel: str) -> list[dict]:
    return [{"when": round(e["ts"]), "what": e["what"], "actor": e["actor"],
             "note": None if rel != "hr" and hr_only_note(e) else opened(e["note"], "event.note")}
            for e in access.visible(rows, rel)]


def incident_points_now(db, inc: dict) -> float:
    """Its points now, from the ledger: after HR's changes to it and any re-score."""
    if inc["status"] == "approved":
        for line in points.history(db, inc["person_id"]):
            if line["kind"] == "incident" and line["incident_id"] == str(inc["id"]):
                return line["amount"]
    return inc["final_points"]


def add_task(db, person_id, kind: str, *, step_key=None, reason: str = "", incident_id=None,
             writeup_id=None, hr_only: bool = False) -> int:
    cur = db.conn.execute(
        "INSERT INTO discipline_tasks (person_id, kind, step_key, reason, incident_id, writeup_id, "
        "hr_only, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(person_id), kind, step_key, reason[:300], incident_id, writeup_id, 1 if hr_only else 0,
         time.time()))
    db.conn.commit()
    return cur.lastrowid


def close_tasks(db, actor_id, *, writeup_id=None, incident_id=None, outcome: str = "Done") -> None:
    column, value = (("writeup_id", writeup_id) if writeup_id is not None
                     else ("incident_id", incident_id))
    db.conn.execute(f"UPDATE discipline_tasks SET status = 'done', done_by = ?, done_at = ?, "
                    f"outcome = ? WHERE {column} = ? AND status = 'open'",
                    (actor_id and str(actor_id), time.time(), outcome, value))
    db.conn.commit()


def staff_of(db, person: dict, also=()) -> list[str]:
    """Who hears about someone's discipline: their managers (and whoever else is named, while
    they still see the file). Never the person themselves."""
    found = list(team_managers(db, person))
    roles = db.all_roles()
    for pid in also:
        other = db.person(pid) if pid else None
        if not other or other["archived"] or pid in found or pid == person["id"]:
            continue
        if access.relation(access.viewer_for(db, other), person,
                           roles.get(person["id"], set())) in ("manager", "director"):
            found.append(pid)
    return found


# ---------------------------------------------------------------- recording an incident

class NotSetUp(ValueError):
    pass


def form_data(db, person: dict, today: date) -> dict:
    """What the incident form needs."""
    ver = version_on(db, today)
    if ver is None:
        return {"set_up": False, "message": NOT_SET_UP}
    _, body = ver
    return {"set_up": True, "today": today.isoformat(),
            "earliest": (today - timedelta(days=MAX_BACK_DAYS)).isoformat(),
            "person": {"id": person["id"], "name": person["name"], "first": first_name(person)},
            "offences": [{"key": o["key"], "name": o["name"], "category": o["category"],
                          "attendance": is_attendance(o), "default": o["points"],
                          "min": points_range(o)[0], "max": points_range(o)[1],
                          "wording": wording(o), "writeup_required": bool(o["writeup_required"])}
                         for o in offences(body)],
            "leaves": [{"key": k, "name": v} for k, v in leaves(body).items()],
            "steps": [{"key": s["key"], "name": s["name"], "rank": s["rank"],
                       "hr_only": hr_only_step(s)} for s in steps(body)],
            "points": totals(db, person["id"], today)}


def preview(db, person: dict, data: dict, rel: str, today: date) -> dict:
    """The live preview on the form: "Maria: 4 → 6 points. Reaches Written warning. A write-up
    is needed." Raises NotSetUp or ValueError."""
    happened = as_date(data.get("happened")) or today
    ver = version_on(db, happened) or version_on(db, today)
    if ver is None:
        raise NotSetUp(NOT_SET_UP)
    _, body = ver
    o = offence(body, str(data.get("offence") or ""))
    if o is None:
        raise ValueError("Choose the offence from the list.")
    leave = str(data.get("protected") or "no") if is_attendance(o) else "no"
    manager_points = clean_points(data.get("points"))
    s = score(db, body, person["id"], o["key"], happened, leave, manager_points)
    track = track_of(body, o)
    before = totals(db, person["id"], today)
    after = {**before, track: before.get(track, 0.0) + s["final"]}
    key, reason = predicted(db, person, body, o, happened, s, today)
    found = step(body, key)
    text = f"{first_name(person)}: {fmt(before.get(track, 0))} → {fmt(after[track])} points."
    if s["ctx"]["protected"]:
        text += " Protected leave scores 0 and never leads to a step."
    elif found and hr_only_step(found):
        text += (f" Reaches {step_name(body, key)}. HR decides what happens next." if rel == "hr"
                 else " It reaches a step HR decides on.")
    elif found:
        text += f" Reaches {step_name(body, key)}. A write-up is needed."
    else:
        text += " No new step."
    if needs_hr(body, o):
        text += " HR approves it before it counts."
    pending = db.conn.execute("SELECT COUNT(*) FROM incidents WHERE person_id = ? AND status IN "
                              "('with_hr', 'returned')", (person["id"],)).fetchone()[0]
    required_writeup = bool(o["writeup_required"])
    return {"text": text, "default": s["default"], "final": s["final"], "track": track,
            "before": before.get(track, 0.0), "after": after[track], "errors": s["errors"],
            "protected": bool(s["ctx"]["protected"]), "on_probation": s["ctx"]["on_probation"],
            "repeat": bool(s["repeat"] and s["repeat"] > 1),   # a repeat of the same offence
            "step": found and (None if hr_only_step(found) and rel != "hr" else
                               {"key": key, "name": step_name(body, key), "reason": reason}),
            "needs_writeup": bool(found and not hr_only_step(found)) or (
                required_writeup and not s["ctx"]["protected"]),
            "waiting": pending, "range": points_range(o)}


def predicted(db, person: dict, body: dict, o: dict, happened: date, s: dict,
              today: date) -> tuple[str | None, str]:
    """The step the form's incident would make due once HR approves it (the preview's guess)."""
    if s["errors"] or s["ctx"]["protected"]:
        return None, ""
    track = track_of(body, o)
    before = totals(db, person["id"], today)
    return required(db, body, person["id"], o["key"], happened, today,
                    {**before, track: before.get(track, 0.0) + s["final"]}, repeat=s["repeat"])


def _incident_fields(db, person: dict, data: dict, today: date) -> dict:
    """The form's fields, checked and scored. Raises NotSetUp or ValueError (plain words)."""
    happened = as_date(data.get("happened"))
    if happened is None:
        raise ValueError("Choose the day it happened.")
    if happened > today or happened < today - timedelta(days=MAX_BACK_DAYS):
        raise ValueError("Choose a day in the last year, not in the future.")
    ver = version_on(db, happened) or version_on(db, today)
    if ver is None:
        raise NotSetUp(NOT_SET_UP)
    version_id, body = ver
    o = offence(body, str(data.get("offence") or ""))
    if o is None:
        raise ValueError("Choose the offence from the list.")
    what = clean_text(data.get("what"), TEXT_LIMIT)
    if not what:
        raise ValueError("Say what happened.")
    leave = "no"
    if is_attendance(o):
        leave = str(data.get("protected") or "")
        if leave not in leaves(body):
            raise ValueError("Answer the question: was this protected leave?")
    manager_points = clean_points(data.get("points"))
    s = score(db, body, person["id"], o["key"], happened, leave, manager_points)
    if s["errors"]:
        raise ValueError(s["errors"][0])
    reason = clean_text(data.get("reason"), 1000)
    changed = manager_points is not None and manager_points != o["points"]   # the offence's own
    if changed and not reason and not s["ctx"]["protected"]:
        raise ValueError("Say why you chose different points.")
    return {"offence_key": o["key"], "offence_name": o["name"][:120],
            "category": str(o["category"] or "")[:60], "track": track_of(body, o),
            "happened": happened.isoformat(), "what": seal(what, "incident.what"),
            "protected": leave, "default_points": s["default"],
            "manager_points": manager_points if changed else None,
            "manager_reason": seal(reason, "incident.manager_reason") if changed else None,
            "final_points": s["final"], "version_id": version_id,
            "probation_key": s["ctx"]["on_probation"], "_body": body, "_offence": o,
            "_repeat": s["repeat"]}


WRITEUP_FIELDS = ("improvement", "consequences", "support", "manager_note")


def _writeup_texts(data: dict, body: dict, *, recommended=None) -> dict:
    """A write-up's fields from the form, checked. Raises ValueError."""
    found = {}
    for key in WRITEUP_FIELDS:
        if key in data:
            column = "manager_note" if key == "manager_note" else key
            found[column] = seal(clean_text(data.get(key), TEXT_LIMIT) or None, f"writeup.{column}")
    if "what" in data:
        found["what"] = seal(clean_text(data.get("what"), TEXT_LIMIT) or None, "writeup.what")
    if "follow_up" in data:
        found["follow_up"] = _day(data.get("follow_up"), "Type the follow-up date as a date.")
    for key in ("suspension_from", "suspension_to"):
        if key in data:
            found[key] = _day(data.get(key), "Type the suspension dates as dates.")
    if "suspension_paid" in data:
        found["suspension_paid"] = None if data.get("suspension_paid") is None else (
            1 if data.get("suspension_paid") is True else 0)
    if found.get("suspension_from") and found.get("suspension_to") and \
            found["suspension_to"] < found["suspension_from"]:
        raise ValueError("The suspension ends before it starts.")
    if "step" in data and data.get("step"):
        key = str(data["step"])
        if step(body, key) is None:
            raise ValueError("Choose the step from the list.")
        if recommended and rank(body, key) > rank(body, recommended):
            raise ValueError("A write-up can't be for a higher step than the point system calls for.")
        reason = clean_text(data.get("downgrade_reason"), 1000)
        lower = recommended and rank(body, key) < rank(body, recommended)
        if lower and not reason:
            raise ValueError("Say why a lower step than the point system calls for.")
        found["step_key"] = key
        found["step_name"] = step_name(body, key)
        found["downgrade_reason"] = seal(reason, "writeup.downgrade_reason") if lower else None
    return found


def _day(value, message: str) -> str | None:
    """A date field: None when blank, else YYYY-MM-DD. Raises ValueError."""
    if not value:
        return None
    found = as_date(value)
    if found is None:
        raise ValueError(message)
    return found.isoformat()


def record_incident(db, person: dict, viewer, rel: str, data: dict, today: date) -> dict:
    """The manager form, submitted: an incident (and its write-up, when the form took one), with
    HR, or counted at once when the point system lets this offence skip HR. Returns
    {incident, writeup, step}. Raises NotSetUp or ValueError."""
    fields = _incident_fields(db, person, data, today)
    body, o, repeat = fields.pop("_body"), fields.pop("_offence"), fields.pop("_repeat")
    hr_needed = needs_hr(body, o)
    cur = db.conn.execute(
        f"INSERT INTO incidents ({', '.join(fields)}, person_id, status, recorded_by, "
        f"recorded_by_name, recorded_at) VALUES ({', '.join('?' * len(fields))}, ?, 'with_hr', "
        "?, ?, ?)",
        (*fields.values(), person["id"], viewer.id, viewer.name, time.time()))
    iid = cur.lastrowid
    event(db, person["id"], "Recorded", viewer.id, viewer.name, incident_id=iid)
    wid = None
    wanted = data.get("writeup")
    if isinstance(wanted, dict) and fields["protected"] == "no":
        guess = {"errors": [], "ctx": {"protected": None}, "final": fields["final_points"],
                 "repeat": repeat}
        recommended, _ = predicted(db, person, body, o, as_date(fields["happened"]), guess, today)
        texts = _writeup_texts(wanted, body, recommended=recommended)
        texts.pop("step_name", None)
        wid = new_writeup(db, person, get_incident(db, iid), body,
                          texts.pop("step_key", None) or recommended, recommended=recommended,
                          created_by=viewer.id, created_by_name=viewer.name, status="with_hr",
                          texts=texts)
    db.conn.commit()
    inc = get_incident(db, iid)
    result = {"incident": iid, "writeup": wid, "step": None}
    if hr_needed:
        tell(db, hr_admins(db, exclude=(person["id"], viewer.id)), "incident_submitted",
             title=f"{person['name']}: an incident is waiting for your approval.",
             link=f"#/incident/{iid}", once=f"incident-{iid}-submitted-{time.time():.0f}")
    else:
        result.update(approve_incident(db, inc, "policy", "Point system (no HR approval needed)",
                                       today=today, auto=True))
    return result


def edit_incident(db, inc: dict, person: dict, viewer, data: dict, today: date) -> dict:
    """The manager's changes to an incident HR returned; it goes back to HR. Raises ValueError."""
    if inc["status"] != "returned":
        raise ValueError("Only an incident HR returned can be changed.")
    fields = _incident_fields(db, person, data, today)
    body = fields.pop("_body")
    fields.pop("_offence"), fields.pop("_repeat")
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.conn.execute(f"UPDATE incidents SET {sets}, status = 'with_hr' WHERE id = ?",
                    (*fields.values(), inc["id"]))
    event(db, person["id"], "Changed and sent to HR again", viewer.id, viewer.name,
          incident_id=inc["id"])
    w = attached_writeup(db, inc["id"])
    if w and isinstance(data.get("writeup"), dict):
        texts = _writeup_texts(data["writeup"], body, recommended=w["recommended_key"])
        _update_writeup(db, w, texts, viewer)
    if w:
        db.conn.execute("UPDATE writeups SET status = 'with_hr', submitted_at = ? WHERE id = ?",
                        (time.time(), w["id"]))
    db.conn.commit()
    tell(db, hr_admins(db, exclude=(person["id"], viewer.id)), "incident_submitted",
         title=f"{person['name']}: a changed incident is waiting for your approval.",
         link=f"#/incident/{inc['id']}", once=f"incident-{inc['id']}-again-{time.time():.0f}")
    return get_incident(db, inc["id"])


def approve_incident(db, inc: dict, actor_id, actor: str, *, hr_points=None, hr_reason: str = "",
                     today: date, auto: bool = False) -> dict:
    """HR's approval: the points count from today (expiring from the day it happened), and the
    step it makes due becomes a task. Returns {step, task, writeup}. Raises ValueError."""
    if inc["status"] != "with_hr":
        raise ValueError("It isn't waiting for HR.")
    person = db.person(inc["person_id"])
    happened = date.fromisoformat(inc["happened"])
    body = body_of(db, inc["version_id"], happened)
    if not body:
        raise NotSetUp(NOT_SET_UP)
    # Scored again now, against the incidents counting (which repeat this is), for the ledger
    # (so a re-score can work it out again) and for the step it makes due. What counts is this
    # score, not the one from when it was recorded: incidents HR approved since then can make it
    # a repeat.
    s = score(db, body, inc["person_id"], inc["offence_key"], happened, inc["protected"],
              inc["manager_points"])
    recorded = inc["final_points"]
    scored = recorded if s["errors"] else s["final"]
    final = scored
    if hr_points is not None and hr_points != scored:
        if inc["protected"] != "no":
            raise ValueError("Protected leave always scores 0.")
        if not hr_reason:
            raise ValueError("Say why you changed the points.")
        final = hr_points
    changed = final != scored
    entry = points.add_incident_entry(
        db, inc["person_id"], inc["id"], inc["track"], final, happened, today, inc["version_id"],
        actor_id, f"Incident: {inc['offence_name']}", offence=inc["offence_key"],
        scoring={**s["result"], "manager": inc["manager_points"], "hr": final if changed else None,
                 "on_probation": s["ctx"]["on_probation"]})
    db.conn.execute(
        "UPDATE incidents SET status = 'approved', default_points = ?, final_points = ?, "
        "hr_points = ?, hr_reason = ?, decided_by = ?, decided_by_name = ?, decided_at = ?, "
        "approved_on = ?, entry_id = ? WHERE id = ?",
        (inc["default_points"] if s["errors"] else s["default"], final,
         hr_points if changed else None, seal(hr_reason, "incident.hr_reason") if changed else None,
         str(actor_id), actor, time.time(), today.isoformat(),
         str(entry) if entry is not None else None,
         inc["id"]))
    what = ("Counted (the point system needs no HR approval for it)" if auto else "Approved by HR") + (
        f" at {fmt(final)} points, changed from {fmt(scored)}" if changed else f" at {fmt(final)} points")
    if scored != recorded:
        what += f" (the rules give {fmt(scored)} now, {fmt(recorded)} when it was recorded)"
    event(db, inc["person_id"], what, actor_id, actor, incident_id=inc["id"])
    db.conn.commit()
    return evaluate_step(db, person, get_incident(db, inc["id"]), actor_id, actor, today,
                         repeat=s["repeat"])


def evaluate_step(db, person: dict, inc: dict, actor_id, actor: str, today: date,
                  repeat=None) -> dict:
    """After approval: the step it makes due (design 7.5), as a task with a pre-filled write-up;
    suspensions and termination reviews to HR only."""
    happened = date.fromisoformat(inc["happened"])
    body = body_of(db, inc["version_id"], happened)
    attached = attached_writeup(db, inc["id"])
    key, reason = (None, "Protected leave: no step.") if inc["protected"] != "no" else required(
        db, body, inc["person_id"], inc["offence_key"], happened, today, repeat=repeat)
    o = offence(body, inc["offence_key"]) or {}
    if not key and attached and o.get("writeup_required"):
        key = o.get("min_step") or (steps(body)[0]["key"] if steps(body) else None)
        reason = "This offence always needs a write-up."
    db.conn.execute("UPDATE incidents SET step_key = ?, step_reason = ? WHERE id = ?",
                    (key, reason[:300], inc["id"]))
    db.conn.commit()
    result = {"step": key, "task": None, "writeup": None}
    if not key:
        if attached:
            void_draft(db, attached, "Not needed: no step was reached.", actor_id, actor)
        return result
    s = step(body, key) or {"key": key}
    name = step_name(body, key)
    hr_people = hr_admins(db, exclude=(person["id"],))
    if is_termination(s):
        if attached:
            void_draft(db, attached, "Not needed: HR holds a termination review instead.",
                       actor_id, actor)
        task = add_task(db, person["id"], "termination", step_key=key, reason=reason,
                        incident_id=inc["id"], hr_only=True)
        tell(db, hr_people, "termination_review", urgent=True, link="#/discipline",
             title=f"{person['name']}: a termination review is needed. {reason}".strip(),
             once=f"task-{task}")
        return {**result, "task": task}
    hr_only = hr_only_step(s)
    if attached:
        proposed = attached["step_key"]
        keep = (proposed and attached["downgrade_reason"] and step(body, proposed)
                and rank(body, proposed) < rank(body, key))
        db.conn.execute(
            "UPDATE writeups SET step_key = ?, step_name = ?, recommended_key = ?, "
            "hr_only = ?, version_id = ?, downgrade_reason = ? WHERE id = ?",
            (proposed if keep else key, step_name(body, proposed if keep else key), key,
             1 if hr_only else 0,
             inc["version_id"], attached["downgrade_reason"] if keep else None, attached["id"]))
        db.conn.commit()
        wid = attached["id"]
    else:
        owner = None if hr_only else inc["recorded_by"]
        wid = new_writeup(db, person, inc, body, key, created_by=owner,
                          created_by_name=inc["recorded_by_name"] if owner else None,
                          hr_only=hr_only)
    task = add_task(db, person["id"], "suspension" if hr_only else "step", step_key=key,
                    reason=reason, incident_id=inc["id"], writeup_id=wid, hr_only=hr_only)
    who = hr_people if hr_only else staff_of(db, person, (inc["recorded_by"],)) + hr_people
    tell(db, who, "step_due", urgent=True, link=f"#/writeup/{wid}", once=f"task-{task}",
         title=f"{person['name']}: {name} is due. {reason}".strip())
    return {**result, "task": task, "writeup": wid}


def return_incident(db, inc: dict, note: str, actor_id, actor: str) -> None:
    if inc["status"] != "with_hr":
        raise ValueError("It isn't waiting for HR.")
    if not note:
        raise ValueError("Write a note for the manager.")
    db.conn.execute("UPDATE incidents SET status = 'returned', return_note = ? WHERE id = ?",
                    (seal(note, "incident.return_note"), inc["id"]))
    db.conn.execute("UPDATE writeups SET status = 'draft', return_note = ? WHERE incident_id = ? "
                    "AND status = 'with_hr'", (seal(note, "writeup.return_note"), inc["id"]))
    event(db, inc["person_id"], "Returned to the manager", actor_id, actor, incident_id=inc["id"],
          note=note)
    db.conn.commit()
    person = db.person(inc["person_id"]) or {"name": ""}
    tell(db, [inc["recorded_by"]], "incident_returned", link=f"#/incident/{inc['id']}",
         title=f"{person['name']}: HR returned an incident to you with a note.",
         once=f"incident-{inc['id']}-returned-{time.time():.0f}")


def void_incident(db, inc: dict, reason: str, actor_id, actor: str, today: date) -> None:
    """HR voids an incident (a fresh code first): its points stop counting (a void line), any
    draft write-up with it goes, its open tasks close, and later write-ups are checked."""
    if inc["status"] == "voided":
        raise ValueError("It's already voided.")
    if not reason:
        raise ValueError("Say why.")
    lines = []
    if inc["status"] == "approved":
        lines = points.void(db, person_id=inc["person_id"], incident_id=inc["id"],
                            actor_id=actor_id, reason=reason)
    db.conn.execute("UPDATE incidents SET status = 'voided', voided_by = ?, voided_at = ?, "
                    "void_reason = ? WHERE id = ?",
                    (str(actor_id), time.time(), seal(reason, "incident.void_reason"), inc["id"]))
    event(db, inc["person_id"], "Voided", actor_id, actor, incident_id=inc["id"], note=reason,
          visibility="hr")
    db.conn.commit()
    w = attached_writeup(db, inc["id"])
    if w:
        void_draft(db, w, "Its incident was voided.", actor_id, actor)
    close_tasks(db, actor_id, incident_id=inc["id"], outcome="Incident voided")
    if inc["status"] == "approved":
        recheck_later(db, inc["person_id"], inc["happened"], actor_id, actor, today,
                      cause=f"An incident from {inc['happened']} was voided.", lines=lines)


def adjust_incident(db, inc: dict, new_points: float, reason: str, actor_id, actor: str,
                    today: date) -> float:
    """HR changes one incident's points: an adjustment line, so the original stays visible."""
    if inc["status"] != "approved":
        raise ValueError("Only an approved incident's points can be changed. Approve it first.")
    if inc["protected"] != "no":
        raise ValueError("Protected leave always scores 0.")
    if not reason:
        raise ValueError("Say why.")
    change = new_points - incident_points_now(db, inc)
    if change == 0:
        raise ValueError("That's the points it already has.")
    # It counts from today; the ledger ends it with its incident (incident_id), and a re-score
    # keeps HR's points (scoring "hr").
    line = points.adjust(db, person_id=inc["person_id"], track=inc["track"], points=change,
                         starts=today, ends=None, actor_id=actor_id, reason=reason,
                         incident_id=inc["id"], scoring={"hr": new_points})
    event(db, inc["person_id"], f"Points changed by HR to {fmt(new_points)}", actor_id, actor,
          incident_id=inc["id"], note=reason, points_change=change, visibility="managers")
    db.conn.commit()
    if change < 0:
        recheck_later(db, inc["person_id"], inc["happened"], actor_id, actor, today,
                      cause=f"An incident from {inc['happened']} now has fewer points.", lines=[line])
    return change


def adjust_person(db, person: dict, data: dict, actor_id, actor: str, today: date) -> None:
    """HR adds or takes away points for someone, with a reason, a start and an optional end."""
    if version_on(db, today) is None:
        raise NotSetUp(NOT_SET_UP)
    change = clean_points(data.get("points"), -MAX_POINTS, MAX_POINTS)
    if not change:
        raise ValueError("Type the points to add (or take away, with a minus sign).")
    starts = as_date(data.get("starts")) or today
    ends = as_date(data.get("ends")) if data.get("ends") else None
    if ends is not None and ends < starts:
        raise ValueError("It stops counting before it starts.")
    reason = clean_text(data.get("reason"), 1000)
    if not reason:
        raise ValueError("Say why.")
    body = version_on(db, today)[1]
    tracks = [t["key"] for t in body["tracks"]]
    track = str(data.get("track") or tracks[0])
    if track not in tracks:
        raise ValueError("Choose the point total.")
    line = points.adjust(db, person_id=person["id"], track=track, points=change, starts=starts,
                         ends=ends, actor_id=actor_id, reason=reason, incident_id=None)
    if change < 0:
        recheck_later(db, person["id"], starts.isoformat(), actor_id, actor, today,
                      cause="HR took points away.", lines=[line])


def recheck_later(db, person_id, since: str, actor_id, actor: str, today: date, *, cause: str,
                  lines=(), was: dict | None = None) -> int:
    """After a void or reduction: each later write-up whose step would now be lower becomes a
    "check this" task for HR. Nothing is undone by itself. Its step is worked out twice, as at its
    incident's approval (the totals that day, the step they were on, which repeat it was): as
    things were before this change, and as if the change had always been so. lines: the ledger
    lines the change added (they count from today, so they're moved back to when the line they
    change started counting); was: a write-up's row as it was before the change (voided, lowered
    or ending earlier). Only a step lower than both that "before" and the write-up's own is
    flagged. Returns how many."""
    person = db.person(person_id) or {"id": person_id, "name": person_id}
    new = {int(i) for i in lines}
    every = points.lines_of(db, person_id)
    started = {ln["id"]: ln["counts_from"] for ln in every}
    bodies = points.bodies_for(db, every)
    before = points.Calc([ln for ln in every if ln["id"] not in new], bodies, today)
    after = points.Calc([{**ln, "counts_from": min(ln["counts_from"], started.get(ln["target_id"],
                                                                                  ln["counts_from"]))}
                         if ln["id"] in new else ln for ln in every], bodies, today)
    repeats = {ln["incident_id"]: ln["scoring"].get("repeat") for ln in every
               if ln["kind"] == "incident"}
    count = 0
    for w in [dict(r) for r in db.conn.execute(
            "SELECT * FROM writeups WHERE person_id = ? AND status IN ('approved', 'delivered', "
            "'active') AND COALESCE(issued_on, ?) >= ? ORDER BY id",
            (str(person_id), today.isoformat(), since))]:
        inc = get_incident(db, w["incident_id"]) if w["incident_id"] else None
        if inc is None or inc["status"] != "approved":
            continue
        happened = date.fromisoformat(inc["happened"])
        body = body_of(db, w["version_id"] or inc["version_id"], happened)
        on = as_date(inc["approved_on"]) or happened
        ctx = context(db, person_id, happened, inc["protected"], repeats.get(str(inc["id"])))

        def step_from(calc, instead):
            active, _ = current_step(db, body, person_id, on, leave_out=w["id"], instead=instead)
            then, _ = current_step(db, body, person_id, happened, delivered_only=True,
                                   leave_out=w["id"], instead=instead)
            counted = {t: policy.as_points(q) for t, q in calc.totals(on).items()}
            return points.required_step(body, ctx, counted, inc["offence_key"], active,
                                        escalate_from=then)["step"]
        name = f"{w['amended_name'] or w['step_name']} from {w['issued_on'] or 'its approval'}"
        try:
            was_key = step_from(before, {was["id"]: was} if was else None)
            now_key = step_from(after, None)
        except Exception as err:  # noqa: BLE001 - HR checks it by hand instead
            log.warning("HR portal: checking a later write-up failed: %r", err)
            now_key = None
            reason = f"{cause} The {name} couldn't be checked again: check it by hand."
        else:
            own = w["amended_key"] or w["step_key"]
            if rank(body, now_key) >= min(rank(body, was_key), rank(body, own)):
                continue
            reason = (f"{cause} The {name} may now be too high: "
                      f"{step_name(body, now_key) or 'no step'} instead.")
        task = add_task(db, person_id, "check", step_key=now_key, reason=reason,
                        incident_id=inc["id"], writeup_id=w["id"], hr_only=True)
        tell(db, hr_admins(db, exclude=(person_id,)), "discipline_check", link="#/discipline",
             title=f"{person['name']}: a later write-up needs checking.", once=f"task-{task}")
        count += 1
    return count


# ---------------------------------------------------------------- write-ups

def new_writeup(db, person: dict, inc: dict | None, body: dict, key, *, created_by,
                created_by_name, status: str = "draft", hr_only: bool = False,
                texts: dict | None = None, recommended=None) -> int:
    """A write-up pre-filled from its incident: what happened, the policy broken (quoted from
    the handbook wording), and what a repeat leads to."""
    o = offence(body, inc["offence_key"]) if inc else None
    nxt = next_step_name(body, key) if key else None
    fields = {"what": seal(opened(inc["what"], "incident.what") if inc else None, "writeup.what"),
              "policy": seal(wording(o) or None, "writeup.policy"),
              "consequences": seal(f"Another incident may lead to the next step: {nxt}." if nxt
                                   else None, "writeup.consequences")}
    fields.update(texts or {})
    downgrade = fields.pop("downgrade_reason", None)
    fields.pop("step_name", None)
    cur = db.conn.execute(
        f"INSERT INTO writeups ({', '.join(fields)}, person_id, incident_id, version_id, step_key, "
        "step_name, recommended_key, downgrade_reason, hr_only, status, created_by, "
        f"created_by_name, created_at, submitted_at) VALUES ({', '.join('?' * len(fields))}, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (*fields.values(), person["id"], inc["id"] if inc else None, inc and inc["version_id"],
         key, step_name(body, key) if key else None, recommended or key, downgrade,
         1 if hr_only else 0, status, created_by, created_by_name, time.time(),
         time.time() if status == "with_hr" else None))
    event(db, person["id"], "Write-up started" if status == "draft" else "Write-up sent to HR",
          created_by, created_by_name or "HR portal", writeup_id=cur.lastrowid,
          incident_id=inc["id"] if inc else None)
    db.conn.commit()
    return cur.lastrowid


def _update_writeup(db, w: dict, texts: dict, viewer) -> None:
    """Saves the changes. Whoever changes what it says is one of its authors (so they don't
    approve it); HR's own note doesn't count."""
    if not texts:
        return
    sets = ", ".join(f"{k} = ?" for k in texts)
    author = set(texts) != {"hr_note"}
    db.conn.execute(f"UPDATE writeups SET {sets}, edited_by = CASE WHEN ? THEN ? ELSE edited_by END, "
                    "edited_at = ? WHERE id = ?",
                    (*texts.values(), author, viewer.id, time.time(), w["id"]))


def edit_writeup(db, w: dict, data: dict, viewer, rel: str) -> None:
    """Changes to a draft (managers, directors, HR). HR's own note can change until it's voided."""
    hr_note = "hr_note" in data and rel == "hr"
    if w["status"] == "voided" or (w["status"] != "draft" and not hr_note):
        raise ValueError("Only a draft can be changed. HR sends it back first.")
    texts = {}
    if w["status"] == "draft":
        body = body_of(db, w["version_id"], today_of(db))
        texts = _writeup_texts(data, body, recommended=w["recommended_key"])
        if texts.get("suspension_from") or texts.get("suspension_to"):
            if not is_suspension(step(body, texts.get("step_key") or w["step_key"])):
                raise ValueError("Suspension dates are only for a suspension.")
    if hr_note:
        texts["hr_note"] = seal(clean_text(data.get("hr_note"), TEXT_LIMIT) or None, "writeup.hr_note")
    _update_writeup(db, w, texts, viewer)
    if texts:
        event(db, w["person_id"], "Write-up changed", viewer.id, viewer.name, writeup_id=w["id"],
              visibility="hr" if set(texts) <= {"hr_note"} else "managers")
    db.conn.commit()


def submit_writeup(db, w: dict, viewer, person: dict) -> None:
    if w["status"] != "draft":
        raise ValueError("It isn't a draft.")
    if not w["step_key"]:
        raise ValueError("It waits for HR to approve its incident first.")
    for key, label in (("what", "what happened"), ("improvement", "the expected improvement"),
                       ("consequences", "the consequences of a repeat")):
        if not opened(w[key], f"writeup.{key}"):
            raise ValueError(f"Fill in {label}.")
    db.conn.execute("UPDATE writeups SET status = 'with_hr', submitted_at = ? WHERE id = ?",
                    (time.time(), w["id"]))
    event(db, w["person_id"], "Sent to HR", viewer.id, viewer.name, writeup_id=w["id"])
    db.conn.commit()
    tell(db, hr_admins(db, exclude=(person["id"], viewer.id)), "writeup_submitted",
         link=f"#/writeup/{w['id']}", title=f"{person['name']}: a write-up is waiting for HR.",
         once=f"writeup-{w['id']}-submitted-{time.time():.0f}")


def approve_writeup(db, w: dict, viewer, person: dict) -> None:
    if w["status"] != "with_hr":
        raise ValueError("It isn't waiting for HR.")
    access.not_own_work(viewer, w["created_by"])
    access.not_own_work(viewer, w["edited_by"])
    inc = get_incident(db, w["incident_id"]) if w["incident_id"] else None
    if (inc and inc["status"] != "approved") or not w["step_key"]:
        raise ValueError("Approve its incident first.")
    body = body_of(db, w["version_id"], today_of(db))
    db.conn.execute("UPDATE writeups SET status = 'approved', hr_only = 0, approved_by = ?, "
                    "approved_by_name = ?, approved_at = ?, return_note = NULL WHERE id = ?",
                    (viewer.id, viewer.name, time.time(), w["id"]))
    lower = w["recommended_key"] and rank(body, w["step_key"]) < rank(body, w["recommended_key"])
    event(db, w["person_id"], "Approved by HR" + (
        f" as a lower step than the point system called for ({step_name(body, w['recommended_key'])}): "
        "a downgrade" if lower else ""), viewer.id, viewer.name, writeup_id=w["id"],
          note=opened(w["downgrade_reason"], "writeup.downgrade_reason") if lower else None)
    db.conn.commit()
    tell(db, staff_of(db, person, (w["created_by"],)), "writeup_ready", link=f"#/writeup/{w['id']}",
         title=f"{person['name']}: a write-up is ready to deliver.", once=f"writeup-{w['id']}-ready")


def return_writeup(db, w: dict, note: str, viewer, person: dict) -> None:
    if w["status"] != "with_hr":
        raise ValueError("It isn't waiting for HR.")
    if not note:
        raise ValueError("Write a note for the manager.")
    db.conn.execute("UPDATE writeups SET status = 'draft', return_note = ? WHERE id = ?",
                    (seal(note, "writeup.return_note"), w["id"]))
    event(db, w["person_id"], "Returned to the manager", viewer.id, viewer.name,
          writeup_id=w["id"], note=note)
    db.conn.commit()
    tell(db, staff_of(db, person, (w["created_by"], w["edited_by"])), "writeup_returned",
         link=f"#/writeup/{w['id']}", title=f"{person['name']}: HR returned a write-up with a note.",
         once=f"writeup-{w['id']}-returned-{time.time():.0f}")


def void_draft(db, w: dict, reason: str, actor_id, actor: str) -> None:
    db.conn.execute("UPDATE writeups SET status = 'voided', voided_by = ?, voided_at = ?, "
                    "void_reason = ? WHERE id = ?", (actor_id and str(actor_id), time.time(),
                                                     seal(reason, "writeup.void_reason"), w["id"]))
    event(db, w["person_id"], "Draft dropped", actor_id, actor, writeup_id=w["id"], note=reason,
          visibility="hr")
    db.conn.commit()
    close_tasks(db, actor_id, writeup_id=w["id"], outcome="Write-up not needed")


def _deliver_fields(db, w: dict, viewer, method: str, today: date) -> dict:
    """The delivery details: the end date counts from the day it's delivered, as long as its
    step stays active (policy.step_end)."""
    fields = {"method": method}
    if not w["delivered_at"]:
        body = body_of(db, w["version_id"], today)
        fields.update(delivered_by=viewer.id, delivered_by_name=viewer.name, delivered_at=time.time(),
                      issued_on=today.isoformat(), ends_on=ends_on(body, w["step_key"], today))
    return fields


def _set(db, w: dict, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.conn.execute(f"UPDATE writeups SET {sets} WHERE id = ?", (*fields.values(), w["id"]))


def send_to_sign(db, w: dict, viewer, person: dict, today: date) -> None:
    """Delivered through the portal: the employee reads and signs it there."""
    if w["status"] != "approved":
        raise ValueError("It isn't ready to deliver.")
    _set(db, w, status="delivered", **_deliver_fields(db, w, viewer, "portal", today))
    event(db, w["person_id"], "Delivered: sent to the employee to sign in the HR portal", viewer.id,
          viewer.name, writeup_id=w["id"], visibility="employee")
    db.conn.commit()
    ends_to_points(db, get_writeup(db, w["id"]), viewer.id, "The write-up's end date")
    close_tasks(db, viewer.id, writeup_id=w["id"], outcome="Write-up delivered")
    tell(db, [person["id"]], "writeup_to_sign", link=f"#/writeup/{w['id']}",
         title="You have a write-up to read and sign.", once=f"writeup-{w['id']}-sign")


def made_active(db, w: dict, viewer, method: str, today: date, what: str, **fields) -> None:
    """Signed, refused with a witness, or signed on paper: it's active, and its signed PDF is
    asked of the documents part's queue (made now, or once Google Drive can)."""
    if w["status"] not in ("approved", "delivered"):
        raise ValueError("It isn't waiting for a signature.")
    first_time = not w["delivered_at"]
    _set(db, w, status="active", **_deliver_fields(db, w, viewer, method, today), **fields)
    event(db, w["person_id"], what, viewer.id, viewer.name, writeup_id=w["id"],
          visibility="employee")
    db.conn.commit()
    if first_time:
        ends_to_points(db, get_writeup(db, w["id"]), viewer.id, "The write-up's end date")
    close_tasks(db, viewer.id, writeup_id=w["id"], outcome="Write-up delivered")
    documents.queue_pdf(db, WRITEUPS, w["person_id"], w["id"])


def void_writeup(db, w: dict, reason: str, actor_id, actor: str, today: date, *,
                 with_incident: bool = False) -> None:
    """HR voids a write-up (a fresh code first). With its incident, its points stop too. Later
    write-ups are checked again."""
    if w["status"] == "voided":
        raise ValueError("It's already voided.")
    if not reason:
        raise ValueError("Say why.")
    db.conn.execute("UPDATE writeups SET status = 'voided', voided_by = ?, voided_at = ?, "
                    "void_reason = ? WHERE id = ?",
                    (str(actor_id), time.time(), seal(reason, "writeup.void_reason"), w["id"]))
    event(db, w["person_id"], "Voided", actor_id, actor, writeup_id=w["id"], note=reason,
          visibility="hr")
    db.conn.commit()
    close_tasks(db, actor_id, writeup_id=w["id"], outcome="Write-up voided")
    inc = get_incident(db, w["incident_id"]) if w["incident_id"] else None
    if with_incident and inc and inc["status"] != "voided":
        void_incident(db, inc, reason, actor_id, actor, today)
    elif w["status"] in ("delivered", "active"):
        recheck_later(db, w["person_id"], w["issued_on"], actor_id, actor, today,
                      cause=f"The write-up from {w['issued_on']} was voided.", was=w)


def change_ends(db, w: dict, new_end: date, reason: str, actor_id, actor: str, today: date) -> None:
    if w["status"] not in ("delivered", "active"):
        raise ValueError("Only a delivered write-up has an end date.")
    if not reason:
        raise ValueError("Say why.")
    if new_end < date.fromisoformat(w["issued_on"]):
        raise ValueError("It can't stop counting before it was delivered.")
    db.conn.execute("UPDATE writeups SET ends_on = ? WHERE id = ?", (new_end.isoformat(), w["id"]))
    event(db, w["person_id"], f"HR changed when it stops counting: {w['ends_on']} → {new_end}",
          actor_id, actor, writeup_id=w["id"], note=reason)
    db.conn.commit()
    lines = ends_to_points(db, get_writeup(db, w["id"]), actor_id, reason)
    if new_end.isoformat() < (w["ends_on"] or "9999"):
        recheck_later(db, w["person_id"], w["issued_on"], actor_id, actor, today,
                      cause=f"The write-up from {w['issued_on']} now stops counting earlier.",
                      lines=lines, was=w)


# ---------------------------------------------------------------- appeals

def appeal_until(db, w: dict) -> str | None:
    if not w["issued_on"]:
        return None
    days = setting(db, "dis_appeal_days")
    return (date.fromisoformat(w["issued_on"]) + timedelta(days=days)).isoformat()


def file_appeal(db, w: dict, person: dict, text: str, today: date) -> int:
    if w["status"] not in ("delivered", "active"):
        raise ValueError("This write-up can't be appealed.")
    if get_appeal(db, writeup_id=w["id"]):
        raise ValueError("You've already appealed this write-up.")
    if today.isoformat() > (appeal_until(db, w) or ""):
        raise ValueError(f"Appeals are made within {setting(db, 'dis_appeal_days')} days of "
                         "receiving the write-up.")
    if not text:
        raise ValueError("Say why you're appealing.")
    cur = db.conn.execute("INSERT INTO appeals (writeup_id, person_id, filed_at, text) VALUES "
                          "(?, ?, ?, ?)",
                          (w["id"], person["id"], time.time(), seal(text, "appeal.text")))
    event(db, person["id"], "Appealed by the employee", person["id"], person["name"],
          writeup_id=w["id"], visibility="employee")
    db.conn.commit()
    tell(db, hr_admins(db, exclude=(person["id"],)), "appeal_submitted", link=f"#/writeup/{w['id']}",
         title=f"{person['name']} appealed a write-up.", once=f"appeal-{cur.lastrowid}")
    return cur.lastrowid


def decide_appeal(db, appeal: dict, w: dict, outcome: str, note: str, viewer, today: date, *,
                  new_points=None, new_step=None) -> None:
    """HR's decision on an appeal, with a note the employee sees: upheld (no change), amended
    (points through an adjustment line, or a lower step) or voided (the write-up and its
    incident stop counting). Raises ValueError, or Refused for HR's own work."""
    if appeal["status"] != "open":
        raise ValueError("It's already decided.")
    for author in (w["created_by"], w["edited_by"], w["approved_by"]):
        access.not_own_work(viewer, author)
    if outcome not in APPEAL_OUTCOMES:
        raise ValueError("Choose upheld, amended or voided.")
    if not note:
        raise ValueError("Write a note: the employee and their manager see it.")
    person = db.person(w["person_id"])
    inc = get_incident(db, w["incident_id"]) if w["incident_id"] else None
    if outcome == "amended":
        if new_points is None and not new_step:
            raise ValueError("Change the points or the step, or choose upheld.")
        body = body_of(db, w["version_id"], today)
        if new_step:
            current = w["amended_key"] or w["step_key"]
            if step(body, new_step) is None or rank(body, new_step) >= rank(body, current):
                raise ValueError("Choose a lower step.")
        if new_points is not None:
            if inc is None or inc["status"] != "approved":
                raise ValueError("Its incident's points can't be changed.")
            adjust_incident(db, inc, new_points, f"Appeal: {note}", viewer.id, viewer.name, today)
        if new_step:
            db.conn.execute("UPDATE writeups SET amended_key = ?, amended_name = ? WHERE id = ?",
                            (new_step, step_name(body, new_step), w["id"]))
            event(db, w["person_id"], f"Step changed on appeal to {step_name(body, new_step)}",
                  viewer.id, viewer.name, writeup_id=w["id"], visibility="employee")
    db.conn.execute("UPDATE appeals SET status = ?, decided_by = ?, decided_by_name = ?, "
                    "decided_at = ?, note = ? WHERE id = ?",
                    (outcome, viewer.id, viewer.name, time.time(), seal(note, "appeal.note"),
                     appeal["id"]))
    event(db, w["person_id"], f"Appeal decided: {APPEAL_OUTCOMES[outcome]}", viewer.id, viewer.name,
          writeup_id=w["id"], note=note, visibility="employee")
    db.conn.commit()
    if outcome == "voided":
        void_writeup(db, get_writeup(db, w["id"]), f"Appeal: {note}", viewer.id, viewer.name, today,
                     with_incident=True)
    elif outcome == "amended" and new_step:
        recheck_later(db, w["person_id"], w["issued_on"], viewer.id, viewer.name, today,
                      cause=f"The write-up from {w['issued_on']} was lowered on appeal.", was=w)
    link, once = f"#/writeup/{w['id']}", f"appeal-{appeal['id']}-decided"
    tell(db, [w["person_id"]], "appeal_decided", link=link, once=once,
         title="HR decided your appeal. Open it to read the decision.")
    tell(db, staff_of(db, person, (w["created_by"], w["delivered_by"])), "appeal_decided",
         link=link, once=once, title=f"{person['name']}: HR decided an appeal.")


def decide_termination(db, task: dict, outcome: str, reason: str, viewer) -> None:
    """A termination review is never automatic: HR chooses, with a reason."""
    if task["kind"] != "termination" or task["status"] != "open":
        raise ValueError("There's no termination review open here.")
    if outcome not in TERMINATION_OUTCOMES:
        raise ValueError("Choose terminate, a last-chance agreement or no action.")
    if not reason:
        raise ValueError("Say why.")
    db.conn.execute("UPDATE discipline_tasks SET status = 'done', done_by = ?, done_at = ?, "
                    "outcome = ?, outcome_note = ? WHERE id = ?",
                    (viewer.id, time.time(), outcome, seal(reason, "task.outcome_note"), task["id"]))
    event(db, task["person_id"], f"Termination review: {TERMINATION_OUTCOMES[outcome]}", viewer.id,
          viewer.name, incident_id=task["incident_id"], note=reason, visibility="hr")
    db.conn.commit()
    if outcome == "terminate":
        add_task(db, task["person_id"], "separation", reason="Terminated after a termination "
                 "review: archive them in Connecteam by hand.", incident_id=task["incident_id"],
                 hr_only=True)


# ---------------------------------------------------------------- the signed PDF

def pdf_blocks(db, w: dict) -> tuple[str, list]:
    """The signed write-up as plain blocks for pdfdoc.page: only what the employee sees (no
    manager or HR notes)."""
    person = db.person(w["person_id"]) or {"name": w["person_id"]}
    inc = get_incident(db, w["incident_id"]) if w["incident_id"] else None
    name = w["amended_name"] or w["step_name"] or "Write-up"
    about = (inc["category"] or inc["offence_name"]) if inc else ""
    title = f"{name}{' - ' + about if about else ''} (signed)"
    text = {k: opened(w[k], f"writeup.{k}") or "" for k in WRITEUP_TEXTS}
    blocks = [("heading", name), ("row", "Employee", person["name"]),
              ("row", "Delivered", w["issued_on"] or ""), ("row", "Counts until", w["ends_on"] or "")]
    if inc:
        blocks.append(("row", "Offence", inc["offence_name"]))
    for key, label in (("what", "What happened"), ("policy", "Policy"),
                       ("improvement", "Expected improvement"),
                       ("consequences", "If it happens again"), ("support", "Support offered")):
        if text[key]:
            blocks += [("heading", label), ("paragraph", text[key])]
    if w["follow_up"]:
        blocks.append(("row", "Follow-up date", w["follow_up"]))
    if w["suspension_from"]:
        blocks.append(("row", "Suspension", f"{w['suspension_from']} to {w['suspension_to'] or ''}, "
                       + ("paid" if w["suspension_paid"] else "unpaid")))
    comment = opened(w["comment"], "writeup.comment")
    if comment:
        blocks += [("heading", "Employee's comment"), ("paragraph", comment)]
    blocks += [("heading", "Signature"),
               ("paragraph", SIGN_STATEMENT.format(days=setting(db, "dis_appeal_days"))),
               ("row", "How", METHODS.get(w["method"] or "", ""))]
    signed = local_time(db, w["signed_at"]) if w["signed_at"] else ""
    if w["method"] == "refused":
        witness = opened(w["witness_name"], "writeup.witness_name") or ""
        blocks += [("paragraph", "The employee refused to sign."),
                   ("signature", "Witness", witness, signed)]
    elif w["method"] == "paper":
        blocks.append(("paragraph", "Signed on paper. A photo of the signed page is filed with it."))
    else:
        name = opened(w["signed_name"], "writeup.signed_name") or ""
        blocks.append(("signature", "Employee", name, signed))
    blocks.append(("signature", "Delivered by", w["delivered_by_name"] or "",
                   local_time(db, w["delivered_at"]) if w["delivered_at"] else ""))
    return title, blocks


def pdf_page(svc, row: dict):
    """The documents queue's PDF maker for a signed write-up (register_pdf_maker; row["ref"] is
    the write-up): (person, title, HTML, the day it was delivered), or None when there's
    nothing to make any more (voided, or filed already)."""
    db = svc.db
    w = get_writeup(db, row["ref"])
    if not w or w["status"] != "active" or w["pdf_doc_id"]:
        return None
    title, blocks = pdf_blocks(db, w)
    return (w["person_id"], title, pdfdoc.page(title, blocks, subtitle=svc.property_name() or None),
            w["issued_on"])


def pdf_filed(svc, row: dict, doc: dict) -> None:
    """The queue made the signed PDF (a "writeups" document, on its way to the Discipline
    folder in Google Drive): it's the write-up's now."""
    db = svc.db
    w = get_writeup(db, row["ref"])
    if not w or w["pdf_doc_id"]:
        return
    db.conn.execute("UPDATE writeups SET pdf_doc_id = ? WHERE id = ?", (doc["id"], w["id"]))
    event(db, w["person_id"], "Signed PDF filed (on its way to Google Drive)", None, "HR portal",
          writeup_id=w["id"])
    db.conn.commit()
    person = db.person(w["person_id"])
    audit.record(db, "Filed a signed write-up", target_id=w["person_id"],
                 target=person["name"] if person else None)


def pdf_state(db, w: dict, rel: str) -> dict:
    """How the signed PDF is going, from the documents queue's work on it: its status (waiting,
    done, attention), and for HR the last problem."""
    work = documents.pdf_work(db, WRITEUPS, w["id"]) if w["status"] != "draft" else None
    stuck = not w["pdf_doc_id"] and work is not None
    return {"status": "done" if w["pdf_doc_id"] else work and work["status"],
            "error": work["error"] if stuck and rel == "hr" else None,
            "filed": bool(w["pdf_doc_id"]), "attention": stuck and work["status"] == "attention"}


# ---------------------------------------------------------------- background work

async def nightly(svc) -> None:
    reminders(svc.db, today_of(svc.db))


def reminders(db, today: date, now: float | None = None) -> int:
    """Write-ups sent to sign in the portal and not signed after N days (HR setting): their
    managers are reminded, then every N days. They count either way."""
    now = time.time() if now is None else now
    gap = setting(db, "dis_sign_days") * 86400
    count = 0
    for w in [dict(r) for r in db.conn.execute("SELECT * FROM writeups WHERE status = 'delivered'")]:
        if now - (w["reminded_at"] or w["delivered_at"] or now) < gap - 3600:
            continue
        person = db.person(w["person_id"])
        if not person or person["archived"]:
            continue
        tell(db, staff_of(db, person, (w["delivered_by"],)), "writeup_overdue",
             link=f"#/writeup/{w['id']}", once=f"writeup-{w['id']}-overdue-{today}",
             title=f"{person['name']} hasn't signed the write-up from {w['issued_on']} yet. It "
                   "counts either way.")
        db.conn.execute("UPDATE writeups SET reminded_at = ? WHERE id = ?", (now, w["id"]))
        db.conn.commit()
        count += 1
    return count


# ---------------------------------------------------------------- what each person sees

def state_of(w: dict, today: date) -> str:
    if w["status"] in ("delivered", "active") and w["ends_on"] and w["ends_on"] < today.isoformat():
        return "expired"
    return w["status"]


def can_see_writeup(w: dict, rel: str | None) -> bool:
    if rel == "hr":
        return True
    if rel in ("manager", "director"):
        return not (w["hr_only"] and w["status"] in ("draft", "with_hr")) and (
            w["status"] != "voided" or bool(w["delivered_at"]))
    if rel == "self":
        return w["status"] in ("delivered", "active") or (
            w["status"] == "voided" and bool(w["delivered_at"]))
    return False


def local_time(db, ts: float, pattern: str = "%Y-%m-%d %H:%M") -> str:
    """A moment as people here read it: in HR's time zone (Pacific)."""
    try:
        zone = ZoneInfo(db.setting("time_zone"))
    except (ValueError, KeyError):
        zone = ZoneInfo("America/Los_Angeles")
    return datetime.fromtimestamp(ts, zone).strftime(pattern)


def withdrawn(db, w: dict) -> dict:
    """A voided write-up for anyone but HR: only that it was withdrawn, and when."""
    day = local_time(db, w["voided_at"] or time.time(), "%Y-%m-%d")
    return {"id": w["id"], "status": "voided", "state": "voided", "voided": True,
            "withdrawn_on": day, "status_word": f"Withdrawn on {day}", "step": "Withdrawn",
            "issued_on": w["issued_on"], "can": {}}


def writeup_summary(db, w: dict, rel: str, today: date) -> dict:
    if w["status"] == "voided" and rel != "hr":
        return withdrawn(db, w)
    state = state_of(w, today)
    appeal = get_appeal(db, writeup_id=w["id"])
    return {"id": w["id"], "step": w["amended_name"] or w["step_name"] or "Write-up (step to come)",
            "status": w["status"], "state": state,
            "status_word": (f"Expired {w['ends_on']}: no longer counts" if state == "expired"
                            else WRITEUP_WORDS.get(state, state)),
            "issued_on": w["issued_on"], "ends_on": w["ends_on"],
            "under_appeal": bool(appeal and appeal["status"] == "open"),
            "created_at": round(w["created_at"])}


def writeup_view(db, svc, w: dict, rel: str, viewer, person: dict, today: date) -> dict:
    """A write-up as this viewer may see it: hidden text never leaves the server."""
    if w["status"] == "voided" and rel != "hr":
        return withdrawn(db, w)
    staff = rel in STAFF
    body = body_of(db, w["version_id"], today)
    found = writeup_summary(db, w, rel, today)
    found.update(person={"id": person["id"], "name": person["name"]},
                 texts={k: opened(w[k], f"writeup.{k}") or "" for k in WRITEUP_TEXTS},
                 follow_up=w["follow_up"],
                 suspension=w["suspension_from"] and {"from": w["suspension_from"],
                                                      "to": w["suspension_to"],
                                                      "paid": bool(w["suspension_paid"])},
                 method=w["method"], method_word=METHODS.get(w["method"] or ""),
                 signed_at=w["signed_at"] and round(w["signed_at"]),
                 signed_name=opened(w["signed_name"], "writeup.signed_name"),
                 comment=opened(w["comment"], "writeup.comment"),
                 delivered_by_name=w["delivered_by_name"],
                 statement=SIGN_STATEMENT.format(days=setting(db, "dis_appeal_days")),
                 appeal_until=appeal_until(db, w))
    if w["method"] == "refused":
        found["witness"] = opened(w["witness_name"], "writeup.witness_name")
    appeal = get_appeal(db, writeup_id=w["id"])
    if appeal:
        found["appeal"] = {"id": appeal["id"], "status": appeal["status"],
                           "word": "Waiting for HR" if appeal["status"] == "open"
                           else APPEAL_OUTCOMES[appeal["status"]],
                           "text": opened(appeal["text"], "appeal.text"),
                           "note": opened(appeal["note"], "appeal.note"),
                           "filed_at": round(appeal["filed_at"]),
                           "decided_by_name": appeal["decided_by_name"] if staff else None}
    if staff:
        inc = get_incident(db, w["incident_id"]) if w["incident_id"] else None
        found.update(manager_note=opened(w["manager_note"], "writeup.manager_note") or "",
                     return_note=opened(w["return_note"], "writeup.return_note"),
                     recommended=w["recommended_key"] and step_name(body, w["recommended_key"]),
                     downgrade_reason=opened(w["downgrade_reason"], "writeup.downgrade_reason"),
                     step_key=w["step_key"], created_by_name=w["created_by_name"],
                     approved_by_name=w["approved_by_name"],
                     incident={"id": inc["id"], "offence": inc["offence_name"],
                               "happened": inc["happened"], "status": inc["status"]} if inc and (
                                   rel == "hr" or inc["status"] != "voided") else None,
                     is_suspension=is_suspension(step(body, w["step_key"])),
                     steps=[{"key": s["key"], "name": s.get("name") or s["key"]}
                            for s in steps(body) if not w["recommended_key"] or
                            rank(body, s["key"]) <= rank(body, w["recommended_key"])],
                     pdf=pdf_state(db, w, rel),
                     history=history_view(db, events_of(db, writeup_id=w["id"]), rel))
    else:
        found["history"] = history_view(db, events_of(db, writeup_id=w["id"]), "self")
    if rel == "hr":
        found["hr_note"] = opened(w["hr_note"], "writeup.hr_note") or ""
        found["void_reason"] = opened(w["void_reason"], "writeup.void_reason")
    # The signed PDF, and the photo of a page signed on paper: documents of the "writeups" kind,
    # opened through the documents part (can_open_file decides, every look is logged).
    for key, column in (("pdf_url", "pdf_doc_id"), ("photo_url", "photo_doc_id")):
        doc = documents.get(db, w[column]) if w[column] else None
        if doc and documents.may_open(viewer, rel, doc, documents.kind(db, doc["kind_id"])):
            found[key] = documents.view_url(doc)
    found["can"] = abilities(db, w, rel, viewer, today)
    return found


def abilities(db, w: dict, rel: str, viewer, today: date) -> dict:
    staff = rel in STAFF
    status = w["status"]
    appeal = get_appeal(db, writeup_id=w["id"])
    mine = viewer.id in (w["created_by"], w["edited_by"])
    return {"edit": staff and status == "draft", "hr_note": rel == "hr" and status != "voided",
            "submit": staff and status == "draft" and bool(w["step_key"]),
            "approve": rel == "hr" and status == "with_hr" and not mine,
            "return": rel == "hr" and status == "with_hr",
            "deliver": staff and status in ("approved", "delivered"),
            "send": staff and status == "approved",
            "sign": rel == "self" and status == "delivered",
            "appeal": rel == "self" and status in ("delivered", "active") and appeal is None
            and today.isoformat() <= (appeal_until(db, w) or ""),
            "decide_appeal": rel == "hr" and bool(appeal) and appeal["status"] == "open"
            and viewer.id not in (w["created_by"], w["edited_by"], w["approved_by"]),
            "void": rel == "hr" and status != "voided",
            "change_ends": rel == "hr" and status in ("delivered", "active"),
            "pdf_retry": rel == "hr" and pdf_state(db, w, rel)["attention"]}


def incident_view(db, inc: dict, rel: str, viewer, person: dict, today: date) -> dict:
    body = body_of(db, inc["version_id"], date.fromisoformat(inc["happened"]))
    s = step(body, inc["step_key"])
    show_step = s and (rel == "hr" or not hr_only_step(s))
    w = db.conn.execute("SELECT * FROM writeups WHERE incident_id = ? ORDER BY id DESC LIMIT 1",
                        (inc["id"],)).fetchone()
    found = {"id": inc["id"], "person": {"id": person["id"], "name": person["name"]},
             "offence": inc["offence_name"], "offence_key": inc["offence_key"],
             "category": inc["category"], "happened": inc["happened"],
             "what": opened(inc["what"], "incident.what") or "",
             "protected": leaves(body).get(inc["protected"], "Protected leave")
             if inc["protected"] != "no" else None,
             "protected_key": inc["protected"],
             "default_points": inc["default_points"], "manager_points": inc["manager_points"],
             "manager_reason": opened(inc["manager_reason"], "incident.manager_reason"),
             "final_points": inc["final_points"], "points_now": incident_points_now(db, inc),
             "status": inc["status"], "status_word": INCIDENT_WORDS.get(inc["status"], inc["status"]),
             "recorded_by_name": inc["recorded_by_name"], "recorded_at": round(inc["recorded_at"]),
             "decided_by_name": inc["decided_by_name"], "approved_on": inc["approved_on"],
             "return_note": opened(inc["return_note"], "incident.return_note"),
             "step": step_name(body, inc["step_key"]) if show_step else None,
             "step_reason": inc["step_reason"] if show_step else None,
             "on_probation": bool(inc["probation_key"]),
             "writeup_id": w["id"] if w and can_see_writeup(dict(w), rel) else None,
             "history": history_view(db, events_of(db, incident_id=inc["id"]), rel)}
    if rel == "hr":
        found.update(hr_points=inc["hr_points"],
                     hr_reason=opened(inc["hr_reason"], "incident.hr_reason"),
                     void_reason=opened(inc["void_reason"], "incident.void_reason"))
        if inc["status"] == "with_hr" and body:
            # What approving it now counts (a repeat counts the incidents HR approved since it
            # was recorded), and earlier ones still waiting, which approving first would count.
            s = score(db, body, inc["person_id"], inc["offence_key"],
                      date.fromisoformat(inc["happened"]), inc["protected"], inc["manager_points"])
            found["points_if_approved"] = inc["final_points"] if s["errors"] else s["final"]
            found["earlier_waiting"] = db.conn.execute(
                "SELECT COUNT(*) FROM incidents WHERE person_id = ? AND status = 'with_hr' AND "
                "id != ? AND happened <= ?", (inc["person_id"], inc["id"], inc["happened"])).fetchone()[0]
    found["can"] = {"approve": rel == "hr" and inc["status"] == "with_hr"
                    and inc["recorded_by"] != viewer.id,
                    "return": rel == "hr" and inc["status"] == "with_hr",
                    "void": rel == "hr" and inc["status"] != "voided",
                    "adjust": rel == "hr" and inc["status"] == "approved" and inc["protected"] == "no",
                    "edit": inc["status"] == "returned" and inc["recorded_by"] == viewer.id}
    return found


def lines_view(db, person_id, on: date, rel: str, see_expired: bool, actions: bool = False) -> list[dict]:
    """The points history ("bank statement") as this relation may see it: voided lines are HR's,
    expired ones the employee's only when the policy allows. actions (HR, today): each line's id
    and what HR can do to it (void_line, end_line)."""
    names = {str(r["id"]): r["offence_name"] for r in db.conn.execute(
        "SELECT id, offence_name FROM incidents WHERE person_id = ?", (str(person_id),))}
    found = []
    # history() hands reasons back unsealed: only HR's view keeps them.
    for line in points.history(db, str(person_id), on, True):
        state, kind = line["state"], line["kind"]
        if rel != "hr" and (state == "voided" or kind == "void" or line.get("end_kind")):
            continue   # voids, and end dates set on other lines, are HR's bookkeeping
        if state == "expired" and not see_expired:
            continue
        offence_name = names.get(str(line["incident_id"]))
        word = ("End date changed" if line.get("end_kind") else
                {"adjustment": "Adjustment by HR", "credit": "Credit", "void": "Void"}.get(kind, kind))
        item = {"date": line["happened"],
                "label": offence_name if kind == "incident" and offence_name else
                f"{word}: {offence_name}" if offence_name else word,
                "kind": kind, "points": line["points"], "state": state, "ends": line.get("ends"),
                "incident_id": line["incident_id"] if rel in STAFF else None}
        if rel == "hr" and line.get("reason"):
            item["reason"] = line["reason"]
        if actions and rel == "hr":
            item.update(id=line["id"], can={"void": may_void_line(line), "end": may_end_line(line)})
        found.append(item)
    return found


def may_void_line(line: dict) -> bool:
    """HR can void points it added or took off, a change to an incident's points, a credit, or its
    own override end (an incident is voided from its own page, so its records follow)."""
    return line["state"] != "voided" and (line["kind"] == "credit" or (
        line["kind"] == "adjustment" and line.get("end_kind") != "writeup"))


def may_end_line(line: dict) -> bool:
    """HR can set when an incident or added points stop counting (an override, which wins)."""
    return line["state"] != "voided" and (line["kind"] == "incident" or (
        line["kind"] == "adjustment" and not line.get("target_id") and line["points"] > 0))


def _line_of(db, person_id, line_id) -> dict:
    found = next((ln for ln in points.history(db, str(person_id)) if ln["id"] == line_id), None)
    if found is None:
        raise ValueError("That line isn't in their points.")
    return found


def void_line(db, person: dict, line_id: int, reason: str, actor_id, actor: str, today: date) -> None:
    """HR voids one line of someone's points (a mistaken adjustment or credit): it stops counting
    from today. Later write-ups are checked again (only a real drop makes a task). Raises
    ValueError."""
    if not reason:
        raise ValueError("Say why.")
    line = _line_of(db, person["id"], line_id)
    if not may_void_line(line):
        raise ValueError("Void an incident from its own page. This line can't be voided here.")
    lines = points.void(db, person["id"], actor_id, reason, target_id=line_id, starts=today)
    if line["incident_id"]:
        event(db, person["id"], "HR voided a change to its points", actor_id, actor,
              incident_id=int(line["incident_id"]), note=reason, visibility="hr")
        db.conn.commit()
    recheck_later(db, person["id"], line["happened"], actor_id, actor, today,
                  cause=f"HR voided a points line from {line['happened']}.", lines=lines)


def end_line(db, person: dict, line_id: int, ends: date, reason: str, actor_id, actor: str,
             today: date) -> None:
    """HR sets the day an incident or added points stop counting (the first day they no longer
    count): an override, which wins over every expiry rule. Raises ValueError."""
    if not reason:
        raise ValueError("Say why.")
    line = _line_of(db, person["id"], line_id)
    if not may_end_line(line):
        raise ValueError("Only an incident or added points can be given an end date.")
    if ends <= date.fromisoformat(line["happened"]):
        raise ValueError("It can't stop counting before it happened. To cancel it, void it.")
    old = line.get("ends")
    lines = points.set_ends(db, person["id"], ends, actor_id, reason, target_id=line_id, starts=today)
    if line["incident_id"]:
        event(db, person["id"], f"HR changed when its points stop counting: {old or 'no end'} → "
              f"{ends}", actor_id, actor, incident_id=int(line["incident_id"]), note=reason)
        db.conn.commit()
    recheck_later(db, person["id"], line["happened"], actor_id, actor, today,
                  cause=f"Points from {line['happened']} now stop counting on {ends}.", lines=lines)


def person_view(db, viewer, person: dict, rel: str, as_of: date | None = None) -> dict:
    """The Discipline section of someone's page (or their file "as of" a past day, for staff)."""
    today = today_of(db)
    on = as_of or today
    ver = version_on(db, today)
    body = ver[1] if ver else {}
    see_total, see_expired = switches(body)
    pid = person["id"]
    writeups = [dict(r) for r in db.conn.execute(
        "SELECT * FROM writeups WHERE person_id = ? ORDER BY COALESCE(issued_on, '9999') DESC, "
        "id DESC",
        (pid,))]
    found = {"set_up": ver is not None, "message": None if ver else NOT_SET_UP,
             "today": today.isoformat(), "as_of": on.isoformat()}
    if rel == "self":
        shown = [writeup_summary(db, w, rel, on) for w in writeups if can_see_writeup(w, rel)]
        found["writeups"] = [w for w in shown if see_expired or w["state"] != "expired"]
        found["points"] = totals(db, pid, on) if see_total else None
        found["next_drop"] = next_drop(db, pid, on) if see_total else None
        found["lines"] = lines_view(db, pid, on, rel, see_expired) if see_total else []
        found["can"] = {}
        return found
    active, _ = current_step(db, body, pid, on)
    found.update(points=totals(db, pid, on), next_drop=next_drop(db, pid, on),
                 step=step_name(body, active) if active else None,
                 lines=lines_view(db, pid, on, rel, True, actions=as_of is None),
                 writeups=[writeup_summary(db, w, rel, on) for w in writeups
                           if can_see_writeup(w, rel)
                           and (not as_of or (w["issued_on"] or "9999") <= on.isoformat())])
    incidents = [dict(r) for r in db.conn.execute(
        "SELECT * FROM incidents WHERE person_id = ? ORDER BY happened DESC, id DESC LIMIT 200",
        (pid,))]
    found["incidents"] = [
        {"id": i["id"], "offence": i["offence_name"], "happened": i["happened"],
         "points": incident_points_now(db, i), "status": i["status"],
         "status_word": INCIDENT_WORDS.get(i["status"], i["status"]),
         "protected": i["protected"] != "no"}
        for i in incidents if (rel == "hr" or i["status"] != "voided")
        and (not as_of or i["happened"] <= on.isoformat())]
    found["tasks"] = [task_view(db, t, body) for t in db.conn.execute(
        "SELECT * FROM discipline_tasks WHERE person_id = ? AND status = 'open' ORDER BY id", (pid,))
        if rel == "hr" or not t["hr_only"]]
    found["tracks"] = sorted(set(found["points"]) | {track_of(body, {})}) if ver else []
    found["can"] = {"record": ver is not None and not person["archived"], "as_of": True,
                    "adjust": rel == "hr" and ver is not None}
    return found


def task_view(db, t, body: dict) -> dict:
    t = dict(t)
    return {"id": t["id"], "kind": t["kind"], "word": TASK_WORDS.get(t["kind"], t["kind"]),
            "step": step_name(body, t["step_key"]) if t["step_key"] else None,
            "reason": t["reason"], "writeup_id": t["writeup_id"], "incident_id": t["incident_id"],
            "created_at": round(t["created_at"]), "person_id": t["person_id"]}


def summary_for_review(db, person_id, start, end) -> dict:
    """For the review's side panel: {points: {track: total} on the end day, writeups: [{date,
    step, status}] delivered in the period (not voided), next_drop}."""
    start, end = as_date(start), as_date(end) or today_of(db)
    rows = db.conn.execute(
        "SELECT * FROM writeups WHERE person_id = ? AND status IN ('delivered', 'active') AND "
        "issued_on >= ? AND issued_on <= ? ORDER BY issued_on", (str(person_id),
                                                                (start or date.min).isoformat(),
                                                                end.isoformat()))
    return {"points": totals(db, person_id, end),
            "writeups": [{"date": w["issued_on"], "step": w["amended_name"] or w["step_name"],
                          "status": WRITEUP_WORDS[state_of(dict(w), end)]} for w in rows],
            "next_drop": next_drop(db, person_id, end)}


def _clear_probation_points(db, row: dict, actor_id) -> None:
    """A probation whose policy clears its points on passing: each of its incidents still counting
    stops counting the day it passed (HR's override end). Points taken off would come from the
    oldest points instead, so points from before the probation are left as they were."""
    day = as_date(row["decided_on"]) or today_of(db)
    c = points.calc(db, row["person_id"], day)
    counting = {ln["incident_id"] for ln in c.bases if ln["kind"] == "incident" and c.alive(ln, day)}
    for (iid,) in db.conn.execute(
            "SELECT id FROM incidents WHERE person_id = ? AND status = 'approved' AND happened >= ? "
            "AND happened <= ?", (row["person_id"], row["starts"], row["ends"])).fetchall():
        if str(iid) in counting:
            points.set_ends(db, row["person_id"], day, actor_id,
                            "Passed probation: its points are cleared.", incident_id=iid, starts=day)


probation.on_pass_clear.append(_clear_probation_points)


# ---------------------------------------------------------------- the person page and to-dos

@server.section("discipline", 50)
def discipline_section(svc, v, p: dict, rel: str) -> dict | None:
    if rel is None:
        return None
    db = svc.db
    try:
        data = person_view(db, v, p, rel)
    except Exception as err:  # noqa: BLE001 - the rest of the page still shows
        log.warning("HR portal: the discipline section failed: %r", err)
        return {"title": "Discipline", "rows": [], "notes": ["This section couldn't be shown."]}
    if rel == "self" and not data["set_up"] and not data["writeups"]:
        return None
    rows = []
    if data.get("points") is not None:
        shown = ", ".join(fmt(x) for x in data["points"].values())
        rows.append({"label": "Points", "value": shown or "0"})
    return {"title": "Discipline", "rows": rows, "notes": [data["message"]] if data["message"] else [],
            **data}


@server.todo
def discipline_todos(svc, v) -> list[dict]:
    db = svc.db
    found = []
    for w in db.conn.execute("SELECT id FROM writeups WHERE person_id = ? AND status = 'delivered'",
                             (v.id,)):
        found.append({"text": "You have a write-up to read and sign.", "link": f"#/writeup/{w['id']}"})
    if not v.is_staff:
        return found
    for i in db.conn.execute("SELECT id, person_id FROM incidents WHERE recorded_by = ? AND "
                             "status = 'returned'", (v.id,)).fetchall():
        person = db.person(i["person_id"])
        if access.person_relation(db, v, person) in STAFF:   # access follows the person, live
            found.append({"text": f"HR returned an incident about {person['name']} to you.",
                          "link": f"#/incident/{i['id']}"})
    if v.is_hr:
        q = queue_counts(db, v)
        for key, text in (("incidents", "incident{s} waiting for your approval"),
                          ("writeups", "write-up{s} waiting for HR"),
                          ("appeals", "appeal{s} to decide"), ("tasks", "discipline task{s} open"),
                          ("pdfs", "signed write-up{s} that couldn't be filed")):
            if q[key]:
                found.append({"text": f"{q[key]} {text.format(s='' if q[key] == 1 else 's')}.",
                              "link": "#/discipline"})
        return found
    team = team_rows(db, v, light=True)
    for key, text in (("tasks", "step{s} due: write-up{s} to finish"),
                      ("to_deliver", "write-up{s} ready to deliver"),
                      ("returned", "write-up{s} HR sent back to you")):
        n = sum(r[key] for r in team)
        if n:
            found.append({"text": f"{n} {text.format(s='' if n == 1 else 's')}.", "link": "#/team"})
    return found


def queue_counts(db, v) -> dict:
    q = hr_queue(db, v)
    return {"incidents": len(q["incidents"]), "writeups": len(q["writeups"]),
            "appeals": len(q["appeals"]), "tasks": len(q["tasks"]), "pdfs": len(q["pdfs"])}


def hr_queue(db, v) -> dict:
    """HR's discipline queue: never anything about the HR admin looking at it."""
    today = today_of(db)
    found = {"incidents": [], "writeups": [], "appeals": [], "tasks": [], "pdfs": [], "unsigned": []}
    names = {p["id"]: p["name"] for p in db.people(archived=None)}
    for i in db.conn.execute("SELECT * FROM incidents WHERE status = 'with_hr' AND person_id != ? "
                             "ORDER BY recorded_at", (v.id,)):
        found["incidents"].append({"id": i["id"], "person_id": i["person_id"],
                                   "name": names.get(i["person_id"]), "offence": i["offence_name"],
                                   "happened": i["happened"], "points": i["final_points"],
                                   "by": i["recorded_by_name"], "own_work": i["recorded_by"] == v.id})
    for w in db.conn.execute("SELECT * FROM writeups WHERE status = 'with_hr' AND person_id != ? "
                             "ORDER BY submitted_at", (v.id,)):
        found["writeups"].append({"id": w["id"], "person_id": w["person_id"],
                                  "name": names.get(w["person_id"]),
                                  "step": w["step_name"] or "Waiting for its incident",
                                  "by": w["created_by_name"],
                                  "own_work": v.id in (w["created_by"], w["edited_by"])})
    for a in db.conn.execute("SELECT a.*, w.created_by, w.edited_by, w.approved_by, w.step_name, "
                             "w.amended_name FROM appeals a JOIN writeups w ON w.id = a.writeup_id "
                             "WHERE a.status = 'open' AND a.person_id != ? ORDER BY a.filed_at",
                             (v.id,)):
        found["appeals"].append({"id": a["id"], "writeup_id": a["writeup_id"],
                                 "person_id": a["person_id"], "name": names.get(a["person_id"]),
                                 "step": a["amended_name"] or a["step_name"],
                                 "filed_at": round(a["filed_at"]),
                                 "own_work": v.id in (a["created_by"], a["edited_by"],
                                                      a["approved_by"])})
    body = (version_on(db, today) or ("", {}))[1]
    for t in db.conn.execute("SELECT * FROM discipline_tasks WHERE status = 'open' AND person_id != ? "
                             "ORDER BY id", (v.id,)):
        found["tasks"].append({**task_view(db, t, body), "name": names.get(t["person_id"])})
    for row in documents.pdf_works(db, WRITEUPS, "attention"):
        if row["person_id"] != v.id:
            found["pdfs"].append({"id": int(row["ref"]), "person_id": row["person_id"],
                                  "name": names.get(row["person_id"]), "error": row["error"]})
    for w in db.conn.execute("SELECT * FROM writeups WHERE status = 'delivered' AND reminded_at IS "
                             "NOT NULL AND person_id != ?", (v.id,)):
        found["unsigned"].append({"id": w["id"], "person_id": w["person_id"],
                                  "name": names.get(w["person_id"]), "issued_on": w["issued_on"]})
    found["probation"] = probation.hr_queue(db, v)
    found["waiting_pdfs"] = len(documents.pdf_works(db, WRITEUPS, "waiting"))
    return found


def team_rows(db, v, light: bool = False) -> list[dict]:
    """A manager's "My team": everyone whose file they see, with what needs doing."""
    today = today_of(db)
    body = (version_on(db, today) or ("", {}))[1]
    found = []
    for p, rel in access.visible_people(db, v):
        if rel == "self" or rel not in STAFF:
            continue
        pid = p["id"]
        counts = {k: 0 for k in ("drafts", "to_deliver", "unsigned", "waiting", "returned", "tasks")}
        for w in db.conn.execute("SELECT status, hr_only, created_by, edited_by, return_note FROM "
                                 "writeups WHERE person_id = ? AND status IN ('draft', 'approved', "
                                 "'delivered')", (pid,)):
            if w["hr_only"] and rel != "hr":
                continue
            key = {"draft": "drafts", "approved": "to_deliver", "delivered": "unsigned"}[w["status"]]
            counts[key] += 1
            counts["returned"] += bool(w["status"] == "draft" and w["return_note"]
                                       and v.id in (w["created_by"], w["edited_by"]))
        counts["waiting"] = db.conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE person_id = ? AND status IN ('with_hr', 'returned')",
            (pid,)).fetchone()[0]
        counts["tasks"] = db.conn.execute(
            "SELECT COUNT(*) FROM discipline_tasks WHERE person_id = ? AND status = 'open' AND "
            "(hr_only = 0 OR ?)", (pid, 1 if rel == "hr" else 0)).fetchone()[0]
        row = {"id": pid, "name": p["name"], **counts}
        if not light:
            pts = totals(db, pid, today)
            active, _ = current_step(db, body, pid, today)
            on = probation.current(db, pid, today)
            row.update(points=sum(pts.values()) if pts else 0.0,
                       step=step_name(body, active) if active else None,
                       probation_ends=on["end"] if on else None,
                       probation=probation.kind_word(db, on["kind"]) if on else None)
        found.append(row)
    return found


@server.settings_section("discipline_workflow", 45)
def workflow_settings(svc, v) -> dict:
    db = svc.db
    return {"appeal_days": setting(db, "dis_appeal_days"), "sign_days": setting(db, "dis_sign_days"),
            "probation_word": probation.word(db), "probation_show_end": probation.shows_end(db),
            "words": probation.WORDS, "set_up": version_on(db, today_of(db)) is not None}


# ---------------------------------------------------------------- routes

def _person_named(request) -> tuple:
    svc = svc_of(request)
    return svc, viewer_of(request), request[server.PERSON], request[server.RELATION]


def _record(request, getter, key: str, *, staff: bool = False, hr: bool = False) -> tuple:
    """The record a route names, with its person and how the viewer sees it, when they may: 404
    otherwise, and a plain refusal on their own file."""
    svc = svc_of(request)
    v = viewer_of(request)
    row = getter(svc.db, request.match_info[key])
    person = svc.db.person(row["person_id"]) if row else None
    rel = access.person_relation(svc.db, v, person)
    if row is None or rel is None:
        raise Refused(server.NOT_FOUND, 404)
    if (staff or hr) and rel == "self":
        raise Refused(access.NOT_YOURS)
    if (hr and rel != "hr") or (staff and rel not in STAFF):
        raise Refused(server.NOT_FOUND, 404)
    return svc, v, row, person, rel


def _writeup(request, *, staff: bool = False, hr: bool = False) -> tuple:
    svc, v, w, person, rel = _record(request, get_writeup, "wid", staff=staff, hr=hr)
    if not can_see_writeup(w, rel):
        raise Refused(server.NOT_FOUND, 404)
    return svc, v, w, person, rel


def _done(svc, request, v, person, action: str, detail: str | None = None) -> None:
    audit.record(svc.db, action, actor_id=v.id, actor=v.name, target_id=person["id"],
                 target=person["name"], detail=detail, ip=svc.client_ip(request))


def _writeup_reply(svc, w_id, rel, v, person, message: str) -> web.Response:
    db = svc.db
    w = get_writeup(db, w_id)
    return web.json_response({"ok": True, "message": message,
                              "writeup": writeup_view(db, svc, w, rel, v, person, today_of(db))
                              if can_see_writeup(w, rel) else None})


def _guarded(fn):
    """Turn a plain refusal (ValueError) into a 400, NotSetUp into a 409."""
    async def handler(request):
        try:
            return await fn(request)
        except NotSetUp as err:
            return error(str(err), 409, not_set_up=True)
        except ValueError as err:
            return error(str(err))
    handler.hr_rule = fn.hr_rule
    handler.__name__ = fn.__name__
    return handler


def guarded(who: str, **kw):
    def wrap(fn):
        return _guarded(rule(who, **kw)(fn))
    return wrap


@guarded("signed_in", person=True)
async def api_person_discipline(request):
    """Someone's discipline: today, or as of a past day (staff)."""
    svc, v, p, rel = _person_named(request)
    as_of = as_date(request.query.get("as_of")) if rel in STAFF else None
    data = person_view(svc.db, v, p, rel, as_of)
    _done(svc, request, v, p, "Viewed discipline" + (f" as of {as_of}" if as_of else ""))
    return web.json_response(data)


@guarded("staff", person=True, not_self=True)
async def api_incident_form(request):
    svc, v, p, rel = _person_named(request)
    return web.json_response(form_data(svc.db, p, today_of(svc.db)))


@guarded("staff", person=True, not_self=True)
async def api_incident_preview(request):
    svc, v, p, rel = _person_named(request)
    data = await read_json(request)
    return web.json_response(preview(svc.db, p, data, rel, today_of(svc.db)))


@guarded("staff", person=True, not_self=True)
async def api_record_incident(request):
    svc, v, p, rel = _person_named(request)
    if p["archived"]:
        return error("They've left: incidents can't be recorded.", 409)
    data = await read_json(request)
    result = record_incident(svc.db, p, v, rel, data, today_of(svc.db))
    _done(svc, request, v, p, "Recorded an incident")
    inc = get_incident(svc.db, result["incident"])
    message = ("Sent to HR for approval." if inc["status"] == "with_hr" else "Recorded. It counts now.")
    # What the step was stays with HR (a termination review or suspension is HR's alone).
    w = get_writeup(svc.db, result["writeup"]) if result["writeup"] else None
    reply = result if rel == "hr" else {
        "incident": result["incident"], "writeup": w["id"] if w and can_see_writeup(w, rel) else None}
    return web.json_response({"ok": True, "message": message, **reply})


@guarded("hr", person=True, not_self=True, sensitive=True)
async def api_adjust_person(request):
    svc, v, p, rel = _person_named(request)
    data = await read_json(request)
    adjust_person(svc.db, p, data, v.id, v.name, today_of(svc.db))
    _done(svc, request, v, p, "Adjusted points", f"{data.get('points')} points")
    return web.json_response({"ok": True, "message": "Points adjusted."})


def _line_id(request) -> int:
    return int(request.match_info["eid"])


@guarded("hr", person=True, not_self=True, sensitive=True)
async def api_void_line(request):
    """HR voids one line of the points statement: an adjustment, a change or a credit."""
    svc, v, p, rel = _person_named(request)
    data = await read_json(request)
    void_line(svc.db, p, _line_id(request), clean_text(data.get("reason"), 1000), v.id, v.name,
              today_of(svc.db))
    _done(svc, request, v, p, "Voided a points line", f"Line {_line_id(request)}")
    return web.json_response({"ok": True, "message": "Voided. It no longer counts."})


@guarded("hr", person=True, not_self=True, sensitive=True)
async def api_end_line(request):
    """HR sets when one incident's (or added points') points stop counting."""
    svc, v, p, rel = _person_named(request)
    data = await read_json(request)
    ends = as_date(data.get("ends"))
    if ends is None:
        return error("Choose the first day it no longer counts.")
    end_line(svc.db, p, _line_id(request), ends, clean_text(data.get("reason"), 1000), v.id, v.name,
             today_of(svc.db))
    _done(svc, request, v, p, "Changed when points stop counting",
          f"Line {_line_id(request)}: from {ends.isoformat()}")
    return web.json_response({"ok": True, "message": f"It stops counting on {ends}."})


@guarded("staff")
async def api_incident(request):
    svc, v, inc, person, rel = _record(request, get_incident, "iid", staff=True)
    if inc["status"] == "voided" and rel != "hr":
        return error(server.NOT_FOUND, 404)
    _done(svc, request, v, person, "Viewed an incident")
    return web.json_response(incident_view(svc.db, inc, rel, v, person, today_of(svc.db)))


@guarded("staff")
async def api_edit_incident(request):
    svc, v, inc, person, rel = _record(request, get_incident, "iid", staff=True)
    if inc["recorded_by"] != v.id:
        return error("Only the person who recorded it changes it.", 403)
    data = await read_json(request)
    edit_incident(svc.db, inc, person, v, data, today_of(svc.db))
    _done(svc, request, v, person, "Changed an incident and sent it to HR again")
    return web.json_response({"ok": True, "message": "Sent to HR again."})


@guarded("hr")
async def api_approve_incident(request):
    svc, v, inc, person, rel = _record(request, get_incident, "iid", hr=True)
    access.not_own_work(v, inc["recorded_by"])
    data = await read_json(request)
    hr_points = clean_points(data.get("points"))
    result = approve_incident(svc.db, inc, v.id, v.name, hr_points=hr_points,
                              hr_reason=clean_text(data.get("reason"), 1000), today=today_of(svc.db))
    final = get_incident(svc.db, inc["id"])["final_points"]
    _done(svc, request, v, person, "Approved an incident", f"{fmt(final)} points")
    body = body_of(svc.db, inc["version_id"], today_of(svc.db))
    message = "Approved. It counts now."
    if result["step"]:
        message += f" {step_name(body, result['step'])} is due: it's on the task list."
    return web.json_response({"ok": True, "message": message, **result})


@guarded("hr")
async def api_return_incident(request):
    svc, v, inc, person, rel = _record(request, get_incident, "iid", hr=True)
    data = await read_json(request)
    return_incident(svc.db, inc, clean_text(data.get("note"), 1000), v.id, v.name)
    _done(svc, request, v, person, "Returned an incident to the manager")
    return web.json_response({"ok": True, "message": "Returned to the manager."})


@guarded("hr", sensitive=True)
async def api_void_incident(request):
    svc, v, inc, person, rel = _record(request, get_incident, "iid", hr=True)
    data = await read_json(request)
    void_incident(svc.db, inc, clean_text(data.get("reason"), 1000), v.id, v.name, today_of(svc.db))
    _done(svc, request, v, person, "Voided an incident")
    return web.json_response({"ok": True, "message": "Voided. Its points no longer count."})


@guarded("hr", sensitive=True)
async def api_adjust_incident(request):
    svc, v, inc, person, rel = _record(request, get_incident, "iid", hr=True)
    data = await read_json(request)
    new_points = clean_points(data.get("points"))
    if new_points is None:
        return error("Type the points it should have.")
    adjust_incident(svc.db, inc, new_points, clean_text(data.get("reason"), 1000), v.id, v.name,
                    today_of(svc.db))
    _done(svc, request, v, person, "Changed an incident's points", f"Now {fmt(new_points)}")
    return web.json_response({"ok": True, "message": f"It now has {fmt(new_points)} points."})


@guarded("signed_in")
async def api_writeup(request):
    svc, v, w, person, rel = _writeup(request)
    _done(svc, request, v, person, "Viewed a write-up")
    return web.json_response(writeup_view(svc.db, svc, w, rel, v, person, today_of(svc.db)))


@guarded("staff")
async def api_edit_writeup(request):
    svc, v, w, person, rel = _writeup(request, staff=True)
    data = await read_json(request)
    edit_writeup(svc.db, w, data, v, rel)
    _done(svc, request, v, person, "Changed a write-up")
    return _writeup_reply(svc, w["id"], rel, v, person, "Saved.")


@guarded("staff")
async def api_submit_writeup(request):
    svc, v, w, person, rel = _writeup(request, staff=True)
    submit_writeup(svc.db, w, v, person)
    _done(svc, request, v, person, "Sent a write-up to HR")
    return _writeup_reply(svc, w["id"], rel, v, person, "Sent to HR.")


@guarded("hr")
async def api_approve_writeup(request):
    svc, v, w, person, rel = _writeup(request, hr=True)
    approve_writeup(svc.db, w, v, person)
    _done(svc, request, v, person, "Approved a write-up")
    return _writeup_reply(svc, w["id"], rel, v, person, "Approved. Their manager can deliver it now.")


@guarded("hr")
async def api_return_writeup(request):
    svc, v, w, person, rel = _writeup(request, hr=True)
    data = await read_json(request)
    return_writeup(svc.db, w, clean_text(data.get("note"), 1000), v, person)
    _done(svc, request, v, person, "Returned a write-up to the manager")
    return _writeup_reply(svc, w["id"], rel, v, person, "Returned to the manager.")


@guarded("staff")
async def api_send_writeup(request):
    svc, v, w, person, rel = _writeup(request, staff=True)
    send_to_sign(svc.db, w, v, person, today_of(svc.db))
    _done(svc, request, v, person, "Delivered a write-up through the portal")
    return _writeup_reply(svc, w["id"], rel, v, person,
                          f"Sent. {first_name(person)} reads and signs it in the HR portal.")


@guarded("staff")
async def api_in_person(request):
    """The write-up as the employee sees it, for reading on the manager's device."""
    svc, v, w, person, rel = _writeup(request, staff=True)
    if w["status"] not in ("approved", "delivered"):
        return error("It isn't waiting for a signature.", 409)
    view = writeup_view(svc.db, svc, w, "self", v, person, today_of(svc.db))
    view.update(history=[], can={}, needs_code=bool(person["totp_on"]),
                can_sign_here=bool(person["kiosk_hash"]) and not person["archived"])
    _done(svc, request, v, person, "Opened a write-up to deliver in person")
    return web.json_response(view)


async def check_person(svc, request, person: dict, pin, code) -> str | None:
    """Someone's kiosk PIN, and their authenticator code if they use one, as at sign-in: None when
    right, else why not. Wrong tries count towards their lock, like a sign-in."""
    db = svc.db
    ip = svc.client_ip(request)
    if refusal := svc.throttle.refusal(ip):
        return refusal
    if not person["kiosk_hash"] or person["archived"] or person["access_ended"]:
        return "They can't sign in the HR portal, so they can't sign here. Print it and use paper."
    if signin.locked(person):
        return "Too many wrong tries: their sign-in is locked for now. Use paper, or try later."
    ok = True
    if person["totp_on"] and not signin.check_totp(db, person, code):
        ok = False
    if ok:
        pin = signin.clean_pin(pin)
        try:
            ok = bool(pin) and await signin.pin_matches(pin, person["kiosk_hash"])
        except signin.Busy as err:
            return str(err)
    if not ok:
        svc.throttle.failed(ip)
        server.wrong(svc, db.person(person["id"]), ip)
        audit.record(db, "Signature check failed", target_id=person["id"], target=person["name"],
                     ip=ip, security=True)
        return SIGN_FAILED
    db.update_person(person["id"], failed=0, failed_at=None)   # right: the streak starts again
    return None


def _signature(data: dict) -> tuple[str, str]:
    name = " ".join(clean_text(data.get("name"), 120).split())
    if len(name) < 2:
        raise ValueError("Type your name.")
    return name, clean_text(data.get("comment"), TEXT_LIMIT)


@guarded("staff")
async def api_sign_in_person(request):
    svc, v, w, person, rel = _writeup(request, staff=True)
    data = await read_json(request)
    name, comment = _signature(data)
    if w["status"] not in ("approved", "delivered"):
        return error("It isn't waiting for a signature.", 409)
    if why := await check_person(svc, request, person, data.get("pin"), data.get("code")):
        return error(why, 403)
    w = get_writeup(svc.db, w["id"])
    made_active(svc.db, w, v, "in_person", today_of(svc.db), "Signed in person", signed_at=time.time(),
                signed_name=seal(name, "writeup.signed_name"),
                comment=seal(comment or None, "writeup.comment"))
    _done(svc, request, v, person, "Write-up signed in person")
    return _writeup_reply(svc, w["id"], rel, v, person, "Signed. Thank you.")


@guarded("signed_in")
async def api_sign(request):
    svc, v, w, person, rel = _writeup(request)
    if rel != "self":
        return error("Only they can sign it.", 403)
    data = await read_json(request)
    name, comment = _signature(data)
    if w["status"] != "delivered":
        return error("It isn't waiting for your signature.", 409)
    if why := await check_person(svc, request, person, data.get("pin"), data.get("code")):
        return error(why, 403)
    w = get_writeup(svc.db, w["id"])
    made_active(svc.db, w, v, w["method"] or "portal", today_of(svc.db), "Signed by the employee",
                signed_at=time.time(), signed_name=seal(name, "writeup.signed_name"),
                comment=seal(comment or None, "writeup.comment"))
    _done(svc, request, v, person, "Signed a write-up")
    return _writeup_reply(svc, w["id"], rel, v, person, "Signed. Thank you.")


@guarded("staff")
async def api_refused(request):
    """Refused to sign: another portal user witnesses it with their PIN (and code), or the
    manager types a witness's name."""
    svc, v, w, person, rel = _writeup(request, staff=True)
    data = await read_json(request)
    if w["status"] not in ("approved", "delivered"):
        return error("It isn't waiting for a signature.", 409)
    typed = " ".join(clean_text(data.get("witness_name"), 120).split())
    who = str(data.get("witness") or "").strip()[:80]
    witness_id = None
    if who:
        found = signin.find_person(svc.db, who)
        if found is None or found["id"] in (person["id"], v.id) or not server.may_sign_in(found):
            svc.throttle.failed(svc.client_ip(request))
            return error("The witness must be someone else who can sign in to the HR portal: type "
                         "their mobile number, and their PIN.", 403)
        if why := await check_person(svc, request, found, data.get("witness_pin"),
                                     data.get("witness_code")):
            return error(why, 403)
        witness_id, typed = found["id"], found["name"]
    elif len(typed) < 2:
        return error("Have a witness confirm with their PIN, or type the witness's name.")
    made_active(svc.db, get_writeup(svc.db, w["id"]), v, "refused", today_of(svc.db),
                "Refused to sign, with a witness"
                + (" (confirmed with their PIN)" if witness_id else ""),
                signed_at=time.time(), witness_id=witness_id,
                witness_name=seal(typed, "writeup.witness_name"))
    _done(svc, request, v, person, "Write-up refused, witnessed")
    return _writeup_reply(svc, w["id"], rel, v, person, "Recorded. The write-up is active.")


@guarded("staff")
async def api_paper(request):
    """Signed on paper: a photo (or PDF) of the signed page, filed with the write-up."""
    svc, v, w, person, rel = _writeup(request, staff=True)
    if w["status"] not in ("approved", "delivered"):
        return error("It isn't waiting for a signature.", 409)
    if request.content_length is None or request.content_length > files.PHOTO_LIMIT:
        return error(f"That file is too big (more than {files.PHOTO_LIMIT // 2**20} MB).", 413)
    try:
        data, mime = await files.clean_upload(await request.read(), files.PHOTO_LIMIT)
    except files.FileRefused as err:
        return error(str(err))
    # A document of the "writeups" kind, straight to the Discipline folder in Drive. Keyed by
    # the write-up, so sending it twice at once files it once.
    doc = await documents.add_generated(
        svc, person, WRITEUPS, data, mime, f"{w['step_name'] or 'Write-up'} (signed on paper)",
        today_of(svc.db), v.id, upload_id=f"writeup-{w['id']}-paper")
    w = get_writeup(svc.db, w["id"])
    made_active(svc.db, w, v, "paper", today_of(svc.db), "Signed on paper (photo filed)",
                signed_at=time.time(), photo_doc_id=doc["id"])
    _done(svc, request, v, person, "Write-up signed on paper (photo filed)")
    return _writeup_reply(svc, w["id"], rel, v, person, "Filed. The write-up is active.")


@guarded("hr", sensitive=True)
async def api_void_writeup(request):
    svc, v, w, person, rel = _writeup(request, hr=True)
    data = await read_json(request)
    void_writeup(svc.db, w, clean_text(data.get("reason"), 1000), v.id, v.name, today_of(svc.db),
                 with_incident=data.get("with_incident") is True)
    _done(svc, request, v, person, "Voided a write-up")
    return _writeup_reply(svc, w["id"], rel, v, person, "Voided.")


@guarded("hr", sensitive=True)
async def api_writeup_ends(request):
    svc, v, w, person, rel = _writeup(request, hr=True)
    data = await read_json(request)
    ends = as_date(data.get("ends_on"))
    if ends is None:
        return error("Choose the day it stops counting.")
    change_ends(svc.db, w, ends, clean_text(data.get("reason"), 1000), v.id, v.name, today_of(svc.db))
    _done(svc, request, v, person, "Changed when a write-up stops counting", ends.isoformat())
    return _writeup_reply(svc, w["id"], rel, v, person, f"It now counts until {ends}.")


@guarded("signed_in")
async def api_appeal(request):
    svc, v, w, person, rel = _writeup(request)
    if rel != "self":
        return error("Only they can appeal it.", 403)
    data = await read_json(request)
    file_appeal(svc.db, w, person, clean_text(data.get("text"), TEXT_LIMIT), today_of(svc.db))
    _done(svc, request, v, person, "Appealed a write-up")
    return _writeup_reply(svc, w["id"], rel, v, person, "Your appeal was sent to HR.")


@guarded("hr")
async def api_pdf_retry(request):
    """HR's Retry on a signed PDF that needs attention (the same as on Needs attention)."""
    svc, v, w, person, rel = _writeup(request, hr=True)
    work = documents.pdf_work(svc.db, WRITEUPS, w["id"])
    if w["pdf_doc_id"] or not work or not documents.retry(svc.db, work["id"]):
        return error("There's nothing to retry.", 409)
    _done(svc, request, v, person, "Tried a signed write-up's PDF again")
    return _writeup_reply(svc, w["id"], rel, v, person, "It will be tried again now.")


@guarded("hr", sensitive=True)
async def api_decide_appeal(request):
    svc, v, appeal, person, rel = _record(request, lambda db, x: get_appeal(db, x), "aid", hr=True)
    w = get_writeup(svc.db, appeal["writeup_id"])
    data = await read_json(request)
    outcome = str(data.get("outcome") or "")
    decide_appeal(svc.db, appeal, w, outcome, clean_text(data.get("note"), TEXT_LIMIT), v,
                  today_of(svc.db), new_points=clean_points(data.get("points")),
                  new_step=str(data.get("step") or "") or None)
    _done(svc, request, v, person, "Decided an appeal", APPEAL_OUTCOMES.get(outcome))
    return _writeup_reply(svc, w["id"], rel, v, person, f"Decided: {APPEAL_OUTCOMES[outcome]}.")


@guarded("hr", sensitive=True)
async def api_decide_task(request):
    svc, v, task, person, rel = _record(request, get_task, "tid", hr=True)
    data = await read_json(request)
    outcome = str(data.get("outcome") or "")
    decide_termination(svc.db, task, outcome, clean_text(data.get("reason"), 1000), v)
    _done(svc, request, v, person, "Decided a termination review", TERMINATION_OUTCOMES.get(outcome))
    message = "Recorded." + (" Archive them in Connecteam by hand: it's on the task list."
                             if outcome == "terminate" else "")
    return web.json_response({"ok": True, "message": message})


@guarded("hr")
async def api_task_done(request):
    svc, v, task, person, rel = _record(request, get_task, "tid", hr=True)
    if task["status"] != "open" or task["kind"] == "termination":
        return error("Decide a termination review with its three choices." if task["kind"] ==
                     "termination" else "It's already done.", 409)
    data = await read_optional(request)
    db = svc.db
    db.conn.execute("UPDATE discipline_tasks SET status = 'done', done_by = ?, done_at = ?, "
                    "outcome = 'Done', outcome_note = ? WHERE id = ?",
                    (v.id, time.time(),
                     seal(clean_text(data.get("note"), 1000) or None, "task.outcome_note"), task["id"]))
    db.conn.commit()
    _done(svc, request, v, person, "Closed a discipline task", TASK_WORDS.get(task["kind"]))
    return web.json_response({"ok": True, "message": "Done."})


@guarded("hr")
async def api_queue(request):
    svc = svc_of(request)
    v = viewer_of(request)
    audit.record(svc.db, "Viewed the discipline queue", actor_id=v.id, actor=v.name,
                 ip=svc.client_ip(request))
    set_up = version_on(svc.db, today_of(svc.db)) is not None
    return web.json_response({**hr_queue(svc.db, v), "set_up": set_up,
                              "message": None if set_up else NOT_SET_UP})


@guarded("staff")
async def api_team(request):
    svc = svc_of(request)
    v = viewer_of(request)
    db = svc.db
    audit.record(db, "Viewed My team", actor_id=v.id, actor=v.name, ip=svc.client_ip(request))
    names = {p["id"]: p["name"] for p, _ in access.visible_people(db, v)}
    body = (version_on(db, today_of(db)) or ("", {}))[1]
    tasks = [{**task_view(db, t, body), "name": names[t["person_id"]]} for t in db.conn.execute(
        "SELECT * FROM discipline_tasks WHERE status = 'open' ORDER BY id")
        if t["person_id"] in names and t["person_id"] != v.id and (v.is_hr or not t["hr_only"])]
    returned = [{"id": i["id"], "name": names.get(i["person_id"], ""), "offence": i["offence_name"]}
                for i in db.conn.execute("SELECT * FROM incidents WHERE recorded_by = ? AND status = "
                                         "'returned'", (v.id,))]
    return web.json_response({"people": team_rows(db, v), "tasks": tasks, "returned": returned,
                              "set_up": bool(body), "message": None if body else NOT_SET_UP})


@guarded("hr", sensitive=True)
async def api_workflow_settings(request):
    svc = svc_of(request)
    v = viewer_of(request)
    data = await read_json(request)
    values = {}
    for key, field in (("dis_appeal_days", "appeal_days"), ("dis_sign_days", "sign_days")):
        if field in data:
            default, low, high = SETTINGS[key]
            try:
                values[key] = max(low, min(high, int(data[field])))
            except (TypeError, ValueError):
                return error("Type a number of days.")
    if "probation_word" in data:
        if data["probation_word"] not in probation.WORDS:
            return error("Choose the wording.")
        values["probation_word"] = data["probation_word"]
    if "probation_show_end" in data:
        values["probation_show_end"] = "1" if data["probation_show_end"] is True else "0"
    svc.db.set_settings(values)
    audit.record(svc.db, "Changed HR settings", actor_id=v.id, actor=v.name,
                 detail=", ".join(sorted(values)), ip=svc.client_ip(request), security=True)
    return web.json_response({"ok": True})


@server.add_routes
def routes(app: web.Application) -> None:
    r = app.router
    pid = r"/api/people/{pid:[A-Za-z0-9_-]{1,40}}"
    r.add_get(pid + "/discipline", api_person_discipline)
    r.add_get(pid + "/incident-form", api_incident_form)
    r.add_post(pid + "/incident-preview", api_incident_preview)
    r.add_post(pid + "/incidents", api_record_incident)
    r.add_post(pid + "/points", api_adjust_person)
    r.add_post(pid + r"/points/{eid:\d{1,12}}/void", api_void_line)
    r.add_post(pid + r"/points/{eid:\d{1,12}}/ends", api_end_line)
    iid = r"/api/incidents/{iid:\d{1,12}}"
    r.add_get(iid, api_incident)
    r.add_put(iid, api_edit_incident)
    r.add_post(iid + "/approve", api_approve_incident)
    r.add_post(iid + "/return", api_return_incident)
    r.add_post(iid + "/void", api_void_incident)
    r.add_post(iid + "/adjust", api_adjust_incident)
    wid = r"/api/writeups/{wid:\d{1,12}}"
    r.add_get(wid, api_writeup)
    r.add_put(wid, api_edit_writeup)
    r.add_post(wid + "/submit", api_submit_writeup)
    r.add_post(wid + "/approve", api_approve_writeup)
    r.add_post(wid + "/return", api_return_writeup)
    r.add_post(wid + "/send", api_send_writeup)
    r.add_get(wid + "/in-person", api_in_person)
    r.add_post(wid + "/sign-in-person", api_sign_in_person)
    r.add_post(wid + "/sign", api_sign)
    r.add_post(wid + "/refused", api_refused)
    r.add_post(wid + "/paper", api_paper)
    r.add_post(wid + "/void", api_void_writeup)
    r.add_post(wid + "/ends", api_writeup_ends)
    r.add_post(wid + "/appeal", api_appeal)
    r.add_post(wid + "/pdf-retry", api_pdf_retry)
    r.add_post(r"/api/appeals/{aid:\d{1,12}}/decide", api_decide_appeal)
    tid = r"/api/discipline-tasks/{tid:\d{1,12}}"
    r.add_post(tid + "/decide", api_decide_task)
    r.add_post(tid + "/done", api_task_done)
    r.add_get("/api/discipline/queue", api_queue)
    r.add_get("/api/discipline/team", api_team)
    r.add_put("/api/settings/discipline-workflow", api_workflow_settings)


# ---------------------------------------------------------------- the signed write-ups' kind

def can_open_file(viewer, rel: str | None, doc: dict) -> bool:
    """Who may open a write-up's files (the signed PDF, the paper photo): HR always; their
    managers and the employee while the write-up isn't voided."""
    if rel == "hr":
        return True
    db = hrdb.current
    row = db and db.conn.execute("SELECT status FROM writeups WHERE pdf_doc_id = ? OR photo_doc_id = ?",
                                 (doc.get("id"), doc.get("id"))).fetchone()
    return bool(row) and row["status"] != "voided" and rel in ("self", "manager", "director")


documents.register_kind(WRITEUPS, "Write-ups", "managers", "Discipline", "discipline",
                        can_open=can_open_file)
documents.register_pdf_maker(WRITEUPS, pdf_page, done=pdf_filed)
