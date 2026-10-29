"""Doors and transfers, from an audit: a remote lock-out stays unsure of an unseen enrolment until
it has looked for it, a backlog of lock records in one delivery is read, only the add-on network's
own https fallback skips certificate checks, a phone revoked while a restore runs stays revoked,
and a staff file listing someone twice is refused rather than failing.

Nothing here talks to a real TT Hotel or Home Assistant (see test_tthotel, test_door_activity and
test_wristbands_server for the fakes used).
"""

import asyncio
import time

import aiohttp
import pytest

from test_door_activity import POOL, deliver, form, fresh_state, record, stored
from test_tthotel import LOST
from test_tthotel import run as tt_run
from test_wristbands_server import Portal, run

import ha  # noqa: E402
import main  # noqa: E402
import transfer  # noqa: E402
import tthotel  # noqa: E402

ADD = "/v3/identityCard/addForReversedCardNumber"


# ---------------------------------------------------------------- remote lock-out

def test_lock_out_stays_unsure_until_it_has_looked(monkeypatch):
    async def scenario(env):
        env.fake.queued[ADD] = ["drop"]
        assert (await env.tt.lock_out_card(9, LOST))["unsure"] is True
        assert len(env.fake.cards) == 1   # enrolled, unseen
        # The look gets no answer: still unsure, so the next try looks again.
        env.fake.queued["/v3/identityCard/list"] = ["drop"]
        result = await env.tt.lock_out_card(9, LOST, find_first=True)
        assert result["status"] == "retry" and result["unsure"] is True
        # Our own minute cap holds it back before it looks: still unsure.
        env.tt._recent.extend([time.time()] * tthotel.MINUTE_CAP)
        result = await env.tt.lock_out_card(9, LOST, find_first=True)
        assert result["wait"] is True and result["unsure"] is True
        env.tt._recent.clear()
        # Found at last: deleted, and the number was never enrolled twice.
        result = await env.tt.lock_out_card(9, LOST, find_first=True)
        assert result["status"] == "done" and result["unsure"] is False
        assert env.fake.cards == {} and env.fake.count(ADD) == 1
    tt_run(monkeypatch, scenario)


def test_waiting_a_minute_keeps_an_unsure_door_tried(monkeypatch):
    saved = []

    class Lockouts:
        def set_lockout(self, card_id, lock_id, method, status, detail=None, tries=None):
            saved.append((status, tries))

    async def held_back(lock_id, card_number, *, api="reversed", card_id=None, find_first=False):
        return {"status": "retry", "card_id": None, "code": None, "message": "This minute.",
                "unsure": find_first, "wait": True}
    monkeypatch.setattr(main, "db", Lockouts())
    monkeypatch.setattr(main.tthotel, "lock_out_card", held_back, raising=False)
    # The card may be on the door: the wait still counts, so a restart looks for it first.
    door = {"unsure": True}
    assert asyncio.run(main.lock_out_try(1, LOST, 900, "reversed", 1, False, door, True)) == "wait"
    assert door["unsure"] is True and saved[-1] == ("pending", 1)
    # Nothing sent before: the wait isn't a try.
    assert asyncio.run(main.lock_out_try(1, LOST, 900, "reversed", 1, False, {}, True)) == "wait"
    assert saved[-1] == ("pending", 0)


# ---------------------------------------------------------------- lock records

def test_a_backlog_of_records_in_one_delivery_is_kept(tmp_path, monkeypatch):
    fresh_state(monkeypatch)

    async def scenario():
        p = Portal(tmp_path)
        async with p:
            now = time.time()
            backlog = [record(49, str(10**9 + i), at=now - i, username="x" * 90)
                       for i in range(300)]
            assert len(form(POOL, backlog)) > 64 * 1024   # past the staff app's own limit
            assert await deliver(p, POOL, backlog) == (200, "success")
            assert len(stored(p)) == 300
    run(scenario())


# ---------------------------------------------------------------- Home Assistant

def test_only_the_add_on_networks_own_https_skips_certificate_checks(monkeypatch):
    class Session:
        def __init__(self):
            self.ssl = {}

        def request(self, method, url, ssl, **kwargs):
            self.ssl[url.rsplit("/auth", 1)[0]] = ssl
            raise aiohttp.ClientError("unreachable")

    def checks(**env) -> dict:
        for key in ("SUPERVISOR_TOKEN", "HA_CORE_URL", "HA_URL"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        client = ha.HomeAssistant()
        client.session = Session()
        with pytest.raises(ha.HomeAssistantError):
            asyncio.run(client._core_request("POST", "/auth/login_flow", json={}))
        return client.session.ssl

    # Running without Home Assistant OS, or with Core's address set: always checked.
    assert checks(HA_URL="https://ha.example.com") == {"https://ha.example.com": True}
    assert checks(SUPERVISOR_TOKEN="t", HA_CORE_URL="https://ha.example.com:8123") == {
        "https://ha.example.com:8123": True}
    # The add-on network's own fallback: its certificate is for another name.
    assert checks(SUPERVISOR_TOKEN="t") == {"http://homeassistant:8123": True,
                                            "https://homeassistant:8123": False}


# ---------------------------------------------------------------- backups and imports

def test_a_phone_revoked_while_a_restore_runs_stays_revoked(tmp_path, monkeypatch):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            status, backup = await p.admin_call("GET", "/api/backup")
            assert status == 200 and p.db.device_by_token(p.token)
            saving = main.save_safety_backup

            async def revoked_meanwhile():
                # Another admin revokes the lost phone while the copy is being saved.
                status, _ = await p.admin_call("POST", f"/api/devices/{p.device}/revoke")
                assert status == 200
                return await saving()
            monkeypatch.setattr(main, "save_safety_backup", revoked_meanwhile)
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, reply
            assert p.db.device_by_token(p.token) is None
            device = p.db.device(p.device)
            assert device["status"] == "pending" and device["enroll_code"]
    run(scenario())


def test_a_staff_file_listing_someone_twice_is_refused(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            exported = transfer.export_staff(p.db, "test")
            ana = next(m for m in exported["staff"] if m["name"] == "Ana")
            exported["staff"] += [{**ana, "name": "Zoe"}, {**ana, "name": "zoe"}]
            count = len(p.db.staff_list())
            for mode in ("replace", "copy", "skip"):
                with pytest.raises(ValueError, match="lists zoe twice"):
                    transfer.import_staff(p.db, exported, mode)
                status, reply = await p.admin_call("POST", "/api/import/staff",
                                                   {"file": exported, "mode": mode})
                assert status == 400 and reply["error"] == "The file lists zoe twice."
            assert len(p.db.staff_list()) == count   # all or nothing
    run(scenario())
