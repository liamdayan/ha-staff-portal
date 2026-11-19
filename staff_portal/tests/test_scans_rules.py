"""Scans without the server: a scan-to option's rules and file names (app/scans.py), and talking
to a scanner over eSCL (app/escl.py) against the pretend one in fake_escl.py. Nothing here talks
to a real scanner or to Google."""

import asyncio
import os
import sys
from datetime import datetime

import aiohttp
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import escl  # noqa: E402
import scans  # noqa: E402
from fake_escl import FakeScanner, page_jpeg  # noqa: E402

WHEN = datetime(2026, 11, 18, 14, 32)
OPTION = {"label": "Invoices", "prefix": "INV", "format": "{prefix}-{name}", "folder": "Invoices"}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- names

def test_the_name_is_the_format_filled_in_and_numbered_from_the_second():
    base = scans.base_name(OPTION, "Ana  Lopez", "Front Desk", WHEN)
    assert base == "INV-Ana Lopez"
    assert [scans.numbered(base, n) for n in range(3)] == ["INV-Ana Lopez.pdf", "INV-Ana Lopez-2.pdf",
                                                           "INV-Ana Lopez-3.pdf"]
    every = {**OPTION, "format": "{prefix} {first}_{last} {department} {option} {date} {time} {year}{month}{day}"}
    assert scans.base_name(every, "Ana Maria Lopez", "Front Desk", WHEN) == (
        "INV Ana_Lopez Front Desk Invoices 2026-11-18 1432 20261118")


def test_nothing_in_a_name_can_leave_its_folder():
    # A person or department named to climb out of the folder, or with characters files can't have.
    nasty = scans.base_name(OPTION, "../../etc/passwd", "", WHEN)
    assert "/" not in nasty and "\\" not in nasty and not nasty.startswith(".")
    assert scans.base_name(OPTION, 'A<b>:"c"|?*\x00\x1f', "", WHEN) == "INV-A b c"
    assert scans.base_name({**OPTION, "format": "{name}"}, "..", "", WHEN) == "Invoices"   # nothing left: the option's name
    # A piece that's empty (one-word name: no last name) leaves no dangling or doubled dashes.
    assert scans.base_name({**OPTION, "format": "{prefix}-{last}-{first}"}, "Cher", "", WHEN) == "INV-Cher"
    assert scans.base_name({**OPTION, "format": "{last}-{date}"}, "Cher", "", WHEN) == "2026-11-18"
    long = scans.base_name(OPTION, "x" * 500, "", WHEN)
    assert len(long) <= scans.MAX_BASE


@pytest.mark.parametrize("text, message", [
    ("{prefix}-{nmae}", "isn't one of the pieces"),
    ("{prefix}-{name", "brace without its pair"),
    ("scan", "needs at least one piece"),
    ("{name}/{date}", "can't have any of"),
    ("x" * 200 + "{name}", "at most 120"),
])
def test_a_bad_format_says_why(text, message):
    with pytest.raises(ValueError, match=message):
        scans.clean_format(text)


def test_an_option_is_checked_and_tidied():
    got = scans.clean_option({"label": " Guest  IDs ", "prefix": "ID", "format": "", "departments": [2, 9, 2]},
                             departments={1, 2})
    assert got == {"label": "Guest IDs", "prefix": "ID", "format": "{prefix}-{name}", "folder": "Guest IDs",
                   "color": "color", "quality": "normal", "paper": "letter", "departments": [2], "enabled": True}
    for raw, message in (({"label": ""}, "Give the option a name"),
                         ({"label": "A", "format": "{prefix}-{date}"}, "enter a prefix"),
                         ({"label": "A", "prefix": "P", "color": "sepia"}, "Pick the colour"),
                         ({"label": "A", "prefix": "P", "departments": ["1"]}, "Pick departments"),
                         ("x", "couldn't be read")):
        with pytest.raises(ValueError, match=message):
            scans.clean_option(raw, {1})
    # A folder can't be a path.
    assert scans.clean_option({"label": "A", "prefix": "P", "folder": "../HR/Secret"}, set())["folder"] == "HR Secret"


# ---------------------------------------------------------------- the scanner's address

def test_the_scanners_address_is_tidied_or_refused():
    assert escl.clean_address(" 192.168.1.50 ") == "http://192.168.1.50"
    assert escl.clean_address("BRW502E6679F589.local.") == "http://brw502e6679f589.local"   # names in small letters
    assert escl.clean_address("https://printer.lan:8443/eSCL/") == "https://printer.lan:8443"
    assert escl.clean_address("") == ""
    for bad in ("ftp://printer", "http://user:pw@printer", "http://printer/admin", "http://printer?x=1",
                "http://print er", "http://printer:99999", "http://", "file:///etc/passwd"):
        with pytest.raises(ValueError):
            escl.clean_address(bad)


# ---------------------------------------------------------------- scanning

async def with_scanner(fake, fn):
    async with aiohttp.ClientSession() as session:
        return await fn(escl.Scanner(fake.address, session))


def test_what_the_scanner_can_do_and_how_it_is():
    async def scenario():
        async with FakeScanner(feeder=2) as fake:
            caps = await with_scanner(fake, lambda s: s.capabilities())
            assert caps == {"model": "Brother MFC-J4345DW", "sources": ["Platen", "Feeder"],
                            "colors": ["color", "gray", "bw"], "resolutions": [100, 200, 300, 600],
                            "jpeg": True, "feeder_sheets": 20}
            assert await with_scanner(fake, lambda s: s.status()) == {"state": "Idle", "feeder": "loaded"}
            fake.feeder = 0
            assert (await with_scanner(fake, lambda s: s.status()))["feeder"] == "empty"
    run(scenario())


def test_the_feeder_gives_every_page_and_the_glass_one():
    async def scenario():
        async with FakeScanner(feeder=3) as fake:
            fake.not_ready = 2   # "not ready yet" twice before the first page
            seen = []
            pages = await with_scanner(fake, lambda s: s.scan("Feeder", "gray", 200, "letter", seen.append))
            assert len(pages) == 3 and seen == [1, 2, 3] and all(p[:2] == b"\xff\xd8" for p in pages)
            job = fake.jobs[0]
            assert "<pwg:InputSource>Feeder</pwg:InputSource>" in job and "Grayscale8" in job
            assert "<scan:XResolution>200</scan:XResolution>" in job and "image/jpeg" in job
            assert "<pwg:Width>2550</pwg:Width>" in job and "<pwg:Height>3300</pwg:Height>" in job   # Letter
            # The glass: one page, and the scanner is let go straight away.
            pages = await with_scanner(fake, lambda s: s.scan("Platen", "color", 300, "a4"))
            assert len(pages) == 1 and fake.deleted == ["job2"]
            assert "<pwg:Height>3507</pwg:Height>" in fake.jobs[1]
    run(scenario())


def test_problems_at_the_scanner_are_said_plainly():
    async def scenario():
        async with FakeScanner(feeder=0, glass=False) as fake:
            for source in ("Feeder", "Platen"):
                with pytest.raises(escl.ScanError) as err:
                    await with_scanner(fake, lambda s: s.scan(source, "color", 200, "letter"))
                assert err.value.code == "empty" and "No pages came through" in err.value.message
            fake.busy = True
            with pytest.raises(escl.ScanError) as err:
                await with_scanner(fake, lambda s: s.scan("Feeder", "color", 200, "letter"))
            assert err.value.code == "busy"
            # A jam after one page: the job is stopped on the scanner, nothing is kept.
            fake.busy, fake.feeder, fake.jam_after = False, 5, 1
            with pytest.raises(escl.ScanError) as err:
                await with_scanner(fake, lambda s: s.scan("Feeder", "color", 200, "letter"))
            assert err.value.code == "stopped" and "paper jam" in err.value.message
            assert fake.deleted[-1] == f"job{len(fake.jobs)}"
            # More pages than allowed in one go.
            fake.jam_after, fake.feeder = None, 6
            with pytest.raises(escl.ScanError, match="more than 4 pages"):
                await with_scanner(fake, lambda s: s.scan("Feeder", "color", 200, "letter", limit=4))
            # Something that isn't a JPEG.
            fake.feeder, fake.page = 1, b"<html>not a picture</html>"
            with pytest.raises(escl.ScanError, match="couldn't be read"):
                await with_scanner(fake, lambda s: s.scan("Feeder", "color", 200, "letter"))
        # Nothing at the address.
        async with aiohttp.ClientSession() as session:
            with pytest.raises(escl.ScanError) as err:
                await escl.Scanner("http://127.0.0.1:9", session).status()
            assert err.value.code == "offline"
    run(scenario())


def test_the_scanners_xml_is_not_trusted():
    for raw in (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>', b"<unclosed>",
                b"<x>" + b"a" * (escl.MAX_XML + 1) + b"</x>"):
        with pytest.raises(escl.ScanError):
            escl.read_capabilities(raw)
    # An answer that isn't a scanner's: nothing it can do, rather than a crash.
    assert escl.read_capabilities(b"<html><body>Printer</body></html>")["sources"] == []


# ---------------------------------------------------------------- pages into a PDF

def test_pages_become_one_pdf_that_a_reader_opens():
    pypdfium2 = pytest.importorskip("pypdfium2")
    pages = [page_jpeg(1, size=(1700, 2200)), page_jpeg(2, gray=True, size=(1700, 2800)), page_jpeg(3)]
    raw = escl.pdf_from_jpegs(pages, 200)
    assert raw.startswith(b"%PDF-1.4") and raw.rstrip().endswith(b"%%EOF")
    assert all(page in raw for page in pages)          # the JPEGs as they came, not re-compressed
    pdf = pypdfium2.PdfDocument(raw)
    assert len(pdf) == 3
    assert pdf[0].get_size() == (612, 792)             # 1700 x 2200 at 200 dpi is Letter
    assert pdf[1].get_size() == (612, 1008)            # and Legal
    image = pdf[0].render(scale=0.1).to_pil()
    assert image.getpixel((image.width // 2, image.height // 2))[:3] != (255, 255, 255)   # the picture is there
    pdf.close()
    assert escl.jpeg_size(pages[0]) == (1700, 2200, 3) and escl.jpeg_size(pages[1])[2] == 1
    for bad in (b"", b"\xff\xd8", b"\xff\xd8\xff\xda\x00\x02", b"GIF89a" + b"\x00" * 20):
        with pytest.raises(escl.ScanError):
            escl.jpeg_size(bad)
    with pytest.raises(ValueError):
        escl.pdf_from_jpegs([], 200)
