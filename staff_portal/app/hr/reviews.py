"""Performance reviews in the HR portal (design section 8). The recommended answers to questions
40 to 45 are the defaults, and each is an HR setting (HR settings, Reviews).

Kinds: the end of the introductory period (probation), annual (on each work anniversary, or in
one month for everyone), 30- and 60-day check-ins (off by default), and extra reviews a manager or
HR starts by hand.

When they open: the nightly run opens scheduled reviews a set number of days before they're due,
once each (a key per person: annual:2027, probation:12, checkin30:2026-10-01). It skips one when
another review for the same person is due within a few weeks, and an end-of-probation review
cancels a check-in near it that nobody has started. When a probation's end date moves, its review
moves with it.

Forms: HR builds them: sections rated on the form's own scale (1 to 5 to start with) with a
comment needed for low ratings, comment-only sections, goals, and a final recommendation, plus
extra sections per department. Saving one makes a new version, and a review keeps the version it
opened with.

Who writes it: the primary manager of the person's main department (the first of their
departments with a primary manager, unless HR chose one), worked out on every request so it
follows the person live. HR writes it when there's no such manager, when that's the person
themselves, or when the manager can't see them (say, they're a manager too). HR can hand it to
someone else. Anyone else who sees the person (a second department's manager, a director, HR) can
add comments, each with who may see it, and everyone working on it keeps a private note only they
and HR admins see. Nobody reviews themselves.

The flow: a self-review first (annual reviews only, by default) -> the writer fills in the form,
which saves as they go -> HR's second approval (end-of-probation reviews, by default) -> the
writer shares it and records the meeting date -> the employee reads it, comments, and
acknowledges it ("received", not "agree") or refuses -> closed, and its PDF goes to the person's
Reviews folder in Google Drive (the documents part). An end-of-probation review's recommendation
(pass, extend or end) goes to the probation part once it's final; HR confirms the outcome there.

Ratings, dates and statuses stay readable; everything people type is encrypted. A shared review
is never rewritten (a trigger refuses it); comments and form versions are only ever added. The
other parts used here (probation, discipline, certificates, documents, pdfdoc) are looked up when
needed, and one that isn't there shows "Not available" instead of failing.
"""

import html
import importlib
import inspect
import json
import logging
import re
import time
from datetime import date, timedelta

from aiohttp import web

from . import access, audit, crypto, notify, people
from .access import Refused, rule
from .server import (PERSON, add_routes, departments_list, error, group_names, read_json, section,
                     settings_section, svc_of, todo, viewer_of)
from .service import PARTS

log = logging.getLogger("staff_portal")

DAY = 86400
DOC_KIND = "reviews"                 # the documents part's kind for final PDFs
NOT_FOUND = "That isn't here, or you can't see it."
NOT_AVAILABLE = "Not available"
KINDS = {"probation": "End of introductory period", "annual": "Annual review",
         "checkin30": "30-day check-in", "checkin60": "60-day check-in", "adhoc": "Extra review"}
# The same inside a sentence ("Ana Lee's annual review is due ...").
NOUNS = {"probation": "end-of-introductory-period review", "annual": "annual review",
         "checkin30": "30-day check-in", "checkin60": "60-day check-in", "adhoc": "extra review"}
# Which review wins when two for one person fall close together.
PRIORITY = {"probation": 3, "annual": 2, "adhoc": 2, "checkin60": 1, "checkin30": 1}
OPEN = ("writing", "approval", "ready", "shared")
MOVABLE = ("writing", "approval", "ready")      # not shared yet: can move, change hands, be cancelled
STATUS_WORDS = {"writing": "Being written", "approval": "Waiting for HR's approval",
                "ready": "Ready to share", "shared": "Shared: waiting for the employee",
                "closed": "Closed", "cancelled": "Cancelled"}
SELF_WORDS = {"none": "", "waiting": "Waiting for the self-review", "done": "Self-review sent",
              "skipped": "Carried on without the self-review"}
ACK_WORDS = {"received": "Acknowledged as received", "refused": "Refused to acknowledge",
             "paper": "Acknowledged on paper", "no_answer": "Closed without an answer"}
PROBATION_CHOICES = {"pass": "Pass: the introductory period ends as planned",
                     "extend": "Extend the introductory period", "end": "End employment"}
CERT_WORDS = {"valid": "Valid", "expiring": "Expiring", "expired": "Expired", "due": "Due",
              "missing": "Missing", "waiting": "Waiting for HR", "waived": "Waived"}
COMMENT_LEVELS = {"employee": "The employee too", "managers": "Managers, directors and HR",
                  "directors": "Directors and HR", "hr": "HR admins only"}    # who sees a comment
SECTION_KINDS = {"rating": "Rated, with a comment", "text": "A comment only",
                 "goals": "Goals (a list)"}
OVERALL = "overall"
STAFF_RELS = ("manager", "director", "hr")
MAX_TEXT = 4000
MAX_GOALS = 10
GOAL_TEXT = 300
MAX_FORMS = 20
CATCH_UP_DAYS = 7          # a scheduled review the nightly run missed still opens this late
ANNUAL_MIN_DAYS = 183      # a fixed-month annual review skips people who started this recently
PDF_WAIT = 3600            # Google or the documents part not ready: the PDF is tried again then
PDF_RETRY = (300, 1800, 7200, 21600)   # after a failure, then daily
EDIT_AUDIT_GAP = 900       # saving as they type: one audit line per person and review this often
NO_PDF_YET = "Saving review PDFs to Google Drive isn't available yet."

# HR settings, with the recommended answers to questions 40 to 45.
SETTINGS = {
    "reviews_probation": "1",         # end-of-probation reviews (dates from the probation part)
    "reviews_annual": "anniversary",  # annual: "anniversary", a fixed "month", or "off"
    "reviews_annual_month": "1",      # ...that month (1 to 12)
    "reviews_checkin_30": "0",        # 30- and 60-day check-ins, off unless wanted
    "reviews_checkin_60": "0",
    "reviews_self": "annual",         # self-reviews: "annual" only, "all" kinds, or "none"
    "reviews_second": "probation",    # HR's second approval: "probation" reviews, "all", "none"
    "reviews_open_days": "14",        # a scheduled review opens this many days before it's due
    "reviews_gap_days": "21",         # skipped when another is due within this many days (0: never)
    "reviews_remind_before": "7",     # the writer is reminded this many days before (0: not)...
    "reviews_remind_every": "7",      # ...on the day, then every this many days after
    "reviews_overdue_days": "14",     # HR is told when a review is this many days late
    "reviews_ack_days": "7",          # the employee is reminded to acknowledge after this long
    "reviews_forms": "{}",            # {kind: form}, else the starter form
}
NUMBERS = {"reviews_annual_month": (1, 12), "reviews_open_days": (1, 60),
           "reviews_gap_days": (0, 90), "reviews_remind_before": (0, 30),
           "reviews_remind_every": (1, 30), "reviews_overdue_days": (1, 90),
           "reviews_ack_days": (1, 30)}
CHOICES = {"reviews_probation": ("1", "0"), "reviews_annual": ("anniversary", "month", "off"),
           "reviews_checkin_30": ("1", "0"), "reviews_checkin_60": ("1", "0"),
           "reviews_self": ("annual", "all", "none"), "reviews_second": ("probation", "all", "none")}

STARTER_FORM = "standard"
STARTER = {
    "name": "Standard review",
    "scale": ["Unsatisfactory", "Needs improvement", "Meets expectations", "Exceeds expectations",
              "Outstanding"],
    "comment_at": 2,   # ratings at or below this need a comment
    "sections": [
        {"key": "guest_service", "title": "Guest service", "kind": "rating",
         "help": "Welcoming, helpful and polite with guests."},
        {"key": "teamwork", "title": "Teamwork", "kind": "rating",
         "help": "Helps others and works well with the team."},
        {"key": "reliability", "title": "Reliability and attendance", "kind": "rating",
         "help": "On time, ready to work, and covers their shifts."},
        {"key": "job_knowledge", "title": "Job knowledge and quality", "kind": "rating",
         "help": "Knows the job and does it well."},
        {"key": "cleanliness", "title": "Cleanliness and safety", "kind": "rating",
         "help": "Keeps work areas clean and works safely."},
        {"key": "communication", "title": "Communication", "kind": "rating",
         "help": "Listens, keeps others in the loop, and asks when unsure."},
        {"key": "policy", "title": "Follows policy (including alcohol and food safety)",
         "kind": "rating", "help": "Follows the handbook, and the OLCC and food safety rules."},
        {"key": "goals", "title": "Goals", "kind": "goals",
         "help": "What to work on before the next review."},
    ],
    "departments": {},
    "recommendations": ["Keep going as you are", "Agree a plan to improve", "Talk with HR"],
}

SCHEMA = """
-- Review forms. A saved form is never changed: saving makes a new version, and a review keeps
-- the version it opened with.
CREATE TABLE IF NOT EXISTS review_forms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family TEXT NOT NULL,                -- the form (its versions share this)
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    body TEXT NOT NULL,                  -- JSON: scale, sections, department sections, choices
    created_by TEXT,
    created_by_name TEXT,
    created_at REAL NOT NULL,
    UNIQUE (family, version)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,             -- Connecteam user ID
    kind TEXT NOT NULL,                  -- KINDS
    source TEXT,                         -- what opened it (annual:2027, ...); NULL: by hand
    form_id INTEGER NOT NULL,
    dept TEXT,                           -- their main department when it opened (extra sections)
    period_start TEXT,                   -- YYYY-MM-DD: what the review covers
    period_end TEXT,
    due TEXT NOT NULL,                   -- YYYY-MM-DD
    probation_id INTEGER,                -- end of probation: which one
    assigned TEXT,                       -- who HR gave it to: a person, "hr", or NULL (the rule)
    status TEXT NOT NULL DEFAULT 'writing',
    self_status TEXT NOT NULL DEFAULT 'none',    -- none, waiting, done, skipped
    ratings TEXT NOT NULL DEFAULT '{}',          -- {section: rating}
    texts TEXT,                                  -- encrypted JSON: {section: comment or [goals]}
    self_ratings TEXT NOT NULL DEFAULT '{}',
    self_texts TEXT,                             -- encrypted
    overall INTEGER,                     -- the writer's own overall rating (else worked out)
    overall_reason TEXT,                 -- encrypted: why
    recommendation TEXT,
    extend_to TEXT,                      -- end of probation, "extend": the new end date
    started INTEGER NOT NULL DEFAULT 0,  -- 1: the writer saved something
    returned_note TEXT,                  -- encrypted: why HR sent it back
    submitted_by TEXT,
    submitted_at REAL,
    approved_by TEXT,
    approved_by_name TEXT,
    approved_at REAL,
    meeting_date TEXT,
    shared_by TEXT,
    shared_at REAL,
    ack TEXT,                            -- ACK_WORDS
    ack_at REAL,
    employee_comment TEXT,               -- encrypted
    close_note TEXT,                     -- encrypted: the writer's note when they closed it
    closed_by TEXT,
    closed_at REAL,
    cancelled_by TEXT,
    cancelled_at REAL,
    cancel_reason TEXT,                  -- encrypted
    recommendation_sent_at REAL,         -- end of probation: passed to the probation part
    pdf_doc_id INTEGER,                  -- the final PDF (documents)
    pdf_tries INTEGER NOT NULL DEFAULT 0,
    pdf_next_at REAL,
    pdf_error TEXT,
    created_by TEXT,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS reviews_source ON reviews (person_id, source)
    WHERE source IS NOT NULL;
CREATE INDEX IF NOT EXISTS reviews_person ON reviews (person_id, due);
CREATE INDEX IF NOT EXISTS reviews_status ON reviews (status, due);
CREATE INDEX IF NOT EXISTS reviews_pdf ON reviews (pdf_next_at) WHERE pdf_next_at IS NOT NULL;
-- A shared review is never rewritten.
CREATE TRIGGER IF NOT EXISTS reviews_issued BEFORE UPDATE OF person_id, kind, form_id, dept,
    ratings, texts, self_ratings, self_texts, overall, overall_reason, recommendation, extend_to
    ON reviews WHEN OLD.status IN ('shared', 'closed')
BEGIN SELECT RAISE(ABORT, 'shared reviews are never rewritten'); END;
-- Comments from others who see the person (a second department's manager, a director, HR).
CREATE TABLE IF NOT EXISTS review_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL,
    author_id TEXT NOT NULL,
    author_name TEXT NOT NULL,
    text TEXT NOT NULL,                  -- encrypted
    visibility TEXT NOT NULL,            -- access.LEVELS
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS review_comments_review ON review_comments (review_id, id);
-- Each writer's private note on a review: only they and HR admins see it.
CREATE TABLE IF NOT EXISTS review_notes (
    review_id INTEGER NOT NULL,
    author_id TEXT NOT NULL,
    author_name TEXT NOT NULL,
    text TEXT NOT NULL,                  -- encrypted
    updated_at REAL NOT NULL,
    PRIMARY KEY (review_id, author_id)
);
-- The main department HR chose for someone (whose primary manager writes their reviews).
CREATE TABLE IF NOT EXISTS review_people (
    person_id TEXT PRIMARY KEY,
    main_group TEXT NOT NULL,
    set_by TEXT,
    set_at REAL NOT NULL
);
"""

for _kind, _title in (
        ("review_due", "A review is waiting for you to write."),
        ("review_reminder", "A review you're writing is due soon or late."),
        ("review_overdue", "A review is overdue."),
        ("review_self", "Please fill in your self-review."),
        ("review_self_done", "A self-review was sent."),
        ("review_approval", "A review needs your approval."),
        ("review_approved", "A review was approved and is ready to share."),
        ("review_returned", "A review was sent back to you."),
        ("review_shared", "You have a review to read."),
        ("review_acknowledged", "A review was acknowledged."),
        ("review_moved", "A review's due date moved.")):
    notify.register_kind(_kind, _title)


def other(name: str):
    """Another part of the HR portal, or None while it isn't here (each part arrives on its own).
    Its functions are looked up when used, so a missing one shows "Not available"."""
    try:
        return importlib.import_module(f"{__package__}.{name}")
    except ImportError:
        return None


async def _done(result):
    return await result if inspect.isawaitable(result) else result


# ---------------------------------------------------------------- small helpers

def _line(value, limit: int) -> str:
    """One line of plain text: no control characters, spaces squeezed."""
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).split())[:limit]


def _text(value, limit: int = MAX_TEXT) -> str:
    """Text people typed, kept plain: line breaks stay, other control characters go."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text).strip()[:limit]


def _date(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def nice(d) -> str:
    d = _date(d)
    return f"{d:%b} {d.day}, {d.year}" if d else ""


def today_of(db) -> date:
    return date.fromisoformat(notify.local_day(db))


def anniversary(start: date, year: int) -> date:
    try:
        return start.replace(year=year)
    except ValueError:   # February 29th
        return date(year, 2, 28)


def month_end(year: int, month: int) -> date:
    return (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1))


def setting(db, key: str) -> str:
    value = db.setting(key, SETTINGS[key])
    return value if key not in CHOICES or value in CHOICES[key] else SETTINGS[key]


def number(db, key: str) -> int:
    lo, hi = NUMBERS[key]
    try:
        return max(lo, min(hi, int(setting(db, key))))
    except ValueError:
        return int(SETTINGS[key])


def settings_of(db) -> dict:
    return {key: (number(db, key) if key in NUMBERS else setting(db, key)) for key in SETTINGS
            if key != "reviews_forms"} | {"reviews_forms": forms_chosen(db)}


def _unseal(value, purpose: str, default=None):
    try:
        return crypto.unseal_text(value, purpose)
    except crypto.Damaged:
        return default


def _json_sealed(row: dict, column: str) -> dict:
    raw = _unseal(row[column], f"reviews.{column}")
    try:
        found = json.loads(raw) if raw else {}
    except ValueError:
        found = {}
    return found if isinstance(found, dict) else {}


def _seal_json(value: dict, column: str) -> str | None:
    return crypto.seal_text(json.dumps(value), f"reviews.{column}") if value else None


def _ratings(row: dict, column: str = "ratings") -> dict:
    try:
        found = json.loads(row[column] or "{}")
    except ValueError:
        return {}
    return {k: v for k, v in found.items() if isinstance(v, int)} if isinstance(found, dict) else {}


def _plain(value):
    """Another part's answer as plain JSON (dates as YYYY-MM-DD)."""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value if value is None or isinstance(value, (str, int, float, bool)) else str(value)


def hr_admins(db, but=()) -> list[str]:
    return [pid for pid in db.with_role(access.HR_ADMIN) if pid not in set(map(str, but))]


# ---------------------------------------------------------------- forms

def _new_key(title: str, keys: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:24] or "section"
    key, n = base, 2
    while key in keys or key == OVERALL:
        key, n = f"{base}_{n}", n + 1
    return key


def _sections(raw, keys: set, most: int) -> list[dict]:
    if not isinstance(raw, list) or len(raw) > most:
        raise ValueError(f"There can be at most {most} sections there.")
    found = []
    for s in raw:
        if not isinstance(s, dict):
            raise ValueError("That form couldn't be read.")
        title = _line(s.get("title"), 80)
        if not title:
            raise ValueError("Give every section a title.")
        if s.get("kind") not in SECTION_KINDS:
            raise ValueError("Choose what each section asks for.")
        key = str(s.get("key") or "")
        if not re.fullmatch(r"[a-z0-9_]{1,30}", key) or key in keys or key == OVERALL:
            key = _new_key(title, keys)
        keys.add(key)
        found.append({"key": key, "title": title, "kind": s["kind"], "help": _line(s.get("help"), 300)})
    return found


def clean_form(data) -> dict:
    """A form as HR saved it, checked. Raises ValueError (plain words)."""
    if not isinstance(data, dict):
        raise ValueError("That form couldn't be read.")
    name = _line(data.get("name"), 60)
    if not name:
        raise ValueError("Give the form a name.")
    scale = data.get("scale")
    if not isinstance(scale, list) or not 2 <= len(scale) <= 10:
        raise ValueError("The rating scale has 2 to 10 points.")
    scale = [_line(s, 40) for s in scale]
    if not all(scale):
        raise ValueError("Give every point on the scale a name.")
    comment_at = data.get("comment_at", 0)
    if not isinstance(comment_at, int) or isinstance(comment_at, bool) or \
            not 0 <= comment_at < len(scale):
        raise ValueError("Choose which ratings need a comment.")
    keys: set = set()
    sections = _sections(data.get("sections"), keys, 30)
    if not sections:
        raise ValueError("A form needs at least one section.")
    raw = data.get("departments") or {}
    if not isinstance(raw, dict) or len(raw) > 50:
        raise ValueError("Choose departments from the list.")
    departments = {}
    for group, extra in raw.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", str(group)):
            raise ValueError("Choose departments from the list.")
        if found := _sections(extra, keys, 10):
            departments[str(group)] = found
    recs = data.get("recommendations") or []
    if not isinstance(recs, list) or len(recs) > 8:
        raise ValueError("A form has at most 8 recommendations to choose from.")
    recs = [r for r in (_line(r, 80) for r in recs) if r]
    if len({r.casefold() for r in recs}) != len(recs):
        raise ValueError("Each recommendation is listed once.")
    return {"name": name, "scale": scale, "comment_at": comment_at, "sections": sections,
            "departments": departments, "recommendations": recs}


def _form_row(row) -> dict | None:
    if not row:
        return None
    found = dict(row)
    found["body"] = json.loads(found["body"])
    return found


def form(db, form_id) -> dict | None:
    return _form_row(db.conn.execute("SELECT * FROM review_forms WHERE id = ?", (form_id,)).fetchone())


def latest(db, family: str) -> dict | None:
    return _form_row(db.conn.execute("SELECT * FROM review_forms WHERE family = ? ORDER BY version "
                                     "DESC LIMIT 1", (str(family),)).fetchone())


def families(db) -> list[dict]:
    """Each form's newest version (no bodies)."""
    return [dict(r) for r in db.conn.execute(
        "SELECT f.id, f.family, f.version, f.name, f.created_by_name, f.created_at FROM review_forms f "
        "WHERE f.version = (SELECT MAX(version) FROM review_forms g WHERE g.family = f.family) "
        "ORDER BY f.family = ? DESC, f.name COLLATE NOCASE", (STARTER_FORM,))]


def forms_chosen(db) -> dict:
    """{kind: form} as HR chose, each a form that exists (else the starter form)."""
    chosen = db.setting_json("reviews_forms", {}) or {}
    known = {f["family"] for f in families(db)}
    return {k: chosen.get(k) if chosen.get(k) in known else STARTER_FORM for k in KINDS}


def _insert_form(db, family: str, body: dict, actor_id, actor: str) -> int:
    version = (db.conn.execute("SELECT MAX(version) FROM review_forms WHERE family = ?",
                               (family,)).fetchone()[0] or 0) + 1
    cur = db.conn.execute("INSERT INTO review_forms (family, version, name, body, created_by, "
                          "created_by_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (family, version, body["name"], json.dumps(body), actor_id, actor,
                           time.time()))
    db.conn.commit()
    return cur.lastrowid


def save_form(db, family: str | None, data, actor_id, actor: str) -> dict:
    """A new version of a form (or a new form, family None). Raises ValueError."""
    body = clean_form(data)
    if family is None:
        known = {f["family"] for f in families(db)}
        if len(known) >= MAX_FORMS:
            raise ValueError(f"There can be at most {MAX_FORMS} forms.")
        n = len(known) + 1
        while f"form{n}" in known:
            n += 1
        family = f"form{n}"
    elif not latest(db, family):
        raise ValueError("That form isn't here.")
    return form(db, _insert_form(db, family, body, actor_id, actor))


def form_for(db, kind: str) -> dict:
    return latest(db, forms_chosen(db)[kind]) or latest(db, STARTER_FORM)


def sections_of(body: dict, dept: str | None) -> list[dict]:
    """The form's sections, with its extra ones for this department before the goals."""
    base = list(body["sections"])
    extra = body.get("departments", {}).get(str(dept or ""), [])
    at = next((i for i, s in enumerate(base) if s["kind"] == "goals"), len(base))
    return base[:at] + extra + base[at:]


def form_of(db, review: dict) -> tuple[dict, list[dict]]:
    body = form(db, review["form_id"])["body"]
    return body, sections_of(body, review["dept"])


def choices_of(review: dict, body: dict) -> dict:
    """{key: words} for the final recommendation."""
    if review["kind"] == "probation":
        return dict(PROBATION_CHOICES)
    return {r: r for r in body.get("recommendations", [])}


# ---------------------------------------------------------------- who writes it

def primaries(db) -> dict[str, str]:
    """{department: its primary manager}."""
    return {g: pid for pid, g in db.conn.execute(
        "SELECT person_id, group_id FROM manager_departments WHERE is_primary = 1")}


def main_group(db, person: dict) -> str | None:
    """Their main department: HR's choice, else the first of theirs with a primary manager."""
    groups = people.groups(person)
    row = db.conn.execute("SELECT main_group FROM review_people WHERE person_id = ?",
                          (person["id"],)).fetchone()
    if row and row[0] in groups:
        return row[0]
    prim = primaries(db)
    return next((g for g in groups if g in prim), groups[0] if groups else None)


def can_write_for(db, writer_id, person: dict) -> bool:
    """Whether someone may write this person's review: a manager, director or HR admin who sees
    their file, still here, and not the person themselves."""
    if not writer_id or str(writer_id) == person["id"]:
        return False
    w = db.person(writer_id)
    if not w or w["archived"] or w["access_ended"]:
        return False
    return access.person_relation(db, access.viewer_for(db, w), person) in STAFF_RELS


def default_writer(db, person: dict) -> str | None:
    group = main_group(db, person)
    manager = primaries(db).get(group) if group else None
    return manager if manager and can_write_for(db, manager, person) else None


def writer_of(db, review: dict, person: dict) -> str | None:
    """Who writes it now (a person ID), or None: HR (any HR admin but the person)."""
    who = review["assigned"]
    if who == "hr":
        return None
    if who and can_write_for(db, who, person):
        return who
    return default_writer(db, person)


def writes(db, viewer, review: dict, person: dict, rel: str | None) -> bool:
    w = writer_of(db, review, person)
    return viewer.id == w if w else rel == "hr"


def writer_name(db, review: dict, person: dict) -> str:
    w = writer_of(db, review, person)
    p = db.person(w) if w else None
    return p["name"] if p else "HR"


def tell_writer(db, review: dict, person: dict, kind: str, title: str, once: str, but=()) -> None:
    w = writer_of(db, review, person)
    for pid in [w] if w else hr_admins(db, but=[person["id"]]):
        if pid not in set(map(str, but)):
            notify.notify(pid, kind, link=f"#/reviews/{review['id']}", title=title, once=once, db=db)


# ---------------------------------------------------------------- reviews

def get(db, review_id) -> dict | None:
    try:
        rid = int(review_id)
    except (TypeError, ValueError):
        return None
    row = db.conn.execute("SELECT * FROM reviews WHERE id = ?", (rid,)).fetchone()
    return dict(row) if row else None


def of_person(db, person_id) -> list[dict]:
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM reviews WHERE person_id = ? ORDER BY due DESC, id DESC", (str(person_id),))]


def needs_self(db, kind: str) -> bool:
    chosen = setting(db, "reviews_self")
    return chosen == "all" or (chosen == "annual" and kind == "annual")


def needs_approval(db, review: dict) -> bool:
    chosen = setting(db, "reviews_second")
    return chosen == "all" or (chosen == "probation" and review["kind"] == "probation")


def untouched(review: dict) -> bool:
    return (review["status"] == "writing" and not review["started"] and not review["self_texts"]
            and _ratings(review, "self_ratings") == {} and review["self_status"] != "done")


def create(db, person: dict, kind: str, due: date, *, source: str | None = None,
           period: tuple | None = None, probation_id=None, assigned: str | None = None,
           actor_id=None, actor: str = "HR portal") -> int | None:
    """Open a review. Returns its ID, or None when the same one (source) is already open."""
    f = form_for(db, kind)
    start, end = period or (person["start_date"] or due.isoformat(), due.isoformat())
    cur = db.conn.execute(
        "INSERT OR IGNORE INTO reviews (person_id, kind, source, form_id, dept, period_start, "
        "period_end, due, probation_id, assigned, self_status, created_by, created_at) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (person["id"], kind, source, f["id"], main_group(db, person), str(start), str(end),
         due.isoformat(), probation_id, assigned, "waiting" if needs_self(db, kind) else "none",
         actor_id, time.time()))
    db.conn.commit()
    if not cur.rowcount:
        return None
    review = get(db, cur.lastrowid)
    audit.record(db, "Opened a review", actor_id=actor_id, actor=actor, target_id=person["id"],
                 target=person["name"], detail=f"{KINDS[kind]}, due {due.isoformat()}")
    tell_writer(db, review, person, "review_due",
                f"{person['name']}'s {NOUNS[kind]} is yours to write, due {nice(due)}.",
                f"review:{review['id']}:open", but=[actor_id] if actor_id else ())
    if review["self_status"] == "waiting":
        notify.notify(person["id"], "review_self", link=f"#/reviews/{review['id']}",
                      once=f"review:{review['id']}:self", db=db)
    return review["id"]


def near(db, person_id, due: date, gap: int) -> list[dict]:
    """Their other reviews (not cancelled) due within gap days of this date."""
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM reviews WHERE person_id = ? AND status != 'cancelled' AND due BETWEEN ? AND ?",
        (str(person_id), (due - timedelta(days=gap)).isoformat(),
         (due + timedelta(days=gap)).isoformat()))]


def open_scheduled(db, person: dict, kind: str, due: date, source: str, period: tuple,
                   probation_id=None, coming: dict | None = None) -> int | None:
    """A review the schedule calls for, unless another for the same person is due within the gap:
    then the more important one wins (an end-of-probation review over a check-in), and a less
    important one nobody has started is cancelled. coming: {person: date} of end-of-probation
    reviews not open yet, so a check-in just before one is skipped too."""
    if db.conn.execute("SELECT 1 FROM reviews WHERE person_id = ? AND source = ?",
                       (person["id"], source)).fetchone():
        return None
    gap = number(db, "reviews_gap_days")
    others = near(db, person["id"], due, gap) if gap else []
    if any(PRIORITY[r["kind"]] >= PRIORITY[kind] for r in others):
        return None
    ahead = (coming or {}).get(person["id"])
    if gap and ahead and kind != "probation" and abs((ahead - due).days) <= gap:
        return None
    rid = create(db, person, kind, due, source=source, period=period, probation_id=probation_id)
    for r in others:
        if rid and r["source"] and untouched(r):
            cancel(db, r, f"Replaced by the {NOUNS[kind]} due {nice(due)}.",
                   None, "HR portal")
    return rid


def cancel(db, review: dict, reason: str, actor_id, actor: str) -> None:
    db.conn.execute("UPDATE reviews SET status = 'cancelled', cancelled_by = ?, cancelled_at = ?, "
                    "cancel_reason = ? WHERE id = ?",
                    (actor_id, time.time(), crypto.seal_text(reason, "reviews.cancel_reason"),
                     review["id"]))
    db.conn.commit()
    person = db.person(review["person_id"])
    audit.record(db, "Cancelled a review", actor_id=actor_id, actor=actor,
                 target_id=review["person_id"], target=person["name"] if person else None,
                 detail=f"{KINDS[review['kind']]}, due {review['due']}")


def move(db, review: dict, due: date, end: date | None) -> None:
    """The probation's end date moved: its review moves with it."""
    db.conn.execute("UPDATE reviews SET due = ?, period_end = COALESCE(?, period_end) WHERE id = ?",
                    (due.isoformat(), end.isoformat() if end else None, review["id"]))
    db.conn.commit()
    person = db.person(review["person_id"])
    audit.record(db, "Moved a review with the end of the introductory period",
                 target_id=review["person_id"], target=person and person["name"],
                 detail=f"Due {review['due']} -> {due.isoformat()}")
    if person:
        tell_writer(db, get(db, review["id"]), person, "review_moved",
                    f"{person['name']}'s {NOUNS[review['kind']]} is now due "
                    f"{nice(due)}.", f"review:{review['id']}:moved:{due.isoformat()}")


# ---------------------------------------------------------------- the nightly run

def probation_rows(db, until: date) -> list[dict]:
    """probation.end_review_due(db, until): end-of-probation reviews due by then, cleaned."""
    fn = getattr(other("probation"), "end_review_due", None)
    if fn is None or setting(db, "reviews_probation") != "1":
        return []
    try:
        rows = fn(db, until) or []
    except Exception as err:  # noqa: BLE001 - another part's problem mustn't stop the schedule
        log.warning("HR reviews: reading probation dates failed: %r", err)
        return []
    found = []
    for row in rows:
        try:
            due, end = _date(row["due"]), _date(row.get("end"))
            if due:
                found.append({"person_id": str(row["person_id"]), "probation_id": row["probation_id"],
                              "due": due, "end": end})
        except (KeyError, TypeError, AttributeError):
            continue
    return found


def follow_probation(db, today: date) -> int:
    """Open end-of-probation reviews, and move open ones whose probation end date moved."""
    open_days, opened = number(db, "reviews_open_days"), 0
    current = getattr(other("probation"), "current", None)
    if current and setting(db, "reviews_probation") == "1":
        for r in [dict(x) for x in db.conn.execute(
                "SELECT * FROM reviews WHERE kind = 'probation' AND status IN ('writing', 'approval', "
                "'ready') AND probation_id IS NOT NULL")]:
            try:
                now_on = current(db, r["person_id"], today)
            except Exception as err:  # noqa: BLE001
                log.warning("HR reviews: reading a probation failed: %r", err)
                continue
            if not isinstance(now_on, dict) or str(now_on.get("id")) != str(r["probation_id"]):
                continue
            new_end, old_end, due = _date(now_on.get("end")), _date(r["period_end"]), _date(r["due"])
            if new_end and old_end and due and new_end != old_end:
                move(db, r, due + (new_end - old_end), new_end)
    for row in probation_rows(db, today + timedelta(days=open_days)):
        person = db.person(row["person_id"])
        if not person or person["archived"]:
            continue
        source = f"probation:{row['probation_id']}"
        found = db.conn.execute("SELECT * FROM reviews WHERE person_id = ? AND source = ?",
                                (person["id"], source)).fetchone()
        if found:
            if found["status"] in MOVABLE and found["due"] != row["due"].isoformat():
                move(db, dict(found), row["due"], row["end"])
            continue
        if row["due"] > today + timedelta(days=open_days):
            continue
        start = _date(person["start_date"]) or (row["end"] or row["due"]) - timedelta(days=90)
        if open_scheduled(db, person, "probation", row["due"], source,
                          (start.isoformat(), (row["end"] or row["due"]).isoformat()),
                          probation_id=row["probation_id"]):
            opened += 1
    return opened


def schedule(db, today: date) -> int:
    """Open the annual reviews and check-ins due soon (a missed night catches up for a week)."""
    open_days, gap = number(db, "reviews_open_days"), number(db, "reviews_gap_days")
    annual, month = setting(db, "reviews_annual"), number(db, "reviews_annual_month")
    checkins = [d for d in (30, 60) if setting(db, f"reviews_checkin_{d}") == "1"]
    if annual == "off" and not checkins:
        return 0
    coming = {r["person_id"]: r["due"] for r in probation_rows(
        db, today + timedelta(days=open_days + gap))}

    def soon(due: date, opens: date | None = None) -> bool:
        first = opens or due - timedelta(days=open_days)
        return first <= today <= due + timedelta(days=CATCH_UP_DAYS)
    opened = 0
    for p in db.people(archived=False):
        start = _date(p["start_date"])
        if not start:
            continue
        for year in (today.year, today.year + 1):
            if annual == "anniversary":
                due = anniversary(start, year)
                if year > start.year and soon(due):
                    period = (max(start, anniversary(start, year - 1)).isoformat(), due.isoformat())
                    opened += bool(open_scheduled(db, p, "annual", due, f"annual:{year}", period,
                                                  coming=coming))
            elif annual == "month":
                due = month_end(year, month)
                opens = min(due - timedelta(days=open_days), date(year, month, 1))
                if soon(due, opens) and (due - start).days >= ANNUAL_MIN_DAYS:
                    period = (max(start, anniversary(due, year - 1)).isoformat(), due.isoformat())
                    opened += bool(open_scheduled(db, p, "annual", due, f"annual:{year}", period,
                                                  coming=coming))
        for days in checkins:
            due = start + timedelta(days=days)
            if soon(due):
                opened += bool(open_scheduled(db, p, f"checkin{days}", due, f"checkin{days}:{start}",
                                              (start.isoformat(), due.isoformat()), coming=coming))
    return opened


def cancel_leavers(db) -> int:
    """Reviews not shared yet of people who've left are cancelled."""
    rows = [dict(r) for r in db.conn.execute(
        "SELECT r.* FROM reviews r JOIN people p ON p.id = r.person_id WHERE p.archived = 1 AND "
        "r.status IN ('writing', 'approval', 'ready')")]
    for r in rows:
        cancel(db, r, "They left.", None, "HR portal")
    return len(rows)


def pass_recommendation(db, review: dict, actor_id) -> bool:
    """End of probation: the final recommendation goes to the probation part, where HR confirms
    the outcome. False while it can't (that part isn't here, or it failed): tried again nightly."""
    if review["kind"] != "probation" or review["recommendation_sent_at"] or \
            review["probation_id"] is None or review["recommendation"] not in PROBATION_CHOICES:
        return True
    fn = getattr(other("probation"), "record_recommendation", None)
    if fn is None:
        return False
    try:
        fn(db, review["probation_id"], review["recommendation"],
           _date(review["extend_to"]) if review["recommendation"] == "extend" else None,
           review["id"], actor_id)
    except Exception as err:  # noqa: BLE001 - kept, and tried again
        log.warning("HR reviews: passing a probation recommendation failed: %r", err)
        return False
    db.conn.execute("UPDATE reviews SET recommendation_sent_at = ? WHERE id = ?",
                    (time.time(), review["id"]))
    db.conn.commit()
    return True


def pass_waiting(db) -> int:
    rows = [dict(r) for r in db.conn.execute(
        "SELECT * FROM reviews WHERE kind = 'probation' AND status IN ('ready', 'shared', 'closed') "
        "AND recommendation_sent_at IS NULL")]
    return sum(1 for r in rows if r["recommendation"] in PROBATION_CHOICES
               and pass_recommendation(db, r, r["approved_by"] or r["submitted_by"]))


def remind(db, today: date, now: float | None = None) -> int:
    """Reminders: the writer before, on and after the due date (and HR once it's late by the set
    number of days), approvers when it's late, the employee about their self-review, and about
    acknowledging a shared review. Each once, however often the night runs."""
    now = time.time() if now is None else now
    before, every = number(db, "reviews_remind_before"), number(db, "reviews_remind_every")
    overdue, ack = number(db, "reviews_overdue_days"), number(db, "reviews_ack_days")
    count = 0
    for r in [dict(x) for x in db.conn.execute(
            "SELECT * FROM reviews WHERE status IN ('writing', 'approval', 'ready', 'shared')")]:
        person = db.person(r["person_id"])
        due = _date(r["due"])
        if not person or person["archived"] or not due:
            continue
        days, rid, name = (due - today).days, r["id"], person["name"]
        what, link = NOUNS[r["kind"]], f"#/reviews/{r['id']}"
        late = -days
        if r["status"] in ("writing", "ready"):
            title, key = None, None
            if 0 < days <= before:
                title, key = f"{name}'s {what} is due {nice(due)}.", f"before:{due}"
            elif days == 0:
                title, key = f"{name}'s {what} is due today.", f"due:{due}"
            elif late > 0 and late % every == 0:
                title, key = f"{name}'s {what} is {late} days late.", f"late:{due}:{late}"
            if title:
                tell_writer(db, r, person, "review_reminder", title, f"review:{rid}:{key}")
                count += 1
        if r["status"] in MOVABLE and late >= overdue:
            for pid in hr_admins(db, but=[person["id"]]):
                notify.notify(pid, "review_overdue", link=link, once=f"review:{rid}:overdue:{due}",
                              title=f"{name}'s {what} is {late} days late.", db=db)
            count += 1
        if r["status"] == "approval" and late > 0 and late % every == 0:
            for pid in hr_admins(db, but=[person["id"], r["submitted_by"]]):
                notify.notify(pid, "review_approval", link=link, once=f"review:{rid}:approve:{late}",
                              title=f"{name}'s {what} is waiting for your approval.", db=db)
        if r["status"] == "writing" and r["self_status"] == "waiting" and days in (before, 0):
            notify.notify(person["id"], "review_self", link=link, once=f"review:{rid}:self:{days}",
                          db=db)
        if r["status"] == "shared" and r["shared_at"]:
            waited = int((now - r["shared_at"]) // DAY)
            if waited >= ack and waited % ack == 0:
                notify.notify(person["id"], "review_shared", link=link,
                              once=f"review:{rid}:ack:{waited}", db=db)
                if waited == ack:
                    tell_writer(db, r, person, "review_reminder",
                                f"{name} hasn't acknowledged their review yet.", f"review:{rid}:noack")
    return count


async def nightly(svc) -> dict:
    """The nightly run (service.daily_jobs)."""
    db = svc.db
    today = today_of(db)
    return {"cancelled": cancel_leavers(db), "probation": follow_probation(db, today),
            "scheduled": schedule(db, today), "passed": pass_waiting(db),
            "reminders": remind(db, today)}


# ---------------------------------------------------------------- the final PDF

def pdf_title(review: dict) -> str:
    """Its name in Drive (after the date): "Annual review 2026"."""
    if review["kind"] == "annual":
        return f"Annual review {review['due'][:4]}"
    return KINDS[review["kind"]] + (" review" if review["kind"] == "probation" else "")


def pdf_blocks(db, review: dict, person: dict, names: dict) -> list[tuple]:
    """What the employee saw, as plain blocks: ("heading", text), ("paragraph", text) and
    ("row", label, value). No private notes, no comments they can't see, and no end-of-probation
    recommendation (HR decides that in the probation part)."""
    body, sections = form_of(db, review)
    ratings, texts = _ratings(review), _json_sealed(review, "texts")
    scale = body["scale"]
    blocks = [("row", "Name", person["name"]),
              ("row", "Department", names.get(review["dept"] or "", "") or "None"),
              ("row", "Review", KINDS[review["kind"]]),
              ("row", "Covers", f"{nice(review['period_start'])} to {nice(review['period_end'])}"),
              ("row", "Written by", _name(db, review["submitted_by"])),
              ("row", "Meeting", nice(review["meeting_date"]) or "Not recorded")]
    if review["approved_by_name"]:
        blocks.append(("row", "Approved by", review["approved_by_name"]))
    for s in sections:
        blocks.append(("heading", s["title"]))
        if s["kind"] == "rating" and s["key"] in ratings:
            n = ratings[s["key"]]
            blocks.append(("row", "Rating", f"{n} of {len(scale)}: {scale[n - 1]}"))
        value = texts.get(s["key"])
        if s["kind"] == "goals":
            for goal in value if isinstance(value, list) else []:
                blocks.append(("paragraph", f"- {goal}"))
        elif isinstance(value, str) and value:
            blocks.append(("paragraph", value))
    o = overall_of(review, body, sections)
    blocks.append(("heading", "Overall"))
    if o["value"] is not None:
        blocks.append(("row", "Overall rating", f"{o['value']} of {len(scale)}: {o['word']}"))
    if o["override"] is not None:
        blocks.append(("paragraph", f"Worked out from the ratings: {o['auto']}. Why it differs: "
                                    f"{o['reason'] or ''}"))
    if texts.get(OVERALL):
        blocks.append(("paragraph", texts[OVERALL]))
    if review["kind"] != "probation" and review["recommendation"]:
        blocks.append(("row", "Recommendation", review["recommendation"]))
    for c in comments_for(db, review, "self"):
        blocks += [("heading", f"Comment from {c['author']}"), ("paragraph", c["text"])]
    blocks.append(("heading", "The employee"))
    if comment := _unseal(review["employee_comment"], "reviews.employee_comment"):
        blocks.append(("paragraph", comment))
    blocks.append(("row", ACK_WORDS.get(review["ack"] or "", "Closed"),
                   nice(notify.local_day(db, review["ack_at"] or review["closed_at"]))))
    if review["ack"] in ("received", "paper"):
        blocks.append(("paragraph", "Acknowledging means they received this review, not that "
                                    "they agree with it."))
    return blocks


def page_html(title: str, blocks: list[tuple]) -> str:
    """The PDF's HTML: the documents part's pdfdoc.page when it's here, else the same here.
    Everything is escaped: nothing people typed is ever HTML."""
    maker = getattr(other("pdfdoc"), "page", None)
    if maker is not None:
        return maker(title, blocks)
    esc = html.escape
    parts = [f"<h1>{esc(title)}</h1>"]
    for block in blocks:
        if block[0] == "heading":
            parts.append(f"<h2>{esc(block[1])}</h2>")
        elif block[0] == "paragraph":
            parts += [f"<p>{esc(line)}</p>" for line in str(block[1]).split("\n")]
        elif block[0] == "row":
            parts.append(f"<p><b>{esc(block[1])}:</b> {esc(block[2])}</p>")
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{esc(title)}</title></head>'
            f"<body>{''.join(parts)}</body></html>")


def _pdf_later(db, review: dict, message: str, now: float, failed: bool) -> None:
    tries = review["pdf_tries"] + (1 if failed else 0)
    wait = (PDF_RETRY[tries - 1] if tries <= len(PDF_RETRY) else DAY) if failed else PDF_WAIT
    db.conn.execute("UPDATE reviews SET pdf_tries = ?, pdf_next_at = ?, pdf_error = ? WHERE id = ?",
                    (tries, now + wait, message[:300], review["id"]))
    db.conn.commit()


async def make_pdf(svc, review: dict, now: float | None = None) -> bool:
    """A closed review's final PDF, made through Google Drive (documents.make_pdf) and saved to
    the person's Reviews folder (documents.add_generated). Kept and tried again later when that
    can't happen yet."""
    db, now = svc.db, time.time() if now is None else now
    docs = other("documents")
    maker, adder = getattr(docs, "make_pdf", None), getattr(docs, "add_generated", None)
    person = db.person(review["person_id"])
    if maker is None or adder is None or person is None:
        _pdf_later(db, review, NO_PDF_YET, now, False)
        return False
    # Claimed first, so a second look meanwhile leaves it alone.
    db.conn.execute("UPDATE reviews SET pdf_next_at = ? WHERE id = ?", (now + 600, review["id"]))
    db.conn.commit()
    title = pdf_title(review)
    unavailable = getattr(docs, "PdfUnavailable", None)
    try:
        page = page_html(f"{title}: {person['name']}",
                         pdf_blocks(db, review, person, group_names(svc)))
        data = await _done(maker(svc, page))
        doc = await _done(adder(svc, person, DOC_KIND, data, "application/pdf", title,
                                notify.local_day(db, review["closed_at"]), review["closed_by"]))
    except Exception as err:  # noqa: BLE001 - kept, and tried again
        waiting = isinstance(unavailable, type) and isinstance(err, unavailable)
        if not waiting:
            log.warning("HR reviews: making a review's PDF failed: %r", err)
        _pdf_later(db, get(db, review["id"]), str(err) if waiting else
                   "Making the PDF failed. It's tried again later.", now, not waiting)
        return False
    db.conn.execute("UPDATE reviews SET pdf_doc_id = ?, pdf_next_at = NULL, pdf_error = NULL "
                    "WHERE id = ?", (doc["id"] if isinstance(doc, dict) else doc, review["id"]))
    db.conn.commit()
    audit.record(db, "Saved a review's PDF", target_id=person["id"], target=person["name"],
                 detail=title)
    return True


async def make_pdfs(svc, now: float | None = None) -> int:
    now = time.time() if now is None else now
    rows = [dict(r) for r in svc.db.conn.execute(
        "SELECT * FROM reviews WHERE status = 'closed' AND pdf_doc_id IS NULL AND pdf_next_at <= ? "
        "ORDER BY pdf_next_at LIMIT 3", (now,))]
    made = 0
    for r in rows:
        if svc.running:
            made += await make_pdf(svc, r, now)
    return made


async def tick(svc, now: float) -> None:
    """Every half minute (service.tick_jobs): PDFs of closed reviews that are due."""
    await make_pdfs(svc, now)


# ---------------------------------------------------------------- what each viewer sees

def employee_sees(review: dict) -> bool:
    """Whether the employee sees their review: once it's shared, and before that only while
    their self-review is asked for (just that part)."""
    return review["status"] in ("shared", "closed") or (
        review["self_status"] in ("waiting", "done") and review["status"] != "cancelled")


def standing(db, viewer, review: dict | None) -> tuple[dict, str]:
    """(person, relation) for someone looking at a review, or Refused (404). The employee sees
    their own review once it's shared, and before that only their self-review."""
    person = db.person(review["person_id"]) if review else None
    rel = access.person_relation(db, viewer, person)
    if rel is None:
        raise Refused(NOT_FOUND, 404)
    if rel == "self" and not employee_sees(review):
        raise Refused(NOT_FOUND, 404)
    return person, rel


def overall_of(review: dict, body: dict, sections: list[dict]) -> dict:
    """The overall rating: worked out from the rated sections (their average), unless the writer
    chose their own, with a reason."""
    ratings, scale = _ratings(review), body["scale"]
    rated = [ratings[s["key"]] for s in sections if s["kind"] == "rating" and s["key"] in ratings]
    auto = round(sum(rated) / len(rated), 1) if rated else None
    value = review["overall"] if review["overall"] is not None else auto
    word = scale[max(1, min(len(scale), int(value + 0.5))) - 1] if value is not None else None
    return {"auto": auto, "override": review["overall"], "value": value, "word": word,
            "reason": _unseal(review["overall_reason"], "reviews.overall_reason")}


def comments_for(db, review: dict, rel: str, viewer_id: str | None = None) -> list[dict]:
    """The comments this relation may see (and the viewer's own): the employee only those
    marked for them, once the review is shared."""
    rows = [dict(r) for r in db.conn.execute(
        "SELECT * FROM review_comments WHERE review_id = ? ORDER BY id", (review["id"],))]
    if rel == "self" and review["status"] not in ("shared", "closed"):
        return []
    shown = {c["id"] for c in access.visible(rows, rel)}
    return [{"id": c["id"], "author": c["author_name"], "at": round(c["created_at"]),
             "visibility": c["visibility"],
             "level": COMMENT_LEVELS.get(c["visibility"], "HR admins only"),
             "text": _unseal(c["text"], "reviews.comment", "")}
            for c in rows if c["id"] in shown or (rel != "self" and c["author_id"] == viewer_id)]


def _name(db, pid) -> str:
    p = db.person(pid) if pid else None
    return p["name"] if p else ("HR" if not pid else str(pid))


def summary(db, review: dict, person: dict, rel: str, today: date) -> dict:
    status = review["status"]
    word = STATUS_WORDS[status]
    if rel == "self":
        word = {"shared": "Ready for you to read", "closed": "Closed"}.get(
            status, SELF_WORDS[review["self_status"]] or "Being written")
    found = {"id": review["id"], "person": {"id": person["id"], "name": person["name"]},
             "kind": review["kind"], "kind_word": KINDS[review["kind"]], "status": status,
             "status_word": word, "due": review["due"],
             "overdue": status in MOVABLE and review["due"] < today.isoformat(),
             "self_status": review["self_status"], "self_word": SELF_WORDS[review["self_status"]],
             "period": {"start": review["period_start"], "end": review["period_end"]},
             "writer": writer_name(db, review, person), "meeting_date": review["meeting_date"],
             "shared_at": review["shared_at"] and round(review["shared_at"]),
             "ack": review["ack"], "ack_word": ACK_WORDS.get(review["ack"] or ""),
             "ack_at": review["ack_at"] and round(review["ack_at"]),
             "closed_at": review["closed_at"] and round(review["closed_at"])}
    if rel != "self":
        found.update(submitted_by=review["submitted_by"] and _name(db, review["submitted_by"]),
                     approved_by=review["approved_by_name"], created_at=round(review["created_at"]),
                     cancelled=status == "cancelled" and {
                         "at": round(review["cancelled_at"] or 0),
                         "reason": _unseal(review["cancel_reason"], "reviews.cancel_reason")})
    return found


def side_panel(db, review: dict, person: dict, today: date) -> dict:
    """Read only, for those writing or overseeing it: points and write-ups in the period
    (discipline), certificates now (certificates), and the last review's goals. A part that isn't
    here (or fails) is None, shown as "Not available"."""
    start = _date(review["period_start"]) or today
    end = _date(review["period_end"]) or today
    found = {"period": {"start": start.isoformat(), "end": end.isoformat()}}
    fn = getattr(other("discipline"), "summary_for_review", None)
    try:
        got = fn(db, person["id"], start, end) if fn else None
    except Exception as err:  # noqa: BLE001 - shown as not available
        log.info("HR reviews: the discipline summary failed: %r", err)
        got = None
    if isinstance(got, dict):
        points = got.get("points") or {}
        found["discipline"] = {
            "points": [{"track": str(k), "total": _plain(v)} for k, v in points.items()]
            if isinstance(points, dict) else [],
            "writeups": [{"date": _plain(w.get("date")), "step": _plain(w.get("step")),
                          "status": _plain(w.get("status"))}
                         for w in got.get("writeups") or [] if isinstance(w, dict)],
            "next_drop": _plain(got.get("next_drop"))}
    else:
        found["discipline"] = None
    fn = getattr(other("certificates"), "status_of", None)
    try:
        certs = fn(db, person["id"], today) if fn else None
    except Exception as err:  # noqa: BLE001
        log.info("HR reviews: certificate status failed: %r", err)
        certs = None
    found["certificates"] = [
        {"name": _plain(c.get("name")), "expires": _plain(c.get("expires")),
         "status": CERT_WORDS.get(c.get("status"), _plain(c.get("status"))),
         "flag": _plain(c.get("flag"))}
        for c in certs if isinstance(c, dict)] if isinstance(certs, list) else None
    found["goals"] = last_goals(db, review)
    return found


def last_goals(db, review: dict) -> list[str]:
    """The goals set in their last closed review before this one."""
    row = db.conn.execute("SELECT * FROM reviews WHERE person_id = ? AND status = 'closed' AND "
                          "id != ? AND closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 1",
                          (review["person_id"], review["id"])).fetchone()
    if not row:
        return []
    row = dict(row)
    _, sections = form_of(db, row)
    texts = _json_sealed(row, "texts")
    return [g for s in sections if s["kind"] == "goals"
            for g in (texts.get(s["key"]) if isinstance(texts.get(s["key"]), list) else [])]


def can_of(db, viewer, review: dict, person: dict, rel: str) -> dict:
    status, writer = review["status"], writes(db, viewer, review, person, rel)
    staff = rel in STAFF_RELS
    return {
        "write": writer and status == "writing",
        "skip_self": writer and status == "writing" and review["self_status"] == "waiting",
        "self": rel == "self" and status == "writing" and review["self_status"] == "waiting",
        "approve": rel == "hr" and status == "approval" and viewer.id != review["submitted_by"],
        "share": (writer or rel == "hr") and status == "ready",
        "acknowledge": rel == "self" and status == "shared",
        "close": (writer or rel == "hr") and status == "shared",
        "reassign": rel == "hr" and status in MOVABLE,
        "cancel": rel == "hr" and status in MOVABLE,
        "comment": staff and status != "cancelled",
        "note": staff,
    }


def view(svc, viewer, review: dict, person: dict, rel: str) -> dict:
    """The review as this viewer may see it. Text they can't see is never in it."""
    db = svc.db
    today = today_of(db)
    body, sections = form_of(db, review)
    staff, status = rel in STAFF_RELS, review["status"]
    shared = status in ("shared", "closed")
    out = {"review": summary(db, review, person, rel, today), "relation": rel,
           "can": can_of(db, viewer, review, person, rel),
           "form": {"name": body["name"], "scale": body["scale"], "comment_at": body["comment_at"],
                    "sections": sections, "probation": review["kind"] == "probation",
                    "choices": choices_of(review, body)}}
    if staff or shared:
        out["answers"] = {"ratings": _ratings(review), "texts": _json_sealed(review, "texts")}
        out["overall"] = overall_of(review, body, sections)
        if staff or review["kind"] != "probation":
            out["recommendation"] = {"choice": review["recommendation"],
                                     "extend_to": review["extend_to"]}
        out["employee_comment"] = _unseal(review["employee_comment"], "reviews.employee_comment")
    if rel == "self" or review["self_status"] == "done":
        out["self_review"] = {"ratings": _ratings(review, "self_ratings"),
                              "texts": _json_sealed(review, "self_texts")}
    out["comments"] = comments_for(db, review, rel, viewer.id)
    if out["can"]["write"]:
        out["problems"] = problems(db, review)
    if staff:
        out["note"] = _unseal((db.conn.execute(
            "SELECT text FROM review_notes WHERE review_id = ? AND author_id = ?",
            (review["id"], viewer.id)).fetchone() or [None])[0], "reviews.note", "")
        out["returned_note"] = _unseal(review["returned_note"], "reviews.returned_note")
        out["close_note"] = _unseal(review["close_note"], "reviews.close_note")
        out["side"] = side_panel(db, review, person, today)
        out["levels"] = COMMENT_LEVELS
    if rel == "hr":
        out["notes"] = [{"author": r["author_name"], "at": round(r["updated_at"]),
                         "text": _unseal(r["text"], "reviews.note", "")}
                        for r in db.conn.execute(
                            "SELECT * FROM review_notes WHERE review_id = ? AND author_id != ? "
                            "ORDER BY updated_at", (review["id"], viewer.id))]
        out["writers"] = writer_choices(db, person)
        out["assigned"] = review["assigned"] or "auto"
        out["recommendation_sent"] = bool(review["recommendation_sent_at"])
    if shared:
        out["pdf"] = pdf_state(db, review, rel)
    return out


def pdf_state(db, review: dict, rel: str) -> dict:
    found = {"ready": bool(review["pdf_doc_id"]), "link": None}
    docs = other("documents")
    url = getattr(docs, "view_url", None)
    if review["pdf_doc_id"] and url is not None:
        try:
            doc = docs.get(db, review["pdf_doc_id"])
            found["link"] = url(doc) if doc else None
        except Exception as err:  # noqa: BLE001 - the link is left out
            log.info("HR reviews: no link to a review's PDF: %r", err)
    if rel == "hr" and not review["pdf_doc_id"] and review["status"] == "closed":
        found["error"] = review["pdf_error"] or "Waiting to be made."
    return found


def writer_choices(db, person: dict) -> list[dict]:
    """Who HR can hand this review to: managers, directors and HR admins who see the person."""
    ids = []
    for role in (access.MANAGER, access.DIRECTOR, access.HR_ADMIN):
        ids += [pid for pid in db.with_role(role) if pid not in ids]
    found = [{"id": pid, "name": _name(db, pid)} for pid in ids if can_write_for(db, pid, person)]
    return sorted(found, key=lambda c: c["name"].casefold())


def can_open(viewer, rel, doc) -> bool:
    """The documents part asks this before opening a review's PDF: the employee, and those who
    see their file."""
    return rel in ("self", *STAFF_RELS)


# ---------------------------------------------------------------- lists, sections and to-dos

def listing(db, viewer) -> dict:
    """The viewer's reviews: what they have to do now, others still open that they can see, and
    closed ones."""
    day = today_of(db)
    today = day.isoformat()
    roles = db.all_roles()
    if viewer.is_staff:
        rows = db.conn.execute("SELECT * FROM reviews WHERE status != 'cancelled' ORDER BY due, id")
    else:
        rows = db.conn.execute("SELECT * FROM reviews WHERE person_id = ? AND status != 'cancelled' "
                               "ORDER BY due, id", (viewer.id,))
    seen: dict[str, tuple] = {}
    found = {"todo": [], "open": [], "closed": []}
    for r in [dict(x) for x in rows]:
        if r["person_id"] not in seen:
            p = db.person(r["person_id"])
            seen[r["person_id"]] = (p, p and access.relation(viewer, p, roles.get(p["id"], set())))
        person, rel = seen[r["person_id"]]
        if not rel:
            continue
        if rel == "self" and not employee_sees(r):
            continue
        can = can_of(db, viewer, r, person, rel)
        what = ("Fill in your self-review" if can["self"] else "Read and acknowledge it"
                if can["acknowledge"] else "Approve it" if can["approve"] else "Write it"
                if can["write"] else "Share it" if can["share"] else None)
        item = {"id": r["id"], "person": person["name"], "person_id": person["id"],
                "kind_word": KINDS[r["kind"]], "due": r["due"], "what": what,
                "status_word": summary(db, r, person, rel, day)["status_word"],
                "overdue": r["status"] in MOVABLE and r["due"] < today}
        if what:
            found["todo"].append(item)
        elif r["status"] == "closed":
            found["closed"].append(item)
        elif rel != "self" or r["status"] == "shared":
            found["open"].append(item)
    found["closed"] = sorted(found["closed"], key=lambda i: i["due"], reverse=True)[:100]
    return found


@todo
def review_todos(svc, v) -> list[dict]:
    db = svc.db
    todos = listing(db, v)["todo"]
    found = []
    counts: dict[str, int] = {}
    for item in todos:
        counts[item["what"]] = counts.get(item["what"], 0) + 1
    words = {"Write it": "review{s} to write", "Share it": "review{s} to share",
             "Approve it": "review{s} waiting for your approval"}
    for what, n in counts.items():
        if what in words:
            found.append({"text": f"{n} {words[what].format(s='' if n == 1 else 's')}.",
                          "link": "#/reviews"})
        elif what == "Fill in your self-review":
            found.append({"text": "Please fill in your self-review.", "link": "#/reviews"})
        else:
            found.append({"text": "You have a review to read and acknowledge.", "link": "#/reviews"})
    if v.is_hr:
        stuck = db.conn.execute("SELECT COUNT(*) FROM reviews WHERE status = 'closed' AND "
                                "pdf_doc_id IS NULL AND pdf_tries >= 3").fetchone()[0]
        if stuck:
            found.append({"text": f"{stuck} closed review{'s' if stuck != 1 else ''} couldn't be "
                                  "saved to Google Drive yet. They're tried again.",
                          "link": "#/reviews"})
        unsent = db.conn.execute("SELECT COUNT(*) FROM reviews WHERE kind = 'probation' AND status IN "
                                 "('ready', 'shared', 'closed') AND recommendation_sent_at IS NULL"
                                 ).fetchone()[0]
        if unsent:
            found.append({"text": f"{unsent} end-of-probation recommendation"
                                  f"{'s' if unsent != 1 else ''} couldn't reach the introductory "
                                  "period records yet.", "link": "#/reviews"})
    return found


@section("reviews", 60)
def reviews_section(svc, v, p: dict, rel: str) -> dict | None:
    db = svc.db
    today = today_of(db)
    items = []
    for r in of_person(db, p["id"]):
        if r["status"] == "cancelled" and rel == "self":
            continue
        if rel == "self" and not employee_sees(r):
            continue
        s = summary(db, r, p, rel, today)
        items.append({"id": r["id"], "kind_word": s["kind_word"], "due": r["due"],
                      "status_word": s["status_word"], "overdue": s["overdue"]})
    found = {"title": "Reviews", "items": items, "rows": [],
             "can_start": rel in STAFF_RELS and not p["archived"]}
    if rel == "hr":
        names = group_names(svc)
        main, chosen = main_group(db, p), db.conn.execute(
            "SELECT main_group FROM review_people WHERE person_id = ?", (p["id"],)).fetchone()
        w = default_writer(db, p)
        found["main"] = {"group": main or "", "chosen": bool(chosen and chosen[0] == main),
                         "name": names.get(main, f"Group {main}") if main else "None",
                         "writer": _name(db, w) if w else "HR",
                         "groups": [{"group": g, "name": names.get(g, f"Group {g}")}
                                    for g in people.groups(p)]}
    found["rows"] = [{"label": i["kind_word"], "value": f"{i['status_word']} (due {i['due']})"}
                     for i in items[:10]]
    return found


@settings_section("reviews", 60)
def reviews_settings(svc, v) -> dict:
    return {"values": settings_of(svc.db), "forms": families(svc.db), "kinds": KINDS}


# ---------------------------------------------------------------- routes

_edits: dict[tuple, float] = {}


def edited(db, viewer, review: dict, person: dict, action: str = "Worked on a review") -> None:
    """Saving as people type: one audit line per person, review and kind of change every
    EDIT_AUDIT_GAP seconds."""
    key, now = (viewer.id, review["id"], action), time.time()
    if now - _edits.get(key, 0) >= EDIT_AUDIT_GAP:
        if len(_edits) > 2000:
            _edits.clear()
        _edits[key] = now
        audit.record(db, action, actor_id=viewer.id, actor=viewer.name, target_id=person["id"],
                     target=person["name"], detail=f"{KINDS[review['kind']]}, due {review['due']}")


def loaded(request) -> tuple:
    svc = svc_of(request)
    v = viewer_of(request)
    review = get(svc.db, request.match_info["rid"])
    if review is None:
        raise Refused(NOT_FOUND, 404)
    person, rel = standing(svc.db, v, review)
    return svc, v, review, person, rel


def changed(svc, request, v, review: dict, person: dict, action: str, detail: str = "") -> None:
    audit.record(svc.db, action, actor_id=v.id, actor=v.name, target_id=person["id"],
                 target=person["name"], ip=svc.client_ip(request),
                 detail=f"{KINDS[review['kind']]}, due {review['due']}" +
                 (f": {detail}" if detail else ""))


def must(ok: bool, message: str, status: int = 409) -> None:
    if not ok:
        raise Refused(message, status)


def _rating(value, scale: list) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= len(scale):
        raise ValueError("Choose a rating on the scale.")
    return value


def _goals(value) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_GOALS:
        raise ValueError(f"Add at most {MAX_GOALS} goals.")
    return [g for g in (_text(x, GOAL_TEXT) for x in value) if g]


def answer(review: dict, sections: list[dict], scale: list, data: dict, prefix: str = "") -> dict:
    """One section's rating, comment or goals from what was typed: the columns to save."""
    key = str(data.get("section") or "")
    sec = {"key": OVERALL, "kind": "text"} if key == OVERALL else \
        next((s for s in sections if s["key"] == key), None)
    if sec is None:
        raise ValueError("That section isn't on this form.")
    ratings, texts = _ratings(review, prefix + "ratings"), _json_sealed(review, prefix + "texts")
    if "rating" in data:
        if sec["kind"] != "rating":
            raise ValueError("That section isn't rated.")
        value = _rating(data["rating"], scale)
        if value is None:
            ratings.pop(key, None)
        else:
            ratings[key] = value
    if "text" in data:
        if sec["kind"] == "goals":
            raise ValueError("Goals are a list.")
        text = _text(data["text"])
        if text:
            texts[key] = text
        else:
            texts.pop(key, None)
    if "goals" in data:
        if sec["kind"] != "goals":
            raise ValueError("That section has no goals.")
        goals = _goals(data["goals"])
        if goals:
            texts[key] = goals
        else:
            texts.pop(key, None)
    return {prefix + "ratings": json.dumps(ratings),
            prefix + "texts": _seal_json(texts, prefix + "texts")}


def _save(db, review: dict, columns: dict) -> dict:
    sets = ", ".join(f"{k} = ?" for k in columns)
    db.conn.execute(f"UPDATE reviews SET {sets} WHERE id = ?", (*columns.values(), review["id"]))
    db.conn.commit()
    return get(db, review["id"])


@rule("signed_in")
async def api_reviews(request: web.Request):
    svc = svc_of(request)
    v = viewer_of(request)
    found = listing(svc.db, v)
    audit.record(svc.db, "Viewed the reviews list", actor_id=v.id, actor=v.name,
                 ip=svc.client_ip(request))
    return web.json_response(found | {"staff": v.is_staff, "hr": v.is_hr})


@rule("signed_in")
async def api_review(request: web.Request):
    svc, v, review, person, rel = loaded(request)
    audit.record(svc.db, "Viewed a review", actor_id=v.id, actor=v.name, target_id=person["id"],
                 target=person["name"], ip=svc.client_ip(request),
                 detail=f"{KINDS[review['kind']]}, due {review['due']}")
    return web.json_response(view(svc, v, review, person, rel))


@rule("signed_in")
async def api_answer(request: web.Request):
    """The writer's rating, comment or goals for one section, saved as they go."""
    data = await read_json(request)
    svc, v, review, person, rel = loaded(request)
    must(can_of(svc.db, v, review, person, rel)["write"],
         "Only the person writing this review can change it, while it's being written.", 403)
    body, sections = form_of(svc.db, review)
    try:
        columns = answer(review, sections, body["scale"], data)
    except ValueError as err:
        return error(str(err))
    review = _save(svc.db, review, columns | {"started": 1})
    edited(svc.db, v, review, person)
    return web.json_response({"ok": True, "overall": overall_of(review, body, sections),
                              "problems": problems(svc.db, review)})


@rule("signed_in")
async def api_overall(request: web.Request):
    """The writer's own overall rating (with a reason), and the final recommendation."""
    data = await read_json(request)
    svc, v, review, person, rel = loaded(request)
    must(can_of(svc.db, v, review, person, rel)["write"],
         "Only the person writing this review can change it, while it's being written.", 403)
    body, sections = form_of(svc.db, review)
    columns = {}
    try:
        if "override" in data:
            columns["overall"] = _rating(data["override"], body["scale"])
        if "reason" in data:
            columns["overall_reason"] = crypto.seal_text(_text(data["reason"]) or None,
                                                         "reviews.overall_reason")
        if "recommendation" in data:
            choice = data["recommendation"] or None
            if choice is not None and choice not in choices_of(review, body):
                raise ValueError("Choose a recommendation from the list.")
            columns["recommendation"] = choice
        if "extend_to" in data:
            value = data["extend_to"] or None
            if value is not None and not _date(value):
                raise ValueError("Type the date as YYYY-MM-DD, like 2026-12-01.")
            columns["extend_to"] = _date(value).isoformat() if value else None
    except ValueError as err:
        return error(str(err))
    if columns:
        review = _save(svc.db, review, columns | {"started": 1})
        edited(svc.db, v, review, person)
    return web.json_response({"ok": True, "overall": overall_of(review, body, sections),
                              "problems": problems(svc.db, review)})


@rule("signed_in")
async def api_self(request: web.Request):
    """The employee's self-review, saved as they go."""
    data = await read_json(request)
    svc, v, review, person, rel = loaded(request)
    must(can_of(svc.db, v, review, person, rel)["self"], "Your self-review can't be changed now.")
    body, sections = form_of(svc.db, review)
    try:
        columns = answer(review, sections, body["scale"], data, prefix="self_")
    except ValueError as err:
        return error(str(err))
    _save(svc.db, review, columns)
    edited(svc.db, v, review, person, "Worked on a self-review")
    return web.json_response({"ok": True})


@rule("signed_in")
async def api_self_send(request: web.Request):
    svc, v, review, person, rel = loaded(request)
    must(can_of(svc.db, v, review, person, rel)["self"], "Your self-review can't be sent now.")
    _save(svc.db, review, {"self_status": "done"})
    changed(svc, request, v, review, person, "Sent a self-review")
    tell_writer(svc.db, review, person, "review_self_done", f"{person['name']} sent their self-review.",
                f"review:{review['id']}:self_done")
    return web.json_response({"ok": True})


@rule("signed_in")
async def api_skip_self(request: web.Request):
    svc, v, review, person, rel = loaded(request)
    must(can_of(svc.db, v, review, person, rel)["skip_self"], "There's no self-review to wait for.")
    _save(svc.db, review, {"self_status": "skipped"})
    changed(svc, request, v, review, person, "Carried on without the self-review")
    return web.json_response({"ok": True})


@rule("signed_in")
async def api_note(request: web.Request):
    """A private note: only its writer and HR admins see it."""
    data = await read_json(request)
    svc, v, review, person, rel = loaded(request)
    must(can_of(svc.db, v, review, person, rel)["note"], "You can't keep notes on this review.", 403)
    text = _text(data.get("text"))
    if text:
        svc.db.conn.execute("INSERT OR REPLACE INTO review_notes (review_id, author_id, author_name, "
                            "text, updated_at) VALUES (?, ?, ?, ?, ?)",
                            (review["id"], v.id, v.name, crypto.seal_text(text, "reviews.note"),
                             time.time()))
    else:
        svc.db.conn.execute("DELETE FROM review_notes WHERE review_id = ? AND author_id = ?",
                            (review["id"], v.id))
    svc.db.conn.commit()
    edited(svc.db, v, review, person, "Changed a private note on a review")
    return web.json_response({"ok": True})


@rule("signed_in")
async def api_comment(request: web.Request):
    """A comment from someone else who sees the person, with who may see it."""
    data = await read_json(request)
    svc, v, review, person, rel = loaded(request)
    must(can_of(svc.db, v, review, person, rel)["comment"], "You can't comment on this review.", 403)
    text = _text(data.get("text"))
    level = data.get("visibility") or "managers"
    if not text:
        return error("Type a comment first.")
    if level not in COMMENT_LEVELS:
        return error("Choose who can see it.")
    svc.db.conn.execute("INSERT INTO review_comments (review_id, author_id, author_name, text, "
                        "visibility, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (review["id"], v.id, v.name, crypto.seal_text(text, "reviews.comment"), level,
                         time.time()))
    svc.db.conn.commit()
    changed(svc, request, v, review, person, "Commented on a review", COMMENT_LEVELS[level])
    return web.json_response({"ok": True, "comments": comments_for(svc.db, review, rel, v.id)})


def problems(db, review: dict) -> list[str]:
    """What's missing before the writer can finish."""
    body, sections = form_of(db, review)
    ratings, texts = _ratings(review), _json_sealed(review, "texts")
    found = []
    for s in sections:
        if s["kind"] != "rating":
            continue
        if s["key"] not in ratings:
            found.append(f"Rate “{s['title']}”.")
        elif ratings[s["key"]] <= body["comment_at"] and not texts.get(s["key"]):
            found.append(f"Add a comment to “{s['title']}”: low ratings need one.")
    reason = _unseal(review["overall_reason"], "reviews.overall_reason")
    if review["overall"] is not None and not reason:
        found.append("Say why the overall rating differs from the worked-out one.")
    choices = choices_of(review, body)
    if choices and review["recommendation"] not in choices:
        found.append("Choose a recommendation.")
    if review["kind"] == "probation" and review["recommendation"] == "extend":
        end = _date(review["extend_to"])
        if not end or end <= (_date(review["period_end"]) or today_of(db)):
            found.append("Choose the new end date, after the current one.")
    if review["self_status"] == "waiting":
        found.append("The self-review hasn't been sent yet. Wait for it, or carry on without it.")
    return found


@rule("signed_in")
async def api_submit(request: web.Request):
    """The writer is done: to HR for approval when that's needed, else ready to share."""
    svc, v, review, person, rel = loaded(request)
    db = svc.db
    must(can_of(db, v, review, person, rel)["write"],
         "Only the person writing this review can finish it.", 403)
    missing = problems(db, review)
    if missing:
        return error(missing[0], problems=missing)
    approval = needs_approval(db, review)
    now = time.time()
    review = _save(db, review, {"status": "approval" if approval else "ready", "submitted_by": v.id,
                                "submitted_at": now})
    changed(svc, request, v, review, person, "Finished writing a review")
    if approval:
        for pid in hr_admins(db, but=[person["id"], v.id]):
            notify.notify(pid, "review_approval", link=f"#/reviews/{review['id']}", db=db,
                          title=f"{person['name']}'s {NOUNS[review['kind']]} needs your "
                                "approval.", once=f"review:{review['id']}:submitted:{round(now)}")
    else:
        pass_recommendation(db, review, v.id)
    return web.json_response({"ok": True, "status": review["status"]})


@rule("signed_in")
async def api_approve(request: web.Request):
    svc, v, review, person, rel = loaded(request)
    db = svc.db
    must(rel == "hr" and review["status"] == "approval", "Only HR approves reviews that are waiting.",
         403)
    access.not_own_work(v, review["submitted_by"])
    review = _save(db, review, {"status": "ready", "approved_by": v.id, "approved_by_name": v.name,
                                "approved_at": time.time(), "returned_note": None})
    changed(svc, request, v, review, person, "Approved a review")
    pass_recommendation(db, review, v.id)
    if review["submitted_by"]:
        notify.notify(review["submitted_by"], "review_approved", link=f"#/reviews/{review['id']}",
                      db=db, title=f"{person['name']}'s review was approved: share it at your meeting.",
                      once=f"review:{review['id']}:approved")
    return web.json_response({"ok": True})


@rule("signed_in")
async def api_return(request: web.Request):
    """HR sends it back to the writer, with a note."""
    data = await read_json(request)
    svc, v, review, person, rel = loaded(request)
    db = svc.db
    must(rel == "hr" and review["status"] == "approval",
         "Only HR sends back reviews that are waiting.", 403)
    access.not_own_work(v, review["submitted_by"])
    note = _text(data.get("note"))
    if not note:
        return error("Say what needs changing.")
    review = _save(db, review, {"status": "writing", "returned_note":
                                crypto.seal_text(note, "reviews.returned_note")})
    changed(svc, request, v, review, person, "Sent a review back")
    if review["submitted_by"]:
        notify.notify(review["submitted_by"], "review_returned", link=f"#/reviews/{review['id']}",
                      db=db, title=f"HR sent {person['name']}'s review back to you.",
                      once=f"review:{review['id']}:returned:{round(time.time())}")
    return web.json_response({"ok": True})


@rule("signed_in")
async def api_share(request: web.Request):
    """Shared with the employee, with the date of the meeting."""
    data = await read_json(request)
    svc, v, review, person, rel = loaded(request)
    db = svc.db
    must(can_of(db, v, review, person, rel)["share"], "This review isn't ready to share.")
    meeting, today = _date(data.get("meeting_date")), today_of(db)
    if not meeting or abs((meeting - today).days) > 90:
        return error("Type the meeting date as YYYY-MM-DD, within 90 days of today.")
    review = _save(db, review, {"status": "shared", "meeting_date": meeting.isoformat(),
                                "shared_by": v.id, "shared_at": time.time()})
    changed(svc, request, v, review, person, "Shared a review", f"meeting {meeting.isoformat()}")
    notify.notify(person["id"], "review_shared", link=f"#/reviews/{review['id']}", db=db,
                  once=f"review:{review['id']}:shared")
    return web.json_response({"ok": True})


def close(svc, v, review: dict, person: dict, how: str, comment: str | None = None,
          note: str | None = None) -> dict:
    db, now = svc.db, time.time()
    columns = {"status": "closed", "ack": how, "ack_at": now, "closed_by": v.id, "closed_at": now,
               "pdf_next_at": now}
    if comment:
        columns["employee_comment"] = crypto.seal_text(comment, "reviews.employee_comment")
    if note:
        columns["close_note"] = crypto.seal_text(note, "reviews.close_note")
    review = _save(db, review, columns)
    for pid in {*(hr_admins(db, but=[person["id"]])), review["submitted_by"] or ""} - {"", v.id}:
        notify.notify(pid, "review_acknowledged", link=f"#/reviews/{review['id']}", db=db,
                      title=f"{person['name']}'s review is closed: {ACK_WORDS[how].lower()}.",
                      once=f"review:{review['id']}:closed")
    return review


@rule("signed_in")
async def api_acknowledge(request: web.Request):
    """The employee: received (not agreeing), or refused, with a comment if they like."""
    data = await read_json(request)
    svc, v, review, person, rel = loaded(request)
    must(can_of(svc.db, v, review, person, rel)["acknowledge"],
         "This review can't be acknowledged now.")
    how = data.get("answer")
    if how not in ("received", "refused"):
        return error("Choose whether you've received it, or refuse.")
    review = close(svc, v, review, person, how, comment=_text(data.get("comment")) or None)
    changed(svc, request, v, review, person, "Acknowledged a review" if how == "received"
            else "Refused to acknowledge a review")
    return web.json_response({"ok": True})


@rule("signed_in")
async def api_close(request: web.Request):
    """The writer (or HR) closes a shared review: the employee refused in the meeting, signed a
    printed copy, or didn't answer."""
    data = await read_json(request)
    svc, v, review, person, rel = loaded(request)
    must(can_of(svc.db, v, review, person, rel)["close"], "Only a shared review can be closed.")
    how = data.get("how")
    if how not in ("refused", "paper", "no_answer"):
        return error("Choose what happened.")
    review = close(svc, v, review, person, how, note=_text(data.get("note")) or None)
    changed(svc, request, v, review, person, "Closed a review", ACK_WORDS[how])
    return web.json_response({"ok": True})


@rule("signed_in")
async def api_writer(request: web.Request):
    """HR hands the review to someone else (a manager, director or HR admin who sees the person),
    to HR, or back to the rule (the primary manager of their main department)."""
    data = await read_json(request)
    svc, v, review, person, rel = loaded(request)
    db = svc.db
    must(can_of(db, v, review, person, rel)["reassign"],
         "Only HR can hand over a review not shared yet.", 403)
    who = str(data.get("writer") or "")
    if who not in ("auto", "hr") and not can_write_for(db, who, person):
        return error("Choose a manager, director or HR admin who can see their file.")
    review = _save(db, review, {"assigned": None if who == "auto" else who})
    changed(svc, request, v, review, person, "Handed over a review", writer_name(db, review, person))
    tell_writer(db, review, person, "review_due",
                f"{person['name']}'s {NOUNS[review['kind']]} is yours to write, due "
                f"{nice(review['due'])}.",
                f"review:{review['id']}:to:{who}:{round(time.time())}", but=[v.id])
    return web.json_response({"ok": True})


@rule("signed_in", sensitive=True)
async def api_cancel(request: web.Request):
    data = await read_json(request)
    svc, v, review, person, rel = loaded(request)
    must(can_of(svc.db, v, review, person, rel)["cancel"],
         "Only HR can cancel a review not shared yet.", 403)
    reason = _text(data.get("reason"), 300)
    if not reason:
        return error("Say why it's cancelled.")
    cancel(svc.db, review, reason, v.id, v.name)
    return web.json_response({"ok": True})


@rule("staff", person=True, not_self=True)
async def api_start(request: web.Request):
    """An extra review, started by a manager, director or HR: they write it (HR may leave it to
    the usual writer instead)."""
    svc, v = svc_of(request), viewer_of(request)
    db, p = svc.db, request[PERSON]
    data = await read_json(request)
    if p["archived"]:
        return error("They've left.", 409)
    today = today_of(db)
    due = _date(data.get("due")) or today + timedelta(days=14)
    if not today <= due <= today + timedelta(days=365):
        return error("Choose a due date within the next year.")
    assigned = None if v.is_hr and data.get("writer") == "auto" else v.id
    last = db.conn.execute("SELECT MAX(period_end) FROM reviews WHERE person_id = ? AND status = "
                           "'closed'", (p["id"],)).fetchone()[0]
    start = max(filter(None, (_date(last), _date(p["start_date"]))), default=today)
    rid = create(db, p, "adhoc", due, period=(start.isoformat(), due.isoformat()), assigned=assigned,
                 actor_id=v.id, actor=v.name)
    return web.json_response({"ok": True, "id": rid})


@rule("hr", person=True, not_self=True, sensitive=True)
async def api_main_group(request: web.Request):
    """HR picks someone's main department: its primary manager writes their reviews."""
    svc, v = svc_of(request), viewer_of(request)
    db, p = svc.db, request[PERSON]
    data = await read_json(request)
    group = str(data.get("group") or "")
    if group and group not in people.groups(p):
        return error("Choose one of their departments.")
    if group:
        db.conn.execute("INSERT OR REPLACE INTO review_people (person_id, main_group, set_by, set_at) "
                        "VALUES (?, ?, ?, ?)", (p["id"], group, v.id, time.time()))
    else:
        db.conn.execute("DELETE FROM review_people WHERE person_id = ?", (p["id"],))
    db.conn.commit()
    audit.record(db, "Chose the main department for reviews", actor_id=v.id, actor=v.name,
                 target_id=p["id"], target=p["name"], ip=svc.client_ip(request),
                 detail=group_names(svc).get(group, group) if group else "Automatic")
    return web.json_response({"ok": True})


@rule("hr")
async def api_forms(request: web.Request):
    svc = svc_of(request)
    return web.json_response({"forms": families(svc.db), "chosen": forms_chosen(svc.db),
                              "kinds": KINDS, "section_kinds": SECTION_KINDS,
                              "departments": departments_list(svc)})


@rule("hr")
async def api_form(request: web.Request):
    svc = svc_of(request)
    f = latest(svc.db, request.match_info["family"])
    if not f:
        return error(NOT_FOUND, 404)
    versions = [dict(r) for r in svc.db.conn.execute(
        "SELECT id, version, created_by_name, created_at FROM review_forms WHERE family = ? "
        "ORDER BY version DESC", (f["family"],))]
    used = svc.db.conn.execute("SELECT COUNT(*) FROM reviews WHERE form_id IN (SELECT id FROM "
                               "review_forms WHERE family = ?)", (f["family"],)).fetchone()[0]
    return web.json_response({"form": f, "versions": versions, "used": used,
                              "section_kinds": SECTION_KINDS, "departments": departments_list(svc)})


@rule("hr", sensitive=True)
async def api_form_save(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    data = await read_json(request)
    family = data.get("family") or None
    try:
        saved = save_form(svc.db, str(family) if family else None, data.get("form"), v.id, v.name)
    except ValueError as err:
        return error(str(err))
    audit.record(svc.db, "Saved a review form", actor_id=v.id, actor=v.name, ip=svc.client_ip(request),
                 detail=f"{saved['name']}, version {saved['version']}", security=True)
    return web.json_response({"ok": True, "form": saved})


@rule("hr", sensitive=True)
async def api_settings(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    db = svc.db
    data = await read_json(request)
    values = {}
    for key, allowed in CHOICES.items():
        if key in data:
            if str(data[key]) not in allowed:
                return error("Choose from the list.")
            values[key] = str(data[key])
    for key, (lo, hi) in NUMBERS.items():
        if key in data:
            try:
                values[key] = max(lo, min(hi, int(data[key])))
            except (TypeError, ValueError):
                return error("Type a number.")
    if "reviews_forms" in data:
        chosen, known = data["reviews_forms"], {f["family"] for f in families(db)}
        if not isinstance(chosen, dict) or not set(chosen) <= set(KINDS) or \
                not set(map(str, chosen.values())) <= known:
            return error("Choose a form for each kind of review.")
        values["reviews_forms"] = json.dumps({k: str(f) for k, f in chosen.items()})
    db.set_settings(values)
    audit.record(db, "Changed review settings", actor_id=v.id, actor=v.name, ip=svc.client_ip(request),
                 detail=", ".join(sorted(values)), security=True)
    return web.json_response({"ok": True, "values": settings_of(db)})


@add_routes
def routes(app: web.Application) -> None:
    r = app.router
    rid = r"/api/reviews/{rid:\d{1,12}}"
    r.add_get("/api/reviews", api_reviews)
    r.add_get(rid, api_review)
    r.add_put(rid + "/answer", api_answer)
    r.add_put(rid + "/overall", api_overall)
    r.add_put(rid + "/self", api_self)
    r.add_post(rid + "/self/send", api_self_send)
    r.add_post(rid + "/skip-self", api_skip_self)
    r.add_put(rid + "/note", api_note)
    r.add_post(rid + "/comments", api_comment)
    r.add_post(rid + "/submit", api_submit)
    r.add_post(rid + "/approve", api_approve)
    r.add_post(rid + "/return", api_return)
    r.add_post(rid + "/share", api_share)
    r.add_post(rid + "/acknowledge", api_acknowledge)
    r.add_post(rid + "/close", api_close)
    r.add_put(rid + "/writer", api_writer)
    r.add_post(rid + "/cancel", api_cancel)
    pid = r"/api/people/{pid:[A-Za-z0-9_-]{1,40}}"
    r.add_post(pid + "/reviews", api_start)
    r.add_put(pid + "/reviews/main-department", api_main_group)
    r.add_get("/api/review-forms", api_forms)
    r.add_get(r"/api/review-forms/{family:[a-z0-9_]{1,30}}", api_form)
    r.add_post("/api/review-forms", api_form_save)
    r.add_put("/api/settings/reviews", api_settings)


# ---------------------------------------------------------------- wiring

def setup(db) -> None:
    """Its tables, and the starter form the first time (on start)."""
    db.add_schema(SCHEMA)
    db.append_only("review_forms")
    db.append_only("review_comments")
    if not latest(db, STARTER_FORM):
        _insert_form(db, STARTER_FORM, STARTER, None, "HR portal")


def attach(svc) -> None:
    svc.on_start.append(lambda s: setup(s.db))
    svc.daily_jobs.append(nightly)
    svc.tick_jobs.append(tick)


def register_documents() -> bool:
    """Final PDFs are documents of their own kind ("reviews": the Reviews folder in Drive), shown
    here rather than in the Documents section, opened by those who see the review. False while
    the documents part can't take it yet."""
    register = getattr(other("documents"), "register_kind", None)
    if register is None:
        return False
    register(DOC_KIND, "Reviews", "managers", "Reviews", "reviews", can_open=can_open)
    return True


PARTS.append(attach)
register_documents()
