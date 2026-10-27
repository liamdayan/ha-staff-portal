"""Link existing staff (and Relink everyone), the person dialog's Connecteam link, departments
made from smart groups, and linked people following their smart group's department.

Nothing here talks to a real Connecteam or Home Assistant: Connecteam is the small fake from
test_staffcards_server, and Home Assistant's admin check is replaced as in test_encoder_dashboard.
"""

from test_encoder_dashboard import PAT, VIC, as_user, call, dashboard_portal, ingress_client
from test_staffcards_server import FakeConnecteam, by_name, connecteam_on, ct_user, department, sync
from test_wristbands_server import Portal, run

import ctlink  # noqa: E402
import main  # noqa: E402
from db import Database, check_pin  # noqa: E402

GROUPS = [{"id": 11, "name": "Housekeeping"}, {"id": 22, "name": "Managers"}]


def linked_on(p: Portal, **extra):
    """Connecteam on, without adding everyone (the owner links people by hand)."""
    connecteam_on(p, connecteam_auto_staff="0", connecteam_auto_departments="0", **extra)


async def links(p: Portal) -> dict:
    status, plan = await p.admin_call("GET", "/api/connecteam/links")
    assert status == 200, plan
    return plan


def row(plan: dict, staff_id: int) -> dict:
    return next(m for m in plan["staff"] if m["id"] == staff_id)


# ---------------------------------------------------------------- matching

def test_names_are_matched_carefully():
    assert ctlink.name_tokens("  José O'Neil-Ryan (2) ") == ("jose", "oneil", "ryan")
    level = ctlink.match_level
    assert level("Ana Lopez", "ana  LÓPEZ") == ctlink.SAME
    assert level("Lopez Ana", "Ana Lopez") == ctlink.SWAPPED
    assert level("Bob Smith", "Robert Smith") == ctlink.NICKNAME
    assert level("Chris Smith", "Christopher Smith") == ctlink.NICKNAME
    assert level("Bob", "Robert Smith") == ctlink.FIRST_NAME
    assert level("Bob Smith", "Robert Jones") is None
    assert level("Al Smith", "Alan Smith") is None          # two letters is too short to tell
    assert level("", "Ana") is None
    assert ctlink.same_name("Smith, John", "John Smith") and not ctlink.same_name("Jon Smith", "John Smith")

    staff = [{"id": 1, "name": "Ana Lopez"}, {"id": 2, "name": "Chris"}, {"id": 3, "name": "Bob Smith"},
             {"id": 4, "name": "Robert Smith"}, {"id": 5, "name": "Dee"}]
    users = [{"id": "a", "name": "Ana Lopez"}, {"id": "c1", "name": "Christopher Hale"},
             {"id": "c2", "name": "Christine Hale"}, {"id": "r", "name": "Robert Smith"},
             {"id": "d", "name": "Dee Dee"}, {"id": "d2", "name": "Dee Ward"}]
    found = {sid: (u["id"], lvl) for sid, (u, lvl) in ctlink.suggest(staff, users).items()}
    # Chris matches two people, and so does Dee: no suggestion. Robert Smith is the best match
    # for Robert Smith, so Bob Smith (a weaker match for the same user) gets none.
    assert found == {1: ("a", ctlink.SAME), 4: ("r", ctlink.SAME)}
    # Two people matching one user equally well: neither.
    twins = [{"id": 1, "name": "Ana Lopez"}, {"id": 2, "name": "Lopez Ana"}]
    assert ctlink.suggest(twins, [{"id": "a", "name": "Ana Lopez"}]) == {1: ({"id": "a", "name": "Ana Lopez"}, 1)}
    assert ctlink.suggest([{"id": 1, "name": "Ana"}, {"id": 2, "name": "ana"}],
                          [{"id": "a", "name": "Ana Lopez"}]) == {}


# ---------------------------------------------------------------- Link existing staff

def test_link_existing_staff(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        linked_on(p)
        house = department(p, "Housekeeping", smart_group_id="11", clock_in=1)
        p.db.update_staff(p.cy, department_id=None)
        archived = p.person("Eve", "5151", p.front_desk)
        p.db.archive_staff(archived, "Admin")
        users = [ct_user(701, "Ana", "Lopez", "7001", [11]), ct_user(702, "Robert", "Jones", "7002"),
                 ct_user(703, "Cy", "Moss", "2222", [11]), ct_user(704, "Eve", code="7004"),
                 ct_user(705, "Dee", "Ward", "7005")]
        async with FakeConnecteam(users, GROUPS) as ct, p:
            # Dee is linked already (705). Eve is archived: never offered, never matched.
            p.db.update_staff(p.dee, connecteam_user_id="705")
            plan = await links(p)
            assert ct.count("/users/v1/users") == 1
            assert [m["name"] for m in plan["staff"]] == ["Ana", "Bob", "Cy", "Dee"]
            assert row(plan, p.ana)["suggested"] == {"id": "701", "name": "Ana Lopez", "why": "Same first name"}
            assert row(plan, p.bob)["suggested"]["id"] == "702"     # Bob: Robert
            assert row(plan, p.cy)["suggested"]["id"] == "703"
            assert row(plan, p.dee)["suggested"] is None              # linked already
            assert next(u for u in plan["users"] if u["id"] == "705")["linked_to"] == p.dee
            assert next(u for u in plan["users"] if u["id"] == "701")["department_id"] == house
            # The step reuses the cached user list.
            await links(p)
            assert ct.count("/users/v1/users") == 1

            # Ana and Cy (Bob: Don't link), with Cy's kiosk code as PIN, which is Bob's: kept.
            before = {sid: p.db.staff(sid) for sid in (p.ana, p.bob, p.cy)}
            status, reply = await p.admin_call("POST", "/api/connecteam/links", {
                "links": [{"staff_id": p.ana, "connecteam_user_id": "701"},
                          {"staff_id": p.cy, "connecteam_user_id": "703"}],
                "use_kiosk_code": True})
            assert status == 200, reply
            assert reply == {"changed": 2, "pins": 1, "pins_kept": ["Cy"]}
            assert ct.count("/users/v1/users") == 1
            ana, cy, bob = p.db.staff(p.ana), p.db.staff(p.cy), p.db.staff(p.bob)
            assert ana["connecteam_user_id"] == "701" and cy["connecteam_user_id"] == "703"
            assert bob == before[p.bob]
            assert check_pin("7001", ana["pin_hash"]) and ana["pin_from_connecteam"] == 1
            assert check_pin("3333", cy["pin_hash"])                   # kept: 2222 is Bob's
            # Role kept; department from the smart group; their clock-in rule doesn't switch on
            # although Housekeeping requires it: set for them, so it beats the department's.
            for m, was in ((ana, before[p.ana]), (cy, before[p.cy])):
                assert m["role_id"] == was["role_id"] and m["department_id"] == house
                assert m["connecteam_auto"] == 0 and m["department_set"] == 0
                assert p.db.clock_in_rule(m) == (False, "person")
                assert not main.needs_clock_in(m)
            assert ("Admin", "Linked Ana to Connecteam", True) in p.activity()
            detail = next(a["detail"] for a in p.db.activity(20) if a["action"] == "Linked Ana to Connecteam")
            assert detail == ("Connecteam user Ana Lopez; PIN set to their kiosk code; department: "
                              "Housekeeping; may sign in without clocking in")

            # Bob with the clock-in tick, keeping his department.
            status, _ = await p.admin_call("POST", "/api/connecteam/links", {
                "links": [{"staff_id": p.bob, "connecteam_user_id": "702"}],
                "follow_department": False, "require_clock_in": True})
            bob = p.db.staff(p.bob)
            assert status == 200 and bob["department_set"] == 1 and bob["department_id"] is None
            assert p.db.clock_in_rule(bob)[0] and main.needs_clock_in(bob)
            # Nothing left to link; Relink everyone has suggestions for everyone.
            plan = await links(p)
            assert all(m["suggested"] is None for m in plan["staff"])
            assert row(plan, p.ana)["relink"]["id"] == "701"
    run(scenario())


def test_one_connecteam_user_one_person(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        linked_on(p)
        users = [ct_user(711, "Ana", code="7111"), ct_user(712, "Bob", code="7112")]
        async with FakeConnecteam(users) as ct, p:
            p.db.update_staff(p.ana, connecteam_user_id="711")
            archived = p.person("Eve", "5151", p.front_desk)
            p.db.update_staff(archived, connecteam_user_id="712")
            p.db.archive_staff(archived, "Admin")
            post = lambda body: p.admin_call("POST", "/api/connecteam/links", body)
            # Someone else's user (archived people's too), twice in one batch, unknown users.
            status, reply = await post({"links": [{"staff_id": p.bob, "connecteam_user_id": "711"}]})
            assert status == 400 and "linked to both Bob and Ana" in reply["error"]
            status, reply = await post({"links": [{"staff_id": p.cy, "connecteam_user_id": "712"}]})
            assert status == 400 and "Eve" in reply["error"]
            status, _ = await post({"links": [{"staff_id": p.bob, "connecteam_user_id": "999"}]})
            assert status == 400
            status, _ = await post({"links": [{"staff_id": p.bob, "connecteam_user_id": "711"},
                                              {"staff_id": p.bob, "connecteam_user_id": None}]})
            assert status == 400
            status, _ = await post({"links": [{"staff_id": archived, "connecteam_user_id": None}]})
            assert status == 400                                   # archived: restore first
            assert p.db.staff(p.bob)["connecteam_user_id"] is None
            # A swap, in one go.
            p.db.update_staff(p.bob, connecteam_user_id="999")
            ct.users = users + [ct_user(999, "Zed")]
            main.connecteam._users = None
            status, reply = await post({"links": [{"staff_id": p.ana, "connecteam_user_id": "999"},
                                                  {"staff_id": p.bob, "connecteam_user_id": "711"},
                                                  {"staff_id": p.cy, "connecteam_user_id": None}]})
            assert status == 200 and reply["changed"] == 2              # Cy: already so
            assert p.db.staff(p.ana)["connecteam_user_id"] == "999"
            assert p.db.staff(p.bob)["connecteam_user_id"] == "711"
            assert ("Admin", "Changed Ana's Connecteam link", True) in p.activity()
            # The person dialog: someone else's user is refused; changing and unlinking log.
            status, reply = await p.admin_call("PATCH", f"/api/staff/{p.cy}", {"connecteam_user_id": "711"})
            assert status == 400 and "already linked to Bob" in str(reply)
            assert (await p.admin_call("PATCH", f"/api/staff/{p.bob}", {"connecteam_user_id": None}))[0] == 200
            assert ("Admin", "Unlinked Bob from Connecteam", True) in p.activity()
            assert (await p.admin_call("PATCH", f"/api/staff/{p.cy}", {"connecteam_user_id": "711"}))[0] == 200
            cy = p.db.staff(p.cy)
            assert cy["connecteam_user_id"] == "711" and not p.db.clock_in_rule(cy)[0]
            log = next(a for a in p.db.activity(20) if a["action"] == "Linked Cy to Connecteam")
            assert log["detail"] == "Connecteam user Ana"
    run(scenario())


def test_relink_everyone_shows_broken_links(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        linked_on(p)
        users = [ct_user(721, "Ana", code="7211"), ct_user(722, "Bob", code="7212")]
        async with FakeConnecteam(users), p:
            # Ana and Bob were linked the wrong way round; Cy to someone who's gone.
            p.db.update_staff(p.ana, connecteam_user_id="722")
            p.db.update_staff(p.bob, connecteam_user_id="721")
            p.db.update_staff(p.cy, connecteam_user_id="799")
            plan = await links(p)
            ana, bob, cy = row(plan, p.ana), row(plan, p.bob), row(plan, p.cy)
            assert (ana["link_name"], ana["relink"]["id"], ana["broken"]) == ("Bob", "721", False)
            assert bob["relink"]["id"] == "722"
            assert cy["broken"] and cy["link_name"] is None and cy["relink"] is None
            status, _ = await p.admin_call("POST", "/api/connecteam/links", {"links": [
                {"staff_id": p.ana, "connecteam_user_id": "721"},
                {"staff_id": p.bob, "connecteam_user_id": "722"},
                {"staff_id": p.cy, "connecteam_user_id": None}]})
            assert status == 200
            assert [p.db.staff(s)["connecteam_user_id"] for s in (p.ana, p.bob, p.cy)] == ["721", "722", None]
            log = {a["action"]: a["detail"] for a in p.db.activity(20)}
            assert log["Changed Ana's Connecteam link"] == "From Bob to Ana"
            assert log["Unlinked Cy from Connecteam"] == "Was Connecteam user 799 (no longer in Connecteam)"
    run(scenario())


def test_only_administrators_link(tmp_path, monkeypatch):
    async def scenario():
        p = dashboard_portal(tmp_path, monkeypatch)
        linked_on(p)
        async with FakeConnecteam([ct_user(731, "Ana")]) as ct, p:
            client = await ingress_client()
            try:
                body = {"links": [{"staff_id": p.ana, "connecteam_user_id": "731"}]}
                for method, path, data in (("GET", "/api/connecteam/links", None),
                                           ("POST", "/api/connecteam/links", body),
                                           ("POST", "/api/connecteam/departments", {})):
                    status, _ = await call(client, method, path, as_user(VIC, "Vic"), data)
                    assert status == 403
                status, _ = await call(client, "POST", "/api/connecteam/links", as_user(PAT, dashboard=False), body)
                assert status == 403                               # not from the dashboard
                assert ct.calls == [] and p.db.staff(p.ana)["connecteam_user_id"] is None
                status, _ = await call(client, "POST", "/api/connecteam/links", as_user(PAT), body)
                assert status == 200 and p.db.staff(p.ana)["connecteam_user_id"] == "731"
                assert ("Pat Owner", "Linked Ana to Connecteam", True) in p.activity()
            finally:
                await client.close()
            # The staff app has no such route, and Connecteam off refuses.
            resp = await p.staff.get("/api/connecteam/links")
            assert resp.status in (401, 403, 404)
            p.db.update_settings({"connecteam_enabled": "0"})
            assert (await p.admin_call("GET", "/api/connecteam/links"))[0] == 400
    run(scenario())


# ---------------------------------------------------------------- the sync and linked people

def test_add_everyone_never_duplicates_someone_here(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        connecteam_on(p, connecteam_auto_clock_in="1", connecteam_auto_departments="0")
        house = department(p, "Housekeeping", smart_group_id="11")
        gone = p.person("Finn", "6161", p.front_desk)
        p.db.update_staff(p.cy, name="Cy Moss")
        p.db.archive_staff(gone, "Admin")
        users = [ct_user(741, "Bob", code="7411", groups=[11]), ct_user(742, "Moss", "Cy", "7412"),
                 ct_user(743, "Finn", code="7413"), ct_user(744, "Gil", code="7414")]
        async with FakeConnecteam(users) as ct, p:
            counts = await sync(p)
            # Bob and Cy (swapped) wait to be linked; archived Finn is never matched, so the
            # Connecteam Finn is added beside him.
            assert (counts["added"], counts["waiting"]) == (2, 2)
            names = sorted(m["name"] for m in p.db.staff_list())
            assert "Bob (2)" not in names and "Finn (2)" in names and "Gil" in names
            assert p.db.staff(p.bob)["connecteam_user_id"] is None
            plan = await links(p)
            assert row(plan, p.bob)["suggested"]["id"] == "741"
            assert all(m["id"] != gone for m in plan["staff"])
            # Linked by the owner: the sync then follows the rules for people added by hand.
            await p.admin_call("POST", "/api/connecteam/links", {"links": [
                {"staff_id": p.bob, "connecteam_user_id": "741"}]})
            bob = p.db.staff(p.bob)
            assert bob["department_id"] == house and not p.db.clock_in_rule(bob)[0]
            counts = await sync(p)
            assert (counts["added"], counts["waiting"]) == (0, 1)              # Cy still waits
            # A new kiosk code, another group, the clock-in setting: only the department follows.
            p.db.update_settings({"connecteam_auto_clock_in": "0"})
            p.db.update_settings({"connecteam_auto_clock_in": "1"})
            managers = department(p, "Managers", smart_group_id="22")
            ct.users = [ct_user(741, "Bob", code="9999", groups=[22])] + users[1:]
            await sync(p)
            after = p.db.staff(p.bob)
            assert after["department_id"] == managers and after["pin_hash"] == bob["pin_hash"]
            assert (after["require_clock_in"], after["role_id"], after["connecteam_auto"]) == (
                bob["require_clock_in"], bob["role_id"], 0)
            assert ("Staff Portal", "Moved Bob to Managers", True) in p.activity() or (
                "Admin", "Moved Bob to Managers", True) in p.activity()
            # Archived or removed in Connecteam: people added by hand are never turned off.
            ct.users = users[1:]
            assert (await sync(p))["turned_off"] == 0
            assert p.db.staff(p.bob)["status"] == "active"
            # Archived here: never matched, never moved, never brought back.
            p.db.archive_staff(p.bob, "Admin")
            ct.users = [ct_user(741, "Bob", code="9999", groups=[11])] + users[1:]
            counts = await sync(p)
            assert p.db.staff(p.bob)["department_id"] == managers
            assert p.db.staff(p.bob)["status"] == "archived" and counts["added"] == 0
    run(scenario())


def test_departments_follow_the_smart_group_unless_set_by_hand(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        linked_on(p)
        house = department(p, "Housekeeping", smart_group_id="11", clock_in=1)
        managers = department(p, "Managers", smart_group_id="22")
        users = [ct_user(751, "Ana", groups=[22, 11])]          # Housekeeping comes first
        async with FakeConnecteam(users) as ct, p:
            p.db.update_staff(p.ana, connecteam_user_id="751", require_clock_in=0)
            assert main.connecteam_sync_wanted() and main.staff_sync_due()
            await sync(p)
            ana = p.db.staff(p.ana)
            assert ana["department_id"] == house
            log = next(a for a in p.db.activity(20) if a["action"] == "Moved Ana to Housekeeping")
            assert log["detail"] == ("their Connecteam smart group; now must be clocked in to sign "
                                     "in (Housekeeping's rule)")
            # By hand on the Staff tab: kept; Follow Connecteam undoes it.
            assert (await p.admin_call("PATCH", f"/api/staff/{p.ana}", {"department_id": managers}))[0] == 200
            await sync(p)
            assert p.db.staff(p.ana)["department_id"] == managers
            assert (await p.admin_call("PATCH", f"/api/staff/{p.ana}", {"department_set": False}))[0] == 200
            assert p.db.staff(p.ana)["department_id"] == house
            # No matching group: left as is. Unlinked: nothing follows Connecteam.
            ct.users = [ct_user(751, "Ana")]
            await sync(p)
            assert p.db.staff(p.ana)["department_id"] == house
            await p.admin_call("PATCH", f"/api/staff/{p.ana}", {"connecteam_user_id": None})
            assert not main.connecteam_sync_wanted()
    run(scenario())


# ---------------------------------------------------------------- departments from smart groups

def test_departments_from_smart_groups(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        connecteam_on(p, connecteam_auto_staff="0")
        assert p.db.settings()["connecteam_auto_departments"] == "1"   # on for new setups
        spa = department(p, "Spa")                                    # by hand, same name as a group
        groups = GROUPS + [{"id": 33, "name": "Spa"}]
        async with FakeConnecteam([ct_user(761, "Ana", groups=[11])], groups) as ct, p:
            p.db.update_staff(p.ana, connecteam_user_id="761")
            counts = await sync(p)
            assert (counts["departments_added"], counts["departments_renamed"]) == (2, 0)
            depts = {d["name"]: d for d in p.db.departments()}
            house = depts["Housekeeping"]
            assert (house["smart_group_id"], house["role_id"], house["card_access"], house["clock_in"],
                    house["group_named"]) == ("11", None, None, None, 1)
            assert depts["Spa"]["id"] == spa and depts["Spa"]["smart_group_id"] == "33"
            assert p.db.staff(p.ana)["department_id"] == house["id"]
            assert ("Admin", "Made department Housekeeping from a Connecteam smart group", True) in p.activity()
            # The smart group list is cached: the next sync asks for users only.
            await sync(p)
            assert ct.count("/users/v1/smart-groups") == 1
            # Renamed in Connecteam: followed, unless renamed here. Removed: kept and marked.
            await p.admin_call("PUT", f"/api/departments/{depts['Managers']['id']}", {"name": "Bosses"})
            ct.groups = [{"id": 11, "name": "Rooms"}, {"id": 22, "name": "Leaders"}]
            main.connecteam._groups = None
            counts = await sync(p)
            assert counts["departments_renamed"] == 1
            depts = {d["id"]: d for d in p.db.departments()}
            assert depts[house["id"]]["name"] == "Rooms"
            assert depts[spa]["name"] == "Spa" and depts[spa]["group_gone"] == 1
            assert {d["name"] for d in depts.values()} == {"Rooms", "Bosses", "Spa"}
            assert ("Admin", "Renamed department Housekeeping to Rooms", True) in p.activity()
            # An empty list never marks every group gone.
            ct.groups = []
            main.connecteam._groups = None
            await sync(p)
            assert p.db.department(house["id"])["group_gone"] == 0
            # Add departments now: at once, then at most once a minute.
            ct.groups = [{"id": 11, "name": "Rooms"}, {"id": 44, "name": "Kitchen"}]
            main.connecteam._groups = None
            status, reply = await p.admin_call("POST", "/api/connecteam/departments", {})
            assert status == 200 and reply["departments_added"] == 1
            assert (await p.admin_call("POST", "/api/connecteam/departments", {}))[0] == 429
            # Turned off: nothing is made.
            main.departments_synced[0] = 0.0
            p.db.update_settings({"connecteam_auto_departments": "0"})
            assert (await p.admin_call("POST", "/api/connecteam/departments", {}))[0] == 400
    run(scenario())


def test_existing_setups_keep_departments_by_hand(tmp_path):
    db = Database(str(tmp_path))
    db.conn.execute("DELETE FROM settings WHERE key = 'connecteam_auto_departments'")
    db.conn.commit()
    db.close()
    assert Database(str(tmp_path)).settings()["connecteam_auto_departments"] == "0"
    assert Database(str(tmp_path / "new")).settings()["connecteam_auto_departments"] == "1"
