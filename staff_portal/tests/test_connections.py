"""Settings > Connections: TT Hotel and Connecteam, each with its own form and Save button, and
each folded to one line with its status and requests. The dashboard builds those lines from
/api/state, which has to carry what they need without asking TT Hotel or Connecteam anything.

Nothing here talks to a real TT Hotel or Connecteam.
"""

import json
import re
import time

from test_wristbands_server import PASSWORD, SECRET, Portal, run, tthotel_account

import main  # noqa: E402

# What each section's Save button sends: only its own fields, secrets left blank to keep them.
TT_FORM = {"tthotel_client_id": "client-2", "tthotel_client_secret": "", "tthotel_username": "hotel2",
           "tthotel_password_md5": "", "tthotel_region": "global", "tthotel_sync_hours": "12",
           "tthotel_clear_secret": "0"}
CT_FORM = {"connecteam_enabled": "1", "connecteam_client_id": "ct-client",
           "connecteam_client_secret": "", "connecteam_region": "au",
           "connecteam_poll_minutes": "90", "connecteam_clear_secret": "0",
           "connecteam_auto_staff": "0", "connecteam_auto_clock_in": "1"}


def test_each_connection_saves_on_its_own(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        tthotel_account(p)
        p.db.update_settings({"connecteam_client_secret": "ct-secret", "wb_card_wait": "45",
                              "property_name": "Hot Lake"})
        async with p:
            try:
                status, _ = await p.admin_call("PUT", "/api/settings", TT_FORM)
                assert status == 200
                s = p.db.settings()
                assert (s["tthotel_client_id"], s["tthotel_username"], s["tthotel_sync_hours"]) == (
                    "client-2", "hotel2", "12")
                # Blank keeps the saved secret and password; nothing else changes.
                assert s["tthotel_client_secret"] == SECRET and s["tthotel_password_md5"] == PASSWORD
                assert s["connecteam_client_secret"] == "ct-secret" and s["wb_card_wait"] == "45"
                assert s["wristbands_enabled"] == "1" and s["property_name"] == "Hot Lake"
                assert p.activity()[0] == ("Admin", "Updated TT Hotel settings", True)

                status, _ = await p.admin_call("PUT", "/api/settings", CT_FORM)
                assert status == 200
                s = p.db.settings()
                assert (s["connecteam_enabled"], s["connecteam_client_id"], s["connecteam_region"],
                        s["connecteam_poll_minutes"]) == ("1", "ct-client", "au", "90")
                assert s["connecteam_client_secret"] == "ct-secret"
                assert s["tthotel_client_id"] == "client-2" and s["tthotel_client_secret"] == SECRET
                assert p.activity()[0] == ("Admin", "Updated Connecteam settings", True)

                # The Wristbands tab's form no longer carries the account, so saving it keeps it.
                status, _ = await p.admin_call("PUT", "/api/settings", {
                    "wristbands_enabled": "1", "wb_card_wait": "60", "wb_lockout_staff": "1"})
                assert status == 200
                s = p.db.settings()
                assert s["wb_card_wait"] == "60" and s["tthotel_client_id"] == "client-2"
                assert s["tthotel_client_secret"] == SECRET and s["tthotel_password_md5"] == PASSWORD
                assert p.activity()[0] == ("Admin", "Updated wristband settings", True)

                # Remove the saved Client Secret and password: both go, and only those.
                status, _ = await p.admin_call("PUT", "/api/settings",
                                               {**TT_FORM, "tthotel_clear_secret": "1"})
                assert status == 200
                s = p.db.settings()
                assert s["tthotel_client_secret"] == "" and s["tthotel_password_md5"] == ""
                assert s["tthotel_client_id"] == "client-2" and s["connecteam_client_secret"] == "ct-secret"

                # A bad value in one section is refused, and saves nothing.
                status, reply = await p.admin_call("PUT", "/api/settings",
                                                   {**CT_FORM, "connecteam_region": "moon"})
                assert status == 400 and reply["error"]
                assert p.db.settings()["connecteam_region"] == "au"
            finally:
                main.connecteam.forget()   # Connecteam was turned on above
    run(scenario())


def test_settings_log_text():
    assert main.settings_log_text({"tthotel_client_id": "x", "tthotel_sync_hours": 6}) == \
        "Updated TT Hotel settings"
    assert main.settings_log_text({"connecteam_enabled": "1", "connecteam_client_secret": ""}) == \
        "Updated Connecteam settings"
    assert main.settings_log_text({"wristbands_enabled": "1", "tthotel_region": "global",
                                   "wb_card_wait": 30}) == "Updated wristband settings"
    assert main.settings_log_text({"property_name": "Lodge", "connecteam_enabled": "0"}) == \
        "Updated settings"
    assert main.settings_log_text({}) == "Updated settings"


def test_state_carries_the_closed_lines(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        tthotel_account(p)
        month = time.strftime("%Y-%m", time.gmtime())
        p.db.update_settings({"tthotel_usage": json.dumps({"month": month, "count": 1240})})
        async with p:
            status, state = await p.admin_call("GET", "/api/state")
            assert status == 200
            wb, ct, s = state["wristbands"], state["connecteam"], state["settings"]
            # "1,240 requests this month (limit 7,500)"
            assert (wb["used_month"], wb["monthly_cap"], wb["enabled"]) == (1240, 7500, True)
            assert wb["paused_until"] is None and wb["halted"] is None
            # Set up or not: the secrets are only ever "saved or not".
            assert s["tthotel_client_id"] == "client-1" and s["tthotel_username"] == "hotel"
            assert s["tthotel_client_secret_set"] == "1" and s["tthotel_password_md5_set"] == "1"
            assert not {"tthotel_client_secret", "tthotel_password_md5", "tthotel_usage",
                        "connecteam_client_secret", "connecteam_usage"} & set(s)
            assert s["connecteam_secret_set"] == "0"
            # "0 requests today (limit 2,000)", and when a pause ends.
            assert ct["used_today"] == 0 and ct["daily_cap"] == main.connecteam.daily_cap()
            assert ct["paused"] is None and ct["paused_until"] is None

            until = time.time() + 600
            main.connecteam.paused_until, main.connecteam.pause_reason = until, "Waiting a minute."
            main.tthotel.paused_until, main.tthotel.pause_reason = until, "TT Hotel asked to wait."
            try:
                state = (await p.admin_call("GET", "/api/state"))[1]
                assert state["connecteam"]["paused_until"] == until
                assert state["connecteam"]["paused"] == "Waiting a minute."
                assert state["wristbands"]["paused_until"] == until
                assert state["wristbands"]["pause_reason"] == "TT Hotel asked to wait."
            finally:
                main.connecteam.paused_until, main.connecteam.pause_reason = 0.0, None
                main.tthotel.paused_until, main.tthotel.pause_reason = 0.0, None
            # Reading the state never asks TT Hotel or Connecteam: no request was counted.
            assert main.tthotel.usage()["count"] == 1240 and main.connecteam.usage()["count"] == 0
    run(scenario())


def test_dashboard_page_has_both_connections_folded(tmp_path):
    async def scenario():
        p = Portal(tmp_path)
        async with p:
            status, page = await p.admin_call("GET", "/")
            assert status == 200
            settings = page[page.index('id="tab-settings"'):]
            wristbands = page[page.index('id="tab-wristbands"'):page.index('id="tab-layout"')]
            # Both under Settings, closed until opened.
            for name in ("conn-tthotel", "conn-connecteam"):
                tag = re.search(rf'<details[^>]*id="{name}"[^>]*>', settings)
                assert tag and " open" not in tag[0]
            tt_form = settings[settings.index('id="tt-form"'):settings.index('id="ct-form"')]
            for field in ("tthotel_client_id", "tthotel_client_secret", "tthotel_username",
                          "tthotel_password_md5", "tthotel_region", "tthotel_sync_hours",
                          "tthotel_clear_secret"):
                assert f'name="{field}"' in tt_form and f'name="{field}"' not in wristbands
            assert 'id="wb-test"' in tt_form and 'name="wristbands_enabled"' in wristbands
            ct_form = settings[settings.index('id="ct-form"'):settings.index('id="settings-form"')]
            start = settings.index('id="settings-form"')
            main_form = settings[start:settings.index("</form>", start)]
            assert 'name="connecteam_client_secret"' in ct_form and 'id="ct-test"' in ct_form
            assert 'name="property_name"' in main_form and 'name="connecteam_' not in main_form
            # The Wristbands tab points to it.
            assert 'data-open-conn="conn-tthotel"' in wristbands
    run(scenario())
