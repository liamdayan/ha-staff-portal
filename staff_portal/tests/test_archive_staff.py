"""Archiving people (Staff tab): their access ends everywhere at once and nothing of theirs is
deleted. Every way in refuses them, the Connecteam sync never brings them back, Restore puts them
back as they were, only archived people can be deleted, a still-valid key card of theirs tried at
a door alerts, and backups, exports and migration files carry it. Also the Key cards section's
totals.

Nothing here talks to a real TT Hotel, Connecteam or Home Assistant (see test_wristbands_server,
test_staffcards_server and test_door_activity for the fakes used).
"""

import json
import time

from test_door_activity import POOL, deliver, fresh_state, record, settle, stored
from test_signin_and_lookup_server import phone
from test_staffcards_server import FakeConnecteam, by_name, connecteam_on, ct_user, department, sync
from test_wristbands_server import Portal, run, staff_card

import db as db_module  # noqa: E402
import doorlog  # noqa: E402
import main  # noqa: E402
import migrate  # noqa: E402
import transfer  # noqa: E402
from db import check_pin  # noqa: E402
from permissions import ActionRejected  # noqa: E402

FLOOR_ONE = [{"level": "floor", "building": 1, "floor": 1}]


async def archive(p: Portal, staff_id: int) -> dict:
    status, reply = await p.admin_call("POST", f"/api/staff/{staff_id}/archive")
    assert status == 200, reply
    return reply


async def restore(p: Portal, staff_id: int) -> dict:
    status, reply = await p.admin_call("POST", f"/api/staff/{staff_id}/restore")
    assert status == 200, reply
    return reply


async def unlock(p: Portal, headers: dict, **body) -> tuple[int, dict]:
    resp = await p.staff.post("/api/unlock", json=body, headers=headers)
    return resp.status, await resp.json()


def key_card(p: Portal, holder: int, number: str, days: float = 1, name: str | None = None) -> int:
    """A staff key card made for someone (as the encoder leaves it): its wb_cards ID."""
    job_id = p.db.add_wb_job(card_type="staff", name=name or p.db.staff(holder)["name"], count=1,
                             records=[{"level": "door", "door": POOL}], summary="Pool Gate",
                             expires_at=time.time() + days * 86400, allow_lockout=False,
                             staff_id=None, device_id=p.device, actor="Admin", role=None, ip=None,
                             encoder_url="ws://127.0.0.1:1", holder_staff_id=holder,
                             source="dashboard")
    p.db.update_wb_card(job_id, 1, status="written", card_number=number,
                        written_at=time.time() - 7200)
    p.db.update_wb_job(job_id, status="done", finished_at=time.time())
    return p.db.wb_job(job_id)["cards"][0]["id"]


# ---------------------------------------------------------------- no access anywhere

def test_archived_people_have_no_access_anywhere(tmp_path, monkeypatch):
    sent = []

    async def call_service(domain, service, data, refresh_token=None):
        sent.append((domain, service, refresh_token))
    monkeypatch.setattr(main.ha, "call_service", call_service)

    async def scenario():
        p = Portal(tmp_path)
        shared_id, shared = phone(p, "Pool phone", mode="staff")
        p.db.set_device_staff(shared_id, [p.ana, p.bob])
        p.db.update_device(shared_id, staff_all=0)
        p.db.update_staff(p.ana, ha_user="ana", ha_refresh_token="ana-refresh")
        async with p:
            desk = await p.sign_in(p.ana)                                  # role-based computer
            pool = await p.sign_in(p.ana, shared["Authorization"][7:])     # shared kiosk phone
            bob = await p.sign_in(p.bob)
            ana = p.db.staff(p.ana)
            assert await main.send_command(p.db.device(p.device), ana, "light", "turn_on",
                                           {"entity_id": "light.porch"})
            assert sent == [("light", "turn_on", "ana-refresh")]
            assert {m["id"] for m in p.db.staff_for_device(p.db.device(shared_id))} == {p.ana, p.bob}
            assert next(r for r in p.db.roles() if r["id"] == p.front_desk)["staff_count"] == 2

            running = p.db.add_wb_job(card_type="day_pass", name="Smith", count=5, records=[],
                                      summary="Pool", expires_at=time.time() + 3600,
                                      allow_lockout=False, staff_id=p.ana, device_id=p.device,
                                      actor="Ana", role=None, ip=None, encoder_url="ws://x:1")
            reply = await archive(p, p.ana)
            assert reply["cards"] == []
            assert p.db.wb_job(running)["status"] == "cancelled"   # no more cards from her job
            ana = p.db.staff(p.ana)
            assert (ana["status"], ana["archived_by"], ana["archived_from"]) == (
                "archived", "Admin", "active")
            assert ana["archived_at"] and ana["pin_hash"] and ana["role_id"] == p.front_desk
            # Signed out at once, everywhere.
            for who in (desk, pool):
                status, reply = await p.call("GET", "/api/wristbands/log", who)
                assert status == 403 and reply["error"] == "pin_required"
            assert all(sid != p.ana for _, _, sid in main.pin_sessions.values())
            assert (await p.call("GET", "/api/wristbands/log", bob))[0] == 200
            # No sign-in by name or PIN, on any device; not offered in the name lists.
            for headers in ({"Authorization": f"Bearer {p.token}"}, shared):
                status, reply = await unlock(p, headers, staff_id=p.ana, pin="1111")
                assert status == 404 and reply["error"] == "Pick your name again."
                status, me = await p.call("GET", "/api/me", headers)
                assert p.ana not in [m["id"] for m in me["staff"]]
            p.db.update_device(shared_id, pin_only=1)
            status, reply = await unlock(p, shared, pin="1111")
            assert status == 403 and reply["error"].startswith("Wrong PIN")
            assert p.db.staff_pins(p.db.device(shared_id)) == [(p.bob, p.db.staff(p.bob)["pin_hash"])]
            assert p.db.staff_pin_lengths(p.db.device(shared_id)) == [4]
            assert {m["id"] for m in p.db.staff_for_device(p.db.device(shared_id))} == {p.bob}
            # Their PIN stays taken (Restore gives it back), but they don't count in their role.
            assert await main.pin_taken("1111")
            assert next(r for r in p.db.roles() if r["id"] == p.front_desk)["staff_count"] == 1
            # Their Home Assistant link is never used, even for a command already on its way.
            try:
                await main.send_command(p.db.device(p.device), ana, "light", "turn_off",
                                        {"entity_id": "light.porch"})
                raise AssertionError("a command was sent for an archived person")
            except ActionRejected:
                pass
            assert len(sent) == 1
            # A session somehow left over is refused all the same, and so is an encoder link.
            main.pin_sessions["left-over"] = (p.device, time.time(), p.ana)
            status, reply = await p.call("GET", "/api/wristbands/log",
                                         {**desk, "X-Session": "left-over"})
            assert status == 403 and reply["error"] == "pin_required"
            main.pin_sessions["left-over"] = (p.device, time.time(), p.ana)
            assert main.link_refusal(p.device, "left-over", p.ana) == "signed_out"

            # No key card for them, from the staff app or the Staff tab.
            card = staff_card(FLOOR_ONE, name=" ana ")
            for path in ("/api/wristbands/preview", "/api/wristbands/jobs"):
                status, reply = await p.call("POST", path, bob, card)
                assert status == 403 and reply["field"] == "name", reply
                assert reply["error"] == "Ana is archived, so no key card can be made for them."
            blocked = [a for a in p.db.activity(50) if a["action"] == "Blocked: wristbands"]
            assert len(blocked) == 1 and "archived" in blocked[0]["detail"]   # only the real try
            house = department(p, "Housekeeping", card_access=FLOOR_ONE)
            p.db.update_staff(p.ana, department_id=house)
            status, reply = await p.admin_call("POST", f"/api/staff/{p.ana}/keycard", {})
            assert status == 400 and "archived" in reply["error"]
            # A lost card of theirs: a notice card, never a replacement.
            lost = key_card(p, p.ana, "778899")
            p.db.set_card_state(lost, "lost", "Admin")
            status, reply = await p.admin_call("POST", f"/api/keycards/{lost}/replace", {})
            assert status == 409 and "notice card" in reply["error"]

            # The dashboard can't edit, unlock or link them until they're restored.
            for method, path, body in (("PATCH", f"/api/staff/{p.ana}", {"active": True}),
                                       ("PATCH", f"/api/staff/{p.ana}", {"role_id": p.manager}),
                                       ("POST", f"/api/staff/{p.ana}/link",
                                        {"username": "ana", "password": "x"})):
                status, reply = await p.admin_call(method, path, body)
                assert status == 409 and "Restore them first" in reply["error"], (path, reply)
            assert (await p.admin_call("POST", f"/api/staff/{p.ana}/unlock"))[0] == 200
            assert p.db.staff(p.ana)["status"] == "archived"
            assert (await p.admin_call("POST", f"/api/staff/{p.ana}/archive"))[0] == 409
            # Editing a phone's people keeps them on it (they're not offered there), for Restore.
            status, _ = await p.admin_call("PATCH", f"/api/devices/{shared_id}",
                                           {"staff_ids": [p.bob, p.cy]})
            assert status == 200
            assert set(p.db.device_staff_ids(shared_id)) == {p.ana, p.bob, p.cy}
            # Adding someone else by their name says who has it.
            status, reply = await p.admin_call("POST", "/api/staff", {"name": "ANA", "pin": "9090"})
            assert status == 400 and "Ana is archived under that name" in reply["error"]
            # The dashboard lists them with their status, and the log says who archived them.
            _, state = await p.admin_call("GET", "/api/state")
            row = next(m for m in state["staff"] if m["id"] == p.ana)
            assert row["status"] == "archived" and row["archived_at"]
            assert "pin_hash" not in row and "ha_refresh_token" not in row
            assert ("Admin", "Archived Ana", True) in p.activity()
    run(scenario())


def test_restore_puts_them_back_as_they_were(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            await archive(p, p.ana)
            assert (await p.admin_call("POST", f"/api/staff/{p.dee}/restore"))[0] == 409
            reply = await restore(p, p.ana)
            assert reply["status"] == "active"
            ana = p.db.staff(p.ana)
            assert (ana["archived_at"], ana["archived_by"], ana["archived_from"]) == (None, None, None)
            assert ana["role_id"] == p.front_desk
            await p.sign_in(p.ana)                    # their own PIN works again
            assert ("Admin", "Restored Ana", True) in p.activity()
            # Someone turned off before they were archived comes back turned off.
            assert (await p.admin_call("PATCH", f"/api/staff/{p.dee}", {"active": False}))[0] == 200
            await archive(p, p.dee)
            assert p.db.staff(p.dee)["archived_from"] == "disabled"
            assert (await restore(p, p.dee))["status"] == "disabled"
            # A wrong-PIN lock doesn't come back with them.
            p.db.update_staff(p.cy, failed_pins=3, locked_until=time.time() + 600)
            await archive(p, p.cy)
            await restore(p, p.cy)
            cy = p.db.staff(p.cy)
            assert (cy["status"], cy["failed_pins"], cy["locked_until"]) == ("active", 0, None)
    run(scenario())


def test_only_archived_people_can_be_deleted(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            status, reply = await p.admin_call("DELETE", f"/api/staff/{p.cy}")
            assert status == 409 and "Archive Cy first" in reply["error"]
            assert p.db.staff(p.cy)
            await archive(p, p.cy)
            assert (await p.admin_call("DELETE", f"/api/staff/{p.cy}"))[0] == 200
            assert p.db.staff(p.cy) is None
            assert ("Admin", "Deleted staff member Cy", True) in p.activity()
            assert (await p.admin_call("DELETE", f"/api/staff/{p.cy}"))[0] == 404
    run(scenario())


# ---------------------------------------------------------------- Connecteam

def test_the_connecteam_sync_never_brings_them_back(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        connecteam_on(p)
        house = department(p, "Housekeeping", smart_group_id="11", role_id=p.front_desk)
        department(p, "Managers", smart_group_id="22", role_id=p.manager)
        users = [ct_user(601, "Ivy", code="6061", groups=[11]), ct_user(602, "Jon", code="6062")]
        async with FakeConnecteam(users) as ct, p:
            assert (await sync(p))["added"] == 2
            ivy = by_name(p, "Ivy")
            main.pin_sessions["ivy-session"] = (p.device, time.time(), ivy["id"])
            await archive(p, ivy["id"])
            assert "ivy-session" not in main.pin_sessions
            # Moved and given a new kiosk code in Connecteam: left exactly as she was.
            ct.users = [ct_user(601, "Ivy", code="7071", groups=[22]), users[1]]
            counts = await sync(p)
            assert counts == {"added": 0, "updated": 0, "turned_off": 0, "turned_on": 0,
                              "no_code": 0}
            now = p.db.staff(ivy["id"])
            assert (now["status"], now["department_id"], now["role_id"]) == (
                "archived", house, p.front_desk)
            assert check_pin("6061", now["pin_hash"])
            # Archived or removed in Connecteam: still archived (not just turned off), and never
            # added again when Connecteam lists them once more.
            ct.users = [users[1]]
            assert (await sync(p))["turned_off"] == 0
            assert p.db.staff(ivy["id"])["status"] == "archived"
            ct.users = users
            assert (await sync(p))["added"] == 0
            assert [m["name"] for m in p.db.staff_list()].count("Ivy") == 1
            assert p.db.staff(ivy["id"])["status"] == "archived"
            # They aren't why the time clock is checked.
            p.db.update_staff(ivy["id"], require_clock_in=1, clock_in_set=1)
            p.db.update_staff(by_name(p, "Jon")["id"], require_clock_in=0, clock_in_set=1)
            assert not main.connecteam_wanted()
            # Add from Connecteam says where they are.
            main.connecteam._users = None
            status, reply = await p.admin_call("GET", "/api/connecteam/users")
            assert status == 200, reply
            listed = {u["id"]: u for u in reply["users"]}
            assert listed["601"]["linked_to"] == "Ivy" and listed["601"]["linked_archived"] is True
            assert listed["602"]["linked_archived"] is False
            # Restored, they're the sync's again: removed in Connecteam means turned off.
            await restore(p, ivy["id"])
            ct.users = [users[1]]
            assert (await sync(p))["turned_off"] == 1
            assert p.db.staff(ivy["id"])["status"] == "disabled"
    run(scenario())


# ---------------------------------------------------------------- their key cards at the doors

def test_a_still_valid_card_of_an_archived_person_alerts_at_the_door(tmp_path, monkeypatch):
    sent = fresh_state(monkeypatch)

    def alerts(p):
        return [(a["action"], a["detail"]) for a in p.db.activity(500)
                if a["actor"] == "Door activity"][::-1]

    async def scenario():
        p = Portal(tmp_path)
        card = key_card(p, p.ana, "4242424242")
        short = key_card(p, p.ana, "5151515151", days=-0.01)    # expired a moment ago
        bobs = key_card(p, p.bob, "6161616161")
        async with p:
            now = time.time()
            await deliver(p, POOL, [record(49, "4242424242", at=now - 60)])
            await settle()
            assert alerts(p) == [] and stored(p)[-1]["alert"] is None
            reply = await archive(p, p.ana)
            # Archiving lists the cards that still open doors (not the expired one).
            assert [c["id"] for c in reply["cards"]] == [card]
            assert reply["cards"][0]["holder_archived"] is True
            assert p.db.activity(1)[0]["detail"].startswith("1 key card still opens doors")
            # A tap from before they were archived, arriving late, isn't an alert.
            await deliver(p, POOL, [record(49, "4242424242", at=now - 30)])
            await settle()
            assert alerts(p) == [] and stored(p)[-1]["alert"] is None

            # (Each try at its own time: TT Hotel sending the very same record again is dropped.)
            await deliver(p, POOL, [record(49, "4242424242", at=now + 1)])
            await settle()
            (action, detail), = alerts(p)
            assert action == "Archived person's card opened Pool Gate"
            assert detail.startswith("Card 4242424242 (Ana) · opened")
            (domain, service, data), = sent
            assert data["title"] == "Archived person's card opened Pool Gate"
            assert "Card 4242424242 (Ana) belongs to someone who was archived" in data["message"]
            assert data["notification_id"] == f"staff_access_card_{card}"
            assert stored(p)[-1]["alert"] == "archived" and stored(p)[-1]["problem"] == 1
            # Once per card every 10 minutes, like the others.
            await deliver(p, POOL, [record(49, "4242424242", success=0, at=now + 2)])
            await settle()
            assert len(alerts(p)) == 1 and len(sent) == 1
            main.card_alerts[f"card {card}"] -= main.CARD_ALERT_GAP
            await deliver(p, POOL, [record(49, "4242424242", success=0, at=now + 3)])
            await settle()
            assert alerts(p)[-1][0] == "Archived person's card tried at Pool Gate"
            assert len(sent) == 2
            # An expired card of theirs opens nothing; someone else's card is just a card.
            await deliver(p, POOL, [record(49, "5151515151", at=now + 4),
                                    record(49, "6161616161", at=now + 4)])
            await settle()
            assert [r["alert"] for r in stored(p)[-2:]] == [None, None]
            assert [r["card_id"] for r in stored(p)[-2:]] == [short, bobs]
            # Revoked or reported lost, it alerts as that instead; restored, not at all.
            p.db.set_card_state(card, "lost", "Admin")
            main.card_alerts.clear()
            await deliver(p, POOL, [record(49, "4242424242", success=0, at=now + 5)])
            await settle()
            assert stored(p)[-1]["alert"] == "lost"
            other = key_card(p, p.ana, "7272727272")
            await restore(p, p.ana)
            await deliver(p, POOL, [record(49, "7272727272", at=now + 6)])
            await settle()
            assert stored(p)[-1]["card_id"] == other and stored(p)[-1]["alert"] is None
            _, data = await p.admin_call("GET", "/api/door-activity?problems=1")
            assert "archived" in [r["alert"] for r in data["records"]]
    run(scenario())


def test_alert_rule_for_archived_holders():
    card = {"id": 1, "state": "active", "state_at": None, "cleared_at": None, "expires_at": 2000.0,
            "holder_status": "archived", "holder_archived_at": 1000.0}
    tap = lambda at: {"at": at, "record_type": 49}
    assert doorlog.alert_for(tap(1500), card) == "archived"
    assert doorlog.alert_for(tap(900), card) is None          # before they were archived
    assert doorlog.alert_for(tap(2100), card) is None         # after it expired
    assert doorlog.alert_for(tap(1500), {**card, "cleared_at": 1200.0}) is None
    assert doorlog.alert_for(tap(1500), {**card, "holder_status": "active"}) is None
    assert doorlog.alert_for(tap(1500), {**card, "holder_status": None}) is None


# ---------------------------------------------------------------- the Key cards section

def test_key_card_totals_and_one_persons_cards(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        for number in ("100001", "100002", "100003"):
            key_card(p, p.ana, number)
        lost = key_card(p, p.bob, "200001")
        key_card(p, p.bob, "200002", days=-1)                     # expired: not counted
        old_lost = key_card(p, p.cy, "200003", days=-1)
        p.db.set_card_state(lost, "lost", "Admin")
        p.db.set_card_state(old_lost, "lost", "Admin")
        async with p:
            _, state = await p.admin_call("GET", "/api/state")
            assert state["key_card_totals"] == {"active": 3, "lost": 1}
            status, reply = await p.admin_call("GET", "/api/keycards?state=active")
            assert status == 200 and reply["totals"] == {"active": 3, "lost": 1}
            status, reply = await p.admin_call("GET", f"/api/keycards?state=all&holder={p.bob}")
            assert sorted(c["card_number"] for c in reply["cards"]) == ["200001", "200002"]
            assert all(c["holder_archived"] is False for c in reply["cards"])
            assert (await p.admin_call("GET", "/api/keycards?holder=bob"))[0] == 400
    run(scenario())


# ---------------------------------------------------------------- backups, exports, migrations

def test_backups_exports_and_migrations_carry_it(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            assert (await p.admin_call("PATCH", f"/api/staff/{p.dee}", {"active": False}))[0] == 200
            await archive(p, p.dee)
            await archive(p, p.cy)
            dee = p.db.staff(p.dee)
            status, backup = await p.admin_call("GET", "/api/backup")
            row = next(m for m in backup["staff"] if m["id"] == p.dee)
            assert (row["status"], row["archived_at"], row["archived_from"]) == (
                "archived", dee["archived_at"], "disabled")
            # Restores keep it.
            p.db.restore_staff(p.dee)
            p.db.restore_staff(p.cy)
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, reply
            dee = p.db.staff(p.dee)
            assert (dee["status"], dee["archived_from"], dee["archived_by"]) == (
                "archived", "disabled", row["archived_by"])
            assert p.db.staff(p.cy)["status"] == "archived"
            assert p.db.staff(p.ana)["archived_at"] is None
            # A backup from before archiving: everyone as they are in it.
            old = json.loads(json.dumps(backup))
            for m in old["staff"]:
                for key in ("archived_at", "archived_by", "archived_from"):
                    m.pop(key)
                if m["status"] == "archived":
                    m["status"] = "active"
            assert (await p.admin_call("POST", "/api/restore", {"backup": old}))[0] == 200
            assert {m["status"] for m in p.db.staff_list()} == {"active"}
            # A damaged one: archived, with a time and the status Restore puts back.
            bad = json.loads(json.dumps(backup))
            row = next(m for m in bad["staff"] if m["id"] == p.cy)
            row.update(archived_at="soon", archived_by={"x": 1}, archived_from="locked")
            assert (await p.admin_call("POST", "/api/restore", {"backup": bad}))[0] == 200
            cy = p.db.staff(p.cy)
            assert cy["status"] == "archived" and cy["archived_at"] > time.time() - 60
            assert (cy["archived_by"], cy["archived_from"]) == (None, "active")

            # Migration files too.
            snapshot = migrate.make_snapshot(p.db, main.branding, main.VERSION)
            clean = migrate.clean_snapshot(transfer.read_backup_file(transfer.pack_backup(snapshot)),
                                           main.clean_settings, p.db.settings(),
                                           main.branding.clean_logo)
            other = db_module.Database(str(tmp_path / "migrated"))
            other.replace_everything(clean)
            assert other.staff(p.cy)["status"] == "archived"
            assert other.staff(p.cy)["archived_at"] == cy["archived_at"]
            other.conn.close()

            # The staff export marks them; importing it keeps them archived, and someone archived
            # here stays archived whatever a file says.
            exported = transfer.export_staff(p.db, "test")
            people = {m["name"]: m for m in exported["staff"]}
            assert people["Cy"]["archived"] is True and people["Cy"]["enabled"] is False
            assert "archived" not in people["Ana"]
            q = Portal(tmp_path / "other")
            result = transfer.import_staff(q.db, exported, "replace")
            assert q.db.staff(q.cy)["status"] == "archived" and q.db.staff(q.cy)["archived_at"]
            assert q.db.staff(q.ana)["status"] == "active"
            assert q.cy in result["signed_out"]
            people["Cy"].pop("archived")
            people["Cy"]["enabled"] = True
            result = transfer.import_staff(q.db, exported, "replace")
            assert result["kept_archived"] == ["Cy"] and q.db.staff(q.cy)["status"] == "archived"
            q.db.restore_staff(q.cy)
            assert q.db.staff(q.cy)["status"] == "active"
            q.db.conn.close()
    run(scenario())
