// HR portal: performance reviews. The Reviews page (what each person has to do, and the others
// they can see), one review (a one-page form that saves as it goes, tap-to-rate, with a read-only
// side panel), the Reviews section of a person's page, HR's review settings and the forms editor.
// Plain script under the page's Content-Security-Policy: no inline handlers, and every text is a
// text node (h()), never HTML.
(() => {
  "use strict";

  const H = window.HRPortal;
  if (!H) return;
  const { h, api, toast } = H;

  // Its own stylesheet, from this address like everything else.
  const me = document.currentScript;
  const version = me && me.src.includes("?") ? me.src.slice(me.src.indexOf("?")) : "";
  document.head.append(h("link", { rel: "stylesheet", href: `/static/hr-reviews.css${version}` }));

  const quiet = (err) => ["SignedOut", "Stale"].includes(err?.constructor?.name);
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
  const redraw = () => H.route();
  const day = (iso) => (iso ? new Date(`${iso}T12:00:00`).toLocaleDateString([], {
    month: "short", day: "numeric", year: "numeric" }) : "");
  const firstName = (name) => String(name || "").split(" ")[0];
  const card = (...children) => h("section", { class: "card" }, ...children);
  const plainText = (text, cls = "") => h("p", { class: `rv-text ${cls}` }, text);
  const ratingText = (n, scale) => (n ? `${n} of ${scale.length}: ${scale[n - 1]}` : "Not rated");

  // ---------------------------------------------------------------- saving as people type

  // Saves go one after another (so they arrive in order); typing waits a moment first. The
  // status line says whether everything is saved.
  function saver(statusEl, onSaved) {
    let chain = Promise.resolve();
    const waiting = new Map();
    const say = (text, bad) => {
      statusEl.textContent = text;
      statusEl.classList.toggle("bad", !!bad);
    };
    function send(method, path, body) {
      say("Saving…");
      chain = chain.then(() => api(method, path, body)).then((r) => {
        if (!waiting.size) say("All changes saved");
        if (onSaved) onSaved(r);
      }, (err) => {
        if (!quiet(err)) say(`Not saved: ${err.message}`, true);
      });
      return chain;
    }
    function later(key, method, path, body, ms = 700) {
      clearTimeout(waiting.get(key)?.timer);
      say("Saving…");
      waiting.set(key, { method, path, body, timer: setTimeout(() => {
        waiting.delete(key);
        send(method, path, body);
      }, ms) });
    }
    function flush() {
      for (const [key, w] of waiting) {
        clearTimeout(w.timer);
        waiting.delete(key);
        send(w.method, w.path, w.body);
      }
      return chain;
    }
    // Leaving the page sends what's still waiting.
    window.addEventListener("hashchange", flush, { once: true });
    return { send, later, flush };
  }

  // ---------------------------------------------------------------- the parts of a form

  function rater(scale, value, onPick, label) {
    const word = h("span", { class: "rv-word" }, value ? scale[value - 1] : "Not rated yet");
    const buttons = scale.map((name, i) => h("button", {
      type: "button", class: "rv-rate", "aria-pressed": String(value === i + 1), title: name,
      "aria-label": `${i + 1}: ${name}`,
      onclick: () => {
        for (const b of buttons) b.setAttribute("aria-pressed", String(b === buttons[i]));
        word.textContent = name;
        onPick(i + 1);
      } }, String(i + 1)));
    return h("div", null, h("div", { class: "rv-rates", role: "group", "aria-label": label }, buttons), word);
  }

  function textBox(value, placeholder, onText, rows = 3) {
    const box = h("textarea", { class: "rv-box", rows: String(rows), maxlength: "4000", placeholder });
    box.value = value || "";
    box.addEventListener("input", () => onText(box.value));
    return box;
  }

  function goalsEditor(goals, onChange) {
    const list = h("div", { class: "rv-goals" });
    const values = () => [...list.querySelectorAll("input")].map((i) => i.value);
    const add = (text) => {
      const input = h("input", { type: "text", maxlength: "300", value: text || "", "aria-label": "Goal" });
      input.addEventListener("input", () => onChange(values()));
      const row = h("div", { class: "rv-goal" }, input, h("button", { type: "button", class: "linkish", onclick: () => {
        row.remove();
        onChange(values());
      } }, "Remove"));
      list.append(row);
      return input;
    };
    (goals?.length ? goals : [""]).forEach(add);
    return h("div", null, list, h("button", { type: "button", onclick: () => {
      if (list.children.length >= 10) return toast("At most 10 goals.", true);
      add("").focus();
    } }, "Add a goal"));
  }

  // One section, to fill in (edit) or to read. answers: {ratings, texts}.
  function sectionBlock(s, form, answers, edit) {
    const box = h("div", { class: "rv-section" }, h("h3", null, s.title), s.help ? h("p", { class: "muted small" }, s.help) : null);
    const rating = answers.ratings?.[s.key];
    const text = answers.texts?.[s.key];
    if (!edit) {
      if (s.kind === "rating") box.append(h("p", { class: "rv-rating" }, ratingText(rating, form.scale)));
      if (s.kind === "goals") {
        box.append(Array.isArray(text) && text.length ? h("ul", null, text.map((g) => h("li", null, g)))
          : h("p", { class: "muted" }, "None set."));
      } else if (text) box.append(plainText(text));
      else if (s.kind === "text") box.append(h("p", { class: "muted" }, "Nothing written."));
      return box;
    }
    if (s.kind === "goals") {
      box.append(goalsEditor(text, (goals) => edit.later(s.key, { section: s.key, goals })));
      return box;
    }
    // A low rating needs a comment: said under the box until there is one.
    const now = { rating, text: text || "" };
    const need = h("p", { class: "warn small" }, "A low rating needs a comment.");
    const check = () => { need.hidden = !(edit.commentNeeded && now.rating && now.rating <= form.comment_at && !now.text.trim()); };
    check();
    if (s.kind === "rating") {
      box.append(rater(form.scale, rating, (n) => {
        now.rating = n;
        check();
        edit.now({ section: s.key, rating: n });
      }, s.title));
    }
    box.append(h("label", null, s.kind === "rating" ? "Comment" : "Your words",
      textBox(text, edit.placeholder || "", (value) => {
        now.text = value;
        check();
        edit.later(`${s.key}:text`, { section: s.key, text: value });
      })), need);
    return box;
  }

  // ---------------------------------------------------------------- the Reviews page

  async function showList() {
    const data = await api("GET", "/api/reviews");
    const item = (i) => h("li", null, h("a", { class: "item", href: `#/reviews/${i.id}` },
      h("div", { class: "title" }, H.me.staff ? `${i.person} · ${i.kind_word}` : i.kind_word),
      h("div", { class: "sub" }, [`Due ${day(i.due)}`, i.what || i.status_word].join(" · ")),
      i.overdue ? h("span", { class: "badge bad" }, "Late") : null));
    const list = (rows, empty) => (rows.length ? h("ul", { class: "list" }, rows.map(item)) : h("p", { class: "muted" }, empty));
    H.show(
      card(h("h1", null, "Reviews"),
        H.me.staff ? h("p", { class: "muted small" }, "To start an extra review, open the person's page.") : null,
        data.hr ? h("div", { class: "actions" }, h("a", { class: "button", href: "#/reviews/forms" }, "Review forms"),
          h("a", { class: "button", href: "#/settings" }, "Review settings")) : null),
      card(h("h2", null, "To do"), list(data.todo, "Nothing to do.")),
      data.staff || data.open.length ? card(h("h2", null, data.staff ? "Others still open" : "Shared with you"), list(data.open, "None.")) : null,
      card(h("details", { class: "rv-more" }, h("summary", null, `Closed (${data.closed.length})`), list(data.closed, "None yet."))));
  }

  // ---------------------------------------------------------------- one review

  async function showReview(id) {
    const data = await api("GET", `/api/reviews/${encodeURIComponent(id)}`);
    const r = data.review;
    const form = data.form;
    const can = data.can;
    const path = `/api/reviews/${r.id}`;
    const staff = data.relation !== "self";
    const status = h("p", { class: "rv-status small", role: "status" }, "All changes saved");
    const problemsBox = h("div");
    const overallBox = h("div");
    const drawOverall = (o) => overallBox.replaceChildren(overallSummary(o, form));
    const drawProblems = (list) => problemsBox.replaceChildren(list.length
      ? h("div", null, h("h3", null, "Before you finish"), h("ul", { class: "rv-problems" }, list.map((p) => h("li", null, p))))
      : h("p", { class: "muted small" }, "Everything needed is filled in."));

    const head = card(h("h1", null, staff ? r.person.name : r.kind_word),
      h("p", { class: "muted" }, [staff ? r.kind_word : null, `Due ${day(r.due)}`, r.status_word].filter(Boolean).join(" · ")),
      r.overdue ? h("span", { class: "badge bad" }, "Late") : null,
      r.self_word && staff ? h("span", { class: "badge" }, r.self_word) : null,
      h("p", { class: "muted small" }, `Covers ${day(r.period.start)} to ${day(r.period.end)}. Written by ${r.writer}.`
        + (r.meeting_date ? ` Meeting: ${day(r.meeting_date)}.` : "")),
      staff ? h("p", null, h("a", { href: `#/people/${encodeURIComponent(r.person.id)}` }, `${r.person.name}'s file`)) : null,
      r.cancelled ? h("p", { class: "warn" }, `Cancelled: ${r.cancelled.reason || ""}`) : null,
      data.returned_note && r.status === "writing" ? h("p", { class: "warn" }, `HR sent this back: ${data.returned_note}`) : null);

    const parts = [head];
    if (data.side) parts.push(sidePanel(data.side));

    // The self-review: filled in by the employee, read by the others once it's sent.
    if (can.self) parts.push(selfForm(data, path, status));
    else if (data.self_review && (staff || r.self_status === "done")) {
      parts.push(card(h("h2", null, staff ? "Their self-review" : "Your self-review"),
        data.form.sections.filter((s) => s.kind !== "goals" || data.self_review.texts[s.key])
          .map((s) => sectionBlock(s, form, data.self_review, null)),
        data.self_review.texts.overall ? [h("h3", null, "Anything else"), plainText(data.self_review.texts.overall)] : null));
    }

    // The review itself.
    if (can.write) {
      const save = saver(status, (res) => {
        if (res?.overall) drawOverall(res.overall);
        if (res?.problems) drawProblems(res.problems);
      });
      const edit = {
        commentNeeded: true,
        now: (body) => save.send("PUT", `${path}/answer`, body),
        later: (key, body) => save.later(key, "PUT", `${path}/answer`, body),
      };
      parts.push(card(h("h2", null, "The review"),
        h("p", { class: "muted small" }, "Tap a number to rate. Everything saves as you go."),
        form.sections.map((s) => sectionBlock(s, form, data.answers, edit))));
      parts.push(overallEditor(data, save, overallBox, path));
      drawOverall(data.overall);
      drawProblems(data.problems || []);
      parts.push(h("div", { class: "rv-savebar" }, status));
      parts.push(card(h("h2", null, "Finish"), problemsBox, h("div", { class: "actions" },
        can.skip_self ? h("button", { type: "button", onclick: async () => {
          if (!(await H.confirmDialog("Carry on without the self-review? It can't be sent after this.", "Carry on"))) return;
          if (await run(() => api("POST", `${path}/skip-self`, {}), "Carrying on without it.")) await redraw();
        } }, "Carry on without the self-review") : null,
        h("button", { type: "button", class: "primary", onclick: async () => {
          await save.flush();
          const r2 = await run(() => api("POST", `${path}/submit`, {}));
          if (!r2) return;
          toast(r2.status === "approval" ? "Sent to HR for approval." : "Done. Share it at your meeting.");
          await redraw();
        } }, "Finish"))));
    } else if (data.answers) {
      parts.push(card(h("h2", null, "The review"), form.sections.map((s) => sectionBlock(s, form, data.answers, null)),
        h("div", { class: "rv-section" }, h("h3", null, "Overall"), overallSummary(data.overall, form),
          data.answers.texts.overall ? plainText(data.answers.texts.overall) : null,
          data.recommendation?.choice ? h("p", null, h("strong", null, "Recommendation: "),
            form.choices[data.recommendation.choice] || data.recommendation.choice,
            data.recommendation.choice === "extend" && data.recommendation.extend_to ? ` to ${day(data.recommendation.extend_to)}` : "") : null,
          form.probation && data.relation === "hr" ? h("p", { class: data.recommendation_problem ? "warn small" : "muted small" }, data.recommendation_sent
            ? "Passed to the introductory period records: confirm the outcome there."
            : data.recommendation_problem ? `Couldn't be passed to the probation records: ${data.recommendation_problem} Decide the outcome on their page.`
              : "Not passed to the introductory period records yet.") : null)));
    } else if (staff) {
      parts.push(card(h("h2", null, "The review"), h("p", { class: "muted" }, "Not started yet.")));
    }

    if (r.status === "closed" || data.employee_comment) parts.push(closedCard(data));
    parts.push(...actionCards(data, path));
    if (staff || data.comments.length) parts.push(commentsCard(data, path));
    if (data.note !== undefined) parts.push(noteCard(data, path));
    H.show(parts);
  }

  function overallSummary(o, form) {
    if (!o || o.value === null) return h("p", { class: "muted" }, "No overall rating yet: rate the sections first.");
    return h("div", null, h("p", { class: "rv-rating" }, `Overall: ${o.value} of ${form.scale.length}: ${o.word}`),
      o.override !== null ? h("p", { class: "muted small" }, `Worked out from the ratings: ${o.auto ?? "none yet"}. Why it differs: ${o.reason || "not said yet"}`)
        : h("p", { class: "muted small" }, "Worked out from the ratings (their average)."));
  }

  function overallEditor(data, save, overallBox, path) {
    const form = data.form;
    const o = data.overall;
    const reasonWrap = h("label", { hidden: o.override === null }, "Why it differs from the worked-out rating",
      textBox(o.reason, "", (value) => save.later("reason", "PUT", `${path}/overall`, { reason: value })));
    const pick = h("select", { "aria-label": "Overall rating", onchange: () => {
      const n = pick.value ? Number(pick.value) : null;
      reasonWrap.hidden = n === null;
      save.send("PUT", `${path}/overall`, { override: n });
    } }, h("option", { value: "" }, "Use the worked-out rating"),
    form.scale.map((name, i) => h("option", { value: String(i + 1), selected: o.override === i + 1 }, `${i + 1}: ${name}`)));
    const choices = Object.entries(form.choices);
    const rec = data.recommendation || {};
    const extend = h("label", { hidden: rec.choice !== "extend" }, "New end date",
      h("input", { type: "date", value: rec.extend_to || "", onchange: (e) => save.send("PUT", `${path}/overall`, { extend_to: e.target.value }) }));
    return card(h("h2", null, "Overall"), overallBox,
      h("label", null, "Overall rating", h("small", null, "Choose your own only if the worked-out one doesn't fit, and say why."), pick),
      reasonWrap,
      h("label", null, "Overall comments", textBox(data.answers.texts.overall, "", (value) =>
        save.later("overall", "PUT", `${path}/answer`, { section: "overall", text: value }))),
      choices.length ? h("fieldset", { class: "rv-choices" }, h("legend", null, form.probation ? "Recommendation for the end of the introductory period" : "Recommendation"),
        choices.map(([key, words]) => h("label", { class: "check" }, h("input", {
          type: "radio", name: "recommendation", value: key, checked: rec.choice === key,
          onchange: () => {
            extend.hidden = key !== "extend";
            save.send("PUT", `${path}/overall`, { recommendation: key });
          } }), words)), form.probation ? extend : null,
        form.probation ? h("p", { class: "muted small" }, "HR confirms the outcome. The employee doesn't see this recommendation.") : null) : null);
  }

  function selfForm(data, path, status) {
    const save = saver(status);
    const edit = { commentNeeded: false, placeholder: "Optional",
      now: (body) => save.send("PUT", `${path}/self`, body),
      later: (key, body) => save.later(key, "PUT", `${path}/self`, body) };
    const answers = data.self_review || { ratings: {}, texts: {} };
    const sections = data.form.sections.map((s) => sectionBlock(s, data.form, answers, edit));
    const extra = h("div", { class: "rv-section" }, h("h3", null, "Anything else"),
      textBox(answers.texts.overall, "Optional", (value) => edit.later("overall", { section: "overall", text: value })));
    return card(h("h2", null, "Your self-review"),
      h("p", null, "Rate yourself and add anything you'd like your manager to know. It saves as you go; send it when you're done."),
      sections, extra, h("div", { class: "rv-savebar" }, status),
      h("div", { class: "actions" }, h("button", { type: "button", class: "primary", onclick: async () => {
        await save.flush();
        if (!(await H.confirmDialog("Send your self-review? You can't change it after.", "Send"))) return;
        if (await run(() => api("POST", `${path}/self/send`, {}), "Sent. Thank you.")) await redraw();
      } }, "Send my self-review")));
  }

  function sidePanel(side) {
    const na = () => h("p", { class: "muted" }, "Not available");
    const d = side.discipline;
    const discipline = d ? [
      h("dl", { class: "rows" }, d.points.length ? d.points.flatMap((p) => [h("dt", null, `${p.track} points now`), h("dd", null, String(p.total))])
        : [h("dt", null, "Points now"), h("dd", null, "None")],
      d.next_drop?.date ? [h("dt", null, "Next drop"), h("dd", null, `${d.next_drop.points} on ${day(d.next_drop.date)}`)] : []),
      h("h4", null, "Write-ups in this period"),
      d.writeups.length ? h("ul", null, d.writeups.map((w) => h("li", null, `${day(w.date)}: ${w.step} (${w.status})`)))
        : h("p", { class: "muted" }, "None.")] : na();
    const certs = side.certificates;
    const certificates = certs ? (certs.length ? h("ul", null, certs.map((c) => h("li", null,
      `${c.name}: ${c.status}${c.expires ? `, expires ${day(c.expires)}` : ""}`, c.flag ? h("span", { class: "badge bad" }, c.flag) : null)))
      : h("p", { class: "muted" }, "None needed.")) : na();
    return h("section", { class: "card rv-side" }, h("details", { open: window.matchMedia("(min-width: 760px)").matches },
      h("summary", null, h("h2", null, "Background (read only)")),
      h("p", { class: "muted small" }, `From ${day(side.period.start)} to ${day(side.period.end)}.`),
      h("h3", null, "Points and write-ups"), discipline,
      h("h3", null, "Certificates"), certificates,
      h("h3", null, "Goals from the last review"),
      side.goals.length ? h("ul", null, side.goals.map((g) => h("li", null, g))) : h("p", { class: "muted" }, "None set.")));
  }

  function closedCard(data) {
    const r = data.review;
    return card(h("h2", null, r.status === "closed" ? "Closed" : "The employee's answer"),
      r.ack_word ? h("p", null, `${r.ack_word}${r.ack_at ? ` on ${H.when(r.ack_at)}` : ""}.`) : null,
      r.ack === "received" ? h("p", { class: "muted small" }, "Acknowledging means they received it, not that they agree.") : null,
      data.employee_comment ? [h("h3", null, data.relation === "self" ? "Your comment" : "Their comment"), plainText(data.employee_comment)] : null,
      data.close_note ? [h("h3", null, "Note"), plainText(data.close_note)] : null,
      data.pdf?.link ? h("p", null, h("a", { href: data.pdf.link }, "Download the PDF")) : null,
      data.pdf && !data.pdf.ready ? h("p", { class: "muted small" }, "The PDF is on its way to Google Drive.") : null,
      data.pdf?.error ? h("p", { class: "warn small" }, data.pdf.error) : null);
  }

  function actionCards(data, path) {
    const r = data.review;
    const can = data.can;
    const cards = [];
    if (can.approve) {
      cards.push(card(h("h2", null, "HR's approval"), h("p", null, "Read it through, then approve it or send it back with a note."),
        h("div", { class: "actions" },
          h("button", { type: "button", class: "primary", onclick: async () => {
            if (await run(() => api("POST", `${path}/approve`, {}), "Approved.")) await redraw();
          } }, "Approve"),
          h("button", { type: "button", onclick: async () => {
            const note = await H.ask("Send it back", "What needs changing?");
            if (note === null) return;
            if (await run(() => api("POST", `${path}/return`, { note }), "Sent back.")) await redraw();
          } }, "Send back"))));
    }
    if (can.share) {
      const date = h("input", { type: "date", value: new Date().toLocaleDateString("en-CA"), required: true });
      cards.push(card(h("h2", null, "Share it"), h("p", null, `Go through it with ${firstName(r.person.name)} at your meeting, then share it. They can read it, comment, and acknowledge it in the HR portal.`),
        h("label", null, "Meeting date", date),
        h("div", { class: "actions" }, h("button", { type: "button", class: "primary", onclick: async () => {
          if (await run(() => api("POST", `${path}/share`, { meeting_date: date.value }), "Shared.")) await redraw();
        } }, `Share with ${firstName(r.person.name)}`))));
    }
    if (can.acknowledge) {
      const comment = textBox("", "Optional", () => {}, 4);
      const answer = (value, text) => async () => {
        if (!(await H.confirmDialog(text, value === "received" ? "I've received it" : "Refuse", value !== "received"))) return;
        if (await run(() => api("POST", `${path}/acknowledge`, { answer: value, comment: comment.value }), "Thank you.")) await redraw();
      };
      cards.push(card(h("h2", null, "Acknowledge it"),
        h("p", null, "Acknowledging means you've received this review. It doesn't mean you agree with it."),
        h("label", null, "Your comment", h("small", null, "Anything you'd like to add. It's kept with the review."), comment),
        h("div", { class: "actions" },
          h("button", { type: "button", class: "primary", onclick: answer("received", "Acknowledge that you've received this review?") }, "I've received it"),
          h("button", { type: "button", class: "danger", onclick: answer("refused", "Refuse to acknowledge this review? It's recorded that you refused.") }, "I refuse to acknowledge it"))));
    }
    if (can.close) {
      const how = h("select", null, h("option", { value: "paper" }, "They acknowledged it on paper"),
        h("option", { value: "refused" }, "They refused to acknowledge it"),
        h("option", { value: "no_answer" }, "They didn't answer"));
      const note = textBox("", "Optional", () => {}, 2);
      cards.push(card(h("details", { class: "rv-more" }, h("summary", null, "Close it without their answer"),
        h("p", { class: "muted small" }, "For someone who can't use the HR portal, refused in the meeting, or hasn't answered."),
        h("label", null, "What happened", how), h("label", null, "Note", note),
        h("div", { class: "actions" }, h("button", { type: "button", onclick: async () => {
          if (await run(() => api("POST", `${path}/close`, { how: how.value, note: note.value }), "Closed.")) await redraw();
        } }, "Close it")))));
    }
    if (can.reassign || can.cancel) {
      const who = h("select", { "aria-label": "Who writes it" },
        h("option", { value: "auto", selected: data.assigned === "auto" }, "The usual writer (primary manager of their main department)"),
        h("option", { value: "hr", selected: data.assigned === "hr" }, "HR"),
        data.writers.map((w) => h("option", { value: w.id, selected: data.assigned === w.id }, w.name)));
      cards.push(card(h("h2", null, "HR"),
        can.reassign ? [h("label", null, "Who writes it", who), h("div", { class: "actions" }, h("button", { type: "button", onclick: async () => {
          if (await run(() => api("PUT", `${path}/writer`, { writer: who.value }), "Handed over.")) await redraw();
        } }, "Hand over"))] : null,
        data.notes?.length ? [h("h3", null, "Others' private notes"), data.notes.map((n) => h("div", { class: "rv-note" },
          h("p", { class: "muted small" }, `${n.author}, ${H.when(n.at)}`), plainText(n.text)))] : null,
        can.cancel ? h("div", { class: "actions" }, h("button", { type: "button", class: "danger", onclick: async () => {
          const reason = await H.ask("Cancel this review", "Why? (kept with the review)");
          if (reason === null) return;
          if (await run(() => api("POST", `${path}/cancel`, { reason }), "Cancelled.")) await redraw();
        } }, "Cancel this review")) : null));
    } else if (data.notes?.length) {
      cards.push(card(h("h2", null, "Others' private notes"), data.notes.map((n) => h("div", { class: "rv-note" },
        h("p", { class: "muted small" }, `${n.author}, ${H.when(n.at)}`), plainText(n.text)))));
    }
    return cards;
  }

  function commentsCard(data, path) {
    const list = h("div");
    const draw = (comments) => list.replaceChildren(...(comments.length ? comments.map((c) => h("div", { class: "rv-note" },
      h("p", { class: "muted small" }, `${c.author}, ${H.when(c.at)}`, data.relation !== "self" ? h("span", { class: "badge" }, c.level) : null),
      plainText(c.text))) : [h("p", { class: "muted" }, "No comments.")]));
    draw(data.comments);
    if (!data.can.comment) return card(h("h2", null, "Comments"), list);
    const text = textBox("", "", () => {}, 3);
    const level = h("select", null, Object.entries(data.levels).map(([key, words]) =>
      h("option", { value: key, selected: key === "managers" }, words)));
    return card(h("h2", null, "Comments"), list,
      h("label", null, "Add a comment", h("small", null, "Others who see this person can add comments. Choose who can see yours."), text),
      h("label", null, "Who can see it", level),
      h("div", { class: "actions" }, h("button", { type: "button", onclick: async () => {
        const r = await run(() => api("POST", `${path}/comments`, { text: text.value, visibility: level.value }), "Comment added.");
        if (!r) return;
        text.value = "";
        draw(r.comments);
      } }, "Add comment")));
  }

  function noteCard(data, path) {
    const status = h("p", { class: "muted small", role: "status" }, "");
    const save = saver(status);
    return card(h("h2", null, "My private note"),
      h("p", { class: "muted small" }, "Only you and HR admins see this. It isn't part of the review."),
      textBox(data.note, "", (value) => save.later("note", "PUT", `${path}/note`, { text: value }), 3), status);
  }

  // ---------------------------------------------------------------- HR's forms

  function sectionsEditor(sections, kinds) {
    const list = h("div", { class: "rv-edit" });
    const row = (s) => {
      const title = h("input", { type: "text", maxlength: "80", value: s.title || "", "aria-label": "Section title", placeholder: "Title" });
      const kind = h("select", { "aria-label": "What it asks for" }, Object.entries(kinds).map(([k, words]) =>
        h("option", { value: k, selected: (s.kind || "rating") === k }, words)));
      const help = h("input", { type: "text", maxlength: "300", value: s.help || "", "aria-label": "Help text", placeholder: "Help (optional)" });
      const el = h("div", { class: "rv-edit-row", "data-key": s.key || "" }, title, kind, help,
        h("div", { class: "actions" },
          h("button", { type: "button", onclick: () => el.previousElementSibling?.before(el) }, "Up"),
          h("button", { type: "button", onclick: () => el.nextElementSibling?.after(el) }, "Down"),
          h("button", { type: "button", class: "danger", onclick: () => el.remove() }, "Remove")));
      el.read = () => ({ key: el.dataset.key || "", title: title.value, kind: kind.value, help: help.value });
      return el;
    };
    sections.forEach((s) => list.append(row(s)));
    const box = h("div", null, list, h("button", { type: "button", onclick: () => {
      const el = row({});
      list.append(el);
      el.querySelector("input").focus();
    } }, "Add a section"));
    box.read = () => [...list.children].map((el) => el.read());
    return box;
  }

  async function showForms() {
    const data = await api("GET", "/api/review-forms");
    H.show(card(h("h1", null, "Review forms"),
      h("p", { class: "muted small" }, "Each kind of review uses a form (choose which in HR settings, Reviews). Saving a form makes a new version: reviews already open keep the version they started with."),
      h("ul", { class: "list" }, data.forms.map((f) => h("li", null, h("a", { class: "item", href: `#/reviews/forms/${f.family}` },
        h("div", { class: "title" }, f.name), h("div", { class: "sub" }, `Version ${f.version}`
          + `${Object.entries(data.chosen).filter(([, fam]) => fam === f.family).length ? ` · used for ${Object.entries(data.chosen).filter(([, fam]) => fam === f.family).map(([k]) => data.kinds[k].toLowerCase()).join(", ")}` : ""}`))))),
      h("div", { class: "actions" }, h("a", { class: "button", href: "#/reviews/forms/new" }, "New form"))));
  }

  async function showForm(family) {
    const fresh = family === "new";
    const data = fresh ? { ...(await api("GET", "/api/review-forms")), form: null } : await api("GET", `/api/review-forms/${encodeURIComponent(family)}`);
    const body = data.form?.body || { name: "", scale: ["Unsatisfactory", "Needs improvement", "Meets expectations", "Exceeds expectations", "Outstanding"],
      comment_at: 2, sections: [], departments: {}, recommendations: [] };
    const kinds = data.section_kinds;
    const lines = (box) => box.value.split("\n").map((x) => x.trim()).filter(Boolean);
    const name = h("input", { type: "text", maxlength: "60", value: body.name, required: true });
    const scale = h("textarea", { rows: "5" });
    scale.value = body.scale.join("\n");
    const commentAt = h("select");
    const commentOptions = (points, chosen) => commentAt.replaceChildren(...[...Array(Math.max(points, 1))].map((_, i) =>
      h("option", { value: String(i), selected: chosen === i }, i === 0 ? "No rating needs a comment" : `Ratings of ${i} or lower`)));
    commentOptions(body.scale.length, body.comment_at);
    scale.addEventListener("input", () => commentOptions(lines(scale).length, Number(commentAt.value)));
    const sections = sectionsEditor(body.sections, kinds);
    const depts = data.departments.map((d) => {
      const editor = sectionsEditor(body.departments[d.group] || [], kinds);
      editor.group = d.group;
      return h("details", { class: "rv-dept", open: !!body.departments[d.group] }, h("summary", null, d.name), editor);
    });
    const recs = h("textarea", { rows: "4" });
    recs.value = body.recommendations.join("\n");
    const save = async () => {
      const departments = {};
      for (const el of depts) {
        const editor = el.querySelector(".rv-edit").parentElement;
        const found = editor.read();
        if (found.length) departments[editor.group] = found;
      }
      const result = await run(() => api("POST", "/api/review-forms", { family: fresh ? null : family,
        form: { name: name.value, scale: lines(scale), comment_at: Number(commentAt.value), sections: sections.read(),
          departments, recommendations: lines(recs) } }), "Saved as a new version.");
      if (!result) return;
      const target = `#/reviews/forms/${result.form.family}`;
      if (location.hash === target) await redraw();
      else location.hash = target;
    };
    H.show(card(h("h1", null, fresh ? "New review form" : body.name),
      fresh ? null : h("p", { class: "muted small" }, `Version ${data.form.version}. Used by ${data.used} review${data.used === 1 ? "" : "s"}. Saving makes version ${data.form.version + 1}; reviews already open keep theirs.`),
      h("label", null, "Name", name),
      h("label", null, "Rating scale", h("small", null, "One point per line, from the lowest to the highest (2 to 10)."), scale),
      h("label", null, "Comments needed for", commentAt),
      h("label", null, "Recommendations to choose from", h("small", null, "One per line (at most 8). End-of-introductory-period reviews always use pass, extend or end."), recs)),
    card(h("h2", null, "Sections"), h("p", { class: "muted small" }, "An overall rating, worked out from the rated sections, always comes last."), sections),
    card(h("h2", null, "Extra sections by department"), h("p", { class: "muted small" }, "Added for people whose main department this is."),
      depts.length ? depts : h("p", { class: "muted" }, "No departments yet.")),
    card(h("div", { class: "actions" }, h("a", { class: "button", href: "#/reviews/forms" }, "Back"),
      h("button", { type: "button", class: "primary", onclick: save }, "Save"))));
  }

  // ---------------------------------------------------------------- the page, the section, settings

  H.registerPage("reviews", {
    nav: "Reviews",
    async render(_main, parts) {
      if (parts[0] === "forms" && H.me.hr) return parts[1] ? showForm(parts[1]) : showForms();
      if (parts[0]) return showReview(parts[0]);
      return showList();
    },
  });

  H.registerSection("reviews", (box, s, person, again) => {
    box.append(h("h2", null, s.title));
    box.append(s.items.length ? h("ul", { class: "list" }, s.items.map((i) => h("li", null, h("a", { class: "item", href: `#/reviews/${i.id}` },
      h("div", { class: "title" }, i.kind_word), h("div", { class: "sub" }, `Due ${day(i.due)} · ${i.status_word}`),
      i.overdue ? h("span", { class: "badge bad" }, "Late") : null))))
      : h("p", { class: "muted" }, "No reviews yet."));
    if (s.main) {
      const pick = h("select", null, h("option", { value: "" }, "Automatic"),
        s.main.groups.map((g) => h("option", { value: g.group, selected: s.main.chosen && g.group === s.main.group }, g.name)));
      box.append(h("dl", { class: "rows" }, h("dt", null, "Main department"), h("dd", null, s.main.name),
        h("dt", null, "Writes their reviews"), h("dd", null, s.main.writer)),
      s.main.groups.length > 1 ? [h("label", null, "Change the main department",
        h("small", null, "Its primary manager writes their reviews. Automatic: the first of their departments with a primary manager."), pick),
      h("div", { class: "actions" }, h("button", { type: "button", onclick: async () => {
        if (await run(() => api("PUT", `/api/people/${encodeURIComponent(person.id)}/reviews/main-department`, { group: pick.value }), "Saved.")) await again();
      } }, "Save the main department"))] : null);
    }
    if (s.can_start) {
      box.append(h("div", { class: "actions" }, h("button", { type: "button", onclick: async () => {
        const due = h("input", { type: "date", value: new Date(Date.now() + 14 * 86400000).toLocaleDateString("en-CA") });
        const who = H.me.hr ? h("select", null, h("option", { value: "me" }, "Me"), h("option", { value: "auto" }, "Their usual writer")) : null;
        const choice = await H.openDialog(`An extra review for ${person.name}`, [h("label", null, "Due", due),
          who ? h("label", null, "Who writes it", who) : h("p", { class: "muted small" }, "You write it.")],
        [{ label: "Cancel", value: "cancel" }, { label: "Start it", value: "ok", class: "primary" }]);
        if (choice !== "ok") return;
        const r = await run(() => api("POST", `/api/people/${encodeURIComponent(person.id)}/reviews`, { due: due.value, writer: who ? who.value : "me" }), "Started.");
        if (r) location.hash = `#/reviews/${r.id}`;
      } }, "Start a review")));
    }
  });

  H.registerSettings("reviews", (box, data, again) => {
    const v = data.values;
    const select = (name, options) => h("select", { name }, Object.entries(options).map(([value, words]) =>
      h("option", { value, selected: String(v[name]) === value }, words)));
    const check = (name, words) => h("label", { class: "check" }, h("input", { type: "checkbox", name, checked: v[name] === "1" }), words);
    const num = (name, words, hint, min, max) => h("label", null, words, hint ? h("small", null, hint) : null,
      h("input", { type: "number", name, min: String(min), max: String(max), value: String(v[name]) }));
    const months = Object.fromEntries([...Array(12)].map((_, i) => [String(i + 1), new Date(2026, i, 1).toLocaleDateString([], { month: "long" })]));
    const forms = Object.fromEntries(data.forms.map((f) => [f.family, f.name]));
    const form = h("form", { onsubmit: (e) => {
      e.preventDefault();
      const f = form.elements;
      const body = { reviews_probation: f.reviews_probation.checked ? "1" : "0", reviews_annual: f.reviews_annual.value,
        reviews_annual_month: f.reviews_annual_month.value, reviews_checkin_30: f.reviews_checkin_30.checked ? "1" : "0",
        reviews_checkin_60: f.reviews_checkin_60.checked ? "1" : "0", reviews_self: f.reviews_self.value,
        reviews_second: f.reviews_second.value, reviews_forms: {} };
      for (const key of ["reviews_open_days", "reviews_gap_days", "reviews_remind_before", "reviews_remind_every", "reviews_overdue_days", "reviews_ack_days"]) body[key] = f[key].value;
      for (const kind of Object.keys(data.kinds)) body.reviews_forms[kind] = f[`form_${kind}`].value;
      run(async () => {
        await api("PUT", "/api/settings/reviews", body);
        await again();
      }, "Saved.");
    } },
    h("h3", null, "Kinds of review"),
    check("reviews_probation", "End of introductory period (the dates come from each person's introductory period)"),
    h("label", null, "Annual reviews", select("reviews_annual", { anniversary: "On each person's work anniversary", month: "In one month for everyone", off: "Off" })),
    h("label", null, "The month, if in one month", select("reviews_annual_month", months)),
    check("reviews_checkin_30", "30-day check-ins"), check("reviews_checkin_60", "60-day check-ins"),
    h("h3", null, "Steps"),
    h("label", null, "Self-reviews", select("reviews_self", { annual: "For annual reviews only", all: "For every review", none: "None" })),
    h("label", null, "HR's second approval", select("reviews_second", { probation: "For end-of-introductory-period reviews only", all: "For every review", none: "None" })),
    h("h3", null, "Timing"),
    num("reviews_open_days", "Open a review this many days before it's due", "1 to 60.", 1, 60),
    num("reviews_gap_days", "Skip a review when another for the same person is due within (days)", "0 never skips. An end-of-introductory-period review wins over a check-in.", 0, 90),
    num("reviews_remind_before", "Remind the writer this many days before", "0: no reminder before. They're also reminded on the day.", 0, 30),
    num("reviews_remind_every", "Then, once late, remind them every (days)", null, 1, 30),
    num("reviews_overdue_days", "Tell HR when a review is this many days late", null, 1, 90),
    num("reviews_ack_days", "Remind the employee to acknowledge after (days)", null, 1, 30),
    h("h3", null, "Forms"),
    Object.entries(data.kinds).map(([kind, words]) => h("label", null, words,
      h("select", { name: `form_${kind}` }, Object.entries(forms).map(([family, name]) =>
        h("option", { value: family, selected: v.reviews_forms[kind] === family }, name))))),
    h("p", null, h("a", { href: "#/reviews/forms" }, "Edit review forms")),
    h("p", { class: "muted small" }, "Reviews aren't linked to pay."),
    h("div", { class: "actions" }, h("button", { type: "submit", class: "primary" }, "Save")));
    box.append(h("h2", null, "Reviews"), form);
  });
})();
