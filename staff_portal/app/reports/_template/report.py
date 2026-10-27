"""Template report: hours worked per person in one week, from Connecteam's time clock, with an
optional CSV of targets uploaded by a manager. Copy this folder to start a new report (see
REPORTS.md). Hidden from the lists because its folder starts with "_".

report.py imports nothing but the standard library: everything else comes in through ctx.
"""

import csv
import io
from datetime import date, timedelta


async def data(ctx, params):
    """What view.js draws. ctx.ct raises ctx.ct.Unavailable when Connecteam can't answer; the
    staff app then shows its message, so there's no need to catch it."""
    try:
        start = date.fromisoformat(params["week"]) if params["week"] else None
    except ValueError:
        raise ValueError("Pick a week.") from None
    start = start or ctx.today - timedelta(days=ctx.today.weekday())   # this Monday
    end = start + timedelta(days=6)
    hours = {}
    for row in await ctx.ct.timesheets(start, end):
        if row["type"] == "shift" and row["hours"]:
            hours[row["name"]] = hours.get(row["name"], 0) + row["hours"]
    targets = {}
    files = ctx.uploads("targets")
    if files:   # newest first: use the latest
        for line in csv.reader(io.StringIO(files[0]["text"])):
            if len(line) >= 2:
                try:
                    targets[line[0].strip()] = float(line[1])
                except ValueError:
                    pass   # a header row, or a typo
    return {
        "title": ctx.settings["title"],
        "week": start.isoformat(),
        "people": [{"name": n, "hours": round(h, 2), "target": targets.get(n)}
                   for n, h in sorted(hours.items())],
        "targets_file": files[0]["name"] if files else None,
    }


REPORT = dict(
    key="template",
    name="Hours this week",
    description="Hours worked by each person this week (template report).",
    data=data,
    params={"week": ""},                      # ?week=YYYY-MM-DD (blank: this week)
    uploads=[{"key": "targets", "name": "Weekly targets", "accept": ".csv",
              "help": "A CSV with each person's name and their weekly hours."}],
    settings=[{"key": "title", "default": "Hours this week"}],
)
