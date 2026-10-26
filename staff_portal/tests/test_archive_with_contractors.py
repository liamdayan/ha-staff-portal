"""Archiving people together with staff doors and contractor badges (0.9.1): an archived person
makes nothing, whatever their role or their own wristband rules allow (contractor badges
included); Door activity tells an archived person's key card from a contractor badge at a staff
door; and backups and migration files carry archived people, staff doors and the contractor
settings together."""

import json
import time

from test_archive_staff import archive, key_card, restore, unlock
from test_door_activity import deliver, fresh_state, record, settle, stored
from test_staff_doors_and_contractors import contractor
from test_wristbands_server import FRONT_DESK, Portal, run

import db as db_module  # noqa: E402
import main  # noqa: E402
import migrate  # noqa: E402
import transfer  # noqa: E402

BADGES = {**FRONT_DESK, "types": ["day_pass", "overnight", "contractor"]}
OFFICE = 201   # Room 201 in the test doors, renamed and marked a staff door here


def staff_office(p: Portal) -> None:
    p.db.set_wb_door(OFFICE, name="Staff Office", kind="staff")


def badge_job(p: Portal, maker: int, number: str, name="Sam Lee (Acme)") -> int:
    """A contractor badge made by someone (as the encoder leaves it): its wb_cards ID."""
    job_id = p.db.add_wb_job(card_type="contractor", name=name, count=1,
                             records=[{"level": "door", "door": OFFICE}], summary="Staff Office",
                             expires_at=time.time() + 2 * 86400, allow_lockout=False,
                             staff_id=maker, device_id=p.device, actor="Dee", role=None, ip=None,
                             encoder_url="ws://127.0.0.1:1")
    p.db.update_wb_card(job_id, 1, status="written", card_number=number,
                        written_at=time.time() - 7200)
    p.db.update_wb_job(job_id, status="done", finished_at=time.time())
    return p.db.wb_job(job_id)["cards"][0]["id"]


def test_an_archived_person_makes_no_contractor_badges(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        staff_office(p)
        # Dee's own rules allow contractor badges (role-based computer); a shared kiosk computer
        # whose role allows them too.
        p.db.update_staff(p.dee, wristbands=json.dumps(BADGES))
        p.db.set_role_wristbands(p.front_desk, BADGES)
        kiosk_id, kiosk_token = p.computer("Kiosk PC", "staff", "ws://127.0.0.1:1")
        async with p:
            desk = await p.sign_in(p.dee)
            kiosk = await p.sign_in(p.dee, kiosk_token)
            for who in (desk, kiosk):
                mine = (await p.call("GET", "/api/wristbands", who))[1]["types"]["contractor"]
                assert mine["allowed"], mine
                status, reply = await p.call("POST", "/api/wristbands/preview", who,
                                             contractor(door_ids=[900, OFFICE]))
                assert status == 200, reply
            running = p.db.add_wb_job(card_type="contractor", name="Sam Lee (Acme)", count=3,
                                      records=[], summary="Staff Office",
                                      expires_at=time.time() + 3600, allow_lockout=False,
                                      staff_id=p.dee, device_id=p.device, actor="Dee", role=None,
                                      ip=None, encoder_url="ws://x:1")
            before = next(r for r in p.db.roles() if r["id"] == p.front_desk)["staff_count"]

            await archive(p, p.dee)
            # The badges still being made stop, and nothing more can be made or even looked at.
            assert p.db.wb_job(running)["status"] == "cancelled"
            for who in (desk, kiosk):
                for method, path, body in (("GET", "/api/wristbands", None),
                                           ("POST", "/api/wristbands/preview", contractor()),
                                           ("POST", "/api/wristbands/jobs", contractor())):
                    status, reply = await p.call(method, path, who, body)
                    assert (status, reply["error"]) == (403, "pin_required"), (path, reply)
            for headers in ({"Authorization": f"Bearer {p.token}"},
                            {"Authorization": f"Bearer {kiosk_token}"}):
                status, reply = await unlock(p, headers, staff_id=p.dee, pin="4444")
                assert status == 404
            assert [j["id"] for j in p.jobs() if j["card_type"] == "contractor"] == [running]
            assert main.wristband_permissions(p.db.device(kiosk_id), None)["types"] == []
            # Their role no longer counts them, and their own rules can't be changed meanwhile.
            assert next(r for r in p.db.roles()
                        if r["id"] == p.front_desk)["staff_count"] == before - 1
            status, _ = await p.admin_call("PATCH", f"/api/staff/{p.dee}",
                                           {"wristbands": {**BADGES, "types": ["contractor"]}})
            assert status == 409

            # Restored, they're back as they were: their own rules included.
            await restore(p, p.dee)
            assert json.loads(p.db.staff(p.dee)["wristbands"])["types"] == BADGES["types"]
            desk = await p.sign_in(p.dee)
            status, reply = await p.call("POST", "/api/wristbands/preview", desk, contractor())
            assert status == 200, reply
    run(scenario())


def test_door_activity_at_a_staff_door(tmp_path, monkeypatch):
    sent = fresh_state(monkeypatch)

    async def scenario():
        p = Portal(tmp_path)
        staff_office(p)
        ana_card = key_card(p, p.ana, "4242424242")
        badge = badge_job(p, p.dee, "555123")
        async with p:
            now = time.time()
            await archive(p, p.ana)
            await archive(p, p.dee)   # who made the badge doesn't matter: it's the contractor's
            await deliver(p, OFFICE, [record(49, "4242424242", at=now + 1),
                                      record(49, "555123", at=now + 1)])
            await settle()
            rows = {r["card_id"]: r for r in stored(p)}
            assert (rows[ana_card]["alert"], rows[badge]["alert"]) == ("archived", None)
            _, data = await p.admin_call("GET", "/api/door-activity")
            seen = {r["card_id"]: (r["door"], r["who"], r["card_type"], r["alert"])
                    for r in data["records"]}
            assert seen == {ana_card: ("Staff Office", "Ana", "staff", "archived"),
                            badge: ("Staff Office", "Sam Lee (Acme)", "contractor", None)}
            (domain, service, note), = sent
            assert note["title"] == "Archived person's card opened Staff Office"
            assert "Card 4242424242 (Ana) belongs to someone who was archived" in note["message"]

            # An alert about a contractor badge calls it one.
            p.db.set_card_state(badge, "revoked", "Admin")
            await deliver(p, OFFICE, [record(49, "555123", success=0, at=time.time() + 2)])
            await settle()
            assert stored(p)[-1]["alert"] == "revoked"
            line = next(a for a in p.db.activity(10) if a["actor"] == "Door activity")
            assert line["action"] == "Revoked card tried at Staff Office"
            assert line["detail"].startswith("Contractor badge 555123 (Sam Lee (Acme)) · refused")
            assert sent[-1][2]["message"].startswith(
                "Contractor badge 555123 (Sam Lee \\(Acme\\)) was revoked.")
    run(scenario())


def test_backups_and_migration_files_carry_both(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        staff_office(p)
        p.db.update_settings({"wb_contractor_max": "8", "wb_lockout_contractor": "1"})
        p.db.update_staff(p.dee, wristbands=json.dumps(BADGES))
        badge_job(p, p.dee, "555123")
        async with p:
            await archive(p, p.dee)
            status, backup = await p.admin_call("GET", "/api/backup")
            assert status == 200
            dee = next(m for m in backup["staff"] if m["id"] == p.dee)
            assert (dee["status"], dee["archived_from"]) == ("archived", "active")
            assert {d["lock_id"]: d["kind"] for d in backup["wristband_doors"]}[OFFICE] == "staff"

            p.db.restore_staff(p.dee)
            p.db.set_wb_door(OFFICE, kind=None)
            p.db.update_settings({"wb_contractor_max": "5", "wb_lockout_contractor": "0"})
            status, reply = await p.admin_call("POST", "/api/restore", {"backup": backup})
            assert status == 200, reply
            assert p.db.staff(p.dee)["status"] == "archived"
            assert json.loads(p.db.staff(p.dee)["wristbands"])["types"] == BADGES["types"]
            assert p.db.wb_door(OFFICE)["kind"] == "staff"
            s = p.db.settings()
            assert (s["wb_contractor_max"], s["wb_lockout_contractor"]) == ("8", "1")
            assert [(j["card_type"], j["staff_id"]) for j in p.jobs()] == [("contractor", p.dee)]

        snapshot = migrate.make_snapshot(p.db, main.branding, main.VERSION)
        clean = migrate.clean_snapshot(transfer.read_backup_file(transfer.pack_backup(snapshot)),
                                       main.clean_settings, p.db.settings(),
                                       main.branding.clean_logo)
        other = db_module.Database(str(tmp_path / "migrated"))
        try:
            other.replace_everything(clean)
            assert other.staff(p.dee)["status"] == "archived"
            assert other.wb_door(OFFICE)["kind"] == "staff"
            assert other.settings()["wb_contractor_max"] == "8"
        finally:
            other.conn.close()
    run(scenario())
