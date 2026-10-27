"""People in several departments, and hidden departments.

A person is in every department whose Connecteam smart group they're in (or the ones picked by
hand). Their role, key card and clock-in rule each come from the first of their departments, in
the Staff tab's order, that sets it; their own, set by hand, still wins. Every department counts
for reports and tasks. A department that follows a smart group is hidden rather than deleted: it
gives nothing, is offered nowhere and is never made again. Connecteam's groups for everyone ("All
users group", "All admins group") are made hidden, and an older install hides its own once.

Nothing here talks to a real Connecteam: the fake one from test_staffcards_server serves the user
list and the smart groups.
"""

import json
import time

from test_migrate_and_backup_logs import Fresh
from test_reports_server import go, setup, share  # noqa: F401 - setup is a fixture
from test_staffcards_server import FLOOR_ONE, FakeConnecteam, by_name, connecteam_on, ct_user, sync
from test_tasks import Tasks
from test_wristbands_server import Portal, fake_area_layout, fake_states, run

import main  # noqa: E402
import transfer  # noqa: E402
from db import BUILT_IN_GROUPS, Database, department_list  # noqa: E402

# The owner's list: Connecteam's groups for everyone first, Female and Male, two smart groups
# both named Front Desk, and people in several groups.
GROUPS = [{"id": 1, "name": "All admins group", "numberOfUsers": 4},
          {"id": 2, "name": "All users group", "numberOfUsers": 40},
          {"id": 3, "name": "Female", "numberOfUsers": 20},
          {"id": 4, "name": "Male", "numberOfUsers": 20},
          {"id": 5, "name": "Front Desk", "numberOfUsers": 6},
          {"id": 6, "name": "Front Desk", "numberOfUsers": 1},
          {"id": 7, "name": "Housekeeping", "numberOfUsers": 9}]
USERS = [ct_user(901, "Gia", code="9011", groups=[2, 3, 5, 7]),
         ct_user(902, "Hal", code="9022", groups=[1, 2, 4, 6]),
         ct_user(903, "Ivo", code="9033", groups=[2, 4, 7])]


def ids(member) -> list[int]:
    return department_list(member["department_ids"])


def by_dept_name(p) -> dict:
    return {d["name"]: d for d in p.db.departments()}


async def hide(p, dept_id, action="hide"):
    return await p.admin_call("POST", f"/api/departments/{dept_id}/{action}")


# ---------------------------------------------------------------- from the sync

def test_the_sync_puts_people_in_every_group_and_hides_the_catch_alls(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        connecteam_on(p)
        async with FakeConnecteam(USERS, GROUPS) as ct, p:
            counts = await sync(p)
            assert counts["added"] == 3 and counts["departments_added"] == 7
            depts = by_dept_name(p)
            assert list(depts) == ["All admins group", "All users group", "Female", "Front Desk",
                                   "Front Desk (2)", "Housekeeping", "Male"]
            # Connecteam's groups for everyone are made hidden; the rest are shown.
            assert {n for n, d in depts.items() if d["hidden"]} == {"All admins group", "All users group"}
            assert ("Admin", "Made department All users group from a Connecteam smart group",
                    True) in p.activity()
            # Everyone is in every department whose smart group they're in (hidden ones too).
            gia, hal, ivo = (by_name(p, n) for n in ("Gia", "Hal", "Ivo"))
            assert ids(gia) == [depts[n]["id"] for n in ("All users group", "Female", "Front Desk",
                                                          "Housekeeping")]
            assert ids(hal) == [depts[n]["id"] for n in ("All admins group", "All users group",
                                                          "Front Desk (2)", "Male")]
            # Real people counts, not everyone in a catch-all.
            depts = by_dept_name(p)
            assert depts["All users group"]["staff_count"] == 3
            assert depts["Housekeeping"]["staff_count"] == 2 and depts["Front Desk"]["staff_count"] == 1
            assert depts["Front Desk (2)"]["staff_count"] == 1
            # The two Front Desks can be told apart: each one's group ID and size.
            status, state = await p.admin_call("GET", "/api/state")
            twins = [(d["smart_group_id"], d["smart_group_name"], d["smart_group_users"])
                     for d in state["departments"] if d["name"].startswith("Front Desk")]
            assert twins == [("5", "Front Desk", 6), ("6", "Front Desk", 1)]
            # The person's shown departments: hidden ones aren't offered anywhere.
            assert main.task_people()[gia["id"]]["department_ids"] == [
                depts[n]["id"] for n in ("Female", "Front Desk", "Housekeeping")]
            # Leaving a group in Connecteam takes them out of its department.
            ct.users = [ct_user(901, "Gia", code="9011", groups=[2, 5]), *USERS[1:]]
            await sync(p)
            assert ids(by_name(p, "Gia")) == [depts["All users group"]["id"], depts["Front Desk"]["id"]]
            assert any(a == "Gia's departments: All users group, Front Desk" for _, a, _ in p.activity())
    run(scenario())


def test_roles_come_from_the_first_department_that_sets_one(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        connecteam_on(p)
        async with FakeConnecteam(USERS, GROUPS) as ct, p:
            await sync(p)
            d = {n: x["id"] for n, x in by_dept_name(p).items()}
            # All users group (hidden) and Female leave the role alone: Front Desk's counts.
            p.db.update_department(d["Front Desk"], role_id=p.front_desk)
            p.db.update_department(d["Housekeeping"], role_id=p.manager)
            await sync(p)
            assert by_name(p, "Gia")["role_id"] == p.front_desk     # Front Desk is above
            assert by_name(p, "Ivo")["role_id"] == p.manager        # only Housekeeping sets one
            # Drag Housekeeping above Front Desk: hers comes from Housekeeping now.
            order = [x["id"] for x in p.db.departments()]
            order.remove(d["Housekeeping"])
            order.insert(order.index(d["Front Desk"]), d["Housekeeping"])
            assert (await p.admin_call("PUT", "/api/departments/order", {"order": order}))[0] == 200
            await sync(p)
            assert by_name(p, "Gia")["role_id"] == p.manager
            # A hidden department gives nothing, even with a role.
            p.db.update_department(d["All users group"], role_id=p.no_cards)
            await sync(p)
            assert by_name(p, "Gia")["role_id"] == p.manager
            # Shown again (it's first in the list), its role counts.
            assert (await hide(p, d["All users group"], "show"))[0] == 200
            await sync(p)
            assert by_name(p, "Gia")["role_id"] == p.no_cards
            assert (await hide(p, d["All users group"]))[0] == 200
            await sync(p)
            assert by_name(p, "Gia")["role_id"] == p.manager
            # The Staff tab says which department each person's role and key card come from.
            status, state = await p.admin_call("GET", "/api/state")
            row = next(m for m in state["staff"] if m["name"] == "Gia")
            assert row["role_department"] == d["Housekeeping"] and row["card_department"] is None
            # A role set by hand still wins.
            gia = by_name(p, "Gia")
            assert (await p.admin_call("PATCH", f"/api/staff/{gia['id']}",
                                       {"role_id": p.front_desk}))[0] == 200
            await sync(p)
            assert by_name(p, "Gia")["role_id"] == p.front_desk
            # Out of every department that gives a role: the one that came with them goes.
            ct.users = [USERS[0], USERS[1], ct_user(903, "Ivo", code="9033", groups=[2, 4])]
            await sync(p)
            assert by_name(p, "Ivo")["role_id"] is None
            # Her own role, not a department's: none is marked as giving it.
            status, state = await p.admin_call("GET", "/api/state")
            assert next(m for m in state["staff"] if m["name"] == "Gia")["role_department"] is None
    run(scenario())


def test_key_card_and_clock_in_from_the_first_that_sets_it(tmp_path):
    db = Database(str(tmp_path))
    staff_id = db.create_staff("Ana", "1111")
    leaves = db.create_department("All users group", smart_group_id="2")
    desk = db.create_department("Front Desk", card_access=FLOOR_ONE, clock_in=0)
    house = db.create_department("Housekeeping", card_access=[{"level": "all"}], clock_in=1)
    db.update_staff(staff_id, department_ids=[house, leaves, desk], connecteam_user_id="7")
    ana = db.staff(staff_id)
    # The stored order doesn't matter: the Staff tab's does. All users group sets nothing.
    assert db.giving_department(ana, "card_access")["id"] == desk
    assert db.clock_in_rule(ana) == (False, "department") and db.clock_in_department(ana) == desk
    # A department that sets something but is hidden gives nothing; the next one counts.
    db.update_department(desk, hidden=1)
    assert db.giving_department(ana, "card_access")["id"] == house
    assert db.clock_in_rule(ana) == (True, "department")
    assert db.member_department_ids(ana) == [leaves, house]
    # Leaving all three alone gives nothing at all.
    db.update_department(house, card_access=None, clock_in=None)
    assert db.giving_department(ana, "card_access") is None
    assert db.clock_in_rule(ana) == (False, "default")
    # Their own clock-in setting, set by hand, still wins.
    db.update_department(house, clock_in=1)
    db.update_staff(staff_id, clock_in_set=1, require_clock_in=0)
    assert db.clock_in_rule(db.staff(staff_id)) == (False, "person")
    # A role that was deleted doesn't count as set.
    role = db.create_role("Gone")
    db.update_department(leaves, role_id=role)
    db.delete_role(role)
    assert db.giving_department(ana, "role_id") is None
    db.conn.close()


def test_make_key_card_uses_the_first_department_with_one(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        p.db.update_settings({"wristbands_enabled": "1"})
        plain = p.db.create_department("Front Desk")
        async with p:
            p.db.update_staff(p.cy, department_ids=[plain])
            status, reply = await p.admin_call("POST", f"/api/staff/{p.cy}/keycard", {})
            assert status == 400 and "department with a key card template" in reply["error"]
            house = p.db.create_department("Housekeeping", card_access=FLOOR_ONE)
            p.db.update_staff(p.cy, department_ids=[plain, house])
            assert p.db.giving_department(p.db.staff(p.cy), "card_access")["id"] == house
            # The staff app's card for Cy records the department its doors came from.
            holder = main.staff_named("Cy")
            assert (p.db.giving_department(holder, "card_access") or {}).get("id") == house
    run(scenario())


# ---------------------------------------------------------------- reports and tasks

def test_reports_count_every_department(setup):  # noqa: F811
    p = setup

    async def scenario():
        async with p:
            await share(p, view=[p.front], manage=[p.kitchen])
            p.db.update_staff(p.cy, department_ids=[p.front, p.kitchen])
            cy = p.db.staff(p.cy)
            assert main.report_rights(cy, "hours") == (True, True)
            ctx = main.report_context(main.REPORTS["hours"], cy, True)
            assert ctx.person["departments"] == [{"id": p.front, "name": "Front"},
                                                 {"id": p.kitchen, "name": "Kitchen"}]
            # Hidden, Kitchen gives nothing: Cy only views, and it's left out of the context.
            p.db.update_department(p.kitchen, smart_group_id="22")
            assert (await hide(p, p.kitchen))[0] == 200
            assert main.report_rights(p.db.staff(p.cy), "hours") == (True, False)
            assert main.report_rights(p.db.staff(p.bob), "hours") == (False, False)
            ctx = main.report_context(main.REPORTS["hours"], p.db.staff(p.cy), False)
            assert [d["id"] for d in ctx.departments] == [p.front]
            assert {s["connecteam_user_id"] for s in ctx.staff} == set()
            # Hidden departments aren't offered, but the choice survives a save, for Show.
            await share(p, view=[p.front], manage=[])
            assert main.db.report("hours")["manage"] == [p.kitchen]
            status, reply = await p.admin_call("PUT", "/api/reports/hours", {"view": [p.kitchen]})
            assert status == 400
            assert (await hide(p, p.kitchen, "show"))[0] == 200
            assert main.report_rights(p.db.staff(p.bob), "hours") == (True, True)
            # Linked people are listed with every department they're in.
            p.db.update_staff(p.cy, connecteam_user_id="77")
            ctx = main.report_context(main.REPORTS["hours"], p.db.staff(p.cy), False)
            assert ctx.staff == [{"connecteam_user_id": "77", "department_ids": [p.front, p.kitchen],
                                  "department_id": p.front}]
    go(scenario())


def test_tasks_count_every_department(tmp_path):
    async def scenario():
        p = Tasks(tmp_path)
        p.db.update_staff(p.ana, department_ids=[p.desk, p.house])
        async with p:
            ana, cy = await p.sign_in(p.ana), await p.sign_in(p.cy)
            await p.add(cy, title="Rooms")                       # Housekeeping
            status, body = await p.call("GET", "/api/tasks", ana)
            assert {t["title"] for t in body["tasks"]} == {"Rooms"}
            assert body["me"]["department_ids"] == [p.desk, p.house]
            mine = await p.add(ana, title="Mine")                # her first department
            assert mine["department_id"] == p.desk
            status, body = await p.call("GET", "/api/tasks/people", ana)
            assert next(x for x in body["people"] if x["id"] == p.ana)["department_ids"] == [p.desk, p.house]
            # Hidden, Housekeeping gives nothing: its tasks are gone from her list, and it isn't
            # offered (a task for it is shown under its own name to those who still see it).
            p.db.update_department(p.house, smart_group_id="7")
            assert (await hide(p, p.house))[0] == 200
            status, body = await p.call("GET", "/api/tasks", ana)
            assert {t["title"] for t in body["tasks"]} == {"Mine"}
            assert body["me"]["department_ids"] == [p.desk]
            assert p.house not in [d["id"] for d in body["departments"]]
            status, body = await p.call("GET", "/api/tasks", cy)
            assert [t["department"] for t in body["tasks"] if t["title"] == "Rooms"] == ["Housekeeping"]
            status, body = await p.call("POST", "/api/tasks", ana, {"title": "x", "department_id": p.house})
            assert status == 400
    run(scenario())


# ---------------------------------------------------------------- hiding

def test_hide_and_show(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        connecteam_on(p)
        async with FakeConnecteam(USERS, GROUPS), p:
            await sync(p)
            d = {n: x["id"] for n, x in by_dept_name(p).items()}
            # Female follows a smart group: Hide, not Delete. Its people stay in it.
            assert (await hide(p, d["Female"]))[0] == 200
            assert p.db.department(d["Female"])["hidden"] == 1
            assert ("Admin", "Hid department Female", True) in p.activity()
            assert d["Female"] in ids(by_name(p, "Gia"))
            # Never made again by the sync, and kept up to date like any other.
            counts = await sync(p)
            assert counts["departments_added"] == 0 and len(p.db.departments()) == 7
            assert p.db.department(d["Female"])["hidden"] == 1
            # Shown again, as it was.
            assert (await hide(p, d["Female"], "show"))[0] == 200
            assert p.db.department(d["Female"])["hidden"] == 0
            assert ("Admin", "Showed department Female", True) in p.activity()
            # Hidden ones can't be picked anywhere.
            assert (await hide(p, d["Male"]))[0] == 200
            status, reply = await p.admin_call("PATCH", f"/api/staff/{p.ana}", {"department_ids": [d["Male"]]})
            assert status == 400
            status, reply = await p.admin_call("PUT", "/api/tasks/forms/55",
                                               {"name": "x", "department_id": d["Male"]})
            assert status == 400 and not p.db.task_forms()
            # The HR portal doesn't offer them either.
            groups = {x["smart_group_id"] for x in main.hr_departments() if x["hidden"]}
            assert groups == {"1", "2", "4"}
            # Made by hand: Delete, not Hide.
            plain = p.db.create_department("Spa")
            status, reply = await hide(p, plain)
            assert status == 400 and "Delete it instead" in reply["error"]
            # Deleting one that follows a smart group: the sync only makes it again.
            assert (await p.admin_call("DELETE", f"/api/departments/{d['Housekeeping']}"))[0] == 200
            assert (await sync(p))["departments_added"] == 1
    run(scenario())


def test_a_department_used_by_a_task_form_cant_be_hidden(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        house = p.db.create_department("Housekeeping", smart_group_id="7")
        p.db.set_task_form("55", {"name": "Room issue", "department_id": house, "title_question": "",
                                  "assign_submitter": 0}, time.time())
        async with p:
            status, hidden = await hide(p, house)
            status_deleted, deleted = await p.admin_call("DELETE", f"/api/departments/{house}")
            assert status == status_deleted == 409
            assert hidden["error"] == deleted["error"] == (
                "Room issue still sends its tasks to Housekeeping. On the Tasks tab, pick another "
                "department for it (or Everyone) first.")
            assert p.db.department(house)["hidden"] == 0
            # Showing is never in the way.
            assert (await hide(p, house, "show"))[0] == 200
    run(scenario())


def test_an_older_install_hides_its_catch_alls_once(tmp_path):
    assert BUILT_IN_GROUPS == ("all users group", "all admins group")
    db = Database(str(tmp_path))
    everyone = db.create_department("All users group", smart_group_id="2")
    admins = db.create_department("ALL ADMINS GROUP", smart_group_id="1")     # any capitals
    kept = db.create_department("Front Desk", smart_group_id="5")
    staff_id = db.create_staff("Ana", "1111")
    # Given something by the owner, or not following a smart group: left shown.
    owned = db.create_department("All Users Group", smart_group_id="3", clock_in=1)
    by_hand = db.create_department("all admins group", smart_group_id=None)
    # As installs from before: one department each, and no hiding.
    db.conn.execute("ALTER TABLE departments DROP COLUMN hidden")
    db.conn.execute("ALTER TABLE staff DROP COLUMN department_ids")
    db.conn.execute("ALTER TABLE staff ADD COLUMN department_id INTEGER")
    db.conn.execute("UPDATE staff SET department_id = ? WHERE id = ?", (kept, staff_id))
    db.conn.commit()
    db.conn.close()
    db = Database(str(tmp_path))
    hidden = {d["id"] for d in db.departments() if d["hidden"]}
    assert hidden == {everyone, admins}
    assert owned not in hidden and by_hand not in hidden and kept not in hidden
    # Their one department became the first of their list.
    assert ids(db.staff(staff_id)) == [kept] and db.staff(staff_id)["department_id"] is None
    logged = [a["action"] for a in db.activity()]
    assert "Hid department All users group" in logged and len(logged) == 2
    db.conn.close()
    # Only once: shown again by the owner, it stays shown.
    db = Database(str(tmp_path))
    db.update_department(everyone, hidden=0)
    db.conn.close()
    db = Database(str(tmp_path))
    assert not db.department(everyone)["hidden"] and len(db.activity()) == 2
    db.conn.close()


# ---------------------------------------------------------------- by hand

def test_departments_picked_by_hand(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        desk = p.db.create_department("Front Desk", smart_group_id="5")
        house = p.db.create_department("Housekeeping", clock_in=1)
        hidden = p.db.create_department("All users group", smart_group_id="2", hidden=1)
        async with p:
            # Not linked: any number, from the Edit dialog.
            status, _ = await p.admin_call("PATCH", f"/api/staff/{p.ana}",
                                           {"department_ids": [house, desk]})
            assert status == 200 and ids(p.db.staff(p.ana)) == [desk, house]
            assert p.db.activity(1)[0]["detail"] == "departments: Front Desk, Housekeeping"
            # A hidden one they're in stays when the picked ones are saved.
            p.db.update_staff(p.ana, department_ids=[desk, hidden])
            assert (await p.admin_call("PATCH", f"/api/staff/{p.ana}", {"department_ids": [house]}))[0] == 200
            assert ids(p.db.staff(p.ana)) == [house, hidden]
            for bad in ([999], [hidden], ["x"], [True]):
                status, _ = await p.admin_call("PATCH", f"/api/staff/{p.ana}", {"department_ids": bad})
                assert status == 400, bad
            # None at all.
            assert (await p.admin_call("PATCH", f"/api/staff/{p.ana}", {"department_ids": []}))[0] == 200
            assert ids(p.db.staff(p.ana)) == [hidden]
            # Added with several.
            status, reply = await p.admin_call("POST", "/api/staff", {
                "name": "Eve", "pin": "5555", "department_ids": [desk, house]})
            assert status == 200 and ids(p.db.staff(reply["id"])) == [desk, house]

            # Linked and following Connecteam: the same list sent again changes nothing...
            p.db.update_settings({"connecteam_enabled": "1"})
            p.db.update_staff(p.bob, connecteam_user_id="20", department_ids=[desk])
            assert (await p.admin_call("PATCH", f"/api/staff/{p.bob}", {"department_ids": [desk]}))[0] == 200
            assert p.db.staff(p.bob)["department_set"] == 0
            # ...and a change sets the whole list by hand, which the sync leaves alone.
            assert (await p.admin_call("PATCH", f"/api/staff/{p.bob}",
                                       {"department_ids": [desk, house]}))[0] == 200
            bob = p.db.staff(p.bob)
            assert (ids(bob), bob["department_set"]) == ([desk, house], 1)
            # Its clock-in rule comes with it (Housekeeping's: the only one that sets one).
            assert p.db.clock_in_rule(bob) == (True, "department")
            async with FakeConnecteam([ct_user(20, "Bob", code="2222", groups=[5])]):
                connecteam_on(p, connecteam_auto_staff="0", connecteam_auto_departments="0")
                await sync(p)
                assert ids(p.db.staff(p.bob)) == [desk, house]
                # Follow Connecteam: back to their smart groups' departments (Housekeeping has
                # no smart group, so it's kept).
                await main.connecteam.users()
                assert (await p.admin_call("PATCH", f"/api/staff/{p.bob}", {"department_set": False}))[0] == 200
                assert ids(p.db.staff(p.bob)) == [desk, house]
                p.db.update_department(house, smart_group_id="8")
                await sync(p)
                assert ids(p.db.staff(p.bob)) == [desk]
    run(scenario())


# ---------------------------------------------------------------- backups and migration files

def test_backups_carry_every_department_and_hiding(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        desk = p.db.create_department("Front Desk", smart_group_id="5", role_id=p.front_desk)
        house = p.db.create_department("Housekeeping")
        everyone = p.db.create_department("All users group", smart_group_id="2", hidden=1)
        p.db.update_staff(p.ana, department_ids=[desk, house, everyone])
        async with p:
            status, backup = await p.admin_call("GET", "/api/backup")
            assert status == 200
            row = next(m for m in backup["staff"] if m["name"] == "Ana")
            assert json.loads(row["department_ids"]) == [desk, house, everyone]
            assert [d["hidden"] for d in backup["departments"]] == [0, 0, 1]
            p.db.update_staff(p.ana, department_ids=[])
            p.db.update_department(everyone, hidden=0)
            assert (await p.admin_call("POST", "/api/restore", {"backup": backup}))[0] == 200
            assert ids(p.db.staff(p.ana)) == [desk, house, everyone]
            assert p.db.department(everyone)["hidden"] == 1

            # A backup from before: one department each, no hiding. Their one department is
            # their list, and Connecteam's group for everyone comes back hidden.
            old = json.loads(json.dumps(backup))
            for m in old["staff"]:
                m["department_id"] = (json.loads(m.pop("department_ids")) or [None])[0]
            for d in old["departments"]:
                d.pop("hidden")
            assert (await p.admin_call("POST", "/api/restore", {"backup": old}))[0] == 200
            assert ids(p.db.staff(p.ana)) == [desk]
            assert [d["hidden"] for d in p.db.departments()] == [0, 0, 1]

            # Departments and staff files: names, and whether each is hidden.
            p.db.update_staff(p.ana, department_ids=[desk, house])
            staff_file = transfer.export_staff(p.db, main.VERSION)
            ana = next(m for m in staff_file["staff"] if m["name"] == "Ana")
            assert (ana["departments"], ana["department"]) == (["Front Desk", "Housekeeping"], "Front Desk")
            dept_file = transfer.export_departments(p.db, main.VERSION)
            assert [d["hidden"] for d in dept_file["departments"]] == [False, False, True]
            p.db.update_staff(p.ana, department_ids=[])
            transfer.import_staff(p.db, staff_file, "replace")
            assert ids(p.db.staff(p.ana)) == [desk, house]
            # A staff file from before: "department", one name.
            ana.pop("departments")
            transfer.import_staff(p.db, staff_file, "replace")
            assert ids(p.db.staff(p.ana)) == [desk]
            p.db.update_department(everyone, hidden=0)
            transfer.import_departments(p.db, dept_file, "replace")
            assert p.db.department(everyone)["hidden"] == 1
            # A departments file from before hiding adds Connecteam's group for everyone hidden.
            for d in dept_file["departments"]:
                d.pop("hidden")
            p.db.delete_department(everyone)
            transfer.import_departments(p.db, dept_file, "skip")
            assert by_dept_name(p)["All users group"]["hidden"] == 1
    run(scenario())


def test_migration_files_carry_every_department_and_hiding(tmp_path):
    async def scenario():
        p = Portal(tmp_path / "old")
        desk = p.db.create_department("Front Desk", smart_group_id="5")
        everyone = p.db.create_department("All users group", smart_group_id="2", hidden=1)
        p.db.update_staff(p.ana, department_ids=[desk, everyone])
        async with p:
            status, made = await p.admin_call("POST", "/api/migrate/create")
            assert status == 200, made
        f = Fresh(tmp_path / "new")
        async with f:
            status, reply = await f.admin_call("POST", "/api/migrate/restore",
                                               {"file": made["file"], "code": made["code"]})
            assert status == 200, reply
            assert ids(next(m for m in f.db.staff_list() if m["name"] == "Ana")) == [desk, everyone]
            assert f.db.department(everyone)["hidden"] == 1 and f.db.department(desk)["hidden"] == 0
    run(scenario())


def test_the_hr_portal_leaves_hidden_groups_out():
    import hr.server as hr_server

    class People:
        def people(self, archived=False):
            return [{"groups": json.dumps(["2", "5", "8"])}]

    class Service:
        db = People()

        def departments(self):
            return [{"smart_group_id": "2", "name": "All users group", "hidden": True},
                    {"smart_group_id": "5", "name": "Front Desk", "hidden": False},
                    {"smart_group_id": None, "name": "Spa", "hidden": False}]

        def group_names(self):
            return {"8": "Pool"}
    assert hr_server.departments_list(Service()) == [{"group": "5", "name": "Front Desk"},
                                                     {"group": "8", "name": "Pool"}]


# ---------------------------------------------------------------- start page per department

LAYOUT = {"pages": [{"id": "lobby", "name": "Lobby", "items": []},
                    {"id": "pool", "name": "Pool", "items": []}], "show_unplaced": True}


def test_start_page_comes_from_the_first_department_that_sets_one(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "area_layout", fake_area_layout)
    monkeypatch.setattr(main.ha, "states", fake_states)

    async def scenario():
        p = Portal(tmp_path)
        p.db.set_layout(LAYOUT)
        leaves = p.db.create_department("Female", smart_group_id="3")
        desk = p.db.create_department("Front Desk", start_page="page:pool")
        house = p.db.create_department("Housekeeping", start_page="tool:tasks")
        p.db.update_staff(p.ana, department_ids=[house, leaves, desk])
        shown = {"pages": [{"id": "lobby"}, {"id": "pool"}]}
        offered = {"tasks": True, "reports": False, "wristbands": False}
        async with p:
            ana = p.db.staff(p.ana)
            # Female leaves it alone; Front Desk is above Housekeeping.
            assert main.start_for(ana, shown, offered) == {"page": "pool"}
            # Hidden, it gives nothing: the next one that sets one counts.
            p.db.update_department(desk, hidden=1)
            assert main.start_for(ana, shown, offered) == {"tool": "tasks"}
            p.db.update_department(desk, hidden=0)
            # A page or tool they can't see here: as always (not the next department's).
            assert main.start_for(ana, {"pages": [{"id": "lobby"}]}, offered) is None
            p.db.update_department(desk, start_page="tool:reports")
            assert main.start_for(ana, shown, offered) is None
            assert main.start_for(ana, shown, {**offered, "reports": True}) == {"tool": "reports"}
            assert main.start_for(None, shown, offered) is None
            # The staff app is told, with their data: Tasks (Front Desk's Pool page isn't one of
            # Ana's pages on this computer, so Housekeeping's doesn't count either).
            who = await p.sign_in(p.ana)
            status, body = await p.call("GET", "/api/entities", who)
            assert status == 200 and body["start"] is None
            p.db.update_department(desk, start_page=None)
            status, body = await p.call("GET", "/api/entities", who)
            assert body["start"] == {"tool": "tasks"}
            # Set from the dashboard: pages of the layout and the three tools only.
            for bad in ("page:gone", "tool:settings", "lobby", 5):
                status, reply = await p.admin_call("PUT", f"/api/departments/{house}", {"start_page": bad})
                assert status == 400, bad
            assert (await p.admin_call("PUT", f"/api/departments/{house}", {"start_page": "page:lobby"}))[0] == 200
            assert (await p.admin_call("PUT", f"/api/departments/{house}", {"start_page": None}))[0] == 200
            assert p.db.department(house)["start_page"] is None
    run(scenario())


def test_a_removed_start_page_is_left_alone(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        p.db.set_layout(LAYOUT)
        desk = p.db.create_department("Front Desk", start_page="page:pool")
        house = p.db.create_department("Housekeeping", start_page="tool:tasks")
        p.db.update_staff(p.ana, department_ids=[desk, house])
        shown = {"pages": [{"id": "lobby"}, {"id": "pool"}]}
        async with p:
            p.db.set_layout({**LAYOUT, "pages": LAYOUT["pages"][:1]})   # Pool deleted
            # Front Desk now leaves it alone, so Housekeeping's counts.
            assert main.start_for(p.db.staff(p.ana), shown, {"tasks": True}) == {"tool": "tasks"}
            status, state = await p.admin_call("GET", "/api/state")
            flags = {d["name"]: d["start_page_removed"] for d in state["departments"]}
            assert flags == {"Front Desk": True, "Housekeeping": False}
            # Saving the department unchanged keeps it; it can't be picked anew.
            assert (await p.admin_call("PUT", f"/api/departments/{desk}", {"name": "Desk",
                                                                          "start_page": "page:pool"}))[0] == 200
            assert (await p.admin_call("PUT", f"/api/departments/{house}", {"start_page": "page:pool"}))[0] == 400
    run(scenario())


def test_start_pages_in_backups_and_files(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        p.db.set_layout(LAYOUT)
        desk = p.db.create_department("Front Desk", start_page="page:pool")
        async with p:
            status, backup = await p.admin_call("GET", "/api/backup")
            assert backup["departments"][0]["start_page"] == "page:pool"
            p.db.update_department(desk, start_page=None)
            assert (await p.admin_call("POST", "/api/restore", {"backup": backup}))[0] == 200
            assert p.db.department(desk)["start_page"] == "page:pool"
            # Damaged, or from before start pages: left alone.
            backup["departments"][0]["start_page"] = "<b>x</b>"
            assert (await p.admin_call("POST", "/api/restore", {"backup": backup}))[0] == 200
            assert p.db.department(desk)["start_page"] is None
            p.db.update_department(desk, start_page="tool:reports")
            file = transfer.export_departments(p.db, main.VERSION)
            assert file["departments"][0]["start_page"] == "tool:reports"
            p.db.update_department(desk, start_page=None)
            transfer.import_departments(p.db, file, "replace")
            assert p.db.department(desk)["start_page"] == "tool:reports"
    run(scenario())


def test_a_choice_made_in_this_sign_in_beats_the_start_page(tmp_path):
    """The staff app's startChoice, run in node: the start page opens once per sign-in, never over
    a page or tool chosen in it, and only when it can be opened."""
    import os
    import re
    import shutil
    import subprocess
    import pytest
    if not shutil.which("node"):
        pytest.skip("no node")
    here = os.path.dirname(os.path.abspath(__file__))
    source = open(os.path.join(here, "..", "app", "staff", "app.js"), encoding="utf-8").read()
    fn = re.search(r"  function startChoice\(.*?\n  }\n", source, re.S).group(0)
    script = fn + """
const eq = (a, b, what) => { if (JSON.stringify(a) !== JSON.stringify(b)) { console.error(what, JSON.stringify(a)); process.exit(1); } };
const tools = ["tasks"], pages = ["lobby", "pool"];
eq(startChoice({ page: "pool" }, null, "s1", tools, pages), { page: "pool" }, "first time in this sign-in");
eq(startChoice({ page: "pool" }, "s1", "s1", tools, pages), null, "chosen (or applied) in this sign-in");
eq(startChoice({ page: "pool" }, "s1", "s2", tools, pages), { page: "pool" }, "the next person's sign-in");
eq(startChoice({ tool: "tasks" }, "device", "s3", tools, pages), { tool: "tasks" }, "a tool");
eq(startChoice({ tool: "reports" }, null, "s4", tools, pages), null, "a tool not offered");
eq(startChoice({ page: "gone" }, null, "s5", tools, pages), null, "a page not here");
eq(startChoice(null, null, "s6", tools, pages), null, "none set");
"""
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
