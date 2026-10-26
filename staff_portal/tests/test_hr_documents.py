"""The HR portal's documents part: kinds of document and who sees each (every role against every
visibility level), the restricted I-9 and accommodation sections, uploads (type checks,
re-encoding, limits, date-first names), HR's check, the delivery queue to Google Drive and
Connecteam (backoff, idempotency, waiting without using tries, restarts), the recycle bin,
retention and legal holds, the migration blocker, and PDFs made through Google Drive.

Nothing here talks to a real Google, Connecteam, TT Hotel or Home Assistant: Google is the fake
script (tests/fake_bridge.py) and Connecteam's documents a stand-in.
"""

import asyncio
import contextlib
import io
import json
import os
import re
import sys
import time

import aiohttp
import pytest
from aiohttp.test_utils import TestServer
from PIL import Image

from hr_helpers import CHANGE, HR, READ, ct_user

from hr import access, audit, crypto, ctapi, delivery, docroutes, documents, files, gdrive, notify, \
    pdfdoc, signin  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_bridge import HR_DRIVE, RESTRICTED_DRIVE, FakeBridge  # noqa: E402

SECRET = "docs-secret-" + "d" * 30
DAY = 86400
ANA = ct_user(201, "Ana", "Lee", code="4821", groups=[501], mobile="541-555-0201")
BO = ct_user(202, "Bo", "Diaz", code="7302", groups=[501], mobile="541-555-0202")
MAX = ct_user(203, "Max", "Ray", code="9900", groups=[501], mobile="541-555-0203")
DEE = ct_user(204, "Dee", "Fox", code="5566", groups=[502], mobile="541-555-0204")
HANA = ct_user(205, "Hana", "Ito", code="7788", groups=[502], mobile="541-555-0205")
IVY = ct_user(206, "Ivy", "Park", code="3344", groups=[502], mobile="541-555-0206")
# Bo (202) is the file we look at: an employee in the Pub (501), which Max manages.
ROLES = {"self": "202", "other": "201", "manager": "203", "director": "204", "hr": "205", "hr2": "206"}
SEES = {"self": {"employee"}, "other": set(), "manager": {"employee", "managers"},
        "director": {"employee", "managers", "directors"}, "hr": set(access.LEVELS)}
PDF = b"%PDF-1.4\n% a scanned form\n1 0 obj << >> endobj\n%%EOF\n"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def fresh_parts(monkeypatch):
    """Quick retries, and other parts' kinds and PDF makers kept to each test."""
    monkeypatch.setattr(gdrive, "RETRY_DELAYS", (0.01, 0.01))
    for name in ("REGISTERED", "CAN_OPEN", "PDF_MAKERS"):
        monkeypatch.setattr(documents, name, {})
    monkeypatch.setattr(documents, "OWNED", {documents.CERTIFICATES: "certificates"})
    monkeypatch.setattr(docroutes, "ct_documents", {"at": 0.0, "list": None})


async def people(hr) -> dict:
    await hr.add_people(ANA, BO, MAX, DEE, HANA, IVY)
    hr.give("203", "manager", {"501": True})
    hr.give("204", "director")
    hr.give("205", "hr_admin")
    hr.give("206", "hr_admin")
    sessions = {role: signin.new_session(hr.hdb, pid, "127.0.0.1", "test") for role, pid in ROLES.items()}
    confirm(hr)
    return sessions


def confirm(hr, at=None):
    """Every session has just typed a fresh code (sensitive actions)."""
    hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = ?", (time.time() if at is None else at,))
    hr.hdb.conn.commit()


@contextlib.asynccontextmanager
async def google(hr, monkeypatch, restricted=False):
    """The fake Google Drive script, set up as the owner would (the drives already allowed)."""
    fake = FakeBridge(SECRET)
    server = TestServer(fake.app())
    await server.start_server()
    origin = str(server.make_url("")).rstrip("/")
    monkeypatch.setattr(gdrive, "TEST_ORIGIN", origin)
    fake.allowed = [HR_DRIVE] + ([RESTRICTED_DRIVE] if restricted else [])
    hr.db.update_settings({"hr_google_url": f"{origin}/macros/s/FAKE-bridge/exec",
                           "hr_google_secret": SECRET, "hr_google_drive": HR_DRIVE,
                           "hr_google_restricted_drive": RESTRICTED_DRIVE if restricted else ""})
    try:
        yield fake
    finally:
        await delivery.settle(hr.svc)
        await gdrive.close()
        await server.close()


def kid(hr, key) -> int:
    return documents.kind_by_key(hr.hdb, key)["id"]


async def upload(hr, cookie, pid, kind, data, title="", day="", name="file.jpg"):
    form = aiohttp.FormData()
    form.add_field("kind", str(kind))
    form.add_field("title", title)
    form.add_field("date", day)
    form.add_field("file", data, filename=name, content_type="application/octet-stream")
    resp = await hr.client.post(f"/api/people/{pid}/documents", data=form,
                                headers={**CHANGE, "Cookie": f"{signin.COOKIE}={cookie}"})
    return resp.status, await resp.json()


async def open_file(hr, cookie, did):
    resp = await hr.client.get(f"/api/documents/{did}/file",
                               headers={**READ, "Cookie": f"{signin.COOKIE}={cookie}"})
    return resp.status, await resp.read(), resp


def jpeg(color=(200, 30, 30), marker="SecretPhone 12") -> bytes:
    """A small photo with camera details in it (EXIF), as phones add."""
    img = Image.new("RGB", (64, 48), color)
    exif = img.getexif()
    exif[0x010F] = marker          # Make
    exif[0x010E] = "GPS 44.97,-118.2"   # ImageDescription
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif)
    return out.getvalue()


def drive_path(fake, file_id) -> list[str]:
    """Where a file is in the fake drives: the drive's name, then each folder, then its name."""
    names, current = [], fake.files[file_id]
    while True:
        names.insert(0, current["name"])
        parent = current["parents"][0]
        if parent in fake.drives:
            return [fake.drives[parent], *names]
        current = fake.files[parent]


def section(page, name="documents"):
    return next((s for s in page["sections"] if s["name"] == name), None)


def deliveries(hr, did):
    return documents.deliveries_of(hr.hdb, did)


def actions(hr) -> list[str]:
    return [line["action"] for line in audit.lines(hr.hdb, 200)]


# ---------------------------------------------------------------- kinds in HR settings

def test_kinds_start_with_the_owners_defaults_and_hr_changes_them(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await people(hr)
            status, data, _ = await hr.call("GET", "/api/settings", cookie=s["hr"])
            box = data["sections"]["documents"]
            kinds = {k["key"]: k for k in box["kinds"]}
            assert {key: (k["name"], k["visibility"], k["folder"], k["uploaders"], k["connecteam"])
                    for key, k in kinds.items()} == {
                "certificates": ("Certificates", "employee", "Certificates", "employee", 1),
                "policies": ("Signed policies", "employee", "Documents", "hr", 0),
                "training": ("Training records", "employee", "Documents", "managers", 0),
                "hiring": ("Hiring paperwork", "directors", "Documents", "hr", 0),
                "other": ("Other", "hr", "Documents", "hr", 0),
                "i9": ("I-9", "restricted", "I-9", "hr", 0),
                "accommodation": ("Reasonable accommodation", "restricted", "Reasonable accommodation", "hr", 0)}
            assert kinds["i9"]["restricted"] and kinds["certificates"]["owned"] == "certificates"
            assert set(kinds["i9"]["fixed"]) == {"visibility", "uploaders", "connecteam", "ct_document"}
            assert box["copies_on"] is False and "every member of that drive" in box["restricted_warning"]
            assert box["drive"] and "isn't set up" in box["drive"]

            # HR adds a kind: its name, who sees it, its Drive folder and who may add one.
            status, r, _ = await hr.call("POST", "/api/documents/kinds", {
                "name": "Uniform  sizes", "visibility": "managers", "folder": "Uniforms",
                "uploaders": "managers"}, s["hr"])
            assert status == 200, r
            new = documents.kind(hr.hdb, r["id"])
            assert (new["key"], new["name"], new["visibility"], new["folder"], new["uploaders"]) == \
                (None, "Uniform sizes", "managers", "Uniforms", "managers")
            # ...renames it and lets fewer people see it.
            status, _, _ = await hr.call("PUT", f"/api/documents/kinds/{r['id']}",
                                         {"name": "Uniforms", "visibility": "directors"}, s["hr"])
            assert status == 200 and documents.kind(hr.hdb, r["id"])["visibility"] == "directors"
            # Restricted only when it's made (its files are kept apart), and a restricted kind
            # stays restricted, HR's only and out of Connecteam whatever is asked.
            status, err, _ = await hr.call("PUT", f"/api/documents/kinds/{r['id']}", {"visibility": "restricted"}, s["hr"])
            assert status == 400 and "only be restricted when it's made" in err["error"]
            i9 = kid(hr, "i9")
            status, _, _ = await hr.call("PUT", f"/api/documents/kinds/{i9}", {
                "name": "Form I-9", "visibility": "employee", "uploaders": "employee", "connecteam": True,
                "ct_document": 9}, s["hr"])
            k = documents.kind(hr.hdb, i9)
            assert status == 200 and (k["name"], k["visibility"], k["uploaders"], k["connecteam"],
                                      k["ct_document"]) == ("Form I-9", "restricted", "hr", 0, None)
            status, r2, _ = await hr.call("POST", "/api/documents/kinds", {
                "name": "Medical leave", "visibility": "restricted", "uploaders": "employee",
                "connecteam": True}, s["hr"])
            k = documents.kind(hr.hdb, r2["id"])
            assert (k["visibility"], k["uploaders"], k["connecteam"]) == ("restricted", "hr", 0)
            # Mistakes are refused in plain words.
            for body, words in (({"name": ""}, "1 to 60"), ({"name": "Other"}, "already a kind"),
                                ({"name": "X", "folder": "../etc"}, "Drive folder name"),
                                ({"name": "Y", "visibility": "everyone"}, "who can see"),
                                ({"name": "Z", "uploaders": "anyone"}, "who may add")):
                status, err, _ = await hr.call("POST", "/api/documents/kinds", body, s["hr"])
                assert status == 400 and words in err["error"], (body, err)
            # The Connecteam copy only while the owner's documents copy is on, and to a document.
            training = kid(hr, "training")
            status, err, _ = await hr.call("PUT", f"/api/documents/kinds/{training}", {"connecteam": True}, s["hr"])
            assert status == 400 and err["error"] == documents.COPIES_OFF
            hr.db.update_settings({"hr_connecteam_documents": "1"})
            status, err, _ = await hr.call("PUT", f"/api/documents/kinds/{training}", {"connecteam": True}, s["hr"])
            assert status == 400 and "Connecteam document" in err["error"]
            status, _, _ = await hr.call("PUT", f"/api/documents/kinds/{training}",
                                         {"connecteam": True, "ct_document": "55"}, s["hr"])
            assert status == 200 and documents.kind(hr.hdb, training)["ct_document"] == 55
            # Only HR admins, and only with a fresh code.
            for role in ("self", "manager", "director"):
                status, _, _ = await hr.call("POST", "/api/documents/kinds", {"name": "Mine"}, s[role])
                assert status == 403
            confirm(hr, time.time() - 3600)
            status, err, _ = await hr.call("PUT", f"/api/documents/kinds/{training}", {"name": "T"}, s["hr"])
            assert (status, err["error"]) == (401, "reconfirm")
            assert documents.kind(hr.hdb, training)["name"] == "Training records"
            lines = audit.lines(hr.hdb, 50, security=True)
            assert {"Added a kind of document", "Changed a kind of document"} <= {l["action"] for l in lines}
    run(scenario())


# ---------------------------------------------------------------- who sees what

def test_every_role_against_every_visibility_level(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await people(hr)
            ids = {}
            for level in access.LEVELS:
                status, r, _ = await hr.call("POST", "/api/documents/kinds", {
                    "name": f"Level {level}", "visibility": level, "uploaders": "employee"}, s["hr"])
                assert status == 200
                status, up = await upload(hr, s["hr"], "202", r["id"], PDF, title=f"File {level}")
                assert status == 200 and up["document"]["status"] == "active", up
                ids[level] = up["document"]["id"]
            for role in ("self", "other", "manager", "director", "hr"):
                status, page, _ = await hr.call("GET", "/api/people/202", cookie=s[role])
                sees = SEES[role]
                if role == "other":
                    assert status == 404
                else:
                    box = section(page)
                    shown = {d["kind_name"] for d in box["documents"] if d["kind_name"].startswith("Level")}
                    # Restricted ones are never on the page, even for HR (they're shown on request).
                    assert shown == {f"Level {l}" for l in sees - {"restricted"}}, role
                    addable = {k["name"] for k in box["kinds"] if k["name"].startswith("Level")}
                    assert addable == shown, role
                    assert "File restricted" not in json.dumps(page)
                    assert ("Level restricted" in json.dumps(page)) == (role == "hr")   # its name only
                    assert ("hr" in box) == (role == "hr")
                for level, did in ids.items():
                    status, body, _ = await open_file(hr, s[role], did)
                    assert (status == 200) == (level in sees), (role, level, status)
                    if status != 200:
                        assert json.loads(body)["error"] == "That isn't here, or you can't see it."
            # A document id that doesn't exist looks the same as one you can't see.
            assert (await open_file(hr, s["manager"], 999999))[0] == 404
            # Certificates belong to their own section: managers see status and dates there, but
            # never the file; the employee and HR open it.
            cert = await documents.add(hr.svc, hr.hdb.person("202"), documents.kind_by_key(hr.hdb, "certificates"),
                                       PDF, "application/pdf", "OLCC Service Permit", "2026-10-25",
                                       access.viewer_for(hr.hdb, hr.hdb.person("205")), "hr")
            for role, ok in (("self", True), ("manager", False), ("director", False), ("hr", True)):
                assert ((await open_file(hr, s[role], cert["id"]))[0] == 200) == ok, role
                status, page, _ = await hr.call("GET", "/api/people/202", cookie=s[role])
                assert "OLCC Service Permit" not in json.dumps(section(page))
            # Access follows the person live: Bo moves to the kitchen, and Max loses his files.
            await hr.add_people(ANA, ct_user(202, "Bo", "Diaz", code="7302", groups=[502],
                                             mobile="541-555-0202"), MAX, DEE, HANA, IVY)
            assert (await open_file(hr, s["manager"], ids["employee"]))[0] == 404
            # Every look at a file is in the audit log.
            assert actions(hr).count("Opened a document") >= 9
    run(scenario())


def test_a_part_that_owns_a_kind_decides_who_opens_its_files(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await people(hr)
            # Discipline's signed write-ups: level "managers", but the employee may open theirs.
            documents.register_kind("writeups", "Write-ups (signed)", "managers", "Discipline",
                                    "discipline", can_open=lambda v, rel, doc: rel in ("self", "hr"))
            k = documents.kind_by_key(hr.hdb, "writeups")
            assert (k["name"], k["visibility"], k["folder"]) == ("Write-ups (signed)", "managers", "Discipline")
            assert documents.OWNED["writeups"] == "discipline"
            doc = await documents.add_generated(hr.svc, "202", "writeups", PDF, "application/pdf",
                                                "Written warning - Attendance (signed)", "2026-10-25", "205")
            assert documents.view_url(doc) == f"/api/documents/{doc['id']}/file" == documents.view_url(doc["id"])
            for role, ok in (("self", True), ("manager", False), ("director", False), ("hr", True)):
                assert ((await open_file(hr, s[role], doc["id"]))[0] == 200) == ok, role
            # Not in the Documents section (its part shows it), and not added, accepted or
            # deleted from there.
            status, page, _ = await hr.call("GET", "/api/people/202", cookie=s["hr"])
            assert "Written warning" not in json.dumps(section(page))
            assert "Write-ups" not in [k["name"] for k in section(page)["kinds"]]
            status, err = await upload(hr, s["hr"], "202", k["id"], PDF)
            assert status == 400
            status, err, _ = await hr.call("DELETE", f"/api/documents/{doc['id']}", cookie=s["hr"])
            assert status == 409 and "own section" in err["error"]
            # HR may rename it and move its Drive folder, nothing else.
            status, _, _ = await hr.call("PUT", f"/api/documents/kinds/{k['id']}", {
                "name": "Signed write-ups", "folder": "Discipline files", "visibility": "employee"}, s["hr"])
            k = documents.kind_by_key(hr.hdb, "writeups")
            assert (k["name"], k["folder"], k["visibility"]) == ("Signed write-ups", "Discipline files", "managers")
            # Registering again (another start) keeps HR's changes.
            documents.register_kind("writeups", "Write-ups (signed)", "managers", "Discipline", "discipline")
            assert documents.kind_by_key(hr.hdb, "writeups")["name"] == "Signed write-ups"
            with pytest.raises(ValueError):
                documents.register_kind("Bad Key", "x", "managers", "Discipline", "x")
            with pytest.raises(ValueError):
                documents.register_kind("x", "x", "everyone", "Discipline", "x")
    run(scenario())


# ---------------------------------------------------------------- restricted sections

def test_restricted_files_are_hr_only_kept_apart_and_never_copied(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            s = await people(hr)
            hr.db.update_settings({"hr_connecteam_documents": "1"})
            i9 = kid(hr, "i9")
            # To anyone but HR the kind doesn't exist: the same answer as a made-up one.
            for role in ("self", "manager", "director"):
                assert await upload(hr, s[role], "202", i9, PDF) == \
                    await upload(hr, s[role], "202", 99999, PDF) == \
                    (400, {"error": "Choose a kind of document from the list."})
            status, r = await upload(hr, s["hr"], "202", i9, PDF, title="I-9 form", day="2026-01-05")
            assert status == 200 and r["document"]["status"] == "active"
            did = r["document"]["id"]
            await delivery.settle(hr.svc)
            doc = documents.get(hr.hdb, did)
            # No restricted drive picked: a "Restricted" folder in the HR drive, never Connecteam.
            assert drive_path(fake, doc["drive_id"]) == ["HR Employee Files", "Restricted", "Diaz, Bo (CT 202)",
                                                         "I-9", "2026-01-05 I-9 form.pdf"]
            assert [d["target"] for d in deliveries(hr, did)] == ["drive"]
            # Sealed with a key of its own on this computer.
            with open(documents.copy_path(hr.svc.folder, doc), "rb") as f:
                blob = f.read()
            with pytest.raises(crypto.Damaged):
                crypto.unseal(blob, documents.FILE_PURPOSE)
            assert crypto.unseal(blob, documents.RESTRICTED_PURPOSE) == PDF
            # Nobody else learns it exists: not on the page, not openable, no list.
            for role in ("self", "manager", "director"):
                status, page, _ = await hr.call("GET", "/api/people/202", cookie=s[role])
                text = json.dumps(page)
                assert status == 200 and "I-9" not in text and "accommodation" not in text.lower()
                status, body, _ = await open_file(hr, s[role], did)
                assert status == 404 and body == (await open_file(hr, s[role], 999999))[1]
                status, _, _ = await hr.call("GET", "/api/people/202/documents/restricted", cookie=s[role])
                assert status == 403
                status, _, _ = await hr.call("POST", f"/api/documents/{did}/restore", {}, s[role])
                assert status == 403
            # HR sees a count; the list on request; each look is logged.
            status, page, _ = await hr.call("GET", "/api/people/202", cookie=s["hr"])
            assert section(page)["hr"]["restricted"] == {"count": 1, "apart": False,
                                                         "kinds": ["I-9", "Reasonable accommodation"]}
            assert "I-9 form" not in json.dumps(section(page)["documents"])
            status, listed, _ = await hr.call("GET", "/api/people/202/documents/restricted", cookie=s["hr"])
            assert [d["title"] for d in listed["documents"]] == ["I-9 form"]
            assert "every member of that drive" in listed["warning"]
            assert {k["name"] for k in listed["kinds"]} == {"I-9", "Reasonable accommodation"}
            assert (await open_file(hr, s["hr"], did))[0] == 200
            assert {"Viewed restricted documents", "Opened a restricted document"} <= set(actions(hr))
            # An HR admin's own I-9: they're only an employee in their own file, so it's hidden
            # from them too; the other HR admin sees it.
            status, r = await upload(hr, s["hr2"], "205", i9, PDF, title="Hana I-9")
            assert status == 200
            own = r["document"]["id"]
            status, page, _ = await hr.call("GET", "/api/people/205", cookie=s["hr"])
            assert "Hana I-9" not in json.dumps(page) and "hr" not in section(page)
            assert (await open_file(hr, s["hr"], own))[0] == 404
            assert (await open_file(hr, s["hr2"], own))[0] == 200
            status, att, _ = await hr.call("GET", "/api/documents/attention", cookie=s["hr"])
            assert "Hana I-9" not in json.dumps(att)
    run(scenario())


def test_restricted_files_go_to_the_restricted_drive_when_one_is_picked(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch, restricted=True) as fake:
            s = await people(hr)
            status, r = await upload(hr, s["hr"], "202", kid(hr, "accommodation"), PDF, title="Request",
                                     day="2026-03-01")
            await delivery.settle(hr.svc)
            doc = documents.get(hr.hdb, r["document"]["id"])
            assert drive_path(fake, doc["drive_id"]) == ["HR Restricted", "Diaz, Bo (CT 202)",
                                                         "Reasonable accommodation", "2026-03-01 Request.pdf"]
            status, page, _ = await hr.call("GET", "/api/people/202", cookie=s["hr"])
            assert section(page)["hr"]["restricted"]["apart"] is True
            status, data, _ = await hr.call("GET", "/api/settings", cookie=s["hr"])
            assert data["sections"]["documents"]["restricted_warning"] is None
            # Ordinary files stay in the HR drive.
            status, r = await upload(hr, s["hr"], "202", kid(hr, "other"), PDF, title="Note", day="2026-03-02")
            await delivery.settle(hr.svc)
            doc = documents.get(hr.hdb, r["document"]["id"])
            assert drive_path(fake, doc["drive_id"])[:3] == ["HR Employee Files", "Employees", "Diaz, Bo (CT 202)"]
    run(scenario())


# ---------------------------------------------------------------- uploads and HR's check

def test_uploads_are_checked_cleaned_named_by_date_and_wait_for_hr(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            s = await people(hr)
            training, policies = kid(hr, "training"), kid(hr, "policies")
            # Only who the kind allows: training records are added by managers (and HR).
            assert (await upload(hr, s["self"], "202", training, jpeg()))[0] == 400
            # The type comes from the bytes, never the name.
            status, r = await upload(hr, s["manager"], "202", training, b"<html><script>x()</script>", name="card.jpg")
            assert (status, r["error"]) == (400, files.NOT_ALLOWED)
            heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 100
            status, r = await upload(hr, s["manager"], "202", training, heic, name="IMG_0001.HEIC")
            assert (status, r["error"]) == (400, files.HEIC)
            # 15 MB for everyone but HR; 25 MB for HR's scanned paperwork.
            big = b"%PDF-1.4\n" + b"0" * (15 * 2**20)
            status, r = await upload(hr, s["manager"], "202", training, big)
            assert status == 413 and "more than 15 MB" in r["error"]
            status, r = await upload(hr, s["hr"], "202", policies, big)
            assert status == 200, r
            status, r = await upload(hr, s["hr"], "202", policies, b"%PDF-1.4\n" + b"0" * (25 * 2**20))
            assert status == 413 and "more than 25 MB" in r["error"]
            status, r = await upload(hr, s["manager"], "202", training, jpeg(), day="10/20/2026")
            assert status == 400 and "YYYY-MM-DD" in r["error"]
            # A photo: re-encoded without its camera details, and waiting for HR.
            status, r = await upload(hr, s["manager"], "202", training, jpeg(), title="Food safety / basics",
                                     day="2026-10-20")
            assert status == 200 and r["document"]["status"] == "waiting" and "HR checks it" in r["message"]
            did = r["document"]["id"]
            doc = documents.get(hr.hdb, did)
            kept = await documents.load_copy(hr.svc.folder, doc)
            assert files.sniff(kept) == "jpeg" and b"SecretPhone" not in kept and b"GPS 44" not in kept
            assert not Image.open(io.BytesIO(kept)).getexif()
            assert documents.title_of(doc) == "Food safety basics"
            # HR admins are told (in the portal only); nothing went to Drive yet.
            for admin in ("205", "206"):
                assert any(i["kind"] == "document_waiting" and "Bo Diaz" in i["title"]
                           for i in notify.inbox(hr.hdb, admin))
            assert not [f for f in fake.files.values() if f["name"].startswith("2026-10-20")]
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["hr"])
            assert any("waiting for HR to check" in t["text"] for t in home["todos"])
            # Managers can't check; HR accepts, and it goes to Drive, the date first.
            assert (await hr.call("POST", f"/api/documents/{did}/accept", {}, s["manager"]))[0] == 403
            status, r, _ = await hr.call("POST", f"/api/documents/{did}/accept", {}, s["hr"])
            assert status == 200
            await delivery.settle(hr.svc)
            doc = documents.get(hr.hdb, did)
            assert drive_path(fake, doc["drive_id"]) == ["HR Employee Files", "Employees", "Diaz, Bo (CT 202)",
                                                         "Documents", "2026-10-20 Food safety basics.jpg"]
            assert fake.files[doc["drive_id"]]["appProperties"]["upload_id"] == doc["upload_id"]
            assert doc["keep_until"] > time.time() + 29 * DAY      # kept 30 days for quick viewing
            # A PDF is kept byte for byte.
            status, r = await upload(hr, s["hr"], "202", policies, PDF, title="Handbook acknowledgement",
                                     day="2026-10-25")
            await delivery.settle(hr.svc)
            doc = documents.get(hr.hdb, r["document"]["id"])
            assert fake.files[doc["drive_id"]]["data"] == PDF
            assert fake.files[doc["drive_id"]]["name"] == "2026-10-25 Handbook acknowledgement.pdf"
            # Not accepted: HR says why, whoever added it (and Bo) is told, it never reaches
            # Drive, and it goes after 30 days.
            status, r = await upload(hr, s["manager"], "202", training, jpeg((10, 200, 10)), title="Blurry")
            bad = r["document"]["id"]
            assert (await hr.call("POST", f"/api/documents/{bad}/reject", {"reason": " "}, s["hr"]))[0] == 400
            status, _, _ = await hr.call("POST", f"/api/documents/{bad}/reject", {"reason": "Too blurry to read"}, s["hr"])
            assert status == 200
            for pid in ("203", "202"):
                assert any(i["kind"] == "document_rejected" for i in notify.inbox(hr.hdb, pid))
            for role in ("manager", "self"):
                status, page, _ = await hr.call("GET", "/api/people/202", cookie=s[role])
                shown = next(d for d in section(page)["documents"] if d["id"] == bad)
                assert shown["status_word"] == "Not accepted" and shown["reason"] == "Too blurry to read"
            assert deliveries(hr, bad) == []
            documents.nightly(hr.svc, now=time.time() + 31 * DAY)
            assert documents.get(hr.hdb, bad)["status"] == "gone"
            assert not os.path.exists(documents.copy_path(hr.svc.folder, documents.get(hr.hdb, bad)))
            # An HR admin's own upload waits for another HR admin (nobody approves their own).
            await hr.call("PUT", f"/api/documents/kinds/{policies}", {"uploaders": "employee"}, s["hr"])
            status, r = await upload(hr, s["hr"], "205", policies, PDF, title="My handbook")
            assert status == 200 and r["document"]["status"] == "waiting"
            mine = r["document"]["id"]
            status, err, _ = await hr.call("POST", f"/api/documents/{mine}/accept", {}, s["hr"])
            assert status == 403 and "own file" in err["error"]
            assert not any("Hana Ito" in i["title"] for i in notify.inbox(hr.hdb, "205"))
            assert any("Hana Ito" in i["title"] for i in notify.inbox(hr.hdb, "206"))
            assert (await hr.call("POST", f"/api/documents/{mine}/accept", {}, s["hr2"]))[0] == 200
            # At most so many uploads wait in one file.
            monkeypatch.setattr(documents, "MAX_WAITING", 1)
            await upload(hr, s["manager"], "202", training, jpeg((1, 2, 3)))
            status, r = await upload(hr, s["manager"], "202", training, jpeg((4, 5, 6)))
            assert status == 400 and "waiting for HR" in r["error"]
            await delivery.settle(hr.svc)
            assert "waiting for HR to accept or reject it" in hr.svc.migration_blocker()
            assert "Added a document (waiting for HR)" in actions(hr)
            assert {"Accepted a document", "Didn't accept a document"} <= set(actions(hr))
    run(scenario())


def test_a_broken_or_odd_upload_is_refused(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await people(hr)
            headers = {**CHANGE, "Cookie": f"{signin.COOKIE}={s['hr']}"}
            resp = await hr.client.post("/api/people/202/documents", json={"kind": 1}, headers=headers)
            assert resp.status == 400
            resp = await hr.client.post("/api/people/202/documents", data=b"--x\r\nnonsense",
                                        headers={**headers, "Content-Type": "multipart/form-data; boundary=x"})
            assert resp.status == 400
            form = aiohttp.FormData()
            form.add_field("kind", str(kid(hr, "other")))
            resp = await hr.client.post("/api/people/202/documents", data=form, headers=headers)
            assert resp.status == 400 and (await resp.json())["error"] == docroutes.UNREADABLE
            form.add_field("note", b"x", filename="x.txt")
            resp = await hr.client.post("/api/people/202/documents", data=form, headers=headers)
            assert resp.status == 400 and (await resp.json())["error"] == "Choose a file to add."
            # From another site: refused before anything is read.
            resp = await hr.client.post("/api/people/202/documents", data=form,
                                        headers={**headers, "Origin": "https://evil.example"})
            assert resp.status == 403
            # Former employees: only HR adds to their file.
            hr.hdb.update_person("202", archived=1, archived_at=time.time())
            status, r = await upload(hr, s["hr"], "202", kid(hr, "other"), PDF)
            assert status == 200
    run(scenario())


# ---------------------------------------------------------------- the delivery queue

def test_failures_back_off_then_need_attention_with_retry(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            s = await people(hr)
            fake.fail_next(code="unavailable", message="Google Drive had a problem: boom", op="upload", times=20)
            status, r = await upload(hr, s["hr"], "202", kid(hr, "policies"), PDF)
            did = r["document"]["id"]
            await delivery.settle(hr.svc)
            [row] = deliveries(hr, did)
            gaps = [round(row["due_at"] - time.time())]
            while row["status"] == "waiting":
                at = row["due_at"]
                await delivery.run_due(hr.svc, now=at)
                row = deliveries(hr, did)[0]
                if row["status"] == "waiting":
                    gaps.append(round(row["due_at"] - at))
            # 1 minute, 5, 30, 2 hours, 6 hours, then daily; after 8 tries it needs attention.
            assert gaps[0] in (59, 60) and gaps[1:] == [300, 1800, 7200, 21600, 86400, 86400]
            assert (row["status"], row["tries"]) == ("attention", 8) and "boom" in row["error"]
            assert any(i["kind"] == "upload_stuck" for i in notify.inbox(hr.hdb, "205"))
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["hr"])
            assert any(t["text"] == "1 upload needs attention." and t["link"] == "#/attention"
                       for t in home["todos"])
            status, att, _ = await hr.call("GET", "/api/documents/attention", cookie=s["hr"])
            [item] = att["stuck"]
            assert (item["id"], item["word"], item["name"], item["tries"]) == (row["id"], "Google Drive", "Bo Diaz", 8)
            assert item["title"] == "Signed policies: Signed policies"
            assert (await hr.call("GET", "/api/documents/attention", cookie=s["manager"]))[0] == 403
            assert migration_text(hr)
            # Retry: its 8 tries back, sent now.
            fake.failures.clear()
            assert (await hr.call("POST", f"/api/deliveries/{row['id']}/retry", {}, s["manager"]))[0] == 403
            status, _, _ = await hr.call("POST", f"/api/deliveries/{row['id']}/retry", {}, s["hr"])
            assert status == 200
            await delivery.settle(hr.svc)
            assert deliveries(hr, did)[0]["status"] == "done" and documents.get(hr.hdb, did)["drive_id"]
            assert (await hr.call("POST", f"/api/deliveries/{row['id']}/retry", {}, s["hr"]))[0] == 404
            assert hr.svc.migration_blocker() is None
    run(scenario())


def migration_text(hr) -> str | None:
    return hr.svc.migration_blocker()


def test_a_retry_after_a_lost_reply_never_makes_a_second_copy(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            s = await people(hr)
            # Google saves the file, but every answer is lost (three tries in a row).
            fake.fail_next(status=500, op="upload", after_nonce=True, times=3)
            status, r = await upload(hr, s["hr"], "202", kid(hr, "policies"), PDF, title="Once")
            did = r["document"]["id"]
            await delivery.settle(hr.svc)
            [row] = deliveries(hr, did)
            assert (row["status"], row["tries"]) == ("waiting", 1)
            doc = documents.get(hr.hdb, did)
            copies = lambda: [f for f in fake.files.values() if f["appProperties"].get("upload_id") == doc["upload_id"]]  # noqa: E731
            assert len(copies()) == 1 and fake.ops.count("upload") == 3
            await delivery.run_due(hr.svc, now=row["due_at"])
            assert deliveries(hr, did)[0]["status"] == "done"
            assert len(copies()) == 1 and documents.get(hr.hdb, did)["drive_id"] == copies()[0]["id"]
    run(scenario())


def test_a_script_deployed_afresh_is_told_its_drives_again(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            s = await people(hr)
            await upload(hr, s["hr"], "202", kid(hr, "policies"), PDF, title="First")
            await delivery.settle(hr.svc)
            # The owner deployed the script again: it no longer knows which drives it may use.
            fake.allowed = []
            status, r = await upload(hr, s["hr"], "202", kid(hr, "policies"), PDF, title="Second")
            did = r["document"]["id"]
            await delivery.settle(hr.svc)
            [row] = deliveries(hr, did)
            assert (row["status"], row["tries"], row["code"]) == ("waiting", 1, "not_allowed")
            assert fake.allowed == [HR_DRIVE]                 # told again
            await delivery.run_due(hr.svc, now=row["due_at"])
            doc = documents.get(hr.hdb, did)
            assert deliveries(hr, did)[0]["status"] == "done"
            # The same folders, found again by their tags.
            assert drive_path(fake, doc["drive_id"])[:4] == ["HR Employee Files", "Employees",
                                                             "Diaz, Bo (CT 202)", "Documents"]
            assert len([f for f in fake.files.values() if f["name"] == "Diaz, Bo (CT 202)"]) == 1
    run(scenario())


def test_waiting_for_google_uses_no_tries_and_the_queue_survives_a_restart(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await people(hr)
            status, r = await upload(hr, s["hr"], "202", kid(hr, "policies"), PDF, title="Waiting")
            assert "until Google Drive is set up" in r["message"]
            did = r["document"]["id"]
            await delivery.settle(hr.svc)
            [row] = deliveries(hr, did)
            assert (row["status"], row["tries"], row["code"]) == ("waiting", 0, "not_set_up")
            for _ in range(10):
                await delivery.run_due(hr.svc, now=deliveries(hr, did)[0]["due_at"])
            assert deliveries(hr, did)[0]["tries"] == 0
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["hr"])
            assert any("waiting for Google Drive to be set up" in t["text"] for t in home["todos"])
            # A migration file waits for it, even with HR turned off.
            assert "1 HR file is still waiting" in hr.svc.migration_blocker()
            await hr.svc.stop()
            assert "1 HR file is still waiting" in hr.svc.migration_blocker()
            status, data = await hr.admin_call("POST", "/api/migrate/create")
            assert status == 409 and "still waiting to reach Google" in data["error"]
            # A restart (or an update): the work is still there, and goes once Google is set up.
            assert hr.svc.start() == "on"
            async with google(hr, monkeypatch) as fake:
                # A script that's out of date waits too, without a try.
                fake.fail_next(code="outdated", message="old", op="ensureFolder")
                hr.hdb.conn.execute("UPDATE deliveries SET due_at = 0")
                hr.hdb.conn.commit()
                hr.svc.sync_state["tried"] = time.time()    # no people sync in this tick
                await hr.svc.tick()                         # the service's half-minute tick
                await delivery.settle(hr.svc)
                row = deliveries(hr, did)[0]
                assert (row["status"], row["tries"], row["code"]) == ("waiting", 0, "outdated")
                await delivery.run_due(hr.svc, now=row["due_at"])
                assert deliveries(hr, did)[0]["status"] == "done"
                assert hr.svc.migration_blocker() is None
    run(scenario())


class Lane:
    """A stand-in for HR's Connecteam lane: records certificate copies, or raises what's queued."""

    def __init__(self):
        self.calls, self.errors = [], []
        self.stopped = {"chat": None, "documents": None}

    async def upload_certificate(self, user_id, document_id, pages, expires):
        self.calls.append((user_id, document_id, [(n, m, len(d)) for n, m, d in pages], expires))
        if self.errors:
            raise self.errors.pop(0)
        return {"status": "approved", "expires_at": None, "file_ids": ["f1"]}


def test_connecteam_copies_wait_for_the_lane_and_only_while_the_copy_is_on(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch):
            s = await people(hr)
            lane = Lane()
            monkeypatch.setattr(ctapi, "lane", lambda: lane)
            monkeypatch.setattr(ctapi, "default", lane)
            training = kid(hr, "training")
            hr.db.update_settings({"hr_connecteam_documents": "1"})
            await hr.call("PUT", f"/api/documents/kinds/{training}", {"connecteam": True, "ct_document": 55}, s["hr"])
            # The lane is full, then Connecteam's copies are stopped: both wait without a try.
            lane.errors = [ctapi.CTError("paused", "HR's share of Connecteam is used up for now.", True),
                           ctapi.CTError("stopped", "Connecteam refused the certificate copy.")]
            status, r = await upload(hr, s["hr"], "202", training, PDF, title="Food safety", day="2026-10-01")
            did = r["document"]["id"]
            await delivery.settle(hr.svc)
            row = next(d for d in deliveries(hr, did) if d["target"] == "connecteam")
            assert (row["status"], row["tries"], row["code"]) == ("waiting", 0, "paused")
            await delivery.run_due(hr.svc, now=row["due_at"])
            row = next(d for d in deliveries(hr, did) if d["target"] == "connecteam")
            assert (row["status"], row["tries"], row["code"]) == ("waiting", 0, "stopped")
            # A problem only a person can fix needs attention at once, with "Don't copy".
            lane.errors = [ctapi.CTError("not_assigned", "This person isn't assigned this document's pack.")]
            await delivery.run_due(hr.svc, now=row["due_at"])
            row = next(d for d in deliveries(hr, did) if d["target"] == "connecteam")
            assert row["status"] == "attention" and "pack" in row["error"]
            assert documents.get(hr.hdb, did)["keep_until"] is None   # kept here until it's settled
            status, att, _ = await hr.call("GET", "/api/documents/attention", cookie=s["hr"])
            assert att["stuck"][0]["can_skip"] is True
            # Retry: it goes, with the date-first name, to the kind's Connecteam document.
            await hr.call("POST", f"/api/deliveries/{row['id']}/retry", {}, s["hr"])
            await delivery.settle(hr.svc)
            assert lane.calls[-1] == ("202", 55, [("2026-10-01 Food safety.pdf", "application/pdf", len(PDF))], None)
            assert documents.get(hr.hdb, did)["ct_copied_at"]
            assert documents.get(hr.hdb, did)["keep_until"] > time.time() + 29 * DAY
            # "Don't copy": Drive keeps the file.
            lane.errors = [ctapi.CTError("expired", "This card has already expired.")]
            status, r = await upload(hr, s["hr"], "202", training, PDF, title="Old")
            await delivery.settle(hr.svc)
            row = next(d for d in deliveries(hr, r["document"]["id"]) if d["target"] == "connecteam")
            status, _, _ = await hr.call("POST", f"/api/deliveries/{row['id']}/skip", {}, s["hr"])
            assert status == 200 and deliveries(hr, r["document"]["id"])[1]["status"] == "skipped"
            # The owner turns the copy off: queued copies are dropped, new files aren't copied.
            hr.db.update_settings({"hr_connecteam_documents": "0"})
            status, r = await upload(hr, s["hr"], "202", training, PDF, title="New")
            await delivery.settle(hr.svc)
            assert [d["target"] for d in deliveries(hr, r["document"]["id"])] == ["drive"]
    run(scenario())


# ---------------------------------------------------------------- deleting, holds and retention

def test_deleting_restoring_the_recycle_bin_and_legal_holds(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            s = await people(hr)
            status, r = await upload(hr, s["hr"], "202", kid(hr, "policies"), PDF, title="Handbook")
            did = r["document"]["id"]
            await delivery.settle(hr.svc)
            drive_id = documents.get(hr.hdb, did)["drive_id"]
            # Deleting needs a fresh code, and only HR.
            assert (await hr.call("DELETE", f"/api/documents/{did}", cookie=s["manager"]))[0] == 403
            confirm(hr, time.time() - 3600)
            status, err, _ = await hr.call("DELETE", f"/api/documents/{did}", cookie=s["hr"])
            assert (status, err["error"]) == (401, "reconfirm")
            confirm(hr)
            status, r, _ = await hr.call("DELETE", f"/api/documents/{did}", cookie=s["hr"])
            assert status == 200 and "recycle bin for 30 days" in r["message"]
            # In the recycle bin: HR sees it (and can open it); nobody else.
            status, page, _ = await hr.call("GET", "/api/people/202", cookie=s["hr"])
            shown = next(d for d in section(page)["documents"] if d["id"] == did)
            assert shown["status_word"] == "In the recycle bin" and shown["can_restore"]
            assert shown["bin_until"] > time.time() + 29 * DAY
            status, page, _ = await hr.call("GET", "/api/people/202", cookie=s["self"])
            assert did not in [d["id"] for d in section(page)["documents"]]
            assert (await open_file(hr, s["self"], did))[0] == 404
            assert (await open_file(hr, s["hr"], did))[0] == 200
            # Restored as it was.
            status, _, _ = await hr.call("POST", f"/api/documents/{did}/restore", {}, s["hr"])
            assert status == 200 and documents.get(hr.hdb, did)["status"] == "active"
            assert len(deliveries(hr, did)) == 1                 # it was in Drive already
            # A legal hold stops every deletion.
            path = "/api/people/202/legal-hold"
            assert (await hr.call("PUT", path, {"on": True}, s["hr"]))[0] == 400
            status, _, _ = await hr.call("PUT", path, {"on": True, "reason": "Claim 2026-14"}, s["hr"])
            assert status == 200 and documents.hold_of(hr.hdb, "202")["reason"] == "Claim 2026-14"
            status, err, _ = await hr.call("DELETE", f"/api/documents/{did}", cookie=s["hr"])
            assert status == 409 and err["error"] == documents.HOLD_ON
            status, page, _ = await hr.call("GET", "/api/people/202", cookie=s["hr"])
            assert section(page)["hr"]["hold"]["reason"] == "Claim 2026-14"
            status, page, _ = await hr.call("GET", "/api/people/202", cookie=s["manager"])
            assert "Claim 2026-14" not in json.dumps(page)
            assert (await hr.call("PUT", "/api/people/205/legal-hold", {"on": True, "reason": "x"}, s["hr"]))[0] == 403
            # Under a hold, the recycle bin isn't emptied either.
            await hr.call("PUT", path, {"on": False}, s["hr"])
            await hr.call("DELETE", f"/api/documents/{did}", cookie=s["hr"])
            await hr.call("PUT", path, {"on": True, "reason": "Claim 2026-15"}, s["hr"])
            documents.nightly(hr.svc, now=time.time() + 31 * DAY)
            assert documents.get(hr.hdb, did)["status"] == "deleted"
            await hr.call("PUT", path, {"on": False}, s["hr"])
            # After 30 days: removed here, and into the shared drive's trash.
            later = time.time() + 31 * DAY
            counts = documents.nightly(hr.svc, now=later)
            assert counts["binned"] == 1
            doc = documents.get(hr.hdb, did)
            assert (doc["status"], doc["stored"]) == ("gone", 0)
            await delivery.settle(hr.svc)
            await delivery.run_due(hr.svc, now=later)
            assert fake.files[drive_id]["trashed"] is True
            assert (await open_file(hr, s["hr"], did))[0] == 404
            assert {"Deleted a document", "Restored a document", "Put a legal hold on a file",
                    "Lifted a legal hold"} <= set(actions(hr))
            # A file copied to Connecteam: removing it there is a to-do for HR (Connecteam's API
            # can't).
            status, r = await upload(hr, s["hr"], "202", kid(hr, "training"), PDF, title="Copied")
            other = r["document"]["id"]
            hr.hdb.conn.execute("UPDATE documents SET ct_copied_at = ? WHERE id = ?", (time.time(), other))
            hr.hdb.conn.commit()
            status, r, _ = await hr.call("DELETE", f"/api/documents/{other}", cookie=s["hr"])
            assert "Connecteam" in r["message"]
            status, att, _ = await hr.call("GET", "/api/documents/attention", cookie=s["hr"])
            [todo] = att["todos"]
            assert todo["name"] == "Bo Diaz" and "Remove its copy in Connecteam" in todo["what"]
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["hr"])
            assert any("removed in Connecteam" in t["text"] for t in home["todos"])
            assert (await hr.call("POST", f"/api/documents/todos/{todo['id']}/done", {}, s["hr"]))[0] == 200
            status, att, _ = await hr.call("GET", "/api/documents/attention", cookie=s["hr"])
            assert att["todos"] == []
    run(scenario())


def test_records_past_retention_are_listed_never_deleted_by_themselves(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await people(hr)
            for pid in ("201", "202"):
                await upload(hr, s["hr"], pid, kid(hr, "other"), PDF)
            now = time.time()
            hr.hdb.update_person("201", archived=1, archived_at=now - 6 * 365 * DAY)   # left 6 years ago
            hr.hdb.update_person("202", archived=1, archived_at=now - 4 * 365 * DAY)   # 4 years ago
            assert [p["name"] for p in documents.past_retention(hr.hdb)] == ["Ana Lee"]
            # Never deleted by themselves: the nightly run only tells HR (once).
            for _ in range(2):
                assert documents.nightly(hr.svc)["binned"] == 0
            assert documents.of_person(hr.hdb, "201", ("active",))
            assert [i["kind"] for i in notify.inbox(hr.hdb, "205")].count("records_due") == 1
            status, att, _ = await hr.call("GET", "/api/documents/attention", cookie=s["hr"])
            assert [r["name"] for r in att["retention"]] == ["Ana Lee"] and att["years"] == 5
            status, home, _ = await hr.call("GET", "/api/home", cookie=s["hr"])
            assert any("past the time they're kept" in t["text"] for t in home["todos"])
            status, page, _ = await hr.call("GET", "/api/people/201", cookie=s["hr"])
            assert section(page)["hr"]["retention"]["past"] is True
            # HR deletes them (fresh code), not before their time.
            status, err, _ = await hr.call("POST", "/api/people/202/documents/delete-all", {}, s["hr"])
            assert status == 409
            status, r, _ = await hr.call("POST", "/api/people/201/documents/delete-all", {}, s["hr"])
            assert status == 200 and documents.of_person(hr.hdb, "201", ("deleted",))
            assert documents.past_retention(hr.hdb) == []
            # The period is an HR setting; a legal hold keeps someone off the list.
            await hr.call("PUT", "/api/settings/general", {"retention_years": 3}, s["hr"])
            assert [p["name"] for p in documents.past_retention(hr.hdb)] == ["Bo Diaz"]
            documents.set_hold(hr.hdb, "202", True, "Claim", "205", "Hana Ito")
            assert documents.past_retention(hr.hdb) == []
            status, err, _ = await hr.call("POST", "/api/people/202/documents/delete-all", {}, s["hr"])
            assert status == 409 and err["error"] == documents.HOLD_ON
    run(scenario())


def test_drive_folders_follow_names_and_former_employees(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            s = await people(hr)
            twin = ct_user(207, "Bo", "Diaz", code="1212", groups=[502], mobile="541-555-0207")
            await hr.add_people(ANA, BO, MAX, DEE, HANA, IVY, twin)
            for pid in ("202", "207"):
                await upload(hr, s["hr"], pid, kid(hr, "other"), PDF, title=f"For {pid}")
            await delivery.settle(hr.svc)
            people_folders = [f for f in fake.files.values() if f["appProperties"].get("staff_access") == "person"]
            # Two people with one name stay apart: found by their Connecteam ID, never by name.
            assert sorted(f["name"] for f in people_folders) == ["Diaz, Bo (CT 202)", "Diaz, Bo (CT 207)"]
            # A new name in Connecteam: the folder is renamed in the nightly run.
            await hr.add_people(ANA, ct_user(202, "Bo", "Diaz-Ruiz", code="7302", groups=[501],
                                             mobile="541-555-0202"), MAX, DEE, HANA, IVY, twin)
            assert documents.nightly(hr.svc)["renamed"] == 1
            await delivery.settle(hr.svc)
            assert sorted(f["name"] for f in people_folders) == ["Diaz, Bo (CT 207)", "Diaz-Ruiz, Bo (CT 202)"]
            # Former employees: only when HR confirms they've left.
            path = "/api/people/202/drive-folder/former"
            assert (await hr.call("POST", path, {}, s["hr"]))[0] == 409   # still active
            hr.hdb.update_person("202", archived=1, archived_at=time.time())
            status, page, _ = await hr.call("GET", "/api/people/202", cookie=s["hr"])
            assert section(page)["hr"]["can_move_folder"] is True
            status, _, _ = await hr.call("POST", path, {}, s["hr"])
            assert status == 200
            await delivery.settle(hr.svc)
            bo = next(f for f in people_folders if f["appProperties"]["ct_user"] == "202")
            assert drive_path(fake, bo["id"]) == ["HR Employee Files", "Former employees", "Diaz-Ruiz, Bo (CT 202)"]
            assert (await hr.call("POST", path, {}, s["hr"]))[0] == 409   # already moved
    run(scenario())


# ---------------------------------------------------------------- opening files

def test_opening_files_sandboxed_pdfs_downloaded_and_fetched_from_drive(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            s = await people(hr)
            status, r = await upload(hr, s["hr"], "202", kid(hr, "policies"), jpeg(), title="Photo")
            photo = r["document"]["id"]
            status, r = await upload(hr, s["hr"], "202", kid(hr, "policies"), PDF, title="Signed")
            pdf = r["document"]["id"]
            await delivery.settle(hr.svc)
            status, body, resp = await open_file(hr, s["self"], photo)
            assert status == 200 and resp.content_type == "image/jpeg" and files.sniff(body) == "jpeg"
            assert resp.headers["Content-Disposition"].startswith("inline")
            assert resp.headers["Content-Security-Policy"].startswith("sandbox")
            assert resp.headers["X-Content-Type-Options"] == "nosniff"
            assert resp.headers["Cache-Control"] == "no-store"
            status, body, resp = await open_file(hr, s["self"], pdf)
            assert status == 200 and body == PDF
            assert re.fullmatch(r'attachment; filename="\d{4}-\d\d-\d\d Signed\.pdf"',
                                resp.headers["Content-Disposition"])   # PDFs are always downloads
            # 30 days later the copy here is gone: it's fetched from Drive and kept again.
            documents.nightly(hr.svc, now=time.time() + 31 * DAY)
            assert documents.get(hr.hdb, pdf)["stored"] == 0
            status, body, _ = await open_file(hr, s["self"], pdf)
            assert status == 200 and body == PDF and fake.ops.count("download") == 1
            assert documents.get(hr.hdb, pdf)["stored"] == 1
            assert (await open_file(hr, s["self"], pdf))[1] == PDF and fake.ops.count("download") == 1
            # Changed in Drive by hand to something else: not shown here.
            documents.nightly(hr.svc, now=time.time() + 31 * DAY)
            fake.files[documents.get(hr.hdb, photo)["drive_id"]]["data"] = b"<html>not a photo</html>"
            status, body, _ = await open_file(hr, s["self"], photo)
            assert status == 409
            # Google not set up and no copy here: says so.
            hr.db.update_settings({"hr_google_url": ""})
            assert (await open_file(hr, s["self"], pdf))[0] == 503
            assert actions(hr).count("Opened a document") == 4    # each one that opened
    run(scenario())


# ---------------------------------------------------------------- PDFs

def test_pdfdoc_makes_plain_escaped_html():
    html = pdfdoc.page("Written warning <b>bold</b>", [
        ("heading", "What happened"),
        ("paragraph", "Line 1\nLine 2\n\nNew <script>alert(1)</script> & café — done"),
        ("rows", [("Date", "2026-10-25"), {"label": "Points", "value": 2}]),
        {"signature": "Employee", "name": "Jane O'Hara", "date": "2026-10-26"},
        ("signature", "Witness"),
        ("list", ["One", "Two <i>"]),
        "A plain string is a paragraph.",
        ("note", "Small print"),
        ("row", "Step", "Written"),
        {"subheading": "More"},
        ("divider",),
    ], subtitle="Hot Lake Lodge", footer="Signing confirms receipt, not agreement.")
    assert html.isascii() and html.startswith("<!doctype html>")
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html and "<b>" not in html and "<i>" not in html
    assert "caf&#233;" in html and "&#8212;" in html and "Jane O&#x27;Hara" in html
    assert "Line 1<br>Line 2</p><p" in html
    tags = set(re.findall(r"<\s*/?\s*([a-zA-Z0-9]+)", html))
    assert tags <= {"html", "head", "meta", "title", "body", "h1", "h2", "h3", "p", "br", "table", "tr",
                    "td", "ul", "li", "hr"}
    assert "on" not in {a.split("=")[0][:2] for a in re.findall(r"\s([a-z-]+)=", html)}
    for bad in ([("javascript", "x")], [("rows", "not rows")], [("row", "only a label")], [{"a": 1}],
                [("paragraph", "x" * 60_000)], [42]):
        with pytest.raises(ValueError):
            pdfdoc.page("Title", bad)
    with pytest.raises(ValueError):
        pdfdoc.page("", [])
    with pytest.raises(ValueError):
        pdfdoc.page("T", [("paragraph", "x" * 40_000)] * 60)


def test_pdfs_are_made_through_google_drive_and_kept_in_the_kinds_folder(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr:
            await people(hr)
            html = pdfdoc.page("Written warning", [("rows", [("Employee", "Bo Diaz")]),
                                                   ("paragraph", "Late three times.")])
            with pytest.raises(documents.PdfUnavailable) as caught:
                await documents.make_pdf(hr.svc, html)
            assert caught.value.code == "not_set_up" and "isn't set up" in caught.value.message
            async with google(hr, monkeypatch) as fake:
                data = await documents.make_pdf(hr.svc, html)
                assert data.startswith(b"%PDF-") and b"(Written warning)" in data and b"(Late three times.)" in data
                assert fake.pdfs == [html]
                assert any(f["name"] == "_OCR scratch" for f in fake.files.values())
                # Through the client directly, too; an old script says it's out of date.
                assert (await hr.svc.google().render_pdf("<p>Hi</p>")).startswith(b"%PDF-")
                with pytest.raises(ValueError):
                    await hr.svc.google().render_pdf("")
                fake.version = 1
                with pytest.raises(documents.PdfUnavailable) as caught:
                    await documents.make_pdf(hr.svc, html)
                assert caught.value.code == "outdated" and "Copy the script" in caught.value.message
                fake.version = gdrive.SCRIPT_VERSION
                # A kind another part owns: generated files skip HR's check and go to its folder.
                documents.register_kind("writeups", "Write-ups", "managers", "Discipline", "discipline")
                doc = await documents.add_generated(hr.svc, hr.hdb.person("202"), "writeups", data,
                                                    "application/pdf", "Written warning - Attendance (signed)",
                                                    "2026-10-25", "203", upload_id="writeup-7")
                assert doc["status"] == "active" and doc["added_by_name"] == "Max Ray"
                again = await documents.add_generated(hr.svc, "202", "writeups", data, "application/pdf",
                                                      "Other", "2026-10-26", "203", upload_id="writeup-7")
                assert again["id"] == doc["id"]
                await delivery.settle(hr.svc)
                doc = documents.get(hr.hdb, doc["id"])
                assert drive_path(fake, doc["drive_id"]) == [
                    "HR Employee Files", "Employees", "Diaz, Bo (CT 202)", "Discipline",
                    "2026-10-25 Written warning - Attendance (signed).pdf"]
                for bad in (dict(kind_key="nothing"), dict(mime="image/png"), dict(data=b"<html>")):
                    args = {"kind_key": "writeups", "mime": "application/pdf", "data": data, **bad}
                    with pytest.raises(ValueError):
                        await documents.add_generated(hr.svc, "202", args["kind_key"], args["data"],
                                                      args["mime"], "x", "2026-10-25")
    run(scenario())


def test_a_pdf_google_cant_make_yet_is_made_later_by_the_queue(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await people(hr)
            made, finished = [], []

            def maker(svc, row):
                made.append(row["ref"])
                return (svc.db.person("202"), f"Annual review {row['ref']}",
                        pdfdoc.page("Annual review", [("paragraph", "Good work")]), "2026-10-20")

            async def done(svc, row, doc):
                finished.append((row["ref"], doc["id"]))
            documents.register_kind("reviews", "Reviews", "managers", "Reviews", "reviews")
            documents.register_pdf_maker("reviews", maker, done=done)
            try:
                await documents.make_pdf(hr.svc, "<p>x</p>")
            except documents.PdfUnavailable:
                first = documents.queue_pdf(hr.hdb, "reviews", "202", 42)
            assert first and documents.queue_pdf(hr.hdb, "reviews", "202", 42) is None   # once
            await delivery.settle(hr.svc)
            row = hr.hdb.conn.execute("SELECT * FROM deliveries WHERE id = ?", (first,)).fetchone()
            assert (row["status"], row["tries"], row["code"], row["ref"]) == ("waiting", 0, "not_set_up", "42")
            assert made == []
            async with google(hr, monkeypatch) as fake:
                await delivery.run_due(hr.svc, now=row["due_at"])
                await delivery.settle(hr.svc)
                row = hr.hdb.conn.execute("SELECT * FROM deliveries WHERE id = ?", (first,)).fetchone()
                doc = documents.get(hr.hdb, row["document_id"])
                assert row["status"] == "done" and made == ["42"] and finished == [("42", doc["id"])]
                assert documents.title_of(doc) == "Annual review 42" and doc["doc_date"] == "2026-10-20"
                assert drive_path(fake, doc["drive_id"])[-2:] == ["Reviews", "2026-10-20 Annual review 42.pdf"]
                assert b"(Good work)" in fake.files[doc["drive_id"]]["data"]
                # Nothing to make any more: skipped. A part's mistake: needs attention.
                documents.register_pdf_maker("reviews", lambda svc, row: None)
                skipped = documents.queue_pdf(hr.hdb, "reviews", "202", 43)
                await delivery.settle(hr.svc)
                documents.register_pdf_maker("reviews", lambda svc, row: ("202", "Empty", "  "))
                broken = documents.queue_pdf(hr.hdb, "reviews", "202", 44)
                lost = documents.queue(hr.hdb, "202", "pdf", kind_key="gone", ref="45")
                await delivery.settle(hr.svc)
                status = lambda i: hr.hdb.conn.execute(   # noqa: E731
                    "SELECT status, tries, code FROM deliveries WHERE id = ?", (i,)).fetchone()
                assert status(skipped)[0] == "skipped"
                assert tuple(status(broken))[::2] == ("attention", "bad_request")
                assert tuple(status(lost)) == ("waiting", 0, "no_maker")
                status_code, att, _ = await hr.call("GET", "/api/documents/attention", cookie=s["hr"])
                assert [(i["word"], i["title"]) for i in att["stuck"]] == [("Making a PDF", "A PDF for Reviews")]
                # Making it doesn't wait for a migration file.
                assert hr.svc.migration_blocker() is None
    run(scenario())
