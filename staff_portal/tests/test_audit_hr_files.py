"""Audit fixes for the HR portal's files and card checks: PDF previews take turns (PDFium can't run
twice at once), a phone's HDR photo (MPO) is a photo, a very narrow back photo can't make a huge
sheet, a page full of dates is read quickly and off the event loop, and a decision made while
another HR admin's request was arriving isn't overwritten.

Nothing here talks to Google, Connecteam, TT Hotel or Home Assistant.
"""

import asyncio
import io
import threading
import time
from datetime import date, timedelta

import pytest

from hr_helpers import HR

from hr import access, audit, certificates, docroutes, documents, files, notify, ocr_dates  # noqa: E402
from test_hr_certificates import PERMIT_TEXT, add, google, run, setup  # noqa: E402
from test_hr_certificates import jpeg as card_jpeg  # noqa: E402
from test_hr_documents import kid, people, real_pdf, upload  # noqa: E402
from test_hr_documents import jpeg as doc_jpeg  # noqa: E402


# ---------------------------------------------------------------- files

def test_two_pdf_previews_at_once_take_turns(monkeypatch):
    import pypdfium2 as pdfium
    real = pdfium.PdfDocument
    guard, open_now, most = threading.Lock(), [0], [0]

    def counted(data):
        with guard:
            open_now[0] += 1
            most[0] = max(most[0], open_now[0])
        time.sleep(0.2)   # long enough for the other preview to start
        pdf = real(data)
        close = pdf.close

        def done():
            close()
            with guard:
                open_now[0] -= 1
        pdf.close = done
        return pdf

    monkeypatch.setattr(pdfium, "PdfDocument", counted)
    data = real_pdf(2)

    async def both():
        return await asyncio.gather(files.pdf_preview_async(data), files.pdf_preview_async(data))
    shown = run(both())
    assert all(files.sniff(s) == "jpeg" for s in shown)
    assert most[0] == 1


def test_a_phones_hdr_photo_mpo_is_a_photo():
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (400, 300), (200, 30, 30)).save(
        out, "MPO", save_all=True, append_images=[Image.new("RGB", (200, 150), (0, 0, 0))])
    assert Image.open(io.BytesIO(out.getvalue())).format == "MPO"
    cleaned, mime = files.clean_image(out.getvalue())
    img = Image.open(io.BytesIO(cleaned))
    assert mime == "image/jpeg" and img.format == "JPEG" and img.size == (400, 300)


def test_a_very_narrow_back_photo_cant_make_a_huge_sheet(monkeypatch):
    from PIL import Image
    made = []
    new = Image.new
    monkeypatch.setattr(Image, "new", lambda mode, size, *a, **k: (made.append(size), new(mode, size, *a, **k))[1])
    front, back = card_jpeg(size=(2000, 1200)), card_jpeg((20, 20, 200), size=(100, 3000))
    data, mime = run(certificates.prepare(front, back))
    assert mime == "image/jpeg"
    # Each part at most MAX_SIDE tall (was 2000 x 61,224 pixels: about a gigabyte of memory).
    assert made and max(h for _, h in made) <= 2 * files.MAX_SIDE + 24
    stacked = Image.open(io.BytesIO(data))
    assert stacked.width > 1000   # the front is still readable, not a sliver


# ---------------------------------------------------------------- reading dates

def test_a_page_full_of_dates_is_read_quickly():
    today = date(2026, 10, 29)
    # 1,000 labelled dates on one line (took about 6 seconds).
    line = " ".join(f"Issued {1 + i % 12}/{1 + i % 28}/{2018 + i % 8}" for i in range(1000))
    start = time.time()
    ocr_dates.pick_dates(line, "olcc", today)
    assert time.time() - start < 2
    # 3,000 unlabelled dates, one a line (took about 8 seconds).
    days = [today - timedelta(days=3000) + timedelta(days=i * 2) for i in range(3000)]
    start = time.time()
    found = ocr_dates.pick_dates("\n".join(f"{d.month}/{d.day}/{d.year}" for d in days), "generic", today)
    assert time.time() - start < 2 and found["how"] == "read"
    # The labels still go with the right dates.
    found = ocr_dates.pick_dates("Issued 06/14/2024 Expires 06/14/2029", "olcc", today)
    assert (found["issued"], found["expires"]) == (date(2024, 6, 14), date(2029, 6, 14))
    found = ocr_dates.pick_dates("Expires 01/02/2020 06/14/2029", "olcc", today)
    assert found["expires"] == date(2020, 1, 2)


def test_the_dates_are_picked_off_the_event_loop_from_the_kept_text(tmp_path, monkeypatch):
    seen = []
    real = certificates.pick
    monkeypatch.setattr(certificates, "pick", lambda text, *a: (
        seen.append((threading.current_thread() is threading.main_thread(), len(text))), real(text, *a))[1])

    async def scenario():
        async with HR(tmp_path) as hr, google(hr, monkeypatch) as fake:
            sessions = await setup(hr)
            fake.ocr_queue.append(PERMIT_TEXT + "x " * 20_000)
            status, data = await add(hr, sessions["ana"], "301", "olcc", card_jpeg())
            assert status == 200, data
            await certificates.settle(hr.svc)
            assert certificates.get(hr.hdb, data["card"]["id"])["read_expires"] == "2029-06-14"
    run(scenario())
    assert seen == [(False, certificates.MAX_TEXT)]


# ---------------------------------------------------------------- decisions made at once

def test_a_document_accepted_while_a_reject_arrives_stays_accepted(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr:
            s = await people(hr)
            status, r = await upload(hr, s["manager"], "202", kid(hr, "training"), doc_jpeg())
            assert status == 200 and r["document"]["status"] == "waiting"
            did = r["document"]["id"]
            real = docroutes.read_json

            async def slow_body(request):
                # Ivy accepts it while Hana's "not accepted" is still arriving.
                documents.activate(hr.hdb, documents.get(hr.hdb, did), "206")
                return await real(request)
            monkeypatch.setattr(docroutes, "read_json", slow_body)
            status, err, _ = await hr.call("POST", f"/api/documents/{did}/reject",
                                           {"reason": "Too blurry"}, s["hr"])
            assert status == 409 and "isn't waiting" in err["error"]
            assert documents.get(hr.hdb, did)["status"] == "active"
            assert not any(i["kind"] == "document_rejected" for i in notify.inbox(hr.hdb, "203"))
            # And the store itself only rejects what's still waiting.
            assert documents.reject(hr.hdb, documents.get(hr.hdb, did), "Late", "205") is False
            assert documents.get(hr.hdb, did)["status"] == "active"
    run(scenario())


def test_a_card_rejected_while_a_confirm_arrives_stays_rejected(tmp_path, monkeypatch):
    async def scenario():
        async with HR(tmp_path) as hr:
            sessions = await setup(hr)
            ivy = access.viewer_for(hr.hdb, hr.hdb.person("308"))
            status, data = await add(hr, sessions["ana"], "301", "olcc", card_jpeg())
            assert status == 200, data
            cid = data["card"]["id"]
            real = certificates.read_json

            async def slow_body(request):
                # Ivy rejects it while Hana's confirm is still arriving.
                certificates.reject(hr.svc, certificates.get(hr.hdb, cid), "Blurry photo", ivy)
                return await real(request)
            monkeypatch.setattr(certificates, "read_json", slow_body)
            status, err, _ = await hr.call("POST", f"/api/people/301/certificates/{cid}/confirm",
                                           {"expires": "2029-06-14"}, sessions["hana"])
            assert status == 400 and err["error"] == certificates.DECIDED
            row = certificates.get(hr.hdb, cid)
            assert row["status"] == "rejected" and row["document_id"] is None
            assert hr.hdb.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
            assert not any(i["kind"] == "cert_confirmed" for i in notify.inbox(hr.hdb, "301"))
    run(scenario())


def test_a_card_confirmed_first_is_not_also_rejected(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            sessions = await setup(hr)
            hana = access.viewer_for(hr.hdb, hr.hdb.person("307"))
            ivy = access.viewer_for(hr.hdb, hr.hdb.person("308"))
            status, data = await add(hr, sessions["ana"], "301", "olcc", card_jpeg())
            stale = certificates.get(hr.hdb, data["card"]["id"])
            t = certificates.type_by_key(hr.hdb, "olcc")
            values = certificates.clean_confirm({"expires": "2029-06-14"}, t, certificates.today(hr.hdb))
            await certificates.confirm(hr.svc, stale, values, hana)
            with pytest.raises(ValueError, match="already decided"):
                certificates.reject(hr.svc, stale, "Blurry photo", ivy)
            assert certificates.get(hr.hdb, stale["id"])["status"] == "confirmed"
            assert not any(i["kind"] == "cert_rejected" for i in notify.inbox(hr.hdb, "301"))
            assert "Didn't accept a certificate" not in [line["action"] for line in audit.lines(hr.hdb, 200)]
    run(scenario())


def test_a_card_gone_while_a_confirm_arrives_is_just_decided(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            sessions = await setup(hr)
            hana = access.viewer_for(hr.hdb, hr.hdb.person("307"))
            status, data = await add(hr, sessions["ana"], "301", "olcc", card_jpeg())
            stale = certificates.get(hr.hdb, data["card"]["id"])
            hr.hdb.conn.execute("DELETE FROM certificates WHERE id = ?", (stale["id"],))
            hr.hdb.conn.commit()
            t = certificates.type_by_key(hr.hdb, "olcc")
            values = certificates.clean_confirm({"expires": "2029-06-14"}, t, certificates.today(hr.hdb))
            with pytest.raises(ValueError, match="already decided"):
                await certificates.confirm(hr.svc, stale, values, hana)
            assert stale["id"] not in certificates._busy.get(hr.svc, set())
    run(scenario())
