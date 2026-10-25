"""The database itself: a write-ahead log that's put back into the file on the way out and
before Home Assistant backs the add-on up, the indexes the busy queries use, card numbers found
by how they start, and settings kept between reads but never stale."""

import os
import subprocess
import sys
import time

from test_wristbands_server import Portal, run

import db as db_module
from db import Database

CHECKPOINT = os.path.join(os.path.dirname(db_module.__file__), "checkpoint.py")
POOL = {"level": "door", "building": 2, "floor": 1, "mac": "C540E09C8C90", "door": 900,
        "label": "Pool Gate"}


def wal_size(path) -> int:
    wal = os.path.join(path, db_module.DB_FILE + "-wal")
    return os.path.getsize(wal) if os.path.exists(wal) else 0


def test_write_ahead_log_and_closing(tmp_path):
    d = Database(str(tmp_path))
    assert d.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert d.conn.execute("PRAGMA synchronous").fetchone()[0] == 1   # NORMAL
    d.update_settings({"property_name": "Hot Lake Lodge"})
    assert wal_size(tmp_path) > 0   # the change is in the log, not yet in the file
    d.close()
    assert wal_size(tmp_path) == 0
    again = Database(str(tmp_path))
    assert again.settings()["property_name"] == "Hot Lake Lodge"
    again.close()


def test_checkpoint_before_a_backup_never_fails_it(tmp_path):
    d = Database(str(tmp_path))
    d.update_settings({"property_name": "Hot Lake Lodge"})
    assert wal_size(tmp_path) > 0

    def checkpoint(data_dir):
        return subprocess.run([sys.executable, "-B", CHECKPOINT], capture_output=True, text=True,
                              env={**os.environ, "DATA_DIR": str(data_dir)}, timeout=60)

    # While the add-on runs (its connection open): the log goes back into the file.
    done = checkpoint(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert wal_size(tmp_path) == 0
    d.update_settings({"property_name": "Still here"})   # the add-on carries on as before
    assert d.settings()["property_name"] == "Still here"
    d.close()
    # No database yet, or a damaged one: still a success, so the backup goes ahead.
    assert checkpoint(tmp_path / "none").returncode == 0
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / db_module.DB_FILE).write_bytes(b"not a database at all" * 100)
    done = checkpoint(bad)
    assert done.returncode == 0 and "couldn't checkpoint" in done.stdout


def plan(d, sql, params=()) -> str:
    return " / ".join(r[3] for r in d.conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_busy_queries_use_their_indexes(tmp_path):
    d = Database(str(tmp_path))
    names = {r[0] for r in d.conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"wb_jobs_unfinished", "wb_jobs_staff", "wb_jobs_key_cards"} <= names
    # The statuses are spelled out, as the partial index needs.
    assert "wb_jobs_unfinished" in plan(
        d, f"SELECT id FROM wb_jobs WHERE encoder_url = ? AND {db_module.UNFINISHED} "
           "ORDER BY id LIMIT 1", ("ws://127.0.0.1:8102",))
    assert "wb_jobs_staff" in plan(
        d, "SELECT * FROM wb_jobs WHERE ts < ? AND staff_id = ? ORDER BY ts DESC, id DESC LIMIT 50",
        (time.time(), 1))
    assert db_module.UNFINISHED == "status IN ('queued', 'running', 'needs_attention')"
    d.close()


def test_an_unfinished_job_is_still_found_and_cancelled(tmp_path):
    d = Database(str(tmp_path))
    job = dict(card_type="day_pass", name="Rivera party", count=1, records=[POOL],
               summary="Pool Gate", expires_at=time.time() + 3600, allow_lockout=False,
               staff_id=None, device_id=None, actor="Ana", role=None, ip=None,
               encoder_url="ws://10.0.0.5:8102")
    first = d.add_wb_job(**job)
    assert d.active_wb_job("ws://10.0.0.5:8102")["id"] == first
    assert d.active_wb_job("ws://10.0.0.6:8102") is None
    d.update_wb_job(first, status="needs_attention")
    assert d.active_wb_job("ws://10.0.0.5:8102")["id"] == first
    d.update_wb_job(first, status="done")
    assert d.active_wb_job("ws://10.0.0.5:8102") is None
    second = d.add_wb_job(**job)
    assert d.cancel_unfinished_wb_jobs("Stopped") == [second]
    assert d.wb_job(second)["status"] == "cancelled" and d.wb_job(first)["status"] == "done"
    d.close()


def test_card_numbers_are_found_by_how_they_start(tmp_path):
    d = Database(str(tmp_path))
    made = {}
    for name, number in (("Rivera party", "1234567890"), ("Room 1234", "5550001111"),
                         ("Chen party", "9912345678")):
        job_id = d.add_wb_job(card_type="day_pass", name=name, count=1, records=[POOL],
                              summary="Pool Gate", expires_at=time.time() + 3600,
                              allow_lockout=False, staff_id=None, device_id=None, actor="Ana",
                              role=None, ip=None, encoder_url="ws://10.0.0.5:8102")
        d.update_wb_card(job_id, 1, status="written", card_number=number, written_at=time.time())
        made[name] = job_id

    def found(q):
        return {job["name"] for job in d.wb_jobs(q=q)}

    # A few digits: card numbers starting with them, and names holding them.
    assert found("1234") == {"Rivera party", "Room 1234"}
    assert found("99123") == {"Chen party"}
    assert found("2345") == set()   # inside a card number only: not found
    # As long as a card number: card numbers only.
    assert found("123456") == {"Rivera party"}
    assert found("9912345678") == {"Chen party"}
    assert found("party") == {"Rivera party", "Chen party"}
    d.close()


def test_settings_are_kept_but_never_stale(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            p.db.update_settings({"property_name": "Hot Lake Lodge"})
            first = p.db.settings()
            first["property_name"] = "changed by a caller"   # a copy: the kept one is untouched
            assert p.db.settings()["property_name"] == "Hot Lake Lodge"
            status, backup = await p.admin_call("GET", "/api/backup")
            assert status == 200
            p.db.update_settings({"property_name": "Renamed since"})
            assert p.db.settings()["property_name"] == "Renamed since"
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, reply
            assert p.db.settings()["property_name"] == "Hot Lake Lodge"
            # A restore that fails changes nothing, and nothing kept says otherwise.
            backup["settings"]["property_name"] = "Never applied"
            backup["roles"] = "damaged"
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 400, reply
            assert p.db.settings()["property_name"] == "Hot Lake Lodge"
    run(scenario())
