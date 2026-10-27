"""Shared setup for the HR portal's tests: a fake Connecteam with what HR reads (users with
mobile numbers, emails and profile fields; archived users; profile field names; the time clock),
and an HR harness: a fresh Staff Access database, a fresh HR folder, the HR web app and the
dashboard as test clients.

Nothing here talks to a real Connecteam, Google, TT Hotel or Home Assistant: Connecteam's
address points at the fake (or at a closed local port when no fake is running).
"""

import base64
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from test_wristbands_server import Portal, free_port

import connecteam  # noqa: E402
import main  # noqa: E402
import transfer  # noqa: E402
from hr import crypto, delivery, hrdb, people, server as hr_server, signin, totp  # noqa: E402
from hr.service import HRService  # noqa: E402

HOST = "hr.example.com"
ORIGIN = f"https://{HOST}"
CHANGE = {"Host": HOST, "Origin": ORIGIN, "X-Requested-With": "hr-portal",
          "Sec-Fetch-Site": "same-origin"}
READ = {"Host": HOST, "Sec-Fetch-Site": "same-origin"}
NOWHERE = f"http://127.0.0.1:{free_port()}"   # nothing listens: a stray request fails here
connecteam.API_OVERRIDE = NOWHERE


def ct_user(user_id, first, last="", code=None, groups=(), mobile=None, email=None, fields=None,
            created=1_700_000_000, archived=False):
    return {"userId": user_id, "firstName": first, "lastName": last, "kioskCode": code,
            "smartGroupsIds": list(groups), "isArchived": archived, "phoneNumber": mobile,
            "email": email, "createdAt": created,
            "customFields": [{"customFieldId": k, "value": v} for k, v in (fields or {}).items()]}


class FakeConnecteam:
    """Connecteam as Staff Access and the HR portal read it, and as reports and tasks read it
    (ctdata.py). refuse: paths answered with 403 (an entry ending in '*' is a prefix); too_many:
    paths answered with 429. tokens: the client IDs that asked for a token; asked: each
    request's query, as (path, {name: value}).

    For reports and tasks (build them with ct_activity, ct_shift, ct_form, ct_submission):
    - activities: {time clock ID: [time activities by user]} (clock 1 by default; clocked_in
      people are added to clock 1 as open shifts);
    - schedulers: [{"schedulerId", "name", "isArchived"}] and shifts: {scheduler ID: [shift]};
    - jobs: {instance ID (time clock or scheduler): [{"jobId", "title", "subJobs"}]};
    - forms: [form] and submissions: {form ID: [submission]};
    - policy_types: time off policy types [{"id", "name"}] (/time-off/v1/policy-types).
    Lists are paged with limit and offset as Connecteam does (paging.offset when more follow),
    shifts are filtered by startTime and endTime, and submissions by submittingStartTimestamp and
    submittingEndTime."""

    def __init__(self, users=(), archived=(), fields=(), clocked_in=()):
        self.users = list(users)
        self.archived = list(archived)
        self.fields = list(fields)
        self.clocked_in = list(clocked_in)
        self.activities: dict[int, list[dict]] = {}
        self.schedulers: list[dict] = []
        self.shifts: dict[int, list[dict]] = {}
        self.jobs: dict[int, list[dict]] = {}
        self.forms: list[dict] = []
        self.submissions: dict[int, list[dict]] = {}
        self.policy_types: list[dict] = []
        self.asked: list[tuple[str, dict]] = []
        self.refuse: set[str] = set()
        self.too_many: set[str] = set()
        self.gone: set[str] = set()      # paths answered 404 (a feature the plan doesn't have)
        self.calls: list[str] = []
        self.tokens: list[str] = []
        self.port = free_port()
        self.app = web.Application()
        self.app.router.add_route("*", "/{tail:.*}", self.handle)
        self.runner = None

    async def handle(self, request):
        path = request.path
        self.calls.append(path + ("?archived" if request.query.get("userStatus") == "archived" else ""))
        self.asked.append((path, dict(request.query)))
        if path in self.refuse or any(p.endswith("*") and path.startswith(p[:-1]) for p in self.refuse):
            return web.json_response({"detail": "Missing scope"}, status=403)
        if path in self.gone:
            return web.json_response({"error": "not here"}, status=404)
        if path in self.too_many:
            return web.json_response({"detail": "Too many requests"}, status=429,
                                     headers={"Retry-After": "120"})
        if path == "/oauth/v1/token":
            auth = request.headers.get("Authorization", "").split()[-1]
            self.tokens.append(base64.b64decode(auth).decode().split(":")[0])
            return web.json_response({"access_token": "fake", "token_type": "Bearer", "expires_in": 3600,
                                      "scope": "users.read time_clock.read"})
        if path == "/users/v1/users":
            source = self.archived if request.query.get("userStatus") == "archived" else self.users
            offset = int(request.query.get("offset", 0))
            return web.json_response({"data": {"users": source[offset:offset + 500]}})
        if path == "/users/v1/custom-fields":
            return web.json_response({"data": {"customFields": self.fields}})
        if path == "/users/v1/smart-groups":
            return web.json_response({"data": {"groups": []}})
        if path == "/time-clock/v1/time-clocks":
            ids = sorted({1, *self.activities})
            return web.json_response({"data": {"timeClocks": [{"id": i} for i in ids]}})
        if path.startswith("/time-clock/v1/time-clocks/"):
            now = time.time()
            clock = int(path.split("/")[4])
            open_shifts = [{"userId": uid, "shifts": [{"id": f"open-{uid}",
                                                       "start": {"timestamp": now - 600}}]}
                           for uid in self.clocked_in] if clock == 1 else []
            return web.json_response({"data": {"timeActivitiesByUsers": [
                *open_shifts, *self.activities.get(clock, [])]}})
        if path == "/scheduler/v1/schedulers":
            return web.json_response({"data": {"schedulers": self.schedulers}})
        if path.startswith("/scheduler/v1/schedulers/") and path.endswith("/shifts"):
            q = request.query
            low, high = int(q.get("startTime", 0)), int(q.get("endTime", 2**40))
            found = [s for s in self.shifts.get(int(path.split("/")[4]), [])
                     if low <= s["startTime"] <= high]
            return self.page(request, "shifts", found)
        if path == "/jobs/v1/jobs":
            found = {}
            for instance in request.query.getall("instanceIds", []):
                for job in self.jobs.get(int(instance), []):
                    found.setdefault(job["jobId"], job)
            return self.page(request, "jobs", list(found.values()))
        if path == "/time-off/v1/policy-types":
            return web.json_response({"data": {"policyTypes": self.policy_types}})
        if path == "/forms/v1/forms":
            return self.page(request, "forms", self.forms)
        if path.startswith("/forms/v1/forms/") and path.endswith("/form-submissions"):
            q = request.query
            low = int(q.get("submittingStartTimestamp", 0))
            high = int(q.get("submittingEndTime", 2**40))
            found = [s for s in self.submissions.get(int(path.split("/")[4]), [])
                     if low <= s["submissionTimestamp"] <= high]
            return self.page(request, "formSubmissions", found)
        return web.json_response({"error": "not here"}, status=404)

    @staticmethod
    def page(request, key: str, items: list):
        """One page of a list, as Connecteam pages it."""
        limit, offset = int(request.query.get("limit", 10)), int(request.query.get("offset", 0))
        body = {"data": {key: items[offset:offset + limit]}, "paging": {}}
        if offset + limit < len(items):
            body["paging"]["offset"] = offset + limit
        return web.json_response(body)

    async def __aenter__(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        connecteam.API_OVERRIDE = f"http://127.0.0.1:{self.port}"
        fresh_connecteam()
        await main.connecteam.start()
        return self

    async def __aexit__(self, *exc):
        await main.connecteam.close()
        connecteam.API_OVERRIDE = NOWHERE
        await self.runner.cleanup()

    def count(self, path: str) -> int:
        return sum(1 for c in self.calls if c == path)


def ct_activity(user_id, shifts=(), breaks=(), time_offs=()) -> dict:
    """One person's time activities on a time clock. shifts: (start, end or None, job ID or None,
    scheduler shift ID or None) with times as Unix seconds; breaks: (start, end)."""
    return {"userId": user_id,
            "shifts": [{"id": f"s{user_id}-{start}", "start": {"timestamp": start, "timezone": "UTC"},
                        "end": {"timestamp": end} if end else None, "jobId": job,
                        "schedulerShiftId": planned} for start, end, job, planned in shifts],
            "manualBreaks": [{"id": f"b{user_id}-{start}", "manualBreakId": 9,
                              "start": {"timestamp": start}, "end": {"timestamp": end}}
                             for start, end in breaks],
            "timeOffs": list(time_offs)}


def ct_shift(shift_id, start, end, users=(), title="", job=None, published=True,
             open_shift=False) -> dict:
    """A scheduled shift (times as Unix seconds)."""
    return {"id": shift_id, "title": title, "startTime": start, "endTime": end,
            "timezone": "UTC", "assignedUserIds": list(users), "jobId": job,
            "isOpenShift": open_shift, "isPublished": published, "breaks": [], "statuses": []}


def ct_form(form_id, name, questions=()) -> dict:
    """A form; questions: (question ID, title, type)."""
    return {"formId": form_id, "formName": name, "createdAt": 1_700_000_000,
            "lastUpdatedAt": 1_700_000_000, "settings": {},
            "questions": [{"questionId": q, "title": t, "questionType": k, "description": ""}
                          for q, t, k in questions]}


def ct_submission(submission_id, form_id, user_id, at, answers=()) -> dict:
    """A form submission at Unix time `at`. answers: Connecteam answer objects, such as
    {"questionId": "q1", "questionType": "openEnded", "value": "Leaking tap",
    "wasSubmittedEmpty": False}."""
    return {"formSubmissionId": submission_id, "formId": form_id, "submissionTimestamp": at,
            "submissionTimezone": "UTC", "entryNum": 1, "submittingUserId": user_id,
            "isAnonymous": user_id is None, "managerFields": [], "answers": list(answers),
            "submissionStartTimestamp": at - 60}


def fresh_connecteam() -> None:
    c = main.connecteam
    c.forget()
    c.paused_until = 0.0
    c.pause_reason = None
    c._recent.clear()
    c._hr_recent.clear()
    c._reports_recent.clear()
    c.reports_paused_until = 0.0
    c.reports_pause_reason = None
    c.reports_failures = 0
    c.hr_paused_until = 0.0
    c.hr_pause_reason = None
    c.hr_failures = 0
    c.limits.clear()
    main.connecteam_tested[0] = 0.0


class HR:
    """An install with Connecteam on and the HR portal set up. fresh: nothing else set up (no
    staff, phones or roles), as a migration needs."""

    def __init__(self, tmp_path, enabled=True, fresh=False):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.p = Portal(tmp_path)
        self.db = main.db
        if fresh:
            for table in ("staff_devices", "device_tokens", "staff", "devices", "role_entities",
                          "roles", "wb_jobs"):
                self.db.conn.execute(f"DELETE FROM {table}")
            self.db.conn.commit()
        self.db.update_settings({"connecteam_enabled": "1", "connecteam_client_id": "client",
                                 "connecteam_client_secret": "secret",
                                 "hr_enabled": "1" if enabled else "0", "hr_host": HOST,
                                 "property_name": "Hot Lake"})
        fresh_connecteam()
        self.folder = str(tmp_path / "data")
        self.previous = main.hr_service   # put back afterwards: its key seals settings (main.HRSealer)
        self.svc = main.hr_service = HRService(
            self.folder, main_settings=lambda: main.db.settings(), connecteam=main.connecteam,
            departments=main.hr_departments, group_names=lambda: {"501": "Pub", "502": "Kitchen"},
            from_tunnel=lambda remote: main.from_tunnel(remote),
            client_ip=lambda request: main.client_ip(request),
            property_name=lambda: "Hot Lake", icon=lambda: b"\x89PNG fake",
            reseal=lambda: main.db.reseal_settings())
        # Tests drive the background work by hand (svc.tick(), sync_people(), nightly()), and the
        # nightly run doesn't start by itself whatever the time of day.
        self.svc.start = lambda background=False, _s=self.svc: HRService.start(_s, False)
        self.svc.daily_state["day"] = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
        self.client = self.admin = None
        self.pins: dict[str, str] = {}

    async def __aenter__(self):
        if self.svc.enabled():
            assert self.svc.start() == "on"
        self.client = TestClient(TestServer(hr_server.make_app(self.svc)))
        await self.client.start_server()
        self.admin = TestClient(TestServer(main.admin_app()))
        await self.admin.start_server()
        await main.tthotel.start()
        return self

    async def __aexit__(self, *exc):
        await self.client.close()
        await self.admin.close()
        await main.encoders.close_all()   # the Front Desk PC's encoder runner (from /api/state)
        await main.tthotel.close()
        await self.svc.stop()
        main.hr_service = self.previous

    @property
    def hdb(self) -> hrdb.HRDatabase:
        return self.svc.db

    # ------------------------------------------------------------ people

    async def add_people(self, *users):
        """Put people in the HR portal as a sync would ([ct_user(...)]), remembering PINs."""
        listed = [{"id": str(u["userId"]), "name": f"{u['firstName']} {u['lastName']}".strip(),
                   "kiosk_code": u["kioskCode"], "groups": [str(g) for g in u["smartGroupsIds"]],
                   "mobile": u["phoneNumber"], "email": u["email"], "created_at": u["createdAt"],
                   "fields": {str(f["customFieldId"]): f["value"] for f in u["customFields"]}}
                  for u in users]
        for u in listed:
            if u["kiosk_code"]:
                self.pins[u["id"]] = u["kiosk_code"]
        return await people.apply_users(self.hdb, listed)

    def give(self, person_id, role, groups=None):
        self.hdb.set_role(person_id, role, True, by="test")
        if groups is not None:
            self.hdb.set_manager_groups(person_id, groups)

    def enrol(self, person_id) -> str:
        """Turn on an authenticator for someone directly, as from HR's setup code; returns its
        secret."""
        secret = totp.new_secret()
        signin.enable_totp(self.hdb, person_id, secret, 0, "hr")
        return secret

    # ------------------------------------------------------------ requests

    async def call(self, method, path, body=None, cookie=None, headers=None):
        hdrs = dict(CHANGE if method not in ("GET", "HEAD") else READ)
        if cookie:
            hdrs["Cookie"] = f"{signin.COOKIE}={cookie}"
        hdrs.update(headers or {})
        resp = await self.client.request(method, path, json=body, headers=hdrs)
        text = await resp.text()
        try:
            data = json.loads(text)
        except ValueError:
            data = text
        return resp.status, data, resp

    async def sign_in(self, person_id, code="", pin=None, mobile=None) -> str:
        p = self.hdb.person(person_id)
        status, data, resp = await self.call("POST", "/api/signin", {
            "mobile": mobile or people.mobile_of(p), "pin": pin or self.pins[person_id],
            "code": code})
        assert status == 200, data
        return resp.cookies[signin.COOKIE].value

    async def confirm(self, cookie, person_id, secret=None):
        """Re-confirm a session (sensitive actions): with a fresh code, or the PIN."""
        body = {"code": next_code(self, person_id, secret)} if secret else {"pin": self.pins[person_id]}
        status, data, _ = await self.call("POST", "/api/reconfirm", body, cookie)
        assert status == 200, data

    async def admin_call(self, method, path, body=None):
        resp = await self.admin.request(method, path, headers={"X-Requested-With": "staff-portal"},
                                        json=body)
        raw = await resp.read()
        if raw[:2] == transfer.GZIP_MAGIC:
            raw = transfer.gunzip(raw)   # a backup download
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, raw.decode()


async def queue_done(hr: HR, now: float | None = None) -> None:
    """The documents part's delivery queue has done what's due: PDFs made, files sent."""
    await delivery.settle(hr.svc)
    await delivery.run_due(hr.svc, now)
    await delivery.settle(hr.svc)


def next_code(hr: HR, person_id, secret) -> str:
    """A code for the next time step not used yet (each code works once)."""
    used = hr.hdb.person(person_id)["totp_step"]
    step = max(int(time.time() // 30) - 1, used + 1)
    return totp.code_at(secret, step)


def wipe_key(hr: HR) -> None:
    crypto.use(None)
