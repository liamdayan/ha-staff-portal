"""How the HR portal's two halves work together: notifications that stay in the portal's inbox,
HR reading Connecteam through Staff Access's one connection in a lane of its own, the
dashboard's Connecteam test, and every HR setting in migration files but never in Staff Access's
own backups or /api/state (a 0.10.x file's HR Connecteam keys are ignored).

Nothing here talks to a real Connecteam, Google, TT Hotel or Home Assistant: Connecteam is the
fake in hr_helpers.
"""

import asyncio
import json
import time

from hr_helpers import HR, FakeConnecteam, ct_user

import connecteam  # noqa: E402
import db as database  # noqa: E402
import main  # noqa: E402
import migrate  # noqa: E402
from hr import gdrive, notify  # noqa: E402

ANA = ct_user(301, "Ana", "Lee", code="4821", groups=[501], mobile="541-555-0301")
BO = ct_user(302, "Bo", "Diaz", code="7302", groups=[501], mobile="541-555-0302")
OLD_HR_CONNECTEAM = {"hr_connecteam_client_id": "hr-app", "hr_connecteam_client_secret": "hr-secret",
                     "hr_connecteam_chat": "1", "hr_connecteam_documents": "1"}


def run(coro):
    return asyncio.run(coro)


def test_notifications_stay_in_the_portal(tmp_path):
    async def scenario():
        async with FakeConnecteam([ANA]) as ct:
            async with HR(tmp_path) as hr:
                await hr.add_people(ANA)
                hr.svc.sync_state["tried"] = time.time()   # no people sync during these ticks
                await hr.svc.tick()
                assert notify.notify("301", "welcome", urgent=True)
                assert notify.notify("301", "welcome", once="w") and not notify.notify("301", "welcome", once="w")
                await hr.svc.tick()
                assert notify.unread(hr.hdb, "301") == 2
                # Nothing goes to Connecteam (no chat), and the old outbox is gone.
                assert ct.calls == [] and main.connecteam.hr_usage()["count"] == 0
                assert not hr.hdb.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'outbox'").fetchone()
    run(scenario())


def test_a_sync_with_no_room_in_the_lane_is_tried_again_soon(tmp_path):
    """Turning HR on just after a busy minute mustn't leave its people list empty for an hour:
    the first sync has no room this minute, so it's tried again in a minute and a half."""
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
                # The dashboard says why, on the HR tab and in the Connecteam section.
                assert main.connecteam.hr_status()["paused"]
                status, state = await hr.admin_call("GET", "/api/state")
                assert "fewer requests" in state["hr"]["connecteam"]["paused"]
                assert "fewer requests" in state["connecteam"]["hr"]["paused"]
                # The time clock carries on.
                ct.too_many.clear()
                await c.check()
                assert c.last_error is None and c.clocked_in == {"301"}
    run(scenario())


def test_connecteam_test_shows_each_step(tmp_path):
    async def scenario():
        async with FakeConnecteam([ANA, BO], clocked_in=[301]) as ct:
            async with HR(tmp_path) as hr:
                status, data = await hr.admin_call("POST", "/api/connecteam/test")
                assert status == 200 and data["ok"]
                assert [(c["check"], c["ok"]) for c in data["checks"]] == [
                    ("Getting an access token", True), ("Reading users", True),
                    ("Reading the time clock", True), ("Reading smart groups", True)]
                assert "users.read" in data["checks"][0]["detail"]
                assert data["checks"][1]["detail"] == "Read 2 active users."
                assert "1 people are clocked in" in data["checks"][2]["detail"]
                # One connection: Staff Access's keys, for everything.
                assert set(ct.tokens) == {"client"}
                # At most every few seconds, and logged.
                assert (await hr.admin_call("POST", "/api/connecteam/test"))[0] == 429
                assert any(a["action"] == "Tested the Connecteam connection" for a in hr.db.activity(20))
                # A refusal: that step fails in Connecteam's own words, and the others still run.
                main.connecteam_tested[0] = 0.0
                ct.refuse.add("/users/v1/users")
                status, data = await hr.admin_call("POST", "/api/connecteam/test")
                assert status == 200 and not data["ok"]
                assert [c["ok"] for c in data["checks"]] == [True, False, True, True]
                assert "“Missing scope”" in data["checks"][1]["detail"]
                assert "Expert plan" in data["checks"][1]["detail"]
                # The HR portal's old routes are gone.
                assert (await hr.admin_call("POST", "/api/hr/connecteam/test"))[0] in (404, 405)
                assert (await hr.admin_call("PUT", "/api/hr/connecteam", {}))[0] in (404, 405)
    run(scenario())


def test_hr_off_reads_nothing(tmp_path):
    async def scenario():
        async with FakeConnecteam([ANA]) as ct:
            async with HR(tmp_path, enabled=False) as hr:
                await hr.svc.tick()
                notify.notify("301", "welcome", urgent=True)    # no HR database: nothing happens
                await main.connecteam.check()
                assert main.connecteam.hr_usage()["count"] == 0 and ct.count("/users/v1/custom-fields") == 0
                status, state = await hr.admin_call("GET", "/api/state")
                assert state["hr"]["status"] == "off" and state["hr"]["connecteam"]["used_today"] == 0
    run(scenario())


# ---------------------------------------------------------------- settings, backups, migration

HR_SETTINGS = {
    "hr_enabled": "1", "hr_host": "hr.example.com",
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
            assert "g" * 43 not in text and "A" * 40 not in text
            status, backup = await hr.admin_call("GET", "/api/backup")
            assert status == 200 and not any(k.startswith("hr_") for k in backup["settings"])
            text = json.dumps(backup)
            assert "g" * 43 not in text
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
            assert not any(k.startswith("hr_connecteam_c") for k in main.db.settings())
    run(scenario())


def test_damaged_hr_settings_in_a_migration_are_left_out():
    raw = {**HR_SETTINGS, "hr_enabled": "yes", "hr_host": "<script>"}
    found = {**migrate._hr_settings(raw), **migrate._google_settings(raw)}
    assert "hr_enabled" not in found and "hr_host" not in found and found["hr_google_secret"] == "g" * 43


def test_old_hr_connecteam_keys_are_ignored_and_removed(tmp_path):
    """0.10.0 to 0.10.3 had HR's own Connecteam app, chat and certificate copies: a migration file
    from then that still has them is ignored, and an install that has them drops them at start."""
    found = migrate._hr_settings({**HR_SETTINGS, **OLD_HR_CONNECTEAM})
    assert found == {"hr_enabled": "1", "hr_host": "hr.example.com"}
    old = database.Database(str(tmp_path))
    old.conn.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                         list(OLD_HR_CONNECTEAM.items()))
    old.conn.commit()
    old.conn.close()
    again = database.Database(str(tmp_path))
    assert not set(OLD_HR_CONNECTEAM) & set(again.settings())
    assert not set(OLD_HR_CONNECTEAM) & (set(database.DEFAULT_SETTINGS) | set(database.PRIVATE_SETTINGS))
    again.conn.close()


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


def test_hr_lane_constants():
    assert (connecteam.HR_MINUTE_CAP, connecteam.HR_DAILY_CAP, connecteam.HR_STOP_SHARE) == (6, 500, 0.5)
    assert connecteam.HRBusy("x").budget
