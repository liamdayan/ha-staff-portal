"""Door activity: what happened at the doors (TT Hotel's records, sent to Staff Access), with
filters for the dates, the door, the kind of card, the result, and a name or card number.

Reads only what's already stored here (ctx.doors): it never asks TT Hotel or Connecteam.
Card numbers go only to people who may manage the report.
"""

import re
from datetime import date, datetime, time, timedelta

DAYS = 7          # the dates shown at first: the last week, today included
MAX_DAYS = 180    # door activity is kept this long
KINDS = {"": "Every card", "guest": "Guest wristbands", "staff": "Staff cards",
         "contractor": "Contractor badges", "other": "Not made here"}
RESULTS = {"": "Everything", "opened": "Opened", "refused": "Refused", "problems": "Problems only"}
CARD_WORDS = {"day_pass": "Day pass", "overnight": "Overnight", "staff": "Staff card",
              "contractor": "Contractor badge"}
ALERTS = {"lost": "Lost card", "revoked": "Revoked card", "deleted": "Deleted card",
          "archived": "Archived person's card"}


def _day(raw: str, what: str) -> date | None:
    if not raw:
        return None
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"Pick a valid {what} date.") from None
    if not 2000 <= day.year <= 2100:   # far-off years can't be worked with (or have records)
        raise ValueError(f"Pick a valid {what} date.")
    return day


def _label(at: float, tz, today: date) -> str:
    when = datetime.fromtimestamp(at, tz)
    clock = when.strftime("%I:%M %p").lstrip("0")
    if when.date() == today:
        return f"Today {clock}"
    if when.date() == today - timedelta(days=1):
        return f"Yesterday {clock}"
    return f"{when.strftime('%a, %b')} {when.day}, {clock}"


async def data(ctx, params):
    if ctx.doors is None:
        raise ValueError("Door activity isn't available here.")
    end = _day(params["end"], "end") or ctx.today
    start = _day(params["start"], "start") or end - timedelta(days=DAYS - 1)
    if start > end:
        raise ValueError("The start date is after the end date.")
    if (end - start).days >= MAX_DAYS:
        raise ValueError(f"Pick at most {MAX_DAYS} days. Older door activity isn't kept.")
    door = params["door"]
    if not re.fullmatch(r"[0-9]{0,16}", door):
        raise ValueError("Pick a door from the list.")
    kind, result = params["kind"], params["result"]
    if kind not in KINDS:
        raise ValueError("Pick a kind of card from the list.")
    if result not in RESULTS:
        raise ValueError("Pick a result from the list.")
    q = " ".join(params["q"].split())[:60]

    since = datetime.combine(start, time(), ctx.tz).timestamp()
    until = datetime.combine(end + timedelta(days=1), time(), ctx.tz).timestamp()
    limit = ctx.doors.LIMIT
    found = ctx.doors.records(since, until, door=int(door) if door else None, kind=kind,
                              result=result, q=q, numbers=ctx.manage, limit=limit + 1)
    capped = len(found) > limit
    found = found[:limit]
    records = []
    for r in found:
        row = {"at": r["at"], "when": _label(r["at"], ctx.tz, ctx.today), "door": r["door"],
               "who": r["who"], "card": CARD_WORDS.get(r["card_type"], ""), "how": r["how"],
               "result": "Alarm" if r["alarm"] else "Opened" if r["ok"] else "Refused",
               "alert": ALERTS.get(r["alert"] or "", ""), "problem": r["problem"]}
        if ctx.manage:
            row["card_number"] = r["card_number"] or ""
        records.append(row)
    return {
        "start": start.isoformat(), "end": end.isoformat(), "today": ctx.today.isoformat(),
        "door": door, "kind": kind, "result": result, "q": q,
        "doors": ctx.doors.list(), "kinds": KINDS, "results": RESULTS,
        "records": records, "capped": capped, "limit": limit, "card_numbers": ctx.manage,
        "counts": {"total": len(records),
                   "opened": sum(1 for r in records if r["result"] == "Opened"),
                   "refused": sum(1 for r in records if r["result"] == "Refused"),
                   "problems": sum(1 for r in records if r["problem"])},
    }


REPORT = dict(
    key="door_activity",
    name="Door activity",
    description="Who opened which doors and when, with filters for dates, doors, cards and results.",
    data=data,
    params={"start": "", "end": "", "door": "", "kind": "", "result": "", "q": ""},
)
