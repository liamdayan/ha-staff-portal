"""The discipline workflow (design 7.3 to 7.10): the one manager form with its live preview and
the protected-leave question, HR's approval, step tasks and termination reviews (HR only),
write-ups from draft to signed (in the portal, in person with the employee's PIN and code,
refused with a witness, on paper), the signed PDF, appeals, voids, HR's adjustments, reminders,
and who sees what. The point system and its ledger are the real ones (policy.py, points.py: a
small published policy from tests/point_system.py); documents and their PDF queue are the real
part, with Google as the fake script (tests/fake_bridge.py).

Nothing here talks to a real Connecteam, Google, TT Hotel or Home Assistant.
"""

import asyncio
import io
import json
import time
from datetime import date, timedelta

import pytest

import point_system
from fake_bridge import html_text
from hr_helpers import CHANGE, HR, ct_user, next_code, queue_done
from test_hr_documents import google

from hr import delivery, discipline, documents, notify, probation, signin  # noqa: E402

MARIA = ct_user(401, "Maria", "Lopez", code="4821", groups=[501], mobile="541-555-0401")
BO = ct_user(402, "Bo", "Diaz", code="7302", groups=[502], mobile="541-555-0402")
MAX = ct_user(403, "Max", "Ray", code="9900", groups=[501], mobile="541-555-0403")
DEE = ct_user(404, "Dee", "Fox", code="5566", groups=[502], mobile="541-555-0404")
HANA = ct_user(405, "Hana", "Ito", code="7788", groups=[502], mobile="541-555-0405")
HALE = ct_user(406, "Hale", "Moss", code="6655", groups=[502], mobile="541-555-0406")
WHO = {"maria": "401", "bo": "402", "max": "403", "dee": "404", "hana": "405", "hale": "406"}
SECRET_MANAGER = "MGRNOTE-77"
SECRET_HR = "HRNOTE-55"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def engine(monkeypatch):
    return point_system.install(monkeypatch)


async def setup(hr: HR, *, confirmed: bool = True) -> dict:
    """Maria (Pub) and Bo (Kitchen); Max manages the Pub; Dee is a director; Hana and Hale are
    HR admins. Returns a session for each."""
    await hr.add_people(MARIA, BO, MAX, DEE, HANA, HALE)
    hr.give("403", "manager", {"501": True})
    hr.give("404", "director")
    hr.give("405", "hr_admin")
    hr.give("406", "hr_admin")
    sessions = {name: signin.new_session(hr.hdb, pid, "127.0.0.1", "test") for name, pid in WHO.items()}
    hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = ?",
                        (time.time() if confirmed else time.time() - 600,))
    hr.hdb.conn.commit()
    return sessions


def today(hr: HR) -> date:
    return probation.today_of(hr.hdb)


def day(hr: HR, offset: int = 0) -> str:
    return (today(hr) + timedelta(days=offset)).isoformat()


def section(page: dict, name: str) -> dict | None:
    return next((s for s in page["sections"] if s["name"] == name), None)


def inbox(hr: HR, name: str) -> list[tuple]:
    return [(i["kind"], i["title"], i["urgent"]) for i in notify.inbox(hr.hdb, WHO[name])]


def kinds(hr: HR, name: str) -> list[str]:
    return [k for k, _, _ in inbox(hr, name)]


RUDE = {"offence": "rude", "what": "Shouted at a guest at the bar.", "points": ""}
WRITEUP = {"improvement": "Stay calm, and ask a manager for help.",
           "consequences": "Another incident may lead to a final written warning.",
           "support": "A refresher on guest service.", "manager_note": SECRET_MANAGER}


async def record(hr, s, who="max", pid="401", **extra) -> dict:
    status, data, _ = await hr.call("POST", f"/api/people/{pid}/incidents",
                                    {**RUDE, "happened": day(hr, -1), **extra}, s[who])
    assert status == 200, data
    return data


async def issued(hr, s, *, approver="hana", writeup_approver="hale") -> int:
    """An incident with its write-up, both approved by HR: the write-up's ID, ready to deliver."""
    data = await record(hr, s, writeup=dict(WRITEUP))
    status, r, _ = await hr.call("POST", f"/api/incidents/{data['incident']}/approve", {}, s[approver])
    assert status == 200 and r["writeup"] == data["writeup"], r
    status, r, _ = await hr.call("POST", f"/api/writeups/{data['writeup']}/approve", {}, s[writeup_approver])
    assert status == 200, r
    return data["writeup"]




# ---------------------------------------------------------------- until a policy is published

def test_nothing_is_recorded_until_a_point_system_is_published(tmp_path, engine):
    pol, pts = engine

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            status, form, _ = await hr.call("GET", "/api/people/401/incident-form", cookie=s["max"])
            assert status == 200 and form == {"set_up": False, "message": discipline.NOT_SET_UP}
            status, data, _ = await hr.call("POST", "/api/people/401/incidents",
                                            {**RUDE, "happened": day(hr)}, s["max"])
            assert status == 409 and data["not_set_up"] and "isn't set up yet" in data["error"]
            status, data, _ = await hr.call("POST", "/api/people/401/incident-preview",
                                            {**RUDE, "happened": day(hr)}, s["max"])
            assert status == 409
            assert hr.hdb.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0
            # The section says so plainly to staff; employees see nothing yet.
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["max"])
            sec = section(page, "discipline")
            assert sec["set_up"] is False and sec["notes"] == [discipline.NOT_SET_UP]
            assert sec["can"]["record"] is False
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["maria"])
            assert section(page, "discipline") is None
            # Probation still works: a new hire's introductory period from their start date.
            hr.hdb.update_person("401", start_date=day(hr, -10))
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["maria"])
            [prob] = section(page, "probation")["probations"]
            assert prob["ends"] == day(hr, 79) and prob["title"] == "Introductory period"   # its 90th day
            # Published: the form opens, with the protected-leave choices.
            pol.publish()
            status, form, _ = await hr.call("GET", "/api/people/401/incident-form", cookie=s["max"])
            assert form["set_up"] and [o["key"] for o in form["offences"]][:3] == ["late", "ncns", "rude"]
            late = form["offences"][0]
            assert late["attendance"] and (late["min"], late["max"], late["default"]) == (0.5, 1.5, 1.0)
            assert [l["name"] for l in form["leaves"]][:3] == ["No", "Oregon sick time", "Paid Leave Oregon"]
    run(scenario())


# ---------------------------------------------------------------- the manager form and HR

def test_the_form_previews_then_hr_approves_and_a_step_is_due(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            pts.add("401", 4, today(hr) - timedelta(days=30))
            status, p, _ = await hr.call("POST", "/api/people/401/incident-preview",
                                         {**RUDE, "happened": day(hr, -1)}, s["max"])
            assert status == 200, p
            assert p["text"].startswith("Maria: 4 → 6 points. Reaches Written warning. A write-up is needed.")
            assert p["needs_writeup"] and p["step"]["name"] == "Written warning"
            # Points outside the offence's range, or changed without a reason, are refused.
            status, p, _ = await hr.call("POST", "/api/people/401/incident-preview",
                                         {**RUDE, "happened": day(hr), "points": 5}, s["max"])
            assert p["errors"] == ["Managers may choose 1 to 3 points for Rude to a guest."]
            status, data, _ = await hr.call("POST", "/api/people/401/incidents",
                                            {**RUDE, "happened": day(hr), "points": 5}, s["max"])
            assert status == 400 and "1 to 3 points" in data["error"]
            status, data, _ = await hr.call("POST", "/api/people/401/incidents",
                                            {**RUDE, "happened": day(hr), "points": 3}, s["max"])
            assert status == 400 and data["error"] == "Say why you chose different points."
            status, data, _ = await hr.call("POST", "/api/people/401/incidents",
                                            {**RUDE, "happened": day(hr, 1)}, s["max"])
            assert status == 400 and "not in the future" in data["error"]
            # Recorded with the write-up on the same form: with HR, not counting yet.
            data = await record(hr, s, writeup=dict(WRITEUP))
            assert data["message"] == "Sent to HR for approval."
            inc = discipline.get_incident(hr.hdb, data["incident"])
            w = discipline.get_writeup(hr.hdb, data["writeup"])
            assert inc["status"] == "with_hr" and w["status"] == "with_hr" and w["step_key"] == "written"
            assert pts.totals(None, "401", today(hr)) == {"discipline": 4.0}
            assert "incident_submitted" in kinds(hr, "hana") and "incident_submitted" in kinds(hr, "hale")
            assert kinds(hr, "max") == [] and kinds(hr, "maria") == []
            # The text people typed is sealed.
            raw = json.dumps([dict(r) for r in hr.hdb.conn.execute("SELECT * FROM incidents")] +
                             [dict(r) for r in hr.hdb.conn.execute("SELECT * FROM writeups")])
            assert "Shouted" not in raw and SECRET_MANAGER not in raw and "Stay calm" not in raw
            # Only HR approves, and never their own work.
            status, data2, _ = await hr.call("POST", f"/api/incidents/{inc['id']}/approve", {}, s["max"])
            assert status == 403
            status, r, _ = await hr.call("POST", f"/api/incidents/{inc['id']}/approve", {}, s["hana"])
            assert status == 200 and "Written warning is due" in r["message"], r
            # Counted from today, expiring from the day it happened.
            [line] = pts.lines("401", "incident")
            assert (line["incident_id"], line["q"], line["happened"], line["counts_from"], line["offence"]) == \
                (str(inc["id"]), 8, today(hr) - timedelta(days=1), today(hr), "rude")
            assert pts.totals(None, "401", today(hr)) == {"discipline": 6.0}
            [task] = [dict(t) for t in hr.hdb.conn.execute("SELECT * FROM discipline_tasks")]
            assert task["kind"] == "step" and task["writeup_id"] == w["id"] and not task["hr_only"]
            assert task["reason"] == "Points 6 ≥ 6 → Written warning"
            assert ("step_due", "Maria Lopez: Written warning is due. Points 6 ≥ 6 → Written warning", 1) \
                in inbox(hr, "max")
            assert "step_due" in kinds(hr, "hale")
            # The manager's "My team" and HR's queue.
            status, team, _ = await hr.call("GET", "/api/discipline/team", cookie=s["max"])
            row = next(p for p in team["people"] if p["id"] == "401")
            assert row["points"] == 6.0 and row["tasks"] == 1 and [t["kind"] for t in team["tasks"]] == ["step"]
            status, q, _ = await hr.call("GET", "/api/discipline/queue", cookie=s["hale"])
            assert [x["id"] for x in q["writeups"]] == [w["id"]] and q["incidents"] == []
            status, q, _ = await hr.call("GET", "/api/discipline/queue", cookie=s["max"])
            assert status == 403
            # The incident as the manager sees it.
            status, view, _ = await hr.call("GET", f"/api/incidents/{inc['id']}", cookie=s["max"])
            assert view["status_word"] == "Approved" and view["step"] == "Written warning"
            assert view["what"] == "Shouted at a guest at the bar." and "hr_reason" not in view
            assert [h["what"] for h in view["history"]] == ["Recorded", "Write-up sent to HR",
                                                            "Approved by HR at 2 points"]
    run(scenario())


def test_protected_leave_scores_zero_and_never_leads_to_a_step(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            pts.add("401", 9, today(hr) - timedelta(days=10))
            late = {"offence": "late", "happened": day(hr, -2), "what": "Came in 40 minutes late."}
            status, data, _ = await hr.call("POST", "/api/people/401/incidents", late, s["max"])
            assert status == 400 and data["error"] == "Answer the question: was this protected leave?"
            status, p, _ = await hr.call("POST", "/api/people/401/incident-preview",
                                         {**late, "protected": "sick"}, s["max"])
            assert p["text"] == ("Maria: 9 → 9 points. Protected leave scores 0 and never leads to "
                                 "a step. HR approves it before it counts.")
            assert p["protected"] and p["step"] is None and not p["needs_writeup"]
            status, data, _ = await hr.call("POST", "/api/people/401/incidents",
                                            {**late, "protected": "sick", "points": 1.5}, s["max"])
            assert status == 200, data
            inc = discipline.get_incident(hr.hdb, data["incident"])
            assert inc["final_points"] == 0 and inc["protected"] == "sick"
            status, r, _ = await hr.call("POST", f"/api/incidents/{inc['id']}/approve",
                                         {"points": 2, "reason": "Too late"}, s["hana"])
            assert status == 400 and r["error"] == "Protected leave always scores 0."
            status, r, _ = await hr.call("POST", f"/api/incidents/{inc['id']}/approve", {}, s["hana"])
            assert status == 200 and r["step"] is None
            assert hr.hdb.conn.execute("SELECT COUNT(*) FROM discipline_tasks").fetchone()[0] == 0
            status, view, _ = await hr.call("GET", f"/api/incidents/{inc['id']}", cookie=s["hana"])
            assert view["protected"] == "Oregon sick time" and view["points_now"] == 0
            assert view["can"]["adjust"] is False
            # The point system was told about the leave.
            assert pts.contexts[-1]["protected"] == "sick"
    run(scenario())


def test_who_may_record_approve_and_see(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            body = {**RUDE, "happened": day(hr)}
            assert (await hr.call("POST", "/api/people/402/incidents", body, s["maria"]))[0] == 403
            assert (await hr.call("POST", "/api/people/401/incidents", body, s["maria"]))[0] == 403
            assert (await hr.call("POST", "/api/people/402/incidents", body, s["max"]))[0] == 404   # Kitchen
            status, data, _ = await hr.call("POST", "/api/people/403/incidents", body, s["max"])
            assert status == 403 and "own file" in data["error"]
            assert (await hr.call("POST", "/api/people/401/incidents", body, s["dee"]))[0] == 200
            # HR records too, but another HR admin approves it.
            status, data, _ = await hr.call("POST", "/api/people/402/incidents", body, s["hana"])
            assert status == 200
            iid = data["incident"]
            status, r, _ = await hr.call("POST", f"/api/incidents/{iid}/approve", {}, s["hana"])
            assert status == 403 and "approves their own work" in r["error"]
            status, view, _ = await hr.call("GET", f"/api/incidents/{iid}", cookie=s["hana"])
            assert view["can"]["approve"] is False
            assert (await hr.call("POST", f"/api/incidents/{iid}/approve", {}, s["hale"]))[0] == 200
            # Max doesn't see the Kitchen; employees never open incidents.
            assert (await hr.call("GET", f"/api/incidents/{iid}", cookie=s["max"]))[0] == 404
            assert (await hr.call("GET", f"/api/incidents/{iid}", cookie=s["bo"]))[0] == 403
            # An HR admin's own file: another HR admin records it, and they never see it.
            status, data, _ = await hr.call("POST", "/api/people/405/incidents", body, s["hale"])
            assert status == 200
            status, r, _ = await hr.call("GET", f"/api/incidents/{data['incident']}", cookie=s["hana"])
            assert status == 403 and "own file" in r["error"]
            status, q, _ = await hr.call("GET", "/api/discipline/queue", cookie=s["hana"])
            assert data["incident"] not in [i["id"] for i in q["incidents"]]
            assert "incident_submitted" not in [k for k, t, _ in inbox(hr, "hana") if "Hana" in t]
            status, q, _ = await hr.call("GET", "/api/discipline/queue", cookie=s["hale"])
            assert [i["own_work"] for i in q["incidents"] if i["id"] == data["incident"]] == [True]
    run(scenario())


def test_hr_returns_an_incident_and_the_manager_sends_it_again(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            pts.add("401", 4, today(hr) - timedelta(days=30))
            data = await record(hr, s, writeup=dict(WRITEUP))
            iid, wid = data["incident"], data["writeup"]
            status, r, _ = await hr.call("POST", f"/api/incidents/{iid}/return", {"note": ""}, s["hana"])
            assert status == 400
            status, r, _ = await hr.call("POST", f"/api/incidents/{iid}/return",
                                         {"note": "Which guest? Add the table number."}, s["hana"])
            assert status == 200
            assert ("incident_returned", "Maria Lopez: HR returned an incident to you with a note.", 0) in inbox(hr, "max")
            assert discipline.get_writeup(hr.hdb, wid)["status"] == "draft"
            status, view, _ = await hr.call("GET", f"/api/incidents/{iid}", cookie=s["max"])
            assert view["return_note"] == "Which guest? Add the table number." and view["can"]["edit"]
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["max"])
            assert any("returned an incident" in t["text"] for t in home["todos"])
            # Only the person who recorded it changes it.
            changed = {**RUDE, "happened": day(hr, -1), "what": "Shouted at the guest at table 12."}
            status, r, _ = await hr.call("PUT", f"/api/incidents/{iid}", changed, s["dee"])
            assert status == 403
            status, r, _ = await hr.call("PUT", f"/api/incidents/{iid}", changed, s["max"])
            assert status == 200, r
            inc = discipline.get_incident(hr.hdb, iid)
            assert inc["status"] == "with_hr"
            assert discipline.opened(inc["what"], "incident.what") == "Shouted at the guest at table 12."
            assert discipline.get_writeup(hr.hdb, wid)["status"] == "with_hr"
            assert sum(k == "incident_submitted" for k in kinds(hr, "hana")) == 2
            # Once approved it's never rewritten: the database itself refuses.
            assert (await hr.call("POST", f"/api/incidents/{iid}/approve", {}, s["hana"]))[0] == 200
            assert (await hr.call("PUT", f"/api/incidents/{iid}", changed, s["max"]))[0] == 400
            with pytest.raises(Exception, match="never rewritten"):
                hr.hdb.conn.execute("UPDATE incidents SET final_points = 0 WHERE id = ?", (iid,))
            hr.hdb.conn.rollback()
    run(scenario())


def test_a_termination_review_goes_to_hr_only_and_is_never_automatic(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            status, p, _ = await hr.call("POST", "/api/people/401/incident-preview",
                                         {"offence": "theft", "happened": day(hr)}, s["max"])
            assert p["step"] is None and "It reaches a step HR decides on." in p["text"]
            assert "Termination" not in p["text"]
            status, p, _ = await hr.call("POST", "/api/people/401/incident-preview",
                                         {"offence": "theft", "happened": day(hr)}, s["hana"])
            assert "Reaches Termination review. HR decides what happens next." in p["text"]
            data = await record(hr, s, offence="theft", what="Took cash from the till.")
            iid = data["incident"]
            status, r, _ = await hr.call("POST", f"/api/incidents/{iid}/approve", {}, s["hana"])
            assert status == 200 and r["step"] == "termination_review"
            [task] = [dict(t) for t in hr.hdb.conn.execute("SELECT * FROM discipline_tasks")]
            assert task["kind"] == "termination" and task["hr_only"] and task["writeup_id"] is None
            assert ("termination_review", 1) in [(k, u) for k, _, u in inbox(hr, "hale")]
            # The manager and the employee learn nothing of it.
            assert not {"step_due", "termination_review"} & set(kinds(hr, "max"))
            assert kinds(hr, "maria") == []
            status, view, _ = await hr.call("GET", f"/api/incidents/{iid}", cookie=s["max"])
            assert view["step"] is None and view["step_reason"] is None
            status, team, _ = await hr.call("GET", "/api/discipline/team", cookie=s["max"])
            assert team["tasks"] == []
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["max"])
            assert section(page, "discipline")["tasks"] == []
            assert "Termination" not in json.dumps(page)
            # HR chooses one of three outcomes, with a reason, after a fresh code.
            status, q, _ = await hr.call("GET", "/api/discipline/queue", cookie=s["hale"])
            [t] = q["tasks"]
            assert t["word"] == "Termination review"
            url = f"/api/discipline-tasks/{t['id']}/decide"
            assert (await hr.call("POST", url, {"outcome": "terminate"}, s["hale"]))[0] == 400
            assert (await hr.call("POST", url, {"outcome": "fire", "reason": "x"}, s["hale"]))[0] == 400
            assert (await hr.call("POST", url, {"outcome": "terminate", "reason": "x"}, s["max"]))[0] == 403
            assert (await hr.call("POST", f"/api/discipline-tasks/{t['id']}/done", {}, s["hale"]))[0] == 409
            status, r, _ = await hr.call("POST", url, {"outcome": "terminate",
                                                       "reason": "Theft confirmed on camera."}, s["hale"])
            assert status == 200 and "Archive them in Connecteam by hand" in r["message"]
            tasks = {t["kind"]: dict(t) for t in hr.hdb.conn.execute("SELECT * FROM discipline_tasks")}
            assert tasks["termination"]["outcome"] == "terminate" and tasks["termination"]["status"] == "done"
            assert tasks["separation"]["status"] == "open" and tasks["separation"]["hr_only"]
            status, r, _ = await hr.call("POST", f"/api/discipline-tasks/{tasks['separation']['id']}/done",
                                         {"note": "Archived."}, s["hale"])
            assert status == 200
    run(scenario())


# ---------------------------------------------------------------- write-ups and signatures

def test_a_write_up_from_draft_to_signed_in_the_portal(tmp_path, engine, monkeypatch):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            s = await setup(hr)
            pts.add("401", 4, today(hr) - timedelta(days=30))
            data = await record(hr, s, writeup=dict(WRITEUP))
            wid = data["writeup"]
            assert (await hr.call("POST", f"/api/incidents/{data['incident']}/approve", {}, s["hana"]))[0] == 200
            status, r, _ = await hr.call("PUT", f"/api/writeups/{wid}", {"hr_note": SECRET_HR}, s["hana"])
            assert status == 200
            # Not shown to the employee before it's delivered; managers never see HR's note.
            assert (await hr.call("GET", f"/api/writeups/{wid}", cookie=s["maria"]))[0] == 404
            status, raw, resp = await hr.call("GET", f"/api/writeups/{wid}", cookie=s["max"])
            assert status == 200 and SECRET_HR not in json.dumps(raw) and raw["manager_note"] == SECRET_MANAGER
            assert raw["texts"]["policy"] == "Treat every guest with courtesy."
            # HR sends it back; the manager edits and sends it again.
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/return",
                                         {"note": "Add what support we'll give."}, s["hale"])
            assert status == 200 and "writeup_returned" in kinds(hr, "max")
            status, r, _ = await hr.call("PUT", f"/api/writeups/{wid}",
                                         {"support": "Two shadow shifts with Ana."}, s["max"])
            assert status == 200 and r["writeup"]["texts"]["support"] == "Two shadow shifts with Ana."
            assert (await hr.call("POST", f"/api/writeups/{wid}/submit", {}, s["max"]))[0] == 200
            assert (await hr.call("POST", f"/api/writeups/{wid}/approve", {}, s["max"]))[0] == 403
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/approve", {}, s["hale"])
            assert status == 200 and r["writeup"]["status"] == "approved"
            assert "writeup_ready" in kinds(hr, "max")
            # Issued records are never rewritten.
            assert (await hr.call("PUT", f"/api/writeups/{wid}", {"support": "None"}, s["max"]))[0] == 400
            # Delivered through the portal: Maria signs it there.
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/send", {}, s["max"])
            assert status == 200 and r["writeup"]["status_word"] == "Delivered: waiting for signature"
            assert ("writeup_to_sign", "You have a write-up to read and sign.", 0) in inbox(hr, "maria")
            status, mine, _ = await hr.call("GET", f"/api/writeups/{wid}", cookie=s["maria"])
            text = json.dumps(mine)
            assert status == 200 and SECRET_MANAGER not in text and SECRET_HR not in text
            assert "manager_note" not in mine and "hr_note" not in mine and "return_note" not in mine
            assert "Add what support" not in text
            assert mine["statement"].startswith("Signing confirms you received this write-up. It "
                                                "doesn't mean you agree with it.")
            assert mine["can"]["sign"] and mine["can"]["appeal"] and mine["step"] == "Written warning"
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["maria"])
            assert {"text": "You have a write-up to read and sign.", "link": f"#/writeup/{wid}"} in home["todos"]
            # A wrong PIN counts towards her lock, like a sign-in.
            sign = {"name": "Maria Lopez", "comment": "I was covering two sections.", "pin": "0000"}
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/sign", sign, s["maria"])
            assert status == 403 and r["error"] == discipline.SIGN_FAILED
            assert hr.hdb.person("401")["failed"] == 1
            assert (await hr.call("POST", f"/api/writeups/{wid}/sign", {**sign, "pin": "4821"}, s["max"]))[0] == 403
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/sign", {**sign, "pin": "4821"}, s["maria"])
            assert status == 200, r
            assert r["writeup"]["status"] == "active" and r["writeup"]["comment"] == "I was covering two sections."
            await queue_done(hr)
            # The signed PDF: made through the documents part's PDF queue, filed as a "writeups"
            # document, and in Maria's Discipline folder in Drive.
            w = discipline.get_writeup(hr.hdb, wid)
            filed = documents.get(hr.hdb, w["pdf_doc_id"])
            assert documents.kind(hr.hdb, filed["kind_id"])["key"] == "writeups"
            assert (filed["mime"], documents.title_of(filed), filed["doc_date"]) == (
                "application/pdf", "Written warning - Conduct (signed)", day(hr))
            assert fake.names_in("Discipline") == [f"{day(hr)} Written warning - Conduct (signed).pdf"]
            [page] = fake.pdfs
            text = " ".join(html_text(page))
            assert SECRET_MANAGER not in page and SECRET_HR not in page and "I was covering two sections." in text
            title, blocks = discipline.pdf_blocks(hr.hdb, w)
            assert ["signature", "Employee"] == list(blocks[-2][:2]) and blocks[-2][2] == "Maria Lopez"
            # The step task closed when it was delivered; Max sees how it was signed.
            assert hr.hdb.conn.execute("SELECT status FROM discipline_tasks").fetchone()[0] == "done"
            status, view, _ = await hr.call("GET", f"/api/writeups/{wid}", cookie=s["max"])
            assert view["method_word"] == "Signed in the HR portal" and view["pdf"]["filed"]
            assert view["pdf_url"] == documents.view_url(filed)
            status, mine, _ = await hr.call("GET", f"/api/writeups/{wid}", cookie=s["maria"])
            assert mine["pdf_url"] == view["pdf_url"] and "pdf" not in mine
            assert (await hr.call("POST", f"/api/writeups/{wid}/sign", {**sign, "pin": "4821"}, s["maria"]))[0] == 409
    run(scenario())


def test_the_pdf_waits_while_google_drive_isnt_set_up(tmp_path, engine, monkeypatch):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            wid = await issued(hr, s)
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/refused", {"witness_name": "Ana Cruz"}, s["max"])
            assert status == 200
            await queue_done(hr)
            # It waits in the documents queue, without using up tries (HR sees why).
            work = documents.pdf_work(hr.hdb, "writeups", wid)
            assert (work["status"], work["code"], work["tries"]) == ("waiting", "not_set_up", 0)
            assert discipline.get_writeup(hr.hdb, wid)["pdf_doc_id"] is None
            _, view, _ = await hr.call("GET", f"/api/writeups/{wid}", cookie=s["hana"])
            assert view["pdf"]["status"] == "waiting" and "isn't set up yet" in view["pdf"]["error"]
            _, view, _ = await hr.call("GET", f"/api/writeups/{wid}", cookie=s["max"])
            assert view["pdf"]["error"] is None and "pdf_url" not in view
            _, queue, _ = await hr.call("GET", "/api/discipline/queue", cookie=s["hana"])
            assert queue["waiting_pdfs"] == 1 and queue["pdfs"] == []
            async with google(hr, monkeypatch) as fake:
                await queue_done(hr)
                assert fake.pdfs == []                           # not due yet
                await queue_done(hr, time.time() + delivery.WAIT_SETUP + 1)
                assert discipline.get_writeup(hr.hdb, wid)["pdf_doc_id"]
                # Refused: the PDF says so, with the witness.
                text = " ".join(html_text(fake.pdfs[-1]))
                assert "The employee refused to sign." in text and "Ana Cruz" in text
                assert fake.names_in("Discipline") == [f"{day(hr)} Coaching - Conduct (signed).pdf"]
                # It keeps failing: HR's Needs attention page (and the Discipline page) has it,
                # and Retry tries it again.
                second = await issued(hr, s)
                fake.fail_next(code="unavailable", message="Google is busy.", op="pdf", times=99)
                status, r, _ = await hr.call("POST", f"/api/writeups/{second}/refused", {"witness_name": "Ana Cruz"}, s["max"])
                assert status == 200
                for n in range(delivery.MAX_TRIES):
                    await queue_done(hr, time.time() + (n + 1) * 2 * 86400)
                assert documents.pdf_work(hr.hdb, "writeups", second)["status"] == "attention"
                fake.failures.clear()
                _, queue, _ = await hr.call("GET", "/api/discipline/queue", cookie=s["hana"])
                assert [p["id"] for p in queue["pdfs"]] == [second]
                _, attention, _ = await hr.call("GET", "/api/documents/attention", cookie=s["hana"])
                assert [i["target"] for i in attention["stuck"]] == ["pdf"]
                _, view, _ = await hr.call("GET", f"/api/writeups/{second}", cookie=s["hana"])
                assert view["can"]["pdf_retry"]
                status, r, _ = await hr.call("POST", f"/api/writeups/{second}/pdf-retry", {}, s["hana"])
                assert status == 200
                await queue_done(hr)
                assert discipline.get_writeup(hr.hdb, second)["pdf_doc_id"]
                assert (await hr.call("POST", f"/api/writeups/{second}/pdf-retry", {}, s["hana"]))[0] == 409
    run(scenario())


def test_signing_in_person_refusing_with_a_witness_and_paper(tmp_path, engine, monkeypatch):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            s = await setup(hr)
            first = await issued(hr, s)
            secret = hr.enrol("401")
            # In person: Maria reads it on Max's phone, without the manager's notes.
            status, view, _ = await hr.call("GET", f"/api/writeups/{first}/in-person", cookie=s["max"])
            assert status == 200 and view["needs_code"] and view["can_sign_here"]
            assert SECRET_MANAGER not in json.dumps(view) and view["texts"]["improvement"]
            url = f"/api/writeups/{first}/sign-in-person"
            sign = {"name": "Maria Lopez", "comment": "", "pin": "4821"}
            status, r, _ = await hr.call("POST", url, sign, s["max"])
            assert status == 403                                     # her code is needed too
            assert (await hr.call("POST", url, {**sign, "code": next_code(hr, "401", secret)}, s["maria"]))[0] == 403
            status, r, _ = await hr.call("POST", url, {**sign, "code": next_code(hr, "401", secret)}, s["max"])
            assert status == 200 and r["writeup"]["method_word"] == "Signed in person"
            w = discipline.get_writeup(hr.hdb, first)
            assert w["issued_on"] == day(hr) and w["ends_on"] == day(hr, 89)      # Coaching: 90 days, from today
            # Refused, with another portal user as witness confirming with their PIN.
            second = await issued(hr, s)
            url = f"/api/writeups/{second}/refused"
            status, r, _ = await hr.call("POST", url, {}, s["max"])
            assert status == 400
            status, r, _ = await hr.call("POST", url, {"witness": "541-555-0401", "witness_pin": "4821"}, s["max"])
            assert status == 403                                     # not Maria herself
            status, r, _ = await hr.call("POST", url, {"witness": "541-555-0402", "witness_pin": "1111"}, s["max"])
            assert status == 403 and hr.hdb.person("402")["failed"] == 1
            status, r, _ = await hr.call("POST", url, {"witness": "541-555-0402", "witness_pin": "7302"}, s["max"])
            assert status == 200 and r["writeup"]["witness"] == "Bo Diaz"
            assert r["writeup"]["method_word"] == "Refused to sign (witnessed)"
            assert discipline.get_writeup(hr.hdb, second)["witness_id"] == "402"
            # Signed on paper: the photo of the signed page is filed with it.
            third = await issued(hr, s)
            from PIL import Image
            buf = io.BytesIO()
            Image.new("RGB", (40, 30), "white").save(buf, "JPEG")
            upload = {**CHANGE, "Content-Type": "application/octet-stream",
                      "Cookie": f"{signin.COOKIE}={s['max']}"}
            resp = await hr.client.post(f"/api/writeups/{third}/paper", data=b"not a picture", headers=upload)
            assert resp.status == 400 and "Only photos" in (await resp.json())["error"]
            resp = await hr.client.post(f"/api/writeups/{third}/paper", data=buf.getvalue(), headers=upload)
            r = await resp.json()
            assert resp.status == 200, r
            assert r["writeup"]["method_word"] == "Signed on paper"
            photo = documents.get(hr.hdb, discipline.get_writeup(hr.hdb, third)["photo_doc_id"])
            assert documents.kind(hr.hdb, photo["kind_id"])["key"] == "writeups" and photo["mime"] == "image/jpeg"
            steps = [discipline.get_writeup(hr.hdb, x)["step_name"] for x in (first, second, third)]
            assert documents.title_of(photo) == f"{steps[2]} (signed on paper)"
            assert r["writeup"]["photo_url"] == documents.view_url(photo)
            resp = await hr.client.post(f"/api/writeups/{third}/paper", data=buf.getvalue(), headers=upload)
            assert resp.status == 409                                 # already signed
            # Maria and Max open the photo through the documents part; another employee can't.
            for who, code in (("maria", 200), ("max", 200), ("bo", 404)):
                resp = await hr.client.get(r["writeup"]["photo_url"], headers={
                    "Host": "hr.example.com", "Cookie": f"{signin.COOKIE}={s[who]}"})
                assert resp.status == code, who
            await queue_done(hr)
            assert len(fake.pdfs) == 3
            assert fake.names_in("Discipline") == sorted(
                [f"{day(hr)} {steps[2]} (signed on paper).jpg"] + [f"{day(hr)} {x} - Conduct (signed).pdf" for x in steps])
    run(scenario())


def test_unsigned_write_ups_remind_the_manager(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            wid = await issued(hr, s)
            assert (await hr.call("POST", f"/api/writeups/{wid}/send", {}, s["max"]))[0] == 200
            assert discipline.reminders(hr.hdb, today(hr)) == 0
            later = time.time() + 3 * 86400
            assert discipline.reminders(hr.hdb, today(hr), later) == 1
            [(kind, title, _)] = [i for i in inbox(hr, "max") if i[0] == "writeup_overdue"]
            assert title.endswith("hasn't signed the write-up from " + day(hr) + " yet. It counts either way.")
            assert discipline.reminders(hr.hdb, today(hr), later + 3600) == 0
            assert discipline.reminders(hr.hdb, today(hr) + timedelta(days=3), later + 3 * 86400) == 1
            status, q, _ = await hr.call("GET", "/api/discipline/queue", cookie=s["hana"])
            assert [u["id"] for u in q["unsigned"]] == [wid]
            # It counts either way: it's Maria's current step.
            body = pol.current(None, today(hr))["body"]
            assert discipline.current_step(hr.hdb, body, "401", today(hr))[0] == "coaching"
    run(scenario())


# ---------------------------------------------------------------- appeals and voids

def test_appeals_upheld_amended_and_voided(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            early = await issued(hr, s)              # Coaching (2 points)
            later = await issued(hr, s)              # Verbal warning (4 points)
            for wid in (early, later):
                assert (await hr.call("POST", f"/api/writeups/{wid}/send", {}, s["max"]))[0] == 200
                status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/sign",
                                             {"name": "Maria Lopez", "pin": "4821"}, s["maria"])
                assert status == 200
            # Maria appeals the early one; only she can, once.
            url = f"/api/writeups/{early}/appeal"
            assert (await hr.call("POST", url, {"text": "x"}, s["max"]))[0] == 403
            assert (await hr.call("POST", url, {"text": ""}, s["maria"]))[0] == 400
            status, r, _ = await hr.call("POST", url, {"text": "I was helping another guest."}, s["maria"])
            assert status == 200 and r["writeup"]["appeal"]["word"] == "Waiting for HR"
            assert r["writeup"]["under_appeal"]
            assert (await hr.call("POST", url, {"text": "Again"}, s["maria"]))[0] == 400
            assert "appeal_submitted" in kinds(hr, "hana") and "appeal_submitted" in kinds(hr, "hale")
            aid = r["writeup"]["appeal"]["id"]
            # The HR admin who approved the write-up doesn't decide its appeal.
            decide = f"/api/appeals/{aid}/decide"
            status, r, _ = await hr.call("POST", decide, {"outcome": "voided", "note": "x"}, s["hale"])
            assert status == 403 and "approves their own work" in r["error"]
            assert (await hr.call("POST", decide, {"outcome": "voided", "note": ""}, s["hana"]))[0] == 400
            status, r, _ = await hr.call("POST", decide, {"outcome": "voided",
                                                          "note": "The camera shows she was helping."}, s["hana"])
            assert status == 200, r
            w = discipline.get_writeup(hr.hdb, early)
            inc = discipline.get_incident(hr.hdb, w["incident_id"])
            assert w["status"] == "voided" and inc["status"] == "voided"
            [line] = [ln for ln in pts.lines("401", "incident") if ln["incident_id"] == str(inc["id"])]
            assert line["id"] in {v["target_id"] for v in pts.lines("401", "void")}
            assert pts.totals(None, "401", today(hr)) == {"discipline": 2.0}
            # Maria and Max see only "Withdrawn on"; HR sees everything.
            for who in ("maria", "max"):
                status, view, _ = await hr.call("GET", f"/api/writeups/{early}", cookie=s[who])
                assert view["status_word"] == f"Withdrawn on {day(hr)}" and "texts" not in view
                assert "helping" not in json.dumps(view)
            status, view, _ = await hr.call("GET", f"/api/writeups/{early}", cookie=s["hana"])
            assert view["void_reason"] == "Appeal: The camera shows she was helping."
            assert view["appeal"]["word"] == "Voided"
            assert ("appeal_decided", "HR decided your appeal. Open it to read the decision.", 0) in inbox(hr, "maria")
            assert "appeal_decided" in kinds(hr, "max")
            # The later write-up is checked again: with 2 points it would now be Coaching.
            [check] = [dict(t) for t in hr.hdb.conn.execute(
                "SELECT * FROM discipline_tasks WHERE kind = 'check'")]
            assert check["writeup_id"] == later and check["hr_only"]
            assert "may now be too high: Coaching instead" in check["reason"]
            assert discipline.get_writeup(hr.hdb, later)["status"] == "active"     # nothing undone
            assert "discipline_check" in kinds(hr, "hale")
            # Amended: a lower step, through HR; upheld: no change.
            url = f"/api/writeups/{later}/appeal"
            status, r, _ = await hr.call("POST", url, {"text": "Too harsh."}, s["maria"])
            aid = r["writeup"]["appeal"]["id"]
            decide = f"/api/appeals/{aid}/decide"
            assert (await hr.call("POST", decide, {"outcome": "amended", "note": "x"}, s["hana"]))[0] == 400
            assert (await hr.call("POST", decide, {"outcome": "amended", "note": "x", "step": "final"}, s["hana"]))[0] == 400
            status, r, _ = await hr.call("POST", decide, {"outcome": "amended", "step": "coaching",
                                                          "note": "Coaching fits better."}, s["hana"])
            assert status == 200 and r["writeup"]["step"] == "Coaching"
            status, view, _ = await hr.call("GET", f"/api/writeups/{later}", cookie=s["maria"])
            assert view["appeal"]["note"] == "Coaching fits better." and view["appeal"]["word"] == "Amended"
            # Out of time: 7 days by default.
            third = await issued(hr, s)
            assert (await hr.call("POST", f"/api/writeups/{third}/send", {}, s["max"]))[0] == 200
            hr.hdb.conn.execute("UPDATE writeups SET issued_on = ? WHERE id = ?", (day(hr, -8), third))
            hr.hdb.conn.commit()
            status, r, _ = await hr.call("POST", f"/api/writeups/{third}/appeal", {"text": "Late"}, s["maria"])
            assert status == 400 and "within 7 days" in r["error"]
    run(scenario())


def test_hr_adjustments_voids_and_the_as_of_view(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr, confirmed=False)
            hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = ? WHERE person_id != '405'", (time.time(),))
            hr.hdb.conn.commit()
            data = await record(hr, s)
            iid = data["incident"]
            assert (await hr.call("POST", f"/api/incidents/{iid}/approve", {"points": 1}, s["hale"]))[0] == 400
            status, r, _ = await hr.call("POST", f"/api/incidents/{iid}/approve",
                                         {"points": 1.25, "reason": "First time, and busy night."}, s["hale"])
            assert status == 200
            inc = discipline.get_incident(hr.hdb, iid)
            assert (inc["default_points"], inc["final_points"], inc["hr_points"]) == (2, 1.25, 1.25)
            # Adjusting needs a fresh code (Hana hasn't typed one lately).
            body = {"points": -0.5, "reason": "Good attendance credit.", "starts": day(hr)}
            status, r, _ = await hr.call("POST", "/api/people/401/points", body, s["hana"])
            assert (status, r.get("error")) == (401, "reconfirm"), (status, r)
            assert (await hr.call("POST", "/api/people/401/points", body, s["max"]))[0] == 403
            assert (await hr.call("POST", "/api/people/401/points", {**body, "reason": ""}, s["hale"]))[0] == 400
            assert (await hr.call("POST", "/api/people/401/points", {**body, "points": 0.3}, s["hale"]))[0] == 400
            assert (await hr.call("POST", "/api/people/401/points", body, s["hale"]))[0] == 200
            [credit] = [ln for ln in pts.lines("401", "adjustment") if ln["q"] == -2]
            assert (credit["track"], credit["counts_from"], credit["ends"], credit["target_id"]) == \
                ("discipline", today(hr), None, None)
            # One incident's points: an adjustment line, the original kept.
            status, r, _ = await hr.call("POST", f"/api/incidents/{iid}/adjust",
                                         {"points": 2, "reason": "Guest complained in writing."}, s["hale"])
            assert status == 200
            status, view, _ = await hr.call("GET", f"/api/incidents/{iid}", cookie=s["max"])
            assert (view["final_points"], view["points_now"]) == (1.25, 2)
            assert view["history"][-1]["what"] == "Points changed by HR to 2"
            assert "hr_reason" not in view
            status, view, _ = await hr.call("GET", f"/api/incidents/{iid}", cookie=s["hale"])
            assert view["hr_reason"] == "First time, and busy night."
            # The "as of" view: yesterday, nothing counted yet.
            status, d, _ = await hr.call("GET", f"/api/people/401/discipline?as_of={day(hr, -1)}", cookie=s["max"])
            assert d["as_of"] == day(hr, -1) and d["points"] == {"discipline": 0.0} and [i["id"] for i in d["incidents"]] == [iid]
            status, d, _ = await hr.call("GET", "/api/people/401/discipline", cookie=s["max"])
            assert d["points"] == {"discipline": 1.5}
            assert sorted(l["label"] for l in d["lines"]) == ["Adjustment by HR",
                                                              "Adjustment by HR: Rude to a guest",
                                                              "Rude to a guest"]
            status, d, _ = await hr.call("GET", f"/api/people/401/discipline?as_of={day(hr, -1)}", cookie=s["maria"])
            assert d["as_of"] == day(hr)                      # employees see today only
            # Voiding: a fresh code, a reason; HR only sees it afterwards.
            assert (await hr.call("POST", f"/api/incidents/{iid}/void", {"reason": ""}, s["hale"]))[0] == 400
            status, r, _ = await hr.call("POST", f"/api/incidents/{iid}/void", {"reason": "Wrong person."}, s["hale"])
            assert status == 200 and any(v["incident_id"] == str(iid) for v in pts.lines("401", "void"))
            assert (await hr.call("GET", f"/api/incidents/{iid}", cookie=s["max"]))[0] == 404
            status, d, _ = await hr.call("GET", "/api/people/401/discipline", cookie=s["max"])
            assert d["incidents"] == []
            status, d, _ = await hr.call("GET", "/api/people/401/discipline", cookie=s["hale"])
            assert [i["status"] for i in d["incidents"]] == ["voided"]
            assert any(l["state"] == "voided" for l in d["lines"])
            # The audit log has every look and change.
            actions = {r[0] for r in hr.hdb.conn.execute("SELECT action FROM audit")}
            assert {"Recorded an incident", "Approved an incident", "Adjusted points",
                    "Changed an incident's points", "Voided an incident", "Viewed an incident",
                    "Viewed discipline as of " + day(hr, -1)} <= actions
    run(scenario())


def test_changing_when_a_write_up_stops_counting_and_voiding_it(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            wid = await issued(hr, s)
            assert (await hr.call("POST", f"/api/writeups/{wid}/ends", {"ends_on": day(hr, 30), "reason": "x"}, s["hana"]))[0] == 400
            assert (await hr.call("POST", f"/api/writeups/{wid}/send", {}, s["max"]))[0] == 200
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/ends",
                                         {"ends_on": day(hr, -1), "reason": "Earlier"}, s["hana"])
            assert status == 400
            assert (await hr.call("POST", f"/api/writeups/{wid}/ends", {"ends_on": day(hr, 30)}, s["hana"]))[0] == 400
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/ends",
                                         {"ends_on": day(hr, 30), "reason": "Shorter, as agreed."}, s["hana"])
            assert status == 200 and r["writeup"]["ends_on"] == day(hr, 30)
            assert (await hr.call("POST", f"/api/writeups/{wid}/ends", {"ends_on": day(hr, 30), "reason": "x"}, s["max"]))[0] == 403
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/void",
                                         {"reason": "Delivered to the wrong person."}, s["hana"])
            assert status == 200 and r["writeup"]["status"] == "voided"
            assert discipline.get_incident(hr.hdb, discipline.get_writeup(hr.hdb, wid)["incident_id"])["status"] == "approved"
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["maria"])
            assert [w["status_word"] for w in section(page, "discipline")["writeups"]] == [f"Withdrawn on {day(hr)}"]
    run(scenario())


# ---------------------------------------------------------------- who sees what

def test_employees_see_their_own_issued_write_ups_and_points_as_the_policy_allows(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            pts.add("401", 1, today(hr) - timedelta(days=400))          # long expired
            wid = await issued(hr, s)
            waiting = await record(hr, s, offence="rude")               # with HR still
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["maria"])
            sec = section(page, "discipline")
            assert sec["writeups"] == [] and sec["points"] == {"discipline": 2.0}
            assert [l["state"] for l in sec["lines"]] == ["expired", "counts"]
            assert "incidents" not in sec and "tasks" not in sec
            assert (await hr.call("POST", f"/api/writeups/{wid}/send", {}, s["max"]))[0] == 200
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["maria"])
            assert [w["id"] for w in section(page, "discipline")["writeups"]] == [wid]
            # Managers see pending incidents and everything but HR's notes.
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["max"])
            sec = section(page, "discipline")
            assert [i["status"] for i in sec["incidents"]] == ["with_hr", "approved"]
            assert waiting["incident"] in [i["id"] for i in sec["incidents"]]
            # The policy's switches: no total, no expired history for employees.
            pol.publish({**point_system.BODY, "visibility": {"employee_sees_total": False,
                                                            "employee_sees_expired": False}})
            hr.hdb.conn.execute("UPDATE writeups SET ends_on = ? WHERE id = ?", (day(hr, -1), wid))
            hr.hdb.conn.commit()
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["maria"])
            sec = section(page, "discipline")
            assert sec["points"] is None and sec["lines"] == [] and sec["writeups"] == []
            status, page, _ = await hr.call("GET", "/api/people/401", cookie=s["max"])
            [w] = section(page, "discipline")["writeups"]
            assert w["status_word"] == f"Expired {day(hr, -1)}: no longer counts"
            # Bo's file isn't Max's, and Maria's isn't Bo's.
            assert (await hr.call("GET", "/api/people/402/discipline", cookie=s["max"]))[0] == 404
            assert (await hr.call("GET", f"/api/writeups/{wid}", cookie=s["bo"]))[0] == 404
    run(scenario())


def test_suspensions_stay_with_hr_until_approved_and_a_lower_step_is_a_downgrade(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            data = await record(hr, s, offence="safety", what="Left a knife in the sink water.")
            assert (await hr.call("POST", f"/api/incidents/{data['incident']}/approve", {}, s["hana"]))[0] == 200
            [task] = [dict(t) for t in hr.hdb.conn.execute("SELECT * FROM discipline_tasks")]
            assert task["kind"] == "suspension" and task["hr_only"]
            wid = task["writeup_id"]
            assert (await hr.call("GET", f"/api/writeups/{wid}", cookie=s["max"]))[0] == 404
            assert "step_due" not in kinds(hr, "max") and "step_due" in kinds(hr, "hale")
            # HR fills it in, with the suspension's dates, and another HR admin approves it.
            status, r, _ = await hr.call("PUT", f"/api/writeups/{wid}", {
                "improvement": "Knives go straight to the knife rack.", "consequences": "Termination review.",
                "suspension_from": day(hr, 1), "suspension_to": day(hr, 2), "suspension_paid": False}, s["hana"])
            assert status == 200, r
            assert r["writeup"]["suspension"] == {"from": day(hr, 1), "to": day(hr, 2), "paid": False}
            assert (await hr.call("POST", f"/api/writeups/{wid}/submit", {}, s["hana"]))[0] == 200
            assert (await hr.call("POST", f"/api/writeups/{wid}/approve", {}, s["hana"]))[0] == 403
            assert (await hr.call("POST", f"/api/writeups/{wid}/approve", {}, s["hale"]))[0] == 200
            status, view, _ = await hr.call("GET", f"/api/writeups/{wid}", cookie=s["max"])
            assert status == 200 and view["step"] == "Suspension"
    run(scenario())


def test_a_lower_step_than_the_point_system_calls_for_is_a_downgrade(tmp_path, engine):
    pol, pts = engine
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            pts.add("401", 6, today(hr) - timedelta(days=3))       # 6 + 2 = 8: Final written
            status, r, _ = await hr.call("POST", "/api/people/401/incidents", {
                **RUDE, "happened": day(hr), "writeup": {**WRITEUP, "step": "written"}}, s["max"])
            assert status == 400 and "lower step" in r["error"]
            data = await record(hr, s, writeup={**WRITEUP, "step": "written",
                                                "downgrade_reason": "She owned it straight away."})
            w = discipline.get_writeup(hr.hdb, data["writeup"])
            assert (w["step_key"], w["recommended_key"]) == ("written", "final")
            assert (await hr.call("POST", f"/api/incidents/{data['incident']}/approve", {}, s["hana"]))[0] == 200
            w = discipline.get_writeup(hr.hdb, data["writeup"])
            assert (w["step_key"], w["recommended_key"]) == ("written", "final")
            status, r, _ = await hr.call("POST", f"/api/writeups/{w['id']}/approve", {}, s["hana"])
            assert status == 200
            assert "a downgrade" in r["writeup"]["history"][-1]["what"]
            assert r["writeup"]["history"][-1]["note"] == "She owned it straight away."
            assert r["writeup"]["recommended"] == "Final written warning"
    run(scenario())


def test_an_offence_the_policy_lets_skip_hr_counts_at_once(tmp_path, engine):
    pol, pts = engine
    body = pol.publish()
    body["approval"] = {"incidents": "per_offence", "writeups": "all"}
    next(o for o in body["offences"] if o["key"] == "theft")["hr_approval"] = False

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            status, p, _ = await hr.call("POST", "/api/people/401/incident-preview",
                                         {"offence": "phone", "happened": day(hr)}, s["max"])
            assert "HR approves" not in p["text"]
            data = await record(hr, s, offence="phone", what="On the phone at the bar.", points=0.25,
                                reason="Only a minute.")
            assert data["message"] == "Recorded. It counts now." and "step" not in data
            inc = discipline.get_incident(hr.hdb, data["incident"])
            assert inc["status"] == "approved" and inc["decided_by"] == "policy" and inc["final_points"] == 0.25
            assert pts.totals(None, "401", today(hr)) == {"discipline": 0.25}
            assert kinds(hr, "hana") == []
            # Counted at once and reaching a termination review: the manager isn't told the step.
            data = await record(hr, s, offence="theft", what="Took a bottle home.")
            assert set(data) == {"ok", "message", "incident", "writeup"} and data["writeup"] is None
            assert [t["kind"] for t in hr.hdb.conn.execute("SELECT kind FROM discipline_tasks")] == ["termination"]
            assert "termination_review" not in kinds(hr, "max") and "termination_review" in kinds(hr, "hana")
            status, data, _ = await hr.call("POST", "/api/people/401/incidents", {
                **RUDE, "offence": "theft", "happened": day(hr)}, s["hale"])
            assert status == 200 and "task" in data           # HR sees it all
    run(scenario())


def test_workflow_settings_and_the_writeups_kind(tmp_path, engine):
    pol, pts = engine

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            status, st, _ = await hr.call("GET", "/api/settings", cookie=s["hana"])
            mine = st["sections"]["discipline_workflow"]
            assert (mine["appeal_days"], mine["sign_days"], mine["probation_word"]) == (7, 3, "introductory period")
            assert mine["set_up"] is False
            url = "/api/settings/discipline-workflow"
            assert (await hr.call("PUT", url, {"appeal_days": 10}, s["max"]))[0] == 403
            assert (await hr.call("PUT", url, {"probation_word": "trial"}, s["hana"]))[0] == 400
            status, r, _ = await hr.call("PUT", url, {"appeal_days": 99, "sign_days": 2, "probation_word": "probation",
                                                      "probation_show_end": False}, s["hana"])
            assert status == 200
            assert discipline.setting(hr.hdb, "dis_appeal_days") == 60
            assert probation.word(hr.hdb) == "probation" and not probation.shows_end(hr.hdb)
            # Who opens a write-up's files: HR always; the employee and their managers unless voided.
            hr.hdb.conn.execute(
                "INSERT INTO writeups (person_id, status, created_at, pdf_doc_id) VALUES ('401', 'active', 0, 77)")
            hr.hdb.conn.commit()
            assert [discipline.can_open_file(None, rel, {"id": 77}) for rel in
                    ("hr", "self", "manager", "director", None)] == [True, True, True, True, False]
            assert discipline.can_open_file(None, "self", {"id": 78}) is False
            hr.hdb.conn.execute("UPDATE writeups SET status = 'voided' WHERE pdf_doc_id = 77")
            hr.hdb.conn.commit()
            assert [discipline.can_open_file(None, rel, {"id": 77}) for rel in ("hr", "self")] == [True, False]
    run(scenario())
