"""Employee documents in the HR portal: the kinds of document and who sees each, the files
themselves, and what happens to them over time.

Kinds (HR settings): a name, who can see it (access.LEVELS), its Drive subfolder, who may add one
(HR only; managers and directors too; or the employee too: anyone but HR waits for HR to check
it), and whether it's copied to Connecteam (only while the owner has the Connecteam documents
copy on). The defaults follow the owner's decisions. "I-9" and "Reasonable accommodation" are
restricted: HR admins only (nobody else even learns one exists), stored apart (folders.py, and
sealed with keys of their own here), every look logged, never copied to Connecteam. Whether a
kind is restricted is fixed when it's made, because its files are stored apart.

Other parts of the HR portal keep their own kinds here (register_kind): certificates, signed
write-ups, final reviews. Those are left out of the Documents section, their part decides who
may open them (can_open), and files the portal makes itself go straight to Drive
(add_generated). PDFs are made by Google Drive from plain HTML (pdfdoc.py, make_pdf), always
through the delivery queue: the part asks for one (queue_pdf), and the queue has the part write
its HTML (register_pdf_maker) as soon as Google can make it, keeps the PDF (add_generated), and
retries or puts it on HR's Needs attention list like any upload.

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
import inspect
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
import weakref
from datetime import date

from . import access, audit, crypto, files, folders, gdrive, hrdb, notify, service

DAY = 86400
BIN_DAYS = 30            # deleted files can be restored for this long
CACHE_DAYS = 30          # a delivered file stays here this long for quick viewing
REJECTED_DAYS = 30       # a rejected upload goes after this long
MAX_KINDS = 50
MAX_WAITING = 30         # uploads waiting for HR in one person's file, at most
FILE_PURPOSE = "file.document"
RESTRICTED_PURPOSE = "file.restricted"   # restricted files are sealed with a key of their own
CERTIFICATES = "certificates"
RESTRICTED = "restricted"
KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")
UPLOADERS = {"hr": "HR admins only",
             "managers": "Their managers and directors too (HR checks it)",
             "employee": "The employee too (HR checks it)"}
UPLOAD_RELATIONS = {"hr": {"hr"}, "managers": {"manager", "director", "hr"},
                    "employee": {"self", "manager", "director", "hr"}}
STATUS_WORDS = {"waiting": "Waiting for HR", "active": "Saved", "rejected": "Not accepted",
                "deleted": "In the recycle bin", "gone": "Deleted"}
SHOWN = ("waiting", "active", "rejected")
HOLD_ON = "A legal hold is on for this person: nothing in their file can be deleted."
COPIES_OFF = ("Connecteam copies are off. The owner turns them on in Home Assistant: Settings, "
              "Connections, Connecteam for HR, Copy confirmed certificates to Connecteam.")
NO_PDF = ("PDFs are made through Google Drive, which isn't set up yet. The owner connects it in "
          "Home Assistant: Settings, Connections, Google Drive (HR files). The PDF is made as "
          "soon as it is.")
TOO_MANY_WAITING = (f"There are already {MAX_WAITING} uploads waiting for HR in this file. Add "
                    "more once HR has checked those.")

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

# Kinds other parts of the HR portal add (register_kind): {key: (name, level, subfolder)}.
REGISTERED: dict[str, tuple] = {}
# Kinds another part shows in a section of its own ({key: section name}): the Documents section
# leaves them out, and their own part adds, accepts and deletes them.
OWNED: dict[str, str] = {CERTIFICATES: "certificates"}
# Who may open a file of a kind another part owns: {key: can_open(viewer, rel, doc) -> bool}.
CAN_OPEN: dict = {}
# PDFs made later by the delivery queue (target "pdf"): {kind key: (maker, done)}, where
# maker(svc, row) -> (person, title, html[, doc_date]) or None, and done(svc, row, doc).
PDF_MAKERS: dict = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_kinds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE,                     -- built-in and registered kinds (NULL for kinds HR adds)
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
-- Former employees, making a PDF another part asked for. It survives restarts and updates.
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER,                 -- the file (a PDF: the one made, once it's made)
    person_id TEXT NOT NULL,
    target TEXT NOT NULL,                -- drive, connecteam, trash, rename, former, pdf
    kind_key TEXT,                       -- pdf: the kind it's for (its part makes the HTML)...
    ref TEXT,                            -- ...and that part's record (a write-up, a review)
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

notify.register_kind("document_waiting", "A document is waiting for HR to check it.")
notify.register_kind("document_rejected", "A document you added wasn't accepted. See why in the HR portal.")
notify.register_kind("records_due", "A former employee's records are past the time they're kept.")


def setup(db) -> None:
    """The tables, and the built-in and registered kinds the first time (on start)."""
    db.add_schema(SCHEMA + folders.SCHEMA)
    _ensure_kinds(db)


def _ensure_kinds(db) -> None:
    now = time.time()
    rows = [(key, name, level, folder, connecteam, uploaders)
            for key, name, level, folder, connecteam, uploaders in BUILT_IN]
    rows += [(key, name, level, folder, 0, "hr") for key, (name, level, folder) in REGISTERED.items()]
    for position, (key, name, level, folder, connecteam, uploaders) in enumerate(rows):
        db.conn.execute(
            "INSERT OR IGNORE INTO doc_kinds (key, name, visibility, folder, connecteam, uploaders, "
            "position, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, name, level, folder, connecteam, uploaders, position * 10, now))
    db.conn.commit()


# ---------------------------------------------------------------- other parts' kinds

def register_kind(key: str, name: str, level: str, subfolder: str, owner_section: str, *,
                  can_open=None) -> None:
    """A kind of document another part of the HR portal owns, such as "writeups" (Discipline:
    subfolder "Discipline", level "managers") or "reviews". Called when that part is imported.
    The kind is made on start if it isn't there (HR may rename it later, and its Drive
    subfolder), it's left out of the Documents section (owner_section shows it), and
    can_open(viewer, rel, doc) -> bool, when given, decides who may open its files; otherwise
    the kind's level does."""
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise ValueError("A kind's key is short lowercase text, like writeups.")
    if level not in access.LEVELS:
        raise ValueError(f"Choose a level from {', '.join(access.LEVELS)}.")
    REGISTERED[key] = (" ".join(str(name or "").split())[:60] or key, level,
                       folders.clean_subfolder(subfolder))
    OWNED[key] = str(owner_section or key)
    if can_open is not None:
        CAN_OPEN[key] = can_open
    if hrdb.current is not None:
        _ensure_kinds(hrdb.current)


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


def owned_by(k: dict) -> str | None:
    """The section of another part that shows this kind, or None (the Documents section)."""
    return OWNED.get(k["key"]) if k.get("key") else None


def fixed_fields(k: dict) -> set[str]:
    """What HR can't change on a kind: a restricted kind stays restricted, HR only and out of
    Connecteam; a kind another part owns keeps that part's rules (HR can rename it and change its
    Drive subfolder)."""
    if is_restricted(k) or owned_by(k):
        return {"visibility", "uploaders", "connecteam", "ct_document"}
    return set()


def clean_kind(data: dict, current: dict | None = None, copies: bool = True) -> dict:
    """The fields of a kind HR is adding or changing, checked. copies: whether the owner has the
    Connecteam copy on (a kind's copy can only be turned on then). Raises ValueError (plain
    words)."""
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
        on = not restricted and data.get("connecteam") in (True, 1, "1")
        if on and not copies and not (current and current["connecteam"]):
            raise ValueError(COPIES_OFF)
        values["connecteam"] = 1 if on else 0
    if "ct_document" in data and "ct_document" not in fixed and not restricted:
        doc = data.get("ct_document")
        if doc in (None, "", 0):
            values["ct_document"] = None
        elif isinstance(doc, (int, str)) and not isinstance(doc, bool) and str(doc).isdigit() \
                and 0 < int(doc) < 2**53:
            values["ct_document"] = int(doc)
        else:
            raise ValueError("Choose the Connecteam document from the list.")
    copied = values.get("connecteam", current["connecteam"] if current else 0)
    target = values["ct_document"] if "ct_document" in values else (current or {}).get("ct_document")
    if copied and not target and "connecteam" not in fixed:
        raise ValueError("Choose the Connecteam document these go to.")
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
    """Whether they may open the file itself, by the kind's level. Certificates: the employee
    and HR only; managers and directors see their status and dates."""
    return can_see(rel, k) and (k["key"] != CERTIFICATES or rel in ("self", "hr"))


def may_open(viewer, rel: str | None, doc: dict, k: dict | None) -> bool:
    """Whether this viewer may open this file: restricted files HR admins only; a kind another
    part owns by that part's can_open; others by the kind's level. Waiting ones too (the person
    who added it, and anyone who sees the kind); rejected ones only HR and whoever added it; the
    recycle bin only HR."""
    if rel is None or k is None or doc["status"] not in SHOWN + ("deleted",):
        return False
    if doc["status"] == "deleted" and rel != "hr":
        return False
    if doc["restricted"] or is_restricted(k):
        return rel == "hr"
    check = CAN_OPEN.get(k["key"]) if k["key"] else None
    if check is not None:
        return bool(check(viewer, rel, doc))
    if doc["status"] == "rejected" and rel != "hr" and str(doc["added_by"]) != str(viewer.id):
        return False
    return can_open(rel, k)


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


def by_upload_id(db, upload_id: str) -> dict | None:
    row = db.conn.execute("SELECT * FROM documents WHERE upload_id = ?", (upload_id,)).fetchone()
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
    if isinstance(value, date):
        value = value.isoformat()
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


def kind_of_file(doc: dict) -> str:
    """"pdf" or "image", for showing it."""
    return "pdf" if doc["mime"] == files.MIME["pdf"] else "image"


def file_name(doc: dict) -> str:
    """Its name in Drive: the date first, so folders sort in order ("2026-10-25 Handbook
    acknowledgement.pdf")."""
    kind_of = next((x for x, m in files.MIME.items() if m == doc["mime"]), "pdf")
    return f"{doc['doc_date']} {title_of(doc)}.{files.EXTENSION[kind_of]}"


def view_url(doc) -> str:
    """Where the portal opens a document (a doc, or its ID): the route checks who's asking, by
    the kind's own part (can_open) or its level, and logs every look."""
    return f"/api/documents/{int(doc['id'] if isinstance(doc, dict) else doc)}/file"


def files_dir(folder: str) -> str:
    return os.path.join(folder, "files")


def copy_path(folder: str, doc: dict) -> str:
    return os.path.join(files_dir(folder), f"{doc['upload_id']}.bin")


def _purpose(restricted) -> str:
    return RESTRICTED_PURPOSE if restricted else FILE_PURPOSE


def _write(path: str, data: bytes) -> None:
    crypto.write_private(path, data)


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


async def save_copy(folder: str, upload_id: str, data: bytes, restricted: bool = False) -> None:
    """The encrypted copy on this computer (in a worker thread)."""
    sealed = await crypto.seal_async(data, _purpose(restricted))
    await asyncio.to_thread(_write, os.path.join(files_dir(folder), f"{upload_id}.bin"), sealed)


async def load_copy(folder: str, doc: dict) -> bytes | None:
    """The file from this computer, or None when it isn't kept here (any more)."""
    if not doc["stored"]:
        return None
    try:
        blob = await asyncio.to_thread(_read, copy_path(folder, doc))
        return await crypto.unseal_async(blob, _purpose(doc["restricted"]))
    except (OSError, crypto.Damaged):
        return None


def drop_copy(db, folder: str, doc: dict) -> None:
    try:
        os.remove(copy_path(folder, doc))
    except FileNotFoundError:
        pass
    db.conn.execute("UPDATE documents SET stored = 0, keep_until = NULL WHERE id = ?", (doc["id"],))
    db.conn.commit()


async def cache_copy(db, folder: str, doc: dict, data: bytes, now: float | None = None) -> None:
    """Keep a file fetched from Drive here again for CACHE_DAYS (it was opened)."""
    await save_copy(folder, doc["upload_id"], data, bool(doc["restricted"]))
    now = time.time() if now is None else now
    db.conn.execute("UPDATE documents SET stored = 1, keep_until = ? WHERE id = ?",
                    (now + CACHE_DAYS * DAY, doc["id"]))
    db.conn.commit()


def waiting_count(db, person_id) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM documents WHERE person_id = ? AND status = "
                           "'waiting'", (str(person_id),)).fetchone()[0]


async def add(svc, person: dict, k: dict, data: bytes, mime: str, title: str, doc_date: str,
              viewer, rel: str, *, waiting: bool | None = None, ct_document: int | None = None,
              ct_expires: str | None = None) -> dict:
    """A cleaned file (files.clean_upload) for someone's file. HR's go straight on their way to
    Drive (activate, with ct_document and ct_expires for its Connecteam copy: a confirmed
    certificate's); anyone else's wait for HR (waiting=True: HR's too). Returns the document.
    Raises ValueError when too many already wait in that file."""
    db = svc.db
    waiting = rel != "hr" if waiting is None else waiting
    if waiting and waiting_count(db, person["id"]) >= MAX_WAITING:
        raise ValueError(TOO_MANY_WAITING)
    upload_id = secrets.token_urlsafe(24)
    await save_copy(svc.folder, upload_id, data, is_restricted(k))
    now = time.time()
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
        activate(db, doc, svc.main_settings(), viewer.id if viewer else None,
                 ct_document=ct_document, ct_expires=ct_expires, now=now)
    return get(db, doc["id"])


async def add_generated(svc, person, kind_key: str, data: bytes, mime: str, title: str,
                        doc_date, actor_id=None, *, upload_id: str | None = None) -> dict:
    """A file the portal made itself (a signed write-up's PDF, a final review's) for the kind
    another part registered: no HR check, straight into the delivery queue to that kind's Drive
    subfolder. person: the person (or their ID). upload_id: a key of the caller's that makes it
    idempotent (asking again returns the same document). Returns the document. Raises
    ValueError."""
    db = svc.db
    k = kind_by_key(db, kind_key)
    if k is None:
        raise ValueError(f"There's no kind of document {kind_key!r}: register_kind it first.")
    pid = str(person["id"] if isinstance(person, dict) else person)
    if db.person(pid) is None:
        raise ValueError("That person isn't in the HR portal.")
    sniffed = files.sniff(data)
    if sniffed is None or files.MIME[sniffed] != mime or len(data) > files.DOCUMENT_LIMIT:
        raise ValueError("A generated file is a PDF or picture of up to 25 MB, of the type it says.")
    if upload_id and (found := by_upload_id(db, upload_id)):
        return found
    upload_id = upload_id or secrets.token_urlsafe(24)
    await save_copy(svc.folder, upload_id, data, is_restricted(k))
    now = time.time()
    try:
        cur = db.conn.execute(
            "INSERT INTO documents (person_id, kind_id, title, doc_date, mime, size, upload_id, "
            "restricted, status, added_by, added_by_name, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "'active', ?, ?, ?)",
            (pid, k["id"], crypto.seal_text(clean_title(title, k["name"]), "documents.title"),
             clean_date(doc_date, db), mime, len(data), upload_id, 1 if is_restricted(k) else 0,
             None if actor_id is None else str(actor_id),
             svc.person_name(actor_id) if actor_id else "HR portal", now))
    except sqlite3.IntegrityError:   # made meanwhile with the same upload_id
        db.conn.rollback()
        return by_upload_id(db, upload_id)
    db.conn.commit()
    doc = get(db, cur.lastrowid)
    activate(db, doc, svc.main_settings(), actor_id, now=now)
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
                    "= ? AND target IN ('drive', 'connecteam') AND status IN ('waiting', "
                    "'attention')", (doc["id"],))
    if doc["ct_copied_at"]:
        db.conn.execute("INSERT INTO doc_todos (document_id, person_id, what, created_at) VALUES "
                        "(?, ?, 'remove_connecteam', ?)", (doc["id"], doc["person_id"], now))
    db.conn.commit()


def restore(db, doc: dict, settings: dict, now: float | None = None) -> None:
    """Out of the recycle bin: back as it was, and anything not yet done is done."""
    now = time.time() if now is None else now
    # Deleted before HR checked it, after HR rejected it (only those have a reason), or saved.
    status = "waiting" if doc["decided_at"] is None else "rejected" if doc["reason"] else "active"
    db.conn.execute("UPDATE documents SET status = ?, deleted_by = NULL, deleted_at = NULL "
                    "WHERE id = ?", (status, doc["id"]))
    db.conn.execute("UPDATE doc_todos SET done_at = ?, done_by = 'restored' WHERE document_id = ? "
                    "AND done_at IS NULL", (now, doc["id"]))
    db.conn.commit()
    if status != "active":
        return
    if not doc["drive_id"]:
        queue(db, doc["person_id"], "drive", doc["id"], now)
    k = kind(db, doc["kind_id"])
    if not doc["ct_copied_at"] and doc["ct_document"] and k and k["connecteam"] and \
            not doc["restricted"] and copies_on(settings):
        queue(db, doc["person_id"], "connecteam", doc["id"], now)


# ---------------------------------------------------------------- PDFs (made by Google Drive)

class PdfUnavailable(Exception):
    """A PDF can't be made now: Google Drive isn't set up (code "not_set_up"), or the bridge
    failed (its gdrive error code: outdated, unavailable, quota, ...). The message is plain.
    The delivery queue's "pdf" work waits or tries again (parts ask through queue_pdf)."""

    def __init__(self, message: str, code: str = "not_set_up"):
        super().__init__(message)
        self.message, self.code = message, code


async def make_pdf(svc, html: str) -> bytes:
    """A PDF of plain HTML (pdfdoc.page), made by Google Drive through the bridge (the queue's
    "pdf" work calls it). Raises PdfUnavailable, or ValueError for HTML that's empty or too
    long."""
    bridge = svc.google() if svc is not None else None
    if bridge is None or not svc.main_settings().get("hr_google_drive"):
        raise PdfUnavailable(NO_PDF)
    try:
        return await bridge.render_pdf(html)
    except gdrive.BridgeError as err:
        raise PdfUnavailable(err.message, err.code) from None


def register_pdf_maker(kind_key: str, maker, *, done=None) -> None:
    """How the delivery queue makes a PDF another part asked for (queue_pdf) once Google can:
    maker(svc, row) (may be async) returns (person, title, html) or (person, title, html,
    doc_date) for the queue's row (row["ref"] is the part's own record), or None when there's
    nothing to make any more. The PDF is then kept with add_generated, and done(svc, row, doc)
    (optional, may be async) is told which document it became."""
    if not callable(maker) or (done is not None and not callable(done)):
        raise ValueError("A PDF maker is a function.")
    PDF_MAKERS[kind_key] = (maker, done)


def queue_pdf(db, kind_key: str, person_id, ref, now: float | None = None) -> int | None:
    """Make this PDF through the delivery queue (target "pdf"): straight away in the background
    when Google can, else once it can, retried like any upload, and on HR's Needs attention list
    when it keeps failing. ref: the part's own record (say, the write-up's ID), handed back in
    row["ref"]. Asking twice for the same one queues it once."""
    return queue(db, person_id, "pdf", None, now, kind_key=kind_key, ref=str(ref))


def pdf_work(db, kind_key: str, ref) -> dict | None:
    """The queue's latest work on that PDF (status waiting, done, attention or skipped, with
    error and tries), or None: for the part to show how it's going, and offer HR's Retry."""
    row = db.conn.execute("SELECT * FROM deliveries WHERE target = 'pdf' AND kind_key = ? AND "
                          "ref = ? ORDER BY id DESC LIMIT 1", (kind_key, str(ref))).fetchone()
    return dict(row) if row else None


def pdf_works(db, kind_key: str, status: str) -> list[dict]:
    """The queue's PDF work for one kind in one status (waiting, attention), oldest first."""
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM deliveries WHERE target = 'pdf' AND kind_key = ? AND status = ? ORDER BY id",
        (kind_key, status))]


async def run_maker(fn, *args):
    result = fn(*args)
    return await result if inspect.isawaitable(result) else result


# ---------------------------------------------------------------- the delivery queue's rows

_service = None   # the running HR service (a weak reference), so new work starts straight away


def queue(db, person_id, target: str, document_id: int | None = None,
          now: float | None = None, *, kind_key: str | None = None,
          ref: str | None = None) -> int | None:
    """Add work for delivery.py, unless the same work is already waiting. Returns its ID."""
    now = time.time() if now is None else now
    same = db.conn.execute(
        "SELECT id FROM deliveries WHERE person_id = ? AND target = ? AND document_id IS ? AND "
        "kind_key IS ? AND ref IS ? AND status IN ('waiting', 'attention')",
        (str(person_id), target, document_id, kind_key, ref)).fetchone()
    if same:
        return None
    cur = db.conn.execute("INSERT INTO deliveries (document_id, person_id, target, kind_key, ref, "
                          "due_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (document_id, str(person_id), target, kind_key, ref, now, now))
    db.conn.commit()
    _poke()
    return cur.lastrowid


def _poke() -> None:
    """New work: the delivery queue looks now rather than at its next half-minute tick."""
    svc = _service() if _service is not None else None
    if svc is None or not svc.running:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    from . import delivery
    delivery.kick(svc)


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
                        "AND drive_id IS NOT NULL AND stored = 1", (now + CACHE_DAYS * DAY, doc_id))
        db.conn.commit()


def retry(db, delivery_id: int, now: float | None = None) -> bool:
    """HR's Retry on work that needs attention: tried again now, with its 8 tries back."""
    changed = db.conn.execute("UPDATE deliveries SET status = 'waiting', tries = 0, due_at = ? "
                              "WHERE id = ? AND status = 'attention'",
                              (time.time() if now is None else now, delivery_id)).rowcount
    db.conn.commit()
    if changed:
        _poke()
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
                                "('waiting', 'active', 'rejected')", (p["id"],)).fetchone()[0]
        if count:
            found.append({"id": p["id"], "name": p["name"], "left_at": p["archived_at"],
                          "due_at": due, "documents": count})
    return found


def delete_all(db, person_id, actor_id, now: float | None = None) -> int:
    """Every document in someone's file into the recycle bin (HR, once their records are past
    retention). Returns how many. Raises ValueError under a legal hold."""
    if on_hold(db, person_id):
        raise ValueError(HOLD_ON)
    docs = of_person(db, person_id, SHOWN)
    for doc in docs:
        delete(db, doc, actor_id, now)
    return len(docs)


# ---------------------------------------------------------------- the nightly run

def nightly(svc, now: float | None = None) -> dict:
    """Copies kept long enough here go; the recycle bin empties into Drive's trash after 30 days
    and rejected uploads go after 30 days (neither under a legal hold); folders of people whose
    name changed are renamed; HR hears of records newly past retention. Returns counts."""
    db, folder = svc.db, svc.folder
    now = time.time() if now is None else now
    counts = {"cached": 0, "binned": 0, "rejected": 0, "renamed": 0, "strays": 0, "due": 0}
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
        if on_hold(db, doc["person_id"]):
            continue
        drop_copy(db, folder, doc)
        db.conn.execute("UPDATE documents SET status = 'gone' WHERE id = ?", (doc["id"],))
        db.conn.commit()
        counts["rejected"] += 1
    for pid in {r[0] for r in db.conn.execute(
            "SELECT DISTINCT person_id FROM drive_folders WHERE person_id != '' AND sub = ''")}:
        person = db.person(pid)
        if person and folders.needs_rename(db, person) and queue(db, pid, "rename", None, now):
            counts["renamed"] += 1
    for due in past_retention(db, now):
        for admin in db.with_role(access.HR_ADMIN):
            if notify.notify(admin, "records_due", db=db, link="#/attention",
                             once=f"retention-{due['id']}"):
                counts["due"] += 1
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


# ---------------------------------------------------------------- the part's hooks

def _part(svc) -> None:
    """The documents part of a new HR service (service.PARTS): its tables on start, the delivery
    queue every half minute, the nightly clean-up, the migration check, and its pages."""
    from . import delivery, docroutes  # noqa: F401 - the queue, and the routes and sections
    svc.on_start.append(_started)
    svc.tick_jobs.append(delivery.tick)
    svc.daily_jobs.append(_nightly)
    svc.migration_blockers.append(migration_blocker)
    svc.on_stop.append(delivery.stop)


def _started(svc) -> None:
    global _service
    setup(svc.db)
    _service = weakref.ref(svc)


async def _nightly(svc) -> None:
    counts = nightly(svc)
    if counts["binned"] or counts["rejected"]:
        audit.record(svc.db, "Emptied the documents recycle bin",
                     detail=f"{counts['binned']} deleted and {counts['rejected']} rejected file(s) "
                            "removed after 30 days")


service.PARTS.append(_part)
