"""Attendance & Leave Weekly: late arrivals, early clock-outs, missed lunches, no-shows, required
forms and leave balances, from Connecteam (through ctx.ct) and the newest Gusto upload.

The checks themselves run in view.js, ported from the original report page so the two are easy
to compare. This file only fetches and tidies: every time it hands over is local wall-clock
minutes since 1970-01-01 in the property's time zone ("abs minutes", as the original used), and
every day is a day number since then.

Who can see whom (a setting): everyone who may view the report sees every department (the
default), or viewers see only the people in their own departments while those who manage the
report see everyone. The filtering happens here, so nothing about other people is sent."""

import datetime as dt
import json
import re
import unicodedata

EPOCH = dt.date(1970, 1, 1)
DAY = dt.timedelta(days=1)

# The original page's defaults. Managers change them on the Rules tab (report settings).
DEFAULT_RULES = dict(weekStart=0, lateGrace=5, earlyGrace=5, lunchAfter=6, lunchMin=30,
                     defaultShift=8, lowSick=8, lowPto=2, actionAt=4, aliases="", earlySkip=[])
FREQUENCIES = ("shift", "week", "hour")
PERIODS = ("week", "pay", "month")
WHO_SEES = ("all", "own")    # everyone sees all departments, or viewers only their own
MAX_FORMS = 20
NAME_LEN = 120
ALIASES_LEN = 12000          # characters of name matches
APPLIES_LEN = 1000           # characters of a form's "who must submit"
MAX_SKIP = 50                # departments and jobs not checked for early clock-outs
SETTINGS_ROOM = 46 * 1024    # saved settings as JSON (the add-on takes at most 48 KB)
GUSTO_MAX = 256 * 1024       # bytes of a Gusto upload


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
    rules["aliases"] = _text(raw.get("aliases"), ALIASES_LEN)
    skip = raw.get("earlySkip")
    rules["earlySkip"] = [_text(s, NAME_LEN) for s in skip if isinstance(s, str) and s.strip()][:MAX_SKIP] \
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
                          applies_to=_text(f.get("applies_to"), APPLIES_LEN),
                          every=every, night_every=_interval(f.get("night_every"), every)))
    return found[:MAX_FORMS]


def clean_settings(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return dict(rules=clean_rules(raw.get("rules")), forms=clean_forms(raw.get("forms")),
                who_sees=raw.get("who_sees") if raw.get("who_sees") in WHO_SEES else "all")


def check_settings(raw) -> dict:
    """What a manager saves, cleaned as it's read, and small enough to keep."""
    found = clean_settings(raw)
    if len(json.dumps(found)) > SETTINGS_ROOM:
        raise ValueError("The rules are too long to save. Shorten the name matches or who must "
                         "submit each form.")
    return found


def settings_of(ctx) -> dict:
    return clean_settings(ctx.settings)


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


def _bare(value) -> bool:
    """A clock time with no date of its own ("06:30", "6:30 AM")."""
    if isinstance(value, dt.time):
        return True
    if not isinstance(value, str):
        return False
    try:
        dt.datetime.fromisoformat(value.strip())
        return False
    except ValueError:
        return True


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


def timesheet_rows(rows, tz, day_hours=8) -> list[dict]:
    """Time clock entries: punches (work and breaks), and time off with its hours (a policy
    Connecteam counts in days is day_hours a day: the rules' shift length)."""
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
        if start is not None and end is not None and end < start and _bare(r.get("clock_out")):
            end += 1440  # out past midnight, given as a bare time (ctdata always gives the date)
        sched_start = abs_minutes(r.get("sched_start"), day, tz)
        sched_end = abs_minutes(r.get("sched_end"), day, tz)
        kind, job = _text(r.get("type"), NAME_LEN), _text(r.get("job"), NAME_LEN)
        if kind == "time_off":   # ctdata's kind; the view reads "Time off - <policy>"
            kind = f"Time off - {job}" if job and job.lower() != "time off" else "Time off"
        hours = _hours(r.get("hours"))
        if hours is None and _hours(r.get("days")) is not None:
            hours = round(_hours(r.get("days")) * day_hours, 2)
        out.append(dict(uid=_uid(r), name=_name(r), day=day_number(day), **{"in": start}, out=end,
                        brk=_number(r.get("break_minutes"), 0, 1440), job=job, type=kind,
                        hours=hours, ss=sched_start, se=sched_end))
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


def department_map(departments, users, staff=()) -> dict:
    """{Connecteam user ID: Staff Access department {id, name}}. First the department someone
    linked to Connecteam has here (staff: [{connecteam_user_id, department_id}], set by hand or
    by sync); else by smart group: a department follows one, and users() lists each person's
    groups, someone in several getting the first department, as Staff Access does."""
    named = {d.get("id"): d for d in departments or [] if isinstance(d, dict) and d.get("name")}
    mapped = [d for d in named.values() if d.get("smart_group_id")]
    found = {}
    for s in staff or []:
        d = named.get(s.get("department_id")) if isinstance(s, dict) else None
        if d and s.get("connecteam_user_id") not in (None, ""):
            found[str(s["connecteam_user_id"])[:40]] = dict(id=d.get("id"), name=_text(d["name"], NAME_LEN))
    for r in users or []:
        uid = _user_id(r) if isinstance(r, dict) else None
        groups = {str(g) for g in r.get("groups") or []} if uid else set()
        d = next((d for d in mapped if str(d["smart_group_id"]) in groups), None)
        if d and uid not in found:
            found[uid] = dict(id=d.get("id"), name=_text(d["name"], NAME_LEN))
    return found


# ---------------------------------------------------------------- who can see whom

def parse_csv(text) -> list[list[str]]:
    """Rows of a CSV file, the way view.js reads Gusto's (its delimiter guessed the same way)."""
    text = str(text or "").lstrip("\ufeff")
    head = "\n".join(re.split(r"\r?\n", text)[:8])
    delim = max((",", ";", "\t"), key=lambda c: (head.count(c), -(",;\t".index(c))))
    rows, row, field, quoted, i = [], [], "", False, 0
    while i < len(text):
        c = text[i]
        if quoted:
            if c == '"':
                if text[i + 1:i + 2] == '"':
                    field += '"'
                    i += 1
                else:
                    quoted = False
            else:
                field += c
        elif c == '"' and field == "":
            quoted = True
        elif c == delim:
            row.append(field)
            field = ""
        elif c in "\r\n":
            if c == "\r" and text[i + 1:i + 2] == "\n":
                i += 1
            row.append(field)
            rows.append(row)
            row, field = [], ""
        else:
            field += c
        i += 1
    if field or row:
        row.append(field)
        rows.append(row)
    return [r for r in rows if any(v.strip() for v in r)]


def _csv_text(rows) -> str:
    def cell(v):
        return '"' + v.replace('"', '""') + '"' if re.search(r'[",;\t\r\n]', v) else v
    return "\n".join(",".join(cell(v) for v in r) for r in rows) + "\n"


def name_key(name) -> str:
    """A name as view.js's normKey has it: "Patel, Dev" and "Dev Q. Patel" are "dev patel"."""
    n = str(name or "").strip()
    if "," in n:
        last, *rest = n.split(",")
        n = f"{' '.join(rest)} {last}"
    n = "".join(ch for ch in unicodedata.normalize("NFD", n) if not unicodedata.combining(ch)).lower()
    words = [w for w in re.sub(r"[^a-z'\- ]+", " ", n).split() if len(re.sub(r"['\-]", "", w)) > 1]
    return f"{words[0]} {words[-1]}" if len(words) > 1 else (words[0] if words else "")


# Common short names, kept the same as view.js's NICKNAMES: each line is one name and the
# names it goes by. Tom matches Thomas through this, which the spelling rules below can't see.
NICKNAMES = [
    "abigail abby", "albert al bert", "alexander alex xander", "alexandra alex alexa sandra sasha",
    "alfred al alfie fred", "andrew andy drew", "anthony tony", "antonia toni tonia",
    "barbara barb barbie", "benjamin ben benji", "catherine cathy kate katie cat",
    "charles charlie chuck chaz", "christina chris tina", "christine chris tina",
    "christopher chris kit", "daniel dan danny", "david dave davey", "deborah deb debbie",
    "donald don donnie", "dorothy dot dottie", "douglas doug", "edward ed eddie ted ned",
    "elizabeth liz lizzie beth betty eliza libby", "eugene gene", "frances fran frannie",
    "francis frank frankie", "frederick fred freddie", "gerald gerry jerry", "gregory greg",
    "harold harry hal", "henry hank harry", "isabel izzy bella", "isabella izzy bella",
    "jacob jake", "james jim jimmy jamie", "jennifer jen jenny", "jeffrey jeff",
    "jessica jess jessie", "john jack johnny", "jonathan jon jonny", "joseph joe joey",
    "joshua josh", "katherine kate katie kathy kat", "kathryn kate katie kathy kat",
    "kenneth ken kenny", "lawrence larry", "leonard leo len lenny", "louis lou",
    "margaret maggie meg peggy", "matthew matt", "michael mike mikey", "nathan nate",
    "nathaniel nate nathan", "nicholas nick nicky", "patricia pat patty trish",
    "patrick pat paddy", "peter pete", "philip phil", "raymond ray", "rebecca becky becca",
    "richard rick ricky dick rich", "robert rob robbie bob bobby bert", "ronald ron ronnie",
    "samantha sam sammy", "samuel sam sammy", "stephen steve", "steven steve",
    "susan sue susie", "theodore ted teddy theo", "thomas tom tommy", "timothy tim timmy",
    "victoria vicky tori", "vincent vince", "walter walt", "william will bill billy liam",
    "zachary zach zack", "alejandro alex", "eduardo lalo eddie", "francisco paco pancho frank",
    "guillermo memo will", "ignacio nacho", "jesus chuy", "jose pepe", "manuel manny",
    "roberto beto rob"]
_NICK = {}
for _line in NICKNAMES:
    _names = _line.split()
    for _n in _names:
        _NICK.setdefault(_n, set()).update(_names)


def _alike(a, b) -> bool:
    """view.js's firstNamesAlike: Nate and Nathan, Toni and Tonia, Liam and William, and the
    common short names (Tom and Thomas)."""
    if a == b or b in _NICK.get(a, ()):
        return True
    if len(a) < 3 or len(b) < 3:
        return False
    return a.startswith(b) or b.startswith(a) or a.endswith(b) or b.endswith(a) or a[:3] == b[:3]


def aliases_of(text) -> dict:
    """The rules' name matches: {Gusto name key: Connecteam name key}."""
    found = {}
    for line in str(text or "").split("\n"):
        parts = [p.strip() for p in re.split(r"=|→|->", line)]
        if len(parts) > 1 and parts[0] and parts[1]:
            found[name_key(parts[0])] = name_key(parts[1])
    return found


def match_names(keys, everyone: set, alias: dict) -> dict:
    """Gusto names (keys) to Connecteam people (keys), as view.js matches them: by name or a name
    match from the rules, else by a short name (same last name, first names alike) only when
    it's the one person it could be, that person has no row of their own, and no other Gusto
    name points at them too."""
    found, guesses = {}, {}
    for key in keys:
        key2 = alias.get(key, key)
        if key2 in everyone:
            found[key] = key2
            continue
        first, _, last = key2.partition(" ")
        alike = [k for k in everyone if last and k.split(" ")[-1] == last and _alike(first, k.split(" ")[0])]
        if len(alike) == 1:
            guesses[key] = alike[0]
    taken = set(found.values())
    counts = {}
    for k in guesses.values():
        counts[k] = counts.get(k, 0) + 1
    found.update({key: k for key, k in guesses.items() if k not in taken and counts[k] == 1})
    return found


GUSTO_NAME_HEAD = re.compile(
    r"^(employee|employee name|employee full name|name|full name|user|users|user name|team member|"
    r"staff( member)?|worker|first|first name|given name|legal first name|preferred first name)$", re.I)
# view.js's name columns (NAME_FIELDS): a strong heading scores 3, a weak one 1.
NAME_COLUMNS = dict(
    employee=(re.compile(r"^(employee|employee name|employee full name|name|full name|user|users|user name|"
                         r"team member|staff( member)?|worker|submitted by|submitter|assigned to|assignee)$", re.I),
              re.compile(r"employee|user|staff|submitted by|assigned|name", re.I),
              re.compile(r"first|last|job|form|company|manager|approved|location|position|title|policy|"
                         r"\bid\b|number|#|email|phone|type|note|comment", re.I)),
    first=(re.compile(r"^(first|first name|given name|legal first name|preferred first name)$", re.I),
           re.compile(r"first ?name", re.I), None),
    last=(re.compile(r"^(last|last name|surname|family name|legal last name)$", re.I),
          re.compile(r"last ?name|surname", re.I), None))


def _name_column(headers, kind):
    strong, weak, bad = NAME_COLUMNS[kind]
    best, score = None, 0
    for i, cell in enumerate(headers):
        cell = cell.strip()
        if not cell or (bad and bad.search(cell)):
            continue
        s = 3 if strong.match(cell) else 1 if weak.search(cell) else 0
        if s > score:
            best, score = i, s
    return best


def gusto_for(text, allowed: set, everyone: set, aliases: str) -> str:
    """Gusto's file with only the rows of people this viewer may see: the lines above the column
    headers (the report's title and the company), the headers, and each row whose name (in the
    name column, or first and last name columns) is an allowed person's, matched as view.js
    matches it (match_names). allowed and everyone are name keys. Nothing if the headers can't
    be found."""
    rows = parse_csv(text)
    head = next((i for i, r in enumerate(rows[:25]) if any(GUSTO_NAME_HEAD.match(v.strip()) for v in r)),
                None)
    if head is None:
        return ""
    who = _name_column(rows[head], "employee")
    first, last = _name_column(rows[head], "first"), _name_column(rows[head], "last")
    if who is None and first is None:
        return ""

    def cell(r, i):
        return r[i].strip() if i is not None and i < len(r) else ""

    def name(r):
        return cell(r, who) or " ".join(filter(None, (cell(r, first), cell(r, last))))
    body = [(r, name_key(name(r))) for r in rows[head + 1:]]
    matched = match_names({k for _, k in body if k}, everyone, aliases_of(aliases))
    kept = rows[:head + 1] + [r for r, k in body if k and matched.get(k) in allowed]
    return _csv_text(kept)


def only_own(ctx, d: dict, users, dept_of: dict) -> dict:
    """The report's data with only the people in the viewer's own departments."""
    own = {x.get("id") for x in (ctx.person or {}).get("departments") or [] if isinstance(x, dict)}
    allowed = {uid for uid, dept in dept_of.items() if dept["id"] in own}
    keys = {uid: name_key(n) for uid, n in user_names(users).items()}
    d["timesheets"] = [t for t in d["timesheets"] if t["uid"] in allowed]
    d["shifts"] = [s for s in d["shifts"] if s["uid"] in allowed or (s["uid"] is None and s["open"])]
    for f in d["forms"]:
        f["subs"] = [s for s in f["subs"] if s["uid"] in allowed or s["uid"] is None]
        for s in f["subs"]:
            if s["uid"] is None:
                s["name"] = "Anonymous"
    d["users"] = {uid: n for uid, n in d["users"].items() if uid in allowed}
    d["departments"] = {uid: n for uid, n in d["departments"].items() if uid in allowed}
    mine = {keys[u] for u in allowed if u in keys}
    aliases = d["settings"]["rules"]["aliases"]
    if d["gusto"]:
        text = gusto_for(d["gusto"]["text"], mine, set(keys.values()), aliases)
        d["gusto"] = dict(d["gusto"], text=text) if text else None
    # Only the name matches for people this viewer sees, and no one else named as having to
    # submit a form (departments and jobs stay).
    kept = [line for line in aliases.split("\n")
            if len(parts := re.split(r"=|→|->", line)) > 1 and name_key(parts[1]) in mine]
    others = set(keys.values()) - mine
    forms = [dict(f, applies_to=", ".join(x.strip() for x in re.split(r"[,;\n]", f["applies_to"])
                                         if x.strip() and name_key(x) not in others))
             for f in d["settings"]["forms"]]
    d["settings"] = dict(d["settings"], forms=forms,
                         rules=dict(d["settings"]["rules"], aliases="\n".join(kept)))
    for f in d["forms"]:
        f["applies_to"] = next((x["applies_to"] for x in forms if x["id"] == f["id"]), "")
    d["own_only"] = True
    return d


# ---------------------------------------------------------------- the report

async def _fetch(ctx, errors, what, call, *args):
    """One Connecteam read. A refusal (no permission, paused by the budget, Connecteam down)
    becomes a plain message and the rest of the report still shows."""
    try:
        return await call(*args), True
    except ctx.ct.Unavailable as e:
        errors.append(f"{what}: {e}")
        return [], False


async def data(ctx, params) -> dict:
    params = params or {}
    settings = settings_of(ctx)
    rules = settings["rules"]
    tz = ctx.tz
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
    own_only = settings["who_sees"] == "own" and not ctx.manage
    ts, have_ts = await _fetch(ctx, errors, "Timesheets", ct.timesheets, lo, hi)
    shifts, have_shifts = await _fetch(ctx, errors, "Schedules", ct.shifts, lo, hi)
    # Without the user list, people show under their Gusto title; when viewers see only their
    # own departments, nobody can be shown.
    users, have_users = await _fetch(ctx, errors if own_only else [], "People", ct.users)
    forms = []
    for f in settings["forms"]:
        subs, ok = await _fetch(ctx, errors, f["name"], ct.form_submissions, f["id"], lo, hi)
        forms.append(dict(f, subs=submission_rows(subs, tz), loaded=ok))
    choices = []
    if ctx.manage and params.get("form_list"):
        found, _ = await _fetch(ctx, errors, "Forms", ct.forms)
        choices = [dict(id=_text(f.get("id"), 80), name=_text(f.get("name"), NAME_LEN))
                   for f in found if isinstance(f, dict) and f.get("id")]
    gusto = None
    uploads = [u for u in ctx.uploads("gusto") or [] if isinstance(u, dict)]
    if uploads:
        u = max(uploads, key=lambda u: u.get("uploaded_at") or 0)
        gusto = dict(name=_text(u.get("name"), 200), text=str(u.get("text") or ""),
                     uploaded_at=u.get("uploaded_at"), by=_text(u.get("by"), NAME_LEN))
    dept_of = department_map(ctx.departments, users, getattr(ctx, "staff", ()))
    now = getattr(ctx, "now", None) or dt.datetime.now(tz)
    found = dict(
        period=kind, start=start.isoformat(), end=end.isoformat(),
        start_day=day_number(start), end_day=day_number(end),
        fetch_start=day_number(lo), fetch_end=day_number(hi),
        today=day_number(today), now=abs_minutes(now, None, tz),
        timesheets=timesheet_rows(ts, tz, rules["defaultShift"]), shifts=shift_rows(shifts, tz), forms=forms,
        have=dict(timesheets=have_ts, shifts=have_shifts),
        users=user_names(users), departments={uid: d["name"] for uid, d in dept_of.items()},
        gusto=gusto, settings=settings, form_choices=choices, errors=errors, own_only=False)
    return only_own(ctx, found, users if have_users else [], dept_of) if own_only else found


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
                  accept=".csv,text/csv",
                  # Gusto's file is a few KB; a small cap keeps filtering it quick for each viewer.
                  max_bytes=GUSTO_MAX)],
    settings=[dict(key="rules", name="Rules", default=DEFAULT_RULES),
              dict(key="forms", name="Required forms", default=[]),
              dict(key="who_sees", name="Who can see whom", default="all")],
    check_settings=check_settings,
)
