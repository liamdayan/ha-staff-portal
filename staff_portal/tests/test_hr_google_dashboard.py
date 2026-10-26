"""Settings > Connections > Google Drive (HR files) on the dashboard: copying the script (and
making a new secret), the Web app URL and the drives, the check when it's opened, and Test
connection. The secret never goes back to the browser except inside the script it copies, and
these settings stay out of backups but travel in migration files.

Google is the fake script (tests/fake_bridge.py); nothing here talks to Google.
"""

import contextlib
import json
import sys
import os

from aiohttp.test_utils import TestServer

from test_migrate_and_backup_logs import Fresh
from test_wristbands_server import Portal, run

import main  # noqa: E402
from hr import gdrive  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_bridge import HR_DRIVE, RESTRICTED_DRIVE, FakeBridge  # noqa: E402


@contextlib.asynccontextmanager
async def google(monkeypatch, secret):
    """The fake script, serving `secret`, and its Web app URL (accepted as a test address)."""
    fake = FakeBridge(secret)
    server = TestServer(fake.app())
    await server.start_server()
    origin = str(server.make_url("")).rstrip("/")
    monkeypatch.setattr(gdrive, "TEST_ORIGIN", origin)
    monkeypatch.setattr(gdrive, "RETRY_DELAYS", (0.01, 0.01))
    main.google_asked.update(check=0.0, test=0.0)
    try:
        yield fake, f"{origin}/macros/s/FAKE-bridge/exec"
    finally:
        await gdrive.close()
        await server.close()


async def copy_script(p, new=False):
    status, reply = await p.admin_call("POST", "/api/hr-google/script", {"new_secret": True} if new else None)
    assert status == 200, reply
    return reply


def secret_of(p):
    return p.db.settings()["hr_google_secret"]


def test_off_until_set_up_and_nothing_is_asked(tmp_path, monkeypatch):
    async def scenario():
        p = Portal(tmp_path)
        async with p, google(monkeypatch, "unused-" + "s" * 40) as (fake, url):
            status, state = await p.admin_call("GET", "/api/state")
            assert state["hr_google"] == {"set_up": False, "script_made": False, "drive_set": False,
                                          "script_version": gdrive.SCRIPT_VERSION, "checked_at": None,
                                          "email": None, "version": None, "code": None, "error": None}
            status, reply = await p.admin_call("POST", "/api/hr-google/check")
            assert status == 400 and "Copy the script" in reply["error"]
            status, reply = await p.admin_call("POST", "/api/hr-google/test")
            assert status == 400
            assert fake.methods == []
    run(scenario())


def test_copy_the_script_and_make_a_new_secret(tmp_path, monkeypatch):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            first = await copy_script(p)
            secret = secret_of(p)
            assert first["new_secret"] and gdrive.SECRET_RE.fullmatch(secret)
            assert first["script"] == gdrive.script_source(secret)
            # Copying again gives the same script: the one in Google keeps working.
            again = await copy_script(p)
            assert not again["new_secret"] and secret_of(p) == secret
            # The secret is never in the dashboard's state or the Google settings it reads.
            for path in ("/api/state", "/api/hr-google"):
                status, reply = await p.admin_call("GET", path)
                assert status == 200 and secret not in json.dumps(reply)
            status, state = await p.admin_call("GET", "/api/state")
            assert not any(k.startswith("hr_google") for k in state["settings"])
            assert state["hr_google"]["script_made"] and not state["hr_google"]["set_up"]
            # A new secret replaces it; with a Web app URL saved, the status says to paste again.
            p.db.update_settings({"hr_google_url": "https://script.google.com/macros/s/" + "A" * 40 + "/exec"})
            renewed = await copy_script(p, new=True)
            assert renewed["new_secret"] and secret_of(p) != secret
            assert secret_of(p) in renewed["script"] and secret not in renewed["script"]
            status, view = await p.admin_call("GET", "/api/hr-google")
            assert view["code"] == "auth" and "old secret" in view["error"]
            logged = [a["action"] for a in p.db.activity(10)]
            assert logged[:3] == ["Made a new secret for the Google Drive script",
                                  "Copied the Google Drive script", "Made the Google Drive script"]
    run(scenario())


def test_web_app_url_drives_and_test_connection(tmp_path, monkeypatch):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            await copy_script(p)
            async with google(monkeypatch, secret_of(p)) as (fake, url):
                for bad in ("https://example.com/exec", "http://script.google.com/macros/s/x/exec",
                            "https://script.google.com/macros/s/" + "A" * 40 + "/dev"):
                    status, reply = await p.admin_call("PUT", "/api/hr-google", {"url": bad})
                    assert status == 400 and reply["error"]
                assert p.db.settings()["hr_google_url"] == ""
                # A new address is checked at once: who it runs as, and the drives it sees.
                status, reply = await p.admin_call("PUT", "/api/hr-google", {"url": url, "drive": "",
                                                                             "restricted_drive": ""})
                assert status == 200 and reply["problem"] is None
                g = reply["google"]
                assert g["set_up"] and g["email"] == "owner@hotlake.example" and not g["drive_set"]
                assert {d["name"] for d in g["drives"]} >= {"HR Employee Files", "HR Restricted"}
                assert g["url"] == url and "secret" not in json.dumps(g).lower().replace("script_made", "")
                assert fake.allowed == []
                # Picking the drive tells the script it may use it, and only it.
                status, reply = await p.admin_call("PUT", "/api/hr-google", {"url": url, "drive": HR_DRIVE,
                                                                             "restricted_drive": ""})
                assert status == 200 and reply["google"]["drive_set"]
                assert fake.allowed == [HR_DRIVE]
                status, reply = await p.admin_call("PUT", "/api/hr-google", {"drive": HR_DRIVE,
                                                                             "restricted_drive": HR_DRIVE})
                assert status == 400
                status, reply = await p.admin_call("PUT", "/api/hr-google", {"restricted_drive": RESTRICTED_DRIVE})
                assert status == 200 and fake.allowed == [HR_DRIVE, RESTRICTED_DRIVE]
                status, state = await p.admin_call("GET", "/api/state")
                assert state["hr_google"]["email"] == "owner@hotlake.example"
                assert "drives" not in state["hr_google"] and "url" not in state["hr_google"]

                # A copy of the script deployed afresh knows no drives: opening the connection
                # (the check) tells it again.
                fake.allowed = []
                main.google_asked.update(check=0.0)
                status, reply = await p.admin_call("POST", "/api/hr-google/check")
                assert status == 200 and reply["problem"] is None
                assert fake.allowed == [HR_DRIVE, RESTRICTED_DRIVE]
                # Checks a moment apart answer from the last one.
                posts = len(fake.methods)
                status, reply = await p.admin_call("POST", "/api/hr-google/check")
                assert status == 200 and len(fake.methods) == posts

                # Test connection: each check with a plain result.
                status, reply = await p.admin_call("POST", "/api/hr-google/test")
                assert status == 200
                assert [c["check"] for c in reply["checks"]] == [
                    "Sign-in", "Shared drive", "Make and remove a test folder", "Read text from a test image"]
                assert all(c["ok"] for c in reply["checks"])
                assert reply["google"]["code"] is None
                status, reply = await p.admin_call("POST", "/api/hr-google/test")
                assert status == 429
                logged = p.db.activity(5)[0]
                assert (logged["action"], logged["detail"]) == (
                    "Tested the Google Drive connection (HR files)", "Every check passed.")

                # The settings file to paste in appsscript.json asks for full Drive access.
                status, view = await p.admin_call("GET", "/api/hr-google")
                assert status == 200 and '"https://www.googleapis.com/auth/drive"' in view["manifest"]
                assert '"serviceId": "drive"' in view["manifest"]
                # Every failed check is in the activity log, not only the first.
                fake.write_refused = "The script doesn't have full Google Drive access."
                google_asked = __import__("main").google_asked
                google_asked["test"] = google_asked["check"] = 0
                status, reply = await p.admin_call("POST", "/api/hr-google/test")
                assert status == 200 and [c["ok"] for c in reply["checks"]] == [True, True, False, False]
                detail = p.db.activity(1)[0]["detail"]
                assert "Make and remove a test folder:" in detail and "Read text from a test image:" in detail
    run(scenario())


def test_problems_show_in_a_few_words(tmp_path, monkeypatch):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            await copy_script(p)
            async with google(monkeypatch, "another-secret-" + "z" * 40) as (fake, url):
                status, reply = await p.admin_call("PUT", "/api/hr-google", {"url": url})
                assert status == 200 and "secret doesn't match" in reply["problem"]
                assert reply["google"]["code"] == "auth"
                status, reply = await p.admin_call("POST", "/api/hr-google/test")
                assert status == 200, reply
                assert reply["checks"] == [{"check": "Sign-in", "ok": False,
                                            "detail": reply["google"]["error"]}]
                logged = p.db.activity(1)[0]
                assert not logged["ok"] and logged["detail"].startswith("Sign-in:")
            # The right secret, but the script deployed is older than this version's.
            async with google(monkeypatch, secret_of(p)) as (fake, url):
                fake.version = gdrive.SCRIPT_VERSION - 1
                status, reply = await p.admin_call("PUT", "/api/hr-google", {"url": url})
                assert reply["google"]["code"] == "outdated" and "New version" in reply["problem"]
                status, state = await p.admin_call("GET", "/api/state")
                assert state["hr_google"]["code"] == "outdated"
                # Fixed: the next check clears it.
                fake.version = gdrive.SCRIPT_VERSION
                main.google_asked.update(check=0.0)
                status, reply = await p.admin_call("POST", "/api/hr-google/check")
                assert reply["google"]["code"] is None and reply["google"]["email"]
    run(scenario())


def test_changes_need_the_dashboard_header(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            for method, path in (("POST", "/api/hr-google/script"), ("PUT", "/api/hr-google"),
                                 ("POST", "/api/hr-google/test"), ("POST", "/api/hr-google/check")):
                resp = await p.admin.request(method, path, json={})
                assert resp.status == 403
            assert p.db.settings()["hr_google_secret"] == ""
    run(scenario())


def test_google_settings_stay_out_of_backups_but_travel_in_migrations(tmp_path):
    async def scenario():
        p = Portal(tmp_path / "old")
        url = "https://script.google.com/macros/s/AKfycb" + "Q" * 40 + "/exec"
        async with p:
            await copy_script(p)
            secret = secret_of(p)
            p.db.update_settings({"hr_google_url": url, "hr_google_drive": HR_DRIVE,
                                  "hr_google_restricted_drive": RESTRICTED_DRIVE,
                                  "hr_connecteam_usage": json.dumps({"day": "2026-10-26", "count": 9})})
            # A backup has none of it, and restoring one changes none of it.
            status, backup = await p.admin_call("GET", "/api/backup")
            assert status == 200
            text = json.dumps(backup)
            assert secret not in text and url not in text and HR_DRIVE not in text
            backup["settings"].update(hr_google_url="https://script.google.com/macros/s/" + "E" * 40 + "/exec",
                                      hr_google_secret="evil-" + "e" * 40, hr_google_drive="")
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, reply
            s = p.db.settings()
            assert (s["hr_google_url"], s["hr_google_secret"], s["hr_google_drive"]) == (url, secret, HR_DRIVE)
            status, made = await p.admin_call("POST", "/api/migrate/create")
            assert status == 200

        f = Fresh(tmp_path / "new")
        async with f:
            status, reply = await f.admin_call("POST", "/api/migrate/restore",
                                               {"file": made["file"], "code": made["code"]})
            assert status == 200, reply
            s = f.db.settings()
            assert (s["hr_google_url"], s["hr_google_secret"], s["hr_google_drive"],
                    s["hr_google_restricted_drive"]) == (url, secret, HR_DRIVE, RESTRICTED_DRIVE)
            assert json.loads(s["hr_connecteam_usage"])["count"] == 9
            assert gdrive.bridge_from_settings(s).url == url
    run(scenario())


def test_damaged_google_settings_in_a_migration_are_dropped():
    import migrate
    good = {"hr_google_url": "https://script.google.com/macros/s/" + "A" * 40 + "/exec",
            "hr_google_secret": "s" * 43, "hr_google_drive": HR_DRIVE,
            "hr_google_restricted_drive": HR_DRIVE}
    assert migrate._google_settings(good) == {**good, "hr_google_restricted_drive": ""}
    for bad in ({**good, "hr_google_url": "https://evil.example/exec"},
                {**good, "hr_google_secret": 'x"; alert(1); "' + "x" * 40},
                {**good, "hr_google_drive": "../../"}, {}):
        assert migrate._google_settings(bad) == {}


def test_hr_lane_is_set_up_and_quiet(tmp_path):
    # The HR portal's Connecteam lane exists from the start, inside Staff Access's budget, and
    # sends nothing on its own.
    Portal(tmp_path)
    assert main.hr_connecteam is main.ctapi.lane()
    assert main.hr_connecteam.ct is main.connecteam
    assert main.hr_connecteam.usage()["count"] == 0
    assert main.hr_connecteam.status()["paused"] is None
    # One HR lane: the HR people sync and these requests count in the same place.
    assert main.hr_connecteam.usage() is not None and main.connecteam.hr_usage()["count"] == 0
    assert main.hr_connecteam.get_settings() == {"client_id": "", "client_secret": "", "documents": False}
