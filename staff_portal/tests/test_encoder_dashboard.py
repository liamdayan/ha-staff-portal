"""This computer (the one you're using): the dashboard's card dialogs making key cards on the card
encoder plugged into the computer the dashboard is open on, with no device set up.

The dashboard page gets a ticket (POST /api/encoder/ticket on the admin server, which needs the
dashboard's own header and a Home Assistant administrator), opens its own link (GET
/api/encoder/link, through ingress) and passes each SN Web Socket request it's sent on to the
helper on its computer, as the staff app does in This computer (USB) mode. Here a fake dashboard
does that, with encoder_sim standing in for SN Web Socket. Requests through "ingress" come from
the Supervisor's address with the signed-in user's headers, as Home Assistant sends them.

Nothing here talks to a real TT Hotel, Connecteam or Home Assistant (see test_wristbands_server).
"""

import asyncio
import json
import logging
import os
import re
import time

import aiohttp
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from test_encoder_local import ALLOWED, FakeBrowser, sim_url
from test_wristbands_server import (DOORS, FakeTTHotel, Portal, Simulator, run, tt_lock,
                                    tthotel_account, waiting_for)

import encoder  # noqa: E402
import main  # noqa: E402
from encoder import NO_LINK, NO_REPLY, UNREACHABLE, DashboardRelay  # noqa: E402

PAT, SAM, VIC = "a1" * 16, "b2" * 16, "c3" * 16   # two administrators and a plain user
APP = os.path.join(os.path.dirname(__file__), "..", "app")


def as_user(user: str, name: str = "Pat Owner", dashboard: bool = True) -> dict:
    """The headers a dashboard request carries through ingress."""
    headers = {"X-Remote-User-Id": user, "X-Remote-User-Display-Name": name}
    return {**headers, "X-Requested-With": "staff-portal"} if dashboard else headers


def dashboard_portal(tmp_path, monkeypatch) -> Portal:
    """A portal with no computer's encoder set up, Pat and Sam as Home Assistant administrators
    and Vic as a plain user."""
    p = Portal(tmp_path, encoder_url=None)
    for table in (main.dashboard_tickets, main.recent_dashboard_tickets, main.dashboard_links):
        table.clear()
    p.admins = {PAT, SAM}

    async def is_admin(user_id):
        return user_id in p.admins
    monkeypatch.setattr(main.ha, "is_admin", is_admin)
    house = p.db.create_department(name="Housekeeping",
                                   card_access=[{"level": "door", "door": 900}])
    p.db.update_staff(p.cy, department_id=house)
    return p


async def ingress_client() -> TestClient:
    """The dashboard as Home Assistant's ingress reaches it: from the Supervisor's address."""
    app = main.admin_app()

    @web.middleware
    async def from_supervisor(request, handler):
        return await handler(request.clone(remote="172.30.32.2"))
    app.middlewares.insert(0, from_supervisor)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def call(client: TestClient, method: str, path: str, headers: dict, body=None):
    resp = await client.request(method, path, headers=headers, json=body)
    text = await resp.text()
    try:
        return resp.status, json.loads(text)
    except ValueError:
        return resp.status, text


async def refused(client: TestClient, path: str, headers: dict) -> int:
    try:
        ws = await client.ws_connect(path, headers=headers)
    except aiohttp.WSServerHandshakeError as err:
        return err.status
    await ws.close()
    raise AssertionError("the link was opened")


async def job_until(client, headers, job_id, check, timeout=10.0) -> dict:
    """Poll a card job the way the dashboard does until check(job) holds."""
    deadline = time.monotonic() + timeout
    while True:
        status, reply = await call(client, "GET", f"/api/keycards/jobs/{job_id}", headers)
        assert status == 200, reply
        if check(reply["job"]):
            return reply["job"]
        assert time.monotonic() < deadline, reply["job"]
        await asyncio.sleep(0.05)


class FakeDashboard(FakeBrowser):
    """The dashboard page using This computer: a ticket, its own link, and SN Web Socket
    (encoder_sim) on the admin's computer (helper_url None: it isn't running). Requests outside
    the allow-list are dropped, as the page does. closing: what the add-on said before closing
    the link."""

    def __init__(self, client: TestClient, headers: dict, helper_url: str | None):
        super().__init__(None, None, helper_url)
        self.client = client
        self.headers = headers
        self.closing: list[str] = []

    async def ticket(self) -> str:
        status, reply = await call(self.client, "POST", "/api/encoder/ticket", self.headers)
        assert status == 200, reply
        assert reply["expires_in"] == 30 and reply["path"] == "api/encoder/link"
        return reply["ticket"]

    async def open(self, ticket: str | None = None) -> "FakeDashboard":
        self.ws = await self.client.ws_connect(
            f"/api/encoder/link?ticket={ticket or await self.ticket()}", headers=self.headers)
        assert await asyncio.wait_for(self.ws.receive_json(), 5) == {"type": "linked"}
        self.session = aiohttp.ClientSession()
        await self._connect_helper()
        self._tasks = [asyncio.create_task(self._from_addon())]
        return self

    async def _from_addon(self) -> None:
        async for msg in self.ws:
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if data.get("type") == "closing":
                self.closing.append(data.get("why"))
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


# ---------------------------------------------------------------- opening the link

def test_the_dashboard_link_needs_a_ticket_for_this_admin(tmp_path, monkeypatch):
    async def scenario():
        p = dashboard_portal(tmp_path, monkeypatch)
        async with p:
            ingress = await ingress_client()
            try:
                pat = as_user(PAT)
                # Only the dashboard itself may ask (its header), and only an administrator.
                status, _ = await call(ingress, "POST", "/api/encoder/ticket",
                                       as_user(PAT, dashboard=False))
                assert status == 403 and not main.dashboard_tickets
                status, _ = await call(ingress, "POST", "/api/encoder/ticket", as_user(VIC))
                assert status == 403 and not main.dashboard_tickets
                status, _ = await call(ingress, "POST", "/api/encoder/ticket",
                                       {"X-Requested-With": "staff-portal"})
                assert status == 403 and not main.dashboard_tickets

                # No ticket, a made-up one, another admin's, a used one, an expired one: refused.
                assert await refused(ingress, "/api/encoder/link", pat) == 401
                assert await refused(ingress, "/api/encoder/link?ticket=made-up", pat) == 401
                dash = FakeDashboard(ingress, pat, None)
                ticket = await dash.ticket()
                assert main.dashboard_tickets[ticket][0] == PAT
                assert await refused(ingress, f"/api/encoder/link?ticket={ticket}",
                                     as_user(SAM, "Sam")) == 401
                assert ticket not in main.dashboard_tickets   # good once, even for a wrong try
                ticket = await dash.ticket()
                await dash.open(ticket)
                assert main.dashboard_links[PAT].ws is not None
                assert main.encoders.find_dashboard_relay(PAT).connected
                assert await refused(ingress, f"/api/encoder/link?ticket={ticket}", pat) == 401
                ticket = await dash.ticket()
                main.dashboard_tickets[ticket] = (PAT, time.time() - 1)
                assert await refused(ingress, f"/api/encoder/link?ticket={ticket}", pat) == 401
                # Someone who isn't an administrator can't use one either (nor see the page).
                ticket = await dash.ticket()
                assert await refused(ingress, f"/api/encoder/link?ticket={ticket}",
                                     as_user(VIC, dashboard=False)) == 403
                # A plain request (not a WebSocket) with a good ticket opens nothing.
                status, _ = await call(ingress, "GET",
                                       f"/api/encoder/link?ticket={await dash.ticket()}", pat)
                assert status == 400 and main.dashboard_links[PAT].ws is not None
                # The ticket is good for 30 seconds.
                ticket = await dash.ticket()
                assert 29 < main.dashboard_tickets[ticket][1] - time.time() <= 30

                # Wristbands turned off: no ticket.
                p.db.update_settings({"wristbands_enabled": "0"})
                status, reply = await call(ingress, "POST", "/api/encoder/ticket", pat)
                assert status == 409 and "Wristbands is turned off" in reply["error"]
                p.db.update_settings({"wristbands_enabled": "1"})
                # Tickets are limited per admin; another admin isn't held up.
                monkeypatch.setattr(main, "LINK_TICKETS_PER_MINUTE", 3)
                main.recent_dashboard_tickets.clear()
                statuses = [(await call(ingress, "POST", "/api/encoder/ticket", pat))[0]
                            for _ in range(4)]
                assert statuses == [200, 200, 200, 429]
                assert (await call(ingress, "POST", "/api/encoder/ticket", as_user(SAM)))[0] == 200
                await dash.close()
            finally:
                await ingress.close()
    run(scenario())


def test_the_dashboard_page_may_reach_sn_web_socket_on_its_own_computer(tmp_path, monkeypatch):
    async def scenario():
        p = dashboard_portal(tmp_path, monkeypatch)
        async with p:
            resp = await p.admin.get("/")
            assert resp.status == 200
            assert "connect-src 'self' ws://127.0.0.1:8102 ws://localhost:8102;" in (
                resp.headers["Content-Security-Policy"])
            # Its preview of the staff app still can't.
            resp = await p.admin.get("/preview")
            assert "8102" not in resp.headers["Content-Security-Policy"]
    run(scenario())


# ---------------------------------------------------------------- cards through the link

def test_key_cards_made_revoked_and_noticed_on_this_computer(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    async def scenario():
        async with Simulator() as sim, Simulator() as sam_sim, \
                FakeTTHotel([tt_lock(d) for d in DOORS]):
            p = dashboard_portal(tmp_path, monkeypatch)
            tthotel_account(p)
            async with p:
                ingress = await ingress_client()
                pat = as_user(PAT)
                make = f"/api/staff/{p.cy}/keycard"
                try:
                    # Not linked yet: nothing starts (the dialog waits for the link first).
                    status, reply = await call(ingress, "POST", make, pat, {"encoder": "here"})
                    assert status == 409 and reply["error"] == main.DASHBOARD_NOT_LINKED
                    # No computer has an encoder, and This computer wasn't asked for.
                    status, reply = await call(ingress, "POST", make, pat, {})
                    assert status == 400 and reply["error"] == main.NO_ENCODER
                    assert not p.jobs()

                    dash = await FakeDashboard(ingress, pat, sim_url(sim)).open()
                    sam = await FakeDashboard(ingress, as_user(SAM, "Sam"),
                                              sim_url(sam_sim)).open()
                    status, reply = await call(ingress, "POST", make, pat, {"encoder": "here"})
                    assert status == 200, reply
                    job_id = reply["job"]["id"]
                    record = p.db.wb_job(job_id)
                    assert record["encoder_url"] == f"local:dashboard-{PAT}"
                    assert record["device_id"] is None and record["staff_id"] is None
                    assert (record["actor"], record["source"]) == ("Pat Owner", "dashboard")
                    await job_until(ingress, pat, job_id, waiting_for(1))
                    first = sim.place_card()
                    job = await job_until(ingress, pat, job_id, lambda j: j["status"] == "done")
                    assert job["cards"][0]["card_number"] == first
                    assert [r["mac"] for r in sim.cards[first]["records"]] == ["C540E09C8C90"]
                    sim.remove_card()
                    # Only Pat's own computer was sent anything, only the usual requests, and
                    # the encoder was let go after the job.
                    assert sam.calls == [] and not sam_sim.calls
                    assert dash.calls and set(dash.calls) <= ALLOWED
                    assert dash.calls[-1] == "CE_DisconnectComm" and not sim.connected
                    assert dash.hotel_infos > 0
                    assert "FAKEHOTELINFO" not in caplog.text   # the hotel key is never logged

                    # Made from the dashboard by Pat: the Key cards list and card log say so.
                    status, listing = await call(ingress, "GET", "/api/keycards", pat)
                    card = next(c for c in listing["cards"] if c["card_number"] == first)
                    assert (card["holder"], card["made_by"], card["source"]) == (
                        "Cy", "Pat Owner", "dashboard")
                    status, log = await call(ingress, "GET", "/api/wristbands/jobs", pat)
                    made = next(j for j in log["jobs"] if j["id"] == job_id)
                    assert (made["made_by"], made["source"]) == ("Pat Owner", "dashboard")
                    assert ("Pat Owner", "Made a key card for Cy", True) in p.activity()

                    # Revoke (card in hand) on this computer.
                    card_id = card["id"]
                    dash.calls.clear()
                    status, reply = await call(ingress, "POST", f"/api/keycards/{card_id}/revoke",
                                               pat, {"encoder": "here"})
                    assert status == 200, reply
                    job_id = reply["job"]["id"]
                    await job_until(ingress, pat, job_id, waiting_for(1))
                    sim.place_card(first)
                    await job_until(ingress, pat, job_id, lambda j: j["status"] == "done")
                    sim.remove_card()
                    assert p.db.key_card(card_id)["state"] == "revoked"
                    assert sim.cards[first]["records"] == []
                    assert set(dash.calls) <= ALLOWED

                    # A lost card notice on this computer.
                    status, reply = await call(ingress, "POST", make, pat, {"encoder": "here"})
                    job_id = reply["job"]["id"]
                    await job_until(ingress, pat, job_id, waiting_for(1))
                    lost = sim.place_card()
                    await job_until(ingress, pat, job_id, lambda j: j["status"] == "done")
                    sim.remove_card()
                    lost_id = next(c["id"] for c in p.db.key_cards("all")
                                   if c["card_number"] == lost)
                    status, _ = await call(ingress, "POST", f"/api/keycards/{lost_id}/lost", pat)
                    assert status == 200
                    status, reply = await call(ingress, "POST", f"/api/keycards/{lost_id}/notice",
                                               pat, {"encoder": "here"})
                    assert status == 200, reply
                    job_id = reply["job"]["id"]
                    assert reply["job"]["kind"] == "notice"
                    await job_until(ingress, pat, job_id, waiting_for(1))
                    spare = sim.place_card()
                    await job_until(ingress, pat, job_id, lambda j: j["status"] == "done")
                    sim.remove_card()
                    assert [c["cardNo"] for c in sim.cards[spare]["cancels"]] == [lost]
                    assert sam.calls == []

                    # Without "here", the computers' own encoders are used as before.
                    status, reply = await call(ingress, "POST", make, pat, {})
                    assert status == 400 and reply["error"] == main.NO_ENCODER
                    for page in (dash, sam):
                        await page.close()
                finally:
                    await ingress.close()
    run(scenario())


def test_this_computer_with_the_add_on_on_the_same_computer(tmp_path, monkeypatch):
    """The dashboard opened on Home Assistant's own computer (no ingress): one link, as "local"."""
    async def scenario():
        async with Simulator() as sim, FakeTTHotel([tt_lock(d) for d in DOORS]):
            p = dashboard_portal(tmp_path, monkeypatch)
            tthotel_account(p)
            async with p:
                dash = await FakeDashboard(p.admin, {"X-Requested-With": "staff-portal"},
                                           sim_url(sim)).open()
                status, reply = await p.admin_call("POST", f"/api/staff/{p.cy}/keycard",
                                                   {"encoder": "here"})
                assert status == 200, reply
                assert p.db.wb_job(reply["job"]["id"])["encoder_url"] == "local:dashboard-local"
                await job_until(p.admin, {}, reply["job"]["id"], waiting_for(1))
                sim.place_card()
                job = await job_until(p.admin, {}, reply["job"]["id"],
                                      lambda j: j["status"] == "done")
                assert job["made_by"] == "Admin"
                sim.remove_card()
                await dash.close()
    run(scenario())


# ---------------------------------------------------------------- when the link closes

def test_the_dashboard_link_closes_and_its_jobs_need_attention(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LINK_CHECK_SECONDS", 0.05)

    async def scenario():
        async with Simulator() as sim, FakeTTHotel([tt_lock(d) for d in DOORS]):
            p = dashboard_portal(tmp_path, monkeypatch)
            tthotel_account(p)
            p.db.update_settings({"wb_card_wait": "1"})
            async with p:
                ingress = await ingress_client()
                pat = as_user(PAT)
                make = f"/api/staff/{p.cy}/keycard"
                try:
                    # A newer link of Pat's (another tab or computer) takes over; it says why.
                    first = await FakeDashboard(ingress, pat, None).open()
                    second = await FakeDashboard(ingress, pat, None).open()
                    assert await first.wait_closed() == main.LINK_REPLACED
                    assert first.closing == ["replaced"]
                    assert not second.closed.is_set() and PAT in main.dashboard_links
                    await first.close()
                    # Wristbands turned off closes it.
                    p.db.update_settings({"wristbands_enabled": "0"})
                    assert await second.wait_closed() == main.LINK_NOT_ALLOWED
                    assert second.closing == ["not_allowed"]
                    await second.close()
                    p.db.update_settings({"wristbands_enabled": "1"})
                    # So does no longer being an administrator.
                    page = await FakeDashboard(ingress, pat, None).open()
                    p.admins.discard(PAT)
                    assert await page.wait_closed() == main.LINK_NOT_ALLOWED
                    await page.close()
                    p.admins.add(PAT)
                    for _ in range(100):   # the add-on lets the link go just after it closes
                        if PAT not in main.dashboard_links:
                            break
                        await asyncio.sleep(0.01)
                    assert PAT not in main.dashboard_links
                    assert not main.encoders.find_dashboard_relay(PAT).connected

                    # The page closed mid-job: the card needs attention, in this computer's words.
                    page = await FakeDashboard(ingress, pat, sim_url(sim)).open()
                    status, reply = await call(ingress, "POST", make, pat, {"encoder": "here"})
                    job_id = reply["job"]["id"]
                    await job_until(ingress, pat, job_id, waiting_for(1))
                    await page.close()
                    job = await job_until(ingress, pat, job_id,
                                          lambda j: j["status"] == "needs_attention")
                    assert job["message"] == ("The card encoder on this computer isn't "
                                              "connected. Keep this page open, then try again.")
                    # Back again: the unfinished job comes back to finish or stop first...
                    page = await FakeDashboard(ingress, pat, sim_url(sim)).open()
                    status, reply = await call(ingress, "POST", make, pat, {"encoder": "here"})
                    assert status == 409 and reply["job"]["id"] == job_id
                    assert reply["job"]["status"] == "needs_attention"
                    # ...and Try again carries on.
                    status, reply = await call(ingress, "POST",
                                               f"/api/keycards/jobs/{job_id}/retry", pat)
                    assert status == 200, reply
                    await job_until(ingress, pat, job_id, waiting_for(1))
                    sim.place_card()
                    await job_until(ingress, pat, job_id, lambda j: j["status"] == "done")
                    sim.remove_card()
                    await page.close()

                    # SN Web Socket not running on this computer: said at once.
                    page = await FakeDashboard(ingress, pat, None).open()
                    started = time.monotonic()
                    status, reply = await call(ingress, "POST", make, pat,
                                               {"encoder": "here", "mode": "add"})
                    assert status == 200, reply
                    job = await job_until(ingress, pat, reply["job"]["id"],
                                          lambda j: j["status"] == "needs_attention")
                    assert time.monotonic() - started < encoder.CALL_TIMEOUT
                    assert job["message"] == ("Can't reach SN Web Socket on this computer. Check "
                                              "that it's running, then try again.")
                    status, _ = await call(ingress, "POST",
                                           f"/api/keycards/jobs/{job['id']}/cancel", pat)
                    assert status == 200

                    # The add-on stopping closes it, saying so.
                    await main.close_dashboard_links(None)
                    assert await page.wait_closed() == main.LINK_STOPPING
                    assert page.closing == ["stopping"]
                    await page.close()
                finally:
                    await ingress.close()
    run(scenario())


def test_a_dashboard_link_that_floods_is_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LINK_MESSAGES_PER_MINUTE", 5)

    async def scenario():
        p = dashboard_portal(tmp_path, monkeypatch)
        async with p:
            ingress = await ingress_client()
            try:
                page = await FakeDashboard(ingress, as_user(PAT), None).open()
                for _ in range(10):
                    if page.ws.closed:
                        break
                    await page.ws.send_json({"type": "status", "helper": "ready"})
                assert await page.wait_closed() == main.LINK_TOO_BUSY
                await page.close()
            finally:
                await ingress.close()
    run(scenario())


# ---------------------------------------------------------------- the relay and the page

def test_dashboard_addresses_and_wording():
    async def scenario():
        assert encoder.dashboard_address(PAT) == f"local:dashboard-{PAT}"
        assert encoder.dashboard_user(f"local:dashboard-{PAT}") == PAT
        assert encoder.local_device(f"local:dashboard-{PAT}") is None
        for url in ("local:7", "local:dashboard-", "local:dashboard-a b", "ws://local:dashboard-a",
                    None, 7):
            assert encoder.dashboard_user(url) is None, url
        for user in ("", "a b", "x" * 65, "../x"):
            try:
                encoder.dashboard_address(user)
                raise AssertionError(user)
            except ValueError:
                pass
        # One runner and one link per admin, apart from the computers'.
        encoders = encoder.Encoders(None, lambda *update: None)
        runner = encoders.get(encoder.dashboard_address(PAT))
        assert runner._relay is encoders.find_dashboard_relay(PAT)
        assert encoders.get(encoder.dashboard_address(SAM))._relay is not runner._relay
        assert encoders.find_relay(0) is None
        status = runner.status()
        assert status["local"] is True and status["link"] is False
        assert status["message"] == "Not connected"
        relay = encoders.dashboard_relay(PAT)
        assert runner.explain(NO_LINK) == ("The card encoder on this computer isn't connected. "
                                           "Keep this page open, then try again.")
        assert runner.explain(NO_REPLY).startswith("SN Web Socket on this computer stopped")
        relay.helper = "blocked"
        assert runner.explain(UNREACHABLE).startswith("This browser is blocking")
        relay.helper = None
        assert runner.explain(UNREACHABLE).startswith("Can't reach SN Web Socket on this computer")
        assert runner.explain(101) == encoder.failure_message(101, "day_pass")
        assert relay.missing_message() == "Connecting to the card encoder on this computer…"
        assert isinstance(relay, DashboardRelay) and relay.name() == "this computer"
        await encoders.close_all()
        assert encoders.find_dashboard_relay(PAT) is None
    asyncio.run(scenario())


def js_object(source: str, name: str) -> dict:
    """A constant object of {"function": {"field": "type"}} in the dashboard's or staff app's
    script, as Python."""
    body = re.search(r"const " + name + r" = \{(.*?)\n  \};", source, re.S)[1]
    found = {}
    for function, fields in re.findall(r"(\w+): \{([^}]*)\}", body):
        found[function] = dict(re.findall(r"(\w+): \"(\w+)\"", fields))
    return found


def test_the_dashboard_passes_on_only_what_the_staff_app_does():
    with open(os.path.join(APP, "admin", "admin.js"), encoding="utf-8") as f:
        dashboard = f.read()
    with open(os.path.join(APP, "staff", "app.js"), encoding="utf-8") as f:
        staff = f.read()
    relayed = js_object(dashboard, "HERE_RELAYED")
    assert relayed == js_object(staff, "WB_RELAYED")
    assert set(relayed) == encoder.RELAY_FUNCTIONS
    # SN Web Socket's copy of the hotel key is left out of every answer, and nothing about a
    # request is ever written to the console (only that one was dropped).
    section = dashboard[dashboard.index("const HERE = "):dashboard.index("function hereView")]
    assert "delete reply.hotelInfo;" in section
    logged = re.findall(r"console\.\w+\((.*?)\);\n", section, re.S)
    assert len(logged) == 1 and re.sub(r'"[^"]*"|\s', "", logged[0]) == "request?:"
    # It talks only to SN Web Socket on this computer and to its own link.
    assert re.findall(r'"(ws://[^"]+)"', section) == ["ws://127.0.0.1:8102", "ws://localhost:8102"]
    # The dashboard offers This computer first, and says plainly when the page isn't secure.
    assert '[[HERE, "This computer (the one you\'re using)", hereWords()],' in dashboard
    assert "Open Home Assistant at its https address to use this computer's encoder" in dashboard
