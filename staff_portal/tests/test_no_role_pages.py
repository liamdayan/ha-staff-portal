"""0.12.25: someone with no role sees no layout pages on a role-based device (it used to be every
page, and with them any access the layout's items give), unless pages are picked for them."""

import json

import test_wristbands_server  # noqa: F401  (sets up a test data folder before main is imported)

import main  # noqa: E402
from db import Database  # noqa: E402

LAYOUT = {"pages": [
    {"id": "pool", "name": "Pool", "items": [{"type": "device", "entity_id": "light.pool", "access": "control"}]},
    {"id": "office", "name": "Office", "items": [{"type": "device", "entity_id": "switch.office", "access": "view"}]},
], "page_groups": []}


def test_no_role_means_no_pages_not_every_page(tmp_path, monkeypatch):
    previous, main.db = main.db, Database(str(tmp_path))
    previous.conn.close()
    monkeypatch.setattr(main, "offered", lambda eid: True)
    monkeypatch.setattr(main, "controllable", lambda eid: True)
    areas = main.AreaLayout([], {}, {})
    db = main.db

    nobody = db.staff(db.create_staff("Sam", "1234"))
    assert nobody["role_id"] is None
    assert main.person_page_choice(nobody) == []
    assert main.person_pages(nobody, LAYOUT) == []
    # Nothing comes from the layout's own access settings either.
    assert main.person_access(nobody, areas, LAYOUT) == {}

    # Pages picked for them still count, with those pages' access.
    db.update_staff(nobody["id"], layout_pages=json.dumps(["pool"]))
    picked = db.staff(nobody["id"])
    assert main.person_pages(picked, LAYOUT) == ["pool"]
    assert main.person_access(picked, areas, LAYOUT) == {"light.pool": "control"}

    # A role that shows every page still means every page.
    role_id = db.create_role("Front desk")
    with_role = db.create_staff("Ana", "5678")
    db.update_staff(with_role, role_id=role_id)
    member = db.staff(with_role)
    assert main.person_page_choice(member) is None
    assert main.person_pages(member, LAYOUT) is None
    db.set_role_pages(role_id, ["office"])
    assert main.person_pages(db.staff(with_role), LAYOUT) == ["office"]
