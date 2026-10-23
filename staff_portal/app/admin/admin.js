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
    try { data = await res.json(); } catch { data = { error: await res.text().catch(() => "") }; }
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
    state.roles = data.roles;
    state.staff = data.staff;
    state.areas = data.areas;
    state.layout = data.layout;
    state.devices = data.devices;
    state.hasLogo = data.has_logo;
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
        "No phones yet. Add one, then scan its QR code with the phone's camera."));
      return;
    }
    list.replaceChildren(...state.devices.map((d) => {
      const [label, cls] = STATUS[d.status] || [d.status, ""];
      const roleSelect = h("select", { "aria-label": "Role" },
        h("option", { value: "" }, "No role (sees nothing)"),
        state.roles.map((r) => h("option", { value: r.id, selected: r.id === d.role_id }, r.name)));
      roleSelect.addEventListener("change", () => run(async () => {
        await api("PATCH", `api/devices/${d.id}`, { role_id: roleSelect.value || null });
        await load();
      }, "Role changed. The phone updates within a few seconds."));

      const actions = [];
      if (d.status === "pending" && d.setup_url) {
        actions.push(h("button", { class: "btn primary", onclick: () => showQr(d) }, "Show setup QR"));
      }
      if (d.status === "locked") {
        actions.push(h("button", { class: "btn primary", onclick: () => deviceAction(d, "unlock",
          null, "Phone unlocked.") }, "Unlock"));
      }
      if (d.status !== "pending" || !d.setup_url) {
        actions.push(h("button", { class: "btn", onclick: () => deviceAction(d, "reenroll",
          d.status === "active" ? `Sign out ${d.name} and make a new setup QR code?` : null) },
          d.status === "active" ? "Re-pair" : "New setup QR"));
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
              d.signin_mode === "staff"
                ? h("span", { class: "badge shared" }, "Shared \u00b7 staff sign in")
                : d.has_pin ? h("span", { class: "badge" }, "PIN") : null,
              d.ha_user ? h("span", {
                class: d.ha_link_error ? "badge locked" : "badge linked",
                title: d.ha_link_error || "Actions show in Home Assistant as this user",
              }, d.ha_link_error ? `HA user ${d.ha_user}: link broken` : `HA user: ${d.ha_user}`) : null,
              h("span", null, `Last used ${ago(d.last_seen).toLowerCase()}`)),
            d.status !== "revoked" && phonePagesText(d) ? h("div", { class: "meta" }, `Pages: ${phonePagesText(d)}`,
              d.custom_order ? " \u00b7 rearranged on the phone" : "") : null),
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

  // Pages a phone can be given: layout pages plus the automatic More page.
  function layoutPageOptions(pages = state.layout?.pages || [], showMore = state.layout?.show_unplaced !== false) {
    const options = pages.map((p) => ({ id: p.id, name: p.name }));
    if (pages.length && showMore) options.push({ id: "more", name: "More (allowed devices not on any page)" });
    return options;
  }

  // Page IDs a phone's list may refer to: saved pages, pages in the unsaved layout draft, and
  // More. Anything else is a page that no longer exists.
  function knownPageIds() {
    const ids = new Set(["more", ...(state.layout?.pages || []).map((p) => p.id)]);
    for (const p of layoutDraft?.pages || []) ids.add(p.id);
    return ids;
  }

  function phonePagesText(d) {
    if (!(state.layout?.pages || []).length) return null;
    if (d.layout_pages == null) return "All pages";
    const names = layoutPageOptions().filter((o) => d.layout_pages.includes(o.id))
      .map((o) => (o.id === "more" ? "More" : o.name));
    return names.length ? names.join(", ") : "None";
  }

  function pinNote(text) {
    return state.settings.require_pin === "1"
      ? "Required (Settings \u2192 Phone security), 4 to 8 digits. Staff enter it when opening the app."
      : text;
  }

  function editPhone(d) {
    const dialog = $("#phone-dialog");
    const form = $("#phone-form");
    form.reset();
    $("[data-title]", dialog).textContent = d ? `Edit ${d.name}` : "Add phone";
    form.elements["name"].value = d ? d.name : "";
    fillRoleOptions(form.elements["role_id"], d ? d.role_id : state.roles[0]?.id);
    $("[data-pin-hint]", dialog).textContent = d
      ? (d.has_pin ? "Enter a new PIN to change it, or leave blank and tick below to remove it."
        : pinNote("Optional, 4 to 8 digits."))
      : pinNote("Optional, 4 to 8 digits. Staff enter it when opening the app.");
    const mode = form.elements["signin_mode"];
    mode.value = d?.signin_mode || "phone";
    form.elements["staff_all"].checked = d ? !!d.staff_all : true;
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
      const body = { name: form.elements["name"].value, role_id: form.elements["role_id"].value || null };
      body.signin_mode = mode.value;
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
          const known = knownPageIds();
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
          else toast("Phone added. Set the staff app address in Settings to get its QR code.");
        }
      }, d ? "Saved." : null);
    };
    dialog.showModal();
  }

  function showQr(d) {
    const dialog = $("#qr-dialog");
    $("[data-name]", dialog).textContent = d.name;
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

  $("#add-phone").addEventListener("click", () => editPhone(null));

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
              h("span", null, `Last signed in ${ago(m.last_seen).toLowerCase()}`)),
            h("div", { class: "meta" }, phones.length
              ? `Can sign in on: ${phones.join(", ")}`
              : "Not on any shared phone yet"))),
        h("div", { class: "card-actions" }, actions));
    }));
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

  function editStaff(m) {
    const dialog = $("#staff-dialog");
    const form = $("#staff-form");
    form.reset();
    $("[data-title]", dialog).textContent = m ? `Edit ${m.name}` : "Add staff member";
    form.elements["name"].value = m ? m.name : "";
    form.elements["pin"].required = !m;
    $("[data-pin-hint]", dialog).textContent = m
      ? "Leave blank to keep their current PIN."
      : "4 to 8 digits. They enter it after tapping their name.";
    form.elements["active"].checked = !m || m.status !== "disabled";
    form.elements["active"].closest("label").hidden = !m;
    orderResetRow(dialog, m, "staff");
    dialog.onclose = () => {
      if (dialog.returnValue !== "save") return;
      const body = { name: form.elements["name"].value };
      const pin = form.elements["pin"].value.trim();
      if (pin) body.pin = pin;
      if (m) body.active = form.elements["active"].checked;
      run(async () => {
        if (m) await api("PATCH", `api/staff/${m.id}`, body);
        else await api("POST", "api/staff", body);
        await load();
      }, m ? "Saved." : `${body.name} added.`);
    };
    dialog.showModal();
  }

  $("#add-staff").addEventListener("click", () => editStaff(null));

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
    refreshIcon();
  }

  $("#settings-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.target).entries());
    body.ha_logbook = e.target.elements["ha_logbook"].checked ? "1" : "0";
    for (const name of ["allow_phone_reorder", "require_pin", "phones_via_tunnel_only"]) {
      body[name] = e.target.elements[name].checked ? "1" : "0";
    }
    run(async () => {
      await api("PUT", "api/settings", body);
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
  let pageIndex = 0;
  const newId = () => Math.random().toString(36).slice(2, 10);
  const entityById = (id) => entities?.find((e) => e.entity_id === id);
  const entityName = (id) => entityById(id)?.name || id;

  function draft() {
    if (!layoutDraft) {
      layoutDraft = JSON.parse(JSON.stringify(state.layout || { pages: [], show_unplaced: true }));
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
    if (pageIndex >= d.pages.length) pageIndex = Math.max(0, d.pages.length - 1);
    $("#page-list").replaceChildren(...(d.pages.length ? d.pages.map((p, i) => h("li", { class: "sort-row" },
      dragHandle(`Reorder page ${p.name}`),
      h("button", { class: i === pageIndex ? "role-item active" : "role-item",
        onclick: () => { pageIndex = i; renderLayoutEditor(); } },
      h("span", { class: "role-name" }, p.name),
      h("span", { class: "role-meta" }, `${p.items.length} item${p.items.length === 1 ? "" : "s"}`))))
      : [h("li", { class: "empty small" }, "No pages yet. Phones use the automatic layout.")]));
    renderPageEditor();
  }

  sortable($("#page-list"), "pages", (from, to) => {
    const d = draft();
    const selected = d.pages[pageIndex];
    moveItem(d.pages, from, to);
    pageIndex = d.pages.indexOf(selected);
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
    if (item.type === "heading") return ["Heading", item.title, ""];
    if (item.type === "device") {
      return ["Device", item.label || entityName(item.entity_id), item.entity_id];
    }
    if (item.type === "area") {
      const area = state.areas.find((a) => a.id === item.area_id);
      return ["Area", item.title || area?.name || "Deleted area", "Every device in the area the role allows"];
    }
    const onCard = item.entities.filter((m) => m.summary).map((m) => m.label || entityName(m.entity_id));
    return ["Group card", item.title,
      `${item.entities.length} device${item.entities.length === 1 ? "" : "s"}` +
      (onCard.length ? ` \u00b7 card shows ${onCard.join(", ")}` : "")];
  }

  function renderPageEditor() {
    const box = $("#page-editor");
    const d = draft();
    const page = d.pages[pageIndex];
    if (!page) {
      box.replaceChildren(h("div", { class: "empty" },
        "Add a page to start a custom layout, for example \u201cSoaking\u201d or \u201cMechanical.\u201d"));
      return;
    }
    const add = (type) => editItem(page, null, type);
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
          h("button", { class: "btn danger", onclick: async () => {
            if (!await askConfirm(`Delete the ${page.name} page?`, "Delete page")) return;
            d.pages.splice(pageIndex, 1);
            markLayoutDirty();
            renderLayoutEditor();
          } }, "Delete page"))),
      h("div", { class: "shown-on" },
        h("span", null, "Shown on: ", h("strong", null, shownOnText(page.id))),
        h("button", { class: "btn", onclick: () => choosePhones(page) }, "Choose phones")),
      h("div", { class: "add-row" }, "Add:",
        h("button", { class: "btn", onclick: () => add("group") }, "Group card"),
        h("button", { class: "btn", onclick: () => add("device") }, "Device"),
        h("button", { class: "btn", onclick: () => add("area") }, "Whole area"),
        h("button", { class: "btn", onclick: () => add("heading") }, "Heading")),
      page.items.length
        ? itemList(page)
        : h("div", { class: "empty" }, "This page is empty. Add a group card, device, area or heading."));
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

  function phonesShowing(pageId) {
    return state.devices.filter((d) => d.status !== "revoked" &&
      (d.layout_pages == null || d.layout_pages.includes(pageId)));
  }

  function shownOnText(pageId) {
    const all = state.devices.filter((d) => d.status !== "revoked");
    const on = phonesShowing(pageId);
    if (!all.length) return "no phones set up yet";
    if (on.length === all.length) return "all phones";
    if (!on.length) return "no phones";
    return on.map((d) => d.name).join(", ");
  }

  // Pick which phones show one page. Saved straight away (it's a phone setting, not part of
  // the layout draft).
  function choosePhones(page) {
    const dialog = $("#item-dialog");
    const body = $("[data-body]", dialog);
    $("[data-error]", dialog).textContent = "";
    $("[data-title]", dialog).textContent = `Phones that show \u201c${page.name}\u201d`;
    const devices = state.devices.filter((d) => d.status !== "revoked");
    const boxes = devices.map((d) => ({
      d, input: h("input", { type: "checkbox", checked: d.layout_pages == null || d.layout_pages.includes(page.id) }),
    }));
    body.onclick = null;
    body.oninput = null;
    body.replaceChildren(devices.length
      ? h("div", { class: "item-fields" },
        h("p", { class: "hint" }, "Each phone still only sees what its role allows on this page. " +
          "Unticking a phone that shows all pages switches it to a fixed list of pages, so it " +
          "won't get new pages automatically. Changes here are saved straight away."),
        h("div", { class: "member-list" }, boxes.map(({ d, input }) => h("label", { class: "check pick-row" },
          input, h("span", null, d.name, h("small", null,
            ` \u00b7 ${d.role_name || "no role"} \u00b7 ${d.layout_pages == null ? "all pages" : "chosen pages"}`))))))
      : h("p", { class: "hint" }, "Set up a phone on the Phones tab first."));
    const form = $("#item-form");
    // Enter on a checkbox would otherwise "click" the first button, which is Cancel.
    form.onkeydown = (ev) => {
      if (ev.key === "Enter" && ev.target.tagName === "INPUT") ev.preventDefault();
    };
    form.onsubmit = (ev) => {
      if (ev.submitter?.value !== "save") return;
      // Every page that could still exist: saved, in the unsaved draft, and More. A phone that
      // showed all pages keeps all of these except this one, so discarding the draft later
      // can't take pages away from it. Pages that no longer exist are dropped from lists.
      const known = knownPageIds();
      const changes = [];
      for (const { d, input } of boxes) {
        const shows = d.layout_pages == null || d.layout_pages.includes(page.id);
        if (input.checked === shows) continue;
        const current = (d.layout_pages ?? [...known]).filter((id) => known.has(id));
        const next = input.checked ? [...current, page.id] : current.filter((id) => id !== page.id);
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
      }, "Phones updated.");
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
      body.replaceChildren(h("label", null, "Heading text", input));
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
        h("div", { class: "pick-head" }, "Add devices"),
        picker.el));
      validate = () => (!item.title?.trim() ? "Give the card a title."
        : !item.entities.length ? "Add at least one device." : null);
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

  $("#add-page").addEventListener("click", async () => {
    const name = await askText("Page name (for example Soaking or Mechanical):");
    if (!name?.trim()) return;
    const d = draft();
    d.pages.push({ id: newId(), name: name.trim().slice(0, 30), items: [] });
    pageIndex = d.pages.length - 1;
    markLayoutDirty();
    renderLayoutEditor();
  });

  $("#show-unplaced").addEventListener("change", (e) => {
    draft().show_unplaced = e.target.checked;
    markLayoutDirty();
  });

  $("#save-layout").addEventListener("click", () => run(async () => {
    const d = draft();
    const { layout } = await api("PUT", "api/layout", { pages: d.pages, show_unplaced: d.show_unplaced !== false });
    state.layout = layout;
    layoutDraft = null;
    renderLayoutEditor();
  }, "Layout saved. Phones update within a few seconds."));

  $("#discard-layout").addEventListener("click", async () => {
    if (!await askConfirm("Discard your layout changes?", "Discard")) return;
    layoutDraft = null;
    renderLayoutEditor();
  });

  // ---------------------------------------------------------------- start

  $("#version").textContent = `v${SCRIPT_VERSION}`;

  load().then(() => {
    let tab = "phones";
    try { tab = sessionStorage.getItem("tab") || tab; } catch { /* ignore */ }
    selectTab(tab);
  }).catch((err) => toast(err.message, true));
})();
