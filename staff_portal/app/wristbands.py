"""Wristbands: who may make which TT Hotel wristbands and key cards, and what goes on each one.

The card encoder writes one record per door (or per floor, per building, or one "all doors"
record), and the locks check the card on their own, offline. A mistake here is a door that
opens for the wrong person, so every rule lives in this module and main.py only calls plan()
and options():

- Permissions are checked again on every request; nothing from the phone or computer is trusted.
- Damaged permission data means no access, never every door.
- Building and floor numbers start at 1: to the encoder, building 0 means "every door" and
  floor 0 means "the whole building", so a zero would silently widen a card.
- Expiry times are wall-clock times in the property's time zone. A time that doesn't exist on
  a daylight-saving day moves forward past the gap; one that happens twice uses the first.
- A door is marked a public door, a guest room or a staff door (wb_doors.kind; NULL = not marked
  yet, which counts as a public door and a guest room). Rooms go on overnight wristbands; public
  doors are the shared doors. Staff doors only go on staff cards and contractor badges.
- Contractor badges open doors picked from anywhere in the maker's own door scope, and last at
  most CONTRACTOR_DAYS days.

Permission JSON (roles.wristbands; staff.wristbands where NULL means "same as their role"):
{"types": ["day_pass", "overnight", "staff", "contractor"],
 "staff_levels": ["door", "floor", "building", "all"],
 "doors": {"all": true} | {"buildings": [n], "floors": [[building, floor]], "doors": [lock id]},
 "edit_doors": bool, "edit_end_time": bool, "max_nights": n (0 = no limit),
 "max_staff_days": n (0 = no limit), "see_all_cards": bool}
"""

import copy
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TYPES = ("day_pass", "overnight", "staff", "contractor")
LEVELS = ("door", "floor", "building", "all")   # staff-card record levels, narrowest first
BLANK_MAC = "000000000000"     # what floor, building and all-door records carry instead of a lock
MAX_BUILDING = 254             # the encoder's range for building numbers...
MAX_FLOOR = 255                # ...and floor numbers
MAX_LOCK_ID = 2 ** 53          # TT Hotel lock IDs are positive whole numbers
MAX_NAME = 80                  # a guest's or staff member's name on a card job
MAX_LIST = 256                 # doors, rooms or access choices in one request
MAX_SCOPE = 5000               # buildings, floors or doors in one permission
MAX_LIMIT = 3650               # max_nights and max_staff_days (ten years)
MAX_DESCRIBED = 4              # door names in a summary before "+2 more"
MAX_TIMESTAMP = 2 ** 32 - 1    # the encoder takes a 32-bit expiry
STAFF_CARD_TIME = "23:59"      # staff cards last until the end of the chosen day
LATEST_END_TIME = "23:59"      # the latest a day pass's end time can be changed to (still today)
# wb_doors.kind: a public door, a guest room or a staff door; NULL = not marked
DOOR_KINDS = ("public", "room", "staff")
KIND_WORDS = {"public": "a public door", "room": "a guest room", "staff": "a staff door"}
CONTRACTOR_DAYS = 7            # the longest a contractor badge lasts (a hard limit)
CONTRACTOR_TIME = "23:59"      # a contractor badge's form starts at the end of today
# The last date an expiry can fall on, so far-off dates are refused before any time-zone math.
LAST_DATE = datetime.fromtimestamp(MAX_TIMESTAMP, timezone.utc).date()

NO_ACCESS = {"types": [], "staff_levels": [], "doors": {"buildings": [], "floors": [], "doors": []},
             "edit_doors": False, "edit_end_time": False, "max_nights": 0, "max_staff_days": 0,
             "see_all_cards": False}

# Used when a stored setting is missing or damaged (the same defaults as db.DEFAULT_SETTINGS).
SETTING_DEFAULTS = {
    "wristbands_enabled": "0", "wb_daypass_end": "20:00", "wb_daypass_doors": "[]",
    "wb_daypass_max": "20", "wb_checkout_time": "11:00", "wb_overnight_doors": "[]",
    "wb_overnight_max": "20", "wb_staff_days": "90", "wb_staff_max": "5", "wb_lockout_daypass": "0",
    "wb_lockout_overnight": "0", "wb_lockout_staff": "1", "wb_max_records": "20",
    "wb_contractor_max": "5", "wb_lockout_contractor": "0",
}
NUMBER_RANGES = {"wb_daypass_max": (1, 50), "wb_overnight_max": (1, 50),
                 "wb_staff_days": (1, 3650), "wb_staff_max": (1, 20), "wb_max_records": (1, 64),
                 "wb_contractor_max": (1, 20)}

NOUNS = {"day_pass": "day passes", "overnight": "overnight wristbands", "staff": "staff cards",
         "contractor": "contractor badges"}
LEVEL_REFUSALS = {"all": "You can't make cards that open every door.",
                  "building": "You can't make building cards.",
                  "floor": "You can't make floor cards.",
                  "door": "You can't make staff cards for single doors."}
NO_DAY_PASS_DOORS = "No day-pass doors are set up yet. Ask a manager to choose them."
TOO_FAR = "That date is too far away. Pick an earlier date."
TOO_MANY_DOORS = "That's too many doors for one card. Use floor or building access instead."
TURNED_OFF = "Wristbands are turned off. Ask a manager to turn them on."
NOT_ALLOWED = "You don't have access to make wristbands. Ask a manager if you need it."
CONTRACTOR_TOO_LONG = f"Contractor badges can last at most {CONTRACTOR_DAYS} days."
NO_CONTRACTOR_DOORS = "There are no doors you can put on a badge. Ask a manager."

HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAC_RE = re.compile(r"^[0-9A-F]{12}$")


class PlanError(ValueError):
    """A card request that can't be made. message is shown to staff; field names the part of
    the form to fix; status is 400 (fix the input) or 403 (not allowed)."""

    def __init__(self, message: str, field: str | None = None, status: int = 400):
        super().__init__(message)
        self.message = message
        self.field = field
        self.status = status


@dataclass(frozen=True)
class Record:
    """One record written onto a card (one CE_WriteCard call). door is the lock ID for door
    records, else None. asdict() gives the JSON stored in wb_jobs.records."""
    level: str          # door | floor | building | all
    building: int
    floor: int
    mac: str            # 12 hex characters; BLANK_MAC unless level is "door"
    door: int | None
    label: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Plan:
    """A checked card request, ready to become a card job."""
    type: str
    name: str
    count: int
    records: list[Record]
    summary: str          # "Pool Gate, Gym", for people to read
    expires_at: int       # unix seconds
    expires_label: str    # "8:00 PM today"
    allow_lockout: bool   # may open a deadbolted door

    def public(self) -> dict:
        """The plan as the preview shows it (no lock MACs)."""
        return {"type": self.type, "name": self.name, "count": self.count,
                "records": [{"level": r.level, "label": r.label} for r in self.records],
                "summary": self.summary, "expires_at": self.expires_at,
                "expires_label": self.expires_label}


# ---------------------------------------------------------------- permissions

def _whole(value) -> int | None:
    """A whole number from JSON (or digits in a string); None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit() and len(value) <= 16:
        return int(value)
    return None


def _flag(raw: dict, key: str) -> bool:
    value = raw.get(key, False)
    if value in (True, False, 0, 1) and not isinstance(value, float):
        return bool(value)
    raise ValueError(f"Wristband setting {key} must be on or off.")


def _limit(raw: dict, key: str) -> int:
    value = _whole(raw.get(key, 0))
    if value is None or not 0 <= value <= MAX_LIMIT:
        raise ValueError(f"Wristband limits must be whole numbers from 0 to {MAX_LIMIT}.")
    return value


def _choices(raw, allowed: tuple, what: str) -> list[str]:
    if not isinstance(raw, (list, tuple)) or len(raw) > 20 or not all(v in allowed for v in raw):
        raise ValueError(f"Choose {what} from the list.")
    return [v for v in allowed if v in raw]


def _numbers(raw, hi: int, lo: int = 0) -> list[int]:
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)) or len(raw) > MAX_SCOPE:
        raise ValueError("Choose doors from the list.")
    values = [_whole(v) for v in raw]
    if any(v is None or not lo <= v <= hi for v in values):
        raise ValueError("Choose doors from the list.")
    return sorted(set(values))


def _clean_doors(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Choose doors from the list.")
    if raw.get("all") is True:
        return {"all": True}
    buildings = _numbers(raw.get("buildings"), MAX_BUILDING)
    floors = raw.get("floors") or []
    if not isinstance(floors, (list, tuple)) or len(floors) > MAX_SCOPE:
        raise ValueError("Choose floors from the list.")
    pairs = set()
    for pair in floors:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("Choose floors from the list.")
        b, f = _whole(pair[0]), _whole(pair[1])
        if b is None or f is None or not 0 <= b <= MAX_BUILDING or not 0 <= f <= MAX_FLOOR:
            raise ValueError("Choose floors from the list.")
        if b not in buildings:   # a floor inside a chosen building adds nothing
            pairs.add((b, f))
    return {"buildings": buildings, "floors": [list(p) for p in sorted(pairs)],
            "doors": _numbers(raw.get("doors"), MAX_LOCK_ID, 1)}


def clean_permissions(raw) -> dict | None:
    """An admin's wristband permission for a role or a person, checked and tidied. None stays
    None (a role that makes no wristbands, or a person who follows their role). Keys left out
    mean "no"; unknown keys are dropped. Raises ValueError with a message for the admin."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("Wristband permissions must be an object.")
    return {"types": _choices(raw.get("types", []), TYPES, "card types"),
            "staff_levels": _choices(raw.get("staff_levels", []), LEVELS, "staff card access"),
            "doors": _clean_doors(raw.get("doors", {})),
            "edit_doors": _flag(raw, "edit_doors"),
            "edit_end_time": _flag(raw, "edit_end_time"),
            "max_nights": _limit(raw, "max_nights"),
            "max_staff_days": _limit(raw, "max_staff_days"),
            "see_all_cards": _flag(raw, "see_all_cards")}


def no_access() -> dict:
    """A fresh copy of NO_ACCESS (callers may change what they get)."""
    return copy.deepcopy(NO_ACCESS)


def permission_choice(raw) -> dict | None:
    """A stored wristband permission: None when nothing is stored (NULL), otherwise a clean
    permission. A damaged value means no access rather than every door, so it can never widen
    what anyone may make. Takes the stored JSON text or an already-read dict."""
    if raw is None or raw == "":
        return None
    try:
        value = raw if isinstance(raw, dict) else json.loads(raw)
        clean = clean_permissions(value)
    except (TypeError, ValueError, RecursionError):
        return no_access()
    return clean if clean is not None else no_access()


def _scope(perm: dict | None) -> tuple[bool, set, set, set]:
    """(all doors, buildings, (building, floor) pairs, door IDs) from a permission; anything
    malformed counts as nothing."""
    doors = perm.get("doors") if isinstance(perm, dict) else None
    if not isinstance(doors, dict):
        return False, set(), set(), set()
    if doors.get("all") is True:
        return True, set(), set(), set()

    def items(key):
        value = doors.get(key)
        return value if isinstance(value, list) else []
    floors = {tuple(p) for p in items("floors") if isinstance(p, list) and len(p) == 2}
    return False, set(items("buildings")), floors, set(items("doors"))


def _types(perm: dict | None) -> list[str]:
    value = perm.get("types") if isinstance(perm, dict) else None
    return [t for t in TYPES if isinstance(value, list) and t in value]


def _levels(perm: dict | None) -> list[str]:
    value = perm.get("staff_levels") if isinstance(perm, dict) else None
    return [v for v in LEVELS if isinstance(value, list) and v in value]


def _perm_number(perm: dict | None, key: str) -> int:
    value = perm.get(key) if isinstance(perm, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _perm_flag(perm: dict | None, key: str) -> bool:
    return isinstance(perm, dict) and perm.get(key) is True


def door_in_scope(perm: dict | None, door: dict) -> bool:
    """Whether a door (a wb_doors row) is one this permission may put on cards: every door,
    its building, its building and floor, or the door itself."""
    every, buildings, floors, door_ids = _scope(perm)
    if every:
        return True
    d = _Door.of(door)
    if d is None:
        return False
    if d.id in door_ids:
        return True
    if d.building is None:
        return False
    return d.building in buildings or (d.floor is not None and (d.building, d.floor) in floors)


def _covers_building(perm: dict | None, building: int) -> bool:
    every, buildings, _, _ = _scope(perm)
    return every or building in buildings


def _covers_floor(perm: dict | None, building: int, floor: int) -> bool:
    every, buildings, floors, _ = _scope(perm)
    return every or building in buildings or (building, floor) in floors


# ---------------------------------------------------------------- doors and settings

def door_kind(value) -> str | None:
    """A door's kind as stored: 'public', 'room', 'staff', or None (not marked yet) for anything
    else."""
    return value if value in DOOR_KINDS else None


@dataclass(frozen=True)
class _Door:
    id: int
    name: str
    mac: str
    building: int | None
    floor: int | None
    building_name: str
    floor_name: str
    hidden: bool
    active: bool
    kind: str | None = None   # 'public', 'room', 'staff' or None (not marked: public or room)

    @classmethod
    def of(cls, row) -> "_Door | None":
        """A wb_doors row (or the same keys from TT Hotel's list), or None if it has no ID."""
        if not isinstance(row, dict):
            return None
        lock_id = _whole(row.get("lock_id", row.get("id")))
        if lock_id is None:
            return None

        def number(*keys):
            for key in keys:
                if key in row:
                    return _whole(row[key])
            return None

        def text(key):
            value = row.get(key)
            return value.strip() if isinstance(value, str) else ""
        name = text("name") or text("alias") or text("tt_name") or f"Door {lock_id}"
        return cls(id=lock_id, name=name,
                   mac=re.sub(r"[^0-9A-Fa-f]", "", str(row.get("mac") or "")).upper(),
                   building=number("building_number", "building"),
                   floor=number("floor_number", "floor"),
                   building_name=text("building_name"), floor_name=text("floor_name"),
                   hidden=bool(row.get("hidden")), active=bool(row.get("active", True)),
                   kind=door_kind(row.get("kind")))

    @property
    def building_label(self) -> str:
        if self.building is None:
            return ""
        return self.building_name or f"Building {self.building}"

    @property
    def floor_label(self) -> str:
        if self.floor is None:
            return ""
        return self.floor_name or f"Floor {self.floor}"


def _door_map(doors) -> dict[int, _Door]:
    rows = doors.values() if isinstance(doors, dict) else doors or []
    found = {}
    for row in rows:
        door = _Door.of(row)
        if door is not None:
            found[door.id] = door
    return found


def _setting(settings: dict, key: str):
    value = settings.get(key) if isinstance(settings, dict) else None
    return SETTING_DEFAULTS[key] if value is None else value


def setting_time(settings: dict, key: str) -> str:
    """An HH:MM setting, or its default when the stored value is damaged."""
    value = _setting(settings, key)
    return value if isinstance(value, str) and HHMM_RE.match(value) else SETTING_DEFAULTS[key]


def setting_number(settings: dict, key: str) -> int:
    lo, hi = NUMBER_RANGES[key]
    value = _whole(_setting(settings, key))
    return value if value is not None and lo <= value <= hi else int(SETTING_DEFAULTS[key])


def setting_flag(settings: dict, key: str) -> bool:
    return _setting(settings, key) in ("1", 1, True)


def setting_doors(settings: dict, key: str) -> tuple[int, ...]:
    """A JSON list of lock IDs; a damaged value means no doors."""
    value = _setting(settings, key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return ()
    if not isinstance(value, list):
        return ()
    ids = [_whole(v) for v in value[:MAX_LIST]]
    return tuple(dict.fromkeys(v for v in ids if v is not None and v >= 1))


def shared_doors(settings: dict, key: str, doors: dict) -> tuple[int, ...]:
    """The day-pass (wb_daypass_doors) or overnight (wb_overnight_doors) shared doors, leaving
    out staff doors: those never go on a guest's card, even while still listed in the setting
    (from a backup made before the door was marked, say). doors: by lock ID, as plan() and
    options() have them."""
    return tuple(i for i in setting_doors(settings, key)
                 if not (i in doors and doors[i].kind == "staff"))


def clean_hhmm(value) -> str:
    """A 24-hour time setting such as "20:00". Raises ValueError for the admin."""
    if isinstance(value, str) and HHMM_RE.match(value.strip()):
        return value.strip()
    raise ValueError("Times must be 24-hour times like 20:00.")


def parse_time_zone(name) -> ZoneInfo:
    """An IANA time zone such as "America/Los_Angeles". Raises ValueError."""
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        raise ValueError("Choose a time zone like America/Los_Angeles.")
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("That time zone isn't known. Use one like America/Los_Angeles.") from None


# ---------------------------------------------------------------- time

def local_today(now: float, tz: ZoneInfo) -> date:
    return datetime.fromtimestamp(now, tz).date()


def at_local_time(tz: ZoneInfo, day: date, hhmm: str) -> datetime:
    """A wall-clock time on a local date. A time inside a spring-forward gap moves forward past
    the gap; one that happens twice (fall-back) uses the first occurrence."""
    hours, minutes = hhmm.split(":")
    naive = datetime.combine(day, time(int(hours), int(minutes)))
    # fold=0 applies the offset from before the change, so a gap time lands after it.
    return naive.replace(tzinfo=tz, fold=0).astimezone(timezone.utc).astimezone(tz)


def format_time(when: datetime) -> str:
    """'8:00 PM'"""
    return when.strftime("%I:%M %p").lstrip("0")


def format_expiry(ts: float, now: float, tz: ZoneInfo) -> str:
    """'8:00 PM today', '11:00 AM tomorrow', '11:00 AM Fri, Oct 17', with the year when it
    isn't this year."""
    when = datetime.fromtimestamp(ts, tz)
    today = local_today(now, tz)
    if when.date() == today:
        day = "today"
    elif when.date() == today + timedelta(days=1):
        day = "tomorrow"
    elif when.year == today.year:
        day = f"{when:%a}, {when:%b} {when.day}"
    else:
        day = f"{when:%a}, {when:%b} {when.day}, {when.year}"
    return f"{format_time(when)} {day}"


# ---------------------------------------------------------------- reading a request

@dataclass(frozen=True)
class _Request:
    type: str
    name: str
    count: int
    door_ids: tuple[int, ...] | None   # day-pass doors, or overnight shared doors; None = defaults
    room_ids: tuple[int, ...]
    checkout: date | None
    access: tuple[dict, ...]
    expires_on: date | None
    end_time: str | None = None   # a day pass's or contractor badge's end time ("21:30")
    company: str = ""             # a contractor's company (optional)


def _id_tuple(raw, field: str) -> tuple[int, ...]:
    if not isinstance(raw, (list, tuple)) or len(raw) > MAX_LIST or not all(
            isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= MAX_LOCK_ID for v in raw):
        raise PlanError("Pick doors from the list.", field)
    return tuple(dict.fromkeys(raw))


def _date(raw, field: str, message: str) -> date | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or not DATE_RE.match(raw):
        raise PlanError(message, field)
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        raise PlanError(message, field) from None
    if day > LAST_DATE:
        raise PlanError(TOO_FAR, field)
    return day


def _end_time(raw) -> str | None:
    """An end time from the form ("21:30"): a day pass's (None for the usual one) or a
    contractor badge's."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or not HHMM_RE.match(raw):
        raise PlanError("Pick a valid time.", "end_time")
    return raw


def _access_number(item: dict, key: str, hi: int) -> int:
    value = item.get(key)
    # From 1: building 0 means every door and floor 0 the whole building to the encoder.
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= hi:
        raise PlanError("Choose what this card opens from the list.", "access")
    return value


def _access_entry(item) -> dict:
    if not isinstance(item, dict):
        raise PlanError("Choose what this card opens from the list.", "access")
    level = item.get("level")
    if level == "all":
        return {"level": "all"}
    if level == "building":
        return {"level": "building", "building": _access_number(item, "building", MAX_BUILDING)}
    if level == "floor":
        return {"level": "floor", "building": _access_number(item, "building", MAX_BUILDING),
                "floor": _access_number(item, "floor", MAX_FLOOR)}
    if level == "door":
        return {"level": "door", "door": _access_number(item, "door", MAX_LOCK_ID)}
    raise PlanError("Choose what this card opens from the list.", "access")


def _read_request(data) -> _Request:
    """The request body, shape-checked. Raises PlanError; unknown keys are ignored."""
    if not isinstance(data, dict):
        raise PlanError("That request couldn't be read. Please try again.")
    card_type = data.get("type")
    if card_type not in TYPES:
        raise PlanError("Pick what kind of card to make.", "type")
    name = data.get("name")
    if name is None:
        name = ""
    if not isinstance(name, str):
        raise PlanError("Enter a name.", "name")
    count = data.get("count")
    if count is None:
        count = 1
    if not isinstance(count, int) or isinstance(count, bool):
        label = "Party size" if card_type in ("day_pass", "overnight") else "Quantity"
        raise PlanError(f"{label} must be a whole number.", "count")
    company = data.get("company") if card_type == "contractor" else None
    if company is None:
        company = ""
    if not isinstance(company, str):
        raise PlanError("Enter the company's name, or leave it blank.", "company")
    door_ids, room_ids = data.get("door_ids"), data.get("room_ids")
    access = data.get("access")
    if access is None:
        access = []
    if not isinstance(access, (list, tuple)) or len(access) > MAX_LIST:
        raise PlanError("Choose what this card opens from the list.", "access")
    entries = [_access_entry(item) for item in access]
    return _Request(
        type=card_type, name=name, count=count,
        door_ids=None if door_ids is None else _id_tuple(door_ids, "door_ids"),
        room_ids=() if room_ids is None else _id_tuple(room_ids, "room_ids"),
        checkout=_date(data.get("checkout"), "checkout", "Pick a valid checkout date."),
        access=tuple({tuple(e.items()): e for e in entries}.values()),
        expires_on=_date(data.get("expires_on"), "expires_on", "Pick a valid expiry date."),
        end_time=(_end_time(data.get("end_time")) if card_type in ("day_pass", "contractor")
                  else None),
        company=company)


# ---------------------------------------------------------------- shared checks

def _clean_name(request: _Request) -> str:
    prompt = {"staff": "Enter the staff member's name.",
              "contractor": "Enter the contractor's name."}.get(request.type,
                                                               "Enter the guest's name.")
    name = " ".join(request.name.split())
    if not name:
        raise PlanError(prompt, "name")
    if len(name) > MAX_NAME:
        raise PlanError(f"Names can be at most {MAX_NAME} characters.", "name")
    return name


def _check_count(request: _Request, maximum: int) -> int:
    if not 1 <= request.count <= maximum:
        label = "Party size" if request.type in ("day_pass", "overnight") else "Quantity"
        raise PlanError(f"{label} must be between 1 and {maximum}.", "count")
    return request.count


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _past_cutoff(expiry: datetime) -> str:
    return f"Day passes end at {format_time(expiry)}. It's already past that time."


def _chosen_doors(perm: dict, requested: tuple[int, ...] | None, defaults: tuple[int, ...],
                  refusal: str) -> tuple[int, ...]:
    if requested is None or set(requested) == set(defaults):
        return tuple(defaults)
    if not _perm_flag(perm, "edit_doors"):
        raise PlanError(refusal, "door_ids", 403)
    return requested


def _resolve(doors: dict[int, _Door], perm: dict, ids: tuple[int, ...],
             configured: tuple[int, ...], field: str,
             wanted: str | None = None) -> list[_Door]:
    """The doors in order, checked: listed, active, offered, of the right kind and in this
    person's scope. wanted: the kind that belongs here ('public' for shared doors, 'room' for
    rooms): a door marked as anything else (a staff door, say) is refused, and one not marked
    yet counts as either. None: any kind (staff cards and contractor badges).

    A hidden door, or a guest room as a shared door (or a public door as a room), is allowed
    only when it comes from the configured defaults (a manager may hide the pool gate from
    pickers and still put it on every day pass, and marking a door later never stops cards that
    are set up to open it). A staff door never is: making a door a staff door takes it off the
    shared doors (db.Database.set_wb_door), and shared_doors() leaves out any still listed."""
    found = []
    for lock_id in ids:
        from_settings = lock_id in configured
        fix = (" Ask a manager to update the wristband doors." if from_settings
               else " Refresh the page and pick again.")
        door = doors.get(lock_id)
        if door is None:
            raise PlanError("One of the doors is no longer listed in TT Hotel." + fix, field)
        if not door.active:
            raise PlanError(f"{door.name} is no longer listed in TT Hotel." + fix, field)
        if door.hidden and not from_settings:
            raise PlanError(f"{door.name} isn't available for cards. Pick a different door.", field)
        if wanted and door.kind not in (None, wanted) and (door.kind == "staff"
                                                            or not from_settings):
            raise PlanError(f"{door.name} is {KIND_WORDS[door.kind]}, not {KIND_WORDS[wanted]}. "
                            f"Pick {KIND_WORDS[wanted]}.", field)
        if not door_in_scope(perm, {"lock_id": door.id, "building_number": door.building,
                                    "floor_number": door.floor}):
            raise PlanError(f"You don't have access to {door.name}.", field, 403)
        found.append(door)
    return found


def _door_record(door: _Door, field: str) -> Record:
    if door.building is None or door.floor is None:
        raise PlanError(f"{door.name} has no building or floor number in TT Hotel. Ask a manager "
                        "to set it in the TT Hotel app.", field)
    if (not MAC_RE.match(door.mac) or door.mac == BLANK_MAC
            or not 0 <= door.building <= MAX_BUILDING or not 0 <= door.floor <= MAX_FLOOR):
        raise PlanError(f"{door.name} has lock details the encoder can't use. Ask a manager to "
                        "check it in the TT Hotel app.", field)
    return Record("door", door.building, door.floor, door.mac, door.id, door.name)


def _describe(records: list[Record]) -> str:
    labels = [r.label for r in records]
    text = ", ".join(labels[:MAX_DESCRIBED])
    if len(labels) > MAX_DESCRIBED:
        text += f" +{len(labels) - MAX_DESCRIBED} more"
    return text


def _finish(request: _Request, name: str, count: int, records: list[Record], expiry: datetime,
            allow_lockout: bool, settings: dict, now: float, tz: ZoneInfo, *,
            doors_field: str, date_field: str | None) -> Plan:
    unique, seen = [], set()
    for record in records:
        key = (record.level, record.building, record.floor, record.mac)
        if key not in seen:
            seen.add(key)
            unique.append(record)
    if len(unique) > setting_number(settings, "wb_max_records"):
        raise PlanError(TOO_MANY_DOORS, doors_field)
    expires_at = int(expiry.timestamp())
    if expires_at > MAX_TIMESTAMP:
        raise PlanError(TOO_FAR, date_field)
    return Plan(type=request.type, name=name, count=count, records=unique,
                summary=_describe(unique), expires_at=expires_at,
                expires_label=format_expiry(expires_at, now, tz), allow_lockout=allow_lockout)


# ---------------------------------------------------------------- each card type

def _day_pass_expiry(settings: dict, now: float, tz: ZoneInfo) -> datetime:
    return at_local_time(tz, local_today(now, tz), setting_time(settings, "wb_daypass_end"))


def _end_time_left(now: float, tz: ZoneInfo) -> bool:
    """Whether a later end time can still be picked today (up to 11:59 PM)."""
    return now < at_local_time(tz, local_today(now, tz), LATEST_END_TIME).timestamp()


def _day_pass_end(request: _Request, perm: dict, settings: dict, now: float,
                  tz: ZoneInfo) -> datetime:
    """When this day pass stops working: the setting's time today, or, for people with the
    edit_end_time rule, any later time today they pick (it must still be ahead)."""
    usual = setting_time(settings, "wb_daypass_end")
    may_change = _perm_flag(perm, "edit_end_time")
    if request.end_time is None or request.end_time == usual:
        expiry = _day_pass_expiry(settings, now, tz)
        if now >= expiry.timestamp():
            if may_change and _end_time_left(now, tz):
                raise PlanError(_past_cutoff(expiry) + " Pick a later time.", "end_time")
            raise PlanError(_past_cutoff(expiry))
        return expiry
    if not may_change:
        raise PlanError("You can't change when day passes stop working.", "end_time", 403)
    expiry = at_local_time(tz, local_today(now, tz), request.end_time)
    if expiry.timestamp() <= now:
        raise PlanError("That time has already passed. Pick a later time.", "end_time")
    return expiry


def _plan_day_pass(request, perm, doors, settings, now, tz) -> Plan:
    defaults = shared_doors(settings, "wb_daypass_doors", doors)
    if not defaults:
        raise PlanError(NO_DAY_PASS_DOORS)
    expiry = _day_pass_end(request, perm, settings, now, tz)
    name = _clean_name(request)
    count = _check_count(request, setting_number(settings, "wb_daypass_max"))
    door_ids = _chosen_doors(perm, request.door_ids, defaults,
                             "You can't change which doors a day pass opens.")
    if not door_ids:
        raise PlanError("Pick at least one door.", "door_ids")
    chosen = _resolve(doors, perm, door_ids, defaults, "door_ids", "public")
    records = [_door_record(door, "door_ids") for door in chosen]
    return _finish(request, name, count, records, expiry,
                   setting_flag(settings, "wb_lockout_daypass"), settings, now, tz,
                   doors_field="door_ids", date_field=None)


def _plan_overnight(request, perm, doors, settings, now, tz) -> Plan:
    name = _clean_name(request)
    count = _check_count(request, setting_number(settings, "wb_overnight_max"))
    checkout = request.checkout
    if checkout is None:
        raise PlanError("Pick a checkout date.", "checkout")
    expiry = at_local_time(tz, checkout, setting_time(settings, "wb_checkout_time"))
    if expiry.timestamp() <= now:
        raise PlanError("That checkout time has already passed. Pick a later checkout date.",
                        "checkout")
    max_nights = _perm_number(perm, "max_nights")
    nights = (checkout - local_today(now, tz)).days
    if max_nights and nights > max_nights:
        raise PlanError("You can make overnight wristbands for up to "
                        f"{_plural(max_nights, 'night')}.", "checkout")
    rooms = _resolve(doors, perm, request.room_ids, (), "room_ids", "room")
    shared_defaults = shared_doors(settings, "wb_overnight_doors", doors)
    shared_ids = _chosen_doors(perm, request.door_ids, shared_defaults,
                               "You can't change the shared doors on overnight wristbands.")
    shared = _resolve(doors, perm, shared_ids, shared_defaults, "door_ids", "public")
    # Rooms are optional: most rooms take keys, so a wristband may open only the shared doors.
    records = ([_door_record(door, "room_ids") for door in rooms]
               + [_door_record(door, "door_ids") for door in shared])
    if not records:
        raise PlanError("Pick a room, or a shared door.", "room_ids")
    return _finish(request, name, count, records, expiry,
                   setting_flag(settings, "wb_lockout_overnight"), settings, now, tz,
                   doors_field="room_ids", date_field="checkout")


def _staff_records(request: _Request, perm: dict, doors: dict[int, _Door]) -> list[Record]:
    levels = set(_levels(perm))
    access = request.access
    if any(entry["level"] == "all" for entry in access):
        # An all-doors record opens everything, so anything else on the card is redundant.
        if "all" not in levels or not _scope(perm)[0]:
            raise PlanError(LEVEL_REFUSALS["all"], "access", 403)
        return [Record("all", 0, 0, BLANK_MAC, None, "All doors")]
    for entry in access:
        if entry["level"] not in levels:
            raise PlanError(LEVEL_REFUSALS[entry["level"]], "access", 403)

    building_names: dict[int, str] = {}
    floor_names: dict[tuple[int, int], str] = {}
    for door in doors.values():
        if not door.active or door.building is None:
            continue
        b = door.building
        if door.building_name or b not in building_names:
            building_names[b] = door.building_label
        if door.floor is not None and (door.floor_name or (b, door.floor) not in floor_names):
            floor_names[(b, door.floor)] = door.floor_label

    buildings = {e["building"] for e in access if e["level"] == "building"}
    floors = {(e["building"], e["floor"]) for e in access if e["level"] == "floor"}
    door_ids = tuple(e["door"] for e in access if e["level"] == "door")
    chosen = {door.id: door for door in _resolve(doors, perm, door_ids, (), "access")}

    records = []
    for entry in access:
        level = entry["level"]
        if level == "building":
            b = entry["building"]
            label = building_names.get(b, f"Building {b}")
            if not _covers_building(perm, b):
                raise PlanError(f"You don't have access to all of {label}.", "access", 403)
            if b not in building_names:
                raise PlanError(f"No doors are listed in building {b}. Ask a manager to sync "
                                "the doors, then try again.", "access")
            records.append(Record("building", b, 0, BLANK_MAC, None, label))
        elif level == "floor":
            b, f = entry["building"], entry["floor"]
            label = (f"{building_names.get(b, f'Building {b}')}, "
                     f"{floor_names.get((b, f), f'Floor {f}')}")
            if not _covers_floor(perm, b, f):
                raise PlanError(f"You don't have access to all of {label}.", "access", 403)
            if (b, f) not in floor_names:
                raise PlanError(f"No doors are listed on floor {f} of building {b}. Ask a "
                                "manager to sync the doors, then try again.", "access")
            if b in buildings:
                continue   # the building record already opens this floor
            records.append(Record("floor", b, f, BLANK_MAC, None, label))
        else:
            record = _door_record(chosen[entry["door"]], "access")
            if record.building in buildings or (record.building, record.floor) in floors:
                continue   # already opened by a building or floor record
            records.append(record)
    return records


def _plan_staff(request, perm, doors, settings, now, tz) -> Plan:
    name = _clean_name(request)
    count = _check_count(request, setting_number(settings, "wb_staff_max"))
    if not request.access:
        raise PlanError("Choose what this card opens.", "access")
    expires_on = request.expires_on
    if expires_on is None:
        raise PlanError("Pick an expiry date.", "expires_on")
    today = local_today(now, tz)
    if expires_on < today:
        raise PlanError("The expiry date can't be in the past.", "expires_on")
    max_days = _perm_number(perm, "max_staff_days")
    if max_days and expires_on > today + timedelta(days=max_days):
        raise PlanError(f"Staff cards can't expire more than {_plural(max_days, 'day')} from "
                        "today.", "expires_on")
    expiry = at_local_time(tz, expires_on, STAFF_CARD_TIME)
    if expiry.timestamp() <= now:
        raise PlanError("That expiry time has already passed. Pick a later date.", "expires_on")
    records = _staff_records(request, perm, doors)
    return _finish(request, name, count, records, expiry,
                   setting_flag(settings, "wb_lockout_staff"), settings, now, tz,
                   doors_field="access", date_field="expires_on")


def contractor_latest(now: float, tz: ZoneInfo) -> datetime:
    """The latest a contractor badge may end: this time of day, CONTRACTOR_DAYS days from today
    by the property's clock (a daylight-saving change in between makes that an hour more or less
    than 7 x 24 hours). A time that doesn't exist then moves past the gap; one that happens
    twice uses the first."""
    here = datetime.fromtimestamp(now, tz)
    later = datetime.combine(here.date() + timedelta(days=CONTRACTOR_DAYS), here.time())
    return later.replace(tzinfo=tz, fold=0).astimezone(timezone.utc).astimezone(tz)


def _contractor_name(request: _Request) -> str:
    """'Sam Lee (Acme Plumbing)': the contractor's name, with their company when one is given,
    as the card log, Look up and Door activity show it."""
    name = _clean_name(request)
    company = " ".join(request.company.split())
    if not company:
        return name
    full = f"{name} ({company})"
    if len(full) > MAX_NAME:
        raise PlanError(f"The name and company together can be at most {MAX_NAME} characters.",
                        "company")
    return full


def _contractor_end(request: _Request, now: float, tz: ZoneInfo) -> datetime:
    """When a contractor badge stops working: the date and time picked, later than now and no
    later than contractor_latest()."""
    if request.expires_on is None:
        raise PlanError("Pick the last day it works.", "expires_on")
    if request.end_time is None:
        raise PlanError("Pick the time it stops working.", "end_time")
    today = local_today(now, tz)
    if request.expires_on < today:
        raise PlanError("That date has already passed. Pick a later date.", "expires_on")
    if request.expires_on > today + timedelta(days=CONTRACTOR_DAYS):
        raise PlanError(CONTRACTOR_TOO_LONG, "expires_on")
    expiry = at_local_time(tz, request.expires_on, request.end_time)
    if expiry.timestamp() <= now:
        raise PlanError("That time has already passed. Pick a later time.", "end_time")
    if expiry.timestamp() > contractor_latest(now, tz).timestamp():
        raise PlanError(CONTRACTOR_TOO_LONG, "end_time")
    return expiry


def _plan_contractor(request, perm, doors, settings, now, tz) -> Plan:
    """A contractor badge: the doors picked, from any kind of door in the maker's own scope (staff
    doors too), until a date and time at most CONTRACTOR_DAYS days away."""
    name = _contractor_name(request)
    count = _check_count(request, setting_number(settings, "wb_contractor_max"))
    expiry = _contractor_end(request, now, tz)
    if not request.door_ids:
        raise PlanError("Pick at least one door.", "door_ids")
    records = [_door_record(door, "door_ids")
               for door in _resolve(doors, perm, request.door_ids, (), "door_ids")]
    most = setting_number(settings, "wb_max_records")
    if len({(r.building, r.floor, r.mac) for r in records}) > most:
        raise PlanError(f"That's too many doors for one badge. Pick at most {most}.", "door_ids")
    return _finish(request, name, count, records, expiry,
                   setting_flag(settings, "wb_lockout_contractor"), settings, now, tz,
                   doors_field="door_ids", date_field="expires_on")


PLANNERS = {"day_pass": _plan_day_pass, "overnight": _plan_overnight, "staff": _plan_staff,
            "contractor": _plan_contractor}


def plan(request: dict, perm: dict | None, doors: dict, settings: dict, now: float,
         tz: ZoneInfo) -> Plan:
    """Check a card request (the JSON body from the Wristbands screen) against this person's
    permission, the door list (every wb_doors row by lock ID, hidden and inactive included) and
    the settings. Raises PlanError with a message for staff."""
    parsed = _read_request(request)
    if not setting_flag(settings, "wristbands_enabled"):
        raise PlanError(TURNED_OFF, None, 403)
    if parsed.type not in _types(perm):
        raise PlanError(f"You don't have permission to make {NOUNS[parsed.type]}.", None, 403)
    return PLANNERS[parsed.type](parsed, perm, _door_map(doors), settings, now, tz)


# ---------------------------------------------------------------- what the screen offers

def options(perm: dict | None, doors: dict, settings: dict, now: float, tz: ZoneInfo) -> dict:
    """The body of GET /api/wristbands for this person (main.py adds "encoder"): which cards
    they may make right now, with form defaults, and the doors they may pick from."""
    door_map = _door_map(doors)
    types = _types(perm)
    reason = None
    if not setting_flag(settings, "wristbands_enabled"):
        reason = TURNED_OFF
    elif not types:
        reason = NOT_ALLOWED
    today = local_today(now, tz)
    edit = _perm_flag(perm, "edit_doors")

    def refusal(card_type: str) -> str | None:
        if reason:
            return reason
        if card_type in types:
            return None
        return f"You don't have permission to make {NOUNS[card_type]}."

    def permitted(card_type: str) -> bool:
        """Whether this person may make this kind at all (the app leaves out the ones they may
        not, and shows why one they may can't be made just now)."""
        return reason is None and card_type in types

    day_expiry = _day_pass_expiry(settings, now, tz)
    day_defaults = shared_doors(settings, "wb_daypass_doors", door_map)
    day_passed = now >= day_expiry.timestamp()
    # Someone who may change the end time can still pick a later one after the usual time.
    edit_end = _perm_flag(perm, "edit_end_time")
    day_reason = refusal("day_pass")
    if day_reason is None and not day_defaults:
        day_reason = NO_DAY_PASS_DOORS
    elif day_reason is None and day_passed and not (edit_end and _end_time_left(now, tz)):
        day_reason = _past_cutoff(day_expiry)

    staff_reason = refusal("staff")
    levels = _levels(perm)
    if staff_reason is None and not levels:
        staff_reason = "Your access doesn't include any staff card doors yet. Ask a manager."
    max_days = _perm_number(perm, "max_staff_days")
    staff_days = setting_number(settings, "wb_staff_days")
    if max_days:
        staff_days = min(staff_days, max_days)

    offered = [d for d in door_map.values() if d.active and not d.hidden and door_in_scope(
        perm, {"lock_id": d.id, "building_number": d.building, "floor_number": d.floor})]
    offered.sort(key=lambda d: (d.building is None, d.building or 0, d.floor is None,
                                d.floor or 0, d.name.lower(), d.id))

    contractor_reason = refusal("contractor")
    if contractor_reason is None and not offered:
        contractor_reason = NO_CONTRACTOR_DOORS
    latest = contractor_latest(now, tz)
    # The form starts at the end of today (tomorrow's, in the day's last minute).
    end_on = today if now < at_local_time(tz, today, CONTRACTOR_TIME).timestamp() else (
        today + timedelta(days=1))
    return {
        "enabled": reason is None,
        "reason": reason,
        "time_zone": str(tz.key if hasattr(tz, "key") else tz),
        "today": today.isoformat(),
        "now": int(now),
        "types": {
            "day_pass": {
                "allowed": day_reason is None, "reason": day_reason,
                "permitted": permitted("day_pass"),
                "expires_at": int(day_expiry.timestamp()),
                "expires_label": format_expiry(day_expiry.timestamp(), now, tz),
                "door_ids": list(day_defaults), "can_edit_doors": edit,
                "max": setting_number(settings, "wb_daypass_max"),
                # The usual end time ("20:00"), whether it has passed today, and whether this
                # person may pick another (a later time today, sent as end_time).
                "end_time": setting_time(settings, "wb_daypass_end"),
                "end_time_passed": day_passed, "can_edit_end_time": edit_end,
            },
            "overnight": {
                "allowed": refusal("overnight") is None, "reason": refusal("overnight"),
                "permitted": permitted("overnight"),
                "checkout_time": setting_time(settings, "wb_checkout_time"),
                "default_checkout": (today + timedelta(days=1)).isoformat(),
                "door_ids": list(shared_doors(settings, "wb_overnight_doors", door_map)),
                "can_edit_doors": edit, "max_nights": _perm_number(perm, "max_nights"),
                "max": setting_number(settings, "wb_overnight_max"),
            },
            "staff": {
                "allowed": staff_reason is None, "reason": staff_reason, "levels": levels,
                "permitted": permitted("staff"),
                "max_days": max_days,
                "default_expires_on": (today + timedelta(days=staff_days)).isoformat(),
                "max": setting_number(settings, "wb_staff_max"),
            },
            "contractor": {
                "allowed": contractor_reason is None, "reason": contractor_reason,
                "permitted": permitted("contractor"),
                "max": setting_number(settings, "wb_contractor_max"),
                "max_days": CONTRACTOR_DAYS,
                "default_expires_on": end_on.isoformat(), "default_end_time": CONTRACTOR_TIME,
                # The latest end allowed right now (this time, CONTRACTOR_DAYS days ahead).
                "latest_on": latest.date().isoformat(), "latest_time": latest.strftime("%H:%M"),
                "latest_at": int(latest.timestamp()),
                "latest_label": format_expiry(latest.timestamp(), now, tz),
            },
        },
        # kind: 'public', 'room', 'staff' or None (not marked yet). Rooms are offered for
        # overnight wristbands, public doors as shared doors, doors not marked yet as both, and
        # every kind (staff doors too) for staff cards and contractor badges.
        "doors": [{"id": d.id, "name": d.name, "building": d.building,
                   "building_name": d.building_label, "floor": d.floor,
                   "floor_name": d.floor_label, "kind": d.kind} for d in offered],
        "see_all_cards": _perm_flag(perm, "see_all_cards"),
    }
