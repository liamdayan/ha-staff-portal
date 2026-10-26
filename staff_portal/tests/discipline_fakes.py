"""Stand-ins for the parts the discipline workflow calls but doesn't own, for its tests:

- policy.py and points.py (the points builder's): a published point system and an in-memory
  points ledger with the contract's names (policy.current/get; points.incident_points,
  add_incident_entry, adjust, void, totals, history, required_step, next_drop);
- documents.py's register_kind / add_generated / make_pdf / PdfUnavailable / view_url, and
  pdfdoc.page (the documents builder's).

install(monkeypatch) puts them in place and returns (policy, points, docs).
"""

import copy
import html
import types
from datetime import date, timedelta

from hr import discipline, documents, probation  # noqa: E402

STEPS = [
    {"key": "coaching", "name": "Coaching", "rank": 1, "active_days": 90},
    {"key": "verbal", "name": "Verbal warning", "rank": 2, "active_days": 180},
    {"key": "written", "name": "Written warning", "rank": 3, "active_days": 270},
    {"key": "final", "name": "Final written warning", "rank": 4, "active_days": 365},
    {"key": "suspension", "name": "Suspension", "rank": 5, "active_days": 365},
    {"key": "termination_review", "name": "Termination review", "rank": 6, "active_days": 0},
]
BODY = {
    "tracks": [{"key": "discipline", "name": "Discipline"}],
    "offences": [
        {"key": "late", "name": "Late 8+ minutes", "category": "Attendance", "attendance": True,
         "default_points": 1, "manager_range": {"min": 0.5, "max": 1.5},
         "handbook": "Be on time for every shift.", "hr_approval": True},
        {"key": "ncns", "name": "No call / no show", "category": "Attendance", "attendance": True,
         "default_points": 3, "manager_range": {"min": 3, "max": 3}},
        {"key": "rude", "name": "Rude to a guest", "category": "Conduct", "attendance": False,
         "default_points": 2, "manager_range": {"min": 1, "max": 3},
         "handbook": "Treat every guest with courtesy."},
        {"key": "theft", "name": "Theft", "category": "Conduct", "default_points": 0,
         "manager_range": {"min": 0, "max": 0}, "minimum_step": "termination_review",
         "serious": True},
        {"key": "phone", "name": "Phone on the floor", "category": "Conduct",
         "default_points": 0.5, "manager_range": {"min": 0.25, "max": 1}, "hr_approval": False},
        {"key": "safety", "name": "Unsafe knife handling", "category": "Safety",
         "default_points": 5, "manager_range": {"min": 5, "max": 5}, "minimum_step": "suspension"},
    ],
    "steps": STEPS,
    # (threshold, step) for the one track; the probation ladder is lower.
    "ladders": {"discipline": [[2, "coaching"], [4, "verbal"], [6, "written"], [8, "final"],
                               [10, "termination_review"]]},
    "visibility": {"employee_sees_total": True, "employee_sees_expired": True},
    "approval": {"all": True},
    "probation": [
        {"key": "intro", "name": "Introductory period", "kind": "introductory", "days": 90,
         "days_by_department": {"502": 60}, "extensions": {"count": 1, "days": 30, "maximum": 120},
         "review_lead_days": 14, "end_passes": "remind", "on_pass": "keep",
         "ladder": [[3, "final"], [5, "termination_review"]]},
        {"key": "last_chance", "name": "Last-chance probation", "kind": "disciplinary", "days": 90,
         "extensions": {"count": 1, "days": 30, "maximum": 180}, "review_lead_days": 7},
    ],
}


class FakePolicy:
    """policy.py: versions published with an effective date."""

    def __init__(self):
        self.versions = []    # (effective, id, body), oldest first

    def publish(self, body=None, effective: date | None = None, vid: str = "v1") -> dict:
        body = copy.deepcopy(BODY if body is None else body)
        self.versions.append((effective or date(2020, 1, 1), vid, body))
        self.versions.sort(key=lambda v: v[0])
        return body

    def current(self, db, on):
        found = [v for v in self.versions if v[0] <= on]
        return {"id": found[-1][1], "body": found[-1][2], "effective": found[-1][0]} if found else None

    def get(self, db, version_id):
        return next(({"id": v[1], "body": v[2]} for v in self.versions if v[1] == version_id), None)


class FakePoints:
    """points.py: an in-memory ledger. Points expire a year after the day they happened."""

    def __init__(self, policy: FakePolicy):
        self.policy = policy
        self.lines: list[dict] = []
        self.contexts: list[dict] = []     # every ctx incident_points was given
        self.calls: list[tuple] = []

    # -- scoring
    def incident_points(self, body, offence_key, ctx, history, manager_points=None, hr_points=None):
        self.contexts.append(dict(ctx))
        o = next(o for o in body["offences"] if o["key"] == offence_key)
        default = float(o["default_points"])
        repeat = None
        if offence_key == "late" and sum(1 for h in history if h["offence_key"] == "late") >= 2:
            default, repeat = 1.5, "Third late arrival in 90 days"
        low, high = o["manager_range"]["min"], o["manager_range"]["max"]
        errors, final = [], default
        if manager_points is not None:
            if not low <= manager_points <= high:
                errors.append(f"Choose between {low:g} and {high:g} points for this offence.")
            else:
                final = manager_points
        if hr_points is not None:
            final = hr_points
        if ctx.get("protected"):
            final = 0.0
        return {"default": default, "final": final, "repeat": repeat,
                "protected": bool(ctx.get("protected")), "probation_multiplier": 1.0, "errors": errors}

    def required_step(self, body, ctx, totals, offence_key, active_step_key):
        total = sum(totals.values())
        ladder = body["ladders"]["discipline"]
        if ctx.get("on_probation"):
            ladder = next(p["ladder"] for p in body["probation"] if p["key"] == ctx["on_probation"])
        found, reason = None, ""
        for threshold, key in ladder:
            if total >= threshold:
                found, reason = key, f"Points {total:g} ≥ {threshold:g} → {key}"
        o = next((o for o in body["offences"] if o["key"] == offence_key), {})
        rank = {s["key"]: s["rank"] for s in body["steps"]}
        if o.get("minimum_step") and rank[o["minimum_step"]] > rank.get(found, 0):
            found, reason = o["minimum_step"], f"{o['name']} always goes to at least {o['minimum_step']}"
        if active_step_key == "final":
            found, reason = "termination_review", "New incident while on Final written"
        return {"step": found, "reason": reason}

    # -- the ledger
    def _add(self, **line) -> int:
        line["id"] = len(self.lines) + 1
        line.setdefault("voided", False)
        self.lines.append(line)
        return line["id"]

    def add_incident_entry(self, db, person_id, incident_id, track, points, happened, counts_from,
                           version_id, actor_id, reason):
        self.calls.append(("entry", person_id, incident_id, points, happened, counts_from))
        return self._add(kind="incident", person_id=str(person_id), incident_id=incident_id,
                         track=track, points=float(points), happened=happened.isoformat(),
                         counts_from=counts_from.isoformat(),
                         ends=(happened + timedelta(days=365)).isoformat(), reason=reason)

    def add(self, person_id, points, happened: date, track="discipline"):
        """A line that was already there (test setup)."""
        return self._add(kind="incident", person_id=str(person_id), incident_id=None, track=track,
                         points=float(points), happened=happened.isoformat(),
                         counts_from=happened.isoformat(),
                         ends=(happened + timedelta(days=365)).isoformat(), reason="Before")

    def adjust(self, db, *, person_id, track, points, starts, ends, actor_id, reason, incident_id=None):
        self.calls.append(("adjust", person_id, track, points, starts, ends, incident_id, reason))
        return self._add(kind="adjustment", person_id=str(person_id), incident_id=incident_id,
                         track=track, points=float(points), happened=starts.isoformat(),
                         counts_from=starts.isoformat(),
                         ends=(ends or starts + timedelta(days=365)).isoformat(), reason=reason)

    def void(self, db, *, person_id, incident_id, actor_id, reason):
        self.calls.append(("void", person_id, incident_id, reason))
        for line in self.lines:
            if line["incident_id"] == incident_id and line["person_id"] == str(person_id):
                line["voided"] = True

    def credit(self, *args, **kwargs):
        raise AssertionError("credits aren't used by the discipline workflow")

    def _state(self, line, on: date) -> str:
        if line["voided"]:
            return "voided"
        if line["ends"] < on.isoformat():
            return "expired"
        return "counts" if line["counts_from"] <= on.isoformat() else "later"

    def totals(self, db, person_id, as_of):
        found = {}
        for line in self.lines:
            if line["person_id"] == str(person_id) and self._state(line, as_of) == "counts":
                found[line["track"]] = found.get(line["track"], 0.0) + line["points"]
        return found

    def history(self, db, person_id, as_of, include_expired):
        return [{**line, "state": self._state(line, as_of)} for line in self.lines
                if line["person_id"] == str(person_id) and line["counts_from"] <= as_of.isoformat()
                and (include_expired or self._state(line, as_of) != "expired")]

    def next_drop(self, db, person_id, as_of):
        counting = [line for line in self.lines if line["person_id"] == str(person_id)
                    and self._state(line, as_of) == "counts"]
        if not counting:
            return None
        first = min(line["ends"] for line in counting)
        return {"date": date.fromisoformat(first) + timedelta(days=1),
                "points": sum(line["points"] for line in counting if line["ends"] == first)}


class PdfUnavailable(Exception):
    pass


class FakeDocs:
    """documents.py's part for write-ups: kinds registered, files filed, PDFs made."""

    def __init__(self):
        self.kinds: list[tuple] = []
        self.added: list[dict] = []
        self.pages: list[tuple] = []      # (title, blocks) given to pdfdoc.page
        self.google = True                # False: make_pdf says Google isn't set up

    def register_kind(self, key, name, level, subfolder, owner_section, can_open=None):
        self.kinds.append((key, name, level, subfolder, owner_section, can_open))

    async def add_generated(self, svc, person, kind_key, data, mime, title, doc_date, actor_id):
        self.added.append({"person": person["id"], "kind": kind_key, "data": data, "mime": mime,
                           "title": title, "date": doc_date, "actor": actor_id})
        return {"id": 1000 + len(self.added)}

    async def make_pdf(self, svc, page_html):
        if not self.google:
            raise PdfUnavailable("Google Drive isn't set up yet, so the PDF waits.")
        return b"%PDF-1.4 " + page_html.encode()

    def page(self, title, blocks):
        self.pages.append((title, blocks))
        return "<h1>" + html.escape(title) + "</h1>" + "".join(
            "<p>" + " ".join(html.escape(str(x)) for x in b[1:]) + "</p>" for b in blocks)

    def view_url(self, doc):
        return f"/api/documents/{doc['id']}/file"


def install(monkeypatch):
    pol = FakePolicy()
    pts = FakePoints(pol)
    docs = FakeDocs()
    monkeypatch.setattr(discipline, "policy", pol)
    monkeypatch.setattr(discipline, "points", pts)
    monkeypatch.setattr(probation, "policy", pol)
    monkeypatch.setattr(discipline, "pdfdoc", types.SimpleNamespace(page=docs.page))
    for name in ("register_kind", "add_generated", "make_pdf", "view_url"):
        monkeypatch.setattr(documents, name, getattr(docs, name), raising=False)
    monkeypatch.setattr(documents, "PdfUnavailable", PdfUnavailable, raising=False)
    monkeypatch.setattr(documents, "get", lambda db, doc_id: {"id": doc_id})
    return pol, pts, docs
