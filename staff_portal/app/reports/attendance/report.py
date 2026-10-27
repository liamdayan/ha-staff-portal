"""Attendance & Leave Weekly: late arrivals, early clock-outs, missed lunches, no-shows, required
forms and leave balances, from Connecteam (through ctx.ct) and the newest Gusto upload.

The checks themselves run in view.js, ported from the original report page so the two are easy
to compare. This file only fetches and tidies: every time it hands over is local wall-clock
minutes since 1970-01-01 in the property's time zone ("abs minutes", as the original used), and
every day is a day number since then. Imports nothing but datetime, as reports must."""

import datetime as dt

EPOCH = dt.date(1970, 1, 1)
DAY = dt.timedelta(days=1)

# The original page's defaults. Managers change them on the Rules tab (report settings).
DEFAULT_RULES = dict(weekStart=0, lateGrace=5, earlyGrace=5, lunchAfter=6, lunchMin=30,
                     defaultShift=8, lowSick=8, lowPto=2, actionAt=4, aliases="", earlySkip=[])
FREQUENCIES = ("shift", "week", "hour")
PERIODS = ("week", "pay", "month")
MAX_FORMS = 20
NAME_LEN = 120


# ---------------------------------------------------------------- settings

def _number(value, default, top=1000):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    v = min(max(v, 0), top)
    return int(v) if v == int(v) else v


def _text(value, limit):
    return str(value or "")[:limit] if isinstance(value, (str, int, float)) else ""


def clean_rules(raw) -> dict:
    """Stored rules with the defaults filled in and every value in range. Whatever is stored
    (an old version, a hand-edited backup) can't break the report."""
    raw = raw if isinstance(raw, dict) else {}
    rules = dict(DEFAULT_RULES)
    for key, default in DEFAULT_RULES.items():
        if key in ("aliases", "earlySkip") or key not in raw:
            continue
        rules[key] = _number(raw[key], default, 6 if key == "weekStart" else 1000)
    rules["weekStart"] = int(rules["weekStart"])
    rules["aliases"] = _text(raw.get("aliases"), 20000)
    skip = raw.get("earlySkip")
    rules["earlySkip"] = [_text(s, NAME_LEN) for s in skip if isinstance(s, str) and s.strip()][:200] \
        if isinstance(skip, list) else []
    return rules


def _interval(value, default):
    # Blank means the default; 0 switches checks off for that part of the day (as the original).
    if value in ("", None):
        return default
    return _number(value, default, 24)


def clean_forms(raw) -> list[dict]:
    """The required forms: which Connecteam form, how often it's due, and for whom."""
    found, seen = [], set()
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict):
            continue
        form_id = _text(f.get("id"), 80).strip()
        if not form_id or form_id in seen:
            continue
        seen.add(form_id)
        every = _interval(f.get("every"), 1)
        found.append(dict(id=form_id, name=_text(f.get("name"), NAME_LEN).strip() or "Untitled form",
                          frequency=f.get("frequency") if f.get("frequency") in FREQUENCIES else "shift",
                          applies_to=_text(f.get("applies_to"), 2000),
                          every=every, night_every=_interval(f.get("night_every"), every)))
    return found[:MAX_FORMS]


def settings_of(ctx) -> dict:
    stored = ctx.settings if isinstance(getattr(ctx, "settings", None), dict) else {}
    return dict(rules=clean_rules(stored.get("rules")), forms=clean_forms(stored.get("forms")))


# ---------------------------------------------------------------- periods and times

def day_number(day: dt.date) -> int:
    return (day - EPOCH).days


def period_of(at: dt.date, kind: str, week_start: int) -> tuple[dt.date, dt.date]:
    """The week, pay period (1st to 15th, 16th to month end) or month holding a date."""
    if kind == "week":
        start = at - dt.timedelta(days=((at.weekday() + 1) % 7 - week_start) % 7)
        return start, start + dt.timedelta(days=6)
    first = at.replace(day=1)
    last = (first + dt.timedelta(days=32)).replace(day=1) - DAY
    if kind == "pay":
        return (first, first.replace(day=15)) if at.day <= 15 else (first.replace(day=16), last)
    return first, last


def _date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _clock(text: str):
    """'8:05', '08:05:00', '8:05 PM', '8pm' -> minutes after midnight, or None."""
    t = text.strip().lower().replace(".", "").replace(" ", "")
    ap = None
    for end in ("am", "pm", "a", "p"):
        if t.endswith(end):
            ap, t = end[0], t[:-len(end)]
            break
    parts = t.split(":")
    if not parts[0].isdigit() or len(parts) > 3 or not all(p.isdigit() for p in parts[1:]):
        return None
    h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    if ap:
        h = h % 12 + (12 if ap == "p" else 0)
    return h * 60 + m if h < 24 and m < 60 else None


def abs_minutes(value, day, tz):
    """A clock time (datetime, ISO text, epoch seconds, or a bare time on `day`) as local
    wall-clock minutes since 1970, or None. Times with an offset are moved into the property's
    time zone when ctx.tz is a tzinfo; otherwise their own wall clock is used."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 1e11 else value
        when = dt.datetime.fromtimestamp(secs, tz) if tz else dt.datetime.fromtimestamp(secs)
    elif isinstance(value, dt.datetime):
        when = value.astimezone(tz) if tz and value.tzinfo else value
    elif isinstance(value, dt.time):
        return None if day is None else day_number(day) * 1440 + value.hour * 60 + value.minute
    else:
        text = str(value).strip()
        try:
            when = dt.datetime.fromisoformat(text)
        except ValueError:
            minutes = _clock(text)
            return None if minutes is None or day is None else day_number(day) * 1440 + minutes
        if tz and when.tzinfo:
            when = when.astimezone(tz)
    return day_number(when.date()) * 1440 + when.hour * 60 + when.minute


def _hours(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


# ---------------------------------------------------------------- Connecteam rows

def _uid(row, keys=("user_id", "userId")):
    # Only the users list may use "id": on a shift or a submission that's the row's own ID.
    v = next((row[k] for k in keys if row.get(k) not in (None, "")), None)
    return None if v is None else str(v)[:40]


def _user_id(row):
    return _uid(row, ("user_id", "userId", "id"))


def _name(row):
    name = row.get("name")
    if not name:
        name = " ".join(str(row.get(k) or "").strip() for k in ("firstName", "lastName",
                                                                  "first_name", "last_name")).strip()
    return _text(name, NAME_LEN).strip()


def _day_of(row, start):
    """The row's own date, else the day its start falls on (in the property's time zone)."""
    day = _date(row.get("date")) if row.get("date") not in (None, "") else None
    return day or (None if start is None else EPOCH + dt.timedelta(days=start // 1440))


def timesheet_rows(rows, tz) -> list[dict]:
    """Time clock entries: punches (work and breaks), and time off with its hours."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        given = _date(r.get("date")) if r.get("date") not in (None, "") else None
        start = abs_minutes(r.get("clock_in"), given, tz)
        day = _day_of(r, start)
        if day is None:
            continue
        end = abs_minutes(r.get("clock_out"), day, tz)
        if start is not None and end is not None and end <= start:
            end += 1440  # out past midnight, given as a bare time
        sched_start = abs_minutes(r.get("sched_start"), day, tz)
        sched_end = abs_minutes(r.get("sched_end"), day, tz)
        out.append(dict(uid=_uid(r), name=_name(r), day=day_number(day), **{"in": start}, out=end,
                        brk=_number(r.get("break_minutes"), 0, 1440),
                        job=_text(r.get("job"), NAME_LEN), type=_text(r.get("type"), NAME_LEN),
                        hours=_hours(r.get("hours")), ss=sched_start, se=sched_end))
    return out


def shift_rows(rows, tz) -> list[dict]:
    """Scheduled shifts, drafts and open shifts included (flagged): the view skips them for
    people, as the original did, but an open shift still says a job is scheduled."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        given = _date(r.get("date")) if r.get("date") not in (None, "") else None
        start = abs_minutes(r.get("start"), given, tz)
        day = _day_of(r, start)
        if day is None:
            continue
        end = abs_minutes(r.get("end"), day, tz)
        out.append(dict(uid=_uid(r), name=_name(r), day=day_number(day), start=start, end=end,
                        job=_text(r.get("job"), NAME_LEN), title=_text(r.get("title"), NAME_LEN),
                        draft=bool(r.get("draft")), open=bool(r.get("open"))))
    return out


def submission_rows(rows, tz) -> list[dict]:
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        at = abs_minutes(r.get("submitted_at"), None, tz)
        if at is not None:
            out.append(dict(uid=_uid(r), name=_name(r), at=at))
    return out


def user_names(users) -> dict:
    return {u: n for u, n in ((_user_id(r), _name(r)) for r in users or [] if isinstance(r, dict)) if u and n}


def department_map(departments, users) -> dict:
    """{Connecteam user ID: Staff Access department}. Departments follow Connecteam smart
    groups; a department lists its people's user IDs (connecteam_user_ids, user_ids, or
    members with a connecteam_user_id), or a user row names its department. First one wins,
    as for departments themselves."""
    found = {}
    for d in departments or []:
        if not isinstance(d, dict) or not d.get("name"):
            continue
        ids = d.get("connecteam_user_ids") or d.get("user_ids") or d.get("members") or []
        for m in ids if isinstance(ids, list) else []:
            uid = _uid(m, ("connecteam_user_id", "user_id", "id")) if isinstance(m, dict) else (
                str(m) if m not in (None, "") else None)
            if uid:
                found.setdefault(uid, _text(d["name"], NAME_LEN))
    for r in users or []:
        if isinstance(r, dict) and _user_id(r) and isinstance(r.get("department"), str) and r["department"]:
            found.setdefault(_user_id(r), _text(r["department"], NAME_LEN))
    return found


# ---------------------------------------------------------------- the report

async def _maybe(value):
    return await value if hasattr(value, "__await__") else value


async def _fetch(ctx, errors, what, call, *args):
    """One Connecteam read. A refusal (no permission, paused by the budget, Connecteam down)
    becomes a plain message and the rest of the report still shows."""
    unavailable = getattr(ctx.ct, "Unavailable", None) or Exception
    try:
        return await _maybe(call(*args)), True
    except unavailable as e:
        errors.append(f"{what}: {e}")
        return [], False


async def data(ctx, params) -> dict:
    params = params or {}
    settings = settings_of(ctx)
    rules = settings["rules"]
    tz = ctx.tz if isinstance(getattr(ctx, "tz", None), dt.tzinfo) else None
    today = ctx.today
    kind = params.get("period") if params.get("period") in PERIODS else "week"
    at = _date(params.get("at")) or today
    # Two years back at most, and not past next month: each new range is a Connecteam read.
    at = min(max(at, today - dt.timedelta(days=730)), today + dt.timedelta(days=31))
    start, end = period_of(at, kind, rules["weekStart"])
    # A day either side: overnight shifts, and forms handed in the morning after a shift.
    lo, hi = start - DAY, end + DAY
    errors = []
    ct = ctx.ct
    ts, have_ts = await _fetch(ctx, errors, "Timesheets", ct.timesheets, lo, hi)
    shifts, have_shifts = await _fetch(ctx, errors, "Schedules", ct.shifts, lo, hi)
    users, _ = await _fetch(ctx, [], "Users", ct.users)
    forms = []
    for f in settings["forms"]:
        subs, ok = await _fetch(ctx, errors, f["name"], ct.form_submissions, f["id"], lo, hi)
        forms.append(dict(f, subs=submission_rows(subs, tz), loaded=ok))
    can_manage = bool(getattr(ctx, "can_manage", False) or getattr(ctx, "manage", False))
    choices = []
    if can_manage and params.get("form_list"):
        found, _ = await _fetch(ctx, errors, "Forms", ct.forms)
        choices = [dict(id=_text(f.get("id"), 80), name=_text(f.get("name"), NAME_LEN))
                   for f in found if isinstance(f, dict) and f.get("id")]
    gusto = None
    uploads = [u for u in (await _maybe(ctx.uploads("gusto")) or []) if isinstance(u, dict)]
    if uploads:
        try:
            uploads.sort(key=lambda u: u.get("uploaded_at") or 0, reverse=True)
        except TypeError:
            pass  # mixed kinds of date: keep the order given (newest first)
        u = uploads[0]
        gusto = dict(name=_text(u.get("name"), 200), text=str(u.get("text") or ""),
                     uploaded_at=u.get("uploaded_at"), by=_text(u.get("by"), NAME_LEN))
    now = getattr(ctx, "now", None) or (dt.datetime.now(tz) if tz else dt.datetime.now())
    return dict(
        period=kind, start=start.isoformat(), end=end.isoformat(),
        start_day=day_number(start), end_day=day_number(end),
        fetch_start=day_number(lo), fetch_end=day_number(hi),
        today=day_number(today), now=abs_minutes(now, None, tz),
        timesheets=timesheet_rows(ts, tz), shifts=shift_rows(shifts, tz), forms=forms,
        have=dict(timesheets=have_ts, shifts=have_shifts),
        users=user_names(users), departments=department_map(getattr(ctx, "departments", None), users),
        gusto=gusto, settings=settings, can_manage=can_manage, form_choices=choices, errors=errors)


REPORT = dict(
    key="attendance",
    name="Attendance & Leave",
    description="Late arrivals, early clock-outs, missed lunches and shifts, required forms, "
                "and leave balances from Gusto.",
    data=data,
    params=dict(period=list(PERIODS), at="date (YYYY-MM-DD) in the period", form_list="1 = also "
                "list Connecteam forms (managers, for the Rules tab)"),
    uploads=[dict(key="gusto", name="Gusto time off balances",
                  help="In Gusto: Reports › Time Off Balances, downloaded as CSV. "
                       "The newest upload is used.",
                  accept=".csv,text/csv")],
    settings=[dict(key="rules", name="Rules", default=DEFAULT_RULES),
              dict(key="forms", name="Required forms", default=[])],
)
