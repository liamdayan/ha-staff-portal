"""Employee documents in the HR portal: the kinds of document and who sees each, the files
themselves, and what happens to them over time.

Kinds (HR settings): a name, who can see it (access.LEVELS), its Drive subfolder, who may add one
(HR only; managers and directors too; or the employee too: anyone but HR waits for HR to check
it), and whether it's copied to Connecteam (only while the owner has the Connecteam documents
copy on). The defaults follow the owner's decisions. "I-9" and "Reasonable accommodation" are
restricted: HR admins only (nobody else even learns one exists), stored apart (folders.py), every
look logged, never copied to Connecteam. Whether a kind is restricted is fixed when it's made,
because its files are stored apart.

A file's life:
- added: its type checked from its bytes, pictures re-encoded without metadata, PDFs byte for
  byte (files.py), then kept encrypted on this computer (DATA_DIR/hr/files);
- waiting: added by someone other than HR, it waits for HR to accept or reject it. A rejected
  one never reaches Drive and goes after 30 days;
- active: on its way to Google Drive (and Connecteam where that's on) through the delivery
  queue (delivery.py), then kept here 30 more days for quick viewing (Drive has the original);
- deleted: HR deleted it (a fresh code first). It's in the recycle bin for 30 days and can be
  restored, then it's removed here and moved to the shared drive's trash. Where it was copied
  to Connecteam, HR gets a to-do to remove it there (Connecteam's API can't). The audit line
  stays. A legal hold on the person stops every deletion.
- Records of people who left are listed for HR after the retention period (5 years, an HR
  setting), never deleted by themselves.

Titles and reasons are encrypted; dates, kinds and statuses stay readable so lists sort.
"""

import asyncio
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
from datetime import date

from . import access, crypto, files, folders, hrdb, notify

DAY = 86400
BIN_DAYS = 30            # deleted files can be restored for this long
CACHE_DAYS = 30          # a delivered file stays here this long for quick viewing
REJECTED_DAYS = 30       # a rejected upload goes after this long
MAX_KINDS = 50
FILE_PURPOSE = "file.document"
CERTIFICATES = "certificates"
RESTRICTED = "restricted"
UPLOADERS = {"hr": "HR admins only",
             "managers": "Their managers and directors too (HR checks it)",
             "employee": "The employee too (HR checks it)"}
UPLOAD_RELATIONS = {"hr": {"hr"}, "managers": {"manager", "director", "hr"},
                    "employee": {"self", "manager", "director", "hr"}}
STATUS_WORDS = {"waiting": "Waiting for HR", "active": "Saved", "rejected": "Not accepted",
                "deleted": "In the recycle bin"}
SHOWN = ("waiting", "active", "rejected")
HOLD_ON = "A legal hold is on for this person: nothing in their file can be deleted."

# The kinds a new HR portal starts with: (key, name, who sees it, Drive subfolder, copied to
# Connecteam, who may add one).
BUILT_IN = (
    (CERTIFICATES, "Certificates", "employee", "Certificates", 1, "employee"),
    ("policies", "Signed policies", "employee", "Documents", 0, "hr"),
    ("training", "Training records", "employee", "Documents", 0, "managers"),
    ("hiring", "Hiring paperwork", "directors", "Documents", 0, "hr"),
    ("other", "Other", "hr", "Documents", 0, "hr"),
    ("i9", "I-9", RESTRICTED, "I-9", 0, "hr"),
    ("accommodation", "Reasonable accommodation", RESTRICTED, "Reasonable accommodation", 0, "hr"),
)

# Kinds another part of the HR portal shows in a section of its own ({key: section name}), such
# as certificates: the Documents section leaves them out.
OWNED: dict[str, str] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_kinds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE,                     -- the built-in kinds (NULL for kinds HR adds)
    name TEXT NOT NULL,
    visibility TEXT NOT NULL,            -- access.LEVELS
    folder TEXT NOT NULL,                -- the Drive subfolder
    connecteam INTEGER NOT NULL DEFAULT 0,   -- copied to Connecteam (while the copy is on)
    ct_document INTEGER,                 -- ...to this Connecteam document
    uploaders TEXT NOT NULL DEFAULT 'hr',    -- UPLOADERS
    position INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL,             -- Connecteam user ID
    kind_id INTEGER NOT NULL,
    title TEXT NOT NULL,                 -- encrypted
    doc_date TEXT NOT NULL,              -- YYYY-MM-DD, first in the file's name
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    upload_id TEXT NOT NULL UNIQUE,      -- tags its copies, so a retry never makes a second one
    restricted INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,                -- waiting, active, rejected, deleted, gone
    added_by TEXT,
    added_by_name TEXT,
    added_at REAL NOT NULL,
    decided_by TEXT,                     -- the HR admin who accepted or rejected it
    decided_at REAL,
    reason TEXT,                         -- encrypted: why it was rejected
    deleted_by TEXT,
    deleted_at REAL,
    stored INTEGER NOT NULL DEFAULT 1,   -- 1: an encrypted copy is on this computer
    keep_until REAL,                     -- ...until then (30 days after it reached Drive)
    drive_id TEXT,
    drive_link TEXT,
    ct_document INTEGER,                 -- the Connecteam copy: which document...
    ct_expires TEXT,                     -- ...and its expiry date (certificates)
    ct_copied_at REAL
);
CREATE INDEX IF NOT EXISTS documents_person ON documents (person_id, status);
CREATE INDEX IF NOT EXISTS documents_status ON documents (status);
-- Work for Google Drive and Connecteam, tried until it's done: a file to Drive, its Connecteam
-- copy, moving a deleted one to Drive's trash, renaming a person's folders, moving them to
-- Former employees. It survives restarts and updates.
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER,                 -- NULL for work on a person's folders
    person_id TEXT NOT NULL,
    target TEXT NOT NULL,                -- drive, connecteam, trash, rename, former
    status TEXT NOT NULL DEFAULT 'waiting',  -- waiting, done, attention, skipped
    tries INTEGER NOT NULL DEFAULT 0,
    due_at REAL NOT NULL,
    created_at REAL NOT NULL,
    done_at REAL,
    error TEXT,                          -- the last problem, in plain words
    code TEXT
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries (status, due_at);
CREATE INDEX IF NOT EXISTS deliveries_document ON deliveries (document_id);
CREATE TABLE IF NOT EXISTS legal_holds (
    person_id TEXT PRIMARY KEY,
    reason TEXT,                         -- encrypted
    set_by TEXT,
    set_by_name TEXT,
    set_at REAL NOT NULL
);
-- To-dos for HR about documents: remove_connecteam (a deleted file's copy, by hand).
CREATE TABLE IF NOT EXISTS doc_todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    person_id TEXT NOT NULL,
    what TEXT NOT NULL,
    created_at REAL NOT NULL,
    done_at REAL,
    done_by TEXT
);
"""


def setup(db) -> None:
    """The tables, and the built-in kinds the first time (on start)."""
    db.add_schema(SCHEMA + folders.SCHEMA)
    now = time.time()
    for position, (key, name, level, folder, connecteam, uploaders) in enumerate(BUILT_IN):
        db.conn.execute(
            "INSERT OR IGNORE INTO doc_kinds (key, name, visibility, folder, connecteam, uploaders, "
            "position, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, name, level, folder, connecteam, uploaders, position * 10, now))
    db.conn.commit()


# ---------------------------------------------------------------- kinds

def kinds(db) -> list[dict]:
    return [dict(r) for r in db.conn.execute("SELECT * FROM doc_kinds ORDER BY position, id")]


def kind(db, kind_id) -> dict | None:
    try:
        kid = int(kind_id)
    except (TypeError, ValueError):
        return None
    row = db.conn.execute("SELECT * FROM doc_kinds WHERE id = ?", (kid,)).fetchone()
    return dict(row) if row else None


def kind_by_key(db, key: str) -> dict | None:
    row = db.conn.execute("SELECT * FROM doc_kinds WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


def is_restricted(k: dict) -> bool:
    return k["visibility"] == RESTRICTED


def fixed_fields(k: dict) -> set[str]:
    """What HR can't change on a kind: a restricted kind stays restricted, HR only and out of
    Connecteam; certificates keep the design's rules (their own part decides the rest)."""
    if is_restricted(k):
        return {"visibility", "uploaders", "connecteam", "ct_document"}
    if k["key"] == CERTIFICATES:
        return {"visibility", "uploaders", "ct_document"}
    return set()


def clean_kind(data: dict, current: dict | None = None) -> dict:
    """The fields of a kind HR is adding or changing, checked. Raises ValueError (plain words)."""
    fixed = fixed_fields(current) if current else set()
    values = {}
    if "name" in data or current is None:
        name = " ".join(str(data.get("name") or "").split())
        if not 1 <= len(name) <= 60 or re.search(r"[\x00-\x1f\x7f]", name):
            raise ValueError("A kind's name is 1 to 60 characters.")
        values["name"] = name
    if ("visibility" in data or current is None) and "visibility" not in fixed:
        level = data.get("visibility") or "hr"
        allowed = set(access.LEVELS) if current is None else set(access.LEVELS) - {RESTRICTED}
        if level not in allowed:
            raise ValueError("Choose who can see it." if current is None or level != RESTRICTED else
                             "A kind can only be restricted when it's made: its files are kept apart.")
        values["visibility"] = level
    restricted = (values.get("visibility") if current is None else current["visibility"]) == RESTRICTED
    if "folder" in data or current is None:
        values["folder"] = folders.clean_subfolder(data.get("folder") or "Documents")
    if ("uploaders" in data or current is None) and "uploaders" not in fixed:
        who = "hr" if restricted else data.get("uploaders") or "hr"
        if who not in UPLOADERS:
            raise ValueError("Choose who may add one.")
        values["uploaders"] = who
    if ("connecteam" in data or current is None) and "connecteam" not in fixed:
        values["connecteam"] = 0 if restricted else 1 if data.get("connecteam") in (True, 1, "1") else 0
    if "ct_document" in data and "ct_document" not in fixed and not restricted:
        doc = data.get("ct_document")
        if doc in (None, "", 0):
            values["ct_document"] = None
        elif isinstance(doc, (int, str)) and not isinstance(doc, bool) and str(doc).isdigit() \
                and 0 < int(doc) < 2**53:
            values["ct_document"] = int(doc)
        else:
            raise ValueError("Choose the Connecteam document from the list.")
    return values


def add_kind(db, values: dict) -> int:
    if db.conn.execute("SELECT COUNT(*) FROM doc_kinds").fetchone()[0] >= MAX_KINDS:
        raise ValueError(f"There can be at most {MAX_KINDS} kinds of document.")
    if db.conn.execute("SELECT 1 FROM doc_kinds WHERE name = ? COLLATE NOCASE",
                       (values["name"],)).fetchone():
        raise ValueError("There's already a kind with that name.")
    last = db.conn.execute("SELECT COALESCE(MAX(position), 0) FROM doc_kinds").fetchone()[0]
    cur = db.conn.execute(
        "INSERT INTO doc_kinds (name, visibility, folder, connecteam, ct_document, uploaders, "
        "position, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (values["name"], values["visibility"], values["folder"], values.get("connecteam", 0),
         values.get("ct_document"), values["uploaders"], last + 10, time.time()))
    db.conn.commit()
    return cur.lastrowid


def update_kind(db, kind_id: int, values: dict) -> None:
    if "name" in values and db.conn.execute(
            "SELECT 1 FROM doc_kinds WHERE name = ? COLLATE NOCASE AND id != ?",
            (values["name"], kind_id)).fetchone():
        raise ValueError("There's already a kind with that name.")
    if values:
        sets = ", ".join(f"{k} = ?" for k in values)
        db.conn.execute(f"UPDATE doc_kinds SET {sets} WHERE id = ?", (*values.values(), kind_id))
        db.conn.commit()


# ---------------------------------------------------------------- who sees what

def can_see(rel: str | None, k: dict) -> bool:
    """Whether this relation sees documents of this kind at all (restricted: HR admins only)."""
    return access.can_see_level(rel, k["visibility"] if k["visibility"] in access.LEVELS else "hr")


def can_open(rel: str | None, k: dict) -> bool:
    """Whether they may open the file itself. Certificates: the employee and HR only; managers
    and directors see their status and dates."""
    return can_see(rel, k) and (k["key"] != CERTIFICATES or rel in ("self", "hr"))


def can_upload(rel: str | None, k: dict) -> bool:
    return can_see(rel, k) and rel in UPLOAD_RELATIONS.get(
        "hr" if is_restricted(k) else k["uploaders"], {"hr"})


def upload_limit(rel: str | None) -> int:
    """HR's documents may be bigger (scanned paperwork); everyone else's are photos."""
    return files.DOCUMENT_LIMIT if rel == "hr" else files.PHOTO_LIMIT


def shown_to(rel: str | None, doc: dict, k: dict) -> bool:
    """Whether a document shows on the person's page for this relation (the recycle bin: HR)."""
    if not can_see(rel, k):
        return False
    return doc["status"] in SHOWN or (doc["status"] == "deleted" and rel == "hr")


def copies_on(settings: dict) -> bool:
    """The owner's switch for Connecteam document copies (Settings, Connections)."""
    return settings.get("hr_connecteam_documents") == "1" and settings.get("connecteam_enabled") == "1"


# ---------------------------------------------------------------- documents

def get(db, doc_id) -> dict | None:
    try:
        did = int(doc_id)
    except (TypeError, ValueError):
        return None
    row = db.conn.execute("SELECT * FROM documents WHERE id = ?", (did,)).fetchone()
    return dict(row) if row else None


def of_person(db, person_id, statuses=SHOWN + ("deleted",)) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    return [dict(r) for r in db.conn.execute(
        f"SELECT * FROM documents WHERE person_id = ? AND status IN ({marks}) "
        "ORDER BY doc_date DESC, id DESC", (str(person_id), *statuses))]


def title_of(doc: dict) -> str:
    try:
        return crypto.unseal_text(doc["title"], "documents.title") or ""
    except crypto.Damaged:
        return "(damaged)"


def reason_of(doc: dict) -> str | None:
    try:
        return crypto.unseal_text(doc["reason"], "documents.reason")
    except crypto.Damaged:
        return None


def clean_title(value, default: str) -> str:
    title = " ".join(re.sub(r"[\x00-\x1f\x7f/\\]+", " ", str(value or "")).split())[:120]
    return title or default


def clean_date(value, db) -> str:
    """The document's date (YYYY-MM-DD; today in HR's time zone when blank). Raises ValueError."""
    text = str(value or "").strip()
    if not text:
        return notify.local_day(db)
    try:
        found = date.fromisoformat(text)
    except ValueError:
        raise ValueError("Type the date as YYYY-MM-DD, like 2026-10-25.") from None
    if not 1950 <= found.year <= date.today().year + 1:
        raise ValueError("That date doesn't look right.")
    return found.isoformat()


def file_name(doc: dict) -> str:
    """Its name in Drive: the date first, so folders sort in order ("2026-10-25 Handbook
    acknowledgement.pdf")."""
    kind_of = next((x for x, m in files.MIME.items() if m == doc["mime"]), "pdf")
    return f"{doc['doc_date']} {title_of(doc)}.{files.EXTENSION[kind_of]}"


def files_dir(folder: str) -> str:
    return os.path.join(folder, "files")


def copy_path(folder: str, doc: dict) -> str:
    return os.path.join(files_dir(folder), f"{doc['upload_id']}.bin")


def _write(path: str, data: bytes) -> None:
    crypto.write_private(path, data)


async def save_copy(folder: str, upload_id: str, data: bytes) -> None:
    """The encrypted copy on this computer (in a worker thread)."""
    sealed = await crypto.seal_async(data, FILE_PURPOSE)
    await asyncio.to_thread(_write, os.path.join(files_dir(folder), f"{upload_id}.bin"), sealed)


async def load_copy(folder: str, doc: dict) -> bytes | None:
    """The file from this computer, or None when it isn't kept here (any more)."""
    if not doc["stored"]:
        return None
    try:
        blob = await asyncio.to_thread(_read, copy_path(folder, doc))
        return await crypto.unseal_async(blob, FILE_PURPOSE)
    except (OSError, crypto.Damaged):
        return None


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def drop_copy(db, folder: str, doc: dict) -> None:
    try:
        os.remove(copy_path(folder, doc))
    except FileNotFoundError:
        pass
    db.conn.execute("UPDATE documents SET stored = 0, keep_until = NULL WHERE id = ?", (doc["id"],))
    db.conn.commit()


async def cache_copy(db, folder: str, doc: dict, data: bytes, now: float | None = None) -> None:
    """Keep a file fetched from Drive here again for CACHE_DAYS (it was opened)."""
    await save_copy(folder, doc["upload_id"], data)
    now = time.time() if now is None else now
    db.conn.execute("UPDATE documents SET stored = 1, keep_until = ? WHERE id = ?",
                    (now + CACHE_DAYS * DAY, doc["id"]))
    db.conn.commit()


async def add(svc, person: dict, k: dict, data: bytes, mime: str, title: str, doc_date: str,
              viewer, rel: str) -> dict:
    """A cleaned file (files.clean_upload) for someone's file. HR's go straight on their way to
    Drive (activate); anyone else's wait for HR. Returns the document."""
    upload_id = secrets.token_urlsafe(24)
    await save_copy(svc.folder, upload_id, data)
    db, now = svc.db, time.time()
    waiting = rel != "hr"
    cur = db.conn.execute(
        "INSERT INTO documents (person_id, kind_id, title, doc_date, mime, size, upload_id, "
        "restricted, status, added_by, added_by_name, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?)",
        (str(person["id"]), k["id"], crypto.seal_text(title, "documents.title"), doc_date, mime,
         len(data), upload_id, 1 if is_restricted(k) else 0, "waiting" if waiting else "active",
         viewer.id if viewer else None, viewer.name if viewer else None, now))
    db.conn.commit()
    doc = get(db, cur.lastrowid)
    if not waiting:
        activate(db, doc, svc.main_settings(), viewer.id if viewer else None, now=now)
    return get(db, doc["id"])


def activate(db, doc: dict, settings: dict, actor_id=None, *, ct_document: int | None = None,
             ct_expires: str | None = None, now: float | None = None) -> None:
    """Accepted (or added by HR): on its way to Drive, and to Connecteam where the kind is copied
    there and the owner has the copy on. ct_document and ct_expires: the Connecteam document and
    expiry date for this file (certificates), else the kind's document."""
    now = time.time() if now is None else now
    k = kind(db, doc["kind_id"])
    target = ct_document or (k["ct_document"] if k else None)
    db.conn.execute("UPDATE documents SET status = 'active', decided_by = COALESCE(decided_by, ?), "
                    "decided_at = COALESCE(decided_at, ?), ct_document = ?, ct_expires = ? "
                    "WHERE id = ?", (actor_id, now, target, ct_expires, doc["id"]))
    db.conn.commit()
    queue(db, doc["person_id"], "drive", doc["id"], now)
    if k and k["connecteam"] and target and not doc["restricted"] and copies_on(settings):
        queue(db, doc["person_id"], "connecteam", doc["id"], now)


def reject(db, doc: dict, reason: str, actor_id, now: float | None = None) -> None:
    now = time.time() if now is None else now
    db.conn.execute("UPDATE documents SET status = 'rejected', decided_by = ?, decided_at = ?, "
                    "reason = ?, keep_until = ? WHERE id = ?",
                    (actor_id, now, crypto.seal_text(reason, "documents.reason"),
                     now + REJECTED_DAYS * DAY, doc["id"]))
    db.conn.commit()


def delete(db, doc: dict, actor_id, now: float | None = None) -> None:
    """Into the recycle bin. Work not done yet for it stops; a copy in Connecteam becomes a
    to-do for HR. Raises ValueError under a legal hold."""
    if on_hold(db, doc["person_id"]):
        raise ValueError(HOLD_ON)
    now = time.time() if now is None else now
    db.conn.execute("UPDATE documents SET status = 'deleted', deleted_by = ?, deleted_at = ? "
                    "WHERE id = ?", (actor_id, now, doc["id"]))
    db.conn.execute("UPDATE deliveries SET status = 'skipped', error = 'Deleted' WHERE document_id "
                    "= ? AND status IN ('waiting', 'attention')", (doc["id"],))
    if doc["ct_copied_at"]:
        db.conn.execute("INSERT INTO doc_todos (document_id, person_id, what, created_at) VALUES "
                        "(?, ?, 'remove_connecteam', ?)", (doc["id"], doc["person_id"], now))
    db.conn.commit()


def restore(db, doc: dict, settings: dict, now: float | None = None) -> None:
    """Out of the recycle bin: back as it was, and anything not yet done is done."""
    now = time.time() if now is None else now
    was_waiting = doc["decided_at"] is None   # deleted before HR accepted it
    db.conn.execute("UPDATE documents SET status = ?, deleted_by = NULL, deleted_at = NULL "
                    "WHERE id = ?", ("waiting" if was_waiting else "active", doc["id"]))
    db.conn.execute("UPDATE doc_todos SET done_at = ?, done_by = 'restored' WHERE document_id = ? "
                    "AND done_at IS NULL", (now, doc["id"]))
    db.conn.commit()
    if was_waiting:
        return
    if not doc["drive_id"]:
        queue(db, doc["person_id"], "drive", doc["id"], now)
    k = kind(db, doc["kind_id"])
    if not doc["ct_copied_at"] and doc["ct_document"] and k and k["connecteam"] and \
            not doc["restricted"] and copies_on(settings):
        queue(db, doc["person_id"], "connecteam", doc["id"], now)


# ---------------------------------------------------------------- the delivery queue's rows

def queue(db, person_id, target: str, document_id: int | None = None,
          now: float | None = None) -> int | None:
    """Add work for delivery.py, unless the same work is already waiting. Returns its ID."""
    now = time.time() if now is None else now
    same = db.conn.execute(
        "SELECT id FROM deliveries WHERE person_id = ? AND target = ? AND document_id IS ? AND "
        "status IN ('waiting', 'attention')", (str(person_id), target, document_id)).fetchone()
    if same:
        return None
    cur = db.conn.execute("INSERT INTO deliveries (document_id, person_id, target, due_at, "
                          "created_at) VALUES (?, ?, ?, ?, ?)",
                          (document_id, str(person_id), target, now, now))
    db.conn.commit()
    return cur.lastrowid


def deliveries_of(db, doc_id: int) -> list[dict]:
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM deliveries WHERE document_id = ? ORDER BY id", (doc_id,))]


def release_if_done(db, doc_id: int, now: float | None = None) -> None:
    """Every copy made: the one here is kept CACHE_DAYS more for quick viewing, then goes."""
    now = time.time() if now is None else now
    open_work = db.conn.execute(
        "SELECT 1 FROM deliveries WHERE document_id = ? AND target IN ('drive', 'connecteam') AND "
        "status IN ('waiting', 'attention')", (doc_id,)).fetchone()
    if not open_work:
        db.conn.execute("UPDATE documents SET keep_until = ? WHERE id = ? AND status = 'active' "
                        "AND drive_id IS NOT NULL", (now + CACHE_DAYS * DAY, doc_id))
        db.conn.commit()


def retry(db, delivery_id: int, now: float | None = None) -> bool:
    """HR's Retry on work that needs attention: tried again now, with its 8 tries back."""
    changed = db.conn.execute("UPDATE deliveries SET status = 'waiting', tries = 0, due_at = ? "
                              "WHERE id = ? AND status = 'attention'",
                              (time.time() if now is None else now, delivery_id)).rowcount
    db.conn.commit()
    return bool(changed)


def skip(db, delivery_id: int) -> bool:
    """HR's "Don't copy" on a Connecteam copy that needs attention: Drive keeps the file."""
    changed = db.conn.execute("UPDATE deliveries SET status = 'skipped', error = 'Not copied: HR "
                              "chose not to' WHERE id = ? AND status = 'attention' AND target = "
                              "'connecteam'", (delivery_id,)).rowcount
    db.conn.commit()
    if changed:
        row = db.conn.execute("SELECT document_id FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
        release_if_done(db, row[0])
    return bool(changed)


# ---------------------------------------------------------------- legal holds and retention

def on_hold(db, person_id) -> bool:
    return db.conn.execute("SELECT 1 FROM legal_holds WHERE person_id = ?",
                           (str(person_id),)).fetchone() is not None


def hold_of(db, person_id) -> dict | None:
    row = db.conn.execute("SELECT * FROM legal_holds WHERE person_id = ?", (str(person_id),)).fetchone()
    if not row:
        return None
    try:
        reason = crypto.unseal_text(row["reason"], "holds.reason")
    except crypto.Damaged:
        reason = None
    return {"reason": reason, "set_by": row["set_by_name"], "set_at": row["set_at"]}


def set_hold(db, person_id, on: bool, reason: str, actor_id, actor: str) -> None:
    if on:
        db.conn.execute("INSERT OR REPLACE INTO legal_holds (person_id, reason, set_by, set_by_name, "
                        "set_at) VALUES (?, ?, ?, ?, ?)",
                        (str(person_id), crypto.seal_text(reason, "holds.reason"), actor_id, actor,
                         time.time()))
    else:
        db.conn.execute("DELETE FROM legal_holds WHERE person_id = ?", (str(person_id),))
    db.conn.commit()


def retention_years(db) -> int:
    return db.setting_int("retention_years", 1, 30)


def keep_until(db, person: dict) -> float | None:
    """When a former employee's records are due for review (None while they're active)."""
    if not person["archived"] or not person["archived_at"]:
        return None
    return person["archived_at"] + retention_years(db) * 365.25 * DAY


def past_retention(db, now: float | None = None) -> list[dict]:
    """Former employees whose records are past the retention period, with documents still here
    and no legal hold: listed for HR to review and delete (never deleted by themselves)."""
    now = time.time() if now is None else now
    found = []
    for p in db.people(archived=True):
        due = keep_until(db, p)
        if due is None or due > now or on_hold(db, p["id"]):
            continue
        count = db.conn.execute("SELECT COUNT(*) FROM documents WHERE person_id = ? AND status IN "
                                "('waiting', 'active')", (p["id"],)).fetchone()[0]
        if count:
            found.append({"id": p["id"], "name": p["name"], "left_at": p["archived_at"],
                          "due_at": due, "documents": count})
    return found


# ---------------------------------------------------------------- the nightly run

def nightly(svc, now: float | None = None) -> dict:
    """Copies kept long enough here go; the recycle bin empties into Drive's trash after 30 days
    (not under a legal hold); rejected uploads go after 30 days; folders of people whose name
    changed are renamed. Returns counts."""
    db, folder = svc.db, svc.folder
    now = time.time() if now is None else now
    counts = {"cached": 0, "binned": 0, "rejected": 0, "renamed": 0, "strays": 0}
    for doc in [dict(r) for r in db.conn.execute(
            "SELECT * FROM documents WHERE status = 'active' AND stored = 1 AND keep_until < ?",
            (now,))]:
        drop_copy(db, folder, doc)
        counts["cached"] += 1
    for doc in [dict(r) for r in db.conn.execute(
            "SELECT * FROM documents WHERE status = 'deleted' AND deleted_at < ?",
            (now - BIN_DAYS * DAY,))]:
        if on_hold(db, doc["person_id"]):
            continue
        drop_copy(db, folder, doc)
        db.conn.execute("UPDATE documents SET status = 'gone' WHERE id = ?", (doc["id"],))
        db.conn.commit()
        if doc["drive_id"]:
            queue(db, doc["person_id"], "trash", doc["id"], now)
        counts["binned"] += 1
    for doc in [dict(r) for r in db.conn.execute(
            "SELECT * FROM documents WHERE status = 'rejected' AND keep_until < ?", (now,))]:
        drop_copy(db, folder, doc)
        db.conn.execute("UPDATE documents SET status = 'gone' WHERE id = ?", (doc["id"],))
        db.conn.commit()
        counts["rejected"] += 1
    for pid in {r[0] for r in db.conn.execute(
            "SELECT DISTINCT person_id FROM drive_folders WHERE person_id != '' AND sub = ''")}:
        person = db.person(pid)
        if person and folders.needs_rename(db, person) and queue(db, pid, "rename", None, now):
            counts["renamed"] += 1
    counts["strays"] = remove_strays(db, folder, now)
    return counts


def remove_strays(db, folder: str, now: float) -> int:
    """Encrypted copies no document points to (a crash between saving and recording), after a
    day."""
    try:
        names = os.listdir(files_dir(folder))
    except FileNotFoundError:
        return 0
    known = {f"{r[0]}.bin" for r in db.conn.execute("SELECT upload_id FROM documents WHERE stored = 1")}
    count = 0
    for name in names:
        path = os.path.join(files_dir(folder), name)
        try:
            if name not in known and now - os.path.getmtime(path) > DAY:
                os.remove(path)
                count += 1
        except OSError:
            pass
    return count


# ---------------------------------------------------------------- migration files

def migration_blocker(svc) -> str | None:
    """A migration file carries HR's database but not the files kept on this computer, so it
    waits while any of them haven't reached Google Drive (or Connecteam) yet, or wait for HR.
    Checked while HR is off too (reading its database on its own)."""
    if svc.running:
        return _blocker(svc.db.conn)
    path = os.path.join(svc.folder, hrdb.DB_FILE)
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{urllib.parse.quote(path)}?mode=ro", uri=True,
                               timeout=hrdb.BUSY_SECONDS)
        try:
            return _blocker(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _blocker(conn) -> str | None:
    try:
        going = conn.execute("SELECT COUNT(DISTINCT document_id) FROM deliveries WHERE target IN "
                             "('drive', 'connecteam') AND status IN ('waiting', 'attention')"
                             ).fetchone()[0]
        checking = conn.execute("SELECT COUNT(*) FROM documents WHERE status = 'waiting'").fetchone()[0]
    except sqlite3.OperationalError:   # the documents part never ran here
        return None
    if going:
        return (f"{going} HR file{' is' if going == 1 else 's are'} still waiting to reach Google "
                "Drive or Connecteam. Wait until they're sent (the HR portal's Needs attention page "
                "shows any that are stuck), then try again.")
    if checking:
        return (f"{checking} HR upload{' is' if checking == 1 else 's are'} waiting for HR to "
                f"accept or reject {'it' if checking == 1 else 'them'} in the HR portal. Once HR "
                "has, try again.")
    return None
