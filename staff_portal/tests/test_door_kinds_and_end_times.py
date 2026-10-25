"""Door kinds (public doors and guest rooms) and changing when a day pass stops working: the rules
in wristbands.py, then the dashboard, the staff app, backups, migrations and a card made on the
encoder simulator with a later end time."""

import json
from datetime import datetime, time as clock
from zoneinfo import ZoneInfo

import pytest

from test_wristbands_rules import (DESK, DOORS as RULE_DOORS, LA, NOW, SETTINGS, make, perm,
                                   refused, utc)
from test_wristbands_server import (DOORS, FRONT_DESK, FakeTTHotel, Portal, Simulator, day_pass,
                                    job_until, noon_zone, overnight, run, staff_card, tt_lock,
                                    tthotel_account, waiting_for)

import main  # noqa: E402
import migrate  # noqa: E402
from db import Database  # noqa: E402
from wristbands import NO_ACCESS, clean_permissions, door_kind, options, permission_choice

# Pool Gate and Gym are public doors, the rooms on floors 2 and 3 guest rooms; the second
# building's rooms aren't marked yet.
MARKS = {1: "public", 2: "public", 201: "room", 202: "room", 203: "room", 301: "room"}
MARKED = {i: {**d, "kind": MARKS.get(i)} for i, d in RULE_DOORS.items()}
EDITOR = perm(edit_doors=True)
LATER = perm(edit_end_time=True)
LATE = utc(2025, 10, 15, 3, 30)          # 8:30 PM in Los Angeles, after day passes end (8:00 PM)
LAST_MINUTE = utc(2025, 10, 15, 6, 59) + 30   # 11:59:30 PM: no later time is left today


def stay(who, doors=MARKED, **data):
    return make(who, doors=doors, **{"type": "overnight", "name": "Smith", "count": 1,
                                     "room_ids": [201], "checkout": "2025-10-16", **data})


# ---------------------------------------------------------------- door kinds: the rules

def test_door_kind_values():
    assert [door_kind(v) for v in ("public", "room", None, "", "Room", "suite", 1)] == [
        "public", "room", None, None, None, None, None]
    doors = options(DESK, MARKED, SETTINGS, NOW, LA)["doors"]
    kinds = {d["id"]: d["kind"] for d in doors}
    assert (kinds[1], kinds[201], kinds[2101]) == ("public", "room", None)
    # A damaged kind counts as not marked (offered as both), never as anything else.
    damaged = {**MARKED, 1: {**MARKED[1], "kind": "lobby"}}
    doors = options(DESK, damaged, SETTINGS, NOW, LA)["doors"]
    assert {d["id"]: d["kind"] for d in doors}[1] is None


def test_rooms_must_be_guest_rooms_or_not_marked():
    assert [r.door for r in stay(DESK).records] == [201, 2]
    assert [r.door for r in stay(DESK, room_ids=[2101]).records] == [2101, 2]
    refused(DESK, field="room_ids", doors=MARKED, type="overnight", name="Smith",
            room_ids=[201, 1], checkout="2025-10-16",
            message="Pool Gate is a public door, not a guest room. Pick a guest room.")
    # Before anything is marked, every door can be a room, as before.
    assert [r.door for r in stay(DESK, doors=RULE_DOORS, room_ids=[1]).records] == [1, 2]


def test_shared_doors_must_be_public_doors_or_not_marked():
    result = make(EDITOR, doors=MARKED, type="day_pass", name="A", door_ids=[1, 2101])
    assert [r.door for r in result.records] == [1, 2101]
    refused(EDITOR, field="door_ids", doors=MARKED, type="day_pass", name="A", door_ids=[1, 201],
            message="Room 201 is a guest room, not a public door. Pick a public door.")
    refused(EDITOR, field="door_ids", doors=MARKED, type="overnight", name="Smith",
            room_ids=[202], checkout="2025-10-16", door_ids=[2, 203],
            message="Room 203 is a guest room, not a public door. Pick a public door.")
    assert [r.door for r in stay(EDITOR, door_ids=[1]).records] == [201, 1]


def test_doors_in_the_settings_keep_working_when_marked_the_other_way():
    # Gym is one of the day-pass doors and the overnight shared door, then marked as a room.
    gym_room = {**MARKED, 2: {**MARKED[2], "kind": "room"}}
    assert [r.door for r in make(DESK, doors=gym_room, type="day_pass", name="A").records] == [1, 2]
    assert [r.door for r in stay(DESK, doors=gym_room).records] == [201, 2]
    # Kept by someone who changes the doors too; only a room they add is refused.
    result = make(EDITOR, doors=gym_room, type="day_pass", name="A", door_ids=[2, 2101])
    assert [r.door for r in result.records] == [2, 2101]
    refused(EDITOR, field="door_ids", doors=gym_room, type="day_pass", name="A", door_ids=[2, 202])


def test_staff_cards_open_doors_of_any_kind():
    owner = perm(("staff",), ("door",))
    result = make(owner, doors=MARKED, type="staff", name="Eve", expires_on="2025-10-20",
                  access=[{"level": "door", "door": 1}, {"level": "door", "door": 201}])
    assert [r.door for r in result.records] == [1, 201]


# ---------------------------------------------------------------- day pass end time: the rules

def test_end_time_needs_the_rule():
    refused(DESK, status=403, field="end_time", type="day_pass", name="A", end_time="22:00",
            message="You can't change when day passes stop working.")
    # The usual time, sent as it is, or left out: fine for everyone.
    for end in ("20:00", "", None):
        result = make(DESK, type="day_pass", name="A", end_time=end)
        assert (result.expires_at, result.expires_label) == (utc(2025, 10, 15, 3), "8:00 PM today")


def test_end_time_any_later_time_today():
    late = make(LATER, type="day_pass", name="A", end_time="22:30")
    assert (late.expires_at, late.expires_label) == (utc(2025, 10, 15, 5, 30), "10:30 PM today")
    early = make(LATER, type="day_pass", name="A", end_time="06:00")   # earlier than usual
    assert (early.expires_at, early.expires_label) == (utc(2025, 10, 14, 13), "6:00 AM today")
    last = make(LATER, type="day_pass", name="A", end_time="23:59")
    assert last.expires_label == "11:59 PM today"
    for passed in ("04:59", "05:00", "00:00"):   # it's 5:00 AM
        refused(LATER, field="end_time", type="day_pass", name="A", end_time=passed,
                message="That time has already passed. Pick a later time.")


@pytest.mark.parametrize("bad", ["24:00", "9:00", "21:30:00", "21:60", "noon", 2130, True,
                                 ["21:00"]])
def test_end_time_must_be_a_time(bad):
    refused(LATER, field="end_time", message="Pick a valid time.", type="day_pass", name="A",
            end_time=bad)


def test_end_time_only_for_day_passes():
    # Other cards have their own end dates; an end_time sent with them is ignored.
    assert stay(DESK, doors=RULE_DOORS, end_time="nonsense").expires_label == "11:00 AM Thu, Oct 16"


def test_after_the_usual_end_time():
    usual = "Day passes end at 8:00 PM. It's already past that time."
    refused(DESK, now=LATE, message=usual, type="day_pass", name="A")
    refused(DESK, now=LATE, status=403, field="end_time", type="day_pass", name="A",
            end_time="21:00")
    # With the rule they must pick a later time.
    refused(LATER, now=LATE, field="end_time", message=usual + " Pick a later time.",
            type="day_pass", name="A")
    refused(LATER, now=LATE, field="end_time", type="day_pass", name="A", end_time="20:15")
    result = make(LATER, now=LATE, type="day_pass", name="A", end_time="21:00")
    assert (result.expires_at, result.expires_label) == (utc(2025, 10, 15, 4), "9:00 PM today")
    # A minute before midnight there's no later time left, so it's as for everyone else.
    refused(LATER, now=LAST_MINUTE, message=usual, type="day_pass", name="A")


def test_end_time_on_a_daylight_saving_day():
    # March 9, 2025 in Los Angeles: 2:30 AM doesn't exist, so it moves past the gap.
    now = utc(2025, 3, 9, 9)   # 1:00 AM PST
    result = make(LATER, now=now, type="day_pass", name="A", end_time="02:30")
    assert (result.expires_at, result.expires_label) == (utc(2025, 3, 9, 10, 30), "3:30 AM today")


def test_end_time_in_options():
    day = options(DESK, RULE_DOORS, SETTINGS, NOW, LA)["types"]["day_pass"]
    assert (day["end_time"], day["end_time_passed"], day["can_edit_end_time"]) == (
        "20:00", False, False)
    late = options(DESK, RULE_DOORS, SETTINGS, LATE, LA)["types"]["day_pass"]
    assert (late["allowed"], late["end_time_passed"]) == (False, True)
    # Someone who may pick a later time can still make day passes after the usual time.
    late = options(LATER, RULE_DOORS, SETTINGS, LATE, LA)["types"]["day_pass"]
    assert (late["allowed"], late["reason"]) == (True, None)
    assert (late["end_time_passed"], late["can_edit_end_time"]) == (True, True)
    last = options(LATER, RULE_DOORS, SETTINGS, LAST_MINUTE, LA)["types"]["day_pass"]
    assert last["allowed"] is False and last["reason"].startswith("Day passes end at 8:00 PM")
    custom = options(LATER, RULE_DOORS, {**SETTINGS, "wb_daypass_end": "18:30"}, NOW, LA)
    assert custom["types"]["day_pass"]["end_time"] == "18:30"


def test_end_time_rule_in_permissions():
    assert clean_permissions({"edit_end_time": True})["edit_end_time"] is True
    assert clean_permissions({})["edit_end_time"] is False and NO_ACCESS["edit_end_time"] is False
    with pytest.raises(ValueError):
        clean_permissions({"edit_end_time": "yes"})
    assert permission_choice('{"edit_end_time": "yes"}') == NO_ACCESS


# ---------------------------------------------------------------- door kinds: the add-on

def test_marking_doors_from_the_dashboard(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            status, _ = await p.admin_call("PATCH", "/api/wristbands/doors/101", {"kind": "room"})
            assert status == 200 and p.db.wb_door(101)["kind"] == "room"
            status, reply = await p.admin_call("PATCH", "/api/wristbands/doors/101",
                                               {"kind": "suite"})
            assert status == 400 and reply["error"] and p.db.wb_door(101)["kind"] == "room"
            status, _ = await p.admin_call("PATCH", "/api/wristbands/doors/101", {"kind": None})
            assert status == 200 and p.db.wb_door(101)["kind"] is None
            # A whole floor or building at once.
            status, reply = await p.admin_call("POST", "/api/wristbands/doors/kind",
                                               {"door_ids": [101, 102, 201], "kind": "room"})
            assert (status, reply) == (200, {"changed": 3})
            status, reply = await p.admin_call("POST", "/api/wristbands/doors/kind",
                                               {"door_ids": [900, 901, 900], "kind": "public"})
            assert (status, reply) == (200, {"changed": 2})
            for body in ({"door_ids": [], "kind": "room"}, {"door_ids": ["101"], "kind": "room"},
                         {"door_ids": [True], "kind": "room"}, {"door_ids": 101, "kind": "room"},
                         {"door_ids": [101], "kind": "lobby"},
                         {"door_ids": list(range(1, 5002)), "kind": "room"}):
                status, reply = await p.admin_call("POST", "/api/wristbands/doors/kind", body)
                assert status == 400 and reply["error"], body
            assert (await p.admin_call("POST", "/api/wristbands/doors/kind",
                                       {"door_ids": [555], "kind": "room"}))[1] == {"changed": 0}
            status, doors = await p.admin_call("GET", "/api/wristbands/doors")
            assert {d["id"]: d["kind"] for d in doors["doors"]} == {
                101: "room", 102: "room", 201: "room", 900: "public", 901: "public"}
            actions = [a[1] for a in p.activity()]
            assert {"Marked 3 doors as guest rooms", "Marked 2 doors as public doors",
                    "Marked 0 doors as guest rooms", "Updated door Room 101"} <= set(actions)
            # TT Hotel's door sync never changes a door's kind, even for a door it dropped and
            # lists again.
            p.db.sync_wb_doors([d for d in DOORS if d["lock_id"] != 201])
            p.db.sync_wb_doors(DOORS)
            assert {d["lock_id"]: d["kind"] for d in p.db.wb_doors()}[201] == "room"
            assert p.db.wb_door(900)["kind"] == "public"
            # Unmarking a whole floor.
            status, reply = await p.admin_call("POST", "/api/wristbands/doors/kind",
                                               {"door_ids": [101, 102], "kind": None})
            assert reply == {"changed": 2} and p.db.wb_door(102)["kind"] is None
            assert "Unmarked 2 doors" in [a[1] for a in p.activity()]
    run(scenario())


def test_staff_app_follows_door_kinds(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        p.db.set_wb_door_kinds([101, 102], "room")
        p.db.set_wb_door_kinds([900], "public")
        async with p:
            ana, bob = await p.sign_in(p.ana), await p.sign_in(p.bob)
            status, opts = await p.call("GET", "/api/wristbands", ana)
            assert {d["id"]: d["kind"] for d in opts["doors"]} == {
                101: "room", 102: "room", 201: None, 900: "public", 901: None}
            # Rooms: guest rooms and doors not marked yet; a public door is refused.
            preview = "/api/wristbands/preview"
            assert (await p.call("POST", preview, ana, overnight([101, 201])))[0] == 200
            status, reply = await p.call("POST", preview, ana, overnight([900]))
            assert (status, reply["field"]) == (400, "room_ids")
            assert reply["error"] == ("Pool Gate is a public door, not a guest room. Pick a "
                                      "guest room.")
            # Shared doors: public doors and doors not marked yet; a guest room is refused.
            status, _ = await p.call("POST", preview, bob, day_pass(door_ids=[900, 201]))
            assert status == 200
            status, reply = await p.call("POST", "/api/wristbands/jobs", bob,
                                         day_pass(door_ids=[900, 101]))
            assert (status, reply["field"]) == (400, "door_ids") and "guest room" in reply["error"]
            status, reply = await p.call("POST", "/api/wristbands/preview", bob,
                                         overnight([102], door_ids=[102]))
            assert (status, reply["field"]) == (400, "door_ids")
            # Staff cards open any door.
            status, _ = await p.call("POST", "/api/wristbands/preview", bob,
                                     staff_card([{"level": "door", "door": 101}]))
            assert status == 200
            # Day-pass doors a manager chose before marking them keep working.
            p.db.set_wb_door_kinds([900, 901], "room")
            assert (await p.call("POST", preview, ana, day_pass()))[0] == 200
            assert (await p.call("POST", preview, ana, overnight([101])))[0] == 200
            assert not p.jobs()
    run(scenario())


def test_door_kinds_in_backups_and_migrations(tmp_path):
    async def scenario():
        p = Portal(tmp_path / "old")
        p.db.set_wb_door_kinds([101, 102, 201], "room")
        p.db.set_wb_door_kinds([900], "public")
        async with p:
            status, backup = await p.admin_call("GET", "/api/backup")
            assert status == 200
            kinds = {d["lock_id"]: d["kind"] for d in backup["wristband_doors"]}
            assert kinds == {101: "room", 102: "room", 201: "room", 900: "public", 901: None}
            p.db.set_wb_door_kinds([101, 900, 901], "room")
            p.db.set_wb_door_kinds([102], None)
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, reply
            assert {d["lock_id"]: d["kind"] for d in p.db.wb_doors()} == kinds
            # A backup from before door kinds leaves them as they are; a damaged kind is none.
            old = json.loads(json.dumps(backup))
            for row in old["wristband_doors"]:
                row.pop("kind")
                row["name"] = "Renamed" if row["lock_id"] == 900 else row["name"]
            p.db.set_wb_door_kinds([901], "public")
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": old})
            assert status == 200, reply
            assert p.db.wb_door(900)["name"] == "Renamed"
            assert {d["lock_id"]: d["kind"] for d in p.db.wb_doors()} == {**kinds, 901: "public"}
            damaged = json.loads(json.dumps(backup))
            for row in damaged["wristband_doors"]:
                row["kind"] = "lobby" if row["lock_id"] == 101 else row["kind"]
            assert (await p.admin_call("POST", "/api/restore", {"backup": damaged}))[0] == 200
            assert p.db.wb_door(101)["kind"] is None

        # A migration file carries the whole door list, kinds included.
        p.db.set_wb_door_kinds([101], "room")
        snapshot = json.loads(json.dumps(migrate.make_snapshot(p.db, main.branding, main.VERSION)))
        fresh = Database(str(tmp_path / "new"))
        try:
            snap = migrate.clean_snapshot(json.loads(json.dumps(snapshot)), main.clean_settings,
                                          fresh.settings(), main.branding.clean_logo)
            assert {d["lock_id"]: d["kind"] for d in snap["migrate"]["doors"]}[101] == "room"
            fresh.replace_everything(snap)
            assert {d["lock_id"]: d["kind"] for d in fresh.wb_doors()} == {**kinds, 101: "room"}
            # One made before door kinds: every door not marked yet.
            for row in snapshot["migrate"]["doors"]:
                row.pop("kind")
            for row in snapshot["wristband_doors"]:
                row.pop("kind")
            snap = migrate.clean_snapshot(snapshot, main.clean_settings, fresh.settings(),
                                          main.branding.clean_logo)
            assert {d["kind"] for d in snap["migrate"]["doors"]} == {None}
        finally:
            fresh.conn.close()
    run(scenario())


# ---------------------------------------------------------------- day pass end time: the add-on

def local_time(hhmm: str) -> int:
    """Today at this time in the tests' noon time zone, as the add-on works it out."""
    zone = ZoneInfo(noon_zone())
    hours, minutes = map(int, hhmm.split(":"))
    return int(datetime.combine(datetime.now(zone).date(), clock(hours, minutes), zone).timestamp())


def test_day_pass_end_time_from_the_staff_app(tmp_path):
    async def scenario():
        async with Simulator() as sim, FakeTTHotel([tt_lock(d) for d in DOORS]):
            p = Portal(tmp_path, encoder_url=f"ws://127.0.0.1:{sim._runner.addresses[0][1]}")
            tthotel_account(p)
            p.db.update_staff(p.dee, wristbands=json.dumps({**FRONT_DESK, "edit_end_time": True}))
            async with p:
                ana, dee = await p.sign_in(p.ana), await p.sign_in(p.dee)
                day = (await p.call("GET", "/api/wristbands", ana))[1]["types"]["day_pass"]
                assert (day["can_edit_end_time"], day["end_time"]) == (False, "23:00")
                day = (await p.call("GET", "/api/wristbands", dee))[1]["types"]["day_pass"]
                assert (day["can_edit_end_time"], day["end_time_passed"]) == (True, False)
                # Without the rule: refused, and noted as a refusal.
                for path in ("/api/wristbands/preview", "/api/wristbands/jobs"):
                    status, reply = await p.call("POST", path, ana, day_pass(end_time="23:30"))
                    assert (status, reply["field"]) == (403, "end_time"), reply
                assert ("Ana on Front Desk PC", "Blocked: wristbands", False) in p.activity()
                assert (await p.call("POST", "/api/wristbands/preview", ana,
                                     day_pass(end_time="23:00")))[0] == 200
                # With it: any later time today. It's about noon where these tests run.
                status, reply = await p.call("POST", "/api/wristbands/preview", dee,
                                             day_pass(end_time="11:00"))
                assert (status, reply["field"]) == (400, "end_time")
                status, reply = await p.call("POST", "/api/wristbands/preview", dee,
                                             day_pass(end_time="23:30"))
                assert status == 200 and reply["plan"]["expires_label"] == "11:30 PM today"
                assert reply["plan"]["expires_at"] == local_time("23:30")

                # The job, the card log, the activity log and the card itself use that time.
                status, reply = await p.call("POST", "/api/wristbands/jobs", dee,
                                             day_pass(count=1, end_time="23:30"))
                assert status == 200, reply
                job_id = reply["job"]["id"]
                assert reply["job"]["expires_label"] == "11:30 PM today"
                await job_until(p, dee, job_id, waiting_for(1))
                card = sim.place_card()
                await job_until(p, dee, job_id, lambda j: j["status"] == "done")
                assert p.db.wb_job(job_id)["expires_at"] == local_time("23:30")
                assert {r["endDate"] for r in sim.cards[card]["records"]} == {local_time("23:30")}
                log = (await p.call("GET", "/api/wristbands/log", dee))[1]["jobs"]
                assert [(j["id"], j["expires_at"]) for j in log] == [(job_id, local_time("23:30"))]
                made = next(a for a in p.db.activity(50) if a["action"].startswith("Made"))
                assert made["detail"].endswith("until 11:30 PM today")
                sim.remove_card()
    run(scenario())


def test_day_passes_after_the_usual_end_time(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        p.db.update_settings({"wb_daypass_end": "00:00"})   # long gone by noon
        p.db.update_staff(p.dee, wristbands=json.dumps({**FRONT_DESK, "edit_end_time": True}))
        async with p:
            ana, dee = await p.sign_in(p.ana), await p.sign_in(p.dee)
            day = (await p.call("GET", "/api/wristbands", ana))[1]["types"]["day_pass"]
            assert day["allowed"] is False and "already past" in day["reason"]
            day = (await p.call("GET", "/api/wristbands", dee))[1]["types"]["day_pass"]
            assert (day["allowed"], day["end_time_passed"]) == (True, True)
            status, reply = await p.call("POST", "/api/wristbands/preview", dee, day_pass())
            assert (status, reply["field"]) == (400, "end_time")
            assert "Pick a later time" in reply["error"]
            status, reply = await p.call("POST", "/api/wristbands/preview", dee,
                                         day_pass(end_time="22:00"))
            assert status == 200 and reply["plan"]["expires_at"] == local_time("22:00")
            status, reply = await p.call("POST", "/api/wristbands/preview", ana, day_pass())
            assert status == 400 and reply["error"] == ("Day passes end at 12:00 AM. It's already "
                                                        "past that time.")
    run(scenario())


def test_role_editor_keeps_the_end_time_rule(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            status, _ = await p.admin_call("PUT", f"/api/roles/{p.no_cards}", {
                "name": "Housekeeping", "entities": {},
                "wristbands": {**FRONT_DESK, "edit_end_time": True}})
            assert status == 200
            assert p.db.role(p.no_cards)["wristbands"]["edit_end_time"] is True
            status, _ = await p.admin_call("PATCH", f"/api/staff/{p.cy}", {
                "wristbands": {**FRONT_DESK, "edit_end_time": True}})
            assert status == 200
            assert json.loads(p.db.staff(p.cy)["wristbands"])["edit_end_time"] is True
            status, reply = await p.admin_call("PATCH", f"/api/staff/{p.cy}", {
                "wristbands": {**FRONT_DESK, "edit_end_time": "yes"}})
            assert status == 400 and reply["error"]
    run(scenario())
