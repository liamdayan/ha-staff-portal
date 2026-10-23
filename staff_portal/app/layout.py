"""Custom phone layouts: pages made of headings, single devices, whole areas and group cards.

The layout only decides how things are arranged. What a phone can see or control still comes
from its role: when a layout is sent to a phone, anything the role doesn't allow is removed.
"""

import re

from permissions import offered

MAX_PAGES = 20
MAX_ITEMS = 200
MAX_GROUP_ENTITIES = 40
MAX_SUMMARY = 4
MAX_PHONE_PAGES = 60
ITEM_TYPES = ("heading", "device", "area", "group")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MORE_PAGE_ID = "more"  # the automatic page for allowed devices that aren't on any page


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _id(value, fallback: str) -> str:
    value = str(value or "")
    return value if ID_RE.match(value) else fallback


def clean_layout(data) -> dict:
    """Validate a layout sent from the admin dashboard. Unknown fields are dropped."""
    if not isinstance(data, dict):
        raise ValueError("layout must be an object")
    raw_pages = [p for p in (data.get("pages") or [])[:MAX_PAGES] if isinstance(p, dict)]
    # IDs the admin sent explicitly. A page whose ID is missing, "more" or a repeat gets a new
    # one that clashes with none of these, so no other page's ID (or a phone's choice) moves.
    explicit = {str(p.get("id")) for p in raw_pages if ID_RE.match(str(p.get("id") or ""))}
    used: set[str] = set()
    pages = []
    for p_index, page in enumerate(raw_pages):
        items = []
        for i_index, item in enumerate((page.get("items") or [])[:MAX_ITEMS]):
            if not isinstance(item, dict) or item.get("type") not in ITEM_TYPES:
                continue
            kind = item["type"]
            base = {"id": _id(item.get("id"), f"p{p_index}i{i_index}"), "type": kind}
            if kind == "heading":
                title = _text(item.get("title"), 60)
                if title:
                    items.append({**base, "title": title})
            elif kind == "device":
                eid = str(item.get("entity_id") or "")
                if offered(eid):
                    items.append({**base, "entity_id": eid, "label": _text(item.get("label"), 40)})
            elif kind == "area":
                try:
                    area_id = int(item.get("area_id"))
                except (TypeError, ValueError):
                    continue
                items.append({**base, "area_id": area_id, "title": _text(item.get("title"), 60)})
            elif kind == "group":
                members, summary_count = [], 0
                for member in (item.get("entities") or [])[:MAX_GROUP_ENTITIES]:
                    if not isinstance(member, dict) or not offered(str(member.get("entity_id") or "")):
                        continue
                    summary = bool(member.get("summary")) and summary_count < MAX_SUMMARY
                    summary_count += summary
                    members.append({"entity_id": member["entity_id"],
                                    "label": _text(member.get("label"), 40), "summary": summary})
                title = _text(item.get("title"), 60)
                if title and members:
                    items.append({**base, "title": title, "entities": members})
        page_id = str(page.get("id") or "")
        if not ID_RE.match(page_id) or page_id == MORE_PAGE_ID or page_id in used:
            page_id = _free_id(f"p{p_index}", used | explicit | {MORE_PAGE_ID})
        used.add(page_id)
        pages.append({"id": page_id,
                      "name": _text(page.get("name"), 30) or f"Page {p_index + 1}",
                      "items": items})
    return {"pages": pages, "show_unplaced": bool(data.get("show_unplaced", True))}


def clean_phone_pages(value) -> list[str] | None:
    """A phone's page choice from the admin dashboard: None (all pages) or a list of page IDs."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("layout_pages must be a list of page IDs or null")
    ids: list[str] = []
    for item in value:
        if isinstance(item, str) and ID_RE.match(item) and item not in ids:
            ids.append(item)
    if len(ids) > MAX_PHONE_PAGES:
        raise ValueError(f"a phone can be given at most {MAX_PHONE_PAGES} pages")
    return ids


def visible_layout(layout: dict, access: dict[str, str], areas,
                   page_ids: list[str] | None = None) -> dict | None:
    """The layout as one phone should see it, or None to use the automatic layout.

    `access` is the phone's {entity_id: level}; `areas` is the current AreaLayout.
    `page_ids` limits which pages this phone shows (None = all). The More page only holds
    devices that aren't on *any* page, so leaving a page off one phone doesn't move its
    devices to that phone's More page.
    """
    if not layout or not layout.get("pages"):
        return None
    wanted = None if page_ids is None else set(page_ids)
    placed: set[str] = set()
    pages = []
    for page in layout["pages"]:
        items = []
        for item in page["items"]:
            kind = item["type"]
            if kind == "heading":
                items.append(item)
            elif kind == "device":
                if item["entity_id"] in access:
                    items.append(item)
                    placed.add(item["entity_id"])
            elif kind == "group":
                members = [m for m in item["entities"] if m["entity_id"] in access]
                if members:
                    items.append({**item, "entities": members})
                    placed.update(m["entity_id"] for m in members)
            elif kind == "area":
                ids = sorted((eid for eid in access
                              if item["area_id"] in areas.membership.get(eid, [])))
                if ids:
                    title = item["title"] or areas.names.get(item["area_id"], "")
                    items.append({**item, "title": title, "entity_ids": ids})
                    placed.update(ids)
        items = _drop_empty_headings(items)
        if items and (wanted is None or page["id"] in wanted):
            pages.append({**page, "items": items})

    if layout.get("show_unplaced", True) and (wanted is None or MORE_PAGE_ID in wanted):
        rest = [eid for eid in access if eid not in placed]
        if rest:
            items = []
            by_group: dict[tuple, list[str]] = {}
            for eid in rest:
                by_group.setdefault(areas.group(eid), []).append(eid)
            for (_, heading), ids in sorted(by_group.items()):
                items.append({"id": f"more-{len(items)}", "type": "area", "area_id": None,
                              "title": heading or "Other", "entity_ids": sorted(ids)})
            pages.append({"id": MORE_PAGE_ID, "name": "More", "items": items})
    return {"pages": pages}


def _free_id(base: str, taken: set[str]) -> str:
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def _drop_empty_headings(items: list[dict]) -> list[dict]:
    """Remove headings with nothing under them (everything below was filtered out)."""
    result = []
    for index, item in enumerate(items):
        if item["type"] == "heading":
            following = next((i for i in items[index + 1:]), None)
            if following is None or following["type"] == "heading":
                continue
        result.append(item)
    return result
