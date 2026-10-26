"""The documents part's pages in the HR portal, through server.py's extension points: a
person's Documents section, opening a file (documents.view_url), adding one, HR's check (accept
or reject), the recycle bin, legal holds, records past retention, the Needs attention page, the
kinds of document in HR settings, and HR's to-dos.

Every route has an access rule, and each one checks how the viewer sees the person whose file it
is (access.person_relation) before it says anything about a document. One they may not see is
"not here", so nobody learns that a restricted document exists. Every look at a file and every
change goes into the audit log.
"""

import asyncio
import time

from aiohttp import BodyPartReader, web

from . import access, audit, ctapi, delivery, documents, files, folders, gdrive, notify, server
from .access import Refused, rule
from .server import PERSON, RELATION, error, read_json, svc_of, viewer_of

UPLOAD_SECONDS = 180      # a phone on a slow connection may take a while
FORM_EXTRA = 64 * 1024    # the form's other fields, around the file
MAX_PARTS = 8
FIELDS = ("kind", "title", "date")
CT_DOCS_TTL = 600         # Connecteam's list of documents is reused this long (one request)
MAX_REASON = 500
NOT_HERE = server.NOT_FOUND
RESTRICTED_SHOWN = "Restricted document"
UNREADABLE = "That upload couldn't be read. Try again."
RESTRICTED_WARNING = (
    "Restricted files (I-9, Reasonable accommodation) are kept in a “Restricted” folder in "
    "the HR shared drive, which every member of that drive can open. To keep them apart, the "
    "owner picks a second shared drive with only the owner and the HR lead in it, such as "
    "“HR Restricted”, in Home Assistant (Settings, Connections, Google Drive (HR files)).")
DELIVERY_WORDS = {"done": "Sent", "waiting": "Waiting", "attention": "Needs attention",
                  "skipped": "Not sent"}
TODO_WORDS = {"remove_connecteam": "Remove its copy in Connecteam by hand (Connecteam can't "
                                   "remove files for Staff Access)."}
ct_documents = {"at": 0.0, "list": None}


# ---------------------------------------------------------------- helpers

def doc_for(request: web.Request) -> tuple[dict, dict, dict, str | None]:
    """The document a route names, its kind, its person, and how the viewer sees them. Refused
    (not here) when there's no such document."""
    db = svc_of(request).db
    doc = documents.get(db, request.match_info["did"])
    person = db.person(doc["person_id"]) if doc else None
    k = documents.kind(db, doc["kind_id"]) if doc else None
    if doc is None or person is None or k is None or doc["status"] == "gone":
        raise Refused(NOT_HERE, 404)
    return doc, k, person, access.person_relation(db, viewer_of(request), person)


def hr_doc(request: web.Request) -> tuple[dict, dict, dict]:
    """A document an HR admin acts on: in someone else's file (rule 1), else not here."""
    doc, k, person, rel = doc_for(request)
    if rel == "self" and documents.shown_to(rel, doc, k):
        raise Refused(access.NOT_YOURS)
    if rel != "hr":
        raise Refused(NOT_HERE, 404)
    return doc, k, person


def hr_sees(viewer, person_id, doc: dict, k: dict) -> bool:
    """Whether an item about this document goes on an HR admin's lists: anyone else's, and their
    own only where they'd see it as an employee (never their own restricted ones)."""
    if str(person_id) != viewer.id:
        return True
    return documents.shown_to("self", doc, k) and not doc["restricted"]


def deliveries_view(db, doc_id: int) -> list[dict]:
    """The latest work for each copy of a file (Google Drive, Connecteam), for HR."""
    latest = {}
    for d in documents.deliveries_of(db, doc_id):
        if d["target"] in ("drive", "connecteam"):
            latest[d["target"]] = d
    return [{"id": d["id"], "target": d["target"], "word": delivery.TARGET_WORDS[d["target"]],
             "status": d["status"], "status_word": DELIVERY_WORDS.get(d["status"], d["status"]),
             "error": d["error"] if d["status"] != "done" else None, "tries": d["tries"],
             "can_skip": d["status"] == "attention" and d["target"] == "connecteam"}
            for d in latest.values()]


def doc_view(svc, viewer, rel: str | None, doc: dict, k: dict) -> dict:
    """A document as this viewer may see it (plain text; nothing they can't see)."""
    db = svc.db
    mine = doc["added_by"] is not None and str(doc["added_by"]) == viewer.id
    view = {"id": doc["id"], "kind": k["id"], "kind_name": k["name"],
            "title": documents.title_of(doc), "date": doc["doc_date"], "status": doc["status"],
            "status_word": documents.STATUS_WORDS.get(doc["status"], doc["status"]),
            "type": documents.kind_of_file(doc), "size": doc["size"],
            "added_by": doc["added_by_name"], "added_at": round(doc["added_at"]), "mine": mine,
            "url": documents.view_url(doc) if documents.may_open(viewer, rel, doc, k) else None}
    if doc["status"] == "rejected" and (rel in ("hr", "self") or mine):
        view["reason"] = documents.reason_of(doc)
    if rel == "hr":
        owned = documents.owned_by(k)
        view.update(
            restricted=bool(doc["restricted"]), drive_link=doc["drive_link"],
            deliveries=deliveries_view(db, doc["id"]),
            can_check=doc["status"] == "waiting" and not owned and not mine,
            can_delete=doc["status"] in documents.SHOWN and not owned,
            can_restore=doc["status"] == "deleted",
            bin_until=round(doc["deleted_at"] + documents.BIN_DAYS * documents.DAY)
            if doc["deleted_at"] else None)
    return view


def drive_note(svc) -> str | None:
    """Why files can't reach Google Drive yet, for HR (None when they can)."""
    return None if folders.drive_ok(svc.main_settings()) else delivery.NO_GOOGLE


# ---------------------------------------------------------------- the person's Documents section

def section_kinds(db, rel: str | None, restricted: bool = False) -> list[dict]:
    """The kinds the Documents section shows for this relation: not ones another part owns, and
    the restricted ones only on HR's request (restricted=True)."""
    return [k for k in documents.kinds(db) if not documents.owned_by(k)
            and documents.is_restricted(k) == restricted and documents.can_see(rel, k)]


def file_list(svc, viewer, person: dict, rel: str | None, kinds: list[dict]) -> list[dict]:
    by_id = {k["id"]: k for k in kinds}
    return [doc_view(svc, viewer, rel, d, by_id[d["kind_id"]])
            for d in documents.of_person(svc.db, person["id"])
            if d["kind_id"] in by_id and documents.shown_to(rel, d, by_id[d["kind_id"]])]


def addable(kinds: list[dict], rel: str | None, person: dict) -> list[dict]:
    if person["archived"] and rel != "hr":
        return []
    return [{"id": k["id"], "name": k["name"]} for k in kinds if documents.can_upload(rel, k)]


@server.section("documents", 50)
def documents_section(svc, v: access.Viewer, p: dict, rel: str) -> dict | None:
    db = svc.db
    kinds = section_kinds(db, rel)
    if not kinds:
        return None
    docs = file_list(svc, v, p, rel, kinds)
    current = [d for d in docs if d["status"] != "deleted"]
    built = {"title": "Documents",
             "rows": [{"label": "Files", "value": str(len(current)) if current else "None yet"}],
             "documents": docs, "kinds": addable(kinds, rel, p),
             "limit_mb": documents.upload_limit(rel) // 2**20,
             "waiting_note": None if rel == "hr" else "Files you add wait for HR to check them."}
    if rel == "hr":
        settings = svc.main_settings()
        restricted_kinds = section_kinds(db, rel, restricted=True)
        count = sum(1 for d in documents.of_person(db, p["id"]) if d["restricted"])
        due = documents.keep_until(db, p)
        built["hr"] = {
            "hold": documents.hold_of(db, p["id"]),
            "restricted": {"count": count, "kinds": [k["name"] for k in restricted_kinds],
                           "apart": folders.place_drive(settings, folders.RESTRICTED)[1]},
            "archived": bool(p["archived"]),
            "can_move_folder": bool(p["archived"]) and folders.in_employees(db, p["id"]),
            "retention": {"due_at": round(due), "past": due <= time.time()} if due else None,
            "drive": drive_note(svc)}
    return built


@rule("hr", person=True, not_self=True)
async def api_restricted(request: web.Request):
    """A person's restricted documents (I-9, Reasonable accommodation), on HR's request. Every
    look is in the audit log."""
    svc, p, aid, name, ip = server.hr_action(request)
    v = viewer_of(request)
    kinds = section_kinds(svc.db, "hr", restricted=True)
    docs = file_list(svc, v, p, "hr", kinds)
    audit.record(svc.db, "Viewed restricted documents", actor_id=aid, actor=name, target_id=p["id"],
                 target=p["name"], detail=f"{len(docs)} file(s)", ip=ip)
    apart = folders.place_drive(svc.main_settings(), folders.RESTRICTED)[1]
    return web.json_response({"documents": docs, "kinds": addable(kinds, "hr", p),
                              "warning": None if apart else RESTRICTED_WARNING,
                              "limit_mb": documents.upload_limit("hr") // 2**20})


# ---------------------------------------------------------------- opening a file

@rule("signed_in")
async def api_file(request: web.Request):
    """documents.view_url: the file itself, after the role and relationship checks (its part's
    can_open, or the kind's level). Pictures open in the portal; PDFs are always downloads; both
    are sandboxed. Fetched from Drive (and kept here 30 days) when it isn't here any more."""
    svc = svc_of(request)
    db, v = svc.db, viewer_of(request)
    doc, k, person, rel = doc_for(request)
    if not documents.may_open(v, rel, doc, k):
        return error(NOT_HERE, 404)
    data = await documents.load_copy(svc.folder, doc)
    if data is None:
        bridge = svc.google()
        if not doc["drive_id"]:
            return error("This file isn't on this computer any more, and never reached Google "
                         "Drive.", 410)
        if bridge is None:
            return error("This file is kept in Google Drive, which isn't set up right now. Try "
                         "again later.", 503)
        try:
            data = await bridge.download(doc["drive_id"])
        except gdrive.BridgeError as err:
            return error(f"Couldn't get the file from Google Drive: {err.message}", 502)
        if files.MIME.get(files.sniff(data) or "") != doc["mime"]:
            return error("The file in Google Drive was changed, so it isn't shown. HR can open it "
                         "in Google Drive.", 409)
        await documents.cache_copy(db, svc.folder, doc, data)
    audit.record(db, "Opened a restricted document" if doc["restricted"] else "Opened a document",
                 actor_id=v.id, actor=v.name, target_id=person["id"], target=person["name"],
                 detail=f"{k['name']}, {doc['doc_date']}", ip=svc.client_ip(request))
    return files.file_response(data, doc["mime"], documents.file_name(doc))


# ---------------------------------------------------------------- adding a file

async def _read_part(part: BodyPartReader, limit: int, message: str) -> bytes:
    found = bytearray()
    while chunk := await part.read_chunk(256 * 1024):
        found.extend(chunk)
        if len(found) > limit:
            raise files.FileRefused(message)
    return bytes(found)


async def read_upload(request: web.Request, limit: int) -> tuple[dict, bytes | None]:
    """A form with a file (multipart): ({"kind", "title", "date"}, the file's bytes). Raises
    files.FileRefused with a plain message (too big, unreadable, too slow)."""
    too_big = f"That file is too big (more than {limit // 2**20} MB)."
    if request.content_type != "multipart/form-data":
        raise files.FileRefused(UNREADABLE)
    if request.content_length is not None and request.content_length > limit + FORM_EXTRA:
        raise files.FileRefused(too_big)
    fields, data = {}, None
    try:
        async with asyncio.timeout(UPLOAD_SECONDS):
            reader = await request.multipart()
            for _ in range(MAX_PARTS):
                part = await reader.next()
                if part is None:
                    return fields, data
                if not isinstance(part, BodyPartReader):
                    raise files.FileRefused(UNREADABLE)
                if part.name == "file" and data is None:
                    data = await _read_part(part, limit, too_big)
                elif part.name in FIELDS and part.name not in fields:
                    fields[part.name] = (await _read_part(part, 1000, UNREADABLE)).decode("utf-8", "replace")
                else:
                    await part.release()
    except files.FileRefused:
        raise
    except TimeoutError:
        raise files.FileRefused("The upload took too long. Try again on a better connection.") from None
    except Exception:  # noqa: BLE001 - a broken form is just refused
        raise files.FileRefused(UNREADABLE) from None
    raise files.FileRefused(UNREADABLE)   # too many parts


@rule("signed_in", person=True)
async def api_upload(request: web.Request):
    """Add a file to someone's file: a form with kind, title, date and file. HR's go straight
    to Drive; anyone else's wait for HR. Only kinds the viewer may add (never restricted ones,
    unless they're HR; never ones another part owns)."""
    svc = svc_of(request)
    db, v = svc.db, viewer_of(request)
    p, rel = request[PERSON], request[RELATION]
    limit = documents.upload_limit(rel)
    try:
        fields, data = await read_upload(request, limit)
    except files.FileRefused as err:
        return error(str(err), 413 if "too big" in str(err) else 400)
    k = documents.kind(db, fields.get("kind"))
    if k is None or documents.owned_by(k) or not documents.can_upload(rel, k) or \
            (p["archived"] and rel != "hr"):
        return error("Choose a kind of document from the list.")
    if not data:
        return error("Choose a file to add.")
    try:
        day = documents.clean_date(fields.get("date"), db)
        clean, mime = await files.clean_upload(data, limit)
        doc = await documents.add(svc, p, k, clean, mime, documents.clean_title(fields.get("title"), k["name"]),
                                  day, v, rel)
    except ValueError as err:   # files.FileRefused too
        return error(str(err))
    waiting = doc["status"] == "waiting"
    audit.record(db, "Added a document" + (" (waiting for HR)" if waiting else ""), actor_id=v.id,
                 actor=v.name, target_id=p["id"], target=p["name"], detail=k["name"],
                 ip=svc.client_ip(request))
    if waiting:
        for admin in db.with_role(access.HR_ADMIN):
            if admin not in (v.id, p["id"]):
                notify.notify(admin, "document_waiting", db=db, link=f"#/people/{p['id']}",
                              title=f"A document for {p['name']} is waiting for HR to check it.")
    return web.json_response({
        "ok": True, "document": doc_view(svc, v, rel, doc, k),
        "message": "Added. HR checks it before it's kept." if waiting else
        "Added. It's on its way to Google Drive." if folders.drive_ok(svc.main_settings()) else
        "Added. It's kept here until Google Drive is set up."})


# ---------------------------------------------------------------- HR's check, delete and restore

@rule("hr")
async def api_accept(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    doc, k, person = hr_doc(request)
    if documents.owned_by(k):
        return error("Check this in its own section.", 409)
    if doc["status"] != "waiting":
        return error("It isn't waiting for HR any more.", 409)
    access.not_own_work(v, doc["added_by"])
    documents.activate(svc.db, doc, svc.main_settings(), v.id)
    audit.record(svc.db, "Accepted a document", actor_id=v.id, actor=v.name, target_id=person["id"],
                 target=person["name"], detail=k["name"], ip=svc.client_ip(request))
    return web.json_response({"ok": True, "message": "Accepted. It's on its way to Google Drive."})


@rule("hr")
async def api_reject(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    doc, k, person = hr_doc(request)
    data = await read_json(request)
    reason = " ".join(str(data.get("reason") or "").split())[:MAX_REASON]
    if documents.owned_by(k):
        return error("Check this in its own section.", 409)
    if doc["status"] != "waiting":
        return error("It isn't waiting for HR any more.", 409)
    if not reason:
        return error("Say why, so whoever added it knows what to do.")
    access.not_own_work(v, doc["added_by"])
    documents.reject(svc.db, doc, reason, v.id)
    audit.record(svc.db, "Didn't accept a document", actor_id=v.id, actor=v.name,
                 target_id=person["id"], target=person["name"], detail=k["name"],
                 ip=svc.client_ip(request))
    for pid in {str(doc["added_by"] or ""), str(person["id"])} - {"", v.id}:
        who = svc.db.person(pid)
        rel = access.person_relation(svc.db, access.viewer_for(svc.db, who), person) if who else None
        if rel and documents.can_see(rel, k):
            notify.notify(pid, "document_rejected", db=svc.db, link=f"#/people/{person['id']}",
                          once=f"rejected-{doc['id']}")
    return web.json_response({"ok": True, "message": "Not accepted. Whoever added it is told why."})


@rule("hr", sensitive=True)
async def api_delete(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    doc, k, person = hr_doc(request)
    if documents.owned_by(k):
        return error("Delete this in its own section.", 409)
    if doc["status"] not in documents.SHOWN:
        return error("It's already in the recycle bin.", 409)
    try:
        documents.delete(svc.db, doc, v.id)
    except ValueError as err:
        return error(str(err), 409)
    audit.record(svc.db, "Deleted a document", actor_id=v.id, actor=v.name, target_id=person["id"],
                 target=person["name"], detail=k["name"], ip=svc.client_ip(request))
    message = "In the recycle bin for 30 days, then it's removed."
    if doc["ct_copied_at"]:
        message += " Its copy in Connecteam has to be removed there by hand: see Needs attention."
    return web.json_response({"ok": True, "message": message})


@rule("hr")
async def api_restore(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    doc, k, person = hr_doc(request)
    if doc["status"] != "deleted":
        return error("It isn't in the recycle bin.", 409)
    documents.restore(svc.db, doc, svc.main_settings())
    audit.record(svc.db, "Restored a document", actor_id=v.id, actor=v.name, target_id=person["id"],
                 target=person["name"], detail=k["name"], ip=svc.client_ip(request))
    return web.json_response({"ok": True, "message": "Restored."})


# ---------------------------------------------------------------- legal hold, retention, folders

@rule("hr", person=True, not_self=True, sensitive=True)
async def api_legal_hold(request: web.Request):
    svc, p, aid, name, ip = server.hr_action(request)
    data = await read_json(request)
    on = data.get("on") is True
    reason = " ".join(str(data.get("reason") or "").split())[:300]
    if on and not reason:
        return error("Say why (for example, the case or request it's for).")
    if not on and not documents.on_hold(svc.db, p["id"]):
        return web.json_response({"ok": True, "message": "There's no legal hold on this file."})
    documents.set_hold(svc.db, p["id"], on, reason, aid, name)
    audit.record(svc.db, "Put a legal hold on a file" if on else "Lifted a legal hold", actor_id=aid,
                 actor=name, target_id=p["id"], target=p["name"], ip=ip)
    return web.json_response({"ok": True, "message": "Legal hold on: nothing in this file can be "
                              "deleted." if on else "Legal hold lifted."})


@rule("hr", person=True, not_self=True, sensitive=True)
async def api_delete_all(request: web.Request):
    """Records past retention: everything in the person's file into the recycle bin."""
    svc, p, aid, name, ip = server.hr_action(request)
    due = documents.keep_until(svc.db, p)
    if due is None or due > time.time():
        return error("Their records are kept until the retention period is over.", 409)
    try:
        count = documents.delete_all(svc.db, p["id"], aid)
    except ValueError as err:
        return error(str(err), 409)
    audit.record(svc.db, "Deleted a former employee's files (past retention)", actor_id=aid,
                 actor=name, target_id=p["id"], target=p["name"], detail=f"{count} file(s)", ip=ip)
    return web.json_response({"ok": True, "message": f"{count} file{'s' if count != 1 else ''} in the "
                              "recycle bin for 30 days, then removed."})


@rule("hr", person=True, not_self=True)
async def api_move_folder(request: web.Request):
    """HR confirmed they've left: their Drive folder goes into Former employees."""
    svc, p, aid, name, ip = server.hr_action(request)
    if not p["archived"]:
        return error("Only former employees' folders move to Former employees.", 409)
    if not folders.in_employees(svc.db, p["id"]):
        return error("They have no Drive folder under Employees.", 409)
    documents.queue(svc.db, p["id"], "former")
    audit.record(svc.db, "Moved a Drive folder to Former employees", actor_id=aid, actor=name,
                 target_id=p["id"], target=p["name"], ip=ip)
    return web.json_response({"ok": True, "message": "Their folder is moved to Former employees."})


# ---------------------------------------------------------------- Needs attention (HR)

def _item(svc, viewer, row: dict) -> dict | None:
    """A piece of work on HR's Needs attention list (None: not for this HR admin's eyes)."""
    db = svc.db
    doc = documents.get(db, row["document_id"]) if row["document_id"] else None
    k = documents.kind(db, doc["kind_id"]) if doc else None
    if doc and k and not hr_sees(viewer, row["person_id"], doc, k):
        return None
    title = None
    if doc:
        title = RESTRICTED_SHOWN if doc["restricted"] else f"{k['name'] if k else 'Document'}: " \
                                                           f"{documents.title_of(doc)}"
    return {"id": row["id"], "person_id": row["person_id"], "name": svc.person_name(row["person_id"]),
            "target": row["target"], "word": delivery.TARGET_WORDS.get(row["target"], row["target"]),
            "title": title, "error": row["error"], "tries": row["tries"],
            "due_at": round(row["due_at"]), "can_skip": row["target"] == "connecteam"}


@rule("hr")
async def api_attention(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    db = svc.db
    rows = lambda status: [dict(r) for r in db.conn.execute(   # noqa: E731
        "SELECT * FROM deliveries WHERE status = ? ORDER BY due_at, id LIMIT 200", (status,))]
    stuck = [i for i in (_item(svc, v, r) for r in rows("attention")) if i]
    waiting = [i for i in (_item(svc, v, r) for r in rows("waiting")) if i]
    checks = {}
    for d in db.conn.execute("SELECT * FROM documents WHERE status = 'waiting' ORDER BY added_at"):
        d = dict(d)
        k = documents.kind(db, d["kind_id"])
        if k and not documents.owned_by(k) and str(d["person_id"]) != v.id:
            entry = checks.setdefault(d["person_id"], {"person_id": d["person_id"], "count": 0,
                                                       "name": svc.person_name(d["person_id"])})
            entry["count"] += 1
    todos = []
    for t in db.conn.execute("SELECT * FROM doc_todos WHERE done_at IS NULL ORDER BY id"):
        doc = documents.get(db, t["document_id"])
        k = documents.kind(db, doc["kind_id"]) if doc else None
        if doc and k and hr_sees(v, t["person_id"], doc, k):
            todos.append({"id": t["id"], "person_id": t["person_id"],
                          "name": svc.person_name(t["person_id"]),
                          "title": f"{k['name']}: {documents.title_of(doc)}",
                          "what": TODO_WORDS.get(t["what"], t["what"]), "created_at": round(t["created_at"])})
    holds = [{"person_id": r["person_id"], "name": svc.person_name(r["person_id"]),
              "since": round(r["set_at"]), "by": r["set_by_name"]}
             for r in db.conn.execute("SELECT * FROM legal_holds ORDER BY set_at")
             if r["person_id"] != v.id]
    retention = [{**r, "left_at": round(r["left_at"]), "due_at": round(r["due_at"])}
                 for r in documents.past_retention(db)]
    return web.json_response({"stuck": stuck, "waiting": waiting, "checks": list(checks.values()),
                              "todos": todos, "holds": holds, "retention": retention,
                              "years": documents.retention_years(db), "drive": drive_note(svc)})


@rule("hr")
async def api_retry(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    did = int(request.match_info["dlv"])
    row = svc.db.conn.execute("SELECT * FROM deliveries WHERE id = ?", (did,)).fetchone()
    if not row or not documents.retry(svc.db, did):
        return error(NOT_HERE, 404)
    audit.record(svc.db, "Tried an upload again", actor_id=v.id, actor=v.name,
                 target_id=row["person_id"], target=svc.person_name(row["person_id"]),
                 detail=delivery.TARGET_WORDS.get(row["target"]), ip=svc.client_ip(request))
    return web.json_response({"ok": True, "message": "It will be tried again now."})


@rule("hr")
async def api_skip(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    did = int(request.match_info["dlv"])
    row = svc.db.conn.execute("SELECT * FROM deliveries WHERE id = ?", (did,)).fetchone()
    if not row or not documents.skip(svc.db, did):
        return error(NOT_HERE, 404)
    audit.record(svc.db, "Chose not to copy a file to Connecteam", actor_id=v.id, actor=v.name,
                 target_id=row["person_id"], target=svc.person_name(row["person_id"]),
                 ip=svc.client_ip(request))
    return web.json_response({"ok": True, "message": "Not copied. Google Drive keeps the file."})


@rule("hr")
async def api_todo_done(request: web.Request):
    svc, v = svc_of(request), viewer_of(request)
    tid = int(request.match_info["tid"])
    row = svc.db.conn.execute("SELECT * FROM doc_todos WHERE id = ? AND done_at IS NULL", (tid,)).fetchone()
    if not row:
        return error(NOT_HERE, 404)
    svc.db.conn.execute("UPDATE doc_todos SET done_at = ?, done_by = ? WHERE id = ?",
                        (time.time(), v.id, tid))
    svc.db.conn.commit()
    audit.record(svc.db, "Removed a file's copy in Connecteam", actor_id=v.id, actor=v.name,
                 target_id=row["person_id"], target=svc.person_name(row["person_id"]),
                 ip=svc.client_ip(request))
    return web.json_response({"ok": True})


@server.todo
def document_todos(svc, v: access.Viewer) -> list[dict]:
    if not v.is_hr:
        return []
    db, found = svc.db, []
    count = lambda sql, *args: db.conn.execute(sql, args).fetchone()[0]   # noqa: E731
    plural = lambda n, one, many: one if n == 1 else many                  # noqa: E731
    stuck = count("SELECT COUNT(*) FROM deliveries WHERE status = 'attention'")
    if stuck:
        found.append({"text": f"{stuck} {plural(stuck, 'upload needs', 'uploads need')} attention.",
                      "link": "#/attention"})
    checks = sum(1 for d in db.conn.execute(
        "SELECT d.person_id, k.key FROM documents d JOIN doc_kinds k ON k.id = d.kind_id WHERE "
        "d.status = 'waiting' AND d.person_id != ?", (v.id,))
        if not documents.OWNED.get(d[1] or ""))
    if checks:
        found.append({"text": f"{checks} {plural(checks, 'document is', 'documents are')} waiting "
                              "for HR to check.", "link": "#/attention"})
    todos = count("SELECT COUNT(*) FROM doc_todos WHERE done_at IS NULL")
    if todos:
        found.append({"text": f"{todos} deleted {plural(todos, 'file needs its copy', 'files need their copies')} "
                              "removed in Connecteam by hand.", "link": "#/attention"})
    due = len(documents.past_retention(db))
    if due:
        found.append({"text": f"{due} former {plural(due, 'employee', 'employees')}' records are past "
                              "the time they're kept. Review and delete them.", "link": "#/attention"})
    waiting = count("SELECT COUNT(*) FROM deliveries WHERE status = 'waiting' AND code = 'not_set_up'")
    if waiting:
        found.append({"text": f"{waiting} {plural(waiting, 'file is', 'files are')} waiting for Google "
                              "Drive to be set up (the owner does it in Home Assistant).",
                      "link": "#/attention"})
    return found


# ---------------------------------------------------------------- kinds (HR settings)

@server.settings_section("documents", 50)
def documents_settings(svc, v: access.Viewer) -> dict:
    db = svc.db
    settings = svc.main_settings()
    counts = dict(db.conn.execute("SELECT kind_id, COUNT(*) FROM documents WHERE status IN "
                                  "('waiting', 'active', 'rejected') GROUP BY kind_id").fetchall())
    apart = folders.place_drive(settings, folders.RESTRICTED)[1]
    return {"kinds": [{**k, "owned": documents.owned_by(k), "restricted": documents.is_restricted(k),
                       "fixed": sorted(documents.fixed_fields(k)), "count": counts.get(k["id"], 0)}
                      for k in documents.kinds(db)],
            "levels": access.LEVELS, "uploaders": documents.UPLOADERS,
            "copies_on": documents.copies_on(settings), "drive": drive_note(svc),
            "restricted_warning": None if apart else RESTRICTED_WARNING}


async def _kind_change(request: web.Request, current: dict | None):
    svc, v = svc_of(request), viewer_of(request)
    data = await read_json(request)
    try:
        values = documents.clean_kind(data, current, documents.copies_on(svc.main_settings()))
        if current is None:
            kid = documents.add_kind(svc.db, values)
        else:
            kid = current["id"]
            documents.update_kind(svc.db, kid, values)
    except ValueError as err:
        return error(str(err))
    audit.record(svc.db, "Added a kind of document" if current is None else "Changed a kind of document",
                 actor_id=v.id, actor=v.name, detail=(values.get("name") or current["name"])[:60] +
                 (f" ({', '.join(sorted(values))})" if current else ""), ip=svc.client_ip(request),
                 security=True)
    return web.json_response({"ok": True, "id": kid})


@rule("hr", sensitive=True)
async def api_add_kind(request: web.Request):
    return await _kind_change(request, None)


@rule("hr", sensitive=True)
async def api_change_kind(request: web.Request):
    current = documents.kind(svc_of(request).db, request.match_info["kid"])
    if current is None:
        return error(NOT_HERE, 404)
    return await _kind_change(request, current)


@rule("hr")
async def api_ct_documents(request: web.Request):
    """Connecteam's documents, to choose which one a kind is copied to (one request, reused for
    10 minutes)."""
    svc = svc_of(request)
    if not documents.copies_on(svc.main_settings()):
        return error(documents.COPIES_OFF, 409)
    if ct_documents["list"] is None or time.time() - ct_documents["at"] > CT_DOCS_TTL:
        try:
            found = await ctapi.lane().list_documents()
        except ctapi.CTError as err:
            return error(err.message, 502)
        ct_documents.update(at=time.time(), list=found)
    return web.json_response({"documents": ct_documents["list"]})


# ---------------------------------------------------------------- routes

@server.add_routes
def routes(app: web.Application) -> None:
    r = app.router
    pid = r"/api/people/{pid:[A-Za-z0-9_-]{1,40}}"
    did = r"/api/documents/{did:\d{1,12}}"
    r.add_get(did + "/file", api_file)
    r.add_post(pid + "/documents", api_upload)
    r.add_get(pid + "/documents/restricted", api_restricted)
    r.add_post(pid + "/documents/delete-all", api_delete_all)
    r.add_put(pid + "/legal-hold", api_legal_hold)
    r.add_post(pid + "/drive-folder/former", api_move_folder)
    r.add_post(did + "/accept", api_accept)
    r.add_post(did + "/reject", api_reject)
    r.add_delete(did, api_delete)
    r.add_post(did + "/restore", api_restore)
    r.add_get("/api/documents/attention", api_attention)
    r.add_post(r"/api/deliveries/{dlv:\d{1,12}}/retry", api_retry)
    r.add_post(r"/api/deliveries/{dlv:\d{1,12}}/skip", api_skip)
    r.add_post(r"/api/documents/todos/{tid:\d{1,12}}/done", api_todo_done)
    r.add_post("/api/documents/kinds", api_add_kind)
    r.add_put(r"/api/documents/kinds/{kid:\d{1,12}}", api_change_kind)
    r.add_get("/api/documents/connecteam", api_ct_documents)
