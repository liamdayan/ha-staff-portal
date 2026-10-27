// HR portal: certificates. Loads after hr.js and uses window.HRPortal. Everything shown is plain
// text (h() makes text nodes), with no inline handlers or styles in markup (the page's
// Content-Security-Policy). Pages: My certificates; Certificates (HR's compliance board, a
// manager's team board); adding a card (camera first); HR's check screen; and HR settings.
(() => {
  "use strict";

  const H = window.HRPortal;
  if (!H) return;
  const { h, api, toast, ask, openDialog, show, when, pageHead, emptyState, initial } = H;

  // This part's stylesheet, from this address like hr.css (so the page's policy allows it).
  const version = (document.querySelector('link[href*="/static/hr.css"]')?.getAttribute("href") || "").split("v=")[1] || "";
  document.head.append(h("link", { rel: "stylesheet", href: `/static/hr-certificates.css?v=${encodeURIComponent(version)}` }));

  const enc = encodeURIComponent;
  const PHOTO_LIMIT = 15 * 1024 * 1024;
  const HEIC = "That photo is in Apple's HEIC format. Take the photo again with the camera button.";
  const day = (iso) => {
    if (!iso) return "";
    const [y, m, d] = String(iso).split("-").map(Number);
    return `${m}/${d}/${y}`;
  };
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  // Statuses as the design system's badges: valid green, soon or waiting amber, lapsed red.
  const TONE = { valid: "ok", expiring: "warn", due: "warn", waiting: "warn", expired: "bad", missing: "bad",
    waived: "quiet", confirmed: "ok", rejected: "bad", replaced: "quiet", withdrawn: "quiet" };
  const badge = (status, word) => h("span", { class: `badge ${TONE[status] || "quiet"}` }, word);
  const PROBLEMS = ["expired", "missing", "due"];

  async function run(fn, okText) {
    try {
      const result = await fn();
      if (result !== false && okText) toast(okText);
      return result;
    } catch (err) {
      if (err.message !== "Signed out" && err.message !== "Moved on") toast(err.message, true);
      return false;
    }
  }

  // ---------------------------------------------------------------- statuses and cards

  function statusText(s) {
    if (s.status === "waived") return `Waived until ${day(s.waived_until)}`;
    if (s.status === "valid" || s.status === "expiring") {
      const left = s.days_left <= 60 ? ` (${plural(s.days_left, "day")} left)` : "";
      return `Expires ${day(s.expires)}${left}`;
    }
    if (s.status === "expired") return `Expired ${day(s.expires)}`;
    if (s.status === "due") return `Needed by ${day(s.due)}`;
    if (s.status === "missing") return s.due ? `Was needed by ${day(s.due)}` : "None on file";
    if (s.status === "waiting") return "A card is waiting for HR to check";
    return "";
  }

  // "May not serve alcohol" and the OLCC notes, as a row of badges under the line.
  function flags(s) {
    const chips = [s.flag ? h("span", { class: "badge bad cert-flag" }, s.flag) : null,
      s.temporary ? h("span", { class: "badge quiet" }, "Temporary permit") : null,
      s.under_21 ? h("span", { class: "badge warn" }, "Under 21: may not mix drinks or bartend") : null].filter(Boolean);
    return chips.length ? h("div", { class: "chips" }, chips) : null;
  }

  function readLine(c) {
    if (c.status !== "waiting") return null;
    if (c.read_state === "pending") return "Reading the date…";
    if (c.read_state === "done" && c.read_expires) {
      return `We read: expires ${day(c.read_expires)}${c.read_how === "calculated" ? " (worked out from the issue date)" : ""}. HR will confirm.`;
    }
    if (c.read_state === "done") return "We couldn't find the expiry date. HR will check it.";
    return "HR will check the date.";
  }

  async function showPhoto(c) {
    const body = c.pdf
      ? H.pdfPages(c.photo, `The ${c.type} (PDF)`)
      : [h("div", { class: "cert-photo small-photo" }, h("img", { src: c.photo, alt: `Photo of the ${c.type}` }))];
    await openDialog(c.type, body, [{ label: "Close", value: "ok", class: "primary" }]);
  }

  // The body shared by My certificates, a person's page and the check screen's side panel.
  function certBody(view, redraw) {
    const nameOf = (key) => view.types.find((t) => t.key === key)?.name || key;
    const self = view.relation === "self";
    const nodes = [];
    if (view.statuses.length) {
      nodes.push(h("ul", { class: "list" }, view.statuses.map((s) => h("li", { class: "cert-item" }, h("div", { class: "item" },
        h("div", { class: "cert-head" }, h("span", { class: "title" }, s.name), badge(s.status, s.status_word)),
        h("div", { class: "sub" }, statusText(s), s.via ? ` · ${nameOf(s.via)} accepted` : "",
          s.required ? "" : " · Not needed for their job"),
        flags(s),
        view.can_add && s.status !== "waiting" && s.status !== "waived" ? h("div", { class: "actions cert-buttons" },
          h("a", { class: "button small-btn" + (PROBLEMS.includes(s.status) ? " primary" : ""),
            href: `#/card/add/${enc(view.person.id)}?type=${enc(s.type_key)}` },
          s.card_id ? (self ? "Renew" : "Add a new card") : (self ? "Add my card" : "Add a card"))) : null,
        view.can_check && s.waiting_id ? h("div", { class: "actions cert-buttons" },
          h("a", { class: "button small-btn primary", href: `#/certificates/check/${enc(view.person.id)}/${s.waiting_id}` }, "Check it")) : null)))));
    } else {
      nodes.push(emptyState("No certificates are needed for their job."));
    }
    const recent = view.cards.filter((c) => c.status === "waiting" || c.status === "rejected"
      || (c.status === "confirmed" && (self || view.relation === "hr")));
    if (recent.length) {
      nodes.push(h("h3", { class: "eyebrow cert-group" }, "Cards added"), h("ul", { class: "list" }, recent.map((c) => h("li", { class: "cert-item" }, h("div", { class: "item" },
        h("div", { class: "cert-head" }, h("span", { class: "title" }, c.type), badge(c.status, c.status_word)),
        h("div", { class: "sub" }, [`Added ${when(c.added_at)}`, c.added_by ? `by ${c.added_by}` : null,
          c.expires ? `expires ${day(c.expires)}` : null, c.number ? `number ${c.number}` : null].filter(Boolean).join(" · ")),
        readLine(c) ? h("p", { class: "small cert-line" }, readLine(c)) : null,
        c.reason ? h("p", { class: "warn small cert-line" }, `Not accepted: ${c.reason}`) : null,
        c.photo || (view.can_check && c.status === "waiting") ? h("div", { class: "actions cert-buttons" },
          c.photo ? h("button", { type: "button", class: "small-btn", onclick: () => showPhoto(c) }, c.pdf ? "PDF" : "See the photo") : null,
          view.can_check && c.status === "waiting" ? h("a", { class: "button small-btn primary", href: `#/certificates/check/${enc(view.person.id)}/${c.id}` }, "Check it") : null) : null)))));
    }
    if (view.waivers.length) {
      nodes.push(h("h3", { class: "eyebrow cert-group" }, "Waived"), h("ul", { class: "list" }, view.waivers.map((w) => h("li", { class: "cert-item" },
        h("div", { class: "item split" },
          h("div", { class: "item-text" },
            h("div", { class: "title" }, `${w.type}, until ${day(w.ends)}`),
            w.reason ? h("div", { class: "sub" }, `${w.reason}${w.set_by ? ` (${w.set_by})` : ""}`) : null),
          view.can_waive ? h("button", { type: "button", class: "small-btn", onclick: () => run(async () => {
            await api("DELETE", `/api/people/${enc(view.person.id)}/certificates/waivers/${w.id}`);
            await redraw();
          }, "The waiver has ended.") }, "End the waiver") : null)))));
    }
    const buttons = [];
    if (view.can_add) {
      buttons.push(h("a", { class: "button", href: `#/card/add/${enc(view.person.id)}` }, self ? "Add or renew a card" : "Add a card for them"));
    }
    if (view.can_waive) buttons.push(h("button", { type: "button", onclick: () => waive(view, redraw) }, "Waive a certificate"));
    if (buttons.length) nodes.push(h("div", { class: "actions" }, buttons));
    return nodes;
  }

  async function waive(view, redraw) {
    const type = h("select", { name: "type" }, view.types.map((t) => h("option", { value: t.key }, t.name)));
    const reason = h("textarea", { name: "reason", rows: "3", maxlength: "300" });
    const ends = h("input", { type: "date", name: "ends" });
    const choice = await openDialog("Waive a certificate", [
      h("p", { class: "muted small" }, `${view.person.name} won't be counted as missing it until the waiver ends. Only HR sees why.`),
      h("label", null, "Certificate", type), h("label", null, "Why", reason), h("label", null, "Until", ends)],
    [{ label: "Cancel", value: "cancel" }, { label: "Waive", value: "ok", class: "primary" }]);
    if (choice !== "ok") return;
    const r = await run(() => api("POST", `/api/people/${enc(view.person.id)}/certificates/waivers`,
      { type: type.value, reason: reason.value, ends: ends.value }));
    if (r) {
      toast(r.message);
      await redraw();
    }
  }

  H.registerSection("certificates", (box, s, person, redraw) => {
    const problems = s.certs.statuses.filter((x) => PROBLEMS.includes(x.status) || x.flag).length;
    const waiting = s.certs.statuses.filter((x) => x.status === "waiting").length;
    const head = h("div", { class: "card-head" }, h("h2", null, s.title),
      problems ? h("span", { class: "badge bad" }, `${problems} to sort out`)
        : waiting ? h("span", { class: "badge warn" }, "Waiting for HR") : null);
    box.append(...[head, ...certBody(s.certs, redraw)].flat().filter(Boolean));
  });

  // ---------------------------------------------------------------- My certificates

  async function renderMine() {
    const view = await api("GET", `/api/people/${enc(H.me.id)}/certificates`);
    const again = () => renderMine();
    show(pageHead("My certificates", "Cards your job needs, such as a Food Handler Card or an OLCC Service Permit. Add a photo of a new card here: HR checks it.",
      H.me.staff ? h("div", { class: "page-actions" }, h("a", { class: "button small-btn", href: "#/certificates" }, "See the certificate board")) : null),
    h("section", { class: "card" }, certBody(view, again)));
  }

  H.registerPage("my-certificates", {
    get nav() { return H.me && !H.me.staff ? "My certificates" : null; },
    render: () => renderMine(),
  });

  // ---------------------------------------------------------------- adding a card

  function fileProblem(file) {
    if (!file) return null;
    if (/hei[cf]/i.test(file.type) || /\.hei[cf]$/i.test(file.name)) return HEIC;
    if (file.size > PHOTO_LIMIT) return "That file is too big (more than 15 MB).";
    return null;
  }

  function preview(file, box) {
    box.replaceChildren();
    if (!file) return;
    if (file.type === "application/pdf") {
      box.append(h("p", { class: "small" }, `PDF: ${file.name}`));
      return;
    }
    const reader = new FileReader();
    reader.onload = () => box.replaceChildren(h("img", { src: String(reader.result), alt: "The photo you chose" }));
    reader.readAsDataURL(file);
  }

  // A file chooser styled as a button: the camera (asking iPhones for JPEG), or a file.
  function chooser(label, accept, capture, onPick) {
    const input = h("input", { type: "file", accept, class: "cert-input", capture: capture ? "environment" : null });
    input.addEventListener("change", () => onPick(input.files[0] || null, input));
    return h("label", { class: "button cert-file" }, label, input);
  }

  async function upload(path, form) {
    const res = await fetch(path, { method: "POST", credentials: "same-origin", headers: { "X-Requested-With": "hr-portal" }, body: form });
    let data = {};
    try { data = await res.json(); } catch { data = { error: `Something went wrong (${res.status}).` }; }
    if (res.status === 401) {
      await api("GET", "/api/me").catch(() => {});   // shows the sign-in page
      throw new Error("Signed out");
    }
    if (!res.ok) throw new Error(data.error || `Something went wrong (${res.status}).`);
    return data;
  }

  async function renderAdd(personId, params) {
    const view = await api("GET", `/api/people/${enc(personId)}/certificates`);
    const self = view.relation === "self";
    if (!view.can_add) {
      return show(h("section", { class: "card" }, emptyState("You can't add a card for them."),
        h("div", { class: "actions" }, h("a", { class: "button", href: "#/" }, "Go home"))));
    }
    const needed = view.statuses.map((s) => s.type_key);
    const wanted = params.get("type") || needed.find((k) => !["valid", "waiting", "waived"].includes(view.statuses.find((s) => s.type_key === k)?.status)) || needed[0];
    const type = h("select", { name: "type", required: true }, view.types.map((t) => h("option", { value: t.key, selected: t.key === wanted }, t.name)));
    const picked = { front: null, back: null };
    const error = h("p", { class: "error", role: "alert", hidden: true });
    const frontBox = h("div", { class: "cert-preview" });
    const backBox = h("div", { class: "cert-preview" });
    const fail = (text) => { error.textContent = text; error.hidden = !text; };
    const pick = (side, box) => (file, input) => {
      const problem = fileProblem(file);
      if (problem) {
        input.value = "";
        picked[side] = null;
        preview(null, box);
        return fail(problem);
      }
      fail("");
      picked[side] = file;
      preview(file, box);
    };
    const send = h("button", { type: "submit", class: "primary" }, "Send to HR");
    const form = h("form", { onsubmit: async (e) => {
      e.preventDefault();
      if (!picked.front) return fail("Take a photo of the card first, or choose its PDF.");
      fail("");
      send.disabled = true;
      send.textContent = "Sending…";
      try {
        const body = new FormData();
        body.append("type", type.value);
        body.append("front", picked.front, picked.front.name || "front.jpg");
        if (picked.back) body.append("back", picked.back, picked.back.name || "back.jpg");
        const r = await upload(`/api/people/${enc(personId)}/certificates`, body);
        if (view.relation === "hr") location.hash = `#/certificates/check/${enc(personId)}/${r.card.id}`;
        else await showReading(view, r.card);
      } catch (err) {
        if (err.message !== "Signed out") fail(err.message);
      } finally {
        send.disabled = false;
        send.textContent = "Send to HR";
      }
    } },
    h("label", null, "Which card", type),
    h("div", { class: "sub-card cert-side-box" },
      h("h3", null, "Front of the card"),
      h("div", { class: "actions" },
        chooser("Take a photo", "image/jpeg", true, pick("front", frontBox)),
        chooser("Choose a file", "image/jpeg,image/png,image/webp,application/pdf", false, pick("front", frontBox))),
      frontBox),
    h("div", { class: "sub-card cert-side-box" },
      h("h3", null, "Back of the card (if it has dates on it)"),
      h("div", { class: "actions" }, chooser("Take a photo of the back", "image/jpeg", true, pick("back", backBox))),
      backBox),
    error,
    h("div", { class: "actions full" }, send));
    show(h("header", { class: "page-head" },
      h("a", { class: "back-link", href: self ? "#/my-certificates" : `#/people/${enc(personId)}` }, "Back"),
      h("h1", { class: "page-title" }, self ? "Add or renew a card" : `Add a card for ${view.person.name}`),
      h("p", { class: "page-sub" }, view.relation === "hr"
        ? "Next you check the date and confirm it."
        : "HR checks every card before it counts.")),
    h("section", { class: "card" }, h("h2", null, "Photo tips"), h("ul", { class: "cert-tips" }, view.tips.map((t) => h("li", null, t))),
      view.reading ? null : h("p", { class: "muted small" }, "HR types the date from the photo.")),
    h("section", { class: "card" }, form));
  }

  // "We read: expires 6/14/2029. HR will confirm." The page asks for a few seconds while Google
  // reads the card, then stops (HR confirms either way).
  async function showReading(view, card) {
    const line = h("p", { class: "cert-reading", role: "status" }, readLine(card) || "");
    const where = location.hash;
    show(pageHead("Sent to HR", `${card.type} for ${view.relation === "self" ? "you" : view.person.name}.`),
      h("section", { class: "card" }, line,
        h("div", { class: "actions full" },
          h("a", { class: "button primary", href: view.relation === "self" ? "#/my-certificates" : `#/people/${enc(view.person.id)}` }, "Done"))));
    for (let i = 0; i < 20 && card.read_state === "pending"; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      if (location.hash !== where) return;
      try {
        card = (await api("GET", `/api/people/${enc(view.person.id)}/certificates/${card.id}/reading`, undefined, { background: true })).card;
      } catch { return; }
      line.textContent = readLine(card) || "";
    }
    if (card.read_state === "pending") line.textContent = "We're still reading it. HR will confirm the date.";
  }

  H.registerPage("card", {
    render: async (_main, parts, params) => {
      if (parts[0] === "add" && parts[1]) return renderAdd(decodeURIComponent(parts[1]), params);
      show(h("section", { class: "card" }, emptyState("That page isn't here."),
        h("div", { class: "actions" }, h("a", { class: "button", href: "#/" }, "Go home"))));
    },
  });

  // ---------------------------------------------------------------- the boards

  function filterForm(data, params) {
    const sel = (name, label, options) => h("label", null, label, h("select", { name },
      options.map(([value, text]) => h("option", { value, selected: (params.get(name) || "") === String(value) }, text))));
    const form = h("form", { class: "row cert-filters", onchange: () => form.requestSubmit(), onsubmit: (e) => {
      e.preventDefault();
      const q = new URLSearchParams();
      for (const name of ["group", "type", "status", "within"]) if (form.elements[name].value) q.set(name, form.elements[name].value);
      location.hash = `#/certificates${q.toString() ? `?${q}` : ""}`;
    } },
    sel("group", "Department", [["", "All"], ...data.departments.map((d) => [d.group, d.name])]),
    sel("type", "Certificate", [["", "All"], ...data.types.map((t) => [t.key, t.name])]),
    sel("status", "Status", [["", "All"], ["problems", "Needs attention"], ...Object.entries(data.statuses)]),
    sel("within", "Runs out within", [["", "Any time"], ...data.within.map((d) => [d, `${d} days`])]));
    return form;
  }

  // A person and one certificate: their initial, name (a link to their page), the status on the right.
  function boardRow(r) {
    return h("li", { class: "cert-item" }, h("div", { class: "item with-avatar" },
      h("span", { class: "avatar", "aria-hidden": "true" }, initial(r.name)),
      h("div", { class: "item-text" },
        h("div", { class: "cert-head" }, h("a", { class: "title", href: `#/people/${enc(r.person_id)}` }, r.name), badge(r.status, r.status_word)),
        h("div", { class: "sub" }, [r.type, statusText(r), r.departments.join(", ")].filter(Boolean).join(" · ")),
        flags(r))));
  }

  async function renderBoard(params) {
    const q = new URLSearchParams();
    for (const name of ["group", "type", "status", "within"]) if (params.get(name)) q.set(name, params.get(name));
    const data = await api("GET", `/api/certificates/board${q.toString() ? `?${q}` : ""}`);
    const counts = Object.entries(data.counts).filter(([, n]) => n);
    const statusLink = (k) => {
      const p = new URLSearchParams(q);
      p.set("status", k);
      return `#/certificates?${p}`;
    };
    const actions = h("div", { class: "page-actions" },
      data.hr && data.waiting ? h("a", { class: "button small-btn primary", href: "#/certificates/check" }, `${data.waiting} to check`) : null,
      h("a", { class: "button small-btn", href: "#/my-certificates" }, "My certificates"),
      data.hr ? h("button", { type: "button", class: "small-btn", onclick: () => run(async () => {
        const r = await api("POST", "/api/certificates/export", Object.fromEntries(q));
        const link = h("a", { href: URL.createObjectURL(new Blob([r.csv], { type: "text/csv" })), download: r.name });
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(link.href), 30000);
      }, "Downloaded.") }, "Download (spreadsheet)") : null);
    show(pageHead(data.hr ? "Certificate board" : "Team certificates",
      data.hr ? "Everyone's certificates and when they run out." : "The certificates your team needs. Photos are only seen by the person and HR.",
      actions),
    data.banners.map((b) => h("p", { class: "note bad", role: "alert" }, b)),
    counts.length ? h("div", { class: "stats" }, counts.map(([k, n]) => h("a", {
      class: TONE[k] === "bad" ? "stat alert" : "stat", href: statusLink(k), "aria-label": `${n} ${data.statuses[k].toLowerCase()}` },
    h("span", { class: "stat-value" }, String(n)), h("span", { class: "stat-label" }, data.statuses[k])))) : null,
    h("section", { class: "card" }, filterForm(data, params),
      counts.length ? null : h("p", { class: "muted small" }, "No one needs a certificate yet. HR chooses who needs each one in HR settings.")),
    h("section", { class: "card flush" },
      data.rows.length ? h("ul", { class: "list" }, data.rows.map(boardRow))
        : emptyState(data.total ? "No one matches." : "Nothing to show yet.")));
  }

  // ---------------------------------------------------------------- HR: checking cards

  async function renderWaiting() {
    const data = await api("GET", "/api/certificates/waiting");
    show(h("header", { class: "page-head" },
      h("a", { class: "back-link", href: "#/certificates" }, "Certificate board"),
      h("h1", { class: "page-title" }, "Cards to check"),
      h("p", { class: "page-sub" }, "Oldest first. Another HR admin checks your own.")),
    h("section", { class: "card flush" },
      data.cards.length ? h("ul", { class: "list" }, data.cards.map((c) => h("li", null,
        h("a", { class: "item with-avatar", href: `#/certificates/check/${enc(c.person_id)}/${c.id}` },
          h("span", { class: "avatar", "aria-hidden": "true" }, initial(c.name)),
          h("div", { class: "item-text" },
            h("div", { class: "title" }, `${c.name}: ${c.type}`),
            h("div", { class: "sub" }, [`Added ${when(c.added_at)}`, c.added_by ? `by ${c.added_by}` : null].filter(Boolean).join(" · ")),
            readLine(c) ? h("div", { class: "sub" }, readLine(c)) : null)))))
        : emptyState("Nothing to check.", "Cards people add wait here for HR.")));
  }

  async function renderCheck(personId, cardId) {
    const d = await api("GET", `/api/people/${enc(personId)}/certificates/${enc(cardId)}`);
    const c = d.card;
    // The page's head: back to the cards, the person's initial, name and card, and a link to their page.
    const head = (sub) => h("header", { class: "page-head with-avatar" },
      h("a", { class: "back-link", href: "#/certificates/check" }, "Cards to check"),
      h("span", { class: "avatar lg", "aria-hidden": "true" }, initial(d.person.name)),
      h("h1", { class: "page-title" }, `${d.person.name}: ${d.type.name}`),
      sub ? h("p", { class: "page-sub" }, sub) : null,
      h("div", { class: "page-actions" }, h("a", { class: "button small-btn", href: `#/people/${enc(personId)}` }, `${d.person.name}'s page`)));
    const added = [`Added ${when(c.added_at)}`, c.added_by ? `by ${c.added_by}` : null].filter(Boolean).join(" · ");
    // The photo: zoom and turn (turning asks the server for an upright copy).
    let zoom = 1;
    let turn = 0;
    const img = c.photo && !c.pdf ? h("img", { src: c.photo, alt: `Photo of ${d.person.name}'s ${d.type.name}` }) : null;
    const setZoom = (z) => { zoom = Math.min(4, Math.max(1, z)); img.style.width = `${zoom * 100}%`; };
    const photo = h("section", { class: "card cert-side" }, h("h2", null, "The card"),
      img ? [h("div", { class: "cert-photo" }, img), h("div", { class: "actions cert-buttons" },
        h("button", { type: "button", class: "small-btn", onclick: () => setZoom(zoom - 0.5), "aria-label": "Zoom out" }, "−"),
        h("button", { type: "button", class: "small-btn", onclick: () => setZoom(zoom + 0.5), "aria-label": "Zoom in" }, "+"),
        // Turning works while the photo waits here; a confirmed card opens as its document.
        c.status === "waiting" ? h("button", { type: "button", class: "small-btn", onclick: () => { turn = (turn + 90) % 360; img.src = `${c.photo}?turn=${turn}`; } }, "Turn") : null,
        h("a", { class: "button small-btn", href: c.photo, target: "_blank", rel: "noopener" }, "Full size"))]
        : c.photo ? H.pdfPages(c.photo, `${d.person.name}'s ${d.type.name} (PDF)`)
          : emptyState("The photo isn't kept here any more."));
    const read = h("section", { class: "card cert-side" }, h("h2", null, "What Google read"),
      d.segments.length ? h("div", { class: "cert-text" }, d.segments.map((s) => (s.d ? h("mark", null, s.t) : s.l ? h("strong", null, s.t) : s.t)))
        : h("p", { class: "note" }, c.read_note || (c.read_state === "pending" ? "Still reading it. Type the date from the photo, or come back in a minute." : "Nothing was read.")),
      d.warnings.length ? h("ul", { class: "cert-warnings" }, d.warnings.map((w) => h("li", { class: "warn small" }, w))) : null);
    if (c.status !== "waiting") {
      return show(head(added), h("p", { class: "note" }, `This card was already decided: ${c.status_word.toLowerCase()}.`), photo);
    }
    const expires = h("input", { type: "date", name: "expires", required: true, value: d.suggest.expires || "" });
    const issued = h("input", { type: "date", name: "issued", value: d.suggest.issued || "" });
    const number = h("input", { name: "number", maxlength: "40", autocomplete: "off" });
    const tick = (name, label, checked) => h("label", { class: "check" }, h("input", { type: "checkbox", name, checked }), label);
    const next = () => { location.hash = d.next || "#/certificates/check"; };
    const form = h("form", { onsubmit: async (e) => {
      e.preventDefault();
      const f = form.elements;
      const r = await run(() => api("POST", `/api/people/${enc(personId)}/certificates/${c.id}/confirm`, {
        expires: f.expires.value, issued: f.issued.value, number: f.number.value,
        temporary: !!f.temporary?.checked, under_21: !!f.under_21?.checked, checked_issuer: f.checked_issuer.checked }));
      if (r) {
        toast(r.message);
        next();
      }
    } },
    h("label", null, "Expires", h("small", null, d.suggest.expires ? `${c.how_word || "Read from card"}: ${day(d.suggest.expires)}. Correct it if it's wrong.` : "Not found on the card: type it from the photo."), expires),
    h("div", { class: "row cert-row" },
      h("label", null, "Issued", h("small", null, "If it's on the card"), issued),
      h("label", null, d.type.olcc ? "Permit number" : "Card number", h("small", null, "If it's on the card"), number)),
    d.type.olcc ? [tick("temporary", "It's a temporary permit", d.suggest.temporary),
      tick("under_21", "Under 21: may not mix drinks or bartend", d.suggest.under_21)] : null,
    tick("checked_issuer", d.type.olcc ? "Checked with OLCC" : "Checked with the issuer", false),
    h("div", { class: "actions" },
      h("button", { type: "button", class: "danger", onclick: async () => {
        const reason = await ask("Don't accept this card", `Why? ${d.person.name} sees this, so say what to fix.`);
        if (reason === null) return;
        const r = await run(() => api("POST", `/api/people/${enc(personId)}/certificates/${c.id}/reject`, { reason }));
        if (r) {
          toast(r.message);
          next();
        }
      } }, "Don't accept"),
      h("button", { type: "submit", class: "primary" }, "Confirm")));
    show(head(added),
      d.current ? h("p", { class: "note" }, `It replaces the card that expires ${day(d.current.expires)}.`) : null,
      h("div", { class: "cert-check" }, photo, read),
      h("section", { class: "card" }, h("h2", null, "Confirm the dates"), form));
  }

  H.registerPage("certificates", {
    nav: "Certificates",
    who: (me) => me.staff,
    render: async (_main, parts, params) => {
      if (parts[0] === "check" && H.me.hr) return parts[2] ? renderCheck(decodeURIComponent(parts[1]), parts[2]) : renderWaiting();
      return renderBoard(params);
    },
  });

  // ---------------------------------------------------------------- HR settings

  function typeForm(t, data, again) {
    const groups = new Set(t.groups_confirmed ? t.groups : t.suggested);
    const chosenPeople = new Set(t.people);
    const peopleList = h("ul", { class: "list" });
    const drawPeople = () => peopleList.replaceChildren(...[...chosenPeople].map((id) => h("li", null, h("div", { class: "item split" },
      h("span", { class: "title" }, data.people.find((p) => p.id === id)?.name || id),
      h("button", { type: "button", class: "small-btn", onclick: () => { chosenPeople.delete(id); drawPeople(); } }, "Remove")))));
    drawPeople();
    const addPerson = h("select", { "aria-label": "Add someone who needs it" }, h("option", { value: "" }, "Add someone…"),
      data.people.map((p) => h("option", { value: p.id }, p.name)));
    addPerson.addEventListener("change", () => {
      if (addPerson.value) chosenPeople.add(addPerson.value);
      addPerson.value = "";
      drawPeople();
    });
    const others = data.types.filter((o) => o.key !== t.key);
    const form = h("form", { onsubmit: (e) => {
      e.preventDefault();
      const f = form.elements;
      const body = {
        name: f.name.value, lasts_years: f.lasts_years.value, due_days: f.due_days.value, remind: f.remind.value,
        lapse_message: f.lapse_message.value, flag_until_valid: f.flag_until_valid.checked, olcc: f.olcc.checked,
        reader: f.reader.value, active: f.active.checked, people: [...chosenPeople],
        groups: data.departments.filter((d) => f[`g-${d.group}`]?.checked).map((d) => d.group),
        accepts: others.filter((o) => f[`a-${o.key}`]?.checked).map((o) => o.key),
      };
      run(async () => {
        await api("PUT", `/api/settings/certificates/types/${t.id}`, body);
        await again();
      }, "Saved.");
    } },
    h("label", null, "Name", h("input", { name: "name", value: t.name, maxlength: "60", required: true })),
    h("div", { class: "row" },
      h("label", null, "Lasts (years)", h("small", null, "0: as printed"), h("input", { name: "lasts_years", type: "number", min: "0", max: "20", value: t.lasts_years })),
      h("label", null, "Days to get one", h("small", null, "After starting"), h("input", { name: "due_days", type: "number", min: "0", max: "365", value: t.due_days }))),
    h("label", null, "Heads-up days before it runs out", h("small", null, "For example 60, 30, 14, 7"), h("input", { name: "remind", value: t.remind.join(", "), inputmode: "numeric" })),
    h("label", null, "Message while it's expired or missing", h("small", null, "Shown on every board, for example “May not serve alcohol”"), h("input", { name: "lapse_message", value: t.lapse_message, maxlength: "80" })),
    h("label", { class: "check" }, h("input", { type: "checkbox", name: "flag_until_valid", checked: !!t.flag_until_valid }), "Show the message until it's valid, even during a new hire's days"),
    h("label", { class: "check" }, h("input", { type: "checkbox", name: "olcc", checked: !!t.olcc }), "Ask about OLCC temporary permits and under 21"),
    h("label", null, "How its dates are read", h("select", { name: "reader" }, Object.entries(data.readers).map(([k, v]) => h("option", { value: k, selected: k === t.reader }, v)))),
    h("h3", null, "Departments that need it"),
    t.groups_confirmed ? null : h("p", { class: "warn small" }, t.suggested.length
      ? "Suggested from the department names. Check them and save: nobody needs it until you do."
      : "Choose the departments and save: nobody needs it until you do."),
    data.departments.length ? data.departments.map((d) => h("label", { class: "check" },
      h("input", { type: "checkbox", name: `g-${d.group}`, checked: groups.has(d.group) }), d.name))
      : h("p", { class: "muted small" }, "No departments yet: they come from Connecteam's smart groups."),
    h("h3", null, "People who need it too"), peopleList, addPerson,
    others.length ? [h("h3", null, "Accepted in its place"), ...others.map((o) => h("label", { class: "check" },
      h("input", { type: "checkbox", name: `a-${o.key}`, checked: t.accepts.includes(o.key) }), o.name))] : null,
    h("h3", null, "Tracking"),
    h("label", { class: "check" }, h("input", { type: "checkbox", name: "active", checked: !!t.active }), "Track this certificate"),
    h("div", { class: "actions" }, h("button", { type: "submit", class: "primary" }, `Save ${t.name}`)));
    const chips = [t.active ? null : h("span", { class: "badge quiet" }, "Not tracked"),
      t.groups_confirmed ? null : h("span", { class: "badge warn" }, "Departments to confirm")].filter(Boolean);
    return h("details", { class: "cert-type", open: !t.groups_confirmed || null },
      h("summary", null, h("span", { class: "title" }, t.name), chips.length ? h("span", { class: "chips" }, chips) : null), form);
  }

  H.registerSettings("certificates", (box, data, again) => {
    const remind = h("form", { class: "row cert-inline", onsubmit: (e) => {
      e.preventDefault();
      run(() => api("PUT", "/api/settings/certificates", { remind_every: remind.elements.remind_every.value }), "Saved.");
    } }, h("label", null, "Remind about expired or missing cards every (days)", h("small", null, "1 to 14. The person, their managers and HR are reminded."),
      h("input", { name: "remind_every", type: "number", min: "1", max: "14", value: data.remind_every })),
    h("button", { type: "submit" }, "Save"));
    const add = h("form", { class: "row cert-inline", onsubmit: (e) => {
      e.preventDefault();
      run(async () => {
        await api("POST", "/api/settings/certificates/types", { name: add.elements.name.value });
        await again();
      }, "Added. Set it up below.");
    } }, h("label", null, "Add a kind of certificate", h("small", null, "Such as CPR and First Aid, or a pool operator's certificate"),
      h("input", { name: "name", maxlength: "60", required: true })),
    h("button", { type: "submit" }, "Add"));
    box.append(...[h("h2", null, "Certificates"),
      h("p", { class: "muted small" }, "Which certificates people need and when. Cards are checked by HR before they count."),
      remind,
      h("div", { class: "cert-types" }, data.types.map((t) => typeForm(t, data, again))),
      add].flat().filter(Boolean));
  });
})();
