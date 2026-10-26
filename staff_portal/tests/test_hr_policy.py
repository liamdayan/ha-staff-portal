"""The point system as settings (hr/policy.py): the sample, validation in plain English, versions
(draft, published from a day, retired, withdrawn), the import spreadsheet both ways, and HR's
Discipline setup pages: who may use them, fresh confirmation for publishing, the audit log, the
simulator and re-scoring.

Nothing here talks to a real Connecteam, Google, TT Hotel or Home Assistant.
"""

import asyncio
import copy
import sqlite3
import time
from datetime import date, timedelta

import pytest

from hr_helpers import HR, ct_user

from hr import audit, crypto, points, policy, signin  # noqa: E402
from hr.hrdb import HRDatabase  # noqa: E402

D = date.fromisoformat


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(tmp_path):
    ring = crypto.Keyring(str(tmp_path / "hr"))
    assert ring.load(create=True) == "ready"
    crypto.use(ring)
    database = HRDatabase(str(tmp_path / "hr"))
    policy.setup(database)
    points.setup(database)
    yield database
    database.close()
    crypto.use(None)


# ---------------------------------------------------------------- the sample and validation

def test_the_sample_is_the_designs():
    b = policy.normalize(policy.SAMPLE)
    assert policy.validate(policy.SAMPLE) == [] and policy.normalize(b) == b
    assert [t["name"] for t in b["tracks"]] == ["Discipline"]
    assert [(r["points"], policy.step_name(b, r["step"])) for r in b["ladders"]["discipline"]] == [
        (3, "Coaching"), (5, "Verbal warning"), (7, "Written warning"), (9, "Final written warning"),
        (11, "Termination review")]
    assert b["expiry"] == {"rule": "first", "rolling": {"months": 12}, "clean_reset": None}
    assert b["approval"]["incidents"] == "all" and b["credits"]["on"] is False
    intro = policy.intro_policy(b, ["501"])
    assert intro["days"] == 90 and intro["extensions"] == {"count": 1, "days": 30, "max_days": 120}
    assert [(r["points"], r["step"]) for r in intro["ladders"]["discipline"]] == [(3, "final"), (5, "termination")]
    assert intro["serious_floor"] == "termination" and intro["rehires"] and not intro["transfers"]
    assert {p["kind"] for p in b["probation"]} == {"introductory", "disciplinary"}
    assert b["visibility"]["employee_sees_total"] and b["visibility"]["employee_sees_expired"]
    assert [p["name"] for p in b["protected_leave"]][:3] == ["Oregon sick time", "Paid Leave Oregon", "OFLA/FMLA"]
    assert policy.offence(b, "theft")["min_step"] == "termination"
    assert policy.offence(b, "alcohol")["min_step"] == "final"
    assert policy.needs_hr_approval(b, "late") and policy.writeup_needs_hr(b, "coaching")
    assert policy.step_end(b, "written", "2026-01-31") == D("2026-10-31")
    assert policy.step_end(b, "termination", "2026-01-31") is None
    assert policy.probation_days(b, "introductory", ["501"]) == 90


def problems_with(**changes) -> list[str]:
    b = copy.deepcopy(policy.SAMPLE)
    for key, value in changes.items():
        part, _, field = key.partition(".")
        if field:
            b[part][field] = value
        else:
            b[part] = value
    return policy.validate(b)


def test_validation_names_each_problem_plainly():
    offences = copy.deepcopy(policy.SAMPLE["offences"])
    offences[1].update(points=1.3)
    assert problems_with(offences=offences) == [
        'Offence "Late 8 minutes or more": its points must be from 0 to 100, in quarter points.']
    offences = copy.deepcopy(policy.SAMPLE["offences"])
    offences[1].update(min=1.25, max=2)
    assert problems_with(offences=offences) == [
        "Offence \"Late 8 minutes or more\": the manager's range must include its points (1)."]
    offences = copy.deepcopy(policy.SAMPLE["offences"])
    offences[2]["key"] = "late"
    assert 'Two offences share the key "late".' in problems_with(offences=offences)
    offences[2]["key"] = "Late Arrival"
    assert any("needs a short key" in p for p in problems_with(offences=offences))
    assert problems_with(point_size=0.5) == [
        'Offence "Late 1 to 7 minutes": the manager\'s range must be in half points.',
        'Offence "Missed clock-in or clock-out": the manager\'s range must be in half points.']
    assert problems_with(point_size=0.3) == ["Points must be whole, half or quarter points."]
    ladders = {"discipline": [{"points": 5, "step": "verbal"}, {"points": 3, "step": "coaching"}]}
    assert problems_with(ladders=ladders) == [
        "The Discipline ladder: thresholds must go up, from the fewest points to the most."]
    ladders = {"discipline": [{"points": 3, "step": "verbal"}, {"points": 5, "step": "coaching"}]}
    assert problems_with(ladders=ladders) == ["The Discipline ladder: higher thresholds must lead to higher steps."]
    assert problems_with(ladders={"discipline": [{"points": 3, "step": "warning"}]}) == [
        'The Discipline ladder: "warning" isn\'t one of the steps.']
    assert problems_with(ladders={}) == [
        "The Discipline ladder has no steps: say how many points lead to each step."]
    assert problems_with(escalation=[{"while": "final", "to": "coaching"}]) == [
        "Escalation from Final written warning must go to a higher step."]
    assert problems_with(**{"expiry.rule": "sometimes"}) == ["Choose when points stop counting."]
    assert problems_with(**{"expiry.rolling": None}) == ["Say how long points count for, such as 12 months."]
    assert problems_with(**{"expiry.rolling": {"days": 0}}) == ["Say how long points count for, such as 12 months."]
    assert problems_with(**{"expiry.clean_reset": {"weeks": 2}}) == [
        "The clean-period reset must be a number of days or months, or off."]
    assert problems_with(**{"expiry.rule": "writeup", "expiry.rolling": None}) == []
    assert problems_with(**{"credits.on": True, "credits.points": 0}) == [
        "Credits: the points earned back must be more than 0, in quarter points."]
    steps = copy.deepcopy(policy.SAMPLE["steps"])
    steps[1]["rank"] = 1
    assert "Two steps share a rank: give each step its own place in the order." in problems_with(steps=steps)
    steps = copy.deepcopy(policy.SAMPLE["steps"])
    steps[0]["active"] = "a while"
    assert problems_with(steps=steps) == [
        'Step "Coaching": how long it stays active must be a number of days or months, or never.']
    offences = copy.deepcopy(policy.SAMPLE["offences"])
    offences[1]["repeat"] = {"within": {"days": 90}, "tiers": [{"nth": 1, "add": 1, "step": None}]}
    assert problems_with(offences=offences) == [
        'Offence "Late 8 minutes or more": repeats count up from the 2nd, each one once.']
    offences[1]["repeat"] = {"within": {"days": 90}, "tiers": [{"nth": 2, "add": 0, "step": None}]}
    assert problems_with(offences=offences) == [
        'Offence "Late 8 minutes or more": a repeat must add points or lead to a step.']
    offences[1]["repeat"] = {"within": None, "tiers": [{"nth": 2, "add": 1}]}
    assert problems_with(offences=offences) == [
        'Offence "Late 8 minutes or more": its repeat rule needs a period, such as 90 days.']
    pb = copy.deepcopy(policy.SAMPLE["probation"])
    pb[0].update(multiplier=9, days=0, when_end_passes="forget",
                 overrides={"nope": {"points": 1}, "late": {"points": 2, "min": 3, "max": 4}})
    pb[0]["extensions"]["max_days"] = 10
    found = problems_with(probation=pb)
    assert found == [
        'Probation "Introductory period": its length must be from 1 to 730 days.',
        'Probation "Introductory period": the points multiplier must be from 0.25 to 5, like 1.5.',
        'Probation "Introductory period": "nope" isn\'t one of the offences.',
        "Probation \"Introductory period\": the manager's range for Late 8 minutes or more must include its points.",
        'Probation "Introductory period": choose what happens when the end passes with no decision.']
    assert problems_with(protected_leave=[{"key": "no", "name": "No"}]) == [
        'Protected leave can\'t use the key "no": it means the absence wasn\'t protected.']


def test_validation_never_crashes_on_junk():
    for junk in (None, [], "text", 5, {"tracks": "x"}, {"offences": [1, 2]}, {"steps": None},
                 {"ladders": [], "expiry": "x", "credits": None, "probation": {"a": 1}},
                 {"point_size": "big", "tracks": [{"key": 5, "name": None}]}):
        found = policy.validate(junk)
        assert found and all(isinstance(p, str) for p in found), junk
    assert policy.validate({"offences": [{}] * 400})


def test_normalize_fixes_the_obvious():
    b = copy.deepcopy(policy.SAMPLE)
    b["offences"][1].update(points="1", min="0.5", max="1.5", attendance="yes", wording="Line one\r\nLine\x00 two  ")
    b["steps"][0]["active"] = "6 months"
    b["offences"][2].update(min=None, max="")
    clean = policy.normalize(b)
    assert clean["offences"][1]["points"] == 1 and clean["offences"][1]["min"] == 0.5
    assert clean["offences"][1]["attendance"] is True
    assert clean["offences"][1]["wording"] == "Line one\nLine two"
    assert clean["steps"][0]["active"] == {"months": 6}
    assert (clean["offences"][2]["min"], clean["offences"][2]["max"]) == (1, 1)
    assert policy.validate(clean) == []


def test_spans_from_text():
    assert policy.parse_span("90 days") == {"days": 90}
    assert policy.parse_span("12 Months") == {"months": 12}
    assert policy.parse_span("1 year") == {"months": 12}
    assert policy.parse_span("2 weeks") == {"days": 14}
    assert policy.parse_span("never") is None and policy.parse_span("") is None
    assert policy.parse_span("30", "days") == {"days": 30}
    for bad in ("30", "soon", "0 days", "999 months"):
        with pytest.raises(ValueError):
            policy.parse_span(bad)
    assert policy.span_text({"months": 12}) == "12 months" and policy.span_text({"months": 24}) == "2 years"
    assert policy.span_text({"days": 1}) == "1 day" and policy.span_text(None) == "never"


# ---------------------------------------------------------------- versions

def test_the_sample_ships_as_a_draft_not_in_use(db):
    [v] = policy.versions(db)
    assert (v["status"], v["state"], v["label"], v["source"]) == ("draft", "draft", "Draft, not in use", "sample")
    assert policy.current(db, "2026-10-26") is None and not policy.set_up(db)
    policy.setup(db)                       # starting again doesn't add another
    assert len(policy.versions(db)) == 1


def test_drafts_publish_and_published_versions_never_change(db):
    [sample] = policy.versions(db)
    vid = sample["id"]
    vid = policy.save_draft(db, {**policy.SAMPLE, "name": "Hot Lake 2026"}, "hr", vid)
    assert policy.get(db, vid)["name"] == "Hot Lake 2026"
    broken = copy.deepcopy(policy.SAMPLE)
    broken["steps"] = []
    bad = policy.save_draft(db, broken, "hr")
    with pytest.raises(ValueError, match="Fix these first"):
        policy.publish(db, bad, "2026-11-01", "hr", on=D("2026-10-26"))
    with pytest.raises(ValueError, match="Choose the day"):
        policy.publish(db, vid, "soon", "hr", on=D("2026-10-26"))
    # The first version may start earlier, to load past incidents.
    policy.publish(db, vid, "2026-01-01", "hr", on=D("2026-10-26"))
    assert policy.current(db, "2025-12-31") is None and policy.current(db, "2026-01-01")["id"] == vid
    with pytest.raises(ValueError, match="can't be changed"):
        policy.save_draft(db, policy.SAMPLE, "hr", vid)
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute("UPDATE policy_versions SET body = '{}' WHERE id = ?", (vid,))
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute("DELETE FROM policy_versions WHERE id = ?", (vid,))
    db.conn.rollback()
    with pytest.raises(ValueError, match="Only a draft"):
        policy.delete_draft(db, vid)
    with pytest.raises(ValueError, match="Only a draft"):
        policy.publish(db, vid, "2027-01-01", "hr", on=D("2026-10-26"))
    # Later versions: from today on, and after the one before.
    v2 = policy.save_draft(db, {**policy.SAMPLE, "name": "2027"}, "hr")
    with pytest.raises(ValueError, match="before today"):
        policy.publish(db, v2, "2026-10-25", "hr", on=D("2026-10-26"))
    with pytest.raises(ValueError, match="within a year"):
        policy.publish(db, v2, "2028-01-01", "hr", on=D("2026-10-26"))
    policy.publish(db, v2, "2027-01-01", "hr", on=D("2026-10-26"))
    v3 = policy.save_draft(db, {**policy.SAMPLE, "name": "Later"}, "hr")
    with pytest.raises(ValueError, match="must start after 2027"):
        policy.publish(db, v3, "2026-12-01", "hr", on=D("2026-10-26"))
    states = {v["id"]: (v["status"], v["state"], v["label"]) for v in policy.versions(db, "2026-10-26")}
    assert states[vid] == ("published", "in_use", "In use since Jan 1, 2026")
    assert states[v2] == ("published", "scheduled", "Starts Jan 1, 2027")
    assert states[v3] == ("draft", "draft", "Draft, not in use")
    later = {v["id"]: (v["status"], v["state"], v["label"]) for v in policy.versions(db, "2027-02-01")}
    assert later[vid] == ("retired", "retired", "Retired Jan 1, 2027") and later[v2][1] == "in_use"
    assert policy.current(db, "2026-12-31")["id"] == vid and policy.current(db, "2027-01-01")["id"] == v2
    # A scheduled version can be withdrawn before it starts; one in force can't.
    with pytest.raises(ValueError, match="already in force"):
        policy.withdraw(db, vid, "hr", on=D("2026-10-26"))
    policy.withdraw(db, v2, "hr", on=D("2026-10-26"))
    assert policy.current(db, "2027-06-01")["id"] == vid
    assert {v["id"]: v["state"] for v in policy.versions(db)}[v2] == "withdrawn"
    policy.delete_draft(db, v3)
    assert policy.get(db, v3) is None


# ---------------------------------------------------------------- the import spreadsheet

def custom_body() -> dict:
    b = copy.deepcopy(policy.SAMPLE)
    b["name"] = "Hot Lake, 2027"
    b["tracks"] = [{"key": "attendance", "name": "Attendance"}, {"key": "conduct", "name": "Conduct"}]
    b["ladders"] = {"attendance": [{"points": 4, "step": "verbal"}, {"points": 8.5, "step": "final"}],
                    "conduct": [{"points": 2.25, "step": "written"}]}
    for o in b["offences"]:
        o["track"] = "attendance" if o["attendance"] else "conduct"
    b["offences"][1]["wording"] = 'Be on time, "ready" to work,\nevery shift.'
    b["credits"] = {"on": True, "clean": {"days": 60}, "points": 0.5, "max_per_year": 3, "tracks": ["attendance"]}
    b["expiry"] = {"rule": "rolling", "rolling": {"days": 365}, "clean_reset": {"months": 6}}
    b["probation"][0].update(multiplier=1.5, department_days={"501": 120}, departments=["501", "502"],
                             overrides={"no_call": {"points": 5, "min": 4, "max": 6}}, on_pass="clear",
                             when_end_passes="pass")
    b["probation"][0]["ladders"] = {"attendance": [{"points": 3, "step": "final"}]}
    b["probation"][1]["ladders"] = {}
    b["escalation"] = [{"while": "any", "to": "next"}]
    b["skip_steps"] = True
    b["approval"] = {"incidents": "per_offence", "writeups": "per_step"}
    b["visibility"]["employee_sees_expired"] = False
    b["protected_leave"] = [{"key": "sick", "name": "Sick time"}]
    return policy.normalize(b)


def test_export_then_import_gives_the_same_point_system():
    for b in (policy.normalize(policy.SAMPLE), custom_body()):
        assert policy.validate(b) == []
        text = policy.export_csv(b)
        back, questions = policy.import_csv(text)
        assert questions == []
        assert back == b
    text = policy.export_csv(custom_body())
    assert text.startswith("Settings\r\nSetting,Value\r\nPoint system,\"Hot Lake, 2027\"\r\n")
    assert "Points stop counting,After the rolling window" in text


def test_spreadsheet_cells_never_run_as_formulas():
    b = copy.deepcopy(policy.SAMPLE)
    b["offences"][0]["name"] = '=HYPERLINK("http://example.com","Late")'
    b["offences"][1]["wording"] = "@SUM(A1)"
    b["offences"][2]["category"] = "-Attendance"
    text = policy.export_csv(b)
    assert "'=HYPERLINK" in text and "'@SUM" in text and "'-Attendance" in text
    back, questions = policy.import_csv(text)
    assert questions == [] and back == policy.normalize(b)
    minus = copy.deepcopy(policy.SAMPLE)
    minus["ladders"]["discipline"][0]["points"] = 3
    assert "'3" not in policy.export_csv(minus)


def test_import_asks_about_what_it_had_to_guess():
    text = "\n".join([
        "stray line",
        "Settings", "Setting,Value", "Point system,Ours", "Point size,half", "Rolling window,12",
        "Steps go in order,maybe", "Colour,blue",
        "", "Steps", "Key,Name,Rank,Active for", "c,Coaching,1,6 months", "w,Written,2,9", "f,Final,3,1 year",
        "", "Ladder", "Point total,Points,Step", "Discipline,2,Coaching", ",4,w", ",6,Final", ",8,Sacked",
        "", "Offences", "Name,Points,Manager min,Manager max,Attendance,Minimum step,Shoe size",
        "Late,1,0.5,1.5,yes,,", "Late,1,,,y,,", "Theft,six,,,no,Final,", "Fighting,,,,no,Gone,",
        "", "Repeats", "Offence,Within,Occurrence,Extra points,Step", "late,90,3rd,1,", "Late,90 days,4,1,",
        "Nope,90 days,2,1,",
    ])
    b, questions = policy.import_csv(text)
    assert b["name"] == "Ours" and b["point_size"] == 0.5
    assert b["expiry"]["rolling"] == {"months": 12}
    assert [s["key"] for s in b["steps"]] == ["c", "w", "f"] and b["steps"][1]["active"] == {"months": 9}
    assert [o["key"] for o in b["offences"]] == ["late", "late_2", "theft", "fighting"]
    assert b["offences"][0]["repeat"] == {"within": {"days": 90}, "tiers": [
        {"nth": 3, "add": 1, "step": None}, {"nth": 4, "add": 1, "step": None}]}
    assert b["offences"][2]["min_step"] == "f" and b["offences"][2]["points"] == 0
    expected = [
        'Settings: "Colour" isn\'t a setting the portal knows, so it was left out.',
        'Rolling window: "12" has no unit, so it was read as 12 months. Is that right?',
        'Steps go in order: "maybe" isn\'t yes or no, so yes was used.',
        'Step "Written": "9" has no unit, so it was read as 9 months. Is that right?',
        'Ladder: "Sacked" isn\'t one of the steps, so it was left out.',
        'Offences: the column "shoe size" wasn\'t used.',
        'Offence "Theft": "six" isn\'t a number, so it was left empty.',
        'Offence "Theft" has no points, so 0 was used. How many points is it?',
        'Offence "Fighting": "Gone" isn\'t one of the steps, so it was left out.',
        'Repeats for "Late": "90" has no unit, so it was read as 90 days. Is that right?',
        'Repeats: "Nope" isn\'t one of the offences, so it was left out.',
        "Line 1 isn't under a section (such as Offences), so it was left out.",
        "The file had no probation rules. Add them under Probation, or leave it empty if probation "
        "works like everyone else.",
        "The file had no protected leave list, so the standard Oregon list was used. Ask your "
        "employment lawyer to confirm it.",
    ]
    for q in expected:
        assert q in questions, q
    assert any(q.startswith("These settings weren't in the file") for q in questions)


def test_import_reads_pasted_spreadsheet_text_and_refuses_junk():
    text = policy.export_csv(policy.SAMPLE).replace(",", "\t")
    tabbed = "\n".join(line for line in text.splitlines() if "Handbook" not in line and "Be " not in line)
    b, _ = policy.import_csv("﻿" + tabbed)
    assert len(b["steps"]) == 6 and b["ladders"]["discipline"][0] == {"points": 3, "step": "coaching"}
    with pytest.raises(ValueError, match="no sections"):
        policy.import_csv("just,some,cells\n1,2,3")
    with pytest.raises(ValueError, match="too big"):
        policy.import_csv("x" * (policy.MAX_CSV + 1))
    b, questions = policy.import_csv("Offences\nName,Points\nLate,1")
    assert b["offences"][0]["track"] == "discipline" and len(b["steps"]) == 6
    assert any("sample's were used" in q for q in questions)


# ---------------------------------------------------------------- the pages

PEOPLE = [ct_user(301, "Ana", "Lee", code="4821", groups=[501], mobile="541-555-0301"),
          ct_user(302, "Bo", "Diaz", code="7302", groups=[501], mobile="541-555-0302"),
          ct_user(303, "Max", "Ray", code="9900", groups=[501], mobile="541-555-0303"),
          ct_user(305, "Hana", "Ito", code="7788", groups=[502], mobile="541-555-0305"),
          ct_user(306, "Ken", "Oda", code="6655", groups=[502], mobile="541-555-0306")]


async def sessions(hr: HR) -> dict:
    await hr.add_people(*PEOPLE)
    hr.give("303", "manager", {"501": True})
    hr.give("305", "hr_admin")
    hr.give("306", "hr_admin")
    return {role: signin.new_session(hr.hdb, pid, "127.0.0.1", "test")
            for role, pid in (("employee", "301"), ("manager", "303"), ("hr", "305"), ("hr2", "306"))}


def confirm_all(hr: HR, ago: float = 0) -> None:
    hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = ?", (time.time() - ago,))
    hr.hdb.conn.commit()


def test_until_published_everything_says_it_isnt_set_up(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await sessions(hr)
            for role in ("employee", "manager", "hr"):
                status, data, _ = await hr.call("GET", "/api/policy/current", cookie=s[role])
                assert status == 200 and data == {"set_up": False, "message": policy.NOT_SET_UP}
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["hr"])
            assert any("point system isn't set up yet" in t["text"] for t in home["todos"])
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["manager"])
            assert not any("point system" in t["text"] for t in home["todos"])
            status, data, _ = await hr.call("GET", "/api/settings", cookie=s["hr"])
            assert data["sections"]["discipline"]["set_up"] is False
            assert data["sections"]["discipline"]["drafts"] == 1
            status, data, _ = await hr.call("GET", "/api/policy", cookie=s["hr"])
            assert data["current"] is None and [v["source"] for v in data["versions"]] == ["sample"]
            # Only HR admins reach the setup.
            for role in ("employee", "manager"):
                assert (await hr.call("GET", "/api/policy", cookie=s[role]))[0] == 403
                assert (await hr.call("POST", "/api/policy", {"from": "sample"}, cookie=s[role]))[0] == 403
    run(scenario())


def test_setting_up_testing_and_publishing(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await sessions(hr)
            admin = s["hr"]
            status, data, _ = await hr.call("GET", "/api/policy", cookie=admin)
            sample = data["versions"][0]["id"]
            status, v, _ = await hr.call("GET", f"/api/policy/{sample}", cookie=admin)
            assert status == 200 and v["problems"] == [] and v["first"] is True
            assert v["version"]["state"] == "draft" and {d["group"] for d in v["departments"]} >= {"501", "502"}
            # Edit the draft: problems come back, and it's saved anyway.
            body = copy.deepcopy(v["body"])
            body["name"] = "Hot Lake 2026"
            body["ladders"]["discipline"][0]["points"] = 30
            status, saved, _ = await hr.call("PUT", f"/api/policy/{sample}", {"body": body}, cookie=admin)
            assert status == 200 and saved["problems"] and saved["version"]["name"] == "Hot Lake 2026"
            # The simulator tests what's on screen, even unsaved.
            body["ladders"]["discipline"][0]["points"] = 3
            sim_req = {"body": body, "start": "2026-01-05", "department": "501",
                       "events": [{"date": "2026-02-01", "offence": "late_callout"},
                                  {"date": "2026-02-02", "offence": "late"}]}
            status, sim, _ = await hr.call("POST", f"/api/policy/{sample}/simulate", sim_req, cookie=admin)
            assert status == 200 and sim["problems"] == []
            assert [r["step"] for r in sim["rows"] if r["kind"] == "incident"] == [None, "final"]
            status, saved, _ = await hr.call("PUT", f"/api/policy/{sample}", {"body": body}, cookie=admin)
            assert saved["problems"] == []
            # Publishing asks for a fresh code (or PIN).
            confirm_all(hr, ago=600)
            status, data, _ = await hr.call("POST", f"/api/policy/{sample}/publish",
                                            {"effective": "2026-01-01"}, cookie=admin)
            assert (status, data["error"]) == (401, "reconfirm")
            await hr.confirm(admin, "305")
            status, data, _ = await hr.call("POST", f"/api/policy/{sample}/publish",
                                            {"effective": "2026-01-01"}, cookie=admin)
            assert status == 200 and data["version"]["state"] == "in_use"
            assert (await hr.call("PUT", f"/api/policy/{sample}", {"body": body}, cookie=admin))[0] == 409
            # Everyone can now read it.
            status, data, _ = await hr.call("GET", "/api/policy/current", cookie=s["employee"])
            assert status == 200 and data["set_up"] and data["policy"]["name"] == "Hot Lake 2026"
            p = data["policy"]
            assert p["tracks"][0]["ladder"][0] == {"points": 3, "step": "Coaching"}
            assert p["expiry"].startswith("Points stop counting 12 months after the day it happened, or when")
            late = next(o for o in p["offences"] if o["name"] == "Late 8 minutes or more")
            assert late["repeat"] == "the 3rd within 90 days adds 1 point" and late["range"] == [0.5, 1.5]
            status, home, _ = await hr.call("GET", "/api/home", cookie=admin)
            assert not any("point system" in t["text"] for t in home["todos"])
            # A new draft from it, exported and imported back.
            status, new, _ = await hr.call("POST", "/api/policy", {"from": sample}, cookie=admin)
            status, exp, _ = await hr.call("GET", f"/api/policy/{new['id']}/export", cookie=admin)
            assert exp["filename"] == "Hot Lake 2026 new draft.csv" and exp["csv"].startswith("Settings")
            status, imp, _ = await hr.call("POST", "/api/policy/import", {"csv": exp["csv"], "name": "Imported"},
                                           cookie=admin)
            assert status == 200 and imp["questions"] == [] and imp["problems"] == []
            assert policy.get(hr.hdb, imp["id"])["name"] == "Imported"
            status, bad, _ = await hr.call("POST", "/api/policy/import", {"csv": "a,b"}, cookie=admin)
            assert status == 400 and "no sections" in bad["error"]
            # Deleting a draft asks again after 5 minutes; a published one can't go.
            confirm_all(hr, ago=600)
            assert (await hr.call("DELETE", f"/api/policy/{imp['id']}", cookie=admin))[0] == 401
            confirm_all(hr)
            assert (await hr.call("DELETE", f"/api/policy/{imp['id']}", cookie=admin))[0] == 200
            assert (await hr.call("DELETE", f"/api/policy/{sample}", cookie=admin))[0] == 409
            # Scheduled for later, then withdrawn.
            later = (policy.today(hr.hdb) + timedelta(days=30)).isoformat()
            status, data, _ = await hr.call("POST", f"/api/policy/{new['id']}/publish", {"effective": later},
                                            cookie=admin)
            assert status == 200 and data["version"]["state"] == "scheduled"
            assert (await hr.call("POST", f"/api/policy/{new['id']}/withdraw", {}, cookie=admin))[0] == 200
            assert (await hr.call("POST", f"/api/policy/{sample}/withdraw", {}, cookie=admin))[0] == 409
            actions = [a["action"] for a in audit.lines(hr.hdb, 200)]
            for action in ("Saved a point system draft", "Published the point system", "Started a point system draft",
                           "Exported the point system", "Imported a point system", "Deleted a point system draft",
                           "Withdrew a point system version"):
                assert action in actions, action
            published = [a for a in audit.lines(hr.hdb, 200) if a["action"] == "Published the point system"][-1]
            assert published["security"] == 1 and "2026-01-01" in published["detail"]
            assert (await hr.call("GET", "/api/policy/999", cookie=admin))[0] == 404
    run(scenario())


def test_rescoring_from_the_pages(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await sessions(hr)
            admin = s["hr"]
            confirm_all(hr)
            db = hr.hdb
            [sample] = policy.versions(db)
            policy.publish(db, sample["id"], "2025-06-01", "305", on=D("2025-06-01"))
            day = policy.today(db) - timedelta(days=20)
            for pid in ("301", "302", "305"):
                points.add_incident_entry(db, pid, f"inc-{pid}", "discipline", 1, day, day, sample["id"], "303",
                                          "Late", offence="late", scoring={"protected": False})
            b = copy.deepcopy(policy.SAMPLE)
            next(o for o in b["offences"] if o["key"] == "late").update(points=2, min=1, max=2)
            v2 = policy.save_draft(db, b, "305")
            policy.publish(db, v2, policy.today(db).isoformat(), "305")
            status, data, _ = await hr.call("GET", "/api/policy/rescore", cookie=admin)
            assert [p["name"] for p in data["people"]] == ["Ana Lee", "Bo Diaz"]     # not their own file
            status, data, _ = await hr.call("POST", "/api/policy/rescore/preview", {"people": ["305"]}, cookie=admin)
            assert status == 403 and "own file" in data["error"]
            status, preview, _ = await hr.call("POST", "/api/policy/rescore/preview", {"people": ["301", "302"]},
                                               cookie=admin)
            assert status == 200 and preview["changed"] == 2
            assert [(p["before"], p["after"]) for p in preview["people"]] == [
                ({"discipline": 1.0}, {"discipline": 2.0})] * 2
            assert points.totals(db, "301")["discipline"] == 1.0
            status, data, _ = await hr.call("POST", "/api/policy/rescore", {"people": ["301"]}, cookie=admin)
            assert status == 400 and "reason" in data["error"]
            confirm_all(hr, ago=600)
            status, data, _ = await hr.call("POST", "/api/policy/rescore", {"people": ["301"], "reason": "Handbook"},
                                            cookie=admin)
            assert (status, data["error"]) == (401, "reconfirm")
            confirm_all(hr)
            status, done, _ = await hr.call("POST", "/api/policy/rescore", {"people": ["301"], "reason": "Handbook"},
                                            cookie=admin)
            assert status == 200 and done["changed"] == 1
            assert points.totals(db, "301")["discipline"] == 2.0 and points.totals(db, "302")["discipline"] == 1.0
            line = next(a for a in audit.lines(db) if a["action"] == "Re-scored points")
            assert line["target_id"] == "301" and "Handbook" not in str(audit.lines(db))
            # Another HR admin re-scores the first one's own file.
            status, data, _ = await hr.call("GET", "/api/policy/rescore", cookie=s["hr2"])
            assert [p["name"] for p in data["people"]] == ["Bo Diaz", "Hana Ito"]
    run(scenario())


def test_simulator_from_the_page_needs_events_and_a_saved_version(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await sessions(hr)
            [sample] = policy.versions(hr.hdb)
            path = f"/api/policy/{sample['id']}/simulate"
            status, data, _ = await hr.call("POST", path, {"start": "2026-01-01"}, cookie=s["hr"])
            assert status == 400
            status, data, _ = await hr.call("POST", path, {"start": "2026-01-01", "events": [
                {"date": "2026-03-01", "offence": "theft"}]}, cookie=s["hr"])
            [row] = [r for r in data["rows"] if r["kind"] == "incident"]
            assert row["step"] == "termination" and row["reason"].startswith("Theft or dishonesty goes to")
            assert (await hr.call("POST", path, {"events": []}, cookie=s["manager"]))[0] == 403
            big = {"start": "2026-01-01", "events": [{"date": "2026-03-01", "offence": "late"}] * 201}
            status, data, _ = await hr.call("POST", path, big, cookie=s["hr"])
            assert "At most 200" in data["problems"][0]
    run(scenario())


def test_the_nightly_run_awards_credits(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await sessions(hr)
            db = hr.hdb
            b = copy.deepcopy(policy.SAMPLE)
            b["credits"]["on"] = True
            vid = policy.save_draft(db, b, "305")
            policy.publish(db, vid, "2025-01-01", "305", on=D("2025-01-01"))
            day = policy.today(db) - timedelta(days=100)
            points.add_incident_entry(db, "301", "a", "discipline", 2, day, day, vid, "303", "Late", offence="late")
            await hr.svc.nightly()
            assert points.totals(db, "301")["discipline"] == 1.0
            assert [ln["kind"] for ln in points.lines_of(db, "301")] == ["incident", "credit"]
    run(scenario())
