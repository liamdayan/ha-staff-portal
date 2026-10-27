"""The Connecteam task sync: Staff Access's tasks and Connecteam's Quick Tasks, both ways, and
form tasks that follow a submission's manager field (its status), both ways.

Quick Tasks (Connecteam's Tasks API; scopes in brackets):
- boards:  GET    /tasks/v1/taskboards                                  [quick_tasks.read]
- read:    GET    /tasks/v1/taskboards/{board}/tasks?status=all         [quick_tasks.read]
- create:  POST   /tasks/v1/taskboards/{board}/tasks                    [quick_tasks.write]
- change:  PUT    /tasks/v1/taskboards/{board}/tasks/{task}             [quick_tasks.write]
- delete:  DELETE /tasks/v1/taskboards/{board}/tasks/{task}             [quick_tasks.delete, optional]
A followed form: PUT /forms/v1/forms/{form}/form-submissions/{id}       [forms.write]

How the two sides map (a task's "view", the same shape for both):
- title, notes (Connecteam's description), due (Unix seconds: a due date with no time is 11:59
  PM in the property's zone), users (Connecteam user IDs: the assignee when they're linked here),
  done (Connecteam's "completed"), archived (Connecteam's isArchived; Won't do or the bin here).
- To do and In progress are "published" there (In progress is kept here); Done is "completed".
  A task with no one linked assigned is a "draft" in Connecteam, which it requires, and which
  says nothing about done.
- Won't do and the bin archive the Connecteam task. Connecteam can't archive a draft, so an
  unassigned one is deleted there instead (and made again if it's reopened or put back), when the
  app has quick_tasks.delete; without it the draft is left on the board and the task says so.
- Archived or deleted in Connecteam: the bin here, saying so ("Someone in Connecteam").

Each synced task keeps what both sides held when they last agreed (ct_sync: "seen", Connecteam's
view, and "base", this side's), so a change is known by which side moved: one side's change is
copied to the other, a change copied in from Connecteam is never sent back (no echoes), and when
both changed the same field the newer wins. Connecteam gives no change times, so its change
counts as made when its board was last read before (the earliest it can have been); a change
here that's waiting to be sent (ct_pending) and is newer wins. Either way the history says so.

Connecteam has no reference field and no "changed since", so each read takes the whole board
(status=all, 100 a page). A create that may have half-arrived (a timeout) is kept as "creating"
and found again on the next read by its title and assignees before it's ever sent twice.

Every request goes through the reports lane (connecteam.py, reports=True) and is counted here
too: board reads are planned with the form checks within tasks.FORM_DAILY_REQUESTS a day,
changes are sent as they happen (at most WRITE_DAILY_CAP a day; the rest wait). A refusal or a
"too many requests" pauses only this sync, with the reason on the Tasks tab, and it tries again
later; the clock checks carry on. Nothing is asked while the sync is off, and a restore turns it
off (db._pause_task_sync) so a restored copy never sends anything on its own.
"""

import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime, timezone

import ctdata
import tasks
from connecteam import ConnecteamError, OwnTooMany, Rejected, ReportsBusy, ReportsRefused

log = logging.getLogger("staff_portal")

FIELDS = ("title", "notes", "due", "users", "done", "archived")
PAGE = 100                 # tasks a page (Connecteam's most)
MAX_PAGES = 20             # pages read from one board, at most (2,000 tasks)
MAX_BOARDS = 20
WRITE_DAILY_CAP = 200      # changes sent a day, at most (inside the reports lane's 1,000)
PUSH_BATCH = 30            # changes sent in one go
REFUSED_WAIT = 30 * 60     # a refusal pauses the sync this long (or until Save or Test)
MAX_BACKOFF = 3600
BY_CT = {"name": "Someone in Connecteam"}
IN_CT = "in Connecteam"
NO_BOARD = "Not in Connecteam: no board is chosen for this department."
CT_ID_RE = tasks.CT_ID_RE
SCOPES_NEEDED = ("quick_tasks.read", "quick_tasks.write")
SCOPE_DELETE = "quick_tasks.delete"   # optional: only for closing a task nobody is assigned to
LEFT_OPEN = ("Still on the Connecteam board: Connecteam can't close a task nobody is assigned "
             "to without the quick_tasks.delete permission.")
SCOPES_FOLLOW = ("forms.write",)

_db = lambda: None   # noqa: E731 - the Database (main.db; tests swap it)
_ct = None           # connecteam.Connecteam
_zone = lambda: timezone.utc   # noqa: E731
_lock: asyncio.Lock | None = None   # one sync step at a time (made for the running loop)
_lock_loop = None
_kick: asyncio.Task | None = None
paused_until = 0.0
pause_reason: str | None = None
failures = 0


class Paused(Exception):
    """The sync is paused (a refusal or "too many requests"): nothing was asked."""


class Busy(Exception):
    """No room in the lane or today's changes are used up: nothing was asked; try later."""


def _guard() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
    return _lock


def configure(get_db, connecteam, zone) -> None:
    global _db, _ct, _zone
    _db, _ct, _zone = get_db, connecteam, zone


def retry_refused() -> int:
    """Once at start: changes Connecteam refused (it said no to what was sent) are tried again,
    in case an update to the add-on fixed what it didn't like. Returns how many."""
    db = _db()
    cur = db.conn.execute("UPDATE tasks SET ct_pending = ? WHERE ct_pending IS NULL AND "
                          "ct_note LIKE 'Connecteam didn''t take%'", (time.time(),))
    db.conn.commit()
    return cur.rowcount


def on() -> bool:
    s = _db().settings()
    return s.get("ct_tasks_on") == "1" and s.get("connecteam_enabled") == "1"


def lift_pause() -> None:
    """Save or Test: try again now."""
    global paused_until, pause_reason, failures
    paused_until, pause_reason, failures = 0.0, None, 0


def _pause(seconds: float, reason: str) -> None:
    global paused_until, pause_reason
    paused_until = max(paused_until, time.time() + seconds)
    pause_reason = reason
    log.warning("Task sync: %s", reason)


# ---------------------------------------------------------------- the budget

def usage() -> dict:
    """Today's (UTC) requests: {"day", "reads", "writes"}."""
    try:
        u = json.loads(_db().settings().get("ct_tasks_usage") or "{}")
    except ValueError:
        u = {}
    today = datetime.now(timezone.utc).date().isoformat()
    return u if isinstance(u, dict) and u.get("day") == today else {"day": today, "reads": 0, "writes": 0}


def _count(kind: str) -> None:
    u = usage()
    u[kind] = int(u.get(kind) or 0) + 1
    _db().update_settings({"ct_tasks_usage": json.dumps(u)})


async def _ask(method: str, path: str, params=None, body=None) -> dict:
    """One request in the reports lane that pauses only this sync when refused or told to slow
    down. Raises Paused or Busy (nothing asked), Rejected, or ConnecteamError (no answer: it
    may have arrived)."""
    global failures
    if _ct is None or _db().settings().get("connecteam_enabled") != "1":
        # Turned off in Settings: nothing is asked (a followed form's status waits too).
        raise Paused("Connecteam is turned off in Settings → Connections.")
    if time.time() < paused_until:
        raise Paused(pause_reason or "The task sync is paused.")
    write = method != "GET"
    if write and int(usage().get("writes") or 0) >= WRITE_DAILY_CAP:
        raise Busy(f"The task sync sent its {WRITE_DAILY_CAP} changes for today; the rest go "
                   "after midnight UTC.")
    try:
        data = await _ct._request(method, path, params, body, reports=True, own_pause=True)
    except OwnTooMany as err:
        failures += 1
        wait = min(MAX_BACKOFF, max(err.retry_after or 0, 60 * 2 ** min(failures - 1, 6)))
        _pause(wait, f"Connecteam asked for fewer requests, so the task sync waits "
                     f"{max(1, round(wait / 60))} min (the clock checks carry on).")
        _count("writes" if write else "reads")
        raise Paused(pause_reason) from None
    except ReportsRefused as err:
        _pause(REFUSED_WAIT, str(err))
        _count("writes" if write else "reads")
        raise Paused(str(err)) from None
    except ReportsBusy as err:
        raise Busy(str(err)) from None
    except ConnecteamError:
        _count("writes" if write else "reads")
        raise
    failures = 0
    _count("writes" if write else "reads")
    return data


# ---------------------------------------------------------------- the two sides as one shape

def _key(field: str, value):
    """What's compared: text without its spacing (Connecteam may re-wrap it), users in order."""
    if field in ("title", "notes"):
        return " ".join(str(value or "").split())
    if field == "users":
        return tuple(sorted(value or ()))
    return value


def _same(a: dict, b: dict) -> bool:
    return all(_key(f, a.get(f)) == _key(f, b.get(f)) for f in FIELDS)


def due_stamp(due_date, due_time, zone) -> int | None:
    if not due_date:
        return None
    when = datetime.fromisoformat(f"{due_date}T{due_time or '23:59'}").replace(tzinfo=zone)
    return int(when.timestamp())


def due_parts(stamp, zone) -> tuple[str | None, str | None]:
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or not 0 < stamp < 2**33:
        return None, None
    when = datetime.fromtimestamp(stamp, zone)
    clock = when.strftime("%H:%M")
    return when.date().isoformat(), None if clock == "23:59" else clock


def _links() -> tuple[dict[int, int], dict[int, dict]]:
    """Linked people: {staff ID: Connecteam user ID} for everyone, and {Connecteam user ID:
    person} for those who may be given tasks (as in the staff app: anyone not turned off or
    archived, so someone who hasn't signed in yet too)."""
    to_ct, to_staff = {}, {}
    for r in _db().conn.execute("SELECT id, name, status, archived_at, connecteam_user_id FROM staff "
                                "WHERE connecteam_user_id IS NOT NULL"):
        uid = str(r["connecteam_user_id"])
        if uid.isdigit() and len(uid) < 16:
            to_ct[r["id"]] = int(uid)
            if r["status"] not in ("disabled", "archived") and not r["archived_at"]:
                to_staff.setdefault(int(uid), dict(r))
    return to_ct, to_staff


def sa_view(t: dict, to_ct: dict[int, int]) -> dict:
    uid = to_ct.get(t["assigned_id"]) if t["assigned_id"] else None
    return {"title": t["title"], "notes": t["notes"] or "",
            "due": due_stamp(t["due_date"], t["due_time"], _zone()),
            "users": [uid] if uid else [], "done": t["status"] == "done",
            "archived": t["status"] == "wont" or bool(t["deleted_at"])}


def html_text(raw) -> str:
    """A Connecteam description as plain text: read as [{type: html, html}] (or {content})."""
    parts = raw if isinstance(raw, list) else [raw]
    text = []
    for p in parts:
        if isinstance(p, dict):
            p = p.get("html") if p.get("html") is not None else p.get("content")
        if isinstance(p, str):
            p = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d)>", "\n", p)
            text.append(html.unescape(re.sub(r"<[^>]*>", "", p)))
    return tasks.clean_text(BY_RE.sub("", "\n".join(text)), tasks.MAX_NOTES, lines=True)


def ct_view(r: dict) -> dict:
    """A Connecteam task in the shared shape, its text capped as typed text is."""
    users = sorted({u for u in r.get("userIds") or [] if isinstance(u, int) and not isinstance(u, bool)
                    and 0 < u < 2**53})
    due = r.get("dueDate")
    return {"title": tasks.clean_text(r.get("title"), tasks.MAX_TITLE) or "Untitled",
            "notes": html_text(r.get("description")),
            "due": int(due) if isinstance(due, (int, float)) and not isinstance(due, bool)
            and 0 < due < 2**33 else None,
            "users": users, "done": r.get("status") == "completed",
            "archived": r.get("isArchived") is True,
            # Connecteam's API can't say how a recurring task repeats, or whether completing it
            # completes the series: kept to show it, never compared or sent (not in FIELDS).
            "recurring": r.get("type") == "recurring"}


BY_LINE = "Added by {} in Staff Access"
BY_RE = re.compile(r"\n*Added by [^\n]{1,80} in Staff Access\s*$")


def _by(t: dict) -> str:
    """Who made a task here, for its description in Connecteam: its API can't set who created a
    task (that's always the account the keys belong to). "" when Staff Access didn't make it."""
    name = (t.get("created_by_name") or "").strip()
    return name[:60] if name and t.get("source") == "quick" else ""


RECURRING = ("This task repeats in Connecteam, so it's edited or deleted there. Its status "
             "can be changed here: each repeat is a task of its own.")
_logged_recurring: set[tuple] = set()


def recurring(t: dict) -> bool:
    """A repeat of a Connecteam recurring task (Connecteam lists each repeat as a task of its own,
    days ahead): its status syncs both ways; it's edited or deleted only in Connecteam, where
    that could touch the series."""
    return bool(t.get("ct_task_id")) and bool((_sync(t).get("seen") or {}).get("recurring"))


def repeats_not_yet(today: str) -> set[int]:
    """The repeats of Connecteam recurring tasks that don't show yet. Connecteam lists each
    repeat as a task of its own, days ahead, and says nothing of which series it's in, so a
    series is its board, title and person. A repeat shows from the day after the one before it
    was due (a daily one each day; a weekly one the day after last week's), and the first of a
    series straight away. today: "YYYY-MM-DD" in the property's zone."""
    series: dict[tuple, list[tuple[str, int]]] = {}
    for r in _db().conn.execute(
            "SELECT id, ct_board_id, title, assigned_id, due_date, ct_sync FROM tasks WHERE "
            "ct_task_id IS NOT NULL AND due_date IS NOT NULL AND ct_sync LIKE '%\"recurring\":true%'"):
        if (_sync(dict(r)).get("seen") or {}).get("recurring"):
            key = (r["ct_board_id"], r["title"].strip().lower(), r["assigned_id"])
            series.setdefault(key, []).append((r["due_date"], r["id"]))
    hidden = set()
    for repeats in series.values():
        repeats.sort()
        for (before, _), (due, task_id) in zip(repeats, repeats[1:]):
            if due > today and before >= today:   # the one before isn't past its day yet
                hidden.add(task_id)
    return hidden


def _log_recurring(board_id: str, r: dict) -> None:
    """What Connecteam sends for a recurring task (its docs don't say how they work), once per
    change, so the add-on log shows how its repeats come through."""
    key = (str(r.get("id")), r.get("status"), r.get("startTime"), r.get("dueDate"), r.get("isArchived"))
    if key in _logged_recurring or len(_logged_recurring) > 2000:
        return
    _logged_recurring.add(key)
    log.info("Task sync: recurring task on board %s: id %s, status %s, start %s, due %s, "
             "archived %s, %d people", board_id, *key[:5], len(r.get("userIds") or []))


def _payload(v: dict, by: str = "") -> dict:
    """A task as Connecteam takes it: without anyone it must be a draft, and not archived. by:
    who made it here, as the description's last line (read back without it)."""
    notes = v["notes"] or ""
    if by:
        notes = (notes + "\n\n" if notes else "") + BY_LINE.format(by)
    body = {"title": v["title"] or "Untitled", "userIds": list(v["users"]),
            "status": ("completed" if v["done"] else "published") if v["users"] else "draft",
            "isArchived": bool(v["archived"] and v["users"]),
            "description": {"content": notes}}
    if v["due"] and v["due"] > time.time():
        body["dueDate"] = v["due"]   # Connecteam refuses a date in the past, even one it had
    return body


def _with_start(body: dict) -> dict:
    """Connecteam takes a due date only with a start time (it compares them), and refuses one
    in the past: the task's own when it's still ahead, otherwise a minute from now."""
    if "dueDate" in body and "startTime" not in body:
        body["startTime"] = min(int(time.time()) + 60, body["dueDate"])
    return body


# ---------------------------------------------------------------- what's kept on a task

def _sync(t: dict) -> dict:
    try:
        value = json.loads(t.get("ct_sync") or "{}")
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _save(task_id: int, *, pending_was=None, clear_pending=False, **columns) -> None:
    """Set some ct_ columns (ct_sync as a dict); clear_pending: the change waiting is sent, unless
    another came meanwhile (ct_pending moved on)."""
    if "ct_sync" in columns and isinstance(columns["ct_sync"], dict):
        columns["ct_sync"] = json.dumps(columns["ct_sync"], separators=(",", ":"))
    db = _db()
    if columns:
        db.conn.execute(f"UPDATE tasks SET {', '.join(f'{k} = ?' for k in columns)} WHERE id = ?",
                        (*columns.values(), task_id))
    if clear_pending:
        db.conn.execute("UPDATE tasks SET ct_pending = NULL WHERE id = ? AND (ct_pending IS ? OR "
                        "ct_pending <= ?)", (task_id, pending_was, pending_was or 0))
    db.conn.commit()


def _seen_ct(ct_task_id: str, board_id: str) -> None:
    _db().conn.execute("INSERT OR IGNORE INTO task_ct_seen (ct_task_id, board_id, seen_at) "
                       "VALUES (?, ?, ?)", (ct_task_id, board_id, time.time()))


def boards() -> list[dict]:
    return [dict(r) for r in _db().conn.execute("SELECT * FROM task_ct_boards ORDER BY picked_at, board_id")]


def board_for(department_id) -> dict | None:
    """The board new tasks of a department go to (the first chosen for it)."""
    return next((b for b in boards() if b["department_id"] == department_id), None)


def follows(t: dict) -> dict | None:
    """The manager field a form task follows, if its form follows one."""
    if t.get("source") != "form" or not t.get("submission_id") or not t.get("form_id"):
        return None
    form = _db().task_form(t["form_id"])
    return tasks.follow_of(form) if form else None


# ---------------------------------------------------------------- changes made here

def touch(task_id: int, new: bool = False) -> None:
    """A task was changed here (new: just made): mark it to be sent, and send soon. Only tasks
    in Connecteam (or meant to be) are marked; a new one with no board says so, quietly."""
    db = _db()
    t = db.task(task_id)
    if not t:
        return
    if t["source"] == "form":
        wanted = follows(t) is not None
    else:
        wanted = bool(t["ct_task_id"] or _sync(t).get("gone") or _sync(t).get("creating"))
        if new and not wanted and on():
            if board_for(t["department_id"]):
                wanted = True
            else:
                _save(task_id, ct_note=NO_BOARD)
    if wanted:
        db.conn.execute("UPDATE tasks SET ct_pending = ? WHERE id = ?", (time.time(), task_id))
        db.conn.commit()
        kick()


def kick() -> None:
    """Send what's waiting, soon (one sender at a time)."""
    global _kick
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _kick is None or _kick.done():
        _kick = loop.create_task(_send_soon())


async def _send_soon() -> None:
    await asyncio.sleep(0)
    try:
        for _ in range(3):   # again while changes come in during a send
            started = time.time()
            await push_pending()
            if not _db().conn.execute("SELECT 1 FROM tasks WHERE ct_pending >= ? LIMIT 1",
                                      (started,)).fetchone():
                break
    except Exception as err:  # noqa: BLE001 - a background send never raises
        log.warning("Task sync: %r", err)


async def settle() -> None:
    """Wait for a send that's under way (tests, and before a backup)."""
    while _kick is not None and not _kick.done():
        await asyncio.wait([_kick])


async def push_pending() -> int:
    """Send the changes waiting, oldest first. Returns how many were sent."""
    sent = 0
    async with _guard():
        # Quick tasks wait while the sync is off; a followed form's don't need it.
        rows = [dict(r) for r in _db().conn.execute(
            "SELECT * FROM tasks WHERE ct_pending IS NOT NULL AND (source = 'form' OR ?) "
            "ORDER BY ct_pending LIMIT ?", (1 if on() else 0, PUSH_BATCH))]
        for t in rows:
            try:
                sent += bool(await _push(t))
            except (Paused, Busy):
                break
            except ConnecteamError as err:
                log.info("Task sync: couldn't send task %s: %s", t["id"], err)
                break
    return sent


async def _push(t: dict) -> bool:
    if t["source"] == "form":
        return await _push_form(t)
    if not on():
        return False   # waits until the sync is on again
    sync, (to_ct, _) = _sync(t), _links()
    mapped = {b["board_id"]: b for b in boards()}
    if sync.get("creating"):
        board = mapped.get(sync.get("board"))
        if board:
            await _poll(board)   # finds it if it arrived, or lets it be made again
            return False
        _save(t["id"], ct_sync={}, clear_pending=True, pending_was=t["ct_pending"])
        return False
    sa = sa_view(t, to_ct)
    if not t["ct_task_id"]:
        if sa["archived"]:
            _save(t["id"], clear_pending=True, pending_was=t["ct_pending"])
            return False
        board = mapped.get(sync.get("board")) or board_for(t["department_id"])
        if not board:
            _save(t["id"], ct_note=NO_BOARD, clear_pending=True, pending_was=t["ct_pending"])
            return False
        return await _create(t, board, sa)
    board = mapped.get(t["ct_board_id"])
    seen, base = sync.get("seen"), sync.get("base")
    if not board:   # its board isn't followed any more
        _save(t["id"], clear_pending=True, pending_was=t["ct_pending"])
        return False
    if seen is None or base is None:
        await _poll(board)   # Connecteam's copy first (after a restore)
        return False
    merged = {f: sa[f] if _key(f, sa[f]) != _key(f, base[f]) else seen[f] for f in FIELDS}
    if _same(merged, seen):
        _save(t["id"], ct_sync={"seen": seen, "base": sa}, clear_pending=True, pending_was=t["ct_pending"])
        return False
    path = f"/tasks/v1/taskboards/{board['board_id']}/tasks/{t['ct_task_id']}"
    try:
        if merged["archived"] and not merged["users"]:   # a draft can't be archived
            if _ct.scopes is not None and SCOPE_DELETE not in _ct.scopes:
                _left_open(t, seen, sa)
                return False
            try:
                await _ask("DELETE", path)
            except Rejected as err:
                if err.status != 403:
                    raise
                _left_open(t, seen, sa)
                return False
            _gone(t, board["board_id"])
            return True
        data = await _ask("PUT", path, body=_with_start({**await _kept_fields(board["board_id"], t["ct_task_id"]),
                                                         **_payload(merged, _by(t))}))
    except Rejected as err:
        if err.status == 404:   # deleted there: made again unless it's finished with
            _gone(t, board["board_id"])
            if not merged["archived"]:
                return await _create({**t, "ct_task_id": None, "ct_sync": "{}"}, board, sa)
            return False
        if err.status < 500:
            _save(t["id"], ct_note=f"Connecteam didn't take the last change: {err}"[:300],
                  clear_pending=True, pending_was=t["ct_pending"])
            return False
        raise
    _save(t["id"], ct_sync={"seen": _answered(data, merged), "base": sa}, ct_note=None, clear_pending=True,
          pending_was=t["ct_pending"])
    return True


async def _kept_fields(board_id: str, task_id: str) -> dict:
    """What Connecteam holds on a task that Staff Access doesn't (its start time while it's
    still ahead, labels and type), read just before a change and sent back as they are. {} when
    it can't be read (the change still goes)."""
    try:
        data = await _ask("GET", f"/tasks/v1/taskboards/{board_id}/tasks",
                          [("taskIds", task_id), ("status", "all"), ("limit", str(PAGE))])
    except Rejected:
        return {}
    r = next((r for r in data.get("tasks") or [] if isinstance(r, dict) and str(r.get("id")) == task_id), None)
    if r is None:
        return {}
    kept = {}
    if isinstance(r.get("startTime"), int) and not isinstance(r["startTime"], bool) and r["startTime"] > time.time():
        kept["startTime"] = r["startTime"]   # one in the past is refused, so it's left as it is
    if isinstance(r.get("labelIds"), list):
        kept["labelIds"] = [str(x) for x in r["labelIds"] if isinstance(x, (str, int))][:50]
    if r.get("type") in ("oneTime", "recurring"):
        kept["type"] = r["type"]
    # Sub-tasks have endpoints of their own and Connecteam's own "complete a task" example
    # leaves them out, so they're never sent: sending them could make them again.
    return kept


def _answered(data: dict, sent: dict) -> dict:
    """What Connecteam holds after a write: its reply when it gives the task (it drops people
    who left, and keeps a due date the API can't clear), else what was sent."""
    if not isinstance(data.get("title"), str):
        return sent
    v = ct_view(data)
    if "description" not in data:
        v["notes"] = sent["notes"]
    if v["due"] is None and "dueDate" not in data:
        v["due"] = sent["due"]
    if not v["users"]:
        v["done"] = sent["done"]   # a draft says nothing about done
    return v


def _left_open(t: dict, seen: dict, sa: dict) -> None:
    """Won't do or the bin here for a task nobody is assigned to, with no permission to delete
    it there: Connecteam keeps its draft, both sides count as agreed (so it isn't tried again),
    and the task says so."""
    _save(t["id"], ct_sync={"seen": seen, "base": sa}, ct_note=LEFT_OPEN, clear_pending=True,
          pending_was=t["ct_pending"])


def _gone(t: dict, board_id: str) -> None:
    """Its Connecteam task is deleted: the link goes, and it's made again if it comes back."""
    _save(t["id"], ct_task_id=None, ct_sync={"gone": True, "board": board_id},
          clear_pending=True, pending_was=t["ct_pending"])


async def _create(t: dict, board: dict, sa: dict) -> bool:
    """Make the task in Connecteam. Marked "creating" first: if no answer comes, the next read
    of the board looks for it before it's ever made twice."""
    board_id = board["board_id"]
    _save(t["id"], ct_sync={"creating": time.time(), "board": board_id, "base": sa})
    try:
        data = await _ask("POST", f"/tasks/v1/taskboards/{board_id}/tasks", body=_with_start(_payload(sa, _by(t))))
    except (Paused, Busy):
        _save(t["id"], ct_sync={"board": board_id})   # never asked
        raise
    except Rejected as err:
        if err.status >= 500:
            raise   # it may have been made: the marker stays
        _save(t["id"], ct_sync={}, ct_note=f"Connecteam didn't take it: {err}"[:300],
              clear_pending=True, pending_was=t["ct_pending"])
        return False
    cid = str(data.get("id") or "")
    if not CT_ID_RE.match(cid):
        raise ConnecteamError("Connecteam made the task without saying its ID.")
    _seen_ct(cid, board_id)
    _save(t["id"], ct_task_id=cid, ct_board_id=board_id, ct_sync={"seen": _answered(data, sa), "base": sa},
          ct_note=None, clear_pending=True, pending_was=t["ct_pending"])
    return True


async def _push_form(t: dict) -> bool:
    """A followed form task's status, written to its submission's manager field."""
    follow = follows(t)
    if not follow or t["deleted_at"] or not str(t["form_id"]).isdigit():
        _save(t["id"], clear_pending=True, pending_was=t["ct_pending"])
        return False
    option = tasks.follow_option(follow, t["status"])
    if option is None or option["id"] == (t["ct_status"] or ""):
        note = None if option else (
            f"Connecteam keeps its {follow['name'] or 'status'}: {tasks.STATUS_NAMES[t['status']]} "
            "isn't one of its values, and the API can't clear it.")
        _save(t["id"], ct_note=note, clear_pending=True, pending_was=t["ct_pending"])
        return False
    body = {"managerFields": [{"managerFieldId": follow["field"], "managerFieldType": "status",
                               "status": {"statusOptionId": option["id"], "name": option["name"],
                                          "color": option["color"]}}]}
    note_field, note = await _note_field(t["form_id"]), None
    if note_field:
        note = marked_note(t["manager_note"], option["name"], t["status_by"])
        body["managerFields"].append({"managerFieldId": note_field, "managerFieldType": "note", "note": note})
    try:
        await _ask("PUT", f"/forms/v1/forms/{t['form_id']}/form-submissions/{t['submission_id']}",
                   body=body)
    except Rejected as err:
        if err.status >= 500:
            raise
        _save(t["id"], ct_note=f"Connecteam didn't take the status: {err}"[:300],
              clear_pending=True, pending_was=t["ct_pending"])
        return False
    _save(t["id"], ct_status=option["id"], ct_sync={"wrote": time.time()}, ct_note=None,
          clear_pending=True, pending_was=t["ct_pending"],
          **({"manager_note": note} if note is not None else {}))
    return True


MARKED_RE = re.compile(r"^Marked .{1,80} by .{1,80} in Staff Access$")


def marked_note(note: str | None, status: str, by: str | None) -> str:
    """The submission's note with who changed its status here as its last line: Connecteam's
    API can't say who made a change (it's always the account the keys belong to). An earlier
    such line is replaced, so the note doesn't grow with each change; a manager's text stays."""
    kept = [line for line in (note or "").split("\n") if not MARKED_RE.match(line.strip())]
    while kept and not kept[-1].strip():
        kept.pop()
    line = f"Marked {status or 'changed'} by {(by or 'someone').strip()[:60]} in Staff Access"
    return "\n".join(kept + [line])[-tasks.MAX_NOTES:]


async def _note_field(form_id) -> str | None:
    """The form's note manager field, when it has exactly one (else None: nothing is written)."""
    try:
        notes = [f["id"] for f in await ctdata.form_fields(form_id) if f["type"] == "note"]
    except (ctdata.Unavailable, ValueError):
        return None
    return notes[0] if len(notes) == 1 else None


# ---------------------------------------------------------------- changes made in Connecteam

async def poll_due(weight: int) -> None:
    """Read the boards that are due (each every tasks.form_interval(weight))."""
    if not on() or time.time() < paused_until:
        return
    every = tasks.form_interval(weight)
    async with _guard():
        for b in boards():
            if time.time() - (b["last_poll"] or 0) < every:
                continue
            try:
                await _poll(b)
            except (Paused, Busy):
                break
            except ConnecteamError as err:
                log.info("Task sync: board %s: %s", b["board_id"], err)


async def _list(board_id: str) -> tuple[list[dict], bool, int]:
    """Every task on a board: (tasks, whether that's all of them, pages read). A reply without
    a list of tasks is an error, never an empty board."""
    found, offset = [], 0
    for page_no in range(1, MAX_PAGES + 1):
        data = await _ask("GET", f"/tasks/v1/taskboards/{board_id}/tasks",
                          [("status", "all"), ("limit", str(PAGE)), ("offset", str(offset))])
        page = data.get("tasks")
        if not isinstance(page, list):
            raise ConnecteamError("Connecteam sent an unexpected list of tasks.")
        found += [r for r in page if isinstance(r, dict) and CT_ID_RE.match(str(r.get("id") or ""))]
        nxt = (data.get("_paging") or {}).get("offset")
        if len(page) < PAGE or nxt is None:
            return found, True, page_no
        if not isinstance(nxt, int) or isinstance(nxt, bool) or nxt <= offset:
            return found, False, page_no   # paging that doesn't move on: not all of them
        offset = nxt
    return found, False, MAX_PAGES


async def _poll(b: dict) -> None:
    """Read one board and bring its changes here (call inside _guard())."""
    db, now = _db(), time.time()
    board_id = b["board_id"]
    try:
        remote, complete, pages = await _list(board_id)
    except Rejected as err:
        db.conn.execute("UPDATE task_ct_boards SET last_poll = ?, last_error = ? WHERE board_id = ?",
                        (now, ("That board isn't in Connecteam any more." if err.status == 404
                               else str(err))[:300], board_id))
        db.conn.commit()
        return
    except Paused as err:
        db.conn.execute("UPDATE task_ct_boards SET last_error = ? WHERE board_id = ?",
                        (str(err)[:300], board_id))
        db.conn.commit()
        raise
    ct_time = b["last_ok"] or 0.0
    by_id = {str(r["id"]): r for r in remote}
    for r in remote:
        if r.get("type") == "recurring":
            _log_recurring(board_id, r)
    to_ct, to_staff = _links()
    seen_ids = {r[0] for r in db.conn.execute("SELECT ct_task_id FROM task_ct_seen")}
    # Creates that may have arrived: found by title and assignees, never made twice.
    for t in [dict(r) for r in db.conn.execute(
            "SELECT * FROM tasks WHERE ct_task_id IS NULL AND ct_sync LIKE '%\"creating\"%'")]:
        sync = _sync(t)
        if sync.get("board") != board_id or not sync.get("creating"):
            continue
        base = sync.get("base") or sa_view(t, to_ct)
        match = next((cid for cid, r in by_id.items() if cid not in seen_ids
                      and _key("title", ct_view(r)["title"]) == _key("title", base["title"])
                      and ct_view(r)["users"] == sorted(base["users"])), None)
        if match:
            seen_ids.add(match)
            _seen_ct(match, board_id)
            v = ct_view(by_id[match])
            if not v["users"]:
                v["done"] = base["done"]
            _save(t["id"], ct_task_id=match, ct_board_id=board_id,
                  ct_sync={"seen": v, "base": base}, ct_note=None)
        elif complete:
            _save(t["id"], ct_sync={"board": board_id})   # never arrived: made when next sent
    # Tasks linked here: their changes, or their deletion.
    linked = [dict(r) for r in db.conn.execute(
        "SELECT * FROM tasks WHERE ct_task_id IS NOT NULL AND ct_board_id = ?", (board_id,))]
    if complete and not remote and linked and not await _board_listed(board_id):
        # An empty board that isn't among Connecteam's boards (deleted or archived): its tasks
        # here stay as they are, rather than every one going to the bin.
        db.conn.execute("UPDATE task_ct_boards SET last_poll = ?, last_error = ? WHERE board_id = ?",
                        (now, "That board isn't in Connecteam any more.", board_id))
        db.conn.commit()
        return
    for t in linked:
        r = by_id.get(t["ct_task_id"])
        if r is not None:
            _reconcile(t, ct_view(r), ct_time, to_ct, to_staff)
        elif complete and not (_sync(t).get("seen") or {}).get("archived"):
            # Gone from the whole board (an archived one may simply not be listed).
            if not t["deleted_at"]:
                _bin(t, now, True)
            _gone(t, board_id)
    # New ones: tasks here. The first read takes only open ones, not the board's whole past.
    first = b["last_ok"] is None
    for cid, r in by_id.items():
        if cid in seen_ids:
            continue
        v = ct_view(r)
        if not v["archived"] and not (first and v["done"]):
            _import(b, cid, v, to_ct, to_staff)
        _seen_ct(cid, board_id)
    db.conn.execute("UPDATE task_ct_boards SET last_poll = ?, last_ok = ?, last_error = NULL, "
                    "pages = ? WHERE board_id = ?", (now, now, pages, board_id))
    db.conn.commit()


async def _board_listed(board_id: str) -> bool:
    """Whether a board is still among Connecteam's boards, not archived (one request)."""
    return any(b["id"] == board_id and not b["archived"] for b in await ct_boards())


def _import(b: dict, cid: str, v: dict, to_ct, to_staff) -> None:
    person = next((to_staff[u] for u in v["users"] if u in to_staff), None)
    due_date, due_time = due_parts(v["due"], _zone())
    fields = {"title": v["title"], "notes": v["notes"], "department_id": b["department_id"],
              "assigned_id": person["id"] if person else None,
              "assigned_name": person["name"][:tasks.MAX_NAME] if person else None,
              "due_date": due_date, "due_time": due_time, "priority": "normal",
              "source": "connecteam", "status": "done" if v["done"] else "todo",
              "ct_task_id": cid, "ct_board_id": b["board_id"]}
    db = _db()
    task_id = db.add_task(fields, BY_CT, time.time(), commit=False, note=IN_CT)
    seen = {**v}
    _save(task_id, ct_sync={"seen": seen, "base": sa_view(db.task(task_id), to_ct)})


def _reconcile(t: dict, remote: dict, ct_time: float, to_ct, to_staff) -> None:
    """Bring one task's Connecteam changes here, field by field (the newer wins when both sides
    changed one); what changed here stays waiting to be sent."""
    sync = _sync(t)
    seen, base = sync.get("seen"), sync.get("base")
    if seen is None or base is None:   # adopted after a restore: Connecteam's copy wins
        t = _apply(t, remote, to_staff, IN_CT)
        _save(t["id"], ct_sync={"seen": remote, "base": sa_view(t, to_ct)}, clear_pending=True,
              pending_was=t["ct_pending"])
        return
    if not remote["users"]:
        remote = {**remote, "done": seen["done"]}   # a draft says nothing about done
    sa = sa_view(t, to_ct)
    changes, keep, theirs, ours = {}, {}, [], []
    for f in FIELDS:
        ct_moved = _key(f, remote[f]) != _key(f, seen[f])
        sa_moved = _key(f, sa[f]) != _key(f, base[f])
        if ct_moved and sa_moved and _key(f, sa[f]) != _key(f, remote[f]):
            if (t["ct_pending"] or 0) > ct_time:
                ours.append(f)
                ct_moved = False
            else:
                theirs.append(f)
                sa_moved = False
        if ct_moved:
            changes[f] = remote[f]
        elif sa_moved:
            keep[f] = base[f]
    if changes:
        t = _apply(t, changes, to_staff, f"{IN_CT}, newer than the change here" if theirs else IN_CT)
    if ours:
        _db()._task_event(t["id"], time.time(), {"name": "Staff Access"}, "edited",
                          note="kept: newer than the change in Connecteam")
    new_base = {**sa_view(t, to_ct), **keep}
    _save(t["id"], ct_sync={"seen": remote, "base": new_base},
          clear_pending=not keep, pending_was=t["ct_pending"])


def _apply(t: dict, changes: dict, to_staff, note: str) -> dict:
    """Connecteam's changes, made here as "Someone in Connecteam" (never sent back)."""
    db, now = _db(), time.time()
    edit = {}
    if "title" in changes and changes["title"] != t["title"]:
        edit["title"] = changes["title"]
    if "notes" in changes and _key("notes", changes["notes"]) != _key("notes", t["notes"]):
        edit["notes"] = changes["notes"]
    if "due" in changes:
        due_date, due_time = due_parts(changes["due"], _zone())
        if (due_date, due_time) != (t["due_date"], t["due_time"]):
            edit.update(due_date=due_date, due_time=due_time)
    if "users" in changes:
        person = next((to_staff[u] for u in changes["users"] if u in to_staff), None)
        if (person and person["id"]) != t["assigned_id"]:
            edit.update(assigned_id=person["id"] if person else None,
                        assigned_name=person["name"][:tasks.MAX_NAME] if person else None)
    if edit:
        db.conn.execute(f"UPDATE tasks SET {', '.join(f'{k} = ?' for k in edit)}, updated_at = ? "
                        "WHERE id = ?", (*edit.values(), now, t["id"]))
        db._task_event(t["id"], now, BY_CT, "edited", note=note)
    status = t["status"]
    if "done" in changes:
        status = "done" if changes["done"] else ("todo" if status in ("done", "wont") else status)
    if changes.get("archived") is False and status == "wont":
        status = "todo"
    if status != t["status"]:
        db.conn.execute("UPDATE tasks SET status = ?, status_at = ?, status_by = ?, updated_at = ? "
                        "WHERE id = ?", (status, now, BY_CT["name"], now, t["id"]))
        db._task_event(t["id"], now, BY_CT, "status", t["status"], status, note)
    t = dict(db.task(t["id"]))
    if changes.get("archived") is True and not t["deleted_at"] and t["status"] != "wont":
        _bin(t, now, True, note)
    elif changes.get("archived") is False and t["deleted_at"]:
        _bin(t, now, False, note)
    db.conn.commit()
    return dict(db.task(t["id"]))


def _bin(t: dict, now: float, deleted: bool, note: str = IN_CT) -> None:
    db = _db()
    db.conn.execute("UPDATE tasks SET deleted_at = ?, deleted_by = ?, updated_at = ? WHERE id = ?",
                    (now if deleted else None, BY_CT["name"] if deleted else None, now, t["id"]))
    db._task_event(t["id"], now, BY_CT, "deleted" if deleted else "restored", note=note)
    db.conn.commit()


# ---------------------------------------------------------------- forms that follow a manager field

async def follow_submissions(form: dict, subs: list[dict], staff_by_ct: dict, zone, now: float,
                             read_at: float | None = None) -> int:
    """A followed form's submissions (FORM_LOOKBACK_DAYS of them, older than the form's choosing
    too): one that isn't done becomes a task in the status its manager field maps to, and a
    task's status follows the field's. A status changed here and not yet sent wins when it's
    newer than the field's last change, and a read that began before a task's status was last
    written there (read_at) says nothing about it. Returns how many tasks were made."""
    follow = tasks.follow_of(form)
    db = _db()
    made, new, marked = 0, [], []
    async with _guard():
        for sub in subs:
            sid = str(sub.get("id") or "")[:80] if isinstance(sub, dict) else ""
            if not sid:
                continue
            option = ((sub.get("manager") or {}).get("status") or {}).get(follow["field"]) or {}
            option_id, at = option.get("id") or "", option.get("at") or now
            want = tasks.follow_status(follow, option_id, option.get("name") or "")
            row = db.conn.execute("SELECT * FROM tasks WHERE form_id = ? AND submission_id = ?",
                                  (form["form_id"], sid)).fetchone()
            if row is None:
                if want == "done":
                    continue   # done ones aren't tasks
                # (Submissions seen before following, which only marks them so as not to make
                # tasks of old ones, count: a followed form makes a task of every open one.)
                when = tasks.submitted_time(sub.get("submitted_at"), zone) or now
                fields = tasks.task_from_submission(form, sub, staff_by_ct, when, zone)
                new.append({**fields, "status": "doing" if want == "doing" else "todo",
                            "ct_status": option_id})
                marked.append(sid)
                continue
            t = dict(row)
            fresh = tasks.task_from_submission(form, sub, staff_by_ct, now, zone)
            if (fresh["photos"], fresh["manager_note"]) != (t["photos"], t["manager_note"]):
                db.conn.execute("UPDATE tasks SET photos = ?, manager_note = ? WHERE id = ?",
                                (fresh["photos"], fresh["manager_note"], t["id"]))
            if option_id == (t["ct_status"] or "") or (
                    read_at is not None and (_sync(t).get("wrote") or 0) > read_at):
                continue   # the same, or read before the last write there (the next read tells)
            if t["deleted_at"] or (t["ct_pending"] or 0) > at:
                if not t["deleted_at"]:
                    db._task_event(t["id"], now, {"name": "Staff Access"}, "edited",
                                   note="kept: newer than the change in Connecteam")
                db.conn.execute("UPDATE tasks SET ct_status = ? WHERE id = ?", (option_id, t["id"]))
                continue
            status = "doing" if want == "todo" and t["status"] == "doing" else want
            if status != t["status"]:
                db.conn.execute("UPDATE tasks SET status = ?, status_at = ?, status_by = ?, "
                                "updated_at = ? WHERE id = ?", (status, now, BY_CT["name"], now, t["id"]))
                db._task_event(t["id"], now, BY_CT, "status", t["status"], status, IN_CT)
            db.conn.execute("UPDATE tasks SET ct_status = ?, ct_pending = NULL, ct_note = NULL "
                            "WHERE id = ?", (option_id, t["id"]))
        db.conn.commit()
        if new:
            made = db.add_form_tasks(form["form_id"], marked, new, now)
    return made


# ---------------------------------------------------------------- the dashboard

def poll_weight(forms: list[dict]) -> int:
    """How many requests a round of checks takes, for planning them (tasks.form_interval): one
    for a form's new submissions, two for a followed form (it reads back FORM_LOOKBACK_DAYS,
    a month at a time), and each board's pages when the sync is on."""
    weight = sum(2 if form.get("follow") else 1 for form in forms)
    if on():
        weight += sum(max(1, b["pages"] or 1) for b in boards())
    return weight


def status() -> dict:
    """What the Tasks tab shows about the sync."""
    s = _db().settings()
    paused = time.time() < paused_until
    return {"enabled": s.get("ct_tasks_on") == "1", "connecteam": s.get("connecteam_enabled") == "1",
            "note": s.get("ct_tasks_note") or None,
            "paused": pause_reason if paused else None,
            "retry_at": paused_until if paused else None,
            "boards": boards(), "usage": usage(), "write_cap": WRITE_DAILY_CAP,
            "waiting": _db().conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE ct_pending IS NOT NULL").fetchone()[0]}


def save(enabled: bool, chosen, departments: dict[int, str]) -> None:
    """The owner's choice: on or off, and the boards with their departments. A board kept keeps
    when it was last read; one added starts afresh (its first read takes only open tasks). A board
    may keep the department it has even once that's hidden (its tasks are then seen as a gone
    department's are), so the rest can still be saved."""
    if not isinstance(chosen, list) or len(chosen) > MAX_BOARDS:
        raise tasks.TaskError(f"Pick at most {MAX_BOARDS} boards.")
    rows, ids, had = [], set(), {b["board_id"]: b["department_id"] for b in boards()}
    for b in chosen:
        board_id = str(b.get("board_id") or "") if isinstance(b, dict) else ""
        if not CT_ID_RE.match(board_id) or board_id in ids:
            raise tasks.TaskError("That isn't a Connecteam board.")
        department_id = tasks._id(b.get("department_id"))
        if department_id is not None and department_id not in departments and (
                board_id not in had or had[board_id] != department_id):
            raise tasks.TaskError("That department doesn't exist any more.")
        ids.add(board_id)
        rows.append((board_id, tasks.clean_text(b.get("name"), tasks.MAX_FORM_NAME), department_id))
    db, now = _db(), time.time()
    db.conn.execute(f"DELETE FROM task_ct_boards WHERE board_id NOT IN ({', '.join('?' * len(ids))})",
                    tuple(ids))
    for board_id, name, department_id in rows:
        db.conn.execute("""INSERT INTO task_ct_boards (board_id, name, department_id, picked_at)
                           VALUES (?, ?, ?, ?) ON CONFLICT (board_id) DO UPDATE SET
                           name = excluded.name, department_id = excluded.department_id""",
                        (board_id, name, department_id, now))
    db.conn.commit()
    db.update_settings({"ct_tasks_on": "1" if enabled else "0",
                        **({"ct_tasks_note": ""} if enabled else {})})
    lift_pause()


async def ct_boards() -> list[dict]:
    """Connecteam's boards, to choose from (one request)."""
    data = await _ask("GET", "/tasks/v1/taskboards")
    found = []
    for b in data.get("taskBoards") if isinstance(data.get("taskBoards"), list) else []:
        board_id = str(b.get("id") or "") if isinstance(b, dict) else ""
        if CT_ID_RE.match(board_id):
            found.append({"id": board_id, "name": tasks.clean_text(b.get("name"), tasks.MAX_FORM_NAME)
                          or f"Board {board_id}", "archived": b.get("isArchived") is True})
    return found[:200]


async def test() -> dict:
    """Test: the permissions the Connecteam app has, and one read (the list of boards), as the
    Connections card shows its checks: {ok, checks: [{check, ok, detail}]}."""
    lift_pause()
    checks = []
    try:
        await _ct._access_token(reports=True)
    except ConnecteamError as err:
        return {"ok": False, "checks": [{"check": "Access token", "ok": False, "detail": str(err)}]}
    scopes = _ct.scopes
    if scopes is None:
        checks.append({"check": "Permissions", "ok": True,
                       "detail": "Connecteam didn't say which permissions the app has."})
    else:
        missing = [s for s in SCOPES_NEEDED if s not in scopes]
        checks.append({"check": "Permissions", "ok": not missing, "detail":
                       "quick_tasks.read and quick_tasks.write." if not missing
                       else f"The Connecteam app is missing {', '.join(missing)}."})
        if not missing and SCOPE_DELETE not in scopes:
            checks.append({"check": "Unassigned tasks", "ok": True, "detail": "No "
                           "quick_tasks.delete (not needed): a task nobody is assigned to stays on "
                           "the Connecteam board when it's marked Won't do or binned here."})
        follow = all(s in scopes for s in SCOPES_FOLLOW)
        checks.append({"check": "Forms", "ok": True, "detail": "forms.write: followed forms write "
                       "their status back." if follow else "No forms.write: forms that follow a "
                       "manager field only read it."})
    try:
        found = await ct_boards()
        checks.append({"check": "Reading boards", "ok": True,
                       "detail": f"{len(found)} board{'s' if len(found) != 1 else ''}."})
    except (Paused, Busy, ConnecteamError) as err:
        checks.append({"check": "Reading boards", "ok": False, "detail": str(err)})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
