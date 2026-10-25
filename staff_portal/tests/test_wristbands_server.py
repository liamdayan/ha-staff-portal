"""Wristbands in the add-on's two web servers: permissions on every staff route, settings and
secrets, backups, and a card job from start to finish against the encoder simulator.

Nothing here talks to a real TT Hotel, Connecteam or Home Assistant: TT Hotel is a small fake
aiohttp server (TTHOTEL_API), the encoder is encoder_sim, and Home Assistant is never asked
(the time zone is set in Settings, and the logbook is turned off).
"""

import asyncio
import json
import os
import socket
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
sys.path.insert(0, APP)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Set before main is imported: its database and the TT Hotel address are read at import. Never
# left to whatever the environment says, so a test can't touch a real data folder or account.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="staff-portal-test-")
TT_PORT = free_port()
os.environ["TTHOTEL_API"] = f"http://127.0.0.1:{TT_PORT}"

import main  # noqa: E402
import transfer  # noqa: E402
import tthotel  # noqa: E402
import discovery  # noqa: E402
import encoder_sim  # noqa: E402
from db import Database  # noqa: E402

ADMIN_HEADERS = {"X-Requested-With": "staff-portal"}
DAY_DOORS = [900, 901]          # Pool Gate, Spa Door
OVERNIGHT_DOORS = [900]
FRONT_DESK = {"types": ["day_pass", "overnight"], "staff_levels": [], "doors": {"all": True},
              "edit_doors": False, "max_nights": 3, "max_staff_days": 0, "see_all_cards": False}
MANAGER = {"types": ["day_pass", "overnight", "staff"],
           "staff_levels": ["door", "floor", "building", "all"], "doors": {"all": True},
           "edit_doors": True, "max_nights": 0, "max_staff_days": 30, "see_all_cards": True}
DOORS = [
    {"lock_id": 101, "name": "Room 101", "alias": "", "mac": "C540E09C8C01", "building_number": 1,
     "building_name": "Lodge", "floor_number": 1, "floor_name": "First", "battery": 90,
     "has_gateway": False, "lock_type": 1},
    {"lock_id": 102, "name": "Room 102", "alias": "", "mac": "C540E09C8C02", "building_number": 1,
     "building_name": "Lodge", "floor_number": 1, "floor_name": "First", "battery": 80,
     "has_gateway": False, "lock_type": 1},
    {"lock_id": 201, "name": "Room 201", "alias": "", "mac": "C540E09C8C03", "building_number": 1,
     "building_name": "Lodge", "floor_number": 2, "floor_name": "Second", "battery": 70,
     "has_gateway": False, "lock_type": 1},
    {"lock_id": 900, "name": "Pool Gate", "alias": "", "mac": "C540E09C8C90", "building_number": 2,
     "building_name": "Pool House", "floor_number": 1, "floor_name": "Ground", "battery": 60,
     "has_gateway": True, "lock_type": 1},
    {"lock_id": 901, "name": "Spa Door", "alias": "Spa", "mac": "C540E09C8C91",
     "building_number": 2, "building_name": "Pool House", "floor_number": 1,
     "floor_name": "Ground", "battery": 50, "has_gateway": False, "lock_type": 1},
]


def noon_zone() -> str:
    """A fixed-offset time zone where it's around midday now, so day passes (which end at a time
    today) can always be made whenever the tests run."""
    offset = 12 - datetime.now(timezone.utc).hour
    offset = max(-11, min(12, offset))
    return "Etc/GMT" if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


def local_date(days: int) -> str:
    return (datetime.now(ZoneInfo(noon_zone())) + timedelta(days=days)).date().isoformat()


# ---------------------------------------------------------------- a fresh portal per test

class Portal:
    """A fresh database, an encoder address, people and roles, and test clients for both apps."""

    def __init__(self, tmp_path, encoder_url="ws://127.0.0.1:1"):
        previous, main.db = main.db, Database(str(tmp_path))
        previous.conn.close()
        for table in (main.pin_sessions, main.recent_wristband_requests, main.recent_actions,
                      main.recent_unlocks, main.failures, main.lookups):
            table.clear()
        main.wristbands_tested[0] = main.doors_synced[0] = 0.0
        main.encoders_tested.clear()
        main.ha_zone.update(name=None, at=float("-inf"), tried=float("-inf"))
        main.discovery = main.Discovery(lambda: main.encoder_networks(),
                                        lambda: main.encoder_auto_wanted())
        main.tthotel.forget()
        self.db = main.db
        self.db.update_settings({"wristbands_enabled": "1", "ha_logbook": "0",
                                 "wb_time_zone": noon_zone(), "wb_daypass_end": "23:00",
                                 "wb_daypass_doors": json.dumps(DAY_DOORS),
                                 "wb_overnight_doors": json.dumps(OVERNIGHT_DOORS)})
        self.db.sync_wb_doors(DOORS)
        self.front_desk = self.role("Front Desk", FRONT_DESK)
        self.manager = self.role("Manager", MANAGER)
        self.no_cards = self.role("Housekeeping", None)
        self.device, self.token = self.computer("Front Desk PC", "role", encoder_url)
        self.pins = {}
        self.ana = self.person("Ana", "1111", self.front_desk)
        self.bob = self.person("Bob", "2222", self.manager)
        self.cy = self.person("Cy", "3333", self.no_cards)
        self.dee = self.person("Dee", "4444", self.front_desk)
        self.staff = self.admin = None

    def role(self, name, perms):
        role_id = self.db.create_role(name)
        self.db.set_role_wristbands(role_id, perms)
        return role_id

    def computer(self, name, mode, encoder_url):
        device_id = self.db.create_device(name, self.front_desk if mode != "role" else None, None)
        self.db.update_device(device_id, kind="computer", signin_mode=mode, encoder_url=encoder_url)
        code = self.db.device(device_id)["enroll_code"]
        device, token = self.db.claim_enroll_code(code)
        return device_id, token

    def person(self, name, pin, role_id, own=None):
        staff_id = self.db.create_staff(name, pin)
        self.db.update_staff(staff_id, role_id=role_id,
                             wristbands=None if own is None else json.dumps(own))
        self.pins[staff_id] = pin
        return staff_id

    async def __aenter__(self):
        self.staff = TestClient(TestServer(main.staff_app()))
        self.admin = TestClient(TestServer(main.admin_app()))
        await self.staff.start_server()
        await self.admin.start_server()
        await main.tthotel.start()
        return self

    async def __aexit__(self, *exc):
        await main.encoders.close_all()
        await main.tthotel.close()
        await self.staff.close()
        await self.admin.close()

    async def sign_in(self, staff_id, token=None) -> dict:
        token = token or self.token
        headers = {"Authorization": f"Bearer {token}"}
        resp = await self.staff.post("/api/unlock", json={"staff_id": staff_id,
                                                          "pin": self.pins[staff_id]},
                                     headers=headers)
        assert resp.status == 200, await resp.text()
        return {**headers, "X-Session": (await resp.json())["session"]}

    async def call(self, method, path, who, body=None):
        resp = await self.staff.request(method, path, headers=who, json=body)
        return resp.status, await resp.json()

    async def admin_call(self, method, path, body=None):
        resp = await self.admin.request(method, path, headers=ADMIN_HEADERS, json=body)
        text = await resp.text()
        try:
            return resp.status, json.loads(text)
        except ValueError:
            return resp.status, text

    def jobs(self):
        return self.db.wb_jobs(1000)

    def activity(self):
        return [(a["actor"], a["action"], bool(a["ok"])) for a in self.db.activity(500)]


def run(coro):
    return asyncio.run(coro)


def day_pass(name="Smith party", count=2, **extra):
    return {"type": "day_pass", "name": name, "count": count, **extra}


def overnight(rooms=(101,), nights=1, **extra):
    return {"type": "overnight", "name": "Jones", "count": 2, "room_ids": list(rooms),
            "checkout": local_date(nights), **extra}


def staff_card(access, days=7, **extra):
    return {"type": "staff", "name": "Eve", "count": 1, "access": access,
            "expires_on": local_date(days), **extra}


# ---------------------------------------------------------------- who may make cards

def test_permissions_resolve_per_device_and_person(tmp_path):
    p = Portal(tmp_path)
    device = p.db.device(p.device)
    ana, cy = p.db.staff(p.ana), p.db.staff(p.cy)
    assert main.wristband_permissions(device, ana)["types"] == ["day_pass", "overnight"]
    assert main.wristband_permissions(device, cy)["types"] == []
    assert main.wristband_permissions(device, None)["types"] == []
    # A person's own permissions win over their role's on a role-based device.
    p.db.update_staff(p.cy, wristbands=json.dumps(FRONT_DESK))
    assert main.wristband_permissions(device, p.db.staff(p.cy))["types"] == FRONT_DESK["types"]
    # Damaged permission data means nothing, never everything.
    p.db.update_staff(p.cy, wristbands="{not json")
    assert main.wristband_permissions(device, p.db.staff(p.cy))["types"] == []
    p.db.conn.execute("UPDATE roles SET wristbands = '[1, 2' WHERE id = ?", (p.manager,))
    assert main.wristband_permissions(device, p.db.staff(p.bob))["types"] == []
    # A shared kiosk device uses the device's own role, whoever signs in.
    p.db.update_device(p.device, signin_mode="staff", role_id=p.front_desk)
    assert main.wristband_permissions(p.db.device(p.device), cy)["types"] == FRONT_DESK["types"]
    # No encoder, a kiosk device without people, or Wristbands turned off: nothing.
    for change in ({"encoder_url": None}, {"signin_mode": "phone"}):
        assert main.wristband_permissions({**p.db.device(p.device), **change}, ana)["types"] == []
    p.db.update_settings({"wristbands_enabled": "0"})
    assert main.wristband_permissions(p.db.device(p.device), ana)["types"] == []


STAFF_ROUTES = [("GET", "/api/wristbands", None), ("POST", "/api/wristbands/preview", day_pass()),
                ("POST", "/api/wristbands/jobs", day_pass()),
                ("GET", "/api/wristbands/jobs/1", None),
                ("POST", "/api/wristbands/jobs/1/retry", None),
                ("POST", "/api/wristbands/jobs/1/skip", None),
                ("POST", "/api/wristbands/jobs/1/cancel", None),
                ("GET", "/api/wristbands/log", None),
                ("POST", "/api/wristbands/lookup", None), ("GET", "/api/wristbands/lookup", None)]


def test_every_staff_route_needs_a_person_allowed_to_make_cards(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "area_layout", fake_area_layout)
    monkeypatch.setattr(main.ha, "states", fake_states)

    async def scenario():
        p = Portal(tmp_path)
        kiosk, kiosk_token = p.computer("Lobby", "phone", "ws://127.0.0.1:1")
        kiosk_only = {"Authorization": f"Bearer {kiosk_token}"}
        async with p:
            cy = await p.sign_in(p.cy)
            for method, path, body in STAFF_ROUTES:
                # Not signed in at all, signed in without wristband access, a kiosk device.
                assert (await p.staff.request(method, path, json=body)).status == 401, path
                status, reply = await p.call(method, path, cy, body)
                if path.endswith("/cancel"):
                    # Anyone may stop their own jobs; Cy has none.
                    assert status == 404, (path, reply)
                else:
                    assert status == 403 and "wristbands" in reply["error"], (path, reply)
                status, _ = await p.call(method, path, kiosk_only, body)
                assert status == 403, path
            assert not p.jobs()
            assert ("Cy on Front Desk PC", "Blocked: wristbands", False) in p.activity()
            # The app is only told to offer Wristbands to someone who may use it.
            status, data = await p.call("GET", "/api/entities", cy)
            assert status == 200 and data["wristbands"] is False
            ana = await p.sign_in(p.ana)
            status, data = await p.call("GET", "/api/entities", ana)
            assert data["wristbands"] is True
    run(scenario())


async def fake_area_layout(max_age=300.0):
    return main.AreaLayout([], {}, {})


async def fake_states(max_age=2.0):
    return {}


def test_plans_are_checked_against_the_persons_permissions(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        # Dee may only use the pool house (building 2), and only door-level staff cards.
        p.db.update_staff(p.dee, wristbands=json.dumps({
            **MANAGER, "doors": {"buildings": [2]}, "staff_levels": ["door"],
            "see_all_cards": False}))
        async with p:
            ana, dee, bob = await p.sign_in(p.ana), await p.sign_in(p.dee), await p.sign_in(p.bob)
            # Card types the person may not make.
            status, reply = await p.call("POST", "/api/wristbands/preview", ana,
                                         staff_card([{"level": "door", "door": 900}]))
            assert status == 403, reply
            # Doors out of their scope.
            status, reply = await p.call("POST", "/api/wristbands/preview", dee, overnight([101]))
            assert status == 403, reply
            # Staff card levels they don't have, and ones they do.
            status, reply = await p.call("POST", "/api/wristbands/preview", dee,
                                         staff_card([{"level": "building", "building": 2}]))
            assert status == 403, reply
            status, reply = await p.call("POST", "/api/wristbands/preview", dee,
                                         staff_card([{"level": "door", "door": 900}]))
            assert status == 200, reply
            # Changing a day pass's doors needs edit_doors.
            status, reply = await p.call("POST", "/api/wristbands/preview", ana,
                                         day_pass(door_ids=[900]))
            assert status == 403, reply
            status, reply = await p.call("POST", "/api/wristbands/preview", bob,
                                         day_pass(door_ids=[900]))
            assert status == 200, reply
            # The longest stay and the furthest staff card end date they may give.
            preview = "/api/wristbands/preview"
            status, reply = await p.call("POST", preview, ana, overnight(nights=5))
            assert status in (400, 403) and reply["field"], reply
            status, reply = await p.call("POST", preview, ana, overnight(nights=2))
            assert status == 200, reply
            status, reply = await p.call("POST", "/api/wristbands/preview", bob,
                                         staff_card([{"level": "all"}], days=60))
            assert status in (400, 403) and reply["field"], reply
            status, reply = await p.call("POST", "/api/wristbands/preview", bob,
                                         staff_card([{"level": "all"}], days=20))
            assert status == 200, reply
            plan = reply["plan"]
            assert plan["type"] == "staff" and plan["count"] == 1 and plan["expires_label"]
            assert all(set(r) == {"level", "label"} for r in plan["records"])
            # Refusals are noted; typing mistakes aren't. No job came of any of it.
            blocked = [a for a in p.activity() if a[1] == "Blocked: wristbands"]
            assert len(blocked) == 4
            assert not p.jobs()
            # The same rules apply to making cards, not just previews.
            status, reply = await p.call("POST", "/api/wristbands/jobs", dee, overnight([101]))
            assert status == 403 and not p.jobs()
    run(scenario())


def test_options_show_only_what_the_person_may_use(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        p.db.update_staff(p.dee, wristbands=json.dumps({**FRONT_DESK,
                                                        "doors": {"floors": [[1, 1]]}}))
        p.db.set_wb_door(102, hidden=1)
        async with p:
            status, options = await p.call("GET", "/api/wristbands", await p.sign_in(p.dee))
            assert status == 200, options
            types = options["types"]
            assert types["day_pass"]["allowed"] and not types["staff"]["allowed"]
            assert [d["id"] for d in options["doors"]] == [101]   # 102 hidden, 201 another floor
            assert options["time_zone"] == noon_zone() and options["see_all_cards"] is False
            assert set(options["encoder"]) >= {"connected", "busy", "error"}
            # Nothing secret about the encoder or TT Hotel reaches the app.
            text = json.dumps(options)
            assert "C540E09C8C" not in text and "hotelInfo" not in text
    run(scenario())


def test_rate_limit_per_device(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            ana = await p.sign_in(p.ana)
            statuses = [(await p.call("POST", "/api/wristbands/preview", ana, day_pass()))[0]
                        for _ in range(main.WRISTBAND_REQUESTS_PER_MINUTE + 1)]
            assert statuses[:-1] == [200] * main.WRISTBAND_REQUESTS_PER_MINUTE
            assert statuses[-1] == 429
    run(scenario())


# ---------------------------------------------------------------- settings and secrets

def test_settings_validation(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            ok = {"wb_daypass_end": "8:30", "wb_checkout_time": "11:00:00",
                  "wb_time_zone": "America/Denver", "wb_daypass_doors": ["900", 901, 901],
                  "wb_sectors": "0000000000011111", "wb_card_wait": "5",
                  "tthotel_region": "china", "tthotel_sync_hours": 100}
            status, _ = await p.admin_call("PUT", "/api/settings", ok)
            assert status == 200
            s = p.db.settings()
            assert s["wb_daypass_end"] == "08:30" and s["wb_checkout_time"] == "11:00"
            assert json.loads(s["wb_daypass_doors"]) == [900, 901]
            assert s["wb_card_wait"] == "10" and s["tthotel_sync_hours"] == "48"   # kept in range
            assert ("Admin", "Updated wristband settings", True) in p.activity()
            for bad in ({"wb_daypass_end": "25:00"}, {"wb_checkout_time": "11am"},
                        {"wb_daypass_end": "20:60"}, {"wb_time_zone": "Mars/Base"},
                        {"wb_time_zone": "../../etc/passwd"}, {"wb_sectors": "0000000000000000"},
                        {"wb_sectors": "01"}, {"wb_overnight_doors": ["pool"]},
                        {"wb_overnight_doors": "{"}, {"tthotel_region": "moon"},
                        {"tthotel_password_md5": "my real password"},
                        {"tthotel_client_secret": "has spaces in it"}):
                status, reply = await p.admin_call("PUT", "/api/settings", bad)
                assert status == 400 and reply["error"], bad
            assert p.db.settings()["wb_daypass_end"] == "08:30"   # nothing bad was saved
    run(scenario())


def test_encoder_address_on_devices(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            status, reply = await p.admin_call("POST", "/api/devices", {
                "name": "Desk 2", "kind": "computer", "signin_mode": "role",
                "encoder_url": "192.168.1.50", "encoder_port": "com3"})
            assert status == 200, reply
            device = p.db.device(reply["id"])
            assert device["encoder_url"] == "ws://192.168.1.50:8102"
            assert device["encoder_port"] == "COM3"
            # One spelling per encoder, so two computers sharing it take turns.
            for good, stored in (("ws://desk-pc.local:9000/", "ws://desk-pc.local:9000"),
                                 ("WS://Desk-PC.Local:09000/", "ws://desk-pc.local:9000"),
                                 ("", None), (None, None)):
                status, _ = await p.admin_call("PATCH", f"/api/devices/{device['id']}",
                                               {"encoder_url": good, "encoder_port": "auto"})
                assert status == 200
                assert p.db.device(device["id"])["encoder_url"] == stored
                assert p.db.device(device["id"])["encoder_port"] is None
            for bad in ({"encoder_url": "http://192.168.1.50:8102"},
                        {"encoder_url": "ws://192.168.1.50:99999"},
                        {"encoder_url": "ws://user@host:8102"}, {"encoder_url": "ws://a b"},
                        {"encoder_port": "COM999"}, {"encoder_port": "/dev/../etc/shadow"},
                        {"encoder_port": "LPT1"}):
                status, reply = await p.admin_call("PATCH", f"/api/devices/{device['id']}", bad)
                assert status == 400 and reply["error"], bad
            status, reply = await p.admin_call("POST", "/api/devices", {
                "name": "Desk 3", "encoder_url": "ftp://x"})
            assert status == 400
            assert not any(d["name"] == "Desk 3" for d in p.db.devices())
    run(scenario())


SECRET, PASSWORD = "s3cret-Client-Secret", "0123456789abcdef0123456789abcdef"


def test_secrets_never_reach_the_browser_or_backups(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            status, _ = await p.admin_call("PUT", "/api/settings", {
                "tthotel_client_id": "client-1", "tthotel_username": "hotel@example.com",
                "tthotel_client_secret": SECRET, "tthotel_password_md5": PASSWORD.upper()})
            assert status == 200
            p.db.update_settings({"tthotel_token": json.dumps({"token": "tok-123",
                                                               "refresh": "ref-456"}),
                                  "tthotel_usage": json.dumps({"month": "2026-10", "count": 7})})
            assert p.db.settings()["tthotel_password_md5"] == PASSWORD
            status, state = await p.admin_call("GET", "/api/state")
            text = json.dumps(state)
            for secret in (SECRET, PASSWORD, "tok-123", "ref-456"):
                assert secret not in text
            settings = state["settings"]
            assert settings["tthotel_client_secret_set"] == "1"
            assert settings["tthotel_password_md5_set"] == "1"
            for key in ("tthotel_client_secret", "tthotel_password_md5", "tthotel_token",
                        "tthotel_usage"):
                assert key not in settings
            assert settings["tthotel_client_id"] == "client-1"
            assert "wristbands" in state and "encoders" in state["wristbands"]
            # A blank secret keeps the saved one; "clear" removes both.
            await p.admin_call("PUT", "/api/settings", {"tthotel_client_secret": "",
                                                       "tthotel_password_md5": ""})
            assert p.db.settings()["tthotel_client_secret"] == SECRET
            status, backup = await p.admin_call("GET", "/api/backup")
            text = json.dumps(backup)
            for secret in (SECRET, PASSWORD, "tok-123", "ref-456"):
                assert secret not in text
            assert backup["schema"] == 3 and backup["settings"]["tthotel_client_id"] == "client-1"
            # A backup can't plant or clear secrets either.
            planted = {**backup, "settings": {**backup["settings"], "tthotel_client_secret": "evil",
                                              "tthotel_password_md5": "f" * 32,
                                              "tthotel_token": '{"token": "evil"}',
                                              "tthotel_clear_secret": "1"}}
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": planted})
            assert status == 200, reply
            s = p.db.settings()
            assert s["tthotel_client_secret"] == SECRET and s["tthotel_password_md5"] == PASSWORD
            assert "evil" not in s["tthotel_token"]
            await p.admin_call("PUT", "/api/settings", {"tthotel_clear_secret": True})
            s = p.db.settings()
            assert s["tthotel_client_secret"] == "" and s["tthotel_password_md5"] == ""
    run(scenario())


# ---------------------------------------------------------------- roles, people and backups

def test_role_and_person_permissions_from_the_dashboard(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            status, _ = await p.admin_call("PUT", f"/api/roles/{p.no_cards}",
                                           {"name": "Housekeeping", "entities": {},
                                            "wristbands": {**FRONT_DESK, "types": ["day_pass"]}})
            assert status == 200
            assert p.db.role(p.no_cards)["wristbands"]["types"] == ["day_pass"]
            # Left out: kept.
            await p.admin_call("PUT", f"/api/roles/{p.no_cards}", {"name": "Housekeeping",
                                                                  "entities": {}})
            assert p.db.role(p.no_cards)["wristbands"]["types"] == ["day_pass"]
            status, reply = await p.admin_call("PUT", f"/api/roles/{p.no_cards}",
                                               {"name": "Housekeeping", "entities": {},
                                                "wristbands": {"types": ["master_key"]}})
            assert status == 400 and reply["error"]
            # A person: their own, or null for the same as their role.
            status, _ = await p.admin_call("PATCH", f"/api/staff/{p.cy}",
                                           {"wristbands": {**FRONT_DESK, "types": ["overnight"]}})
            assert status == 200
            member = next(m for m in p.db.staff_list() if m["id"] == p.cy)
            assert member["wristbands"]["types"] == ["overnight"]
            await p.admin_call("PATCH", f"/api/staff/{p.cy}", {"wristbands": None})
            assert p.db.staff(p.cy)["wristbands"] is None
            # A copy of a role makes the same cards.
            status, reply = await p.admin_call("POST", f"/api/roles/{p.front_desk}/duplicate",
                                               {"name": "Front Desk 2"})
            assert p.db.role(reply["id"])["wristbands"]["types"] == ["day_pass", "overnight"]
    run(scenario())


def test_backup_round_trip_and_old_backups(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        p.db.update_staff(p.dee, wristbands=json.dumps({**FRONT_DESK, "doors": {"doors": [900]}}))
        p.db.update_device(p.device, encoder_port="COM4")
        p.db.set_wb_door(900, name="Pool", hidden=1)
        p.db.update_settings({"wb_checkout_time": "10:30"})
        async with p:
            status, backup = await p.admin_call("GET", "/api/backup")
            assert status == 200 and backup["schema"] == 3
            assert {"lock_id": 900, "name": "Pool", "hidden": 1} in backup["wristband_doors"]
            # Change everything, then restore.
            p.db.set_role_wristbands(p.front_desk, None)
            p.db.update_staff(p.dee, wristbands=None)
            p.db.update_device(p.device, encoder_url=None, encoder_port=None)
            p.db.set_wb_door(900, name="", hidden=0)
            p.db.update_settings({"wb_checkout_time": "12:00"})
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, reply
            assert p.db.role(p.front_desk)["wristbands"] == FRONT_DESK
            assert json.loads(p.db.staff(p.dee)["wristbands"])["doors"]["doors"] == [900]
            device = p.db.device(p.device)
            assert device["encoder_url"] == "ws://127.0.0.1:1" and device["encoder_port"] == "COM4"
            door = p.db.wb_door(900)
            assert door["name"] == "Pool" and door["hidden"] == 1
            assert p.db.settings()["wb_checkout_time"] == "10:30"
            # The door list and card records aren't part of a backup, and survive a restore.
            assert len(p.db.wb_doors()) == len(DOORS)

            # Card jobs stay with their makers only while the same ID has the same name: a
            # backup from elsewhere where Ana's ID is someone else's leaves her jobs to no one.
            def add_job(staff_id):
                return p.db.add_wb_job(
                    card_type="day_pass", name="Smith", count=1, records=[], summary="Pool Gate",
                    expires_at=time.time() + 3600, allow_lockout=False, staff_id=staff_id,
                    device_id=p.device, actor="Someone", role=None, ip=None,
                    encoder_url="ws://127.0.0.1:1")
            ana_job, dee_job, gone_job = add_job(p.ana), add_job(p.dee), add_job(9999)
            other = json.loads(json.dumps(backup))
            for row in other["staff"]:
                if row["id"] == p.ana:
                    row["name"] = "Zed"
            for row in other["devices"]:
                row["encoder_url"] = row["encoder_url"] and "WS://127.0.0.1:0001/"
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": other})
            assert status == 200, reply
            assert [p.db.wb_job(j)["staff_id"] for j in (ana_job, dee_job, gone_job)] == [
                None, p.dee, None]
            assert p.db.device(p.device)["encoder_url"] == "ws://127.0.0.1:1"

            # A backup made before wristbands (schema 2) still restores: no wristbands for roles,
            # people follow their role, no encoders, door names and settings left as they are.
            old = json.loads(json.dumps(backup))
            old["schema"] = 2
            for row in old["roles"] + old["staff"]:
                row.pop("wristbands")
            for row in old["devices"]:
                row.pop("encoder_url")
                row.pop("encoder_port")
            old.pop("wristband_doors")
            old["settings"] = {k: v for k, v in old["settings"].items()
                               if not main.wristband_setting(k)}
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": old})
            assert status == 200, reply
            assert p.db.role(p.front_desk)["wristbands"] is None
            assert p.db.staff(p.dee)["wristbands"] is None
            assert p.db.device(p.device)["encoder_url"] is None
            assert p.db.wb_door(900)["name"] == "Pool"
            assert p.db.settings()["wb_checkout_time"] == "10:30"

            # Damaged permissions in a file mean no wristbands, never more.
            bad = json.loads(json.dumps(backup))
            for row in bad["roles"]:
                row["wristbands"] = '{"types": ["day_pass"], "doors": "everything"'
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": bad})
            assert status == 200, reply
            assert p.db.role(p.front_desk)["wristbands"]["types"] == []
            # A newer backup is refused.
            status, reply = await p.admin_call("POST", "/api/restore/preview",
                                               {"backup": {**backup, "schema": 4}})
            assert status == 400 and "newer" in reply["error"]
    run(scenario())


def test_exports_and_imports_carry_wristbands(tmp_path):
    p = Portal(tmp_path)
    p.db.update_staff(p.dee, wristbands=json.dumps({**FRONT_DESK, "max_nights": 1}))
    roles = transfer.export_roles(p.db, "test")
    staff = transfer.export_staff(p.db, "test")
    assert next(r for r in roles["roles"] if r["name"] == "Front Desk")["wristbands"] == FRONT_DESK
    assert next(r for r in roles["roles"] if r["name"] == "Housekeeping")["wristbands"] is None
    assert next(m for m in staff["staff"] if m["name"] == "Dee")["wristbands"]["max_nights"] == 1
    assert next(m for m in staff["staff"] if m["name"] == "Ana")["wristbands"] is None
    # Into a fresh system.
    q = Portal(tmp_path / "other")
    q.db.set_role_wristbands(q.front_desk, None)
    q.db.update_staff(q.dee, wristbands=None)
    transfer.import_roles(q.db, roles, "replace")
    transfer.import_staff(q.db, staff, "replace")
    assert q.db.role(q.front_desk)["wristbands"] == FRONT_DESK
    assert json.loads(q.db.staff(q.dee)["wristbands"])["max_nights"] == 1
    # Older files without wristbands leave them as they are.
    for row in roles["roles"]:
        row.pop("wristbands")
    transfer.import_roles(q.db, roles, "replace")
    assert q.db.role(q.front_desk)["wristbands"] == FRONT_DESK


# ---------------------------------------------------------------- doors and TT Hotel

class FakeTTHotel:
    """Just enough of TT Hotel's cloud API: sign-in, the lock list, and the encoder's key."""

    def __init__(self, locks):
        self.locks = locks
        self.calls: list[str] = []
        self.app = web.Application()
        for path in ("/oauth2/token", "/v3/lock/listByHotel", "/v3/hotel/getInfo"):
            self.app.router.add_route("*", path, self.handle)
        self.runner = None

    async def handle(self, request):
        self.calls.append(request.path)
        if request.path == "/oauth2/token":
            return web.json_response({"access_token": "fake-token", "refresh_token": "fake-refresh",
                                      "uid": 1, "expires_in": 7776000})
        if request.path == "/v3/hotel/getInfo":
            return web.json_response({"hotelInfo": "FAKEHOTELINFO", "errcode": 0})
        return web.json_response({"list": self.locks, "pageNo": 1, "pageSize": 1000, "pages": 1,
                                  "total": len(self.locks)})

    async def __aenter__(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", TT_PORT).start()
        self.api, tthotel.API_OVERRIDE = tthotel.API_OVERRIDE, f"http://127.0.0.1:{TT_PORT}"
        return self

    async def __aexit__(self, *exc):
        tthotel.API_OVERRIDE = self.api
        await self.runner.cleanup()


def tt_lock(door: dict) -> dict:
    mac = door["mac"]
    return {"lockId": door["lock_id"], "lockName": door["name"], "lockAlias": door["alias"],
            "lockMac": ":".join(mac[i:i + 2] for i in range(0, 12, 2)),
            "electricQuantity": door["battery"], "hasGateway": int(door["has_gateway"]),
            "buildingNumber": door["building_number"], "buildingName": door["building_name"],
            "floorNumber": door["floor_number"], "floorName": door["floor_name"], "type": 1}


def tthotel_account(p: Portal):
    p.db.update_settings({"tthotel_client_id": "client-1", "tthotel_client_secret": SECRET,
                          "tthotel_username": "hotel", "tthotel_password_md5": PASSWORD})


def test_door_sync_and_door_choices(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        tthotel_account(p)
        p.db.set_wb_door(101, name="Garden Room")
        listed = [tt_lock(d) for d in DOORS if d["lock_id"] != 201]
        listed.append(tt_lock({**DOORS[0], "lock_id": 301, "name": "Room 301",
                               "mac": "C540E09C8C31"}))
        async with FakeTTHotel(listed), p:
            status, reply = await p.admin_call("POST", "/api/wristbands/doors/sync")
            assert status == 200, reply
            assert reply == {"added": 1, "updated": 0, "removed": 1, "total": len(listed)}
            # Names chosen here stay; doors TT Hotel dropped are kept, but inactive.
            assert p.db.wb_door(101)["name"] == "Garden Room"
            assert p.db.wb_door(201)["active"] == 0
            status, reply = await p.admin_call("POST", "/api/wristbands/doors/sync")
            assert status == 429
            status, doors = await p.admin_call("GET", "/api/wristbands/doors")
            door = next(d for d in doors["doors"] if d["id"] == 900)
            assert door["mac_last4"] == "8C90" and "mac" not in door
            status, _ = await p.admin_call("PATCH", "/api/wristbands/doors/900",
                                           {"name": "  Pool  ", "hidden": True})
            assert status == 200
            assert p.db.wb_door(900)["name"] == "Pool" and p.db.wb_door(900)["hidden"] == 1
            assert (await p.admin_call("PATCH", "/api/wristbands/doors/5", {"name": "x"}))[0] == 404
            status, reply = await p.admin_call("POST", "/api/wristbands/test")
            assert status == 200, reply
            assert reply == {"doors": len(listed), "buildings": 2}
            assert (await p.admin_call("POST", "/api/wristbands/test"))[0] == 429
            actions = [a[1] for a in p.activity()]
            assert "Synced doors from TT Hotel" in actions
            assert "Tested the TT Hotel connection" in actions
    run(scenario())


# ---------------------------------------------------------------- card jobs on the encoder

def test_last_card_made_on_an_encoder(tmp_path):
    p = Portal(tmp_path)
    url = "ws://127.0.0.1:1"

    def job(at, number, encoder_url=url):
        job_id = p.db.add_wb_job(
            card_type="day_pass", name="Smith", count=1, records=[], summary="Pool Gate",
            expires_at=at + 3600, allow_lockout=False, staff_id=p.ana, device_id=p.device,
            actor="Ana", role=None, ip=None, encoder_url=encoder_url)
        p.db.update_wb_card(job_id, 1, status="written", card_number=number, written_at=at)
    now = time.time()
    job(now - 7200, "OLD")
    assert p.db.wb_last_card(url, now - 3600) is None
    job(now - 60, "A1")
    job(now - 30, "B1", "ws://10.0.0.9:8102")
    assert p.db.wb_last_card(url, now - 3600) == "A1"
    assert p.db.wb_last_card("ws://10.0.0.9:8102", now - 3600) == "B1"


def test_startup_closes_unfinished_jobs(tmp_path):
    p = Portal(tmp_path)
    job_id = p.db.add_wb_job(card_type="day_pass", name="Smith", count=2, records=[],
                             summary="Pool", expires_at=time.time() + 3600, allow_lockout=False,
                             staff_id=p.ana, device_id=p.device, actor="Ana on Front Desk PC",
                             role="Front Desk", ip="127.0.0.1", encoder_url="ws://127.0.0.1:1")
    p.db.update_wb_card(job_id, 1, status="written", card_number="1234")
    p.db.update_wb_job(job_id, status="running")
    main.recover_wristband_jobs()
    job = p.db.wb_job(job_id)
    assert job["status"] == "cancelled" and job["finished_at"]
    assert [c["status"] for c in job["cards"]] == ["written", "cancelled"]
    assert any("after a restart" in a[1] for a in p.activity())


def test_time_zone_is_home_assistants_unless_set(tmp_path, monkeypatch):
    calls, down = [], [False]

    async def fake_config():
        calls.append(("GET", "/config"))
        if down[0]:
            raise main.HomeAssistantError("can't connect")
        return {"time_zone": "America/Denver"}
    monkeypatch.setattr(main.ha, "config", fake_config)

    async def scenario():
        p = Portal(tmp_path)
        p.db.update_settings({"wb_time_zone": ""})
        assert (await main.wristband_zone()).key == "America/Denver"
        assert (await main.wristband_zone()).key == "America/Denver"
        assert calls == [("GET", "/config")]                  # read once, then remembered
        p.db.update_settings({"wb_time_zone": "Europe/London"})
        assert (await main.wristband_zone()).key == "Europe/London"
        # Home Assistant out of reach and its zone never read: no guessing, no cards.
        p.db.update_settings({"wb_time_zone": ""})
        main.ha_zone.update(name=None, at=float("-inf"), tried=float("-inf"))
        main.discovery = main.Discovery(lambda: main.encoder_networks(),
                                        lambda: main.encoder_auto_wanted())
        down[0] = True
        assert await main.wristband_zone() is None
        assert await main.wristband_zone() is None
        assert len(calls) == 2                                # failures aren't retried at once
        async with p:
            ana = await p.sign_in(p.ana)
            status, reply = await p.call("POST", "/api/wristbands/jobs", ana, day_pass())
            assert status == 503 and "time zone" in reply["error"]
            status, options = await p.call("GET", "/api/wristbands", ana)
            assert status == 200 and options["enabled"] is False and options["reason"]
            assert not p.jobs()
    run(scenario())


async def job_until(p: Portal, who: dict, job_id: int, check, timeout: float = 10.0) -> dict:
    """Poll a job the way the app does until check(job) holds."""
    deadline = time.monotonic() + timeout
    while True:
        status, reply = await p.call("GET", f"/api/wristbands/jobs/{job_id}", who)
        assert status == 200, reply
        if check(reply["job"]):
            return reply["job"]
        assert time.monotonic() < deadline, reply["job"]
        await asyncio.sleep(0.05)


def waiting_for(seq):
    return lambda job: job["phase"] == "waiting_for_card" and job["current"] == seq


class Simulator:
    async def __aenter__(self):
        self.sim = encoder_sim.EncoderSim()
        self.url = await self.sim.start()
        return self.sim

    async def __aexit__(self, *exc):
        await self.sim.stop()


def test_card_job_from_start_to_finish(tmp_path):
    async def scenario():
        async with Simulator() as sim, FakeTTHotel([tt_lock(d) for d in DOORS]):
            p = Portal(tmp_path, encoder_url=f"ws://127.0.0.1:{sim._runner.addresses[0][1]}")
            tthotel_account(p)
            async with p:
                ana = await p.sign_in(p.ana)
                status, reply = await p.call("POST", "/api/wristbands/jobs", ana, day_pass())
                assert status == 200, reply
                job = reply["job"]
                assert job["type"] == "day_pass" and job["count"] == 2
                assert job["made_by"] == "Ana on Front Desk PC" and job["expires_label"]
                job_id = job["id"]
                # One job at a time on an encoder.
                status, reply = await p.call("POST", "/api/wristbands/jobs", ana, day_pass())
                assert status == 409 and "busy" in reply["error"]
                await job_until(p, ana, job_id, waiting_for(1))
                first = sim.place_card()                      # a blank card: set up, then written
                job = await job_until(p, ana, job_id, lambda j: j["phase"] == "remove_card")
                assert job["message"] == "Take wristband 1 off the encoder"
                sim.remove_card()
                await job_until(p, ana, job_id, waiting_for(2))
                second = sim.place_card()
                job = await job_until(p, ana, job_id, lambda j: j["status"] == "done")
                assert [(c["status"], c["card_number"]) for c in job["cards"]] == [
                    ("written", first), ("written", second)]
                # The day-pass doors went on each card, ending at the day-pass time today.
                record = p.db.wb_job(job_id)
                assert [r["door"] for r in record["records"]] == DAY_DOORS
                assert {r["mac"] for r in sim.cards[first]["records"]} == {
                    "C540E09C8C90", "C540E09C8C91"}
                assert all(r["endDate"] == int(record["expires_at"])
                           for r in sim.cards[second]["records"])
                assert record["staff_id"] == p.ana and record["finished_at"]
                assert not sim.connected                       # let go after the job
                assert ("Ana on Front Desk PC", "Made 2 day passes for Smith party",
                        True) in p.activity()

                # Jobs are seen by their maker, and by people who may see every card.
                dee, bob = await p.sign_in(p.dee), await p.sign_in(p.bob)
                assert (await p.call("GET", f"/api/wristbands/jobs/{job_id}", dee))[0] == 404
                assert (await p.call("POST", f"/api/wristbands/jobs/{job_id}/cancel",
                                     dee))[0] == 404
                assert (await p.call("GET", f"/api/wristbands/jobs/{job_id}", bob))[0] == 200
                assert (await p.call("GET", "/api/wristbands/log", dee))[1]["jobs"] == []
                status, log = await p.call("GET", "/api/wristbands/log", bob)
                assert [j["id"] for j in log["jobs"]] == [job_id]
                status, log = await p.call("GET", f"/api/wristbands/log?q={second[:6]}", ana)
                assert [j["id"] for j in log["jobs"]] == [job_id]
                status, log = await p.call("GET", "/api/wristbands/log?q=nobody", ana)
                assert log["jobs"] == []
                assert (await p.call("GET", "/api/wristbands/log?before=nan", ana))[0] == 400
                status, jobs = await p.admin_call("GET", "/api/wristbands/jobs")
                assert [j["id"] for j in jobs["jobs"]] == [job_id]

                # Look up a wristband: what's on it, and our record of it.
                assert (await p.call("GET", "/api/wristbands/lookup", ana))[1]["state"] == "error"
                status, reply = await p.call("POST", "/api/wristbands/lookup", ana)
                assert status == 200 and reply["state"] == "waiting"
                sim.place_card(first)
                deadline = time.monotonic() + 10
                while True:
                    status, found = await p.call("GET", "/api/wristbands/lookup", ana)
                    if found["state"] != "waiting" or time.monotonic() > deadline:
                        break
                    await asyncio.sleep(0.05)
                assert found["state"] == "done", found
                assert found["card_number"] == first and found["job"]["id"] == job_id
                assert sorted(r["label"] for r in found["card"]) == ["Pool Gate", "Spa"]
                assert "C540E09C8C" not in json.dumps(found)
                # Someone else's look-up isn't shown to Dee, and Dee can't see Ana's job.
                assert (await p.call("GET", "/api/wristbands/lookup", dee))[1]["state"] == "error"
                sim.remove_card()
    run(scenario())


def test_card_that_fails_needs_a_decision(tmp_path):
    async def scenario():
        async with Simulator() as sim, FakeTTHotel([tt_lock(d) for d in DOORS]):
            p = Portal(tmp_path, encoder_url=f"ws://127.0.0.1:{sim._runner.addresses[0][1]}")
            tthotel_account(p)
            async with p:
                ana = await p.sign_in(p.ana)
                status, reply = await p.call("POST", "/api/wristbands/jobs", ana, overnight([101]))
                assert status == 200, reply
                job_id = reply["job"]["id"]
                assert (await p.call("POST", f"/api/wristbands/jobs/{job_id}/retry", ana))[0] == 409
                await job_until(p, ana, job_id, waiting_for(1))
                sim.fail_next("CE_WriteCard", 104)             # the card is full
                card = sim.place_card(initialized=True)
                job = await job_until(p, ana, job_id, lambda j: j["phase"] == "needs_attention")
                assert job["status"] == "needs_attention" and job["current"] == 1
                assert job["cards"][0]["status"] == "failed" and "104" not in job["message"]
                # Try again: the same card is written this time.
                status, reply = await p.call("POST", f"/api/wristbands/jobs/{job_id}/retry", ana)
                assert status == 200, reply
                await job_until(p, ana, job_id, lambda j: j["phase"] == "remove_card")
                sim.remove_card()
                await job_until(p, ana, job_id, waiting_for(2))
                sim.fail_next("CE_WriteCard", 104)
                sim.place_card(initialized=True)
                await job_until(p, ana, job_id, lambda j: j["phase"] == "needs_attention")
                status, reply = await p.call("POST", f"/api/wristbands/jobs/{job_id}/skip", ana)
                assert status == 200, reply
                job = await job_until(p, ana, job_id, lambda j: j["status"] == "done")
                assert [c["status"] for c in job["cards"]] == ["written", "skipped"]
                assert job["cards"][0]["card_number"] == card
                assert ("Ana on Front Desk PC", "Made 2 overnight wristbands for Room 101",
                        True) in p.activity()
                assert ("Ana on Front Desk PC", "Made only 1 of 2 overnight wristbands for Jones",
                        False) in p.activity()
                sim.remove_card()

                # Stop: the job ends at once, cards not made are cancelled.
                status, reply = await p.call("POST", "/api/wristbands/jobs", ana, day_pass(count=3))
                job_id = reply["job"]["id"]
                await job_until(p, ana, job_id, waiting_for(1))
                status, reply = await p.call("POST", f"/api/wristbands/jobs/{job_id}/cancel", ana)
                assert status == 200
                job = await job_until(p, ana, job_id, lambda j: j["status"] == "cancelled")
                assert [c["status"] for c in job["cards"]] == ["cancelled"] * 3
                assert ("Ana on Front Desk PC", "Couldn't make 3 day passes for Smith party",
                        False) in p.activity()
                # The encoder is free again.
                status, reply = await p.call("POST", "/api/wristbands/jobs", ana, day_pass(count=1))
                assert status == 200, reply
                job_id = reply["job"]["id"]
                await job_until(p, ana, job_id, waiting_for(1))
                # Ana loses wristband access mid-job: she can't carry on, but can still stop it.
                p.db.set_role_wristbands(p.front_desk, None)
                assert (await p.call("POST", f"/api/wristbands/jobs/{job_id}/skip", ana))[0] == 403
                dee = await p.sign_in(p.dee)
                assert (await p.call("POST", f"/api/wristbands/jobs/{job_id}/cancel",
                                     dee))[0] == 404
                status, reply = await p.call("POST", f"/api/wristbands/jobs/{job_id}/cancel", ana)
                assert status == 200, reply
                for _ in range(200):
                    if p.db.wb_job(job_id)["status"] == "cancelled":
                        break
                    await asyncio.sleep(0.05)
                assert p.db.wb_job(job_id)["status"] == "cancelled"
    run(scenario())


def test_testing_an_encoder_from_the_dashboard(tmp_path):
    async def scenario():
        async with Simulator() as sim:
            p = Portal(tmp_path, encoder_url=f"ws://127.0.0.1:{sim._runner.addresses[0][1]}")
            other, _ = p.computer("Back Office", "role", "ws://127.0.0.1:1")
            plain, _ = p.computer("Kitchen", "role", None)
            async with p:
                status, reply = await p.admin_call("POST", f"/api/devices/{p.device}/encoder-test")
                assert status == 200 and reply["version"], reply
                assert not sim.connected                       # let go again
                test = f"/api/devices/{p.device}/encoder-test"
                assert (await p.admin_call("POST", test))[0] == 429
                status, reply = await p.admin_call("POST", f"/api/devices/{other}/encoder-test")
                assert status == 502 and reply["error"]
                assert (await p.admin_call("POST", f"/api/devices/{plain}/encoder-test"))[0] == 400
                assert (await p.admin_call("POST", "/api/devices/999/encoder-test"))[0] == 404
                actions = [a for a in p.activity() if a[1].startswith("Tested the encoder")]
                assert ("Admin", "Tested the encoder on Front Desk PC", True) in actions
                assert ("Admin", "Tested the encoder on Back Office", False) in actions
    run(scenario())


# ---------------------------------------------------------------- Automatic card encoders

def network_answer(address="127.0.0.1", prefix=30):
    """Home Assistant's {"type": "network"} answer, as a fake ws_command."""
    async def ws_command(payload, timeout=10.0):
        assert payload == {"type": "network"}
        return {"adapters": [{"name": "lo", "enabled": True, "auto": True, "default": True,
                              "ipv4": [{"address": address, "network_prefix": prefix}],
                              "ipv6": []}], "configured_adapters": []}
    return ws_command


def helper(ip, port=8102):
    return {"ip": ip, "url": f"ws://{ip}:{port}", "version": "", "found_at": time.time()}


async def search_done():
    for _ in range(300):
        if not main.discovery.scanning:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the search didn't finish")


def test_automatic_encoder_is_found_and_used(tmp_path, monkeypatch):
    # 127.0.0.0/8 isn't private, so it may only be searched in this test.
    monkeypatch.setattr(discovery, "PRIVATE_NETWORKS",
                        (*discovery.PRIVATE_NETWORKS, discovery.ipaddress.ip_network("127.0.0.0/8")))
    monkeypatch.setattr(main.ha, "ws_command", network_answer())

    async def scenario():
        async with Simulator() as sim:
            port = sim._runner.addresses[0][1]
            p = Portal(tmp_path, encoder_url=None)
            p.db.update_device(p.device, encoder_auto=1)
            main.discovery.port = port
            async with p:
                ana = p.db.staff(p.ana)
                # Nothing found yet: no Wristbands here, and a search starts by itself.
                assert main.wristband_permissions(p.db.device(p.device), ana)["types"] == []
                assert main.discovery.scanning and main.discovery.scans == 1
                await search_done()
                url = f"ws://127.0.0.1:{port}"
                device = p.db.device(p.device)
                assert main.encoder_address(device) == url
                assert main.wristband_permissions(device, ana)["types"] == FRONT_DESK["types"]
                assert main.tthotel_wanted()
                # The staff app and the dashboard use the encoder that was found.
                who = await p.sign_in(p.ana)
                status, reply = await p.call("GET", "/api/wristbands", who)
                assert status == 200 and "encoder" in reply, reply
                status, reply = await p.admin_call("POST", f"/api/devices/{p.device}/encoder-test")
                assert status == 200 and reply["version"], reply
                status, state = await p.admin_call("GET", "/api/state")
                card = next(d for d in state["devices"] if d["id"] == p.device)
                assert card["encoder_mode"] == "auto" and card["encoder_address"] == url
                assert card["encoder_text"] == "Automatic: 127.0.0.1"
                assert card["wristbands_note"] is None
                assert url in state["wristbands"]["encoders"]
                assert state["wristbands"]["discovery"]["helpers"][0]["ip"] == "127.0.0.1"
                # Typed the same way by hand on another computer: the same runner, so they
                # take turns with the encoder.
                other, _ = p.computer("Back Office", "role", f"WS://127.0.0.1:{port}/".lower())
                p.db.update_device(other, encoder_url=main.transfer.encoder_url(url))
                assert main.encoder_address(p.db.device(other)) == url
                assert main.encoders.find(url) is main.encoders.get(
                    main.encoder_address(p.db.device(other)))
                # A typed address always wins over Automatic.
                p.db.update_device(p.device, encoder_url="ws://127.0.0.1:1")
                assert main.encoder_address(p.db.device(p.device)) == "ws://127.0.0.1:1"
                assert main.encoder_mode(p.db.device(p.device)) == "address"
            assert main.discovery.scans == 1
    run(scenario())


def test_which_helper_an_automatic_computer_uses(tmp_path):
    async def scenario():
        p = Portal(tmp_path, encoder_url=None)
        p.db.update_device(p.device, encoder_auto=1)
        found = main.discovery
        found.scanned_at, found.networks = time.time(), ["192.168.1.0/24"]
        found._auto_at = time.monotonic()      # as if it had just searched
        device = lambda: p.db.device(p.device)   # noqa: E731
        # None found.
        assert main.encoder_address(device()) is None
        assert main.encoder_text(device()) == "Automatic: no encoder found on 192.168.1.0/24"
        assert "hasn't been found" in main.wristbands_note(device())
        # One found: used.
        found.helpers = [helper("192.168.1.50")]
        assert main.encoder_address(device()) == "ws://192.168.1.50:8102"
        assert main.encoder_text(device()) == "Automatic: 192.168.1.50"
        # Several: someone chooses, and the choice is remembered by its address.
        found.helpers = [helper("192.168.1.50"), helper("192.168.1.60")]
        assert main.encoder_address(device()) is None
        assert main.encoder_text(device()) == "Automatic: 2 found, choose one"
        async with p:
            status, reply = await p.admin_call("PATCH", f"/api/devices/{p.device}",
                                               {"encoder_mode": "auto",
                                                "encoder_auto_ip": "192.168.1.60"})
            assert status == 200, reply
        assert device()["encoder_auto_ip"] == "192.168.1.60"
        assert main.encoder_address(device()) == "ws://192.168.1.60:8102"
        # It moved: back to the only one found, or a choice again if there are several.
        found.helpers = [helper("192.168.1.61")]
        assert main.encoder_address(device()) == "ws://192.168.1.61:8102"
        found.helpers = [helper("192.168.1.50"), helper("192.168.1.61")]
        assert main.encoder_address(device()) is None
        # While searching, and after a search that couldn't run.
        found.helpers = []
        found.error = "Couldn't read Home Assistant's network setting."
        assert main.encoder_text(device()) == f"Automatic: {found.error}"
        assert found.scans == 0                # all within 10 minutes of the last search
    run(scenario())


def test_no_searching_unless_something_uses_automatic(tmp_path, monkeypatch):
    asked = []

    async def ws_command(payload, timeout=10.0):
        asked.append(payload)
        # One address (a /32), so nothing on a real network is swept.
        return {"adapters": [{"name": "eth0", "enabled": True,
                              "ipv4": [{"address": "192.168.77.10", "network_prefix": 32}]}]}
    monkeypatch.setattr(main.ha, "ws_command", ws_command)

    async def scenario():
        p = Portal(tmp_path)                      # a typed address only
        main.discovery._probe_timeout = 0.01
        async with p:
            device = p.db.device(p.device)
            assert main.wristband_permissions(device, p.db.staff(p.ana))["types"]
            assert main.discovery.scan_soon() is False
            await p.admin_call("GET", "/api/state")
            # Automatic, but Wristbands is off: still no search.
            p.db.update_settings({"wristbands_enabled": "0"})
            p.db.update_device(p.device, encoder_url=None, encoder_auto=1)
            assert main.encoder_address(p.db.device(p.device)) is None
            assert main.discovery.scan_soon() is False
            assert main.discovery.scans == 0 and not asked
            # Find encoders works anyway, at most every 30 seconds.
            status, reply = await p.admin_call("POST", "/api/wristbands/encoders/find")
            assert status == 200, reply
            assert reply["discovery"]["networks"] == ["192.168.77.10/32"]
            assert reply["discovery"]["helpers"] == [] and asked == [{"type": "network"}]
            assert any(d["encoder_text"] == "Automatic: no encoder found on 192.168.77.10/32"
                       for d in reply["devices"])
            status, reply = await p.admin_call("POST", "/api/wristbands/encoders/find")
            assert status == 429 and "seconds" in reply["error"]
            assert ("Admin", "Looked for card encoders", True) in p.activity()
            # Wristbands on: an encoder that isn't found, or stops answering, is looked for
            # again, at most every 10 minutes.
            p.db.update_settings({"wristbands_enabled": "1"})
            main.encoder_address(p.db.device(p.device))
            assert main.discovery.scans == 1          # the button's search was just now
            main.discovery._auto_at -= discovery.AUTO_SCAN_GAP
            main.encoder_address(p.db.device(p.device))
            assert main.discovery.scans == 2
            await search_done()
            main.discovery.helpers = [helper("192.168.77.20", 1)]
            runner = main.encoders.get("ws://192.168.77.20:1")
            runner.reachable = True
            main.discovery._auto_at -= discovery.AUTO_SCAN_GAP
            main.encoder_address(p.db.device(p.device))
            assert main.discovery.scans == 2          # found and answering: no search
            runner.reachable = False
            main.encoder_address(p.db.device(p.device))
            assert main.discovery.scans == 3
            main.encoder_address(p.db.device(p.device))
            assert main.discovery.scans == 3
            await search_done()
            # Networks typed in settings are searched instead of Home Assistant's.
            p.db.update_settings({"wb_scan_subnets": "10.9.8.0/30"})
            assert [str(n) for n in await main.encoder_networks()] == ["10.9.8.0/30"]
            assert len(asked) == 3              # the button and two searches of its own
    run(scenario())


def test_card_encoder_setting_on_devices(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            status, reply = await p.admin_call("POST", "/api/devices", {
                "name": "Desk 2", "kind": "computer", "signin_mode": "role",
                "encoder_mode": "auto"})
            assert status == 200, reply
            desk = reply["id"]
            device = p.db.device(desk)
            assert device["encoder_auto"] == 1 and device["encoder_url"] is None
            path = f"/api/devices/{desk}"
            for body, url, auto, chosen in (
                    ({"encoder_mode": "auto", "encoder_auto_ip": " 192.168.1.60 "}, None, 1,
                     "192.168.1.60"),
                    ({"encoder_mode": "auto", "encoder_auto_ip": ""}, None, 1, None),
                    ({"encoder_mode": "address", "encoder_url": "192.168.1.50"},
                     "ws://192.168.1.50:8102", 0, None),
                    ({"encoder_mode": "off", "encoder_url": "192.168.1.50"}, None, 0, None),
                    ({"encoder_url": "192.168.1.51"}, "ws://192.168.1.51:8102", 0, None)):
                status, reply = await p.admin_call("PATCH", path, body)
                assert status == 200, (body, reply)
                device = p.db.device(desk)
                assert (device["encoder_url"], device["encoder_auto"],
                        device["encoder_auto_ip"]) == (url, auto, chosen), body
            for bad in ({"encoder_mode": "address"}, {"encoder_mode": "address", "encoder_url": ""},
                        {"encoder_mode": "sometimes"},
                        {"encoder_mode": "auto", "encoder_auto_ip": "printer"},
                        {"encoder_mode": "auto", "encoder_auto_ip": "fe80::1"}):
                status, reply = await p.admin_call("PATCH", path, bad)
                assert status == 400 and reply["error"], bad
            # New computers have none.
            status, reply = await p.admin_call("POST", "/api/devices", {"name": "Desk 3",
                                                                        "kind": "computer"})
            device = p.db.device(reply["id"])
            assert device["encoder_auto"] == 0 and device["encoder_url"] is None
            # The networks to search, in Wristbands settings.
            status, _ = await p.admin_call("PUT", "/api/settings",
                                           {"wb_scan_subnets": "192.168.1.0/24 10.0.0.7/30"})
            assert status == 200
            assert p.db.settings()["wb_scan_subnets"] == "192.168.1.0/24, 10.0.0.4/30"
            for bad in ("192.168.0.0/16", "8.8.8.0/24", "nonsense"):
                status, reply = await p.admin_call("PUT", "/api/settings", {"wb_scan_subnets": bad})
                assert status == 400 and reply["error"], bad
            status, _ = await p.admin_call("PUT", "/api/settings", {"wb_scan_subnets": ""})
            assert status == 200 and p.db.settings()["wb_scan_subnets"] == ""
    run(scenario())


def test_automatic_encoders_in_backups(tmp_path):
    async def scenario():
        p = Portal(tmp_path, encoder_url=None)
        p.db.update_device(p.device, encoder_auto=1, encoder_auto_ip="192.168.1.60")
        async with p:
            status, backup = await p.admin_call("GET", "/api/backup")
            row = next(d for d in backup["devices"] if d["id"] == p.device)
            assert row["encoder_auto"] == 1 and row["encoder_auto_ip"] == "192.168.1.60"
            p.db.update_device(p.device, encoder_auto=0, encoder_auto_ip=None)
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, reply
            device = p.db.device(p.device)
            assert device["encoder_auto"] == 1 and device["encoder_auto_ip"] == "192.168.1.60"
            # Damaged values are dropped; backups from before Automatic mean Off.
            for row in backup["devices"]:
                row["encoder_auto_ip"] = "<script>"
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200 and p.db.device(p.device)["encoder_auto_ip"] is None
            for row in backup["devices"]:
                row.pop("encoder_auto")
                row.pop("encoder_auto_ip")
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, reply
            assert p.db.device(p.device)["encoder_auto"] == 0
    run(scenario())


def test_why_wristbands_wont_show_on_a_computer(tmp_path):
    p = Portal(tmp_path)
    note = lambda: main.wristbands_note(p.db.device(p.device))   # noqa: E731
    assert note() is None                                  # role-based, with an encoder
    p.db.update_device(p.device, signin_mode="phone")
    assert "Kiosk mode" in note() and "Shared" in note()
    p.db.update_device(p.device, signin_mode="staff", role_id=p.no_cards)
    assert "its role can't make any wristbands" in note()
    p.db.update_device(p.device, role_id=p.front_desk)
    assert note() is None
    p.db.update_device(p.device, encoder_url=None)
    assert "has no card encoder" in note()
    p.db.update_device(p.device, encoder_auto=1)
    assert "hasn't been found yet" in note()
    p.db.update_settings({"wristbands_enabled": "0"})
    assert "Wristbands is turned off" in note()
    p.db.update_device(p.device, encoder_auto=0)
    assert note() is None                                  # no wristbands meant here at all
    p.db.update_settings({"wristbands_enabled": "1"})
    p.db.update_device(p.device, kind="phone")
    assert note() is None                                  # only computers
