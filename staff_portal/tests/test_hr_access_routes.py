"""Every role against every HR portal route: a route is only ever reached by someone its rule
allows (signed in, the right role, someone whose file they may see, not their own file where
that's the rule, and a fresh code for sensitive actions). Then what each role actually sees in
the people list and on a person's page.

Nothing here talks to a real Connecteam, Google, TT Hotel or Home Assistant.
"""

import asyncio
import re
import time

from aiohttp import web

from hr_helpers import HR, ct_user

from hr import access, signin  # noqa: E402

ANA = ct_user(201, "Ana", "Lee", code="4821", groups=[501], mobile="541-555-0201", email="ana@example.com")
BO = ct_user(202, "Bo", "Diaz", code="7302", groups=[501], mobile="541-555-0202")
MAX = ct_user(203, "Max", "Ray", code="9900", groups=[501], mobile="541-555-0203")
DEE = ct_user(204, "Dee", "Fox", code="5566", groups=[502], mobile="541-555-0204")
HANA = ct_user(205, "Hana", "Ito", code="7788", groups=[502], mobile="541-555-0205")
ROLES = {"employee": "201", "manager": "203", "director": "204", "hr": "205"}
TARGET = "202"   # Bo: an employee in the Pub (501), which Max manages


def run(coro):
    return asyncio.run(coro)


async def setup(hr: HR):
    await hr.add_people(ANA, BO, MAX, DEE, HANA)
    hr.give("203", "manager", {"501": True})
    hr.give("204", "director")
    hr.give("205", "hr_admin")
    return {role: signin.new_session(hr.hdb, pid, "127.0.0.1", "test")
            for role, pid in ROLES.items()}


def foundation(page: dict) -> list[str]:
    """The foundation's own sections of a person's page (other parts of the portal add theirs)."""
    return [s["name"] for s in page["sections"] if s["name"] in ("details", "signin")]


def concrete(path: str, pid: str) -> str:
    """A real address for a route's pattern."""
    path = re.sub(r"\{pid[^}]*\}", pid, path)
    path = re.sub(r"\{token[^}]*\}", "A" * 32, path)
    path = re.sub(r"\{name[^}]*\}", "hr.js", path)
    return re.sub(r"\{\w+[^}]*\}", "1", path)


def expected(rule: access.Rule, role: str | None, pid: str) -> bool:
    if rule.who == "public":
        return True
    if role is None:
        return False
    if rule.who == "staff" and role == "employee":
        return False
    if rule.who == "hr" and role != "hr":
        return False
    if rule.person:
        mine = pid == ROLES[role]
        if rule.not_self and mine:
            return False
        if not mine and role == "employee":
            return False          # employees only ever see their own file
    return True


def stub_handlers(app: web.Application) -> list:
    """Replace every handler with one that only says it was reached (keeping its rule)."""
    routes = []
    for route in app.router.routes():
        if route.method == "HEAD":
            continue
        rule = access.rule_of(route.handler)

        async def reached(request):
            return web.json_response({"reached": True})
        reached.hr_rule = rule
        route._handler = reached
        routes.append((route.method, route.resource.canonical, rule))
    return routes


def test_every_role_against_every_route(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            sessions = await setup(hr)
            routes = stub_handlers(hr.client.server.app)
            assert len(routes) >= 35
            hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = ?", (time.time(),))
            hr.hdb.conn.commit()
            checked = 0
            for method, path, rule in routes:
                for role in (None, *ROLES):
                    for pid in ({TARGET, ROLES[role]} if role and rule.person else {TARGET}):
                        status, data, _ = await hr.call(method, concrete(path, pid), {},
                                                        cookie=sessions.get(role))
                        reached = isinstance(data, dict) and data.get("reached") is True
                        assert reached == expected(rule, role, pid), (method, path, role, pid, status, data)
                        if not reached and role is None and rule.who != "public":
                            assert (status, data["error"]) == (401, "signed_out")
                        checked += 1
            assert checked > 150
            # Sensitive actions need a fresh code or PIN, whoever asks.
            hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = ?", (time.time() - 600,))
            hr.hdb.conn.commit()
            sensitive = [(m, p) for m, p, r in routes if r.sensitive]
            assert len(sensitive) >= 10
            for method, path in sensitive:
                status, data, _ = await hr.call(method, concrete(path, TARGET), {}, cookie=sessions["hr"])
                assert (status, data["error"]) == (401, "reconfirm"), path
    run(scenario())


def test_roles_follow_people_live(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            sessions = await setup(hr)
            # The people list, by role.
            names = {}
            for role, cookie in sessions.items():
                status, data, _ = await hr.call("GET", "/api/people", cookie=cookie)
                assert status == 200
                names[role] = sorted(p["name"] for p in data["people"])
            assert names["employee"] == ["Ana Lee"]
            assert names["manager"] == ["Ana Lee", "Bo Diaz", "Max Ray"]       # Pub, and themselves
            assert names["director"] == ["Ana Lee", "Bo Diaz", "Dee Fox", "Max Ray"]   # not HR admins
            assert names["hr"] == ["Ana Lee", "Bo Diaz", "Dee Fox", "Hana Ito", "Max Ray"]
            # Former employees: HR only.
            hr.hdb.update_person("201", archived=1)
            status, data, _ = await hr.call("GET", "/api/people?former=1", cookie=sessions["hr"])
            assert [p["name"] for p in data["people"]] == ["Ana Lee"]
            status, data, _ = await hr.call("GET", "/api/people?former=1", cookie=sessions["manager"])
            assert data["former"] is False and "Ana Lee" not in [p["name"] for p in data["people"]]
            hr.hdb.update_person("201", archived=0)
            # A person's page: managers see the details but not contact details or sign-in.
            status, page, _ = await hr.call("GET", "/api/people/201", cookie=sessions["manager"])
            labels = {r["label"] for s in page["sections"] for r in s["rows"]}
            assert status == 200 and "Mobile" not in labels and "Start date" in labels
            assert foundation(page) == ["details"]
            status, page, _ = await hr.call("GET", "/api/people/201", cookie=sessions["hr"])
            assert foundation(page) == ["details", "signin"]
            assert "541-555-0201" in str(page)
            status, page, _ = await hr.call("GET", "/api/people/201", cookie=sessions["employee"])
            assert foundation(page) == ["details"] and "541-555-0201" in str(page)
            # HR admins see their own file only as an employee.
            status, page, _ = await hr.call("GET", "/api/people/205", cookie=sessions["hr"])
            assert foundation(page) == ["details"]
            # Bo moves to the kitchen in Connecteam: Max loses him at once.
            await hr.add_people(ANA, ct_user(202, "Bo", "Diaz", code="7302", groups=[502],
                                             mobile="541-555-0202"), MAX, DEE, HANA)
            assert (await hr.call("GET", "/api/people/202", cookie=sessions["manager"]))[0] == 404
            # Max loses the manager role: on his next click.
            hr.hdb.set_role("203", "manager", False)
            assert (await hr.call("GET", "/api/people/201", cookie=sessions["manager"]))[0] == 404
            # Bo is Max's direct report in Connecteam: a manager sees a report who is a manager.
            hr.give("203", "manager", {"501": True})
            hr.give("202", "manager", {})
            assert (await hr.call("GET", "/api/people/202", cookie=sessions["manager"]))[0] == 404
            hr.hdb.update_person("202", managers='["203"]')
            assert (await hr.call("GET", "/api/people/202", cookie=sessions["manager"]))[0] == 200
    run(scenario())


def test_hr_settings_roles_and_departments(tmp_path):
    async def scenario():
        async with HR(tmp_path) as hr:
            sessions = await setup(hr)
            hr.hdb.conn.execute("UPDATE sessions SET confirmed_at = ?", (time.time(),))
            hr.hdb.conn.commit()
            admin = sessions["hr"]
            status, s, _ = await hr.call("GET", "/api/settings", cookie=admin)
            assert status == 200 and s["general"]["totp_policy"] == "staff"
            assert {d["group"] for d in s["departments"]} >= {"501", "502"}
            assert [m["name"] for m in s["sections"]["roles"]["managers"]] == ["Max Ray"]
            status, _, _ = await hr.call("PUT", "/api/settings/role", {"person_id": "201", "role": "director"}, admin)
            assert status == 200 and hr.hdb.roles_of("201") == {"director"}
            status, _, _ = await hr.call("PUT", "/api/settings/role", {"person_id": "201", "role": "manager"}, admin)
            assert hr.hdb.roles_of("201") == {"manager"}
            # Departments: one primary manager each.
            status, _, _ = await hr.call("PUT", "/api/people/201/departments",
                                         {"groups": {"501": True, "502": False}}, admin)
            assert status == 200
            assert hr.hdb.manager_groups("201") == {"501": True, "502": False}
            assert hr.hdb.manager_groups("203") == {"501": False}
            status, _, _ = await hr.call("PUT", "/api/people/201/departments", {"groups": {"999": True}}, admin)
            assert status == 400
            # HR admins can't change their own role, and HR admins are the owner's to give.
            status, data, _ = await hr.call("PUT", "/api/settings/role", {"person_id": "205", "role": "manager"}, admin)
            assert status == 403 and "own file" in data["error"]
            status, _, _ = await hr.call("PUT", "/api/settings/role", {"person_id": "201", "role": "hr_admin"}, admin)
            assert status == 400
            status, _, _ = await hr.call("PUT", "/api/settings/general", {"totp_policy": "everyone",
                                                                          "session_minutes": "45"}, admin)
            assert status == 200 and hr.hdb.setting("totp_policy") == "everyone"
            assert hr.hdb.setting("session_minutes") == "30"      # 5 to 30
            assert (await hr.call("PUT", "/api/settings/general", {"quiet_start": "9pm"}, admin))[0] == 400
            status, r, _ = await hr.call("POST", "/api/settings/key", {"keep_out": True}, admin)
            assert status == 200 and len(r["recovery_key"]) == 24 and hr.svc.keyring.kept_out()
            status, r, _ = await hr.call("POST", "/api/settings/key", {"keep_out": False}, admin)
            assert r["recovery_key"] is None and not hr.svc.keyring.kept_out()
            # The audit log, and its check.
            status, a, _ = await hr.call("GET", "/api/audit", cookie=admin)
            assert status == 200 and {"Changed role", "Changed HR settings",
                                       "Changed a manager's departments"} <= {l["action"] for l in a["lines"]}
            status, check, _ = await hr.call("POST", "/api/audit/check", {}, admin)
            assert status == 200 and check["ok"] and check["lines"] > 5
    run(scenario())
