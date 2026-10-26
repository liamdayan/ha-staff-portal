"""The delivery queue: work for Google Drive and Connecteam that must happen even if they're
down for a while. Each piece of work is a row in deliveries (documents.py), so it survives
restarts and add-on updates:

  drive       a file to the person's folder in Drive (folders.py), tagged with its upload_id,
              so a retry after a timeout never makes a second copy;
  connecteam  its copy on the person's Connecteam document (hr/ctapi.py, HR's request lane);
  trash       a file out of the recycle bin into the shared drive's trash;
  rename      a person's folders, after their name changed in Connecteam;
  former      a person's folders into Former employees (HR confirmed they've left);
  pdf         a PDF another part asked for while Google couldn't make it (documents.queue_pdf):
              its part writes the HTML again (register_pdf_maker), Google Drive makes the PDF,
              and it's kept like any file the portal makes (documents.add_generated).

One piece at a time, in the background. A failure is tried again after 1 minute, 5, 30, 2 hours,
6 hours, then daily; after 8 tries it "needs attention" (HR's list, with Retry). Some things wait
without using up a try: Google Drive not set up yet, the script needing a new version,
Connecteam's lane being full or paused, or Connecteam's copies stopped until the owner fixes the
HR connection. Problems only a person can fix (say, someone not assigned the document's pack in
Connecteam) need attention at once, in plain words.
"""

import asyncio
import logging
import time
import weakref
from datetime import date

from . import audit, ctapi, documents, files, folders, gdrive, notify

log = logging.getLogger("staff_portal")

MAX_TRIES = 8
WAIT_SETUP = 600        # Google Drive not set up, or its script out of date: look again then
WAIT_BUSY = 300         # Connecteam's lane full or paused
AT_ONCE = 5             # rows taken per look (one at a time)
NO_GOOGLE = ("Google Drive isn't set up yet, so files wait here (encrypted). The owner connects "
             "it in Home Assistant: Settings, Connections, Google Drive (HR files).")
LOST = "The file isn't on this computer any more, so it can't be sent. Add it again."
COPY_OFF = "Not copied: the Connecteam documents copy is off."
NO_MAKER = "The part of the HR portal that makes this PDF isn't running, so it waits."
TARGET_WORDS = {"drive": "Google Drive", "connecteam": "Connecteam", "trash": "Drive's trash",
                "rename": "Renaming a Drive folder", "former": "Moving a Drive folder",
                "pdf": "Making a PDF"}
CT_SETUP = ("stopped", "not_set_up")   # the HR connection itself: the owner fixes it, then it goes

notify.register_kind("upload_stuck", "An upload to Google Drive or Connecteam needs attention.")


class Wait(Exception):
    """Not now, and not a failed try."""

    def __init__(self, message: str, seconds: float, code: str = "wait"):
        super().__init__(message)
        self.message, self.seconds, self.code = message, seconds, code


class Attention(Exception):
    """Only a person can fix this: on HR's Needs attention list straight away."""

    def __init__(self, message: str, code: str = "attention"):
        super().__init__(message)
        self.message, self.code = message, code


class Skip(Exception):
    """Nothing to do after all (the copy is off, the file was deleted)."""


# ---------------------------------------------------------------- the worker

_workers: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def kick(svc) -> None:
    """Look for due work now, in the background (one worker per running HR portal)."""
    if not svc.running:
        return
    state = _workers.setdefault(svc, {"task": None, "again": False})
    if state["task"] is not None and not state["task"].done():
        state["again"] = True
        return
    state["again"] = False
    state["task"] = asyncio.get_running_loop().create_task(_work(svc, state))


async def _work(svc, state) -> None:
    try:
        while svc.running:
            if not await run_due(svc) and not state["again"]:
                break
            state["again"] = False
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001 - the queue is looked at again next tick
        log.warning("HR portal: the delivery queue failed: %r", err)


async def settle(svc) -> None:
    """Wait for the worker to finish what's due (tests, and stopping)."""
    state = _workers.get(svc)
    while state and state["task"] is not None and not state["task"].done():
        await asyncio.shield(state["task"])


async def stop(svc) -> None:
    state = _workers.pop(svc, None)
    if state and state["task"] is not None and not state["task"].done():
        state["task"].cancel()
        try:
            await state["task"]
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - stopping regardless
            pass


async def tick(svc, now: float) -> None:
    """Every half minute: start the worker when something is due."""
    if svc.db.conn.execute("SELECT 1 FROM deliveries WHERE status = 'waiting' AND due_at <= ? "
                           "LIMIT 1", (now,)).fetchone():
        kick(svc)


async def run_due(svc, now: float | None = None, limit: int = AT_ONCE) -> int:
    """Do the work that's due, one piece at a time. Returns how many were looked at."""
    now = time.time() if now is None else now
    rows = [dict(r) for r in svc.db.conn.execute(
        "SELECT * FROM deliveries WHERE status = 'waiting' AND due_at <= ? ORDER BY due_at, id "
        "LIMIT ?", (now, limit))]
    for row in rows:
        if not svc.running:
            break
        await deliver(svc, row, now)
    return len(rows)


# ---------------------------------------------------------------- one piece of work

async def deliver(svc, row: dict, now: float | None = None) -> str:
    """Do one piece of work and record how it went. Returns its new status."""
    db = svc.db
    now = time.time() if now is None else now
    work = {"drive": _drive, "connecteam": _connecteam, "trash": _trash, "rename": _rename,
            "former": _former, "pdf": _pdf}.get(row["target"])
    try:
        if work is None:
            raise Skip("Unknown work")
        await work(svc, row)
    except Skip as err:
        _set(db, row, "skipped", error=str(err))
        _release(db, row)
        return "skipped"
    except Wait as err:
        _set(db, row, "waiting", due_at=now + err.seconds, error=err.message, code=err.code)
        return "waiting"
    except Attention as err:
        return _stuck(svc, row, err.message, err.code, row["tries"] + 1)
    except gdrive.BridgeError as err:
        if err.code in ("auth", "outdated"):
            _set(db, row, "waiting", due_at=now + WAIT_SETUP, error=err.message, code=err.code)
            return "waiting"
        if err.code == "too_big":
            return _stuck(svc, row, err.message, err.code, row["tries"] + 1)
        if err.code in ("not_allowed", "bad_reply"):
            await _heal(svc)
        return _failed(svc, row, err.message, err.code, now)
    except ctapi.CTError as err:
        if err.budget:
            _set(db, row, "waiting", due_at=now + WAIT_BUSY, error=err.message, code=err.code)
            return "waiting"
        if err.code == "off":
            _set(db, row, "skipped", error=COPY_OFF)
            _release(db, row)
            return "skipped"
        if err.code in CT_SETUP or (err.code == "not_allowed" and _ct_stopped()):
            _set(db, row, "waiting", due_at=now + WAIT_SETUP, error=err.message, code=err.code)
            return "waiting"
        if not err.retry:
            return _stuck(svc, row, err.message, err.code, row["tries"] + 1)
        return _failed(svc, row, err.message, err.code, now)
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001 - tried again later like any failure
        log.warning("HR portal: delivery %s (%s) failed: %r", row["id"], row["target"], err)
        return _failed(svc, row, "Something went wrong sending it. It will be tried again.",
                       "error", now)
    _set(db, row, "done", done_at=time.time(), error=None, code=None)
    _release(db, row)
    return "done"


def _ct_stopped() -> bool:
    """Whether Connecteam refused the HR connection's copies as a whole (keys or permissions)."""
    return bool(ctapi.default is not None and ctapi.default.stopped.get("documents"))


def _release(db, row: dict) -> None:
    if row["document_id"] and row["target"] in ("drive", "connecteam"):
        documents.release_if_done(db, row["document_id"])


def _set(db, row: dict, status: str, **fields) -> str:
    fields["status"] = status
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.conn.execute(f"UPDATE deliveries SET {sets} WHERE id = ?", (*fields.values(), row["id"]))
    db.conn.commit()
    return status


def _failed(svc, row: dict, message: str, code: str, now: float) -> str:
    tries = row["tries"] + 1
    if tries >= MAX_TRIES:
        return _stuck(svc, row, message, code, tries)
    _set(svc.db, row, "waiting", tries=tries, due_at=now + notify.retry_after(tries), error=message,
         code=code)
    return "waiting"


def _stuck(svc, row: dict, message: str, code: str, tries: int) -> str:
    db = svc.db
    _set(db, row, "attention", tries=tries, error=message, code=code)
    person = db.person(row["person_id"])
    audit.record(db, f"{TARGET_WORDS.get(row['target'], 'An upload')}: needs attention",
                 target_id=row["person_id"], target=person["name"] if person else None,
                 detail=message)
    for admin in db.with_role("hr_admin"):
        notify.notify(admin, "upload_stuck", db=db, link="#/attention",
                      once=f"delivery-{row['id']}-{tries}")
    return "attention"


async def _heal(svc) -> None:
    """Drive refused a folder or file: the script may have been deployed afresh (it forgets which
    drives it may use), or a folder was deleted or moved by hand. Tell the script the drives
    again, and find the folders again next time."""
    bridge = svc.google()
    wanted = gdrive.drive_ids(svc.main_settings())
    folders.forget(svc.db)
    if bridge is None or not wanted:
        return
    try:
        info = await bridge.ping()
        if info["allowed"] != wanted:
            await bridge.setup(wanted)
    except gdrive.BridgeError as err:
        log.info("HR portal: checking the Google Drive script: %s", err)


def _bridge(svc):
    bridge = svc.google()
    if bridge is None or not svc.main_settings().get("hr_google_drive"):
        raise Wait(NO_GOOGLE, WAIT_SETUP, "not_set_up")
    return bridge


def _document(svc, row: dict) -> dict:
    doc = documents.get(svc.db, row["document_id"])
    if doc is None:
        raise Skip("The document is gone")
    return doc


async def _drive(svc, row: dict) -> None:
    db = svc.db
    doc = _document(svc, row)
    if doc["status"] not in ("active", "deleted"):
        raise Skip("Not accepted")
    if doc["drive_id"]:
        return   # already there
    bridge = _bridge(svc)
    data = await documents.load_copy(svc.folder, doc)
    if data is None:
        raise Attention(LOST, "lost")
    k = documents.kind(db, doc["kind_id"]) or {"folder": "Documents"}
    person = db.person(doc["person_id"]) or {"id": doc["person_id"], "name": doc["person_id"]}
    settings = svc.main_settings()
    place = folders.RESTRICTED if doc["restricted"] else folders.MAIN
    parent = await folders.subfolder(db, bridge, settings, person, place, k["folder"])
    result = await bridge.upload(parent, documents.file_name(doc), doc["mime"], data,
                                 {"upload_id": doc["upload_id"], folders.TAG: "document",
                                  "ct_user": str(doc["person_id"])})
    db.conn.execute("UPDATE documents SET drive_id = ?, drive_link = ? WHERE id = ?",
                    (result["id"], result["webViewLink"] or None, doc["id"]))
    db.conn.commit()


async def _connecteam(svc, row: dict) -> None:
    db = svc.db
    doc = _document(svc, row)
    if doc["status"] != "active" or doc["restricted"] or not doc["ct_document"]:
        raise Skip("Not copied")
    if doc["ct_copied_at"]:
        return
    if not documents.copies_on(svc.main_settings()):
        raise Skip(COPY_OFF)
    data = await documents.load_copy(svc.folder, doc)
    if data is None:
        raise Attention(LOST, "lost")
    expires = date.fromisoformat(doc["ct_expires"]) if doc["ct_expires"] else None
    try:
        await ctapi.lane().upload_certificate(doc["person_id"], int(doc["ct_document"]),
                                              [(documents.file_name(doc), doc["mime"], data)], expires)
    except ValueError as err:
        raise Attention(str(err), "bad_request") from None
    db.conn.execute("UPDATE documents SET ct_copied_at = ? WHERE id = ?", (time.time(), doc["id"]))
    db.conn.commit()


async def _trash(svc, row: dict) -> None:
    doc = _document(svc, row)
    if not doc["drive_id"] or doc["status"] != "gone":
        raise Skip("Nothing to move to the trash")
    await _bridge(svc).trash(doc["drive_id"])


def _person(svc, row: dict) -> dict:
    person = svc.db.person(row["person_id"])
    if person is None:
        raise Skip("No such person")
    return person


async def _rename(svc, row: dict) -> None:
    person = _person(svc, row)
    await folders.rename_person(svc.db, _bridge(svc), svc.main_settings(), person)


async def _former(svc, row: dict) -> None:
    person = _person(svc, row)
    await folders.move_to_former(svc.db, _bridge(svc), svc.main_settings(), person)


async def _pdf(svc, row: dict) -> None:
    db = svc.db
    doc = documents.get(db, row["document_id"]) if row["document_id"] else None
    found = documents.PDF_MAKERS.get(row["kind_key"])
    if found is None:
        raise Wait(NO_MAKER, WAIT_SETUP, "no_maker")
    maker, done = found
    if doc is None:
        _bridge(svc)   # nothing is asked of the part until Google can make it
        try:
            made = await documents.run_maker(maker, svc, row)
            if not made:
                raise Skip("Nothing to make any more")
            person, title, html = made[:3]
            day = made[3] if len(made) > 3 else notify.local_day(db, row["created_at"])
            try:
                data = await documents.make_pdf(svc, html)
            except documents.PdfUnavailable as err:
                if err.code == "not_set_up":
                    raise Wait(NO_GOOGLE, WAIT_SETUP, "not_set_up") from None
                raise gdrive.BridgeError(err.code, err.message) from None
            # Tagged with this row, so a retry after a crash finds it instead of adding another.
            doc = await documents.add_generated(svc, person, row["kind_key"], data, files.MIME["pdf"],
                                                title, day, upload_id=f"pdf-{row['id']}")
        except ValueError as err:   # the part's HTML or record isn't right: a person looks
            raise Attention(str(err), "bad_request") from None
        db.conn.execute("UPDATE deliveries SET document_id = ? WHERE id = ?", (doc["id"], row["id"]))
        db.conn.commit()
    if done is not None:
        await documents.run_maker(done, svc, row, doc)
