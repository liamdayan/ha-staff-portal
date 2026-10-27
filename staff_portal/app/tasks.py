"""Tasks: a to-do list for each department in the staff app.

A task belongs to a department or to Everyone, may be assigned to one person, and has a status:
To do, In progress, Done or Won't do. Quick tasks are added by people in the staff app; others
come from Connecteam forms chosen on the dashboard (one task per submission), and from
Connecteam's Quick Tasks when the task sync is on (ctsync.py). A form may follow one of its
manager fields: its tasks then show the submission's status (follow_status). This module holds
the rules: who sees and edits what, checking what's typed, and turning a form submission into a
task. db.py stores them and main.py serves them; everything is checked again on the server for
every request.
"""

import json
import re
from datetime import date, datetime, timezone

STATUSES = ("todo", "doing", "done", "wont")
STATUS_NAMES = {"todo": "To do", "doing": "In progress", "done": "Done", "wont": "Won't do"}
FINISHED = ("done", "wont")
PRIORITIES = ("normal", "urgent")
SOURCES = ("quick", "form", "connecteam")
# One tap on a task's status chip moves it on; Won't do and Reopen are in its menu.
NEXT_STATUS = {"todo": "doing", "doing": "done", "done": "todo", "wont": "todo"}
HISTORY_ACTIONS = ("created", "status", "edited", "deleted", "restored")

MAX_TITLE = 120
MAX_NOTES = 4000
MAX_NAME = 60            # a person's or department's name, as kept on a task
MAX_FORM_NAME = 120
MAX_QUESTION = 200
BIN_DAYS = 30            # deleted tasks can be put back for this long
KEEP_FINISHED_DAYS = 365 # finished tasks are kept this long, then dropped
LIST_FINISHED_DAYS = 30  # the staff app lists finished tasks from this far back
FORM_CHECK_SECONDS = 600 # each form chosen on the dashboard is checked this often, at most...
FORM_DAILY_REQUESTS = 300  # ...and all of them together ask about this many times a day (under a
                         # third of the reports lane's 1,000), so the checks are spread out more
                         # as forms are added: up to 2 forms every 10 minutes, 5 every 24
FORM_OVERLAP = 3600      # a check looks back this far before the last good one (a submission
                         # sent from a phone that was offline arrives late)
FORM_LOOKBACK_DAYS = 31  # the most a check reaches back (after the add-on was off a while)
MAX_FORMS = 50
MAX_PHOTOS = 20          # photo links kept on a task
MAX_URL = 2000
MAX_OPTIONS = 30         # a followed manager field's status options
MAX_NOTE = 60            # a history entry's note ("in Connecteam")
CT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")   # a Connecteam task, board or option ID
MAX_TASKS = 50_000       # in a backup (the newest)
MAX_HISTORY = 500_000
FORM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# Control characters are dropped from what people type (new lines and tabs stay in notes).
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class TaskError(ValueError):
    """A request that can't be done, with a message for the person."""


# ---------------------------------------------------------------- who sees and edits

def can_see(task: dict, person: dict, manager: bool = False, live=None) -> bool:
    """People see tasks for any of their departments (person["department_ids"]: the ones not
    hidden), tasks for Everyone, tasks assigned to them, and tasks they made. A task whose
    department no longer exists or is hidden (not in live, the department IDs shown) is seen by
    those two and by anyone who may Manage tasks, to move it on."""
    if task.get("deleted_at"):
        return False
    dept = task["department_id"]
    return (dept is None
            or (dept in (person.get("department_ids") or ()) and (live is None or dept in live))
            or task["assigned_id"] == person["id"] or task["created_by_id"] == person["id"]
            or (manager and live is not None and dept not in live))


def can_edit(task: dict, person: dict, manager: bool, live=None) -> bool:
    """Only the person who made a task, or someone whose role may Manage tasks, edits or deletes
    it (and only one they can see). Anyone who sees it may change its status."""
    return (manager or task["created_by_id"] == person["id"]) and (
        can_see({**task, "deleted_at": None}, person, manager, live))


def can_restore(task: dict, person: dict, manager: bool, live=None) -> bool:
    """A task in the bin: the person who made it, or a manager who could see it if it weren't
    deleted, may put it back (and see it there)."""
    return bool(task.get("deleted_at")) and can_edit(task, person, manager, live)


# ---------------------------------------------------------------- what's typed

def clean_text(value, limit: int, lines: bool = False) -> str:
    text = CONTROL_RE.sub("", str(value or "").replace("\r\n", "\n").replace("\r", "\n"))
    if not lines:
        text = " ".join(text.split())
    else:
        text = "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")
    return text.strip()[:limit] if not lines else text[:limit]


def clean_date(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not DATE_RE.match(text):
        raise TaskError("Pick a real due date.")
    try:
        date.fromisoformat(text)
    except ValueError:
        raise TaskError("Pick a real due date.") from None
    return text


def clean_time(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not TIME_RE.match(text):
        raise TaskError("Pick a real due time.")
    return text


def _id(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise TaskError("That isn't a real choice.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise TaskError("That isn't a real choice.") from None
    if not 0 < number < 2**53:
        raise TaskError("That isn't a real choice.")
    return number


def clean_task(data: dict, departments: dict[int, str], people: dict[int, str],
               old: dict | None = None) -> dict:
    """The fields of a new or edited task, checked. departments: {id: name} that exist;
    people: {id: name} of people who may be assigned (signed-in staff who aren't turned off).
    old: the task being edited; a field left out keeps its value. Raises TaskError."""
    old = old or {}
    pick = lambda key, default=None: data[key] if key in data else old.get(key, default)
    title = clean_text(pick("title"), MAX_TITLE)
    if not title:
        raise TaskError("Give the task a title.")
    department_id = _id(pick("department_id"))
    if department_id is not None and department_id not in departments and \
            department_id != old.get("department_id"):
        raise TaskError("That department doesn't exist any more.")
    assigned_id = _id(pick("assigned_id"))
    if assigned_id is not None and assigned_id not in people and \
            assigned_id != old.get("assigned_id"):
        raise TaskError("That person can't be given tasks.")
    due_date = clean_date(pick("due_date"))
    due_time = clean_time(pick("due_time")) if due_date else None
    priority = pick("priority", "normal")
    if priority not in PRIORITIES:
        raise TaskError("Pick Normal or Urgent.")
    assigned_name = (people.get(assigned_id) or old.get("assigned_name") or "")[:MAX_NAME] \
        if assigned_id is not None else None
    return {"title": title, "notes": clean_text(pick("notes", ""), MAX_NOTES, lines=True),
            "department_id": department_id, "assigned_id": assigned_id,
            "assigned_name": assigned_name, "due_date": due_date, "due_time": due_time,
            "priority": priority}


def clean_status(data: dict, task: dict) -> str:
    """The status a person asked for. `from` is the status they saw: if someone else changed
    it meanwhile, nothing changes (so two taps from two phones don't skip a step)."""
    status = data.get("status")
    if status not in STATUSES:
        raise TaskError("Pick To do, In progress, Done or Won't do.")
    if "from" in data and data["from"] != task["status"]:
        raise TaskError(f"Someone just changed this task to {STATUS_NAMES[task['status']]}.")
    return status


# ---------------------------------------------------------------- tasks from Connecteam forms

def clean_form(data: dict, departments: dict[int, str]) -> dict:
    """A form's task settings from the dashboard: where its tasks go, which answer becomes the
    title ("" = "Form name: submitter"), whether the person who sent it gets the task, and the
    manager field its tasks follow (clean_follow; None: none)."""
    department_id = _id(data.get("department_id"))
    if department_id is not None and department_id not in departments:
        raise TaskError("That department doesn't exist any more.")
    name = clean_text(data.get("name"), MAX_FORM_NAME)
    return {"name": name, "department_id": department_id,
            "title_question": clean_text(data.get("title_question"), MAX_QUESTION),
            "assign_submitter": 1 if data.get("assign_submitter") in (1, True, "1") else 0,
            "follow": clean_follow(data.get("follow"))}


def clean_follow(raw) -> str | None:
    """A form's followed manager field, as stored (JSON): {field, name, options: [{id, name,
    color, status}], unset}. Each status option of the field maps to a task status; unset is
    the status while none is chosen (To do or In progress). None: the form doesn't follow one."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise TaskError("That manager field setting is damaged.") from None
    if not raw:
        return None
    if not isinstance(raw, dict) or not CT_ID_RE.match(str(raw.get("field") or "")):
        raise TaskError("Pick one of the form's status manager fields.")
    options, ids = [], set()
    for o in raw.get("options") if isinstance(raw.get("options"), list) else []:
        oid = str(o.get("id") or "") if isinstance(o, dict) else ""
        if not CT_ID_RE.match(oid) or oid in ids or len(options) >= MAX_OPTIONS:
            continue
        if o.get("status") not in STATUSES:
            raise TaskError("Pick a task status for each of the field's values.")
        ids.add(oid)
        options.append({"id": oid, "name": clean_text(o.get("name"), 80),
                        "color": clean_text(o.get("color"), 20), "status": o["status"]})
    unset = raw.get("unset") if raw.get("unset") in ("todo", "doing") else "todo"
    return json.dumps({"field": str(raw["field"]), "name": clean_text(raw.get("name"), 80),
                       "options": options, "unset": unset}, separators=(",", ":"))


def follow_of(form: dict) -> dict | None:
    """A form's followed manager field (clean_follow's), read back; None: none (or damaged)."""
    try:
        return json.loads(clean_follow(form.get("follow")) or "null")
    except TaskError:
        return None


def follow_status(follow: dict, option_id: str, name: str = "") -> str:
    """The task status a submission's manager field status maps to ("" = none chosen), found by
    the option's ID, else by its name (Connecteam doesn't promise a submission's option IDs are
    the form's); one the dashboard doesn't know yet counts as To do."""
    if not option_id and not name:
        return follow["unset"]
    found = next((o for o in follow["options"] if option_id and o["id"] == option_id), None) or next(
        (o for o in follow["options"] if name and o["name"].strip().lower() == name.strip().lower()), None)
    return found["status"] if found else "todo"


def follow_option(follow: dict, status: str) -> dict | None:
    """The manager field option a task status is written back as: the first mapped to it. None:
    no option is (To do while none chosen means it, and Connecteam can't clear a status)."""
    return next((o for o in follow["options"] if o["status"] == status), None)


def clean_photos(raw) -> str | None:
    """A task's photo links, as stored (JSON [{url, name}]): only https links."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    found = [{"url": p["url"], "name": clean_text(p.get("name"), 120)}
             for p in (raw if isinstance(raw, list) else [])[:MAX_PHOTOS]
             if isinstance(p, dict) and isinstance(p.get("url"), str)
             and p["url"].startswith("https://") and len(p["url"]) <= MAX_URL
             and not CONTROL_RE.search(p["url"]) and " " not in p["url"]]
    return json.dumps(found, separators=(",", ":")) if found else None


def submitted_time(value, zone=None) -> float | None:
    """When a submission was sent, as a Unix time: ctdata gives the property's local time
    ("YYYY-MM-DDTHH:MM", in zone); seconds, milliseconds and ISO times with an offset are read
    too. None when it can't be read."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 1e11 else 1)
    text = str(value or "").strip()
    if re.fullmatch(r"\d{9,14}(\.\d+)?", text):
        return submitted_time(float(text))
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=zone or timezone.utc)
    return when.timestamp()


def answer_text(value) -> str:
    """An answer as plain text: lists joined, anything else as written."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(t for t in (answer_text(v) for v in value) if t)
    if isinstance(value, dict):
        for key in ("text", "value", "name", "label"):
            if key in value:
                return answer_text(value[key])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def task_from_submission(form: dict, sub: dict, staff_by_ct: dict[str, dict],
                         when: float, zone=None) -> dict:
    """A new task for one form submission: the chosen answer (or "Form name: submitter") as its
    title, every answer in its notes, assigned to the person who sent it when the form says so
    and they're linked to Connecteam here. zone: the property's time zone, for when it was sent.
    Returns the fields db.add_task takes."""
    who = clean_text(sub.get("name"), MAX_NAME) or "Someone"
    answers = [(clean_text(a.get("question"), MAX_QUESTION), answer_text(a.get("answer")))
               for a in (sub.get("answers") or []) if isinstance(a, dict)]
    title = ""
    wanted = form.get("title_question", "").casefold()
    if wanted:
        title = next((clean_text(text, MAX_TITLE) for question, text in answers
                      if question.casefold() == wanted and clean_text(text, MAX_TITLE)), "")
    if not title:
        title = clean_text(f"{form['name'] or 'Form'}: {who}", MAX_TITLE)
    stamp = datetime.fromtimestamp(when, zone or timezone.utc).strftime("%b %-d, %Y %-I:%M %p")
    lines = [f"Sent by {who} on {stamp}.", ""]
    lines += [f"{question or 'Question'}: {text or '-'}" for question, text in answers]
    person = staff_by_ct.get(str(sub.get("user_id") or "")) if form.get("assign_submitter") else None
    notes = [clean_text(n, MAX_NOTES, lines=True) for n in
             ((sub.get("manager") or {}).get("notes") or {}).values()]
    return {"title": title, "notes": clean_text("\n".join(lines), MAX_NOTES, lines=True),
            "photos": clean_photos(sub.get("photos")),
            "manager_note": clean_text("\n".join(n for n in notes if n), MAX_NOTES, lines=True) or None,
            "department_id": form["department_id"],
            "assigned_id": person["id"] if person else None,
            "assigned_name": person["name"][:MAX_NAME] if person else None,
            "due_date": None, "due_time": None, "priority": "normal",
            "source": "form", "form_id": form["form_id"], "form_name": form["name"],
            "submission_id": str(sub.get("id"))[:80], "source_url": clean_url(sub.get("url"))}


def clean_url(value) -> str | None:
    """A link back to a submission in Connecteam: only an https address on connecteam.com."""
    text = str(value or "").strip()
    if re.fullmatch(r"https://([a-z0-9-]+\.)*connecteam\.com(/[^\s\"'<>]*)?", text) and len(text) <= 500:
        return text
    return None


def form_range(form: dict, now: float, zone) -> tuple[datetime, date]:
    """What a form's check asks Connecteam about (ctdata.form_submissions' start and end):
    submissions from the moment the form was chosen, or FORM_OVERLAP before the last good check
    (one that arrived late still counts), at most FORM_LOOKBACK_DAYS back, to today in the
    property's zone. So a check is usually one page. Repeats are never made into tasks twice
    (db.task_form_seen)."""
    since = max(form["picked_at"], (form.get("last_ok") or 0) - FORM_OVERLAP,
                now - FORM_LOOKBACK_DAYS * 86400)
    return datetime.fromtimestamp(since, zone), datetime.fromtimestamp(now, zone).date()


def form_interval(count: int) -> int:
    """Seconds between a form's checks when `count` forms make tasks: every 10 minutes, or
    longer so that all of them stay within FORM_DAILY_REQUESTS a day (whole minutes)."""
    spread = -(-max(count, 1) * 86400 // FORM_DAILY_REQUESTS)
    return max(FORM_CHECK_SECONDS, -(-spread // 60) * 60)


def form_requests_a_day(count: int) -> int:
    """About how many Connecteam requests the checks make a day (one page each)."""
    return round(count * 86400 / form_interval(count)) if count else 0


def form_due(form: dict, now: float, count: int = 1) -> bool:
    return now - (form.get("last_check") or 0) >= form_interval(count)


# ---------------------------------------------------------------- tasks in backups

def _flag_id(value):
    """An ID from a backup: a whole number, None, or damaged (False)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**53:
        return False
    return value


def _when(value, now: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < now + 86400:
        return None
    return float(value)


def clean_backup_section(raw, now: float) -> dict | None:
    """The tasks section of a backup or migration file, checked as strictly as typing it in.
    None: the file has none (a backup from before tasks), and the ones here stay. A task whose
    department can't be read is left out rather than becoming a task for Everyone."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("The backup's tasks are damaged.")

    def rows(key: str, limit: int) -> list:
        value = raw.get(key, [])
        if not isinstance(value, list) or len(value) > limit:
            raise ValueError("The backup's tasks are damaged or too many.")
        return value

    tasks, ids, submissions, ct_ids = [], set(), set(), set()
    for row in rows("tasks", MAX_TASKS):
        if not isinstance(row, dict):
            continue
        task_id = _flag_id(row.get("id"))
        department_id = _flag_id(row.get("department_id"))
        created_at = _when(row.get("created_at"), now)
        title = clean_text(row.get("title"), MAX_TITLE)
        if not task_id or task_id in ids or department_id is False or not created_at or not title:
            continue
        ids.add(task_id)
        assigned_id = _flag_id(row.get("assigned_id")) or None
        status = row.get("status") if row.get("status") in STATUSES else "todo"
        try:
            due_date = clean_date(row.get("due_date"))
            due_time = clean_time(row.get("due_time")) if due_date else None
        except TaskError:
            due_date = due_time = None
        source = row.get("source") if row.get("source") in SOURCES else "quick"
        ct_task_id = str(row.get("ct_task_id") or "")
        ct_task_id = ct_task_id if CT_ID_RE.match(ct_task_id) and ct_task_id not in ct_ids else None
        ct_ids.add(ct_task_id)
        ct_board_id = str(row.get("ct_board_id") or "")
        form_id = str(row.get("form_id") or "")
        form_id = form_id if source == "form" and FORM_ID_RE.match(form_id) else None
        submission_id = clean_text(row.get("submission_id"), 80) or None if form_id else None
        if submission_id and (form_id, submission_id) in submissions:
            submission_id = None   # one task per submission
        submissions.add((form_id, submission_id))
        tasks.append({
            "id": task_id, "title": title,
            "notes": clean_text(row.get("notes"), MAX_NOTES, lines=True),
            "department_id": department_id, "assigned_id": assigned_id,
            "assigned_name": clean_text(row.get("assigned_name"), MAX_NAME) or None
            if assigned_id else None,
            "due_date": due_date, "due_time": due_time,
            "priority": row.get("priority") if row.get("priority") in PRIORITIES else "normal",
            "status": status, "status_at": _when(row.get("status_at"), now) or created_at,
            "status_by": clean_text(row.get("status_by"), MAX_NAME) or None,
            "created_at": created_at, "created_by_id": _flag_id(row.get("created_by_id")) or None,
            "created_by_name": clean_text(row.get("created_by_name"), MAX_NAME),
            "source": source,
            "form_id": form_id,
            "form_name": clean_text(row.get("form_name"), MAX_FORM_NAME) or None,
            "submission_id": submission_id,
            "source_url": clean_url(row.get("source_url")),
            "updated_at": _when(row.get("updated_at"), now) or created_at,
            "deleted_at": _when(row.get("deleted_at"), now),
            "deleted_by": clean_text(row.get("deleted_by"), MAX_NAME) or None,
            "ct_task_id": ct_task_id,
            "ct_board_id": ct_board_id if ct_task_id and CT_ID_RE.match(ct_board_id) else None,
            "ct_status": str(row.get("ct_status") or "")[:80] if CT_ID_RE.match(
                str(row.get("ct_status") or "")) else None,
            "photos": clean_photos(row.get("photos")),
            "manager_note": clean_text(row.get("manager_note"), MAX_NOTES, lines=True) or None})
    history = []
    for row in rows("history", MAX_HISTORY):
        if not isinstance(row, dict) or row.get("task_id") not in ids:
            continue
        at = _when(row.get("at"), now)
        if not at or row.get("action") not in HISTORY_ACTIONS:
            continue
        history.append({"task_id": row["task_id"], "at": at,
                        "by_id": _flag_id(row.get("by_id")) or None,
                        "by_name": clean_text(row.get("by_name"), MAX_NAME),
                        "action": row["action"],
                        "from_status": row.get("from_status") if row.get("from_status") in STATUSES else None,
                        "to_status": row.get("to_status") if row.get("to_status") in STATUSES else None,
                        "note": clean_text(row.get("note"), MAX_NOTE) or None})
    forms, form_ids = [], set()
    for row in rows("forms", MAX_FORMS):
        form_id = str(row.get("form_id") or "") if isinstance(row, dict) else ""
        department_id = _flag_id(row.get("department_id")) if form_id else False
        picked_at = _when(row.get("picked_at"), now) if form_id else None
        if not FORM_ID_RE.match(form_id) or form_id in form_ids or department_id is False or not picked_at:
            continue
        form_ids.add(form_id)
        try:
            follow = clean_follow(row.get("follow"))
        except TaskError:
            follow = None
        forms.append({"form_id": form_id, "name": clean_text(row.get("name"), MAX_FORM_NAME),
                      "follow": follow,
                      "department_id": department_id,
                      "title_question": clean_text(row.get("title_question"), MAX_QUESTION),
                      "assign_submitter": 1 if row.get("assign_submitter") in (1, True) else 0,
                      "picked_at": picked_at, "last_ok": _when(row.get("last_ok"), now)})
    seen = sorted({(str(row[0]), str(row[1])[:80]) for row in rows("seen", MAX_HISTORY)
                   if isinstance(row, list) and len(row) == 2 and FORM_ID_RE.match(str(row[0]))
                   and row[1] not in (None, "")})
    return {"tasks": tasks, "history": history, "forms": forms, "seen": seen,
            "sync": _clean_sync(raw.get("sync"), now)}


def _clean_sync(raw, now: float) -> dict:
    """The Connecteam task sync's part of a backup: its boards (each with its department), the
    Connecteam tasks already linked or passed over, and whether it was on (a restore turns it
    off all the same: db._pause_task_sync)."""
    raw = raw if isinstance(raw, dict) else {}
    boards, board_ids = [], set()
    for row in raw.get("boards") if isinstance(raw.get("boards"), list) else []:
        board_id = str(row.get("board_id") or "") if isinstance(row, dict) else ""
        department_id = _flag_id(row.get("department_id")) if board_id else False
        if not CT_ID_RE.match(board_id) or board_id in board_ids or department_id is False \
                or len(boards) >= MAX_FORMS:
            continue
        board_ids.add(board_id)
        boards.append({"board_id": board_id, "name": clean_text(row.get("name"), MAX_FORM_NAME),
                       "department_id": department_id,
                       "picked_at": _when(row.get("picked_at"), now) or now})
    seen = raw.get("seen") if isinstance(raw.get("seen"), list) else []
    seen = sorted({(str(r[0]), str(r[1])) for r in seen[:MAX_HISTORY] if isinstance(r, list)
                   and len(r) == 2 and CT_ID_RE.match(str(r[0])) and CT_ID_RE.match(str(r[1]))})
    return {"boards": boards, "seen": seen, "enabled": raw.get("enabled") is True}
