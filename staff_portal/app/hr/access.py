"""Who may see and do what in the HR portal. Every route has a rule (rule()), the server checks
it on every request, and startup refuses any route without one (check_routes).

Roles:
  HR admin   given by the owner on the Home Assistant dashboard: everyone, including former
             employees, HR settings and sign-ins.
  Director   given by an HR admin: everyone in every department except HR admins and other
             directors (unless they're the director's direct reports in Connecteam).
  Manager    given by an HR admin, with the departments (smart groups) HR assigns: the people in
             those departments, but not other managers, directors or HR admins unless they're
             the manager's direct reports.
  Employee   everyone: only their own file.

Rules over every role:
  1. Nobody acts on their own file except as an employee (relation "self", whatever their role).
  2. Managers don't see managers or HR admins unless they're their direct reports.
  3. Access follows the person live: roles, departments and who's in them are read on every
     request, so a move in Connecteam changes who sees someone at once.
  4. Every text has a visibility level, and text a viewer can't see never leaves the server.
  5. Issued records are never rewritten (hrdb.append_only; corrections are new rows).
  6. Nobody approves their own work (not_own_work).
"""

import dataclasses
import json

HR_ADMIN, DIRECTOR, MANAGER, EMPLOYEE = "hr_admin", "director", "manager", "employee"
STAFF_ROLES = (HR_ADMIN, DIRECTOR, MANAGER)
ROLE_WORDS = {HR_ADMIN: "HR admin", DIRECTOR: "Director", MANAGER: "Manager", EMPLOYEE: "Employee"}

# Who can see a piece of text or a kind of document, from the widest to the narrowest.
LEVELS = {
    "employee": "The employee, their managers, directors and HR",
    "managers": "Their managers, directors and HR",
    "directors": "Directors and HR",
    "hr": "HR admins only",
    "restricted": "Restricted: HR admins only, kept apart",
}
SEES = {"self": {"employee"}, "manager": {"employee", "managers"},
        "director": {"employee", "managers", "directors"}, "hr": set(LEVELS)}

WHO = ("public", "signed_in", "staff", "hr")
NOT_YOURS = "You can't do this on your own file. Another HR admin or the owner can."
OWN_WORK = "Someone else has to approve this: nobody approves their own work."


class Refused(Exception):
    """Not allowed: status 403 (or 404 where even saying so would tell too much)."""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


def _list(value) -> list[str]:
    try:
        found = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return []
    return [str(v) for v in found] if isinstance(found, list) else []


def groups_of(person: dict) -> set[str]:
    return set(_list(person.get("groups")))


def managers_of(person: dict) -> set[str]:
    """Their direct manager(s) in Connecteam."""
    return set(_list(person.get("managers")))


@dataclasses.dataclass(frozen=True)
class Viewer:
    """The signed-in person, as of this request."""
    id: str
    name: str
    roles: frozenset
    groups: dict          # the departments they manage: {smart group ID: primary}
    session: dict | None = None

    @property
    def is_hr(self) -> bool:
        return HR_ADMIN in self.roles

    @property
    def is_director(self) -> bool:
        return DIRECTOR in self.roles

    @property
    def is_manager(self) -> bool:
        return MANAGER in self.roles

    @property
    def is_staff(self) -> bool:
        return bool(self.roles & set(STAFF_ROLES))

    @property
    def role(self) -> str:
        return next((r for r in STAFF_ROLES if r in self.roles), EMPLOYEE)


def viewer_for(db, person: dict, session: dict | None = None) -> Viewer:
    roles = frozenset(db.roles_of(person["id"]))
    return Viewer(person["id"], person["name"], roles,
                  db.manager_groups(person["id"]) if MANAGER in roles else {}, session)


def relation(viewer: Viewer, person: dict, person_roles: set[str] | None = None) -> str | None:
    """How the viewer sees this person's file: "self", "hr", "director", "manager", or None (not
    at all). person_roles: the person's roles, when already read."""
    if person["id"] == viewer.id:
        return "self"
    if viewer.is_hr:
        return "hr"
    if person.get("archived"):
        return None   # former employees: HR only
    senior = person_roles if person_roles is not None else set()
    direct = viewer.id in managers_of(person)
    if viewer.is_director and (direct or not senior & {HR_ADMIN, DIRECTOR}):
        return "director"
    if viewer.is_manager and (direct or (not senior and groups_of(person) & set(viewer.groups))):
        return "manager"
    return None


def person_relation(db, viewer: Viewer, person: dict | None) -> str | None:
    if not person:
        return None
    return relation(viewer, person, db.roles_of(person["id"]))


def visible_people(db, viewer: Viewer, archived: bool = False) -> list[tuple[dict, str]]:
    """Everyone the viewer may see, with how ((person, relation)). archived: former employees
    (only HR sees them)."""
    if not viewer.is_staff:
        me = db.person(viewer.id)
        return [(me, "self")] if me and bool(me["archived"]) == archived else []
    roles = db.all_roles()
    found = []
    for p in db.people(archived=archived):
        rel = relation(viewer, p, roles.get(p["id"], set()))
        if rel:
            found.append((p, rel))
    return found


def can_see_level(rel: str | None, level: str) -> bool:
    return level in SEES.get(rel or "", set())


def visible(items: list[dict], rel: str | None, key: str = "visibility") -> list[dict]:
    """The items (texts, documents) this relation may see. Anything without a known level is
    HR only, so a mistake hides rather than shows."""
    return [i for i in items if can_see_level(rel, i.get(key) if i.get(key) in LEVELS else "hr")]


def not_own_file(viewer: Viewer, person_id) -> None:
    if str(person_id) == viewer.id:
        raise Refused(NOT_YOURS)


def not_own_work(viewer: Viewer, author_id) -> None:
    if author_id is not None and str(author_id) == viewer.id:
        raise Refused(OWN_WORK)


# ---------------------------------------------------------------- route rules

@dataclasses.dataclass(frozen=True)
class Rule:
    who: str                 # public, signed_in, staff (managers and up) or hr (HR admins)
    person: bool = False     # the route names a person ({pid}): the viewer must see their file
    not_self: bool = False   # ...and it mustn't be their own (rule 1)
    sensitive: bool = False  # asks for a fresh authenticator code (or PIN) first


def rule(who: str, *, person: bool = False, not_self: bool = False, sensitive: bool = False):
    """Mark a handler with who may use it. A route without a rule is never served."""
    if who not in WHO:
        raise ValueError(who)

    def mark(handler):
        handler.hr_rule = Rule(who, person, not_self, sensitive)
        return handler
    return mark


def rule_of(handler) -> Rule | None:
    found = getattr(handler, "hr_rule", None)
    return found if isinstance(found, Rule) else None


def check_routes(app) -> None:
    """Startup check: every route has a rule. Raises RuntimeError naming the ones that don't."""
    missing = [f"{route.method} {route.resource.canonical}" for route in app.router.routes()
               if route.method != "HEAD" and rule_of(route.handler) is None]
    if missing:
        raise RuntimeError("HR routes without an access rule: " + ", ".join(missing))


def allows(r: Rule, viewer: Viewer | None) -> bool:
    """The role part of a rule (the person part needs the person: person_relation)."""
    if r.who == "public":
        return True
    if viewer is None:
        return False
    return (r.who == "signed_in" or (r.who == "staff" and viewer.is_staff)
            or (r.who == "hr" and viewer.is_hr))
