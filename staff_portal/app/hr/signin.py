"""Signing in to the HR portal: a mobile number (as in Connecteam, or a sign-in name HR set),
the person's Connecteam kiosk code as their PIN, and an authenticator code when they use one.

- Kiosk codes are never stored: only a slow salted hash (scrypt over an HMAC with a secret
  pepper made from the HR key), refreshed when the user list is synced. The scrypt settings are
  kept with each hash so they can be raised later. A sign-in never costs a Connecteam request.
- The authenticator code is checked before the PIN. A wrong code still waits for a dummy PIN
  check, so timing doesn't tell which numbers have an authenticator (that is, staff roles).
- Every failure gets the same message, whichever part was wrong.
- Per person: 5 wrong tries lock sign-in for 15 minutes; 15 in a day lock it until an HR admin
  unlocks it. A lock never signs out someone already signed in. Counted apart from the staff
  app's locks.
- Per address and overall: too many failures pause new sign-ins for a while.
- Sessions are stored hashed, end after the idle time HR sets (5 to 30 minutes) and after 8 hours
  whatever happens, and each person has at most 5. The cookie is __Host-hr: HttpOnly, Secure,
  SameSite=Strict.
- PIN checks run in worker threads, PIN_WORKERS at a time, so sign-ins never stall doors.
"""

import asyncio
import collections
import hashlib
import hmac
import re
import secrets
import time

from . import crypto, totp

FAILED = "That didn't work. Check your number, PIN and code."
SLOW_DOWN = "Too many tries. Wait a few minutes, then try again."
PAUSED = "Sign-in is paused for a few minutes after too many failed tries. Try again soon."
BUSY = "The HR portal is busy. Try again in a moment."
COOKIE = "__Host-hr"
COOKIE_ATTRS = "Path=/; Secure; HttpOnly; SameSite=Strict"
MAX_AGE = 8 * 3600              # a session ends after this, however busy
MAX_SESSIONS = 5                # signed-in devices per person
TOUCH_GAP = 30                  # a session's last use is written at most this often
LOCK_AFTER = 5                  # wrong tries in a row before a short lock
LOCK_SECONDS = 15 * 60
DAY_LIMIT = 15                  # wrong tries in a day before a lock only HR lifts
FAILURE_MEMORY = 15 * 60        # tries in a row are forgotten after this quiet time
ADDRESS_LIMIT = 10              # failures from one address in ADDRESS_WINDOW...
ADDRESS_WINDOW = 10 * 60
OVERALL_LIMIT = 100             # ...and from everywhere, which pauses sign-in for PAUSE_SECONDS
PAUSE_SECONDS = 5 * 60
PIN_WORKERS = 2
PIN_QUEUE = 20                  # PIN checks waiting at most; more are told it's busy
CONFIRM_SECONDS = 5 * 60        # a sensitive action needs the code (or PIN) typed this recently
RECOVERY_CODES = 8
SETUP_TTL = {"qr": 15 * 60, "link": 72 * 3600, "owner": 15 * 60}
SCRYPT = (14, 8, 1)             # log2(n), r, p: about 16 MB and a few tenths of a second at most
RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


# ---------------------------------------------------------------- PINs (kiosk codes)

def clean_pin(text) -> str | None:
    pin = str(text or "").strip()
    return pin if re.fullmatch(r"\d{4,8}", pin) else None


def _peppered(code: str) -> bytes:
    return crypto.mac(code, "pin-pepper").encode()


def hash_code(code: str) -> str:
    """The stored form of a kiosk code: scrypt over a keyed hash, with its settings."""
    log_n, r, p = SCRYPT
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(_peppered(code), salt=salt, n=2 ** log_n, r=r, p=p)
    return f"s1${log_n}${r}${p}${salt.hex()}${digest.hex()}"


def check_code(code: str, stored: str | None) -> bool:
    try:
        tag, log_n, r, p, salt, digest = (stored or "").split("$")
        if tag != "s1" or not 10 <= int(log_n) <= 20:
            return False
        found = hashlib.scrypt(_peppered(code), salt=bytes.fromhex(salt), n=2 ** int(log_n),
                               r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(found.hex(), digest)


_dummy: list[str] = []


def dummy_check(code: str) -> bool:
    """As slow as a real check, for a number nobody has, so timing doesn't tell."""
    if not _dummy:
        _dummy.append(hash_code("00000000"))
    check_code(code, _dummy[0])
    return False


_slots: dict = {}


def pin_slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _slots:
        _slots.clear()
        _slots[loop] = asyncio.Semaphore(PIN_WORKERS)
    return _slots[loop]


waiting = [0]


async def pin_matches(code: str, stored: str | None) -> bool:
    """check_code in a worker thread, PIN_WORKERS at a time. Raises Busy when too many wait."""
    if waiting[0] >= PIN_QUEUE:
        raise Busy(BUSY)
    waiting[0] += 1
    try:
        async with pin_slots():
            if stored:
                return await asyncio.to_thread(check_code, code, stored)
            return await asyncio.to_thread(dummy_check, code)
    finally:
        waiting[0] -= 1


class Busy(Exception):
    pass


# ---------------------------------------------------------------- finding someone

def normalize_mobile(text) -> str | None:
    """A phone number as digits, the US country code dropped: +1 (541) 555-0100 -> 5415550100."""
    digits = re.sub(r"\D", "", str(text or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if 7 <= len(digits) <= 15 else None


def mobile_key(mobile) -> str | None:
    number = normalize_mobile(mobile)
    return crypto.mac("mobile:" + number, "signin") if number else None


def clean_signin_name(text) -> str | None:
    name = " ".join(str(text or "").split()).casefold()
    return name if 3 <= len(name) <= 60 and not normalize_mobile(name) else None


def name_key(text) -> str | None:
    name = clean_signin_name(text)
    return crypto.mac("name:" + name, "signin") if name else None


def find_person(db, typed: str) -> dict | None:
    """Who a sign-in is for: by mobile number, else by the sign-in name HR set."""
    key = mobile_key(typed)
    column = "signin_hash"
    if key is None:
        key, column = name_key(typed), "signin_name_hash"
    if key is None:
        return None
    rows = db.conn.execute(f"SELECT * FROM people WHERE {column} = ? AND archived = 0",
                           (key,)).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None   # a shared number signs in nobody


# ---------------------------------------------------------------- throttles (in memory)

class Throttle:
    """Failures per address and overall, separate from the staff app's."""

    def __init__(self):
        self.by_address: dict[str, collections.deque] = {}
        self.overall: collections.deque = collections.deque()
        self.paused_until = 0.0

    def _trim(self, times: collections.deque, now: float) -> None:
        while times and now - times[0] > ADDRESS_WINDOW:
            times.popleft()

    def refusal(self, ip: str, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        if now < self.paused_until:
            return PAUSED
        times = self.by_address.get(ip)
        if times is not None:
            self._trim(times, now)
            if len(times) >= ADDRESS_LIMIT:
                return SLOW_DOWN
        return None

    def failed(self, ip: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if len(self.by_address) > 10_000:
            self.by_address.clear()   # made-up addresses mustn't grow this without limit
        times = self.by_address.setdefault(ip, collections.deque())
        self._trim(times, now)
        times.append(now)
        self._trim(self.overall, now)
        self.overall.append(now)
        if len(self.overall) >= OVERALL_LIMIT:
            self.paused_until = now + PAUSE_SECONDS
            self.overall.clear()

    def clear(self) -> None:
        self.by_address.clear()
        self.overall.clear()
        self.paused_until = 0.0


# ---------------------------------------------------------------- per-person locks

def locked(person: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return bool(person["hard_locked"]) or (person["locked_until"] or 0) > now


def wrong_try(db, person: dict, now: float | None = None) -> str | None:
    """Count a wrong try. Returns "locked" or "hard_locked" when this one locked them."""
    now = time.time() if now is None else now
    quiet = now - (person["failed_at"] or 0) > FAILURE_MEMORY
    tries = (0 if quiet else person["failed"]) + 1
    since = person["day_since"]
    if not since or now - since > 86400:
        since, today = now, 1
    else:
        today = person["day_failed"] + 1
    fields = {"failed": tries, "failed_at": now, "day_failed": today, "day_since": since}
    result = None
    if today >= DAY_LIMIT:
        fields.update(hard_locked=1, failed=0)
        result = "hard_locked"
    elif tries >= LOCK_AFTER:
        fields.update(locked_until=now + LOCK_SECONDS, failed=0)
        result = "locked"
    db.update_person(person["id"], **fields)
    return result


def unlock(db, person_id) -> None:
    db.update_person(person_id, failed=0, failed_at=None, locked_until=None, day_failed=0,
                     day_since=None, hard_locked=0)


# ---------------------------------------------------------------- authenticator

def totp_required(db, person: dict, roles: set[str] | None = None) -> bool:
    """Whether HR's setting "Who must use an authenticator code" covers this person."""
    roles = db.roles_of(person["id"]) if roles is None else roles
    policy = db.setting("totp_policy")
    if policy == "everyone":
        return True
    if policy == "hr":
        return "hr_admin" in roles
    if policy == "nobody":
        return False
    return bool(roles & {"hr_admin", "director", "manager"})


ISSUED = ("hr", "owner")   # set up with a setup code someone else made


def needs_setup_code(db, person: dict, roles: set[str] | None = None) -> bool:
    """Whether this person must use an authenticator but has none set up with a setup code from
    HR or the owner. A phone they added themselves in My account doesn't count, so a kiosk code
    alone never reaches a manager's account."""
    issued = person["totp_on"] and person["totp_source"] in ISSUED
    return not issued and totp_required(db, person, roles)


def totp_secret(person: dict) -> str | None:
    return crypto.unseal_text(person["totp_secret"], "people.totp") if person["totp_on"] else None


def check_totp(db, person: dict, code) -> bool:
    """The person's authenticator code, right and not used before (each works once)."""
    secret = totp_secret(person)
    step = totp.check(secret, code, person["totp_step"]) if secret else None
    if step is None:
        return False
    db.update_person(person["id"], totp_step=step)
    return True


def clean_recovery_code(text) -> str | None:
    code = re.sub(r"[\s-]+", "", str(text or "")).upper()
    return code if len(code) == 10 and all(c in RECOVERY_ALPHABET for c in code) else None


def new_recovery_codes(db, person_id) -> list[str]:
    """8 new one-time recovery codes (the old ones stop working). Shown once."""
    codes = ["".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(10)) for _ in range(RECOVERY_CODES)]
    db.conn.execute("DELETE FROM recovery_codes WHERE person_id = ?", (str(person_id),))
    db.conn.executemany("INSERT INTO recovery_codes (person_id, code_hash) VALUES (?, ?)",
                        [(str(person_id), crypto.mac(c, "recovery")) for c in codes])
    db.conn.commit()
    return [f"{c[:5]}-{c[5:]}" for c in codes]


def use_recovery_code(db, person_id, typed) -> bool:
    code = clean_recovery_code(typed)
    if not code:
        return False
    used = db.conn.execute(
        "UPDATE recovery_codes SET used_at = ? WHERE person_id = ? AND code_hash = ? "
        "AND used_at IS NULL", (time.time(), str(person_id), crypto.mac(code, "recovery"))).rowcount
    db.conn.commit()
    return bool(used)


def recovery_codes_left(db, person_id) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM recovery_codes WHERE person_id = ? AND "
                           "used_at IS NULL", (str(person_id),)).fetchone()[0]


def enable_totp(db, person_id, secret: str, step: int, source: str = "self") -> list[str]:
    """Turn the authenticator on (a new phone replaces the old one). source: who made the setup
    code ("hr", "owner", or "self" in My account). Returns new recovery codes."""
    db.update_person(person_id, totp_secret=crypto.seal_text(secret, "people.totp"), totp_on=1,
                     totp_step=step, totp_redo=0, totp_source=source)
    return new_recovery_codes(db, person_id)


def disable_totp(db, person_id) -> None:
    db.update_person(person_id, totp_secret=None, totp_on=0, totp_step=0, totp_redo=0,
                     totp_source=None)
    db.conn.execute("DELETE FROM recovery_codes WHERE person_id = ?", (str(person_id),))
    db.conn.commit()


# ---------------------------------------------------------------- sessions

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(db, person_id, ip: str, agent: str, via_recovery: bool = False) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    pid = str(person_id)
    # At most MAX_SESSIONS: the oldest go to make room.
    extra = db.conn.execute("SELECT id FROM sessions WHERE person_id = ? ORDER BY last_used DESC "
                            "LIMIT -1 OFFSET ?", (pid, MAX_SESSIONS - 1)).fetchall()
    db.conn.executemany("DELETE FROM sessions WHERE id = ?", [(r[0],) for r in extra])
    db.conn.execute(
        "INSERT INTO sessions (token_hash, person_id, created_at, last_used, confirmed_at, ip, "
        "agent, via_recovery) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (_hash(token), pid, now, now, now, ip[:64], agent[:200], 1 if via_recovery else 0))
    db.conn.commit()
    return token


def idle_seconds(db) -> int:
    return db.setting_int("session_minutes", 5, 30) * 60


def session_for(db, token: str | None, touch: bool = True, now: float | None = None) -> dict | None:
    """The live session for a cookie, or None (ended, idle too long, or past 8 hours)."""
    if not token or len(token) > 100:
        return None
    now = time.time() if now is None else now
    row = db.conn.execute("SELECT * FROM sessions WHERE token_hash = ?", (_hash(token),)).fetchone()
    if row is None:
        return None
    if now - row["last_used"] > idle_seconds(db) or now - row["created_at"] > MAX_AGE:
        db.conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
        db.conn.commit()
        return None
    if touch and now - row["last_used"] > TOUCH_GAP:
        db.conn.execute("UPDATE sessions SET last_used = ? WHERE id = ?", (now, row["id"]))
        db.conn.commit()
    return dict(row)


def end_session(db, session_id: int) -> None:
    db.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    db.conn.commit()


def end_sessions(db, person_id=None) -> int:
    """Sign someone out everywhere (everyone, when person_id is None)."""
    if person_id is None:
        count = db.conn.execute("DELETE FROM sessions").rowcount
    else:
        count = db.conn.execute("DELETE FROM sessions WHERE person_id = ?",
                                (str(person_id),)).rowcount
    db.conn.commit()
    return count


def confirmed(session: dict | None, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return bool(session) and now - (session["confirmed_at"] or 0) <= CONFIRM_SECONDS


def mark_confirmed(db, session_id: int) -> None:
    db.conn.execute("UPDATE sessions SET confirmed_at = ? WHERE id = ?", (time.time(), session_id))
    db.conn.commit()


def prune(db, now: float | None = None) -> None:
    """Forget sessions and setup codes that ended (the hourly tidy-up)."""
    now = time.time() if now is None else now
    db.conn.execute("DELETE FROM sessions WHERE last_used < ? OR created_at < ?",
                    (now - 30 * 60, now - MAX_AGE))
    db.conn.execute("DELETE FROM setup_tokens WHERE expires_at < ?", (now - 86400,))
    db.conn.commit()


def cookie(token: str) -> str:
    return f"{COOKIE}={token}; {COOKIE_ATTRS}; Max-Age={MAX_AGE}"


def cleared_cookie() -> str:
    return f"{COOKIE}=; {COOKIE_ATTRS}; Max-Age=0"


# ---------------------------------------------------------------- setup codes

def new_setup_token(db, person_id, kind: str, by: str | None) -> tuple[str, float]:
    """A one-time setup code for an authenticator: a QR code on HR's screen ("qr", 15 minutes),
    a link HR sends by hand ("link", 72 hours), or the owner's QR code for an HR admin on the
    dashboard ("owner"). Earlier unused ones for this person stop working. Returns (code,
    expires at)."""
    token = secrets.token_urlsafe(24)
    now = time.time()
    expires = now + SETUP_TTL[kind]
    db.conn.execute("DELETE FROM setup_tokens WHERE person_id = ? AND used_at IS NULL",
                    (str(person_id),))
    db.conn.execute("INSERT INTO setup_tokens (token_hash, person_id, kind, created_by, created_at, "
                    "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (_hash(token), str(person_id), kind, by, now, expires))
    db.conn.commit()
    return token, expires


def setup_token(db, token: str | None) -> dict | None:
    """A setup code that can still be used."""
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token):
        return None
    row = db.conn.execute("SELECT * FROM setup_tokens WHERE token_hash = ? AND used_at IS NULL "
                          "AND expires_at > ?", (_hash(token), time.time())).fetchone()
    return dict(row) if row else None


def update_setup_token(db, token: str, **fields) -> None:
    allowed = {"used_at", "pin_ok_at", "pending_secret"}
    if set(fields) - allowed:
        raise ValueError(fields)
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.conn.execute(f"UPDATE setup_tokens SET {sets} WHERE token_hash = ?",
                    (*fields.values(), _hash(token)))
    db.conn.commit()
