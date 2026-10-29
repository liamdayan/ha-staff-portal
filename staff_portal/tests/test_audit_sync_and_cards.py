"""Audit fixes (0.12.23): the Connecteam task sync, door records matched to cards, and phone
layouts sent with the wrong types."""

import time

import pytest

from hr_helpers import ct_submission, ct_task
from test_ct_sync import ALL_SCOPES, FOLLOW, binned, maintenance_form, poll, sent, status_field, \
    synced, writes
from test_wristbands_server import run

import ctsync  # noqa: E402
import layout  # noqa: E402
import main  # noqa: E402


def test_a_followed_form_without_forms_write_never_pauses_the_task_sync(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        ct.scope = "users.read time_clock.read forms.read quick_tasks.read quick_tasks.write"
        p.db.update_settings({"wb_time_zone": "UTC"})
        ct.forms = [maintenance_form()]
        tap = [{"questionId": "q1", "questionType": "openEnded", "value": "Leaky tap",
                "wasSubmittedEmpty": False}]
        subs = [ct_submission("s1", 55, 7, int(time.time()) - 2 * 86400, tap)]
        subs[0]["managerFields"] = status_field()
        ct.submissions = {55: subs}
        ct.refuse.add("/forms/v1/forms/55/form-submissions/*")
        async with p, ct:
            status, body = await p.admin_call("PUT", "/api/tasks/forms/55", {
                "name": "Maintenance Request", "department_id": p.house, "title_question": "Problem",
                "follow": FOLLOW})
            assert status == 200, body
            assert await main.check_task_forms() == 1
            leaky = p.db.conn.execute("SELECT * FROM tasks WHERE title = 'Leaky tap'").fetchone()
            cy = await p.sign_in(p.cy)
            await p.call("POST", f"/api/tasks/{leaky['id']}/status", cy, {"status": "doing"})
            ana = await p.sign_in(p.ana)
            t = await p.add(ana, title="Fix gate", assigned_id=p.ana)
            await ctsync.settle()
            await sent(p)
            assert not ctsync.status()["paused"]
            assert p.db.task(t["id"])["ct_task_id"]   # quick tasks still go
            row = p.db.task(leaky["id"])
            assert row["ct_pending"] is None and "forms.write" in row["ct_note"]
    run(scenario())


def test_a_refused_form_write_with_scopes_unknown_concerns_only_that_task(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        p.db.update_settings({"wb_time_zone": "UTC"})
        ct.forms = [maintenance_form()]
        tap = [{"questionId": "q1", "questionType": "openEnded", "value": "Leaky tap",
                "wasSubmittedEmpty": False}]
        subs = [ct_submission("s1", 55, 7, int(time.time()) - 2 * 86400, tap)]
        subs[0]["managerFields"] = status_field()
        ct.submissions = {55: subs}
        async with p, ct:
            await p.admin_call("PUT", "/api/tasks/forms/55", {
                "name": "Maintenance Request", "department_id": p.house, "title_question": "Problem",
                "follow": FOLLOW})
            assert await main.check_task_forms() == 1
            leaky = p.db.conn.execute("SELECT * FROM tasks WHERE title = 'Leaky tap'").fetchone()
            main.connecteam.scopes = None   # a token that doesn't list them
            ct.refuse.add("/forms/v1/forms/55/form-submissions/*")
            cy = await p.sign_in(p.cy)
            await p.call("POST", f"/api/tasks/{leaky['id']}/status", cy, {"status": "doing"})
            await ctsync.settle()
            assert not ctsync.status()["paused"]
            assert p.db.task(leaky["id"])["ct_pending"] is None
    run(scenario())


def test_a_refused_delete_concerns_only_that_task(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        async with p, ct:
            ana = await p.sign_in(p.ana)
            u = await p.add(ana, title="Sweep")
            await ctsync.settle()
            tid = p.db.task(u["id"])["ct_task_id"]
            main.connecteam.forget()   # scopes unknown (a restart)
            ct.scope = ALL_SCOPES.replace(" quick_tasks.delete", "")
            ct.refuse.add(f"/tasks/v1/taskboards/501/tasks/{tid}")
            await binned(p, u["id"], ana)
            await sent(p)
            assert not ctsync.status()["paused"]
            assert p.db.task(u["id"])["ct_note"] == ctsync.LEFT_OPEN
    run(scenario())


def test_a_change_made_here_during_a_board_read_is_still_sent(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        ct.board_tasks["501"] = [ct_task("c1", "Check the boiler", [7])]
        async with p, ct:
            await poll(p)
            tid = p.db.conn.execute("SELECT id FROM tasks WHERE ct_task_id = 'c1'").fetchone()[0]
            ct.board_tasks["501"][0]["title"] = "Check the boiler now"   # renamed there
            real, fired = ct.quick_tasks, []

            async def during(request, path):
                if request.method == "GET" and request.query.get("status") == "published" and not fired:
                    fired.append(1)   # marked Done here while the board is being read
                    p.db.conn.execute("UPDATE tasks SET status = 'done', status_at = ?, updated_at = ? "
                                      "WHERE id = ?", (time.time(), time.time(), tid))
                    p.db.conn.commit()
                    ctsync.touch(tid)
                return await real(request, path)
            ct.quick_tasks = during
            await poll(p)
            ct.quick_tasks = real
            await sent(p)
            here, there = p.db.task(tid), ct.board_tasks["501"][0]
            assert (here["title"], here["status"]) == ("Check the boiler now", "done")
            assert there["status"] == "completed"
    run(scenario())


def test_a_create_with_no_answer_on_a_long_board_doesnt_read_it_every_minute(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        ct.board_tasks["501"] = [ct_task(f"h{i}", f"Old {i}", [7], status="completed")
                                 for i in range(2100)]
        async with p, ct:
            await poll(p)
            ana = await p.sign_in(p.ana)
            ct.hang_up.add("POST")
            t = await p.add(ana, title="Only once", assigned_id=p.ana)
            await ctsync.settle()
            ct.hang_up.clear()
            mark = len(ct.task_reads)
            await sent(p)   # looked for straight away: its open and draft tasks, a page or two
            assert 0 < len(ct.task_reads) - mark <= 2
            assert p.db.task(t["id"])["ct_task_id"]   # found
            assert len(writes(ct, "POST")) == 1
    run(scenario())


def test_a_create_still_unanswered_is_looked_for_at_most_every_few_minutes(tmp_path):
    async def scenario():
        p, ct = synced(tmp_path)
        async with p, ct:
            ana = await p.sign_in(p.ana)
            ct.hang_up.add("POST!")   # never made, and no answer
            t = await p.add(ana, title="Only once", assigned_id=p.ana)
            await ctsync.settle()
            sync = ctsync._sync(p.db.task(t["id"]))
            ctsync._save(t["id"], ct_sync={**sync, "checked": time.time()})   # just looked
            mark = len(ct.task_reads)
            await sent(p)
            await sent(p)
            assert len(ct.task_reads) == mark   # not again within CREATING_RECHECK
    run(scenario())


def test_a_kept_start_after_a_due_date_moved_earlier_is_brought_back():
    soon, later = int(time.time()) + 3600, int(time.time()) + 86400
    body = ctsync._with_start({"startTime": later, "dueDate": soon})
    assert body["startTime"] <= body["dueDate"] == soon
    body = ctsync._with_start({"startTime": soon, "dueDate": later})
    assert body["startTime"] == soon   # a start before the due stays as it is


def test_a_door_record_from_before_a_card_was_written_isnt_that_card(tmp_path):
    from db import Database
    db = Database(str(tmp_path))
    now = time.time()
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(wb_jobs)")}
    job = {k: v for k, v in dict(ts=now, card_type="staff", name="Bob", count=1, records="[]",
                                 summary="", expires_at=now + 86400, actor="x", encoder_url="u",
                                 status="done", kind="write").items() if k in cols}
    jid = db.conn.execute(f"INSERT INTO wb_jobs ({','.join(job)}) VALUES ({','.join('?' * len(job))})",
                          list(job.values())).lastrowid
    db.conn.execute("INSERT INTO wb_cards (job_id, seq, status, card_number, written_at) VALUES "
                    "(?, 1, 'written', '12345678', ?)", (jid, now))
    db.conn.commit()
    assert db.match_card("12345678", now - 3 * 86400) is None
    assert db.match_card("12345678", now + 60)["name"] == "Bob"


@pytest.mark.parametrize("sent_in", [{"pages": 5}, {"pages": True}, {"pages": {"a": 1}}])
def test_a_phone_order_of_the_wrong_type_is_cleaned_not_a_crash(sent_in):
    assert layout.clean_phone_order(sent_in)["pages"] == []


def test_a_layout_of_the_wrong_types_is_cleaned_not_a_crash():
    assert layout.clean_layout({"page_groups": 3, "pages": 5})["pages"] == []
    cleaned = layout.clean_layout({"pages": [{"id": "a", "name": "A", "items": [
        {"type": "area", "area_id": float("inf")}, {"type": "group", "entities": 7}]}]})
    assert all(i["type"] != "area" for i in cleaned["pages"][0]["items"])
