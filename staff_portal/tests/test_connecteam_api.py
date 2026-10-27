"""Staff Access's Connecteam connection (app/connecteam.py), checked against a fake Connecteam that
answers as Connecteam's documentation says it does: the token (POST /oauth/v1/token, HTTP Basic,
grant_type=client_credentials; its reply lists the app's scopes), replies wrapped in
{"requestId", "data", "paging"}, limit and offset (1 to 500; 10 by default), userStatus
(active, archived or all), 422 for a bad parameter, and 401 or 403 with a "detail".

It covers every request the add-on makes, the one token per set of keys, a refused request asked
once more with a fresh token, a stop that quotes Connecteam and says what's missing, the stop
tried again by itself, Test connection's steps, the caps, and the rule that the HR portal's
refusals and 429s never stop Staff Access's clock checks.

Nothing here talks to Connecteam.
"""

import asyncio
import base64
import contextlib
import time
import urllib.parse

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import connecteam
from connecteam import Connecteam, ConnecteamError, Halt, HRBusy, HRRefused

BOTH = "users.read time_clock.read"
PLAN_TEXT = "Your plan doesn't include API access to this hub"


def user(uid, first, archived=False):
    return {"userId": uid, "firstName": first, "lastName": "Test", "phoneNumber": "+15415550100",
            "userType": "user", "email": f"u{uid}@example.com", "customFields": [], "isArchived": archived,
            "kioskCode": "4821", "createdAt": 1_700_000_000, "smartGroupsIds": [501]}


class FakeConnecteam:
    """Connecteam as documented. scopes: what each client's app has; plan_refuses: paths answered
    with 403 although the scope is there (the plan); reply_next: one-off answers."""

    def __init__(self):
        self.scopes = {"staff-access-app": BOTH}
        self.plan_refuses: set[str] = set()
        self.users = [user(100 + i, f"P{i:03d}") for i in range(3)]
        self.archived = [user(900, "Gone", archived=True)]
        self.fields = [{"id": 6208000 + i, "name": f"Field {i}", "type": "str"} for i in range(14)]
        self.tokens: dict[str, tuple[str, str]] = {}   # token -> (client ID, scopes)
        self.token_forms: list[dict] = []
        self.calls: list[tuple[str, dict, dict]] = []  # (path, query, headers) of API requests
        self.replies: dict[str, list] = {}

    def reply_next(self, path, status, body=None, headers=None, times=1):
        self.replies.setdefault(path, []).extend([(status, body or {}, headers or {})] * times)

    def count(self, path=""):
        return sum(1 for p, _, _ in self.calls if p.startswith(path))

    def app(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    def _page(self, request, items, key):
        q = request.query
        try:
            limit, offset = int(q.get("limit", 10)), int(q.get("offset", 0))
            assert 1 <= limit <= 500 and offset >= 0
        except (ValueError, AssertionError):
            return web.json_response({"detail": [{"loc": ["query", "limit"], "msg": "ensure this value "
                                                  "is less than or equal to 500", "type": "value_error"}]},
                                     status=422)
        return web.json_response({"requestId": "r-1", "data": {key: items[offset:offset + limit]},
                                  "paging": {"offset": offset + limit}})

    async def handle(self, request):
        path = request.path
        if path == "/oauth/v1/token":
            form = dict(urllib.parse.parse_qsl(await request.text()))
            self.token_forms.append({"form": form, "type": request.headers.get("Content-Type", "")})
            client, _, secret = base64.b64decode(
                request.headers.get("Authorization", "Basic ").split()[-1]).decode().partition(":")
            if form.get("grant_type") != "client_credentials":
                return web.json_response({"detail": "Invalid grant type"}, status=400)
            if client not in self.scopes or secret != "secret":
                return web.json_response({"detail": "Invalid client credentials"}, status=401)
            token = f"t{len(self.tokens) + 1}-{client}"
            self.tokens[token] = (client, self.scopes[client])
            return web.json_response({"access_token": token, "token_type": "bearer", "expires_in": 86400,
                                      "scope": self.scopes[client]})
        self.calls.append((path, dict(request.query), dict(request.headers)))
        if self.replies.get(path):
            status, body, headers = self.replies[path].pop(0)
            return web.json_response(body, status=status, headers=headers)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.tokens:
            return web.json_response({"detail": "Not authenticated"}, status=401)
        need = "time_clock.read" if path.startswith("/time-clock/") else "users.read"
        if need not in self.tokens[token][1].split():
            return web.json_response({"detail": f"Missing required scope: {need}"}, status=403)
        if path in self.plan_refuses:
            return web.json_response({"detail": PLAN_TEXT}, status=403)
        if path == "/users/v1/users":
            status = request.query.get("userStatus", "active")
            if status not in ("active", "archived", "all"):
                return web.json_response({"detail": [{"msg": "value is not a valid enumeration member"}]},
                                         status=422)
            chosen = {"active": self.users, "archived": self.archived,
                      "all": self.users + self.archived}[status]
            return self._page(request, chosen, "users")
        if path == "/users/v1/custom-fields":
            return self._page(request, self.fields, "customFields")
        if path == "/users/v1/smart-groups":
            return web.json_response({"requestId": "r", "data": {"groups": [
                {"id": 501, "name": "Pub", "groupSegmentId": 7, "numberOfUsers": 3}]}})
        if path == "/time-clock/v1/time-clocks":
            return web.json_response({"requestId": "r", "data": {"timeClocks": [
                {"id": 11, "name": "Main", "isArchived": False}]}})
        if path == "/time-clock/v1/time-clocks/11/time-activities":
            if not {"startDate", "endDate"} <= set(request.query):
                return web.json_response({"detail": [{"msg": "field required"}]}, status=422)
            return web.json_response({"requestId": "r", "data": {"timeActivitiesByUsers": [
                {"userId": 101, "shifts": [{"start": {"timestamp": time.time() - 600}, "end": None}]}]}})
        return web.json_response({"detail": "Not Found"}, status=404)


class Env:
    def __init__(self, fake, ct, store, keys):
        self.fake, self.ct, self.store, self.keys = fake, ct, store, keys


@contextlib.asynccontextmanager
async def environment(monkeypatch):
    fake = FakeConnecteam()
    server = TestServer(fake.app())
    await server.start_server()
    monkeypatch.setattr(connecteam, "API_OVERRIDE", str(server.make_url("")).rstrip("/"))
    store = {"ct": {}, "hr": {}}
    keys = {"id": "staff-access-app", "secret": "secret"}
    ct = Connecteam(lambda: (True, keys["id"], keys["secret"], "global", 60), lambda: True,
                    (lambda: store["ct"], lambda u: store.__setitem__("ct", u)), "test",
                    hr_usage_store=(lambda: store["hr"], lambda u: store.__setitem__("hr", u)))
    await ct.start()
    try:
        yield Env(fake, ct, store, keys)
    finally:
        await ct.close()
        await server.close()


def run(coro):
    return asyncio.run(coro)


async def clock_checks_still_work(env):
    """Staff Access's clock check: not stopped, not paused, and it finds who's clocked in."""
    ct = env.ct
    assert ct.halted is None and ct.paused_until == 0.0
    ct.clocked_in = set()
    await ct.check()
    assert ct.last_error is None and ct.clocked_in == {"101"}


# ---------------------------------------------------------------- requests as documented

def test_every_request_is_in_its_documented_form(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            assert len(await env.ct.users()) == 3
            assert [u["id"] for u in await env.ct.archived_users()] == ["900"]
            assert len(await env.ct.profile_fields()) == 14   # more than Connecteam's default 10
            await env.ct.smart_groups()
            await env.ct.check()
            assert env.ct.clocked_in == {"101"}
            # The token: form-encoded grant_type only (no scope: the app's own), HTTP Basic.
            assert env.fake.token_forms == [{"form": {"grant_type": "client_credentials"},
                                             "type": "application/x-www-form-urlencoded"}]
            assert env.ct.scopes == {"users.read", "time_clock.read"}
            queries = [(p, q) for p, q, _ in env.fake.calls]
            assert queries[:4] == [
                ("/users/v1/users", {"limit": "500", "offset": "0"}),
                ("/users/v1/users", {"limit": "500", "offset": "0", "userStatus": "archived"}),
                ("/users/v1/custom-fields", {"limit": "500", "offset": "0"}),
                ("/users/v1/smart-groups", {})]
            path, q = queries[5]
            assert queries[4] == ("/time-clock/v1/time-clocks", {})
            assert path == "/time-clock/v1/time-clocks/11/time-activities"
            assert set(q) == {"startDate", "endDate", "activityTypes"} and q["activityTypes"] == "shift"
            # Every API request: the bearer token and JSON, never the keys.
            for _, _, headers in env.fake.calls:
                assert headers["Authorization"] == "Bearer t1-staff-access-app"
                assert headers["Accept"] == "application/json" and "X-API-KEY" not in headers
            assert len(env.fake.tokens) == 1   # one token for all of it
    run(scenario())


def test_users_are_read_page_by_page(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            env.fake.users = [user(1000 + i, f"P{i:04d}") for i in range(1203)]
            found = await env.ct.users()
            assert len(found) == 1203 and len({u["id"] for u in found}) == 1203
            assert [q["offset"] for p, q, _ in env.fake.calls] == ["0", "500", "1000"]
    run(scenario())


def test_one_token_for_one_set_of_keys(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            env.fake.scopes["other-app"] = BOTH
            await env.ct.users()
            await env.ct.users(max_age=0)
            assert len(env.fake.tokens) == 1
            # New keys (Settings saved): never the old keys' token.
            env.keys["id"] = "other-app"
            env.ct.forget()
            await env.ct.users()
            assert env.fake.calls[-1][2]["Authorization"] == "Bearer t2-other-app"
            # A token is renewed before it runs out.
            env.ct._token = (env.ct._token[0], time.time() - 1)
            env.ct._last_token_try = 0.0
            await env.ct.users(max_age=0)
            assert env.fake.calls[-1][2]["Authorization"] == "Bearer t3-other-app"
    run(scenario())


# ---------------------------------------------------------------- refusals

def test_a_refusal_is_asked_once_more_with_a_fresh_token(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            await env.ct.users()
            # A replaced or expired token, answered 403 or 401: a new token, then it works.
            for status in (403, 401):
                env.fake.reply_next("/users/v1/users", status, {"detail": "Token is no longer valid"})
                assert len(await env.ct.users(max_age=0)) == 3
                assert env.ct.halted is None
            assert len(env.fake.tokens) == 3
    run(scenario())


def test_a_missing_permission_is_named_in_connecteams_words(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            env.fake.scopes["staff-access-app"] = "time_clock.read publishers.read"
            with pytest.raises(Halt) as err:
                await env.ct.users()
            text = str(err.value)
            assert "Connecteam refused /users/v1/users (HTTP 403): “Missing required scope: users.read”" in text
            assert "doesn't have the users.read permission (it has: publishers.read, time_clock.read)" in text
            assert "create a new OAuth app" in text
            assert env.ct.halted == text and len(env.fake.tokens) == 2   # tried once more first
            # The HR portal waits, without repeating another request's refusal.
            with pytest.raises(HRBusy) as busy:
                await env.ct.profile_fields()
            assert "/users/v1/users" not in str(busy.value) and "stopped" in str(busy.value)
            with pytest.raises(ConnecteamError) as again:
                await env.ct.smart_groups()
            assert "Automatic checks are stopped" in str(again.value)
    run(scenario())


def test_a_plan_refusal_says_its_the_plan(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            env.fake.plan_refuses.add("/users/v1/users")
            with pytest.raises(Halt) as err:
                await env.ct.users()
            assert f"“{PLAN_TEXT}”" in str(err.value) and "it's your Connecteam plan" in str(err.value)
            # Bad keys: Connecteam's words too.
            env.ct.forget()
            env.keys["secret"] = "wrong"
            with pytest.raises(Halt) as err:
                await env.ct.users()
            assert "(HTTP 401): “Invalid client credentials”" in str(err.value)
    run(scenario())


def test_a_bad_parameter_is_quoted_and_isnt_a_stop(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            env.fake.reply_next("/users/v1/users", 422, {"detail": [{"msg": "value is not a valid "
                                                                     "enumeration member"}]})
            with pytest.raises(ConnecteamError) as err:
                await env.ct.users()
            assert not isinstance(err.value, Halt) and env.ct.halted is None
            assert "(HTTP 422): “value is not a valid enumeration member”" in str(err.value)
    run(scenario())


def test_a_stop_is_tried_again_by_itself_a_few_times_a_day(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            ct = env.ct
            env.fake.scopes["staff-access-app"] = "users.read"
            await ct.check()
            assert ct.halted and "time_clock.read" in ct.halted and ct.retry_at() > time.time()
            before = env.fake.count()
            await ct.check()                        # stopped: nothing asked
            assert env.fake.count() == before
            for n in range(connecteam.HALT_RETRIES):
                ct.halted_at -= connecteam.HALT_RETRY   # half an hour later
                await ct.check()                    # tried again, and stopped again
                assert ct.halted and env.fake.count() > before
                before = env.fake.count()
            assert ct.retry_at() is None            # that's today's tries
            ct.halted_at -= connecteam.HALT_RETRY
            await ct.check()
            assert env.fake.count() == before
            # Fixed in Connecteam; Test (or Save) lifts it, and the tries start over.
            env.fake.scopes["staff-access-app"] = BOTH
            checks = await ct.diagnose()
            assert all(c["ok"] for c in checks) and ct.halted is None and ct._halt_tries[1] == 0
            # A stop that's tried again and works stays lifted.
            env.fake.reply_next("/time-clock/v1/time-clocks", 403, {"detail": "Forbidden"}, times=2)
            ct._clocks = None
            await ct.check()
            assert ct.halted
            ct.halted_at -= connecteam.HALT_RETRY
            await clock_checks_still_work_after_retry(ct)
    run(scenario())


async def clock_checks_still_work_after_retry(ct):
    await ct.check()
    assert ct.halted is None and ct.last_error is None and ct.clocked_in == {"101"}


def test_test_connection_shows_each_step(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            checks = await env.ct.diagnose()
            assert [(c["check"], c["ok"]) for c in checks] == [
                ("Getting an access token", True), ("Reading users", True),
                ("Reading the time clock", True), ("Reading smart groups", True)]
            assert checks[0]["detail"] == ("Connecteam issued an access token with: time_clock.read, "
                                           "users.read. Reports and tasks also need schedule.read "
                                           "and forms.read.")
            # An app without users.read: the token says so, and each step shows Connecteam's words.
            env.fake.scopes["staff-access-app"] = "time_clock.read"
            checks = await env.ct.diagnose()
            assert [c["ok"] for c in checks] == [False, False, True, False]
            assert "the app lacks users.read (it has: time_clock.read)" in checks[0]["detail"]
            assert "“Missing required scope: users.read”" in checks[1]["detail"]
            assert env.ct.halted
            # Bad keys: only the token step, in Connecteam's words.
            env.keys["secret"] = "wrong"
            env.ct.forget()
            checks = await env.ct.diagnose()
            assert len(checks) == 1 and "“Invalid client credentials”" in checks[0]["detail"]
    run(scenario())


# ---------------------------------------------------------------- the HR portal's lane

def test_hr_refusals_never_stop_the_clock_checks(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            ct = env.ct
            # HR's request refused (twice: a fresh token first): only HR's request fails.
            env.fake.reply_next("/users/v1/custom-fields", 403, {"detail": "Forbidden"}, times=2)
            with pytest.raises(HRRefused) as err:
                await ct.profile_fields()
            assert "for the HR portal (HTTP 403): “Forbidden”" in str(err.value)
            await clock_checks_still_work(env)
            # Too many requests for HR: HR's lane waits, the clock checks don't.
            env.fake.reply_next("/users/v1/users", 429, {"detail": "Too many requests"},
                                {"Retry-After": "120"})
            with pytest.raises(HRBusy):
                await ct.users(hr=True)
            assert ct.hr_paused_until > time.time() and ct.hr_status()["paused"]
            await clock_checks_still_work(env)
            # Connecteam failing or answering nonsense for HR: no effect either.
            ct.hr_paused_until = 0.0
            ct._hr_recent.clear()
            env.fake.reply_next("/users/v1/smart-groups", 500, {"detail": "Internal error"})
            with pytest.raises(ConnecteamError):
                await ct.smart_groups(hr=True)
            env.fake.reply_next("/users/v1/smart-groups", 200, ["not", "an", "object"])
            with pytest.raises(ConnecteamError):
                await ct.smart_groups(hr=True)
            await clock_checks_still_work(env)
    run(scenario())


def test_hr_lane_limits(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            ct, day = env.ct, time.strftime("%Y-%m-%d", time.gmtime())
            for _ in range(connecteam.HR_MINUTE_CAP - 1):
                await ct.users(max_age=0, hr=True)   # a token, then 5 reads
            assert ct.hr_usage()["count"] == 6 and ct.usage()["count"] == 6
            with pytest.raises(HRBusy, match="this minute"):
                await ct.users(max_age=0, hr=True)
            await clock_checks_still_work(env)       # Staff Access's own room this minute
            ct._hr_recent.clear()
            env.store["hr"] = {"day": day, "count": connecteam.HR_DAILY_CAP}
            with pytest.raises(HRBusy, match="500"):
                await ct.users(max_age=0, hr=True)
            env.store["hr"]["count"] = 0
            half = int(ct.daily_cap() * connecteam.HR_STOP_SHARE)
            env.store["ct"] = {"day": day, "count": half}
            with pytest.raises(HRBusy, match="time clock"):
                await ct.users(max_age=0, hr=True)
            await clock_checks_still_work(env)
            assert env.store["ct"]["count"] > half
    run(scenario())


def test_the_accounts_own_limits_pause_everyone(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            env.fake.reply_next("/users/v1/users", 200, {"data": {"users": []}},
                                {"x-ratelimit-day-limit": "10000", "x-ratelimit-day-remaining": "50"})
            await env.ct.users(hr=True)
            assert env.ct.paused_until > time.time()
            with pytest.raises(HRBusy, match="paused"):
                await env.ct.users(max_age=0, hr=True)
    run(scenario())


def test_the_caps_and_how_the_shares_add_up():
    """9,000 a day for Staff Access (the owner's choice, of Expert's 10,000), and the lanes inside it."""
    ct = Connecteam(lambda: (True, "a", "b", "global", 60), lambda: True, (dict, lambda u: None), "t")
    assert ct.daily_cap() == 9000                     # Expert: 10,000 a day, 1,000 left for others
    ct.limits["day_limit"] = 20_000
    assert ct.daily_cap() == 9000                     # Enterprise: still 9,000
    ct.limits["day_limit"] = 100
    assert ct.daily_cap() == 90                       # SBP: 90% of 100
    assert connecteam.MINUTE_CAP == 60 and connecteam.DAILY_CAP == 9000
    assert 10_000 - connecteam.DAILY_CAP >= 1000      # the account's other integrations
    # HR and reports stop at half of the day, so the clock checks keep at least 4,500; both lanes
    # fit under that half together.
    half = connecteam.DAILY_CAP * max(connecteam.HR_STOP_SHARE, connecteam.REPORTS_STOP_SHARE)
    assert connecteam.DAILY_CAP - half >= 4500
    assert connecteam.HR_DAILY_CAP + connecteam.REPORTS_DAILY_CAP <= half
    assert (connecteam.MINUTE_CAP - connecteam.HR_MINUTE_CAP - connecteam.REPORTS_MINUTE_CAP
            >= connecteam.CLOCK_MINUTE_KEEP)
    assert connecteam.MINUTE_CAP <= 100 - connecteam.LOW_MINUTE_LEFT - 30   # the account keeps room
    # The clock checks' own plan (15% of the account's day) fits in what's left to them.
    assert connecteam.DEFAULT_DAY_LIMIT * connecteam.POLL_SHARE <= connecteam.DAILY_CAP - half
