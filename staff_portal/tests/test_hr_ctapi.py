"""The HR portal's own Connecteam requests (app/hr/ctapi.py): chat messages from "Hot Lake HR",
certificate copies onto Connecteam documents, HR's request lane inside Staff Access's budget, and
the rule that HR's problems never stop Staff Access's time clock checks.

Connecteam is a fake aiohttp server (connecteam.API_OVERRIDE); nothing here talks to Connecteam.
"""

import asyncio
import contextlib
import time
from datetime import date, datetime, timezone

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import connecteam
from connecteam import Connecteam
from hr import ctapi
from hr.ctapi import CTError, HRConnecteam

TODAY = date.today()


class FakeConnecteam:
    """Connecteam's token, time clock, publishers, chat, attachments and documents."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []     # (method, path) of API requests
        self.storage: dict[str, tuple[bytes, str, str | None]] = {}
        self.completed: set[str] = set()
        self.messages: list[tuple[int, dict]] = []
        self.entries: list[tuple[int, int, dict]] = []
        self.tokens: list[str] = []                # client IDs that asked for a token
        self.rejected_clients: set[str] = set()
        self.publishers = [{"id": 77, "name": "Hot Lake HR", "imageUrl": ""},
                           {"id": 78, "name": "Schedule Bot", "imageUrl": ""}]
        self.documents = [{"documentId": 52114, "name": "OLCC Service Permit", "documentType": "document",
                           "expiration": {"type": "entry"}}]
        self.replies: dict[str, list] = {}         # path start -> [(status, body, headers)] to send next
        self.server = None
        self._files = 0

    def reply_next(self, path_start, status, body=None, headers=None, times=1):
        self.replies.setdefault(path_start, []).extend([(status, body or {}, headers or {})] * times)

    def count(self, path_start=""):
        return sum(1 for _, path in self.calls if path.startswith(path_start))

    def app(self):
        app = web.Application(client_max_size=40 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    async def handle(self, request):
        path = request.path
        if path.startswith("/storage/"):
            # Connecteam's file storage: not its API, and never sent the key.
            self.storage[path[9:]] = (await request.read(), request.headers.get("Content-Type"),
                                      request.headers.get("Authorization"))
            return web.Response()
        self.calls.append((request.method, path))
        for start, queued in self.replies.items():
            if path.startswith(start) and queued:
                status, body, headers = queued.pop(0)
                return web.json_response(body, status=status, headers=headers)
        if path == "/oauth/v1/token":
            client = request.headers.get("Authorization", "")
            import base64
            client_id = base64.b64decode(client.split()[-1]).decode().split(":")[0]
            self.tokens.append(client_id)
            if client_id in self.rejected_clients:
                return web.json_response({"error": "invalid_client"}, status=401)
            return web.json_response({"access_token": f"token-{client_id}", "expires_in": 3600})
        if path == "/time-clock/v1/time-clocks":
            return web.json_response({"data": {"timeClocks": [{"id": 1}]}})
        if path.startswith("/time-clock/v1/time-clocks/1/time-activities"):
            return web.json_response({"data": {"timeActivitiesByUsers": [
                {"userId": 101, "shifts": [{"start": {"timestamp": time.time() - 600}, "end": None}]}]}})
        if path == "/publishers/v1/publishers":
            return web.json_response({"data": {"publishers": self.publishers}, "paging": {"offset": 0}})
        if path.startswith("/chat/v1/conversations/privateMessage/"):
            body = await request.json()
            if body["senderId"] not in [p["id"] for p in self.publishers]:
                return web.json_response({"detail": "Sender id not found"}, status=404)
            self.messages.append((int(path.rsplit("/", 1)[1]), body))
            return web.json_response({"requestId": "r", "data": {}})
        if path == "/attachments/v1/files/generate-upload-url":
            body = await request.json()
            assert body["featureType"] == "documents"
            self._files += 1
            file_id = f"9f1c2b3a-4d5e-4f60-8a7b-{self._files:012d}"
            return web.json_response({"data": {"fileId": file_id,
                                               "uploadFileUrl": f"{self.url}/storage/{file_id}?sig=x"}})
        if path.startswith("/attachments/v1/files/complete-upload/"):
            file_id = path.rsplit("/", 1)[1]
            if file_id not in self.storage:
                return web.json_response({"error": "not uploaded"}, status=400)
            self.completed.add(file_id)
            return web.json_response({"data": {"fileId": file_id}})
        if path == "/documents/v1/documents":
            return web.json_response({"data": {"documents": self.documents}, "paging": {"offset": 0}})
        if path.startswith("/documents/v1/users/") and path.endswith("/entry"):
            body = await request.json()
            if not set(body["fileIds"]) <= self.completed:
                return web.json_response({"error": "x", "extraData": {"errorCode": "FILE_NOT_FOUND"}},
                                         status=400)
            parts = path.split("/")
            self.entries.append((int(parts[4]), int(parts[6]), body))
            return web.json_response({"data": {"userId": int(parts[4]), "documentId": int(parts[6]),
                                               "documentType": "document", "status": "approved",
                                               "expiresAt": body.get("expiresAt")}})
        return web.json_response({"detail": "Not Found"}, status=404)


class Env:
    def __init__(self, fake, ct, hr, store):
        self.fake, self.ct, self.hr, self.store = fake, ct, hr, store


@contextlib.asynccontextmanager
async def environment(monkeypatch, hr_settings=None, enabled=True):
    fake = FakeConnecteam()
    fake.server = TestServer(fake.app())
    await fake.server.start_server()
    fake.url = str(fake.server.make_url("")).rstrip("/")
    monkeypatch.setattr(connecteam, "API_OVERRIDE", fake.url)
    store = {"ct": {}, "hr": {}}
    ct = Connecteam(lambda: (enabled, "staff-access-app", "secret", "global", 60), lambda: True,
                    (lambda: store["ct"], lambda u: store.__setitem__("ct", u)), "test",
                    hr_usage_store=(lambda: store["hr"], lambda u: store.__setitem__("hr", u)))
    settings = {"publisher": "Hot Lake HR", "documents": True} if hr_settings is None else hr_settings
    hr = HRConnecteam(ct, lambda: settings)
    await ct.start()
    try:
        yield Env(fake, ct, hr, store)
    finally:
        await ct.close()
        await fake.server.close()


def run(coro):
    return asyncio.run(coro)


def clock_checks_still_work(env):
    """Staff Access's clock check: not stopped, not paused, and it still finds who's clocked in."""
    ct = env.ct
    assert ct.halted is None and ct.paused_until == 0.0 and ct.failures == 0
    return ct.check()


# ---------------------------------------------------------------- chat

def test_chat_message_from_the_hr_publisher(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            await env.hr.send_chat("101", "You have something in the HR portal: https://hr.example")
            assert env.fake.messages == [(101, {"senderId": 77, "text": "You have something in the "
                                                "HR portal: https://hr.example"})]
            # A token, the publisher and the message; then just the message.
            assert env.fake.count() == 3
            await env.hr.send_chat(102, "Again")
            assert env.fake.count() == 4 and env.fake.count("/publishers") == 1
            # Every request counts in HR's lane and in Staff Access's budget.
            assert env.store["hr"]["count"] == 4 and env.store["ct"]["count"] == 4
            assert env.fake.tokens == ["staff-access-app"]
    run(scenario())


def test_chat_problems_in_plain_words(monkeypatch):
    async def scenario():
        async with environment(monkeypatch, {"publisher": "HR Bot"}) as env:
            with pytest.raises(CTError) as err:
                await env.hr.send_chat(101, "Hello")
            assert err.value.code == "no_publisher" and 'named "HR Bot"' in err.value.message
            assert "Custom publishers" in err.value.message
        async with environment(monkeypatch) as env:
            await env.hr.publisher_id()
            env.fake.publishers = []   # deleted in Connecteam since
            with pytest.raises(CTError) as err:
                await env.hr.send_chat(101, "Hello")
            assert err.value.code == "no_publisher" and env.hr._publisher is None
            env.fake.reply_next("/chat/", 404, {"detail": "User is suspended"})
            env.fake.publishers = [{"id": 77, "name": "Hot Lake HR"}]
            with pytest.raises(CTError) as err:
                await env.hr.send_chat(101, "Hello")
            assert err.value.code == "no_user"
            for bad in ((None, "x"), ("abc", "x"), ("0", "x"), (101, ""), (101, "x" * 600)):
                with pytest.raises(ValueError):
                    await env.hr.send_chat(*bad)
    run(scenario())


# ---------------------------------------------------------------- certificates

def test_certificate_copy_is_off_until_turned_on(monkeypatch):
    async def scenario():
        async with environment(monkeypatch, {"documents": False}) as env:
            with pytest.raises(CTError) as err:
                await env.hr.upload_certificate(101, 52114, [("card.jpg", "image/jpeg", b"jpeg")],
                                                date(TODAY.year + 4, 6, 14))
            assert err.value.code == "off"
            assert env.fake.count() == 0
    run(scenario())


def test_certificate_copy_uploads_each_page_then_sets_the_entry(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            expires = date(TODAY.year + 4, 6, 14)
            result = await env.hr.upload_certificate(
                101, 52114, [("front.jpg", "image/jpeg", b"front"), ("back.jpg", "image/jpeg", b"back")],
                expires)
            noon = int(datetime(expires.year, 6, 14, 12, tzinfo=timezone.utc).timestamp())
            assert result["status"] == "approved" and result["expires_at"] == noon
            assert len(result["file_ids"]) == 2
            user, document, body = env.fake.entries[0]
            assert (user, document, body) == (101, 52114, {"fileIds": result["file_ids"], "expiresAt": noon})
            # The pages went to storage as they are, without the key; that isn't an API request.
            stored = [env.fake.storage[f] for f in result["file_ids"]]
            assert stored == [(b"front", "image/jpeg", None), (b"back", "image/jpeg", None)]
            assert env.fake.count("/attachments/v1/files/generate-upload-url") == 2
            assert env.fake.count("/attachments/v1/files/complete-upload/") == 2
            # Two pages: five requests, plus the token.
            assert env.fake.count() == 6 and env.store["hr"]["count"] == 6
            env.ct._hr_recent.clear()   # a minute later
            assert [d["name"] for d in await env.hr.list_documents()] == ["OLCC Service Permit"]
    run(scenario())


def test_certificate_problems_become_to_dos(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            page = [("card.jpg", "image/jpeg", b"jpeg")]
            later = date(TODAY.year + 3, 1, 31)
            for code, kind, words in (("DOCUMENT_NOT_ASSIGNED", "not_assigned", "isn't assigned"),
                                      ("ENTRY_PENDING_REVIEW", "pending_review", "waiting for approval"),
                                      ("EXPIRATION_DATE_IN_PAST", "expired", "kept in Google Drive"),
                                      ("EXPIRATION_NOT_SUPPORTED", "setup", "Per entry"),
                                      ("INSUFFICIENT_PERMISSION", "not_allowed", "permission on this")):
                env.ct._hr_recent.clear()   # a minute apart
                env.fake.reply_next("/documents/v1/users/", 400 if code != "INSUFFICIENT_PERMISSION" else 403,
                                    {"error": "x", "extraData": {"errorCode": code}})
                with pytest.raises(CTError) as err:
                    await env.hr.upload_certificate(101, 52114, page, later)
                assert (err.value.code, err.value.retry) == (kind, False) and words in err.value.message
            # A pack permission is about one pack: copies aren't stopped.
            assert env.hr.stopped["documents"] is None
            # An expired card never costs a request.
            before = env.fake.count()
            with pytest.raises(CTError) as err:
                await env.hr.upload_certificate(101, 52114, page, date(2020, 1, 1))
            assert err.value.code == "expired" and env.fake.count() == before
            # Somewhere odd to send the file: refused.
            env.ct._hr_recent.clear()
            env.fake.reply_next("/attachments/v1/files/generate-upload-url", 200, {"data": {
                "fileId": "9f1c2b3a-4d5e-4f60-8a7b-aaaaaaaaaaaa", "uploadFileUrl": "ftp://elsewhere/x"}})
            with pytest.raises(CTError) as err:
                await env.hr.upload_certificate(101, 52114, page, later)
            assert err.value.code == "bad_reply"
            for bad in ((101, 0, page, later), (101, 52114, [], later), (101, 52114, page * 11, later),
                        (101, 52114, [("", "image/jpeg", b"x")], later)):
                with pytest.raises(ValueError):
                    await env.hr.upload_certificate(*bad)
    run(scenario())


# ---------------------------------------------------------------- HR never stops the clock checks

def test_hr_errors_never_stop_staff_access_clock_checks(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            # No Communications hub: chat is refused. Only HR's chat stops.
            env.fake.reply_next("/publishers/", 403, {"detail": "Forbidden"})
            with pytest.raises(CTError) as err:
                await env.hr.send_chat(101, "Hello")
            assert err.value.code == "not_allowed" and "Communications hub" in err.value.message
            assert env.hr.stopped["chat"] and env.hr.stopped["documents"] is None
            with pytest.raises(CTError) as err:
                await env.hr.send_chat(101, "Hello")
            assert err.value.code == "stopped"
            await clock_checks_still_work(env)
            assert env.ct.clocked_in == {"101"} and env.ct.last_error is None

            # No HR & Skills hub on Expert: documents are refused (403 with no error code).
            env.fake.reply_next("/attachments/", 403, {"detail": "Forbidden"})
            with pytest.raises(CTError) as err:
                await env.hr.upload_certificate(101, 52114, [("c.jpg", "image/jpeg", b"x")],
                                                date(TODAY.year + 2, 1, 1))
            assert err.value.code == "not_allowed" and "HR & Skills hub on Expert" in err.value.message
            env.ct.clocked_in = set()
            await clock_checks_still_work(env)
            assert env.ct.clocked_in == {"101"}

            # Too many requests for HR: HR waits, the clock checks don't.
            env.hr.forget()
            env.fake.reply_next("/publishers/", 429, {}, {"Retry-After": "120"})
            with pytest.raises(CTError) as err:
                await env.hr.send_chat(101, "Hello")
            assert err.value.code == "paused" and err.value.retry and env.ct.hr_paused_until > time.time()
            await clock_checks_still_work(env)

            # Connecteam failing or answering nonsense for HR: no effect either.
            env.ct.hr_paused_until = 0.0
            env.ct._hr_recent.clear()
            env.fake.reply_next("/publishers/", 500, {})
            with pytest.raises(CTError) as err:
                await env.hr.send_chat(101, "Hello")
            assert err.value.code == "unavailable" and err.value.retry
            env.fake.reply_next("/publishers/", 200, ["not", "an", "object"])
            with pytest.raises(CTError) as err:
                await env.hr.send_chat(101, "Hello")
            assert err.value.code == "bad_reply"
            env.ct.clocked_in = set()
            await clock_checks_still_work(env)
            assert env.ct.clocked_in == {"101"} and env.ct.last_error is None
    run(scenario())


def test_hr_keys_refused_stop_only_hr(monkeypatch):
    async def scenario():
        settings = {"client_id": "hr-app", "client_secret": "hr-secret", "documents": True}
        async with environment(monkeypatch, settings) as env:
            env.fake.rejected_clients.add("hr-app")
            with pytest.raises(CTError) as err:
                await env.hr.send_chat(101, "Hello")
            assert err.value.code == "not_allowed" and "Client ID and Secret" in err.value.message
            assert env.hr.stopped == {"chat": err.value.message, "documents": err.value.message}
            await clock_checks_still_work(env)
            assert env.ct.clocked_in == {"101"}
            # HR's own app, then the time clock's: two tokens, never shared.
            assert env.fake.tokens == ["hr-app", "staff-access-app"]
            # Test lifts the stop (and says what's wrong if it still is).
            env.fake.rejected_clients.clear()
            env.hr._last_token_try = 0.0
            results = await env.hr.test()
            assert results["chat"]["ok"] and results["documents"]["ok"]
            assert env.hr.stopped == {"chat": None, "documents": None}
    run(scenario())


# ---------------------------------------------------------------- HR's lane

def test_hr_lane_limits(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            await env.hr.send_chat(101, "Hello")   # token, publisher, message
            for _ in range(3):
                await env.hr.send_chat(101, "Hello")
            assert env.fake.count() == 6
            # 6 a minute.
            with pytest.raises(CTError) as err:
                await env.hr.send_chat(101, "Hello")
            assert err.value.code == "paused" and "this minute" in err.value.message
            assert env.fake.count() == 6
            # The clock checks still have Staff Access's own room this minute.
            await clock_checks_still_work(env)
            env.ct._hr_recent.clear()
            # 500 a day.
            env.store["hr"] = {"day": time.strftime("%Y-%m-%d", time.gmtime()), "count": ctapi.DAY_CAP}
            with pytest.raises(CTError) as err:
                await env.hr.send_chat(101, "Hello")
            assert err.value.code == "paused" and "500" in err.value.message
            env.store["hr"]["count"] = 0
            # Half of Staff Access's daily allowance used: HR waits, the time clock carries on.
            half = int(env.ct.daily_cap() * ctapi.STOP_SHARE)
            env.store["ct"] = {"day": time.strftime("%Y-%m-%d", time.gmtime()), "count": half}
            with pytest.raises(CTError) as err:
                await env.hr.send_chat(101, "Hello")
            assert err.value.code == "paused" and "time clock" in err.value.message
            env.ct.clocked_in = set()
            await clock_checks_still_work(env)
            assert env.ct.clocked_in == {"101"}
            assert env.store["ct"]["count"] > half
            # A certificate copy needs room for all of it, or it doesn't start.
            env.store["ct"]["count"] = 0
            env.ct._hr_recent.extend([time.time()] * 3)
            before = env.fake.count()
            with pytest.raises(CTError) as err:
                await env.hr.upload_certificate(101, 52114, [("a.jpg", "image/jpeg", b"a"),
                                                             ("b.jpg", "image/jpeg", b"b")],
                                                date(TODAY.year + 3, 1, 1))
            assert err.value.code == "paused" and env.fake.count() == before
    run(scenario())


def test_staff_access_pauses_hold_hr_too(monkeypatch):
    async def scenario():
        async with environment(monkeypatch) as env:
            env.ct._pause(time.time() + 600, "Your Connecteam account has only 50 of its 10000 API "
                                             "requests left today.")
            with pytest.raises(CTError) as err:
                await env.hr.send_chat(101, "Hello")
            assert err.value.code == "paused" and "only 50" in err.value.message
            assert env.fake.count() == 0
        # The account running low, seen in HR's own reply, pauses everyone (as before).
        async with environment(monkeypatch) as env:
            env.fake.reply_next("/publishers/", 200, {"data": {"publishers": [{"id": 77, "name": "Hot Lake HR"}]}},
                                {"x-ratelimit-day-limit": "10000", "x-ratelimit-day-remaining": "50"})
            await env.hr.publisher_id()
            assert env.ct.paused_until > time.time()
    run(scenario())


def test_connecteam_off_or_not_set_up(monkeypatch):
    async def scenario():
        async with environment(monkeypatch, enabled=False) as env:
            with pytest.raises(CTError) as err:
                await env.hr.send_chat(101, "Hello")
            assert err.value.code == "not_set_up" and env.fake.count() == 0
    run(scenario())


def test_module_level_calls_use_the_configured_lane(monkeypatch):
    monkeypatch.setattr(ctapi, "default", None)
    with pytest.raises(CTError):
        ctapi.lane()

    async def scenario():
        async with environment(monkeypatch) as env:
            ctapi.configure(env.ct, lambda: {"documents": True})
            await ctapi.send_chat(101, "Hello")
            assert env.fake.messages[-1] == (101, {"senderId": 77, "text": "Hello"})
            assert (await ctapi.upload_certificate(101, 52114, [("a.pdf", "application/pdf", b"%PDF")],
                                                   None))["status"] == "approved"
            assert "expiresAt" not in env.fake.entries[-1][2]
            assert ctapi.lane().status()["day_cap"] == ctapi.DAY_CAP
    run(scenario())
