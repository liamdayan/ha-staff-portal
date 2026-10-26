"""The HR portal's Google Drive client (app/hr/gdrive.py) against the fake script
(tests/fake_bridge.py): signing, Google's redirect, the drive operations, retries and limits,
and errors in plain words. Nothing here talks to Google."""

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
from aiohttp.test_utils import TestServer

from hr import gdrive
from hr.gdrive import Bridge, BridgeError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_bridge import HR_DRIVE, OTHER_DRIVE, RESTRICTED_DRIVE, FakeBridge  # noqa: E402

SECRET = "test-secret-" + "x" * 30


def run(coro):
    return asyncio.run(coro)


@contextlib.asynccontextmanager
async def bridge_and_fake(secret=SECRET, **fake_options):
    fake = FakeBridge(SECRET, **fake_options)
    server = TestServer(fake.app())
    await server.start_server()
    try:
        yield Bridge(str(server.make_url("/macros/s/FAKE-bridge/exec")), secret), fake
    finally:
        await gdrive.close()
        await server.close()


@pytest.fixture(autouse=True)
def quick_retries(monkeypatch):
    monkeypatch.setattr(gdrive, "RETRY_DELAYS", (0.01, 0.01))


def test_ping_follows_googles_redirect_with_a_get():
    async def scenario():
        async with bridge_and_fake() as (bridge, fake):
            info = await bridge.ping()
            assert info["email"] == "owner@hotlake.example"
            assert info["version"] == gdrive.SCRIPT_VERSION
            assert {d["name"] for d in info["drives"]} == {"HR Employee Files", "HR Restricted",
                                                          "Marketing"}
            assert info["allowed"] == []
            # A POST to /exec, then a GET of the address Google sends back.
            assert fake.methods == [("POST", "/macros/s/FAKE-bridge/exec"), ("GET", "/macros/echo")]
    run(scenario())


def test_folders_files_and_reading_text():
    async def scenario():
        async with bridge_and_fake() as (bridge, fake):
            assert [d["name"] for d in await bridge.setup([HR_DRIVE, RESTRICTED_DRIVE])] == [
                "HR Employee Files", "HR Restricted"]
            assert fake.allowed == [HR_DRIVE, RESTRICTED_DRIVE]
            employees = await bridge.ensure_folder(HR_DRIVE, "Employees", {"hr": "employees"})
            assert await bridge.ensure_folder(HR_DRIVE, "Employees", {"hr": "employees"}) == employees
            jane = await bridge.ensure_folder(employees, "Smith, Jane (CT 101)", {"ct_user": "101"})
            certs = await bridge.ensure_folder(jane, "Certificates", {"part": "certificates"})
            photo = os.urandom(200_000)
            up = await bridge.upload(certs, "2026-10-25 OLCC Service Permit - expires 2031-10-24.jpg",
                                     "image/jpeg", photo, {"upload_id": "card-1", "ct_user": "101"})
            assert up["size"] == len(photo) and up["webViewLink"].startswith("https://")
            # The same upload again (a retry after a timeout) gives the same file.
            again = await bridge.upload(certs, "other name.jpg", "image/jpeg", photo,
                                        {"upload_id": "card-1"})
            assert again["id"] == up["id"]
            assert len([f for f in fake.children(certs)]) == 1
            assert await bridge.download(up["id"]) == photo
            # Without an upload_id one is made from the folder, name and bytes: still no copy.
            a = await bridge.upload(certs, "scan.pdf", "application/pdf", b"%PDF-1.4 x", {})
            b = await bridge.upload(certs, "scan.pdf", "application/pdf", b"%PDF-1.4 x", {})
            assert a["id"] == b["id"]
            await bridge.rename(jane, "Jones, Jane (CT 101)")
            assert fake.files[jane]["name"] == "Jones, Jane (CT 101)"
            former = await bridge.ensure_folder(HR_DRIVE, "Former employees", {"hr": "former"})
            await bridge.move(jane, former)
            assert fake.files[jane]["parents"] == [former]
            await bridge.trash(a["id"])
            assert fake.files[a["id"]]["trashed"]
            # Reading text: the fake answers with what's registered for these bytes.
            card = b"\xff\xd8\xff fake jpeg"
            fake.set_ocr_text(card, "OREGON FOOD HANDLER CARD\nExpires 06/14/2029")
            assert await bridge.ocr(card, "image/jpeg") == "OREGON FOOD HANDLER CARD\nExpires 06/14/2029"
            assert any(f["name"] == "_OCR scratch" for f in fake.children(HR_DRIVE))
            # A PDF carries its own text for the fake.
            assert await bridge.ocr(b"%PDF-1.4\n%OCR-TEXT: Valid through 1/2/2030\n", "application/pdf") \
                == "Valid through 1/2/2030"
            with pytest.raises(ValueError):
                await bridge.ocr(card, "image/heic")
    run(scenario())


def test_the_script_keeps_files_inside_the_chosen_drives():
    async def scenario():
        async with bridge_and_fake() as (bridge, fake):
            with pytest.raises(BridgeError) as err:
                await bridge.ensure_folder(HR_DRIVE, "Employees", {"hr": "employees"})
            assert err.value.code == "not_allowed" and "Pick the HR shared drive" in err.value.message
            await bridge.setup([HR_DRIVE])
            outside = fake.add("Plan.pdf", OTHER_DRIVE, "application/pdf", b"%PDF")
            for call in (bridge.rename(outside["id"], "x"), bridge.trash(outside["id"]),
                         bridge.download(outside["id"]),
                         bridge.ensure_folder(RESTRICTED_DRIVE, "x", {"a": "1"}),
                         bridge.upload(OTHER_DRIVE, "x.pdf", "application/pdf", b"x", {})):
                with pytest.raises(BridgeError) as err:
                    await call
                assert err.value.code == "not_allowed"
            assert fake.files[outside["id"]]["name"] == "Plan.pdf"
    run(scenario())


def test_wrong_secret_is_refused_at_once():
    async def scenario():
        async with bridge_and_fake(secret="another-secret-" + "y" * 30) as (bridge, fake):
            with pytest.raises(BridgeError) as err:
                await bridge.ping()
            assert err.value.code == "auth"
            assert "secret doesn't match" in err.value.message
            assert len([m for m in fake.methods if m[0] == "POST"]) == 1   # never retried
            checks = await bridge.self_test()
            assert checks == [{"check": "Sign-in", "ok": False, "detail": err.value.message}]
    run(scenario())


def test_an_older_script_says_it_is_out_of_date(monkeypatch):
    async def scenario():
        async with bridge_and_fake() as (bridge, fake):
            await bridge.setup([HR_DRIVE])
            monkeypatch.setattr(gdrive, "SCRIPT_VERSION", gdrive.SCRIPT_VERSION + 1)
            with pytest.raises(BridgeError) as err:
                await bridge.ping()
            assert err.value.code == "outdated" and "New version" in err.value.message
            # File work goes on with a script that still speaks this protocol...
            assert await bridge.ensure_folder(HR_DRIVE, "Employees", {"hr": "employees"})
            # ...and Test connection says it's out of date.
            checks = await bridge.self_test()
            assert checks[-1]["check"] == "Script version" and not checks[-1]["ok"]
            # One too old for it, or an op it doesn't know, is out of date too.
            fake.version = 0
            with pytest.raises(BridgeError) as err:
                await bridge.ensure_folder(HR_DRIVE, "Employees", {"hr": "employees"})
            assert err.value.code == "outdated"
            fake.version = gdrive.SCRIPT_VERSION
            fake.op_trash = None
            with pytest.raises(BridgeError) as err:
                await bridge.trash(HR_DRIVE)
            assert err.value.code == "outdated"
    run(scenario())


def test_retries_on_busy_and_server_errors_each_signed_afresh():
    async def scenario():
        async with bridge_and_fake() as (bridge, fake):
            fake.fail_next(status=503)
            fake.fail_next(status=429)
            assert (await bridge.ping())["email"]
            assert len([m for m in fake.methods if m[0] == "POST"]) == 3
            # A failure after the script used the request's nonce: the retry is a new request.
            fake.fail_next(status=502, after_nonce=True)
            assert (await bridge.ping())["email"]
            # Still failing after three tries: a plain reason.
            fake.fail_next(status=429, times=3)
            with pytest.raises(BridgeError) as err:
                await bridge.ping()
            assert err.value.code == "quota"
            fake.fail_next(status=500, times=3)
            with pytest.raises(BridgeError) as err:
                await bridge.ping()
            assert err.value.code == "unavailable"
            # The script's own "busy" answer is passed on (no retry: Staff Access tries later).
            fake.fail_next(code="quota", message="Google is busy.")
            with pytest.raises(BridgeError) as err:
                await bridge.ping()
            assert (err.value.code, err.value.message) == ("quota", "Google is busy.")
    run(scenario())


def test_a_slow_answer_times_out_and_is_retried(monkeypatch):
    monkeypatch.setattr(gdrive, "SHORT_TIMEOUT", 0.2)

    async def scenario():
        async with bridge_and_fake() as (bridge, fake):
            fake.delay = 0.5
            started = time.monotonic()
            with pytest.raises(BridgeError) as err:
                await bridge.ping()
            assert err.value.code == "unavailable" and "in time" in err.value.message
            assert len([m for m in fake.methods if m[0] == "POST"]) == 3
            assert time.monotonic() - started < 5
    run(scenario())


def test_only_googles_answer_host_is_followed():
    async def scenario():
        async with bridge_and_fake() as (bridge, fake):
            fake.redirect_to = "http://evil.example/steal"
            with pytest.raises(BridgeError) as err:
                await bridge.ping()
            assert err.value.code == "bad_reply"
            fake.redirect_to = "https://accounts.google.com/ServiceLogin"
            with pytest.raises(BridgeError) as err:
                await bridge.ping()
            assert err.value.code == "not_allowed" and "Who has access to Anyone" in err.value.message
            assert not any(m == ("GET", "/steal") for m in fake.methods)
        # Google's real answer host is allowed, over https only.
        bridge = Bridge("https://script.google.com/macros/s/" + "A" * 30 + "/exec", SECRET)
        assert bridge._redirect("https://script.googleusercontent.com/macros/echo?x=1").startswith(
            "https://script.googleusercontent.com/")
        with pytest.raises(BridgeError):
            bridge._redirect("http://script.googleusercontent.com/macros/echo")
        with pytest.raises(BridgeError):
            bridge._redirect("https://script.googleusercontent.com.evil.example/")
    run(scenario())


def test_googles_pages_are_explained():
    async def scenario():
        async with bridge_and_fake() as (bridge, fake):
            for status in (401, 403, 404):
                fake.fail_next(status=status)
                with pytest.raises(BridgeError) as err:
                    await bridge.ping()
                assert err.value.code == "not_allowed"
            fake.fail_next(page="<html>Script function not found: doPost</html>")
            with pytest.raises(BridgeError) as err:
                await bridge.ping()
            assert err.value.code == "bad_reply" and "isn't there" in err.value.message
            fake.fail_next(page="<html>Something else</html>")
            with pytest.raises(BridgeError) as err:
                await bridge.ping()
            assert err.value.code == "bad_reply" and "a page instead" in err.value.message
    run(scenario())


def test_files_over_25_mb_never_leave():
    async def scenario():
        async with bridge_and_fake() as (bridge, fake):
            await bridge.setup([HR_DRIVE])
            sent = len(fake.methods)
            big = b"x" * (gdrive.MAX_BYTES + 1)
            with pytest.raises(BridgeError) as err:
                await bridge.upload(HR_DRIVE, "big.pdf", "application/pdf", big, {})
            assert err.value.code == "too_big" and "25 MB" in err.value.message
            with pytest.raises(BridgeError):
                await bridge.ocr(big, "application/pdf")
            assert len(fake.methods) == sent
            stored = fake.add("huge.pdf", HR_DRIVE, "application/pdf", big)
            with pytest.raises(BridgeError) as err:
                await bridge.download(stored["id"])
            assert err.value.code == "too_big"
    run(scenario())


def test_at_most_two_requests_at_once():
    async def scenario():
        async with bridge_and_fake() as (bridge, fake):
            await bridge.setup([HR_DRIVE])
            fake.delay = 0.1
            ids = await asyncio.gather(*(bridge.ensure_folder(HR_DRIVE, f"F{i}", {"n": str(i)})
                                         for i in range(6)))
            assert len(set(ids)) == 6
            assert fake.most_at_once == 2
    run(scenario())


def test_self_test_checks_each_part():
    async def scenario():
        async with bridge_and_fake() as (bridge, fake):
            checks = await bridge.self_test()
            assert [c["ok"] for c in checks] == [True, False, False, False]
            await bridge.setup([HR_DRIVE])
            checks = await bridge.self_test()
            assert [c["check"] for c in checks] == ["Sign-in", "Shared drive",
                                                    "Make and remove a test folder",
                                                    "Read text from a test image"]
            assert all(c["ok"] for c in checks)
    run(scenario())


def test_bad_arguments_never_reach_google():
    async def scenario():
        async with bridge_and_fake() as (bridge, fake):
            for call in (bridge.rename("../etc", "x"), bridge.ensure_folder(HR_DRIVE, "", {}),
                         bridge.ensure_folder(HR_DRIVE, "x", {"bad key": "1"}),
                         bridge.ensure_folder(HR_DRIVE, "x", {"k": "v" * 200}),
                         bridge.upload(HR_DRIVE, "x", "application/vnd.google-apps.document", b"x", {}),
                         bridge.setup([HR_DRIVE, HR_DRIVE]), bridge.setup([])):
                with pytest.raises(ValueError):
                    await call
            assert fake.methods == []
    run(scenario())


def test_signing_rules():
    # Only what both languages write the same way can be signed.
    for bad in ({"n": 1.5}, {"n": 2 ** 60}, {"n": float("nan")}, {1: "x"}, {"s": {1, 2}}):
        with pytest.raises(ValueError):
            gdrive.canonical(bad)
    assert gdrive.canonical({"b": 1, "a": ["\u00e9", None, True]}) == '{"a":["\\u00e9",null,true],"b":1}'
    one = gdrive.signed_request(SECRET, "ping", {})
    two = gdrive.signed_request(SECRET, "ping", {})
    assert one["nonce"] != two["nonce"] and one["sig"] != two["sig"]
    assert one["sig"] == gdrive.signature(SECRET, 1, "ping", one["ts"], one["nonce"], {})
    assert SECRET not in json.dumps(one)


def test_web_app_url_and_settings():
    good = "https://script.google.com/macros/s/AKfycbx" + "A" * 40 + "/exec"
    workspace = "https://script.google.com/a/macros/hotlakelodge.com/s/AKfycbx" + "B" * 40 + "/exec"
    assert gdrive.clean_url(f"  {good}  ") == good
    assert gdrive.clean_url(good + "?x=1") == good
    assert gdrive.clean_url(workspace) == workspace
    assert gdrive.clean_url("") == ""
    for bad in (good.replace("https", "http"), good.replace("/exec", "/dev"),
                "https://script.google.com.evil.example/macros/s/" + "A" * 40 + "/exec",
                "https://evil.example/macros/s/" + "A" * 40 + "/exec", "javascript:alert(1)",
                "http://127.0.0.1:4255/macros/s/FAKE-bridge/exec"):
        with pytest.raises(ValueError):
            gdrive.clean_url(bad)
    with pytest.raises(ValueError, match="/exec"):
        gdrive.clean_url(good.replace("/exec", "/dev"))
    secret = gdrive.new_secret()
    assert gdrive.bridge_from_settings({}) is None
    assert gdrive.bridge_from_settings({"hr_google_url": good, "hr_google_secret": ""}) is None
    assert gdrive.bridge_from_settings({"hr_google_url": "nope", "hr_google_secret": secret}) is None
    bridge = gdrive.bridge_from_settings({"hr_google_url": good, "hr_google_secret": secret})
    assert bridge.url == good and bridge.secret == secret
    assert gdrive.drive_ids({"hr_google_drive": HR_DRIVE, "hr_google_restricted_drive": ""}) == [HR_DRIVE]
    assert gdrive.drive_ids({"hr_google_drive": HR_DRIVE,
                             "hr_google_restricted_drive": RESTRICTED_DRIVE}) == [HR_DRIVE, RESTRICTED_DRIVE]
    assert gdrive.drive_ids({}) == []
    assert gdrive.clean_drive_id(HR_DRIVE) == HR_DRIVE and gdrive.clean_drive_id(" ") == ""
    with pytest.raises(ValueError):
        gdrive.clean_drive_id("x'; drop")


def test_the_test_address_is_only_accepted_when_set(monkeypatch):
    monkeypatch.setattr(gdrive, "TEST_ORIGIN", "http://127.0.0.1:4255")
    assert gdrive.clean_url("http://127.0.0.1:4255/macros/s/FAKE-bridge/exec").startswith("http://127")
    with pytest.raises(ValueError):
        gdrive.clean_url("http://127.0.0.1:4256/macros/s/FAKE-bridge/exec")


def test_errors_use_the_agreed_codes():
    assert set(gdrive.SCRIPT_CODES.values()) <= set(gdrive.ERROR_CODES)
    assert gdrive._script_error({"code": "bad_request", "message": "x"}).code == "bad_reply"
    assert gdrive._script_error("junk").code == "bad_reply"
    assert gdrive._script_error({"code": "not_allowed", "message": "Nope."}).message == "Nope."


def test_fake_bridge_runs_on_its_own(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_bridge.py")
    proc = subprocess.Popen([sys.executable, script, "--port", str(port), "--secret", SECRET],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        assert "macros/s/FAKE-bridge/exec" in proc.stdout.readline()
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/macros/s/FAKE-bridge/exec") as r:
                    assert b"working" in r.read()
                break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("the fake didn't start")

        async def scenario():
            bridge = Bridge(f"http://127.0.0.1:{port}/macros/s/FAKE-bridge/exec", SECRET)
            try:
                assert (await bridge.ping())["email"] == "owner@hotlake.example"
            finally:
                await gdrive.close()
        run(scenario())
    finally:
        proc.terminate()
        proc.wait(timeout=10)
