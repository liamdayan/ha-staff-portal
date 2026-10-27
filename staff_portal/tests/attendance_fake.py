"""The attendance report's test setup, on the shared report helpers (tests/report_helpers.py):
the report loaded as the add-on loads it, a StubCt that can refuse some calls, a FakeCtx with a
fixed "now", and a week of made-up staff that trips every rule."""

import datetime as dt
import os
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from report_helpers import REPORTS_DIR, FakeCtx as BaseCtx, StubCt, Unavailable, load_report as load

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(REPORTS_DIR, "attendance")
LA = ZoneInfo("America/Los_Angeles")


def load_report():
    """report.py, loaded by the add-on's own loader (reports.load), with its functions."""
    report = load("attendance")
    return SimpleNamespace(**report.data.__globals__, loaded=report)


class FakeCt(StubCt):
    """ctdata from lists; fail: the calls that raise Unavailable (as a paused budget would)."""

    def __init__(self, timesheets=(), shifts=(), forms=(), submissions=None, users=(), fail=()):
        super().__init__(timesheets, shifts, forms, submissions, users)
        self.fail = set(fail)

    def _check(self, *call):
        self.calls.append(call)
        if call[0] in self.fail:
            raise Unavailable("Paused: reports have used their Connecteam allowance for now.")


class FakeCtx(BaseCtx):
    def __init__(self, ct, settings=None, uploads=None, departments=None, today=dt.date(2026, 10, 20),
                 now=None, tz="America/Los_Angeles", manage=False, person=None, staff=()):
        super().__init__(ct=ct, uploads=uploads, settings=settings, departments=departments or [],
                         tz=tz, today=today, manage=manage, staff=staff,
                         person=person or {"id": 1, "name": "Liam Test", "connecteam_user_id": "1",
                                           "departments": [{"id": 1, "name": "Front Office"}]})
        self.now = now or dt.datetime(2026, 10, 20, 9, 0, tzinfo=self.tz)
        self.upload_keys = []

    def uploads(self, key):
        self.upload_keys.append(key)
        return super().uploads(key)


# ---------------------------------------------------------------- the sample week (Sun Oct 11 - Sat Oct 17, 2026)

def at(day, hhmm):
    """An ISO time with the property's offset, the way Connecteam's API gives times."""
    h, m = map(int, hhmm.split(":"))
    return dt.datetime(2026, 10, day, h, m, tzinfo=LA).isoformat()


def punch(uid, name, day, start, end, brk=0, job="", type="shift"):
    return dict(user_id=uid, name=name, date=f"2026-10-{day:02d}", clock_in=at(day, start),
                clock_out=at(day, end) if end else None, break_minutes=brk, job=job, type=type,
                hours=None, sched_start=None, sched_end=None)


def worked(uid, name, day, start, end, brk=30, job=""):
    """A shift as ctdata gives it: the work row from clock-in to clock-out (break_minutes 0),
    and a lunch taken with Connecteam's Break button as a row of its own ("break") inside it,
    from mid-shift."""
    rows = [punch(uid, name, day, start, end, job=job)]
    if brk:
        h, m = map(int, start.split(":"))
        mid = h * 60 + m + 4 * 60
        rows.append(punch(uid, name, day, f"{mid // 60:02d}:{mid % 60:02d}",
                          f"{(mid + brk) // 60:02d}:{(mid + brk) % 60:02d}", brk=brk, job="Break",
                          type="break"))
    return rows


def shift(uid, name, day, start, end, job, title="", draft=False, open=False):
    return dict(user_id=uid, name=name, date=f"2026-10-{day:02d}", start=at(day, start),
                end=at(day, end), job=job, title=title or job, draft=draft, open=open)


GUSTO = """Time Off Balances Report
,
Sample Lodge Co,
Employee,Title,Policy,Accrued time,Hours used,Ending balance
"Ortiz, Maya",Front Desk,Paid Time Off,46.5,4,42.5
"Ortiz, Maya",Front Desk,Sick,32,8,24
"Patel, Devendra",Front Desk,Paid Time Off,6.5,4,2.5
"Patel, Devendra",Front Desk,Sick,11,8,3
"Reyes, Jonah",Maintenance,Paid Time Off,4,4,0
"Reyes, Jonah",Maintenance,Sick,14,8,6
"Brooks, Rose",Thermal Pub,Paid Time Off,24,4,20
"Brooks, Rose",Thermal Pub,Sick,22,8,14
"Shah, Priya",Housekeeping,Paid Time Off,35,4,31
"Shah, Priya",Housekeeping,Sick,26,8,18
"Whitaker, Sam",Maintenance,Paid Time Off,24,4,20
"Whitaker, Sam",Maintenance,Sick,4,2,2
"Else, Nobody",Spa,Sick,1,0,1
"""

FORMS = [dict(id="f1", name="Shift handoff log"), dict(id="f2", name="Weekly safety walk"),
         dict(id="f3", name="Tub temperature check"), dict(id="f9", name="Lost and found")]


def sample(**kw):
    """Maya late, Dev early, Lena (Rose in Gusto, by alias) a short lunch and a missed tub check,
    Jonah a no-show his sick leave can't cover, Priya off sick, Sam clean but a new hire."""
    ts = [
        *worked("1", "Maya Ortiz", 12, "06:12", "14:31", job="Front Desk"),
        *worked("2", "Dev Patel", 13, "14:00", "22:00", job="Front Desk"),
        *worked("3", "Lena Brooks", 14, "14:00", "21:00", brk=15, job="Thermal Pub"),
        dict(punch("5", "Priya Shah", 16, "08:30", None, type="Time off - Sick leave"),
             clock_in=None, hours=7.5),
        *worked("6", "Sam Whitaker", 17, "09:00", "17:30", job="Maintenance"),
    ]
    shifts = [
        shift("1", "Maya Ortiz", 12, "06:00", "14:30", "Front Desk"),
        shift("2", "Dev Patel", 13, "14:00", "22:30", "Front Desk"),
        shift("3", "Lena Brooks", 14, "14:00", "21:00", "Thermal Pub"),
        shift("4", "Jonah Reyes", 15, "09:00", "17:30", "Maintenance"),
        shift("5", "Priya Shah", 16, "08:30", "16:00", "Housekeeping"),
        shift("6", "Sam Whitaker", 17, "09:00", "17:30", "Maintenance"),
        shift("1", "Maya Ortiz", 17, "06:00", "14:30", "Front Desk", draft=True),
        shift(None, "", 16, "10:00", "18:00", "Spa", open=True),
    ]
    subs = {
        "f1": [dict(user_id="1", name="Maya Ortiz", submitted_at=at(12, "14:25")),
               dict(user_id="2", name="Dev Patel", submitted_at=at(14, "00:05")),
               dict(user_id="6", name="Sam Whitaker", submitted_at=at(17, "17:20"))],
        "f2": [dict(user_id="6", name="Sam Whitaker", submitted_at=at(17, "10:00"))],
        "f3": [dict(user_id="3", name="Lena Brooks", submitted_at=at(14, t))
               for t in ("14:10", "15:10", "17:05", "18:30", "19:10", "20:15")],
    }
    # Maya and Dev are in the Front Office's smart group (501), Jonah and Sam in Maintenance's (502).
    groups = {"1": ["501"], "2": ["501", "502"], "4": ["502"], "6": ["502", "999"]}
    users = [dict(user_id=str(i), name=n, groups=groups.get(str(i), [])) for i, n in enumerate(
        ["", "Maya Ortiz", "Dev Patel", "Lena Brooks", "Jonah Reyes", "Priya Shah", "Sam Whitaker"]) if n]
    ct = FakeCt(ts, shifts, FORMS, subs, users, fail=kw.pop("fail", ()))
    settings = kw.pop("settings", None) or dict(
        rules=dict(aliases="Rose Brooks = Lena Brooks"),
        forms=[dict(id="f1", name="Shift handoff log", frequency="shift", applies_to=""),
               dict(id="f2", name="Weekly safety walk", frequency="week", applies_to="Maintenance"),
               dict(id="f3", name="Tub temperature check", frequency="hour", applies_to="Thermal Pub",
                    every=1, night_every=0)])
    uploads = kw.pop("uploads", None) or {"gusto": [
        dict(name="old.csv", text="Employee,Sick\nMaya Ortiz,1\n", uploaded_at=100.0, by="Old"),
        dict(name="time-off-balances.csv", text=GUSTO, uploaded_at=200.0, by="Liam")]}
    departments = kw.pop("departments", None) or [
        dict(id=1, name="Front Office", smart_group_id="501"),
        dict(id=2, name="Maintenance", smart_group_id="502"), dict(id=3, name="Spa", smart_group_id=None)]
    return ct, FakeCtx(ct, settings=settings, uploads=uploads, departments=departments, **kw)
