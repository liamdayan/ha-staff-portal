"""Connecteam: pick staff from the Connecteam user list, and only let linked people sign in while
they're clocked in.

Uses Connecteam's OAuth 2.0 client-credentials flow (Integration Center -> OAuth 2.0), so the
add-on only ever holds a client ID and secret it was given, never anyone's password. Scopes
needed: users.read, time_clock.read. A user's kiosk code (users.read) can become their PIN.

Who is clocked in comes from the time clock's time activities: a shift with no end yet is a
person on the clock. The add-on checks every minute (and right away when someone who looks
clocked out tries to sign in), and keeps the last answer for up to STALE_AFTER seconds if
Connecteam can't be reached, after which clock-in-only people can't sign in until it answers.
"""

import asyncio
import logging
import os
import time

import aiohttp

log = logging.getLogger("staff_portal")

API = os.environ.get("CONNECTEAM_API", "https://api.connecteam.com").rstrip("/")
POLL_SECONDS = 60
STALE_AFTER = 15 * 60        # trust the last clock-in list this long when Connecteam is unreachable
FORCE_GAP = 10               # an on-demand check at most this often
MAX_SHIFT_HOURS = 24         # an open shift older than this doesn't count (a forgotten clock-out)


class ConnecteamError(Exception):
    pass


class Connecteam:
    def __init__(self, get_settings):
        self._get_settings = get_settings   # () -> (enabled, client_id, client_secret)
        self.session: aiohttp.ClientSession | None = None
        self._token: tuple[str, float] | None = None
        self._token_for: tuple[str, str] | None = None
        self.clocked_in: set[str] = set()
        self.checked_at: float | None = None    # last successful check
        self.last_error: str | None = None
        self._last_attempt = 0.0
        self._lock = asyncio.Lock()

    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    async def close(self):
        if self.session:
            await self.session.close()

    # ------------------------------------------------------------ API

    def _credentials(self) -> tuple[str, str]:
        enabled, client_id, secret = self._get_settings()
        if not enabled:
            raise ConnecteamError("Connecteam is turned off in Settings.")
        if not client_id or not secret:
            raise ConnecteamError("Enter the Connecteam Client ID and Client Secret in Settings.")
        return client_id, secret

    async def _access_token(self) -> str:
        creds = self._credentials()
        if self._token and self._token_for == creds and self._token[1] > time.time():
            return self._token[0]
        try:
            async with self.session.post(
                f"{API}/oauth/v1/token", data={"grant_type": "client_credentials"},
                auth=aiohttp.BasicAuth(*creds),
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 400 or not isinstance(body, dict) or not body.get("access_token"):
                    raise ConnecteamError("Connecteam didn't accept the Client ID and Secret "
                                          f"(HTTP {resp.status}). Check them in Settings.")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise ConnecteamError(f"Can't reach Connecteam: {err or type(err).__name__}") from err
        ttl = body.get("expires_in") if isinstance(body.get("expires_in"), (int, float)) else 86400
        self._token = (str(body["access_token"]), time.time() + max(60, ttl - 300))
        self._token_for = creds
        return self._token[0]

    async def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in (1, 2):
            token = await self._access_token()
            try:
                async with self.session.get(f"{API}{path}", params=params,
                                            headers={"Authorization": f"Bearer {token}"}) as resp:
                    if resp.status == 401 and attempt == 1:
                        self._token = None   # expired or revoked: get a new one and retry once
                        continue
                    body = await resp.json(content_type=None)
                    if resp.status >= 400:
                        raise ConnecteamError(f"Connecteam refused {path} (HTTP {resp.status}). "
                                              "Check the app's scopes: users and time clock, read.")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                raise ConnecteamError(f"Can't reach Connecteam: {err or type(err).__name__}") from err
            if not isinstance(body, dict):
                raise ConnecteamError("Connecteam sent an unexpected reply.")
            data = body.get("data", body)
            return data if isinstance(data, dict) else {"items": data}
        raise ConnecteamError("Connecteam didn't accept the access token.")

    async def users(self) -> list[dict]:
        """Active Connecteam users as [{"id", "name", "kiosk_code"}], sorted by name.
        kiosk_code is None unless it's usable as a PIN (4 to 8 digits)."""
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
        return sorted(found, key=lambda u: u["name"].lower())

    async def kiosk_code(self, user_id: str) -> str | None:
        """One user's kiosk code, if it's usable as a PIN."""
        for u in await self.users():
            if u["id"] == str(user_id):
                return u["kiosk_code"]
        raise ConnecteamError("That person isn't an active Connecteam user.")

    async def time_clock_ids(self) -> list[str]:
        data = await self._get("/time-clock/v1/time-clocks")
        return [str(c["id"]) for c in _first_list(data, "timeClocks")
                if isinstance(c, dict) and c.get("id") is not None and not c.get("isArchived")]

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
                for shift in entry.get("shifts") or []:
                    if isinstance(shift, dict) and _is_open(shift, now):
                        on_clock.add(str(entry["userId"]))
        return on_clock

    # ------------------------------------------------------------ status

    async def check(self, force: bool = False) -> None:
        """Refresh who's clocked in. Failures are kept in last_error, never raised."""
        async with self._lock:
            if force and time.time() - self._last_attempt < FORCE_GAP:
                return
            self._last_attempt = time.time()
            try:
                self.clocked_in = await self.fetch_clocked_in()
                self.checked_at = time.time()
                self.last_error = None
            except ConnecteamError as err:
                if self.last_error != str(err):
                    log.warning("Connecteam: %s", err)
                self.last_error = str(err)

    def status(self, user_id: str | None) -> str:
        """'in', 'out', or 'unknown' (no answer from Connecteam in STALE_AFTER seconds)."""
        if not self.checked_at or time.time() - self.checked_at > STALE_AFTER:
            return "unknown"
        return "in" if user_id and str(user_id) in self.clocked_in else "out"

    def fresh(self, max_age: float) -> bool:
        return bool(self.checked_at) and time.time() - self.checked_at < max_age

    def forget(self) -> None:
        """Credentials changed or Connecteam was turned off: start from scratch."""
        self._token = None
        self.clocked_in = set()
        self.checked_at = None
        self.last_error = None
        self._last_attempt = 0.0

    async def run(self) -> None:
        """Background loop: check every POLL_SECONDS while Connecteam is turned on."""
        while True:
            try:
                if self._get_settings()[0]:
                    await self.check()
            except Exception as err:  # noqa: BLE001 - the loop must keep running
                log.warning("Connecteam check failed: %s", err)
            await asyncio.sleep(POLL_SECONDS)


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
