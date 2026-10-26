"""The real point system (policy.py, points.py) for the discipline tests: a small published
policy, the same one every discipline and probation test uses, and a few conveniences around the
real ledger.

install(monkeypatch) returns (policy, points) helpers:
- policy.publish(body=None) publishes BODY (or the body given) the moment the HR portal starts
  (or at once, if it's running), in force since well before any incident in the tests (a later
  one from today). It returns the body, which a test may change until then.
- points.add(person, points, day) puts points that were already there (an HR adjustment from
  that day, expiring like an incident); points.totals / lines read the real ledger; contexts
  holds every ctx incident_points was given.
"""

import copy
from datetime import timedelta

from hr import hrdb, points as real_points, policy as real_policy, service  # noqa: E402

STEPS = [
    {"key": "coaching", "name": "Coaching", "rank": 1, "active": {"days": 90}},
    {"key": "verbal", "name": "Verbal warning", "rank": 2, "active": {"days": 180}},
    {"key": "written", "name": "Written warning", "rank": 3, "active": {"days": 270}},
    {"key": "final", "name": "Final written warning", "rank": 4, "active": {"days": 365}},
    {"key": "suspension", "name": "Suspension", "rank": 5, "active": {"days": 365}, "hr_only": True},
    {"key": "termination_review", "name": "Termination review", "rank": 6, "active": None,
     "hr_only": True, "termination": True},
]
BODY = {
    "name": "Test point system",
    "point_size": 0.25,
    "skip_steps": True,
    "tracks": [{"key": "discipline", "name": "Discipline"}],
    "offences": [
        {"key": "late", "name": "Late 8+ minutes", "track": "discipline", "category": "Attendance",
         "attendance": True, "points": 1, "min": 0.5, "max": 1.5,
         "repeat": {"within": {"days": 90}, "tiers": [{"nth": 3, "add": 0.5, "step": None}]},
         "wording": "Be on time for every shift."},
        {"key": "ncns", "name": "No call / no show", "track": "discipline", "category": "Attendance",
         "attendance": True, "points": 3, "min": 3, "max": 3},
        {"key": "rude", "name": "Rude to a guest", "track": "discipline", "category": "Conduct",
         "points": 2, "min": 1, "max": 3, "wording": "Treat every guest with courtesy."},
        {"key": "theft", "name": "Theft", "track": "discipline", "category": "Conduct", "points": 0,
         "min": 0, "max": 0, "min_step": "termination_review", "serious": True},
        {"key": "phone", "name": "Phone on the floor", "track": "discipline", "category": "Conduct",
         "points": 0.5, "min": 0.25, "max": 1, "hr_approval": False},
        {"key": "safety", "name": "Unsafe knife handling", "track": "discipline", "category": "Safety",
         "points": 5, "min": 5, "max": 5, "min_step": "suspension"},
    ],
    "steps": STEPS,
    "ladders": {"discipline": [{"points": 2, "step": "coaching"}, {"points": 4, "step": "verbal"},
                               {"points": 6, "step": "written"}, {"points": 8, "step": "final"},
                               {"points": 10, "step": "termination_review"}]},
    "escalation": [{"while": "final", "to": "termination_review"}],
    "expiry": {"rule": "rolling", "rolling": {"months": 12}, "clean_reset": None},
    "visibility": {"employee_sees_total": True, "employee_sees_expired": True},
    "approval": {"incidents": "all", "writeups": "all"},
    "probation": [
        {"key": "intro", "name": "Introductory period", "kind": "introductory", "days": 90,
         "department_days": {"502": 60}, "extensions": {"count": 1, "days": 30, "max_days": 120},
         "review_lead_days": 14, "when_end_passes": "remind", "on_pass": "keep",
         "ladders": {"discipline": [{"points": 3, "step": "final"},
                                    {"points": 5, "step": "termination_review"}]}},
        {"key": "last_chance", "name": "Last-chance probation", "kind": "disciplinary", "days": 90,
         "extensions": {"count": 1, "days": 30, "max_days": 180}, "review_lead_days": 7},
    ],
}
SINCE_DAYS = 1000      # the version is in force from this long ago (a first version may start back)


class Policy:
    """policy.py, with a test's point system published when the HR portal starts."""

    def __init__(self):
        self.pending = None

    def publish(self, body: dict | None = None) -> dict:
        self.pending = copy.deepcopy(BODY if body is None else body)
        if hrdb.current is not None:
            self.started(hrdb.current)
        return self.pending

    def started(self, db) -> None:
        if self.pending is None:
            return
        body, self.pending = self.pending, None
        problems = real_policy.validate(body)
        assert not problems, problems
        vid = real_policy.save_draft(db, body, "test")
        on = real_policy.today(db)
        later = real_policy.current(db, on) is not None    # a newer version: from today
        real_policy.publish(db, vid, on if later else on - timedelta(days=SINCE_DAYS), "test", on=on)

    def current(self, db, on):
        return real_policy.current(db or hrdb.current, on)


class Points:
    """points.py, with points that were there before and the contexts scoring was given."""

    def __init__(self):
        self.contexts: list[dict] = []

    def add(self, person_id, points, day, track="discipline"):
        return real_points.adjust(hrdb.current, str(person_id), track, points, day, "test", "Before")

    def totals(self, db, person_id, as_of):
        return real_points.totals(db or hrdb.current, str(person_id), as_of)

    def lines(self, person_id, kind=None) -> list[dict]:
        return [ln for ln in real_points.lines_of(hrdb.current, str(person_id))
                if kind is None or ln["kind"] == kind]


def install(monkeypatch):
    pol, pts = Policy(), Points()
    monkeypatch.setattr(service, "PARTS", [*service.PARTS,
                                           lambda svc: svc.on_start.append(lambda s: pol.started(s.db))])
    real = real_points.incident_points

    def recorded(body, offence_key, ctx, *args, **kwargs):
        pts.contexts.append(dict(ctx))
        return real(body, offence_key, ctx, *args, **kwargs)
    monkeypatch.setattr(real_points, "incident_points", recorded)
    return pol, pts
