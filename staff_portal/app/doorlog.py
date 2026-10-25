"""Door activity: the lock records TT Hotel pushes to its "Callback URL" (Settings > Integration)
for locks connected to a gateway, and what Staff Access makes of them.

TT Hotel POSTs a form (notifyType=1, lockId, lockMac, records: a JSON array as a string) to
<staff app address>/tthotel/records/<token>, and wants the plain text "success" back, or it sends
the same records again. Nothing here talks to TT Hotel: these are the rules for reading what it
sends (strictly: anything unexpected is dropped, never guessed at), the words the dashboard and
the staff app show for it, and how a card number in a record is matched to a card made here.

Passcodes are never kept: a record's keyboardPwd is a card number only for card records, and is
the passcode itself for passcode records.
"""

import json
import re
import secrets
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from wristbands import format_time, local_today

MAX_RECORDS = 300            # records kept from one delivery (TT Hotel sends a few at a time)
MAX_CARD_NUMBER = 40         # characters, as wb_cards keeps them
MAX_USERNAME = 100
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
EARLIEST_MS = 946684800000   # 2000-01-01: a lock date before this is a lock that lost its clock
LOCK_AHEAD = 300             # seconds a lock's clock may be ahead of TT Hotel's and still be used
LOCK_BEHIND = 30 * 86400     # ...or behind it (a gateway that was offline uploads late)

# Record types from TT Hotel's cloud (its "Record type of cloud" list) worth listing, and how the
# door was opened, in the dashboard's words. Locking, door sensors, parking locks and the like
# are left out: Door activity lists unlocks and the alarms that matter.
HOW = {1: "App", 4: "Passcode", 7: "Card", 8: "Fingerprint", 9: "Wristband", 10: "Key",
       12: "Remote", 32: "From inside", 44: "Tamper alarm", 46: "Key fob",
       48: "Locked after wrong tries", 49: "Card", 50: "Heat alarm", 51: "Deleted card",
       55: "Remote control", 57: "QR code", 58: "QR code", 63: "Passage mode", 67: "Face",
       68: "Face", 71: "Face", 75: "App", 76: "Remote", 84: "Palm", 85: "Palm", 88: "Palm",
       92: "Admin passcode"}
CARD_TYPES = {7, 9, 49, 51}            # keyboardPwd is a card number
REFUSED_TYPES = {51, 58, 68, 71, 88}   # attempts that never open the door, whatever "success" says
ALARM_TYPES = {44, 48}                 # not an unlock: always listed as a problem
DELETED_CARD = 51


def new_token() -> str:
    return secrets.token_urlsafe(32)


def callback_address(public_url: str, token: str) -> str | None:
    """The address to paste into TT Hotel, or None until the staff app's address is set."""
    base = str(public_url or "").rstrip("/")
    return f"{base}/tthotel/records/{token}" if base and token else None


def _whole(value, lo: int, hi: int) -> int | None:
    """An int (or its digits as text, as a form sends them) within lo..hi, else None."""
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,19}", value.strip()):
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        return None
    return value


def _text(value, limit: int) -> str:
    """Printable text without control characters, trimmed to `limit` ('' for anything else)."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return "".join(ch for ch in str(value) if ch.isprintable()).strip()[:limit]


def read_delivery(body: bytes, content_type: str, now: float) -> tuple[int | None, list[dict], int]:
    """A delivery from TT Hotel: (lock ID, the records worth keeping, how many were dropped).
    The lock ID is None when the delivery isn't a lock records notice it can be read as."""
    try:
        if content_type == "application/json":
            fields = json.loads(body.decode("utf-8"))
            if not isinstance(fields, dict):
                return None, [], 0
        else:
            fields = {k: v[0] for k, v in urllib.parse.parse_qs(
                body.decode("utf-8"), max_num_fields=20, strict_parsing=False).items()}
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, [], 0
    lock_id = _whole(fields.get("lockId"), 1, 2**53)
    if _whole(fields.get("notifyType"), 1, 1) is None or lock_id is None:
        return None, [], 0
    raw = fields.get("records")
    try:
        raw = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, RecursionError):
        raw = None
    if not isinstance(raw, list):
        return lock_id, [], 0
    records = [r for r in (read_record(item, now) for item in raw[:MAX_RECORDS]) if r]
    return lock_id, records, len(raw) - len(records)


def read_record(item, now: float) -> dict | None:
    """One record from a delivery, checked field by field, or None to drop it. Returns
    {"record_type", "success", "card_number", "username", "lock_date", "server_date", "at",
    "battery"}: times in seconds, "at" being when it happened (the lock's time, unless its clock
    is clearly off), the card number only for card records."""
    if not isinstance(item, dict):
        return None
    kind = _whole(item.get("recordType"), 0, 10_000)
    success = _whole(item.get("success"), 0, 1)
    latest = int((now + 366 * 86400) * 1000)
    lock_ms = _whole(item.get("lockDate"), EARLIEST_MS, latest)
    server_ms = _whole(item.get("serverDate"), EARLIEST_MS, latest)
    if kind not in HOW or success is None or (lock_ms is None and server_ms is None):
        return None
    lock_date = lock_ms / 1000 if lock_ms is not None else None
    server_date = server_ms / 1000 if server_ms is not None else None
    at = lock_date
    if at is None or (server_date is not None and not
                      server_date - LOCK_BEHIND <= at <= server_date + LOCK_AHEAD):
        at = server_date
    number = ""
    if kind in CARD_TYPES:
        number = _text(item.get("keyboardPwd"), MAX_CARD_NUMBER + 1)
        if len(number) > MAX_CARD_NUMBER:
            number = ""
    return {"record_type": kind, "success": 0 if kind in REFUSED_TYPES else success,
            "card_number": number, "username": _text(item.get("username"), MAX_USERNAME),
            "lock_date": lock_date, "server_date": server_date, "at": at,
            "battery": _whole(item.get("electricQuantity"), 0, 100)}


def card_keys(number: str) -> list[str]:
    """The ways a card made here may be written down for this record's card number, best first,
    as compared without leading zeros (as encoder.py's _same_card compares them): the number
    itself, then with its bytes reversed, since TT Hotel says card readers of different makes
    show the same card in either byte order. Empty for a number that isn't plain digits."""
    if not number.isascii() or not number.isdigit() or not number.lstrip("0"):
        return []
    value = int(number)
    size = max(4, (value.bit_length() + 7) // 8)
    flipped = str(int.from_bytes(value.to_bytes(size, "big"), "little"))
    keys = [number.lstrip("0")]
    if flipped != keys[0] and flipped != "0":
        keys.append(flipped)
    return keys


def alert_for(record: dict, card: dict | None) -> str | None:
    """Why a record should alert someone: "lost" or "revoked" (a card made here in that state,
    tried after it was marked so, not a tap from before that arrived late), "deleted" (TT Hotel
    says a deleted card was tried), else None."""
    if card and card["state"] in ("lost", "revoked") and (card["state_at"] or 0) <= record["at"]:
        return card["state"]
    return "deleted" if record["record_type"] == DELETED_CARD else None


def when_label(at: float, now: float, zone: ZoneInfo) -> str:
    """'today 3:12 PM', 'yesterday 9:05 AM', 'Fri, Oct 17 3:12 PM' (with the year when it isn't
    this year), at the property."""
    when = datetime.fromtimestamp(at, zone)
    today = local_today(now, zone)
    if when.date() == today:
        day = "today"
    elif when.date() == today - timedelta(days=1):
        day = "yesterday"
    elif when.year == today.year:
        day = f"{when:%a}, {when:%b} {when.day}"
    else:
        day = f"{when:%a}, {when:%b} {when.day}, {when.year}"
    return f"{day} {format_time(when)}"


def used_label(door: str, at: float, success: bool, now: float, zone: ZoneInfo | None) -> str:
    """'Pool Gate, today 3:12 PM' (with ', refused' when the door didn't open), or '' without
    the property's time zone (the app then writes the time itself)."""
    if zone is None:
        return ""
    return f"{door}, {when_label(at, now, zone)}{'' if success else ', refused'}"
