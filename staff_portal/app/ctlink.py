"""Matching staff to Connecteam users by name, for Link existing staff (main.py).

Nothing here links anyone: it only suggests, and the owner confirms. A suggestion is made only
when it's the one clear match both ways: one Connecteam user for the person, and no one else here
matching that user as well.
"""

import unicodedata

# How close a match is, best first, and how the dashboard says it.
SAME, SWAPPED, NICKNAME, FIRST_NAME = 1, 2, 3, 4
WHY = {SAME: "Same name", SWAPPED: "Same name, first and last swapped",
       NICKNAME: "Same surname, first name looks like a short form",
       FIRST_NAME: "Same first name"}

# Common short forms, each with its full names. Any two names in a group count as the same.
NICKNAMES = [
    {"robert", "rob", "robbie", "bob", "bobby"}, {"william", "will", "bill", "billy", "liam"},
    {"elizabeth", "liz", "lizzie", "beth", "betty", "eliza"}, {"margaret", "maggie", "meg", "peggy"},
    {"richard", "rick", "ricky", "dick"}, {"james", "jim", "jimmy", "jamie"},
    {"john", "jack", "johnny"}, {"edward", "ed", "eddie", "ted", "ned"},
    {"katherine", "catherine", "kathryn", "kate", "katie", "kathy", "cathy", "kat"},
    {"jennifer", "jen", "jenny"}, {"michael", "mike", "mikey", "mick"},
    {"anthony", "tony"}, {"charles", "charlie", "chuck"}, {"thomas", "tom", "tommy"},
    {"joseph", "joe", "joey"}, {"daniel", "dan", "danny"}, {"david", "dave"},
    {"steven", "stephen", "steve"}, {"susan", "sue", "suzy"}, {"patricia", "pat", "patty", "trish"},
    {"alexander", "alex", "sandy"}, {"alexandra", "alex", "sandra"}, {"henry", "harry", "hank"},
    {"nicholas", "nick", "nicky"}, {"benjamin", "ben", "benny"}, {"samuel", "sam", "sammy"},
    {"samantha", "sam", "sammy"}, {"rebecca", "becky", "becca"}, {"deborah", "debra", "deb", "debbie"},
    {"victoria", "vicky", "tori"}, {"lawrence", "larry"}, {"gregory", "greg"},
    {"matthew", "matt"}, {"christopher", "chris"}, {"christine", "chris", "christy"},
    {"andrew", "andy", "drew"}, {"timothy", "tim"}, {"kenneth", "ken", "kenny"},
    {"jonathan", "jon"}, {"abigail", "abby"}, {"francis", "frank"}, {"frances", "fran"},
    {"peter", "pete"}, {"ronald", "ron"}, {"donald", "don"}, {"raymond", "ray"},
]
NICKNAME_GROUPS: dict[str, list[set]] = {}
for _group in NICKNAMES:
    for _name in _group:
        NICKNAME_GROUPS.setdefault(_name, []).append(_group)


def name_tokens(name) -> tuple[str, ...]:
    """A name as lower-case words without accents or punctuation ("José O'Neil-Ryan" ->
    ("jose", "oneil", "ryan")). Numbers are dropped, so "Bob (2)" is ("bob",)."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold().replace("'", "")
    words = "".join(c if c.isalnum() else " " for c in text).split()
    return tuple(w for w in words if not w.isdigit())


def nickname_like(a: str, b: str) -> bool:
    """Whether two first names could be one person's: the same, a known short form, or one the
    start of the other (at least three letters: "Chris", "Christopher")."""
    if a == b:
        return True
    if any(b in group for group in NICKNAME_GROUPS.get(a, ())):
        return True
    return min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a))


def match_level(staff_name, user_name) -> int | None:
    """How well a staff member's name matches a Connecteam user's (SAME... FIRST_NAME), or None."""
    s, u = name_tokens(staff_name), name_tokens(user_name)
    if not s or not u:
        return None
    if s == u:
        return SAME
    if sorted(s) == sorted(u):
        return SWAPPED
    if len(s) > 1 and len(u) > 1 and s[-1] == u[-1] and nickname_like(s[0], u[0]):
        return NICKNAME
    if len(s) == 1 and nickname_like(s[0], u[0]):
        return FIRST_NAME
    return None


def same_name(a, b) -> bool:
    """The strict match "Add everyone from Connecteam" holds back for: the same words, perhaps
    swapped round."""
    return match_level(a, b) in (SAME, SWAPPED)


def suggest(staff: list[dict], users: list[dict]) -> dict[int, tuple[dict, int]]:
    """{staff id: (Connecteam user, level)} for each person ({"id", "name"}) with one clear match
    among users ({"id", "name"}). The best level wins; a tie at it is no suggestion, and so is a
    user who's the best match for two people at the same level."""
    best: dict[int, tuple[dict, int]] = {}
    for m in staff:
        found = [(u, level) for u in users if (level := match_level(m["name"], u["name"]))]
        if not found:
            continue
        top = min(level for _, level in found)
        at_top = [u for u, level in found if level == top]
        if len(at_top) == 1:
            best[m["id"]] = (at_top[0], top)
    # One user, one person: kept only for the person it matches best, and only if that's one.
    by_user: dict[str, list[tuple[int, int]]] = {}
    for staff_id, (u, level) in best.items():
        by_user.setdefault(u["id"], []).append((level, staff_id))
    for claims in by_user.values():
        if len(claims) > 1:
            claims.sort()
            keep = claims[0][1] if claims[0][0] < claims[1][0] else None
            for _, staff_id in claims:
                if staff_id != keep:
                    best.pop(staff_id)
    return best
