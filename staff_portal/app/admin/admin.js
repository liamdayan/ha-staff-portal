(() => {
  "use strict";

  // All URLs are relative: Home Assistant serves this page under an ingress path.
  const state = { settings: {}, roles: [], devices: [], staff: [], areas: [], hasLogo: false };
  let entities = null;
  let haAreas = [];
  let editing = null; // { id, name, entities: {entity_id: level}, dirty }
  let oldestActivity = null;

  const $ = (sel, root = document) => root.querySelector(sel);
  const toastEl = $("#toast");
  // Filled in by the add-on when it serves the page. The header shows it once the script runs,
  // so "loading…" staying there means the dashboard script didn't start.
  const SCRIPT_VERSION = "{{SCRIPT_VERSION}}";

  // Never fail silently: unexpected errors stay on screen and are written to the add-on's log.
  function reportError(message, error, where) {
    const detail = [message, where, error?.stack].filter(Boolean).join("\n");
    const panel = $("#error-panel");
    if (panel) {
      $("[data-detail]", panel).textContent = detail;
      panel.hidden = false;
    }
    fetch("api/client-error", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "staff-portal" },
      body: JSON.stringify({ detail: detail.slice(0, 4000), agent: navigator.userAgent }),
    }).catch(() => { /* nothing more we can do */ });
  }
  window.addEventListener("error", (e) =>
    reportError(e.message, e.error, e.filename ? `${e.filename}:${e.lineno}:${e.colno}` : ""));
  $("[data-close-error]")?.addEventListener("click", () => { $("#error-panel").hidden = true; });
  window.addEventListener("unhandledrejection", (e) =>
    reportError(String(e.reason?.message || e.reason), e.reason, "in a background task"));

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === false || v == null) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(c));
    }
    return el;
  }

  // ---------------------------------------------------------------- drag to reorder

  const GRIP_ICON = '<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">' +
    '<path d="M4 6h12M4 10h12M4 14h12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';

  // The \u2261 grip at the start of a row that can be reordered.
  function dragHandle(label) {
    const handle = h("button", { type: "button", class: "drag-handle", "aria-label": label,
      title: "Drag to reorder, or use the arrow keys" });
    handle.innerHTML = GRIP_ICON;
    return handle;
  }

  function moveItem(list, from, to) {
    const [moved] = list.splice(from, 1);
    list.splice(to, 0, moved);
  }

  function scrollParent(el) {
    for (let node = el.parentElement; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (/(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight) return node;
    }
    return document.scrollingElement;
  }

  // Makes the .sort-row children of `container` reorderable by dragging their grip (mouse,
  // touch or pen) or by focusing the grip and pressing the arrow keys. onMove(from, to) updates
  // the data and redraws; it may return a promise.
  let activeDrag = false; // only one reorder at a time, across all lists

  function sortable(container, key, onMove) {
    container.dataset.sortable = key;
    const rows = () => [...container.children].filter((el) => el.classList.contains("sort-row"));
    const refocus = (index) => {
      const list = document.querySelector(`[data-sortable="${key}"]`);
      const row = list && [...list.children].filter((el) => el.classList.contains("sort-row"))[index];
      row?.querySelector(".drag-handle")?.focus();
    };

    container.addEventListener("keydown", async (ev) => {
      const handle = ev.target.closest?.(".drag-handle");
      if (!handle || activeDrag || (ev.key !== "ArrowUp" && ev.key !== "ArrowDown")) return;
      const from = rows().indexOf(handle.closest(".sort-row"));
      const to = from + (ev.key === "ArrowUp" ? -1 : 1);
      if (from < 0 || to < 0 || to >= rows().length) return;
      ev.preventDefault();
      await onMove(from, to);
      refocus(to); // right away, so a quick second key press still moves the same row
    });

    container.addEventListener("pointerdown", (ev) => {
      const handle = ev.target.closest?.(".drag-handle");
      const row = handle?.closest(".sort-row");
      if (!row || row.parentElement !== container || ev.button > 0 || !ev.isPrimary || activeDrag) return;
      ev.preventDefault();
      activeDrag = true;
      const from = rows().indexOf(row);
      const grabOffset = ev.clientY - row.getBoundingClientRect().top;
      const scroller = scrollParent(container);
      let pointerY = ev.clientY;
      // Nothing moves until the pointer has travelled a few pixels, so a click or tap on a grip
      // (even one near the edge of the window, where auto-scroll would kick in) changes nothing.
      let dragging = false;
      // Capture on the list, not the grip: the row (and its grip) are moved around the page
      // while dragging, and moving an element drops any pointer capture it holds.
      container.setPointerCapture(ev.pointerId);

      const middle = (el) => { const r = el.getBoundingClientRect(); return r.top + r.height / 2; };
      const place = () => {
        // Move past any neighbour whose middle the pointer has crossed...
        let prev = row.previousElementSibling;
        while (prev?.classList.contains("sort-row") && pointerY < middle(prev)) {
          container.insertBefore(row, prev);
          prev = row.previousElementSibling;
        }
        let next = row.nextElementSibling;
        while (next?.classList.contains("sort-row") && pointerY > middle(next)) {
          container.insertBefore(row, next.nextElementSibling);
          next = row.nextElementSibling;
        }
        // ...and keep the row under the pointer.
        row.style.transform = "";
        row.style.transform = `translateY(${pointerY - grabOffset - row.getBoundingClientRect().top}px)`;
      };

      // Scroll when dragging near the top or bottom edge of the list's scroll area.
      let frame = requestAnimationFrame(function autoScroll() {
        const box = scroller === document.scrollingElement
          ? { top: 0, bottom: window.innerHeight } : scroller.getBoundingClientRect();
        const edge = 48;
        let dy = 0;
        if (pointerY < box.top + edge) dy = -Math.min(18, Math.ceil((box.top + edge - pointerY) / 3));
        else if (pointerY > box.bottom - edge) dy = Math.min(18, Math.ceil((pointerY - box.bottom + edge) / 3));
        if (dy && dragging) {
          scroller.scrollTop += dy;
          place();
        }
        frame = requestAnimationFrame(autoScroll);
      });

      const move = (e) => {
        if (e.pointerId !== ev.pointerId) return;
        pointerY = e.clientY;
        if (!dragging) {
          if (Math.abs(pointerY - ev.clientY) < 5) return;
          dragging = true;
          row.classList.add("dragging");
          document.body.classList.add("sorting");
        }
        place();
      };
      let finished = false;
      const end = (e) => {
        if (finished || (e && e.pointerId !== ev.pointerId)) return;
        finished = true;
        activeDrag = false;
        cancelAnimationFrame(frame);
        for (const [type, fn] of listeners) container.removeEventListener(type, fn);
        if (container.hasPointerCapture(ev.pointerId)) container.releasePointerCapture(ev.pointerId);
        row.classList.remove("dragging");
        row.style.transform = "";
        document.body.classList.remove("sorting");
        const to = rows().indexOf(row);
        if (dragging && to !== from && to >= 0) onMove(from, to);
      };
      const listeners = [["pointermove", move], ["pointerup", end], ["pointercancel", end],
        ["lostpointercapture", end]];
      for (const [type, fn] of listeners) container.addEventListener(type, fn);
    });
  }

  function toast(text, isError) {
    toastEl.textContent = text;
    toastEl.classList.toggle("error", !!isError);
    toastEl.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { toastEl.hidden = true; }, 3500);
  }

  // In-page replacements for confirm() and prompt(). Home Assistant's apps show this page in a
  // frame where the browser's own pop-ups can be blocked, which made those buttons do nothing.
  function askConfirm(message, okLabel = "OK") {
    return new Promise((resolve) => {
      const dialog = $("#ask-dialog");
      $("[data-message]", dialog).textContent = message;
      $("[data-input-wrap]", dialog).hidden = true;
      const ok = $("button[value=ok]", dialog);
      ok.textContent = okLabel;
      ok.classList.toggle("danger-fill", /delete|remove|revoke|discard|unlink/i.test(okLabel + message));
      dialog.returnValue = "";
      dialog.onclose = () => resolve(dialog.returnValue === "ok");
      dialog.showModal();
      ok.focus();
    });
  }

  function askText(message, placeholder = "", initial = "") {
    return new Promise((resolve) => {
      const dialog = $("#ask-dialog");
      const input = $("[data-input]", dialog);
      $("[data-message]", dialog).textContent = message;
      $("[data-input-wrap]", dialog).hidden = false;
      input.value = initial;
      input.placeholder = placeholder;
      // Enter means OK (otherwise the form would "click" its first button, Cancel).
      input.onkeydown = (ev) => {
        if (ev.key === "Enter") {
          ev.preventDefault();
          dialog.close("ok");
        }
      };
      const ok = $("button[value=ok]", dialog);
      ok.textContent = "OK";
      ok.classList.remove("danger-fill");
      dialog.returnValue = "";
      dialog.onclose = () => resolve(dialog.returnValue === "ok" && input.value.trim() ? input.value.trim() : null);
      dialog.showModal();
      input.focus();
    });
  }

  // In a dialog, Enter in a text box means Save (the highlighted button). Left alone, the
  // browser would "click" the form's first button, Cancel, and quietly throw the entry away.
  // Dialogs that handle Enter themselves (preventDefault) are left as they are.
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" || ev.defaultPrevented || ev.isComposing) return;
    const input = ev.target;
    const form = input.tagName === "INPUT" ? input.closest("dialog form") : null;
    if (!form) return;
    ev.preventDefault();
    if (["checkbox", "radio", "button", "submit", "file"].includes(input.type)) return;
    const save = form.querySelector(".dialog-actions .primary");
    if (save && !save.disabled) form.requestSubmit(save);
  });

  async function api(method, path, body) {
    // The header marks requests as coming from this dashboard; the server refuses changes
    // without it, which stops other websites from sending them through an admin's browser.
    const opts = { method, headers: { "X-Requested-With": "staff-portal" } };
    if (body instanceof FormData) opts.body = body;
    else if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    let data = {};
    // Read the body once: some errors are plain text, and a failed .json() would use it up.
    const text = await res.text().catch(() => "");
    try { data = JSON.parse(text); } catch { data = { error: text.trim().slice(0, 300) }; }
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }

  async function run(fn, okText) {
    try {
      // fn returns false when the user cancelled, so no success message is shown.
      if ((await fn()) !== false && okText) toast(okText);
    } catch (err) {
      toast(err.message, true);
    }
  }

  function ago(ts) {
    if (!ts) return "Never";
    const s = Date.now() / 1000 - ts;
    if (s < 90) return "Just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} hr ago`;
    if (s < 86400 * 14) return `${Math.round(s / 86400)} days ago`;
    return new Date(ts * 1000).toLocaleDateString();
  }

  function when(ts) {
    return new Date(ts * 1000).toLocaleString([], {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    });
  }

  async function load() {
    const data = await api("GET", "api/state");
    state.settings = data.settings;
    // The dashboard wears the same brand color as the phones.
    if (/^#[0-9a-f]{6}$/i.test(state.settings.accent_color || "")) {
      document.documentElement.style.setProperty("--accent", state.settings.accent_color);
    }
    state.roles = data.roles;
    state.staff = data.staff;
    state.areas = data.areas;
    state.layout = data.layout;
    state.devices = data.devices;
    state.hasLogo = data.has_logo;
    state.tunnelSources = data.tunnel_sources;
    state.connecteam = data.connecteam;
    $("#add-from-connecteam").hidden = !data.connecteam.enabled;
    $("#top-title").textContent = `${state.settings.property_name} · Staff Access`;
    renderPhones();
    renderStaff();
    renderAreaSettings();
    renderRoleList();
  }

  // ---------------------------------------------------------------- tabs

  function selectTab(name) {
    document.querySelectorAll("[role=tab]").forEach((b) =>
      b.setAttribute("aria-selected", String(b.dataset.tab === name)));
    document.querySelectorAll(".tab").forEach((s) => { s.hidden = s.id !== `tab-${name}`; });
    try {
      if (name === "activity") loadActivity(true);
      if (name === "settings") fillSettings();
      if ((name === "roles" || name === "layout") && !entities) loadEntities();
      if (name === "layout") renderLayoutEditor();
      if (name === "backup") loadSafetyBackups();
    } catch (err) {
      reportError(`Couldn't open the ${name} tab: ${err.message}`, err, "");
    }
    try { sessionStorage.setItem("tab", name); } catch { /* ignore */ }
  }

  document.querySelectorAll("[role=tab]").forEach((b) =>
    b.addEventListener("click", async () => {
      if (layoutDraft?.dirty && b.dataset.tab !== "layout") {
        if (!await askConfirm("Discard unsaved layout changes?", "Discard")) return;
        layoutDraft = null;
      }
      if (editing?.dirty) {
        if (!await askConfirm("Discard unsaved role changes?", "Discard")) return;
        editing = null;
        renderRoleEditor();
        renderRoleList();
      }
      selectTab(b.dataset.tab);
    }));
  document.querySelectorAll("[data-goto]").forEach((b) =>
    b.addEventListener("click", () => selectTab(b.dataset.goto)));

  // ---------------------------------------------------------------- phones

  const STATUS = {
    pending: ["Waiting for setup", "pending"],
    active: ["Active", "active"],
    locked: ["Locked (wrong PINs)", "locked"],
    revoked: ["Access removed", "revoked"],
  };

  function renderPhones() {
    $("#url-notice").hidden = !!state.settings.public_url;
    const list = $("#phone-list");
    if (!state.devices.length) {
      list.replaceChildren(h("div", { class: "empty" },
        "Nothing set up yet. Add a phone (set up by scanning a QR code) or a computer, such as a " +
        "front desk PC (set up with a link)."));
      return;
    }
    list.replaceChildren(...state.devices.map((d) => {
      const [label, cls] = STATUS[d.status] || [d.status, ""];
      const word = kindWord(d);
      const setup = d.kind === "computer" ? "setup link" : "setup QR";
      const roleSelect = h("select", { "aria-label": "Role" },
        h("option", { value: "" }, "No role (sees nothing)"),
        state.roles.map((r) => h("option", { value: r.id, selected: r.id === d.role_id }, r.name)));
      roleSelect.addEventListener("change", () => run(async () => {
        await api("PATCH", `api/devices/${d.id}`, { role_id: roleSelect.value || null });
        await load();
      }, `Role changed. The ${word} updates within a few seconds.`));

      const actions = [];
      if (d.status === "pending" && d.setup_url) {
        actions.push(h("button", { class: "btn primary", onclick: () => showQr(d) }, `Show ${setup}`));
      }
      if (d.status === "locked") {
        actions.push(h("button", { class: "btn primary", onclick: () => deviceAction(d, "unlock",
          null, "Unlocked.") }, "Unlock"));
      }
      if (d.status !== "pending" || !d.setup_url) {
        actions.push(h("button", { class: "btn", onclick: () => deviceAction(d, "reenroll",
          d.status === "active" ? `Sign out ${d.name} and make a new ${setup === "setup QR" ? "setup QR code" : setup}?` : null) },
          d.status === "active" ? "Re-pair" : `New ${setup}`));
      }
      actions.push(h("button", { class: "btn", onclick: () => editPhone(d) }, "Edit"));
      if (d.ha_user) {
        actions.push(h("button", { class: "btn", onclick: () => linkUser(d) },
          d.ha_link_error ? "Relink HA user" : "Change HA user"));
        actions.push(h("button", { class: "btn subtle", onclick: () => run(async () => {
          if (!await askConfirm(`Unlink ${d.name} from Home Assistant user ${d.ha_user}?`, "Unlink")) return false;
          await api("DELETE", `api/devices/${d.id}/link`);
          await load();
        }, "Unlinked.") }, "Unlink"));
      } else if (d.signin_mode !== "staff") {
        actions.push(h("button", { class: "btn", onclick: () => linkUser(d) }, "Link HA user"));
      }
      if (d.status !== "revoked") {
        actions.push(h("button", { class: "btn danger", onclick: () => deviceAction(d, "revoke",
          `Remove access for ${d.name}? It will be signed out right away.`, "Access removed.") },
          "Revoke"));
      } else {
        actions.push(h("button", { class: "btn danger", onclick: () => run(async () => {
          if (!await askConfirm(`Delete ${d.name} from the list?`, "Delete")) return false;
          await api("DELETE", `api/devices/${d.id}`);
          await load();
        }) }, "Delete"));
      }

      return h("article", { class: `card phone ${cls}` },
        h("div", { class: "phone-head" },
          h("div", null,
            h("h3", null, d.name),
            h("div", { class: "meta" },
              h("span", { class: `badge ${cls}` }, label),
              d.kind === "computer" ? h("span", { class: "badge computer" }, "Computer") : null,
              d.signin_mode === "staff"
                ? h("span", { class: "badge shared" }, d.pin_only ? "Shared \u00b7 PIN only" : "Shared \u00b7 staff sign in")
                : d.has_pin ? h("span", { class: "badge" }, "PIN") : null,
              d.ha_user ? h("span", {
                class: d.ha_link_error ? "badge locked" : "badge linked",
                title: d.ha_link_error || "Actions show in Home Assistant as this user",
              }, d.ha_link_error ? `HA user ${d.ha_user}: link broken` : `HA user: ${d.ha_user}`) : null,
              h("span", null, `Last used ${ago(d.last_seen).toLowerCase()}`)),
            d.status !== "revoked" && phonePagesText(d) ? h("div", { class: "meta" }, `Pages: ${phonePagesText(d)}`,
              d.custom_order ? ` \u00b7 rearranged on the ${word}` : "") : null),
          h("label", { class: "inline-select" }, "Role", roleSelect)),
        h("div", { class: "card-actions" }, actions));
    }));
  }

  async function deviceAction(d, action, confirmText, okText) {
    const labels = { revoke: "Revoke", reenroll: "Re-pair" };
    if (confirmText && !await askConfirm(confirmText, labels[action] || "OK")) return;
    await run(async () => {
      await api("POST", `api/devices/${d.id}/${action}`);
      await load();
      if (action === "reenroll") {
        const fresh = state.devices.find((x) => x.id === d.id);
        if (fresh) showQr(fresh);
      }
    }, okText);
  }

  function fillRoleOptions(select, selected) {
    select.replaceChildren(
      h("option", { value: "" }, "No role (sees nothing)"),
      ...state.roles.map((r) => h("option", { value: r.id, selected: r.id === selected }, r.name)));
  }

  // What a phone can be given: page groups (each stands for all of its pages, including ones
  // added later), pages that aren't in a group, and the automatic More page.
  function layoutPageOptions(layout = state.layout) {
    const pages = layout?.pages || [];
    const groups = layout?.page_groups || [];
    const options = groups.map((g) => {
      const names = pages.filter((p) => p.page_group === g.id).map((p) => p.name);
      return { id: `group:${g.id}`, name: `${g.name} (group: ${names.length ? names.join(", ") : "no pages yet"})` };
    });
    for (const p of pages) if (!groups.some((g) => g.id === p.page_group)) options.push({ id: p.id, name: p.name });
    if (pages.length && layout?.show_unplaced !== false) options.push({ id: "more", name: "More (allowed devices not on any page)" });
    return options;
  }

  // What a phone's list may name: pages and page groups that are saved or in the unsaved layout,
  // and More. Anything else no longer exists.
  function knownChoices() {
    const ids = new Set(["more"]);
    for (const layout of [state.layout, layoutDraft]) {
      for (const p of layout?.pages || []) ids.add(p.id);
      for (const g of layout?.page_groups || []) ids.add(`group:${g.id}`);
    }
    return ids;
  }

  // "Every page" written out as a list (each group, each page outside a group, and More), for
  // when a phone that showed every page is switched to chosen pages.
  function allChoices() {
    const ids = new Set();
    for (const layout of [state.layout, layoutDraft]) {
      const groups = new Set((layout?.page_groups || []).map((g) => g.id));
      for (const id of groups) ids.add(`group:${id}`);
      for (const p of layout?.pages || []) if (!groups.has(p.page_group)) ids.add(p.id);
    }
    ids.add("more");
    return [...ids];
  }

  function phonePagesText(d) {
    const layout = state.layout;
    if (!(layout?.pages || []).length) return null;
    if (d.layout_pages == null) return "All pages";
    const names = d.layout_pages.map((id) => {
      if (id === "more") return "More";
      if (id.startsWith("group:")) {
        const group = (layout.page_groups || []).find((g) => `group:${g.id}` === id);
        return group && `${group.name} (group)`;
      }
      return layout.pages.find((p) => p.id === id)?.name;
    }).filter(Boolean);
    return names.length ? names.join(", ") : "None";
  }

  function pinNote(text) {
    return state.settings.require_pin === "1"
      ? "Required (Settings \u2192 Phone security), 4 to 8 digits. Staff enter it when opening the app."
      : text;
  }

  const kindWord = (d) => (d?.kind === "computer" ? "computer" : "phone");

  function editPhone(d, newKind = "phone") {
    const dialog = $("#phone-dialog");
    const form = $("#phone-form");
    form.reset();
    $("[data-title]", dialog).textContent = d ? `Edit ${d.name}` : `Add ${newKind}`;
    const kindSelect = form.elements["kind"];
    kindSelect.value = d?.kind || newKind;
    const syncKind = () => {
      const word = kindSelect.value === "computer" ? "computer" : "phone";
      dialog.querySelectorAll("[data-word]").forEach((el) => { el.textContent = word; });
      form.elements["name"].placeholder = word === "computer" ? "Front desk computer" : "Maintenance phone";
    };
    kindSelect.onchange = syncKind;
    syncKind();
    form.elements["name"].value = d ? d.name : "";
    fillRoleOptions(form.elements["role_id"], d ? d.role_id : state.roles[0]?.id);
    $("[data-pin-hint]", dialog).textContent = d
      ? (d.has_pin ? "Enter a new PIN to change it, or leave blank and tick below to remove it."
        : pinNote("Optional, 4 to 8 digits."))
      : pinNote("Optional, 4 to 8 digits. Staff enter it when opening the app.");
    const mode = form.elements["signin_mode"];
    mode.value = d?.signin_mode || "phone";
    form.elements["staff_all"].checked = d ? !!d.staff_all : true;
    form.elements["pin_only"].checked = !!d?.pin_only;
    const pick = $("[data-staff-list]", dialog);
    pick.replaceChildren(...state.staff.map((m) => h("label", { class: "check" },
      h("input", { type: "checkbox", value: m.id, checked: !!d?.staff_ids.includes(m.id) }),
      m.status === "disabled" ? `${m.name} (inactive)` : m.name)));
    $("[data-no-staff]", dialog).hidden = state.staff.length > 0;
    orderResetRow(dialog, d, "devices");
    const pageOptions = layoutPageOptions();
    const chosenPages = d?.layout_pages ?? null;
    $("[data-pages-pick]", dialog).hidden = !pageOptions.length;
    $("[data-pages-none]", dialog).hidden = pageOptions.length > 0;
    form.elements["pages_all"].checked = chosenPages === null;
    const pagesList = $("[data-pages-list]", dialog);
    pagesList.replaceChildren(...pageOptions.map((o) => h("label", { class: "check" },
      h("input", { type: "checkbox", value: o.id, checked: chosenPages === null || chosenPages.includes(o.id) }),
      o.name)));
    const syncMode = () => {
      const shared = mode.value === "staff";
      form.elements["pin"].closest("label").hidden = shared;
      $("#clear-pin")?.closest("label").toggleAttribute("hidden", shared);
      $("[data-staff-pick]", dialog).hidden = !shared;
      pick.hidden = form.elements["staff_all"].checked;
      pagesList.hidden = form.elements["pages_all"].checked;
    };
    mode.onchange = syncMode;
    form.elements["staff_all"].onchange = syncMode;
    form.elements["pages_all"].onchange = syncMode;
    let clear = $("#clear-pin");
    if (clear) clear.closest("label").remove();
    if (d?.has_pin) {
      form.elements["pin"].closest("label").after(h("label", { class: "check" },
        h("input", { type: "checkbox", id: "clear-pin" }), "Remove PIN"));
    }
    syncMode();
    dialog.onclose = () => {
      if (dialog.returnValue !== "save") return;
      const body = { name: form.elements["name"].value, role_id: form.elements["role_id"].value || null,
        kind: kindSelect.value };
      body.signin_mode = mode.value;
      if (mode.value === "staff") body.pin_only = form.elements["pin_only"].checked;
      if (pageOptions.length) {
        const all = form.elements["pages_all"].checked;
        const ticked = [...pagesList.querySelectorAll("input:checked")].map((i) => i.value);
        const was = (id) => chosenPages === null || chosenPages.includes(id);
        const changed = all !== (chosenPages === null) ||
          (!all && pageOptions.some((o) => ticked.includes(o.id) !== was(o.id)));
        if (changed) {
          // Keep choices this dialog doesn't list (the More page while it's switched off, or a
          // page that's only in the unsaved layout) so saving here doesn't quietly drop them.
          const offered = new Set(pageOptions.map((o) => o.id));
          const known = knownChoices();
          body.layout_pages = all ? null
            : [...ticked, ...(chosenPages || []).filter((id) => !offered.has(id) && known.has(id))];
        }
      }
      if (mode.value === "staff") {
        body.staff_all = form.elements["staff_all"].checked;
        body.staff_ids = [...pick.querySelectorAll("input:checked")].map((i) => Number(i.value));
      } else {
        const pin = form.elements["pin"].value.trim();
        if (pin) body.pin = pin;
        else if ($("#clear-pin")?.checked) body.pin = "";
      }
      run(async () => {
        if (d) {
          await api("PATCH", `api/devices/${d.id}`, body);
          await load();
        } else {
          const { id } = await api("POST", "api/devices", body);
          await load();
          const created = state.devices.find((x) => x.id === id);
          if (created?.setup_url) showQr(created);
          else toast(`Added. Set the staff app address in Settings to get its ${body.kind === "computer" ? "setup link" : "QR code"}.`);
        }
      }, d ? "Saved." : null);
    };
    dialog.showModal();
  }

  function showQr(d) {
    const dialog = $("#qr-dialog");
    $("[data-name]", dialog).textContent = d.name;
    const computer = d.kind === "computer";
    dialog.querySelectorAll("[data-for]").forEach((el) => {
      el.hidden = el.dataset.for !== (computer ? "computer" : "phone");
    });
    const link = $("[data-link]", dialog);
    link.value = d.setup_url;
    link.onfocus = () => link.select();
    $("[data-copy]", dialog).onclick = async () => {
      link.select();
      // The clipboard API can be blocked inside Home Assistant's frame; fall back to the old way.
      try { await navigator.clipboard.writeText(d.setup_url); } catch { document.execCommand("copy"); }
      toast("Link copied. Open it on the computer you're setting up.");
    };
    $("[data-qr]", dialog).replaceChildren(
      h("img", { src: `api/devices/${d.id}/qr.svg?t=${Date.now()}`, alt: "Setup QR code" }));
    $("[data-url]", dialog).textContent = d.setup_url;
    $("[data-expires]", dialog).textContent =
      `This code works once and expires ${new Date(d.enroll_expires * 1000).toLocaleString()}.`;
    dialog.showModal();
  }
  $("#qr-dialog [data-close]").addEventListener("click", () => {
    $("#qr-dialog").close();
    load();
  });

  $("#add-phone").addEventListener("click", () => editPhone(null, "phone"));
  $("#add-computer").addEventListener("click", () => editPhone(null, "computer"));

  function linkUser(d, kind = "devices") {
    const dialog = $("#link-dialog");
    const form = $("#link-form");
    form.reset();
    $("[data-name]", dialog).textContent = d.name;
    $("[data-error]", dialog).textContent = d.ha_link_error || "";
    if (d.ha_user) form.elements["username"].value = d.ha_user;
    const submit = $("button[value=save]", form);
    form.onsubmit = async (e) => {
      e.preventDefault();
      submit.disabled = true;
      try {
        await api("POST", `api/${kind}/${d.id}/link`, {
          username: form.elements["username"].value,
          password: form.elements["password"].value,
        });
        form.elements["password"].value = "";
        dialog.close();
        await load();
        toast(`${d.name} is now linked. Actions show in Home Assistant as that user.`);
      } catch (err) {
        $("[data-error]", dialog).textContent = err.message;
      } finally {
        submit.disabled = false;
      }
    };
    $("button[value=cancel]", form).onclick = () => {
      form.elements["password"].value = "";
      dialog.close();
    };
    dialog.showModal();
  }

  // ---------------------------------------------------------------- staff

  function phonesFor(m) {
    return state.devices.filter((d) => d.signin_mode === "staff" && d.status !== "revoked" &&
      (d.staff_all || d.staff_ids.includes(m.id))).map((d) => d.name);
  }

  function renderStaff() {
    const list = $("#staff-list");
    if (!state.staff.length) {
      list.replaceChildren(h("div", { class: "empty" },
        "No staff yet. Add the people who share phones, each with their own PIN. Then set a ",
        "phone to \u201cShared\u201d (Phones \u2192 Edit) so they can sign in on it."));
      return;
    }
    list.replaceChildren(...state.staff.map((m) => {
      const status = m.status === "disabled" ? ["Inactive", "revoked"]
        : m.locked ? ["Locked for now (wrong PINs)", "locked"] : ["Active", "active"];
      const phones = phonesFor(m);
      const actions = [h("button", { class: "btn", onclick: () => editStaff(m) }, "Edit")];
      if (m.locked) {
        actions.unshift(h("button", { class: "btn primary", onclick: () => run(async () => {
          await api("POST", `api/staff/${m.id}/unlock`);
          await load();
        }, `${m.name} can sign in again.`) }, "Unlock"));
      }
      if (m.ha_user) {
        actions.push(h("button", { class: "btn", onclick: () => linkUser(m, "staff") },
          m.ha_link_error ? "Relink HA user" : "Change HA user"));
        actions.push(h("button", { class: "btn subtle", onclick: () => run(async () => {
          if (!await askConfirm(`Unlink ${m.name} from Home Assistant user ${m.ha_user}?`, "Unlink")) return false;
          await api("DELETE", `api/staff/${m.id}/link`);
          await load();
        }, "Unlinked.") }, "Unlink"));
      } else {
        actions.push(h("button", { class: "btn", onclick: () => linkUser(m, "staff") }, "Link HA user"));
      }
      actions.push(h("button", { class: "btn danger", onclick: () => run(async () => {
        if (!await askConfirm(`Delete ${m.name}? Their past activity stays in the log.`, "Delete")) return false;
        await api("DELETE", `api/staff/${m.id}`);
        await load();
      }, "Deleted.") }, "Delete"));
      return h("article", { class: `card phone ${m.status === "disabled" ? "revoked" : ""}` },
        h("div", { class: "phone-head" },
          h("div", null,
            h("h3", null, m.name),
            h("div", { class: "meta" },
              h("span", { class: `badge ${status[1]}` }, status[0]),
              m.ha_user ? h("span", {
                class: m.ha_link_error ? "badge locked" : "badge linked",
                title: m.ha_link_error || "Actions show in Home Assistant as this user",
              }, m.ha_link_error ? `HA user ${m.ha_user}: link broken` : `HA user: ${m.ha_user}`) : null,
              clockBadge(m),
              h("span", null, `Last signed in ${ago(m.last_seen).toLowerCase()}`)),
            h("div", { class: "meta" }, phones.length
              ? `Can sign in on: ${phones.join(", ")}`
              : "Not on any shared phone yet"))),
        h("div", { class: "card-actions" }, actions));
    }));
  }

  // Connecteam link and, for clocked-in-only people, whether they're on the clock right now.
  function clockBadge(m) {
    if (!m.connecteam_user_id) return null;
    if (!m.clock) {
      return h("span", { class: "badge linked", title: m.require_clock_in
        ? "Connecteam is turned off in Settings, so the clock isn't checked"
        : "Can sign in whether or not they're clocked in" }, "Connecteam");
    }
    const [text, cls, tip] = {
      in: ["Clocked in", "active", "Can sign in now"],
      out: ["Not clocked in", "pending", "Can't sign in until they clock in on Connecteam"],
      unknown: ["Clock status unknown", "locked", state.connecteam?.last_error
        || "Connecteam hasn't answered lately, so they can't sign in"],
    }[m.clock];
    return h("span", { class: `badge ${cls}`, title: tip }, `Connecteam: ${text}`);
  }

  // "Reset to your layout" for a phone or person that rearranged things on the phone.
  // Takes effect straight away.
  function orderResetRow(dialog, target, kind) {
    const row = $("[data-order-reset]", dialog);
    row.hidden = !target?.custom_order;
    $("button", row).onclick = () => run(async () => {
      await api("DELETE", `api/${kind}/${target.id}/order`);
      await load();
      row.hidden = true;
    }, "Back to your layout. The phone updates within a few seconds.");
  }

  // ctUser: a Connecteam user picked in "Add from Connecteam" ({id, name}).
  function editStaff(m, ctUser) {
    const dialog = $("#staff-dialog");
    const form = $("#staff-form");
    form.reset();
    $("[data-title]", dialog).textContent = m ? `Edit ${m.name}`
      : ctUser ? "Add from Connecteam" : "Add staff member";
    form.elements["name"].value = m ? m.name : ctUser ? ctUser.name.slice(0, 40) : "";
    form.elements["pin"].required = !m;
    $("[data-pin-hint]", dialog).textContent = m
      ? "Leave blank to keep their current PIN."
      : ctUser ? "Connecteam has no kiosk code for them that works as a PIN, so choose 4 to 8 digits."
      : "4 to 8 digits. They enter it after tapping their name.";
    form.elements["active"].checked = !m || m.status !== "disabled";
    form.elements["active"].closest("label").hidden = !m;
    // Connecteam: shown for people who are (or are being) linked.
    let ctId = m ? m.connecteam_user_id : ctUser?.id;
    const ctBox = $("[data-ct]", dialog);
    ctBox.hidden = !ctId;
    $("[data-ct-label]", dialog).textContent = ctUser ? `Connecteam: ${ctUser.name}` : "Linked to Connecteam";
    form.elements["require_clock_in"].checked = m ? !!m.require_clock_in : true;
    // Their Connecteam kiosk code as the PIN: fetched by the add-on, never shown here.
    const kiosk = form.elements["use_kiosk_code"];
    kiosk.closest("label").hidden = ctUser ? !ctUser.has_kiosk_code : !m?.connecteam_user_id;
    $("[data-kiosk-label]", dialog).textContent = m
      ? "Change their PIN to their current Connecteam kiosk code" : "Use their Connecteam kiosk code as their PIN";
    kiosk.checked = !!ctUser?.has_kiosk_code;
    const pinRow = $("[data-pin-row]", dialog);
    kiosk.onchange = () => {
      pinRow.hidden = kiosk.checked;
      form.elements["pin"].required = !m && !kiosk.checked;
    };
    kiosk.onchange();
    const unlink = $("[data-ct-unlink]", dialog);
    unlink.hidden = !m;
    unlink.onclick = () => {
      ctId = null;
      kiosk.checked = false;
      kiosk.onchange();
      ctBox.hidden = true;
      toast("Click Save to unlink them from Connecteam.");
    };
    orderResetRow(dialog, m, "staff");
    dialog.onclose = () => {
      if (dialog.returnValue !== "save") return;
      const body = { name: form.elements["name"].value };
      const pin = form.elements["pin"].value.trim();
      if (pin) body.pin = pin;
      if (m) body.active = form.elements["active"].checked;
      if (ctId) {
        body.connecteam_user_id = ctId;
        body.require_clock_in = form.elements["require_clock_in"].checked;
        if (kiosk.checked) {
          body.use_kiosk_code = true;
          delete body.pin;
        }
      } else if (m?.connecteam_user_id) {
        body.connecteam_user_id = null;
      }
      run(async () => {
        if (m) await api("PATCH", `api/staff/${m.id}`, body);
        else await api("POST", "api/staff", body);
        await load();
      }, m ? "Saved." : `${body.name} added.`);
    };
    dialog.showModal();
  }

  $("#add-staff").addEventListener("click", () => editStaff(null));

  // Pick someone from the Connecteam user list, then finish adding them in the staff dialog.
  $("#add-from-connecteam").addEventListener("click", () => {
    const dialog = $("#ct-dialog");
    const msg = $("[data-ct-msg]", dialog);
    const search = $("[data-ct-search]", dialog);
    const list = $("[data-ct-users]", dialog);
    msg.textContent = "Loading your Connecteam users\u2026";
    search.hidden = true;
    search.value = "";
    list.replaceChildren();
    let users = [];
    const draw = () => {
      const q = search.value.trim().toLowerCase();
      const shown = users.filter((u) => u.name.toLowerCase().includes(q));
      list.replaceChildren(...shown.map((u) => h("button", {
        type: "button", class: "ct-user", disabled: !!u.linked_to,
        onclick: () => { dialog.close(); editStaff(null, u); },
      }, h("span", null, u.name),
        h("small", null, u.linked_to ? `Already added as ${u.linked_to}`
          : u.has_kiosk_code ? "Add" : "Add (no kiosk code)"))));
      if (!shown.length) list.replaceChildren(h("p", { class: "hint" }, users.length ? "No one matches." : "No active users in Connecteam."));
    };
    search.oninput = draw;
    dialog.showModal();
    api("GET", "api/connecteam/users").then((data) => {
      users = data.users;
      msg.textContent = "Choose who to add. Their Connecteam kiosk code becomes their PIN.";
      search.hidden = false;
      draw();
      search.focus();
    }, (err) => { msg.textContent = err.message; });
  });

  // ---------------------------------------------------------------- roles

  function renderRoleList() {
    const list = $("#role-list");
    list.replaceChildren(...state.roles.map((r) => {
      const count = Object.keys(r.entities).length;
      const areaCount = Object.keys(r.areas).length;
      const parts = [`${count} device${count === 1 ? "" : "s"}`];
      if (areaCount) parts.push(`${areaCount} area${areaCount === 1 ? "" : "s"}`);
      parts.push(`${r.device_count} phone${r.device_count === 1 ? "" : "s"}`);
      return h("li", null, h("button", {
        class: editing?.id === r.id ? "role-item active" : "role-item",
        onclick: () => openRole(r.id),
      }, h("span", { class: "role-name" }, r.name),
      h("span", { class: "role-meta" }, parts.join(" · "))));
    }));
    if (!state.roles.length) list.replaceChildren(h("li", { class: "empty small" }, "No roles yet."));
  }

  async function loadEntities() {
    try {
      const data = await api("GET", "api/entities");
      entities = data.entities;
      haAreas = data.ha_areas;
    } catch (err) {
      entities = null;
      toast(err.message, true);
    }
    if (editing) renderRoleEditor();
    if (!$("#tab-layout").hidden) renderLayoutEditor();
  }

  async function openRole(id) {
    if (editing?.dirty && editing.id !== id && !await askConfirm("Discard unsaved changes?", "Discard")) return;
    const role = state.roles.find((r) => r.id === id);
    editing = { id, name: role.name, entities: { ...role.entities }, areas: { ...role.areas },
      dirty: false, filter: "", area: "", onlySelected: false };
    renderRoleList();
    renderRoleEditor();
  }

  $("#add-role").addEventListener("click", async () => {
    const name = await askText("Role name (for example Maintenance or Housekeeping):");
    if (!name) return;
    run(async () => {
      const { id } = await api("POST", "api/roles", { name });
      await load();
      openRole(id);
    });
  });

  function setLevel(entityId, level) {
    if (level === "none") delete editing.entities[entityId];
    else editing.entities[entityId] = level;
    editing.dirty = true;
    $("#save-role")?.removeAttribute("disabled");
  }

  const RANK = { none: 0, view: 1, control: 2 };
  const LABELS = { none: "Hidden", view: "View", control: "Control" };

  // The strongest access this role gets to a device through its areas, and which area gives it.
  function accessFromAreas(e) {
    let best = { level: "none", area: null };
    for (const id of e.area_ids || []) {
      const level = editing.areas[id] || "none";
      if (RANK[level] > RANK[best.level]) best = { level, area: state.areas.find((a) => a.id === id) };
    }
    if (best.level === "control" && !e.controllable) best.level = "view";
    return best;
  }

  // Always three equal-width buttons so the choices line up down the page. Options a device
  // can't have (Control on a sensor) are shown greyed out instead of being left out.
  function segmented(options, current, label, onPick) {
    const wrap = h("div", { class: "seg", role: "radiogroup", "aria-label": label });
    for (const opt of ["none", "view", "control"]) {
      const allowed = options.includes(opt);
      const btn = h("button", {
        type: "button", role: "radio", "aria-checked": String(current === opt), class: `seg-${opt}`,
        disabled: !allowed, title: allowed ? null : "This kind of device can only be viewed",
      }, LABELS[opt]);
      btn.addEventListener("click", () => {
        wrap.querySelectorAll("button").forEach((b) => b.setAttribute("aria-checked", String(b === btn)));
        onPick(opt);
      });
      wrap.append(btn);
    }
    return wrap;
  }

  function levelControl(e) {
    const options = e.controllable ? ["none", "view", "control"] : ["none", "view"];
    return segmented(options, editing.entities[e.entity_id] || "none", `Access to ${e.name}`,
      (level) => {
        setLevel(e.entity_id, level);
        updateCount();
      });
  }

  function updateCount() {
    const n = Object.keys(editing.entities).length;
    const el = $("#role-count");
    if (el) el.textContent = `${n} device${n === 1 ? "" : "s"} selected`;
  }

  function areaSection(redraw) {
    if (!state.areas.length) {
      return h("div", { class: "role-areas empty-areas" },
        h("h3", { class: "editor-sub" }, "Areas"),
        h("p", { class: "hint" }, "Create areas such as \u201cSoaking Area\u201d in ",
          h("button", { type: "button", class: "linkish", onclick: () => selectTab("settings") }, "Settings"),
          " to give access to a whole area at once."));
    }
    return h("div", { class: "role-areas" },
      h("h3", { class: "editor-sub" }, "Areas"),
      h("p", { class: "hint" }, "Covers every device in the area, including ones added later. ",
        "A device gets whichever is higher: its area\u2019s setting or its own setting below."),
      state.areas.map((a) => h("div", { class: "entity-row" },
        h("div", { class: "entity-text" },
          h("div", { class: "entity-name" }, a.name),
          h("div", { class: "entity-id" }, areaSummary(a))),
        segmented(["none", "view", "control"], editing.areas[a.id] || "none", `Access to ${a.name}`,
          (level) => {
            if (level === "none") delete editing.areas[a.id];
            else editing.areas[a.id] = level;
            editing.dirty = true;
            $("#save-role")?.removeAttribute("disabled");
            redraw();
          }))));
  }

  function areaSummary(a) {
    const names = a.ha_areas.map((id) => haAreas.find((x) => x.id === id)?.name || id);
    const parts = [];
    if (names.length) parts.push(`Home Assistant: ${names.join(", ")}`);
    if (a.entities.length) parts.push(`${a.entities.length} added device${a.entities.length === 1 ? "" : "s"}`);
    return parts.join(" \u00b7 ") || "Empty";
  }

  function renderRoleEditor() {
    const box = $("#role-editor");
    if (!editing) {
      box.replaceChildren(h("div", { class: "empty" },
        "Pick a role, or create one like “Maintenance” or “Housekeeping.”"));
      return;
    }
    const nameInput = h("input", { value: editing.name, maxlength: "60", "aria-label": "Role name" });
    nameInput.addEventListener("input", () => {
      editing.name = nameInput.value;
      editing.dirty = true;
      $("#save-role").removeAttribute("disabled");
    });
    const search = h("input", { type: "search", placeholder: "Search devices", value: editing.filter });
    const areas = entities ? [...new Set(entities.map((e) => e.area).filter(Boolean))].sort() : [];
    const areaSel = h("select", { "aria-label": "Area" }, h("option", { value: "" }, "All areas"),
      areas.map((a) => h("option", { value: a, selected: a === editing.area }, a)),
      h("option", { value: "~", selected: editing.area === "~" }, "No area"));
    const only = h("input", { type: "checkbox", checked: editing.onlySelected });
    const listBox = h("div", { class: "entity-list" });

    const drawList = () => {
      if (!entities) {
        listBox.replaceChildren(h("div", { class: "empty" }, "Loading devices from Home Assistant…"));
        return;
      }
      const q = editing.filter.toLowerCase();
      const shown = entities.filter((e) =>
        (!q || e.name.toLowerCase().includes(q) || e.entity_id.includes(q)) &&
        (!editing.area || (editing.area === "~" ? !e.area : e.area === editing.area)) &&
        (!editing.onlySelected || editing.entities[e.entity_id]));
      if (!shown.length) {
        listBox.replaceChildren(h("div", { class: "empty" }, "No devices match."));
        return;
      }
      const groups = new Map();
      for (const e of shown) {
        const k = e.area || "No area";
        if (!groups.has(k)) groups.set(k, []);
        groups.get(k).push(e);
      }
      listBox.replaceChildren(...[...groups].map(([area, list]) => {
        const bulk = (level) => () => {
          list.forEach((e) => setLevel(e.entity_id,
            level === "control" && !e.controllable ? "view" : level));
          drawList();
          updateCount();
        };
        return h("div", { class: "area-group" },
          h("div", { class: "area-head" }, h("h4", null, area),
            h("div", { class: "bulk" }, "Set all:",
              h("button", { type: "button", class: "linkish", onclick: bulk("none") }, "Hidden"),
              h("button", { type: "button", class: "linkish", onclick: bulk("view") }, "View"),
              h("button", { type: "button", class: "linkish", onclick: bulk("control") }, "Control"))),
          list.map((e) => {
            const via = accessFromAreas(e);
            return h("div", { class: "entity-row" },
              h("div", { class: "entity-text" },
                h("div", { class: "entity-name" }, e.name),
                h("div", { class: "entity-id", title: e.entity_id }, `${e.entity_id} · ${e.state}`),
                via.area && RANK[via.level] >= RANK[editing.entities[e.entity_id] || "none"]
                  ? h("div", { class: "via" }, `${LABELS[via.level]} via ${via.area.name}`)
                  : null),
              levelControl(e));
          }));
      }));
    };

    search.addEventListener("input", () => { editing.filter = search.value; drawList(); });
    areaSel.addEventListener("change", () => { editing.area = areaSel.value; drawList(); });
    only.addEventListener("change", () => { editing.onlySelected = only.checked; drawList(); });

    const save = h("button", { class: "btn primary", id: "save-role", disabled: !editing.dirty }, "Save role");
    save.addEventListener("click", () => run(async () => {
      await api("PUT", `api/roles/${editing.id}`,
        { name: editing.name, entities: editing.entities, areas: editing.areas });
      editing.dirty = false;
      save.setAttribute("disabled", "");
      await load();
    }, "Role saved. Phones pick up the change within a few seconds."));
    const dup = h("button", { class: "btn" }, "Duplicate");
    dup.addEventListener("click", async () => {
      const name = await askText(editing.dirty
        ? "Name for the copy. It gets your unsaved changes; this role stays as it was last saved."
        : "Name for the copy. No phones move to it until you pick it on a phone.", "", `${editing.name} (copy)`);
      if (!name) return;
      run(async () => {
        const { id } = await api("POST", `api/roles/${editing.id}/duplicate`, { name });
        if (editing.dirty) {
          await api("PUT", `api/roles/${id}`, { name, entities: editing.entities, areas: editing.areas });
        }
        editing.dirty = false;
        await load();
        openRole(id);
      }, `Created ${name}. Pick it on a phone under Phones → Edit.`);
    });
    const del = h("button", { class: "btn danger" }, "Delete role");
    del.addEventListener("click", async () => {
      const role = state.roles.find((r) => r.id === editing.id);
      const extra = role?.device_count ? ` ${role.device_count} phone(s) will lose all access.` : "";
      if (!await askConfirm(`Delete the ${editing.name} role?${extra}`, "Delete role")) return;
      run(async () => {
        await api("DELETE", `api/roles/${editing.id}`);
        editing = null;
        await load();
        renderRoleEditor();
      }, "Role deleted.");
    });

    box.replaceChildren(
      h("div", { class: "editor-head" },
        h("label", { class: "role-name-field" }, "Role name", nameInput),
        h("div", { class: "editor-actions" }, del, dup, save)),
      h("p", { class: "hint" },
        "View lets staff see a device’s status. Control also lets them operate it. ",
        "Sensors can only be viewed. Anything left Hidden doesn’t exist as far as the phone is concerned."),
      areaSection(drawList),
      h("h3", { class: "editor-sub" }, "Individual devices"),
      h("div", { class: "filters" }, search, areaSel,
        h("label", { class: "check" }, only, "Only selected"),
        h("span", { class: "count", id: "role-count" }),
        h("button", { type: "button", class: "linkish", onclick: () => { entities = null; drawList(); loadEntities(); } },
          "Reload devices")),
      listBox);
    updateCount();
    drawList();
  }

  window.addEventListener("beforeunload", (e) => {
    if (editing?.dirty || layoutDraft?.dirty) e.preventDefault();
  });

  // ---------------------------------------------------------------- activity

  async function loadActivity(reset) {
    const rows = $("#activity-rows");
    if (reset) { oldestActivity = null; rows.replaceChildren(); }
    await run(async () => {
      const q = oldestActivity ? `?before=${oldestActivity}` : "";
      const { activity } = await api("GET", `api/activity${q}`);
      if (reset && !activity.length) {
        rows.replaceChildren(h("tr", null, h("td", { colspan: "4", class: "empty" }, "Nothing yet.")));
      }
      for (const a of activity) {
        rows.append(h("tr", { class: a.ok ? "" : "failed" },
          h("td", { class: "nowrap" }, when(a.ts)),
          h("td", null, a.actor, a.role ? h("div", { class: "sub" }, a.role) : null),
          h("td", null, a.action.replace(/_/g, " "), a.detail ? h("div", { class: "sub" }, a.detail) : null),
          h("td", null, a.entity_id || "")));
      }
      if (activity.length) oldestActivity = activity[activity.length - 1].ts;
      $("#more-activity").hidden = activity.length < 200;
    });
  }
  $("#refresh-activity").addEventListener("click", () => loadActivity(true));
  $("#more-activity").addEventListener("click", () => loadActivity(false));

  // ---------------------------------------------------------------- areas (in Settings)

  function renderAreaSettings() {
    const list = $("#area-list");
    if (!state.areas.length) {
      list.replaceChildren(h("div", { class: "empty small" }, "No areas yet."));
      return;
    }
    list.replaceChildren(...state.areas.map((a) => h("div", { class: "area-row sort-row" },
      dragHandle(`Reorder area ${a.name}`),
      h("div", { class: "entity-text" },
        h("div", { class: "entity-name" }, a.name),
        h("div", { class: "entity-id" }, areaSummary(a))),
      h("div", { class: "area-actions" },
        h("button", { type: "button", class: "btn", onclick: () => editArea(a) }, "Edit"),
        h("button", { type: "button", class: "btn danger", onclick: () => run(async () => {
          if (!await askConfirm(`Delete the ${a.name} area? Roles lose any access they had through it.`, "Delete area")) return false;
          await api("DELETE", `api/areas/${a.id}`);
          await load();
        }, "Area deleted.") }, "Delete")))));
  }

  // Area order is saved straight away. Update the list locally first so a quick second move
  // starts from what's on screen, save one change at a time, and always reload afterwards so
  // a failed save puts the list back to what the server has.
  let areaSaves = Promise.resolve();
  sortable($("#area-list"), "areas", (from, to) => {
    moveItem(state.areas, from, to);
    renderAreaSettings();
    const order = state.areas.map((a) => a.id);
    areaSaves = areaSaves.then(() => run(async () => {
      try {
        await api("PUT", "api/areas/order", { order });
      } finally {
        await load();
      }
    }));
  });

  async function editArea(a) {
    if (!entities) await loadEntities();
    if (!entities) return;
    const dialog = $("#area-dialog");
    const form = $("#area-form");
    form.reset();
    $("[data-title]", dialog).textContent = a ? `Edit ${a.name}` : "Add area";
    form.elements["name"].value = a ? a.name : "";
    const chosenHa = new Set(a ? a.ha_areas : []);
    const chosen = new Set(a ? a.entities : []);
    $("[data-ha-areas]", dialog).replaceChildren(...(haAreas.length
      ? haAreas.map((x) => h("label", { class: "check" },
        h("input", { type: "checkbox", checked: chosenHa.has(x.id),
          onchange: (ev) => (ev.target.checked ? chosenHa.add(x.id) : chosenHa.delete(x.id)) }),
        x.name))
      : [h("p", { class: "hint" }, "No areas are set up in Home Assistant.")]));
    const box = $("[data-devices]", dialog);
    const search = $("[data-search]", dialog);
    const only = $("[data-only]", dialog);
    const count = () => { $("[data-count]", dialog).textContent = `(${chosen.size} selected)`; };
    const draw = () => {
      const q = search.value.toLowerCase();
      const shown = entities.filter((e) => (!q || e.name.toLowerCase().includes(q) || e.entity_id.includes(q)) &&
        (!only.checked || chosen.has(e.entity_id)));
      box.replaceChildren(...shown.slice(0, 400).map((e) => h("label", { class: "check pick-row" },
        h("input", { type: "checkbox", checked: chosen.has(e.entity_id), onchange: (ev) => {
          if (ev.target.checked) chosen.add(e.entity_id); else chosen.delete(e.entity_id);
          count();
        } }),
        h("span", null, e.name, h("small", { title: e.entity_id }, e.ha_area ? ` \u00b7 ${e.ha_area}` : "")))));
      if (shown.length > 400) box.append(h("p", { class: "hint" }, `Showing 400 of ${shown.length}. Search to narrow it down.`));
      if (!shown.length) box.append(h("p", { class: "hint" }, "No devices match."));
    };
    search.oninput = draw;
    search.onkeydown = (ev) => { if (ev.key === "Enter") ev.preventDefault(); };
    only.onchange = draw;
    count();
    draw();
    dialog.onclose = () => {
      if (dialog.returnValue !== "save") return;
      const name = form.elements["name"].value;
      run(async () => {
        const id = a ? a.id : (await api("POST", "api/areas", { name })).id;
        await api("PUT", `api/areas/${id}`, { name, ha_areas: [...chosenHa], entities: [...chosen] });
        await load();
        await loadEntities(); // device headings and area membership changed
      }, a ? "Area saved." : `${name} added.`);
    };
    dialog.showModal();
  }

  $("#add-area").addEventListener("click", () => editArea(null));

  // ---------------------------------------------------------------- settings

  function refreshIcon() {
    const t = Date.now();
    $("#icon-preview").src = `icon-192.png?t=${t}`;
    $("#top-icon").src = `icon-192.png?t=${t}`;
    $("#remove-logo").hidden = !state.hasLogo;
  }

  function fillSettings() {
    const form = $("#settings-form");
    for (const [k, v] of Object.entries(state.settings)) {
      const field = form.elements[k];
      if (field?.type === "checkbox") field.checked = v === "1";
      else if (field) field.value = v;
    }
    renderTunnelSources();
    renderConnecteam();
    refreshIcon();
  }

  function renderConnecteam() {
    const form = $("#settings-form");
    const saved = state.settings.connecteam_secret_set === "1";
    const secret = form.elements["connecteam_client_secret"];
    secret.value = "";
    secret.placeholder = saved ? "Saved — leave blank to keep" : "";
    $("#ct-clear-row").hidden = !saved;
    const ct = state.connecteam || {};
    const lines = [];
    if (!ct.enabled) lines.push("Turned off.");
    else {
      lines.push(ct.halted ? `Stopped: ${ct.halted} Fix it, then save the settings or click Test connection.`
        : ct.paused ? `Paused: ${ct.paused}`
        : ct.last_error ? `Problem: ${ct.last_error}`
        : ct.checked_at ? `Connected. Checked ${ago(ct.checked_at).toLowerCase()}; ${ct.clocked_in} clocked in.`
        : "Not checked yet.");
      if (!ct.needed) lines.push("No one needs to be clocked in to sign in, so the time clock isn't being checked.");
      else lines.push(`Checking about every ${Math.max(1, Math.round(ct.interval / 60))} min.`);
      let use = `Requests today: ${ct.used_today} (Staff Access stops at ${ct.daily_cap})`;
      if (ct.account_limit) use += `; your account has ${ct.account_left} of ${ct.account_limit} left`;
      lines.push(use + ".");
    }
    $("#ct-status").replaceChildren(...lines.flatMap((t, i) => (i ? [h("br"), t] : [t])));
  }

  // Tests the saved keys, so Save settings first.
  $("#ct-test").addEventListener("click", () => run(async () => {
    try {
      const r = await api("POST", "api/connecteam/test");
      toast(`Connected: ${r.users} users, ${r.time_clocks} time clock${r.time_clocks === 1 ? "" : "s"}, ` +
        `${r.clocked_in} clocked in now.`);
    } finally {
      await load();
      renderConnecteam();
    }
  }));

  // Where Cloudflare traffic has arrived from lately, so the tunnel address can be filled in.
  function renderTunnelSources() {
    const box = $("#tunnel-sources");
    const sources = state.tunnelSources || [];
    if (!sources.length) {
      box.replaceChildren(h("p", { class: "hint" }, "No traffic from Cloudflare seen since the add-on started. " +
        "Open the staff app through your https address once and reload this page to see where it comes from."));
      return;
    }
    const input = $("#settings-form").elements["tunnel_addresses"];
    box.replaceChildren(h("p", { class: "hint" }, "Cloudflare traffic recently came from:"),
      ...sources.slice(0, 5).map((src) => h("div", { class: "tunnel-row" },
        h("code", null, src.address),
        h("span", { class: "hint" }, ` ${ago(src.last_seen).toLowerCase()}, ${src.count} request${src.count === 1 ? "" : "s"} \u00b7 `,
          src.ha_network ? "the Cloudflared add-on (allowed)" : src.allowed ? "allowed" : "not allowed yet"),
        !src.allowed ? h("button", { type: "button", class: "btn", onclick: () => {
          input.value = [...input.value.split(/[\s,]+/).filter(Boolean), src.address].join(", ");
          toast(`Added ${src.address}. Click Save settings to keep it.`);
        } }, "Use this address") : null)),
      h("p", { class: "hint" }, "Only add an address you recognize as the computer running cloudflared."));
  }

  $("#settings-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.target).entries());
    body.ha_logbook = e.target.elements["ha_logbook"].checked ? "1" : "0";
    for (const name of ["allow_phone_reorder", "require_pin", "phones_via_tunnel_only", "control_lock",
      "connecteam_enabled", "connecteam_clear_secret"]) {
      body[name] = e.target.elements[name].checked ? "1" : "0";
    }
    run(async () => {
      try {
        await api("PUT", "api/settings", body);
      } catch (err) {
        // A refusal (such as turning on tunnel-only too early) may name a newly seen address.
        await load();
        renderTunnelSources();
        throw err;
      }
      await load();
      fillSettings();
    }, "Settings saved.");
  });

  $("#logo-file").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("logo", file);
    run(async () => {
      await api("POST", "api/logo", fd);
      await load();
      refreshIcon();
    }, "Logo uploaded. Phones show the new icon next time it's added to the home screen.");
    e.target.value = "";
  });

  $("#remove-logo").addEventListener("click", () => run(async () => {
    await api("DELETE", "api/logo");
    await load();
    refreshIcon();
  }, "Logo removed."));

  // ---------------------------------------------------------------- layout

  // Working copy of the layout; saved with "Save layout".
  let layoutDraft = null;
  let pageSel = null;   // the open page's ID...
  let groupSel = null;  // ...or the open page group's ID
  const MAX_PAGES = 20;
  const MAX_PAGE_GROUPS = 12;
  const newId = () => Math.random().toString(36).slice(2, 10);
  const entityById = (id) => entities?.find((e) => e.entity_id === id);
  const entityName = (id) => entityById(id)?.name || id;

  function draft() {
    if (!layoutDraft) {
      layoutDraft = JSON.parse(JSON.stringify(state.layout || { pages: [], show_unplaced: true }));
      layoutDraft.page_groups ||= [];
      layoutDraft.dirty = false;
    }
    return layoutDraft;
  }

  function markLayoutDirty() {
    draft().dirty = true;
    $("#save-layout").disabled = false;
    $("#discard-layout").disabled = false;
  }

  function renderLayoutEditor() {
    const d = draft();
    $("#show-unplaced").checked = d.show_unplaced !== false;
    $("#save-layout").disabled = !d.dirty;
    $("#discard-layout").disabled = !d.dirty;
    if (groupSel && !d.page_groups.some((g) => g.id === groupSel)) groupSel = null;
    if (!groupSel && !d.pages.some((p) => p.id === pageSel)) pageSel = d.pages[0]?.id ?? null;
    const blocks = pageBlocks(d);
    $("#page-list").replaceChildren(...(blocks.length
      ? blocks.map((b) => (b.page ? pageRow(b.page) : groupRow(b)))
      : [h("li", { class: "empty small" }, "No pages yet. Phones use the automatic layout.")]));
    if (groupSel) renderGroupEditor();
    else renderPageEditor();
  }

  function pageRow(p) {
    return h("li", { class: "sort-row" },
      dragHandle(`Reorder page ${p.name}`),
      h("button", { class: !groupSel && p.id === pageSel ? "role-item active" : "role-item",
        onclick: () => { pageSel = p.id; groupSel = null; renderLayoutEditor(); } },
      h("span", { class: "role-name" }, p.name),
      h("span", { class: "role-meta" }, `${p.items.length} item${p.items.length === 1 ? "" : "s"}`)));
  }

  // A page group in the page list: its name, then its pages, which reorder within the group.
  function groupRow({ group, pages }) {
    const list = h("ul", { class: "group-pages" }, pages.length ? pages.map(pageRow)
      : [h("li", { class: "empty small" }, "No pages yet")]);
    sortable(list, `group-${group.id}`, (from, to) => {
      const d = draft();
      const inGroup = d.pages.filter((p) => p.page_group === group.id);
      moveItem(inGroup, from, to);
      let next = 0;
      d.pages = d.pages.map((p) => (p.page_group === group.id ? inGroup[next++] : p));
      markLayoutDirty();
      renderLayoutEditor();
    });
    return h("li", { class: "sort-row page-group" },
      dragHandle(`Reorder group ${group.name}`),
      h("div", { class: "page-group-box" },
        h("button", { class: groupSel === group.id ? "role-item group-item active" : "role-item group-item",
          onclick: () => { groupSel = group.id; renderLayoutEditor(); } },
        h("span", { class: "role-name" }, group.name),
        h("span", { class: "role-meta" }, `Group \u00b7 ${pages.length} page${pages.length === 1 ? "" : "s"}`)),
        list));
  }

  // The page group a page is in, if any.
  function groupOf(page, d = draft()) {
    return page.page_group ? d.page_groups.find((g) => g.id === page.page_group) || null : null;
  }

  // The page list's order: single pages, and page groups with their pages side by side (where
  // the group's first page is, which is also the order phones show them in). Groups with no
  // pages yet come last.
  function pageBlocks(d) {
    const blocks = [];
    const seen = new Set();
    for (const page of d.pages) {
      const group = groupOf(page, d);
      if (!group) {
        blocks.push({ page });
      } else if (!seen.has(group.id)) {
        seen.add(group.id);
        blocks.push({ group, pages: d.pages.filter((p) => p.page_group === group.id) });
      }
    }
    for (const group of d.page_groups) if (!seen.has(group.id)) blocks.push({ group, pages: [] });
    return blocks;
  }

  function setBlocks(d, blocks) {
    d.pages = blocks.flatMap((b) => (b.page ? [b.page] : b.pages));
    d.page_groups = blocks.filter((b) => b.group).map((b) => b.group);
  }

  sortable($("#page-list"), "pages", (from, to) => {
    const d = draft();
    const blocks = pageBlocks(d);
    moveItem(blocks, from, to);
    setBlocks(d, blocks);
    markLayoutDirty();
    renderLayoutEditor();
  });

  const ACCESS_TEXT = { none: "Hidden", view: "View only", control: "Control" };

  // How an item's access override reads in the item list ("" when it follows the role).
  function accessNote(item) {
    const own = item.type === "group" ? item.entities.filter((m) => m.access).length : 0;
    const parts = [];
    if (item.access) parts.push(`Access: ${ACCESS_TEXT[item.access]} on every phone`);
    if (own) parts.push(`${own} device${own === 1 ? "" : "s"} with their own access`);
    return parts.join(" \u00b7 ");
  }

  function itemSummary(item) {
    if (item.type === "heading") return ["Heading", item.title, item.collapsed ? "Starts collapsed" : ""];
    if (item.type === "device") {
      return ["Device", item.label || entityName(item.entity_id), item.entity_id];
    }
    if (item.type === "area") {
      const area = state.areas.find((a) => a.id === item.area_id);
      return ["Area", item.title || area?.name || "Deleted area",
        "Every device in the area the role allows" + (item.collapsed ? " \u00b7 starts collapsed" : "")];
    }
    const onCard = item.entities.filter((m) => m.summary).map((m) => m.label || entityName(m.entity_id));
    const devices = item.entities.filter((m) => m.entity_id).length;
    const sections = item.entities.filter((m) => m.type === "divider").length;
    return ["Group card", item.title,
      `${devices} device${devices === 1 ? "" : "s"}` +
      (sections ? ` \u00b7 ${sections} divider${sections === 1 ? "" : "s"}` : "") +
      (onCard.length ? ` \u00b7 card shows ${onCard.join(", ")}` : "")];
  }

  function renderPageEditor() {
    const box = $("#page-editor");
    const d = draft();
    const page = d.pages.find((p) => p.id === pageSel);
    if (!page) {
      box.replaceChildren(h("div", { class: "empty" },
        "Add a page to start a custom layout, for example \u201cSoaking\u201d or \u201cMechanical.\u201d " +
        "Put pages in a group (such as \u201cPool staff\u201d) to give them to phones together."));
      return;
    }
    const add = (type) => editItem(page, null, type);
    const group = groupOf(page, d);
    const groupPick = d.page_groups.length ? h("label", { class: "inline-select" }, "Group",
      h("select", { onchange: (e) => {
        if (e.target.value) page.page_group = e.target.value;
        else delete page.page_group;
        setBlocks(d, pageBlocks(d));
        markLayoutDirty();
        renderLayoutEditor();
      } },
      h("option", { value: "" }, "No group"),
      ...d.page_groups.map((g) => h("option", { value: g.id, selected: g.id === group?.id }, g.name)))) : null;
    box.replaceChildren(
      h("div", { class: "editor-head" },
        h("label", { class: "role-name-field" }, "Page name",
          h("input", { value: page.name, maxlength: "30", oninput: (e) => {
            page.name = e.target.value;
            markLayoutDirty();
            const btn = $("#page-list .role-item.active .role-name");
            if (btn) btn.textContent = page.name;
          } })),
        h("div", { class: "editor-actions" },
          h("button", { class: "btn", onclick: () => duplicatePage(page) }, "Duplicate page"),
          h("button", { class: "btn danger", onclick: async () => {
            if (!await askConfirm(`Delete the ${page.name} page?`, "Delete page")) return;
            d.pages = d.pages.filter((p) => p !== page);
            pageSel = null;
            markLayoutDirty();
            renderLayoutEditor();
          } }, "Delete page"))),
      group
        ? h("div", { class: "shown-on" }, groupPick,
          h("span", null, "Shown on: ", h("strong", null, shownOnText(`group:${group.id}`)),
            ` (with the ${group.name} group)`),
          h("button", { class: "btn", onclick: () => { groupSel = group.id; renderLayoutEditor(); } }, "Open group"))
        : h("div", { class: "shown-on" }, groupPick,
          h("span", null, "Shown on: ", h("strong", null, shownOnText(page.id))),
          h("button", { class: "btn", onclick: () => choosePhones(page.id, page.name) }, "Choose devices")),
      h("div", { class: "add-row" }, "Add:",
        h("button", { class: "btn", onclick: () => add("group") }, "Group card"),
        h("button", { class: "btn", onclick: () => add("device") }, "Device"),
        h("button", { class: "btn", onclick: () => add("area") }, "Whole area"),
        h("button", { class: "btn", onclick: () => add("heading") }, "Heading")),
      page.items.length
        ? itemList(page)
        : h("div", { class: "empty" }, "This page is empty. Add a group card, device, area or heading."));
  }

  // A copy of an item, with its own ID, named "(copy)" where it has a title.
  function copyOf(item) {
    const copy = JSON.parse(JSON.stringify(item));
    copy.id = newId();
    if (copy.type === "group" || copy.type === "heading") copy.title = `${copy.title} (copy)`.slice(0, 60);
    return copy;
  }

  // Duplicate puts the copy right below and opens it, since it usually needs renaming and
  // different devices (Tub 1 -> Tub 2).
  function duplicateItem(page, index) {
    page.items.splice(index + 1, 0, copyOf(page.items[index]));
    markLayoutDirty();
    renderLayoutEditor();
    editItem(page, index + 1, page.items[index + 1].type);
  }

  // A copy of a page with new IDs for it and its items, in the same group. Item titles stay
  // as they are.
  function copyPage(page, name = page.name) {
    const copy = { id: newId(), name: name.slice(0, 30),
      items: page.items.map((item) => ({ ...copyOf(item), title: item.title })) };
    for (const item of copy.items) if (item.title === undefined) delete item.title;
    if (page.page_group) copy.page_group = page.page_group;
    return copy;
  }

  function roomForPages(count) {
    if (draft().pages.length + count <= MAX_PAGES) return true;
    toast(`A layout can have at most ${MAX_PAGES} pages.`, true);
    return false;
  }

  function duplicatePage(page) {
    const d = draft();
    if (!roomForPages(1)) return;
    const copy = copyPage(page, `${page.name} (copy)`);
    d.pages.splice(d.pages.indexOf(page) + 1, 0, copy);
    pageSel = copy.id;
    markLayoutDirty();
    renderLayoutEditor();
    const group = groupOf(copy, d);
    const picky = state.devices.filter((x) => x.status !== "revoked" && x.layout_pages != null).length;
    toast(group ? `Copied as ${copy.name}, in the ${group.name} group. Rename it, then Save layout.`
      : picky ? `Copied as ${copy.name}. ${picky} device${picky === 1 ? " is" : "s are"} set to specific pages and won't show it until you add it (Choose devices).`
      : `Copied as ${copy.name}. Rename it, then Save layout.`);
  }

  async function addPage(group) {
    if (!roomForPages(1)) return;
    const name = await askText(group ? `Name of the new page in ${group.name}:`
      : "Page name (for example Soaking or Mechanical):");
    if (!name?.trim()) return;
    const d = draft();
    const page = { id: newId(), name: name.trim().slice(0, 30), items: [] };
    if (group) page.page_group = group.id;
    d.pages.push(page);
    setBlocks(d, pageBlocks(d));
    pageSel = page.id;
    groupSel = null;
    markLayoutDirty();
    renderLayoutEditor();
  }

  function renderGroupEditor() {
    const box = $("#page-editor");
    const d = draft();
    const group = d.page_groups.find((g) => g.id === groupSel);
    const pages = d.pages.filter((p) => p.page_group === group.id);
    const others = d.pages.filter((p) => p.page_group !== group.id);
    const regroup = (page, groupId) => {
      if (groupId) page.page_group = groupId;
      else delete page.page_group;
      setBlocks(d, pageBlocks(d));
      markLayoutDirty();
      renderLayoutEditor();
    };
    const moveIn = others.length ? h("select", {
      "aria-label": "Move a page into this group",
      onchange: (e) => regroup(d.pages.find((p) => p.id === e.target.value), group.id),
    }, h("option", { value: "" }, "Move a page here\u2026"),
    ...others.map((p) => h("option", { value: p.id }, groupOf(p, d) ? `${p.name} (from ${groupOf(p, d).name})` : p.name))) : null;
    box.replaceChildren(
      h("div", { class: "editor-head" },
        h("label", { class: "role-name-field" }, "Group name",
          h("input", { value: group.name, maxlength: "30", oninput: (e) => {
            group.name = e.target.value;
            markLayoutDirty();
            const label = $("#page-list .group-item.active .role-name");
            if (label) label.textContent = group.name;
          } })),
        h("div", { class: "editor-actions" },
          h("button", { class: "btn", onclick: () => duplicateGroup(group) }, "Duplicate group"),
          h("button", { class: "btn danger", onclick: () => deleteGroup(group) }, "Delete group"))),
      h("p", { class: "hint" }, "Give a group to phones and computers to show all of its pages. Pages you add " +
        "to the group later show up on them too, and each device still only sees what its role allows."),
      h("div", { class: "shown-on" },
        h("span", null, "Shown on: ", h("strong", null, shownOnText(`group:${group.id}`))),
        h("button", { class: "btn", onclick: () => choosePhones(`group:${group.id}`, group.name) }, "Choose devices")),
      h("div", { class: "add-row" }, "Add:",
        h("button", { class: "btn", onclick: () => addPage(group) }, "New page"), moveIn),
      pages.length
        ? h("div", { class: "item-list" }, pages.map((page) => h("div", { class: "item-row" },
          h("span", { class: "item-kind" }, "Page"),
          h("div", { class: "entity-text" },
            h("div", { class: "entity-name" }, page.name),
            h("div", { class: "entity-id" }, `${page.items.length} item${page.items.length === 1 ? "" : "s"}`)),
          h("div", { class: "area-actions" },
            h("button", { class: "btn", onclick: () => { pageSel = page.id; groupSel = null; renderLayoutEditor(); } }, "Edit"),
            h("button", { class: "btn", "aria-label": `Take ${page.name} out of the group`,
              onclick: () => regroup(page, null) }, "Take out")))))
        : h("div", { class: "empty" }, "No pages in this group yet. Add a new page, or move one here."));
  }

  // A copy of a whole group with copies of all its pages, right below it. The copy isn't on
  // any device yet (except devices that show every page), ready to be renamed and given out.
  function duplicateGroup(group) {
    const d = draft();
    if (d.page_groups.length >= MAX_PAGE_GROUPS) return toast(`A layout can have at most ${MAX_PAGE_GROUPS} groups.`, true);
    const originals = d.pages.filter((p) => p.page_group === group.id);
    if (!roomForPages(originals.length)) return;
    const copy = { id: newId(), name: `${group.name} (copy)`.slice(0, 30) };
    const pages = originals.map((p) => ({ ...copyPage(p), page_group: copy.id }));
    const blocks = pageBlocks(d);
    blocks.splice(blocks.findIndex((b) => b.group?.id === group.id) + 1, 0, { group: copy, pages });
    setBlocks(d, blocks);
    groupSel = copy.id;
    markLayoutDirty();
    renderLayoutEditor();
    toast(`Copied as ${copy.name} with ${pages.length} page${pages.length === 1 ? "" : "s"}. ` +
      "Rename it, choose its devices, then Save layout.");
  }

  async function deleteGroup(group) {
    const d = draft();
    const pages = d.pages.filter((p) => p.page_group === group.id);
    const choice = pages.length
      ? await askChoice(`Delete the group ${group.name}? It has ${pages.length} page${pages.length === 1 ? "" : "s"}: ` +
        `${pages.map((p) => p.name).join(", ")}.`, [
        ["keep", "Keep its pages", "They stay in the layout, outside any group. Devices that got the group keep them."],
        ["delete", "Delete its pages too", "They're removed from every device.", "danger"],
      ])
      : (await askConfirm(`Delete the group ${group.name}?`, "Delete group") ? "keep" : null);
    if (!choice) return;
    if (choice === "delete") d.pages = d.pages.filter((p) => p.page_group !== group.id);
    else for (const page of pages) delete page.page_group;
    d.page_groups = d.page_groups.filter((g) => g !== group);
    groupSel = null;
    markLayoutDirty();
    renderLayoutEditor();
  }

  // The page's items as reorderable rows.
  function itemList(page) {
    const list = h("div", { class: "item-list" }, page.items.map((item, i) => {
      const [kind, title, sub] = itemSummary(item);
      return h("div", { class: `item-row item-${item.type} sort-row` },
        dragHandle(`Reorder ${title}`),
        h("span", { class: "item-kind" }, kind),
        h("div", { class: "entity-text" },
          h("div", { class: "entity-name" }, title),
          sub ? h("div", { class: "entity-id" }, sub) : null,
          item.type !== "heading" && accessNote(item)
            ? h("div", { class: "via access-note" }, accessNote(item)) : null),
        h("div", { class: "area-actions" },
          h("button", { class: "btn", onclick: () => editItem(page, i, item.type) }, "Edit"),
          h("button", { class: "btn", "aria-label": `Duplicate ${title}`, onclick: () => duplicateItem(page, i) },
            "Duplicate"),
          h("button", { class: "btn danger", "aria-label": `Remove ${title}`, onclick: () => {
            page.items.splice(i, 1);
            markLayoutDirty();
            renderLayoutEditor();
          } }, "Remove")));
    }));
    sortable(list, "page-items", (from, to) => {
      moveItem(page.items, from, to);
      markLayoutDirty();
      renderLayoutEditor();
    });
    return list;
  }

  // Devices given a page (its ID) or a page group ("group:<id>"), or every page.
  function phonesShowing(key) {
    return state.devices.filter((d) => d.status !== "revoked" &&
      (d.layout_pages == null || d.layout_pages.includes(key)));
  }

  function shownOnText(key) {
    const all = state.devices.filter((d) => d.status !== "revoked");
    const on = phonesShowing(key);
    if (!all.length) return "no devices set up yet";
    if (on.length === all.length) return "all devices";
    if (!on.length) return "no devices";
    return on.map((d) => d.name).join(", ");
  }

  // Pick which devices show one page (key: its ID) or page group (key: "group:<id>"). Saved
  // straight away (it's a device setting, not part of the layout draft).
  function choosePhones(key, name) {
    const dialog = $("#item-dialog");
    const body = $("[data-body]", dialog);
    $("[data-error]", dialog).textContent = "";
    $("[data-title]", dialog).textContent = `Devices that show \u201c${name}\u201d`;
    const devices = state.devices.filter((d) => d.status !== "revoked");
    const boxes = devices.map((d) => ({
      d, input: h("input", { type: "checkbox", checked: d.layout_pages == null || d.layout_pages.includes(key) }),
    }));
    body.onclick = null;
    body.oninput = null;
    body.replaceChildren(devices.length
      ? h("div", { class: "item-fields" },
        h("p", { class: "hint" }, "Each phone or computer still only sees what its role allows on " +
          (key.startsWith("group:") ? "these pages. " : "this page. ") +
          "Unticking a device that shows all pages switches it to a fixed list of pages, so it " +
          "won't get new pages automatically. Changes here are saved straight away."),
        h("div", { class: "member-list" }, boxes.map(({ d, input }) => h("label", { class: "check pick-row" },
          input, h("span", null, d.name, h("small", null,
            ` \u00b7 ${d.role_name || "no role"} \u00b7 ${d.layout_pages == null ? "all pages" : "chosen pages"}`))))))
      : h("p", { class: "hint" }, "Set up a phone or computer on the Phones & computers tab first."));
    const form = $("#item-form");
    // Enter on a checkbox would otherwise "click" the first button, which is Cancel.
    form.onkeydown = (ev) => {
      if (ev.key === "Enter" && ev.target.tagName === "INPUT") ev.preventDefault();
    };
    form.onsubmit = (ev) => {
      if (ev.submitter?.value !== "save") return;
      // A device that showed all pages keeps every page and group that could still exist (saved
      // or in the unsaved draft) except this one, so discarding the draft later can't take
      // pages away from it. Pages and groups that no longer exist are dropped from lists.
      const known = knownChoices();
      const changes = [];
      for (const { d, input } of boxes) {
        const shows = d.layout_pages == null || d.layout_pages.includes(key);
        if (input.checked === shows) continue;
        const current = (d.layout_pages ?? allChoices()).filter((id) => known.has(id));
        const next = input.checked ? [...current, key] : current.filter((id) => id !== key);
        changes.push([d, next]);
      }
      if (!changes.length) return;
      run(async () => {
        const failed = [];
        for (const [d, next] of changes) {
          try {
            await api("PATCH", `api/devices/${d.id}`, { layout_pages: next });
          } catch (err) {
            failed.push(`${d.name} (${err.message})`);
          }
        }
        // Always refresh, so the screen matches what was actually saved.
        await load();
        renderLayoutEditor();
        if (failed.length) throw new Error(`Couldn't update ${failed.join(", ")}.`);
      }, "Devices updated.");
    };
    dialog.showModal();
  }

  // Searchable device list used by the item dialogs.
  function devicePicker(onPick, isPicked) {
    const search = h("input", { type: "search", placeholder: "Search devices to add" });
    const list = h("div", { class: "pick-box tall" });
    const draw = () => {
      const q = search.value.toLowerCase();
      const shown = (entities || []).filter((e) => !isPicked(e.entity_id) &&
        (!q || e.name.toLowerCase().includes(q) || e.entity_id.includes(q)));
      list.replaceChildren(...shown.slice(0, 200).map((e) => h("button", {
        type: "button", class: "pick-add", onclick: () => { onPick(e); draw(); },
      }, h("span", null, e.name, h("small", null, e.area ? ` \u00b7 ${e.area}` : "")), h("span", { class: "plus" }, "+"))));
      if (shown.length > 200) list.append(h("p", { class: "hint" }, `Showing 200 of ${shown.length}. Search to narrow it down.`));
      if (!shown.length) list.append(h("p", { class: "hint" }, "No devices match."));
    };
    search.addEventListener("input", draw);
    search.addEventListener("keydown", (ev) => { if (ev.key === "Enter") ev.preventDefault(); });
    draw();
    return { el: h("div", { class: "pick-block" }, search, list), redraw: draw };
  }

  // "Use role" (no override), or Hidden / View / Control for every phone showing the page.
  function accessPicker(item, canControl) {
    const choices = [["", "Use role"], ["none", "Hidden"], ["view", "View"], ["control", "Control"]];
    const wrap = h("div", { class: "seg seg-access", role: "radiogroup", "aria-label": "Access on phones" });
    const draw = () => wrap.replaceChildren(...choices.map(([value, text]) => {
      const allowed = value !== "control" || canControl();
      if (!allowed && item.access === "control") item.access = "view";
      return h("button", {
        type: "button", role: "radio", class: `seg-${value || "role"}`,
        "aria-checked": String((item.access || "") === value), disabled: !allowed,
        title: allowed ? null : "This kind of device can only be viewed",
        onclick: () => {
          if (value) item.access = value; else delete item.access;
          draw();
        },
      }, text);
    }));
    draw();
    return {
      el: h("div", { class: "access-field" },
        h("div", { class: "pick-head" }, "Access on phones"),
        wrap,
        h("p", { class: "hint" }, "Use role keeps whatever each phone\u2019s role allows. Hidden, View or " +
          "Control replaces that on every phone that shows this page, even phones whose role doesn\u2019t " +
          "include the device.")),
      redraw: draw,
    };
  }

  function collapsedToggle(item) {
    const box = h("input", { type: "checkbox", checked: !!item.collapsed });
    box.addEventListener("change", () => {
      if (box.checked) item.collapsed = true;
      else delete item.collapsed;
    });
    return h("label", { class: "check" }, box, "Start collapsed");
  }

  async function editItem(page, index, type) {
    if (!entities) await loadEntities();
    if (!entities) return;
    const existing = index == null ? null : page.items[index];
    const item = existing ? JSON.parse(JSON.stringify(existing)) : { id: newId(), type };
    const dialog = $("#item-dialog");
    const body = $("[data-body]", dialog);
    const error = $("[data-error]", dialog);
    error.textContent = "";
    body.oninput = () => { error.textContent = ""; };
    body.onclick = () => { error.textContent = ""; };
    const titles = { group: "group card", device: "device", area: "whole area", heading: "heading" };
    $("[data-title]", dialog).textContent = `${existing ? "Edit" : "Add"} ${titles[type]}`;
    let validate = () => null;

    if (type === "heading") {
      const input = h("input", { value: item.title || "", maxlength: "60", placeholder: "Tubs" });
      input.addEventListener("input", () => { item.title = input.value; });
      body.replaceChildren(h("div", { class: "item-fields" }, h("label", null, "Heading text", input),
        collapsedToggle(item),
        h("p", { class: "hint" }, "On phones and computers, tapping a heading folds away everything under it, " +
          "up to the next heading. Each one remembers what was folded; this only sets how it starts.")));
      validate = () => (item.title?.trim() ? null : "Enter the heading text.");
    } else if (type === "area") {
      const select = h("select", null, state.areas.map((a) =>
        h("option", { value: a.id, selected: a.id === item.area_id }, a.name)));
      const title = h("input", { value: item.title || "", maxlength: "60", placeholder: "Same as the area name" });
      item.area_id = item.area_id ?? state.areas[0]?.id;
      select.addEventListener("change", () => { item.area_id = Number(select.value); });
      title.addEventListener("input", () => { item.title = title.value; });
      body.replaceChildren(state.areas.length
        ? h("div", { class: "item-fields" }, h("label", null, "Area", select),
          h("label", null, "Heading on the phone (optional)", title),
          h("p", { class: "hint" }, "Shows every device in this area that the phone's role allows, " +
            "including devices added to the area later."),
          collapsedToggle(item),
          accessPicker(item, () => true).el,
          h("p", { class: "hint" }, "Control only applies to devices that can be operated; sensors stay View."))
        : h("p", { class: "hint" }, "Create areas in Settings \u2192 Areas first."));
      validate = () => (item.area_id != null ? null : "Create an area first.");
    } else if (type === "device") {
      const chosen = h("div", { class: "chosen-device" });
      const label = h("input", { value: item.label || "", maxlength: "40", placeholder: "Same as the device name" });
      label.addEventListener("input", () => { item.label = label.value; });
      const drawChosen = () => chosen.replaceChildren(item.entity_id
        ? h("div", null, h("strong", null, entityName(item.entity_id)), h("div", { class: "entity-id" }, item.entity_id))
        : h("p", { class: "hint" }, "Pick a device below."));
      const access = accessPicker(item, () => !item.entity_id || entityById(item.entity_id)?.controllable !== false);
      const picker = devicePicker((e) => { item.entity_id = e.entity_id; drawChosen(); access.redraw(); }, () => false);
      drawChosen();
      body.replaceChildren(h("div", { class: "item-fields" }, chosen,
        h("label", null, "Label (optional)", label), access.el, picker.el));
      validate = () => (item.entity_id ? null : "Pick a device.");
    } else {
      item.entities = item.entities || [];
      const title = h("input", { value: item.title || "", maxlength: "60", placeholder: "Tub 1" });
      title.addEventListener("input", () => { item.title = title.value; });
      const members = h("div", { class: "member-list" });
      let picker = null;
      const drawMembers = () => {
        const onCard = item.entities.filter((m) => m.summary).length;
        members.replaceChildren(...(item.entities.length ? item.entities.map((m, i) => {
          if (m.type === "divider") {
            const name = h("input", { value: m.title || "", maxlength: "40", placeholder: "Section name, such as Cold plunge",
              "aria-label": "Divider name", "data-divider": "" });
            name.addEventListener("input", () => { m.title = name.value; });
            return h("div", { class: "member-row divider-row sort-row" },
              dragHandle(`Reorder divider ${m.title || ""}`.trim()),
              h("span", { class: "divider-tag" }, "Divider"),
              h("div", { class: "member-main" }, name),
              h("div", { class: "area-actions" },
                h("button", { type: "button", class: "btn subtle", "aria-label": "Remove divider",
                  onclick: () => { item.entities.splice(i, 1); drawMembers(); } }, "\u2715")));
          }
          const label = h("input", { value: m.label || "", maxlength: "40", placeholder: entityName(m.entity_id),
            "aria-label": `Label for ${entityName(m.entity_id)}` });
          label.addEventListener("input", () => { m.label = label.value; });
          const summary = h("input", { type: "checkbox", checked: !!m.summary, disabled: !m.summary && onCard >= 4 });
          summary.addEventListener("change", () => { m.summary = summary.checked; drawMembers(); });
          const canControl = entityById(m.entity_id)?.controllable !== false;
          const own = h("select", { class: "member-access", "aria-label": `Access for ${entityName(m.entity_id)}` },
            [["", "Same as card"], ["none", "Hidden"], ["view", "View"], ["control", "Control"]]
              .filter(([value]) => value !== "control" || canControl)
              .map(([value, text]) => h("option", { value, selected: (m.access || "") === value }, text)));
          own.addEventListener("change", () => { if (own.value) m.access = own.value; else delete m.access; });
          return h("div", { class: "member-row sort-row" },
            dragHandle(`Reorder ${m.label || entityName(m.entity_id)}`),
            h("div", { class: "member-main" }, label,
              h("div", { class: "entity-id", title: m.entity_id }, `${entityName(m.entity_id)} \u00b7 ${m.entity_id}`)),
            h("label", { class: "check on-card" }, summary, "On card"),
            own,
            h("div", { class: "area-actions" },
              h("button", { type: "button", class: "btn subtle", "aria-label": "Remove",
                onclick: () => { item.entities.splice(i, 1); drawMembers(); picker.redraw(); } }, "\u2715")));
        }) : [h("p", { class: "hint" }, "No devices yet. Add them from the list below.")]));
      };
      sortable(members, "group-members", (from, to) => {
        moveItem(item.entities, from, to);
        drawMembers();
      });
      picker = devicePicker((e) => {
        item.entities.push({ entity_id: e.entity_id, label: "", summary: item.entities.filter((m) => m.summary).length < 2 });
        drawMembers();
      }, (id) => item.entities.some((m) => m.entity_id === id));
      drawMembers();
      body.replaceChildren(h("div", { class: "item-fields" },
        h("label", null, "Card title", title),
        accessPicker(item, () => true).el,
        h("div", { class: "pick-head" }, "In this group"),
        h("p", { class: "hint" }, "Give each device a short label like \u201cWater\u201d or \u201cSetpoint.\u201d " +
          "Tick up to 4 to show on the card; tapping the card shows all of them, plus controls where the phone has Control. " +
          "Each device can also have its own access, which wins over the card\u2019s."),
        members,
        h("div", { class: "divider-add" },
          h("button", { type: "button", class: "btn", onclick: () => {
            item.entities.push({ type: "divider", title: "" });
            drawMembers();
            [...members.querySelectorAll("[data-divider]")].pop()?.focus();
          } }, "+ Add divider"),
          h("span", { class: "hint" }, "Splits the card\u2019s screen on the phone into named sections. " +
            "Drag it by \u2261 to where the section starts.")),
        h("div", { class: "pick-head" }, "Add devices"),
        picker.el));
      validate = () => (!item.title?.trim() ? "Give the card a title."
        : !item.entities.some((m) => m.entity_id) ? "Add at least one device."
        : item.entities.some((m) => m.type === "divider" && !m.title?.trim())
          ? "Name every divider, or remove the empty one." : null);
    }

    const form = $("#item-form");
    // Enter in a text box would otherwise "click" the first button, which is Cancel.
    form.onkeydown = (ev) => {
      if (ev.key === "Enter" && ev.target.tagName === "INPUT") ev.preventDefault();
    };
    form.onsubmit = (ev) => {
      if (ev.submitter?.value !== "save") return;
      const problem = validate();
      if (problem) {
        ev.preventDefault();
        error.textContent = problem;
        return;
      }
      if (existing) page.items[index] = item;
      else page.items.push(item);
      markLayoutDirty();
      renderLayoutEditor();
    };
    dialog.showModal();
  }

  $("#add-page").addEventListener("click", () => addPage(null));

  $("#add-group").addEventListener("click", async () => {
    const d = draft();
    if (d.page_groups.length >= MAX_PAGE_GROUPS) return toast(`A layout can have at most ${MAX_PAGE_GROUPS} groups.`, true);
    const name = await askText("Group name (for example Pool staff or Front desk):");
    if (!name?.trim()) return;
    const group = { id: newId(), name: name.trim().slice(0, 30) };
    d.page_groups.push(group);
    groupSel = group.id;
    markLayoutDirty();
    renderLayoutEditor();
  });

  $("#show-unplaced").addEventListener("change", (e) => {
    draft().show_unplaced = e.target.checked;
    markLayoutDirty();
  });

  $("#save-layout").addEventListener("click", () => run(async () => {
    const d = draft();
    const { layout } = await api("PUT", "api/layout",
      { pages: d.pages, page_groups: d.page_groups, show_unplaced: d.show_unplaced !== false });
    state.layout = layout;
    layoutDraft = null;
    renderLayoutEditor();
  }, "Layout saved. Phones update within a few seconds."));

  $("#discard-layout").addEventListener("click", async () => {
    if (!await askConfirm("Discard your layout changes?", "Discard")) return;
    layoutDraft = null;
    renderLayoutEditor();
  });

  // ---------------------------------------------------------------- layout preview

  // The staff app with the layout being edited, as a phone or a computer shows it. It runs in a
  // frame on the dashboard's own address, asks this page for the draft (staffPortalPreview),
  // shows live readings, and never sends a command.
  const preview = { view: "phone", device: "" };
  const PREVIEW_SIZES = { phone: [390, 780], desktop: [1280, 800] };

  window.staffPortalPreview = () => {
    const d = draft();
    return {
      layout: { pages: d.pages, page_groups: d.page_groups, show_unplaced: d.show_unplaced !== false },
      device_id: preview.device ? Number(preview.device) : null,
    };
  };

  function openPreview() {
    const dialog = $("#preview-dialog");
    const select = $("#preview-device");
    const devices = state.devices.filter((d) => d.status !== "revoked");
    select.replaceChildren(h("option", { value: "" }, "Everything on the layout"),
      ...devices.map((d) => h("option", { value: d.id }, `${d.name} (${d.role_name || "no role"})`)));
    if (!devices.some((d) => String(d.id) === preview.device)) preview.device = "";
    select.value = preview.device;
    select.onchange = () => { preview.device = select.value; loadPreview(); };
    for (const btn of dialog.querySelectorAll("[data-view]")) {
      btn.onclick = () => { preview.view = btn.dataset.view; loadPreview(); };
    }
    dialog.showModal();
    loadPreview();
  }

  function loadPreview() {
    const dialog = $("#preview-dialog");
    for (const btn of dialog.querySelectorAll("[data-view]")) {
      btn.setAttribute("aria-checked", String(btn.dataset.view === preview.view));
    }
    const device = state.devices.find((d) => String(d.id) === preview.device);
    $("[data-preview-note]", dialog).textContent = (draft().dirty ? "Showing your unsaved changes. " : "") +
      (device ? `What ${device.name} shows, with its role and pages. `
        : "Everything on the layout, as a device allowed to control all of it would show it. ") +
      "Readings are live. Tapping a control doesn't change anything.";
    const query = new URLSearchParams({ view: preview.view });
    if (preview.device) query.set("device", preview.device);
    $("[data-frame]", dialog).src = `preview?${query}`;
    fitPreview();
  }

  // The computer view is a full-size screen scaled down to fit; the phone view is phone-sized.
  function fitPreview() {
    const dialog = $("#preview-dialog");
    const stage = $("[data-stage]", dialog);
    const frame = $("[data-frame]", dialog);
    const [width, height] = PREVIEW_SIZES[preview.view];
    const scale = Math.min(1, (stage.clientWidth - 24) / width, (window.innerHeight - 250) / height);
    frame.style.width = `${width}px`;
    frame.style.height = `${height}px`;
    frame.style.transform = `scale(${scale})`;
    const box = frame.parentElement;
    box.style.width = `${Math.round(width * scale)}px`;
    box.style.height = `${Math.round(height * scale)}px`;
    box.className = `preview-box ${preview.view}`;
  }

  $("#preview-layout").addEventListener("click", openPreview);
  $("#preview-dialog [data-close]").addEventListener("click", () => $("#preview-dialog").close());
  $("#preview-dialog").addEventListener("close", () => { $("#preview-dialog [data-frame]").src = "about:blank"; });
  window.addEventListener("resize", () => { if ($("#preview-dialog").open) fitPreview(); });

  // ---------------------------------------------------------------- backup and transfer

  const KIND_NAMES = { layout: "a layout", roles: "roles", staff: "staff members", backup: "a full backup" };

  // Pick between a few labelled options; resolves to the chosen key, or null on Cancel.
  function askChoice(message, choices) {
    return new Promise((resolve) => {
      const dialog = $("#choice-dialog");
      $("[data-message]", dialog).textContent = message;
      // The first choice gets the focus (so Enter picks it): list the safest one first.
      $("[data-choices]", dialog).replaceChildren(...choices.map(([value, label, sub, cls]) =>
        h("button", { class: cls ? `choice ${cls}` : "choice", value },
          h("strong", null, label), sub ? h("span", null, sub) : null)));
      dialog.returnValue = "";
      dialog.onclose = () => resolve(choices.some(([v]) => v === dialog.returnValue) ? dialog.returnValue : null);
      dialog.showModal();
      $(".choice", dialog)?.focus();
    });
  }

  // Downloads go through fetch so a refusal shows as a message instead of a broken file.
  async function downloadFile(path) {
    const res = await fetch(path, { cache: "no-store" });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `Download failed (${res.status}).`);
    const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || "staff-portal.json";
    const url = URL.createObjectURL(await res.blob());
    const a = h("a", { href: url, download: name });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // Ask for a file and read it as a Staff Portal file of the expected kind.
  function pickFile(kind) {
    return new Promise((resolve, reject) => {
      const input = $("#import-file");
      input.value = "";
      input.onchange = async () => {
        const file = input.files[0];
        if (!file) return resolve(null);
        try {
          if (file.size > 16 * 1024 * 1024) throw new Error("That file is too big to be a Staff Portal file.");
          let data;
          try { data = JSON.parse(await file.text()); } catch { throw new Error("That isn't a Staff Portal export or backup file."); }
          if (data?.format !== "staff-portal") throw new Error("That isn't a Staff Portal export or backup file.");
          if (data.kind !== kind) throw new Error(`That file holds ${KIND_NAMES[data.kind] || "something else"}, not ${KIND_NAMES[kind]}.`);
          resolve(data);
        } catch (err) {
          reject(err);
        }
      };
      input.click();
    });
  }

  const fullDate = (ts) => (ts ? new Date(ts * 1000).toLocaleString(undefined,
    { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }) : "an unknown date");

  document.querySelectorAll("[data-export]").forEach((btn) => btn.addEventListener("click", () =>
    run(() => downloadFile(`api/export/${btn.dataset.export}`))));
  $("#download-backup").addEventListener("click", () => run(() => downloadFile("api/backup"),
    "Backup downloaded. Keep it somewhere private."));

  document.querySelectorAll("[data-import]").forEach((btn) => btn.addEventListener("click", () => run(async () => {
    const kind = btn.dataset.import;
    const file = await pickFile(kind);
    if (!file) return false;
    return kind === "layout" ? importLayout(file) : importNamed(kind, file);
  })));

  // A layout goes into the Layout tab's draft, so it can be checked before saving.
  async function importLayout(file) {
    if (layoutDraft?.dirty && !await askConfirm("The Layout tab has unsaved changes. Replace them with the imported layout?", "Replace")) return false;
    const fileAreas = new Map((file.areas || []).map((a) => [a.id, String(a.name || "").toLowerCase()]));
    const ours = new Map(state.areas.map((a) => [a.name.toLowerCase(), a.id]));
    let dropped = 0;
    const pages = (file.layout?.pages || []).map((page) => ({
      ...page,
      items: (page.items || []).flatMap((item) => {
        if (item.type !== "area") return [item];
        const id = ours.get(fileAreas.get(item.area_id));
        if (id == null) { dropped += 1; return []; }
        return [{ ...item, area_id: id }];
      }),
    }));
    const groups = (Array.isArray(file.layout?.page_groups) ? file.layout.page_groups : [])
      .filter((g) => g && typeof g === "object" && typeof g.id === "string");
    layoutDraft = { pages, page_groups: groups, show_unplaced: file.layout?.show_unplaced !== false, dirty: false };
    pageSel = null;
    groupSel = null;
    selectTab("layout");
    markLayoutDirty();
    renderLayoutEditor();
    // Devices whose chosen pages and groups match nothing in the file won't show any of it.
    const ids = new Set([...pages.map((p) => p.id), ...groups.map((g) => `group:${g.id}`)]);
    const picky = state.devices.filter((d) => d.status !== "revoked" && d.layout_pages &&
      !d.layout_pages.some((id) => ids.has(id))).length;
    toast([`Imported ${pages.length} page${pages.length === 1 ? "" : "s"} into the editor. Check it, then click Save layout.`,
      dropped ? `${dropped} whole-area item${dropped === 1 ? "" : "s"} left out: no area here with the same name.` : "",
      picky ? `${picky} device${picky === 1 ? " is" : "s are"} set to specific pages; use Choose devices to add the new ones.` : ""]
      .filter(Boolean).join(" "));
    return false;
  }

  // Roles and staff are added to what's here; same names get the admin's choice.
  async function importNamed(kind, file) {
    const list = (kind === "roles" ? file.roles : file.staff) || [];
    const have = new Set((kind === "roles" ? state.roles : state.staff).map((x) => x.name.toLowerCase()));
    const clashes = list.filter((x) => have.has(String(x.name || "").trim().toLowerCase())).map((x) => x.name);
    const word = kind === "roles" ? ["role", "roles"] : ["staff member", "staff members"];
    let mode = "copy";
    if (clashes.length) {
      const shown = clashes.slice(0, 5).join(", ") + (clashes.length > 5 ? `, and ${clashes.length - 5} more` : "");
      mode = await askChoice(`${clashes.length} ${clashes.length === 1 ? word[0] : word[1]} in the file already ` +
        `exist${clashes.length === 1 ? "s" : ""} here: ${shown}. What should happen to ${clashes.length === 1 ? "it" : "them"}?`, [
        ["skip", "Skip them", `Keep yours and only add the ${word[1]} that are new.`],
        ["copy", "Keep both", "Add the file's version with “(imported)” after the name."],
        ["replace", "Replace them", kind === "roles"
          ? "Use the file's access settings. Phones keep their role."
          : "Use the file's PIN and settings. Anyone whose PIN changes is signed out, people you've " +
            "turned off stay off, and Home Assistant links are kept.", "danger"],
      ]);
      if (!mode) return false;
    }
    const result = await api("POST", `api/import/${kind}`, { file, mode });
    await load();
    // A role open in the editor that the import just replaced must show the imported access,
    // or saving it would quietly put the old access back.
    let reloaded = false;
    if (kind === "roles" && editing && mode === "replace" &&
        clashes.some((n) => String(n).trim().toLowerCase() === editing.name.trim().toLowerCase())) {
      editing.dirty = false;
      if (state.roles.some((r) => r.id === editing.id)) openRole(editing.id);
      reloaded = true;
    }
    const notes = [`${result.added} added`, result.replaced && `${result.replaced} replaced`,
      result.skipped && `${result.skipped} skipped`].filter(Boolean).join(", ");
    const extra = [
      result.missing_areas?.length && `No area here called ${result.missing_areas.join(", ")}; that access was left out.`,
      result.missing_phones?.length && `No phone here called ${result.missing_phones.join(", ")}.`,
      result.bad_pin && `${result.bad_pin} left out because their PIN in the file was damaged.`,
      result.kept_off?.length && `${result.kept_off.join(", ")} stayed turned off.`,
    ].filter(Boolean).join(" ");
    toast([`Imported ${word[1]}: ${notes}.`, extra, reloaded ? "The role open in the editor was reloaded." : ""]
      .filter(Boolean).join(" "));
    return false;
  }

  $("#restore-backup").addEventListener("click", () => run(async () => {
    const file = await pickFile("backup");
    return file ? restoreBackup(file) : false;
  }));

  async function restoreBackup(file) {
    const info = await api("POST", "api/restore/preview", { backup: file });
    const counts = [`${info.roles} role${info.roles === 1 ? "" : "s"}`, `${info.devices} phone${info.devices === 1 ? "" : "s"}`,
      `${info.staff} staff member${info.staff === 1 ? "" : "s"}`, `${info.pages} layout page${info.pages === 1 ? "" : "s"}`];
    // Phones this system didn't sign in (another system's backup, a revoked phone, an edited
    // file) come back needing setup.
    const setup = info.needs_setup || [];
    const setupNote = !setup.length ? ""
      : `\n\n${setup.length === 1 ? `${setup[0]} needs` : `${setup.length} phones and computers need`} ` +
        `to be set up again after this restore${setup.length === 1 ? "" : ` (${setup.slice(0, 8).join(", ")}` +
        `${setup.length > 8 ? ", \u2026" : ""})`}, because ${setup.length === 1 ? "it wasn't" : "they weren't"} ` +
        "signed in on this system.";
    const ok = await askConfirm(
      `Restore the backup of ${info.property_name || "Staff Access"} from ${fullDate(info.exported_at)}?\n\n` +
      `It has ${counts.join(", ")}. Everything in Staff Access now (settings, roles, layout, phones and staff) ` +
      "is replaced; the activity log stays. A copy of the current setup is saved first, so you can undo this." +
      setupNote,
      "Replace and restore");
    if (!ok) return false;
    await api("POST", "api/restore", { backup: file });
    editing = null;
    layoutDraft = null;
    await load();
    renderRoleEditor();
    loadSafetyBackups();
    toast("Backup restored. Your previous setup was saved first; to undo, restore it from Saved before restoring.");
    return false;
  }

  async function loadSafetyBackups() {
    const box = $("#safety-backups");
    let list = [];
    try { list = (await api("GET", "api/safety-backups")).backups; } catch { /* shown as empty */ }
    if (!list.length) { box.replaceChildren(); return; }
    box.replaceChildren(
      h("div", { class: "pick-head" }, "Saved before restoring"),
      h("p", { class: "hint" }, "Each restore first saves what it replaced. Restore one of these to undo. The last 5 are kept."),
      h("div", { class: "safety-list" }, list.map((b) => h("div", { class: "safety-row" },
        h("div", { class: "entity-text" },
          h("div", { class: "entity-name" }, `Before the restore on ${fullDate(b.saved_at)}`),
          h("div", { class: "entity-id" }, `${Math.max(1, Math.round(b.size / 1024))} KB`)),
        h("div", { class: "area-actions" },
          h("button", { class: "btn", onclick: () => run(() => downloadFile(`api/safety-backups/${b.name}`)) }, "Download"),
          h("button", { class: "btn", onclick: () => run(async () => {
            const res = await fetch(`api/safety-backups/${b.name}`, { cache: "no-store" });
            if (!res.ok) throw new Error("That saved copy couldn't be read.");
            return restoreBackup(await res.json());
          }) }, "Restore this"))))));
  }

  // ---------------------------------------------------------------- start

  $("#version").textContent = `v${SCRIPT_VERSION}`;

  load().then(() => {
    let tab = "phones";
    try { tab = sessionStorage.getItem("tab") || tab; } catch { /* ignore */ }
    selectTab(tab);
  }).catch((err) => toast(err.message, true));
})();
