"""TT Hotel: the property's door list and the card encoder's hotel key, from TT Hotel's cloud
API (the TTLock Open Platform).

Wristbands need two things from TT Hotel (and a third, only when an admin asks for it):

- The door list (GET /v3/lock/listByHotel, with an access token from the integration's username
  and password). Synced in the background every few hours while wristbands are in use and kept
  in the database (through the on_doors callback), so a restart costs nothing: the next sync is
  timed from the last one saved (the last_sync callback).
- hotelInfo (GET /v3/hotel/getInfo, client ID and secret only): the key the card encoder needs
  to write cards. TT Hotel issues it for 10 minutes; it's reused for HOTEL_INFO_TTL.

The details come from TT Hotel (Windows) > Settings > Integration. The password there is already
MD5: it's sent as it is (in lower case, as the API asks) and never stored any other way.

TTLock's free plan allows 30,000 API calls a month, every call counts, and going over gets calls
refused (and the account emailed), so this client asks as little as it can:

- Hard caps of its own: MINUTE_CAP calls a minute and MONTHLY_SHARE of the monthly quota,
  counted across restarts. Background door syncs stop at SYNC_SHARE of that, keeping the rest
  for making wristbands.
- The access token lasts 90 days. It's saved (with a fingerprint of the credentials it belongs
  to, never the credentials), renewed a week before it runs out, and asked for at most once a
  minute.
- hotelInfo is cached, and a failed fetch is remembered for INFO_RETRY_GAP seconds, so a burst
  of wristbands can't become a burst of requests. Cards that only open single doors may use an
  older hotelInfo when TT Hotel can't be reached.
- Remote lock-out of a lost card (experimental, doors on a gateway only, on an admin's say-so):
  the lost card's number is enrolled on the lock through TT Hotel with dates a year ahead (so it
  can't open anything meanwhile), then deleted again, which makes the lock refuse that number for
  good. Two calls a door, both through the caps; a busy or offline gateway is reported as worth
  trying again later, never retried here.
- TT Hotel's quota errors are never retried: 30007 (monthly limit) pauses everything until next
  month (UTC), 30006 for an hour. Pauses and stops are saved with the request count, so a
  restart doesn't ask again.
- Rejected credentials stop the requests that need them until the TT Hotel settings are saved
  again or an admin tests the connection. A bad client ID or secret stops everything; a bad
  username or password stops logins and the door list but not hotelInfo, so wristbands keep
  working with the doors already synced.
- A failed background sync is retried later and later (doubling, up to a day).

TT Hotel reports errors in the body ("errcode") even with HTTP 200, so every reply is checked.
Request URLs carry the client secret and access token, so they're never logged or shown.
"""

import asyncio
import calendar
import collections
import hashlib
import inspect
import logging
import os
import re
import time

import aiohttp

log = logging.getLogger("staff_portal")

REGIONS = {"global": "https://euapi.ttlock.com", "china": "https://cnapi.ttlock.com"}
API_OVERRIDE = os.environ.get("TTHOTEL_API", "").rstrip("/")   # tests / simulator

MONTHLY_QUOTA = 30_000       # TTLock's free tier; every call counts
MONTHLY_SHARE = 0.25         # our hard cap: 7,500 calls a month, counted across restarts
SYNC_SHARE = 0.5             # background door syncs stop at half the cap; the rest is kept for
                             # card-making
MINUTE_CAP = 30              # calls a minute, at most
HOTEL_INFO_TTL = 8 * 60      # hotelInfo is valid 10 minutes; refresh with a margin
INFO_RETRY_GAP = 30          # a failed hotelInfo fetch is remembered this long
TOKEN_MARGIN = 7 * 86400     # refresh the 90-day token a week early
TOKEN_GAP = 60               # at most one token request a minute
TOKEN_LIFETIME = 90 * 86400  # when TT Hotel doesn't say how long a token lasts
DEFAULT_SYNC_HOURS = 6       # door list refresh when the setting is missing or odd
FIRST_SYNC_DELAY = 60        # after a start or a settings change, the first background sync waits
RETRY_BASE = 5 * 60          # a failed background sync is retried after this, doubling each time...
MAX_BACKOFF = 24 * 3600      # ...up to a day
PAGE_SIZE = 1000             # TT Hotel's largest page of locks
MAX_PAGES = 20               # 20,000 doors; guards against a server that ignores pageNo
QUOTA_PAUSE = 3600           # 30006: TT Hotel's short-term limit; wait an hour
MAX_RETRY_AFTER = 24 * 3600  # never wait longer than this when TT Hotel asks us to slow down
MAX_SAVED_PAUSE = 32 * 86400  # a saved pause longer than this is damaged (the longest is a month)
MAX_REASON = 300             # a saved pause or stop reason, in characters
QUOTA_CODES = {30006, 30007}    # pause until next UTC month (30007) / one hour (30006); never retry
TOKEN_CODES = {10003, 10004}    # token invalid/expired: drop token, re-auth once, retry once
CLIENT_HALT_CODES = {10000, 10001, 30008}           # bad client ID or secret, wrong region
LOGIN_HALT_CODES = {10007, 30002, 30005, -2018}     # username or password refused, not the admin
AUTH_HALT_CODES = CLIENT_HALT_CODES | LOGIN_HALT_CODES  # Halt until settings change or Test
GATEWAY_RETRY_CODES = {-3002, -3003, -3036, -3037}  # gateway or lock offline or busy: try later
LOCKOUT_APIS = {"reversed": "/v3/identityCard/addForReversedCardNumber",   # TT Hotel's advice
                "plain": "/v3/identityCard/add"}   # ...when reversed numbers don't match the lock
LOCKOUT_NAME = "Lost card"   # the name a lost card is briefly enrolled under (and found by)
LOCKOUT_AHEAD = 365 * 86400  # it's enrolled to start a year from now: it never opens anything...
LOCKOUT_SPAN = 86400         # ...and to last a day, should the delete never happen
MAX_LOCKOUT_CARD = 20        # digits in a card number

STOPPED_NOTE = (" (Staff Access stopped asking TT Hotel until the TT Hotel settings are saved "
                "again or the connection is tested.)")
MESSAGES = {
    10000: "TT Hotel doesn't know that Client ID. Check it in Settings (TT Hotel > Settings > "
           "Integration).",
    10001: "TT Hotel didn't accept the Client ID and Client Secret. Check them in Settings.",
    10003: "TT Hotel didn't accept the access token, even a new one. If this keeps happening, "
           "check the TT Hotel username and password in Settings.",
    10007: "TT Hotel didn't accept the username or password. Check them in Settings (use the MD5 "
           "password from TT Hotel > Settings > Integration).",
    30002: "TT Hotel didn't accept the username. Check it in Settings.",
    30005: "TT Hotel needs the MD5 password from TT Hotel > Settings > Integration. Check it in "
           "Settings.",
    30008: "This hotel's account is on TT Hotel's other server. Change the TT Hotel region in "
           "Settings.",
    80000: "This Home Assistant's clock is more than 5 minutes off, so TT Hotel refused the "
           "request. Fix the time on Home Assistant.",
    90000: "TT Hotel's server had a problem. Try again in a few minutes.",
    -2018: "That TT Hotel account isn't the hotel's admin. Use the details from TT Hotel > "
           "Settings > Integration.",
    -2012: "That door isn't connected to a gateway, so TT Hotel can't reach it.",
    -3002: "The door's gateway is offline. Check it has power and internet, then try again.",
    -3003: "The door's gateway is busy. Try again in a few minutes.",
    -3036: "The door is offline (out of the gateway's reach, or its battery is flat).",
    -3037: "The door is busy. Try again in a few minutes.",
    -4043: "This door doesn't support adding cards through TT Hotel.",
    -4056: "The door's card memory is full.",
    -1021: "TT Hotel doesn't know that card on this door.",
}
MESSAGES[10004] = MESSAGES[10003]


class TTHotelError(Exception):
    """A plain-English problem talking to TT Hotel, shown to admins as is.

    code is TT Hotel's errcode (None for network or format problems). needs_admin means only an
    admin can fix it (settings, credentials, limits), so staff are told to ask a manager."""

    def __init__(self, message: str, *, code: int | None = None, needs_admin: bool = False):
        super().__init__(message)
        self.code = code
        self.needs_admin = needs_admin


class MinuteFull(TTHotelError):
    """Refused by Staff Access's own per-minute cap: nothing was sent, and a minute later
    there's room again."""

    def __init__(self):
        super().__init__("Staff Access has made enough TT Hotel requests for this minute.")


class Halt(TTHotelError):
    """Credentials rejected: no automatic requests until settings saved or Test clicked.
    everything: the client ID or secret (every request) rather than the login."""

    def __init__(self, message: str, *, code: int | None = None, everything: bool = True):
        super().__init__(message, code=code, needs_admin=True)
        self.everything = everything


class TTHotel:
    def __init__(self, get_settings, wanted, usage_store, token_store, user_agent: str, *,
                 on_doors=None, last_sync=None):
        # get_settings() -> (enabled, region, client_id, client_secret, username, password_md5,
        #                    sync_hours)
        self._get_settings = get_settings
        self._wanted = wanted                  # () -> whether any computer can make wristbands
        self._load_usage, self._save_usage = usage_store
        self._load_token, self._save_token = token_store
        self._user_agent = user_agent
        # on_doors(list of doors) -> {"added", "updated", "removed", "total"}: saves the list
        self.on_doors = on_doors
        # last_sync() -> (when the saved door list was last synced, how many doors) or None
        self._last_sync = last_sync
        self.session: aiohttp.ClientSession | None = None
        self._token: dict | None = None        # {"token", "refresh", "expires_at", "for"}
        self._last_token_try = 0.0
        self._info: tuple[str, float] | None = None              # hotelInfo, fetched at
        self._info_error: tuple[TTHotelError, float] | None = None
        self._recent = collections.deque()     # times of requests in the last minute
        self._api_sent = 0                     # "api" requests that got past the caps
        self.synced_at: float | None = None    # last successful door sync
        self.door_count: int | None = None
        self.building_count: int | None = None
        self.last_error: str | None = None
        self.halted: str | None = None         # why requests stopped (credentials rejected)
        self._halt_everything = False          # ...all of them, or only the ones that log in
        self.paused_until = 0.0                # no requests before this (limits)
        self.pause_reason: str | None = None
        self.failures = 0
        self.next_due: float | None = None     # when the next background sync is due
        self._sync_lock = asyncio.Lock()
        self._info_lock = asyncio.Lock()
        self._token_lock = asyncio.Lock()
        self._wake = asyncio.Event()

    async def start(self):
        # Fresh locks for this event loop (one made in an earlier loop can't be waited on here).
        self._sync_lock = asyncio.Lock()
        self._info_lock = asyncio.Lock()
        self._token_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                                             headers={"User-Agent": self._user_agent})
        self._load_stops()
        self._load_last_sync()

    def _load_last_sync(self) -> None:
        """Pick up the saved door list's last sync, so a restart doesn't sync again early."""
        try:
            synced_at, doors = (self._last_sync and self._last_sync()) or (None, None)
        except Exception:  # noqa: BLE001 - unknown just means syncing soon
            return
        if isinstance(synced_at, (int, float)) and 0 < synced_at <= time.time():
            self.synced_at = float(synced_at)
            self.door_count = doors if isinstance(doors, int) else None
            self.next_due = max(time.time() + FIRST_SYNC_DELAY, synced_at + self.interval())

    async def close(self):
        if self.session:
            await self.session.close()

    # ------------------------------------------------------------ budget

    def _settings(self) -> tuple:
        return self._get_settings()

    def _api(self) -> str:
        return API_OVERRIDE or REGIONS.get(self._settings()[1], REGIONS["global"])

    def monthly_cap(self) -> int:
        return int(MONTHLY_QUOTA * MONTHLY_SHARE)

    def usage(self) -> dict:
        month = time.strftime("%Y-%m", time.gmtime())
        u = self._load_usage()
        if isinstance(u, dict) and u.get("month") == month and isinstance(u.get("count"), int):
            return {"month": month, "count": u["count"]}
        return {"month": month, "count": 0}

    def _spend(self, kind: str, background: bool = False, count: bool = True) -> None:
        """Count a request against our own caps, or refuse it. kind: "info" (hotelInfo, client
        ID and secret only), "token" or "api" (both need the login). count=False only checks."""
        now = time.time()
        if self.halted and (self._halt_everything or kind != "info"):
            raise TTHotelError(self.halted + STOPPED_NOTE, needs_admin=True)
        if now < self.paused_until:
            raise TTHotelError(self.pause_reason or "Waiting before asking TT Hotel again.",
                               needs_admin=True)
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= MINUTE_CAP:
            raise MinuteFull()
        u = self.usage()
        cap = self.monthly_cap()
        if u["count"] >= cap:
            self._pause(_next_utc_month(), f"Staff Access used its monthly share of TT Hotel "
                                           f"requests ({cap:,}); it continues next month (UTC).")
            raise TTHotelError(self.pause_reason, needs_admin=True)
        if background and u["count"] >= cap * SYNC_SHARE:
            raise TTHotelError("Automatic door syncs are off for the rest of this month, so the "
                               "remaining TT Hotel requests are kept for making wristbands. "
                               "Sync the doors by hand if they changed.")
        if not count:
            return
        self._recent.append(now)
        u["count"] += 1
        self._save_usage({**u, **self._stops()})

    def _pause(self, until: float, reason: str) -> None:
        if until > self.paused_until:
            self.paused_until = until
            self.pause_reason = reason
            log.warning("TT Hotel: %s", reason)
            self._save_stops()

    def _stops(self) -> dict:
        """A pause or stop in force, as it's saved with the request count ({} when there's none,
        so the count is saved on its own)."""
        stops = {}
        if time.time() < self.paused_until:
            stops.update(paused_until=self.paused_until, pause_reason=self.pause_reason)
        if self.halted:
            stops.update(halted=self.halted, halt_everything=self._halt_everything)
        return stops

    def _save_stops(self) -> None:
        self._save_usage({**self.usage(), **self._stops()})

    def _load_stops(self) -> None:
        """Pick up a saved pause or stop, so a restart doesn't ask TT Hotel again after it said
        stop. Anything odd in the saved copy is ignored."""
        try:
            saved = self._load_usage()
        except Exception:  # noqa: BLE001 - a damaged copy just means no saved stop
            saved = None
        saved = saved if isinstance(saved, dict) else {}
        self.paused_until, self.pause_reason = 0.0, None
        self.halted, self._halt_everything = None, False
        until, reason = saved.get("paused_until"), saved.get("pause_reason")
        now = time.time()
        if (isinstance(until, (int, float)) and not isinstance(until, bool)
                and now < until <= now + MAX_SAVED_PAUSE and isinstance(reason, str)):
            self.paused_until, self.pause_reason = float(until), reason[:MAX_REASON]
        halted = saved.get("halted")
        if isinstance(halted, str) and halted:
            self.halted = halted[:MAX_REASON]
            self._halt_everything = saved.get("halt_everything") is True

    def _too_many(self, resp: aiohttp.ClientResponse) -> TTHotelError:
        """An HTTP 429: wait (Retry-After, or longer each time), never retry straight away."""
        self.failures += 1
        wait = min(3600, 60 * 2 ** min(self.failures - 1, 6))
        try:
            wait = max(wait, int(float(resp.headers.get("Retry-After", 0))))
        except (ValueError, OverflowError):
            pass
        wait = min(wait, MAX_RETRY_AFTER)   # one odd reply mustn't stop requests for good
        self._pause(time.time() + wait, "TT Hotel asked Staff Access to slow down (too many "
                                        f"requests); waiting {round(wait / 60)} min.")
        return TTHotelError(self.pause_reason, needs_admin=True)

    def _halt(self, reason: str, everything: bool) -> None:
        if self.halted != reason:
            log.warning("TT Hotel: %s Automatic requests stopped.", reason)
        self.halted = reason
        self._halt_everything = self._halt_everything or everything
        self._save_stops()

    # ------------------------------------------------------------ requests

    def _credentials(self, login: bool) -> tuple:
        enabled, _, client_id, secret, username, password, _ = self._settings()
        if not enabled:
            raise TTHotelError("Wristbands are turned off in Settings.", needs_admin=True)
        if not client_id or not secret:
            raise TTHotelError("Enter the TT Hotel Client ID and Client Secret in Settings.",
                               needs_admin=True)
        if login and (not username or not password):
            raise TTHotelError("Enter the TT Hotel username and password in Settings.",
                               needs_admin=True)
        return self._api(), client_id, secret, username or "", (password or "").lower()

    async def _request(self, method: str, path: str, params: dict, *, kind: str,
                       background: bool = False) -> dict:
        """One HTTP request through the caps. Returns the reply or raises TTHotelError."""
        self._spend(kind, background)
        if kind == "api":
            self._api_sent += 1
        url = f"{self._api()}{path}"
        try:
            if method == "POST":   # TT Hotel wants form-encoded bodies
                request = self.session.post(url, data=params)
            else:
                request = self.session.get(url, params=params)
            async with request as resp:
                if resp.status == 429:
                    raise self._too_many(resp)
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
                status = resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
            # Never the error text: some of aiohttp's include the URL, secret and all.
            log.warning("TT Hotel %s %s failed: %s", method, path, type(err).__name__)
            raise TTHotelError(f"Can't reach TT Hotel: {_network_reason(err)}.") from None
        if not isinstance(body, dict):
            if status >= 500:
                raise TTHotelError(MESSAGES[90000])
            raise TTHotelError("TT Hotel sent a reply Staff Access couldn't read.")
        if body.get("errcode") not in (None, 0, "0"):
            raise self._api_error(body.get("errcode"), body.get("errmsg"), path)
        if status >= 400:
            log.warning("TT Hotel %s %s: HTTP %s", method, path, status)
            raise TTHotelError(MESSAGES[90000] if status >= 500
                               else f"TT Hotel refused the request (HTTP {status}).")
        return body

    def _api_error(self, raw_code, raw_message, path: str) -> TTHotelError:
        code = _as_int(raw_code)
        detail = " ".join(str(raw_message or "").split())[:160]
        log.warning("TT Hotel %s: errcode %s %s", path, raw_code, detail)
        if code == 30007:
            self._pause(_next_utc_month(), "TT Hotel says this account used its API requests "
                                           "for the month; Staff Access waits until next "
                                           "month (UTC).")
            return TTHotelError(self.pause_reason, code=code, needs_admin=True)
        if code == 30006:
            self._pause(time.time() + QUOTA_PAUSE, "TT Hotel says too many requests were made; "
                                                   "Staff Access waits an hour.")
            return TTHotelError(self.pause_reason, code=code, needs_admin=True)
        if code in AUTH_HALT_CODES:
            everything = code in CLIENT_HALT_CODES
            self._halt(MESSAGES[code], everything)
            return Halt(MESSAGES[code], code=code, everything=everything)
        if code in MESSAGES:
            return TTHotelError(MESSAGES[code], code=code)
        if detail:
            return TTHotelError(f"TT Hotel reported a problem: {detail.rstrip('.')}.", code=code)
        return TTHotelError(f"TT Hotel reported a problem (error {raw_code}).", code=code)

    # ------------------------------------------------------------ access token

    def _stored_token(self, fingerprint: str) -> dict | None:
        """The token for these credentials, from memory or the saved copy."""
        if self._token and self._token.get("for") == fingerprint:
            return self._token
        try:
            saved = self._load_token()
        except Exception:  # noqa: BLE001 - a damaged copy just means logging in again
            saved = None
        if (isinstance(saved, dict) and saved.get("for") == fingerprint
                and isinstance(saved.get("token"), str) and saved["token"]
                and isinstance(saved.get("expires_at"), (int, float))):
            self._token = {"token": saved["token"], "refresh": str(saved.get("refresh") or ""),
                           "expires_at": float(saved["expires_at"]), "for": fingerprint}
            return self._token
        return None

    def _drop_token(self) -> None:
        self._token = None
        self._last_token_try = 0.0   # rejected: a new one may be asked for straight away
        self._save_token({})

    async def _access_token(self, background: bool = False) -> str:
        async with self._token_lock:
            creds = self._credentials(login=True)
            fingerprint = _fingerprint(creds)
            token = self._stored_token(fingerprint)
            now = time.time()
            if token and token["expires_at"] - now > TOKEN_MARGIN:
                return token["token"]
            if now - self._last_token_try < TOKEN_GAP:
                if token and token["expires_at"] > now:
                    return token["token"]   # still good; renew it a bit later
                raise TTHotelError("Waiting a minute before asking TT Hotel for a new access "
                                   "token.")
            self._spend("token", background, count=False)   # a refusal mustn't use up the minute
            self._last_token_try = now
            fresh = None
            if token and token["refresh"]:
                try:
                    fresh = await self._token_request(
                        creds, {"grant_type": "refresh_token", "refresh_token": token["refresh"]},
                        background)
                except Halt:
                    raise
                except TTHotelError as err:
                    if err.code is None or err.code in QUOTA_CODES:
                        if token["expires_at"] > now:
                            return token["token"]   # TT Hotel unreachable: keep the old one
                        raise
                    log.info("TT Hotel token refresh failed (%s); logging in again", err)
            if fresh is None:
                fresh = await self._token_request(
                    creds, {"username": creds[3], "password": creds[4]}, background)
            fresh["for"] = fingerprint
            self._token = fresh
            self._save_token(dict(fresh))
            return fresh["token"]

    async def _token_request(self, creds: tuple, fields: dict, background: bool) -> dict:
        body = await self._request("POST", "/oauth2/token",
                                   {"clientId": creds[1], "clientSecret": creds[2], **fields},
                                   kind="token", background=background)
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise TTHotelError("TT Hotel didn't send an access token.")
        refresh = body.get("refresh_token")
        lifetime = body.get("expires_in")
        if not isinstance(lifetime, (int, float)) or not 3600 <= lifetime <= 10 * 365 * 86400:
            lifetime = TOKEN_LIFETIME   # also catches NaN
        return {"token": token, "refresh": refresh if isinstance(refresh, str) else "",
                "expires_at": time.time() + lifetime}

    async def _authed_get(self, path: str, params: dict, background: bool = False) -> dict:
        return await self._authed("GET", path, params, background)

    async def _authed(self, method: str, path: str, params: dict,
                      background: bool = False) -> dict:
        for attempt in (1, 2):
            token = await self._access_token(background)
            client_id = self._credentials(login=True)[1]
            try:
                return await self._request(
                    method, path, {**params, "clientId": client_id, "accessToken": token,
                                   "date": _date_ms()}, kind="api", background=background)
            except TTHotelError as err:
                if err.code not in TOKEN_CODES or attempt == 2:
                    raise
                log.info("TT Hotel didn't accept the saved access token; logging in again")
                self._drop_token()
        raise TTHotelError(MESSAGES[10004])   # not reached

    # ------------------------------------------------------------ hotelInfo and doors

    async def hotel_info(self, *, fresh: bool) -> str:
        """The encoder's hotel key. fresh=True (setting up the encoder or a blank card, floor,
        building and all-door records) never returns an old one. fresh=False (cards for single
        doors, which the encoder writes offline) falls back to the last one of any age when TT
        Hotel can't be reached. Raises TTHotelError."""
        async with self._info_lock:
            now = time.time()
            if self._info and now - self._info[1] < HOTEL_INFO_TTL:
                return self._info[0]
            if self._info_error and now - self._info_error[1] < INFO_RETRY_GAP:
                if not fresh and self._info:
                    return self._info[0]
                raise self._info_error[0]
            try:
                creds = self._credentials(login=False)
                body = await self._request("GET", "/v3/hotel/getInfo",
                                           {"clientId": creds[1], "clientSecret": creds[2],
                                            "date": _date_ms()}, kind="info")
                info = body.get("hotelInfo")
                if not isinstance(info, str) or not info:
                    raise TTHotelError("TT Hotel didn't send the hotel key (hotelInfo).")
            except TTHotelError as err:
                self._info_error = (err, time.time())
                if not fresh and self._info:
                    return self._info[0]
                raise
            # Aged from when the request started: TT Hotel issued it no earlier than that.
            self._info = (info, now)
            self._info_error = None
            return info

    def hotel_info_expired(self) -> None:
        """The encoder said the hotel key expired (code 13): don't reuse it."""
        self._info = None

    async def hotel_locks(self, *, background: bool = False) -> list[dict]:
        """Every hotel lock in TT Hotel (all pages), normalized. Locks without a usable ID or
        MAC are left out."""
        found, page = {}, 1
        while True:
            body = await self._authed_get("/v3/lock/listByHotel",
                                          {"pageNo": page, "pageSize": PAGE_SIZE, "type": 1},
                                          background)
            items = body.get("list")
            if items is None:
                items = []
            if not isinstance(items, list):
                raise TTHotelError("TT Hotel sent a door list Staff Access couldn't read.")
            for item in items:
                door = _door(item)
                if door is None:
                    log.warning("TT Hotel: skipped a lock without a usable ID or MAC")
                    continue
                found[door["lock_id"]] = door
            pages = _as_int(body.get("pages"))
            if not items or page >= MAX_PAGES:
                break
            if (len(items) < PAGE_SIZE) if pages is None else page >= pages:
                break
            page += 1
        return list(found.values())

    async def sync(self, *, admin: bool = False) -> dict:
        """Fetch the door list and save it through on_doors. Returns its counts plus
        "buildings"; raises TTHotelError (also kept in last_error for the dashboard).
        admin: an admin asked (Test, or Sync now), which also clears a stop caused by rejected
        credentials; otherwise it's a background sync, which stops at SYNC_SHARE."""
        async with self._sync_lock:
            if admin and self.halted:
                self.halted = None
                self._halt_everything = False
                self._save_stops()
            try:
                doors = await self.hotel_locks(background=not admin)
                result = {"added": 0, "updated": 0, "removed": 0, "total": len(doors)}
                if self.on_doors is not None:
                    saved = self.on_doors(doors)
                    if inspect.isawaitable(saved):
                        saved = await saved
                    result = dict(saved or result)
            except TTHotelError as err:
                self.failures += 1
                if self.last_error != str(err):
                    log.warning("TT Hotel door sync: %s", err)
                self.last_error = str(err)
                raise
            except Exception as err:  # noqa: BLE001 - a malformed reply must not reach a request
                self.failures += 1
                log.warning("TT Hotel door sync failed: %r", err)
                self.last_error = "Staff Access couldn't read or save TT Hotel's door list."
                raise TTHotelError(self.last_error) from None
            self.synced_at = time.time()
            self.door_count = len(doors)
            self.building_count = len({d["building_number"] for d in doors
                                       if d["building_number"] is not None})
            self.last_error = None
            self.failures = 0
            result.setdefault("total", len(doors))
            result.setdefault("buildings", self.building_count)
            return result

    # ------------------------------------------------------------ lost cards

    def _room(self, calls: int) -> None:
        """Refuse up front unless the caps leave room for this many calls, so a lock-out isn't
        cut off between enrolling the lost card and deleting it."""
        self._spend("api", count=False)
        now = time.time()
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) + calls > MINUTE_CAP:
            raise MinuteFull()
        if self.usage()["count"] + calls > self.monthly_cap():
            raise TTHotelError("Staff Access has nearly used its monthly share of TT Hotel "
                               "requests, so it can't lock out a card this month.",
                               needs_admin=True)

    async def lock_out_card(self, lock_id: int, card_number: str, *, api: str = "reversed",
                            card_id: int | None = None, find_first: bool = False) -> dict:
        """Make a door on a gateway refuse a lost card for good: enroll the card number on the
        lock (starting a year from now, so it can't open anything), then delete it, which TT
        Hotel says blacklists that number in the lock (encoder-made cards included).

        api: "reversed" (addForReversedCardNumber, TT Hotel's advice for encoder card numbers)
        or "plain" (add). card_id: the card enrolled by an earlier try whose delete didn't go
        through; only the delete is sent. find_first: an earlier try's enrolment may or may not
        have happened (no answer from TT Hotel), so look for it on the lock first (one more
        call) instead of enrolling the number twice.

        Never raises. Returns {"status": "done"|"retry"|"failed", "card_id": the enrolled card
        (when known), "code": TT Hotel's errcode or None, "message": a plain sentence for admins
        (None when done), "unsure": True when an enrolment may have happened unseen, "wait":
        True when Staff Access's own per-minute cap held this try back, so TT Hotel wasn't
        asked}. "retry" means trying again later may work (gateway or door busy or offline, no
        answer, the minute's requests used up); retries are the caller's to space out and cap."""
        # An earlier enrolment that may have happened stays unsure until the look for it is done.
        result = {"status": "failed", "card_id": card_id, "code": None, "message": None,
                  "unsure": bool(find_first and card_id is None), "wait": False}
        lock = _as_int(lock_id)
        number = str(card_number or "").strip()
        if (lock is None or not 1 <= lock <= 2 ** 53 or api not in LOCKOUT_APIS
                or not re.fullmatch(rf"[0-9]{{1,{MAX_LOCKOUT_CARD}}}", number)
                or (card_id is not None and (_as_int(card_id) is None or _as_int(card_id) < 1))):
            result["message"] = "That door or card number can't be locked out."
            return result
        step = "delete" if card_id is not None else "add"
        try:
            calls = 1 if card_id is not None else 2
            self._room(calls + (1 if find_first and card_id is None else 0))
            if card_id is None and find_first:
                card_id = await self._find_lockout(lock, number)
                result.update(card_id=card_id, unsure=False)
            if card_id is None:
                start = _date_ms() + LOCKOUT_AHEAD * 1000
                sent = self._api_sent
                try:
                    body = await self._authed("POST", LOCKOUT_APIS[api], {
                        "lockId": lock, "cardNumber": number, "cardName": LOCKOUT_NAME,
                        "startDate": start, "endDate": start + LOCKOUT_SPAN * 1000,
                        "addType": 2})
                except TTHotelError as err:
                    if err.code is None and self._api_sent > sent:
                        result["unsure"] = True   # sent, but no readable answer
                    raise
                card_id = _as_int(body.get("cardId"))
                if card_id is None or card_id < 1:
                    result.update(unsure=True, message="TT Hotel added the card to the door but "
                                  "didn't say which card it is, so it couldn't be deleted. Try "
                                  "again (Staff Access looks for it first).")
                    return result
                result["card_id"] = card_id
            step = "delete"
            await self._authed("POST", "/v3/identityCard/delete",
                               {"lockId": lock, "cardId": card_id, "deleteType": 2})
        except TTHotelError as err:
            retry = (err.code in GATEWAY_RETRY_CODES
                     or (err.code is None and not err.needs_admin))
            message = str(err)
            if step == "delete" and result["card_id"] is not None:
                message += (" The card is on the door, with dates starting a year from now, "
                            "until it's deleted: it can't open the door meanwhile, but try again "
                            "well before then.")
            result.update(status="retry" if retry else "failed", code=err.code, message=message,
                          wait=isinstance(err, MinuteFull))
            return result
        except Exception as err:  # noqa: BLE001 - a malformed reply must not reach a request
            log.warning("TT Hotel lock-out of a card on lock %s failed: %r", lock, err)
            result.update(status="retry", unsure=card_id is None,
                          message="Something went wrong talking to TT Hotel. Try again.")
            return result
        log.info("TT Hotel: card locked out on lock %s", lock)
        result.update(status="done", card_id=card_id)
        return result

    async def _find_lockout(self, lock: int, number: str) -> int | None:
        """This lost card, enrolled by an earlier try (by its name, its start a year ahead and
        its number), or None. Another lost card's enrolment left on the same door is never
        taken for it: deleting that one wouldn't lock this number out."""
        body = await self._authed("GET", "/v3/identityCard/list",
                                  {"lockId": lock, "pageNo": 1, "pageSize": 20, "orderBy": 1,
                                   "searchStr": LOCKOUT_NAME})
        items = body.get("list")
        now_ms = _date_ms()
        numbers = _lockout_numbers(number)
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get("cardName") != LOCKOUT_NAME:
                continue
            listed = str(item.get("cardNumber") or "").strip()
            if len(listed) > MAX_LOCKOUT_CARD + 5 or _as_int(listed) not in numbers:
                continue
            start, found = _as_int(item.get("startDate")), _as_int(item.get("cardId"))
            if start is not None and start > now_ms + 30 * 86400 * 1000 and found and found > 0:
                return found
        return None

    # ------------------------------------------------------------ status

    def interval(self) -> int:
        """Seconds between background door syncs (the setting, 1 to 48 hours)."""
        hours = _as_int(self._settings()[6])
        return 3600 * (hours if hours and 1 <= hours <= 48 else DEFAULT_SYNC_HOURS)

    def state(self) -> dict:
        """For the dashboard."""
        settings = self._settings()
        now = time.time()
        paused = now < self.paused_until
        try:
            token = self._stored_token(_fingerprint(self._credentials(login=True)))
        except TTHotelError:
            token = None
        try:
            needed = bool(self._wanted())
        except Exception:  # noqa: BLE001 - a status read must not fail
            needed = False
        return {"enabled": bool(settings[0]), "synced_at": self.synced_at,
                "doors": self.door_count, "last_error": self.last_error,
                "halted": self.halted, "paused": paused,
                "pause_reason": self.pause_reason if paused else None,
                "paused_until": self.paused_until if paused else None,
                "used_month": self.usage()["count"], "monthly_cap": self.monthly_cap(),
                "quota": MONTHLY_QUOTA, "token_expires": token["expires_at"] if token else None,
                "needed": needed}

    def forget(self, *, saved_stops: bool = False) -> None:
        """Settings changed, or a backup was restored: start from scratch (pauses for TT Hotel's
        own limits stay, and so does the saved token, which is checked against the credentials
        before it's used). saved_stops: a migration just saved another install's request count
        with its pause or stop, for these same credentials, so those apply here instead (TT
        Hotel refused these credentials, or asked to wait) and a stop isn't cleared."""
        self._token = None
        self._last_token_try = 0.0
        self._info = None
        self._info_error = None
        self.last_error = None
        if saved_stops:
            until, reason = self.paused_until, self.pause_reason
            self._load_stops()
            if until > max(self.paused_until, time.time()):   # a pause of this install's own
                self.paused_until, self.pause_reason = until, reason
                self._save_stops()
        elif self.halted:
            self.halted = None
            self._halt_everything = False
            self._save_stops()
        self.failures = 0
        self.next_due = None
        self._wake.set()

    async def run(self) -> None:
        """Background loop: refresh the door list every few hours while wristbands are in use."""
        while True:
            self._wake.clear()
            wait = 300   # nothing to do: look again in a while, or when the settings change
            try:
                if self._settings()[0] and self._wanted():
                    now = time.time()
                    if self.next_due is None:
                        self.next_due = now + FIRST_SYNC_DELAY   # just started, or settings changed
                    if now >= self.next_due:
                        await self._background_sync()
                    wait = max(1, self.next_due - time.time())
                else:
                    self.next_due = None
            except Exception as err:  # noqa: BLE001 - the loop must keep running
                log.warning("TT Hotel door sync failed: %s", err)
                self.next_due = time.time() + RETRY_BASE
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=min(wait, 300))
            except asyncio.TimeoutError:
                pass

    async def _background_sync(self) -> None:
        now = time.time()
        if self.halted:   # the door list needs the login, so any stop applies
            self.last_error = self.halted + STOPPED_NOTE
            self.next_due = now + self.interval()
            return
        if now < self.paused_until:
            self.last_error = self.pause_reason
            self.next_due = self.paused_until + 1
            return
        try:
            await self.sync()
        except TTHotelError:
            delay = min(MAX_BACKOFF, RETRY_BASE * 2 ** min(self.failures - 1, 10))
            self.next_due = time.time() + max(RETRY_BASE, delay)
        else:
            self.next_due = time.time() + self.interval()


def _next_utc_month() -> float:
    t = time.gmtime()
    year, month = (t.tm_year + 1, 1) if t.tm_mon == 12 else (t.tm_year, t.tm_mon + 1)
    return float(calendar.timegm((year, month, 1, 0, 0, 0)))


def _date_ms() -> int:
    """TT Hotel wants the current time in milliseconds, within 5 minutes of its own."""
    return int(time.time() * 1000)


def _fingerprint(creds: tuple) -> str:
    """Which credentials a saved token belongs to, without saving the credentials."""
    return hashlib.sha256("\n".join(creds).encode()).hexdigest()


def _network_reason(err: BaseException) -> str:
    if isinstance(err, asyncio.TimeoutError):
        return "the request timed out"
    if isinstance(err, aiohttp.ClientConnectorError):
        return "no connection to the server (check the internet connection)"
    return "the connection failed"


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _lockout_numbers(number: str) -> set[int]:
    """The numbers TT Hotel may list a lost card under: as sent, or with its bytes reversed
    (what addForReversedCardNumber stores: 3661413478 is listed as 1723612378)."""
    value = int(number)
    size = max(4, (value.bit_length() + 7) // 8)
    return {value, int.from_bytes(value.to_bytes(size, "big"), "little")}


def _text(value, limit: int = 100) -> str:
    return " ".join(str(value).split())[:limit] if value is not None else ""


def _door(item) -> dict | None:
    """One listByHotel item as a door, or None without a usable ID or MAC. Everything but
    lockId and lockMac is optional (the vendor's own examples aren't valid JSON)."""
    if not isinstance(item, dict):
        return None
    lock_id = _as_int(item.get("lockId"))
    mac = re.sub(r"[^0-9A-Fa-f]", "", str(item.get("lockMac") or "")).upper()
    if lock_id is None or not 1 <= lock_id <= 2 ** 53 or len(mac) != 12:
        return None
    building = item.get("buildingNumber")
    if building is None:
        building = item.get("buildNumber")   # the spelling in the vendor's example
    battery = _as_int(item.get("electricQuantity"))

    def place(value):
        number = _as_int(value)
        return number if number is not None and 0 <= number <= 255 else None
    return {"lock_id": lock_id, "name": _text(item.get("lockName")),
            "alias": _text(item.get("lockAlias")), "mac": mac,
            "building_number": place(building), "building_name": _text(item.get("buildingName")),
            "floor_number": place(item.get("floorNumber")),
            "floor_name": _text(item.get("floorName")),
            "battery": battery if battery is not None and 0 <= battery <= 100 else None,
            "has_gateway": _as_int(item.get("hasGateway")) == 1, "lock_type": 1}
