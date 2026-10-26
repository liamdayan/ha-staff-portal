"""The dates on a card or certificate, from the text Google Drive reads off a photo or PDF
(gdrive.Bridge.ocr). HR always confirms the date; this only suggests one, says how it got it
("read" from the card, "calculated" from the issue date, or "not_found") and warns about
anything odd.

No issuer publishes its layout, so it looks for labels ("Expires", "Issued", ...) next to each
date: on the same line, or on the line above or below (cards often print the label over or under
the value, sometimes several side by side). Birth dates are ignored. Common misreads in numbers
are fixed (the letter O for zero, l or I for one). Without an expiry label, two dates exactly as
far apart as the card lasts give the expiry, or it's worked out from the issue date.

An Oregon Food Handler Card lasts 3 years from its issue date, an OLCC Service Permit 5 years.
The OLCC Server Education course certificate lasts 2 years and isn't a permit: it's flagged.
"""

import re
from datetime import date

LASTS_YEARS = {"food_handler": 3, "olcc": 5}
GENERIC_YEARS = 10        # nothing lasts longer than this ahead ("generic" kinds)
SLACK_DAYS = 31           # dates this close to a whole number of years apart still pair up

_LABELS = {
    "expires": r"expiration\s+date|expiration|expiry\s+date|expiry|expires?\s+on|expires?|exp"
               r"|valid\s+(?:through|thru|until|till)|good\s+(?:through|thru|until)",
    "issued": r"date\s+of\s+issue|issue\s+date|issued\s+on|date\s+issued|issued|valid\s+from"
              r"|effective\s+date"
              r"|effective|completion\s+date|date\s+completed|completed\s+on|completed"
              r"|test\s+date|exam\s+date|date\s+of\s+(?:test|exam|training)|training\s+date"
              r"|certification\s+date|date\s+certified|certified\s+on",
    "birth": r"date\s+of\s+birth|birth\s*date|birthday|birth|born|d\s*\.?\s*o\s*\.?\s*b\.?",
}
LABEL_RE = re.compile("|".join(f"(?P<{kind}>(?<![A-Za-z])(?:{words})(?![A-Za-z]))"
                               for kind, words in _LABELS.items()), re.I)

# A digit as OCR may read it: O or o for 0, I, l or | for 1, S for 5, B for 8.
D = r"[0-9OoIl|SB]"
FIX = str.maketrans("OoIl|SB", "0011158")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
MONTH = r"(?i:(" + "|".join(MONTHS) + r")[a-z]*\.?)"
NUMERIC_RE = re.compile(rf"(?<![0-9|/])({D}{{1,2}})\s{{0,2}}([/.\-])\s{{0,2}}({D}{{1,2}})"
                        rf"\s{{0,2}}\2\s{{0,2}}({D}{{4}}|{D}{{2}})(?![0-9|/])")
ISO_RE = re.compile(rf"(?<![0-9])({D}{{4}})-({D}{{1,2}})-({D}{{1,2}})(?![0-9])")
MONTH_FIRST_RE = re.compile(rf"(?<![A-Za-z]){MONTH}\s{{0,2}}({D}{{1,2}})(?i:st|nd|rd|th)?\s{{0,2}},?"
                            rf"\s{{0,2}}({D}{{4}})(?![0-9])")
DAY_FIRST_RE = re.compile(rf"(?<![0-9|])({D}{{1,2}})(?i:st|nd|rd|th)?[\s\-]{{1,2}}{MONTH},?"
                          rf"[\s\-]{{1,2}}({D}{{4}})(?![0-9])")
MONTH_YEAR_RE = re.compile(rf"(?<![0-9|/])({D}{{1,2}})\s{{0,2}}/\s{{0,2}}({D}{{4}})(?![0-9|/])")

COURSE_RE = re.compile(r"server\s+education|certificate\s+of\s+(?:completion|achievement)"
                       r"|course\s+(?:completion|completed|certificate)|completed\s+the\s+course"
                       r"|has\s+(?:successfully\s+)?completed", re.I)
PERMIT_RE = re.compile(r"permit\s*(?:#|no\b|number|id\b)|service\s+permit\s+card", re.I)
TEMPORARY_RE = re.compile(r"temporary\s+(?:alcohol\s+)?(?:service\s+)?permit", re.I)
OLCC_RE = re.compile(r"\bOLCC\b|liquor\s+(?:and\s+cannabis\s+)?(?:control\s+)?commission"
                     r"|service\s+permit", re.I)
FOOD_RE = re.compile(r"food\s+handler", re.I)


def pick_dates(text: str, kind: str, today: date, name: str | None = None) -> dict:
    """{"expires": date | None, "issued": date | None, "how": "read" | "calculated" |
    "not_found", "warnings": [plain sentences]} for kind "food_handler", "olcc" or "generic".
    name: the employee's, to warn when it isn't on the card."""
    years = LASTS_YEARS.get(kind)
    found = _labelled_dates(str(text or ""), today)
    births = [d for d, label in found if label == "birth"]
    found = [(d, label) for d, label in found if label != "birth" and d not in births]
    expiry = [d for d, label in found if label == "expires"]
    issue = [d for d, label in found if label == "issued"]
    unlabelled = sorted({d for d, label in found if label is None and
                         _add_years(today, -((years or GENERIC_YEARS) + 1)) <= d
                         <= _add_years(today, (years or GENERIC_YEARS) + 1)})
    course = kind == "olcc" and bool(COURSE_RE.search(text or "")) and not PERMIT_RE.search(text or "")
    warnings = []
    expires = issued = None
    how = "not_found"

    if expiry:
        sane = [d for d in expiry if d <= _add_years(today, (years or GENERIC_YEARS) + 1)]
        expires, how = max(sane or expiry), "read"
        before = [d for d in issue if d < expires] or [d for d in unlabelled if d < expires and (
            not years or _near(_add_years(d, years), expires))]
        issued = max(before) if before else None
    elif issue:
        issued = max(d for d in issue)
        if years and not course:
            expires, how = _add_years(issued, years), "calculated"
    elif pair := _pair(unlabelled, years):
        issued, expires = pair
        how = "read"
    elif unlabelled:
        ahead = [d for d in unlabelled if d > today]
        if ahead:
            expires, how = max(ahead), "read"
            warnings.append("No label was found next to the date: check it's the expiry date.")
        else:
            issued = max(unlabelled)
            if years and not course:
                expires, how = _add_years(issued, years), "calculated"
                warnings.append("No label was found next to the date: it was taken as the issue "
                                "date. Check it.")

    if expires and expires < today:
        warnings.append(f"The card expired on {_us(expires)}.")
    if expires and expires > _add_years(today, years or GENERIC_YEARS) and \
            (expires - _add_years(today, years or GENERIC_YEARS)).days > SLACK_DAYS:
        warnings.append("The expiry date is further ahead than this card lasts. Check it.")
    if issued and issued > today:
        warnings.append("The issue date is in the future. Check it.")
    if how == "read" and issued and years and not _near(_add_years(issued, years), expires):
        warnings.append(f"The dates on the card aren't {years} years apart. Check them.")
    warnings += _kind_warnings(text or "", kind, course, births, today)
    if name and not _name_on_card(name, text or ""):
        warnings.append("The employee's name wasn't found on the card. Check it's theirs.")
    return {"expires": expires, "issued": issued, "how": how, "warnings": warnings}


def _kind_warnings(text: str, kind: str, course: bool, births: list[date], today: date) -> list[str]:
    out = []
    if course:
        out.append("This looks like the OLCC Server Education course certificate, not the Service "
                   "Permit. The course certificate lasts 2 years and doesn't allow serving alcohol.")
    if kind == "olcc":
        if TEMPORARY_RE.search(text):
            out.append("This looks like a temporary permit. Ask for the permit itself when it "
                       "arrives.")
        if FOOD_RE.search(text) and not OLCC_RE.search(text):
            out.append("This looks like a Food Handler Card, not an OLCC Service Permit.")
        if any(_add_years(born, 21) > today for born in births):
            out.append("The date of birth on the card shows someone under 21, who may not mix "
                       "drinks or bartend.")
    if kind == "food_handler" and OLCC_RE.search(text) and not FOOD_RE.search(text):
        out.append("This looks like an OLCC Service Permit, not a Food Handler Card.")
    return out


# ---------------------------------------------------------------- finding dates and labels

def _labelled_dates(text: str, today: date) -> list[tuple[date, str | None]]:
    """Every date in the text with its label ("expires", "issued", "birth" or None)."""
    lines = [(_dates_in(line, today), _labels_in(line)) for line in text.splitlines()]
    lines = [(dates, labels) for dates, labels in lines if dates or labels]
    out = []
    pending = []   # (line index, dates) with no label on their own line
    for i, (dates, labels) in enumerate(lines):
        if not dates:
            continue
        loose = []
        for pos, end, d, month_only in dates:
            # The closest label before it on the line, unless another date sits between them.
            before = [(lend, kind) for lstart, lend, kind in labels if lend <= pos
                      and not any(lend <= other < pos for other, _, _, _ in dates)]
            label = max(before)[1] if before else None
            if label is None and not labels:
                loose.append((d, month_only))
            elif not (month_only and label != "expires"):
                out.append((d, label))
        if loose:
            pending.append((i, loose))
    # Dates alone on their line take the labels on the line above or below them (one each, in
    # order), whichever way round this card does it more often.
    def pairs(step):
        got = {}
        for i, loose in pending:
            j = i + step
            if 0 <= j < len(lines) and lines[j][1] and not lines[j][0] and len(lines[j][1]) == len(loose):
                got[i] = [kind for _, _, kind in lines[j][1]]
        return got
    above, below = pairs(-1), pairs(1)
    chosen = above if len(above) >= len(below) else below
    for i, loose in pending:
        kinds = chosen.get(i) or [None] * len(loose)
        for (d, month_only), kind in zip(loose, kinds):
            if not (month_only and kind != "expires"):
                out.append((d, kind))
    return out


def _labels_in(line: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.lastgroup) for m in LABEL_RE.finditer(line)]


def _dates_in(line: str, today: date) -> list[tuple[int, int, date, bool]]:
    """(start, end, date, month_only) for each date in a line. A month and year alone
    ("06/2029") is the last day of that month, and only counts with an expiry label."""
    found = []
    taken = []

    def add(m, year, month, day, month_only=False):
        if any(s < m.end() and m.start() < e for s, e in taken):
            return
        d = _make_date(year, month, day, today)
        if d:
            taken.append((m.start(), m.end()))
            found.append((m.start(), m.end(), d, month_only))

    for m in ISO_RE.finditer(line):
        add(m, m[1], m[2], m[3])
    for m in NUMERIC_RE.finditer(line):
        first, second = _number(m[1]), _number(m[3])
        if first is not None and second is not None and first > 12 >= second:
            add(m, m[4], m[3], m[1])   # day first
        else:
            add(m, m[4], m[1], m[3])
    for m in MONTH_FIRST_RE.finditer(line):
        add(m, m[3], MONTHS.index(m[1].lower()[:3]) + 1, m[2])
    for m in DAY_FIRST_RE.finditer(line):
        add(m, m[3], MONTHS.index(m[2].lower()[:3]) + 1, m[1])
    for m in MONTH_YEAR_RE.finditer(line):
        add(m, m[2], m[1], "last", month_only=True)
    found.sort()
    return found


def _number(part) -> int | None:
    text = str(part).translate(FIX)
    return int(text) if text.isdigit() else None


def _make_date(year, month, day, today: date) -> date | None:
    """A date from parts as read (letters fixed). A two-digit year is this century unless that's
    more than 30 years ahead. The year must have at least two real digits."""
    if isinstance(year, str) and sum(ch.isdigit() for ch in year) < 2:
        return None
    y, m = _number(year), _number(month)
    if day == "last":
        d = 31
    else:
        day = re.sub(r"(?i)(st|nd|rd|th)$", "", str(day).strip())
        d = _number(day)
    if y is None or m is None or d is None:
        return None
    if y < 100:
        y += 2000 if y <= today.year % 100 + 30 else 1900
    if not 1900 <= y <= 2100 or not 1 <= m <= 12:
        return None
    if day == "last":
        while d > 27:
            try:
                return date(y, m, d)
            except ValueError:
                d -= 1
    try:
        return date(y, m, d)
    except ValueError:
        return None


# ---------------------------------------------------------------- helpers

def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:   # February 29th
        return d.replace(year=d.year + years, day=28)


def _near(a: date, b: date | None) -> bool:
    return b is not None and abs((a - b).days) <= SLACK_DAYS


def _pair(dates: list[date], years: int | None) -> tuple[date, date] | None:
    """Two dates a card's lifetime apart (any whole number of years for "generic"): (issued,
    expires), the latest such pair."""
    best = None
    for i, first in enumerate(dates):
        for later in dates[i + 1:]:
            spans = [years] if years else range(1, GENERIC_YEARS + 1)
            if any(abs((_add_years(first, n) - later).days) <= 1 for n in spans):
                best = (first, later)
    return best


def _us(d: date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


def _name_on_card(name: str, text: str) -> bool:
    words = [w for w in re.findall(r"[A-Za-z]+", name.lower()) if len(w) >= 3]
    found = set(re.findall(r"[a-z]+", text.lower()))
    return not words or any(w in found for w in words)
