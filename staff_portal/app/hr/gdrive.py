"""Google Drive for the HR portal, through a small Apps Script "bridge" (bridge.gs) that the
owner pastes into script.google.com and deploys as a web app under their own Google account.
There's no Google Cloud project, key file or Workspace admin change: the script runs as the owner
and can use the shared drives they belong to, but only the ones chosen in Staff Access (it keeps
their IDs and refuses anything outside them).

Each request is a JSON POST to the web app's /exec address:
    {"v": PROTOCOL, "op": ..., "args": {...}, "ts": seconds, "nonce": ..., "sig": ...}
with sig = HMAC-SHA256(secret, "v|op|ts|nonce|" + sha256(canonical args JSON)). The canonical
JSON has sorted keys, no spaces and everything outside printable ASCII escaped, which bridge.gs
rebuilds from what it receives. The script refuses a request more than 5 minutes off, a nonce it
has seen in the last 10 minutes, and unknown ops. It answers {"ok": true, "result": ...} or
{"ok": false, "error": {"code", "message"}}, plus its own version.

Google answers the POST with a redirect to script.googleusercontent.com, fetched with GET. That's
done here by hand (aiohttp would follow any redirect), only to that host or back to the web app's
own address (the fake bridge in tests).

Limits: at most MAX_AT_ONCE requests at once (Apps Script allows 30 runs at once per account,
and the owner may use it for other things), timeouts (a script run may take 6 minutes), retries
with backoff on 429, 5xx and timeouts (each try is signed afresh), and files up to MAX_BYTES.
Every error is a BridgeError with a plain-English message and one of ERROR_CODES.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import time
import urllib.parse
import weakref

import aiohttp

PROTOCOL = 1          # the request format (bridge.gs's PROTOCOL)
SCRIPT_VERSION = 3    # the script written by script_source(); ping says which one is deployed
MIN_SCRIPT_VERSION = 1   # the oldest deployed script file operations still work with
PDF_VERSION = 2       # the oldest deployed script that makes PDFs (the pdf op)
MAX_BYTES = 25 * 1024 * 1024          # a file uploaded, downloaded or read
MAX_HTML = 2 * 1024 * 1024            # characters of HTML a PDF is made from
MAX_REPLY = 36 * 1024 * 1024          # a reply: a 25 MB file as base64 is about 33.4 MB
MAX_AT_ONCE = 2
SHORT_TIMEOUT = 60    # seconds: ping, setup, folders, rename, move, trash
LONG_TIMEOUT = 380    # files, reading text and the self-test (a script run may take 6 minutes)
RETRY_DELAYS = (2, 8)   # seconds before the 2nd and 3rd tries (plus a little randomness)
USER_AGENT = "HomeAssistant-StaffPortal-HR (+https://github.com/liamdayan/ha-staff-portal)"
REDIRECT_HOST = "script.googleusercontent.com"
OCR_TYPES = ("image/jpeg", "image/png", "image/gif", "application/pdf")
ERROR_CODES = ("auth", "outdated", "not_allowed", "too_big", "unavailable", "quota", "bad_reply")
SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.gs")
# For testing: the fake bridge's address (such as http://127.0.0.1:4255), accepted as a Web app
# URL besides script.google.com.
TEST_ORIGIN = os.environ.get("GOOGLE_SCRIPT_API", "").rstrip("/")

URL_RE = re.compile(r"https://script\.google\.com/(?:a/macros/[A-Za-z0-9.-]{1,253}/|macros/)"
                    r"s/[A-Za-z0-9_-]{20,200}/exec")
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
ID_RE = re.compile(r"[A-Za-z0-9_-]{10,200}")
PROP_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,40}")
MAX_PROPS = 10

# The script's own error codes, as this client's (bridge.gs says why in plain words).
SCRIPT_CODES = {"auth": "auth", "outdated": "outdated", "not_allowed": "not_allowed",
                "too_big": "too_big", "quota": "quota", "unavailable": "unavailable"}
OUTDATED = ("The script in Google is older than this version of Staff Access. In Staff Access, "
            "click Copy the script, paste it over the old one in script.google.com, then click "
            "Deploy, Manage deployments, Edit (the pencil), Version: New version, Deploy. The Web "
            "app URL stays the same.")
NOT_ANYONE = ("Google asked for a sign-in, so the web app isn't open to Staff Access. In "
              "script.google.com, click Deploy, Manage deployments, Edit (the pencil), set Who has "
              "access to Anyone, then click Deploy. If Anyone isn't offered, your Google Workspace "
              "doesn't allow it: see the user guide.")


class BridgeError(Exception):
    """A request to the Google Drive script failed. code is one of ERROR_CODES; the message
    says what happened in plain words."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _Retry(Exception):
    """A try that may work again in a moment (429, 5xx, a timeout): the error to raise if not."""

    def __init__(self, error: BridgeError, wait: float = 0.0):
        super().__init__(error.message)
        self.error = error
        self.wait = wait


# ---------------------------------------------------------------- the script and its settings

def new_secret() -> str:
    """A fresh secret for a new script (256 bits)."""
    return secrets.token_urlsafe(32)


def script_source(secret: str) -> str:
    """The script the owner pastes into script.google.com, with this install's secret and the
    script version written in."""
    if not SECRET_RE.fullmatch(secret or ""):
        raise ValueError("That secret doesn't look right.")
    with open(SCRIPT_PATH, encoding="utf-8") as f:
        text = f.read()
    return text.replace("{{SECRET}}", secret).replace("{{VERSION}}", str(SCRIPT_VERSION))


def clean_url(value) -> str:
    """The Web app URL as pasted, checked: https://script.google.com/macros/s/.../exec (or
    /a/macros/your-domain/s/.../exec on Google Workspace). "" stays "". Raises ValueError."""
    url = str(value or "").strip()
    if not url:
        return ""
    url = url.split("?", 1)[0].split("#", 1)[0]
    if URL_RE.fullmatch(url) or (TEST_ORIGIN and re.fullmatch(
            re.escape(TEST_ORIGIN) + r"/macros/s/[A-Za-z0-9_-]{4,200}/exec", url)):
        return url
    if url.endswith("/dev"):
        raise ValueError("That's the test address, which only works for you. Use the Web app URL "
                         "shown after Deploy, which ends in /exec.")
    raise ValueError("Paste the Web app URL shown after Deploy: it starts with "
                     "https://script.google.com/ and ends in /exec.")


def clean_drive_id(value) -> str:
    """A shared drive's ID as chosen in the dashboard ("" for none). Raises ValueError."""
    drive_id = str(value or "").strip()
    if drive_id and not ID_RE.fullmatch(drive_id):
        raise ValueError("Pick the shared drive from the list.")
    return drive_id


def bridge_from_settings(settings: dict) -> "Bridge | None":
    """The bridge as set up in Staff Access's settings, or None while it isn't (no Web app URL
    or no secret yet)."""
    try:
        url = clean_url(settings.get("hr_google_url"))
    except ValueError:
        return None
    secret = settings.get("hr_google_secret") or ""
    if not url or not SECRET_RE.fullmatch(secret):
        return None
    return Bridge(url, secret, shared_session)


def drive_ids(settings: dict) -> list[str]:
    """The drives the script may use, from the settings: the HR drive, then the restricted one
    if it's a drive of its own (otherwise restricted files go in a folder in the HR drive)."""
    ids = [settings.get("hr_google_drive") or "", settings.get("hr_google_restricted_drive") or ""]
    return [d for i, d in enumerate(ids) if d and d not in ids[:i]]


# ---------------------------------------------------------------- signing

def canonical(value) -> str:
    """args as signed: JSON with sorted keys, no spaces, and everything outside printable ASCII
    escaped (\\u00e9), which bridge.gs builds the same way from what it receives. Only text,
    whole numbers (within JavaScript's exact range), true, false, null, lists and objects."""
    _check_plain(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def _check_plain(value, depth: int = 0) -> None:
    if depth > 20:
        raise ValueError("Nested too deep.")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Keys must be text.")
            _check_plain(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_plain(item, depth + 1)
    elif isinstance(value, bool) or value is None or isinstance(value, str):
        pass
    elif isinstance(value, int):
        if abs(value) > 2 ** 53:
            raise ValueError("Numbers must fit JavaScript.")
    else:
        raise ValueError("Only text, whole numbers, true, false, null, lists and objects.")


def signature(secret: str, v: int, op: str, ts: int, nonce: str, args: dict) -> str:
    digest = hashlib.sha256(canonical(args).encode("ascii")).hexdigest()
    message = f"{v}|{op}|{ts}|{nonce}|{digest}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def signed_request(secret: str, op: str, args: dict, now: float | None = None) -> dict:
    """A request body, signed, with a fresh nonce."""
    ts = int(now if now is not None else time.time())
    nonce = secrets.token_urlsafe(18)
    return {"v": PROTOCOL, "op": op, "args": args, "ts": ts, "nonce": nonce,
            "sig": signature(secret, PROTOCOL, op, ts, nonce, args)}


# ---------------------------------------------------------------- shared session and limit

# One HTTP session and one limit of MAX_AT_ONCE per event loop (tests run several loops).
_sessions: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, aiohttp.ClientSession]" = \
    weakref.WeakKeyDictionary()
_gates: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = \
    weakref.WeakKeyDictionary()


def shared_session() -> aiohttp.ClientSession:
    loop = asyncio.get_running_loop()
    session = _sessions.get(loop)
    if session is None or session.closed:
        session = _sessions[loop] = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
    return session


def _gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = _gates[loop] = asyncio.Semaphore(MAX_AT_ONCE)
    return gate


async def close() -> None:
    """On the way out: close this loop's shared session."""
    session = _sessions.pop(asyncio.get_running_loop(), None)
    if session is not None:
        await session.close()


# ---------------------------------------------------------------- the client

class Bridge:
    """The Google Drive script at `url`, signing with `secret`. session_factory() gives the
    aiohttp session to use (kept open by whoever owns it: shared_session by default)."""

    def __init__(self, url: str, secret: str, session_factory=shared_session):
        self.url = url
        self.secret = secret
        self._session_factory = session_factory
        self.last_version: int | None = None   # the deployed script's, from its last reply

    # ------------------------------------------------ operations

    async def ping(self) -> dict:
        """{"version", "email", "drives": [{"id", "name"}], "allowed": [drive IDs]}. Raises
        BridgeError("outdated") when the deployed script is older than SCRIPT_VERSION."""
        result = await self._call("ping", {}, SHORT_TIMEOUT, min_version=SCRIPT_VERSION)
        drives = result.get("drives") if isinstance(result.get("drives"), list) else []
        return {"version": self.last_version, "email": _text(result.get("email"), 320),
                "drives": [{"id": d["id"], "name": _text(d.get("name"), 200) or d["id"]}
                           for d in drives[:500]
                           if isinstance(d, dict) and isinstance(d.get("id"), str)
                           and ID_RE.fullmatch(d["id"])],
                "allowed": [d for d in result.get("allowed") or []
                            if isinstance(d, str) and ID_RE.fullmatch(d)]}

    async def setup(self, drive_ids: list[str]) -> list[dict]:
        """Lets the script use these shared drives, and only these (the HR drive first)."""
        ids = [_id(d) for d in drive_ids]
        if not 1 <= len(ids) <= 2 or len(set(ids)) != len(ids):
            raise ValueError("One or two different shared drives.")
        result = await self._call("setup", {"drives": ids}, SHORT_TIMEOUT)
        return [{"id": d.get("id"), "name": _text(d.get("name"), 200)}
                for d in result.get("drives") or [] if isinstance(d, dict)]

    async def ensure_folder(self, parent_id: str, name: str, props: dict) -> str:
        """The ID of the folder in parent_id tagged with props (or, with no props, named name),
        made if it isn't there. Asking again never makes a second one."""
        result = await self._call("ensureFolder", {"parent": _id(parent_id), "name": _name(name),
                                                   "props": _props(props)}, SHORT_TIMEOUT)
        return _result_id(result)

    async def rename(self, file_id: str, name: str) -> None:
        await self._call("rename", {"id": _id(file_id), "name": _name(name)}, SHORT_TIMEOUT)

    async def move(self, file_id: str, parent_id: str) -> None:
        await self._call("move", {"id": _id(file_id), "parent": _id(parent_id)}, SHORT_TIMEOUT)

    async def upload(self, parent_id: str, name: str, mime: str, data: bytes, props: dict) -> dict:
        """Uploads a file: {"id", "name", "size", "webViewLink"}. props["upload_id"] makes it
        idempotent (a file with it in that folder is returned instead of a second copy); without
        one, it's made from the folder, name and content, so a retry after a timeout is safe."""
        _check_size(data)
        props = dict(props or {})
        if "upload_id" not in props:
            props["upload_id"] = hashlib.sha256(
                f"{parent_id}\n{name}\n".encode() + data).hexdigest()[:40]
        if not re.fullmatch(r"[a-z]+/[A-Za-z0-9.+_-]{1,100}", mime or "") or \
                mime.startswith("application/vnd.google-apps"):
            raise ValueError("Unsupported file type.")
        result = await self._call("upload", {
            "parent": _id(parent_id), "name": _name(name), "mimeType": mime,
            "data": base64.b64encode(data).decode(), "props": _props(props)}, LONG_TIMEOUT)
        size = result.get("size")
        return {"id": _result_id(result), "name": _text(result.get("name"), 300),
                "size": int(size) if isinstance(size, (int, str)) and str(size).isdigit() else len(data),
                "webViewLink": _text(result.get("webViewLink"), 500)}

    async def download(self, file_id: str) -> bytes:
        result = await self._call("download", {"id": _id(file_id)}, LONG_TIMEOUT)
        try:
            data = base64.b64decode(result.get("data") or "", validate=True)
        except (ValueError, TypeError):
            raise BridgeError("bad_reply", "Google sent the file in a form Staff Access can't "
                                           "read.") from None
        _check_size(data)
        return data

    async def trash(self, file_id: str) -> None:
        """Moves a file or folder to the shared drive's trash (Google empties it after 30 days)."""
        await self._call("trash", {"id": _id(file_id)}, SHORT_TIMEOUT)

    async def ocr(self, data: bytes, mime: str) -> str:
        """The text Google Drive reads from a photo or PDF. The script converts it to a Google
        Doc in the "_OCR scratch" folder of the HR drive, takes its text and deletes it straight
        away. Send a reduced copy: Google reads files up to about 2 MB best."""
        _check_size(data)
        if mime not in OCR_TYPES:
            raise ValueError("Google reads text from JPEG, PNG, GIF and PDF files only.")
        result = await self._call("ocr", {"data": base64.b64encode(data).decode(),
                                          "mimeType": mime}, LONG_TIMEOUT)
        text = result.get("text")
        if not isinstance(text, str):
            raise BridgeError("bad_reply", "Google didn't send the text back.")
        return text[:200_000]

    async def render_pdf(self, html: str) -> bytes:
        """A PDF of plain HTML (hr/pdfdoc.py makes it): the script turns it into a Google Doc in
        the "_OCR scratch" folder of the HR drive, saves that as a PDF and deletes it straight
        away. Raises BridgeError("outdated") while the deployed script is older than PDF_VERSION."""
        if not isinstance(html, str) or not html.strip() or len(html) > MAX_HTML:
            raise ValueError("A PDF is made from 1 character to 2 MB of HTML.")
        result = await self._call("pdf", {"html": html}, LONG_TIMEOUT, min_version=PDF_VERSION)
        try:
            data = base64.b64decode(result.get("data") or "", validate=True)
        except (ValueError, TypeError):
            raise BridgeError("bad_reply", "Google sent the PDF in a form Staff Access can't "
                                           "read.") from None
        _check_size(data)
        if not data.startswith(b"%PDF-"):
            raise BridgeError("bad_reply", "Google didn't send a PDF back.")
        return data

    async def self_test(self) -> list[dict]:
        """Test connection: [{"check", "ok", "detail"}] for signing in, the drive, making and
        removing a test folder and reading text from a test image. Never raises: a request that
        fails is one failed check."""
        try:
            result = await self._call("selfTest", {}, LONG_TIMEOUT)
        except BridgeError as err:
            what = {"auth": "Sign-in", "outdated": "Script version"}.get(err.code, "Reaching the script")
            return [{"check": what, "ok": False, "detail": err.message}]
        checks = [{"check": _text(c.get("check"), 80), "ok": c.get("ok") is True,
                   "detail": _text(c.get("detail"), 300)}
                  for c in result.get("checks") or [] if isinstance(c, dict)][:10]
        if not checks:
            raise BridgeError("bad_reply", "Google sent an unexpected reply.")
        if (self.last_version or 0) < SCRIPT_VERSION:
            checks.append({"check": "Script version", "ok": False, "detail": OUTDATED})
        return checks

    # ------------------------------------------------ requests

    async def _call(self, op: str, args: dict, timeout: float,
                    min_version: int = MIN_SCRIPT_VERSION) -> dict:
        error = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            if attempt:
                wait = max(RETRY_DELAYS[attempt - 1], error.wait)
                await asyncio.sleep(wait * random.uniform(1.0, 1.25))
            try:
                async with _gate():
                    reply = await self._once(signed_request(self.secret, op, args), timeout)
                break
            except _Retry as retry:
                error = retry
        else:
            raise error.error
        version = reply.get("version")
        self.last_version = version if isinstance(version, int) and not isinstance(version, bool) \
            else None
        if not reply.get("ok"):
            raise _script_error(reply.get("error"))
        if (self.last_version or 0) < min_version:
            raise BridgeError("outdated", OUTDATED)
        result = reply.get("result")
        if not isinstance(result, dict):
            raise BridgeError("bad_reply", "Google sent an unexpected reply.")
        return result

    async def _once(self, body: dict, timeout: float) -> dict:
        """One try: the POST, then the GET it's redirected to."""
        session = self._session_factory()
        limit = aiohttp.ClientTimeout(total=timeout, sock_connect=15)
        try:
            async with session.post(self.url, data=json.dumps(body),
                                    headers={"Content-Type": "application/json"},
                                    allow_redirects=False, timeout=limit) as resp:
                if resp.status in (302, 303):
                    target = self._redirect(resp.headers.get("Location", ""))
                else:
                    return await self._reply(resp)
            async with session.get(target, allow_redirects=False, timeout=limit) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    self._redirect(resp.headers.get("Location", ""))   # explains a sign-in
                    raise BridgeError("bad_reply", "Google sent Staff Access somewhere unexpected.")
                return await self._reply(resp)
        except (asyncio.TimeoutError, TimeoutError):
            raise _Retry(BridgeError("unavailable", "Google didn't answer in time. Staff Access "
                                                    "will try again later.")) from None
        except aiohttp.ClientError as err:
            raise _Retry(BridgeError("unavailable", "Can't reach Google: "
                                     f"{err or type(err).__name__}")) from None

    def _redirect(self, location: str) -> str:
        """Where a redirect may go: script.googleusercontent.com (Google's answer), or this web
        app's own address (the fake bridge). A sign-in page means the web app isn't open to
        Anyone."""
        target = urllib.parse.urljoin(self.url, location)
        parts, own = urllib.parse.urlsplit(target), urllib.parse.urlsplit(self.url)
        if (parts.scheme == "https" and parts.hostname == REDIRECT_HOST) or (
                parts.scheme == own.scheme and parts.netloc == own.netloc):
            return target
        if parts.hostname and parts.hostname.endswith("accounts.google.com"):
            raise BridgeError("not_allowed", NOT_ANYONE)
        raise BridgeError("bad_reply", "Google sent Staff Access somewhere unexpected.")

    async def _reply(self, resp: aiohttp.ClientResponse) -> dict:
        if resp.status == 429:
            raise _Retry(BridgeError("quota", "Google is busy (too many requests). Staff Access "
                                              "will try again later."), _retry_after(resp))
        if resp.status >= 500:
            raise _Retry(BridgeError("unavailable", f"Google had a problem (HTTP {resp.status}). "
                                                    "Staff Access will try again later."))
        if resp.status in (401, 403):
            raise BridgeError("not_allowed", NOT_ANYONE)
        if resp.status == 404:
            raise BridgeError("not_allowed", "There's no web app at that address. Check the Web "
                                             "app URL in Staff Access: copy it again after Deploy.")
        raw = bytearray()
        async for chunk in resp.content.iter_chunked(256 * 1024):
            raw.extend(chunk)
            if len(raw) > MAX_REPLY:
                raise BridgeError("too_big", "Google's reply was too big.")
        try:
            reply = json.loads(bytes(raw))
        except ValueError:
            reply = None
        if resp.status != 200 or not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
            page = bytes(raw[:4000]).decode("utf-8", "replace")
            if "Script function not found" in page:
                raise BridgeError("bad_reply", "Google says the script isn't there. Paste the "
                                               "script again, save it, and deploy a new version.")
            raise BridgeError("bad_reply", "Google answered with a page instead of the script's "
                                           f"reply (HTTP {resp.status}). Check the Web app URL, "
                                           "and that the script was deployed as a web app.")
        return reply


# ---------------------------------------------------------------- helpers

def _script_error(error) -> BridgeError:
    """The script's {"code", "message"} as a BridgeError (its message is already plain)."""
    if not isinstance(error, dict):
        return BridgeError("bad_reply", "Google sent an unexpected reply.")
    message = _text(error.get("message"), 400) or "Google Drive refused the request."
    code = SCRIPT_CODES.get(error.get("code"), "bad_reply")
    return BridgeError(code, OUTDATED if code == "outdated" else message)


def _retry_after(resp: aiohttp.ClientResponse) -> float:
    try:
        return min(60.0, max(0.0, float(resp.headers.get("Retry-After", 0))))
    except (ValueError, OverflowError):
        return 0.0


def _text(value, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _id(value) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError("That isn't a Google Drive ID.")
    return value


def _name(value) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    if not name or len(name) > 250:
        raise ValueError("A file or folder name needs 1 to 250 characters.")
    return name


def _props(props: dict) -> dict:
    """Tags for a file or folder: at most MAX_PROPS, short text (Google allows 124 bytes for a
    key and its value)."""
    if not isinstance(props, dict) or len(props) > MAX_PROPS:
        raise ValueError(f"At most {MAX_PROPS} tags.")
    clean = {}
    for key, value in props.items():
        value = str(value)
        if (not PROP_KEY_RE.fullmatch(str(key)) or not value
                or len(key.encode()) + len(value.encode()) > 120):
            raise ValueError("Tags must be short text.")
        clean[key] = value
    return clean


def _check_size(data: bytes) -> None:
    if len(data) > MAX_BYTES:
        raise BridgeError("too_big", "That file is bigger than 25 MB, which is the most Staff "
                                     "Access sends to Google Drive.")


def _result_id(result: dict) -> str:
    file_id = result.get("id")
    if not isinstance(file_id, str) or not ID_RE.fullmatch(file_id):
        raise BridgeError("bad_reply", "Google sent an unexpected reply.")
    return file_id
