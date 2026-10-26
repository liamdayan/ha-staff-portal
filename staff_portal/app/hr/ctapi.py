"""The HR portal's own Connecteam requests: a chat message from the custom publisher "Hot Lake
HR" (Communications hub; the chat.write and publishers.read permissions), and copying a confirmed
certificate onto the person's document in Connecteam, with its expiry date (Documents API: the HR
& Skills hub on the Expert plan; attachments.write and documents.write), and, once a day while
those copies are on, where each person stands on those documents (documents.read), so a card
added in Connecteam's own app becomes a to-do for HR. Certificate copies are off until HR turns
them on.

Every request goes through Staff Access's own Connecteam budget (connecteam.py: its caps, the
account's rate-limit headers and pauses) and HR's one smaller lane inside it (connecteam.hr_room
and hr_spend, shared with the HR portal's people sync):
- at most MINUTE_CAP requests a minute and DAY_CAP a day (counted across restarts);
- none once Staff Access as a whole has used STOP_SHARE of its daily allowance, so the time
  clock checks always have room.

HR's problems stay HR's. A refused permission, a missing plan feature or a bad key stops only
that HR feature (until Test or new settings), and a "too many requests" pauses only HR's lane. None
of it ever stops or pauses Staff Access's clock checks; only the account itself running low does
that, as it always has.

The HR side may use its own Connecteam app (HR settings: client_id and client_secret), so the
time clock's app never needs the extra permissions; without one it uses Staff Access's. Either
way it has its own access token. Errors are CTError, with a plain-English message and whether
trying again later may work (retry).
"""

import asyncio
import logging
import re
import time
from datetime import date, datetime, timezone

import aiohttp

import connecteam
from connecteam import Connecteam, ConnecteamError, HRBusy, _first_list

log = logging.getLogger("staff_portal")

MINUTE_CAP = connecteam.HR_MINUTE_CAP   # HR's requests a minute, at most (6)
DAY_CAP = connecteam.HR_DAILY_CAP       # ...and a day (500)
STOP_SHARE = connecteam.HR_STOP_SHARE   # none once Staff Access has used this share of its day
PUBLISHER = "Hot Lake HR"
PUBLISHER_TTL = 6 * 3600  # the publisher's ID is looked up again after this
MAX_CHAT = 490            # characters (Connecteam: under 500)
MAX_FILES = 10            # pages in one Connecteam document entry
MAX_FILE_BYTES = 25 * 1024 * 1024
STORAGE_TIMEOUT = 120     # seconds to send a file to Connecteam's storage
STATUS_PAGE = 200         # rows a page in the daily document status check...
STATUS_PAGES = 5          # ...and pages at most (1,000 rows)
FEATURES = ("chat", "documents")

NOT_ALLOWED = {
    "chat": "Connecteam refused the chat message. The HR connection needs the chat.write and "
            "publishers.read permissions, and your plan needs Connecteam's Communications hub.",
    "documents": "Connecteam refused the certificate copy. The HR connection needs the "
                 "attachments.write, documents.read and documents.write permissions, and your plan "
                 "needs the HR & Skills hub on Expert.",
}
# Connecteam's documents errors (extraData.errorCode): (code, message, worth retrying later).
DOCUMENT_ERRORS = {
    "DOCUMENT_NOT_ASSIGNED": ("not_assigned", "This person isn't assigned this document's pack in "
                              "Connecteam. Assign it there, then press Retry.", False),
    "ENTRY_PENDING_REVIEW": ("pending_review", "This person's card is waiting for approval in "
                             "Connecteam. Approve or reject it there, then press Retry.", False),
    "EXPIRATION_DATE_IN_PAST": ("expired", "This card has already expired, so Connecteam won't take "
                                "it. It's kept in Google Drive.", False),
    "EXPIRATION_NOT_SUPPORTED": ("setup", "The document in Connecteam doesn't have its own "
                                 "expiration date per person. In Connecteam, set its expiration to "
                                 "Per entry, then press Retry.", False),
    "DOCUMENT_TYPE_NOT_UPLOADABLE": ("setup", "That Connecteam document is one people sign, so a "
                                     "file can't be added to it. Use a plain upload document.", False),
    "DOCUMENT_NOT_FOUND": ("setup", "That document no longer exists in Connecteam. Choose it again "
                           "in HR settings.", False),
    "INSUFFICIENT_PERMISSION": ("not_allowed", "The HR connection doesn't have Upload or Approve "
                                "permission on this document's pack in Connecteam. Give it "
                                "permission there, then press Retry.", False),
    "FILE_NOT_FOUND": ("unavailable", "Connecteam lost the uploaded file. It will be sent again.", True),
}


class CTError(Exception):
    """An HR Connecteam request that didn't work. code: not_set_up, off, paused, stopped,
    not_allowed, no_publisher, no_user, not_assigned, pending_review, expired, setup,
    unavailable or bad_reply. retry: whether trying again later may work by itself. budget: it
    was HR's lane (paused, or used up for now), so a queue waits without counting a try."""

    def __init__(self, code: str, message: str, retry: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry = retry
        self.budget = code == "paused"


class HRConnecteam:
    def __init__(self, ct: Connecteam, get_settings=None):
        # get_settings() -> {"client_id", "client_secret" (HR's own app, optional), "publisher"
        # (the chat sender's name), "documents" (certificate copies on)}
        self.ct = ct
        self.get_settings = get_settings or (lambda: {})
        self._token: tuple[str, float, tuple] | None = None
        self._last_token_try = 0.0
        self._token_lock = asyncio.Lock()
        self._publisher: tuple[int, str, float] | None = None
        self.stopped = dict.fromkeys(FEATURES)    # feature -> why it stopped (None: working)

    # ------------------------------------------------------------ budget (connecteam.py's HR lane)

    def usage(self) -> dict:
        return self.ct.hr_usage()

    def room(self, n: int = 1) -> None:
        """Whether n more requests fit right now, in HR's lane and Staff Access's; raises
        CTError("paused") if not. Spends nothing."""
        try:
            self.ct.hr_room(n)
        except HRBusy as err:
            raise CTError("paused", str(err), True) from None

    def _with_token(self, n: int) -> int:
        """n requests, plus one for a new access token if it's needed."""
        fresh = self._token and self._token[1] > time.time()
        return n if fresh else n + 1

    def _spend(self) -> None:
        """Count one request in HR's lane and Staff Access's budget, or refuse it."""
        try:
            self.ct.hr_spend(own_keys=True)   # a halt is about Staff Access's keys: see _credentials
        except ConnecteamError as err:
            raise CTError("paused", str(err), True) from None

    def _too_many(self, resp: aiohttp.ClientResponse) -> CTError:
        """A 429: HR's lane waits (backing off), never Staff Access's clock checks."""
        return CTError("paused", str(self.ct.hr_too_many(resp)), True)

    # ------------------------------------------------------------ requests

    def _credentials(self) -> tuple[str, str]:
        enabled, client_id, secret, _, _ = self.ct._settings()
        if not enabled:
            raise CTError("not_set_up", "Connecteam is turned off in Staff Access (Settings, "
                                        "Connections), so the HR portal can't use it.")
        s = self.get_settings() or {}
        own = (str(s.get("client_id") or ""), str(s.get("client_secret") or ""))
        if all(own):
            return own
        if client_id and secret:
            if self.ct.halted:   # the same keys Connecteam refused for Staff Access
                raise CTError("stopped", self.ct.halted)
            return client_id, secret
        raise CTError("not_set_up", "Enter the Connecteam Client ID and Secret in Staff Access first.")

    def _check_stopped(self, feature: str) -> None:
        if self.stopped[feature]:
            raise CTError("stopped", self.stopped[feature] + " (Stopped until the HR Connecteam "
                                                             "settings change or Test is pressed.)")

    def _stop(self, feature: str | None, reason: str) -> None:
        for f in (feature,) if feature else FEATURES:
            if self.stopped[f] != reason:
                log.warning("HR portal, Connecteam %s: %s Stopped.", f, reason)
            self.stopped[f] = reason

    async def _access_token(self) -> str:
        async with self._token_lock:
            creds = self._credentials()
            if self._token and self._token[2] == creds and self._token[1] > time.time():
                return self._token[0]
            if time.time() - self._last_token_try < 60:
                raise CTError("paused", "Waiting a minute before asking Connecteam for a new "
                                        "access token.", True)
            self._spend()   # refused: nothing was asked, so no wait
            self._last_token_try = time.time()
            try:
                async with self.ct.session.post(
                        f"{self.ct._api()}/oauth/v1/token", data={"grant_type": "client_credentials"},
                        headers={"Authorization": aiohttp.encode_basic_auth(*creds)}) as resp:
                    self.ct._read_limits(resp)
                    if resp.status == 429:
                        raise self._too_many(resp)
                    status, body = resp.status, await _json(resp)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise CTError("unavailable", f"Can't reach Connecteam: {err or type(err).__name__}",
                              True) from None
            if status in (400, 401, 403):
                reason = (f"Connecteam didn't accept the HR connection's Client ID and Secret "
                          f"(HTTP {status}). Check them.")
                self._stop(None, reason)
                raise CTError("not_allowed", reason)
            if status >= 400 or not isinstance(body, dict) or not body.get("access_token"):
                raise CTError("unavailable", f"Connecteam couldn't issue an access token (HTTP {status}).",
                              True)
            ttl = body.get("expires_in")
            ttl = min(max(ttl, 360), 86400) if isinstance(ttl, (int, float)) and ttl == ttl else 86400
            self._token = (str(body["access_token"]), time.time() + ttl - 300, creds)
            return self._token[0]

    async def _request(self, method: str, path: str, feature: str, body: dict | None = None,
                       params: dict | None = None) -> dict:
        for attempt in (1, 2):
            token = await self._access_token()
            self._spend()
            try:
                async with self.ct.session.request(
                        method, f"{self.ct._api()}{path}", json=body, params=params,
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}) as resp:
                    # The account's own headers: they pause everything only when the account
                    # itself runs low, as for Staff Access's own requests.
                    self.ct._read_limits(resp)
                    if resp.status == 429:
                        raise self._too_many(resp)
                    if resp.status == 401 and attempt == 1:
                        self._token = None   # expired or revoked: a new one, once
                        self._last_token_try = 0.0
                        continue
                    status, reply = resp.status, await _json(resp)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise CTError("unavailable", f"Can't reach Connecteam: {err or type(err).__name__}",
                              True) from None
            if status >= 400:
                raise self._refusal(status, reply, feature)
            data = reply.get("data", reply) if isinstance(reply, dict) else None
            if not isinstance(data, dict):
                raise CTError("bad_reply", "Connecteam sent an unexpected reply.", True)
            self.ct.hr_failures = 0
            return data
        reason = "Connecteam didn't accept the HR connection's access token."
        self._stop(None, reason)
        raise CTError("not_allowed", reason)

    def _refusal(self, status: int, reply, feature: str) -> CTError:
        extra = reply.get("extraData") if isinstance(reply, dict) else None
        code = extra.get("errorCode") if isinstance(extra, dict) else None
        if code in DOCUMENT_ERRORS:
            kind, message, retry = DOCUMENT_ERRORS[code]
            return CTError(kind, message, retry)
        if status in (401, 403):
            self._stop(feature, NOT_ALLOWED[feature])
            return CTError("not_allowed", NOT_ALLOWED[feature])
        detail = str(reply.get("detail") or reply.get("error") or "") if isinstance(reply, dict) else ""
        if status == 404 and "sender" in detail.lower():
            self._publisher = None
            return CTError("no_publisher", self._no_publisher())
        if status == 404:
            return CTError("no_user", "Connecteam doesn't have this person, or they're suspended.")
        if status >= 500:
            return CTError("unavailable", f"Connecteam had a problem (HTTP {status}). It will be "
                                          "tried again later.", True)
        return CTError("bad_reply", f"Connecteam refused the request (HTTP {status}).")

    def _no_publisher(self) -> str:
        return (f'Connecteam has no custom publisher named "{self._publisher_name()}". Add it in '
                "Connecteam under Settings, Feed settings, Custom publishers.")

    def _publisher_name(self) -> str:
        return str((self.get_settings() or {}).get("publisher") or PUBLISHER).strip()[:80]

    # ------------------------------------------------------------ chat

    async def publisher_id(self) -> int:
        """The chat sender's ID in Connecteam (looked up by name, kept PUBLISHER_TTL)."""
        name = self._publisher_name()
        if self._publisher and self._publisher[1] == name and time.time() - self._publisher[2] < PUBLISHER_TTL:
            return self._publisher[0]
        data = await self._request("GET", "/publishers/v1/publishers", "chat", params={"limit": 100})
        for p in _first_list(data, "publishers"):
            if (isinstance(p, dict) and isinstance(p.get("id"), int) and not isinstance(p["id"], bool)
                    and str(p.get("name") or "").strip().lower() == name.lower()):
                self._publisher = (p["id"], name, time.time())
                return p["id"]
        raise CTError("no_publisher", self._no_publisher())

    async def send_chat(self, user_id, text: str) -> None:
        """A private chat message to one person, from the HR publisher. One request (two when the
        publisher's ID has to be looked up)."""
        self._check_stopped("chat")
        uid = _user_id(user_id)
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(text or "")).strip()
        if not text or len(text) > MAX_CHAT:
            raise ValueError(f"A chat message needs 1 to {MAX_CHAT} characters.")
        cached = self._publisher and self._publisher[1] == self._publisher_name()
        self.room(self._with_token(1 if cached else 2))
        sender = await self.publisher_id()
        await self._request("POST", f"/chat/v1/conversations/privateMessage/{uid}", "chat",
                            body={"senderId": sender, "text": text})

    # ------------------------------------------------------------ documents

    async def list_documents(self) -> list[dict]:
        """Connecteam's documents ([{"id", "name", "type", "expiration"}]), to choose which one a
        kind of certificate goes to. One request."""
        self._check_stopped("documents")
        data = await self._request("GET", "/documents/v1/documents", "documents")
        found = []
        for d in _first_list(data, "documents")[:1000]:
            if not isinstance(d, dict) or not isinstance(d.get("documentId"), int):
                continue
            expiration = d.get("expiration") if isinstance(d.get("expiration"), dict) else {}
            found.append({"id": d["documentId"], "name": str(d.get("name") or "")[:120],
                          "type": d.get("documentType"), "expiration": expiration.get("type")})
        return found

    async def user_documents(self, document_ids) -> list[dict]:
        """Where each person stands on these Connecteam documents (up to 100), for the daily
        status check: [{"user_id", "document_id", "status" (pendingUserUpload, pendingApproval,
        approved, rejected, ...), "uploaded_at", "expires_at" (epoch seconds or None)}]. One
        request per STATUS_PAGE rows: about one a day for 100 people with two cards each."""
        self._check_stopped("documents")
        ids = sorted({d for d in document_ids if isinstance(d, int) and not isinstance(d, bool)
                      and 0 < d < 2**53})[:100]
        found = []
        for page in range(STATUS_PAGES if ids else 0):
            self.room(self._with_token(1))
            data = await self._request("GET", "/documents/v1/user-documents", "documents", params=[
                *(("documentIds", i) for i in ids), ("limit", STATUS_PAGE),
                ("offset", page * STATUS_PAGE)])
            rows = _first_list(data, "documents")
            for r in rows:
                if not isinstance(r, dict) or not all(_whole(r.get(k)) for k in ("userId", "documentId")):
                    continue
                found.append({"user_id": r["userId"], "document_id": r["documentId"],
                              "status": str(r.get("status") or "")[:40],
                              "uploaded_at": r["uploadedAt"] if _whole(r.get("uploadedAt")) else None,
                              "expires_at": r["expiresAt"] if _whole(r.get("expiresAt")) else None})
            if len(rows) < STATUS_PAGE:
                break
        return found

    async def upload_certificate(self, user_id, document_id: int, files: list,
                                 expires: date | None) -> dict:
        """Puts a confirmed certificate onto the person's Connecteam document: each file (name,
        MIME type, bytes; up to MAX_FILES pages) is uploaded (generate-upload-url, the file to
        storage, complete-upload), then the entry is set with the expiry date. Two requests a
        page plus one. Returns {"status" ("approved" or "pendingApproval"), "expires_at",
        "file_ids"}."""
        if not (self.get_settings() or {}).get("documents"):
            raise CTError("off", "Copying certificates to Connecteam is turned off in HR settings.")
        self._check_stopped("documents")
        uid = _user_id(user_id)
        if isinstance(document_id, bool) or not isinstance(document_id, int) or document_id <= 0:
            raise ValueError("Choose the Connecteam document.")
        if not 1 <= len(files) <= MAX_FILES:
            raise ValueError(f"A certificate has 1 to {MAX_FILES} pages.")
        for name, mime, data in files:
            if not name or not re.fullmatch(r"[a-z]+/[A-Za-z0-9.+_-]{1,100}", mime or "") or \
                    len(data) > MAX_FILE_BYTES:
                raise ValueError("A page must be a named file of up to 25 MB.")
        expires_at = None
        if expires:
            # Noon UTC, so it's the same day across the US.
            expires_at = int(datetime(expires.year, expires.month, expires.day, 12,
                                      tzinfo=timezone.utc).timestamp())
            if expires_at <= time.time():
                kind, message, _ = DOCUMENT_ERRORS["EXPIRATION_DATE_IN_PAST"]
                raise CTError(kind, message)
        self.room(self._with_token(2 * len(files) + 1))   # all of it now, never left half done
        file_ids = []
        for name, mime, data in files:
            up = await self._request("POST", "/attachments/v1/files/generate-upload-url", "documents",
                                     body={"fileName": str(name)[:200], "fileTypeHint": mime,
                                           "featureType": "documents"})
            file_id, url = up.get("fileId"), up.get("uploadFileUrl")
            if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", file_id) or \
                    not isinstance(url, str) or not _storage_url(url):
                raise CTError("bad_reply", "Connecteam sent an unexpected reply.", True)
            await self._to_storage(url, mime, data)
            await self._request("PUT", f"/attachments/v1/files/complete-upload/{file_id}", "documents")
            file_ids.append(file_id)
        body = {"fileIds": file_ids}
        if expires_at:
            body["expiresAt"] = expires_at
        row = await self._request("PUT", f"/documents/v1/users/{uid}/documents/{document_id}/entry",
                                  "documents", body=body)
        return {"status": row.get("status"), "expires_at": row.get("expiresAt"), "file_ids": file_ids}

    async def _to_storage(self, url: str, mime: str, data: bytes) -> None:
        """The file itself goes to Connecteam's storage (a pre-signed address that works for 5
        minutes), not to its API: no key is sent and no request is counted."""
        try:
            async with self.ct.session.put(url, data=data, headers={"Content-Type": mime},
                                           timeout=aiohttp.ClientTimeout(total=STORAGE_TIMEOUT)) as resp:
                status = resp.status
                await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise CTError("unavailable", f"Couldn't send the file to Connecteam: "
                                         f"{err or type(err).__name__}", True) from None
        if status >= 400:
            raise CTError("unavailable", f"Connecteam's file storage didn't take the file (HTTP "
                                         f"{status}). It will be sent again.", True)

    # ------------------------------------------------------------ test and status

    async def test(self) -> dict:
        """The HR settings' Test: the chat sender, and the documents (each {"ok", "detail"}).
        Lifts a stop first. Two requests (plus the access token)."""
        self.stopped = dict.fromkeys(FEATURES)
        self._publisher = None
        results = {}
        for feature, check in (("chat", self._test_chat), ("documents", self._test_documents)):
            try:
                results[feature] = {"ok": True, "detail": await check()}
            except CTError as err:
                results[feature] = {"ok": False, "detail": err.message}
        return results

    async def _test_chat(self) -> str:
        await self.publisher_id()
        return f'Chat messages can be sent as "{self._publisher_name()}".'

    async def _test_documents(self) -> str:
        docs = await self.list_documents()
        return f"Connecteam's documents can be read ({len(docs)} found)."

    def forget(self) -> None:
        """HR's Connecteam settings changed: a new token and publisher, and stops lifted (pauses
        for rate limits stay)."""
        self._token = None
        self._last_token_try = 0.0
        self._publisher = None
        self.stopped = dict.fromkeys(FEATURES)
        self.ct.hr_failures = 0

    def status(self) -> dict:
        now, ct = time.time(), self.ct
        return {"used_today": self.usage()["count"], "day_cap": DAY_CAP, "minute_cap": MINUTE_CAP,
                "paused": ct.hr_pause_reason if ct.hr_paused_until > now else None,
                "paused_until": ct.hr_paused_until if ct.hr_paused_until > now else None,
                "stopped": dict(self.stopped)}


# ---------------------------------------------------------------- the add-on's one lane

default: HRConnecteam | None = None


def configure(ct: Connecteam, get_settings=None) -> HRConnecteam:
    """Sets up the connection the module-level calls use (main.py does this at start)."""
    global default
    default = HRConnecteam(ct, get_settings)
    return default


def lane() -> HRConnecteam:
    if default is None:
        raise CTError("not_set_up", "The HR portal's Connecteam connection isn't set up.")
    return default


async def send_chat(user_id, text: str) -> None:
    await lane().send_chat(user_id, text)


async def upload_certificate(user_id, document_id: int, files: list, expires: date | None) -> dict:
    return await lane().upload_certificate(user_id, document_id, files, expires)


# ---------------------------------------------------------------- helpers

def _user_id(value) -> int:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{1,15}", text) or int(text) <= 0:
        raise ValueError("That isn't a Connecteam user ID.")
    return int(text)


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _storage_url(url: str) -> bool:
    """A pre-signed storage address: https (plain http only against the test Connecteam)."""
    return url.startswith("https://") or bool(connecteam.API_OVERRIDE and
                                              url.startswith("http://127.0.0.1"))


async def _json(resp: aiohttp.ClientResponse):
    try:
        return await resp.json(content_type=None)
    except ValueError:
        return None
