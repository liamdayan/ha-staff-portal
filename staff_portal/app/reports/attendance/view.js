// Attendance & Leave Weekly in the staff app. A port of the original report page: the same
// checks (late, early, missed punches, lunch rules, leave cover, balances, the forms grid),
// now fed by report.py from Connecteam and the newest Gusto upload. Times are local wall-clock
// minutes since 1970 ("abs minutes") and days are day numbers since then, as in the original,
// so day maths ignores time zones and daylight saving. The calculations are kept close to the
// original's so the two are easy to compare; they're exported for the Node tests at the end.
(function () {
  "use strict";

  // ---------------------------------------------------------------- small helpers
  const DAY_MS = 864e5;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const DOW_LONG = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

  const ymd = (y, m, d) => { const t = Date.UTC(y, m - 1, d); const x = new Date(t); return x.getUTCMonth() === m - 1 && x.getUTCDate() === d ? Math.round(t / DAY_MS) : null; };
  const dow = n => (((n % 7) + 7) + 4) % 7;
  const dt = n => new Date(n * DAY_MS);
  const iso = n => dt(n).toISOString().slice(0, 10);
  const fmtDay = n => `${DOW[dow(n)]} ${MONTHS[dt(n).getUTCMonth()]} ${dt(n).getUTCDate()}`;
  const fmtDate = n => `${MONTHS[dt(n).getUTCMonth()]} ${dt(n).getUTCDate()}, ${dt(n).getUTCFullYear()}`;
  const fmtShort = n => `${MONTHS[dt(n).getUTCMonth()]} ${dt(n).getUTCDate()}`;
  const fmtRange = (a, b) => {
    const A = dt(a), B = dt(b);
    if (A.getUTCFullYear() !== B.getUTCFullYear()) return `${fmtDate(a)} – ${fmtDate(b)}`;
    if (A.getUTCMonth() !== B.getUTCMonth()) return `${MONTHS[A.getUTCMonth()]} ${A.getUTCDate()} – ${MONTHS[B.getUTCMonth()]} ${B.getUTCDate()}, ${B.getUTCFullYear()}`;
    return `${MONTHS[A.getUTCMonth()]} ${A.getUTCDate()} – ${B.getUTCDate()}, ${B.getUTCFullYear()}`;
  };
  const fmtClock = abs => { const m = ((abs % 1440) + 1440) % 1440; let hr = Math.floor(m / 60); const mi = m % 60; const ap = hr < 12 ? "a" : "p"; hr = hr % 12 || 12; return `${hr}:${String(mi).padStart(2, "0")}${ap}`; };
  // Durations read as minutes under an hour and hours + minutes above it: 45 min, 3h 30m, 8h.
  const fmtDur = m => { const neg = m < 0; m = Math.round(Math.abs(m)); const s = m === 0 ? "0h" : m < 60 ? `${m} min` : m % 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m / 60}h`; return neg ? "−" + s : s; };
  const fmtH = hrs => fmtDur(hrs * 60);
  const fmtHrs = hrs => { const v = Math.round(hrs * 10) / 10; return Number.isInteger(v) ? v.toFixed(0) : v.toFixed(1); };
  const plural = (n, w, p) => `${n} ${n === 1 ? w : (p || w + "s")}`;

  // ---------------------------------------------------------------- CSV (Gusto's file)
  function parseCSV(text) {
    text = String(text || "").replace(/^﻿/, "");
    const head = text.split(/\r?\n/).slice(0, 8).join("\n");
    let d = ",", best = -1;
    for (const c of [",", ";", "\t"]) { const n = head.split(c).length - 1; if (n > best) { best = n; d = c; } }
    const rows = []; let row = [], f = "", q = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (q) {
        if (c === '"') { if (text[i + 1] === '"') { f += '"'; i++; } else q = false; }
        else f += c;
      } else if (c === '"' && f === "") q = true;
      else if (c === d) { row.push(f); f = ""; }
      else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(f); rows.push(row); row = []; f = ""; }
      else f += c;
    }
    if (f !== "" || row.length) { row.push(f); rows.push(row); }
    return rows.filter(r => r.some(v => String(v).trim() !== ""));
  }
  function parseHours(s) {
    s = String(s || "").trim(); if (!s) return null;
    const m = s.match(/^(-?\d{1,4}):(\d{2})$/);
    if (m) return +m[1] + (+m[1] < 0 ? -1 : 1) * (+m[2] / 60);
    const n = parseFloat(s.replace(/[^\d.\-]/g, ""));
    return isNaN(n) ? null : n;
  }

  // ---------------------------------------------------------------- names
  function normKey(n) {
    n = String(n || "").trim(); if (!n) return "";
    if (/,/.test(n)) { const [a, ...b] = n.split(","); n = `${b.join(" ")} ${a}`; }
    const t = n.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z'\- ]+/g, " ").split(/\s+/).filter(w => w.replace(/['\-]/g, "").length > 1);
    if (!t.length) return "";
    return t.length > 1 ? `${t[0]} ${t[t.length - 1]}` : t[0];
  }
  function niceName(n) {
    n = String(n || "").trim().replace(/\s+/g, " ");
    if (/,/.test(n)) { const [a, ...b] = n.split(","); n = `${b.join(" ").trim()} ${a.trim()}`; }
    if (n === n.toUpperCase() || n === n.toLowerCase()) n = n.toLowerCase().replace(/(^|[\s\-'])([a-z])/g, (_, p, c) => p + c.toUpperCase());
    return n;
  }
  // Common short names, kept the same as report.py's NICKNAMES: each line is one name and the names it goes by.
  const NICKNAMES = [
    "abigail abby", "albert al bert", "alexander alex xander", "alexandra alex alexa sandra sasha", "alfred al alfie fred", "andrew andy drew",
    "anthony tony", "antonia toni tonia", "barbara barb barbie", "benjamin ben benji", "catherine cathy kate katie cat",
    "charles charlie chuck chaz", "christina chris tina", "christine chris tina", "christopher chris kit", "daniel dan danny", "david dave davey",
    "deborah deb debbie", "donald don donnie", "dorothy dot dottie", "douglas doug", "edward ed eddie ted ned",
    "elizabeth liz lizzie beth betty eliza libby", "eugene gene", "frances fran frannie", "francis frank frankie", "frederick fred freddie",
    "gerald gerry jerry", "gregory greg", "harold harry hal", "henry hank harry", "isabel izzy bella", "isabella izzy bella", "jacob jake",
    "james jim jimmy jamie", "jennifer jen jenny", "jeffrey jeff", "jessica jess jessie", "john jack johnny", "jonathan jon jonny",
    "joseph joe joey", "joshua josh", "katherine kate katie kathy kat", "kathryn kate katie kathy kat", "kenneth ken kenny", "lawrence larry",
    "leonard leo len lenny", "louis lou", "margaret maggie meg peggy", "matthew matt", "michael mike mikey", "nathan nate", "nathaniel nate nathan",
    "nicholas nick nicky", "patricia pat patty trish", "patrick pat paddy", "peter pete", "philip phil", "raymond ray", "rebecca becky becca",
    "richard rick ricky dick rich", "robert rob robbie bob bobby bert", "ronald ron ronnie", "samantha sam sammy", "samuel sam sammy",
    "stephen steve", "steven steve", "susan sue susie", "theodore ted teddy theo", "thomas tom tommy", "timothy tim timmy", "victoria vicky tori",
    "vincent vince", "walter walt", "william will bill billy liam", "zachary zach zack", "alejandro alex", "eduardo lalo eddie",
    "francisco paco pancho frank", "guillermo memo will", "ignacio nacho", "jesus chuy", "jose pepe", "manuel manny", "roberto beto rob"];
  const NICK = new Map();
  for (const line of NICKNAMES) { const names = line.split(" "); for (const n of names) { if (!NICK.has(n)) NICK.set(n, new Set()); names.forEach(x => NICK.get(n).add(x)); } }
  // Nickname check for unmatched names: same last name, and first names like Nate/Nathan, Toni/Tonia, Liam/William, Tom/Thomas.
  function firstNamesAlike(a, b) {
    if (a === b || (NICK.has(a) && NICK.get(a).has(b))) return true;
    if (a.length < 3 || b.length < 3) return false;
    return a.startsWith(b) || b.startsWith(a) || a.endsWith(b) || b.endsWith(a) || a.slice(0, 3) === b.slice(0, 3);
  }
  const NOT_A_PERSON = /^(unassigned|open shift|open|none|n\/a|-+|total|totals|grand total)$/i;

  // ---------------------------------------------------------------- Gusto column detection
  const NAME_FIELDS = [
    { k: "employee", label: "Employee name", strong: /^(employee|employee name|employee full name|name|full name|user|users|user name|team member|staff( member)?|worker|submitted by|submitter|assigned to|assignee)$/i, weak: /employee|user|staff|submitted by|assigned|name/i, not: /first|last|job|form|company|manager|approved|location|position|title|policy|\bid\b|number|#|email|phone|type|note|comment/i },
    { k: "firstName", label: "First name", strong: /^(first|first name|given name|legal first name|preferred first name)$/i, weak: /first ?name/i },
    { k: "lastName", label: "Last name", strong: /^(last|last name|surname|family name|legal last name)$/i, weak: /last ?name|surname/i },
  ];
  const BALANCES = {
    fields: [...NAME_FIELDS,
      { k: "title", label: "Title / department", strong: /^(title|job title|department|dept|position|role)$/i, weak: /title|department|position|role/i },
      { k: "accrued", label: "Accrued this year (hrs)", strong: /^(accrued|accrued time|accrued hours|hours accrued|earned|hours earned)$/i, weak: /accru|earned/i },
      { k: "used", label: "Hours used this year", strong: /^(hours used|used|used hours|hours taken|taken)$/i, weak: /used|taken/i },
      { k: "pto", label: "PTO / vacation balance (hrs)", strong: /^(pto|vacation|paid time off|pto balance|vacation balance|paid time off balance)(\s*\((hrs|hours)\))?$/i, weak: /pto|vacation|paid time off|personal/i, prefer: /bal|remain|avail/i, avoid: /used|taken|accru|ytd|rate|max|cap|request|pending/i },
      { k: "sick", label: "Sick balance (hrs)", strong: /^(sick|sick leave|sick time|sick balance|sick leave balance)(\s*\((hrs|hours)\))?$/i, weak: /sick/i, prefer: /bal|remain|avail/i, avoid: /used|taken|accru|ytd|rate|max|cap|request|pending/i },
      { k: "policy", label: "Policy name (one row per policy)", strong: /^(policy|time off policy|policy name|time off type|category)$/i, weak: /policy/i },
      { k: "balance", label: "Balance (one row per policy)", strong: /^(balance|ending balance|current balance|remaining|available|hours remaining|balance\s*\((hrs|hours)\))$/i, weak: /balance|remaining|available/i, not: /pto|vacation|sick/i },
    ],
    need: m => (has(m, "employee") || has(m, "firstName")) && (has(m, "pto") || has(m, "sick") || (has(m, "policy") && has(m, "balance"))),
  };
  function has(m, k) { return m && m[k] != null && m[k] >= 0; }
  function scoreHeader(f, hd) {
    hd = String(hd || "").trim(); if (!hd) return 0;
    if (f.not && f.not.test(hd)) return 0;
    let s = f.strong.test(hd) ? 3 : f.weak.test(hd) ? 1 : 0;
    if (!s) return 0;
    if (f.prefer && f.prefer.test(hd)) s += 1;
    if (f.avoid && f.avoid.test(hd)) s *= 0.2;
    return s;
  }
  function autoMap(headers) {
    const pairs = [];
    BALANCES.fields.forEach((f, fi) => headers.forEach((hd, ci) => { const s = scoreHeader(f, hd); if (s > 0) pairs.push({ k: f.k, ci, s, fi }); }));
    pairs.sort((a, b) => b.s - a.s || a.fi - b.fi || a.ci - b.ci);
    const map = {}, used = new Set();
    for (const p of pairs) { if (p.k in map || used.has(p.ci)) continue; map[p.k] = p.ci; used.add(p.ci); }
    return map;
  }
  function detectHeader(rows) {
    let best = 0, bestScore = -1;
    for (let i = 0; i < Math.min(rows.length, 25); i++) {
      const n = Object.keys(autoMap(rows[i])).length;
      if (n > bestScore) { best = i; bestScore = n; }
    }
    return best;
  }
  // A Gusto file's table: the header row found by its columns (Gusto puts a title and the
  // company's address above it), the columns matched, and the rows below. null if it isn't one.
  function gustoTable(text) {
    const rows = parseCSV(text);
    if (rows.length < 2) return null;
    const hr = detectHeader(rows), map = autoMap(rows[hr] || []);
    return BALANCES.need(map) ? { headers: rows[hr], data: rows.slice(hr + 1), map } : null;
  }
  const cell = (r, m, k) => has(m, k) ? String(r[m[k]] ?? "").trim() : "";
  function rowName(r, m) {
    let n = cell(r, m, "employee");
    if (!n) n = [cell(r, m, "firstName"), cell(r, m, "lastName")].filter(Boolean).join(" ");
    return n;
  }

  // ---------------------------------------------------------------- model
  const DEFAULT_RULES = { weekStart: 0, lateGrace: 5, earlyGrace: 5, lunchAfter: 6, lunchMin: 30, defaultShift: 8, lowSick: 8, lowPto: 2, actionAt: 4, aliases: "", earlySkip: [] };
  const TIMEOFF_RE = /time.?off|\bpto\b|sick|vacation|holiday|leave|bereave|jury|personal day|\bvto\b/i;
  const BREAK_RE = /break|lunch|meal/i;
  function aliasMap(text) {
    const m = new Map();
    String(text || "").split(/\n/).forEach(line => {
      const [a, b] = line.split(/=|→|->/).map(x => (x || "").trim());
      if (a && b) m.set(normKey(a), normKey(b));
    });
    return m;
  }
  function mkShift(start, end, job) {
    if (end != null && end <= start) end += 1440;
    return { start, end: end ?? null, job: job || "" };
  }
  // The department a person is shown under: their Staff Access department (Connecteam smart
  // group) when known, otherwise their Gusto title, as the original did.
  const deptOf = p => p.dept || p.title || "No department";
  // Every department a person is in: the department filter shows them under each one.
  const deptsOf = p => p.depts && p.depts.length ? p.depts : [deptOf(p)];

  function buildModel(d, R) {
    const A = aliasMap(R.aliases);
    const people = new Map(), byName = new Map();
    const keyOf = raw => { const k = normKey(raw); return A.get(k) || k; };
    const users = d.users || {}, deptByUser = d.departments || {}, deptsByUser = d.department_lists || {};
    // People are keyed by their Connecteam user ID, or by name (the original's way) without one.
    const ref = (raw, uid) => {
      raw = (uid != null && users[uid]) || raw;
      if (!raw || NOT_A_PERSON.test(String(raw).trim())) return null;
      const nk = keyOf(raw), k = uid != null ? "u" + uid : nk;
      if (!k) return null;
      let p = people.get(k);
      if (!p && uid == null && byName.get(nk)) p = byName.get(nk);
      if (!p) {
        p = { key: k, uid: uid ?? null, nk, name: niceName(raw), dept: uid != null ? deptByUser[uid] || null : null, depts: uid != null ? deptsByUser[uid] || null : null, punches: [], shifts: [], off: new Map(), bal: null, title: null, jobs: new Set(), blips: new Set() };
        people.set(k, p);
        // two people with the same name can't be told apart by name: neither is matched by it
        if (nk) byName.set(nk, byName.has(nk) && byName.get(nk) !== p ? false : p);
      }
      return p;
    };
    const fuzzy = [];
    // Find a Connecteam person for a name from Gusto or a form: exact key, alias, then a unique nickname match.
    const lookup = raw => {
      const k = keyOf(raw); const hit = byName.get(k);
      if (hit) return hit;
      if (hit === false) return null;
      const [f, l] = k.split(" "); if (!l) return null;
      const c = [...people.values()].filter(p => { const [pf, pl] = p.nk.split(" "); return pl === l && firstNamesAlike(f, pf); });
      if (c.length === 1) { fuzzy.push([niceName(raw), c[0].name]); return c[0]; }
      return null;
    };

    // Gusto names to people, as report.py's match_names does (it filters the file the same way): by name or
    // alias, else by a short name only when it's the one person it could be, that person has no row of their own,
    // and no other Gusto name points at them too (so Devi Patel's balance never lands on Dev Patel's row).
    const matchGusto = raws => {
      const found = new Map(), exact = new Set(), guesses = [];
      for (const raw of new Set(raws.filter(x => x && !NOT_A_PERSON.test(x)))) {
        const k = keyOf(raw), hit = byName.get(k);
        if (hit) { found.set(raw, hit); exact.add(hit); continue; }
        const [f, l] = k.split(" "); if (hit === false || !l) continue;
        const c = [...people.values()].filter(p => { const [pf, pl] = p.nk.split(" "); return pl === l && firstNamesAlike(f, pf); });
        if (c.length === 1) guesses.push([raw, c[0]]);
      }
      const count = new Map(); guesses.forEach(([, p]) => count.set(p, (count.get(p) || 0) + 1));
      for (const [raw, p] of guesses) if (!exact.has(p) && count.get(p) === 1) { found.set(raw, p); fuzzy.push([niceName(raw), p.name]); }
      return found;
    };

    // timesheets: work punches, breaks, and time off
    const punchJobs = new Set(), inTimesheets = new Set();
    for (const t of d.timesheets || []) {
      const p = ref(t.name, t.uid); if (!p) continue;
      inTimesheets.add(p);
      const type = t.type || "";
      if (type && TIMEOFF_RE.test(type) && !BREAK_RE.test(type)) {
        let hrs = t.hours;
        if (!hrs && t.in != null && t.out != null) hrs = (t.out - t.in) / 60;
        const label = type.replace(/^time[\s\-]?off\s*[-:–]?\s*/i, "").trim() || t.job || "Time off";
        const o = p.off.get(t.day);
        p.off.set(t.day, { type: o ? o.type : label, hrs: (o ? o.hrs : 0) + (hrs || 0) });
        continue;
      }
      if (t.in == null) continue;
      const isBreak = BREAK_RE.test(type) || (!type && BREAK_RE.test(t.job || ""));
      const job = !isBreak && t.job && !/^shift$/i.test(t.job) ? t.job : "";
      p.punches.push({ in: t.in, out: t.out ?? null, brk: t.brk || 0, job, isBreak });
      if (job) { p.jobs.add(job.toLowerCase()); punchJobs.add(job); }
      if (t.ss != null) p.shifts.push(mkShift(t.ss, t.se, ""));
    }

    // schedules: drafts are skipped; an open shift still says its job is on the schedule
    const schedJobs = new Set(); let shiftCount = 0;
    for (const s of d.shifts || []) {
      if (s.draft) continue;
      if (s.job) schedJobs.add(s.job.toLowerCase());
      shiftCount++;
      if (s.open || s.start == null) continue;
      const p = ref(s.name, s.uid); if (!p) continue;
      const label = [s.job, s.title].filter((v, i, a) => v && !(i && a[0] && a[0].toLowerCase() === v.toLowerCase())).join(" · ");
      p.shifts.push(mkShift(s.start, s.end, label));
      if (s.job) p.jobs.add(s.job.toLowerCase());
    }

    // tidy: drop duplicate punches and merge overlapping shifts
    for (const p of people.values()) {
      const seen = new Set();
      // a clock-in and clock-out in the same minute or two isn't a shift worked
      p.blips = new Set(p.punches.filter(x => !x.isBreak && x.out != null && x.out - x.in < 2).map(x => Math.floor(x.in / 1440)));
      p.punches = p.punches.filter(x => x.isBreak || x.out == null || x.out - x.in >= 2);
      p.punches = p.punches.sort((a, b) => a.in - b.in || (a.out ?? 0) - (b.out ?? 0)).filter(x => { const id = `${x.in}|${x.out}|${x.isBreak}`; if (seen.has(id)) return false; seen.add(id); return true; });
      p.shifts.sort((a, b) => a.start - b.start);
      const merged = [];
      for (const s of p.shifts) {
        const prev = merged[merged.length - 1];
        const end = s.end ?? s.start + R.defaultShift * 60;
        // the same shift twice, and back-to-back shifts up to an hour apart, become one block
        if (prev && s.start <= prev.end + 60) {
          prev.end = Math.max(prev.end, end);
          const parts = [...prev.job.split(" · "), ...s.job.split(" · ")].filter(Boolean);
          prev.job = parts.filter((v, i) => parts.findIndex(x => x.toLowerCase() === v.toLowerCase()) === i).join(" · ");
        } else merged.push({ start: s.start, end, job: s.job, noEnd: s.end == null });
      }
      p.shifts = merged;
    }

    // Gusto balances: the newest upload, matched onto Connecteam people
    const balUnmatched = new Set(); let balPeople = 0;
    const bt = d.gusto ? gustoTable(d.gusto.text) : null;
    if (bt) {
      const long = !has(bt.map, "pto") && !has(bt.map, "sick") && has(bt.map, "policy") && has(bt.map, "balance");
      const matched = matchGusto(bt.data.map(r => rowName(r, bt.map)));
      const seenRaw = new Set();
      for (const r of bt.data) {
        const raw = rowName(r, bt.map); if (!raw || NOT_A_PERSON.test(raw)) continue;
        if (!seenRaw.has(raw)) { seenRaw.add(raw); balPeople++; }
        const p = matched.get(raw);
        if (!p) { balUnmatched.add(niceName(raw)); continue; }
        p.bal = p.bal || { pto: null, sick: null, sickUsed: null, ptoUsed: null, sickAccrued: null, ptoAccrued: null };
        const title = cell(r, bt.map, "title"); if (title) p.title = title;
        if (long) {
          const pol = cell(r, bt.map, "policy"), v = parseHours(cell(r, bt.map, "balance")), u = parseHours(cell(r, bt.map, "used"));
          if (v == null) continue;
          const kind = /sick/i.test(pol) ? "sick" : /pto|vacation|paid|personal|flex/i.test(pol) ? "pto" : null;
          if (!kind) continue;
          p.bal[kind] = (p.bal[kind] || 0) + v;
          if (u != null) p.bal[kind + "Used"] = (p.bal[kind + "Used"] || 0) + u;
          const a = parseHours(cell(r, bt.map, "accrued"));
          if (a != null) p.bal[kind + "Accrued"] = (p.bal[kind + "Accrued"] || 0) + a;
        } else {
          const pto = parseHours(cell(r, bt.map, "pto")), sick = parseHours(cell(r, bt.map, "sick"));
          if (pto != null) p.bal.pto = pto;
          if (sick != null) p.bal.sick = sick;
        }
      }
    }

    // required forms
    const titleKey = t => String(t || "").trim().toLowerCase();
    const forms = (d.forms || []).map(f => {
      const subs = new Map(); const unmatched = new Set(); const times = []; let rows = 0;
      for (const s of f.subs || []) {
        if (s.at == null) continue;
        const day = Math.floor(s.at / 1440);
        const p = (s.uid != null && people.get("u" + s.uid)) || lookup((s.uid != null && users[s.uid]) || s.name);
        const nm = p ? p.name : niceName((s.uid != null && users[s.uid]) || s.name || "Someone");
        times.push({ t: s.at, name: nm });
        const k = p ? p.key : s.uid != null ? "u" + s.uid : keyOf(s.name);
        if (!p) unmatched.add(nm);
        if (!subs.has(k)) subs.set(k, new Set());
        subs.get(k).add(day); rows++;
      }
      // "Who must submit" takes names, departments, Gusto titles or jobs (e.g. "Housekeeping")
      const appliesTo = String(f.applies_to || "");
      const applies = [...new Set(appliesTo.split(/[,;\n]/).map(s => s.trim()).filter(Boolean).flatMap(s => {
        const byTitle = [...people.values()].filter(p => titleKey(p.title) === titleKey(s) || titleKey(p.dept) === titleKey(s) || p.jobs.has(titleKey(s))).map(p => p.key);
        if (byTitle.length) return byTitle;
        const p = lookup(s); return [p ? p.key : keyOf(s)];
      }))];
      const who = appliesTo.split(/[,;\n]/).map(s => s.trim().toLowerCase()).filter(Boolean);
      return { id: f.id, name: f.name || "Untitled form", frequency: f.frequency || "shift", appliesTo, applies, who, every: hoursSetting(f.every, 1), nightEvery: hoursSetting(f.night_every, hoursSetting(f.every, 1)), ...checkHours(f),
        subs, times: times.sort((a, b) => a.t - b.t), rows, unmatched: [...unmatched], hasFile: f.loaded !== false };
    });

    const list = [...people.values()];
    const seenFuzzy = new Set();
    const depts = new Set();
    list.forEach(p => { deptsOf(p).forEach(x => { if (x !== "No department") depts.add(x); }); if (p.title) depts.add(p.title); });
    schedJobs.forEach(j => { const shown = (d.shifts || []).find(s => s.job && s.job.toLowerCase() === j); if (shown) depts.add(shown.job); });
    punchJobs.forEach(j => { if (![...depts].some(x => x.toLowerCase() === j.toLowerCase())) depts.add(j); });
    const fetched = day => day >= d.fetch_start && day <= d.fetch_end;
    const hasTimesheet = !!(d.have && d.have.timesheets);
    return {
      people: list, forms, balUnmatched: [...balUnmatched], balPeople, gustoOk: !!bt,
      noBal: list.filter(p => !p.bal).map(p => p.name),
      fuzzy: fuzzy.filter(([a, b]) => { const id = a + "|" + b; if (seenFuzzy.has(id)) return false; seenFuzzy.add(id); return true; }),
      hasTimesheet, hasSchedule: list.some(p => p.shifts.length),
      // scheduled, but not one punch or time off in the dates read: maybe not on a time clock yet
      noPunches: hasTimesheet ? list.filter(p => p.shifts.length && !inTimesheets.has(p)).map(p => p.name) : [],
      // a shift can only be a no-show on a date the timesheets were read for
      covered: (p, day) => hasTimesheet && fetched(day),
      scheduled: (day, job) => shiftCount > 0 && fetched(day) && (!job || schedJobs.has(job.toLowerCase())),
      departments: [...depts].sort((a, b) => a.localeCompare(b)),
    };
  }

  // ---------------------------------------------------------------- periods
  function weekStartOf(day, ws) { return day - ((dow(day) - ws + 7) % 7); }
  function periodOf(day, kind, ws) {
    if (kind === "week") { const s = weekStartOf(day, ws); return { start: s, end: s + 6 }; }
    const d = dt(day), y = d.getUTCFullYear(), m = d.getUTCMonth() + 1, last = new Date(Date.UTC(y, m, 0)).getUTCDate();
    if (kind === "pay") return d.getUTCDate() <= 15 ? { start: ymd(y, m, 1), end: ymd(y, m, 15) } : { start: ymd(y, m, 16), end: ymd(y, m, last) };
    return { start: ymd(y, m, 1), end: ymd(y, m, last) };
  }
  // The day to show after switching period: a longer period holds the one being looked at (a past week's month);
  // a shorter one goes to today's (Month → Week is this week), as does any switch while today is in view.
  const PERIOD_SIZE = { week: 0, pay: 1, month: 2 };
  function switchAnchor(P, from, to, today) {
    if (today >= P.start && today <= P.end) return today;
    return PERIOD_SIZE[to] < PERIOD_SIZE[from] ? today : P.start;
  }
  function periodLabel(P, kind) {
    if (kind === "month") { const d = dt(P.start); return `${MONTHS_LONG[d.getUTCMonth()]} ${d.getUTCFullYear()}`; }
    return fmtRange(P.start, P.end);
  }

  // Pair each person's work punches with their scheduled shifts. A punch goes to the shift it overlaps most,
  // or to a shift starting within 3 hours of it; leftovers become unscheduled blocks.
  function blocksFor(p, lo, hi) {
    const shifts = p.shifts.filter(s => s.end > lo - 1440 && s.start < hi + 1440).map(s => ({ shift: s, punches: [] }));
    const work = p.punches.filter(x => !x.isBreak && x.in >= lo - 1440 && x.in < hi + 1440);
    const loose = [];
    for (const x of work) {
      const out = x.out ?? x.in + 1;
      let best = null, bestScore = 0;
      for (const b of shifts) {
        const ov = Math.min(out, b.shift.end) - Math.max(x.in, b.shift.start);
        const score = ov > 0 ? 1e6 + ov : (Math.abs(x.in - b.shift.start) <= 180 || Math.abs(out - b.shift.end) <= 180) ? 1e3 - Math.abs(x.in - b.shift.start) / 1e3 : 0;
        if (score > bestScore) { bestScore = score; best = b; }
      }
      if (best) best.punches.push(x); else loose.push(x);
    }
    const blocks = shifts;
    let cur = null;
    for (const x of loose) {
      const lastOut = cur && cur.punches[cur.punches.length - 1].out;
      if (cur && lastOut != null && x.in - lastOut <= 180) cur.punches.push(x);
      else { cur = { shift: null, punches: [x] }; blocks.push(cur); }
    }
    for (const b of blocks) b.anchor = b.shift ? b.shift.start : b.punches[0].in;
    return blocks.filter(b => b.anchor >= lo && b.anchor < hi).sort((a, b) => a.anchor - b.anchor);
  }

  // A block's break time: gaps between its work punches, and breaks taken with Connecteam's Break
  // button (their own rows, inside the shift: ctdata never puts them on the work row), counted
  // once where the two overlap. A break still going counts up to now.
  function breakTime(p, ps, NOW) {
    const first = ps[0].in, last = ps[ps.length - 1].out ?? NOW;
    const spans = [];
    for (let i = 1; i < ps.length; i++) { const prev = ps[i - 1].out; if (prev != null && ps[i].in > prev) spans.push([prev, ps[i].in]); }
    for (const x of p.punches) if (x.isBreak) { const a = Math.max(x.in, first), e = Math.min(x.out ?? NOW, last); if (e > a) spans.push([a, e]); }
    spans.sort((a, b) => a[0] - b[0]);
    let total = 0, upTo = -Infinity;
    for (const [a, e] of spans) { const s = Math.max(a, upTo); if (e > s) total += e - s; upTo = Math.max(upTo, e); }
    return total;
  }

  // Hourly team checks (e.g. tub temperatures): each time slot in which someone matching the form's job or title
  // was clocked in for at least 30 minutes needs one submission from anyone. A missed slot counts against
  // everyone who was on that job for 15 minutes or more of it. The form's checked hours (6a–10p unless set) use
  // one interval; the rest of the night another, or none (0), and unchecked times aren't shown at all.
  const DAY_FROM = 6 * 60, DAY_UNTIL = 22 * 60;
  const clockMinutes = v => { const m = /^(\d{1,2}):(\d{2})$/.exec(String(v || "")); return m && +m[1] < 24 && +m[2] < 60 ? +m[1] * 60 + +m[2] : null; };
  function checkHours(f) {
    const from = clockMinutes(f.day_from), until = clockMinutes(f.day_until) === 0 ? 1440 : clockMinutes(f.day_until);
    return from == null || until == null || until <= from ? { from: DAY_FROM, until: DAY_UNTIL } : { from, until };
  }
  const span = f => `${fmtClock(f.from).replace(":00", "")}–${fmtClock(f.until).replace(":00", "")}`;
  // An interval of 0 switches checks off for that part of the day; blank means the default.
  const hoursSetting = (v, d) => v === "" || v == null || isNaN(+v) ? d : +v <= 0 ? 0 : Math.max(0.5, +v);
  const freqText = f => {
    const day = (f.every === 0 ? "Not checked " : `Every ${f.every === 1 ? "hour" : fmtH(f.every)} `) + span(f);
    const night = f.nightEvery === 0 ? (f.every === 0 ? " or overnight" : ", not overnight") : f.nightEvery !== f.every ? `, every ${fmtH(f.nightEvery)} overnight` : "";
    return day + night;
  };
  function daySlots(every, nightEvery, from, until) {
    const out = []; let m = 0;
    while (m < 1440) {
      const night = m >= until || m < from;
      const iv = night ? nightEvery : every;
      const edge = m < from ? from : m < until ? until : 1440;
      const end = iv === 0 ? edge : Math.min(m + iv * 60, edge);
      out.push([m, end, iv === 0]); m = end;
    }
    return out;
  }
  function hourlyForm(f, rows, lo, hi, model, NOW) {
    const slots = daySlots(f.every, f.nightEvery, f.from, f.until);
    const slotAt = abs => { const d = Math.floor(abs / 1440), m = abs - d * 1440; const s = slots.find(x => m >= x[0] && m < x[1]); return [d * 1440 + s[0], d * 1440 + s[1], s[2]]; };
    const cov = new Map(); // slot start -> { end, total, people: Map(rec -> minutes) }
    for (const r of rows) {
      const title = `${r.p.title || ""} ${r.p.dept || ""}`.toLowerCase();
      for (const day of r.days) for (const b of day.blocks) for (const x of b.punches) {
        const text = `${x.job || ""} ${b.job || ""} ${title}`.toLowerCase();
        if (f.who.length && !f.who.some(w => text.includes(w))) continue;
        // still clocked in: count them on until their scheduled end, not indefinitely
        const open = b.shift ? Math.max(x.in, b.shift.end) : x.in + 60;
        let a = Math.max(x.in, lo), end = Math.min(x.out ?? Math.min(NOW, open), hi);
        // only the time someone is both clocked in and scheduled counts, so arriving 10 minutes early
        // or staying late doesn't make them answer for the neighbouring hour
        if (b.shift) { a = Math.max(a, b.shift.start); end = Math.min(end, b.shift.end); }
        else if (model.scheduled(Math.floor(x.in / 1440), "")) continue;
        while (a < end) {
          const [ss, se, off] = slotAt(a); const e = Math.min(end, se);
          if (off) { a = e; continue; }
          let c = cov.get(ss); if (!c) { c = { end: se, total: 0, people: new Map() }; cov.set(ss, c); }
          c.total += e - a; c.people.set(r, (c.people.get(r) || 0) + e - a); a = e;
        }
      }
    }
    let expected = 0, done = 0; const missBy = new Map(); const slotState = new Map();
    for (const [ss, c] of [...cov.entries()].sort((a, b) => a[0] - b[0])) {
      const by = f.times.filter(x => x.t >= ss && x.t < c.end);
      const onAll = [...c.people.entries()].sort((a, b) => b[1] - a[1]).map(([r]) => r.p.name);
      if (c.end > NOW) { slotState.set(ss, { st: "open", on: onAll, by }); continue; }
      if (Math.min(c.total, c.end - ss) < Math.min(30, (c.end - ss) / 2)) { slotState.set(ss, { st: "light", on: onAll, by }); continue; }
      expected++;
      const on = [...c.people.entries()].filter(([, m]) => m >= 15).map(([r]) => r);
      on.forEach(r => { r.formsExpected++; });
      if (by.length) { done++; slotState.set(ss, { st: "done", on: onAll, by }); continue; }
      slotState.set(ss, { st: "missed", on: onAll, by, blamed: on.map(r => r.p.name) });
      for (const r of on) {
        r.formsMissed++;
        const d = Math.floor(ss / 1440);
        r.slotMisses = r.slotMisses || new Map();
        const k = `${f.name}|${d}`; r.slotMisses.set(k, (r.slotMisses.get(k) || []).concat(fmtClock(ss)));
        missBy.set(r, (missBy.get(r) || 0) + 1);
      }
    }
    for (const [r, n] of missBy) r.formMisses.push(`${f.name} ×${n}`);
    const misses = [...missBy.entries()].sort((a, b) => b[1] - a[1]).map(([r, n]) => ({ name: r.p.name, n }));
    // one row per day, one cell per time slot
    const grid = [];
    for (let d = Math.floor(lo / 1440); d < Math.floor(hi / 1440); d++) {
      grid.push({ d, cells: slots.map(([s0, s1, off]) => { const ss = d * 1440 + s0; const x = slotState.get(ss) || { st: off ? "off" : (d * 1440 + s1) > NOW ? "later" : "none", on: [], by: f.times.filter(t => t.t >= ss && t.t < d * 1440 + s1) }; return { ss, se: d * 1440 + s1, ...x }; }) });
    }
    // times that aren't checked (an interval of 0) are left out of the grid
    const shown = slots.map((x, i) => x[2] ? -1 : i).filter(i => i >= 0);
    grid.forEach(g => { g.cells = shown.map(i => g.cells[i]); });
    return { ...f, expected, done, missed: expected - done, misses, grid, slots: shown.map(i => slots[i]) };
  }

  // The report for one period. opts: { R: rules, dept: '' for all, now: abs minutes, today: day }
  function computePeriod(model, P, opts) {
    const R = opts.R, NOW = opts.now, TODAY = opts.today; const n = P.end - P.start + 1;
    const days = Array.from({ length: n }, (_, i) => P.start + i);
    const lo = P.start * 1440, hi = (P.end + 1) * 1440;
    const skip = new Set((R.earlySkip || []).map(s => s.toLowerCase()));
    const rows = [];
    for (const p of model.people) {
      if (opts.dept && !deptsOf(p).includes(opts.dept)) continue;
      const blocks = blocksFor(p, lo, hi);
      const offDays = days.filter(d => p.off.has(d));
      if (!blocks.length && !offDays.length) continue;
      const rec = { p, days: days.map(d => ({ d, blocks: [], off: p.off.get(d) || null })), late: [], early: [], lunch: [], missed: [], noOut: 0, worked: new Set(), offHrs: 0, workedMin: 0, formsExpected: 0, formsMissed: 0, formMisses: [], unchecked: 0 };
      for (const d of offDays) rec.offHrs += p.off.get(d).hrs || 0;
      const titleSkipped = [p.title, p.dept, ...(p.depts || [])].some(t => t && skip.has(t.toLowerCase()));
      for (const b of blocks) {
        const d = Math.floor(b.anchor / 1440); const day = rec.days[d - P.start];
        const s = b.shift; b.flags = [];
        if (b.punches.length) {
          const ps = b.punches.slice().sort((a, c) => a.in - c.in);
          rec.worked.add(d);
          b.firstIn = ps[0].in;
          b.lastOut = ps[ps.length - 1].out;
          b.brk = ps.reduce((a, x) => a + (x.brk || 0), 0) + breakTime(p, ps, NOW);
          b.segments = ps.length;
          const punchJobs = [...new Set(ps.map(x => x.job).filter(Boolean))];
          b.job = s && s.job ? s.job : punchJobs.join(" · ");
          if (b.lastOut != null) rec.workedMin += b.lastOut - b.firstIn - b.brk;
          if (s) {
            const late = b.firstIn - s.start;
            if (late > R.lateGrace) { rec.late.push(late); b.flags.push(["bad", `Late ${fmtDur(late)}`]); }
            const jobSkipped = s.job.split(" · ").some(j => skip.has(j.toLowerCase()));
            if (!s.noEnd && b.lastOut != null && !titleSkipped && !jobSkipped) {
              const early = s.end - b.lastOut;
              if (early > R.earlyGrace) { rec.early.push(early); b.flags.push(["warn", `Left ${fmtDur(early)} early`]); }
            }
          } else if (model.scheduled(d, punchJobs.length === 1 ? punchJobs[0] : "")) b.flags.push(["info", "Not on schedule"]);
          if (b.lastOut == null) {
            if (NOW - b.firstIn < 16 * 60) b.flags.push(["info", "On the clock"]);
            else { rec.noOut++; b.flags.push(["warn", "No clock-out"]); }
          } else if (b.lastOut - b.firstIn >= R.lunchAfter * 60 && b.brk < R.lunchMin) {
            rec.lunch.push(b.brk);
            b.flags.push(["bad", b.brk > 0 ? `Short lunch ${fmtDur(b.brk)}` : "No lunch"]);
          }
        } else if (s) {
          b.job = s.job;
          if (day.off) b.flags.push(["info", `${day.off.type} ${fmtH(day.off.hrs)}`]);
          else if (s.end > NOW) b.upcoming = true;
          else if (!model.covered(p, d)) { b.unchecked = true; rec.unchecked++; }
          else {
            const hrs = (s.end - s.start) / 60;
            rec.missed.push({ d, hrs }); b.flags.push(["bad", `No-show · ${fmtH(hrs)}`]);
            if (p.blips.has(d)) b.flags.push(["warn", "Clocked in and out at once"]);
          }
        }
        day.blocks.push(b);
      }
      rows.push(rec);
    }
    const byKey = new Map(rows.map(r => [r.p.key, r]));

    const forms = model.forms.map(f => {
      if (f.frequency === "hour") return hourlyForm(f, rows, lo, hi, model, NOW);
      let expected = 0, done = 0; const misses = []; const people = [];
      const pool = f.applies.length ? f.applies.map(k => byKey.get(k)).filter(Boolean) : rows.filter(r => r.worked.size);
      for (const r of pool) {
        const sub = f.subs.get(r.p.key) || new Set();
        const worked = [...r.worked];
        people.push({ name: r.p.name, cells: days.map(d => ({ d, worked: r.worked.has(d), sub: sub.has(d) || (r.worked.has(d) && sub.has(d + 1)) })) });
        let exp = 0, got = 0;
        if (f.frequency === "week") {
          const wks = [...new Set(worked.map(d => weekStartOf(d, R.weekStart)))];
          exp = wks.length; got = wks.filter(w => Array.from({ length: 7 }, (_, i) => w + i).some(d => sub.has(d))).length;
        } else {
          exp = worked.length; got = worked.filter(d => sub.has(d) || sub.has(d + 1)).length;
          // the days it wasn't handed in, for the person's day-by-day view
          for (const d of worked) if (!sub.has(d) && !sub.has(d + 1)) {
            r.dayFormMisses = r.dayFormMisses || new Map();
            r.dayFormMisses.set(d, (r.dayFormMisses.get(d) || []).concat(f.name));
          }
        }
        expected += exp; done += got;
        if (exp - got > 0) { misses.push({ name: r.p.name, n: exp - got }); r.formsMissed += exp - got; r.formMisses.push(`${f.name} ×${exp - got}`); }
        r.formsExpected += exp;
      }
      return { ...f, expected, done, missed: expected - done, misses, people };
    });

    for (const r of rows) {
      r.lateMin = r.late.reduce((a, b) => a + b, 0);
      r.earlyMin = r.early.reduce((a, b) => a + b, 0);
      r.missedHrs = r.missed.reduce((a, b) => a + b.hrs, 0);
      r.incidents = r.late.length + r.early.length + r.lunch.length + r.missed.length + r.formsMissed;
      const b = r.p.bal;
      const sick = b && b.sick != null ? b.sick : null, pto = b && b.pto != null ? b.pto : null;
      r.sick = sick; r.pto = pto;
      // A low balance is only a warning if the person has had enough hours to reach the threshold:
      // new hires who haven't accrued that much yet aren't flagged. Below zero is always flagged.
      const lowCheck = (v, accrued, thr) => {
        if (v == null) return { low: false, early: false };
        if (v < 0) return { low: true, early: false };
        if (!(thr > 0) || v >= thr) return { low: false, early: false };
        if (accrued != null && accrued < thr) return { low: false, early: true };
        return { low: true, early: false };
      };
      const sl = lowCheck(sick, b && b.sickAccrued, R.lowSick), pl = lowCheck(pto, b && b.ptoAccrued, R.lowPto);
      r.sickLow = sl.low; r.ptoLow = pl.low; r.sickEarly = sl.early; r.ptoEarly = pl.early;
      r.low = r.sickLow || r.ptoLow;
      r.shortfall = 0;
      if (!r.missedHrs) r.cover = b ? { cls: "", main: "No missed shifts", sub: "" } : { cls: "warn", main: "No Gusto match", sub: "Add a name match in Rules" };
      else if (!b) r.cover = { cls: "warn", main: `${fmtH(r.missedHrs)} missed`, sub: "No Gusto balance found" };
      else {
        const s = Math.max(sick || 0, 0), t = s + Math.max(pto || 0, 0);
        if (r.missedHrs <= s) r.cover = { cls: "good", main: "Sick covers it", sub: `${fmtH(r.missedHrs)} of ${fmtH(s)} sick` };
        else if (r.missedHrs <= t) r.cover = { cls: "warn", main: "Needs PTO too", sub: `${fmtH(s)} sick + ${fmtH(r.missedHrs - s)} PTO` };
        else { r.shortfall = r.missedHrs - t; r.cover = { cls: "bad", main: `Short ${fmtH(r.shortfall)}`, sub: `${fmtH(r.missedHrs)} missed, ${fmtH(t)} available` }; }
      }
      const weeks = Math.max(1, n / 7);
      r.status = (r.shortfall > 0 || r.incidents >= R.actionAt * weeks) ? "action" : (r.incidents > 0 || r.low) ? "watch" : "clear";
    }
    const rank = { action: 0, watch: 1, clear: 2 };
    rows.sort((a, b) => rank[a.status] - rank[b.status] || b.incidents - a.incidents || a.p.name.localeCompare(b.p.name));

    const sum = f => rows.reduce((a, r) => a + f(r), 0);
    const totals = {
      late: sum(r => r.late.length), lateMin: sum(r => r.lateMin), latePeople: rows.filter(r => r.late.length).length,
      early: sum(r => r.early.length), earlyMin: sum(r => r.earlyMin),
      lunch: sum(r => r.lunch.length),
      missed: sum(r => r.missed.length), missedHrs: sum(r => r.missedHrs), unchecked: sum(r => r.unchecked),
      formsMissed: forms.reduce((a, f) => a + f.missed, 0), formsExpected: forms.reduce((a, f) => a + f.expected, 0),
      action: rows.filter(r => r.status === "action").length, watch: rows.filter(r => r.status === "watch").length,
    };
    return { P, days, rows, forms, totals, inProgress: P.end >= TODAY };
  }

  // ---------------------------------------------------------------- summary and CSV
  function summaryText(W, period, dept) {
    if (!W) return "";
    const T = W.totals;
    const lines = [`Attendance — ${period === "week" ? "week of " : ""}${periodLabel(W.P, period)}${dept ? ` — ${dept}` : ""}${W.inProgress ? " (in progress)" : ""}`, "",
      `Late arrivals: ${T.late}${T.late ? ` (${fmtDur(T.lateMin)})` : ""} · Early clock-outs: ${T.early} · Missed lunches: ${T.lunch} · Missed shifts: ${T.missed}${T.missed ? ` (${fmtH(T.missedHrs)})` : ""} · Forms missed: ${T.formsMissed} of ${T.formsExpected}`];
    const line = r => {
      const bits = [];
      if (r.late.length) bits.push(`late ${r.late.length}× (${fmtDur(r.lateMin)})`);
      if (r.early.length) bits.push(`left early ${r.early.length}×`);
      if (r.lunch.length) bits.push(`missed lunch ${r.lunch.length}×`);
      if (r.missed.length) bits.push(`missed ${plural(r.missed.length, "shift")} (${fmtH(r.missedHrs)})`);
      if (r.formsMissed) bits.push(`${r.formsMissed} forms missed`);
      const bal = (r.sick != null || r.pto != null) ? ` — sick ${r.sick != null ? fmtH(r.sick) : "?"}, PTO ${r.pto != null ? fmtH(r.pto) : "?"}` : "";
      const cov = r.missedHrs ? ` → ${r.cover.main}` : "";
      return `• ${r.p.name}: ${bits.join(", ") || "low leave balance"}${bal}${cov}`;
    };
    const act = W.rows.filter(r => r.status === "action"), watch = W.rows.filter(r => r.status === "watch");
    if (act.length) lines.push("", "Needs action", ...act.map(line));
    if (watch.length) lines.push("", "Watch", ...watch.map(line));
    const clear = W.rows.filter(r => r.status === "clear");
    if (clear.length) lines.push("", `Clear: ${clear.map(r => r.p.name).join(", ")}`);
    return lines.join("\n");
  }
  function csvText(W) {
    // A leading = + - @ would make a spreadsheet run the cell as a formula: quote it as text.
    const q = v => { v = String(v ?? ""); if (/^[=+\-@]/.test(v) && !/^-?\d/.test(v)) v = "'" + v; return /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v; };
    const head = ["Period start", "Period end", "Employee", "Department", "Status", "Hours worked", "Late count", "Late minutes", "Early clock-outs", "Early minutes", "Missed lunches", "Missed shifts", "Missed shift hours", "Forms expected", "Forms missed", "Sick balance hrs", "PTO balance hrs", "Shortfall hrs", "Coverage", "Detail"];
    const rows = W.rows.map(r => [fmtDate(W.P.start), fmtDate(W.P.end), r.p.name, deptOf(r.p), r.status, fmtHrs(r.workedMin / 60), r.late.length, Math.round(r.lateMin), r.early.length, Math.round(r.earlyMin), r.lunch.length, r.missed.length, fmtHrs(r.missedHrs), r.formsExpected, r.formsMissed, r.sick ?? "", r.pto ?? "", fmtHrs(r.shortfall), r.cover.main,
      r.days.flatMap(d => d.blocks.filter(b => b.flags.some(f => f[0] !== "info")).map(b => `${fmtDay(d.d)}: ${b.flags.filter(f => f[0] !== "info").map(f => f[1]).join("; ")}`)).join(" | ")]);
    return [head, ...rows].map(r => r.map(q).join(",")).join("\n");
  }
  function fileName(W, period, dept, ext) {
    const w = dt(W.P.start);
    return `attendance-${period}-${w.getUTCFullYear()}-${String(w.getUTCMonth() + 1).padStart(2, "0")}-${String(w.getUTCDate()).padStart(2, "0")}${dept ? "-" + dept.toLowerCase().replace(/[^a-z0-9]+/g, "-") : ""}.${ext}`;
  }

  // ================================================================ the page
  // Everything below draws the report with the staff app's h() builder (text is always set as
  // text, never as HTML) and talks to the server only through `api`.
  let h = null, ctx = null, model = null, modelFor = null, current = null, firstLoad = true;
  const ui = { tab: "report", period: "week", dept: "", open: new Set(), openForms: new Set(), loading: false, draft: null, dirty: false, printing: null, formList: false };
  const PREFS = "staff-report.attendance";
  try { const p = JSON.parse(localStorage.getItem(PREFS) || "{}"); ui.period = ["week", "pay", "month"].includes(p.period) ? p.period : "week"; ui.dept = typeof p.dept === "string" ? p.dept : ""; } catch { /* storage blocked */ }
  const remember = () => { try { localStorage.setItem(PREFS, JSON.stringify({ period: ui.period, dept: ui.dept })); } catch { /* storage blocked */ } };

  const rules = () => ({ ...DEFAULT_RULES, ...((ctx.data.settings || {}).rules || {}) });
  const canManage = () => !!ctx.api.manage;
  const toast = (text, isError) => { if (ctx.api.toast) ctx.api.toast(text, isError); };

  // The app calls render() again with new data after refresh(), an upload or saved settings.
  function render(el, data, api) {
    h = api.h; ctx = { el, data, api };
    ui.loading = false;
    if (data && data.period) ui.period = data.period;
    draw();
    // the period picked last time on this device, if it isn't what came first
    if (firstLoad) {
      firstLoad = false;
      try { const p = JSON.parse(localStorage.getItem(PREFS) || "{}"); if (p.period && p.period !== data.period && ["week", "pay", "month"].includes(p.period)) { ui.period = p.period; load({ at: iso(data.today) }); } } catch { /* storage blocked */ }
    }
  }

  // Ask for the report again: the app runs data() on the server and calls render() with it.
  function load(extra) {
    const d = ctx.data;
    const params = { period: ui.period, at: iso(d.start_day), ...(ui.formList ? { form_list: 1 } : {}), ...(extra || {}) };
    ui.loading = true; draw();
    ctx.api.refresh(params);
  }

  function draw() {
    if (!ctx || !h) return;
    const d = ctx.data;
    if (modelFor !== d) { model = buildModel(d, rules()); modelFor = d; }
    const keep = [...ctx.el.querySelectorAll("[data-keep]")].map(x => [x.dataset.keep, x.scrollLeft]);
    const body = ui.tab === "rules" ? rulesView() : ui.tab === "gusto" ? gustoView() : reportView();
    ctx.el.replaceChildren(h("div", { class: "att" + (ui.printing ? " att-printing-" + ui.printing : "") + (ui.loading ? " att-loading" : "") },
      header(), banner(), body));
    for (const [k, x] of keep) { const el = ctx.el.querySelector(`[data-keep="${k}"]`); if (el) el.scrollLeft = x; }
  }

  function header() {
    const tab = (key, label, dot) => h("button", { class: "att-tab" + (ui.tab === key ? " active" : ""), type: "button", "aria-pressed": String(ui.tab === key),
      onclick: () => {
        // Unsaved rules are kept (on every tab, until Save or Undo); say so when leaving them.
        if (ui.tab === "rules" && key !== "rules" && ui.dirty) toast("Your rule changes aren’t saved yet. They’re kept on the Rules tab until you Save or Undo.");
        ui.tab = key; if (key === "rules" && canManage() && !ui.formList) { ui.formList = true; load(); } else draw();
      } },
      label, dot ? h("span", { class: "att-dot", "aria-label": typeof dot === "string" ? dot : "needs attention", title: typeof dot === "string" ? dot : null }) : null);
    // The app shows the report's name above it, so only the tabs here.
    return h("div", { class: "att-head" },
      h("div", { class: "att-tabs att-noprint", role: "group", "aria-label": "Sections" },
        tab("report", "Report"), tab("gusto", "Leave balances", !ctx.data.gusto || !model.gustoOk),
        tab("rules", "Rules", ui.dirty && "changes not saved")));
  }

  function banner() {
    const errs = ctx.data.errors || [];
    if (!errs.length) return null;
    return h("div", { class: "att-banner att-noprint" }, h("b", null, "Some Connecteam data didn't load."),
      h("ul", null, errs.map(e => h("li", null, e))), h("span", null, "The rest of the report still shows. Reports try again when you reopen or change the period."));
  }

  // ---------------------------------------------------------------- report tab
  function stepTo(day) { ui.open.clear(); load({ at: iso(day) }); }

  function reportView() {
    const d = ctx.data, R = rules();
    const P = { start: d.start_day, end: d.end_day };
    const depts = [...new Set(model.people.flatMap(deptsOf))].sort();
    if (ui.dept && !depts.includes(ui.dept)) ui.dept = "";
    const W = computePeriod(model, P, { R, dept: ui.dept, now: d.now, today: d.today }); current = W;
    const T = W.totals;

    const sub = [];
    if (ui.period === "week") sub.push(`${DOW_LONG[R.weekStart]} to ${DOW_LONG[(R.weekStart + 6) % 7]}`);
    if (ui.period === "pay") sub.push("Semi-monthly pay period");
    if (ui.period === "month" || ui.period === "pay") sub.push(fmtRange(P.start, P.end));
    if (W.inProgress) sub.push("in progress, so shifts that haven’t ended aren’t counted");
    if (T.unchecked) sub.push(`${plural(T.unchecked, "shift")} without timesheets not checked`);
    if (d.own_only) sub.push("people in your department only");

    const seg = (key, label) => h("button", { type: "button", class: "att-seg-btn" + (ui.period === key ? " active" : ""), "aria-pressed": String(ui.period === key),
      onclick: () => {
        if (ui.period === key) return;
        const anchor = switchAnchor(P, ui.period, key, d.today);
        ui.period = key; remember(); stepTo(anchor);
      } }, label);
    const prevOk = P.start > d.today - 730, nextOk = P.end < d.today;
    const bar = h("div", { class: "att-weekbar att-noprint" },
      h("div", { class: "att-seg", role: "group", "aria-label": "Report period" }, seg("week", "Week"), seg("pay", "Pay period"), seg("month", "Month")),
      h("div", { class: "att-stepper" },
        h("button", { type: "button", class: "att-round", "aria-label": "Previous period", disabled: !prevOk || ui.loading, onclick: () => stepTo(P.start - 1) }, "‹"),
        h("div", { class: "att-week-label" }, h("strong", null, periodLabel(P, ui.period)), h("span", null, ui.loading ? "Loading…" : sub.join(" · "))),
        h("button", { type: "button", class: "att-round", "aria-label": "Next period", disabled: !nextOk || ui.loading, onclick: () => stepTo(P.end + 1) }, "›")));

    const deptSel = depts.length < 2 ? null : h("select", { class: "att-btn att-select", "aria-label": "Department", onchange: e => { ui.dept = e.target.value; ui.open.clear(); remember(); draw(); } },
      h("option", { value: "" }, "All departments"), depts.map(x => h("option", { value: x, selected: x === ui.dept }, x)));
    const menu = h("details", { class: "att-menu" },
      h("summary", { class: "att-btn primary" }, "Print or PDF ▾"),
      h("div", { class: "att-menu-pop", role: "menu" },
        menuItem("all", "Everyone, with daily detail", "Every row opened, one section per person"),
        menuItem("open", "Only the rows I’ve opened", "Summary plus the people you expanded"),
        menuItem("summary", "Summary only", "Totals, staff table and forms")));
    const actions = h("div", { class: "att-actions att-noprint" }, deptSel,
      h("button", { type: "button", class: "att-btn", onclick: () => copyText(summaryText(current, ui.period, ui.dept)) }, "Copy summary"),
      h("button", { type: "button", class: "att-btn", onclick: downloadCSV }, "Download CSV"), menu);

    const kpi = (label, val, s, cls) => h("div", { class: "att-kpi " + (cls || "") }, h("span", { class: "att-k-label" }, label), h("span", { class: "att-k-val" }, String(val)), h("span", { class: "att-k-sub" }, s));
    const kpis = h("div", { class: "att-kpis" },
      kpi("Late arrivals", T.late, T.late ? `${fmtDur(T.lateMin)} total · ${plural(T.latePeople, "person", "people")}` : `over ${R.lateGrace} min grace`, T.late ? "warm" : ""),
      kpi("Early clock-outs", T.early, T.early ? `${fmtDur(T.earlyMin)} short of schedule` : `over ${R.earlyGrace} min early`, T.early ? "warm" : ""),
      kpi("Missed lunches", T.lunch, `shifts of ${fmtH(R.lunchAfter)} or more, lunch under ${fmtDur(R.lunchMin)}`, T.lunch ? "warm" : ""),
      kpi("Missed shifts", T.missed, T.missed ? `${fmtH(T.missedHrs)} not covered by leave` : (model.hasSchedule ? "scheduled, no punch" : "needs Connecteam schedules"), T.missed ? "hot" : ""),
      kpi("Forms missed", T.formsMissed, T.formsExpected ? `of ${T.formsExpected} expected` : "no required forms set", T.formsMissed ? "warm" : ""),
      kpi("Need action", T.action, `${T.watch} more to watch`, T.action ? "hot" : ""));

    const notes = [
      model.hasTimesheet ? "Timesheets from every Connecteam time clock" : "Timesheets didn't load",
      d.have && d.have.shifts ? (model.hasSchedule ? "Schedules from Connecteam (drafts skipped)" : "No Connecteam schedules in this period, so no late, early or no-show checks") : "Schedules didn't load",
      d.gusto ? `Gusto balances uploaded ${when(d.gusto.uploaded_at)}${d.gusto.by ? ` by ${d.gusto.by}` : ""}` : "Gusto balances not uploaded",
      "Connecteam data is kept for 10 minutes",
    ];
    const noPunch = model.noPunches.filter(n => W.rows.some(r => r.p.name === n && r.missed.length));
    const noPunchNote = noPunch.length ? h("p", { class: "att-warn-text att-noprint" }, `On the schedule but with no punches at all: ${noPunch.join(", ")}. If they aren’t on a Connecteam time clock yet, their shifts count as no-shows.`) : null;
    return h("section", { class: "att-report" }, bar, actions, printHead(W), kpis, noPunchNote,
      h("div", { class: "att-panel" }, h("div", { class: "att-panel-head" }, h("h2", null, ui.period === "week" ? "Staff this week" : "Staff this period"), h("p", { class: "att-noprint" }, "Tap a name to see each day's punches.")), roster(W, P)),
      h("div", { class: "att-panel att-forms-panel" }, h("div", { class: "att-panel-head" }, h("h2", null, "Required forms"), h("p", { class: "att-noprint" }, "Tap a form to see every day, hour by hour.")), formsTable(W)),
      h("div", { class: "att-foot-note" }, notes.map(n => h("span", null, n))),
      printFoot(R));
  }

  function menuItem(mode, title, small) {
    return h("button", { type: "button", class: "att-menu-item", role: "menuitem", onclick: e => { e.target.closest("details").open = false; printReport(mode); } }, title, h("small", null, small));
  }

  function roster(W, P) {
    const R = rules();
    if (W.rows.length && W.totals.unchecked && W.rows.every(r => !r.worked.size))
      return h("div", { class: "att-empty" }, `Schedules cover these dates but the timesheets didn't load, so attendance can’t be checked for ${fmtRange(P.start, P.end)} yet. Try again in a few minutes.`);
    if (!W.rows.length)
      return h("div", { class: "att-empty" }, `No shifts or punches fall in this period${ui.dept ? ` for ${ui.dept}` : ""}. Use the arrows to move to another period.`);
    const nil = text => h("span", { class: "att-nil" }, text || "—");
    const c = (n, s, cls) => n ? h("div", { class: "att-cell " + cls }, h("b", null, `${n}×`), h("span", { class: "att-sub" }, s)) : nil();
    const bal = (v, low) => v == null ? nil() : h("span", { class: "att-bal " + (!low ? "" : v <= 0 ? "zero" : "low") }, fmtH(v));
    // a cell with nothing in it ("—") is left off the phone card
    const td = (label, cls, kid) => h("td", { class: [cls, kid.classList && kid.classList.contains("att-nil") && kid.textContent === "—" ? "att-nil-cell" : null].filter(Boolean).join(" ") || null, "data-label": label }, kid);
    const detailFor = ui.printing === "all" ? () => true : ui.printing === "summary" ? () => false : r => ui.open.has(r.p.key);
    const body = [];
    for (const r of W.rows) {
      const open = detailFor(r);
      const toggle = () => { ui.open.has(r.p.key) ? ui.open.delete(r.p.key) : ui.open.add(r.p.key); draw(); };
      body.push(h("tr", { class: "att-row" + (open ? " open" : ""), tabindex: "0", "aria-expanded": String(open), onclick: toggle,
        onkeydown: e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } } },
        h("td", { class: "att-who-cell" }, h("span", { class: "att-who" }, h("span", { class: "att-caret", "aria-hidden": "true" }, "▸"),
          h("span", { class: "att-cell" }, r.p.name, h("span", { class: "att-sub" }, deptOf(r.p))))),
        td("Status", "att-status-cell", h("span", { class: "att-pill " + r.status }, r.status === "action" ? "Action" : r.status === "watch" ? "Watch" : "Clear")),
        td("Worked", "r num", r.workedMin ? fmtDur(r.workedMin) : nil()),
        td("Late", null, c(r.late.length, fmtDur(r.lateMin), "warn")),
        td("Left early", null, c(r.early.length, fmtDur(r.earlyMin), "warn")),
        td("Missed lunch", null, c(r.lunch.length, `of ${plural(r.worked.size, "shift")}`, "warn")),
        td("Missed shifts", null, c(r.missed.length, fmtH(r.missedHrs), "bad")),
        td("Forms missed", null, r.formsExpected ? (r.formsMissed ? h("div", { class: "att-cell warn" }, h("b", null, String(r.formsMissed)), h("span", { class: "att-sub" }, `of ${r.formsExpected}`)) : nil(`0 of ${r.formsExpected}`)) : nil()),
        td("Leave balance", "r", h("div", { class: "att-bals" }, h("span", null, h("span", { class: "k" }, "Sick"), bal(r.sick, r.sickLow)), h("span", null, h("span", { class: "k" }, "PTO"), bal(r.pto, r.ptoLow)))),
        td("Missed time vs. leave", null, h("div", { class: "att-cover " + r.cover.cls }, h("b", null, r.cover.main), r.cover.sub ? h("span", { class: "att-sub" }, r.cover.sub) : null))));
      if (open) body.push(h("tr", { class: "att-detail" }, h("td", { colspan: "10" }, dayGrid(r, P, R))));
    }
    return h("div", { class: "att-tbl-wrap" }, h("table", { class: "att-roster" },
      h("thead", null, h("tr", null, ["Employee", "Status", "Worked", "Late", "Left early", "Missed lunch", "Missed shifts", "Forms missed", "Leave balance", "Missed time vs. leave"].map((t, i) => h("th", { class: i === 2 || i === 8 ? "r" : null }, t)))),
      h("tbody", null, body)));
  }

  function dayGrid(r, P, R) {
    const lead = (dow(P.start) - R.weekStart + 7) % 7;
    const cells = Array.from({ length: lead }, () => h("div", { class: "att-day blank", "aria-hidden": "true" }));
    for (const day of r.days) {
      const empty = !day.blocks.length && !day.off;
      const kids = [h("span", { class: "dh" }, `${DOW[dow(day.d)]} ${r.days.length > 7 ? fmtShort(day.d) : dt(day.d).getUTCDate()}`)];
      for (const b of day.blocks) {
        const blk = [];
        if (b.shift) blk.push(h("span", { class: "ds" }, `Sched ${fmtClock(b.shift.start)}${b.shift.noEnd ? "" : "–" + fmtClock(b.shift.end)}`));
        if (b.job) blk.push(h("span", { class: "dj" }, b.job));
        if (b.firstIn != null) {
          blk.push(h("span", { class: "dp" }, `${fmtClock(b.firstIn)} → ${b.lastOut != null ? fmtClock(b.lastOut) : "?"}`));
          blk.push(h("span", { class: "db" }, `${b.brk ? `Break ${fmtDur(b.brk)}` : "No break"}${b.segments > 1 ? ` · ${b.segments} punches` : ""}`));
        } else if (b.upcoming) blk.push(h("span", { class: "db" }, "Upcoming"));
        else if (b.unchecked) blk.push(h("span", { class: "db" }, "No timesheet for this date"));
        if (b.flags.length) blk.push(h("span", { class: "att-chips" }, b.flags.map(([c, t]) => h("span", { class: "att-chip " + c }, t))));
        kids.push(h("div", { class: "blk" }, blk));
      }
      if (day.off && !day.blocks.some(b => b.shift && !b.punches.length)) kids.push(h("span", { class: "att-chips" }, h("span", { class: "att-chip info" }, `${day.off.type} ${fmtH(day.off.hrs)}`)));
      if (r.slotMisses) for (const [k, hrs] of r.slotMisses) {
        const [fname, dd] = k.split("|");
        if (+dd === day.d) kids.push(h("span", { class: "att-chips" }, h("span", { class: "att-chip warn", title: `${fname} not done in the slot starting ${hrs.join(", ")}` }, `${fname}: ${hrs.length} missed`)), h("span", { class: "db" }, hrs.join(", ")));
      }
      const notIn = r.dayFormMisses && r.dayFormMisses.get(day.d);
      if (notIn) kids.push(h("span", { class: "att-chips" }, notIn.map(n => h("span", { class: "att-chip warn", title: `${n}: no submission for this shift` }, `${n} not done`))));
      if (empty) kids.push(h("span", { class: "ds" }, "Off"));
      cells.push(h("div", { class: "att-day" + (empty ? " off" : "") }, kids));
    }
    const foot = [];
    if (model.noPunches.includes(r.p.name)) foot.push("No punches at all in these dates. If they aren’t on a Connecteam time clock yet, their shifts show as no-shows.");
    if (r.formMisses.length) foot.push(`Forms missed: ${r.formMisses.join(", ")}`);
    if (r.offHrs) foot.push(`Leave recorded: ${fmtH(r.offHrs)}`);
    if (r.noOut) foot.push(`${plural(r.noOut, "shift")} without a clock-out`);
    if (r.sick != null || r.pto != null) foot.push(`Gusto balance: ${r.sick != null ? fmtH(r.sick) : "—"} sick, ${r.pto != null ? fmtH(r.pto) : "—"} PTO`);
    const b = r.p.bal;
    if (b && (b.sickUsed != null || b.ptoUsed != null)) foot.push(`Used this year: ${b.sickUsed != null ? fmtH(b.sickUsed) : "—"} sick, ${b.ptoUsed != null ? fmtH(b.ptoUsed) : "—"} PTO`);
    if (b && (b.sickAccrued != null || b.ptoAccrued != null)) foot.push(`Accrued this year: ${b.sickAccrued != null ? fmtH(b.sickAccrued) : "—"} sick, ${b.ptoAccrued != null ? fmtH(b.ptoAccrued) : "—"} PTO`);
    const early = [r.sickEarly ? "sick" : null, r.ptoEarly ? "PTO" : null].filter(Boolean);
    if (early.length) foot.push(`Low ${early.join(" and ")} not flagged: not enough accrued yet to reach the threshold`);
    return h("div", null, h("div", { class: "att-days" + (r.days.length > 7 ? " long" : "") }, cells), foot.length ? h("div", { class: "att-detail-foot" }, foot.map(f => h("span", null, f))) : null);
  }

  function formsTable(W) {
    if (!W.forms.length) return h("div", { class: "att-empty" }, canManage() ? "No required forms yet. Add Connecteam forms on the Rules tab and say who must fill them in." : "No required forms yet. Someone who manages this report can add them.");
    const body = [];
    for (const f of W.forms) {
      const pct = f.expected ? f.done / f.expected : 1;
      const who = f.misses.length ? f.misses.map(m => `${m.name}${m.n > 1 ? ` ×${m.n}` : ""}`).join(", ") : (f.expected ? "Nobody" : "");
      const fo = ui.printing ? ui.printing !== "summary" && f.frequency === "hour" : ui.openForms.has(f.id);
      const toggle = () => { ui.openForms.has(f.id) ? ui.openForms.delete(f.id) : ui.openForms.add(f.id); draw(); };
      const bar = h("i"); bar.style.width = `${Math.round(pct * 100)}%`;
      body.push(h("tr", { class: "att-row" + (fo ? " open" : ""), tabindex: "0", "aria-expanded": String(fo), onclick: toggle, onkeydown: e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } } },
        h("td", { class: "att-who-cell" }, h("span", { class: "att-who" }, h("span", { class: "att-caret", "aria-hidden": "true" }, "▸"), h("b", null, f.name)), f.hasFile ? null : h("div", { class: "att-who-missed" }, "Submissions didn't load")),
        h("td", { "data-label": "Required" }, f.frequency === "hour" ? freqText(f) : f.frequency === "week" ? "Once a week" : "Every shift",
          h("div", { class: "att-who-missed" }, f.frequency === "hour" ? `whoever is on ${f.who.length ? f.appliesTo : "shift"}` : f.applies.length ? plural(f.applies.length, "person", "people") : "Everyone who worked")),
        h("td", { class: "r num", "data-label": "Expected" }, String(f.expected)),
        h("td", { class: "r num", "data-label": "Submitted" }, String(f.done)),
        h("td", { class: "r num", "data-label": "Missed" }, f.missed ? h("b", { class: "att-bad-text" }, String(f.missed)) : "0"),
        h("td", { "data-label": "Completion" }, f.expected ? h("div", { class: "att-bar-row" }, h("div", { class: "att-bar " + (pct < 0.7 ? "poor" : pct < 0.9 ? "low" : "") }, bar), h("span", { class: "num" }, `${Math.round(pct * 100)}%`)) : h("span", { class: "att-nil" }, "—")),
        h("td", { class: "att-who-missed", "data-label": "Who missed" }, who)));
      if (fo) body.push(h("tr", { class: "att-detail" }, h("td", { colspan: "7" }, formDetail(f))));
    }
    return h("div", { class: "att-tbl-wrap" }, h("table", { class: "att-forms" },
      h("thead", null, h("tr", null, ["Form", "Required", "Expected", "Submitted", "Missed", "Completion", "Who missed"].map((t, i) => h("th", { class: i >= 2 && i <= 4 ? "r" : null }, t)))),
      h("tbody", null, body)));
  }

  const slotLabel = (ss, se) => { const a = fmtClock(ss).replace(":00", ""); return se - ss > 60 ? `${a}–${fmtClock(se).replace(":00", "")}` : a; };
  // A grid square; its explanation shows on hover, and as a message when tapped on a phone.
  const square = (st, tip, mark, dot) => h("td", { class: "att-hc " + st, title: tip, onclick: () => toast(tip) }, mark, dot ? h("i") : null);
  const sample = (st, mark) => h("i", { class: "att-hc " + st }, mark || "");

  function formDetail(f) {
    if (f.frequency === "hour") {
      const mark = { done: "✓", missed: "✗", light: "·", open: "…", later: "", none: "", off: "" };
      const rows = f.grid.map(g => {
        const miss = g.cells.filter(c => c.st === "missed").length;
        return h("tr", null, h("th", { class: "hd" }, fmtDay(g.d)), g.cells.map(c => {
          const tip = [`${fmtDay(g.d)}, ${fmtClock(c.ss)}–${fmtClock(c.se)}`,
            c.st === "done" ? `Done by ${[...new Set(c.by.map(x => x.name))].join(", ")} at ${c.by.map(x => fmtClock(x.t)).join(", ")}` : c.st === "missed" ? "Not done" : c.st === "light" ? "On shift under 30 minutes, not required" : c.st === "open" ? "Not over yet" : c.st === "none" ? "Nobody on shift" : c.st === "off" ? "Not checked (interval set to 0)" : "",
            c.on.length ? `On shift: ${c.on.join(", ")}` : ""].filter(Boolean).join(" · ");
          return square(c.st, tip, mark[c.st], c.st !== "done" && c.by.length > 0);
        }), h("td", { class: "r num" }, miss ? h("b", { class: "att-bad-text" }, String(miss)) : h("span", { class: "att-nil" }, "0")));
      });
      const list = f.grid.flatMap(g => g.cells.filter(c => c.st === "missed").map(c => h("li", null, h("b", null, `${fmtDay(g.d)} ${fmtClock(c.ss)}`), ` · on shift: ${(c.blamed && c.blamed.length ? c.blamed : c.on).join(", ") || "—"}`)));
      return h("div", null,
        h("div", { class: "att-hgrid-wrap", "data-keep": "h-" + f.id }, h("table", { class: "att-hgrid" },
          h("thead", null, h("tr", null, h("th"), f.slots.map(([s0, s1]) => h("th", null, slotLabel(s0, s1))), h("th", { class: "r" }, "Missed"))),
          h("tbody", null, rows))),
        h("div", { class: "att-legend" }, h("span", null, sample("done", "✓"), "Done"), h("span", null, sample("missed", "✗"), "Missed"), h("span", null, sample("light", "·"), "On shift under 30 min"),
          h("span", null, sample("none"), "Nobody on shift"), h("span", null, sample("open", "…"), "Not over yet"),           h("span", null, "Hover or tap a square for who was on and who submitted.")),
        list.length ? h("details", { class: "att-misslist", open: !!ui.printing }, h("summary", null, `${plural(list.length, "missed check")}: times and who was on`), h("ul", null, list)) : null);
    }
    if (!f.people.length) return h("div", { class: "att-empty" }, "Nobody this form applies to worked in this period.");
    const days = f.people[0].cells.map(c => c.d);
    const body = f.people.map(pp => h("tr", null, h("th", { class: "hd" }, pp.name), pp.cells.map(c => {
      const st = !c.worked ? (c.sub ? "extra" : "none") : c.sub ? "done" : f.frequency === "week" ? "light" : "missed";
      return square(st, `${fmtDay(c.d)} · ${!c.worked ? "Not working" : c.sub ? "Submitted" : "No submission"}`, st === "done" || st === "extra" ? "✓" : st === "missed" ? "✗" : "", false);
    })));
    return h("div", null,
      h("div", { class: "att-hgrid-wrap", "data-keep": "p-" + f.id }, h("table", { class: "att-hgrid" },
        h("thead", null, h("tr", null, h("th"), days.map(d => h("th", null, DOW[dow(d)], h("br"), String(dt(d).getUTCDate()))))),
        h("tbody", null, body))),
      h("div", { class: "att-legend" }, h("span", null, sample("done", "✓"), "Worked and submitted"),
        h("span", null, sample(f.frequency === "week" ? "light" : "missed", f.frequency === "week" ? "" : "✗"), "Worked, no submission"),
        h("span", null, sample("none"), "Not working"), f.frequency === "week" ? h("span", null, "Weekly forms count once per week worked.") : null));
  }

  // ---------------------------------------------------------------- print (the browser's own PDF)
  function printHead(W) {
    const notes = [];
    if (W.inProgress) notes.push("Period in progress: shifts that had not ended are not counted.");
    if (W.totals.unchecked) notes.push(`${plural(W.totals.unchecked, "shift")} had no timesheets and were not checked.`);
    const now = new Date();
    return h("div", { class: "att-print-only att-print-head" },
      h("div", { class: "att-eyebrow" }, "Connecteam × Gusto"),
      h("h2", null, `Attendance & Leave · ${ui.period === "week" ? "Week of " : ui.period === "pay" ? "Pay period " : ""}${periodLabel(W.P, ui.period)}`),
      h("p", null, [ui.dept || "All departments", fmtRange(W.P.start, W.P.end), `Printed ${MONTHS[now.getMonth()]} ${now.getDate()}, ${now.getFullYear()} ${now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`].join("  ·  ")),
      notes.length ? h("p", null, notes.join("  ")) : null);
  }
  function printFoot(R) {
    return h("p", { class: "att-print-only att-print-foot" }, `Grace ${R.lateGrace} min late / ${R.earlyGrace} min early · lunch ${fmtDur(R.lunchMin)} for shifts of ${fmtH(R.lunchAfter)}+`);
  }
  function printReport(mode) {
    if (mode === "open" && !ui.open.size) { toast("Open at least one person’s row first, or choose “Everyone, with daily detail”.", true); return; }
    ui.printing = mode; draw();
    document.body.classList.add("att-printing");
    const done = () => { window.removeEventListener("afterprint", done); document.body.classList.remove("att-printing"); ui.printing = null; draw(); };
    window.addEventListener("afterprint", done);
    window.print();
  }

  // ---------------------------------------------------------------- copy and CSV
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); toast("Summary copied"); }
    catch {
      const ta = h("textarea", { readonly: true, class: "att-offscreen" }); ta.value = text; document.body.append(ta); ta.select();
      let ok = false; try { ok = document.execCommand("copy"); } catch { /* blocked */ }
      ta.remove(); toast(ok ? "Summary copied" : "Copy was blocked here. Use Download CSV instead.", !ok);
    }
  }
  function downloadCSV() {
    if (!current) return;
    ctx.api.download(fileName(current, ui.period, ui.dept, "csv"), csvText(current));
  }

  // ---------------------------------------------------------------- leave balances tab (Gusto)
  function when(v) {
    if (v == null || v === "") return "";
    const t = typeof v === "number" ? new Date(v < 1e11 ? v * 1000 : v) : new Date(v);
    return isNaN(t) ? String(v) : `${MONTHS[t.getMonth()]} ${t.getDate()}, ${t.getFullYear()} ${t.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  }
  async function uploadGusto(input) {
    const file = input.files && input.files[0]; input.value = "";
    if (!file) return;
    if (!/\.csv$/i.test(file.name)) { toast("Choose the CSV file from Gusto (Excel files aren't read here).", true); return; }
    let text = "";
    try { text = await file.text(); } catch { toast("Couldn't read that file.", true); return; }
    if (!gustoTable(text)) { toast("That doesn't look like Gusto's Time Off Balances report: no name and balance columns found.", true); return; }
    ui.loading = true; draw();
    // The app says how it went, and draws the report again once it's uploaded.
    const res = await ctx.api.upload("gusto", file);
    if (!res || !res.ok) { ui.loading = false; draw(); }
  }
  function gustoView() {
    const d = ctx.data, g = d.gusto, kids = [];
    const head = h("div", { class: "att-panel-head" }, h("div", null, h("h2", null, "Gusto time off balances"),
      h("p", null, "In Gusto: Reports › Time Off Balances, downloaded as CSV. Only the newest upload is used, and everyone who can see this report sees it. Each person’s Gusto title is their department here unless Staff Access knows their department.")));
    if (canManage()) kids.push(h("label", { class: "att-btn primary att-file-btn" }, "Upload Gusto CSV",
      h("input", { type: "file", accept: ".csv,text/csv", "aria-label": "Choose the Gusto CSV file", onchange: e => uploadGusto(e.target) })));
    if (!g) kids.push(h("p", { class: "att-muted" }, canManage() ? "Nothing uploaded yet." : "Nothing uploaded yet. Someone who manages this report can upload it."));
    else {
      kids.push(h("div", { class: "att-file" + (model.gustoOk ? "" : " err") }, h("b", null, g.name || "Gusto file"),
        h("span", null, model.gustoOk ? `Uploaded ${when(g.uploaded_at)}${g.by ? ` by ${g.by}` : ""} · ${plural(model.balPeople, "person", "people")}` : "The columns weren't recognised. Upload Gusto’s Time Off Balances report as CSV.")));
      if (model.gustoOk) {
        if (model.fuzzy.length) kids.push(h("p", { class: "att-muted" }, `Matched by similar first name: ${model.fuzzy.map(([a, b]) => `${a} = ${b}`).join(", ")}. If one is wrong, add the right pair under Rules.`));
        if (model.balUnmatched.length) kids.push(h("p", { class: "att-warn-text" }, `In Gusto but not seen in Connecteam this period: ${model.balUnmatched.join(", ")}.`));
        if (model.noBal.length) kids.push(h("p", { class: "att-warn-text" }, `In Connecteam but no Gusto balance: ${model.noBal.join(", ")}. Add name matches under Rules if they’re in Gusto under another name.`));
      }
    }
    return h("section", { class: "att-panel" }, head, h("div", { class: "att-pad" }, kids));
  }

  // ---------------------------------------------------------------- rules tab
  function draft() {
    if (!ui.draft) { const s = ctx.data.settings || {}; ui.draft = { rules: { ...DEFAULT_RULES, ...(s.rules || {}), earlySkip: [...((s.rules || {}).earlySkip || [])] }, forms: (s.forms || []).map(f => ({ ...f })), who_sees: s.who_sees === "own" ? "own" : "all" }; }
    return ui.draft;
  }
  // The app says how it went, and draws the report again with the saved rules.
  async function saveRules() {
    const sent = ui.draft;
    const res = await ctx.api.saveSettings(sent);
    if (res && res.ok && ui.draft === sent) { ui.draft = null; ui.dirty = false; }
  }
  function rulesView() {
    const manage = canManage(), D = draft(), R = D.rules, ro = !manage;
    const changed = () => { ui.dirty = true; draw(); };
    const field = (id, label, input, hint) => h("div", { class: "att-field" }, h("label", { for: id }, label), input, hint ? h("span", { class: "att-hint" }, hint) : null);
    const num = (k, label, hint, step) => field("att-r-" + k, label, h("input", { id: "att-r-" + k, class: "att-input", type: "number", min: "0", step: String(step || 1), value: String(R[k]), disabled: ro, inputmode: "decimal",
      onchange: e => { R[k] = +e.target.value || 0; changed(); } }), hint);
    const skip = new Set((R.earlySkip || []).map(s => s.toLowerCase()));
    const form = h("div", { class: "att-rules" },
      field("att-r-whoSees", "Who can see whom", h("select", { id: "att-r-whoSees", class: "att-input", disabled: ro, onchange: e => { D.who_sees = e.target.value; changed(); } },
        [["all", "Everyone sees all departments"], ["own", "Viewers see only their own department"]].map(([v, t]) => h("option", { value: v, selected: D.who_sees === v }, t))),
        "With “only their own department”, people who view this report see only the people in their department. People who manage it see everyone."),
      field("att-r-weekStart", "Week starts on", h("select", { id: "att-r-weekStart", class: "att-input", disabled: ro, onchange: e => { R.weekStart = +e.target.value; changed(); } },
        DOW_LONG.map((x, i) => h("option", { value: String(i), selected: +R.weekStart === i }, x))), "Connecteam schedules usually run Sunday to Saturday."),
      num("lateGrace", "Late after (minutes)", "Clock-ins within this many minutes of the scheduled start are on time."),
      num("earlyGrace", "Early clock-out after (minutes)", "Clock-outs within this many minutes of the scheduled end are fine."),
      num("lunchAfter", "Lunch required for shifts of (hours)", "Oregon requires a 30-minute meal period for work periods of 6 hours or more.", 0.5),
      num("lunchMin", "Minimum lunch (minutes)", "Lunch breaks and gaps between punches count toward lunch."),
      num("defaultShift", "Shift length if no end time (hours)", "Used when a scheduled shift only has a start time.", 0.5),
      num("lowSick", "Flag sick balances under (hours)", "Skipped for anyone who hasn’t accrued this much yet, like new hires. Below zero is always flagged.", 0.5),
      num("lowPto", "Flag PTO balances under (hours)", "PTO builds slowly, so this is set lower. Same new-hire rule. 0 turns the PTO flag off.", 0.5),
      num("actionAt", "Action at this many incidents a week", "Late, early, lunch, missed shift and form misses. Scaled up for pay periods and months. Any uncovered missed shift is always Action."),
      h("div", { class: "att-field span" }, h("span", { class: "att-label" }, "Check early clock-outs for"),
        model.departments.length ? h("div", { class: "att-checks" }, model.departments.map(x => h("label", null, h("input", { type: "checkbox", checked: !skip.has(x.toLowerCase()), disabled: ro,
          onchange: e => { const set = new Set(R.earlySkip || []); if (e.target.checked) [...set].filter(y => y.toLowerCase() === x.toLowerCase()).forEach(y => set.delete(y)); else set.add(x); R.earlySkip = [...set]; changed(); } }), x)))
          : h("span", { class: "att-hint" }, "Departments and jobs appear here once there are shifts in the period shown."),
        h("span", { class: "att-hint" }, "Everyone is checked by default, since leaving early needs approval. Untick a department, Gusto title or Connecteam job to stop flagging early clock-outs there. Late arrivals and missed lunches are still checked.")),
      field("att-r-aliases", "Name matches between Gusto and Connecteam", h("textarea", { id: "att-r-aliases", class: "att-input att-mono", disabled: ro, maxlength: "12000", placeholder: "Robert Smith = Bobby Smith\nNguyen, Thanh = Ty Nguyen", onchange: e => { R.aliases = e.target.value; changed(); } }, R.aliases || ""),
        "One per line: the name in Gusto, then =, then the Connecteam name. People are matched by their Connecteam ID first; middle names, initials and common short names (Tom and Thomas, Nate and Nathan) already match on their own when only one person fits."));
    form.querySelector("textarea").value = R.aliases || "";
    const top = h("div", { class: "att-panel-head" }, h("h2", null, "Rules"),
      manage ? h("div", { class: "att-actions" },
        h("button", { type: "button", class: "att-btn", onclick: () => { D.rules = { ...DEFAULT_RULES, aliases: R.aliases, earlySkip: [] }; changed(); } }, "Reset to defaults"),
        h("button", { type: "button", class: "att-btn primary", disabled: !ui.dirty, onclick: saveRules }, "Save"),
        ui.dirty ? h("button", { type: "button", class: "att-btn", onclick: () => { ui.draft = null; ui.dirty = false; draw(); } }, "Undo changes") : null,
        ui.dirty ? h("p", { class: "att-hint att-unsaved", role: "status" }, "Not saved yet. Changes here and under Required forms are kept while you look at other tabs, and saved together.") : null)
        : h("p", null, "Only people who manage this report can change these."));
    return h("section", { class: "att-rules-tab" }, h("div", { class: "att-panel" }, top, form), formsEditor(D, ro, changed));
  }

  function formsEditor(D, ro, changed) {
    const info = new Map(model.forms.map(f => [f.id, f]));
    const items = D.forms.map((f, idx) => {
      const hourly = f.frequency === "hour";
      const due = h("select", { class: "att-input", disabled: ro, "aria-label": "Due", onchange: e => { f.frequency = e.target.value; if (f.frequency === "hour" && !f.every) { f.every = 1; f.night_every = 0; } changed(); } },
        [["shift", "Every shift worked"], ["week", "Once a week"], ["hour", "On a timer, by whoever is on shift"]].map(([v, t]) => h("option", { value: v, selected: f.frequency === v }, t)));
      const fi = info.get(f.id);
      return h("div", { class: "att-form-item" },
        h("div", { class: "att-form-row" },
          h("div", { class: "att-field" }, h("label", null, "Form"), h("input", { class: "att-input", value: f.name, disabled: ro, "aria-label": "Form name", onchange: e => { f.name = e.target.value; changed(); } })),
          h("div", { class: "att-field" }, h("label", null, "Due"), due),
          whoPicker(f, ro, changed),
          ro ? null : h("button", { type: "button", class: "att-btn ghost", onclick: () => { D.forms.splice(idx, 1); changed(); } }, "Remove")),
        hourly ? hourFields(f, ro, changed) : null,
        fi && fi.unmatched.length ? h("div", { class: "att-warn-text" }, `Submitted by people not seen working this period: ${fi.unmatched.slice(0, 8).join(", ")}`) : null);
    });
    let add = null;
    if (!ro) {
      const picked = new Set(D.forms.map(f => f.id));
      const choices = (ctx.data.form_choices || []).filter(c => !picked.has(c.id));
      if (choices.length) {
        const sel = h("select", { class: "att-input", "aria-label": "Connecteam form" }, choices.map(c => h("option", { value: c.id }, c.name)));
        add = h("div", { class: "att-add-form" }, sel, h("button", { type: "button", class: "att-btn", onclick: () => {
          const c = choices.find(x => x.id === sel.value); if (!c) return;
          D.forms.push({ id: c.id, name: c.name, frequency: "shift", applies_to: "", every: 1, night_every: 0, day_from: "06:00", day_until: "22:00" }); changed();
        } }, "Add form"));
      } else add = h("p", { class: "att-hint" }, ui.loading ? "Loading Connecteam forms…" : (ctx.data.form_choices || []).length ? "Every Connecteam form is already here." : "No Connecteam forms found. If Connecteam data didn't load, try again in a few minutes.");
    }
    return h("div", { class: "att-panel" },
      h("div", { class: "att-panel-head" }, h("h2", null, "Required forms"), h("p", null, "Connecteam forms people must fill in: every shift, once a week, or on a timer by whoever is on shift. Saved with the rules.")),
      h("div", { class: "att-pad att-form-list" }, items.length ? items : h("p", { class: "att-muted" }, "None yet."), add));
  }

  // A timed form's checked hours and how often in them, and how often outside them (0: not at all).
  function hourFields(f, ro, changed) {
    const hrs = checkHours(f);
    const clock = m => `${String(Math.floor(m / 60) % 24).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
    const time = (key, value, label) => h("input", { class: "att-input", type: "time", step: "1800", value: clock(value), disabled: ro, "aria-label": label,
      onchange: e => { f[key] = e.target.value; changed(); } });
    const num = (key, value, label) => h("input", { class: "att-input", type: "number", min: "0", step: "0.5", value: String(value), disabled: ro, "aria-label": label,
      onchange: e => { f[key] = e.target.value; changed(); } });
    return h("div", { class: "att-form-row four" },
      h("div", { class: "att-field" }, h("label", null, "Checked from"), time("day_from", hrs.from, "Checked from")),
      h("div", { class: "att-field" }, h("label", null, "Until"), time("day_until", hrs.until, "Until")),
      h("div", { class: "att-field" }, h("label", null, "Every (hours)"), num("every", hoursSetting(f.every, 1), "Every how many hours")),
      h("div", { class: "att-field" }, h("label", null, "Rest of the night (hours) · 0 = not checked"), num("night_every", hoursSetting(f.night_every, hoursSetting(f.every, 1)), "Rest of the night, every how many hours")));
  }

  function whoPicker(f, ro, changed) {
    const hourly = f.frequency === "hour";
    const chosen = String(f.applies_to || "").split(/[,;\n]/).map(x => x.trim()).filter(Boolean);
    const isOn = v => chosen.some(c => c.toLowerCase() === v.toLowerCase());
    const jobs = model.departments;
    const people = hourly ? [] : model.people.map(p => p.name).sort((a, b) => a.localeCompare(b));
    const extra = chosen.filter(c => ![...jobs, ...people].some(v => v.toLowerCase() === c.toLowerCase()));
    const set = list => { f.applies_to = list.join(", "); changed(); };
    const box = v => h("label", null, h("input", { type: "checkbox", checked: isOn(v), disabled: ro,
      onchange: e => set(e.target.checked ? [...chosen, v] : chosen.filter(c => c.toLowerCase() !== v.toLowerCase())) }), v);
    return h("div", { class: "att-field" }, h("span", { class: "att-label" }, hourly ? "Jobs or departments on shift" : "Who must submit"),
      h("details", { class: "att-ms" }, h("summary", null, chosen.length ? chosen.join(", ") : h("span", { class: "att-muted" }, hourly ? "Anyone on shift" : "Everyone who worked")),
        h("div", { class: "att-ms-pop" },
          chosen.length && !ro ? h("button", { type: "button", class: "att-clear", onclick: () => set([]) }, `Clear (${hourly ? "anyone on shift" : "everyone"})`) : null,
          extra.length ? [h("div", { class: "grp" }, "Typed earlier"), extra.map(box)] : null,
          h("div", { class: "grp" }, "Departments and jobs"), jobs.length ? jobs.map(box) : h("div", { class: "grp" }, "None in this period yet"),
          people.length ? [h("div", { class: "grp" }, "People"), people.map(box)] : null)));
  }

  // ---------------------------------------------------------------- hook up
  const core = { parseCSV, parseHours, normKey, niceName, firstNamesAlike, NICKNAMES, gustoTable, buildModel, computePeriod, periodOf, periodLabel, blocksFor, daySlots, hoursSetting, freqText,
    summaryText, csvText, fileName, fmtDur, fmtClock, ymd, switchAnchor, DEFAULT_RULES };
  if (typeof module === "object" && module.exports) module.exports = core;
  if (typeof StaffReports !== "undefined") StaffReports.register("attendance", { render });
})();
