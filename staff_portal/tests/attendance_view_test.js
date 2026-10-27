// Runs the attendance view's calculations (view.js, loaded as a module) on report data and
// prints what the page would show, as JSON, for test_report_attendance.py to check.
// Usage: node attendance_view_test.js <view.js> <job.json>
// job: { data: report.py's output, dept: "", periods: [[day, kind, weekStart], ...] }
"use strict";
const fs = require("fs");
const view = require(process.argv[2]);
const job = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const d = job.data;
const R = { ...view.DEFAULT_RULES, ...((d.settings || {}).rules || {}) };
const model = view.buildModel(d, R);
const W = view.computePeriod(model, { start: d.start_day, end: d.end_day }, { R, dept: job.dept || "", now: d.now, today: d.today });
const out = {
  rows: W.rows.map(r => ({
    name: r.p.name, key: r.p.key, dept: r.p.dept, title: r.p.title, status: r.status, late: r.late, early: r.early, lunch: r.lunch,
    missed: r.missed.length, missedHrs: r.missedHrs, formsExpected: r.formsExpected, formsMissed: r.formsMissed, formMisses: r.formMisses,
    workedMin: r.workedMin, sick: r.sick, pto: r.pto, sickLow: r.sickLow, ptoLow: r.ptoLow, sickEarly: r.sickEarly, cover: r.cover,
    offHrs: r.offHrs, unchecked: r.unchecked,
    flags: r.days.flatMap(day => day.blocks.flatMap(b => b.flags.map(f => f[1]))),
  })),
  forms: W.forms.map(f => ({ name: f.name, expected: f.expected, done: f.done, missed: f.missed, misses: f.misses,
    grid: f.grid ? f.grid.map(g => g.cells.map(c => c.st).join(",")) : null })),
  totals: W.totals, inProgress: W.inProgress,
  departments: model.departments, fuzzy: model.fuzzy, balUnmatched: model.balUnmatched, noBal: model.noBal, gustoOk: model.gustoOk,
  csv: view.csvText(W), summary: view.summaryText(W, d.period, job.dept || ""), file: view.fileName(W, d.period, job.dept || "", "csv"),
  periods: (job.periods || []).map(([day, kind, ws]) => view.periodOf(day, kind, ws)),
  gusto: (job.gustoTexts || []).map(t => { const g = view.gustoTable(t); return g ? Object.keys(g.map).sort() : null; }),
};
process.stdout.write(JSON.stringify(out));
