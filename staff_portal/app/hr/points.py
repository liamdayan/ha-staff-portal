"""Points, kept like a bank statement (design 7.1 to 7.6): every change to someone's points is a
dated line in point_entries, and totals are always added up from the lines. Lines are never
changed or deleted (hrdb.append_only): a correction is a new line that points at the one it
corrects, with a reason.

Lines:
  incident    points from an incident (discipline.py): the day it happened, and the day it
              counts from (HR's approval). Its expiry is dated from the day it happened.
  adjustment  HR adds or takes off points from a start day, with an optional end day; or, pointing
              at an incident (target_id), changes its points (the original stays visible), the day
              it stops counting (end_kind "override"; the write-up's end date is end_kind
              "writeup"), or the version it's scored under (a re-score)
  credit      points earned back after a clean stretch (when the point system has credits on)
  void        an incident, adjustment or credit stops counting, from the void's day on

Points are whole numbers of quarter points inside (1.5 points is 6), so sums stay exact. Dates are
calendar days in HR's time zone, never times, so clock changes can't move a boundary. A line
counts from its counts_from day; "as of" a day, only lines counting by then are used.

When points stop counting (the version an incident was scored under decides, 7.6):
  rule "writeup"  on the end date of the write-up it led to (until then it counts);
  rule "rolling"  a rolling window from the day it happened (12 months after Jan 31 is Jan 31);
  rule "first"    whichever of the two comes first (the recommended default);
  clean reset     optionally, a stretch with no new incident in that point total clears it all;
                  any new incident restarts the clock;
  HR's override   always wins. An end day is the first day it no longer counts.
Credits and points taken off take off the oldest points counting that day first, and go with
them when they expire, so they never pile up against future incidents and a total is never
below 0.
"""

import bisect
import json
import time
from collections import defaultdict
from datetime import date, timedelta
from fractions import Fraction

from . import audit, crypto, policy, service
from .policy import add_span, as_date, as_points, fmt, points_text, quarters

SCHEMA = """
-- The points ledger: one line per change to someone's points; never changed or deleted.
CREATE TABLE IF NOT EXISTS point_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,
    kind TEXT NOT NULL,              -- incident, adjustment, credit or void
    track TEXT NOT NULL,
    quarters INTEGER NOT NULL,       -- points x 4, so quarter points stay exact; minus takes off
    happened TEXT NOT NULL,          -- YYYY-MM-DD: the day it happened (an adjustment: its start)
    counts_from TEXT NOT NULL,       -- YYYY-MM-DD: the day it starts counting, or takes effect
    ends TEXT,                       -- YYYY-MM-DD: the first day it no longer counts
    end_kind TEXT,                   -- a line changing another's end: writeup or override
    target_id INTEGER,               -- the line this one changes or voids
    incident_id TEXT,                -- the incident it belongs to (discipline.py)
    offence TEXT,
    version_id INTEGER,              -- the point system version it was scored under
    scoring TEXT,                    -- JSON: how its points were worked out (numbers and keys)
    reason TEXT,                     -- sealed
    actor_id TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS point_entries_person ON point_entries (person_id, id);
CREATE INDEX IF NOT EXISTS point_entries_incident ON point_entries (incident_id);
"""

KINDS = ("incident", "adjustment", "credit", "void")
REASON_LEN = 2000
YEAR = {"months": 12}


def setup(db) -> None:
    db.add_schema(SCHEMA)
    db.append_only("point_entries")


# ---------------------------------------------------------------- the arithmetic

_SPANS: dict = {}


def _add(day: date, span) -> date | None:
    """add_span, remembered (the same few spans are added to the same days again and again)."""
    if not span:
        return None
    key = (day, *next(iter(span.items())))
    if key not in _SPANS:
        if len(_SPANS) > 50000:
            _SPANS.clear()
        _SPANS[key] = add_span(day, span)
    return _SPANS[key]


def _is_base(line) -> bool:
    """Points that count by themselves: incidents, and points HR added."""
    return line["kind"] == "incident" or (line["kind"] == "adjustment" and not line["target_id"]
                                          and line["q"] > 0)


def _is_minus(line) -> bool:
    """Points taken off: credits, and points HR took off."""
    return line["kind"] == "credit" or (line["kind"] == "adjustment" and not line["target_id"]
                                        and line["q"] < 0)


class Calc:
    """One person's lines, as known on a day (lines counting from later days are left out),
    worked out on any day from then on. bodies: {version id: point system body}."""

    def __init__(self, lines: list[dict], bodies: dict, known: date):
        self.known = known
        self.bodies = bodies
        self.lines = sorted((ln for ln in lines if ln["counts_from"] <= known),
                            key=lambda ln: (ln["happened"], ln["id"]))
        self.void_at: dict = {}
        self.changes: dict = defaultdict(list)
        for ln in sorted(self.lines, key=lambda ln: (ln["counts_from"], ln["id"])):
            if ln["kind"] == "void" and ln["target_id"]:
                self.void_at.setdefault(ln["target_id"], ln["counts_from"])
            elif ln["kind"] == "adjustment" and ln["target_id"]:
                self.changes[ln["target_id"]].append(ln)
        self.bases = [ln for ln in self.lines if _is_base(ln)]
        self.minus = [ln for ln in self.lines if _is_minus(ln)]
        self._ends: dict = {}       # worked out once per line and day
        self._amounts: dict = {}
        self._days: dict = {}
        self._resets: dict = {}

    def voided(self, line, day: date) -> bool:
        when = self.void_at.get(line["id"])
        return when is not None and when <= day

    def _changes(self, line, day: date) -> list[dict]:
        return [c for c in self.changes.get(line["id"], ()) if c["counts_from"] <= day
                and not self.voided(c, day)]

    def amount(self, line, day: date) -> int:
        """An incident's (or added points') quarter points on a day, after HR's changes."""
        key = (line["id"], day)
        if key not in self._amounts:
            self._amounts[key] = max(0, line["q"] + sum(c["q"] for c in self._changes(line, day)
                                                        if not c["end_kind"]))
        return self._amounts[key]

    def version(self, line, day: date):
        found = line["version_id"]
        for c in self._changes(line, day):
            if c["version_id"] is not None and not c["end_kind"]:
                found = c["version_id"]
        return found

    def incident_days(self, track: str, day: date) -> list[date]:
        key = (track, day)
        if key not in self._days:
            self._days[key] = sorted({ln["happened"] for ln in self.bases if ln["kind"] == "incident"
                                      and ln["track"] == track and ln["counts_from"] <= day
                                      and not self.voided(ln, day) and self.amount(ln, day) > 0})
        return self._days[key]

    def reset_day(self, line, day: date, span) -> date:
        """The clean-period reset after this line: the end of the first stretch, from the day it
        happened, with no new incident in its point total."""
        days = self.incident_days(line["track"], day)
        key = (line["track"], day, json.dumps(span, sort_keys=True))
        if key not in self._resets:
            # Where the stretch from each incident ends: at the next incident's reset, while the
            # next one comes before this one's stretch is over.
            ends = [None] * len(days)
            for i in range(len(days) - 1, -1, -1):
                own = _add(days[i], span)
                ends[i] = ends[i + 1] if i + 1 < len(days) and days[i + 1] < own else own
            self._resets[key] = ends
        ends = self._resets[key]
        start = line["happened"]
        j = bisect.bisect_right(days, start)
        return ends[j] if j < len(days) and days[j] < _add(start, span) else _add(start, span)

    def end(self, line, day: date) -> date | None:
        """The first day this line no longer counts, as things stand on `day` (None: not yet
        known, so it counts until something ends it)."""
        key = (line["id"], day)
        if key in self._ends:
            return self._ends[key]
        override = writeup = None
        for c in self._changes(line, day):
            if c["end_kind"] == "override":
                override = c
            elif c["end_kind"] == "writeup":
                writeup = c
        if override:
            found = override["ends"]
        else:
            expiry = (self.bodies.get(self.version(line, day)) or {}).get("expiry") or {}
            rule = expiry.get("rule")
            if line["kind"] == "adjustment" and line["ends"]:
                found = line["ends"]
            else:
                ends = [d for d in (
                    _add(line["happened"], expiry.get("rolling")) if rule in ("rolling", "first") else None,
                    writeup["ends"] if writeup and rule in ("writeup", "first") else None) if d]
                found = min(ends) if ends else None
            if expiry.get("clean_reset"):
                reset = self.reset_day(line, day, expiry["clean_reset"])
                found = min(found, reset) if found else reset
        self._ends[key] = found
        return found

    def alive(self, line, day: date) -> bool:
        if line["counts_from"] > day or self.voided(line, day):
            return False
        ends = self.end(line, day)
        return ends is None or day < ends

    def minus_alive(self, line, day: date) -> bool:
        return line["counts_from"] <= day and not self.voided(line, day) and (
            line["ends"] is None or day < line["ends"])

    def state(self, day: date) -> tuple[dict, dict, dict]:
        """({track: quarter points counting}, {line id: points left}, {line id: points taken off})
        on a day. Credits and points taken off take the oldest points counting on their own day
        first."""
        taken: dict = defaultdict(int)
        used: dict = defaultdict(int)
        for m in sorted((m for m in self.minus if self.minus_alive(m, day)),
                        key=lambda m: (m["counts_from"], m["id"])):
            need, when = -m["q"], m["counts_from"]
            for lot in self.bases:          # oldest first (sorted by the day it happened)
                if not need:
                    break
                if lot["track"] != m["track"] or not self.alive(lot, when):
                    continue
                take = min(self.amount(lot, when) - taken[lot["id"]], need)
                if take > 0:
                    taken[lot["id"]] += take
                    used[m["id"]] += take
                    need -= take
        totals: dict = defaultdict(int)
        left = {}
        for lot in self.bases:
            left[lot["id"]] = max(0, self.amount(lot, day) - taken[lot["id"]])
            if self.alive(lot, day):
                totals[lot["track"]] += left[lot["id"]]
        return dict(totals), left, dict(used)

    def totals(self, day: date) -> dict:
        return self.state(day)[0]

    def describe(self, day: date) -> list[dict]:
        """Every line known on `day`, with its state: counts, expired, voided (or applied, for
        voids)."""
        _, left, used = self.state(day)
        found, states = [], {}
        for ln in self.lines:
            row = {"id": ln["id"], "kind": ln["kind"], "track": ln["track"],
                   "points": as_points(ln["q"]), "happened": ln["happened"].isoformat(),
                   "counts_from": ln["counts_from"].isoformat(), "incident_id": ln["incident_id"],
                   "offence": ln["offence"], "version_id": ln["version_id"],
                   "target_id": ln["target_id"], "end_kind": ln["end_kind"],
                   "actor_id": ln.get("actor_id"), "created_at": ln.get("created_at"),
                   "scoring": ln.get("scoring") or {}, "reason": ln.get("reason")}
            if _is_base(ln):
                ends = self.end(ln, day)
                state = "voided" if self.voided(ln, day) else "counts" if self.alive(ln, day) \
                    else "expired"
                row.update(ends=ends and ends.isoformat(), amount=as_points(self.amount(ln, day)),
                           counting=as_points(left[ln["id"]]) if state == "counts" else 0.0)
            elif _is_minus(ln):
                state = "voided" if self.voided(ln, day) else "counts" if self.minus_alive(ln, day) \
                    else "expired"
                row.update(ends=ln["ends"] and ln["ends"].isoformat(),
                           taken_off=as_points(used.get(ln["id"], 0)))
            elif ln["kind"] == "void":
                state = "applied"
            else:
                state = "voided" if self.voided(ln, day) else None   # its target's, below
                row["ends"] = ln["ends"] and ln["ends"].isoformat()
            states[ln["id"]] = state
            row["state"] = state
            found.append(row)
        for row in found:
            if row["state"] is None:
                row["state"] = states.get(row["target_id"]) or "counts"
        return found


# ---------------------------------------------------------------- reading the ledger

def _line(r) -> dict:
    return {"id": r["id"], "person_id": r["person_id"], "kind": r["kind"], "track": r["track"],
            "q": r["quarters"], "happened": as_date(r["happened"]),
            "counts_from": as_date(r["counts_from"]), "ends": as_date(r["ends"]),
            "end_kind": r["end_kind"], "target_id": r["target_id"], "incident_id": r["incident_id"],
            "offence": r["offence"], "version_id": r["version_id"],
            "scoring": json.loads(r["scoring"]) if r["scoring"] else {},
            "sealed": r["reason"], "actor_id": r["actor_id"], "created_at": r["created_at"]}


def lines_of(db, person_id) -> list[dict]:
    """A person's lines, oldest first (reasons still sealed: see history())."""
    return [_line(r) for r in db.conn.execute(
        "SELECT * FROM point_entries WHERE person_id = ? ORDER BY id", (str(person_id),))]


def bodies_for(db, lines) -> dict:
    """{version id: body} for the versions these lines were scored under."""
    found = {}
    for vid in {ln["version_id"] for ln in lines if ln["version_id"] is not None}:
        v = policy.get(db, vid)
        if v:
            found[vid] = v["body"]
    return found


def calc(db, person_id, known=None) -> Calc:
    """A person's points as known on a day (today by default)."""
    lines = lines_of(db, person_id)
    return Calc(lines, bodies_for(db, lines), as_date(known) or policy.today(db))


def _floats(totals: dict) -> dict:
    return {t: as_points(q) for t, q in totals.items()}


def totals(db, person_id, as_of=None) -> dict:
    """{track: points counting} on a day (today by default), after expiry, voids, adjustments
    and credits. Every point total of the version in force then is there, at 0 if need be."""
    day = as_date(as_of) or policy.today(db)
    found = {t: 0 for t in _tracks_on(db, day)}
    found.update(calc(db, person_id, day).totals(day))
    return _floats(found)


def _tracks_on(db, day: date) -> list[str]:
    v = policy.current(db, day)
    return [t["key"] for t in v["body"]["tracks"]] if v else []


def history(db, person_id, as_of=None, include_expired=True) -> list[dict]:
    """Every line counting by that day, oldest first, with its state (counts, expired, voided,
    or applied for a void), its end day, its reason (unsealed: managers' and HR's own words,
    so send it only to people allowed to see it) and how much of it still counts."""
    day = as_date(as_of) or policy.today(db)
    c = calc(db, person_id, day)
    sealed = {ln["id"]: ln["sealed"] for ln in c.lines}
    found = []
    for row in c.describe(day):
        if row["state"] == "expired" and not include_expired:
            continue
        try:
            row["reason"] = crypto.unseal_text(sealed.get(row["id"]), "points.reason")
        except crypto.Damaged:
            row["reason"] = None
        found.append(row)
    return found


def next_drop(db, person_id, as_of=None) -> dict | None:
    """The next day points stop counting, if nothing else happens: {date, points (how many stop),
    tracks {track: points}, totals (after)}, or None."""
    day = as_date(as_of) or policy.today(db)
    c = calc(db, person_id, day)
    before = c.totals(day)
    days = sorted({e for ln in c.bases if c.alive(ln, day) and (e := c.end(ln, day)) and e > day})
    for d in days:
        after = c.totals(d)
        dropped = {t: before.get(t, 0) - after.get(t, 0) for t in before
                   if before.get(t, 0) > after.get(t, 0)}
        if dropped:
            return {"date": d.isoformat(), "points": as_points(sum(dropped.values())),
                    "tracks": _floats(dropped), "totals": _floats(after)}
        before = after
    return None


def past_incidents(db, person_id, as_of=None) -> list[dict]:
    """A person's incidents for repeat rules (incident_points' history): [{offence, happened,
    points, protected, voided}]."""
    day = as_date(as_of) or policy.today(db)
    c = calc(db, person_id, day)
    return [{"offence": ln["offence"], "happened": ln["happened"],
             "points": as_points(c.amount(ln, day)), "voided": c.voided(ln, day),
             "protected": bool((ln["scoring"] or {}).get("protected"))}
            for ln in c.bases if ln["kind"] == "incident"]


# ---------------------------------------------------------------- scoring one incident

def repeat_of(body: dict, offence_key, happened, history) -> tuple[int | None, dict | None]:
    """(which occurrence this is within the offence's repeat period, the repeat that applies), or
    (None, None) without a repeat rule. Protected absences, voided incidents and incidents that
    ended up with 0 points don't count. An earlier one counts while its period hasn't ended:
    within 90 days of Jan 1 means before Apr 1."""
    o = policy.offence(body, offence_key)
    rule = o and o.get("repeat")
    day = as_date(happened)
    if not rule or not day:
        return None, None
    nth = 1
    for h in history or ():
        when = as_date(h.get("happened"))
        if (h.get("offence") == offence_key and when and when <= day and not h.get("voided")
                and not h.get("protected") and quarters(h.get("points") or 0) > 0
                and add_span(when, rule["within"]) > day):
            nth += 1
    tier = None
    for t in rule["tiers"]:
        if t["nth"] <= nth:
            tier = t
    return nth, tier


def _choice(value, size_q: int, what: str, errors: list) -> int | None:
    if value is None or value == "":
        return None
    try:
        q = quarters(value)
    except ValueError:
        errors.append(f"{what} must be a number of points.")
        return None
    if q < 0 or q % size_q or q > policy.MAX_POINTS * 4:
        unit = {4: "whole", 2: "half", 1: "quarter"}[size_q]
        errors.append(f"{what} must be from 0 to {policy.MAX_POINTS}, in {unit} points.")
        return None
    return q


def incident_points(body: dict, offence_key, ctx: dict, history, manager_points=None,
                    hr_points=None) -> dict:
    """The points for one incident (design 7.3), in this order:
      1. the offence's points (or, on probation, the probation policy's own points for it);
      2. its repeat rule adds points for a repeat within its period;
      3. the probation multiplier (not for offences with probation points of their own),
         rounded to the nearest whole, half or quarter point;
      4. a protected absence scores 0, whatever else;
      5. a manager may choose the offence's points within its range (repeats and the multiplier
         still apply on top), and HR may set any value, including 0.
    ctx: {"happened": date, "on_probation": probation key or None, "protected": leave key or
    None}. history: the person's other incidents (past_incidents()).
    Returns {default (what the rules give), final, repeat (the occurrence, or None), protected,
    probation_multiplier, errors, track, scored (after the manager's choice), repeat_step, range
    ([min, max] a manager may choose), notes}."""
    errors, notes = [], []
    o = policy.offence(body, offence_key)
    size_q = quarters(body.get("point_size") or 0.25)
    blank = {"default": 0.0, "final": 0.0, "repeat": None, "protected": False,
             "probation_multiplier": 1.0, "errors": errors, "track": None, "scored": 0.0,
             "repeat_step": None, "range": [0.0, 0.0], "notes": notes}
    if not o:
        errors.append("That offence isn't in this point system.")
        return blank
    ctx = ctx or {}
    prob_key = ctx.get("on_probation")
    prob = policy.probation_policy(body, prob_key)
    if prob_key and not prob:
        errors.append("That probation policy isn't in this point system.")
    special = (prob or {}).get("overrides", {}).get(o["key"]) if prob else None
    if special:
        base = quarters(special["points"])
        lo = quarters(special["min"] if special.get("min") is not None else special["points"])
        hi = quarters(special["max"] if special.get("max") is not None else special["points"])
        mult = Fraction(1)
        notes.append(f"{prob['name']}: {points_text(base)} for {o['name']}.")
    else:
        base, lo, hi = quarters(o["points"]), quarters(o["min"]), quarters(o["max"])
        mult = Fraction(str(prob["multiplier"])) if prob else Fraction(1)
    nth, tier = repeat_of(body, o["key"], ctx.get("happened"), history)
    add = quarters(tier["add"]) if tier else 0
    if tier:
        notes.append(f"{policy.ordinal(nth)} within {policy.span_text(o['repeat']['within'])}"
                     + (f": {points_text(add)} more" if add else ""))
    if mult != 1:
        notes.append(f"{prob['name']}: points × {fmt(int(mult * 4))}.")
    default = policy.round_to((base + add) * mult, size_q)
    chosen = base
    picked = _choice(manager_points, size_q, "The manager's points", errors)
    if picked is not None:
        if lo <= picked <= hi:
            chosen = picked
        else:
            errors.append(f"Managers may choose {fmt(lo)} to {fmt(hi)} points for {o['name']}.")
    scored = policy.round_to((chosen + add) * mult, size_q)
    leave = ctx.get("protected")
    protected = bool(leave) and leave != "no"
    final = scored
    if protected:
        if not o.get("attendance"):
            errors.append(f"Only attendance offences can be protected leave, and {o['name']} isn't one.")
        final = 0
        notes.append("Protected leave: 0 points, and it never leads to a step.")
        if hr_points not in (None, "") and _choice(hr_points, size_q, "HR's points", errors):
            errors.append("A protected absence always scores 0.")
    else:
        hr = _choice(hr_points, size_q, "HR's points", errors)
        if hr is not None:
            final = hr
    return {"default": as_points(default), "final": as_points(final), "repeat": nth,
            "protected": protected, "probation_multiplier": float(mult), "errors": errors,
            "track": o["track"], "scored": as_points(scored),
            "repeat_step": tier["step"] if tier else None,
            "range": [as_points(lo), as_points(hi)], "notes": notes}


def required_step(body: dict, ctx: dict, totals: dict, offence_key, active_step_key) -> dict:
    """The step an approved incident calls for (design 7.5): the highest of the step its point
    total reaches (on the probation ladder while on probation), the offence's minimum step, its
    repeat's step, the probation floor for serious offences, and escalation while a step is still
    active. When steps go in order, points alone only reach the next step on the ladder above the
    active one (the reason says how far they reached). Only a step higher than the active one is
    due. ctx: {"on_probation", "protected", "repeat" (incident_points' repeat)}. totals: {track:
    points}, including this incident.
    Returns {step (key, or None: nothing new is due), reason, reached (the highest step found),
    hr_only}."""
    ctx = ctx or {}
    if ctx.get("protected") and ctx["protected"] != "no":
        return {"step": None, "reason": "A protected absence never leads to a step.",
                "reached": None, "hr_only": False}
    o = policy.offence(body, offence_key)
    track = o["track"] if o else (body["tracks"][0]["key"] if body.get("tracks") else "")
    total = quarters((totals or {}).get(track, 0) or 0)
    prob = policy.probation_policy(body, ctx.get("on_probation"))
    active = policy.step(body, active_step_key)
    active_rank = active["rank"] if active else 0
    found = []          # (rank, key, reason)
    rungs = policy.ladder(body, track, prob and prob["key"])
    on_ladder = [r for r in rungs if quarters(r["points"]) <= total]
    if on_ladder:
        top = on_ladder[-1]
        reason = f"Points {fmt(total)} ≥ {fmt(quarters(top['points']))} → {policy.step_name(body, top['step'])}"
        key = top["step"]
        if not body.get("skip_steps") and policy.rank(body, key) > active_rank:
            above = [r for r in rungs if policy.rank(body, r["step"]) > active_rank]
            nxt = above[0]["step"] if above else key
            if nxt != key:
                reason += (f". Steps go in order, so the next step is "
                           f"{policy.step_name(body, nxt)}")
                key = nxt
        found.append((policy.rank(body, key), key, reason, top["step"]))
    if o and o.get("min_step"):
        found.append((policy.rank(body, o["min_step"]), o["min_step"],
                      f"{o['name']} goes to {policy.step_name(body, o['min_step'])} at least", None))
    if o and ctx.get("repeat"):
        nth, tier = ctx["repeat"], None
        for t in (o.get("repeat") or {}).get("tiers") or ():
            if t["nth"] <= nth:
                tier = t
        if tier and tier["step"]:
            found.append((policy.rank(body, tier["step"]), tier["step"],
                          f"{policy.ordinal(nth)} {o['name']} within "
                          f"{policy.span_text(o['repeat']['within'])} → "
                          f"{policy.step_name(body, tier['step'])}", None))
    if prob and o and o.get("serious") and prob.get("serious_floor"):
        found.append((policy.rank(body, prob["serious_floor"]), prob["serious_floor"],
                      f"Serious offence during {prob['name']} → "
                      f"{policy.step_name(body, prob['serious_floor'])}", None))
    if active:
        ordered = policy.steps(body)
        for rule in body.get("escalation") or ():
            if rule["while"] not in (active["key"], "any"):
                continue
            if rule["to"] == "next":
                higher = [s for s in ordered if s["rank"] > active_rank]
                to = higher[0]["key"] if higher else None
            else:
                to = rule["to"]
            if to:
                found.append((policy.rank(body, to), to,
                              f"New incident while on {active['name']} → {policy.step_name(body, to)}",
                              None))
    if not found:
        return {"step": None, "reason": "Below the first step.", "reached": None, "hr_only": False}
    best = max(found, key=lambda f: f[0])
    reached = max((f[3] or f[1] for f in found), key=lambda k: policy.rank(body, k))
    if best[0] <= active_rank:
        return {"step": None, "reason": f"Already on {active['name']}: no new step.",
                "reached": reached, "hr_only": False}
    s = policy.step(body, best[1])
    return {"step": best[1], "reason": best[2], "reached": reached,
            "hr_only": bool(s.get("hr_only") or s.get("termination"))}


# ---------------------------------------------------------------- writing lines

def _today(db) -> date:
    return policy.today(db)


def _seal(reason) -> str | None:
    text = " ".join(str(reason or "").split())[:REASON_LEN]
    return crypto.seal_text(text, "points.reason") if text else None


def _need_reason(reason) -> None:
    if not " ".join(str(reason or "").split()):
        raise ValueError("Give a reason.")


def _day(value, what: str) -> date:
    d = as_date(value)
    if d is None:
        raise ValueError(f"Choose {what}.")
    return d


def _insert(db, person_id, kind, track, q, happened: date, counts_from: date, actor_id, reason, *,
            ends=None, end_kind=None, target_id=None, incident_id=None, offence=None,
            version_id=None, scoring=None, commit=True) -> int:
    if db.person(person_id) is None:
        raise ValueError("That person isn't here.")
    cur = db.conn.execute(
        "INSERT INTO point_entries (person_id, kind, track, quarters, happened, counts_from, ends, "
        "end_kind, target_id, incident_id, offence, version_id, scoring, reason, actor_id, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(person_id), kind, track, q, happened.isoformat(), counts_from.isoformat(),
         ends.isoformat() if ends else None, end_kind, target_id,
         None if incident_id is None else str(incident_id), offence, version_id,
         json.dumps(scoring, separators=(",", ":")) if scoring else None, _seal(reason),
         None if actor_id is None else str(actor_id), time.time()))
    if commit:
        db.conn.commit()
    return cur.lastrowid


def _scoring(scoring, ctx=None) -> dict:
    """What's kept of incident_points' result: numbers and keys, never the leave's kind."""
    s, ctx = scoring or {}, ctx or {}
    found = {k: s.get(k) for k in ("default", "final", "repeat", "probation_multiplier")
             if s.get(k) is not None}
    for k in ("manager", "hr", "on_probation"):
        if (s.get(k) if k in s else ctx.get(k)) is not None:
            found[k] = s.get(k) if k in s else ctx.get(k)
    found["protected"] = bool(s.get("protected") or (ctx.get("protected") not in (None, "", "no")))
    return found


def add_incident_entry(db, person_id, incident_id, track, points, happened, counts_from,
                       version_id, actor_id, reason, *, offence=None, scoring=None) -> int:
    """An approved incident's points. counts_from is HR's approval day; its expiry is dated
    from `happened`. offence and scoring (incident_points' result, plus "manager", "hr" and
    "on_probation" as chosen) let a re-score work it out again. Returns the line's id."""
    q = quarters(points)
    day, counts = _day(happened, "the day it happened"), _day(counts_from, "the day it counts from")
    if q < 0:
        raise ValueError("An incident's points can't be below 0.")
    if counts < day:
        raise ValueError("An incident can't count before it happened.")
    if not track:
        raise ValueError("Choose a point total.")
    return _insert(db, person_id, "incident", str(track), q, day, counts, actor_id, reason,
                   incident_id=incident_id, offence=offence, version_id=version_id,
                   scoring=_scoring(scoring))


def _targets(db, person_id, target_id=None, incident_id=None, *, bases_only=True) -> list[dict]:
    if target_id is not None:
        found = [ln for ln in lines_of(db, person_id) if ln["id"] == int(target_id)]
    elif incident_id is not None:
        found = [ln for ln in lines_of(db, person_id) if ln["incident_id"] == str(incident_id)
                 and ln["kind"] == "incident"]
    else:
        found = []
    if bases_only:
        found = [ln for ln in found if _is_base(ln)]
    if not found:
        raise ValueError("That line isn't in their points.")
    return found


def adjust(db, person_id, track, points, starts, actor_id, reason, *, ends=None, target_id=None,
           incident_id=None, version_id=None) -> int:
    """HR adds (or, minus, takes off) points from `starts`, with an optional end day. Pointing at
    an incident (target_id, or incident_id), it changes that incident's points from `starts`
    instead: the original stays visible, and the change expires with it. Returns the line's id."""
    _need_reason(reason)
    q = quarters(points)
    day = _day(starts, "the start day")
    if target_id is not None or incident_id is not None:
        [target] = _targets(db, person_id, target_id, incident_id)[:1]
        if q == 0 and version_id is None:
            raise ValueError("Give the points to add or take off.")
        return _insert(db, person_id, "adjustment", target["track"], q, target["happened"], day,
                       actor_id, reason, target_id=target["id"], incident_id=target["incident_id"],
                       offence=target["offence"], version_id=version_id)
    end = as_date(ends)
    if q == 0:
        raise ValueError("Give the points to add or take off.")
    if abs(q) > policy.MAX_POINTS * 4:
        raise ValueError(f"At most {policy.MAX_POINTS} points at a time.")
    if end is not None and end <= day:
        raise ValueError("The end day must be after the start day.")
    if not track:
        raise ValueError("Choose a point total.")
    if version_id is None and q > 0 and end is None:
        v = policy.current(db, day)     # added points expire like an incident on their start day
        version_id = v["id"] if v else None
    return _insert(db, person_id, "adjustment", str(track), q, day, day, actor_id, reason,
                   ends=end, version_id=version_id)


def set_ends(db, person_id, ends, actor_id, reason, *, target_id=None, incident_id=None,
             writeup=False, starts=None) -> list[int]:
    """Change the day an incident (or added points) stops counting, from `starts` (today by
    default). writeup=True records its write-up's end date, which counts under the "writeup" and
    "first" expiry rules; otherwise it's HR's override, which always wins. Returns the line ids."""
    if not writeup:
        _need_reason(reason)
    end = _day(ends, "the end day")
    day = as_date(starts) or _today(db)
    found = []
    for t in _targets(db, person_id, target_id, incident_id):
        found.append(_insert(db, person_id, "adjustment", t["track"], 0, t["happened"], day,
                             actor_id, reason, ends=end, end_kind="writeup" if writeup else "override",
                             target_id=t["id"], incident_id=t["incident_id"], offence=t["offence"],
                             commit=False))
    db.conn.commit()
    return found


def void(db, person_id, actor_id, reason, *, target_id=None, incident_id=None,
         starts=None) -> list[int]:
    """An incident (every line of it, with incident_id), an adjustment or a credit stops
    counting from `starts` (today by default). Returns the void lines' ids."""
    _need_reason(reason)
    day = as_date(starts) or _today(db)
    lines = lines_of(db, person_id)
    voided = {ln["target_id"] for ln in lines if ln["kind"] == "void"}
    if incident_id is not None:
        ids = {ln["id"] for ln in lines if ln["incident_id"] == str(incident_id) and ln["kind"] == "incident"}
        targets = [ln for ln in lines if ln["kind"] != "void" and (
            ln["id"] in ids or ln["target_id"] in ids)]
    else:
        targets = [ln for ln in lines if target_id is not None and ln["id"] == int(target_id)
                   and ln["kind"] != "void"]
    targets = [t for t in targets if t["id"] not in voided]
    if not targets:
        raise ValueError("There's nothing left to void there.")
    found = [_insert(db, person_id, "void", t["track"], 0, t["happened"], day, actor_id, reason,
                     target_id=t["id"], incident_id=t["incident_id"], offence=t["offence"],
                     commit=False) for t in targets]
    db.conn.commit()
    return found


def credit(db, person_id, track, points, on, actor_id, reason, version_id=None) -> int:
    """Points earned back on a day (they take off the oldest points counting then)."""
    q = quarters(points)
    if q <= 0:
        raise ValueError("A credit is a number of points more than 0.")
    day = _day(on, "the day")
    return _insert(db, person_id, "credit", str(track), -q, day, day, actor_id, reason,
                   version_id=version_id)


# ---------------------------------------------------------------- credits

def _next_credit_day(c: Calc, track: str, cfg: dict) -> date | None:
    """The first day a credit could be due after the last incident and credit (ignoring how many
    points there are)."""
    incidents = c.incident_days(track, c.known)
    if not incidents:
        return None
    credits = sorted(m["counts_from"] for m in c.minus if m["kind"] == "credit"
                     and m["track"] == track and not c.voided(m, c.known))
    day = add_span(incidents[-1], cfg["clean"])
    if credits:
        day = max(day, add_span(credits[-1], cfg["clean"]))
    most = int(cfg.get("max_per_year") or 1)
    if len(credits) >= most:
        day = max(day, add_span(credits[-most], YEAR))
    return day


def credits_due(lines: list[dict], bodies: dict, until: date, config_at, first_on=None) -> list[dict]:
    """Credit lines earned on or before `until` that aren't in the lines yet, oldest first.
    config_at(day) -> (version id, its credits settings) in force that day, or (None, None);
    first_on(day) -> the first day from `day` on when credits are on (None: never)."""
    new: list[dict] = []
    next_id = -1
    for track in sorted({ln["track"] for ln in lines}):
        floor = None
        for _ in range(500):
            vid, cfg = config_at(until)
            if not cfg:
                break
            c = Calc(lines + new, bodies, until)
            day = _next_credit_day(c, track, cfg)
            if day is None:
                break
            if floor and day < floor:
                day = floor
            if day > until:
                break
            vid, cfg = config_at(day)
            if not (cfg and cfg.get("on") and (not cfg.get("tracks") or track in cfg["tracks"])):
                later = first_on(day + timedelta(days=1)) if first_on else None
                if later is None or later > until:
                    break
                floor = later
                continue
            have = Calc(lines + new, bodies, day).totals(day).get(track, 0)
            if have <= 0:
                # Nothing to take off: the next chance is when points start counting again.
                later = [ln["counts_from"] for ln in c.bases if ln["track"] == track
                         and ln["counts_from"] > day]
                if not later:
                    break
                floor = min(later)
                continue
            q = min(quarters(cfg["points"]), have)
            new.append({"id": next_id, "kind": "credit", "track": track, "q": -q, "happened": day,
                        "counts_from": day, "ends": None, "end_kind": None, "target_id": None,
                        "incident_id": None, "offence": None, "version_id": vid, "scoring": {},
                        "reason": f"Clean for {policy.span_text(cfg['clean'])}: "
                                  f"{points_text(q)} back."})
            next_id -= 1
            floor = None
    return sorted(new, key=lambda ln: ln["counts_from"])


def _credit_settings(db):
    """config_at and first_on for credits_due, from the published versions."""
    rows = [(as_date(v["effective"]), v) for v in policy.versions(db)
            if v["effective"] and v["state"] != "withdrawn" and v["state"] != "draft"]
    rows.sort(key=lambda r: r[0])
    bodies = {v["id"]: policy.get(db, v["id"])["body"] for _, v in rows}

    def config_at(day):
        found = [v for eff, v in rows if eff <= day]
        if not found:
            return None, None
        v = found[-1]
        return v["id"], bodies[v["id"]].get("credits")

    def first_on(day):
        vid, cfg = config_at(day)
        if cfg and cfg.get("on"):
            return day
        for eff, v in rows:
            if eff > day and (bodies[v["id"]].get("credits") or {}).get("on"):
                return eff
        return None
    return config_at, first_on


def award_credits(db, until=None) -> int:
    """The nightly job: credits earned by `until` (today), for everyone with points. Each is a
    credit line, and an audit line. Returns how many."""
    day = as_date(until) or _today(db)
    config_at, first_on = _credit_settings(db)
    if not first_on(date(2000, 1, 1)):
        return 0          # no version has credits on
    count = 0
    for (pid,) in db.conn.execute("SELECT DISTINCT person_id FROM point_entries").fetchall():
        lines = lines_of(db, pid)
        for c in credits_due(lines, bodies_for(db, lines), day, config_at, first_on):
            credit(db, pid, c["track"], as_points(-c["q"]), c["counts_from"], None, c["reason"],
                   version_id=c["version_id"])
            audit.record(db, "Points earned back", target_id=pid, target=_name(db, pid),
                         detail=f"{points_text(-c['q'])}, {c['counts_from'].isoformat()}")
            count += 1
    return count


def _name(db, person_id) -> str:
    p = db.person(person_id)
    return p["name"] if p else str(person_id)


# ---------------------------------------------------------------- preview, simulator, re-score

def preview(db, person_id, body: dict, offence_key, ctx: dict, *, manager_points=None,
            hr_points=None, as_of=None, active_step=None) -> dict:
    """What recording this incident would do, for the manager's form: {before, after (totals),
    points (incident_points), step (required_step), text, e.g. "Maria: 4 → 6 points. Reaches
    Written warning. A write-up is needed."}."""
    day = as_date(as_of) or _today(db)
    ctx = dict(ctx or {})
    ctx.setdefault("happened", day)
    scored = incident_points(body, offence_key, ctx, past_incidents(db, person_id, day),
                             manager_points, hr_points)
    lines = lines_of(db, person_id)
    bodies = bodies_for(db, lines)
    before = Calc(lines, bodies, day).totals(day)
    track = scored["track"]
    after = dict(before)
    if track and not scored["errors"]:
        bodies["preview"] = body
        extra = {"id": 0, "kind": "incident", "track": track, "q": quarters(scored["final"]),
                 "happened": as_date(ctx["happened"]) or day, "counts_from": day, "ends": None,
                 "end_kind": None, "target_id": None, "incident_id": None, "offence": offence_key,
                 "version_id": "preview", "scoring": {}}
        after = Calc(lines + [extra], bodies, day).totals(day)
    step = required_step(body, {**ctx, "repeat": scored["repeat"]}, _floats(after), offence_key,
                         active_step)
    first = _name(db, person_id).split(" ")[0]
    text = f"{first}: {fmt(before.get(track, 0))} → {fmt(after.get(track, 0))} points."
    if step["step"]:
        text += f" Reaches {policy.step_name(body, step['step'])}."
    o = policy.offence(body, offence_key)
    if step["step"] or (o and o.get("writeup_required")):
        text += " A write-up is needed."
    return {"before": _floats(before), "after": _floats(after), "points": scored, "step": step,
            "text": text}


def _sim_events(events) -> tuple[list[dict], list[str]]:
    found, problems = [], []
    for n, ev in enumerate(events or [], 1):
        if not isinstance(ev, dict):
            continue
        kind = ev.get("kind") or "incident"
        day = as_date(ev.get("date"))
        if day is None:
            problems.append(f"Line {n} needs a date.")
            continue
        approved = as_date(ev.get("approved")) or day
        if approved < day:
            problems.append(f"Line {n}: it can't be approved before it happened.")
            continue
        found.append({**ev, "kind": kind, "date": day, "approved": approved, "n": n})
    return found, problems


def simulate(body: dict, events, start, *, department=None, until=None) -> dict:
    """The simulator: made-up incidents for a test person who started on `start`, and what the
    point system makes of them. Each step is treated as issued the day its incident counts, and
    covers the points counting then (their write-up end date). events: [{"date", "offence",
    "protected", "manager_points", "hr_points", "approved"}], plus {"kind": "adjust", "date",
    "points", "ends", "track"} and {"kind": "probation", "policy", "date", "ends"}.
    Returns {rows: [...] by date, totals, until, problems}: a row is an incident, an adjustment,
    a drop (points stopped counting), a credit, a step ending, or probation starting or ending."""
    problems = policy.validate(body)
    start_day = as_date(start)
    if start_day is None:
        problems.append("Choose the test person's start date.")
    if len(events or []) > 200:
        problems.append("At most 200 made-up incidents.")
    evs, more = _sim_events(events)
    problems += more
    if problems:
        return {"rows": [], "totals": {}, "until": None, "problems": problems}
    body = policy.normalize(body)
    bodies = {"sim": body}
    windows = []      # (probation key, from, to)
    intro = policy.intro_policy(body, [department] if department else [])
    if intro:
        days = policy.probation_days(body, intro["key"], [department] if department else [])
        windows.append((intro["key"], start_day, start_day + timedelta(days=days)))
    for ev in evs:
        if ev["kind"] == "probation":
            p = policy.probation_policy(body, ev.get("policy"))
            if not p:
                problems.append(f"Line {ev['n']}: choose a probation policy.")
                continue
            ends = as_date(ev.get("ends")) or ev["date"] + timedelta(days=p["days"])
            windows.append((p["key"], ev["date"], ends))
    last = max([e["approved"] for e in evs] + [start_day])
    horizon = as_date(until) or add_span(last, {"months": 24})
    rows, lines, issued = [], [], []      # issued: (step key, from, to)
    config = lambda d: ("sim", body["credits"]) if body["credits"]["on"] else (None, None)  # noqa: E731
    first_on = lambda d: d if body["credits"]["on"] else None   # noqa: E731

    def add_credits(through: date):
        for c in credits_due(lines, bodies, through, config, first_on):
            c["id"] = len(lines) + 1
            lines.append(c)
            total = Calc(lines, bodies, c["counts_from"]).totals(c["counts_from"]).get(c["track"], 0)
            rows.append({"date": c["counts_from"], "kind": "credit", "track": c["track"],
                         "points": as_points(c["q"]), "total": as_points(total), "text": c["reason"]})

    def active_on(day: date):
        live = [s for s in issued if s[1] <= day and (s[2] is None or day < s[2])]
        return max(live, key=lambda s: policy.rank(body, s[0]))[0] if live else None

    def probation_on(day: date):
        for key, frm, to in windows:
            if frm <= day < to:
                return key
        return None

    for key, frm, to in windows:
        name = policy.probation_policy(body, key)["name"]
        rows.append({"date": frm, "kind": "probation", "text": f"{name} starts"})
        rows.append({"date": to - timedelta(days=1), "kind": "probation",
                     "text": f"Last day of {name}"})
    for ev in sorted(evs, key=lambda e: (e["approved"], e["date"], e["n"])):
        if ev["kind"] == "probation":
            continue
        add_credits(ev["approved"] - timedelta(days=1))
        day = ev["approved"]
        before = Calc(lines, bodies, day).totals(day)
        if ev["kind"] == "adjust":
            try:
                q = quarters(ev.get("points"))
            except ValueError:
                problems.append(f"Line {ev['n']}: give the points to add or take off.")
                continue
            track = ev.get("track") or body["tracks"][0]["key"]
            lines.append({"id": len(lines) + 1, "kind": "adjustment", "track": track, "q": q,
                          "happened": ev["date"], "counts_from": ev["date"],
                          "ends": as_date(ev.get("ends")), "end_kind": None, "target_id": None,
                          "incident_id": None, "offence": None,
                          "version_id": "sim" if q > 0 and not ev.get("ends") else None, "scoring": {}})
            after = Calc(lines, bodies, day).totals(day)
            rows.append({"date": ev["date"], "kind": "adjust", "track": track, "points": as_points(q),
                         "total_before": as_points(before.get(track, 0)),
                         "total": as_points(after.get(track, 0))})
            continue
        prob = probation_on(ev["date"])
        history = [{"offence": ln["offence"], "happened": ln["happened"],
                    "points": as_points(ln["q"]), "protected": ln["scoring"].get("protected")}
                   for ln in lines if ln["kind"] == "incident"]
        ctx = {"happened": ev["date"], "on_probation": prob, "protected": ev.get("protected")}
        scored = incident_points(body, ev.get("offence"), ctx, history, ev.get("manager_points"),
                                 ev.get("hr_points"))
        o = policy.offence(body, ev.get("offence"))
        row = {"date": ev["date"], "approved": ev["approved"], "kind": "incident",
               "offence": ev.get("offence"), "name": o["name"] if o else "", "track": scored["track"],
               "default": scored["default"], "points": scored["final"], "repeat": scored["repeat"],
               "multiplier": scored["probation_multiplier"], "protected": scored["protected"],
               "on_probation": prob, "errors": scored["errors"], "notes": scored["notes"]}
        if scored["errors"]:
            rows.append(row)
            continue
        lines.append({"id": len(lines) + 1, "kind": "incident", "track": scored["track"],
                      "q": quarters(scored["final"]), "happened": ev["date"], "counts_from": day,
                      "ends": None, "end_kind": None, "target_id": None, "incident_id": None,
                      "offence": o["key"], "version_id": "sim",
                      "scoring": {"protected": scored["protected"]}})
        c = Calc(lines, bodies, day)
        after = c.totals(day)
        active = active_on(day)
        need = required_step(body, {**ctx, "repeat": scored["repeat"]}, _floats(after), o["key"], active)
        row.update(total_before=as_points(before.get(scored["track"], 0)),
                   total=as_points(after.get(scored["track"], 0)), active_step=active,
                   step=need["step"], step_name=policy.step_name(body, need["step"]) if need["step"] else None,
                   reason=need["reason"])
        rows.append(row)
        if need["step"]:
            ends = policy.step_end(body, need["step"], day)
            issued.append((need["step"], day, ends))
            if ends:
                rows.append({"date": ends, "kind": "step_ends", "step": need["step"],
                             "text": f"{policy.step_name(body, need['step'])} ends"})
                for lot in c.bases:     # the write-up covers the points counting now
                    if c.alive(lot, day) and not any(ch["end_kind"] == "writeup"
                                                     for ch in c.changes.get(lot["id"], ())):
                        lines.append({"id": len(lines) + 1, "kind": "adjustment", "track": lot["track"],
                                      "q": 0, "happened": lot["happened"], "counts_from": day,
                                      "ends": ends, "end_kind": "writeup", "target_id": lot["id"],
                                      "incident_id": None, "offence": lot["offence"],
                                      "version_id": None, "scoring": {}})
    add_credits(horizon)
    # Points that stop counting (expiry, the clean reset), each day it happens.
    final = Calc(lines, bodies, horizon)
    days = sorted({e for ln in final.bases if (e := final.end(ln, horizon)) and e <= horizon}
                  | {ln["ends"] for ln in final.minus if ln["ends"] and ln["ends"] <= horizon})
    for d in days:
        known = Calc(lines, bodies, d - timedelta(days=1))
        before, after = known.totals(d - timedelta(days=1)), known.totals(d)
        for track in sorted(set(before) | set(after)):
            change = after.get(track, 0) - before.get(track, 0)
            if change:
                rows.append({"date": d, "kind": "drop" if change < 0 else "restore", "track": track,
                             "points": as_points(change), "total": as_points(after.get(track, 0))})
    order = {"probation": 0, "drop": 1, "restore": 1, "step_ends": 2, "credit": 3, "adjust": 4,
             "incident": 5}
    rows = [r for r in rows if r["date"] <= horizon]
    rows.sort(key=lambda r: (r["date"], order[r["kind"]]))
    for r in rows:
        for k in ("date", "approved"):
            if isinstance(r.get(k), date):
                r[k] = r[k].isoformat()
    totals = {t["key"]: 0 for t in body["tracks"]}
    totals.update(final.totals(horizon))
    return {"rows": rows, "totals": _floats(totals), "until": horizon.isoformat(),
            "problems": problems}


def _rescore_person(db, person_id, version: dict, day: date) -> dict:
    """What re-scoring one person's counting incidents under `version` would do."""
    body = version["body"]
    lines = lines_of(db, person_id)
    bodies = bodies_for(db, lines)
    bodies[version["id"]] = body
    c = Calc(lines, bodies, day)
    before = c.totals(day)
    changes, extra, history = [], [], []
    for ln in c.bases:
        if ln["kind"] != "incident":
            continue
        scoring = ln["scoring"] or {}
        for ch in c._changes(ln, day):
            if ch["version_id"] is not None and ch["scoring"]:
                scoring = ch["scoring"]     # the latest re-score's choices
        now_q = c.amount(ln, day)
        counting = c.alive(ln, day)
        if not counting or not ln["offence"]:
            if not c.voided(ln, day):
                history.append({"offence": ln["offence"], "happened": ln["happened"],
                                "points": as_points(now_q), "protected": scoring.get("protected")})
            continue
        known = policy.offence(body, ln["offence"]) or policy.offence(bodies.get(ln["version_id"]) or {},
                                                                     ln["offence"])
        item = {"entry_id": ln["id"], "incident_id": ln["incident_id"], "offence": ln["offence"],
                "name": known["name"] if known else ln["offence"],
                "happened": ln["happened"].isoformat(), "old": as_points(now_q),
                "old_ends": (e := c.end(ln, day)) and e.isoformat(), "notes": []}
        o = policy.offence(body, ln["offence"])
        if not o:
            item.update(new=item["old"], new_ends=item["old_ends"], change=False,
                        notes=["This offence isn't in the new version, so it's kept as it is."])
            changes.append(item)
            history.append({"offence": ln["offence"], "happened": ln["happened"],
                            "points": as_points(now_q), "protected": scoring.get("protected")})
            continue
        prob = scoring.get("on_probation")
        if prob and not policy.probation_policy(body, prob):
            item["notes"].append("Their probation policy isn't in the new version: scored as not on probation.")
            prob = None
        ctx = {"happened": ln["happened"], "on_probation": prob,
               "protected": "other" if scoring.get("protected") else None}
        manager = scoring.get("manager")
        result = incident_points(body, o["key"], ctx, history, manager, scoring.get("hr"))
        if manager is not None and any("Managers may choose" in e for e in result["errors"]):
            item["notes"].append("The manager's points are outside the new range, so the new "
                                 "default is used.")
            manager = None
            result = incident_points(body, o["key"], ctx, history, None, scoring.get("hr"))
        new_q = quarters(result["final"])
        line = {"id": -len(extra) - 1, "kind": "adjustment", "track": ln["track"], "q": new_q - now_q,
                "happened": ln["happened"], "counts_from": day, "ends": None, "end_kind": None,
                "target_id": ln["id"], "incident_id": ln["incident_id"], "offence": ln["offence"],
                "version_id": version["id"],
                "scoring": _scoring(result, {"manager": manager, "hr": scoring.get("hr"),
                                             "on_probation": prob})}
        trial = Calc(lines + extra + [line], bodies, day)
        new_end = trial.end(ln, day)
        item.update(new=as_points(new_q), new_ends=new_end and new_end.isoformat())
        item["change"] = new_q != now_q or item["new_ends"] != item["old_ends"]
        if item["change"]:
            extra.append(line)
            item["line"] = line
        changes.append(item)
        history.append({"offence": ln["offence"], "happened": ln["happened"],
                        "points": as_points(new_q), "protected": scoring.get("protected")})
    after = Calc(lines + extra, bodies, day).totals(day)
    return {"person_id": str(person_id), "name": _name(db, person_id), "changes": changes,
            "before": _floats(before), "after": _floats(after)}


def rescore_preview(db, person_ids, version_id, *, on=None) -> dict:
    """What re-scoring these people's counting incidents under a version would change (nothing is
    written): {version, people: [{person_id, name, changes: [{entry_id, offence, happened, old,
    new, old_ends, new_ends, change, notes}], before, after}], changed}. Expired and voided
    incidents stay as they are. Raises ValueError."""
    day = as_date(on) or _today(db)
    v = policy.get(db, version_id)
    now = policy.current(db, day)
    if not v or not now or now["id"] != v["id"]:
        raise ValueError("Only the version in use today can re-score people.")
    people = [_rescore_person(db, pid, v, day) for pid in dict.fromkeys(map(str, person_ids))]
    for p in people:
        for ch in p["changes"]:
            ch.pop("line", None)
    return {"version": {"id": v["id"], "name": v["name"]}, "people": people,
            "changed": sum(1 for p in people for ch in p["changes"] if ch["change"])}


def rescore(db, person_ids, version_id, actor_id, reason, *, on=None) -> dict:
    """Re-score these people's counting incidents under the version in use: an adjustment line
    for each incident that changes (its points and expiry then follow the new version). Returns
    the preview of what was done."""
    _need_reason(reason)
    day = as_date(on) or _today(db)
    v = policy.get(db, version_id)
    now = policy.current(db, day)
    if not v or not now or now["id"] != v["id"]:
        raise ValueError("Only the version in use today can re-score people.")
    done = []
    for pid in dict.fromkeys(map(str, person_ids)):
        p = _rescore_person(db, pid, v, day)
        for ch in p["changes"]:
            line = ch.pop("line", None)
            if line:
                _insert(db, pid, "adjustment", line["track"], line["q"], line["happened"], day,
                        actor_id, f"Re-scored under {v['name']}. {reason}", target_id=line["target_id"],
                        incident_id=line["incident_id"], offence=line["offence"],
                        version_id=v["id"], scoring=line["scoring"], commit=False)
        done.append(p)
    db.conn.commit()
    return {"version": {"id": v["id"], "name": v["name"]}, "people": done,
            "changed": sum(1 for p in done for ch in p["changes"] if ch["change"])}


def scored_elsewhere(db, version_id, on=None) -> list[dict]:
    """People with counting incidents scored under another version than this one: who a
    re-score could change. [{id, name, incidents}]."""
    day = as_date(on) or _today(db)
    found = []
    for (pid,) in db.conn.execute("SELECT DISTINCT person_id FROM point_entries "
                                  "WHERE kind = 'incident'").fetchall():
        c = calc(db, pid, day)
        n = sum(1 for ln in c.bases if ln["kind"] == "incident" and c.alive(ln, day)
                and c.version(ln, day) != int(version_id))
        if n:
            found.append({"id": pid, "name": _name(db, pid), "incidents": n})
    return sorted(found, key=lambda p: p["name"].casefold())


# ---------------------------------------------------------------- joining the HR portal

async def nightly(svc) -> None:
    award_credits(svc.db)


def _part(svc) -> None:
    svc.on_start.append(lambda s: setup(s.db))
    svc.daily_jobs.append(nightly)


service.PARTS.append(_part)
