// HR portal: certificates. Loads after hr.js and uses window.HRPortal. Everything shown is plain
// text (h() makes text nodes), with no inline handlers or styles in markup (the page's
// Content-Security-Policy). Pages: My certificates; Certificates (HR's compliance board, a
// manager's team board); adding a card (camera first); HR's check screen; and HR settings.
(() => {
  "use strict";

  const H = window.HRPortal;
  if (!H) return;
  const { h, api, toast, ask, openDialog, show, when } = H;

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
  const badge = (status, word) => h("span", { class: `cert-badge s-${status}` }, word);

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

  function flags(s) {
    return [s.flag ? h("p", { class: "cert-flag" }, s.flag) : null,
      s.temporary ? h("span", { class: "badge" }, "Temporary permit") : null,
      s.under_21 ? h("span", { class: "badge bad" }, "Under 21: may not mix drinks or bartend") : null];
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
      ? [h("p", null, "It's a PDF, so it's downloaded rather than shown here."),
        h("a", { class: "button", href: c.photo, download: "" }, "Download the PDF")]
      : [h("div", { class: "cert-photo small-photo" }, h("img", { src: c.photo, alt: `Photo of the ${c.type}` }))];
    await openDialog(c.type, body, [{ label: "Close", value: "ok", class: "primary" }]);
  }

  // The body shared by My certificates, a person's page and the check screen's side panel.
  function certBody(view, redraw) {
    const nameOf = (key) => view.types.find((t) => t.key === key)?.name || key;
    const self = view.relation === "self";
    const nodes = [];
    if (view.statuses.length) {
      nodes.push(h("ul", { class: "list" }, view.statuses.map((s) => h("li", { class: "cert-item" },
        h("div", { class: "cert-head" }, h("span", { class: "title" }, s.name), badge(s.status, s.status_word)),
        h("div", { class: "sub" }, statusText(s), s.via ? ` · ${nameOf(s.via)} accepted` : "",
          s.required ? "" : " · Not needed for their job"),
        flags(s),
        view.can_add && s.status !== "waiting" && s.status !== "waived" ? h("div", { class: "actions" },
          h("a", { class: "button" + (["expired", "missing", "due"].includes(s.status) ? " primary-link" : ""),
            href: `#/card/add/${enc(view.person.id)}?type=${enc(s.type_key)}` },
          s.card_id ? (self ? "Renew" : "Add a new card") : (self ? "Add my card" : "Add a card"))) : null,
        view.can_check && s.waiting_id ? h("div", { class: "actions" },
          h("a", { class: "button", href: `#/certificates/check/${enc(view.person.id)}/${s.waiting_id}` }, "Check it")) : null))));
    } else {
      nodes.push(h("p", { class: "muted" }, "No certificates are needed for their job."));
    }
    const recent = view.cards.filter((c) => c.status === "waiting" || c.status === "rejected"
      || (c.status === "confirmed" && (self || view.relation === "hr")));
    if (recent.length) {
      nodes.push(h("h3", null, "Cards added"), h("ul", { class: "list" }, recent.map((c) => h("li", { class: "cert-item" },
        h("div", { class: "cert-head" }, h("span", { class: "title" }, c.type), h("span", { class: "muted small" }, c.status_word)),
        h("div", { class: "sub" }, [`Added ${when(c.added_at)}`, c.added_by ? `by ${c.added_by}` : null,
          c.expires ? `expires ${day(c.expires)}` : null, c.number ? `number ${c.number}` : null].filter(Boolean).join(" · ")),
        readLine(c) ? h("p", { class: "small" }, readLine(c)) : null,
        c.reason ? h("p", { class: "warn" }, `Not accepted: ${c.reason}`) : null,
        h("div", { class: "actions" },
          c.photo ? h("button", { type: "button", onclick: () => showPhoto(c) }, c.pdf ? "PDF" : "See the photo") : null,
          view.can_check && c.status === "waiting" ? h("a", { class: "button", href: `#/certificates/check/${enc(view.person.id)}/${c.id}` }, "Check it") : null)))));
    }
    if (view.waivers.length) {
      nodes.push(h("h3", null, "Waived"), h("ul", { class: "list" }, view.waivers.map((w) => h("li", { class: "cert-item" },
        h("div", { class: "title" }, `${w.type}, until ${day(w.ends)}`),
        w.reason ? h("div", { class: "sub" }, `${w.reason}${w.set_by ? ` (${w.set_by})` : ""}`) : null,
        view.can_waive ? h("button", { type: "button", class: "linkish", onclick: () => run(async () => {
          await api("DELETE", `/api/people/${enc(view.person.id)}/certificates/waivers/${w.id}`);
          await redraw();
        }, "The waiver has ended.") }, "End the waiver") : null))));
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
    box.append(...[h("h2", null, s.title), ...certBody(s.certs, redraw)].flat().filter(Boolean));
  });

  // ---------------------------------------------------------------- My certificates

  async function renderMine() {
    const view = await api("GET", `/api/people/${enc(H.me.id)}/certificates`);
    const again = () => renderMine();
    show(h("section", { class: "card" }, h("h1", null, "My certificates"),
      h("p", { class: "muted small" }, "Cards your job needs, such as a Food Handler Card or an OLCC Service Permit. Add a photo of a new card here: HR checks it."),
      H.me.staff ? h("p", null, h("a", { href: "#/certificates" }, "See the certificate board")) : null),
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
      return show(h("section", { class: "card" }, h("p", null, "You can't add a card for them."), h("a", { href: "#/" }, "Go home")));
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
    h("h3", null, "Front of the card"),
    h("div", { class: "actions" },
      chooser("Take a photo", "image/jpeg", true, pick("front", frontBox)),
      chooser("Choose a file", "image/jpeg,image/png,image/webp,application/pdf", false, pick("front", frontBox))),
    frontBox,
    h("h3", null, "Back of the card (if it has dates on it)"),
    h("div", { class: "actions" }, chooser("Take a photo of the back", "image/jpeg", true, pick("back", backBox))),
    backBox, error,
    h("div", { class: "actions full" }, send));
    show(h("section", { class: "card" },
      h("h1", null, self ? "Add or renew a card" : `Add a card for ${view.person.name}`),
      h("p", { class: "muted small" }, view.relation === "hr"
        ? "Next you check the date and confirm it."
        : "HR checks every card before it counts."),
      h("h2", null, "Photo tips"), h("ul", { class: "cert-tips" }, view.tips.map((t) => h("li", null, t))),
      view.reading ? null : h("p", { class: "muted small" }, "HR types the date from the photo."),
      form),
    h("p", null, h("a", { href: self ? "#/my-certificates" : `#/people/${enc(personId)}` }, "Back")));
  }

  // "We read: expires 6/14/2029. HR will confirm." The page asks for a few seconds while Google
  // reads the card, then stops (HR confirms either way).
  async function showReading(view, card) {
    const line = h("p", { class: "cert-reading", role: "status" }, readLine(card) || "");
    const where = location.hash;
    show(h("section", { class: "card" }, h("h1", null, "Sent to HR"),
      h("p", null, `${card.type} for ${view.relation === "self" ? "you" : view.person.name}.`), line,
      h("div", { class: "actions" },
        h("a", { class: "button primary-link", href: view.relation === "self" ? "#/my-certificates" : `#/people/${enc(view.person.id)}` }, "Done"))));
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
      show(h("section", { class: "card" }, h("p", null, "That page isn't here."), h("a", { href: "#/" }, "Go home")));
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

  function boardRow(r) {
    return h("li", { class: "cert-item" },
      h("div", { class: "cert-head" }, h("a", { class: "title", href: `#/people/${enc(r.person_id)}` }, r.name), badge(r.status, r.status_word)),
      h("div", { class: "sub" }, [r.type, statusText(r), r.departments.join(", ")].filter(Boolean).join(" · ")),
      flags(r));
  }

  async function renderBoard(params) {
    const q = new URLSearchParams();
    for (const name of ["group", "type", "status", "within"]) if (params.get(name)) q.set(name, params.get(name));
    const data = await api("GET", `/api/certificates/board${q.toString() ? `?${q}` : ""}`);
    const counts = Object.entries(data.counts).filter(([, n]) => n).map(([k, n]) => `${n} ${data.statuses[k].toLowerCase()}`);
    const hrLinks = data.hr ? h("div", { class: "actions" },
      data.waiting ? h("a", { class: "button primary-link", href: "#/certificates/check" }, `${data.waiting} to check`) : null,
      data.connecteam ? h("a", { class: "button", href: "#/certificates/connecteam" }, `${plural(data.connecteam, "card")} added in Connecteam`) : null,
      h("button", { type: "button", onclick: () => run(async () => {
        const r = await api("POST", "/api/certificates/export", Object.fromEntries(q));
        const link = h("a", { href: URL.createObjectURL(new Blob([r.csv], { type: "text/csv" })), download: r.name });
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(link.href), 30000);
      }, "Downloaded.") }, "Download (spreadsheet)")) : null;
    show(h("section", { class: "card" },
      h("h1", null, data.hr ? "Certificate board" : "Team certificates"),
      h("p", { class: "muted small" }, data.hr ? "Everyone's certificates and when they run out." : "The certificates your team needs. Photos are only seen by the person and HR."),
      h("p", null, h("a", { href: "#/my-certificates" }, "My certificates")),
      data.banners.map((b) => h("p", { class: "cert-banner", role: "alert" }, b)),
      hrLinks, filterForm(data, params),
      h("p", { class: "muted small" }, counts.length ? `Everyone: ${counts.join(", ")}.` : "No one needs a certificate yet. HR chooses who needs each one in HR settings.")),
    h("section", { class: "card" },
      data.rows.length ? h("ul", { class: "list" }, data.rows.map(boardRow))
        : h("p", { class: "muted" }, data.total ? "No one matches." : "Nothing to show yet.")));
  }

  // ---------------------------------------------------------------- HR: checking cards

  async function renderWaiting() {
    const data = await api("GET", "/api/certificates/waiting");
    show(h("section", { class: "card" }, h("h1", null, "Cards to check"),
      h("p", { class: "muted small" }, "Oldest first. Another HR admin checks your own."),
      data.cards.length ? h("ul", { class: "list" }, data.cards.map((c) => h("li", null,
        h("a", { class: "item", href: `#/certificates/check/${enc(c.person_id)}/${c.id}` },
          h("div", { class: "title" }, `${c.name}: ${c.type}`),
          h("div", { class: "sub" }, [`Added ${when(c.added_at)}`, c.added_by ? `by ${c.added_by}` : null].filter(Boolean).join(" · ")),
          readLine(c) ? h("div", { class: "sub" }, readLine(c)) : null))))
        : h("p", { class: "muted" }, "Nothing to check."),
      h("p", null, h("a", { href: "#/certificates" }, "Certificate board"))));
  }

  async function renderCheck(personId, cardId) {
    const d = await api("GET", `/api/people/${enc(personId)}/certificates/${enc(cardId)}`);
    const c = d.card;
    const back = h("p", null, h("a", { href: "#/certificates/check" }, "Cards to check"), " · ",
      h("a", { href: `#/people/${enc(personId)}` }, `${d.person.name}'s page`));
    // The photo: zoom and turn (turning asks the server for an upright copy).
    let zoom = 1;
    let turn = 0;
    const img = c.photo && !c.pdf ? h("img", { src: c.photo, alt: `Photo of ${d.person.name}'s ${d.type.name}` }) : null;
    const setZoom = (z) => { zoom = Math.min(4, Math.max(1, z)); img.style.width = `${zoom * 100}%`; };
    const photo = h("section", { class: "card cert-side" }, h("h2", null, "The card"),
      img ? [h("div", { class: "cert-photo" }, img), h("div", { class: "actions" },
        h("button", { type: "button", onclick: () => setZoom(zoom - 0.5), "aria-label": "Zoom out" }, "−"),
        h("button", { type: "button", onclick: () => setZoom(zoom + 0.5), "aria-label": "Zoom in" }, "+"),
        h("button", { type: "button", onclick: () => { turn = (turn + 90) % 360; img.src = `${c.photo}?turn=${turn}`; } }, "Turn"),
        h("a", { class: "button", href: c.photo, target: "_blank", rel: "noopener" }, "Full size"))]
        : c.photo ? h("a", { class: "button", href: c.photo, download: "" }, "Download the PDF")
          : h("p", { class: "muted" }, "The photo isn't kept here any more."));
    const read = h("section", { class: "card cert-side" }, h("h2", null, "What Google read"),
      d.segments.length ? h("div", { class: "cert-text" }, d.segments.map((s) => (s.d ? h("mark", null, s.t) : s.l ? h("strong", null, s.t) : s.t)))
        : h("p", { class: "muted" }, c.read_note || (c.read_state === "pending" ? "Still reading it. Type the date from the photo, or come back in a minute." : "Nothing was read.")),
      d.warnings.length ? h("ul", { class: "cert-warnings" }, d.warnings.map((w) => h("li", { class: "warn" }, w))) : null);
    if (c.status !== "waiting") {
      return show(h("section", { class: "card" }, h("h1", null, `${d.person.name}: ${d.type.name}`),
        h("p", null, `This card was already decided: ${c.status_word.toLowerCase()}.`), back), photo);
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
    h("label", null, "Issued", h("small", null, "If it's on the card"), issued),
    h("label", null, d.type.olcc ? "Permit number" : "Card number", h("small", null, "If it's on the card"), number),
    d.type.olcc ? [tick("temporary", "It's a temporary permit", d.suggest.temporary),
      tick("under_21", "Under 21: may not mix drinks or bartend", d.suggest.under_21)] : null,
    tick("checked_issuer", d.type.olcc ? "Checked with OLCC" : "Checked with the issuer", false),
    h("div", { class: "actions" },
      h("button", { type: "submit", class: "primary" }, "Confirm"),
      h("button", { type: "button", class: "danger", onclick: async () => {
        const reason = await ask("Don't accept this card", `Why? ${d.person.name} sees this, so say what to fix.`);
        if (reason === null) return;
        const r = await run(() => api("POST", `/api/people/${enc(personId)}/certificates/${c.id}/reject`, { reason }));
        if (r) {
          toast(r.message);
          next();
        }
      } }, "Don't accept")));
    show(h("section", { class: "card" }, h("h1", null, `${d.person.name}: ${d.type.name}`),
      h("p", { class: "muted small" }, [`Added ${when(c.added_at)}`, c.added_by ? `by ${c.added_by}` : null].filter(Boolean).join(" · ")),
      d.current ? h("p", { class: "small" }, `It replaces the card that expires ${day(d.current.expires)}.`) : null, back),
    h("div", { class: "cert-check" }, photo, read),
    h("section", { class: "card" }, h("h2", null, "Confirm the dates"), form));
  }

  async function renderConnecteam() {
    const again = () => renderConnecteam();
    const data = await api("GET", "/api/certificates/connecteam");
    show(h("section", { class: "card" }, h("h1", null, "Cards added in Connecteam"),
      h("p", { class: "muted small" }, "Once a day the HR portal looks at Connecteam's documents. Staff Access can't fetch a card someone added in Connecteam's own app, so approve or reject it there, and ask them to add it here too."),
      data.copy_on ? null : h("p", { class: "warn" }, "The Connecteam copy is off, so Connecteam isn't checked."),
      data.error ? h("p", { class: "warn" }, data.error) : null,
      data.checked_at ? h("p", { class: "muted small" }, `Last checked ${when(data.checked_at)}.`) : null,
      data.notes.length ? h("ul", { class: "list" }, data.notes.map((n) => h("li", { class: "cert-item" },
        h("p", null, n.text), h("div", { class: "sub" }, `Found ${when(n.found_at)}`),
        h("div", { class: "actions" }, h("a", { class: "button", href: `#/people/${enc(n.person_id)}` }, "Their page"),
          h("button", { type: "button", onclick: () => run(async () => {
            await api("POST", `/api/certificates/connecteam/${n.id}/done`, {});
            await again();
          }, "Done.") }, "Done")))))
        : h("p", { class: "muted" }, "Nothing to do."),
      h("p", null, h("a", { href: "#/certificates" }, "Certificate board"))));
  }

  H.registerPage("certificates", {
    nav: "Certificates",
    who: (me) => me.staff,
    render: async (_main, parts, params) => {
      if (parts[0] === "check" && H.me.hr) return parts[2] ? renderCheck(decodeURIComponent(parts[1]), parts[2]) : renderWaiting();
      if (parts[0] === "connecteam" && H.me.hr) return renderConnecteam();
      return renderBoard(params);
    },
  });

  // ---------------------------------------------------------------- HR settings

  function typeForm(t, data, again) {
    const groups = new Set(t.groups_confirmed ? t.groups : t.suggested);
    const chosenPeople = new Set(t.people);
    const peopleList = h("ul", { class: "list" });
    const drawPeople = () => peopleList.replaceChildren(...[...chosenPeople].map((id) => h("li", null, h("div", { class: "item split" },
      h("span", null, data.people.find((p) => p.id === id)?.name || id),
      h("button", { type: "button", class: "linkish", onclick: () => { chosenPeople.delete(id); drawPeople(); } }, "Remove")))));
    drawPeople();
    const addPerson = h("select", { "aria-label": "Add someone who needs it" }, h("option", { value: "" }, "Add someone…"),
      data.people.map((p) => h("option", { value: p.id }, p.name)));
    addPerson.addEventListener("change", () => {
      if (addPerson.value) chosenPeople.add(addPerson.value);
      addPerson.value = "";
      drawPeople();
    });
    const ct = data.connecteam;
    const matched = !t.ct_document && ct.documents.find((d) => d.name.trim().toLowerCase() === t.name.trim().toLowerCase());
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
        ct_document: f.ct_document.value || null,
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
    h("h4", null, "Departments that need it"),
    t.groups_confirmed ? null : h("p", { class: "warn" }, t.suggested.length
      ? "Suggested from the department names. Check them and save: nobody needs it until you do."
      : "Choose the departments and save: nobody needs it until you do."),
    data.departments.length ? data.departments.map((d) => h("label", { class: "check" },
      h("input", { type: "checkbox", name: `g-${d.group}`, checked: groups.has(d.group) }), d.name))
      : h("p", { class: "muted small" }, "No departments yet: they come from Connecteam's smart groups."),
    h("h4", null, "People who need it too"), peopleList, addPerson,
    others.length ? [h("h4", null, "Accepted in its place"), ...others.map((o) => h("label", { class: "check" },
      h("input", { type: "checkbox", name: `a-${o.key}`, checked: t.accepts.includes(o.key) }), o.name))] : null,
    h("h4", null, "Connecteam document"),
    h("p", { class: ct.copy_on ? "muted small" : "warn" }, ct.copy_on
      ? "A confirmed card is copied there with its expiry date, so Connecteam reminds the person too."
      : "The Connecteam copy is off. The owner turns it on in Home Assistant (Settings, Connections, Connecteam for HR); until then cards stay in Google Drive and the portal."),
    h("select", { name: "ct_document", "aria-label": "Connecteam document" }, h("option", { value: "" }, "None"),
      ct.documents.map((d) => h("option", { value: d.id, selected: d.id === t.ct_document || (matched && matched.id === d.id) }, d.name)),
      t.ct_document && !ct.documents.some((d) => d.id === t.ct_document) ? h("option", { value: t.ct_document, selected: true }, `Document ${t.ct_document}`) : null),
    matched ? h("p", { class: "muted small" }, "Matched by its name. Save to use it.") : null,
    h("label", { class: "check" }, h("input", { type: "checkbox", name: "active", checked: !!t.active }), "Track this certificate"),
    h("div", { class: "actions" }, h("button", { type: "submit", class: "primary" }, `Save ${t.name}`)));
    return h("details", { class: "cert-type", open: !t.groups_confirmed || null },
      h("summary", null, t.name, t.active ? "" : " (not tracked)", t.groups_confirmed ? "" : " · departments to confirm"), form);
  }

  H.registerSettings("certificates", (box, data, again) => {
    const remind = h("form", { class: "row cert-inline", onsubmit: (e) => {
      e.preventDefault();
      run(() => api("PUT", "/api/settings/certificates", { remind_every: remind.elements.remind_every.value }), "Saved.");
    } }, h("label", null, "Remind about expired or missing cards every (days)", h("small", null, "1 to 14. The person, their managers and HR are reminded."),
      h("input", { name: "remind_every", type: "number", min: "1", max: "14", value: data.remind_every })),
    h("div", { class: "actions" }, h("button", { type: "submit" }, "Save")));
    const add = h("form", { class: "row cert-inline", onsubmit: (e) => {
      e.preventDefault();
      run(async () => {
        await api("POST", "/api/settings/certificates/types", { name: add.elements.name.value });
        await again();
      }, "Added. Set it up below.");
    } }, h("label", null, "Add a kind of certificate", h("small", null, "Such as CPR and First Aid, or a pool operator's certificate"),
      h("input", { name: "name", maxlength: "60", required: true })),
    h("div", { class: "actions" }, h("button", { type: "submit" }, "Add")));
    const refresh = h("button", { type: "button", onclick: () => run(async () => {
      await api("POST", "/api/settings/certificates/connecteam-documents", {});
      await again();
    }, "Connecteam's documents were read.") }, "Read Connecteam's documents");
    box.append(...[h("h2", null, "Certificates"),
      h("p", { class: "muted small" }, "Which certificates people need and when. Cards are checked by HR before they count."),
      remind,
      data.types.map((t) => typeForm(t, data, again)),
      h("div", { class: "actions" }, refresh),
      data.connecteam.error ? h("p", { class: "warn" }, data.connecteam.error) : null,
      data.connecteam.listed_at ? h("p", { class: "muted small" }, `Connecteam's documents were read ${when(data.connecteam.listed_at)}.`) : null,
      add].flat().filter(Boolean));
  });
})();
