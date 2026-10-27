"""Performance reviews in the HR portal: forms and their versions, when reviews open (anniversaries,
a fixed month, check-ins, the end of probation, the gap between two), who writes them, the whole
flow (self-review, writing, HR's approval, sharing, acknowledging, the final PDF), who sees what
(private notes, comments, the side panel), reminders, and the settings.

The other parts reviews read (probation, discipline, certificates) are small stand-ins here (the
real ones together: test_hr_parts_together.py); final PDFs go through the real documents part's
PDF queue, with Google as the fake script (tests/fake_bridge.py). Nothing here talks to a real
Connecteam, Google, TT Hotel or Home Assistant.
"""

import asyncio
import json
import sqlite3
import sys
import time
import types
from datetime import date, datetime, timedelta, timezone

import pytest

from hr_helpers import HR, ct_user, queue_done
from test_hr_documents import google

from hr import audit, delivery, documents, notify, reviews, signin  # noqa: E402

ANA = ct_user(301, "Ana", "Lee", code="4821", groups=[501], mobile="541-555-0301")
BO = ct_user(302, "Bo", "Diaz", code="7302", groups=[501, 502], mobile="541-555-0302")
MAX = ct_user(303, "Max", "Ray", code="9900", groups=[501], mobile="541-555-0303")
KIM = ct_user(304, "Kim", "Orr", code="6611", groups=[502], mobile="541-555-0304")
DEE = ct_user(305, "Dee", "Fox", code="5566", groups=[502], mobile="541-555-0305")
HANA = ct_user(306, "Hana", "Ito", code="7788", groups=[502], mobile="541-555-0306")
IVY = ct_user(307, "Ivy", "Moe", code="3344", groups=[501], mobile="541-555-0307")
LEO = ct_user(308, "Leo", "Park", code="2468", groups=[], mobile="541-555-0308")
EVERYONE = (ANA, BO, MAX, KIM, DEE, HANA, IVY, LEO)
FORM_KEYS = ["guest_service", "teamwork", "reliability", "job_knowledge", "cleanliness",
             "communication", "policy"]


def run(coro):
    return asyncio.run(coro)


async def setup(hr: HR, confirmed: bool = True) -> dict:
    """Max manages the Pub (501), Kim the Kitchen (502), both primary; Dee is a director; Hana
    and Ivy are HR admins. Returns {person: session cookie}."""
    await hr.add_people(*EVERYONE)
    hr.give("303", "manager", {"501": True})
    hr.give("304", "manager", {"502": True})
    hr.give("305", "director")
    hr.give("306", "hr_admin")
    hr.give("307", "hr_admin")
    sessions = {str(u["userId"]): signin.new_session(hr.hdb, str(u["userId"]), "127.0.0.1", "test")
                for u in EVERYONE}
    hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = ?", (time.time() + 3600 if confirmed else None,))
    hr.hdb.conn.commit()
    return sessions


def today(hr: HR) -> date:
    return reviews.today_of(hr.hdb)


def open_review(hr: HR, pid: str, kind: str = "annual", due: date | None = None, **extra) -> int:
    due = due or today(hr) + timedelta(days=10)
    return reviews.create(hr.hdb, hr.hdb.person(pid), kind, due, **extra)


def inbox_kinds(hr: HR, pid: str) -> list[str]:
    return [i["kind"] for i in notify.inbox(hr.hdb, pid, 200)]


def actions(hr: HR) -> list[str]:
    return [line["action"] for line in audit.lines(hr.hdb, 500)]


async def fill(hr: HR, cookie: str, rid: int, rating: int = 4) -> None:
    for key in FORM_KEYS:
        status, data, _ = await hr.call("PUT", f"/api/reviews/{rid}/answer",
                                        {"section": key, "rating": rating}, cookie=cookie)
        assert status == 200, data


def fake_probation(rows=(), current=None, record=True):
    """The probation part as reviews uses it: end_review_due gives the rows due by a date."""
    mod = types.ModuleType("hr.probation")
    mod.rows = list(rows)
    mod.now = dict(current or {})
    mod.recorded = []
    mod.end_review_due = lambda db, on: [r for r in mod.rows if r["due"] <= on]
    mod.current = lambda db, pid, on: mod.now.get(str(pid))
    if record:
        mod.record_recommendation = lambda *args: mod.recorded.append(args)
    return mod


# ---------------------------------------------------------------- forms

def test_starter_form_versions_and_department_sections(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            db = hr.hdb
            starter = reviews.latest(db, "standard")
            body = starter["body"]
            assert starter["version"] == 1 and body["scale"][0] == "Unsatisfactory" and len(body["scale"]) == 5
            assert [x["key"] for x in body["sections"]] == FORM_KEYS + ["goals"]
            assert body["sections"][-1]["kind"] == "goals" and body["comment_at"] == 2
            first = open_review(hr, "301")
            # HR saves a new version with a Pub section and a 1-to-4 scale.
            new = json.loads(json.dumps(body))
            new["scale"] = ["Poor", "Fair", "Good", "Great"]
            new["departments"] = {"501": [{"title": "Bar skills", "kind": "rating"}]}
            status, data, _ = await hr.call("POST", "/api/review-forms",
                                            {"family": "standard", "form": new}, cookie=s["306"])
            assert status == 200 and data["form"]["version"] == 2
            assert data["form"]["body"]["departments"]["501"][0]["key"] == "bar_skills"
            second = open_review(hr, "302", due=today(hr) + timedelta(days=11))
            third = open_review(hr, "304", due=today(hr) + timedelta(days=12))
            # The old review keeps the form it opened with; the new ones get the new version.
            assert reviews.form_of(db, reviews.get(db, first))[0]["scale"][0] == "Unsatisfactory"
            body2, sections = reviews.form_of(db, reviews.get(db, second))
            assert body2["scale"] == ["Poor", "Fair", "Good", "Great"]
            # Department sections only for that department, before the goals.
            assert [x["key"] for x in sections][-2:] == ["bar_skills", "goals"]
            assert "bar_skills" not in [x["key"] for x in reviews.form_of(db, reviews.get(db, third))[1]]
            # Forms are only ever added to.
            with pytest.raises(sqlite3.DatabaseError):
                db.conn.execute("UPDATE review_forms SET name = 'x'")
            # A second form, chosen for check-ins.
            other = dict(new, name="Check-in", sections=[{"title": "How it's going", "kind": "text"}],
                         departments={}, recommendations=[])
            status, data, _ = await hr.call("POST", "/api/review-forms", {"form": other}, cookie=s["306"])
            assert status == 200 and data["form"]["family"] == "form2"
            status, data, _ = await hr.call("GET", "/api/review-forms", cookie=s["306"])
            assert [f["family"] for f in data["forms"]] == ["standard", "form2"]
            assert data["chosen"]["checkin30"] == "standard"
            status, _, _ = await hr.call("PUT", "/api/settings/reviews",
                                         {"reviews_forms": {"checkin30": "form2"}}, cookie=s["306"])
            assert status == 200
            check = open_review(hr, "308", kind="checkin30")
            assert reviews.form_of(db, reviews.get(db, check))[0]["name"] == "Check-in"
            status, data, _ = await hr.call("GET", "/api/review-forms/form2", cookie=s["306"])
            assert status == 200 and data["used"] == 1 and data["versions"][0]["version"] == 1
            # Bad forms, in plain words.
            for broken, words in (({**new, "scale": ["One"]}, "2 to 10"),
                                  ({**new, "sections": []}, "at least one"),
                                  ({**new, "comment_at": 9}, "need a comment"),
                                  ({**new, "sections": [{"title": "", "kind": "rating"}]}, "title"),
                                  ({**new, "sections": [{"title": "A", "kind": "essay"}]}, "asks for"),
                                  ({**new, "recommendations": ["A", "a"]}, "once")):
                status, data, _ = await hr.call("POST", "/api/review-forms", {"form": broken},
                                                cookie=s["306"])
                assert status == 400 and words in data["error"], (words, data)
            status, data, _ = await hr.call("POST", "/api/review-forms",
                                            {"family": "nope", "form": new}, cookie=s["306"])
            assert status == 400
            # Only HR, with a fresh code.
            assert (await hr.call("GET", "/api/review-forms", cookie=s["303"]))[0] == 403
            hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = NULL")
            hr.hdb.conn.commit()
            status, data, _ = await hr.call("POST", "/api/review-forms", {"form": new}, cookie=s["306"])
            assert (status, data["error"]) == (401, "reconfirm")
            assert "Saved a review form" in actions(hr)
    run(scenario())


# ---------------------------------------------------------------- when reviews open

def test_annual_reviews_on_anniversaries_and_in_a_fixed_month(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db = hr.hdb
            day = date(2026, 10, 26)
            starts = {"301": "2024-11-01",   # anniversary in 6 days: opens
                      "302": "2025-10-30",   # first anniversary in 4 days: opens
                      "303": "2026-10-20",   # started this year: nothing
                      "304": "2026-01-01",   # anniversary in January: not yet
                      "305": "2023-10-21",   # 5 days ago, missed: still opens (catch-up)
                      "306": "2023-10-01",   # 25 days ago: too late
                      "307": None, "308": None}
            for pid, start in starts.items():
                db.update_person(pid, start_date=start)
            assert reviews.schedule(db, day) == 3
            rows = {r["person_id"]: r for r in map(dict, db.conn.execute("SELECT * FROM reviews"))}
            assert set(rows) == {"301", "302", "305"}
            assert rows["301"]["due"] == "2026-11-01" and rows["301"]["source"] == "annual:2026"
            assert (rows["301"]["period_start"], rows["301"]["period_end"]) == ("2025-11-01", "2026-11-01")
            assert rows["302"]["period_start"] == "2025-10-30"
            assert rows["301"]["self_status"] == "waiting"      # annual reviews ask for a self-review
            assert reviews.schedule(db, day) == 0               # once only
            assert reviews.schedule(db, day + timedelta(days=1)) == 0
            # Archived people get none.
            db.update_person("304", start_date="2025-11-02", archived=1)
            assert reviews.schedule(db, day) == 0
            # A fixed month for everyone (November): opens on November 1st, due the 30th.
            db.conn.execute("DELETE FROM reviews")
            db.conn.commit()
            db.set_settings({"reviews_annual": "month", "reviews_annual_month": "11"})
            assert reviews.schedule(db, day) == 0
            assert reviews.schedule(db, date(2026, 11, 1)) == 4     # not those who started in 2026
            assert {r[0] for r in db.conn.execute("SELECT due FROM reviews")} == {"2026-11-30"}
            assert "303" not in {r[0] for r in db.conn.execute("SELECT person_id FROM reviews")}
            db.set_settings({"reviews_annual": "off"})
            db.conn.execute("DELETE FROM reviews")
            db.conn.commit()
            assert reviews.schedule(db, date(2026, 11, 1)) == 0
    run(scenario())


def test_check_ins_and_the_gap_between_two_reviews(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db = hr.hdb
            day = date(2026, 10, 26)
            for pid in ("301", "302", "303", "304", "305", "306", "307"):
                db.update_person(pid, start_date="2020-01-15")
            db.update_person("301", start_date=(day - timedelta(days=20)).isoformat())
            db.update_person("308", start_date=(day - timedelta(days=50)).isoformat())
            db.update_person("302", start_date=(day - timedelta(days=21)).isoformat())
            # Off by default.
            assert reviews.schedule(db, day) == 0
            # Leo's probation review is due in 25 days, so his 60-day check-in (in 10) is skipped.
            probation = fake_probation([{"person_id": "308", "probation_id": 5, "end": day + timedelta(days=39),
                                         "due": day + timedelta(days=25)}])
            monkeypatch.setitem(sys.modules, "hr.probation", probation)
            db.set_settings({"reviews_checkin_30": "1", "reviews_checkin_60": "1"})
            assert reviews.schedule(db, day) == 2
            kinds = {(r[0], r[1]) for r in db.conn.execute("SELECT person_id, kind FROM reviews")}
            assert kinds == {("301", "checkin30"), ("302", "checkin30")}
            ana = dict(db.conn.execute("SELECT * FROM reviews WHERE person_id = '301'").fetchone())
            assert ana["due"] == (day + timedelta(days=10)).isoformat() and ana["self_status"] == "none"
            assert ana["source"] == f"checkin30:{day - timedelta(days=20)}"
            # Bo's check-in has been started; Ana's hasn't.
            db.conn.execute("UPDATE reviews SET started = 1 WHERE person_id = '302'")
            db.conn.commit()
            # Their end-of-probation reviews open: Ana's untouched check-in makes way, Bo's stays.
            probation.rows += [{"person_id": "301", "probation_id": 6, "end": day + timedelta(days=28),
                                "due": day + timedelta(days=14)},
                               {"person_id": "302", "probation_id": 7, "end": day + timedelta(days=28),
                                "due": day + timedelta(days=14)}]
            assert reviews.follow_probation(db, day) == 2
            rows = {(r["person_id"], r["kind"]): dict(r) for r in db.conn.execute("SELECT * FROM reviews")}
            assert rows[("301", "checkin30")]["status"] == "cancelled"
            assert rows[("302", "checkin30")]["status"] == "writing"
            assert rows[("301", "probation")]["probation_id"] == 6
            assert ("308", "probation") not in rows    # due in 25 days: not open yet
            # A check-in never comes back next to a review that's already there.
            assert reviews.schedule(db, day) == 0
            # No gap: both.
            db.set_settings({"reviews_gap_days": "0"})
            assert reviews.schedule(db, day) == 1     # Leo's 60-day check-in
            assert "Cancelled a review" in actions(hr)
    run(scenario())


def test_end_of_probation_reviews_open_and_move(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            db = hr.hdb
            day = today(hr)
            db.update_person("308", start_date=(day - timedelta(days=70)).isoformat())
            db.update_person("301", start_date=(day - timedelta(days=70)).isoformat())
            end = day + timedelta(days=20)
            probation = fake_probation([{"person_id": "301", "probation_id": 7, "end": end,
                                         "due": end - timedelta(days=14)}], {"301": {"id": 7, "end": end}})
            monkeypatch.setitem(sys.modules, "hr.probation", probation)
            result = await reviews.nightly(hr.svc)
            assert result["probation"] == 1
            r = dict(db.conn.execute("SELECT * FROM reviews").fetchone())
            assert (r["kind"], r["probation_id"], r["due"], r["period_end"]) == (
                "probation", 7, (end - timedelta(days=14)).isoformat(), end.isoformat())
            assert r["self_status"] == "none"        # self-reviews: annual only
            assert "review_due" in inbox_kinds(hr, "303")      # Max: the Pub's primary manager
            # HR extends the probation by 30 days: the review moves with it.
            probation.now["301"] = {"id": 7, "end": end + timedelta(days=30)}
            probation.rows[0].update(end=end + timedelta(days=30), due=end + timedelta(days=16))
            await reviews.nightly(hr.svc)
            r = reviews.get(db, r["id"])
            assert r["due"] == (end + timedelta(days=16)).isoformat()
            assert r["period_end"] == (end + timedelta(days=30)).isoformat()
            assert "review_moved" in inbox_kinds(hr, "303")
            # The probation part's own due date wins when it gives one.
            probation.rows[0].update(due=day + timedelta(days=3), end=end + timedelta(days=30))
            await reviews.nightly(hr.svc)
            assert reviews.get(db, r["id"])["due"] == (day + timedelta(days=3)).isoformat()
            # Once shared it stays put.
            db.conn.execute("UPDATE reviews SET status = 'shared'")
            db.conn.commit()
            probation.now["301"] = {"id": 7, "end": end + timedelta(days=60)}
            probation.rows[0].update(due=day + timedelta(days=1))
            await reviews.nightly(hr.svc)
            assert reviews.get(db, r["id"])["due"] == (day + timedelta(days=3)).isoformat()
            # Turned off: no new ones.
            db.set_settings({"reviews_probation": "0"})
            probation.rows.append({"person_id": "308", "probation_id": 8, "end": end, "due": day})
            assert (await reviews.nightly(hr.svc))["probation"] == 0
            # Without the probation part at all, nothing breaks.
            monkeypatch.setitem(sys.modules, "hr.probation", None)
            db.set_settings({"reviews_probation": "1"})
            assert (await reviews.nightly(hr.svc))["probation"] == 0
            assert (await hr.call("GET", "/api/reviews", cookie=s["303"]))[0] == 200
    run(scenario())


def test_leavers_reviews_are_cancelled(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            rid = open_review(hr, "301")
            hr.hdb.update_person("301", archived=1)
            await reviews.nightly(hr.svc)
            assert reviews.get(hr.hdb, rid)["status"] == "cancelled"
    run(scenario())


# ---------------------------------------------------------------- who writes it

def test_who_writes_it(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            db = hr.hdb
            name = lambda rid: reviews.writer_name(db, reviews.get(db, rid), db.person(reviews.get(db, rid)["person_id"]))  # noqa: E731
            ana, bo, leo = open_review(hr, "301"), open_review(hr, "302"), open_review(hr, "308")
            maxs, hana = open_review(hr, "303", kind="adhoc"), open_review(hr, "306", kind="adhoc")
            assert name(ana) == "Max Ray"            # the Pub's primary manager
            assert name(bo) == "Max Ray"             # Pub and Kitchen: the first with a primary
            assert name(leo) == "HR"                 # no department
            assert name(maxs) == "HR"                # the Pub's manager is Max himself
            assert name(hana) == "HR"                # Kim can't see an HR admin
            # HR picks Bo's main department: the Kitchen, so Kim writes it.
            status, _, _ = await hr.call("PUT", "/api/people/302/reviews/main-department",
                                         {"group": "502"}, cookie=s["306"])
            assert status == 200 and name(bo) == "Kim Orr"
            status, data, _ = await hr.call("PUT", "/api/people/302/reviews/main-department",
                                            {"group": "999"}, cookie=s["306"])
            assert status == 400
            assert (await hr.call("PUT", "/api/people/302/reviews/main-department", {"group": ""},
                                  cookie=s["303"]))[0] == 403
            await hr.call("PUT", "/api/people/302/reviews/main-department", {"group": ""}, cookie=s["306"])
            assert name(bo) == "Max Ray"
            # Ana moves to the Kitchen in Connecteam: Kim writes it now, and Max can't see it.
            await hr.add_people(ct_user(301, "Ana", "Lee", code="4821", groups=[502], mobile="541-555-0301"),
                                BO, MAX, KIM, DEE, HANA, IVY, LEO)
            assert name(ana) == "Kim Orr"
            assert (await hr.call("GET", f"/api/reviews/{ana}", cookie=s["303"]))[0] == 404
            status, data, _ = await hr.call("GET", f"/api/reviews/{ana}", cookie=s["304"])
            assert status == 200 and data["can"]["write"]
            # HR hands it over: to Dee (a director), to HR, and back to the rule.
            status, data, _ = await hr.call("PUT", f"/api/reviews/{ana}/writer", {"writer": "305"},
                                            cookie=s["306"])
            assert status == 200 and name(ana) == "Dee Fox"
            assert (await hr.call("PUT", f"/api/reviews/{ana}/answer", {"section": "teamwork", "rating": 3},
                                  cookie=s["304"]))[0] == 403
            assert (await hr.call("PUT", f"/api/reviews/{ana}/answer", {"section": "teamwork", "rating": 3},
                                  cookie=s["305"]))[0] == 200
            for who in ("301", "302", "999"):     # herself, an employee, nobody
                status, data, _ = await hr.call("PUT", f"/api/reviews/{ana}/writer", {"writer": who},
                                                cookie=s["306"])
                assert status == 400 and "can see their file" in data["error"]
            await hr.call("PUT", f"/api/reviews/{ana}/writer", {"writer": "hr"}, cookie=s["306"])
            assert name(ana) == "HR"
            await hr.call("PUT", f"/api/reviews/{ana}/writer", {"writer": "auto"}, cookie=s["306"])
            assert name(ana) == "Kim Orr"
            assert (await hr.call("PUT", f"/api/reviews/{ana}/writer", {"writer": "hr"},
                                  cookie=s["304"]))[0] == 403
            # The HR pool: any HR admin but the person. Max's review: Hana or Ivy; Hana's: Ivy only.
            status, data, _ = await hr.call("GET", f"/api/reviews/{maxs}", cookie=s["306"])
            assert data["can"]["write"] and data["relation"] == "hr"
            assert (await hr.call("GET", f"/api/reviews/{maxs}", cookie=s["303"]))[0] == 404
            assert (await hr.call("GET", f"/api/reviews/{hana}", cookie=s["306"]))[0] == 404
            status, data, _ = await hr.call("GET", f"/api/reviews/{hana}", cookie=s["307"])
            assert data["can"]["write"]
            assert {"id": "305", "name": "Dee Fox"} not in data["writers"]    # Dee can't see HR admins
            # Nobody reviews themselves: Max can't start one on himself.
            assert (await hr.call("POST", "/api/people/303/reviews", {}, cookie=s["303"]))[0] == 403
    run(scenario())


def test_extra_reviews_started_by_hand(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            db = hr.hdb
            due = (today(hr) + timedelta(days=5)).isoformat()
            assert (await hr.call("POST", "/api/people/302/reviews", {"due": due}, cookie=s["301"]))[0] == 403
            assert (await hr.call("POST", "/api/people/301/reviews", {"due": due}, cookie=s["304"]))[0] == 404
            status, data, _ = await hr.call("POST", "/api/people/301/reviews",
                                            {"due": "2001-01-01"}, cookie=s["303"])
            assert status == 400
            status, data, _ = await hr.call("POST", "/api/people/301/reviews", {"due": due}, cookie=s["303"])
            assert status == 200
            r = reviews.get(db, data["id"])
            assert (r["kind"], r["assigned"], r["due"], r["self_status"], r["source"]) == (
                "adhoc", "303", due, "none", None)
            assert "review_due" not in inbox_kinds(hr, "303")     # they started it themselves
            # A director starts one: they write it. HR may leave it to the usual writer.
            status, data, _ = await hr.call("POST", "/api/people/302/reviews", {}, cookie=s["305"])
            assert reviews.get(db, data["id"])["assigned"] == "305"
            assert reviews.get(db, data["id"])["due"] == (today(hr) + timedelta(days=14)).isoformat()
            status, data, _ = await hr.call("POST", "/api/people/302/reviews", {"writer": "auto"},
                                            cookie=s["306"])
            r = reviews.get(db, data["id"])
            assert r["assigned"] is None and reviews.writer_name(db, r, db.person("302")) == "Max Ray"
            assert "review_due" in inbox_kinds(hr, "303")
            hr.hdb.update_person("308", archived=1)
            assert (await hr.call("POST", "/api/people/308/reviews", {}, cookie=s["306"]))[0] == 409
    run(scenario())


# ---------------------------------------------------------------- the flow

def test_an_annual_review_from_self_review_to_pdf(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            s = await setup(hr)
            db = hr.hdb
            rid = open_review(hr, "301", source="annual:2026")
            path = f"/api/reviews/{rid}"
            assert "review_self" in inbox_kinds(hr, "301") and "review_due" in inbox_kinds(hr, "303")
            # Ana's self-review: only that part, before it's shared.
            status, data, _ = await hr.call("GET", path, cookie=s["301"])
            assert status == 200 and data["can"]["self"] and "answers" not in data
            assert data["review"]["status_word"] == "Waiting for the self-review"
            assert (await hr.call("GET", path, cookie=s["302"]))[0] == 404     # another employee
            assert (await hr.call("GET", path, cookie=s["304"]))[0] == 404     # another department
            status, data, _ = await hr.call("PUT", path + "/self", {"section": "teamwork", "rating": 4,
                                                                    "text": "I cover shifts."}, cookie=s["301"])
            assert status == 200
            assert (await hr.call("PUT", path + "/self", {"section": "teamwork", "rating": 9},
                                  cookie=s["301"]))[0] == 400
            assert (await hr.call("PUT", path + "/self", {"section": "nope", "rating": 1},
                                  cookie=s["301"]))[0] == 400
            assert (await hr.call("PUT", path + "/answer", {"section": "teamwork", "rating": 1},
                                  cookie=s["301"]))[0] == 403
            status, data, _ = await hr.call("GET", path, cookie=s["303"])
            assert "self_review" not in data          # not sent yet
            # Max writes it, saving as he goes.
            await fill(hr, s["303"], rid)
            status, data, _ = await hr.call("PUT", path + "/answer", {"section": "teamwork", "rating": 2},
                                            cookie=s["303"])
            assert data["overall"]["auto"] == round((4 * 6 + 2) / 7, 1) and data["overall"]["word"]
            await hr.call("PUT", path + "/answer", {"section": "goals", "goals": ["Learn the bar", " ", "Lead a shift"]},
                          cookie=s["303"])
            await hr.call("PUT", path + "/answer", {"section": "overall", "text": "A good year."}, cookie=s["303"])
            assert (await hr.call("PUT", path + "/answer", {"section": "goals", "text": "x"},
                                  cookie=s["303"]))[0] == 400
            status, data, _ = await hr.call("POST", path + "/submit", {}, cookie=s["303"])
            problems = data["problems"]
            assert status == 400 and any("comment" in p and "Teamwork" in p for p in problems)
            assert any("recommendation" in p for p in problems) and any("self-review" in p for p in problems)
            # The self-review arrives.
            assert (await hr.call("POST", path + "/self/send", {}, cookie=s["301"]))[0] == 200
            assert "review_self_done" in inbox_kinds(hr, "303")
            assert (await hr.call("PUT", path + "/self", {"section": "teamwork", "rating": 5},
                                  cookie=s["301"]))[0] == 409
            status, data, _ = await hr.call("GET", path, cookie=s["303"])
            assert data["self_review"]["ratings"] == {"teamwork": 4}
            assert data["self_review"]["texts"]["teamwork"] == "I cover shifts."
            assert data["answers"]["texts"]["goals"] == ["Learn the bar", "Lead a shift"]
            await hr.call("PUT", path + "/answer", {"section": "teamwork", "text": "Late twice."}, cookie=s["303"])
            await hr.call("PUT", path + "/overall", {"override": 4, "recommendation": "Keep going as you are"},
                          cookie=s["303"])
            status, data, _ = await hr.call("POST", path + "/submit", {}, cookie=s["303"])
            assert data["problems"] == ["Say why the overall rating differs from the worked-out one."]
            assert (await hr.call("PUT", path + "/overall", {"recommendation": "Promote"},
                                  cookie=s["303"]))[0] == 400
            await hr.call("PUT", path + "/overall", {"reason": "Strong second half."}, cookie=s["303"])
            status, data, _ = await hr.call("POST", path + "/submit", {}, cookie=s["303"])
            assert (status, data["status"]) == (200, "ready")      # annual: no second approval
            assert (await hr.call("PUT", path + "/answer", {"section": "teamwork", "rating": 5},
                                  cookie=s["303"]))[0] == 403
            # Still only her self-review for Ana until it's shared.
            status, data, _ = await hr.call("GET", path, cookie=s["301"])
            assert "answers" not in data and "Late twice" not in json.dumps(data)
            status, data, _ = await hr.call("POST", path + "/share", {"meeting_date": "1999-01-01"},
                                            cookie=s["303"])
            assert status == 400
            meeting = today(hr).isoformat()
            assert (await hr.call("POST", path + "/share", {"meeting_date": meeting}, cookie=s["301"]))[0] == 409
            assert (await hr.call("POST", path + "/share", {"meeting_date": meeting}, cookie=s["303"]))[0] == 200
            assert "review_shared" in inbox_kinds(hr, "301")
            # Ana reads it: the review, without the writer's tools.
            status, data, _ = await hr.call("GET", path, cookie=s["301"])
            assert data["answers"]["texts"]["teamwork"] == "Late twice."
            assert data["overall"]["value"] == 4 and data["overall"]["reason"] == "Strong second half."
            assert data["recommendation"]["choice"] == "Keep going as you are"
            assert data["can"]["acknowledge"] and not data["can"]["write"]
            assert "side" not in data and "note" not in data and "notes" not in data
            assert data["review"]["status_word"] == "Ready for you to read"
            status, data, _ = await hr.call("POST", path + "/acknowledge", {"answer": "agree"}, cookie=s["301"])
            assert status == 400
            status, _, _ = await hr.call("POST", path + "/acknowledge",
                                         {"answer": "received", "comment": "Thanks <b>Max</b>."}, cookie=s["301"])
            assert status == 200
            r = reviews.get(db, rid)
            assert (r["status"], r["ack"]) == ("closed", "received")
            assert "review_acknowledged" in inbox_kinds(hr, "303")
            assert "review_acknowledged" in inbox_kinds(hr, "306") and "review_acknowledged" in inbox_kinds(hr, "307")
            assert (await hr.call("POST", path + "/acknowledge", {"answer": "received"}, cookie=s["301"]))[0] == 409
            # A shared review is never rewritten.
            with pytest.raises(sqlite3.DatabaseError):
                db.conn.execute("UPDATE reviews SET ratings = '{}' WHERE id = ?", (rid,))
            db.conn.rollback()
            # The final PDF: what Ana saw, escaped, made through the documents part's PDF queue and
            # saved as a "reviews" document in her Reviews folder.
            await queue_done(hr)
            doc = documents.get(db, reviews.get(db, rid)["pdf_doc_id"])
            title = f"Annual review {r['due'][:4]}"
            assert documents.kind(db, doc["kind_id"])["key"] == "reviews" and doc["mime"] == "application/pdf"
            assert (documents.title_of(doc), doc["doc_date"]) == (title, today(hr).isoformat())
            assert fake.names_in("Reviews") == [f"{today(hr).isoformat()} {title}.pdf"]
            [page] = fake.pdfs
            assert "Ana Lee" in page and "Late twice." in page and "Learn the bar" in page
            assert "Thanks &lt;b&gt;Max&lt;/b&gt;." in page and "<b>Max</b>" not in page
            assert "Acknowledged as received" in page and "not that they agree" in page
            documents.queue_pdf(db, "reviews", "301", rid)          # asked again: nothing more
            await queue_done(hr)
            assert len(fake.pdfs) == 1
            status, data, _ = await hr.call("GET", path, cookie=s["301"])
            assert data["pdf"] == {"ready": True, "link": documents.view_url(doc)}
            done = {"Opened a review", "Viewed a review", "Worked on a review", "Sent a self-review",
                    "Finished writing a review", "Shared a review", "Acknowledged a review",
                    "Saved a review's PDF", "Worked on a self-review"}
            assert done <= set(actions(hr))
            # The lists: Ana's closed review, nothing left to do.
            status, data, _ = await hr.call("GET", "/api/reviews", cookie=s["301"])
            assert data["todo"] == [] and [i["id"] for i in data["closed"]] == [rid]
    run(scenario())


def test_end_of_probation_review_with_hr_approval(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            s = await setup(hr)
            db = hr.hdb
            day = today(hr)
            end = day + timedelta(days=14)
            probation = fake_probation([{"person_id": "302", "probation_id": 9, "end": end, "due": day}])
            monkeypatch.setitem(sys.modules, "hr.probation", probation)
            assert reviews.follow_probation(db, day) == 1
            rid = db.conn.execute("SELECT id FROM reviews").fetchone()[0]
            path = f"/api/reviews/{rid}"
            status, data, _ = await hr.call("GET", path, cookie=s["303"])
            assert data["form"]["probation"] and set(data["form"]["choices"]) == {"pass", "extend", "end"}
            await fill(hr, s["303"], rid, rating=3)
            await hr.call("PUT", path + "/overall", {"recommendation": "extend"}, cookie=s["303"])
            status, data, _ = await hr.call("POST", path + "/submit", {}, cookie=s["303"])
            assert data["problems"] == ["Choose the new end date, after the current one."]
            await hr.call("PUT", path + "/overall", {"extend_to": end.isoformat()}, cookie=s["303"])
            assert (await hr.call("POST", path + "/submit", {}, cookie=s["303"]))[0] == 400
            new_end = end + timedelta(days=30)
            await hr.call("PUT", path + "/overall", {"extend_to": new_end.isoformat()}, cookie=s["303"])
            status, data, _ = await hr.call("POST", path + "/submit", {}, cookie=s["303"])
            assert data["status"] == "approval" and probation.recorded == []
            assert "review_approval" in inbox_kinds(hr, "306") and "review_approval" in inbox_kinds(hr, "307")
            # The writer can't approve it; HR sends it back once, then approves.
            assert (await hr.call("POST", path + "/approve", {}, cookie=s["303"]))[0] == 403
            assert (await hr.call("POST", path + "/return", {"note": ""}, cookie=s["306"]))[0] == 400
            assert (await hr.call("POST", path + "/return", {"note": "Say more on reliability."},
                                  cookie=s["306"]))[0] == 200
            assert "review_returned" in inbox_kinds(hr, "303")
            status, data, _ = await hr.call("GET", path, cookie=s["303"])
            assert data["review"]["status"] == "writing" and data["returned_note"] == "Say more on reliability."
            await hr.call("PUT", path + "/answer", {"section": "reliability", "text": "Two late starts."},
                          cookie=s["303"])
            await hr.call("POST", path + "/submit", {}, cookie=s["303"])
            assert (await hr.call("POST", path + "/approve", {}, cookie=s["307"]))[0] == 200
            assert probation.recorded == [(db, 9, "extend", new_end, rid, "303")]   # the writer's
            r = reviews.get(db, rid)
            assert (r["status"], r["approved_by_name"]) == ("ready", "Ivy Moe") and r["recommendation_sent_at"]
            assert "review_approved" in inbox_kinds(hr, "303")
            await hr.call("POST", path + "/share", {"meeting_date": day.isoformat()}, cookie=s["303"])
            # Bo never sees the recommendation: HR decides the outcome in the probation part.
            status, data, _ = await hr.call("GET", path, cookie=s["302"])
            assert "recommendation" not in data and "extend" not in json.dumps(data.get("answers"))
            # Max closes it: Bo refused to acknowledge in the meeting.
            assert (await hr.call("POST", path + "/close", {"how": "maybe"}, cookie=s["303"]))[0] == 400
            assert (await hr.call("POST", path + "/close", {"how": "refused", "note": "Walked out."},
                                  cookie=s["302"]))[0] == 409
            assert (await hr.call("POST", path + "/close", {"how": "refused", "note": "Walked out."},
                                  cookie=s["303"]))[0] == 200
            await queue_done(hr)
            [page] = fake.pdfs
            assert "Refused to acknowledge" in page and "Extend" not in page and "Walked out" not in page
            doc = documents.get(db, reviews.get(db, rid)["pdf_doc_id"])
            assert documents.title_of(doc) == "End of introductory period review"
    run(scenario())


def test_probation_recommendation_waits_for_the_probation_part(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            db = hr.hdb
            db.set_settings({"reviews_second": "none"})
            probation = fake_probation([{"person_id": "301", "probation_id": 4, "end": today(hr),
                                         "due": today(hr)}], record=False)
            monkeypatch.setitem(sys.modules, "hr.probation", probation)
            reviews.follow_probation(db, today(hr))
            rid = db.conn.execute("SELECT id FROM reviews").fetchone()[0]
            await fill(hr, s["303"], rid)
            await hr.call("PUT", f"/api/reviews/{rid}/overall", {"recommendation": "pass"}, cookie=s["303"])
            status, data, _ = await hr.call("POST", f"/api/reviews/{rid}/submit", {}, cookie=s["303"])
            assert data["status"] == "ready"           # no second approval: straight to ready
            assert not reviews.get(db, rid)["recommendation_sent_at"]
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["306"])
            assert any("recommendation" in t["text"] for t in home["todos"])
            probation.record_recommendation = lambda *args: probation.recorded.append(args)
            assert reviews.pass_waiting(db) == 1
            assert probation.recorded[0][1:4] == (4, "pass", None)
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["306"])
            assert not any("recommendation" in t["text"] for t in home["todos"])
    run(scenario())


# ---------------------------------------------------------------- who sees what

def test_private_notes_comments_and_hidden_text(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            db = hr.hdb
            rid = open_review(hr, "302", kind="adhoc")        # Bo: Pub (Max writes) and Kitchen (Kim)
            path = f"/api/reviews/{rid}"
            assert (await hr.call("PUT", path + "/note", {"text": "MAX-NOTE"}, cookie=s["303"]))[0] == 200
            assert (await hr.call("PUT", path + "/note", {"text": "KIM-NOTE"}, cookie=s["304"]))[0] == 200
            assert (await hr.call("PUT", path + "/note", {"text": "BO-NOTE"}, cookie=s["302"]))[0] == 404
            for cookie, text, level in ((s["304"], "KIM-MANAGERS", "managers"),
                                        (s["304"], "KIM-EMPLOYEE", "employee"),
                                        (s["305"], "DEE-HR-ONLY", "hr")):
                status, data, _ = await hr.call("POST", path + "/comments", {"text": text, "visibility": level},
                                                cookie=cookie)
                assert status == 200, data
            assert (await hr.call("POST", path + "/comments", {"text": "x", "visibility": "restricted"},
                                  cookie=s["304"]))[0] == 400
            assert (await hr.call("POST", path + "/comments", {"text": "", "visibility": "hr"},
                                  cookie=s["304"]))[0] == 400

            async def seen(pid):
                status, _, resp = await hr.call("GET", path, cookie=s[pid])
                return status, await resp.text()
            status, text = await seen("303")
            assert "MAX-NOTE" in text and "KIM-NOTE" not in text and "KIM-MANAGERS" in text
            assert "KIM-EMPLOYEE" in text and "DEE-HR-ONLY" not in text
            status, text = await seen("304")
            assert "KIM-NOTE" in text and "MAX-NOTE" not in text and "DEE-HR-ONLY" not in text
            status, text = await seen("305")                   # Dee sees her own HR-only comment
            assert "DEE-HR-ONLY" in text and "MAX-NOTE" not in text and "KIM-NOTE" not in text
            status, text = await seen("306")
            for secret in ("MAX-NOTE", "KIM-NOTE", "KIM-MANAGERS", "KIM-EMPLOYEE", "DEE-HR-ONLY"):
                assert secret in text
            assert (await seen("302"))[0] == 404               # Bo: not shared yet
            # Shared: Bo sees only what's for him.
            await fill(hr, s["303"], rid)
            await hr.call("PUT", path + "/overall", {"recommendation": "Talk with HR"}, cookie=s["303"])
            await hr.call("POST", path + "/submit", {}, cookie=s["303"])
            await hr.call("POST", path + "/share", {"meeting_date": today(hr).isoformat()}, cookie=s["303"])
            status, text = await seen("302")
            assert status == 200 and "KIM-EMPLOYEE" in text
            for secret in ("MAX-NOTE", "KIM-NOTE", "KIM-MANAGERS", "DEE-HR-ONLY", '"side"'):
                assert secret not in text
            # Comments can be added later, never changed.
            with pytest.raises(sqlite3.DatabaseError):
                db.conn.execute("UPDATE review_comments SET visibility = 'employee'")
            db.conn.rollback()
            # Everything people typed is encrypted in the database.
            raw = open(db.path, "rb").read() + open(db.path + "-wal", "rb").read()
            for secret in (b"MAX-NOTE", b"KIM-NOTE", b"KIM-MANAGERS", b"KIM-EMPLOYEE", b"DEE-HR-ONLY"):
                assert secret not in raw
            # An emptied note is gone.
            await hr.call("PUT", path + "/note", {"text": ""}, cookie=s["303"])
            assert "MAX-NOTE" not in (await seen("306"))[1]
    run(scenario())


def test_the_meeting_date_starts_at_hrs_today_not_the_devices(tmp_path, monkeypatch):
    """Share it's "Meeting date" starts at the review's `today`: HR's day in its time zone. At 9 PM
    Pacific a device on UTC is already on tomorrow, so the device's date would be a day late."""
    real = time.time
    nine_pm_pacific = datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc).timestamp()   # Oct 26th there
    shift = nine_pm_pacific - real()
    monkeypatch.setattr(time, "time", lambda: real() + shift)

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            assert today(hr) == date(2026, 10, 26)
            rid = open_review(hr, "301", kind="adhoc")
            for who in ("303", "306"):   # the writer and HR (the employee sees it once shared)
                status, data, _ = await hr.call("GET", f"/api/reviews/{rid}", cookie=s[who])
                assert status == 200 and data["today"] == "2026-10-26", (who, status)
            status, body, _ = await hr.call("GET", "/static/hr-reviews.js")
            assert status == 200
            assert 'const date = h("input", { type: "date", value: data.today, required: true });' in body
            assert 'toLocaleDateString("en-CA"), required: true' not in body
    run(scenario())


def test_roles_on_review_actions(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr, confirmed=False)
            db = hr.hdb
            rid = open_review(hr, "301", kind="adhoc")
            path = f"/api/reviews/{rid}"
            # Employees can't write, finish, share or hand over.
            for method, sub, body in (("PUT", "/answer", {"section": "teamwork", "rating": 3}),
                                      ("PUT", "/overall", {"override": 3}), ("POST", "/submit", {}),
                                      ("POST", "/approve", {}), ("PUT", "/writer", {"writer": "hr"})):
                assert (await hr.call(method, path + sub, body, cookie=s["302"]))[0] == 404
            # Dee (a director) can read and comment but not write.
            status, data, _ = await hr.call("GET", path, cookie=s["305"])
            assert status == 200 and data["can"]["comment"] and not data["can"]["write"]
            assert "side" in data
            assert (await hr.call("PUT", path + "/answer", {"section": "teamwork", "rating": 3},
                                  cookie=s["305"]))[0] == 403
            # Cancelling needs HR and a fresh code.
            status, data, _ = await hr.call("POST", path + "/cancel", {"reason": "Duplicate"}, cookie=s["306"])
            assert (status, data["error"]) == (401, "reconfirm")
            await hr.confirm(s["306"], "306")
            assert (await hr.call("POST", path + "/cancel", {"reason": ""}, cookie=s["306"]))[0] == 400
            assert (await hr.call("POST", path + "/cancel", {"reason": "Duplicate"}, cookie=s["306"]))[0] == 200
            assert reviews.get(db, rid)["status"] == "cancelled"
            assert (await hr.call("PUT", path + "/answer", {"section": "teamwork", "rating": 3},
                                  cookie=s["303"]))[0] == 403
            await hr.confirm(s["303"], "303")
            assert (await hr.call("POST", path + "/cancel", {"reason": "x"}, cookie=s["303"]))[0] == 403
            # Nobody approves their own work: an HR admin who wrote it can't approve it.
            hrs = open_review(hr, "308", kind="probation")
            await fill(hr, s["306"], hrs)
            await hr.call("PUT", f"/api/reviews/{hrs}/overall", {"recommendation": "pass"}, cookie=s["306"])
            status, data, _ = await hr.call("POST", f"/api/reviews/{hrs}/submit", {}, cookie=s["306"])
            assert data["status"] == "approval"
            assert "review_approval" in inbox_kinds(hr, "307") and "review_approval" not in inbox_kinds(hr, "306")
            status, data, _ = await hr.call("POST", f"/api/reviews/{hrs}/approve", {}, cookie=s["306"])
            assert status == 403 and "own work" in data["error"]
            assert (await hr.call("POST", f"/api/reviews/{hrs}/approve", {}, cookie=s["307"]))[0] == 200
            # Unknown reviews are just not found.
            assert (await hr.call("GET", "/api/reviews/999999", cookie=s["306"]))[0] == 404
    run(scenario())


def test_side_panel_with_and_without_the_other_parts(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            db = hr.hdb
            old = open_review(hr, "301", kind="adhoc", due=today(hr) - timedelta(days=200))
            texts = reviews._seal_json({"goals": ["Learn the bar"]}, "texts")
            db.conn.execute("UPDATE reviews SET status = 'closed', closed_at = ?, texts = ? WHERE id = ?",
                            (time.time() - 86400, texts, old))
            db.conn.commit()
            rid = open_review(hr, "301", kind="adhoc")
            monkeypatch.setitem(sys.modules, "hr.discipline", None)
            monkeypatch.setitem(sys.modules, "hr.certificates", None)
            status, data, _ = await hr.call("GET", f"/api/reviews/{rid}", cookie=s["303"])
            side = data["side"]
            assert side["discipline"] is None and side["certificates"] is None
            assert side["goals"] == ["Learn the bar"]
            calls = []
            discipline = types.ModuleType("hr.discipline")

            def summary_for_review(db_, pid, start, end):
                calls.append((pid, start, end))
                return {"points": {"Discipline": 2.5}, "next_drop": {"date": date(2027, 1, 2), "points": 1},
                        "writeups": [{"date": date(2026, 9, 1), "step": "Verbal", "status": "active"}]}
            discipline.summary_for_review = summary_for_review
            certificates = types.ModuleType("hr.certificates")
            certificates.status_of = lambda db_, pid, on: [
                {"type_key": "olcc", "name": "OLCC Service Permit", "status": "expired",
                 "expires": date(2026, 9, 30), "flag": "May not serve alcohol"}]
            monkeypatch.setitem(sys.modules, "hr.discipline", discipline)
            monkeypatch.setitem(sys.modules, "hr.certificates", certificates)
            status, data, _ = await hr.call("GET", f"/api/reviews/{rid}", cookie=s["303"])
            side = data["side"]
            r = reviews.get(db, rid)
            # Points now: the period up to today (its end is the due day, still to come).
            assert calls == [("301", date.fromisoformat(r["period_start"]),
                              min(date.fromisoformat(r["period_end"]), today(hr)))]
            assert side["discipline"]["points"] == [{"track": "Discipline", "total": 2.5}]
            assert side["discipline"]["writeups"] == [{"date": "2026-09-01", "step": "Verbal", "status": "active"}]
            assert side["discipline"]["next_drop"] == {"date": "2027-01-02", "points": 1}
            assert side["certificates"] == [{"name": "OLCC Service Permit", "expires": "2026-09-30",
                                             "status": "Expired", "flag": "May not serve alcohol"}]
            # A part that fails is "Not available", not an error.
            discipline.summary_for_review = lambda *a: 1 / 0
            status, data, _ = await hr.call("GET", f"/api/reviews/{rid}", cookie=s["303"])
            assert status == 200 and data["side"]["discipline"] is None
    run(scenario())


def test_lists_sections_and_to_dos(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            annual = open_review(hr, "301")                        # self-review asked
            extra = open_review(hr, "302", kind="adhoc", due=today(hr) - timedelta(days=3))
            status, data, _ = await hr.call("GET", "/api/reviews", cookie=s["303"])
            todo = {i["id"]: i for i in data["todo"]}
            assert set(todo) == {annual, extra} and todo[extra]["overdue"] and todo[annual]["what"] == "Write it"
            status, data, _ = await hr.call("GET", "/api/reviews", cookie=s["301"])
            assert [(i["id"], i["what"]) for i in data["todo"]] == [(annual, "Fill in your self-review")]
            status, data, _ = await hr.call("GET", "/api/reviews", cookie=s["304"])
            assert data["todo"] == [] and [i["id"] for i in data["open"]] == [extra]    # Bo is in the Kitchen
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["303"])
            assert {"text": "2 reviews to write.", "link": "#/reviews"} in home["todos"]
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["301"])
            assert {"text": "Please fill in your self-review.", "link": "#/reviews"} in home["todos"]
            # A person's page.
            status, page, _ = await hr.call("GET", "/api/people/301", cookie=s["303"])
            sec = next(x for x in page["sections"] if x["name"] == "reviews")
            assert sec["can_start"] and [i["id"] for i in sec["items"]] == [annual] and "main" not in sec
            status, page, _ = await hr.call("GET", "/api/people/302", cookie=s["306"])
            sec = next(x for x in page["sections"] if x["name"] == "reviews")
            assert sec["main"]["name"] == "Pub" and sec["main"]["writer"] == "Max Ray"
            assert [g["name"] for g in sec["main"]["groups"]] == ["Pub", "Kitchen"]
            status, page, _ = await hr.call("GET", "/api/people/302", cookie=s["302"])
            sec = next(x for x in page["sections"] if x["name"] == "reviews")
            assert sec["items"] == [] and not sec["can_start"]        # not shared yet
            # HR's settings page carries the review settings.
            status, data, _ = await hr.call("GET", "/api/settings", cookie=s["306"])
            assert data["sections"]["reviews"]["values"]["reviews_annual"] == "anniversary"
            assert data["sections"]["reviews"]["values"]["reviews_forms"]["annual"] == "standard"
    run(scenario())


# ---------------------------------------------------------------- reminders and PDFs

def test_reminders_go_once_each(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db = hr.hdb
            due = today(hr) + timedelta(days=30)
            rid = open_review(hr, "302", kind="adhoc", due=due)
            base = len(inbox_kinds(hr, "303"))
            count = lambda pid, kind: inbox_kinds(hr, pid).count(kind)  # noqa: E731
            reviews.remind(db, due - timedelta(days=8))
            assert count("303", "review_reminder") == 0
            for _ in range(2):
                reviews.remind(db, due - timedelta(days=7))
            assert count("303", "review_reminder") == 1
            reviews.remind(db, due)
            reviews.remind(db, due + timedelta(days=3))
            reviews.remind(db, due + timedelta(days=7))
            assert count("303", "review_reminder") == 3 and count("306", "review_overdue") == 0
            for _ in range(2):
                reviews.remind(db, due + timedelta(days=14))
            assert count("303", "review_reminder") == 4
            assert count("306", "review_overdue") == 1 and count("307", "review_overdue") == 1
            assert count("302", "review_overdue") == 0 and len(inbox_kinds(hr, "303")) == base + 4
            # Waiting for approval and late: the HR admins who didn't write it.
            db.conn.execute("UPDATE reviews SET status = 'approval', submitted_by = '306' WHERE id = ?", (rid,))
            db.conn.commit()
            reviews.remind(db, due + timedelta(days=21))
            assert count("307", "review_approval") == 1 and count("306", "review_approval") == 0
            # A self-review: a reminder a week before.
            sid = open_review(hr, "301", due=due)
            assert count("301", "review_self") == 1
            reviews.remind(db, due - timedelta(days=7))
            reviews.remind(db, due - timedelta(days=7))
            assert count("301", "review_self") == 2
            # Shared and not acknowledged for a week: the employee, and once the writer.
            db.conn.execute("UPDATE reviews SET status = 'shared', shared_at = ?, self_status = 'done' "
                            "WHERE id = ?", (time.time() - 7 * 86400 - 60, sid))
            db.conn.commit()
            before = count("303", "review_reminder")
            for _ in range(2):
                reviews.remind(db, due + timedelta(days=1))
            assert count("301", "review_shared") == 1 and count("303", "review_reminder") == before + 1
            # Settings change the timing.
            db.set_settings({"reviews_remind_before": "0", "reviews_overdue_days": "30"})
            assert reviews.number(db, "reviews_remind_before") == 0
            db.set_settings({"reviews_overdue_days": "500"})
            assert reviews.number(db, "reviews_overdue_days") == 90
    run(scenario())


def test_the_pdf_waits_for_google_in_the_documents_queue(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            db = hr.hdb
            rid = open_review(hr, "302", kind="adhoc")
            await fill(hr, s["303"], rid)
            await hr.call("PUT", f"/api/reviews/{rid}/overall", {"recommendation": "Talk with HR"}, cookie=s["303"])
            await hr.call("POST", f"/api/reviews/{rid}/submit", {}, cookie=s["303"])
            await hr.call("POST", f"/api/reviews/{rid}/share", {"meeting_date": today(hr).isoformat()},
                          cookie=s["303"])
            await hr.call("POST", f"/api/reviews/{rid}/close", {"how": "paper"}, cookie=s["303"])
            await queue_done(hr)
            # Google isn't set up: the documents queue keeps it, without using up tries. HR sees why;
            # the writer only that it's coming.
            work = documents.pdf_work(db, "reviews", rid)
            assert (work["status"], work["code"], work["tries"]) == ("waiting", "not_set_up", 0)
            status, data, _ = await hr.call("GET", f"/api/reviews/{rid}", cookie=s["306"])
            assert data["pdf"]["ready"] is False and data["pdf"]["error"] == delivery.NO_GOOGLE
            status, data, _ = await hr.call("GET", f"/api/reviews/{rid}", cookie=s["303"])
            assert "error" not in data["pdf"]
            async with google(hr, monkeypatch) as fake:
                await queue_done(hr, time.time() + delivery.WAIT_SETUP + 1)
                assert len(fake.pdfs) == 1 and fake.names_in("Reviews") == [
                    f"{notify.local_day(db)} Extra review.pdf"]
            doc = documents.get(db, reviews.get(db, rid)["pdf_doc_id"])
            status, data, _ = await hr.call("GET", f"/api/reviews/{rid}", cookie=s["302"])
            assert data["pdf"] == {"ready": True, "link": documents.view_url(doc)}
            assert data["review"]["ack_word"] == "Acknowledged on paper"
            # Bo opens it; Ana (another employee) can't.
            for who, code in (("302", 200), ("301", 404)):
                resp = await hr.client.get(documents.view_url(doc), headers={
                    "Host": "hr.example.com", "Cookie": f"{signin.COOKIE}={s[who]}"})
                assert resp.status == code, who
    run(scenario())


# ---------------------------------------------------------------- settings and wiring

def test_review_settings(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr, confirmed=False)
            status, data, _ = await hr.call("PUT", "/api/settings/reviews", {"reviews_self": "all"},
                                            cookie=s["306"])
            assert (status, data["error"]) == (401, "reconfirm")
            assert (await hr.call("PUT", "/api/settings/reviews", {}, cookie=s["303"]))[0] == 403
            await hr.confirm(s["306"], "306")
            for bad in ({"reviews_self": "sometimes"}, {"reviews_gap_days": "lots"},
                        {"reviews_forms": {"annual": "nope"}}, {"reviews_forms": {"yearly": "standard"}}):
                assert (await hr.call("PUT", "/api/settings/reviews", bad, cookie=s["306"]))[0] == 400
            status, data, _ = await hr.call("PUT", "/api/settings/reviews", {
                "reviews_self": "all", "reviews_second": "all", "reviews_checkin_30": "1",
                "reviews_annual": "month", "reviews_annual_month": 13, "reviews_gap_days": 30}, cookie=s["306"])
            values = data["values"]
            assert status == 200 and values["reviews_self"] == "all" and values["reviews_annual_month"] == 12
            assert values["reviews_gap_days"] == 30 and values["reviews_checkin_30"] == "1"
            assert "Changed review settings" in actions(hr)
            # Every kind now asks for a self-review, and every review for HR's approval.
            rid = open_review(hr, "301", kind="adhoc")
            assert reviews.get(hr.hdb, rid)["self_status"] == "waiting"
            assert reviews.needs_approval(hr.hdb, reviews.get(hr.hdb, rid))
    run(scenario())


def test_reviews_are_wired_into_the_hr_portal(tmp_path):
    async def scenario():
        async with HR(tmp_path, enabled=False) as hr:
            assert reviews.nightly in hr.svc.daily_jobs   # (final PDFs: the documents queue)
            # Off: nothing made, nothing kept.
            assert hr.svc.db is None and not (tmp_path / "data" / "hr").exists()
            status, body, _ = await hr.call("GET", "/api/reviews")
            assert status == 503
        async with HR(tmp_path / "on") as hr:
            tables = {r[0] for r in hr.hdb.conn.execute("SELECT name FROM sqlite_master")}
            assert {"reviews", "review_forms", "review_comments", "review_notes", "review_people"} <= tables
            status, body, _ = await hr.call("GET", "/")
            assert '/static/hr-reviews.js?v=' in body
            status, body, _ = await hr.call("GET", "/static/hr-reviews.js")
            assert status == 200 and "registerPage" in body
            status, body, _ = await hr.call("GET", "/static/hr-reviews.css")
            assert status == 200
    run(scenario())


def test_final_pdfs_are_their_own_kind_of_document(tmp_path):
    """The "reviews" kind (the Reviews folder in Drive), opened by those who see the review, and
    made by the documents part's PDF queue."""
    assert documents.REGISTERED["reviews"] == ("Reviews", "managers", "Reviews")
    assert documents.OWNED["reviews"] == "reviews" and documents.CAN_OPEN["reviews"] is reviews.can_open
    assert documents.PDF_MAKERS["reviews"] == (reviews.pdf_page, reviews.pdf_saved)
    can_open = reviews.can_open
    assert all(can_open(None, rel, {}) for rel in ("self", "manager", "director", "hr"))
    assert not can_open(None, None, {})

    async def scenario():
        async with HR(tmp_path) as hr:
            k = documents.kind_by_key(hr.hdb, "reviews")
            assert (k["name"], k["folder"], k["visibility"]) == ("Reviews", "Reviews", "managers")
    run(scenario())
