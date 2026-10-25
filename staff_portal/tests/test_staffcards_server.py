"""Staff cards on the server: departments, adding everyone from Connecteam (and the per-person
clock-in override), key cards made from the Staff tab, the Key cards list, revoking, lost cards
(notice cards, replacements and the experimental remote lock-out), and backups of all of it.

Nothing here talks to a real TT Hotel, Connecteam or Home Assistant: Connecteam is a small fake
aiohttp server (connecteam.API_OVERRIDE), TT Hotel is the fake from test_wristbands_server (or a
fake lock_out_card), and the encoder is encoder_sim.
"""

import asyncio
import json
import time

from aiohttp import web

from test_wristbands_server import (DOORS, FakeTTHotel, Portal, Simulator, free_port, run,
                                    staff_card, tt_lock, tthotel_account, waiting_for)

import connecteam  # noqa: E402
import main  # noqa: E402
import transfer  # noqa: E402
from db import check_pin  # noqa: E402

FLOOR_ONE = [{"level": "floor", "building": 1, "floor": 1}]   # Rooms 101 and 102
POOL = [{"level": "door", "door": 900}]                       # the Pool Gate (on a gateway)


# ---------------------------------------------------------------- a fake Connecteam

class FakeConnecteam:
    """Just enough of Connecteam's API: a token, the user list and the smart groups."""

    def __init__(self, users=(), groups=()):
        self.users = list(users)
        self.groups = list(groups)
        self.calls: list[str] = []
        self.port = free_port()
        self.app = web.Application()
        for path in ("/oauth/v1/token", "/users/v1/users", "/users/v1/smart-groups"):
            self.app.router.add_route("*", path, self.handle)
        self.runner = None

    async def handle(self, request):
        self.calls.append(request.path)
        if request.path == "/oauth/v1/token":
            return web.json_response({"access_token": "fake", "expires_in": 3600})
        if request.path == "/users/v1/smart-groups":
            return web.json_response({"data": {"groups": self.groups}})
        offset = int(request.query.get("offset", 0))
        return web.json_response({"data": {"users": self.users[offset:offset + 500]}})

    async def __aenter__(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        self.api, connecteam.API_OVERRIDE = connecteam.API_OVERRIDE, f"http://127.0.0.1:{self.port}"
        main.connecteam.forget()
        main.connecteam.paused_until = 0.0
        main.connecteam._recent.clear()
        await main.connecteam.start()
        return self

    async def __aexit__(self, *exc):
        await main.connecteam.close()
        connecteam.API_OVERRIDE = self.api
        await self.runner.cleanup()

    def count(self, path: str) -> int:
        return sum(1 for c in self.calls if c == path)


def ct_user(user_id, first, last="", code=None, groups=()):
    return {"userId": user_id, "firstName": first, "lastName": last, "kioskCode": code,
            "smartGroupsIds": list(groups), "isArchived": False}


def connecteam_on(p: Portal, **extra):
    p.db.update_settings({"connecteam_enabled": "1", "connecteam_client_id": "client",
                          "connecteam_client_secret": "secret", "connecteam_auto_staff": "1",
                          **extra})
    main.staff_sync.update(at=None, result=None, error=None, tried=0.0, soon=False)
    main.kiosk_matches.clear()


async def sync(p: Portal) -> dict:
    """One staff sync, with a fresh user list (as Sync staff now does, ignoring its pause)."""
    main.connecteam._users = None
    return await main.run_staff_sync("Admin", fresh=True)


def by_name(p: Portal, name: str) -> dict:
    return next(m for m in p.db.staff_list() if m["name"] == name)


def department(p: Portal, name: str, **fields) -> int:
    return p.db.create_department(name=name, **fields)


# ---------------------------------------------------------------- departments

def test_departments_from_the_dashboard(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            status, reply = await p.admin_call("POST", "/api/departments", {
                "name": " Housekeeping ", "smart_group_id": "11", "role_id": p.front_desk,
                "card_access": FLOOR_ONE, "card_days": "14", "card_lockout": False})
            assert status == 200, reply
            house = reply["id"]
            dept = p.db.department(house)
            assert (dept["name"], dept["smart_group_id"], dept["role_id"]) == (
                "Housekeeping", "11", p.front_desk)
            assert dept["card_access"] == FLOOR_ONE and dept["card_days"] == 14
            assert dept["card_lockout"] == 0
            # Names are unique (ignoring capitals), and one smart group has one department.
            assert (await p.admin_call("POST", "/api/departments", {"name": "housekeeping"}))[0] == 400
            status, reply = await p.admin_call("POST", "/api/departments",
                                               {"name": "Spa", "smart_group_id": 11})
            assert status == 400 and "Housekeeping" in reply["error"]
            for bad in ({"name": "Spa", "smart_group_id": "eleven"},
                        {"name": "Spa", "card_access": [{"level": "floor", "building": 0, "floor": 1}]},
                        {"name": "Spa", "card_access": [{"level": "everything"}]},
                        {"name": "Spa", "card_access": "all"},
                        {"name": "Spa", "card_days": "soon"},
                        {"name": "Spa", "role_id": 999}):
                assert (await p.admin_call("POST", "/api/departments", bad))[0] == 400, bad
            status, reply = await p.admin_call("POST", "/api/departments",
                                               {"name": "Spa", "card_access": [], "card_days": 99999})
            spa = reply["id"]
            assert p.db.department(spa)["card_access"] is None          # nothing chosen: no card
            assert p.db.department(spa)["card_days"] == transfer.MAX_CARD_DAYS
            status, _ = await p.admin_call("PUT", f"/api/departments/{spa}",
                                           {"card_access": POOL, "card_lockout": True})
            assert status == 200 and p.db.department(spa)["card_access"] == POOL
            assert (await p.admin_call("PUT", "/api/departments/999", {"name": "x"}))[0] == 404
            # Order matters (the first matching department wins), and it's kept.
            assert (await p.admin_call("PUT", "/api/departments/order",
                                       {"order": [spa, house, 12345]}))[0] == 200
            assert [d["id"] for d in p.db.departments()] == [spa, house]
            # People get a department in their Edit dialog; it shows on the Staff tab.
            assert (await p.admin_call("PATCH", f"/api/staff/{p.ana}",
                                       {"department_id": house}))[0] == 200
            assert (await p.admin_call("PATCH", f"/api/staff/{p.ana}",
                                       {"department_id": 999}))[0] == 400
            status, state = await p.admin_call("GET", "/api/state")
            ana = next(m for m in state["staff"] if m["id"] == p.ana)
            assert ana["department_name"] == "Housekeeping" and ana["key_cards"] == 0
            assert [d["name"] for d in state["departments"]] == ["Spa", "Housekeeping"]
            assert next(d for d in state["departments"] if d["id"] == house)["staff_count"] == 1
            # Deleting one leaves its people with no department.
            assert (await p.admin_call("DELETE", f"/api/departments/{house}"))[0] == 200
            assert p.db.staff(p.ana)["department_id"] is None
            assert ("Admin", "Deleted department Housekeeping", True) in p.activity()
            # A damaged template makes no card, never a wider one.
            p.db.conn.execute("UPDATE departments SET card_access = '[{\"level\": \"all\"' "
                              "WHERE id = ?", (spa,))
            assert p.db.department(spa)["card_access"] == []
    run(scenario())


# ---------------------------------------------------------------- adding everyone from Connecteam

def test_connecteam_sync_adds_updates_and_turns_off(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        connecteam_on(p)
        managers = department(p, "Managers", smart_group_id="22", role_id=p.manager)
        house = department(p, "Housekeeping", smart_group_id="11", role_id=p.front_desk)
        # Ana was added by hand and linked to Connecteam: the sync never touches her.
        p.db.update_staff(p.ana, connecteam_user_id="500")
        users = [ct_user(500, "Ana", code="5000", groups=[22]),
                 ct_user(501, "Eve", "Adams", "5555", [11]),
                 ct_user(502, "Bob", code="6666"),                  # Bob is taken: "Bob (2)"
                 ct_user(503, "Finn"),                              # no kiosk code
                 ct_user(504, "Gus", code="1111"),                  # Ana's PIN: not usable
                 ct_user(505, "Hal", code="7777", groups=[11, 22])]  # Managers comes first
        async with FakeConnecteam(users) as ct, p:
            counts = await sync(p)
            assert counts == {"added": 5, "updated": 0, "turned_off": 0, "turned_on": 0,
                              "no_code": 2}
            assert ("Admin", "Synced staff from Connecteam", True) in p.activity()
            assert p.db.activity(1)[0]["detail"] == "5 added, 2 without a kiosk code"
            eve, bob2, finn, gus, hal = (by_name(p, n) for n in
                                         ("Eve Adams", "Bob (2)", "Finn", "Gus", "Hal"))
            assert (eve["status"], eve["connecteam_auto"], eve["require_clock_in"]) == ("active", 1, 1)
            assert eve["department_id"] == house and eve["role_id"] == p.front_desk
            assert hal["department_id"] == managers and hal["role_id"] == p.manager
            assert check_pin("5555", p.db.staff(eve["id"])["pin_hash"])
            assert eve["pin_from_connecteam"] == 1 and bob2["status"] == "active"
            for m in (finn, gus):
                assert m["status"] == "disabled" and m["connecteam_note"] == main.NO_KIOSK_CODE
            ana = p.db.staff(p.ana)
            assert ana["connecteam_auto"] == 0 and ana["department_id"] is None
            assert check_pin("1111", ana["pin_hash"])
            assert ct.count("/users/v1/users") == 1

            # Admin changes by hand: Eve's role and clock-in rule, Bob (2)'s PIN.
            assert (await p.admin_call("PATCH", f"/api/staff/{eve['id']}",
                                       {"role_id": p.no_cards, "require_clock_in": False}))[0] == 200
            eve = p.db.staff(eve["id"])
            assert eve["role_set"] == 1 and eve["clock_in_set"] == 1
            assert eve["require_clock_in"] == 0
            assert p.db.activity(1)[0]["detail"] == "may sign in without clocking in"
            assert (await p.admin_call("PATCH", f"/api/staff/{bob2['id']}", {"pin": "8888"}))[0] == 200
            # Turning on someone with no kiosk code needs a PIN.
            status, reply = await p.admin_call("PATCH", f"/api/staff/{gus['id']}", {"active": True})
            assert status == 400 and "PIN" in reply["error"]
            main.pin_sessions["eve-session"] = (p.device, time.time(), eve["id"])
            main.pin_sessions["bob2-session"] = (p.device, time.time(), bob2["id"])

            users[1] = ct_user(501, "Eve", "Adams", "5556", [22])   # moved, new kiosk code
            users[2] = ct_user(502, "Bob", code="6667")
            users[3] = ct_user(503, "Finn", code="9999")
            users[5] = ct_user(505, "Hal", code="7778", groups=[11, 22])
            ct.users = users
            counts = await sync(p)
            assert counts == {"added": 0, "updated": 2, "turned_off": 0, "turned_on": 1,
                              "no_code": 0}
            eve = p.db.staff(eve["id"])
            assert eve["department_id"] == managers                # follows the smart group
            assert eve["role_id"] == p.no_cards                    # but not the role set by hand
            assert eve["require_clock_in"] == 0                    # nor the clock-in override
            assert check_pin("5556", eve["pin_hash"])               # the PIN follows the code
            assert "eve-session" not in main.pin_sessions           # ...and she signs in again
            assert check_pin("8888", p.db.staff(bob2["id"])["pin_hash"])   # an admin's PIN stays
            assert "bob2-session" in main.pin_sessions
            assert check_pin("7778", p.db.staff(hal["id"])["pin_hash"])
            finn = p.db.staff(finn["id"])
            assert finn["status"] == "active" and finn["connecteam_note"] is None
            assert check_pin("9999", finn["pin_hash"])

            # The clock-in rule for people added from Connecteam follows the setting, unless
            # it was changed for them by hand.
            p.db.update_settings({"connecteam_auto_clock_in": "0"})
            counts = await sync(p)
            assert p.db.staff(hal["id"])["require_clock_in"] == 0
            p.db.update_settings({"connecteam_auto_clock_in": "1"})
            await sync(p)
            assert p.db.staff(hal["id"])["require_clock_in"] == 1
            assert p.db.staff(eve["id"])["require_clock_in"] == 0

            # Archived (no longer listed): turned off and signed out, never deleted; back again
            # when they're listed again. An admin turning someone off is final.
            main.pin_sessions["hal-session"] = (p.device, time.time(), hal["id"])
            ct.users = [u for u in users if u["userId"] != 505]
            counts = await sync(p)
            assert counts["turned_off"] == 1
            hal = p.db.staff(hal["id"])
            assert hal["status"] == "disabled" and hal["connecteam_note"] == main.LEFT_CONNECTEAM
            assert "hal-session" not in main.pin_sessions
            assert p.db.staff(p.ana)["status"] == "active"          # added by hand: untouched
            ct.users = users
            assert (await sync(p))["turned_on"] == 1
            assert p.db.staff(hal["id"])["status"] == "active"
            assert (await p.admin_call("PATCH", f"/api/staff/{finn['id']}", {"active": False}))[0] == 200
            ct.users = [u for u in users if u["userId"] != 503]
            await sync(p)
            ct.users = users
            await sync(p)
            assert p.db.staff(finn["id"])["status"] == "disabled"

            # An empty list never turns everyone off.
            ct.users = []
            before = [(m["id"], m["status"]) for m in p.db.staff_list()]
            try:
                await sync(p)
                raise AssertionError("an empty user list was accepted")
            except main.ConnecteamError as err:
                assert "no active users" in str(err)
            assert [(m["id"], m["status"]) for m in p.db.staff_list()] == before
            assert main.staff_sync_state()["error"]
    run(scenario())


def test_sync_staff_now_and_its_budget(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with FakeConnecteam([ct_user(601, "Ivy", code="4321")],
                                  [{"id": 11, "name": "Housekeeping", "groupSegmentId": 1,
                                    "numberOfUsers": 3},
                                   {"id": "x", "name": "Broken"}]) as ct, p:
            # Off: nothing is asked, and the button says why.
            assert (await p.admin_call("POST", "/api/connecteam/sync-staff"))[0] == 400
            assert (await p.admin_call("GET", "/api/connecteam/smart-groups"))[0] == 400
            assert not main.staff_sync_due() and ct.calls == []
            connecteam_on(p)
            assert main.staff_sync_due()
            status, reply = await p.admin_call("POST", "/api/connecteam/sync-staff")
            assert status == 200 and reply["added"] == 1 and reply["detail"] == "1 added"
            assert by_name(p, "Ivy")["connecteam_auto"] == 1
            # At most once a minute by hand, and the regular sync waits an hour.
            assert (await p.admin_call("POST", "/api/connecteam/sync-staff"))[0] == 429
            assert not main.staff_sync_due()
            main.sync_staff_soon()                         # Save: within a minute of the last
            assert not main.staff_sync_due()
            main.staff_sync["tried"] -= main.STAFF_SYNC_GAP
            assert main.staff_sync_due()
            # Never while Connecteam is stopped (rejected keys) or paused (rate limits).
            main.connecteam.halted = "Connecteam didn't accept the Client ID and Secret."
            assert not main.staff_sync_due()
            main.connecteam.halted = None
            main.connecteam.paused_until = time.time() + 60
            assert not main.staff_sync_due()
            main.connecteam.paused_until = 0.0
            # The hourly sync reuses the cached user list (five minutes), so it costs nothing
            # right after Sync staff now, and an unchanged sync writes nothing to the log.
            users_calls, logged = ct.count("/users/v1/users"), len(p.db.activity(500))
            await main.run_staff_sync()
            assert ct.count("/users/v1/users") == users_calls
            assert len(p.db.activity(500)) == logged
            # Smart groups, for the department picker: cached for hours.
            status, reply = await p.admin_call("GET", "/api/connecteam/smart-groups")
            assert status == 200 and reply["groups"] == [
                {"id": "11", "name": "Housekeeping", "users": 3}]
            assert (await p.admin_call("GET", "/api/connecteam/smart-groups"))[0] == 200
            assert ct.count("/users/v1/smart-groups") == 1
            # The dashboard's state never asks Connecteam.
            calls = len(ct.calls)
            status, state = await p.admin_call("GET", "/api/state")
            assert state["staff_sync"]["enabled"] and len(ct.calls) == calls
            # Saving the settings asks for a sync soon (at most once a minute).
            main.staff_sync["soon"] = False
            status, _ = await p.admin_call("PUT", "/api/settings", {"connecteam_auto_clock_in": "0"})
            assert status == 200 and main.staff_sync["soon"]
    run(scenario())


# ---------------------------------------------------------------- key cards

async def admin_job_until(p: Portal, job_id: int, check, timeout: float = 10.0) -> dict:
    """Poll a card job the way the dashboard does until check(job) holds."""
    deadline = time.monotonic() + timeout
    while True:
        status, reply = await p.admin_call("GET", f"/api/keycards/jobs/{job_id}")
        assert status == 200, reply
        if check(reply["job"]):
            return reply["job"]
        assert time.monotonic() < deadline, reply["job"]
        await asyncio.sleep(0.05)


def done(job):
    return job["status"] == "done"


async def make_card(p: Portal, sim, path: str, body=None, number=None) -> tuple[dict, str]:
    """Start a card job from the dashboard, put a card on the encoder, and wait for it."""
    status, reply = await p.admin_call("POST", path, body or {})
    assert status == 200, reply
    job_id = reply["job"]["id"]
    await admin_job_until(p, job_id, waiting_for(1))
    card = sim.place_card(number)
    job = await admin_job_until(p, job_id, lambda j: j["phase"] == "remove_card" or done(j))
    sim.remove_card()
    job = await admin_job_until(p, job_id, done)
    return job, card


def card_id(p: Portal, number: str) -> int:
    return next(c["id"] for c in p.db.key_cards("all") if c["card_number"] == number)


def test_key_cards_made_from_the_staff_tab(tmp_path):
    async def scenario():
        async with Simulator() as sim, FakeTTHotel([tt_lock(d) for d in DOORS]):
            p = Portal(tmp_path, encoder_url=f"ws://127.0.0.1:{sim._runner.addresses[0][1]}")
            tthotel_account(p)
            house = department(p, "Housekeeping", card_access=FLOOR_ONE, card_days=10,
                               card_lockout=0)
            plain = department(p, "Kitchen")
            p.db.update_staff(p.cy, department_id=house)
            p.db.update_staff(p.dee, department_id=plain)
            async with p:
                # No department, or one without a key card template: nothing to make.
                for who in (p.ana, p.dee):
                    status, reply = await p.admin_call("POST", f"/api/staff/{who}/keycard")
                    assert status == 400 and "department" in reply["error"]
                job, first = await make_card(p, sim, f"/api/staff/{p.cy}/keycard")
                assert job["made_by"] == "Admin" and job["source"] == "dashboard"
                assert job["holder_staff_id"] == p.cy
                record = p.db.wb_job(job["id"])
                assert record["department_id"] == house and record["allow_lockout"] == 0
                assert [r["level"] for r in record["records"]] == ["floor"]
                assert ("Admin", "Made a key card for Cy", True) in p.activity()
                status, listing = await p.admin_call("GET", "/api/keycards")
                card = next(c for c in listing["cards"] if c["card_number"] == first)
                assert (card["holder"], card["department"], card["state"]) == (
                    "Cy", "Housekeeping", "active")
                assert listing["encoders"][0]["device_id"] == p.device
                assert listing["remote"]["ready"] is False and listing["remote"]["why"]
                status, state = await p.admin_call("GET", "/api/state")
                assert next(m for m in state["staff"] if m["id"] == p.cy)["key_cards"] == 1

                # They already have one: replace it, or add another?
                status, reply = await p.admin_call("POST", f"/api/staff/{p.cy}/keycard")
                assert status == 409 and reply["needs_choice"]
                assert reply["current"][0]["card_number"] == first
                job, second = await make_card(p, sim, f"/api/staff/{p.cy}/keycard",
                                              {"mode": "replace"})
                old, new = p.db.key_card(card_id(p, first)), p.db.key_card(card_id(p, second))
                assert old["state"] == "revoked" and second in old["state_by"]
                assert new["cancelled_card"] == first
                assert [c["cardNo"] for c in sim.cards[second]["cancels"]] == [first]
                assert {lo["lock_id"] for lo in old["lockouts"]} == {101, 102}
                assert {lo["method"] for lo in old["lockouts"]} == {"replacement"}
                job, third = await make_card(p, sim, f"/api/staff/{p.cy}/keycard", {"mode": "add"})
                assert p.db.key_card(card_id(p, second))["state"] == "active"

                # Revoke (card in hand): only that card is cleared.
                status, reply = await p.admin_call("POST", f"/api/keycards/{card_id(p, third)}/revoke")
                assert status == 200, reply
                job_id = reply["job"]["id"]
                assert reply["job"]["kind"] == "revoke"
                await admin_job_until(p, job_id, waiting_for(1))
                sim.place_card(second)                                 # the wrong card
                job = await admin_job_until(p, job_id, lambda j: second in (j["message"] or ""))
                assert third in job["message"]
                sim.remove_card()
                if job["status"] == "needs_attention":
                    assert (await p.admin_call("POST", f"/api/keycards/jobs/{job_id}/retry"))[0] == 200
                await admin_job_until(p, job_id, waiting_for(1))
                sim.place_card(third)
                await admin_job_until(p, job_id, done)
                sim.remove_card()
                assert p.db.key_card(card_id(p, third))["state"] == "revoked"
                assert sim.cards[third]["records"] == []
                assert p.db.key_card(card_id(p, second))["state"] == "active"
                assert ("Admin", f"Revoked key card {third} (Cy)", True) in p.activity()
                status, reply = await p.admin_call("POST", f"/api/keycards/{card_id(p, third)}/revoke")
                assert status == 409

                # The list: filters and search.
                async def numbers(query):
                    status, reply = await p.admin_call("GET", f"/api/keycards?{query}")
                    assert status == 200, reply
                    return sorted(c["card_number"] for c in reply["cards"])
                assert await numbers("state=active") == [second]
                assert await numbers("state=revoked") == sorted([first, third])
                assert await numbers("state=all") == sorted([first, second, third])
                assert await numbers(f"state=all&q={second}") == [second]
                assert await numbers("state=all&q=cy") == sorted([first, second, third])
                assert await numbers("state=all&q=nobody") == []
                assert (await p.admin_call("GET", "/api/keycards?state=gone"))[0] == 400
                assert (await p.admin_call("GET", "/api/keycards/99999"))[0] == 404
    run(scenario())


def test_lost_cards_notice_and_staff_app_revoke(tmp_path):
    async def scenario():
        async with Simulator() as sim, FakeTTHotel([tt_lock(d) for d in DOORS]):
            p = Portal(tmp_path, encoder_url=f"ws://127.0.0.1:{sim._runner.addresses[0][1]}")
            tthotel_account(p)
            house = department(p, "Housekeeping", card_access=FLOOR_ONE, card_days=10)
            p.db.update_staff(p.cy, department_id=house)
            async with p:
                job, lost = await make_card(p, sim, f"/api/staff/{p.cy}/keycard")
                lost_id = card_id(p, lost)
                # A notice or replacement is only for a card reported lost.
                assert (await p.admin_call("POST", f"/api/keycards/{lost_id}/notice"))[0] == 409
                status, reply = await p.admin_call("POST", f"/api/keycards/{lost_id}/lost")
                assert status == 200 and reply["card"]["state"] == "lost"
                assert {d["id"] for d in reply["card"]["doors"]} == {101, 102}
                assert (await p.admin_call("POST", f"/api/keycards/{lost_id}/lost"))[0] == 409
                assert ("Admin", f"Reported key card {lost} (Cy) lost", True) in p.activity()

                # A lost card notice on a spare card: the doors to tap it on are listed.
                job, spare = await make_card(p, sim, f"/api/keycards/{lost_id}/notice")
                assert job["kind"] == "notice"
                assert [c["cardNo"] for c in sim.cards[spare]["cancels"]] == [lost]
                assert sim.cards[spare]["records"] == []
                card = p.db.key_card(lost_id)
                assert card["state"] == "lost"
                assert {(lo["lock_id"], lo["method"], lo["status"]) for lo in card["lockouts"]} == {
                    (101, "notice", "pending"), (102, "notice", "pending")}
                status, reply = await p.admin_call("PATCH", f"/api/keycards/{lost_id}/lockouts/101",
                                                   {"method": "notice", "done": True})
                assert status == 200
                ticks = {lo["lock_id"]: lo["status"] for lo in reply["card"]["lockouts"]}
                assert ticks == {101: "done", 102: "pending"}
                assert (await p.admin_call("PATCH", f"/api/keycards/{lost_id}/lockouts/900",
                                           {"done": True}))[0] == 404
                status, listing = await p.admin_call("GET", "/api/keycards?state=lost")
                assert [c["card_number"] for c in listing["cards"]] == [lost]
                assert listing["cards"][0]["doors"]

                # A replacement for the lost card carries its cancellation; it stays lost.
                job, replacement = await make_card(p, sim, f"/api/keycards/{lost_id}/replace")
                assert p.db.key_card(card_id(p, replacement))["cancelled_card"] == lost
                assert p.db.key_card(card_id(p, replacement))["holder_staff_id"] == p.cy
                assert p.db.key_card(lost_id)["state"] == "lost"

                # A staff card made in the staff app is matched to its person by name, and
                # people who may see every card can revoke it there (card in hand).
                bob, ana = await p.sign_in(p.bob), await p.sign_in(p.ana)
                status, reply = await p.call("POST", "/api/wristbands/jobs", bob,
                                             staff_card(POOL, name="  cy "))
                assert status == 200, reply
                job_id = reply["job"]["id"]
                assert p.db.wb_job(job_id)["holder_staff_id"] == p.cy
                assert p.db.wb_job(job_id)["source"] == "staff_app"
                await admin_job_until(p, job_id, waiting_for(1))
                pool = sim.place_card()
                await admin_job_until(p, job_id, lambda j: j["phase"] == "remove_card" or done(j))
                sim.remove_card()
                await admin_job_until(p, job_id, done)
                status, log = await p.call("GET", "/api/wristbands/log", bob)
                logged = next(j for j in log["jobs"] if j["id"] == job_id)
                assert logged["cards"][0]["state"] == "active" and logged["cards"][0]["id"]
                pool_id = card_id(p, pool)
                status, reply = await p.call("POST", f"/api/wristbands/cards/{pool_id}/revoke", ana)
                assert status == 403
                assert ("Ana on Front Desk PC", "Blocked: revoke a key card", False) in p.activity()
                status, reply = await p.call("POST", f"/api/wristbands/cards/{lost_id}/revoke", bob)
                assert status == 409                                  # lost: not in anyone's hand
                status, reply = await p.call("POST", f"/api/wristbands/cards/{pool_id}/revoke", bob)
                assert status == 200, reply
                job_id = reply["job"]["id"]
                await admin_job_until(p, job_id, waiting_for(1))
                sim.place_card(pool)
                await admin_job_until(p, job_id, done)
                sim.remove_card()
                assert p.db.key_card(pool_id)["state"] == "revoked"
                assert p.db.key_card(pool_id)["state_by"] == "Bob on Front Desk PC"
    run(scenario())


def add_card(p: Portal, number: str, records, holder=None, expires_in=86400) -> int:
    """A staff card as if it had been made on the encoder."""
    job_id = p.db.add_wb_job(card_type="staff", name="Eve", count=1, records=records,
                             summary="test", expires_at=time.time() + expires_in,
                             allow_lockout=True, staff_id=None, device_id=p.device, actor="Admin",
                             role=None, ip=None, encoder_url="ws://127.0.0.1:1",
                             holder_staff_id=holder, source="dashboard")
    p.db.update_wb_card(job_id, 1, status="written", card_number=number, written_at=time.time())
    return p.db.wb_job(job_id)["cards"][0]["id"]


def door_records(*lock_ids):
    doors = {d["lock_id"]: d for d in DOORS}
    return [{"level": "door", "building": doors[i]["building_number"],
             "floor": doors[i]["floor_number"], "mac": doors[i]["mac"], "door": i,
             "label": doors[i]["name"]} for i in lock_ids]


def test_remote_lock_out(tmp_path, monkeypatch):
    calls, answers = [], []

    async def fake_lock_out(lock_id, card_number, *, api="reversed", card_id=None,
                            find_first=False):
        calls.append((lock_id, card_number, api, card_id, find_first))
        return answers.pop(0) if answers else {"status": "done", "card_id": 7}
    monkeypatch.setattr(main.tthotel, "lock_out_card", fake_lock_out, raising=False)
    monkeypatch.setattr(main, "REMOTE_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(main, "REMOTE_PACE_SECONDS", 0)

    async def until_finished(p, card):
        deadline = time.monotonic() + 5
        while card in main.remote_running:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        return p.db.key_card(card)

    async def scenario():
        p = Portal(tmp_path)
        card = add_card(p, "123456789", door_records(900, 101), holder=p.cy)
        async with p:
            # Off by default, then untested: shown, but it can't be used.
            status, reply = await p.admin_call("POST", f"/api/keycards/{card}/lockout")
            assert status == 409 and "turned off" in reply["error"]
            assert (await p.admin_call("POST", "/api/keycards/remote-test/passed"))[0] == 400
            assert (await p.admin_call("PUT", "/api/settings", {"wb_remote_lockout": "1"}))[0] == 200
            status, reply = await p.admin_call("POST", f"/api/keycards/{card}/lockout")
            assert status == 409 and "Test" in reply["error"]
            # The test helper: it still opened the door, so the other number format is next.
            status, reply = await p.admin_call("POST", "/api/keycards/remote-test/failed")
            assert reply["remote"]["api"] == "plain" and not reply["remote"]["ready"]
            status, reply = await p.admin_call("POST", "/api/keycards/remote-test/lockout")
            assert status == 409                                  # no test card made yet
            status, reply = await p.admin_call("POST", "/api/keycards/remote-test/start",
                                               {"door_id": 101})
            assert status == 400 and "gateway" in reply["error"]
            # Passed only after the test card was locked out, with the format used now.
            status, reply = await p.admin_call("POST", "/api/keycards/remote-test/passed")
            assert status == 409 and not main.remote_state()["ready"]
            main.remote_test["locked_out"] = "reversed"
            assert (await p.admin_call("POST", "/api/keycards/remote-test/passed"))[0] == 409
            main.remote_test["locked_out"] = "plain"
            status, reply = await p.admin_call("POST", "/api/keycards/remote-test/passed")
            assert reply["remote"]["ready"] and reply["remote"]["tested_at"]
            assert main.remote_test["locked_out"] is None
            # Tested is never taken from the settings form, and changing the format resets it.
            await p.admin_call("PUT", "/api/settings", {"wb_remote_lockout_tested": ""})
            assert main.remote_state()["ready"]
            assert (await p.admin_call("PUT", "/api/settings",
                                       {"wb_remote_lockout_api": "sideways"}))[0] == 400
            await p.admin_call("PUT", "/api/settings", {"wb_remote_lockout_api": "reversed"})
            assert not main.remote_state()["ready"]
            main.remote_test["locked_out"] = "reversed"
            await p.admin_call("POST", "/api/keycards/remote-test/passed")

            # Only for a card reported lost; only its gateway doors; two calls a door, in
            # tthotel. A busy gateway is tried again a few times, then given up on.
            status, reply = await p.admin_call("POST", f"/api/keycards/{card}/lockout")
            assert status == 409 and "lost" in reply["error"]
            await p.admin_call("POST", f"/api/keycards/{card}/lost")
            answers[:] = [{"status": "retry", "card_id": None, "message": "The door is busy."}] * 5
            status, reply = await p.admin_call("POST", f"/api/keycards/{card}/lockout")
            assert status == 200, reply
            assert reply["card"]["remote_running"]
            assert (await p.admin_call("POST", f"/api/keycards/{card}/lockout"))[0] == 409
            result = await until_finished(p, card)
            assert [c[0] for c in calls] == [900] * main.REMOTE_TRIES     # never a storm
            assert calls[0][2] == "reversed"
            remote = next(lo for lo in result["lockouts"] if lo["method"] == "remote")
            assert (remote["lock_id"], remote["status"], remote["tries"]) == (
                900, "failed", main.REMOTE_TRIES)
            assert "busy" in remote["detail"]
            # Again: the door was tried before, so the card is looked for there first; this
            # time the first try enrolled the card, so the next only deletes it.
            calls.clear()
            answers[:] = [{"status": "retry", "card_id": 55, "message": "No answer."},
                          {"status": "done", "card_id": 55}]
            assert (await p.admin_call("POST", f"/api/keycards/{card}/lockout"))[0] == 200
            result = await until_finished(p, card)
            assert [(c[3], c[4]) for c in calls] == [(None, True), (55, False)]
            remote = next(lo for lo in result["lockouts"] if lo["method"] == "remote")
            assert remote["status"] == "done" and "for good" in remote["detail"]
            # Nothing left to lock out remotely.
            status, reply = await p.admin_call("POST", f"/api/keycards/{card}/lockout")
            assert status == 400 and "gateway" in reply["error"]
            assert any(a[1].startswith("Locked key card 123456789") for a in p.activity())
            # An expired card can't open anything: no lock-out needed.
            old = add_card(p, "222", door_records(900), expires_in=-60)
            await p.admin_call("POST", f"/api/keycards/{old}/lost")
            assert (await p.admin_call("POST", f"/api/keycards/{old}/lockout"))[0] == 409
            status, listing = await p.admin_call("GET", "/api/keycards?state=expired")
            assert listing["cards"] == []
    run(scenario())


def test_a_busy_gateway_does_not_hold_up_other_doors(tmp_path, monkeypatch):
    calls = []

    async def fake_lock_out(lock_id, card_number, *, api="reversed", card_id=None,
                            find_first=False):
        calls.append((lock_id, card_id))
        if lock_id == 900 and len([c for c in calls if c[0] == 900]) == 1:
            return {"status": "retry", "card_id": 41, "message": "The door is busy."}
        return {"status": "done", "card_id": card_id or 42}
    monkeypatch.setattr(main.tthotel, "lock_out_card", fake_lock_out, raising=False)
    monkeypatch.setattr(main, "REMOTE_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(main, "REMOTE_PACE_SECONDS", 0)

    async def scenario():
        p = Portal(tmp_path)
        card_id = add_card(p, "123456789", door_records(900, 101), holder=p.cy)
        async with p:
            p.db.add_lockouts(card_id, [900, 101], "remote")
            main.remote_running.add(card_id)
            await main.remote_lockout_run(p.db.key_card(card_id), [900, 101], "Admin")
            # The other door went straight after the busy one, which was tried again last
            # (only the delete: the card was already on it).
            assert calls == [(900, None), (101, None), (900, 41)]
            card = p.db.key_card(card_id)
            assert {lo["lock_id"]: lo["status"] for lo in card["lockouts"]} == {900: "done",
                                                                               101: "done"}
            assert card_id not in main.remote_running
    run(scenario())


# ---------------------------------------------------------------- backups, exports, imports

def test_departments_and_new_staff_fields_in_backups(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        house = department(p, "Housekeeping", smart_group_id="11", role_id=p.front_desk,
                           card_access=FLOOR_ONE, card_days=14, card_lockout=0)
        p.db.update_staff(p.cy, department_id=house, connecteam_user_id="77", connecteam_auto=1,
                          clock_in_set=1, role_set=1, pin_from_connecteam=1,
                          connecteam_note=main.NO_KIOSK_CODE)
        p.db.update_settings({"wb_remote_lockout": "1", "wb_remote_lockout_tested": "1700000000",
                              "connecteam_auto_staff": "1"})
        async with p:
            status, backup = await p.admin_call("GET", "/api/backup")
            assert status == 200 and backup["schema"] == 3
            assert [d["name"] for d in backup["departments"]] == ["Housekeeping"]
            p.db.delete_department(house)
            p.db.update_staff(p.cy, connecteam_auto=0, clock_in_set=0, role_set=0)
            p.db.update_settings({"wb_remote_lockout_tested": "", "connecteam_auto_staff": "0"})
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, reply
            dept = p.db.department(house)
            assert dept["card_access"] == FLOOR_ONE and dept["smart_group_id"] == "11"
            assert dept["role_id"] == p.front_desk and dept["card_lockout"] == 0
            cy = p.db.staff(p.cy)
            assert (cy["department_id"], cy["connecteam_auto"], cy["clock_in_set"],
                    cy["role_set"], cy["pin_from_connecteam"], cy["connecteam_note"]) == (
                house, 1, 1, 1, 1, main.NO_KIOSK_CODE)
            assert p.db.settings()["connecteam_auto_staff"] == "1"
            # Whether remote lock-out was tested never comes from a file.
            assert p.db.settings()["wb_remote_lockout_tested"] == ""

            # A backup from before departments: the ones here stay; people come back with the
            # defaults (added by hand, no department).
            old = json.loads(json.dumps(backup))
            old.pop("departments")
            for row in old["staff"]:
                for key in ("department_id", "connecteam_auto", "clock_in_set", "role_set",
                            "pin_from_connecteam", "connecteam_note"):
                    row.pop(key)
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": old})
            assert status == 200, reply
            assert p.db.department(house)["name"] == "Housekeeping"
            cy = p.db.staff(p.cy)
            assert (cy["department_id"], cy["connecteam_auto"], cy["role_set"]) == (None, 0, 0)

            # Damaged templates in a file make no card; repeated smart groups are dropped.
            bad = json.loads(json.dumps(backup))
            bad["departments"][0]["card_access"] = '[{"level": "all"'
            bad["departments"].append({**bad["departments"][0], "id": house + 1, "name": "Spa",
                                       "card_access": [{"level": "door", "door": 0}]})
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": bad})
            assert status == 200, reply
            assert [d["card_access"] for d in p.db.departments()] == [[], []]
            assert [d["smart_group_id"] for d in p.db.departments()] == ["11", None]
            bad["departments"][1]["name"] = "housekeeping"
            assert (await p.admin_call("POST", "/api/restore/preview", {"backup": bad}))[0] == 400

            # Exports and imports: departments by name, people's departments by name.
            status, _ = await p.admin_call("POST", "/api/restore", {"backup": backup})
            status, exported = await p.admin_call("GET", "/api/export/departments")
            assert status == 200 and exported["departments"][0]["role"] == "Front Desk"
            staff = transfer.export_staff(p.db, "test")
            assert next(m for m in staff["staff"] if m["name"] == "Cy")["department"] == "Housekeeping"
            q = Portal(tmp_path / "other")
            async with q:
                status, reply = await q.admin_call("POST", "/api/import/departments",
                                                   {"file": exported, "mode": "replace"})
                assert status == 200 and reply["added"] == 1, reply
                dept = q.db.departments()[0]
                assert (dept["name"], dept["role_id"], dept["card_access"]) == (
                    "Housekeeping", q.front_desk, FLOOR_ONE)
                status, reply = await q.admin_call("POST", "/api/import/departments",
                                                   {"file": exported, "mode": "copy"})
                assert reply["added"] == 1
                names = [d["name"] for d in q.db.departments()]
                assert names == ["Housekeeping", "Housekeeping (imported)"]
                assert q.db.departments()[1]["smart_group_id"] is None   # one group, one department
                result = transfer.import_staff(q.db, staff, "replace")
                assert q.db.staff(q.cy)["department_id"] == dept["id"]
                assert result["missing_departments"] == []
                assert q.db.staff(q.cy)["connecteam_auto"] == 0        # imports are by hand
            q.db.conn.close()
    run(scenario())


# ---------------------------------------------------------------- review fixes

def test_sync_checks_few_pins_and_never_reuses_a_pin_given_meanwhile(tmp_path, monkeypatch):
    async def scenario():
        p = Portal(tmp_path)
        connecteam_on(p)
        users = [ct_user(700 + i, f"Person {i}", code=f"70{i:02d}") for i in range(8)]
        async with FakeConnecteam(users) as ct, p:
            assert (await sync(p))["added"] == 8
            # An admin gives one of them a PIN of their own: the sync never checks it again.
            mine = by_name(p, "Person 0")["id"]
            assert (await p.admin_call("PATCH", f"/api/staff/{mine}", {"pin": "9090"}))[0] == 200
            checks = []
            real_check = main.check_pin
            monkeypatch.setattr(main, "check_pin", lambda pin, stored: (
                checks.append(pin), real_check(pin, stored))[1])
            # After a restart (nothing remembered), each person's code is checked against their
            # own PIN only: one check each, not one for every PIN here.
            main.kiosk_matches.clear()
            assert (await sync(p))["updated"] == 0
            assert len(checks) == 7 and "7000" not in checks
            checks.clear()
            assert (await sync(p))["updated"] == 0 and checks == []

            # Zed's kiosk code is given to Bob by hand while the sync is checking PINs: Zed
            # isn't given it too (the PIN alone says who someone is on PIN-only devices).
            real_thread = asyncio.to_thread
            given = []

            async def to_thread(fn, *args):
                result = await real_thread(fn, *args)
                if not given and isinstance(result, list) and result and isinstance(
                        result[0], str):   # the new people's PINs were just made
                    given.append(True)
                    p.db.update_staff(p.bob, pin_hash=main.hash_pin("8080"))
                return result
            monkeypatch.setattr(main.asyncio, "to_thread", to_thread)
            ct.users = users + [ct_user(799, "Zed", code="8080")]
            assert (await sync(p))["added"] == 0     # not this time...
            monkeypatch.setattr(main.asyncio, "to_thread", real_thread)
            counts = await sync(p)                   # ...then without a PIN, turned off
            assert counts["added"] == 1 and counts["no_code"] == 1
            zed = by_name(p, "Zed")
            assert zed["status"] == "disabled" and zed["connecteam_note"] == main.NO_KIOSK_CODE
            owners = [m["id"] for m in p.db.staff_list()
                      if real_check("8080", p.db.staff(m["id"])["pin_hash"])]
            assert owners == [p.bob]
            # The same when an admin types a PIN while a sync is giving it out.
            assert await main.pin_taken("7001")
    run(scenario())


def test_sync_takes_people_out_of_a_department_they_left(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        connecteam_on(p)
        managers = department(p, "Managers", smart_group_id="22", role_id=p.manager)
        spa = department(p, "Spa", role_id=p.front_desk)          # no smart group: by hand
        users = [ct_user(801, "Eve", code="8101", groups=[22]),
                 ct_user(802, "Ivy", code="8102", groups=[22])]
        async with FakeConnecteam(users) as ct, p:
            await sync(p)
            eve, ivy = by_name(p, "Eve"), by_name(p, "Ivy")
            assert (eve["department_id"], eve["role_id"]) == (managers, p.manager)
            p.db.update_staff(ivy["id"], department_id=spa)       # chosen by hand
            ct.users = [ct_user(801, "Eve", code="8101", groups=[33]),
                        ct_user(802, "Ivy", code="8102")]
            assert (await sync(p))["updated"] == 1
            eve = p.db.staff(eve["id"])
            assert (eve["department_id"], eve["role_id"]) == (None, None)   # no Manager role
            assert p.db.staff(ivy["id"])["department_id"] == spa
            # A role an admin chose stays, out of the department too.
            ct.users = users
            await sync(p)
            assert p.db.staff(eve["id"])["role_id"] == p.manager
            await p.admin_call("PATCH", f"/api/staff/{eve['id']}", {"role_id": p.front_desk})
            ct.users = [ct_user(801, "Eve", code="8101")]
            await sync(p)
            eve = p.db.staff(eve["id"])
            assert (eve["department_id"], eve["role_id"]) == (None, p.front_desk)
    run(scenario())


def test_saving_the_edit_dialog_keeps_the_syncs_note(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        connecteam_on(p)
        users = [ct_user(901, "Finn"), ct_user(902, "Ivy", code="9202")]
        async with FakeConnecteam(users) as ct, p:
            await sync(p)
            ct.users = [users[0]]
            await sync(p)                                         # Ivy archived: turned off
            finn, ivy = by_name(p, "Finn"), by_name(p, "Ivy")
            assert ivy["connecteam_note"] == main.LEFT_CONNECTEAM
            # The Edit dialog saved without turning them on (it always sends "active").
            for m in (finn, ivy):
                status, _ = await p.admin_call("PATCH", f"/api/staff/{m['id']}", {
                    "name": m["name"], "active": False, "require_clock_in": False,
                    "connecteam_user_id": m["connecteam_user_id"]})
                assert status == 200
            assert p.db.staff(finn["id"])["connecteam_note"] == main.NO_KIOSK_CODE
            assert p.db.staff(ivy["id"])["connecteam_note"] == main.LEFT_CONNECTEAM
            # So the sync still turns them on: Finn has a kiosk code now, Ivy is back.
            ct.users = [ct_user(901, "Finn", code="9101"), users[1]]
            assert (await sync(p))["turned_on"] == 2
            finn = p.db.staff(finn["id"])
            assert finn["status"] == "active" and check_pin("9101", finn["pin_hash"])
            assert p.db.staff(ivy["id"])["status"] == "active"
            # Turned off by hand is final, as before.
            await p.admin_call("PATCH", f"/api/staff/{ivy['id']}", {"active": False})
            assert p.db.staff(ivy["id"])["connecteam_note"] is None
    run(scenario())


def test_people_the_sync_added_stay_linked_while_it_runs(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        connecteam_on(p)
        async with FakeConnecteam([ct_user(951, "Ivy", code="9511")]), p:
            await sync(p)
            ivy = by_name(p, "Ivy")
            status, reply = await p.admin_call("PATCH", f"/api/staff/{ivy['id']}",
                                               {"connecteam_user_id": None})
            assert status == 400 and "Archive them in Connecteam" in reply["error"]
            assert p.db.staff(ivy["id"])["connecteam_user_id"] == "951"
            assert (await sync(p))["added"] == 0
            # With adding everyone off, unlinking makes them someone added by hand.
            p.db.update_settings({"connecteam_auto_staff": "0"})
            status, _ = await p.admin_call("PATCH", f"/api/staff/{ivy['id']}",
                                           {"connecteam_user_id": None})
            ivy = p.db.staff(ivy["id"])
            assert status == 200 and (ivy["connecteam_user_id"], ivy["connecteam_auto"]) == (
                None, 0)
    run(scenario())


def test_restores_keep_remote_lock_out_and_department_roles_honest(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        house = department(p, "Housekeeping", role_id=p.front_desk)
        spa = department(p, "Spa", role_id=p.no_cards)
        p.db.update_settings({"wb_remote_lockout": "1", "wb_remote_lockout_api": "reversed"})
        async with p:
            status, backup = await p.admin_call("GET", "/api/backup")
            # Tested with the other number format since: the backup's format was never seen to
            # work, so it isn't "tested" after the restore.
            p.db.update_settings({"wb_remote_lockout_api": "plain",
                                  "wb_remote_lockout_tested": "1700000000"})
            assert (await p.admin_call("POST", "/api/restore", {"backup": backup}))[0] == 200
            assert main.remote_state()["api"] == "reversed" and not main.remote_state()["ready"]
            # The same format: still tested.
            p.db.update_settings({"wb_remote_lockout_tested": "1700000000"})
            assert (await p.admin_call("POST", "/api/restore", {"backup": backup}))[0] == 200
            assert main.remote_state()["ready"]

            # A backup from before departments whose role IDs belong to other roles: the
            # departments here keep their roles by name.
            old = json.loads(json.dumps(backup))
            old.pop("departments")
            for role in old["roles"]:
                role["name"] = {p.front_desk: "Manager", p.manager: "Front Desk",
                                p.no_cards: "Cleaners"}[role["id"]]
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": old})
            assert status == 200, reply
            assert p.db.department(house)["role_id"] == p.manager     # "Front Desk" now
            assert p.db.department(spa)["role_id"] is None             # no "Housekeeping" role
    run(scenario())


def test_remote_lock_out_waits_for_our_own_minute_cap(tmp_path, monkeypatch):
    calls = []

    async def fake_lock_out(lock_id, card_number, *, api="reversed", card_id=None,
                            find_first=False):
        calls.append((lock_id, time.monotonic()))
        if len(calls) <= 2:
            return {"status": "retry", "card_id": None, "wait": True,
                    "message": "Staff Access has made enough TT Hotel requests for this minute."}
        return {"status": "done", "card_id": 42}
    monkeypatch.setattr(main.tthotel, "lock_out_card", fake_lock_out, raising=False)
    monkeypatch.setattr(main, "REMOTE_RETRY_SECONDS", 30)     # no second round needed
    monkeypatch.setattr(main, "REMOTE_MINUTE_WAIT", 0.01)
    monkeypatch.setattr(main, "REMOTE_PACE_SECONDS", 0.05)

    async def scenario():
        p = Portal(tmp_path)
        card_id = add_card(p, "123456789", door_records(900, 101), holder=p.cy)
        async with p:
            p.db.add_lockouts(card_id, [900, 101], "remote")
            main.remote_running.add(card_id)
            await asyncio.wait_for(main.remote_lockout_run(p.db.key_card(card_id), [900, 101],
                                                           "Admin"), 5)
            # Held back twice by our own cap: waited, not counted as tries, never failed.
            assert [c[0] for c in calls] == [900, 900, 900, 101]
            assert calls[3][1] - calls[2][1] >= 0.05             # doors are paced
            card = p.db.key_card(card_id)
            assert {lo["lock_id"]: (lo["status"], lo["tries"]) for lo in card["lockouts"]} == {
                900: ("done", 1), 101: ("done", 1)}
    run(scenario())


def test_sync_fills_in_missing_pin_lengths_once(tmp_path, monkeypatch):
    async def scenario():
        p = Portal(tmp_path)
        connecteam_on(p)
        users = [ct_user(901, "Eve", code="90123"), ct_user(902, "Ivy", code="9020")]
        async with FakeConnecteam(users) as ct, p:
            await sync(p)
            eve, ivy = by_name(p, "Eve"), by_name(p, "Ivy")
            # As if their PINs were set before lengths were recorded; Ivy's PIN was changed by
            # hand, so her kiosk code isn't her PIN any more.
            p.db.update_staff(eve["id"], pin_length=None)
            p.db.update_staff(ivy["id"], pin_hash=main.hash_pin("1234"), pin_length=None,
                              pin_from_connecteam=0)
            main.pin_length_tried.clear()
            await sync(p)
            assert p.db.staff(eve["id"])["pin_length"] == 5
            assert p.db.staff(ivy["id"])["pin_length"] is None
            # A kiosk code that isn't the PIN is only checked once.
            checks = []
            real = main.check_pin
            monkeypatch.setattr(main, "check_pin", lambda *a: checks.append(a) or real(*a))
            await sync(p)
            assert not any(code == "9020" for code, _ in checks)
    run(scenario())
