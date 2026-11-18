"""Someone with no role on a role-based device gets nothing from the layout's pages (0.12.25: it
used to be every page, and with them any access the layout's items give), unless pages are picked
for them. What's set for them personally still shows (0.12.26: 0.12.25 hid that too)."""

import json

import test_wristbands_server  # noqa: F401  (sets up a test data folder before main is imported)

import layout as layout_kit  # noqa: E402
import main  # noqa: E402
from db import Database  # noqa: E402

LAYOUT = {"pages": [
    {"id": "pool", "name": "Pool", "items": [
        {"id": "i1", "type": "device", "entity_id": "light.pool", "access": "control"},
        {"id": "i2", "type": "device", "entity_id": "light.sauna"}]},
    {"id": "office", "name": "Office", "items": [
        {"id": "i3", "type": "device", "entity_id": "switch.office", "access": "view"}]},
], "page_groups": []}


def shown(member, areas) -> dict:
    """{page: [devices]} as the staff app would show this person."""
    access = main.person_access(member, areas, LAYOUT)
    seen = layout_kit.visible_layout(LAYOUT, access, areas, main.person_pages(member, LAYOUT))
    return {p["id"]: [i["entity_id"] for i in p["items"]] for p in seen["pages"]}


def test_no_role_gets_nothing_from_the_pages_but_keeps_their_own(tmp_path, monkeypatch):
    previous, main.db = main.db, Database(str(tmp_path))
    previous.conn.close()
    monkeypatch.setattr(main, "offered", lambda eid: True)
    monkeypatch.setattr(main, "controllable", lambda eid: True)
    areas = main.AreaLayout([], {}, {})
    db = main.db

    nobody = db.staff(db.create_staff("Sam", "1234"))
    assert nobody["role_id"] is None
    # Nothing comes from the layout's own access settings, so nothing is on show.
    assert main.person_override_pages(nobody, LAYOUT) == []
    assert main.person_access(nobody, areas, LAYOUT) == {}
    assert shown(nobody, areas) == {}

    # A device given to them personally shows, on the page it's on, and nothing else does.
    db.set_staff_overrides(nobody["id"], {"light.sauna": "view"}, {})
    assert main.person_access(nobody, areas, LAYOUT) == {"light.sauna": "view"}
    assert shown(nobody, areas) == {"pool": ["light.sauna"]}
    db.set_staff_overrides(nobody["id"], {}, {})

    # Pages picked for them count, with those pages' access.
    db.update_staff(nobody["id"], layout_pages=json.dumps(["pool"]))
    picked = db.staff(nobody["id"])
    assert main.person_access(picked, areas, LAYOUT) == {"light.pool": "control"}
    assert shown(picked, areas) == {"pool": ["light.pool"]}

    # A role that shows every page still means every page; a deleted role is no role.
    role_id = db.create_role("Front desk")
    with_role = db.create_staff("Ana", "5678")
    db.update_staff(with_role, role_id=role_id)
    member = db.staff(with_role)
    assert main.person_override_pages(member, LAYOUT) is None
    assert shown(member, areas) == {"pool": ["light.pool"], "office": ["switch.office"]}
    db.set_role_pages(role_id, ["office"])
    assert shown(db.staff(with_role), areas) == {"office": ["switch.office"]}
    assert main.person_override_pages({"id": with_role, "role_id": 999999}, LAYOUT) == []
