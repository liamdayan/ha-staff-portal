"""Audit fixes in the discipline workflow and its points: a voided incident takes its write-up
not delivered yet with it, a credit HR voided isn't given again, a probation incident approved
after a passed "clear" probation never counts, an appeal voided on a write-up HR already voided
voids its incident, and a manager's points on probation are kept as chosen.

Nothing here talks to a real Connecteam, Google, TT Hotel or Home Assistant.
"""

import copy
from datetime import timedelta

import point_system
from hr_helpers import HR
from test_hr_discipline import engine, issued, run, setup, today  # noqa: F401 - engine is a fixture
from test_hr_points import credits_body, db, incident, publish  # noqa: F401 - db is a fixture
from test_hr_points import total as points_total
from test_hr_records import approve, record, total

from hr import discipline, points, probation


def test_voiding_an_incident_voids_its_approved_writeup(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            wid = await issued(hr, s)
            w = discipline.get_writeup(hr.hdb, wid)
            assert w["status"] == "approved"
            status, r, _ = await hr.call("POST", f"/api/incidents/{w['incident_id']}/void",
                                         {"reason": "Wrong person"}, s["hana"])
            assert status == 200, r
            w = discipline.get_writeup(hr.hdb, wid)
            assert w["status"] == "voided"
            body = discipline.body_of(hr.hdb, w["version_id"], today(hr))
            assert discipline.current_step(hr.hdb, body, "401", today(hr)) == (None, 0.0)
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/send", {}, s["max"])
            assert status != 200    # a voided write-up isn't the manager's to see
            # One left approved from before: it still isn't delivered.
            hr.hdb.conn.execute("UPDATE writeups SET status = 'approved' WHERE id = ?", (wid,))
            hr.hdb.conn.commit()
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/send", {}, s["max"])
            assert status == 400 and r["error"] == "Its incident was voided.", r
            assert discipline.get_writeup(hr.hdb, wid)["status"] == "approved"
    run(scenario())


def test_a_voided_credit_isnt_given_again(db):
    vid = publish(db, credits_body(), "2025-01-01")
    incident(db, vid, "2026-01-01", 5)
    assert points.award_credits(db, "2026-04-01") == 1
    assert points_total(db, "2026-04-01") == 4.0
    cid = [ln for ln in points.lines_of(db, "1") if ln["kind"] == "credit"][0]["id"]
    points.void(db, "1", "hr", "Given by mistake", target_id=cid, starts="2026-04-02")
    assert points_total(db, "2026-04-02") == 5.0
    assert points.award_credits(db, "2026-04-02") == 0
    assert points_total(db, "2026-04-02") == 5.0
    # The next one comes a clean stretch after the voided one.
    assert points.award_credits(db, "2026-06-29") == 0
    assert points.award_credits(db, "2026-06-30") == 1
    assert points_total(db, "2026-06-30") == 4.0


def test_a_probation_incident_approved_after_a_clearing_pass_never_counts(tmp_path, engine):
    pol, pts = engine
    body = copy.deepcopy(point_system.BODY)
    body["probation"][1]["on_pass"] = "clear"
    pol.publish(body)

    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db, t = hr.hdb, today(hr)
            row = probation.place(db, db.person("401"), t + timedelta(days=60), "Last chance.", "405",
                                  "Hana", "last_chance", today=t - timedelta(days=30))
            iid = record(db, "rude", t - timedelta(days=2), t - timedelta(days=2))   # waiting for HR
            probation.decide(db, row["id"], "pass", actor_id="406", actor="Hale", reason="Done well.",
                             today=t - timedelta(days=1))
            approve(db, iid, t)
            assert total(db, t) == 0.0
            assert discipline.get_incident(db, iid)["status"] == "approved"
            # One from after the pass counts as usual.
            later = record(db, "rude", t, t)
            approve(db, later, t)
            assert total(db, t) == 2.0
    run(scenario())


def test_an_appeal_voided_on_a_writeup_hr_already_voided_voids_its_incident(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            wid = await issued(hr, s)
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/send", {}, s["max"])
            assert status == 200, r
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/appeal", {"text": "Not me"},
                                         s["maria"])
            assert status == 200, r
            aid = discipline.get_appeal(hr.hdb, writeup_id=wid)["id"]
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/void", {"reason": "Oops"},
                                         s["hana"])
            assert status == 200, r
            status, r, _ = await hr.call("POST", f"/api/appeals/{aid}/decide",
                                         {"outcome": "voided", "note": "Agreed"}, s["hana"])
            assert status == 200, r
            w = discipline.get_writeup(hr.hdb, wid)
            assert discipline.get_appeal(hr.hdb, aid)["status"] == "voided"
            assert discipline.get_incident(hr.hdb, w["incident_id"])["status"] == "voided"
            assert discipline.totals(hr.hdb, "401", today(hr)).get("discipline", 0) == 0
    run(scenario())


def test_an_appeal_whose_void_fails_stays_open(tmp_path, engine, monkeypatch):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            wid = await issued(hr, s)
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/send", {}, s["max"])
            assert status == 200, r
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/appeal", {"text": "Not me"},
                                         s["maria"])
            assert status == 200, r
            aid = discipline.get_appeal(hr.hdb, writeup_id=wid)["id"]

            def fails(*args, **kwargs):
                raise ValueError("It can't be voided.")
            monkeypatch.setattr(discipline, "void_writeup", fails)
            status, r, _ = await hr.call("POST", f"/api/appeals/{aid}/decide",
                                         {"outcome": "voided", "note": "Agreed"}, s["hana"])
            assert status == 400, r
            assert discipline.get_appeal(hr.hdb, aid)["status"] == "open"
    run(scenario())


def test_a_managers_points_on_probation_are_kept_when_its_policy_gives_other_points(tmp_path, engine):
    pol, pts = engine
    body = copy.deepcopy(point_system.BODY)
    body["probation"][1]["overrides"] = {"rude": {"points": 3, "min": 1, "max": 3}}
    pol.publish(body)

    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db, t = hr.hdb, today(hr)
            probation.place(db, db.person("401"), t + timedelta(days=60), "Last chance.", "405",
                            "Hana", "last_chance", today=t - timedelta(days=30))
            # The offence's own 2, while the probation gives it 3: no reason asked, as the form.
            iid = record(db, "rude", t - timedelta(days=1), t, points="2")
            inc = discipline.get_incident(db, iid)
            assert inc["final_points"] == 2.0 and inc["manager_points"] == 2.0
            approve(db, iid, t)
            assert total(db, t) == 2.0
            # Other than the offence's own 2, the form asks why, as before.
            other = record(db, "rude", t, t, points="3", reason="It was bad.")
            assert discipline.get_incident(db, other)["manager_points"] == 3.0
    run(scenario())


def test_two_approvals_at_the_same_time_count_the_points_once(tmp_path, engine, monkeypatch):
    import asyncio
    from test_hr_discipline import record as record_api
    pol, pts = engine
    pol.publish()
    real_read = discipline.read_json

    async def slow_read(request):   # a body that arrives a moment after the request
        await asyncio.sleep(0.05)
        return await real_read(request)
    monkeypatch.setattr(discipline, "read_json", slow_read)

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            data = await record_api(hr, s)
            path = f"/api/incidents/{data['incident']}/approve"
            answers = await asyncio.gather(hr.call("POST", path, {}, s["hana"]),
                                           hr.call("POST", path, {}, s["hale"]))
            assert sorted(a[0] for a in answers)[0] == 200
            assert [a[0] for a in answers].count(200) == 1
            assert len(pts.lines("401", "incident")) == 1
    run(scenario())
