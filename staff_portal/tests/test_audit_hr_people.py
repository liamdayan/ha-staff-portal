"""Audit fixes for people from Connecteam: the date order worked out from the start dates is
kept, so a sync where no date tells doesn't rewrite everyone's start date, and a start date
estimated from the day someone was added to Connecteam is HR's day, not UTC's.

Nothing here talks to a real Connecteam, Google, TT Hotel or Home Assistant.
"""

import asyncio

import pytest

import hr_helpers  # noqa: F401  (points Connecteam at a closed local port)
from hr import crypto, people
from hr.hrdb import HRDatabase


@pytest.fixture
def ring(tmp_path):
    keyring = crypto.Keyring(str(tmp_path / "hr"))
    assert keyring.load(create=True) == "ready"
    crypto.use(keyring)
    yield keyring
    crypto.use(None)


@pytest.fixture
def db(tmp_path, ring):
    database = HRDatabase(str(tmp_path / "hr"))
    yield database
    database.close()


def run(coro):
    return asyncio.run(coro)


def user(uid, name, start=None, created=1_700_000_000):
    u = {"id": uid, "name": name, "kiosk_code": None, "groups": ["501"], "mobile": None,
         "email": None, "created_at": created, "fields": {"77": start} if start else {}}
    u["field_names"] = {"77": "Start Date"} if start else {}
    return u


def test_month_first_dates_stay_month_first_when_no_date_tells(db):
    team = [user("1", "Amy", "03/04/2024"), user("2", "Bob", "05/06/2023"),
            user("3", "Cat", "07/08/2022"), user("4", "Zed", "10/25/2024")]
    run(people.apply_users(db, team))                  # Zed's date says month first
    assert db.person("1")["start_date"] == "2024-03-04"
    db.update_person("1", start_confirmed=1)
    run(people.apply_users(db, team[:3]))              # Zed leaves: nothing tells now
    amy = db.person("1")
    assert (amy["start_date"], amy["start_confirmed"]) == ("2024-03-04", 1)
    assert db.person("2")["start_date"] == "2023-05-06"
    assert db.conn.execute("SELECT COUNT(*) FROM audit WHERE action = "
                           "'Start date changed in Connecteam'").fetchone()[0] == 0


def test_a_new_start_field_works_its_order_out_again(db):
    run(people.apply_users(db, [user("1", "Amy", "03/04/2024"), user("4", "Zed", "10/25/2024")]))
    # Another field with nothing that tells: Connecteam's usual day first again.
    db.set_settings({"start_field": "88"})
    u = user("1", "Amy")
    u["fields"] = {"88": "03/04/2024"}
    run(people.apply_users(db, [u, user("4", "Zed")]))
    assert db.person("1")["start_date"] == "2024-04-03"


def test_an_estimated_start_date_is_hrs_day(db):
    # Added at 6:30 PM Pacific on Oct 28 (1:30 AM UTC on Oct 29).
    run(people.apply_users(db, [user("1", "Ana", created=1_761_701_400)]))
    p = db.person("1")
    assert (p["start_date"], p["start_estimated"]) == ("2025-10-28", 1)
