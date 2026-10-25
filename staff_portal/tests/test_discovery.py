"""Finding SN Web Socket on the network (discovery.py): which networks Home Assistant's answer
gives, the networks an admin may type, what counts as SN Web Socket's greeting, which helper an
Automatic computer uses, and real searches against the encoder simulator on 127.0.0.1.

127.0.0.0/8 isn't a private network, so the searches here add it to the networks that may be
searched (discovery.PRIVATE_NETWORKS) for the test only.
"""

import asyncio
import ipaddress

import pytest
from aiohttp import web

import discovery
import encoder_sim
from discovery import Discovery, DiscoveryError, ha_networks, parse_networks, resolve

LOOPBACK = ipaddress.ip_network("127.0.0.0/8")


def run(coro):
    return asyncio.run(coro)


def adapter(address, prefix, enabled=True, **extra):
    return {"name": "eth0", "enabled": enabled, "auto": True, "default": True,
            "ipv4": [{"address": address, "network_prefix": prefix}],
            "ipv6": [{"address": "fe80::1", "network_prefix": 64, "flowinfo": 0, "scope_id": 2}],
            **extra}


@pytest.fixture
def loopback_searchable(monkeypatch):
    monkeypatch.setattr(discovery, "PRIVATE_NETWORKS", (*discovery.PRIVATE_NETWORKS, LOOPBACK))


# ---------------------------------------------------------------- which networks

def test_networks_from_home_assistant():
    answer = {"adapters": [
        adapter("192.168.1.23", 24),
        adapter("10.0.0.5", 24, enabled=False),       # not used by Home Assistant
        adapter("8.8.4.4", 24),                       # not private
        adapter("10.1.2.3", 16),                      # too wide: the /24 around Home Assistant
        adapter("172.20.0.5", 28),                    # smaller than /24: as it is
        adapter("192.168.1.99", 24),                  # the same network again
        {"name": "odd", "enabled": True, "ipv4": [{"address": "not an address",
                                                   "network_prefix": 24},
                                                  {"address": "192.168.9.1", "network_prefix": "24"},
                                                  {"address": "192.168.9.1", "network_prefix": True},
                                                  "junk"]},
        "junk",
    ], "configured_adapters": []}
    assert [str(n) for n in ha_networks(answer)] == ["192.168.1.0/24", "10.1.2.0/24",
                                                     "172.20.0.0/28"]
    for nothing in (None, [], {}, {"adapters": "x"}, {"adapters": [adapter("192.168.1.2", 24,
                                                                           enabled="yes")]}):
        assert ha_networks(nothing) == []
    many = {"adapters": [adapter(f"192.168.{i}.2", 24) for i in range(10)]}
    assert len(ha_networks(many)) == discovery.MAX_NETWORKS


def test_networks_typed_by_an_admin():
    assert [str(n) for n in parse_networks(" 192.168.1.0/24, 10.0.5.7/24  172.16.3.0/28;")] == [
        "192.168.1.0/24", "10.0.5.0/24", "172.16.3.0/28"]
    assert [str(n) for n in parse_networks("192.168.1.50")] == ["192.168.1.50/32"]
    assert parse_networks("") == [] and parse_networks(None) == []
    for bad, words in (("192.168.0.0/16", "too big"), ("8.8.8.0/24", "private"),
                       ("192.168.1.0/33", "isn't a network"), ("fe80::/64", "isn't a network"),
                       ("hello", "isn't a network"), ("172.15.255.0/24", "private"),
                       (", ".join(f"10.0.{i}.0/24" for i in range(5)), "at most")):
        with pytest.raises(ValueError, match=words):
            parse_networks(bad)


def test_addresses_tried():
    assert [str(a) for a in discovery.addresses(ipaddress.ip_network("192.168.1.0/30"))] == [
        "192.168.1.1", "192.168.1.2"]
    assert len(discovery.addresses(ipaddress.ip_network("192.168.1.0/24"))) == 254
    assert [str(a) for a in discovery.addresses(ipaddress.ip_network("192.168.1.9/32"))] == [
        "192.168.1.9"]


def test_greeting():
    assert discovery.greeting('{"errorCode": 0, "description": "you are connection"}') == ""
    assert discovery.greeting('{"errorCode": 0, "description": "You are connection",'
                              ' "snWebSocketVersion": "2.5.1"}') == "2.5.1"
    for other in ('{"errorCode": 1, "description": "you are connection"}',
                  '{"errorCode": false, "description": "you are connection"}',
                  '{"errorCode": 0, "description": "hello"}', '{"errorCode": 0}', "[]",
                  "not json", None, b"\x00"):
        assert discovery.greeting(other) is None, other


# ---------------------------------------------------------------- which helper is used

def helper(ip):
    return {"ip": ip, "url": f"ws://{ip}:8102", "version": "", "found_at": 1.0}


def test_resolution_rules():
    one, two = helper("192.168.1.50"), helper("192.168.1.60")
    assert resolve([], None) == (None, "none")
    assert resolve([], "192.168.1.50") == (None, "missing")
    # Never one an admin hasn't confirmed, even when it's the only one found: anything on the
    # network can greet like SN Web Socket, and the encoder is sent the hotel key.
    assert resolve([one], None) == (None, "choose")
    assert resolve([one, two], None) == (None, "choose")
    # The confirmed one, wherever it is in the list.
    assert resolve([one, two], "192.168.1.60") == (two, "ok")
    assert resolve([one], "192.168.1.50") == (one, "ok")
    # The confirmed one isn't found: "not found", never whatever else answered.
    assert resolve([two], "192.168.1.50") == (None, "missing")
    assert resolve([one, helper("192.168.1.70")], "192.168.1.60") == (None, "missing")
    # Discovery.resolve() uses what was found last.
    found = Discovery(lambda: None, lambda: False)
    found.helpers = [one]
    assert found.resolve(None) == (None, "choose")
    assert found.resolve("192.168.1.50") == (one, "ok")


# ---------------------------------------------------------------- searching

class NotSNWebSocket:
    """Something else listening: a web server, and a WebSocket that greets differently."""

    async def __aenter__(self):
        app = web.Application()

        async def other(request):
            ws = web.WebSocketResponse()
            if not ws.can_prepare(request).ok:
                return web.Response(text="a printer")
            await ws.prepare(request)
            await ws.send_json({"hello": "world"})
            await ws.receive()
            return ws
        app.router.add_get("/", other)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.port = self.runner.addresses[0][1]
        return self

    async def __aexit__(self, *exc):
        await self.runner.cleanup()


def finder(port, address="127.0.0.1", prefix=30):
    """A search of the network in a Home Assistant answer, as main.py passes it on."""
    answer = {"adapters": [adapter(address, prefix)]}

    async def source():
        return ha_networks(answer)
    return Discovery(source, lambda: True, port=port, probe_timeout=0.3, greeting_timeout=1.0)


async def until_done(found: Discovery):
    for _ in range(200):
        if not found.scanning:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the search didn't finish")


def test_search_finds_sn_web_socket_without_touching_the_encoder(loopback_searchable):
    async def scenario():
        sim = encoder_sim.EncoderSim()
        url = await sim.start()
        port = int(url.rsplit(":", 1)[1])
        try:
            found = finder(port)
            state = await found.find()
            assert state["error"] is None and state["networks"] == ["127.0.0.0/30"]
            assert [(h["ip"], h["url"]) for h in state["helpers"]] == [
                ("127.0.0.1", f"ws://127.0.0.1:{port}")]
            assert state["helpers"][0]["found_at"] == state["scanned_at"] > 0
            assert not state["scanning"] and found.scans == 1
            # Only the greeting was read: nothing was asked of the encoder.
            assert list(sim.calls) == [] and not sim.connected
            for _ in range(50):
                if sim.sockets == 0:
                    break
                await asyncio.sleep(0.02)
            assert sim.sockets == 0
            # Found, but only used once it's confirmed.
            assert found.resolve(None) == (None, "choose")
            assert found.resolve("127.0.0.1")[0]["ip"] == "127.0.0.1"
        finally:
            await sim.stop()
        # It's gone: the next search says so.
        found._admin_at = float("-inf")
        state = await found.find()
        assert state["helpers"] == [] and state["error"] is None
        assert found.resolve("127.0.0.1") == (None, "missing")
    run(scenario())


def test_search_ignores_other_programs(loopback_searchable):
    async def scenario():
        async with NotSNWebSocket() as other:
            found = finder(other.port)
            assert (await found.find())["helpers"] == []
    run(scenario())


def test_problems_are_reported_never_raised():
    async def scenario():
        async def unknown():
            raise DiscoveryError("Couldn't read Home Assistant's network setting.")

        async def broken():
            raise RuntimeError("boom")

        async def nothing():
            return []
        for source, error in ((unknown, "Couldn't read Home Assistant's network setting."),
                              (broken, discovery.FAILED), (nothing, discovery.NO_NETWORKS)):
            found = Discovery(source, lambda: True)
            state = await found.find()
            assert state["error"] == error and state["helpers"] == [] and not state["scanning"]
        # A broken "is anything Automatic?" check doesn't start a search, or raise.
        found = Discovery(nothing, lambda: 1 / 0)
        assert found.scan_soon() is False and found.scans == 0
        await found.close()
    run(scenario())


def test_automatic_searches_are_throttled_and_only_when_wanted():
    async def scenario():
        wanted = [False]
        found = Discovery(lambda: asyncio.sleep(0, []), lambda: wanted[0])
        assert found.scan_soon() is False and found.scans == 0   # nothing uses Automatic
        wanted[0] = True
        assert found.scan_soon() is True and found.scans == 1
        assert found.scan_soon() is False                         # already searching
        await until_done(found)
        assert found.scan_soon() is False and found.scans == 1   # not again for 10 minutes
        found._auto_at -= discovery.AUTO_SCAN_GAP
        assert found.scan_soon() is True and found.scans == 2
        await until_done(found)
        # The dashboard's button: any time, but at most every 30 seconds.
        assert found.admin_wait() == 0
        await found.find()
        assert found.scans == 3 and 29 < found.admin_wait() <= discovery.ADMIN_SCAN_GAP
    run(scenario())
    # Without an event loop there's nothing to search from.
    found = Discovery(lambda: asyncio.sleep(0, []), lambda: True)
    assert found.scan_soon() is False and found.scans == 0
