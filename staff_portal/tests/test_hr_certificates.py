"""The HR portal's certificates (app/hr/certificates.py): types and who needs them, deadlines from
start dates, statuses, adding a card (the camera photo, HEIC refused, a back photo, a PDF), the
date read by the fake Google script and HR's check screen, confirm and reject, the boards and their
spreadsheet, waivers, reminders and their cadence, nothing copied to Connecteam, and who may do
what.

Google is tests/fake_bridge.py and Connecteam a fake aiohttp server; nothing here talks to Google,
Connecteam, TT Hotel or Home Assistant.
"""

import asyncio
import contextlib
import csv
import io
import json
import os
import sys
import time
from datetime import date, timedelta

import aiohttp
from aiohttp.test_utils import TestServer

from hr_helpers import CHANGE, HR, FakeConnecteam, ct_user

from hr import certificates, delivery, documents, gdrive, notify, signin  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_bridge import HR_DRIVE, FakeBridge  # noqa: E402

SECRET = "certs-secret-" + "x" * 30
# Ana and Bo work in the Pub (501), Cy at the Front Desk (503), Dee in the Kitchen (502). Max
# manages the Pub, Fay is a director, Hana an HR admin and Ivy another HR admin.
ANA = ct_user(301, "Ana", "Lee", code="4821", groups=[501], mobile="541-555-0301")
BO = ct_user(302, "Bo", "Diaz", code="7302", groups=[501], mobile="541-555-0302")
CY = ct_user(303, "Cy", "Park", code="6611", groups=[503], mobile="541-555-0303")
DEE = ct_user(304, "Dee", "Fox", code="5566", groups=[502], mobile="541-555-0304")
MAX = ct_user(305, "Max", "Ray", code="9900", groups=[501], mobile="541-555-0305")
FAY = ct_user(306, "Fay", "Ito", code="3344", groups=[502], mobile="541-555-0306")
HANA = ct_user(307, "Hana", "Ito", code="7788", groups=[502], mobile="541-555-0307")
IVY = ct_user(308, "Ivy", "Moss", code="2233", groups=[502], mobile="541-555-0308")
GROUPS = {"501": "Pub", "502": "Kitchen", "503": "Front Desk"}
PERMIT_TEXT = ("OREGON LIQUOR AND CANNABIS COMMISSION\nSERVICE PERMIT\nANA LEE\nPermit # 123456\n"
               "Issued 06/14/2024\nExpires 06/14/2029\n")


def run(coro):
    return asyncio.run(coro)


def jpeg(color=(200, 30, 30), size=(800, 500), exif=False) -> bytes:
    from PIL import Image
    img = Image.new("RGB", size, color)
    out = io.BytesIO()
    if exif:
        data = Image.Exif()
        data[0x010F] = "PhoneMaker"          # camera make: metadata that must go
        img.save(out, "JPEG", exif=data.tobytes())
    else:
        img.save(out, "JPEG")
    return out.getvalue()


def pdf(text: str) -> bytes:
    return b"%PDF-1.4\n%OCR-TEXT:" + text.encode() + b"\n%%EOF\n"


async def setup(hr: HR, confirm_groups=True) -> dict:
    """People, roles, start dates long ago (so nothing is "due"), and HR's department choices."""
    hr.svc.group_names = lambda: GROUPS
    await hr.add_people(ANA, BO, CY, DEE, MAX, FAY, HANA, IVY)
    hr.give("305", "manager", {"501": True})
    hr.give("306", "director")
    hr.give("307", "hr_admin")
    hr.give("308", "hr_admin")
    for pid in ("301", "302", "303", "304", "305", "306", "307", "308"):
        hr.hdb.update_person(pid, start_date="2020-01-01", start_estimated=0, start_confirmed=1)
    if confirm_groups:
        for key, groups in (("food_handler", ["501"]), ("olcc", ["501", "503"])):
            t = certificates.type_by_key(hr.hdb, key)
            certificates.update_type(hr.hdb, t["id"], {"groups": json.dumps(groups), "groups_confirmed": 1})
    sessions = {name: signin.new_session(hr.hdb, pid, "127.0.0.1", "test") for name, pid in (
        ("ana", "301"), ("bo", "302"), ("cy", "303"), ("dee", "304"), ("max", "305"), ("fay", "306"),
        ("hana", "307"), ("ivy", "308"))}
    # Signing in counts as typing a code: start without, so sensitive actions really ask.
    hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = 0")
    hr.hdb.conn.commit()
    return sessions


def fresh(hr: HR) -> None:
    """Every session counts as having just typed its code (sensitive actions)."""
    hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = ?", (time.time(),))
    hr.hdb.conn.commit()


def put_card(hr: HR, pid: str, key: str, expires: str | None, status="confirmed", **extra) -> int:
    cur = hr.hdb.conn.execute(
        "INSERT INTO certificates (person_id, type_key, status, mime, size, upload_id, added_at, "
        "read_state, expires, stored, temporary, under_21) VALUES (?, ?, ?, 'image/jpeg', 1, ?, ?, "
        "'done', ?, 0, ?, ?)", (pid, key, status, f"test-{time.time_ns()}", time.time(), expires,
                                extra.get("temporary", 0), extra.get("under_21", 0)))
    hr.hdb.conn.commit()
    return cur.lastrowid


def by_type(statuses: list[dict]) -> dict:
    return {s["type_key"]: s for s in statuses}


async def add(hr: HR, cookie: str, pid: str, key: str, front: bytes, back: bytes | None = None,
              front_type="image/jpeg", name="card.jpg"):
    form = aiohttp.FormData()
    form.add_field("type", key)
    form.add_field("front", front, filename=name, content_type=front_type)
    if back is not None:
        form.add_field("back", back, filename="back.jpg", content_type="image/jpeg")
    resp = await hr.client.request("POST", f"/api/people/{pid}/certificates", data=form,
                                   headers={**CHANGE, "Cookie": f"{signin.COOKIE}={cookie}"})
    return resp.status, await resp.json()


@contextlib.asynccontextmanager
async def google(hr: HR, monkeypatch):
    """The fake Google script, set up with the HR drive chosen."""
    fake = FakeBridge(SECRET)
    server = TestServer(fake.app())
    await server.start_server()
    origin = str(server.make_url("")).rstrip("/")
    monkeypatch.setattr(gdrive, "TEST_ORIGIN", origin)
    monkeypatch.setattr(gdrive, "RETRY_DELAYS", (0.01, 0.01))
    fake.allowed = [HR_DRIVE]
    seen = fake.sizes = []
    original = fake.read_text
    fake.read_text = lambda data: (seen.append(len(data)), original(data))[1]
    hr.db.update_settings({"hr_google_url": f"{origin}/macros/s/FAKE-bridge/exec",
                           "hr_google_secret": SECRET, "hr_google_drive": HR_DRIVE})
    try:
        yield fake
    finally:
        await gdrive.close()
        await server.close()


# ---------------------------------------------------------------- types and statuses

def test_types_start_suggested_and_nothing_counts_until_hr_confirms(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            sessions = await setup(hr, confirm_groups=False)
            keys = [t["key"] for t in certificates.types(hr.hdb)]
            assert keys == ["food_handler", "olcc", "food_manager"]
            olcc = certificates.type_by_key(hr.hdb, "olcc")
            assert (olcc["lasts_years"], olcc["due_days"], olcc["lapse_message"]) == (5, 7, "May not serve alcohol")
            assert certificates.type_by_key(hr.hdb, "food_handler")["accepts"] == ["food_manager"]
            # Suggested by name, but nobody needs anything until HR saves the departments.
            assert certificates.status_of(hr.hdb, "301") == []
            status, home, _ = await hr.call("GET", "/api/home", cookie=sessions["hana"])
            assert any("Confirm which departments need the Oregon Food Handler Card and the OLCC "
                       "Service Permit" in t["text"] for t in home["todos"])
            status, s, _ = await hr.call("GET", "/api/settings", cookie=sessions["hana"])
            shown = {t["key"]: t for t in s["sections"]["certificates"]["types"]}
            assert shown["olcc"]["suggested"] == ["501", "503"] and shown["food_handler"]["suggested"] == ["501"]
            assert shown["food_manager"]["groups_confirmed"] == 1
            # HR confirms (a sensitive change: a fresh code or PIN first).
            path = f"/api/settings/certificates/types/{olcc['id']}"
            status, data, _ = await hr.call("PUT", path, {"groups": ["501", "503"]}, sessions["hana"])
            assert (status, data["error"]) == (401, "reconfirm")
            await hr.confirm(sessions["hana"], "307")
            status, _, _ = await hr.call("PUT", path, {"groups": ["501", "503"]}, sessions["hana"])
            assert status == 200
            assert certificates.type_by_key(hr.hdb, "olcc")["groups_confirmed"] == 1
            assert [s["type_key"] for s in certificates.status_of(hr.hdb, "303")] == ["olcc"]
            assert certificates.status_of(hr.hdb, "304") == []          # the kitchen needs nothing
            # Managers and employees can't change settings.
            for who in ("max", "ana"):
                status, _, _ = await hr.call("PUT", path, {"groups": []}, sessions[who])
                assert status == 403
    run(scenario())


def test_deadlines_from_start_dates_and_every_status(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db, on = hr.hdb, date(2026, 10, 26)
            # A new hire who started on the 20th: OLCC within 7 days (flagged until it's valid),
            # the Food Handler Card within 30 days (just due, no flag).
            db.update_person("301", start_date="2026-10-20")
            s = by_type(certificates.status_of(db, "301", on))
            assert (s["olcc"]["status"], s["olcc"]["due"], s["olcc"]["flag"]) == ("due", "2026-10-27", "May not serve alcohol")
            assert (s["food_handler"]["status"], s["food_handler"]["due"], s["food_handler"]["flag"]) == ("due", "2026-11-19", None)
            # The 7 days pass: missing.
            s = by_type(certificates.status_of(db, "301", date(2026, 10, 28)))
            assert (s["olcc"]["status"], s["olcc"]["flag"]) == ("missing", "May not serve alcohol")
            assert s["food_handler"]["status"] == "due"
            s = by_type(certificates.status_of(db, "301", date(2026, 11, 20)))
            assert (s["food_handler"]["status"], s["food_handler"]["flag"]) == ("missing", "May not handle food")
            # No start date at all: missing.
            db.update_person("302", start_date=None)
            assert by_type(certificates.status_of(db, "302", on))["olcc"]["status"] == "missing"
            # Cards: valid, expiring inside the first heads-up (6 months for OLCC), expired.
            put_card(hr, "303", "olcc", "2029-06-14", temporary=1)
            s = by_type(certificates.status_of(db, "303", on))["olcc"]
            assert (s["status"], s["expires"], s["flag"], s["temporary"]) == ("valid", "2029-06-14", None, True)
            assert by_type(certificates.status_of(db, "303", date(2029, 1, 1)))["olcc"]["status"] == "expiring"
            assert by_type(certificates.status_of(db, "303", date(2029, 6, 14)))["olcc"]["status"] == "expiring"
            s = by_type(certificates.status_of(db, "303", date(2029, 6, 15)))["olcc"]
            assert (s["status"], s["flag"]) == ("expired", "May not serve alcohol")
            # A new card waiting for HR: "waiting", still flagged until it's confirmed.
            put_card(hr, "303", "olcc", None, status="waiting")
            s = by_type(certificates.status_of(db, "303", date(2029, 6, 15)))["olcc"]
            assert (s["status"], s["flag"]) == ("waiting", "May not serve alcohol") and s["waiting_id"]
            # ...but a valid card stays valid while its renewal waits.
            assert by_type(certificates.status_of(db, "303", on))["olcc"]["status"] == "valid"
            # A food manager certificate is accepted in place of a Food Handler Card.
            put_card(hr, "304", "food_manager", "2030-01-01")
            assert [s["status"] for s in certificates.status_of(db, "304", on)] == ["valid"]   # not needed: held
            db.update_person("304", groups='["501"]')
            s = by_type(certificates.status_of(db, "304", on))
            assert (s["food_handler"]["status"], s["food_handler"]["via"]) == ("valid", "food_manager")
            # Named on a type: needed whatever the department.
            olcc = certificates.type_by_key(db, "olcc")
            certificates.update_type(db, olcc["id"], {"people": json.dumps(["306"])})
            assert by_type(certificates.status_of(db, "306", on))["olcc"]["status"] == "missing"
            # A type no longer tracked: nobody needs it.
            certificates.update_type(db, olcc["id"], {"active": 0})
            assert "olcc" not in by_type(certificates.status_of(db, "306", on))
    run(scenario())


def test_waivers_need_a_reason_an_end_and_a_fresh_code(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            sessions = await setup(hr)
            on = certificates.today(hr.hdb)
            ends = (on + timedelta(days=90)).isoformat()
            body = {"type": "olcc", "reason": "Doesn't serve alcohol this season", "ends": ends}
            status, data, _ = await hr.call("POST", "/api/people/302/certificates/waivers", body, sessions["hana"])
            assert (status, data["error"]) == (401, "reconfirm")
            fresh(hr)
            for bad in ({**body, "reason": ""}, {**body, "ends": ""}, {**body, "ends": "2001-01-01"},
                        {**body, "type": "nope"}):
                assert (await hr.call("POST", "/api/people/302/certificates/waivers", bad, sessions["hana"]))[0] == 400
            for who in ("max", "fay", "bo"):
                assert (await hr.call("POST", "/api/people/302/certificates/waivers", body, sessions[who]))[0] in (403, 404)
            status, _, _ = await hr.call("POST", "/api/people/302/certificates/waivers", body, sessions["hana"])
            assert status == 200
            s = by_type(certificates.status_of(hr.hdb, "302"))["olcc"]
            assert (s["status"], s["flag"], s["waived_until"]) == ("waived", None, ends)
            # The reason is HR's: the employee and the manager see only that it's waived.
            _, mine, _ = await hr.call("GET", "/api/people/302/certificates", cookie=sessions["bo"])
            assert mine["waivers"] == [{"id": 1, "type": "OLCC Service Permit", "ends": ends}]
            _, team, _ = await hr.call("GET", "/api/people/302/certificates", cookie=sessions["max"])
            assert "season" not in json.dumps(team)
            _, theirs, _ = await hr.call("GET", "/api/people/302/certificates", cookie=sessions["hana"])
            assert theirs["waivers"][0]["reason"] == "Doesn't serve alcohol this season"
            assert "season" not in str(hr.hdb.conn.execute("SELECT reason FROM cert_waivers").fetchone()[0])
            # It ends by itself after its end date, or when HR ends it.
            assert by_type(certificates.status_of(hr.hdb, "302", on + timedelta(days=91)))["olcc"]["status"] == "missing"
            status, _, _ = await hr.call("DELETE", "/api/people/302/certificates/waivers/1", cookie=sessions["hana"])
            assert status == 200 and by_type(certificates.status_of(hr.hdb, "302"))["olcc"]["status"] == "missing"
            # HR can't waive their own.
            body["type"] = "food_handler"
            assert (await hr.call("POST", "/api/people/307/certificates/waivers", body, sessions["hana"]))[0] == 403
    run(scenario())


# ---------------------------------------------------------------- adding a card, reading it, HR's check

def test_employee_adds_a_card_google_reads_it_hr_corrects_and_confirms(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            sessions = await setup(hr)
            old = put_card(hr, "301", "olcc", "2026-10-01")
            fake.ocr_queue.append(PERMIT_TEXT)
            big = jpeg(size=(4000, 3000), exif=True)
            status, data = await add(hr, sessions["ana"], "301", "olcc", big)
            assert status == 200, data
            card = data["card"]
            assert card["status"] == "waiting" and card["read_state"] == "pending"
            await certificates.settle(hr.svc)
            # "We read: expires 6/14/2029. HR will confirm."
            status, data, _ = await hr.call("GET", f"/api/people/301/certificates/{card['id']}/reading",
                                            cookie=sessions["ana"])
            assert data["card"]["read_expires"] == "2029-06-14" and data["card"]["read_how"] == "read"
            assert fake.sizes and max(fake.sizes) <= certificates.READ_LIMIT    # a reduced copy
            # Kept encrypted here: the photo and the text read.
            row = certificates.get(hr.hdb, card["id"])
            assert "SERVICE PERMIT" not in str(row["read_text"])
            on_disk = open(os.path.join(certificates.store_dir(hr.svc.folder), f"{row['upload_id']}.bin"), "rb").read()
            assert on_disk[:3] != b"\xff\xd8\xff"
            # HR is told, and sees it on the list (not Ana's own manager, who can't check it).
            assert any(i["kind"] == "cert_uploaded" for i in notify.inbox(hr.hdb, "307"))
            _, waiting, _ = await hr.call("GET", "/api/certificates/waiting", cookie=sessions["hana"])
            assert [c["id"] for c in waiting["cards"]] == [card["id"]]
            path = f"/api/people/301/certificates/{card['id']}"
            assert (await hr.call("GET", path, cookie=sessions["max"]))[0] == 403
            status, check, _ = await hr.call("GET", path, cookie=sessions["hana"])
            assert status == 200
            assert check["suggest"]["expires"] == "2029-06-14" and check["suggest"]["issued"] == "2024-06-14"
            marked = [s["t"] for s in check["segments"] if s.get("d")]
            assert marked == ["06/14/2024", "06/14/2029"]
            assert check["current"]["expires"] == "2026-10-01"
            # The photo: Ana and HR only, without its metadata; HR can turn it.
            status, _, resp = await hr.call("GET", f"{path}/photo", cookie=sessions["max"])
            assert status == 404
            resp = await hr.client.get(f"{path}/photo?turn=90", headers={
                "Host": "hr.example.com", "Cookie": f"{signin.COOKIE}={sessions['hana']}"})
            photo = await resp.read()
            assert resp.status == 200 and resp.headers["Content-Type"] == "image/jpeg"
            assert "sandbox" in resp.headers["Content-Security-Policy"] and b"PhoneMaker" not in photo
            from PIL import Image
            assert Image.open(io.BytesIO(photo)).height > Image.open(io.BytesIO(photo)).width   # turned
            # HR corrects the date, ticks "Checked with OLCC", and confirms.
            status, data, _ = await hr.call("POST", f"{path}/confirm", {
                "expires": "2029-06-13", "issued": "2024-06-14", "number": "123456",
                "under_21": True, "checked_issuer": True}, sessions["hana"])
            assert status == 200, data
            row = certificates.get(hr.hdb, card["id"])
            assert (row["status"], row["expires"], row["corrected"], row["under_21"], row["checked_issuer"]) == \
                ("confirmed", "2029-06-13", 1, 1, 1)
            assert row["read_text"] is None and row["stored"] == 0
            assert certificates.get(hr.hdb, old)["status"] == "replaced"
            assert "123456" not in str(row["number"])
            s = by_type(certificates.status_of(hr.hdb, "301"))["olcc"]
            assert (s["status"], s["under_21"]) == ("valid", True)
            assert any("confirmed" in i["title"] for i in notify.inbox(hr.hdb, "301"))
            # It's a document now, on its way to Google Drive with the expiry in its name.
            doc = documents.get(hr.hdb, row["document_id"])
            assert doc["status"] == "active" and documents.title_of(doc).endswith("expires 2029-06-13")
            assert documents.title_of(doc) == "OLCC Service Permit - expires 2029-06-13"
            assert [d["target"] for d in documents.deliveries_of(hr.hdb, doc["id"])] == ["drive"]
            await delivery.run_due(hr.svc)
            await delivery.settle(hr.svc)
            names = [f["name"] for f in fake.files.values() if f["data"]]
            assert names == [f"{certificates.today(hr.hdb).isoformat()} OLCC Service Permit - expires 2029-06-13.jpg"]
            # Deciding twice doesn't work.
            assert (await hr.call("POST", f"{path}/confirm", {"expires": "2029-06-13"}, sessions["hana"]))[0] == 409
            # Ana sees it, with its number and photo; her manager sees the dates only.
            _, mine, _ = await hr.call("GET", "/api/people/301/certificates", cookie=sessions["ana"])
            confirmed = [c for c in mine["cards"] if c["id"] == card["id"]][0]
            assert confirmed["number"] == "123456" and confirmed["photo"] == documents.view_url(doc)
            # The photo now opens as the document it became (its own copy, then Drive's).
            ana = {"Host": "hr.example.com", "Cookie": f"{signin.COOKIE}={sessions['ana']}"}
            resp = await hr.client.get(confirmed["photo"], headers=ana)
            assert resp.status == 200 and (await resp.read())[:3] == b"\xff\xd8\xff"
            assert (await hr.client.get(f"{path}/photo", headers=ana)).status == 404
            max_ = {"Host": "hr.example.com", "Cookie": f"{signin.COOKIE}={sessions['max']}"}
            assert (await hr.client.get(confirmed["photo"], headers=max_)).status == 404
            _, team, _ = await hr.call("GET", "/api/people/301/certificates", cookie=sessions["max"])
            confirmed = [c for c in team["cards"] if c["id"] == card["id"]][0]
            assert "number" not in confirmed and "photo" not in confirmed and confirmed["expires"] == "2029-06-13"
    run(scenario())


def test_rejected_cards_never_reach_the_folder_and_say_why(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            sessions = await setup(hr)
            fake.ocr_default = "blurry"
            # Max adds Bo's card from his phone (Bo has no smartphone).
            status, data = await add(hr, sessions["max"], "302", "food_handler", jpeg())
            assert status == 200
            card = data["card"]
            await certificates.settle(hr.svc)
            assert certificates.get(hr.hdb, card["id"])["read_how"] == "not_found"
            path = f"/api/people/302/certificates/{card['id']}"
            assert (await hr.call("POST", f"{path}/reject", {"reason": ""}, sessions["hana"]))[0] == 400
            assert (await hr.call("POST", f"{path}/reject", {"reason": "Blurry"}, sessions["max"]))[0] == 403
            status, _, _ = await hr.call("POST", f"{path}/reject", {"reason": "The photo is blurry. Retake it."},
                                         sessions["hana"])
            assert status == 200
            row = certificates.get(hr.hdb, card["id"])
            assert row["status"] == "rejected" and row["document_id"] is None
            assert hr.hdb.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
            assert any(i["kind"] == "cert_rejected" for i in notify.inbox(hr.hdb, "302"))
            assert any(i["kind"] == "cert_rejected" for i in notify.inbox(hr.hdb, "305"))
            _, mine, _ = await hr.call("GET", "/api/people/302/certificates", cookie=sessions["bo"])
            assert mine["cards"][0]["reason"] == "The photo is blurry. Retake it."
            assert "blurry" not in str(row["reason"])
            # The photo goes after 30 days.
            counts = certificates.tidy(hr.svc, time.time() + 31 * 86400)
            assert counts["photos"] == 1 and certificates.get(hr.hdb, card["id"])["stored"] == 0
            assert not os.listdir(certificates.store_dir(hr.svc.folder))
    run(scenario())


def test_heic_pdfs_back_photos_and_a_newer_photo(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            sessions = await setup(hr)
            heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
            status, data = await add(hr, sessions["ana"], "301", "olcc", heic, front_type="image/heic", name="IMG.HEIC")
            assert status == 400 and "Take the photo again" in data["error"]
            status, data = await add(hr, sessions["ana"], "301", "olcc", b"MZ not a picture")
            assert status == 400
            status, data = await add(hr, sessions["ana"], "301", "nope", jpeg())
            assert status == 400 and data["error"] == "Choose which card it is."
            # The online course's PDF: kept byte for byte and read as it is.
            course = pdf("Oregon Food Handler Card\nAna Lee\nIssued 03/02/2026\n")
            status, data = await add(hr, sessions["ana"], "301", "food_handler", course,
                                     front_type="application/pdf", name="card.pdf")
            assert status == 200 and data["card"]["status"] == "waiting"
            await certificates.settle(hr.svc)
            row = certificates.get(hr.hdb, data["card"]["id"])
            assert (row["mime"], row["read_expires"], row["read_how"]) == ("application/pdf", "2029-03-02", "calculated")
            # A PDF can't have a back photo.
            status, data = await add(hr, sessions["ana"], "301", "olcc", course, back=jpeg(),
                                     front_type="application/pdf", name="card.pdf")
            assert status == 400 and "PDF" in data["error"]
            # Front and back become one picture, the back under the front.
            fake.ocr_queue.append(PERMIT_TEXT)
            status, data = await add(hr, sessions["ana"], "301", "olcc", jpeg(size=(800, 500)),
                                     back=jpeg((20, 20, 200), size=(800, 500)))
            assert status == 200
            first = data["card"]["id"]
            await certificates.settle(hr.svc)
            from PIL import Image
            stored = await certificates._load(hr.svc.folder, certificates.get(hr.hdb, first))
            img = Image.open(io.BytesIO(stored))
            assert img.height > 1000 and img.width == 800
            # A newer photo of the same card replaces one still waiting: the newest counts.
            status, data = await add(hr, sessions["ana"], "301", "olcc", jpeg((0, 200, 0)))
            assert certificates.get(hr.hdb, first)["status"] == "withdrawn"
            assert certificates.get(hr.hdb, first)["stored"] == 0
            assert data["card"]["status"] == "waiting"
    run(scenario())


def test_a_legal_hold_keeps_every_card_photo(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch):
            sessions = await setup(hr)
            documents.set_hold(hr.hdb, "301", True, "OLCC permit dispute", "305", "Hana")
            status, data = await add(hr, sessions["ana"], "301", "olcc", jpeg())
            first = data["card"]["id"]
            # A newer photo replaces the waiting one, but the older photo is kept.
            status, data = await add(hr, sessions["ana"], "301", "olcc", jpeg((0, 200, 0)))
            await certificates.settle(hr.svc)
            older = certificates.get(hr.hdb, first)
            assert (older["status"], older["stored"]) == ("withdrawn", 1)
            # Rejected: kept past 30 days while the hold is on...
            path = f"/api/people/301/certificates/{data['card']['id']}"
            assert (await hr.call("POST", f"{path}/reject", {"reason": "Blurry."}, sessions["hana"]))[0] == 200
            later = time.time() + 31 * 86400
            assert certificates.tidy(hr.svc, later)["photos"] == 0
            assert certificates.get(hr.hdb, data["card"]["id"])["stored"] == 1
            # ...and tidied once it's lifted.
            documents.set_hold(hr.hdb, "301", False, "", "305", "Hana")
            assert certificates.tidy(hr.svc, later)["photos"] == 2
    run(scenario())


def test_google_busy_is_tried_again_then_hr_types_the_date(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            sessions = await setup(hr)
            fake.fail_next(status=503, op="ocr", times=3)      # every try of the first read
            status, data = await add(hr, sessions["cy"], "303", "olcc", jpeg())
            await certificates.settle(hr.svc)
            row = certificates.get(hr.hdb, data["card"]["id"])
            assert (row["read_state"], row["read_tries"]) == ("pending", 1) and "busy" in row["read_note"]
            # Half a minute later nothing is due yet; five minutes later it's read.
            await certificates.tick(hr.svc, time.time() + 30)
            await certificates.settle(hr.svc)
            assert certificates.get(hr.hdb, row["id"])["read_tries"] == 1
            fake.ocr_queue.append(PERMIT_TEXT)
            hr.hdb.conn.execute("UPDATE certificates SET read_due = 0")
            hr.hdb.conn.commit()
            await certificates.tick(hr.svc, time.time())
            await certificates.settle(hr.svc)
            assert certificates.get(hr.hdb, row["id"])["read_expires"] == "2029-06-14"
            # Google refusing for good: HR types the date.
            fake.fail_next(code="not_allowed", message="Not in the HR drives.", op="ocr")
            status, data = await add(hr, sessions["bo"], "302", "olcc", jpeg((1, 2, 3)))
            await certificates.settle(hr.svc)
            row = certificates.get(hr.hdb, data["card"]["id"])
            assert row["read_state"] == "failed" and "Type the date" in row["read_note"]
    run(scenario())


def test_without_google_hr_types_the_date(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            sessions = await setup(hr)
            status, data = await add(hr, sessions["cy"], "303", "olcc", jpeg())
            assert status == 200
            card = data["card"]
            assert card["read_state"] == "skipped" and "isn't set up" in card["read_note"]
            path = f"/api/people/303/certificates/{card['id']}"
            status, check, _ = await hr.call("GET", path, cookie=sessions["hana"])
            assert check["segments"] == [] and check["suggest"]["expires"] is None
            for bad in ({}, {"expires": "06/14/2029"}, {"expires": "2090-01-01"},
                        {"expires": "2029-06-14", "issued": "2030-01-01"},
                        {"expires": "2029-06-14", "number": "<b>1</b>"}):
                status, data, _ = await hr.call("POST", f"{path}/confirm", bad, sessions["hana"])
                assert status == 400, bad
            status, data, _ = await hr.call("POST", f"{path}/confirm", {"expires": "2029-06-14", "temporary": True},
                                            sessions["hana"])
            assert status == 200
            row = certificates.get(hr.hdb, card["id"])
            assert (row["expires"], row["temporary"], row["corrected"]) == ("2029-06-14", 1, 0)
            # Without Google, the file waits here for Drive (encrypted).
            doc = documents.get(hr.hdb, row["document_id"])
            await delivery.run_due(hr.svc)
            assert documents.deliveries_of(hr.hdb, doc["id"])[0]["code"] == "not_set_up"
            # An HR admin's own card waits for another HR admin.
            status, data = await add(hr, sessions["hana"], "307", "olcc", jpeg())
            assert status == 200
            own = data["card"]["id"]
            _, waiting, _ = await hr.call("GET", "/api/certificates/waiting", cookie=sessions["hana"])
            assert own not in [c["id"] for c in waiting["cards"]]
            for method, tail in (("GET", ""), ("POST", "/confirm"), ("POST", "/reject")):
                status, _, _ = await hr.call(method, f"/api/people/307/certificates/{own}{tail}",
                                             {"expires": "2029-06-14", "reason": "fine"} if method == "POST" else None,
                                             sessions["hana"])
                assert status in (403, 404)
            _, waiting, _ = await hr.call("GET", "/api/certificates/waiting", cookie=sessions["ivy"])
            assert own in [c["id"] for c in waiting["cards"]]
            # A migration waits while a card's photo is only here.
            assert "waiting for HR to confirm" in hr.svc.migration_blocker()
            status, _, _ = await hr.call("POST", f"/api/people/307/certificates/{own}/confirm",
                                         {"expires": "2029-06-14"}, sessions["ivy"])
            assert status == 200 and certificates.migration_blocker(hr.svc) is None
    run(scenario())


def test_who_may_add_see_and_check(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            sessions = await setup(hr)
            # Ana herself, Max for his Pub team, Fay (director), HR: yes. Bo for Ana, Max for
            # the kitchen: no (they can't see that file at all).
            for who, pid, ok in (("ana", "301", True), ("max", "301", True), ("fay", "301", True),
                                 ("hana", "301", True), ("bo", "301", False), ("max", "304", False)):
                status, _ = await add(hr, sessions[who], pid, "olcc", jpeg())
                assert (status == 200) == ok, (who, pid, status)
            # Too many in a day.
            for _ in range(certificates.ADDS_A_DAY):
                await add(hr, sessions["dee"], "304", "food_manager", jpeg())
            status, data = await add(hr, sessions["dee"], "304", "food_manager", jpeg())
            assert status == 429
            # The person's page: a section when they need a card; managers see no photos.
            _, page, _ = await hr.call("GET", "/api/people/301", cookie=sessions["max"])
            section = [s for s in page["sections"] if s["name"] == "certificates"][0]
            assert section["certs"]["can_add"] and not section["certs"]["can_check"]
            assert all("photo" not in c for c in section["certs"]["cards"])
            _, page, _ = await hr.call("GET", "/api/people/301", cookie=sessions["hana"])
            section = [s for s in page["sections"] if s["name"] == "certificates"][0]
            assert section["certs"]["can_check"] and all(c.get("photo") for c in section["certs"]["cards"])
            # Someone who needs nothing and has nothing gets no section.
            _, page, _ = await hr.call("GET", "/api/people/308", cookie=sessions["hana"])
            assert "certificates" not in [s["name"] for s in page["sections"]]
            # The boards: staff only.
            assert (await hr.call("GET", "/api/certificates/board", cookie=sessions["ana"]))[0] == 403
            assert (await hr.call("GET", "/api/certificates/waiting", cookie=sessions["max"]))[0] == 403
            assert (await hr.call("POST", "/api/certificates/export", {}, sessions["max"]))[0] == 403
    run(scenario())


# ---------------------------------------------------------------- boards

def test_boards_banners_filters_and_the_spreadsheet(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            sessions = await setup(hr)
            on = certificates.today(hr.hdb)
            soon = (on + timedelta(days=20)).isoformat()
            put_card(hr, "301", "olcc", (on - timedelta(days=3)).isoformat())          # Ana: expired
            put_card(hr, "301", "food_handler", soon)                                  # ...expiring
            put_card(hr, "303", "olcc", (on + timedelta(days=900)).isoformat())        # Cy: valid
            hr.hdb.update_person("302", name="=HYPERLINK(1)")                          # Bo: missing both
            status, b, _ = await hr.call("GET", "/api/certificates/board", cookie=sessions["hana"])
            assert status == 200 and b["hr"]
            assert b["banners"] == ["1 OLCC Service Permit expired: may not serve alcohol",
                                    "2 Oregon Food Handler Cards missing: may not handle food",
                                    "2 OLCC Service Permits missing: may not serve alcohol"]
            rows = {(r["name"], r["type_key"]): r for r in b["rows"]}
            assert rows[("Ana Lee", "olcc")]["status"] == "expired"
            assert rows[("Ana Lee", "food_handler")]["status"] == "expiring"
            assert rows[("Cy Park", "olcc")]["departments"] == ["Front Desk"]
            assert b["rows"][0]["status"] == "expired"                                 # worst first
            # Filters.
            _, f, _ = await hr.call("GET", "/api/certificates/board?within=30", cookie=sessions["hana"])
            assert [(r["name"], r["type_key"]) for r in f["rows"]] == [("Ana Lee", "food_handler")]
            _, f, _ = await hr.call("GET", "/api/certificates/board?group=503", cookie=sessions["hana"])
            assert {r["name"] for r in f["rows"]} == {"Cy Park"}
            _, f, _ = await hr.call("GET", "/api/certificates/board?status=problems&type=olcc", cookie=sessions["hana"])
            assert {r["name"] for r in f["rows"]} == {"Ana Lee", "=HYPERLINK(1)", "Max Ray"}
            # Max's team board: the Pub only, with its banners.
            _, team, _ = await hr.call("GET", "/api/certificates/board", cookie=sessions["max"])
            assert {r["name"] for r in team["rows"]} == {"Ana Lee", "=HYPERLINK(1)", "Max Ray"}
            assert "1 OLCC Service Permit expired: may not serve alcohol" in team["banners"]
            assert not team["hr"] and "waiting" not in team
            _, home, _ = await hr.call("GET", "/api/home", cookie=sessions["max"])
            assert any("on your team" in t["text"] for t in home["todos"])
            # The spreadsheet: HR only, with a fresh code, formulas made plain text.
            status, data, _ = await hr.call("POST", "/api/certificates/export", {"type": "olcc"}, sessions["hana"])
            assert (status, data["error"]) == (401, "reconfirm")
            fresh(hr)
            status, data, _ = await hr.call("POST", "/api/certificates/export", {"type": "olcc"}, sessions["hana"])
            assert status == 200 and data["name"].endswith(".csv")
            table = list(csv.reader(io.StringIO(data["csv"])))
            assert table[0][:4] == ["Name", "Departments", "Certificate", "Status"]
            assert {r[0] for r in table[1:]} == {"Ana Lee", "'=HYPERLINK(1)", "Max Ray", "Cy Park"}
            assert all(r[2] == "OLCC Service Permit" for r in table[1:])
            lines = [line["action"] for line in hr.hdb.conn.execute("SELECT action FROM audit")]
            assert "Downloaded the certificate board" in lines and "Viewed the certificate board" in lines
    run(scenario())


# ---------------------------------------------------------------- reminders

def test_reminders_cadence_heads_ups_and_running_twice(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db = hr.hdb
            on = date(2026, 10, 26)
            put_card(hr, "301", "olcc", "2026-10-20")                         # Ana: expired
            put_card(hr, "301", "food_handler", "2026-11-20")                 # ...25 days left
            put_card(hr, "303", "olcc", "2027-03-01")                         # Cy: 126 days (OLCC's 182)
            db.update_person("304", groups='["501"]', start_date="2026-10-25")   # Dee: a new hire

            def inbox(pid, kind=None):
                return [i for i in notify.inbox(db, pid, 200) if kind is None or i["kind"] == kind]

            counts = certificates.reminders(db, on)
            assert counts["lapsed"] == 5 and counts["due"] == 2      # Ana's OLCC, Bo's and Max's two; Dee's two
            lapse = inbox("301", "cert_lapsed")
            assert len(lapse) == 1 and lapse[0]["urgent"] == 1 and "expired on 10/20/2026" in lapse[0]["title"]
            ahead = inbox("301", "cert_expiring")
            assert len(ahead) == 1 and "runs out on 11/20/2026" in ahead[0]["title"]
            assert len(inbox("303", "cert_expiring")) == 1                # the 6-month heads-up
            assert {i["title"] for i in inbox("304", "cert_due")} == {
                "You need a OLCC Service Permit by 11/1/2026. Add it in the HR portal.",
                "You need a Oregon Food Handler Card by 11/24/2026. Add it in the HR portal."}
            # Max (the Pub's manager) and both HR admins get one message each.
            team = inbox("305", "cert_team")
            assert len(team) == 1 and team[0]["title"].startswith("3 certificates are expired or missing")
            assert inbox("307", "cert_team")[0]["title"].startswith("5 certificates")
            assert len(inbox("308", "cert_team")) == 1
            assert inbox("306", "cert_team") == []                         # directors aren't "their managers"
            before = db.conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
            # Running it again the same day sends nothing; nor does the next day (every 2 days).
            assert certificates.reminders(db, on) == {"lapsed": 0, "ahead": 0, "due": 0}
            assert certificates.reminders(db, on + timedelta(days=1)) == {"lapsed": 0, "ahead": 0, "due": 0}
            assert db.conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == before
            again = certificates.reminders(db, on + timedelta(days=2))
            assert again["lapsed"] == 5 and len(inbox("301", "cert_lapsed")) == 2
            # HR changes it to every day.
            db.set_settings({"cert_remind_every": "1"})
            assert certificates.reminders(db, on + timedelta(days=3))["lapsed"] == 5
            # The next heads-up stage, once.
            assert certificates.reminders(db, date(2026, 11, 6))["ahead"] == 1          # 14 days left
            assert certificates.reminders(db, date(2026, 11, 7))["ahead"] == 0
            # A renewed card starts again: confirmed, the lapse reminders stop.
            db.conn.execute("UPDATE certificates SET status = 'replaced' WHERE person_id = '301' AND type_key = 'olcc'")
            put_card(hr, "301", "olcc", "2031-10-20")
            db.conn.commit()
            told = len(inbox("301", "cert_lapsed"))
            assert certificates.reminders(db, date(2026, 11, 8))["lapsed"] > 0
            assert len(inbox("301", "cert_lapsed")) == told                               # not Ana
            # The nightly run does it through daily_jobs.
            hr.svc.daily_state["day"] = "2000-01-01"
            await hr.svc.nightly()
            assert db.conn.execute("SELECT COUNT(*) FROM cert_reminders").fetchone()[0] > 0
    run(scenario())


def test_employees_get_every_heads_up_from_the_portal(tmp_path):
    """Nothing is copied to Connecteam, so Connecteam doesn't remind anyone: the portal sends
    every heads-up, the last ones too."""
    async def scenario():
        async with HR(tmp_path) as hr:
            await setup(hr)
            db = hr.hdb
            put_card(hr, "303", "olcc", "2026-11-20")
            db.conn.commit()
            assert certificates.reminders(db, date(2026, 10, 26))["ahead"] == 1      # 25 days left
            assert len([i for i in notify.inbox(db, "303") if i["kind"] == "cert_expiring"]) == 1
            assert len([i for i in notify.inbox(db, "307") if i["kind"] == "cert_expiring"]) == 1
    run(scenario())


# ---------------------------------------------------------------- Connecteam

def test_cards_stay_in_drive_and_connecteam_is_never_asked(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch), FakeConnecteam() as fake:
            sessions = await setup(hr)
            olcc = certificates.type_by_key(hr.hdb, "olcc")
            fresh(hr)
            # An old client's Connecteam document is ignored.
            status, _, _ = await hr.call("PUT", f"/api/settings/certificates/types/{olcc['id']}",
                                         {"ct_document": "52114", "lasts_years": "3"}, sessions["hana"])
            assert status == 200 and "ct_document" not in certificates.type_by_key(hr.hdb, "olcc")
            status, data = await add(hr, sessions["ana"], "301", "olcc", jpeg())
            await certificates.settle(hr.svc)
            path = f"/api/people/301/certificates/{data['card']['id']}/confirm"
            status, _, _ = await hr.call("POST", path, {"expires": "2029-06-14"}, sessions["hana"])
            assert status == 200
            await delivery.run_due(hr.svc)
            await delivery.settle(hr.svc)
            doc = documents.get(hr.hdb, certificates.get(hr.hdb, data["card"]["id"])["document_id"])
            assert [d["target"] for d in documents.deliveries_of(hr.hdb, doc["id"])] == ["drive"]
            assert doc["drive_id"] and "ct_document" not in doc
            await certificates.nightly(hr.svc)
            await certificates.tick(hr.svc, time.time())
            assert fake.calls == []
            # The settings and the routes for Connecteam's documents are gone.
            status, s, _ = await hr.call("GET", "/api/settings", cookie=sessions["hana"])
            assert "connecteam" not in s["sections"]["certificates"]
            assert "ct_document" not in s["sections"]["certificates"]["types"][0]
            status, board, _ = await hr.call("GET", "/api/certificates/board", cookie=sessions["hana"])
            assert status == 200 and "connecteam" not in board
            assert (await hr.call("GET", "/api/certificates/connecteam", cookie=sessions["hana"]))[0] in (404, 405)
            assert (await hr.call("POST", "/api/settings/certificates/connecteam-documents", {},
                                  sessions["hana"]))[0] in (404, 405)
            # An older version's to-dos about cards added in Connecteam's own app are gone.
            hr.hdb.conn.execute("CREATE TABLE IF NOT EXISTS cert_ct_notes (id INTEGER PRIMARY KEY)")
            hr.hdb.conn.commit()
            certificates.setup(hr.svc)
            assert not hr.hdb.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'cert_ct_notes'").fetchone()
    run(scenario())


# ---------------------------------------------------------------- settings

def test_hr_adds_a_kind_of_certificate(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            sessions = await setup(hr)
            fresh(hr)
            status, data, _ = await hr.call("POST", "/api/settings/certificates/types", {"name": "CPR and First Aid"},
                                            sessions["hana"])
            assert status == 200
            t = certificates.type_by_id(hr.hdb, data["id"])
            assert (t["key"], t["reader"], t["groups_confirmed"], t["active"]) == ("cpr_and_first_aid", "generic", 1, 1)
            assert (await hr.call("POST", "/api/settings/certificates/types", {"name": "cpr and first aid"},
                                  sessions["hana"]))[0] == 400
            path = f"/api/settings/certificates/types/{t['id']}"
            for bad in ({"lasts_years": "x"}, {"remind": "60, nine"}, {"remind": [1000]}, {"groups": ["999"]},
                        {"people": ["nobody"]}, {"accepts": ["cpr_and_first_aid"]}, {"reader": "x"},
                        {"name": ""}):
                assert (await hr.call("PUT", path, bad, sessions["hana"]))[0] == 400, bad
            status, _, _ = await hr.call("PUT", path, {
                "lasts_years": "2", "due_days": "14", "remind": "30, 7", "lapse_message": "May not lifeguard",
                "groups": ["502"], "people": ["303"]}, sessions["hana"])
            assert status == 200
            t = certificates.type_by_id(hr.hdb, t["id"])
            assert (t["lasts_years"], t["due_days"], t["remind"], t["groups"], t["people"]) == (2, 14, [30, 7], ["502"], ["303"])
            assert by_type(certificates.status_of(hr.hdb, "304"))["cpr_and_first_aid"]["flag"] == "May not lifeguard"
            # Its dates: two years from the issue date when no expiry is printed.
            found = certificates.pick("CPR certificate\nCompleted 05/01/2026", t, date(2026, 10, 26), None)
            assert (found["expires"], found["how"]) == (date(2028, 5, 1), "calculated")
            # Remind every N days: 1 to 14.
            assert (await hr.call("PUT", "/api/settings/certificates", {"remind_every": 30}, sessions["hana"]))[0] == 400
            assert (await hr.call("PUT", "/api/settings/certificates", {"remind_every": 3}, sessions["hana"]))[0] == 200
            assert certificates.remind_every(hr.hdb) == 3
    run(scenario())


def test_off_by_default_and_inert(tmp_path):
    async def scenario():
        async with HR(tmp_path, enabled=False) as hr:
            status, body, _ = await hr.call("GET", "/api/certificates/board")
            assert status == 503 and "turned off" in body
            assert not (tmp_path / "data" / "hr").exists()
            assert certificates.migration_blocker(hr.svc) is None
    run(scenario())
