"""A stand-in for TT Hotel's "SN Web Socket" helper with a USB card encoder plugged in, for
tests and for trying Wristbands without the hardware. Not started by the add-on.

It speaks SN Web Socket 2.5.1's protocol for the requests Staff Portal sends: a greeting on
connect, answers that repeat the request's fields plus "errorCode" (and "data"), errors of its
own without the requestId, and the encoder driver's codes. Cards behave like real ones: a blank
card must be set up (CE_InitCard) before it can be cleared or written (106 until then), records
add up until the card is cleared, and CE_ReadCard returns them.

    python encoder_sim.py --port 8102 [--auto] [--host 0.0.0.0]

Then enter ws://<this computer>:8102 as a computer's encoder address in Staff Access. Put a card
on the encoder and take it off with
    curl -X POST localhost:8102/control -d '{"action": "place"}'
    curl -X POST localhost:8102/control -d '{"action": "remove"}'
(also "place" with "card", "initialized" or "foreign"; "fail" with "function" and "code";
"unplug"; "plug"), see everything with GET /state, or use --auto: a blank card appears about
1.5 s after the encoder is first asked for one, and goes away about a second after it's written.
"""

import argparse
import collections
import json
import random
import re
import time

from aiohttp import WSMsgType, web

VERSION = "2.5.1"            # the SN Web Socket version this behaves like
GREETING = {"errorCode": 0, "description": "you are connection"}
NO_CARD = 101                # CE_GetCardID's errorCode with nothing on the encoder
AUTO_PLACE_SECONDS = 1.5     # --auto: a card appears this long after the first poll without one
AUTO_REMOVE_SECONDS = 1.0    # --auto: a written card is lifted off this long after the write
AUTO_IDLE_RESET = 5.0        # --auto: a gap in polling means nobody was waiting for a card
OUR_HOTEL = "this hotel"     # every hotelInfo counts as this hotel (real ones change often)
OTHER_HOTEL = "another hotel"
MAC_RE = re.compile(r"^[0-9A-Fa-f]{12}$")


class EncoderSim:
    """The simulated helper and encoder. Its controls (place_card, remove_card, fail_next,
    unplug, plug_in) work from tests in the same process, or through POST /control."""

    def __init__(self, *, auto: bool = False, place_after: float = AUTO_PLACE_SECONDS,
                 remove_after: float = AUTO_REMOVE_SECONDS):
        self.auto = auto
        self.place_after = place_after
        self.remove_after = remove_after
        self.cards: dict[str, dict] = {}     # number -> {"hotel": None or whose, "records"}
        self.present: str | None = None      # the card on the encoder
        self.connected = False               # the driver has the USB port open
        self.plugged = True
        self.dead_port_code: int | None = None   # unplugged while connected: CE_GetCardID's code
        self.hotel_info: str | None = None
        self.sectors = "0000000000011111"
        self.beeps = 0
        self.calls: collections.deque = collections.deque(maxlen=2000)   # function names
        self.sockets = 0                     # WebSockets open right now
        self.connections = 0                 # WebSockets ever opened
        self._failures: dict[str, collections.deque] = {}
        self._random = random.Random()
        self._wait_started: float | None = None
        self._last_poll: float | None = None
        self._remove_at: float | None = None
        self._runner: web.AppRunner | None = None

    # ------------------------------------------------------------ controls

    def place_card(self, number: str | None = None, *, initialized: bool = False,
                   foreign: bool = False) -> str:
        """Put a card on the encoder and return its number. A new card is blank unless
        initialized (already this hotel's) or foreign (another hotel's); a card seen before
        keeps what's on it."""
        if number is None:
            number = self._new_number()
        number = str(number).strip()
        if number not in self.cards:
            hotel = OTHER_HOTEL if foreign else OUR_HOTEL if initialized else None
            self.cards[number] = {"hotel": hotel, "records": []}
        self.present = number
        self._remove_at = None
        self._wait_started = None
        return number

    def remove_card(self) -> None:
        self.present = None
        self._remove_at = None

    def fail_next(self, function: str, code: int) -> None:
        """The next call to this function answers with this errorCode (calls queue up)."""
        self._failures.setdefault(function, collections.deque()).append(int(code))

    def unplug(self, card_id_code: int | None = None) -> None:
        """Pull the USB cable. The driver still thinks the port is open; CE_GetCardID then
        answers card_id_code (None: like an empty encoder, which is what makes it tricky)."""
        self.plugged = False
        self.dead_port_code = card_id_code

    def plug_in(self) -> None:
        self.plugged = True

    def state(self) -> dict:
        return {"present": self.present, "connected": self.connected, "plugged": self.plugged,
                "sectors": self.sectors, "beeps": self.beeps, "auto": self.auto,
                "cards": self.cards, "calls": list(self.calls)[-50:]}

    # ------------------------------------------------------------ server

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._websocket)
        app.router.add_post("/control", self._control)
        app.router.add_get("/state", self._state)
        return app

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> str:
        """Serve in the background; returns the ws:// address (port 0 picks a free one)."""
        self._runner = web.AppRunner(self.app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        bound = self._runner.addresses[0][1]
        return f"ws://{host}:{bound}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _websocket(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse()
        if not ws.can_prepare(request).ok:
            return web.Response(text=f"SN Web Socket simulator {VERSION}\n")
        await ws.prepare(request)
        self.sockets += 1
        self.connections += 1
        try:
            await ws.send_json(GREETING)
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                await ws.send_json(self.answer(msg.data))
        finally:
            self.sockets -= 1
        return ws

    async def _control(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except ValueError:
            return web.json_response({"error": "send JSON"}, status=400)
        action = data.get("action") if isinstance(data, dict) else None
        if action == "place":
            card = self.place_card(data.get("card"), initialized=bool(data.get("initialized")),
                                   foreign=bool(data.get("foreign")))
            return web.json_response({"ok": True, "present": card})
        if action == "remove":
            self.remove_card()
        elif action == "fail" and isinstance(data.get("function"), str):
            self.fail_next(data["function"], int(data.get("code", 1)))
        elif action == "unplug":
            self.unplug(data.get("code"))
        elif action == "plug":
            self.plug_in()
        elif action == "auto":
            self.auto = bool(data.get("on", True))
        else:
            return web.json_response({"error": "unknown action"}, status=400)
        return web.json_response({"ok": True, "present": self.present})

    async def _state(self, request: web.Request) -> web.Response:
        return web.json_response(self.state())

    # ------------------------------------------------------------ protocol

    def answer(self, text: str) -> dict:
        """One request in, one answer out, as SN Web Socket 2.5.1 does it."""
        try:
            request = json.loads(text)
        except ValueError:
            request = None
        if not isinstance(request, dict) or not request.get("function"):
            return {"errorCode": -1, "function": "", "snWebSocketVersion": VERSION}
        function = str(request["function"])
        self.calls.append(function)
        handler = getattr(self, f"_fn_{function}", None)
        if handler is None:
            # The helper's own errors don't repeat the request (so no requestId).
            return {"errorCode": -3, "description": "功能无效", "function": function,
                    "snWebSocketVersion": VERSION}
        failures = self._failures.get(function)
        if failures:
            code, data = failures.popleft(), None
        else:
            code, data = handler(request)
        if function == "GetDeviceInfo":   # nests its errorCode inside data, nothing on top
            return {"data": {"errorCode": code, **({"data": data} if data else {})}, **request,
                    "snWebSocketVersion": VERSION}
        reply = {**request, "errorCode": code, "function": function,
                 "snWebSocketVersion": VERSION}
        if data is not None:
            reply["data"] = data
        return reply

    def _ready(self) -> int:
        return 0 if self.connected and self.plugged else 3

    def _card(self, hotel_info, ours: bool = True) -> tuple[int, dict | None]:
        if self._ready():
            return 3, None
        if not hotel_info:
            return 13, None
        if self.present is None:
            return 101, None
        card = self.cards[self.present]
        if ours and card["hotel"] != OUR_HOTEL:
            return 106, None   # blank, or another hotel's
        return 0, card

    def _fn_GetDeviceInfo(self, request):
        if not self.plugged:
            return 1003, None
        self.connected = True
        return 0, {"snWebSocketVersion": VERSION, "dll": "simulator", "encoder": "sim-1",
                   "sector": self.sectors}

    def _fn_CE_ConnectComm(self, request):
        if not self.plugged or not str(request.get("portname") or "").strip():
            return 1, None
        self.connected = True
        return 0, None

    def _fn_CE_DisconnectComm(self, request):
        self.connected = False
        return 0, None

    def _fn_CE_GetVersion(self, request):
        if self._ready():
            return 3, None
        return 0, {"dll": "simulator", "encoder": "sim-1"}

    def _fn_getSerialPortList(self, request):
        return 0, [{"path": "COM3", "manufacturer": "Silicon Labs"}]

    def _fn_CE_InitCardEncoder(self, request):
        if self._ready():
            return 3, None
        if not request.get("hotelInfo"):
            return 13, None
        self.hotel_info = request["hotelInfo"]
        return 0, None

    def _fn_CE_SetSectors(self, request):
        if self._ready():
            return 3, None
        sectors = request.get("sectors")
        if not isinstance(sectors, str) or not re.fullmatch(r"[01]{16}", sectors):
            return 2, None
        self.sectors = sectors
        return 0, None

    def _fn_CE_GetCardID(self, request):
        if not self.connected:
            return 3, {"errorCode": 3, "data": "", "type": 0}
        if not self.plugged:
            code = self.dead_port_code if self.dead_port_code is not None else NO_CARD
            return code, {"errorCode": code, "data": "", "type": 0}
        if self.auto:
            self._auto_step()
        if self.present is None:
            return NO_CARD, {"errorCode": NO_CARD, "data": "", "type": 0}
        return 0, {"errorCode": 0, "data": self.present, "type": 0,
                   "transformData": self.present}

    def _fn_CE_InitCard(self, request):
        code, card = self._card(request.get("hotelInfo"), ours=False)
        if code:
            return code, None
        if card["hotel"] == OTHER_HOTEL:
            return 106, None
        card.update(hotel=OUR_HOTEL, records=[])
        return 0, None

    def _fn_CE_ClearCard(self, request):
        code, card = self._card(request.get("hotelInfo"))
        if code:
            return code, None
        card["records"] = []
        return 0, None

    def _fn_CE_WriteCard(self, request):
        code, card = self._card(request.get("hotelInfo"))
        if code:
            return code, None
        building, floor = request.get("buildNo"), request.get("floorNo")
        mac, expires = request.get("mac"), request.get("timestamp")
        if not (isinstance(building, int) and 0 <= building <= 254 and isinstance(floor, int)
                and 0 <= floor <= 255 and isinstance(mac, str) and MAC_RE.match(mac)
                and isinstance(expires, int) and 0 < expires <= 0xFFFFFFFF):
            return 2, None
        if len(card["records"]) >= 4 * (self.sectors.count("1") or 16):
            return 104, None
        card["records"].append({"buildNo": building, "floorNo": floor, "mac": mac.upper(),
                                "startDate": int(time.time()), "endDate": expires,
                                "allowLockOut": int(bool(request.get("allowLockOut")))})
        if self.auto:
            self._remove_at = time.monotonic() + self.remove_after
        return 0, None

    def _fn_CE_ReadCard(self, request):
        code, card = self._card(request.get("hotelInfo"))
        if code:
            return code, None
        return 0, [dict(r) for r in card["records"]]

    def _fn_CE_Beep(self, request):
        if self._ready():
            return 3, None
        self.beeps += 1
        return 0, None

    # ------------------------------------------------------------ internals

    def _new_number(self) -> str:
        while True:
            number = str(self._random.randrange(10 ** 9, 10 ** 10))
            if number not in self.cards:
                return number

    def _auto_step(self) -> None:
        """--auto: someone at the desk puts a fresh card down and lifts written ones off."""
        now = time.monotonic()
        if self._last_poll is not None and now - self._last_poll > AUTO_IDLE_RESET:
            self._wait_started = None
        self._last_poll = now
        if self.present is not None and self._remove_at is not None and now >= self._remove_at:
            self.remove_card()
        if self.present is None:
            if self._wait_started is None:
                self._wait_started = now
            elif now - self._wait_started >= self.place_after:
                self.place_card()


def main() -> None:
    parser = argparse.ArgumentParser(description="SN Web Socket and card encoder simulator")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (0.0.0.0 to reach it from Home Assistant)")
    parser.add_argument("--port", type=int, default=8102)
    parser.add_argument("--auto", action="store_true",
                        help="put a blank card down when one is wanted, and lift written ones off")
    args = parser.parse_args()
    sim = EncoderSim(auto=args.auto)
    print(f"SN Web Socket simulator on ws://{args.host}:{args.port}"
          + (" (cards come and go by themselves)" if args.auto else ""))
    web.run_app(sim.app(), host=args.host, port=args.port, access_log=None, print=None)


if __name__ == "__main__":
    main()
