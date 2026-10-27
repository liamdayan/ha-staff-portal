"""Connecteam data for reports and tasks: timesheets, scheduled shifts, forms and their
submissions, and the user list, read through Connecteam's API in the reports lane of
connecteam.py (at most REPORTS_MINUTE_CAP requests a minute and REPORTS_DAILY_CAP a day, and none
once Staff Access has used half its daily share). A refusal or a "too many requests" stops only
reports and tasks; the clock checks carry on.

Nothing is asked until a report is opened or a task source is due. Answers are kept by range:
the same range is asked again at most every MIN_REFRESH seconds, and the lists that hardly change
(time clocks, schedules, jobs, forms) are reused for LISTS_TTL. Each call raises Unavailable with
a plain reason when Connecteam can't answer (a missing scope, the budget, or no connection).

Endpoints and the OAuth scopes they need (Integration Center -> OAuth 2.0):
- users:       GET /users/v1/users                                  users.read
- timesheets:  GET /time-clock/v1/time-clocks                       time_clock.read
               GET /time-clock/v1/time-clocks/{id}/time-activities  time_clock.read
- shifts:      GET /scheduler/v1/schedulers                         schedule.read
               GET /scheduler/v1/schedulers/{id}/shifts             schedule.read
- job names:   GET /jobs/v1/jobs?instanceIds=...                    jobs.read (optional: without
               it, jobs show without names)
- forms:       GET /forms/v1/forms                                  forms.read
               GET /forms/v1/forms/{id}/form-submissions            forms.read
- time off:    GET /time-off/v1/policy-types                        time_off.read (optional:
               without it, time off shows as "Time off" with no policy name)

A refused permission isn't asked again for REFUSED_TTL (or until the Connecteam settings are
saved), so a missing scope costs one request, not one per report opened.

Times are given in the property's time zone (configure's zone) as "YYYY-MM-DDTHH:MM", dates as
"YYYY-MM-DD", and Connecteam user IDs as text.
"""

import asyncio
import logging
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from connecteam import ConnecteamError, ReportsBusy, ReportsRefused, scope_for

log = logging.getLogger("staff_portal")

MIN_REFRESH = 10 * 60      # the same range is asked again at most this often
LISTS_TTL = 6 * 3600       # time clocks, schedules, jobs and forms
USERS_TTL = 3600           # the user list (shared with Staff Access's own copy)
MAX_RANGE_DAYS = 400       # the longest range one call may ask about
CHUNK_DAYS = 90            # time activities: Connecteam allows at most 92 days per request
MAX_PAGES = 20             # pages read for one list, at most
MAX_WAIT = 70              # seconds a call waits for the minute's requests to have room
MAX_CACHED = 64            # answers kept
MAX_ANSWER = 2000          # characters of one form answer
MAX_ANSWERS = 200          # answers kept from one submission
REFUSED_TTL = 30 * 60      # a permission Connecteam refused isn't asked again for this long


class Unavailable(Exception):
    """Connecteam can't give this now; the message says why, in plain words."""


_ct = None                                  # connecteam.Connecteam
_zone = lambda: ZoneInfo("UTC")             # noqa: E731 - the property's time zone
_cache: dict[tuple, tuple[object, float]] = {}
_locks: dict[tuple, asyncio.Lock] = {}
_refused: dict[str, float] = {}             # optional lists Connecteam refused, and when
_denied: dict[str, tuple[str, float]] = {}  # permission: (Connecteam's refusal, when)


def configure(connecteam, zone) -> None:
    """Use this Connecteam client, and zone() for the property's time zone (a ZoneInfo)."""
    global _ct, _zone
    _ct, _zone = connecteam, zone
    if forget not in connecteam.on_forget:
        connecteam.on_forget.append(forget)
    forget()


def forget() -> None:
    """Drop every copy kept (the Connecteam settings changed)."""
    _cache.clear()
    _refused.clear()
    _denied.clear()


# ---------------------------------------------------------------- asking

def _refusal(err: ReportsRefused) -> str:
    """A refusal in plain words: connecteam.py quotes Connecteam's reply and names the
    permission (scope) that's missing, or says it's the plan."""
    return str(err)


def _check_denied(scope: str) -> None:
    """A permission refused lately: the same refusal again, without asking."""
    hit = _denied.get(scope)
    if hit and time.time() - hit[1] < REFUSED_TTL:
        raise Unavailable(hit[0])


def _deny(scope: str, err: ReportsRefused) -> Unavailable:
    _denied[scope] = (_refusal(err), time.time())
    return Unavailable(_refusal(err))


async def _ask(path: str, params: dict | None = None) -> dict:
    """One request in the reports lane, waiting (up to MAX_WAIT in all) when only the minute's
    requests are used up. Raises Unavailable."""
    if _ct is None:
        raise Unavailable("Connecteam isn't set up for reports.")
    scope = scope_for(path)
    _check_denied(scope)
    waited = 0.0
    while True:
        try:
            return await _ct._get(path, params, reports=True)
        except ReportsBusy as err:
            if err.wait is None or waited + err.wait > MAX_WAIT:
                raise Unavailable(str(err)) from None
            waited += err.wait
            await asyncio.sleep(err.wait)
        except ReportsRefused as err:
            raise _deny(scope, err) from None
        except ConnecteamError as err:
            raise Unavailable(f"Connecteam can't be reached right now: {err}") from None


async def _pages(path: str, params: dict, key: str, limit: int) -> list:
    """Every item of a paged list (at most MAX_PAGES pages)."""
    found, offset = [], 0
    for _ in range(MAX_PAGES):
        query = [(k, str(x)) for k, v in params.items() for x in (v if isinstance(v, list) else [v])]
        data = await _ask(path, query + [("limit", str(limit)), ("offset", str(offset))])
        page = _list(data, key)
        found.extend(page)
        paging = data.get("_paging") if isinstance(data.get("_paging"), dict) else {}
        nxt = paging.get("offset")
        if (len(page) < limit or not isinstance(nxt, int) or isinstance(nxt, bool)
                or nxt <= offset):
            break
        offset = nxt
    return found


async def _kept(key: tuple, max_age: float, fetch):
    """fetch()'s answer, reused for max_age; one fetch at a time for the same key."""
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < max_age:
        return hit[0]
    if len(_locks) > 4 * MAX_CACHED:
        for old in [k for k, held in _locks.items() if not held.locked()]:
            del _locks[old]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[1] < max_age:
            return hit[0]
        value = await fetch()
        if len(_cache) >= MAX_CACHED:
            for old in sorted(_cache, key=lambda k: _cache[k][1])[:len(_cache) - MAX_CACHED + 1]:
                _cache.pop(old, None)
                _locks.pop(old, None)
        _cache[key] = (value, time.time())
        return value


async def _optional(name: str, fetch, empty):
    """A list reports can do without (job names): empty when Connecteam refuses it, and not
    asked again for LISTS_TTL."""
    if time.time() - _refused.get(name, 0) < LISTS_TTL:
        return empty
    try:
        return await fetch()
    except Unavailable as err:
        if "refused" in str(err):
            _refused[name] = time.time()
            return empty
        raise


# ---------------------------------------------------------------- times

def _range(start, end) -> tuple[date, date]:
    start, end = _day(start), _day(end)
    if end < start:
        raise ValueError("The end date is before the start date.")
    if (end - start).days > MAX_RANGE_DAYS:
        raise ValueError(f"Pick at most {MAX_RANGE_DAYS} days.")
    return start, end


def _day(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _midnight(day: date) -> int:
    return int(datetime.combine(day, dtime(0), _zone()).timestamp())


def _local(ts) -> datetime | None:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts != ts:
        return None
    ts = ts / 1000 if ts > 1e11 else ts
    if not 0 < ts < 1e11:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(_zone())


def _text(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M") if dt else None


def _stamp(point) -> float | None:
    """A time activity's start or end ({"timestamp": ...}), as seconds."""
    ts = point.get("timestamp") if isinstance(point, dict) else None
    dt = _local(ts)
    return dt.timestamp() if dt else None


def _uid(value) -> str | None:
    return None if value is None or isinstance(value, bool) else str(value)[:40]


def _list(data: dict, key: str) -> list:
    value = data.get(key) if isinstance(data, dict) else None
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    for value in (data or {}).values():
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
    return []


def _short(value, limit: int = 200) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return " ".join(str(value).split())[:limit]


# ---------------------------------------------------------------- lists

async def _names() -> dict[str, str]:
    return {u["user_id"]: u["name"] for u in await users()}


async def users() -> list[dict]:
    """Active Connecteam users as [{user_id, name, groups}] (groups: smart group IDs), from the
    user list Staff Access keeps (asked again at most hourly, in the reports lane)."""
    if _ct is None:
        raise Unavailable("Connecteam isn't set up for reports.")
    _check_denied("users.read")
    try:
        found = await _ct.users(max_age=USERS_TTL, reports=True)
    except ReportsBusy as err:
        raise Unavailable(str(err)) from None
    except ReportsRefused as err:
        raise _deny("users.read", err) from None
    except ConnecteamError as err:
        raise Unavailable(f"Connecteam can't be reached right now: {err}") from None
    return [{"user_id": u["id"], "name": u["name"], "groups": list(u["groups"])} for u in found]


async def _clocks() -> list[str]:
    async def fetch():
        _check_denied("time_clock.read")
        try:
            return await _ct.time_clock_ids(max_age=LISTS_TTL, reports=True)
        except ReportsBusy as err:
            raise Unavailable(str(err)) from None
        except ReportsRefused as err:
            raise _deny("time_clock.read", err) from None
        except ConnecteamError as err:
            raise Unavailable(f"Connecteam can't be reached right now: {err}") from None
    if _ct is None:
        raise Unavailable("Connecteam isn't set up for reports.")
    return await _kept(("clocks",), LISTS_TTL, fetch)


async def _schedulers() -> list[str]:
    async def fetch():
        data = await _ask("/scheduler/v1/schedulers")
        return [str(s["schedulerId"]) for s in _list(data, "schedulers")
                if s.get("schedulerId") is not None and not s.get("isArchived")]
    return await _kept(("schedulers",), LISTS_TTL, fetch)


async def _jobs(instances: list[str]) -> dict[str, str]:
    """{job ID: name} for the jobs of these time clocks or schedules (one request for all of
    them), sub-jobs too ("Parent: Sub")."""
    async def fetch():
        found = {}
        for job in await _pages("/jobs/v1/jobs", {"instanceIds": list(instances)}, "jobs", 500):
            title = _short(job.get("title"), 80)
            if job.get("jobId") is not None:
                found[str(job["jobId"])] = title
            for sub in job.get("subJobs") or []:
                if isinstance(sub, dict) and sub.get("jobId") is not None:
                    found[str(sub["jobId"])] = ": ".join(filter(None, (title, _short(sub.get("title"), 80))))
        return found
    if not instances:
        return {}
    return await _optional("jobs", lambda: _kept(("jobs", *instances), LISTS_TTL, fetch), {})


async def _policies() -> dict[str, str]:
    """{time off policy type ID: its name} (optional: needs time_off.read)."""
    async def fetch():
        data = await _ask("/time-off/v1/policy-types")
        return {str(p["id"]): _short(p.get("name"), 80) for p in _list(data, "policyTypes")
                if p.get("id") is not None}
    return await _optional("policies", lambda: _kept(("policies",), LISTS_TTL, fetch), {})


# ---------------------------------------------------------------- timesheets

async def timesheets(start, end) -> list[dict]:
    """Every time clock's punches between two dates (inclusive), as [{user_id, name, date,
    clock_in, clock_out, break_minutes, job, type, hours, sched_start, sched_end}], sorted by
    name and clock-in. type: "shift" (worked time), "break" (a manual break: clock_in to
    clock_out, with its length in break_minutes) or "time_off" (leave: one row a day, job its
    policy's name, policy_id, and hours, or days when Connecteam counts that policy in days;
    clock_in and clock_out only for part of a day). clock_out is None while someone is still on
    the clock; hours is None then too. sched_start and sched_end are the scheduled shift the
    punch was for, when Connecteam links one. A punch on a sub-job is named "Job: Sub-job"."""
    start, end = _range(start, end)
    return await _kept(("timesheets", start, end), MIN_REFRESH,
                       lambda: _fetch_timesheets(start, end))


async def _fetch_timesheets(start: date, end: date) -> list[dict]:
    names = await _names()
    raw = []
    clocks = await _clocks()
    jobs = await _jobs(clocks)
    for clock in clocks:
        day = start - timedelta(days=1)   # a day either side: Connecteam's days are its own zone's
        while day <= end + timedelta(days=1):
            last = min(day + timedelta(days=CHUNK_DAYS - 1), end + timedelta(days=1))
            data = await _ask(f"/time-clock/v1/time-clocks/{clock}/time-activities",
                              {"startDate": day.isoformat(), "endDate": last.isoformat()})
            raw.extend((entry, jobs) for entry in _list(data, "timeActivitiesByUsers"))
            day = last + timedelta(days=1)
    linked = any(s.get("schedulerShiftId") for entry, _ in raw
                 for s in entry.get("shifts") or [] if isinstance(s, dict))
    policies = {}
    if any(entry.get("timeOffs") for entry, _ in raw):
        policies = await _policies()
    planned = {}
    if linked:
        try:
            planned = {s["id"]: s for s in await _shift_rows(start, end) if s["id"]}
        except Unavailable:
            planned = {}   # no schedule.read: punches without their scheduled times
    rows, seen = [], set()
    for entry, jobs in raw:
        uid = _uid(entry.get("userId"))
        if uid is None:
            continue
        name = names.get(uid, f"User {uid}")
        for kind, items in (("shift", entry.get("shifts")), ("break", entry.get("manualBreaks")),
                            ("time_off", entry.get("timeOffs"))):
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                if item.get("id") is not None:
                    if (kind, str(item["id"])) in seen:
                        continue
                    seen.add((kind, str(item["id"])))
                found = (_time_off(item, uid, name, policies) if kind == "time_off"
                         else [_punch(kind, item, uid, name, jobs, planned)])
                rows.extend(row for row in found
                            if row and start.isoformat() <= row["date"] <= end.isoformat())
    rows.sort(key=lambda r: (r["name"].lower(), r["clock_in"] or r["date"]))
    return rows


def _punch(kind: str, item: dict, uid: str, name: str, jobs: dict, planned: dict) -> dict | None:
    began, ended = _stamp(item.get("start")), _stamp(item.get("end"))
    if began is None:
        return None
    if ended is not None and ended < began:
        ended = None
    minutes = round((ended - began) / 60) if ended is not None else None
    plan = planned.get(str(item.get("schedulerShiftId") or "")) if kind == "shift" else None
    job = _job(item, jobs) if kind == "shift" else "Break"
    hours = round(minutes / 60, 2) if minutes is not None and kind != "break" else None
    return {"user_id": uid, "name": name, "date": _local(began).date().isoformat(),
            "clock_in": _text(_local(began)), "clock_out": _text(_local(ended)),
            "break_minutes": (minutes or 0) if kind == "break" else 0,
            "job": job, "type": kind, "hours": hours,
            "sched_start": plan["start"] if plan else None, "sched_end": plan["end"] if plan else None}


def _job(item: dict, jobs: dict) -> str:
    """A punch's or shift's job: its sub-job when it has one ("Parent: Sub")."""
    for key in ("subJobId", "jobId"):
        if item.get(key) not in (None, "") and str(item[key]) in jobs:
            return jobs[str(item[key])]
    return ""


def _time_off(item: dict, uid: str, name: str, policies: dict) -> list[dict]:
    """Connecteam's time off (policyTypeId, duration {value, units: hours or days}, isAllDay),
    as one row a day: an item over several days is split evenly across them. hours is set for
    hours; days (a day's share) for days, which the report turns into hours by its shift length.
    A part of a day keeps its clock times."""
    began, ended = _local((item.get("start") or {}).get("timestamp")), _local((item.get("end") or {}).get("timestamp"))
    if began is None:
        return []
    last = (ended - timedelta(seconds=1)).date() if ended and ended > began else began.date()
    days = [began.date() + timedelta(days=i) for i in range(min((last - began.date()).days + 1, 366))]
    size = item.get("duration") if isinstance(item.get("duration"), dict) else {}
    value = size.get("value")
    value = value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None
    units = size.get("units") if size.get("units") in ("hours", "days") else "hours"
    if value is None and ended and not item.get("isAllDay"):
        value, units = (ended - began).total_seconds() / 3600, "hours"
    policy_id = _short(item.get("policyTypeId"), 80) or None
    whole = item.get("isAllDay") or len(days) > 1
    share = round(value / len(days), 2) if value is not None else None
    return [{"user_id": uid, "name": name, "date": day.isoformat(),
             "clock_in": None if whole else _text(began), "clock_out": None if whole else _text(ended),
             "break_minutes": 0, "job": policies.get(policy_id or "", "") or "Time off",
             "type": "time_off", "policy_id": policy_id,
             "hours": share if units == "hours" else None,
             "days": share if units == "days" else None,
             "sched_start": None, "sched_end": None} for day in days]


# ---------------------------------------------------------------- scheduled shifts

async def shifts(start, end) -> list[dict]:
    """Every schedule's shifts between two dates (inclusive), one row per person on the shift,
    as [{user_id, name, date, start, end, job, title, draft, open}]. draft: not published yet;
    open: an open shift (user_id None and name "" when nobody has taken it)."""
    rows = await _shift_rows(*_range(start, end))
    return [{k: v for k, v in r.items() if k != "id"} for r in rows]


async def _shift_rows(start: date, end: date) -> list[dict]:
    return await _kept(("shifts", start, end), MIN_REFRESH, lambda: _fetch_shifts(start, end))


async def _fetch_shifts(start: date, end: date) -> list[dict]:
    names = await _names()
    rows = []
    schedulers = await _schedulers()
    jobs = await _jobs(schedulers)
    for scheduler in schedulers:
        found = await _pages(f"/scheduler/v1/schedulers/{scheduler}/shifts",
                             {"startTime": _midnight(start),
                              "endTime": _midnight(end + timedelta(days=1)) - 1}, "shifts", 500)
        for s in found:
            began, ended = _local(s.get("startTime")), _local(s.get("endTime"))
            if not began or not start <= began.date() <= end:
                continue
            base = {"id": _short(s.get("id"), 80), "date": began.date().isoformat(),
                    "start": _text(began), "end": _text(ended),
                    "job": _job(s, jobs),
                    "title": _short(s.get("title"), 120),
                    "draft": s.get("isPublished") is False, "open": bool(s.get("isOpenShift"))}
            people = [_uid(u) for u in s.get("assignedUserIds") or [] if _uid(u)]
            for uid in people or [None]:
                rows.append({"user_id": uid, "name": names.get(uid, f"User {uid}") if uid else "",
                             **base})
    rows.sort(key=lambda r: (r["start"] or "", r["name"].lower()))
    return rows


# ---------------------------------------------------------------- forms

async def forms() -> list[dict]:
    """Connecteam's forms as [{id, name, questions: [{id, title, type}]}], sorted by name."""
    async def fetch():
        found = []
        for f in await _pages("/forms/v1/forms", {}, "forms", 300):
            if f.get("formId") is None:
                continue
            questions = []
            _questions(f.get("questions"), questions)
            found.append({"id": str(f["formId"])[:40], "name": _short(f.get("formName"), 120)
                          or f"Form {f['formId']}", "questions": questions})
        found.sort(key=lambda f: f["name"].lower())
        return found
    return await _kept(("forms",), LISTS_TTL, fetch)


async def form_submissions(form_id, start, end) -> list[dict]:
    """A form's submissions between two dates (inclusive), as [{id, user_id, name, submitted_at,
    answers: [{question, answer}]}], oldest first. start may also be a datetime: only
    submissions from that moment on. Answers are plain text (files and photos as a count)."""
    form_id = str(form_id).strip()
    if not form_id.isdigit() or len(form_id) > 20:
        raise ValueError("That isn't a Connecteam form.")
    since = start if isinstance(start, datetime) else None
    start, end = _range(start, end)
    if since and since.tzinfo is None:
        since = since.replace(tzinfo=_zone())
    begin = int(since.timestamp()) if since else _midnight(start)
    finish = min(_midnight(end + timedelta(days=1)) - 1, int(time.time()) + 60)
    return await _kept(("submissions", form_id, begin, end), MIN_REFRESH,
                       lambda: _fetch_submissions(form_id, begin, finish))


async def _fetch_submissions(form_id: str, begin: int, finish: int) -> list[dict]:
    titles = {}
    for f in await forms():
        if f["id"] == form_id:
            titles = {q["id"]: q["title"] for q in f["questions"]}
    names = await _names()
    found = await _pages(f"/forms/v1/forms/{form_id}/form-submissions",
                         {"submittingStartTimestamp": begin, "submittingEndTime": finish},
                         "formSubmissions", 100)
    rows = []
    for s in found:
        if s.get("formSubmissionId") is None:
            continue
        uid = None if s.get("isAnonymous") else _uid(s.get("submittingUserId"))
        answers = []
        _answers(s.get("answers"), titles, answers)
        rows.append({"id": str(s["formSubmissionId"])[:80], "user_id": uid,
                     "name": names.get(uid, f"User {uid}") if uid else "Anonymous",
                     "submitted_at": _text(_local(s.get("submissionTimestamp"))),
                     "answers": answers[:MAX_ANSWERS]})
    rows.sort(key=lambda r: r["submitted_at"] or "")
    return rows


def _questions(raw, into: list, depth: int = 0) -> None:
    """A form's questions, those inside groups too (their answers come inside the group's)."""
    for q in raw if isinstance(raw, list) and depth < 5 else []:
        if not isinstance(q, dict) or len(into) >= MAX_ANSWERS:
            continue
        if q.get("questionId") is not None:
            into.append({"id": _short(q.get("questionId"), 80), "title": _short(q.get("title"), 200),
                         "type": _short(q.get("questionType"), 40)})
        _questions(q.get("questions"), into, depth + 1)


def _answers(raw, titles: dict, into: list) -> None:
    for a in raw if isinstance(raw, list) else []:
        if not isinstance(a, dict) or len(into) >= MAX_ANSWERS:
            continue
        kind = a.get("questionType")
        if kind == "group":
            _answers(a.get("answers"), titles, into)
            continue
        if kind == "multiGroup":
            for item in a.get("groupAnswers") or []:
                if isinstance(item, dict):
                    _answers(item.get("answers"), titles, into)
            continue
        if kind == "description" or a.get("wasHidden"):
            continue
        text = "" if a.get("wasSubmittedEmpty") else _answer_text(kind, a)
        question = titles.get(_short(a.get("questionId"), 80)) or "Question"
        into.append({"question": question, "answer": text[:MAX_ANSWER]})


def _answer_text(kind, a: dict) -> str:
    """One answer as plain text."""
    if kind in ("openEnded", "emailAddress", "phoneNumber", "slider"):
        return _short(a.get("value"), MAX_ANSWER)
    if kind == "number":
        return _short(a.get("inputValue"))
    if kind == "rating":
        return _short(a.get("ratingValue"))
    if kind == "multipleChoice":
        return ", ".join(_short(o.get("text")) for o in a.get("selectedAnswers") or []
                         if isinstance(o, dict))
    if kind == "yesNo":
        index = a.get("selectedIndex")
        return {0: "Yes", 1: "No"}.get(index, "") if not isinstance(index, bool) else ""
    if kind == "task":
        return "Done" if a.get("isChecked") else "Not done"
    if kind == "datetime":
        dt = _local(a.get("timestamp"))
        if not dt:
            return ""
        if a.get("isTimeSubmitted") is False:
            return dt.date().isoformat()
        return _text(dt).replace("T", " ") if a.get("isDateSubmitted") is not False else dt.strftime("%H:%M")
    if kind == "location":
        place = a.get("locationInput")
        return _short(place.get("address")) if isinstance(place, dict) else ""
    if kind == "formula":
        return _short(a.get("result"))
    for key, word in (("files", "file"), ("images", "photo"), ("videos", "video")):
        if isinstance(a.get(key), list):
            n = len(a[key])
            return f"{n} {word}{'s' if n != 1 else ''}" if n else ""
    if kind == "audioRecording":
        return "Audio recording" if a.get("audioUrl") else ""
    return _short(a.get("value"), MAX_ANSWER)
