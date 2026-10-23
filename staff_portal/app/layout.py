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
ITEM_TYPES = ("heading", "device", "area", "group")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _id(value, fallback: str) -> str:
    value = str(value or "")
    return value if ID_RE.match(value) else fallback


def clean_layout(data) -> dict:
    """Validate a layout sent from the admin dashboard. Unknown fields are dropped."""
    if not isinstance(data, dict):
        raise ValueError("layout must be an object")
    pages = []
    for p_index, page in enumerate((data.get("pages") or [])[:MAX_PAGES]):
        if not isinstance(page, dict):
            continue
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
        pages.append({"id": _id(page.get("id"), f"p{p_index}"),
                      "name": _text(page.get("name"), 30) or f"Page {p_index + 1}",
                      "items": items})
    return {"pages": pages, "show_unplaced": bool(data.get("show_unplaced", True))}


def visible_layout(layout: dict, access: dict[str, str], areas) -> dict | None:
    """The layout as one phone should see it, or None to use the automatic layout.

    `access` is the phone's {entity_id: level}; `areas` is the current AreaLayout.
    """
    if not layout or not layout.get("pages"):
        return None
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
        if items:
            pages.append({**page, "items": items})

    if layout.get("show_unplaced", True):
        rest = [eid for eid in access if eid not in placed]
        if rest:
            items = []
            by_group: dict[tuple, list[str]] = {}
            for eid in rest:
                by_group.setdefault(areas.group(eid), []).append(eid)
            for (_, heading), ids in sorted(by_group.items()):
                items.append({"id": f"more-{len(items)}", "type": "area", "area_id": None,
                              "title": heading or "Other", "entity_ids": sorted(ids)})
            pages.append({"id": "more", "name": "More", "items": items})
    return {"pages": pages}


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
