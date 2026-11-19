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
// Sorted by name: the PDF follows the rows it's given, and says the order.
fs.writeFileSync(path.join(out, "sorted.pdf"), view.reportPdf({ ...W, rows: view.sortRows(W.rows, "name") }, { period: d.period, dept: "",
  mode: "summary", open, R, noPunches: [], hasSchedule: true, sorted: "Name (A to Z)", made: "Oct 19, 2026 9:05 AM" }));
// Forty people (the same six over again): the summary is one page, staff table and forms.
const forty = { ...W, rows: Array.from({ length: 7 }, () => W.rows).flat().slice(0, 40) };
for (const mode of ["all", "summary"]) {
  fs.writeFileSync(path.join(out, `forty-${mode}.pdf`), view.reportPdf(forty, { period: d.period, dept: "", mode, open, R,
    noPunches: [], hasSchedule: true, made: "Oct 19, 2026 9:05 AM" }));
}
// Very long names, jobs and form names: everything must still stay inside its box.
const longer = s => s.replace(/Lena Brooks/g, "Maximiliana Featherstonehaugh-Wolfeschlegelsteinhausen")
  .replace(/Thermal Pub/g, "Thermal Pub & Geothermal Soaking Pools Supervisor")
  .replace(/Tub temperature check/g, "Tub temperature and chlorine check with a very long name")
  .replace(/Front Desk/g, "Supercalifragilisticexpialidocious");
const d2 = JSON.parse(longer(JSON.stringify(d)));
const model2 = view.buildModel(d2, R);
const W2 = view.computePeriod(model2, { start: d2.start_day, end: d2.end_day }, { R, dept: "", now: d2.now, today: d2.today });
fs.writeFileSync(path.join(out, "stress.pdf"), view.reportPdf(W2, { period: d2.period, dept: "", mode: "all", open, R,
  noPunches: model2.noPunches, hasSchedule: model2.hasSchedule, made: "Oct 19, 2026 9:05 AM" }));
// Letters the PDF can't show.
const doc = view.pdfDoc(200, 100);
doc.page();
doc.text("Zoë (Núñez) \\ 日本 → 5 − 2 “ok” 😀", 10, 20);
fs.writeFileSync(path.join(out, "letters.pdf"), doc.bytes());
process.stdout.write(JSON.stringify({ people: W.rows.map(r => r.p.name), keys: W.rows.map(r => r.p.key),
  widths: [view.HELV.length, view.HELV_BOLD.length], codes: view.pdfCodes("é→😀") }));
