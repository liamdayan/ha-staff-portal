"""Audit fixes in main.py's staff routes: a control refused as "too soon" goes through when it's
tried again, wrong setup codes from anywhere can't stop a real phone being set up, and a slow
Refresh now isn't started over (and nobody else's report dropped) while the app waits for it.
Nothing here talks to Connecteam, TT Hotel or Home Assistant."""

import asyncio
import time

from test_audit_frontend import FAKE_DOM, app_part, need_node, node
from test_reports_server import go, setup, share  # noqa: F401 - setup is a fixture
from test_wristbands_server import Portal, fake_area_layout, run

import ctdata  # noqa: E402
import main  # noqa: E402


# ---------------------------------------------------------------- controls

def test_a_command_refused_as_too_soon_is_sent_when_tried_again(tmp_path, monkeypatch):
    """Unlock, then Lock straight away: refused (too soon). Lock again a moment later, as the
    message says: it reaches the lock, not ignored as a double tap."""
    sent, door = [], {"entity_id": "lock.pool_gate", "state": "locked", "attributes": {}}

    async def states(max_age=2.0):
        return {"lock.pool_gate": dict(door)}

    async def call_service(domain, service, data, refresh_token=None):
        sent.append(service)
        door["state"] = "unlocked" if service == "unlock" else "locked"
    monkeypatch.setattr(main, "area_layout", fake_area_layout)
    monkeypatch.setattr(main, "access_for", lambda *a, **k: {"lock.pool_gate": "control"})
    monkeypatch.setattr(main.ha, "states", states)
    monkeypatch.setattr(main.ha, "call_service", call_service)
    monkeypatch.setattr(main, "ENTITY_MIN_INTERVAL", 0.2)
    main.last_command.clear()
    main.last_entity_action.clear()

    async def scenario():
        p = Portal(tmp_path)
        async with p:
            ana = await p.sign_in(p.ana)
            act = lambda action: p.call("POST", "/api/action", ana,  # noqa: E731
                                        {"entity_id": "lock.pool_gate", "action": action})
            assert (await act("unlock")) == (200, {"ok": True})
            status, body = await act("lock")
            assert status == 429 and "Wait a second" in body["error"]
            await asyncio.sleep(0.25)
            assert (await act("lock")) == (200, {"ok": True})
            assert sent == ["unlock", "lock"] and door["state"] == "locked"
            # A real double tap is still ignored.
            assert (await act("lock"))[1] == {"ok": True, "unchanged": True}
            assert sent == ["unlock", "lock"]
    run(scenario())


# ---------------------------------------------------------------- setting up phones

def test_wrong_setup_codes_from_anywhere_cant_block_a_real_phone(tmp_path):
    """Wrong codes from many addresses reach the limit for everyone: wrong codes are refused
    (429), but a phone with a right code is still set up."""
    async def scenario():
        p = Portal(tmp_path)
        device_id = p.db.create_device("Pool phone", p.front_desk, None)
        code = p.db.device(device_id)["enroll_code"]
        now = time.time()
        main.failures[main.ANY_ADDRESS] = (main.ENROLL_FAILURES_ANYWHERE, now)
        async with p:
            resp = await p.staff.post("/api/enroll", json={"code": "wrong"})
            assert resp.status == 429
            resp = await p.staff.post("/api/enroll", json={"code": code})
            assert resp.status == 200 and (await resp.json())["token"]
            assert p.db.device(device_id)["status"] == "active"
            # The address that sent too many wrong codes still gets in with a right one.
            main.failures["127.0.0.1"] = (20, now)
            other = p.db.create_device("Spa phone", p.front_desk, None)
            resp = await p.staff.post("/api/enroll", json={"code": p.db.device(other)["enroll_code"]})
            assert resp.status == 200
    run(scenario())


# ---------------------------------------------------------------- Refresh now

def test_a_slow_refresh_now_isnt_started_over_by_the_apps_own_polls(setup, monkeypatch):
    """The staff app polls a pending Refresh now: past the two minutes between Refresh nows, a
    poll mustn't read Connecteam afresh again, start the report over, or drop someone else's
    report still being made."""
    p = setup
    monkeypatch.setattr(main, "REPORT_WAIT", 0.05)
    monkeypatch.setattr(main, "REPORT_FRESH_GAP", 0.2)
    monkeypatch.setattr(main, "report_fresh", [0.0])
    forgot, runs, gate = [], [], asyncio.Event()
    monkeypatch.setattr(ctdata, "forget_months", lambda: forgot.append(1))
    real = ctdata.timesheets

    async def slow(start, end):
        runs.append(start)
        await gate.wait()
        return await real(start, end)

    async def scenario():
        async with p:
            await share(p)
            ana, dee = await p.sign_in(p.ana), await p.sign_in(p.dee)
            monkeypatch.setattr(ctdata, "timesheets", slow)
            path = "/api/reports/hours?week=2026-10-19"
            assert (await p.call("GET", path, dee))[0] == 202
            assert (await p.call("GET", path + "&fresh=1", ana))[0] == 202
            assert len(forgot) == 1 and len(runs) == 2
            for _ in range(3):
                await asyncio.sleep(0.25)
                assert (await p.call("GET", path + "&fresh=1", ana))[0] == 202
            assert len(forgot) == 1 and len(runs) == 2 and len(main.report_jobs) == 2
            gate.set()
            await asyncio.sleep(0.05)
            assert (await p.call("GET", path + "&fresh=1", ana))[0] == 200
            assert (await p.call("GET", path, dee))[0] == 200
            assert len(runs) == 2 and main.report_jobs == {}
            # Once nothing is waiting, Refresh now works again; a finished report is dropped.
            await asyncio.sleep(0.25)
            assert (await p.call("GET", path + "&fresh=1", ana))[0] == 200
            assert len(forgot) == 2
    go(scenario())


@need_node
def test_the_staff_app_asks_for_refresh_now_once_then_waits_for_it():
    """rpRun sends fresh=1 with its first request only; its polls after a 202 leave it out."""
    script = FAKE_DOM + """
globalThis.setTimeout = (f) => { Promise.resolve().then(f); return 0; };
globalThis.clearTimeout = () => {};
const asked = [];
let answers = [];
const api = async (method, url) => { asked.push(url); return answers.shift(); };
const body = Object.assign(el("div"), { classList: { add() {}, remove() {} }, prepend() {} });
const report = { key: "hours" };
let rp = { open: report, seq: 0, body };
const reportViews = new Map([["hours", { render() {} }]]);
const fill = () => {}, toast = () => {}, rpHome = () => {}, rpApi = () => ({});
const rpAuthError = () => false, rpMessage = () => el("p");
""" + app_part(r"  async function rpRun\(params, fresh = false\) \{.*?\n  \}\n") + """
(async () => {
  const pending = { status: 202, data: { pending: true } };
  answers = [pending, pending, { status: 200, data: { data: {} } }];
  await rpRun({ week: "2026-10-19", none: null }, true);
  eq(asked, ["/api/reports/hours?week=2026-10-19&fresh=1", "/api/reports/hours?week=2026-10-19",
             "/api/reports/hours?week=2026-10-19"], "refresh now");
  asked.length = 0;
  answers = [pending, { status: 200, data: { data: {} } }];
  await rpRun({}, true);
  eq(asked, ["/api/reports/hours?fresh=1", "/api/reports/hours"], "no params");
  asked.length = 0;
  answers = [{ status: 200, data: { data: {} } }];
  await rpRun({ week: "2026-10-19" });
  eq(asked, ["/api/reports/hours?week=2026-10-19"], "a plain look");
})();
"""
    node(script)
