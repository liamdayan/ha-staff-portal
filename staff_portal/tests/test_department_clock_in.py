"""The clock-in rule per department: a department can say everyone in it must be clocked in on
Connecteam to sign in, or that no one in it needs to, or leave it to each person. A person's own
setting, changed by hand, still wins. One rule (db.clock_in_rule) is used for sign-in, the time
clock checks, the staff app's list of names and the Staff tab.

Nothing here talks to a real Connecteam: who's clocked in is set directly (and a check that
would ask Connecteam is counted instead), or the fake Connecteam from test_staffcards_server
serves the user list for the staff sync.
"""

import itertools
import json
import time

from test_staffcards_server import FakeConnecteam, by_name, connecteam_on, ct_user, department, sync
from test_wristbands_server import Portal, run

import main  # noqa: E402
import transfer  # noqa: E402
from db import Database  # noqa: E402


def clock(p: Portal, monkeypatch, clocked_in=()) -> list:
    """Connecteam turned on, a time clock answer from just now with these user IDs on the
    clock, and any check that would ask Connecteam counted instead (the list returned)."""
    p.db.update_settings({"connecteam_enabled": "1", "connecteam_client_id": "client",
                          "connecteam_client_secret": "secret"})
    checks = []

    async def check(force=False, admin=False):
        checks.append(force)
    monkeypatch.setattr(main.connecteam, "check", check)
    monkeypatch.setattr(main.connecteam, "checked_at", time.time())
    monkeypatch.setattr(main.connecteam, "clocked_in", set(clocked_in))
    return checks


async def unlock(p: Portal, staff_id) -> tuple[int, dict]:
    resp = await p.staff.post("/api/unlock", json={"staff_id": staff_id, "pin": p.pins[staff_id]},
                              headers={"Authorization": f"Bearer {p.token}"})
    return resp.status, await resp.json()


async def off_clock(p: Portal) -> dict:
    """The staff app's list of names: who shows "Not clocked in"."""
    status, reply = await p.call("GET", "/api/me", {"Authorization": f"Bearer {p.token}"})
    assert status == 200, reply
    return {m["id"]: m["off_clock"] for m in reply["staff"]}


async def staff_tab(p: Portal, staff_id) -> dict:
    status, state = await p.admin_call("GET", "/api/state")
    assert status == 200, state
    return next(m for m in state["staff"] if m["id"] == staff_id)


# ---------------------------------------------------------------- the rule

def test_precedence_matrix(tmp_path):
    """Their own if set by hand, else their department's if it sets one, else their own."""
    db = Database(str(tmp_path))
    rules = {None: db.create_department("Front Desk"),
             1: db.create_department("Housekeeping", clock_in=1),
             0: db.create_department("Managers", clock_in=0)}
    assert db.department_clock_ins() == {rules[1]: 1, rules[0]: 0}
    for n, (set_by_hand, own, dept) in enumerate(
            itertools.product((0, 1), (0, 1), (None, 1, 0, "none"))):
        staff_id = db.create_staff(f"Person {n}", f"{1000 + n}")
        db.update_staff(staff_id, connecteam_user_id=str(n), clock_in_set=set_by_hand,
                        require_clock_in=own, department_id=None if dept == "none" else rules[dept])
        expected = ((bool(own), "person") if set_by_hand
                    else (bool(dept), "department") if dept in (0, 1)
                    else (bool(own), "default"))
        member = db.staff(staff_id)
        assert db.clock_in_rule(member) == expected, (set_by_hand, own, dept)
        assert db.clock_in_rule(member, db.department_clock_ins()) == expected
    db.conn.close()


def test_departments_set_it_and_the_staff_tab_says_where_it_comes_from(tmp_path, monkeypatch):
    async def scenario():
        p = Portal(tmp_path)
        clock(p, monkeypatch)
        p.db.update_staff(p.ana, connecteam_user_id="10", require_clock_in=0)
        async with p:
            status, reply = await p.admin_call("POST", "/api/departments",
                                               {"name": "Housekeeping", "clock_in": True})
            assert status == 200, reply
            house = reply["id"]
            assert p.db.department(house)["clock_in"] == 1
            assert p.db.activity(1)[0]["detail"] == "clock-in: everyone must be clocked in to sign in"
            for bad in ("sometimes", 2, [], {"on": True}):
                status, reply = await p.admin_call("PUT", f"/api/departments/{house}", {"clock_in": bad})
                assert status == 400 and "clock-in" in reply["error"], bad
            # Left out: kept. "0" / false: no one needs to; null: each person's own.
            assert (await p.admin_call("PUT", f"/api/departments/{house}", {"name": "House"}))[0] == 200
            assert p.db.department(house)["clock_in"] == 1
            assert p.db.activity(1)[0]["detail"] is None
            assert (await p.admin_call("PUT", f"/api/departments/{house}", {"clock_in": "0"}))[0] == 200
            assert p.db.department(house)["clock_in"] == 0
            assert (await p.admin_call("PUT", f"/api/departments/{house}", {"clock_in": None}))[0] == 200
            assert p.db.department(house)["clock_in"] is None
            assert p.db.activity(1)[0]["detail"] == "clock-in: each person's own setting"
            status, state = await p.admin_call("GET", "/api/state")
            assert next(d for d in state["departments"] if d["id"] == house)["clock_in"] is None

            # Ana's own (not set by hand): the default.
            ana = await staff_tab(p, p.ana)
            assert (ana["clock_in"], ana["clock_in_from"], ana["clock"]) == (False, "default", None)
            # Her department says everyone must be clocked in: she must, and she's not on the clock.
            p.db.update_staff(p.ana, department_id=house)
            await p.admin_call("PUT", f"/api/departments/{house}", {"clock_in": True})
            ana = await staff_tab(p, p.ana)
            assert (ana["clock_in"], ana["clock_in_from"], ana["clock"]) == (True, "department", "out")
            assert ana["require_clock_in"] == 0 and ana["clock_in_set"] == 0

            # The switch on her row: set by hand, beating the department's.
            assert (await p.admin_call("PATCH", f"/api/staff/{p.ana}",
                                       {"require_clock_in": False}))[0] == 200
            ana = await staff_tab(p, p.ana)
            assert (ana["clock_in"], ana["clock_in_from"], ana["clock"]) == (False, "person", None)
            assert (ana["require_clock_in"], ana["clock_in_set"]) == (0, 1)
            assert p.db.activity(1)[0]["detail"] == "may sign in without clocking in"
            # Use the department's setting.
            assert (await p.admin_call("PATCH", f"/api/staff/{p.ana}", {"clock_in_set": False}))[0] == 200
            ana = await staff_tab(p, p.ana)
            assert (ana["clock_in"], ana["clock_in_from"], ana["clock_in_set"]) == (True, "department", 0)
            assert p.db.activity(1)[0]["detail"] == "clock-in rule from their department"

            # Saving Edit with the switch as it shows changes nothing, here or when she moves to
            # a department that leaves it to her (the switch then follows, untouched).
            front = p.db.create_department("Front Desk")
            for body in ({"name": "Ana", "require_clock_in": True},
                         {"name": "Ana", "department_id": front, "require_clock_in": False},
                         {"name": "Ana", "department_id": house, "require_clock_in": True}):
                assert (await p.admin_call("PATCH", f"/api/staff/{p.ana}", body))[0] == 200
                assert p.db.staff(p.ana)["clock_in_set"] == 0, body
            # Moving her and flipping the switch in the same save: set by hand.
            assert (await p.admin_call("PATCH", f"/api/staff/{p.ana}",
                                       {"department_id": front, "require_clock_in": True}))[0] == 200
            assert (p.db.staff(p.ana)["require_clock_in"], p.db.staff(p.ana)["clock_in_set"]) == (1, 1)
            assert p.db.clock_in_rule(p.db.staff(p.ana)) == (True, "person")

            # Adding someone from Connecteam into a department: the dialog's switch follows the
            # department, so it's only theirs if it differs.
            status, reply = await p.admin_call("POST", "/api/staff", {
                "name": "Eve", "pin": "5555", "connecteam_user_id": "11", "department_id": house,
                "require_clock_in": True})
            eve = p.db.staff(reply["id"])
            assert (eve["clock_in_set"], p.db.clock_in_rule(eve)) == (0, (True, "department"))
            status, reply = await p.admin_call("POST", "/api/staff", {
                "name": "Fay", "pin": "6666", "connecteam_user_id": "12", "department_id": house,
                "require_clock_in": False})
            fay = p.db.staff(reply["id"])
            assert (fay["clock_in_set"], p.db.clock_in_rule(fay)) == (1, (False, "person"))
    run(scenario())


# ---------------------------------------------------------------- signing in

def test_a_department_change_takes_effect_at_the_next_sign_in(tmp_path, monkeypatch):
    async def scenario():
        p = Portal(tmp_path)
        checks = clock(p, monkeypatch, clocked_in={"20"})
        house = department(p, "Housekeeping")
        # Ana (not on the clock) and Bob (on it) are in Housekeeping; neither must clock in.
        p.db.update_staff(p.ana, connecteam_user_id="10", require_clock_in=0, department_id=house)
        p.db.update_staff(p.bob, connecteam_user_id="20", require_clock_in=0, department_id=house)
        async with p:
            assert not main.connecteam_wanted()
            assert (await unlock(p, p.ana))[0] == 200
            assert (await off_clock(p))[p.ana] is False
            assert checks == []

            # Housekeeping: everyone must be clocked in. Ana's next sign-in asks Connecteam once
            # (she may have just clocked in), then is refused; Bob's costs nothing.
            assert (await p.admin_call("PUT", f"/api/departments/{house}", {"clock_in": 1}))[0] == 200
            assert main.connecteam_wanted()
            status, reply = await unlock(p, p.ana)
            assert status == 403 and reply["clock"] == "clocked_out", reply
            assert checks == [True]
            assert ("Ana on Front Desk PC", "Blocked sign-in: not clocked in", False) in p.activity()
            assert (await off_clock(p))[p.ana] is True
            assert (await unlock(p, p.bob))[0] == 200
            assert checks == [True]
            # Signed in before the change: her next control signs her out.
            assert main.clock_block(p.db.staff(p.ana)) == "clocked_out"

            # Her own setting, changed by hand, still wins...
            assert (await p.admin_call("PATCH", f"/api/staff/{p.ana}",
                                       {"require_clock_in": False}))[0] == 200
            assert (await unlock(p, p.ana))[0] == 200
            assert (await off_clock(p))[p.ana] is False
            # ...until she goes back to the department's setting.
            assert (await p.admin_call("PATCH", f"/api/staff/{p.ana}", {"clock_in_set": False}))[0] == 200
            assert (await unlock(p, p.ana))[0] == 403

            # No one in it needs to clock in: beats each person's own (not set by hand).
            p.db.update_staff(p.ana, require_clock_in=1)
            assert (await p.admin_call("PUT", f"/api/departments/{house}", {"clock_in": 0}))[0] == 200
            assert (await unlock(p, p.ana))[0] == 200
            assert not main.connecteam_wanted()     # so the time clock isn't checked for anyone
            # Each person's own again: Ana's says she must.
            assert (await p.admin_call("PUT", f"/api/departments/{house}", {"clock_in": None}))[0] == 200
            assert (await unlock(p, p.ana))[0] == 403
            assert main.connecteam_wanted()
            # Turned off people never need the clock checked.
            p.db.update_staff(p.ana, status="disabled")
            assert not main.connecteam_wanted()
    run(scenario())


# ---------------------------------------------------------------- the Connecteam staff sync

def test_the_staff_sync_never_changes_it(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        connecteam_on(p, connecteam_auto_clock_in="1")
        house = department(p, "Housekeeping", smart_group_id="11", clock_in=0)
        managers = department(p, "Managers", smart_group_id="22")
        users = [ct_user(801, "Eve", code="5555", groups=[11]),
                 ct_user(802, "Gil", code="6666", groups=[11])]
        async with FakeConnecteam(users) as ct, p:
            assert (await sync(p))["added"] == 2
            eve, gil = by_name(p, "Eve"), by_name(p, "Gil")
            # Added with the setting as their own, but Housekeeping says no one needs to.
            assert (eve["require_clock_in"], eve["clock_in_set"]) == (1, 0)
            assert p.db.clock_in_rule(eve) == (False, "department")
            # Gil's set by hand.
            assert (await p.admin_call("PATCH", f"/api/staff/{gil['id']}",
                                       {"require_clock_in": True}))[0] == 200

            # Syncing again, with the setting changed, changes neither department nor person.
            p.db.update_settings({"connecteam_auto_clock_in": "0"})
            await sync(p)
            assert p.db.department(house)["clock_in"] == 0
            eve, gil = p.db.staff(eve["id"]), p.db.staff(gil["id"])
            assert eve["require_clock_in"] == 0      # her default follows the setting...
            assert p.db.clock_in_rule(eve) == (False, "department")   # ...her department wins
            assert (gil["require_clock_in"], gil["clock_in_set"]) == (1, 1)
            assert p.db.clock_in_rule(gil) == (True, "person")

            # Moved to Managers (each person's own) by the smart group: Eve's own default
            # applies there, Gil's own setting still does.
            p.db.update_settings({"connecteam_auto_clock_in": "1"})
            ct.users = [ct_user(801, "Eve", code="5555", groups=[22]),
                        ct_user(802, "Gil", code="6666", groups=[22])]
            await sync(p)
            eve, gil = p.db.staff(eve["id"]), p.db.staff(gil["id"])
            assert eve["department_id"] == gil["department_id"] == managers
            assert p.db.clock_in_rule(eve) == (True, "default")
            assert p.db.clock_in_rule(gil) == (True, "person")
            assert p.db.department(managers)["clock_in"] is None
    run(scenario())


# ---------------------------------------------------------------- backups, exports, imports

def test_backups_exports_and_imports_carry_it(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        house = department(p, "Housekeeping", clock_in=1)
        spa = department(p, "Spa", clock_in=0)
        front = department(p, "Front Desk")
        p.db.update_staff(p.cy, department_id=house, connecteam_user_id="77", require_clock_in=0,
                          clock_in_set=1)
        async with p:
            status, backup = await p.admin_call("GET", "/api/backup")
            assert status == 200
            assert {d["name"]: d["clock_in"] for d in backup["departments"]} == {
                "Housekeeping": 1, "Spa": 0, "Front Desk": None}
            p.db.update_department(house, clock_in=None)
            p.db.update_department(spa, clock_in=1)
            p.db.update_staff(p.cy, clock_in_set=0)
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, reply
            assert [p.db.department(d)["clock_in"] for d in (house, spa, front)] == [1, 0, None]
            assert p.db.clock_in_rule(p.db.staff(p.cy)) == (False, "person")

            # A backup from before the rule: each person's own. A damaged one: the same.
            old = json.loads(json.dumps(backup))
            for row in old["departments"]:
                row.pop("clock_in")
            old["departments"][1]["clock_in"] = "maybe"
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": old})
            assert status == 200, reply
            assert [d["clock_in"] for d in p.db.departments()] == [None, None, None]

            # Exports and imports: the department's rule, and a person's own set by hand.
            status, _ = await p.admin_call("POST", "/api/restore", {"backup": backup})
            status, exported = await p.admin_call("GET", "/api/export/departments")
            assert {d["name"]: d["clock_in"] for d in exported["departments"]} == {
                "Housekeeping": True, "Spa": False, "Front Desk": None}
            staff = transfer.export_staff(p.db, "test")
            cy = next(m for m in staff["staff"] if m["name"] == "Cy")
            assert (cy["require_clock_in"], cy["clock_in_set"]) == (False, True)
            q = Portal(tmp_path / "other")
            async with q:
                status, reply = await q.admin_call("POST", "/api/import/departments",
                                                   {"file": exported, "mode": "skip"})
                assert status == 200 and reply["added"] == 3, reply
                assert {d["name"]: d["clock_in"] for d in q.db.departments()} == {
                    "Housekeeping": 1, "Spa": 0, "Front Desk": None}
                # Replacing from a file from before the rule leaves each one's as it is.
                older = json.loads(json.dumps(exported))
                for row in older["departments"]:
                    row.pop("clock_in")
                older["departments"][0]["clock_in"] = False
                status, reply = await q.admin_call("POST", "/api/import/departments",
                                                   {"file": older, "mode": "replace"})
                assert status == 200 and reply["replaced"] == 3, reply
                assert {d["name"]: d["clock_in"] for d in q.db.departments()} == {
                    "Housekeeping": 0, "Spa": 0, "Front Desk": None}
                # A new person from a staff file keeps their own setting, set by hand.
                q.db.delete_staff(q.cy)
                transfer.import_staff(q.db, staff, "skip")
                cy = by_name(q, "Cy")
                assert (cy["require_clock_in"], cy["clock_in_set"]) == (0, 1)
                assert q.db.clock_in_rule(cy) == (False, "person")
            q.db.conn.close()
    run(scenario())
