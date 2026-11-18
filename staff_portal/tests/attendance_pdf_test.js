// Writes the attendance report's PDF files (view.js's reportPdf) for test_report_attendance.py to
// open with a PDF reader. Usage: node attendance_pdf_test.js <view.js> <job.json> <folder>
// job: { data: report.py's output, dept: "", open: [person keys] or null (the first person) }
// Prints {people: [names], keys: [keys], widths: [how many character widths each font has]}.
"use strict";
const fs = require("fs");
const path = require("path");
const view = require(process.argv[2]);
const job = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const out = process.argv[4];
const d = job.data;
const R = { ...view.DEFAULT_RULES, ...((d.settings || {}).rules || {}) };
const model = view.buildModel(d, R);
const W = view.computePeriod(model, { start: d.start_day, end: d.end_day }, { R, dept: job.dept || "", now: d.now, today: d.today });
const open = new Set(job.open || W.rows.slice(0, 1).map(r => r.p.key));
for (const mode of ["all", "open", "summary"]) {
  const bytes = view.reportPdf(W, { period: d.period, dept: job.dept || "", mode, open, R, noPunches: model.noPunches,
    hasSchedule: model.hasSchedule, made: "Oct 19, 2026 9:05 AM" });
  fs.writeFileSync(path.join(out, `${mode}.pdf`), bytes);
}
// A long made-up report, for page breaks: every person many times over.
const many = { ...W, rows: Array.from({ length: 12 }, () => W.rows).flat() };
fs.writeFileSync(path.join(out, "long.pdf"), view.reportPdf(many, { period: d.period, dept: "", mode: "all", open, R,
  noPunches: [], hasSchedule: true, made: "Oct 19, 2026 9:05 AM" }));
// Letters the PDF can't show.
const doc = view.pdfDoc(200, 100);
doc.page();
doc.text("Zoë (Núñez) \\ 日本 → 5 − 2 “ok” 😀", 10, 20);
fs.writeFileSync(path.join(out, "letters.pdf"), doc.bytes());
process.stdout.write(JSON.stringify({ people: W.rows.map(r => r.p.name), keys: W.rows.map(r => r.p.key),
  widths: [view.HELV.length, view.HELV_BOLD.length], codes: view.pdfCodes("é→😀") }));
