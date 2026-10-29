"""The staff app and the Attendance report's page, under Node (skipped when node isn't
installed): nothing from one sign-in is left on screen for the next person, and CSV cells a
spreadsheet would run as formulas are quoted as text. Nothing here talks to Connecteam."""

import asyncio
import json
import os
import re
import shutil
import subprocess

import pytest

from attendance_fake import HERE, REPORT_DIR, load_report, sample

rep = load_report()
VIEW = os.path.join(REPORT_DIR, "view.js")
APP = os.path.join(HERE, "..", "app", "staff", "app.js")
need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed")


def app_part(pattern):
    """A piece of app.js (a function, from its first line to its closing brace)."""
    source = open(APP, encoding="utf-8").read()
    found = re.search(pattern, source, re.S)
    assert found, pattern
    return found.group(0)


def node(script):
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


# A tiny stand-in for the page: elements that know their parent, h(), document and window.
FAKE_DOM = """
const eq = (a, b, what) => { if (JSON.stringify(a) !== JSON.stringify(b)) { console.error(what, JSON.stringify(a)); process.exit(1); } };
function el(tag, attrs) {
  const e = { tag, attrs: attrs || {}, parent: null, kids: [], focus() {},
    append(...k) { for (const c of k) { c.parent = e; e.kids.push(c); } },
    remove() { if (e.parent) { e.parent.kids = e.parent.kids.filter(c => c !== e); e.parent = null; } },
    querySelector() { return { focus() {} }; } };
  return e;
}
const h = (tag, attrs, ...kids) => { const e = el(tag, attrs); e.append(...kids.flat().filter(k => k && typeof k === "object")); return e; };
const listeners = [];
const window = { addEventListener: (t, f) => listeners.push(f), removeEventListener: (t, f) => { const i = listeners.indexOf(f); if (i >= 0) listeners.splice(i, 1); } };
const all = (node, test) => [...(test(node) ? [node] : []), ...node.kids.flatMap(k => all(k, test))];
const document = { head: el("head"), body: Object.assign(el("body"), { classList: { remove() {} } }),
  querySelector: sel => document.querySelectorAll(sel)[0] || null,
  querySelectorAll: sel => {
    const m = sel.match(/^(\\w+)\\[data-report(?:="(.*)")?\\]$/);
    return all(document.head, n => n.tag === m[1] && "data-report" in n.attrs && (m[2] === undefined || n.attrs["data-report"] === m[2]));
  } };
const history = { state: null };
"""


# ---------------------------------------------------------------- sign-out

@need_node
def test_report_views_load_fresh_after_sign_out():
    """A report's view.js keeps its own state (the Attendance Rules tab, an unsaved draft), so
    rpReset forgets the loaded views: the next person's rpLoad runs view.js again."""
    script = FAKE_DOM + "\n".join([
        app_part(r"  const reportViews = new Map\(\);.*?\n  let rp = null;\n"),
        app_part(r"  window\.StaffReports = Object\.freeze\(\{.*?\n  \}\);\n"),
        app_part(r"  function rpReset\(\) \{.*?\n  \}\n"),
        app_part(r"  function rpLoad\(report\) \{.*?\n  \}\n"),
    ]) + """
(async () => {
  const report = { key: "attendance", view_js: "/reports/attendance/view.js" };
  const scripts = () => document.querySelectorAll("script[data-report]").length;
  // A's sign-in: the script loads and registers its view
  let loaded = rpLoad(report);
  eq(scripts(), 1, "first load adds the script");
  const first = { render() {} };
  window.StaffReports.register("attendance", first);
  eq(await loaded, true, "first load");
  // sign-out, then B opens the report: a fresh copy of view.js, not A's
  rpReset();
  eq(scripts(), 0, "sign-out removes the loaded script");
  loaded = rpLoad(report);
  eq(scripts(), 1, "the next sign-in loads it again");
  const second = { render() {} };
  window.StaffReports.register("attendance", second);
  eq(await loaded, true, "second load");
  eq(reportViews.get("attendance") === second, true, "the next person gets the new view");
  // sign-out while view.js is still loading: the old try failing late doesn't spoil the next one
  rpReset();
  const late = rpLoad(report);
  const oldScript = document.querySelector("script[data-report]");
  rpReset();
  loaded = rpLoad(report);
  oldScript.attrs.onerror();
  eq(await late, false, "the old try gives up");
  const third = { render() {} };
  window.StaffReports.register("attendance", third);
  eq(await loaded, true, "the next load still finishes");
})();
"""
    node(script)


@need_node
def test_task_photo_closes_at_sign_out():
    """The full-screen task photo sits on document.body, over the PIN screen: tkReset (sign-out,
    lock, an error screen) and closing the task sheet close it, with its key listener."""
    decl = re.search(r"\n  let tkPhotoClose[^\n]*\n", open(APP, encoding="utf-8").read())
    script = FAKE_DOM + "let tk = null;\nconst tkIcon = () => null;\n" + (decl.group(0) if decl else "") + "\n".join([
        app_part(r"  function tkReset\(\) \{.*?\n  \}\n"),
        app_part(r"  function tkCloseSheet\(fromBack\) \{.*?\n  \}\n"),
        app_part(r"  function tkPhotoBig\(data, label\) \{.*?\n  \}\n"),
    ]) + """
const photos = () => document.body.kids.filter(k => k.attrs.class === "tk-photo-big").length;
const openSheet = () => { tk = { sheet: { el: el("div") } }; document.body.append(tk.sheet.el); };
// A opens a task and its photo, then the session ends
openSheet();
tkPhotoBig("data:image/png;base64,AA==", "Photo 1");
eq([photos(), listeners.length], [1, 1], "photo open");
tkReset();
eq([photos(), listeners.length, document.body.kids.length], [0, 0, 0], "gone at sign-out");
// a photo left open after the Tasks screen was already forgotten
tkPhotoBig("data:image/png;base64,AA==", "Photo 1");
tkReset();
eq([photos(), listeners.length], [0, 0], "gone even with no tasks screen");
// closing the sheet (Back) closes its photo; a second photo replaces the first
openSheet();
tkPhotoBig("data:image/png;base64,AA==", "Photo 1");
tkPhotoBig("data:image/png;base64,AA==", "Photo 2");
eq([photos(), listeners.length], [1, 1], "one photo at a time");
tkCloseSheet(true);
eq([photos(), listeners.length], [0, 0], "gone with the sheet");
"""
    node(script)


# ---------------------------------------------------------------- CSV formulas

@need_node
def test_attendance_csv_quotes_a_name_starting_with_minus_and_a_digit(tmp_path):
    _, ctx = sample()
    data = asyncio.run(rep.data(ctx, {"period": "week", "at": "2026-10-14"}))
    data["users"]["6"] = "-1+cmd|' /C calc'!A0"
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"data": data}))
    done = subprocess.run(["node", os.path.join(HERE, "attendance_view_test.js"), VIEW, str(path)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    lines = json.loads(done.stdout)["csv"].split("\n")
    assert any(",'-1+cmd|' /C calc'!A0," in line for line in lines)  # not run as a formula
    assert not any(",-1+cmd" in line for line in lines)


@need_node
def test_report_csv_cell_quotes_formulas_but_not_numbers():
    script = app_part(r"  function rpCsvCell\(v\) \{.*?\n  \}\n") + """
const eq = (a, b) => { if (a !== b) { console.error(JSON.stringify(a), "!=", JSON.stringify(b)); process.exit(1); } };
eq(rpCsvCell("=Sum(1)"), "'=Sum(1)");
eq(rpCsvCell("-1+cmd|' /C calc'!A0"), "'-1+cmd|' /C calc'!A0");
eq(rpCsvCell("@x"), "'@x");
eq(rpCsvCell("\\tHousekeeping"), "'\\tHousekeeping");
eq(rpCsvCell("-1.5"), "-1.5");
eq(rpCsvCell(-2), "-2");
eq(rpCsvCell(""), "");
eq(rpCsvCell(null), "");
eq(rpCsvCell('Ortiz, "Maya"'), '"Ortiz, ""Maya""\\x22');
eq(rpCsvCell("+1,2"), `"'+1,2"`);
"""
    node(script)
