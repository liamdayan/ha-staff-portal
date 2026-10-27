"""ctdata.py: Connecteam data for reports and tasks, read through the reports lane against the
fake Connecteam (hr_helpers.FakeConnecteam): what each call gives, caching by range, the lane's
caps, and that a refusal or a "too many requests" stops only reports while the clock checks keep
running. Nothing here talks to a real Connecteam."""

import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from hr_helpers import (FakeConnecteam, ct_activity, ct_form, ct_shift, ct_submission, ct_user,
                        fresh_connecteam)
from test_wristbands_server import Portal

import connecteam  # noqa: E402
import ctdata  # noqa: E402
import main  # noqa: E402

ZONE = ZoneInfo("America/Los_Angeles")


def at(day: str, hm: str) -> int:
    """Unix seconds for a local time at the property."""
    return int(datetime.fromisoformat(f"{day}T{hm}").replace(tzinfo=ZONE).timestamp())


@pytest.fixture(autouse=True)
def roomy_minute(request, monkeypatch):
    """Most tests ask for more than 4 things at once: the minute's cap has its own tests."""
    if "lane" not in request.node.name and "minute" not in request.node.name:
        monkeypatch.setattr(connecteam, "REPORTS_MINUTE_CAP", 1000)
        monkeypatch.setattr(connecteam, "MINUTE_CAP", 1000)


def setup(tmp_path):
    p = Portal(tmp_path)
    p.db.update_settings({"connecteam_enabled": "1", "connecteam_client_id": "client",
                          "connecteam_client_secret": "secret", "wb_time_zone": "America/Los_Angeles"})
    fresh_connecteam()
    return p


def fake():
    ct = FakeConnecteam(users=[ct_user(7, "Ana", "Ray", groups=[501]), ct_user(8, "Bob", "Lee")])
    ct.jobs = {1: [{"jobId": "j1", "title": "Front desk", "subJobs": [{"jobId": "j1a", "title": "Nights"}]}],
               30: [{"jobId": "j1", "title": "Front desk"}]}
    ct.schedulers = [{"schedulerId": 30, "name": "Lodge", "isArchived": False},
                     {"schedulerId": 31, "name": "Old", "isArchived": True}]
    ct.shifts = {30: [ct_shift("sh1", at("2026-10-19", "09:00"), at("2026-10-19", "17:00"), [7],
                               title="Morning", job="j1"),
                      ct_shift("sh2", at("2026-10-20", "09:00"), at("2026-10-20", "13:00"), [7, 8],
                               published=False),
                      ct_shift("sh3", at("2026-10-21", "22:00"), at("2026-10-22", "06:00"), [],
                               open_shift=True),
                      ct_shift("sh4", at("2026-10-30", "09:00"), at("2026-10-30", "10:00"), [8])],
                 31: [ct_shift("old", at("2026-10-19", "09:00"), at("2026-10-19", "10:00"), [8])]}
    ct.activities = {1: [ct_activity(7, shifts=[(at("2026-10-19", "08:58"), at("2026-10-19", "12:00"), "j1", "sh1"),
                                                (at("2026-10-19", "12:30"), at("2026-10-19", "17:02"), "j1a", None),
                                                (at("2026-10-26", "09:00"), None, None, None)],
                                     breaks=[(at("2026-10-19", "12:00"), at("2026-10-19", "12:30"))])],
                     2: [ct_activity(8, shifts=[(at("2026-10-18", "23:30"), at("2026-10-19", "07:30"), None, None)])]}
    return ct


def go(coro):
    return asyncio.run(coro)


def test_timesheets_from_every_clock_in_the_property_zone(tmp_path):
    p = setup(tmp_path)

    async def scenario():
        async with p, fake() as ct:
            rows = await ctdata.timesheets(date(2026, 10, 19), date(2026, 10, 25))
            return rows, ct
    rows, ct = go(scenario())
    assert [(r["name"], r["type"], r["clock_in"], r["clock_out"]) for r in rows] == [
        ("Ana Ray", "shift", "2026-10-19T08:58", "2026-10-19T12:00"),
        ("Ana Ray", "break", "2026-10-19T12:00", "2026-10-19T12:30"),
        ("Ana Ray", "shift", "2026-10-19T12:30", "2026-10-19T17:02")]   # Bob's started on the 18th
    first, brk, second = rows
    assert first == {"user_id": "7", "name": "Ana Ray", "date": "2026-10-19",
                     "clock_in": "2026-10-19T08:58", "clock_out": "2026-10-19T12:00",
                     "break_minutes": 0, "job": "Front desk", "type": "shift", "hours": 3.03,
                     "sched_start": "2026-10-19T09:00", "sched_end": "2026-10-19T17:00"}
    assert brk["break_minutes"] == 30 and brk["hours"] is None and brk["job"] == "Break"
    assert second["job"] == "Front desk: Nights" and second["sched_start"] is None
    # A day either side is asked (Connecteam's days are its own zone's), per clock.
    asked = [q for path, q in ct.asked if path.endswith("/time-activities")]
    assert {(q["startDate"], q["endDate"]) for q in asked} == {("2026-10-18", "2026-10-26")}
    assert len(asked) == 2


def test_an_open_shift_and_the_day_it_started(tmp_path):
    p = setup(tmp_path)

    async def scenario():
        async with p, fake():
            return await ctdata.timesheets("2026-10-18", "2026-10-26")
    rows = go(scenario())
    bob = [r for r in rows if r["name"] == "Bob Lee"]
    assert bob[0]["date"] == "2026-10-18" and bob[0]["hours"] == 8.0
    still_on = [r for r in rows if r["clock_out"] is None]
    assert still_on[0]["date"] == "2026-10-26" and still_on[0]["hours"] is None


def test_long_ranges_are_asked_92_days_at_most_at_a_time(tmp_path):
    p = setup(tmp_path)

    async def scenario():
        async with p, fake() as ct:
            await ctdata.timesheets(date(2026, 1, 1), date(2026, 7, 31))
            return ct
    ct = go(scenario())
    for path, q in ct.asked:
        if path == "/time-clock/v1/time-clocks/1/time-activities":
            span = date.fromisoformat(q["endDate"]) - date.fromisoformat(q["startDate"])
            assert span.days < 92
    with pytest.raises(ValueError):
        go(ctdata.timesheets(date(2026, 1, 1), date(2027, 6, 1)))
    with pytest.raises(ValueError):
        go(ctdata.timesheets(date(2026, 2, 1), date(2026, 1, 1)))


def test_shifts_one_row_per_person_with_drafts_and_open_shifts(tmp_path):
    p = setup(tmp_path)

    async def scenario():
        async with p, fake() as ct:
            return await ctdata.shifts(date(2026, 10, 19), date(2026, 10, 25)), ct
    rows, ct = go(scenario())
    assert rows == [
        {"user_id": "7", "name": "Ana Ray", "date": "2026-10-19", "start": "2026-10-19T09:00",
         "end": "2026-10-19T17:00", "job": "Front desk", "title": "Morning", "draft": False, "open": False},
        {"user_id": "7", "name": "Ana Ray", "date": "2026-10-20", "start": "2026-10-20T09:00",
         "end": "2026-10-20T13:00", "job": "", "title": "", "draft": True, "open": False},
        {"user_id": "8", "name": "Bob Lee", "date": "2026-10-20", "start": "2026-10-20T09:00",
         "end": "2026-10-20T13:00", "job": "", "title": "", "draft": True, "open": False},
        {"user_id": None, "name": "", "date": "2026-10-21", "start": "2026-10-21T22:00",
         "end": "2026-10-22T06:00", "job": "", "title": "", "draft": False, "open": True}]
    assert not any("/schedulers/31/" in path for path, _ in ct.asked)   # archived schedule


def test_shift_pages_are_all_read(tmp_path):
    p = setup(tmp_path)
    ct = fake()
    ct.shifts[30] = [ct_shift(f"x{i}", at("2026-10-19", "09:00") + i, at("2026-10-19", "10:00"), [7])
                     for i in range(1203)]

    async def scenario():
        async with p, ct:
            return await ctdata.shifts(date(2026, 10, 19), date(2026, 10, 19))
    assert len(go(scenario())) == 1203
    assert ct.count("/scheduler/v1/schedulers/30/shifts") == 3


def test_forms_and_submissions_as_plain_text(tmp_path):
    p = setup(tmp_path)
    ct = fake()
    ct.forms = [ct_form(5, "Maintenance request", [("q1", "What's wrong?", "openEnded"),
                                                   ("q2", "Where", "multipleChoice"),
                                                   ("q3", "Urgent?", "yesNo"),
                                                   ("q4", "Photos", "image")]),
                ct_form(6, "Accident report")]
    ct.submissions = {5: [
        ct_submission("s1", 5, 7, at("2026-10-20", "10:15"), [
            {"questionId": "q1", "questionType": "openEnded", "value": "Tap <b>leaks</b>",
             "wasSubmittedEmpty": False},
            {"questionId": "q2", "questionType": "multipleChoice", "wasSubmittedEmpty": False,
             "selectedAnswers": [{"multipleChoiceOptionId": "o1", "text": "Room 4"}]},
            {"questionId": "q3", "questionType": "yesNo", "selectedIndex": 0, "wasSubmittedEmpty": False},
            {"questionId": "q4", "questionType": "image", "wasSubmittedEmpty": False,
             "images": [{"url": "https://x/1.jpg"}, {"url": "https://x/2.jpg"}]}]),
        ct_submission("s2", 5, None, at("2026-10-21", "08:00"), [
            {"questionId": "q1", "questionType": "openEnded", "wasSubmittedEmpty": True}]),
        ct_submission("s0", 5, 8, at("2026-10-10", "08:00"))]}

    async def scenario():
        async with p, ct:
            forms = await ctdata.forms()
            subs = await ctdata.form_submissions(5, date(2026, 10, 19), date(2026, 10, 25))
            later = await ctdata.form_submissions(
                "5", datetime(2026, 10, 20, 12, 0, tzinfo=ZONE), date(2026, 10, 25))
            return forms, subs, later
    forms, subs, later = go(scenario())
    assert [(f["id"], f["name"]) for f in forms] == [("6", "Accident report"), ("5", "Maintenance request")]
    assert forms[1]["questions"][0] == {"id": "q1", "title": "What's wrong?", "type": "openEnded"}
    assert subs == [
        {"id": "s1", "user_id": "7", "name": "Ana Ray", "submitted_at": "2026-10-20T10:15",
         "answers": [{"question": "What's wrong?", "answer": "Tap <b>leaks</b>"},
                     {"question": "Where", "answer": "Room 4"},
                     {"question": "Urgent?", "answer": "Yes"},
                     {"question": "Photos", "answer": "2 photos"}]},
        {"id": "s2", "user_id": None, "name": "Anonymous", "submitted_at": "2026-10-21T08:00",
         "answers": [{"question": "What's wrong?", "answer": ""}]}]
    assert [s["id"] for s in later] == ["s2"]
    with pytest.raises(ValueError):
        go(ctdata.form_submissions("../users", date(2026, 10, 19), date(2026, 10, 25)))


def test_the_same_range_is_asked_again_at_most_every_ten_mins(tmp_path):
    p = setup(tmp_path)

    async def scenario():
        async with p, fake() as ct:
            path = "/time-clock/v1/time-clocks/1/time-activities"
            await ctdata.timesheets(date(2026, 10, 19), date(2026, 10, 25))
            await ctdata.timesheets(date(2026, 10, 19), date(2026, 10, 25))
            assert ct.count(path) == 1
            await ctdata.timesheets(date(2026, 10, 12), date(2026, 10, 18))   # another range
            assert ct.count(path) == 2
            for key, (value, when) in list(ctdata._cache.items()):
                ctdata._cache[key] = (value, when - ctdata.MIN_REFRESH - 1)
            await ctdata.timesheets(date(2026, 10, 19), date(2026, 10, 25))
            assert ct.count(path) == 3
            # The lists that hardly change (clocks, jobs, schedules, users) weren't asked again.
            assert ct.count("/time-clock/v1/time-clocks") == 1
            assert ct.count("/users/v1/users") == 1
    go(scenario())


def test_nothing_is_asked_until_a_report_is_opened(tmp_path):
    p = setup(tmp_path)

    async def scenario():
        async with p, fake() as ct:
            await asyncio.sleep(0.05)
            return ct
    assert go(scenario()).calls == []


def test_a_refusal_quotes_connecteam_and_stops_only_reports(tmp_path):
    p = setup(tmp_path)
    ct = fake()
    ct.refuse.add("/scheduler/v1/schedulers")
    ct.clocked_in = [7]

    async def scenario():
        async with p, ct:
            with pytest.raises(ctdata.Unavailable) as err:
                await ctdata.shifts(date(2026, 10, 19), date(2026, 10, 25))
            assert "“Missing scope”" in str(err.value)
            assert "schedule.read" in str(err.value)
            assert main.connecteam.halted is None
            await main.connecteam.check(force=True)
            assert main.connecteam.clocked_in == {"7"} and main.connecteam.last_error is None
            # Timesheets still come, without the scheduled times they'd have been linked to.
            rows = await ctdata.timesheets(date(2026, 10, 19), date(2026, 10, 25))
            assert rows and all(r["sched_start"] is None for r in rows)
    go(scenario())


def test_job_names_are_optional(tmp_path):
    p = setup(tmp_path)
    ct = fake()
    ct.refuse.add("/jobs/v1/jobs")

    async def scenario():
        async with p, ct:
            rows = await ctdata.timesheets(date(2026, 10, 19), date(2026, 10, 25))
            assert rows[0]["job"] == ""
            asked = ct.count("/jobs/v1/jobs")   # once, and once more with a fresh token
            await ctdata.timesheets(date(2026, 10, 12), date(2026, 10, 18))
            assert ct.count("/jobs/v1/jobs") == asked   # not asked again for a while
    go(scenario())


def test_too_many_requests_pauses_only_reports(tmp_path):
    p = setup(tmp_path)
    ct = fake()
    ct.too_many.add("/forms/v1/forms")
    ct.clocked_in = [8]
    ct.activities = {}

    async def scenario():
        async with p, ct:
            with pytest.raises(ctdata.Unavailable, match="reports wait"):
                await ctdata.forms()
            c = main.connecteam
            assert c.reports_paused_until > c.paused_until
            # Reports wait now, even for other data...
            with pytest.raises(ctdata.Unavailable, match="reports wait"):
                await ctdata.shifts(date(2026, 10, 19), date(2026, 10, 25))
            # ...but the clock check carries on.
            await c.check(force=True)
            assert c.clocked_in == {"8"} and c.last_error is None
    go(scenario())


def test_the_lane_caps_a_minute_and_a_day(tmp_path, monkeypatch):
    p = setup(tmp_path)

    async def scenario():
        async with p, fake() as ct:
            c = main.connecteam
            for _ in range(connecteam.REPORTS_MINUTE_CAP):
                c.reports_spend()
            with pytest.raises(connecteam.ReportsBusy) as err:
                c.reports_room()
            assert 0 < err.value.wait <= 61
            monkeypatch.setattr(ctdata, "MAX_WAIT", 0)
            with pytest.raises(ctdata.Unavailable, match="this minute"):
                await ctdata.forms()
            assert ct.calls == []
            c._reports_recent.clear()
            c._save_reports_usage({"day": c.reports_usage()["day"],
                                   "count": connecteam.REPORTS_DAILY_CAP})
            cap = connecteam.REPORTS_DAILY_CAP
            with pytest.raises(ctdata.Unavailable, match=f"{cap} Connecteam requests for today"):
                await ctdata.forms()
            # The count is kept across restarts, in its own setting.
            assert f'"count": {cap}' in main.db.settings()["reports_connecteam_usage"]
            assert ct.calls == []
    go(scenario())


def test_reports_wait_for_the_minute_when_its_short(tmp_path, monkeypatch):
    p = setup(tmp_path)
    slept = []

    async def nap(seconds):
        slept.append(seconds)
        main.connecteam._reports_recent.clear()   # the minute has passed

    async def scenario():
        async with p, fake():
            for _ in range(connecteam.REPORTS_MINUTE_CAP):
                main.connecteam.reports_spend()
            monkeypatch.setattr(ctdata.asyncio, "sleep", nap)
            return await ctdata.forms()
    assert go(scenario()) == []
    assert slept and slept[0] <= 61


def test_nothing_once_staff_access_used_half_its_share(tmp_path):
    p = setup(tmp_path)

    async def scenario():
        async with p, fake() as ct:
            c = main.connecteam
            ct.clocked_in = [7]
            c._save_usage({"day": c.usage()["day"], "count": int(c.daily_cap() * 0.5)})
            with pytest.raises(ctdata.Unavailable, match="half of its Connecteam requests"):
                await ctdata.users()
            assert ct.calls == []
            await c.check(force=True)   # the time clock still has the rest
            assert c.clocked_in == {"7"}
    go(scenario())


def test_report_requests_count_in_staff_access_total_too(tmp_path):
    p = setup(tmp_path)

    async def scenario():
        async with p, fake():
            c = main.connecteam
            before = c.usage()["count"]
            await ctdata.forms()
            # A token and the forms list, in both counts.
            assert c.reports_usage()["count"] == 2
            assert c.usage()["count"] == before + 2
    go(scenario())


def test_users_without_private_fields(tmp_path):
    p = setup(tmp_path)

    async def scenario():
        async with p, fake():
            return await ctdata.users()
    assert go(scenario()) == [{"user_id": "7", "name": "Ana Ray", "groups": ["501"]},
                              {"user_id": "8", "name": "Bob Lee", "groups": []}]


def test_changed_settings_drop_what_was_kept(tmp_path):
    p = setup(tmp_path)

    async def scenario():
        async with p, fake() as ct:
            await ctdata.forms()
            main.connecteam.forget()
            await ctdata.forms()
            assert ct.count("/forms/v1/forms") == 2
    go(scenario())


def test_dates_follow_the_property_zone_across_midnight(tmp_path):
    p = setup(tmp_path)
    p.db.update_settings({"wb_time_zone": "Pacific/Auckland"})

    async def scenario():
        async with p, fake():
            return await ctdata.timesheets(date(2026, 10, 19), date(2026, 10, 20))
    rows = go(scenario())
    # Bob's 23:30 Los Angeles start is the next afternoon in Auckland.
    bob = [r for r in rows if r["user_id"] == "8"]
    assert bob and bob[0]["date"] == "2026-10-19" and bob[0]["clock_in"].endswith("19:30")


def test_the_lane_maths_leaves_the_clock_checks_room(tmp_path):
    """Reports (15 a minute, 1,000 a day) and HR (6 and 500) fit inside Staff Access's 30 and
    5,000, and together never take the clock checks' last 10 of a minute."""
    assert (connecteam.REPORTS_MINUTE_CAP, connecteam.REPORTS_DAILY_CAP) == (15, 1000)
    assert (connecteam.MINUTE_CAP, connecteam.DAILY_CAP) == (30, 5000)
    assert connecteam.CLOCK_MINUTE_KEEP >= 10
    assert connecteam.REPORTS_MINUTE_CAP <= connecteam.MINUTE_CAP - connecteam.CLOCK_MINUTE_KEEP
    assert (connecteam.REPORTS_DAILY_CAP + connecteam.HR_DAILY_CAP
            <= connecteam.DAILY_CAP * connecteam.REPORTS_STOP_SHARE)
    p = setup(tmp_path)

    async def scenario():
        async with p, fake():
            c = main.connecteam
            for _ in range(connecteam.HR_MINUTE_CAP):
                c.hr_spend()
            spent = 0
            while True:
                try:
                    c.reports_spend()
                except connecteam.ReportsBusy:
                    break
                spent += 1
            # HR's 6 and reports' 14 make 20: neither lane gets more this minute...
            assert spent == connecteam.MINUTE_CAP - connecteam.CLOCK_MINUTE_KEEP - connecteam.HR_MINUTE_CAP
            with pytest.raises(connecteam.HRBusy):
                c.hr_room()
            # ...and the clock checks still have 10.
            for _ in range(connecteam.CLOCK_MINUTE_KEEP):
                c._spend()
            with pytest.raises(connecteam.ConnecteamError):
                c._spend()
    go(scenario())
