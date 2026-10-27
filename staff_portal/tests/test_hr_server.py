"""The HR portal's web server: what every request must get past (the tunnel, the off and locked
pages, the address, the change headers), signing in, setting up an authenticator, locks,
sessions and fresh codes for sensitive actions.

Nothing here talks to a real Connecteam, Google, TT Hotel or Home Assistant.
"""

import asyncio
import time

from hr_helpers import CHANGE, HOST, HR, READ, ct_user, next_code

import main  # noqa: E402
from hr import audit, notify, signin, totp  # noqa: E402

ANA = ct_user(101, "Ana", "Lee", code="4821", groups=[501], mobile="+1 541-555-0101")
BO = ct_user(102, "Bo", "Diaz", code="7302", groups=[501], mobile="541-555-0102")
MAX = ct_user(103, "Max", "Ray", code="99001", groups=[501], mobile="541-555-0103")
HANA = ct_user(104, "Hana", "Ito", code="55667788", groups=[502], mobile="541-555-0104")


def run(coro):
    return asyncio.run(coro)


def actions(hr: HR) -> list[str]:
    return [line["action"] for line in audit.lines(hr.hdb, 200)]


# ---------------------------------------------------------------- the guard

def test_off_means_a_plain_page_and_nothing_else(tmp_path):
    async def scenario():
        async with HR(tmp_path, enabled=False) as hr:
            for method, path in (("GET", "/"), ("GET", "/api/me"), ("POST", "/api/signin"),
                                 ("GET", "/static/hr.js")):
                status, body, resp = await hr.call(method, path, {})
                assert status == 503 and "turned off" in body
                assert resp.headers["Cache-Control"] == "no-store"
            assert hr.svc.db is None and not (tmp_path / "data" / "hr").exists()
    run(scenario())


def test_tunnel_address_and_change_headers(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(ANA)
            status, body, resp = await hr.call("GET", "/")
            assert status == 200 and "<title>Hot Lake HR</title>" in body
            csp = resp.headers["Content-Security-Policy"]
            assert "script-src 'self'" in csp and "unsafe-inline" not in csp
            assert "frame-ancestors 'none'" in csp and resp.headers["X-Frame-Options"] == "DENY"
            assert resp.headers["Referrer-Policy"] == "no-referrer"
            assert '<script src="/static/hr.js?v=' in body
            status, body, _ = await hr.call("GET", "/static/hr.js")
            assert status == 200 and "HRPortal" in body
            assert (await hr.call("GET", "/static/..%2fserver.py"))[0] == 404
            # Another address on the same port, such as the staff app's.
            status, _, _ = await hr.call("GET", "/", headers={"Host": "staff.example.com"})
            assert status == 421
            # Not through the tunnel: refused, whatever else it says.
            hr.svc.from_tunnel = lambda remote: False
            status, body, _ = await hr.call("GET", "/")
            assert status == 403 and "secure address" in body
            hr.svc.from_tunnel = lambda remote: main.from_tunnel(remote)
            # A change needs the exact Origin, X-Requested-With and Sec-Fetch-Site.
            body = {"mobile": "5415550101", "pin": "4821"}
            for missing in ("Origin", "X-Requested-With", "Sec-Fetch-Site"):
                headers = {k: v for k, v in CHANGE.items() if k != missing}
                resp = await hr.client.post("/api/signin", json=body, headers=headers)
                assert resp.status == 403, missing
            resp = await hr.client.post("/api/signin", json=body,
                                        headers={**CHANGE, "Origin": "https://evil.example"})
            assert resp.status == 403
            resp = await hr.client.get("/api/me", headers={**READ, "Sec-Fetch-Site": "cross-site"})
            assert resp.status == 403
            # Unknown routes are just not found.
            assert (await hr.call("GET", "/api/nothing"))[0] == 404
    run(scenario())


def test_locked_hr_says_so(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            hr.svc.keyring.keep_out(True)
            await hr.svc.stop()
            (tmp_path / "data" / "hr" / "outside-backups" / "master.key").unlink()
            assert hr.svc.start() == "locked" and hr.svc.status() == "locked"
            status, body, _ = await hr.call("GET", "/")
            assert status == 503 and "recovery key" in body
    run(scenario())


# ---------------------------------------------------------------- signing in

def test_sign_in_with_mobile_and_kiosk_code(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(ANA, BO)
            # Wrong PIN, unknown number, no PIN: always the same message.
            for body in ({"mobile": "541 555 0101", "pin": "0000"},
                         {"mobile": "541 555 0199", "pin": "4821"},
                         {"mobile": "541 555 0101", "pin": ""}):
                status, data, _ = await hr.call("POST", "/api/signin", body)
                assert (status, data["error"]) == (401, signin.FAILED)
            status, data, resp = await hr.call("POST", "/api/signin",
                                               {"mobile": "(541) 555-0101", "pin": "4821"})
            assert status == 200
            cookie = resp.headers["Set-Cookie"]
            assert cookie.startswith("__Host-hr=") and "HttpOnly" in cookie and "Secure" in cookie
            assert "SameSite=Strict" in cookie and "Path=/" in cookie
            token = resp.cookies[signin.COOKIE].value
            status, me, _ = await hr.call("GET", "/api/me", cookie=token)
            assert status == 200 and me["name"] == "Ana Lee" and me["role"] == "employee"
            assert me["last_signin"]["at"] is None          # the first time
            assert "Signed in" in actions(hr) and "Sign-in failed" in actions(hr)
            # The second time, home shows the one before.
            token = await hr.sign_in("101")
            status, me, _ = await hr.call("GET", "/api/me", cookie=token)
            assert me["last_signin"]["at"] and me["last_signin"]["ip"] == "127.0.0.1"
            # A sign-in name for someone without a mobile number.
            hr.hdb.update_person("102", signin_hash=None)
            hr.hdb.update_person("102", signin_name_hash=signin.name_key("bo diaz"))
            status, _, _ = await hr.call("POST", "/api/signin", {"mobile": "Bo  Diaz", "pin": "7302"})
            assert status == 200
            # Signed out: the cookie goes and the session ends.
            status, _, resp = await hr.call("POST", "/api/signout", {}, cookie=token)
            assert status == 200 and "Max-Age=0" in resp.headers["Set-Cookie"]
            assert (await hr.call("GET", "/api/me", cookie=token))[0] == 401
    run(scenario())


def test_people_who_must_use_an_authenticator_set_it_up_with_hrs_code(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(ANA, MAX)
            hr.give("103", "manager", {"501": True})
            # A manager's kiosk code alone never gets in (the default setting).
            status, data, _ = await hr.call("POST", "/api/signin", {"mobile": "5415550103", "pin": "99001"})
            assert (status, data["error"]) == (401, signin.FAILED)
            # The setup code: confirm the name, the PIN, the QR code, a code, recovery codes.
            token, _ = signin.new_setup_token(hr.hdb, "103", "qr", "test")
            status, info, _ = await hr.call("GET", f"/api/setup/{token}")
            assert status == 200 and info["name"] == "Max Ray" and info["has_code"]
            assert (await hr.call("POST", f"/api/setup/{token}/finish", {"code": "123456"}))[0] == 409
            status, data, _ = await hr.call("POST", f"/api/setup/{token}/pin", {"pin": "11111"})
            assert status == 403
            status, data, _ = await hr.call("POST", f"/api/setup/{token}/pin", {"pin": "99001"})
            assert status == 200 and data["qr"].startswith("data:image/svg+xml")
            secret = data["secret"].replace(" ", "")
            status, _, _ = await hr.call("POST", f"/api/setup/{token}/finish", {"code": "000000"})
            assert status == 403
            code = totp.code_at(secret, int(time.time() // 30))
            status, done, resp = await hr.call("POST", f"/api/setup/{token}/finish", {"code": code})
            assert status == 200 and len(done["recovery_codes"]) == 8
            assert resp.cookies[signin.COOKIE].value
            assert (await hr.call("GET", f"/api/setup/{token}"))[0] == 404     # used: gone
            # Now the code is needed, and each code works once.
            status, _, _ = await hr.call("POST", "/api/signin", {"mobile": "5415550103", "pin": "99001",
                                                                 "code": code})
            assert status == 401
            hr.hdb.update_person("103", failed=0)
            await hr.sign_in("103", code=next_code(hr, "103", secret))
            # A recovery code works once, tells HR, and asks for a new phone.
            hr.give("101", "hr_admin")
            await hr.sign_in("103", code=done["recovery_codes"][0])
            assert hr.hdb.person("103")["totp_redo"]
            assert notify.inbox(hr.hdb, "101")[0]["title"] == "Max Ray signed in with a recovery code."
            status, _, _ = await hr.call("POST", "/api/signin", {
                "mobile": "5415550103", "pin": "99001", "code": done["recovery_codes"][0]})
            assert status == 401
    run(scenario())


def test_locks_after_wrong_tries_and_hr_unlocks(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(ANA, BO)
            hr.give("102", "hr_admin")
            hr.db.update_settings({})   # nothing else
            token = await hr.sign_in("101")
            hr.svc.throttle.clear()
            for _ in range(5):
                await hr.call("POST", "/api/signin", {"mobile": "5415550101", "pin": "1111"})
                hr.svc.throttle.clear()   # per-address limits aside: this is about Ana's lock
            assert signin.locked(hr.hdb.person("101"))
            status, _, _ = await hr.call("POST", "/api/signin", {"mobile": "5415550101", "pin": "4821"})
            assert status == 401                             # right PIN, but locked
            assert (await hr.call("GET", "/api/me", cookie=token))[0] == 200   # still signed in there
            assert "Locked for 15 minutes" in actions(hr)
            # An HR admin unlocks Ana (a sensitive action: a fresh PIN first).
            hr.db.update_settings({})
            hr.hdb.set_settings({"totp_policy": "nobody"})
            admin = await hr.sign_in("102")
            hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = 0")
            hr.hdb.conn.commit()
            status, data, _ = await hr.call("POST", "/api/people/101/unlock", {}, cookie=admin)
            assert (status, data["error"], data["method"]) == (401, "reconfirm", "pin")
            await hr.confirm(admin, "102")
            status, _, _ = await hr.call("POST", "/api/people/101/unlock", {}, cookie=admin)
            assert status == 200 and not signin.locked(hr.hdb.person("101"))
            await hr.sign_in("101")
    run(scenario())


def test_too_many_failures_from_one_address_slow_down(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(ANA)
            for _ in range(signin.ADDRESS_LIMIT):
                await hr.call("POST", "/api/signin", {"mobile": "5415550199", "pin": "1111"})
            status, data, _ = await hr.call("POST", "/api/signin", {"mobile": "5415550101", "pin": "4821"})
            assert status == 429 and data["error"] == signin.SLOW_DOWN
    run(scenario())


def test_sessions_end_with_access_and_kiosk_code_changes(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(ANA, BO)
            tokens = [await hr.sign_in("101") for _ in range(6)]
            assert (await hr.call("GET", "/api/me", cookie=tokens[0]))[0] == 401   # 5 at most
            status, acc, _ = await hr.call("GET", "/api/account", cookie=tokens[-1])
            assert len(acc["sessions"]) == 5 and sum(s["current"] for s in acc["sessions"]) == 1
            other = next(s for s in acc["sessions"] if not s["current"])
            assert (await hr.call("DELETE", f"/api/account/sessions/{other['id']}", cookie=tokens[-1]))[0] == 200
            # Someone else's session can't be ended this way.
            bo = await hr.sign_in("102")
            bo_session = hr.hdb.conn.execute("SELECT id FROM sessions WHERE person_id = '102'").fetchone()[0]
            assert (await hr.call("DELETE", f"/api/account/sessions/{bo_session}", cookie=tokens[-1]))[0] == 404
            # A new kiosk code in Connecteam ends Ana's sessions.
            await hr.add_people(ct_user(101, "Ana", "Lee", code="1357", groups=[501],
                                        mobile="+1 541-555-0101"), BO)
            assert (await hr.call("GET", "/api/me", cookie=tokens[-1]))[0] == 401
            assert (await hr.call("GET", "/api/me", cookie=bo))[0] == 200
            # Sign out everywhere.
            status, data, _ = await hr.call("POST", "/api/signout-everywhere", {}, cookie=bo)
            assert status == 200 and data["ended"] == 1
            assert (await hr.call("GET", "/api/me", cookie=bo))[0] == 401
    run(scenario())


def test_my_authenticator_turn_on_new_phone_off(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(ANA)
            token = await hr.sign_in("101")
            status, _, _ = await hr.call("POST", "/api/account/authenticator", {"pin": "0000"}, token)
            assert status == 403
            status, r, _ = await hr.call("POST", "/api/account/authenticator", {"pin": "4821"}, token)
            assert status == 200
            secret = r["secret"].replace(" ", "")
            code = totp.code_at(secret, int(time.time() // 30))
            status, done, _ = await hr.call("POST", "/api/account/authenticator/finish",
                                            {"token": r["token"], "code": code}, token)
            assert status == 200 and len(done["recovery_codes"]) == 8
            # Turning it off is sensitive: a fresh code first.
            hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = 0")
            hr.hdb.conn.commit()
            status, data, _ = await hr.call("DELETE", "/api/account/authenticator", None, token)
            assert (status, data["method"]) == (401, "code")
            await hr.confirm(token, "101", secret)
            assert (await hr.call("DELETE", "/api/account/authenticator", None, token))[0] == 200
            assert not hr.hdb.person("101")["totp_on"]
            # Someone whose role needs one can't add their own with only their PIN.
            hr.give("101", "manager", {})
            status, data, _ = await hr.call("POST", "/api/account/authenticator", {"pin": "4821"}, token)
            assert status == 403 and "setup code from HR" in data["error"]
    run(scenario())


def test_hr_admins_end_access_and_reset_sign_in(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(ANA, BO)
            hr.give("102", "hr_admin")
            secret = hr.enrol("102")
            admin = await hr.sign_in("102", code=next_code(hr, "102", secret))
            ana = await hr.sign_in("101")
            # Their own file: never (another HR admin or the owner does it).
            status, data, _ = await hr.call("POST", "/api/people/102/reset-signin", {}, admin)
            assert status == 403 and "own file" in data["error"]
            status, data, _ = await hr.call("POST", "/api/people/101/end-access", {}, admin)
            assert status == 200
            assert (await hr.call("GET", "/api/me", cookie=ana))[0] == 401
            assert (await hr.call("POST", "/api/signin", {"mobile": "5415550101", "pin": "4821"}))[0] == 401
            assert (await hr.call("POST", "/api/people/101/allow-access", {}, admin))[0] == 200
            await hr.sign_in("101")
            status, data, _ = await hr.call("POST", "/api/people/101/setup-code", {"kind": "link"}, admin)
            assert status == 200 and data["url"].startswith(f"https://{HOST}/setup/")
            assert (await hr.call("POST", "/api/people/101/reset-signin", {}, admin))[0] == 200
            assert notify.inbox(hr.hdb, "101")[0]["kind"] == "signin_reset"
            assert {"Ended HR access now", "Allowed HR sign-in again", "Made a setup link",
                    "Reset HR sign-in"} <= set(actions(hr))
    run(scenario())


# ---------------------------------------------------------------- review fixes (0.10.0)

async def own_phone(hr: HR, cookie: str, pin: str) -> str:
    """Add an authenticator in My account with only the PIN; returns its secret."""
    status, r, _ = await hr.call("POST", "/api/account/authenticator", {"pin": pin}, cookie)
    assert status == 200, r
    secret = r["secret"].replace(" ", "")
    status, r, _ = await hr.call("POST", "/api/account/authenticator/finish",
                                 {"token": r["token"],
                                  "code": totp.code_at(secret, int(time.time() // 30))}, cookie)
    assert status == 200, r
    return secret


def test_a_phone_added_with_only_a_kiosk_code_never_reaches_a_staff_role(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(ANA, BO, MAX, HANA)
            hr.give("104", "hr_admin")
            hsecret = hr.enrol("104")
            admin = await hr.sign_in("104", code=next_code(hr, "104", hsecret))
            # Someone who knows only Bo's kiosk code adds their own phone while he's an employee.
            bo = await hr.sign_in("102")
            secret = await own_phone(hr, bo, "7302")
            assert hr.hdb.person("102")["totp_source"] == "self"
            # HR makes Bo a manager: that phone is removed and his open session ends.
            await hr.confirm(admin, "104", hsecret)
            status, data, _ = await hr.call("PUT", "/api/settings/role",
                                            {"person_id": "102", "role": "manager"}, admin)
            assert status == 200, data
            assert not hr.hdb.person("102")["totp_on"]
            assert (await hr.call("GET", "/api/me", cookie=bo))[0] == 401
            status, _, _ = await hr.call("POST", "/api/signin", {
                "mobile": "5415550102", "pin": "7302",
                "code": totp.code_at(secret, int(time.time() // 30) + 1)})
            assert status == 401
            assert "Needs a setup code from HR" in actions(hr)
            # Even if one were left behind, the sign-in needs one from a setup code.
            signin.enable_totp(hr.hdb, "102", secret, 0, "self")
            status, _, _ = await hr.call("POST", "/api/signin", {
                "mobile": "5415550102", "pin": "7302",
                "code": totp.code_at(secret, int(time.time() // 30) + 2)})
            assert status == 401
            # HR's setup code makes it count; a new phone later keeps that.
            status, data, _ = await hr.call("POST", "/api/people/102/setup-code", {"kind": "qr"}, admin)
            token = data["url"].rsplit("/", 1)[1]
            status, data, _ = await hr.call("POST", f"/api/setup/{token}/pin", {"pin": "7302"})
            secret = data["secret"].replace(" ", "")
            status, _, _ = await hr.call("POST", f"/api/setup/{token}/finish",
                                         {"code": totp.code_at(secret, int(time.time() // 30))})
            assert status == 200 and hr.hdb.person("102")["totp_source"] == "hr"
            bo = await hr.sign_in("102", code=next_code(hr, "102", secret))   # fresh: confirmed
            await own_phone(hr, bo, "7302")
            assert hr.hdb.person("102")["totp_source"] == "hr"
            # A setup code someone made for themselves in My account isn't for the public page.
            status, r, _ = await hr.call("POST", "/api/account/authenticator", {}, bo)
            assert (await hr.call("GET", f"/api/setup/{r['token']}"))[0] == 404

            # The owner making someone an HR admin does the same.
            ana = await hr.sign_in("101")
            await own_phone(hr, ana, "4821")
            status, _ = await hr.admin_call("PUT", "/api/hr/admins", {"person_ids": ["104", "101"]})
            assert status == 200
            assert not hr.hdb.person("101")["totp_on"]
            assert (await hr.call("GET", "/api/me", cookie=ana))[0] == 401

            # So does HR changing who must use one: Max (an employee) loses his own phone when
            # it becomes everyone. HR's own, from a setup code, stays.
            mx = await hr.sign_in("103")
            await own_phone(hr, mx, "99001")
            await hr.confirm(admin, "104", hsecret)
            status, _, _ = await hr.call("PUT", "/api/settings/general",
                                         {"totp_policy": "everyone"}, admin)
            assert status == 200
            assert not hr.hdb.person("103")["totp_on"]
            assert (await hr.call("GET", "/api/me", cookie=mx))[0] == 401
            assert hr.hdb.person("104")["totp_on"]
            assert (await hr.call("GET", "/api/me", cookie=admin))[0] == 200
    run(scenario())


def test_ending_access_stops_setup_codes_made_before(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(BO, HANA)
            hr.give("104", "hr_admin")
            hsecret = hr.enrol("104")
            admin = await hr.sign_in("104", code=next_code(hr, "104", hsecret))
            await hr.confirm(admin, "104", hsecret)
            status, data, _ = await hr.call("POST", "/api/people/102/setup-code", {"kind": "link"}, admin)
            link = data["url"].rsplit("/", 1)[1]
            # Bo also started one himself in My account (15 minutes).
            bo = await hr.sign_in("102")
            mine, _ = signin.new_setup_token(hr.hdb, "102", "qr", "102")
            assert (await hr.call("POST", "/api/people/102/end-access", {}, admin))[0] == 200
            for token in (link, mine):
                assert (await hr.call("GET", f"/api/setup/{token}"))[0] == 404
                assert (await hr.call("POST", f"/api/setup/{token}/pin", {"pin": "7302"}))[0] == 404
            assert hr.hdb.person("102")["access_ended"] == 1
            assert (await hr.call("GET", "/api/me", cookie=bo))[0] == 401
            # A setup code HR makes afterwards is HR allowing them back.
            status, data, _ = await hr.call("POST", "/api/people/102/setup-code", {"kind": "qr"}, admin)
            assert status == 200 and hr.hdb.person("102")["access_ended"] == 0
    run(scenario())


def test_a_wrong_authenticator_code_takes_as_long_as_a_pin_check(tmp_path, monkeypatch):
    dummies = []
    real = signin.dummy_check
    monkeypatch.setattr(signin, "dummy_check", lambda code: dummies.append(code) or real(code))

    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(MAX)
            hr.give("103", "manager", {"501": True})
            hr.enrol("103")
            status, _, _ = await hr.call("POST", "/api/signin",
                                         {"mobile": "5415550103", "pin": "0000", "code": "000000"})
            assert status == 401 and len(dummies) == 1
    run(scenario())


def test_the_authenticator_code_is_a_second_step_after_the_right_pin(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            await hr.add_people(MAX)
            hr.give("103", "manager", {"501": True})
            secret = hr.enrol("103")
            pin = hr.pins["103"]
            # The right PIN alone: asked for the code, no session, not a wrong try.
            status, data, resp = await hr.call("POST", "/api/signin", {"mobile": "5415550103", "pin": pin})
            assert status == 200 and data == {"need_code": True}
            assert signin.COOKIE not in resp.cookies and hr.hdb.person("103")["failed"] == 0
            # A wrong PIN alone: refused as always, and counted.
            status, data, _ = await hr.call("POST", "/api/signin", {"mobile": "5415550103", "pin": "0000"})
            assert status == 401 and "need_code" not in str(data)
            assert hr.hdb.person("103")["failed"] == 1
            # Then with the code: signed in.
            status, _, resp = await hr.call("POST", "/api/signin", {
                "mobile": "5415550103", "pin": pin, "code": next_code(hr, "103", secret)})
            assert status == 200 and resp.cookies[signin.COOKIE].value
    run(scenario())


def test_hrs_reasons_for_changing_points_and_end_dates_stay_with_hr(tmp_path, monkeypatch):
    import point_system
    from test_hr_discipline import day, issued, setup
    from hr import discipline
    pol, _ = point_system.install(monkeypatch)
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr)
            wid = await issued(hr, s)
            assert (await hr.call("POST", f"/api/writeups/{wid}/send", {}, s["max"]))[0] == 200
            iid = discipline.get_writeup(hr.hdb, wid)["incident_id"]
            status, r, _ = await hr.call("POST", f"/api/incidents/{iid}/adjust",
                                         {"points": 1, "reason": "HRSECRET-ADJ medical"}, s["hale"])
            assert status == 200, r
            status, r, _ = await hr.call("POST", f"/api/writeups/{wid}/ends",
                                         {"ends_on": day(hr, 5), "reason": "HRSECRET-ENDS"}, s["hale"])
            assert status == 200, r
            for who in ("max", "dee", "hana"):
                _, inc, _ = await hr.call("GET", f"/api/incidents/{iid}", cookie=s[who])
                _, w, _ = await hr.call("GET", f"/api/writeups/{wid}", cookie=s[who])
                notes = [h["note"] for h in inc["history"] + w["history"]]
                whats = " ".join(h["what"] for h in inc["history"] + w["history"])
                assert "Points changed by HR to 1" in whats and "stops counting" in whats
                hr_sees = who == "hana"
                assert ("HRSECRET-ADJ medical" in notes) is hr_sees, (who, notes)
                assert ("HRSECRET-ENDS" in notes) is hr_sees, (who, notes)
    run(scenario())


def test_approving_with_other_points_than_the_rules_give_needs_a_fresh_code(tmp_path, monkeypatch):
    import point_system
    from test_hr_discipline import record, setup
    from hr import discipline
    pol, _ = point_system.install(monkeypatch)
    pol.publish()

    async def scenario():
        async with HR(tmp_path) as hr:
            s = await setup(hr, confirmed=False)
            first = (await record(hr, s))["incident"]
            second = (await record(hr, s))["incident"]
            _, view, _ = await hr.call("GET", f"/api/incidents/{first}", cookie=s["hana"])
            computed = view["points_if_approved"]
            other = computed + 1
            # Other points, with a reason: a fresh code (a PIN without an authenticator) first.
            status, r, _ = await hr.call("POST", f"/api/incidents/{first}/approve",
                                         {"points": other, "reason": "Witnesses disagree."}, s["hana"])
            assert (status, r["error"], r["method"]) == (401, "reconfirm", "pin")
            assert discipline.get_incident(hr.hdb, first)["status"] == "with_hr"
            hr.enrol("405")
            status, r, _ = await hr.call("POST", f"/api/incidents/{first}/approve",
                                         {"points": other, "reason": "Witnesses disagree."}, s["hana"])
            assert (status, r["method"]) == (401, "code")
            # The rules' own points, typed or left blank, need none.
            status, r, _ = await hr.call("POST", f"/api/incidents/{first}/approve",
                                         {"points": computed}, s["hana"])
            assert status == 200, r
            assert discipline.get_incident(hr.hdb, first)["final_points"] == computed
            _, view, _ = await hr.call("GET", f"/api/incidents/{second}", cookie=s["hana"])
            computed = view["points_if_approved"]
            # Once confirmed, other points go through.
            hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = ? WHERE person_id = '405'",
                                (time.time(),))
            hr.hdb.conn.commit()
            status, r, _ = await hr.call("POST", f"/api/incidents/{second}/approve",
                                         {"points": computed + 1, "reason": "Witnesses disagree."},
                                         s["hana"])
            assert status == 200, r
            inc = discipline.get_incident(hr.hdb, second)
            assert (inc["status"], inc["final_points"], inc["hr_points"]) == \
                ("approved", computed + 1, computed + 1)
    run(scenario())
