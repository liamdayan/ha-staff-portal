"""Staff doors (a third door kind, only for staff cards and contractor badges) and contractor
badges (any door in the maker's own scope, for at most 7 days): the rules in wristbands.py, then
the dashboard, the staff app, a badge made on the encoder simulator, Door activity, backups and
migration files."""

import json
import time
from datetime import datetime, time as clock
from zoneinfo import ZoneInfo

import pytest

from test_door_activity import deliver, fresh_state, record
from test_wristbands_rules import DOORS as RULE_DOORS, LA, NOW, SETTINGS, make, perm, refused, utc
from test_wristbands_server import (DOORS, FRONT_DESK, FakeTTHotel, Portal, Simulator, day_pass,
                                    job_until, local_date, noon_zone, overnight, run, staff_card,
                                    tt_lock, tthotel_account, waiting_for)

import encoder  # noqa: E402
import main  # noqa: E402
import migrate  # noqa: E402
from db import Database  # noqa: E402
from wristbands import (CONTRACTOR_TOO_LONG, NO_ACCESS, NO_CONTRACTOR_DOORS, TURNED_OFF,
                        clean_permissions, contractor_latest, door_kind, options,
                        permission_choice)

# Pool Gate and Gym are public doors, the rooms on floors 2 and 3 guest rooms, the second
# building's first door a staff door (the boiler room); its second door isn't marked yet.
MARKS = {1: "public", 2: "public", 201: "room", 202: "room", 203: "room", 301: "room",
         2101: "staff"}
KINDS = {i: {**d, "kind": MARKS.get(i)} for i, d in RULE_DOORS.items()}
KINDS[2101]["name"] = "Boiler Room"
EDITOR = perm(edit_doors=True)
CONTRACTOR = perm(("contractor",))
BADGE = {"type": "contractor", "name": "Sam Lee", "count": 1, "door_ids": [1, 2101],
         "expires_on": "2025-10-16", "end_time": "17:00"}
LAST_MINUTE = utc(2025, 10, 15, 6, 59) + 30   # 11:59:30 PM on Tuesday, Oct 14 in Los Angeles


def badge(who=CONTRACTOR, now=NOW, settings=None, doors=None, **changes):
    return make(who, now, settings, KINDS if doors is None else doors, **{**BADGE, **changes})


def refused_badge(who=CONTRACTOR, **changes):
    expected = {k: changes.pop(k) for k in ("status", "field", "message", "now", "settings")
                if k in changes}
    return refused(who, doors=KINDS, **expected, **{**BADGE, **changes})


# ---------------------------------------------------------------- staff doors: the rules

def test_staff_is_a_door_kind():
    assert [door_kind(v) for v in ("public", "room", "staff", "Staff", "office", None)] == [
        "public", "room", "staff", None, None, None]
    doors = {d["id"]: d["kind"] for d in options(CONTRACTOR, KINDS, SETTINGS, NOW, LA)["doors"]}
    assert (doors[1], doors[201], doors[2101], doors[2201]) == ("public", "room", "staff", None)


def test_staff_doors_are_never_shared_doors_or_rooms():
    message = "Boiler Room is a staff door, not a public door. Pick a public door."
    refused(EDITOR, field="door_ids", message=message, doors=KINDS, type="day_pass", name="A",
            door_ids=[1, 2101])
    refused(EDITOR, field="door_ids", message=message, doors=KINDS, type="overnight",
            name="Smith", room_ids=[201], checkout="2025-10-16", door_ids=[2, 2101])
    refused(perm(), field="room_ids", doors=KINDS, type="overnight", name="Smith",
            room_ids=[201, 2101], checkout="2025-10-16",
            message="Boiler Room is a staff door, not a guest room. Pick a guest room.")
    # A door not marked yet is still both, as before.
    result = make(EDITOR, doors=KINDS, type="day_pass", name="A", door_ids=[1, 2201])
    assert [r.door for r in result.records] == [1, 2201]
    result = make(perm(), doors=KINDS, type="overnight", name="Smith", room_ids=[2201],
                  checkout="2025-10-16")
    assert [r.door for r in result.records] == [2201, 2]


def test_staff_doors_already_in_the_settings_keep_working():
    # The Gym is a day-pass door and the overnight shared door, then marked a staff door.
    gym = {**KINDS, 2: {**KINDS[2], "kind": "staff"}}
    assert [r.door for r in make(perm(), doors=gym, type="day_pass", name="A").records] == [1, 2]
    assert [r.door for r in make(perm(), doors=gym, type="overnight", name="Smith",
                                 room_ids=[201], checkout="2025-10-16").records] == [201, 2]
    # Kept by someone who changes the doors too; only a staff door they add is refused.
    result = make(EDITOR, doors=gym, type="day_pass", name="A", door_ids=[2, 2201])
    assert [r.door for r in result.records] == [2, 2201]
    refused(EDITOR, field="door_ids", doors=gym, type="day_pass", name="A", door_ids=[2, 2101])


def test_staff_cards_and_contractor_badges_open_staff_doors():
    owner = perm(("staff",), ("door",))
    result = make(owner, doors=KINDS, type="staff", name="Eve", expires_on="2025-10-20",
                  access=[{"level": "door", "door": 2101}, {"level": "door", "door": 201}])
    assert [r.door for r in result.records] == [2101, 201]
    result = badge(door_ids=[1, 201, 2101, 2201])
    assert [r.door for r in result.records] == [1, 201, 2101, 2201]


# ---------------------------------------------------------------- contractor badges: the rules

def test_contractor_badges_need_the_permission():
    for who in (perm(), perm(("staff",), ("door", "all")), None):
        refused_badge(who, status=403,
                      message="You don't have permission to make contractor badges.")
    result = badge()
    assert (result.type, result.name, result.count) == ("contractor", "Sam Lee", 1)
    assert [r.label for r in result.records] == ["Pool Gate", "Boiler Room"]
    assert (result.expires_at, result.expires_label) == (utc(2025, 10, 17), "5:00 PM Thu, Oct 16")
    assert result.allow_lockout is False
    assert result.public()["summary"] == "Pool Gate, Boiler Room"
    # Nobody has it until it's ticked; it's checked like the other types.
    assert "contractor" not in NO_ACCESS["types"]
    assert clean_permissions({})["types"] == []
    assert clean_permissions({"types": ["contractor", "day_pass"]})["types"] == [
        "day_pass", "contractor"]
    assert permission_choice('{"types": ["contractor", "vip"]}') == NO_ACCESS
    # The options say who may make them.
    mine = options(CONTRACTOR, KINDS, SETTINGS, NOW, LA)["types"]["contractor"]
    assert (mine["allowed"], mine["permitted"], mine["reason"]) == (True, True, None)
    desk = options(perm(), KINDS, SETTINGS, NOW, LA)["types"]["contractor"]
    assert (desk["allowed"], desk["permitted"]) == (False, False)
    assert desk["reason"] == "You don't have permission to make contractor badges."


def test_contractor_doors_follow_the_makers_own_scope():
    main_building = perm(("contractor",), doors={"buildings": [1], "floors": [], "doors": []})
    assert [r.door for r in badge(main_building, door_ids=[1, 201]).records] == [1, 201]
    refused_badge(main_building, status=403, field="door_ids", door_ids=[1, 2101],
                  message="You don't have access to Boiler Room.")
    floor_and_door = perm(("contractor",), doors={"floors": [[1, 2]], "doors": [2101]})
    assert [r.door for r in badge(floor_and_door, door_ids=[202, 2101]).records] == [202, 2101]
    refused_badge(floor_and_door, status=403, field="door_ids", door_ids=[202, 1])
    refused_badge(perm(("contractor",), doors={}), status=403, field="door_ids")
    # Hidden, no longer listed, unknown and unusable doors, as for the other cards.
    refused_badge(field="door_ids", door_ids=[3],
                  message="Staff Room isn't available for cards. Pick a different door.")
    refused_badge(field="door_ids", door_ids=[4], message="Old Door is no longer listed in TT "
                  "Hotel. Refresh the page and pick again.")
    refused_badge(field="door_ids", door_ids=[999])
    refused_badge(field="door_ids", door_ids=[5])
    for none in ([], None):
        refused_badge(field="door_ids", door_ids=none, message="Pick at least one door.")
    refused_badge(field="door_ids", door_ids=["1"])
    refused_badge(field="door_ids", door_ids=[1, 2, 201], settings={"wb_max_records": "2"},
                  message="That's too many doors for one badge. Pick at most 2.")


def test_contractor_badges_last_at_most_seven_days():
    # It's 5:00 AM on Tuesday, Oct 14: the latest end is 5:00 AM on Tuesday, Oct 21.
    assert contractor_latest(NOW, LA).timestamp() == NOW + 7 * 86400
    last = badge(expires_on="2025-10-21", end_time="05:00")
    assert (last.expires_at, last.expires_label) == (NOW + 7 * 86400, "5:00 AM Tue, Oct 21")
    refused_badge(field="end_time", message=CONTRACTOR_TOO_LONG, expires_on="2025-10-21",
                  end_time="05:01")
    for later in ("2025-10-22", "2026-01-01", "2099-12-31"):
        refused_badge(field="expires_on", message=CONTRACTOR_TOO_LONG, expires_on=later,
                      end_time="01:00")
    assert CONTRACTOR_TOO_LONG == "Contractor badges can last at most 7 days."
    # It must end later than now.
    soon = badge(expires_on="2025-10-14", end_time="05:01")
    assert soon.expires_label == "5:01 AM today"
    for passed in ("05:00", "04:59", "00:00"):
        refused_badge(field="end_time", expires_on="2025-10-14", end_time=passed,
                      message="That time has already passed. Pick a later time.")
    refused_badge(field="expires_on", expires_on="2025-10-13",
                  message="That date has already passed. Pick a later date.")
    # Both are needed, and must be a real date and time.
    refused_badge(field="expires_on", expires_on=None, message="Pick the last day it works.")
    refused_badge(field="expires_on", expires_on="", message="Pick the last day it works.")
    refused_badge(field="end_time", end_time=None, message="Pick the time it stops working.")
    for bad in ("25:00", "5:00", "17:00:00", 1700):
        refused_badge(field="end_time", end_time=bad, message="Pick a valid time.")
    for bad in ("2025-02-30", "Oct 16", 20251016):
        refused_badge(field="expires_on", expires_on=bad)


@pytest.mark.parametrize("now, last_day, latest, after", [
    # Clocks go back an hour on Nov 2, 2025: the same time 7 days on is 7 days and an hour away.
    (utc(2025, 10, 27, 17), "2025-11-03", "10:00", "10:01"),
    # Clocks go forward on Mar 9, 2025: 7 days on is an hour less than 7 x 24 hours.
    (utc(2025, 3, 3, 18), "2025-03-10", "10:00", "10:01"),
    # 2:30 AM doesn't exist on Mar 9: the limit moves past the gap, to 3:30 AM.
    (utc(2025, 3, 2, 10, 30), "2025-03-09", "03:30", "03:31"),
    # 1:30 AM happens twice on Nov 2: the first one counts.
    (utc(2025, 10, 26, 8, 30), "2025-11-02", "01:30", "01:31"),
])
def test_seven_days_across_daylight_saving_changes(now, last_day, latest, after):
    hours, minutes = map(int, latest.split(":"))
    limit = contractor_latest(now, LA)
    assert (limit.date().isoformat(), limit.hour, limit.minute) == (last_day, hours, minutes)
    made = badge(now=now, expires_on=last_day, end_time=latest)
    assert made.expires_at == int(limit.timestamp())
    refused_badge(now=now, field="end_time", message=CONTRACTOR_TOO_LONG, expires_on=last_day,
                  end_time=after)
    want = options(CONTRACTOR, KINDS, SETTINGS, now, LA)["types"]["contractor"]
    assert (want["latest_on"], want["latest_time"], want["latest_at"]) == (
        last_day, latest, int(limit.timestamp()))


def test_contractor_end_time_inside_a_daylight_saving_gap():
    # 2:30 AM on Mar 9, 2025 doesn't exist in Los Angeles: it moves to 3:30 AM, as for day passes.
    now = utc(2025, 3, 8, 18)   # 10:00 AM on Saturday, Mar 8
    made = badge(now=now, expires_on="2025-03-09", end_time="02:30")
    assert (made.expires_at, made.expires_label) == (utc(2025, 3, 9, 10, 30), "3:30 AM tomorrow")


def test_contractor_count_name_company_and_lockout():
    assert badge(count=5).count == 5
    for bad in (6, 0, -1):
        refused_badge(field="count", count=bad, message="Quantity must be between 1 and 5.")
    refused_badge(field="count", count="2", message="Quantity must be a whole number.")
    assert badge(count=12, settings={"wb_contractor_max": "12"}).count == 12
    refused_badge(field="count", count=6, settings={"wb_contractor_max": "99"})  # damaged: 5
    refused_badge(field="name", name="  ", message="Enter the contractor's name.")
    assert badge(company="  Acme   Plumbing ").name == "Sam Lee (Acme Plumbing)"
    assert badge(company="").name == badge(company=None).name == "Sam Lee"
    refused_badge(field="company", company=5,
                  message="Enter the company's name, or leave it blank.")
    refused_badge(field="company", name="A" * 60, company="B" * 30,
                  message="The name and company together can be at most 80 characters.")
    assert len(badge(name="A" * 70, company="B" * 7).name) == 80
    # Only contractor badges have a company; it's ignored on the others.
    assert make(perm(), type="day_pass", name="A", company=5).name == "A"
    assert badge(settings={"wb_lockout_contractor": "1"}).allow_lockout is True


def test_contractor_options():
    o = options(CONTRACTOR, KINDS, {**SETTINGS, "wb_contractor_max": "12"}, NOW, LA)
    mine = o["types"]["contractor"]
    assert (mine["max"], mine["max_days"]) == (12, 7)
    # The form starts at the end of today; in the day's last minute, at the end of tomorrow.
    assert (mine["default_expires_on"], mine["default_end_time"]) == ("2025-10-14", "23:59")
    late = options(CONTRACTOR, KINDS, SETTINGS, LAST_MINUTE, LA)["types"]["contractor"]
    assert late["default_expires_on"] == "2025-10-15"
    assert late["latest_label"] == "11:59 PM Tue, Oct 21"
    # Every kind of door in their scope is offered to pick from (hidden and gone ones aren't).
    assert {d["id"] for d in o["doors"]} == {1, 2, 5, 6, 7, 201, 202, 203, 301, 2101, 2201}
    # No doors in their scope, or wristbands off.
    nowhere = options(perm(("contractor",), doors={}), KINDS, SETTINGS, NOW, LA)
    assert (nowhere["types"]["contractor"]["allowed"], nowhere["types"]["contractor"]["reason"],
            nowhere["types"]["contractor"]["permitted"]) == (False, NO_CONTRACTOR_DOORS, True)
    off = options(CONTRACTOR, KINDS, {**SETTINGS, "wristbands_enabled": "0"}, NOW, LA)
    assert off["types"]["contractor"]["reason"] == TURNED_OFF
    refused_badge(status=403, settings={"wristbands_enabled": "0"}, message=TURNED_OFF)


def test_encoder_words_for_badges():
    assert encoder.card_noun("contractor") == "badge"
    assert encoder.place_message("contractor", 1, 1) == "Place the badge on the encoder"
    assert encoder.summary_message("contractor", "done", [{"status": "written"}] * 2) == (
        "All 2 badges are ready")
    assert main.card_noun("contractor", 2) == "2 contractor badges"
    assert main.card_noun("contractor") == "a contractor badge"


# ---------------------------------------------------------------- staff doors: the add-on

def test_marking_staff_doors_from_the_dashboard(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            status, _ = await p.admin_call("PATCH", "/api/wristbands/doors/101", {"kind": "staff"})
            assert status == 200 and p.db.wb_door(101)["kind"] == "staff"
            status, reply = await p.admin_call("PATCH", "/api/wristbands/doors/101",
                                               {"kind": "office"})
            assert (status, reply["error"]) == (400, "Choose Public door, Guest room, Staff door "
                                                     "or Not marked.")
            status, reply = await p.admin_call("POST", "/api/wristbands/doors/kind",
                                               {"door_ids": [102, 201], "kind": "staff"})
            assert (status, reply) == (200, {"changed": 2})
            status, doors = await p.admin_call("GET", "/api/wristbands/doors")
            assert {d["id"]: d["kind"] for d in doors["doors"]} == {
                101: "staff", 102: "staff", 201: "staff", 900: None, 901: None}
            assert "Marked 2 doors as staff doors" in [a[1] for a in p.activity()]
            assert any(a["action"] == "Updated door Room 101" and a["detail"] == "marked as a "
                       "staff door" for a in p.db.activity(50))
            # TT Hotel's door sync leaves it alone.
            p.db.sync_wb_doors(DOORS)
            assert p.db.wb_door(101)["kind"] == "staff"
    run(scenario())


def test_staff_doors_can_not_be_ticked_as_day_pass_or_overnight_doors(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            # Pool Gate and Spa are the day-pass doors; marking them staff doors keeps them.
            p.db.set_wb_door_kinds([900, 901, 101], "staff")
            status, reply = await p.admin_call("PUT", "/api/settings", {
                "wb_daypass_doors": [900, 901], "wb_overnight_doors": [900]})
            assert status == 200, reply
            # A staff door newly ticked is refused, and nothing is saved.
            for key, value in (("wb_daypass_doors", [900, 901, 101]),
                               ("wb_overnight_doors", [101])):
                status, reply = await p.admin_call("PUT", "/api/settings", {
                    key: value, "wb_checkout_time": "09:00"})
                assert status == 400 and reply["error"] == (
                    "Room 101 is a staff door, so it can't go on day passes or overnight "
                    "wristbands. Untick it, or mark it as a public door."), key
            assert p.db.settings()["wb_checkout_time"] == "11:00"
            assert json.loads(p.db.settings()["wb_daypass_doors"]) == [900, 901]
            # Doors of the other kinds, or not marked, are as before.
            p.db.set_wb_door_kinds([101], "room")
            status, _ = await p.admin_call("PUT", "/api/settings", {
                "wb_overnight_doors": [900, 101, 102]})
            assert status == 200
    run(scenario())


def test_staff_app_keeps_staff_doors_off_guest_cards(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        p.db.set_wb_door_kinds([201], "staff")
        async with p:
            ana, bob = await p.sign_in(p.ana), await p.sign_in(p.bob)
            status, opts = await p.call("GET", "/api/wristbands", ana)
            assert {d["id"]: d["kind"] for d in opts["doors"]}[201] == "staff"
            preview = "/api/wristbands/preview"
            status, reply = await p.call("POST", preview, bob, day_pass(door_ids=[900, 201]))
            assert (status, reply["field"]) == (400, "door_ids")
            assert reply["error"] == "Room 201 is a staff door, not a public door. Pick a public door."
            status, reply = await p.call("POST", "/api/wristbands/jobs", bob,
                                         overnight([101], door_ids=[900, 201]))
            assert (status, reply["field"]) == (400, "door_ids")
            status, reply = await p.call("POST", preview, ana, overnight([201]))
            assert (status, reply["field"]) == (400, "room_ids") and "staff door" in reply["error"]
            # Staff cards open it.
            status, reply = await p.call("POST", preview, bob,
                                         staff_card([{"level": "door", "door": 201}]))
            assert status == 200 and reply["plan"]["summary"] == "Room 201"
            # Day-pass doors marked staff doors after they were chosen keep working.
            p.db.set_wb_door_kinds([900, 901], "staff")
            assert (await p.call("POST", preview, ana, day_pass()))[0] == 200
            assert (await p.call("POST", preview, ana, overnight([101])))[0] == 200
            assert not p.jobs()
    run(scenario())


# ---------------------------------------------------------------- contractor badges: the add-on

def contractor(days=2, end="17:00", **extra):
    return {"type": "contractor", "name": "Sam Lee", "company": "Acme Plumbing", "count": 1,
            "door_ids": [900, 201], "expires_on": local_date(days), "end_time": end, **extra}


def local_end(days: int, hhmm: str) -> int:
    zone = ZoneInfo(noon_zone())
    hours, minutes = map(int, hhmm.split(":"))
    day = datetime.fromisoformat(local_date(days)).date()
    return int(datetime.combine(day, clock(hours, minutes), zone).timestamp())


def test_contractor_badge_from_the_staff_app(tmp_path, monkeypatch):
    fresh_state(monkeypatch)

    async def scenario():
        async with Simulator() as sim, FakeTTHotel([tt_lock(d) for d in DOORS]):
            p = Portal(tmp_path, encoder_url=f"ws://127.0.0.1:{sim._runner.addresses[0][1]}")
            tthotel_account(p)
            p.db.set_wb_door_kinds([201], "staff")
            # Dee may make contractor badges for the pool house and Room 201 only.
            p.db.update_staff(p.dee, wristbands=json.dumps({
                **FRONT_DESK, "types": ["day_pass", "overnight", "contractor"],
                "doors": {"buildings": [2], "floors": [], "doors": [201]}}))
            async with p:
                ana, dee = await p.sign_in(p.ana), await p.sign_in(p.dee)
                badge_type = (await p.call("GET", "/api/wristbands", ana))[1]["types"]["contractor"]
                assert (badge_type["permitted"], badge_type["allowed"]) == (False, False)
                for path in ("/api/wristbands/preview", "/api/wristbands/jobs"):
                    status, reply = await p.call("POST", path, ana, contractor())
                    assert status == 403 and reply["error"] == (
                        "You don't have permission to make contractor badges.")
                assert ("Ana on Front Desk PC", "Blocked: wristbands", False) in p.activity()

                opts = (await p.call("GET", "/api/wristbands", dee))[1]
                mine = opts["types"]["contractor"]
                assert (mine["allowed"], mine["max"], mine["max_days"]) == (True, 5, 7)
                assert {d["id"] for d in opts["doors"]} == {900, 901, 201}
                preview = "/api/wristbands/preview"
                status, reply = await p.call("POST", preview, dee, contractor(door_ids=[900, 101]))
                assert (status, reply["error"]) == (403, "You don't have access to Room 101.")
                status, reply = await p.call("POST", preview, dee, contractor(days=8))
                assert (status, reply["field"], reply["error"]) == (
                    400, "expires_on", "Contractor badges can last at most 7 days.")
                status, reply = await p.call("POST", preview, dee, contractor(count=6))
                assert (status, reply["field"]) == (400, "count")
                status, reply = await p.call("POST", preview, dee, contractor())
                assert status == 200, reply
                assert reply["plan"]["name"] == "Sam Lee (Acme Plumbing)"
                assert reply["plan"]["summary"] == "Pool Gate, Room 201"
                assert reply["plan"]["expires_at"] == local_end(2, "17:00")

                status, reply = await p.call("POST", "/api/wristbands/jobs", dee, contractor())
                assert status == 200, reply
                job_id = reply["job"]["id"]
                assert reply["job"]["type"] == "contractor"
                job = await job_until(p, dee, job_id, waiting_for(1))
                assert job["message"] == "Place the badge on the encoder"
                card = sim.place_card()
                job = await job_until(p, dee, job_id, lambda j: j["status"] == "done")
                assert job["message"] == "The badge is ready"
                number = job["cards"][0]["card_number"]
                assert {r["mac"] for r in sim.cards[card]["records"]} == {
                    "C540E09C8C90", "C540E09C8C03"}
                assert {r["endDate"] for r in sim.cards[card]["records"]} == {local_end(2, "17:00")}
                sim.remove_card()
                assert ("Dee on Front Desk PC", "Made a contractor badge for Sam Lee (Acme "
                        "Plumbing)", True) in p.activity()
                log = (await p.call("GET", "/api/wristbands/log?q=acme", dee))[1]["jobs"]
                assert [(j["id"], j["type"], j["name"]) for j in log] == [
                    (job_id, "contractor", "Sam Lee (Acme Plumbing)")]
                # Not a staff key card: it's not on the Staff tab's Key cards list.
                _, cards = await p.admin_call("GET", "/api/keycards?state=all")
                assert cards["cards"] == []

                # Door activity names the contractor and says it's a contractor badge.
                assert await deliver(p, 900, [record(49, number)]) == (200, "success")
                _, data = await p.admin_call("GET", "/api/door-activity")
                assert [(r["who"], r["card_type"]) for r in data["records"]] == [
                    ("Sam Lee (Acme Plumbing)", "contractor")]
                await deliver(p, 900, [record(4, "", username="Pool code")])
                _, data = await p.admin_call("GET", "/api/door-activity")
                assert data["records"][0]["card_type"] is None
    run(scenario())


def test_contractor_settings_and_permissions_from_the_dashboard(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            s = p.db.settings()
            assert (s["wb_contractor_max"], s["wb_lockout_contractor"]) == ("5", "0")
            status, _ = await p.admin_call("PUT", "/api/settings", {
                "wb_contractor_max": 99, "wb_lockout_contractor": "1"})
            assert status == 200
            s = p.db.settings()
            assert (s["wb_contractor_max"], s["wb_lockout_contractor"]) == ("20", "1")
            assert ("Admin", "Updated wristband settings", True) in p.activity()
            status, reply = await p.admin_call("PUT", "/api/settings", {"wb_contractor_max": "x"})
            assert status == 400 and reply["error"]
            # The role and person editors' Contractor badges box.
            status, _ = await p.admin_call("PUT", f"/api/roles/{p.no_cards}", {
                "name": "Housekeeping", "entities": {},
                "wristbands": {**FRONT_DESK, "types": ["contractor"]}})
            assert status == 200
            assert p.db.role(p.no_cards)["wristbands"]["types"] == ["contractor"]
            status, _ = await p.admin_call("PATCH", f"/api/staff/{p.ana}", {
                "wristbands": {**FRONT_DESK, "types": ["overnight", "contractor"]}})
            assert status == 200
            assert json.loads(p.db.staff(p.ana)["wristbands"])["types"] == [
                "overnight", "contractor"]
            cy = await p.sign_in(p.cy)
            mine = (await p.call("GET", "/api/wristbands", cy))[1]["types"]
            assert mine["contractor"]["allowed"] and not mine["day_pass"]["permitted"]
    run(scenario())


def test_staff_doors_and_contractor_badges_in_backups_and_migrations(tmp_path):
    async def scenario():
        p = Portal(tmp_path / "old")
        p.db.set_wb_door_kinds([201], "staff")
        p.db.set_wb_door_kinds([900], "public")
        p.db.update_settings({"wb_contractor_max": "9", "wb_lockout_contractor": "1"})
        p.db.set_role_wristbands(p.front_desk, {**FRONT_DESK, "types": ["contractor"]})
        job_id = p.db.add_wb_job(
            card_type="contractor", name="Sam Lee (Acme)", count=1, records=[], summary="Pool Gate",
            expires_at=time.time() + 3600, allow_lockout=False, staff_id=p.dee,
            device_id=p.device, actor="Dee", role=None, ip=None, encoder_url="ws://127.0.0.1:1")
        p.db.update_wb_card(job_id, 1, status="written", card_number="555123")
        p.db.update_wb_job(job_id, status="done", finished_at=time.time())
        async with p:
            status, backup = await p.admin_call("GET", "/api/backup")
            assert status == 200
            kinds = {d["lock_id"]: d["kind"] for d in backup["wristband_doors"]}
            assert (kinds[201], kinds[900], kinds[101]) == ("staff", "public", None)
            assert (backup["settings"]["wb_contractor_max"],
                    backup["settings"]["wb_lockout_contractor"]) == ("9", "1")
            p.db.set_wb_door_kinds([201, 900], None)
            p.db.update_settings({"wb_contractor_max": "5", "wb_lockout_contractor": "0"})
            p.db.set_role_wristbands(p.front_desk, None)
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, reply
            assert {d["lock_id"]: d["kind"] for d in p.db.wb_doors()} == kinds
            s = p.db.settings()
            assert (s["wb_contractor_max"], s["wb_lockout_contractor"]) == ("9", "1")
            assert p.db.role(p.front_desk)["wristbands"]["types"] == ["contractor"]
            assert [(j["card_type"], j["name"]) for j in p.jobs()] == [
                ("contractor", "Sam Lee (Acme)")]
            # A 0.9.0 backup: no staff doors or contractor settings, and nothing else changes.
            old = json.loads(json.dumps(backup))
            for key in ("wb_contractor_max", "wb_lockout_contractor"):
                old["settings"].pop(key)
            for row in old["wristband_doors"]:
                row["kind"] = None if row["kind"] == "staff" else row["kind"]
            p.db.update_settings({"wb_contractor_max": "3"})
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": old})
            assert status == 200, reply
            assert p.db.settings()["wb_contractor_max"] == "3"
            assert p.db.wb_door(201)["kind"] is None and p.db.wb_door(900)["kind"] == "public"

        # A migration file carries staff doors and the contractor settings.
        p.db.set_wb_door_kinds([201], "staff")
        snapshot = json.loads(json.dumps(migrate.make_snapshot(p.db, main.branding, main.VERSION)))
        fresh = Database(str(tmp_path / "new"))
        try:
            snap = migrate.clean_snapshot(snapshot, main.clean_settings, fresh.settings(),
                                          main.branding.clean_logo)
            assert {d["lock_id"]: d["kind"] for d in snap["migrate"]["doors"]}[201] == "staff"
            fresh.replace_everything(snap)
            assert fresh.wb_door(201)["kind"] == "staff"
            assert fresh.settings()["wb_contractor_max"] == "3"
        finally:
            fresh.conn.close()
    run(scenario())
