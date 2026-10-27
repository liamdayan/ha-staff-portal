"""The Connecteam task sync (ctsync.py), forms that follow a manager field, and task photos.

Every rule has a test: quick tasks made and changed here go to their department's board, and
Connecteam's come here; the status mapping both ways; the bin and deletion both ways; the newer
change wins; nothing echoes back; a create that timed out is never made twice; the budget, and a
refusal or "too many requests" pausing only the sync (the clock checks carry on); nothing asked
while it's off; a restore leaves it off; and who may reach what. Then followed forms (both ways,
done ones hidden, how far back) and the photo proxy (who, which hosts, how big, what kind).

Connecteam is the fake in tests/hr_helpers.py, speaking the documented Quick Tasks API; photos
come from a small local image server standing in for Connecteam's.
"""

import io
import json
import time

import pytest
from aiohttp import web

from hr_helpers import FakeConnecteam, ct_form, ct_submission, ct_task, ct_user, fresh_connecteam
from test_tasks import Tasks, submission
from test_wristbands_server import free_port, run

import connecteam  # noqa: E402
import ctdata  # noqa: E402
import ctphotos  # noqa: E402
import ctsync  # noqa: E402
import main  # noqa: E402
import tasks  # noqa: E402

ALL_SCOPES = "users.read time_clock.read forms.read forms.write quick_tasks.read quick_tasks.write quick_tasks.delete"


def synced(tmp_path, on=True):
    """A portal with Connecteam on and the task sync on, Front Desk's board B1, Ana linked as
    Connecteam user 7 (Dee and Cy aren't linked)."""
    p = Tasks(tmp_path)
    p.db.update_settings({"connecteam_enabled": "1", "connecteam_client_id": "client",
                          "connecteam_client_secret": "secret"})
    p.db.update_staff(p.ana, connecteam_user_id="7")
    fresh_connecteam()
    ct = FakeConnecteam(users=[ct_user(7, "Ana"), ct_user(9, "Zed")])
    ct.scope = ALL_SCOPES
    ct.boards = [{"id": 501, "name": "Front desk board", "isArchived": False},
                 {"id": 502, "name": "Housekeeping board", "isArchived": False}]
    ct.board_tasks = {"501": []}
    ctsync.save(on, [{"board_id": "501", "name": "Front desk board", "department_id": p.desk}],
                main.task_departments())
    return p, ct


async def poll(p):
    """Read every board now."""
    await ctsync.settle()
    p.db.conn.execute("UPDATE task_ct_boards SET last_poll = NULL")
    await ctsync.poll_due(1)


async def sent(p):
    await ctsync.settle()
    await ctsync.push_pending()


def writes(ct, method=None):
    return [w for w in ct.writes if method is None or w[0] == method]


def history(p, task_id):
    return [(e["by_name"], e["action"], e["to_status"], e["note"]) for e in p.db.task_history(task_id)]


# ---------------------------------------------------------------- Staff Access -> Connecteam

def test_new_tasks_go_to_their_departments_board_and_changes_follow(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        async with p, ct:
            ana = await p.sign_in(p.ana)
            t = await p.add(ana, title="Fix gate", notes="Hinge\nloose", assigned_id=p.ana,
                            due_date="2026-11-02", due_time="09:30")
            await ctsync.settle()
            [made] = ct.board_tasks["501"]
            assert (made["title"], made["userIds"], made["status"]) == ("Fix gate", [7], "published")
            assert made["dueDate"] == ctsync.due_stamp("2026-11-02", "09:30", main.report_zone())
            assert writes(ct, "POST")[0][2]["description"] == {"content": "Hinge\nloose\n\nAdded by Ana in Staff Access"}
            row = p.db.task(t["id"])
            assert (row["ct_task_id"], row["ct_board_id"], row["ct_pending"]) == (made["id"], "501", None)
            # Title, notes and assignee changes go too. Dee isn't linked: an unassigned draft.
            await p.call("PUT", f"/api/tasks/{t['id']}", ana, {"title": "Fix the gate", "notes": "Now",
                                                              "assigned_id": p.dee})
            await ctsync.settle()
            method, path, body = writes(ct)[-1]
            assert (method, path) == ("PUT", f"/tasks/v1/taskboards/501/tasks/{made['id']}")
            assert (body["title"], body["userIds"], body["status"], body["description"]) == (
                "Fix the gate", [], "draft", {"content": "Now\n\nAdded by Ana in Staff Access"})
            assert (await p.listed(ana))["Fix the gate"]["connecteam"] is True
            # A date with no time is the end of that day there.
            await p.call("PUT", f"/api/tasks/{t['id']}", ana, {"due_date": "2026-11-05", "due_time": ""})
            await ctsync.settle()
            assert writes(ct)[-1][2]["dueDate"] == ctsync.due_stamp("2026-11-05", "23:59", main.report_zone())
            # Housekeeping has no board: its tasks stay here, and say so.
            cy = await p.sign_in(p.cy)
            mop = await p.add(cy, title="Mop the lobby")
            await ctsync.settle()
            assert p.db.task(mop["id"])["ct_note"] == ctsync.NO_BOARD and len(ct.board_tasks["501"]) == 1
            assert (await p.listed(cy))["Mop the lobby"]["ct_note"] == ctsync.NO_BOARD
            # Form tasks aren't Quick Tasks.
            p.db.set_task_form("55", {"name": "Maintenance", "department_id": p.desk, "title_question": "",
                                      "assign_submitter": 0}, 1_700_000_000)
            p.db.add_form_tasks("55", ["s1"], [tasks.task_from_submission(
                p.db.task_form("55"), submission("s1", 1_700_000_100), {}, 1_700_000_100)], time.time())
            await sent(p)
            assert len(ct.board_tasks["501"]) == 1 and len(writes(ct, "POST")) == 1
    run(scenario())


def test_status_mapping_both_ways(tmp_path):
    """To do and In progress are "published" (In progress kept here), Done is "completed", Won't
    do archives (an unassigned draft can't be archived, so it's deleted, and made again when
    it's reopened). From Connecteam: completed is Done, reopened is To do (In progress kept)."""
    async def scenario():
        p, ct = synced(tmp_path)
        async with p, ct:
            ana = await p.sign_in(p.ana)
            t = await p.add(ana, title="Count towels", assigned_id=p.ana)
            await ctsync.settle()
            [remote] = ct.board_tasks["501"]
            status = lambda s: p.call("POST", f"/api/tasks/{t['id']}/status", ana, {"status": s})
            await status("doing")
            await sent(p)
            assert not writes(ct, "PUT")   # In progress: still "published" there, nothing to send
            await status("done")
            await sent(p)
            assert remote["status"] == "completed"
            await status("wont")
            await sent(p)
            assert (remote["status"], remote["isArchived"]) == ("published", True)
            await status("todo")
            await sent(p)
            assert (remote["status"], remote["isArchived"]) == ("published", False)
            # From Connecteam: completed is Done; reopened, Done goes back to To do.
            remote["status"] = "completed"
            await poll(p)
            assert p.db.task(t["id"])["status"] == "done"
            remote["status"] = "published"
            await poll(p)
            assert p.db.task(t["id"])["status"] == "todo"
            # In progress here stays when Connecteam says it's open.
            await status("doing")
            await sent(p)
            await poll(p)
            assert p.db.task(t["id"])["status"] == "doing"
            assert ("Someone in Connecteam", "status", "done", "in Connecteam") in history(p, t["id"])
            # Unassigned: a draft, deleted for Won't do, made again when reopened.
            u = await p.add(ana, title="Sweep")
            await ctsync.settle()
            first = p.db.task(u["id"])["ct_task_id"]
            assert next(x for x in ct.board_tasks["501"] if x["id"] == first)["status"] == "draft"
            await p.call("POST", f"/api/tasks/{u['id']}/status", ana, {"status": "wont"})
            await sent(p)
            assert writes(ct)[-1][0] == "DELETE" and p.db.task(u["id"])["ct_task_id"] is None
            await p.call("POST", f"/api/tasks/{u['id']}/status", ana, {"status": "todo"})
            await sent(p)
            again = p.db.task(u["id"])["ct_task_id"]
            assert again and again != first and writes(ct)[-1][0] == "POST"
    run(scenario())


# ---------------------------------------------------------------- Connecteam -> Staff Access

def test_connecteam_tasks_come_here_and_their_changes_follow(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        p.db.update_staff(p.dee, connecteam_user_id="9")
        ct.board_tasks["501"] = [
            ct_task("c1", "Check the boiler", [7], notes="Room <b>4</b> &amp; 5"),
            ct_task("c2", "Done long ago", [7], status="completed"),
            ct_task("c3", "Archived", [7], archived=True)]
        async with p, ct:
            await poll(p)
            rows = {r["title"]: dict(r) for r in p.db.conn.execute("SELECT * FROM tasks")}
            # The first read takes only open tasks, not the board's whole past.
            assert set(rows) == {"Check the boiler"}
            boiler = rows["Check the boiler"]
            assert (boiler["source"], boiler["department_id"], boiler["assigned_id"]) == (
                "connecteam", p.desk, p.ana)
            assert boiler["notes"] == "Room 4 & 5"   # Connecteam's HTML, as text
            assert history(p, boiler["id"]) == [("Someone in Connecteam", "created", "todo", "in Connecteam")]
            ana = await p.sign_in(p.ana)
            seen = (await p.listed(ana))["Check the boiler"]
            assert seen["source"] == "connecteam" and seen["connecteam"] is True
            # New ones after that come, finished ones too; text is capped as typed text is.
            ct.board_tasks["501"].append(ct_task("c4", "x" * 500, [], status="completed"))
            # Changes: title, description, due date, assignees.
            remote = ct.board_tasks["501"][0]
            remote.update(title="Check the boiler again", userIds=[9], dueDate=1_800_000_000,
                          description=[{"type": "html", "html": "<p>Line 1</p><p>Line 2</p>"}])
            await poll(p)
            after = p.db.task(boiler["id"])
            assert (after["title"], after["notes"], after["assigned_id"]) == (
                "Check the boiler again", "Line 1\nLine 2", p.dee)
            assert (after["due_date"], after["due_time"]) == ctsync.due_parts(1_800_000_000, main.report_zone())
            long = p.db.conn.execute("SELECT * FROM tasks WHERE source = 'connecteam' AND status = 'done'").fetchone()
            assert len(long["title"]) == tasks.MAX_TITLE and long["assigned_id"] is None
            assert not writes(ct)   # nothing sent back
    run(scenario())


def test_the_bin_and_deletion_both_ways(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        async with p, ct:
            ana = await p.sign_in(p.ana)
            t = await p.add(ana, title="Polish brass", assigned_id=p.ana)
            await ctsync.settle()
            [remote] = ct.board_tasks["501"]
            # The bin here archives it there; putting it back unarchives it.
            await p.call("DELETE", f"/api/tasks/{t['id']}", ana)
            await ctsync.settle()
            assert remote["isArchived"] is True
            await p.call("POST", f"/api/tasks/{t['id']}/restore", ana)
            await ctsync.settle()
            assert remote["isArchived"] is False
            # Archived there: the bin here, saying so.
            remote["isArchived"] = True
            await poll(p)
            row = p.db.task(t["id"])
            assert row["deleted_at"] and row["deleted_by"] == "Someone in Connecteam"
            assert history(p, t["id"])[-1] == ("Someone in Connecteam", "deleted", None, "in Connecteam")
            # Unarchived there: back here.
            remote["isArchived"] = False
            await poll(p)
            assert p.db.task(t["id"])["deleted_at"] is None
            # Deleted there: the bin here; putting it back makes it there again.
            ct.board_tasks["501"].clear()
            await poll(p)
            row = p.db.task(t["id"])
            assert row["deleted_at"] and row["ct_task_id"] is None
            await p.call("POST", f"/api/tasks/{t['id']}/restore", ana)
            await ctsync.settle()
            [again] = ct.board_tasks["501"]
            assert again["title"] == "Polish brass" and p.db.task(t["id"])["ct_task_id"] == again["id"]
            # An old Connecteam task is never brought back as new after its task here is gone.
            p.db.conn.execute("DELETE FROM tasks")
            await poll(p)
            assert p.db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    run(scenario())


# ---------------------------------------------------------------- both sides at once

def test_the_newer_change_wins_and_the_history_says_where_from(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        async with p, ct:
            ana = await p.sign_in(p.ana)
            t = await p.add(ana, title="Paint fence", assigned_id=p.ana)
            await ctsync.settle()
            await poll(p)
            [remote] = ct.board_tasks["501"]
            # Both change the title. The change here waits (not sent yet) and is newer than the
            # last read of the board: it wins, and is sent.
            p.db.conn.execute("UPDATE task_ct_boards SET last_ok = ?", (time.time() - 100,))
            p.db.conn.execute("UPDATE tasks SET title = 'Paint fence white', ct_pending = ? WHERE id = ?",
                              (time.time(), t["id"]))
            p.db.conn.commit()
            remote["title"] = "Paint fence blue"
            await poll(p)
            assert p.db.task(t["id"])["title"] == "Paint fence white"
            assert history(p, t["id"])[-1] == ("Staff Access", "edited", None,
                                              "kept: newer than the change in Connecteam")
            await sent(p)
            assert remote["title"] == "Paint fence white"
            # Now the change here is older than the last read: Connecteam's wins.
            p.db.conn.execute("UPDATE task_ct_boards SET last_ok = ?", (time.time(),))
            p.db.conn.execute("UPDATE tasks SET title = 'Paint fence red', ct_pending = ? WHERE id = ?",
                              (time.time() - 500, t["id"]))
            p.db.conn.commit()
            remote["title"] = "Paint fence green"
            await poll(p)
            assert p.db.task(t["id"])["title"] == "Paint fence green"
            assert history(p, t["id"])[-1] == ("Someone in Connecteam", "edited", None,
                                              "in Connecteam, newer than the change here")
            await sent(p)
            assert remote["title"] == "Paint fence green" and p.db.task(t["id"])["ct_pending"] is None
    run(scenario())


def test_changes_from_connecteam_never_echo_back(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        ct.board_tasks["501"] = [ct_task("c1", "Restock bar", [9])]   # Zed: not linked here
        async with p, ct:
            await poll(p)
            remote = ct.board_tasks["501"][0]
            remote.update(title="Restock the bar", status="completed")
            await poll(p)
            row = p.db.task(p.db.conn.execute("SELECT id FROM tasks").fetchone()[0])
            assert (row["title"], row["status"], row["assigned_id"], row["ct_pending"]) == (
                "Restock the bar", "done", None, None)
            await sent(p)
            await poll(p)
            assert not writes(ct)
            # An edit here sends only what changed here: Zed stays on it there.
            p.db.set_role_manage_tasks(p.front_desk, True)
            ana = await p.sign_in(p.ana)
            status, body = await p.call("PUT", f"/api/tasks/{row['id']}", ana, {"notes": "Two crates"})
            assert status == 200, body
            await ctsync.settle()
            [(method, _, body)] = writes(ct)
            assert method == "PUT" and body["userIds"] == [9] and body["status"] == "completed"
    run(scenario())


def test_a_create_that_timed_out_is_never_made_twice(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        async with p, ct:
            ana = await p.sign_in(p.ana)
            ct.hang_up.add("POST")   # made, but the reply never comes
            t = await p.add(ana, title="Fix sign", assigned_id=p.ana)
            await ctsync.settle()
            assert len(ct.board_tasks["501"]) == 1 and p.db.task(t["id"])["ct_task_id"] is None
            assert '"creating"' in p.db.task(t["id"])["ct_sync"]
            ct.hang_up.clear()
            await sent(p)   # reads the board first, and finds it
            [made] = ct.board_tasks["501"]
            assert p.db.task(t["id"])["ct_task_id"] == made["id"] and len(writes(ct, "POST")) == 1
            await poll(p)
            assert p.db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1   # not imported too
            # Lost before it arrived: made once, the next time.
            ct.hang_up.add("POST!")
            u = await p.add(ana, title="Fix bell", assigned_id=p.ana)
            await ctsync.settle()
            ct.hang_up.clear()
            await sent(p)
            await sent(p)
            assert [x["title"] for x in ct.board_tasks["501"]] == ["Fix sign", "Fix bell"]
            assert p.db.task(u["id"])["ct_task_id"] == ct.board_tasks["501"][1]["id"]
    run(scenario())


# ---------------------------------------------------------------- the budget

def test_polls_share_the_form_checks_budget():
    weight = lambda forms, pages: forms + pages
    for forms, pages in ((0, 1), (2, 1), (3, 5), (50, 20)):
        every = tasks.form_interval(weight(forms, pages))
        assert every >= 600 and weight(forms, pages) * 86400 / every <= tasks.FORM_DAILY_REQUESTS
    assert ctsync.WRITE_DAILY_CAP + tasks.FORM_DAILY_REQUESTS <= connecteam.REPORTS_DAILY_CAP / 2


def test_a_refusal_or_too_many_pauses_only_the_sync(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        async with p, ct:
            status, body = await p.admin_call("GET", "/api/tasks/sync")
            assert body["checks_a_day"] == 300 and body["requests_a_day"] == 144
            ct.too_many.add("/tasks/v1/taskboards/501/tasks")
            await poll(p)
            status, body = await p.admin_call("GET", "/api/tasks/sync")
            assert "fewer requests" in body["sync"]["paused"]
            assert main.connecteam.reports_paused_until == 0 and main.connecteam.paused_until == 0
            # Paused: nothing is asked, and the clock checks carry on.
            asked = len(ct.calls)
            await poll(p)
            assert len(ct.calls) == asked
            assert await main.connecteam.fetch_clocked_in() == set()
            assert await ctdata.forms() == []   # reports aren't paused either
            # Refused: the reason names the permission.
            ct.too_many.clear()
            ct.scope = "users.read time_clock.read"
            ct.refuse.add("/tasks/v1/taskboards/501/tasks")
            ctsync.lift_pause()
            main.connecteam.forget()
            await poll(p)
            assert "quick_tasks.read" in ctsync.status()["paused"]
            assert main.connecteam.halted is None
            # Today's changes are capped: the rest wait.
            ctsync.lift_pause()
            ct.refuse.clear()
            ct.scope = ALL_SCOPES
            p.db.update_settings({"ct_tasks_usage": json.dumps({**ctsync.usage(), "writes": ctsync.WRITE_DAILY_CAP})})
            ana = await p.sign_in(p.ana)
            t = await p.add(ana, title="Later", assigned_id=p.ana)
            await ctsync.settle()
            assert not ct.board_tasks["501"] and p.db.task(t["id"])["ct_pending"]
    run(scenario())


def test_nothing_is_asked_while_the_sync_is_off(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path, on=False)
        async with p, ct:
            ana = await p.sign_in(p.ana)
            t = await p.add(ana, title="Quiet", assigned_id=p.ana)
            await p.call("POST", f"/api/tasks/{t['id']}/status", ana, {"status": "done"})
            await sent(p)
            await main.check_task_forms()
            await ctsync.poll_due(1)
            assert not [c for c in ct.calls if c.startswith("/tasks/")]
            assert p.db.task(t["id"])["ct_note"] is None   # not "no board": just not syncing
    run(scenario())


def test_a_restore_keeps_ids_and_leaves_the_sync_off(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        async with p, ct:
            ana = await p.sign_in(p.ana)
            t = await p.add(ana, title="Back up", assigned_id=p.ana)
            await ctsync.settle()
            [remote] = ct.board_tasks["501"]
            status, backup = await p.admin_call("GET", "/api/backup")
            assert backup["tasks"]["sync"]["boards"][0]["board_id"] == "501"
            assert backup["tasks"]["sync"]["enabled"] is True
            status, body = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, body
            row = p.db.task(t["id"])
            assert row["ct_task_id"] == remote["id"] and row["ct_sync"] is None
            s = p.db.settings()
            assert s["ct_tasks_on"] == "0" and "restore" in s["ct_tasks_note"]
            # Off: a change here asks nothing.
            remote["title"] = "Changed there"
            asked = len(ct.calls)
            await p.call("PUT", f"/api/tasks/{t['id']}", ana, {"title": "Changed here"})
            await sent(p)
            await ctsync.poll_due(1)
            assert len(ct.calls) == asked
            # Turned on again: Connecteam's copy is read first, and nothing is made twice.
            status, body = await p.admin_call("PUT", "/api/tasks/sync", {"enabled": True, "boards": [
                {"board_id": "501", "name": "Front desk board", "department_id": p.desk}]})
            assert status == 200 and body["sync"]["enabled"] and not body["sync"]["note"]
            await sent(p)
            await poll(p)
            assert len(ct.board_tasks["501"]) == 1 and not writes(ct, "POST")[1:]
            assert p.db.task(t["id"])["title"] == "Changed there"
    run(scenario())


# ---------------------------------------------------------------- the dashboard, and who may do what

def test_the_dashboard_sets_up_and_tests_the_sync(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path, on=False)
        async with p, ct:
            status, body = await p.admin_call("GET", "/api/tasks/sync/boards")
            assert body["boards"][0] == {"id": "501", "name": "Front desk board", "archived": False}
            status, body = await p.admin_call("POST", "/api/tasks/sync/test")
            assert body["ok"] and body["checks"][-1] == {"check": "Reading boards", "ok": True, "detail": "2 boards."}
            ct.scope = "users.read time_clock.read quick_tasks.read"
            main.connecteam.forget()
            status, body = await p.admin_call("POST", "/api/tasks/sync/test")
            assert not body["ok"] and "quick_tasks.write" in body["checks"][0]["detail"]
            for bad in ({"boards": [{"board_id": "../x"}]}, {"boards": [{"board_id": "1", "department_id": 999}]},
                        {"boards": "x"}):
                status, _ = await p.admin_call("PUT", "/api/tasks/sync", {"enabled": True, **bad})
                assert status == 400
            status, body = await p.admin_call("PUT", "/api/tasks/sync", {"enabled": True, "boards": [
                {"board_id": "502", "name": "<b>House</b>", "department_id": p.house}]})
            assert status == 200 and body["sync"]["boards"][0]["department"] == "Housekeeping"
            assert body["sync"]["boards"][0]["name"] == "<b>House</b>"   # text, shown as text
            # A department with a board can't be deleted from under it.
            status, body = await p.admin_call("DELETE", f"/api/departments/{p.house}")
            assert status == 409 and "board" in body["error"]
    run(scenario())


def test_only_the_dashboard_and_people_who_see_a_task_reach_these(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        async with p, ct:
            for method, path in (("PUT", "/api/tasks/sync"), ("POST", "/api/tasks/sync/test")):
                resp = await p.admin.request(method, path, json={})
                assert resp.status == 403   # without the dashboard's own header
            resp = await p.staff.get("/api/tasks/sync")
            assert resp.status == 404       # not on the staff app at all
            ana = await p.sign_in(p.ana)
            t = await p.add(ana, title="Photo task")
            assert (await p.staff.get(f"/api/tasks/{t['id']}/photos/0")).status in (401, 403)   # no PIN
            cy = await p.sign_in(p.cy)
            status, body = await p.call("GET", f"/api/tasks/{t['id']}/photos/0", cy)
            assert status == 404 and "isn't there" in body["error"]   # not theirs to see
    run(scenario())


# ---------------------------------------------------------------- forms that follow a manager field

FOLLOW = {"field": "mf1", "name": "Status", "unset": "todo", "options": [
    {"id": "o1", "name": "Working on it", "color": "#f5a623", "status": "doing"},
    {"id": "o2", "name": "Done", "color": "#7ed321", "status": "done"}]}


def status_field(option=None, at=None):
    return [{"managerFieldId": "mf1", "managerFieldType": "status", "lastUpdatedTimestamp": at,
             **({"status": option} if option else {})},
            {"managerFieldId": "mf2", "managerFieldType": "note", "note": "Ordered the part"}]


def maintenance_form():
    form = ct_form(55, "Maintenance Request", [("q1", "Problem", "openEnded"), ("q2", "Photo", "image")])
    form["settings"] = {"managerFields": [
        {"managerFieldId": "mf1", "name": "Status", "managerFieldType": "status", "statuses": [
            {"managerFieldStatusId": "o1", "name": "Working on it", "color": "#f5a623"},
            {"managerFieldStatusId": "o2", "name": "Done", "color": "#7ed321"}]},
        {"managerFieldId": "mf2", "name": "Note", "managerFieldType": "note"}]}
    return form


def test_a_followed_form_shows_open_submissions_and_follows_their_status(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path, on=False)   # following a form doesn't need the Quick Tasks sync
        p.db.update_settings({"wb_time_zone": "UTC"})
        ct.forms = [maintenance_form()]
        now = int(time.time())
        working = {"statusOptionId": "o1", "name": "Working on it", "color": "#f5a623"}
        done = {"statusOptionId": "o2", "name": "Done", "color": "#7ed321"}
        tap = lambda text: [{"questionId": "q1", "questionType": "openEnded", "value": text,
                             "wasSubmittedEmpty": False}]
        subs = [ct_submission("s1", 55, 7, now - 5 * 86400, tap("Leaky tap")),       # before it was picked
                ct_submission("s2", 55, 7, now - 4 * 86400, tap("Broken lamp")),
                ct_submission("s3", 55, 7, now - 3 * 86400, tap("Fixed already")),
                ct_submission("s4", 55, 7, now - 40 * 86400, tap("Too old"))]
        subs[1]["managerFields"] = status_field(working, now - 3600)
        subs[2]["managerFields"] = status_field(done, now - 3600)
        subs[0]["managerFields"] = status_field()
        ct.submissions = {55: subs}
        async with p, ct:
            status, body = await p.admin_call("GET", "/api/tasks/forms/55/fields")
            assert body["fields"][0]["statuses"][0] == {"id": "o1", "name": "Working on it", "color": "#f5a623"}
            assert body["notes"] == ["Note"]
            status, body = await p.admin_call("PUT", "/api/tasks/forms/55", {
                "name": "Maintenance Request", "department_id": p.house, "title_question": "Problem",
                "follow": FOLLOW})
            assert status == 200, body
            assert await main.check_task_forms() == 2
            rows = {r["title"]: dict(r) for r in p.db.conn.execute("SELECT * FROM tasks")}
            # Done ones and ones older than the look-back aren't tasks; older-than-picked open ones are.
            assert set(rows) == {"Leaky tap", "Broken lamp"}
            assert (rows["Leaky tap"]["status"], rows["Broken lamp"]["status"]) == ("todo", "doing")
            assert rows["Leaky tap"]["assigned_id"] is None and rows["Leaky tap"]["department_id"] == p.house
            assert rows["Leaky tap"]["manager_note"] == "Ordered the part"
            # Connecteam -> here: Done there is Done here.
            subs[0]["managerFields"] = status_field(done, now)
            ctdata.forget()
            p.db.conn.execute("UPDATE task_forms SET last_check = NULL")
            await main.check_task_forms()
            leaky = p.db.task(rows["Leaky tap"]["id"])
            assert leaky["status"] == "done" and leaky["ct_status"] == "o2"
            assert history(p, leaky["id"])[-1] == ("Someone in Connecteam", "status", "done", "in Connecteam")
            assert not writes(ct)   # not echoed back
            # Here -> Connecteam: reopening as In progress writes "Working on it".
            cy = await p.sign_in(p.cy)
            await p.call("POST", f"/api/tasks/{leaky['id']}/status", cy, {"status": "doing"})
            await ctsync.settle()
            [(method, path, body)] = writes(ct)
            assert (method, path) == ("PUT", "/forms/v1/forms/55/form-submissions/s1")
            # Who did it goes in the form's note, under what the manager wrote.
            assert body == {"managerFields": [
                {"managerFieldId": "mf1", "managerFieldType": "status", "status": working},
                {"managerFieldId": "mf2", "managerFieldType": "note",
                 "note": "Ordered the part\nMarked Working on it by Cy in Staff Access"}]}
            assert p.db.task(leaky["id"])["ct_status"] == "o1"
            # To do is "no status": Connecteam can't clear one, so it isn't written, and the task says so.
            await p.call("POST", f"/api/tasks/{leaky['id']}/status", cy, {"status": "todo"})
            await ctsync.settle()
            assert len(writes(ct)) == 1 and "can't clear" in p.db.task(leaky["id"])["ct_note"]
            # Done here writes Done there.
            await p.call("POST", f"/api/tasks/{leaky['id']}/status", cy, {"status": "done"})
            await ctsync.settle()
            assert writes(ct)[-1][2]["managerFields"][0]["status"] == done
    run(scenario())


def test_the_follow_setting_is_checked():
    assert tasks.clean_follow(None) is None and tasks.clean_follow({}) is None
    kept = json.loads(tasks.clean_follow(FOLLOW))
    assert kept["options"][1]["status"] == "done" and kept["unset"] == "todo"
    assert tasks.follow_status(kept, "") == "todo" and tasks.follow_status(kept, "o1") == "doing"
    assert tasks.follow_status(kept, "new-option") == "todo"
    assert tasks.follow_option(kept, "done")["id"] == "o2" and tasks.follow_option(kept, "wont") is None
    for bad in ({"field": "../x"}, {"field": "mf1", "options": [{"id": "o1", "status": "maybe"}]}, "nope"):
        with pytest.raises(tasks.TaskError):
            tasks.clean_follow(bad)


# ---------------------------------------------------------------- photos

def jpeg(size=(40, 30)) -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(out, "PNG")
    return out.getvalue()


class PhotoHost:
    """Connecteam's photo host, standing in: /ok.png, /big.png (over the cap), /page.html,
    /evil.svg, /away (a redirect to another host); counts what's asked."""

    def __init__(self):
        self.port, self.asked = free_port(), []
        app = web.Application()
        app.router.add_get("/{name}", self.handle)
        self.runner = web.AppRunner(app)

    async def handle(self, request):
        name = request.match_info["name"]
        self.asked.append(name)
        if name == "ok.png":
            return web.Response(body=jpeg(), content_type="image/png")
        if name == "big.png":
            return web.Response(body=b"x" * (ctphotos.MAX_BYTES + 10), content_type="image/png")
        if name == "page.html":
            return web.Response(text="<script>alert(1)</script>", content_type="text/html")
        if name == "evil.svg":
            return web.Response(text="<svg onload=alert(1)/>", content_type="image/svg+xml")
        if name == "away":
            raise web.HTTPFound("http://example.org/ok.png")
        if name == "notimage.png":
            return web.Response(body=b"not an image", content_type="image/png")
        return web.Response(status=403)   # an expired link

    async def __aenter__(self):
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        return self

    async def __aexit__(self, *exc):
        await self.runner.cleanup()


def test_photos_come_through_the_staff_server_only_for_who_may_see_them(tmp_path, monkeypatch):
    monkeypatch.setattr(ctphotos, "HOSTS", ("127.0.0.1",))
    monkeypatch.setattr(ctphotos, "SCHEMES", ("http",))

    async def scenario():
        p = Tasks(tmp_path)
        async with p, PhotoHost() as host:
            monkeypatch.setattr(ctphotos, "PORTS", (host.port,))
            ana = await p.sign_in(p.ana)
            t = await p.add(ana, title="With photos")
            base = f"http://127.0.0.1:{host.port}"
            names = ("ok.png", "big.png", "page.html", "evil.svg", "away", "gone.png", "notimage.png")
            photos = [{"url": f"{base}/{n}", "name": n} for n in names]
            photos.append({"url": "https://example.org/ok.png", "name": "elsewhere"})
            p.db.conn.execute("UPDATE tasks SET photos = ? WHERE id = ?", (json.dumps(photos), t["id"]))
            p.db.conn.commit()
            assert (await p.listed(ana))["With photos"]["photos"][0] == "ok.png"
            resp = await p.staff.get(f"/api/tasks/{t['id']}/photos/0", headers=ana)
            assert resp.status == 200 and resp.content_type == "image/jpeg"
            assert (await resp.read())[:2] == b"\xff\xd8"   # made again as a plain JPEG
            # Kept: the second look doesn't fetch it again.
            await p.staff.get(f"/api/tasks/{t['id']}/photos/0", headers=ana)
            assert host.asked.count("ok.png") == 1
            # Too big, not an image, SVG, off Connecteam's hosts, expired, or unreadable: unavailable.
            for n in range(1, len(photos) + 1):
                status, body = await p.call("GET", f"/api/tasks/{t['id']}/photos/{n}", ana)
                assert status == 404 and body["error"] == "Photo unavailable.", n
            assert "example.org" not in str(host.asked)
            # Someone who can't see the task can't see its photos.
            cy = await p.sign_in(p.cy)
            status, _ = await p.call("GET", f"/api/tasks/{t['id']}/photos/0", cy)
            assert status == 404
    run(scenario())


def test_only_connecteams_own_hosts_over_https():
    ok = ctphotos.host_ok
    assert ok("https://cdn.connecteam.com/a.jpg") and ok("https://connecteam.com/a.jpg")
    for bad in ("http://cdn.connecteam.com/a.jpg", "https://connecteam.com.evil.io/a.jpg",
                "https://evilconnecteam.com/a.jpg", "https://user:pw@cdn.connecteam.com/a",
                "https://127.0.0.1/a.jpg", "javascript:alert(1)", "https://s3.amazonaws.com/x"):
        assert not ok(bad), bad
    # Submissions keep https links only; a task keeps at most MAX_PHOTOS.
    many = [{"url": f"https://cdn.connecteam.com/{i}.jpg"} for i in range(40)] + [{"url": "http://x/1.jpg"}]
    assert len(json.loads(tasks.clean_photos(many))) == tasks.MAX_PHOTOS
    assert tasks.clean_photos([{"url": "http://x/1.jpg"}]) is None


# ---------------------------------------------------------------- review fixes

def test_photo_links_that_parsers_read_differently_are_refused():
    ok = ctphotos.host_ok
    for bad in ("https://@cdn.connecteam.com/a", "https://:@cdn.connecteam.com/a",
                "https://evil.com\\@cdn.connecteam.com/a", "https://cdn.connecteam.com\\evil.com/a",
                "https://cdn.connecteam.com:8443/a", "https://cdn.connecteam.com\t.evil.com/a",
                "https://evil.com%40cdn.connecteam.com/a", "https://evıl.connecteam.com/a",
                "https://cdn.connecteam.com%2f@evil.com/a", "https://evil.com#@cdn.connecteam.com/a",
                "https://[::1]/a", "https://169.254.169.254/a", "https:// cdn.connecteam.com/a", None):
        assert not ok(bad), bad
    assert ok("https://cdn.connecteam.com:443/a.jpg?sig=x") and ok("https://CDN.connecteam.com./a")
    # Only public addresses are connected to.
    for private in ("127.0.0.1", "10.1.2.3", "192.168.1.5", "169.254.169.254", "100.64.0.1", "::1",
                    "fe80::1%eth0", "::ffff:10.0.0.1", "fc00::1", "0.0.0.0", "224.0.0.1", "nonsense"):
        assert not ctphotos.public_ip(private), private
    assert ctphotos.public_ip("8.8.8.8") and ctphotos.public_ip("2606:4700::1")


def test_a_connecteam_name_that_points_inside_the_network_is_never_fetched(monkeypatch):
    import aiohttp
    asked = []

    async def resolve(self, host, port=0, family=0):
        asked.append(host)
        return [{"hostname": host, "host": "127.0.0.1", "port": port, "family": 2, "proto": 0, "flags": 0},
                {"hostname": host, "host": "::ffff:192.168.0.9", "port": port, "family": 10, "proto": 0,
                 "flags": 0}]
    monkeypatch.setattr(aiohttp.ThreadedResolver, "resolve", resolve)

    async def scenario():
        with pytest.raises(OSError):
            await ctphotos.PublicResolver().resolve("cdn.connecteam.com", 443)
        with pytest.raises(ctphotos.Unavailable):
            await ctphotos._fetch("https://cdn.connecteam.com/a.jpg")
        # A mixed answer keeps only the public address.
        monkeypatch.setattr(aiohttp.ThreadedResolver, "resolve", lambda *a, **k: _done(
            [{"hostname": "h", "host": "10.0.0.1", "port": 443, "family": 2, "proto": 0, "flags": 0},
             {"hostname": "h", "host": "8.8.8.8", "port": 443, "family": 2, "proto": 0, "flags": 0}]))
        assert [r["host"] for r in await ctphotos.PublicResolver().resolve("h", 443)] == ["8.8.8.8"]
    run(scenario())
    assert asked == ["cdn.connecteam.com", "cdn.connecteam.com"]   # the fetch went through it


async def _done(value):
    return value


def test_a_photo_that_failed_isnt_fetched_again_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ctphotos, "HOSTS", ("127.0.0.1",))
    monkeypatch.setattr(ctphotos, "SCHEMES", ("http",))
    ctphotos._failed.clear()

    async def scenario():
        async with PhotoHost() as host:
            monkeypatch.setattr(ctphotos, "PORTS", (host.port,))
            task = {"id": 1, "photos": json.dumps([{"url": f"http://127.0.0.1:{host.port}/gone.png"}])}
            for _ in range(3):
                with pytest.raises(ctphotos.Unavailable):
                    await ctphotos.photo(task, 0, str(tmp_path))
            assert host.asked == ["gone.png"]
            ctphotos._failed.clear()
            with pytest.raises(ctphotos.Unavailable):
                await ctphotos.photo(task, 0, str(tmp_path))
            assert host.asked == ["gone.png", "gone.png"]
    run(scenario())


def test_a_bad_partial_or_missing_board_read_never_bins_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(ctsync, "MAX_PAGES", 4)

    def room():   # a fresh minute in the reports lane
        main.connecteam._reports_recent.clear()
        main.connecteam._recent.clear()

    async def scenario():
        p, ct = synced(tmp_path)
        async with p, ct:
            ana = await p.sign_in(p.ana)
            t = await p.add(ana, title="Keep me", assigned_id=p.ana)
            await ctsync.settle()
            [remote] = ct.board_tasks["501"]
            real = ct.quick_tasks

            async def odd(request, path):
                if request.method == "GET" and path.endswith("/tasks"):
                    return web.json_response(reply)
                return await real(request, path)
            ct.quick_tasks = odd
            # A reply without a list of tasks: an error, not an empty board.
            for reply in ({"data": {}}, {"data": {"tasks": None}}, {"data": []}):
                await poll(p)
                assert p.db.task(t["id"])["deleted_at"] is None, reply
            # Paging that doesn't move on: not the whole board, so nothing's taken as gone.
            others = [ct_task(f"o{i}", f"Other {i}", [9]) for i in range(ctsync.PAGE)]
            reply = {"data": {"tasks": others}, "paging": {"offset": 0}}
            await poll(p)
            row = p.db.task(t["id"])
            assert row["deleted_at"] is None and row["ct_task_id"] == remote["id"]
            # The paging cap reached: the same.
            reply = {"data": {"tasks": others}, "paging": {"offset": 1}}
            calls = iter(range(1, 1000))

            async def endless(request, path):
                if request.method == "GET" and path.endswith("/tasks"):
                    return web.json_response({"data": {"tasks": others}, "paging": {"offset": next(calls) * 100}})
                return await real(request, path)
            ct.quick_tasks = endless
            room()
            await poll(p)
            assert p.db.task(t["id"])["deleted_at"] is None
            assert ctsync.boards()[0]["pages"] == ctsync.MAX_PAGES
            # A deleted (or archived) board that reads as empty: nothing binned, the Tasks tab says so.
            room()
            ct.quick_tasks = real
            ct.board_tasks["501"].clear()
            ct.boards = [b for b in ct.boards if b["id"] != 501]
            await poll(p)
            row = p.db.task(t["id"])
            assert row["deleted_at"] is None and row["ct_task_id"] == remote["id"]
            assert ctsync.boards()[0]["last_error"] == "That board isn't in Connecteam any more."
            ct.boards.append({"id": 501, "name": "Front desk board", "isArchived": True})
            await poll(p)
            assert p.db.task(t["id"])["deleted_at"] is None
    run(scenario())


def test_a_create_that_timed_out_isnt_made_again_after_a_partial_read(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        async with p, ct:
            ana = await p.sign_in(p.ana)
            ct.hang_up.add("POST")
            t = await p.add(ana, title="Only once", assigned_id=p.ana)
            await ctsync.settle()
            ct.hang_up.clear()
            real = ct.quick_tasks
            others = [ct_task(f"o{i}", f"Other {i}", [9]) for i in range(ctsync.PAGE)]

            async def stuck(request, path):   # the made task is beyond what's read
                if request.method == "GET" and path.endswith("/tasks"):
                    return web.json_response({"data": {"tasks": others}, "paging": {"offset": 0}})
                return await real(request, path)
            ct.quick_tasks = stuck
            await sent(p)
            await sent(p)
            assert len(writes(ct, "POST")) == 1 and '"creating"' in p.db.task(t["id"])["ct_sync"]
            ct.quick_tasks = real
            await sent(p)
            assert len(writes(ct, "POST")) == 1
            assert p.db.task(t["id"])["ct_task_id"] == ct.board_tasks["501"][0]["id"]
    run(scenario())


def test_a_hidden_department_keeps_its_board_and_forms_and_the_rest_still_saves(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        async with p, ct:
            p.db.set_task_form("55", {"name": "Maintenance", "department_id": p.desk, "title_question": "",
                                      "assign_submitter": 0}, time.time())
            p.db.update_department(p.desk, hidden=1)
            board = {"board_id": "501", "name": "Front desk board", "department_id": p.desk}
            status, body = await p.admin_call("PUT", "/api/tasks/sync", {"enabled": False, "boards": [board]})
            assert status == 200, body
            assert ctsync.boards()[0]["department_id"] == p.desk and not ctsync.on()
            # A hidden department can't be newly picked.
            status, _ = await p.admin_call("PUT", "/api/tasks/sync", {"enabled": False, "boards": [
                board, {"board_id": "502", "name": "House", "department_id": p.desk}]})
            assert status == 400
            status, body = await p.admin_call("PUT", "/api/tasks/forms/55", {
                "department_id": p.desk, "title_question": "Problem"})
            assert status == 200, body
            assert p.db.task_form("55")["title_question"] == "Problem"
            status, _ = await p.admin_call("PUT", "/api/tasks/forms/56", {"department_id": p.desk})
            assert status == 400
    run(scenario())


def test_someone_who_hasnt_signed_in_yet_can_be_given_a_connecteam_task(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        p.db.update_staff(p.dee, connecteam_user_id="9", status="pending")
        ct.board_tasks["501"] = [ct_task("c1", "Learn the till", [9])]
        async with p, ct:
            await poll(p)
            [row] = p.db.conn.execute("SELECT * FROM tasks").fetchall()
            assert row["assigned_id"] == p.dee
            await sent(p)
            assert not writes(ct)   # nothing to send back
    run(scenario())


def test_nothing_is_written_while_connecteam_itself_is_off(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path, on=False)
        p.db.update_settings({"wb_time_zone": "UTC"})
        ct.forms = [maintenance_form()]
        sub = ct_submission("s1", 55, 7, int(time.time()) - 86400, [
            {"questionId": "q1", "questionType": "openEnded", "value": "Leaky tap", "wasSubmittedEmpty": False}])
        sub["managerFields"] = status_field()
        ct.submissions = {55: [sub]}
        async with p, ct:
            status, body = await p.admin_call("PUT", "/api/tasks/forms/55", {
                "name": "Maintenance Request", "department_id": p.house, "title_question": "Problem",
                "follow": FOLLOW})
            assert status == 200, body
            assert await main.check_task_forms() == 1
            [t] = p.db.conn.execute("SELECT * FROM tasks").fetchall()
            p.db.update_settings({"connecteam_enabled": "0"})
            cy = await p.sign_in(p.cy)
            await p.call("POST", f"/api/tasks/{t['id']}/status", cy, {"status": "done"})
            await sent(p)
            assert not writes(ct) and p.db.task(t["id"])["ct_pending"]   # waits
            p.db.update_settings({"connecteam_enabled": "1"})
            await sent(p)
            assert writes(ct)[-1][2]["managerFields"][0]["status"]["statusOptionId"] == "o2"
    run(scenario())


def test_a_followed_forms_status_written_here_isnt_undone_by_an_older_read(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path, on=False)
        p.db.update_settings({"wb_time_zone": "UTC"})
        ct.forms = [maintenance_form()]
        working = {"statusOptionId": "o1", "name": "Working on it", "color": "#f5a623"}
        sub = ct_submission("s1", 55, 7, int(time.time()) - 86400, [
            {"questionId": "q1", "questionType": "openEnded", "value": "Lamp", "wasSubmittedEmpty": False}])
        sub["managerFields"] = status_field(working, int(time.time()) - 3600)
        ct.submissions = {55: [sub]}
        async with p, ct:
            status, body = await p.admin_call("PUT", "/api/tasks/forms/55", {
                "name": "Maintenance Request", "department_id": p.house, "title_question": "Problem",
                "follow": FOLLOW})
            assert status == 200, body
            assert await main.check_task_forms() == 1
            [t] = p.db.conn.execute("SELECT * FROM tasks").fetchall()
            assert t["status"] == "doing"
            cy = await p.sign_in(p.cy)
            await p.call("POST", f"/api/tasks/{t['id']}/status", cy, {"status": "done"})
            await ctsync.settle()
            assert writes(ct)[-1][2]["managerFields"][0]["status"]["statusOptionId"] == "o2"
            # The next check reads afresh (no copy kept from before the write): still Done.
            p.db.conn.execute("UPDATE task_forms SET last_check = NULL")
            asked = ct.calls.count("/forms/v1/forms/55/form-submissions")
            await main.check_task_forms()
            assert ct.calls.count("/forms/v1/forms/55/form-submissions") > asked
            row = p.db.task(t["id"])
            assert (row["status"], row["ct_status"]) == ("done", "o2")
            # A read that began before the write (here: the old reply) changes nothing.
            old = [{"id": "s1", "manager": {"status": {"mf1": {"id": "o1", "name": "Working on it",
                                                               "at": time.time() - 3600}}, "notes": {}},
                    "user_id": "7", "name": "Ana", "submitted_at": None, "answers": [], "photos": []}]
            form = p.db.task_form("55")
            await ctsync.follow_submissions(form, old, {}, main.report_zone(), time.time(), time.time() - 30)
            assert p.db.task(t["id"])["status"] == "done"
            # A read after it does: Working on it again there is In progress here.
            await ctsync.follow_submissions(form, old, {}, main.report_zone(), time.time(), time.time())
            assert p.db.task(t["id"])["status"] == "doing"
    run(scenario())


def test_without_delete_permission_an_unassigned_task_stays_on_the_board(tmp_path):
    """quick_tasks.delete is optional: without it, Won't do on a task nobody is assigned to (a
    draft, which Connecteam can't close) leaves the draft there, says so, and isn't tried again;
    Test passes and says what it means."""
    async def scenario():
        p, ct = synced(tmp_path)
        ct.scope = ALL_SCOPES.replace(" quick_tasks.delete", "")
        async with p, ct:
            ana = await p.sign_in(p.ana)
            u = await p.add(ana, title="Sweep")
            await ctsync.settle()
            first = p.db.task(u["id"])["ct_task_id"]
            await p.call("POST", f"/api/tasks/{u['id']}/status", ana, {"status": "wont"})
            await sent(p)
            assert not writes(ct, "DELETE") and p.db.task(u["id"])["ct_task_id"] == first
            assert "quick_tasks.delete" in p.db.task(u["id"])["ct_note"]
            await sent(p)
            await poll(p)
            assert not writes(ct, "DELETE") and p.db.task(u["id"])["status"] == "wont"
            # An assigned task is archived as before: that needs no delete.
            t = await p.add(ana, title="Count towels", assigned_id=p.ana)
            await ctsync.settle()
            await p.call("POST", f"/api/tasks/{t['id']}/status", ana, {"status": "wont"})
            await sent(p)
            remote = next(x for x in ct.board_tasks["501"] if x["id"] == p.db.task(t["id"])["ct_task_id"])
            assert remote["isArchived"] is True
            status, body = await p.admin_call("POST", "/api/tasks/sync/test")
            assert body["ok"] and any(c["check"] == "Unassigned tasks" for c in body["checks"])
    run(scenario())


def test_connecteams_reasons_are_shown_and_refused_changes_are_tried_again(tmp_path):
    """A 400 or 422 says what Connecteam said (nested, or its validation errors with where), and
    a change it refused goes again once after the add-on starts."""
    assert connecteam._words({"error": {"message": "Due date must be after start time"}}) == \
        "Due date must be after start time"
    assert connecteam._words({"detail": [{"loc": ["body", "dueDate"], "msg": "bad"}]}) == "body.dueDate: bad"
    assert connecteam._compact({"code": 17}) == '{"code":17}'
    said = {"details": {"error_message": "Request body is invalid", "error_code": 1002},
            "error": {"body.startTime": {"message": "Value error, Task start date is in the past"}}}
    assert connecteam._words(said) == "body.startTime: Value error, Task start date is in the past"
    # Dates already past aren't sent: Connecteam refuses them, even ones it had.
    old, soon = int(time.time()) - 3600, int(time.time()) + 3600
    view = {"title": "x", "notes": "", "users": [7], "done": True, "archived": False}
    assert "dueDate" not in ctsync._payload({**view, "due": old})
    assert ctsync._payload({**view, "due": soon})["dueDate"] == soon
    # A due date goes with a start time (Connecteam compares them): a minute from now at most.
    body = ctsync._with_start(ctsync._payload({**view, "due": soon}))
    assert time.time() <= body["startTime"] <= min(time.time() + 61, soon)
    assert "startTime" not in ctsync._with_start(ctsync._payload({**view, "due": None}))
    assert ctsync._with_start({"dueDate": soon, "startTime": soon - 5})["startTime"] == soon - 5

    async def scenario():
        p, ct = synced(tmp_path)
        async with p, ct:
            ana = await p.sign_in(p.ana)
            t = await p.add(ana, title="Count towels", assigned_id=p.ana)
            await ctsync.settle()
            p.db.conn.execute("UPDATE tasks SET ct_note = ?, ct_pending = NULL WHERE id = ?",
                              ("Connecteam didn't take the last change: x", t["id"]))
            assert ctsync.retry_refused() == 1 and p.db.task(t["id"])["ct_pending"]
            assert ctsync.retry_refused() == 0
    run(scenario())


def test_who_made_it_goes_in_the_description_and_isnt_read_back():
    """Connecteam's API can't set who created a task, so the description's last line says."""
    view = {"title": "x", "notes": "Fix the tap", "users": [7], "done": False, "archived": False, "due": None}
    body = ctsync._payload(view, "Ana Ray")
    assert body["description"]["content"] == "Fix the tap\n\nAdded by Ana Ray in Staff Access"
    assert ctsync._payload({**view, "notes": ""}, "Ana Ray")["description"]["content"] == "Added by Ana Ray in Staff Access"
    read = [{"type": "html", "html": "<p>Fix the tap</p><p>Added by Ana Ray in Staff Access</p>"}]
    assert ctsync.html_text(read) == "Fix the tap"
    assert ctsync._by({"created_by_name": "Ana Ray", "source": "quick"}) == "Ana Ray"
    assert ctsync._by({"created_by_name": "Connecteam form", "source": "form"}) == ""


def test_a_followed_form_matches_status_by_name_too():
    follow = {"unset": "todo", "options": [{"id": "o1", "name": "Working on it", "status": "doing"},
                                           {"id": "o2", "name": "Done", "status": "done"}]}
    assert tasks.follow_status(follow, "o2") == "done"
    assert tasks.follow_status(follow, "other-id", "Done") == "done"   # IDs that don't match
    assert tasks.follow_status(follow, "", "") == "todo"
    assert tasks.follow_status(follow, "x", "Unknown") == "todo"


def test_who_changed_a_followed_forms_status_goes_in_its_note():
    """Connecteam credits every API change to the keys' owner, so the note says who did it:
    one line, replaced on the next change, under whatever a manager wrote."""
    one = ctsync.marked_note("Ordered in a box", "Done", "Isaiah Kast")
    assert one == "Ordered in a box\nMarked Done by Isaiah Kast in Staff Access"
    again = ctsync.marked_note(one, "Working on it", "Ana Ray")
    assert again == "Ordered in a box\nMarked Working on it by Ana Ray in Staff Access"
    assert ctsync.marked_note(None, "Done", None) == "Marked Done by someone in Staff Access"


def test_the_staff_app_lists_finished_tasks_for_two_days():
    assert tasks.LIST_FINISHED_DAYS == 2


def test_a_recurring_connecteam_task_shows_each_repeat_on_its_day(tmp_path):
    """Connecteam lists each repeat of a recurring task as a task of its own, days ahead: each
    shows from its day, its status syncs both ways, and it's edited or deleted only in
    Connecteam (that could touch the series)."""
    async def scenario():
        p, ct = synced(tmp_path)
        day = 86400
        now = int(time.time())
        today = {**ct_task("r1", "Check the fire doors", [7], due=now + 3600), "type": "recurring"}
        later = {**ct_task("r2", "Check the fire doors", [7], due=now + 3 * day), "type": "recurring"}
        ct.board_tasks["501"] = [today, later]
        async with p, ct:
            await poll(p)
            ana = await p.sign_in(p.ana)
            status, body = await p.call("GET", "/api/tasks", ana)
            shown = [t for t in body["tasks"] if t["title"] == "Check the fire doors"]
            assert len(shown) == 1 and shown[0]["ct_recurring"] is True and shown[0]["can_edit"] is False
            tid = shown[0]["id"]
            for method, path, sent_body in (("PUT", f"/api/tasks/{tid}", {"title": "x"}),
                                            ("DELETE", f"/api/tasks/{tid}", None)):
                status, answer = await p.call(method, path, ana, sent_body)
                assert status == 409 and "repeats in Connecteam" in answer["error"], (method, path)
            # Done here completes that repeat only, keeping its type.
            status, _ = await p.call("POST", f"/api/tasks/{tid}/status", ana, {"status": "done"})
            await sent(p)
            assert status == 200 and today["status"] == "completed" and later["status"] == "published"
            assert writes(ct, "PUT")[-1][2]["type"] == "recurring"
    run(scenario())


def test_a_weekly_repeat_shows_the_day_after_the_last_one_was_due(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        now, day = int(time.time()), 86400
        ct.board_tasks["501"] = [
            {**ct_task("w1", "Clean the vents", [7], due=now - day), "type": "recurring"},
            {**ct_task("w2", "Clean the vents", [7], due=now + 6 * day), "type": "recurring"},
            {**ct_task("w3", "Clean the vents", [7], due=now + 13 * day), "type": "recurring"}]
        async with p, ct:
            await poll(p)
            ana = await p.sign_in(p.ana)
            status, body = await p.call("GET", "/api/tasks", ana)
            dues = sorted(t["due_date"] for t in body["tasks"] if t["title"] == "Clean the vents")
            # Last week's was due yesterday, so next week's shows now; the one after waits.
            assert len(dues) == 2
    run(scenario())


def test_recurring_tasks_brought_in_before_it_was_kept_get_it_on_the_next_read(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        now = int(time.time())
        today = {**ct_task("r1", "Wipe the counters", [7], due=now + 3600), "type": "recurring"}
        later = {**ct_task("r2", "Wipe the counters", [7], due=now + 2 * 86400), "type": "recurring"}
        ct.board_tasks["501"] = [today, later]
        async with p, ct:
            await poll(p)
            for row in p.db.conn.execute("SELECT id, ct_sync FROM tasks").fetchall():   # as 0.12.8 kept them
                sync = json.loads(row["ct_sync"])
                sync["seen"].pop("recurring", None)
                p.db.conn.execute("UPDATE tasks SET ct_sync = ? WHERE id = ?", (json.dumps(sync), row["id"]))
            p.db.conn.commit()
            await poll(p)
            ana = await p.sign_in(p.ana)
            status, body = await p.call("GET", "/api/tasks", ana)
            shown = [t for t in body["tasks"] if t["title"] == "Wipe the counters"]
            assert len(shown) == 1 and shown[0]["ct_recurring"] is True
    run(scenario())
