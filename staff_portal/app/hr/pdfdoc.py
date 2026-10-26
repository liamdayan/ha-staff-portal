"""Plain HTML for the PDFs the HR portal makes (signed write-ups, final reviews), which Google
Drive turns into a PDF (documents.make_pdf, through the bridge's pdf op). It only has headings,
paragraphs, label and value rows, lists and signature lines. Everything is escaped text: nothing
people typed is ever taken as HTML. Characters outside plain ASCII become numeric entities
(&#233;), so the page reads the same whatever Google assumes about its encoding.

    pdfdoc.page("Written warning", [
        ("heading", "What happened"),
        ("paragraph", text),                        # a blank line starts a new paragraph
        ("rows", [("Date", "2026-10-25"), ("Points", "2")]),
        ("signature", "Employee", "Jane Smith", "2026-10-26 10:14"),
    ], subtitle="Hot Lake Lodge", footer="Signing confirms receipt, not agreement.")

A block is a tuple (kind, ...), a dict ({"heading": ...}, {"paragraph": ...}, {"rows": [...]},
{"signature": label, "name": ..., "date": ...}, ...) or a plain string (a paragraph). Kinds:

  heading, subheading       a heading (h2), a smaller one (h3)
  paragraph                 text; line breaks are kept
  note                      small grey text
  rows                      [(label, value), ...] or [{"label", "value"}, ...]: a two-column table
  row                       one label and value
  list                      [text, ...]: a bulleted list
  signature                 a line to sign on: label, then the name and date when signed
  divider                   a thin line

Short aliases work too (h2, h3, p, text, small, table, fields, field, bullets, items, sign, hr).
Anything else raises ValueError, so a mistake shows up in tests rather than on paper.
"""

import html
import re

MAX_BLOCKS = 500
MAX_ROWS = 500
MAX_TEXT = 50_000        # characters in one piece of text
MAX_HTML = 2 * 1024 * 1024   # the whole page (gdrive.MAX_HTML)

ALIASES = {"h1": "heading", "h2": "heading", "title": "heading", "h3": "subheading",
           "p": "paragraph", "text": "paragraph", "para": "paragraph", "small": "note",
           "table": "rows", "fields": "rows", "field": "row", "bullets": "list", "items": "list",
           "ul": "list", "sign": "signature", "hr": "divider", "rule": "divider"}
KINDS = ("heading", "subheading", "paragraph", "note", "rows", "row", "list", "signature", "divider")

# Fixed styles (Google Docs keeps inline styles best). Nothing in them comes from people.
BODY = "font-family:Arial,Helvetica,sans-serif;font-size:11pt;color:#111111;line-height:1.35"
H1 = "font-size:18pt;margin:0 0 4pt 0"
H2 = "font-size:13pt;margin:14pt 0 4pt 0"
H3 = "font-size:11pt;margin:10pt 0 3pt 0"
P = "margin:0 0 6pt 0"
NOTE = "margin:0 0 6pt 0;font-size:9pt;color:#555555"
TABLE = "width:100%;border-collapse:collapse;margin:4pt 0 8pt 0"
LABEL = "width:34%;padding:3pt 8pt 3pt 0;vertical-align:top;color:#555555;border-bottom:1px solid #dddddd"
VALUE = "padding:3pt 0;vertical-align:top;border-bottom:1px solid #dddddd"
SIGN = "padding:22pt 8pt 2pt 0;vertical-align:bottom;border-bottom:1px solid #000000"
SIGN_DATE = "width:32%;padding:22pt 0 2pt 0;vertical-align:bottom;border-bottom:1px solid #000000"
SIGN_LABEL = "padding:2pt 0 10pt 0;font-size:9pt;color:#555555;vertical-align:top"
RULE = "border:none;border-top:1px solid #999999;margin:10pt 0"


def esc(value) -> str:
    """Text as HTML: escaped, control characters dropped, plain ASCII (others as &#NNN;)."""
    text = "" if value is None else str(value)
    if len(text) > MAX_TEXT:
        raise ValueError(f"A piece of text in a PDF is at most {MAX_TEXT} characters.")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f  ]", "", text)
    return html.escape(text, quote=True).encode("ascii", "xmlcharrefreplace").decode("ascii")


def _lines(value) -> str:
    """Text with its line breaks kept."""
    text = str("" if value is None else value).replace("\r\n", "\n").replace("\r", "\n")
    return "<br>".join(esc(line) for line in text.split("\n"))


def _paragraphs(value, style: str) -> str:
    text = str("" if value is None else value).replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    chunks = [c for c in re.split(r"\n[ \t]*\n", text) if c.strip()] or [""]
    return "".join(f'<p style="{style}">{_lines(c)}</p>' for c in chunks)


def _pairs(rows) -> list[tuple]:
    if not isinstance(rows, (list, tuple)) or len(rows) > MAX_ROWS:
        raise ValueError(f"Rows are a list of at most {MAX_ROWS} (label, value) pairs.")
    found = []
    for r in rows:
        if isinstance(r, dict):
            found.append((r.get("label", ""), r.get("value", "")))
        elif isinstance(r, (list, tuple)) and len(r) == 2:
            found.append((r[0], r[1]))
        else:
            raise ValueError("Each row is a (label, value) pair.")
    return found


def _rows(pairs: list[tuple]) -> str:
    cells = "".join(f'<tr><td style="{LABEL}">{esc(label)}</td><td style="{VALUE}">{_lines(value)}</td></tr>'
                    for label, value in pairs)
    return f'<table style="{TABLE}">{cells}</table>'


def _signature(label, name=None, signed=None) -> str:
    return (f'<table style="{TABLE}"><tr><td style="{SIGN}">{esc(name or "")}</td>'
            f'<td style="{SIGN_DATE}">{esc(signed or "")}</td></tr>'
            f'<tr><td style="{SIGN_LABEL}">{esc(label)}</td><td style="{SIGN_LABEL}">Date</td></tr></table>')


def _parse(block) -> tuple[str, list]:
    """(kind, its arguments) from a tuple, dict or string."""
    if isinstance(block, str):
        return "paragraph", [block]
    if isinstance(block, dict):
        keys = [k for k in block if ALIASES.get(k, k) in KINDS]
        if len(keys) != 1:
            raise ValueError(f"A block names one of: {', '.join(KINDS)}.")
        kind = ALIASES.get(keys[0], keys[0])
        if kind == "signature":
            return kind, [block[keys[0]], block.get("name"), block.get("date") or block.get("signed")]
        if kind == "row":
            value = block[keys[0]]
            if isinstance(value, (list, tuple)):
                return kind, list(value)
            return kind, [value, block.get("value", "")]
        return kind, [block[keys[0]]]
    if isinstance(block, (list, tuple)) and block and isinstance(block[0], str):
        kind = ALIASES.get(block[0], block[0])
        if kind in KINDS:
            return kind, list(block[1:])
    raise ValueError(f"A block is a string, or a tuple or dict naming one of: {', '.join(KINDS)}.")


def block_html(block) -> str:
    kind, args = _parse(block)
    first = args[0] if args else ""
    if kind == "heading":
        return f'<h2 style="{H2}">{esc(first)}</h2>'
    if kind == "subheading":
        return f'<h3 style="{H3}">{esc(first)}</h3>'
    if kind == "paragraph":
        return _paragraphs(first, P)
    if kind == "note":
        return _paragraphs(first, NOTE)
    if kind == "rows":
        return _rows(_pairs(first))
    if kind == "row":
        if len(args) != 2:
            raise ValueError("A row is a label and a value.")
        return _rows([(args[0], args[1])])
    if kind == "list":
        if not isinstance(first, (list, tuple)) or len(first) > MAX_ROWS:
            raise ValueError("A list is a list of texts.")
        items = "".join(f'<li style="{P}">{_lines(i)}</li>' for i in first)
        return f"<ul>{items}</ul>"
    if kind == "signature":
        if not 1 <= len(args) <= 3:
            raise ValueError("A signature is a label, then the name and date once signed.")
        return _signature(*args)
    return f'<hr style="{RULE}">'   # divider


def page(title: str, blocks: list, *, subtitle: str | None = None, footer: str | None = None) -> str:
    """A whole page of plain, safe HTML for documents.make_pdf. Raises ValueError for a block it
    doesn't know, or a page too long to make into a PDF."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError("A PDF needs a title.")
    if not isinstance(blocks, (list, tuple)) or len(blocks) > MAX_BLOCKS:
        raise ValueError(f"A PDF has at most {MAX_BLOCKS} blocks.")
    parts = [f'<h1 style="{H1}">{esc(title.strip())}</h1>']
    if subtitle:
        parts.append(_paragraphs(subtitle, NOTE))
    parts.extend(block_html(b) for b in blocks)
    if footer:
        parts.append(f'<hr style="{RULE}">' + _paragraphs(footer, NOTE))
    text = ('<!doctype html><html><head><meta charset="utf-8"><title>' + esc(title.strip()) +
            f'</title></head><body style="{BODY}">' + "".join(parts) + "</body></html>")
    if len(text) > MAX_HTML:
        raise ValueError("That's too long to make into a PDF.")
    return text
