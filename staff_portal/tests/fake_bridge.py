"""A stand-in for the HR portal's Google Drive script (app/hr/bridge.gs), speaking the same
protocol over HTTP: a signed JSON POST to .../exec, answered with a 302 to a one-time address
fetched with GET (as Google does, through script.googleusercontent.com), with shared drives kept
in memory. For the add-on's tests and for browser runs; nothing here talks to Google.

    python fake_bridge.py --port 4255 --secret <the secret in the copied script>

then set GOOGLE_SCRIPT_API=http://127.0.0.1:4255 for the add-on and paste
http://127.0.0.1:4255/macros/s/FAKE-bridge/exec as the Web app URL.

Reading text (ocr) is faked: the text registered for those exact bytes (set_ocr_text), else a
"%OCR-TEXT:" line in the file (a PDF keeps it, byte for byte), else the sidecar of a look-alike
image in --ocr-dir (card.jpg and card.jpg.txt: matched by a small grayscale thumbnail, so a
re-encoded or slightly smaller copy still matches), else the next text queued with
POST /fake/ocr-text {"text": ...}, else --ocr-text.

Making a PDF (pdf) is faked too: a small, valid one-font PDF holding the HTML's text, line by
line (html_text), so tests can look for what it says. The HTML sent is kept in .pdfs.
"""

import argparse
import asyncio
import base64
import hashlib
import html as html_lib
import io
import re
import json
import os
import secrets
import sys
import time

from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
from hr import gdrive  # noqa: E402

FOLDER = "application/vnd.google-apps.folder"
HR_DRIVE = "0AHrDriveAAAAAAAAAAAA"
RESTRICTED_DRIVE = "0ARestrictedAAAAAAAAA"
OTHER_DRIVE = "0AOtherDriveAAAAAAAAA"
DRIVES = {HR_DRIVE: "HR Employee Files", RESTRICTED_DRIVE: "HR Restricted",
          OTHER_DRIVE: "Marketing"}
SELF_TEST_TEXT = "EXPIRES 06/14/2029"
OP_VERSIONS = {"pdf": gdrive.PDF_VERSION}   # ops a script older than this doesn't have


class Refused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class FakeBridge:
    def __init__(self, secret: str, email: str = "owner@hotlake.example", drives: dict | None = None,
                 ocr_dir: str | None = None, ocr_text: str = ""):
        self.secret = secret
        self.email = email
        self.drives = dict(DRIVES if drives is None else drives)
        self.allowed: list[str] = []
        self.files: dict[str, dict] = {}
        self.version = gdrive.SCRIPT_VERSION
        self.nonces: dict[str, float] = {}
        self.pending: dict[str, bytes] = {}
        self.ops: list[str] = []                 # ops answered, in order
        self.methods: list[tuple[str, str]] = []  # (method, path) of every request
        self.failures: list[dict] = []           # queued: see fail_next
        self.redirect_to: str | None = None      # send the 302 here instead (tests the guard)
        self.delay = 0.0
        self.active = self.most_at_once = 0
        self.ocr_texts: dict[str, str] = {}
        self.ocr_queue: list[str] = []
        self.ocr_default = ocr_text
        self.ocr_images: list[tuple[int, str]] = []
        self.ocr_calls = 0
        self.pdfs: list[str] = []                # the HTML of every PDF made
        if ocr_dir:
            self.load_ocr_dir(ocr_dir)
        self._ids = 0

    # ------------------------------------------------ test controls

    def fail_next(self, status: int | None = None, code: str | None = None, message: str = "Failed.",
                  op: str | None = None, times: int = 1, page: str | None = None,
                  after_nonce: bool = False) -> None:
        """The next `times` requests (for `op`, or any) get this: an HTTP status, a page
        instead of JSON, or the script's {"code", "message"} error. after_nonce: only once the
        request's nonce was used up (a retry must be signed afresh)."""
        for _ in range(times):
            self.failures.append({"status": status, "code": code, "message": message, "op": op,
                                  "page": page, "after_nonce": after_nonce})

    def set_ocr_text(self, data: bytes, text: str) -> None:
        self.ocr_texts[hashlib.sha256(data).hexdigest()] = text

    def load_ocr_dir(self, folder: str) -> None:
        for name in sorted(os.listdir(folder)):
            sidecar = os.path.join(folder, name + ".txt")
            if name.endswith(".txt") or not os.path.exists(sidecar):
                continue
            with open(os.path.join(folder, name), "rb") as f, open(sidecar, encoding="utf-8") as t:
                data, text = f.read(), t.read()
            self.set_ocr_text(data, text)
            print_hash = _thumb_hash(data)
            if print_hash is not None:
                self.ocr_images.append((print_hash, text))

    def add(self, name: str, parent: str, mime: str = FOLDER, data: bytes | None = None,
            props: dict | None = None, public: dict | None = None) -> dict:
        """A file or folder put there by hand (or by another app)."""
        self._ids += 1
        file = {"id": f"fakefile{self._ids:08d}", "name": name, "mimeType": mime,
                "parents": [parent], "driveId": self._drive_of(parent), "trashed": False,
                "appProperties": dict(props or {}), "properties": dict(public or props or {}),
                "data": data, "modified": time.time()}
        self.files[file["id"]] = file
        return file

    def children(self, parent: str) -> list[dict]:
        return [f for f in self.files.values() if parent in f["parents"] and not f["trashed"]]

    # ------------------------------------------------ HTTP

    def app(self) -> web.Application:
        app = web.Application(client_max_size=40 * 1024 * 1024)
        app.router.add_get(r"/macros/s/{id:[A-Za-z0-9_-]+}/exec", self.get_page)
        app.router.add_post(r"/macros/s/{id:[A-Za-z0-9_-]+}/exec", self.post)
        app.router.add_get("/macros/echo", self.echo)
        app.router.add_post("/fake/ocr-text", self.queue_ocr_text)
        app.router.add_get("/fake/files", self.list_files)
        return app

    async def get_page(self, request: web.Request):
        self.methods.append(("GET", request.path))
        return web.Response(text="Staff Access HR bridge: it's working. It only answers requests "
                                 "from Staff Access.")

    async def post(self, request: web.Request):
        self.methods.append(("POST", request.path))
        self.active += 1
        self.most_at_once = max(self.most_at_once, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            body = await request.text()
            failure = self._failure(body, before_nonce=True)
            if failure:
                return failure
            reply = self.handle(body)
            failure = self._failure(body, before_nonce=False)
            if failure:
                return failure
        finally:
            self.active -= 1
        key = secrets.token_urlsafe(24)
        self.pending[key] = json.dumps(reply).encode()
        location = (self.redirect_to or "/macros/echo") + f"?user_content_key={key}&lib=fake"
        raise web.HTTPFound(location)

    async def echo(self, request: web.Request):
        self.methods.append(("GET", request.path))
        body = self.pending.pop(request.query.get("user_content_key", ""), None)
        if body is None:
            return web.Response(status=404, text="Sorry, unable to open the file at this time.")
        return web.Response(body=body, content_type="application/json")

    async def queue_ocr_text(self, request: web.Request):
        data = await request.json()
        self.ocr_queue.append(str(data.get("text") or ""))
        return web.json_response({"queued": len(self.ocr_queue)})

    async def list_files(self, request: web.Request):
        return web.json_response({"allowed": self.allowed, "files": [
            {k: v for k, v in f.items() if k != "data"} | {"size": len(f["data"] or b"")}
            for f in self.files.values()]})

    def _failure(self, body: str, before_nonce: bool) -> web.Response | None:
        if not self.failures:
            return None
        try:
            op = json.loads(body).get("op")
        except (ValueError, AttributeError):
            op = None
        failure = self.failures[0]
        if (failure["op"] and failure["op"] != op) or failure["after_nonce"] == before_nonce:
            return None
        self.failures.pop(0)
        if failure["status"]:
            return web.Response(status=failure["status"], text="Error", headers={"Retry-After": "0"})
        if failure["page"] is not None:
            return web.Response(text=failure["page"], content_type="text/html")
        reply = json.dumps({"ok": False, "error": {"code": failure["code"],
                                                   "message": failure["message"]},
                            "version": self.version}).encode()
        key = secrets.token_urlsafe(24)
        self.pending[key] = reply
        raise web.HTTPFound(f"/macros/echo?user_content_key={key}")

    # ------------------------------------------------ the script's logic

    def handle(self, body: str) -> dict:
        try:
            result = self._handle(body)
            reply = {"ok": True, "result": result}
        except Refused as err:
            reply = {"ok": False, "error": {"code": err.code, "message": err.message}}
        reply["version"] = self.version
        return reply

    def _handle(self, body: str) -> dict:
        try:
            req = json.loads(body)
            op, args, ts, nonce = req["op"], req["args"], req["ts"], req["nonce"]
            expected = gdrive.signature(self.secret, req["v"], op, ts, nonce, args)
        except (ValueError, KeyError, TypeError):
            raise Refused("auth", "That request isn't from Staff Access.") from None
        if not isinstance(req.get("sig"), str) or not secrets.compare_digest(expected, req["sig"]):
            raise Refused("auth", "The secret doesn't match. In Staff Access, click Copy the "
                                  "script, paste it here over the old one, and deploy a new version.")
        if abs(time.time() - ts) > 300:
            raise Refused("auth", "The request's time is more than 5 minutes off.")
        now = time.time()
        self.nonces = {n: t for n, t in self.nonces.items() if now - t < 600}
        if nonce in self.nonces:
            raise Refused("auth", "That request was already used.")
        self.nonces[nonce] = now
        handler = getattr(self, f"op_{op}", None)
        if req["v"] != gdrive.PROTOCOL or handler is None or self.version < OP_VERSIONS.get(op, 0):
            raise Refused("outdated", "This script is older than Staff Access.")
        self.ops.append(op)
        return handler(args)

    def _drive_of(self, parent: str) -> str | None:
        if parent in self.drives:
            return parent
        file = self.files.get(parent)
        return file["driveId"] if file else None

    def _in_drives(self, file_id, folder_only: bool = False) -> dict:
        if not isinstance(file_id, str) or not gdrive.ID_RE.fullmatch(file_id):
            raise Refused("bad_request", "That isn't a Google Drive ID.")
        if not self.allowed:
            raise Refused("not_allowed", "Pick the HR shared drive in Staff Access first.")
        if file_id in self.allowed:
            return {"id": file_id, "driveId": file_id, "mimeType": FOLDER, "parents": [],
                    "top": True, "trashed": False}
        file = self.files.get(file_id)
        if file is None:
            raise Refused("not_allowed", "Google Drive has no such file or folder in the HR drives.")
        if file["driveId"] not in self.allowed:
            raise Refused("not_allowed", "That file or folder isn't in the HR drives.")
        if folder_only and (file["mimeType"] != FOLDER or file["trashed"]):
            raise Refused("bad_request", "That isn't a folder in the HR drives.")
        return file

    def _find(self, parent: str, props: dict, kind: str | None, name: str | None = None):
        for f in self.children(parent):
            if kind == "folder" and f["mimeType"] != FOLDER or kind == "file" and f["mimeType"] == FOLDER:
                continue
            if props and all(f["appProperties"].get(k) == v or f["properties"].get(k) == v
                             for k, v in props.items()):
                return f
            if not props and f["name"] == name:
                return f
        return None

    @staticmethod
    def _view(f: dict) -> dict:
        return {"id": f["id"], "name": f["name"], "size": len(f["data"] or b""),
                "webViewLink": f"https://drive.google.com/file/d/{f['id']}/view"}

    @staticmethod
    def _bytes(data) -> bytes:
        if not isinstance(data, str) or len(data) > -(-gdrive.MAX_BYTES // 3) * 4:
            raise Refused("too_big", "That file is bigger than 25 MB.")
        try:
            return base64.b64decode(data, validate=True)
        except ValueError:
            raise Refused("bad_request", "The file didn't arrive whole.") from None

    def op_ping(self, args):
        return {"version": self.version, "email": self.email,
                "drives": [{"id": k, "name": v} for k, v in self.drives.items()],
                "allowed": list(self.allowed)}

    def op_setup(self, args):
        ids = args.get("drives")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 2 or len(set(ids)) != len(ids):
            raise Refused("bad_request", "Pick one or two different shared drives.")
        for drive_id in ids:
            if drive_id not in self.drives:
                raise Refused("not_allowed", "Google Drive has no such file or folder in the HR drives.")
        self.allowed = list(ids)
        return {"drives": [{"id": d, "name": self.drives[d]} for d in ids]}

    def op_ensureFolder(self, args):
        parent = self._in_drives(args.get("parent"), True)
        props = args.get("props") or {}
        found = self._find(parent["id"], props, "folder", args.get("name"))
        if found:
            return {"id": found["id"], "name": found["name"], "created": False}
        folder = self.add(args["name"], parent["id"], props=props)
        return {"id": folder["id"], "name": folder["name"], "created": True}

    def op_rename(self, args):
        file = self._in_drives(args.get("id"))
        if file.get("top"):
            raise Refused("bad_request", "A shared drive can't be renamed from here.")
        file["name"] = args["name"]
        return {"id": file["id"], "name": file["name"]}

    def op_move(self, args):
        file = self._in_drives(args.get("id"))
        parent = self._in_drives(args.get("parent"), True)
        if file.get("top"):
            raise Refused("bad_request", "A shared drive can't be moved.")
        file["parents"] = [parent["id"]]
        file["driveId"] = parent["driveId"]
        return {"id": file["id"], "parent": parent["id"]}

    def op_upload(self, args):
        parent = self._in_drives(args.get("parent"), True)
        props = args.get("props") or {}
        if not props.get("upload_id"):
            raise Refused("bad_request", "An upload needs its upload_id tag.")
        data = self._bytes(args.get("data"))
        found = self._find(parent["id"], {"upload_id": props["upload_id"]}, "file")
        if found:
            return self._view(found)
        return self._view(self.add(args["name"], parent["id"], args["mimeType"], data, props))

    def op_download(self, args):
        file = self._in_drives(args.get("id"))
        if file.get("top") or file["mimeType"] == FOLDER:
            raise Refused("bad_request", "Only uploaded files can be downloaded.")
        if len(file["data"] or b"") > gdrive.MAX_BYTES:
            raise Refused("too_big", "That file is bigger than 25 MB.")
        return {"data": base64.b64encode(file["data"] or b"").decode(), "name": file["name"],
                "mimeType": file["mimeType"], "size": len(file["data"] or b"")}

    def op_trash(self, args):
        file = self._in_drives(args.get("id"))
        if file.get("top"):
            raise Refused("bad_request", "A shared drive can't be trashed.")
        file["trashed"] = True
        return {"id": file["id"], "trashed": True}

    def op_ocr(self, args):
        if args.get("mimeType") not in gdrive.OCR_TYPES:
            raise Refused("bad_request", "Google reads text from JPEG, PNG, GIF and PDF files only.")
        if not self.allowed:
            raise Refused("not_allowed", "Pick the HR shared drive in Staff Access first.")
        data = self._bytes(args.get("data"))
        self.ocr_calls += 1
        self._scratch()
        return {"text": self.read_text(data)}

    def _scratch(self) -> None:
        if not self._find(self.allowed[0], {"staff_access": "ocr_scratch"}, "folder"):
            self.add("_OCR scratch", self.allowed[0], props={"staff_access": "ocr_scratch"})

    def op_pdf(self, args):
        html = args.get("html")
        if not isinstance(html, str) or not html.strip() or len(html) > gdrive.MAX_HTML:
            raise Refused("bad_request", "A PDF is made from 1 character to 2 MB of HTML.")
        if not self.allowed:
            raise Refused("not_allowed", "Pick the HR shared drive in Staff Access first.")
        self._scratch()
        self.pdfs.append(html)
        data = text_pdf(html_text(html))
        return {"data": base64.b64encode(data).decode(), "size": len(data)}

    def read_text(self, data: bytes) -> str:
        known = self.ocr_texts.get(hashlib.sha256(data).hexdigest())
        if known is not None:
            return known
        marker = data.find(b"%OCR-TEXT:")
        if marker >= 0:
            return data[marker + 10:].split(b"\n%%", 1)[0].decode("utf-8", "replace").strip()
        thumb = _thumb_hash(data)
        if thumb is not None and self.ocr_images:
            distance, text = min((bin(thumb ^ h).count("1"), t) for h, t in self.ocr_images)
            if distance <= 24:
                return text
        if self.ocr_queue:
            return self.ocr_queue.pop(0)
        return self.ocr_default

    def op_selfTest(self, args):
        checks = [{"check": "Sign-in", "ok": True,
                   "detail": f"Staff Access is signed in to Google as {self.email}."}]
        if not self.allowed:
            why = "No shared drive picked yet: pick one in Staff Access and click Save."
            return {"checks": checks + [{"check": c, "ok": False, "detail": why} for c in (
                "Shared drive", "Make and remove a test folder", "Read text from a test image")]}
        names = " and ".join(self.drives[d] for d in self.allowed)
        checks.append({"check": "Shared drive", "ok": True, "detail": f"Can use {names}."})
        checks.append({"check": "Make and remove a test folder", "ok": True,
                       "detail": "Made and removed a test folder."})
        checks.append({"check": "Read text from a test image", "ok": True,
                       "detail": f'Google read the test image: "{SELF_TEST_TEXT}".'})
        return {"checks": checks}


def html_text(html: str) -> list[str]:
    """The text of the HTML a PDF is made from, a line per heading, paragraph, row or line break."""
    text = re.sub(r"(?is)<(head|style|script)\b.*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>|</(p|h[1-6]|tr|li|div|table)>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>", "  ", text)
    text = html_lib.unescape(re.sub(r"<[^>]*>", "", text))
    return [" ".join(line.split()) for line in text.split("\n") if line.strip()]


def text_pdf(lines: list[str], per_page: int = 48, width: int = 90) -> bytes:
    """A small valid PDF (Helvetica, letter size) with these lines of text, wrapped."""
    wrapped = []
    for line in lines:
        while len(line) > width:
            cut = line.rfind(" ", 0, width)
            cut = cut if cut > 0 else width
            wrapped.append(line[:cut])
            line = line[cut:].lstrip()
        wrapped.append(line)
    pages = [wrapped[i:i + per_page] for i in range(0, len(wrapped), per_page)] or [[]]

    def literal(line: str) -> str:
        safe = line.encode("latin-1", "replace").decode("latin-1")
        return "(" + safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "",
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    kids = []
    for page in pages:
        stream = "BT /F1 11 Tf 14 TL 56 740 Td " + " ".join(f"{literal(l)} Tj T*" for l in page) + " ET"
        objects.append(f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
                       f"<< /Font << /F1 3 0 R >> >> /Contents {len(objects)} 0 R >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _thumb_hash(data: bytes) -> int | None:
    """A 16 x 16 grayscale thumbnail as 256 bits (lighter or darker than its average), so a
    re-encoded copy of an image still matches; None for something that isn't an image."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            pixels = list(im.convert("L").resize((16, 16)).getdata())
    except Exception:  # noqa: BLE001 - not an image Pillow reads
        return None
    mean = sum(pixels) / len(pixels)
    return sum(1 << i for i, p in enumerate(pixels) if p > mean)


def main() -> None:
    parser = argparse.ArgumentParser(description="A fake of the HR portal's Google Drive script.")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--secret", required=True, help="the secret in the copied script")
    parser.add_argument("--email", default="owner@hotlake.example")
    parser.add_argument("--ocr-dir", help="images and PDFs with a .txt beside each: what's read")
    parser.add_argument("--ocr-text", default="", help="what's read when nothing else matches")
    parser.add_argument("--script-version", type=int, default=gdrive.SCRIPT_VERSION)
    args = parser.parse_args()
    fake = FakeBridge(args.secret, args.email, ocr_dir=args.ocr_dir, ocr_text=args.ocr_text)
    fake.version = args.script_version
    print(f"Fake Google Drive script: http://{args.host}:{args.port}/macros/s/FAKE-bridge/exec "
          f"(set GOOGLE_SCRIPT_API=http://{args.host}:{args.port} for the add-on)", flush=True)
    web.run_app(fake.app(), host=args.host, port=args.port, print=None, access_log=None)


if __name__ == "__main__":
    main()
