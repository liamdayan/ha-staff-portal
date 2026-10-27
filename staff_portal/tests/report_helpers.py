"""Shared setup for reports' tests: a stand-in for ctdata (StubCt) and the ctx a report's data()
gets (FakeCtx), so a report.py can be tested on its own, and a way to load a report folder.

StubCt answers from lists given to it, as ctdata does (same fields, dates filtered the same way),
counts its calls, and raises Unavailable when told to. Nothing here talks to Connecteam.
"""

import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

import reports as reports_kit  # noqa: E402
from ctdata import Unavailable  # noqa: E402

REPORTS_DIR = os.path.join(APP, "reports")


def _day(value) -> date:
    return value.date() if isinstance(value, datetime) else (
        value if isinstance(value, date) else date.fromisoformat(str(value)))


class StubCt:
    """ctdata's interface from lists. timesheets and shifts rows as ctdata gives them (filtered
    by their "date"); forms as [{id, name, questions}]; submissions {form ID: [rows]} (filtered by
    "submitted_at"). unavailable: a message, to make every call raise Unavailable with it."""

    Unavailable = Unavailable

    def __init__(self, timesheets=(), shifts=(), forms=(), submissions=None, users=(),
                 unavailable=None):
        self.rows = {"timesheets": list(timesheets), "shifts": list(shifts)}
        self.form_list = list(forms)
        self.submission_rows = dict(submissions or {})
        self.user_list = list(users)
        self.unavailable = unavailable
        self.calls: list[tuple] = []

    def _check(self, *call):
        self.calls.append(call)
        if self.unavailable:
            raise Unavailable(self.unavailable)

    async def timesheets(self, start, end):
        self._check("timesheets", _day(start), _day(end))
        return [r for r in self.rows["timesheets"]
                if _day(start).isoformat() <= r["date"] <= _day(end).isoformat()]

    async def shifts(self, start, end):
        self._check("shifts", _day(start), _day(end))
        return [r for r in self.rows["shifts"]
                if _day(start).isoformat() <= r["date"] <= _day(end).isoformat()]

    async def forms(self):
        self._check("forms")
        return list(self.form_list)

    async def form_submissions(self, form_id, start, end):
        self._check("form_submissions", str(form_id), start, end)
        low = start.strftime("%Y-%m-%dT%H:%M") if isinstance(start, datetime) else _day(start).isoformat()
        high = _day(end).isoformat() + "T99"
        return [r for r in self.submission_rows.get(str(form_id), [])
                if low <= (r["submitted_at"] or "") <= high]

    async def users(self):
        self._check("users")
        return list(self.user_list)


class FakeCtx:
    """What a report's data(ctx, params) gets, without the add-on: ct (a StubCt), uploads(key)
    from {key: [{name, text, uploaded_at, by}]} (newest first), settings, person, departments,
    staff, tz, today and manage."""

    def __init__(self, ct=None, uploads=None, settings=None, person=None, departments=(),
                 tz="America/Los_Angeles", today=None, manage=False, staff=()):
        self.ct = ct or StubCt()
        self._uploads = dict(uploads or {})
        self.settings = dict(settings or {})
        self.person = person or {"id": 1, "name": "Ana", "connecteam_user_id": None,
                                 "departments": []}
        self.departments = list(departments)
        self.staff = list(staff)
        self.tz = ZoneInfo(tz)
        self.today = today or datetime.now(self.tz).date()
        self.manage = manage

    def uploads(self, key):
        return list(self._uploads.get(key, []))


def load_report(folder: str) -> "reports_kit.Report":
    """A report folder under app/reports/ (such as "_template"), loaded as the add-on loads it."""
    return reports_kit.load(os.path.join(REPORTS_DIR, folder))


def settings_for(report, saved=None) -> dict:
    """The settings data() would get: the report's defaults with `saved` over them."""
    return reports_kit.stored_settings(report, saved or {})
