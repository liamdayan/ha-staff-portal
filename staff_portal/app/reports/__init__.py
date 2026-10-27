"""Reports in the staff app: each report is a folder here, app/reports/<key>/, with report.py
(REPORT = dict(...)), view.js (StaffReports.register) and an optional view.css. Folders are found
at startup, so adding a report needs no other change (see REPORTS.md). A folder whose name starts
with "_" (the template) is hidden from the lists.

This module finds the reports, checks what each declares, keeps their uploaded files (newest
UPLOADS_KEPT of each kind, under DATA_DIR/reports/<key>/uploads/) and builds the ctx a report's
data() gets. Who may see or manage a report is decided in main.py, on every request.
"""

import dataclasses
import importlib.util
import inspect
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime

log = logging.getLogger("staff_portal")

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_RE = re.compile(r"^_?[a-z][a-z0-9_]{0,39}$")
UPLOAD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
PARAM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
TEXT_TYPES = {".csv": "text/csv", ".tsv": "text/tab-separated-values", ".txt": "text/plain",
              ".json": "application/json"}
UPLOADS_KEPT = 5                   # newest files kept of each upload
UPLOAD_MAX = 2 * 1024 * 1024       # bytes, unless the report asks for less (or up to UPLOAD_CAP)
UPLOAD_CAP = 5 * 1024 * 1024
SETTINGS_MAX = 48 * 1024           # a report's settings, as JSON (the staff app takes 64 KB)
SETTINGS_DEPTH = 12
PARAM_MAX = 200                    # characters of one param value
FILE_ID_RE = re.compile(r"^([a-z][a-z0-9_]{0,39})-(\d{13})-([0-9a-f]{8})$")


@dataclasses.dataclass
class Report:
    key: str
    name: str
    description: str
    data: object                   # async fn(ctx, params) -> dict
    params: dict                   # {name: default}
    uploads: list                  # [{key, name, help, accept, exts, max_bytes}]
    settings: dict                 # {key: default}
    check_settings: object | None  # optional fn(settings) -> settings (raises ValueError)
    hidden: bool
    folder: str
    has_css: bool

    def public(self) -> dict:
        return {"key": self.key, "name": self.name, "description": self.description}


# ---------------------------------------------------------------- finding reports

def discover(folder: str = HERE) -> dict[str, Report]:
    """Every report folder that loads and declares REPORT properly, by key. A broken one is
    logged and left out; it never stops the add-on."""
    found = {}
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not KEY_RE.match(name) or not os.path.isfile(os.path.join(path, "report.py")):
            continue
        try:
            report = load(path)
            found[report.key] = report
        except Exception as err:  # noqa: BLE001 - one broken report mustn't stop the others
            log.warning("Report %s couldn't be loaded: %s", name, err)
    return found


def load(path: str) -> Report:
    """One report folder. Raises ValueError (or whatever its report.py raises)."""
    folder = os.path.basename(path.rstrip(os.sep))
    spec = importlib.util.spec_from_file_location(f"staff_reports.{folder}",
                                                  os.path.join(path, "report.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw = getattr(module, "REPORT", None)
    if not isinstance(raw, dict):
        raise ValueError("report.py has no REPORT dict.")
    hidden = folder.startswith("_")
    key = str(raw.get("key") or "")
    if key != folder.lstrip("_") or not KEY_RE.match(key) or key.startswith("_"):
        raise ValueError(f"REPORT key must be the folder's name ({folder.lstrip('_')}).")
    if not inspect.iscoroutinefunction(raw.get("data")):
        raise ValueError("REPORT data must be an async function: async def data(ctx, params).")
    if not os.path.isfile(os.path.join(path, "view.js")):
        raise ValueError("The folder has no view.js.")
    check = raw.get("check_settings")
    return Report(key=key, name=_one_line(raw.get("name"), 80) or key,
                  description=_one_line(raw.get("description"), 300),
                  data=raw["data"], params=_params(raw.get("params")),
                  uploads=_uploads(raw.get("uploads")), settings=_settings(raw.get("settings")),
                  check_settings=check if callable(check) else None, hidden=hidden,
                  folder=path, has_css=os.path.isfile(os.path.join(path, "view.css")))


def _one_line(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _params(raw) -> dict:
    """params as {name: default}: from a dict, a list of names, or a list of {key, default}."""
    items = raw.items() if isinstance(raw, dict) else [
        (p, None) if isinstance(p, str) else (p.get("key"), p.get("default"))
        for p in raw or [] if isinstance(p, (str, dict))]
    found = {}
    for name, default in items:
        if not isinstance(name, str) or not PARAM_RE.match(name):
            raise ValueError(f"Bad param name: {name!r}")
        found[name] = None if default is None else str(default)[:PARAM_MAX]
    return found


def _settings(raw) -> dict:
    """settings as {key: default}: from a dict, a list of keys, or a list of {key, default}."""
    items = raw.items() if isinstance(raw, dict) else [
        (s, None) if isinstance(s, str) else (s.get("key"), s.get("default"))
        for s in raw or [] if isinstance(s, (str, dict))]
    found = {}
    for key, default in items:
        if not isinstance(key, str) or not PARAM_RE.match(key):
            raise ValueError(f"Bad setting key: {key!r}")
        found[key] = default
    return found


def _uploads(raw) -> list[dict]:
    found = []
    for u in raw or []:
        if not isinstance(u, dict) or not UPLOAD_KEY_RE.match(str(u.get("key") or "")):
            raise ValueError("Each upload needs a key: lower-case letters, digits and _.")
        if any(f["key"] == u["key"] for f in found):
            raise ValueError(f"Upload {u['key']} is declared twice.")
        accept = str(u.get("accept") or ".csv")
        exts = sorted({t.strip().lower() for t in accept.split(",") if t.strip().lower() in TEXT_TYPES}
                      | {ext for ext, mime in TEXT_TYPES.items()
                         if mime in {t.strip().lower() for t in accept.split(",")}})
        if not exts:
            raise ValueError(f"Upload {u['key']} accepts no text file type (.csv, .tsv, .txt, .json).")
        size = u.get("max_bytes")
        size = min(size, UPLOAD_CAP) if isinstance(size, int) and size > 0 else UPLOAD_MAX
        found.append({"key": u["key"], "name": _one_line(u.get("name"), 80) or u["key"],
                      "help": _one_line(u.get("help"), 300), "accept": ",".join(exts),
                      "exts": exts, "max_bytes": size})
    return found


# ---------------------------------------------------------------- params and settings

def clean_params(report: Report, query) -> dict:
    """The params a report declared, from the request's query (others ignored): text, at most
    PARAM_MAX characters, else the default."""
    return {name: (str(query[name])[:PARAM_MAX] if name in query else default)
            for name, default in report.params.items()}


def _depth(value, level: int = 0) -> int:
    if isinstance(value, dict):
        return max([_depth(v, level + 1) for v in value.values()] or [level + 1])
    if isinstance(value, list):
        return max([_depth(v, level + 1) for v in value] or [level + 1])
    return level


def clean_settings(report: Report, data) -> dict:
    """Settings sent by someone who may manage the report: only the keys it declared, as JSON of
    at most SETTINGS_MAX bytes, then its own check_settings. Raises ValueError."""
    if not report.settings:
        raise ValueError("This report has no settings.")
    if not isinstance(data, dict):
        raise ValueError("Settings must be an object.")
    kept = {k: v for k, v in data.items() if k in report.settings}
    if _depth(kept) > SETTINGS_DEPTH:
        raise ValueError("Those settings are nested too deeply.")
    if report.check_settings:
        kept = report.check_settings(kept)
        if not isinstance(kept, dict):
            raise ValueError("The report's check_settings must return a dict.")
    if len(json.dumps(kept)) > SETTINGS_MAX:
        raise ValueError("Those settings are too big.")
    return kept


def stored_settings(report: Report, stored) -> dict:
    """What a report's data() and view see: its defaults, with what was saved over them."""
    saved = stored if isinstance(stored, dict) else {}
    return {k: saved.get(k, default) for k, default in report.settings.items()}


# ---------------------------------------------------------------- uploads

def upload_folder(data_dir: str, key: str) -> str:
    assert KEY_RE.match(key)
    return os.path.join(data_dir, "reports", key, "uploads")


def check_file(upload: dict, filename: str, raw: bytes) -> tuple[str, str]:
    """(a safe file name, its text) for a file someone uploads. Raises ValueError."""
    name = os.path.basename(str(filename or "").replace("\\", "/"))
    name = "".join(ch for ch in name if ch.isprintable()).strip()[:120] or "upload"
    ext = os.path.splitext(name)[1].lower()
    if ext not in upload["exts"]:
        raise ValueError(f"Choose a {' or '.join(upload['exts'])} file.")
    if not raw:
        raise ValueError("That file is empty.")
    if len(raw) > upload["max_bytes"]:
        raise ValueError(f"That file is too big (at most {_size(upload['max_bytes'])}).")
    text = decode(raw)
    if text is None:
        raise ValueError("That isn't a text file.")
    if ext == ".json":
        try:
            json.loads(text)
        except (ValueError, RecursionError):
            raise ValueError("That JSON file is damaged.") from None
    return name, text


def decode(raw: bytes) -> str | None:
    """A text file's text (UTF-8, else Windows' Western encoding), or None when it isn't text."""
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("cp1252")
        except UnicodeDecodeError:
            return None


def _size(n: int) -> str:
    return f"{n // 2**20} MB" if n >= 2**20 else f"{max(1, n // 1024)} KB"


def save_upload(data_dir: str, key: str, upload: dict, name: str, raw: bytes, by: str,
                now: float | None = None) -> dict:
    """Keep a checked file (check_file) and its details; the oldest beyond UPLOADS_KEPT go."""
    folder = upload_folder(data_dir, key)
    os.makedirs(folder, exist_ok=True)
    now = time.time() if now is None else now
    file_id = f"{upload['key']}-{int(now * 1000):013d}-{secrets.token_hex(4)}"
    meta = {"name": name, "uploaded_at": now, "by": str(by or "")[:120], "size": len(raw)}
    _write(os.path.join(folder, file_id + ".dat"), raw)
    _write(os.path.join(folder, file_id + ".json"), json.dumps(meta).encode())
    for old in _file_ids(folder, upload["key"])[UPLOADS_KEPT:]:
        for ext in (".dat", ".json"):
            try:
                os.remove(os.path.join(folder, old + ext))
            except OSError:
                pass
    return meta


def _write(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _file_ids(folder: str, upload_key: str) -> list[str]:
    """The files kept for one upload, newest first."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    ids = {n[:-4] for n in names if n.endswith(".dat")}
    return sorted((i for i in ids if (m := FILE_ID_RE.match(i)) and m[1] == upload_key),
                  reverse=True)


def uploads(data_dir: str, key: str, upload_key: str, text: bool = True) -> list[dict]:
    """An upload's files, newest first: [{name, uploaded_at, by, size, text}] (text left out
    when text is False)."""
    folder = upload_folder(data_dir, key)
    found = []
    for file_id in _file_ids(folder, upload_key)[:UPLOADS_KEPT]:
        try:
            with open(os.path.join(folder, file_id + ".json"), encoding="utf-8") as f:
                meta = json.load(f)
            item = {"name": str(meta.get("name") or "upload")[:120],
                    "uploaded_at": float(meta.get("uploaded_at") or 0),
                    "by": str(meta.get("by") or "")[:120], "size": int(meta.get("size") or 0)}
            if text:
                with open(os.path.join(folder, file_id + ".dat"), "rb") as f:
                    item["text"] = decode(f.read()) or ""
        except (OSError, ValueError, TypeError):
            continue
        found.append(item)
    return found


def all_uploads(data_dir: str, known: dict[str, Report]) -> dict:
    """Every kept upload of every report, for a migration file: {key: {upload key: [{name,
    uploaded_at, by, text}]}} (oldest first, so they're saved back in order)."""
    found = {}
    for key, report in known.items():
        for upload in report.uploads:
            files = uploads(data_dir, key, upload["key"])
            if files:
                found.setdefault(key, {})[upload["key"]] = [
                    {k: f[k] for k in ("name", "uploaded_at", "by", "text")} for f in reversed(files)]
    return found


def clean_uploads(raw, known: dict[str, Report]) -> dict:
    """The uploads in a migration file, checked as an upload is (anything that doesn't fit a
    report here is left out): {key: {upload key: [(name, bytes, by, uploaded_at)]}}."""
    found = {}
    for key, kinds in (raw.items() if isinstance(raw, dict) else []):
        report = known.get(key)
        if not report or not isinstance(kinds, dict):
            continue
        for upload in report.uploads:
            files = kinds.get(upload["key"])
            for f in (files if isinstance(files, list) else [])[-UPLOADS_KEPT:]:
                if not isinstance(f, dict) or not isinstance(f.get("text"), str):
                    continue
                raw_bytes = f["text"].encode()
                try:
                    name, _ = check_file(upload, f.get("name"), raw_bytes)
                except ValueError:
                    continue
                when = f.get("uploaded_at")
                when = float(when) if isinstance(when, (int, float)) and not isinstance(
                    when, bool) and 0 < when < 1e11 else time.time()
                found.setdefault(key, {}).setdefault(upload["key"], []).append(
                    (name, raw_bytes, str(f.get("by") or "")[:120], when))
    return found


def restore_uploads(data_dir: str, cleaned: dict, known: dict[str, Report]) -> int:
    """Save a migration's uploads (clean_uploads) here; returns how many."""
    count = 0
    for key, kinds in cleaned.items():
        uploads_of = {u["key"]: u for u in known[key].uploads}
        for upload_key, files in kinds.items():
            for name, raw, by, when in files:
                save_upload(data_dir, key, uploads_of[upload_key], name, raw, by, when)
                count += 1
    return count


# ---------------------------------------------------------------- what data() gets

class Context:
    """What a report's data(ctx, params) gets: ct (ctdata), uploads(key), settings, person
    {id, name, departments: [{id, name}], connecteam_user_id}, departments [{id, name,
    smart_group_id}], staff [{connecteam_user_id, department_id}] (the department each linked
    person has here), tz (a ZoneInfo), today (a date there) and manage (whether this person may
    manage the report)."""

    def __init__(self, *, ct, report: Report, data_dir: str, settings: dict, person: dict,
                 departments: list[dict], tz, manage: bool = False, staff=()):
        self.ct = ct
        self.report = report
        self.settings = settings
        self.person = person
        self.departments = departments
        self.staff = list(staff)
        self.tz = tz
        self.today = datetime.now(tz).date()
        self.manage = manage
        self._data_dir = data_dir

    def uploads(self, key: str) -> list[dict]:
        """An upload's newest files first: [{name, text, uploaded_at, by}]."""
        if not any(u["key"] == key for u in self.report.uploads):
            raise ValueError(f"This report declares no upload called {key}.")
        return [{k: f[k] for k in ("name", "text", "uploaded_at", "by")}
                for f in uploads(self._data_dir, self.report.key, key)]
