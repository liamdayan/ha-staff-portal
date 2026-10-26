"""Encryption for the HR portal: one random master key, and AES-256-GCM for private fields and
files, each purpose (a field, a kind of file) with its own key made from the master key (HKDF).

What a sealed value looks like: a version byte, a 12-byte nonce, then the ciphertext and its tag.
The purpose is also authenticated with it, so a value can't be moved to another field.

Where the key lives, in DATA_DIR/hr:
  master.key                      the default: in Home Assistant backups, so a restore just works
  outside-backups/master.key      while "Keep the HR key out of backups" is on (config.yaml's
                                  backup_exclude leaves this folder out of backups)
  master.key.recovery             also while it's on: the key locked with the HR recovery key
                                  (20 characters, shown once), which is in backups. After a
                                  restore, HR stays locked until the recovery key is typed in.

Small fields are sealed on the event loop (microseconds); files go through seal_async, which
runs in a worker thread, a few at a time.
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

KEY_FILE = "master.key"
OUTSIDE = "outside-backups"          # matched by backup_exclude in config.yaml
RECOVERY_FILE = "master.key.recovery"
VERSION = 1
NONCE_BYTES = 12
# The recovery key: 20 characters from 32 (100 bits), shown in groups of 4 like ABCD-EFGH-...
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 20
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
FILE_WORKERS = 2                      # files sealed or opened at once (each in a thread)

LOCKED = ("The HR portal is locked: its key isn't on this computer. Type the HR recovery key in "
          "Staff Access (HR portal tab) to unlock it.")
LOST = ("The HR key is missing and there's no recovery key to bring it back. HR records on this "
        "computer can't be opened.")
BAD_RECOVERY = ("Type the HR recovery key shown when “Keep the HR key out of backups” was "
                "turned on: 20 letters and numbers in groups of 4 (it never has O, 0, I or 1).")
WRONG_RECOVERY = "That isn't this HR portal's recovery key. Check it and try again."


class Locked(Exception):
    """The key isn't available (HR is off, or locked until the recovery key is typed)."""


class Damaged(ValueError):
    """A sealed value didn't open: changed, damaged, or sealed for another purpose."""


def write_private(path: str, data: bytes) -> None:
    """Write a key file so a crash leaves either the old file or the new one, readable only by
    the add-on."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def new_recovery_key() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i:i + 4] for i in range(0, CODE_LENGTH, 4))


def clean_recovery_key(text) -> str:
    code = re.sub(r"[\s-]+", "", str(text or "")).upper()
    if len(code) != CODE_LENGTH or any(ch not in CODE_ALPHABET for ch in code):
        raise ValueError(BAD_RECOVERY)
    return code


def _recovery_cipher(code: str, salt: bytes) -> AESGCM:
    return AESGCM(Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(
        clean_recovery_key(code).encode()))


class Keyring:
    """The master key for one HR folder, and the keys made from it."""

    def __init__(self, folder: str):
        self.folder = folder
        self._key: bytes | None = None
        self._subkeys: dict[str, bytes] = {}

    def _path(self, *parts) -> str:
        return os.path.join(self.folder, *parts)

    @property
    def ready(self) -> bool:
        return self._key is not None

    def kept_out(self) -> bool:
        """Whether "Keep the HR key out of backups" is on."""
        return os.path.exists(self._path(RECOVERY_FILE))

    def status(self) -> str:
        """"ready", "new" (never made: HR was never turned on), "locked" (kept out of backups and
        missing, as after a restore: the recovery key brings it back) or "lost"."""
        if self._key is not None or self._read() is not None:
            return "ready"
        if self.kept_out():
            return "locked"
        return "lost" if os.path.exists(self._path("hr.db")) else "new"

    def _read(self) -> bytes | None:
        for path in (self._path(KEY_FILE), self._path(OUTSIDE, KEY_FILE)):
            try:
                with open(path, "rb") as f:
                    key = base64.b64decode(f.read().strip(), validate=True)
            except (OSError, binascii.Error, ValueError):
                continue
            if len(key) == 32:
                return key
        return None

    def load(self, create: bool = False) -> str:
        """Load the key (making it the first time, if create). Returns status()."""
        key = self._read()
        if key is None and create and self.status() == "new":
            key = secrets.token_bytes(32)
            write_private(self._path(KEY_FILE), base64.b64encode(key))
        self._use(key)
        return self.status()

    def _use(self, key: bytes | None) -> None:
        self._key = key
        self._subkeys.clear()

    def forget(self) -> None:
        """HR turned off: the key is dropped from memory (it stays on disk)."""
        self._use(None)

    def key(self) -> bytes:
        if self._key is None:
            raise Locked(LOCKED)
        return self._key

    def subkey(self, purpose: str) -> bytes:
        found = self._subkeys.get(purpose)
        if found is None:
            found = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                         info=b"staff-access-hr/" + purpose.encode()).derive(self.key())
            self._subkeys[purpose] = found
        return found

    def keep_out(self, on: bool) -> str | None:
        """Turn "Keep the HR key out of backups" on (returns the recovery key, to show once) or
        off (the key goes back where backups include it). Slow (scrypt): run it in a thread."""
        key = self.key()
        if on:
            code = new_recovery_key()
            salt, nonce = os.urandom(16), os.urandom(NONCE_BYTES)
            sealed = _recovery_cipher(code, salt).encrypt(nonce, key, b"hr-recovery")
            # The locked copy first, then the key outside backups, then the old one goes.
            write_private(self._path(RECOVERY_FILE), json.dumps({
                "version": VERSION, "kdf": {"n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P},
                "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(nonce).decode(),
                "key": base64.b64encode(sealed).decode()}).encode())
            write_private(self._path(OUTSIDE, KEY_FILE), base64.b64encode(key))
            _remove(self._path(KEY_FILE))
            return code
        write_private(self._path(KEY_FILE), base64.b64encode(key))
        _remove(self._path(OUTSIDE, KEY_FILE))
        _remove(self._path(RECOVERY_FILE))
        return None

    def unlock(self, code: str) -> None:
        """Bring the key back with the recovery key (after a restore or a move). Raises
        ValueError with a message for the owner. Slow (scrypt): run it in a thread."""
        try:
            with open(self._path(RECOVERY_FILE), "rb") as f:
                locked = json.loads(f.read())
            salt = base64.b64decode(locked["salt"], validate=True)
            nonce = base64.b64decode(locked["nonce"], validate=True)
            sealed = base64.b64decode(locked["key"], validate=True)
        except (OSError, ValueError, KeyError, TypeError, binascii.Error):
            raise ValueError("There's no locked HR key here to unlock.") from None
        try:
            key = _recovery_cipher(code, salt).decrypt(nonce, sealed, b"hr-recovery")
        except InvalidTag:
            raise ValueError(WRONG_RECOVERY) from None
        write_private(self._path(OUTSIDE, KEY_FILE), base64.b64encode(key))
        self._use(key)

    def export(self) -> dict:
        """What a migration file carries: the key itself, or (kept out of backups) only its
        locked copy, so the new computer needs the recovery key."""
        if self.kept_out():
            with open(self._path(RECOVERY_FILE), "rb") as f:
                return {"recovery": f.read().decode()}
        return {"key": base64.b64encode(self.key()).decode()}

    def take(self, carried: dict) -> None:
        """Write what export() made, on the new computer (before HR starts)."""
        if isinstance(carried.get("key"), str):
            key = base64.b64decode(carried["key"], validate=True)
            if len(key) != 32:
                raise ValueError("bad key")
            write_private(self._path(KEY_FILE), base64.b64encode(key))
        elif isinstance(carried.get("recovery"), str):
            json.loads(carried["recovery"])
            write_private(self._path(RECOVERY_FILE), carried["recovery"].encode())
        else:
            raise ValueError("no key")


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- the key in use

ring: Keyring | None = None      # service.py sets it when HR starts
_slots: dict = {}                # the running event loop's limit on files at once


def file_slots() -> asyncio.Semaphore:
    """FILE_WORKERS at a time, for the running event loop (tests start a new loop each time)."""
    loop = asyncio.get_running_loop()
    if loop not in _slots:
        _slots.clear()
        _slots[loop] = asyncio.Semaphore(FILE_WORKERS)
    return _slots[loop]


def use(keyring: Keyring | None) -> None:
    global ring
    ring = keyring


def _ring() -> Keyring:
    if ring is None or not ring.ready:
        raise Locked(LOCKED)
    return ring


def seal(data: bytes, purpose: str) -> bytes:
    """Encrypt bytes for one purpose (such as "people.mobile" or "file.certificate")."""
    nonce = os.urandom(NONCE_BYTES)
    return bytes([VERSION]) + nonce + AESGCM(_ring().subkey(purpose)).encrypt(
        nonce, bytes(data), b"hr:" + purpose.encode())


def unseal(blob: bytes, purpose: str) -> bytes:
    """Decrypt what seal() made for the same purpose. Raises Damaged if it doesn't open."""
    if len(blob) < 1 + NONCE_BYTES + 16 or blob[0] != VERSION:
        raise Damaged("damaged")
    try:
        return AESGCM(_ring().subkey(purpose)).decrypt(
            blob[1:1 + NONCE_BYTES], blob[1 + NONCE_BYTES:], b"hr:" + purpose.encode())
    except InvalidTag:
        raise Damaged("damaged") from None


async def seal_async(data: bytes, purpose: str) -> bytes:
    """seal() for files: in a worker thread, FILE_WORKERS at a time."""
    async with file_slots():
        return await asyncio.to_thread(seal, data, purpose)


async def unseal_async(blob: bytes, purpose: str) -> bytes:
    async with file_slots():
        return await asyncio.to_thread(unseal, blob, purpose)


def seal_text(text: str | None, purpose: str) -> str | None:
    """A private text field as stored: sealed, as base64 text. None stays None."""
    if text is None:
        return None
    return base64.b64encode(seal(str(text).encode(), purpose)).decode()


def unseal_text(value: str | None, purpose: str) -> str | None:
    """A stored text field read back. None stays None; a damaged one raises Damaged."""
    if value is None:
        return None
    try:
        blob = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise Damaged("damaged") from None
    return unseal(blob, purpose).decode()


def mac(text: str, purpose: str) -> str:
    """A keyed hash (HMAC-SHA256) for finding things without storing them readably, and for
    chaining the audit log."""
    return hmac.new(_ring().subkey(purpose), text.encode(), hashlib.sha256).hexdigest()
