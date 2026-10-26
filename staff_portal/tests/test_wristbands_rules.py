"""wristbands.py: permissions, door scope, every reason a card request is refused, the options
the Wristbands screen gets, and expiry times (including daylight-saving days)."""

import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import wristbands
from wristbands import (BLANK_MAC, NO_ACCESS, NO_DAY_PASS_DOORS, TOO_MANY_DOORS, PlanError,
                        Record, at_local_time, clean_permissions, door_in_scope, format_expiry,
                        options, permission_choice, plan)

LA = ZoneInfo("America/Los_Angeles")


def utc(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())


NOW = utc(2025, 10, 14, 12)   # 5:00 AM Tuesday in Los Angeles


def perm(types=("day_pass", "overnight"), levels=(), doors=None, **changes):
    value = {"types": list(types), "staff_levels": list(levels),
             "doors": doors if doors is not None else {"all": True}, "edit_doors": False,
             "edit_end_time": False, "max_nights": 0, "max_staff_days": 0, "see_all_cards": False}
    value.update(changes)
    return value


DESK = perm()
OWNER = perm(("day_pass", "overnight", "staff"), ("door", "floor", "building", "all"),
             edit_doors=True, see_all_cards=True)
STAFF_ALL = perm(("staff",), ("door", "floor", "building"))
B1 = {"buildings": [1], "floors": [], "doors": []}
B2 = {"buildings": [2], "floors": [], "doors": []}


def mac(lock_id):
    return f"C540E0{lock_id:06X}"


def door(lock_id, name, building=1, floor=1, *, building_name="", floor_name="", hidden=False,
         active=True, lock_mac=None):
    return {"lock_id": lock_id, "tt_name": name, "alias": "", "name": "",
            "mac": lock_mac or mac(lock_id), "lock_type": 1, "building_number": building,
            "building_name": building_name, "floor_number": floor, "floor_name": floor_name,
            "battery": 80, "has_gateway": 0, "hidden": int(hidden), "active": int(active),
            "synced_at": 0}


DOORS = {d["lock_id"]: d for d in [
    door(1, "Pool Gate", 1, 1, building_name="Main", floor_name="Lobby"),
    door(2, "Gym", 1, 1, building_name="Main", floor_name="Lobby"),
    door(3, "Staff Room", 1, 1, hidden=True),
    door(4, "Old Door", 1, 1, active=False),
    door(5, "Shed", None, None),
    door(6, "Odd Lock", 1, 1, lock_mac="not-a-mac"),
    door(7, "Annex", 1, None),
    *[door(i, f"Room {i}", 1, 2) for i in (201, 202, 203)],
    door(301, "Room 301", 1, 3),
    door(2101, "Room 2101", 2, 1),
    door(2201, "Room 2201", 2, 2),
]}
SETTINGS = {"wristbands_enabled": "1", "wb_daypass_end": "20:00", "wb_daypass_doors": "[1, 2]",
            "wb_daypass_max": "20", "wb_checkout_time": "11:00", "wb_overnight_doors": "[2]",
            "wb_overnight_max": "20", "wb_staff_days": "90", "wb_staff_max": "5",
            "wb_lockout_daypass": "0", "wb_lockout_overnight": "0", "wb_lockout_staff": "1",
            "wb_max_records": "20"}


def make(who, now=NOW, settings=None, doors=None, **data):
    return plan(data, who, DOORS if doors is None else doors,
                SETTINGS if settings is None else {**SETTINGS, **settings}, now, LA)


def refused(who, *, status=400, field=None, message=None, now=NOW, settings=None, **data):
    with pytest.raises(PlanError) as info:
        make(who, now, settings, **data)
    err = info.value
    assert (err.status, err.field) == (status, field), err.message
    if message is not None:
        assert err.message == message
    return err


def rec(lock_id, label, building=1, floor=1):
    return Record("door", building, floor, mac(lock_id), lock_id, label)


# ---------------------------------------------------------------- permissions

def test_clean_permissions_tidies_and_fills_in():
    assert clean_permissions(None) is None
    clean = clean_permissions({"types": ["staff", "day_pass"], "staff_levels": ["all", "door"],
                               "doors": {"buildings": [2, 1, 2], "floors": [[1, 3], [3, 1]],
                                         "doors": ["7", 5]},
                               "edit_doors": True, "max_nights": 3, "unknown": "dropped"})
    assert clean == {"types": ["day_pass", "staff"], "staff_levels": ["door", "all"],
                     "doors": {"buildings": [1, 2], "floors": [[3, 1]], "doors": [5, 7]},
                     "edit_doors": True, "edit_end_time": False, "max_nights": 3,
                     "max_staff_days": 0, "see_all_cards": False}
    assert clean_permissions({"doors": {"all": True, "buildings": [1]}})["doors"] == {"all": True}
    assert clean_permissions({}) == NO_ACCESS


@pytest.mark.parametrize("raw", [
    "day_pass", [], {"types": ["vip"]}, {"types": "day_pass"}, {"staff_levels": ["master"]},
    {"doors": []}, {"doors": {"buildings": [255]}}, {"doors": {"buildings": [-1]}},
    {"doors": {"floors": [[1]]}}, {"doors": {"floors": [[1, 256]]}}, {"doors": {"doors": [0]}},
    {"doors": {"doors": [1.5]}}, {"doors": {"doors": list(range(1, 6000))}},
    {"edit_doors": "yes"}, {"max_nights": -1}, {"max_nights": 4000}, {"max_staff_days": "x"},
    {"see_all_cards": 2},
])
def test_clean_permissions_refuses(raw):
    with pytest.raises(ValueError):
        clean_permissions(raw)


def test_permission_choice_fails_closed():
    assert permission_choice(None) is None and permission_choice("") is None
    assert permission_choice(json.dumps(OWNER)) == OWNER
    assert permission_choice(OWNER) == OWNER
    for damaged in ("{", "null", "[]", '{"types": ["vip"]}', '{"doors": "all"}', 7):
        assert permission_choice(damaged) == NO_ACCESS
    nothing = permission_choice("{")
    nothing["types"].append("staff")   # a copy: the constant can't be changed through it
    assert NO_ACCESS["types"] == []


# (permission doors, door 1 in building 1 floor 1, door 201 on 1/2, door 2101 in building 2)
SCOPE_MATRIX = [
    ({"all": True}, True, True, True),
    (B1, True, True, False),
    ({"buildings": [], "floors": [[1, 2]], "doors": []}, False, True, False),
    ({"buildings": [], "floors": [], "doors": [201, 2101]}, False, True, True),
    ({"buildings": [], "floors": [], "doors": []}, False, False, False),
    ({}, False, False, False),
    ("all", False, False, False),
]


@pytest.mark.parametrize("scope,first,room,other", SCOPE_MATRIX)
def test_door_in_scope_matrix(scope, first, room, other):
    who = {"doors": scope}
    assert [door_in_scope(who, DOORS[i]) for i in (1, 201, 2101)] == [first, room, other]


def test_door_in_scope_needs_a_place_for_building_and_floor_scopes():
    assert not door_in_scope({"doors": B1}, DOORS[5])               # no building number
    assert door_in_scope({"doors": {"doors": [5]}}, DOORS[5])        # but listed by itself
    assert not door_in_scope({"doors": {"floors": [[1, 1]]}}, DOORS[7])
    assert not door_in_scope(None, DOORS[1]) and not door_in_scope(NO_ACCESS, DOORS[1])


# ---------------------------------------------------------------- reading the request

@pytest.mark.parametrize("data,field", [
    ({"type": "vip"}, "type"),
    ({"type": "day_pass", "name": 5}, "name"),
    ({"type": "day_pass", "count": "4"}, "count"),
    ({"type": "day_pass", "count": True}, "count"),
    ({"type": "day_pass", "count": 2.0}, "count"),
    ({"type": "day_pass", "door_ids": "1,2"}, "door_ids"),
    ({"type": "day_pass", "door_ids": [0]}, "door_ids"),
    ({"type": "day_pass", "door_ids": [True]}, "door_ids"),
    ({"type": "day_pass", "door_ids": list(range(1, 300))}, "door_ids"),
    ({"type": "overnight", "room_ids": [1.5]}, "room_ids"),
    ({"type": "overnight", "checkout": "10/16/2025"}, "checkout"),
    ({"type": "overnight", "checkout": "2025-02-30"}, "checkout"),
    ({"type": "overnight", "checkout": 20251016}, "checkout"),
    ({"type": "staff", "access": {"level": "all"}}, "access"),
    ({"type": "staff", "access": ["all"]}, "access"),
    ({"type": "staff", "access": [{"level": "wing"}]}, "access"),
    ({"type": "staff", "access": [{"level": "master"}]}, "access"),
    ({"type": "staff", "access": [{"level": "building"}]}, "access"),
    # Zero would turn a building record into an all-doors one (and a floor into a building).
    ({"type": "staff", "access": [{"level": "building", "building": 0}]}, "access"),
    ({"type": "staff", "access": [{"level": "floor", "building": 1, "floor": 0}]}, "access"),
    ({"type": "staff", "access": [{"level": "building", "building": 255}]}, "access"),
    ({"type": "staff", "access": [{"level": "door", "door": "9"}]}, "access"),
    ({"type": "staff", "expires_on": "tomorrow"}, "expires_on"),
])
def test_malformed_requests(data, field):
    with pytest.raises(PlanError) as info:
        plan(data, OWNER, DOORS, SETTINGS, NOW, LA)
    assert (info.value.status, info.value.field) == (400, field)


def test_not_an_object():
    with pytest.raises(PlanError) as info:
        plan([], OWNER, DOORS, SETTINGS, NOW, LA)
    assert (info.value.status, info.value.field) == (400, None)


@pytest.mark.parametrize("card_type", ["day_pass", "overnight", "staff"])
def test_card_type_needs_permission(card_type):
    for who in (perm(()), None, NO_ACCESS, permission_choice("{")):
        err = refused(who, status=403, type=card_type, name="A")
        assert "permission" in err.message


def test_wristbands_turned_off():
    refused(OWNER, status=403, message=wristbands.TURNED_OFF,
            settings={"wristbands_enabled": "0"}, type="day_pass", name="A")


# ---------------------------------------------------------------- day pass

def test_day_pass_happy_path():
    result = make(DESK, type="day_pass", name="  Smith   Family ", count=4)
    assert (result.type, result.name, result.count) == ("day_pass", "Smith Family", 4)
    assert result.records == [rec(1, "Pool Gate"), rec(2, "Gym")]
    assert result.summary == "Pool Gate, Gym"
    assert result.expires_at == utc(2025, 10, 15, 3)   # 8:00 PM PDT
    assert result.expires_label == "8:00 PM today"
    assert result.allow_lockout is False
    assert result.public() == {
        "type": "day_pass", "name": "Smith Family", "count": 4,
        "records": [{"level": "door", "label": "Pool Gate"}, {"level": "door", "label": "Gym"}],
        "summary": "Pool Gate, Gym", "expires_at": utc(2025, 10, 15, 3),
        "expires_label": "8:00 PM today"}
    assert result.records[0].to_dict() == {"level": "door", "building": 1, "floor": 1,
                                           "mac": mac(1), "door": 1, "label": "Pool Gate"}


def test_day_pass_lockout_and_door_names_follow_settings():
    assert make(DESK, settings={"wb_lockout_daypass": "1"}, type="day_pass", name="A").allow_lockout
    doors = {**DOORS, 1: {**DOORS[1], "alias": "Pool", "name": "Outdoor Pool"}}
    assert make(DESK, doors=doors, type="day_pass", name="A").records[0].label == "Outdoor Pool"


def test_day_pass_without_configured_doors():
    for value in ("[]", "{", "[0]", None):
        refused(DESK, message=NO_DAY_PASS_DOORS, settings={"wb_daypass_doors": value},
                type="day_pass", name="A")
    # Even someone who may change doors needs the defaults set up first.
    refused(OWNER, message=NO_DAY_PASS_DOORS, settings={"wb_daypass_doors": "[]"},
            type="day_pass", name="A", door_ids=[1])


def test_day_pass_cutoff():
    refused(DESK, now=utc(2025, 10, 15, 3), message="Day passes end at 8:00 PM. It's already "
            "past that time.", type="day_pass", name="A")
    assert make(DESK, utc(2025, 10, 15, 2, 59), type="day_pass", name="A").expires_label == \
        "8:00 PM today"
    refused(DESK, now=utc(2025, 10, 15, 2, 45), settings={"wb_daypass_end": "19:30"},
            message="Day passes end at 7:30 PM. It's already past that time.",
            type="day_pass", name="A")
    # A damaged setting falls back to the default time.
    assert make(DESK, settings={"wb_daypass_end": "8pm"}, type="day_pass",
                name="A").expires_label == "8:00 PM today"


def test_day_pass_end_time_is_adjustable():
    late = make(DESK, settings={"wb_daypass_end": "22:15"}, type="day_pass", name="A")
    assert late.expires_at == utc(2025, 10, 15, 5, 15) and late.expires_label == "10:15 PM today"


def test_day_pass_after_midnight_utc_is_still_today_locally():
    evening = utc(2025, 10, 15, 1)   # 6:00 PM PDT on Oct 14
    assert make(DESK, evening, type="day_pass", name="A").expires_at == utc(2025, 10, 15, 3)


@pytest.mark.parametrize("name", ["", "   ", None, "x" * 81])
def test_guest_name_required(name):
    refused(DESK, field="name", type="day_pass", name=name)


def test_guest_name_80_characters_is_fine():
    assert len(make(DESK, type="day_pass", name="x" * 80).name) == 80


@pytest.mark.parametrize("count", [0, -1, 21])
def test_day_pass_party_size(count):
    refused(DESK, field="count", message="Party size must be between 1 and 20.",
            type="day_pass", name="A", count=count)


def test_party_size_follows_setting():
    refused(DESK, field="count", settings={"wb_daypass_max": "3"}, type="day_pass", name="A",
            count=4)
    three = make(DESK, settings={"wb_daypass_max": "3"}, type="day_pass", name="A", count=3)
    assert three.count == 3
    assert make(DESK, type="day_pass", name="A").count == 1   # count left out: one


def test_day_pass_door_changes():
    same = make(DESK, type="day_pass", name="A", door_ids=[2, 1])
    assert [r.door for r in same.records] == [1, 2]
    refused(DESK, status=403, field="door_ids", type="day_pass", name="A", door_ids=[1])
    editor = perm(edit_doors=True)
    custom = make(editor, type="day_pass", name="A", door_ids=[201, 1])
    assert [r.door for r in custom.records] == [201, 1]
    refused(editor, field="door_ids", message="Pick at least one door.", type="day_pass",
            name="A", door_ids=[])


def test_hidden_doors_only_as_configured_defaults():
    hidden = {"wb_daypass_doors": "[1, 3]"}
    result = make(DESK, settings=hidden, type="day_pass", name="A")
    assert [r.door for r in result.records] == [1, 3]
    editor = perm(edit_doors=True)
    result = make(editor, settings=hidden, type="day_pass", name="A", door_ids=[3])
    assert [r.door for r in result.records] == [3]
    refused(editor, field="door_ids", message="Staff Room isn't available for cards. Pick a "
            "different door.", type="day_pass", name="A", door_ids=[3])


def test_inactive_and_missing_doors():
    err = refused(DESK, field="door_ids", settings={"wb_daypass_doors": "[1, 4]"},
                  type="day_pass", name="A")
    assert err.message.startswith("Old Door is no longer listed") and "manager" in err.message
    err = refused(DESK, field="door_ids", settings={"wb_daypass_doors": "[1, 999]"},
                  type="day_pass", name="A")
    assert err.message.startswith("One of the doors") and "manager" in err.message
    err = refused(perm(edit_doors=True), field="door_ids", type="day_pass", name="A", door_ids=[4])
    assert "Refresh" in err.message


def test_day_pass_doors_must_be_in_scope():
    refused(perm(doors=B2), status=403, field="door_ids", message="You don't have access to "
            "Pool Gate.", type="day_pass", name="A")
    refused(perm(doors={"doors": [1]}), status=403, field="door_ids",
            message="You don't have access to Gym.", type="day_pass", name="A")
    assert make(perm(doors={"doors": [1, 2]}), type="day_pass", name="A")


def test_doors_without_building_or_floor():
    for lock_id, name in ((5, "Shed"), (7, "Annex")):
        refused(DESK, field="door_ids", settings={"wb_daypass_doors": f"[{lock_id}]"},
                message=f"{name} has no building or floor number in TT Hotel. Ask a manager to "
                        "set it in the TT Hotel app.", type="day_pass", name="A")


def test_door_with_a_bad_mac():
    err = refused(DESK, field="door_ids", settings={"wb_daypass_doors": "[6]"}, type="day_pass",
                  name="A")
    assert err.message.startswith("Odd Lock has lock details")


def test_too_many_records():
    refused(DESK, field="door_ids", message=TOO_MANY_DOORS, settings={"wb_max_records": "1"},
            type="day_pass", name="A")


def test_summary_is_shortened():
    result = make(perm(edit_doors=True), type="day_pass", name="A",
                  door_ids=[1, 2, 201, 202, 203, 301])
    assert result.summary == "Pool Gate, Gym, Room 201, Room 202 +2 more"
    assert len(result.records) == 6


# ---------------------------------------------------------------- overnight

def overnight(who, now=NOW, settings=None, **data):
    return make(who, now, settings, **{"type": "overnight", "name": "Smith", "count": 2,
                                       "room_ids": [201], "checkout": "2025-10-16", **data})


def overnight_refused(who, now=NOW, settings=None, **kwargs):
    data = {"type": "overnight", "name": "Smith", "count": 2, "room_ids": [201],
            "checkout": "2025-10-16"}
    expect = {k: kwargs.pop(k) for k in ("status", "field", "message") if k in kwargs}
    return refused(who, now=now, settings=settings, **expect, **{**data, **kwargs})


def test_overnight_happy_path():
    result = overnight(DESK, room_ids=[201, 202])
    assert [r.door for r in result.records] == [201, 202, 2]
    assert result.summary == "Room 201, Room 202, Gym"
    assert result.expires_at == utc(2025, 10, 16, 18)   # 11:00 AM PDT
    assert result.expires_label == "11:00 AM Thu, Oct 16"
    assert (result.allow_lockout, result.count) == (False, 2)


def test_checkout_time_is_adjustable():
    result = overnight(DESK, settings={"wb_checkout_time": "12:30"})
    assert result.expires_at == utc(2025, 10, 16, 19, 30)
    assert result.expires_label == "12:30 PM Thu, Oct 16"


def test_overnight_room_that_is_also_shared_is_written_once():
    assert [r.door for r in overnight(DESK, room_ids=[2, 201]).records] == [2, 201]


def test_overnight_expiry_across_dst():
    fall = utc(2025, 10, 31, 19)   # Oct 31, noon PDT
    assert overnight(DESK, fall, checkout="2025-11-03").expires_at == utc(2025, 11, 3, 19)  # PST
    spring = utc(2026, 3, 6, 20)   # Mar 6, noon PST
    assert overnight(DESK, spring, checkout="2026-03-09").expires_at == utc(2026, 3, 9, 18)  # PDT


def test_overnight_checkout_dates():
    assert overnight(DESK, checkout="2025-10-14").expires_label == "11:00 AM today"
    overnight_refused(DESK, utc(2025, 10, 14, 19), field="checkout", checkout="2025-10-14")
    overnight_refused(DESK, field="checkout", checkout="2025-10-13")
    overnight_refused(DESK, field="checkout", message="Pick a checkout date.", checkout=None)
    overnight_refused(DESK, field="checkout", checkout="2200-01-01")   # past 2106
    overnight_refused(DESK, settings={"wb_checkout_time": "17:00"}, field="checkout",
                      message=wristbands.TOO_FAR, checkout="9999-12-31")


def test_overnight_night_limit():
    capped = perm(max_nights=2)
    assert overnight(capped, checkout="2025-10-16")
    overnight_refused(capped, field="checkout", message="You can make overnight wristbands for "
                      "up to 2 nights.", checkout="2025-10-17")
    overnight_refused(perm(max_nights=1), field="checkout",
                      message="You can make overnight wristbands for up to 1 night.")


def test_overnight_rooms():
    # Rooms are optional (most rooms take keys): then it opens only the shared doors, and it
    # needs at least one of either.
    no_rooms = overnight(DESK, room_ids=[])
    assert [r.door for r in no_rooms.records] == [2] and no_rooms.summary == "Gym"
    overnight_refused(DESK, settings={"wb_overnight_doors": "[]"}, field="room_ids",
                      message="Pick a room, or a shared door.", room_ids=[])
    overnight_refused(DESK, field="room_ids", room_ids=[3])     # hidden
    overnight_refused(DESK, field="room_ids", room_ids=[4])     # no longer listed
    overnight_refused(DESK, field="room_ids", room_ids=[999])
    overnight_refused(perm(doors=B2), status=403, field="room_ids")
    floor2 = {"floors": [[1, 2]], "doors": [2]}
    assert overnight(perm(doors=floor2), room_ids=[203])
    overnight_refused(perm(doors=floor2), status=403, field="room_ids", room_ids=[301])
    # The shared door must be in scope too.
    overnight_refused(perm(doors={"floors": [[1, 2]]}), status=403, field="door_ids")


def test_overnight_shared_door_changes():
    overnight_refused(DESK, status=403, field="door_ids", door_ids=[])
    assert [r.door for r in overnight(DESK, door_ids=[2]).records] == [201, 2]
    editor = perm(edit_doors=True)
    assert [r.door for r in overnight(editor, door_ids=[]).records] == [201]
    assert [r.door for r in overnight(editor, door_ids=[1, 2]).records] == [201, 1, 2]
    assert [r.door for r in overnight(DESK, settings={"wb_overnight_doors": "[]"}).records] == [201]


def test_overnight_party_size_and_lockout():
    settings = {"wb_overnight_max": "2", "wb_lockout_overnight": "1"}
    overnight_refused(DESK, settings=settings, field="count", count=3)
    assert overnight(DESK, settings=settings).allow_lockout is True


def test_overnight_too_many_records():
    overnight_refused(DESK, settings={"wb_max_records": "2"}, field="room_ids",
                      message=TOO_MANY_DOORS, room_ids=[201, 202])


# ---------------------------------------------------------------- staff cards

def staff(who, now=NOW, settings=None, **data):
    return make(who, now, settings, **{"type": "staff", "name": "Jane Doe", "count": 1,
                                       "access": [{"level": "all"}], "expires_on": "2025-12-31",
                                       **data})


def staff_refused(who, now=NOW, settings=None, **kwargs):
    data = {"type": "staff", "name": "Jane Doe", "count": 1, "access": [{"level": "all"}],
            "expires_on": "2025-12-31"}
    expect = {k: kwargs.pop(k) for k in ("status", "field", "message") if k in kwargs}
    return refused(who, now=now, settings=settings, **expect, **{**data, **kwargs})


def floor(b, f):
    return {"level": "floor", "building": b, "floor": f}


def test_staff_all_doors_card():
    result = staff(OWNER, access=[floor(1, 2), {"level": "all"}])
    assert result.records == [Record("all", 0, 0, BLANK_MAC, None, "All doors")]
    assert result.summary == "All doors"
    assert result.expires_at == utc(2026, 1, 1, 7, 59)   # 11:59 PM PST on Dec 31
    assert result.expires_label == "11:59 PM Wed, Dec 31"
    assert result.allow_lockout is True                  # wb_lockout_staff defaults on


def test_all_doors_needs_the_level_and_every_door():
    staff_refused(STAFF_ALL, status=403, field="access",
                  message="You can't make cards that open every door.")
    staff_refused({**OWNER, "doors": B1}, status=403, field="access")


# (doors, building 1, floor 1/2, door 201, door 2101)
STAFF_SCOPE_MATRIX = [
    ({"all": True}, True, True, True, True),
    (B1, True, True, True, False),
    ({"floors": [[1, 2]]}, False, True, True, False),
    ({"doors": [201]}, False, False, True, False),
    ({}, False, False, False, False),
]


@pytest.mark.parametrize("scope,building,floor_ok,door_ok,other_building", STAFF_SCOPE_MATRIX)
def test_staff_scope_matrix(scope, building, floor_ok, door_ok, other_building):
    who = {**STAFF_ALL, "doors": scope}
    for entry, allowed in (({"level": "building", "building": 1}, building),
                           (floor(1, 2), floor_ok), ({"level": "door", "door": 201}, door_ok),
                           ({"level": "door", "door": 2101}, other_building)):
        if allowed:
            assert staff(who, access=[entry]).records
        else:
            staff_refused(who, status=403, field="access", access=[entry])


@pytest.mark.parametrize("level,entry", [
    ("building", {"level": "building", "building": 1}),
    ("floor", floor(1, 2)),
    ("door", {"level": "door", "door": 201}),
])
def test_staff_levels_are_required(level, entry):
    others = [lv for lv in wristbands.LEVELS if lv != level]
    staff_refused(perm(("staff",), others), status=403, field="access",
                  message=wristbands.LEVEL_REFUSALS[level], access=[entry])
    assert staff(perm(("staff",), (level,)), access=[entry]).records


def test_staff_records_and_labels():
    result = staff(STAFF_ALL, access=[{"level": "building", "building": 2}, floor(1, 1),
                                      floor(1, 2), {"level": "door", "door": 301}])
    assert result.records == [
        Record("building", 2, 0, BLANK_MAC, None, "Building 2"),
        Record("floor", 1, 1, BLANK_MAC, None, "Main, Lobby"),
        Record("floor", 1, 2, BLANK_MAC, None, "Main, Floor 2"),
        rec(301, "Room 301", 1, 3),
    ]
    assert result.summary == "Building 2, Main, Lobby, Main, Floor 2, Room 301"
    b1 = staff(STAFF_ALL, access=[{"level": "building", "building": 1}])
    assert b1.records[0].label == "Main" and b1.summary == "Main"


def test_staff_records_already_covered_are_dropped():
    result = staff(STAFF_ALL, access=[
        {"level": "door", "door": 201}, floor(1, 2), {"level": "building", "building": 2},
        {"level": "door", "door": 2101}, floor(2, 2), {"level": "door", "door": 301}])
    assert [(r.level, r.building, r.floor) for r in result.records] == [
        ("floor", 1, 2), ("building", 2, 0), ("door", 1, 3)]


def test_staff_unknown_places_and_unavailable_doors():
    staff_refused(STAFF_ALL, field="access", access=[{"level": "building", "building": 9}])
    staff_refused(STAFF_ALL, field="access", access=[floor(1, 9)])
    for lock_id in (3, 4, 5, 999):   # hidden, no longer listed, no building, unknown
        staff_refused(STAFF_ALL, field="access", access=[{"level": "door", "door": lock_id}])


def test_staff_expiry_rules():
    staff_refused(OWNER, field="expires_on", message="Pick an expiry date.", expires_on=None)
    staff_refused(OWNER, field="expires_on", message="The expiry date can't be in the past.",
                  expires_on="2025-10-13")
    assert staff(OWNER, expires_on="2025-10-14").expires_label == "11:59 PM today"
    capped = {**OWNER, "max_staff_days": 30}
    assert staff(capped, expires_on="2025-11-13").expires_at == utc(2025, 11, 14, 7, 59)
    staff_refused(capped, field="expires_on", message="Staff cards can't expire more than 30 "
                  "days from today.", expires_on="2025-11-14")
    staff_refused(OWNER, field="expires_on", message="That date is too far away. Pick an "
                  "earlier date.", expires_on="2150-01-01")
    # The last possible date, in a zone west of UTC, would land past year 9999 in UTC.
    for last in ("9999-12-31", "2106-02-07"):
        staff_refused(OWNER, field="expires_on", message=wristbands.TOO_FAR, expires_on=last)
    staff_refused(OWNER, utc(2025, 10, 15, 6, 59) + 30, field="expires_on",
                  message="That expiry time has already passed. Pick a later date.",
                  expires_on="2025-10-14")


def test_staff_quantity_and_name():
    staff_refused(OWNER, field="count", message="Quantity must be between 1 and 5.", count=6)
    assert staff(OWNER, count=5).count == 5
    staff_refused(OWNER, field="name", message="Enter the staff member's name.", name=" ")
    staff_refused(OWNER, field="access", message="Choose what this card opens.", access=[])


def test_staff_too_many_records():
    settings = {"wb_max_records": "2", "wb_lockout_staff": "0"}
    staff_refused(STAFF_ALL, settings=settings, field="access", message=TOO_MANY_DOORS,
                  access=[{"level": "door", "door": i} for i in (201, 202, 203)])
    assert staff(STAFF_ALL, settings=settings, access=[floor(1, 2)]).allow_lockout is False


# ---------------------------------------------------------------- options

def test_options_shape():
    result = options(DESK, DOORS, SETTINGS, NOW, LA)
    doors = result.pop("doors")
    assert result == {
        "enabled": True, "reason": None, "time_zone": "America/Los_Angeles",
        "today": "2025-10-14", "now": NOW,
        "types": {
            "day_pass": {"allowed": True, "reason": None, "expires_at": utc(2025, 10, 15, 3),
                         "expires_label": "8:00 PM today", "door_ids": [1, 2],
                         "can_edit_doors": False, "max": 20, "permitted": True,
                         "end_time": "20:00", "end_time_passed": False,
                         "can_edit_end_time": False},
            "overnight": {"allowed": True, "reason": None, "checkout_time": "11:00",
                          "default_checkout": "2025-10-15", "door_ids": [2],
                          "can_edit_doors": False, "max_nights": 0, "max": 20,
                          "permitted": True},
            "staff": {"allowed": False, "reason": "You don't have permission to make staff "
                      "cards.", "levels": [], "max_days": 0, "default_expires_on": "2026-01-12",
                      "max": 5, "permitted": False},
            "contractor": {"allowed": False, "reason": "You don't have permission to make "
                           "contractor badges.", "permitted": False, "max": 5, "max_days": 7,
                           "default_expires_on": "2025-10-14", "default_end_time": "23:59",
                           "latest_on": "2025-10-21", "latest_time": "05:00",
                           "latest_at": utc(2025, 10, 21, 12),
                           "latest_label": "5:00 AM Tue, Oct 21"},
        },
        "see_all_cards": False,
    }
    # Active, offered doors only, by building and floor; hidden and unlisted ones left out.
    assert [d["id"] for d in doors] == [2, 6, 1, 201, 202, 203, 301, 7, 2101, 2201, 5]
    assert doors[0] == {"id": 2, "name": "Gym", "building": 1, "building_name": "Main",
                        "floor": 1, "floor_name": "Lobby", "kind": None}
    assert doors[3]["building_name"] == "Building 1" and doors[3]["floor_name"] == "Floor 2"
    assert (doors[7]["floor"], doors[7]["floor_name"]) == (None, "")


def test_options_doors_follow_scope():
    result = options(perm(doors={"floors": [[1, 2]], "doors": [2101]}), DOORS, SETTINGS, NOW, LA)
    assert [d["id"] for d in result["doors"]] == [201, 202, 203, 2101]
    assert options(None, DOORS, SETTINGS, NOW, LA)["doors"] == []


def test_options_reasons():
    late = options(DESK, DOORS, SETTINGS, utc(2025, 10, 15, 3, 30), LA)["types"]["day_pass"]
    assert (late["allowed"], late["reason"]) == (
        False, "Day passes end at 8:00 PM. It's already past that time.")
    empty = options(DESK, DOORS, {**SETTINGS, "wb_daypass_doors": "[]"}, NOW, LA)
    assert (empty["types"]["day_pass"]["allowed"], empty["types"]["day_pass"]["reason"]) == (
        False, NO_DAY_PASS_DOORS)
    nobody = options(NO_ACCESS, DOORS, SETTINGS, NOW, LA)
    assert (nobody["enabled"], nobody["reason"]) == (False, wristbands.NOT_ALLOWED)
    assert all(not t["allowed"] for t in nobody["types"].values())
    off = options(OWNER, DOORS, {**SETTINGS, "wristbands_enabled": "0"}, NOW, LA)
    assert (off["enabled"], off["reason"]) == (False, wristbands.TURNED_OFF)
    assert all(t["reason"] == wristbands.TURNED_OFF for t in off["types"].values())
    no_levels = options(perm(("staff",)), DOORS, SETTINGS, NOW, LA)["types"]["staff"]
    assert no_levels["allowed"] is False and no_levels["reason"]


def test_options_staff_and_limits():
    opts = options({**OWNER, "max_staff_days": 30, "max_nights": 3}, DOORS, SETTINGS, NOW, LA)
    assert opts["types"]["staff"]["levels"] == ["door", "floor", "building", "all"]
    assert opts["types"]["staff"]["default_expires_on"] == "2025-11-13"
    assert opts["types"]["staff"]["max_days"] == 30
    assert opts["types"]["overnight"]["max_nights"] == 3
    assert opts["types"]["day_pass"]["can_edit_doors"] is True and opts["see_all_cards"] is True
    custom = options(OWNER, DOORS, {**SETTINGS, "wb_staff_days": "7"}, NOW, LA)
    assert custom["types"]["staff"]["default_expires_on"] == "2025-10-21"


def test_options_late_evening_uses_the_local_date():
    opts = options(DESK, DOORS, SETTINGS, utc(2025, 10, 15, 4), LA)   # 9:00 PM PDT, Oct 14
    assert opts["today"] == "2025-10-14"
    assert opts["types"]["overnight"]["default_checkout"] == "2025-10-15"
    assert opts["types"]["day_pass"]["allowed"] is False


# ---------------------------------------------------------------- time

def test_day_pass_expiry_label_and_offset():
    noon = utc(2025, 10, 14, 19)   # 12:00 PDT
    end = at_local_time(LA, date(2025, 10, 14), "20:00")
    assert end.isoformat() == "2025-10-14T20:00:00-07:00"
    assert format_expiry(end.timestamp(), noon, LA) == "8:00 PM today"


def test_expiry_labels():
    noon = utc(2025, 10, 14, 19)
    tomorrow = at_local_time(LA, date(2025, 10, 15), "11:00")
    assert tomorrow.isoformat() == "2025-10-15T11:00:00-07:00"
    assert format_expiry(tomorrow.timestamp(), noon, LA) == "11:00 AM tomorrow"
    later = at_local_time(LA, date(2025, 10, 17), "11:00")
    assert format_expiry(later.timestamp(), noon, LA) == "11:00 AM Fri, Oct 17"
    next_year = at_local_time(LA, date(2026, 1, 2), "23:59")
    assert format_expiry(next_year.timestamp(), noon, LA) == "11:59 PM Fri, Jan 2, 2026"


def test_dst_gap_moves_forward_and_repeated_hour_uses_the_first():
    gap = at_local_time(LA, date(2026, 3, 8), "02:30")
    assert gap.isoformat() == "2026-03-08T03:30:00-07:00"
    repeated = at_local_time(LA, date(2025, 11, 2), "01:30")
    assert repeated.utcoffset().total_seconds() == -7 * 3600


def test_setting_helpers():
    assert wristbands.clean_hhmm(" 07:05 ") == "07:05"
    for bad in ("7:05", "24:00", "12:60", 1200, None):
        with pytest.raises(ValueError):
            wristbands.clean_hhmm(bad)
    assert wristbands.parse_time_zone("Europe/London").key == "Europe/London"
    for bad in ("", "Mars/Olympus", "../etc/passwd", None):
        with pytest.raises(ValueError):
            wristbands.parse_time_zone(bad)
    assert wristbands.setting_number({"wb_daypass_max": "99"}, "wb_daypass_max") == 20
    assert wristbands.setting_doors({"wb_overnight_doors": '[3, "4", 3, 0]'},
                                    "wb_overnight_doors") == (3, 4)
