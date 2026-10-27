"""Moving everything to another install of the add-on (Backup tab > Migrate), such as when the
owner switches Home Assistant from one repository of the add-on to another, which Home Assistant
treats as a different add-on with its own empty /data.

The old install makes a migration file: a full backup (logs included) plus what a backup never
carries, so nothing has to be set up or typed again on the new install:
- device tokens (by hash, as stored) and the record of tokens issued, so every enrolled phone and
  computer stays signed in, and the setup codes of phones still waiting to be set up;
- the Connecteam and TT Hotel secrets, TT Hotel's sign-in and the request counts, whether
  remote lock-out was tested, and the Door activity address's secret;
- the whole door list, and Home Assistant user links;
- the HR portal (hr/): its settings (the HR Connecteam connection, and the Google Drive
  connection: the script's Web app URL and secret, and the drives, so the script already deployed
  in Google keeps working), its database (private fields already encrypted with its key), and its
  key. When "Keep the HR key out of backups" is on, only the key locked with the HR recovery key
  travels, so the new install asks for the recovery key before HR opens.

All of it is encrypted with AES-256-GCM under a key made (scrypt) from a one-time code: 20
characters from a 32-character alphabet (100 bits), made with `secrets`, shown to the admin once
and never stored, logged or put in any address. The file is JSON: a readable header (format,
version, key settings, salt, nonce, when it was made and expires, the add-on version) and "data",
the encrypted snapshot (gzip-compressed JSON, base64). Every header field is authenticated with
the data (as canonical JSON), so changing any of them makes the file unopenable.

A file works for 24 hours, and only on a fresh install (no staff, devices, roles, areas,
departments, card jobs or layout pages). The new install can't know whether the code was used
elsewhere before, so those two rules are what keep a file to one use: once restored, the new
install isn't fresh any more.
"""

import base64
import binascii
import json
import os
import re
import secrets
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

import doorlog
import transfer
from db import DOOR_COLUMNS, PRIVATE_SETTINGS
from hr import crypto as hr_crypto, gdrive, service as hr_service
from wristbands import MAC_RE, door_kind

FORMAT = transfer.MIGRATE_FORMAT
VERSION = 1                 # the file's own layout; the add-on's version is in the header too
VALID_SECONDS = 24 * 3600
# 32 symbols, so each is 5 bits: no 0 or O, no 1 or I, nothing that reads as another.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 20            # 100 bits: far beyond guessing, even with the file in hand
CODE_GROUP = 4              # shown as 5 groups of 4, like ABCD-EFGH-JKLM-NPQR-STUV
# scrypt: 64 MB and a fraction of a second per try (more on a Raspberry Pi). The code alone is
# out of reach of guessing; this makes each guess expensive too. A file asking for more memory
# than SCRYPT_MAX_N needs is refused rather than tried.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**16, 8, 1
SCRYPT_MAX_N = 2**17
CIPHER = "AES-256-GCM"
SALT_BYTES, NONCE_BYTES = 16, 12
# Settings a migration carries that a backup never does (PRIVATE_SETTINGS, and whether remote
# lock-out was seen working with these locks: the same locks and TT Hotel account).
SECRET_SETTINGS = (*PRIVATE_SETTINGS, "wb_remote_lockout_tested")
TYPED_SECRETS = ("connecteam_client_secret", "tthotel_client_secret", "tthotel_password_md5")
MAX_SAVED_OBJECT = 16 * 1024   # TT Hotel's sign-in or a request count, as JSON
HR_HOST_RE = re.compile(r"^[a-z0-9.-]{0,253}$")
TOKEN_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SETUP_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
REFRESH_TOKEN_RE = re.compile(r"^[\x21-\x7e]{1,2000}$")
MAX_ROWS = transfer.MAX_ROWS

NOT_MIGRATION = ("That isn't a Staff Portal migration file. Make one with Create migration file "
                 "on the old install.")
DAMAGED = "That migration file is damaged. Make a new one on the old install."
BAD_CODE = ("Type the code shown when the migration file was made: 20 letters and numbers in "
            "groups of 4 (it never has O, 0, I or 1).")
WRONG_CODE = ("That code doesn't open this file. Check it against the code shown when the file "
              "was made; if it's right, the file was damaged or changed, so make a new one.")
EXPIRED = ("That migration file has expired: they work for 24 hours. Start the old install "
           "again, make a new file, then stop it again.")
NEWER = ("That migration file was made by a newer version of Staff Portal{}. Update this add-on "
         "first, then try again.")


# ---------------------------------------------------------------- the code

def new_code() -> str:
    """A fresh one-time code, grouped for reading: ABCD-EFGH-JKLM-NPQR-STUV."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i:i + CODE_GROUP] for i in range(0, CODE_LENGTH, CODE_GROUP))


def clean_code(text) -> str:
    """A code as typed (any case, with or without the dashes and spaces), as the 20 characters
    the key is made from. Raises ValueError."""
    code = re.sub(r"[\s-]+", "", str(text or "")).upper()
    if len(code) != CODE_LENGTH or any(ch not in CODE_ALPHABET for ch in code):
        raise ValueError(BAD_CODE)
    return code


def _key(code: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(clean_code(code).encode())


# ---------------------------------------------------------------- making a file (old install)

def make_snapshot(db, branding, version: str) -> dict:
    """Everything a migration carries: a full backup with its logs, and under "migrate" what a
    backup never has. Reads the database it's given (main.read_everything's, off the event
    loop)."""
    rows = lambda sql, *params: [dict(r) for r in db.conn.execute(sql, params)]
    settings = db.settings()
    data = transfer.make_backup(db, branding, version)
    data["migrate"] = {
        "device_tokens": rows("SELECT token_hash, device_id, issued_at FROM device_tokens"),
        "secrets": {key: settings.get(key, "") for key in SECRET_SETTINGS},
        "doors": rows(f"SELECT {', '.join(DOOR_COLUMNS)} FROM wb_doors"),
        "links": {table: rows(f"""SELECT id, ha_user, ha_refresh_token, ha_link_error FROM {table}
                                  WHERE ha_refresh_token IS NOT NULL""")
                  for table in ("devices", "staff")},
        "setup_codes": rows("""SELECT id, enroll_code, enroll_expires FROM devices
                               WHERE status = 'pending' AND enroll_code IS NOT NULL
                               AND enroll_expires > ?""", time.time()),
    }
    return data


def _header_bytes(file: dict) -> bytes:
    """What's authenticated with the data: every other field of the file, as canonical JSON (so
    the file can be read and written again, by the dashboard say, without changing it)."""
    return json.dumps({k: v for k, v in file.items() if k != "data"}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True).encode()


def seal(data: dict, code: str, app_version: str, now: float | None = None) -> dict:
    """The migration file for a snapshot (make_snapshot), locked with the code: the header
    fields, then "data". Works only on data already read, so it runs off the event loop."""
    now = int(time.time() if now is None else now)
    salt, nonce = os.urandom(SALT_BYTES), os.urandom(NONCE_BYTES)
    file = {"format": FORMAT, "version": VERSION, "cipher": CIPHER,
            "kdf": {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P},
            "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(nonce).decode(),
            "created_at": now, "expires_at": now + VALID_SECONDS,
            "app_version": str(app_version)[:20]}
    sealed = AESGCM(_key(code, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)).encrypt(
        nonce, transfer.pack_backup(data), _header_bytes(file))
    file["data"] = base64.b64encode(sealed).decode()
    return file


# ---------------------------------------------------------------- opening a file (new install)

def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _whole(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _when(value, default: float | None = None) -> float | None:
    # Positive and before the year 5000, as for times in a backup.
    return float(value) if _number(value) and 0 < value < 1e11 else default


def read_file(raw) -> dict:
    """A migration file as uploaded (its bytes; gzip-compressed is fine too) or as the dashboard
    sends it (already read: the object, or its text). Raises ValueError."""
    try:
        if isinstance(raw, (bytes, bytearray)) and raw[:2] == transfer.GZIP_MAGIC:
            raw = transfer.gunzip(raw)
        file = json.loads(raw) if isinstance(raw, (bytes, bytearray, str)) else raw
    except (ValueError, RecursionError):
        file = None
    if not isinstance(file, dict) or file.get("format") != FORMAT:
        if isinstance(file, dict) and file.get("format") == transfer.FORMAT:
            raise ValueError("That's a backup or export file, not a migration file. Make a "
                             "migration file on the old install with Create migration file.")
        raise ValueError(NOT_MIGRATION)
    return file


def read_header(file: dict) -> tuple[dict, bytes, bytes]:
    """A migration file's header fields (checked), the bytes they're authenticated as, and the
    encrypted data. Raises ValueError. Nothing in the header can be trusted until the data opens
    with it, so it can only ever be a reason to refuse."""
    version, kdf = file.get("version"), file.get("kdf")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError(DAMAGED)
    if version > VERSION:
        raise ValueError(NEWER.format(""))
    try:
        salt = base64.b64decode(str(file.get("salt")), validate=True)
        nonce = base64.b64decode(str(file.get("nonce")), validate=True)
        data = file.get("data")
        sealed = base64.b64decode(data, validate=True) if isinstance(data, str) else b""
    except (binascii.Error, ValueError):
        raise ValueError(DAMAGED) from None
    n = kdf.get("n") if isinstance(kdf, dict) else None
    if (not isinstance(kdf, dict) or kdf.get("name") != "scrypt" or not isinstance(n, int)
            or isinstance(n, bool) or not 2**14 <= n <= SCRYPT_MAX_N or n & (n - 1)
            or kdf.get("r") != SCRYPT_R or kdf.get("p") != SCRYPT_P
            or file.get("cipher", CIPHER) != CIPHER or len(salt) != SALT_BYTES
            or len(nonce) != NONCE_BYTES or len(sealed) <= 16
            # Real times: NaN would never expire, and a huge one can't be shown as a date.
            or _when(file.get("created_at")) is None or _when(file.get("expires_at")) is None
            or not isinstance(file.get("app_version"), str)):
        raise ValueError(DAMAGED)
    header = {k: v for k, v in file.items() if k != "data"}
    return {**header, "salt": salt, "nonce": nonce}, _header_bytes(file), sealed


def _version(text) -> tuple[int, ...] | None:
    match = re.fullmatch(r"(\d{1,4})\.(\d{1,4})\.(\d{1,4})", str(text or "").strip())
    return tuple(int(x) for x in match.groups()) if match else None


def newer(theirs: str, ours: str) -> bool:
    """Whether add-on version `theirs` is newer than `ours` (an unknown one, such as "dev" in
    development, is never newer; the backup's schema is still checked)."""
    a, b = _version(theirs), _version(ours)
    return a is not None and b is not None and a > b


def open_file(raw, code: str, app_version: str, now: float | None = None) -> tuple[dict, dict]:
    """Open a migration file (as read_file takes it) with its code: (the snapshot, the header).
    Raises ValueError with a message for the admin: not a migration file, expired, made by a
    newer version, the wrong code (or a changed or damaged file: the two can't be told apart),
    or damaged inside. Works only on what was sent, so it runs off the event loop."""
    header, head, sealed = read_header(read_file(raw))
    now = time.time() if now is None else now
    if header["expires_at"] <= now:
        raise ValueError(EXPIRED)
    if newer(header["app_version"], app_version):
        raise ValueError(NEWER.format(f" ({header['app_version'][:20]})"))
    kdf = header["kdf"]
    try:
        plain = AESGCM(_key(code, header["salt"], kdf["n"], kdf["r"], kdf["p"])).decrypt(
            header["nonce"], sealed, head)
    except InvalidTag:
        raise ValueError(WRONG_CODE) from None
    data = transfer.read_backup_file(plain)
    if not isinstance(data, dict) or not isinstance(data.get("migrate"), dict):
        raise ValueError(DAMAGED)
    return data, {k: v for k, v in header.items() if k not in ("salt", "nonce")}


def _list(extra: dict, key: str, limit: int = MAX_ROWS) -> list[dict]:
    value = extra.get(key) or []
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(DAMAGED)
    return [row for row in value if isinstance(row, dict)]


def _saved_object(value) -> str:
    """TT Hotel's sign-in or a request count, as JSON text of an object ("{}" if damaged)."""
    try:
        small = isinstance(value, str) and len(value) <= MAX_SAVED_OBJECT
        found = json.loads(value) if small else None
    except (ValueError, RecursionError):
        found = None
    return json.dumps(found) if isinstance(found, dict) else "{}"


def _sealed(value) -> bool:
    """An HR setting sealed with the HR key (kept out of backups): it travels as it is, and opens
    once the HR recovery key is typed on the new install."""
    return isinstance(value, str) and hr_crypto.SEALED_RE.fullmatch(value) is not None


def _google_settings(raw: dict) -> dict:
    """The HR portal's Google Drive connection, checked as the dashboard checks it: the script
    deployed in Google keeps working with the same secret and Web app URL. Nothing from a file
    made before it existed, or one with anything wrong in it."""
    try:
        url = raw["hr_google_url"] if _sealed(raw.get("hr_google_url")) else gdrive.clean_url(
            raw.get("hr_google_url"))
        drive, restricted = (gdrive.clean_drive_id(raw.get(k)) for k in
                             ("hr_google_drive", "hr_google_restricted_drive"))
    except ValueError:
        return {}
    secret = raw.get("hr_google_secret")
    if not _sealed(secret) and not (isinstance(secret, str) and gdrive.SECRET_RE.fullmatch(secret)):
        return {}
    return {"hr_google_url": url, "hr_google_secret": secret, "hr_google_drive": drive,
            "hr_google_restricted_drive": restricted if restricted != drive else ""}


def clean_snapshot(data: dict, clean_settings, current_settings: dict, clean_logo,
                   now: float | None = None) -> dict:
    """Validate an opened migration file as strictly as a backup (transfer.clean_backup, trusting
    the device tokens the file lists, since only the old install could have made it), plus what
    only a migration carries, under "migrate" for db.replace_everything. Raises ValueError.
    Works only on data already read, so it runs off the event loop."""
    now = time.time() if now is None else now
    extra = data["migrate"]
    tokens, hashes = [], set()
    for r in _list(extra, "device_tokens", MAX_ROWS * 4):
        token = r.get("token_hash")
        # A token belongs to one device (the database insists): a second claim is dropped.
        if (isinstance(token, str) and TOKEN_HASH_RE.match(token) and token not in hashes
                and _whole(r.get("device_id"), 1, 10**9)):
            hashes.add(token)
            tokens.append((token, r["device_id"], _when(r.get("issued_at"), now)))
    snap = transfer.clean_backup(data, clean_settings, current_settings, clean_logo,
                                 {(device_id, token) for token, device_id, _ in tokens})
    devices = {d["id"]: d for d in snap["devices"]}
    staff = {m["id"] for m in snap["staff"]}

    # Secrets: typed ones are checked as the Settings form checks them (a blank one stays blank).
    raw = extra.get("secrets") if isinstance(extra.get("secrets"), dict) else {}
    typed = {k: raw[k].strip() for k in TYPED_SECRETS if isinstance(raw.get(k), str)}
    checked = clean_settings({k: v for k, v in typed.items() if v}, current_settings)
    secret_values = {k: checked.get(k, "") for k in typed}
    for key in ("connecteam_usage", "tthotel_token", "tthotel_usage", "hr_connecteam_usage",
                "hr_google_status"):
        secret_values[key] = _saved_object(raw.get(key))
    tested = raw.get("wb_remote_lockout_tested")
    secret_values["wb_remote_lockout_tested"] = (
        tested if isinstance(tested, str) and re.fullmatch(r"\d{1,12}", tested) else "")
    # The Door activity address, so TT Hotel's Callback URL keeps working (a file from before it
    # existed leaves this install's own).
    callback = raw.get("tthotel_callback_token")
    if isinstance(callback, str) and doorlog.TOKEN_RE.match(callback):
        secret_values["tthotel_callback_token"] = callback
    secret_values.update(_hr_settings(raw))
    secret_values.update(_google_settings(raw))

    # The door list, every column (a backup only has names and hiding, for doors already here).
    doors, lock_ids = None, set()
    if extra.get("doors") is not None:
        doors = []
        number = lambda v, lo, hi: v if _whole(v, lo, hi) else None
        text = lambda v: v[:100] if isinstance(v, str) else ""
        for row in _list(extra, "doors"):
            lock_id = row.get("lock_id")
            if (not _whole(lock_id, 1, 2**53) or lock_id in lock_ids
                    or not isinstance(row.get("mac"), str) or not MAC_RE.match(row["mac"])):
                continue
            lock_ids.add(lock_id)
            doors.append({
                "lock_id": lock_id, "tt_name": text(row.get("tt_name")),
                "alias": text(row.get("alias")), "mac": row["mac"],
                "lock_type": number(row.get("lock_type"), 0, 1000) or 1,
                "building_number": number(row.get("building_number"), -2**31, 2**31),
                "building_name": text(row.get("building_name")),
                "floor_number": number(row.get("floor_number"), -2**31, 2**31),
                "floor_name": text(row.get("floor_name")),
                "battery": number(row.get("battery"), 0, 100),
                "has_gateway": 1 if row.get("has_gateway") in (1, True) else 0,
                "name": str(row.get("name") or "").strip()[:60],   # as in a backup
                "hidden": 1 if row.get("hidden") in (1, True) else 0,
                "active": 0 if row.get("active") in (0, False) else 1,
                "synced_at": _when(row.get("synced_at"), now),
                "kind": door_kind(row.get("kind"))})

    # Home Assistant links: for people, and for devices that are still the same signed-in device.
    links = {"devices": {}, "staff": {}}
    raw_links = extra.get("links") if isinstance(extra.get("links"), dict) else {}
    for table, ids in (("devices", {i for i, d in devices.items() if d["token_hash"]}),
                       ("staff", staff)):
        for row in _list(raw_links, table):
            token = row.get("ha_refresh_token")
            if (_whole(row.get("id"), 1, 10**9) and row["id"] in ids and isinstance(token, str)
                    and REFRESH_TOKEN_RE.match(token)):
                user, error = row.get("ha_user"), row.get("ha_link_error")
                links[table][row["id"]] = (user[:200] if isinstance(user, str) else None, token,
                                           error[:300] if isinstance(error, str) else None)

    # Setup codes still waiting, so QR codes already handed out keep working.
    setup_codes = {}
    for row in _list(extra, "setup_codes"):
        device = devices.get(row["id"]) if _whole(row.get("id"), 1, 10**9) else None
        code = row.get("enroll_code")
        expires = _when(row.get("enroll_expires"))
        if (device and device["status"] == "pending" and isinstance(code, str)
                and SETUP_CODE_RE.match(code) and expires and expires > now
                and code not in {c for c, _ in setup_codes.values()}):
            setup_codes[device["id"]] = (code, expires)

    snap["migrate"] = {
        "device_tokens": [t for t in tokens if t[1] in devices],
        "secrets": secret_values, "doors": doors, "links": links, "setup_codes": setup_codes,
        "hr": _hr_part(extra.get("hr"))}
    return snap


def _hr_settings(raw: dict) -> dict:
    """The HR portal's settings in a migration file, checked (anything odd is left out). A file
    from 0.10.0 to 0.10.3 may also carry HR's own Connecteam keys, chat and copy switches: they're
    ignored (HR reads through the one Connecteam connection now)."""
    found = {}
    if raw.get("hr_enabled") in ("0", "1"):
        found["hr_enabled"] = raw["hr_enabled"]
    host = raw.get("hr_host")
    if isinstance(host, str) and HR_HOST_RE.match(host):
        found["hr_host"] = host
    return found


def _hr_part(hr) -> dict | None:
    """The HR database and key a migration file carries (the database checked as
    service.take_migration checks it, so a bad one is refused before anything is replaced), or
    None when the old install never turned HR on."""
    if hr is None:
        return None
    if (not isinstance(hr, dict)
            or not (isinstance(hr.get("key"), str) or isinstance(hr.get("recovery"), str))):
        raise ValueError(DAMAGED)
    hr_service.migrated_db(hr)   # checked before anything here is replaced (ValueError)
    return {k: hr[k] for k in ("db", "key", "recovery") if isinstance(hr.get(k), str)}
