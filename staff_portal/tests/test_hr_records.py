"""Discipline, probation and review records kept right (the records review of 0.10.0):

- an incident counts the points the rules give when HR approves it (a repeat counts the incidents
  HR approved since it was recorded), and HR sees that before approving;
- after a void or reduction, later write-ups are checked as they were worked out (the same day's
  totals, the step they were on, which repeat it was), so a real drop makes a task and a small
  one doesn't;
- escalation goes only from a warning delivered before the incident happened;
- a write-up's end date stops the points that led to its step, as the simulator says;
- HR voids an adjustment or a credit, and sets when one entry stops counting;
- a probation never passes by itself over a recommendation HR hasn't confirmed, extending it
  answers the recommendation, it lasts its number of days (the end is its last day), clearing
  its points on passing leaves older points alone, and a review covers the probation itself;
- a re-score keeps HR's own points; an extension after a review closed gets its own review; a
  recommendation that can never reach HR is shown, not retried.

The point system and ledger are the real ones (tests/point_system.py). Nothing here talks to a
real Connecteam, Google, TT Hotel or Home Assistant.
"""

import asyncio
import copy
import time
from datetime import date, timedelta
from types import SimpleNamespace as NS

import pytest

import point_system
import test_hr_parts_together as together
from hr_helpers import HR
from test_hr_discipline import kinds, section, setup, today

from hr import discipline, points, policy, probation, reviews  # noqa: E402

MAX = NS(id="403", name="Max Ray")
HANA = NS(id="405", name="Hana Ito")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def engine(monkeypatch):
    return point_system.install(monkeypatch)


def record(db, offence: str, on: date, recorded: date, **extra) -> int:
    """Max records an incident about Maria that happened `on`. Returns its id."""
    data = {"offence": offence, "happened": on.isoformat(), "what": "It happened.", **extra}
    if offence in ("late", "ncns"):
        data.setdefault("protected", "no")
    return discipline.record_incident(db, db.person("401"), MAX, "manager", data, recorded)["incident"]


def approve(db, iid: int, on: date) -> dict:
    return discipline.approve_incident(db, discipline.get_incident(db, iid), "405", "Hana Ito", today=on)


def deliver(db, wid: int, on: date) -> None:
    """HR approved the write-up, and Max delivered it that day (refused to sign, witnessed)."""
    db.conn.execute("UPDATE writeups SET status = 'approved' WHERE id = ?", (wid,))
    db.conn.commit()
    discipline.made_active(db, discipline.get_writeup(db, wid), MAX, "refused", on,
                           "Refused to sign, with a witness", witness_name=None)


def checks(db) -> list[str]:
    return [r[0] for r in db.conn.execute("SELECT reason FROM discipline_tasks WHERE kind = 'check'")]


def total(db, on=None) -> float:
    return points.totals(db, "401", on)["discipline"]


# ---------------------------------------------------------------- scoring at approval

def test_a_repeat_counts_when_hr_approves_the_earlier_incidents_at_the_same_sitting(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            db, t = hr.hdb, today(hr)
            # Three lates within 90 days (the third scores 0.5 more), all recorded before HR's
            # weekly sitting: each was scored 1 point when recorded.
            ids = [record(db, "late", t - timedelta(days=d), t) for d in (30, 20, 10)]
            assert [discipline.get_incident(db, i)["final_points"] for i in ids] == [1.0, 1.0, 1.0]
            status, view, _ = await hr.call("GET", f"/api/incidents/{ids[2]}", cookie=s["hana"])
            assert (view["points_if_approved"], view["earlier_waiting"]) == (1.0, 2)
            for i in ids[:2]:
                approve(db, i, t)
            # HR sees what approving the third counts now, before approving it.
            status, view, _ = await hr.call("GET", f"/api/incidents/{ids[2]}", cookie=s["hana"])
            assert (view["final_points"], view["points_if_approved"], view["earlier_waiting"]) == (1.0, 1.5, 0)
            status, view, _ = await hr.call("GET", f"/api/incidents/{ids[2]}", cookie=s["max"])
            assert "points_if_approved" not in view
            approve(db, ids[2], t)
            third = discipline.get_incident(db, ids[2])
            assert (third["default_points"], third["final_points"], third["hr_points"]) == (1.5, 1.5, None)
            [line] = [ln for ln in points.lines_of(db, "401") if ln["incident_id"] == str(ids[2])]
            assert (line["q"], line["scoring"]["final"], line["scoring"]["repeat"]) == (6, 1.5, 3)
            assert total(db) == 3.5
            whats = [e["what"] for e in discipline.events_of(db, incident_id=ids[2])]
            assert "Approved by HR at 1.5 points (the rules give 1.5 now, 1 when it was recorded)" in whats
            # HR's own points still win, measured against today's score.
            other = record(db, "late", t - timedelta(days=5), t)
            discipline.approve_incident(db, discipline.get_incident(db, other), "405", "Hana Ito",
                                        hr_points=1.0, hr_reason="Traffic accident.", today=t)
            found = discipline.get_incident(db, other)
            assert (found["final_points"], found["hr_points"]) == (1.0, 1.0)
    run(scenario())


# ---------------------------------------------------------------- checking later write-ups

def test_a_void_finds_a_later_write_up_that_is_now_too_high(tmp_path, engine):
    pol, pts = engine
    pol.publish()      # skip_steps: points reach their step straight away

    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db, t = hr.hdb, today(hr)
            a = record(db, "rude", t - timedelta(days=40), t - timedelta(days=40))
            approve(db, a, t - timedelta(days=40))                          # 2: Coaching
            b = record(db, "rude", t - timedelta(days=10), t - timedelta(days=10), points=3, reason="Loud.")
            r = approve(db, b, t - timedelta(days=10))                      # 5: Verbal
            assert r["step"] == "verbal"
            deliver(db, r["writeup"], t - timedelta(days=10))
            # HR voids the first today: the Verbal warning was reached on 5 points, now 3.
            discipline.void_incident(db, discipline.get_incident(db, a), "Wrong person.", "406", "Hale", t)
            assert total(db) == 3.0
            [reason] = checks(db)
            assert reason.endswith("may now be too high: Coaching instead.") and "was voided" in reason
    run(scenario())


def test_a_small_reduction_raises_no_false_alarm_when_steps_go_in_order(tmp_path, engine):
    pol, pts = engine
    body = copy.deepcopy(point_system.BODY)
    body["skip_steps"] = False
    pol.publish(body)

    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db, t = hr.hdb, today(hr)
            small = record(db, "late", t - timedelta(days=60), t)
            approve(db, small, t - timedelta(days=60))
            for d in (50, 30):          # 4 points: Coaching; then 7: Verbal (the next step)
                i = record(db, "rude", t - timedelta(days=d), t - timedelta(days=d), points=3, reason="Loud.")
                r = approve(db, i, t - timedelta(days=d))
                deliver(db, r["writeup"], t - timedelta(days=d))
            assert [w["step_key"] for w in db.conn.execute("SELECT step_key FROM writeups ORDER BY id")] == \
                ["coaching", "verbal"]
            # A quarter point off the old late: 6.75 is still Verbal (the next after Coaching).
            discipline.adjust_incident(db, discipline.get_incident(db, small), 0.75, "Typo.", "406", "Hale", t)
            assert total(db) == 6.75 and checks(db) == []
            # Taking the Coaching warning away does lower the Verbal one: Coaching would be next.
            [first] = [r["id"] for r in db.conn.execute("SELECT id FROM writeups WHERE step_key = 'coaching'")]
            discipline.void_writeup(db, discipline.get_writeup(db, first), "Not delivered properly.", "406",
                                    "Hale", t)
            [reason] = checks(db)
            assert "Verbal warning" in reason and reason.endswith("Coaching instead.")
    run(scenario())


# ---------------------------------------------------------------- escalation

def test_escalation_goes_only_from_a_warning_delivered_before_the_incident_happened(tmp_path, engine):
    pol, pts = engine
    body = copy.deepcopy(point_system.BODY)
    body["ladders"]["discipline"][-1]["points"] = 20     # so only escalation reaches it here
    pol.publish(body)      # escalation: a new incident while on Final written -> termination review

    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db, t = hr.hdb, today(hr)
            pts.add("401", 6, t - timedelta(days=100))
            # Two incidents recorded the same day; the late happened first.
            late = record(db, "late", t - timedelta(days=3), t, points=0.5, reason="Two minutes.")
            rude = record(db, "rude", t - timedelta(days=2), t)
            r = approve(db, rude, t)
            assert r["step"] == "final"
            db.conn.execute("UPDATE writeups SET status = 'with_hr', edited_by = '403' WHERE id = ?", (r["writeup"],))
            db.conn.commit()
            discipline.approve_writeup(db, discipline.get_writeup(db, r["writeup"]), HANA, db.person("401"))
            # Approved, not delivered: the late (from before it) isn't "while on Final written".
            assert approve(db, late, t)["step"] is None
            assert "Already on Final written warning" in discipline.get_incident(db, late)["step_reason"]
            deliver(db, r["writeup"], t)
            # Nor is one recorded later that happened before it was delivered...
            before = record(db, "late", t - timedelta(days=1), t, points=0.5, reason="Two minutes.")
            assert approve(db, before, t)["step"] is None
            assert not db.conn.execute("SELECT 1 FROM discipline_tasks WHERE kind = 'termination'").fetchone()
            # ...but one on the day it was delivered is.
            after = record(db, "late", t, t, points=0.5, reason="Two minutes.")
            assert approve(db, after, t)["step"] == "termination_review"
            assert discipline.get_incident(db, after)["step_reason"] == \
                "New incident while on Final written warning → Termination review"
    run(scenario())


# ---------------------------------------------------------------- write-up end dates

@pytest.mark.parametrize("rule", ["writeup", "first"])
def test_a_write_up_end_date_stops_the_points_that_led_to_it_as_the_simulator_says(tmp_path, engine, rule):
    pol, pts = engine
    body = copy.deepcopy(point_system.BODY)
    body["expiry"] = {"rule": rule, "rolling": {"months": 12}, "clean_reset": None}
    pol.publish(body)

    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db, t = hr.hdb, today(hr)
            late_day, rude_day = t - timedelta(days=200), t
            sim = points.simulate(policy.normalize(body), [
                {"date": late_day.isoformat(), "offence": "late", "protected": "no"},
                {"date": rude_day.isoformat(), "offence": "rude"}],
                (t - timedelta(days=400)).isoformat(), until=(t + timedelta(days=400)).isoformat())
            drops = [(r["date"], r["points"]) for r in sim["rows"] if r["kind"] == "drop"]
            # Coaching (90 days) covers both incidents' points: they stop together.
            assert drops == [((rude_day + timedelta(days=90)).isoformat(), -3.0)]
            approve(db, record(db, "late", late_day, late_day), late_day)
            r = approve(db, record(db, "rude", rude_day, rude_day), rude_day)
            assert r["step"] == "coaching"
            deliver(db, r["writeup"], rude_day)
            stops = date.fromisoformat(drops[0][0])
            assert (total(db, stops - timedelta(days=1)), total(db, stops)) == (3.0, 0.0)
            assert total(db, t + timedelta(days=2000)) == 0.0
            # HR moving the write-up's end moves the drop for the same points.
            w = discipline.get_writeup(db, r["writeup"])
            discipline.change_ends(db, w, t + timedelta(days=10), "Agreed at the meeting.", "406", "Hale", t)
            later = t + timedelta(days=11)
            assert (total(db, later - timedelta(days=1)), total(db, later)) == (3.0, 0.0)
            # A later write-up doesn't take over points another already covers.
            ends = [ln for ln in points.lines_of(db, "401") if ln["end_kind"] == "writeup"]
            assert {ln["scoring"]["writeup"] for ln in ends} == {r["writeup"]}
            assert len({ln["target_id"] for ln in ends}) == 2
    run(scenario())


# ---------------------------------------------------------------- HR undoing a line

def test_hr_voids_an_adjustment_or_a_credit_and_sets_when_one_entry_stops_counting(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr, confirmed=False)
            db, t = hr.hdb, today(hr)
            hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = ?", (time.time(),))
            hr.hdb.conn.commit()
            # HR adds 5 points by mistake (no end date).
            status, r, _ = await hr.call("POST", "/api/people/401/points",
                                         {"points": 5, "reason": "Meant for Bo."}, s["hana"])
            assert status == 200 and total(db) == 5.0
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["hana"])
            [line] = section(page, "discipline")["lines"]
            assert line["can"] == {"void": True, "end": True}
            url = f"/api/people/401/points/{line['id']}"
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["max"])
            assert "can" not in section(page, "discipline")["lines"][0]
            assert (await hr.call("POST", url + "/void", {"reason": "x"}, s["max"]))[0] == 403
            assert (await hr.call("POST", url + "/void", {"reason": ""}, s["hana"]))[0] == 400
            # It needs a fresh code.
            hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = ?", (time.time() - 3600,))
            hr.hdb.conn.commit()
            status, r, _ = await hr.call("POST", url + "/void", {"reason": "Meant for Bo."}, s["hana"])
            assert (status, r["error"]) == (401, "reconfirm")
            hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = ?", (time.time(),))
            hr.hdb.conn.commit()
            status, r, _ = await hr.call("POST", url + "/void", {"reason": "Meant for Bo."}, s["hana"])
            assert status == 200 and total(db) == 0.0
            assert (await hr.call("POST", url + "/void", {"reason": "Again."}, s["hana"]))[0] == 400
            # A credit too.
            rude = record(db, "rude", t - timedelta(days=5), t - timedelta(days=5))
            approve(db, rude, t - timedelta(days=5))
            cid = points.credit(db, "401", "discipline", 1, t, None, "Clean for 60 days.")
            assert total(db) == 1.0
            status, r, _ = await hr.call("POST", f"/api/people/401/points/{cid}/void",
                                         {"reason": "Not earned: a late was missed."}, s["hana"])
            assert status == 200 and total(db) == 2.0
            # One incident's points stop counting on a day HR sets (an override, which wins).
            [inc_line] = [ln for ln in points.lines_of(db, "401") if ln["kind"] == "incident"]
            url = f"/api/people/401/points/{inc_line['id']}/ends"
            assert (await hr.call("POST", url, {"ends": (t - timedelta(days=5)).isoformat(),
                                                "reason": "x"}, s["hana"]))[0] == 400
            assert (await hr.call("POST", url, {"ends": (t + timedelta(days=10)).isoformat()}, s["hana"]))[0] == 400
            status, r, _ = await hr.call("POST", url, {"ends": (t + timedelta(days=10)).isoformat(),
                                                       "reason": "Settled at the grievance meeting."}, s["hana"])
            assert status == 200, r
            assert (total(db, t + timedelta(days=9)), total(db, t + timedelta(days=10))) == (2.0, 0.0)
            assert any("stop counting" in e["what"] for e in discipline.events_of(db, incident_id=rude))
            # The manager sees that it changed, not HR's reason.
            for who, sees in (("max", False), ("hana", True)):
                _, inc, _ = await hr.call("GET", f"/api/incidents/{rude}", cookie=s[who])
                assert any("stop counting" in h["what"] for h in inc["history"]), who
                notes = [h["note"] for h in inc["history"]]
                assert ("Settled at the grievance meeting." in notes) is sees, (who, notes)
            # Points taken off can't be given an end here, and an incident is voided on its page.
            minus = points.adjust(db, "401", "discipline", -1, t, "405", "Fewer.")
            assert (await hr.call("POST", f"/api/people/401/points/{minus}/ends",
                                  {"ends": (t + timedelta(days=3)).isoformat(), "reason": "x"}, s["hana"]))[0] == 400
            assert (await hr.call("POST", f"/api/people/401/points/{inc_line['id']}/void",
                                  {"reason": "x"}, s["hana"]))[0] == 400
            actions = [r[0] for r in db.conn.execute("SELECT action FROM audit")]
            assert actions.count("Voided a points line") == 2
            assert "Changed when points stop counting" in actions
    run(scenario())


def test_voiding_added_points_a_later_write_up_relied_on_makes_a_check(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db, t = hr.hdb, today(hr)
            discipline.adjust_person(db, db.person("401"), {"points": 4, "reason": "From paper files.",
                                                            "starts": (t - timedelta(days=20)).isoformat()},
                                     "405", "Hana", t)
            r = approve(db, record(db, "rude", t - timedelta(days=10), t - timedelta(days=10)), t - timedelta(days=10))
            assert r["step"] == "written"
            deliver(db, r["writeup"], t - timedelta(days=10))
            [line] = [ln for ln in points.lines_of(db, "401") if ln["kind"] == "adjustment" and not ln["target_id"]]
            discipline.void_line(db, db.person("401"), line["id"], "They were Bo's.", "406", "Hale", t)
            [reason] = checks(db)
            assert reason.endswith("may now be too high: Coaching instead.")
    run(scenario())


# ---------------------------------------------------------------- probation

def test_probation_never_passes_by_itself_over_a_recommendation_hr_has_not_confirmed(tmp_path, engine):
    pol, pts = engine
    body = copy.deepcopy(point_system.BODY)
    body["probation"][0]["when_end_passes"] = "pass"
    pol.publish(body)

    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db, t = hr.hdb, today(hr)
            db.update_person("401", start_date=(t - timedelta(days=85)).isoformat())   # Pub: 90 days
            db.update_person("402", start_date=(t - timedelta(days=55)).isoformat())   # Kitchen: 60 days
            probation.ensure_all(db, t)
            maria, bo = probation.current(db, "401", t), probation.current(db, "402", t)
            assert maria["end"] == bo["end"] == (t + timedelta(days=4)).isoformat()
            probation.record_recommendation(db, maria["id"], "end", None, None, "403", "Missed shifts.")
            counts = probation.daily(db, t + timedelta(days=5))
            assert counts["passed"] == 1 and counts["reminded"] == 1
            assert probation.get(db, bo["id"])["status"] == "passed"
            row = probation.get(db, maria["id"])
            assert (row["status"], row["recommendation"]) == ("open", "end")
            assert "probation_overdue" in kinds(hr, "hana")
    run(scenario())


def test_extending_a_probation_answers_its_recommendation(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db, t = hr.hdb, today(hr)
            db.update_person("401", start_date=(t - timedelta(days=79)).isoformat())   # ends in 10 days
            row = probation.ensure_introductory(db, db.person("401"), t)
            probation.record_recommendation(db, row["id"], "extend", t + timedelta(days=30), None, "403")
            assert [x["recommendation"] for x in probation.hr_queue(db, HANA)["recommended"]] == ["Extend"]
            probation.decide(db, row["id"], "extend", t + timedelta(days=30), actor_id="405", actor="Hana",
                             reason="As recommended.")
            row = probation.get(db, row["id"])
            assert (row["recommendation"], row["recommend_to"], row["recommended_by"]) == (None, None, None)
            assert probation.hr_queue(db, HANA)["recommended"] == []
            assert "Recommended: Extend to" in " ".join(e["what"] for e in probation.events(db, row["id"]))
    run(scenario())


def test_a_probation_lasts_its_days_as_the_simulator_counts_them(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db, t = hr.hdb, today(hr)
            start = t - timedelta(days=10)
            db.update_person("401", start_date=start.isoformat())
            row = probation.ensure_introductory(db, db.person("401"), t)
            sim = points.simulate(policy.normalize(point_system.BODY), [], start.isoformat(),
                                  department="501")
            [last] = [r["date"] for r in sim["rows"] if r.get("text", "").startswith("Last day")]
            assert row["ends"] == last == (start + timedelta(days=89)).isoformat()
            assert probation.current(db, "401", start + timedelta(days=89))
            assert probation.current(db, "401", start + timedelta(days=90)) is None
    run(scenario())


def test_clearing_probation_points_on_passing_leaves_older_points_alone(tmp_path, engine):
    pol, pts = engine
    body = copy.deepcopy(point_system.BODY)
    body["probation"][1]["on_pass"] = "clear"
    pol.publish(body)

    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db, t = hr.hdb, today(hr)
            pts.add("401", 3, t - timedelta(days=300))           # stop counting in 65 days
            row = probation.place(db, db.person("401"), t + timedelta(days=60), "Last chance.", "405", "Hana",
                                  "last_chance", today=t - timedelta(days=30))
            approve(db, record(db, "rude", t - timedelta(days=10), t), t)
            assert total(db) == 5.0
            probation.decide(db, row["id"], "pass", actor_id="406", actor="Hale", reason="Done well.", today=t)
            assert total(db) == 3.0
            assert total(db, t + timedelta(days=100)) == 0.0
            assert [ln["end_kind"] for ln in points.lines_of(db, "401") if ln["kind"] == "adjustment"
                    and ln["target_id"]] == ["override"]
    run(scenario())


# ---------------------------------------------------------------- re-scoring

def test_a_re_score_keeps_hrs_own_points_for_an_incident(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db, t = hr.hdb, today(hr)
            rude = record(db, "rude", t - timedelta(days=5), t - timedelta(days=5))
            approve(db, rude, t - timedelta(days=5))
            discipline.adjust_incident(db, discipline.get_incident(db, rude), 1, "Appeal: amended.", "406", "Hale", t)
            body = copy.deepcopy(point_system.BODY)
            body["name"] = "v2"
            body["offences"][1]["max"] = 3.5           # an unrelated change
            pol.publish(body)
            v = policy.current(db, t)
            [change] = points.rescore_preview(db, ["401"], v["id"])["people"][0]["changes"]
            assert (change["old"], change["new"], change["change"]) == (1.0, 1.0, False)
            assert "HR set its points to 1: kept." in change["notes"]
            points.rescore(db, ["401"], v["id"], "406", "New version.")
            assert total(db) == 1.0
    run(scenario())


# ---------------------------------------------------------------- end-of-probation reviews

def test_a_short_extension_after_the_review_closed_gets_its_own_review(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await together.setup(hr)
            db, day = hr.hdb, together.today(hr)
            db.update_person("201", start_date=(day - timedelta(days=69)).isoformat())
            await hr.svc.nightly()
            prob = probation.current(db, "201", day)
            end = date.fromisoformat(prob["end"])
            [rid] = [r[0] for r in db.conn.execute("SELECT id FROM reviews WHERE kind = 'probation'")]
            path = f"/api/reviews/{rid}"
            for key in together.FORM_KEYS:
                await hr.call("PUT", f"{path}/answer", {"section": key, "rating": 3}, s["max"])
            new_end = end + timedelta(days=14)
            await hr.call("PUT", f"{path}/overall", {"recommendation": "extend", "extend_to": new_end.isoformat()}, s["max"])
            await hr.call("POST", f"{path}/submit", {}, s["max"])
            await hr.call("POST", f"{path}/approve", {}, s["hana"])
            status, r, _ = await hr.call("POST", f"/api/probations/{prob['id']}/decide",
                                         {"outcome": "extend", "extend_to": new_end.isoformat(), "reason": "ok"},
                                         s["ivy"])
            assert status == 200, r
            await hr.call("POST", f"{path}/share", {"meeting_date": day.isoformat()}, s["max"])
            await hr.call("POST", f"{path}/acknowledge", {"answer": "received"}, s["ana"])
            assert reviews.get(db, rid)["status"] == "closed"
            new_due = new_end - timedelta(days=14)
            for d in range(-14, 1):
                reviews.follow_probation(db, new_due + timedelta(days=d))
            later = [dict(x) for x in db.conn.execute("SELECT * FROM reviews WHERE kind = 'probation' AND id != ?",
                                                      (rid,))]
            assert [(x["due"], x["period_end"]) for x in later] == [(new_due.isoformat(), new_end.isoformat())]
    run(scenario())


def test_a_disciplinary_probations_review_covers_the_probation_and_says_so(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await together.setup(hr)
            db, day = hr.hdb, together.today(hr)
            db.update_person("201", start_date="2016-04-01")
            row = probation.place(db, db.person("201"), day + timedelta(days=20), "Last chance.", "205", "Hana")
            reviews.follow_probation(db, day)
            [review] = [dict(x) for x in db.conn.execute("SELECT * FROM reviews WHERE kind = 'probation'")]
            assert (review["probation_id"], review["period_start"], review["period_end"]) == (
                row["id"], day.isoformat(), row["ends"])
            status, view, _ = await hr.call("GET", f"/api/reviews/{review['id']}", cookie=s["hana"])
            assert view["review"]["kind_word"] == "End of disciplinary probation"
            titles = [i["title"] for i in together.notify.inbox(db, "203", 50)]
            assert any("end-of-disciplinary-probation review" in x for x in titles)
            assert reviews.pdf_title(db, review) == "End of disciplinary probation review"
    run(scenario())


def test_a_recommendation_that_can_never_reach_hr_is_shown_and_not_tried_again(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await together.setup(hr)
            db, day = hr.hdb, together.today(hr)
            db.update_person("201", start_date=(day - timedelta(days=69)).isoformat())
            await hr.svc.nightly()
            prob = probation.current(db, "201", day)
            end = date.fromisoformat(prob["end"])
            [rid] = [r[0] for r in db.conn.execute("SELECT id FROM reviews WHERE kind = 'probation'")]
            path = f"/api/reviews/{rid}"
            for key in together.FORM_KEYS:
                await hr.call("PUT", f"{path}/answer", {"section": key, "rating": 3}, s["max"])
            await hr.call("PUT", f"{path}/overall", {"recommendation": "extend",
                                                     "extend_to": (end + timedelta(days=14)).isoformat()}, s["max"])
            assert (await hr.call("POST", f"{path}/submit", {}, s["max"]))[0] == 200
            # While it waits for HR's approval, HR extends the probation for a leave, past that date.
            probation.change_end(db, probation.get(db, prob["id"]), end + timedelta(days=40), "Leave.", "206",
                                 "Ivy", leave=True)
            assert (await hr.call("POST", f"{path}/approve", {}, s["hana"]))[0] == 200
            review = reviews.get(db, rid)
            assert review["recommendation_sent_at"] is None
            assert review["recommendation_problem"] == "Choose the new end date, after the current one."
            assert probation.get(db, prob["id"])["recommendation"] is None
            assert "review_not_passed" in together.kinds(hr, "ivy") and "review_not_passed" in together.kinds(hr, "max")
            status, view, _ = await hr.call("GET", path, cookie=s["ivy"])
            assert view["recommendation_problem"] == review["recommendation_problem"]
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["ivy"])
            assert any("couldn't reach HR's decision" in t["text"] for t in home["todos"])
            # The nightly run doesn't try it again.
            assert reviews.pass_waiting(db) == 0
            assert together.kinds(hr, "ivy").count("review_not_passed") == 1
            # Once HR decides the probation, the to-do goes.
            probation.decide(db, prob["id"], "pass", actor_id="206", actor="Ivy", reason="Fine.")
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["ivy"])
            assert not any("couldn't reach HR's decision" in t["text"] for t in home["todos"])
    run(scenario())
