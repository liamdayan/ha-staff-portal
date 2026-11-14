"""The Door activity report (app/reports/door_activity/): its filters, dates in the property's
time zone, card numbers only for people who manage it, and the add-on's ctx.doors reading the
stored lock records."""

import asyncio
import shutil
import subprocess
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from report_helpers import REPORTS_DIR, FakeCtx, load_report
from test_door_activity import card, fresh_state, record
from test_wristbands_server import Portal

import doorlog  # noqa: E402
import main  # noqa: E402

FOLDER = "door_activity"
POOL, SPA = 900, 901
TZ = ZoneInfo("America/Los_Angeles")


def run(report, ctx, **params):
    return asyncio.run(report.data(ctx, {**report.params, **params}))


class StubDoors:
    """ctx.doors without the add-on: records what it was asked."""
    LIMIT = 3

    def __init__(self, rows=()):
        self.rows, self.asked = list(rows), []

    def list(self):
        return [{"id": POOL, "name": "Pool Gate"}, {"id": SPA, "name": "Spa"}]

    def records(self, since, until, **filters):
        self.asked.append((since, until, filters))
        return self.rows[:filters.get("limit", self.LIMIT)]


def row(at, ok=True, **extra):
    return {"id": 1, "at": at, "lock_id": POOL, "door": "Pool Gate", "who": "Room 12 · Smith",
            "card_id": 5, "card_number": "4455", "card_type": "overnight", "how": "Card", "ok": ok,
            "alarm": False, "alert": None, "problem": not ok, "battery": 50, **extra}


def test_it_loads_as_the_add_on_loads_it():
    report = load_report(FOLDER)
    assert report.key == "door_activity" and not report.hidden
    assert set(report.params) == {"start", "end", "door", "kind", "result", "q"}


def test_filters_reach_the_door_reader_as_the_propertys_days():
    report = load_report(FOLDER)
    today = date(2026, 11, 14)
    doors = StubDoors()
    out = run(report, FakeCtx(today=today, doors=doors))
    since, until, filters = doors.asked[0]
    assert (out["start"], out["end"]) == ("2026-11-08", "2026-11-14")      # the last 7 days
    assert since == datetime(2026, 11, 8, tzinfo=TZ).timestamp()
    assert until == datetime(2026, 11, 15, tzinfo=TZ).timestamp()
    assert filters == {"door": None, "kind": "", "result": "", "q": "", "limit": 4}
    run(report, FakeCtx(today=today, doors=doors), start="2026-11-01", end="2026-11-02",
        door=str(SPA), kind="guest", result="problems", q="  room   12 ")
    assert doors.asked[1][2] == {"door": SPA, "kind": "guest", "result": "problems", "q": "room 12",
                                 "limit": 4}
    assert [d["name"] for d in out["doors"]] == ["Pool Gate", "Spa"]


@pytest.mark.parametrize("params, message", [
    ({"start": "2026-13-01"}, "Pick a valid start date."),
    ({"start": "2026-11-10", "end": "2026-11-01"}, "The start date is after the end date."),
    ({"start": "2026-01-01", "end": "2026-11-01"}, "Pick at most 180 days."),
    ({"door": "pool"}, "Pick a door from the list."),
    ({"kind": "vip"}, "Pick a kind of card from the list."),
    ({"result": "maybe"}, "Pick a result from the list."),
])
def test_bad_filters_are_refused_in_plain_words(params, message):
    with pytest.raises(ValueError, match=message):
        run(load_report(FOLDER), FakeCtx(today=date(2026, 11, 14), doors=StubDoors()), **params)


def test_card_numbers_only_for_people_who_manage_it_and_the_newest_are_capped():
    report = load_report(FOLDER)
    today = date(2026, 11, 14)
    at = datetime(2026, 11, 14, 15, 12, tzinfo=TZ).timestamp()
    rows = [row(at), row(at - 86400, ok=False, alert="revoked"), row(at - 3 * 86400), row(at - 4 * 86400)]
    viewer = run(report, FakeCtx(today=today, doors=StubDoors(rows)))
    assert viewer["capped"] and len(viewer["records"]) == 3 and not viewer["card_numbers"]
    assert all("card_number" not in r for r in viewer["records"])
    first, second = viewer["records"][:2]
    assert (first["when"], first["who"], first["card"], first["result"]) == (
        "Today 3:12 PM", "Room 12 · Smith", "Overnight", "Opened")
    assert (second["when"], second["result"], second["alert"]) == (
        "Yesterday 3:12 PM", "Refused", "Revoked card")
    assert viewer["records"][2]["when"] == "Wed, Nov 11, 3:12 PM"
    assert viewer["counts"] == {"total": 3, "opened": 2, "refused": 1, "problems": 1}
    manager = run(report, FakeCtx(today=today, manage=True, doors=StubDoors(rows[:1])))
    assert manager["card_numbers"] and manager["records"][0]["card_number"] == "4455"
    assert not manager["capped"]


def test_the_add_ons_door_reader_filters_the_stored_records(tmp_path, monkeypatch):
    fresh_state(monkeypatch)
    p = Portal(tmp_path)
    now = time.time()
    card(p, "1111", name="Room 12 · Smith", card_type="overnight")
    card(p, "2222", name="Eve")
    card(p, "3333", name="Sam (Acme)", card_type="contractor")

    def add(lock_id, number, at, success=1):
        p.db.add_lock_records(lock_id, [doorlog.read_record(record(49, number, success, at=at), at)], now)

    add(POOL, "1111", now - 60)
    add(POOL, "2222", now - 120, success=0)
    add(SPA, "3333", now - 180)
    add(SPA, "9999", now - 240)                 # not made here
    add(POOL, "1111", now - 10 * 86400)         # before the dates asked for
    doors = main.ReportDoors()

    def who(**filters):
        return [r["who"] or r["card_number"] for r in doors.records(now - 86400, now + 1, **filters)]

    assert who() == ["Room 12 · Smith", "Eve", "Sam (Acme)", "9999"]
    assert who(door=SPA) == ["Sam (Acme)", "9999"]
    assert who(kind="guest") == ["Room 12 · Smith"]
    assert who(kind="staff") == ["Eve"] and who(kind="contractor") == ["Sam (Acme)"]
    assert who(kind="other") == ["9999"]
    assert who(result="refused") == ["Eve"] and who(result="problems") == ["Eve"]
    assert who(result="opened") == ["Room 12 · Smith", "Sam (Acme)", "9999"]
    assert who(q="room 12") == ["Room 12 · Smith"] and who(q="99") == ["9999"]
    assert who(q="%") == []                     # typed wildcards are plain text
    assert [d["id"] for d in doors.list()] and all(d["name"] for d in doors.list())


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")
def test_the_view_is_valid_javascript():
    subprocess.run(["node", "--check", f"{REPORTS_DIR}/{FOLDER}/view.js"], check=True)
