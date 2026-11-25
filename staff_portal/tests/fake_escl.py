"""A pretend eSCL scanner (a Brother MFC-J4345DW by what it says of itself) for the Scans tests:
a small web server on this computer. Nothing here talks to a real scanner.

    async with FakeScanner(feeder=3) as scanner:
        scanner.address     # "http://127.0.0.1:<port>"
        scanner.feeder      # sheets waiting in the feeder (scanned pages come off it)
        scanner.jobs        # the settings XML of each job started, as text
        scanner.not_ready   # how many times the next page answers 503 first
        scanner.busy        # True: starting a job answers 503
        scanner.jam_after   # a number: the feeder jams after that many pages of a job
        scanner.pieces      # a number: answers are sent that many bytes at a time, with a pause
                            # between (a real scanner's answer arrives over the network in pieces)
        scanner.adf_state   # "say" (the default): the status says whether the feeder has paper;
                            # None: it leaves the feeder out; any other text: it says that
"""

import asyncio
import io

from aiohttp import web
from PIL import Image

CAPABILITIES = """<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
<pwg:Version>2.63</pwg:Version>
<pwg:MakeAndModel>Brother MFC-J4345DW</pwg:MakeAndModel>
<scan:Platen><scan:PlatenInputCaps><scan:MaxWidth>2550</scan:MaxWidth><scan:MaxHeight>3507</scan:MaxHeight>
<scan:SettingProfiles><scan:SettingProfile>
<scan:ColorModes><scan:ColorMode>BlackAndWhite1</scan:ColorMode><scan:ColorMode>Grayscale8</scan:ColorMode><scan:ColorMode>RGB24</scan:ColorMode></scan:ColorModes>
<scan:DocumentFormats><pwg:DocumentFormat>application/pdf</pwg:DocumentFormat><pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat></scan:DocumentFormats>
<scan:SupportedResolutions><scan:DiscreteResolutions>
<scan:DiscreteResolution><scan:XResolution>100</scan:XResolution><scan:YResolution>100</scan:YResolution></scan:DiscreteResolution>
<scan:DiscreteResolution><scan:XResolution>200</scan:XResolution><scan:YResolution>200</scan:YResolution></scan:DiscreteResolution>
<scan:DiscreteResolution><scan:XResolution>300</scan:XResolution><scan:YResolution>300</scan:YResolution></scan:DiscreteResolution>
<scan:DiscreteResolution><scan:XResolution>600</scan:XResolution><scan:YResolution>600</scan:YResolution></scan:DiscreteResolution>
</scan:DiscreteResolutions></scan:SupportedResolutions>
</scan:SettingProfile></scan:SettingProfiles></scan:PlatenInputCaps></scan:Platen>
<scan:Adf><scan:AdfSimplexInputCaps><scan:MaxWidth>2550</scan:MaxWidth><scan:MaxHeight>4200</scan:MaxHeight></scan:AdfSimplexInputCaps>
<scan:FeederCapacity>20</scan:FeederCapacity></scan:Adf>
</scan:ScannerCapabilities>"""


def page_jpeg(number: int, gray: bool = False, size=(170, 220)) -> bytes:
    """A small JPEG standing in for a scanned page (each page a different shade)."""
    shade = 40 + (number * 37) % 200
    image = Image.new("L" if gray else "RGB", size, shade if gray else (shade, 255 - shade, 120))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=60)
    return out.getvalue()


class FakeScanner:
    def __init__(self, feeder: int = 0, glass: bool = True):
        self.feeder = feeder          # sheets in the feeder
        self.glass = glass            # whether there's a page on the glass to scan
        self.jobs: list[str] = []
        self.deleted: list[str] = []
        self.not_ready = 0
        self.busy = False
        self.jam_after: int | None = None
        self.pieces = 0
        self.adf_state = "say"
        self.capabilities = CAPABILITIES
        self.page = None              # bytes to send as every page instead of a JPEG
        self._left: dict[str, int] = {}     # pages each job still has to give
        self._given: dict[str, int] = {}
        self._gray: dict[str, bool] = {}
        self._runner = None
        self.address = ""

    async def __aenter__(self):
        app = web.Application()
        app.router.add_get("/eSCL/ScannerCapabilities", self._capabilities)
        app.router.add_get("/eSCL/ScannerStatus", self._status)
        app.router.add_post("/eSCL/ScanJobs", self._start)
        app.router.add_get("/eSCL/ScanJobs/{job}/NextDocument", self._next)
        app.router.add_delete("/eSCL/ScanJobs/{job}", self._delete)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.address = f"http://127.0.0.1:{self._runner.addresses[0][1]}"
        return self

    async def __aexit__(self, *exc):
        await self._runner.cleanup()

    async def _send(self, request, body: bytes, kind: str):
        if not self.pieces:
            return web.Response(body=body, content_type=kind)
        resp = web.StreamResponse(headers={"Content-Type": kind})
        await resp.prepare(request)
        for at in range(0, len(body), self.pieces):
            await resp.write(body[at:at + self.pieces])
            await asyncio.sleep(0.01)
        await resp.write_eof()
        return resp

    async def _capabilities(self, request):
        return await self._send(request, self.capabilities.encode(), "text/xml")

    async def _status(self, request):
        adf = ("ScannerAdfLoaded" if self.feeder else "ScannerAdfEmpty") if self.adf_state == "say" else self.adf_state
        adf = f"<scan:AdfState>{adf}</scan:AdfState>" if adf is not None else ""
        return web.Response(content_type="text/xml", text=f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
<pwg:Version>2.63</pwg:Version><pwg:State>{'Processing' if self.busy else 'Idle'}</pwg:State>
{adf}</scan:ScannerStatus>""")

    async def _start(self, request):
        if self.busy:
            return web.Response(status=503)
        body = await request.text()
        self.jobs.append(body)
        job = f"job{len(self.jobs)}"
        feeder = "<pwg:InputSource>Feeder</pwg:InputSource>" in body
        self._left[job] = self.feeder if feeder else (1 if self.glass else 0)
        self._given[job] = 0
        self._gray[job] = "Grayscale8" in body or "BlackAndWhite1" in body
        if feeder:
            self.feeder = 0
        # A full address, as real scanners send: only its path should be used.
        return web.Response(status=201, headers={"Location": f"http://scanner.invalid/eSCL/ScanJobs/{job}"})

    async def _next(self, request):
        job = request.match_info["job"]
        if job not in self._left:
            return web.Response(status=404)
        if self.not_ready > 0:
            self.not_ready -= 1
            return web.Response(status=503)
        if self.jam_after is not None and self._given[job] >= self.jam_after:
            return web.Response(status=500)
        if self._left[job] <= 0:
            return web.Response(status=404)
        self._left[job] -= 1
        self._given[job] += 1
        body = self.page if self.page is not None else page_jpeg(self._given[job], self._gray[job])
        return await self._send(request, body, "image/jpeg")

    async def _delete(self, request):
        self.deleted.append(request.match_info["job"])
        self._left.pop(request.match_info["job"], None)
        return web.Response(status=200)
