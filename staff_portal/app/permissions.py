"""What staff are allowed to do to each kind of device.

Staff never send raw service calls. They pick an action from this table, and every
parameter is checked against the entity's own limits before anything reaches Home Assistant.
"""

# Parameter specs:
#   ("number", min_attr, max_attr, fallback_min, fallback_max)
#   ("choice", list_attr)
ON_OFF = {"turn_on": {}, "turn_off": {}, "toggle": {}}

ACTIONS: dict[str, dict[str, dict]] = {
    "light": {
        "turn_on": {"brightness_pct": ("number", None, None, 1, 100)},
        "turn_off": {},
        "toggle": {},
    },
    "switch": ON_OFF,
    "fan": ON_OFF,
    "input_boolean": ON_OFF,
    "siren": {"turn_on": {}, "turn_off": {}},
    "climate": {
        "set_temperature": {"temperature": ("number", "min_temp", "max_temp", 40, 95)},
        "set_hvac_mode": {"hvac_mode": ("choice", "hvac_modes")},
        "turn_on": {},
        "turn_off": {},
    },
    "water_heater": {
        "set_temperature": {"temperature": ("number", "min_temp", "max_temp", 90, 160)},
        "set_operation_mode": {"operation_mode": ("choice", "operation_list")},
    },
    "humidifier": {"turn_on": {}, "turn_off": {}},
    "lock": {"lock": {}, "unlock": {}},
    "cover": {"open_cover": {}, "close_cover": {}, "stop_cover": {}},
    "valve": {"open_valve": {}, "close_valve": {}},
    "button": {"press": {}},
    "input_button": {"press": {}},
    "script": {"turn_on": {}},
    "scene": {"turn_on": {}},
    "vacuum": {"start": {}, "pause": {}, "return_to_base": {}},
}

# Read-only domains staff may be given "view" access to.
VIEW_ONLY = {
    "sensor", "binary_sensor", "weather", "device_tracker", "person",
    "camera", "update", "event", "number", "select", "input_number", "input_select",
}

# Never offered in the admin picker: internal or not meaningful to staff.
HIDDEN = {"zone", "sun", "automation", "conversation", "tts", "stt", "wake_word", "todo", "image"}


def domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def controllable(entity_id: str) -> bool:
    return domain_of(entity_id) in ACTIONS


def offered(entity_id: str) -> bool:
    d = domain_of(entity_id)
    return d not in HIDDEN and (d in ACTIONS or d in VIEW_ONLY)


class ActionRejected(Exception):
    pass


def build_service_call(entity_id: str, action: str, params: dict, state: dict | None):
    """Validate a staff request and return (domain, service, data) for Home Assistant."""
    domain = domain_of(entity_id)
    allowed = ACTIONS.get(domain, {})
    if action not in allowed:
        raise ActionRejected(f"'{action}' is not allowed for {domain}")
    attrs = (state or {}).get("attributes", {})
    data = {"entity_id": entity_id}
    for name, value in (params or {}).items():
        spec = allowed[action].get(name)
        if spec is None:
            raise ActionRejected(f"unexpected parameter '{name}'")
        if spec[0] == "number":
            _, min_attr, max_attr, lo, hi = spec
            try:
                num = float(value)
            except (TypeError, ValueError):
                raise ActionRejected(f"'{name}' must be a number") from None
            lo = attrs.get(min_attr, lo) if min_attr else lo
            hi = attrs.get(max_attr, hi) if max_attr else hi
            if not lo <= num <= hi:
                raise ActionRejected(f"'{name}' must be between {lo} and {hi}")
            data[name] = int(num) if num.is_integer() else num
        elif spec[0] == "choice":
            choices = attrs.get(spec[1]) or []
            if value not in choices:
                raise ActionRejected(f"'{value}' is not a valid {name}")
            data[name] = value
    return domain, action, data


PAST_TENSE = {
    "turn_on": "turned on", "turn_off": "turned off", "toggle": "toggled",
    "lock": "locked", "unlock": "unlocked", "press": "pressed",
    "open_cover": "opened", "close_cover": "closed", "stop_cover": "stopped",
    "open_valve": "opened", "close_valve": "closed",
    "start": "started", "pause": "paused", "return_to_base": "sent back to its dock",
}


def describe_action(entity_id: str, action: str, data: dict) -> str:
    """Plain-English summary for Home Assistant's logbook, such as 'set to 140°'."""
    domain = domain_of(entity_id)
    if "temperature" in data:
        return f"set to {data['temperature']}°"
    if "hvac_mode" in data:
        return f"set to {data['hvac_mode'].replace('_', ' ')} mode"
    if "operation_mode" in data:
        return f"set to {data['operation_mode'].replace('_', ' ')} mode"
    if "brightness_pct" in data:
        return f"turned on at {data['brightness_pct']}% brightness"
    if domain == "script" and action == "turn_on":
        return "run"
    if domain == "scene" and action == "turn_on":
        return "activated"
    return PAST_TENSE.get(action, action.replace("_", " "))
