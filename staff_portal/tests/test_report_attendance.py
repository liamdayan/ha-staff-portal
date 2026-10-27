"""Attendance & Leave report: report.py's fetching and tidying (with the shared report helpers,
see attendance_fake.py), who can see whom, the report through the staff server with the real
ctdata against the fake Connecteam, and view.js's calculations under Node (skipped when node
isn't installed). Nothing here talks to Connecteam or Gusto."""

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

def test_report_imports_only_the_standard_library():
    tree = ast.parse(open(os.path.join(REPORT_DIR, "report.py")).read())
    found = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    found |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert found == {"datetime", "json", "re", "unicodedata"}


def test_report_definition():
    r = rep.loaded   # as the add-on loads it
    assert r.key == "attendance" and r.name == "Attendance & Leave" and r.description and not r.hidden
    assert [u["key"] for u in r.uploads] == ["gusto"] and r.uploads[0]["exts"] == [".csv"]
    assert r.settings == {"rules": rep.DEFAULT_RULES, "forms": [], "who_sees": "all"}
    assert r.check_settings is rep.check_settings and r.has_css
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
    assert forms[0] == dict(id="a", name="Untitled form", frequency="hour", applies_to="", every=1, night_every=0,
                            day_from="06:00", day_until="22:00")
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
    assert ("forms",) not in ct.calls and d["form_choices"] == [] and d["own_only"] is False
    assert ctx.upload_keys == ["gusto"]
    assert (d["start"], d["end"], d["period"]) == ("2026-10-11", "2026-10-17", "week")
    assert d["fetch_start"] == rep.day_number(lo) and d["fetch_end"] == rep.day_number(hi)


def test_form_list_only_for_managers_who_ask():
    ct, ctx = sample(manage=True)
    assert run(ctx)["form_choices"] == [] and ("forms",) not in ct.calls
    d = run(ctx, {"period": "week", "at": "2026-10-14", "form_list": "1"})
    assert [f["id"] for f in d["form_choices"]] == [f["id"] for f in FORMS]
    ct, ctx = sample()
    assert run(ctx, {"form_list": "1"})["form_choices"] == [] and ("forms",) not in ct.calls


def test_rows_are_normalized():
    _, ctx = sample()
    d = run(ctx)
    maya, lunch = [t for t in d["timesheets"] if t["uid"] == "1"]
    assert maya["in"] % 1440 == 6 * 60 + 12 and maya["out"] % 1440 == 14 * 60 + 31 and maya["brk"] == 0
    # A lunch taken with the Break button is its own row, as ctdata gives it.
    assert lunch["type"] == "break" and lunch["brk"] == 30 and maya["in"] < lunch["in"] < maya["out"]
    assert maya["day"] == rep.day_number(dt.date(2026, 10, 12)) and maya["job"] == "Front Desk"
    priya = next(t for t in d["timesheets"] if t["uid"] == "5")
    assert priya["in"] is None and priya["hours"] == 7.5 and priya["type"].startswith("Time off")
    # ctdata's time off rows read as the view expects: "Time off - <policy>"
    off = rep.timesheet_rows([dict(user_id="5", name="P", date="2026-10-16", clock_in="2026-10-16T08:00",
                                   clock_out=None, break_minutes=0, job="Sick leave", type="time_off",
                                   hours=7.5)], LA)[0]
    assert off["type"] == "Time off - Sick leave" and off["job"] == "Sick leave"
    assert {(s["draft"], s["open"]) for s in d["shifts"]} == {(False, False), (True, False), (False, True)}
    # from Connecteam smart groups: the first department whose group someone is in
    assert d["departments"] == {"1": "Front Office", "2": "Front Office", "4": "Maintenance",
                                "6": "Maintenance"}
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


def test_departments_from_smart_groups():
    departments = [dict(id=1, name="Spa", smart_group_id="10"), dict(id=2, name="Pub", smart_group_id=20),
                   dict(id=3, name="Desk", smart_group_id=None), "junk"]
    users = [dict(user_id="7", name="A", groups=["20", "10"]), dict(user_id="8", name="B", groups=["20"]),
             dict(user_id="9", name="C", groups=[]), dict(user_id="10", name="D")]
    assert rep.department_map(departments, users) == {"7": dict(id=1, name="Spa"),
                                                      "8": dict(id=2, name="Pub")}


def test_departments_chosen_here_come_first():
    """Someone linked to Connecteam has the department they have here (set by hand, maybe one
    with no smart group); smart groups only for those not linked."""
    settings = dict(who_sees="own")
    staff = [dict(connecteam_user_id="5", department_id=3), dict(connecteam_user_id="4", department_id=1),
             dict(connecteam_user_id="9", department_id=99)]
    _, ctx = sample(staff=staff, settings=settings,
                    person={"id": 5, "name": "Spa lead", "departments": [{"id": 3, "name": "Spa"}]})
    d = run(ctx)
    assert d["departments"] == {"5": "Spa"} and {t["uid"] for t in d["timesheets"]} == {"5"}
    _, ctx = sample(staff=staff, manage=True)
    d = run(ctx)
    assert d["departments"] == {"1": "Front Office", "2": "Front Office", "4": "Front Office",
                                "5": "Spa", "6": "Maintenance"}


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
    assert d["settings"] == dict(rules=rep.DEFAULT_RULES, forms=[], who_sees="all") and d["forms"] == []


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
    # unchecked times (overnight, set to 0) aren't shown: 6a to 10p, one square an hour
    assert len(wed) == 16 and "off" not in wed and wed[8:15] == ["done", "done", "missed", "done", "done", "done", "done"]
    lena = by_name(out)["Lena Brooks"]
    assert lena["formsMissed"] == 2 and lena["formMisses"] == ["Shift handoff log ×1", "Tub temperature check ×1"]
    # the shift she didn't hand it in shows on that day
    assert [names for _, names in lena["dayFormMisses"]] == [["Shift handoff log"]]
    assert by_name(out)["Dev Patel"]["dayFormMisses"] == []
    assert out["totals"]["formsExpected"] == 12 and out["totals"]["formsMissed"] == 2


@need_node
def test_view_matches_names_and_departments(tmp_path):
    _, ctx = sample()
    out = view(tmp_path, run(ctx))
    rows = by_name(out)
    # Staff Access department first, then the Gusto title
    assert rows["Maya Ortiz"]["dept"] == "Front Office" and rows["Jonah Reyes"]["dept"] == "Maintenance"
    assert rows["Lena Brooks"]["dept"] is None and rows["Lena Brooks"]["title"] == "Thermal Pub"  # by alias
    assert out["fuzzy"] == [["Devendra Patel", "Dev Patel"]] and out["balUnmatched"] == ["Nobody Else"]
    assert out["noBal"] == [] and out["gustoOk"] is True
    assert "Front Office" in out["departments"] and "Maintenance" in out["departments"]
    front = view(tmp_path, run(ctx), dept="Front Office")
    assert [r["name"] for r in front["rows"]] == ["Dev Patel", "Maya Ortiz"]
    # Dev is in both smart groups: shown under Front Office (the first), and in either filter.
    maint = view(tmp_path, run(ctx), dept="Maintenance")
    assert sorted(r["name"] for r in maint["rows"]) == ["Dev Patel", "Jonah Reyes", "Sam Whitaker"]


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
def test_view_counts_breaks_taken_with_the_break_button(tmp_path):
    """ctdata puts a manual break on its own row inside the shift. It counts as lunch and isn't
    worked time; a shift split around the same break doesn't count it twice."""
    def row(day, a, b, kind="shift", brk=0):
        return dict(user_id="7", name="Ann Lee", date=f"2026-10-{day}", clock_in=f"2026-10-{day}T{a}",
                    clock_out=f"2026-10-{day}T{b}", break_minutes=brk, job="Break" if kind == "break" else "Spa",
                    type=kind)
    ct = FakeCt([row(12, "08:00", "16:30"), row(12, "12:00", "12:30", "break", 30),
                 row(13, "08:00", "12:00"), row(13, "12:00", "12:30", "break", 30), row(13, "12:30", "16:30")])
    out = view(tmp_path, run(FakeCtx(ct)))
    ann = by_name(out)["Ann Lee"]
    assert ann["lunch"] == [] and "No lunch" not in ann["flags"]
    assert ann["workedMin"] == 2 * 8 * 60


@need_node
def test_view_a_clock_in_and_out_in_the_same_minute_is_a_blip(tmp_path):
    """Times with a date are never moved a day on: a 20-second punch stays a blip, not 24 hours."""
    ct = FakeCt([dict(user_id="7", name="Ann Lee", date="2026-10-13", clock_in="2026-10-13T09:00",
                      clock_out="2026-10-13T09:00", break_minutes=0, job="Spa", type="shift")],
                [dict(user_id="7", name="Ann Lee", date="2026-10-13", start="2026-10-13T09:00",
                      end="2026-10-13T17:00", job="Spa", title="")])
    d = run(FakeCtx(ct))
    assert d["timesheets"][0]["out"] == d["timesheets"][0]["in"]
    ann = by_name(view(tmp_path, d))["Ann Lee"]
    assert ann["workedMin"] == 0 and ann["missed"] == 1
    assert ann["flags"] == ["No-show · 8h", "Clocked in and out at once"]


@need_node
def test_view_notes_people_scheduled_with_no_punches_at_all(tmp_path):
    """Jonah is scheduled and never punched: his no-show gets the original's hint that he may not
    be on a time clock yet."""
    _, ctx = sample()
    out = view(tmp_path, run(ctx))
    assert out["noPunches"] == ["Jonah Reyes"]
    _, ctx = sample(fail={"timesheets"})
    assert view(tmp_path, run(ctx))["noPunches"] == []


@need_node
def test_view_switching_period_goes_where_expected(tmp_path):
    """Month → Week shows this week, even from a past month; a longer period holds the one
    being looked at."""
    _, ctx = sample()
    d = run(ctx)
    today = d["today"]
    sept = (today - 50, today - 21)               # a past month
    week = (today - 16, today - 10)               # a past week
    out = view(tmp_path, d, anchors=[[*sept, "month", "week", today], [*sept, "month", "pay", today],
                                     [*week, "week", "month", today], [*week, "week", "pay", today],
                                     [today - 3, today + 3, "week", "month", today]])
    assert out["anchors"] == [today, today, week[0], week[0], today]


def test_view_shows_the_title_once():
    """The staff app puts the report's name above it; the view doesn't repeat it."""
    js = open(VIEW).read()
    head = js[js.index("function header()"):js.index("function banner()")]
    assert "Attendance & Leave" not in head and '"h1"' not in head


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
    assert tub["expected"] == 1 and tub["grid"][3].split(",")[8:11] == ["done", "open", "later"]


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


# ---------------------------------------------------------------- who can see whom

def test_everyone_sees_all_departments_by_default():
    _, ctx = sample()
    d = run(ctx)
    assert d["own_only"] is False and {t["uid"] for t in d["timesheets"]} == {"1", "2", "3", "5", "6"}
    assert "Whitaker" in d["gusto"]["text"]


def test_viewers_see_only_their_own_department_on_the_server():
    settings = dict(rules=dict(aliases="Rose Brooks = Lena Brooks"), who_sees="own",
                    forms=[dict(id="f1", name="Shift handoff log")])
    _, ctx = sample(settings=settings)   # Liam views the report from the Front Office
    d = run(ctx)
    assert d["own_only"] is True
    assert {t["uid"] for t in d["timesheets"]} == {"1", "2"}
    assert {s["uid"] for s in d["shifts"]} == {"1", "2", None}   # and the open shift nobody took
    assert {s["uid"] for s in d["forms"][0]["subs"]} == {"1", "2"}
    assert d["users"] == {"1": "Maya Ortiz", "2": "Dev Patel"}
    assert d["departments"] == {"1": "Front Office", "2": "Front Office"}
    # Gusto: the title lines, the headers, and only Maya's and Dev's (by short name) rows.
    lines = d["gusto"]["text"].splitlines()
    assert lines[:3] == ["Time Off Balances Report", "Sample Lodge Co,",
                         "Employee,Title,Policy,Accrued time,Hours used,Ending balance"]
    assert [line.split(",")[1].strip('" ') for line in lines[3:]] == ["Maya", "Maya", "Devendra", "Devendra"]
    for name in ("Reyes", "Brooks", "Shah", "Whitaker", "Else"):   # not even in the name matches
        assert name not in json.dumps(d)
    # Those who manage the report see everyone.
    _, ctx = sample(settings=settings, manage=True)
    d = run(ctx)
    assert d["own_only"] is False and len({t["uid"] for t in d["timesheets"]}) == 5


def test_own_department_without_a_department_or_the_user_list_shows_nobody():
    settings = dict(who_sees="own")
    _, ctx = sample(settings=settings, person={"id": 9, "name": "Cy", "connecteam_user_id": None,
                                               "departments": []})
    d = run(ctx)
    assert d["timesheets"] == [] and d["users"] == {} and d["gusto"]["text"].count("\n") == 3
    _, ctx = sample(settings=settings, fail={"users"})
    d = run(ctx)
    assert d["timesheets"] == [] and any(e.startswith("People:") for e in d["errors"])


def test_gusto_rows_are_kept_by_name_alias_and_short_name():
    everyone = {"maya ortiz", "dev patel", "lena brooks", "jonah reyes"}
    text = ("Employee First,Employee Last,Sick\nMaya,Ortiz,1\nRose,Brooks,2\nJonah,Reyes,3\n"
            "Nobody,Known,4\n")
    kept = rep.gusto_for(text.replace("Employee First,Employee Last", "First name,Last name"),
                         {"maya ortiz", "lena brooks"}, everyone, "Rose Brooks = Lena Brooks")
    assert kept == "First name,Last name,Sick\nMaya,Ortiz,1\nRose,Brooks,2\n"
    assert rep.gusto_for("no,headers\n1,2\n", {"maya ortiz"}, everyone, "") == ""
    assert rep.parse_csv('a;b\n"x;""y""";2\n') == [["a", "b"], ['x;"y"', "2"]]
    assert rep.name_key("Ortiz, Maya J.") == "maya ortiz" and rep.name_key("José  Núñez") == "jose nunez"


def test_gusto_rows_are_kept_only_by_their_name_column():
    """A Manager or Approved by column naming an allowed person doesn't keep someone else's row,
    and a relative who isn't in Connecteam isn't kept by a short name when the allowed person has
    a row of their own. Tom matches Thomas (a common short name), still only when unique."""
    everyone = {"dev patel", "maria lopez", "tom baker"}
    text = ("Time Off Balances\nEmployee,Title,Manager,Sick,PTO\nDev Patel,Front Desk,,10,5\n"
            "Devi Patel,Housekeeping,,3,1\nMaria Lopez,Housekeeping,Dev Patel,4,4\nLopez Dev,Kitchen,,1,1\n"
            '"Baker, Thomas",Housekeeping,,2,2\n')
    kept = rep.gusto_for(text, {"dev patel"}, everyone, "").splitlines()
    assert kept == ["Time Off Balances", "Employee,Title,Manager,Sick,PTO", "Dev Patel,Front Desk,,10,5"]
    kept = rep.gusto_for(text, {"tom baker"}, everyone, "").splitlines()
    assert kept[2:] == ['"Baker, Thomas",Housekeeping,,2,2']
    # First and last name columns; two Gusto names for one person are both left out.
    two = "First name,Last name,Sick\nThomas,Baker,1\nTommy,Baker,2\n"
    assert rep.gusto_for(two, {"tom baker"}, everyone, "").splitlines()[1:] == []
    assert rep.match_names({"devi patel"}, everyone, {}) == {"devi patel": "dev patel"}  # no row of his own
    assert rep.match_names({"devi patel", "dev patel"}, everyone, {}) == {"dev patel": "dev patel"}


@need_node
def test_view_matches_nicknames_like_the_server(tmp_path):
    """view.js and report.py share the short-name list, and match Gusto names the same way."""
    names = subprocess.run(["node", "-e", f"process.stdout.write(JSON.stringify(require({json.dumps(VIEW)}).NICKNAMES))"],
                           capture_output=True, text=True, check=True).stdout
    assert json.loads(names) == rep.NICKNAMES
    gusto = ("Employee,Title,Manager,Sick,PTO\n\"Baker, Thomas\",Housekeeping,,2,2\nDev Patel,Front Desk,,10,5\n"
             "Devi Patel,Housekeeping,,3,1\nMaria Lopez,Housekeeping,Dev Patel,4,4\n")
    ct = FakeCt([dict(user_id=u, name=n, date="2026-10-12", clock_in="2026-10-12T09:00",
                      clock_out="2026-10-12T13:00", break_minutes=0, job="Spa", type="shift")
                 for u, n in (("1", "Tom Baker"), ("2", "Dev Patel"), ("3", "Maria Lopez"))])
    out = view(tmp_path, run(FakeCtx(ct, uploads={"gusto": [dict(name="g.csv", text=gusto, uploaded_at=1)]})))
    rows = by_name(out)
    assert rows["Tom Baker"]["sick"] == 2 and rows["Dev Patel"]["sick"] == 10 and rows["Maria Lopez"]["sick"] == 4
    assert out["fuzzy"] == [["Thomas Baker", "Tom Baker"]] and out["balUnmatched"] == ["Devi Patel"]


def test_a_gusto_upload_is_small_enough_to_filter_quickly():
    """Filtering runs for each viewer's open, in the add-on's one event loop: the upload is capped
    at 256 KB (Gusto's own file is a few KB), and the biggest one takes well under a second."""
    import time
    upload = rep.loaded.uploads[0]
    assert upload["key"] == "gusto" and upload["max_bytes"] == 256 * 1024
    everyone = {f"first{i} last{i}" for i in range(300)}
    text = "Employee,Title,Policy,Balance\n" + "".join(
        f'"Last{i % 400}, First{i % 400}",Desk,Sick,{i}\n' for i in range(8000))
    text = text[:upload["max_bytes"]]
    began = time.process_time()
    rep.gusto_for(text, {"first1 last1"}, everyone, "")
    assert time.process_time() - began < 1


def test_settings_are_cleaned_and_kept_small():
    saved = rep.check_settings(dict(who_sees="everyone", rules=dict(aliases="x" * 50000),
                                    forms=[dict(id=str(i), applies_to="y" * 5000) for i in range(30)],
                                    other="dropped"))
    assert saved["who_sees"] == "all" and len(saved["rules"]["aliases"]) == rep.ALIASES_LEN
    assert len(saved["forms"]) == rep.MAX_FORMS and set(saved) == {"rules", "forms", "who_sees"}
    assert len(json.dumps(saved)) <= rep.SETTINGS_ROOM
    with pytest.raises(ValueError, match="too long"):
        rep.check_settings(dict(rules=dict(aliases="é" * rep.ALIASES_LEN),
                                forms=[dict(id=str(i), applies_to="é" * 1000) for i in range(20)]))


# ---------------------------------------------------------------- through the staff server

def test_the_report_through_the_server_with_the_real_ctdata(tmp_path, monkeypatch):
    """The add-on's own report, ctdata and reports lane against the fake Connecteam: viewers in
    one department see only their own people once the owner's setting says so, managers see
    everyone and save the settings, cleaned."""
    from hr_helpers import FakeConnecteam, ct_activity, ct_user, fresh_connecteam
    from test_wristbands_server import Portal
    import connecteam
    import main

    monkeypatch.setattr(connecteam, "REPORTS_MINUTE_CAP", 1000)
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path))
    main.recent_report_requests.clear()
    p = Portal(tmp_path / "db")
    p.db.update_settings({"connecteam_enabled": "1", "connecteam_client_id": "client",
                          "connecteam_client_secret": "secret", "wb_time_zone": "America/Los_Angeles"})
    front = p.db.create_department("Front Office", smart_group_id="501")
    maint = p.db.create_department("Maintenance", smart_group_id="502")
    p.db.update_staff(p.ana, department_ids=[front])
    p.db.update_staff(p.bob, department_ids=[maint])
    p.db.set_report("attendance", "test", enabled=True, view=[front], manage=[maint],
                    settings={"who_sees": "own", "rules": {"aliases": "Jon Reyes = Jonah Reyes"},
                              "forms": [{"id": "5", "name": "Log", "applies_to": "Jonah Reyes, Front Office"}]})
    fresh_connecteam()

    def stamp(day, hm):
        return int(dt.datetime.fromisoformat(f"2026-10-{day}T{hm}").replace(tzinfo=LA).timestamp())
    ct = FakeConnecteam(users=[ct_user(7, "Maya", "Ortiz", groups=[501]),
                               ct_user(8, "Jonah", "Reyes", groups=[502])])
    ct.activities = {1: [ct_activity(7, shifts=[(stamp(12, "06:12"), stamp(12, "14:31"), None, None)]),
                         ct_activity(8, shifts=[(stamp(13, "09:00"), stamp(13, "17:00"), None, None)])]}

    async def scenario():
        async with p, ct:
            ana, bob = await p.sign_in(p.ana), await p.sign_in(p.bob)
            path = "/api/reports/attendance?period=week&at=2026-10-14"
            status, body = await p.call("GET", path, ana)
            assert status == 200, body
            d = body["data"]
            assert body["manage"] is False and d["own_only"] is True
            assert {t["uid"] for t in d["timesheets"]} == {"7"} and d["users"] == {"7": "Maya Ortiz"}
            # Not even in the settings sent with it (name matches, who must submit a form).
            assert d["departments"] == {"7": "Front Office"} and "Reyes" not in json.dumps(body)
            assert body["settings"]["forms"][0]["applies_to"] == "Front Office"
            # Schedules weren't set up in the fake: the rest of the report still shows.
            assert d["have"]["timesheets"] is True
            status, body = await p.call("GET", path, bob)
            assert body["manage"] is True and body["data"]["own_only"] is False
            assert {t["uid"] for t in body["data"]["timesheets"]} == {"7", "8"}
            # Only those who manage it change who sees whom.
            status, _ = await p.call("PUT", "/api/reports/attendance/settings", ana, {"who_sees": "all"})
            assert status == 403
            status, body = await p.call("PUT", "/api/reports/attendance/settings", bob,
                                        {"who_sees": "all", "rules": {"lateGrace": "9"}, "x": 1})
            assert status == 200 and body["settings"]["who_sees"] == "all"
            assert body["settings"]["rules"]["lateGrace"] == 9 and "x" not in body["settings"]
            status, body = await p.call("GET", path, ana)
            assert {t["uid"] for t in body["data"]["timesheets"]} == {"7", "8"}
    asyncio.run(scenario())


def test_a_timed_forms_checked_hours():
    hours = lambda **f: {k: v for k, v in rep.clean_forms([dict(id="a", frequency="hour", **f)])[0].items()
                         if k.startswith("day_")}
    assert hours(day_from="7:30", day_until="23:00") == dict(day_from="07:30", day_until="23:00")
    assert hours(day_from="08:00", day_until="00:00") == dict(day_from="08:00", day_until="00:00")   # to midnight
    for bad in (dict(day_from="22:00", day_until="06:00"), dict(day_from="25:00", day_until="23:00"),
                dict(day_from="<b>", day_until=""), {}):
        assert hours(**bad) == dict(day_from="06:00", day_until="22:00")


@need_node
def test_view_a_timed_form_checks_only_its_own_hours(tmp_path):
    _, ctx = sample()
    data = run(ctx)
    tub = next(f for f in data["settings"]["forms"] if f["name"] == "Tub temperature check")
    tub.update(day_from="15:00", day_until="20:00", every=1, night_every=0)
    for f in data["forms"]:
        if f["name"] == tub["name"]:
            f.update(day_from="15:00", day_until="20:00", every=1, night_every=0)
    out = view(tmp_path, data)
    got = {f["name"]: f for f in out["forms"]}["Tub temperature check"]
    wed = got["grid"][3].split(",")
    # 3p to 8p only: five squares, the 4pm check still missed
    assert len(wed) == 5 and wed == ["done", "missed", "done", "done", "done"]
    assert (got["expected"], got["missed"]) == (5, 1)
