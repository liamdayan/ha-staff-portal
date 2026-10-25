"""tthotel.py against a fake TT Hotel cloud API on 127.0.0.1: the access token, the request
budget (monthly cap, minute cap, background share), TT Hotel's quota errors, rejected
credentials, the hotelInfo cache, the door list and the background sync loop."""

import asyncio
import json
import re
import time

import pytest
from aiohttp import web

import tthotel
from tthotel import Halt, TTHotel, TTHotelError

SECRET = "client-secret-0123"
PASSWORD = "E10ADC3949BA59ABBE56E057F20F883E"


def lock(lock_id, mac, **extra):
    return {"lockId": lock_id, "lockMac": mac, "lockAlias": f"Door {lock_id}", **extra}


class FakeTTHotel:
    """Just enough of TT Hotel's cloud API, with the same habits: errors in the body with HTTP
    200, form-encoded token requests, and the request time checked."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []   # (method, kind, parameters)
        self.tokens: set[str] = set()
        self.refresh_tokens: set[str] = set()
        self.seq = 0
        self.locks: list[dict] = []
        self.page_size = 1000
        self.overrides: dict[str, tuple[int, object]] = {}   # kind -> (HTTP status, body)
        self.hotel_info = "HOTEL-KEY-1"
        self.runner = None
        self.url = None

    def count(self, kind: str) -> int:
        return sum(1 for _, k, _ in self.requests if k == kind)

    def issue(self) -> dict:
        self.seq += 1
        access, refresh = f"access-{self.seq}", f"refresh-{self.seq}"
        self.tokens.add(access)
        self.refresh_tokens.add(refresh)
        return {"access_token": access, "refresh_token": refresh, "uid": 7,
                "expires_in": 90 * 86400}

    async def handle(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            assert request.content_type == "application/x-www-form-urlencoded"
            params = dict(await request.post())
        else:
            params = dict(request.query)
        kind = request.path
        if kind == "/oauth2/token":
            kind = "refresh" if params.get("grant_type") == "refresh_token" else "login"
        self.requests.append((request.method, kind, params))
        if kind in self.overrides:
            status, body = self.overrides[kind]
            return (web.json_response(body, status=status) if not isinstance(body, str)
                    else web.Response(text=body, status=status))
        if kind != "login" and kind != "refresh":
            date = int(params.get("date", 0))
            if abs(date - time.time() * 1000) > 300_000:
                return web.json_response({"errcode": 80000, "errmsg": "date must be current"})
        if kind == "login":
            return web.json_response(self.issue())
        if kind == "refresh":
            if params.get("refresh_token") not in self.refresh_tokens:
                return web.json_response({"errcode": 10011, "errmsg": "invalid refresh_token"})
            return web.json_response(self.issue())
        if kind == "/v3/hotel/getInfo":
            return web.json_response({"errcode": 0, "errmsg": "", "hotelInfo": self.hotel_info})
        if kind == "/v3/lock/listByHotel":
            if params.get("accessToken") not in self.tokens:
                return web.json_response({"errcode": 10004, "errmsg": "invalid token"})
            page = int(params["pageNo"])
            size = self.page_size
            pages = max(1, -(-len(self.locks) // size))
            return web.json_response({"list": self.locks[(page - 1) * size:page * size],
                                      "pageNo": page, "pageSize": size, "pages": pages,
                                      "total": len(self.locks)})
        return web.json_response({"errcode": -3, "errmsg": "not here"}, status=404)

    async def start(self) -> str:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        return self.url

    async def stop(self):
        await self.runner.cleanup()


class Store:
    """A (load, save) pair kept in memory, like a settings key."""

    def __init__(self, value=None):
        self.value = value if value is not None else {}
        self.saves = 0

    def load(self):
        return self.value

    def save(self, value):
        self.saves += 1
        self.value = json.loads(json.dumps(value))


class Env:
    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.fake = FakeTTHotel()
        self.settings = [True, "global", "cid123", SECRET, "hotel_admin", PASSWORD, 6]
        self.wanted = True
        self.usage = Store()
        self.token = Store()
        self.saved_doors: list[list[dict]] = []
        self.clients: list[TTHotel] = []

    def client(self, **kwargs) -> TTHotel:
        def on_doors(doors):
            self.saved_doors.append(doors)
            return {"added": len(doors), "updated": 0, "removed": 0, "total": len(doors)}
        client = TTHotel(lambda: tuple(self.settings), lambda: self.wanted,
                         (self.usage.load, self.usage.save), (self.token.load, self.token.save),
                         "StaffPortal-test/1", on_doors=kwargs.get("on_doors", on_doors),
                         last_sync=kwargs.get("last_sync"))
        self.clients.append(client)
        return client

    async def __aenter__(self):
        self.monkeypatch.setattr(tthotel, "API_OVERRIDE", await self.fake.start())
        self.tt = self.client()
        await self.tt.start()
        return self

    async def __aexit__(self, *exc):
        for client in self.clients:
            await client.close()
        await self.fake.stop()


def run(monkeypatch, scenario):
    async def main():
        async with Env(monkeypatch) as env:
            await scenario(env)
    asyncio.run(main())


async def refused(coro, cls=TTHotelError, match=None):
    with pytest.raises(cls) as info:
        await coro
    if match:
        assert re.search(match, str(info.value)), str(info.value)
    return info.value


# ---------------------------------------------------------------- access token

def test_login_once_token_saved_with_a_fingerprint_and_reused(monkeypatch):
    async def scenario(env):
        env.fake.locks = [lock(1, "C5:40:E0:9C:8C:C1")]
        assert len(await env.tt.hotel_locks()) == 1
        assert len(await env.tt.hotel_locks()) == 1
        assert env.fake.count("login") == 1
        method, _, form = next(r for r in env.fake.requests if r[1] == "login")
        assert method == "POST"
        assert form == {"clientId": "cid123", "clientSecret": SECRET, "username": "hotel_admin",
                        "password": PASSWORD.lower()}   # already MD5: lower case, never re-hashed
        _, _, params = env.fake.requests[-1]
        assert params["clientId"] == "cid123" and params["accessToken"] == "access-1"
        assert params["pageSize"] == "1000" and params["pageNo"] == "1" and params["type"] == "1"
        assert abs(int(params["date"]) - time.time() * 1000) < 5000    # milliseconds
        saved = env.token.value
        assert saved["token"] == "access-1" and saved["refresh"] == "refresh-1"
        assert re.fullmatch(r"[0-9a-f]{64}", saved["for"])
        assert SECRET not in json.dumps(saved) and PASSWORD.lower() not in json.dumps(saved)
        assert abs(saved["expires_at"] - (time.time() + 90 * 86400)) < 60
        # After a restart the saved token is used as is.
        second = env.client()
        await second.start()
        await second.hotel_locks()
        assert env.fake.count("login") == 1
        assert second.state()["token_expires"] == saved["expires_at"]
    run(monkeypatch, scenario)


def test_token_for_other_credentials_is_not_reused(monkeypatch):
    async def scenario(env):
        await env.tt.hotel_locks()
        env.settings[4] = "someone_else"
        await refused(env.tt.hotel_locks(), match="Waiting a minute")   # still one a minute...
        env.tt.forget()   # ...unless the settings were saved, which is how they change
        await env.tt.hotel_locks()
        assert env.fake.count("login") == 2
        # The server (from the region) is part of the fingerprint too.
        monkeypatch.setattr(tthotel, "API_OVERRIDE", "")
        env.settings[1] = "global"
        europe = tthotel._fingerprint(env.tt._credentials(login=True))
        env.settings[1] = "china"
        assert tthotel._fingerprint(env.tt._credentials(login=True)) != europe
    run(monkeypatch, scenario)


def test_token_refreshed_a_week_before_it_runs_out(monkeypatch):
    async def scenario(env):
        await env.tt.hotel_locks()
        env.tt._token["expires_at"] = time.time() + 8 * 86400   # more than a week left
        await env.tt.hotel_locks()
        assert env.fake.count("refresh") == 0
        env.tt._token["expires_at"] = time.time() + 6 * 86400
        await env.tt.hotel_locks()   # renewed at most once a minute: the old one still works
        assert env.fake.count("refresh") == 0
        env.tt._last_token_try -= 61
        await env.tt.hotel_locks()
        assert (env.fake.count("refresh"), env.fake.count("login")) == (1, 1)
        _, _, form = next(r for r in env.fake.requests if r[1] == "refresh")
        assert form == {"clientId": "cid123", "clientSecret": SECRET,
                        "grant_type": "refresh_token", "refresh_token": "refresh-1"}
        assert env.fake.requests[-1][2]["accessToken"] == "access-2"
        assert env.token.value["token"] == "access-2"
        # A refresh TT Hotel refuses falls back to the password, in the same go.
        env.fake.refresh_tokens.clear()
        env.tt._token["expires_at"] = time.time() + 86400
        env.tt._last_token_try = 0
        await env.tt.hotel_locks()
        assert (env.fake.count("refresh"), env.fake.count("login")) == (2, 2)
    run(monkeypatch, scenario)


def test_token_requests_at_most_once_a_minute(monkeypatch):
    async def scenario(env):
        env.fake.overrides["login"] = (200, {"errcode": 90000, "errmsg": "internal error"})
        await refused(env.tt.hotel_locks(), match="server had a problem")
        del env.fake.overrides["login"]
        await refused(env.tt.hotel_locks(), match="Waiting a minute")
        assert env.fake.count("login") == 1
        env.tt._last_token_try -= 61
        await env.tt.hotel_locks()
        assert env.fake.count("login") == 2
    run(monkeypatch, scenario)


def test_rejected_token_logs_in_again_and_retries_once(monkeypatch):
    async def scenario(env):
        await env.tt.hotel_locks()
        env.fake.tokens.clear()   # revoked at TT Hotel
        env.fake.locks = [lock(5, "AA:BB:CC:DD:EE:FF")]
        assert [d["lock_id"] for d in await env.tt.hotel_locks()] == [5]
        assert env.fake.count("login") == 2 and env.token.value["token"] == "access-2"
        # Rejected again straight after a new login: an error, not a loop.
        env.fake.overrides["/v3/lock/listByHotel"] = (200, {"errcode": 10004, "errmsg": "no"})
        before = len(env.fake.requests)
        err = await refused(env.tt.hotel_locks())
        assert err.code == 10004 and not isinstance(err, Halt)
        kinds = [k for _, k, _ in env.fake.requests[before:]]
        assert kinds == ["/v3/lock/listByHotel", "login", "/v3/lock/listByHotel"]
    run(monkeypatch, scenario)


# ---------------------------------------------------------------- errors

def test_errcode_with_http_200_is_an_error_in_plain_words(monkeypatch):
    async def scenario(env):
        env.fake.overrides["/v3/lock/listByHotel"] = (200, {"errcode": 12345,
                                                            "errmsg": "something odd"})
        err = await refused(env.tt.hotel_locks())
        assert (err.code, str(err)) == (12345, "TT Hotel reported a problem: something odd.")
        env.fake.overrides["/v3/hotel/getInfo"] = (200, {"errcode": "80000", "errmsg": "date"})
        await refused(env.tt.hotel_info(fresh=True), match="clock is more than 5 minutes off")
        env.fake.overrides["/v3/hotel/getInfo"] = (200, "<html>proxy</html>")
        env.tt._info_error = None
        await refused(env.tt.hotel_info(fresh=True), match="couldn't read")
        env.fake.overrides["/v3/hotel/getInfo"] = (502, "Bad gateway")
        env.tt._info_error = None
        await refused(env.tt.hotel_info(fresh=True), match="server had a problem")
        env.fake.overrides["/v3/hotel/getInfo"] = (200, {"errcode": 0})
        env.tt._info_error = None
        await refused(env.tt.hotel_info(fresh=True), match="hotelInfo")
    run(monkeypatch, scenario)


def test_network_errors_are_plain_and_never_show_the_secret(monkeypatch):
    async def scenario(env):
        await env.fake.stop()
        await env.fake.start()   # a new port...
        await env.fake.runner.cleanup()   # ...with nothing listening
        monkeypatch.setattr(tthotel, "API_OVERRIDE", env.fake.url)
        err = await refused(env.tt.hotel_info(fresh=True), match="^Can't reach TT Hotel: ")
        assert err.code is None and SECRET not in str(err) and "127.0.0.1" not in str(err)
        await env.fake.start()
    run(monkeypatch, scenario)


def test_nothing_is_asked_until_set_up(monkeypatch):
    async def scenario(env):
        env.settings[0] = False
        err = await refused(env.tt.hotel_info(fresh=True), match="turned off")
        assert err.needs_admin
        env.settings[0], env.settings[3] = True, ""
        env.tt.forget()
        await refused(env.tt.hotel_info(fresh=True), match="Client ID and Client Secret")
        env.settings[3], env.settings[5] = SECRET, ""
        await refused(env.tt.hotel_locks(), match="username and password")
        assert env.fake.requests == []
    run(monkeypatch, scenario)


# ---------------------------------------------------------------- budget

def test_monthly_quota_error_pauses_until_next_month_and_is_never_retried(monkeypatch):
    async def scenario(env):
        env.fake.overrides["/v3/hotel/getInfo"] = (200, {"errcode": 30007, "errmsg": "monthly"})
        err = await refused(env.tt.hotel_info(fresh=True), match="until next month")
        assert err.code == 30007 and err.needs_admin
        assert env.tt.paused_until == tthotel._next_utc_month() > time.time()
        assert env.fake.count("/v3/hotel/getInfo") == 1
        env.tt._info_error = None
        del env.fake.overrides["/v3/hotel/getInfo"]
        await refused(env.tt.hotel_info(fresh=True), match="until next month")
        await refused(env.tt.sync(admin=True), match="until next month")
        assert len(env.fake.requests) == 1
        state = env.tt.state()
        assert state["paused"] is True and "next month" in state["pause_reason"]
        env.tt.forget()   # settings saved: TT Hotel's own pause still stands
        await refused(env.tt.hotel_info(fresh=True), match="until next month")
    run(monkeypatch, scenario)


def test_call_number_error_pauses_an_hour(monkeypatch):
    async def scenario(env):
        env.fake.overrides["/v3/hotel/getInfo"] = (200, {"errcode": 30006, "errmsg": "limit"})
        await refused(env.tt.hotel_info(fresh=True), match="waits an hour")
        assert 3590 < env.tt.paused_until - time.time() <= 3600
        assert env.fake.count("/v3/hotel/getInfo") == 1
    run(monkeypatch, scenario)


def test_http_429_pauses(monkeypatch):
    async def scenario(env):
        env.fake.overrides["/v3/hotel/getInfo"] = (429, {"errcode": 0})
        await refused(env.tt.hotel_info(fresh=True), match="slow down")
        assert env.tt.paused_until > time.time() + 50
    run(monkeypatch, scenario)


def test_own_monthly_cap_counted_across_restarts(monkeypatch):
    async def scenario(env):
        month = time.strftime("%Y-%m", time.gmtime())
        await env.tt.hotel_info(fresh=True)
        assert env.usage.value == {"month": month, "count": 1}
        env.usage.value = {"month": month, "count": 7499}
        env.tt._info = None
        await env.tt.hotel_info(fresh=True)
        assert env.usage.value["count"] == 7500 == env.tt.monthly_cap()
        env.tt._info = None
        await refused(env.tt.hotel_info(fresh=True), match="monthly share")
        assert env.fake.count("/v3/hotel/getInfo") == 2
        assert env.tt.paused_until == tthotel._next_utc_month()
        assert env.tt.state()["used_month"] == 7500
        # A new month starts from zero (the saved pause is gone with the old month's count).
        env.usage.value = {"month": "2000-01", "count": 99999}
        fresh = env.client()
        await fresh.start()
        await fresh.hotel_info(fresh=True)
        assert env.usage.value == {"month": month, "count": 1}
    run(monkeypatch, scenario)


def test_pauses_and_stops_survive_a_restart(monkeypatch):
    async def scenario(env):
        month = time.strftime("%Y-%m", time.gmtime())
        env.fake.overrides["/v3/hotel/getInfo"] = (200, {"errcode": 30007, "errmsg": "monthly"})
        await refused(env.tt.hotel_info(fresh=True), match="until next month")
        assert env.usage.value["paused_until"] == tthotel._next_utc_month()
        assert env.usage.value["count"] == 1 and env.usage.value["month"] == month
        # After a restart, neither a card nor the door list asks TT Hotel again.
        restarted = env.client()
        await restarted.start()
        await refused(restarted.hotel_info(fresh=True), match="until next month")
        await refused(restarted.sync(admin=True), match="until next month")
        assert len(env.fake.requests) == 1 and restarted.state()["paused"] is True
        # Rejected client details: stopped after a restart too, until an admin acts.
        env.usage.value = {"month": month, "count": 1}
        env.fake.overrides["/v3/hotel/getInfo"] = (200, {"errcode": 10001, "errmsg": "invalid"})
        tt = env.client()
        await tt.start()
        await refused(tt.hotel_info(fresh=True), Halt)
        assert env.usage.value["halted"] and env.usage.value["halt_everything"] is True
        restarted = env.client()
        await restarted.start()
        await refused(restarted.hotel_info(fresh=True), match="stopped asking TT Hotel")
        assert env.fake.count("/v3/hotel/getInfo") == 2
        restarted.forget()   # settings saved: the stop is lifted, and saved as lifted
        assert "halted" not in env.usage.value
        # A damaged or far-off saved pause is ignored.
        for odd in ({"paused_until": "soon", "pause_reason": "x"},
                    {"paused_until": time.time() + 400 * 86400, "pause_reason": "x"},
                    {"halted": 5}):
            env.usage.value = {"month": month, "count": 1, **odd}
            fresh = env.client()
            await fresh.start()
            assert not fresh.state()["paused"] and fresh.state()["halted"] is None
    run(monkeypatch, scenario)


def test_a_restart_doesnt_sync_the_doors_early(monkeypatch):
    async def scenario(env):
        synced = time.time() - 600
        tt = env.client(last_sync=lambda: (synced, 12))
        await tt.start()
        assert tt.state()["synced_at"] == synced and tt.state()["doors"] == 12
        assert tt.next_due == pytest.approx(synced + 6 * 3600)
        # Synced long ago (or never): soon after the start, as before.
        old = env.client(last_sync=lambda: (time.time() - 7 * 3600, 3))
        await old.start()
        assert old.next_due == pytest.approx(time.time() + tthotel.FIRST_SYNC_DELAY, abs=5)
        never = env.client(last_sync=lambda: (None, 0))
        await never.start()
        assert never.synced_at is None and never.next_due is None
        # Saving the settings still syncs soon.
        tt.forget()
        assert tt.next_due is None
    run(monkeypatch, scenario)


def test_minute_cap(monkeypatch):
    async def scenario(env):
        now = time.time()
        env.tt._recent.extend([now - 30] * tthotel.MINUTE_CAP)
        await refused(env.tt.hotel_info(fresh=True), match="enough TT Hotel requests")
        assert env.fake.requests == []
        env.tt._recent[0] = now - 61   # one of them is now more than a minute old
        env.tt._info_error = None
        assert await env.tt.hotel_info(fresh=True) == "HOTEL-KEY-1"
    run(monkeypatch, scenario)


def test_background_syncs_stop_at_their_share(monkeypatch):
    async def scenario(env):
        month = time.strftime("%Y-%m", time.gmtime())
        env.usage.value = {"month": month, "count": int(7500 * tthotel.SYNC_SHARE)}
        await refused(env.tt.sync(), match="kept for making wristbands")
        assert env.fake.requests == []
        assert "kept for making wristbands" in env.tt.state()["last_error"]
        # Card-making and an admin's sync still go ahead.
        await env.tt.hotel_info(fresh=True)
        result = await env.tt.sync(admin=True)
        assert result["total"] == 0 and env.tt.state()["last_error"] is None
    run(monkeypatch, scenario)


# ---------------------------------------------------------------- rejected credentials

def test_bad_client_details_stop_everything_until_tested(monkeypatch):
    async def scenario(env):
        env.fake.overrides["/v3/hotel/getInfo"] = (200, {"errcode": 10001, "errmsg": "invalid"})
        err = await refused(env.tt.hotel_info(fresh=True), Halt, "Client ID and Client Secret")
        assert err.everything and err.needs_admin
        env.tt._info_error = None
        await refused(env.tt.hotel_info(fresh=True), match="stopped asking TT Hotel")
        await refused(env.tt.sync(), match="stopped asking")
        assert len(env.fake.requests) == 1
        assert env.tt.state()["halted"].startswith("TT Hotel didn't accept the Client ID")
        # Test connection (an admin's sync) lifts the stop and asks again.
        del env.fake.overrides["/v3/hotel/getInfo"]
        await env.tt.sync(admin=True)
        assert env.tt.state()["halted"] is None and env.fake.count("/v3/lock/listByHotel") == 1
    run(monkeypatch, scenario)


def test_bad_password_stops_the_door_list_but_not_wristbands(monkeypatch):
    async def scenario(env):
        env.fake.overrides["login"] = (400, {"errcode": 10007, "errmsg": "invalid password"})
        err = await refused(env.tt.sync(admin=True), Halt, "username or password")
        assert not err.everything
        assert await env.tt.hotel_info(fresh=True) == "HOTEL-KEY-1"   # still allowed
        env.tt._last_token_try = 0
        await refused(env.tt.sync(), match="stopped asking")
        assert env.fake.count("login") == 1
        # Saving the settings again starts over.
        del env.fake.overrides["login"]
        env.tt.forget()
        await env.tt.sync()
        assert env.fake.count("login") == 2
    run(monkeypatch, scenario)


# ---------------------------------------------------------------- hotelInfo

def test_hotel_info_cache(monkeypatch):
    async def scenario(env):
        assert await env.tt.hotel_info(fresh=True) == "HOTEL-KEY-1"
        env.fake.hotel_info = "HOTEL-KEY-2"
        assert await env.tt.hotel_info(fresh=True) == "HOTEL-KEY-1"
        value, fetched = env.tt._info
        env.tt._info = (value, fetched - tthotel.HOTEL_INFO_TTL + 5)   # still fresh
        assert await env.tt.hotel_info(fresh=False) == "HOTEL-KEY-1"
        env.tt._info = (value, fetched - tthotel.HOTEL_INFO_TTL)
        assert await env.tt.hotel_info(fresh=True) == "HOTEL-KEY-2"
        assert env.fake.count("/v3/hotel/getInfo") == 2
        params = env.fake.requests[-1][2]
        assert set(params) == {"clientId", "clientSecret", "date"}   # no login needed
        assert env.fake.count("login") == 0
        env.tt.hotel_info_expired()   # the encoder said the key ran out
        env.fake.hotel_info = "HOTEL-KEY-3"
        assert await env.tt.hotel_info(fresh=True) == "HOTEL-KEY-3"
    run(monkeypatch, scenario)


def test_failed_hotel_info_is_remembered_and_old_key_only_for_single_doors(monkeypatch):
    async def scenario(env):
        await env.tt.hotel_info(fresh=True)
        value, fetched = env.tt._info
        env.tt._info = (value, fetched - 3600)   # an hour old
        env.fake.overrides["/v3/hotel/getInfo"] = (200, {"errcode": 90000, "errmsg": "down"})
        assert await env.tt.hotel_info(fresh=False) == "HOTEL-KEY-1"
        await refused(env.tt.hotel_info(fresh=True), match="server had a problem")
        assert await env.tt.hotel_info(fresh=False) == "HOTEL-KEY-1"
        for _ in range(5):   # a burst of wristbands: no more requests for a while
            await refused(env.tt.hotel_info(fresh=True))
        assert env.fake.count("/v3/hotel/getInfo") == 2
        err, failed_at = env.tt._info_error
        env.tt._info_error = (err, failed_at - tthotel.INFO_RETRY_GAP)
        del env.fake.overrides["/v3/hotel/getInfo"]
        assert await env.tt.hotel_info(fresh=True) == "HOTEL-KEY-1"
        assert env.fake.count("/v3/hotel/getInfo") == 3
    run(monkeypatch, scenario)


def test_no_old_key_to_fall_back_on(monkeypatch):
    async def scenario(env):
        env.fake.overrides["/v3/hotel/getInfo"] = (200, {"errcode": 90000, "errmsg": "down"})
        await refused(env.tt.hotel_info(fresh=False), match="server had a problem")
    run(monkeypatch, scenario)


# ---------------------------------------------------------------- doors

def test_door_list_pages_and_is_normalized(monkeypatch):
    async def scenario(env):
        env.fake.page_size = 2
        env.fake.locks = [
            lock(101, "c5:40:e0:9c:8c:c1", lockName="YS_101", buildingNumber=1,
                 buildingName=" Main ", floorNumber=2, floorName="Floor 2", electricQuantity=80,
                 hasGateway=1),
            {"lockId": "102", "lockMac": "AA-BB-CC-DD-EE-01", "buildNumber": 3,
             "floorNumber": "1"},
            {"lockId": 103, "lockMac": "not-a-mac"},
            {"lockMac": "AA:BB:CC:DD:EE:02"},
            lock(104, "AA:BB:CC:DD:EE:04", buildingNumber=999, electricQuantity=300),
        ]
        doors = await env.tt.hotel_locks()
        pages = [p["pageNo"] for _, k, p in env.fake.requests if k == "/v3/lock/listByHotel"]
        assert pages == ["1", "2", "3"]
        assert doors == [
            {"lock_id": 101, "name": "YS_101", "alias": "Door 101", "mac": "C540E09C8CC1",
             "building_number": 1, "building_name": "Main", "floor_number": 2,
             "floor_name": "Floor 2", "battery": 80, "has_gateway": True, "lock_type": 1},
            {"lock_id": 102, "name": "", "alias": "", "mac": "AABBCCDDEE01",
             "building_number": 3, "building_name": "", "floor_number": 1, "floor_name": "",
             "battery": None, "has_gateway": False, "lock_type": 1},
            {"lock_id": 104, "name": "", "alias": "Door 104", "mac": "AABBCCDDEE04",
             "building_number": None, "building_name": "", "floor_number": None,
             "floor_name": "", "battery": None, "has_gateway": False, "lock_type": 1},
        ]
    run(monkeypatch, scenario)


def test_sync_saves_doors_and_reports_state(monkeypatch):
    async def scenario(env):
        env.fake.locks = [lock(1, "00:00:00:00:00:01", buildingNumber=1),
                          lock(2, "00:00:00:00:00:02", buildingNumber=2),
                          lock(3, "00:00:00:00:00:03", buildingNumber=2)]
        result = await env.tt.sync()
        assert result == {"added": 3, "updated": 0, "removed": 0, "total": 3, "buildings": 2}
        assert [d["lock_id"] for d in env.saved_doors[-1]] == [1, 2, 3]
        state = env.tt.state()
        assert set(state) >= {"enabled", "synced_at", "doors", "last_error", "halted", "paused",
                              "pause_reason", "used_month", "monthly_cap", "quota",
                              "token_expires", "needed"}
        assert (state["doors"], state["last_error"], state["halted"], state["paused"]) == (
            3, None, None, False)
        assert state["used_month"] == 2 and state["monthly_cap"] == 7500
        assert state["quota"] == 30000 and state["needed"] is True
        assert abs(state["synced_at"] - time.time()) < 5 and state["token_expires"]
        # A failed request never saves a partial list.
        env.fake.overrides["/v3/lock/listByHotel"] = (200, {"errcode": 90000})
        await refused(env.tt.sync(), match="server had a problem")
        assert len(env.saved_doors) == 1 and env.tt.state()["last_error"]
    run(monkeypatch, scenario)


def test_a_failing_save_is_reported_plainly(monkeypatch):
    async def scenario(env):
        def broken(doors):
            raise RuntimeError("database is locked")
        tt = env.client(on_doors=broken)
        await tt.start()
        err = await refused(tt.sync(admin=True), match="couldn't read or save")
        assert "database" not in str(err) and tt.failures == 1
    run(monkeypatch, scenario)


# ---------------------------------------------------------------- background loop

def test_background_loop_only_when_wanted(monkeypatch):
    monkeypatch.setattr(tthotel, "FIRST_SYNC_DELAY", 0)

    async def scenario(env):
        env.wanted = False
        task = asyncio.create_task(env.tt.run())
        await asyncio.sleep(0.1)
        assert env.fake.requests == [] and env.tt.next_due is None
        env.wanted = True
        env.tt.forget()   # wakes the loop, as saving the settings does
        for _ in range(100):
            if env.saved_doors:
                break
            await asyncio.sleep(0.02)
        assert env.saved_doors and env.tt.state()["synced_at"]
        assert 6 * 3600 - 10 < env.tt.next_due - time.time() <= 6 * 3600
        task.cancel()
    run(monkeypatch, scenario)


def test_failed_background_syncs_back_off(monkeypatch):
    async def scenario(env):
        env.fake.overrides["/v3/lock/listByHotel"] = (200, {"errcode": 90000})
        delays = []
        for _ in range(3):
            await env.tt._background_sync()
            delays.append(round((env.tt.next_due - time.time()) / 60))
        assert delays == [5, 10, 20]
        env.tt.failures = 40
        await env.tt._background_sync()
        assert env.tt.next_due - time.time() == pytest.approx(tthotel.MAX_BACKOFF, abs=5)
        # Stopped after rejected details: nothing is sent until an admin acts.
        env.tt._halt("TT Hotel didn't accept the Client ID and Client Secret.", True)
        before = len(env.fake.requests)
        await env.tt._background_sync()
        assert len(env.fake.requests) == before and "stopped asking" in env.tt.last_error
    run(monkeypatch, scenario)
