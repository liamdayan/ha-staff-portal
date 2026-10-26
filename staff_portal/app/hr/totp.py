"""Authenticator codes (TOTP, RFC 6238): SHA-1, 6 digits, a new code every 30 seconds, and the
code one step either side accepted for a phone whose clock is a little off. Each code works once:
the caller keeps the step last used and passes it in. Works with Google Authenticator,
Microsoft Authenticator and similar apps, and with code-generator key fobs."""

import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
import urllib.parse

STEP = 30
DIGITS = 6
WINDOW = 1   # steps either side


def new_secret() -> str:
    """A fresh secret: 160 random bits, as the base32 text authenticator apps take."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    text = secret.upper()
    return base64.b32decode(text + "=" * (-len(text) % 8))


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def clean_code(text) -> str | None:
    """Six digits as typed (spaces allowed), or None."""
    code = re.sub(r"\s+", "", str(text or ""))
    return code if re.fullmatch(rf"\d{{{DIGITS}}}", code) else None


def check(secret: str, code, last_step: int = 0, now: float | None = None) -> int | None:
    """The time step this code belongs to, if it's right and newer than last_step (a code that
    was already used, or an older one, never works again); else None."""
    code = clean_code(code)
    if not code:
        return None
    current = int((time.time() if now is None else now) // STEP)
    for step in range(current - WINDOW, current + WINDOW + 1):
        if step > last_step and hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def uri(secret: str, account: str, issuer: str) -> str:
    """The otpauth:// address an authenticator app reads from the setup QR code."""
    label = urllib.parse.quote(f"{issuer}:{account}")
    query = urllib.parse.urlencode({"secret": secret, "issuer": issuer, "algorithm": "SHA1",
                                    "digits": DIGITS, "period": STEP})
    return f"otpauth://totp/{label}?{query}"


def grouped(secret: str) -> str:
    """The secret in groups of 4, for typing into an app by hand."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
