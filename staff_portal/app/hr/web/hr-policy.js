// HR portal: the point system. HR's Discipline setup (the point system as settings, in versions:
// offences in an editable table, steps, ladders as threshold bars, escalation, expiry and
// credits, probation, visibility and approval), the simulator, the import spreadsheet, publishing
// with a date, re-scoring, and the point system as everyone may read it. Plain script under the
// page's strict Content-Security-Policy: everything shown is text (h() makes text nodes, never
// HTML), sizes are set through the style object, and the styles are in hr-policy.css.
(() => {
  "use strict";

  const H = window.HRPortal;
  if (!H) return;
  const { api, toast, confirmDialog, openDialog, show } = H;
  // Children may be nested lists, and nothing (null, false) is left out: H.h flattens one level.
  const kids = (xs) => xs.flat(Infinity).filter((x) => x != null && x !== false);
  const h = (tag, attrs, ...children) => H.h(tag, attrs, ...kids(children));
  const add = (el, ...xs) => { el.append(...kids(xs)); return el; };
  const fill = (el, ...xs) => { el.replaceChildren(...kids(xs)); return el; };
  const SVG = "http://www.w3.org/2000/svg";
  const version = document.currentScript ? new URL(document.currentScript.src).searchParams.get("v") || "" : "";
  add(document.head, h("link", { rel: "stylesheet", href: `/static/hr-policy.css?v=${version}` }));

  const NOT_SET_UP = "The point system isn't set up yet.";
  const drafts = {};      // unsaved edits by version, kept while the page is open
  const sims = {};        // the simulator's made-up incidents by version

  // ---------------------------------------------------------------- basics

  const quiet = (err) => ["Signed out", "Moved on"].includes(err && err.message);

  async function run(fn, okText) {
    try {
      const result = await fn();
      if (result !== false && okText) toast(okText);
      return result;
    } catch (err) {
      if (!quiet(err)) toast(err.message, true);
      return false;
    }
  }

  const clone = (x) => JSON.parse(JSON.stringify(x));
  const num = (n) => String(Math.round(Number(n) * 100) / 100);
  const pts = (n) => `${num(n)} point${Math.abs(Number(n)) === 1 ? "" : "s"}`;
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  function day(iso) {
    if (!iso) return "";
    const [y, m, d] = String(iso).slice(0, 10).split("-").map(Number);
    return new Date(y, m - 1, d).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
  }

  function span(s) {
    if (!s || typeof s !== "object") return "never";
    if ("days" in s) return plural(s.days, "day");
    return s.months % 12 === 0 && s.months >= 24 ? plural(s.months / 12, "year") : plural(s.months, "month");
  }

  function ordinal(n) {
    const tail = n % 100 >= 10 && n % 100 <= 20 ? "th" : { 1: "st", 2: "nd", 3: "rd" }[n % 10] || "th";
    return `${n}${tail}`;
  }

  function keyFor(name, taken) {
    let base = String(name || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 24) || "item";
    if (!/^[a-z]/.test(base)) base = `x_${base}`.slice(0, 24);
    let key = base;
    for (let n = 2; taken.has(key); n += 1) key = `${base}_${n}`;
    return key;
  }

  function download(name, text) {
    const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
    const a = h("a", { href: url, download: name, hidden: true });
    add(document.body, a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  function svg(tag, attrs, ...children) {
    const el = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null && v !== false) el.setAttribute(k, v);
    for (const c of kids(children)) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return el;
  }

  // One tooltip for every chart on the page; text only.
  const tip = h("div", { class: "viz-tip", role: "tooltip", hidden: true });
  add(document.body, tip);
  function showTip(x, y, lines) {
    fill(tip, ...lines.map((l) => h("div", { class: l.cls || "" }, l.text)));
    tip.hidden = false;
    const w = tip.offsetWidth;
    const left = Math.max(8 + window.scrollX, Math.min(x - w / 2, window.scrollX + document.documentElement.clientWidth - w - 8));
    tip.style.left = `${left}px`;
    tip.style.top = `${y - tip.offsetHeight - 10}px`;
  }
  const hideTip = () => { tip.hidden = true; };
  window.addEventListener("hashchange", hideTip);

  // ---------------------------------------------------------------- ladders: threshold bars

  const shade = (i, n) => `r${n <= 1 ? 3 : Math.round((i * 4) / (n - 1)) + 1}`;

  // A ladder as a bar from 0 points: each step's stretch in the ordinal ramp, thresholds below,
  // and each stretch says what it is on hover or focus. stepName(key) gives a step's name.
  function ladderBar(rungs, stepName) {
    const valid = (rungs || []).filter((r) => Number(r.points) > 0).map((r) => ({ points: Number(r.points), step: r.step }))
      .sort((a, b) => a.points - b.points);
    const box = h("div", { class: "lbar-box" });
    if (!valid.length) return box;
    const top = valid.at(-1).points;
    const end = top + Math.max(2, top * 0.2);
    const segs = [{ from: 0, to: valid[0].points, step: null }]
      .concat(valid.map((r, i) => ({ from: r.points, to: i + 1 < valid.length ? valid[i + 1].points : end, step: r.step, i })));
    const words = valid.map((r) => `${num(r.points)} points: ${stepName(r.step)}`).join(", ");
    const bar = h("div", { class: "lbar", role: "img", "aria-label": `Ladder. ${words}.` });
    for (const s of segs) {
      const text = s.step ? `${stepName(s.step)}: from ${pts(s.from)}` : `No step: under ${pts(s.to)}`;
      const seg = h("div", { class: `lseg ${s.step ? shade(s.i, valid.length) : "none"}`, tabindex: "0", "aria-label": text });
      seg.style.flexGrow = String(Math.max(s.to - s.from, 0.01));
      const at = () => {
        const r = seg.getBoundingClientRect();
        showTip(r.left + r.width / 2 + window.scrollX, r.top + window.scrollY, [{ text, cls: "v" }]);
      };
      seg.addEventListener("pointerenter", at);
      seg.addEventListener("focus", at);
      seg.addEventListener("pointerleave", hideTip);
      seg.addEventListener("blur", hideTip);
      add(bar, seg);
    }
    const ticks = h("div", { class: "lticks", "aria-hidden": "true" });
    let last = -100;
    for (const at of [0, ...valid.map((r) => r.points)]) {
      const pct = (at / end) * 100;
      if (pct - last < 7) continue;        // too close to the one before: the list below has it
      last = pct;
      const t = h("span", null, num(at));
      t.style.left = `${pct}%`;
      add(ticks, t);
    }
    add(box, bar, ticks);
    return box;
  }

  // ---------------------------------------------------------------- the point system, to read

  async function showRead(main, data) {
    const top = h("section", { class: "card" }, h("h1", null, "Point system"));
    if (H.me && H.me.hr) add(top, h("p", null, h("a", { href: "#/policy" }, "Discipline setup")));
    if (!data.set_up) {
      add(top, h("p", { class: "warn" }, data.message || NOT_SET_UP));
      return show(top);
    }
    const p = data.policy;
    add(top, h("p", { class: "muted" }, `${p.name}, in force since ${day(p.effective)}.`),
      h("p", null, p.expiry), p.credits ? h("p", null, p.credits) : null,
      h("p", { class: "muted small" }, p.steps_in_order ? "Steps go in order, though serious offences can go straight to a higher step."
        : "Points can lead straight to any step."));
    const steps = h("section", { class: "card" }, h("h2", null, "Steps"));
    for (const t of p.tracks) {
      if (p.tracks.length > 1) add(steps, h("h3", null, t.name));
      add(steps, ladderBar(t.ladder.map((r) => ({ points: r.points, step: r.step })), (s) => s),
        h("ul", { class: "legend-list" }, t.ladder.map((r, i) => h("li", null,
          h("span", { class: `sw ${shade(i, t.ladder.length)}` }), `${pts(r.points)} or more: ${r.step}`))));
    }
    add(steps, h("h3", null, "How long each step stays active"),
      h("ul", { class: "legend-list" }, p.steps.map((s) => h("li", null, `${s.name}: ${s.active || "until HR closes it"}`))));
    const byCategory = {};
    for (const o of p.offences) (byCategory[o.category || "Other"] ||= []).push(o);
    const offences = h("section", { class: "card" }, h("h2", null, "Offences and their points"),
      Object.entries(byCategory).map(([cat, list]) => [h("h3", null, cat), h("ul", { class: "offence-list" }, list.map((o) => h("li", null,
        h("span", { class: "pts" }, pts(o.points)), h("div", { class: "title" }, o.name),
        o.range[0] !== o.range[1] ? h("div", { class: "why" }, `A manager may choose ${num(o.range[0])} to ${num(o.range[1])}.`) : null,
        o.repeat ? h("div", { class: "why" }, `Repeats: ${o.repeat}.`) : null,
        o.min_step ? h("div", { class: "why" }, `Goes to ${o.min_step} at least.`) : null,
        o.wording ? h("div", { class: "why" }, o.wording) : null)))]));
    const probation = p.probation.length ? h("section", { class: "card" }, h("h2", null, "Probation"),
      p.probation.map((x) => [h("h3", null, x.name), h("p", { class: "muted small" }, `${x.kind}. ${plural(x.days, "day")}` +
        (x.multiplier !== 1 ? `; points × ${num(x.multiplier)}` : "") + (x.serious ? `; a serious offence goes to ${x.serious}` : "") + "."),
      x.ladders.map((l) => h("ul", { class: "legend-list" }, l.ladder.map((r) => h("li", null, `${pts(r.points)} or more: ${r.step}`))))])) : null;
    const leave = h("section", { class: "card" }, h("h2", null, "Protected leave"),
      h("p", { class: "muted small" }, "An absence that was protected leave scores 0 points and never leads to a step:"),
      h("ul", { class: "legend-list" }, p.protected_leave.map((n) => h("li", null, n))));
    show(top, steps, offences, probation, leave);
  }

  // ---------------------------------------------------------------- setup: the versions

  async function showSetup(main) {
    const again = () => showSetup(main);
    const data = await api("GET", "/api/policy");
    const now = data.current;
    const status = h("section", { class: "card" }, h("h1", null, "Discipline setup"),
      now ? h("p", null, `In use: ${now.name}, since ${day(now.effective)}.`) : h("p", { class: "warn" },
        `${NOT_SET_UP} Load your written point system (import the spreadsheet, or edit a draft), test it in the simulator, then publish it with a date. Until then managers can't record incidents.`),
      h("div", { class: "actions" },
        h("a", { class: "button", href: "#/policy/import" }, "Import a spreadsheet"),
        h("button", { type: "button", onclick: () => newDraft("sample") }, "New draft from the sample"),
        h("button", { type: "button", onclick: () => newDraft("blank") }, "New blank draft"),
        now ? h("a", { class: "button", href: "#/policy/rescore" }, "Re-score people") : null,
        h("a", { class: "button", href: "#/policy/read" }, "Read it as staff do")));
    const list = h("section", { class: "card versions" }, h("h2", null, "Versions"),
      h("p", { class: "muted small" }, "Drafts aren't used until published. A published version is never changed: make a new draft from it. Every incident keeps the version it was scored under."),
      h("ul", { class: "list" }, data.versions.map((v) => h("li", null, h("a", { class: "item", href: `#/policy/${v.id}` },
        h("div", { class: "title" }, v.name),
        h("div", { class: "sub" }, [v.label, v.state === "draft" ? `changed ${H.when(v.updated_at)}` : null].filter(Boolean).join(" · ")))))));
    show(status, list);

    async function newDraft(from) {
      const r = await run(() => api("POST", "/api/policy", { from }), "Draft made.");
      if (r) location.hash = `#/policy/${r.id}`;
      else await again();
    }
  }

  // ---------------------------------------------------------------- setup: one version

  async function showEditor(main, id) {
    const data = await api("GET", `/api/policy/${id}`);
    const editable = data.version.status === "draft";
    const kept = drafts[id];
    const b = editable && kept ? kept.body : clone(data.body);
    const ed = { editable, dirty: editable && !!kept, fresh: new Set(), problems: data.problems };
    const stepOptions = () => [...b.steps].sort((x, y) => x.rank - y.rank).map((s) => [s.key, s.name || s.key]);
    const stepName = (k) => (b.steps.find((s) => s.key === k) || {}).name || k;
    const dirtyNote = h("span", { class: "pol-dirty", hidden: !ed.dirty }, "Unsaved changes");
    const touch = () => {
      if (!ed.editable) return;
      ed.dirty = true;
      drafts[id] = { body: b };
      dirtyNote.hidden = false;
    };

    // ------------ small bound controls
    const dis = !editable;
    function textIn(obj, key, label, { max = 80, onInput } = {}) {
      const el = h("input", { type: "text", value: obj[key] ?? "", maxlength: String(max), disabled: dis, "aria-label": label });
      el.addEventListener("input", () => { obj[key] = el.value; touch(); if (onInput) onInput(el.value); });
      return el;
    }
    function numIn(obj, key, label, { step = "0.25", min = "0", max = null, blank = false, onInput } = {}) {
      const el = h("input", { type: "number", inputmode: "decimal", step, min, max, value: obj[key] ?? "", disabled: dis, "aria-label": label });
      el.addEventListener("input", () => {
        obj[key] = el.value === "" ? (blank ? null : "") : Number(el.value);
        touch();
        if (onInput) onInput();
      });
      return el;
    }
    function checkIn(obj, key, label, { onChange, hint } = {}) {
      const el = h("input", { type: "checkbox", checked: !!obj[key], disabled: dis });
      el.addEventListener("change", () => { obj[key] = el.checked; touch(); if (onChange) onChange(); });
      return h("label", { class: "check" }, el, h("span", null, label, hint ? h("small", null, hint) : null));
    }
    function selectIn(obj, key, label, options, { none = null, onChange, parse } = {}) {
      const el = h("select", { disabled: dis, "aria-label": label },
        none !== null ? h("option", { value: "" }, none) : null,
        options.map(([value, text]) => h("option", { value, selected: String(obj[key]) === String(value) }, text)));
      el.addEventListener("change", () => {
        obj[key] = el.value === "" ? null : parse ? parse(el.value) : el.value;
        touch();
        if (onChange) onChange();
      });
      return el;
    }
    function spanIn(obj, key, label, { never = true } = {}) {
      const cur = obj[key] && typeof obj[key] === "object" ? obj[key] : null;
      const n = h("input", { type: "number", inputmode: "numeric", min: "1", step: "1", value: cur ? (cur.days ?? cur.months) : "",
        disabled: dis || (never && !cur), "aria-label": `${label}: how many` });
      const u = h("select", { disabled: dis, "aria-label": `${label}: days or months` },
        h("option", { value: "days", selected: !!(cur && "days" in cur) }, "days"),
        h("option", { value: "months", selected: !!(cur && "months" in cur) }, "months"),
        never ? h("option", { value: "never", selected: !cur }, "never") : null);
      const sync = () => {
        if (u.value === "never") {
          obj[key] = null;
          n.value = "";
          n.disabled = true;
        } else {
          n.disabled = dis;
          obj[key] = { [u.value]: n.value === "" ? 0 : Number(n.value) };
        }
        touch();
      };
      n.addEventListener("input", sync);
      u.addEventListener("change", sync);
      return h("span", { class: "pol-span" }, n, u);
    }
    const field = (label, control, hint, cls) => h("label", { class: cls || "" }, label, hint ? h("small", null, hint) : null, control);
    const removeBtn = (label, fn) => (editable ? h("button", { type: "button", class: "linkish", onclick: () => { fn(); touch(); } }, label) : null);
    const addBtn = (label, fn) => (editable ? h("div", { class: "actions" }, h("button", { type: "button", onclick: () => { fn(); touch(); } }, label)) : null);

    // A section that draws itself again when its rows change.
    function section(title, note, draw) {
      const box = h("section", { class: "card" });
      const redraw = () => fill(box, h("h2", null, title), note ? h("p", { class: "pol-note" }, note) : null, draw(redraw));
      redraw();
      box.redraw = redraw;
      return box;
    }

    // Keys: given once from the name. Something added in this editing session takes its name's
    // key while it's typed, and everything pointing at it follows.
    function rekey(kind, oldKey, newKey) {
      if (oldKey === newKey) return;
      const swap = (v) => (v === oldKey ? newKey : v);
      if (kind === "step") {
        for (const rungs of Object.values(b.ladders)) for (const r of rungs) r.step = swap(r.step);
        for (const e of b.escalation) { e.while = swap(e.while); e.to = swap(e.to); }
        for (const o of b.offences) {
          o.min_step = swap(o.min_step);
          for (const t of (o.repeat && o.repeat.tiers) || []) t.step = swap(t.step);
        }
        for (const p of b.probation) {
          p.serious_floor = swap(p.serious_floor);
          for (const rungs of Object.values(p.ladders || {})) for (const r of rungs) r.step = swap(r.step);
        }
      } else if (kind === "track") {
        for (const o of b.offences) o.track = swap(o.track);
        const move = (obj) => { if (obj && oldKey in obj) { obj[newKey] = obj[oldKey]; delete obj[oldKey]; } };
        move(b.ladders);
        for (const p of b.probation) move(p.ladders);
        b.credits.tracks = (b.credits.tracks || []).map(swap);
      } else if (kind === "offence") {
        for (const p of b.probation) {
          if (p.overrides && oldKey in p.overrides) { p.overrides[newKey] = p.overrides[oldKey]; delete p.overrides[oldKey]; }
        }
      }
    }
    function nameIn(item, list, kind, label, onInput) {
      return textIn(item, "name", label, { onInput: (value) => {
        if (ed.fresh.has(item)) {
          const taken = new Set(list.filter((x) => x !== item).map((x) => x.key));
          const key = keyFor(value || kind, taken);
          rekey(kind, item.key, key);
          item.key = key;
        }
        if (onInput) onInput();
      } });
    }

    // ------------ header: name, state, problems and actions
    const problemsBox = h("div");
    const drawProblems = () => fill(problemsBox, ed.problems.length
      ? [h("p", { class: "warn" }, `${plural(ed.problems.length, "problem")} to fix before it can be published:`),
        h("ul", { class: "pol-problems" }, ed.problems.slice(0, 30).map((p) => h("li", null, p)))]
      : h("p", { class: "pol-ok" }, editable ? "No problems: test it in the simulator, then publish it." : "No problems."));
    drawProblems();

    async function save(quietly = false) {
      const r = await run(() => api("PUT", `/api/policy/${id}`, { body: b }), quietly ? null : "Saved.");
      if (!r) return false;
      ed.problems = r.problems;
      ed.dirty = false;
      delete drafts[id];
      dirtyNote.hidden = true;
      drawProblems();
      return true;
    }

    async function publish() {
      if (ed.dirty && !(await save(true))) return;
      if (ed.problems.length) return toast("Fix the problems first.", true);
      const input = h("input", { type: "date", value: data.today, required: true, min: data.first ? null : data.today });
      const choice = await openDialog("Publish the point system", [
        h("p", null, "From the day you choose, new incidents are scored with this version, and managers can record incidents. Earlier incidents keep the points they were given unless you re-score them."),
        data.first ? h("p", { class: "muted small" }, "This is the first version, so it can start earlier, to load past incidents.") : null,
        h("label", null, "It starts on", input),
        h("p", { class: "muted small" }, "Publishing asks for your code (or PIN) again.")],
      [{ label: "Cancel", value: "cancel" }, { label: "Publish", value: "ok", class: "primary" }]);
      if (choice !== "ok") return;
      const r = await run(() => api("POST", `/api/policy/${id}/publish`, { effective: input.value }), "Published.");
      if (r) await showEditor(main, id);
    }

    const name = editable ? field("Name", textIn(b, "name", "Name of this version")) : h("p", { class: "pol-name" }, b.name);
    const header = h("section", { class: "card" },
      h("div", { class: "pol-head" }, h("h1", null, editable ? "Edit the point system" : "Point system"),
        h("span", { class: "badge" }, data.version.label)),
      name, problemsBox,
      h("div", { class: "actions" },
        editable ? h("button", { type: "button", class: "primary", onclick: () => save() }, "Save draft") : null,
        h("a", { class: "button", href: `#/policy/${id}/simulate` }, "Simulator"),
        editable ? h("button", { type: "button", onclick: publish }, "Publish…") : null,
        h("button", { type: "button", onclick: () => run(async () => {
          const r = await api("GET", `/api/policy/${id}/export`);
          download(r.filename, r.csv);
        }) }, "Export spreadsheet"),
        !editable ? h("button", { type: "button", onclick: () => run(async () => {
          const r = await api("POST", "/api/policy", { from: id });
          location.hash = `#/policy/${r.id}`;
        }, "Draft made.") }, "New draft from this") : null,
        data.version.state === "scheduled" ? h("button", { type: "button", class: "danger", onclick: async () => {
          if (!(await confirmDialog(`Withdraw ${b.name}? It won't start on ${day(data.version.effective)}.`, "Withdraw", true))) return;
          if (await run(() => api("POST", `/api/policy/${id}/withdraw`, {}), "Withdrawn.")) await showEditor(main, id);
        } }, "Withdraw") : null,
        data.version.state === "in_use" ? h("a", { class: "button", href: "#/policy/rescore" }, "Re-score people") : null,
        editable ? h("button", { type: "button", class: "danger", onclick: async () => {
          if (!(await confirmDialog(`Delete the draft ${b.name}?`, "Delete", true))) return;
          if (await run(() => api("DELETE", `/api/policy/${id}`), "Deleted.")) {
            delete drafts[id];
            location.hash = "#/policy";
          }
        } }, "Delete draft") : null,
        dirtyNote),
      h("p", null, h("a", { href: "#/policy" }, "All versions")));

    // ------------ basics: point size, order of steps, point totals
    const basics = section("Basics", null, (redraw) => [
      field("Points are", selectIn(b, "point_size", "Point size", [["1", "Whole points"], ["0.5", "Half points"], ["0.25", "Quarter points"]],
        { parse: Number }), "Every offence, threshold and adjustment uses these."),
      h("fieldset", { class: "pol-set" }, h("legend", null, "Steps"),
        [[false, "Steps go in order (serious offences, repeats and escalation can still jump)"], [true, "Points can skip steps"]].map(([value, label]) => {
          const el = h("input", { type: "radio", name: `skip-${id}`, checked: !!b.skip_steps === value, disabled: dis });
          el.addEventListener("change", () => { b.skip_steps = value; touch(); });
          return h("label", { class: "check" }, el, label);
        })),
      h("h3", null, "Point totals"),
      h("p", { class: "pol-note" }, "Most places keep one total, Discipline. Some keep Attendance and Conduct apart: each total has its own ladder."),
      b.tracks.map((t) => h("div", { class: "pol-row" }, field("Name", nameIn(t, b.tracks, "track", "Point total name"), null, "wide"),
        h("span", { class: "pol-key" }, t.key),
        b.tracks.length > 1 ? removeBtn("Remove", () => { b.tracks.splice(b.tracks.indexOf(t), 1); delete b.ladders[t.key]; redraw(); redrawLadders(); }) : null)),
      addBtn("Add a point total", () => {
        const t = { key: keyFor("total", new Set(b.tracks.map((x) => x.key))), name: "" };
        ed.fresh.add(t);
        b.tracks.push(t);
        b.ladders[t.key] = [];
        redraw();
        redrawLadders();
      })]);

    // ------------ steps
    const steps = section("Steps", "From the lowest to the highest. Each stays active for a while after it's issued (a new incident then can escalate), and its write-up's end date can end the points behind it. HR only: like suspensions, the employee sees nothing until HR acts.", (redraw) => {
      const ordered = [...b.steps].sort((x, y) => x.rank - y.rank);
      const renumber = (list) => list.forEach((s, i) => { s.rank = i + 1; });
      return [ordered.map((s, i) => h("div", { class: "pol-row" },
        field("Name", nameIn(s, b.steps, "step", "Step name", () => redrawLadders()), null, "wide"),
        field("Stays active for", spanIn(s, "active", "Stays active for")),
        h("div", { class: "pol-checks" },
          checkIn(s, "hr_meeting", "HR meeting"),
          checkIn(s, "hr_only", "HR only"),
          checkIn(s, "termination", "Termination review"),
          b.approval.writeups === "per_step" ? checkIn(s, "hr_approval", "HR approves its write-ups") : null),
        editable ? h("div", { class: "pol-move" },
          h("button", { type: "button", "aria-label": `Move ${s.name} down`, disabled: i === 0, onclick: () => {
            [ordered[i - 1], ordered[i]] = [ordered[i], ordered[i - 1]]; renumber(ordered); touch(); redraw(); redrawLadders();
          } }, "↑"),
          h("button", { type: "button", "aria-label": `Move ${s.name} up`, disabled: i === ordered.length - 1, onclick: () => {
            [ordered[i + 1], ordered[i]] = [ordered[i], ordered[i + 1]]; renumber(ordered); touch(); redraw(); redrawLadders();
          } }, "↓")) : null,
        removeBtn("Remove", () => { b.steps.splice(b.steps.indexOf(s), 1); renumber([...b.steps].sort((x, y) => x.rank - y.rank)); redraw(); redrawLadders(); }))),
      addBtn("Add a step", () => {
        const s = { key: keyFor("step", new Set(b.steps.map((x) => x.key))), name: "", rank: b.steps.length + 1, active: { months: 6 },
          hr_meeting: false, hr_only: false, termination: false, hr_approval: true };
        ed.fresh.add(s);
        b.steps.push(s);
        redraw();
      })];
    });

    // ------------ ladders, as threshold bars
    // A ladder's threshold bar over its rows (points, then the step they lead to). Typing
    // redraws only the bar and the swatches, so the field keeps its focus.
    function ladderEditor(ladders, track, label, emptyNote) {
      ladders[track] ||= [];
      const rungs = ladders[track];
      const box = h("div");
      const barHolder = h("div");
      const swatches = new Map();
      const refresh = () => {
        const sorted = [...rungs].filter((r) => Number(r.points) > 0).sort((x, y) => x.points - y.points);
        fill(barHolder, ladderBar(rungs, stepName));
        for (const [r, sw] of swatches) {
          const i = sorted.indexOf(r);
          sw.className = `sw ${i >= 0 ? shade(i, sorted.length) : "none"}`;
        }
      };
      const draw = () => {
        swatches.clear();
        fill(box, h("h3", null, label), barHolder,
          !rungs.length && emptyNote ? h("p", { class: "pol-note" }, emptyNote) : null,
          rungs.length ? h("div", { class: "rung head", "aria-hidden": "true" }, h("span"), h("span", null, "Points"), h("span", null, "lead to")) : null,
          rungs.map((r) => {
            const sw = h("span", { class: "sw", "aria-hidden": "true" });
            swatches.set(r, sw);
            return h("div", { class: "rung" }, sw,
              numIn(r, "points", `${label}: points`, { onInput: refresh }),
              selectIn(r, "step", `${label}: the step they lead to`, stepOptions(), { none: "Choose a step", onChange: refresh }),
              editable ? h("button", { type: "button", class: "linkish x", "aria-label": "Remove this threshold",
                onclick: () => { rungs.splice(rungs.indexOf(r), 1); touch(); draw(); } }, "✕") : null);
          }),
          addBtn("Add a threshold", () => {
            const top = rungs.reduce((m, r) => Math.max(m, Number(r.points) || 0), 0);
            rungs.push({ points: top + 2, step: null });
            draw();
          }));
        refresh();
      };
      draw();
      return box;
    }
    const laddersBox = h("div");
    const ladders = h("section", { class: "card" }, h("h2", null, "Ladders"),
      h("p", { class: "pol-note" }, "The point total that leads to each step. Probation policies can have lower ones, below."), laddersBox);
    function redrawLadders() {
      fill(laddersBox, ...b.tracks.map((t) => ladderEditor(b.ladders, t.key, b.tracks.length > 1 ? t.name || t.key : "Thresholds")));
    }
    redrawLadders();

    // ------------ escalation
    const escalation = section("Escalation", "What a new incident does while a step is still active.", (redraw) => [
      b.escalation.map((e) => h("div", { class: "pol-row" },
        field("While on", selectIn(e, "while", "While on", [["any", "Any step"], ...stepOptions()], { none: "Choose" }), null, "wide"),
        field("a new incident goes to", selectIn(e, "to", "Goes to", [["next", "The next step"], ...stepOptions()], { none: "Choose" }), null, "wide"),
        removeBtn("Remove", () => { b.escalation.splice(b.escalation.indexOf(e), 1); redraw(); }))),
      b.escalation.length ? null : h("p", { class: "muted" }, "None: a new incident is scored by its points alone."),
      addBtn("Add a rule", () => { b.escalation.push({ while: null, to: null }); redraw(); })]);

    // ------------ offences: an editable table
    let filter = "";
    const offences = section("Offences", "Each kind of problem and its points. A manager may choose within the range; only HR goes beyond it.", (redraw) => {
      const find = h("input", { type: "search", value: filter, placeholder: "Find an offence", "aria-label": "Find an offence" });
      const rows = b.offences.map((o) => offenceRow(o, redraw));
      find.addEventListener("input", () => {
        filter = find.value.trim().toLowerCase();
        rows.forEach((r, i) => { r.hidden = !!filter && !String(b.offences[i].name || "").toLowerCase().includes(filter); });
      });
      if (filter) rows.forEach((r, i) => { r.hidden = !String(b.offences[i].name || "").toLowerCase().includes(filter); });
      return [b.offences.length > 8 ? find : null,
        h("div", { class: "otable" }, h("div", { class: "ohead", "aria-hidden": "true" },
          h("span", null, "Offence"), h("span", null, "Points"), h("span", null, "Managers may choose"), h("span", null, "")), rows),
        addBtn("Add an offence", () => {
          const o = { key: keyFor("offence", new Set(b.offences.map((x) => x.key))), name: "", track: b.tracks[0] ? b.tracks[0].key : "",
            category: "", attendance: false, points: 1, min: 1, max: 1, serious: false, repeat: null, min_step: null,
            hr_approval: true, writeup_required: false, attachment_required: false, wording: "" };
          ed.fresh.add(o);
          b.offences.push(o);
          filter = "";
          redraw();
          const last = offences.querySelector(".offence:last-of-type .o-name input");
          if (last) last.focus();
        })];
    });

    function offenceRow(o, redraw) {
      const detail = h("div", { class: "odetail", hidden: true });
      const more = h("button", { type: "button", "aria-expanded": "false", onclick: () => {
        detail.hidden = !detail.hidden;
        more.setAttribute("aria-expanded", String(!detail.hidden));
        more.textContent = detail.hidden ? "More" : "Less";
        if (!detail.hidden) drawDetail();
      } }, "More");
      const tiny = (text) => h("span", { class: "tiny" }, text);
      const row = h("div", { class: "orow" },
        h("label", { class: "o-name" }, tiny("Offence"), nameIn(o, b.offences, "offence", "Offence name")),
        h("label", { class: "o-pts" }, tiny("Points"), numIn(o, "points", `Points for ${o.name || "this offence"}`)),
        h("label", { class: "o-lo" }, tiny("Manager min"), numIn(o, "min", "Managers may choose from")),
        h("label", { class: "o-hi" }, tiny("Manager max"), numIn(o, "max", "Managers may choose up to")),
        h("div", { class: "o-more" }, more));
      function drawDetail() {
        const repeatBox = h("div", { class: "pol-sub" });
        const drawRepeat = () => {
          const r = o.repeat;
          fill(repeatBox, h("h4", null, "Repeats"),
            !r ? h("p", { class: "pol-note" }, "No repeat rule: every one scores the same.") : [
              field("Counts as a repeat within", spanIn(r, "within", "Repeat period", { never: false })),
              (r.tiers || []).map((t) => h("div", { class: "pol-row" },
                field("The", numIn(t, "nth", "Which repeat (2 for the second)", { step: "1", min: "2" }), "occurrence (2 is the second)", "narrow"),
                field("adds", numIn(t, "add", "Extra points"), "points", "narrow"),
                field("and goes to", selectIn(t, "step", "Step for this repeat", stepOptions(), { none: "No step" })),
                removeBtn("Remove", () => { r.tiers.splice(r.tiers.indexOf(t), 1); if (!r.tiers.length) o.repeat = null; drawRepeat(); }))),
            ],
            addBtn(r ? "Add a repeat" : "Add a repeat rule", () => {
              o.repeat ||= { within: { days: 90 }, tiers: [] };
              const top = o.repeat.tiers.reduce((m, t) => Math.max(m, Number(t.nth) || 1), 1);
              o.repeat.tiers.push({ nth: top + 1, add: 1, step: null });
              drawRepeat();
            }));
        };
        drawRepeat();
        fill(detail, 
          h("div", { class: "pol-row" },
            field("Category", textIn(o, "category", "Category", { max: 40 }), "Such as Attendance or Conduct"),
            b.tracks.length > 1 ? field("Counts in", selectIn(o, "track", "Point total", b.tracks.map((t) => [t.key, t.name || t.key]), { none: "Choose" })) : null,
            field("Minimum step", selectIn(o, "min_step", "Minimum step", stepOptions(), { none: "None" }), "It goes straight to this step at least")),
          h("div", { class: "pol-checks" },
            checkIn(o, "attendance", "Attendance", { hint: "Asks whether it was protected leave" }),
            checkIn(o, "serious", "Serious", { hint: "Probation can send serious offences higher" }),
            b.approval.incidents === "per_offence" ? checkIn(o, "hr_approval", "HR approves it") : null,
            checkIn(o, "writeup_required", "Write-up required"),
            checkIn(o, "attachment_required", "Attachment required")),
          repeatBox,
          field("Handbook wording", (() => {
            const t = h("textarea", { maxlength: "2000", disabled: dis, rows: "3" }, o.wording || "");
            t.addEventListener("input", () => { o.wording = t.value; touch(); });
            return t;
          })(), "Quoted on write-ups. Plain text."),
          h("p", { class: "pol-key" }, `Key: ${o.key}`),
          editable ? h("div", { class: "actions" }, h("button", { type: "button", class: "danger", onclick: () => {
            b.offences.splice(b.offences.indexOf(o), 1);
            touch();
            redraw();
          } }, "Remove this offence")) : null);
      }
      return h("div", { class: "offence" }, row, detail);
    }

    // ------------ expiry and credits
    const expiry = section("When points stop counting", null, (redraw) => {
      const rule = (value, label, hint) => {
        const el = h("input", { type: "radio", name: `rule-${id}`, checked: b.expiry.rule === value, disabled: dis });
        el.addEventListener("change", () => { b.expiry.rule = value; touch(); redraw(); });
        return h("label", { class: "check" }, el, h("span", null, label, h("small", null, hint)));
      };
      const reset = !!b.expiry.clean_reset;
      return [
        rule("first", "Whichever comes first (recommended)", "The rolling window, or the end of the write-up the points led to."),
        rule("rolling", "After a rolling window", "From the day it happened."),
        rule("writeup", "On the write-up's end date", "Points that didn't lead to a write-up keep counting."),
        b.expiry.rule !== "writeup" ? field("Rolling window", spanIn(b.expiry, "rolling", "Rolling window", { never: false })) : null,
        checkIn({ on: reset }, "on", "Clean-period reset", { hint: "A stretch with no new incident clears them all; a new incident restarts the clock", onChange: () => {
          b.expiry.clean_reset = reset ? null : { months: 6 };
          redraw();
        } }),
        reset ? field("Clean stretch", spanIn(b.expiry, "clean_reset", "Clean stretch", { never: false })) : null,
        h("p", { class: "pol-note" }, "HR can change when any one entry stops counting."),
        h("h3", null, "Earning points back"),
        checkIn(b.credits, "on", "Points can be earned back", { onChange: redraw }),
        b.credits.on ? [h("div", { class: "pol-row" },
          field("After a clean stretch of", spanIn(b.credits, "clean", "Clean stretch for a credit", { never: false })),
          field("this many points come off", numIn(b.credits, "points", "Points earned back"), null, "narrow"),
          field("at most this many times a year", numIn(b.credits, "max_per_year", "Most credits a year", { step: "1", min: "1" }), null, "narrow")),
        b.tracks.length > 1 ? h("div", { class: "pol-checks" }, b.tracks.map((t) => {
          const el = h("input", { type: "checkbox", checked: (b.credits.tracks || []).includes(t.key), disabled: dis });
          el.addEventListener("change", () => {
            const set = new Set(b.credits.tracks || []);
            if (el.checked) set.add(t.key); else set.delete(t.key);
            b.credits.tracks = [...set];
            touch();
          });
          return h("label", { class: "check" }, el, `Only ${t.name || t.key}`);
        })) : null] : null];
    });

    // ------------ probation
    const departments = data.departments || [];
    const probation = section("Probation", "Introductory probation starts on a new hire's start date. Disciplinary probation is placed by HR. Each can have its own ladder and points.", (redraw) => [
      b.probation.map((p) => probationCard(p, redraw)),
      addBtn("Add a probation policy", () => {
        const p = { key: keyFor("probation", new Set(b.probation.map((x) => x.key))), name: "", kind: "introductory", days: 90,
          department_days: {}, departments: [], ladders: {}, multiplier: 1, overrides: {}, serious_floor: null,
          extensions: { count: 1, days: 30, max_days: 120 }, review_lead_days: 14, when_end_passes: "remind", on_pass: "keep",
          rehires: true, transfers: false };
        ed.fresh.add(p);
        b.probation.push(p);
        redraw();
      })]);

    function probationCard(p, redrawAll) {
      const box = h("div", { class: "pol-sub" });
      const deptName = (g) => (departments.find((d) => d.group === g) || {}).name || `Group ${g}`;
      const draw = () => {
        const overrides = Object.entries(p.overrides || {});
        fill(box, 
          h("div", { class: "pol-row" }, field("Name", nameIn(p, b.probation, "probation", "Probation name"), "Such as Introductory period", "wide"),
            field("Kind", selectIn(p, "kind", "Kind", [["introductory", "Introductory (new hires)"], ["disciplinary", "Disciplinary (placed by HR)"]])),
            field("Days", numIn(p, "days", "Days", { step: "1", min: "1" }), "Calendar days", "narrow")),
          h("h4", null, "Departments"),
          departments.length ? h("div", { class: "pol-checks" }, departments.map((d) => {
            const el = h("input", { type: "checkbox", checked: (p.departments || []).includes(d.group), disabled: dis });
            el.addEventListener("change", () => {
              const set = new Set(p.departments || []);
              if (el.checked) set.add(d.group); else set.delete(d.group);
              p.departments = [...set];
              touch();
            });
            return h("label", { class: "check" }, el, d.name);
          })) : null,
          h("p", { class: "pol-note" }, "None ticked: every department."),
          Object.entries(p.department_days || {}).map(([g, days]) => {
            const holder = { days };
            return h("div", { class: "pol-row" }, h("span", null, deptName(g)),
              field("Days", numIn(holder, "days", `Days in ${deptName(g)}`, { step: "1", min: "1", onInput: () => { p.department_days[g] = holder.days; } }), null, "narrow"),
              removeBtn("Remove", () => { delete p.department_days[g]; draw(); }));
          }),
          editable && departments.length ? (() => {
            const pick = h("select", { "aria-label": "Department with its own length" }, h("option", { value: "" }, "A department with its own length…"),
              departments.filter((d) => !(d.group in (p.department_days || {}))).map((d) => h("option", { value: d.group }, d.name)));
            pick.addEventListener("change", () => {
              if (!pick.value) return;
              p.department_days ||= {};
              p.department_days[pick.value] = p.days;
              touch();
              draw();
            });
            return pick;
          })() : null,
          h("div", { class: "pol-row" },
            field("Points ×", numIn(p, "multiplier", "Points multiplier", { step: "0.05", min: "0.25", max: "5" }), "1 for none", "narrow"),
            field("A serious offence goes to", selectIn(p, "serious_floor", "Serious offences go to", stepOptions(), { none: "Nothing extra" }))),
          h("h4", null, "Points of its own for some offences"),
          overrides.map(([k, v]) => h("div", { class: "pol-row" },
            h("span", { class: "wide" }, (b.offences.find((o) => o.key === k) || {}).name || k),
            field("Points", numIn(v, "points", "Points"), null, "narrow"),
            field("Min", numIn(v, "min", "Manager min", { blank: true }), null, "narrow"),
            field("Max", numIn(v, "max", "Manager max", { blank: true }), null, "narrow"),
            removeBtn("Remove", () => { delete p.overrides[k]; draw(); }))),
          editable ? (() => {
            const pick = h("select", { "aria-label": "Offence with its own points" }, h("option", { value: "" }, "An offence with its own points…"),
              b.offences.filter((o) => !(o.key in (p.overrides || {}))).map((o) => h("option", { value: o.key }, o.name || o.key)));
            pick.addEventListener("change", () => {
              if (!pick.value) return;
              const o = b.offences.find((x) => x.key === pick.value);
              p.overrides ||= {};
              p.overrides[pick.value] = { points: o.points, min: null, max: null };
              touch();
              draw();
            });
            return pick;
          })() : null,
          h("p", { class: "pol-note" }, "Offences listed here use these points instead of the multiplier. Without a min and max, managers can't change them."),
          h("h4", null, "Extensions and the end"),
          h("div", { class: "pol-row" },
            field("Extensions", numIn(p.extensions, "count", "How many extensions", { step: "1" }), null, "narrow"),
            field("of days", numIn(p.extensions, "days", "Days per extension", { step: "1", min: "1" }), null, "narrow"),
            field("at most days in all", numIn(p.extensions, "max_days", "Most days in total", { step: "1", min: "1" }), null, "narrow"),
            field("Review due days before the end", numIn(p, "review_lead_days", "Review due days before the end", { step: "1" }), null, "narrow")),
          field("If the end passes with no decision", selectIn(p, "when_end_passes", "When the end passes", [["remind", "It stays open and reminds HR daily"], ["pass", "They pass"]])),
          field("On passing", selectIn(p, "on_pass", "On passing", [["keep", "Points are kept and expire as usual"], ["clear", "Points are cleared (write-ups stay)"]])),
          h("div", { class: "pol-checks" }, checkIn(p, "rehires", "Applies to rehires"), checkIn(p, "transfers", "Applies to transfers")),
          b.tracks.map((t) => ladderEditor(p.ladders, t.key, `Its ladder${b.tracks.length > 1 ? `: ${t.name || t.key}` : ""}`,
            "No thresholds: the usual ladder applies.")),
          editable ? h("div", { class: "actions" }, h("button", { type: "button", class: "danger", onclick: () => {
            b.probation.splice(b.probation.indexOf(p), 1);
            touch();
            redrawAll();
          } }, "Remove this probation policy")) : null);
      };
      draw();
      return box;
    }

    // ------------ visibility, approval, protected leave
    const visibility = section("What employees and managers see", null, () => [
      checkIn(b.visibility, "employee_sees_total", "Employees see their point total"),
      checkIn(b.visibility, "employee_sees_expired", "Employees see points that stopped counting"),
      checkIn(b.visibility, "manager_sees_expired", "Managers see points that stopped counting"),
      checkIn(b.visibility, "employee_sees_probation_end", "Employees see their probation end date")]);
    const approval = section("What HR approves", "Termination reviews and suspensions always go to HR.", (redraw) => [
      field("Incidents", selectIn(b.approval, "incidents", "Incidents HR approves", [["all", "Every incident waits for HR before it counts"], ["per_offence", "Only offences marked HR approves"]], { onChange: () => offences.redraw() })),
      field("Write-ups", selectIn(b.approval, "writeups", "Write-ups HR approves", [["all", "Every write-up"], ["per_step", "Only steps marked HR approves"]], { onChange: () => steps.redraw() }))]);
    const leave = section("Protected leave", "An absence that was protected leave scores 0 points and never leads to a step. Ask your employment lawyer to confirm this list.", (redraw) => [
      b.protected_leave.map((p) => h("div", { class: "pol-row" }, field("Name", nameIn(p, b.protected_leave, "leave", "Protected leave name"), null, "wide"),
        removeBtn("Remove", () => { b.protected_leave.splice(b.protected_leave.indexOf(p), 1); redraw(); }))),
      addBtn("Add protected leave", () => {
        const p = { key: keyFor("leave", new Set(b.protected_leave.map((x) => x.key))), name: "" };
        ed.fresh.add(p);
        b.protected_leave.push(p);
        redraw();
      })]);

    show(header, basics, steps, ladders, escalation, offences, expiry, probation, visibility, approval, leave);
  }

  // ---------------------------------------------------------------- the simulator

  async function showSimulator(main, id) {
    const data = await api("GET", `/api/policy/${id}`);
    const b = drafts[id] ? drafts[id].body : data.body;
    const state = sims[id] ||= { start: "", department: "", events: [] };
    if (!state.start) {
      const t = new Date(data.today + "T00:00:00");
      t.setMonth(t.getMonth() - 6);
      state.start = `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`;
    }
    const offences = b.offences.map((o) => [o.key, o.name || o.key]);
    const results = h("div");
    const list = h("div");

    function eventRow(ev) {
      const o = () => b.offences.find((x) => x.key === ev.offence);
      const box = h("div", { class: "sim-ev" });
      const input = (key, attrs, label) => {
        const el = h("input", { ...attrs, value: ev[key] ?? "", "aria-label": label });
        el.addEventListener("input", () => { ev[key] = el.value === "" ? null : (attrs.type === "number" ? Number(el.value) : el.value); });
        return el;
      };
      const draw = () => {
        if (ev.kind === "adjust") {
          fill(box, h("div", { class: "pol-row" },
            h("label", null, "HR adds points on", input("date", { type: "date" }, "Day")),
            h("label", { class: "narrow" }, "Points", h("small", null, "Minus takes off"), input("points", { type: "number", step: "0.25", inputmode: "decimal" }, "Points")),
            h("label", null, "Until", h("small", null, "Optional"), input("ends", { type: "date" }, "Until")),
            b.tracks.length > 1 ? (() => {
              const s = h("select", { "aria-label": "Point total" }, b.tracks.map((t) => h("option", { value: t.key, selected: ev.track === t.key }, t.name)));
              s.addEventListener("change", () => { ev.track = s.value; });
              ev.track ||= b.tracks[0].key;
              return h("label", null, "Total", s);
            })() : null,
            h("button", { type: "button", class: "linkish", onclick: () => { state.events.splice(state.events.indexOf(ev), 1); drawList(); } }, "Remove")));
          return;
        }
        const pick = h("select", { "aria-label": "Offence" }, h("option", { value: "" }, "Choose an offence"),
          offences.map(([k, n]) => h("option", { value: k, selected: ev.offence === k }, n)));
        pick.addEventListener("change", () => { ev.offence = pick.value; draw(); });
        const protectedPick = o() && o().attendance ? (() => {
          const s = h("select", { "aria-label": "Was it protected leave?" }, h("option", { value: "" }, "Not protected leave"),
            b.protected_leave.map((p) => h("option", { value: p.key, selected: ev.protected === p.key }, p.name)));
          s.addEventListener("change", () => { ev.protected = s.value || null; });
          return h("label", null, "Protected leave?", s);
        })() : null;
        fill(box, h("div", { class: "pol-row" },
          h("label", null, "Happened on", input("date", { type: "date" }, "Happened on")),
          h("label", { class: "wide" }, "Offence", pick), protectedPick,
          h("label", { class: "narrow" }, "Manager's points", h("small", null, "Optional"), input("manager_points", { type: "number", step: "0.25", min: "0", inputmode: "decimal" }, "Manager's points")),
          h("label", { class: "narrow" }, "HR's points", h("small", null, "Optional"), input("hr_points", { type: "number", step: "0.25", min: "0", inputmode: "decimal" }, "HR's points")),
          h("label", null, "HR approved on", h("small", null, "Optional: the day it counts from"), input("approved", { type: "date" }, "HR approved on")),
          h("button", { type: "button", class: "linkish", onclick: () => { state.events.splice(state.events.indexOf(ev), 1); drawList(); } }, "Remove")));
      };
      draw();
      return box;
    }
    function drawList() {
      fill(list, state.events.length ? state.events.map(eventRow)
        : h("p", { class: "muted" }, "No made-up incidents yet."));
    }
    drawList();
    const lastDate = () => {
      const dates = state.events.map((e) => e.date).filter(Boolean).sort();
      return dates.at(-1) || state.start;
    };
    const start = h("input", { type: "date", value: state.start, required: true });
    start.addEventListener("input", () => { state.start = start.value; });
    const dept = h("select", null, h("option", { value: "" }, "Any department"),
      (data.departments || []).map((d) => h("option", { value: d.group, selected: state.department === d.group }, d.name)));
    dept.addEventListener("change", () => { state.department = dept.value; });

    async function simulate() {
      const body = { start: state.start, department: state.department || null,
        events: state.events.map((e) => ({ ...e })), ...(data.version.status === "draft" ? { body: b } : {}) };
      const r = await run(() => api("POST", `/api/policy/${id}/simulate`, body));
      if (!r) return;
      drawResults(r);
    }

    function drawResults(r) {
      if (r.problems && r.problems.length) {
        return fill(results, h("section", { class: "card" }, h("h2", null, "It can't run yet"),
          h("ul", { class: "pol-problems" }, r.problems.map((p) => h("li", null, p)))));
      }
      const chartBox = h("div", { class: "sim-chart" });
      const tracks = b.tracks.map((t) => t.key);
      const legend = tracks.length > 1 ? h("div", { class: "viz-legend" }, b.tracks.map((t, i) =>
        h("span", null, h("span", { class: `key k${(i % 6) + 1}` }), t.name))) : null;
      const totals = Object.entries(r.totals).map(([t, v]) => `${(b.tracks.find((x) => x.key === t) || {}).name || t}: ${pts(v)}`).join(", ");
      fill(results, h("section", { class: "card" },
        h("h2", null, tracks.length > 1 ? "Points over time" : `${(b.tracks[0] || {}).name || "Points"} points over time`),
        legend, chartBox,
        h("p", { class: "muted small" }, `On ${day(r.until)}: ${totals}. Each step is treated as issued the day its incident counts, and covers the points counting then.`)),
      h("section", { class: "card" }, h("h2", null, "Timeline"), timeline(r)));
      const draw = () => fill(chartBox, simChart(r, chartBox.clientWidth || 340));
      draw();
      let timer = null;
      const onResize = () => { clearTimeout(timer); timer = setTimeout(() => { if (chartBox.isConnected) draw(); else window.removeEventListener("resize", onResize); }, 150); };
      window.addEventListener("resize", onResize);
    }

    function rowText(r) {
      const name = (k) => (b.steps.find((s) => s.key === k) || {}).name || k;
      if (r.kind === "incident") {
        const bits = [`${r.name || r.offence}: ${pts(r.points)}`];
        if (r.protected) bits.push("(protected leave)");
        if (r.points !== r.default && !r.protected) bits.push(`(the rules give ${num(r.default)})`);
        return bits.join(" ");
      }
      if (r.kind === "adjust") return r.points > 0 ? `HR added ${pts(r.points)}` : `HR took off ${pts(-r.points)}`;
      if (r.kind === "drop") return `${pts(-r.points)} stopped counting`;
      if (r.kind === "restore") return `${pts(r.points)} count again`;
      if (r.kind === "step_ends") return `${name(r.step)} is no longer active`;
      return r.text || "";
    }

    function timeline(r) {
      return h("ul", { class: "timeline" }, r.rows.map((row) => {
        const quietRow = ["probation", "step_ends"].includes(row.kind);
        const why = [];
        if (row.errors && row.errors.length) why.push(h("div", { class: "error" }, row.errors.join(" ")));
        if (row.notes && row.notes.length) why.push(h("div", null, row.notes.join(" ")));
        if (row.on_probation) why.push(h("div", null, `During ${(b.probation.find((p) => p.key === row.on_probation) || {}).name || "probation"}.`));
        if (row.kind === "incident" && row.step) {
          const s = b.steps.find((x) => x.key === row.step) || {};
          why.push(h("div", null, h("span", { class: `chip${s.hr_only || s.termination ? " hr" : ""}` }, `Step due: ${row.step_name}`), row.reason));
        } else if (row.kind === "incident" && row.reason && !row.errors.length && !row.protected) {
          why.push(h("div", null, row.reason));
        }
        return h("li", { class: quietRow ? "quiet" : "" }, h("span", { class: "when" }, day(row.date)),
          h("span", { class: "what" }, rowText(row)),
          h("span", { class: "tot" }, row.total != null ? ["Total ", h("strong", null, num(row.total))] : ""),
          why.length ? h("div", { class: "why" }, why) : null);
      }));
    }

    // Points over time: one line per point total, the thresholds as dashed lines, probation as
    // a wash, a dot for each incident and a label where a step falls due. Hover or arrow keys
    // move a crosshair through the days something changed.
    function simChart(r, width) {
      const W = Math.max(280, Math.round(width));
      const H2 = 230;
      const m = { l: 30, r: 12, t: 22, b: 26 };
      const dnum = (iso) => { const [y, mo, d] = iso.split("-").map(Number); return Date.UTC(y, mo - 1, d) / 864e5; };
      const rows = r.rows;
      const from = Math.min(dnum(state.start), ...rows.map((x) => dnum(x.date)));
      const to = Math.max(dnum(r.until), from + 30);
      const tracks = b.tracks.map((t) => t.key);
      const ladder = tracks.length === 1 ? (b.ladders[tracks[0]] || []).filter((x) => Number(x.points) > 0) : [];
      const series = Object.fromEntries(tracks.map((t) => [t, [[from, 0]]]));
      for (const row of rows) if (row.total != null && series[row.track]) series[row.track].push([dnum(row.date), row.total]);
      const most = Math.max(1, ...Object.values(series).flat().map((p) => p[1]), ...ladder.map((x) => Number(x.points)));
      const stepY = most <= 6 ? 1 : most <= 14 ? 2 : most <= 30 ? 5 : 10;
      const yMax = Math.ceil((most + stepY * 0.5) / stepY) * stepY;
      const X = (d) => m.l + ((d - from) / (to - from)) * (W - m.l - m.r);
      const Y = (v) => m.t + (1 - v / yMax) * (H2 - m.t - m.b);
      const root = svg("svg", { viewBox: `0 0 ${W} ${H2}`, width: W, height: H2, role: "img", tabindex: "0",
        "aria-label": "Points over time. The timeline below lists every change." });
      // probation as a wash
      for (let i = 0; i < rows.length; i += 1) {
        if (rows[i].kind !== "probation" || !/starts$/.test(rows[i].text || "")) continue;
        const endRow = rows.slice(i + 1).find((x) => x.kind === "probation" && /^Last day/.test(x.text || ""));
        const x1 = X(dnum(rows[i].date));
        const x2 = X(Math.min(to, endRow ? dnum(endRow.date) + 1 : to));
        add(root, svg("rect", { class: "wash", x: x1, y: m.t, width: Math.max(0, x2 - x1), height: H2 - m.t - m.b }),
          svg("text", { class: "lbl", x: x1 + 4, y: m.t - 8 }, "Probation"));
      }
      for (let v = 0; v <= yMax; v += stepY) {
        add(root, svg("line", { class: v === 0 ? "axis" : "grid", x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }),
          svg("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, num(v)));
      }
      // month ticks
      const first = new Date(from * 864e5);
      const months = Math.max(1, Math.round((to - from) / 30.4));
      const every = Math.max(1, Math.ceil(months / Math.max(2, Math.floor((W - m.l - m.r) / 80))));
      for (let k = 0; k <= months + 1; k += every) {
        const t = Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + k + 1, 1) / 864e5;
        if (t > to) break;
        const label = new Date(t * 864e5).toLocaleDateString([], { month: "short", year: "numeric", timeZone: "UTC" });
        add(root, svg("text", { x: X(t), y: H2 - 8, "text-anchor": "middle" }, label));
      }
      for (const rung of ladder) {
        const y = Y(Number(rung.points));
        add(root, svg("line", { class: "thr", x1: m.l, x2: W - m.r, y1: y, y2: y }),
          svg("text", { class: "lbl", x: W - m.r, y: y - 4, "text-anchor": "end" },
            `${(b.steps.find((s) => s.key === rung.step) || {}).name || ""} ${num(rung.points)}`));
      }
      tracks.forEach((t, i) => {
        const pts_ = series[t];
        let d = `M${X(pts_[0][0])},${Y(pts_[0][1])}`;
        for (const [x, v] of pts_.slice(1)) d += `H${X(x)}V${Y(v)}`;
        d += `H${X(to)}`;
        add(root, svg("path", { class: `line s${(i % 6) + 1}`, d }));
      });
      let lastLabel = -1e9;
      for (const row of rows) {
        if (row.kind !== "incident" || row.total == null) continue;
        const i = Math.max(0, tracks.indexOf(row.track));
        const x = X(dnum(row.approved || row.date));
        const y = Y(row.total);
        add(root, svg("circle", { class: `dot f${(i % 6) + 1}`, cx: x, cy: y, r: 4 }));
        const half = (row.step_name || "").length * 3.3;      // about half the label's width
        if (row.step && x - lastLabel > half * 2 + 12) {
          lastLabel = x;
          add(root, svg("text", { class: "step", x: Math.min(Math.max(x, m.l + half + 4), W - m.r - half), y: Math.max(y - 9, 11), "text-anchor": "middle" }, row.step_name));
        }
      }
      // crosshair and tooltip
      const days = [...new Set(rows.map((x) => x.date))].sort();
      const cross = svg("line", { class: "cross", y1: m.t, y2: H2 - m.b, visibility: "hidden" });
      const hit = svg("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H2, fill: "transparent" });
      add(root, cross, hit);
      let at = -1;
      const totalOn = (t, iso) => { let v = 0; for (const [x, val] of series[t]) if (x <= dnum(iso)) v = val; return v; };
      const point = (idx) => {
        at = Math.max(0, Math.min(days.length - 1, idx));
        const iso = days[at];
        const x = X(dnum(iso));
        cross.setAttribute("x1", x);
        cross.setAttribute("x2", x);
        cross.setAttribute("visibility", "visible");
        const box = root.getBoundingClientRect();
        const lines = [{ text: day(iso), cls: "d" },
          ...tracks.map((t) => ({ text: `${pts(totalOn(t, iso))}${tracks.length > 1 ? ` · ${(b.tracks.find((x2) => x2.key === t) || {}).name}` : ""}`, cls: "v" })),
          ...rows.filter((x2) => x2.date === iso).map((x2) => ({ text: rowText(x2) + (x2.step ? `. Step due: ${x2.step_name}` : "") }))];
        showTip(box.left + window.scrollX + (x / W) * box.width, box.top + window.scrollY + (m.t / H2) * box.height, lines);
      };
      const nearest = (clientX) => {
        const box = root.getBoundingClientRect();
        const x = ((clientX - box.left) / box.width) * W;
        let best = 0;
        days.forEach((iso, i) => { if (Math.abs(X(dnum(iso)) - x) < Math.abs(X(dnum(days[best])) - x)) best = i; });
        return best;
      };
      const away = () => { cross.setAttribute("visibility", "hidden"); hideTip(); };
      hit.addEventListener("pointermove", (e) => point(nearest(e.clientX)));
      // A tap on a phone shows the readout until the next tap somewhere else.
      hit.addEventListener("pointerdown", (e) => point(nearest(e.clientX)));
      hit.addEventListener("pointerleave", (e) => { if (e.pointerType !== "touch") away(); });
      document.addEventListener("pointerdown", (e) => { if (e.target !== hit) away(); }, { capture: true });
      root.addEventListener("keydown", (e) => {
        if (!days.length || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
        e.preventDefault();
        point(e.key === "Home" ? 0 : e.key === "End" ? days.length - 1 : at + (e.key === "ArrowRight" ? 1 : -1));
      });
      root.addEventListener("blur", away);
      return root;
    }

    show(h("section", { class: "card" }, h("h1", null, "Simulator"),
      h("p", { class: "muted" }, `${data.body.name}${drafts[id] ? " (with your unsaved changes)" : ""}. Made-up incidents for a test person: nothing here is saved or reaches anyone's file.`),
      h("div", { class: "row" }, h("label", null, "Their start date", start), h("label", null, "Department", dept)),
      h("h2", null, "Made-up incidents"), list,
      h("div", { class: "actions" },
        h("button", { type: "button", onclick: () => { state.events.push({ kind: "incident", date: lastDate(), offence: "" }); drawList(); } }, "Add an incident"),
        h("button", { type: "button", onclick: () => { state.events.push({ kind: "adjust", date: lastDate(), points: 1 }); drawList(); } }, "Add HR points"),
        h("button", { type: "button", class: "primary", onclick: simulate }, "Run")),
      h("p", null, h("a", { href: `#/policy/${id}` }, "Back to the point system"))), results);
  }

  // ---------------------------------------------------------------- import

  async function showImport() {
    const data = await api("GET", "/api/policy");
    const sample = data.versions.find((v) => v.source === "sample") || data.versions.at(-1);
    const text = h("textarea", { rows: "8", "aria-label": "The spreadsheet", placeholder: "Paste the spreadsheet here, or choose its file" });
    const file = h("input", { type: "file", accept: ".csv,.tsv,.txt,text/csv,text/plain" });
    file.addEventListener("change", () => {
      const f = file.files && file.files[0];
      if (!f) return;
      if (f.size > 512 * 1024) return toast("That file is too big: a point system is well under 500 KB.", true);
      const reader = new FileReader();
      reader.onload = () => { text.value = String(reader.result || ""); };
      reader.readAsText(f);
    });
    const name = h("input", { type: "text", maxlength: "80", placeholder: "Such as Hot Lake point system 2026" });
    const out = h("div");
    show(h("section", { class: "card" }, h("h1", null, "Import a point system"),
      h("p", null, "The spreadsheet has sections (Settings, Point totals, Steps, Ladder, Escalation, Offences, Repeats, Probation and Protected leave), each a title row, then a header row, then one row per item. It's loaded as a draft; nothing changes until you publish it."),
      sample ? h("p", null, h("button", { type: "button", class: "linkish", onclick: () => run(async () => {
        const r = await api("GET", `/api/policy/${sample.id}/export`);
        download(r.filename, r.csv);
      }) }, "Download the sample as a spreadsheet to see the layout")) : null,
      h("label", null, "File", file), h("label", null, "Or paste it", h("small", null, "Copied from a spreadsheet is fine"), text),
      h("label", null, "Name", h("small", null, "Optional"), name),
      h("div", { class: "actions" }, h("button", { type: "button", class: "primary", onclick: () => run(async () => {
        if (!text.value.trim()) throw new Error("Choose the file, or paste the spreadsheet.");
        const r = await api("POST", "/api/policy/import", { csv: text.value, name: name.value });
        fill(out, h("section", { class: "card" }, h("h2", null, "Loaded as a draft"),
          r.questions.length ? [h("p", null, "Please check these: we had to guess."), h("ul", { class: "qs" }, r.questions.map((q) => h("li", null, q)))]
            : h("p", { class: "pol-ok" }, "Nothing was unclear."),
          h("div", { class: "actions" }, h("a", { class: "button primary", href: `#/policy/${r.id}` }, "Open the draft"))));
      }, "Imported.") }, "Import as a draft")),
      h("p", null, h("a", { href: "#/policy" }, "All versions"))), out);
  }

  // ---------------------------------------------------------------- re-scoring

  async function showRescore() {
    const data = await api("GET", "/api/policy/rescore");
    if (!data.version) return show(h("section", { class: "card" }, h("h1", null, "Re-score people"), h("p", { class: "warn" }, data.message)));
    const chosen = new Set();
    const out = h("div");
    const boxes = data.people.map((p) => {
      const el = h("input", { type: "checkbox" });
      el.addEventListener("change", () => { if (el.checked) chosen.add(p.id); else chosen.delete(p.id); });
      return h("label", { class: "check" }, el, h("span", null, p.name, h("small", null, `${plural(p.incidents, "incident")} under an earlier version`)));
    });
    const all = h("button", { type: "button", class: "linkish", onclick: () => {
      for (const b of boxes) { const el = b.querySelector("input"); el.checked = true; }
      data.people.forEach((p) => chosen.add(p.id));
    } }, "Choose everyone");
    async function preview() {
      if (!chosen.size) return toast("Choose who to re-score.", true);
      const r = await run(() => api("POST", "/api/policy/rescore/preview", { people: [...chosen] }));
      if (!r) return;
      const reason = h("textarea", { rows: "2", maxlength: "500", placeholder: "Such as: the 2027 handbook lowered late arrivals to 0.5" });
      fill(out, h("section", { class: "card" }, h("h2", null, "Preview"),
        h("p", null, r.changed ? `${plural(r.changed, "incident")} would change.` : "Nothing would change."),
        r.people.map((p) => [h("h3", null, p.name),
          h("p", { class: "muted small" }, Object.keys(p.before).map((t) => `${t}: ${num(p.before[t])} → ${num(p.after[t] ?? 0)} points`).join(", ")),
          h("ul", { class: "list" }, p.changes.map((c) => h("li", null, h("div", { class: "item" },
            h("div", { class: "title" }, `${c.name} on ${day(c.happened)}: ${num(c.old)} → ${num(c.new)}`),
            c.old_ends !== c.new_ends ? h("div", { class: "sub" }, `Stops counting ${c.old_ends ? day(c.old_ends) : "when a write-up ends"} → ${c.new_ends ? day(c.new_ends) : "when a write-up ends"}`) : null,
            c.change ? null : h("div", { class: "sub" }, "No change"),
            c.notes.map((n) => h("div", { class: "sub" }, n))))))]),
        r.changed ? [h("label", null, "Reason", h("small", null, "Kept with each change"), reason),
          h("div", { class: "actions" }, h("button", { type: "button", class: "primary", onclick: async () => {
            const done = await run(() => api("POST", "/api/policy/rescore", { people: [...chosen], reason: reason.value }), "Re-scored.");
            if (done) await showRescore();
          } }, "Re-score them"))] : null));
    }
    show(h("section", { class: "card" }, h("h1", null, "Re-score people"),
      h("p", null, `Score these people's counting incidents again under ${data.version.name}, the version in use. Each change is added as an adjustment, so the original stays visible. You see a preview first.`),
      data.people.length ? [all, h("div", null, boxes), h("div", { class: "actions" }, h("button", { type: "button", class: "primary", onclick: preview }, "Preview"))]
        : h("p", { class: "muted" }, "Everyone's counting incidents are already scored under this version (your own file is for another HR admin)."),
      h("p", null, h("a", { href: "#/policy" }, "All versions"))), out);
  }

  // ---------------------------------------------------------------- registering

  H.registerPage("policy", {
    nav: "Point system",
    async render(main, parts) {
      const hr = H.me && H.me.hr;
      const [first, second] = parts;
      if (hr && !first) return showSetup(main);
      if (hr && first === "import") return showImport();
      if (hr && first === "rescore") return showRescore();
      if (hr && /^\d+$/.test(first || "")) return second === "simulate" ? showSimulator(main, first) : showEditor(main, first);
      return showRead(main, await api("GET", "/api/policy/current"));
    },
  });

  H.registerSettings("discipline", (box, data) => {
    add(box, h("h2", null, "Discipline"),
      data.set_up ? h("p", null, `Point system in use: ${data.current.name}, since ${day(data.current.effective)}.`)
        : h("p", { class: "warn" }, data.message || NOT_SET_UP),
      data.scheduled.map((v) => h("p", { class: "muted small" }, `${v.name}: ${v.label}.`)),
      data.drafts ? h("p", { class: "muted small" }, `${plural(data.drafts, "draft")} not in use.`) : null,
      h("div", { class: "actions" }, h("a", { class: "button", href: "#/policy" }, "Discipline setup")));
  });
})();
