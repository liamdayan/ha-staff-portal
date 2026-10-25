"""This computer (USB): a computer's card encoder reached through the staff app open on it, and
Automatic only ever using an encoder an admin has confirmed.

The add-on never connects to a computer in This computer (USB) mode. The staff app on it gets a
ticket (POST /api/encoder/ticket, with its device token and PIN session), opens its link (GET
/api/encoder/link), and passes each SN Web Socket request it's sent on to the helper on the same
computer, and the answers back. Here a fake browser does that, with encoder_sim standing in for
SN Web Socket at ws://127.0.0.1:8102.

Nothing here talks to a real TT Hotel, Connecteam or Home Assistant (see test_wristbands_server).
"""

import asyncio
import inspect
import json
import logging
import os
import re
import time

import aiohttp
from aiohttp import WSMsgType

from test_wristbands_server import (DOORS, FakeTTHotel, Portal, Simulator, day_pass,
                                    fake_area_layout, fake_states, helper, job_until, run,
                                    search_done, tt_lock, tthotel_account, waiting_for)

import discovery  # noqa: E402
import encoder  # noqa: E402
import main  # noqa: E402
from encoder import (NO_LINK, NO_REPLY, UNREACHABLE, EncoderError, LocalRelay,  # noqa: E402
                     RelaySocket)

# What the staff app forwards (and nothing else): the same list the add-on sends.
ALLOWED = {"CE_DisconnectComm", "GetDeviceInfo", "CE_ConnectComm", "CE_GetCardID",
           "CE_InitCardEncoder", "CE_SetSectors", "CE_InitCard", "CE_ClearCard", "CE_WriteCard",
           "CE_ReadCard", "CE_CancelCard", "CE_ReadCancellationInfo", "CE_Beep", "CE_GetVersion"}


class FakeBrowser:
    """The staff app on a computer in This computer (USB) mode, as far as its link goes: a
    ticket, the link, and SN Web Socket (encoder_sim) on "its own computer". Requests outside
    the allow-list are dropped, as the app does. helper=None: SN Web Socket isn't running."""

    def __init__(self, p: Portal, who: dict, helper_url: str | None):
        self.p = p
        self.who = who
        self.helper_url = helper_url
        self.calls: list[str] = []          # functions the add-on sent to this browser
        self.hotel_infos = 0                # requests that carried the hotel key
        self.ws = None
        self.local = None
        self.session = None
        self.close_code = None
        self.closed = asyncio.Event()
        self.answer = True                  # False: requests are received but not passed on
        self._tasks: list[asyncio.Task] = []

    async def ticket(self) -> str:
        resp = await self.p.staff.post("/api/encoder/ticket", headers=self.who)
        assert resp.status == 200, await resp.text()
        reply = await resp.json()
        assert reply["expires_in"] == 30 and reply["path"] == "/api/encoder/link"
        return reply["ticket"]

    async def open(self, device_id: int, ticket: str | None = None) -> "FakeBrowser":
        before = main.open_links.get(device_id)
        self.ws = await self.p.staff.ws_connect(
            f"/api/encoder/link?ticket={ticket or await self.ticket()}")
        self.session = aiohttp.ClientSession()
        await self._connect_helper()
        self._tasks = [asyncio.create_task(self._from_addon())]
        for _ in range(200):   # the add-on has taken this link as the computer's
            link = main.open_links.get(device_id)
            if link is not None and link is not before:
                return self
            await asyncio.sleep(0.01)
        raise AssertionError("the link wasn't taken")

    async def _connect_helper(self) -> None:
        if self.local is not None and not self.local.closed:
            return
        try:
            if self.helper_url is None:
                raise aiohttp.ClientConnectionError("nothing listening")
            self.local = await self.session.ws_connect(self.helper_url)
            greeting = await self.local.receive_json()
        except aiohttp.ClientError:
            self.local = None
            await self.ws.send_json({"type": "status", "helper": "unreachable"})
            return
        await self.ws.send_json({"type": "reply", "reply": greeting})
        await self.ws.send_json({"type": "status", "helper": "ready",
                                 "version": greeting.get("snWebSocketVersion")})
        self._tasks.append(asyncio.create_task(self._from_helper()))

    async def _from_addon(self) -> None:
        async for msg in self.ws:
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            request = data.get("request") if data.get("type") == "call" else None
            if not isinstance(request, dict):
                continue
            self.calls.append(request.get("function"))
            self.hotel_infos += "hotelInfo" in request
            if request.get("function") not in ALLOWED or not self.answer:
                continue
            await self._connect_helper()
            if self.local is not None:
                await self.local.send_json(request)
        self.close_code = self.ws.close_code
        self.closed.set()

    async def _from_helper(self) -> None:
        async for msg in self.local:
            if msg.type == WSMsgType.TEXT and not self.ws.closed:
                await self.ws.send_json({"type": "reply", "reply": json.loads(msg.data)})

    async def wait_closed(self, timeout: float = 5.0) -> int:
        await asyncio.wait_for(self.closed.wait(), timeout)
        return self.close_code

    async def close(self) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.local is not None:
            await self.local.close()
        if self.session is not None:
            await self.session.close()


def sim_url(sim) -> str:
    return f"ws://127.0.0.1:{sim._runner.addresses[0][1]}"


def local_portal(tmp_path) -> Portal:
    """A portal whose front-desk computer is This computer (USB)."""
    p = Portal(tmp_path, encoder_url=None)
    p.db.update_device(p.device, encoder_local=1)
    return p


async def link_refused(p: Portal, path: str, headers=None) -> int:
    try:
        ws = await p.staff.ws_connect(path, headers=headers)
    except aiohttp.WSServerHandshakeError as err:
        return err.status
    await ws.close()
    raise AssertionError("the link was opened")


def other_computer(p: Portal, name="Back Desk") -> tuple[int, str]:
    device_id, token = p.computer(name, "role", None)
    p.db.update_device(device_id, encoder_local=1)
    return device_id, token


# ---------------------------------------------------------------- opening a link

def test_a_link_needs_a_ticket_for_this_device_and_session(tmp_path, monkeypatch):
    async def scenario():
        p = local_portal(tmp_path)
        back, back_token = other_computer(p)
        async with p:
            ana = await p.sign_in(p.ana)
            # The device token alone isn't enough: the ticket needs a PIN session.
            resp = await p.staff.post("/api/encoder/ticket",
                                      headers={"Authorization": f"Bearer {p.token}"})
            assert resp.status == 403 and (await resp.json())["error"] == "pin_required"
            resp = await p.staff.post("/api/encoder/ticket")
            assert resp.status == 401
            # Someone who can't make wristbands gets none, nor does a computer that isn't
            # This computer (USB).
            cy = await p.sign_in(p.cy)
            resp = await p.staff.post("/api/encoder/ticket", headers=cy)
            assert resp.status == 403
            p.db.update_device(back, encoder_local=0, encoder_url="ws://127.0.0.1:1")
            back_ana = await p.sign_in(p.ana, back_token)
            resp = await p.staff.post("/api/encoder/ticket", headers=back_ana)
            assert resp.status == 403
            p.db.update_device(back, encoder_local=1, encoder_url=None)

            # No ticket, a made-up one, a reused one, an expired one: refused.
            assert await link_refused(p, "/api/encoder/link") == 401
            assert await link_refused(p, "/api/encoder/link?ticket=made-up") == 401
            browser = FakeBrowser(p, ana, None)
            ticket = await browser.ticket()
            await browser.open(p.device, ticket)
            assert await link_refused(p, f"/api/encoder/link?ticket={ticket}") == 401
            await browser.close()
            ticket = await browser.ticket()
            device_id, session, staff_id, _ = main.link_tickets[ticket]
            assert (device_id, session, staff_id) == (p.device, ana["X-Session"], p.ana)
            main.link_tickets[ticket] = (device_id, session, staff_id, time.time() - 1)
            assert await link_refused(p, f"/api/encoder/link?ticket={ticket}") == 401
            # A ticket is only good for the device it was issued to.
            ticket = await browser.ticket()
            other = {"Authorization": f"Bearer {back_token}"}
            assert await link_refused(p, f"/api/encoder/link?ticket={ticket}", other) == 403
            # ...and only while its PIN session lasts.
            ticket = await browser.ticket()
            resp = await p.staff.post("/api/signout", headers=ana)
            assert resp.status == 200
            assert await link_refused(p, f"/api/encoder/link?ticket={ticket}") == 403
            # A plain request (not a WebSocket) with a good ticket opens nothing.
            ana = await p.sign_in(p.ana)
            browser = FakeBrowser(p, ana, None)
            resp = await p.staff.get(f"/api/encoder/link?ticket={await browser.ticket()}")
            assert resp.status == 400 and not main.open_links
            # Tickets are limited per device.
            monkeypatch.setattr(main, "LINK_TICKETS_PER_MINUTE", 3)
            main.recent_link_tickets.clear()
            statuses = [(await p.staff.post("/api/encoder/ticket", headers=ana)).status
                        for _ in range(4)]
            assert statuses == [200, 200, 200, 429]
    run(scenario())


def test_staff_page_may_reach_sn_web_socket_on_its_own_computer(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "area_layout", fake_area_layout)
    monkeypatch.setattr(main.ha, "states", fake_states)

    async def scenario():
        p = local_portal(tmp_path)
        async with p:
            resp = await p.staff.get("/")
            csp = resp.headers["Content-Security-Policy"]
            assert "connect-src 'self' ws://127.0.0.1:8102 ws://localhost:8102;" in csp
            resp = await p.staff.get("/api/me")   # the fallback on everything else
            assert "connect-src 'self' ws://127.0.0.1:8102 ws://localhost:8102;" in (
                resp.headers["Content-Security-Policy"])
            # Not the dashboard's preview of the staff app. (The dashboard itself may, for This
            # computer: test_encoder_dashboard.)
            for path in ("/preview",):
                resp = await p.admin.get(path)
                assert resp.status == 200
                assert "connect-src 'self';" in resp.headers["Content-Security-Policy"], path
                assert "8102" not in resp.headers["Content-Security-Policy"]
            # The staff app is told to open the link.
            ana = await p.sign_in(p.ana)
            status, reply = await p.call("GET", "/api/wristbands", ana)
            assert status == 200 and reply["encoder_local"] is True
            assert reply["encoder"]["local"] is True and reply["encoder"]["link"] is False
            status, reply = await p.call("GET", "/api/entities", ana)
            assert reply["wristbands"] is True and reply["encoder_local"] is True
            cy = await p.sign_in(p.cy)                 # can't make wristbands: no link either
            status, reply = await p.call("GET", "/api/entities", cy)
            assert reply["wristbands"] is False and reply["encoder_local"] is False
            p.db.update_device(p.device, encoder_local=0, encoder_url="ws://127.0.0.1:1")
            status, reply = await p.call("GET", "/api/wristbands", ana)
            assert reply["encoder_local"] is False and "local" not in reply["encoder"]
    run(scenario())


# ---------------------------------------------------------------- cards through the link

def test_cards_are_made_through_the_computers_own_link(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    async def scenario():
        async with Simulator() as sim, Simulator() as back_sim, \
                FakeTTHotel([tt_lock(d) for d in DOORS]):
            p = local_portal(tmp_path)
            tthotel_account(p)
            back, back_token = other_computer(p)
            async with p:
                ana = await p.sign_in(p.ana)
                desk = await FakeBrowser(p, ana, sim_url(sim)).open(p.device)
                dee = await p.sign_in(p.dee, back_token)
                back_browser = await FakeBrowser(p, dee, sim_url(back_sim)).open(back)
                status, reply = await p.call("POST", "/api/wristbands/jobs", ana, day_pass())
                assert status == 200, reply
                job_id = reply["job"]["id"]
                assert p.db.wb_job(job_id)["encoder_url"] == f"local:{p.device}"
                await job_until(p, ana, job_id, waiting_for(1))
                first = sim.place_card()
                await job_until(p, ana, job_id, lambda j: j["phase"] == "remove_card")
                sim.remove_card()
                await job_until(p, ana, job_id, waiting_for(2))
                second = sim.place_card()
                job = await job_until(p, ana, job_id, lambda j: j["status"] == "done")
                assert [(c["status"], c["card_number"]) for c in job["cards"]] == [
                    ("written", first), ("written", second)]
                assert {r["mac"] for r in sim.cards[first]["records"]} == {
                    "C540E09C8C90", "C540E09C8C91"}
                # Only this computer's browser was sent anything, and only the usual requests;
                # the encoder was let go after the job.
                assert back_browser.calls == [] and back_sim.calls.maxlen and not back_sim.calls
                assert desk.calls and set(desk.calls) <= ALLOWED
                assert desk.calls[-1] == "CE_DisconnectComm" and not sim.connected
                assert desk.hotel_infos > 0
                # The hotel key is never logged on the way through.
                assert "FAKEHOTELINFO" not in caplog.text

                # The dashboard sees the link and SN Web Socket's version.
                status, state = await p.admin_call("GET", "/api/state")
                card = next(d for d in state["devices"] if d["id"] == p.device)
                assert card["encoder_mode"] == "local"
                assert card["encoder_address"] == f"local:{p.device}"
                assert card["encoder_text"] == "This computer (USB): connected"
                assert card["encoder_link"]["link"] is True
                assert card["encoder_link"]["helper"] == "ready"
                assert card["encoder_link"]["helper_version"] == "2.5.1"
                assert card["encoder_link"]["message"] == "Connected (SN Web Socket 2.5.1)"
                assert card["wristbands_note"] is None
                encoder_state = state["wristbands"]["encoders"][f"local:{p.device}"]
                assert encoder_state["connected"] is True and encoder_state["local"] is True
                assert encoder_state["last_seen"] and not encoder_state["busy"]

                # A newer link (another tab, or the page reloaded) replaces the old one.
                second_tab = await FakeBrowser(p, ana, sim_url(sim)).open(p.device)
                assert await desk.wait_closed() == main.LINK_REPLACED
                desk.calls.clear()
                status, reply = await p.call("POST", "/api/wristbands/jobs", ana,
                                             day_pass(count=1))
                job_id = reply["job"]["id"]
                await job_until(p, ana, job_id, waiting_for(1))
                sim.place_card()
                await job_until(p, ana, job_id, lambda j: j["status"] == "done")
                assert desk.calls == [] and "CE_WriteCard" in second_tab.calls
                assert back_browser.calls == []
                sim.remove_card()
                for browser in (desk, second_tab, back_browser):
                    await browser.close()
    run(scenario())


def test_a_job_waits_for_the_link_then_needs_attention(tmp_path):
    async def scenario():
        async with Simulator() as sim, FakeTTHotel([tt_lock(d) for d in DOORS]):
            p = local_portal(tmp_path)
            tthotel_account(p)
            p.db.update_settings({"wb_card_wait": "1"})
            async with p:
                ana = await p.sign_in(p.ana)
                status, reply = await p.call("POST", "/api/wristbands/jobs", ana,
                                             day_pass(count=1))
                assert status == 200, reply
                job_id = reply["job"]["id"]
                job = await job_until(p, ana, job_id, lambda j: j["phase"] == "waiting_for_link")
                assert job["message"] == "Open Wristbands on Front Desk PC to connect the encoder"
                job = await job_until(p, ana, job_id, lambda j: j["status"] == "needs_attention")
                assert job["message"] == ("Open Wristbands on Front Desk PC to connect the "
                                          "encoder, then try again.")
                # Wristbands opened: Try again goes through, and the card is made.
                browser = await FakeBrowser(p, ana, sim_url(sim)).open(p.device)
                status, reply = await p.call("POST", f"/api/wristbands/jobs/{job_id}/retry", ana)
                assert status == 200, reply
                await job_until(p, ana, job_id, waiting_for(1))
                card = sim.place_card()
                job = await job_until(p, ana, job_id, lambda j: j["status"] == "done")
                assert job["cards"][0]["card_number"] == card
                sim.remove_card()

                # A job started before the link opens starts as soon as it does.
                await browser.close()
                p.db.update_settings({"wb_card_wait": "30"})
                status, reply = await p.call("POST", "/api/wristbands/jobs", ana,
                                             day_pass(count=1))
                job_id = reply["job"]["id"]
                await job_until(p, ana, job_id, lambda j: j["phase"] == "waiting_for_link")
                browser = await FakeBrowser(p, ana, sim_url(sim)).open(p.device)
                await job_until(p, ana, job_id, waiting_for(1))
                sim.place_card()
                await job_until(p, ana, job_id, lambda j: j["status"] == "done")
                sim.remove_card()
                await browser.close()
    run(scenario())


def test_a_link_that_drops_mid_job_needs_attention(tmp_path):
    async def scenario():
        async with Simulator() as sim, FakeTTHotel([tt_lock(d) for d in DOORS]):
            p = local_portal(tmp_path)
            tthotel_account(p)
            async with p:
                ana = await p.sign_in(p.ana)
                browser = await FakeBrowser(p, ana, sim_url(sim)).open(p.device)
                status, reply = await p.call("POST", "/api/wristbands/jobs", ana, day_pass())
                job_id = reply["job"]["id"]
                await job_until(p, ana, job_id, waiting_for(1))
                await browser.close()                       # the tab was closed
                job = await job_until(p, ana, job_id, lambda j: j["status"] == "needs_attention")
                assert job["message"] == ("Open Wristbands on Front Desk PC to connect the "
                                          "encoder, then try again.")
                assert job["cards"][0]["status"] == "failed"
                # Back again: Try again carries on where it left off.
                browser = await FakeBrowser(p, ana, sim_url(sim)).open(p.device)
                status, reply = await p.call("POST", f"/api/wristbands/jobs/{job_id}/retry", ana)
                assert status == 200, reply
                await job_until(p, ana, job_id, waiting_for(1))
                sim.place_card()
                await job_until(p, ana, job_id, lambda j: j["phase"] == "remove_card")
                sim.remove_card()
                await job_until(p, ana, job_id, waiting_for(2))
                sim.place_card()
                job = await job_until(p, ana, job_id, lambda j: j["status"] == "done")
                assert [c["status"] for c in job["cards"]] == ["written", "written"]
                sim.remove_card()

                # SN Web Socket not running on that computer: said at once, not after a timeout.
                await browser.close()
                browser = await FakeBrowser(p, ana, None).open(p.device)
                started = time.monotonic()
                status, reply = await p.call("POST", "/api/wristbands/jobs", ana,
                                             day_pass(count=1))
                job_id = reply["job"]["id"]
                job = await job_until(p, ana, job_id, lambda j: j["status"] == "needs_attention")
                assert time.monotonic() - started < encoder.CALL_TIMEOUT
                assert job["message"] == ("Can't reach SN Web Socket on Front Desk PC. Check "
                                          "that it's running, then try again.")
                status, state = await p.admin_call("GET", "/api/state")
                card = next(d for d in state["devices"] if d["id"] == p.device)
                assert card["encoder_text"] == ("This computer (USB): SN Web Socket isn't "
                                                "answering")
                assert "SN Web Socket isn't answering" in card["wristbands_note"]
                await p.call("POST", f"/api/wristbands/jobs/{job_id}/cancel", ana)
                await browser.close()
    run(scenario())


def test_look_up_and_revoke_through_the_link(tmp_path):
    async def scenario():
        async with Simulator() as sim, FakeTTHotel([tt_lock(d) for d in DOORS]):
            p = local_portal(tmp_path)
            tthotel_account(p)
            async with p:
                bob = await p.sign_in(p.bob)
                browser = await FakeBrowser(p, bob, sim_url(sim)).open(p.device)
                status, reply = await p.call("POST", "/api/wristbands/jobs", bob,
                                             day_pass(count=1))
                job_id = reply["job"]["id"]
                await job_until(p, bob, job_id, waiting_for(1))
                card = sim.place_card()
                await job_until(p, bob, job_id, lambda j: j["status"] == "done")
                # Look it up: read through the link.
                status, reply = await p.call("POST", "/api/wristbands/lookup", bob)
                assert status == 200 and reply["state"] == "waiting"
                deadline = time.monotonic() + 10
                while True:
                    status, found = await p.call("GET", "/api/wristbands/lookup", bob)
                    if found["state"] != "waiting" or time.monotonic() > deadline:
                        break
                    await asyncio.sleep(0.05)
                assert found["state"] == "done", found
                assert found["card_number"] == card and found["job"]["id"] == job_id
                assert sorted(r["label"] for r in found["card"]) == ["Pool Gate", "Spa"]
                assert found["can_revoke"] is True
                # Revoke it: cleared on the encoder, through the link.
                status, reply = await p.call("POST", "/api/wristbands/lookup/revoke", bob)
                assert status == 200, reply
                revoke = await job_until(p, bob, reply["job"]["id"],
                                         lambda j: j["status"] == "done")
                assert revoke["cards"][0]["status"] == "cleared"
                assert sim.cards[card]["records"] == []
                assert {"CE_ReadCard", "CE_ClearCard"} <= set(browser.calls)
                assert set(browser.calls) <= ALLOWED
                sim.remove_card()

                # No link: a look-up waits for it, then says to open Wristbands there.
                await browser.close()
                p.db.update_settings({"wb_card_wait": "1"})
                status, reply = await p.call("POST", "/api/wristbands/lookup", bob)
                assert status == 200
                deadline = time.monotonic() + 10
                while True:
                    status, found = await p.call("GET", "/api/wristbands/lookup", bob)
                    if found["state"] != "waiting" or time.monotonic() > deadline:
                        break
                    await asyncio.sleep(0.05)
                assert found["state"] == "error"
                assert found["message"] == ("Open Wristbands on Front Desk PC to connect the "
                                            "encoder, then try again.")
    run(scenario())


def test_dashboard_key_cards_and_test_encoder_through_the_link(tmp_path):
    async def scenario():
        async with Simulator() as sim, FakeTTHotel([tt_lock(d) for d in DOORS]):
            p = local_portal(tmp_path)
            tthotel_account(p)
            house = p.db.create_department(name="Housekeeping",
                                           card_access=[{"level": "door", "door": 900}])
            p.db.update_staff(p.cy, department_id=house)
            async with p:
                # Test encoder: only through the link.
                test = f"/api/devices/{p.device}/encoder-test"
                status, reply = await p.admin_call("POST", test)
                assert status == 409
                assert reply["error"] == ("Open Wristbands on Front Desk PC to connect the "
                                          "encoder, then test it again.")
                ana = await p.sign_in(p.ana)
                browser = await FakeBrowser(p, ana, sim_url(sim)).open(p.device)
                status, reply = await p.admin_call("POST", test)
                assert status == 200 and "SN Web Socket 2.5.1" in reply["version"], reply
                assert not sim.connected and browser.calls[-1] == "CE_DisconnectComm"
                assert ("Admin", "Tested the encoder on Front Desk PC", True) in p.activity()

                # Make key card from the Staff tab, on that computer's encoder.
                status, listing = await p.admin_call("GET", "/api/keycards")
                assert [e["device_id"] for e in listing["encoders"]] == [p.device]
                status, reply = await p.admin_call("POST", f"/api/staff/{p.cy}/keycard",
                                                   {"device_id": p.device})
                assert status == 200, reply
                job_id = reply["job"]["id"]
                deadline = time.monotonic() + 10
                while True:
                    status, reply = await p.admin_call("GET", f"/api/keycards/jobs/{job_id}")
                    if reply["job"]["phase"] == "waiting_for_card":
                        break
                    assert time.monotonic() < deadline, reply
                    await asyncio.sleep(0.05)
                card = sim.place_card()
                while True:
                    status, reply = await p.admin_call("GET", f"/api/keycards/jobs/{job_id}")
                    if reply["job"]["status"] == "done":
                        break
                    assert time.monotonic() < deadline, reply
                    await asyncio.sleep(0.05)
                assert reply["job"]["cards"][0]["card_number"] == card
                assert [r["mac"] for r in sim.cards[card]["records"]] == ["C540E09C8C90"]
                sim.remove_card()
                await browser.close()
    run(scenario())


# ---------------------------------------------------------------- when a link closes

def test_a_link_closes_when_its_session_or_access_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LINK_CHECK_SECONDS", 0.05)

    async def scenario():
        p = local_portal(tmp_path)
        async with p:
            async def opened(staff_id):
                who = await p.sign_in(staff_id)
                return who, await FakeBrowser(p, who, None).open(p.device)

            # Signing out closes it at once.
            ana, browser = await opened(p.ana)
            await p.staff.post("/api/signout", headers=ana)
            assert await browser.wait_closed() == main.LINK_SIGNED_OUT
            await browser.close()
            # Idle sign-out.
            ana, browser = await opened(p.ana)
            device_id, _, staff_id = main.pin_sessions[ana["X-Session"]]
            main.pin_sessions[ana["X-Session"]] = (device_id, time.time() - 3600, staff_id)
            assert await browser.wait_closed() == main.LINK_SIGNED_OUT
            await browser.close()
            # The computer is switched to another encoder mode.
            ana, browser = await opened(p.ana)
            status, _ = await p.admin_call("PATCH", f"/api/devices/{p.device}",
                                           {"encoder_mode": "off"})
            assert status == 200
            assert await browser.wait_closed() == main.LINK_NOT_ALLOWED
            await browser.close()
            p.db.update_device(p.device, encoder_local=1)
            # The person's role can no longer make wristbands.
            ana, browser = await opened(p.ana)
            p.db.set_role_wristbands(p.front_desk, None)
            assert await browser.wait_closed() == main.LINK_NOT_ALLOWED
            await browser.close()
            # The computer is revoked.
            bob, browser = await opened(p.bob)
            status, _ = await p.admin_call("POST", f"/api/devices/{p.device}/revoke")
            assert status == 200
            assert await browser.wait_closed() == main.LINK_NOT_ALLOWED
            await browser.close()
            assert not main.open_links
            assert not main.encoders.relay(p.device).connected
    run(scenario())


def test_a_sign_out_during_the_handshake_doesnt_take_the_link(tmp_path, monkeypatch):
    """Access is checked again once the WebSocket is open, before it's taken as the computer's
    link: a sign-out while the handshake went out (which recheck_links can't see yet) closes the
    new link, and the computer's link in another window carries on."""
    async def scenario():
        p = local_portal(tmp_path)
        async with p:
            ana = await p.sign_in(p.ana)
            desk = await FakeBrowser(p, ana, None).open(p.device)
            bob = await p.sign_in(p.bob)
            ticket = await FakeBrowser(p, bob, None).ticket()
            prepare = main.web.WebSocketResponse.prepare

            async def signed_out_meanwhile(ws, request):
                await p.staff.post("/api/signout", headers=bob)
                return await prepare(ws, request)
            monkeypatch.setattr(main.web.WebSocketResponse, "prepare", signed_out_meanwhile)
            ws = await p.staff.ws_connect(f"/api/encoder/link?ticket={ticket}")
            msg = await asyncio.wait_for(ws.receive(), 5)
            assert msg.type == WSMsgType.CLOSE and msg.data == main.LINK_SIGNED_OUT
            await ws.close()
            monkeypatch.setattr(main.web.WebSocketResponse, "prepare", prepare)
            assert main.open_links[p.device].session == ana["X-Session"]
            assert not desk.closed.is_set() and main.encoders.relay(p.device).connected
            await desk.close()
    run(scenario())


def test_a_new_setup_code_closes_the_link_at_once(tmp_path):
    """New setup code signs the computer out: its PIN sessions end and its link closes now, not
    at the link's next check."""
    async def scenario():
        p = local_portal(tmp_path)
        async with p:
            ana = await p.sign_in(p.ana)
            browser = await FakeBrowser(p, ana, None).open(p.device)
            status, _ = await p.admin_call("POST", f"/api/devices/{p.device}/reenroll")
            assert status == 200
            assert await browser.wait_closed(timeout=main.LINK_CHECK_SECONDS / 2) == (
                main.LINK_NOT_ALLOWED)
            assert not any(dev == p.device for dev, *_ in main.pin_sessions.values())
            await browser.close()
    run(scenario())


def test_the_dashboard_note_waits_while_the_link_connects(tmp_path):
    """While the browser link is connecting, or the browser there is asking about local network
    access, the computer's note doesn't say SN Web Socket isn't answering."""
    async def scenario():
        p = local_portal(tmp_path)
        async with p:
            note = lambda: main.wristbands_note(p.db.device(p.device))   # noqa: E731
            assert "Card encoder not connected" in note()
            relay = main.encoders.relay(p.device)

            async def send(message):
                return None
            link = relay.attach(send)
            assert note() is None
            relay.feed(link, {"type": "status", "helper": None})
            assert note() is None
            relay.feed(link, {"type": "status", "helper": "blocked"})
            assert "blocking" in note()
            relay.feed(link, {"type": "status", "helper": "unreachable"})
            assert "SN Web Socket isn't answering" in note()
            relay.feed(link, {"type": "status", "helper": "ready"})
            assert note() is None
            relay.detach(link)
    run(scenario())


def test_a_window_takes_the_link_back_only_when_asked():
    """The staff app takes a link another window took over back only through Use it here, never
    by itself (on becoming visible, say), which would fail a card being made in the other one."""
    with open(os.path.join(os.path.dirname(__file__), "..", "app", "staff", "app.js")) as f:
        source = f.read()
    assert len(re.findall(r"\bwbLinkResume\s*\(", source)) == 1   # its definition, never called
    assert source.count('action: ["Use it here", wbLinkResume]') == 1
    assert len(re.findall(r"\bwbLinkResume\b", source)) == 3      # and a comment


def test_a_link_that_floods_is_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LINK_MESSAGES_PER_MINUTE", 5)

    async def scenario():
        p = local_portal(tmp_path)
        async with p:
            ana = await p.sign_in(p.ana)
            browser = await FakeBrowser(p, ana, None).open(p.device)   # sends one status
            for _ in range(10):
                if browser.ws.closed:
                    break
                await browser.ws.send_json({"type": "status", "helper": "ready"})
            assert await browser.wait_closed() == main.LINK_TOO_BUSY
            await browser.close()
    run(scenario())


# ---------------------------------------------------------------- the relay itself

def test_relay_only_sends_the_runners_requests_and_matches_answers():
    async def scenario():
        relay = LocalRelay(7, lambda device_id: {7: "Desk"}[device_id])
        socket = RelaySocket(relay, timeout=0.5)
        assert relay.url == "local:7" and encoder.local_device("local:7") == 7
        assert encoder.local_device("local:0") is None
        assert encoder.local_device("ws://local:7") is None
        # No link: nothing to send on.
        try:
            await socket.call("CE_Beep")
            raise AssertionError("sent")
        except EncoderError as err:
            assert err.code == NO_LINK
        assert relay.summary() == "Open Wristbands on Desk to connect the encoder"
        sent: list[dict] = []

        async def send(message):
            sent.append(message)
        link = relay.attach(send)
        # Anything but the runner's own requests is refused before it's sent.
        for function in ("getSerialPortList", "CE_ReadRecord", "eval"):
            try:
                await socket.exchange(function)
                raise AssertionError("sent")
            except EncoderError as err:
                assert err.code == 2
        assert sent == []
        # An answer to another request, or from an old link, doesn't count; the right one does.
        task = asyncio.create_task(socket.call("CE_Beep"))
        await asyncio.sleep(0.01)
        request = sent[-1]["request"]
        assert sent[-1]["type"] == "call" and request["function"] == "CE_Beep"
        relay.feed(link, {"type": "reply", "reply": {"requestId": "other", "errorCode": 0}})
        relay.feed(link + 1, {"type": "reply", "reply": {**request, "errorCode": 0}})
        relay.feed(link, "not a dict")
        relay.feed(link, {"type": "reply", "reply": ["not", "a", "dict"]})
        await asyncio.sleep(0.01)
        assert not task.done()
        relay.feed(link, {"type": "reply", "reply": {**request, "errorCode": 0,
                                                     "snWebSocketVersion": "2.5.1"}})
        assert (await task)["errorCode"] == 0
        assert relay.helper == "ready" and relay.helper_version == "2.5.1"
        # A bare answer (no envelope) is taken too, and SN Web Socket's own errors match by
        # function.
        task = asyncio.create_task(socket.exchange("CE_GetVersion"))
        await asyncio.sleep(0.01)
        relay.feed(link, {"function": "CE_GetVersion", "errorCode": -3})
        assert (await task)[0] == -3
        # The browser says SN Web Socket isn't there: the request fails at once.
        task = asyncio.create_task(socket.call("CE_GetCardID"))
        await asyncio.sleep(0.01)
        relay.feed(link, {"type": "status", "helper": "blocked"})
        try:
            await task
            raise AssertionError("answered")
        except EncoderError as err:
            assert err.code == UNREACHABLE
        assert "blocking the encoder" in relay.summary()
        relay.feed(link, {"type": "status", "helper": "who knows", "version": "x" * 500})
        assert relay.helper == "blocked" and len(relay.helper_version) == 40
        relay.feed(link, {"type": "status", "version": None})       # says nothing new
        assert relay.helper == "blocked"
        relay.feed(link, {"type": "status", "helper": None})        # connecting again
        assert relay.helper is None
        assert relay.summary() == "Wristbands is open on Desk; checking SN Web Socket there"
        # No answer in time.
        try:
            await socket.call("CE_Beep")
            raise AssertionError("answered")
        except EncoderError as err:
            assert err.code == NO_REPLY
        # The link closes mid-request.
        task = asyncio.create_task(socket.call("CE_Beep"))
        await asyncio.sleep(0.01)
        relay.detach(link + 5)            # not this link: nothing happens
        assert relay.connected and not task.done()
        relay.detach(link)
        try:
            await task
            raise AssertionError("answered")
        except EncoderError as err:
            assert err.code == NO_LINK
        assert not relay.connected and relay.helper is None

        # A link that can't send (closing) is no link.
        async def broken(message):
            raise ConnectionResetError("closing")
        relay.attach(broken)
        try:
            await socket.call("CE_Beep")
            raise AssertionError("sent")
        except EncoderError as err:
            assert err.code == NO_LINK
    asyncio.run(scenario())


def test_the_runner_sends_only_relay_functions():
    """Every request the runner can make is on the list the browser forwards."""
    sent = set(re.findall(r'(?:call|exchange)\("([A-Za-z_]+)"', inspect.getsource(encoder)))
    assert sent and sent <= encoder.RELAY_FUNCTIONS == ALLOWED


# ---------------------------------------------------------------- Automatic: confirmed only

def test_automatic_never_uses_an_encoder_nobody_confirmed(tmp_path, monkeypatch):
    # 127.0.0.0/8 isn't private, so it may only be searched in this test.
    loopback = discovery.ipaddress.ip_network("127.0.0.0/8")
    monkeypatch.setattr(discovery, "PRIVATE_NETWORKS", (*discovery.PRIVATE_NETWORKS, loopback))

    async def ws_command(payload, timeout=10.0):
        return {"adapters": [{"name": "lo", "enabled": True,
                              "ipv4": [{"address": "127.0.0.1", "network_prefix": 30}]}]}
    monkeypatch.setattr(main.ha, "ws_command", ws_command)

    async def scenario():
        async with Simulator() as sim, FakeTTHotel([tt_lock(d) for d in DOORS]):
            p = Portal(tmp_path, encoder_url=None)
            tthotel_account(p)
            p.db.update_device(p.device, encoder_auto=1)
            house = p.db.create_department(name="Housekeeping",
                                           card_access=[{"level": "door", "door": 900}])
            p.db.update_staff(p.cy, department_id=house)
            main.discovery.port = sim._runner.addresses[0][1]
            async with p:
                await search_done()
                status, reply = await p.admin_call("POST", "/api/wristbands/encoders/find")
                assert status == 200 and reply["discovery"]["helpers"][0]["ip"] == "127.0.0.1"
                # The only helper found, not confirmed: no Wristbands, no dashboard key cards,
                # no test, and nothing ever sent to it.
                ana = await p.sign_in(p.ana)
                status, _ = await p.call("POST", "/api/wristbands/jobs", ana, day_pass())
                assert status == 403
                status, reply = await p.admin_call("POST", f"/api/staff/{p.cy}/keycard", {})
                assert status == 400
                status, reply = await p.admin_call("POST",
                                                   f"/api/devices/{p.device}/encoder-test")
                assert status == 400
                assert list(sim.calls) == [] and not sim.connected
                # Some other address confirmed (the desk's computer, now off): still not used.
                p.db.update_device(p.device, encoder_auto_ip="127.0.0.2")
                assert main.encoder_address(p.db.device(p.device), rescan=False) is None
                status, _ = await p.call("POST", "/api/wristbands/jobs", ana, day_pass())
                assert status == 403
                assert list(sim.calls) == []
                # Confirmed: used.
                status, reply = await p.admin_call("PATCH", f"/api/devices/{p.device}", {
                    "encoder_mode": "auto", "encoder_auto_ip": "127.0.0.1"})
                assert status == 200, reply
                status, reply = await p.admin_call("POST",
                                                   f"/api/devices/{p.device}/encoder-test")
                assert status == 200, reply
                assert "GetDeviceInfo" in sim.calls
    run(scenario())


def test_a_confirmed_encoder_that_moved_isnt_offered_to_the_dashboard(tmp_path):
    """Only the confirmed address counts, even with another helper found (see test_discovery)."""
    p = Portal(tmp_path, encoder_url=None)
    p.db.update_device(p.device, encoder_auto=1, encoder_auto_ip="192.168.1.60")
    main.discovery.helpers = [helper("192.168.1.50")]
    main.discovery.scanned_at, main.discovery.networks = time.time(), ["192.168.1.0/24"]
    main.discovery._auto_at = time.monotonic()
    assert main.encoder_address(p.db.device(p.device)) is None
    assert main.dashboard_encoders() == []
