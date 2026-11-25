"""A network scanner, over eSCL (the "AirScan" protocol most scanners and all-in-one printers
speak, Mopria-certified ones included): plain HTTP on the scanner itself.

    GET  /eSCL/ScannerCapabilities   what it can do (XML)
    GET  /eSCL/ScannerStatus         idle or busy, and whether paper is in the feeder
    POST /eSCL/ScanJobs              start a scan (XML settings); 201 with the job's address
    GET  <job>/NextDocument          the next page; 404 when there are no more; 503 not ready yet
    DELETE <job>                     stop it

Pages are asked for as JPEG, one a request, and put into one PDF here (pdf_from_jpegs) without
re-compressing them, so it doesn't matter how the scanner itself would have made a PDF.

The scanner is a device on the owner's own network, at the address they typed. Still, nothing it
sends is trusted: answers are size-limited, its XML is refused if it defines entities, redirects
aren't followed, the job's address is only ever used as a path on the scanner we asked, and a page
must be a JPEG that isn't absurdly large. Every problem is a ScanError with a plain-English
message for whoever is standing at the scanner.
"""

import asyncio
import io
import re
import struct
import urllib.parse
from xml.etree import ElementTree

import aiohttp

NS = {"scan": "http://schemas.hp.com/imaging/escl/2011/05/03",
      "pwg": "http://www.pwg.org/schemas/2010/12/sm"}
SOURCES = ("Feeder", "Platen")
COLORS = {"color": "RGB24", "gray": "Grayscale8", "bw": "BlackAndWhite1"}
# Paper sizes, in inches (eSCL counts in 300ths of an inch).
PAPER = {"letter": (8.5, 11.0), "legal": (8.5, 14.0), "a4": (8.27, 11.69)}
MAX_XML = 512 * 1024              # a capabilities or status answer
MAX_PAGE_BYTES = 40 * 2 ** 20     # one scanned page
MAX_PAGES = 60                    # in one go (a feeder holds 20 to 50 sheets)
MAX_SIDE = 12000                  # pixels: 14 inches at 600 dpi is 8400
REQUEST_SECONDS = 20              # an ordinary answer
PAGE_SECONDS = 120                # one page at 600 dpi can take a minute
NOT_READY_TRIES = 40              # 503s before a page: about a minute
ADDRESS_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.?$")

NO_ANSWER = "The scanner isn't answering. Check that it's turned on and on the network."


class ScanError(Exception):
    """A scan that couldn't be done. `message` is for the person at the scanner; `code` is one
    of "offline", "busy", "empty", "jam", "stopped", "refused", "bad"."""

    def __init__(self, message: str, code: str = "bad"):
        super().__init__(message)
        self.message = message
        self.code = code


def clean_address(value) -> str:
    """The scanner's address as typed, tidied to "http://host[:port]" (https stays https).
    "" when nothing is typed. Raises ValueError with a message for the owner."""
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port
    except ValueError:
        raise ValueError("Enter the scanner's address, such as 192.168.1.50.") from None
    host = parts.hostname or ""
    if (parts.scheme not in ("http", "https") or not ADDRESS_RE.match(host) or parts.username
            or parts.password or parts.query or parts.fragment
            or parts.path.rstrip("/") not in ("", "/eSCL")):
        raise ValueError("Enter the scanner's address, such as 192.168.1.50.")
    return f"{parts.scheme}://{host.rstrip('.')}{f':{port}' if port else ''}"


def _xml(raw: bytes) -> ElementTree.Element:
    """The scanner's XML, refused if it's too big or defines entities (which is how XML is made
    to swell or to read files)."""
    if len(raw) > MAX_XML or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise ScanError("The scanner sent something that couldn't be read.")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        raise ScanError("The scanner sent something that couldn't be read.") from None


def _texts(node, path: str) -> list[str]:
    return [(e.text or "").strip() for e in node.findall(path, NS) if (e.text or "").strip()]


def read_capabilities(raw: bytes) -> dict:
    """What a scanner says it can do: {model, sources: ["Feeder", "Platen"], colors: ["color",
    "gray", "bw"], resolutions: [100, 200, ...], jpeg: bool, feeder_sheets}."""
    root = _xml(raw)
    sources = []
    if root.find("scan:Platen", NS) is not None:
        sources.append("Platen")
    if root.find("scan:Adf", NS) is not None:
        sources.append("Feeder")
    modes = set(_texts(root, ".//scan:ColorMode"))
    resolutions = sorted({int(v) for v in _texts(root, ".//scan:XResolution") if v.isdigit()
                          and 50 <= int(v) <= 1200})
    sheets = _texts(root, ".//scan:FeederCapacity")
    return {"model": (_texts(root, "pwg:MakeAndModel") or [""])[0][:80],
            "sources": sources,
            "colors": [name for name, mode in COLORS.items() if mode in modes],
            "resolutions": resolutions,
            "jpeg": "image/jpeg" in _texts(root, ".//pwg:DocumentFormat") + _texts(root, ".//scan:DocumentFormatExt"),
            "feeder_sheets": int(sheets[0]) if sheets and sheets[0].isdigit() else None}


def read_status(raw: bytes) -> dict:
    """{state: "Idle" | "Processing" | "Stopped" | ..., feeder: "loaded" | "empty" | "jam" |
    "other" | None (no feeder said), adf: what the scanner said of its feeder, as it said it}.

    Scanners don't all say it the same way: some leave the feeder out of their status, some put
    it deeper in or under another namespace, so it's looked for anywhere and by its name alone.
    None or "other" means "don't know", not "empty" (see source_order)."""
    root = _xml(raw)
    state = (_texts(root, "pwg:State") or ["Unknown"])[0][:40]
    adf = next(((e.text or "").strip()[:60] for e in root.iter()
                if isinstance(e.tag, str) and e.tag.rsplit("}", 1)[-1] == "AdfState"
                and (e.text or "").strip()), "")
    word = adf.lower()
    feeder = (None if not adf else "jam" if "jam" in word
              else "empty" if "empty" in word
              else "loaded" if "loaded" in word or "processing" in word else "other")
    return {"state": state, "feeder": feeder, "adf": adf}


def source_order(sources: list[str], feeder: str | None) -> list[str]:
    """Where to scan from, in the order to try: the feeder when it has paper, the glass when the
    feeder is empty, and, when the scanner doesn't say (or says something we don't know), the
    feeder first and then the glass. `sources` is what the scanner has (read_capabilities);
    when it lists neither, both are tried."""
    has = [s for s in SOURCES if s in sources] or list(SOURCES)
    if feeder == "jam":
        raise ScanError("The scanner's feeder is jammed. Clear it, then try again.", "jam")
    if feeder == "loaded":
        order = ["Feeder", "Platen"]
    elif feeder == "empty":
        order = ["Platen", "Feeder"]
    else:
        order = ["Feeder", "Platen"]
    order = [s for s in order if s in has]
    # Told it's empty, or told it's loaded: trust that (and the other is only a last resort for
    # a scanner that has just the one).
    return order if feeder not in ("loaded", "empty") else order[:1]


def settings_xml(source: str, color: str, dpi: int, paper: str) -> bytes:
    """The scan request: one region the size of the paper, as JPEG pages."""
    if source not in SOURCES or color not in COLORS or paper not in PAPER or not 50 <= dpi <= 1200:
        raise ValueError("scan settings")
    width, height = (round(side * 300) for side in PAPER[paper])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="{NS['scan']}" xmlns:pwg="{NS['pwg']}">
  <pwg:Version>2.6</pwg:Version>
  <scan:Intent>Document</scan:Intent>
  <pwg:ScanRegions>
    <pwg:ScanRegion>
      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
      <pwg:XOffset>0</pwg:XOffset>
      <pwg:YOffset>0</pwg:YOffset>
      <pwg:Width>{width}</pwg:Width>
      <pwg:Height>{height}</pwg:Height>
    </pwg:ScanRegion>
  </pwg:ScanRegions>
  <pwg:InputSource>{source}</pwg:InputSource>
  <scan:ColorMode>{COLORS[color]}</scan:ColorMode>
  <scan:XResolution>{dpi}</scan:XResolution>
  <scan:YResolution>{dpi}</scan:YResolution>
  <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
  <scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt>
</scan:ScanSettings>
""".encode()


class Scanner:
    """One scanner at an address from clean_address()."""

    def __init__(self, address: str, session: aiohttp.ClientSession):
        self.base = address
        self.session = session

    async def _get(self, path: str, limit: int, seconds: float = REQUEST_SECONDS) -> tuple[int, bytes]:
        try:
            async with self.session.get(self.base + path, allow_redirects=False,
                                        timeout=aiohttp.ClientTimeout(total=seconds)) as resp:
                # read(n) gives only what has arrived so far, and a scanner sends its answer in
                # pieces: keep reading to the end (or to just past the limit).
                parts, size = [], 0
                while size <= limit:
                    piece = await resp.content.read(limit + 1 - size)
                    if not piece:
                        break
                    parts.append(piece)
                    size += len(piece)
                if size > limit:
                    raise ScanError("The scanner sent more than expected. Try again.")
                return resp.status, b"".join(parts)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            raise ScanError(NO_ANSWER, "offline") from None

    async def capabilities(self) -> dict:
        status, body = await self._get("/eSCL/ScannerCapabilities", MAX_XML)
        if status != 200:
            raise ScanError("That address answered, but not as a scanner. Check the address, and "
                            "that scanning over the network (AirPrint or eSCL) is turned on.", "refused")
        return read_capabilities(body)

    async def status(self) -> dict:
        status, body = await self._get("/eSCL/ScannerStatus", MAX_XML)
        if status != 200:
            raise ScanError(NO_ANSWER, "offline")
        return read_status(body)

    async def start(self, source: str, color: str, dpi: int, paper: str) -> str:
        """Start a scan. Returns the job's path on the scanner."""
        try:
            async with self.session.post(
                    self.base + "/eSCL/ScanJobs", data=settings_xml(source, color, dpi, paper),
                    headers={"Content-Type": "text/xml"}, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_SECONDS)) as resp:
                await resp.content.read(MAX_XML)
                status, where = resp.status, resp.headers.get("Location", "")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            raise ScanError(NO_ANSWER, "offline") from None
        if status == 503:
            raise ScanError("The scanner is busy. Wait for it to finish, then try again.", "busy")
        if status == 409:
            raise ScanError("The scanner can't scan that way. Ask a manager to check this "
                            "option's settings.", "refused")
        # Only the path is kept: the job is on the scanner we asked, wherever its answer points.
        path = urllib.parse.urlsplit(where).path
        if status != 201 or not re.fullmatch(r"/eSCL/ScanJobs/[A-Za-z0-9._~-]{1,100}/?", path):
            raise ScanError("The scanner didn't start. Try again.", "refused")
        return path.rstrip("/")

    async def next_page(self, job: str) -> bytes | None:
        """The job's next page (a JPEG), or None when there are no more."""
        for _ in range(NOT_READY_TRIES):
            status, body = await self._get(job + "/NextDocument", MAX_PAGE_BYTES, PAGE_SECONDS)
            if status == 200:
                jpeg_size(body)   # a JPEG of a sane size, or a ScanError
                return body
            if status == 404:
                return None
            if status != 503:
                raise ScanError("The scan stopped part way. Check the scanner for a paper jam, "
                                "then try again.", "stopped")
            await asyncio.sleep(1.5)
        raise ScanError("The scanner took too long. Try again.", "busy")

    async def cancel(self, job: str) -> None:
        try:
            async with self.session.delete(self.base + job, allow_redirects=False,
                                           timeout=aiohttp.ClientTimeout(total=5)) as resp:
                await resp.content.read(1024)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            pass

    async def scan(self, source: str, color: str, dpi: int, paper: str, on_page=None,
                   limit: int = MAX_PAGES) -> list[bytes]:
        """Scan everything in the feeder, or the one page on the glass. on_page(count) is called
        as each page arrives. Raises ScanError; a feeder that turns out empty is code "empty"."""
        job = await self.start(source, color, dpi, paper)
        pages: list[bytes] = []
        try:
            while True:
                page = await self.next_page(job)
                if page is None:
                    break
                pages.append(page)
                if on_page:
                    on_page(len(pages))
                if source == "Platen":
                    break
                if len(pages) >= limit:
                    raise ScanError(f"That's more than {limit} pages. Scan it in smaller "
                                    "batches.", "stopped")
        except BaseException:
            await self.cancel(job)
            raise
        if source == "Platen":
            await self.cancel(job)   # the one page is in hand: free the scanner now
        if not pages:
            raise ScanError("No pages came through. Put the pages in the feeder, or one on the "
                            "glass, then try again.", "empty")
        return pages

    async def scan_from(self, order: list[str], color: str, dpi: int, paper, on_page=None,
                        limit: int = MAX_PAGES) -> tuple[str, list[bytes]]:
        """Scan from the first source in `order` (source_order) that gives pages: (source,
        pages). The next is tried only when one gave nothing at all and said so (empty, refused,
        or stopped before a page); a busy or silent scanner, or one that stopped part way, isn't
        tried again. `paper` is a paper name, or a function of the source giving one."""
        for at, source in enumerate(order):
            arrived = [0]

            def seen(count, source=source):
                arrived[0] = count
                if on_page:
                    on_page(count)

            size = paper(source) if callable(paper) else paper
            try:
                return source, await self.scan(source, color, dpi, size, seen, limit)
            except ScanError as err:
                last = at == len(order) - 1
                if last or arrived[0] or err.code not in ("empty", "refused", "stopped"):
                    raise
        raise ScanError("No pages came through. Put the pages in the feeder, or one on the "
                        "glass, then try again.", "empty")


# ---------------------------------------------------------------- JPEG pages into one PDF

def jpeg_size(data: bytes) -> tuple[int, int, int]:
    """(width, height, colour components) of a JPEG, read from its frame header. Raises
    ScanError when it isn't one, or is too large to be a scanned page."""
    bad = ScanError("The scanner sent a page that couldn't be read. Try again.")
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise bad
    at = 2
    while at + 4 <= len(data):
        if data[at] != 0xFF:
            raise bad
        marker = data[at + 1]
        if marker == 0xFF:          # padding
            at += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            at += 2
            continue
        (length,) = struct.unpack(">H", data[at + 2:at + 4])
        if length < 2:
            raise bad
        # Start of frame (baseline, extended, progressive: not the lossless or arithmetic ones).
        if marker in (0xC0, 0xC1, 0xC2):
            if at + 10 > len(data):
                raise bad
            height, width, parts = struct.unpack(">HHB", data[at + 5:at + 10])
            if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE and parts in (1, 3, 4)):
                raise bad
            return width, height, parts
        if marker == 0xDA:          # the picture itself, with no frame header before it
            raise bad
        at += 2 + length
    raise bad


def pdf_from_jpegs(pages: list[bytes], dpi: int) -> bytes:
    """One PDF, a page for each JPEG at its scanned size, the JPEGs put in as they are."""
    if not pages:
        raise ValueError("no pages")
    out = io.BytesIO()
    offsets: list[int] = []

    def obj(body: bytes) -> None:
        offsets.append(out.tell())
        out.write(f"{len(offsets)} 0 obj\n".encode() + body + b"\nendobj\n")

    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    kids = " ".join(f"{3 + i * 3} 0 R" for i in range(len(pages)))
    obj(b"<< /Type /Catalog /Pages 2 0 R >>")
    obj(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    for i, jpeg in enumerate(pages):
        width, height, parts = jpeg_size(jpeg)
        w, h = round(width * 72 / dpi, 2), round(height * 72 / dpi, 2)
        space = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}[parts]
        # Scanners' CMYK JPEGs are stored inverted (Adobe's way).
        decode = " /Decode [1 0 1 0 1 0 1 0]" if parts == 4 else ""
        page, image, content = 3 + i * 3, 4 + i * 3, 5 + i * 3
        draw = f"q {w} 0 0 {h} 0 0 cm /Im0 Do Q".encode()
        obj(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] /Resources << /XObject "
            f"<< /Im0 {image} 0 R >> >> /Contents {content} 0 R >>".encode())
        obj(f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace {space}"
            f" /BitsPerComponent 8 /Filter /DCTDecode{decode} /Length {len(jpeg)} >>\nstream\n".encode()
            + jpeg + b"\nendstream")
        obj(f"<< /Length {len(draw)} >>\nstream\n".encode() + draw + b"\nendstream")
    xref = out.tell()
    out.write(f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(offsets) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()
