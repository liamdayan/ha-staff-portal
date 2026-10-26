"""The HR portal's building blocks on their own: encryption and the HR key, authenticator codes,
PIN hashes, lockouts and throttles, who may see whom, people from Connecteam (with the safety
catch), the audit log's chain, notifications and files.

Nothing here talks to a real Connecteam, Google, TT Hotel or Home Assistant.
"""

import asyncio
import io
import json
import os
import sqlite3
import time

import pytest

import hr_helpers  # noqa: F401  (points Connecteam at a closed local port)
from hr import access, audit, crypto, files, notify, people, signin, totp
from hr.hrdb import HRDatabase


@pytest.fixture
def ring(tmp_path):
    keyring = crypto.Keyring(str(tmp_path / "hr"))
    assert keyring.load(create=True) == "ready"
    crypto.use(keyring)
    yield keyring
    crypto.use(None)


@pytest.fixture
def db(tmp_path, ring):
    database = HRDatabase(str(tmp_path / "hr"))
    yield database
    database.close()


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- encryption and the HR key

def test_fields_and_files_are_sealed_for_one_purpose(ring):
    blob = crypto.seal(b"OLCC 1234567", "card.number")
    assert b"1234567" not in blob and blob[0] == crypto.VERSION
    assert crypto.unseal(blob, "card.number") == b"OLCC 1234567"
    with pytest.raises(crypto.Damaged):
        crypto.unseal(blob, "people.mobile")            # moved to another field: refused
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(crypto.Damaged):
        crypto.unseal(tampered, "card.number")
    assert crypto.unseal_text(crypto.seal_text("541-555-0100", "people.mobile"),
                              "people.mobile") == "541-555-0100"
    assert crypto.seal_text(None, "x") is None
    assert run(crypto.unseal_async(run(crypto.seal_async(b"x" * 100_000, "file")), "file")) == b"x" * 100_000
    crypto.use(None)
    with pytest.raises(crypto.Locked):
        crypto.seal(b"x", "anything")


def test_keep_the_key_out_of_backups_and_unlock_after_a_restore(tmp_path, ring):
    folder = tmp_path / "hr"
    key = ring.key()
    assert (folder / "master.key").exists() and not ring.kept_out()
    code = ring.keep_out(True)
    assert len(code.replace("-", "")) == 20
    assert not (folder / "master.key").exists()
    assert (folder / "outside-backups" / "master.key").exists()
    assert key not in (folder / "master.key.recovery").read_bytes()
    # A restore: the folder outside backups isn't in the backup.
    (folder / "outside-backups" / "master.key").unlink()
    (folder / "hr.db").write_bytes(b"")
    restored = crypto.Keyring(str(folder))
    assert restored.load(create=True) == "locked"           # never makes a new key
    with pytest.raises(ValueError, match="isn't this HR portal's"):
        restored.unlock(crypto.new_recovery_key())
    with pytest.raises(ValueError, match="20 letters"):
        restored.unlock("nope")
    restored.unlock(code.lower().replace("-", " "))
    assert restored.ready and restored.key() == key
    # Back in backups: the key moves back and the locked copy goes.
    restored.keep_out(False)
    assert (folder / "master.key").exists() and not restored.kept_out()
    assert not (folder / "outside-backups" / "master.key").exists()
    # Without the key or a locked copy, it's lost (never silently replaced).
    (folder / "master.key").unlink()
    assert crypto.Keyring(str(folder)).load(create=True) == "lost"


# ---------------------------------------------------------------- authenticator codes

def test_totp_matches_rfc_6238_and_each_code_works_once():
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp.code_at(secret, 1) == "287082"              # RFC 6238, T = 59 s, last 6 digits
    assert totp.code_at(secret, 37037036) == "081804"       # T = 1111111109
    now = 1_700_000_000
    step = now // 30
    code = totp.code_at(secret, step)
    assert totp.check(secret, code, 0, now) == step
    assert totp.check(secret, code, step, now) is None      # used: never again
    assert totp.check(secret, totp.code_at(secret, step - 1), 0, now) == step - 1   # a slow clock
    assert totp.check(secret, totp.code_at(secret, step - 2), 0, now) is None
    assert totp.check(secret, "12345", 0, now) is None
    assert totp.uri(secret, "Ana Lee", "Hot Lake HR").startswith("otpauth://totp/Hot%20Lake%20HR%3AAna%20Lee?secret=")


# ---------------------------------------------------------------- PINs, locks and throttles

def test_kiosk_code_hashes_are_slow_salted_and_peppered(tmp_path, ring):
    stored = signin.hash_code("4821")
    assert stored.startswith("s1$14$8$1$") and "4821" not in stored
    assert signin.check_code("4821", stored) and not signin.check_code("4822", stored)
    assert signin.hash_code("4821") != stored                # salted
    other = crypto.Keyring(str(tmp_path / "other"))
    other.load(create=True)
    crypto.use(other)
    assert not signin.check_code("4821", stored)             # the pepper comes from the HR key
    assert signin.clean_pin("12 34") is None and signin.clean_pin("123456789") is None
    assert signin.normalize_mobile("+1 (541) 555-0100") == "5415550100"


def test_wrong_tries_lock_for_a_while_then_until_hr_unlocks(db):
    db.conn.execute("INSERT INTO people (id, name, added_at) VALUES ('7', 'Ana', 0)")
    now = time.time()
    for i in range(4):
        assert signin.wrong_try(db, db.person("7"), now + i) is None
    assert signin.wrong_try(db, db.person("7"), now + 4) == "locked"
    assert signin.locked(db.person("7"), now + 5) and not signin.locked(db.person("7"), now + 905)
    for i in range(9):
        signin.wrong_try(db, db.person("7"), now + 1000 + i)
    assert signin.wrong_try(db, db.person("7"), now + 1100) == "hard_locked"   # 15 in a day
    assert signin.locked(db.person("7"), now + 86400 * 3)
    signin.unlock(db, "7")
    assert not signin.locked(db.person("7"), now + 1101)


def test_throttles_per_address_and_overall():
    t = signin.Throttle()
    for _ in range(signin.ADDRESS_LIMIT):
        assert t.refusal("203.0.113.5") is None
        t.failed("203.0.113.5")
    assert t.refusal("203.0.113.5") == signin.SLOW_DOWN
    assert t.refusal("198.51.100.7") is None
    for i in range(signin.OVERALL_LIMIT):
        t.failed(f"10.0.{i // 250}.{i % 250}")
    assert t.refusal("198.51.100.7") == signin.PAUSED      # everyone waits a few minutes
    assert t.refusal("198.51.100.7", time.time() + signin.PAUSE_SECONDS + 1) is None


def test_sessions_idle_out_end_after_eight_hours_and_keep_five(db):
    db.conn.execute("INSERT INTO people (id, name, added_at) VALUES ('7', 'Ana', 0)")
    tokens = [signin.new_session(db, "7", "203.0.113.5", "phone") for _ in range(6)]
    assert db.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 5
    assert signin.session_for(db, tokens[0]) is None and signin.session_for(db, tokens[-1])
    stored = db.conn.execute("SELECT token_hash FROM sessions").fetchall()
    assert all(tokens[-1] not in r[0] for r in stored)        # stored hashed
    now = time.time()
    assert signin.session_for(db, tokens[-1], now=now + 14 * 60)
    assert signin.session_for(db, tokens[-2], now=now + 16 * 60) is None   # 15 minutes idle
    db.set_settings({"session_minutes": "30"})
    db.conn.execute("UPDATE sessions SET created_at = ?", (now - 8 * 3600 - 1,))
    assert signin.session_for(db, tokens[-1], now=now + 60) is None       # 8 hours at most
    assert "HttpOnly" in signin.cookie("x") and "Secure" in signin.cookie("x")
    assert "SameSite=Strict" in signin.cookie("x") and signin.cookie("x").startswith("__Host-hr=")


def test_recovery_codes_work_once(db):
    db.conn.execute("INSERT INTO people (id, name, added_at) VALUES ('7', 'Ana', 0)")
    codes = signin.enable_totp(db, "7", totp.new_secret(), 0)
    assert len(codes) == 8 and len(set(codes)) == 8
    assert signin.use_recovery_code(db, "7", codes[0].lower())
    assert not signin.use_recovery_code(db, "7", codes[0])
    assert signin.recovery_codes_left(db, "7") == 7
    assert not signin.use_recovery_code(db, "8", codes[1])


# ---------------------------------------------------------------- who may see whom

def person(pid, groups=(), managers=(), archived=0):
    return {"id": pid, "name": pid, "groups": json.dumps(list(groups)),
            "managers": json.dumps(list(managers)), "archived": archived}


def viewer(pid, roles=(), groups=None):
    return access.Viewer(pid, pid, frozenset(roles), groups or {})


def test_who_sees_whom():
    hr, director = viewer("hr", ["hr_admin"]), viewer("dir", ["director"])
    pub_manager = viewer("mgr", ["manager"], {"501": True})
    cook, bartender = person("cook", ["502"]), person("bar", ["501"], managers=["dir"])
    other_manager = person("mgr2", ["501"])
    report_manager = person("mgr3", ["502"], managers=["mgr"])
    gone = person("gone", ["501"], archived=1)
    rel = access.relation
    assert rel(hr, gone, set()) == "hr" and rel(hr, cook, set()) == "hr"
    assert rel(director, gone, set()) is None and rel(pub_manager, gone, set()) is None
    assert rel(director, cook, set()) == "director"
    assert rel(director, other_manager, {"manager"}) == "director"
    assert rel(director, person("hr2"), {"hr_admin"}) is None       # HR admins and directors...
    assert rel(director, person("dir2"), {"director"}) is None
    assert rel(viewer("dir2", ["director"]), bartender, set()) == "director"
    assert rel(director, person("dir3", managers=["dir"]), {"director"}) == "director"  # ...unless a report
    assert rel(pub_manager, bartender, set()) == "manager"
    assert rel(pub_manager, cook, set()) is None                     # not their department
    assert rel(pub_manager, other_manager, {"manager"}) is None      # managers don't see managers
    assert rel(pub_manager, report_manager, {"manager"}) == "manager"   # unless a direct report
    assert rel(viewer("cook"), cook, set()) == "self"
    assert rel(viewer("cook"), bartender, set()) is None
    assert rel(hr, person("hr"), {"hr_admin"}) == "self"             # own file: as an employee
    # Access follows the person: a move out of the department ends it at once.
    moved = person("bar", ["502"])
    assert rel(pub_manager, moved, set()) is None


def test_visibility_levels_never_widen():
    items = [{"t": "a", "visibility": "employee"}, {"t": "b", "visibility": "managers"},
             {"t": "c", "visibility": "directors"}, {"t": "d", "visibility": "hr"},
             {"t": "e", "visibility": "restricted"}, {"t": "f", "visibility": "typo"}]
    seen = lambda rel: [i["t"] for i in access.visible(items, rel)]  # noqa: E731
    assert seen("self") == ["a"]
    assert seen("manager") == ["a", "b"]
    assert seen("director") == ["a", "b", "c"]
    assert seen("hr") == ["a", "b", "c", "d", "e", "f"]
    assert seen(None) == []
    with pytest.raises(access.Refused):
        access.not_own_work(viewer("hr", ["hr_admin"]), "hr")
    access.not_own_work(viewer("hr", ["hr_admin"]), "someone else")


def test_startup_refuses_a_route_without_a_rule():
    from aiohttp import web

    async def no_rule(request):
        return web.Response()
    app = web.Application()
    app.router.add_get("/api/oops", no_rule)
    with pytest.raises(RuntimeError, match="/api/oops"):
        access.check_routes(app)
    app = web.Application()
    app.router.add_get("/api/fine", access.rule("hr")(no_rule))
    access.check_routes(app)


# ---------------------------------------------------------------- people from Connecteam

def user(uid, name, code="1234", groups=("501",), mobile=None, email=None, fields=None,
         created=1_700_000_000):
    return {"id": uid, "name": name, "kiosk_code": code, "groups": list(groups), "mobile": mobile,
            "email": email, "created_at": created, "fields": fields or {}}


def test_dates_from_connecteam():
    assert people.parse_date("2024-10-25") == "2024-10-25"
    assert people.parse_date("10/25/2024") == "2024-10-25"
    assert people.parse_date("05/04/2024") == "2024-05-04"
    assert people.parse_date("05/04/2024", "dmy") == "2024-04-05"
    assert people.parse_date("25/10/2024") == "2024-10-25"          # can only be day/month
    assert people.parse_date(1729814400) == "2024-10-25"
    assert people.parse_date(1729814400000) == "2024-10-25"
    assert people.parse_date("not a date") is None and people.parse_date("02/30/2024") is None


def test_people_sync_encrypts_contacts_and_finds_start_dates(tmp_path, db):
    db.set_settings({"ct_fields": json.dumps([{"id": "11", "name": "Employment Start Date"},
                                              {"id": "12", "name": "Direct manager"}])})
    result = run(people.apply_users(db, [
        user("1", "Ana Lee", mobile="+1 541-555-0100", email="ana@example.com",
             fields={"11": "10/25/2024", "12": [2]}),
        user("2", "Bo Diaz", code=None, created=1_700_000_000)]))
    assert result["added"] == 2 and result["no_code"] == 1 and result["warning"] is None
    ana, bo = db.person("1"), db.person("2")
    assert (ana["start_date"], ana["start_estimated"]) == ("2024-10-25", 0)
    assert (bo["start_date"], bo["start_estimated"]) == ("2023-11-14", 1)
    assert json.loads(ana["managers"]) == ["2"]
    assert people.mobile_of(ana) == "+1 541-555-0100" and people.email_of(ana) == "ana@example.com"
    assert signin.find_person(db, "(541) 555-0100")["id"] == "1"
    assert bo["kiosk_hash"] is None and signin.check_code("1234", ana["kiosk_hash"])
    db.conn.commit()
    raw = open(os.path.join(tmp_path, "hr", "hr.db"), "rb").read() + \
        (open(os.path.join(tmp_path, "hr", "hr.db-wal"), "rb").read()
         if os.path.exists(os.path.join(tmp_path, "hr", "hr.db-wal")) else b"")
    assert b"555-0100" not in raw and b"ana@example.com" not in raw and b"1234" not in raw


def test_safety_catch_and_archiving(db):
    team = [user(str(i), f"Person {i}", code=f"{1000 + i}") for i in range(8)]
    run(people.apply_users(db, team))
    tokens = {u["id"]: signin.new_session(db, u["id"], "ip", "agent") for u in team}
    # Connecteam answers with almost nobody: nobody is cut off, and HR is warned.
    result = run(people.apply_users(db, team[:3]))
    assert result["archived"] == 0 and "far fewer" in result["warning"]
    assert db.setting("sync_warning") and signin.session_for(db, tokens["7"])
    result = run(people.apply_users(db, []))
    assert result["archived"] == 0 and result["warning"]
    # One person really left: their access ends, their record stays.
    result = run(people.apply_users(db, team[:7]))
    assert result["archived"] == 1 and result["warning"] is None and not db.setting("sync_warning")
    gone = db.person("7")
    assert gone["archived"] and gone["access_ended"] and signin.session_for(db, tokens["7"]) is None
    # Back in Connecteam: their file is active, but sign-in waits for HR.
    result = run(people.apply_users(db, team))
    assert result["returned"] == 1
    back = db.person("7")
    assert not back["archived"] and back["access_ended"]
    actions = [line["action"] for line in audit.lines(db, 50)]
    assert "Archived in Connecteam: HR access ended" in actions
    assert "Safety catch: Connecteam listed far fewer people" in actions


def test_a_changed_kiosk_code_ends_sessions_and_moves_are_logged(db):
    run(people.apply_users(db, [user("1", "Ana", code="1111", groups=["501"])]))
    token = signin.new_session(db, "1", "ip", "agent")
    run(people.apply_users(db, [user("1", "Ana", code="1111", groups=["501"])]))
    assert signin.session_for(db, token)                       # the same code: nothing changes
    people.kiosk_seen.clear()                                  # after a restart: checked again
    run(people.apply_users(db, [user("1", "Ana", code="1111", groups=["501", "502"])]))
    assert signin.session_for(db, token)
    run(people.apply_users(db, [user("1", "Ana", code="2222", groups=["502"])]))
    assert signin.session_for(db, token) is None
    assert signin.check_code("2222", db.person("1")["kiosk_hash"])
    actions = [line["action"] for line in audit.lines(db, 50)]
    assert "Kiosk code changed in Connecteam: signed out of HR" in actions
    assert "Department changed in Connecteam" in actions


def test_archived_list_and_its_safety_catch(db):
    run(people.apply_users(db, [user("1", "Ana"), user("2", "Bo")]))
    assert people.mark_archived(db, [{"id": "2"}]) == 1
    assert db.person("2")["archived"] and db.person("2")["access_ended"]
    db.set_settings({"sync_warning": "held"})
    assert people.mark_archived(db, [{"id": "1"}]) == 0


# ---------------------------------------------------------------- the audit log

def test_audit_log_is_chained_and_rows_never_change(tmp_path, db):
    for i in range(5):
        audit.record(db, f"Action {i}", actor_id="1", actor="Ana", target_id="2", target="Bo",
                     ip="203.0.113.5", security=i % 2 == 0)
    assert audit.check(db.conn)["ok"] and audit.check(db.conn)["lines"] == 5
    assert [line["action"] for line in audit.lines(db, 10, security=True)] == [
        "Action 4", "Action 2", "Action 0"]
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute("UPDATE audit SET detail = 'x'")
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute("DELETE FROM audit")
    db.conn.rollback()
    # Someone with the file (who can drop the guard) changes a line: the chain shows it.
    db.conn.execute("DROP TRIGGER audit_no_update")
    db.conn.execute("UPDATE audit SET actor = 'Someone else' WHERE id = 3")
    db.conn.commit()
    result = audit.check(db.conn)
    assert not result["ok"] and result["first_bad"] == 3


# ---------------------------------------------------------------- notifications

@pytest.fixture
def chat():
    sent = []
    notify.chat = lambda pid, text: sent.append((pid, text))
    notify.url = lambda: "https://hr.example.com"
    yield sent
    notify.chat = None


def test_notifications_inbox_quiet_hours_and_one_a_day(db, chat):
    db.conn.execute("INSERT INTO people (id, name, added_at) VALUES ('1', 'Ana', 0), ('2', 'Max', 0)")
    db.set_role("2", "manager", True)
    db.set_settings({"quiet_start": "00:00", "quiet_end": "23:59"})   # quiet nearly all day
    entry = notify.notify("1", "card_confirmed", db=db, once="card-9")
    assert entry and notify.unread(db, "1") == 1
    assert notify.notify("1", "card_confirmed", db=db, once="card-9") is None   # a repeat run
    row = db.conn.execute("SELECT * FROM outbox").fetchone()
    assert row["due_at"] > time.time() and "https://hr.example.com" in row["text"]
    assert "card" not in row["text"].lower()                   # never the details
    notify.notify("1", "something_else", db=db)
    assert db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1   # one a day
    notify.notify("1", "expired", urgent=True, db=db)
    assert db.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 2   # urgent goes too
    # Managers: non-urgent things wait for the daily summary; urgent ones go now.
    notify.notify("2", "card_uploaded", db=db)
    notify.notify("2", "card_uploaded_2", db=db)
    assert db.conn.execute("SELECT COUNT(*) FROM outbox WHERE person_id = '2'").fetchone()[0] == 0
    assert notify.summaries(db) == 1
    summary = db.conn.execute("SELECT * FROM outbox WHERE person_id = '2'").fetchone()
    assert "2 new items" in summary["text"] and summary["due_at"] <= time.time()
    assert notify.summaries(db) == 0
    assert run(notify.send_due(db)) == 1 and chat[0][0] == "2"
    notify.mark_read(db, "1")
    assert notify.unread(db, "1") == 0


def test_messages_are_retried_then_need_attention(db):
    db.conn.execute("INSERT INTO people (id, name, added_at) VALUES ('1', 'Ana', 0)")
    calls = []

    def failing(pid, text):
        calls.append(pid)
        raise RuntimeError("Missing permission chat.write")
    notify.chat = failing
    try:
        notify.notify("1", "x", urgent=True, db=db)
        now = time.time()
        for i in range(notify.MAX_TRIES):
            run(notify.send_due(db, now + 10 ** 7 * (i + 1)))
        row = db.conn.execute("SELECT * FROM outbox").fetchone()
        assert row["status"] == "needs_attention" and row["tries"] == 8 and "chat.write" in row["error"]
        assert notify.retry_after(1) == 60 and notify.retry_after(5) == 21600 and notify.retry_after(6) == 86400

        # A refusal by HR's request budget waits without counting a try.
        class HRBusy(Exception):
            pass

        def busy(pid, text):
            raise HRBusy("Staff Access has used half of its Connecteam requests for today")
        notify.chat = busy
        notify.retry(db, row["id"])
        run(notify.send_due(db))
        row = db.conn.execute("SELECT * FROM outbox").fetchone()
        assert row["status"] == "queued" and row["tries"] == 0 and row["due_at"] > time.time()
    finally:
        notify.chat = None


# ---------------------------------------------------------------- files

def jpeg_with_location() -> bytes:
    from PIL import Image
    img = Image.new("RGB", (4000, 1000), "white")
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"                        # camera make
    exif[0x8825] = {1: "N", 2: (45.0, 19.0, 0.0)}       # GPS latitude
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif)
    return out.getvalue()


def test_files_are_checked_by_content_and_cleaned():
    photo = jpeg_with_location()
    assert b"PhoneMaker" in photo and files.sniff(photo) == "jpeg"
    cleaned, mime = run(files.clean_upload(photo))
    from PIL import Image
    img = Image.open(io.BytesIO(cleaned))
    assert mime == "image/jpeg" and max(img.size) == 3000 and b"PhoneMaker" not in cleaned
    assert not img.getexif()
    pdf = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF"
    assert run(files.clean_upload(pdf)) == (pdf, "application/pdf")   # byte for byte
    with pytest.raises(files.FileRefused, match="HEIC"):
        files.check_type(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20)
    with pytest.raises(files.FileRefused, match="Only photos"):
        files.check_type(b"<html><script>alert(1)</script>")
    with pytest.raises(files.FileRefused, match="too big"):
        files.check_type(b"%PDF-" + b"x" * 100, limit=50)
    with pytest.raises(files.FileRefused):
        files.clean_image(b"\xff\xd8\xff" + b"garbage" * 10)
    resp = files.file_response(pdf, "application/pdf", "../../etc/passwd.html")
    assert resp.headers["Content-Disposition"] == 'attachment; filename="etcpasswd.pdf"'
    assert resp.headers["Content-Security-Policy"].startswith("sandbox")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    resp = files.file_response(cleaned, "image/jpeg", "card")
    assert resp.headers["Content-Disposition"].startswith("inline")
