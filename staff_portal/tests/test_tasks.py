"""Tasks: each department's to-do list in the staff app, tasks from Connecteam forms, and tasks in
backups and migration files.

Every rule has a test here: who sees, changes and edits which task (checked on the server), what
may be typed, the bin, the status cycle and its history, the 10-minute form checks (one task per
submission, never twice, only after the form was chosen), and what backups carry. Connecteam is
never asked: most tests set ctdata's answers, and one reads the real ctdata against the fake
Connecteam (tests/hr_helpers.py).
"""

import asyncio
import json
import os
import shutil
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from test_wristbands_server import Portal, run

import ctdata  # noqa: E402
import main  # noqa: E402
import tasks  # noqa: E402
import transfer  # noqa: E402
from test_migrate_and_backup_logs import Fresh  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


class Tasks(Portal):
    """A portal with two departments: Ana and Dee in Front Desk, Cy in Housekeeping, Bob (whose
    role may Manage tasks) in neither."""

    def __init__(self, path):
        super().__init__(path)
        main.recent_task_changes.clear()
        self.desk = self.db.create_department("Front Desk")
        self.house = self.db.create_department("Housekeeping")
        for person, dept in ((self.ana, self.desk), (self.dee, self.desk), (self.cy, self.house)):
            self.db.update_staff(person, department_ids=[dept] if dept else [])
        self.db.set_role_manage_tasks(self.manager, True)

    async def add(self, who, **fields):
        status, body = await self.call("POST", "/api/tasks", who, {"title": "Restock towels", **fields})
        assert status == 200, body
        return body["task"]

    async def listed(self, who) -> dict:
        status, body = await self.call("GET", "/api/tasks", who)
        assert status == 200, body
        return {t["title"]: t for t in body["tasks"]}


def form_answers(**answers):
    return [{"question": q.replace("_", " "), "answer": a} for q, a in answers.items()]


def submission(sub_id, at, user_id="ct-7", name="Ana Lopez", **answers):
    return {"id": sub_id, "user_id": user_id, "name": name, "submitted_at": at,
            "answers": form_answers(**answers) or form_answers(Room="12", Problem="Leaky tap")}


# ---------------------------------------------------------------- the rules on their own

def test_who_sees_and_edits_a_task():
    ana = {"id": 1, "department_ids": [10]}
    cy = {"id": 3, "department_ids": [20]}
    nobody = {"id": 4, "department_ids": []}
    task = {"department_id": 10, "assigned_id": None, "created_by_id": 2, "deleted_at": None}
    assert tasks.can_see(task, ana) and not tasks.can_see(task, cy) and not tasks.can_see(task, nobody)
    assert tasks.can_see({**task, "department_id": None}, nobody)          # Everyone
    assert tasks.can_see({**task, "assigned_id": 3}, cy)                   # given to them
    assert tasks.can_see({**task, "created_by_id": 3}, cy)                 # made by them
    assert not tasks.can_see({**task, "deleted_at": 1.0}, ana)             # in the bin
    # Editing: the maker or a manager, and only a task they can see.
    assert not tasks.can_edit(task, ana, False)
    assert tasks.can_edit(task, ana, True) and not tasks.can_edit(task, cy, True)
    assert tasks.can_edit({**task, "created_by_id": 1}, ana, False)
    # The bin: the maker, or a manager who could see it if it weren't deleted, may put it back.
    gone = {**task, "deleted_at": 5.0}
    assert tasks.can_restore(gone, {"id": 2}, False) and tasks.can_restore(gone, ana, True)
    assert not tasks.can_restore(gone, cy, True) and not tasks.can_restore(gone, nobody, True)
    assert not tasks.can_restore(gone, ana, False) and not tasks.can_restore(task, ana, True)
    # A department that's gone: only its maker, its person, and managers (to move it on).
    assert tasks.can_see(task, cy, True, {20}) and not tasks.can_see(task, cy, True, {10, 20})
    assert not tasks.can_see(task, cy, False, {20}) and tasks.can_edit(task, nobody, True, set())


def test_status_cycle_and_names():
    assert [tasks.NEXT_STATUS[s] for s in ("todo", "doing", "done", "wont")] == ["doing", "done", "todo", "todo"]
    assert tasks.STATUS_NAMES == {"todo": "To do", "doing": "In progress", "done": "Done", "wont": "Won't do"}
    task = {"status": "todo"}
    assert tasks.clean_status({"status": "doing", "from": "todo"}, task) == "doing"
    assert tasks.clean_status({"status": "wont"}, task) == "wont"
    for bad in ({"status": "finished"}, {"status": None}, {"status": "done", "from": "doing"}):
        with pytest.raises(tasks.TaskError):
            tasks.clean_status(bad, task)


def test_what_may_be_typed():
    depts, people = {10: "Front Desk"}, {1: "Ana"}
    clean = lambda **d: tasks.clean_task(d, depts, people)
    got = clean(title="  Fix\tthe\x00 gate  ", notes="Line 1\r\nLine 2  \n\x07", department_id="10",
                assigned_id=1, due_date="2026-11-02", due_time="09:30", priority="urgent")
    assert got == {"title": "Fix the gate", "notes": "Line 1\nLine 2", "department_id": 10,
                   "department_ids": None, "assigned_id": 1, "assigned_name": "Ana", "due_date": "2026-11-02",
                   "due_time": "09:30", "priority": "urgent"}
    assert clean(title="x" * 500)["title"] == "x" * tasks.MAX_TITLE
    assert len(clean(title="x", notes="y" * 9000)["notes"]) == tasks.MAX_NOTES
    assert clean(title="x", due_time="10:00")["due_time"] is None   # a time needs a date
    assert clean(title="<b>hi</b>")["title"] == "<b>hi</b>"          # kept as text, shown as text
    for bad in ({"title": "  "}, {"title": "x", "department_id": 99}, {"title": "x", "assigned_id": 2},
                {"title": "x", "due_date": "2026-02-30"}, {"title": "x", "due_date": "tomorrow"},
                {"title": "x", "due_date": "2026-01-01", "due_time": "25:00"},
                {"title": "x", "priority": "high"}, {"title": "x", "department_id": True},
                {"title": "x", "assigned_id": "abc"}):
        with pytest.raises(tasks.TaskError):
            clean(**bad)
    # Editing keeps what's left out, and a department or person that has gone since stays put.
    old = {"title": "Old", "notes": "n", "department_id": 77, "assigned_id": 9, "assigned_name": "Gone",
           "due_date": None, "due_time": None, "priority": "normal"}
    kept = tasks.clean_task({"title": "New"}, depts, people, old)
    assert (kept["title"], kept["department_id"], kept["assigned_id"], kept["assigned_name"]) == ("New", 77, 9, "Gone")
    # A form's task in several departments keeps them while its department is left as it is
    # (or sent unchanged); another department makes it that one's alone.
    several = {**old, "department_id": 77, "department_ids": "[77, 10]"}
    assert tasks.clean_task({"title": "New"}, depts, people, several)["department_ids"] == "[77, 10]"
    assert tasks.clean_task({"department_id": 77}, depts, people, several)["department_ids"] == "[77, 10]"
    moved = tasks.clean_task({"department_id": 10}, depts, people, several)
    assert (moved["department_id"], moved["department_ids"]) == (10, None)


def test_a_submission_becomes_a_task():
    form = {"form_id": "55", "name": "Maintenance", "department_id": 10, "title_question": "problem",
            "assign_submitter": 1}
    staff = {"ct-7": {"id": 1, "name": "Ana"}}
    when = datetime(2026, 10, 27, 15, 4, tzinfo=timezone.utc).timestamp()
    sub = submission("s1", when, Room="12", Problem="Leaky tap")
    task = tasks.task_from_submission(form, sub, staff, when)
    assert task["title"] == "Leaky tap" and task["department_id"] == 10
    assert (task["assigned_id"], task["assigned_name"]) == (1, "Ana")
    assert task["notes"] == "Sent by Ana Lopez on Oct 27, 2026 3:04 PM.\n\nRoom: 12\nProblem: Leaky tap"
    assert (task["source"], task["form_id"], task["submission_id"]) == ("form", "55", "s1")
    # No such answer (or none chosen): "Form name: who sent it". Not linked: no one's.
    other = tasks.task_from_submission({**form, "title_question": "Nope"}, {**sub, "user_id": "ct-9"}, staff, when)
    assert other["title"] == "Maintenance: Ana Lopez" and other["assigned_id"] is None
    assert tasks.task_from_submission({**form, "assign_submitter": 0}, sub, staff, when)["assigned_id"] is None
    # Answers that are lists or objects are written out as text.
    assert tasks.answer_text(["a", {"value": "b"}, None]) == "a, b"
    # Times Connecteam may give: seconds, milliseconds, ISO text.
    assert tasks.submitted_time(1761577440) == tasks.submitted_time(1761577440000) == 1761577440
    assert tasks.submitted_time("2025-10-27T15:04:00Z") == 1761577440
    # ctdata's own: the property's local time, to the minute
    assert tasks.submitted_time("2025-10-27T08:04", ZoneInfo("America/Los_Angeles")) == 1761577440
    assert tasks.submitted_time("soon") is None and tasks.submitted_time(True) is None
    # A link back only to Connecteam itself.
    assert tasks.clean_url("https://app.connecteam.com/#/forms/55/s1") == "https://app.connecteam.com/#/forms/55/s1"
    for bad in ("http://app.connecteam.com/x", "https://evil.com/connecteam.com", "javascript:alert(1)",
                "https://connecteam.com.evil.com/"):
        assert tasks.clean_url(bad) is None


# ---------------------------------------------------------------- the staff app

def test_tasks_need_someone_signed_in(tmp_path, monkeypatch):
    async def no_areas(max_age=300.0):
        return {}, {}
    monkeypatch.setattr(main.ha, "area_info", no_areas)   # Home Assistant is never asked

    async def scenario():
        p = Tasks(tmp_path)
        kiosk, kiosk_token = p.computer("Lobby phone", "phone", None)
        async with p:
            ana = await p.sign_in(p.ana)
            status, body = await p.call("GET", "/api/entities", ana)
            assert status == 200 and body["tasks"] is True
            # A kiosk device nobody signs in to: no tasks, and every route says so.
            kiosk_headers = {"Authorization": f"Bearer {kiosk_token}"}
            status, body = await p.call("GET", "/api/entities", kiosk_headers)
            assert body["tasks"] is False
            for method, path, data in (("GET", "/api/tasks", None), ("POST", "/api/tasks", {"title": "x"}),
                                       ("GET", "/api/tasks/people", None), ("GET", "/api/tasks/bin", None),
                                       ("GET", "/api/tasks/1", None), ("PUT", "/api/tasks/1", {"title": "x"}),
                                       ("POST", "/api/tasks/1/status", {"status": "done"}),
                                       ("DELETE", "/api/tasks/1", None), ("POST", "/api/tasks/1/restore", None)):
                status, body = await p.call(method, path, kiosk_headers, data)
                assert status == 403 and body["error"] == main.NO_TASKS, (method, path)
                # Without a PIN session on the shared computer: sign in first.
                status, body = await p.call(method, path, {"Authorization": f"Bearer {p.token}"}, data)
                assert status == 403 and body["error"] == "pin_required", (method, path)
            assert p.db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    run(scenario())


def test_people_see_their_department_everyone_and_their_own(tmp_path):
    async def scenario():
        p = Tasks(tmp_path)
        async with p:
            ana, cy, dee, bob = [await p.sign_in(x) for x in (p.ana, p.cy, p.dee, p.bob)]
            mine = await p.add(ana, title="Desk task")                     # Ana's own department
            assert mine["department_id"] == p.desk and mine["department"] == "Front Desk"
            await p.add(ana, title="For all", department_id=None)
            await p.add(cy, title="Rooms")                                 # Housekeeping
            await p.add(cy, title="Given to Dee", assigned_id=p.dee)       # Housekeeping, but Dee's
            await p.add(ana, title="Ana asks Housekeeping", department_id=p.house)
            assert set(await p.listed(ana)) == {"Desk task", "For all", "Ana asks Housekeeping"}
            assert set(await p.listed(dee)) == {"Desk task", "For all", "Given to Dee"}
            assert set(await p.listed(cy)) == {"For all", "Rooms", "Given to Dee", "Ana asks Housekeeping"}
            assert set(await p.listed(bob)) == {"For all"}                 # managing doesn't widen it
            listed = await p.listed(dee)
            assert listed["Given to Dee"]["mine"] and not listed["Desk task"]["mine"]
            assert not listed["Desk task"]["can_edit"]
            # Another department's task can't be opened, changed or deleted: it's not there.
            rooms = (await p.listed(cy))["Rooms"]["id"]
            for method, path, data in (("GET", f"/api/tasks/{rooms}", None),
                                       ("POST", f"/api/tasks/{rooms}/status", {"status": "done"}),
                                       ("PUT", f"/api/tasks/{rooms}", {"title": "Mine now"}),
                                       ("DELETE", f"/api/tasks/{rooms}", None)):
                status, _ = await p.call(method, path, ana, data)
                assert status == 404, (method, path)
            assert p.db.task(rooms)["status"] == "todo" and p.db.task(rooms)["title"] == "Rooms"
            # Moving to another department takes their tasks with it, on the very next request
            # (the ones they made stay theirs).
            await p.add(dee, title="Dee's desk task")
            assert "Dee's desk task" in await p.listed(ana)
            p.db.update_staff(p.ana, department_ids=[p.house])
            moved = await p.listed(ana)
            assert "Rooms" in moved and "Desk task" in moved and "Dee's desk task" not in moved
            # The people list for the form: names and departments only, turned-off people left out.
            p.db.update_staff(p.dee, status="disabled")
            status, body = await p.call("GET", "/api/tasks/people", cy)
            assert {x["name"] for x in body["people"]} == {"Ana", "Bob", "Cy"}
            assert set(body["people"][0]) == {"id", "name", "department_ids"}
            status, body = await p.call("POST", "/api/tasks", cy, {"title": "x", "assigned_id": p.dee})
            assert status == 400
    run(scenario())


def test_anyone_who_sees_a_task_changes_its_status_and_it_is_recorded(tmp_path):
    async def scenario():
        p = Tasks(tmp_path)
        async with p:
            ana, dee = await p.sign_in(p.ana), await p.sign_in(p.dee)
            task = await p.add(ana, title="Check pool")
            path = f"/api/tasks/{task['id']}/status"
            for who, status in ((dee, "doing"), (ana, "done"), (dee, "todo"), (ana, "wont")):
                code, body = await p.call("POST", path, who, {"status": status})
                assert code == 200 and body["task"]["status"] == status
            # Two phones tapping at once: the second saw an old status, so nothing changes.
            code, body = await p.call("POST", path, dee, {"status": "doing", "from": "todo"})
            assert code == 409 and body["task"]["status"] == "wont" and "Won't do" in body["error"]
            code, body = await p.call("POST", path, dee, {"status": "everything"})
            assert code == 409 or code == 400
            code, body = await p.call("GET", f"/api/tasks/{task['id']}", dee)
            trail = [(e["by_name"], e["action"], e["from_status"], e["to_status"]) for e in body["history"]]
            assert trail == [("Ana", "created", None, "todo"), ("Dee", "status", "todo", "doing"),
                             ("Ana", "status", "doing", "done"), ("Dee", "status", "done", "todo"),
                             ("Ana", "status", "todo", "wont")]
            assert p.db.task(task["id"])["status_by"] == "Ana"
            # The same status again changes nothing and adds nothing.
            await p.call("POST", path, ana, {"status": "wont"})
            assert len(p.db.task_history(task["id"])) == 5
    run(scenario())


def test_only_the_maker_or_a_manager_edits_and_deletes(tmp_path):
    async def scenario():
        p = Tasks(tmp_path)
        async with p:
            ana, dee, bob = await p.sign_in(p.ana), await p.sign_in(p.dee), await p.sign_in(p.bob)
            task = await p.add(ana, title="Order soap", department_id=None)
            tid = task["id"]
            status, body = await p.call("PUT", f"/api/tasks/{tid}", dee, {"title": "Dee's now"})
            assert status == 403
            status, body = await p.call("DELETE", f"/api/tasks/{tid}", dee)
            assert status == 403
            status, body = await p.call("PUT", f"/api/tasks/{tid}", ana, {"title": "Order hand soap",
                                                                       "priority": "urgent", "due_date": "2026-11-01"})
            assert status == 200 and body["task"]["title"] == "Order hand soap"
            assert body["task"]["priority"] == "urgent" and body["task"]["department_id"] is None
            # Bob's role may Manage tasks.
            status, body = await p.call("PUT", f"/api/tasks/{tid}", bob, {"notes": "Two boxes"})
            assert status == 200 and body["task"]["notes"] == "Two boxes" and body["task"]["can_edit"]
            # Taking the permission away takes it at once (checked on every request).
            p.db.set_role_manage_tasks(p.manager, False)
            status, _ = await p.call("PUT", f"/api/tasks/{tid}", bob, {"notes": "Three"})
            assert status == 403
            # On a kiosk shared computer, someone without a role gets the computer's.
            assert not main.manages_tasks(p.db.device(p.device), p.db.staff(p.cy))
            p.db.set_role_manage_tasks(p.no_cards, True)
            assert main.manages_tasks(p.db.device(p.device), p.db.staff(p.cy))
            p.db.update_staff(p.dee, role_id=None)
            assert not main.manages_tasks(p.db.device(p.device), p.db.staff(p.dee))
            p.db.update_device(p.device, signin_mode="staff", role_id=p.no_cards)
            assert main.manages_tasks(p.db.device(p.device), p.db.staff(p.dee))
            p.db.update_device(p.device, signin_mode="role", role_id=None)
            assert [e["action"] for e in p.db.task_history(tid)] == ["created", "edited", "edited"]
            assert ("Ana on Front Desk PC", "Changed a task", True) in p.activity()
    run(scenario())


def test_deleted_tasks_go_to_a_30_day_bin(tmp_path):
    async def scenario():
        p = Tasks(tmp_path)
        async with p:
            ana, dee, bob = await p.sign_in(p.ana), await p.sign_in(p.dee), await p.sign_in(p.bob)
            task = await p.add(ana, title="Fix lamp", department_id=None)
            status, _ = await p.call("DELETE", f"/api/tasks/{task['id']}", ana)
            assert status == 200
            assert "Fix lamp" not in await p.listed(ana) and "Fix lamp" not in await p.listed(dee)
            # In the maker's bin and a manager's, nobody else's.
            for who, seen in ((ana, True), (bob, True), (dee, False)):
                status, body = await p.call("GET", "/api/tasks/bin", who)
                assert [t["title"] for t in body["tasks"]] == (["Fix lamp"] if seen else [])
                assert body["days"] == 30
            assert (await p.call("GET", "/api/tasks", ana))[1]["bin"] == 1
            status, _ = await p.call("POST", f"/api/tasks/{task['id']}/status", dee, {"status": "done"})
            assert status == 404
            status, _ = await p.call("POST", f"/api/tasks/{task['id']}/restore", dee)
            assert status == 404
            status, _ = await p.call("POST", f"/api/tasks/{task['id']}/restore", bob)
            assert status == 200 and "Fix lamp" in await p.listed(dee)
            assert [e["action"] for e in p.db.task_history(task["id"])] == ["created", "deleted", "restored"]
            # Emptied after 30 days; finished tasks are dropped after a year.
            await p.call("DELETE", f"/api/tasks/{task['id']}", ana)
            done = await p.add(ana, title="Old and done")
            await p.call("POST", f"/api/tasks/{done['id']}/status", ana, {"status": "done"})
            now = time.time()
            assert p.db.purge_tasks(now + 29 * 86400, tasks.BIN_DAYS, tasks.KEEP_FINISHED_DAYS) == 0
            assert p.db.purge_tasks(now + 31 * 86400, tasks.BIN_DAYS, tasks.KEEP_FINISHED_DAYS) == 1
            assert p.db.task(task["id"]) is None and p.db.task_history(task["id"]) == []
            assert p.db.purge_tasks(now + 366 * 86400, tasks.BIN_DAYS, tasks.KEEP_FINISHED_DAYS) == 1
            assert p.db.task(done["id"]) is None
    run(scenario())


def test_a_managers_bin_holds_only_tasks_they_could_see(tmp_path):
    """Manage tasks doesn't open other departments' deleted tasks (form answers, say)."""
    async def scenario():
        p = Tasks(tmp_path)
        async with p:
            cy, bob = await p.sign_in(p.cy), await p.sign_in(p.bob)
            task = await p.add(cy, title="Incident in 12", notes="What happened: ...")
            assert task["department_id"] == p.house
            await p.call("DELETE", f"/api/tasks/{task['id']}", cy)
            status, body = await p.call("GET", "/api/tasks/bin", bob)
            assert body["tasks"] == [] and (await p.call("GET", "/api/tasks", bob))[1]["bin"] == 0
            for method, path in (("GET", f"/api/tasks/{task['id']}"),
                                 ("POST", f"/api/tasks/{task['id']}/restore")):
                assert (await p.call(method, path, bob))[0] == 404
            # In Housekeeping, the manager sees it there and may put it back.
            p.db.update_staff(p.bob, department_ids=[p.house])
            status, body = await p.call("GET", "/api/tasks/bin", bob)
            assert [t["title"] for t in body["tasks"]] == ["Incident in 12"]
            assert (await p.call("POST", f"/api/tasks/{task['id']}/restore", bob))[0] == 200
    run(scenario())


def test_a_deleted_departments_tasks_reach_managers_and_forms_keep_theirs(tmp_path, monkeypatch):
    async def scenario():
        p = Tasks(tmp_path)
        async with p:
            cy, ana, bob = await p.sign_in(p.cy), await p.sign_in(p.ana), await p.sign_in(p.bob)
            task = await p.add(cy, title="Room 4 drain")
            p.db.set_task_form("55", {"name": "Room issue", "department_id": p.house, "title_question": "",
                                      "assign_submitter": 0}, time.time())
            # A form still sends its tasks there: the department can't be deleted yet.
            status, body = await p.admin_call("DELETE", f"/api/departments/{p.house}")
            assert status == 409 and "Room issue" in body["error"] and p.db.department(p.house)
            p.db.set_task_form("55", {"name": "Room issue", "department_id": None, "title_question": "",
                                      "assign_submitter": 0}, time.time())
            status, _ = await p.admin_call("DELETE", f"/api/departments/{p.house}")
            assert status == 200 and not p.db.department(p.house)
            # Its tasks: the maker still sees them, a manager sees and moves them, others don't.
            assert "Room 4 drain" in await p.listed(cy) and "Room 4 drain" not in await p.listed(ana)
            listed = await p.listed(bob)
            assert listed["Room 4 drain"]["department"] == "Old department" and listed["Room 4 drain"]["can_edit"]
            status, body = await p.call("PUT", f"/api/tasks/{task['id']}", bob, {"department_id": p.desk})
            assert status == 200 and "Room 4 drain" in await p.listed(ana)
    run(scenario())


def test_the_list_finished_tasks_and_polls(tmp_path):
    async def scenario():
        p = Tasks(tmp_path)
        async with p:
            ana = await p.sign_in(p.ana)
            urgent = await p.add(ana, title="Urgent one", priority="urgent")
            await p.add(ana, title="Due later", due_date="2026-12-01")
            await p.add(ana, title="Due sooner", due_date="2026-11-01", due_time="08:00")
            old = await p.add(ana, title="Done long ago")
            await p.call("POST", f"/api/tasks/{old['id']}/status", ana, {"status": "done"})
            p.db.conn.execute("UPDATE tasks SET status_at = ? WHERE id = ?", (time.time() - 40 * 86400, old["id"]))
            p.db.conn.commit()
            status, body = await p.call("GET", "/api/tasks", ana)
            assert [t["title"] for t in body["tasks"]] == ["Urgent one", "Due sooner", "Due later"]
            assert "Done long ago" not in [t["title"] for t in body["tasks"]]
            assert body["me"] == {"id": p.ana, "name": "Ana", "department_ids": [p.desk], "manager": False}
            assert {d["name"] for d in body["departments"]} == {"Front Desk", "Housekeeping"}
            # Polls: the same version back costs nothing; a change gives a new one.
            status, again = await p.call("GET", f"/api/tasks?v={body['v']}", ana)
            assert again == {"v": body["v"], "unchanged": True}
            await p.call("POST", f"/api/tasks/{urgent['id']}/status", ana, {"status": "doing"})
            status, changed = await p.call("GET", f"/api/tasks?v={body['v']}", ana)
            assert "tasks" in changed and changed["v"] != body["v"]
            # Text people type comes back as text, exactly.
            await p.add(ana, title="<img src=x onerror=alert(1)>", notes="<script>x</script>")
            listed = await p.listed(ana)
            assert listed["<img src=x onerror=alert(1)>"]["notes"] == "<script>x</script>"
    run(scenario())


def test_too_many_changes_at_once(tmp_path):
    async def scenario():
        p = Tasks(tmp_path)
        async with p:
            ana = await p.sign_in(p.ana)
            task = await p.add(ana)
            for i in range(main.TASK_CHANGES_PER_MINUTE - 1):
                status, _ = await p.call("POST", f"/api/tasks/{task['id']}/status", ana,
                                         {"status": "doing" if i % 2 == 0 else "todo"})
                assert status == 200
            status, body = await p.call("POST", f"/api/tasks/{task['id']}/status", ana, {"status": "done"})
            assert status == 429
            status, _ = await p.call("GET", "/api/tasks", ana)   # reading isn't limited
            assert status == 200
    run(scenario())


# ---------------------------------------------------------------- tasks from forms

class FakeCt:
    """What ctdata answers: submissions per form, or Unavailable; and every request made."""

    def __init__(self, monkeypatch):
        self.subs, self.fail, self.calls = {}, {}, []
        monkeypatch.setattr(ctdata, "form_submissions", self.form_submissions)
        monkeypatch.setattr(ctdata, "forms", self.forms)

    async def form_submissions(self, form_id, start, end):
        self.calls.append((form_id, start, end))
        if form_id in self.fail:
            raise ctdata.Unavailable(self.fail[form_id])
        return self.subs.get(form_id, [])

    async def forms(self):
        self.calls.append(("forms",))
        if "forms" in self.fail:
            raise ctdata.Unavailable(self.fail["forms"])
        return [{"id": "55", "name": "Maintenance", "questions": [
                    {"id": "q1", "title": "Room", "type": "openEnded"},
                    {"id": "q2", "title": "Read this first", "type": "description"},
                    {"id": "q3", "title": "Problem", "type": "openEnded"},
                    {"id": "q4", "title": "Notes", "type": "openEnded"}]},
                {"id": "56", "name": "Inspection", "questions": []}]


def test_form_submissions_become_tasks_once(tmp_path, monkeypatch):
    async def scenario():
        p = Tasks(tmp_path)
        ct = FakeCt(monkeypatch)
        p.db.update_staff(p.ana, connecteam_user_id="ct-7")
        async with p:
            # Nothing is asked while no form is chosen, or while Connecteam is off.
            assert await main.check_task_forms() == 0 and ct.calls == []
            status, body = await p.admin_call("PUT", "/api/tasks/forms/55", {
                "name": "Maintenance", "department_id": p.house, "title_question": "Problem",
                "assign_submitter": True})
            assert status == 200 and body["forms"][0]["department"] == "Housekeeping"
            picked = p.db.task_form("55")["picked_at"]
            assert await main.check_task_forms() == 0 and ct.calls == []
            assert p.db.task_form("55")["last_error"] == "Connecteam is turned off in Settings."
            p.db.update_settings({"connecteam_enabled": "1"})
            p.db.conn.execute("UPDATE task_forms SET last_check = NULL")
            ct.subs["55"] = [submission("old", picked - 120, Problem="Before it was chosen"),
                             submission("s1", picked + 60, Problem="Leaky tap"),
                             submission("s2", picked + 120, user_id="ct-unknown", name="Guest", Room="3"),
                             submission("s2", picked + 120, Problem="Same one twice in one answer"),
                             {"id": None, "submitted_at": picked + 5}, "junk"]
            assert await main.check_task_forms() == 2
            made = {t["submission_id"]: t for t in map(dict, p.db.conn.execute(
                "SELECT * FROM tasks WHERE source = 'form'"))}
            assert set(made) == {"s1", "s2"}
            assert made["s1"]["title"] == "Leaky tap" and made["s1"]["assigned_id"] == p.ana
            assert made["s1"]["department_id"] == p.house and made["s1"]["created_by_name"] == "Connecteam form"
            assert made["s2"]["title"] == "Maintenance: Guest" and made["s2"]["assigned_id"] is None
            assert "Problem: Leaky tap" in made["s1"]["notes"]
            # The range asked about: from the moment it was chosen to today.
            _, start, end = ct.calls[0]
            assert abs(start.timestamp() - picked) < 0.001 and end == datetime.now(main.report_zone()).date()
            # Checked every 10 minutes, no sooner.
            ct.calls.clear()
            assert await main.check_task_forms() == 0 and ct.calls == []
            # Never twice: not on the next check, not after its task was deleted and the bin emptied.
            p.db.conn.execute("UPDATE task_forms SET last_check = last_check - 601")
            p.db.bin_task(made["s1"]["id"], {"name": "Cy"}, time.time() - 31 * 86400)
            p.db.purge_tasks(time.time(), tasks.BIN_DAYS, tasks.KEEP_FINISHED_DAYS)
            ct.subs["55"].append(submission("s3", picked + 600, Problem="New one"))
            assert await main.check_task_forms() == 1
            assert sorted(r[0] for r in p.db.conn.execute(
                "SELECT submission_id FROM tasks WHERE source = 'form'")) == ["s2", "s3"]
            assert p.db.task_form("55")["made"] == 3 and p.db.task_form("55")["last_error"] is None
            # People see them by the usual rules: Cy in Housekeeping, and Ana, whose it is.
            cy, dee = await p.sign_in(p.cy), await p.sign_in(p.dee)
            assert "New one" in await p.listed(cy) and "Maintenance: Guest" not in await p.listed(dee)
            # Only a manager edits a form's task (no one here made it).
            listed = await p.listed(cy)
            assert not listed["New one"]["can_edit"] and listed["New one"]["source"] == "form"
    run(scenario())


def test_form_checks_that_fail_pause_only_that_form(tmp_path, monkeypatch):
    async def scenario():
        p = Tasks(tmp_path)
        ct = FakeCt(monkeypatch)
        p.db.update_settings({"connecteam_enabled": "1"})
        async with p:
            for form_id in ("55", "56"):
                await p.admin_call("PUT", f"/api/tasks/forms/{form_id}", {"name": f"Form {form_id}"})
            picked = p.db.task_form("56")["picked_at"]
            ct.fail["55"] = "Reports and tasks are paused: Connecteam's daily budget is used up."
            ct.subs["56"] = [submission("a", picked + 30)]
            assert await main.check_task_forms() == 1
            assert p.db.task_form("55")["last_error"].startswith("Reports and tasks are paused")
            assert p.db.task_form("56")["last_error"] is None
            # The failed one waits its 10 minutes like the rest (no hammering a paused budget).
            ct.calls.clear()
            assert await main.check_task_forms() == 0 and ct.calls == []
            status, body = await p.admin_call("GET", "/api/tasks/forms")
            assert {f["form_id"]: f["last_error"] is None for f in body["forms"]} == {"55": False, "56": True}
            # A form removed while its check was on its way makes nothing.
            p.db.conn.execute("UPDATE task_forms SET last_check = NULL")
            del ct.fail["55"]
            ct.subs["55"] = [submission("late", time.time() + 5)]
            real = ct.form_submissions

            async def remove_meanwhile(form_id, start, end):
                p.db.delete_task_form(form_id)
                return await real(form_id, start, end)
            monkeypatch.setattr(ctdata, "form_submissions", remove_meanwhile)
            assert await main.check_task_forms() == 0
            assert p.db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    run(scenario())


def test_forms_through_the_real_ctdata_and_the_reports_lane(tmp_path):
    """No stand-in: the dashboard's forms and questions, and a check's submissions, come from
    ctdata reading the fake Connecteam in the reports lane. A refusal is the form's error and
    never stops Staff Access's own requests."""
    from hr_helpers import FakeConnecteam, ct_form, ct_submission, ct_user, fresh_connecteam

    async def scenario():
        p = Tasks(tmp_path)
        p.db.update_settings({"connecteam_enabled": "1", "connecteam_client_id": "client",
                              "connecteam_client_secret": "secret",
                              "wb_time_zone": "America/Los_Angeles"})
        p.db.update_staff(p.ana, connecteam_user_id="7")
        fresh_connecteam()
        ct = FakeConnecteam(users=[ct_user(7, "Ana", "Lopez")])
        ct.forms = [ct_form(55, "Maintenance", [("q1", "Problem", "openEnded"),
                                                ("q2", "Room", "openEnded")])]
        async with p, ct:
            status, body = await p.admin_call("GET", "/api/connecteam/forms")
            assert status == 200 and body["forms"] == [{"id": "55", "name": "Maintenance"}]
            status, body = await p.admin_call("GET", "/api/tasks/forms/55/questions")
            assert body["questions"] == ["Problem", "Room"]
            await p.admin_call("PUT", "/api/tasks/forms/55", {
                "name": "Maintenance", "department_id": p.house, "title_question": "Problem",
                "assign_submitter": True})
            picked = p.db.task_form("55")["picked_at"]
            tap = {"questionId": "q1", "questionType": "openEnded", "value": "Leaky tap",
                   "wasSubmittedEmpty": False}
            ct.submissions = {55: [ct_submission(901, 55, 7, int(picked) - 3600, [tap]),
                                   ct_submission(902, 55, 7, int(picked) + 30, [tap])]}
            assert await main.check_task_forms() == 1
            task = dict(p.db.conn.execute("SELECT * FROM tasks WHERE source = 'form'").fetchone())
            assert (task["title"], task["submission_id"], task["assigned_id"]) == ("Leaky tap", "902", p.ana)
            asked = [q for path, q in ct.asked if path.endswith("/form-submissions")]
            assert asked and int(asked[0]["submittingStartTimestamp"]) == int(picked)
            assert main.connecteam.reports_usage()["count"] > 0   # counted in the reports lane
            # Refused: the form's card says why, naming the permission; Staff Access carries on.
            ct.refuse.add("/forms/v1/forms/55/form-submissions")
            p.db.conn.execute("UPDATE task_forms SET last_check = NULL")
            ctdata.forget()
            assert await main.check_task_forms() == 0
            assert "forms.read" in p.db.task_form("55")["last_error"]
            assert main.connecteam.halted is None
    run(scenario())


def test_form_checks_stay_within_their_share_of_the_reports_lane():
    """Checks look back an hour before the last good one, not a day, so one page usually does;
    and they're spread out as forms are added, so all of them stay under FORM_DAILY_REQUESTS a
    day (under a third of the lane) however many are chosen."""
    import connecteam
    zone = ZoneInfo("America/Los_Angeles")
    now = 1_800_000_000
    form = {"picked_at": now - 5 * 86400, "last_ok": now - 600}
    assert tasks.form_range(form, now, zone)[0].timestamp() == now - 600 - 3600
    assert tasks.form_range({**form, "last_ok": None}, now, zone)[0].timestamp() == now - 5 * 86400
    assert tasks.FORM_DAILY_REQUESTS <= 0.3 * connecteam.REPORTS_DAILY_CAP
    for count in range(1, tasks.MAX_FORMS + 1):
        every = tasks.form_interval(count)
        assert every >= tasks.FORM_CHECK_SECONDS and every % 60 == 0
        assert count * 86400 / every <= tasks.FORM_DAILY_REQUESTS
    assert [tasks.form_interval(n) // 60 for n in (1, 2, 3, 7, 50)] == [10, 10, 15, 34, 240]
    assert tasks.form_requests_a_day(0) == 0 and tasks.form_requests_a_day(1) == 144
    # Due by the stretched interval: with 7 forms, 10 minutes isn't enough.
    assert not tasks.form_due({"last_check": now - 601}, now, 7)
    assert tasks.form_due({"last_check": now - 34 * 60}, now, 7) and tasks.form_due({}, now, 7)


def test_the_dashboard_says_what_form_checks_cost(tmp_path, monkeypatch):
    async def scenario():
        p = Tasks(tmp_path)
        FakeCt(monkeypatch)
        async with p:
            status, body = await p.admin_call("GET", "/api/tasks/forms")
            assert (body["check_minutes"], body["requests_a_day"], body["lane_a_day"]) == (10, 0, 2000)
            for form_id in ("55", "56", "57"):
                status, body = await p.admin_call("PUT", f"/api/tasks/forms/{form_id}", {"name": form_id})
            assert (body["check_minutes"], body["requests_a_day"]) == (15, 288)
            status, body = await p.admin_call("DELETE", "/api/tasks/forms/57")
            assert (body["check_minutes"], body["requests_a_day"]) == (10, 288)
    run(scenario())


def test_the_dashboard_chooses_forms(tmp_path, monkeypatch):
    async def scenario():
        p = Tasks(tmp_path)
        ct = FakeCt(monkeypatch)
        async with p:
            status, body = await p.admin_call("GET", "/api/connecteam/forms")
            assert status == 200 and body["forms"][0] == {"id": "55", "name": "Maintenance"}
            ct.fail["forms"] = "Staff Access's Connecteam key can't read forms. Add the forms scope."
            status, body = await p.admin_call("GET", "/api/connecteam/forms")
            assert status == 502 and "forms scope" in body["error"]
            del ct.fail["forms"]
            status, body = await p.admin_call("GET", "/api/tasks/forms/55/questions")
            assert body["questions"] == ["Room", "Problem", "Notes"]   # from ctdata.forms()
            assert (await p.admin_call("GET", "/api/tasks/forms/57/questions"))[0] == 404
            for bad in ({"department_id": 999}, {"department_id": "x"}):
                status, body = await p.admin_call("PUT", "/api/tasks/forms/55", bad)
                assert status == 400
            status, _ = await p.admin_call("PUT", "/api/tasks/forms/bad%20id", {})
            assert status == 404
            status, body = await p.admin_call("PUT", "/api/tasks/forms/55", {"name": "<b>Maint</b>"})
            first = p.db.task_form("55")
            assert first["name"] == "<b>Maint</b>" and first["department_id"] is None
            # Changing its settings doesn't move when it was chosen.
            await p.admin_call("PUT", "/api/tasks/forms/55", {"department_id": p.desk, "assign_submitter": 1})
            again = p.db.task_form("55")
            assert again["picked_at"] == first["picked_at"] and again["department_id"] == p.desk
            assert again["name"] == "<b>Maint</b>" and again["assign_submitter"] == 1
            status, body = await p.admin_call("DELETE", "/api/tasks/forms/55")
            assert body["forms"] == [] and p.db.task_form("55") is None
            assert ("Admin", "Stopped a form making tasks", True) in p.activity()
            # The admin routes are the dashboard's alone: without its header they're refused.
            resp = await p.admin.request("PUT", "/api/tasks/forms/55", json={})
            assert resp.status == 403
    run(scenario())


def test_manage_tasks_is_a_role_setting(tmp_path):
    async def scenario():
        p = Tasks(tmp_path)
        async with p:
            status, _ = await p.admin_call("PUT", f"/api/roles/{p.front_desk}", {"name": "Front Desk",
                                                                               "manage_tasks": True})
            assert status == 200 and p.db.role(p.front_desk)["manage_tasks"] == 1
            # Saving a role without it leaves it as it is.
            await p.admin_call("PUT", f"/api/roles/{p.front_desk}", {"name": "Front Desk"})
            assert p.db.role(p.front_desk)["manage_tasks"] == 1
            status, body = await p.admin_call("GET", "/api/state")
            assert {r["name"]: r["manage_tasks"] for r in body["roles"]}["Front Desk"] == 1
            copy = p.db.duplicate_role(p.front_desk, "Front Desk 2")
            assert p.db.role(copy)["manage_tasks"] == 1
            # Role files carry it too.
            exported = transfer.export_roles(p.db, "test")
            assert {r["name"]: r["manage_tasks"] for r in exported["roles"]}["Front Desk"] is True
            p.db.set_role_manage_tasks(p.front_desk, False)
            transfer.import_roles(p.db, exported, "replace")
            assert p.db.role(p.front_desk)["manage_tasks"] == 1
    run(scenario())


# ---------------------------------------------------------------- backups and migration files

async def some_tasks(p):
    ana, cy = await p.sign_in(p.ana), await p.sign_in(p.cy)
    first = await p.add(ana, title="Back up me", notes="line 1\nline 2", due_date="2026-11-02", due_time="10:00")
    await p.call("POST", f"/api/tasks/{first['id']}/status", ana, {"status": "doing"})
    binned = await p.add(cy, title="In the bin")
    await p.call("DELETE", f"/api/tasks/{binned['id']}", cy)
    p.db.set_task_form("55", {"name": "Maintenance", "department_id": p.house, "title_question": "Problem",
                              "assign_submitter": 1}, 1_700_000_000)
    p.db.add_form_tasks("55", ["s1", "s0"], [tasks.task_from_submission(
        p.db.task_form("55"), submission("s1", 1_700_000_100), {}, 1_700_000_100)], 1_700_000_200)
    return first, binned


def test_backups_carry_tasks_forms_and_the_role_setting(tmp_path):
    async def scenario():
        p = Tasks(tmp_path)
        async with p:
            first, binned = await some_tasks(p)
            status, backup = await p.admin_call("GET", "/api/backup")
            assert status == 200
            section = backup["tasks"]
            assert {t["title"] for t in section["tasks"]} == {"Back up me", "In the bin", "Leaky tap"}
            assert [f["form_id"] for f in section["forms"]] == ["55"] and sorted(map(tuple, section["seen"])) == [
                ("55", "s0"), ("55", "s1")]
            assert {r["name"]: r["manage_tasks"] for r in backup["roles"]}["Manager"] == 1
            # Everything changes, then the backup goes back.
            p.db.conn.execute("DELETE FROM tasks")
            p.db.conn.execute("DELETE FROM task_forms")
            p.db.conn.execute("DELETE FROM task_form_seen")
            p.db.set_role_manage_tasks(p.manager, False)
            status, body = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, body
            back = p.db.task(first["id"])
            assert (back["title"], back["notes"], back["status"], back["due_time"]) == (
                "Back up me", "line 1\nline 2", "doing", "10:00")
            assert p.db.task(binned["id"])["deleted_at"] is not None
            assert [e["action"] for e in p.db.task_history(first["id"])] == ["created", "status"]
            assert p.db.task_form("55")["picked_at"] == 1_700_000_000
            assert p.db.task_seen("55", ["s0", "s1", "s9"]) == {"s0", "s1"}
            assert p.db.role(p.manager)["manage_tasks"] == 1
    run(scenario())


def test_backups_from_before_tasks_keep_the_ones_here(tmp_path):
    async def scenario():
        p = Tasks(tmp_path)
        async with p:
            first, _ = await some_tasks(p)
            status, backup = await p.admin_call("GET", "/api/backup")
            del backup["tasks"]
            for role in backup["roles"]:
                del role["manage_tasks"]
            # Ana is renamed in the backup: her tasks here are no longer hers or by her.
            for m in backup["staff"]:
                if m["id"] == p.ana:
                    m["name"] = "Anabel"
            status, body = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, body
            kept = p.db.task(first["id"])
            assert kept["title"] == "Back up me" and kept["created_by_id"] is None
            assert kept["created_by_name"] == "Ana"
            assert p.db.task_form("55") is not None
            assert p.db.role(p.manager)["manage_tasks"] == 0   # older backups: no one manages tasks
    run(scenario())


def test_damaged_tasks_in_a_backup_never_widen_who_sees_them():
    now = time.time()
    good = {"id": 1, "title": "Fine", "department_id": 3, "created_at": now - 10, "status": "done",
            "status_at": now - 5}
    section = tasks.clean_backup_section({"tasks": [
        good,
        {**good, "id": 2, "department_id": "3"},          # damaged department: left out, not Everyone
        {**good, "id": 3, "department_id": True},
        {**good, "id": 1},                                # repeated
        {**good, "id": 4, "title": " "},
        {**good, "id": 5, "status": "gone", "priority": "huge", "due_date": "2026-13-01",
         "source_url": "https://evil.example/", "source": "form", "form_id": "55", "submission_id": "x"},
        {**good, "id": 6, "source": "form", "form_id": "55", "submission_id": "x"},   # same submission
        {**good, "id": 7, "department_id": None, "created_at": now + 10 * 86400},   # from the future
        "junk"],
        "history": [{"task_id": 1, "at": now, "action": "status", "from_status": "todo", "to_status": "done"},
                    {"task_id": 2, "at": now, "action": "status"}, {"task_id": 1, "at": now, "action": "hack"}],
        "forms": [{"form_id": "55", "picked_at": now - 99, "department_id": None},
                  {"form_id": "55", "picked_at": now}, {"form_id": "bad id", "picked_at": now},
                  {"form_id": "56", "picked_at": now, "department_id": "1"}],
        "seen": [["55", "s1"], ["55", ""], ["bad id", "s"], "junk"]}, now)
    assert [t["id"] for t in section["tasks"]] == [1, 5, 6]
    odd = section["tasks"][1]
    assert (odd["status"], odd["priority"], odd["due_date"], odd["source_url"]) == ("todo", "normal", None, None)
    assert section["tasks"][2]["submission_id"] is None
    assert [h["task_id"] for h in section["history"]] == [1]
    assert [f["form_id"] for f in section["forms"]] == ["55"]
    assert section["seen"] == [("55", "s1")]
    assert tasks.clean_backup_section(None, now) is None
    for bad in ("x", {"tasks": "x"}, {"tasks": [{}] * (tasks.MAX_TASKS + 1)}):
        with pytest.raises(ValueError):
            tasks.clean_backup_section(bad, now)


def test_migration_files_carry_tasks(tmp_path):
    async def scenario():
        p = Tasks(tmp_path / "old")
        async with p:
            first, _ = await some_tasks(p)
            await p.admin_call("PUT", "/api/tasks/forms/55", {"name": "Maintenance", "title_question": "Problem",
                                                              "department_ids": [p.desk, p.house]})
            desk, house = p.desk, p.house
            status, made = await p.admin_call("POST", "/api/migrate/create")
            assert status == 200, made
        f = Fresh(tmp_path / "new")
        async with f:
            status, reply = await f.admin_call("POST", "/api/migrate/restore",
                                               {"file": made["file"], "code": made["code"]})
            assert status == 200, reply
            assert f.db.task(first["id"])["title"] == "Back up me"
            assert f.db.task_form("55")["title_question"] == "Problem"
            assert tasks.departments_of(f.db.task_form("55")) == [desk, house]
            [moved] = [dict(r) for r in f.db.conn.execute("SELECT * FROM tasks WHERE source = 'form'")]
            assert tasks.departments_of(moved) == [desk, house]   # open: moved with its form
            assert f.db.task_seen("55", ["s0"]) == {"s0"}
    run(scenario())


# ---------------------------------------------------------------- forms sent to several departments

def test_a_task_in_several_departments_is_seen_in_each():
    """The rule on its own: anyone in any of its shown departments; a manager once all are gone."""
    task = {"department_id": 1, "department_ids": "[1, 2]", "assigned_id": None, "created_by_id": None}
    person = lambda *depts: {"id": 9, "department_ids": list(depts)}
    assert tasks.departments_of(task) == [1, 2]
    assert tasks.can_see(task, person(1)) and tasks.can_see(task, person(2)) and not tasks.can_see(task, person(3))
    assert tasks.can_see(task, person(2), live={2, 3}) and not tasks.can_see(task, person(1), live={2, 3})
    assert not tasks.can_see(task, person(), True, live={2}) and tasks.can_see(task, person(), True, live={3})
    # One department, or none, as before; a damaged list falls back to department_id.
    assert tasks.departments_of({"department_id": 4, "department_ids": None}) == [4]
    assert tasks.departments_of({"department_id": None}) == []
    assert tasks.departments_of({"department_id": 4, "department_ids": "[1, \"x\"]"}) == [4]
    assert tasks.stored_departments([5]) == {"department_id": 5, "department_ids": None}
    assert tasks.stored_departments([5, 6]) == {"department_id": 5, "department_ids": "[5, 6]"}
    assert tasks.stored_departments([]) == {"department_id": None, "department_ids": None}
    # A form's departments: any number of known ones, in the order given; none is Everyone.
    known = {3: "Front Desk", 1: "Housekeeping", 2: "Maintenance"}
    form = lambda **d: tasks.clean_form(d, known)
    assert form(department_ids=[2, 3, 2])["department_ids"] == [3, 2]
    assert form(department_ids=[2, 3])["department_id"] == 3
    assert form(department_ids=[]) ["department_ids"] == [] == form(department_ids=None)["department_ids"]
    assert form(department_id=1)["department_ids"] == [1] and form()["department_ids"] == []
    for bad in ([9], [1, "x"], "1", [True], list(range(1, 60))):
        with pytest.raises(tasks.TaskError):
            form(department_ids=bad)


def test_a_form_sent_to_several_departments(tmp_path, monkeypatch):
    async def scenario():
        p = Tasks(tmp_path)
        FakeCt(monkeypatch)
        spa = p.db.create_department("Spa")
        async with p:
            ana, cy, dee, bob = [await p.sign_in(x) for x in (p.ana, p.cy, p.dee, p.bob)]
            p.db.update_staff(p.dee, department_ids=[spa])
            # The dashboard: the departments ticked, in the Staff tab's order.
            status, body = await p.admin_call("PUT", "/api/tasks/forms/55", {
                "name": "Maintenance Request", "department_ids": [p.house, p.desk], "title_question": "Problem"})
            assert status == 200, body
            assert body["forms"][0]["department_ids"] == [p.desk, p.house]
            assert body["forms"][0]["department"] == "Front Desk, Housekeeping"
            form = p.db.task_form("55")
            assert (form["department_id"], json.loads(form["department_ids"])) == (p.desk, [p.desk, p.house])
            for bad in ({"department_ids": [999]}, {"department_ids": "x"}, {"department_ids": [p.desk, "x"]}):
                assert (await p.admin_call("PUT", "/api/tasks/forms/55", {"name": "M", **bad}))[0] == 400
            assert tasks.departments_of(p.db.task_form("55")) == [p.desk, p.house]   # unchanged
            # Its submissions: one task each, in both departments.
            p.db.add_form_tasks("55", ["s1", "s2"], [
                tasks.task_from_submission(p.db.task_form("55"), submission("s1", time.time(), Problem="Leaky tap"),
                                           {}, time.time()),
                tasks.task_from_submission(p.db.task_form("55"), submission("s2", time.time(), Problem="Old lamp"),
                                           {}, time.time())], time.time())
            rows = {r["title"]: dict(r) for r in p.db.conn.execute("SELECT * FROM tasks")}
            assert len(rows) == 2 and rows["Leaky tap"]["department_id"] == p.desk
            assert json.loads(rows["Leaky tap"]["department_ids"]) == [p.desk, p.house]
            # Seen by people in either department, not by others (Dee in Spa; Bob, a manager, in none).
            assert "Leaky tap" in await p.listed(ana) and "Leaky tap" in await p.listed(cy)
            assert "Leaky tap" not in await p.listed(dee) and "Leaky tap" not in await p.listed(bob)
            leak = rows["Leaky tap"]["id"]
            assert (await p.call("GET", f"/api/tasks/{leak}", dee))[0] == 404
            assert (await p.call("POST", f"/api/tasks/{leak}/status", dee, {"status": "done"}))[0] == 404
            assert (await p.call("POST", f"/api/tasks/{leak}/status", cy, {"status": "done"}))[0] == 200
            shown = (await p.listed(ana))["Old lamp"]
            assert shown["department"] == "Front Desk, Housekeeping"
            assert shown["department_ids"] == [p.desk, p.house] and shown["department_id"] == p.desk
            # A hidden one doesn't count: only the shown departments' people see it.
            p.db.update_department(p.house, hidden=1)
            assert "Old lamp" in await p.listed(ana) and "Old lamp" not in await p.listed(cy)
            p.db.update_department(p.house, hidden=0)
            # Three or more: the first two and how many more.
            p.db.conn.execute("UPDATE tasks SET department_ids = ? WHERE title = 'Old lamp'",
                              (json.dumps([p.desk, p.house, spa]),))
            assert (await p.listed(dee))["Old lamp"]["department"] == "Front Desk, Housekeeping +1"
            # Changing the form's departments moves its open tasks; finished ones stay.
            status, body = await p.admin_call("PUT", "/api/tasks/forms/55", {
                "name": "Maintenance Request", "department_ids": [p.house, spa], "title_question": "Problem"})
            assert status == 200
            lamp = p.db.task(rows["Old lamp"]["id"])
            assert tasks.departments_of(lamp) == [p.house, spa] and lamp["department_id"] == p.house
            assert tasks.departments_of(p.db.task(leak)) == [p.desk, p.house]   # Done: stays
            assert p.db.task_history(lamp["id"])[-1]["note"] == "the form's departments changed"
            assert "Old lamp" in await p.listed(dee) and "Old lamp" not in await p.listed(ana)
            # Everyone: no list at all.
            await p.admin_call("PUT", "/api/tasks/forms/55", {"name": "M", "department_ids": []})
            form = p.db.task_form("55")
            assert (form["department_id"], form["department_ids"]) == (None, "[]")
            lamp = p.db.task(lamp["id"])
            assert (lamp["department_id"], lamp["department_ids"]) == (None, None)
            assert "Old lamp" in await p.listed(bob)
            # Back to two; a manager's edit keeps them unless another department is picked.
            p.db.update_staff(p.bob, department_ids=[p.desk])
            await p.admin_call("PUT", "/api/tasks/forms/55", {"name": "M", "department_ids": [p.desk, p.house]})
            status, _ = await p.call("PUT", f"/api/tasks/{lamp['id']}", bob, {"title": "New lamp"})
            assert status == 200 and tasks.departments_of(p.db.task(lamp["id"])) == [p.desk, p.house]
            await p.call("PUT", f"/api/tasks/{lamp['id']}", bob, {"department_id": p.house})
            assert tasks.departments_of(p.db.task(lamp["id"])) == [p.house]
    run(scenario())


def test_a_department_a_form_sends_tasks_to_cant_be_deleted_or_hidden(tmp_path):
    async def scenario():
        p = Tasks(tmp_path)
        pool = p.db.create_department("Pool", smart_group_id="8")
        p.db.set_task_form("55", tasks.clean_form({"name": "Room issue", "department_ids": [p.desk, pool]},
                                                  {p.desk: "", pool: ""}), time.time())
        async with p:
            for dept, call in ((p.desk, ("DELETE", f"/api/departments/{p.desk}")),
                               (pool, ("POST", f"/api/departments/{pool}/hide"))):
                status, body = await p.admin_call(*call)
                assert status == 409 and "Room issue still sends its tasks to" in body["error"]
                assert p.db.department(dept)["hidden"] == 0
            # Housekeeping isn't one of them.
            assert (await p.admin_call("DELETE", f"/api/departments/{p.house}"))[0] == 200
            await p.admin_call("PUT", "/api/tasks/forms/55", {"name": "Room issue", "department_ids": [pool]})
            assert (await p.admin_call("DELETE", f"/api/departments/{p.desk}"))[0] == 200
            assert (await p.admin_call("POST", f"/api/departments/{pool}/hide"))[0] == 409
            # A hidden department the form has stays while its other settings are saved.
            await p.admin_call("PUT", "/api/tasks/forms/55", {"name": "Room issue", "department_ids": []})
            assert (await p.admin_call("POST", f"/api/departments/{pool}/hide"))[0] == 200
            p.db.set_task_form("55", {"name": "Room issue", "department_ids": [pool], "title_question": "",
                                      "assign_submitter": 0}, time.time())
            status, _ = await p.admin_call("PUT", "/api/tasks/forms/55", {"name": "Renamed", "department_ids": [pool]})
            assert status == 200 and p.db.task_form("55")["name"] == "Renamed"
    run(scenario())


def test_older_forms_and_tasks_have_one_department(tmp_path):
    """An install from before forms had several: its form's department becomes its list, and its
    tasks (department_id alone) are seen as before."""
    from db import Database
    p = Tasks(tmp_path)
    p.db.set_task_form("55", {"name": "Maintenance", "department_id": p.house, "title_question": "",
                              "assign_submitter": 0}, time.time())
    p.db.set_task_form("56", {"name": "Lost property", "department_id": None, "title_question": "",
                              "assign_submitter": 0}, time.time())
    p.db.add_form_tasks("55", ["s1"], [tasks.task_from_submission(
        p.db.task_form("55"), submission("s1", time.time()), {}, time.time())], time.time())
    for table in ("task_forms", "tasks"):
        p.db.conn.execute(f"ALTER TABLE {table} DROP COLUMN department_ids")
    p.db.conn.commit()
    p.db.conn.close()
    db = Database(str(tmp_path))
    assert json.loads(db.task_form("55")["department_ids"]) == [p.house]
    assert json.loads(db.task_form("56")["department_ids"]) == []
    [task] = [dict(r) for r in db.conn.execute("SELECT * FROM tasks")]
    assert task["department_ids"] is None and tasks.departments_of(task) == [p.house]
    cy = {"id": p.cy, "department_ids": [p.house]}
    assert [t["id"] for t in db.tasks_for(cy, 0)] == [task["id"]] and tasks.can_see(task, cy)
    assert db.tasks_for({"id": p.ana, "department_ids": [p.desk]}, 0) == []
    # Opened again, nothing changes twice.
    db.conn.close()
    assert json.loads(Database(str(tmp_path)).task_form("55")["department_ids"]) == [p.house]


def test_backups_carry_a_forms_and_its_tasks_departments(tmp_path):
    async def scenario():
        p = Tasks(tmp_path)
        async with p:
            await p.admin_call("PUT", "/api/tasks/forms/55", {"name": "M", "department_ids": [p.desk, p.house]})
            p.db.add_form_tasks("55", ["s1"], [tasks.task_from_submission(
                p.db.task_form("55"), submission("s1", time.time()), {}, time.time())], time.time())
            quick = await p.add(await p.sign_in(p.ana), title="Quick one")
            status, backup = await p.admin_call("GET", "/api/backup")
            assert status == 200
            p.db.conn.execute("DELETE FROM tasks")
            p.db.conn.execute("DELETE FROM task_forms")
            status, body = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, body
            assert tasks.departments_of(p.db.task_form("55")) == [p.desk, p.house]
            [made] = [dict(r) for r in p.db.conn.execute("SELECT * FROM tasks WHERE source = 'form'")]
            assert tasks.departments_of(made) == [p.desk, p.house] and made["department_id"] == p.desk
            assert p.db.task(quick["id"])["department_ids"] is None
            # (A restore signs everyone out: who sees what, straight from the database.)
            seen = [t["title"] for t in p.db.tasks_for({"id": p.cy, "department_ids": [p.house]}, 0)]
            assert made["title"] in seen and "Quick one" not in seen
            # A backup from before several departments: one each.
            section = backup["tasks"]
            for row in section["tasks"] + section["forms"]:
                row.pop("department_ids", None)
            old = tasks.clean_backup_section(section, time.time())
            assert [f["department_ids"] for f in old["forms"]] == [[p.desk]]
            assert {t["department_ids"] for t in old["tasks"]} == {None}
    run(scenario())


def test_damaged_department_lists_in_a_backup_are_left_out():
    now = time.time()
    task = {"id": 1, "title": "Fine", "department_id": 3, "department_ids": "[3, 4]", "created_at": now - 10}
    form = {"form_id": "55", "picked_at": now - 10, "department_id": 3, "department_ids": "[3, 4]"}
    bad = ("[3, \"x\"]", "[4, 3]", "{}", "nonsense", "[]", [3, True], list(range(3, 70)))
    section = tasks.clean_backup_section({
        "tasks": [task, *({**task, "id": i, "department_ids": b} for i, b in enumerate(bad, 2)),
                  {**task, "id": 20, "department_id": None, "department_ids": "[]"},
                  {**task, "id": 21, "department_ids": [3, 4, 4]}],
        "forms": [form, *({**form, "form_id": f"5{i}", "department_ids": b} for i, b in enumerate(bad, 6)),
                  {**form, "form_id": "70", "department_ids": [3]}]}, now)
    assert [(t["id"], t["department_ids"]) for t in section["tasks"]] == [(1, "[3, 4]"), (20, None), (21, "[3, 4]")]
    assert [(f["form_id"], f["department_ids"]) for f in section["forms"]] == [("55", [3, 4]), ("70", [3])]


def test_the_staff_apps_department_filter_matches_any_of_a_tasks_departments():
    if not shutil.which("node"):
        pytest.skip("no node")
    import re
    source = open(os.path.join(HERE, "..", "app", "staff", "app.js"), encoding="utf-8").read()
    code = re.search(r"  const tkDepts = .*?;\n", source).group(0) + \
        re.search(r"  function tkShown\(\) \{.*?\n  }\n", source, re.S).group(0)
    script = code + """
const tk = { pending: new Map(), filter: { mine: false, dept: "all", status: "all" }, data: { tasks: [
  { id: 1, department_id: 3, department_ids: [3, 4], status: "todo" },
  { id: 2, department_id: 4, status: "todo" },
  { id: 3, department_id: null, department_ids: [], status: "todo" },
  { id: 4, department_id: 5, department_ids: [5], status: "todo" }] } };
const ids = (dept) => { tk.filter.dept = dept; return tkShown().map((t) => t.id).join(","); };
const eq = (a, b) => { if (a !== b) { console.error(a, "!==", b); process.exit(1); } };
eq(ids("all"), "1,2,3,4"); eq(ids("3"), "1"); eq(ids("4"), "1,2"); eq(ids("everyone"), "3"); eq(ids("5"), "4");
"""
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


# ---------------------------------------------------------------- the page

def test_staff_app_script_parses_and_keeps_text_as_text():
    script = open(os.path.join(HERE, "..", "app", "staff", "app.js"), encoding="utf-8").read()
    start = script.index("// ---------------------------------------------------------------- tasks")
    section = script[start:script.index("// ---------------------------------------------------------------- wiring")]
    # Task text only ever reaches the page through h() (text nodes) and textContent.
    assert ".innerHTML" not in section.replace("span.innerHTML = `<svg", "")
    assert "insertAdjacentHTML" not in section and "eval(" not in section
    if shutil.which("node"):
        for name in ("staff/app.js", "admin/admin.js"):
            subprocess.run(["node", "--check", os.path.join(HERE, "..", "app", name)], check=True)
