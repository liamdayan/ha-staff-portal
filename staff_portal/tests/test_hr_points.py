"""The point system's arithmetic (hr/points.py): the ledger, totals as of any day, the expiry
rules at their boundaries, voids, adjustments, credits, repeats, probation, the step an incident
calls for, the next drop, the simulator and re-scoring.

Every date is a calendar day, so the boundaries are tested on and around the days the clocks
change. Nothing here talks to a real Connecteam, Google, TT Hotel or Home Assistant.
"""

import copy
import sqlite3
import time
from datetime import date

import pytest

import hr_helpers  # noqa: F401  (points Connecteam at a closed local port)
from hr import audit, crypto, points, policy
from hr.hrdb import HRDatabase
from hr.policy import add_span, quarters

D = date.fromisoformat


@pytest.fixture
def db(tmp_path):
    ring = crypto.Keyring(str(tmp_path / "hr"))
    assert ring.load(create=True) == "ready"
    crypto.use(ring)
    database = HRDatabase(str(tmp_path / "hr"))
    policy.setup(database)
    points.setup(database)
    for pid, name in (("1", "Maria Lopez"), ("2", "Sam Reed"), ("9", "Hana Ito")):
        database.conn.execute("INSERT INTO people (id, name, added_at) VALUES (?, ?, ?)",
                              (pid, name, time.time()))
    database.conn.commit()
    yield database
    database.close()
    crypto.use(None)


def body(**changes) -> dict:
    """The sample, changed: body(expiry={"rule": "rolling", ...}) or body(**{"credits.on": True})."""
    b = copy.deepcopy(policy.SAMPLE)
    for key, value in changes.items():
        part, _, field = key.partition(".")
        if field:
            b[part][field] = value
        else:
            b[part] = value
    b = policy.normalize(b)
    assert policy.validate(b) == [], policy.validate(b)
    return b


def publish(db, b: dict, effective: str) -> int:
    vid = policy.save_draft(db, b, "hr")
    policy.publish(db, vid, effective, "hr", on=D(effective))
    return vid


def incident(db, vid, happened, pts, *, person="1", counts_from=None, offence="late", track="discipline",
             incident_id=None, scoring=None):
    return points.add_incident_entry(db, person, incident_id or f"i-{happened}-{pts}", track, pts, happened,
                                     counts_from or happened, vid, "m1", "What happened",
                                     offence=offence, scoring=scoring)


def total(db, day, person="1", track="discipline") -> float:
    return points.totals(db, person, day).get(track, 0)


# ---------------------------------------------------------------- dates and quarter points

def test_spans_add_by_calendar_day_and_month_end():
    assert add_span(D("2026-01-31"), {"months": 12}) == D("2027-01-31")
    assert add_span(D("2026-01-31"), {"months": 1}) == D("2026-02-28")
    assert add_span(D("2028-01-31"), {"months": 1}) == D("2028-02-29")       # a leap year
    assert add_span(D("2028-02-29"), {"months": 12}) == D("2029-02-28")
    assert add_span(D("2026-03-31"), {"months": 1}) == D("2026-04-30")
    assert add_span(D("2026-12-15"), {"months": 3}) == D("2027-03-15")
    assert add_span(D("2026-01-01"), {"days": 90}) == D("2026-04-01")
    # The days the clocks change are ordinary days: no hour is ever lost or gained.
    assert add_span(D("2026-03-07"), {"days": 1}) == D("2026-03-08")
    assert add_span(D("2026-10-31"), {"days": 1}) == D("2026-11-01")
    assert add_span(D("2026-03-08"), {"days": 238}) == D("2026-11-01")
    assert add_span(D("2026-01-01"), None) is None


def test_quarter_points_stay_exact():
    assert quarters(1.5) == 6 and quarters("0.25") == 1 and quarters(3) == 12 and quarters("-1") == -4
    for bad in (0.1, "0.3", "abc", None, True, float("nan"), "1e9"):
        with pytest.raises(ValueError):
            quarters(bad)
    assert policy.fmt(6) == "1.5" and policy.fmt(8) == "2" and policy.fmt(1) == "0.25" and policy.fmt(-3) == "-0.75"
    assert policy.points_text(4) == "1 point" and policy.points_text(2) == "0.5 points"
    # Rounding a multiplied value: to the nearest allowed point, halves up.
    from fractions import Fraction
    assert policy.round_to(Fraction(3, 2) * 1, 1) == 2          # 0.25 x 1.5 = 0.375 -> 0.5
    assert policy.round_to(Fraction(3, 2) * 6, 2) == 10         # 1.5 x 1.5 = 2.25 -> 2.5 in halves
    assert policy.round_to(Fraction(3, 2) * 4, 4) == 8          # 1 x 1.5 -> 2 in whole points
    assert policy.round_to(Fraction(5, 4) * 4, 1) == 5          # 1 x 1.25 = 1.25 exactly


def test_many_quarter_points_add_up_exactly(db):
    vid = publish(db, body(), "2026-01-01")
    for n in range(40):
        incident(db, vid, "2026-02-01", 0.25, incident_id=f"q{n}", offence="late_short")
    assert total(db, "2026-02-01") == 10.0
    for n in range(3):
        points.adjust(db, "1", "discipline", "-0.75", "2026-02-02", "hr", "Correction")
    assert total(db, "2026-02-02") == 7.75


# ---------------------------------------------------------------- the ledger

def test_the_ledger_is_append_only_and_reasons_are_sealed(db):
    vid = publish(db, body(), "2026-01-01")
    eid = incident(db, vid, "2026-02-01", 1)
    raw = db.conn.execute("SELECT reason FROM point_entries WHERE id = ?", (eid,)).fetchone()[0]
    assert "What happened" not in raw
    assert points.history(db, "1", "2026-02-01")[0]["reason"] == "What happened"
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute("UPDATE point_entries SET quarters = 0")
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute("DELETE FROM point_entries")
    db.conn.rollback()
    assert total(db, "2026-02-01") == 1.0


def test_writing_lines_refuses_nonsense(db):
    vid = publish(db, body(), "2026-01-01")
    with pytest.raises(ValueError, match="before it happened"):
        incident(db, vid, "2026-02-10", 1, counts_from="2026-02-01")
    with pytest.raises(ValueError, match="below 0"):
        incident(db, vid, "2026-02-10", -1)
    with pytest.raises(ValueError):
        incident(db, vid, "2026-02-10", 0.1)
    with pytest.raises(ValueError, match="person"):
        incident(db, vid, "2026-02-10", 1, person="404")
    with pytest.raises(ValueError, match="reason"):
        points.adjust(db, "1", "discipline", 1, "2026-02-01", "hr", "  ")
    with pytest.raises(ValueError, match="after the start"):
        points.adjust(db, "1", "discipline", 1, "2026-02-01", "hr", "Why", ends="2026-02-01")
    with pytest.raises(ValueError, match="points to add"):
        points.adjust(db, "1", "discipline", 0, "2026-02-01", "hr", "Why")
    with pytest.raises(ValueError, match="isn't in their points"):
        points.adjust(db, "1", None, 1, "2026-02-01", "hr", "Why", incident_id="nope")
    with pytest.raises(ValueError, match="nothing left to void"):
        points.void(db, "1", "hr", "Why", incident_id="nope")
    with pytest.raises(ValueError, match="more than 0"):
        points.credit(db, "1", "discipline", 0, "2026-02-01", None, "x")
    assert db.conn.execute("SELECT COUNT(*) FROM point_entries").fetchone()[0] == 0


# ---------------------------------------------------------------- when points stop counting

def test_rolling_window_ends_on_the_same_day_a_year_later(db):
    vid = publish(db, body(**{"expiry.rule": "rolling"}), "2025-01-01")
    incident(db, vid, "2026-01-31", 2)
    incident(db, vid, "2028-02-29", 1, incident_id="leap")
    assert total(db, "2026-01-30") == 0.0          # not yet
    assert total(db, "2026-01-31") == 2.0          # counts the day it happened (approved that day)
    assert total(db, "2027-01-30") == 2.0          # the last day it counts
    assert total(db, "2027-01-31") == 0.0          # the first day it doesn't
    assert total(db, "2029-02-27") == 1.0 and total(db, "2029-02-28") == 0.0
    h = points.history(db, "1", "2027-01-31")
    assert h[0]["state"] == "expired" and h[0]["ends"] == "2027-01-31" and h[0]["counting"] == 0.0
    assert [r["state"] for r in points.history(db, "1", "2027-01-31", include_expired=False)] == []


def test_points_count_from_approval_but_expire_from_the_day_it_happened(db):
    vid = publish(db, body(**{"expiry.rule": "rolling"}), "2025-01-01")
    incident(db, vid, "2026-01-10", 3, counts_from="2026-02-01")
    assert total(db, "2026-01-31") == 0.0 and points.history(db, "1", "2026-01-31") == []
    assert total(db, "2026-02-01") == 3.0
    assert total(db, "2027-01-09") == 3.0 and total(db, "2027-01-10") == 0.0


def test_write_up_end_date_rule(db):
    vid = publish(db, body(**{"expiry.rule": "writeup"}), "2025-01-01")
    incident(db, vid, "2026-01-10", 3, incident_id="a")
    assert total(db, "2030-01-01") == 3.0          # no write-up yet: it keeps counting
    points.set_ends(db, "1", "2026-10-10", "hr", "", incident_id="a", writeup=True, starts="2026-01-20")
    assert total(db, "2026-01-19") == 3.0
    assert total(db, "2026-10-09") == 3.0 and total(db, "2026-10-10") == 0.0
    # As of a day before the write-up was recorded, the end wasn't known yet.
    assert points.history(db, "1", "2026-01-19")[0]["ends"] is None


def test_whichever_comes_first(db):
    vid = publish(db, body(), "2025-01-01")          # the sample: first of 12 months or the write-up
    incident(db, vid, "2026-01-10", 3, incident_id="a")
    incident(db, vid, "2026-01-10", 2, incident_id="b")
    points.set_ends(db, "1", "2026-07-10", "hr", "", incident_id="a", writeup=True, starts="2026-01-10")
    points.set_ends(db, "1", "2027-07-10", "hr", "", incident_id="b", writeup=True, starts="2026-01-10")
    assert total(db, "2026-07-09") == 5.0
    assert total(db, "2026-07-10") == 2.0          # a: its write-up ended first
    assert total(db, "2027-01-09") == 2.0
    assert total(db, "2027-01-10") == 0.0          # b: 12 months came first


def test_clean_period_reset(db):
    vid = publish(db, body(**{"expiry.rule": "rolling", "expiry.rolling": {"months": 12},
                              "expiry.clean_reset": {"months": 6}}), "2025-01-01")
    incident(db, vid, "2026-01-01", 2, incident_id="a")
    incident(db, vid, "2026-03-01", 1, incident_id="b")      # restarts the clock
    incident(db, vid, "2026-03-01", 0, incident_id="z")      # 0 points: doesn't restart anything
    assert total(db, "2026-08-31") == 3.0
    assert total(db, "2026-09-01") == 0.0                    # 6 clean months after Mar 1
    incident(db, vid, "2026-10-01", 1, incident_id="c")      # after the reset: a fresh start
    assert total(db, "2026-10-01") == 1.0
    assert total(db, "2027-03-31") == 1.0 and total(db, "2027-04-01") == 0.0
    # Another point total keeps its own clock.
    b2 = body(**{"expiry.rule": "rolling", "expiry.clean_reset": {"months": 6},
                 "tracks": [{"key": "discipline", "name": "Discipline"}, {"key": "conduct", "name": "Conduct"}],
                 "ladders": {"discipline": [{"points": 3, "step": "coaching"}],
                             "conduct": [{"points": 3, "step": "coaching"}]}})
    vid2 = publish(db, b2, "2028-01-01")
    incident(db, vid2, "2028-02-01", 1, person="2", incident_id="d")
    incident(db, vid2, "2028-06-01", 1, person="2", incident_id="e", track="conduct")
    assert points.totals(db, "2", "2028-08-01") == {"discipline": 0.0, "conduct": 1.0}


def test_hr_override_wins_from_its_own_day(db):
    vid = publish(db, body(**{"expiry.clean_reset": {"months": 3}}), "2025-01-01")
    incident(db, vid, "2026-01-10", 2, incident_id="a")
    points.set_ends(db, "1", "2027-06-01", "hr", "Kept longer after the appeal", incident_id="a",
                    starts="2026-03-01")
    assert total(db, "2026-02-28") == 2.0
    assert total(db, "2026-04-10") == 2.0            # the clean reset (Apr 10) no longer applies
    assert total(db, "2027-05-31") == 2.0 and total(db, "2027-06-01") == 0.0
    points.set_ends(db, "1", "2026-05-01", "hr", "Shortened", incident_id="a", starts="2026-04-20")
    assert total(db, "2026-04-30") == 2.0 and total(db, "2026-05-01") == 0.0
    with pytest.raises(ValueError, match="reason"):
        points.set_ends(db, "1", "2026-05-01", "hr", "", incident_id="a")


# ---------------------------------------------------------------- voids and adjustments

def test_voids_count_from_their_day_and_take_the_incidents_changes_too(db):
    vid = publish(db, body(), "2025-01-01")
    incident(db, vid, "2026-01-10", 3, incident_id="a")
    points.adjust(db, "1", None, 1, "2026-01-20", "hr", "More", incident_id="a")
    assert total(db, "2026-01-20") == 4.0
    ids = points.void(db, "1", "hr", "Appeal upheld", incident_id="a", starts="2026-03-01")
    assert len(ids) == 2                                # the incident and the change to it
    assert total(db, "2026-02-28") == 4.0 and total(db, "2026-03-01") == 0.0
    states = {r["kind"]: r["state"] for r in points.history(db, "1", "2026-03-01")}
    assert states == {"incident": "voided", "adjustment": "voided", "void": "applied"}
    with pytest.raises(ValueError, match="nothing left"):
        points.void(db, "1", "hr", "Again", incident_id="a")


def test_adjustments_with_start_and_end_days(db):
    vid = publish(db, body(**{"expiry.rule": "rolling"}), "2025-01-01")
    points.adjust(db, "1", "discipline", 2, "2026-03-01", "hr", "Agreed", ends="2026-06-01")
    assert total(db, "2026-02-28") == 0.0 and total(db, "2026-03-01") == 2.0
    assert total(db, "2026-05-31") == 2.0 and total(db, "2026-06-01") == 0.0
    # Added points without an end day expire like an incident on their start day.
    points.adjust(db, "1", "discipline", 1, "2026-07-01", "hr", "Added")
    assert total(db, "2027-06-30") == 1.0 and total(db, "2027-07-01") == 0.0
    assert vid


def test_changing_one_incidents_points_keeps_the_original(db):
    vid = publish(db, body(), "2025-01-01")
    eid = incident(db, vid, "2026-01-10", 3, incident_id="a")
    points.adjust(db, "1", None, -2, "2026-02-01", "hr", "Lowered", target_id=eid)
    assert total(db, "2026-01-31") == 3.0 and total(db, "2026-02-01") == 1.0
    points.adjust(db, "1", None, -5, "2026-03-01", "hr", "Too far", incident_id="a")
    assert total(db, "2026-03-01") == 0.0               # never below 0
    h = points.history(db, "1", "2026-02-01")
    assert [(r["kind"], r["points"]) for r in h] == [("incident", 3.0), ("adjustment", -2.0)]
    assert h[0]["amount"] == 1.0 and h[1]["state"] == "counts" and h[1]["target_id"] == eid
    # The change expires with its incident.
    assert total(db, "2027-01-10") == 0.0
    assert points.history(db, "1", "2027-01-10")[1]["state"] == "expired"


def test_taking_points_off_takes_the_oldest_and_never_piles_up(db):
    vid = publish(db, body(**{"expiry.rule": "rolling"}), "2025-01-01")
    incident(db, vid, "2026-01-10", 1, incident_id="a")       # expires 2027-01-10
    incident(db, vid, "2026-03-10", 1, incident_id="b")       # expires 2027-03-10
    points.adjust(db, "1", "discipline", -1, "2026-04-01", "hr", "Good record")
    assert total(db, "2026-04-01") == 1.0
    assert total(db, "2027-01-10") == 1.0                    # a was the one taken off
    assert total(db, "2027-03-10") == 0.0
    incident(db, vid, "2027-04-01", 1, incident_id="c")
    assert total(db, "2027-04-01") == 1.0                    # nothing left over to take off c
    # Taking off more than there is: 0, and the rest is dropped.
    points.adjust(db, "2", "discipline", -5, "2026-01-01", "hr", "Fresh start")
    incident(db, vid, "2026-02-01", 2, person="2", incident_id="d")
    assert total(db, "2026-02-01", person="2") == 2.0
    # A removal with an end day gives the points back after it.
    points.adjust(db, "2", "discipline", -1, "2026-03-01", "hr", "For now", ends="2026-04-01")
    assert total(db, "2026-03-15", person="2") == 1.0 and total(db, "2026-04-01", person="2") == 2.0
    taken = [r["taken_off"] for r in points.history(db, "2", "2026-03-15") if r["kind"] == "adjustment"]
    assert taken == [0.0, 1.0]       # the first had nothing to take off on its day


def test_a_voided_credit_gives_the_points_back(db):
    vid = publish(db, body(), "2025-01-01")
    incident(db, vid, "2026-01-10", 2)
    cid = points.credit(db, "1", "discipline", 1, "2026-05-01", None, "Clean for 90 days")
    assert total(db, "2026-05-01") == 1.0
    points.void(db, "1", "hr", "Given by mistake", target_id=cid, starts="2026-05-10")
    assert total(db, "2026-05-09") == 1.0 and total(db, "2026-05-10") == 2.0


# ---------------------------------------------------------------- versions per incident

def test_each_incident_keeps_the_rules_it_was_scored_under(db):
    v1 = publish(db, body(**{"expiry.rule": "rolling", "expiry.rolling": {"months": 12}}), "2025-01-01")
    incident(db, v1, "2026-01-10", 2, incident_id="old")
    v2 = policy.save_draft(db, body(**{"expiry.rule": "rolling", "expiry.rolling": {"months": 6}}), "hr")
    policy.publish(db, v2, "2026-03-01", "hr", on=D("2026-02-01"))
    assert policy.current(db, "2026-02-28")["id"] == v1 and policy.current(db, "2026-03-01")["id"] == v2
    incident(db, v2, "2026-03-10", 1, incident_id="new")
    assert total(db, "2026-09-09") == 3.0
    assert total(db, "2026-09-10") == 2.0                     # new: 6 months
    assert total(db, "2027-01-09") == 2.0 and total(db, "2027-01-10") == 0.0   # old: still 12


# ---------------------------------------------------------------- one incident's points

def history_of(*items):
    return [{"offence": o, "happened": D(d), "points": p, **extra} for o, d, p, extra in items]


def test_incident_points_default_repeat_and_window_boundary():
    b = body()
    ctx = {"happened": D("2026-04-01")}
    r = points.incident_points(b, "late", ctx, [])
    assert (r["default"], r["final"], r["repeat"], r["errors"]) == (1.0, 1.0, 1, [])
    # Two earlier lates within 90 days: the third adds a point.
    two = history_of(("late", "2026-01-02", 1, {}), ("late", "2026-03-01", 1, {}))
    r = points.incident_points(b, "late", ctx, two)
    assert (r["final"], r["repeat"]) == (2.0, 3) and "3rd within 90 days" in r["notes"][0]
    # Exactly 90 days earlier is outside the window (Jan 1 + 90 days = Apr 1).
    edge = history_of(("late", "2026-01-01", 1, {}), ("late", "2026-03-01", 1, {}))
    assert points.incident_points(b, "late", ctx, edge)["repeat"] == 2
    # Voided, protected and 0-point incidents, other offences and later ones don't count.
    noise = history_of(("late", "2026-03-02", 1, {"voided": True}), ("late", "2026-03-03", 0, {}),
                       ("late", "2026-03-04", 1, {"protected": True}), ("conduct", "2026-03-05", 2, {}),
                       ("late", "2026-04-02", 1, {}))
    assert points.incident_points(b, "late", ctx, noise)["repeat"] == 1
    # The same day counts (two lates the same day are two lates).
    assert points.incident_points(b, "late", ctx, history_of(("late", "2026-04-01", 1, {})))["repeat"] == 2
    # No repeat rule: no count.
    assert points.incident_points(b, "conduct", ctx, two)["repeat"] is None


def test_manager_range_and_hr_any_value():
    b = body()
    ctx = {"happened": D("2026-04-01")}
    assert points.incident_points(b, "late", ctx, [], manager_points=1.5)["final"] == 1.5
    assert points.incident_points(b, "late", ctx, [], manager_points="0.5")["final"] == 0.5
    r = points.incident_points(b, "late", ctx, [], manager_points=2)
    assert r["errors"] == ["Managers may choose 0.5 to 1.5 points for Late 8 minutes or more."]
    assert r["final"] == 1.0 and r["range"] == [0.5, 1.5]
    assert "number" in points.incident_points(b, "late", ctx, [], manager_points="lots")["errors"][0]
    r = points.incident_points(b, "late", ctx, [], manager_points=0.3)
    assert r["errors"] and r["final"] == 1.0
    # HR can set anything from 0 up, and both the default and the final are kept.
    r = points.incident_points(b, "late", ctx, [], manager_points=1.5, hr_points=0)
    assert (r["default"], r["scored"], r["final"]) == (1.0, 1.5, 0.0)
    assert points.incident_points(b, "late", ctx, [], hr_points=7.25)["final"] == 7.25
    assert points.incident_points(b, "late", ctx, [], hr_points=-1)["errors"]
    # A repeat still adds its points on top of the manager's choice.
    two = history_of(("late", "2026-03-01", 1, {}), ("late", "2026-03-15", 1, {}))
    assert points.incident_points(b, "late", ctx, two, manager_points=0.5)["final"] == 1.5
    # Whole points only: half points are refused.
    whole = body(point_size=1, offences=[{**policy.SAMPLE["offences"][3], "points": 2, "min": 1, "max": 3}])
    assert points.incident_points(whole, "late_callout", ctx, [], manager_points=1.5)["errors"]
    assert points.incident_points(b, "nope", ctx, [])["errors"] == ["That offence isn't in this point system."]


def test_protected_absences_score_zero_and_never_lead_to_a_step():
    b = body()
    ctx = {"happened": D("2026-04-01"), "protected": "sick"}
    r = points.incident_points(b, "no_call", ctx, [], manager_points=3)
    assert (r["final"], r["protected"], r["default"]) == (0.0, True, 3.0)
    assert points.incident_points(b, "no_call", ctx, [], hr_points=3)["errors"] == ["A protected absence always scores 0."]
    assert points.incident_points(b, "no_call", {**ctx, "protected": "no"}, [])["final"] == 3.0
    assert "attendance" in points.incident_points(b, "conduct", ctx, [])["errors"][0]
    step = points.required_step(b, ctx, {"discipline": 20}, "no_call", None)
    assert step["step"] is None and "protected" in step["reason"]


def test_probation_multiplier_and_its_own_points():
    b = copy.deepcopy(policy.SAMPLE)
    b["probation"][0]["multiplier"] = 1.5
    b["probation"][0]["overrides"] = {"no_call": {"points": 5, "min": None, "max": None}}
    b = body(probation=b["probation"])
    ctx = {"happened": D("2026-04-01"), "on_probation": "introductory"}
    r = points.incident_points(b, "late", ctx, [])
    assert (r["final"], r["probation_multiplier"]) == (1.5, 1.5)
    assert points.incident_points(b, "late_short", ctx, [], manager_points=0.25)["final"] == 0.5   # 0.375 -> 0.5
    assert points.incident_points(b, "conduct", ctx, [], manager_points=3)["final"] == 4.5
    r = points.incident_points(b, "no_call", ctx, [])
    assert (r["final"], r["probation_multiplier"], r["range"]) == (5.0, 1.0, [5.0, 5.0])
    assert points.incident_points(b, "no_call", ctx, [], manager_points=3)["errors"]
    assert points.incident_points(b, "late", {**ctx, "on_probation": None}, [])["final"] == 1.0
    assert points.incident_points(b, "late", {**ctx, "on_probation": "gone"}, [])["errors"]


# ---------------------------------------------------------------- the step required

def test_ladder_steps_in_order_and_skipping():
    b = body()
    step = points.required_step(b, {}, {"discipline": 5.5}, "late", None)
    # In order: no active step, so the first step on the ladder, noting how far points reached.
    assert step["step"] == "coaching" and step["reached"] == "verbal"
    assert step["reason"] == ("Points 5.5 ≥ 5 → Verbal warning. Steps go in order, so the next "
                              "step is Coaching")
    step = points.required_step(b, {}, {"discipline": 5.5}, "late", "coaching")
    assert (step["step"], step["reason"]) == ("verbal", "Points 5.5 ≥ 5 → Verbal warning")
    assert points.required_step(b, {}, {"discipline": 2.75}, "late", None)["step"] is None
    assert points.required_step(b, {}, {"discipline": 3}, "late", None)["step"] == "coaching"
    skip = body(skip_steps=True)
    assert points.required_step(skip, {}, {"discipline": 9}, "late", None)["step"] == "final"
    # Points under the active step: nothing new.
    done = points.required_step(b, {}, {"discipline": 6}, "late", "written")
    assert done["step"] is None and done["reason"] == "Already on Written warning: no new step."
    # Above Final written on the ladder comes Termination review (Suspension isn't on it).
    t = points.required_step(b, {}, {"discipline": 11}, "late", "suspension")
    assert t["step"] == "termination" and t["hr_only"] is True


def test_offence_minimums_repeats_and_escalation_jump():
    b = body()
    s = points.required_step(b, {}, {"discipline": 6}, "theft", None)
    assert (s["step"], s["reason"]) == ("termination", "Theft or dishonesty goes to Termination review at least")
    assert points.required_step(b, {}, {"discipline": 5}, "alcohol", "coaching")["step"] == "final"
    s = points.required_step(b, {"repeat": 2}, {"discipline": 6}, "no_call", None)
    assert s["step"] == "termination" and "2nd No call, no show within 12 months" in s["reason"]
    assert points.required_step(b, {"repeat": 1}, {"discipline": 3}, "no_call", None)["step"] == "coaching"
    s = points.required_step(b, {}, {"discipline": 9.5}, "late", "final")
    assert (s["step"], s["reason"]) == ("termination", "New incident while on Final written warning → Termination review")
    anyb = body(escalation=[{"while": "any", "to": "next"}])
    assert points.required_step(anyb, {}, {"discipline": 0.5}, "late", "verbal")["step"] == "written"
    assert points.required_step(anyb, {}, {"discipline": 0.5}, "late", "termination")["step"] is None


def test_probation_ladder_and_serious_floor():
    b = body()
    ctx = {"on_probation": "introductory"}
    s = points.required_step(b, ctx, {"discipline": 3}, "late", None)
    assert (s["step"], s["reason"]) == ("final", "Points 3 ≥ 3 → Final written warning")
    assert points.required_step(b, ctx, {"discipline": 5}, "late", "final")["step"] == "termination"
    s = points.required_step(b, ctx, {"discipline": 5}, "alcohol", None)
    assert (s["step"], s["reason"]) == ("termination", "Serious offence during Introductory period → "
                                                       "Termination review")
    assert points.required_step(b, ctx, {"discipline": 0.5}, "impaired", None)["step"] == "termination"
    # Without probation the same 3 points is only Coaching.
    assert points.required_step(b, {}, {"discipline": 3}, "late", None)["step"] == "coaching"


# ---------------------------------------------------------------- totals, history, next drop

def test_next_drop(db):
    vid = publish(db, body(**{"expiry.rule": "rolling"}), "2025-01-01")
    assert points.next_drop(db, "1", "2026-01-01") is None
    incident(db, vid, "2026-01-10", 2, incident_id="a")
    incident(db, vid, "2026-01-10", 1, incident_id="b")
    incident(db, vid, "2026-04-01", 0.5, incident_id="c", offence="late_short")
    drop = points.next_drop(db, "1", "2026-06-01")
    assert drop == {"date": "2027-01-10", "points": 3.0, "tracks": {"discipline": 3.0},
                    "totals": {"discipline": 0.5}}
    assert points.next_drop(db, "1", "2027-01-10")["date"] == "2027-04-01"
    assert points.next_drop(db, "1", "2027-04-01") is None


def test_totals_list_every_point_total_of_the_version_in_force(db):
    b = body(tracks=[{"key": "discipline", "name": "Discipline"}, {"key": "attendance", "name": "Attendance"}],
             ladders={"discipline": [{"points": 3, "step": "coaching"}],
                      "attendance": [{"points": 3, "step": "coaching"}]})
    publish(db, b, "2026-01-01")
    assert points.totals(db, "1", "2026-02-01") == {"discipline": 0.0, "attendance": 0.0}
    assert points.totals(db, "1", "2025-02-01") == {}


def test_past_incidents_feed_repeat_rules(db):
    vid = publish(db, body(), "2025-01-01")
    incident(db, vid, "2026-01-10", 1, incident_id="a", scoring={"protected": False})
    incident(db, vid, "2026-02-10", 0, incident_id="b", scoring={"protected": True})
    points.void(db, "1", "hr", "Mistake", incident_id="a", starts="2026-03-01")
    past = points.past_incidents(db, "1", "2026-03-01")
    assert [(p["offence"], p["points"], p["voided"], p["protected"]) for p in past] == [
        ("late", 1.0, True, False), ("late", 0.0, False, True)]
    assert points.incident_points(body(), "late", {"happened": D("2026-03-05")}, past)["repeat"] == 1


# ---------------------------------------------------------------- credits

def credits_body(**more):
    return body(**{"credits.on": True, "credits.clean": {"days": 90}, "credits.points": 1,
                   "credits.max_per_year": 2, "expiry.rule": "rolling", **more})


def test_credits_after_a_clean_stretch(db):
    vid = publish(db, credits_body(), "2025-01-01")
    incident(db, vid, "2026-01-01", 3)
    assert points.award_credits(db, "2026-03-31") == 0
    assert points.award_credits(db, "2026-04-01") == 1           # 90 clean days after Jan 1
    assert total(db, "2026-04-01") == 2.0
    assert points.award_credits(db, "2026-04-01") == 0           # never twice
    assert points.award_credits(db, "2026-06-29") == 0
    assert points.award_credits(db, "2026-06-30") == 1           # 90 days after the last credit
    assert total(db, "2026-06-30") == 1.0
    # At most 2 a year: the next waits until the first leaves the 12 months.
    assert points.award_credits(db, "2027-03-31") == 0
    lines = [ln for ln in points.lines_of(db, "1") if ln["kind"] == "credit"]
    assert [ln["counts_from"].isoformat() for ln in lines] == ["2026-04-01", "2026-06-30"]
    assert any(a["action"] == "Points earned back" for a in audit.lines(db))


def test_credits_restart_with_a_new_incident_and_stop_at_zero(db):
    vid = publish(db, credits_body(**{"credits.max_per_year": 10}), "2025-01-01")
    incident(db, vid, "2026-01-01", 1, incident_id="a")
    incident(db, vid, "2026-03-01", 1, incident_id="b")          # the clock restarts
    assert points.award_credits(db, "2026-05-29") == 0
    assert points.award_credits(db, "2026-05-30") == 1           # Mar 1 + 90 days
    assert points.award_credits(db, "2026-08-28") == 1
    assert total(db, "2026-08-28") == 0.0
    assert points.award_credits(db, "2027-12-31") == 0           # nothing left to earn back
    incident(db, vid, "2028-01-01", 1, incident_id="c")
    assert total(db, "2028-01-01") == 1.0                         # credits never pile up


def test_catching_up_on_missed_nights_and_credits_off(db):
    vid = publish(db, credits_body(**{"credits.max_per_year": 5}), "2025-01-01")
    incident(db, vid, "2026-01-01", 3)
    assert points.award_credits(db, "2026-12-31") == 3           # Apr 1, Jun 30, Sep 28
    assert [ln["counts_from"].isoformat() for ln in points.lines_of(db, "1") if ln["kind"] == "credit"] == [
        "2026-04-01", "2026-06-30", "2026-09-28"]
    assert total(db, "2026-12-31") == 0.0
    publish(db, body(), "2027-01-01")      # a version with credits off
    incident(db, vid, "2027-02-01", 2, person="2")
    assert points.award_credits(db, "2027-12-31") == 0


# ---------------------------------------------------------------- the simulator

def test_simulator_timeline_with_probation_steps_and_expiry():
    b = body()
    sim = points.simulate(b, [
        {"date": "2026-02-01", "offence": "late"},
        {"date": "2026-02-10", "offence": "late_callout"},          # on probation: 3 -> Final written
        {"date": "2026-03-05", "offence": "conduct", "manager_points": 1},
        {"date": "2026-07-01", "offence": "late", "protected": "sick"},
    ], "2026-01-15", until="2027-06-01")
    assert sim["problems"] == []
    kinds = [(r["date"], r["kind"]) for r in sim["rows"]]
    assert kinds[0] == ("2026-01-15", "probation") and ("2026-04-14", "probation") in kinds
    rows = [r for r in sim["rows"] if r["kind"] == "incident"]
    assert [r["on_probation"] for r in rows] == ["introductory", "introductory", "introductory", None]
    assert [r["total"] for r in rows] == [1.0, 3.0, 4.0, 4.0]
    assert rows[1]["step"] == "final" and rows[1]["reason"] == "Points 3 ≥ 3 → Final written warning"
    assert rows[2]["step"] == "termination" and "while on Final written warning" in rows[2]["reason"]
    assert rows[3]["points"] == 0.0 and rows[3]["protected"] and rows[3]["step"] is None
    ends = [r for r in sim["rows"] if r["kind"] == "step_ends"]
    assert ends == [{"date": "2027-02-10", "kind": "step_ends", "step": "final",
                     "text": "Final written warning ends"}]
    drops = [(r["date"], r["points"], r["total"]) for r in sim["rows"] if r["kind"] == "drop"]
    assert drops == [("2027-02-01", -1.0, 3.0), ("2027-02-10", -2.0, 1.0), ("2027-03-05", -1.0, 0.0)]
    assert sim["totals"] == {"discipline": 0.0}


def test_simulator_credits_approval_days_and_problems():
    b = credits_body()
    sim = points.simulate(b, [{"date": "2026-01-01", "offence": "late_callout", "approved": "2026-01-05"}],
                          "2020-01-01", until="2026-12-31")
    credits = [(r["date"], r["points"]) for r in sim["rows"] if r["kind"] == "credit"]
    assert credits == [("2026-04-01", -1.0), ("2026-06-30", -1.0)]
    assert [r["approved"] for r in sim["rows"] if r["kind"] == "incident"] == ["2026-01-05"]
    bad = points.simulate(b, [{"date": "2026-01-05", "approved": "2026-01-01", "offence": "late"},
                              {"offence": "late"}], "2026-01-01")
    assert bad["rows"] == [] and len(bad["problems"]) == 2
    broken = copy.deepcopy(b)
    broken["steps"] = []
    assert points.simulate(broken, [], "2026-01-01")["problems"]
    assert "start date" in points.simulate(b, [], None)["problems"][0]
    errs = points.simulate(b, [{"date": "2026-01-05", "offence": "nope"}], "2026-01-01")
    assert [r["errors"] for r in errs["rows"] if r["kind"] == "incident"] == [
        ["That offence isn't in this point system."]]


def test_simulator_department_probation_length_and_adjustments():
    pb = copy.deepcopy(policy.SAMPLE["probation"])
    pb[0]["department_days"] = {"501": 120}
    b = body(probation=pb)
    sim = points.simulate(b, [{"date": "2026-04-20", "offence": "late"},
                              {"kind": "adjust", "date": "2026-04-25", "points": 2, "track": "discipline"}],
                          "2026-01-01", department="501", until="2026-12-31")
    probation = [r for r in sim["rows"] if r["kind"] == "probation"]
    assert probation[1]["date"] == "2026-04-30"         # 120 days, the last day
    inc = [r for r in sim["rows"] if r["kind"] == "incident"][0]
    assert inc["on_probation"] == "introductory"
    adj = [r for r in sim["rows"] if r["kind"] == "adjust"][0]
    assert (adj["total_before"], adj["total"]) == (1.0, 3.0)


# ---------------------------------------------------------------- re-scoring

def test_rescore_preview_then_apply(db):
    v1 = publish(db, body(**{"expiry.rule": "rolling"}), "2025-01-01")
    sc = {"manager": 1.5, "hr": None, "on_probation": None, "protected": False}
    incident(db, v1, "2026-01-10", 1.5, incident_id="a", scoring=sc)
    incident(db, v1, "2026-01-20", 2, incident_id="b", offence="conduct", scoring={"hr": 2})
    incident(db, v1, "2026-01-25", 0, incident_id="c", offence="late", scoring={"protected": True})
    incident(db, v1, "2026-01-30", 1, incident_id="d", offence="gone_offence")
    incident(db, v1, "2025-01-05", 1, incident_id="old")       # expired already: left alone
    offences = copy.deepcopy(policy.SAMPLE["offences"])
    for o in offences:
        if o["key"] == "late":
            o.update(points=2, min=2, max=3)
        if o["key"] == "conduct":
            o["points"], o["max"] = 3, 3
    v2_body = body(offences=offences, **{"expiry.rule": "rolling", "expiry.rolling": {"months": 6}})
    v2 = policy.save_draft(db, v2_body, "hr")
    policy.publish(db, v2, "2026-03-01", "hr", on=D("2026-03-01"))
    with pytest.raises(ValueError, match="in use today"):
        points.rescore_preview(db, ["1"], v1, on=D("2026-03-02"))
    before = db.conn.execute("SELECT COUNT(*) FROM point_entries").fetchone()[0]
    preview = points.rescore_preview(db, ["1"], v2, on=D("2026-03-02"))
    assert db.conn.execute("SELECT COUNT(*) FROM point_entries").fetchone()[0] == before
    [p] = preview["people"]
    by = {c["incident_id"]: c for c in p["changes"]}
    assert set(by) == {"a", "b", "c", "d"}
    assert (by["a"]["old"], by["a"]["new"]) == (1.5, 2.0) and "outside the new range" in by["a"]["notes"][0]
    assert (by["a"]["old_ends"], by["a"]["new_ends"]) == ("2027-01-10", "2026-07-10")
    assert (by["b"]["old"], by["b"]["new"]) == (2.0, 2.0) and by["b"]["change"]   # HR's value kept; ends sooner
    assert by["c"]["new"] == 0.0 and by["c"]["notes"] == []
    assert by["d"]["change"] is False and "isn't in the new version" in by["d"]["notes"][0]
    assert p["before"] == {"discipline": 4.5} and p["after"] == {"discipline": 5.0}
    assert preview["changed"] == 3
    done = points.rescore(db, ["1"], v2, "hr", "New handbook", on=D("2026-03-02"))
    assert done["changed"] == 3
    assert total(db, "2026-03-01") == 4.5 and total(db, "2026-03-02") == 5.0
    assert total(db, "2026-07-10") == 3.0          # a follows 6 months now...
    assert total(db, "2026-07-20") == 1.0          # ...and b; d keeps its own 12
    reasons = [h["reason"] for h in points.history(db, "1", "2026-03-02") if h["kind"] == "adjustment"]
    assert reasons and all(r.startswith("Re-scored under") and r.endswith("New handbook") for r in reasons)
    # Done again: nothing more changes.
    assert points.rescore_preview(db, ["1"], v2, on=D("2026-03-03"))["changed"] == 0
    assert points.scored_elsewhere(db, v2, on=D("2026-03-03")) == [{"id": "1", "name": "Maria Lopez", "incidents": 1}]
    with pytest.raises(ValueError, match="reason"):
        points.rescore(db, ["1"], v2, "hr", " ", on=D("2026-03-02"))


def test_preview_for_the_managers_form(db):
    vid = publish(db, body(), "2025-01-01")
    incident(db, vid, "2026-01-10", 4, offence="conduct", incident_id="a")
    b = policy.get(db, vid)["body"]
    p = points.preview(db, "1", b, "late_callout", {"happened": D("2026-02-01")}, as_of="2026-02-02",
                       active_step="coaching")
    assert p["text"] == "Maria: 4 → 6 points. Reaches Verbal warning. A write-up is needed."
    assert (p["before"], p["after"]) == ({"discipline": 4.0}, {"discipline": 6.0})
    p = points.preview(db, "1", b, "late_short", {"happened": D("2026-02-01")}, as_of="2026-02-02",
                       active_step="coaching")
    assert p["text"] == "Maria: 4 → 4.5 points."
    p = points.preview(db, "1", b, "late", {"happened": D("2026-02-01")}, manager_points=9, as_of="2026-02-02")
    assert p["points"]["errors"] and p["after"] == p["before"]
