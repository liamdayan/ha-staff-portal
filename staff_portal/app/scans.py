"""Scans: the rules for a "scan to" option (checked and tidied here) and the name a scanned file
gets. No network and no database: main.py does those.

An option is one button in the staff app's Scans tool: where the file goes (a folder in the
Google shared drive for scans), who may use it (departments), how it's scanned (colour, quality,
paper) and how the file is named. The name is the owner's choice, never typed by staff: a format
made of fixed text and pieces in braces that are filled in at scan time,

    {prefix}-{name}          ->  INV-Ana Lopez.pdf
    {prefix}-{date}-{first}  ->  INV-2026-11-18-Ana.pdf

and when a file of that name was already scanned to that option, a number goes on the end:
INV-Ana Lopez-2.pdf, INV-Ana Lopez-3.pdf (see numbered()).
"""

import re
from datetime import datetime

# The pieces a format may use, with what each one is (shown in the dashboard).
PIECES = {
    "prefix": "this option's prefix",
    "name": "the person's full name",
    "first": "their first name",
    "last": "their last name",
    "department": "their department",
    "option": "this option's name",
    "date": "the date, as 2026-11-18",
    "time": "the time, as 1432",
    "year": "the year",
    "month": "the month, as 11",
    "day": "the day of the month, as 18",
}
DEFAULT_FORMAT = "{prefix}-{name}"
COLORS = {"color": "Colour", "gray": "Grey", "bw": "Black and white"}
QUALITY = {"normal": 200, "high": 300}          # dots per inch
PAPERS = {"letter": "Letter (8.5 × 11 in)", "legal": "Legal (8.5 × 14 in, feeder only)",
          "a4": "A4 (210 × 297 mm)"}
MAX_OPTIONS = 40
MAX_LABEL = 40
MAX_PREFIX = 24
MAX_FORMAT = 120
MAX_FOLDER = 80
MAX_BASE = 120          # a file's name before its number and ".pdf"
PIECE_RE = re.compile(r"\{([^{}]*)\}")
# Not allowed in a file or folder name (Windows' list, which is the strictest), and controls.
BAD_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


def file_text(value, limit: int) -> str:
    """Text that's safe in a file or folder name: no path or control characters, spaces tidied,
    no dots or spaces at the ends (Windows drops them, and ".." means the folder above)."""
    text = BAD_CHARS_RE.sub(" ", str(value or ""))
    return " ".join(text.split())[:limit].strip(" .")


def clean_format(value) -> str:
    """A name format as typed, checked. Raises ValueError with a message for the owner."""
    text = " ".join(str(value or "").split()) or DEFAULT_FORMAT
    if len(text) > MAX_FORMAT:
        raise ValueError(f"The file name format can be at most {MAX_FORMAT} characters.")
    for piece in PIECE_RE.findall(text):
        if piece not in PIECES:
            raise ValueError(f"The file name format has {{{piece}}}, which isn't one of the pieces. "
                             f"Use {', '.join('{' + p + '}' for p in PIECES)}.")
    rest = PIECE_RE.sub("", text)
    if "{" in rest or "}" in rest:
        raise ValueError("The file name format has a brace without its pair. Write pieces like {name}.")
    if BAD_CHARS_RE.search(rest):
        raise ValueError('The file name format can\'t have any of \\ / : * ? " < > |')
    if not PIECE_RE.search(text):
        raise ValueError("The file name format needs at least one piece, such as {name} or {date}, "
                         "so files don't all get the same name.")
    return text


def clean_option(raw, departments: set[int]) -> dict:
    """A scan-to option from the dashboard, checked and tidied: {label, prefix, format, folder,
    color, quality, paper, departments, enabled}. departments: the IDs that exist. Raises
    ValueError with a message for the owner."""
    if not isinstance(raw, dict):
        raise ValueError("That option couldn't be read.")
    label = file_text(raw.get("label"), MAX_LABEL)
    if not label:
        raise ValueError("Give the option a name, such as Invoices.")
    prefix = file_text(raw.get("prefix"), MAX_PREFIX)
    text = clean_format(raw.get("format"))
    if "{prefix}" in text and not prefix:
        raise ValueError("The file name format uses {prefix}, so enter a prefix (or take {prefix} "
                         "out of the format).")
    folder = file_text(raw.get("folder"), MAX_FOLDER) or label
    color, quality, paper = raw.get("color", "color"), raw.get("quality", "normal"), raw.get("paper", "letter")
    if color not in COLORS or quality not in QUALITY or paper not in PAPERS:
        raise ValueError("Pick the colour, quality and paper from the lists.")
    chosen = raw.get("departments", [])
    if not isinstance(chosen, list) or not all(
            isinstance(i, int) and not isinstance(i, bool) for i in chosen):
        raise ValueError("Pick departments from the list.")
    return {"label": label, "prefix": prefix, "format": text, "folder": folder, "color": color,
            "quality": quality, "paper": paper,
            "departments": sorted({i for i in chosen if i in departments}),
            "enabled": raw.get("enabled", True) is True}


def base_name(option: dict, person: str, department: str, when: datetime) -> str:
    """The file's name before any number and ".pdf": the option's format, filled in. `when` is
    the time at the property."""
    words = " ".join(str(person or "").split()).split(" ")
    values = {
        "prefix": option["prefix"], "name": " ".join(words), "first": words[0],
        "last": words[-1] if len(words) > 1 else "", "department": department or "",
        "option": option["label"], "date": when.strftime("%Y-%m-%d"), "time": when.strftime("%H%M"),
        "year": when.strftime("%Y"), "month": when.strftime("%m"), "day": when.strftime("%d"),
    }
    text = PIECE_RE.sub(lambda m: file_text(values.get(m.group(1), ""), MAX_BASE), option["format"])
    # A piece that came out empty (no last name, say) leaves no doubled or dangling dashes.
    text = re.sub(r"\s*([-_])(\s*[-_])+\s*", r"\1", text)
    text = file_text(text, MAX_BASE).strip("-_ ")
    return text or file_text(option["label"], MAX_BASE) or "Scan"


def numbered(base: str, earlier: int) -> str:
    """The file's full name: the base, with -2, -3... when it's been used `earlier` times."""
    return f"{base}{f'-{earlier + 1}' if earlier > 0 else ''}.pdf"
