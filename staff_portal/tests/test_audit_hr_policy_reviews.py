"""Audit fixes for probation (introductory policies by department, extending, a start date
moved into the past) and the point system setup (a big import, numbers JSON allows but Python
can't convert). The real point system (tests/point_system.py), and the real HR portal.

Nothing here talks to a real Connecteam, Google, TT Hotel or Home Assistant.
"""

import asyncio
import copy

import pytest

import point_system
from hr_helpers import HR

from hr import policy, probation  # noqa: E402
from test_hr_discipline import day, setup, today  # noqa: E402
from test_hr_policy import confirm_all, sessions  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def engine(monkeypatch):
    return point_system.install(monkeypatch)


def test_an_introductory_policy_for_some_departments_leaves_the_others_out(tmp_path, engine):
    pol, pts = engine
    body = copy.deepcopy(point_system.BODY)
    body["probation"][0]["departments"] = ["502"]      # the Kitchen's only
    pol.publish(body)

    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            hr.hdb.update_person("401", start_date=day(hr, -10))    # Maria: the Pub
            hr.hdb.update_person("402", start_date=day(hr, -10))    # Bo: the Kitchen
            assert probation.policy_for(hr.hdb, probation.INTRODUCTORY, groups=["501"]) is None
            assert probation.ensure_introductory(hr.hdb, hr.hdb.person("401")) is None
            probation.ensure_all(hr.hdb)
            assert probation.current(hr.hdb, "401", today(hr)) is None
            assert probation.current(hr.hdb, "402", today(hr))["policy_key"] == "intro"
            # The live result agrees with the simulator.
            assert policy.intro_policy(body, ["501"]) is None
    run(scenario())


def test_extending_needs_a_later_end_and_not_one_already_past(tmp_path, engine):
    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            hr.hdb.update_person("401", start_date=day(hr, -79))     # ends in 10 days
            row = probation.ensure_introductory(hr.hdb, hr.hdb.person("401"))
            # An "extend" to an earlier day isn't a quiet shortening.
            with pytest.raises(ValueError, match="after the current one"):
                probation.decide(hr.hdb, row["id"], "extend", day(hr, 5), actor_id="405",
                                 reason="More time.")
            assert probation.get(hr.hdb, row["id"])["ends"] == day(hr, 10)
            # Overdue, with "end employment" recommended: extending to a day that's already
            # past would clear it and leave it overdue.
            hr.hdb.conn.execute("UPDATE probations SET ends = ?, first_ends = ? WHERE id = ?",
                                (day(hr, -10), day(hr, -10), row["id"]))
            hr.hdb.conn.commit()
            probation.record_recommendation(hr.hdb, row["id"], "end", None, None, "403")
            with pytest.raises(ValueError, match="Choose today or a later day"):
                probation.decide(hr.hdb, row["id"], "extend", day(hr, -5), actor_id="405",
                                 reason="More time.")
            after = probation.get(hr.hdb, row["id"])
            assert (after["ends"], after["recommendation"]) == (day(hr, -10), "end")
            # A real extension still works.
            done = probation.decide(hr.hdb, row["id"], "extend", day(hr, 5), actor_id="405",
                                    reason="More time.")
            assert done["ends"] == day(hr, 5) and done["extensions"] == 1
    run(scenario())


def test_a_start_date_moved_into_the_past_waives_the_untouched_probation(tmp_path, engine):
    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            hr.hdb.update_person("401", start_date=day(hr, -10))
            row = probation.ensure_introductory(hr.hdb, hr.hdb.person("401"))
            assert row["status"] == "open"
            # HR confirms the real start date: two years back, so the period was long over.
            hr.hdb.update_person("401", start_date=day(hr, -730))
            assert probation.ensure_introductory(hr.hdb, hr.hdb.person("401")) is None
            [moved] = probation.of_person(hr.hdb, "401")
            assert (moved["starts"], moved["status"]) == (day(hr, -730), "waived")
            assert probation.current(hr.hdb, "401", today(hr)) is None
            assert not probation.end_review_due(hr.hdb, today(hr))
            assert [e["what"] for e in probation.events(hr.hdb, row["id"])][-1] == \
                "Start date changed: the period was already over"
            # And it stays that way.
            assert probation.ensure_introductory(hr.hdb, hr.hdb.person("401"))["status"] == "waived"
            assert len(probation.of_person(hr.hdb, "401")) == 1
    run(scenario())


def test_an_import_too_big_to_save_says_so(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await sessions(hr)
            confirm_all(hr)
            monkeypatch.setattr(policy, "MAX_BODY", 100)     # the sample's saved by now
            status, data, _ = await hr.call("POST", "/api/policy/import",
                                            {"csv": "Offences\nName,Points\nLate,1"}, cookie=s["hr"])
            assert (status, data["error"]) == (400, "This point system is too big to save.")
    run(scenario())


def test_infinity_is_refused_and_huge_numbers_are_problems_not_a_crash(tmp_path):
    body = copy.deepcopy(point_system.BODY)
    body["steps"][0]["active"] = {"days": float("inf")}
    body["point_size"] = 10 ** 400
    clean = policy.normalize(body)
    assert clean["steps"][0]["active"] == {"days": float("inf")} and clean["point_size"] == 10 ** 400
    assert policy.validate(body)

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await sessions(hr)
            confirm_all(hr)
            status, data, _ = await hr.call("GET", "/api/policy", cookie=s["hr"])
            sample = data["versions"][0]["id"]
            status, saved, _ = await hr.call("PUT", f"/api/policy/{sample}", {"body": body},
                                             cookie=s["hr"])
            assert (status, saved["error"]) == (409, "A number in it isn't a real number.")
            body["steps"][0]["active"] = {"days": 30}
            status, saved, _ = await hr.call("PUT", f"/api/policy/{sample}", {"body": body},
                                             cookie=s["hr"])
            assert status == 200 and saved["problems"]   # the huge number is only a problem
    run(scenario())
