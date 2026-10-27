// HR portal: discipline and probation (hr/discipline.py, hr/probation.py). Plain script under the
// page's Content-Security-Policy: no inline handlers, and everything shown is plain text (h() makes
// text nodes, never HTML). Adds the Discipline and Probation sections of a person's page, the
// incident form with its live preview, incident and write-up pages (with signing in person and in
// the portal), the manager's "My team", HR's discipline queue, and HR settings for this part.
(() => {
  "use strict";

  const H = window.HRPortal;
  if (!H) return;
  const { h, api, toast, openDialog, confirmDialog, show } = H;

  // Its own stylesheet next to hr.css (the page's policy allows styles from this address).
  const version = ((document.currentScript && document.currentScript.src) || "").match(/[?&]v=([\w-]+)/);
  document.head.append(h("link", { rel: "stylesheet", href: `/static/hr-discipline.css${version ? `?v=${version[1]}` : ""}` }));

  // ---------------------------------------------------------------- small helpers

  const pts = (n) => String(Math.round(Number(n || 0) * 100) / 100);
  const plural = (n, word) => `${pts(n)} ${word}${Number(n) === 1 ? "" : "s"}`;
  const total = (points) => Object.values(points || {}).reduce((a, b) => a + Number(b || 0), 0);
  const firstName = (name) => String(name || "").split(" ")[0];
  const day = (iso) => {
    const m = String(iso || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
    return m ? new Date(+m[1], +m[2] - 1, +m[3]).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" }) : "";
  };
  // Dates default to today in HR's time zone, as the add-on counts points (the section's
  // `today`), never the device's date: in another time zone, or with a clock set to UTC, that's
  // tomorrow in the evening, and added points wouldn't count until then.
  // Errors the portal already handled (signed out, moved to another page, a code not typed).
  const quiet = (err) => ["Signed out", "Moved on", "Not confirmed."].includes(err && err.message);

  async function act(fn, okText) {
    try {
      const r = await fn();
      toast((r && r.message) || okText || "Done.");
      return r || true;
    } catch (err) {
      if (!quiet(err)) toast(err.message, true);
      return false;
    }
  }

  // Append only real nodes: the DOM's own append would write "null" for a missing one.
  const put = (el, ...nodes) => el.append(...nodes.flat().filter((n) => n != null && n !== false));
  const badge = (text, cls = "") => h("span", { class: `badge ${cls}`.trim() }, text);
  const STATE_CLASS = { draft: "dim", with_hr: "warn", approved: "ok", delivered: "warn", active: "ok", expired: "dim", voided: "bad", returned: "warn" };

  function rows(list) {
    const items = list.filter(([, v]) => v !== null && v !== undefined && v !== "");
    return items.length ? h("dl", { class: "rows" }, items.flatMap(([k, v]) => [h("dt", null, k), h("dd", null, v)])) : null;
  }

  function block(label, text, note) {
    return text ? h("div", { class: "text-block" }, h("h3", null, label, note ? h("small", { class: "muted" }, ` (${note})`) : null),
      h("p", { class: "plain" }, text)) : null;
  }

  function item(href, title, sub, ...extra) {
    const inner = [h("div", { class: "title" }, title), sub ? h("div", { class: "sub" }, sub) : null, ...extra];
    return h("li", null, href ? h("a", { class: "item", href }, inner) : h("div", { class: "item" }, inner));
  }

  const list = (items, empty) => (items.length ? h("ul", { class: "list" }, items) : empty ? h("p", { class: "muted" }, empty) : null);
  const card = (...children) => h("section", { class: "card" }, ...children);
  const button = (label, onclick, cls = "") => h("button", { type: "button", class: cls, onclick }, label);
  const link = (label, href, cls = "button") => h("a", { class: cls, href }, label);

  function history(lines) {
    if (!lines || !lines.length) return null;
    return h("details", null, h("summary", null, "History"), h("ul", { class: "list lines" }, lines.map((l) =>
      h("li", null, h("div", { class: "what" }, l.what), h("div", { class: "muted small" }, [H.when(l.when), l.actor].filter(Boolean).join(" · ")),
        l.note ? h("div", { class: "small plain" }, l.note) : null))));
  }

  // ---------------------------------------------------------------- form fields and dialogs

  function field(label, input, hint) {
    return h("label", null, label, hint ? h("small", null, hint) : null, input);
  }

  function textField(name, label, { value = "", hint = null, area = false, type = "text", attrs = {} } = {}) {
    const input = area ? h("textarea", { name, rows: "4", maxlength: "4000", ...attrs })
      : h("input", { name, type, value: value ?? "", autocomplete: "off", ...attrs });
    if (area) input.value = value || "";
    return { name, node: field(label, input, hint), get: () => input.value.trim(), input };
  }

  function choiceField(name, label, options, value, hint) {
    const input = h("select", { name }, options.map(([v, l]) => h("option", { value: v, selected: v === value }, l)));
    return { name, node: field(label, input, hint), get: () => input.value, input };
  }

  function checkField(name, label, checked = false) {
    const input = h("input", { type: "checkbox", name, checked });
    return { name, node: h("label", { class: "check" }, input, label), get: () => input.checked, input };
  }

  // A dialog of fields: their values, or null when cancelled.
  async function formDialog(title, fields, okLabel = "Save", { danger = false, intro = null } = {}) {
    const nodes = [intro ? h("p", { class: "muted small plain" }, intro) : null, ...fields.map((f) => (f.node || f))].filter(Boolean);
    const pick = await openDialog(title, nodes, [{ label: "Cancel", value: "cancel" },
      { label: okLabel, value: "ok", class: danger ? "primary danger" : "primary" }]);
    if (pick !== "ok") return null;
    return Object.fromEntries(fields.filter((f) => f.name).map((f) => [f.name, f.get()]));
  }

  const reasonField = (label = "Reason") => textField("reason", label, { area: true, hint: "Required. Kept in the record." });

  // ---------------------------------------------------------------- the Discipline section

  H.registerSection("discipline", (box, s, p, again) => {
    const self = p.relation === "self";
    put(box, h("h2", null, "Discipline"));
    if (s.message && !self) put(box, h("p", { class: "warn" }, s.message));
    const tracks = Object.entries(s.points || {});
    put(box, rows([
      ["Points now", s.points ? (tracks.length > 1 ? tracks.map(([t, v]) => `${t}: ${pts(v)}`).join(", ") : pts(total(s.points))) : null],
      ["Current step", self ? null : s.step || "None"],
      ["Next drop", s.next_drop ? `${plural(s.next_drop.points, "point")} on ${day(s.next_drop.date)}` : null],
    ]));
    if (s.tasks && s.tasks.length) {
      put(box, h("h3", null, "To do"), list(s.tasks.map((t) => item(t.writeup_id ? `#/writeup/${t.writeup_id}` : t.incident_id ? `#/incident/${t.incident_id}` : null,
        t.word + (t.step ? `: ${t.step}` : ""), t.reason))));
    }
    put(box, h("h3", null, "Write-ups"), list((s.writeups || []).map(writeupItem), self ? "None." : "No write-ups."));
    if (s.incidents) {
      put(box, h("h3", null, "Incidents"), list(s.incidents.map((i) => item(`#/incident/${i.id}`, i.offence,
        [day(i.happened), plural(i.points, "point"), i.protected ? "protected leave" : null].filter(Boolean).join(" · "),
        badge(i.status_word, STATE_CLASS[i.status] || ""))), "No incidents."));
    }
    if (s.lines && s.lines.length) put(box, pointLines(s.lines, p, again));
    const buttons = [];
    if (!self && s.can && s.can.record) buttons.push(link("Record an incident", `#/record/${encodeURIComponent(p.id)}`, "button primary"));
    if (!self && s.can && s.can.as_of) {
      buttons.push(button("As of a date", async () => {
        const value = await H.ask("Points as of a date", "Show their points and records as they were on", { type: "date", value: s.today });
        if (value) location.hash = `#/discipline-history/${encodeURIComponent(p.id)}?on=${value}`;
      }));
    }
    if (s.can && s.can.adjust) buttons.push(button("Adjust points", () => adjustPoints(p, s, again)));
    if (buttons.length) put(box, h("div", { class: "actions" }, buttons));
  });

  function writeupItem(w) {
    return item(`#/writeup/${w.id}`, w.step, [w.status_word, w.issued_on && w.state !== "voided" ? `delivered ${day(w.issued_on)}` : null].filter(Boolean).join(" · "),
      w.under_appeal ? badge("Under appeal", "warn") : null);
  }

  // The points statement. For HR (today's view), each line it can undo has its own actions.
  function pointLines(lines, p = null, again = null) {
    return h("details", null, h("summary", null, "Points history"), h("ul", { class: "list" }, lines.map((l) => {
      const li = item(l.incident_id ? `#/incident/${l.incident_id}` : null, `${l.points > 0 ? "+" : ""}${pts(l.points)} · ${l.label}`,
        [day(l.date), l.ends && l.state === "counts" ? `counts until ${day(l.ends)}` : null, l.reason].filter(Boolean).join(" · "),
        l.state === "expired" ? badge(`Expired${l.ends ? ` ${day(l.ends)}` : ""}: no longer counts`, "dim")
          : l.state === "voided" ? badge("Voided", "bad") : null);
      const actions = p && l.can ? lineActions(l, p, again) : [];
      if (actions.length) put(li, h("div", { class: "actions" }, actions));
      return li;
    })));
  }

  function lineActions(l, p, again) {
    const url = `/api/people/${encodeURIComponent(p.id)}/points/${encodeURIComponent(l.id)}`;
    const found = [];
    if (l.can.end) {
      found.push(button("Change when it stops", async () => {
        const values = await formDialog("When it stops counting", [textField("ends", "First day it no longer counts", { type: "date", value: l.ends || "" }), reasonField()],
          "Save", { intro: "This always wins over the point system's expiry rules." });
        if (values && await act(() => api("POST", `${url}/ends`, values))) await again();
      }));
    }
    if (l.can.void) {
      found.push(button("Void", async () => {
        const values = await formDialog("Void this line", [reasonField()], "Void",
          { danger: true, intro: "It stops counting from today and stays on the statement as voided. Later write-ups are checked again." });
        if (values && await act(() => api("POST", `${url}/void`, values))) await again();
      }, "danger"));
    }
    return found;
  }

  async function adjustPoints(p, s, again) {
    const fields = [textField("points", "Points to add", { type: "number", attrs: { step: "0.25", min: "-100", max: "100", inputmode: "decimal" },
      hint: "Use a minus sign to take points away, like -1. Whole, half or quarter points." }),
    (s.tracks || []).length > 1 ? choiceField("track", "Point total", s.tracks.map((t) => [t, t]), s.tracks[0]) : null,
    textField("starts", "Counts from", { type: "date", value: s.today }),
    textField("ends", "Stops counting (optional)", { type: "date" }), reasonField()].filter(Boolean);
    const values = await formDialog(`Adjust ${firstName(p.name)}'s points`, fields, "Adjust");
    if (!values) return;
    if (await act(() => api("POST", `/api/people/${encodeURIComponent(p.id)}/points`, values))) await again();
  }

  // ---------------------------------------------------------------- the Probation section

  H.registerSection("probation", (box, s, p, again) => {
    put(box, h("h2", null, s.title));
    if (!s.probations.length) put(box, h("p", { class: "muted" }, "Not on probation."));
    for (const pr of s.probations) put(box, probationBlock(pr, s, p, again));
    if (s.can.place) {
      put(box, h("div", { class: "actions" }, button("Place on disciplinary probation", async () => {
        const fields = [textField("ends", "Ends on", { type: "date" }),
          s.policies.length > 1 ? choiceField("policy_key", "Rules", s.policies.map((x) => [x.key, x.name]), s.policies[0].key) : null,
          reasonField("Why")].filter(Boolean);
        const values = await formDialog(`Place ${firstName(p.name)} on disciplinary probation`, fields, "Place",
          { intro: "For example as part of a last-chance agreement. They're told, and asked to acknowledge it." });
        if (values && await act(() => api("POST", `/api/people/${encodeURIComponent(p.id)}/probation`, values))) await again();
      })));
    }
  });

  function probationBlock(pr, s, p, again) {
    const self = p.relation === "self";
    const open = pr.status === "open";
    const limits = pr.extension_limits;
    // The kind as a heading only when the section's title doesn't already say it.
    const state = badge(pr.status_word, open ? "warn" : "dim");
    const el = h("div", { class: "probation" }, pr.title === s.title ? h("p", null, state) : h("h3", null, pr.title, " ", state),
      rows([["Started", day(pr.starts)], ["Ends", pr.ends ? day(pr.ends) : null],
        ["Review due", !self && open ? day(pr.review_due) : null],
        ["Extensions", limits ? `${pr.extensions} of ${limits.count} used (each up to ${limits.days} days; ${limits.maximum} days in all)` : null],
        ["Recommended", pr.recommendation ? `${pr.recommendation.word}${pr.recommendation.to ? ` to ${day(pr.recommendation.to)}` : ""}` : null],
        ["Decided", pr.decided_on ? day(pr.decided_on) : null], ["Why HR placed it", pr.reason], ["HR's reason", pr.outcome_note]]));
    if (pr.recommendation && pr.recommendation.note) put(el, h("p", { class: "small plain" }, `Recommendation note: ${pr.recommendation.note}`));
    if (self && pr.needs_ack) {
      put(el, h("p", { class: "warn" }, "This changed. Read it, then acknowledge it. Acknowledging means you've seen it."),
        h("div", { class: "actions" }, button("Acknowledge", async () => {
          if (await act(() => api("POST", `/api/probations/${pr.id}/acknowledge`, {}))) await again();
        }, "primary")));
    }
    const buttons = [];
    if (s.can.recommend && open) buttons.push(button("Recommend an outcome", () => recommend(pr, again)));
    if (s.can.decide && open) {
      buttons.push(button("Change the end date", () => changeEnd(pr, again)), button("Decide", () => decide(pr, again), "primary"));
    }
    if (s.can.decide && pr.separation_todo) {
      buttons.push(button("Separation done", async () => {
        if (await confirmDialog("Archived them in Connecteam and finished the separation by hand?", "Done")
          && await act(() => api("POST", `/api/probations/${pr.id}/separation-done`, {}))) await again();
      }));
    }
    if (buttons.length) put(el, h("div", { class: "actions" }, buttons));
    const hist = history(pr.history);
    if (hist) put(el, hist);
    return el;
  }

  function outcomeFields(choices, withNote) {
    const outcome = choiceField("outcome", "Outcome", choices, choices[0][0]);
    const to = textField("extend_to", "New end date (for an extension)", { type: "date" });
    to.node.hidden = true;
    outcome.input.addEventListener("change", () => { to.node.hidden = outcome.get() !== "extend"; });
    return [outcome, to, withNote];
  }

  async function recommend(pr, again) {
    const values = await formDialog("Recommend an outcome", outcomeFields([["pass", "Pass"], ["extend", "Extend"], ["end", "End employment"]],
      textField("note", "Note for HR", { area: true })), "Send to HR", { intro: "HR decides. Usually this comes from the end-of-probation review." });
    if (values && await act(() => api("POST", `/api/probations/${pr.id}/recommend`, values))) await again();
  }

  async function changeEnd(pr, again) {
    const values = await formDialog("Change the end date", [textField("ends", "New end date", { type: "date", value: pr.ends }),
      checkField("leave", "For a leave of absence (doesn't count as an extension)"), reasonField()], "Save",
    { intro: `Extensions: ${pr.extensions} of ${pr.extension_limits.count} used, each up to ${pr.extension_limits.days} days, ${pr.extension_limits.maximum} days in all. The employee is asked to acknowledge it.` });
    if (values && await act(() => api("POST", `/api/probations/${pr.id}/change`, values))) await again();
  }

  async function decide(pr, again) {
    const values = await formDialog("Decide", outcomeFields([["pass", "Pass"], ["extend", "Extend"], ["end", "End employment"], ["waive", "Waive (it no longer applies)"]],
      reasonField()), "Decide", { intro: "End employment becomes a to-do: archive them in Connecteam by hand." });
    if (values && await act(() => api("POST", `/api/probations/${pr.id}/decide`, values))) await again();
  }

  // ---------------------------------------------------------------- the incident form

  H.registerPage("record", { who: (me) => me.staff, render: renderForm });

  async function renderForm(main, parts, params) {
    const pid = decodeURIComponent(parts[0] || "");
    const editId = params.get("edit");
    const form = await api("GET", `/api/people/${encodeURIComponent(pid)}/incident-form`);
    const back = `#/people/${encodeURIComponent(pid)}`;
    if (!form.set_up) return show(card(h("h1", null, "Record an incident"), h("p", { class: "warn" }, form.message), link("Back", back)));
    const editing = editId ? await api("GET", `/api/incidents/${encodeURIComponent(editId)}`) : null;
    const who = form.person;
    const byKey = Object.fromEntries(form.offences.map((o) => [o.key, o]));
    const groups = {};
    for (const o of form.offences) (groups[o.category || "Other"] ||= []).push(o);
    const offence = h("select", { name: "offence" }, h("option", { value: "" }, "Choose the offence"),
      Object.entries(groups).map(([cat, list_]) => h("optgroup", { label: cat }, list_.map((o) => h("option", { value: o.key }, o.name)))));
    const happened = h("input", { type: "date", name: "happened", value: editing ? editing.happened : form.today, min: form.earliest, max: form.today });
    const what = h("textarea", { name: "what", rows: "4", maxlength: "4000" });
    what.value = editing ? editing.what : "";
    const leave = h("select", { name: "protected" }, h("option", { value: "" }, "Choose"), form.leaves.map((l) => h("option", { value: l.key }, l.name)));
    const leaveBox = field("Was this protected leave?", leave, "Oregon law protects these absences: anything but No scores 0 and never leads to a step.");
    const points = h("input", { type: "number", name: "points", step: "0.25", inputmode: "decimal" });
    const pointsHint = h("small");
    const reason = h("textarea", { name: "reason", rows: "2", maxlength: "1000" });
    const reasonBox = field("Why these points?", reason, "Needed when you choose points other than the default.");
    const preview = h("div", { class: "preview", role: "status", "aria-live": "polite" }, h("p", { class: "muted" }, "Choose the offence to see what it means."));
    const errorBox = h("p", { class: "error", role: "alert", hidden: true });
    const wu = writeupFields(form);
    const submit = h("button", { type: "submit", class: "primary" }, editing ? "Send to HR again" : "Send to HR");

    if (editing) {
      offence.value = editing.offence_key;
      leave.value = byKey[editing.offence_key]?.attendance ? editing.protected_key : "";
      if (editing.manager_points != null) points.value = editing.manager_points;
      reason.value = editing.manager_reason || "";
    }

    const chosen = () => byKey[offence.value];
    const body = () => {
      const o = chosen();
      const data = { offence: offence.value, happened: happened.value, what: what.value, points: points.value, reason: reason.value };
      if (o && o.attendance) data.protected = leave.value;
      return data;
    };
    const layout = () => {
      const o = chosen();
      leaveBox.hidden = !(o && o.attendance);
      points.disabled = !o;
      if (o) {
        points.min = o.min;
        points.max = o.max;
        points.placeholder = pts(o.default);
        pointsHint.textContent = o.min === o.max ? `${plural(o.default, "point")}.` : `Default ${pts(o.default)}. You may choose ${pts(o.min)} to ${pts(o.max)}.`;
      }
      reasonBox.hidden = !(points.value !== "" && o && Number(points.value) !== Number(o.default));
      wu.policy.textContent = (o && o.wording) || "No handbook wording for this offence.";
    };
    let timer = null;
    let seq = 0;
    const refresh = () => {
      layout();
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const mine = ++seq;
        if (!chosen()) {
          preview.replaceChildren(h("p", { class: "muted" }, "Choose the offence to see what it means."));
          wu.box.hidden = true;
          return;
        }
        try {
          const r = await api("POST", `/api/people/${encodeURIComponent(pid)}/incident-preview`, body());
          if (mine !== seq) return;
          preview.replaceChildren();
          put(preview, h("strong", null, r.text),
            r.errors.length ? h("ul", { class: "error" }, r.errors.map((e) => h("li", null, e))) : null,
            r.repeat ? h("p", { class: "small" }, "A repeat: the repeat rule raised the points.") : null,
            r.on_probation ? h("p", { class: "small" }, "They were on probation that day, so probation rules apply.") : null,
            r.waiting ? h("p", { class: "muted small" }, `${r.waiting} other incident${r.waiting === 1 ? " is" : "s are"} still with HR and not counted yet.`) : null);
          wu.box.hidden = editing || !r.needs_writeup;
          wu.setSteps(r.step);
          submit.textContent = editing ? "Send to HR again" : r.text.includes("HR approves") ? "Send to HR" : "Record";
        } catch (err) {
          if (!quiet(err) && mine === seq) preview.replaceChildren(h("p", { class: "error" }, err.message));
        }
      }, 250);
    };
    for (const el of [offence, happened, leave, points]) el.addEventListener("change", refresh);
    points.addEventListener("input", refresh);

    const formEl = h("form", { onsubmit: async (e) => {
      e.preventDefault();
      errorBox.hidden = true;
      submit.disabled = true;
      try {
        const data = body();
        if (!wu.box.hidden) data.writeup = wu.values();
        const r = editing ? await api("PUT", `/api/incidents/${encodeURIComponent(editId)}`, data)
          : await api("POST", `/api/people/${encodeURIComponent(pid)}/incidents`, data);
        toast(r.message);
        location.hash = editing ? `#/incident/${encodeURIComponent(editId)}` : back;
      } catch (err) {
        if (!quiet(err)) {
          errorBox.textContent = err.message;
          errorBox.hidden = false;
        }
      } finally {
        submit.disabled = false;
      }
    } },
    field("Offence", offence), field("When it happened", happened),
    field("What happened", what, "Plain facts: what, where, who saw it. Their managers and HR see this."),
    leaveBox, h("label", null, "Points", pointsHint, points), reasonBox, preview, wu.box, errorBox,
    h("div", { class: "actions" }, submit, link("Cancel", editing ? `#/incident/${encodeURIComponent(editId)}` : back)));
    show(card(h("h1", null, editing ? "Change the incident" : `Record an incident for ${firstName(who.name)}`),
      editing && editing.return_note ? h("p", { class: "warn plain" }, `HR's note: ${editing.return_note}`) : null,
      h("p", { class: "muted small" }, "HR approves it before it counts or they see anything. When it reaches a step, the write-up goes with it."),
      formEl));
    layout();
    if (editing) refresh();
  }

  // The write-up part of the form, shown when the preview says a write-up is needed.
  function writeupFields(form) {
    const policy = h("p", { class: "plain quote" });
    const f = {
      improvement: textField("improvement", "Expected improvement", { area: true }),
      consequences: textField("consequences", "If it happens again", { area: true, hint: "Leave blank to say the next step." }),
      support: textField("support", "Support offered", { area: true, hint: "Training, a schedule change, someone to ask." }),
      follow_up: textField("follow_up", "Follow-up date", { type: "date" }),
      manager_note: textField("manager_note", "Note for managers and HR", { area: true, hint: "The employee never sees this." }),
    };
    const step = h("select", { name: "step" });
    const downgrade = textField("downgrade_reason", "Why a lower step?", { area: true });
    const stepBox = field("Step", step, "The step the point system calls for. To ask HR for a lower one, choose it and say why.");
    downgrade.node.hidden = true;
    let recommended = null;
    step.addEventListener("change", () => { downgrade.node.hidden = !step.value || step.value === recommended; });
    const box = h("fieldset", { hidden: true }, h("legend", null, "The write-up"),
      h("div", { class: "text-block" }, h("h3", null, "Policy", h("small", { class: "muted" }, " (quoted on the write-up)")), policy),
      Object.values(f).map((x) => x.node), stepBox, downgrade.node);
    return {
      box, policy,
      setSteps(found) {
        if (!found || found.key === recommended) return;
        recommended = found.key;
        const ranks = Object.fromEntries(form.steps.map((s) => [s.key, s.rank]));
        step.replaceChildren();
        put(step, h("option", { value: found.key }, found.name),
          form.steps.filter((s) => s.rank < ranks[found.key] && !s.hr_only).reverse().map((s) => h("option", { value: s.key }, `${s.name} (lower)`)));
        downgrade.node.hidden = true;
      },
      values() {
        const v = Object.fromEntries(Object.entries(f).map(([k, x]) => [k, x.get()]));
        if (step.value && step.value !== recommended) Object.assign(v, { step: step.value, downgrade_reason: downgrade.get() });
        return v;
      },
    };
  }

  // ---------------------------------------------------------------- an incident

  H.registerPage("incident", { who: (me) => me.staff, render: renderIncident });

  // What approving counts now: a repeat counts the incidents HR approved since it was recorded.
  function approveIntro(i) {
    const now = i.points_if_approved ?? i.final_points;
    let text = `It counts from today at ${plural(now, "point")}, and expires counting from ${day(i.happened)}.`;
    if (now !== i.final_points) text += ` It was ${plural(i.final_points, "point")} when recorded: the rules give ${pts(now)} now.`;
    if (i.earlier_waiting) text += ` ${i.earlier_waiting} earlier incident${i.earlier_waiting === 1 ? " is" : "s are"} still waiting for HR: approving ${i.earlier_waiting === 1 ? "it" : "them"} first can make this a repeat.`;
    return text;
  }

  async function renderIncident(main, parts) {
    const id = encodeURIComponent(parts[0] || "");
    const i = await api("GET", `/api/incidents/${id}`);
    const pointsText = i.points_now !== i.final_points ? `${pts(i.points_now)} (approved at ${pts(i.final_points)}, then changed by HR)`
      : pts(i.final_points);
    const buttons = [];
    if (i.can.approve) {
      buttons.push(button("Approve", async () => {
        const values = await formDialog("Approve the incident", [textField("points", "Points (leave blank to keep them)", { type: "number", attrs: { step: "0.25", min: "0", max: "100", inputmode: "decimal" } }),
          textField("reason", "Why other points?", { area: true, hint: "Only when you change the points. Changing them asks for a fresh code." })], "Approve",
        { intro: approveIntro(i) });
        if (values && await act(() => api("POST", `/api/incidents/${id}/approve`, values))) await H.route();
      }, "primary"));
    }
    if (i.can.return) {
      buttons.push(button("Return to the manager", async () => {
        const values = await formDialog("Return with a note", [textField("note", "Note for the manager", { area: true })], "Return");
        if (values && await act(() => api("POST", `/api/incidents/${id}/return`, values))) await H.route();
      }));
    }
    if (i.can.edit) buttons.push(link("Change and send again", `#/record/${encodeURIComponent(i.person.id)}?edit=${id}`, "button primary"));
    if (i.can.adjust) {
      buttons.push(button("Change its points", async () => {
        const values = await formDialog("Change its points", [textField("points", "Points it should have", { type: "number", value: pts(i.points_now), attrs: { step: "0.25", min: "0", max: "100", inputmode: "decimal" } }), reasonField()],
          "Change", { intro: "Recorded as an adjustment: the original stays visible." });
        if (values && await act(() => api("POST", `/api/incidents/${id}/adjust`, values))) await H.route();
      }));
    }
    if (i.can.void) {
      buttons.push(button("Void", async () => {
        const values = await formDialog("Void the incident", [reasonField()], "Void", { danger: true, intro: "Its points stop counting. Later write-ups are checked again; nothing else is undone." });
        if (values && await act(() => api("POST", `/api/incidents/${id}/void`, values))) await H.route();
      }, "danger"));
    }
    show(card(h("h1", null, i.offence), h("p", null, link(i.person.name, `#/people/${encodeURIComponent(i.person.id)}`, "")),
      badge(i.status_word, STATE_CLASS[i.status] || ""), i.on_probation ? badge("During probation", "warn") : null,
      i.return_note ? h("p", { class: "warn plain" }, `HR's note: ${i.return_note}`) : null,
      rows([["Happened", day(i.happened)], ["Protected leave", i.protected], ["Points", pointsText],
        ["If approved now", i.points_if_approved != null && i.points_if_approved !== i.final_points ? pts(i.points_if_approved) : null], ["Default points", pts(i.default_points)],
        ["Manager's points", i.manager_points != null ? pts(i.manager_points) : null], ["Why", i.manager_reason], ["HR's points", i.hr_points != null ? pts(i.hr_points) : null],
        ["HR's reason", i.hr_reason], ["Recorded by", i.recorded_by_name], ["Approved", i.approved_on ? `${day(i.approved_on)} by ${i.decided_by_name}` : null],
        ["Step", i.step ? `${i.step}${i.step_reason ? ` (${i.step_reason})` : ""}` : null], ["Void reason", i.void_reason]]),
      block("What happened", i.what), i.writeup_id ? h("p", null, link("Open the write-up", `#/writeup/${i.writeup_id}`, "")) : null,
      buttons.length ? h("div", { class: "actions" }, buttons) : null),
    card(history(i.history) || h("p", { class: "muted" }, "No history yet.")));
  }

  // ---------------------------------------------------------------- a write-up

  H.registerPage("writeup", { render: renderWriteup });

  const TEXTS = [["what", "What happened"], ["policy", "Policy"], ["improvement", "Expected improvement"],
    ["consequences", "If it happens again"], ["support", "Support offered"]];

  function writeupBody(w) {
    return [...TEXTS.map(([k, label]) => block(label, w.texts[k])),
      rows([["Follow-up date", w.follow_up ? day(w.follow_up) : null],
        ["Suspension", w.suspension ? `${day(w.suspension.from)} to ${day(w.suspension.to)}, ${w.suspension.paid ? "paid" : "unpaid"}` : null],
        ["Counts until", w.ends_on ? day(w.ends_on) : null]])];
  }

  async function renderWriteup(main, parts) {
    const id = encodeURIComponent(parts[0] || "");
    if (parts[1] === "in-person") return renderInPerson(id);
    const w = await api("GET", `/api/writeups/${id}`);
    if (w.voided && !w.texts) {
      return show(card(h("h1", null, "Write-up"), h("p", null, w.status_word), h("p", { class: "muted small" }, "It was withdrawn and no longer counts.")));
    }
    const staff = "manager_note" in w;
    const self = !staff;
    const head = card(h("h1", null, w.step), h("p", null, link(w.person.name, `#/people/${encodeURIComponent(w.person.id)}`, "")),
      badge(w.status_word, STATE_CLASS[w.state] || ""), w.under_appeal ? badge("Under appeal", "warn") : null,
      w.return_note && w.status === "draft" ? h("p", { class: "warn plain" }, `HR's note: ${w.return_note}`) : null,
      w.recommended && w.recommended !== w.step ? h("p", { class: "small" }, `The point system called for ${w.recommended}.${w.downgrade_reason ? ` Why lower: ${w.downgrade_reason}` : ""}`) : null,
      w.incident ? h("p", { class: "small" }, link(`Incident: ${w.incident.offence}, ${day(w.incident.happened)}`, `#/incident/${w.incident.id}`, "")) : null);
    const content = w.can.edit ? draftForm(w) : card(...writeupBody(w),
      staff ? block("Note for managers and HR", w.manager_note, "the employee never sees this") : null,
      "hr_note" in w ? block("HR note", w.hr_note, "HR only") : null);
    const delivery = w.method || w.appeal ? card(h("h2", null, "Delivery"), rows([["How", w.method_word], ["Delivered by", w.delivered_by_name],
      ["Signed as", w.signed_name], ["When", w.signed_at ? H.when(w.signed_at) : null], ["Witness", w.witness]]),
    block(self ? "Your comment" : "Their comment", w.comment), w.appeal ? appealBlock(w) : null) : null;
    const actions = writeupActions(w, id);
    const sign = w.can.sign ? signForm(w, id) : null;
    show(head, content, delivery, sign, actions.length ? card(h("div", { class: "actions" }, actions)) : null,
      card(history(w.history) || h("p", { class: "muted" }, "No history yet.")));
  }

  function appealBlock(w) {
    const a = w.appeal;
    return h("div", { class: "text-block" }, h("h3", null, "Appeal ", badge(a.word, a.status === "open" ? "warn" : "dim")),
      h("p", { class: "plain" }, a.text), a.note ? h("p", { class: "plain" }, `HR's decision: ${a.note}`) : null);
  }

  function draftForm(w) {
    const f = [textField("what", "What happened", { area: true, value: w.texts.what }),
      textField("improvement", "Expected improvement", { area: true, value: w.texts.improvement }),
      textField("consequences", "If it happens again", { area: true, value: w.texts.consequences }),
      textField("support", "Support offered", { area: true, value: w.texts.support }),
      textField("follow_up", "Follow-up date", { type: "date", value: w.follow_up || "" })];
    if (w.is_suspension) {
      f.push(textField("suspension_from", "Suspension from", { type: "date", value: w.suspension?.from || "" }),
        textField("suspension_to", "Suspension to", { type: "date", value: w.suspension?.to || "" }),
        checkField("suspension_paid", "Paid suspension", !!w.suspension?.paid));
    }
    f.push(textField("manager_note", "Note for managers and HR", { area: true, value: w.manager_note, hint: "The employee never sees this." }));
    const steps = w.steps || [];
    const stepField = steps.length > 1 ? choiceField("step", "Step", steps.map((s) => [s.key, s.name]), w.step_key,
      "Choosing a lower step than the point system calls for needs a reason, and HR's approval.") : null;
    const why = textField("downgrade_reason", "Why a lower step?", { area: true, value: w.downgrade_reason || "" });
    if (stepField) f.push(stepField, why);
    const values = () => Object.fromEntries(f.map((x) => [x.name, x.get()]));
    const save = async (send) => {
      const ok = await act(async () => {
        await api("PUT", `/api/writeups/${w.id}`, values());
        return send ? api("POST", `/api/writeups/${w.id}/submit`, {}) : { message: "Saved." };
      });
      if (ok) await H.route();
    };
    return card(h("h2", null, "Draft"), h("div", { class: "text-block" }, h("h3", null, "Policy", h("small", { class: "muted" }, " (quoted automatically)")),
      h("p", { class: "plain quote" }, w.texts.policy || "No handbook wording.")),
    f.map((x) => x.node), h("div", { class: "actions" }, button("Save", () => save(false)), w.can.submit ? button("Send to HR", () => save(true), "primary") : null));
  }

  function writeupActions(w, id) {
    const c = w.can;
    const post = (path, body, ok) => act(() => api("POST", `/api/writeups/${id}/${path}`, body ?? {}), ok);
    const again = async (r) => { if (r) await H.route(); };
    const out = [];
    if (c.approve) out.push(button("Approve", async () => again(await post("approve")), "primary"));
    if (c.return) {
      out.push(button("Send back with a note", async () => {
        const v = await formDialog("Send back to the manager", [textField("note", "Note", { area: true })], "Send back");
        if (v) await again(await post("return", v));
      }));
    }
    if (c.deliver) out.push(link("Deliver in person", `#/writeup/${id}/in-person`, "button primary"));
    if (c.send) {
      out.push(button("Send to sign in the portal", async () => {
        if (await confirmDialog(`Send it to ${firstName(w.person.name)} to read and sign in the HR portal? They're told it's waiting.`, "Send")) await again(await post("send"));
      }));
    }
    if (c.appeal) {
      out.push(button("Appeal", async () => {
        const v = await formDialog("Appeal this write-up", [textField("text", "Why you're appealing", { area: true })], "Send to HR",
          { intro: `You can appeal until ${day(w.appeal_until)}. HR decides, and you'll see the decision here.` });
        if (v) await again(await post("appeal", v));
      }));
    }
    if (c.decide_appeal) out.push(button("Decide the appeal", () => decideAppeal(w, again), "primary"));
    if ("hr_note" in w && c.hr_note) {
      out.push(button("HR note", async () => {
        const v = await formDialog("HR note", [textField("hr_note", "Only HR admins see this", { area: true, value: w.hr_note })]);
        if (v) await again(await act(() => api("PUT", `/api/writeups/${id}`, v)));
      }));
    }
    if (c.change_ends) {
      out.push(button("Change when it stops counting", async () => {
        const v = await formDialog("When it stops counting", [textField("ends_on", "Counts until", { type: "date", value: w.ends_on }), reasonField()], "Save");
        if (v) await again(await post("ends", v));
      }));
    }
    if (c.pdf_retry) out.push(button("File the PDF again", async () => again(await post("pdf-retry"))));
    if (c.void) {
      out.push(button("Void", async () => {
        const v = await formDialog("Void the write-up", [reasonField(), checkField("with_incident", "Void its incident too (its points stop counting)")], "Void",
          { danger: true, intro: "Only HR sees voided write-ups. If it was delivered, the employee sees it was withdrawn." });
        if (v) await again(await post("void", v));
      }, "danger"));
    }
    if (w.pdf && w.pdf.error) out.push(h("p", { class: "warn small" }, `Signed PDF: ${w.pdf.error}`));
    if (w.pdf_url) out.push(link("Signed PDF", w.pdf_url));
    else if (w.pdf && w.pdf.status === "waiting" && !w.pdf.error) out.push(h("p", { class: "muted small" }, "The signed PDF is on its way to Google Drive."));
    if (w.photo_url) out.push(link("Signed page (photo)", w.photo_url));
    return out;
  }

  async function decideAppeal(w, again) {
    const lower = (w.steps || []).slice(0, Math.max(0, (w.steps || []).findIndex((s) => s.name === w.step)));
    const outcome = choiceField("outcome", "Decision", [["upheld", "Upheld: no change"], ["amended", "Amended"], ["voided", "Voided: it stops counting"]], "upheld");
    const points = textField("points", "Points for its incident (amended)", { type: "number", attrs: { step: "0.25", min: "0", inputmode: "decimal" } });
    const step = choiceField("step", "Lower step (amended)", [["", "Keep the step"], ...lower.map((s) => [s.key, s.name])], "");
    const show_ = () => { points.node.hidden = step.node.hidden = outcome.get() !== "amended"; };
    outcome.input.addEventListener("change", show_);
    show_();
    const v = await formDialog("Decide the appeal", [outcome, points, step, textField("note", "Your decision, for the employee and their manager", { area: true })],
      "Decide", { intro: w.appeal.text ? `Their appeal: ${w.appeal.text}` : null });
    if (v) await again(await act(() => api("POST", `/api/appeals/${w.appeal.id}/decide`, v)));
  }

  // The employee's PIN (and code), with their name and an optional comment.
  function signatureFields(needsCode) {
    const comment = textField("comment", "Comment (optional)", { area: true, hint: "Anything you'd like on the record." });
    const name = textField("name", "Type your full name", { attrs: { autocomplete: "name" } });
    const pin = textField("pin", "Your PIN", { type: "password", hint: "Your Connecteam kiosk code.", attrs: { inputmode: "numeric", autocomplete: "off", maxlength: "8" } });
    const code = needsCode ? textField("code", "The 6-digit code from your authenticator app", { attrs: { inputmode: "numeric", autocomplete: "one-time-code", maxlength: "7" } }) : null;
    return [comment, name, pin, code].filter(Boolean);
  }

  function signForm(w, id) {
    const fields = signatureFields(!!(H.me && H.me.totp_on));
    const error = h("p", { class: "error", role: "alert", hidden: true });
    const go = h("button", { type: "submit", class: "primary" }, "Sign");
    const form = h("form", { onsubmit: async (e) => {
      e.preventDefault();
      error.hidden = true;
      go.disabled = true;
      try {
        const r = await api("POST", `/api/writeups/${id}/sign`, Object.fromEntries(fields.map((f) => [f.name, f.get()])));
        toast(r.message);
        await H.route();
      } catch (err) {
        if (!quiet(err)) { error.textContent = err.message; error.hidden = false; }
      } finally { go.disabled = false; }
    } }, fields.map((f) => f.node), error, h("div", { class: "actions" }, go));
    return card(h("h2", null, "Sign"), h("p", { class: "statement" }, w.statement), form);
  }

  // ---------------------------------------------------------------- delivering in person

  async function renderInPerson(id) {
    const w = await api("GET", `/api/writeups/${id}/in-person`);
    const first = firstName(w.person.name);
    const back = `#/writeup/${id}`;
    const fields = signatureFields(w.needs_code);
    const error = h("p", { class: "error", role: "alert", hidden: true });
    const go = h("button", { type: "submit", class: "primary" }, `${first} signs`);
    const signHere = w.can_sign_here ? h("form", { class: "no-print", onsubmit: async (e) => {
      e.preventDefault();
      error.hidden = true;
      go.disabled = true;
      try {
        const r = await api("POST", `/api/writeups/${id}/sign-in-person`, Object.fromEntries(fields.map((f) => [f.name, f.get()])));
        toast(r.message);
        location.hash = back;
      } catch (err) {
        if (!quiet(err)) { error.textContent = err.message; error.hidden = false; }
      } finally { go.disabled = false; }
    } }, fields.map((f) => f.node), error, h("div", { class: "actions" }, go))
      : h("p", { class: "warn no-print" }, `${first} can't sign in the HR portal yet, so print it and have it signed on paper.`);
    const photo = h("input", { type: "file", accept: "image/jpeg,image/png,image/webp,application/pdf", capture: "environment" });
    const paper = card(h("h2", null, "On paper"), h("p", { class: "muted small" }, "Print it, have it signed, then add a photo of the signed page."),
      h("div", { class: "actions" }, button("Print", () => window.print()),
        h("label", { class: "button file-button" }, "Add the photo", photo)));
    photo.addEventListener("change", async () => {
      const file = photo.files && photo.files[0];
      if (!file) return;
      if (await act(() => upload(`/api/writeups/${id}/paper`, file))) location.hash = back;
      photo.value = "";
    });
    const refused = card(h("h2", null, "Refused to sign"), h("p", { class: "muted small" },
      "It still counts. Another HR portal user can witness it with their PIN, or type the witness's name."),
    h("div", { class: "actions" }, button("Record a refusal", () => refusal(id, back))));
    paper.classList.add("no-print");
    refused.classList.add("no-print");
    show(card(h("p", { class: "no-print muted" }, `Hand this to ${first} to read. Only what ${first} may see is shown.`),
      h("h1", null, w.step), h("p", null, `${w.person.name}${w.issued_on ? ` · ${day(w.issued_on)}` : ""}`),
      h("div", { class: "reading" }, writeupBody(w)), h("p", { class: "statement" }, w.statement),
      h("div", { class: "print-only" }, h("p", { class: "signature-line" }, "Employee signature and date"),
        h("p", { class: "signature-line" }, "Manager signature and date"),
        h("p", { class: "small" }, "Comment:"), h("div", { class: "comment-space" })),
      signHere), paper, refused, h("p", { class: "no-print" }, link("Back to the write-up", back, "")));
  }

  async function refusal(id, back) {
    const witness = textField("witness", "Witness's mobile number", { hint: "Someone else who signs in to the HR portal.", attrs: { inputmode: "tel" } });
    const pin = textField("witness_pin", "Witness's PIN", { type: "password", attrs: { inputmode: "numeric", maxlength: "8" } });
    const code = textField("witness_code", "Witness's 6-digit code, if they use an authenticator", { attrs: { inputmode: "numeric", maxlength: "7" } });
    const name = textField("witness_name", "Or type the witness's name", { hint: "When no portal user is there to confirm." });
    const v = await formDialog("Refused to sign", [witness, pin, code, name], "Record", { intro: "The write-up becomes active either way." });
    if (!v) return;
    if (v.witness) delete v.witness_name;
    if (await act(() => api("POST", `/api/writeups/${id}/refused`, v))) location.hash = back;
  }

  // A file sent as it is (not JSON).
  async function upload(path, file) {
    const res = await fetch(path, { method: "POST", credentials: "same-origin", body: file,
      headers: { "X-Requested-With": "hr-portal", "Content-Type": "application/octet-stream" } });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) {
      await api("GET", "/api/me").catch(() => {});   // shows the sign-in page
      throw new Error("Signed out");
    }
    if (!res.ok) throw new Error(data.error || `Something went wrong (${res.status}).`);
    return data;
  }

  // ---------------------------------------------------------------- "as of" a date

  H.registerPage("discipline-history", { who: (me) => me.staff, render: async (main, parts, params) => {
    const pid = decodeURIComponent(parts[0] || "");
    const on = params.get("on");   // none: today, as the add-on counts it
    const d = await api("GET", `/api/people/${encodeURIComponent(pid)}/discipline${on ? `?as_of=${encodeURIComponent(on)}` : ""}`);
    const picker = h("input", { type: "date", value: d.as_of, max: d.today });
    picker.addEventListener("change", () => { if (picker.value) location.hash = `#/discipline-history/${encodeURIComponent(pid)}?on=${picker.value}`; });
    show(card(h("h1", null, `As of ${day(d.as_of)}`), field("Show another day", picker),
      rows([["Points", pts(total(d.points))], ["Step", d.step || "None"], ["Next drop", d.next_drop ? `${plural(d.next_drop.points, "point")} on ${day(d.next_drop.date)}` : null]]),
      h("p", null, link("Back to their page", `#/people/${encodeURIComponent(pid)}`, ""))),
    card(h("h2", null, "Write-ups"), list(d.writeups.map(writeupItem), "None by then.")),
    card(h("h2", null, "Incidents"), list(d.incidents.map((i) => item(`#/incident/${i.id}`, i.offence, `${day(i.happened)} · ${plural(i.points, "point")} · ${i.status_word}`)), "None by then.")),
    d.lines.length ? card(pointLines(d.lines)) : null);
  } });

  // ---------------------------------------------------------------- My team

  H.registerPage("team", { nav: "My team", who: (me) => me.staff, render: async () => {
    const t = await api("GET", "/api/discipline/team");
    const filter = h("input", { type: "search", placeholder: "Find someone", "aria-label": "Find someone", class: "filter" });
    const people = t.people.map((p) => {
      const counts = [[p.tasks, "step due"], [p.drafts, "draft"], [p.returned, "sent back"], [p.waiting, "with HR"], [p.to_deliver, "to deliver"], [p.unsigned, "not signed"]]
        .filter(([n]) => n).map(([n, word]) => badge(`${n} ${word}`, word === "step due" || word === "sent back" ? "warn" : ""));
      const el = item(`#/people/${encodeURIComponent(p.id)}`, p.name,
        [`${plural(p.points, "point")}`, p.step || "No step", p.probation ? `${p.probation} until ${day(p.probation_ends)}` : null].filter(Boolean).join(" · "), ...counts);
      el.dataset.name = p.name.toLowerCase();
      return el;
    });
    filter.addEventListener("input", () => {
      const q = filter.value.trim().toLowerCase();
      for (const el of people) el.hidden = q && !el.dataset.name.includes(q);
    });
    show(card(h("h1", null, "My team"), t.message ? h("p", { class: "warn" }, t.message) : null,
      h("p", { class: "muted small" }, "Points, steps and probation for everyone whose file you see.")),
    t.tasks.length ? card(h("h2", null, "To do"), list(t.tasks.map((x) => item(x.writeup_id ? `#/writeup/${x.writeup_id}` : `#/people/${encodeURIComponent(x.person_id)}`,
      `${x.name}: ${x.word}${x.step ? ` (${x.step})` : ""}`, x.reason)))) : null,
    t.returned.length ? card(h("h2", null, "Sent back to you by HR"), list(t.returned.map((x) => item(`#/incident/${x.id}`, `${x.name}: ${x.offence}`, "Read HR's note, change it and send it again.")))) : null,
    card(h("h2", null, "People"), t.people.length > 8 ? filter : null, list(people, "No one to show.")));
  } });

  // ---------------------------------------------------------------- HR's discipline queue

  H.registerPage("discipline", { nav: "Discipline", who: (me) => me.hr, render: async () => {
    const q = await api("GET", "/api/discipline/queue");
    const own = (x) => (x.own_work ? badge("Another HR admin approves this", "dim") : null);
    const sections = [];
    const add = (title, items, note) => { if (items.length) sections.push(card(h("h2", null, title), note ? h("p", { class: "muted small" }, note) : null, list(items))); };
    add("Incidents to approve", q.incidents.map((i) => item(`#/incident/${i.id}`, `${i.name}: ${i.offence}`,
      `${day(i.happened)} · ${plural(i.points, "point")} · recorded by ${i.by}`, own(i))));
    add("Write-ups to approve", q.writeups.map((w) => item(`#/writeup/${w.id}`, `${w.name}: ${w.step}`, w.by ? `By ${w.by}` : "Drafted for HR", own(w))));
    add("Appeals to decide", q.appeals.map((a) => item(`#/writeup/${a.writeup_id}`, `${a.name}: ${a.step}`, `Appealed ${H.when(a.filed_at)}`,
      a.own_work ? badge("Another HR admin decides this", "dim") : null)));
    add("Tasks", q.tasks.map((t) => taskItem(t)));
    const p = q.probation;
    add("Probation decisions", [
      ...p.recommended.map((x) => item(`#/people/${encodeURIComponent(x.person_id)}`, `${x.name}: ${x.recommendation}`, `${x.kind}, ends ${day(x.ends)}: recommended, waiting for your decision`)),
      ...p.overdue.map((x) => item(`#/people/${encodeURIComponent(x.person_id)}`, `${x.name}: ended with no decision`, `${x.kind} ended ${day(x.ends)}`, badge("Overdue", "bad"))),
      ...p.due.map((x) => item(`#/people/${encodeURIComponent(x.person_id)}`, `${x.name}: review due`, `${x.kind} ends ${day(x.ends)}`)),
      ...p.separations.map((x) => item(`#/people/${encodeURIComponent(x.person_id)}`, `${x.name}: employment ending`, "Archive them in Connecteam by hand, then mark it done on their page."))]);
    add("Not signed yet", q.unsigned.map((w) => item(`#/writeup/${w.id}`, w.name, `Delivered ${day(w.issued_on)}. It counts either way.`)));
    add("Signed write-ups that couldn't be filed", q.pdfs.map((w) => item(`#/writeup/${w.id}`, w.name, w.error)));
    show(card(h("h1", null, "Discipline"), q.message ? h("p", { class: "warn" }, q.message) : null,
      q.waiting_pdfs ? h("p", { class: "muted small" }, `${q.waiting_pdfs} signed write-up${q.waiting_pdfs === 1 ? " waits" : "s wait"} to be filed in Google Drive.`) : null,
      sections.length ? null : h("p", { class: "muted" }, "Nothing is waiting for you.")), sections);
  } });

  function taskItem(t) {
    const href = t.writeup_id ? `#/writeup/${t.writeup_id}` : t.incident_id ? `#/incident/${t.incident_id}` : `#/people/${encodeURIComponent(t.person_id)}`;
    const el = item(href, `${t.name}: ${t.word}${t.step ? ` (${t.step})` : ""}`, t.reason);
    const done = async (path, values) => { if (await act(() => api("POST", `/api/discipline-tasks/${t.id}/${path}`, values))) await H.route(); };
    if (t.kind === "termination") {
      put(el, h("div", { class: "actions" }, button("Decide", async () => {
        const v = await formDialog("Termination review", [choiceField("outcome", "Outcome", [["no_action", "No action"], ["last_chance", "Last-chance agreement"], ["terminate", "Terminate"]], "no_action"), reasonField()],
          "Decide", { intro: "Never automatic: you decide. Terminate is recorded here; archive them in Connecteam by hand." });
        if (v) await done("decide", v);
      }, "primary")));
    } else if (t.kind === "check" || t.kind === "separation") {
      put(el, h("div", { class: "actions" }, button("Done", async () => {
        const v = await formDialog(t.word, [textField("note", "Note (optional)", { area: true })], "Done");
        if (v) await done("done", v);
      })));
    }
    return el;
  }

  // ---------------------------------------------------------------- HR settings

  H.registerSettings("discipline_workflow", (box, s, again) => {
    const appeal = textField("appeal_days", "Days to appeal a write-up", { type: "number", value: s.appeal_days, attrs: { min: "1", max: "60" } });
    const sign = textField("sign_days", "Remind the manager when a write-up isn't signed after (days)", { type: "number", value: s.sign_days, attrs: { min: "1", max: "30" } });
    const word = choiceField("probation_word", "Screens say", Object.entries(s.words), s.probation_word, "Have your employment lawyer check the wording.");
    const showEnd = checkField("probation_show_end", "Employees see their probation end date", s.probation_show_end);
    const fields = [appeal, sign, word, showEnd];
    put(box, h("h2", null, "Write-ups and probation"),
      s.set_up ? null : h("p", { class: "warn" }, "The point system isn't published yet, so incidents can't be recorded. Probation already works."),
      h("form", { onsubmit: async (e) => {
        e.preventDefault();
        if (await act(() => api("PUT", "/api/settings/discipline-workflow", Object.fromEntries(fields.map((f) => [f.name, f.get()]))), "Saved.")) await again();
      } }, fields.map((f) => f.node), h("div", { class: "actions" }, h("button", { type: "submit", class: "primary" }, "Save"))));
  });
})();
