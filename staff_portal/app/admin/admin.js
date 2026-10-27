(() => {
  "use strict";

  // All URLs are relative: Home Assistant serves this page under an ingress path.
  const state = { settings: {}, roles: [], devices: [], staff: [], areas: [], hasLogo: false, wristbands: {} };
  let entities = null;
  let haAreas = [];
  let editing = null; // Roles tab: { kind: "role"|"person", id, name, entities, areas, pages, wristbands, dirty, ... }
  let oldestActivity = null;
  let wbDoors = null;   // TT Hotel's doors as last synced (api/wristbands/doors), loaded when needed
  let wbDirty = false;  // Wristbands tab: settings changed but not saved yet
  let linkWatcher = null; // the open computer editor's { redraw, fast } for the encoder link check

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

  // Some card encoders also type each card's number, as a keyboard would, when a card is put on
  // them, often with Enter after it (Make key card has one in use). Nobody types that fast: once
  // enough keys have come that close together, they and the Enter or Tab after them are kept
  // from the page, and what the first few typed into a box is taken back, so a card can't fill
  // in a box, save a dialog or press a button. As the staff app does.
  const TYPED_GAP_MS = 50;     // keys this close together...
  const TYPED_KEYS = 5;        // ...this many in a row are the encoder (a card number has 8 or more)
  const TYPED_END_MS = 200;    // its Enter or Tab comes within this of its last key
  const typing = { last: -Infinity, count: 0, card: false, field: null, value: "" };
  window.addEventListener("keydown", (ev) => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.isComposing) return;
    const gap = ev.timeStamp - typing.last;
    if (ev.key.length === 1 && !ev.repeat) {   // a held-down key repeats quickly too: a person
      if (gap > TYPED_GAP_MS) {
        const field = /^(INPUT|TEXTAREA)$/.test(ev.target.tagName) ? ev.target : null;
        Object.assign(typing, { count: 0, card: false, field, value: field?.value ?? "" });
      }
      typing.last = ev.timeStamp;
      typing.count += 1;
      if (typing.count < TYPED_KEYS) return;
      if (!typing.card) {
        typing.card = true;
        const field = typing.field;
        if (field?.isConnected && field.value !== typing.value) {
          field.value = typing.value;
          field.dispatchEvent(new Event("input", { bubbles: true }));
        }
      }
    } else if ((ev.key === "Enter" || ev.key === "Tab") && typing.card && gap <= TYPED_END_MS) {
      typing.card = false;
      typing.count = 0;
    } else {
      typing.card = false;
      typing.count = 0;
      return;
    }
    ev.preventDefault();
    ev.stopImmediatePropagation();
  }, { capture: true });

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
    try {
      data = JSON.parse(text);
    } catch {
      // The web server's own "request too large" answer is technical; say it plainly.
      data = { error: res.status === 413 ? "That's too big to send to Staff Access."
        : text.trim().slice(0, 300) };
    }
    if (!res.ok) {
      // The whole answer goes with the error: some refusals say what to ask next (which encoder).
      const err = new Error(data.error || `Request failed (${res.status})`);
      err.status = res.status;
      err.data = data;
      throw err;
    }
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
    // Archived people are only on the Staff tab's archived list: never offered anywhere else.
    state.staff = data.staff.filter((m) => m.status !== "archived");
    state.archived = data.staff.filter((m) => m.status === "archived");
    state.keyCardTotals = data.key_card_totals || null;
    state.areas = data.areas;
    state.layout = data.layout;
    state.devices = data.devices;
    state.hasLogo = data.has_logo;
    state.tunnelSources = data.tunnel_sources;
    state.connecteam = data.connecteam;
    state.hrGoogle = data.hr_google || {};
    state.wristbands = data.wristbands || {};
    state.departments = data.departments || [];
    state.staffSync = data.staff_sync || {};
    state.notFresh = data.not_fresh || null;
    state.hr = data.hr || {};
    $("#add-from-connecteam").hidden = !data.connecteam.enabled;
    $("#top-title").textContent = `${state.settings.property_name} · Staff Access`;
    renderPhones();
    renderStaff();
    renderStaffSync();
    renderDepartments();
    renderAreaSettings();
    renderRoleList();
    refreshEditing();
    renderWristbandStatus();
    renderEncoderList();
    $("#keycards-section").hidden = !wristbandsOn();
    renderKeyCardTotals();
  }

  // ---------------------------------------------------------------- tabs

  function selectTab(name) {
    document.querySelectorAll("[role=tab]").forEach((b) =>
      b.setAttribute("aria-selected", String(b.dataset.tab === name)));
    document.querySelectorAll(".tab").forEach((s) => { s.hidden = s.id !== `tab-${name}`; });
    try {
      if (name === "activity") loadActivity(true);
      if (name === "staff") openStaffTab();
      if (name === "settings") fillSettings();
      if ((name === "roles" || name === "layout") && !entities) loadEntities();
      if (name === "roles" && wristbandsOn() && !wbDoors) loadDoors();
      if (name === "wristbands") openWristbands();
      if (name === "layout") renderLayoutEditor();
      if (name === "backup") loadSafetyBackups();
      if (name === "hr") openHr();
      if (name === "reports") openReports();
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
        if (!await askConfirm("Discard unsaved changes on the Roles tab?", "Discard")) return;
        editing = null;
        renderRoleEditor();
        renderRoleList();
      }
      if (wbDirty && b.dataset.tab !== "wristbands") {
        if (!await askConfirm("Discard unsaved wristband settings?", "Discard")) return;
        wbDirty = false; // the form is filled again from what's saved when the tab next opens
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
      } else if (d.signin_mode === "phone") {
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
              h("span", { class: "encoder-slot", "data-encoder-badge": d.id }, cardEncoderBadge(d)),
              d.signin_mode === "role"
                ? h("span", { class: "badge shared" }, d.pin_only ? "Role-based \u00b7 PIN only" : "Role-based")
                : d.signin_mode === "staff"
                  ? h("span", { class: "badge shared" }, d.pin_only ? "Shared \u00b7 PIN only" : "Shared \u00b7 staff sign in")
                  : d.has_pin ? h("span", { class: "badge" }, "PIN") : null,
              d.ha_user ? h("span", {
                class: d.ha_link_error ? "badge locked" : "badge linked",
                title: d.ha_link_error || "Actions show in Home Assistant as this user",
              }, d.ha_link_error ? `HA user ${d.ha_user}: link broken` : `HA user: ${d.ha_user}`) : null,
              h("span", null, `Last used ${ago(d.last_seen).toLowerCase()}`)),
            d.status !== "revoked" && d.signin_mode !== "role" && phonePagesText(d)
              ? h("div", { class: "meta" }, `Pages: ${phonePagesText(d)}`,
                d.custom_order ? ` \u00b7 rearranged on the ${word}` : "") : null,
            h("div", { "data-encoder-info": d.id }, cardEncoderInfo(d))),
          d.signin_mode === "role"
            ? h("span", { class: "inline-select" }, "Each person\u2019s own role and pages")
            : h("label", { class: "inline-select" }, "Role", roleSelect)),
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

  function fillRoleOptions(select, selected, none = "No role (sees nothing)") {
    select.replaceChildren(
      h("option", { value: "" }, none),
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
    return choiceNames(d.layout_pages) || "None";
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
    const encoderBox = $("[data-encoder]", dialog);
    const syncKind = () => {
      const word = kindSelect.value === "computer" ? "computer" : "phone";
      dialog.querySelectorAll("[data-word]").forEach((el) => { el.textContent = word; });
      form.elements["name"].placeholder = word === "computer" ? "Front desk computer" : "Maintenance phone";
      // A card encoder plugs into a computer. Offered once Wristbands is on (or one is already
      // set: This computer (USB) is every new computer's own default, so it doesn't count).
      encoderBox.hidden = word !== "computer" ||
        !(wristbandsOn() || (d && !["off", "local"].includes(d.encoder_mode)));
    };
    let pinOnlyTouched = false;   // ticked or unticked by hand: changing Type leaves it be
    let encoderTouched = false;   // chosen by hand: changing Type leaves it be
    kindSelect.onchange = () => {
      syncKind();
      // A new computer hides names by default (Hide names starts ticked); a phone shows them.
      if (!d && !pinOnlyTouched) form.elements["pin_only"].checked = kindSelect.value === "computer";
      // ...and its card encoder is the one plugged into it; a phone has none.
      if (!d && !encoderTouched) {
        encoderMode.value = kindSelect.value === "computer" ? "local" : "off";
        syncEncoder();
      }
    };
    syncKind();
    form.elements["name"].value = d ? d.name : "";
    fillRoleOptions(form.elements["role_id"], d ? d.role_id : state.roles[0]?.id);
    $("[data-pin-hint]", dialog).textContent = d
      ? (d.has_pin ? "Enter a new PIN to change it, or leave blank and tick below to remove it."
        : pinNote("Optional, 4 to 8 digits."))
      : pinNote("Optional, 4 to 8 digits. Staff enter it when opening the app.");
    // Kiosk: the device's own role and pages (shown to one person or team, or to whoever signs
    // in). Role-based: everyone signs in and gets their own role's (signin_mode "role").
    const accessMode = form.elements["access_mode"];
    accessMode.value = d?.signin_mode === "role" ? "role" : "kiosk";
    const mode = form.elements["signin_mode"];
    mode.value = d?.signin_mode === "staff" ? "staff" : "phone";
    form.elements["staff_all"].checked = d ? !!d.staff_all : true;
    // Existing devices keep their choice. A new computer starts PIN-only: a front desk screen
    // is in view of guests, so it shouldn't list everyone's names (untick to show them).
    form.elements["pin_only"].checked = d ? !!d.pin_only : kindSelect.value === "computer";
    form.elements["pin_only"].onchange = () => { pinOnlyTouched = true; };
    const pick = $("[data-staff-list]", dialog);
    pick.replaceChildren(...state.staff.map((m) => h("label", { class: "check" },
      h("input", { type: "checkbox", value: m.id, checked: !!d?.staff_ids.includes(m.id) }),
      m.status === "disabled" ? `${m.name} (inactive)` : m.name)));
    $("[data-no-staff]", dialog).hidden = state.staff.length > 0;
    orderResetRow(dialog, d, "devices");
    form.elements["encoder_url"].value = d?.encoder_url || "";
    form.elements["encoder_port"].value = d?.encoder_port || "";
    // Card encoder: This computer (USB) (reached through the staff app open on this computer; a
    // new computer's default), Off, Automatic (SN Web Socket found on the network, used only once
    // confirmed) or an address typed in (which always wins).
    const encoderMode = form.elements["encoder_mode"];
    encoderMode.value = d ? d.encoder_mode || "off" : kindSelect.value === "computer" ? "local" : "off";
    // Automatic: the encoder confirmed with Use this encoder. On a computer already added that's
    // saved straight away; a new one keeps it until Save.
    let autoIp = d?.encoder_auto_ip || "";
    const findResult = $("[data-encoder-find-result]", dialog);
    findResult.textContent = "";
    findResult.classList.remove("form-error");
    // The device as last loaded: a reload (or the link check) replaces the objects in the list.
    const current = () => (d && state.devices.find((x) => x.id === d.id)) || d;
    const useHelper = (helper) => {
      const name = form.elements["name"].value.trim() || d?.name || "this computer";
      return run(async () => {
        if (!await askUseHelper(name, helper)) return false;
        if (!d) {
          autoIp = helper.ip;
          renderAuto();
          return false;   // saved with the computer
        }
        await saveHelper(d, helper);
        d = current();
        autoIp = d.encoder_auto_ip || "";
        encoderMode.value = "auto";
        syncEncoder();
      }, `Saved: ${d?.name || name} uses the card encoder at ${helper.ip}.`);
    };
    const renderAuto = () => {
      const disc = state.wristbands?.discovery || {};
      const saved = !!d && current().encoder_mode === "auto" && autoIp === (current().encoder_auto_ip || "");
      $("[data-encoder-auto-status]", dialog).textContent = autoStatus(disc, autoIp, saved);
      fill($("[data-encoder-helpers]", dialog),
        helperRows(disc.helpers || [], autoIp, saved ? "In use" : "Chosen", useHelper));
    };
    const renderLocal = () => {
      const line = $("[data-encoder-local-status]", dialog);
      const now = current();
      const [text, look] = !now || now.encoder_mode !== "local"
        ? ["After saving, sign in on this computer and open Wristbands: the encoder connects then.", ""]
        : linkStatus(now, "this computer");
      line.textContent = text;
      line.className = `hint link-status ${look}`.trim();
    };
    const syncEncoder = () => {
      const chosen = encoderMode.value;
      $("[data-encoder-local]", dialog).hidden = chosen !== "local";
      $("[data-encoder-auto]", dialog).hidden = chosen !== "auto";
      $("[data-encoder-address]", dialog).hidden = chosen !== "address";
      $("[data-encoder-port]", dialog).hidden = chosen === "off";
      renderAuto();
      renderLocal();
    };
    encoderMode.onchange = () => {
      encoderTouched = true;
      syncEncoder();
    };
    syncEncoder();
    // While it's open, the link check (checkLinks) redraws what this computer's encoder is doing:
    // all of it when something changed, else just the link's words (its "last connected").
    linkWatcher = {
      redraw: (changed) => {
        if (encoderBox.hidden) return;
        if (changed) syncEncoder();
        else renderLocal();
      },
      fast: () => !encoderBox.hidden && encoderMode.value === "local" && current()?.encoder_mode === "local",
    };
    if (linkWatcher.fast()) scheduleLinkCheck(LINK_CHECK_EDITING_MS);
    const findButton = $("[data-encoder-find]", dialog);
    findButton.onclick = async () => {
      findButton.disabled = true;
      findResult.classList.remove("form-error");
      findResult.textContent = "Looking\u2026 this takes a few seconds.";
      try {
        const r = await findEncoders();
        findResult.textContent = foundText(r.discovery);
      } catch (err) {
        findResult.textContent = err.message;
        findResult.classList.add("form-error");
      } finally {
        findButton.disabled = false;
        renderAuto();
      }
    };
    const encoderPort = () => {
      const port = form.elements["encoder_port"].value.trim();
      return !port || /^auto(matic)?$/i.test(port) ? "auto" : port;
    };
    // Tests what's saved: the add-on connects to the encoder, reads its version and lets it go.
    // This computer (USB) goes through the staff app on that computer, so Wristbands must be open
    // there (the add-on says so if it isn't).
    const encoderResult = $("[data-encoder-result]", dialog);
    const showResult = (text, isError) => {
      encoderResult.textContent = text;
      encoderResult.classList.toggle("form-error", !!isError);
    };
    showResult("");
    const testButton = $("[data-encoder-test]", dialog);
    testButton.hidden = !d;
    testButton.onclick = async () => {
      const typed = form.elements["encoder_url"].value.trim().replace(/\/+$/, "");
      const chosen = encoderMode.value;
      if (chosen === "off") {
        showResult("Choose This computer (USB) or Automatic, or enter an address first.", true);
        return;
      }
      if (chosen !== d.encoder_mode || encoderPort() !== (d.encoder_port || "auto") ||
          (chosen === "address" && typed !== d.encoder_url) ||
          (chosen === "auto" && autoIp !== (d.encoder_auto_ip || ""))) {
        showResult("Save first: Test encoder uses the saved card encoder and port.", true);
        return;
      }
      testButton.disabled = true;
      showResult("Testing…");
      try {
        const r = await api("POST", `api/devices/${d.id}/encoder-test`);
        showResult(`The encoder answered${r.version ? ` (version ${String(r.version).slice(0, 60)})` : ""}.`);
      } catch (err) {
        showResult(err.message, true);
      } finally {
        testButton.disabled = false;
        load().catch(() => { /* the next reload shows the encoder's status */ });
      }
    };
    const pageOptions = layoutPageOptions();
    const chosenPages = d?.layout_pages ?? null;
    form.elements["pages_all"].checked = chosenPages === null;
    const pagesList = $("[data-pages-list]", dialog);
    pagesList.replaceChildren(...pageOptions.map((o) => h("label", { class: "check" },
      h("input", { type: "checkbox", value: o.id, checked: chosenPages === null || chosenPages.includes(o.id) }),
      o.name)));
    const syncMode = () => {
      const roleBased = accessMode.value === "role";
      const shared = roleBased || mode.value === "staff";
      dialog.querySelectorAll("[data-kiosk-only]").forEach((el) => { el.hidden = roleBased; });
      $("[data-role-hint]", dialog).hidden = !roleBased;
      form.elements["pin"].closest("label").hidden = shared;
      $("#clear-pin")?.closest("label").toggleAttribute("hidden", shared);
      $("[data-staff-pick]", dialog).hidden = !shared;
      pick.hidden = form.elements["staff_all"].checked;
      $("[data-pages-pick]", dialog).hidden = roleBased || !pageOptions.length;
      $("[data-pages-none]", dialog).hidden = roleBased || pageOptions.length > 0;
      pagesList.hidden = form.elements["pages_all"].checked;
      // Every card needs a named maker, so only computers people sign in to make wristbands.
      $("[data-encoder-hint]", dialog).textContent = shared
        ? "People who sign in here can make the wristbands their role allows (Roles tab). Each card is " +
          "recorded under their name."
        : "To make wristbands, people must sign in here: set Mode to Role-based, or Who uses it to Shared.";
    };
    accessMode.onchange = syncMode;
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
      linkWatcher = null;
      if (dialog.returnValue !== "save") return;
      const roleBased = accessMode.value === "role";
      const shared = roleBased || mode.value === "staff";
      const body = { name: form.elements["name"].value, kind: kindSelect.value };
      // A role-based device keeps its kiosk role and pages for if it's switched back.
      if (!roleBased) body.role_id = form.elements["role_id"].value || null;
      body.signin_mode = roleBased ? "role" : mode.value;
      if (shared) body.pin_only = form.elements["pin_only"].checked;
      if (pageOptions.length && !roleBased) {
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
      if (!encoderBox.hidden) {
        body.encoder_mode = encoderMode.value;
        if (encoderMode.value === "address") body.encoder_url = form.elements["encoder_url"].value.trim();
        if (encoderMode.value === "auto") body.encoder_auto_ip = autoIp || null;
        body.encoder_port = encoderPort();
      } else if (d && d.encoder_mode !== "off" && kindSelect.value !== "computer") {
        body.encoder_mode = "off"; // a phone has no card encoder
      }
      if (shared) {
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
    return state.devices.filter((d) => d.signin_mode !== "phone" && d.status !== "revoked" &&
      (d.staff_all || d.staff_ids.includes(m.id))).map((d) => d.name);
  }

  // A person's own changes to their role, in a few words ("" if none).
  function ownChangesText(m) {
    const parts = [];
    const devices = Object.keys(m.entities || {}).length;
    const areas = Object.keys(m.areas || {}).length;
    if (areas) parts.push(`${areas} area${areas === 1 ? "" : "s"}`);
    if (devices) parts.push(`${devices} device${devices === 1 ? "" : "s"}`);
    if (m.layout_pages) parts.push("own pages");
    if (m.wristbands != null && wristbandsOn()) parts.push("own wristband rules");
    return parts.join(", ");
  }

  // Staff tab: one thin row per person, opened for everything about them. Sorted by department
  // (each one a group that folds away), name or role, and searchable. The sort and the folded
  // departments are remembered in this browser; who's opened and the search aren't.
  const STAFF_VIEW_KEY = "staff-portal.staff-view";
  const STAFF_SORTS = ["department", "name", "role"];
  const staffView = { q: "", sort: "department", folded: new Set(), open: new Set(), archived: false };
  try {
    const saved = JSON.parse(localStorage.getItem(STAFF_VIEW_KEY) || "null");
    if (STAFF_SORTS.includes(saved?.sort)) staffView.sort = saved.sort;
    if (Array.isArray(saved?.folded)) staffView.folded = new Set(saved.folded.map(String));
  } catch { /* nothing remembered, or storage is off: the defaults */ }

  function saveStaffView() {
    try {
      localStorage.setItem(STAFF_VIEW_KEY,
        JSON.stringify({ sort: staffView.sort, folded: [...staffView.folded] }));
    } catch { /* not remembered this time */ }
  }

  const roleName = (m) => state.roles.find((r) => r.id === m.role_id)?.name || "";
  const departmentOf = (m) => state.departments.find((d) => d.id === m.department_id) || null;
  const byName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" });

  function renderStaff() {
    renderStaffList();
    renderArchived();
  }

  function renderStaffList() {
    const list = $("#staff-list");
    $("#staff-tools").hidden = !state.staff.length;
    if (!state.staff.length) {
      list.replaceChildren(h("div", { class: "empty" },
        "No staff yet. Add the people who share phones, each with their own PIN. Then set a ",
        "phone to \u201cShared\u201d (Phones \u2192 Edit) so they can sign in on it."));
      return;
    }
    // By department only once there are departments (it's by name until then).
    const departments = state.departments.length > 0;
    const sort = staffView.sort === "department" && !departments ? "name" : staffView.sort;
    document.querySelectorAll("#staff-sort button").forEach((b) => {
      b.hidden = b.dataset.sort === "department" && !departments;
      b.setAttribute("aria-checked", String(b.dataset.sort === sort));
    });
    const q = staffView.q.trim().toLowerCase();
    const shown = state.staff.filter((m) => !q || [m.name, roleName(m), departmentOf(m)?.name,
      m.ha_user].some((text) => text && text.toLowerCase().includes(q)));
    $("#staff-count").textContent = q ? `${shown.length} of ${state.staff.length}`
      : `${state.staff.length} ${state.staff.length === 1 ? "person" : "people"}`;
    if (!shown.length) {
      list.replaceChildren(h("div", { class: "empty" }, `No one matches \u201c${staffView.q.trim()}\u201d.`));
      return;
    }
    if (sort === "department") {
      const groups = state.departments.map((d) => [String(d.id), d.name,
        shown.filter((m) => m.department_id === d.id)]);
      groups.push(["none", "No department", shown.filter((m) => !departmentOf(m))]);
      list.replaceChildren(...groups.filter(([, , people]) => people.length)
        .map(([key, title, people]) => staffGroup(key, title, people.sort(byName), !!q)));
      return;
    }
    // By role: people with a role first, by role, then by name.
    const order = sort === "role"
      ? (a, b) => (!roleName(a) - !roleName(b)) || roleName(a).localeCompare(roleName(b)) || byName(a, b)
      : byName;
    list.replaceChildren(...shown.sort(order).map((m) => staffRow(m, sort)));
  }

  // A department and its people. A search shows every match, folded or not.
  function staffGroup(key, title, people, searching) {
    const folded = staffView.folded.has(key) && !searching;
    const body = h("div", { class: "staff-group-body" }, folded ? [] : people.map((m) => staffRow(m, "department")));
    const head = h("button", { type: "button", class: "staff-group-head", "aria-expanded": String(!folded),
      onclick: () => {
        if (staffView.folded.has(key)) staffView.folded.delete(key);
        else staffView.folded.add(key);
        saveStaffView();
        const fold = staffView.folded.has(key) && !searching;
        head.setAttribute("aria-expanded", String(!fold));
        body.replaceChildren(...(fold ? [] : people.map((m) => staffRow(m, "department"))));
      } },
    h("span", { class: "chev", "aria-hidden": "true" }), h("span", { class: "staff-group-name" }, title),
    h("span", { class: "count" }, String(people.length)));
    return h("section", { class: "staff-group" }, head, body);
  }

  // One person: a thin row (name, role, department, anything that needs a look, last sign-in),
  // opened for everything else and what can be done.
  function staffRow(m, sort) {
    const item = h("article", { class: `staff-row ${m.status === "disabled" ? "revoked" : ""}` });
    const draw = () => {
      const open = staffView.open.has(m.id);
      item.classList.toggle("open", open);
      const sub = [roleName(m) || "No role", sort !== "department" ? departmentOf(m)?.name : null];
      const flags = [
        m.status === "disabled" ? ["Inactive", "revoked"] : null,
        m.locked ? ["Locked (wrong PINs)", "locked"] : null,
        m.clock === "out" && m.clock_in ? ["Not clocked in", "pending"] : null,
        m.ha_link_error ? ["HA link broken", "locked"] : null,
      ].filter(Boolean);
      const head = h("button", { type: "button", class: "staff-row-head", "aria-expanded": String(open),
        onclick: () => {
          if (staffView.open.has(m.id)) staffView.open.delete(m.id);
          else staffView.open.add(m.id);
          draw();
          item.querySelector(".staff-row-head").focus({ preventScroll: true });
        } },
      h("span", { class: "chev", "aria-hidden": "true" }),
      h("span", { class: "staff-row-main" }, h("span", { class: "staff-row-name" }, m.name),
        h("span", { class: "staff-row-sub" }, sub.filter(Boolean).join(" \u00b7 "))),
      h("span", { class: "staff-row-side" },
        flags.map(([text, cls]) => h("span", { class: `badge ${cls}` }, text)),
        h("span", { class: "staff-row-seen", title: "Last signed in" }, ago(m.last_seen))));
      item.replaceChildren(head, open ? h("div", { class: "staff-row-body" }, staffDetails(m)) : "");
    };
    draw();
    return item;
  }

  // Everything about one person, and what can be done (an opened row).
  function staffDetails(m) {
    const status = m.status === "disabled" ? ["Inactive", "revoked"]
      : m.locked ? ["Locked for now (wrong PINs)", "locked"] : ["Active", "active"];
    const phones = phonesFor(m);
    const roleSelect = h("select", { "aria-label": `Role for ${m.name}` });
    fillRoleOptions(roleSelect, m.role_id, "No role");
    roleSelect.addEventListener("change", () => run(async () => {
      await api("PATCH", `api/staff/${m.id}`, { role_id: roleSelect.value || null });
      await load();
    }, `${m.name}'s role changed. It applies on role-based devices within a few seconds.`));
    const own = ownChangesText(m);
    const dept = departmentOf(m);
    const actions = [h("button", { class: "btn", onclick: () => editStaff(m) }, "Edit"),
      h("button", { class: "btn", onclick: () => openPersonAccess(m.id) }, "Access")];
    if (wristbandsOn()) {
      actions.splice(1, 0, h("button", { class: "btn", onclick: () => run(() => makeKeyCard(m)) }, "Make key card"));
    }
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
    actions.push(h("button", { class: "btn danger", onclick: () => run(() => archiveStaff(m)) }, "Archive"));
    return [
      h("div", { class: "phone-head" },
        h("div", null,
          h("div", { class: "meta" },
            h("span", { class: `badge ${status[1]}` }, status[0]),
            m.ha_user ? h("span", {
              class: m.ha_link_error ? "badge locked" : "badge linked",
              title: m.ha_link_error || "Actions show in Home Assistant as this user",
            }, m.ha_link_error ? `HA user ${m.ha_user}: link broken` : `HA user: ${m.ha_user}`) : null,
            m.connecteam_auto ? h("span", { class: "badge linked",
              title: "Added by Add everyone from Connecteam, which keeps their department up to date" },
            "From Connecteam") : null,
            clockBadge(m),
            h("span", null, `Last signed in ${ago(m.last_seen).toLowerCase()}`)),
          m.connecteam_note && m.status === "disabled" ? h("div", { class: "meta wb-note" },
            `Turned off: ${m.connecteam_note}.`, m.connecteam_note.startsWith("No usable kiosk code")
              ? " Give them a PIN with Edit, and tick Can sign in." : "") : null,
          h("div", { class: "meta" }, phones.length
            ? `Can sign in on: ${phones.join(", ")}`
            : "Not on any shared phone yet"),
          own ? h("div", { class: "meta" }, `Own changes to their role: ${own}`) : null,
          wristbandsOn() ? keyCardLine(m, dept) : null,
          clockRule(m)),
        h("div", { class: "staff-selects" },
          h("label", { class: "inline-select" }, "Role", roleSelect),
          departmentSelect(m))),
      h("div", { class: "card-actions" }, actions),
    ];
  }

  document.querySelectorAll("#staff-sort button").forEach((b) => b.addEventListener("click", () => {
    staffView.sort = b.dataset.sort;
    saveStaffView();
    renderStaff();
  }));
  $("#staff-search").addEventListener("input", (e) => {
    staffView.q = e.target.value.slice(0, 60);
    renderStaff();
  });
  $("#staff-search").addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });

  // ---- archived people

  // Archived people are kept, with everything they did, but have no access anywhere. They're
  // left out of the list above and folded away under it (Show archived), each with Restore and
  // Delete for good. A search shows the archived people it matches, folded or not.
  function renderArchived() {
    const box = $("#staff-archived");
    const q = staffView.q.trim().toLowerCase();
    const people = state.archived.filter((m) => !q || [m.name, roleName(m), departmentOf(m)?.name, m.ha_user]
      .some((text) => text && text.toLowerCase().includes(q))).sort(byName);
    if (!people.length) {
      box.replaceChildren();
      return;
    }
    const open = staffView.archived || !!q;
    const head = h("button", { type: "button", class: "staff-group-head", "aria-expanded": String(open),
      onclick: () => {
        staffView.archived = !staffView.archived;
        renderArchived();
        $("#staff-archived .staff-group-head")?.focus({ preventScroll: true });
      } },
    h("span", { class: "chev", "aria-hidden": "true" }),
    h("span", { class: "staff-group-name" }, `${open ? "Archived" : "Show archived"} (${people.length})`));
    box.replaceChildren(h("section", { class: "staff-group" }, head,
      open ? h("div", { class: "staff-group-body" }, people.map(archivedRow)) : null));
  }

  function archivedRow(m) {
    const cards = m.key_cards || 0;
    const sub = [roleName(m), departmentOf(m)?.name, m.archived_at
      ? `Archived ${when(m.archived_at)}${m.archived_by ? ` by ${m.archived_by}` : ""}` : "Archived"];
    return h("article", { class: "staff-row archived" },
      h("div", { class: "staff-archived-row" },
        h("span", { class: "staff-row-main" }, h("span", { class: "staff-row-name" }, m.name),
          h("span", { class: "staff-row-sub" }, sub.filter(Boolean).join(" \u00b7 "))),
        cards && wristbandsOn() ? h("button", { type: "button", class: "linkish kc-warn",
          onclick: () => showKeyCardsFor(m) },
        `${plural(cards, "key card")} still open${cards === 1 ? "s" : ""} doors`) : null,
        h("span", { class: "card-actions" },
          h("button", { class: "btn", onclick: () => run(() => restoreStaff(m)) }, "Restore"),
          h("button", { class: "btn danger", onclick: () => run(() => deleteForGood(m)) }, "Delete for good"))));
  }

  // Kiosk-mode phones and computers have their own token and PIN, and belong to no one in
  // particular, so archiving someone leaves them signed in. The ones that look like this person's
  // (named after them, or using their Home Assistant user) are offered for revoking.
  function kioskDevicesOf(m) {
    const words = [m.name, m.name.split(/\s+/)[0]].filter((w) => w && w.length > 1)
      .map((w) => new RegExp(`(^|[^\\p{L}\\p{N}])${w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}($|[^\\p{L}\\p{N}])`, "iu"));
    const kiosks = state.devices.filter((d) => d.signin_mode === "phone" && d.status !== "revoked");
    const theirs = kiosks.filter((d) => words.some((re) => re.test(d.name)) || (m.ha_user && d.ha_user === m.ha_user));
    return { theirs, others: kiosks.length - theirs.length };
  }

  function kioskRevokeRow(d) {
    const role = state.roles.find((r) => r.id === d.role_id)?.name;
    const done = h("span", { class: "hint", hidden: true }, "Revoked");
    const button = h("button", { type: "button", class: "btn danger", onclick: () => run(async () => {
      await api("POST", `api/devices/${d.id}/revoke`);
      d.status = "revoked";
      button.hidden = true;
      done.hidden = false;
    }, `${d.name} is revoked and signed out.`) }, "Revoke");
    return h("li", { class: "archive-kiosk-row" },
      h("span", null, d.name, h("small", null, ` \u00b7 ${d.kind === "computer" ? "computer" : "phone"}` +
        `${role ? ` \u00b7 ${role}` : ""}${d.ha_user ? ` \u00b7 HA user ${d.ha_user}` : ""}`)), button, done);
  }

  // Archive: their own access ends at once, and nothing is deleted. Their key cards keep opening
  // doors (locks check cards on their own), so the dialog lists the ones still valid and says
  // what to do; afterwards the Key cards list opens on them. Kiosk-mode devices that look like
  // theirs can be revoked from the dialog, and their Home Assistant user is theirs to turn off.
  async function archiveStaff(m) {
    const cards = wristbandsOn() || m.key_cards
      ? (await api("GET", `api/keycards?state=active&holder=${m.id}`)).cards || [] : [];
    const dialog = $("#archive-dialog");
    const phones = phonesFor(m);
    const kiosk = kioskDevicesOf(m);
    const remote = remoteInfo();
    $("[data-title]", dialog).textContent = `Archive ${m.name}?`;
    fill($("[data-body]", dialog),
      h("p", null, "Their own access ends straight away:"),
      h("ul", null, [
        `They're signed out, and their PIN stops working on shared and role-based phones and computers` +
          `${phones.length ? ` (${phones.join(", ")})` : ""}.`,
        "They can't make wristbands, contractor badges or key cards, and no key card can be made for them.",
        m.ha_user ? `Staff Access stops using their Home Assistant user (${m.ha_user}). It can still sign in to ` +
          "Home Assistant itself: turn it off there, under Settings > People." : null,
        m.connecteam_user_id ? "The Connecteam sync leaves them archived, even while they're active there." : null,
      ].filter(Boolean).map((text) => h("li", null, text))),
      kiosk.theirs.length ? h("div", { class: "archive-cards" },
        h("p", { class: "wb-warn" }, "Kiosk-mode phones and computers have their own PIN and aren't tied to " +
          "anyone, so archiving doesn't sign them out. If one of these is theirs, revoke it:"),
        h("ul", { class: "archive-kiosk" }, kiosk.theirs.map(kioskRevokeRow)),
        kiosk.others ? h("p", { class: "hint" }, `If another of the ${plural(kiosk.others, "Kiosk-mode device")} ` +
          "is theirs, revoke it under Phones & computers.") : null)
        : kiosk.others ? h("p", { class: "hint" }, "Kiosk-mode phones and computers have their own PIN and aren't " +
          "tied to anyone, so archiving doesn't sign them out: if one is theirs, revoke it under Phones & computers.") : null,
      h("p", { class: "hint" }, "Nothing is deleted: their activity and the cards they made stay. Restore them " +
        "any time from Show archived, under the list."),
      cards.length ? h("div", { class: "archive-cards" },
        h("p", { class: "wb-warn" }, (cards.length === 1 ? "Their key card still opens doors until it expires"
          : `Their ${cards.length} key cards still open doors until they expire`) +
          ": locks check cards on their own, so archiving can't switch them off."),
        h("ul", null, cards.map((c) => h("li", null,
          `No. ${c.card_number} \u00b7 ${c.summary} \u00b7 until ${c.expires_label || when(c.expires_at)}`))),
        h("p", { class: "hint" }, "Next, in the Key cards list: if you have the card, Revoke it. If not, " +
          "Report lost, then write a lost card notice and tap it on each door" +
          `${remote.enabled ? " (or lock it out remotely, on doors with a gateway)" : ""}. ` +
          "Until then, Door activity flags the card at any door.")) : null);
    const ok = await new Promise((resolve) => {
      dialog.returnValue = "";
      dialog.onclose = () => resolve(dialog.returnValue === "archive");
      dialog.showModal();
      $("button[value=cancel]", dialog).focus();
    });
    if (!ok) return false;
    const result = await api("POST", `api/staff/${m.id}/archive`);
    staffView.open.delete(m.id);
    await load();
    const left = result.cards?.length || 0;
    toast(left ? `${m.name} is archived. ${plural(left, "key card")} still open${left === 1 ? "s" : ""} doors: ` +
      "revoke or report lost below."
      : `${m.name} is archived.`);
    if (left && wristbandsOn()) showKeyCardsFor(m);
    return false;
  }

  async function restoreStaff(m) {
    if (!await askConfirm(`Restore ${m.name}? They're back as they were: their PIN, role, department, devices ` +
      "and own settings.", "Restore")) return false;
    const { status } = await api("POST", `api/staff/${m.id}/restore`);
    await load();
    toast(status === "disabled" ? `${m.name} is restored, still turned off as before. Turn them on with Edit.`
      : `${m.name} is restored and can sign in with their PIN again.`);
    return false;
  }

  async function deleteForGood(m) {
    // Their key cards still open doors: once they're deleted, Door activity can't tell whose
    // they are, so those are dealt with first.
    const cards = m.key_cards || 0;
    if (cards) {
      if (await askConfirm(`${m.name} still has ${plural(cards, "key card")} that open${cards === 1 ? "s" : ""} doors. ` +
        `Revoke ${cards === 1 ? "it" : "them"} or report ${cards === 1 ? "it" : "them"} lost first, or delete ` +
        `${m.name} once ${cards === 1 ? "it has" : "they have"} expired.`, wristbandsOn() ? "Show key cards" : "OK")
        && wristbandsOn()) showKeyCardsFor(m);
      return false;
    }
    const again = m.connecteam_auto && state.staffSync?.enabled
      ? " Add everyone from Connecteam adds them again if they're still active in Connecteam." : "";
    if (!await askConfirm(`Delete ${m.name} for good? They're removed from Staff Access and can't be restored. ` +
      `Their past activity and the cards made stay in the logs.${again}`, "Delete for good")) return false;
    await api("DELETE", `api/staff/${m.id}`);
    await load();
    toast(`${m.name} is deleted.`);
    return false;
  }

  // Connecteam link and, for clocked-in-only people, whether they're on the clock right now.
  function clockBadge(m) {
    if (!m.connecteam_user_id) return null;
    if (!m.clock) {
      return h("span", { class: "badge linked", title: m.clock_in
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

  // Whether this person must be clocked in on Connecteam to sign in, as a switch on their card,
  // and where that comes from (the server works it out: clock_in, clock_in_from): set by hand,
  // their department, or their own default. Changing it here (or in Edit) is the admin's choice:
  // their department and the Connecteam sync leave it alone, until "Use the department's setting".
  function clockRule(m) {
    if (!m.connecteam_user_id) return null;
    const required = !!m.clock_in;
    const dept = departmentOf(m);
    const box = h("input", { type: "checkbox", role: "switch", checked: required,
      "aria-label": `${m.name} must be clocked in to sign in` });
    box.addEventListener("change", () => run(async () => {
      try {
        await api("PATCH", `api/staff/${m.id}`, { require_clock_in: box.checked });
      } catch (err) {
        box.checked = required;
        throw err;
      }
      await load();
    }, box.checked ? `${m.name} must be clocked in on Connecteam to sign in.`
      : `${m.name} can sign in without clocking in.`));
    const off = !state.connecteam?.enabled;
    const from = {
      department: [`From ${dept?.name || "their department"}`,
        `Their department sets it for everyone in it. The switch sets it for ${m.name} alone.`],
      person: ["Set by hand", byHand(m)],
      default: m.connecteam_auto ? ["Default", "Follows \u201cPeople added this way must be clocked in\u201d " +
        "in Settings \u2192 Connecteam"] : null,
    }[m.clock_in_from];
    const back = m.clock_in_from === "person" && dept && dept.clock_in != null
      ? h("button", { type: "button", class: "linkish", onclick: () => run(async () => {
        await api("PATCH", `api/staff/${m.id}`, { clock_in_set: false });
        await load();
      }, `${m.name} now follows ${dept.name}: ${dept.clock_in ? "must be clocked in" : "no clock-in needed"}.`) },
      "Use the department's setting") : null;
    return h("div", { class: "meta clock-rule" },
      h("span", { class: `badge ${required ? "pending" : "active"}`,
        title: off ? "Connecteam is turned off in Settings, so the clock isn't checked for anyone" : null },
      required ? "Must be clocked in" : "Clock-in not required"),
      h("label", { class: "switch" }, box, h("span", { class: "switch-track", "aria-hidden": "true" }),
        h("span", null, "Must be clocked in to sign in")),
      from ? h("span", { class: "hint-inline", title: from[1] }, from[0]) : null,
      back);
  }

  const byHand = (m) => m.connecteam_auto
    ? "Set by hand, so their department and the Connecteam sync leave it as it is"
    : "Set by hand, so their department doesn't change it";

  // Their department, saved as soon as it's picked (shown once there are departments).
  function departmentSelect(m) {
    if (!state.departments.length && !m.department_id) return null;
    const select = h("select", { "aria-label": `Department for ${m.name}` });
    fillDepartmentOptions(select, m.department_id);
    select.addEventListener("change", () => run(async () => {
      await api("PATCH", `api/staff/${m.id}`, { department_id: select.value ? Number(select.value) : null });
      await load();
    }, m.connecteam_auto ? `${m.name}'s department changed. The Connecteam sync moves them back if their ` +
      "smart group says otherwise." : `${m.name}'s department changed.`));
    return h("label", { class: "inline-select" }, "Department", select);
  }

  function fillDepartmentOptions(select, selected) {
    select.replaceChildren(h("option", { value: "" }, "No department"),
      ...state.departments.map((d) => h("option", { value: d.id, selected: d.id === selected }, d.name)));
  }

  // Their key cards and the one Make key card would make, in a line.
  function keyCardLine(m, dept) {
    const n = m.key_cards || 0;
    const cards = n ? h("button", { type: "button", class: "linkish", onclick: () => showKeyCardsFor(m) },
      `${plural(n, "active key card")}`) : "No active key card";
    const template = !dept ? "no department, so no key card to make"
      : dept.card_access == null ? `${dept.name} has no key card set`
      : `Make key card: ${accessSummary(dept.card_access)}, ${plural(dept.card_days, "day")}`;
    return h("div", { class: "meta" }, cards, ` \u00b7 ${template}`);
  }

  // ctUser: a Connecteam user picked in "Add from Connecteam" ({id, name}).
  function editStaff(m, ctUser) {
    const dialog = $("#staff-dialog");
    const form = $("#staff-form");
    form.reset();
    $("[data-title]", dialog).textContent = m ? `Edit ${m.name}`
      : ctUser ? "Add from Connecteam" : "Add staff member";
    form.elements["name"].value = m ? m.name : ctUser ? ctUser.name.slice(0, 40) : "";
    fillRoleOptions(form.elements["role_id"], m ? m.role_id : null, "No role");
    fillDepartmentOptions(form.elements["department_id"], m ? m.department_id : null);
    $("[data-dept-row]", dialog).hidden = !state.departments.length && !m?.department_id;
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
    // The clock-in rule as it stands. Until it's touched, it follows the department picked here
    // (the server only sets it by hand when it differs from that).
    const clockBox = form.elements["require_clock_in"];
    const deptSelect = form.elements["department_id"];
    const clockFor = (deptId) => {
      const rule = state.departments.find((d) => d.id === deptId)?.clock_in;
      if (m?.clock_in_set || rule == null) {
        return m ? !!m.require_clock_in : state.settings.connecteam_auto_clock_in !== "0";
      }
      return !!rule;
    };
    let clockTouched = false;
    clockBox.checked = m ? !!m.clock_in : clockFor(null);
    clockBox.onchange = () => { clockTouched = true; };
    deptSelect.onchange = () => {
      if (!clockTouched) clockBox.checked = clockFor(deptSelect.value ? Number(deptSelect.value) : null);
    };
    const ctNote = $("[data-ct-note]", dialog);
    const clockNote = !m ? ""
      : m.clock_in_from === "department" ? `The clock-in rule comes from ${departmentOf(m)?.name || "their department"}. ` +
        `Changing it here sets it for ${m.name} alone.`
      : m.clock_in_from === "person" ? `The clock-in rule was set by hand, so ${m.connecteam_auto
        ? "their department and the sync leave it as it is" : "their department doesn't change it"}.`
      : m.connecteam_auto ? "Changing the clock-in rule here makes it yours: the sync never changes it back." : "";
    ctNote.textContent = [m?.connecteam_auto ? "Added by the Connecteam sync, which keeps their department up to date." : "",
      clockNote].filter(Boolean).join(" ");
    ctNote.hidden = !ctNote.textContent;
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
    // Someone the sync added stays linked while it runs (it would only add them again).
    unlink.hidden = !m || !!(m.connecteam_auto && state.staffSync?.enabled);
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
      const body = { name: form.elements["name"].value, role_id: form.elements["role_id"].value || null };
      const dept = form.elements["department_id"].value;
      if (!$("[data-dept-row]", dialog).hidden) body.department_id = dept ? Number(dept) : null;
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
        h("small", null, u.linked_archived ? `Archived as ${u.linked_to}: restore them on the Staff tab`
          : u.linked_to ? `Already added as ${u.linked_to}`
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

  // The Roles tab edits one thing at a time (editing.kind): a role, or one person's own changes
  // to what their role gives them on role-based devices. People are listed under their role.

  function roleCounts(r) {
    const count = Object.keys(r.entities).length;
    const areaCount = Object.keys(r.areas).length;
    const access = [`${count} device${count === 1 ? "" : "s"}`];
    if (areaCount) access.push(`${areaCount} area${areaCount === 1 ? "" : "s"}`);
    if (wristbandsOn() && r.wristbands?.types?.length) access.push("wristbands");
    const users = [];
    if (r.device_count) users.push(`${r.device_count} kiosk device${r.device_count === 1 ? "" : "s"}`);
    if (r.staff_count) users.push(`${r.staff_count} ${r.staff_count === 1 ? "person" : "people"}`);
    return [access.join(" · "), users.length ? `Used by ${users.join(" · ")}` : ""];
  }

  function renderRoleList() {
    const list = $("#role-list");
    const active = (kind, id) => editing?.kind === kind && editing.id === id;
    const person = (m) => h("li", null, h("button", {
      class: active("person", m.id) ? "role-item person-item active" : "role-item person-item",
      onclick: () => openPerson(m.id),
    }, h("span", { class: "role-name" }, m.name),
    h("span", { class: "role-meta" }, [ownChangesText(m) || (m.role_id ? "Same as role" : "No access yet"),
      m.status === "disabled" ? "inactive" : ""].filter(Boolean).join(" · "))));
    const blocks = state.roles.map((r) => {
      const [access, users] = roleCounts(r);
      const people = state.staff.filter((m) => m.role_id === r.id);
      return h("li", { class: "role-block" },
        h("button", { class: active("role", r.id) ? "role-item active" : "role-item", onclick: () => openRole(r.id) },
          h("span", { class: "role-name" }, r.name),
          h("span", { class: "role-meta" }, access),
          users ? h("span", { class: "role-meta" }, users) : null),
        people.length ? h("ul", { class: "role-people", "aria-label": `People with ${r.name}` }, people.map(person)) : null);
    });
    const loose = state.staff.filter((m) => !m.role_id);
    if (loose.length) {
      blocks.push(h("li", { class: "role-block" },
        h("div", { class: "role-people-head" }, "People without a role"),
        h("ul", { class: "role-people" }, loose.map(person))));
    }
    list.replaceChildren(...blocks);
    if (!blocks.length) list.replaceChildren(h("li", { class: "empty small" }, "No roles yet."));
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

  function roleEditing(id) {
    const role = state.roles.find((r) => r.id === id);
    // wristbands is a nested object, so it's copied in full; undefined (an older add-on) is
    // never sent, which leaves the saved value as it is.
    return role && { kind: "role", id, name: role.name, entities: { ...role.entities }, areas: { ...role.areas },
      pages: role.layout_pages ?? null, wristbands: clone(role.wristbands), dirty: false, filter: "", area: "",
      onlySelected: false };
  }

  function personEditing(id) {
    const m = state.staff.find((x) => x.id === id);
    return m && { kind: "person", id, name: m.name, role_id: m.role_id ?? null, entities: { ...m.entities },
      areas: { ...m.areas }, pages: m.layout_pages ?? null, wristbands: clone(m.wristbands), dirty: false,
      filter: "", area: "", onlySelected: false };
  }

  // Switching to something else in the editor: unsaved changes need a yes first.
  async function canLeaveEditor(kind, id) {
    if (!editing?.dirty || (editing.kind === kind && editing.id === id)) return true;
    return askConfirm("Discard unsaved changes?", "Discard");
  }

  async function openRole(id) {
    if (!await canLeaveEditor("role", id)) return;
    editing = roleEditing(id);
    renderRoleList();
    renderRoleEditor();
  }

  async function openPerson(id) {
    if (!await canLeaveEditor("person", id)) return;
    editing = personEditing(id);
    renderRoleList();
    renderRoleEditor();
  }

  // The Staff tab's Access button.
  function openPersonAccess(id) {
    selectTab("roles");
    openPerson(id);
  }

  // After a reload: an editor with no unsaved changes shows what's saved now (a role changed on
  // the Staff tab, say), so saving it later can't put old settings back.
  function refreshEditing() {
    if (!editing || editing.dirty) return;
    const { filter, area, onlySelected, devicesOpen } = editing;
    const fresh = editing.kind === "person" ? personEditing(editing.id) : roleEditing(editing.id);
    editing = fresh ? { ...fresh, filter, area, onlySelected, devicesOpen } : null;
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

  function markEditing() {
    editing.dirty = true;
    $("#save-role")?.removeAttribute("disabled");
  }

  // A role leaves a device out with "none"; a person's own setting goes back to their role's
  // with "role" (their "none" hides it from them).
  function setLevel(entityId, level) {
    if (level === (editing.kind === "person" ? "role" : "none")) delete editing.entities[entityId];
    else editing.entities[entityId] = level;
    markEditing();
  }

  const RANK = { none: 0, view: 1, control: 2 };
  const LABELS = { none: "Hidden", view: "View", control: "Control", role: "Role" };
  const ROLE_LEVELS = ["none", "view", "control"];
  const PERSON_LEVELS = ["role", "none", "view", "control"];

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

  // What a role gives one device: the higher of its own setting and its areas'.
  function roleGives(roleId, e) {
    const role = state.roles.find((r) => r.id === roleId);
    let level = role?.entities[e.entity_id] || "none";
    for (const id of e.area_ids || []) {
      const lv = role?.areas[id] || "none";
      if (RANK[lv] > RANK[level]) level = lv;
    }
    return level === "control" && !e.controllable ? "view" : level;
  }

  // What the person being edited gets for one device, and why: their own setting for it, else
  // their own setting for one of its areas (the higher), else their role's.
  function personGets(e) {
    const own = editing.entities[e.entity_id];
    const base = roleGives(editing.role_id, e);
    let fromArea = null;
    for (const id of e.area_ids || []) {
      const lv = editing.areas[id];
      if (lv && (!fromArea || RANK[lv] > RANK[fromArea.level])) {
        fromArea = { level: lv, area: state.areas.find((a) => a.id === id) };
      }
    }
    const cap = (lv) => (lv === "control" && !e.controllable ? "view" : lv);
    if (own) return { level: cap(own), why: `Their role gives ${LABELS[base]}` };
    if (fromArea) return { level: cap(fromArea.level), why: `${LABELS[cap(fromArea.level)]} from their ${fromArea.area?.name || "area"} setting` };
    return { level: base, why: editing.role_id ? `${LABELS[base]} from their role` : "Hidden: they have no role" };
  }

  // Equal-width buttons so the choices line up down the page. Options a device can't have
  // (Control on a sensor) are shown greyed out instead of being left out.
  function segmented(options, current, label, onPick, all = ROLE_LEVELS) {
    const wrap = h("div", { class: "seg", role: "radiogroup", "aria-label": label });
    for (const opt of all) {
      const allowed = options.includes(opt);
      const btn = h("button", {
        type: "button", role: "radio", "aria-checked": String(current === opt), class: `seg-${opt}`,
        disabled: !allowed, title: allowed ? (opt === "role" ? "Same as their role" : null) : "This kind of device can only be viewed",
      }, LABELS[opt]);
      btn.addEventListener("click", () => {
        wrap.querySelectorAll("button").forEach((b) => b.setAttribute("aria-checked", String(b === btn)));
        onPick(opt);
      });
      wrap.append(btn);
    }
    return wrap;
  }

  function levelControl(e, onPicked = () => {}) {
    if (editing.kind === "person") {
      const options = e.controllable ? PERSON_LEVELS : ["role", "none", "view"];
      return segmented(options, editing.entities[e.entity_id] || "role", `Access to ${e.name}`,
        (level) => { setLevel(e.entity_id, level); updateCount(); onPicked(); }, PERSON_LEVELS);
    }
    const options = e.controllable ? ROLE_LEVELS : ["none", "view"];
    return segmented(options, editing.entities[e.entity_id] || "none", `Access to ${e.name}`,
      (level) => {
        setLevel(e.entity_id, level);
        updateCount();
        onPicked();
      });
  }

  function updateCount() {
    const n = Object.keys(editing.entities).length;
    const el = $("#role-count");
    if (!el) return;
    el.textContent = editing.kind === "person"
      ? `${n} device${n === 1 ? "" : "s"} set just for them`
      : `${n} device${n === 1 ? "" : "s"} selected`;
  }

  function areaSection(redraw) {
    const person = editing.kind === "person";
    if (!state.areas.length) {
      return h("div", { class: "role-areas empty-areas" },
        h("h3", { class: "editor-sub" }, "Areas"),
        h("p", { class: "hint" }, "Create areas such as “Soaking Area” in ",
          h("button", { type: "button", class: "linkish", onclick: () => selectTab("settings") }, "Settings"),
          " to give access to a whole area at once."));
    }
    const role = person && state.roles.find((r) => r.id === editing.role_id);
    return h("div", { class: "role-areas" },
      h("h3", { class: "editor-sub" }, "Areas"),
      h("p", { class: "hint" }, person
        ? "Their setting for an area replaces what their role gives for every device in it, including " +
          "ones added later. A setting for a single device (below) beats it."
        : "Covers every device in the area, including ones added later. " +
          "A device gets whichever is higher: its area’s setting or its own setting below."),
      state.areas.map((a) => h("div", { class: "entity-row" },
        h("div", { class: "entity-text" },
          h("div", { class: "entity-name" }, a.name),
          h("div", { class: "entity-id" }, areaSummary(a)),
          person ? h("div", { class: "via" }, `Their role: ${LABELS[role?.areas[a.id] || "none"]}`) : null),
        segmented(person ? PERSON_LEVELS : ROLE_LEVELS, editing.areas[a.id] || (person ? "role" : "none"),
          `Access to ${a.name}`, (level) => {
            if (level === (person ? "role" : "none")) delete editing.areas[a.id];
            else editing.areas[a.id] = level;
            markEditing();
            redraw();
          }, person ? PERSON_LEVELS : ROLE_LEVELS))));
  }

  function areaSummary(a) {
    const names = a.ha_areas.map((id) => haAreas.find((x) => x.id === id)?.name || id);
    const parts = [];
    if (names.length) parts.push(`Home Assistant: ${names.join(", ")}`);
    if (a.entities.length) parts.push(`${a.entities.length} added device${a.entities.length === 1 ? "" : "s"}`);
    return parts.join(" · ") || "Empty";
  }

  // Pages for a role (null: every page) or a person (null: their role's). Ticking the box shows
  // the list, where page groups stand for all of their pages.
  function pagePicker(value, allLabel, onChange) {
    const options = layoutPageOptions();
    if (!options.length) {
      return h("p", { class: "hint" }, "No pages yet, so everyone gets the automatic layout. Build pages on the ",
        h("button", { type: "button", class: "linkish", onclick: () => selectTab("layout") }, "Layout"), " tab.");
    }
    const all = h("input", { type: "checkbox", checked: value === null });
    const boxes = options.map((o) => ({ o, input: h("input", { type: "checkbox", value: o.id,
      checked: value === null || value.includes(o.id) }) }));
    const list = h("div", { class: "staff-checks" }, boxes.map(({ o, input }) => h("label", { class: "check" }, input, o.name)));
    list.hidden = value === null;
    const emit = () => {
      if (all.checked) return onChange(null);
      // Keep choices this list doesn't show (More while it's switched off, or a page that's only
      // in the unsaved layout) so saving here doesn't quietly drop them.
      const offered = new Set(options.map((o) => o.id));
      const known = knownChoices();
      onChange([...boxes.filter((b) => b.input.checked).map((b) => b.o.id),
        ...(value || []).filter((id) => !offered.has(id) && known.has(id))]);
    };
    all.addEventListener("change", () => { list.hidden = all.checked; emit(); });
    list.addEventListener("change", emit);
    return h("div", { class: "staff-pick page-pick" }, h("label", { class: "check" }, all, allLabel), list);
  }

  // The devices list with its filters, for a role or a person.
  function deviceListSection() {
    const person = editing.kind === "person";
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
        const bulkLevels = person ? PERSON_LEVELS : ROLE_LEVELS;
        return h("div", { class: "area-group" },
          h("div", { class: "area-head" }, h("h4", null, area),
            h("div", { class: "bulk" }, "Set all:", bulkLevels.map((lv) =>
              h("button", { type: "button", class: "linkish", onclick: bulk(lv) }, LABELS[lv])))),
          list.map((e) => {
            let note = null;
            if (person) {
              note = h("div", { class: "via" });
              const refresh = () => { note.textContent = personGets(e).why; };
              refresh();
              return h("div", { class: "entity-row" },
                h("div", { class: "entity-text" },
                  h("div", { class: "entity-name" }, e.name),
                  h("div", { class: "entity-id", title: e.entity_id }, `${e.entity_id} · ${e.state}`),
                  note),
                levelControl(e, refresh));
            }
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
    const filters = h("div", { class: "filters" }, search, areaSel,
      h("label", { class: "check" }, only, person ? "Only their own settings" : "Only selected"),
      h("button", { type: "button", class: "linkish", onclick: () => { entities = null; drawList(); loadEntities(); } },
        "Reload devices"));
    return { filters, listBox, drawList };
  }

  // The Individual devices list folds away to its title and how many are set: folded each time a
  // role or person is opened (areas cover most devices), and kept as it is while that one is open.
  function deviceFold(filters, listBox) {
    const fold = h("details", { class: "fold-section device-fold", open: !!editing.devicesOpen },
      h("summary", null,
        h("span", { class: "chev", "aria-hidden": "true" }),
        h("h3", { class: "editor-sub" }, "Individual devices"),
        h("span", { class: "fold-count", id: "role-count" })),
      filters, listBox);
    fold.addEventListener("toggle", () => { if (editing) editing.devicesOpen = fold.open; });
    return fold;
  }

  function renderRoleEditor() {
    const box = $("#role-editor");
    if (!editing) {
      box.replaceChildren(h("div", { class: "empty" },
        "Pick a role, or create one like “Maintenance” or “Housekeeping.” Pick a person under a role to " +
        "change their access just for them."));
      return;
    }
    if (editing.kind === "person") {
      renderPersonEditor();
      return;
    }
    const nameInput = h("input", { value: editing.name, maxlength: "60", "aria-label": "Role name" });
    nameInput.addEventListener("input", () => {
      editing.name = nameInput.value;
      markEditing();
    });
    const { filters, listBox, drawList } = deviceListSection();
    const body = () => ({ name: editing.name, entities: editing.entities, areas: editing.areas,
      layout_pages: editing.pages, wristbands: editing.wristbands });

    const save = h("button", { class: "btn primary", id: "save-role", disabled: !editing.dirty }, "Save role");
    save.addEventListener("click", () => run(async () => {
      await api("PUT", `api/roles/${editing.id}`, body());
      editing.dirty = false;
      save.setAttribute("disabled", "");
      await load();
    }, "Role saved. Phones and people pick up the change within a few seconds."));
    const dup = h("button", { class: "btn" }, "Duplicate");
    dup.addEventListener("click", async () => {
      const name = await askText(editing.dirty
        ? "Name for the copy. It gets your unsaved changes; this role stays as it was last saved."
        : "Name for the copy. No phones or people move to it until you choose it for them.", "", `${editing.name} (copy)`);
      if (!name) return;
      run(async () => {
        const { id } = await api("POST", `api/roles/${editing.id}/duplicate`, { name });
        if (editing.dirty) await api("PUT", `api/roles/${id}`, { ...body(), name });
        editing.dirty = false;
        await load();
        openRole(id);
      }, `Created ${name}. Choose it for a device (Phones & computers → Edit) or a person (Staff).`);
    });
    const del = h("button", { class: "btn danger" }, "Delete role");
    del.addEventListener("click", async () => {
      const role = state.roles.find((r) => r.id === editing.id);
      const extra = [
        role?.device_count ? `${role.device_count} kiosk device${role.device_count === 1 ? "" : "s"} will lose all access.` : "",
        role?.staff_count ? `${role.staff_count} ${role.staff_count === 1 ? "person" : "people"} will have no role ` +
          "(on role-based devices they keep only their own settings)." : ""].filter(Boolean).join(" ");
      if (!await askConfirm(`Delete the ${editing.name} role?${extra ? ` ${extra}` : ""}`, "Delete role")) return;
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
      h("h3", { class: "editor-sub" }, "Pages"),
      h("p", { class: "hint" }, "What people with this role see when they sign in on a role-based device. " +
        "Kiosk devices choose their own pages (Phones & computers → Edit)."),
      pagePicker(editing.pages, "All pages, including ones you add later", (pages) => {
        editing.pages = pages;
        markEditing();
      }),
      ...wristbandSection(),
      areaSection(drawList),
      deviceFold(filters, listBox));
    updateCount();
    drawList();
  }

  function renderPersonEditor() {
    const box = $("#role-editor");
    const m = state.staff.find((x) => x.id === editing.id);
    if (!m) {
      editing = null;
      renderRoleEditor();
      return;
    }
    const role = state.roles.find((r) => r.id === editing.role_id);
    const roleSelect = h("select", { "aria-label": `Role for ${m.name}` });
    fillRoleOptions(roleSelect, editing.role_id, "No role");
    roleSelect.addEventListener("change", () => {
      editing.role_id = roleSelect.value ? Number(roleSelect.value) : null;
      markEditing();
      renderPersonEditor();
    });
    const { filters, listBox, drawList } = deviceListSection();
    const save = h("button", { class: "btn primary", id: "save-role", disabled: !editing.dirty }, "Save");
    save.addEventListener("click", () => run(async () => {
      await api("PATCH", `api/staff/${editing.id}`, { role_id: editing.role_id, layout_pages: editing.pages,
        entities: editing.entities, areas: editing.areas, wristbands: editing.wristbands });
      editing.dirty = false;
      await load();
    }, `Saved. ${m.name}'s access on role-based devices changes within a few seconds.`));
    const clear = h("button", { class: "btn" }, "Clear their settings");
    clear.addEventListener("click", () => {
      editing.entities = {};
      editing.areas = {};
      editing.pages = null;
      if (editing.wristbands !== undefined) editing.wristbands = null;
      markEditing();
      renderPersonEditor();
    });
    const rolePages = role?.layout_pages == null ? "all pages" : choiceNames(role.layout_pages) || "no pages";

    box.replaceChildren(
      h("div", { class: "editor-head" },
        h("div", { class: "role-name-field" }, "Person", h("div", { class: "person-name" }, m.name)),
        h("div", { class: "editor-actions" }, clear, save)),
      h("label", { class: "inline-select person-role" }, "Role", roleSelect),
      h("p", { class: "hint" }, `On role-based devices ${m.name} gets ` +
        (role ? `what the ${role.name} role allows` : "only what you set here, as they have no role") +
        ", with the settings below on top. Their own settings beat their role and items' access on the layout. " +
        "On kiosk devices the device’s role decides, not theirs."),
      h("h3", { class: "editor-sub" }, "Pages"),
      pagePicker(editing.pages, `Same as their role (${rolePages})`, (pages) => {
        editing.pages = pages;
        markEditing();
      }),
      ...wristbandSection(),
      areaSection(drawList),
      deviceFold(filters, listBox));
    updateCount();
    drawList();
  }

  // A page choice in words ("Tubs, Pool staff (group), More"; "" for none).
  function choiceNames(choice) {
    const layout = state.layout || {};
    const names = choice.map((id) => {
      if (id === "more") return "More";
      if (id.startsWith("group:")) {
        const group = (layout.page_groups || []).find((g) => `group:${g.id}` === id);
        return group && `${group.name} (group)`;
      }
      return (layout.pages || []).find((p) => p.id === id)?.name;
    }).filter(Boolean);
    return names.join(", ");
  }

  window.addEventListener("beforeunload", (e) => {
    if (editing?.dirty || layoutDraft?.dirty || wbDirty) e.preventDefault();
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
    fillConnections();
    refreshIcon();
  }

  // ------------------------------------------------ connections (in Settings)

  // TT Hotel and Connecteam, each folded to one line: whether it works, and its requests so far
  // against Staff Access's own limit. Opened, each has its own form and Save button, which sends
  // only that connection's settings.
  const connDirty = { tt: false, ct: false };   // changed but not saved yet
  // FormData leaves unticked boxes out, so each form's checkboxes are listed here.
  const CONN_CHECKBOXES = { tt: ["tthotel_clear_secret"], ct: ["connecteam_enabled",
    "connecteam_clear_secret", "connecteam_auto_staff", "connecteam_auto_clock_in"] };

  // Fills each connection's form with what's saved, unless it has unsaved changes. The secrets
  // are write-only: the dashboard only learns whether they're saved.
  function fillConnections() {
    if (!connDirty.tt) {
      fillFields($("#tt-form"), "tthotel_");
      renderWristbandSecrets();
    }
    if (!connDirty.ct) {
      const form = $("#ct-form");
      fillFields(form, "connecteam_");
      const saved = state.settings.connecteam_secret_set === "1";
      const secret = form.elements["connecteam_client_secret"];
      secret.value = "";
      secret.placeholder = saved ? "Saved — leave blank to keep" : "";
      form.elements["connecteam_clear_secret"].checked = false;
      $("#ct-clear-row").hidden = !saved;
    }
    renderConnections();
  }

  function fillFields(form, prefix) {
    for (const [k, v] of Object.entries(state.settings)) {
      const field = k.startsWith(prefix) ? form.elements[k] : null;
      if (field?.type === "checkbox") field.checked = v === "1";
      else if (field) field.value = v;
    }
  }

  // The closed lines, the TT Hotel line on the Wristbands tab, and each one's status when opened.
  function renderConnections() {
    const wb = state.wristbands || {};
    const ct = state.connecteam || {};
    const tt = ttState();
    showConnection($("#conn-tthotel"), tt, requestsText(wb.used_month, "this month", wb.monthly_cap));
    showConnection($("#conn-connecteam"), ctState(), requestsText(ct.used_today, "today", ct.daily_cap));
    // On the Wristbands tab, under its own on/off switch, an account that's ready just says so.
    showState($("#wb-tt-state"), !wb.enabled && ttSetUp() ? ["Details saved", ""] : tt);
    $("#wb-off-note").hidden = !!wb.enabled;
    renderTTStatus();
    renderConnecteam();
    showState($("#hr-ct-state"), hrCtState());
    renderGoogle();
  }

  function showConnection(box, connState, requests) {
    showState($("[data-conn-state]", box), connState);
    $("[data-conn-use]", box).textContent = requests;
  }

  function showState(badge, [text, kind]) {
    badge.textContent = text;
    badge.className = `badge ${kind}`.trim();
  }

  const requestsText = (n, when, cap) =>
    `${count(n)} request${n === 1 ? "" : "s"} ${when} (limit ${count(cap)})`;

  const ttSetUp = () => {
    const s = state.settings;
    return !!(s.tthotel_client_id && s.tthotel_client_secret_set === "1" && s.tthotel_username &&
      s.tthotel_password_md5_set === "1");
  };

  // Whether TT Hotel works, in a few words, with its badge color.
  function ttState() {
    const wb = state.wristbands || {};
    if (!ttSetUp()) return ["Not set up", ""];
    if (!wb.enabled) return ["Wristbands off", ""];
    return problemState(wb.halted, wb.paused && wb.paused_until, wb.last_error) ||
      (wb.synced_at ? ["Connected", "active"] : ["Not checked yet", "pending"]);
  }

  // The same for Connecteam. It counts as connected once a time clock check or a staff sync worked.
  function ctState() {
    const s = state.settings;
    const ct = state.connecteam || {};
    const sync = state.staffSync || {};
    if (!s.connecteam_client_id || s.connecteam_secret_set !== "1") return ["Not set up", ""];
    if (!ct.enabled) return ["Turned off", ""];
    return problemState(ct.halted, ct.paused && ct.paused_until, ct.last_error || (sync.enabled && sync.error)) ||
      (ct.checked_at || (sync.enabled && sync.synced_at) ? ["Connected", "active"] : ["Not checked yet", "pending"]);
  }

  // Stopped (details or access refused: it waits for the owner), paused (its own limits or the
  // service's), or the last request failed. Opened, the status says why.
  function problemState(halted, pausedUntil, error) {
    if (halted) return ["Stopped: access refused", "locked"];
    if (pausedUntil) return [`Paused until ${untilText(pausedUntil)}`, "pending"];
    if (error) return ["Last try failed", "locked"];
    return null;
  }

  // A time today, else a date.
  function untilText(ts) {
    const d = new Date(ts * 1000);
    return d.toDateString() === new Date().toDateString()
      ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
      : d.toLocaleDateString([], { month: "short", day: "numeric" });
  }

  function renderConnecteam() {
    const ct = state.connecteam || {};
    const lines = [];
    if (!ct.enabled) lines.push("Turned off.");
    else {
      lines.push(ct.halted ? `Stopped: ${ct.halted} ` + (ct.retry_at
        ? `It tries again by itself at ${untilText(ct.retry_at)}; or fix it, then save the settings or click Test connection.`
        : "Fix it, then save the settings or click Test connection.")
        : ct.paused ? `Paused: ${ct.paused}`
        : ct.last_error ? `Problem: ${ct.last_error}`
        : ct.checked_at ? `Connected. Checked ${ago(ct.checked_at).toLowerCase()}; ${ct.clocked_in} clocked in.`
        : "Not checked yet.");
      if (!ct.needed) lines.push("No one needs to be clocked in to sign in, so the time clock isn't being checked.");
      else lines.push(`Checking about every ${Math.max(1, Math.round(ct.interval / 60))} min.`);
      let use = `Requests today: ${ct.used_today} (Staff Access stops at ${ct.daily_cap})`;
      if (ct.account_limit) use += `; your account has ${ct.account_left} of ${ct.account_limit} left`;
      lines.push(use + ".");
      const hrLane = ct.hr || {};
      lines.push(`HR portal: ${hrLane.used_today || 0} of them today (it stops at ${hrLane.daily_cap})` +
        (hrLane.paused ? `. Waiting: ${hrLane.paused}` : state.hr?.status === "on" ? "." : "; it's off."));
    }
    $("#ct-status").replaceChildren(...lines.flatMap((t, i) => (i ? [h("br"), t] : [t])));
    renderStaffSync();
  }

  for (const [key, form] of [["tt", $("#tt-form")], ["ct", $("#ct-form")]]) {
    form.addEventListener("input", () => { connDirty[key] = true; });
    form.addEventListener("change", () => { connDirty[key] = true; });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const body = Object.fromEntries(new FormData(form).entries());
      for (const name of CONN_CHECKBOXES[key]) body[name] = form.elements[name].checked ? "1" : "0";
      run(async () => {
        await api("PUT", "api/settings", body);
        connDirty[key] = false;
        await load();
        fillConnections();
      }, key === "tt" ? "TT Hotel settings saved." : "Connecteam settings saved.");
    });
  }

  window.addEventListener("beforeunload", (e) => {
    if (connDirty.tt || connDirty.ct || gdDirty) e.preventDefault();
  });

  // "Settings → Connections" links elsewhere open that connection (data-goto shows the tab).
  document.querySelectorAll("[data-open-conn]").forEach((b) => b.addEventListener("click", () => {
    const box = document.getElementById(b.dataset.openConn);
    box.open = true;
    $("summary", box).focus();
  }));

  // Tests the saved keys, so Save first: each step (a token, users, the time clock, smart groups)
  // with Connecteam's own words. It lifts a stop.
  $("#ct-test").addEventListener("click", () => run(async () => {
    if (connDirty.ct) throw new Error("Click Save Connecteam settings first: the test uses what's saved.");
    const button = $("#ct-test");
    button.disabled = true;
    button.textContent = "Testing…";
    try {
      const r = await api("POST", "api/connecteam/test");
      showChecks($("#ct-checks"), r.checks);
      toast(r.ok ? "Connecteam works." : "Connecteam has a problem: see the list.", !r.ok);
    } finally {
      button.disabled = false;
      button.textContent = "Test connection";
      await load();
    }
  }));

  // ------------------------------------------------ Google Drive (HR files), in Connections

  // The script in the owner's Google account that keeps the HR portal's files. Folded, it says
  // whether it works; opened, it's checked (who it runs as, its drives, its version: at most every
  // few seconds) and shows the setup steps. The secret never comes here, only inside the script
  // text when it's copied.
  let gdDirty = false;
  let gdFull = null;   // api/hr-google: also the Web app URL, the drives chosen and the drives seen
  const GD_PROBLEMS = { auth: "Secret doesn't match", not_allowed: "Access refused",
    unavailable: "Google not answering", quota: "Google busy", bad_reply: "Unexpected reply",
    too_big: "File too big", test: "Test failed" };

  const googleNow = () => ({ ...(gdFull || {}), ...(state.hrGoogle || {}) });

  // Whether it works, in a few words, with its badge color.
  function gdState(g) {
    if (!g.set_up) return ["Not set up", ""];
    if (g.code === "outdated") return ["Script out of date", "pending"];
    if (g.code) return [GD_PROBLEMS[g.code] || "Last try failed", "locked"];
    if (g.email && !g.drive_set) return ["Pick the shared drive", "pending"];
    if (g.email) return [`Connected as ${g.email}`, "active"];
    return ["Not checked yet", "pending"];
  }

  function renderGoogle() {
    const g = googleNow();
    showConnection($("#conn-google"), gdState(g), g.checked_at ? `Checked ${ago(g.checked_at).toLowerCase()}` : "");
    showState($("#hr-google-state"), gdState(g));
    $("#gd-new-secret").hidden = !g.script_made;
    $("#gd-test").disabled = !g.set_up;
    $("#gd-status").textContent = !g.set_up
      ? (g.script_made ? "Paste the Web app URL and click Save." : "Start with Copy the script.")
      : g.error || (g.email
        ? `Connected as ${g.email}, script version ${g.version}.${g.drive_set ? "" : " Pick the shared drive and click Save."}`
        : "Not checked yet.");
    // Set up before or after turning the HR portal on: it only uses Google once it's on.
    $("#gd-hr-off").hidden = state.hr?.status === "on";
    if (gdFull && !gdDirty) fillGoogle();
  }

  // The form, from what's saved: the drive pickers list the shared drives the script saw.
  function fillGoogle() {
    const form = $("#gd-form");
    const g = googleNow();
    const drives = g.drives || [];
    form.elements.url.value = g.url || "";
    const pick = (select, first, chosen) => {
      const options = [h("option", { value: "" }, first), ...drives.map((d) => h("option", { value: d.id }, d.name))];
      if (chosen && !drives.some((d) => d.id === chosen)) options.push(h("option", { value: chosen }, "The drive saved before"));
      select.replaceChildren(...options);
      select.value = chosen || "";
    };
    pick(form.elements.drive, drives.length ? "Choose…"
      : g.set_up ? "No shared drives found yet" : "Save the Web app URL first", g.drive);
    pick(form.elements.restricted_drive, "A “Restricted” folder in the HR drive", g.restricted_drive);
    renderRestrictedWarning();
  }

  function renderRestrictedWarning() {
    const f = $("#gd-form").elements;
    $("#gd-restricted-warn").hidden = !f.drive.value || !!f.restricted_drive.value;
  }

  // Opened: check it (the server answers from its last check if that was a moment ago).
  $("#conn-google").addEventListener("toggle", (e) => {
    if (e.target.open) refreshGoogle();
  });

  async function refreshGoogle() {
    try {
      const r = googleNow().set_up ? await api("POST", "api/hr-google/check")
        : { google: await api("GET", "api/hr-google") };
      gdFull = r.google;
      state.hrGoogle = r.google;
    } catch (err) {
      toast(err.message, true);
    }
    renderGoogle();
  }

  $("#gd-form").addEventListener("input", () => { gdDirty = true; });
  $("#gd-form").addEventListener("change", () => {
    gdDirty = true;
    renderRestrictedWarning();
  });

  $("#gd-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const f = e.target.elements;
    run(async () => {
      const r = await api("PUT", "api/hr-google", { url: f.url.value, drive: f.drive.value,
        restricted_drive: f.restricted_drive.value });
      gdDirty = false;
      gdFull = r.google;
      state.hrGoogle = r.google;
      renderGoogle();
      if (r.problem) toast(`Saved, but Google says: ${r.problem}`, true);
      else if (r.google.set_up && !r.google.drive_set && (r.google.drives || []).length) {
        toast("Saved. Now pick the shared drive and click Save.");
      } else toast("Google Drive settings saved.");
    });
  });

  // Copy the script (the secret is made the first time). Inside Home Assistant's frame the
  // clipboard can be blocked: then the script is shown, selected, to copy by hand.
  async function copyScript(newSecret) {
    const r = await api("POST", "api/hr-google/script", newSecret ? { new_secret: true } : undefined);
    const box = $("#gd-script-box");
    const text = $("#gd-script-text");
    text.value = r.script;
    let copied = true;
    try {
      await navigator.clipboard.writeText(r.script);
    } catch {
      box.hidden = false;
      text.focus();
      text.select();
      copied = document.execCommand("copy");
    }
    box.hidden = copied;
    const note = $("#gd-script-note");
    note.hidden = !newSecret;
    note.textContent = "Paste the new script in script.google.com over the old one, then click Deploy → " +
      "Manage deployments → Edit (the pencil) → Version: New version → Deploy. Until then, the script there " +
      "doesn't answer Staff Access.";
    await load();
    renderGoogle();
    toast(copied ? "Script copied. Paste it in script.google.com." : "Select the script in the box and copy it.");
  }

  $("#gd-copy").addEventListener("click", () => run(() => copyScript(false)));
  // The script project's settings (appsscript.json): Drive turned on and full Drive access asked
  // for, so Google can't hand the script read-only access.
  $("#gd-copy-manifest").addEventListener("click", () => run(async () => {
    const text = googleNow().manifest || (await api("GET", "api/hr-google")).manifest;
    const box = $("#gd-script-box");
    let copied = true;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      $("#gd-script-text").value = text;
      box.hidden = false;
      $("#gd-script-text").focus();
      $("#gd-script-text").select();
      copied = document.execCommand("copy");
    }
    box.hidden = copied;
    toast(copied ? "Settings copied. Paste them over everything in appsscript.json."
      : "Select the settings in the box and copy them.");
  }));
  $("#gd-new-secret").addEventListener("click", () => run(async () => {
    if (!await askConfirm("Make a new secret? The script in Google stops working until you paste the new " +
      "script there and deploy a new version.", "Make a new secret")) return false;
    await copyScript(true);
  }));

  // Test connection: each check with a plain result.
  $("#gd-test").addEventListener("click", () => run(async () => {
    if (gdDirty) throw new Error("Click Save Google Drive settings first: the test uses what's saved.");
    const button = $("#gd-test");
    button.disabled = true;
    button.textContent = "Testing…";
    try {
      const r = await api("POST", "api/hr-google/test");
      gdFull = r.google;
      state.hrGoogle = r.google;
      showChecks($("#gd-checks"), r.checks);
      toast(r.checks.every((c) => c.ok) ? "Google Drive works." : "Google Drive has a problem: see the list.",
        !r.checks.every((c) => c.ok));
    } finally {
      button.textContent = "Test connection";
      renderGoogle();
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
    for (const name of ["allow_phone_reorder", "require_pin", "phones_via_tunnel_only", "control_lock"]) {
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

  // ---------------------------------------------------------------- wristbands

  // TT Hotel wristbands and key cards, made on the card encoder at a front-desk computer. The
  // add-on itself talks to TT Hotel and to the encoder; this tab sets up the account, the defaults
  // and the door names, and the Roles tab says who may make which cards (wristbandPicker).

  const WB_TYPES = [["day_pass", "Day passes"], ["overnight", "Overnight wristbands"],
    ["contractor", "Contractor badges"], ["staff", "Staff cards"]];
  const WB_TYPE_NAMES = { day_pass: "Day pass", overnight: "Overnight", contractor: "Contractor badge",
    staff: "Staff card" };
  const WB_NOUNS = { staff: "card", contractor: "badge" };   // everything else is a wristband
  // What a door is marked as (Doors, below): staff doors only go on staff cards and contractor badges.
  const KIND_WORDS = { public: "public door", room: "guest room", staff: "staff door" };
  const KIND_PLURALS = { public: "public doors", room: "guest rooms", staff: "staff doors" };
  const WB_LEVELS = [["door", "Single doors"], ["floor", "Whole floors"], ["building", "Whole buildings"],
    ["all", "Every door (a master card)"]];
  const WB_NONE = { types: [], staff_levels: [], doors: { buildings: [], floors: [], doors: [] },
    edit_doors: false, edit_end_time: false, max_nights: 0, max_staff_days: 0, see_all_cards: false };
  const WB_MAX_LIMIT = 3650; // the add-on's most nights or staff-card days (ten years)
  const WB_JOB_PAGE = 50;    // card jobs per page, as the add-on sends them
  const WB_SHOWN_NUMBERS = 6; // card numbers listed on one row of Cards made before "+N more"
  // FormData leaves unticked boxes out, so every checkbox in the form is listed here.
  const WB_CHECKBOXES = ["wristbands_enabled", "wb_lockout_daypass",
    "wb_lockout_overnight", "wb_lockout_contractor", "wb_lockout_staff", "wb_remote_lockout"];
  const WB_STATUS = { queued: "Waiting for the encoder", running: "Being made", needs_attention: "Needs attention",
    done: "Done", cancelled: "Stopped" };
  // The day-pass and overnight door lists, ticked but maybe not saved yet. Kept apart from the
  // door list so loading the doors again never loses a tick.
  const wbPicks = { wb_daypass_doors: new Set(), wb_overnight_doors: new Set() };
  let doorsLoading = null;
  let doorsError = null;
  let oldestJob = null;

  const clone = (v) => (v === undefined ? v : JSON.parse(JSON.stringify(v)));
  const wristbandsOn = () => state.settings.wristbands_enabled === "1";
  const plural = (n, word) => `${n} ${n === 1 ? word : `${word}s`}`;
  const count = (n) => Number(n || 0).toLocaleString();
  const listNames = (names) => (names.length <= 4 ? names.join(", ")
    : `${names.slice(0, 4).join(", ")} and ${names.length - 4} more`);

  // What staff see a door called: its name here, else its TT Hotel alias, else TT Hotel's name.
  const doorName = (d) => d.name || d.alias || d.tt_name || `Door ${d.id}`;
  const buildingName = (d) => (d.building == null ? "No building number" : d.building_name || `Building ${d.building}`);
  const floorName = (d) => (d.floor == null ? "No floor number" : d.floor_name || `Floor ${d.floor}`);

  // A door list setting (JSON text of TT Hotel lock IDs); anything damaged reads as none.
  function lockIds(text) {
    try {
      const ids = JSON.parse(text || "[]");
      return Array.isArray(ids) ? ids.filter((id) => Number.isSafeInteger(id) && id > 0) : [];
    } catch {
      return [];
    }
  }

  // Doors by building, then floor, in TT Hotel's numbering (doors without a number last).
  function doorGroups(doors) {
    const byNumber = (a, b) => (a.number == null) - (b.number == null) || (a.number ?? 0) - (b.number ?? 0);
    const byName = (a, b) => doorName(a).localeCompare(doorName(b), undefined, { numeric: true });
    const buildings = new Map();
    for (const d of doors) {
      if (!buildings.has(d.building)) {
        buildings.set(d.building, { number: d.building, name: buildingName(d), floors: new Map(), count: 0 });
      }
      const b = buildings.get(d.building);
      if (d.building_name && d.building != null && b.name === `Building ${d.building}`) b.name = d.building_name;
      if (!b.floors.has(d.floor)) b.floors.set(d.floor, { number: d.floor, name: floorName(d), doors: [] });
      const f = b.floors.get(d.floor);
      if (d.floor_name && d.floor != null && f.name === `Floor ${d.floor}`) f.name = d.floor_name;
      f.doors.push(d);
      b.count += 1;
    }
    return [...buildings.values()].sort(byNumber).map((b) => ({ ...b,
      floors: [...b.floors.values()].sort(byNumber).map((f) => ({ ...f, doors: f.doors.sort(byName) })) }));
  }

  // Whether a permission's doors include this one, as the add-on decides it: every door, its
  // building, its building and floor, or the door itself.
  function scopeHas(doors, d) {
    if (!doors) return false;
    if (doors.all === true || (doors.doors || []).includes(d.id)) return true;
    if (d.building == null) return false;
    return (doors.buildings || []).includes(d.building) || (d.floor != null &&
      (doors.floors || []).some(([b, f]) => b === d.building && f === d.floor));
  }

  // A permission in a few words: "day passes, staff cards; all doors" ("no wristbands" if none).
  function wbSummary(perm) {
    const types = WB_TYPES.filter(([t]) => perm?.types?.includes(t)).map(([, label]) => label.toLowerCase());
    if (!types.length) return "no wristbands";
    const d = perm.doors || {};
    const doors = d.all === true ? ["all doors"] : [
      d.buildings?.length && plural(d.buildings.length, "building"),
      d.floors?.length && plural(d.floors.length, "floor"),
      d.doors?.length && plural(d.doors.length, "door")].filter(Boolean);
    return `${types.join(", ")}; ${doors.length ? doors.join(", ") : "no doors"}`;
  }

  // TT Hotel's doors as last synced. Whatever shows them is drawn again once they arrive.
  function loadDoors() {
    if (doorsLoading) return doorsLoading;
    doorsLoading = (async () => {
      try {
        wbDoors = (await api("GET", "api/wristbands/doors")).doors || [];
        doorsError = null;
      } catch (err) {
        doorsError = err.message;
      }
      doorsLoading = null;
      renderDoors();
      renderDefaultDoors();
      renderDepartments();
      renderStaff();   // each person's "Make key card" line names the doors
      if (editing && !$("#tab-roles").hidden) renderRoleEditor();
    })();
    return doorsLoading;
  }

  // "Loading doors…", or why they couldn't be loaded, while there's no door list yet.
  function doorsPending() {
    if (!doorsError) return h("p", { class: "hint" }, "Loading doors…");
    return h("p", { class: "hint" }, `Couldn't load the doors: ${doorsError} `,
      h("button", { type: "button", class: "linkish", onclick: () => { doorsError = null; loadDoors(); } }, "Try again"));
  }

  // ------------------------------------------------ who may make which cards (Roles tab)

  // The Wristbands part of the role and person editors.
  function wristbandSection() {
    if (editing.wristbands === undefined) return []; // an add-on without wristbands
    const head = h("h3", { class: "editor-sub" }, "Wristbands");
    if (!wristbandsOn()) {
      return [head, h("p", { class: "hint" }, "Turn on wristbands on the ",
        h("button", { type: "button", class: "linkish", onclick: () => selectTab("wristbands") }, "Wristbands"),
        " tab to let people make TT Hotel wristbands and key cards at the front desk.")];
    }
    const person = editing.kind === "person";
    const role = person ? state.roles.find((r) => r.id === editing.role_id) : null;
    return [head,
      h("p", { class: "hint" }, person
        ? `What ${editing.name} may make when they sign in on a role-based front-desk computer with a card ` +
          "encoder. On a shared computer, that computer's role decides."
        : "What people with this role may make when they sign in on a front-desk computer with a card " +
          "encoder: a role-based one, or a shared one with this role. Each card is recorded under the " +
          "name of the person who made it."),
      wristbandPicker(editing.wristbands, person ? `Same as their role (${wbSummary(role?.wristbands)})` : null,
        (value) => {
          editing.wristbands = value;
          markEditing();
        }, person ? role?.wristbands ?? null : null)];
  }

  // Wristband permissions for a role (null: makes none) or a person (null: the same as their
  // role; inheritLabel names that choice and `inherited` is their role's, to start from). Works
  // on its own deep copy and calls onChange with a fresh value after every change.
  function wristbandPicker(value, inheritLabel, onChange, inherited = null) {
    const person = inheritLabel != null;
    const start = { ...clone(WB_NONE), ...clone(value ?? inherited ?? WB_NONE) };
    const types = new Set(start.types || []);
    const levels = new Set(start.staff_levels || []);
    const flags = { edit_doors: start.edit_doors === true, edit_end_time: start.edit_end_time === true,
      see_all_cards: start.see_all_cards === true };
    const limits = { max_nights: start.max_nights || 0, max_staff_days: start.max_staff_days || 0 };
    // Sets, so unticking a building brings back the floors and doors that were ticked inside it.
    const doors = start.doors || {};
    const scope = { all: doors.all === true, buildings: new Set(doors.buildings || []),
      floors: new Set((doors.floors || []).map(([b, f]) => `${b}:${f}`)), doors: new Set(doors.doors || []) };
    const toggle = (set, key, on) => (on ? set.add(key) : set.delete(key));

    // A ticked building or floor stands for everything in it, so what's inside isn't stored too.
    const scopeValue = () => {
      if (scope.all) return { all: true };
      const floors = [...scope.floors].map((k) => k.split(":").map(Number)).filter(([b]) => !scope.buildings.has(b));
      const open = new Set(floors.map(([b, f]) => `${b}:${f}`));
      const known = new Map((wbDoors || []).map((d) => [d.id, d]));
      const ids = [...scope.doors].filter((id) => {
        const d = known.get(id);
        return !d || d.building == null ||
          !(scope.buildings.has(d.building) || (d.floor != null && open.has(`${d.building}:${d.floor}`)));
      });
      return { buildings: [...scope.buildings].sort((a, b) => a - b), floors, doors: ids };
    };
    const current = () => ({
      types: WB_TYPES.map(([t]) => t).filter((t) => types.has(t)),
      staff_levels: WB_LEVELS.map(([l]) => l).filter((l) => levels.has(l)),
      doors: scopeValue(), ...flags, ...limits });

    const same = person ? h("input", { type: "checkbox", checked: value === null }) : null;
    const check = (checked, label, onchange, note) => h("label", { class: "check" },
      h("input", { type: "checkbox", checked, onchange }),
      note ? h("span", null, label, h("small", { class: "wb-count" }, note)) : label);
    const checkRow = (checked, label, note, onchange) => h("label", { class: "check-row" },
      h("input", { type: "checkbox", checked, onchange }), h("span", null, label, h("small", null, note)));
    const limit = (key, label) => {
      const input = h("input", { type: "number", min: "0", max: String(WB_MAX_LIMIT), step: "1",
        value: String(limits[key]) });
      input.addEventListener("input", () => {
        const n = Math.floor(Number(input.value));
        limits[key] = Number.isFinite(n) ? Math.max(0, Math.min(WB_MAX_LIMIT, n)) : 0;
        emit();
      });
      return h("label", { class: "wb-number" }, label, input, h("small", null, "0 means no limit"));
    };

    const typeBoxes = WB_TYPES.map(([t, label]) => check(types.has(t), label, (ev) => {
      toggle(types, t, ev.target.checked);
      emit();
    }, t === "contractor" ? " · any of the doors below, for up to 7 days" : null));
    const levelBox = h("div", { class: "wb-indent" },
      h("div", { class: "wb-count" }, "Staff cards they make may open:"),
      WB_LEVELS.map(([l, label]) => check(levels.has(l), label, (ev) => {
        toggle(levels, l, ev.target.checked);
        emit();
      })));
    const treeBox = h("div", { class: "wb-tree-box" });
    const allBox = h("input", { type: "checkbox", checked: scope.all, onchange: () => {
      scope.all = allBox.checked;
      treeBox.hidden = scope.all;
      emit();
    } });
    treeBox.hidden = scope.all;
    const warn = h("p", { class: "wb-warn" });
    const editDoors = checkRow(flags.edit_doors, "Change the doors when making a day pass or overnight wristband",
      "Otherwise they get the doors set on the Wristbands tab. They can only add doors they have access to.",
      (ev) => { flags.edit_doors = ev.target.checked; emit(); });
    const editEnd = checkRow(flags.edit_end_time, "May change when day passes stop working",
      "Any time later than now, up to 11:59 PM that day, even after the usual time. Otherwise day passes " +
      "stop at the time set on the Wristbands tab.",
      (ev) => { flags.edit_end_time = ev.target.checked; emit(); });
    const nights = limit("max_nights", "Longest overnight stay (nights)");
    const days = limit("max_staff_days", "Longest a staff card lasts (days)");
    const seeAll = checkRow(flags.see_all_cards, "See everyone's cards",
      "In the card log and when looking up a wristband. Otherwise only the ones they made.",
      (ev) => { flags.see_all_cards = ev.target.checked; emit(); });
    const rest = h("div", { class: "wb-body" },
      h("div", { class: "wb-sub" }, "Doors they may put on cards"),
      h("label", { class: "check" }, allBox, "All doors, including ones added later"),
      treeBox, warn, editDoors, editEnd, nights, days, seeAll);
    const body = h("div", { class: "wb-body" }, h("div", { class: "wb-sub" }, "Cards they may make"),
      typeBoxes, levelBox, rest);

    // Default doors outside what's ticked would make every one of those cards fail, so say so.
    const warnings = () => {
      const out = [];
      const v = scopeValue();
      const outside = (key) => lockIds(state.settings[key]).map((id) => wbDoors?.find((d) => d.id === id))
        .filter((d) => d && d.active && d.kind !== "staff" && !scopeHas(v, d)).map(doorName);
      if (types.has("day_pass")) {
        const missing = outside("wb_daypass_doors");
        if (!lockIds(state.settings.wb_daypass_doors).length) {
          out.push("No day-pass doors are chosen on the Wristbands tab yet, so no one can make day passes.");
        } else if (missing.length) {
          out.push(`Day passes open ${listNames(missing)}, which ${missing.length === 1 ? "isn't" : "aren't"} ` +
            "ticked here, so they can't make day passes. Tick those doors, or All doors.");
        }
      }
      const shared = types.has("overnight") ? outside("wb_overnight_doors") : [];
      if (shared.length) {
        out.push(`Every overnight wristband opens ${listNames(shared)}, which ${shared.length === 1 ? "isn't" : "aren't"} ` +
          "ticked here, so they can't make overnight wristbands. Tick those doors, or All doors.");
      }
      if (types.has("staff") && levels.has("all") && !scope.all) {
        out.push("A master card also needs All doors ticked.");
      }
      if (types.has("staff") && !levels.size) out.push("Choose what the staff cards they make may open.");
      return out;
    };
    const sync = () => {
      body.hidden = !!same?.checked;
      levelBox.hidden = !types.has("staff");
      rest.hidden = !types.size;
      editDoors.hidden = !types.has("day_pass") && !types.has("overnight");
      editEnd.hidden = !types.has("day_pass");
      nights.hidden = !types.has("overnight");
      days.hidden = !types.has("staff");
      const notes = warnings();
      warn.replaceChildren(...notes.flatMap((t, i) => (i ? [h("br"), t] : [t])));
      warn.hidden = !notes.length;
    };
    function emit() {
      if (same?.checked) onChange(null);
      else {
        const v = current();
        // A role that makes no cards is stored as none at all.
        onChange(!person && !v.types.length ? null : v);
      }
      sync();
    }

    const treeRow = (picked, label, note, onPick) => {
      const box = h("input", { type: "checkbox", checked: picked });
      const small = h("small", { class: "wb-count" });
      const describe = () => { small.textContent = note(box.checked); };
      box.addEventListener("change", () => { describe(); onPick(box.checked); emit(); });
      describe();
      return h("label", { class: "check" }, box, h("span", null, label, small));
    };
    const drawTree = () => {
      if (!wbDoors) {
        treeBox.replaceChildren(doorsPending());
        if (!doorsError) loadDoors(); // draws the editor again when they arrive
        return;
      }
      const active = wbDoors.filter((d) => d.active);
      if (!active.length) {
        treeBox.replaceChildren(h("p", { class: "hint" }, "No doors yet. Sync them from TT Hotel on the ",
          h("button", { type: "button", class: "linkish", onclick: () => selectTab("wristbands") }, "Wristbands"), " tab."));
        return;
      }
      const doorItem = (d) => h("li", null, check(scope.doors.has(d.id), doorName(d), (ev) => {
        toggle(scope.doors, d.id, ev.target.checked);
        emit();
      }, d.hidden ? " · hidden from staff" : null));
      const howMany = (n) => (on) => (on ? ` · all ${plural(n, "door")}, and ones added later` : ` · ${plural(n, "door")}`);
      treeBox.replaceChildren(h("ul", { class: "wb-tree" }, doorGroups(active).map((b) => {
        const floors = h("ul", { class: "wb-tree" }, b.floors.map((f) => {
          const inside = h("ul", { class: "wb-tree" }, f.doors.map(doorItem));
          // Doors without a building or floor number can only be chosen one by one.
          if (b.number == null || f.number == null) return h("li", null, h("div", { class: "wb-tree-head" }, f.name), inside);
          const key = `${b.number}:${f.number}`;
          inside.hidden = scope.floors.has(key);
          return h("li", null, treeRow(scope.floors.has(key), f.name, howMany(f.doors.length), (on) => {
            toggle(scope.floors, key, on);
            inside.hidden = on;
          }), inside);
        }));
        if (b.number == null) return h("li", null, h("div", { class: "wb-tree-head" }, b.name), floors);
        floors.hidden = scope.buildings.has(b.number);
        return h("li", null, treeRow(scope.buildings.has(b.number), b.name, howMany(b.count), (on) => {
          toggle(scope.buildings, b.number, on);
          floors.hidden = on;
        }), floors);
      })));
    };

    same?.addEventListener("change", emit);
    drawTree();
    sync();
    return h("div", { class: "staff-pick wb-pick" }, same ? h("label", { class: "check" }, same, inheritLabel) : null, body);
  }

  // ------------------------------------------------ the Wristbands tab

  function openWristbands() {
    // Unsaved changes stay on screen; otherwise show what's saved now.
    if (wbDirty) renderWristbandStatus();
    else fillWristbands();
    renderEncoderList();
    loadDoors();
    loadWristbandJobs(true);
    openDoorActivity();
  }

  const isWristbandSetting = (key) => key === "wristbands_enabled" || key.startsWith("tthotel_") || key.startsWith("wb_");

  function fillWristbands() {
    const form = $("#wb-form");
    for (const [k, v] of Object.entries(state.settings)) {
      if (!isWristbandSetting(k)) continue;
      const field = form.elements[k];
      if (field?.type === "checkbox") field.checked = v === "1";
      else if (field) field.value = v;
    }
    for (const key of Object.keys(wbPicks)) wbPicks[key] = new Set(lockIds(state.settings[key]));
    wbDirty = false;
    renderWristbandStatus();
    renderRemoteStatus();
    renderZoneNote();
    renderDefaultDoors();
  }

  // The Client Secret and password are write-only: the dashboard only learns whether they're saved.
  function renderWristbandSecrets() {
    const form = $("#tt-form");
    const saved = { tthotel_client_secret: state.settings.tthotel_client_secret_set === "1",
      tthotel_password_md5: state.settings.tthotel_password_md5_set === "1" };
    for (const [name, isSaved] of Object.entries(saved)) {
      form.elements[name].value = "";
      form.elements[name].placeholder = isSaved ? "Saved — leave blank to keep" : "";
    }
    form.elements["tthotel_clear_secret"].checked = false;
    $("#wb-clear-row").hidden = !saved.tthotel_client_secret && !saved.tthotel_password_md5;
  }

  // TT Hotel's status (Settings → Connections, and its line on this tab) and the door count.
  function renderWristbandStatus() {
    renderConnections();
    renderDoorStatus();
  }

  function renderTTStatus() {
    const wb = state.wristbands || {};
    const lines = [];
    if (!wb.enabled) lines.push("Turned off.");
    else {
      lines.push(wb.halted ? `Stopped: ${wb.halted} Fix it, then save the settings or click Test connection.`
        : wb.paused ? `Paused: ${wb.pause_reason || "TT Hotel asked Staff Access to wait."}`
        : wb.last_error ? `Problem: ${wb.last_error}`
        : wb.synced_at ? "Connected."
        : "Not connected yet. Save the settings, then click Test connection.");
      if (wb.synced_at) lines.push(`Doors last synced ${ago(wb.synced_at).toLowerCase()}: ${plural(wb.doors || 0, "door")}.`);
      lines.push(wb.needed
        ? `Checking for new doors about every ${plural(Number(state.settings.tthotel_sync_hours) || 6, "hour")}.`
        : "Nothing uses TT Hotel's door list yet (no computer has a card encoder, no key card was made on " +
          "This computer lately and no door activity came in), so TT Hotel isn't checked for new doors.");
      lines.push(`Calls this month: ${count(wb.used_month)} of ${count(wb.monthly_cap)} ` +
        `(TT Hotel's free limit is ${count(wb.quota || 30000)}).`);
    }
    $("#wb-status").replaceChildren(...lines.flatMap((t, i) => (i ? [h("br"), t] : [t])));
  }

  // Which time zone cards use: the one typed here, else Home Assistant's.
  function renderZoneNote() {
    const field = $("#wb-form").elements["wb_time_zone"];
    const typed = field.value.trim();
    const saved = state.settings.wb_time_zone || "";
    const ha = state.wristbands?.ha_time_zone;
    const haText = ha ? `Home Assistant's, ${ha}` : "Home Assistant's";
    field.placeholder = ha ? `${ha} (Home Assistant's)` : "Home Assistant's time zone";
    $("#wb-zone-note").textContent = typed !== saved
      ? `${typed || haText} takes effect when you save.`
      : `In effect: ${saved || haText}.`;
  }

  // The day-pass and overnight door lists: the public doors (and doors not marked yet), grouped
  // by building and floor. Guest rooms and staff doors are left out.
  function renderDefaultDoors() {
    drawDoorChecks($("#wb-daypass-doors"), $("#wb-daypass-count"), wbPicks.wb_daypass_doors,
      $("#wb-daypass-rooms"), "day passes open");
    drawDoorChecks($("#wb-overnight-doors"), $("#wb-overnight-count"), wbPicks.wb_overnight_doors,
      $("#wb-overnight-rooms"), "every overnight wristband opens");
  }

  // Guest rooms ticked as shared doors (marked after they were chosen) still go on the cards, so
  // say so. Staff doors never do: marking one takes it off these lists, and one still ticked
  // (from a restored backup, say) is left off every card and dropped when the settings are saved.
  function sharedRoomsWarning(warnEl, picked, opens) {
    const rooms = (wbDoors || []).filter((d) => picked.has(d.id) && d.kind === "room");
    const staff = (wbDoors || []).filter((d) => picked.has(d.id) && d.kind === "staff");
    const lines = [];
    if (rooms.length) {
      const one = rooms.length === 1;
      lines.push(`${listNames(rooms.map(doorName))} ${one ? "is" : "are"} marked as ${one ? "a guest room" : "guest rooms"}, ` +
        `but ${one ? "is" : "are"} ticked here, so ${opens} ${one ? "it" : "them"}. ` +
        `Untick ${one ? "it" : "them"}, or mark ${one ? "it as a public door" : "them as public doors"} under Doors below.`);
    }
    if (staff.length) {
      const one = staff.length === 1;
      lines.push(`${listNames(staff.map(doorName))} ${one ? "is a staff door" : "are staff doors"}, so ` +
        `${one ? "it's" : "they're"} left off these cards, and unticked when you save.`);
    }
    warnEl.textContent = lines.join(" ");
    warnEl.hidden = !lines.length;
  }

  // The shared doors (day passes, every overnight wristband) the add-on took a new staff door
  // off ({setting: [lock ID]}): off the saved settings and the ticks here too.
  // newStaff: the doors just made staff doors, unticked here too (saved or not).
  function applyUnshared(unshared, newStaff = []) {
    for (const [key, ids] of Object.entries(unshared || {})) {
      if (!wbPicks[key]) continue;
      state.settings[key] = JSON.stringify(lockIds(state.settings[key]).filter((id) => !ids.includes(id)));
    }
    for (const picked of Object.values(wbPicks)) {
      for (const id of [...Object.values(unshared || {}).flat(), ...newStaff.map((d) => d.id)]) picked.delete(id);
    }
  }

  // Which of these doors are day-pass or overnight shared doors (ticked, saved or not), as
  // "the day-pass doors and the overnight shared doors".
  function sharedPlaces(doors) {
    const ids = new Set(doors.map((d) => d.id));
    const where = [["wb_daypass_doors", "the day-pass doors"], ["wb_overnight_doors", "the overnight shared doors"]]
      .filter(([key]) => [...wbPicks[key]].some((id) => ids.has(id)) ||
        lockIds(state.settings[key]).some((id) => ids.has(id)))
      .map(([, words]) => words);
    return where.join(" and ");
  }

  function sharedDoorsOf(doors) {
    return doors.filter((d) => ["wb_daypass_doors", "wb_overnight_doors"].some((key) =>
      wbPicks[key].has(d.id) || lockIds(state.settings[key]).includes(d.id)));
  }

  function drawDoorChecks(box, countEl, picked, warnEl, opens) {
    const showCount = () => {
      countEl.textContent = `(${picked.size} chosen)`;
      sharedRoomsWarning(warnEl, picked, opens);
    };
    showCount();
    if (!wbDoors) {
      box.replaceChildren(doorsPending());
      return;
    }
    // Shared doors are public doors, or ones not marked yet. A guest room, a staff door or a door
    // TT Hotel no longer lists stays here while it's ticked, so it can be unticked.
    const doors = wbDoors.filter((d) => (d.active && (!d.kind || d.kind === "public")) || picked.has(d.id));
    const unknown = [...picked].filter((id) => !wbDoors.some((d) => d.id === id));
    if (!doors.length && !unknown.length) {
      box.replaceChildren(h("p", { class: "hint" }, wbDoors.some((d) => d.active)
        ? "Every door is marked as a guest room or a staff door. Mark the shared doors as public doors under Doors below."
        : "No doors yet. Save your TT Hotel details, then click Test connection or Sync from TT Hotel."));
      return;
    }
    const row = (id, label, note) => h("label", { class: "check pick-row" },
      h("input", { type: "checkbox", checked: picked.has(id), onchange: (ev) => {
        if (ev.target.checked) picked.add(id);
        else picked.delete(id);
        showCount();
      } }),
      h("span", null, label, note ? h("small", null, note) : null));
    const rows = [];
    for (const b of doorGroups(doors)) {
      for (const f of b.floors) {
        rows.push(h("div", { class: "wb-door-group" }, `${b.name} · ${f.name}`));
        for (const d of f.doors) {
          rows.push(row(d.id, doorName(d), !d.active ? " · no longer in TT Hotel"
            : d.kind === "room" || d.kind === "staff" ? ` · ${KIND_WORDS[d.kind]}` : d.hidden ? " · hidden from staff" : ""));
        }
      }
    }
    for (const id of unknown) rows.push(row(id, `Door ${id}`, " · not in TT Hotel's list"));
    box.replaceChildren(...rows);
  }

  $("#wb-form").addEventListener("input", (e) => {
    wbDirty = true;
    if (e.target.name === "wb_time_zone") renderZoneNote();
  });
  $("#wb-form").addEventListener("change", () => { wbDirty = true; });

  // Sends only the Wristbands settings; the add-on leaves every other setting as it is.
  $("#wb-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.target;
    const body = Object.fromEntries(new FormData(form).entries());
    for (const name of WB_CHECKBOXES) body[name] = form.elements[name].checked ? "1" : "0";
    for (const [key, picked] of Object.entries(wbPicks)) body[key] = [...picked];
    run(async () => {
      await api("PUT", "api/settings", body);
      await load();
      fillWristbands();
    }, "Wristband settings saved.");
  });

  // Tests the saved details, so Save first.
  $("#wb-test").addEventListener("click", () => run(async () => {
    if (connDirty.tt) throw new Error("Click Save TT Hotel settings first: the test uses what's saved.");
    if (!state.wristbands?.enabled) {
      throw new Error("Wristbands is off: tick Make wristbands and key cards on the Wristbands tab and save first.");
    }
    try {
      const r = await api("POST", "api/wristbands/test");
      toast(`Connected: ${plural(r.doors, "door")} in ${plural(r.buildings, "building")}.`);
    } finally {
      await load();
      await loadDoors();
    }
  }));

  $("#wb-sync").addEventListener("click", () => run(async () => {
    try {
      const r = await api("POST", "api/wristbands/doors/sync");
      toast(`Doors synced: ${r.added} added, ${r.updated} updated, ${r.removed} removed, ${r.total} in all.`);
    } finally {
      await load();
      await loadDoors();
    }
  }));

  try {
    $("#wb-zones").replaceChildren(...Intl.supportedValuesOf("timeZone").map((z) => h("option", { value: z })));
  } catch { /* an older browser: the time zone can still be typed */ }

  // ------------------------------------------------ card encoders

  const isLocal = (address) => String(address || "").startsWith("local:");

  // Whether a computer's card shows its encoder. This computer (USB) is every new computer's own
  // default, so it says nothing while Wristbands is off.
  const showsEncoder = (d) => d.kind === "computer" && d.encoder_mode !== "off" &&
    (d.encoder_mode !== "local" || wristbandsOn());

  // A computer's card encoder on its card in Phones & computers.
  function encoderBadge(d) {
    const s = state.wristbands?.encoders?.[d.encoder_address] || {};
    if (d.encoder_mode === "local") {
      // This computer (USB): its browser link first, as nothing works without it.
      const link = d.encoder_link || {};
      const [text, cls] = !link.link ? ["Card encoder: not connected", ""]
        : ["blocked", "unreachable"].includes(link.helper) ? ["Card encoder: problem", "locked"]
        : s.busy ? ["Card encoder: making cards", "active"]
        : s.error ? ["Card encoder: problem", "locked"]
        : link.helper === "ready" ? ["Card encoder: connected", "active"]
        : ["Card encoder: connecting", "pending"];
      return h("span", { class: `badge ${cls}`.trim(), title: (link.link && s.error) || linkStatus(d)[0] }, text);
    }
    if (!d.encoder_address) {
      return h("span", { class: "badge", title: d.encoder_text || "" }, "Card encoder: not found");
    }
    const [text, cls] = s.error ? ["Card encoder: problem", "locked"]
      : s.busy ? ["Card encoder: making cards", "active"]
      : s.connected ? ["Card encoder", "active"] : ["Card encoder", ""];
    return h("span", { class: `badge ${cls}`.trim(), title: s.error || d.encoder_text || d.encoder_address }, text);
  }

  const cardEncoderBadge = (d) => (showsEncoder(d) ? encoderBadge(d) : null);

  // Under a computer's name in Phones & computers: its card encoder (Automatic and This computer
  // (USB), which change on their own), and why Wristbands won't show on it, worked out by the add-on.
  function cardEncoderInfo(d) {
    const shown = d.status !== "revoked" && showsEncoder(d);
    const line = !shown ? null
      : d.encoder_mode === "auto" ? `Card encoder: ${d.encoder_text}`
      : d.encoder_mode === "local" ? `Card encoder: This computer (USB), ${linkWords(d)}` : null;
    return [line ? h("div", { class: "meta" }, line) : null,
      d.wristbands_note && (d.encoder_mode !== "local" || wristbandsOn())
        ? h("div", { class: "meta wb-note" }, d.wristbands_note) : null];
  }

  // " (SN Web Socket 2.1)" once SN Web Socket has said its version through the link, else "".
  const helperVersion = (link) => (link.helper_version
    ? ` (SN Web Socket ${String(link.helper_version).slice(0, 40)})` : "");

  // This computer (USB): the computer's browser link in a few words, after "This computer (USB), ".
  function linkWords(d) {
    const link = d.encoder_link || {};
    if (!link.link) {
      return `not connected${link.last_seen ? ` (last connected ${ago(link.last_seen).toLowerCase()})` : ""}`;
    }
    if (link.helper === "ready") return `connected${helperVersion(link)}`;
    if (link.helper === "blocked") return "blocked by the browser on it";
    if (link.helper === "unreachable") return "SN Web Socket isn't answering on it";
    return "Wristbands is open on it, checking SN Web Socket";
  }

  // This computer (USB): the computer's browser link in a sentence, and how it looks ("ok",
  // "warn" or ""). `where` names the computer ("this computer" in its own editor).
  function linkStatus(d, where = d.name) {
    const link = d.encoder_link || {};
    if (!link.link) {
      return [`Not connected: it connects while someone signed in on ${where} has Wristbands open` +
        `${link.last_seen ? ` (last connected ${ago(link.last_seen).toLowerCase()})` : ""}.`, ""];
    }
    if (link.helper === "ready") return [`Connected${helperVersion(link)}.`, "ok"];
    if (link.helper === "blocked") {
      return [`Wristbands is open on ${where}, but the browser there is blocking the encoder. Choose ` +
        "Allow when it asks, or allow Apps on device for the staff app (the icon at the left of the " +
        "address bar).", "warn"];
    }
    if (link.helper === "unreachable") {
      return [`Wristbands is open on ${where}, but SN Web Socket isn't answering there: is it running?`, "warn"];
    }
    return [`Wristbands is open on ${where}; checking SN Web Socket there…`, ""];
  }

  // Why no one can use a computer's encoder, or how it was when last used.
  function encoderNote(d) {
    if (d.wristbands_note) return h("div", { class: "via access-note" }, d.wristbands_note);
    if (!d.encoder_address) return h("div", { class: "via access-note" }, d.encoder_text || "");
    const s = state.wristbands?.encoders?.[d.encoder_address];
    if (s?.busy) return h("div", { class: "via" }, "Making cards now.");
    if (s?.error) return h("div", { class: "via access-note" }, `Problem: ${s.error}`);
    if (d.encoder_mode === "local") {
      const [text, look] = linkStatus(d);
      return h("div", { class: look === "ok" ? "via" : look ? "via access-note" : "entity-id" }, text);
    }
    if (s?.connected) {
      return h("div", { class: "via" }, `Working${s.checked_at ? `, last checked ${ago(s.checked_at).toLowerCase()}` : ""}.`);
    }
    return h("div", { class: "entity-id" }, "Not used since the add-on started. Click Test encoder to check it.");
  }

  // What Find encoders (or the add-on on its own) found last, in words.
  function foundText(disc) {
    if (!disc) return "";
    if (disc.error) return disc.error;
    const where = (disc.networks || []).join(", ");
    const found = disc.helpers || [];
    return found.length
      ? `Found ${plural(found.length, "encoder")} on ${where}: ${found.map((f) => f.ip).join(", ")}.`
      : `No encoder found on ${where}. Check that SN Web Socket is running on the front-desk computer.`;
  }

  // An Automatic encoder in the computer editor: the one it uses, or why it has none. It only
  // ever uses the one confirmed with Use this encoder (`saved`: confirmed and saved, not just
  // chosen in the editor), never one it found by itself.
  function autoStatus(disc, chosenIp, saved) {
    if (disc.scanning) return "Looking for encoders…";
    if (chosenIp && !saved) return `Uses the encoder at ${chosenIp} once you click Save.`;
    const found = disc.helpers || [];
    const where = (disc.networks || []).join(", ");
    const searched = disc.scanned_at ? `searched ${ago(disc.scanned_at).toLowerCase()}` : "";
    // The one in use still is after a search that didn't work: the add-on keeps what it found.
    if (chosenIp && found.some((f) => f.ip === chosenIp)) return `Uses the encoder at ${chosenIp} (${searched}).`;
    if (disc.error) return disc.error;
    if (!disc.scanned_at) {
      return chosenIp ? `Uses the encoder at ${chosenIp} once it's found. Not looked for since the add-on ` +
        "started: click Find encoders." : "Not looked for yet. Click Find encoders (it takes a few seconds).";
    }
    if (chosenIp) {
      return `The encoder you chose (${chosenIp}) wasn't found on ${where} (${searched}), so this computer ` +
        "has none for now. Check that SN Web Socket is running on it, then click Find encoders. If its " +
        "address changed, choose it again below: Staff Access never switches to another one by itself.";
    }
    if (!found.length) return `No encoder found on ${where} (${searched}). Check that SN Web Socket is running on this computer, then click Find encoders.`;
    return `Found ${plural(found.length, "encoder")} on ${where} (${searched}). Click Use this encoder on ` +
      "the one running on this computer: none is used until you do.";
  }

  // Automatic: each SN Web Socket helper found, with Use this encoder, or a badge on the one
  // chosen (listed even when the last search didn't find it).
  function helperRows(found, chosen, chosenWord, use) {
    const rows = found.map((f) => ({ ip: f.ip, version: f.version, missing: false }));
    if (chosen && !found.some((f) => f.ip === chosen)) rows.push({ ip: chosen, version: null, missing: true });
    return rows.map((f) => h("div", { class: "helper-row" },
      h("span", null, f.ip,
        f.version ? h("small", null, `SN Web Socket ${String(f.version).slice(0, 40)}`) : null,
        f.missing ? h("small", null, "not found in the last search") : null),
      f.ip === chosen ? h("span", { class: `badge ${f.missing ? "locked" : "active"}` }, chosenWord)
        : h("button", { type: "button", class: "btn", onclick: () => use(f) }, "Use this encoder")));
  }

  // Automatic only uses an encoder an admin confirmed: anything on the network could answer like
  // SN Web Socket, and the encoder is sent TT Hotel's key for it each time cards are made.
  function askUseHelper(name, helper) {
    return askConfirm(`Use the card encoder at ${helper.ip} for ${name}? Staff Access sends it the encoder ` +
      "key from TT Hotel each time cards are made there, so check that this is the address of the " +
      "computer the encoder is plugged into (in Windows: Settings → Network & internet).", "Use this encoder");
  }

  async function saveHelper(d, helper) {
    await api("PATCH", `api/devices/${d.id}`, { encoder_mode: "auto", encoder_auto_ip: helper.ip });
    await load();
  }

  async function findEncoders() {
    const r = await api("POST", "api/wristbands/encoders/find");
    await load();
    return r;
  }

  $("#wb-find-encoders").addEventListener("click", (e) => {
    const button = e.currentTarget;
    button.disabled = true;
    $("#wb-discovery").textContent = "Looking for encoders… this takes a few seconds.";
    run(async () => {
      try {
        toast(foundText((await findEncoders()).discovery));
      } finally {
        button.disabled = false;
        renderDiscovery();
      }
    });
  });

  function renderDiscovery() {
    const disc = state.wristbands?.discovery || {};
    $("#wb-discovery").textContent = disc.scanning ? "Looking for encoders…"
      : disc.scanned_at || disc.error
        ? `${disc.scanned_at ? `Last looked ${ago(disc.scanned_at).toLowerCase()}. ` : ""}${foundText(disc)}`
        : "Not looked for since the add-on started.";
  }

  function renderEncoderList() {
    renderDiscovery();
    const box = $("#wb-encoders");
    const computers = state.devices.filter((d) => d.encoder_mode !== "off" && d.status !== "revoked");
    if (!computers.length) {
      box.replaceChildren(h("p", { class: "hint" }, "No computer has a card encoder yet. On the front-desk " +
        "computer's card in Phones & computers, click Edit and set Card encoder to This computer (USB)."));
      return;
    }
    const found = state.wristbands?.discovery?.helpers || [];
    box.replaceChildren(...computers.map((d) => h("div", { class: "area-row" },
      h("div", { class: "entity-text" },
        h("div", { class: "entity-name" }, d.name),
        h("div", { class: "entity-id", title: d.encoder_address || "" },
          `${d.encoder_text} · port: ${d.encoder_port || "Automatic"}`),
        encoderNote(d),
        // Automatic with no encoder in use: the ones found, to confirm the one at this desk.
        d.encoder_mode === "auto" && !d.encoder_address && found.length
          ? h("div", { class: "helper-list" }, helperRows(found, d.encoder_auto_ip, "Chosen", (f) =>
            run(async () => {
              if (!await askUseHelper(d.name, f)) return false;
              await saveHelper(d, f);
            }, `Saved: ${d.name} uses the card encoder at ${f.ip}.`))) : null),
      h("div", { class: "area-actions" },
        h("button", { type: "button", class: "btn", onclick: () => testEncoder(d) }, "Test encoder"),
        h("button", { type: "button", class: "btn", onclick: () => editPhone(d) }, "Edit")))));
  }

  function testEncoder(d) {
    return run(async () => {
      try {
        const r = await api("POST", `api/devices/${d.id}/encoder-test`);
        toast(`The encoder on ${d.name} answered${r.version ? ` (version ${String(r.version).slice(0, 60)})` : ""}.`);
      } finally {
        await load();
      }
    });
  }

  // ------------------------------------------------ encoder links (This computer (USB))
  // Such an encoder connects only while Wristbands is open on its computer, so while one is on
  // screen (Phones & computers, Wristbands, or its editor) the dashboard asks the add-on again
  // every so often and redraws just the encoder parts, never the rest of the page.

  const LINK_CHECK_MS = 15000;          // often enough to see someone open Wristbands at the desk
  const LINK_CHECK_EDITING_MS = 4000;   // its editor is open: someone is probably setting it up
  // What says how a computer's encoder is now (all worked out by the add-on).
  const LINK_FIELDS = ["encoder_text", "encoder_address", "encoder_link", "wristbands_note"];
  let linkTimer = null;
  let linkChecking = false;

  function linkCheckWanted() {
    if (document.visibilityState !== "visible" || !wristbandsOn()) return false;
    if (!state.devices.some((d) => d.encoder_mode === "local" && d.status !== "revoked")) return false;
    return !!linkWatcher || !$("#tab-phones").hidden || !$("#tab-wristbands").hidden;
  }

  // How the encoders are now, leaving out what changes by itself (when last seen or checked).
  function encoderSignature() {
    const strip = (o) => (o && typeof o === "object" ? { ...o, last_seen: null, checked_at: null } : o);
    return JSON.stringify([state.devices.map((d) => [d.id, ...LINK_FIELDS.map((k) => strip(d[k]))]),
      Object.entries(state.wristbands?.encoders || {}).map(([url, s]) => [url, strip(s)]),
      state.wristbands?.discovery || null]);
  }

  // Each computer card's encoder badge and lines, drawn again in place.
  function redrawEncoderSlots() {
    const byId = new Map(state.devices.map((d) => [String(d.id), d]));
    document.querySelectorAll("[data-encoder-badge]").forEach((slot) => {
      const d = byId.get(slot.dataset.encoderBadge);
      if (d) fill(slot, cardEncoderBadge(d));
    });
    document.querySelectorAll("[data-encoder-info]").forEach((slot) => {
      const d = byId.get(slot.dataset.encoderInfo);
      if (d) fill(slot, cardEncoderInfo(d));
    });
  }

  function scheduleLinkCheck(ms) {
    clearTimeout(linkTimer);
    linkTimer = setTimeout(checkLinks, ms);
  }

  // Never throws: a check that fails is just tried again later.
  async function checkLinks() {
    if (linkChecking) return;   // the check under way schedules the next
    linkChecking = true;
    try {
      if (linkCheckWanted()) {
        const data = await api("GET", "api/state");
        const before = encoderSignature();
        const fresh = new Map((data.devices || []).map((x) => [x.id, x]));
        for (const d of state.devices) {
          const now = fresh.get(d.id);
          if (now) for (const key of LINK_FIELDS) d[key] = now[key];
        }
        state.wristbands = { ...state.wristbands, encoders: data.wristbands?.encoders || {},
          discovery: data.wristbands?.discovery || {} };
        const changed = encoderSignature() !== before;
        redrawEncoderSlots();
        if (changed) renderEncoderList();
        linkWatcher?.redraw(changed);
      }
    } catch { /* the add-on didn't answer: the next check tries again */ }
    linkChecking = false;
    let wait = LINK_CHECK_MS;
    try { if (linkWatcher?.fast()) wait = LINK_CHECK_EDITING_MS; } catch { /* the usual wait */ }
    scheduleLinkCheck(wait);
  }

  // ------------------------------------------------ doors

  function renderDoorStatus() {
    const status = $("#wb-doors-status");
    if (!wbDoors) {
      status.textContent = "";
      return;
    }
    const active = wbDoors.filter((d) => d.active);
    const hidden = active.filter((d) => d.hidden).length;
    const synced = state.wristbands?.synced_at;
    status.textContent = !wbDoors.length ? "No doors yet."
      : `${plural(active.length, "door")}${hidden ? `, ${hidden} hidden from staff` : ""}.` +
        (synced ? ` Synced ${ago(synced).toLowerCase()}.` : "");
  }

  function renderDoors() {
    const box = $("#wb-doors");
    renderDoorStatus();
    if (!wbDoors) {
      box.replaceChildren(doorsPending());
      return;
    }
    if (!wbDoors.length) {
      box.replaceChildren(h("p", { class: "hint" }, "Turn on wristbands above and save your TT Hotel " +
        "details in Settings → Connections, then click Sync from TT Hotel."));
      return;
    }
    const rows = [];
    for (const b of doorGroups(wbDoors)) {
      for (const f of b.floors) {
        rows.push(h("tr", { class: "wb-group" }, h("th", { colspan: "6" }, `${b.name} · ${f.name}`)));
        rows.push(...f.doors.map(doorRow));
      }
    }
    box.replaceChildren(h("p", { id: "wb-kinds-status" }), markDoorsTool(),
      h("div", { class: "table-wrap wb-door-wrap" }, h("table", { class: "activity wb-door-table" },
        h("thead", null, h("tr", null, h("th", null, "Door"), h("th", null, "Kind"), h("th", null, "Battery"),
          h("th", null, "Gateway"), h("th", null, "Status"), h("th", null, "Staff see it"))),
        h("tbody", null, rows))));
    renderKindStatus();
  }

  // ------------------------------------------------ door kinds: public doors and guest rooms

  const DOOR_KINDS = [["", "Not marked"], ["public", "Public door"], ["room", "Guest room"], ["staff", "Staff door"]];
  let markChoice = { where: "", kind: "room" };   // the floor or building and kind last marked

  // How many doors aren't marked yet (they show as both rooms and shared doors until they are),
  // and how many there are of each kind.
  function renderKindStatus() {
    const status = $("#wb-kinds-status");
    if (!status || !wbDoors) return;
    const active = wbDoors.filter((d) => d.active);
    const open = active.filter((d) => !d.kind).length;
    const counted = (kind) => plural(active.filter((d) => d.kind === kind).length, KIND_WORDS[kind]);
    const counts = `${counted("public")}, ${counted("room")} and ${counted("staff")}`;
    status.className = open ? "wb-warn" : "hint";
    status.textContent = !active.length ? ""
      : open ? `${plural(open, "door")} ${open === 1 ? "isn't" : "aren't"} marked yet. Until ${open === 1 ? "it is" : "they are"}, ` +
        `staff see ${open === 1 ? "it" : "them"} both as a guest room and as a shared door. Marked so far: ${counts}.`
      : `Every door is marked: ${counts}.`;
  }

  // Marks a whole floor or building at once.
  function markDoorsTool() {
    const targets = new Map();
    const where = h("select", { "aria-label": "Floor or building" });
    for (const b of doorGroups(wbDoors)) {
      const options = [];
      const all = b.floors.flatMap((f) => f.doors);
      if (b.number != null && b.floors.length > 1) {
        targets.set(`b${b.number}`, { label: b.name, doors: all });
        options.push(h("option", { value: `b${b.number}` }, `Whole building (${plural(all.length, "door")})`));
      }
      b.floors.forEach((f, i) => {
        const key = `f${b.number ?? "-"}:${f.number ?? "-"}:${i}`;
        const whole = b.number != null && b.floors.length === 1;   // a building with one floor
        targets.set(key, { label: whole ? b.name : `${b.name} · ${f.name}`, doors: f.doors });
        options.push(h("option", { value: key }, `${whole ? "Whole building" : f.name} (${plural(f.doors.length, "door")})`));
      });
      where.append(h("optgroup", { label: b.name }, options));
    }
    if (targets.has(markChoice.where)) where.value = markChoice.where;
    const kind = h("select", { "aria-label": "Mark them as" },
      DOOR_KINDS.map(([value, label]) => h("option", { value }, value ? `${label}s` : label)));
    kind.value = markChoice.kind;
    const go = h("button", { type: "button", class: "btn" }, "Mark");
    go.addEventListener("click", () => run(async () => {
      const target = targets.get(where.value);
      if (!target) return;
      const value = kind.value || null;
      markChoice = { where: where.value, kind: kind.value };
      // Doors already marked the other way are only changed once the owner says so, and so are
      // shared doors about to become staff doors (that takes them off day passes and overnight
      // wristbands).
      const other = target.doors.filter((d) => d.kind && d.kind !== value);
      const shared = value === "staff" ? sharedDoorsOf(target.doors.filter((d) => d.kind !== "staff")) : [];
      const doors = plural(target.doors.length, "door");
      const parts = [];
      if (other.length) {
        parts.push(`${plural(other.length, "door")} on ${target.label} ${other.length === 1 ? "is" : "are"} marked another way now.`);
      }
      if (shared.length) {
        parts.push(`${listNames(shared.map(doorName))} ${shared.length === 1 ? "is one of" : "are among"} ` +
          `${sharedPlaces(shared)}. Staff doors never go on day passes or overnight wristbands, so ` +
          `${shared.length === 1 ? "it's" : "they're"} taken off ${shared.length === 1 ? "that list" : "those lists"}.`);
      }
      const question = value
        ? `${parts.join(" ")} Mark all ${doors} as ${KIND_PLURALS[value]}?`
        : `Unmark all ${doors} on ${target.label}? Staff then see them both as guest rooms and as shared doors.`;
      if ((other.length || shared.length) && !(await askConfirm(question, value ? "Mark them" : "Unmark them"))) return;
      const reply = await api("POST", "api/wristbands/doors/kind", { door_ids: target.doors.map((d) => d.id), kind: value });
      for (const d of target.doors) d.kind = value;
      applyUnshared(reply.unshared, value === "staff" ? target.doors : []);
      renderDoors();
      renderDefaultDoors();
      $("#wb-doors .wb-mark button")?.focus();
      toast(value ? `${target.label}: ${doors} marked as ${KIND_PLURALS[value]}.` +
        (shared.length ? ` ${listNames(shared.map(doorName))} ${shared.length === 1 ? "is" : "are"} no longer on day passes or overnight wristbands.` : "")
        : `${target.label}: ${doors} unmarked.`);
    }));
    return h("div", { class: "wb-mark" }, h("span", null, "Mark a whole floor or building:"), where,
      h("span", null, "as"), kind, go);
  }

  // A door's kind, saved as soon as it's changed.
  function doorKindPicker(d) {
    const label = doorName(d);
    const pick = h("select", { "aria-label": `Kind of door for ${label}`, class: d.kind ? null : "wb-unmarked" },
      DOOR_KINDS.map(([value, text]) => h("option", { value }, text)));
    pick.value = d.kind || "";
    pick.addEventListener("change", () => run(async () => {
      const value = pick.value || null;
      // A shared door made a staff door comes off day passes and overnight wristbands: ask first.
      const places = value === "staff" && d.kind !== "staff" ? sharedPlaces([d]) : "";
      if (places && !(await askConfirm(`${doorName(d)} is one of ${places}. Staff doors never go on day passes ` +
        "or overnight wristbands, so it's taken off. Mark it as a staff door?", "Mark it"))) {
        pick.value = d.kind || "";
        return;
      }
      let reply;
      try {
        reply = await api("PATCH", `api/wristbands/doors/${d.id}`, { kind: value });
      } catch (err) {
        pick.value = d.kind || "";
        throw err;
      }
      d.kind = value;
      applyUnshared(reply.unshared, value === "staff" ? [d] : []);
      pick.classList.toggle("wb-unmarked", !value);
      renderKindStatus();
      renderDefaultDoors();
      const shared = ["wb_daypass_doors", "wb_overnight_doors"].some((key) => wbPicks[key].has(d.id));
      toast(value === "staff" && places ? `${doorName(d)} is a staff door now, and no longer on day passes ` +
        "or overnight wristbands: only staff cards and contractor badges open it."
        : value && value !== "public" && shared ? `${doorName(d)} is a ${KIND_WORDS[value]} now, but it's still ` +
        "one of the shared doors above: untick it there."
        : value === "staff" ? `${doorName(d)} is a staff door now: only staff cards and contractor badges open it.`
        : value ? `${doorName(d)} is a ${KIND_WORDS[value]} now.`
        : `${doorName(d)} isn't marked: staff see it both as a room and as a shared door.`);
    }));
    return pick;
  }

  // One door: its name for staff (blank: TT Hotel's) and whether staff see it. Each change is
  // saved straight away.
  function doorRow(d) {
    const ttName = d.alias || d.tt_name || `Door ${d.id}`;
    const name = h("input", { value: d.name, placeholder: ttName, maxlength: "60", "aria-label": `Name for ${ttName}` });
    name.addEventListener("change", () => run(async () => {
      const value = name.value.trim();
      try {
        await api("PATCH", `api/wristbands/doors/${d.id}`, { name: value });
      } catch (err) {
        name.value = d.name;
        throw err;
      }
      d.name = value;
      renderDefaultDoors();
      toast(value ? `Staff now see it as ${value}.` : `Staff now see TT Hotel's name, ${ttName}.`);
    }));
    const shown = h("input", { type: "checkbox", checked: !d.hidden });
    const row = h("tr", { class: [d.active ? "" : "wb-gone", d.hidden ? "wb-hidden" : ""].join(" ").trim() || null },
      h("td", null, name, h("div", { class: "sub" },
        `TT Hotel: ${d.tt_name || "no name"}${d.alias && d.alias !== d.tt_name ? ` (${d.alias})` : ""}` +
        ` · ID ${d.id}${d.mac_last4 ? ` · MAC …${d.mac_last4}` : ""}`)),
      h("td", null, doorKindPicker(d)),
      h("td", { class: d.battery != null && d.battery <= 20 ? "nowrap wb-low" : "nowrap" },
        d.battery == null ? "—" : `${d.battery}%`),
      h("td", null, d.has_gateway ? "Yes" : "No"),
      h("td", null, d.active ? "Active" : "Removed from TT Hotel"),
      h("td", null, h("label", { class: "check" }, shown, "Show")));
    shown.addEventListener("change", () => run(async () => {
      try {
        await api("PATCH", `api/wristbands/doors/${d.id}`, { hidden: !shown.checked });
      } catch (err) {
        shown.checked = !d.hidden;
        throw err;
      }
      d.hidden = !shown.checked;
      row.classList.toggle("wb-hidden", d.hidden);
      renderDoorStatus();
      renderDefaultDoors();
      toast(d.hidden ? `${doorName(d)} is hidden from staff.` : `Staff see ${doorName(d)} again.`);
    }));
    return row;
  }

  // ------------------------------------------------ cards made

  async function loadWristbandJobs(reset) {
    const rows = $("#wb-job-rows");
    if (reset) { oldestJob = null; rows.replaceChildren(); }
    await run(async () => {
      const q = oldestJob ? `?before=${oldestJob}` : "";
      const { jobs } = await api("GET", `api/wristbands/jobs${q}`);
      if (reset && !jobs.length) {
        rows.replaceChildren(h("tr", null, h("td", { colspan: "5", class: "empty" }, "No cards made yet.")));
      }
      rows.append(...jobs.map(jobRow));
      if (jobs.length) oldestJob = jobs[jobs.length - 1].created_at;
      $("#wb-more-jobs").hidden = jobs.length < WB_JOB_PAGE;
    });
  }

  function jobRow(j) {
    const made = (j.cards || []).filter((c) => c.status === "written");
    const numbers = made.map((c) => c.card_number).filter(Boolean);
    const shown = numbers.slice(0, WB_SHOWN_NUMBERS).join(", ") +
      (numbers.length > WB_SHOWN_NUMBERS ? ` +${numbers.length - WB_SHOWN_NUMBERS} more` : "");
    const noun = WB_NOUNS[j.type] || "wristband";
    const unfinished = j.status === "needs_attention" || (j.status === "cancelled" && made.length < j.count);
    return h("tr", null,
      h("td", { class: "nowrap" }, when(j.created_at)),
      h("td", null, j.made_by),
      h("td", null, j.name, h("div", { class: "sub" }, `${WB_TYPE_NAMES[j.type] || j.type} · ${j.summary}`)),
      h("td", null, `${made.length} of ${plural(j.count, noun)}`,
        h("div", { class: "sub" }, [WB_STATUS[j.status] || j.status, numbers.length ? `No. ${shown}` : ""]
          .filter(Boolean).join(" · ")),
        unfinished && j.message ? h("div", { class: "sub wb-job-note" }, j.message) : null),
      h("td", { class: "nowrap" }, j.expires_label || when(j.expires_at)));
  }

  $("#wb-refresh-jobs").addEventListener("click", () => loadWristbandJobs(true));
  $("#wb-more-jobs").addEventListener("click", () => loadWristbandJobs(false));

  // ------------------------------------------------ door activity

  // TT Hotel sends lock records for doors on a gateway to the address shown here (its Callback
  // URL). Each key card's Last used comes from them, and a lost or revoked card tried at a door
  // is marked.

  const DA_ALERTS = { lost: "Lost card", revoked: "Revoked card", deleted: "Deleted card",
    archived: "Archived person's card" };
  const da = { door: "", problems: false, oldest: null, seq: 0, data: null };

  // A key card's Last used: the door, then when (and "refused" if it didn't open); "—" when it
  // hasn't been seen at a door.
  function lastUsed(used) {
    if (!used) return h("span", { class: "sub" }, "—");
    return [h("span", { class: used.ok ? null : "da-refused" }, used.door),
      h("div", { class: "sub" }, `${when(used.at)}${used.ok ? "" : " · refused"}`)];
  }

  function openDoorActivity() {
    loadDoors().then(renderDoorFilter);
    loadDoorActivity(true);
  }

  function renderDoorFilter() {
    const select = $("#da-door");
    const doors = (wbDoors || []).filter((d) => d.active || String(d.id) === da.door)
      .sort((a, b) => doorName(a).localeCompare(doorName(b), undefined, { numeric: true }));
    select.replaceChildren(h("option", { value: "" }, "All doors"),
      ...doors.map((d) => h("option", { value: String(d.id) }, doorName(d))));
    select.value = da.door;
  }

  async function loadDoorActivity(reset) {
    const rows = $("#da-rows");
    const seq = ++da.seq;
    const params = new URLSearchParams();
    if (da.door) params.set("door", da.door);
    if (da.problems) params.set("problems", "1");
    if (!reset && da.oldest) params.set("before", String(da.oldest));
    let data;
    try {
      data = await api("GET", `api/door-activity?${params}`);
    } catch (err) {
      if (seq !== da.seq) return;
      if (reset) rows.replaceChildren(h("tr", null, h("td", { colspan: "5", class: "empty" }, err.message)));
      else toast(err.message, true);
      return;
    }
    if (seq !== da.seq) return;   // a newer filter was chosen meanwhile
    da.data = data;
    renderDoorConnection();
    const records = data.records || [];
    if (reset) {
      da.oldest = null;
      rows.replaceChildren();
      if (!records.length) {
        rows.append(h("tr", null, h("td", { colspan: "5", class: "empty" },
          da.door || da.problems ? "Nothing matches." : "No door activity yet.")));
      }
    }
    rows.append(...records.map(doorRecordRow));
    if (records.length) da.oldest = records[records.length - 1].id;
    $("#da-more").hidden = !data.more;
  }

  // The address to paste into TT Hotel, and how deliveries have gone.
  function renderDoorConnection() {
    const data = da.data;
    $("#da-address-row").hidden = !data.address;
    $("#da-address").value = data.address || "";
    $("#da-alert-ha").checked = !!data.alert_ha;
    const lines = [];
    if (!data.address) lines.push("Set the staff app address in Settings first: TT Hotel needs it to reach Staff Access.");
    lines.push(data.last_record ? `Last record arrived ${ago(data.last_record).toLowerCase()}.`
      : "No records yet. Once TT Hotel has the address, unlocks at doors with a gateway show up here as they happen.");
    const got = data.delivery || {};
    if (got.at && !got.known) {
      lines.push(`TT Hotel's last records were for a door Staff Access doesn't know (lock ${got.lock_id}): ` +
        "click Sync from TT Hotel above.");
    } else if (got.at && got.dropped) {
      lines.push(`${plural(got.dropped, "record")} in TT Hotel's last delivery couldn't be read and ${got.dropped === 1 ? "was" : "were"} left out.`);
    }
    $("#da-status").replaceChildren(...lines.flatMap((t, i) => (i ? [h("br"), t] : [t])));
  }

  function doorRecordRow(r) {
    const who = r.who || (r.card_number ? `Card ${r.card_number}` : "—");
    const result = r.alarm ? ["Alarm", "pending"] : r.ok ? ["Opened", "active"] : ["Refused", "revoked"];
    return h("tr", { class: r.alert ? "da-alert" : null },
      h("td", { class: "nowrap" }, when(r.at)),
      h("td", null, r.door),
      h("td", null, who,
        r.card_number && r.who ? h("div", { class: "sub" },
          `${r.card_type === "contractor" ? "Contractor badge · " : ""}Card ${r.card_number}`) : null,
        r.alert ? h("div", { class: "sub da-alert-note" }, DA_ALERTS[r.alert] || "Card not in use") : null),
      h("td", null, r.how),
      h("td", null, h("span", { class: `badge ${result[1]}` }, result[0])));
  }

  $("#da-refresh").addEventListener("click", () => loadDoorActivity(true));
  $("#da-more").addEventListener("click", () => loadDoorActivity(false));
  $("#da-door").addEventListener("change", (e) => {
    da.door = e.target.value;
    loadDoorActivity(true);
  });
  $("#da-problems").addEventListener("change", (e) => {
    da.problems = e.target.checked;
    loadDoorActivity(true);
  });
  $("#da-address").addEventListener("focus", (e) => e.target.select());
  $("#da-copy").addEventListener("click", async () => {
    const field = $("#da-address");
    field.select();
    // The clipboard API can be blocked inside Home Assistant's frame; fall back to the old way.
    try { await navigator.clipboard.writeText(field.value); } catch { document.execCommand("copy"); }
    toast("Address copied. Paste it into TT Hotel's Callback URL.");
  });
  $("#da-alert-ha").addEventListener("change", (e) => run(async () => {
    const on = e.target.checked;
    try {
      await api("PUT", "api/settings", { wb_lost_card_alert: on ? "1" : "0" });
    } catch (err) {
      e.target.checked = !on;
      throw err;
    }
    state.settings.wb_lost_card_alert = on ? "1" : "0";
    toast(on ? "Home Assistant will be told when a lost or revoked card is tried."
      : "Lost or revoked cards tried at a door are only noted in Activity now.");
  }));
  $("#da-new").addEventListener("click", () => run(async () => {
    if (!await askConfirm("Make a new address? The old one stops working at once: paste the new one " +
      "into TT Hotel's Callback URL straight away, or no door activity arrives.", "Make new address")) return false;
    await api("POST", "api/door-activity/address", {});
    await loadDoorActivity(true);
  }, "New address made. Paste it into TT Hotel's Callback URL."));

  // ---------------------------------------------------------------- staff tab: departments

  // A department gives its people a role and the key card Make key card makes for them; with
  // Add everyone from Connecteam on, people join the one whose smart group they're in (the first
  // in this order, if they're in several).

  let smartGroups = null;   // Connecteam's smart groups ({id, name, users}), once the dialog loaded them

  // replaceChildren, leaving out the parts that aren't shown (null or false).
  const fill = (el, ...kids) => el.replaceChildren(...kids.flat().filter((k) => k != null && k !== false));

  const people = (n) => `${n} ${n === 1 ? "person" : "people"}`;
  const groupName = (id) => smartGroups?.find((g) => g.id === id)?.name
    || state.departments.find((d) => d.smart_group_id === id)?.smart_group_name || "a Connecteam smart group";

  // A key card template ("access" entries) in a few words: "every door", "Building 1, Floor 2"...
  function accessSummary(entries) {
    if (!entries?.length) return "nothing chosen";
    if (entries.some((e) => e.level === "all")) return "every door";
    const sample = (pick) => wbDoors?.find(pick);
    const name = (e) => {
      if (e.level === "door") {
        const d = sample((x) => x.id === e.door);
        return d ? doorName(d) : `Door ${e.door}`;
      }
      const d = sample((x) => x.building === e.building && (e.level === "building" || x.floor === e.floor));
      const b = d ? buildingName(d) : `Building ${e.building}`;
      return e.level === "building" ? b : `${b}, ${d ? floorName(d) : `Floor ${e.floor}`}`;
    };
    if (entries.length <= 3) return entries.map(name).join("; ");
    const n = (level) => entries.filter((e) => e.level === level).length;
    return [n("building") && plural(n("building"), "building"), n("floor") && plural(n("floor"), "floor"),
      n("door") && plural(n("door"), "door")].filter(Boolean).join(", ");
  }

  function departmentSummary(d) {
    const role = state.roles.find((r) => r.id === d.role_id);
    return [people(d.staff_count || 0),
      d.smart_group_id ? `Connecteam: ${groupName(d.smart_group_id)}` : "Added by hand",
      role ? `Role: ${role.name}` : "Role left alone",
      d.card_access == null ? "No key card"
        : `Key card: ${accessSummary(d.card_access)}, ${plural(d.card_days, "day")}` +
          (d.card_lockout ? ", opens deadbolted doors" : ""),
      d.clock_in == null ? (state.connecteam?.enabled ? "Clock-in: each person's own" : null)
        : d.clock_in ? "Clock-in: must be clocked in" : "Clock-in: not needed"].filter(Boolean).join(" · ");
  }

  function renderDepartments() {
    const list = $("#department-list");
    if (!list) return;
    if (!state.departments.length) {
      list.replaceChildren(h("div", { class: "empty small" }, "No departments yet. Add one like " +
        "“Housekeeping” with the doors its key cards open."));
      return;
    }
    list.replaceChildren(...state.departments.map((d) => h("div", { class: "area-row sort-row" },
      dragHandle(`Reorder department ${d.name}`),
      h("div", { class: "entity-text" },
        h("div", { class: "entity-name" }, d.name),
        h("div", { class: "entity-id dept-sub" }, departmentSummary(d)),
        d.card_access && !d.card_access.length
          ? h("div", { class: "via access-note" }, "Its key card is damaged: choose its doors again.") : null),
      h("div", { class: "area-actions" },
        h("button", { type: "button", class: "btn", onclick: () => run(() => editDepartment(d)) }, "Edit"),
        h("button", { type: "button", class: "btn danger", onclick: () => run(async () => {
          const who = d.staff_count ? ` Its ${people(d.staff_count)} stay, with no department.` : "";
          if (!await askConfirm(`Delete the ${d.name} department?${who}`, "Delete department")) return false;
          await api("DELETE", `api/departments/${d.id}`);
          await load();
        }, "Department deleted.") }, "Delete")))));
  }

  // Saved straight away, one move at a time, like the areas list.
  let departmentSaves = Promise.resolve();
  sortable($("#department-list"), "departments", (from, to) => {
    moveItem(state.departments, from, to);
    renderDepartments();
    const order = state.departments.map((d) => d.id);
    departmentSaves = departmentSaves.then(() => run(async () => {
      try {
        await api("PUT", "api/departments/order", { order });
      } finally {
        await load();
      }
    }));
  });

  $("#add-department").addEventListener("click", () => run(() => editDepartment(null)));

  async function editDepartment(d) {
    const dialog = $("#dept-dialog");
    const form = $("#dept-form");
    const error = $("[data-error]", dialog);
    form.reset();
    error.textContent = "";
    $("[data-title]", dialog).textContent = d ? `Edit ${d.name}` : "Add department";
    form.elements["name"].value = d ? d.name : "";
    fillRoleOptions(form.elements["role_id"], d ? d.role_id : null, "Leave their role as it is");
    form.elements["clock_in"].value = d?.clock_in == null ? "" : String(Number(d.clock_in));

    // Smart groups come from Connecteam (cached by the add-on for hours), only when asked for.
    const groupSelect = form.elements["smart_group_id"];
    const groupHint = $("[data-group-hint]", dialog);
    const fillGroups = () => {
      const taken = new Map(state.departments.filter((x) => x.smart_group_id && x.id !== d?.id)
        .map((x) => [x.smart_group_id, x.name]));
      const known = smartGroups || [];
      const options = [h("option", { value: "" }, "None: add people by hand")];
      if (d?.smart_group_id && !known.some((g) => g.id === d.smart_group_id)) {
        options.push(h("option", { value: d.smart_group_id, selected: true }, groupName(d.smart_group_id)));
      }
      for (const g of known) {
        const users = g.users == null ? "" : ` (${people(g.users)})`;
        const used = taken.get(g.id);
        options.push(h("option", { value: g.id, selected: g.id === d?.smart_group_id, disabled: !!used },
          used ? `${g.name}: ${used} follows it` : `${g.name}${users}`));
      }
      groupSelect.replaceChildren(...options);
    };
    fillGroups();
    if (!state.connecteam?.enabled) {
      groupHint.textContent = "Turn on Connecteam in Settings to choose one.";
      groupSelect.disabled = !d?.smart_group_id;
    } else {
      groupSelect.disabled = false;
      groupHint.textContent = smartGroups ? "People in it join this department." : "Loading smart groups…";
      if (!smartGroups) {
        api("GET", "api/connecteam/smart-groups").then((r) => {
          smartGroups = r.groups || [];
          const picked = groupSelect.value;
          fillGroups();
          groupSelect.value = picked;
          groupHint.textContent = smartGroups.length ? "People in it join this department."
            : "Connecteam has no smart groups yet.";
          renderDepartments();
        }, (err) => { groupHint.textContent = `Couldn't load them: ${err.message}`; });
      }
    }

    // The key card template: what Make key card puts on their card.
    const hasCard = form.elements["has_card"];
    const cardBox = $("[data-card-box]", dialog);
    const accessBox = $("[data-access]", dialog);
    let access = clone(d?.card_access || []);
    hasCard.checked = d ? d.card_access != null : false;
    form.elements["card_days"].value = String(d ? d.card_days : 30);
    form.elements["card_lockout"].checked = d ? !!d.card_lockout : true;
    const syncCard = () => { cardBox.hidden = !hasCard.checked; };
    hasCard.onchange = syncCard;
    syncCard();
    const drawAccess = () => {
      if (!wristbandsOn()) {
        accessBox.replaceChildren(h("p", { class: "hint" }, "Turn on wristbands on the ",
          h("button", { type: "button", class: "linkish", onclick: () => { dialog.close(); selectTab("wristbands"); } },
            "Wristbands"), " tab to choose doors." + (access.length ? ` Saved now: ${accessSummary(access)}.` : "")));
        return;
      }
      if (!wbDoors) {
        accessBox.replaceChildren(doorsPending());
        loadDoors().then(() => { if (dialog.open && wbDoors) drawAccess(); });
        return;
      }
      accessBox.replaceChildren(cardAccessPicker(access, (value) => { access = value; }));
    };
    drawAccess();

    form.onsubmit = (ev) => {
      if (ev.submitter?.value !== "save") return;
      ev.preventDefault();
      const name = form.elements["name"].value.trim();
      const days = Math.floor(Number(form.elements["card_days"].value));
      const problem = !name ? "Give the department a name."
        : hasCard.checked && !access.length ? "Choose what the key card opens, or untick Make key cards."
        : hasCard.checked && !(days >= 1 && days <= WB_MAX_LIMIT) ? `A key card lasts 1 to ${WB_MAX_LIMIT} days.`
        : "";
      error.textContent = problem;
      if (problem) return;
      const body = { name, smart_group_id: groupSelect.value || null,
        role_id: form.elements["role_id"].value ? Number(form.elements["role_id"].value) : null,
        clock_in: form.elements["clock_in"].value === "" ? null : form.elements["clock_in"].value === "1",
        card_access: hasCard.checked ? access : null };
      if (hasCard.checked) {
        body.card_days = days;
        body.card_lockout = form.elements["card_lockout"].checked;
      }
      const save = $("button[value=save]", dialog);
      save.disabled = true;
      (async () => {
        try {
          if (d) await api("PUT", `api/departments/${d.id}`, body);
          else await api("POST", "api/departments", body);
          dialog.close("saved");
          await load();
          toast(d ? "Department saved." : `${name} added.`);
        } catch (err) {
          error.textContent = err.message;
        } finally {
          save.disabled = false;
        }
      })();
    };
    dialog.showModal();
    return false;
  }

  // What a key card opens: every door, or whole buildings, whole floors and single doors (as a
  // staff card request's "access"). Ticking a building or floor covers what's in it, including
  // doors added there later. Calls onChange with fresh entries after every change.
  function cardAccessPicker(entries, onChange) {
    const scope = { all: false, buildings: new Set(), floors: new Set(), doors: new Set() };
    for (const e of entries || []) {
      if (e.level === "all") scope.all = true;
      else if (e.level === "building") scope.buildings.add(e.building);
      else if (e.level === "floor") scope.floors.add(`${e.building}:${e.floor}`);
      else if (e.level === "door") scope.doors.add(e.door);
    }
    const toggle = (set, key, on) => (on ? set.add(key) : set.delete(key));
    const value = () => {
      if (scope.all) return [{ level: "all" }];
      const out = [...scope.buildings].sort((a, b) => a - b).map((b) => ({ level: "building", building: b }));
      for (const key of scope.floors) {
        const [b, f] = key.split(":").map(Number);
        if (!scope.buildings.has(b)) out.push({ level: "floor", building: b, floor: f });
      }
      const known = new Map((wbDoors || []).map((d) => [d.id, d]));
      for (const id of scope.doors) {
        const d = known.get(id);
        if (d && d.building != null && (scope.buildings.has(d.building) ||
          (d.floor != null && scope.floors.has(`${d.building}:${d.floor}`)))) continue;
        out.push({ level: "door", door: id });
      }
      return out;
    };
    const count = h("small", { class: "wb-count" });
    const emit = () => {
      const v = value();
      count.textContent = v.length ? accessSummary(v) : "nothing chosen yet";
      onChange(v);
    };
    const tree = h("div", { class: "wb-tree-box" });
    const all = h("input", { type: "checkbox", checked: scope.all, onchange: () => {
      scope.all = all.checked;
      tree.hidden = scope.all;
      emit();
    } });
    tree.hidden = scope.all;
    const check = (checked, label, onchange, note) => h("label", { class: "check" },
      h("input", { type: "checkbox", checked, onchange }),
      note ? h("span", null, label, h("small", { class: "wb-count" }, note)) : label);
    const treeRow = (picked, label, n, onPick) => {
      const box = h("input", { type: "checkbox", checked: picked });
      const small = h("small", { class: "wb-count" });
      const describe = () => {
        small.textContent = box.checked ? ` · all ${plural(n, "door")}, and ones added later` : ` · ${plural(n, "door")}`;
      };
      box.addEventListener("change", () => { describe(); onPick(box.checked); emit(); });
      describe();
      return h("label", { class: "check" }, box, h("span", null, label, small));
    };
    const active = (wbDoors || []).filter((d) => d.active);
    if (!active.length) {
      tree.replaceChildren(h("p", { class: "hint" }, "No doors yet. Sync them from TT Hotel on the Wristbands tab."));
    } else {
      const doorItem = (d) => h("li", null, check(scope.doors.has(d.id), doorName(d), (ev) => {
        toggle(scope.doors, d.id, ev.target.checked);
        emit();
      }, d.has_gateway ? " · gateway" : null));
      tree.replaceChildren(h("ul", { class: "wb-tree" }, doorGroups(active).map((b) => {
        const floors = h("ul", { class: "wb-tree" }, b.floors.map((f) => {
          const inside = h("ul", { class: "wb-tree" }, f.doors.map(doorItem));
          if (b.number == null || f.number == null) return h("li", null, h("div", { class: "wb-tree-head" }, f.name), inside);
          const key = `${b.number}:${f.number}`;
          inside.hidden = scope.floors.has(key);
          return h("li", null, treeRow(scope.floors.has(key), f.name, f.doors.length, (on) => {
            toggle(scope.floors, key, on);
            inside.hidden = on;
          }), inside);
        }));
        if (b.number == null) return h("li", null, h("div", { class: "wb-tree-head" }, b.name), floors);
        floors.hidden = scope.buildings.has(b.number);
        return h("li", null, treeRow(scope.buildings.has(b.number), b.name, b.count, (on) => {
          toggle(scope.buildings, b.number, on);
          floors.hidden = on;
        }), floors);
      })));
    }
    emit();
    return h("div", { class: "wb-pick dept-access" },
      h("label", { class: "check" }, all, h("span", null, "Every door, including ones added later (a master card)")),
      tree, h("p", { class: "hint dept-pick-sum" }, "Opens: ", count));
  }

  // ---------------------------------------------------------------- staff tab: Connecteam sync

  function syncResultText(r) {
    if (!r) return "";
    const parts = [r.added && `${r.added} added`, r.updated && `${r.updated} updated`,
      r.turned_off && `${r.turned_off} turned off`, r.turned_on && `${r.turned_on} turned on`,
      r.no_code && `${r.no_code} without a kiosk code`].filter(Boolean);
    return parts.length ? parts.join(", ") : "no changes";
  }

  function staffSyncText() {
    const s = state.staffSync || {};
    if (!s.enabled) return "";
    if (s.error) return `Connecteam staff sync: ${s.error}`;
    if (!s.synced_at) return "Adding everyone from Connecteam: not synced yet. It runs within a minute or two.";
    return `Synced from Connecteam ${ago(s.synced_at).toLowerCase()}: ${syncResultText(s.result)}. ` +
      "Checked about once an hour.";
  }

  function renderStaffSync() {
    const s = state.staffSync || {};
    const line = $("#staff-sync-status");
    line.textContent = staffSyncText();
    line.hidden = !s.enabled;
    line.classList.toggle("wb-note", !!s.error);
    $("#sync-staff").hidden = !s.enabled;
    const settingsLine = $("#ct-sync-status");
    settingsLine.textContent = !state.connecteam?.enabled ? "Turn on Connecteam to add everyone from it."
      : !s.enabled ? "Adding everyone from Connecteam is turned off." : staffSyncText();
    $("#ct-sync").disabled = !s.enabled;
  }

  function syncStaffNow(button) {
    return run(async () => {
      button.disabled = true;
      try {
        const r = await api("POST", "api/connecteam/sync-staff", {});
        toast(`Synced staff from Connecteam: ${r.detail || syncResultText(r)}.`);
      } finally {
        button.disabled = false;
        await load();
      }
    });
  }

  $("#sync-staff").addEventListener("click", (e) => syncStaffNow(e.currentTarget));
  $("#ct-sync").addEventListener("click", (e) => syncStaffNow(e.currentTarget));

  // ---------------------------------------------------------------- key cards: this computer's encoder

  // This computer (the one you're using): the card encoder plugged into the computer this
  // dashboard is open on, with nothing set up. While a card dialog uses it, this page does what
  // the staff app does in This computer (USB) mode (app.js wbLink): it holds a link to the add-on
  // (a WebSocket through Home Assistant, opened with a one-time ticket) and a connection to TT
  // Hotel's SN Web Socket on this computer, and passes each encoder request from the one to the
  // other, one at a time. Only the requests and fields in HERE_RELAYED pass; anything else is
  // dropped. The encoder's key from TT Hotel (hotelInfo) passes through this page in those
  // requests: it's never logged or kept, and SN Web Socket's copy of it in each answer is left
  // out of what goes back.
  //
  // The messages (encoder.LocalRelay has the add-on's side). From the add-on: {"type": "linked"}
  // once it has taken the link (through ingress the WebSocket opens before it does), {"type":
  // "call", "request": {"function", "requestId", ...}}, and {"type": "closing", "why"} before it
  // closes the link ("replaced": a newer link of yours took over; "not_allowed"; "stopping").
  // To it: {"type": "reply", "reply": <SN Web Socket's answer>}, and {"type": "status",
  // "helper", "version"} on linking, on every change, every HERE_STATUS_MS and whenever a request
  // can't be passed on (helper: "ready", "unreachable", "blocked" or null while not known).

  const HERE = "here";
  // SN Web Socket on this computer: localhost as well, for one that only listens on IPv6 (::1).
  const HERE_HELPER_URLS = ["ws://127.0.0.1:8102", "ws://localhost:8102"];
  const HERE_TICKET_PATH = "api/encoder/ticket";  // a one-time pass for the link (about 30 seconds)
  const HERE_LINK_PATH = "api/encoder/link";      // the link: a WebSocket through ingress
  const HERE_RETRY_MS = [1000, 30000];        // waits before linking again: the first, the longest
  const HERE_HELPER_RETRY_MS = [2000, 30000]; // ...and before trying SN Web Socket again
  const HERE_STEADY_MS = 15000;       // lasted this long: the next retry waits the first wait again
  const HERE_OPEN_MS = 8000;          // SN Web Socket gets this long to accept a connection...
  const HERE_ASKING_OPEN_MS = 60000;  // ...or this long where the browser may ask whether it may
  const HERE_ASKING_AFTER_MS = 1500;  // a connection still waiting this long: the browser is asking
  const HERE_ANSWERED_MS = 2000;      // answered in the site settings instead: start over after this
  const HERE_CALL_MS = 15000;         // ...and this long to answer (as long as the add-on waits)
  const HERE_STATUS_MS = 30000;       // the add-on hears how SN Web Socket is at least this often
  const HERE_LINGER_MS = 120000;      // kept open this long after a card dialog, for the next card
  const HERE_MAX = 256 * 1024;        // largest message passed on either way (the add-on's limit)
  const HERE_TEXT = 16384;            // longest text in a request (an encoder key is a few hundred)
  const HERE_QUEUE = 8;               // requests waiting their turn (the add-on sends one at once)
  // Chrome and Edge ask once whether a site may "access other apps and services on this device"
  // (Apps on device in the site settings). Its name since version 145, then the one before it.
  const HERE_PERMISSIONS = ["loopback-network", "local-network-access"];
  // The SN Web Socket requests the add-on's encoder runner makes, with the type of each of their
  // fields: the staff app's WB_RELAYED, kept in step with encoder.py's RELAY_FUNCTIONS. A request
  // or field not listed here is never passed on.
  const HERE_RELAYED = {
    CE_DisconnectComm: {},
    GetDeviceInfo: { transformCardNo: "boolean" },
    CE_ConnectComm: { portname: "string" },
    CE_GetCardID: {},
    CE_InitCardEncoder: { hotelInfo: "string" },
    CE_SetSectors: { sectors: "string" },
    CE_InitCard: { hotelInfo: "string" },
    CE_ClearCard: { hotelInfo: "string" },
    CE_WriteCard: { hotelInfo: "string", buildNo: "number", floorNo: "number", mac: "string",
      timestamp: "number", allowLockOut: "boolean" },
    CE_ReadCard: { hotelInfo: "string" },
    CE_CancelCard: { hotelInfo: "string", cardNo: "string", timestamp: "number" },
    CE_ReadCancellationInfo: { hotelInfo: "string" },
    CE_Beep: { voiceLen: "number", interval: "number", voiceCount: "number" },
    CE_GetVersion: {},
  };

  // This page's own ID, sent with the link's ticket, the card jobs it starts and their Try again:
  // a job only runs through the link of the page that started it, so with one account signed in
  // on two computers a card is never written on the other one's encoder. Kept for the tab
  // (sessionStorage), so a reload carries on with the job it started.
  const HERE_PAGE = (() => {
    const made = () => Array.from(crypto.getRandomValues(new Uint8Array(18)),
      (b) => b.toString(16).padStart(2, "0")).join("");
    try {
      const kept = sessionStorage.getItem("here-page");
      if (kept && /^[0-9a-f]{36}$/.test(kept)) return kept;
      const id = made();
      sessionStorage.setItem("here-page", id);
      return id;
    } catch {
      return made();
    }
  })();

  // The link while this page holds it (hereHold opens it): to the add-on (ws; state "connecting",
  // "open", "lost", "refused" or "replaced") and to SN Web Socket (helper).
  let here = null;
  let herePermission = null;        // the browser's local network permission, once asked (a promise)
  let hereJob = null;               // the card job using it, while its progress dialog is open
  const hereWatchers = new Set();   // the open dialogs' redraws, called on every change

  // Hold the link, opening it if it isn't open: a card dialog is about to use it.
  function hereHold() {
    if (here) {
      clearTimeout(here.linger);
      here.linger = null;
      return;
    }
    here = {
      ws: null, state: "connecting", closing: null, reason: null, timer: null,
      delay: HERE_RETRY_MS[0], openedAt: 0, beat: null, queue: [], calling: false, linger: null,
      helper: { ws: null, url: null, state: "connecting", version: null, timer: null,
        delay: HERE_HELPER_RETRY_MS[0], trying: null, pending: null, permission: null,
        opening: null },
    };
    hereConnect(here);
    hereHelperConnect(here);
  }

  // Done with it for now: it closes after HERE_LINGER_MS unless it's held again meanwhile.
  function hereRelease() {
    const link = here;
    if (!link || link.linger) return;
    link.linger = setTimeout(() => {
      if (here === link) hereStop();
    }, HERE_LINGER_MS);
  }

  function hereStop() {
    const link = here;
    here = null;
    clearTimeout(link.timer);
    clearTimeout(link.linger);
    clearInterval(link.beat);
    clearTimeout(link.helper.timer);
    link.queue = [];
    const ws = link.ws;
    link.ws = null;
    try { ws?.close(1000); } catch { /* already closing */ }
    link.helper.opening?.close();   // a connection still waiting (on the browser's question, say)
    hereHelperDrop(link);
    hereChanged();
  }

  function hereChanged() {
    for (const redraw of [...hereWatchers]) redraw();
  }

  // Link to the add-on: a one-time pass first (an ordinary dashboard request, so Home Assistant
  // checks it's you), then the WebSocket with it, since a browser can't send a WebSocket any
  // other credentials.
  async function hereConnect(link) {
    clearTimeout(link.timer);
    let pass = null;
    try {
      pass = await api("POST", HERE_TICKET_PATH, { page: HERE_PAGE });
    } catch (err) {
      if (here !== link || link.ws) return;
      // Refused (Wristbands turned off, say): say why, and try again only after a while.
      link.state = [403, 409].includes(err.status) ? "refused" : "lost";
      link.reason = link.state === "refused" ? err.message : null;
      if (link.state === "refused") link.delay = HERE_RETRY_MS[1];
      hereRetry(link);
      return;
    }
    if (here !== link || link.ws) return;   // stopped, or linked by another try meanwhile
    if (typeof pass?.ticket !== "string" || !pass.ticket) {
      link.state = "lost";
      hereRetry(link);
      return;
    }
    const address = new URL(`${HERE_LINK_PATH}?ticket=${encodeURIComponent(pass.ticket)}`, location.href);
    address.protocol = address.protocol === "https:" ? "wss:" : "ws:";
    let ws;
    try {
      ws = new WebSocket(address.href);
    } catch {
      link.state = "lost";
      hereRetry(link);
      return;
    }
    link.ws = ws;
    link.closing = null;
    ws.addEventListener("message", (ev) => {
      if (link.ws === ws) hereMessage(link, ev.data);
    });
    ws.addEventListener("close", (ev) => {
      if (link.ws === ws) hereClosed(link, ev.code);
    });
  }

  function hereClosed(link, code) {
    link.ws = null;
    clearInterval(link.beat);
    link.queue = [];   // nobody to answer now: the add-on gives up on them by itself
    const why = link.closing || { 4000: "replaced", 4003: "not_allowed" }[code];
    link.closing = null;
    if (why === "replaced") {
      // Opened in another window or tab (or on another computer) of yours, which has the encoder
      // now. Taken back only when asked (Use it here: hereResume), never by itself, since that
      // would fail a card being made there.
      link.state = "replaced";
      hereChanged();
      return;
    }
    if (why === "not_allowed") {
      link.state = "refused";
      link.reason = "Staff Access closed the link to this computer's encoder. Check that Wristbands " +
        "is turned on, then try again.";
      link.delay = HERE_RETRY_MS[1];
    } else {
      if (link.openedAt && Date.now() - link.openedAt >= HERE_STEADY_MS) link.delay = HERE_RETRY_MS[0];
      link.state = "lost";
    }
    link.openedAt = 0;
    hereRetry(link);
  }

  // Link again after a wait that doubles each time (with a little jitter).
  function hereRetry(link) {
    clearTimeout(link.timer);
    const wait = link.delay * (0.75 + Math.random() / 2);
    link.delay = Math.min(link.delay * 2, HERE_RETRY_MS[1]);
    link.timer = setTimeout(() => hereConnect(link), wait);
    hereChanged();
  }

  // Use it here: take the encoder back from the window that took it over.
  function hereResume() {
    const link = here;
    if (link?.state !== "replaced") return;
    link.state = "connecting";
    link.delay = HERE_RETRY_MS[0];
    hereConnect(link);
    hereChanged();
  }

  function hereSend(link, message) {
    if (link.ws?.readyState !== WebSocket.OPEN) return;
    try {
      link.ws.send(JSON.stringify(message));
    } catch { /* closing: its close event follows */ }
  }

  // Tell the add-on how SN Web Socket is (and that this link is still here), in its words:
  // connecting, or waiting for the browser's permission, is null (not known yet), and a question
  // nobody answered is blocked (until someone does).
  function hereReport(link) {
    const state = link.helper.state === "unanswered" ? "blocked" : link.helper.state;
    hereSend(link, { type: "status", version: link.helper.version,
      helper: ["ready", "unreachable", "blocked"].includes(state) ? state : null });
  }

  // A message from the add-on. A request that isn't allowed is dropped, never passed on.
  function hereMessage(link, text) {
    const message = hereParse(text);
    if (message?.type === "linked") {
      link.state = "open";
      link.reason = null;
      link.openedAt = Date.now();
      hereReport(link);
      clearInterval(link.beat);
      link.beat = setInterval(() => hereReport(link), HERE_STATUS_MS);
      hereChanged();
    } else if (message?.type === "closing") {
      link.closing = typeof message.why === "string" ? message.why : null;
    } else if (message?.type === "call") {
      const request = hereRequest(message.request);
      if (!request || link.queue.length >= HERE_QUEUE) {
        // Never the request itself: it may hold the encoder's key.
        console.warn(request ? "This computer's encoder: too many requests at once; one was dropped"
          : "This computer's encoder: dropped a request that isn't allowed");
        return;
      }
      link.queue.push({ request, ws: link.ws });
      hereRelayNext(link);
    }
  }

  // A JSON object from a WebSocket message, or null for anything else (or anything too big).
  function hereParse(text) {
    if (typeof text !== "string" || text.length > HERE_MAX) return null;
    try {
      const value = JSON.parse(text);
      return value && typeof value === "object" && !Array.isArray(value) ? value : null;
    } catch {
      return null;
    }
  }

  // The request to pass on to SN Web Socket, rebuilt from only what HERE_RELAYED allows, or null
  // if it asks for anything else (another function, another field, a value of the wrong type).
  function hereRequest(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const name = raw.function;
    const id = raw.requestId;
    if (typeof name !== "string" || !Object.prototype.hasOwnProperty.call(HERE_RELAYED, name) ||
        typeof id !== "string" || !id.length || id.length > 100) {
      return null;
    }
    const fields = HERE_RELAYED[name];
    const request = { function: name, requestId: id };
    for (const [key, value] of Object.entries(raw)) {
      if (key === "function" || key === "requestId") continue;
      const type = Object.prototype.hasOwnProperty.call(fields, key) ? fields[key] : null;
      if (!type || typeof value !== type) return null;
      if (type === "string" && value.length > HERE_TEXT) return null;
      if (type === "number" && !Number.isFinite(value)) return null;
      request[key] = value;
    }
    return request;
  }

  // Pass the waiting requests to SN Web Socket one at a time, each answer back to the add-on
  // (over the link the request came on: a newer link never gets an older link's answers).
  // Without an answer, the add-on hears how SN Web Socket is instead.
  async function hereRelayNext(link) {
    if (link.calling) return;
    link.calling = true;
    while (here === link && link.queue.length) {
      const { request, ws } = link.queue.shift();
      const reply = await hereHelperCall(link, request);
      if (link.ws !== ws) continue;
      if (reply) hereSend(link, { type: "reply", reply });
      else hereReport(link);
    }
    link.calling = false;
  }

  // One request to SN Web Socket: its answer, or null when it couldn't be asked or didn't
  // answer in time.
  async function hereHelperCall(link, request) {
    const helper = link.helper;
    if (!(helper.ws || await hereHelperConnect(link))) return null;
    const ws = helper.ws;
    if (!ws) return null;   // closed meanwhile
    return new Promise((resolve) => {
      const finish = (reply) => {
        clearTimeout(timer);
        if (helper.pending?.finish === finish) helper.pending = null;
        resolve(reply);
      };
      const timer = setTimeout(() => {
        finish(null);
        // Start afresh, so a late answer can't be taken for the next request's.
        if (helper.ws === ws) hereHelperDrop(link, true);
      }, HERE_CALL_MS);
      helper.pending = { name: request.function, id: request.requestId, finish };
      try {
        ws.send(JSON.stringify(request));
      } catch {
        finish(null);
      }
    });
  }

  // Something from SN Web Socket: the answer to the request waiting for one (by its requestId,
  // or by function for SN Web Socket's own errors, which leave that out), else ignored.
  function hereHelperMessage(link, text) {
    const reply = hereParse(text);
    if (!reply) return;
    const helper = link.helper;
    const version = reply.snWebSocketVersion;
    if (typeof version === "string" && version !== helper.version && /^[\x20-\x7e]{1,40}$/.test(version)) {
      helper.version = version;
      hereReport(link);
    }
    const pending = helper.pending;
    if (!pending) return;
    const ownError = !("requestId" in reply) && reply.function === pending.name &&
      reply.errorCode != null && reply.errorCode !== 0;
    if (reply.requestId !== pending.id && !ownError) return;
    delete reply.hotelInfo;   // SN Web Socket repeats the request, encoder key and all
    pending.finish(reply);
  }

  // Connect to SN Web Socket: the address that worked last time first, then the other. Resolves
  // to whether it worked; a failure is tried again later, waiting longer each time, except a
  // question from the browser that nobody answered (as the staff app's wbHelperConnect).
  function hereHelperConnect(link) {
    const helper = link.helper;
    if (helper.trying) return helper.trying;
    clearTimeout(helper.timer);
    helper.trying = (async () => {
      // A browser that's asking holds the connection until it's answered, while an SN Web Socket
      // that isn't running is refused at once: a connection still waiting after a moment means
      // it's asking.
      const askable = hereNetworkState(await hereNetworkPermission()) === "prompt";
      const asking = askable ? setTimeout(() => hereHelperSet(link, "asking"), HERE_ASKING_AFTER_MS) : 0;
      let asked = false;
      try {
        for (const address of new Set([helper.url, ...HERE_HELPER_URLS].filter(Boolean))) {
          const tried = Date.now();
          const ws = await hereHelperOpen(helper, address, askable ? HERE_ASKING_OPEN_MS : HERE_OPEN_MS);
          if (here !== link) {
            ws?.close();
            return false;
          }
          if (ws) {
            hereHelperAttach(link, ws, address);
            return true;
          }
          // Held, then refused: the browser asked and nobody answered, or the question was
          // closed. The other address would only ask again.
          if (askable && Date.now() - tried >= HERE_ASKING_AFTER_MS) {
            asked = true;
            break;
          }
        }
      } finally {
        clearTimeout(asking);
      }
      helper.permission = hereNetworkState(await hereNetworkPermission());   // Block answers it too
      if (here !== link) return false;
      if (helper.permission === "denied") {
        hereHelperSet(link, "blocked");
      } else if (asked && helper.permission !== "granted") {
        hereHelperSet(link, "unanswered");   // Try again asks again; it isn't asked by itself
        return false;
      } else if (asked) {
        helper.delay = HERE_HELPER_RETRY_MS[0];   // allowed in the site settings meanwhile
        hereHelperSet(link, "connecting");
      } else {
        hereHelperSet(link, "unreachable");
      }
      hereHelperRetry(link);
      return false;
    })().finally(() => { helper.trying = null; });
    return helper.trying;
  }

  // Try again (in a dialog): connect to SN Web Socket now, starting over from the first wait.
  function hereHelperTryAgain() {
    const link = here;
    if (!link || link.helper.ws || link.helper.trying) return;
    link.helper.delay = HERE_HELPER_RETRY_MS[0];
    hereHelperSet(link, "connecting");
    hereHelperConnect(link);
  }

  // A WebSocket to SN Web Socket once it's open, or null if it can't be opened in time (or
  // helper.opening, the connection waiting meanwhile, is closed).
  function hereHelperOpen(helper, address, wait) {
    return new Promise((resolve) => {
      let ws;
      try {
        ws = new WebSocket(address);
      } catch {
        resolve(null);   // refused by the browser outright
        return;
      }
      helper.opening = ws;
      const timer = setTimeout(() => ws.close(), wait);   // its close event says no
      const done = (result) => {
        clearTimeout(timer);
        if (helper.opening === ws) helper.opening = null;
        resolve(result);
      };
      ws.addEventListener("open", () => done(ws), { once: true });
      ws.addEventListener("close", () => done(null), { once: true });
    });
  }

  function hereHelperAttach(link, ws, address) {
    const helper = link.helper;
    const openedAt = Date.now();
    helper.ws = ws;
    helper.url = address;
    ws.addEventListener("message", (ev) => {
      if (helper.ws === ws) hereHelperMessage(link, ev.data);
    });
    ws.addEventListener("close", () => {
      if (helper.ws !== ws) return;   // closed on purpose
      helper.ws = null;
      helper.pending?.finish(null);
      if (here !== link) return;
      // SN Web Socket closed or restarted: connect again straight away, unless it closed as soon
      // as it opened, which waits longer each time instead.
      hereHelperSet(link, "connecting");
      if (Date.now() - openedAt >= HERE_STEADY_MS) {
        helper.delay = HERE_HELPER_RETRY_MS[0];
        hereHelperConnect(link);
      } else {
        hereHelperRetry(link);
      }
    });
    hereHelperSet(link, "ready");
  }

  // Close the connection to SN Web Socket, and with `again`, connect afresh. A request still
  // waiting gets no answer.
  function hereHelperDrop(link, again = false) {
    const helper = link.helper;
    const ws = helper.ws;
    helper.ws = null;
    try { ws?.close(); } catch { /* already closing */ }
    helper.pending?.finish(null);
    if (again && here === link) {
      hereHelperSet(link, "connecting");
      hereHelperConnect(link);
    }
  }

  function hereHelperRetry(link) {
    const helper = link.helper;
    clearTimeout(helper.timer);
    const wait = helper.delay * (0.75 + Math.random() / 2);
    helper.delay = Math.min(helper.delay * 2, HERE_HELPER_RETRY_MS[1]);
    helper.timer = setTimeout(() => {
      if (here === link) hereHelperConnect(link);
    }, wait);
  }

  function hereHelperSet(link, state) {
    if (link.helper.state === state) return;
    link.helper.state = state;
    hereReport(link);
    hereChanged();
  }

  // The browser's permission for this site to reach this computer's own apps, under each name it
  // knows (none in browsers without it). Allowing it connects straight away.
  function hereNetworkPermission() {
    if (!herePermission) {
      herePermission = (async () => {
        const found = [];
        for (const name of HERE_PERMISSIONS) {
          try {
            const status = await navigator.permissions.query({ name });
            status.addEventListener("change", hereNetworkChanged);
            found.push(status);
          } catch { /* not a permission this browser has */ }
        }
        return found;
      })();
    }
    return herePermission;
  }

  // Allowed or blocked: connect now. Changing the site settings leaves a connection waiting for
  // the question, so it's started over if it's still waiting a moment later.
  function hereNetworkChanged() {
    const link = here;
    if (!link || link.helper.ws) return;
    const opening = link.helper.opening;
    if (!opening) {
      hereHelperConnect(link);
      return;
    }
    setTimeout(() => {
      if (link.helper.opening === opening) opening.close();
    }, HERE_ANSWERED_MS);
  }

  // Those as one state: blocked under any name is blocked, allowed only when allowed under
  // every name, and null in browsers that have none of them.
  function hereNetworkState(statuses) {
    const states = statuses.map((status) => status.state);
    if (!states.length) return null;
    return states.includes("denied") ? "denied"
      : states.every((s) => s === "granted") ? "granted" : "prompt";
  }

  // This computer's encoder in plain words, for the dialogs: { ready, busy (still connecting:
  // nothing to do but wait), title, note, action: [label, onclick] }.
  function hereView() {
    if (!window.isSecureContext) {
      // Browsers only let a secure page reach the encoder: not Home Assistant at http://<IP>.
      return { title: "Open Home Assistant at its https address to use this computer's encoder",
        note: "This page was opened at a plain http:// address, and the browser only lets a secure " +
          "page reach the card encoder." };
    }
    const link = here;
    if (!link) return { busy: true, title: "Connecting to the card encoder on this computer…" };
    const helper = link.helper.state;
    if (link.state === "replaced") {
      return { title: "Connected in another window",
        note: "Staff Access is using this card encoder in another window or tab (or on another computer " +
          "you're signed in on).",
        action: ["Use it here", hereResume] };
    }
    if (helper === "blocked") {
      return { title: "This browser is blocking the card encoder",
        note: "Click the icon at the left of the address bar and allow Apps on device for this site " +
          "(Local network access in older versions). It connects by itself once it's allowed.",
        action: ["Try again", hereHelperTryAgain] };
    }
    if (helper === "unanswered") {
      return { title: "Allow this site to reach the card encoder",
        note: "The browser asked whether this site may access other apps and services on this device, " +
          "which is how it reaches the card encoder. Click Try again, then choose Allow.",
        action: ["Try again", hereHelperTryAgain] };
    }
    if (helper === "asking") {
      return { busy: true, title: "Choose Allow when the browser asks",
        note: "The browser is asking whether this site may access other apps and services on this " +
          "device, which is how it reaches the card encoder: choose Allow. If the question has gone, " +
          "click the icon at the left of the address bar." };
    }
    if (helper === "unreachable") {
      // The browser only asks once SN Web Socket is running, and one blocking the site without
      // saying so refuses just as quickly: unless it's allowed, it may be either.
      return { title: "SN Web Socket isn't running on this computer",
        note: "Start TT Hotel's SN Web Socket on this computer (the one this browser is on), with the " +
          "card encoder plugged in. It connects by itself once it's running." +
          (link.helper.permission !== "granted" ? " The browser then asks whether this site may access " +
            "other apps and services on this device: choose Allow. If SN Web Socket is running already, " +
            "check that Apps on device isn't blocked for this site (the icon at the left of the address " +
            "bar)." : ""),
        action: ["Try again", hereHelperTryAgain] };
    }
    if (link.state === "refused") {
      return { title: "The card encoder can't be connected", note: link.reason || "Reload this page and try again." };
    }
    if (link.state === "lost") {
      return { busy: true, title: "Connecting to Staff Access…", note: "Can't reach Staff Access right now. Still trying…" };
    }
    if (link.state !== "open" || helper !== "ready") {
      return { busy: true, title: "Connecting to the card encoder on this computer…" };
    }
    return { ready: true, title: "Card encoder ready",
      note: link.helper.version ? `SN Web Socket ${link.helper.version}` : "" };
  }

  // This computer, in the list of card encoders to choose from.
  function hereWords() {
    if (!window.isSecureContext) return "Needs Home Assistant opened at its https address.";
    return "The encoder plugged into the computer you're using, with SN Web Socket running on it." +
      (here && hereView().ready ? " Connected now." : "");
  }

  // This computer: open its link and wait in a dialog, in plain words, until the encoder is ready.
  // Resolves with "ready", "other" (choose another encoder: offered when there is one) or null
  // (cancelled).
  function hereReady(others) {
    if (window.isSecureContext) hereHold();
    if (hereView().ready) return Promise.resolve("ready");
    return new Promise((resolve) => {
      const dialog = $("#here-dialog");
      const part = (key) => $(`[data-${key}]`, dialog);
      let result = null;
      let look = null;
      let keys = null;
      const button = (label, cls, onclick) => h("button", { type: "button", class: cls, onclick }, label);
      const draw = () => {
        const v = hereView();
        if (v.ready) {
          result = "ready";
          dialog.close();
          return;
        }
        part("prompt").textContent = v.title;
        part("note").textContent = v.note || "";
        const next = v.busy ? "busy" : "problem";
        if (next !== look) {
          look = next;
          const visual = part("visual");
          visual.className = v.busy ? "job-visual" : "job-visual alert";
          fill(visual, v.busy ? h("div", { class: "spinner" }) : h("span", { class: "job-symbol" }, "!"));
        }
        const now = `${v.action?.[0] || ""}|${others}`;
        if (now === keys) return;
        keys = now;
        const act = v.action && button(v.action[0], "btn primary", v.action[1]);
        fill(part("actions"), others ? button("Use another encoder", "btn", () => {
          result = "other";
          dialog.close();
        }) : null, button("Cancel", "btn", () => dialog.close()), act);
        (act || $(".btn", part("actions")))?.focus();
      };
      hereWatchers.add(draw);
      dialog.onclose = () => {
        hereWatchers.delete(draw);
        if (result !== "ready") hereRelease();
        resolve(result);
      };
      draw();
      dialog.showModal();
    });
  }

  // A card job on This computer, its link ready. A job of yours still unfinished there is shown
  // instead, to finish or stop first (null).
  async function postHereJob(path, body) {
    try {
      const { job } = await api("POST", path, { ...body, encoder: HERE, page: HERE_PAGE });
      hereJob = job.id;
      return job;
    } catch (err) {
      if (!err.data?.job) {
        hereRelease();
        throw err;
      }
      hereJob = err.data.job.id;
      toast(err.message, true);
      await showJob(err.data.job, "Card job on this computer");
      await afterCardJob();
      return null;
    }
  }

  // ---------------------------------------------------------------- key cards: encoders and progress

  const JOB_POLL_MS = 1000;
  const JOB_HIDDEN_POLL_MS = 4000;
  const JOB_LOST_AFTER = 4;   // failed checks in a row before saying the add-on can't be reached
  const JOB_ACTIVE = new Set(["queued", "running", "needs_attention"]);
  const CARD_WORDS = { written: "Made", cleared: "Cleared", failed: "Didn't work", skipped: "Skipped",
    cancelled: "Stopped", pending: "Waiting" };
  // How each step looks: [symbol, class]. Waiting for the encoder and writing show a spinner.
  const JOB_LOOK = { waiting_for_card: ["", "pulse"], remove_card: ["", "lift"], needs_attention: ["!", "alert"],
    done: ["✓", "ok"], cancelled: ["■", "quiet"] };

  // The card encoders computers use now, one per address (two computers can share one).
  function cardEncoders() {
    const found = new Map();
    for (const d of state.devices) {
      if (d.status === "revoked" || !d.encoder_address) continue;
      const e = found.get(d.encoder_address);
      if (e) e.name = `${e.name}, ${d.name}`;
      else found.set(d.encoder_address, { device_id: d.id, name: d.name, address: d.encoder_address });
    }
    return [...found.values()];
  }

  // How an encoder is now, for choosing one. This computer (USB) connects through the staff app
  // on that computer, so it says Wristbands must be open there (a job waits for it until then).
  function encoderWords(e) {
    const d = state.devices.find((x) => x.id === e.device_id);
    const address = e.address || d?.encoder_address;
    const s = state.wristbands?.encoders?.[address] || e.status || {};
    if (isLocal(address)) {
      const link = d?.encoder_link || s;
      const now = s.busy ? "Making cards right now" : !link.link ? "Not connected now"
        : link.helper === "ready" ? "Connected now" : "Wristbands is open there, but the encoder isn't ready";
      return `This computer (USB): Wristbands must be open on it. ${now}.`;
    }
    return s.busy ? "Making cards right now" : s.error ? `Problem: ${s.error}` : s.connected ? "Working" : "";
  }

  // Which encoder to use: This computer (the one this dashboard is open on) first, then each
  // computer's. Resolves with HERE, a computer's ID, or null if they cancel. With no computer's
  // encoder set up, it's This computer.
  async function pickEncoder(list = cardEncoders()) {
    if (!list.length) return HERE;
    const picked = await askChoice("Which card encoder? Have the card ready next to it.",
      [[HERE, "This computer (the one you're using)", hereWords()],
        ...list.map((e) => [String(e.device_id), e.name, encoderWords(e)])]);
    return picked == null || picked === HERE ? picked : Number(picked);
  }

  // Starts a card job on an encoder the admin picks. Returns the job, or null if they cancelled.
  async function postCardJob(path, body) {
    let list = cardEncoders();
    for (;;) {
      const choice = await pickEncoder(list);
      if (choice == null) return null;
      if (choice === HERE) {
        const ready = await hereReady(list.length > 0);
        if (ready === "other") continue;
        return ready === "ready" ? postHereJob(path, body) : null;
      }
      try {
        return (await api("POST", path, { ...body, device_id: choice })).job;
      } catch (err) {
        if (!err.data?.encoders?.length) throw err;
        list = err.data.encoders;   // the computers changed meanwhile: ask again from the add-on's list
      }
    }
  }

  // The live encoding dialog: place the card, writing, done, or try again / skip / stop, like the
  // front-desk app. Resolves with the job as it last was once the dialog is closed.
  function showJob(job, title, { expiry = true } = {}) {
    return new Promise((resolve) => {
      const dialog = $("#job-dialog");
      const part = (key) => $(`[data-${key}]`, dialog);
      const v = { job, phase: undefined, mode: null, timer: null, acting: false, lost: 0, rev: 0 };
      const active = () => JOB_ACTIVE.has(v.job.status);
      const button = (label, cls, onclick) => h("button", { type: "button", class: cls, onclick }, label);
      const usesHere = job.id === hereJob;   // This computer: its link's trouble shows too

      // The job's own Try again connects to SN Web Socket again, so only Use it here is offered.
      const drawHere = () => {
        const line = part("here");
        const hv = usesHere && active() ? hereView() : null;
        line.hidden = !hv || !!hv.ready;
        const act = hv?.action?.[1] === hereResume ? hv.action : null;
        const key = line.hidden ? "" : `${hv.title}|${hv.note}|${act?.[0]}`;
        if (key === line.dataset.key) return;
        line.dataset.key = key;
        if (!line.hidden) {
          fill(line, h("strong", null, hv.title), hv.note ? ` ${hv.note}` : null,
            act ? [" ", button(act[0], "linkish", act[1])] : null);
        }
      };

      const draw = () => {
        const j = v.job;
        const total = j.cards?.length || j.count || 1;
        part("eyebrow").textContent = title;
        part("step").textContent = active() && j.current && total > 1 ? `Card ${j.current} of ${total}` : "";
        part("prompt").textContent = j.message || "";
        if (j.phase !== v.phase) {
          v.phase = j.phase;
          const [symbol, cls] = JOB_LOOK[j.phase] || [null, ""];
          const visual = part("visual");
          visual.className = cls ? `job-visual ${cls}` : "job-visual";
          fill(visual, symbol == null ? h("div", { class: "spinner" })
            : symbol ? h("span", { class: "job-symbol" }, symbol) : h("div", { class: "job-card" }));
        }
        dialog.classList.toggle("attention", j.status === "needs_attention");
        const dots = part("dots");
        dots.hidden = total < 2;
        dots.replaceChildren(...(j.cards || []).map((c) => {
          const now = active() && c.seq === j.current && c.status === "pending";
          return h("li", { class: `job-dot ${now ? "current" : c.status}`, title: `Card ${c.seq}: ` +
            (now ? "in progress" : (CARD_WORDS[c.status] || c.status).toLowerCase()) },
          c.status === "written" || c.status === "cleared" ? "✓" : String(c.seq));
        }));
        part("meta").textContent = [expiry && j.expires_label && `Works until ${j.expires_label}`, j.summary]
          .filter(Boolean).join(" · ");
        part("conn").hidden = v.lost < JOB_LOST_AFTER;
        drawHere();
        const mode = j.status === "needs_attention" ? "attention" : active() ? "active" : "finished";
        if (mode === v.mode) return;
        v.mode = mode;
        const actions = part("actions");
        if (mode === "attention") {
          const retry = button("Try again", "btn primary", () => decide("retry"));
          fill(actions, button("Stop", "btn danger", stop),
            total > 1 ? button("Skip this one", "btn", () => decide("skip")) : null, retry);
          retry.focus();
        } else if (mode === "active") {
          actions.replaceChildren(button("Stop", "btn danger", stop));
        } else {
          const made = (j.cards || []).filter((c) => c.status === "written" && c.card_number);
          const done = button("Done", "btn primary", () => dialog.close());
          fill(actions, made.length
            ? h("span", { class: "job-made" }, `Card number${made.length === 1 ? "" : "s"}: ` +
              made.map((c) => c.card_number).join(", ")) : null, done);
          done.focus();
        }
      };

      const schedule = () => {
        clearTimeout(v.timer);
        if (dialog.open && active() && !v.acting) {
          v.timer = setTimeout(poll, document.visibilityState === "visible" ? JOB_POLL_MS : JOB_HIDDEN_POLL_MS);
        }
      };
      async function poll() {
        const rev = v.rev;
        try {
          const r = await api("GET", `api/keycards/jobs/${v.job.id}`);
          if (rev !== v.rev || !dialog.open) return;
          v.job = r.job;
          v.lost = 0;
        } catch (err) {
          if (rev !== v.rev || !dialog.open) return;
          if (err.status === 404) {
            v.job = { ...v.job, status: "cancelled", phase: "cancelled", message: err.message };
          } else {
            v.lost += 1;
          }
        }
        draw();
        schedule();
      }
      async function decide(action) {
        if (v.acting) return;
        v.acting = true;
        v.rev += 1;   // a check on its way would show the job from before this
        clearTimeout(v.timer);
        part("actions").classList.add("working");
        try {
          v.job = (await api("POST", `api/keycards/jobs/${v.job.id}/${action}`, { page: HERE_PAGE })).job;
        } catch (err) {
          toast(err.message, true);
        } finally {
          v.acting = false;
          part("actions").classList.remove("working");
        }
        if (!dialog.open) return;
        v.mode = null;
        draw();
        schedule();
      }
      async function stop() {
        const words = v.job.status === "needs_attention" ? "Stop here?" :
          "Stop now? A card being written right now may be left half made: clear it or make it again.";
        if (await askConfirm(words, "Stop")) await decide("cancel");
      }

      // Escape doesn't close it while the encoder is working: Stop says what happens to the card.
      dialog.oncancel = (ev) => { if (active()) ev.preventDefault(); };
      dialog.onclose = () => {
        clearTimeout(v.timer);
        v.rev += 1;
        if (usesHere) {
          hereWatchers.delete(draw);
          hereJob = null;
          hereRelease();
        }
        resolve(v.job);
      };
      if (usesHere) hereWatchers.add(draw);
      draw();
      dialog.showModal();
      part("prompt").focus();
      schedule();
    });
  }

  // After a card job: the people (key card counts) and the Key cards list are drawn again.
  async function afterCardJob() {
    await load();
    loadKeyCards(true);
  }

  // Make key card (Staff tab): their department's key card, on an encoder the admin picks.
  async function makeKeyCard(m) {
    const dept = state.departments.find((d) => d.id === m.department_id);
    if (!dept) throw new Error(`Choose ${m.name}'s department first: their key card comes from it.`);
    if (dept.card_access == null) {
      throw new Error(`${dept.name} has no key card set. Edit the department under Departments to choose its doors.`);
    }
    const ask = async (current) => {
      const numbers = (current || []).map((c) => c.card_number).filter(Boolean);
      const n = current?.length || m.key_cards || 1;
      return askChoice(`${m.name} already has ${n === 1 ? "an active key card" : `${n} active key cards`}` +
        `${numbers.length ? ` (No. ${numbers.join(", ")})` : ""}. Replace it, or add another card?`, [
        ["replace", "Replace their current card", "The new card also cancels the old one at each door it's " +
          "tapped on, and the old one is marked revoked. Best when it's lost or broken" +
          (n > 1 ? " (the newest one is replaced)." : ".")],
        ["add", "Add another card", "Their current card keeps working too."]]);
    };
    let mode = null;
    if (m.key_cards) {
      mode = await ask(null);
      if (!mode) return false;
    }
    let job;
    try {
      job = await postCardJob(`api/staff/${m.id}/keycard`, mode ? { mode } : {});
    } catch (err) {
      if (!err.data?.needs_choice) throw err;
      mode = await ask(err.data.current);   // they got a card meanwhile
      if (!mode) return false;
      job = await postCardJob(`api/staff/${m.id}/keycard`, { mode });
    }
    if (!job) return false;
    const final = await showJob(job, `Key card for ${m.name} · ${dept.name}`);
    await afterCardJob();
    if (final.status === "done" && mode === "replace") {
      toast("Done. Have them tap the new card on each door once: that cancels the old card there.");
    }
    return false;
  }

  // ---------------------------------------------------------------- key cards list (Staff tab)

  const KC_STATE = { active: ["Active", "active"], expired: ["Expired", ""], revoked: ["Revoked", "revoked"],
    lost: ["Lost", "locked"] };
  const KC_SOURCE = { dashboard: "Staff tab", staff_app: "staff app" };
  const LOCKOUT_WORDS = { notice: "lost card notice", replacement: "replacement card", remote: "remote lock-out" };
  const kc = { state: "active", q: "", oldest: null, seq: 0, remote: null, timer: null };

  function openStaffTab() {
    if (!wristbandsOn()) return;
    loadKeyCards(true);   // only read while the section is open
    if (!wbDoors && state.departments.some((d) => d.card_access?.length)) loadDoors();
  }

  // Remote lock-out's state, from the last Key cards list (or the settings until then).
  function remoteInfo() {
    if (kc.remote) return kc.remote;
    const on = state.settings.wb_remote_lockout === "1";
    const tested = Number(state.settings.wb_remote_lockout_tested) || null;
    return { enabled: on, tested_at: tested, ready: on && !!tested, api: state.settings.wb_remote_lockout_api,
      why: !on ? "Remote lock-out is turned off (Wristbands settings). It's experimental."
        : !tested ? "Test remote lock-out on one door first." : null };
  }

  // The Key cards section folds away to its title and a short count: folded at first, and open
  // or closed as last left in this browser.
  const KC_OPEN_KEY = "staff-portal.keycards-open";
  try {
    $("#keycards-section").open = localStorage.getItem(KC_OPEN_KEY) === "1";
  } catch { /* folded */ }
  $("#keycards-section").addEventListener("toggle", () => {
    const open = $("#keycards-section").open;
    try {
      localStorage.setItem(KC_OPEN_KEY, open ? "1" : "0");
    } catch { /* not remembered this time */ }
    if (open && !$("#tab-staff").hidden) loadKeyCards(true);
  });

  // Its closed line: "14 active, 2 lost" (lost cards that haven't expired: they still open doors).
  function renderKeyCardTotals(totals) {
    if (totals) state.keyCardTotals = totals;
    const t = state.keyCardTotals;
    $("#kc-totals").replaceChildren(...!t ? [] : t.lost
      ? [`${t.active} active, `, h("span", { class: "alert" }, `${t.lost} lost`)] : [`${t.active} active`]);
  }

  async function loadKeyCards(reset) {
    if (!$("#keycards-section").open) return;
    const rows = $("#kc-rows");
    const seq = ++kc.seq;
    if (reset) kc.oldest = null;
    const params = new URLSearchParams({ state: kc.state });
    if (kc.q) params.set("q", kc.q);
    if (!reset && kc.oldest) params.set("before", String(kc.oldest));
    let data;
    try {
      data = await api("GET", `api/keycards?${params}`);
    } catch (err) {
      if (seq !== kc.seq) return;
      if (reset) rows.replaceChildren(h("tr", null, h("td", { colspan: "8", class: "empty" }, err.message)));
      else toast(err.message, true);
      return;
    }
    if (seq !== kc.seq) return;   // a newer filter or search was chosen meanwhile
    kc.remote = data.remote || null;
    renderKeyCardTotals(data.totals);
    const cards = data.cards || [];
    if (reset) {
      rows.replaceChildren();
      if (!cards.length) {
        const what = { active: "No active key cards", expired: "No expired key cards", revoked: "No revoked key cards",
          lost: "No lost key cards", all: "No key cards made yet" }[kc.state];
        rows.append(h("tr", null, h("td", { colspan: "8", class: "empty" }, kc.q ? `${what} match.` : `${what}.`)));
      }
    }
    rows.append(...cards.map(keyCardRow));
    if (cards.length) kc.oldest = cards[cards.length - 1].id;
    $("#kc-more").hidden = !data.more;
  }

  // How locking a card out is going: "Locked out of 2 of 5 doors" (or "" if not started).
  function lockoutText(card) {
    const lockouts = card.lockouts || [];
    const done = new Set(lockouts.filter((l) => l.status === "done").map((l) => l.lock_id));
    const failed = lockouts.filter((l) => l.status === "failed" && !done.has(l.lock_id)).length;
    const running = card.remote_running || lockouts.some((l) => l.method === "remote" && l.status === "pending");
    const total = card.doors ? card.doors.length : new Set(lockouts.map((l) => l.lock_id)).size;
    const parts = [];
    if (card.state === "lost") {
      if (card.expires_at <= Date.now() / 1000) parts.push("Expired, so it opens nothing now");
      else if (!total) parts.push("No doors to lock it out of");
      else parts.push(`Locked out of ${done.size} of ${plural(total, "door")}`);
    } else if (lockouts.length) {
      parts.push(`Cancelled at ${done.size} of ${plural(total, "door")}`);
    }
    if (running) parts.push("remote lock-out running");
    if (failed) parts.push(`${failed} remote failed`);
    return parts.join(", ");
  }

  function keyCardRow(card) {
    const [label, cls] = KC_STATE[card.state] || [card.state, ""];
    const progress = lockoutText(card);
    const actions = [];
    if (card.state === "active") {
      actions.push(h("button", { type: "button", class: "btn", onclick: () => run(() => revokeCard(card)) }, "Revoke"),
        h("button", { type: "button", class: "btn danger", onclick: () => run(() => reportLost(card)) }, "Report lost"));
    } else if (card.state === "lost" || card.lockouts?.length) {
      actions.push(h("button", { type: "button", class: card.state === "lost" ? "btn primary" : "btn",
        onclick: () => run(() => openCardDialog(card.id)) }, card.state === "lost" ? "Lock out…" : "Doors"));
    }
    const stateBy = card.state_by && (card.state === "revoked" || card.state === "lost")
      ? `${card.state === "lost" ? "Reported" : "Revoked"} ${card.state_at ? ago(card.state_at).toLowerCase() : ""} by ${card.state_by}`
      : "";
    return h("tr", { class: card.state === "lost" ? "kc-lost" : null },
      h("td", null, card.holder, h("div", { class: "sub" },
        [card.holder_archived ? "Archived" : "", card.department,
          card.name && card.name !== card.holder ? `Card says ${card.name}` : ""].filter(Boolean).join(" · "))),
      h("td", { class: "nowrap kc-number" }, card.card_number,
        card.cancelled_card ? h("div", { class: "sub" }, `Cancels ${card.cancelled_card}`) : null),
      h("td", null, card.summary, card.allow_lockout ? h("div", { class: "sub" }, "Opens deadbolted doors") : null),
      h("td", { class: "nowrap" }, card.made_at ? when(card.made_at) : "",
        h("div", { class: "sub" }, `${card.made_by}${KC_SOURCE[card.source] ? ` · ${KC_SOURCE[card.source]}` : ""}`)),
      h("td", { class: "nowrap" }, card.expires_label || when(card.expires_at)),
      h("td", null, h("span", { class: `badge ${cls}`.trim() }, label),
        stateBy ? h("div", { class: "sub" }, stateBy) : null,
        progress ? h("div", { class: `sub ${card.state === "lost" ? "kc-progress" : ""}` }, progress) : null),
      h("td", null, lastUsed(card.last_used)),
      h("td", { class: "kc-actions" }, actions));
  }

  // Show one person's active key cards: their name in the search box.
  function showKeyCardsFor(m) {
    setKeyCardFilter("active");
    $("#kc-search").value = m.name;
    kc.q = m.name;
    const section = $("#keycards-section");
    if (section.open) loadKeyCards(true);
    else section.open = true;   // opening it reads the list
    section.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function setKeyCardFilter(value) {
    kc.state = value;
    document.querySelectorAll("#kc-filter button").forEach((b) =>
      b.setAttribute("aria-checked", String(b.dataset.state === value)));
  }

  document.querySelectorAll("#kc-filter button").forEach((b) => b.addEventListener("click", () => {
    setKeyCardFilter(b.dataset.state);
    loadKeyCards(true);
  }));
  $("#kc-search").addEventListener("input", (e) => {
    clearTimeout(kc.timer);
    kc.timer = setTimeout(() => {
      kc.q = e.target.value.trim().slice(0, 60);
      loadKeyCards(true);
    }, 250);
  });
  $("#kc-search").addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });
  $("#kc-refresh").addEventListener("click", () => loadKeyCards(true));
  $("#kc-more").addEventListener("click", () => loadKeyCards(false));

  // Revoke (card in hand): the encoder waits for that exact card and clears it.
  async function revokeCard(card) {
    if (!await askConfirm(`Revoke card ${card.card_number} (${card.holder})? You need the card itself: put it ` +
      "on the encoder and it's cleared, so it opens nothing (it can be used again for a new card). If you " +
      "don't have it, use Report lost instead.", "Revoke")) return false;
    const job = await postCardJob(`api/keycards/${card.id}/revoke`, {});
    if (!job) return false;
    const final = await showJob(job, `Revoke card ${card.card_number} · ${card.holder}`, { expiry: false });
    await afterCardJob();
    if (final.status === "done") toast(`Card ${card.card_number} is cleared and revoked.`);
    return false;
  }

  async function reportLost(card) {
    if (!await askConfirm(`Report card ${card.card_number} (${card.holder}) lost? A lost card keeps opening its ` +
      `doors until it expires (${card.expires_label || when(card.expires_at)}) unless it's locked out of each ` +
      "one. Next, choose how.", "Report lost")) return false;
    const { card: lost } = await api("POST", `api/keycards/${card.id}/lost`, {});
    loadKeyCards(true);
    load();
    return openCardDialog(lost.id, lost);
  }

  // ---------------------------------------------------------------- a lost card: lock it out

  const LOST_POLL_MS = 5000;   // while a remote lock-out runs in the background
  let cardView = null;         // { id, card, timer } while the card dialog is open

  async function openCardDialog(id, card = null) {
    const dialog = $("#card-dialog");
    cardView = { id, card: card?.doors ? card : null, timer: null };
    if (!cardView.card) cardView.card = (await api("GET", `api/keycards/${id}`)).card;
    drawCardDialog();
    if (!dialog.open) dialog.showModal();
    scheduleCardPoll();
    return false;
  }

  $("[data-close]", $("#card-dialog")).addEventListener("click", () => $("#card-dialog").close());
  $("#card-dialog").addEventListener("close", () => {
    clearTimeout(cardView?.timer);
    cardView = null;
    loadKeyCards(true);
  });

  function scheduleCardPoll() {
    clearTimeout(cardView?.timer);
    const c = cardView?.card;
    if (!c || !(c.remote_running || c.lockouts?.some((l) => l.method === "remote" && l.status === "pending"))) return;
    const view = cardView;
    view.timer = setTimeout(async () => {
      try {
        const { card } = await api("GET", `api/keycards/${view.id}`);
        if (cardView !== view) return;
        view.card = card;
        drawCardDialog();
      } catch { /* tried again next time */ }
      if (cardView === view) scheduleCardPoll();
    }, LOST_POLL_MS);
  }

  async function refreshCardDialog(card) {
    if (!cardView) return;
    cardView.card = card || (await api("GET", `api/keycards/${cardView.id}`)).card;
    drawCardDialog();
    scheduleCardPoll();
  }

  // A card job started from the card dialog (notice or replacement), then the dialog drawn again.
  async function cardDialogJob(path, title) {
    const job = await postCardJob(path, {});
    if (!job) return false;
    await showJob(job, title);
    await refreshCardDialog();
    load();
    return false;
  }

  function drawCardDialog() {
    const dialog = $("#card-dialog");
    const card = cardView.card;
    const lost = card.state === "lost";
    const expired = card.expires_at <= Date.now() / 1000;
    $("[data-title]", dialog).textContent = `${lost ? "Lost card" : "Card"} ${card.card_number} · ${card.holder}`;
    const lockouts = card.lockouts || [];
    const doors = card.doors || [];
    const byDoor = new Map(doors.map((d) => [d.id, { door: d, rows: [] }]));
    for (const l of lockouts) {
      if (!byDoor.has(l.lock_id)) byDoor.set(l.lock_id, { door: { id: l.lock_id, name: l.door, has_gateway: false }, rows: [] });
      byDoor.get(l.lock_id).rows.push(l);
    }
    const has = (method) => lockouts.some((l) => l.method === method);
    const tick = (l) => {
      const box = h("input", { type: "checkbox", checked: l.status === "done" });
      box.addEventListener("change", () => run(async () => {
        try {
          const { card: next } = await api("PATCH", `api/keycards/${card.id}/lockouts/${l.lock_id}`,
            { method: l.method, done: box.checked });
          await refreshCardDialog(next);
        } catch (err) {
          box.checked = !box.checked;
          throw err;
        }
      }));
      return h("label", { class: "check" }, box, l.method === "notice" ? "Notice tapped" : "Replacement tapped");
    };
    const doorRows = [...byDoor.values()].map(({ door, rows }) => {
      const safe = rows.some((l) => l.status === "done");
      const remote = rows.find((l) => l.method === "remote");
      return h("div", { class: `lo-row ${safe ? "safe" : ""}`.trim() },
        h("div", { class: "entity-text" },
          h("div", { class: "entity-name" }, door.name, door.has_gateway ? h("small", { class: "wb-count" }, " · gateway") : null),
          h("div", { class: "entity-id lo-state" }, safe
            ? `Locked out (${rows.filter((l) => l.status === "done").map((l) => LOCKOUT_WORDS[l.method]).join(", ")})`
            : lost && !expired ? "Still opens it" : ""),
          remote && remote.status !== "done"
            ? h("div", { class: `via ${remote.status === "failed" ? "access-note" : "pending-note"}` },
              `Remote lock-out: ${remote.detail || remote.status}`) : null),
        h("div", { class: "lo-ticks" }, rows.filter((l) => l.method !== "remote").map(tick)));
    });
    const gateways = doors.filter((d) => d.has_gateway &&
      !lockouts.some((l) => l.lock_id === d.id && l.method === "remote" && l.status === "done"));
    const remote = remoteInfo();
    const running = card.remote_running || lockouts.some((l) => l.method === "remote" && l.status === "pending");

    const intro = !lost ? (card.state === "revoked" && has("replacement")
      ? "This card was replaced. Each door stops accepting it once the replacement card is tapped there: tick " +
        "the doors off as that happens."
      : `This card is ${card.state}.`)
      : expired ? "This card has expired, so it opens nothing now. There's nothing more to do."
      : `Reported lost${card.state_by ? ` by ${card.state_by}` : ""}${card.state_at ? ` ${ago(card.state_at).toLowerCase()}` : ""}. ` +
        `Until it expires (${card.expires_label || when(card.expires_at)}) it still opens the doors below. Lock it ` +
        "out of each one in any of these ways; a door is safe once one of them is done there.";

    const section = (head, ...body) => h("section", { class: "lo-method" }, h("h3", null, head), ...body);
    const methods = lost && !expired ? [
      section("1. Lost card notice",
        h("p", { class: "hint" }, "Write a notice onto a spare card (a blank card, or a hotel card nobody " +
          "uses). Tap it on each door below once, then tick the door off. From then on that door refuses the " +
          "lost card."),
        h("div", { class: "lo-actions" }, h("button", { type: "button", class: "btn", onclick: () =>
          run(() => cardDialogJob(`api/keycards/${card.id}/notice`, `Lost card notice for ${card.card_number}`)) },
        has("notice") ? "Write another notice card" : "Write a notice card"))),
      section("2. Replacement card",
        h("p", { class: "hint" }, card.holder_archived ? `${card.holder} is archived, so no new card is made for ` +
          "them. Use a notice card (or remote lock-out) instead."
          : `Make ${card.holder} a new key card that also cancels the lost one. The first ` +
          "time it's tapped on a door, that door refuses the lost card. Tick each door off once it's been tapped. " +
          "It uses their department's key card (or what the lost card opened)."),
        h("div", { class: "lo-actions" }, h("button", { type: "button", class: "btn",
          disabled: !!card.holder_archived, onclick: () =>
          run(() => cardDialogJob(`api/keycards/${card.id}/replace`, `Replacement card for ${card.holder}`)) },
        "Make a replacement card"))),
      section("3. Remote lock-out (experimental)",
        h("p", { class: "hint" }, "For doors with a gateway: TT Hotel tells the door to refuse this card number, " +
          "without anyone walking there. It costs 2 TT Hotel requests per door."),
        h("p", { class: "wb-warn" }, "Permanent: the card number is retired on those doors for good. That " +
          "card can never open them again, even if it's found, cleared and made again."),
        !remote.ready ? h("p", { class: "hint" }, remote.why || "Remote lock-out isn't ready.", " ",
          remote.enabled ? h("button", { type: "button", class: "linkish", onclick: () => openRemoteTest() }, "Test remote lock-out")
            : h("button", { type: "button", class: "linkish", onclick: () => { $("#card-dialog").close(); selectTab("wristbands"); } },
              "Wristbands settings")) : null,
        h("div", { class: "lo-actions" }, h("button", {
          type: "button", class: "btn danger", disabled: !remote.ready || running || !gateways.length,
          title: !remote.ready ? remote.why : !gateways.length ? "None of its doors has a gateway still to do" : null,
          onclick: () => run(() => remoteLockOut(card, gateways)),
        }, running ? "Remote lock-out running…"
          : gateways.length ? `Lock out of ${plural(gateways.length, "gateway door")} remotely` : "No gateway doors left"))),
    ] : [];

    fill($("[data-body]", dialog),
      h("p", { class: "hint lo-intro" }, intro),
      h("div", { class: "lo-summary" }, card.summary, card.cancelled_card ? ` · cancels card ${card.cancelled_card}` : ""),
      byDoor.size ? h("div", { class: "lo-doors" }, doorRows)
        : h("p", { class: "hint" }, "None of the doors this card was made for are in TT Hotel's door list now."),
      ...methods);
  }

  async function remoteLockOut(card, gateways) {
    if (!await askConfirm(`Lock card ${card.card_number} out of ${plural(gateways.length, "door")} through TT Hotel? ` +
      "This is experimental and permanent: the number is retired on those doors for good.", "Lock out remotely")) {
      return false;
    }
    const { card: next } = await api("POST", `api/keycards/${card.id}/lockout`, {});
    await refreshCardDialog(next);
    toast("Remote lock-out started. Each door shows how it went.");
    return false;
  }

  // ---------------------------------------------------------------- remote lock-out: the test

  function renderRemoteStatus() {
    const s = state.settings;
    const tested = Number(s.wb_remote_lockout_tested) || 0;
    const format = s.wb_remote_lockout_api === "plain" ? "as printed" : "reversed";
    $("#wb-remote-status").textContent = s.wb_remote_lockout !== "1" ? "Turned off."
      : tested ? `Tested ${fullDate(tested)} (number format: ${format}): it can be used on lost cards.`
      : "On, not tested yet: it can't be used on lost cards until a test card is refused at a door.";
  }

  $("#wb-remote-test").addEventListener("click", () => run(async () => {
    if (wbDirty) throw new Error("Click Save wristband settings first: the test uses what's saved.");
    if (state.settings.wb_remote_lockout !== "1") {
      throw new Error("Tick Offer remote lock-out for lost cards and save first.");
    }
    return openRemoteTest();
  }));

  const remoteRun = { step: "choose", doorId: null, message: "" };

  async function openRemoteTest() {
    const dialog = $("#remote-dialog");
    if (!wbDoors) await loadDoors();
    if (!dialog.open) {
      remoteRun.step = "choose";
      remoteRun.message = "";
      dialog.showModal();
    }
    drawRemoteTest();
    return false;
  }

  $("[data-close]", $("#remote-dialog")).addEventListener("click", () => $("#remote-dialog").close());
  $("#remote-dialog").addEventListener("close", () => {
    kc.remote = null;   // the settings say how it stands until the Key cards list is read again
    load().then(() => { renderRemoteStatus(); if (cardView) drawCardDialog(); });
  });

  function drawRemoteTest() {
    const dialog = $("#remote-dialog");
    const body = $("[data-body]", dialog);
    const doors = (wbDoors || []).filter((d) => d.active && d.has_gateway);
    const door = doors.find((d) => d.id === remoteRun.doorId);
    const name = door ? doorName(door) : "the door";
    const format = state.settings.wb_remote_lockout_api === "plain" ? "as printed" : "reversed";
    const step = (n, text) => h("div", { class: "rt-step" }, h("span", { class: "rt-n" }, String(n)), h("div", null, text));
    const note = remoteRun.message ? h("p", { class: "wb-warn" }, remoteRun.message) : null;
    const buttons = (...b) => h("div", { class: "lo-actions" }, ...b);
    const go = (stepName, message = "") => { remoteRun.step = stepName; remoteRun.message = message; drawRemoteTest(); };
    const intro = h("p", { class: "hint" }, "Checks at one door that remote lock-out works with your locks, before " +
      "it's used on a lost card. You need a blank card and a few minutes at a door with a gateway. The test card's " +
      `number is retired on that door for good, which is fine for a test card. Number format now: ${format}.`);

    if (remoteRun.step === "choose") {
      if (!doors.length) {
        body.replaceChildren(intro, h("p", { class: "wb-warn" }, "None of your doors has a gateway, so remote " +
          "lock-out can't be used. Use a lost card notice or a replacement card instead."));
        return;
      }
      const select = h("select", { "aria-label": "Door to test" },
        doors.map((d) => h("option", { value: d.id, selected: d.id === remoteRun.doorId }, doorName(d))));
      fill(body, intro, note,
        step(1, "Choose a door with a gateway, and make a test card for it (it works for a day)."),
        h("label", { class: "rt-door" }, "Door", select),
        buttons(h("button", { type: "button", class: "btn primary", onclick: () => run(async () => {
          remoteRun.doorId = Number(select.value);
          const job = await postCardJob("api/keycards/remote-test/start", { door_id: remoteRun.doorId });
          if (!job) return false;
          const final = await showJob(job, "Remote lock-out test card");
          if (final.status === "done" && final.cards?.some((c) => c.status === "written")) go("made");
          else go("choose", "The test card wasn't made. Try again.");
          return false;
        }) }, "Make test card")));
    } else if (remoteRun.step === "made") {
      fill(body, intro, note,
        step(2, `Tap the test card on ${name}. It should open.`),
        buttons(h("button", { type: "button", class: "btn", onclick: () => go("choose",
          "If the test card doesn't open the door, check the door's time and that it's the right door, then make " +
          "another test card.") }, "It didn't open"),
        h("button", { type: "button", class: "btn primary", onclick: () => go("ready") }, "It opened")));
    } else if (remoteRun.step === "ready") {
      const lockBtn = h("button", { type: "button", class: "btn primary danger-fill" }, "Lock the test card out");
      lockBtn.addEventListener("click", () => run(async () => {
        lockBtn.disabled = true;
        lockBtn.textContent = "Asking TT Hotel…";
        try {
          await api("POST", "api/keycards/remote-test/lockout", {});
        } catch (err) {
          go("ready", `That didn't work: ${err.message}`);
          return false;
        }
        go("check");
        return false;
      }));
      fill(body, intro, note,
        step(3, `Now lock the test card out of ${name} through TT Hotel (2 TT Hotel requests). If the door's ` +
          "gateway is busy, wait a few minutes and click it again."),
        buttons(lockBtn));
    } else if (remoteRun.step === "check") {
      fill(body, intro, note,
        step(4, `Wait a minute, then tap the test card on ${name} again. Did the door refuse it?`),
        buttons(h("button", { type: "button", class: "btn", onclick: () => run(async () => {
          await api("POST", "api/keycards/remote-test/failed", {});
          await load();
          const next = state.settings.wb_remote_lockout_api === "plain" ? "as printed" : "reversed";
          go("choose", `Switched the number format to ${next}. Make a new test card and try again. If neither ` +
            "format works, remote lock-out doesn't work with these locks: use notice or replacement cards.");
          return false;
        }) }, "It still opened"),
        h("button", { type: "button", class: "btn primary", onclick: () => run(async () => {
          await api("POST", "api/keycards/remote-test/passed", {});
          kc.remote = null;
          await load();
          go("done");
          return false;
        }) }, "It was refused")));
    } else {
      body.replaceChildren(h("p", { class: "rt-done" }, "✓ Remote lock-out works with your locks."),
        h("p", { class: "hint" }, "It can now be used on lost cards (Staff tab, Key cards, Lock out). Remember: " +
          "a number locked out remotely is retired on that door for good. Throw the test card away or clear it."),
        buttons(h("button", { type: "button", class: "btn primary", onclick: () => dialog.close() }, "Done")));
    }
  }

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
    const moveIn = others.length ? h("span", { class: "inline-select" }, h("select", {
      "aria-label": "Move a page into this group",
      onchange: (e) => regroup(d.pages.find((p) => p.id === e.target.value), group.id),
    }, h("option", { value: "" }, "Move a page here\u2026"),
    ...others.map((p) => h("option", { value: p.id }, groupOf(p, d) ? `${p.name} (from ${groupOf(p, d).name})` : p.name)))) : null;
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
  // Phones and computers that choose their own pages are kiosk devices. A role-based device shows
  // each person their role's pages (or their own), so for those, pages are given to roles.
  const kioskDevices = () => state.devices.filter((d) => d.status !== "revoked" && d.signin_mode !== "role");
  const anyRoleBased = () => state.devices.some((d) => d.status !== "revoked" && d.signin_mode === "role");
  const choiceShows = (choice, key) => choice == null || choice.includes(key);

  function phonesShowing(key) {
    return kioskDevices().filter((d) => choiceShows(d.layout_pages, key));
  }

  function shownOnText(key) {
    const all = kioskDevices();
    const on = phonesShowing(key);
    const kind = anyRoleBased() ? "kiosk devices" : "devices";
    let text = !state.devices.some((d) => d.status !== "revoked") ? "no devices set up yet"
      : !all.length || !on.length ? `no ${kind}`
      : on.length === all.length ? `all ${kind}` : on.map((d) => d.name).join(", ");
    if (anyRoleBased() && state.roles.length) {
      const roles = state.roles.filter((r) => choiceShows(r.layout_pages, key));
      text += ` \u00b7 roles: ${roles.length === state.roles.length ? "all"
        : roles.map((r) => r.name).join(", ") || "none"}`;
    }
    return text;
  }

  // Pick which kiosk devices (and, with role-based devices about, which roles) show one page
  // (key: its ID) or page group (key: "group:<id>"). Saved straight away: these are device and
  // role settings, not part of the layout draft.
  function choosePhones(key, name) {
    const dialog = $("#item-dialog");
    const body = $("[data-body]", dialog);
    $("[data-error]", dialog).textContent = "";
    const withRoles = anyRoleBased() && state.roles.length > 0;
    $("[data-title]", dialog).textContent = withRoles ? `Who sees \u201c${name}\u201d` : `Devices that show \u201c${name}\u201d`;
    const box = (choice) => h("input", { type: "checkbox", checked: choiceShows(choice, key) });
    const devices = kioskDevices().map((d) => ({ d, input: box(d.layout_pages) }));
    const roles = withRoles ? state.roles.map((r) => ({ r, input: box(r.layout_pages) })) : [];
    const these = key.startsWith("group:") ? "these pages" : "this page";
    body.onclick = null;
    body.oninput = null;
    body.replaceChildren(devices.length || roles.length
      ? h("div", { class: "item-fields" },
        h("p", { class: "hint" }, `Everyone still only sees what their role allows on ${these}. ` +
          "Unticking something that shows all pages switches it to a fixed list of pages, so it " +
          "won't get new pages automatically. Changes here are saved straight away."),
        devices.length ? h("div", { class: "pick-head" }, withRoles ? "Kiosk devices" : "Devices") : null,
        devices.length ? h("div", { class: "member-list" }, devices.map(({ d, input }) => h("label", { class: "check pick-row" },
          input, h("span", null, d.name, h("small", null,
            ` \u00b7 ${d.role_name || "no role"} \u00b7 ${d.layout_pages == null ? "all pages" : "chosen pages"}`))))) : null,
        roles.length ? h("div", { class: "pick-head" }, "Roles, for people on role-based devices") : null,
        roles.length ? h("div", { class: "member-list" }, roles.map(({ r, input }) => h("label", { class: "check pick-row" },
          input, h("span", null, r.name, h("small", null,
            ` \u00b7 ${r.staff_count} ${r.staff_count === 1 ? "person" : "people"} \u00b7 ` +
            `${r.layout_pages == null ? "all pages" : "chosen pages"}`))))) : null)
      : h("p", { class: "hint" }, "Set up a phone or computer on the Phones & computers tab first."));
    const form = $("#item-form");
    // Enter on a checkbox would otherwise "click" the first button, which is Cancel.
    form.onkeydown = (ev) => {
      if (ev.key === "Enter" && ev.target.tagName === "INPUT") ev.preventDefault();
    };
    form.onsubmit = (ev) => {
      if (ev.submitter?.value !== "save") return;
      // Something that showed all pages keeps every page and group that could still exist (saved
      // or in the unsaved draft) except this one, so discarding the draft later can't take
      // pages away from it. Pages and groups that no longer exist are dropped from lists.
      const known = knownChoices();
      const next = (choice, checked) => {
        const current = (choice ?? allChoices()).filter((id) => known.has(id));
        return checked ? [...current, key] : current.filter((id) => id !== key);
      };
      const changes = [
        ...devices.filter(({ d, input }) => input.checked !== choiceShows(d.layout_pages, key))
          .map(({ d, input }) => [d.name, `api/devices/${d.id}`, "PATCH", next(d.layout_pages, input.checked)]),
        ...roles.filter(({ r, input }) => input.checked !== choiceShows(r.layout_pages, key))
          .map(({ r, input }) => [r.name, `api/roles/${r.id}`, "PATCH", next(r.layout_pages, input.checked)]),
      ];
      if (!changes.length) return;
      run(async () => {
        const failed = [];
        for (const [who, path, method, pages] of changes) {
          try {
            await api(method, path, { layout_pages: pages });
          } catch (err) {
            failed.push(`${who} (${err.message})`);
          }
        }
        // Always refresh, so the screen matches what was actually saved.
        await load();
        renderLayoutEditor();
        if (failed.length) throw new Error(`Couldn't update ${failed.join(", ")}.`);
      }, roles.length ? "Saved." : "Devices updated.");
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
  // as: "" (everything on the layout), "d:<id>" (a kiosk device), "r:<id>" (a role) or "s:<id>"
  // (a person): role-based devices show each person their own view, so they're previewed by
  // role or person.
  const preview = { view: "phone", as: "" };
  const previewAs = () => {
    const [kind, id] = preview.as.split(":");
    return { kind, id: Number(id) };
  };
  const PREVIEW_SIZES = { phone: [390, 780], desktop: [1280, 800] };

  window.staffPortalPreview = () => {
    const d = draft();
    return {
      layout: { pages: d.pages, page_groups: d.page_groups, show_unplaced: d.show_unplaced !== false },
      device_id: previewAs().kind === "d" ? previewAs().id : null,
      role_id: previewAs().kind === "r" ? previewAs().id : null,
      staff_id: previewAs().kind === "s" ? previewAs().id : null,
    };
  };

  function openPreview() {
    const dialog = $("#preview-dialog");
    const select = $("#preview-device");
    const devices = kioskDevices();
    const people = state.staff.filter((m) => m.status !== "disabled");
    select.replaceChildren(h("option", { value: "" }, "Everything on the layout"),
      devices.length ? h("optgroup", { label: "Kiosk devices" }, devices.map((d) =>
        h("option", { value: `d:${d.id}` }, `${d.name} (${d.role_name || "no role"})`))) : null,
      state.roles.length ? h("optgroup", { label: "Roles, on role-based devices" }, state.roles.map((r) =>
        h("option", { value: `r:${r.id}` }, r.name))) : null,
      people.length ? h("optgroup", { label: "People, on role-based devices" }, people.map((m) =>
        h("option", { value: `s:${m.id}` }, `${m.name} (${m.role_name || "no role"})`))) : null);
    if (![...select.options].some((o) => o.value === preview.as)) preview.as = "";
    select.value = preview.as;
    select.onchange = () => { preview.as = select.value; loadPreview(); };
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
    const { kind, id } = previewAs();
    const device = kind === "d" && state.devices.find((d) => d.id === id);
    const role = kind === "r" && state.roles.find((r) => r.id === id);
    const person = kind === "s" && state.staff.find((m) => m.id === id);
    $("[data-preview-note]", dialog).textContent = (draft().dirty ? "Showing your unsaved layout changes. " : "") +
      (device ? `What ${device.name} shows, with its role and pages. `
        : role ? `What someone with the ${role.name} role sees on a role-based device, before anyone's own settings. `
        : person ? `What ${person.name} sees on a role-based device, with their role and their own saved settings. `
        : "Everything on the layout, as a device allowed to control all of it would show it. ") +
      "Readings are live. Tapping a control doesn't change anything.";
    const query = new URLSearchParams({ view: preview.view });
    if (device) query.set("device", id);
    if (role) query.set("role", id);
    if (person) query.set("staff", id);
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

  const KIND_NAMES = { layout: "a layout", roles: "roles", staff: "staff members", departments: "departments",
    backup: "a full backup" };
  // The add-on's upload limit (MAX_ADMIN_BODY): a bigger file can't be sent. Files read whole here
  // (exports and migration files) are far smaller unpacked; one past this is something else, and
  // reading it would only use up the browser's memory. A backup to restore is never read whole.
  const MAX_FILE_BYTES = 96 * 1024 * 1024;
  const PEEK_BYTES = 64 * 1024;   // enough of a file's start to see what it says it is
  const TOO_BIG = "That file is too big to be a Staff Portal file.";
  const NOT_OURS = "That isn't a Staff Portal export or backup file.";
  const MIGRATE_FORMAT = "staff-portal-migrate";
  const MIGRATE_CODE_CHARS = 20;  // the code's letters and numbers, shown in groups of 4
  const NOT_MIGRATION = "That isn't a Staff Portal migration file.";

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

  function saveBlob(blob, name) {
    const url = URL.createObjectURL(blob);
    const a = h("a", { href: url, download: name });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // Downloads go through fetch so a refusal shows as a message instead of a broken file.
  async function downloadFile(path) {
    const res = await fetch(path, { cache: "no-store" });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `Download failed (${res.status}).`);
    const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || "staff-portal.json";
    saveBlob(await res.blob(), name);
  }

  // A file's contents, unpacked if it's gzipped (as backups are saved, to keep them small) or as
  // it is (older backups, or one a browser unpacked), up to `upTo` bytes: { parts, more }, more
  // being whether there was more past that. Gzip is told apart by its first two bytes, not the
  // file name. wrongFile is the message for a damaged one.
  async function readFileStart(blob, upTo, wrongFile) {
    if (blob.size > MAX_FILE_BYTES) throw new Error(TOO_BIG);
    const head = new Uint8Array(await blob.slice(0, 2).arrayBuffer());
    let stream = blob.stream();
    if (head[0] === 0x1f && head[1] === 0x8b) {
      if (typeof DecompressionStream !== "function") {
        throw new Error("This browser can't open compressed files. Update it, or use a current Chrome, " +
          "Edge, Firefox or Safari.");
      }
      stream = stream.pipeThrough(new DecompressionStream("gzip"));
    }
    // Read piece by piece, so a small file that unpacks to something huge is stopped early.
    const reader = stream.getReader();
    const parts = [];
    let size = 0;
    for (;;) {
      let chunk;
      try { chunk = await reader.read(); } catch { throw new Error(wrongFile); } // damaged gzip
      if (chunk.done) return { parts, more: false };
      size += chunk.value.length;
      parts.push(chunk.value);
      if (size > upTo) {
        reader.cancel().catch(() => { /* already stopping */ });
        return { parts, more: true };
      }
    }
  }

  // Reads a Staff Portal file as JSON. wrongFile is the message for anything that isn't JSON.
  async function readJsonFile(blob, wrongFile) {
    const { parts, more } = await readFileStart(blob, MAX_FILE_BYTES, wrongFile);
    if (more) throw new Error(TOO_BIG);
    try { return JSON.parse(await new Blob(parts).text()); } catch { throw new Error(wrongFile); }
  }

  // What a file says it is, from its start alone: { format, kind }, either missing if the start
  // doesn't say (Staff Portal files begin with both; another tool may have reordered them).
  async function peekFile(blob, wrongFile) {
    const { parts } = await readFileStart(blob, PEEK_BYTES, wrongFile);
    const text = new TextDecoder().decode(await new Blob(parts).arrayBuffer());
    if (!/^\s*\{/.test(text)) throw new Error(wrongFile);   // not a JSON object
    const said = /^\s*\{\s*"format"\s*:\s*"([^"\\]{0,40})"(?:\s*,\s*"kind"\s*:\s*"([^"\\]{0,40})")?/.exec(text);
    return { format: said?.[1], kind: said?.[2] };
  }

  // Ask for a file and read it as a Staff Portal file of the expected kind: its data, or with
  // asPicked, the file itself, to upload as it is. Then only its start is read: a backup with a
  // full log and card history can be hundreds of megabytes unpacked, and the add-on checks all
  // of it anyway (including whatever the start doesn't say).
  function pickFile(kind, asPicked = false) {
    return new Promise((resolve, reject) => {
      const input = $("#import-file");
      input.value = "";
      input.onchange = async () => {
        const file = input.files[0];
        if (!file) return resolve(null);
        try {
          const data = asPicked ? await peekFile(file, NOT_OURS) : await readJsonFile(file, NOT_OURS);
          if (data?.format === MIGRATE_FORMAT) {
            throw new Error("That's a migration file. Open it with Restore from migration file… under Migrate.");
          }
          const says = (value) => !asPicked || value !== undefined;
          if (says(data?.format) && data?.format !== "staff-portal") throw new Error(NOT_OURS);
          if (says(data?.kind) && data.kind !== kind) {
            throw new Error(`That file holds ${KIND_NAMES[data.kind] || "something else"}, not ${KIND_NAMES[kind]}.`);
          }
          resolve(asPicked ? file : data);
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

  // Roles, departments and staff are added to what's here; same names get the admin's choice.
  async function importNamed(kind, file) {
    const list = Array.isArray(file[kind]) ? file[kind] : [];
    const have = new Set((state[kind] || []).map((x) => x.name.toLowerCase()));
    const clashes = list.filter((x) => have.has(String(x?.name || "").trim().toLowerCase())).map((x) => x.name);
    const word = { roles: ["role", "roles"], departments: ["department", "departments"] }[kind]
      || ["staff member", "staff members"];
    let mode = "copy";
    if (clashes.length) {
      const shown = clashes.slice(0, 5).join(", ") + (clashes.length > 5 ? `, and ${clashes.length - 5} more` : "");
      mode = await askChoice(`${clashes.length} ${clashes.length === 1 ? word[0] : word[1]} in the file already ` +
        `exist${clashes.length === 1 ? "s" : ""} here: ${shown}. What should happen to ${clashes.length === 1 ? "it" : "them"}?`, [
        ["skip", "Skip them", `Keep yours and only add the ${word[1]} that are new.`],
        ["copy", "Keep both", "Add the file's version with “(imported)” after the name."],
        ["replace", "Replace them", kind === "roles"
          ? "Use the file's access settings. Phones keep their role."
          : kind === "departments" ? "Use the file's smart group, role, key card and clock-in rule. People stay in them."
          : "Use the file's PIN and settings. Anyone whose PIN changes is signed out, people you've " +
            "turned off stay off, people you've archived stay archived, and Home Assistant links are kept.", "danger"],
      ]);
      if (!mode) return false;
    }
    const result = await api("POST", `api/import/${kind}`, { file, mode });
    await load();
    // A role open in the editor that the import just replaced must show the imported access,
    // or saving it would quietly put the old access back.
    let reloaded = false;
    if (kind !== "departments" && editing && editing.kind === (kind === "roles" ? "role" : "person") && mode === "replace" &&
        clashes.some((n) => String(n).trim().toLowerCase() === editing.name.trim().toLowerCase())) {
      editing.dirty = false;
      refreshEditing();
      reloaded = true;
    }
    const notes = [`${result.added} added`, result.replaced && `${result.replaced} replaced`,
      result.skipped && `${result.skipped} skipped`].filter(Boolean).join(", ");
    const extra = [
      result.missing_areas?.length && `No area here called ${result.missing_areas.join(", ")}; that access was left out.`,
      result.missing_phones?.length && `No phone here called ${result.missing_phones.join(", ")}.`,
      result.missing_roles?.length && `No role here called ${result.missing_roles.join(", ")}, so those people kept the role they had (or have none).`,
      result.missing_pages?.length && `No page or page group here called ${result.missing_pages.join(", ")}; check those pages in Roles.`,
      result.bad_pin && `${result.bad_pin} left out because their PIN in the file was damaged.`,
      result.kept_off?.length && `${result.kept_off.join(", ")} stayed turned off.`,
      result.kept_archived?.length && `${result.kept_archived.join(", ")} stayed archived.`,
    ].filter(Boolean).join(" ");
    toast([`Imported ${word[1]}: ${notes}.`, extra, reloaded ? `The ${kind === "roles" ? "role" : "person"} open on the Roles tab was reloaded.` : ""]
      .filter(Boolean).join(" "));
    return false;
  }

  $("#restore-backup").addEventListener("click", () => run(async () => {
    const file = await pickFile("backup", true);
    if (!file) return false;
    // The file goes up as it was picked: a backup as downloaded is gzip-compressed, a tenth the
    // size of its JSON (the activity log and card history make that tens of megabytes or more).
    const upload = new FormData();
    upload.append("file", file, file.name);
    return restoreBackup(upload);
  }));

  // body: the backup file as an upload (FormData), or { safety_backup: name } for a copy saved
  // here before an earlier restore. It's sent twice: to preview, then to restore.
  async function restoreBackup(body) {
    const info = await api("POST", "api/restore/preview", body);
    const counts = [`${info.roles} role${info.roles === 1 ? "" : "s"}`, `${info.devices} phone${info.devices === 1 ? "" : "s"}`,
      `${info.staff} staff member${info.staff === 1 ? "" : "s"}`, `${info.pages} layout page${info.pages === 1 ? "" : "s"}`];
    // Backups from 0.8.6 on carry the activity log and card history; older ones don't say.
    if (info.activity > 0) counts.push(`${info.activity} activity log entr${info.activity === 1 ? "y" : "ies"}`);
    if (info.wb_cards > 0) counts.push(`${info.wb_cards} card${info.wb_cards === 1 ? "" : "s"} in the card history`);
    // Phones this system didn't sign in (another system's backup, a revoked phone, an edited
    // file) come back needing setup.
    const setup = info.needs_setup || [];
    const setupNote = !setup.length ? ""
      : `\n\n${setup.length === 1 ? `${setup[0]} needs` : `${setup.length} phones and computers need`} ` +
        `to be set up again after this restore${setup.length === 1 ? "" : ` (${setup.slice(0, 8).join(", ")}` +
        `${setup.length > 8 ? ", \u2026" : ""})`}, because ${setup.length === 1 ? "it wasn't" : "they weren't"} ` +
        "signed in on this system.";
    // People archived here since the backup was made stay archived (Restore brings them back).
    const kept = info.kept_archived || [];
    const keptNote = !kept.length ? ""
      : `\n\n${listNames(kept)} ${kept.length === 1 ? "is" : "are"} archived here but not in the backup, so ` +
        `${kept.length === 1 ? "stays" : "stay"} archived. Restore ${kept.length === 1 ? "them" : "any of them"} ` +
        "from Show archived on the Staff tab if you want them back.";
    const ok = await askConfirm(
      `Restore the backup of ${info.property_name || "Staff Access"} from ${fullDate(info.exported_at)}?\n\n` +
      `It has ${counts.join(", ")}. Everything in Staff Access now (settings, roles, layout, phones and staff) ` +
      "is replaced. The activity log and card history are kept, and any entries in the backup that aren't " +
      "here yet are added. A copy of the current setup is saved first, so you can undo this." +
      setupNote + keptNote,
      "Replace and restore");
    if (!ok) return false;
    const result = await api("POST", "api/restore", body);
    await reloadEverything();
    const stayed = result.kept_archived || [];
    toast("Backup restored. Your previous setup was saved first; to undo, restore it from Saved before restoring." +
      (stayed.length ? ` ${listNames(stayed)} stayed archived.` : ""));
    return false;
  }

  // After a restore or a migration replaced everything: unsaved edits belong to the old setup.
  async function reloadEverything() {
    editing = null;
    layoutDraft = null;
    wbDirty = false;
    connDirty.tt = connDirty.ct = false;
    wbDoors = null; // door names and hidden doors come back from the file too
    await load();
    renderRoleEditor();
    refreshIcon();  // the logo may have changed
    loadSafetyBackups();
  }

  async function loadSafetyBackups() {
    const box = $("#safety-backups");
    let list = [];
    try { list = (await api("GET", "api/safety-backups")).backups; } catch { /* shown as empty */ }
    if (!list.length) { box.replaceChildren(); return; }
    box.replaceChildren(
      h("div", { class: "pick-head" }, "Saved before restoring"),
      h("p", { class: "hint" }, "Each restore or migration first saves what it replaced. Restore one of these " +
        "to undo a restore. The last 5 are kept. A migration can't be undone this way (the secrets and " +
        "history it brought stay): to start over, uninstall this add-on and install it again."),
      h("div", { class: "safety-list" }, list.map((b) => h("div", { class: "safety-row" },
        h("div", { class: "entity-text" },
          h("div", { class: "entity-name" }, `Before the restore on ${fullDate(b.saved_at)}`),
          h("div", { class: "entity-id" }, `${Math.max(1, Math.round(b.size / 1024))} KB`)),
        h("div", { class: "area-actions" },
          h("button", { class: "btn", onclick: () => run(() => downloadFile(`api/safety-backups/${b.name}`)) }, "Download"),
          // Restored straight from where it's saved: nothing needs to travel.
          h("button", { class: "btn", onclick: () => run(() => restoreBackup({ safety_backup: b.name })) },
            "Restore this"))))));
  }

  // ---------------------------------------------------------------- migrate to another install

  // The migration file just made, kept only while its code is on screen so it can be saved
  // again if the browser blocked the download. Neither is ever stored anywhere.
  let migration = null;

  const codeChars = (text) => String(text || "").toUpperCase().replace(/[^0-9A-Z]/g, "");
  const groupCode = (chars) => chars.replace(/(.{4})(?=.)/g, "$1-");

  $("#create-migration").addEventListener("click", (ev) => {
    const button = ev.currentTarget;
    run(async () => {
      button.disabled = true;
      try {
        const made = await api("POST", "api/migrate/create", {});
        if (!made.code || !made.file) throw new Error("The migration file didn't arrive. Try again.");
        const text = typeof made.file === "string" ? made.file : JSON.stringify(made.file);
        migration = { blob: new Blob([text], { type: "application/json" }),
          name: made.filename || "staff-portal-migrate.json" };
        showMigrationCode(groupCode(codeChars(made.code)), made.expires_at);
        saveBlob(migration.blob, migration.name);
      } finally {
        button.disabled = false;
      }
      return false;
    });
  });

  // The code is shown this once: the add-on keeps no copy it could show again.
  function showMigrationCode(code, expires) {
    const box = $("#migration-code");
    // Each group can wrap on its own on a narrow screen; the dashes stay with the code.
    const groups = code.split("-").flatMap((g, i) => (i ? ["-", h("wbr"), g] : [g]));
    const codeEl = h("div", { class: "migrate-code", translate: "no" }, groups);
    box.replaceChildren(
      h("div", { class: "pick-head" }, "Write down this code now"),
      h("p", { class: "migrate-warn" }, "It's shown only this once and isn't saved anywhere. Without it, " +
        "the file can't be opened: you'd have to make a new one."),
      codeEl,
      h("div", { class: "backup-actions" },
        h("button", { type: "button", class: "btn primary", onclick: () => copyCode(codeEl, code) }, "Copy code"),
        h("button", { type: "button", class: "btn", onclick: () => saveBlob(migration.blob, migration.name) },
          "Download the file again"),
        h("button", { type: "button", class: "btn subtle", onclick: hideMigrationCode }, "I've written it down")),
      h("p", { class: "hint" }, `The file and code work ${expires ? `until ${fullDate(expires)}` : "for 24 hours"}, ` +
        "on a new install only. " +
        "Keep them apart and private: together they open everything, secrets included. Changes made here " +
        "from now on aren't in the file, so stop this add-on next (step 2)."));
    $("#migration-done").hidden = true;
    box.hidden = false;
    box.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  // The clipboard API can be blocked inside Home Assistant's frame; fall back to the old way.
  async function copyCode(el, code) {
    try {
      await navigator.clipboard.writeText(code);
    } catch {
      getSelection().selectAllChildren(el);
      if (!document.execCommand("copy")) {
        toast("Couldn't copy it here. Select the code and copy it, or write it down.", true);
        return;
      }
    }
    toast("Code copied. Keep it somewhere private, away from the file.");
  }

  async function hideMigrationCode() {
    if (!await askConfirm("Hide the code? It can't be shown again. If you haven't written it down, " +
      "you'll need to make a new migration file.", "Hide code")) return;
    migration = null;
    $("#migration-code").replaceChildren();
    $("#migration-code").hidden = true;
  }

  $("#restore-migration").addEventListener("click", openMigrateDialog);

  function openMigrateDialog() {
    const dialog = $("#migrate-dialog");
    const form = $("#migrate-form");
    const fileInput = $("[data-file]", form);
    const fileName = $("[data-file-name]", form);
    const fileInfo = $("[data-file-info]", form);
    const codeInput = $("[data-code]", form);
    const error = $("[data-error]", form);
    const submit = $("button[value=restore]", form);
    let chosen = null;     // the migration file, read and checked
    let typed = "";        // the code's letters and numbers as last formatted
    let busy = false;      // restoring: the dialog stays open to show how it went

    form.reset();
    fileName.textContent = "No file chosen";
    fileInfo.hidden = true;
    error.textContent = "";
    // Only a new install takes a migration file. The add-on decides (and says why, the same way,
    // in the state); this just says so early.
    $("[data-not-new-why]", form).textContent = state.notFresh || "";
    $("[data-not-new]", form).hidden = !state.notFresh;

    const close = () => {
      form.reset(); // the code doesn't stay in the page once the dialog is closed
      chosen = null;
      dialog.close();
    };

    fileInput.onchange = async () => {
      const file = fileInput.files[0];
      chosen = null;
      error.textContent = "";
      fileInfo.hidden = true;
      fileName.textContent = file ? file.name : "No file chosen";
      if (!file) return;
      try {
        const data = await readJsonFile(file, NOT_MIGRATION);
        if (data?.format === "staff-portal") {
          throw new Error("That's a backup or export file, not a migration file. Make a migration file on the " +
            "other install with Create migration file.");
        }
        if (data?.format !== MIGRATE_FORMAT) throw new Error(NOT_MIGRATION);
        chosen = data;
        // The header is readable without the code; the add-on checks it again (it's sealed in).
        const made = Number(data.created_at) || 0;
        const expires = Number(data.expires_at) || 0;
        const expired = expires > 0 && expires * 1000 < Date.now();
        const by = data.app_version ? ` by Staff Portal ${String(data.app_version).slice(0, 20)}` : "";
        fileInfo.textContent = [made && `Made ${fullDate(made)}${by}.`, expired
          ? `It stopped working ${fullDate(expires)}: make a new one on the other install.`
          : expires && `It works until ${fullDate(expires)}.`].filter(Boolean).join(" ");
        fileInfo.classList.toggle("migrate-warn", expired);
        fileInfo.hidden = !fileInfo.textContent;
        codeInput.focus();
      } catch (err) {
        fileInput.value = "";
        fileName.textContent = "No file chosen";
        error.textContent = err.message;
      }
    };

    // Shows the code in groups of 4 as it's typed or pasted, whatever spacing was used.
    codeInput.oninput = (ev) => {
      let chars = codeChars(codeInput.value);
      let caret = codeChars(codeInput.value.slice(0, codeInput.selectionStart ?? codeInput.value.length)).length;
      // Backspace just after a dash takes the character before it, as if the dash weren't there.
      if (ev.inputType === "deleteContentBackward" && chars === typed && caret > 0) {
        chars = chars.slice(0, caret - 1) + chars.slice(caret);
        caret -= 1;
      }
      chars = chars.slice(0, 32); // a little room past 20, so a longer code is still sent whole
      caret = Math.min(caret, chars.length);
      typed = chars;
      codeInput.value = groupCode(chars);
      const at = caret + Math.floor(Math.max(0, caret - 1) / 4);
      codeInput.setSelectionRange(at, at);
      if (error.textContent) error.textContent = "";
    };

    form.onsubmit = async (ev) => {
      ev.preventDefault();
      error.textContent = "";
      const code = codeChars(codeInput.value);
      if (!chosen) {
        error.textContent = "Choose the migration file first.";
        return;
      }
      if (code.length < MIGRATE_CODE_CHARS) {
        error.textContent = `Type the whole code: ${MIGRATE_CODE_CHARS} letters and numbers, in 5 groups of 4.`;
        codeInput.focus();
        return;
      }
      const cancel = $("button[value=cancel]", form);
      busy = true;
      submit.disabled = cancel.disabled = true;
      submit.textContent = "Restoring…";
      try {
        const done = await api("POST", "api/migrate/restore", { file: chosen, code: groupCode(code) });
        busy = false;
        close();
        await reloadEverything();
        showMigrated(done.summary && done.summary.hr_problem);
      } catch (err) {
        error.textContent = err.message;
      } finally {
        busy = false;
        submit.disabled = cancel.disabled = false;
        submit.textContent = "Restore";
      }
    };
    $("button[value=cancel]", form).onclick = close;
    dialog.oncancel = (ev) => { // Esc: clear the code too, but not while restoring
      ev.preventDefault();
      if (!busy) close();
    };
    dialog.showModal();
  }

  // What's left to do once everything has arrived here.
  function showMigrated(hrProblem) {
    const box = $("#migration-done");
    box.replaceChildren(
      h("div", { class: "pick-head" }, "Migration finished"),
      ...(hrProblem ? [h("p", { class: "form-error" }, hrProblem)] : []),
      h("p", { class: "hint" }, "Everything from the other install is here, and phones and computers stay " +
        "signed in. Staff sign in again with their PINs."),
      h("ol", { class: "migrate-steps" },
        h("li", null, "Check that a phone or two and the front desk computers still work, and look over " +
          "Staff, Roles and Settings here."),
        h("li", null, "Uninstall the old add-on and remove its repository. Don't start it again: it still " +
          "knows the same phones and computers, and it uses the same port."),
        h("li", null, "Delete the migration file and the note of its code.")),
      h("div", { class: "backup-actions" },
        h("button", { type: "button", class: "btn", onclick: () => { box.hidden = true; } }, "Done")));
    $("#migration-code").hidden = true;
    box.hidden = false;
    box.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  // ---------------------------------------------------------------- HR portal

  // The HR portal runs on its own address (port 8788). Here the owner turns it on, chooses the HR
  // admins and shows their setup QR codes, uses the emergency controls and sees security events.
  // HR's own work and settings are in the HR portal itself.
  const hr = { data: null, timer: null };
  const HR_STATES = { off: ["Off", ""], on: ["On", "active"], locked: ["Locked: needs the recovery key", "locked"],
    lost: ["Key missing", "locked"] };

  // ---------------------------------------------------------------- reports

  // Each report: on or off, and which departments may view and manage it (manage includes view).
  async function openReports() {
    let data;
    try {
      data = await api("GET", "api/reports");
    } catch (err) {
      toast(err.message, true);
      return;
    }
    const lane = data.lane;
    $("#reports-lane").textContent = `${lane.used_today} of its ${lane.daily_cap} requests used ` +
      `today (at most ${lane.minute_cap} a minute).` + (lane.paused ? ` ${lane.paused}` : "");
    const list = $("#report-list");
    if (!data.reports.length) {
      list.replaceChildren(h("div", { class: "empty" }, "No reports are installed."));
      return;
    }
    list.replaceChildren(...data.reports.map(reportCard));
  }

  function reportCard(r) {
    const depts = state.departments || [];
    const picks = (kind, chosen) => depts.length ? h("div", { class: "report-depts" }, depts.map((d) =>
      h("label", { class: "check-row" }, h("input", { type: "checkbox", "data-kind": kind, value: d.id,
        checked: chosen.includes(d.id) }), h("span", null, d.name))))
      : h("p", { class: "hint" }, "Add departments on the Staff tab first.");
    const enabled = h("input", { type: "checkbox", checked: r.enabled });
    const card = h("div", { class: "card report-card" },
      h("div", { class: "card-head" }, h("h3", null, r.name)),
      r.description ? h("p", { class: "hint" }, r.description) : null,
      h("label", { class: "check-row" }, enabled, h("span", null, "Turn on")),
      h("h4", null, "Who may view it"), picks("view", r.view),
      h("h4", null, "Who may manage it"),
      h("p", { class: "hint" }, "Upload its files and change its settings. They can view it too."),
      picks("manage", r.manage),
      r.uploads.length ? h("div", null, h("h4", null, "Uploaded files"), r.uploads.map((u) =>
        h("p", { class: "hint" }, `${u.name}: `, u.files.length
          ? `${u.files[0].name}, ${ago(u.files[0].uploaded_at).toLowerCase()} by ${u.files[0].by || "someone"}`
          : "none yet"))) : null,
      h("div", { class: "card-actions" }, h("button", { class: "btn primary", type: "button",
        onclick: () => run(async () => {
          const chosen = (kind) => [...card.querySelectorAll(`input[data-kind="${kind}"]:checked`)]
            .map((i) => Number(i.value));
          await api("PUT", `api/reports/${r.key}`, { enabled: enabled.checked, view: chosen("view"),
            manage: chosen("manage") });
          await openReports();
        }, "Saved. Staff see the change within a few seconds.") }, "Save")));
    return card;
  }

  async function openHr() {
    try {
      hr.data = await api("GET", "api/hr");
    } catch (err) {
      toast(err.message, true);
      return;
    }
    renderHr();
  }

  function renderHr() {
    const d = hr.data;
    if (!d) return;
    showState($("#hr-badge"), HR_STATES[d.status] || ["", ""]);
    // The two HR connections (Settings → Connections) say whether the HR portal is on.
    state.hr = { ...state.hr, status: d.status, connecteam: d.connecteam };
    renderConnections();
    const form = $("#hr-form");
    if (!form.dataset.dirty) {
      form.elements.hr_enabled.checked = d.enabled;
      form.elements.hr_host.value = d.host;
    }
    const on = d.status === "on";
    $("#hr-where").replaceChildren(...(on && d.url ? ["Open it at ", h("a", { href: d.url, target: "_blank", rel: "noopener noreferrer" }, d.url),
      ". In Cloudflare, give this address its own cache rule “Bypass cache”, and turn off Rocket Loader and Email Obfuscation for it."]
      : d.status === "off" ? [d.connecteam_on ? "Off." : "Off. Turn on Connecteam first (Settings → Connections)."]
      : [d.status === "lost" ? "The HR key is missing and there's no locked copy, so HR records here can't be opened." : ""]));
    $("#hr-unlock").hidden = d.status !== "locked";
    for (const id of ["#hr-admins-box", "#hr-emergency", "#hr-events-box"]) $(id).hidden = !on;
    if (!on) return;
    $("#hr-admins").replaceChildren(...(d.admins.length ? d.admins.map(hrAdminRow)
      : [h("p", { class: "hint" }, "No HR admins yet. Add yourself first, then scan your setup QR code.")]));
    const sync = d.sync || {};
    $("#hr-sync-status").textContent = [
      `${d.people} active ${d.people === 1 ? "person" : "people"} from Connecteam.`,
      sync.warning || "",
      sync.error ? `Last sync failed: ${sync.error}` : sync.at ? `Synced ${ago(sync.at).toLowerCase()}.` : "Not synced yet.",
    ].filter(Boolean).join(" ");
    $("#hr-events").replaceChildren(...(d.events.length ? d.events.map((e) => h("tr", null,
      h("td", null, when(e.ts)),
      h("td", null, e.action, e.target ? ` · ${e.target}` : "", e.detail ? h("div", { class: "sub" }, e.detail) : null),
      h("td", null, e.actor, e.ip ? h("div", { class: "sub" }, e.ip) : null)))
      : [h("tr", null, h("td", { colspan: "3", class: "sub" }, "Nothing yet."))]));
  }

  function hrAdminRow(a) {
    const notes = [a.totp_on ? "Authenticator set up" : "Authenticator not set up yet",
      a.locked ? "Locked" : null, a.has_code ? null : "No kiosk code in Connecteam",
      a.last_signin_at ? `Last signed in ${ago(a.last_signin_at).toLowerCase()}` : "Never signed in"].filter(Boolean);
    return h("div", { class: "item-row" },
      h("div", { class: "item-fields" }, h("strong", null, a.name), h("span", { class: "hint" }, notes.join(" · "))),
      h("div", { class: "backup-actions" },
        h("button", { type: "button", class: "btn", onclick: () => hrSetupQr(a) }, "Setup QR code"),
        h("button", { type: "button", class: "btn danger", onclick: async () => {
          if (!(await askConfirm(`Remove ${a.name} as an HR admin? They keep their HR sign-in as an employee.`, "Remove"))) return;
          await run(async () => {
            await api("PUT", "api/hr/admins", { person_ids: hr.data.admins.filter((x) => x.id !== a.id).map((x) => x.id) });
            await openHr();
          }, `${a.name} is no longer an HR admin.`);
        } }, "Remove")));
  }

  function hrDialog(title, ...body) {
    const dialog = $("#hr-dialog");
    dialog.replaceChildren(h("form", { method: "dialog" }, h("h2", null, title), ...body,
      h("div", { class: "dialog-actions" }, h("button", { class: "btn primary", value: "ok" }, "Done"))));
    dialog.showModal();
    return dialog;
  }

  async function hrSetupQr(a) {
    await run(async () => {
      const r = await api("POST", `api/hr/admins/${encodeURIComponent(a.id)}/setup`);
      const link = h("input", { readonly: true, value: r.url, "aria-label": "Setup link" });
      hrDialog(`Set up ${a.name}`,
        h("ol", { class: "qr-steps" },
          h("li", null, "Install an authenticator app on the phone, such as Google Authenticator or Microsoft Authenticator."),
          h("li", null, "Point the phone's camera at this code and open the link."),
          h("li", null, "Type the Connecteam kiosk code, scan the next code into the authenticator app, and save the recovery codes.")),
        h("div", { class: "qr" }, h("img", { src: r.qr, alt: "Setup QR code" })),
        h("div", { class: "copy-row" }, link, h("button", { class: "btn", type: "button", onclick: async () => {
          try { await navigator.clipboard.writeText(r.url); toast("Link copied."); } catch { link.select(); }
        } }, "Copy link")),
        h("p", { class: "hint" }, `It works once, until ${when(r.expires_at)}. Whoever opens it with ${a.name}'s kiosk code sets up their sign-in, so only show it to them.`));
      await openHr();
    });
  }

  $("#hr-form").addEventListener("input", (e) => { e.currentTarget.dataset.dirty = "1"; });
  $("#hr-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    const turningOff = !form.elements.hr_enabled.checked && hr.data?.enabled;
    run(async () => {
      if (turningOff && !(await askConfirm("Turn the HR portal off? Everyone is signed out of it and it only says it's turned off until you turn it on again. Nothing in it is deleted.", "Turn off"))) return false;
      const r = await api("PUT", "api/hr/settings", { hr_enabled: form.elements.hr_enabled.checked ? "1" : "0",
        hr_host: form.elements.hr_host.value });
      delete form.dataset.dirty;
      await openHr();
      if (r.status === "locked") toast("The HR portal is locked: type the HR recovery key below.", true);
    }, "HR portal settings saved.");
  });

  $("#hr-unlock-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = e.currentTarget.elements.recovery_key;
    run(async () => {
      await api("POST", "api/hr/unlock-key", { recovery_key: input.value });
      input.value = "";
      await openHr();
    }, "The HR portal is unlocked.");
  });

  $("#hr-add-admin").addEventListener("click", () => run(async () => {
    const data = await api("GET", "api/hr/people");
    const taken = new Set(hr.data.admins.map((a) => a.id));
    const search = h("input", { type: "search", placeholder: "Search names", autocomplete: "off", "aria-label": "Search names" });
    const list = h("div", { class: "ct-users" });
    const draw = () => {
      const q = search.value.trim().toLowerCase();
      const shown = data.people.filter((p) => p.name.toLowerCase().includes(q));
      list.replaceChildren(...(shown.length ? shown.map((p) => h("button", {
        type: "button", class: "ct-user", disabled: taken.has(p.id), onclick: async () => {
          $("#hr-dialog").close();
          await run(async () => {
            await api("PUT", "api/hr/admins", { person_ids: [...taken, p.id] });
            await openHr();
          }, `${p.name} is an HR admin. Show them their setup QR code next.`);
        } }, h("span", null, p.name), h("small", null, taken.has(p.id) ? "Already an HR admin" : "Make HR admin")))
        : [h("p", { class: "hint" }, data.people.length ? "No one matches." : "No people yet: wait for the first sync from Connecteam.")]));
    };
    search.addEventListener("input", draw);
    hrDialog("Add an HR admin", h("p", { class: "hint" }, "People come from Connecteam."), search, list);
    draw();
    search.focus();
  }));

  $("#hr-sync").addEventListener("click", () => run(async () => {
    const r = await api("POST", "api/hr/sync");
    await openHr();
    toast(r.warning || `Synced: ${r.added} added, ${r.updated} updated, ${r.archived} archived.`, !!r.warning);
  }));

  $("#hr-find").addEventListener("input", () => {
    clearTimeout(hr.timer);
    hr.timer = setTimeout(findHrPeople, 250);
  });

  async function findHrPeople() {
    const q = $("#hr-find").value.trim();
    const box = $("#hr-found");
    if (!q) return box.replaceChildren();
    try {
      const data = await api("GET", `api/hr/people?q=${encodeURIComponent(q)}`);
      box.replaceChildren(...(data.people.length ? data.people.slice(0, 20).map((p) => h("div", { class: "item-row" },
        h("div", { class: "item-fields" }, h("strong", null, p.name),
          h("span", { class: "hint" }, [p.locked ? "Locked" : "Not locked", p.totp_on ? "authenticator on" : "no authenticator"].join(" · "))),
        h("div", { class: "backup-actions" },
          h("button", { type: "button", class: "btn", disabled: !p.locked, onclick: () => run(async () => {
            await api("POST", `api/hr/people/${encodeURIComponent(p.id)}/unlock`);
            await findHrPeople();
          }, `${p.name} is unlocked.`) }, "Unlock"),
          h("button", { type: "button", class: "btn danger", onclick: async () => {
            if (!(await askConfirm(`Reset ${p.name}'s HR sign-in? Their authenticator and recovery codes are removed and they're signed out everywhere.`, "Reset"))) return;
            await run(async () => {
              await api("POST", `api/hr/people/${encodeURIComponent(p.id)}/reset`);
              await openHr();
              await findHrPeople();
            }, `${p.name}'s HR sign-in was reset.`);
          } }, "Reset sign-in"))))
        : [h("p", { class: "hint" }, "No one by that name.")]));
    } catch (err) {
      toast(err.message, true);
    }
  }

  $("#hr-sign-out-all").addEventListener("click", async () => {
    if (!(await askConfirm("Sign everyone out of the HR portal? They can sign in again straight away.", "Sign everyone out"))) return;
    await run(async () => {
      const r = await api("POST", "api/hr/sign-out-everyone");
      toast(`${r.ended} session${r.ended === 1 ? "" : "s"} ended.`);
      await openHr();
    });
  });

  // The HR portal's reads through the Connecteam connection, for its line on the HR tab.
  function hrCtState() {
    const c = state.hr?.connecteam || {};
    if (!state.connecteam?.enabled) return ["Connecteam off", ""];
    if (state.connecteam.halted) return ["Stopped: access refused", "locked"];
    if (c.paused) return ["Waiting", "pending"];
    return ["Reads people", state.hr?.status === "on" ? "active" : ""];
  }

  // Each check with a plain result (Test connection for Connecteam and Google Drive).
  function showChecks(list, checks) {
    list.replaceChildren(...checks.map((c) => h("li", { class: c.ok ? "ok" : "bad" },
      h("span", { class: "conn-mark", role: "img", "aria-label": c.ok ? "OK" : "Problem" }, c.ok ? "\u2713" : "\u2715"),
      h("span", null, h("b", null, c.check), ` ${c.detail}`))));
    list.hidden = false;
  }

  // ---------------------------------------------------------------- start

  $("#version").textContent = `v${SCRIPT_VERSION}`;

  load().then(() => {
    let tab = "phones";
    try { tab = sessionStorage.getItem("tab") || tab; } catch { /* ignore */ }
    selectTab(tab);
  }).catch((err) => toast(err.message, true));
  scheduleLinkCheck(LINK_CHECK_MS);
})();
