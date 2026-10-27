"""Attendance & Leave report: report.py's fetching and tidying (with a fake ctx, see
attendance_fake.py), and view.js's calculations under Node (skipped when node isn't installed).
Nothing here talks to Connecteam or Gusto."""

import ast
import asyncio
import datetime as dt
import json
import os
import shutil
import subprocess

import pytest

from attendance_fake import FORMS, GUSTO, HERE, LA, REPORT_DIR, FakeCt, FakeCtx, load_report, sample

rep = load_report()
VIEW = os.path.join(REPORT_DIR, "view.js")
need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed")


def run(ctx, params=None):
    return asyncio.run(rep.data(ctx, params if params is not None else {"period": "week", "at": "2026-10-14"}))


def view(tmp_path, data, **job):
    path = tmp_path / "job.json"
    path.write_text(json.dumps(dict(job, data=data)))
    done = subprocess.run(["node", os.path.join(HERE, "attendance_view_test.js"), VIEW, str(path)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def by_name(out):
    return {r["name"]: r for r in out["rows"]}


# ---------------------------------------------------------------- report.py

def test_report_imports_only_datetime():
    tree = ast.parse(open(os.path.join(REPORT_DIR, "report.py")).read())
    found = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    found |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert found == {"datetime"}


def test_report_definition():
    r = rep.REPORT
    assert r["key"] == "attendance" and r["name"] and r["description"] and r["data"] is rep.data
    assert [u["key"] for u in r["uploads"]] == ["gusto"] and ".csv" in r["uploads"][0]["accept"]
    assert {s["key"] for s in r["settings"]} == {"rules", "forms"}
    for name in ("report.py", "view.js", "view.css"):
        assert os.path.exists(os.path.join(REPORT_DIR, name))


def test_view_registers_and_has_no_inline_html():
    js = open(VIEW).read()
    assert 'StaffReports.register("attendance"' in js
    # text people type is always set as text: no HTML strings, eval or outside scripts
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "http://", "https://", "setAttribute(\"style\""):
        assert bad not in js
    css = open(os.path.join(REPORT_DIR, "view.css")).read()
    assert "@import" not in css and "http" not in css and "prefers-color-scheme: dark" in css and "@media print" in css


def test_periods():
    d = dt.date
    assert rep.period_of(d(2026, 10, 14), "week", 0) == (d(2026, 10, 11), d(2026, 10, 17))
    assert rep.period_of(d(2026, 10, 11), "week", 0) == (d(2026, 10, 11), d(2026, 10, 17))
    assert rep.period_of(d(2026, 10, 11), "week", 1) == (d(2026, 10, 5), d(2026, 10, 11))
    assert rep.period_of(d(2026, 10, 15), "pay", 0) == (d(2026, 10, 1), d(2026, 10, 15))
    assert rep.period_of(d(2026, 10, 16), "pay", 0) == (d(2026, 10, 16), d(2026, 10, 31))
    assert rep.period_of(d(2028, 2, 20), "pay", 0) == (d(2028, 2, 16), d(2028, 2, 29))
    assert rep.period_of(d(2026, 12, 31), "month", 0) == (d(2026, 12, 1), d(2026, 12, 31))


def test_rules_and_forms_are_cleaned():
    rules = rep.clean_rules(dict(lateGrace="7", lunchAfter=-3, weekStart=9, actionAt="x", aliases=5,
                                 earlySkip=["Spa", 3, "", "Pub"], extra="ignored"))
    assert rules["lateGrace"] == 7 and rules["lunchAfter"] == 0 and rules["weekStart"] == 6
    assert rules["actionAt"] == 4 and rules["aliases"] == "5" and rules["earlySkip"] == ["Spa", "Pub"]
    assert "extra" not in rules and rep.clean_rules("junk") == rep.DEFAULT_RULES
    forms = rep.clean_forms([dict(id="a", frequency="hour", every="", night_every=0), dict(id="a"),
                             dict(id="", name="no id"), "junk", dict(id="b", frequency="daily", every=99)]
                            + [dict(id=str(i)) for i in range(40)])
    assert forms[0] == dict(id="a", name="Untitled form", frequency="hour", applies_to="", every=1, night_every=0)
    assert forms[1]["id"] == "b" and forms[1]["frequency"] == "shift" and forms[1]["every"] == 24
    assert len(forms) == rep.MAX_FORMS


def test_times_become_local_wall_clock_minutes():
    day = dt.date(2026, 10, 12)
    base = rep.day_number(day) * 1440
    assert rep.abs_minutes("2026-10-12T06:12:00-07:00", None, LA) == base + 372
    assert rep.abs_minutes("2026-10-12T13:12:00Z", None, LA) == base + 372  # moved into the property's zone
    assert rep.abs_minutes("2026-10-12T06:12:00", None, LA) == base + 372
    assert rep.abs_minutes(dt.datetime(2026, 10, 12, 13, 12, tzinfo=dt.timezone.utc).timestamp(), None, LA) == base + 372
    assert rep.abs_minutes("6:12 PM", day, LA) == base + 18 * 60 + 12
    assert rep.abs_minutes("12:05am", day, LA) == base + 5
    assert rep.abs_minutes("06:12:30", day, LA) == base + 372
    assert rep.abs_minutes(dt.time(6, 12), day, LA) == base + 372
    for junk in ("", None, "soon", "25:00", True, "6:12"):
        assert rep.abs_minutes(junk, None if junk == "6:12" else day, LA) is None


def test_fetches_one_padded_range_and_only_the_forms_set_up():
    ct, ctx = sample()
    d = run(ctx)
    lo, hi = dt.date(2026, 10, 10), dt.date(2026, 10, 18)  # the week, plus a day either side
    assert ("timesheets", lo, hi) in ct.calls and ("shifts", lo, hi) in ct.calls
    assert [c[1] for c in ct.calls if c[0] == "form_submissions"] == ["f1", "f2", "f3"]
    assert ("forms",) not in ct.calls and d["form_choices"] == [] and d["can_manage"] is False
    assert ctx.upload_keys == ["gusto"]
    assert (d["start"], d["end"], d["period"]) == ("2026-10-11", "2026-10-17", "week")
    assert d["fetch_start"] == rep.day_number(lo) and d["fetch_end"] == rep.day_number(hi)


def test_form_list_only_for_managers_who_ask():
    ct, ctx = sample(can_manage=True)
    assert run(ctx)["form_choices"] == [] and ("forms",) not in ct.calls
    d = run(ctx, {"period": "week", "at": "2026-10-14", "form_list": "1"})
    assert [f["id"] for f in d["form_choices"]] == [f["id"] for f in FORMS]
    ct, ctx = sample()
    assert run(ctx, {"form_list": "1"})["form_choices"] == [] and ("forms",) not in ct.calls


def test_rows_are_normalized():
    _, ctx = sample()
    d = run(ctx)
    maya = next(t for t in d["timesheets"] if t["uid"] == "1")
    assert maya["in"] % 1440 == 6 * 60 + 12 and maya["out"] % 1440 == 14 * 60 + 31 and maya["brk"] == 30
    assert maya["day"] == rep.day_number(dt.date(2026, 10, 12)) and maya["job"] == "Front Desk"
    priya = next(t for t in d["timesheets"] if t["uid"] == "5")
    assert priya["in"] is None and priya["hours"] == 7.5 and priya["type"].startswith("Time off")
    assert {(s["draft"], s["open"]) for s in d["shifts"]} == {(False, False), (True, False), (False, True)}
    assert d["departments"] == {"1": "Front Office", "2": "Front Office"}
    assert d["users"]["4"] == "Jonah Reyes"
    assert d["gusto"]["name"] == "time-off-balances.csv" and d["gusto"]["by"] == "Liam"  # the newest
    assert d["settings"]["rules"]["aliases"] == "Rose Brooks = Lena Brooks" and d["errors"] == []
    assert d["now"] == rep.day_number(dt.date(2026, 10, 20)) * 1440 + 9 * 60
    assert d["today"] == rep.day_number(dt.date(2026, 10, 20))
    json.dumps(d)  # it goes to the browser as JSON


def test_overnight_clock_out_given_as_a_bare_time():
    ct = FakeCt([dict(user_id="9", name="Ty Nguyen", date="2026-10-12", clock_in="22:00", clock_out="06:30",
                      break_minutes=0, job="Night audit", type="shift")])
    d = run(FakeCtx(ct))
    t = d["timesheets"][0]
    assert t["out"] - t["in"] == 8 * 60 + 30


def test_a_rows_own_id_is_never_taken_for_a_person():
    rows = rep.submission_rows([dict(id="sub-1", name="Walk-in", submitted_at="2026-10-12T09:00:00-07:00")], LA)
    assert rows[0]["uid"] is None
    assert rep.shift_rows([dict(id="shift-9", date="2026-10-12", start="09:00", end="17:00")], LA)[0]["uid"] is None
    assert rep.user_names([dict(id=5, firstName="Ana", lastName="Ruiz")]) == {"5": "Ana Ruiz"}


def test_departments_from_members_or_user_rows():
    found = rep.department_map(
        [dict(name="Spa", members=[dict(connecteam_user_id="7"), dict(id=8)]), dict(name="Pub", user_ids=["7", 9]),
         "junk", dict(name=None, user_ids=["1"])],
        [dict(id="10", name="A", department="Kitchen"), dict(id="7", department="Other")])
    assert found == {"7": "Spa", "8": "Spa", "9": "Pub", "10": "Kitchen"}


def test_a_refusal_pauses_only_that_part():
    _, ctx = sample(fail={"timesheets", "form_submissions"})
    d = run(ctx)
    assert d["have"] == {"timesheets": False, "shifts": True}
    assert d["timesheets"] == [] and len(d["shifts"]) == 8
    assert all(f["loaded"] is False for f in d["forms"])
    assert any(e.startswith("Timesheets: Paused") for e in d["errors"]) and len(d["errors"]) == 4
    _, ctx = sample(fail={"users"})
    d = run(ctx)
    assert d["users"] == {} and d["errors"] == [] and d["timesheets"]


def test_params_are_checked():
    ct, ctx = sample()
    d = run(ctx, {"period": "fortnight", "at": "not a date"})
    assert d["period"] == "week" and d["start"] == "2026-10-18"  # this week
    d = run(ctx, {"period": "pay", "at": "1999-01-01"})
    assert d["start"] == "2024-10-16"  # two years back at most
    d = run(ctx, {"period": "month", "at": "2030-01-01"})
    assert d["start"] == "2026-11-01"
    d = run(ctx, None)
    assert d["period"] == "week"


def test_bad_stored_settings_fall_back_to_defaults():
    _, ctx = sample(settings={"rules": "junk", "forms": {"a": 1}})
    d = run(ctx)
    assert d["settings"] == dict(rules=rep.DEFAULT_RULES, forms=[]) and d["forms"] == []


# ---------------------------------------------------------------- view.js, under Node

@need_node
def test_view_js_parses():
    subprocess.run(["node", "--check", VIEW], check=True)


@need_node
def test_view_checks_every_rule(tmp_path):
    _, ctx = sample()
    out = view(tmp_path, run(ctx))
    rows = by_name(out)
    assert [r["name"] for r in out["rows"]] == ["Jonah Reyes", "Lena Brooks", "Dev Patel", "Maya Ortiz", "Priya Shah", "Sam Whitaker"]
    assert rows["Maya Ortiz"]["late"] == [12] and rows["Maya Ortiz"]["status"] == "watch"
    assert rows["Dev Patel"]["early"] == [30] and rows["Dev Patel"]["sickLow"] is True
    assert rows["Lena Brooks"]["lunch"] == [15] and "Short lunch 15 min" in rows["Lena Brooks"]["flags"]
    jonah = rows["Jonah Reyes"]
    assert jonah["missed"] == 1 and jonah["missedHrs"] == 8.5 and jonah["status"] == "action"
    assert jonah["cover"] == dict(cls="bad", main="Short 2h 30m", sub="8h 30m missed, 6h available")
    priya = rows["Priya Shah"]
    assert priya["missed"] == 0 and priya["offHrs"] == 7.5 and priya["flags"] == ["Sick leave 7h 30m"] and priya["status"] == "clear"
    sam = rows["Sam Whitaker"]
    assert sam["sickEarly"] is True and sam["sickLow"] is False and sam["status"] == "clear"  # new hire: not flagged
    assert rows["Maya Ortiz"]["workedMin"] == 8 * 60 + 19 - 30
    t = out["totals"]
    assert (t["late"], t["early"], t["lunch"], t["missed"], t["action"], t["watch"]) == (1, 1, 1, 1, 1, 3)
    assert out["inProgress"] is False


@need_node
def test_view_forms(tmp_path):
    _, ctx = sample()
    out = view(tmp_path, run(ctx))
    forms = {f["name"]: f for f in out["forms"]}
    # every shift: Dev's handed in after midnight still counts; Lena never handed one in
    assert (forms["Shift handoff log"]["expected"], forms["Shift handoff log"]["done"]) == (4, 3)
    assert forms["Shift handoff log"]["misses"] == [dict(name="Lena Brooks", n=1)]
    # once a week, for Maintenance (a Gusto title or Connecteam job): only Sam worked
    assert (forms["Weekly safety walk"]["expected"], forms["Weekly safety walk"]["done"]) == (1, 1)
    # hour by hour while someone from the Thermal Pub is on: 2pm to 9pm, the 4pm check missed
    tub = forms["Tub temperature check"]
    assert (tub["expected"], tub["done"], tub["missed"]) == (7, 6, 1)
    wed = tub["grid"][3].split(",")
    assert wed[0] == "off" and wed[-1] == "off" and wed[9:16] == ["done", "done", "missed", "done", "done", "done", "done"]
    lena = by_name(out)["Lena Brooks"]
    assert lena["formsMissed"] == 2 and lena["formMisses"] == ["Shift handoff log ×1", "Tub temperature check ×1"]
    assert out["totals"]["formsExpected"] == 12 and out["totals"]["formsMissed"] == 2


@need_node
def test_view_matches_names_and_departments(tmp_path):
    _, ctx = sample()
    out = view(tmp_path, run(ctx))
    rows = by_name(out)
    # Staff Access department first, then the Gusto title
    assert rows["Maya Ortiz"]["dept"] == "Front Office" and rows["Jonah Reyes"]["dept"] is None
    assert rows["Jonah Reyes"]["title"] == "Maintenance" and rows["Lena Brooks"]["title"] == "Thermal Pub"  # by alias
    assert out["fuzzy"] == [["Devendra Patel", "Dev Patel"]] and out["balUnmatched"] == ["Nobody Else"]
    assert out["noBal"] == [] and out["gustoOk"] is True
    assert "Front Office" in out["departments"] and "Maintenance" in out["departments"]
    front = view(tmp_path, run(ctx), dept="Front Office")
    assert [r["name"] for r in front["rows"]] == ["Dev Patel", "Maya Ortiz"]
    maint = view(tmp_path, run(ctx), dept="Maintenance")
    assert [r["name"] for r in maint["rows"]] == ["Jonah Reyes", "Sam Whitaker"]


@need_node
def test_view_tells_people_with_the_same_name_apart(tmp_path):
    ct = FakeCt(
        [dict(user_id="7", name="Ty Nguyen", date="2026-10-12", clock_in="2026-10-12T09:00:00-07:00",
              clock_out="2026-10-12T13:00:00-07:00", break_minutes=0, job="Spa", type="shift"),
         dict(user_id="8", name="Ty Nguyen", date="2026-10-13", clock_in="2026-10-13T09:00:00-07:00",
              clock_out="2026-10-13T13:00:00-07:00", break_minutes=0, job="Pub", type="shift")],
        submissions={"f1": [dict(user_id="8", name="Ty Nguyen", submitted_at="2026-10-13T12:00:00-07:00")]})
    ctx = FakeCtx(ct, settings=dict(forms=[dict(id="f1", name="Log")]),
                  uploads={"gusto": [dict(name="g.csv", text="Employee,Sick,PTO\nTy Nguyen,5,5\n", uploaded_at=1)]})
    out = view(tmp_path, run(ctx))
    assert sorted(r["key"] for r in out["rows"]) == ["u7", "u8"]
    rows = {r["key"]: r for r in out["rows"]}
    assert rows["u8"]["formsMissed"] == 0 and rows["u7"]["formsMissed"] == 1  # credited by ID
    assert rows["u7"]["sick"] is None and out["balUnmatched"] == ["Ty Nguyen"]  # not guessed


@need_node
def test_view_without_timesheets_checks_nobody(tmp_path):
    _, ctx = sample(fail={"timesheets"})
    out = view(tmp_path, run(ctx))
    assert out["totals"]["missed"] == 0 and out["totals"]["unchecked"] == 6
    assert all(r["status"] != "action" or r["missed"] == 0 for r in out["rows"])


@need_node
def test_view_period_in_progress(tmp_path):
    # Wednesday 3:30pm: Lena is still on the clock, Thursday's and later shifts are upcoming
    _, ctx = sample(today=dt.date(2026, 10, 14), now=dt.datetime(2026, 10, 14, 15, 30, tzinfo=LA))
    data = run(ctx)
    for t in data["timesheets"]:
        if t["uid"] == "3":
            t["out"] = None
    out = view(tmp_path, data)
    rows = by_name(out)
    assert out["inProgress"] is True and "On the clock" in rows["Lena Brooks"]["flags"]
    assert rows["Jonah Reyes"]["missed"] == 0 and out["totals"]["missed"] == 0
    tub = {f["name"]: f for f in out["forms"]}["Tub temperature check"]
    assert tub["expected"] == 1 and tub["grid"][3].split(",")[9:12] == ["done", "open", "later"]


@need_node
def test_view_rules_change_the_checks(tmp_path):
    _, ctx = sample(settings=dict(rules=dict(earlySkip=["Front Office"], lateGrace=15, lunchMin=10, actionAt=1)))
    out = view(tmp_path, run(ctx))
    rows = by_name(out)
    assert rows["Dev Patel"]["early"] == [] and rows["Maya Ortiz"]["late"] == []
    assert rows["Lena Brooks"]["lunch"] == []
    assert rows["Jonah Reyes"]["status"] == "action" and rows["Dev Patel"]["status"] == "watch"  # low sick only


@need_node
def test_view_gusto_column_detection(tmp_path):
    _, ctx = sample()
    wide = "Report\n\nName,Job title,PTO balance (hrs),Sick balance (hrs),PTO used\nA B,X,1,2,3\n"
    out = view(tmp_path, run(ctx), gustoTexts=[GUSTO, wide, "Date,Clock in\n1,2\n", ""])
    assert out["gusto"][0] == ["accrued", "balance", "employee", "policy", "title", "used"]
    assert out["gusto"][1] == ["employee", "pto", "sick", "title", "used"]
    assert out["gusto"][2] is None and out["gusto"][3] is None


@need_node
def test_view_periods_match_report_py(tmp_path):
    _, ctx = sample()
    days = [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(0, 420, 3)]
    jobs = [(rep.day_number(d), kind, ws) for d in days for kind in ("week", "pay", "month") for ws in (0, 1, 6)]
    out = view(tmp_path, run(ctx), periods=jobs)
    for (day, kind, ws), got in zip(jobs, out["periods"]):
        start, end = rep.period_of(rep.EPOCH + dt.timedelta(days=day), kind, ws)
        assert (got["start"], got["end"]) == (rep.day_number(start), rep.day_number(end))


@need_node
def test_view_summary_and_csv(tmp_path):
    ct, ctx = sample()
    data = run(ctx)
    data["users"]["6"] = "=Sum(1)"
    out = view(tmp_path, data)
    assert out["summary"].startswith("Attendance — week of Oct 11 – 17, 2026")
    assert "Needs action\n• Jonah Reyes: missed 1 shift (8h 30m) — sick 6h, PTO 0h → Short 2h 30m" in out["summary"]
    lines = out["csv"].split("\n")
    assert lines[0].startswith("Period start,Period end,Employee,Department,Status,Hours worked")
    assert '"Oct 11, 2026","Oct 17, 2026",Jonah Reyes,Maintenance,action' in lines[1]
    assert any(",'=Sum(1)," in line for line in lines)  # not run as a formula
    assert out["file"] == "attendance-week-2026-10-11.csv"
