"""Scans through the server: who's offered the tool, scanning from the feeder and the glass,
saving to the Google shared drive for scans with the option's file name (numbered from the
second), problems said plainly, and the dashboard's settings. The scanner is fake_escl.py and
Google is fake_bridge.py: nothing here talks to a real scanner or to Google."""

import asyncio
import base64
import json
import time

import pytest
from aiohttp.test_utils import TestServer

from fake_bridge import HR_DRIVE, OTHER_DRIVE, RESTRICTED_DRIVE, FakeBridge
from fake_escl import FakeScanner
from test_wristbands_server import Portal, fake_area_layout, fake_states

import main  # noqa: E402
from hr import gdrive  # noqa: E402

SECRET = "s" * 43
SCANS_DRIVE = OTHER_DRIVE


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A portal with two departments (Ana and Dee at the Front, Bob in the Kitchen, Cy in none),
    and the in-memory scan state cleared."""
    monkeypatch.setattr(main, "area_layout", fake_area_layout)
    monkeypatch.setattr(main.ha, "states", fake_states)
    monkeypatch.setattr(gdrive, "RETRY_DELAYS", (0.01, 0.01))
    for table in (main.recent_scan_requests, main.scan_jobs):
        table.clear()
    main.scanner_user[0] = None
    main.scan_tested[0] = 0.0
    main.google_asked.update(check=0.0, test=0.0)
    p = Portal(tmp_path / "db")
    p.front = p.db.create_department("Front")
    p.kitchen = p.db.create_department("Kitchen")
    for person, dept in ((p.ana, p.front), (p.dee, p.front), (p.bob, p.kitchen)):
        p.db.update_staff(person, department_ids=[dept])
    p.monkeypatch = monkeypatch
    return p


class Set:
    """The fake scanner and fake Google, started, with the portal pointed at both."""

    def __init__(self, p, feeder=0, version=None, connect=True):
        self.p, self.connect = p, connect
        self.scanner = FakeScanner(feeder=feeder)
        self.google = FakeBridge(SECRET)
        if version is not None:
            self.google.version = version

    async def __aenter__(self):
        await self.scanner.__aenter__()
        self.server = TestServer(self.google.app())
        await self.server.start_server()
        origin = str(self.server.make_url("")).rstrip("/")
        self.p.monkeypatch.setattr(gdrive, "TEST_ORIGIN", origin)
        if self.connect:
            self.p.db.update_settings({
                "hr_google_url": f"{origin}/macros/s/FAKE-bridge/exec", "hr_google_secret": SECRET,
                "hr_google_drive": HR_DRIVE, "scan_address": self.scanner.address,
                "scan_google_drive": SCANS_DRIVE,
                "hr_google_status": json.dumps({"version": self.google.version})})
            self.google.allowed = [HR_DRIVE, SCANS_DRIVE]
        return self

    async def __aexit__(self, *exc):
        await gdrive.close()
        await self.server.close()
        await self.scanner.__aexit__(*exc)

    def files(self) -> list[tuple[str, str]]:
        """(folder name, file name) of every file in the scans drive, oldest first."""
        names = {i: f["name"] for i, f in self.google.files.items()}
        return [(names.get(f["parents"][0], "?"), f["name"]) for f in self.google.files.values()
                if f.get("data") is not None and not f["trashed"]]


def option(p, label="Invoices", prefix="INV", departments=None, **more) -> int:
    return p.db.save_scan_option(None, main.scans.clean_option(
        {"label": label, "prefix": prefix, "departments": [p.front] if departments is None else departments,
         **more}, {p.front, p.kitchen}))


async def until(p, who, check, timeout=10.0) -> dict:
    """Poll the scan in hand, the way the app does, until check(job) holds."""
    deadline = time.monotonic() + timeout
    while True:
        status, reply = await p.call("GET", "/api/scans/job", who)
        assert status == 200, reply
        if reply["job"] and check(reply["job"]):
            return reply["job"]
        assert time.monotonic() < deadline, reply
        await asyncio.sleep(0.02)


ready = lambda job: job["state"] == "ready"
done = lambda job: job["state"] == "done"


# ---------------------------------------------------------------- who gets it

def test_scans_is_for_people_in_an_options_departments(world):
    p = world
    front = option(p)
    option(p, "Kitchen logs", "KIT", [p.kitchen])
    option(p, "Off for now", "OFF", [p.front], enabled=False)

    async def scenario():
        async with Set(p), p:
            ana, bob, cy = await p.sign_in(p.ana), await p.sign_in(p.bob), await p.sign_in(p.cy)
            assert (await p.call("GET", "/api/entities", ana))[1]["scans"] is True
            assert (await p.call("GET", "/api/entities", cy))[1]["scans"] is False
            status, reply = await p.call("GET", "/api/scans", ana)
            assert status == 200 and reply == {"options": [{"id": front, "label": "Invoices"}], "job": None,
                                               "problem": None}
            assert [o["label"] for o in (await p.call("GET", "/api/scans", bob))[1]["options"]] == ["Kitchen logs"]
            # Nobody's departments: no tool, and nothing by asking for it anyway.
            status, reply = await p.call("GET", "/api/scans", cy)
            assert status == 403 and reply["error"] == main.NO_SCANS
            assert (await p.call("POST", f"/api/scans/{front}/scan", cy))[0] == 403
            assert ("Cy on Front Desk PC", "Blocked: scans", False) in p.activity()
            # Someone else's option, one turned off, and one that isn't there: all "not available".
            for other in (front + 1, front + 2, 9999):
                status, reply = await p.call("POST", f"/api/scans/{other}/scan", ana)
                assert status == 404 and "isn't available" in reply["error"]
    asyncio.run(scenario())


# ---------------------------------------------------------------- scanning and saving

def test_the_feeder_is_scanned_and_saved_with_the_options_name(world):
    p = world
    invoices = option(p, folder="Front desk invoices")

    async def scenario():
        async with Set(p, feeder=3) as s, p:
            ana, dee = await p.sign_in(p.ana), await p.sign_in(p.dee)
            status, reply = await p.call("POST", f"/api/scans/{invoices}/scan", ana)
            assert status == 200 and reply["job"]["state"] == "scanning" and reply["job"]["option"] == "Invoices"
            job = await until(p, ana, ready)
            assert (job["pages"], job["source"], job["message"]) == (3, "Feeder", "")
            assert "pages" in job and not any(isinstance(v, (bytes, list)) for v in job.values())
            assert "Grayscale8" not in s.scanner.jobs[0] and "<scan:XResolution>200" in s.scanner.jobs[0]
            # Dee can't see or save Ana's scan.
            assert (await p.call("GET", "/api/scans/job", dee))[1]["job"] is None
            assert (await p.call("POST", "/api/scans/job/save", dee))[0] == 404

            status, reply = await p.call("POST", "/api/scans/job/save", ana)
            assert status == 200 and reply["job"]["state"] == "saving"
            job = await until(p, ana, done)
            assert (job["name"], job["pages"]) == ("INV-Ana.pdf", 3)
            assert s.files() == [("Front desk invoices", "INV-Ana.pdf")]
            saved = next(f for f in s.google.files.values() if f["name"] == "INV-Ana.pdf")
            assert saved["driveId"] == SCANS_DRIVE and saved["data"].startswith(b"%PDF-1.4")
            assert saved["data"].count(b"/Type /Page ") == 3
            assert ("Ana on Front Desk PC", "Scanned INV-Ana.pdf", True) in p.activity()
            assert (await p.call("POST", "/api/scans/job/save", ana))[0] == 409   # already saved

            # The same thing again: numbered from the second, whoever scans it under that name.
            for expected in ("INV-Ana-2.pdf", "INV-Ana-3.pdf"):
                s.scanner.feeder = 1
                await p.call("POST", f"/api/scans/{invoices}/scan", ana)
                await until(p, ana, ready)
                await p.call("POST", "/api/scans/job/save", ana)
                assert (await until(p, ana, done))["name"] == expected
            s.scanner.feeder = 1
            await p.call("POST", f"/api/scans/{invoices}/scan", dee)
            await until(p, dee, ready)
            await p.call("POST", "/api/scans/job/save", dee)
            assert (await until(p, dee, done))["name"] == "INV-Dee.pdf"
            assert [name for _, name in s.files()] == ["INV-Ana.pdf", "INV-Ana-2.pdf", "INV-Ana-3.pdf", "INV-Dee.pdf"]
            # The dashboard lists them, newest first.
            status, view = await p.admin_call("GET", "/api/scans")
            assert [f["name"] for f in view["files"]][:2] == ["INV-Dee.pdf", "INV-Ana-3.pdf"]
            assert view["files"][0]["by"] == "Dee on Front Desk PC" and view["files"][0]["option"] == "Invoices"
    asyncio.run(scenario())


def test_a_scanner_that_does_not_say_about_its_feeder_still_uses_it(world):
    # 0.12.29 scanned the glass whenever the scanner's status didn't say "ScannerAdfLoaded", so
    # a scanner that leaves the feeder out of its status never pulled paper from the feeder.
    p = world
    ids = option(p, "Guest IDs", "ID", paper="legal")

    async def scenario():
        async with Set(p, feeder=3) as s, p:
            s.scanner.adf_state = None
            ana = await p.sign_in(p.ana)
            await p.call("POST", f"/api/scans/{ids}/scan", ana)
            job = await until(p, ana, ready)
            assert (job["pages"], job["source"], job["message"]) == (3, "Feeder", "")
            assert "<pwg:Height>4200</pwg:Height>" in s.scanner.jobs[0]       # Legal in the feeder
            # The feeder's empty now: the same button takes the page on the glass, as Letter.
            await p.call("POST", "/api/scans/job/more", ana)
            job = await until(p, ana, lambda j: ready(j) and j["pages"] == 4)
            assert job["source"] == "Platen" and job["message"] == ""
            assert "<pwg:InputSource>Platen</pwg:InputSource>" in s.scanner.jobs[-1]
            assert "<pwg:Height>3300</pwg:Height>" in s.scanner.jobs[-1]
    asyncio.run(scenario())


def test_the_pages_can_be_looked_over_before_saving(world, caplog):
    p = world
    invoices = option(p)

    async def scenario():
        async with Set(p, feeder=2) as s, p:
            ana, dee = await p.sign_in(p.ana), await p.sign_in(p.dee)
            caplog.set_level("INFO", logger=main.log.name)
            await p.call("POST", f"/api/scans/{invoices}/scan", ana)
            await until(p, ana, ready)
            # What was asked of the scanner and how it answered is in the log.
            said = "\n".join(r.getMessage() for r in caplog.records if r.getMessage().startswith("Scan to Invoices"))
            assert "2 pages in hand from the feeder" in said and "POST ScanJobs (Feeder" in said
            # Each page, small, to look over.
            for n in (1, 2):
                status, reply = await p.call("GET", f"/api/scans/job/page/{n}", ana)
                assert status == 200 and reply["image"].startswith("data:image/jpeg;base64,")
                assert base64.b64decode(reply["image"].split(",", 1)[1])[:2] == b"\xff\xd8"
            assert (await p.call("GET", "/api/scans/job/page/3", ana))[0] == 404
            assert (await p.call("GET", "/api/scans/job/page/0", ana))[0] == 404
            assert (await p.call("GET", "/api/scans/job/page/1", dee))[0] == 404      # not Dee's
            # Once it's saved, the pages are gone from here.
            await p.call("POST", "/api/scans/job/save", ana)
            await until(p, ana, done)
            assert (await p.call("GET", "/api/scans/job/page/1", ana))[0] == 404
            assert main.scan_jobs[p.ana]["previews"] == {}
    asyncio.run(scenario())


def test_a_scanner_that_scans_its_glass_when_asked_for_the_feeder_is_said_plainly(world, caplog):
    p = world
    invoices = option(p)

    async def scenario():
        async with Set(p, feeder=3) as s, p:
            s.scanner.ignores_request = True
            main.scan_capabilities_logged[0] = None
            caplog.set_level("INFO", logger=main.log.name)
            ana = await p.sign_in(p.ana)
            await p.call("POST", f"/api/scans/{invoices}/scan", ana)
            job = await until(p, ana, ready)
            assert job["pages"] == 1 and "didn't scan the way it was asked" in job["message"]
            assert job["source"] is None        # the app doesn't say "from the feeder"
            said = [r.getMessage() for r in caplog.records]
            assert sum("The scanner's capabilities" in m for m in said) == 1
            assert any("<pwg:MakeAndModel>Brother MFC-J4345DW</pwg:MakeAndModel>" in m for m in said)
            assert any("not what was asked for" in m and "record of the job" in m for m in said)
            # The capabilities aren't logged again for the next scan.
            await p.call("POST", "/api/scans/job/more", ana)
            await until(p, ana, lambda j: ready(j) and j["pages"] == 2)
            assert sum("The scanner's capabilities" in r.getMessage() for r in caplog.records) == 1
    asyncio.run(scenario())


def test_the_glass_takes_a_page_at_a_time_into_one_file(world):
    p = world
    ids = option(p, "Guest IDs", "ID", format="{prefix}-{date}-{first}", color="gray", quality="high", paper="legal")

    async def scenario():
        async with Set(p, feeder=0) as s, p:
            ana = await p.sign_in(p.ana)
            await p.call("POST", f"/api/scans/{ids}/scan", ana)
            job = await until(p, ana, ready)
            assert (job["pages"], job["source"]) == (1, "Platen")
            first = s.scanner.jobs[0]
            assert "Grayscale8" in first and "<scan:XResolution>300" in first
            assert "<pwg:Height>3300</pwg:Height>" in first        # Legal doesn't fit the glass: Letter
            # Two more pages: one from the glass, then two through the feeder (Legal there).
            await p.call("POST", "/api/scans/job/more", ana)
            assert (await until(p, ana, lambda j: ready(j) and j["pages"] == 2))["source"] == "Platen"
            s.scanner.feeder = 2
            await p.call("POST", "/api/scans/job/more", ana)
            assert (await until(p, ana, lambda j: ready(j) and j["pages"] == 4))["source"] == "Feeder"
            assert "<pwg:Height>4200</pwg:Height>" in s.scanner.jobs[2]
            await p.call("POST", "/api/scans/job/save", ana)
            job = await until(p, ana, done)
            today = main.datetime.now(main.report_zone()).strftime("%Y-%m-%d")
            assert job["name"] == f"ID-{today}-Ana.pdf" and job["pages"] == 4
            assert s.files() == [("Guest IDs", f"ID-{today}-Ana.pdf")]
            # Starting again drops what wasn't saved.
            await p.call("POST", f"/api/scans/{ids}/scan", ana)
            await until(p, ana, ready)
            assert (await p.call("POST", "/api/scans/job/cancel", ana))[1] == {"job": None}
            assert (await p.call("GET", "/api/scans/job", ana))[1]["job"] is None
            assert len(s.files()) == 1
    asyncio.run(scenario())


def test_problems_are_said_plainly_and_nothing_is_lost(world):
    p = world
    invoices = option(p)

    async def scenario():
        async with Set(p, feeder=2) as s, p:
            ana, dee = await p.sign_in(p.ana), await p.sign_in(p.dee)
            # A jam part way: nothing from that pass is kept, and the scan says why.
            s.scanner.jam_after = 1
            await p.call("POST", f"/api/scans/{invoices}/scan", ana)
            job = await until(p, ana, lambda j: j["state"] == "failed")
            assert job["pages"] == 0 and "paper jam" in job["message"]
            assert (await p.call("POST", "/api/scans/job/save", ana))[0] == 409
            # While Ana's scan runs, Dee is told the scanner is in use.
            s.scanner.jam_after, s.scanner.feeder, s.scanner.not_ready = None, 2, 3
            await p.call("POST", f"/api/scans/{invoices}/scan", ana)
            status, reply = await p.call("POST", f"/api/scans/{invoices}/scan", dee)
            assert status == 409 and reply["error"] == main.SCANNER_IN_USE
            assert (await p.call("POST", "/api/scans/job/save", ana))[0] == 409      # still scanning
            await until(p, ana, ready)
            # Google is down: the pages stay in hand, the name isn't used up, and it works next time.
            s.google.fail_next(status=500, times=5, op="upload")
            await p.call("POST", "/api/scans/job/save", ana)
            job = await until(p, ana, lambda j: ready(j) and j["message"])
            assert job["pages"] == 2 and "couldn't be saved to Google Drive" in job["message"]
            assert ("Ana on Front Desk PC", "Scan not saved", False) in p.activity()
            assert s.files() == []
            await p.call("POST", "/api/scans/job/save", ana)
            assert (await until(p, ana, done))["name"] == "INV-Ana.pdf"
            assert s.files() == [("Invoices", "INV-Ana.pdf")]
            # The scanner is turned off.
            p.db.update_settings({"scan_address": "http://127.0.0.1:9"})
            await p.call("POST", f"/api/scans/{invoices}/scan", dee)
            job = await until(p, dee, lambda j: j["state"] == "failed")
            assert "isn't answering" in job["message"] and main.scanner_user[0] is None
            # Pages left unsaved are forgotten after a while.
            main.scan_jobs[p.dee]["touched"] -= main.SCAN_KEEP_SECONDS + 1
            assert (await p.call("GET", "/api/scans/job", dee))[1]["job"] is None
    asyncio.run(scenario())


def test_nothing_scans_until_it_is_set_up(world):
    p = world
    invoices = option(p)

    async def scenario():
        async with Set(p, connect=False), p:
            ana = await p.sign_in(p.ana)
            assert (await p.call("GET", "/api/scans", ana))[1]["problem"] == main.SCANS_NOT_SET_UP
            status, reply = await p.call("POST", f"/api/scans/{invoices}/scan", ana)
            assert status == 409 and reply["error"] == main.SCANS_NOT_SET_UP
        # A scanner on a kiosk nobody signed in to: no person, no scans.
        async with p:
            device = p.db.create_device("Pool phone", p.front_desk, None)
            _, token = p.db.claim_enroll_code(p.db.device(device)["enroll_code"])
            status, reply = await p.call("GET", "/api/scans", {"Authorization": f"Bearer {token}"})
            assert status == 403 and reply["error"] == main.NO_SCAN_PERSON
    asyncio.run(scenario())


# ---------------------------------------------------------------- the dashboard

def test_the_dashboard_sets_up_the_scanner_the_drive_and_the_options(world):
    p = world

    async def scenario():
        async with Set(p, feeder=1, connect=False) as s, p:
            origin = str(s.server.make_url("")).rstrip("/")
            status, view = await p.admin_call("GET", "/api/scans")
            assert status == 200 and view["options"] == [] and view["address"] == ""
            assert view["google"]["set_up"] is False and "name" in view["pieces"]
            # The scanner: an address that isn't one is refused; a good one is tested.
            assert (await p.admin_call("PUT", "/api/scans/settings", {"address": "http://x/y/z"}))[0] == 400
            assert (await p.admin_call("POST", "/api/scans/test"))[0] == 400
            status, view = await p.admin_call("PUT", "/api/scans/settings", {"address": s.scanner.address})
            assert status == 200 and view["address"] == s.scanner.address
            status, test = await p.admin_call("POST", "/api/scans/test")
            assert test == {"ok": True, "model": "Brother MFC-J4345DW", "feeder": True, "glass": True,
                            "state": "Idle", "paper_in_feeder": True, "feeder_says": "ScannerAdfLoaded"}
            assert (await p.admin_call("POST", "/api/scans/test"))[0] == 429
            assert s.scanner.jobs == []          # a test scans nothing

            # The drive: Google must be connected, the script new enough, and not an HR drive.
            status, reply = await p.admin_call("PUT", "/api/scans/settings", {"drive": SCANS_DRIVE})
            assert status == 400 and "Connect Google Drive first" in reply["error"]
            p.db.update_settings({"hr_google_url": f"{origin}/macros/s/FAKE-bridge/exec", "hr_google_secret": SECRET,
                                  "hr_google_drive": HR_DRIVE, "hr_google_restricted_drive": RESTRICTED_DRIVE,
                                  "hr_google_status": json.dumps({"version": 4})})
            status, reply = await p.admin_call("PUT", "/api/scans/settings", {"drive": SCANS_DRIVE})
            assert status == 400 and "needs updating" in reply["error"]
            assert gdrive.drive_ids(p.db.settings()) == [HR_DRIVE, RESTRICTED_DRIVE]
            p.db.update_settings({"hr_google_status": json.dumps({"version": gdrive.SCANS_VERSION})})
            status, reply = await p.admin_call("PUT", "/api/scans/settings", {"drive": HR_DRIVE})
            assert status == 400 and "isn't one of the HR drives" in reply["error"]
            status, view = await p.admin_call("PUT", "/api/scans/settings", {"drive": SCANS_DRIVE})
            assert status == 200 and view["problem"] is None and view["google"]["drive"] == SCANS_DRIVE
            # The script was told it may use all three, the HR drives first.
            assert s.google.allowed == [HR_DRIVE, RESTRICTED_DRIVE, SCANS_DRIVE]

            # Options: added, shown with an example name, changed, removed.
            bad = await p.admin_call("POST", "/api/scans/options", {"label": "Invoices", "format": "{nope}"})
            assert bad[0] == 400 and "isn't one of the pieces" in bad[1]["error"]
            status, view = await p.admin_call("POST", "/api/scans/options", {
                "label": "Invoices", "prefix": "INV", "departments": [p.front, 999]})
            assert status == 200
            made = view["options"][0]
            assert (made["label"], made["folder"], made["format"], made["departments"], made["example"]) == (
                "Invoices", "Invoices", "{prefix}-{name}", [p.front], "INV-Ana Lopez.pdf")
            status, names = await p.admin_call("POST", "/api/scans/example", {"prefix": "R", "format": "{prefix}_{last}"})
            assert names == {"names": ["R_Lopez.pdf", "R_Lopez-2.pdf", "R_Lopez-3.pdf"]}
            status, view = await p.admin_call("PUT", f"/api/scans/options/{made['id']}", {
                "label": "Bills", "prefix": "B", "format": "{prefix}-{department}", "departments": [p.kitchen],
                "enabled": False})
            assert view["options"][0]["example"] == "B-Front Desk.pdf" and view["options"][0]["enabled"] is False
            assert (await p.admin_call("PUT", "/api/scans/options/999", {"label": "x", "prefix": "x"}))[0] == 404
            status, view = await p.admin_call("DELETE", f"/api/scans/options/{made['id']}")
            assert view["options"] == []
            log = [a[1] for a in p.activity()]
            for action in ("Changed the Scans settings", "Added scan option Invoices", "Changed scan option Bills",
                           "Removed scan option Bills"):
                assert action in log
    asyncio.run(scenario())


def test_options_travel_in_backups_and_older_backups_leave_them_alone(world):
    import transfer
    p = world
    option(p, "Invoices", "INV", [p.front, p.kitchen], folder="Bills")
    p.db.update_settings({"scan_address": "http://192.168.1.50", "scan_google_drive": SCANS_DRIVE})
    backup = json.loads(json.dumps(transfer.make_backup(p.db, main.branding, "test")))
    assert [o["label"] for o in backup["scans"]] == ["Invoices"]
    assert backup["settings"]["scan_address"] == "http://192.168.1.50"
    assert "scan_google_drive" not in backup["settings"]          # like the HR drives: picked again after a restore

    # Restored over an install that has since changed its options: the backup's come back, and
    # the record of files scanned (what names are counted from) stays.
    kept, _ = p.db.reserve_scan_name(1, "INV-Ana", p.ana, "Ana")
    p.db.finish_scan_file(kept, "INV-Ana.pdf", 1, 100, None)
    p.db.delete_scan_option(p.db.save_scan_option(None, main.scans.clean_option({"label": "Later", "prefix": "L"}, set())))
    p.db.update_settings({"scan_address": ""})
    restore = lambda b: p.db.replace_everything(transfer.clean_backup(
        b, main.clean_settings, p.db.settings(), main.branding.clean_logo, p.db.known_tokens()))
    restore(backup)
    back = p.db.scan_options()
    assert [(o["id"], o["label"], o["folder"], o["departments"]) for o in back] == [(1, "Invoices", "Bills", [p.front, p.kitchen])]
    assert p.db.settings()["scan_address"] == "http://192.168.1.50"
    assert p.db.reserve_scan_name(1, "inv-ana", p.ana, "Ana")[1] == 1       # still the second of that name
    # A backup from before Scans: the options here are left as they are.
    older = {k: v for k, v in backup.items() if k != "scans"}
    older["settings"] = {k: v for k, v in backup["settings"].items() if k != "scan_address"}
    restore(older)
    assert [o["label"] for o in p.db.scan_options()] == ["Invoices"]
    cleaned = transfer._scans({"scans": backup["scans"] + [
        {"id": 2, "label": "Bad", "format": "{nope}"}, {"id": 1, "label": "Twice", "prefix": "T"}, "junk"]},
        {p.front})
    assert [(o["id"], o["label"], o["departments"]) for o in cleaned] == [(1, "Invoices", [p.front])]
