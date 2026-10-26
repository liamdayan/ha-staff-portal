"""Door activity: TT Hotel's lock records pushed to the callback address (its secret, limits and
strict reading), storing them, matching card numbers to cards made here, each card's "Last used",
alerts when a lost or revoked card is tried, pruning, and the address in backups and migrations.

Nothing here talks to a real TT Hotel or Home Assistant: deliveries are posted by hand as TT Hotel
would send them, and Home Assistant's call_service is replaced by a list of what was asked.
"""

import asyncio
import json
import time
import urllib.parse
from zoneinfo import ZoneInfo

from test_wristbands_server import Portal, noon_zone, run

import db as db_module  # noqa: E402
import doorlog  # noqa: E402
import main  # noqa: E402
import migrate  # noqa: E402
import transfer  # noqa: E402

POOL, SPA, ROOM = 900, 901, 101          # Pool Gate (battery 60), Spa Door, Room 101
PUBLIC = "https://staff.example.com"


def record(kind=49, number="12345678", success=1, at=None, **extra) -> dict:
    """One record as TT Hotel writes it (times in milliseconds)."""
    at = time.time() if at is None else at
    return {"recordType": kind, "success": success, "keyboardPwd": number, "username": "",
            "lockDate": int(at * 1000), "serverDate": int(at * 1000) + 2000, "electricQuantity": 42,
            **extra}


def form(lock_id, records, notify=1) -> bytes:
    return urllib.parse.urlencode({"notifyType": notify, "lockId": lock_id,
                                   "lockMac": "C5:40:E0:9C:8C:90",
                                   "records": json.dumps(records)}).encode()


async def deliver(p: Portal, lock_id, records, token=None, **headers):
    token = token or main.callback_token()
    resp = await p.staff.post(f"/tthotel/records/{token}", data=form(lock_id, records),
                              headers={"Content-Type": "application/x-www-form-urlencoded",
                                       **headers})
    return resp.status, await resp.text()


def card(p: Portal, number: str, name="Eve", card_type="staff") -> int:
    """A card made here (as the encoder leaves it): its wb_cards ID."""
    job_id = p.db.add_wb_job(card_type=card_type, name=name, count=1, records=[], summary="Pool",
                             expires_at=time.time() + 86400, allow_lockout=False, staff_id=p.bob,
                             device_id=p.device, actor="Bob", role=None, ip=None,
                             encoder_url="ws://127.0.0.1:1")
    p.db.update_wb_card(job_id, 1, status="written", card_number=number,
                        written_at=time.time() - 3600)
    p.db.update_wb_job(job_id, status="done", finished_at=time.time())
    return p.db.wb_job(job_id)["cards"][0]["id"]


def stored(p: Portal) -> list[dict]:
    return [dict(r) for r in p.db.conn.execute("SELECT * FROM wb_lock_records ORDER BY id")]


def fresh_state(monkeypatch) -> list:
    """Clear what the callback keeps in memory, and catch Home Assistant notifications."""
    main.callback_requests.clear()
    main.callback_deliveries.clear()
    main.callback_records.clear()
    main.card_alerts.clear()
    main.recent_alerts.clear()
    main.door_delivery.update(at=None, kept=0, dropped=0, lock_id=None, known=True)
    sent = []

    async def call_service(domain, service, data, refresh_token=None):
        sent.append((domain, service, data))
    monkeypatch.setattr(main.ha, "call_service", call_service)
    return sent


async def settle():
    """Let background tasks (alerts and their Home Assistant notification) run."""
    for _ in range(10):
        await asyncio.sleep(0)


# ---------------------------------------------------------------- reading what TT Hotel sends

def test_reading_a_delivery_strictly():
    now = time.time()
    records = [record(49, "0012345678"), record(4, "123456", username="Pool code"),
               record(1, "", username="manager@example.com"),
               record(30), record(49, success=True), record(49, success=2),
               record(49, lockDate=1625025703000.0, serverDate=None),
               record(7, number=1234567), "junk", {"recordType": "49"},
               record(49, number="9" * 41)]
    lock_id, kept, dropped = doorlog.read_delivery(form(POOL, records), "", now)
    assert lock_id == POOL and dropped == 6
    # Card records keep the card number; a passcode record never keeps the passcode.
    assert [(r["record_type"], r["card_number"], r["username"]) for r in kept] == [
        (49, "0012345678", ""), (4, "", "Pool code"), (1, "", "manager@example.com"),
        (7, "1234567", ""), (49, "", "")]
    assert kept[0]["battery"] == 42 and kept[0]["success"] == 1
    assert abs(kept[0]["at"] - now) < 1 and kept[0]["server_date"] - kept[0]["lock_date"] == 2
    # A lock whose clock is clearly off is listed at TT Hotel's time instead.
    off = doorlog.read_record(record(49, lockDate=int((now - 90 * 86400) * 1000)), now)
    assert off["at"] == off["server_date"] and off["lock_date"] < off["at"]
    only_lock = doorlog.read_record(record(49, serverDate=None), now)
    assert only_lock["at"] == only_lock["lock_date"]
    assert doorlog.read_record(record(49, lockDate=None, serverDate=None), now) is None
    # A deleted card is always refused; other notices, bad forms and bad JSON are dropped.
    assert doorlog.read_record(record(51, success=1), now)["success"] == 0
    assert doorlog.read_delivery(form(POOL, records, notify=2), "", now) == (None, [], 0)
    assert doorlog.read_delivery(b"lockId=x&notifyType=1", "", now) == (None, [], 0)
    assert doorlog.read_delivery(b"lockId=5&notifyType=1&records=[", "", now) == (5, [], 0)
    assert doorlog.read_delivery(b"\xff\xfe", "", now) == (None, [], 0)
    # At most MAX_RECORDS from one delivery; JSON bodies are read the same way.
    many = [record(49, str(10_000 + i)) for i in range(doorlog.MAX_RECORDS + 5)]
    body = json.dumps({"notifyType": 1, "lockId": POOL, "records": json.dumps(many)}).encode()
    lock_id, kept, dropped = doorlog.read_delivery(body, "application/json", now)
    assert len(kept) == doorlog.MAX_RECORDS and dropped == 5


def test_card_numbers_and_labels():
    # Leading zeros don't count; TT Hotel's own example of a number with its bytes reversed.
    assert doorlog.card_keys("0012345") == ["12345", str(int.from_bytes(
        (12345).to_bytes(4, "big"), "little"))]
    assert doorlog.card_keys("3661413478") == ["3661413478", "1723612378"]
    assert doorlog.card_keys("1723612378") == ["1723612378", "3661413478"]
    for nothing in ("", "000", "12a4", "١٢٣"):
        assert doorlog.card_keys(nothing) == []
    zone = ZoneInfo("America/Denver")
    now = 1761400000.0   # Saturday, Oct 25 2025, 7:46 AM in Denver
    assert doorlog.used_label("Pool Gate", now - 60, True, now, zone) == "Pool Gate, today 7:45 AM"
    assert doorlog.used_label("Spa", now - 86400, False, now, zone) == (
        "Spa, yesterday 7:46 AM, refused")
    assert doorlog.used_label("Spa", now - 5 * 86400, True, now, zone) == "Spa, Mon, Oct 20 7:46 AM"
    assert doorlog.used_label("Spa", now, True, now, None) == ""
    assert doorlog.callback_address(PUBLIC + "/", "abc") == f"{PUBLIC}/tthotel/records/abc"
    assert doorlog.callback_address("", "abc") is None


# ---------------------------------------------------------------- the address and its limits

def test_the_address_its_secret_and_its_limits(tmp_path, monkeypatch):
    fresh_state(monkeypatch)

    async def scenario():
        p = Portal(tmp_path)
        async with p:
            # Nothing gets in before an address is made.
            assert p.db.settings()["tthotel_callback_token"] == ""
            resp = await p.staff.post("/tthotel/records/" + "a" * 43, data=form(POOL, [record()]))
            assert resp.status == 404
            # The dashboard makes it; without the staff app's address there's nothing to paste.
            status, data = await p.admin_call("GET", "/api/door-activity")
            assert status == 200 and data["address"] is None and data["records"] == []
            assert data["alert_ha"] is True and data["last_record"] is None
            token = p.db.settings()["tthotel_callback_token"]
            assert doorlog.TOKEN_RE.match(token)
            p.db.update_settings({"public_url": PUBLIC})
            status, data = await p.admin_call("GET", "/api/door-activity")
            assert data["address"] == f"{PUBLIC}/tthotel/records/{token}"
            assert token not in json.dumps((await p.admin_call("GET", "/api/state"))[1])

            # The wrong secret is "not found" (no device sign-in or dashboard headers needed for
            # the right one), and nothing is stored either way but records for known doors.
            assert (await deliver(p, POOL, [record()], token=token[:-1] + "x"))[0] == 404
            assert (await deliver(p, POOL, [record()], token=token + "x"))[0] == 404
            assert stored(p) == []
            assert await deliver(p, POOL, [record()]) == (200, "success")
            assert len(stored(p)) == 1
            # A door TT Hotel hasn't listed here: answered, but dropped (and said so).
            assert await deliver(p, 4242, [record()]) == (200, "success")
            assert len(stored(p)) == 1
            _, data = await p.admin_call("GET", "/api/door-activity")
            assert data["delivery"]["known"] is False and data["delivery"]["lock_id"] == 4242
            assert data["delivery"]["dropped"] == 1
            # Nonsense with the right address is still "success", so TT Hotel doesn't resend it.
            resp = await p.staff.post(f"/tthotel/records/{token}", data=b"\x00garbage")
            assert (resp.status, await resp.text()) == (200, "success")
            # More than the staff app reads at once (64 KB): answered, dropped.
            huge = [record(49, str(10**9 + i), username="x" * 90) for i in range(400)]
            assert len(form(POOL, huge)) > 64 * 1024
            assert await deliver(p, POOL, huge) == (200, "success")
            assert len(stored(p)) == 1
            # Wrong addresses: so many a minute from one caller, and they never use up what the
            # right address may send (TT Hotel isn't refused because someone guesses).
            monkeypatch.setattr(main, "WRONG_ADDRESS_PER_MINUTE", 3)
            monkeypatch.setattr(main, "CALLBACK_PER_MINUTE", 3)
            main.callback_deliveries.clear()
            guesser = {"X-Forwarded-For": "203.0.113.9"}
            codes = [(await deliver(p, POOL, [], token="g" * 43, **guesser))[0]
                     for _ in range(5)]
            assert codes == [404, 404, 404, 429, 429]
            assert (await deliver(p, POOL, [], **guesser))[0] == 200
            # The right address may send so many a minute, from wherever it comes.
            codes = [(await deliver(p, POOL, [], **{"X-Forwarded-For": f"198.51.100.{i}"}))[0]
                     for i in range(3)]
            assert codes == [200, 200, 429]
            # ...and so many records are stored a minute: a delivery past that is refused whole
            # (TT Hotel sends it again later), never cut short or dropped.
            main.callback_deliveries.clear()
            monkeypatch.setattr(main, "CALLBACK_PER_MINUTE", 100)
            monkeypatch.setattr(main, "RECORDS_PER_MINUTE", 5)
            now = time.time()
            assert await deliver(p, POOL, [record(49, str(70 + i), at=now - i) for i in range(3)]
                                 ) == (200, "success")
            assert len(stored(p)) == 4
            later = [record(49, str(80 + i), at=now - i) for i in range(3)]
            assert (await deliver(p, POOL, later))[0] == 429
            assert len(stored(p)) == 4
            main.callback_records.clear()   # a minute on
            assert await deliver(p, POOL, later) == (200, "success")
            assert len(stored(p)) == 7
            # None of it reached TT Hotel.
            assert main.tthotel._api_sent == 0
    run(scenario())


def test_a_new_address_replaces_the_old_one(tmp_path, monkeypatch):
    fresh_state(monkeypatch)

    async def scenario():
        p = Portal(tmp_path)
        p.db.update_settings({"public_url": PUBLIC})
        async with p:
            old = main.callback_token()
            assert (await deliver(p, POOL, [record()], token=old))[0] == 200
            status, data = await p.admin_call("POST", "/api/door-activity/address")
            assert status == 200
            new = p.db.settings()["tthotel_callback_token"]
            assert new != old and data["address"] == f"{PUBLIC}/tthotel/records/{new}"
            assert (await deliver(p, POOL, [record()], token=old))[0] == 404
            assert (await deliver(p, POOL, [record(at=time.time() - 5)], token=new))[0] == 200
            assert len(stored(p)) == 2
            assert ("Admin", "Made a new Door activity address", True) in p.activity()
            # Changes need the dashboard's own header, like every other change.
            resp = await p.admin.post("/api/door-activity/address")
            assert resp.status == 403 and p.db.settings()["tthotel_callback_token"] == new
    run(scenario())


# ---------------------------------------------------------------- storing and matching

def test_records_are_stored_matched_and_shown_as_last_used(tmp_path, monkeypatch):
    fresh_state(monkeypatch)

    async def scenario():
        p = Portal(tmp_path)
        eve = card(p, "0012345678")                   # leading zeros on the card made here
        flipped = card(p, "3661413478", name="Fay")   # TT Hotel shows it the other way round
        wristband = card(p, "777000", name="Smith party", card_type="day_pass")
        async with p:
            now = time.time()
            first = [record(49, "12345678", at=now - 120),
                     record(4, "246810", at=now - 60, username="Pool code"),
                     record(49, "1723612378", success=0, at=now - 30),
                     record(49, "99999", at=now - 20, username="Someone else's card"),
                     record(9, "777000", at=now - 10)]
            assert await deliver(p, POOL, first) == (200, "success")
            rows = stored(p)
            assert [r["card_id"] for r in rows] == [eve, None, flipped, None, wristband]
            assert rows[1]["card_number"] == ""     # the passcode itself is never kept
            assert p.db.wb_door(POOL)["battery"] == 42
            # TT Hotel sending the same records again adds nothing.
            assert await deliver(p, POOL, first) == (200, "success")
            assert len(stored(p)) == 5
            # An older delivery arriving late doesn't take the battery back.
            await deliver(p, POOL, [record(49, "5", at=now - 3600, electricQuantity=90)])
            assert p.db.wb_door(POOL)["battery"] == 42
            await deliver(p, SPA, [record(49, "12345678", at=now - 5)])

            # The Door activity list: newest first, who and how, filters and pages.
            _, data = await p.admin_call("GET", "/api/door-activity")
            listed = data["records"]
            assert [(r["door"], r["who"], r["how"], r["ok"]) for r in listed[:5]] == [
                ("Spa", "Eve", "Card", True), ("Pool Gate", "Smith party", "Wristband", True),
                ("Pool Gate", "Someone else's card", "Card", True),
                ("Pool Gate", "Fay", "Card", False), ("Pool Gate", "Pool code", "Passcode", True)]
            assert listed[0]["card_number"] == "0012345678" and listed[2]["card_number"] == "99999"
            assert data["last_record"] and data["more"] is False
            _, data = await p.admin_call("GET", f"/api/door-activity?door={SPA}")
            assert [r["door"] for r in data["records"]] == ["Spa"]
            _, data = await p.admin_call("GET", "/api/door-activity?problems=1")
            assert [r["who"] for r in data["records"]] == ["Fay"]
            monkeypatch.setattr(main, "DOOR_ACTIVITY_PAGE", 4)
            _, page = await p.admin_call("GET", "/api/door-activity")
            assert page["more"] is True
            _, rest = await p.admin_call("GET", f"/api/door-activity?before={page['records'][-1]['id']}")
            assert [r["id"] for r in page["records"] + rest["records"]] == [r["id"] for r in listed]
            assert (await p.admin_call("GET", "/api/door-activity?door=pool"))[0] == 400

            # Each card's last use: in the card log, a look-up's record (job_view) and Key cards.
            bob = await p.sign_in(p.bob)
            status, log = await p.call("GET", "/api/wristbands/log", bob)
            used = {c["id"]: c["last_used"] for job in log["jobs"] for c in job["cards"]}
            assert used[eve]["door"] == "Spa" and used[eve]["ok"] is True
            assert used[eve]["label"].startswith("Spa, today ")
            assert used[flipped]["label"].endswith(", refused")
            assert used[wristband]["door"] == "Pool Gate"
            zone = ZoneInfo(noon_zone())
            assert main.job_view(p.db.wb_card_job("0012345678"), zone)["cards"][0][
                "last_used"]["door"] == "Spa"
            status, cards = await p.admin_call("GET", "/api/keycards?state=all")
            by_id = {c["id"]: c["last_used"] for c in cards["cards"]}
            assert by_id[eve]["door"] == "Spa" and by_id[flipped]["ok"] is False
            status, one = await p.admin_call("GET", f"/api/keycards/{eve}")
            assert one["card"]["last_used"]["door"] == "Spa"
            # A card number written again (the card was cleared and made anew) is the new card
            # from then on; earlier records stay with the old one.
            again = card(p, "12345678", name="Gil")
            p.db.conn.execute("UPDATE wb_cards SET written_at = ? WHERE id = ?", (now - 1, again))
            p.db.conn.commit()
            await deliver(p, ROOM, [record(49, "12345678", at=now)])
            assert stored(p)[-1]["card_id"] == again
            assert p.db.match_card("12345678", now - 1000)["id"] == eve
    run(scenario())


# ---------------------------------------------------------------- alerts

def test_lost_and_revoked_cards_alert_once_per_card_per_ten_minutes(tmp_path, monkeypatch):
    sent = fresh_state(monkeypatch)

    def alerts(p):
        return [(a["actor"], a["action"], bool(a["ok"]), a["detail"]) for a in p.db.activity(500)
                if a["actor"] == "Door activity"][::-1]

    async def scenario():
        p = Portal(tmp_path)
        lost = card(p, "555000111")
        revoked = card(p, "555000222", name="Hal")
        p.db.set_card_state(lost, "lost", "Admin")
        p.db.set_card_state(revoked, "revoked", "Admin")
        # Reported an hour ago.
        p.db.conn.execute("UPDATE wb_cards SET state_at = ? WHERE id IN (?, ?)",
                          (time.time() - 3600, lost, revoked))
        p.db.conn.commit()
        async with p:
            now = time.time()
            # A tap from before the card was reported lost, arriving late, isn't an alert.
            await deliver(p, POOL, [record(49, "555000111", at=now - 7200)])
            await settle()
            assert alerts(p) == [] and sent == []
            assert stored(p)[-1]["alert"] is None and stored(p)[-1]["problem"] == 0

            await deliver(p, POOL, [record(49, "555000111", success=0, at=now - 10)])
            await settle()
            (actor, action, ok, detail), = alerts(p)
            assert (action, ok) == ("Lost card tried at Pool Gate", False)
            assert detail.startswith("Card 555000111 (Eve) · refused · today ")
            (domain, service, data), = sent
            assert (domain, service) == ("persistent_notification", "create")
            assert data["notification_id"] == f"staff_access_card_{lost}"
            assert data["title"] == "Lost card tried at Pool Gate"
            assert "Card 555000111 (Eve) was reported lost" in data["message"]
            assert stored(p)[-1]["alert"] == "lost" and stored(p)[-1]["problem"] == 1

            # Tried again within 10 minutes: stored and listed, but no new alert.
            await deliver(p, SPA, [record(49, "555000111", success=0, at=now - 5)])
            await settle()
            assert len(alerts(p)) == 1 and len(sent) == 1
            assert stored(p)[-1]["alert"] == "lost"
            # Later on, it alerts again (the same notification, replaced).
            main.card_alerts[f"card {lost}"] -= main.CARD_ALERT_GAP
            await deliver(p, SPA, [record(49, "555000111", success=1, at=now - 4)])
            await settle()
            assert alerts(p)[-1][1] == "Lost card opened Spa"
            assert len(sent) == 2 and sent[-1][2]["notification_id"] == f"staff_access_card_{lost}"

            # Revoked cards, and TT Hotel's own "deleted card" records, alert too.
            await deliver(p, POOL, [record(49, "555000222", success=0, at=now - 3),
                                    record(51, "8080", success=1, at=now - 2, username="Old")])
            await settle()
            assert [a[1] for a in alerts(p)[-2:]] == ["Revoked card tried at Pool Gate",
                                                      "Deleted card tried at Pool Gate"]
            assert sent[-1][2]["notification_id"] == "staff_access_card_8080"
            # TT Hotel's own name for it is in the activity log, not in Home Assistant.
            assert "Card 8080 was deleted in TT Hotel" in sent[-1][2]["message"]
            assert "Old" not in sent[-1][2]["message"]
            assert alerts(p)[-1][3].startswith("Card 8080 (Old) · refused")
            _, data = await p.admin_call("GET", "/api/door-activity?problems=1")
            assert [r["alert"] for r in data["records"]] == ["deleted", "revoked", "lost", "lost",
                                                             "lost"]
            assert data["records"][0]["ok"] is False   # a deleted card never opens a door

            # With the setting off, only the activity log hears of it.
            status, _ = await p.admin_call("PUT", "/api/settings", {"wb_lost_card_alert": "0"})
            assert status == 200 and p.db.settings()["wb_lost_card_alert"] == "0"
            main.card_alerts.clear()
            await deliver(p, POOL, [record(49, "555000222", success=0, at=now - 1)])
            await settle()
            assert alerts(p)[-1][1] == "Revoked card tried at Pool Gate" and len(sent) == 4
            # An active card is just a card being used.
            active = card(p, "123123")
            await deliver(p, POOL, [record(49, "123123", at=now)])
            await settle()
            assert stored(p)[-1]["card_id"] == active and stored(p)[-1]["alert"] is None
            assert len(alerts(p)) == 5 and len(sent) == 4
    run(scenario())


def test_a_cleared_card_given_out_again_is_someone_else(tmp_path, monkeypatch):
    """A card cleared here (revoked on an encoder, found blank, written over) goes back in the
    stack and may be given out again in TT Hotel's own program: records from after that aren't
    the old card, so they neither alert nor count as its use. A replaced card that was never
    handed back (revoked, not cleared) and a lost card still alert."""
    sent = fresh_state(monkeypatch)

    async def scenario():
        p = Portal(tmp_path)
        hal = card(p, "3661413478", name="Hal Former")
        replaced = card(p, "555000333", name="Ida")
        lost = card(p, "555000444", name="Joe")
        hour_ago = time.time() - 3600
        p.db.set_card_state(hal, "revoked", "Admin", cleared=True)
        p.db.set_card_state(replaced, "revoked", "Replaced by card 1")
        p.db.set_card_state(lost, "lost", "Admin")
        p.db.conn.execute("UPDATE wb_cards SET state_at = ?, cleared_at = CASE WHEN id = ? "
                          "THEN ? ELSE cleared_at END WHERE id IN (?, ?, ?)",
                          (hour_ago, hal, hour_ago, hal, replaced, lost))
        p.db.conn.execute("UPDATE wb_cards SET written_at = ? WHERE id = ?",
                          (hour_ago - 3600, hal))   # made an hour before it was revoked
        p.db.conn.commit()
        async with p:
            now = time.time()
            # A guest's card with the cleared card's number (in either byte order): nobody's.
            await deliver(p, POOL, [record(49, "3661413478", at=now - 60),
                                    record(49, "1723612378", at=now - 50)])
            await settle()
            assert [(r["card_id"], r["alert"], r["problem"]) for r in stored(p)] == [
                (None, None, 0), (None, None, 0)]
            assert sent == []
            # A tap from before it was cleared, arriving late, is still Hal's (and no alert);
            # one a moment after it (the lock's clock running ahead) too, but no alert either.
            await deliver(p, POOL, [record(49, "3661413478", at=hour_ago - 600),
                                    record(49, "3661413478", at=hour_ago + 60)])
            await settle()
            assert [(r["card_id"], r["alert"]) for r in stored(p)[-2:]] == [(hal, None), (hal, None)]
            assert sent == []
            _, data = await p.admin_call("GET", "/api/door-activity")
            assert [r["who"] for r in data["records"]][:2] == ["", ""]   # the guest's taps
            # Replaced (never cleared) and lost cards still alert.
            await deliver(p, SPA, [record(49, "555000333", at=now - 5),
                                   record(49, "555000444", success=0, at=now - 4)])
            await settle()
            assert [(r["card_id"], r["alert"]) for r in stored(p)[-2:]] == [
                (replaced, "revoked"), (lost, "lost")]
            assert len(sent) == 2
            # The lost card found and cleared on an encoder: it stays lost, but from then on its
            # number is someone else's. A tap between lost and cleared still alerted.
            p.db.mark_card_cleared(lost)
            assert p.db.key_card(lost)["state"] == "lost"
            await deliver(p, SPA, [record(49, "555000444", at=time.time() + 400)])
            await settle()
            assert stored(p)[-1]["card_id"] is None and stored(p)[-1]["alert"] is None
            assert p.db.match_card("555000444", now - 4)["id"] == lost
            # Written again here: the new card from then on (written over = cleared).
            again = card(p, "3661413478", name="Kim")
            p.db.retire_card_number("3661413478", again, "Bob")
            p.db.conn.execute("UPDATE wb_cards SET written_at = ? WHERE id = ?", (now, again))
            p.db.conn.commit()
            assert p.db.match_card("3661413478", now + 30)["id"] == again
            assert p.db.match_card("3661413478", hour_ago - 600)["id"] == hal
    run(scenario())


def test_revoking_on_an_encoder_marks_the_card_cleared(tmp_path, monkeypatch):
    fresh_state(monkeypatch)
    p = Portal(tmp_path)
    active, lost = card(p, "1000001"), card(p, "1000002")
    p.db.set_card_state(lost, "lost", "Admin")
    for target in (active, lost):
        job_id = p.db.add_wb_job(card_type="staff", name="Eve", count=1, records=[],
                                 summary="Revoke", expires_at=time.time() + 86400,
                                 allow_lockout=False, staff_id=p.bob, device_id=p.device,
                                 actor="Bob", role=None, ip=None, encoder_url="ws://127.0.0.1:1",
                                 kind="revoke", target_card_id=target,
                                 target_card=p.db.wb_card(target)["card_number"])
        p.db.update_wb_card(job_id, 1, status="written", written_at=time.time(),
                            card_number=p.db.wb_card(target)["card_number"])
        main.card_done(job_id, 1)
    rows = {r["id"]: dict(r) for r in p.db.conn.execute(
        "SELECT id, state, cleared_at FROM wb_cards WHERE id IN (?, ?)", (active, lost))}
    assert rows[active]["state"] == "revoked" and rows[active]["cleared_at"]
    assert rows[lost]["state"] == "lost" and rows[lost]["cleared_at"]


def test_home_assistant_never_gets_text_that_makes_links(tmp_path, monkeypatch):
    """Whoever has the address can send any text: what reaches Home Assistant's Markdown is a
    card number (digits only) and the names of cards made here, every punctuation mark
    escaped."""
    sent = fresh_state(monkeypatch)

    async def scenario():
        p = Portal(tmp_path)
        named = card(p, "424242", name="[Reset](https://evil.example) <b>*x*</b>")
        p.db.set_card_state(named, "lost", "Admin")
        p.db.conn.execute("UPDATE wb_cards SET state_at = ? WHERE id = ?",
                          (time.time() - 60, named))
        p.db.conn.commit()
        async with p:
            now = time.time()
            link = "[Re-authorize Staff Access](https://evil.example/login) www.evil.example"
            await deliver(p, POOL, [record(51, "99", at=now - 3, username=link),
                                    record(51, "[x](https://evil.example)", at=now - 2),
                                    record(49, "424242", at=now - 1)])
            await settle()
            messages = [data["message"] for _, _, data in sent]
            assert len(messages) == 3
            assert messages[0].startswith("Card 99 was deleted in TT Hotel. It was tried at Pool "
                                          "Gate")
            assert messages[1].startswith("A card was deleted in TT Hotel.")
            assert messages[2].startswith(
                r"Card 424242 (\[Reset\]\(https\:\/\/evil\.example\) \<b\>\*x\*\<\/b\>) "
                "was reported lost")
            for message in messages:
                assert "evil.example/login" not in message and "](" not in message
                assert "www.evil" not in message and "<b>" not in message
            # The activity log and Door activity keep TT Hotel's text, shown as plain text.
            _, data = await p.admin_call("GET", "/api/door-activity")
            assert data["records"][-1]["who"] == link
    run(scenario())


def test_records_from_the_future_are_dropped():
    now = time.time()
    assert doorlog.read_record(record(49, at=now + 2 * 3600), now) is None
    ahead = doorlog.read_record(record(49, at=now + 600), now)
    assert ahead is not None and abs(ahead["at"] - (now + 600)) < 1
    # A lock clock far ahead with TT Hotel's own time fine: listed at TT Hotel's time.
    skewed = doorlog.read_record(record(49, lockDate=int((now + 30 * 86400) * 1000),
                                        serverDate=int(now * 1000)), now)
    assert skewed["at"] == skewed["server_date"]


def test_door_list_is_wanted_for_the_dashboards_encoder_and_door_activity(tmp_path, monkeypatch):
    fresh_state(monkeypatch)
    p = Portal(tmp_path, encoder_url=None)
    p.db.conn.execute("UPDATE devices SET encoder_url = NULL, encoder_local = 0, encoder_auto = 0")
    p.db.conn.commit()
    p.db.update_settings({"wristbands_enabled": "1"})
    assert not main.tthotel_wanted()
    # A key card made on This computer from the dashboard (valid, or ended lately).
    job_id = p.db.add_wb_job(card_type="staff", name="Eve", count=1, records=[], summary="Pool",
                             expires_at=time.time() - 10 * 86400, allow_lockout=False,
                             staff_id=None, device_id=None, actor="Admin", role=None, ip=None,
                             encoder_url="local:dashboard-" + "a1" * 16)
    assert main.tthotel_wanted()
    p.db.conn.execute("UPDATE wb_jobs SET expires_at = ? WHERE id = ?",
                      (time.time() - 200 * 86400, job_id))
    p.db.conn.commit()
    assert not main.tthotel_wanted()
    # TT Hotel sending door activity (records are only kept for doors on the list).
    main.door_delivery["at"] = time.time()
    assert main.tthotel_wanted()
    p.db.update_settings({"wristbands_enabled": "0"})
    assert not main.tthotel_wanted()


def test_alerts_in_all_are_capped(tmp_path, monkeypatch):
    sent = fresh_state(monkeypatch)
    monkeypatch.setattr(main, "ALERTS_PER_MINUTE", 3)

    async def scenario():
        p = Portal(tmp_path)
        async with p:
            now = time.time()
            # Many deleted cards at once: all listed, but only so many alerts a minute.
            await deliver(p, POOL, [record(51, str(9000 + i), at=now - i) for i in range(6)])
            await settle()
            assert len([r for r in stored(p) if r["alert"] == "deleted"]) == 6
            logged = [a for a in p.db.activity(500) if a["actor"] == "Door activity"]
            assert len(logged) == 3 and len(sent) == 3
            # A minute on, alerts go out again.
            main.recent_alerts.clear()
            await deliver(p, POOL, [record(51, "9100", at=now)])
            await settle()
            assert len(sent) == 4
    run(scenario())


# ---------------------------------------------------------------- limits, backups, migration

def test_door_activity_is_pruned_like_the_other_logs(tmp_path, monkeypatch):
    fresh_state(monkeypatch)
    p = Portal(tmp_path)
    now = time.time()
    old = doorlog.read_record(record(49, "1", at=now - 200 * 86400), now - 200 * 86400)
    p.db.add_lock_records(POOL, [old], now - 200 * 86400)
    assert len(stored(p)) == 1
    p.db.add_lock_records(POOL, [doorlog.read_record(record(49, "2", at=now), now)], now)
    assert [r["card_number"] for r in stored(p)] == ["2"]     # older than 180 days: gone
    monkeypatch.setattr(db_module, "LOCK_RECORDS_MAX_ROWS", 5)
    for i in range(8):
        p.db.add_lock_records(POOL, [doorlog.read_record(record(49, str(10 + i), at=now + i),
                                                         now)], now)
    assert [r["card_number"] for r in stored(p)] == [str(10 + i) for i in range(3, 8)]


def test_backups_never_carry_the_address_but_migrations_do(tmp_path, monkeypatch):
    fresh_state(monkeypatch)

    async def scenario():
        p = Portal(tmp_path)
        async with p:
            token = main.callback_token()
            p.db.update_settings({"wb_lost_card_alert": "0"})
            status, backup = await p.admin_call("GET", "/api/backup")
            assert status == 200 and token not in json.dumps(backup)
            assert "tthotel_callback_token" not in backup["settings"]
            assert backup["settings"]["wb_lost_card_alert"] == "0"   # an ordinary setting
            # A file can't set it either: restoring one that has it changes nothing.
            backup["settings"]["tthotel_callback_token"] = "b" * 43
            backup["settings"]["wb_lost_card_alert"] = "1"
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, reply
            assert p.db.settings()["tthotel_callback_token"] == token
            assert p.db.settings()["wb_lost_card_alert"] == "1"
            # A migration file carries it (encrypted), so TT Hotel's Callback URL keeps working.
            snapshot = migrate.make_snapshot(p.db, main.branding, main.VERSION)
            assert snapshot["migrate"]["secrets"]["tthotel_callback_token"] == token
            unpacked = transfer.read_backup_file(transfer.pack_backup(snapshot))
            clean = migrate.clean_snapshot(unpacked, main.clean_settings, p.db.settings(),
                                           main.branding.clean_logo)
            assert clean["migrate"]["secrets"]["tthotel_callback_token"] == token
            for bad in ("short", "x" * 43 + "/", 5, None):
                unpacked["migrate"]["secrets"]["tthotel_callback_token"] = bad
                clean = migrate.clean_snapshot(unpacked, main.clean_settings, p.db.settings(),
                                               main.branding.clean_logo)
                assert "tthotel_callback_token" not in clean["migrate"]["secrets"]
    run(scenario())
