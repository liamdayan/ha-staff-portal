"""Custom phone layouts: pages made of headings, single devices, whole areas and group cards.

The layout decides how things are arranged. What a phone can see or control comes from its
role, unless a layout item overrides it ("access"): an override applies on every phone that shows
that item's page, and can give more or less than the role does.

Pages can be put in page groups (such as "Pool staff"), so a whole set of pages can be given to
phones at once: a phone's page choice can name a group ("group:<id>"), which stands for every
page in that group, including pages added to it later.
"""

import re

from permissions import offered

MAX_PAGES = 20
MAX_ITEMS = 200
MAX_GROUP_ENTITIES = 40
MAX_GROUP_DIVIDERS = 20  # named dividers that split a group card's detail screen into sections
MAX_SUMMARY = 4
MAX_PHONE_PAGES = 60
MAX_PAGE_GROUPS = 12
GROUP_PREFIX = "group:"  # in a phone's page choice: every page in that page group
ITEM_TYPES = ("heading", "device", "area", "group")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
ACCESS_LEVELS = ("none", "view", "control")  # an item's override; missing = use the role's
ACCESS_RANK = {"none": 0, "view": 1, "control": 2}
MORE_PAGE_ID = "more"  # the automatic page for allowed devices that aren't on any page


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _access(item: dict) -> dict:
    """The item's access override as a field to merge in, or nothing when it follows the role."""
    level = item.get("access")
    return {"access": level} if level in ACCESS_LEVELS else {}


def _collapsed(item: dict) -> dict:
    """Headings and area sections can start folded on phones (each phone remembers its own)."""
    return {"collapsed": True} if item.get("collapsed") is True else {}


def _id(value, fallback: str) -> str:
    value = str(value or "")
    return value if ID_RE.match(value) else fallback


def clean_layout(data) -> dict:
    """Validate a layout sent from the admin dashboard. Unknown fields are dropped."""
    if not isinstance(data, dict):
        raise ValueError("layout must be an object")
    groups, group_ids = [], set()
    raw_groups = [g for g in (data.get("page_groups") or [])[:MAX_PAGE_GROUPS] if isinstance(g, dict)]
    # As with pages below: a replacement ID never takes one another group was sent with.
    explicit_groups = {str(g.get("id")) for g in raw_groups if ID_RE.match(str(g.get("id") or ""))}
    for g_index, group in enumerate(raw_groups):
        group_id = str(group.get("id") or "")
        if not ID_RE.match(group_id) or group_id in group_ids:
            group_id = _free_id(f"g{g_index}", group_ids | explicit_groups)
        group_ids.add(group_id)
        groups.append({"id": group_id, "name": _text(group.get("name"), 30) or f"Group {g_index + 1}"})
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
                    items.append({**base, "title": title, **_collapsed(item)})
            elif kind == "device":
                eid = str(item.get("entity_id") or "")
                if offered(eid):
                    items.append({**base, "entity_id": eid, "label": _text(item.get("label"), 40),
                                  **_access(item)})
            elif kind == "area":
                try:
                    area_id = int(item.get("area_id"))
                except (TypeError, ValueError):
                    continue
                items.append({**base, "area_id": area_id, "title": _text(item.get("title"), 60),
                              **_access(item), **_collapsed(item)})
            elif kind == "group":
                members, summary_count, devices, dividers = [], 0, 0, 0
                for member in (item.get("entities") or [])[:MAX_GROUP_ENTITIES + MAX_GROUP_DIVIDERS]:
                    if not isinstance(member, dict):
                        continue
                    if member.get("type") == "divider":
                        title = _text(member.get("title"), 40)
                        if title and dividers < MAX_GROUP_DIVIDERS:
                            dividers += 1
                            members.append({"type": "divider", "title": title})
                        continue
                    if devices >= MAX_GROUP_ENTITIES or not offered(str(member.get("entity_id") or "")):
                        continue
                    devices += 1
                    summary = bool(member.get("summary")) and summary_count < MAX_SUMMARY
                    summary_count += summary
                    members.append({"entity_id": member["entity_id"],
                                    "label": _text(member.get("label"), 40), "summary": summary,
                                    **_access(member)})
                title = _text(item.get("title"), 60)
                if title and devices:
                    items.append({**base, "title": title, "entities": members, **_access(item)})
        page_id = str(page.get("id") or "")
        if not ID_RE.match(page_id) or page_id == MORE_PAGE_ID or page_id in used:
            page_id = _free_id(f"p{p_index}", used | explicit | {MORE_PAGE_ID})
        used.add(page_id)
        group_id = str(page.get("page_group") or "")
        pages.append({"id": page_id,
                      "name": _text(page.get("name"), 30) or f"Page {p_index + 1}",
                      "items": items,
                      **({"page_group": group_id} if group_id in group_ids else {})})
    return {"pages": _groups_together(pages), "page_groups": groups,
            "show_unplaced": bool(data.get("show_unplaced", True))}


def _groups_together(pages: list[dict]) -> list[dict]:
    """A group's pages side by side, where its first page is, so phones show pages in the same
    order as the editor (which lists each group's pages together)."""
    result, placed = [], set()
    for page in pages:
        group_id = page.get("page_group")
        if not group_id:
            result.append(page)
        elif group_id not in placed:
            placed.add(group_id)
            result.extend(p for p in pages if p.get("page_group") == group_id)
    return result


def group_pages(layout: dict | None) -> dict[str, list[str]]:
    """{page group ID: its page IDs, in order}."""
    members: dict[str, list[str]] = {}
    for page in (layout or {}).get("pages", []):
        if page.get("page_group"):
            members.setdefault(page["page_group"], []).append(page["id"])
    return members


def expand_page_choice(layout: dict | None, choice: list[str] | None) -> list[str] | None:
    """A phone's page choice as page IDs (None: every page). A group entry stands for every
    page in that group now."""
    if choice is None:
        return None
    members = group_pages(layout)
    result: list[str] = []
    for entry in choice:
        ids = members.get(entry[len(GROUP_PREFIX):], []) if entry.startswith(GROUP_PREFIX) else [entry]
        result.extend(i for i in ids if i not in result)
    return result


def keep_removed_groups(choice: list[str] | None, old: dict, new: dict) -> list[str] | None:
    """A phone's page choice after a layout save: a page group that no longer exists is replaced
    by the pages it had, so deleting a group doesn't take its pages away from phones."""
    if choice is None:
        return None
    groups = {g["id"] for g in new.get("page_groups", [])}
    pages = {p["id"] for p in new.get("pages", [])}
    old_members = group_pages(old)
    result: list[str] = []
    for entry in choice:
        if entry.startswith(GROUP_PREFIX) and entry[len(GROUP_PREFIX):] not in groups:
            kept = [i for i in old_members.get(entry[len(GROUP_PREFIX):], []) if i in pages]
        else:
            kept = [entry]
        result.extend(i for i in kept if i not in result)
    return result


def clean_phone_pages(value) -> list[str] | None:
    """A phone's page choice from the admin dashboard: None (all pages) or a list of page IDs
    and page groups ("group:<id>")."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("layout_pages must be a list of page IDs or null")
    ids: list[str] = []
    for item in value:
        if not isinstance(item, str) or item in ids:
            continue
        if ID_RE.match(item[len(GROUP_PREFIX):] if item.startswith(GROUP_PREFIX) else item):
            ids.append(item)
    if len(ids) > MAX_PHONE_PAGES:
        raise ValueError(f"a phone can be given at most {MAX_PHONE_PAGES} pages")
    return ids


def layout_overrides(layout: dict, areas, page_ids: list[str] | None = None) -> dict[str, str]:
    """{entity_id: 'none'|'view'|'control'} set by layout items on the pages this phone shows.

    A device in a group card uses its own override if it has one, otherwise the card's. When
    the same device is overridden in several places, the highest level wins.
    """
    wanted = None if page_ids is None else set(page_ids)
    result: dict[str, str] = {}

    def put(eid: str, level: str | None) -> None:
        if level in ACCESS_LEVELS and ACCESS_RANK[level] >= ACCESS_RANK.get(result.get(eid), -1):
            result[eid] = level

    for page in (layout or {}).get("pages", []):
        if wanted is not None and page["id"] not in wanted:
            continue
        for item in page["items"]:
            kind = item["type"]
            if kind == "device":
                put(item["entity_id"], item.get("access"))
            elif kind == "group":
                for member in item["entities"]:
                    if "entity_id" in member:
                        put(member["entity_id"], member.get("access") or item.get("access"))
            elif kind == "area" and item.get("access"):
                for eid, area_ids in areas.membership.items():
                    if item["area_id"] in area_ids:
                        put(eid, item["access"])
    return result


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
                members = _drop_empty_dividers(
                    [m for m in item["entities"] if "entity_id" not in m or m["entity_id"] in access])
                if any("entity_id" in m for m in members):
                    items.append({**item, "entities": members})
                    placed.update(m["entity_id"] for m in members if "entity_id" in m)
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
                # ID from the heading (not the position) so a phone's own order sticks.
                items.append({"id": f"more:{heading or 'Other'}"[:80], "type": "area", "area_id": None,
                              "title": heading or "Other", "entity_ids": sorted(ids)})
            pages.append({"id": MORE_PAGE_ID, "name": "More", "items": items})
    return {"pages": pages}


def _free_id(base: str, taken: set[str]) -> str:
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def _drop_empty_dividers(members: list[dict]) -> list[dict]:
    """Remove group card dividers with no device under them (everything was filtered out)."""
    result = []
    for index, member in enumerate(members):
        if "entity_id" not in member:
            following = members[index + 1] if index + 1 < len(members) else None
            if following is None or "entity_id" not in following:
                continue
        result.append(member)
    return result


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


# ---------------------------------------------------------------- personal order on the phone

MAX_ORDER_ENTRIES = 300
ORDER_KEY_MAX = 80
AUTO_PAGE_ID = "auto"  # the automatic layout (no pages), whose sections are keyed by heading


def _order_key(value) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= ORDER_KEY_MAX else None


def clean_phone_order(data) -> dict:
    """Validate an arrangement sent by a phone: page IDs and, per page, item IDs."""
    if not isinstance(data, dict):
        raise ValueError("order must be an object")
    pages = [k for k in (data.get("pages") or [])[:MAX_ORDER_ENTRIES] if _order_key(k)]
    items = {}
    raw_items = data.get("items") or {}
    if not isinstance(raw_items, dict):
        raise ValueError("items must be an object")
    for page_id, ids in list(raw_items.items())[:MAX_ORDER_ENTRIES]:
        if _order_key(page_id) and isinstance(ids, list):
            items[page_id] = [k for k in ids[:MAX_ORDER_ENTRIES] if _order_key(k)]
    return {"pages": list(dict.fromkeys(pages)),
            "items": {k: list(dict.fromkeys(v)) for k, v in items.items()}}


def _arrange(things: list, key, preferred: list[str]) -> list:
    """Things in the preferred order; anything not mentioned keeps its default order at the end."""
    rank = {k: i for i, k in enumerate(preferred)}
    known = sorted((t for t in things if key(t) in rank), key=lambda t: rank[key(t)])
    return known + [t for t in things if key(t) not in rank]


def apply_phone_order(layout: dict, order: dict | None) -> dict:
    """A phone's visible layout rearranged by its own order. Only the arrangement changes;
    nothing is added, so the role filtering already applied stays intact."""
    if not order:
        return layout
    pages = _arrange(layout["pages"], lambda p: p["id"], order.get("pages", []))
    pages = [{**p, "items": _arrange(p["items"], lambda i: i["id"], order.get("items", {}).get(p["id"], []))}
             for p in pages]
    return {**layout, "pages": pages}


def apply_auto_order(entities: list[dict], order: dict | None) -> list[dict]:
    """The automatic layout's sections (grouped by heading) in the phone's own order."""
    preferred = (order or {}).get("items", {}).get(AUTO_PAGE_ID, [])
    if not preferred:
        return entities
    groups: dict[str, list[dict]] = {}
    for e in entities:
        groups.setdefault(e["area"] or "Other", []).append(e)
    ordered = _arrange(list(groups.items()), lambda g: g[0], preferred)
    return [e for _, members in ordered for e in members]
