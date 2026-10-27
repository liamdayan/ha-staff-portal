"""A stand-in for app/ctdata.py (reports-core builds the real one): only what Tasks uses, with
the same names. Tests replace forms and form_submissions as they need. With CTDATA_STANDIN set
to a JSON file ({"forms": [...], "submissions": {form_id: [...]}}), it reads that instead, for
trying the add-on in a browser. It never contacts Connecteam."""

import json
import os


class Unavailable(Exception):
    """Connecteam data can't be had right now (permission, paused by budget, unreachable)."""


def _file() -> dict:
    path = os.environ.get("CTDATA_STANDIN")
    if not path:
        raise Unavailable("Connecteam isn't connected in this test.")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


async def forms() -> list[dict]:
    return _file().get("forms", [])


async def form_submissions(form_id, start, end) -> list[dict]:
    return _file().get("submissions", {}).get(str(form_id), [])
