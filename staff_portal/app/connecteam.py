"""Connecteam: pick staff from the Connecteam user list, and only let linked people sign in while
they're clocked in.

Uses Connecteam's OAuth 2.0 client-credentials flow (Integration Center -> OAuth 2.0), so the
add-on only ever holds a client ID and secret it was given, never anyone's password. Scopes
needed: users.read, time_clock.read (read only: Staff Access and the HR portal never write to
Connecteam). A user's kiosk code (users.read) can become their PIN. The token is asked for as
Connecteam documents it (POST /oauth/v1/token, HTTP Basic, grant_type=client_credentials, no
scope: it carries all of the app's scopes, which its reply lists and Test connection shows). It
lasts 24 hours and is renewed 5 minutes early; one token is kept, for one set of keys.

The staff sync (main.py) adds every active Connecteam user as a person, using the same cached
user list (one request, at most hourly) and the smart groups each user is in. The list of smart
groups itself (for choosing a department's group) is cached for SMART_GROUPS_TTL.

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
- Hard caps of its own, counted across restarts: MINUTE_CAP requests a minute and DAILY_CAP a
  day (never more than DAILY_SHARE of the account's daily limit, so the account's other
  integrations always keep the rest: 1,000 a day on Expert).
- It reads Connecteam's x-ratelimit headers and pauses when the account (not just this add-on)
  is running low, honors 429s with exponential backoff, and backs off after errors.
- A refused key or permission (401 or 403, after one try with a fresh token) stops automatic
  requests, with Connecteam's own words. The stop is tried again after HALT_RETRY, at most
  HALT_RETRIES times a day; then it waits until the settings are saved or Test connection is
  clicked, so it never hammers Connecteam with requests that will fail.

If Connecteam can't be asked, the last answer is kept until STALE_MARGIN after the next check
was due, after which clocked-in-only people can't sign in or use controls until it answers.

The HR portal (hr/) reads people through this same connection, in one smaller lane of its own
inside these caps (hr_room and hr_spend): at most HR_MINUTE_CAP a minute and HR_DAILY_CAP a day,
and nothing once Staff Access has used HR_STOP_SHARE of its daily cap, so clock checks always
have room. Its people come from the same cached user list (users(), which also carries mobile
numbers, emails and profile fields). A refusal of an HR request raises HRRefused, and a "too
many requests" pauses only HR's lane (hr_too_many): neither ever stops or pauses Staff Access's
own requests, so the clock checks that let clocked-in-only staff use controls keep running.
Only the account itself running low (its rate-limit headers) pauses everyone, as it always has.

Reports and tasks (ctdata.py) ask through a lane of their own in the same way (reports_room and
reports_spend): at most REPORTS_MINUTE_CAP a minute and REPORTS_DAILY_CAP a day, and nothing once
Staff Access has used REPORTS_STOP_SHARE of its daily cap. A refusal raises ReportsRefused and a
"too many requests" pauses only that lane (reports_too_many); the clock checks carry on.

How the shares add up (Expert: 100 a minute and 10,000 a day for the whole account): Staff
Access takes at most 60 a minute and 9,000 a day (the owner's choice). Inside that, the HR lane
takes at most 6 a minute and 500 a day and the reports lane (reports, and tasks: their checks are
planned at about 1,500 a day, with at most 200 changes sent) 40 a minute and 3,000 a day; both
stop once Staff Access has used half of its day, and together they never take the last
CLOCK_MINUTE_KEEP (10) of a minute. So the clock checks always have at least 10 a minute and
4,500 a day, and the account's other integrations keep 40 a minute and 1,000 a day.
"""

import asyncio
import collections
import json
import logging
import os
import re
import time

import aiohttp

log = logging.getLogger("staff_portal")

REGIONS = {"global": "https://api.connecteam.com", "au": "https://api-au.connecteam.com"}
API_OVERRIDE = os.environ.get("CONNECTEAM_API", "").rstrip("/")   # for testing

STALE_MARGIN = 15 * 60       # trust the last answer this long past the next check when Connecteam is down
FORCE_GAP = 30               # an on-demand check (sign-in or control) at most this often
SKIP_WINDOW = 20 * 60        # an on-demand check this close before an hourly check replaces it
MINUTE_CAP = 60              # requests a minute, at most (Expert allows 100 for the whole account)
DAILY_CAP = 9000             # requests a day, at most...
DAILY_SHARE = 0.9            # ...and never more than this share of the account's daily limit
POLL_SHARE = 0.15            # ...of which regular checks are planned to use at most this much
DEFAULT_DAY_LIMIT = 10_000   # Expert plan, until Connecteam's headers say otherwise
LOW_DAY_SHARE = 0.10         # pause when the account has less than this share left today
LOW_MINUTE_LEFT = 10         # pause for the minute when the account has fewer left than this
CLOCKS_TTL = 6 * 3600        # how long the list of time clocks is reused
USERS_TTL = 5 * 60           # how long the user list is reused
SMART_GROUPS_TTL = 6 * 3600  # how long the list of smart groups is reused
MAX_USER_PAGES = 100         # 50,000 users; guards against a server that ignores the offset
MAX_GROUPS = 5000            # smart groups kept from one reply
MAX_FORCE_GAP = 15 * 60      # on-demand checks slow to this while Connecteam keeps failing
FORCED_SHARE = 0.75          # on-demand checks stop at this share of the daily cap, keeping the
                             # rest for the scheduled ones
MAX_RETRY_AFTER = 24 * 3600  # never wait longer than this when Connecteam asks us to slow down
MAX_SHIFT_HOURS = 24         # an open shift older than this doesn't count (a forgotten clock-out)
HR_MINUTE_CAP = 6            # the HR portal's lane: requests a minute...
HR_DAILY_CAP = 500           # ...and a day...
HR_STOP_SHARE = 0.5          # ...and none once Staff Access has used this share of its daily cap
REPORTS_MINUTE_CAP = 40      # reports and tasks (ctdata.py): requests a minute...
REPORTS_DAILY_CAP = 3000     # ...and a day...
REPORTS_STOP_SHARE = 0.5     # ...and none once Staff Access has used this share of its daily cap
CLOCK_MINUTE_KEEP = 10       # HR and reports together never take the last this many of a minute
FIELDS_TTL = 24 * 3600       # how long the list of profile fields (custom fields) is reused
MAX_FIELDS = 100             # profile fields kept for one user (in memory only, never stored)
PAGE = 500                   # results a page (Connecteam's most; its default is only 10)
HALT_RETRY = 30 * 60         # a stop after a refusal is tried again after this...
HALT_RETRIES = 3             # ...at most this many times a day (UTC), then waits for Save or Test
MAX_SAID = 300               # characters of Connecteam's own words kept from a refusal

# The permission each part of Connecteam's API needs. Staff Access needs users.read and
# time_clock.read; reports and tasks also need schedule.read and forms.read (jobs.read and
# time_off.read are optional: job names, and the names of time off policies).
SCOPES = {"/users/": "users.read", "/time-clock/": "time_clock.read", "/scheduler/": "schedule.read",
          "/jobs/": "jobs.read", "/forms/": "forms.read", "/time-off/": "time_off.read",
          "/tasks/": "quick_tasks.read"}
# Writes (the task sync, ctsync.py): Quick Tasks are written with quick_tasks.write and deleted
# with quick_tasks.delete; a form submission's manager fields are written with forms.write.
WRITE_SCOPES = {("/tasks/", "POST"): "quick_tasks.write", ("/tasks/", "PUT"): "quick_tasks.write",
                ("/tasks/", "DELETE"): "quick_tasks.delete", ("/forms/", "PUT"): "forms.write"}
REPORT_SCOPES = ("schedule.read", "forms.read")
APP_SCOPES = ("users.read, time_clock.read, schedule.read and forms.read (and jobs.read and "
              "time_off.read for job and time off names)")


class ConnecteamError(Exception):
    pass


class Halt(ConnecteamError):
    """Credentials or scopes rejected: don't retry on our own."""


class HRRefused(ConnecteamError):
    """Connecteam refused an HR portal request (a missing permission or plan feature). Only the HR
    side stops what it was doing; Staff Access's own requests carry on."""


class HRBusy(ConnecteamError):
    """The HR portal's lane is used up for now (this minute, today, or Staff Access needs the
    rest), or paused, or Staff Access's connection is stopped: try again later. budget: the
    caller waits without counting a try."""
    budget = True


class ReportsRefused(ConnecteamError):
    """Connecteam refused a reports request (a missing scope or plan feature). Only reports and
    tasks stop; Staff Access's own requests carry on. said: Connecteam's own words, if any."""

    def __init__(self, message: str, said: str | None = None):
        super().__init__(message)
        self.said = said


class ReportsBusy(ConnecteamError):
    """The reports lane is used up for now, or paused. wait: seconds until a request fits again
    this minute (None: not soon)."""
    budget = True

    def __init__(self, message: str, wait: float | None = None):
        super().__init__(message)
        self.wait = wait


class Rejected(ConnecteamError):
    """Connecteam answered a request with an error (other than a refusal or "too many"): status
    is its HTTP status, so a write knows the request arrived (a 4xx was never carried out)."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class OwnTooMany(ReportsBusy):
    """A 429 to a request that pauses only its own caller (the task sync), not the reports lane.
    retry_after: Connecteam's Retry-After, in seconds, when it gave one."""

    def __init__(self, message: str, retry_after: float | None):
        super().__init__(message, None)
        self.retry_after = retry_after


class Connecteam:
    def __init__(self, get_settings, wanted, usage_store, user_agent: str, hr_usage_store=None,
                 reports_usage_store=None):
        # get_settings() -> (enabled, client_id, client_secret, region, poll_minutes)
        self._get_settings = get_settings
        self._wanted = wanted                  # () -> whether anyone needs the clock checked
        self._load_usage, self._save_usage = usage_store
        # The HR portal's own count for today (kept in memory if no store is given).
        held = {}
        self._load_hr_usage, self._save_hr_usage = hr_usage_store or (
            lambda: held.get("u", {}), lambda u: held.update(u=u))
        self._hr_recent = collections.deque()   # times of HR requests in the last minute
        self.hr_paused_until = 0.0              # no HR requests before this (HR's own 429s)
        self.hr_pause_reason: str | None = None
        self.hr_failures = 0
        # Reports and tasks (ctdata.py): their own count for today, kept like HR's.
        held_reports = {}
        self._load_reports_usage, self._save_reports_usage = reports_usage_store or (
            lambda: held_reports.get("u", {}), lambda u: held_reports.update(u=u))
        self._reports_recent = collections.deque()
        self.reports_paused_until = 0.0
        self.reports_pause_reason: str | None = None
        self.reports_failures = 0
        self.on_forget: list = []               # called by forget() (ctdata drops its copies)
        self._fields: tuple[list[dict], float] | None = None
        self._user_agent = user_agent
        self.session: aiohttp.ClientSession | None = None
        self._token: tuple[str, float] | None = None
        self._token_for: tuple | None = None
        self._last_token_try = 0.0
        self._clocks: tuple[list[str], float] | None = None
        self._users: tuple[list[dict], float] | None = None
        self._groups: tuple[list[dict], float] | None = None
        self._token_lock = asyncio.Lock()
        self._recent = collections.deque()      # times of requests in the last minute
        self.clocked_in: set[str] = set()
        self.checked_at: float | None = None    # last successful check
        self.last_error: str | None = None
        self.halted: str | None = None          # why automatic requests stopped (bad keys/scopes)
        self.halted_at = 0.0
        self._halt_tries = ["", 0]              # [UTC day, stops lifted by themselves that day]
        self.scopes: set[str] | None = None     # the permissions the last token carries
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
        return max(50, min(DAILY_CAP, int(self.day_limit() * DAILY_SHARE)))

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

    def hr_usage(self) -> dict:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        u = self._load_hr_usage()
        return u if isinstance(u, dict) and u.get("day") == today and isinstance(
            u.get("count"), int) else {"day": today, "count": 0}

    def hr_room(self, n: int = 1) -> None:
        """Whether n more HR portal requests fit right now, in its lane and in Staff Access's
        caps; raises HRBusy if not. Spends nothing (a flow of several requests checks first, so
        it's never left half done). Pauses for Connecteam's limits hold for HR as well."""
        now = time.time()
        for recent in (self._hr_recent, self._recent):
            while recent and now - recent[0] > 60:
                recent.popleft()
        if now < self.hr_paused_until:
            raise HRBusy(self.hr_pause_reason or "Waiting before asking Connecteam again.")
        if now < self.paused_until:
            raise HRBusy(f"Connecteam requests are paused: {self.pause_reason}")
        if len(self._hr_recent) + n > HR_MINUTE_CAP:
            raise HRBusy("The HR portal has made enough Connecteam requests for this minute. It "
                         "tries again shortly.")
        if self.hr_usage()["count"] + n > HR_DAILY_CAP:
            raise HRBusy(f"The HR portal used its {HR_DAILY_CAP} Connecteam requests for today. "
                         "It continues after midnight UTC.")
        if self.usage()["count"] + n > self.daily_cap() * HR_STOP_SHARE:
            raise HRBusy("Staff Access has used half of its Connecteam requests for today, so the "
                         "HR portal waits until after midnight UTC and leaves the rest for the "
                         "time clock.")
        if len(self._recent) + n > MINUTE_CAP or self._side_minute() + n > MINUTE_CAP - CLOCK_MINUTE_KEEP:
            raise HRBusy("Staff Access has made enough Connecteam requests for this minute. The "
                         "HR portal tries again shortly.")

    def _side_minute(self) -> int:
        """HR and reports requests in the last minute (call after the deques are trimmed)."""
        now = time.time()
        for recent in (self._hr_recent, self._reports_recent):
            while recent and now - recent[0] > 60:
                recent.popleft()
        return len(self._hr_recent) + len(self._reports_recent)

    def hr_spend(self) -> None:
        """Count an HR portal request against its own lane and then Staff Access's caps (so it
        counts there too), or refuse it with HRBusy. While Staff Access's connection is stopped
        (a refusal), HR waits too, without repeating that refusal's words (they're about
        another request)."""
        if self.stop_reason():
            raise HRBusy("Staff Access's Connecteam connection is stopped after a refusal (see "
                         "Settings, Connections), so the HR portal waits until it works again.")
        self.hr_room(1)
        self._spend()
        self._hr_recent.append(time.time())
        u = self.hr_usage()
        u["count"] += 1
        self._save_hr_usage(u)

    def hr_status(self) -> dict:
        """HR's lane, for the dashboard and the HR tab."""
        now = time.time()
        return {"used_today": self.hr_usage()["count"], "daily_cap": HR_DAILY_CAP,
                "minute_cap": HR_MINUTE_CAP,
                "paused": self.hr_pause_reason if self.hr_paused_until > now else None,
                "paused_until": self.hr_paused_until if self.hr_paused_until > now else None}

    def hr_too_many(self, resp: aiohttp.ClientResponse) -> HRBusy:
        """A 429 to an HR request: only HR's lane waits (backing off), never Staff Access's clock
        checks. The account's own rate-limit headers still pause everyone when it runs low."""
        self.hr_failures += 1
        wait = min(3600, 60 * 2 ** min(self.hr_failures - 1, 6))
        try:
            wait = max(wait, min(MAX_RETRY_AFTER, int(float(resp.headers.get("Retry-After", 0)))))
        except (ValueError, OverflowError):
            pass
        self.hr_paused_until = max(self.hr_paused_until, time.time() + wait)
        self.hr_pause_reason = ("Connecteam asked for fewer requests, so the HR portal waits "
                                f"{max(1, round(wait / 60))} min before asking again.")
        log.info("Connecteam: %s", self.hr_pause_reason)
        return HRBusy(self.hr_pause_reason)

    def reports_usage(self) -> dict:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        u = self._load_reports_usage()
        return u if isinstance(u, dict) and u.get("day") == today and isinstance(
            u.get("count"), int) else {"day": today, "count": 0}

    def reports_room(self, n: int = 1) -> None:
        """Whether n more reports requests fit right now, in their lane and in Staff Access's
        caps; raises ReportsBusy if not (with how long until the minute has room, when that's
        all it is). Spends nothing."""
        now = time.time()
        for recent in (self._reports_recent, self._hr_recent, self._recent):
            while recent and now - recent[0] > 60:
                recent.popleft()
        if self.stop_reason():
            raise ReportsBusy("Staff Access's Connecteam connection is stopped after a refusal (see "
                              "Settings, Connections), so reports wait until it works again.")
        if now < self.reports_paused_until:
            raise ReportsBusy(self.reports_pause_reason or "Waiting before asking Connecteam again.")
        if now < self.paused_until:
            raise ReportsBusy(f"Connecteam requests are paused: {self.pause_reason}")
        if self.reports_usage()["count"] + n > REPORTS_DAILY_CAP:
            raise ReportsBusy(f"Reports used their {REPORTS_DAILY_CAP} Connecteam requests for "
                              "today. They continue after midnight UTC.")
        if self.usage()["count"] + n > self.daily_cap() * REPORTS_STOP_SHARE:
            raise ReportsBusy("Staff Access has used half of its Connecteam requests for today, so "
                              "reports wait until after midnight UTC and leave the rest for the "
                              "time clock.")
        side = sorted([*self._hr_recent, *self._reports_recent])
        for recent, cap in ((self._reports_recent, REPORTS_MINUTE_CAP), (self._recent, MINUTE_CAP),
                            (side, MINUTE_CAP - CLOCK_MINUTE_KEEP)):
            if len(recent) + n > cap:
                wait = 60 - (now - recent[len(recent) + n - cap - 1]) + 0.5 if n <= cap else None
                raise ReportsBusy("Reports have made enough Connecteam requests for this minute. "
                                  "Try again in a minute.", wait)

    def reports_spend(self) -> None:
        """Count a reports request against its lane and then Staff Access's caps, or refuse it
        with ReportsBusy."""
        self.reports_room(1)
        self._spend()
        self._reports_recent.append(time.time())
        u = self.reports_usage()
        u["count"] += 1
        self._save_reports_usage(u)

    def reports_too_many(self, resp: aiohttp.ClientResponse) -> ReportsBusy:
        """A 429 to a reports request: only the reports lane waits (backing off)."""
        self.reports_failures += 1
        wait = min(3600, 60 * 2 ** min(self.reports_failures - 1, 6))
        try:
            wait = max(wait, min(MAX_RETRY_AFTER, int(float(resp.headers.get("Retry-After", 0)))))
        except (ValueError, OverflowError):
            pass
        self.reports_paused_until = max(self.reports_paused_until, time.time() + wait)
        self.reports_pause_reason = ("Connecteam asked for fewer requests, so reports wait "
                                     f"{max(1, round(wait / 60))} min before asking again.")
        log.info("Connecteam: %s", self.reports_pause_reason)
        return ReportsBusy(self.reports_pause_reason)

    def _lane_spend(self, hr: bool, reports: bool) -> None:
        if hr:
            self.hr_spend()
        elif reports:
            self.reports_spend()
        else:
            self._spend()

    def _lane_too_many(self, resp, hr: bool, reports: bool) -> ConnecteamError:
        if hr:
            return self.hr_too_many(resp)
        return self.reports_too_many(resp) if reports else self._too_many(resp)

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

    async def _access_token(self, hr: bool = False, reports: bool = False) -> str:
        # One at a time: a time clock check and the staff sync starting together must share one
        # new token, not have the second turned away by the minute's wait between token requests.
        # hr, reports: the HR portal or a report needs it, so a new one is counted in that lane.
        async with self._token_lock:
            return await self._new_access_token(hr, reports)

    async def _new_access_token(self, hr: bool = False, reports: bool = False) -> str:
        creds = self._credentials()
        if self._token and self._token_for == creds and self._token[1] > time.time():
            return self._token[0]
        if time.time() - self._last_token_try < 60:
            raise (HRBusy if hr else ReportsBusy if reports else ConnecteamError)(
                "Waiting a minute before asking Connecteam for a new access token.")
        self._lane_spend(hr, reports)   # refused: nothing was asked, so no wait
        self._last_token_try = time.time()
        self._token = None   # never a token of other keys, even if this fails
        try:
            async with self.session.post(
                f"{self._api()}/oauth/v1/token", data={"grant_type": "client_credentials"},
                headers={"Authorization": aiohttp.encode_basic_auth(creds[0], creds[1]),
                         "Accept": "application/json"},
            ) as resp:
                self._read_limits(resp)
                if resp.status == 429:
                    raise self._lane_too_many(resp, hr, reports)
                if resp.status in (400, 401, 403):
                    said = await _said(resp)
                    # The keys themselves: they're the one connection's, whoever asked.
                    raise _refused(hr, reports, said,
                                   f"Connecteam didn't accept the Client ID and Secret (HTTP "
                                   f"{resp.status}){_quote(said)}. Check them in Settings, Connections.")
                body = await resp.json(content_type=None)
                if resp.status >= 400 or not isinstance(body, dict) or not body.get("access_token"):
                    raise ConnecteamError(f"Connecteam couldn't issue an access token (HTTP {resp.status}).")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise ConnecteamError(f"Can't reach Connecteam: {err or type(err).__name__}") from err
        ttl = body.get("expires_in") if isinstance(body.get("expires_in"), (int, float)) else 86400
        ttl = min(max(ttl, 360), 86400) if ttl == ttl else 86400   # sane, and not NaN
        scope = body.get("scope")
        self.scopes = set(scope.split()) if isinstance(scope, str) else None
        self._token = (str(body["access_token"]), time.time() + ttl - 300)
        self._token_for = creds
        return self._token[0]

    async def _get(self, path: str, params: dict | None = None, hr: bool = False,
                   reports: bool = False) -> dict:
        """hr: an HR portal request (its lane; a refusal raises HRRefused, never Halt). reports:
        a reports or tasks request (ctdata.py; its lane, and a refusal raises ReportsRefused,
        never Halt). A 401 or 403 is asked once more with a fresh token (an expired or replaced
        one is refused too) before it counts as a refusal."""
        return await self._request("GET", path, params, None, hr, reports)

    async def _request(self, method: str, path: str, params=None, body=None, hr: bool = False,
                       reports: bool = False, own_pause: bool = False) -> dict:
        """One request (_get's rules). body: JSON sent with a write. own_pause: a 429 raises
        OwnTooMany and leaves the lane as it is (the task sync pauses itself)."""
        for attempt in (1, 2):
            token = await self._access_token(hr, reports)
            self._lane_spend(hr, reports)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            try:
                async with self.session.request(method, f"{self._api()}{path}", params=params,
                                                json=body, headers=headers) as resp:
                    self._read_limits(resp)
                    if resp.status == 429:
                        if own_pause:
                            try:
                                after = min(float(resp.headers.get("Retry-After", "")), MAX_RETRY_AFTER)
                            except ValueError:
                                after = None
                            raise OwnTooMany("Connecteam asked for fewer requests.", after)
                        raise self._lane_too_many(resp, hr, reports)
                    if resp.status in (401, 403):
                        said = await _said(resp)
                        # A fresh token, once; not for reports when the token says the
                        # permission is missing (it would only throw away the clock's token).
                        if attempt == 1 and not (reports and self.scopes is not None
                                                 and scope_for(path, method) not in self.scopes):
                            self._token = None
                            self._last_token_try = 0.0
                            continue
                        raise _refused(hr, reports, said,
                                       self.refusal(path, resp.status, said, hr, reports, method))
                    answer = await resp.json(content_type=None)
                    if resp.status >= 400:
                        said = _words(answer) or _compact(answer)
                        if method != "GET":
                            log.warning("Connecteam refused %s %s (HTTP %s): %s", method, path,
                                        resp.status, _compact(answer, 600))
                        raise Rejected(f"Connecteam couldn't answer {path} (HTTP {resp.status})"
                                       f"{_quote(said)}.", resp.status)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                raise ConnecteamError(f"Can't reach Connecteam: {err or type(err).__name__}") from err
            if not isinstance(answer, dict):
                if method != "GET" and answer is None:
                    answer = {}
                else:
                    raise ConnecteamError("Connecteam sent an unexpected reply.")
            if hr:
                self.hr_failures = 0
            if reports:
                self.reports_failures = 0
            data = answer.get("data", answer)
            if isinstance(data, dict) and isinstance(answer.get("paging"), dict) and reports:
                data = {**data, "_paging": answer["paging"]}   # where the next page starts
            return data if isinstance(data, dict) else {"items": data}

    def refusal(self, path: str, status: int, said: str, hr: bool = False,
                reports: bool = False, method: str = "GET") -> str:
        """A refusal in plain words: Connecteam's own, then what's missing. A token that lists
        its permissions shows whether it's the permission or the plan."""
        scope = scope_for(path, method)
        who = " for the HR portal" if hr else " for reports" if reports else ""
        text = f"Connecteam refused {path}{who} (HTTP {status}){_quote(said)}."
        if self.scopes is not None and scope not in self.scopes:
            return (f"{text} The Connecteam app doesn't have the {scope} permission (it has: "
                    f"{', '.join(sorted(self.scopes)) or 'none'}). An app's permissions can't be "
                    f"changed in Connecteam: create a new OAuth app with {APP_SCOPES}, and enter "
                    "its keys in Settings, Connections.")
        if self.scopes is not None:
            return (f"{text} The app has {scope}, so it's your Connecteam plan: API access needs "
                    "the Expert plan or higher" + (" on the Operations hub (the time clock)"
                                                   if scope == "time_clock.read" else "") + ".")
        return (f"{text} Check that the OAuth app has the {scope} permission, and that your plan "
                "includes API access (Expert or higher).")

    async def _call(self, coro_fn, *args):
        """An admin-initiated request: same caps, and a halt is recorded."""
        self._check_halt()
        try:
            return await coro_fn(*args)
        except Halt as err:
            self._halt(str(err))
            raise

    def _check_halt(self) -> None:
        if self.stop_reason():
            due = self.retry_at()
            again = f"tried again by itself in {max(1, round((due - time.time()) / 60))} min, " \
                if due else ""
            raise ConnecteamError(f"{self.halted} (Automatic checks are stopped: {again}or when the "
                                  "Connecteam settings are saved or Test connection is clicked.)")

    def _halt(self, reason: str) -> None:
        if self.halted != reason:
            log.warning("Connecteam: %s Automatic checks stopped.", reason)
        self.halted = reason
        self.halted_at = time.time()

    def retry_at(self) -> float | None:
        """When a stop is tried again by itself (None: not stopped, or no tries left today)."""
        today = time.strftime("%Y-%m-%d", time.gmtime())
        tries = self._halt_tries[1] if self._halt_tries[0] == today else 0
        return self.halted_at + HALT_RETRY if self.halted and tries < HALT_RETRIES else None

    def stop_reason(self) -> str | None:
        """Why automatic requests are stopped (None: they aren't). The stop lifts by itself
        after HALT_RETRY, at most HALT_RETRIES times a day."""
        due = self.retry_at()
        if due is not None and time.time() >= due:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            if self._halt_tries[0] != today:
                self._halt_tries = [today, 0]
            self._halt_tries[1] += 1
            log.info("Connecteam: trying again after the stop (%d of %d today).",
                     self._halt_tries[1], HALT_RETRIES)
            self.halted = None
            self._token = None
            self._last_token_try = 0.0
        return self.halted

    async def users(self, max_age: float = USERS_TTL, hr: bool = False,
                    reports: bool = False) -> list[dict]:
        """Active Connecteam users as [{"id", "name", "kiosk_code", "groups", "mobile", "email",
        "created_at", "fields"}], sorted by name. kiosk_code is None unless it's usable as a PIN
        (4 to 8 digits); groups are the IDs (as text) of the smart groups they're in; fields are
        their profile fields {field ID: value} (kept in memory only). hr: the HR portal is asking
        (its lane, and a refusal never stops Staff Access's own requests); reports: the same for
        reports and tasks."""
        if self._users and time.time() - self._users[1] < max_age:
            return self._users[0]
        if hr or reports:
            return await self._fetch_users(hr=hr, reports=reports)
        return await self._call(self._fetch_users)

    async def archived_users(self, reports: bool = False) -> list[dict]:
        """Archived Connecteam users (as users() lists them), for the HR portal's daily check, or
        reports (reports: their lane, for the names of people who have left). Never cached here."""
        return await self._fetch_users(hr=not reports, archived=True, reports=reports)

    async def _fetch_users(self, hr: bool = False, archived: bool = False,
                           reports: bool = False) -> list[dict]:
        found, offset = [], 0
        while True:
            params = {"limit": PAGE, "offset": offset}
            if archived:
                params["userStatus"] = "archived"   # documented: active (the default), archived, all
            data = await self._get("/users/v1/users", params, hr=hr, reports=reports)
            page = _first_list(data, "users")
            for u in page:
                if (not isinstance(u, dict) or u.get("userId") is None
                        or bool(u.get("isArchived")) != archived):
                    continue
                name = " ".join(str(u.get(k) or "").strip() for k in ("firstName", "lastName")).strip()
                code = str(u.get("kioskCode") or "").strip()
                groups = u.get("smartGroupsIds")
                found.append({"id": str(u["userId"]), "name": name or f"User {u['userId']}",
                              "kiosk_code": code if code.isdigit() and 4 <= len(code) <= 8 else None,
                              "groups": [str(g) for g in groups[:MAX_GROUPS]
                                         if isinstance(g, int) and not isinstance(g, bool)]
                              if isinstance(groups, list) else [],
                              "mobile": _short_text(u.get("phoneNumber"), 40),
                              "email": _short_text(u.get("email"), 200),
                              "created_at": _timestamp(u.get("createdAt")),
                              "fields": _profile_fields(u.get("customFields")),
                              "field_names": _field_names(u.get("customFields"))})
            if len(page) < PAGE or offset >= PAGE * (MAX_USER_PAGES - 1):
                break
            offset += PAGE
        found.sort(key=lambda u: u["name"].lower())
        if not archived:
            self._users = (found, time.time())
        return found

    async def profile_fields(self, max_age: float = FIELDS_TTL) -> list[dict]:
        """Connecteam's profile fields (custom fields) as [{"id", "name", "type"}], for the HR
        portal: which one holds the start date, and the direct manager. Through the HR lane,
        reused for a day. One page of PAGE (Connecteam's default is only 10)."""
        if self._fields and time.time() - self._fields[1] < max_age:
            return self._fields[0]
        data = await self._get("/users/v1/custom-fields", {"limit": PAGE, "offset": 0}, hr=True)
        found = []
        for f in _first_list(data, "customFields")[:MAX_FIELDS * 5]:
            fid = f.get("id", f.get("customFieldId")) if isinstance(f, dict) else None
            if isinstance(fid, bool) or not isinstance(fid, (int, str)) or not str(fid).strip():
                continue
            found.append({"id": str(fid)[:40], "name": _short_text(f.get("name"), 80) or f"Field {fid}",
                          "type": _short_text(f.get("type"), 40) or ""})
        self._fields = (found, time.time())
        return found

    async def smart_groups(self, max_age: float = SMART_GROUPS_TTL, hr: bool = False) -> list[dict]:
        """Connecteam's smart groups as [{"id", "name", "users"}], sorted by name (cached). hr: the
        HR portal is asking (its lane; a refusal never stops Staff Access's requests)."""
        if self._groups and time.time() - self._groups[1] < max_age:
            return self._groups[0]
        if hr:
            return await self._fetch_smart_groups(hr=True)
        return await self._call(self._fetch_smart_groups)

    async def _fetch_smart_groups(self, hr: bool = False) -> list[dict]:
        data = await self._get("/users/v1/smart-groups", hr=hr)
        found = []
        for g in _first_list(data, "groups")[:MAX_GROUPS]:
            if not isinstance(g, dict) or isinstance(g.get("id"), bool) or not isinstance(g.get("id"), int):
                continue
            users = g.get("numberOfUsers")
            found.append({"id": str(g["id"]), "name": str(g.get("name") or f"Group {g['id']}")[:80],
                          "users": users if isinstance(users, int) and not isinstance(users, bool)
                          else None})
        found.sort(key=lambda g: g["name"].lower())
        self._groups = (found, time.time())
        return found

    def known_group_names(self) -> dict[str, str]:
        """{smart group ID: name} from the last list read, of any age (never asks Connecteam)."""
        return {g["id"]: g["name"] for g in self._groups[0]} if self._groups else {}

    def known_groups(self) -> dict[str, dict]:
        """{smart group ID: {id, name, users}} from the last list read, of any age (never asks)."""
        return {g["id"]: g for g in self._groups[0]} if self._groups else {}

    async def kiosk_code(self, user_id: str) -> str | None:
        """One user's kiosk code, if it's usable as a PIN (from the cached list when recent)."""
        for u in await self.users():
            if u["id"] == str(user_id):
                return u["kiosk_code"]
        raise ConnecteamError("That person isn't an active Connecteam user.")

    async def time_clock_ids(self, max_age: float = CLOCKS_TTL, reports: bool = False) -> list[str]:
        if self._clocks and time.time() - self._clocks[1] < max_age:
            return self._clocks[0]
        data = await self._get("/time-clock/v1/time-clocks", reports=reports)
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
                self.lift_stop()
            elif self.stop_reason():
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
        self._groups = None
        self._fields = None
        self.clocked_in = set()
        self.checked_at = None
        self.last_error = None
        self.lift_stop()
        self.scopes = None
        self.failures = 0
        self._last_attempt = 0.0
        self.next_due = None
        self._wake.set()
        for forget in self.on_forget:
            forget()

    def lift_stop(self) -> None:
        """Saved settings or Test connection: no stop, and its tries for today start over."""
        self.halted = None
        self._halt_tries = ["", 0]

    async def diagnose(self) -> list[dict]:
        """Test connection, step by step, each with Connecteam's own words: a new access token
        (and the permissions it carries), the users, the time clock, the smart groups. Lifts a
        stop first, and a refusal stops automatic requests again. [{"check", "ok", "detail"}]"""
        steps = []

        async def step(name: str, fn) -> bool:
            try:
                steps.append({"check": name, "ok": True, "detail": await fn()})
            except Halt as err:
                self._halt(str(err))
                steps.append({"check": name, "ok": False, "detail": str(err)})
            except ConnecteamError as err:
                steps.append({"check": name, "ok": False, "detail": str(err)})
            return steps[-1]["ok"]

        async def token() -> str:
            self._token, self._last_token_try = None, 0.0
            await self._access_token()
            if self.scopes is None:
                return "Connecteam issued an access token."
            missing = [s for s in ("users.read", "time_clock.read") if s not in self.scopes]
            if missing:
                raise ConnecteamError(
                    f"Connecteam issued an access token, but the app lacks {' and '.join(missing)} "
                    f"(it has: {', '.join(sorted(self.scopes)) or 'none'}). An app's permissions "
                    f"can't be changed in Connecteam: create a new OAuth app with {APP_SCOPES}, "
                    "and enter its keys here.")
            lacks = [s for s in REPORT_SCOPES if s not in self.scopes]
            return (f"Connecteam issued an access token with: {', '.join(sorted(self.scopes))}."
                    + (f" Reports and tasks also need {' and '.join(lacks)}." if lacks else ""))

        async def users() -> str:
            return f"Read {len(await self._fetch_users())} active users."

        async def clock() -> str:
            clocks = await self.time_clock_ids(max_age=0)
            self.clocked_in = await self.fetch_clocked_in()
            self.checked_at, self.last_error, self.failures = time.time(), None, 0
            return f"Read {len(clocks)} time clock(s); {len(self.clocked_in)} people are clocked in now."

        async def groups() -> str:
            return f"Read {len(await self._fetch_smart_groups())} smart groups."

        async with self._lock:
            self.lift_stop()
            if await step("Getting an access token", token) or self._token:
                for name, fn in (("Reading users", users), ("Reading the time clock", clock),
                                 ("Reading smart groups", groups)):
                    await step(name, fn)
            if not all(s["ok"] for s in steps) and self.last_error is None:
                self.last_error = next(s["detail"] for s in steps if not s["ok"])
        return steps

    async def run(self) -> None:
        """Background loop: regular checks while Connecteam is on and someone needs them."""
        while True:
            self._wake.clear()
            wait = 300   # nothing to do: look again in a while, or when the settings change
            try:
                if self._settings()[0] and self._wanted():
                    now = time.time()
                    if self.next_due is None or (self.halted and self.stop_reason() is None):
                        self.next_due = now   # just started, the settings changed, or a stop lifted
                    if now >= self.next_due:
                        # An on-demand check shortly before this one already did the job.
                        if not (self.checked_at and self.checked_at >= self.next_due - SKIP_WINDOW):
                            await self.check()
                        self.next_due += self.interval()
                        if self.next_due <= time.time():   # behind (asleep, paused): start over
                            self.next_due = time.time() + self.interval()
                    wait = max(1, self.next_due - time.time())
                    if self.retry_at():
                        wait = min(wait, max(1, self.retry_at() - time.time()))
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


async def _said(resp: aiohttp.ClientResponse) -> str:
    """Connecteam's own words in a refusal (its JSON's detail or message, else the text)."""
    try:
        raw = (await resp.content.read(8192)).decode("utf-8", "replace")
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return ""
    try:
        return _words(json.loads(raw))
    except ValueError:
        return " ".join(re.sub(r"<[^>]*>", " ", raw).split())[:MAX_SAID]


def _words(body, depth: int = 0) -> str:
    """The message in a JSON error reply: detail (text, or validation errors' msg, with where),
    message, error_description, error or errors, looking inside a nested object too."""
    if not isinstance(body, dict) or depth > 2:
        return ""
    for key in ("detail", "message", "error_description", "error", "errors", "title"):
        value = body.get(key)
        if isinstance(value, dict) and value and all(isinstance(v, dict) for v in value.values()):
            # {"body.startTime": {"message": ...}}: each field and what's wrong with it
            value = "; ".join(f"{k}: {v.get('message') or v.get('msg') or _compact(v)}"
                              for k, v in list(value.items())[:5])
        elif isinstance(value, dict):
            value = _words(value, depth + 1)
        if isinstance(value, list):
            value = "; ".join(_one_error(v) for v in value[:5])
        if isinstance(value, (str, int)) and str(value).strip():
            return " ".join(str(value).split())[:MAX_SAID]
    return ""


def _one_error(v) -> str:
    """One validation error: its msg, and where ("body.dueDate") when it says."""
    if not isinstance(v, dict):
        return str(v)
    where = ".".join(str(x) for x in v.get("loc") or [] if isinstance(x, (str, int)))
    text = str(v.get("msg") or v.get("message") or _compact(v))
    return f"{where}: {text}" if where else text


def _compact(body, limit: int = MAX_SAID) -> str:
    """A reply as short JSON, for when it has no message we know (never anything we sent)."""
    try:
        text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(body)
    return " ".join(text.split())[:limit] if body not in (None, {}, []) else ""


def scope_for(path: str, method: str = "GET") -> str:
    """The permission a request to this path needs."""
    if method != "GET":
        found = next((s for (prefix, how), s in WRITE_SCOPES.items()
                      if how == method and path.startswith(prefix)), None)
        if found:
            return found
    return next((s for prefix, s in SCOPES.items() if path.startswith(prefix)), "users.read")


def _refused(hr: bool, reports: bool, said: str, message: str) -> ConnecteamError:
    """A refusal in the asking lane's own kind: only Staff Access's own requests halt."""
    if reports:
        return ReportsRefused(message, said or None)
    return HRRefused(message) if hr else Halt(message)


def _quote(said: str) -> str:
    return f": “{said}”" if said else ""


def _short_text(value, limit: int) -> str | None:
    """A plain one-line text value from a reply (None when there's none)."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = " ".join(str(value).split())[:limit]
    return text or None


def _timestamp(value) -> float | None:
    """A time in a reply (seconds or milliseconds since 1970), or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    value = value / 1000 if value > 1e11 else value
    return float(value) if 0 < value < 1e11 else None


def _profile_fields(raw) -> dict[str, object]:
    """A user's profile fields as {field ID: value}: text, numbers and short lists of them (a
    direct manager can be a list of users), at most MAX_FIELDS. Kept in memory only."""
    found: dict[str, object] = {}
    for f in raw[:MAX_FIELDS * 5] if isinstance(raw, list) else []:
        fid = f.get("customFieldId", f.get("id")) if isinstance(f, dict) else None
        if isinstance(fid, bool) or not isinstance(fid, (int, str)) or len(found) >= MAX_FIELDS:
            continue
        value = f.get("value")
        if isinstance(value, list):
            value = [v if isinstance(v, (str, int, float)) and not isinstance(v, bool)
                     else v.get("userId", v.get("id", v.get("value"))) if isinstance(v, dict) else None
                     for v in value[:20]]
            value = [str(v)[:200] for v in value if v is not None and not isinstance(v, bool)]
        elif isinstance(value, dict):
            value = value.get("userId", value.get("id", value.get("value")))
            value = None if isinstance(value, (bool, dict, list)) else value
        elif isinstance(value, bool) or not isinstance(value, (str, int, float)):
            value = None
        if isinstance(value, str):
            value = value[:200]
        if value not in (None, "", []):
            found[str(fid)[:40]] = value
    return found


def _field_names(raw) -> dict[str, str]:
    """{field ID: name} of the profile fields in a user's record (the users list names them), so
    the HR portal finds its fields even when the list of fields can't be read."""
    found: dict[str, str] = {}
    for f in raw[:MAX_FIELDS] if isinstance(raw, list) else []:
        fid = f.get("customFieldId", f.get("id")) if isinstance(f, dict) else None
        name = _short_text(f.get("name"), 80) if isinstance(f, dict) else None
        if isinstance(fid, (int, str)) and not isinstance(fid, bool) and name:
            found[str(fid)[:40]] = name
    return found


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
