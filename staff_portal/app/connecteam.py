"""Connecteam: pick staff from the Connecteam user list, and only let linked people sign in while
they're clocked in.

Uses Connecteam's OAuth 2.0 client-credentials flow (Integration Center -> OAuth 2.0), so the
add-on only ever holds a client ID and secret it was given, never anyone's password. Scopes
needed: users.read, time_clock.read. A user's kiosk code (users.read) can become their PIN.

Who is clocked in comes from the time clock's time activities: a shift with no end yet is a
person on the clock. One check covers everyone (one request per time clock). Connecteam's rate
limits are per account and shared with every other integration (Expert: 100 a minute, 10,000 a
day), and the account's owner uses the API for other things too, so this client asks as little
as it can:

- A regular check once an hour (the setting), only while someone actually needs it
  (Connecteam on, and at least one active, linked person who must be clocked in).
- Signing in or using a control costs nothing if the last check found that person clocked in.
  If it didn't (or there's no recent answer), it checks right away, at most every FORCE_GAP
  seconds; that check updates everyone. If it happened within SKIP_WINDOW of the next hourly
  check, that hourly check is skipped. Reading never checks.
- The list of time clocks and the user list are cached.
- Hard caps of its own: MINUTE_CAP requests a minute and DAILY_SHARE of the account's daily
  limit, counted across restarts.
- It reads Connecteam's x-ratelimit headers and pauses when the account (not just this add-on)
  is running low, honors 429s with exponential backoff, and backs off after errors.
- Rejected credentials or scopes stop all automatic requests until the settings change or an
  admin clicks Test connection, so it never hammers Connecteam with requests that will fail.

If Connecteam can't be asked, the last answer is kept until STALE_MARGIN after the next check
was due, after which clocked-in-only people can't sign in or use controls until it answers.
"""

import asyncio
import collections
import logging
import os
import time

import aiohttp

log = logging.getLogger("staff_portal")

REGIONS = {"global": "https://api.connecteam.com", "au": "https://api-au.connecteam.com"}
API_OVERRIDE = os.environ.get("CONNECTEAM_API", "").rstrip("/")   # for testing

STALE_MARGIN = 15 * 60       # trust the last answer this long past the next check when Connecteam is down
FORCE_GAP = 30               # an on-demand check (sign-in or control) at most this often
SKIP_WINDOW = 20 * 60        # an on-demand check this close before an hourly check replaces it
MINUTE_CAP = 20              # requests a minute, at most (Expert allows 100 for the whole account)
DAILY_SHARE = 0.2            # at most this share of the account's daily limit
POLL_SHARE = 0.15            # ...of which regular checks are planned to use at most this much
DEFAULT_DAY_LIMIT = 10_000   # Expert plan, until Connecteam's headers say otherwise
LOW_DAY_SHARE = 0.10         # pause when the account has less than this share left today
LOW_MINUTE_LEFT = 10         # pause for the minute when the account has fewer left than this
CLOCKS_TTL = 6 * 3600        # how long the list of time clocks is reused
USERS_TTL = 5 * 60           # how long the user list is reused
MAX_FORCE_GAP = 15 * 60      # on-demand checks slow to this while Connecteam keeps failing
FORCED_SHARE = 0.75          # on-demand checks stop at this share of the daily cap, keeping the
                             # rest for the scheduled ones
MAX_RETRY_AFTER = 24 * 3600  # never wait longer than this when Connecteam asks us to slow down
MAX_SHIFT_HOURS = 24         # an open shift older than this doesn't count (a forgotten clock-out)


class ConnecteamError(Exception):
    pass


class Halt(ConnecteamError):
    """Credentials or scopes rejected: don't retry on our own."""


class Connecteam:
    def __init__(self, get_settings, wanted, usage_store, user_agent: str):
        # get_settings() -> (enabled, client_id, client_secret, region, poll_minutes)
        self._get_settings = get_settings
        self._wanted = wanted                  # () -> whether anyone needs the clock checked
        self._load_usage, self._save_usage = usage_store
        self._user_agent = user_agent
        self.session: aiohttp.ClientSession | None = None
        self._token: tuple[str, float] | None = None
        self._token_for: tuple | None = None
        self._last_token_try = 0.0
        self._clocks: tuple[list[str], float] | None = None
        self._users: tuple[list[dict], float] | None = None
        self._recent = collections.deque()      # times of requests in the last minute
        self.clocked_in: set[str] = set()
        self.checked_at: float | None = None    # last successful check
        self.last_error: str | None = None
        self.halted: str | None = None          # why automatic requests stopped (bad keys/scopes)
        self.paused_until = 0.0                 # no requests before this (rate limits)
        self.pause_reason: str | None = None
        self.failures = 0
        self.next_due: float | None = None      # when the next regular check is due
        self.limits: dict = {}                  # latest x-ratelimit-* values seen
        self._last_attempt = 0.0
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()

    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                                             headers={"User-Agent": self._user_agent})

    async def close(self):
        if self.session:
            await self.session.close()

    # ------------------------------------------------------------ budget

    def _settings(self):
        return self._get_settings()

    def _api(self) -> str:
        return API_OVERRIDE or REGIONS.get(self._settings()[3], REGIONS["global"])

    def day_limit(self) -> int:
        return self.limits.get("day_limit") or DEFAULT_DAY_LIMIT

    def daily_cap(self) -> int:
        return max(50, int(self.day_limit() * DAILY_SHARE))

    def usage(self) -> dict:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        u = self._load_usage()
        return u if u.get("day") == today else {"day": today, "count": 0}

    def _spend(self) -> None:
        """Count a request against our own caps, or refuse it."""
        now = time.time()
        if now < self.paused_until:
            raise ConnecteamError(self.pause_reason or "Waiting before asking Connecteam again.")
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= MINUTE_CAP:
            raise ConnecteamError("Staff Access has made enough Connecteam requests for this minute.")
        u = self.usage()
        if u["count"] >= self.daily_cap():
            self._pause(_next_utc_midnight(),
                        f"Staff Access used its daily share of Connecteam requests ({self.daily_cap()}); "
                        "it continues after midnight UTC.")
            raise ConnecteamError(self.pause_reason)
        self._recent.append(now)
        u["count"] += 1
        self._save_usage(u)

    def _pause(self, until: float, reason: str) -> None:
        if until > self.paused_until:
            self.paused_until = until
            self.pause_reason = reason
            log.warning("Connecteam: %s", reason)

    def _read_limits(self, resp: aiohttp.ClientResponse) -> None:
        h = resp.headers
        for key, header in (("minute_limit", "x-ratelimit-minute-limit"),
                            ("minute_left", "x-ratelimit-minute-remaining"),
                            ("minute_reset", "x-ratelimit-minute-reset"),
                            ("day_limit", "x-ratelimit-day-limit"),
                            ("day_left", "x-ratelimit-day-remaining"),
                            ("day_reset", "x-ratelimit-day-reset")):
            try:
                value = int(float(h[header]))
            except (KeyError, ValueError, OverflowError):
                continue
            if 0 <= value < 10 ** 12:   # ignore nonsense rather than act on it
                self.limits[key] = value
        now = time.time()
        left, limit = self.limits.get("day_left"), self.limits.get("day_limit")
        if left is not None and limit and left < limit * LOW_DAY_SHARE:
            self._pause(self._reset_time("day_reset", _next_utc_midnight()),
                        f"Your Connecteam account has only {left} of its {limit} API requests left "
                        "today, so Staff Access stopped asking until the limit resets.")
        if self.limits.get("minute_left") is not None and self.limits["minute_left"] < LOW_MINUTE_LEFT:
            self._pause(self._reset_time("minute_reset", now + 60),
                        "Your Connecteam account is close to its per-minute API limit; waiting a minute.")

    def _reset_time(self, key: str, fallback: float) -> float:
        reset = self.limits.get(key)
        now = time.time()
        return reset + 1 if reset and now < reset < now + 2 * 86400 else fallback

    def _too_many(self, resp: aiohttp.ClientResponse) -> ConnecteamError:
        """A 429: wait for the reset (or back off exponentially), never retry straight away."""
        self.failures += 1
        wait = min(3600, 60 * 2 ** min(self.failures - 1, 6))
        try:
            wait = max(wait, int(float(resp.headers.get("Retry-After", 0))))
        except (ValueError, OverflowError):
            pass
        wait = min(wait, MAX_RETRY_AFTER)   # one odd reply mustn't stop checks for good
        until = min(max(time.time() + wait, self._reset_time("minute_reset", 0)),
                    time.time() + MAX_RETRY_AFTER)
        self._pause(until, "Connecteam asked Staff Access to slow down (too many requests); "
                           f"waiting {round((until - time.time()) / 60)} min.")
        return ConnecteamError(self.pause_reason)

    # ------------------------------------------------------------ API

    def _credentials(self) -> tuple:
        enabled, client_id, secret, region, _ = self._settings()
        if not enabled:
            raise ConnecteamError("Connecteam is turned off in Settings.")
        if not client_id or not secret:
            raise ConnecteamError("Enter the Connecteam Client ID and Client Secret in Settings.")
        return client_id, secret, region

    async def _access_token(self) -> str:
        creds = self._credentials()
        if self._token and self._token_for == creds and self._token[1] > time.time():
            return self._token[0]
        if time.time() - self._last_token_try < 60:
            raise ConnecteamError("Waiting a minute before asking Connecteam for a new access token.")
        self._last_token_try = time.time()
        self._spend()
        try:
            async with self.session.post(
                f"{self._api()}/oauth/v1/token", data={"grant_type": "client_credentials"},
                auth=aiohttp.BasicAuth(creds[0], creds[1]),
            ) as resp:
                self._read_limits(resp)
                if resp.status == 429:
                    raise self._too_many(resp)
                body = await resp.json(content_type=None)
                if resp.status in (400, 401, 403):
                    raise Halt("Connecteam didn't accept the Client ID and Secret "
                               f"(HTTP {resp.status}). Check them in Settings.")
                if resp.status >= 400 or not isinstance(body, dict) or not body.get("access_token"):
                    raise ConnecteamError(f"Connecteam couldn't issue an access token (HTTP {resp.status}).")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise ConnecteamError(f"Can't reach Connecteam: {err or type(err).__name__}") from err
        ttl = body.get("expires_in") if isinstance(body.get("expires_in"), (int, float)) else 86400
        ttl = min(max(ttl, 360), 86400) if ttl == ttl else 86400   # sane, and not NaN
        self._token = (str(body["access_token"]), time.time() + ttl - 300)
        self._token_for = creds
        return self._token[0]

    async def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in (1, 2):
            token = await self._access_token()
            self._spend()
            try:
                async with self.session.get(f"{self._api()}{path}", params=params,
                                            headers={"Authorization": f"Bearer {token}",
                                                     "Accept": "application/json"}) as resp:
                    self._read_limits(resp)
                    if resp.status == 429:
                        raise self._too_many(resp)
                    if resp.status == 401 and attempt == 1:
                        self._token = None   # expired or revoked: get a new one and retry once
                        self._last_token_try = 0.0
                        continue
                    if resp.status in (401, 403):
                        raise Halt(f"Connecteam refused {path} (HTTP {resp.status}). Check that the "
                                   "OAuth app has the users.read and time_clock.read scopes, and that "
                                   "your plan includes API access.")
                    body = await resp.json(content_type=None)
                    if resp.status >= 400:
                        raise ConnecteamError(f"Connecteam couldn't answer {path} (HTTP {resp.status}).")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                raise ConnecteamError(f"Can't reach Connecteam: {err or type(err).__name__}") from err
            if not isinstance(body, dict):
                raise ConnecteamError("Connecteam sent an unexpected reply.")
            data = body.get("data", body)
            return data if isinstance(data, dict) else {"items": data}
        raise Halt("Connecteam didn't accept the access token.")

    async def _call(self, coro_fn, *args):
        """An admin-initiated request: same caps, and a halt is recorded."""
        self._check_halt()
        try:
            return await coro_fn(*args)
        except Halt as err:
            self._halt(str(err))
            raise

    def _check_halt(self) -> None:
        if self.halted:
            raise ConnecteamError(self.halted + " (Automatic checks are stopped until the Connecteam "
                                                "settings are saved again or Test connection is clicked.)")

    def _halt(self, reason: str) -> None:
        if self.halted != reason:
            log.warning("Connecteam: %s Automatic checks stopped.", reason)
        self.halted = reason

    async def users(self, max_age: float = USERS_TTL) -> list[dict]:
        """Active Connecteam users as [{"id", "name", "kiosk_code"}], sorted by name.
        kiosk_code is None unless it's usable as a PIN (4 to 8 digits)."""
        if self._users and time.time() - self._users[1] < max_age:
            return self._users[0]
        return await self._call(self._fetch_users)

    async def _fetch_users(self) -> list[dict]:
        found, offset = [], 0
        while True:
            data = await self._get("/users/v1/users", {"limit": 500, "offset": offset})
            page = _first_list(data, "users")
            for u in page:
                if not isinstance(u, dict) or u.get("userId") is None or u.get("isArchived"):
                    continue
                name = " ".join(str(u.get(k) or "").strip() for k in ("firstName", "lastName")).strip()
                code = str(u.get("kioskCode") or "").strip()
                found.append({"id": str(u["userId"]), "name": name or f"User {u['userId']}",
                              "kiosk_code": code if code.isdigit() and 4 <= len(code) <= 8 else None})
            if len(page) < 500 or offset > 50_000:
                break
            offset += 500
        found.sort(key=lambda u: u["name"].lower())
        self._users = (found, time.time())
        return found

    async def kiosk_code(self, user_id: str) -> str | None:
        """One user's kiosk code, if it's usable as a PIN (from the cached list when recent)."""
        for u in await self.users():
            if u["id"] == str(user_id):
                return u["kiosk_code"]
        raise ConnecteamError("That person isn't an active Connecteam user.")

    async def time_clock_ids(self, max_age: float = CLOCKS_TTL) -> list[str]:
        if self._clocks and time.time() - self._clocks[1] < max_age:
            return self._clocks[0]
        data = await self._get("/time-clock/v1/time-clocks")
        ids = [str(c["id"]) for c in _first_list(data, "timeClocks")
               if isinstance(c, dict) and c.get("id") is not None and not c.get("isArchived")]
        self._clocks = (ids, time.time())
        return ids

    async def fetch_clocked_in(self) -> set[str]:
        """Connecteam user IDs with an open shift on any time clock right now."""
        now = time.time()
        # A window around today in any time zone; open shifts are what matter.
        start = time.strftime("%Y-%m-%d", time.gmtime(now - 2 * 86400))
        end = time.strftime("%Y-%m-%d", time.gmtime(now + 86400))
        on_clock = set()
        for clock_id in await self.time_clock_ids():
            data = await self._get(f"/time-clock/v1/time-clocks/{clock_id}/time-activities",
                                   {"startDate": start, "endDate": end, "activityTypes": "shift"})
            for entry in _first_list(data, "timeActivitiesByUsers"):
                if not isinstance(entry, dict) or entry.get("userId") is None:
                    continue
                shifts = entry.get("shifts")
                for shift in shifts if isinstance(shifts, list) else []:
                    if isinstance(shift, dict) and _is_open(shift, now):
                        on_clock.add(str(entry["userId"]))
        return on_clock

    # ------------------------------------------------------------ status

    def interval(self) -> int:
        """Seconds between regular checks: the setting, or slower to fit the daily budget."""
        minutes = self._settings()[4]
        clocks = len(self._clocks[0]) if self._clocks else 1
        budget = max(1.0, self.day_limit() * POLL_SHARE)
        return int(max(60 * minutes, 86400 * max(1, clocks) / budget))

    async def check(self, force: bool = False, admin: bool = False) -> None:
        """Refresh who's clocked in. Failures are kept in last_error, never raised.
        force: someone is signing in (at most every FORCE_GAP seconds).
        admin: Test connection, which also clears a halt."""
        async with self._lock:
            now = time.time()
            gap = min(MAX_FORCE_GAP, FORCE_GAP * 2 ** min(self.failures, 5))
            if force and now - self._last_attempt < gap:
                return
            if force and self.usage()["count"] >= self.daily_cap() * FORCED_SHARE:
                return   # the rest of today's share is for the scheduled checks
            self._last_attempt = now   # paced from here, even if nothing is sent below
            if admin:
                self.halted = None
            elif self.halted:
                return
            if now < self.paused_until:
                self.last_error = self.pause_reason
                return
            try:
                self.clocked_in = await self.fetch_clocked_in()
                self.checked_at = time.time()
                self.last_error = None
                self.failures = 0
            except Halt as err:
                self._halt(str(err))
                self.last_error = str(err)
            except ConnecteamError as err:
                self.failures += 1
                if self.last_error != str(err):
                    log.warning("Connecteam: %s", err)
                self.last_error = str(err)
            except Exception as err:  # noqa: BLE001 - a malformed reply must not reach a request
                self.failures += 1
                log.warning("Connecteam sent a reply Staff Access couldn't read: %r", err)
                self.last_error = "Connecteam sent a reply Staff Access couldn't read."

    def status(self, user_id: str | None) -> str:
        """'in', 'out', or 'unknown' (no answer since well past when the next check was due)."""
        if not self.checked_at or time.time() - self.checked_at > self.interval() + STALE_MARGIN:
            return "unknown"
        return "in" if user_id and str(user_id) in self.clocked_in else "out"

    def fresh(self, max_age: float) -> bool:
        return bool(self.checked_at) and time.time() - self.checked_at < max_age

    def forget(self) -> None:
        """Settings changed or Connecteam was turned off: start from scratch (caps and pauses
        for the account's rate limits stay)."""
        self._token = None
        self._last_token_try = 0.0
        self._clocks = None
        self._users = None
        self.clocked_in = set()
        self.checked_at = None
        self.last_error = None
        self.halted = None
        self.failures = 0
        self._last_attempt = 0.0
        self.next_due = None
        self._wake.set()

    async def run(self) -> None:
        """Background loop: regular checks while Connecteam is on and someone needs them."""
        while True:
            self._wake.clear()
            wait = 300   # nothing to do: look again in a while, or when the settings change
            try:
                if self._settings()[0] and self._wanted():
                    now = time.time()
                    if self.next_due is None:
                        self.next_due = now   # just started, or the settings changed
                    if now >= self.next_due:
                        # An on-demand check shortly before this one already did the job.
                        if not (self.checked_at and self.checked_at >= self.next_due - SKIP_WINDOW):
                            await self.check()
                        self.next_due += self.interval()
                        if self.next_due <= time.time():   # behind (asleep, paused): start over
                            self.next_due = time.time() + self.interval()
                    wait = max(1, self.next_due - time.time())
                else:
                    self.next_due = None
            except Exception as err:  # noqa: BLE001 - the loop must keep running
                log.warning("Connecteam check failed: %s", err)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=min(wait, 300))
            except asyncio.TimeoutError:
                pass


def _next_utc_midnight() -> float:
    now = time.time()
    return now - now % 86400 + 86400


def _first_list(data: dict, preferred: str) -> list:
    """The list in a Connecteam reply: data[preferred] if present, else the first list found."""
    if isinstance(data.get(preferred), list):
        return data[preferred]
    for value in data.values():
        if isinstance(value, list):
            return value
    return []


def _is_open(shift: dict, now: float) -> bool:
    end = shift.get("end")
    if isinstance(end, dict) and end.get("timestamp"):
        return False
    if end not in (None, {}) and not isinstance(end, dict):
        return False
    start = shift.get("start")
    started = start.get("timestamp") if isinstance(start, dict) else None
    if not isinstance(started, (int, float)):
        return False
    return now - MAX_SHIFT_HOURS * 3600 <= started <= now + 300
