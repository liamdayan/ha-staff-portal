"""The dashboard's live updates: it polls api/state?v= while it's open. Unchanged, the answer is
just {"unchanged": true}; a change gives the whole state with a new version. What changes by
itself all the time (request counts, check times, when people were last seen within 5 minutes)
doesn't count as a change. A poll never makes the add-on ask Connecteam, TT Hotel or Home
Assistant anything.
"""

import time

from test_encoder_dashboard import PAT, as_user, call, dashboard_portal, ingress_client
from test_wristbands_server import Portal, run

import main  # noqa: E402


async def poll(p: Portal, v: str = "") -> dict:
    status, reply = await p.admin_call("GET", f"/api/state?v={v}")
    assert status == 200, reply
    return reply


def test_unchanged_until_something_changes(tmp_path):
    async def scenario():
        async with Portal(tmp_path, encoder_url=None) as p:
            status, full = await p.admin_call("GET", "/api/state")
            assert status == 200 and full["v"] and full["staff"]
            v = full["v"]
            assert await poll(p, v) == {"v": v, "unchanged": True, "activity": full["activity"]}
            # Another admin renames someone: the next poll gets everything, with a new version.
            status, _ = await p.admin_call("PATCH", f"/api/staff/{p.ana}", {"name": "Ana Maria"})
            assert status == 200
            changed = await poll(p, v)
            assert "unchanged" not in changed and changed["v"] != v
            assert any(m["name"] == "Ana Maria" for m in changed["staff"])
            assert changed["activity"] and changed["activity"] != full["activity"]   # it was logged
            assert (await poll(p, changed["v"]))["unchanged"] is True
            # A version the page doesn't have (or none) gets the whole state.
            assert "staff" in await poll(p, "nope") and "staff" in await poll(p)
    run(scenario())


def test_what_changes_by_itself_isnt_a_change(tmp_path, monkeypatch):
    async def scenario():
        async with Portal(tmp_path, encoder_url=None) as p:
            main.tunnel_seen["203.0.113.9"] = (time.time() - 60, 3)
            try:
                v = (await poll(p))["v"]
                monkeypatch.setattr(main.connecteam, "checked_at", time.time())
                main.tunnel_seen["203.0.113.9"] = (time.time(), 40)   # Cloudflare's traffic
                assert (await poll(p, v))["unchanged"] is True
            finally:
                main.tunnel_seen.pop("203.0.113.9", None)
            v = (await poll(p))["v"]
            # Someone signs in for the first time: that shows ("last signed in").
            p.db.update_staff(p.bob, last_seen=time.time())
            v2 = (await poll(p, v))["v"]
            assert v2 != v
            # ...and while they keep using it, a minute later isn't a change.
            p.db.update_staff(p.bob, last_seen=(time.time() // 300) * 300 + 1)
            v3 = (await poll(p))["v"]
            p.db.update_staff(p.bob, last_seen=(time.time() // 300) * 300 + 61)
            assert (await poll(p, v3))["unchanged"] is True
    run(scenario())


def test_a_poll_asks_no_one_outside(tmp_path, monkeypatch):
    """Wristbands is on (so the full read checks Home Assistant's time zone now and then), and
    every way out to Home Assistant, Connecteam and TT Hotel is counted."""
    async def scenario():
        async with Portal(tmp_path, encoder_url=None) as p:
            p.db.update_settings({"connecteam_enabled": "1", "connecteam_client_id": "c",
                                  "connecteam_client_secret": "s"})
            calls = []

            def counted(name):
                async def out(*args, **kwargs):
                    calls.append(name)
                    raise main.HomeAssistantError("not here")
                return out
            for obj, names in ((main.ha, ("config", "_request", "_core_request", "ws_command")),
                               (main.connecteam, ("_request", "_new_access_token")),
                               (main.tthotel, ("_request", "_token_request"))):
                for name in names:
                    monkeypatch.setattr(obj, name, counted(f"{type(obj).__name__}.{name}"))
            v = (await poll(p))["v"]
            for _ in range(3):
                await poll(p, v)
                await poll(p)
            assert calls == []
            # The full read (when the page opens or after a save) may: the time zone is due.
            await p.admin_call("GET", "/api/state")
            assert calls == ["HomeAssistant.config"]
    run(scenario())


def test_the_poll_trusts_who_is_an_admin_for_longer(tmp_path, monkeypatch):
    """Through ingress, the poll alone asks Home Assistant who the admins are at most every
    10 minutes; everything else still checks every minute."""
    async def scenario():
        p = dashboard_portal(tmp_path, monkeypatch)
        ages = []

        async def is_admin(user_id, max_age=60.0):
            ages.append(max_age)
            return user_id in p.admins
        monkeypatch.setattr(main.ha, "is_admin", is_admin)
        async with p:
            client = await ingress_client()
            try:
                assert (await call(client, "GET", "/api/state?v=x", as_user(PAT)))[0] == 200
                assert (await call(client, "GET", "/api/state", as_user(PAT)))[0] == 200
                assert (await call(client, "GET", "/api/activity?v=x", as_user(PAT)))[0] == 200
            finally:
                await client.close()
        assert ages == [main.POLL_ADMIN_SECONDS, 60.0, 60.0]
    run(scenario())
