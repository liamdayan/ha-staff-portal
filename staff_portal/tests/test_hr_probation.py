"""Probation (design 7.8): introductory periods from the Connecteam start date by department,
disciplinary probation placed by HR, extensions within the policy's limits (and for a leave of
absence), the employee's acknowledgement, the end-of-probation recommendation and HR's
decision, reminders when the end passes undecided, the reviews part's calls (current,
end_review_due, record_recommendation, decide), and discipline.summary_for_review. The point
system is a stand-in (discipline_fakes.py); without one, the defaults apply.

Nothing here talks to a real Connecteam, Google, TT Hotel or Home Assistant.
"""

import asyncio
import json
import os
import time
from datetime import timedelta

import pytest

import discipline_fakes
from hr_helpers import HR, ct_user

from hr import discipline, notify, probation  # noqa: E402
from test_hr_discipline import WHO, day, inbox, kinds, setup, today  # noqa: E402

ANN = ct_user(407, "Ann", "Wu", code="1357", groups=[502], mobile="541-555-0407")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def engine(monkeypatch):
    return discipline_fakes.install(monkeypatch)


def section(page, name):
    return next((s for s in page["sections"] if s["name"] == name), None)


def test_introductory_periods_follow_the_start_date_and_department(tmp_path, engine):
    pol, pts = engine

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            # Long-time staff (start dates from 2023) never get one.
            assert probation.ensure_all(hr.hdb) == 0
            # No point system yet: 90 days for everyone.
            hr.hdb.update_person("401", start_date=day(hr, -10))
            hr.hdb.update_person("402", start_date=day(hr, -5))
            assert probation.ensure_all(hr.hdb) == 2
            maria = probation.current(hr.hdb, "401", today(hr))
            assert (maria["kind"], maria["start"], maria["end"], maria["status"], maria["extensions"]) == \
                ("introductory", day(hr, -10), day(hr, 80), "open", 0)
            assert probation.current(hr.hdb, "401", today(hr) - timedelta(days=11)) is None
            assert probation.current(hr.hdb, "401", today(hr) + timedelta(days=81)) is None
            assert probation.current(hr.hdb, "402", today(hr))["end"] == day(hr, 85)
            # Published: the Kitchen (502) has 60 days, from then on.
            pol.publish()
            await hr.add_people(ANN)
            hr.hdb.update_person("407", start_date=day(hr, -3))
            probation.ensure_all(hr.hdb)
            ann = probation.current(hr.hdb, "407", today(hr))
            assert (ann["policy_key"], ann["end"]) == ("intro", day(hr, 57))
            # HR confirms a different start date: an untouched one follows it.
            hr.hdb.update_person("401", start_date=day(hr, -20))
            probation.ensure_introductory(hr.hdb, hr.hdb.person("401"))
            assert probation.current(hr.hdb, "401", today(hr))["end"] == day(hr, 70)
            assert len(probation.of_person(hr.hdb, "401")) == 1
            # Employees see their own, with the end date (an HR setting), managers see more.
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["maria"])
            [mine] = section(page, "probation")["probations"]
            assert mine["ends"] == day(hr, 70) and "history" not in mine and "extensions" not in mine
            hr.hdb.set_settings({"probation_show_end": "0", "probation_word": "probation"})
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["maria"])
            sec = section(page, "probation")
            assert sec["probations"][0]["ends"] is None and sec["title"] == "Probation"
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["max"])
            sec = section(page, "probation")
            assert sec["probations"][0]["ends"] == day(hr, 70) and sec["can"]["recommend"]
            assert not sec["can"]["decide"] and "reason" not in sec["probations"][0]
            # Someone with nothing running: HR sees the section (to place someone), others don't.
            status, page, _ = await hr.call("GET", "/api/people/403", cookie=s["hana"])
            assert section(page, "probation")["can"]["place"]
            status, page, _ = await hr.call("GET", "/api/people/403", cookie=s["dee"])
            assert section(page, "probation") is None
    run(scenario())


def test_extensions_keep_to_the_policy_and_the_employee_acknowledges(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr, confirmed=False)
            hr.hdb.update_person("401", start_date=day(hr, -80))     # ends in 10 days
            row = probation.ensure_introductory(hr.hdb, hr.hdb.person("401"))
            url = f"/api/probations/{row['id']}/change"
            status, r, _ = await hr.call("POST", url, {"ends": day(hr, 40), "reason": "More time."}, s["hana"])
            assert (status, r["error"]) == (401, "reconfirm")
            hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = ?", (time.time(),))
            hr.hdb.conn.commit()
            assert (await hr.call("POST", url, {"ends": day(hr, 40), "reason": "x"}, s["max"]))[0] == 403
            assert (await hr.call("POST", url, {"ends": day(hr, 40)}, s["hana"]))[0] == 400
            status, r, _ = await hr.call("POST", url, {"ends": day(hr, 41), "reason": "More time."}, s["hana"])
            assert status == 400 and r["error"] == "An extension is at most 30 days."
            status, r, _ = await hr.call("POST", url, {"ends": day(hr, 40), "reason": "Needs more time on the bar."}, s["hana"])
            assert status == 200 and r["probation"]["ends"] == day(hr, 40) and r["probation"]["extensions"] == 1
            # The employee is told and asked to acknowledge; managers and HR are told too.
            assert ("probation_changed", "Your introductory period was extended. Open it to acknowledge.", 0) \
                in inbox(hr, "maria")
            assert "probation_changed" in kinds(hr, "max") and "probation_changed" in kinds(hr, "hale")
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["maria"])
            assert any("acknowledge" in t["text"] for t in home["todos"])
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["maria"])
            [mine] = section(page, "probation")["probations"]
            assert mine["needs_ack"] and "reason" not in json.dumps(mine)
            assert (await hr.call("POST", f"/api/probations/{row['id']}/acknowledge", {}, s["max"]))[0] == 404
            status, r, _ = await hr.call("POST", f"/api/probations/{row['id']}/acknowledge", {}, s["maria"])
            assert status == 200 and r["probation"]["needs_ack"] is False
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["maria"])
            assert not any("acknowledge" in t["text"] for t in home["todos"])
            # One extension only; a leave of absence doesn't count, but 120 days is the most.
            status, r, _ = await hr.call("POST", url, {"ends": day(hr, 45), "reason": "Again"}, s["hana"])
            assert status == 400 and "at most 1 time" in r["error"]
            status, r, _ = await hr.call("POST", url, {"ends": day(hr, 60), "reason": "Leave: surgery",
                                                       "leave": True}, s["hana"])
            assert status == 200 and r["probation"]["extensions"] == 1
            # Shortening: not before today.
            assert (await hr.call("POST", url, {"ends": day(hr, -1), "reason": "x"}, s["hana"]))[0] == 400
            status, r, _ = await hr.call("POST", url, {"ends": day(hr, 50), "reason": "Back early."}, s["hana"])
            assert status == 200
            history = [h["what"] for h in r["probation"]["history"]]
            assert history == ["Started on the start date", "Extended", "Acknowledged by the employee",
                               "Extended for a leave of absence", "Shortened"]
            # HR admins' own probation is another HR admin's.
            hr.hdb.update_person("405", start_date=day(hr, -10))
            own = probation.ensure_introductory(hr.hdb, hr.hdb.person("405"))
            status, r, _ = await hr.call("POST", f"/api/probations/{own['id']}/change",
                                         {"ends": day(hr, 90), "reason": "x"}, s["hana"])
            assert status == 404
    run(scenario())


def test_extensions_never_pass_the_policys_maximum(tmp_path, engine):
    pol, pts = engine

    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            hr.hdb.update_person("401", start_date=day(hr, -100))
            assert probation.ensure_introductory(hr.hdb, hr.hdb.person("401")) is None   # over already
            body = pol.publish()
            body["probation"][0]["extensions"] = {"count": 1, "days": 60, "maximum": 120}
            hr.hdb.update_person("401", start_date=day(hr, -10))                         # ends in 80 days
            row = probation.ensure_introductory(hr.hdb, hr.hdb.person("401"))
            with pytest.raises(ValueError, match="longer than 120 days"):
                probation.change_end(hr.hdb, row, today(hr) + timedelta(days=121), "x", "405", "Hana")
            row = probation.change_end(hr.hdb, row, today(hr) + timedelta(days=110), "x", "405", "Hana")
            assert row["ends"] == day(hr, 110) and row["extensions"] == 1
    run(scenario())


def test_hr_places_someone_on_disciplinary_probation(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            url = "/api/people/401/probation"
            assert (await hr.call("POST", url, {"ends": day(hr, 90), "reason": "x"}, s["max"]))[0] == 403
            assert (await hr.call("POST", "/api/people/405/probation", {"ends": day(hr, 90), "reason": "x"},
                                  s["hana"]))[0] == 403
            assert (await hr.call("POST", url, {"ends": day(hr, 90), "reason": ""}, s["hana"]))[0] == 400
            assert (await hr.call("POST", url, {"ends": day(hr, -1), "reason": "x"}, s["hana"]))[0] == 400
            status, r, _ = await hr.call("POST", url, {"ends": day(hr, 90), "policy_key": "last_chance",
                                                       "reason": "Last-chance agreement, signed today."}, s["hana"])
            assert status == 200, r
            assert r["probation"]["kind"] == "disciplinary" and r["probation"]["title"] == "Disciplinary probation"
            assert r["probation"]["reason"] == "Last-chance agreement, signed today."
            assert probation.current(hr.hdb, "401", today(hr))["policy_key"] == "last_chance"
            assert (await hr.call("POST", url, {"ends": day(hr, 60), "reason": "Again"}, s["hana"]))[0] == 400
            assert ("probation_changed", "You were placed on disciplinary probation. Open it to acknowledge.", 0) \
                in inbox(hr, "maria")
            # HR's reason stays with HR; the employee sees it running.
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["max"])
            assert "Last-chance agreement, signed" not in json.dumps(page)
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["maria"])
            [mine] = section(page, "probation")["probations"]
            assert mine["kind"] == "disciplinary" and mine["needs_ack"]
            assert "Last-chance agreement, signed" not in json.dumps(page)
            # The sealed reason isn't readable in the database.
            assert "Last-chance" not in json.dumps([dict(x) for x in hr.hdb.conn.execute("SELECT * FROM probations")])
    run(scenario())


def test_the_end_of_probation_review_and_hrs_decision(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            hr.hdb.update_person("401", start_date=day(hr, -80))     # ends in 10 days: review due
            hr.hdb.update_person("402", start_date=day(hr, -10))     # Kitchen, 60 days: not yet
            probation.ensure_all(hr.hdb)
            maria = probation.current(hr.hdb, "401", today(hr))
            due = probation.end_review_due(hr.hdb, today(hr))
            assert due == [{"person_id": "401", "probation_id": maria["id"], "end": day(hr, 10),
                            "due": day(hr, -4)}]
            assert probation.end_review_due(hr.hdb, today(hr) - timedelta(days=5)) == []
            # The nightly run tells Max and HR once.
            counts = probation.daily(hr.hdb, today(hr))
            assert counts["due"] == 1 and probation.daily(hr.hdb, today(hr))["due"] == 0
            assert ("probation_due", f"Maria Lopez: the end-of-introductory period review is due. It "
                    f"ends on {day(hr, 10)}.", 0) in inbox(hr, "max")
            assert "probation_due" in kinds(hr, "hana") and kinds(hr, "maria") == []
            # Max recommends ending employment; HR decides.
            url = f"/api/probations/{maria['id']}/recommend"
            assert (await hr.call("POST", url, {"outcome": "fire"}, s["max"]))[0] == 400
            assert (await hr.call("POST", url, {"outcome": "extend", "extend_to": day(hr, 5)}, s["max"]))[0] == 400
            assert (await hr.call("POST", url, {"outcome": "end"}, s["maria"]))[0] == 403
            status, r, _ = await hr.call("POST", url, {"outcome": "end", "note": "Missed four shifts."}, s["max"])
            assert status == 200 and r["probation"]["recommendation"]["word"] == "End employment"
            assert "probation_recommended" in kinds(hr, "hana")
            status, q, _ = await hr.call("GET", "/api/discipline/queue", cookie=s["hana"])
            assert [x["recommendation"] for x in q["probation"]["recommended"]] == ["End employment"]
            decide = f"/api/probations/{maria['id']}/decide"
            assert (await hr.call("POST", decide, {"outcome": "end"}, s["hana"]))[0] == 400
            assert (await hr.call("POST", decide, {"outcome": "end", "reason": "x"}, s["max"]))[0] == 403
            status, r, _ = await hr.call("POST", decide, {"outcome": "end", "reason": "Attendance."}, s["hana"])
            assert status == 200 and "Archive them in Connecteam by hand" in r["message"]
            assert r["probation"]["status_word"] == "Employment ending" and r["probation"]["separation_todo"]
            assert not any(k == "probation_changed" for k in kinds(hr, "maria"))
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["hale"])
            assert any("archive in Connecteam" in t["text"] for t in home["todos"])
            assert (await hr.call("POST", decide, {"outcome": "pass", "reason": "x"}, s["hana"]))[0] == 400
            status, r, _ = await hr.call("POST", f"/api/probations/{maria['id']}/separation-done", {}, s["hale"])
            assert status == 200
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["hale"])
            assert not any("archive in Connecteam" in t["text"] for t in home["todos"])
            assert probation.current(hr.hdb, "401", today(hr) + timedelta(days=1)) is None
            # From the reviews part: the recommendation, then HR passes it.
            bo = probation.current(hr.hdb, "402", today(hr))
            probation.record_recommendation(hr.hdb, bo["id"], "pass", None, "review-9", "403")
            row = probation.get(hr.hdb, bo["id"])
            assert (row["recommendation"], row["recommend_review"], row["recommended_by"]) == ("pass", "review-9", "403")
            with pytest.raises(ValueError):
                probation.record_recommendation(hr.hdb, bo["id"], "extend", None, None, "403")
            probation.decide(hr.hdb, bo["id"], "pass", actor_id="405", actor="Hana Ito", reason="Great start.")
            assert probation.get(hr.hdb, bo["id"])["status"] == "passed"
            assert ("probation_changed", "You passed your introductory period.", 0) in inbox(hr, "bo")
            with pytest.raises(ValueError, match="already decided"):
                probation.decide(hr.hdb, bo["id"], "pass", actor_id="405", reason="x")
    run(scenario())


def test_an_end_date_passed_without_a_decision_reminds_daily_or_passes(tmp_path, engine):
    pol, pts = engine
    body = pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            hr.hdb.update_person("401", start_date=day(hr, -89))
            row = probation.ensure_introductory(hr.hdb, hr.hdb.person("401"))
            later = today(hr) + timedelta(days=3)
            assert probation.daily(hr.hdb, later)["reminded"] == 1
            assert probation.daily(hr.hdb, later)["reminded"] == 1        # the same message, once
            assert sum(k == "probation_overdue" for k in kinds(hr, "max")) == 1
            probation.daily(hr.hdb, later + timedelta(days=1))
            assert sum(k == "probation_overdue" for k in kinds(hr, "max")) == 2
            status, q, _ = await hr.call("GET", "/api/discipline/queue", cookie=s["hana"])
            assert [x["id"] for x in q["probation"]["overdue"]] == []   # not overdue yet today
            # A policy that passes it by itself.
            body["probation"][0]["end_passes"] = "pass"
            assert probation.daily(hr.hdb, later + timedelta(days=2))["passed"] == 1
            row = probation.get(hr.hdb, row["id"])
            assert (row["status"], row["decided_by"]) == ("passed", "policy")
    run(scenario())


def test_an_incident_during_probation_uses_its_rules_even_if_approved_later(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            hr.hdb.update_person("401", start_date=day(hr, -95))
            hr.hdb.conn.execute(
                "INSERT INTO probations (person_id, kind, policy_key, starts, first_ends, ends, created_at) "
                "VALUES ('401', 'introductory', 'intro', ?, ?, ?, 0)", (day(hr, -95), day(hr, -5), day(hr, -5)))
            hr.hdb.conn.commit()
            # 3 points during probation: the probation ladder says Final written.
            status, data, _ = await hr.call("POST", "/api/people/401/incidents", {
                "offence": "ncns", "protected": "no", "what": "Didn't come in or call.",
                "happened": day(hr, -6)}, s["max"])
            assert status == 200, data
            assert pts.contexts[-1]["on_probation"] == "intro"
            assert discipline.get_incident(hr.hdb, data["incident"])["probation_key"] == "intro"
            status, r, _ = await hr.call("POST", f"/api/incidents/{data['incident']}/approve", {}, s["hana"])
            assert r["step"] == "final"
            # The same after it ended: the normal ladder (3 points is Coaching).
            status, data, _ = await hr.call("POST", "/api/people/401/incidents", {
                "offence": "ncns", "protected": "no", "what": "Again.", "happened": day(hr, -1)}, s["max"])
            assert pts.contexts[-1]["on_probation"] is None
    run(scenario())


def test_summary_for_the_review_side_panel(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            from test_hr_discipline import issued
            wid = await issued(hr, s)
            assert (await hr.call("POST", f"/api/writeups/{wid}/send", {}, s["max"]))[0] == 200
            summary = discipline.summary_for_review(hr.hdb, "401", today(hr) - timedelta(days=30), today(hr))
            assert summary["points"] == {"discipline": 2.0}
            assert summary["writeups"] == [{"date": day(hr), "step": "Coaching",
                                            "status": "Delivered: waiting for signature"}]
            assert summary["next_drop"] == {"date": day(hr, 365), "points": 2.0}
            assert discipline.summary_for_review(hr.hdb, "401", day(hr, -60), day(hr, -1))["writeups"] == []
            # Without the point system: nothing, rather than failing.
            discipline.points = discipline.policy = None
            assert discipline.summary_for_review(hr.hdb, "401", None, today(hr)) == {
                "points": {}, "writeups": [{"date": day(hr), "step": "Coaching",
                                            "status": "Delivered: waiting for signature"}], "next_drop": None}
    run(scenario())


def test_the_hr_portal_off_keeps_discipline_inert(tmp_path, engine):
    async def scenario():
        async with HR(tmp_path, enabled=False) as hr:
            for path in ("/api/discipline/queue", "/api/people/401/discipline", "/api/writeups/1"):
                status, data, _ = await hr.call("GET", path)
                assert status == 503 and "turned off" in data
            status, data, _ = await hr.call("POST", "/api/people/401/incidents", {})
            assert status == 503
            await hr.svc.tick()
            assert not os.path.exists(os.path.join(hr.folder, "hr", "hr.db"))
            assert notify.KINDS["writeup_to_sign"] == "You have a write-up to read and sign."
    run(scenario())


def test_the_nightly_run_and_ticks_run_the_parts(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            assert probation.nightly in hr.svc.daily_jobs and discipline.nightly in hr.svc.daily_jobs
            assert probation.hourly in hr.svc.tick_jobs      # (signed PDFs: the documents queue)
            hr.hdb.update_person("401", start_date=day(hr, -2))
            hr.svc.sync_state["tried"] = time.time()      # no people sync during this tick
            await hr.svc.tick()
            assert probation.current(hr.hdb, "401", today(hr)) is not None
            await hr.svc.nightly()
            assert WHO["maria"] == "401" and s
    run(scenario())
