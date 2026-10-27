// HR portal: documents. A person's Documents section (open, add, HR's check, delete and restore,
// restricted files on request, legal hold, retention, the Drive folder), HR's Needs attention
// page, and the kinds of document in HR settings. Plain text only: h() makes text nodes.
(() => {
  "use strict";
  const H = window.HRPortal;
  if (!H) return;
  const { h, api, toast, ask, confirmDialog, openDialog, show, when } = H;

  // This part's styles, versioned like hr.css.
  const version = (document.querySelector('link[href^="/static/hr.css"]')?.getAttribute("href") || "").split("?v=")[1];
  document.head.append(h("link", { rel: "stylesheet", href: `/static/hr-documents.css${version ? `?v=${version}` : ""}` }));

  const day = (iso) => (iso ? new Date(`${iso}T12:00:00Z`).toLocaleDateString([], {
    month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }) : "");
  const dateOf = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" }) : "");
  const sizeText = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  // append() and replaceChildren() would show a missing node as the text "null".
  const real = (nodes) => nodes.flat().filter((n) => n != null && n !== false && n !== "");
  const fill = (el, ...nodes) => el.append(...real(nodes));
  const refill = (el, ...nodes) => el.replaceChildren(...real(nodes));
  const today = () => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };

  async function act(fn, okText) {
    try {
      const r = await fn();
      if (r !== false && (r?.message || okText)) toast(r?.message || okText);
      return r;
    } catch (err) {
      // Signed out (the sign-in page shows), moved on, or a code that wasn't typed: say nothing.
      if (!["Not confirmed.", "Signed out", "Moved on"].includes(err.message)) toast(err.message, true);
      return false;
    }
  }

  // A form with a file goes as multipart, so it can't use api() (JSON only).
  async function sendForm(path, form) {
    const res = await fetch(path, { method: "POST", body: form, credentials: "same-origin",
      headers: { "X-Requested-With": "hr-portal" } });
    let data = {};
    try { data = await res.json(); } catch { data = { error: `Something went wrong (${res.status}).` }; }
    if (res.status === 401 && data.error === "signed_out") {
      location.reload();
      throw new Error("You were signed out.");
    }
    if (!res.ok) throw new Error(data.error || `Something went wrong (${res.status}).`);
    return data;
  }

  // ---------------------------------------------------------------- one document

  function openFile(d) {
    if (d.type === "pdf") return;   // a plain download link
    openDialog(d.title, [
      h("img", { class: "doc-image", src: d.url, alt: d.title }),
      h("p", { class: "muted small" }, `${d.kind_name} · ${day(d.date)}`),
      h("p", null, h("a", { href: d.url, target: "_blank", rel: "noopener noreferrer" }, "Open full size")),
    ], [{ label: "Close", value: "ok", class: "primary" }]);
  }

  function docItem(d, redraw) {
    const badges = [];
    if (d.status !== "active") badges.push(h("span", { class: d.status === "waiting" ? "badge" : "badge bad" }, d.status_word));
    if (d.restricted) badges.push(h("span", { class: "badge" }, "Restricted"));
    const sub = [day(d.date), d.type === "pdf" ? "PDF" : "Photo", sizeText(d.size), d.added_by ? `Added by ${d.mine ? "you" : d.added_by}` : null]
      .filter(Boolean).join(" · ");
    const buttons = [];
    if (d.url) {
      buttons.push(d.type === "pdf"
        ? h("a", { class: "button", href: d.url, download: "" }, "Download")
        : h("button", { type: "button", onclick: () => openFile(d) }, "Open"));
    }
    if (d.drive_link) buttons.push(h("a", { class: "button", href: d.drive_link, target: "_blank", rel: "noopener noreferrer" }, "Open in Google Drive"));
    if (d.can_check) {
      buttons.push(h("button", { type: "button", class: "primary", onclick: () => act(async () => {
        const r = await api("POST", `/api/documents/${d.id}/accept`, {});
        await redraw();
        return r;
      }) }, "Accept"));
      buttons.push(h("button", { type: "button", onclick: async () => {
        const reason = await ask("Not accepted", "Why? Whoever added it sees this.");
        if (reason === null) return;
        await act(async () => {
          const r = await api("POST", `/api/documents/${d.id}/reject`, { reason });
          await redraw();
          return r;
        });
      } }, "Don't accept"));
    }
    if (d.can_delete) {
      buttons.push(h("button", { type: "button", class: "danger", onclick: async () => {
        if (!(await confirmDialog(`Delete “${d.title}”? It stays in the recycle bin for 30 days and can be restored until then.`, "Delete", true))) return;
        await act(async () => {
          const r = await api("DELETE", `/api/documents/${d.id}`);
          await redraw();
          return r;
        });
      } }, "Delete"));
    }
    if (d.can_restore) {
      buttons.push(h("button", { type: "button", onclick: () => act(async () => {
        const r = await api("POST", `/api/documents/${d.id}/restore`, {});
        await redraw();
        return r;
      }) }, "Restore"));
    }
    const copies = (d.deliveries || []).map((c) => h("div", { class: c.status === "attention" ? "small error" : "muted small" },
      `${c.word}: ${c.status_word}${c.error ? `. ${c.error}` : ""}`));
    return h("li", null, h("div", { class: "item" },
      h("div", { class: "title" }, d.title, " ", badges),
      h("div", { class: "sub" }, sub),
      d.reason ? h("p", { class: "warn small doc-note" }, `Not accepted: ${d.reason}`) : null,
      d.bin_until ? h("div", { class: "muted small" }, `Removed for good on ${dateOf(d.bin_until)}.`) : null,
      copies,
      buttons.length ? h("div", { class: "actions" }, buttons) : null));
  }

  function docList(docs, redraw) {
    if (!docs.length) return h("p", { class: "muted" }, "No files yet.");
    const groups = new Map();
    for (const d of docs) {
      if (!groups.has(d.kind_name)) groups.set(d.kind_name, []);
      groups.get(d.kind_name).push(d);
    }
    return h("div", null, [...groups].flatMap(([name, list]) => [h("h3", null, name),
      h("ul", { class: "list" }, list.map((d) => docItem(d, redraw)))]));
  }

  // Add a file: a kind, a title, the document's date (first in its name in Drive) and the file.
  function addForm(personId, kinds, limitMb, redraw, note) {
    if (!kinds.length) return null;
    const file = h("input", { type: "file", name: "file", required: true,
      accept: "image/jpeg,image/png,image/webp,application/pdf" });
    const button = h("button", { type: "submit", class: "primary" }, "Add");
    const form = h("form", { class: "doc-add", onsubmit: async (e) => {
      e.preventDefault();
      const f = form.elements;
      const chosen = f.file.files[0];
      if (!chosen) return toast("Choose a file to add.", true);
      if (chosen.size > limitMb * 1048576) return toast(`That file is too big (more than ${limitMb} MB).`, true);
      const body = new FormData();
      body.append("kind", f.kind.value);
      body.append("title", f.title.value);
      body.append("date", f.date.value);
      body.append("file", chosen);
      button.disabled = true;
      button.textContent = "Adding…";
      const r = await act(() => sendForm(`/api/people/${encodeURIComponent(personId)}/documents`, body));
      button.disabled = false;
      button.textContent = "Add";
      if (r) await redraw();
    } },
    h("h3", null, "Add a file"),
    note ? h("p", { class: "muted small" }, note) : null,
    h("label", null, "Kind", h("select", { name: "kind" }, kinds.map((k) => h("option", { value: k.id }, k.name)))),
    h("label", null, "Title", h("small", null, "Leave it blank to use the kind's name."),
      h("input", { name: "title", maxlength: "120", autocomplete: "off" })),
    h("label", null, "Date on the document", h("input", { name: "date", type: "date", value: today(), required: true })),
    h("label", null, "File", h("small", null, `A photo (JPEG, PNG or WebP) or a PDF, up to ${limitMb} MB.`), file),
    h("div", { class: "actions" }, button));
    return form;
  }

  // ---------------------------------------------------------------- HR's part of the section

  function hrBox(s, person, redraw) {
    const x = s.hr;
    const base = `/api/people/${encodeURIComponent(person.id)}`;
    const box = h("div", { class: "doc-hr" });
    if (x.drive) box.append(h("p", { class: "warn small" }, x.drive));

    // Restricted files: only on request, and every look is logged.
    const restricted = h("div", { class: "doc-restricted" });
    const showRestricted = async () => {
      const r = await act(() => api("GET", `${base}/documents/restricted`));
      if (!r) return;
      refill(restricted, h("h3", null, "Restricted files"),
        h("p", { class: "muted small" }, "Only HR admins see these. Each look is in the audit log."),
        r.warning ? h("p", { class: "warn small" }, r.warning) : null,
        docList(r.documents, redraw),
        addForm(person.id, r.kinds, r.limit_mb, redraw, null),
        h("div", { class: "actions" }, h("button", { type: "button", onclick: () => {
          restricted.replaceChildren(restrictedSummary());
        } }, "Hide restricted files")));
    };
    const restrictedSummary = () => h("div", null, h("h3", null, "Restricted files"),
      h("p", { class: "muted small" }, `${x.restricted.kinds.join(", ")}: ${x.restricted.count ? plural(x.restricted.count, "file", "files") : "none yet"}. Only HR admins see these.`),
      h("div", { class: "actions" }, h("button", { type: "button", onclick: showRestricted }, "Show restricted files")));
    if (x.restricted.kinds.length) restricted.append(restrictedSummary());
    box.append(restricted);

    // Legal hold.
    const hold = x.hold;
    fill(box, h("h3", null, "Legal hold"),
      hold ? h("p", { class: "warn small" }, `On since ${dateOf(hold.set_at)} (${hold.set_by}): ${hold.reason || ""}. Nothing in this file can be deleted.`)
        : h("p", { class: "muted small" }, "Off. A legal hold stops every deletion in this file."),
      h("div", { class: "actions" }, h("button", { type: "button", onclick: async () => {
        let body = { on: false };
        if (!hold) {
          const reason = await ask("Legal hold", "Why? (for example, the claim or request it's for)");
          if (reason === null) return;
          body = { on: true, reason };
        } else if (!(await confirmDialog("Lift the legal hold? Files in this file can then be deleted again.", "Lift it"))) return;
        await act(async () => {
          const r = await api("PUT", `${base}/legal-hold`, body);
          await redraw();
          return r;
        });
      } }, hold ? "Lift legal hold" : "Put on legal hold")));

    // Former employees: retention, and their Drive folder.
    if (x.archived) {
      const ret = x.retention;
      fill(box, h("h3", null, "After leaving"),
        ret ? h("p", { class: ret.past ? "warn small" : "muted small" }, ret.past
          ? `Past the time records are kept (since ${dateOf(ret.due_at)}). Review the files, then delete them if they're no longer needed.`
          : `Records are kept until ${dateOf(ret.due_at)}, then listed for HR to review. Nothing is deleted by itself.`) : null,
        h("div", { class: "actions" },
          x.can_move_folder ? h("button", { type: "button", onclick: () => act(async () => {
            const r = await api("POST", `${base}/drive-folder/former`, {});
            await redraw();
            return r;
          }) }, "Move Drive folder to Former employees") : null,
          ret?.past && !hold ? h("button", { type: "button", class: "danger", onclick: async () => {
            if (!(await confirmDialog(`Delete all of ${person.name}'s files? They stay in the recycle bin for 30 days.`, "Delete all", true))) return;
            await act(async () => {
              const r = await api("POST", `${base}/documents/delete-all`, {});
              await redraw();
              return r;
            });
          } }, "Delete their files") : null));
    }
    return box;
  }

  H.registerSection("documents", (box, s, person, redraw) => {
    box.append(h("h2", null, s.title));
    if (s.waiting_note && s.kinds.length) box.append(h("p", { class: "muted small" }, s.waiting_note));
    const shown = s.documents.filter((d) => d.status !== "deleted");
    const binned = s.documents.filter((d) => d.status === "deleted");
    box.append(docList(shown, redraw));
    if (binned.length) box.append(h("h3", null, "Recycle bin"), h("ul", { class: "list" }, binned.map((d) => docItem(d, redraw))));
    const form = addForm(person.id, s.kinds, s.limit_mb, redraw, null);
    if (form) box.append(form);
    if (s.hr) box.append(hrBox(s, person, redraw));
  });

  // ---------------------------------------------------------------- Needs attention (HR)

  async function showAttention() {
    const seq = location.hash;
    const data = await api("GET", "/api/documents/attention");
    const again = () => (location.hash === seq ? showAttention() : undefined);
    const personLink = (i) => h("a", { href: `#/people/${encodeURIComponent(i.person_id)}` }, i.name);
    const card = (title, intro, items) => h("section", { class: "card" }, h("h2", null, title),
      intro ? h("p", { class: "muted small" }, intro) : null,
      items.length ? h("ul", { class: "list" }, items) : h("p", { class: "muted" }, "Nothing here."));
    const stuck = data.stuck.map((i) => h("li", null, h("div", { class: "item" },
      h("div", { class: "title" }, `${i.word}: `, personLink(i)),
      i.title ? h("div", { class: "sub" }, i.title) : null,
      h("p", { class: "small error" }, i.error || "It didn't work."),
      h("div", { class: "muted small" }, `${plural(i.tries, "try", "tries")}.`),
      h("div", { class: "actions" },
        h("button", { type: "button", class: "primary", onclick: () => act(async () => {
          const r = await api("POST", `/api/deliveries/${i.id}/retry`, {});
          await again();
          return r;
        }) }, "Retry")))));
    const waiting = data.waiting.map((i) => h("li", null, h("div", { class: "item" },
      h("div", { class: "title" }, `${i.word}: `, personLink(i)),
      i.title ? h("div", { class: "sub" }, i.title) : null,
      h("div", { class: "muted small" }, [i.error, `Next try: ${when(i.due_at)}`].filter(Boolean).join(" · ")))));
    const checks = data.checks.map((c) => h("li", null, h("a", { class: "item", href: `#/people/${encodeURIComponent(c.person_id)}` },
      h("div", { class: "title" }, c.name), h("div", { class: "sub" }, `${plural(c.count, "file", "files")} to check`))));
    const retention = data.retention.map((r) => h("li", null, h("a", { class: "item", href: `#/people/${encodeURIComponent(r.id)}` },
      h("div", { class: "title" }, r.name),
      h("div", { class: "sub" }, `Left ${dateOf(r.left_at)} · ${plural(r.documents, "file", "files")} · past since ${dateOf(r.due_at)}`))));
    const holds = data.holds.map((x) => h("li", null, h("a", { class: "item", href: `#/people/${encodeURIComponent(x.person_id)}` },
      h("div", { class: "title" }, x.name), h("div", { class: "sub" }, `Since ${dateOf(x.since)}${x.by ? ` (${x.by})` : ""}`))));
    show(h("section", { class: "card" }, h("h1", null, "Needs attention"),
      h("p", { class: "muted small" }, "Files waiting for HR, uploads to Google Drive that need a person, and records past the time they're kept."),
      data.drive ? h("p", { class: "warn small" }, data.drive) : null),
    card("Waiting for HR to check", null, checks),
    card("Couldn't be sent", "Tried 8 times, or something only a person can fix. Fix it, then press Retry.", stuck),
    data.waiting.length ? card("Waiting to be sent", "These are tried again by themselves.", waiting) : null,
    card("Past the time records are kept", `Former employees' records are kept ${plural(data.years, "year", "years")} after they leave, then listed here. Nothing is deleted by itself. Open someone to review and delete their files.`, retention),
    data.holds.length ? card("Legal holds", null, holds) : null);
  }

  H.registerPage("attention", { nav: "Needs attention", who: (me) => me.hr, render: showAttention });

  // ---------------------------------------------------------------- kinds (HR settings)

  H.registerSettings("documents", (box, data, again) => {
    const levelWord = (l) => data.levels[l] || l;
    async function kindDialog(k) {
      const fixed = new Set(k?.fixed || []);
      const name = h("input", { name: "name", value: k?.name || "", maxlength: "60", required: true });
      const level = h("select", { name: "visibility", disabled: fixed.has("visibility") },
        Object.entries(data.levels).filter(([l]) => !k || l !== "restricted" || k.visibility === "restricted")
          .map(([l, word]) => h("option", { value: l, selected: (k?.visibility || "hr") === l }, word)));
      const folder = h("input", { name: "folder", value: k?.folder || "Documents", maxlength: "60" });
      const who = h("select", { name: "uploaders", disabled: fixed.has("uploaders") },
        Object.entries(data.uploaders).map(([u, word]) => h("option", { value: u, selected: (k?.uploaders || "hr") === u }, word)));
      const notes = [];
      if (k?.owned) notes.push(h("p", { class: "muted small" }, "Another part of the HR portal shows these and decides who sees them. You can rename it and change its Drive folder."));
      else if (k?.restricted) notes.push(h("p", { class: "muted small" }, "Restricted: HR admins only, kept apart in Drive."));
      const choice = await openDialog(k ? `Change “${k.name}”` : "Add a kind of document", [
        ...notes,
        h("label", null, "Name", name),
        h("label", null, "Who can see it", h("small", null, k ? "Restricted can only be chosen when a kind is made." : "Restricted kinds are kept apart and can't be changed later."), level),
        h("label", null, "Google Drive folder", h("small", null, "In each person's folder. Files already in Drive stay where they are."), folder),
        h("label", null, "Who may add one", who),
      ], [{ label: "Cancel", value: "cancel" }, { label: k ? "Save" : "Add", value: "ok", class: "primary" }]);
      if (choice !== "ok") return;
      const body = { name: name.value, folder: folder.value };
      if (!fixed.has("visibility")) body.visibility = level.value;
      if (!fixed.has("uploaders")) body.uploaders = who.value;
      const r = await act(() => (k ? api("PUT", `/api/documents/kinds/${k.id}`, body) : api("POST", "/api/documents/kinds", body)), "Saved.");
      if (r) await again();
    }

    fill(box, h("h2", null, "Kinds of document"),
      h("p", { class: "muted small" }, "Who can see each kind, its folder in Google Drive, and who may add one. Files anyone but HR adds wait for HR to check them."),
      data.drive ? h("p", { class: "warn small" }, data.drive) : null,
      data.restricted_warning ? h("p", { class: "warn small" }, data.restricted_warning) : null,
      h("ul", { class: "list" }, data.kinds.map((k) => h("li", null, h("div", { class: "item split doc-kind" },
        h("div", null, h("div", { class: "title" }, k.name, k.restricted ? h("span", { class: "badge" }, "Restricted") : null),
          h("div", { class: "sub" }, [levelWord(k.visibility), `Drive: ${k.folder}`,
            k.owned ? "Shown in its own section" : data.uploaders[k.uploaders],
            plural(k.count, "file", "files")].filter(Boolean).join(" · "))),
        h("button", { type: "button", class: "linkish", onclick: () => kindDialog(k) }, "Change"))))),
      h("div", { class: "actions" }, h("button", { type: "button", class: "primary", onclick: () => kindDialog(null) }, "Add a kind")));
  });
})();
