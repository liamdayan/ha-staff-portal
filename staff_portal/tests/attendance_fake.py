"""A stand-in for the report framework, for the attendance report's tests only: a fake ctx (as
reports-core hands one to a report) with a fake Connecteam reader that has ctdata's interface
and records what it was asked for, plus a week of made-up staff that trips every rule."""

import datetime as dt
import importlib.util
import os
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(HERE, "..", "app", "reports", "attendance")
LA = ZoneInfo("America/Los_Angeles")


def load_report():
    """report.py on its own, as the framework loads it (no package needed)."""
    spec = importlib.util.spec_from_file_location("attendance_report_under_test",
                                                  os.path.join(REPORT_DIR, "report.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Unavailable(Exception):
    """ctdata.Unavailable: a plain reason Connecteam data can't be had."""


class FakeCt:
    Unavailable = Unavailable

    def __init__(self, timesheets=(), shifts=(), forms=(), submissions=None, users=(), fail=()):
        self._ts, self._shifts, self._forms = list(timesheets), list(shifts), list(forms)
        self._subs, self._users, self.fail = dict(submissions or {}), list(users), set(fail)
        self.calls = []

    def _check(self, what):
        if what in self.fail:
            raise Unavailable("Paused: reports have used their Connecteam allowance for now.")

    @staticmethod
    def _within(rows, key, start, end):
        return [r for r in rows if start <= dt.date.fromisoformat(str(r[key])[:10]) <= end]

    async def timesheets(self, start, end):
        self.calls.append(("timesheets", start, end))
        self._check("timesheets")
        return self._within(self._ts, "date", start, end)

    async def shifts(self, start, end):
        self.calls.append(("shifts", start, end))
        self._check("shifts")
        return self._within(self._shifts, "date", start, end)

    async def forms(self):
        self.calls.append(("forms",))
        self._check("forms")
        return list(self._forms)

    async def form_submissions(self, form_id, start, end):
        self.calls.append(("form_submissions", form_id, start, end))
        self._check("form_submissions")
        return self._within(self._subs.get(form_id, []), "submitted_at", start, end)

    def users(self):
        # sync on purpose: the report copes with either kind
        self.calls.append(("users",))
        self._check("users")
        return list(self._users)


class FakeCtx:
    def __init__(self, ct, settings=None, uploads=None, departments=None, today=dt.date(2026, 10, 20),
                 now=None, tz=LA, can_manage=False):
        self.ct, self.settings, self.departments, self.today, self.tz = ct, settings or {}, departments or [], today, tz
        self.now = now or dt.datetime(2026, 10, 20, 9, 0, tzinfo=tz)
        self.person = dict(name="Liam Test", departments=["Front Office"])
        self.can_manage = can_manage
        self._uploads = uploads or {}
        self.upload_keys = []

    def uploads(self, key):
        self.upload_keys.append(key)
        return list(self._uploads.get(key, []))


# ---------------------------------------------------------------- the sample week (Sun Oct 11 - Sat Oct 17, 2026)

def at(day, hhmm):
    """An ISO time with the property's offset, the way Connecteam's API gives times."""
    h, m = map(int, hhmm.split(":"))
    return dt.datetime(2026, 10, day, h, m, tzinfo=LA).isoformat()


def punch(uid, name, day, start, end, brk=30, job="", type="shift"):
    return dict(user_id=uid, name=name, date=f"2026-10-{day:02d}", clock_in=at(day, start),
                clock_out=at(day, end) if end else None, break_minutes=brk, job=job, type=type,
                hours=None, sched_start=None, sched_end=None)


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
        punch("1", "Maya Ortiz", 12, "06:12", "14:31", job="Front Desk"),
        punch("2", "Dev Patel", 13, "14:00", "22:00", job="Front Desk"),
        punch("3", "Lena Brooks", 14, "14:00", "21:00", brk=15, job="Thermal Pub"),
        dict(punch("5", "Priya Shah", 16, "08:30", None, brk=0, type="Time off - Sick leave"),
             clock_in=None, hours=7.5),
        punch("6", "Sam Whitaker", 17, "09:00", "17:30", job="Maintenance"),
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
    users = [dict(id=str(i), name=n) for i, n in enumerate(
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
        dict(id=1, name="Front Office", connecteam_user_ids=["1", "2"])]
    return ct, FakeCtx(ct, settings=settings, uploads=uploads, departments=departments, **kw)
