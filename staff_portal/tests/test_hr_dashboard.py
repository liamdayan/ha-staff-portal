"""The HR portal from the Home Assistant dashboard (turning it on, HR admins and their setup QR
code, the emergency controls, security events, the HR Connecteam connection), its Connecteam
requests (its own lane, and HR errors never stopping Staff Access's clock checks), and HR in
backups and migration files.

Nothing here talks to a real Connecteam, Google, TT Hotel or Home Assistant: Connecteam is the
fake in hr_helpers.
"""

import asyncio
import json
import time

import pytest
from hr_helpers import HR, FakeConnecteam, ct_user

import connecteam  # noqa: E402
import main  # noqa: E402
import migrate  # noqa: E402
from hr import audit, notify, signin  # noqa: E402

FIELDS = [{"id": 11, "name": "Employment Start Date", "type": "date"},
          {"id": 12, "name": "Direct manager", "type": "directManager"},
          {"id": 13, "name": "Birthday", "type": "date"}]
ANA = ct_user(301, "Ana", "Lee", code="4821", groups=[501], mobile="541-555-0301",
              email="ana@example.com", fields={11: "10/25/2024", 12: [303], 13: "01/02/1990"})
BO = ct_user(302, "Bo", "Diaz", code="7302", groups=[501], mobile="541-555-0302")
MAX = ct_user(303, "Max", "Ray", code="9900", groups=[501], mobile="541-555-0303")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- the dashboard

def test_turning_hr_on_admins_setup_qr_and_emergency_controls(tmp_path):
    async def scenario():
        async with FakeConnecteam([ANA, BO, MAX], fields=FIELDS) as ct:
            async with HR(tmp_path, enabled=False) as hr:
                status, state = await hr.admin_call("GET", "/api/hr")
                assert status == 200 and state["status"] == "off" and state["admins"] == []
                assert ct.calls == []                       # off: nothing asked of Connecteam
                # The address must be its own, and Connecteam must be on.
                hr.db.update_settings({"public_url": "https://staff.example.com"})
                status, data = await hr.admin_call("PUT", "/api/hr/settings",
                                                   {"hr_enabled": "1", "hr_host": "https://staff.example.com/"})
                assert status == 400 and "own address" in data["error"]
                status, data = await hr.admin_call("PUT", "/api/hr/settings",
                                                   {"hr_enabled": "1", "hr_host": "not a host"})
                assert status == 400
                hr.db.update_settings({"connecteam_enabled": "0"})
                status, data = await hr.admin_call("PUT", "/api/hr/settings",
                                                   {"hr_enabled": "1", "hr_host": "hr.example.com"})
                assert status == 400 and "Connecteam" in data["error"]
                hr.db.update_settings({"connecteam_enabled": "1"})
                status, data = await hr.admin_call("PUT", "/api/hr/settings",
                                                   {"hr_enabled": "1", "hr_host": "HR.Example.com"})
                assert status == 200 and data["status"] == "on"
                assert hr.db.settings()["hr_host"] == "hr.example.com"
                # People come from Connecteam through HR's lane (and the field names once).
                await hr.svc.tick()
                assert ct.count("/users/v1/custom-fields") == 1 and ct.count("/users/v1/users") == 1
                assert main.connecteam.hr_usage()["count"] >= 2
                ana = hr.hdb.person("301")
                assert ana["start_date"] == "2024-10-25" and json.loads(ana["managers"]) == ["303"]
                assert "1990" not in json.dumps(ana)          # birthdays are never stored
                # HR admins: the owner picks them.
                status, data = await hr.admin_call("PUT", "/api/hr/admins", {"person_ids": ["301"]})
                assert status == 200 and data["added"] == ["301"]
                assert (await hr.admin_call("PUT", "/api/hr/admins", {"person_ids": ["999"]}))[0] == 400
                status, state = await hr.admin_call("GET", "/api/hr")
                assert [a["name"] for a in state["admins"]] == ["Ana Lee"] and state["people"] == 3
                # Ana's setup QR code, on the dashboard: she sets up her authenticator with it.
                status, data = await hr.admin_call("POST", "/api/hr/admins/301/setup")
                assert status == 200 and data["url"].startswith("https://hr.example.com/setup/")
                assert data["qr"].startswith("data:image/svg+xml")
                assert (await hr.admin_call("POST", "/api/hr/admins/302/setup"))[0] == 404
                # Emergency controls.
                token = signin.new_session(hr.hdb, "302", "ip", "agent")
                hr.hdb.update_person("302", hard_locked=1)
                assert (await hr.admin_call("POST", "/api/hr/people/302/unlock"))[0] == 200
                assert not signin.locked(hr.hdb.person("302"))
                assert (await hr.admin_call("POST", "/api/hr/people/302/reset"))[0] == 200
                assert signin.session_for(hr.hdb, token) is None
                signin.new_session(hr.hdb, "303", "ip", "agent")
                status, data = await hr.admin_call("POST", "/api/hr/sign-out-everyone")
                assert status == 200 and data["ended"] == 1
                # Security events only: never who looked at what.
                audit.record(hr.hdb, "Viewed a file", actor_id="301", actor="Ana Lee", target_id="302")
                status, state = await hr.admin_call("GET", "/api/hr")
                events = [e["action"] for e in state["events"]]
                assert {"Turned the HR portal on", "Made HR admin", "Made a setup QR code",
                        "Unlocked HR sign-in", "Reset HR sign-in", "Signed everyone out of HR"} <= set(events)
                assert "Viewed a file" not in events
                # HR events stay out of Staff Access's own activity log.
                assert not any("HR" in a["action"] for a in hr.db.activity(500))
                # Turning it off stops everything; turning it on again keeps what it had.
                status, data = await hr.admin_call("PUT", "/api/hr/settings", {"hr_enabled": "0"})
                assert data["status"] == "off" and hr.svc.db is None
                assert (await hr.admin_call("GET", "/api/hr/people"))[0] == 409
                status, data = await hr.admin_call("PUT", "/api/hr/settings", {"hr_enabled": "1"})
                assert data["status"] == "on" and hr.hdb.roles_of("301") == {"hr_admin"}
    run(scenario())


def test_hr_connecteam_connection_keeps_its_secret(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            status, _ = await hr.admin_call("PUT", "/api/hr/connecteam", {
                "hr_connecteam_client_id": "hr-app", "hr_connecteam_client_secret": "hr-secret",
                "hr_connecteam_chat": "1"})
            assert status == 200
            status, state = await hr.admin_call("GET", "/api/state")
            assert state["hr"]["connecteam"] == {"client_id": "hr-app", "secret_set": True, "chat": True,
                                                 "used_today": 0, "daily_cap": connecteam.HR_DAILY_CAP}
            assert "hr-secret" not in json.dumps(state) and "hr_host" not in state["settings"]
            await hr.admin_call("PUT", "/api/hr/connecteam", {"hr_connecteam_client_id": "hr-app",
                                                              "hr_connecteam_client_secret": ""})
            assert hr.db.settings()["hr_connecteam_client_secret"] == "hr-secret"
            assert hr.svc.chat_wanted()
            await hr.admin_call("PUT", "/api/hr/connecteam", {"hr_connecteam_clear_secret": "1"})
            assert not hr.svc.chat_wanted()
            assert (await hr.admin_call("PUT", "/api/hr/connecteam",
                                        {"hr_connecteam_client_secret": "bad secret"}))[0] == 400
    run(scenario())


def test_unlocking_hr_with_the_recovery_key(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(ANA)
            code = hr.svc.keyring.keep_out(True)
            await hr.svc.stop()
            (tmp_path / "data" / "hr" / "outside-backups" / "master.key").unlink()   # a restore
            assert hr.svc.start() == "locked"
            status, state = await hr.admin_call("GET", "/api/hr")
            assert state["status"] == "locked" and state["key"]["state"] == "locked"
            assert (await hr.admin_call("GET", "/api/hr/people"))[0] == 409
            status, data = await hr.admin_call("POST", "/api/hr/unlock-key", {"recovery_key": "AAAA"})
            assert status == 400
            status, data = await hr.admin_call("POST", "/api/hr/unlock-key", {"recovery_key": code})
            assert status == 200 and data["status"] == "on"
            assert hr.hdb.person("301")["name"] == "Ana Lee"
            assert "Unlocked HR with the recovery key" in [l["action"] for l in audit.lines(hr.hdb, 10)]
    run(scenario())


# ---------------------------------------------------------------- Connecteam: HR's own lane

def test_hr_refusals_never_stop_the_clock_checks(tmp_path):
    """Design section 4, "A must-fix": a "not allowed" reply to an HR request stops only HR's
    request, never Staff Access's automatic requests (the clock checks that let clocked-in-only
    staff use controls)."""
    async def scenario():
        async with FakeConnecteam([ANA], fields=FIELDS, clocked_in=[301]) as ct:
            async with HR(tmp_path) as hr:
                ct.refuse.update({"/users/v1/custom-fields", "/chat/v1/conversations"})
                with pytest.raises(connecteam.HRRefused, match="HR portal"):
                    await main.connecteam.profile_fields()
                with pytest.raises(connecteam.HRRefused, match="Missing scope"):
                    await main.connecteam.hr_request("POST", "/chat/v1/conversations", json={},
                                                     credentials=("hr-app", "hr-secret"))
                ct.refuse.add("/users/v1/users")
                main.connecteam._hr_recent.clear()   # this minute's HR requests, aside
                with pytest.raises(connecteam.HRRefused):
                    await main.connecteam.users(max_age=0, hr=True)
                main.connecteam._hr_recent.clear()
                assert await hr.svc.sync_people(fresh=True) is None
                assert "refused" in hr.svc.sync_state["error"]
                assert main.connecteam.halted is None
                await main.connecteam.check()
                assert main.connecteam.last_error is None and main.connecteam.clocked_in == {"301"}
                # The same refusal for Staff Access's own request still stops it, as before.
                ct.refuse.discard("/users/v1/users")
                ct.refuse.add("/time-clock/v1/time-clocks")
                main.connecteam._clocks = None
                await main.connecteam.check()
                assert main.connecteam.halted
    run(scenario())


def test_hr_lane_limits(tmp_path):
    async def scenario():
        async with FakeConnecteam([ANA], fields=FIELDS) as ct:
            async with HR(tmp_path):
                c = main.connecteam
                for _ in range(connecteam.HR_MINUTE_CAP):
                    c.hr_spend()
                with pytest.raises(connecteam.HRBusy, match="this minute"):
                    c.hr_spend()
                assert c.usage()["count"] == connecteam.HR_MINUTE_CAP   # counted in Staff Access's too
                c._hr_recent.clear()
                main.db.update_settings({"hr_connecteam_usage": json.dumps(
                    {"day": c.hr_usage()["day"], "count": connecteam.HR_DAILY_CAP})})
                with pytest.raises(connecteam.HRBusy, match="500"):
                    c.hr_spend()
                main.db.update_settings({"hr_connecteam_usage": "{}", "connecteam_usage": json.dumps(
                    {"day": c.usage()["day"], "count": c.daily_cap() // 2})})
                with pytest.raises(connecteam.HRBusy, match="half"):
                    c.hr_spend()
                # Staff Access itself still has the rest.
                await c.check()
                assert c.last_error is None
                # A pause Connecteam asked for holds HR too.
                main.db.update_settings({"connecteam_usage": "{}"})
                c.paused_until = time.time() + 60
                with pytest.raises(connecteam.HRBusy):
                    c.hr_spend()
                c.paused_until = 0.0
                assert ct.count("/users/v1/custom-fields") == 0
    run(scenario())


def test_hr_off_adds_no_requests(tmp_path):
    async def scenario():
        async with FakeConnecteam([ANA, BO], fields=FIELDS, clocked_in=[301]) as ct:
            async with HR(tmp_path, enabled=False) as hr:
                hr.db.update_settings({"connecteam_auto_staff": "1"})
                main.staff_sync.update(at=None, result=None, error=None, tried=0.0, soon=False)
                await main.run_staff_sync("Admin", fresh=True)
                await main.connecteam.check()
                await hr.svc.tick()
                assert ct.count("/users/v1/custom-fields") == 0
                assert not any(c.endswith("?archived") for c in ct.calls)
                assert main.connecteam.hr_usage()["count"] == 0
                assert hr.svc.db is None and not (tmp_path / "data" / "hr").exists()
    run(scenario())


def test_a_new_hire_can_sign_in_minutes_after_being_added(tmp_path):
    async def scenario():
        async with FakeConnecteam([ANA], fields=FIELDS) as ct:
            async with HR(tmp_path) as hr:
                await hr.svc.sync_people(fresh=True)
                ct.users.append(BO)                       # added in Connecteam just now
                body = {"mobile": "541-555-0302", "pin": "7302"}
                assert (await hr.call("POST", "/api/signin", body))[0] == 401
                assert hr.svc.sync_state["soon"]
                calls = ct.count("/users/v1/users")
                await hr.svc.tick()                       # not yet: 5 minutes since the last
                assert ct.count("/users/v1/users") == calls
                hr.svc.sync_state["tried"] -= 5 * 60      # 5 minutes later...
                users, at = main.connecteam._users
                main.connecteam._users = (users, at - 5 * 60)
                await hr.svc.tick()                       # ...a fresh list, not an hour-old one
                assert ct.count("/users/v1/users") == calls + 1 and not hr.svc.sync_state["soon"]
                hr.svc.throttle.clear()
                assert (await hr.call("POST", "/api/signin", body))[0] == 200
                # Strangers typing numbers can't use up HR's requests: a few dozen a day at most.
                hr.svc.sync_state["soon_count"] = 48
                await hr.call("POST", "/api/signin", {"mobile": "541-555-0999", "pin": "1111"})
                assert not hr.svc.sync_state["soon"]
    run(scenario())


def test_nightly_run_reads_archived_users_and_fields(tmp_path):
    async def scenario():
        archived = [ct_user(302, "Bo", "Diaz", archived=True)]
        async with FakeConnecteam([ANA, BO], archived=archived, fields=FIELDS) as ct:
            async with HR(tmp_path) as hr:
                await hr.svc.sync_people(fresh=True)
                await hr.svc.nightly()
                assert ct.count("/users/v1/users?archived") == 1
                assert hr.hdb.person("302")["archived"] and hr.hdb.person("302")["access_ended"]
                assert json.loads(hr.hdb.setting("ct_fields"))[0]["name"] == "Employment Start Date"
    run(scenario())


def test_messages_go_out_through_the_outbox(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(ANA)
            sent = []

            async def send_chat(user_id, text):
                sent.append((user_id, text))
            hr.db.update_settings({"hr_connecteam_client_id": "hr-app",
                                   "hr_connecteam_client_secret": "hr-secret", "hr_connecteam_chat": "1"})
            hr.svc.chat_sender = lambda: send_chat
            hr.svc.refresh_chat()                       # as saving the connection does
            hr.svc.sync_state["tried"] = time.time()    # no Connecteam here: no sync this time
            hr.hdb.set_settings({"quiet_start": "00:00", "quiet_end": "00:00"})   # no quiet hours
            notify.notify("301", "welcome", urgent=True)
            await hr.svc.tick()
            assert sent == [("301", "You have something new in the HR portal. Open it here: "
                                    "https://hr.example.com")]
            # Chat turned off: the inbox only.
            hr.db.update_settings({"hr_connecteam_chat": "0"})
            hr.svc.chat_sender = lambda: None
            await hr.svc.tick()
            notify.notify("301", "welcome", urgent=True)
            assert hr.hdb.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
            assert notify.unread(hr.hdb, "301") == 2
    run(scenario())


# ---------------------------------------------------------------- backups and migration files

def test_staff_access_backups_never_hold_hr(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(ANA)
            hr.give("301", "hr_admin")
            hr.db.update_settings({"hr_connecteam_client_secret": "hr-secret"})
            status, backup = await hr.admin_call("GET", "/api/backup")
            assert status == 200
            text = json.dumps(backup)
            assert "hr_" not in json.dumps(backup["settings"]) and "Ana Lee" not in text
            assert "hr-secret" not in text and "hr.example.com" not in text
            # Restoring a Staff Access backup leaves HR as it was.
            status, _ = await hr.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200
            assert hr.db.settings()["hr_enabled"] == "1" and hr.svc.running
            assert hr.hdb.roles_of("301") == {"hr_admin"}
    run(scenario())


def test_migration_carries_hr_and_waits_for_uploads(tmp_path):
    async def scenario():
        async with HR(tmp_path / "old") as old:
            await old.add_people(ANA, BO)
            old.give("301", "hr_admin")
            secret = old.enrol("301")
            # Uploads still on their way to Google or Connecteam: no migration file yet.
            waiting = ["2 uploads are still waiting to reach Google Drive. Try again once they're sent."]
            old.svc.migration_blockers.append(lambda svc: waiting[0] if waiting else None)
            status, data = await old.admin_call("POST", "/api/migrate/create")
            assert status == 409 and "waiting" in data["error"]
            waiting.clear()
            status, made = await old.admin_call("POST", "/api/migrate/create")
            assert status == 200
            file, code = made["file"], made["code"]
            assert b"Ana" not in json.dumps(file).encode()
        async with HR(tmp_path / "new", enabled=False, fresh=True) as new:
            status, reply = await new.admin_call("POST", "/api/migrate/restore", {"file": file, "code": code})
            assert status == 200, reply
            assert new.db.settings()["hr_enabled"] == "1" and new.db.settings()["hr_host"] == "hr.example.com"
            assert new.svc.status() == "on"
            assert new.hdb.person("301")["name"] == "Ana Lee" and new.hdb.roles_of("301") == {"hr_admin"}
            assert signin.totp_secret(new.hdb.person("301")) == secret   # the key came too
    run(scenario())


def test_migration_with_the_key_kept_out_needs_the_recovery_key(tmp_path):
    async def scenario():
        async with HR(tmp_path / "old") as old:
            await old.add_people(ANA)
            recovery = old.svc.keyring.keep_out(True)
            status, made = await old.admin_call("POST", "/api/migrate/create")
            assert status == 200
            sealed, _ = migrate.open_file(made["file"], made["code"], main.VERSION)
            assert "key" not in sealed["migrate"]["hr"] and "recovery" in sealed["migrate"]["hr"]
        async with HR(tmp_path / "new", enabled=False, fresh=True) as new:
            status, _ = await new.admin_call("POST", "/api/migrate/restore", {"file": made["file"],
                                                                              "code": made["code"]})
            assert status == 200 and new.svc.status() == "locked"
            status, data = await new.admin_call("POST", "/api/hr/unlock-key", {"recovery_key": recovery})
            assert status == 200 and data["status"] == "on"
            assert new.hdb.person("301")["name"] == "Ana Lee"
    run(scenario())
