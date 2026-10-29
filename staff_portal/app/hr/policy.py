"""The point system (design 7.1 to 7.6, 7.8's rules and 7.11): HR's discipline policy as settings,
not programming, saved as versions.

A version is a draft (HR edits it; nothing uses it), published (in force from its effective date,
and never changed again) or withdrawn (published, then taken back before its date came). The
version in force on a day is the published one with the latest effective date on or before it
(current()): a newer one takes over from its own date, and the one before is then retired. Every
incident records the version it was scored under (points.py), so old incidents keep their points
unless HR re-scores them. Until a version is published, nothing counts points: the portal ships the
design's sample (SAMPLE) as a draft "not in use", so the setup screens aren't empty.

The body (JSON). Points are numbers of whole, half or quarter points (point_size); lengths of time
("spans") are {"days": n} or {"months": n}, or None for "never".
  name, point_size (1, 0.5 or 0.25), skip_steps (False: steps go in order, though offence minimums,
    repeats, probation floors and escalation can jump)
  tracks          [{key, name}]: separate point totals
  steps           [{key, name, rank, active: span, hr_meeting, hr_only (suspensions and termination
                  reviews: HR only), termination, hr_approval}]
  ladders         {track: [{points, step}]}: the point total that leads to each step
  escalation      [{while: step or "any", to: step or "next"}]: a new incident while a step is active
  expiry          {rule: "writeup" | "rolling" | "first", rolling: span, clean_reset: span or None}
  credits         {on, clean: span, points, max_per_year, tracks: [] (all)}: points back after a
                  clean stretch (off by default)
  offences        [{key, name, track, category, attendance, points, min, max (the manager's range),
                  serious, repeat: {within: span, tiers: [{nth, add, step}]} or None, min_step,
                  hr_approval, writeup_required, attachment_required, wording (the handbook's)}]
  probation       [{key, name, kind: "introductory" | "disciplinary", days, department_days
                  {group: days}, departments [] (all), ladders {track: [{points, step}]},
                  multiplier, overrides {offence: {points, min, max}}, serious_floor: step,
                  extensions {count, days, max_days}, review_lead_days, when_end_passes: "remind" |
                  "pass", on_pass: "keep" | "clear", rehires, transfers}]
  visibility      {employee_sees_total, employee_sees_expired, manager_sees_expired,
                  employee_sees_probation_end}
  approval        {incidents: "all" | "per_offence", writeups: "all" | "per_step"}
  protected_leave [{key, name}]: the answers other than "no" to "Was this protected leave?"

The text in a policy is HR's own (offence names, handbook wording), not anyone's records, so it's
kept readable. It's plain text everywhere: pages show it as text, never as HTML.
"""

import asyncio
import calendar
import copy
import csv
import io
import json
import math
import re
import time
from datetime import date, datetime, timedelta
from fractions import Fraction
from zoneinfo import ZoneInfo

from . import notify

PACIFIC = ZoneInfo("America/Los_Angeles")

SCHEMA = """
-- The point system's versions: drafts HR edits, and published ones, which never change again.
CREATE TABLE IF NOT EXISTS policy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',  -- draft, published or withdrawn
    effective TEXT,                        -- YYYY-MM-DD: in force from this day (published)
    body TEXT NOT NULL,                    -- the point system, JSON
    source TEXT NOT NULL DEFAULT '',       -- sample, import, copy or blank
    based_on INTEGER,
    created_by TEXT,
    created_at REAL NOT NULL,
    updated_by TEXT,
    updated_at REAL NOT NULL,
    published_by TEXT,
    published_at REAL,
    withdrawn_by TEXT,
    withdrawn_at REAL
);
CREATE INDEX IF NOT EXISTS policy_versions_effective ON policy_versions (status, effective);
CREATE TRIGGER IF NOT EXISTS policy_versions_fixed BEFORE UPDATE OF body, name, effective
ON policy_versions WHEN OLD.status != 'draft'
BEGIN SELECT RAISE(ABORT, 'published point systems are never changed'); END;
CREATE TRIGGER IF NOT EXISTS policy_versions_kept BEFORE DELETE ON policy_versions
WHEN OLD.status != 'draft'
BEGIN SELECT RAISE(ABORT, 'published point systems are kept'); END;
"""

NOT_SET_UP = ("The point system isn't set up yet. HR loads it, tests it in the simulator and "
              "publishes it with a date; until then nothing counts points.")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
POINT_SIZES = (1, 0.5, 0.25)
RULES = {"writeup": "On the write-up's end date",
         "rolling": "After a rolling window from the day it happened",
         "first": "Whichever comes first"}
KINDS = {"introductory": "Introductory (new hires)", "disciplinary": "Disciplinary (placed by HR)"}
MAX_BODY = 400 * 1024
MAX = {"tracks": 6, "steps": 12, "offences": 300, "probation": 10, "rungs": 20, "tiers": 6,
       "escalation": 20, "protected_leave": 30, "departments": 200}
MAX_POINTS = 100                       # one incident, one rung, one adjustment
NAME_LEN, WORDING_LEN = 80, 2000

PROTECTED_LEAVE = [
    {"key": "sick", "name": "Oregon sick time"},
    {"key": "plo", "name": "Paid Leave Oregon"},
    {"key": "ofla_fmla", "name": "OFLA/FMLA"},
    {"key": "jury", "name": "Jury duty"},
    {"key": "military", "name": "Military leave"},
    {"key": "dv", "name": "Domestic violence leave"},
    {"key": "injury", "name": "Work injury"},
    {"key": "other", "name": "Other protected leave"},
]


def _offence(key, name, category, points, lo, hi, *, attendance=False, serious=False, repeat=None,
             min_step=None, writeup=False, wording=""):
    return {"key": key, "name": name, "track": "discipline", "category": category,
            "attendance": attendance, "points": points, "min": lo, "max": hi, "serious": serious,
            "repeat": repeat, "min_step": min_step, "hr_approval": True, "writeup_required": writeup,
            "attachment_required": False, "wording": wording}


def _step(key, name, rank, months, *, meeting=False, hr_only=False, termination=False):
    return {"key": key, "name": name, "rank": rank, "active": {"months": months} if months else None,
            "hr_meeting": meeting, "hr_only": hr_only, "termination": termination,
            "hr_approval": True}


# The design's sample (7.11, with the recommended answers to questions 20 to 38): shipped as a
# draft "not in use" until HR loads the owner's own point system.
SAMPLE = {
    "name": "Sample point system (not in use)",
    "point_size": 0.25,
    "skip_steps": False,
    "tracks": [{"key": "discipline", "name": "Discipline"}],
    "steps": [
        _step("coaching", "Coaching", 1, 6),
        _step("verbal", "Verbal warning", 2, 6),
        _step("written", "Written warning", 3, 9),
        _step("final", "Final written warning", 4, 12, meeting=True),
        _step("suspension", "Suspension", 5, 12, meeting=True, hr_only=True),
        _step("termination", "Termination review", 6, None, meeting=True, hr_only=True,
              termination=True),
    ],
    "ladders": {"discipline": [{"points": 3, "step": "coaching"}, {"points": 5, "step": "verbal"},
                               {"points": 7, "step": "written"}, {"points": 9, "step": "final"},
                               {"points": 11, "step": "termination"}]},
    "escalation": [{"while": "final", "to": "termination"},
                   {"while": "suspension", "to": "termination"}],
    "expiry": {"rule": "first", "rolling": {"months": 12}, "clean_reset": None},
    "credits": {"on": False, "clean": {"days": 90}, "points": 1, "max_per_year": 2, "tracks": []},
    "offences": [
        _offence("late_short", "Late 1 to 7 minutes", "Attendance", 0.5, 0.25, 0.5, attendance=True,
                 wording="Be clocked in and ready to work when your shift starts."),
        _offence("late", "Late 8 minutes or more", "Attendance", 1, 0.5, 1.5, attendance=True,
                 repeat={"within": {"days": 90}, "tiers": [{"nth": 3, "add": 1, "step": None}]},
                 wording="Be clocked in and ready to work when your shift starts."),
        _offence("left_early", "Left a shift early without approval", "Attendance", 1, 0.5, 1.5,
                 attendance=True, wording="Stay for your whole shift unless a manager agrees."),
        _offence("late_callout", "Called out less than 2 hours before a shift", "Attendance", 2, 1.5,
                 2.5, attendance=True,
                 wording="If you can't work, call your manager at least 2 hours before your shift."),
        _offence("no_call", "No call, no show", "Attendance", 3, 3, 3, attendance=True,
                 repeat={"within": {"months": 12},
                         "tiers": [{"nth": 2, "add": 0, "step": "termination"}]},
                 writeup=True, wording="Missing a shift without calling is a serious matter."),
        _offence("missed_punch", "Missed clock-in or clock-out", "Attendance", 0.5, 0.25, 0.5,
                 attendance=True, wording="Clock in and out for every shift."),
        _offence("conduct", "Rude to a guest or coworker", "Conduct", 2, 1, 3,
                 wording="Treat guests and coworkers with respect."),
        _offence("safety", "Broke a food safety or safety rule", "Safety", 2, 1, 3,
                 wording="Follow food safety and safety rules at all times."),
        _offence("alcohol", "Served a minor or a visibly intoxicated guest", "Alcohol service", 5,
                 5, 5, serious=True, min_step="final", writeup=True,
                 wording="Check ID and never serve a minor or a visibly intoxicated guest."),
        _offence("theft", "Theft or dishonesty", "Conduct", 6, 6, 6, serious=True,
                 min_step="termination", writeup=True),
        _offence("violence", "Violence or threats", "Conduct", 6, 6, 6, serious=True,
                 min_step="termination", writeup=True),
        _offence("harassment", "Harassment or discrimination", "Conduct", 6, 6, 6, serious=True,
                 min_step="termination", writeup=True),
        _offence("impaired", "Impaired at work (alcohol or drugs)", "Safety", 6, 6, 6,
                 serious=True, min_step="termination", writeup=True),
    ],
    "probation": [
        {"key": "introductory", "name": "Introductory period", "kind": "introductory", "days": 90,
         "department_days": {}, "departments": [],
         "ladders": {"discipline": [{"points": 3, "step": "final"},
                                    {"points": 5, "step": "termination"}]},
         "multiplier": 1, "overrides": {}, "serious_floor": "termination",
         "extensions": {"count": 1, "days": 30, "max_days": 120}, "review_lead_days": 14,
         "when_end_passes": "remind", "on_pass": "keep", "rehires": True, "transfers": False},
        {"key": "disciplinary", "name": "Disciplinary probation", "kind": "disciplinary",
         "days": 90, "department_days": {}, "departments": [],
         "ladders": {"discipline": [{"points": 3, "step": "final"},
                                    {"points": 5, "step": "termination"}]},
         "multiplier": 1, "overrides": {}, "serious_floor": "termination",
         "extensions": {"count": 1, "days": 30, "max_days": 120}, "review_lead_days": 14,
         "when_end_passes": "remind", "on_pass": "keep", "rehires": True, "transfers": True},
    ],
    "visibility": {"employee_sees_total": True, "employee_sees_expired": True,
                   "manager_sees_expired": True, "employee_sees_probation_end": True},
    "approval": {"incidents": "all", "writeups": "all"},
    "protected_leave": PROTECTED_LEAVE,
}

# What a missing part of a body becomes (normalize()).
DEFAULTS = {
    "name": "Point system", "point_size": 0.25, "skip_steps": False, "tracks": [], "steps": [],
    "ladders": {}, "escalation": [], "offences": [], "probation": [],
    "expiry": {"rule": "first", "rolling": {"months": 12}, "clean_reset": None},
    "credits": {"on": False, "clean": {"days": 90}, "points": 1, "max_per_year": 2, "tracks": []},
    "visibility": dict(SAMPLE["visibility"]), "approval": dict(SAMPLE["approval"]),
    "protected_leave": PROTECTED_LEAVE,
}
STEP_DEFAULTS = {"key": "", "name": "", "rank": 0, "active": None, "hr_meeting": False,
                 "hr_only": False, "termination": False, "hr_approval": True}
OFFENCE_DEFAULTS = {"key": "", "name": "", "track": "", "category": "", "attendance": False,
                    "points": 1, "min": None, "max": None, "serious": False, "repeat": None,
                    "min_step": None, "hr_approval": True, "writeup_required": False,
                    "attachment_required": False, "wording": ""}
PROBATION_DEFAULTS = {"key": "", "name": "", "kind": "introductory", "days": 90,
                      "department_days": {}, "departments": [], "ladders": {}, "multiplier": 1,
                      "overrides": {}, "serious_floor": None,
                      "extensions": {"count": 1, "days": 30, "max_days": 120},
                      "review_lead_days": 14, "when_end_passes": "remind", "on_pass": "keep",
                      "rehires": True, "transfers": False}


# ---------------------------------------------------------------- points and time

def quarters(value) -> int:
    """Points as a whole number of quarter points (1.5 -> 6), so sums stay exact. Raises
    ValueError for anything that isn't a number of quarter points."""
    if isinstance(value, bool) or value is None:
        raise ValueError("not a number")
    try:
        exact = Fraction(str(value).strip()) * 4
    except (ValueError, ZeroDivisionError, OverflowError):
        raise ValueError("not a number") from None
    if exact.denominator != 1 or abs(exact) > 4 * 100000:
        raise ValueError("not a number of quarter points")
    return int(exact)


def as_points(q: int) -> float:
    return q / 4


def fmt(q: int) -> str:
    """Quarter points as people write them: 6 -> "1.5", 8 -> "2", 1 -> "0.25"."""
    whole, part = divmod(abs(q), 4)
    text = str(whole) + ("", ".25", ".5", ".75")[part]
    return "-" + text if q < 0 else text


def points_text(q: int) -> str:
    return f"{fmt(q)} point" + ("" if abs(q) == 4 else "s")


def round_to(value: Fraction, size_q: int) -> int:
    """Quarter points rounded to the nearest whole, half or quarter point (halves round up)."""
    steps = value / size_q
    return int((steps + Fraction(1, 2)).__floor__()) * size_q


def add_span(day: date, span) -> date | None:
    """The day a span after `day`: 12 months after Jan 31 is Jan 31; 1 month after Jan 31 is
    Feb 28 (or 29). None (never) stays None."""
    if not span:
        return None
    if "days" in span:
        return day + timedelta(days=int(span["days"]))
    months = day.month - 1 + int(span["months"])
    year, month = day.year + months // 12, months % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def span_text(span) -> str:
    if not span:
        return "never"
    unit, n = ("days", span["days"]) if "days" in span else ("months", span["months"])
    if unit == "months" and n % 12 == 0 and n >= 24:
        unit, n = "years", n // 12
    return f"{n} {unit[:-1] if n == 1 else unit}"


SPAN_RE = re.compile(r"^(\d{1,4})\s*(d|days?|w|weeks?|m|mo|months?|y|yrs?|years?)?$")


def parse_span(text, default_unit: str | None = None):
    """A span from text such as "90 days", "12 months", "1 year" or "6 weeks"; "never" or ""
    is None. Raises ValueError (also for a bare number, unless default_unit says which)."""
    if isinstance(text, dict) or text is None:
        return clean_span(text)
    value = " ".join(str(text).lower().split())
    if value in ("", "never", "none", "no", "-", "does not end", "doesn't end"):
        return None
    m = SPAN_RE.match(value)
    if not m or (not m[2] and not default_unit):
        raise ValueError(text)
    n, unit = int(m[1]), (m[2] or default_unit)[0]
    if unit == "w":
        return clean_span({"days": n * 7})
    if unit == "y":
        return clean_span({"months": n * 12})
    return clean_span({"days": n} if unit == "d" else {"months": n})


def clean_span(value):
    """{"days": n} (1 to 3650) or {"months": n} (1 to 120), or None. Raises ValueError."""
    if value in (None, "", {}):
        return None
    if isinstance(value, str):
        return parse_span(value)
    if not isinstance(value, dict) or len(value) != 1:
        raise ValueError(value)
    unit, n = next(iter(value.items()))
    if unit not in ("days", "months") or isinstance(n, bool) or (
            isinstance(n, float) and not math.isfinite(n)):
        raise ValueError(value)
    n = int(n) if isinstance(n, (int, float)) and n == int(n) else int(str(n))
    if not 1 <= n <= (3650 if unit == "days" else 120):
        raise ValueError(value)
    return {unit: n}


def as_date(value) -> date | None:
    """A date from a date or "YYYY-MM-DD" text, else None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def today(db=None) -> date:
    """Today in HR's time zone (every date in the point system is a calendar day, so clock
    changes never move one)."""
    return datetime.now(notify._zone(db) if db is not None else PACIFIC).date()


# ---------------------------------------------------------------- reading a body

def by_key(items) -> dict:
    return {i.get("key"): i for i in items or [] if isinstance(i, dict)}


def offence(body: dict, key) -> dict | None:
    return by_key(body.get("offences")).get(key)


def step(body: dict, key) -> dict | None:
    return by_key(body.get("steps")).get(key)


def steps(body: dict) -> list[dict]:
    """The steps from the lowest rank to the highest."""
    return sorted((s for s in body.get("steps") or [] if isinstance(s, dict)),
                  key=lambda s: (s.get("rank") if isinstance(s.get("rank"), int) else 0))


def rank(body: dict, key) -> int:
    s = step(body, key)
    return s["rank"] if s else 0


def step_name(body: dict, key) -> str:
    s = step(body, key)
    return s["name"] if s else str(key or "")


def step_end(body: dict, key, start) -> date | None:
    """When a step issued on `start` stops being active (None: it doesn't end by itself)."""
    s = step(body, key)
    return add_span(as_date(start), s.get("active")) if s and as_date(start) else None


def track_name(body: dict, key) -> str:
    t = by_key(body.get("tracks")).get(key)
    return t["name"] if t else str(key or "")


def probation_policy(body: dict, key) -> dict | None:
    return by_key(body.get("probation")).get(key) if key else None


def intro_policy(body: dict, groups=()) -> dict | None:
    """The introductory probation for a new hire in these departments (smart groups): the first
    introductory policy that applies to one of them (or to every department)."""
    groups = {str(g) for g in groups or ()}
    for p in body.get("probation") or []:
        if p.get("kind") == "introductory" and (not p.get("departments")
                                                or groups & {str(g) for g in p["departments"]}):
            return p
    return None


def probation_days(body: dict, key, groups=()) -> int | None:
    """How long a probation lasts for someone in these departments: the longest department
    length that applies, else the policy's own."""
    p = probation_policy(body, key)
    if not p:
        return None
    special = [p["department_days"][g] for g in map(str, groups or ())
               if g in (p.get("department_days") or {})]
    return max(special) if special else p["days"]


def ladder(body: dict, track: str, probation_key=None) -> list[dict]:
    """The ladder that applies: the probation policy's own for this track, if it has one."""
    p = probation_policy(body, probation_key)
    if p and (p.get("ladders") or {}).get(track):
        return p["ladders"][track]
    return (body.get("ladders") or {}).get(track) or []


def needs_hr_approval(body: dict, offence_key) -> bool:
    """Whether an incident waits for HR before it counts (every incident, at launch)."""
    o = offence(body, offence_key)
    return (body.get("approval") or {}).get("incidents", "all") == "all" or not o \
        or bool(o.get("hr_approval", True))


def writeup_needs_hr(body: dict, step_key) -> bool:
    """Whether a write-up at this step waits for HR. Suspensions and termination reviews always do."""
    s = step(body, step_key)
    if not s or s.get("hr_only") or s.get("termination"):
        return True
    return (body.get("approval") or {}).get("writeups", "all") == "all" or bool(s.get("hr_approval", True))


def leave_name(body: dict, key) -> str | None:
    found = by_key(body.get("protected_leave")).get(key)
    return found["name"] if found else None


# ---------------------------------------------------------------- cleaning a body

def _text(value, n: int = NAME_LEN) -> str:
    """One line of plain text: no control characters, spaces collapsed."""
    return " ".join(str(value if value is not None else "").split())[:n]


def _wording(value) -> str:
    """Several lines of plain text."""
    text = str(value if value is not None else "").replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text if c == "\n" or c >= " ")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()[:WORDING_LEN]


def _key(value) -> str:
    return str(value if value is not None else "").strip().lower()[:40]


def _num(value):
    """A number from a number or numeric text; anything else is left for validate() to name."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value      # as it is: float() can't take a huge one
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


def _bool(value):
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in ("1", "yes", "y", "true", "on", "x"):
        return True
    if text in ("0", "no", "n", "false", "off", ""):
        return False
    return value


def _span(value):
    try:
        return clean_span(value)
    except (ValueError, TypeError, OverflowError):
        return value      # validate() names it


def _rungs(value) -> list:
    if not isinstance(value, list):
        return value
    return [{"points": _num(r.get("points")), "step": _key(r.get("step"))} if isinstance(r, dict)
            else r for r in value]


def _fill(defaults: dict, value) -> dict:
    found = copy.deepcopy(defaults)
    if isinstance(value, dict):
        found.update({k: copy.deepcopy(v) for k, v in value.items() if k in defaults})
    return found


def normalize(body) -> dict:
    """A body with every part present and the obvious types fixed (numbers from text, trimmed
    names). What can't be fixed is left as it is, for validate() to name."""
    b = _fill(DEFAULTS, body if isinstance(body, dict) else {})
    b["name"] = _text(b["name"]) or DEFAULTS["name"]
    b["point_size"] = _num(b["point_size"])
    b["skip_steps"] = _bool(b["skip_steps"])
    if isinstance(b["tracks"], list):
        b["tracks"] = [{"key": _key(t.get("key")), "name": _text(t.get("name"))}
                       if isinstance(t, dict) else t for t in b["tracks"]]
    if isinstance(b["steps"], list):
        b["steps"] = [_clean_step(s) if isinstance(s, dict) else s for s in b["steps"]]
    if isinstance(b["ladders"], dict):
        b["ladders"] = {_key(k): _rungs(v) for k, v in b["ladders"].items()}
    if isinstance(b["escalation"], list):
        b["escalation"] = [{"while": _key(e.get("while")), "to": _key(e.get("to"))}
                           if isinstance(e, dict) else e for e in b["escalation"]]
    b["expiry"] = _fill(DEFAULTS["expiry"], b["expiry"])
    b["expiry"]["rule"] = _key(b["expiry"]["rule"])
    for k in ("rolling", "clean_reset"):
        b["expiry"][k] = _span(b["expiry"][k])
    c = b["credits"] = _fill(DEFAULTS["credits"], b["credits"])
    c["on"], c["clean"], c["points"] = _bool(c["on"]), _span(c["clean"]), _num(c["points"])
    c["max_per_year"] = _num(c["max_per_year"])
    if isinstance(c["tracks"], list):
        c["tracks"] = [_key(t) for t in c["tracks"]]
    if isinstance(b["offences"], list):
        b["offences"] = [_clean_offence(o) if isinstance(o, dict) else o for o in b["offences"]]
    if isinstance(b["probation"], list):
        b["probation"] = [_clean_probation(p) if isinstance(p, dict) else p for p in b["probation"]]
    b["visibility"] = {k: _bool(v) for k, v in _fill(DEFAULTS["visibility"], b["visibility"]).items()}
    b["approval"] = {k: _key(v) for k, v in _fill(DEFAULTS["approval"], b["approval"]).items()}
    if isinstance(b["protected_leave"], list):
        b["protected_leave"] = [{"key": _key(p.get("key")), "name": _text(p.get("name"))}
                                if isinstance(p, dict) else p for p in b["protected_leave"]]
    return b


def _clean_step(s: dict) -> dict:
    s = _fill(STEP_DEFAULTS, s)
    s.update(key=_key(s["key"]), name=_text(s["name"]), rank=_num(s["rank"]), active=_span(s["active"]))
    for k in ("hr_meeting", "hr_only", "termination", "hr_approval"):
        s[k] = _bool(s[k])
    return s


def _clean_offence(o: dict) -> dict:
    o = _fill(OFFENCE_DEFAULTS, o)
    o.update(key=_key(o["key"]), name=_text(o["name"]), track=_key(o["track"]),
             category=_text(o["category"], 40), points=_num(o["points"]),
             min_step=_key(o["min_step"]) or None, wording=_wording(o["wording"]))
    o["min"] = o["points"] if o["min"] in (None, "") else _num(o["min"])
    o["max"] = o["points"] if o["max"] in (None, "") else _num(o["max"])
    for k in ("attendance", "serious", "hr_approval", "writeup_required", "attachment_required"):
        o[k] = _bool(o[k])
    r = o["repeat"]
    if isinstance(r, dict):
        tiers = r.get("tiers")
        o["repeat"] = {"within": _span(r.get("within")), "tiers": [
            {"nth": _num(t.get("nth")), "add": _num(t.get("add") if t.get("add") not in (None, "")
                                                    else 0),
             "step": _key(t.get("step")) or None} if isinstance(t, dict) else t
            for t in tiers] if isinstance(tiers, list) else tiers}
    elif r in ("", {}, []):
        o["repeat"] = None
    return o


def _clean_probation(p: dict) -> dict:
    p = _fill(PROBATION_DEFAULTS, p)
    p.update(key=_key(p["key"]), name=_text(p["name"]), kind=_key(p["kind"]), days=_num(p["days"]),
             multiplier=_num(p["multiplier"]), serious_floor=_key(p["serious_floor"]) or None,
             review_lead_days=_num(p["review_lead_days"]),
             when_end_passes=_key(p["when_end_passes"]), on_pass=_key(p["on_pass"]),
             rehires=_bool(p["rehires"]), transfers=_bool(p["transfers"]))
    if isinstance(p["department_days"], dict):
        p["department_days"] = {str(g): _num(d) for g, d in p["department_days"].items()}
    if isinstance(p["departments"], list):
        p["departments"] = [str(g) for g in p["departments"]]
    if isinstance(p["ladders"], dict):
        p["ladders"] = {_key(k): _rungs(v) for k, v in p["ladders"].items() if v}
    if isinstance(p["overrides"], dict):
        p["overrides"] = {_key(k): {"points": _num(v.get("points")),
                                    "min": _num(v.get("min")) if v.get("min") not in (None, "") else None,
                                    "max": _num(v.get("max")) if v.get("max") not in (None, "") else None}
                          if isinstance(v, dict) else v for k, v in p["overrides"].items()}
    e = p["extensions"] = _fill(PROBATION_DEFAULTS["extensions"], p["extensions"])
    for k in e:
        e[k] = _num(e[k])
    return p


# ---------------------------------------------------------------- checking a body

def validate(body) -> list[str]:
    """What's wrong with a point system, in plain English (nothing: it can be published)."""
    try:
        return _problems(normalize(body), raw=body)
    except Exception as err:  # noqa: BLE001 - a damaged body is a problem, not a crash
        return [f"This point system couldn't be read ({type(err).__name__})."]


def _span_ok(v) -> bool:
    """A real span ({"days": n} or {"months": n} in range); None isn't one."""
    try:
        return v is not None and clean_span(v) == v
    except (ValueError, TypeError, OverflowError):
        return False


def _is_int(v, lo, hi) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _points_ok(v, size_q: int, lo: int = 0, hi: int = MAX_POINTS) -> bool:
    try:
        q = quarters(v)
    except ValueError:
        return False
    return q % size_q == 0 and lo * 4 <= q <= hi * 4


def _problems(b: dict, raw=None) -> list[str]:
    found: list[str] = []
    add = found.append
    if not isinstance(raw, dict):
        return ["This point system couldn't be read."]
    if len(json.dumps(raw, default=str)) > MAX_BODY:
        add("This point system is too big to save.")
    size_ok = b["point_size"] in POINT_SIZES
    if not size_ok:
        add("Points must be whole, half or quarter points.")
    size_q = quarters(b["point_size"]) if size_ok else 1
    unit = {4: "whole", 2: "half", 1: "quarter"}[size_q]
    if not isinstance(b["skip_steps"], bool):
        add("Say whether steps can be skipped.")
    for part in ("tracks", "steps", "offences", "probation", "escalation", "protected_leave"):
        if not isinstance(b[part], list) or not all(isinstance(i, dict) for i in b[part]):
            add(f"The {part.replace('_', ' ')} couldn't be read.")
            return found
        if len(b[part]) > MAX[part]:
            add(f"There can be at most {MAX[part]} {part.replace('_', ' ')}.")

    def keys(items, what) -> set:
        seen = set()
        for i in items:
            label = i.get("name") or i.get("key") or "(no name)"
            if not KEY_RE.match(i.get("key") or ""):
                add(f'{what} "{label}" needs a short key: lowercase letters, numbers and _ '
                    '(like late_8).')
            elif i["key"] in seen:
                add(f'Two {what.lower()}s share the key "{i["key"]}".')
            seen.add(i.get("key"))
            if not i.get("name"):
                add(f'{what} "{i.get("key")}" needs a name.')
        return seen

    tracks = keys(b["tracks"], "Point total")
    if not b["tracks"]:
        add("Add at least one point total (most places have one, called Discipline).")
    step_keys = keys(b["steps"], "Step")
    if not b["steps"]:
        add("Add at least one step, such as Coaching or Written warning.")
    ranks = [s["rank"] for s in b["steps"]]
    if not all(_is_int(r, 1, 99) for r in ranks):
        add("Each step needs a rank from 1 to 99 (1 is the lowest).")
    elif len(set(ranks)) != len(ranks):
        add("Two steps share a rank: give each step its own place in the order.")
    for s in b["steps"]:
        label = s.get("name") or s.get("key")
        if s["active"] is not None and not _span_ok(s["active"]):
            add(f'Step "{label}": how long it stays active must be a number of days or months, '
                'or never.')
        for k in ("hr_meeting", "hr_only", "termination", "hr_approval"):
            if not isinstance(s[k], bool):
                add(f'Step "{label}": a yes or no is needed for every switch.')
                break
    if sum(1 for s in b["steps"] if s["termination"] is True) > 1:
        add("Only one step can be the termination review.")
    rank_of = {s["key"]: s["rank"] for s in b["steps"] if _is_int(s["rank"], 1, 99)}

    def check_ladder(rungs, label):
        if not isinstance(rungs, list) or not all(isinstance(r, dict) for r in rungs):
            add(f"{label} couldn't be read.")
            return
        if len(rungs) > MAX["rungs"]:
            add(f"{label} has too many steps.")
        last_q, last_rank = 0, 0
        for r in rungs:
            if not _points_ok(r["points"], size_q, 0, 1000) or quarters(r["points"]) <= 0:
                add(f"{label}: each threshold must be more than 0, in {unit} points.")
                return
            if r["step"] not in step_keys:
                add(f'{label}: "{r["step"] or "(none)"}" isn\'t one of the steps.')
                return
            q = quarters(r["points"])
            if q <= last_q:
                add(f"{label}: thresholds must go up, from the fewest points to the most.")
                return
            if rank_of.get(r["step"], 0) <= last_rank:
                add(f"{label}: higher thresholds must lead to higher steps.")
                return
            last_q, last_rank = q, rank_of.get(r["step"], 0)

    if not isinstance(b["ladders"], dict):
        add("The ladders couldn't be read.")
    else:
        for t, rungs in b["ladders"].items():
            if t not in tracks:
                add(f'There\'s a ladder for "{t}", which isn\'t one of the point totals.')
            else:
                check_ladder(rungs, f"The {_track_label(b, t)} ladder")
        for t in tracks:
            if t and not b["ladders"].get(t):
                add(f"The {_track_label(b, t)} ladder has no steps: say how many points lead to each step.")
    for e in b["escalation"]:
        if e["while"] != "any" and e["while"] not in step_keys:
            add(f'Escalation: "{e["while"] or "(none)"}" isn\'t one of the steps.')
        if e["to"] != "next" and e["to"] not in step_keys:
            add(f'Escalation: "{e["to"] or "(none)"}" isn\'t one of the steps.')
        elif e["to"] != "next" and e["while"] in rank_of and \
                rank_of.get(e["to"], 0) <= rank_of[e["while"]]:
            add(f"Escalation from {step_name(b, e['while'])} must go to a higher step.")
    x = b["expiry"]
    if x["rule"] not in RULES:
        add("Choose when points stop counting.")
    if x["rule"] in ("rolling", "first") and not _span_ok(x["rolling"]):
        add("Say how long points count for, such as 12 months.")
    if x["clean_reset"] is not None and not _span_ok(x["clean_reset"]):
        add("The clean-period reset must be a number of days or months, or off.")
    c = b["credits"]
    if not isinstance(c["on"], bool):
        add("Say whether points can be earned back.")
    elif c["on"]:
        if not _span_ok(c["clean"]):
            add("Credits: say how long a clean stretch must be, such as 90 days.")
        if not _points_ok(c["points"], size_q, 0, MAX_POINTS) or quarters(c["points"]) <= 0:
            add(f"Credits: the points earned back must be more than 0, in {unit} points.")
        if not _is_int(c["max_per_year"], 1, 50):
            add("Credits: the most a year must be a whole number from 1 to 50.")
        if not isinstance(c["tracks"], list) or not set(c["tracks"]) <= tracks:
            add("Credits: choose point totals from the list.")
    offence_keys = keys(b["offences"], "Offence")
    for o in b["offences"]:
        label = o.get("name") or o.get("key")
        if o["track"] not in tracks:
            add(f'Offence "{label}": choose which point total it counts in.')
        if not _points_ok(o["points"], size_q):
            add(f'Offence "{label}": its points must be from 0 to {MAX_POINTS}, in {unit} points.')
            continue
        if not (_points_ok(o["min"], size_q) and _points_ok(o["max"], size_q)):
            add(f"Offence \"{label}\": the manager's range must be in {unit} points.")
        elif not quarters(o["min"]) <= quarters(o["points"]) <= quarters(o["max"]):
            add(f"Offence \"{label}\": the manager's range must include its points "
                f"({fmt(quarters(o['points']))}).")
        for k in ("attendance", "serious", "hr_approval", "writeup_required", "attachment_required"):
            if not isinstance(o[k], bool):
                add(f'Offence "{label}": a yes or no is needed for every switch.')
                break
        if o["min_step"] and o["min_step"] not in step_keys:
            add(f'Offence "{label}": its minimum step isn\'t one of the steps.')
        r = o["repeat"]
        if r is not None:
            if not isinstance(r, dict) or not _span_ok(r.get("within")):
                add(f'Offence "{label}": its repeat rule needs a period, such as 90 days.')
            elif not isinstance(r.get("tiers"), list) or not r["tiers"] or \
                    not all(isinstance(t, dict) for t in r["tiers"]) or len(r["tiers"]) > MAX["tiers"]:
                add(f'Offence "{label}": its repeat rule needs at least one repeat, such as the 2nd.')
            else:
                last = 1
                for t in r["tiers"]:
                    if not _is_int(t["nth"], 2, 50) or t["nth"] <= last:
                        add(f'Offence "{label}": repeats count up from the 2nd, each one once.')
                        break
                    last = t["nth"]
                    if not _points_ok(t["add"], size_q):
                        add(f'Offence "{label}": extra points for a repeat must be in {unit} points.')
                        break
                    if t["step"] and t["step"] not in step_keys:
                        add(f'Offence "{label}": a repeat\'s step isn\'t one of the steps.')
                        break
                    if not t["step"] and quarters(t["add"]) == 0:
                        add(f'Offence "{label}": a repeat must add points or lead to a step.')
                        break
    for p in b["probation"]:
        label = p.get("name") or p.get("key")
        if p["kind"] not in KINDS:
            add(f'Probation "{label}": choose introductory or disciplinary.')
        if not _is_int(p["days"], 1, 730):
            add(f'Probation "{label}": its length must be from 1 to 730 days.')
        dd = p["department_days"]
        if not isinstance(dd, dict) or len(dd) > MAX["departments"] or \
                not all(_is_int(v, 1, 730) for v in dd.values()):
            add(f'Probation "{label}": department lengths must be from 1 to 730 days.')
        if not isinstance(p["departments"], list) or len(p["departments"]) > MAX["departments"]:
            add(f'Probation "{label}": choose departments from the list.')
        if not isinstance(p["ladders"], dict):
            add(f'Probation "{label}": its ladder couldn\'t be read.')
        else:
            for t, rungs in p["ladders"].items():
                if t not in tracks:
                    add(f'Probation "{label}" has a ladder for "{t}", which isn\'t one of the point totals.')
                else:
                    check_ladder(rungs, f'The "{label}" {_track_label(b, t)} ladder')
        m = p["multiplier"]
        if isinstance(m, bool) or not isinstance(m, (int, float)) or not 0.25 <= m <= 5 \
                or Fraction(str(m)).denominator > 100:
            add(f'Probation "{label}": the points multiplier must be from 0.25 to 5, like 1.5.')
        if not isinstance(p["overrides"], dict):
            add(f'Probation "{label}": its offence points couldn\'t be read.')
        else:
            for k, v in p["overrides"].items():
                if k not in offence_keys:
                    add(f'Probation "{label}": "{k}" isn\'t one of the offences.')
                elif not isinstance(v, dict) or not _points_ok(v.get("points"), size_q):
                    add(f'Probation "{label}": points for {_offence_label(b, k)} must be in {unit} points.')
                else:
                    lo = v["min"] if v.get("min") is not None else v["points"]
                    hi = v["max"] if v.get("max") is not None else v["points"]
                    if not (_points_ok(lo, size_q) and _points_ok(hi, size_q)) or \
                            not quarters(lo) <= quarters(v["points"]) <= quarters(hi):
                        add(f"Probation \"{label}\": the manager's range for {_offence_label(b, k)} "
                            "must include its points.")
        if p["serious_floor"] and p["serious_floor"] not in step_keys:
            add(f'Probation "{label}": the step for serious offences isn\'t one of the steps.')
        e = p["extensions"]
        if not (_is_int(e["count"], 0, 5) and _is_int(e["days"], 1, 365)
                and _is_int(e["max_days"], 1, 1095)):
            add(f'Probation "{label}": extensions need a count (0 to 5), days and a most in total.')
        elif _is_int(p["days"], 1, 730) and e["max_days"] < p["days"]:
            add(f'Probation "{label}": the most in total can\'t be shorter than the probation itself.')
        if not _is_int(p["review_lead_days"], 0, 90):
            add(f'Probation "{label}": the review is due 0 to 90 days before the end.')
        if p["when_end_passes"] not in ("remind", "pass"):
            add(f'Probation "{label}": choose what happens when the end passes with no decision.')
        if p["on_pass"] not in ("keep", "clear"):
            add(f'Probation "{label}": choose whether points are kept or cleared on passing.')
        if not (isinstance(p["rehires"], bool) and isinstance(p["transfers"], bool)):
            add(f'Probation "{label}": say whether it applies to rehires and transfers.')
    keys(b["probation"], "Probation")
    for k, v in b["visibility"].items():
        if not isinstance(v, bool):
            add("A yes or no is needed for every visibility switch.")
            break
    if b["approval"].get("incidents") not in ("all", "per_offence") or \
            b["approval"].get("writeups") not in ("all", "per_step"):
        add("Choose what HR approves.")
    keys(b["protected_leave"], "Protected leave")
    if any(p.get("key") == "no" for p in b["protected_leave"]):
        add('Protected leave can\'t use the key "no": it means the absence wasn\'t protected.')
    return list(dict.fromkeys(found))


def _track_label(b: dict, key) -> str:
    return track_name(b, key)


def _offence_label(b: dict, key) -> str:
    o = offence(b, key)
    return o["name"] if o else str(key)


def key_for(name: str, taken=()) -> str:
    """A key from a name ("Late 8+ minutes" -> "late_8_minutes"), not one of taken."""
    base = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")[:24] or "item"
    if not base[0].isalpha():
        base = "x_" + base[:22]
    key, n = base, 2
    while key in taken:
        key, n = f"{base}_{n}", n + 1
    return key


# ---------------------------------------------------------------- versions

def setup(db) -> None:
    """The table, and the sample as a draft the first time HR starts (on start)."""
    db.add_schema(SCHEMA)
    if not db.conn.execute("SELECT 1 FROM policy_versions LIMIT 1").fetchone():
        save_draft(db, SAMPLE, None, source="sample")


def _row(r) -> dict | None:
    if r is None:
        return None
    found = dict(r)
    found["body"] = json.loads(found["body"])
    return found


def get(db, version_id) -> dict | None:
    try:
        vid = int(version_id)
    except (TypeError, ValueError):
        return None
    return _row(db.conn.execute("SELECT * FROM policy_versions WHERE id = ?", (vid,)).fetchone())


def save_draft(db, body: dict, actor_id, version_id=None, *, source: str = "blank",
               based_on=None) -> int:
    """Save a draft (a new one, or version_id's, which must still be a draft). Problems are
    allowed in a draft: validate() names them, and publish() refuses until they're fixed.
    Raises ValueError."""
    clean = normalize(body)
    try:   # Infinity or NaN would be saved but never open again in a browser
        text = json.dumps(clean, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except ValueError:
        raise ValueError("A number in it isn't a real number.") from None
    if len(text) > MAX_BODY:
        raise ValueError("This point system is too big to save.")
    now = time.time()
    if version_id is None:
        cur = db.conn.execute(
            "INSERT INTO policy_versions (name, body, source, based_on, created_by, created_at, "
            "updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (clean["name"], text, source, based_on, actor_id, now, actor_id, now))
        db.conn.commit()
        return cur.lastrowid
    old = get(db, version_id)
    if not old:
        raise ValueError("That version isn't here.")
    if old["status"] != "draft":
        raise ValueError("A published version can't be changed. Make a new draft from it.")
    db.conn.execute("UPDATE policy_versions SET name = ?, body = ?, updated_by = ?, updated_at = ? "
                    "WHERE id = ? AND status = 'draft'", (clean["name"], text, actor_id, now, old["id"]))
    db.conn.commit()
    return old["id"]


def delete_draft(db, version_id) -> None:
    old = get(db, version_id)
    if not old or old["status"] != "draft":
        raise ValueError("Only a draft can be deleted.")
    db.conn.execute("DELETE FROM policy_versions WHERE id = ? AND status = 'draft'", (old["id"],))
    db.conn.commit()


def _published(db) -> list[dict]:
    return [dict(r) for r in db.conn.execute(
        "SELECT id, name, effective, published_at FROM policy_versions WHERE status = 'published' "
        "ORDER BY effective, published_at, id")]


def publish(db, version_id, effective, actor_id, *, on: date | None = None) -> None:
    """Put a draft in force from `effective`. It must have no problems; its date must be today or
    later (the first version may start earlier, to load past incidents) and after every other
    published version's. Raises ValueError."""
    old = get(db, version_id)
    eff = as_date(effective)
    on = on or today(db)
    if not old or old["status"] != "draft":
        raise ValueError("Only a draft can be published.")
    if eff is None:
        raise ValueError("Choose the day it starts.")
    problems = validate(old["body"])
    if problems:
        raise ValueError("Fix these first: " + " ".join(problems[:3]))
    others = _published(db)
    if others and eff < on:
        raise ValueError("It can't start before today: earlier incidents keep the version they "
                         "were scored under.")
    if others and eff <= as_date(others[-1]["effective"]):
        raise ValueError(f"It must start after {others[-1]['name']}, which starts "
                         f"{as_date(others[-1]['effective']).isoformat()}.")
    if eff < on - timedelta(days=3 * 366) or eff > on + timedelta(days=366):
        raise ValueError("Choose a day within a year from today.")
    db.conn.execute("UPDATE policy_versions SET status = 'published', effective = ?, "
                    "published_by = ?, published_at = ? WHERE id = ? AND status = 'draft'",
                    (eff.isoformat(), actor_id, time.time(), old["id"]))
    db.conn.commit()


def withdraw(db, version_id, actor_id, *, on: date | None = None) -> None:
    """Take back a published version that hasn't started yet. Raises ValueError."""
    old = get(db, version_id)
    on = on or today(db)
    if not old or old["status"] != "published":
        raise ValueError("Only a published version can be withdrawn.")
    if as_date(old["effective"]) <= on:
        raise ValueError("It's already in force, so it stays: publish a newer version instead.")
    db.conn.execute("UPDATE policy_versions SET status = 'withdrawn', withdrawn_by = ?, "
                    "withdrawn_at = ? WHERE id = ?", (actor_id, time.time(), old["id"]))
    db.conn.commit()


def current(db, on=None) -> dict | None:
    """The published version in force on that day (today by default), or None."""
    day = as_date(on) or today(db)
    return _row(db.conn.execute(
        "SELECT * FROM policy_versions WHERE status = 'published' AND effective <= ? "
        "ORDER BY effective DESC, published_at DESC, id DESC LIMIT 1", (day.isoformat(),)).fetchone())


def set_up(db, on=None) -> bool:
    """Whether a point system is in force (managers can't record incidents until it is)."""
    return current(db, on) is not None


def versions(db, on=None) -> list[dict]:
    """Every version, newest first, without bodies: {id, name, status (draft, published or
    retired), state (draft, scheduled, in_use, retired or withdrawn), label, effective, until,
    created_at, updated_at, published_at, source, based_on}."""
    day = as_date(on) or today(db)
    published = _published(db)
    now = current(db, day)
    found = []
    for r in db.conn.execute("SELECT id, name, status, effective, source, based_on, created_at, "
                             "updated_at, published_at, published_by FROM policy_versions "
                             "ORDER BY id DESC"):
        v = dict(r)
        later = [p for p in published if p["effective"] > (v["effective"] or "")]
        v["until"] = later[0]["effective"] if v["status"] == "published" and later else None
        if v["status"] == "draft":
            v["state"], v["label"] = "draft", "Draft, not in use"
        elif v["status"] == "withdrawn":
            v["state"], v["label"], v["status"] = "withdrawn", "Withdrawn before it started", "retired"
        elif now and now["id"] == v["id"]:
            v["state"], v["label"] = "in_use", f"In use since {_day(v['effective'])}"
        elif v["effective"] > day.isoformat():
            v["state"], v["label"] = "scheduled", f"Starts {_day(v['effective'])}"
        else:
            v["state"], v["status"] = "retired", "retired"
            v["label"] = f"Retired {_day(v['until'])}" if v["until"] else "Retired"
        found.append(v)
    return found


def _day(iso) -> str:
    d = as_date(iso)
    return f"{calendar.month_abbr[d.month]} {d.day}, {d.year}" if d else ""


# ---------------------------------------------------------------- CSV: import and export

YES_NO = {True: "yes", False: "no"}
RULE_WORDS = {"writeup": "On the write-up's end date", "rolling": "After the rolling window",
              "first": "Whichever comes first"}
SETTINGS_ROWS = [  # (label, where in the body)
    ("Point system", "name"), ("Point size", "point_size"), ("Steps go in order", "skip_steps"),
    ("Points stop counting", "expiry.rule"), ("Rolling window", "expiry.rolling"),
    ("Clean reset after", "expiry.clean_reset"), ("Credits", "credits.on"),
    ("Credit after a clean stretch of", "credits.clean"), ("Credit points", "credits.points"),
    ("Most credits a year", "credits.max_per_year"), ("Credit point totals", "credits.tracks"),
    ("Employees see their total", "visibility.employee_sees_total"),
    ("Employees see expired points", "visibility.employee_sees_expired"),
    ("Managers see expired points", "visibility.manager_sees_expired"),
    ("Employees see their probation end", "visibility.employee_sees_probation_end"),
    ("HR approves incidents", "approval.incidents"), ("HR approves write-ups", "approval.writeups"),
]
TABLES = {  # section: its columns, in order
    "Point totals": ["Key", "Name"],
    "Steps": ["Key", "Name", "Rank", "Active for", "HR meeting", "HR only", "Termination review",
              "HR approves"],
    "Ladder": ["Point total", "Points", "Step"],
    "Escalation": ["While on", "Goes to"],
    "Offences": ["Key", "Name", "Point total", "Category", "Attendance", "Points", "Manager min",
                 "Manager max", "Serious", "Minimum step", "HR approves", "Write-up required",
                 "Attachment required", "Handbook wording"],
    "Repeats": ["Offence", "Within", "Occurrence", "Extra points", "Step"],
    "Probation": ["Key", "Name", "Kind", "Days", "Multiplier", "Serious offences go to",
                  "Extensions", "Extension days", "Most days in total", "Review due days before end",
                  "When the end passes", "On passing", "Rehires", "Transfers", "Departments"],
    "Probation days by department": ["Probation", "Department", "Days"],
    "Probation ladder": ["Probation", "Point total", "Points", "Step"],
    "Probation offences": ["Probation", "Offence", "Points", "Manager min", "Manager max"],
    "Protected leave": ["Key", "Name"],
}
SECTION_NAMES = {s.lower(): s for s in [*TABLES, "Settings"]}
SECTION_NAMES.update({"tracks": "Point totals", "ladders": "Ladder", "offenses": "Offences",
                      "repeat rules": "Repeats", "probation ladders": "Probation ladder"})


def _num_text(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return "" if v is None else str(v)


def _setting_value(b: dict, where: str) -> str:
    if where == "name":
        return b["name"]
    if where == "point_size":
        return _num_text(b["point_size"])
    if where == "skip_steps":
        return YES_NO[not b["skip_steps"]]
    part, key = where.split(".")
    v = b[part][key]
    if where == "expiry.rule":
        return RULE_WORDS.get(v, v)
    if where in ("approval.incidents", "approval.writeups"):
        return {"all": "all", "per_offence": "per offence", "per_step": "per step"}.get(v, v)
    if isinstance(v, bool):
        return YES_NO[v]
    if isinstance(v, list):
        return "; ".join(v)
    if key in ("rolling", "clean_reset", "clean"):
        return span_text(v) if v else "never"
    return _num_text(v)


FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _safe(cell):
    """Text a spreadsheet would run as a formula (=, +, -, @) gets a leading ' so it stays text;
    import_csv takes it off again. Numbers are left alone."""
    if isinstance(cell, str) and cell.startswith(FORMULA) and not isinstance(_num(cell), (int, float)):
        return "'" + cell
    return cell


class _Writer:
    def __init__(self, out):
        self.w = csv.writer(out, lineterminator="\r\n")

    def writerow(self, row):
        self.w.writerow([_safe(c) for c in row])


def export_csv(body: dict) -> str:
    """The point system as the import spreadsheet (import_csv reads it back the same)."""
    b = normalize(body)
    out = io.StringIO()
    w = _Writer(out)
    w.writerow(["Settings"])
    w.writerow(["Setting", "Value"])
    for label, where in SETTINGS_ROWS:
        w.writerow([label, _setting_value(b, where)])
    yn = YES_NO.get

    def table(name, rows):
        w.writerow([])
        w.writerow([name])
        w.writerow(TABLES[name])
        for r in rows:
            w.writerow(["" if c is None else (yn(c) if isinstance(c, bool) else _num_text(c)
                                              if isinstance(c, (int, float)) else c) for c in r])

    table("Point totals", [[t["key"], t["name"]] for t in b["tracks"]])
    table("Steps", [[s["key"], s["name"], s["rank"], span_text(s["active"]), s["hr_meeting"],
                     s["hr_only"], s["termination"], s["hr_approval"]] for s in b["steps"]])
    table("Ladder", [[t, r["points"], r["step"]] for t, rungs in b["ladders"].items()
                     for r in rungs or []])
    table("Escalation", [[e["while"], e["to"]] for e in b["escalation"]])
    table("Offences", [[o["key"], o["name"], o["track"], o["category"], o["attendance"], o["points"],
                        o["min"], o["max"], o["serious"], o["min_step"] or "", o["hr_approval"],
                        o["writeup_required"], o["attachment_required"], o["wording"]]
                       for o in b["offences"]])
    table("Repeats", [[o["key"], span_text(o["repeat"]["within"]), t["nth"], t["add"], t["step"] or ""]
                      for o in b["offences"] if o["repeat"] for t in o["repeat"]["tiers"]])
    table("Probation", [[p["key"], p["name"], p["kind"], p["days"], p["multiplier"],
                         p["serious_floor"] or "", p["extensions"]["count"], p["extensions"]["days"],
                         p["extensions"]["max_days"], p["review_lead_days"], p["when_end_passes"],
                         p["on_pass"], p["rehires"], p["transfers"], "; ".join(p["departments"])]
                        for p in b["probation"]])
    table("Probation days by department", [[p["key"], g, d] for p in b["probation"]
                                           for g, d in p["department_days"].items()])
    table("Probation ladder", [[p["key"], t, r["points"], r["step"]] for p in b["probation"]
                               for t, rungs in p["ladders"].items() for r in rungs or []])
    table("Probation offences", [[p["key"], k, v["points"], v.get("min"), v.get("max")]
                                 for p in b["probation"] for k, v in p["overrides"].items()])
    table("Protected leave", [[p["key"], p["name"]] for p in b["protected_leave"]])
    return out.getvalue()


MAX_CSV = 512 * 1024


def _rows(text: str) -> list[list[str]]:
    text = text.lstrip("﻿")
    first = text.split("\n", 5)
    sample = "\n".join(first[:5])
    delimiter = "\t" if sample.count("\t") > sample.count(",") else \
        ";" if sample.count(";") > sample.count(",") else ","
    return [[_unsafe(c.strip()) for c in row] for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _unsafe(cell: str) -> str:
    return cell[1:] if cell[:1] == "'" and cell[1:2] in ("=", "+", "-", "@") else cell


def _blocks(rows) -> tuple[dict, list[str]]:
    """{section: [header, row, ...]} from rows: a row with only a section's name starts it, and
    the next row is its header."""
    blocks, questions, current = {}, [], None
    for n, row in enumerate(rows[:5000], 1):
        cells = row[:]
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue
        name = SECTION_NAMES.get(cells[0].lower().rstrip(":")) if len(cells) == 1 else None
        if name:
            if name in blocks:
                questions.append(f'"{name}" appears twice: only the first was used.')
                current = None
            else:
                current = blocks[name] = []
            continue
        if current is None:
            questions.append(f"Line {n} isn't under a section (such as Offences), so it was left out.")
            continue
        current.append(cells)
    return blocks, list(dict.fromkeys(questions))


def _table(blocks, name, questions) -> list[dict]:
    """A section's rows as {column: text}, matching its columns by name."""
    rows = blocks.get(name)
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    wanted = {c.lower(): c for c in TABLES.get(name, ["Setting", "Value"])}
    if name == "Settings":
        wanted = {"setting": "Setting", "value": "Value"}
    for h in header:
        if h and h not in wanted:
            questions.append(f'{name}: the column "{h}" wasn\'t used.')
    found = []
    for cells in rows[1:]:
        row = {wanted[h]: (cells[i] if i < len(cells) else "") for i, h in enumerate(header)
               if h in wanted}
        if any(row.values()):
            found.append(row)
    return found


class _Reader:
    """Reads one import, noting what it had to guess as questions for HR."""

    def __init__(self):
        self.questions: list[str] = []

    def ask(self, text: str) -> None:
        if text not in self.questions:
            self.questions.append(text)

    def yes(self, value: str, where: str, default: bool) -> bool:
        v = _bool(value)
        if isinstance(v, bool):
            return v if value.strip() else default
        self.ask(f'{where}: "{value}" isn\'t yes or no, so {YES_NO[default]} was used.')
        return default

    def number(self, value: str, where: str, default=None):
        if not value.strip():
            return default
        v = _num(value.replace(",", "."))
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
        self.ask(f'{where}: "{value}" isn\'t a number' + (
            f", so {_num_text(default)} was used." if default is not None else ", so it was left empty."))
        return default

    def span(self, value: str, where: str, default, unit: str | None = None):
        try:
            return parse_span(value, None)
        except ValueError:
            pass
        try:
            found = parse_span(value, unit or "days")
        except ValueError:
            self.ask(f'{where}: "{value}" isn\'t a length of time like "90 days" or "12 months", so '
                     f"{span_text(default)} was used.")
            return default
        self.ask(f'{where}: "{value}" has no unit, so it was read as {span_text(found)}. '
                 "Is that right?")
        return found

    def key(self, value: str, name: str, taken: set, where: str) -> str:
        k = _key(value)
        if KEY_RE.match(k) and k not in taken:
            return k
        new = key_for(name or value, taken)
        if value:
            self.ask(f'{where}: the key "{value}" was changed to "{new}".')
        return new


def import_csv(text: str) -> tuple[dict, list[str]]:
    """A point system from the import spreadsheet (CSV, or tab-separated text pasted from a
    spreadsheet), with plain-English questions for anything it had to guess. It never refuses
    over what the file says: the result is loaded as a draft, and validate() names what's left.
    Raises ValueError only when the text can't be read at all."""
    if len(text) > MAX_CSV:
        raise ValueError("That file is too big: a point system is well under 500 KB.")
    try:
        rows = _rows(text)
    except csv.Error:
        raise ValueError("That file couldn't be read as a spreadsheet (CSV).") from None
    blocks, questions = _blocks(rows)
    if not blocks:
        raise ValueError("That doesn't look like the point system spreadsheet: it has no sections "
                         "such as Offences or Steps. Export one to see the layout.")
    r = _Reader()
    r.questions = questions
    b = copy.deepcopy(DEFAULTS)
    b["protected_leave"] = copy.deepcopy(PROTECTED_LEAVE)
    _read_settings(r, _table(blocks, "Settings", r.questions), b)

    taken: set = set()
    for row in _table(blocks, "Point totals", r.questions):
        name = _text(row.get("Name") or row.get("Key"))
        k = r.key(row.get("Key", ""), name, taken, "Point totals")
        taken.add(k)
        b["tracks"].append({"key": k, "name": name or k})

    taken = set()
    for i, row in enumerate(_table(blocks, "Steps", r.questions), 1):
        name = _text(row.get("Name") or row.get("Key"))
        k = r.key(row.get("Key", ""), name, taken, "Steps")
        taken.add(k)
        where = f'Step "{name or k}"'
        rk = r.number(row.get("Rank", ""), where, i)
        b["steps"].append({
            "key": k, "name": name or k, "rank": rk if isinstance(rk, int) else i,
            "active": r.span(row.get("Active for", ""), where, None, "months"),
            "hr_meeting": r.yes(row.get("HR meeting", ""), where, False),
            "hr_only": r.yes(row.get("HR only", ""), where, False),
            "termination": r.yes(row.get("Termination review", ""), where, False),
            "hr_approval": r.yes(row.get("HR approves", ""), where, True)})
    if not b["steps"]:
        b["steps"] = copy.deepcopy(SAMPLE["steps"])
        r.ask("There were no steps in the file, so the sample's were used: Coaching, Verbal, "
              "Written, Final written, Suspension and Termination review. Are those yours?")
    step_keys = {s["key"]: s for s in b["steps"]}
    step_names = {s["name"].lower(): s["key"] for s in b["steps"]}

    def step_ref(value, where):
        v = value.strip()
        if not v or v.lower() in ("none", "no", "-"):
            return None
        found = _key(v) if _key(v) in step_keys else step_names.get(v.lower())
        if not found:
            r.ask(f'{where}: "{v}" isn\'t one of the steps, so it was left out.')
        return found

    tracks = {t["key"] for t in b["tracks"]}

    def track_ref(value, where):
        v = _key(value)
        if v in tracks:
            return v
        by_name = {t["name"].lower(): t["key"] for t in b["tracks"]}
        if value.strip().lower() in by_name:
            return by_name[value.strip().lower()]
        if not value.strip() and len(b["tracks"]) == 1:
            return b["tracks"][0]["key"]
        new = key_for(value or "discipline", tracks)
        b["tracks"].append({"key": new, "name": _text(value) or "Discipline"})
        tracks.add(new)
        r.ask(f'{where}: the point total "{value or new}" wasn\'t listed, so it was added.')
        return new

    for row in _table(blocks, "Ladder", r.questions):
        t = track_ref(row.get("Point total", ""), "Ladder")
        pts = r.number(row.get("Points", ""), "Ladder")
        s = step_ref(row.get("Step", ""), "Ladder")
        if pts is not None and s:
            b["ladders"].setdefault(t, []).append({"points": pts, "step": s})
    for t, rungs in b["ladders"].items():
        rungs.sort(key=lambda x: x["points"])

    for row in _table(blocks, "Escalation", r.questions):
        w = row.get("While on", "").strip()
        to = row.get("Goes to", "").strip()
        while_key = "any" if w.lower() in ("any", "any step") else step_ref(w, "Escalation")
        to_key = "next" if to.lower() in ("next", "next step", "the next step") else step_ref(to, "Escalation")
        if while_key and to_key:
            b["escalation"].append({"while": while_key, "to": to_key})

    taken = set()
    for row in _table(blocks, "Offences", r.questions):
        name = _text(row.get("Name") or row.get("Key"))
        k = r.key(row.get("Key", ""), name, taken, "Offences")
        taken.add(k)
        where = f'Offence "{name or k}"'
        pts = r.number(row.get("Points", ""), where)
        if pts is None:
            r.ask(f"{where} has no points, so 0 was used. How many points is it?")
            pts = 0
        lo, hi = r.number(row.get("Manager min", ""), where, pts), r.number(row.get("Manager max", ""), where, pts)
        b["offences"].append({
            "key": k, "name": name or k, "track": track_ref(row.get("Point total", ""), where),
            "category": _text(row.get("Category", ""), 40),
            "attendance": r.yes(row.get("Attendance", ""), where, False), "points": pts,
            "min": lo, "max": hi, "serious": r.yes(row.get("Serious", ""), where, False),
            "repeat": None, "min_step": step_ref(row.get("Minimum step", ""), where),
            "hr_approval": r.yes(row.get("HR approves", ""), where, True),
            "writeup_required": r.yes(row.get("Write-up required", ""), where, False),
            "attachment_required": r.yes(row.get("Attachment required", ""), where, False),
            "wording": _wording(row.get("Handbook wording", ""))})
    if not b["offences"]:
        r.ask("There were no offences in the file. Add them in the Offences list.")
    offences = by_key(b["offences"])
    offence_names = {o["name"].lower(): o["key"] for o in b["offences"]}

    def offence_ref(value, where):
        v = value.strip()
        found = _key(v) if _key(v) in offences else offence_names.get(v.lower())
        if not found:
            r.ask(f'{where}: "{v}" isn\'t one of the offences, so it was left out.')
        return found

    for row in _table(blocks, "Repeats", r.questions):
        k = offence_ref(row.get("Offence", ""), "Repeats")
        if not k:
            continue
        o = offences[k]
        where = f'Repeats for "{o["name"]}"'
        within = r.span(row.get("Within", ""), where, {"months": 12}, "days")
        nth = r.number(re.sub(r"(st|nd|rd|th)$", "", row.get("Occurrence", "").strip().lower()), where)
        if not isinstance(nth, int):
            r.ask(f"{where}: say which repeat it is (2 for the second), so it was left out.")
            continue
        o["repeat"] = o["repeat"] or {"within": within, "tiers": []}
        if within != o["repeat"]["within"]:
            r.ask(f"{where}: one offence has one repeat period, so "
                  f"{span_text(o['repeat']['within'])} was used.")
        o["repeat"]["tiers"].append({"nth": nth, "add": r.number(row.get("Extra points", ""), where, 0),
                                     "step": step_ref(row.get("Step", ""), where)})
    for o in b["offences"]:
        if o["repeat"]:
            o["repeat"]["tiers"].sort(key=lambda t: t["nth"])

    _read_probation(r, blocks, b, step_ref, track_ref, offence_ref)

    if "Protected leave" in blocks:
        b["protected_leave"], taken = [], set()
        for row in _table(blocks, "Protected leave", r.questions):
            name = _text(row.get("Name") or row.get("Key"))
            k = r.key(row.get("Key", ""), name, taken, "Protected leave")
            taken.add(k)
            b["protected_leave"].append({"key": k, "name": name or k})
    else:
        r.ask("The file had no protected leave list, so the standard Oregon list was used. Ask "
              "your employment lawyer to confirm it.")
    if b["credits"]["tracks"]:
        b["credits"]["tracks"] = [t for t in b["credits"]["tracks"] if t in tracks]
    body = normalize(b)
    for problem in validate(body):
        r.ask(problem)
    return body, r.questions


def _read_settings(r: _Reader, rows: list[dict], b: dict) -> None:
    labels = {label.lower(): where for label, where in SETTINGS_ROWS}
    seen = set()
    for row in rows:
        label, value = row.get("Setting", "").strip(), row.get("Value", "").strip()
        where = labels.get(label.lower())
        if not where:
            r.ask(f'Settings: "{label}" isn\'t a setting the portal knows, so it was left out.')
            continue
        seen.add(where)
        if where == "name":
            b["name"] = _text(value) or b["name"]
        elif where == "point_size":
            size = {"whole": 1, "half": 0.5, "quarter": 0.25}.get(value.lower(), _num(value))
            if size in POINT_SIZES:
                b["point_size"] = size
            else:
                r.ask(f'Point size "{value}" isn\'t whole (1), half (0.5) or quarter (0.25) '
                      "points, so quarter points were used.")
        elif where == "skip_steps":
            b["skip_steps"] = not r.yes(value, "Steps go in order", True)
        elif where == "expiry.rule":
            words = {v.lower(): k for k, v in RULE_WORDS.items()}
            words.update({k: k for k in RULES})
            words.update({"1": "writeup", "2": "rolling", "3": "first"})
            rule = words.get(value.lower())
            if rule:
                b["expiry"]["rule"] = rule
            else:
                r.ask(f'"Points stop counting": "{value}" isn\'t one of the choices, so '
                      '"Whichever comes first" was used.')
        elif where in ("expiry.rolling", "expiry.clean_reset", "credits.clean"):
            part, key = where.split(".")
            unit = "days" if where == "credits.clean" else "months"
            b[part][key] = r.span(value, label, b[part][key], unit)
        elif where in ("approval.incidents", "approval.writeups"):
            v = value.lower().replace(" ", "_")
            ok = ("all", "per_offence") if where.endswith("incidents") else ("all", "per_step")
            if v in ok:
                b["approval"][where.split(".")[1]] = v
            else:
                r.ask(f'{label}: "{value}" isn\'t "all" or "{ok[1].replace("_", " ")}", so "all" was used.')
        elif where == "credits.tracks":
            b["credits"]["tracks"] = [_key(t) for t in re.split(r"[;,]", value) if t.strip()]
        else:
            part, key = where.split(".")
            if isinstance(b[part][key], bool):
                b[part][key] = r.yes(value, label, b[part][key])
            else:
                b[part][key] = r.number(value, label, b[part][key])
    missing = [label for label, where in SETTINGS_ROWS if where not in seen and where != "name"]
    if missing:
        r.ask("These settings weren't in the file, so the recommended ones were used: "
              + ", ".join(missing) + ".")


def _read_probation(r: _Reader, blocks, b, step_ref, track_ref, offence_ref) -> None:
    taken: set = set()
    found = {}
    for row in _table(blocks, "Probation", r.questions):
        name = _text(row.get("Name") or row.get("Key"))
        k = r.key(row.get("Key", ""), name, taken, "Probation")
        taken.add(k)
        where = f'Probation "{name or k}"'
        kind = row.get("Kind", "").strip().lower()
        if kind not in KINDS:
            kind = "disciplinary" if "discipl" in kind else "introductory"
            r.ask(f'{where}: its kind "{row.get("Kind", "")}" was read as {kind}.')
        days = r.number(row.get("Days", ""), where, 90)
        found[k] = {
            "key": k, "name": name or k, "kind": kind, "days": days, "department_days": {},
            "departments": [g.strip() for g in re.split(r"[;,]", row.get("Departments", "")) if g.strip()],
            "ladders": {}, "multiplier": r.number(row.get("Multiplier", ""), where, 1), "overrides": {},
            "serious_floor": step_ref(row.get("Serious offences go to", ""), where),
            "extensions": {"count": r.number(row.get("Extensions", ""), where, 1),
                           "days": r.number(row.get("Extension days", ""), where, 30),
                           "max_days": r.number(row.get("Most days in total", ""), where, 120)},
            "review_lead_days": r.number(row.get("Review due days before end", ""), where, 14),
            "when_end_passes": "pass" if "pass" in row.get("When the end passes", "").lower() else "remind",
            "on_pass": "clear" if "clear" in row.get("On passing", "").lower() else "keep",
            "rehires": r.yes(row.get("Rehires", ""), where, True),
            "transfers": r.yes(row.get("Transfers", ""), where, False)}

    def probation_ref(value, where):
        k = _key(value)
        by_name = {p["name"].lower(): p["key"] for p in found.values()}
        k = k if k in found else by_name.get(value.strip().lower())
        if not k:
            r.ask(f'{where}: "{value}" isn\'t one of the probation policies, so it was left out.')
        return k

    for row in _table(blocks, "Probation days by department", r.questions):
        k = probation_ref(row.get("Probation", ""), "Probation days by department")
        days = r.number(row.get("Days", ""), "Probation days by department")
        if k and row.get("Department", "").strip() and days is not None:
            found[k]["department_days"][row["Department"].strip()] = days
    for row in _table(blocks, "Probation ladder", r.questions):
        k = probation_ref(row.get("Probation", ""), "Probation ladder")
        t = track_ref(row.get("Point total", ""), "Probation ladder")
        pts = r.number(row.get("Points", ""), "Probation ladder")
        s = step_ref(row.get("Step", ""), "Probation ladder")
        if k and pts is not None and s:
            found[k]["ladders"].setdefault(t, []).append({"points": pts, "step": s})
    for row in _table(blocks, "Probation offences", r.questions):
        k = probation_ref(row.get("Probation", ""), "Probation offences")
        o = offence_ref(row.get("Offence", ""), "Probation offences")
        pts = r.number(row.get("Points", ""), "Probation offences")
        if k and o and pts is not None:
            found[k]["overrides"][o] = {"points": pts,
                                        "min": r.number(row.get("Manager min", ""), "Probation offences"),
                                        "max": r.number(row.get("Manager max", ""), "Probation offences")}
    for p in found.values():
        for rungs in p["ladders"].values():
            rungs.sort(key=lambda x: x["points"])
    b["probation"] = list(found.values())
    if "Probation" not in blocks:
        r.ask("The file had no probation rules. Add them under Probation, or leave it empty if "
              "probation works like everyone else.")


# ---------------------------------------------------------------- reading it, for everyone

def readable(version: dict) -> dict:
    """The point system in force, as anyone signed in may read it (design 2: everyone can read the
    policy): offences and their points, steps, thresholds, when points stop counting, credits and
    probation. Plain text and numbers only."""
    b = version["body"]
    x, c = b["expiry"], b["credits"]
    rolling = span_text(x["rolling"])
    expiry = {"writeup": "Points stop counting when the write-up they led to ends. Points that "
                         "didn't lead to a write-up keep counting.",
              "rolling": f"Points stop counting {rolling} after the day it happened.",
              "first": f"Points stop counting {rolling} after the day it happened, or when the "
                       "write-up they led to ends, whichever comes first."}.get(x["rule"], "")
    if x["clean_reset"]:
        expiry += (f" A clean stretch of {span_text(x['clean_reset'])} with no new incident "
                   "clears them all.")
    credits = (f"After {span_text(c['clean'])} with no new incident, {points_text(quarters(c['points']))} "
               f"comes off, at most {c['max_per_year']} a year." if c["on"] else None)

    def repeat(o):
        r = o.get("repeat")
        if not r:
            return None
        parts = []
        for t in r["tiers"]:
            what = []
            if quarters(t["add"]):
                what.append(f"adds {points_text(quarters(t['add']))}")
            if t["step"]:
                what.append(f"goes to {step_name(b, t['step'])} at least")
            parts.append(f"the {ordinal(t['nth'])} within {span_text(r['within'])} " + " and ".join(what))
        return "; ".join(parts)

    def rungs(items):
        return [{"points": r["points"], "step": step_name(b, r["step"])} for r in items or []]

    return {
        "name": version["name"], "effective": version["effective"],
        "steps_in_order": not b["skip_steps"],
        "tracks": [{"key": t["key"], "name": t["name"], "ladder": rungs(b["ladders"].get(t["key"]))}
                   for t in b["tracks"]],
        "steps": [{"name": s["name"], "active": span_text(s["active"]) if s["active"] else None}
                  for s in steps(b)],
        "offences": [{"name": o["name"], "category": o["category"], "track": track_name(b, o["track"]),
                      "points": o["points"], "range": [o["min"], o["max"]], "repeat": repeat(o),
                      "min_step": step_name(b, o["min_step"]) if o["min_step"] else None,
                      "attendance": o["attendance"], "wording": o["wording"]} for o in b["offences"]],
        "expiry": expiry, "credits": credits,
        "probation": [{"name": p["name"], "kind": KINDS.get(p["kind"], p["kind"]), "days": p["days"],
                       "multiplier": p["multiplier"],
                       "serious": step_name(b, p["serious_floor"]) if p["serious_floor"] else None,
                       "ladders": [{"track": track_name(b, t), "ladder": rungs(r)}
                                   for t, r in p["ladders"].items()]} for p in b["probation"]],
        "protected_leave": [p["name"] for p in b["protected_leave"]],
    }


# ---------------------------------------------------------------- the HR portal: Discipline setup

from aiohttp import web  # noqa: E402

from . import access, audit, server, service  # noqa: E402

MAX_REQUEST = 640 * 1024      # a point system (or its spreadsheet) is bigger than other requests
VID = r"{vid:\d{1,9}}"


async def read_big(request: web.Request) -> dict:
    """Like server.read_json, with room for a whole point system."""
    if request.content_length is None or request.content_length > MAX_REQUEST:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_REQUEST,
                                            actual_size=request.content_length or 0)
    try:
        async with asyncio.timeout(15):
            data = await request.json()
    except TimeoutError:
        raise web.HTTPRequestTimeout(text="The request took too long to arrive.") from None
    except (ValueError, RecursionError):
        raise web.HTTPBadRequest(text="That request couldn't be read.") from None
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="That request couldn't be read.")
    return data


def _who(request) -> tuple:
    svc = server.svc_of(request)
    v = server.viewer_of(request)
    return svc, svc.db, v, svc.client_ip(request)


def _version(db, request) -> dict | None:
    return get(db, request.match_info["vid"])


def _meta(db, v: dict) -> dict:
    listed = {x["id"]: x for x in versions(db)}
    found = {k: v[k] for k in ("id", "name", "status", "effective", "source", "based_on",
                               "created_at", "updated_at", "published_at")}
    found.update({k: listed[v["id"]][k] for k in ("state", "label", "until", "status")})
    return found


@access.rule("signed_in")
async def api_current(request):
    svc, db, v, _ = _who(request)
    now = current(db)
    if not now:
        return web.json_response({"set_up": False, "message": NOT_SET_UP})
    return web.json_response({"set_up": True, "policy": readable(now)})


@access.rule("hr")
async def api_versions(request):
    svc, db, v, _ = _who(request)
    now = current(db)
    return web.json_response({"versions": versions(db), "today": today(db).isoformat(),
                              "current": now and _meta(db, now), "message": NOT_SET_UP})


@access.rule("hr")
async def api_version(request):
    svc, db, v, _ = _who(request)
    found = _version(db, request)
    if not found:
        return server.error(server.NOT_FOUND, 404)
    published = [x for x in versions(db) if x["status"] != "draft" and x["state"] != "withdrawn"]
    return web.json_response({"version": _meta(db, found), "body": found["body"],
                              "problems": validate(found["body"]), "today": today(db).isoformat(),
                              "first": not published, "departments": server.departments_list(svc),
                              "rules": RULES, "kinds": KINDS, "point_sizes": POINT_SIZES})


@access.rule("hr")
async def api_new(request):
    svc, db, v, ip = _who(request)
    data = await server.read_optional(request)
    source = data.get("from")
    if source == "sample":
        body, how, based = SAMPLE, "sample", None
    elif source == "blank" or source is None:
        body = {**DEFAULTS, "name": "New point system", "tracks": [{"key": "discipline", "name": "Discipline"}]}
        how, based = "blank", None
    else:
        old = get(db, source)
        if not old:
            return server.error("Choose a version to start from.")
        body = {**old["body"], "name": f"{old['name']} (new draft)"[:NAME_LEN]}
        how, based = "copy", old["id"]
    vid = save_draft(db, body, v.id, source=how, based_on=based)
    audit.record(db, "Started a point system draft", actor_id=v.id, actor=v.name,
                 detail=normalize(body)["name"], ip=ip)
    return web.json_response({"ok": True, "id": vid})


@access.rule("hr")
async def api_save(request):
    svc, db, v, ip = _who(request)
    found = _version(db, request)
    if not found:
        return server.error(server.NOT_FOUND, 404)
    data = await read_big(request)
    if not isinstance(data.get("body"), dict):
        return server.error("That point system couldn't be read.")
    try:
        save_draft(db, data["body"], v.id, found["id"])
    except ValueError as err:
        return server.error(str(err), 409)
    saved = get(db, found["id"])
    audit.record(db, "Saved a point system draft", actor_id=v.id, actor=v.name,
                 detail=saved["name"], ip=ip)
    return web.json_response({"ok": True, "problems": validate(saved["body"]),
                              "version": _meta(db, saved)})


@access.rule("hr", sensitive=True)
async def api_delete(request):
    svc, db, v, ip = _who(request)
    found = _version(db, request)
    if not found:
        return server.error(server.NOT_FOUND, 404)
    try:
        delete_draft(db, found["id"])
    except ValueError as err:
        return server.error(str(err), 409)
    audit.record(db, "Deleted a point system draft", actor_id=v.id, actor=v.name,
                 detail=found["name"], ip=ip, security=True)
    return web.json_response({"ok": True})


@access.rule("hr", sensitive=True)
async def api_publish(request):
    svc, db, v, ip = _who(request)
    found = _version(db, request)
    if not found:
        return server.error(server.NOT_FOUND, 404)
    data = await server.read_json(request)
    try:
        publish(db, found["id"], data.get("effective"), v.id)
    except ValueError as err:
        return server.error(str(err), 409)
    audit.record(db, "Published the point system", actor_id=v.id, actor=v.name,
                 detail=f"{found['name']}, in force from {as_date(data['effective']).isoformat()}",
                 ip=ip, security=True)
    return web.json_response({"ok": True, "version": _meta(db, get(db, found["id"]))})


@access.rule("hr", sensitive=True)
async def api_withdraw(request):
    svc, db, v, ip = _who(request)
    found = _version(db, request)
    if not found:
        return server.error(server.NOT_FOUND, 404)
    try:
        withdraw(db, found["id"], v.id)
    except ValueError as err:
        return server.error(str(err), 409)
    audit.record(db, "Withdrew a point system version", actor_id=v.id, actor=v.name,
                 detail=found["name"], ip=ip, security=True)
    return web.json_response({"ok": True})


@access.rule("hr")
async def api_import(request):
    svc, db, v, ip = _who(request)
    data = await read_big(request)
    text = data.get("csv")
    if not isinstance(text, str) or not text.strip():
        return server.error("Choose the spreadsheet (CSV) to import, or paste it.")
    try:
        body, questions = import_csv(text)
    except ValueError as err:
        return server.error(str(err))
    if isinstance(data.get("name"), str) and data["name"].strip():
        body["name"] = _text(data["name"])
    try:
        vid = save_draft(db, body, v.id, source="import")
    except ValueError as err:
        return server.error(str(err))
    audit.record(db, "Imported a point system", actor_id=v.id, actor=v.name, detail=body["name"], ip=ip)
    return web.json_response({"ok": True, "id": vid, "questions": questions,
                              "problems": validate(body)})


@access.rule("hr")
async def api_export(request):
    svc, db, v, ip = _who(request)
    found = _version(db, request)
    if not found:
        return server.error(server.NOT_FOUND, 404)
    name = re.sub(r"[^A-Za-z0-9 _-]+", "", found["name"]).strip() or "Point system"
    audit.record(db, "Exported the point system", actor_id=v.id, actor=v.name, detail=found["name"], ip=ip)
    return web.json_response({"filename": f"{name}.csv", "csv": export_csv(found["body"])})


@access.rule("hr")
async def api_simulate(request):
    svc, db, v, _ = _who(request)
    found = _version(db, request)
    if not found:
        return server.error(server.NOT_FOUND, 404)
    data = await read_big(request)
    body = data["body"] if isinstance(data.get("body"), dict) and found["status"] == "draft" \
        else found["body"]
    events = data.get("events")
    if not isinstance(events, list):
        return server.error("Add a made-up incident first.")
    department = str(data.get("department") or "") or None
    result = await asyncio.to_thread(points.simulate, body, events, data.get("start"),
                                     department=department)
    return web.json_response(result)


@access.rule("hr")
async def api_rescore_people(request):
    svc, db, v, _ = _who(request)
    now = current(db)
    if not now:
        return web.json_response({"version": None, "people": [], "message": NOT_SET_UP})
    people = [p for p in points.scored_elsewhere(db, now["id"]) if p["id"] != v.id]
    return web.json_response({"version": _meta(db, now), "people": people})


def _rescore_ids(v, data) -> list[str]:
    ids = data.get("people")
    if not isinstance(ids, list) or not ids or len(ids) > 500:
        raise ValueError("Choose the people to re-score.")
    ids = [str(i) for i in ids]
    for pid in ids:
        access.not_own_file(v, pid)
    return ids


@access.rule("hr")
async def api_rescore_preview(request):
    svc, db, v, ip = _who(request)
    data = await server.read_json(request)
    now = current(db)
    try:
        ids = _rescore_ids(v, data)
        if not now:
            raise ValueError(NOT_SET_UP)
        result = points.rescore_preview(db, ids, now["id"])
    except ValueError as err:
        return server.error(str(err))
    audit.record(db, "Previewed re-scoring points", actor_id=v.id, actor=v.name,
                 detail=f"{len(ids)} people, under {now['name']}", ip=ip)
    return web.json_response(result)


@access.rule("hr", sensitive=True)
async def api_rescore(request):
    svc, db, v, ip = _who(request)
    data = await server.read_json(request)
    now = current(db)
    reason = " ".join(str(data.get("reason") or "").split())[:500]
    try:
        ids = _rescore_ids(v, data)
        if not now:
            raise ValueError(NOT_SET_UP)
        if not reason:
            raise ValueError("Give a reason for re-scoring.")
        result = points.rescore(db, ids, now["id"], v.id, reason)
    except ValueError as err:
        return server.error(str(err))
    for p in result["people"]:
        n = sum(1 for ch in p["changes"] if ch["change"])
        if n:
            audit.record(db, "Re-scored points", actor_id=v.id, actor=v.name, target_id=p["person_id"],
                         target=p["name"], detail=f"{n} incident(s), under {now['name']}", ip=ip)
    return web.json_response(result)


@server.add_routes
def routes(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/policy", api_versions)
    r.add_post("/api/policy", api_new)
    r.add_get("/api/policy/current", api_current)
    r.add_post("/api/policy/import", api_import)
    r.add_get("/api/policy/rescore", api_rescore_people)
    r.add_post("/api/policy/rescore/preview", api_rescore_preview)
    r.add_post("/api/policy/rescore", api_rescore)
    r.add_get(f"/api/policy/{VID}", api_version)
    r.add_put(f"/api/policy/{VID}", api_save)
    r.add_delete(f"/api/policy/{VID}", api_delete)
    r.add_post(f"/api/policy/{VID}/publish", api_publish)
    r.add_post(f"/api/policy/{VID}/withdraw", api_withdraw)
    r.add_get(f"/api/policy/{VID}/export", api_export)
    r.add_post(f"/api/policy/{VID}/simulate", api_simulate)


@server.settings_section("discipline", 40)
def discipline_settings(svc, viewer) -> dict:
    db = svc.db
    now = current(db)
    listed = versions(db)
    return {"set_up": now is not None, "message": NOT_SET_UP,
            "current": now and {"id": now["id"], "name": now["name"], "effective": now["effective"]},
            "scheduled": [x for x in listed if x["state"] == "scheduled"],
            "drafts": sum(1 for x in listed if x["state"] == "draft")}


@server.todo
def policy_todo(svc, viewer) -> list[dict]:
    if not viewer.is_hr or set_up(svc.db):
        return []
    return [{"text": "The point system isn't set up yet. Load your written point system, test it "
                     "in the simulator and publish it.", "link": "#/policy"}]


def _part(svc) -> None:
    svc.on_start.append(lambda s: setup(s.db))


service.PARTS.append(_part)

from . import points  # noqa: E402  (points.py reads this module: imported last)
