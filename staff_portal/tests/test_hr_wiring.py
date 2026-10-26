"""How the HR portal's two halves work together: notifications sent as Connecteam chat through
hr/ctapi.py, one HR request lane for the people sync and HR's own requests, the dashboard's two
HR connections (Connecteam for HR, Google Drive) side by side, and every HR setting in
migration files but never in Staff Access's own backups or /api/state.

Nothing here talks to a real Connecteam, Google, TT Hotel or Home Assistant: Connecteam is the
fake in hr_helpers.
"""

import asyncio
import json
import time

from hr_helpers import HR, FakeConnecteam, ct_user

import connecteam  # noqa: E402
import main  # noqa: E402
import migrate  # noqa: E402
from hr import audit, ctapi, gdrive, notify  # noqa: E402

ANA = ct_user(301, "Ana", "Lee", code="4821", groups=[501], mobile="541-555-0301")
BO = ct_user(302, "Bo", "Diaz", code="7302", groups=[501], mobile="541-555-0302")
CHAT_ON = {"hr_connecteam_client_id": "hr-app", "hr_connecteam_client_secret": "hr-secret",
           "hr_connecteam_chat": "1"}


def run(coro):
    return asyncio.run(coro)


def no_quiet_hours(hr):
    hr.hdb.set_settings({"quiet_start": "00:00", "quiet_end": "00:00"})
    hr.svc.sync_state["tried"] = time.time()   # no people sync during these ticks


def outbox(hr):
    return [dict(r) for r in hr.hdb.conn.execute("SELECT status, tries, error FROM outbox ORDER BY id")]


def test_notifications_go_out_as_connecteam_chat(tmp_path):
    async def scenario():
        async with FakeConnecteam([ANA]) as ct:
            async with HR(tmp_path) as hr:
                await hr.add_people(ANA)
                no_quiet_hours(hr)
                hr.db.update_settings(CHAT_ON)
                await hr.svc.tick()
                assert notify.chat is ctapi.send_chat
                notify.notify("301", "welcome", urgent=True)
                await hr.svc.tick()
                # From "Hot Lake HR", with the HR app's own keys, and no details in the text.
                assert ct.messages == [(301, {"senderId": 77, "text": "You have something new in the HR "
                                                                      "portal. Open it here: https://hr.example.com"})]
                assert ct.tokens == ["hr-app"] and outbox(hr)[0]["status"] == "sent"
                # Token, publisher and message: in HR's lane, and in Staff Access's own count too.
                assert main.connecteam.hr_usage()["count"] == 3 and main.connecteam.usage()["count"] == 3
    run(scenario())


def test_one_lane_for_the_people_sync_and_hrs_own_requests(tmp_path):
    """The people sync and the chat messages share HR's 6 a minute; a message that doesn't fit
    waits in the outbox without using up one of its 8 tries."""
    async def scenario():
        async with FakeConnecteam([ANA]) as ct:
            async with HR(tmp_path) as hr:
                await hr.add_people(ANA)
                no_quiet_hours(hr)
                hr.db.update_settings(CHAT_ON)
                for _ in range(4):
                    main.connecteam.hr_spend()        # four of this minute's six used elsewhere
                await hr.svc.tick()
                notify.notify("301", "welcome", urgent=True)
                await hr.svc.tick()                   # needs a token, the publisher and the message
                assert ct.messages == []
                [row] = outbox(hr)
                assert row["status"] == "queued" and row["tries"] == 0 and "this minute" in row["error"]
                main.connecteam._hr_recent.clear()    # a minute later
                hr.hdb.conn.execute("UPDATE outbox SET due_at = 0")
                hr.hdb.conn.commit()
                await hr.svc.tick()
                assert len(ct.messages) == 1 and outbox(hr)[0]["status"] == "sent"
    run(scenario())


def test_a_sync_with_no_room_in_the_lane_is_tried_again_soon(tmp_path):
    """Turning HR on just after testing Connecteam for HR mustn't leave its people list empty for
    an hour: the first sync has no room this minute, so it's tried again in a minute and a half."""
    async def scenario():
        async with FakeConnecteam([ANA, BO]) as ct:
            async with HR(tmp_path) as hr:
                for _ in range(connecteam.HR_MINUTE_CAP):
                    main.connecteam.hr_spend()       # this minute's share, used by a Test
                now = time.time()
                await hr.svc.tick(now)
                assert hr.hdb.person("301") is None and "this minute" in hr.svc.sync_state["error"]
                retry = hr.svc.sync_state["retry_at"]
                assert now + 60 < retry < now + 120 and not hr.svc.sync_due(now + 30)
                main.connecteam._hr_recent.clear()   # a minute later
                await hr.svc.tick(retry + 1)
                assert hr.hdb.person("301")["name"] == "Ana Lee" and hr.svc.sync_state["retry_at"] is None
                assert not hr.svc.sync_due(retry + 2)   # and then hourly again
                assert ct.count("/users/v1/users") == 1
    run(scenario())


def test_a_429_for_hr_pauses_only_hr(tmp_path):
    async def scenario():
        async with FakeConnecteam([ANA], clocked_in=[301]) as ct:
            async with HR(tmp_path) as hr:
                ct.too_many.add("/users/v1/users")
                assert await hr.svc.sync_people(fresh=True) is None
                assert "fewer requests" in hr.svc.sync_state["error"]
                c = main.connecteam
                assert c.hr_paused_until > time.time() and c.paused_until == 0.0 and c.halted is None
                # HR's chat waits too (it's the same lane), and says why on the dashboard.
                assert main.hr_connecteam.status()["paused"]
                status, state = await hr.admin_call("GET", "/api/state")
                assert "fewer requests" in state["hr"]["connecteam"]["paused"]
                # The time clock carries on.
                ct.too_many.clear()
                await c.check()
                assert c.last_error is None and c.clocked_in == {"301"}
    run(scenario())


def test_chat_refused_falls_back_to_the_inbox_and_says_why(tmp_path):
    async def scenario():
        async with FakeConnecteam([ANA, BO]) as ct:
            async with HR(tmp_path) as hr:
                await hr.add_people(ANA, BO)
                no_quiet_hours(hr)
                hr.db.update_settings(CHAT_ON)
                ct.refuse.add("/publishers/v1/publishers")   # no Communications hub
                await hr.svc.tick()
                notify.notify("301", "welcome", urgent=True)
                await hr.svc.tick()
                assert outbox(hr)[0]["tries"] == 1 and "Communications hub" in outbox(hr)[0]["error"]
                # Chat stops; the inbox carries on, and messages wait instead of using up tries.
                await hr.svc.tick()
                assert notify.chat is None and "chat.write" in hr.svc.chat_problem()
                notify.notify("302", "welcome", urgent=True)
                assert notify.unread(hr.hdb, "302") == 1
                assert [r["tries"] for r in outbox(hr)] == [1]
                status, state = await hr.admin_call("GET", "/api/hr")
                assert state["chat"]["on"] and not state["chat"]["ready"] and "chat.write" in state["chat"]["why"]
                assert state["connecteam"]["stopped"]["chat"] and not state["connecteam"]["stopped"]["documents"]
                # The HR settings page says why too.
                hr.hdb.set_settings({"totp_policy": "nobody"})
                hr.give("301", "hr_admin")
                cookie = await hr.sign_in("301")
                status, s, _ = await hr.call("GET", "/api/settings", cookie=cookie)
                assert s["chat"]["on"] and not s["chat"]["ready"] and "Communications hub" in s["chat"]["why"]
                # Fixed in Connecteam; the owner presses Test: chat comes back, and the message goes.
                ct.refuse.clear()
                status, data = await hr.admin_call("POST", "/api/hr/connecteam/test")
                assert status == 200 and [c["ok"] for c in data["checks"]] == [True, False]
                await hr.svc.tick()
                assert notify.chat is ctapi.send_chat
                hr.hdb.conn.execute("UPDATE outbox SET due_at = 0")
                hr.hdb.conn.commit()
                await hr.svc.tick()
                assert len(ct.messages) == 1
    run(scenario())


def test_connecteam_for_hr_test_button(tmp_path):
    async def scenario():
        async with FakeConnecteam([ANA]) as ct:
            async with HR(tmp_path) as hr:
                hr.db.update_settings({"hr_connecteam_documents": "1"})
                status, data = await hr.admin_call("POST", "/api/hr/connecteam/test")
                assert status == 200
                chat, docs = data["checks"]
                assert chat == {"check": "Chat messages", "ok": True,
                                "detail": 'Chat messages can be sent as "Hot Lake HR".'}
                # The fake has no documents: what Connecteam says, in plain words.
                assert docs["check"] == "Certificate copies" and not docs["ok"]
                # Without HR's own keys, it used Staff Access's.
                assert ct.tokens == ["client"]
                # At most every few seconds; and logged, in the activity log and HR's security events.
                assert (await hr.admin_call("POST", "/api/hr/connecteam/test"))[0] == 429
                assert any(a["action"] == "Tested the Connecteam connection for HR"
                           for a in hr.db.activity(20))
                assert "Tested the Connecteam connection for HR" in [
                    e["action"] for e in audit.lines(hr.hdb, 20, security=True)]
                main.hr_connecteam_tested["at"] = 0.0
                hr.db.update_settings({"connecteam_enabled": "0"})
                status, data = await hr.admin_call("POST", "/api/hr/connecteam/test")
                assert status == 400 and "Turn on Connecteam" in data["error"]
    run(scenario())


def test_hr_off_sends_nothing_even_with_chat_on(tmp_path):
    async def scenario():
        async with FakeConnecteam([ANA]) as ct:
            async with HR(tmp_path, enabled=False) as hr:
                hr.db.update_settings({**CHAT_ON, "hr_connecteam_documents": "1"})
                await hr.svc.tick()
                notify.notify("301", "welcome", urgent=True)    # no HR database: nothing happens
                await main.connecteam.check()
                assert ct.messages == [] and ct.count("/publishers/v1/publishers") == 0
                assert main.connecteam.hr_usage()["count"] == 0
                status, state = await hr.admin_call("GET", "/api/state")
                assert state["hr"]["status"] == "off" and state["hr"]["connecteam"]["chat"]
    run(scenario())


# ---------------------------------------------------------------- settings, backups, migration

HR_SETTINGS = {
    "hr_enabled": "1", "hr_host": "hr.example.com",
    "hr_connecteam_client_id": "hr-app", "hr_connecteam_client_secret": "hr-secret-value",
    "hr_connecteam_chat": "1", "hr_connecteam_documents": "1",
    "hr_google_url": "https://script.google.com/macros/s/" + "A" * 40 + "/exec",
    "hr_google_secret": "g" * 43, "hr_google_drive": "0AHRdriveAAAAUk9PVA",
    "hr_google_restricted_drive": "0ARestrictedAAUk9PVA",
}


def test_every_hr_setting_is_private(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            hr.db.update_settings(HR_SETTINGS)
            for key in [k for k in main.db.settings() if k.startswith("hr_")]:
                assert key in main.PRIVATE_SETTINGS, key
            status, state = await hr.admin_call("GET", "/api/state")
            text = json.dumps(state)
            assert not any(k.startswith("hr_") for k in state["settings"])
            assert "hr-secret-value" not in text and "g" * 43 not in text and "A" * 40 not in text
            status, backup = await hr.admin_call("GET", "/api/backup")
            assert status == 200 and not any(k.startswith("hr_") for k in backup["settings"])
            text = json.dumps(backup)
            assert "hr-secret-value" not in text and "g" * 43 not in text
            # A backup claiming HR settings changes none of them.
            backup["settings"].update(hr_enabled="0", hr_google_secret="x" * 43, hr_connecteam_chat="0")
            status, _ = await hr.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200
            assert {k: main.db.settings()[k] for k in HR_SETTINGS} == HR_SETTINGS
    run(scenario())


def test_migration_carries_every_hr_setting(tmp_path):
    async def scenario():
        async with HR(tmp_path / "old") as old:
            old.db.update_settings(HR_SETTINGS)
            status, made = await old.admin_call("POST", "/api/migrate/create")
            assert status == 200
        async with HR(tmp_path / "new", enabled=False, fresh=True) as new:
            status, reply = await new.admin_call("POST", "/api/migrate/restore",
                                                 {"file": made["file"], "code": made["code"]})
            assert status == 200, reply
            assert {k: main.db.settings()[k] for k in HR_SETTINGS} == HR_SETTINGS
            assert new.svc.status() == "on"
            assert main.hr_connecteam.get_settings() == {"client_id": "hr-app",
                                                         "client_secret": "hr-secret-value",
                                                         "documents": True}
    run(scenario())


def test_damaged_hr_settings_in_a_migration_are_left_out():
    raw = {**HR_SETTINGS, "hr_connecteam_documents": "yes", "hr_connecteam_client_secret": "has space",
           "hr_host": "<script>"}
    found = {**migrate._hr_settings(raw), **migrate._google_settings(raw)}
    assert "hr_connecteam_documents" not in found and "hr_connecteam_client_secret" not in found
    assert "hr_host" not in found and found["hr_google_secret"] == "g" * 43
    assert found["hr_connecteam_chat"] == "1"


def test_the_hr_service_hands_out_the_google_bridge(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            assert hr.svc.google() is None                      # not set up yet
            hr.db.update_settings(HR_SETTINGS)
            bridge = hr.svc.google()
            assert bridge.url == HR_SETTINGS["hr_google_url"]
            assert gdrive.drive_ids(main.db.settings()) == ["0AHRdriveAAAAUk9PVA", "0ARestrictedAAUk9PVA"]
            await hr.svc.stop()
            assert hr.svc.google() is None                      # HR off: nothing for Google
    run(scenario())


def test_hr_lane_constants_are_one_set():
    assert (ctapi.MINUTE_CAP, ctapi.DAY_CAP, ctapi.STOP_SHARE) == (
        connecteam.HR_MINUTE_CAP, connecteam.HR_DAILY_CAP, connecteam.HR_STOP_SHARE) == (6, 500, 0.5)
    assert ctapi.CTError("paused", "x", True).budget and not ctapi.CTError("not_allowed", "x").budget
    assert connecteam.HRBusy("x").budget
