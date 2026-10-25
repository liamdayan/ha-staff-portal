"""The card encoder: TT Hotel's "SN Web Socket" helper on the front-desk computer, and the job
runner that makes wristbands and key cards on it.

The add-on talks to the encoder, never the browser: SN Web Socket (a free TT Hotel program on
the computer the USB encoder is plugged into) listens on ws://<computer>:8102 and passes each
request to the encoder's driver. The hotel key (hotelInfo) never reaches a browser.

SN Web Socket answers each request with the request's own fields plus "errorCode" (and "data"),
so a "requestId" sent with a request comes back with its answer; anything else (the greeting
on connect, answers to other requests) is ignored. Its own errors (an unknown function, a
request it couldn't run) come back without the requestId, so those are matched by function.
None of this is documented by the vendor; it's what version 2.5.1 does.

One job at a time per encoder address, oldest first, in a runner task of its own. For each
wristband: wait for a card, clear it (setting up a blank card first), write each record, beep,
then wait for it to be taken off. A card already written in this job never counts as the next
one, a card that failed and was skipped isn't used again, and the last card made (or looked
up) isn't overwritten until it has been lifted off. Problems put the job in "needs attention"
(letting go of the encoder meanwhile) until staff choose Try again, Skip or Stop; a job nobody
answers is stopped after ATTENTION_TIMEOUT so it can't hold up the next one. After every job
the encoder is disconnected (CE_DisconnectComm), so TT Hotel's own Windows program can use it
in between.

Progress is saved through an injected on_update(job_id, job_fields, card_fields) callback (the
database lives on the event loop; this module never imports it), and hotelInfo comes from an
injected hotel_info(fresh=...) coroutine.
"""

import asyncio
import collections
import inspect
import itertools
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger("staff_portal")

POLL_SECONDS = 0.3           # how often the encoder is asked whether a card is on it
CALL_TIMEOUT = 15            # seconds SN Web Socket gets to answer one request
CONNECT_TIMEOUT = 5          # seconds to open the WebSocket to the encoder's computer
ATTENTION_TIMEOUT = 15 * 60  # a job left needing attention this long is stopped (a closed tab
                             # or a sleeping computer would otherwise hold the encoder for good)
PROBE_TTL = 30               # how long "the encoder's computer answered" is trusted
LIVE_HISTORY = 200           # finished jobs whose progress is kept in memory, per encoder
MAX_ENCODERS = 100           # encoder addresses kept at once
MAX_MESSAGE = 1024 * 1024    # largest reply accepted from SN Web Socket
MAX_CARD_NUMBER = 64         # characters; anything longer isn't a card number
DEFAULT_SECTORS = "0000000000011111"   # TT Hotel's default: sectors 12 to 16
NEEDS_INIT = {105, 106, 109}     # clearing failed because the card isn't set up for this hotel
COMM_CODES = {3, 4, 16}          # the USB link failed: reconnect before the next try
HOTEL_INFO_EXPIRED = 13
# Our own codes, outside the driver's (0 to 420) and SN Web Socket's (negative, 1000 and up).
UNREACHABLE = -1000          # SN Web Socket couldn't be reached
NO_REPLY = -1001             # ...or didn't answer in time
BUSY = -1002                 # the encoder is making cards for someone else
UNEXPECTED = -9              # an answer without an errorCode (SN Web Socket's "exception" code)

ERROR_MESSAGES = {
    0: "Success",
    1: "Failed",
    2: "Bad parameter",
    3: "Communication error (write); re-plug the encoder cable",
    4: "Communication error (read); re-plug the encoder cable",
    5: "Command error (the encoder returned an error)",
    6: "Key not configured",
    7: "Not in card-issuing state",
    8: "Failed to interrupt",
    9: "Interrupted",
    10: "Time server address not configured",
    11: "Failed to reach the TTLock server",
    12: "TTLock server returned an error",
    13: "Bad or expired hotelInfo",
    14: "This encoder belongs to a different hotel",
    15: "Encoder has not been set up for this hotel",
    16: "Can't connect to the card encoder",
    21: "Not an IC card",
    26: "Failed to disconnect",
    28: "Failed to configure the COM port",
    31: "Failed to open log file",
    32: "Command not supported by this encoder",
    33: "CPU cards are not supported",
    34: "Sector parse failed",
    35: "Sector out of bounds",
    36: "No available sector",
    37: "Incomplete sector data",
    101: "Card not read; check it is centered on the encoder",
    102: "Timed out",
    104: "Card is full; use floor or building access instead of many doors",
    105: "Card can't be decrypted or is not set up for this hotel",
    106: "Card is blank, belongs to another hotel, or the sector setting doesn't match",
    107: "No such record",
    108: "Verification failed",
    109: "Hotel ID not configured on this card",
    110: "Can't report this card as lost",
    201: "Failed to configure encoder key",
    202: "Failed to configure card key",
    203: "Failed to configure hotel info",
    301: "Could not parse card data",
    304: "Sector memory full",
    305: "Failed to decrypt with key",
    307: "No IC card data",
    420: "Failed to return data",
    -1: "SN Web Socket couldn't read the request",
    -3: "This version of SN Web Socket doesn't know that request; update SN Web Socket",
    -9: "SN Web Socket ran into an error",
    -12: "The encoder is busy",
    1003: "No card encoder found on that computer (check the USB cable and driver)",
    1004: "Couldn't open the card encoder (another program, such as TT Hotel, may be using it)",
    11000: "SN Web Socket couldn't load the card encoder driver; reinstall SN Web Socket",
    UNREACHABLE: "Can't reach SN Web Socket on the encoder's computer",
    NO_REPLY: "SN Web Socket didn't answer in time",
    BUSY: "The card encoder is busy making cards",
}

QUEUED_MESSAGE = "Waiting for the encoder…"
WAITING_MESSAGE = "Waiting for another job to finish…"
CONFIGURE_OFFLINE = ("Couldn't reach TT Hotel to set up the card encoder. Check the internet "
                     "connection. Cards still work if the encoder was set up before.")
CONFIGURE_FAILED = ("The card encoder couldn't be set up for this hotel. Cards still work if it "
                    "was set up before.")
TTHOTEL_NOT_SET_UP = ("The TT Hotel connection needs a manager's attention, so the card encoder "
                      "can't be set up. Cards still work if it was set up before.")
FINISHED = ("done", "cancelled")


class EncoderError(Exception):
    """The encoder (or SN Web Socket) said no. code is the driver's or SN Web Socket's code, or
    one of ours; message is a plain description for admins (staff see failure_message())."""

    def __init__(self, code: int, operation: str, detail: str = ""):
        self.code = code
        self.operation = operation
        self.detail = detail
        super().__init__(f"{operation} failed with code {code}: {self.message}")

    @property
    def message(self) -> str:
        base = ERROR_MESSAGES.get(self.code, f"Card encoder error {self.code}")
        return f"{base} ({self.detail})" if self.detail else base


# ---------------------------------------------------------------- wording

def card_noun(card_type: str, plural: bool = False) -> str:
    """'wristband' for guest jobs, 'card' for staff cards."""
    noun = "card" if card_type == "staff" else "wristband"
    return noun + "s" if plural else noun


def place_message(card_type: str, seq: int, total: int) -> str:
    noun = card_noun(card_type)
    return (f"Place the {noun} on the encoder" if total == 1
            else f"Place {noun} {seq} of {total} on the encoder")


def writing_message(card_type: str, seq: int, total: int) -> str:
    noun = card_noun(card_type)
    return f"Writing the {noun}…" if total == 1 else f"Writing {noun} {seq} of {total}…"


def remove_message(card_type: str, seq: int, total: int) -> str:
    noun = card_noun(card_type)
    return (f"Take the {noun} off the encoder" if total == 1
            else f"Take {noun} {seq} off the encoder")


def summary_message(card_type: str, status: str, cards: list[dict]) -> str:
    """The final message of a done or cancelled job."""
    noun, nouns = card_noun(card_type), card_noun(card_type, plural=True)
    total = len(cards)
    written = sum(1 for c in cards if c["status"] == "written")
    skipped = sum(1 for c in cards if c["status"] == "skipped")
    if status == "cancelled":
        if written == 0:
            return f"Cancelled before any {nouns} were made"
        return f"Cancelled after making {written} of {total} {nouns}"
    if written == total:
        return f"The {noun} is ready" if total == 1 else f"All {total} {nouns} are ready"
    if written == 0:
        return f"No {nouns} were made"
    verb = "is" if written == 1 else "are"
    return f"{written} of {total} {nouns} {verb} ready ({skipped} skipped)"


def failure_message(code: int, card_type: str) -> str:
    """A plain sentence for staff about an encoder problem, with what to do next."""
    noun = card_noun(card_type)
    if code == UNREACHABLE:
        return ("Can't reach the card encoder's computer. Check that it's on and SN Web Socket "
                "is running, then try again.")
    if code == NO_REPLY:
        return ("The card encoder's computer stopped answering. Check that SN Web Socket is "
                "running, then try again.")
    if code in (1003, 16):
        return "The card encoder isn't plugged in. Check the USB cable, then try again."
    if code in (1, 1004, 26, 28):
        return ("Couldn't connect to the card encoder. Close TT Hotel on that computer if it's "
                "open, check the USB cable, then try again.")
    if code in (11000, -3, -1, -9):
        return ("SN Web Socket on the encoder's computer needs attention. Ask a manager to "
                "update or reinstall it.")
    if code in (-12, BUSY):
        return "The card encoder is busy. Wait a moment, then try again."
    if code in (3, 4, 5):
        return "The card encoder stopped responding. Check the USB cable, then try again."
    if code in (101, 102):
        return f"The {noun} couldn't be read. Center it on the encoder, then try again."
    if code in (104, 36, 304):
        return (f"There isn't room on the {noun} for that many doors. Use fewer doors, or floor "
                "or building access instead.")
    if code in (105, 106, 108, 109):
        return f"This {noun} belongs to another hotel or can't be set up. Try a different {noun}."
    if code == HOTEL_INFO_EXPIRED:
        return "The authorization from TT Hotel expired. Try again."
    if code in (10, 11, 12):
        return (f"Couldn't reach TT Hotel to authorize the {noun}. Check the internet "
                "connection, then try again.")
    if code in (6, 14, 15, 201, 202, 203):
        return ("The card encoder isn't set up for this hotel. Ask a manager to check the "
                "encoder, then try again.")
    if code in (21, 33):
        return f"That isn't a supported {noun}. Try a different one."
    if code in (34, 35, 37):
        return (f"The {noun} doesn't match the encoder's sector setting. Try a different {noun}, "
                "or ask a manager to check the sector setting.")
    return f"The {noun} couldn't be written. Try again, or use a different {noun}."


# ---------------------------------------------------------------- SN Web Socket

_request_ids = itertools.count(1)
_SESSION_TAG = secrets.token_hex(4)   # requestIds differ between add-on restarts too


def _reply_code(reply: dict) -> int:
    code = reply.get("errorCode")
    if code is None and isinstance(reply.get("data"), dict):
        code = reply["data"].get("errorCode")   # GetDeviceInfo nests it
    if isinstance(code, bool):
        return UNEXPECTED
    try:
        return int(code)
    except (TypeError, ValueError):
        return UNEXPECTED


def _reason(err: BaseException) -> str:
    if isinstance(err, asyncio.TimeoutError):
        return "timed out"
    if isinstance(err, aiohttp.WSServerHandshakeError):
        return "not SN Web Socket"
    if isinstance(err, aiohttp.ClientConnectorError):
        return "couldn't connect"
    return "connection failed"


class SNWebSocket:
    """One encoder address. The WebSocket opens on the first request and stays open until
    close(); requests go one at a time."""

    def __init__(self, url: str, session: aiohttp.ClientSession, *, timeout: float = CALL_TIMEOUT):
        self.url = url
        self.timeout = timeout
        self._session = session
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._lock = asyncio.Lock()
        self.helper_version: str | None = None   # SN Web Socket's version, from its answers

    @property
    def is_open(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def _open(self) -> aiohttp.ClientWebSocketResponse:
        if self.is_open:
            return self._ws
        try:
            self._ws = await asyncio.wait_for(
                self._session.ws_connect(self.url, timeout=aiohttp.ClientWSTimeout(ws_close=5),
                                         max_msg_size=MAX_MESSAGE), CONNECT_TIMEOUT)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as err:
            raise EncoderError(UNREACHABLE, "connect", _reason(err)) from None
        return self._ws

    async def exchange(self, function: str, **params) -> tuple[int, dict]:
        """Send one request; (errorCode, answer) without judging the code."""
        async with self._lock:
            ws = await self._open()
            request_id = f"sp-{_SESSION_TAG}-{next(_request_ids)}"
            loop = asyncio.get_running_loop()
            try:
                await ws.send_json({"function": function, "requestId": request_id, **params})
                deadline = loop.time() + self.timeout
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    msg = await asyncio.wait_for(ws.receive(), remaining)
                    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                                    aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        raise ConnectionResetError("closed")
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        reply = json.loads(msg.data)
                    except ValueError:
                        continue
                    if not isinstance(reply, dict):
                        continue
                    if reply.get("requestId") == request_id:
                        break
                    if ("requestId" not in reply and reply.get("function") == function
                            and reply.get("errorCode") not in (None, 0)):
                        break   # SN Web Socket's own errors leave out the requestId
            except asyncio.TimeoutError:
                await self._drop()
                raise EncoderError(NO_REPLY, function) from None
            except (aiohttp.ClientError, OSError, RuntimeError) as err:
                await self._drop()
                raise EncoderError(UNREACHABLE, function, _reason(err)) from None
        if isinstance(reply.get("snWebSocketVersion"), str):
            self.helper_version = reply["snWebSocketVersion"][:40]
        return _reply_code(reply), reply

    async def ping(self) -> None:
        """Check that SN Web Socket answers, without touching the encoder."""
        async with self._lock:
            await self._open()

    async def call(self, function: str, **params) -> dict:
        """Send one request and return the answer; EncoderError unless errorCode is 0."""
        code, reply = await self.exchange(function, **params)
        if code != 0:
            raise EncoderError(code, function)
        return reply

    async def connect_encoder(self, port: str = "auto") -> dict:
        """Open the encoder's USB port: "auto" lets SN Web Socket find it (GetDeviceInfo, which
        also reports the encoder's version), else the port named (COM3, /dev/ttyUSB0)."""
        if not port or port == "auto":
            reply = await self.call("GetDeviceInfo", transformCardNo=True)
            data = reply.get("data")
            info = data.get("data") if isinstance(data, dict) else None
            return info if isinstance(info, dict) else {}
        await self.call("CE_ConnectComm", portname=port)
        return {}

    async def disconnect_encoder(self) -> None:
        """Let go of the USB port (so TT Hotel's own program can use the encoder). Never
        raises."""
        try:
            await self.exchange("CE_DisconnectComm")
        except Exception as err:  # noqa: BLE001 - nothing more to do about it
            log.info("Card encoder at %s: disconnecting failed: %s", self.url, err)

    async def card_id(self) -> str | None:
        """The card number on the encoder (CE_GetCardID data.data), or None when there's no
        card. A failed USB link raises, so it can't pass for "no card"."""
        code, reply = await self.exchange("CE_GetCardID")
        if code in COMM_CODES or code < 0 or code >= 1000:
            raise EncoderError(code, "CE_GetCardID")
        if code != 0:
            return None
        data = reply.get("data")
        number = data.get("data") if isinstance(data, dict) else None
        number = str(number).strip() if isinstance(number, (str, int)) else ""
        if not number or len(number) > MAX_CARD_NUMBER or not number.isprintable():
            return None
        return number

    async def configure(self, hotel_info: str, sectors: str) -> None:
        """Bind the encoder to the hotel and set the card sectors."""
        if not re.fullmatch(r"[01]{16}", sectors or ""):
            raise EncoderError(2, "CE_SetSectors", "sectors must be 16 characters of 0 and 1")
        await self.call("CE_InitCardEncoder", hotelInfo=hotel_info)
        await self.call("CE_SetSectors", sectors=sectors)

    async def init_card(self, hotel_info: str) -> None:
        """A blank card becomes this hotel's card."""
        await self.call("CE_InitCard", hotelInfo=hotel_info)

    async def clear_card(self, hotel_info: str) -> None:
        """Remove every record from the card."""
        await self.call("CE_ClearCard", hotelInfo=hotel_info)

    async def write_record(self, hotel_info: str, building: int, floor: int, mac: str,
                           expires_at: int, allow_lockout: bool) -> None:
        """Add one record (records add up on the card). expires_at is unix seconds; the driver
        turns it into lock time with the computer's own clock and time zone."""
        if not (0 <= building <= 254 and 0 <= floor <= 255
                and re.fullmatch(r"[0-9A-Fa-f]{12}", mac or "") and 0 < expires_at <= 0xFFFFFFFF):
            raise EncoderError(2, "CE_WriteCard", "record out of range")
        await self.call("CE_WriteCard", hotelInfo=hotel_info, buildNo=int(building),
                        floorNo=int(floor), mac=mac.upper(), timestamp=int(expires_at),
                        allowLockOut=bool(allow_lockout))

    async def read_card(self, hotel_info: str) -> list[dict]:
        """The records on the card, as the driver reports them."""
        reply = await self.call("CE_ReadCard", hotelInfo=hotel_info)
        return _raw_records(reply.get("data"))

    async def beep(self) -> None:
        await self.call("CE_Beep", voiceLen=120, interval=80, voiceCount=1)

    async def version(self) -> str:
        """The encoder's and SN Web Socket's versions, as one line."""
        reply = await self.call("CE_GetVersion")
        data = reply.get("data")
        if isinstance(data, dict):
            text = ", ".join(f"{k} {v}" for k, v in data.items()
                             if isinstance(v, (str, int, float)) and not isinstance(v, bool))
        else:
            text = str(data or "")
        if self.helper_version:
            text = f"SN Web Socket {self.helper_version}" + (f"; {text}" if text else "")
        return " ".join(text.split())[:200] or "unknown"

    async def _drop(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 - already broken
                pass

    async def close(self) -> None:
        async with self._lock:
            await self._drop()


def _raw_records(data) -> list[dict]:
    if isinstance(data, str):
        try:
            data = json.loads(data.replace("\n", ""))
        except ValueError:
            raise EncoderError(301, "CE_ReadCard", "card data isn't JSON") from None
    if isinstance(data, dict):
        for key in ("hotelArray", "records", "data", "hotelInfo"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        raise EncoderError(301, "CE_ReadCard", "unexpected card data")
    return [item for item in data[:256] if isinstance(item, dict)]


def _pick(record: dict, *names):
    lowered = {str(k).lower(): v for k, v in record.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _seconds(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if number > 10 ** 11:   # milliseconds
        number //= 1000
    return number if 0 < number <= 0xFFFFFFFF else None


def _place(value, hi: int) -> int | None:
    """A building or floor number (a whole number, or digits in a string), else None."""
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= hi else None


def card_records(raw: list[dict]) -> list[dict]:
    """Records read off a card, in one shape: {"level", "building", "floor", "mac",
    "starts_at", "expires_at", "allow_lockout"}. The driver's field names aren't documented, so
    several spellings are accepted and anything unreadable is None."""
    found = []
    for record in raw:
        building = _place(_pick(record, "buildNo", "buildingNo", "buildingNumber", "building"),
                          254)
        floor = _place(_pick(record, "floorNo", "floorNumber", "floor"), 255)
        mac = re.sub(r"[^0-9A-Fa-f]", "", str(_pick(record, "mac", "lockMac") or "")).upper()
        if len(mac) != 12:
            mac = ""
        if mac and mac != "000000000000":
            level = "door"
        elif building == 0 and floor in (0, None):
            level = "all"
        elif floor == 0:
            level = "building"
        elif floor is not None:
            level = "floor"
        else:
            level = None
        lockout = _pick(record, "allowLockOut", "allowLockout")
        found.append({"level": level, "building": building, "floor": floor, "mac": mac or None,
                      "starts_at": _seconds(_pick(record, "startDate", "startTime", "start")),
                      "expires_at": _seconds(_pick(record, "endDate", "endTime", "expireTime",
                                                   "timestamp", "end")),
                      "allow_lockout": bool(lockout) if lockout is not None else None})
    return found


# ---------------------------------------------------------------- job runner

class _Cancelled(Exception):
    """The job was stopped."""


class _Failure(Exception):
    """A card attempt failed; message is for staff."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class _Job:
    id: int
    type: str
    records: list[tuple[str, int, int, str]]   # (level, building, floor, mac)
    expires_at: int
    allow_lockout: bool
    port: str
    sectors: str
    card_wait: float
    cards: list[dict]
    status: str = "queued"
    phase: str = "queued"
    current: int | None = None
    message: str = QUEUED_MESSAGE
    cancel: bool = False
    decision: str | None = None
    awaiting: bool = False
    attempt_card: str | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)

    @classmethod
    def from_row(cls, row: dict, port, sectors, card_wait) -> "_Job":
        """A wb_jobs row (or the same keys). Raises ValueError when it can't be written."""
        job_id = row.get("id")
        card_type = row.get("type", row.get("card_type"))
        count = row.get("count")
        records = row.get("records")
        if isinstance(records, str):
            records = json.loads(records)
        if (not isinstance(job_id, int) or card_type not in ("day_pass", "overnight", "staff")
                or not isinstance(count, int) or not 1 <= count <= 100
                or not isinstance(records, list) or not 1 <= len(records) <= 64):
            raise ValueError("not a card job")
        parsed = []
        for r in records:
            get = r.get if isinstance(r, dict) else (lambda k, r=r: getattr(r, k, None))
            level, building, floor = get("level"), get("building"), get("floor")
            mac = str(get("mac") or "").upper()
            if (level not in ("door", "floor", "building", "all") or not isinstance(building, int)
                    or not isinstance(floor, int) or not 0 <= building <= 254
                    or not 0 <= floor <= 255 or not re.fullmatch(r"[0-9A-F]{12}", mac)):
                raise ValueError("a record can't be written")
            parsed.append((level, building, floor, mac))
        expires_at = int(row.get("expires_at") or 0)
        if not 0 < expires_at <= 0xFFFFFFFF:
            raise ValueError("bad expiry")
        if not re.fullmatch(r"[01]{16}", sectors or ""):
            sectors = DEFAULT_SECTORS
        cards = [{"seq": seq, "status": "pending", "card_number": None, "error": None}
                 for seq in range(1, count + 1)]
        for given in row.get("cards") or []:   # a job picked up again keeps what's done
            if (isinstance(given, dict) and isinstance(given.get("seq"), int)
                    and 1 <= given["seq"] <= count
                    and given.get("status") in ("written", "skipped", "pending")):
                cards[given["seq"] - 1].update(status=given["status"],
                                               card_number=given.get("card_number"))
        return cls(id=job_id, type=card_type, records=parsed, expires_at=expires_at,
                   allow_lockout=bool(row.get("allow_lockout")), port=str(port or "auto"),
                   sectors=sectors, card_wait=_seconds_between(card_wait, 0.1, 600),
                   cards=cards)

    @property
    def networked(self) -> bool:
        """Floor, building and all-door records need a fresh hotelInfo (and the internet)."""
        return any(level != "door" for level, *_ in self.records)


_LOOKUP = "lookup"   # the queue's marker for a look-up


def _seconds_between(value, lo: float, hi: float, default: float = 60.0) -> float:
    """A wait from the settings, kept sane (the settings form already checks it)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return default
    return float(min(max(value, lo), hi))


class EncoderRunner:
    """The card jobs for one encoder address, one at a time, oldest first.

    hotel_info(fresh=bool) is a coroutine returning the encoder's hotel key (raising when TT
    Hotel can't give one; an exception with needs_admin=True means only a manager can fix it).
    on_update(job_id, job_fields, card_fields) saves progress: job_fields are wb_jobs columns
    (status, message, finished_at), card_fields is None or {"seq", plus wb_cards columns:
    status, card_number, error, written_at}. It's a plain function, called on the event loop.
    hotel_info_expired() (optional) drops a cached hotelInfo the encoder called expired.
    lingering: the last card made on this encoder before the runner started (after a restart),
    so it isn't overwritten unseen if it's still on the encoder."""

    def __init__(self, url: str, session: aiohttp.ClientSession, hotel_info, on_update, *,
                 poll: float = POLL_SECONDS, timeout: float = CALL_TIMEOUT,
                 attention_timeout: float = ATTENTION_TIMEOUT, hotel_info_expired=None,
                 lingering: str | None = None):
        self.url = url
        self._session = session
        self._hotel_info = hotel_info
        self._on_update = on_update
        self._expired = hotel_info_expired or getattr(
            getattr(hotel_info, "__self__", None), "hotel_info_expired", None)
        self._poll = poll
        self._timeout = timeout
        self._attention = attention_timeout
        self._lock = asyncio.Lock()       # one job, look-up or test on this encoder at a time
        self._queue: collections.deque = collections.deque()
        self._jobs: collections.OrderedDict[int, _Job] = collections.OrderedDict()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._probe_task: asyncio.Task | None = None
        self._pending_saves: set[asyncio.Task] = set()
        self._current: _Job | None = None
        self._socket: SNWebSocket | None = None
        self._encoder_connected = False
        self._needs_reconnect = False
        self._lingering = lingering          # last card made or read, until it's seen off
        self._lingering_verb = "made"
        self._lookup: dict | None = None    # the latest look-up; "_stop" asks it to end
        self._lookup_args: tuple[str, float] = ("auto", 60.0)
        self._lookup_wake = asyncio.Event()
        self._closed = False
        self.connected = False               # the encoder answered the last time it was used
        self.reachable: bool | None = None   # SN Web Socket answered the last check
        self.error: str | None = None        # the last problem, for staff
        self.checked_at: float | None = None

    # ------------------------------------------------------------ public

    def submit(self, job: dict, *, port: str | None = None, sectors: str = DEFAULT_SECTORS,
               card_wait: float = 60) -> None:
        """Queue a card job: a wb_jobs row ("id", "type" or "card_type", "count", "records"
        (JSON text or list), "expires_at", "allow_lockout"; "cards" optional). port is the
        device's encoder_port (None = auto), sectors wb_sectors, card_wait wb_card_wait.
        Returns at once; raises ValueError for a job that can't be written."""
        parsed = _Job.from_row(job, port, sectors, card_wait)
        old = self._jobs.get(parsed.id)
        if old is not None and old.status not in FINISHED:
            return
        self._jobs[parsed.id] = parsed
        self._jobs.move_to_end(parsed.id)
        self._trim()
        ahead = self._current is not None or bool(self._queue) or self._lock.locked()
        self._queue.append(parsed.id)
        parsed.message = WAITING_MESSAGE if ahead else QUEUED_MESSAGE
        self._start_worker()

    def cancel(self, job_id: int) -> bool:
        """Stop a job: at once if it's waiting its turn, else within one poll. A card being
        written is finished first. False if this encoder has no such active job."""
        job = self._jobs.get(job_id)
        if job is None or job.status in FINISHED:
            return False
        job.cancel = True
        job.wake.set()
        if job is not self._current and job_id in self._queue:
            self._queue.remove(job_id)
            self._finish(job, "cancelled")
        return True

    def retry(self, job_id: int) -> bool:
        """Try the failed card again (only while the job needs attention)."""
        return self._decide(job_id, "retry")

    def skip(self, job_id: int) -> bool:
        """Give up on the failed card and go on with the next one."""
        return self._decide(job_id, "skip")

    def live(self, job_id: int) -> dict | None:
        """{"status", "phase", "current", "message", "cards": [{"seq", "status",
        "card_number", "error"}]} while this encoder knows the job, else None."""
        job = self._jobs.get(job_id)
        if job is None:
            return None
        return {"status": job.status, "phase": job.phase, "current": job.current,
                "message": job.message, "cards": [dict(c) for c in job.cards]}

    def status(self) -> dict:
        """{"connected", "busy", "error", "checked_at"} for the Wristbands screen and the
        dashboard. Checks in the background that SN Web Socket still answers, at most every
        PROBE_TTL seconds and never while a job runs."""
        busy = self._busy()
        if not busy:
            self._start_probe()
        return {"connected": bool(self.connected or (self.reachable and not self.error)),
                "busy": busy, "error": self.error, "checked_at": self.checked_at}

    def start_lookup(self, *, port: str | None = None, card_wait: float = 60) -> None:
        """Read the card that will be put on the encoder (after any job ahead of it)."""
        if self._lookup and self._lookup["state"] == "waiting":
            self._lookup["_stop"] = True   # a new look-up replaces one not finished yet
            self._lookup_wake.set()
        self._lookup_args = (str(port or "auto"), _seconds_between(card_wait, 0.1, 600))
        self._lookup = {"state": "waiting", "card_number": None, "card": [], "raw": [],
                        "message": WAITING_MESSAGE if self._busy() else QUEUED_MESSAGE,
                        "started_at": time.time(), "_stop": False}
        if _LOOKUP not in self._queue:
            self._queue.append(_LOOKUP)
        self._start_worker()

    def lookup_state(self) -> dict | None:
        """{"state": "waiting"|"done"|"error", "card_number", "card" (card_records()),
        "message"}, or None before any look-up."""
        if self._lookup is None:
            return None
        return {k: (list(v) if isinstance(v, list) else v) for k, v in self._lookup.items()
                if k != "raw" and not k.startswith("_")}

    def cancel_lookup(self) -> None:
        if self._lookup and self._lookup["state"] == "waiting":
            self._lookup["_stop"] = True
            self._lookup_wake.set()
            if _LOOKUP in self._queue:
                self._queue.remove(_LOOKUP)
                self._lookup.update(state="error", message="Look-up stopped.")

    async def test(self, port: str | None = None) -> str:
        """Connect, read the versions and let go again, for the dashboard's Test encoder.
        Raises EncoderError (BUSY while cards are being made)."""
        if self._busy():
            raise EncoderError(BUSY, "test")
        async with self._lock:
            socket = self._ws()
            try:
                await socket.connect_encoder(str(port or "auto"))
                self._encoder_connected = True
                version = await socket.version()
            except EncoderError as err:
                self._note(err)
                raise
            finally:
                await self._release()
            self._set_status(True, None)
            return version

    async def close(self) -> None:
        """Stop the runner (a job in progress is cancelled) and let go of the encoder."""
        self._closed = True
        for job in list(self._jobs.values()):
            if job.status not in FINISHED:
                job.cancel = True
                job.wake.set()
        self._wake.set()
        for task in (self._task, self._probe_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:  # noqa: BLE001 - it's stopping
                    pass
        for job in list(self._jobs.values()):
            if job.status not in FINISHED:
                self._finish(job, "cancelled")
        await self._release()

    # ------------------------------------------------------------ bookkeeping

    def _busy(self) -> bool:
        return self._current is not None or bool(self._queue) or self._lock.locked()

    def _trim(self) -> None:
        finished = [i for i, j in self._jobs.items() if j.status in FINISHED]
        for job_id in finished[:max(0, len(self._jobs) - LIVE_HISTORY)]:
            del self._jobs[job_id]

    def _decide(self, job_id: int, decision: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or not job.awaiting or job.cancel:
            return False
        job.decision = decision
        job.wake.set()
        return True

    def _save(self, job: _Job, job_fields: dict, card_fields: dict | None = None) -> None:
        try:
            result = self._on_update(job.id, job_fields, card_fields)
            if inspect.isawaitable(result):
                task = asyncio.ensure_future(result)
                self._pending_saves.add(task)
                task.add_done_callback(self._pending_saves.discard)
        except Exception as err:  # noqa: BLE001 - a failed save mustn't stop the encoder
            log.warning("Couldn't save card job %s: %r", job.id, err)

    def _set_card(self, job: _Job, seq: int, **fields) -> None:
        job.cards[seq - 1].update({k: v for k, v in fields.items() if k != "written_at"})
        self._save(job, {}, {"seq": seq, **fields})

    def _set_live(self, job: _Job, phase: str, seq: int | None, message: str) -> None:
        job.phase, job.current, job.message = phase, seq, message

    def _set_status(self, connected: bool, error: str | None) -> None:
        self.connected = connected
        self.error = error
        self.checked_at = time.time()
        if connected:
            self.reachable = True

    def _note(self, err: EncoderError, card_type: str = "day_pass") -> None:
        """What an encoder problem means for the next attempt and for the status."""
        if err.code in COMM_CODES or err.code in (UNREACHABLE, NO_REPLY):
            self._needs_reconnect = True
        if err.code in (UNREACHABLE, NO_REPLY):
            self.reachable = False
        elif err.code == HOTEL_INFO_EXPIRED and self._expired is not None:
            try:
                self._expired()
            except Exception as exc:  # noqa: BLE001 - only a cache
                log.warning("Couldn't drop the expired hotel key: %r", exc)
        if err.code in COMM_CODES or err.code in (UNREACHABLE, NO_REPLY, 1, 1003, 1004, 11000):
            self._set_status(False, failure_message(err.code, card_type))

    def _ws(self) -> SNWebSocket:
        if self._socket is None:
            self._socket = SNWebSocket(self.url, self._session, timeout=self._timeout)
        return self._socket

    async def _release(self) -> None:
        """After every job: let go of the USB port and close the WebSocket."""
        socket = self._socket
        if socket is None:
            return
        if self._encoder_connected and (socket.is_open or self.reachable is not False):
            await socket.disconnect_encoder()
        self._encoder_connected = False
        self._needs_reconnect = False
        await socket.close()

    def _start_worker(self) -> None:
        if self._closed:
            return
        self._wake.set()
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._work())

    def _start_probe(self) -> None:
        if self._closed or (self._probe_task and not self._probe_task.done()):
            return
        if self.checked_at and time.time() - self.checked_at < PROBE_TTL:
            return
        try:
            self._probe_task = asyncio.get_running_loop().create_task(self._probe())
        except RuntimeError:   # no event loop: nothing to check from
            pass

    async def _probe(self) -> None:
        """Does SN Web Socket answer? (Only the helper; the encoder's USB port is left alone.)"""
        self.checked_at = time.time()
        socket = SNWebSocket(self.url, self._session, timeout=CONNECT_TIMEOUT)
        try:
            await socket.ping()
        except EncoderError as err:
            if not self._busy():
                self.reachable = False
                self.connected = False
                self.error = failure_message(err.code, "day_pass")
            return
        except Exception as err:  # noqa: BLE001 - background tasks never raise
            log.warning("Checking the card encoder at %s failed: %r", self.url, err)
            return
        finally:
            await socket.close()
        self.reachable = True
        if self.error == failure_message(UNREACHABLE, "day_pass"):
            self.error = None

    async def _work(self) -> None:
        """The runner task: jobs and look-ups in order, forever. Never raises."""
        while not self._closed:
            if not self._queue:
                self._wake.clear()
                await self._wake.wait()
                continue
            item = self._queue.popleft()
            async with self._lock:
                try:
                    if item == _LOOKUP:
                        await self._run_lookup()
                    else:
                        job = self._jobs.get(item)
                        if job is not None and job.status == "queued":
                            self._current = job
                            await self._run_job(job)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - the runner must keep going
                    log.exception("Card encoder at %s: a job stopped unexpectedly", self.url)
                    job = self._current
                    if job is not None and job.status not in FINISHED:
                        self._finish(job, "cancelled", "Something went wrong, so this job was "
                                                       "stopped. Start it again.")
                finally:
                    self._current = None
                    try:
                        await self._release()
                    except Exception as err:  # noqa: BLE001
                        log.warning("Card encoder at %s: letting go failed: %r", self.url, err)

    # ------------------------------------------------------------ one job

    async def _run_job(self, job: _Job) -> None:
        if job.cancel:
            self._finish(job, "cancelled")
            return
        job.status = "running"
        self._save(job, {"status": "running", "message": None})
        excluded: set[str] = set()   # cards that failed and were skipped, for the rest of the job
        first = True
        noun = card_noun(job.type)
        while True:
            pending = [c["seq"] for c in job.cards if c["status"] == "pending"]
            # Checked before the cancel flag: a Stop that lands while the last card is being
            # written mustn't turn a finished job into a cancelled one.
            if not pending:
                self._finish(job, "done")
                return
            if job.cancel or self._closed:
                self._finish(job, "cancelled")
                return
            seq = pending[0]
            self._set_live(job, "waiting_for_card", seq,
                           place_message(job.type, seq, len(job.cards)))
            job.attempt_card = None
            try:
                await self._prepare(job, first)
                first = False
                await self._make_card(job, seq, len(pending) == 1, excluded)
                continue
            except _Cancelled:
                self._finish(job, "cancelled")
                return
            except _Failure as exc:
                failure = exc
            except EncoderError as exc:
                log.warning("Card job %s, %s %s: %s", job.id, noun, seq, exc)
                self._note(exc, job.type)
                failure = _Failure(failure_message(exc.code, job.type), exc.code)
            except Exception:  # noqa: BLE001 - shown as a failed card, never a crash
                log.exception("Card job %s, %s %s failed unexpectedly", job.id, noun, seq)
                failure = _Failure(f"Something went wrong while making this {noun}. Try again.")
            decision = await self._needs_attention(job, seq, failure)
            if decision in ("cancel", "unattended"):
                if decision == "unattended":
                    log.info("Card job %s waited too long for a decision; stopping it", job.id)
                self._finish(job, "cancelled")
                return
            if decision == "retry":
                self._set_card(job, seq, status="pending", error=None)
            else:
                self._set_card(job, seq, status="skipped")
                if job.attempt_card:
                    excluded.add(job.attempt_card)
            job.status = "running"
            self._save(job, {"status": "running", "message": None})

    async def _prepare(self, job: _Job, first: bool) -> None:
        """Before each card: connect if needed; set the encoder up once per job (and after a
        reconnect)."""
        reconnected = False
        if self._needs_reconnect or not self._encoder_connected:
            socket = self._ws()
            if self._encoder_connected:
                await socket.disconnect_encoder()
            self._encoder_connected = False
            self._needs_reconnect = False
            try:
                await socket.connect_encoder(job.port)
            except EncoderError as err:
                log.warning("Card encoder at %s: connecting failed: %s", self.url, err)
                self._note(err, job.type)
                message = failure_message(err.code, job.type)
                self._set_status(False, message)
                raise _Failure(message, err.code) from None
            self._encoder_connected = True
            self._set_status(True, None)
            reconnected = True
        if first or reconnected:
            await self._configure(job)

    async def _configure(self, job: _Job) -> None:
        """Bind the encoder to the hotel and set the sectors. A failure only leaves a warning:
        the encoder keeps what it was set up with before."""
        try:
            hotel_info = await self._hotel_info(fresh=True)
        except Exception as err:  # noqa: BLE001 - TT Hotel down, or not set up
            log.warning("Card encoder at %s: no hotel key to set it up: %s", self.url, err)
            self.error = (TTHOTEL_NOT_SET_UP if getattr(err, "needs_admin", False)
                          else CONFIGURE_OFFLINE)
            return
        try:
            await self._ws().configure(hotel_info, job.sectors)
        except EncoderError as err:
            log.warning("Card encoder at %s: setting it up failed: %s", self.url, err)
            self._note(err, job.type)
            if err.code not in COMM_CODES:
                self.error = CONFIGURE_FAILED
            return
        self.error = None

    async def _make_card(self, job: _Job, seq: int, last: bool, excluded: set[str]) -> None:
        written = {c["card_number"]: c["seq"] for c in job.cards
                   if c["status"] == "written" and c["card_number"]}
        number = await self._wait_for_card(job, seq, written, excluded)
        job.attempt_card = number
        self._set_live(job, "writing", seq, writing_message(job.type, seq, len(job.cards)))
        await self._write_card(job)
        self._set_card(job, seq, status="written", card_number=number, error=None,
                       written_at=time.time())
        self._lingering, self._lingering_verb = number, "made"
        if not last:
            await self._wait_for_removal(job, seq, number)

    async def _sleep(self, job: _Job) -> None:
        """One poll interval, cut short by Stop or a decision."""
        if job.cancel:
            return
        try:
            await asyncio.wait_for(job.wake.wait(), self._poll)
        except asyncio.TimeoutError:
            pass
        job.wake.clear()

    async def _wait_for_card(self, job: _Job, seq: int, written: dict[str, int],
                             excluded: set[str]) -> str:
        """Poll until a card that may be written is on the encoder."""
        noun = card_noun(job.type)
        total = len(job.cards)
        prompt = place_message(job.type, seq, total)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + job.card_wait
        seen_card = False
        while True:
            if job.cancel or self._closed:
                raise _Cancelled()
            number = await self._ws().card_id()
            seen_card = seen_card or number is not None
            if number is None or number != self._lingering:
                self._lingering = None
            if number is None:
                blocked = None
            elif number in written:
                blocked = remove_message(job.type, written[number], total)
            elif number in excluded:
                blocked = f"Take that {noun} off the encoder and use a new one"
            elif number == self._lingering:
                # Still on the encoder from before: never overwrite it unseen.
                blocked = f"Take the {noun} you just {self._lingering_verb} off the encoder"
            else:
                return number
            self._set_live(job, "waiting_for_card", seq, blocked or prompt)
            if loop.time() >= deadline:
                if not seen_card:
                    # The driver may report a USB port that died (cable pulled and plugged
                    # back in) as plain "no card", so reconnect before the next attempt.
                    self._needs_reconnect = True
                raise _Failure(f"{blocked}, then try again." if blocked else f"No {noun} detected")
            await self._sleep(job)

    async def _write_card(self, job: _Job) -> None:
        socket = self._ws()
        networked = job.networked
        hotel_info = await self._get_hotel_info(job, networked, "access" if networked else "write")
        try:
            await socket.clear_card(hotel_info)
        except EncoderError as err:
            if err.code not in NEEDS_INIT:
                raise
            log.info("Card isn't set up for this hotel yet (code %s); setting it up", err.code)
            if not networked:
                hotel_info = await self._get_hotel_info(job, True, "init")
            await socket.init_card(hotel_info)
            await socket.clear_card(hotel_info)
        for _, building, floor, mac in job.records:
            await socket.write_record(hotel_info, building, floor, mac, job.expires_at,
                                      job.allow_lockout)
        try:
            await socket.beep()
        except EncoderError as err:   # the card is already written
            log.info("Card encoder at %s: beep failed: %s", self.url, err)

    async def _get_hotel_info(self, job: _Job, fresh: bool, purpose: str) -> str:
        try:
            return await self._hotel_info(fresh=fresh)
        except Exception as err:  # noqa: BLE001 - TTHotelError, or anything from the network
            log.warning("No hotel key for card job %s (%s): %s", job.id, purpose, err)
            noun = card_noun(job.type)
            if getattr(err, "needs_admin", False):
                message = (f"{noun.capitalize()}s can't be made until a manager fixes the TT "
                           "Hotel connection in Staff Access. Ask a manager.")
            elif purpose == "init":
                message = (f"This {noun} is blank, and setting it up needs the internet. Check "
                           f"the connection, or use a {noun} that has been used here before.")
            elif purpose == "access":
                message = (f"{noun.capitalize()}s with floor, building or all-door access need "
                           "the internet. Check the connection, then try again.")
            else:
                message = (f"Couldn't reach TT Hotel to write the {noun}. Check the internet "
                           "connection, then try again.")
            raise _Failure(message) from None

    async def _wait_for_removal(self, job: _Job, seq: int, number: str) -> None:
        """Poll until the written card is lifted off (or swapped for another).

        After card_wait the job moves on anyway: the next card's wait keeps asking for this
        one to be taken off and then needs attention, so a card left on the encoder can't keep
        the job running forever."""
        self._set_live(job, "remove_card", seq, remove_message(job.type, seq, len(job.cards)))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + job.card_wait
        while True:
            if job.cancel or self._closed:
                raise _Cancelled()
            try:
                current = await self._ws().card_id()
            except EncoderError as err:
                log.info("Card encoder at %s: reading failed while waiting for removal: %s",
                         self.url, err)
                return   # the next wait for a card reports it properly
            if current is None or current != number or loop.time() >= deadline:
                return
            await self._sleep(job)

    async def _needs_attention(self, job: _Job, seq: int, failure: _Failure) -> str:
        """Record the failure and wait for staff to choose "retry", "skip" or "cancel".
        "unattended" when nobody chooses within ATTENTION_TIMEOUT."""
        job.decision = None
        job.awaiting = True
        job.status = "needs_attention"
        self._set_live(job, "needs_attention", seq, failure.message)
        job.cards[seq - 1].update(status="failed", error=failure.message)
        self._save(job, {"status": "needs_attention", "message": failure.message},
                   {"seq": seq, "status": "failed", "error": failure.message})
        # Waiting may take a while: let go of the encoder meanwhile (Try again connects afresh,
        # which also picks up a USB cable that was plugged back in).
        await self._release()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._attention
        try:
            while True:
                job.wake.clear()
                if job.cancel or self._closed:
                    return "cancel"
                if job.decision:
                    decision, job.decision = job.decision, None
                    return decision
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return "unattended"
                try:
                    await asyncio.wait_for(job.wake.wait(), min(remaining, 5))
                except asyncio.TimeoutError:
                    pass
        finally:
            job.awaiting = False

    def _finish(self, job: _Job, status: str, message: str | None = None) -> None:
        """Record a job's final status (cards not made become cancelled)."""
        if status == "cancelled":
            for card in job.cards:
                if card["status"] in ("pending", "failed"):
                    self._set_card(job, card["seq"], status="cancelled")
        job.status = status
        job.awaiting = False
        message = message or summary_message(job.type, status, job.cards)
        self._set_live(job, status, None, message)
        self._save(job, {"status": status, "message": message, "finished_at": time.time()})

    # ------------------------------------------------------------ look-up

    async def _run_lookup(self) -> None:
        lookup = self._lookup
        if lookup is None or lookup["state"] != "waiting":
            return
        try:
            await self._read_lookup(lookup)
        except Exception:  # noqa: BLE001 - shown as a failed look-up, never a crash
            log.exception("Card encoder at %s: a look-up failed unexpectedly", self.url)
            lookup.update(state="error", message="Something went wrong while reading the "
                                                 "wristband. Try again.")

    async def _read_lookup(self, lookup: dict) -> None:
        port, card_wait = self._lookup_args
        lookup["message"] = "Place the wristband on the encoder"
        socket = self._ws()
        try:
            await socket.connect_encoder(port)
            self._encoder_connected = True
            self._set_status(True, None)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + card_wait
            while True:
                if lookup["_stop"] or self._closed:
                    lookup.update(state="error", message="Look-up stopped.")
                    return
                number = await socket.card_id()
                if number:
                    break
                if loop.time() >= deadline:
                    lookup.update(state="error", message="No wristband detected. Place it on the "
                                                         "encoder, then try again.")
                    return
                self._lookup_wake.clear()
                try:
                    await asyncio.wait_for(self._lookup_wake.wait(), self._poll)
                except asyncio.TimeoutError:
                    pass
            lookup["card_number"] = number
            # A card someone just looked up is fine to write next: they chose to put it there, and
            # looking one up and then reusing it is common. (Only a card just made is held back.)
            self._lingering = None
            message = None
            try:
                hotel_info = await self._hotel_info(fresh=False)
                raw = await socket.read_card(hotel_info)
                lookup.update(raw=raw, card=card_records(raw))
            except EncoderError as err:
                if err.code in NEEDS_INIT:
                    message = ("This wristband is blank or belongs to another hotel, so there "
                               "are no doors on it.")
                else:
                    message = ("The doors on this wristband couldn't be read. Center it on the "
                               "encoder, then try again.")
            except Exception:  # noqa: BLE001 - TT Hotel unreachable: the number is still useful
                message = ("Couldn't reach TT Hotel to read the doors on this wristband. Check "
                           "the internet connection.")
            try:
                await socket.beep()
            except EncoderError:
                pass
            lookup.update(state="done", message=message)
        except EncoderError as err:
            self._note(err)
            lookup.update(state="error", message=failure_message(err.code, "day_pass"))


class Encoders:
    """Every encoder address in use: get(url) gives its runner. Call from the event loop.
    last_card(url) (optional) gives the last card recently made on that encoder, or None."""

    def __init__(self, hotel_info, on_update, *, poll: float = POLL_SECONDS,
                 timeout: float = CALL_TIMEOUT, attention_timeout: float = ATTENTION_TIMEOUT,
                 hotel_info_expired=None, last_card=None):
        self._hotel_info = hotel_info
        self._on_update = on_update
        self._last_card = last_card
        self._options = {"poll": poll, "timeout": timeout, "attention_timeout": attention_timeout,
                         "hotel_info_expired": hotel_info_expired}
        self._runners: dict[str, EncoderRunner] = {}
        self._session: aiohttp.ClientSession | None = None
        self._closing: set[asyncio.Task] = set()

    def get(self, url: str) -> EncoderRunner:
        runner = self._runners.get(url)
        if runner is not None:
            return runner
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=CONNECT_TIMEOUT,
                                              sock_connect=CONNECT_TIMEOUT))
        if len(self._runners) >= MAX_ENCODERS:   # forget an idle one nobody has used lately
            for old_url, old in list(self._runners.items()):
                if not old._busy():
                    del self._runners[old_url]
                    task = asyncio.get_running_loop().create_task(old.close())
                    self._closing.add(task)
                    task.add_done_callback(self._closing.discard)
                    break
        try:
            lingering = self._last_card(url) if self._last_card else None
        except Exception as err:  # noqa: BLE001 - unknown just means nothing lingering
            log.warning("Couldn't read the last card made on an encoder: %r", err)
            lingering = None
        runner = EncoderRunner(url, self._session, self._hotel_info, self._on_update,
                               lingering=lingering, **self._options)
        self._runners[url] = runner
        return runner

    def find(self, url: str | None) -> EncoderRunner | None:
        """The runner for an address, without starting one."""
        return self._runners.get(url) if url else None

    def status(self, url: str | None) -> dict:
        """{"connected", "busy", "error", "checked_at"} for an address."""
        if not url:
            return {"connected": False, "busy": False, "error": None, "checked_at": None}
        return self.get(url).status()

    def live(self, url: str | None, job_id: int) -> dict | None:
        runner = self.find(url)
        return runner.live(job_id) if runner else None

    async def close_all(self) -> None:
        runners, self._runners = list(self._runners.values()), {}
        for runner in runners:
            try:
                await runner.close()
            except Exception as err:  # noqa: BLE001 - shutting down anyway
                log.warning("Closing the card encoder at %s failed: %r", runner.url, err)
        if self._session is not None:
            await self._session.close()
