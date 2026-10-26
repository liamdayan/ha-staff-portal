"""Backups with their logs, and migrating everything to another install of the add-on.

Backups (schema 4) carry the activity log and the card records; a restore adds them to the ones
here, without duplicates and without removing anything, and keeps the usual limits. Migration
files carry everything, secrets and device tokens included, encrypted under a one-time code, and
only go onto a fresh install. Nothing here talks to a real TT Hotel, Connecteam or Home Assistant.
"""

import base64
import gzip
import json
import os
import re
import time

from aiohttp import FormData
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from test_wristbands_server import (ADMIN_HEADERS, DOORS, PASSWORD, SECRET, Portal, run,
                                    tthotel_account)

import db as db_module  # noqa: E402
import main  # noqa: E402
import migrate  # noqa: E402
import transfer  # noqa: E402
from db import Database  # noqa: E402
from tthotel import TTHotelError  # noqa: E402

POOL = {"level": "door", "building": 2, "floor": 1, "mac": "C540E09C8C90", "door": 900,
        "label": "Pool Gate"}
CODE_RE = re.compile(r"^([A-HJ-NP-Z2-9]{4}-){4}[A-HJ-NP-Z2-9]{4}$")


class Fresh(Portal):
    """A new install of the add-on: an empty database, as Home Assistant gives a newly installed
    add-on (no roles, devices or staff), and test clients for both apps."""

    def __init__(self, path):
        previous, main.db = main.db, Database(str(path))
        previous.conn.close()
        for table in (main.pin_sessions, main.failures):
            table.clear()
        main.transfer_running[0] = False
        main.tthotel.forget()
        self.db = main.db
        self.pins = {}
        self.token = None
        self.staff = self.admin = None


def written_job(p, name="Smith party", count=2, card_type="day_pass", **extra) -> int:
    """A finished card job with its cards written, as the encoder leaves them."""
    job_id = p.db.add_wb_job(
        card_type=card_type, name=name, count=count, records=[POOL], summary="Pool Gate",
        expires_at=time.time() + 3600, allow_lockout=False, device_id=p.device,
        actor="Front Desk PC (Ana)", role="Front Desk", ip="203.0.113.5",
        encoder_url="ws://127.0.0.1:1", **{"staff_id": p.ana, **extra})
    for seq in range(1, count + 1):
        p.db.update_wb_card(job_id, seq, status="written", card_number=f"{job_id}{seq:04d}",
                            written_at=time.time())
    p.db.update_wb_job(job_id, status="done", finished_at=time.time())
    return job_id


async def download(p, path):
    resp = await p.admin.get(path, headers=ADMIN_HEADERS)
    return resp.status, resp.headers, await resp.read()


async def upload(p, path, **fields):
    """A multipart upload of a picked file (bytes) and typed text, as a browser form sends them."""
    form = FormData()
    for name, value in fields.items():
        if isinstance(value, bytes):
            form.add_field(name, value, filename="picked-file", content_type="application/octet-stream")
        else:
            form.add_field(name, value)
    resp = await p.admin.post(path, data=form, headers=ADMIN_HEADERS)
    text = await resp.text()
    try:
        return resp.status, json.loads(text)
    except ValueError:
        return resp.status, text


def actions(p) -> list[str]:
    return [r[0] for r in p.db.conn.execute("SELECT action FROM activity ORDER BY id")]


def table_count(p, table: str) -> int:
    return p.db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------- logs in backups

def test_backups_carry_the_logs_and_restores_add_them_without_duplicates(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        p.db.log("Ana", "Turned on", entity_id="switch.sauna", detail="Sauna heater")
        made = written_job(p)
        lost_job = written_job(p, name="Eve", card_type="staff", count=1, holder_staff_id=p.dee)
        lost = p.db.wb_job(lost_job)["cards"][0]
        p.db.set_card_state(lost["id"], "lost", "Bob")
        p.db.add_lockouts(lost["id"], [101, 102], "notice")
        p.db.set_lockout(lost["id"], 101, "notice", "done")
        # Its replacement carries the lost card's cancellation, and points at it.
        replaced = written_job(p, name="Eve", card_type="staff", count=1, holder_staff_id=p.dee,
                               target_card_id=lost["id"], target_card=lost["card_number"])
        async with p:
            status, headers, raw = await download(p, "/api/backup")
            assert status == 200 and headers["Content-Type"] == "application/gzip"
            assert re.search(r'filename="staff-portal-backup-\d{4}-\d\d-\d\d\.json\.gz"',
                             headers["Content-Disposition"])
            backup = json.loads(gzip.decompress(raw))
            assert backup["schema"] == 5
            for key, row_type in transfer.LOG_ROWS.items():
                assert backup[key]["columns"] == list(row_type._fields)
            at = backup["activity"]["columns"].index("action")
            logged = [r[at] for r in backup["activity"]["rows"]]
            assert "Turned on" in logged and logged[-1] == "Downloaded a full backup"
            assert [len(backup[k]["rows"]) for k in ("wb_jobs", "wb_cards", "wb_lockouts")] == [3, 4, 2]

            # The preview says what's in it.
            status, info = await upload(p, "/api/restore/preview", file=raw)
            assert status == 200, info
            assert info["has_logs"] and (info["wb_jobs"], info["wb_cards"]) == (3, 4)
            assert info["activity"] == len(logged)

            # Restoring it here adds nothing: everything in it is here already.
            before = {t: table_count(p, t) for t in ("activity", "wb_jobs", "wb_cards", "wb_lockouts")}
            status, reply = await upload(p, "/api/restore", file=raw)
            assert status == 200, reply
            assert reply["added"] == {"activity": 0, "wb_jobs": 0, "wb_cards": 0}
            assert {t: table_count(p, t) for t in before} == {**before, "activity": before["activity"] + 1}
            assert actions(p)[-1] == "Restored a backup"

            # Lost here (or never here): they come back once, and the cards follow their job.
            p.db.conn.execute("DELETE FROM activity WHERE action = 'Turned on'")
            for job_id in (lost_job, replaced):
                p.db.conn.execute("DELETE FROM wb_lockouts WHERE card_id IN "
                                  "(SELECT id FROM wb_cards WHERE job_id = ?)", (job_id,))
                p.db.conn.execute("DELETE FROM wb_cards WHERE job_id = ?", (job_id,))
                p.db.conn.execute("DELETE FROM wb_jobs WHERE id = ?", (job_id,))
            p.db.conn.commit()
            plain = gzip.decompress(raw)   # a plain JSON file is taken too (told by its first bytes)
            status, reply = await upload(p, "/api/restore", file=plain)
            assert status == 200, reply
            assert reply["added"] == {"activity": 1, "wb_jobs": 2, "wb_cards": 2}
            assert actions(p).count("Turned on") == 1
            assert "Added 1 activity log line and 2 card jobs from it." in p.db.activity(1)[0]["detail"]
            cards = p.db.key_cards("all")
            assert len(cards) == 2 and made not in {c["job_id"] for c in cards}
            back = next(c for c in cards if c["card_number"] == lost["card_number"])
            assert back["job_id"] not in (lost_job, replaced) and back["state"] == "lost"
            assert back["holder_staff_id"] == p.dee and back["state_by"] == "Bob"
            assert {(lo["lock_id"], lo["status"]) for lo in back["lockouts"]} == {
                (101, "done"), (102, "pending")}
            new_replacement = next(c for c in cards if c["id"] != back["id"])
            job = p.db.wb_job(new_replacement["job_id"])
            assert job["target_card_id"] == back["id"] and job["records"] == [POOL]
            assert len(p.db.wb_jobs(100)) == 3   # the job that stayed isn't doubled
            # The log is in time order by ID, so trimming it by ID (on every action) stays right.
            times = [r[0] for r in p.db.conn.execute("SELECT ts FROM activity ORDER BY id")]
            assert times == sorted(times)

            # Saved copies from before a restore are compressed too, and restore by name.
            status, saved = await p.admin_call("GET", "/api/safety-backups")
            name = saved["backups"][0]["name"]
            assert name.endswith(".json.gz")
            status, headers, copy = await download(p, f"/api/safety-backups/{name}")
            assert status == 200 and headers["Content-Type"] == "application/gzip"
            assert json.loads(gzip.decompress(copy))["schema"] == 5
            status, info = await p.admin_call("POST", "/api/restore/preview", {"safety_backup": name})
            assert status == 200 and info["has_logs"], info
            status, reply = await p.admin_call("POST", "/api/restore", {"safety_backup": name})
            assert status == 200, reply
            status, reply = await p.admin_call("POST", "/api/restore",
                                               {"safety_backup": "../staff_portal.db"})
            assert status == 400 and "couldn't be read" in reply["error"]
    run(scenario())


def test_logs_from_another_system_are_added_to_its_own(tmp_path):
    async def scenario():
        p = Portal(tmp_path / "one")
        p.db.log("Ana", "Made 2 day passes")
        written_job(p, name="Smith party")
        written_job(p, name="Jones", staff_id=9999)   # made by someone since deleted
        async with p:
            status, headers, raw = await download(p, "/api/backup")
        q = Portal(tmp_path / "two")
        theirs = written_job(q, name="Lee family")
        q.db.log("Cy", "Signed in")
        async with q:
            status, reply = await upload(q, "/api/restore", file=raw)
            assert status == 200, reply
            assert reply["added"]["wb_jobs"] == 2 and reply["added"]["wb_cards"] == 4
            jobs = {j["name"]: j for j in q.db.wb_jobs(100)}
            assert set(jobs) == {"Smith party", "Jones", "Lee family"}
            assert jobs["Lee family"]["id"] == theirs and len(jobs["Lee family"]["cards"]) == 2
            # Jobs keep their makers when they're in the backup; someone it didn't have is no one.
            assert jobs["Smith party"]["staff_id"] == q.ana and jobs["Jones"]["staff_id"] is None
            assert all(len(j["cards"]) == 2 and j["cards"][0]["status"] == "written"
                       for j in jobs.values())
            assert {"Made 2 day passes", "Signed in"} <= set(actions(q))
    run(scenario())


def test_key_cards_from_another_system_list_by_when_they_were_made(tmp_path):
    async def scenario():
        p = Portal(tmp_path / "one")
        older = written_job(p, name="Eve", card_type="staff", count=1, holder_staff_id=p.dee)
        p.db.conn.execute("UPDATE wb_jobs SET ts = ts - 3600 WHERE id = ?", (older,))
        p.db.conn.commit()
        async with p:
            status, headers, raw = await download(p, "/api/backup")
        q = Portal(tmp_path / "two")
        written_job(q, name="Fay", card_type="staff", count=2, holder_staff_id=q.dee)
        async with q:
            status, reply = await upload(q, "/api/restore", file=raw)
            assert status == 200 and reply["added"]["wb_cards"] == 1, reply
            # The card from the backup has the newest ID here, but it was made first.
            cards = q.db.key_cards("all")
            assert [c["name"] for c in cards] == ["Fay", "Fay", "Eve"]
            assert cards[2]["id"] > cards[0]["id"]
            # Pages follow the same order, here and in the dashboard's list.
            first = q.db.key_cards("all", limit=2)
            rest = q.db.key_cards("all", limit=2, before=first[-1]["id"])
            assert [c["id"] for c in first + rest] == [c["id"] for c in cards]
            status, page = await q.admin_call("GET", f"/api/keycards?state=all&before={first[-1]['id']}")
            assert status == 200 and [c["id"] for c in page["cards"]] == [cards[2]["id"]]
    run(scenario())


def test_backups_from_before_logs_restore_as_before(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        written_job(p)
        async with p:
            status, backup = await p.admin_call("GET", "/api/backup")
            old = {k: v for k, v in backup.items() if k not in transfer.LOG_ROWS}
            old["schema"] = 3
            p.db.conn.execute("DELETE FROM activity")
            p.db.conn.commit()
            status, info = await p.admin_call("POST", "/api/restore/preview", {"backup": old})
            assert status == 200 and not info["has_logs"] and info["activity"] == 0
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": old})
            assert status == 200 and reply["added"] == {}, reply
            assert actions(p) == ["Restored a backup"] and len(p.db.wb_jobs(100)) == 1
            assert "Added" not in p.db.activity(1)[0]["detail"]
            # A damaged log section is refused like any other part of a backup; damaged rows in
            # one are only left out.
            bad = json.loads(json.dumps(backup))
            bad["activity"] = {"columns": ["ts"], "rows": "lots"}
            status, reply = await p.admin_call("POST", "/api/restore/preview", {"backup": bad})
            assert status == 400 and "activity log" in reply["error"]
            odd = json.loads(json.dumps(backup))
            odd["activity"]["rows"] += [["not", "a", "row"], None, [None] * 8,
                                        [time.time() + 5 * 86400, "Ana", None, None, "Future",
                                         None, 1, None]]
            odd["wb_jobs"]["rows"][0][odd["wb_jobs"]["columns"].index("kind")] = "teleport"
            p.db.conn.execute("DELETE FROM wb_jobs")
            p.db.conn.commit()
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": odd})
            assert status == 200, reply
            assert "Future" not in actions(p) and p.db.wb_jobs(100) == []
    run(scenario())


def test_merged_logs_keep_the_usual_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "ACTIVITY_MAX_ROWS", 10)
    p = Portal(tmp_path)
    now = time.time()
    c = p.db.conn
    c.execute("DELETE FROM activity")
    c.executemany("INSERT INTO activity (ts, actor, action) VALUES (?, ?, ?)",
                  [(now - 800 + i, "Then", f"A{i}") for i in range(1, 9)]
                  + [(now - 200 * 86400, "Long ago", "Too old")])
    old_jobs = [written_job(p, name=f"J{i}") for i in (1, 2, 3)]
    running = p.db.wb_job(old_jobs[2])["id"]
    p.db.update_wb_job(running, status="running", finished_at=None)
    p.db.update_wb_card(running, 2, status="pending", card_number=None)
    ancient = written_job(p, name="Ancient")
    for i, job_id in enumerate(old_jobs, 1):
        c.execute("UPDATE wb_jobs SET ts = ? WHERE id = ?", (now - 900 + i, job_id))
    c.execute("UPDATE wb_jobs SET ts = ? WHERE id = ?", (now - 3 * 365 * 86400, ancient))
    c.commit()
    backup = json.loads(json.dumps(transfer.make_backup(p.db, main.branding, "test")))
    monkeypatch.setattr(db_module, "WB_JOBS_MAX_ROWS", 3)

    # Here, since the backup: newer lines and jobs, under new IDs.
    c.execute("DELETE FROM activity")
    for table in ("wb_lockouts", "wb_cards", "wb_jobs"):
        c.execute(f"DELETE FROM {table}")
    c.executemany("INSERT INTO activity (ts, actor, action) VALUES (?, ?, ?)",
                  [(now - 100 + i, "Now", f"H{i}") for i in range(1, 7)])
    c.commit()
    for i in (1, 2):
        c.execute("UPDATE wb_jobs SET ts = ? WHERE id = ?", (now - 50 + i, written_job(p, name=f"K{i}")))
    c.commit()
    snap = transfer.clean_backup(backup, main.clean_settings, p.db.settings(),
                                 main.branding.clean_logo, p.db.known_tokens())
    dropped, added = p.db.replace_everything(snap)
    # The ten newest lines by time, whatever order they came in; the too-old one never came.
    # Only the lines and jobs the limits kept count as added (A5-A8, and J3 with its 2 cards).
    assert added == {"activity": 4, "wb_jobs": 1, "wb_cards": 2}
    assert actions(p) == ["A5", "A6", "A7", "A8", "H1", "H2", "H3", "H4", "H5", "H6"]
    # Restoring it again adds nothing, and says so: what the limits trimmed isn't counted again.
    dropped, added = p.db.replace_everything(snap)
    assert added == {"activity": 0, "wb_jobs": 0, "wb_cards": 0}
    assert actions(p) == ["A5", "A6", "A7", "A8", "H1", "H2", "H3", "H4", "H5", "H6"]
    p.db.log("Ana", "Later")
    assert actions(p) == ["A6", "A7", "A8", "H1", "H2", "H3", "H4", "H5", "H6", "Later"]
    # Jobs: the three newest by time. The one still running in the backup was stopped (nothing
    # runs it here); the ancient one was never added.
    jobs = {j["name"]: j for j in p.db.wb_jobs(100)}
    assert set(jobs) == {"J3", "K1", "K2"}
    assert jobs["J3"]["status"] == "cancelled" and jobs["J3"]["message"] == transfer.STOPPED_IN_BACKUP
    assert [card["status"] for card in jobs["J3"]["cards"]] == ["written", "cancelled"]
    # New jobs trim the same way, by time, never by ID.
    written_job(p, name="K3")
    assert {j["name"] for j in p.db.wb_jobs(100)} == {"K1", "K2", "K3"}
    assert table_count(p, "wb_cards") == 6


def test_big_backups_fit_the_limits(tmp_path, monkeypatch):
    """A busy property: a full activity log (50,000 lines) and 20,000 card jobs of two cards."""
    async def scenario():
        p = Portal(tmp_path)
        now = time.time()
        c = p.db.conn
        c.executemany(
            "INSERT INTO activity (ts, actor, role, entity_id, action, detail, ok, ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(now - 50_000 * 60 + i * 60, f"Front Desk PC (Person {i % 40})", "Front Desk",
              f"switch.soaking_tub_{i % 12}", ("Turned on", "Turned off", "Signed in")[i % 3],
              f"Party {i % 977}: Pool Gate, Spa Door until 8:00 PM", 1, f"203.0.113.{i % 250}")
             for i in range(db_module.ACTIVITY_MAX_ROWS)])
        records = json.dumps([POOL, {**POOL, "door": 901, "mac": "C540E09C8C91", "label": "Spa"}])
        jobs = 20_000
        c.executemany(
            """INSERT INTO wb_jobs (id, ts, card_type, name, count, records, summary, expires_at,
               allow_lockout, staff_id, device_id, actor, role, ip, encoder_url, status,
               finished_at, source) VALUES (?, ?, 'day_pass', ?, 2, ?, 'Pool Gate, Spa', ?, 0, ?,
               ?, ?, 'Front Desk', '203.0.113.9', 'ws://192.168.1.50:8102', 'done', ?, 'staff_app')""",
            [(i, now - jobs * 1800 + i * 1800, f"Guest party {i}", records, now + i, p.ana,
              p.device, "Front Desk PC (Ana)", now - jobs * 1800 + i * 1800 + 40)
             for i in range(1, jobs + 1)])
        c.executemany("INSERT INTO wb_cards (job_id, seq, status, card_number, written_at) "
                      "VALUES (?, ?, 'written', ?, ?)",
                      [(i, s, f"{3_000_000_000 + 2 * i + s}", now) for i in range(1, jobs + 1)
                       for s in (1, 2)])
        c.commit()
        async with p:
            status, headers, raw = await download(p, "/api/backup")
            assert status == 200
            unpacked = gzip.decompress(raw)
            # Far under the upload limit packed (16 MB, the old limit, with room to spare), and
            # under the unpacked limit with plenty of room.
            assert len(raw) < 16 * 2**20 < main.MAX_ADMIN_BODY
            assert len(unpacked) < transfer.MAX_BACKUP_BYTES // 2
            started = time.time()
            status, reply = await upload(p, "/api/restore", file=raw)
            assert status == 200, reply
            assert reply["added"] == {"activity": 0, "wb_jobs": 0, "wb_cards": 0}
            assert time.time() - started < 60
            assert table_count(p, "activity") == db_module.ACTIVITY_MAX_ROWS
            assert table_count(p, "wb_jobs") == jobs and table_count(p, "wb_cards") == 2 * jobs
            # The dashboard may unpack it and send it as JSON: more than the old 16 MB limit.
            assert len(unpacked) > 16 * 2**20
            resp = await p.admin.post("/api/restore/preview", data=b'{"backup":' + unpacked + b"}",
                                      headers={**ADMIN_HEADERS, "Content-Type": "application/json"})
            info = await resp.json()
            assert resp.status == 200 and (info["activity"], info["wb_jobs"]) == (50_000, jobs)
            # Over the limits: refused with a message, before anything changes.
            monkeypatch.setattr(transfer, "MAX_BACKUP_BYTES", len(unpacked) - 1)
            status, reply = await upload(p, "/api/restore/preview", file=raw)
            assert status == 400 and "too big" in reply["error"]
            monkeypatch.setattr(main, "MAX_ADMIN_BODY", len(raw) - 1)
            status, reply = await upload(p, "/api/restore/preview", file=raw)
            assert status == 400 and "too big" in reply["error"]
            # A download cut short is damaged, not half restored.
            monkeypatch.undo()
            status, reply = await upload(p, "/api/restore/preview", file=raw[:len(raw) // 2])
            assert status == 400 and "ends too soon" in reply["error"]
            status, reply = await upload(p, "/api/restore/preview", file=b"\x1f\x8bnot gzip")
            assert status == 400 and "damaged" in reply["error"]
            status, reply = await upload(p, "/api/restore/preview", file=b"PK\x03\x04 a zip")
            assert status == 400 and reply["error"] == transfer.NOT_OURS
    run(scenario())


# ---------------------------------------------------------------- migrate

def old_install(p):
    """Everything an install can have that a backup doesn't carry."""
    tthotel_account(p)
    p.db.update_settings({
        "connecteam_client_id": "ct-client", "connecteam_client_secret": "ct-Secret-123",
        "connecteam_usage": json.dumps({"day": "2026-10-25", "count": 7}),
        "tthotel_token": json.dumps({"token": "tok-123", "refresh": "ref-456",
                                     "expires_at": time.time() + 86400, "for": "client-1"}),
        "tthotel_usage": json.dumps({"month": "2026-10", "count": 12}),
        "wb_remote_lockout_tested": "1700000000"})
    p.db.update_staff(p.bob, ha_user="bob-ha", ha_refresh_token="ha-refresh-bob")
    p.db.update_device(p.device, ha_user="desk-ha", ha_refresh_token="ha-refresh-desk")
    p.db.set_wb_door(900, name="Pool", hidden=1)
    p.db.log("Ana", "Turned on", entity_id="switch.sauna")
    spare = p.db.create_device("Spare phone", p.front_desk, None)   # waiting to be set up
    return spare, written_job(p)


def sealed_with(snapshot, code, **header) -> dict:
    """A migration file sealed with its code but with these header fields, as only someone who
    chose the code could make it (the header is part of what the code locks)."""
    file = {k: v for k, v in migrate.seal(snapshot, code, main.VERSION).items() if k != "data"}
    file.update(header)
    key = migrate._key(code, base64.b64decode(file["salt"]), migrate.SCRYPT_N, migrate.SCRYPT_R,
                       migrate.SCRYPT_P)
    file["data"] = base64.b64encode(AESGCM(key).encrypt(
        base64.b64decode(file["nonce"]), transfer.pack_backup(snapshot),
        migrate._header_bytes(file))).decode()
    return file


def test_migrate_moves_everything_and_phones_stay_signed_in(tmp_path, caplog):
    caplog.set_level("DEBUG")

    async def scenario():
        p = Portal(tmp_path / "old")
        spare, job = old_install(p)
        spare_code = p.db.device(spare)["enroll_code"]
        numbers = [c["card_number"] for c in p.db.wb_job(job)["cards"]]
        async with p:
            status, made = await p.admin_call("POST", "/api/migrate/create")
            assert status == 200, made
            code = made["code"]
            assert CODE_RE.match(code)
            assert re.fullmatch(r"staff-portal-migration-[\d-]+\.json", made["filename"])
            assert 86000 < made["expires_at"] - time.time() <= 86400
            # The file, as the dashboard saves it: JSON with a readable header.
            file = made["file"]
            sealed = json.dumps(file).encode()
            assert file["format"] == "staff-portal-migrate" and file["version"] == 1
            assert file["expires_at"] - file["created_at"] == 24 * 3600
            assert file["kdf"] == {"name": "scrypt", "n": 2**16, "r": 8, "p": 1}
            assert file["cipher"] == "AES-256-GCM" and list(file)[-1] == "data"
            for secret in (SECRET, PASSWORD, "ct-Secret-123", "ha-refresh-bob", "Smith party",
                           code, code.replace("-", "")):
                assert secret.encode() not in sealed
            assert actions(p)[-1] == "Created a migration file"
            status, state = await p.admin_call("GET", "/api/state")
            old_token, pins = p.token, p.pins

        f = Fresh(tmp_path / "new")
        f.db.log("Admin", "Updated settings")   # a fresh install may have a line or two
        async with f:
            assert f.db.setup_found() == {}
            # As the dashboard sends it (the file read as JSON), typed as people type: lower
            # case, spaces for dashes.
            status, reply = await f.admin_call("POST", "/api/migrate/restore",
                                               {"file": file, "code": code.lower().replace("-", " ")})
            assert status == 200, reply
            summary = reply["summary"]
            assert (summary["staff"], summary["roles"], summary["devices"]) == (4, 3, 2)
            assert summary["signed_in"] == 1 and summary["needs_setup"] == []
            assert summary["safety_backup"].endswith(".json.gz")

            # The computer stays signed in with its old token, and people's PINs still work.
            resp = await f.staff.get("/api/me", headers={"Authorization": f"Bearer {old_token}"})
            assert resp.status == 200 and (await resp.json())["device"] == "Front Desk PC"
            f.pins = pins
            who = await f.sign_in(p.ana, token=old_token)
            status, _ = await f.call("GET", "/api/me", who)
            assert status == 200
            assert (p.device, db_module.hash_token(old_token)) in f.db.known_tokens()
            # A phone still waiting keeps the setup code already handed out.
            assert f.db.device(spare)["enroll_code"] == spare_code
            assert f.db.claim_enroll_code(spare_code) is not None

            # Secrets, TT Hotel's sign-in and request counts, and remote lock-out's test.
            s = f.db.settings()
            assert (s["tthotel_client_secret"], s["tthotel_password_md5"]) == (SECRET, PASSWORD)
            assert s["connecteam_client_secret"] == "ct-Secret-123"
            assert json.loads(s["tthotel_token"])["token"] == "tok-123"
            assert json.loads(s["connecteam_usage"])["count"] == 7
            assert json.loads(s["tthotel_usage"])["count"] == 12
            assert s["wb_remote_lockout_tested"] == "1700000000"
            # Home Assistant links, the whole door list, and the logs.
            assert f.db.staff(p.bob)["ha_refresh_token"] == "ha-refresh-bob"
            assert f.db.device(p.device)["ha_refresh_token"] == "ha-refresh-desk"
            assert len(f.db.wb_doors()) == len(DOORS)
            assert (f.db.wb_door(900)["name"], f.db.wb_door(900)["hidden"]) == ("Pool", 1)
            assert f.db.wb_door(101)["mac"] == DOORS[0]["mac"]
            logged = actions(f)
            assert {"Turned on", "Created a migration file", "Updated settings"} <= set(logged)
            assert logged.index("Migrated from another install") > logged.index(
                "Created a migration file")
            assert [c["card_number"] for c in f.db.wb_jobs(10)[0]["cards"]] == numbers

            # One use: this install isn't fresh any more.
            status, reply = await upload(f, "/api/migrate/restore", file=sealed, code=code)
            assert status == 409 and "already has" in reply["error"]

            # The code was never kept or logged anywhere.
            status, state = await f.admin_call("GET", "/api/state")
            for found in (json.dumps(state), json.dumps(f.db.activity(500)),
                          json.dumps(f.db.settings()), caplog.text):
                assert code not in found and code.replace("-", "") not in found
            for folder in (tmp_path, os.environ["DATA_DIR"]):
                for root, _, files in os.walk(folder):
                    for name in files:
                        with open(os.path.join(root, name), "rb") as fh:
                            content = fh.read()
                        assert code.encode() not in content
                        assert code.replace("-", "").encode() not in content
    run(scenario())


def test_migration_files_that_are_refused(tmp_path):
    async def scenario():
        p = Portal(tmp_path / "old")
        old_install(p)
        snapshot = migrate.make_snapshot(p.db, main.branding, main.VERSION)
        code = migrate.new_code()
        packed = lambda file: json.dumps(file).encode()
        made = migrate.seal(snapshot, code, main.VERSION)
        good = packed(made)
        expired = packed(migrate.seal(snapshot, code, main.VERSION, now=time.time() - 25 * 3600))
        newer = packed(migrate.seal(snapshot, code, "99.0.0"))
        later = packed({**made, "expires_at": made["expires_at"] + 3600})
        extra = packed({**made, "note": "added later"})
        data = bytearray(base64.b64decode(made["data"]))
        data[-1] ^= 1
        flipped = packed({**made, "data": base64.b64encode(bytes(data)).decode()})
        bad_kdf = packed({**made, "kdf": {**made["kdf"], "n": 2**30}})
        # Times that aren't real dates, sealed in with the right code: NaN would never expire,
        # and a huge one can't be shown as a date (after everything was already replaced).
        odd_times = [packed(sealed_with(snapshot, code, **header)) for header in (
            {"expires_at": float("nan")}, {"expires_at": float("inf")}, {"created_at": 1e300})]
        async with p:
            status, headers, backup = await download(p, "/api/backup")
            # Not onto an install that's set up: nothing changes there.
            status, reply = await upload(p, "/api/migrate/restore", file=good, code=code)
            assert status == 409 and "already has 4 staff members" in reply["error"]
            assert p.db.staff(p.ana)["name"] == "Ana"

        f = Fresh(tmp_path / "new")
        async with f:
            for sealed, typed, message in (
                    (good, migrate.new_code(), migrate.WRONG_CODE),
                    (flipped, code, migrate.WRONG_CODE),
                    (later, code, migrate.WRONG_CODE),     # the header is part of the lock
                    (extra, code, migrate.WRONG_CODE),
                    (bad_kdf, code, migrate.DAMAGED),       # never tried: too much memory
                    *((odd, code, migrate.DAMAGED) for odd in odd_times),
                    (expired, code, migrate.EXPIRED),
                    (newer, code, "newer version of Staff Portal (99.0.0)"),
                    (backup, code, "That's a backup or export file"),
                    (b"PK\x03\x04 a zip", code, migrate.NOT_MIGRATION),
                    (gzip.compress(good), code, None),     # someone zipped it: still fine
                    (good, "ABCD-EFGH", migrate.BAD_CODE),
                    (good, code.replace(code[0], "0"), migrate.BAD_CODE)):
                if message is None:
                    assert migrate.open_file(sealed, code, main.VERSION)[1]["format"] == (
                        "staff-portal-migrate")
                    assert migrate.open_file(good.decode(), code, main.VERSION)[0]["schema"] == 5
                    continue
                status, reply = await upload(f, "/api/migrate/restore", file=sealed, code=typed)
                assert status == 400 and message in reply["error"], (message, reply)
            status, reply = await upload(f, "/api/migrate/restore", file=b"", code=code)
            assert status == 400 and "Choose the migration file" in reply["error"]
            status, reply = await upload(f, "/api/migrate/restore", code=code)   # not even JSON
            assert status == 400
            status, reply = await f.admin_call("POST", "/api/migrate/restore", {"code": code})
            assert status == 400 and "Choose the migration file" in reply["error"]
            status, reply = await f.admin_call("POST", "/api/migrate/restore",
                                               {"file": "not json", "code": code})
            assert status == 400 and reply["error"] == migrate.NOT_MIGRATION
            # A migration file isn't a backup, and says so.
            status, reply = await upload(f, "/api/restore/preview", file=good)
            assert status == 400 and "That's a migration file" in reply["error"]
            assert f.db.setup_found() == {} and actions(f) == []
            assert f.db.settings()["tthotel_client_secret"] == ""
            # The good file still works afterwards (uploaded as the file itself, this time).
            status, reply = await upload(f, "/api/migrate/restore", file=good, code=code)
            assert status == 200, reply
            assert reply["summary"]["staff"] == 4 and reply["summary"]["signed_in"] == 1
    run(scenario())


def test_migration_codes_and_one_at_a_time(tmp_path):
    codes = {migrate.new_code() for _ in range(200)}
    assert len(codes) == 200 and all(CODE_RE.match(c) for c in codes)
    assert len(migrate.CODE_ALPHABET) == 32 and migrate.CODE_LENGTH * 5 >= 100
    assert not set("0O1I") & set(migrate.CODE_ALPHABET)
    assert migrate.clean_code(" abcd-efgh jklm-npqr-stuv ") == "ABCDEFGHJKLMNPQRSTUV"
    assert migrate.newer("0.9.0", "0.8.5") and not migrate.newer("0.8.5", "0.8.5")
    assert not migrate.newer("0.8.5", "dev") and not migrate.newer("dev", "0.8.5")

    async def scenario():
        p = Portal(tmp_path)
        async with p:
            status, first = await p.admin_call("POST", "/api/migrate/create")
            status, second = await p.admin_call("POST", "/api/migrate/create")
            assert first["code"] != second["code"]
            assert first["file"]["salt"] != second["file"]["salt"]
            assert first["file"]["nonce"] != second["file"]["nonce"]
            # A file opens only with its own code.
            try:
                migrate.open_file(first["file"], second["code"], main.VERSION)
                raise AssertionError("opened with another file's code")
            except ValueError as err:
                assert str(err) == migrate.WRONG_CODE
            # Changes need the dashboard's header, as everywhere in the admin app.
            resp = await p.admin.post("/api/migrate/create")
            assert resp.status == 403
            # One at a time: a restore or another file waits for the one running.
            main.transfer_running[0] = True
            try:
                status, reply = await p.admin_call("POST", "/api/migrate/create")
                assert status == 409 and reply["error"] == main.BUSY
            finally:
                main.transfer_running[0] = False
    run(scenario())


def test_migration_keeps_tt_hotel_pauses_and_stops(tmp_path):
    """TT Hotel refused the old install's credentials, or asked it to wait: the new install, with
    the same credentials, doesn't ask again early, and its next request doesn't lose the stop."""
    async def scenario():
        p = Portal(tmp_path / "old")
        old_install(p)
        refused = "TT Hotel refused the username or password."
        p.db.update_settings({"tthotel_usage": json.dumps({
            "month": time.strftime("%Y-%m", time.gmtime()), "count": 12,
            "paused_until": time.time() + 600, "pause_reason": "TT Hotel asked to slow down.",
            "halted": refused, "halt_everything": False})})
        code = migrate.new_code()
        file = json.dumps(migrate.seal(migrate.make_snapshot(p.db, main.branding, main.VERSION),
                                       code, main.VERSION)).encode()
        f = Fresh(tmp_path / "new")
        tt = main.tthotel
        try:
            async with f:
                assert tt.halted is None and tt.paused_until == 0
                status, reply = await upload(f, "/api/migrate/restore", file=file, code=code)
                assert status == 200, reply
                assert tt.halted == refused and tt.paused_until > time.time() + 500
                for kind in ("api", "info"):
                    try:
                        tt._spend(kind)
                        raise AssertionError(f"asked TT Hotel ({kind}) during its pause")
                    except TTHotelError:
                        pass
                # Once the pause is over, a request without the login may go; the stop for the
                # login is saved with the count again.
                tt.paused_until = 0.0
                tt._spend("info")
                saved = json.loads(f.db.settings()["tthotel_usage"])
                assert saved["count"] == 13 and saved["halted"] == refused

            # A pause of the new install's own that ends later is kept too.
            f = Fresh(tmp_path / "newer")
            async with f:
                tt._pause(time.time() + 1200, "TT Hotel asked this install to wait.")
                status, reply = await upload(f, "/api/migrate/restore", file=file, code=code)
                assert status == 200, reply
                assert tt.paused_until > time.time() + 1100 and tt.halted == refused
                saved = json.loads(f.db.settings()["tthotel_usage"])
                assert saved["paused_until"] == tt.paused_until and saved["halted"] == refused
        finally:
            tt.paused_until, tt.pause_reason = 0.0, None
            tt.forget()
    run(scenario())


def test_dashboard_is_told_the_fresh_install_rule(tmp_path):
    async def scenario():
        f = Fresh(tmp_path)
        async with f:
            status, state = await f.admin_call("GET", "/api/state")
            assert status == 200 and state["not_fresh"] is None
            f.db.create_area("Pool")   # no staff, phones, computers or roles, but not fresh
            status, state = await f.admin_call("GET", "/api/state")
            assert state["not_fresh"] == main.not_fresh() and "already has 1 area." in state["not_fresh"]
    run(scenario())
