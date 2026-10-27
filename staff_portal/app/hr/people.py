"""People in the HR portal come from Connecteam: everyone in the user list Staff Access already
reads (connecteam.users(), cached, so the hourly sync costs no extra request when Staff Access
has just read it), keyed by their Connecteam user ID.

Kept for each person: name, mobile number and email (both encrypted), smart groups (their
departments), start date (from the profile field HR picks, else the day they were added to
Connecteam, marked "estimated" until HR confirms it), direct manager (from the "Direct manager"
profile field) and whether they're archived. Plus a slow hash of their kiosk code for sign-in,
refreshed here: when it changes, their HR sessions end. Kiosk codes, birthdays and other profile
fields are never stored.

Archived in Connecteam (or gone from the list): their HR access ends and their records stay.
Coming back doesn't bring access back by itself: HR allows it again. The safety catch: if
Connecteam suddenly lists far fewer people (none, or more than a quarter fewer), nobody is cut
off and HR gets a warning instead.
"""

import asyncio
import hashlib
import json
import re
import time
from datetime import date, datetime, timezone

from . import audit, crypto, signin

SAFETY_SHARE = 0.75      # a list with fewer than this share of the people active now trips the catch
SAFETY_MIN = 4           # ...once there are at least this many
START_FIELD_NAMES = ("employment start date", "start date", "hire date", "date of hire",
                     "date hired")   # used until HR picks a field
MANAGER_FIELD_NAMES = ("direct manager", "manager", "reports to")
CAUGHT = ("Connecteam listed {n} people, far fewer than the {before} active here, so nobody's HR "
          "access was ended. Check Connecteam; the next sync tries again.")
NO_KIOSK_CODE = "No kiosk code in Connecteam"

# Connecteam user ID -> fingerprint of (stored hash, kiosk code) last found to match, so the
# hourly sync doesn't check every code again (each check is deliberately slow). Memory only.
kiosk_seen: dict[str, str] = {}


def parse_date(value, order: str = "mdy") -> str | None:
    """A date from a Connecteam profile field as YYYY-MM-DD: 2024-10-25, 10/25/2024 (or 25/10/2024
    when order is "dmy"), or a timestamp. None when it isn't one."""
    found = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 1e11 else value
        if 0 < seconds < 1e11:
            found = datetime.fromtimestamp(seconds, timezone.utc).date()
    elif isinstance(value, str):
        text = value.strip()
        try:
            if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})([T ].*)?", text):
                found = date(int(m[1]), int(m[2]), int(m[3]))
            elif m := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", text):
                a, b = int(m[1]), int(m[2])
                month, day = (b, a) if order == "dmy" else (a, b)
                if month > 12 >= day:
                    month, day = day, month
                found = date(int(m[3]), month, day)
            elif re.fullmatch(r"\d{9,13}", text):
                return parse_date(int(text), order)
        except ValueError:
            return None
    return found.isoformat() if found and 1900 <= found.year <= 2100 else None


def ids_in(value) -> list[str]:
    """Connecteam user IDs in a profile field's value (a direct manager)."""
    values = value if isinstance(value, list) else [value]
    return [str(v) for v in values
            if isinstance(v, (int, str)) and not isinstance(v, bool)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", str(v))][:10]


def find_field(fields: list[dict], chosen: str, names, users=()) -> str | None:
    """The profile field to use: the one HR chose, else the first whose name is one of names (in
    order), from the list of fields or else as the users' own records name them."""
    if chosen:
        return chosen
    named = {}
    for f in fields:
        named.setdefault(str(f.get("name", "")).strip().casefold(), str(f.get("id")))
    for u in users:
        for fid, name in (u.get("field_names") or {}).items():
            named.setdefault(str(name).strip().casefold(), str(fid))
    return next((named[n] for n in names if n in named), None)


def date_order(values, chosen: str) -> str:
    """How the dates in values are written: what HR chose (mdy or dmy), else (auto) worked out
    from the dates themselves: a first number over 12 means day first, a second one over 12
    month first. Connecteam's users list writes them day first (30/03/2024), so that's the
    answer when nothing tells."""
    if chosen in ("mdy", "dmy"):
        return chosen
    for v in values:
        if isinstance(v, str) and (m := re.fullmatch(r"\s*(\d{1,2})[/.-](\d{1,2})[/.-]\d{4}\s*", v)):
            if int(m[1]) > 12 >= int(m[2]):
                return "dmy"
            if int(m[2]) > 12 >= int(m[1]):
                return "mdy"
    return "dmy"


def _fingerprint(stored: str, code: str) -> str:
    return hashlib.sha256(f"{stored}:{code}".encode()).hexdigest()


def _unseal(value, purpose):
    try:
        return crypto.unseal_text(value, purpose)
    except crypto.Damaged:
        return None


async def apply_users(db, users: list[dict], *, group_name=lambda g: g,
                      on_signed_out=lambda pid: None, now: float | None = None) -> dict:
    """Bring the people here in line with Connecteam's active users. Returns what changed:
    {"added", "updated", "archived", "returned", "no_code", "warning"}. group_name(id) names a
    department for the audit log; on_signed_out(person id) is told when someone's sessions end."""
    now = time.time() if now is None else now
    existing = {p["id"]: p for p in db.people(archived=None)}
    active_before = sum(1 for p in existing.values() if not p["archived"])
    caught = (not users and active_before > 0) or (
        active_before >= SAFETY_MIN and len(users) < active_before * SAFETY_SHARE)

    # Kiosk codes: check the ones that may have changed, and hash new ones, off the event loop.
    todo = []
    for u in users:
        old = existing.get(u["id"])
        stored = old and old["kiosk_hash"]
        code = u.get("kiosk_code")
        if code and not (stored and kiosk_seen.get(u["id"]) == _fingerprint(stored, code)):
            todo.append((u["id"], code, stored))

    def work():
        done = {}
        for uid, code, stored in todo:
            if stored and signin.check_code(code, stored):
                done[uid] = (stored, False)
            else:
                done[uid] = (signin.hash_code(code), bool(stored))
        return done
    hashed = await asyncio.to_thread(work) if todo else {}

    # From here on nothing is awaited: read fresh and written in one go.
    existing = {p["id"]: p for p in db.people(archived=None)}
    fields_list = db.setting_json("ct_fields", []) or []
    start_field = find_field(fields_list, db.setting("start_field"), START_FIELD_NAMES, users)
    manager_field = find_field(fields_list, db.setting("manager_field"), MANAGER_FIELD_NAMES,
                               users)
    order = date_order([(u.get("fields") or {}).get(start_field) for u in users],
                       db.setting("date_order"))
    counts = {"added": 0, "updated": 0, "archived": 0, "returned": 0, "no_code": 0}
    signed_out = set()
    for u in users:
        uid, old = u["id"], existing.get(u["id"])
        fields = u.get("fields") or {}
        start = ct_start = parse_date(fields.get(start_field), order) if start_field else None
        estimated = 0
        if not start and u.get("created_at"):
            start = datetime.fromtimestamp(u["created_at"], timezone.utc).date().isoformat()
            estimated = 1
        mobile = (u.get("mobile") or "").strip() or None
        email = (u.get("email") or "").strip() or None
        new = {"name": u["name"][:80], "groups": json.dumps(sorted(set(u.get("groups") or []))),
               "managers": json.dumps(ids_in(fields.get(manager_field)) if manager_field else []),
               "synced_at": now}
        if old is None or _unseal(old["mobile"], "people.mobile") != mobile:
            new["mobile"] = crypto.seal_text(mobile, "people.mobile")
            new["signin_hash"] = signin.mobile_key(mobile)
        if old is None or _unseal(old["email"], "people.email") != email:
            new["email"] = crypto.seal_text(email, "people.email")
        if old is not None and ct_start and ct_start != old["start_ct"]:
            # Changed in Connecteam (or there for the first time): it wins, even over a date HR
            # confirmed here. HR's own change stays until Connecteam's changes again.
            new.update(start_date=ct_start, start_estimated=0, start_confirmed=0)
            if old["start_date"] and old["start_date"] != ct_start and not old["start_estimated"]:
                audit.record(db, "Start date changed in Connecteam", target_id=uid,
                             target=u["name"], detail=f"{old['start_date']} → {ct_start}")
        elif old is None or not old["start_confirmed"]:
            new.update(start_date=start, start_estimated=estimated)
        if ct_start:
            new["start_ct"] = ct_start
        code = u.get("kiosk_code")
        if code and uid in hashed:
            stored, changed = hashed[uid]
            new["kiosk_hash"] = stored
            kiosk_seen[uid] = _fingerprint(stored, code)
            if changed:
                signed_out.add(uid)
                audit.record(db, "Kiosk code changed in Connecteam: signed out of HR",
                             target_id=uid, target=u["name"], security=True)
        elif not code:
            counts["no_code"] += 1
            if old and old["kiosk_hash"]:
                new["kiosk_hash"] = None
                signed_out.add(uid)
        if old is None:
            db.conn.execute("INSERT INTO people (id, name, added_at) VALUES (?, ?, ?)",
                            (uid, new["name"], now))
            counts["added"] += 1
        else:
            if old["archived"]:
                # Back in Connecteam: their file is active again, but HR allows sign-in again.
                new.update(archived=0, archived_at=None, access_ended=1)
                counts["returned"] += 1
                audit.record(db, "Back in Connecteam: HR access stays off until HR allows it",
                             target_id=uid, target=u["name"], security=True)
            if old["groups"] != new["groups"]:
                audit.record(db, "Department changed in Connecteam", target_id=uid,
                             target=u["name"], detail=_groups_text(old["groups"], new["groups"],
                                                                   group_name))
            if any(old[k] != v for k, v in new.items() if k != "synced_at"):
                counts["updated"] += 1
        sets = ", ".join(f"{k} = ?" for k in new)
        db.conn.execute(f"UPDATE people SET {sets} WHERE id = ?", (*new.values(), uid))

    listed = {u["id"] for u in users}
    for p in existing.values():
        if p["archived"] or p["id"] in listed or caught:
            continue
        db.conn.execute("UPDATE people SET archived = 1, archived_at = ?, access_ended = 1 "
                        "WHERE id = ?", (now, p["id"]))
        signed_out.add(p["id"])
        counts["archived"] += 1
        audit.record(db, "Archived in Connecteam: HR access ended", target_id=p["id"],
                     target=p["name"], security=True)
    db.conn.commit()
    for pid in signed_out:
        if signin.end_sessions(db, pid):
            on_signed_out(pid)
    warning = CAUGHT.format(n=len(users), before=active_before) if caught else None
    if warning and db.setting("sync_warning") != warning:
        audit.record(db, "Safety catch: Connecteam listed far fewer people", detail=warning,
                     security=True)
    db.set_settings({"last_sync": now, "sync_warning": warning or "",
                     "last_sync_result": json.dumps(counts)})
    return {**counts, "warning": warning}


def _groups_text(before: str, after: str, group_name) -> str:
    def names(raw):
        try:
            ids = json.loads(raw)
        except ValueError:
            ids = []
        return ", ".join(group_name(g) for g in ids) or "none"
    return f"{names(before)} → {names(after)}"


def mark_archived(db, archived: list[dict], now: float | None = None) -> int:
    """The daily check of Connecteam's archived users: anyone here among them is archived (and
    their access ends), unless the safety catch is holding."""
    if db.setting("sync_warning"):
        return 0
    now = time.time() if now is None else now
    count = 0
    for u in archived:
        p = db.person(u["id"])
        if p and not p["archived"]:
            db.conn.execute("UPDATE people SET archived = 1, archived_at = ?, access_ended = 1 "
                            "WHERE id = ?", (now, p["id"]))
            signin.end_sessions(db, p["id"])
            audit.record(db, "Archived in Connecteam: HR access ended", target_id=p["id"],
                         target=p["name"], security=True)
            count += 1
    db.conn.commit()
    return count


def mobile_of(person: dict) -> str | None:
    return _unseal(person.get("mobile"), "people.mobile")


def email_of(person: dict) -> str | None:
    return _unseal(person.get("email"), "people.email")


def groups(person: dict) -> list[str]:
    try:
        found = json.loads(person.get("groups") or "[]")
    except ValueError:
        return []
    return [str(g) for g in found] if isinstance(found, list) else []
