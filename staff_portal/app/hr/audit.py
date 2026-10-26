"""The HR audit log: who did what, to whose file, when and from where. Every sign-in, failure,
lock, reset, look at a record and change goes here, never into Staff Access's Activity log or
Home Assistant's logbook (other Home Assistant users can see those). It never holds PINs, codes
or what records say.

Rows can't be changed or deleted (hrdb.append_only), and each row's hash covers the row before
it, keyed with the HR key: changing, adding or removing a line in the middle shows up when HR
presses Check audit log (check()). Security events (sign-ins, locks, resets, settings) are
marked, and only those are shown to the owner on the Home Assistant dashboard.
"""

import json
import time

from . import crypto

START = "start"
MAX_TEXT = 300


def _cap(value, n: int):
    return None if value is None else str(value)[:n]


def _digest(prev: str, row: tuple) -> str:
    return crypto.mac(prev + "\n" + json.dumps(row, separators=(",", ":"), ensure_ascii=True),
                      "audit")


def record(db, action: str, *, actor_id=None, actor: str = "HR portal", target_id=None,
           target: str | None = None, detail: str | None = None, ip: str | None = None,
           security: bool = False) -> None:
    """Add a line. actor_id and target_id are Connecteam user IDs (or "owner" for the owner on
    the dashboard); actor and target are the names shown."""
    prev = db.conn.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    row = (time.time(), _cap(actor_id, 64), _cap(actor, 120) or "HR portal", _cap(action, 120),
           _cap(target_id, 64), _cap(target, 120), _cap(detail, MAX_TEXT), _cap(ip, 64),
           1 if security else 0)
    db.conn.execute(
        "INSERT INTO audit (ts, actor_id, actor, action, target_id, target, detail, ip, security, "
        "hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (*row, _digest(prev[0] if prev else START, row)))
    db.conn.commit()


COLUMNS = "id, ts, actor_id, actor, action, target_id, target, detail, ip, security"


def lines(db, limit: int = 100, before: int | None = None, security: bool = False,
          target_id: str | None = None) -> list[dict]:
    where, params = [], []
    if before:
        where.append("id < ?")
        params.append(int(before))
    if security:
        where.append("security = 1")
    if target_id is not None:
        where.append("target_id = ?")
        params.append(str(target_id))
    sql = f"SELECT {COLUMNS} FROM audit {'WHERE ' + ' AND '.join(where) if where else ''} " \
          "ORDER BY id DESC LIMIT ?"
    return [dict(r) for r in db.conn.execute(sql, (*params, max(1, min(500, limit))))]


def check(conn) -> dict:
    """Recompute the chain: {"ok", "lines", "first_bad" (the first line that doesn't match, or
    None), "checked_at"}. Reads the whole log: pass a connection of its own (HRDatabase.reader)
    and run it in a worker thread."""
    prev, count = START, 0
    for r in conn.execute(f"SELECT {COLUMNS}, hash FROM audit ORDER BY id"):
        row = (r["ts"], r["actor_id"], r["actor"], r["action"], r["target_id"], r["target"],
               r["detail"], r["ip"], r["security"])
        if _digest(prev, row) != r["hash"]:
            return {"ok": False, "lines": count, "first_bad": r["id"], "checked_at": time.time()}
        prev, count = r["hash"], count + 1
    return {"ok": True, "lines": count, "first_bad": None, "checked_at": time.time()}
