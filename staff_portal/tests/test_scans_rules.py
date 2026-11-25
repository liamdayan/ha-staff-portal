"""Scans without the server: a scan-to option's rules and file names (app/scans.py), and talking
to a scanner over eSCL (app/escl.py) against the pretend one in fake_escl.py. Nothing here talks
to a real scanner or to Google."""

import asyncio
import os
import sys
from datetime import datetime
from xml.etree import ElementTree

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
            assert caps == {"model": "Brother MFC-J4345DW", "version": "2.63", "sources": ["Platen", "Feeder"],
                            "colors": ["color", "gray", "bw"], "resolutions": [100, 200, 300, 600],
                            "jpeg": True, "feeder_sheets": 20}
            assert await with_scanner(fake, lambda s: s.status()) == {
                "state": "Idle", "feeder": "loaded", "adf": "ScannerAdfLoaded"}
            fake.feeder = 0
            assert (await with_scanner(fake, lambda s: s.status()))["feeder"] == "empty"
            # A scanner that doesn't say: "don't know", not "empty".
            fake.adf_state = None
            assert await with_scanner(fake, lambda s: s.status()) == {"state": "Idle", "feeder": None, "adf": ""}
            for said, feeder in (("ScannerAdfJam", "jam"), ("ScannerAdfHatchOpen", "other"),
                                 ("ScannerAdfProcessing", "loaded")):
                fake.adf_state = said
                assert (await with_scanner(fake, lambda s: s.status()))["feeder"] == feeder
    run(scenario())


def test_the_feeder_state_found_wherever_the_scanner_puts_it():
    # Deeper in, or without the eSCL namespace: still the feeder's state.
    for xml in (b'<s:ScannerStatus xmlns:s="x"><s:Adf><s:AdfState>ScannerAdfLoaded</s:AdfState></s:Adf></s:ScannerStatus>',
                b"<ScannerStatus><AdfState>ScannerAdfLoaded</AdfState></ScannerStatus>"):
        assert escl.read_status(xml)["feeder"] == "loaded"


def test_where_to_scan_from():
    both = ["Platen", "Feeder"]
    assert escl.source_order(both, "loaded") == ["Feeder"]
    assert escl.source_order(both, "empty") == ["Platen"]
    assert escl.source_order(both, None) == ["Feeder", "Platen"]       # not said: feeder first
    assert escl.source_order(both, "other") == ["Feeder", "Platen"]
    assert escl.source_order(["Feeder"], "empty") == ["Feeder"]        # a feeder-only scanner
    assert escl.source_order(["Platen"], "loaded") == ["Platen"]
    assert escl.source_order([], None) == ["Feeder", "Platen"]
    with pytest.raises(escl.ScanError) as err:
        escl.source_order(both, "jam")
    assert err.value.code == "jam"


def test_feeder_first_then_the_glass_when_the_scanner_does_not_say():
    async def scenario():
        async with FakeScanner(feeder=3) as fake:
            fake.adf_state = None
            seen = []
            source, pages = await with_scanner(
                fake, lambda s: s.scan_from(["Feeder", "Platen"], "color", 200, "letter", seen.append))
            assert source == "Feeder" and len(pages) == 3 and seen == [1, 2, 3]
            # The feeder turns out empty: the glass, in the same go.
            source, pages = await with_scanner(
                fake, lambda s: s.scan_from(["Feeder", "Platen"], "color", 200, lambda src: "letter"))
            assert source == "Platen" and len(pages) == 1
            assert ["Feeder" in j for j in fake.jobs[1:]] == [True, False]
            # A jam part way isn't retried on the glass.
            fake.feeder, fake.jam_after = 3, 1
            with pytest.raises(escl.ScanError) as err:
                await with_scanner(fake, lambda s: s.scan_from(["Feeder", "Platen"], "color", 200, "letter"))
            assert err.value.code == "stopped" and len(fake.jobs) == 4
            # Nothing anywhere: said plainly.
            fake.feeder, fake.jam_after, fake.glass = 0, None, False
            with pytest.raises(escl.ScanError) as err:
                await with_scanner(fake, lambda s: s.scan_from(["Feeder", "Platen"], "color", 200, "letter"))
            assert err.value.code == "empty"
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


def test_an_answer_that_arrives_in_pieces_is_read_to_the_end():
    # A real scanner's answer comes over the network a piece at a time (0.12.28 read only the
    # first piece: "The scanner sent something that couldn't be read").
    async def go():
        async with FakeScanner(feeder=2) as fake:
            fake.pieces = 300
            async with aiohttp.ClientSession() as session:
                scanner = escl.Scanner(fake.address, session)
                assert (await scanner.capabilities())["model"] == "Brother MFC-J4345DW"
                pages = await scanner.scan("Feeder", "color", 200, "letter")
                assert pages == [page_jpeg(1), page_jpeg(2)]
                fake.capabilities = "<a>" + "x" * escl.MAX_XML + "</a>"   # too much is still refused
                fake.pieces = 100000
                with pytest.raises(escl.ScanError, match="more than expected"):
                    await scanner.capabilities()
    asyncio.run(go())


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


def test_the_scan_request_names_the_source_both_ways_in_the_specifications_order():
    # Some Brother scanners scan the glass when asked for the feeder only as pwg:InputSource.
    feeder = escl.settings_xml("Feeder", "gray", 200, "letter", "2.63").decode()
    assert "<pwg:InputSource>Feeder</pwg:InputSource>" in feeder
    assert "<scan:InputSource>Feeder</scan:InputSource>" in feeder
    assert "<scan:Duplex>false</scan:Duplex>" in feeder and "<pwg:Version>2.63</pwg:Version>" in feeder
    order = ["pwg:Version", "scan:Intent", "pwg:ScanRegions", "pwg:DocumentFormat>", "scan:DocumentFormatExt",
             "pwg:InputSource", "scan:InputSource", "scan:XResolution", "scan:YResolution", "scan:ColorMode",
             "scan:Duplex"]
    assert [feeder.index("<" + tag) for tag in order] == sorted(feeder.index("<" + tag) for tag in order)
    glass = escl.settings_xml("Platen", "color", 300, "a4", "nonsense").decode()
    assert "<scan:InputSource>Platen</scan:InputSource>" in glass and "Duplex" not in glass
    assert "<pwg:Version>2.6</pwg:Version>" in glass          # a version that isn't one: the default
    ElementTree.fromstring(feeder.encode())
    ElementTree.fromstring(glass.encode())


def test_the_scanner_is_asked_in_its_own_version_and_what_it_said_is_kept_for_the_log():
    async def scenario():
        async with FakeScanner(feeder=2) as fake:
            async with aiohttp.ClientSession() as session:
                scanner = escl.Scanner(fake.address, session)
                await scanner.capabilities()
                await scanner.status()
                await scanner.scan("Feeder", "color", 200, "letter")
            assert "<pwg:Version>2.63</pwg:Version>" in fake.jobs[0]
            trail = "\n".join(scanner.trail)
            assert "feeder ScannerAdfLoaded" in trail and "POST ScanJobs (Feeder, letter, 200 dpi" in trail
            assert "-> 201" in trail and trail.count("NextDocument -> 200") == 2 and "NextDocument -> 404" in trail
            fake.busy = True
            async with aiohttp.ClientSession() as session:
                scanner = escl.Scanner(fake.address, session)
                with pytest.raises(escl.ScanError):
                    await scanner.scan("Feeder", "color", 200, "letter")
            assert scanner.trail[-1].endswith("-> 503 (empty)")
    run(scenario())


def test_a_page_preview_is_a_smaller_jpeg_of_the_page():
    page = page_jpeg(1, size=(1700, 2200))
    small = escl.page_preview(page, side=400)
    assert small[:2] == b"\xff\xd8" and len(small) < len(page)
    width, height, parts = escl.jpeg_size(small)
    assert max(width, height) <= 400 and parts == 3 and abs(width / height - 1700 / 2200) < 0.02
    assert escl.jpeg_size(escl.page_preview(page_jpeg(2, gray=True), side=400))[2] == 1
    with pytest.raises(escl.ScanError):
        escl.page_preview(b"<html>not a picture</html>")


def test_a_brother_saying_no_more_pages_too_soon_is_asked_again():
    async def scenario():
        async with FakeScanner(feeder=3) as fake:
            fake.early_404 = 2
            async with aiohttp.ClientSession() as session:
                scanner = escl.Scanner(fake.address, session)
                pages = await scanner.scan("Feeder", "color", 200, "letter")
            assert len(pages) == 3 and not scanner.feeder_left and fake.feeder == 0
            assert "the scanner's record of the job: JobState=Completed, ImagesCompleted=3" in "\n".join(scanner.trail)
    run(scenario())


def test_a_scanner_that_leaves_the_paper_in_its_feeder_is_caught():
    async def scenario():
        async with FakeScanner(feeder=3) as fake:
            fake.ignores_feeder = True      # scans the glass instead, as the Brother was seen to
            async with aiohttp.ClientSession() as session:
                scanner = escl.Scanner(fake.address, session)
                pages = await scanner.scan("Feeder", "color", 200, "letter")
            assert len(pages) == 1 and scanner.feeder_left and fake.feeder == 3
            assert "didn't take it" in "\n".join(scanner.trail)
    run(scenario())


def test_a_jobs_record_in_the_scanners_status():
    raw = (b'<s:ScannerStatus xmlns:s="x" xmlns:p="y"><s:Jobs><s:JobInfo><p:JobUri>/eSCL/ScanJobs/a1</p:JobUri>'
           b'<p:JobState>Aborted</p:JobState><p:JobStateReasons><p:JobStateReason>AdfEmpty</p:JobStateReason>'
           b'</p:JobStateReasons></s:JobInfo><s:JobInfo><p:JobUri>/eSCL/ScanJobs/b2</p:JobUri>'
           b'<p:JobState>Completed</p:JobState></s:JobInfo></s:Jobs></s:ScannerStatus>')
    assert escl.job_info(raw, "/eSCL/ScanJobs/a1") == "JobState=Aborted, JobStateReason=AdfEmpty"
    assert escl.job_info(raw, "/eSCL/ScanJobs/zz") == "" and escl.job_info(b"nonsense", "/x") == ""
