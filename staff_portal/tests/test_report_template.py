"""The template report (app/reports/_template/), and a pattern to copy for a new report: load its
folder as the add-on does, then call its data() with a FakeCtx whose ct is a StubCt (no add-on,
no Connecteam). Copy this file to tests/test_report_<key>.py and change the folder and data.
"""

import asyncio
import os
import shutil
import subprocess
from datetime import date

import pytest

from report_helpers import REPORTS_DIR, FakeCtx, StubCt, Unavailable, load_report, settings_for

FOLDER = "_template"


def punch(name, day, hours, kind="shift"):
    return {"user_id": "7", "name": name, "date": day, "clock_in": f"{day}T09:00",
            "clock_out": f"{day}T17:00", "break_minutes": 0, "job": "", "type": kind,
            "hours": hours, "sched_start": None, "sched_end": None}


def run(report, ctx, **params):
    return asyncio.run(report.data(ctx, {**{k: v for k, v in report.params.items()}, **params}))


def test_it_loads_as_the_add_on_loads_it():
    report = load_report(FOLDER)
    assert report.key == "template" and report.hidden   # "_" folders are left out of the lists
    assert report.params == {"week": ""}
    assert [u["key"] for u in report.uploads] == ["targets"]
    assert report.settings == {"title": "Hours this week"}


def test_hours_add_up_for_the_week_asked_for():
    report = load_report(FOLDER)
    ct = StubCt(timesheets=[punch("Ana", "2026-10-19", 8), punch("Ana", "2026-10-20", 6.5),
                            punch("Bob", "2026-10-21", 4), punch("Bob", "2026-10-21", None),
                            punch("Bob", "2026-10-21", 0.5, kind="break"),
                            punch("Cy", "2026-10-26", 9)])   # the next week
    ctx = FakeCtx(ct=ct, settings=settings_for(report))
    data = run(report, ctx, week="2026-10-19")
    assert data["week"] == "2026-10-19"
    assert data["people"] == [{"name": "Ana", "hours": 14.5, "target": None},
                              {"name": "Bob", "hours": 4, "target": None}]
    assert ct.calls == [("timesheets", date(2026, 10, 19), date(2026, 10, 25))]


def test_blank_week_is_this_week_in_the_property_zone():
    report = load_report(FOLDER)
    ctx = FakeCtx(settings=settings_for(report), today=date(2026, 10, 29))   # a Thursday
    assert run(report, ctx)["week"] == "2026-10-26"


def test_targets_come_from_the_newest_upload_and_settings_from_the_saved_ones():
    report = load_report(FOLDER)
    ctx = FakeCtx(ct=StubCt(timesheets=[punch("Ana", "2026-10-19", 8)]),
                  settings=settings_for(report, {"title": "Ana's hours"}),
                  uploads={"targets": [{"name": "new.csv", "text": "Name,Hours\nAna,30\n",
                                        "uploaded_at": 2, "by": "Bob"},
                                       {"name": "old.csv", "text": "Ana,10\n", "uploaded_at": 1,
                                        "by": "Bob"}]})
    data = run(report, ctx, week="2026-10-19")
    assert data["title"] == "Ana's hours"
    assert data["people"][0]["target"] == 30
    assert data["targets_file"] == "new.csv"


def test_a_bad_week_is_a_plain_error_and_connecteam_errors_pass_through():
    report = load_report(FOLDER)
    with pytest.raises(ValueError, match="Pick a week"):
        run(report, FakeCtx(settings=settings_for(report)), week="soon")
    with pytest.raises(Unavailable, match="scope"):
        run(report, FakeCtx(ct=StubCt(unavailable="Add the time_clock.read scope."),
                            settings=settings_for(report)), week="2026-10-19")


def test_report_py_imports_only_the_standard_library():
    with open(os.path.join(REPORTS_DIR, FOLDER, "report.py"), encoding="utf-8") as f:
        imports = [line.split()[1] for line in f if line.startswith(("import ", "from "))]
    assert set(imports) <= {"csv", "io", "datetime"}


@pytest.mark.skipif(not shutil.which("node"), reason="node isn't installed")
def test_view_js_is_valid():
    subprocess.run(["node", "--check", os.path.join(REPORTS_DIR, FOLDER, "view.js")], check=True)
