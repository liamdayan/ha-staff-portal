"""0.8.3 on the server: the idle time for phones and for computers, new shared computers hiding
names, the PIN length the PIN pad may sign in at (recorded wherever a PIN is set, and only given
out when everyone who could sign in shares it), and Look up showing what's really on a card,
with Revoke from there.

Nothing here talks to a real TT Hotel, Connecteam or Home Assistant (see test_wristbands_server
and test_staffcards_server for the fakes used).
"""

import asyncio
import json
import os
import re
import time

from test_staffcards_server import FakeConnecteam, by_name, connecteam_on, ct_user, sync
from test_wristbands_server import (DOORS, FakeTTHotel, Portal, Simulator, day_pass, job_until,
                                    run, tt_lock, tthotel_account, waiting_for)

import main  # noqa: E402
import transfer  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def phone(p: Portal, name="Pool phone", mode="staff", pin=None) -> tuple[int, dict]:
    """A phone set up and signed in to the add-on: (its ID, the headers it sends)."""
    device_id = p.db.create_device(name, p.front_desk, pin)
    p.db.update_device(device_id, signin_mode=mode)
    _, token = p.db.claim_enroll_code(p.db.device(device_id)["enroll_code"])
    return device_id, {"Authorization": f"Bearer {token}"}


def age_sessions(device_id: int, seconds: float) -> None:
    """Make every PIN session on a device look as if it was last used this long ago."""
    for key, (dev, used, sid) in list(main.pin_sessions.items()):
        if dev == device_id:
            main.pin_sessions[key] = (dev, time.time() - seconds, sid)


async def me(p: Portal, headers: dict) -> dict:
    status, reply = await p.call("GET", "/api/me", headers)
    assert status == 200, reply
    return reply


# ---------------------------------------------------------------- idle time, phones and computers

def test_phones_and_computers_each_have_their_own_idle_time(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        p.db.update_settings({"pin_timeout_minutes": "15", "pin_timeout_computer_minutes": "5"})
        phone_id, phone_auth = phone(p)
        async with p:
            desk = await p.sign_in(p.ana)
            pool = await p.sign_in(p.ana, phone_auth["Authorization"][7:])
            assert (await me(p, desk))["pin_timeout_minutes"] == 5
            assert (await me(p, pool))["pin_timeout_minutes"] == 15
            # Six minutes untouched: the computer asks for a PIN again, the phone doesn't.
            age_sessions(p.device, 6 * 60)
            age_sessions(phone_id, 6 * 60)
            status, reply = await p.call("GET", "/api/wristbands/log", desk)
            assert status == 403 and reply["error"] == "pin_required"
            assert (await p.call("POST", "/api/signout", pool))[0] == 200
            pool = await p.sign_in(p.ana, phone_auth["Authorization"][7:])
            age_sessions(phone_id, 16 * 60)
            assert (await p.call("POST", "/api/signout", pool))[1]["error"] == "pin_required"
            # The preview's idle time follows the view it shows.
            status, reply = await p.admin_call("GET", "/api/preview/me?view=desktop")
            assert reply["pin_timeout_minutes"] == 5
            status, reply = await p.admin_call("GET", "/api/preview/me")
            assert reply["pin_timeout_minutes"] == 15

            # The setting has the same bounds as the phones' one, and is shown in Settings.
            for typed, kept in (("0", "1"), ("5000", "1440"), ("8", "8")):
                status, _ = await p.admin_call("PUT", "/api/settings",
                                               {"pin_timeout_computer_minutes": typed})
                assert status == 200 and p.db.settings()["pin_timeout_computer_minutes"] == kept
            assert (await p.admin_call("PUT", "/api/settings",
                                       {"pin_timeout_computer_minutes": "soon"}))[0] == 400
            status, state = await p.admin_call("GET", "/api/state")
            assert state["settings"]["pin_timeout_computer_minutes"] == "8"
            assert state["settings"]["pin_timeout_minutes"] == "15"

            # Backups carry both, and a restore puts them back (through the same checks).
            status, backup = await p.admin_call("GET", "/api/backup")
            assert backup["settings"]["pin_timeout_computer_minutes"] == "8"
            p.db.update_settings({"pin_timeout_computer_minutes": "30", "pin_timeout_minutes": "2"})
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, reply
            s = p.db.settings()
            assert (s["pin_timeout_computer_minutes"], s["pin_timeout_minutes"]) == ("8", "15")
            backup["settings"]["pin_timeout_computer_minutes"] = "99999"
            assert (await p.admin_call("POST", "/api/restore", {"backup": backup}))[0] == 200
            assert p.db.settings()["pin_timeout_computer_minutes"] == "1440"
    run(scenario())


# ---------------------------------------------------------------- new computers hide names

def test_new_shared_computers_hide_names_unless_asked_not_to(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            async def add(**body):
                status, reply = await p.admin_call("POST", "/api/devices", {"name": "X", **body})
                assert status == 200, reply
                return p.db.device(reply["id"])
            assert (await add(kind="computer", signin_mode="staff"))["pin_only"] == 1
            assert (await add(kind="computer", signin_mode="role"))["pin_only"] == 1
            assert (await add(kind="computer", signin_mode="staff", pin_only=False))["pin_only"] == 0
            assert (await add(kind="phone", signin_mode="staff"))["pin_only"] == 0
            assert (await add(kind="phone", signin_mode="role", pin_only=True))["pin_only"] == 1
            assert (await add(kind="computer"))["pin_only"] == 0          # kiosk: no one signs in
            # Devices already set up keep what they have.
            assert (await p.admin_call("PATCH", f"/api/devices/{p.device}",
                                       {"name": "Front Desk PC"}))[0] == 200
            assert p.db.device(p.device)["pin_only"] == 0
    run(scenario())


# ---------------------------------------------------------------- the PIN length

def test_pin_length_is_only_given_out_when_everyone_shares_it(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            desk = {"Authorization": f"Bearer {p.token}"}
            assert (await me(p, desk))["pin_length"] == 4               # Ana, Bob, Cy, Dee
            # Bob gets a 6-digit PIN: lengths differ, so none is given out.
            assert (await p.admin_call("PATCH", f"/api/staff/{p.bob}", {"pin": "222222"}))[0] == 200
            p.pins[p.bob] = "222222"
            assert p.db.staff(p.bob)["pin_length"] == 6
            reply = await me(p, desk)
            assert reply["pin_length"] is None
            # Never anyone's own length on the list of names.
            assert all(set(m) == {"id", "name", "off_clock"} for m in reply["staff"])
            assert "222222" not in json.dumps(reply)
            # Only people allowed here count: Bob alone, then Bob and Ana.
            p.db.update_device(p.device, staff_all=0)
            p.db.set_device_staff(p.device, [p.bob])
            assert (await me(p, desk))["pin_length"] == 6
            p.db.set_device_staff(p.device, [p.bob, p.ana])
            assert (await me(p, desk))["pin_length"] is None
            # Someone turned off doesn't count; nobody at all gives nothing.
            p.db.update_staff(p.ana, status="disabled")
            assert (await me(p, desk))["pin_length"] == 6
            p.db.set_device_staff(p.device, [])
            assert (await me(p, desk))["pin_length"] is None
            # One unknown (or damaged) length means none is given out, PIN-only too.
            p.db.update_device(p.device, staff_all=1, pin_only=1)
            p.db.update_staff(p.ana, status="active")
            p.db.update_staff(p.bob, pin_length=4)                       # as if it were known
            assert (await me(p, desk))["pin_length"] == 4
            p.db.update_staff(p.cy, pin_length=None)
            assert (await me(p, desk))["pin_length"] is None
            p.db.update_staff(p.cy, pin_length=44)
            assert (await me(p, desk))["pin_length"] is None

            # A device's own PIN: its length. No PIN: none.
            status, reply = await p.admin_call("POST", "/api/devices",
                                               {"name": "Pump phone", "pin": "123456"})
            pump = reply["id"]
            assert p.db.device(pump)["pin_length"] == 6
            _, token = p.db.claim_enroll_code(p.db.device(pump)["enroll_code"])
            pump_auth = {"Authorization": f"Bearer {token}"}
            assert (await me(p, pump_auth))["pin_length"] == 6
            assert (await p.admin_call("PATCH", f"/api/devices/{pump}", {"pin": "12345"}))[0] == 200
            assert (await me(p, pump_auth))["pin_length"] == 5
            assert (await p.admin_call("PATCH", f"/api/devices/{pump}", {"pin": ""}))[0] == 200
            assert p.db.device(pump)["pin_length"] is None
            assert (await me(p, pump_auth))["pin_length"] is None
    run(scenario())


def test_a_pin_pad_signing_in_at_an_old_length_is_told_and_nothing_is_counted(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            desk = {"Authorization": f"Bearer {p.token}"}
            assert (await p.admin_call("PATCH", f"/api/staff/{p.bob}", {"pin": "222222"}))[0] == 200
            before = p.db.device(p.device)
            status, reply = await p.call("POST", "/api/unlock", desk,
                                         {"staff_id": p.ana, "pin": "9999", "length": 4})
            assert status == 409 and reply == {"error": "pin_length_changed", "pin_length": None}
            after = p.db.device(p.device)
            assert after["failed_pins"] == before["failed_pins"]
            assert after["failed_day_count"] == before["failed_day_count"]
            assert p.db.staff(p.ana)["failed_pins"] == 0
            # Sent with the right length (or none), the PIN is checked as usual.
            status, reply = await p.call("POST", "/api/unlock", desk,
                                         {"staff_id": p.ana, "pin": "9999", "length": None})
            assert status == 403 and "Wrong PIN" in reply["error"]
            status, reply = await p.call("POST", "/api/unlock", desk,
                                         {"staff_id": p.ana, "pin": "1111"})
            assert status == 200 and reply["session"]
            assert (await p.admin_call("PATCH", f"/api/staff/{p.bob}", {"pin": "2222"}))[0] == 200
            status, reply = await p.call("POST", "/api/unlock", desk,
                                         {"staff_id": p.ana, "pin": "1111", "length": 4})
            assert status == 200 and reply["session"]
    run(scenario())


def test_pin_lengths_are_recorded_wherever_pins_are_set(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        connecteam_on(p)
        users = [ct_user(601, "Eve", code="55555"), ct_user(602, "Finn")]
        async with FakeConnecteam(users) as ct, p:
            status, reply = await p.admin_call("POST", "/api/staff", {"name": "Gil", "pin": "1234567"})
            assert status == 200 and p.db.staff(reply["id"])["pin_length"] == 7
            # The Connecteam sync: a kiosk code's length, or unknown with no usable code.
            await sync(p)
            eve, finn = by_name(p, "Eve"), by_name(p, "Finn")
            assert (eve["pin_length"], finn["pin_length"]) == (5, None)
            ct.users[0]["kioskCode"] = "555555"
            await sync(p)
            assert p.db.staff(eve["id"])["pin_length"] == 6
            main.connecteam._users = None
    run(scenario())


def test_pin_lengths_travel_in_backups_and_staff_files(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        pump, _ = phone(p, "Pump phone", mode="phone", pin="123456")
        async with p:
            assert (await p.admin_call("PATCH", f"/api/staff/{p.bob}", {"pin": "22222"}))[0] == 200
            status, backup = await p.admin_call("GET", "/api/backup")
            assert {m["name"]: m["pin_length"] for m in backup["staff"]}["Bob"] == 5
            assert {d["name"]: d["pin_length"] for d in backup["devices"]}["Pump phone"] == 6
            p.db.update_staff(p.bob, pin_length=None)
            p.db.update_device(pump, pin_length=None)
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, reply
            assert p.db.staff(p.bob)["pin_length"] == 5 and p.db.device(pump)["pin_length"] == 6
            # Older backups don't know lengths, and a damaged one is unknown: never a guess.
            for m in backup["staff"]:
                m.pop("pin_length")
            for d in backup["devices"]:
                d["pin_length"] = "6"
            backup["staff"][0]["pin_length"] = 99
            assert (await p.admin_call("POST", "/api/restore", {"backup": backup}))[0] == 200
            assert all(m["pin_length"] is None for m in p.db.staff_list())
            assert p.db.device(pump)["pin_length"] is None
    run(scenario())

    q = Portal(tmp_path / "other")
    q.db.update_staff(q.bob, pin_length=None)
    staff = transfer.export_staff(q.db, "test")
    assert {m["name"]: m["pin_length"] for m in staff["staff"]} == {
        "Ana": 4, "Bob": None, "Cy": 4, "Dee": 4}
    r = Portal(tmp_path / "third")
    staff["staff"].append({**staff["staff"][0], "name": "Ann", "pin_length": 7})
    staff["staff"].append({**staff["staff"][0], "name": "Abe"})
    del staff["staff"][-1]["pin_length"]                       # a file from before 0.8.3
    r.db.update_staff(r.cy, pin_length=None)
    transfer.import_staff(r.db, staff, "replace")
    lengths = {m["name"]: m["pin_length"] for m in r.db.staff_list()}
    # Replaced people take the file's; Cy's PIN hash differs (another system), so the file's 4.
    assert lengths == {"Ana": 4, "Bob": None, "Cy": 4, "Dee": 4, "Ann": 7, "Abe": None}
    # The same PIN replaced from a file that doesn't say keeps the length known here.
    del staff["staff"][0]["pin_length"]
    staff["staff"][0]["pin_hash"] = r.db.staff(r.ana)["pin_hash"]
    transfer.import_staff(r.db, staff, "replace")
    assert r.db.staff(r.ana)["pin_length"] == 4


# ---------------------------------------------------------------- the staff app's wording

def test_staff_messages_use_the_property_name():
    with open(os.path.join(HERE, "..", "app", "staff", "app.js")) as f:
        source = f.read()
    assert "reach Staff Portal" not in source
    assert len(re.findall(r"Can't reach \$\{portalName\(\)\}", source)) >= 7
    assert 'return me?.property_name || "Staff Portal";' in source


# ---------------------------------------------------------------- Look up: what's on the card

async def look_up(p: Portal, who: dict, place=None) -> dict:
    """Start a look-up (placing a card once it's waiting, if asked) and wait for its answer."""
    status, reply = await p.call("POST", "/api/wristbands/lookup", who)
    assert status == 200, reply
    if place is not None:
        place()
    deadline = time.monotonic() + 10
    while True:
        status, found = await p.call("GET", "/api/wristbands/lookup", who)
        assert status == 200, found
        if found["state"] != "waiting":
            return found
        assert time.monotonic() < deadline, found
        await asyncio.sleep(0.05)


def test_look_up_shows_a_cleared_card_and_revokes_from_there(tmp_path):
    async def scenario():
        async with Simulator() as sim, FakeTTHotel([tt_lock(d) for d in DOORS]):
            p = Portal(tmp_path, encoder_url=f"ws://127.0.0.1:{sim._runner.addresses[0][1]}")
            tthotel_account(p)
            async with p:
                ana, dee, bob = (await p.sign_in(p.ana), await p.sign_in(p.dee),
                                 await p.sign_in(p.bob))
                status, reply = await p.call("POST", "/api/wristbands/jobs", ana, day_pass())
                job_id = reply["job"]["id"]
                await job_until(p, ana, job_id, waiting_for(1))
                first = sim.place_card()
                await job_until(p, ana, job_id, lambda j: j["phase"] == "remove_card")
                sim.remove_card()
                await job_until(p, ana, job_id, waiting_for(2))
                second = sim.place_card()
                await job_until(p, ana, job_id, lambda j: j["status"] == "done")
                sim.remove_card()

                # A card with doors: the card's own doors, and our record. Ana made it, so she may
                # wipe it; Dee may not (she didn't, and doesn't see every card); Bob may.
                found = await look_up(p, ana, lambda: sim.place_card(first))
                assert found["empty"] is False and found["blank"] is False
                assert sorted(r["label"] for r in found["card"]) == ["Pool Gate", "Spa"]
                assert found["can_revoke"] is True and found["job"]["id"] == job_id
                assert (await look_up(p, bob))["can_revoke"] is True
                found = await look_up(p, dee)
                assert found["can_revoke"] is False and found["job"] is None
                status, reply = await p.call("POST", "/api/wristbands/lookup/revoke", dee)
                assert status == 403
                assert ("Dee on Front Desk PC", "Blocked: revoke a card", False) in p.activity()
                assert p.db.wb_job(job_id)["cards"][0]["state"] == "active"
                # Only after a look-up of one's own.
                assert (await p.call("POST", "/api/wristbands/lookup/revoke", ana))[0] == 409

                # Cancelled in TT Hotel's own app: the card is wiped, our record still says
                # active. Look up says so and the record catches up (once).
                sim.cards[first]["records"] = []
                found = await look_up(p, dee)
                assert found["empty"] is True and found["card"] == []
                assert found["can_revoke"] is False and found["job"] is None
                card = p.db.wb_job(job_id)["cards"][0]
                assert card["card_number"] == first
                assert (card["state"], card["state_by"]) == ("revoked", main.FOUND_CLEARED)
                assert ("Dee on Front Desk PC",
                        f"Card {first} (Smith party) found cleared on the encoder",
                        True) in p.activity()
                found = await look_up(p, ana)
                logged = [a for a in p.activity() if "found cleared" in a[1]]
                assert len(logged) == 1
                assert found["job"]["cards"][0]["state"] == "revoked"
                assert found["job"]["cards"][0]["state_by"] == main.FOUND_CLEARED
                assert (await p.call("POST", "/api/wristbands/lookup/revoke", ana))[0] == 409
                sim.remove_card()

                # A card that reads as blank (or another hotel's, or a sector setting changed)
                # has no doors as far as anyone can tell, but its record is left alone: it may
                # still open doors with the right setting.
                ours, sim.cards[second]["hotel"] = sim.cards[second]["hotel"], None
                found = await look_up(p, ana, lambda: sim.place_card(second))
                assert found["empty"] is True and found["blank"] is True
                assert found["can_revoke"] is False
                assert p.db.wb_job(job_id)["cards"][1]["state"] == "active"
                sim.remove_card()

                # Revoke from Look up: the usual revoke job, for the card on the encoder.
                sim.cards[second]["hotel"] = ours
                found = await look_up(p, ana, lambda: sim.place_card(second))
                assert found["can_revoke"] is True and found["card"]
                status, reply = await p.call("POST", "/api/wristbands/lookup/revoke", ana)
                assert status == 200, reply
                revoke = reply["job"]
                assert revoke["kind"] == "revoke" and revoke["target_card"] == second
                await job_until(p, ana, revoke["id"], lambda j: j["status"] == "done")
                assert sim.cards[second]["records"] == []
                card = p.db.wb_job(job_id)["cards"][1]
                assert (card["state"], card["state_by"]) == ("revoked", "Ana on Front Desk PC")
                assert ("Ana on Front Desk PC", f"Revoked card {second} (Smith party)",
                        True) in p.activity()
                sim.remove_card()
    run(scenario())

