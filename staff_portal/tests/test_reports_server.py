"""Reports in the staff app and the dashboard: finding report folders, access by department (view
and manage) checked on every request, running a report, uploads and settings, the view.js
addresses, the Reports tab, and backups and migration files. A copy of the template report
(app/reports/_template/) is installed as "hours", with ctdata answered by a stand-in. Nothing here
talks to Connecteam, TT Hotel or Home Assistant."""

import asyncio
import json
import os
import shutil

import pytest
from aiohttp import FormData

from report_helpers import REPORTS_DIR
from test_migrate_and_backup_logs import Fresh
from test_wristbands_server import Portal, fake_area_layout, fake_states

import ctdata  # noqa: E402
import main  # noqa: E402
import reports as reports_kit  # noqa: E402

ROW = {"user_id": "7", "name": "Ana", "date": "2026-10-19", "clock_in": "2026-10-19T09:00",
       "clock_out": "2026-10-19T17:00", "break_minutes": 0, "job": "", "type": "shift",
       "hours": 8.0, "sched_start": None, "sched_end": None}


def install(tmp_path, key="hours") -> "reports_kit.Report":
    """The template report copied to a folder of its own, as someone adding a report would."""
    folder = tmp_path / "reports" / key
    shutil.copytree(os.path.join(REPORTS_DIR, "_template"), folder)
    for name in ("report.py", "view.js"):
        path = folder / name
        path.write_text(path.read_text().replace('"template"', f'"{key}"'))
    return reports_kit.load(str(folder))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A portal with two departments (Ana and Dee in Front, Bob in Kitchen, Cy in none), the
    "hours" report installed, and this test's own data folder."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(main, "DATA_DIR", str(data))
    report = install(tmp_path)
    monkeypatch.setitem(main.REPORTS, "hours", report)
    asked = []

    async def timesheets(start, end):
        asked.append((start, end))
        return [ROW]
    monkeypatch.setattr(ctdata, "timesheets", timesheets)
    monkeypatch.setattr(main, "area_layout", fake_area_layout)
    monkeypatch.setattr(main.ha, "states", fake_states)
    main.recent_report_requests.clear()
    p = Portal(tmp_path / "db")
    p.front = p.db.create_department("Front")
    p.kitchen = p.db.create_department("Kitchen")
    for person, dept in ((p.ana, p.front), (p.dee, p.front), (p.bob, p.kitchen)):
        p.db.update_staff(person, department_ids=[dept])
    p.report, p.asked, p.data_dir = report, asked, str(data)
    return p


def go(coro):
    return asyncio.run(coro)


async def share(p, enabled=True, view=None, manage=None):
    status, body = await p.admin_call("PUT", "/api/reports/hours", {
        "enabled": enabled, "view": [p.front] if view is None else view,
        "manage": [p.kitchen] if manage is None else manage})
    assert status == 200, body


async def upload(p, who, text, name="targets.csv", key="targets"):
    form = FormData()
    form.add_field("file", text if isinstance(text, bytes) else text.encode(), filename=name,
                   content_type="text/csv")
    resp = await p.staff.post(f"/api/reports/hours/uploads/{key}", data=form, headers=who)
    return resp.status, await resp.json()


# ---------------------------------------------------------------- finding reports

def test_report_folders_are_found_and_broken_ones_left_out(tmp_path):
    install(tmp_path, "hours")
    bad = tmp_path / "reports" / "broken"
    bad.mkdir()
    (bad / "report.py").write_text("REPORT = dict(key='broken', data=lambda ctx, p: {})\n")
    (bad / "view.js").write_text("")
    wrong = tmp_path / "reports" / "wrong"
    wrong.mkdir()
    (wrong / "report.py").write_text("raise RuntimeError('oops')\n")
    (tmp_path / "reports" / "Not-A-Key").mkdir()
    found = reports_kit.discover(str(tmp_path / "reports"))
    assert list(found) == ["hours"]
    assert found["hours"].has_css and not found["hours"].hidden


def test_the_template_ships_hidden():
    assert main.REPORTS["template"].hidden
    assert set(main.REPORTS) >= {"template"}


def test_uploaded_file_names_are_only_names():
    upload = reports_kit._uploads([{"key": "gusto"}])[0]
    assert reports_kit.check_file(upload, "../../etc/x.csv", b"a,b\n")[0] == "x.csv"
    assert reports_kit.check_file(upload, "C:\\fakepath\\Gusto.CSV", b"a\n")[0] == "Gusto.CSV"
    assert reports_kit.check_file(upload, "b\u202ead.csv", b"a\n")[0] == "bad.csv"
    assert reports_kit.check_file(upload, "x.csv", "caf\u00e9".encode("cp1252"))[1] == "caf\u00e9"


def test_a_report_declares_what_it_needs():
    with pytest.raises(ValueError, match="text file type"):
        reports_kit._uploads([{"key": "x", "accept": ".exe"}])
    assert reports_kit._uploads([{"key": "x", "accept": "text/csv, .txt"}])[0]["exts"] == [".csv", ".txt"]
    assert reports_kit._params([{"key": "week", "default": "x"}, "dept"]) == {"week": "x", "dept": None}
    with pytest.raises(ValueError):
        reports_kit._settings(["bad key"])


# ---------------------------------------------------------------- who sees what

def test_reports_are_off_until_the_owner_shares_them(setup):
    p = setup

    async def scenario():
        async with p:
            ana = await p.sign_in(p.ana)
            status, body = await p.call("GET", "/api/reports", ana)
            assert status == 200 and body == {"reports": []}
            assert (await p.call("GET", "/api/entities", ana))[1]["reports"] is False
            status, body = await p.call("GET", "/api/reports/hours", ana)
            assert status == 403 and body["error"] == main.NO_REPORT
            assert p.asked == []   # nothing asked of Connecteam
    go(scenario())


def test_departments_decide_who_views_and_who_manages(setup):
    p = setup

    async def scenario():
        async with p:
            await share(p)
            ana, bob, cy = (await p.sign_in(p.ana), await p.sign_in(p.bob), await p.sign_in(p.cy))
            status, body = await p.call("GET", "/api/reports", ana)
            listed = body["reports"][0]
            assert (listed["key"], listed["name"], listed["manage"]) == ("hours", "Hours this week", False)
            assert listed["view_js"].startswith("/reports/hours/view.js?a=")
            assert listed["uploads"][0]["key"] == "targets" and listed["uploads"][0]["latest"] is None
            assert (await p.call("GET", "/api/entities", ana))[1]["reports"] is True
            # Managing includes viewing.
            assert (await p.call("GET", "/api/reports", bob))[1]["reports"][0]["manage"] is True
            # No department: nothing.
            assert (await p.call("GET", "/api/reports", cy))[1]["reports"] == []
            assert (await p.call("GET", "/api/reports/hours", cy))[0] == 403
            assert (await p.call("GET", "/api/entities", cy))[1]["reports"] is False
            # Turned off: gone for everyone, at once.
            await share(p, enabled=False)
            assert (await p.call("GET", "/api/reports/hours", bob))[0] == 403
            assert (await p.call("GET", "/api/reports", bob))[1]["reports"] == []
    go(scenario())


def test_a_kiosk_device_without_a_person_shows_no_reports(setup):
    p = setup

    async def scenario():
        async with p:
            await share(p)
            device_id = p.db.create_device("Pool phone", p.front_desk, None)
            code = p.db.device(device_id)["enroll_code"]
            _, token = p.db.claim_enroll_code(code)
            kiosk = {"Authorization": f"Bearer {token}"}
            status, body = await p.call("GET", "/api/reports", kiosk)
            assert status == 403 and body["error"] == main.NO_PERSON
            assert (await p.call("GET", "/api/reports/hours", kiosk))[0] == 403
            assert (await p.call("GET", "/api/entities", kiosk))[1]["reports"] is False
            # Not signed in at all on the shared computer: asked for a PIN.
            status, body = await p.call("GET", "/api/reports", {"Authorization": f"Bearer {p.token}"})
            assert status == 403 and body["error"] == "pin_required"
    go(scenario())


def test_moving_department_or_being_turned_off_takes_effect_at_once(setup):
    p = setup

    async def scenario():
        async with p:
            await share(p)
            ana = await p.sign_in(p.ana)
            assert (await p.call("GET", "/api/reports/hours", ana))[0] == 200
            p.db.update_staff(p.ana, department_ids=[])
            assert (await p.call("GET", "/api/reports/hours", ana))[0] == 403
            p.db.update_staff(p.ana, department_ids=[p.front], status="disabled")
            assert (await p.call("GET", "/api/reports/hours", ana))[0] == 403
    go(scenario())


# ---------------------------------------------------------------- running a report

def test_running_a_report_with_its_params(setup):
    p = setup

    async def scenario():
        async with p:
            await share(p)
            ana = await p.sign_in(p.ana)
            status, body = await p.call("GET", "/api/reports/hours?week=2026-10-19&other=x", ana)
            assert status == 200, body
            assert body["data"]["people"] == [{"name": "Ana", "hours": 8.0, "target": None}]
            assert body["settings"] == {"title": "Hours this week"} and body["manage"] is False
            assert body["params"] == {"week": "2026-10-19"}
            assert [str(a) for a in p.asked[0]] == ["2026-10-19", "2026-10-25"]
            status, body = await p.call("GET", "/api/reports/hours?week=soon", ana)
            assert status == 400 and body["error"] == "Pick a week."
    go(scenario())


def test_connecteam_trouble_and_report_bugs_are_plain_messages(setup, monkeypatch):
    p = setup

    async def unavailable(start, end):
        raise ctdata.Unavailable("Connecteam refused this. Add the time_clock.read scope.")

    async def scenario():
        async with p:
            await share(p)
            ana = await p.sign_in(p.ana)
            monkeypatch.setattr(ctdata, "timesheets", unavailable)
            status, body = await p.call("GET", "/api/reports/hours", ana)
            assert status == 503 and body["unavailable"] is True and "time_clock.read" in body["error"]
            monkeypatch.setattr(ctdata, "timesheets", None)   # a bug in the report
            status, body = await p.call("GET", "/api/reports/hours", ana)
            assert status == 500 and body["error"] == "This report couldn't be made. Try again later."
    go(scenario())


def test_a_slow_report_carries_on_and_is_picked_up_later(setup, monkeypatch):
    """The tunnel gives up on a request after 100 seconds: a long first look answers "pending"
    and carries on, once, until the staff app asks again."""
    p = setup
    monkeypatch.setattr(main, "REPORT_WAIT", 0.05)
    gate, runs = asyncio.Event(), []
    real = ctdata.timesheets

    async def slow(start, end):
        runs.append(start)
        await gate.wait()
        return await real(start, end)

    async def scenario():
        async with p:
            await share(p)
            ana = await p.sign_in(p.ana)
            monkeypatch.setattr(ctdata, "timesheets", slow)
            status, body = await p.call("GET", "/api/reports/hours?week=2026-10-19", ana)
            assert status == 202 and body == {"pending": True}
            status, body = await p.call("GET", "/api/reports/hours?week=2026-10-19", ana)
            assert status == 202 and len(runs) == 1   # the same look, not a second one
            gate.set()
            await asyncio.sleep(0.05)
            status, body = await p.call("GET", "/api/reports/hours?week=2026-10-19", ana)
            assert status == 200 and body["params"] == {"week": "2026-10-19"}
            assert main.report_jobs == {} and len(runs) == 1
    go(scenario())


def test_the_report_gets_the_person_and_departments(setup, monkeypatch):
    p = setup
    seen = {}

    async def data(ctx, params):
        seen.update(person=ctx.person, departments=ctx.departments, tz=str(ctx.tz),
                    today=ctx.today, manage=ctx.manage, ct=ctx.ct)
        return {}

    async def scenario():
        async with p:
            await share(p)
            monkeypatch.setattr(p.report, "data", data)
            p.db.update_settings({"wb_time_zone": "America/Denver"})
            bob = await p.sign_in(p.bob)
            assert (await p.call("GET", "/api/reports/hours", bob))[0] == 200
    go(scenario())
    assert seen["person"] == {"id": p.bob, "name": "Bob", "connecteam_user_id": None,
                              "departments": [{"id": p.kitchen, "name": "Kitchen"}]}
    assert [d["name"] for d in seen["departments"]] == ["Front", "Kitchen"]
    assert seen["tz"] == "America/Denver" and seen["manage"] is True and seen["ct"] is ctdata


# ---------------------------------------------------------------- uploads and settings

def test_only_managers_upload_and_files_are_checked(setup):
    p = setup

    async def scenario():
        async with p:
            await share(p)
            ana, bob = await p.sign_in(p.ana), await p.sign_in(p.bob)
            status, body = await upload(p, ana, "Ana,30\n")
            assert status == 403 and body["error"] == main.NO_REPORT
            assert ("Ana on Front Desk PC", "Blocked: report", False) in p.activity()
            status, body = await upload(p, bob, "Ana,30\n", name="../../x/targets.csv")
            assert status == 200, body
            assert "/" not in body["latest"]["name"] and body["latest"]["by"] == "Bob"
            assert body["latest"]["name"].endswith("targets.csv")
            assert os.listdir(os.path.join(p.data_dir, "reports", "hours", "uploads"))
            status, body = await upload(p, bob, "Ana,30\n", name="targets.xlsx")
            assert status == 400 and body["error"] == "Choose a .csv file."
            status, body = await upload(p, bob, b"PK\x03\x04\x00\x00binary")
            assert status == 400 and body["error"] == "That isn't a text file."
            status, body = await upload(p, bob, "x" * (reports_kit.UPLOAD_MAX + 1))
            assert status == 400 and "too big" in body["error"]
            status, body = await upload(p, bob, "")
            assert status == 400
            assert (await upload(p, bob, "Ana,1\n", key="nope"))[0] == 404
            # Everyone who opens it sees the newest.
            status, body = await p.call("GET", "/api/reports/hours?week=2026-10-19", ana)
            assert body["data"]["people"][0]["target"] == 30
            assert body["data"]["targets_file"].endswith("targets.csv")
            assert ("Bob on Front Desk PC", "Uploaded a file to Hours this week", True) in p.activity()
    go(scenario())


def test_the_newest_five_uploads_are_kept(setup):
    p = setup

    async def scenario():
        async with p:
            await share(p)
            bob = await p.sign_in(p.bob)
            for i in range(7):
                assert (await upload(p, bob, f"Ana,{i}\n", name=f"t{i}.csv"))[0] == 200
    go(scenario())
    files = reports_kit.uploads(p.data_dir, "hours", "targets")
    assert [f["name"] for f in files] == ["t6.csv", "t5.csv", "t4.csv", "t3.csv", "t2.csv"]
    assert files[0]["text"] == "Ana,6\n"
    assert len(os.listdir(os.path.join(p.data_dir, "reports", "hours", "uploads"))) == 10


def test_only_managers_change_settings(setup):
    p = setup

    async def scenario():
        async with p:
            await share(p)
            ana, bob = await p.sign_in(p.ana), await p.sign_in(p.bob)
            status, body = await p.call("PUT", "/api/reports/hours/settings", ana, {"title": "Mine"})
            assert status == 403
            status, body = await p.call("PUT", "/api/reports/hours/settings", bob,
                                        {"title": "Kitchen hours", "sneaky": 1})
            assert status == 200 and body["settings"] == {"title": "Kitchen hours"}
            assert p.db.report("hours")["settings"] == {"title": "Kitchen hours"}
            status, body = await p.call("GET", "/api/reports/hours", ana)
            assert body["data"]["title"] == "Kitchen hours"
            status, body = await p.call("PUT", "/api/reports/hours/settings", bob,
                                        {"title": "x" * (reports_kit.SETTINGS_MAX + 10)})
            assert status == 400 and body["error"] == "Those settings are too big."
            resp = await p.staff.put("/api/reports/hours/settings", headers=bob, json=["title"])
            assert resp.status == 400
    go(scenario())


# ---------------------------------------------------------------- view.js and view.css

def test_view_files_need_an_address_given_to_someone_allowed(setup):
    p = setup

    async def scenario():
        async with p:
            await share(p)
            ana = await p.sign_in(p.ana)
            listed = (await p.call("GET", "/api/reports", ana))[1]["reports"][0]
            resp = await p.staff.get(listed["view_js"])
            assert resp.status == 200 and resp.headers["Content-Type"].startswith("text/javascript")
            assert 'StaffReports.register("hours"' in await resp.text()
            assert "script-src 'self'" in resp.headers["Content-Security-Policy"]
            resp = await p.staff.get(listed["view_css"])
            assert resp.status == 200 and resp.headers["Content-Type"].startswith("text/css")
            for bad in ("/reports/hours/view.js", "/reports/hours/view.js?a=1.2.3.abc",
                        listed["view_js"][:-2] + "00", "/reports/hours/report.py?a=x",
                        listed["view_js"].replace("/hours/", "/template/")):
                assert (await p.staff.get(bad)).status == 404, bad
            # Access removed: the same address stops working.
            await share(p, view=[], manage=[])
            assert (await p.staff.get(listed["view_js"])).status == 404
    go(scenario())


def test_tickets_expire(setup):
    ticket = main.report_ticket("hours", 1, 2, now=0)
    assert not main.ticket_allows("hours", ticket)
    assert not main.ticket_allows("hours", "a.b.c.d")


# ---------------------------------------------------------------- the dashboard

def test_the_reports_tab(setup):
    p = setup

    async def scenario():
        async with p:
            status, body = await p.admin_call("GET", "/api/reports")
            assert status == 200
            listed = {r["key"]: r for r in body["reports"]}
            assert "template" not in listed
            assert listed["hours"]["enabled"] is False and listed["hours"]["view"] == []
            assert body["lane"]["daily_cap"] == 2000 and body["lane"]["minute_cap"] == 40
            await share(p, view=[p.front, p.kitchen], manage=[p.kitchen])
            listed = {r["key"]: r for r in (await p.admin_call("GET", "/api/reports"))[1]["reports"]}
            assert listed["hours"]["enabled"] and listed["hours"]["view"] == [p.front, p.kitchen]
            status, body = await p.admin_call("PUT", "/api/reports/hours", {"view": [999]})
            assert status == 400
            status, body = await p.admin_call("PUT", "/api/reports/hours", {"view": "all"})
            assert status == 400
            assert (await p.admin_call("PUT", "/api/reports/template", {"enabled": True}))[0] == 404
            assert (await p.admin_call("PUT", "/api/reports/nope", {"enabled": True}))[0] == 404
            # A deleted department drops out.
            p.db.delete_department(p.front)
            listed = {r["key"]: r for r in (await p.admin_call("GET", "/api/reports"))[1]["reports"]}
            assert listed["hours"]["view"] == [p.kitchen]
            resp = await p.admin.put("/api/reports/hours", json={"enabled": False})
            assert resp.status == 403   # not from the dashboard
    go(scenario())


# ---------------------------------------------------------------- backups and migration

def test_backups_carry_access_and_settings_but_not_uploads(setup):
    p = setup

    async def scenario():
        async with p:
            await share(p)
            bob = await p.sign_in(p.bob)
            await p.call("PUT", "/api/reports/hours/settings", bob, {"title": "Kitchen hours"})
            assert (await upload(p, bob, "Secret Person,40\n"))[0] == 200
            status, backup = await p.admin_call("GET", "/api/backup")
            assert status == 200
            assert "Secret Person" not in json.dumps(backup)
            assert backup["reports"] == [{"key": "hours", "enabled": True, "view": [p.front],
                                          "manage": [p.kitchen], "settings": {"title": "Kitchen hours"},
                                          "updated_at": backup["reports"][0]["updated_at"],
                                          "updated_by": "Bob on Front Desk PC"}]
            await share(p, enabled=False, view=[], manage=[])
            p.db.set_report("hours", "x", settings={})
            backup["reports"].append({"key": "../bad", "enabled": True})
            status, body = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, body
            r = p.db.report("hours")
            assert (r["enabled"], r["view"], r["manage"], r["settings"]) == (
                True, [p.front], [p.kitchen], {"title": "Kitchen hours"})
            assert set(p.db.reports()) == {"hours"}
            # A backup from before reports leaves them as they are.
            del backup["reports"]
            p.db.set_report("hours", "x", enabled=False)
            status, body = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200 and p.db.report("hours")["enabled"] is False
    go(scenario())


def test_migration_files_carry_uploads_too(setup, tmp_path, monkeypatch):
    p = setup

    async def scenario():
        async with p:
            await share(p)
            bob = await p.sign_in(p.bob)
            for i in range(2):
                assert (await upload(p, bob, f"Ana,{30 + i}\n", name=f"week{i}.csv"))[0] == 200
            status, made = await p.admin_call("POST", "/api/migrate/create")
            assert status == 200, made
        new_data = tmp_path / "new-data"
        new_data.mkdir()
        monkeypatch.setattr(main, "DATA_DIR", str(new_data))
        f = Fresh(tmp_path / "new")
        async with f:
            status, reply = await f.admin_call("POST", "/api/migrate/restore",
                                               {"file": made["file"], "code": made["code"]})
            assert status == 200, reply
            assert f.db.report("hours")["enabled"] is True
            assert f.db.settings()["reports_connecteam_usage"] == p.db.settings()["reports_connecteam_usage"]
    go(scenario())
    files = reports_kit.uploads(str(tmp_path / "new-data"), "hours", "targets")
    assert [(x["name"], x["text"], x["by"]) for x in files] == [
        ("week1.csv", "Ana,31\n", "Bob"), ("week0.csv", "Ana,30\n", "Bob")]
