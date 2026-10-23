(() => {
  "use strict";

  const TOKEN_KEY = "staff_device_token";
  const SESSION_KEY = "staff_pin_session";
  const POLL_MS = 5000;

  const local = storage(() => window.localStorage);
  const session = storage(() => window.sessionStorage);
  const screen = document.getElementById("screen");
  const toastEl = document.getElementById("toast");

  let me = null;
  let tiles = new Map();
  let lastTouch = 0;
  for (const type of ["pointerdown", "keydown"]) {
    document.addEventListener(type, () => { lastTouch = Date.now(); }, { capture: true, passive: true });
  }
  // True while the phone is showing its devices (or an empty-state message about them) and
  // should keep checking for updates, including after the screen has been locked.
  let live = false;
  let pollTimer = null;
  let installPrompt = null;

  const accent = document.querySelector('meta[name="accent"]').content;
  document.documentElement.style.setProperty("--accent", accent);

  // ---------------------------------------------------------------- helpers

  function storage(get) {
    return {
      get(k) { try { return get().getItem(k); } catch { return null; } },
      set(k, v) { try { get().setItem(k, v); } catch { /* storage blocked */ } },
      del(k) { try { get().removeItem(k); } catch { /* storage blocked */ } },
    };
  }

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

  function fromTemplate(id) {
    return document.getElementById(id).content.cloneNode(true);
  }

  function show(node) {
    stopPolling();
    screenKeys = null; // a screen that wants key presses sets this after showing
    tiles = new Map();
    // Keep pop-up messages above the bottom tab bar when there is one.
    document.body.classList.toggle("with-tabs", !!node.querySelector?.(".tabbar"));
    screen.replaceChildren(node);
    drawLock();
  }

  function toast(text, isError) {
    toastEl.textContent = text;
    toastEl.classList.toggle("error", !!isError);
    toastEl.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { toastEl.hidden = true; }, 3000);
  }

  // A computer (mouse and keyboard, not a phone or tablet). It skips the "add to home screen"
  // step and gets the desktop layout's wording.
  function isDesktop() {
    return window.matchMedia("(hover: hover) and (pointer: fine)").matches &&
      !/Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);
  }

  // "phone" or "computer", for messages.
  function word() {
    return (me?.kind || (isDesktop() ? "computer" : "phone")) === "computer" ? "computer" : "phone";
  }

  function isStandalone() {
    return window.navigator.standalone === true ||
      window.matchMedia("(display-mode: standalone)").matches;
  }

  async function api(method, path, body) {
    const headers = { "Content-Type": "application/json" };
    const token = local.get(TOKEN_KEY);
    if (token) headers.Authorization = "Bearer " + token;
    const pinSession = session.get(SESSION_KEY);
    if (pinSession) headers["X-Session"] = pinSession;
    // Tell the server someone is actually using the phone, so their PIN session stays open.
    // Background refreshes on an untouched phone don't, and it signs out when idle.
    if (Date.now() - lastTouch < 60_000) headers["X-Active"] = "1";
    let res;
    try {
      res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
    } catch {
      return { status: 0, data: {} };
    }
    let data = {};
    try { data = await res.json(); } catch { /* empty body */ }
    return { status: res.status, data };
  }

  function message(title, body, retry) {
    live = false;
    const node = fromTemplate("tpl-message");
    node.querySelector("[data-title]").textContent = title;
    node.querySelector("[data-body]").textContent = body;
    if (retry) {
      node.querySelector(".message").append(
        h("button", { class: "btn primary", onclick: () => boot() }, "Try again"));
    }
    document.getElementById("bar").hidden = true;
    show(node);
  }

  // Responses that mean "stop and show a different screen". Returns true if handled.
  function handleAuthError(res) {
    if (res.status === 401) {
      const had = !!local.get(TOKEN_KEY);
      local.del(TOKEN_KEY);
      session.del(SESSION_KEY);
      if (had) {
        message("Access removed",
          `This ${word()} no longer has access. Ask your manager if you think this is a mistake.`);
      } else {
        message(`This ${word()} isn't set up`,
          `Ask your manager for a setup ${word() === "computer" ? "link" : "QR code"}.`);
      }
      return true;
    }
    if (res.status === 423) {
      session.del(SESSION_KEY);
      message("Phone locked",
        `Too many wrong PINs. Ask your manager to unlock this ${word()} from the admin dashboard.`);
      return true;
    }
    if (res.status === 403 && res.data.error === "pin_not_set") {
      message(`This ${word()} needs a PIN`,
        `Ask your manager to set a PIN for this ${word()} before it can be used.`, true);
      return true;
    }
    if (res.status === 403 && res.data.error === "pin_required") {
      session.del(SESSION_KEY);
      askForPin();
      return true;
    }
    if (res.status === 0) {
      message("Can't connect", word() === "computer" ? "Check this computer's network connection." : "Check this phone's Wi-Fi or cell signal.", true);
      return true;
    }
    return false;
  }

  // ---------------------------------------------------------------- startup

  async function boot() {
    const match = location.pathname.match(/^\/e\/([A-Za-z0-9_-]{16,64})$/);
    const code = match && match[1];

    if (local.get(TOKEN_KEY)) {
      const res = await api("GET", "/api/me");
      if (res.status === 200) {
        if (code) history.replaceState(null, "", "/");
        return start(res.data);
      }
      if (!(res.status === 401 && code)) {
        handleAuthError(res) || message("Something went wrong", "Please try again.", true);
        return;
      }
      local.del(TOKEN_KEY); // signed out, but we have a fresh setup code: use it
    }

    if (code) {
      if (isStandalone() || isDesktop() || session.get("use_browser")) return enroll(code);
      return showInstall(code);
    }
    message(`This ${word()} isn't set up`,
      `Ask your manager for a setup ${word() === "computer" ? "link" : "QR code"}.`);
  }

  async function enroll(code) {
    const res = await api("POST", "/api/enroll", { code });
    if (res.status !== 200) {
      if (res.status === 0) return message("Can't connect", `Check this ${word()}'s connection.`, true);
      return message("Setup link didn't work",
        (res.data.error || "Please try again.") +
        ` Ask your manager for a new setup ${word() === "computer" ? "link" : "QR code"}.`);
    }
    local.set(TOKEN_KEY, res.data.token);
    history.replaceState(null, "", "/");
    boot();
  }

  function showInstall(code) {
    live = false;
    const node = fromTemplate("tpl-install");
    const steps = node.querySelector("[data-steps]");
    const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) ||
      (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    const list = ios
      ? ["Tap the Share button (the square with an arrow) at the bottom of Safari.",
         "Scroll down and tap “Add to Home Screen.”",
         "Tap “Add” in the top corner."]
      : ["Tap the ⋮ menu in the top corner of your browser.",
         "Tap “Install app” or “Add to Home screen.”",
         "Confirm by tapping “Install.”"];
    list.forEach((s) => steps.append(h("li", null, s)));
    const box = node.querySelector(".message");
    if (installPrompt) {
      box.insertBefore(h("button", {
        class: "btn primary",
        onclick: async () => { installPrompt.prompt(); installPrompt = null; },
      }, "Install app"), steps);
    }
    node.querySelector("[data-browser]").addEventListener("click", () => {
      session.set("use_browser", "1");
      enroll(code);
    });
    document.getElementById("bar").hidden = true;
    show(node);
  }

  function askForPin() {
    relock(); // whoever signs in next starts with controls locked
    if (me?.shared) showWho();
    else showPin(null);
  }

  // Shared phone: pick your name, then enter your own PIN.
  async function showWho() {
    live = false;
    const res = await api("GET", "/api/me");
    if (res.status !== 200) {
      handleAuthError(res) || message("Something went wrong", "Please try again.", true);
      return;
    }
    me = res.data;
    if (!me.shared) return showPin(null);
    if (!me.staff.length) {
      return message("No one can sign in yet",
        `Ask your manager to add you as a staff member for this ${word()}.`, true);
    }
    const node = fromTemplate("tpl-who");
    node.querySelector("h1").textContent = `Who's using this ${word()}?`;
    const list = node.querySelector("[data-names]");
    for (const member of me.staff) {
      list.append(h("button", { class: "who", onclick: () => showPin(member) },
        h("span", { class: "who-initial" }, member.name.charAt(0).toUpperCase()), member.name));
    }
    document.getElementById("bar").hidden = true;
    show(node);
  }

  function showPin(member) {
    live = false;
    const node = fromTemplate("tpl-pin");
    const dots = node.querySelector("[data-dots]");
    const error = node.querySelector("[data-error]");
    const pad = node.querySelector("[data-pad]");
    if (member) {
      node.querySelector("h1").textContent = `Hi, ${member.name}`;
      node.querySelector("[data-sub]").textContent = "Enter your PIN";
      const back = node.querySelector("[data-back]");
      back.hidden = false;
      back.textContent = `Not ${member.name}?`;
      back.addEventListener("click", () => showWho());
    }
    let pin = "";
    let busy = false;

    const draw = () => {
      dots.replaceChildren(...Array.from({ length: Math.max(4, pin.length) }, (_, i) =>
        h("span", { class: i < pin.length ? "dot on" : "dot" })));
    };
    const submit = async () => {
      if (busy || pin.length < 4) return;
      busy = true;
      const res = await api("POST", "/api/unlock",
        member ? { pin, staff_id: member.id } : { pin });
      busy = false;
      if (res.status === 200) {
        session.set(SESSION_KEY, res.data.session);
        return boot();
      }
      if (res.status === 423 || res.status === 401 || res.status === 0) return handleAuthError(res);
      if (res.status === 404 && member) return showWho();
      pin = "";
      draw();
      error.textContent = res.data.error || "Wrong PIN.";
    };
    const press = (k) => {
      if (k === "del") pin = pin.slice(0, -1);
      else if (k === "ok") return submit();
      else if (pin.length < 8) pin += k;
      error.textContent = "";
      draw();
    };
    for (const k of ["1", "2", "3", "4", "5", "6", "7", "8", "9", "del", "0", "ok"]) {
      const label = k === "del" ? "⌫" : k === "ok" ? "OK" : k;
      pad.append(h("button", {
        class: "key" + (k === "ok" ? " ok" : ""),
        "aria-label": k === "del" ? "Delete" : label,
        onclick: () => press(k),
      }, label));
    }
    draw();
    document.getElementById("bar").hidden = true;
    show(node);
    // On a computer, type the PIN: digits, Backspace, Enter.
    screenKeys = (ev) => {
      if (/^[0-9]$/.test(ev.key)) press(ev.key);
      else if (ev.key === "Backspace") press("del");
      else if (ev.key === "Enter") press("ok");
      else return;
      ev.preventDefault();
    };
  }

  function setSubtitle(staffName) {
    const parts = me.shared && staffName
      ? [staffName, me.role || "No role assigned"]
      : [me.role || "No role assigned", me.device];
    document.getElementById("bar-sub").textContent = parts.filter(Boolean).join(" · ");
  }

  function start(info) {
    me = info;
    document.documentElement.style.setProperty("--accent", info.accent_color);
    document.getElementById("bar-title").textContent = info.property_name;
    setSubtitle(null);
    const logo = document.getElementById("bar-logo");
    logo.hidden = !info.has_logo;
    if (info.has_logo) logo.src = "/logo.png";
    const lockBtn = document.getElementById("lock-btn");
    lockBtn.hidden = !info.pin_required;
    lockBtn.classList.toggle("text", !!info.shared);
    lockBtn.setAttribute("aria-label", info.shared ? "Sign out" : "Lock app");
    lockBtn.querySelector("svg").toggleAttribute("hidden", !!info.shared);
    lockBtn.querySelector("[data-label]").hidden = !info.shared;
    document.getElementById("bar").hidden = false;

    if (info.pin_required && !session.get(SESSION_KEY)) return askForPin();
    show(h("div", { class: "center" }, h("div", { class: "spinner", "aria-label": "Loading" })));
    refresh();
  }

  // ---------------------------------------------------------------- dashboard

  function stopPolling() {
    clearTimeout(pollTimer);
    pollTimer = null;
  }

  async function refresh() {
    if (rearranging) return; // don't replace the Rearrange screen
    stopPolling();
    const res = await api("GET", "/api/entities");
    if (res.status !== 200) {
      if (res.status === 503 || res.status === 0) {
        if (!live) {
          message("Can't reach the system", "It may be restarting. Try again in a minute.", true);
        } else {
          toast("Connection lost. Retrying…", true);
          pollTimer = setTimeout(refresh, POLL_MS);
        }
        return;
      }
      handleAuthError(res) || message("Something went wrong", "Please try again.", true);
      return;
    }
    live = true;
    setSubtitle(res.data.staff_name);
    render(res.data);
    if (document.visibilityState === "visible") pollTimer = setTimeout(refresh, POLL_MS);
  }

  // view: which layout page is open, and which group card's detail screen (if any).
  // The open page is remembered by its ID, so changing which pages a phone has doesn't
  // silently switch staff to a different page.
  const view = { pageId: session.get("page"), detail: null };
  let lastData = null;

  function render(data) {
    lastData = data;
    lock.enabled = !!data.control_lock;
    lock.minutes = data.control_lock_minutes || 10;
    const byId = new Map(data.entities.map((e) => [e.entity_id, e]));
    // Settle which page is open before comparing shapes, so a remembered page that's gone
    // (or the first load) doesn't cause an extra full redraw on the next poll.
    if (data.layout?.pages.length && !data.layout.pages.some((p) => p.id === view.pageId)) {
      view.pageId = data.layout.pages[0].id;
    }
    const shape = JSON.stringify(data.layout
      ? [data.layout, view.pageId, view.detail]
      : data.entities.map((e) => [e.entity_id, e.area]));
    if (shape !== render.shape || !tiles.size) {
      render.shape = shape;
      if (!data.entities.length) {
        show(h("div", { class: "center message" },
          h("h1", null, "Nothing here yet"),
          h("p", null, `Your manager hasn't given this ${word()} access to anything.`)));
        render.shape = null;
        return;
      }
      if (data.layout && !data.layout.pages.length) {
        show(h("div", { class: "center message" },
          h("h1", null, `Nothing on this ${word()} yet`),
          h("p", null, `None of this ${word()}'s pages have anything it's allowed to see. ` +
            `Ask your manager to check the ${word()}'s pages and role.`)));
        render.shape = null;
        return;
      }
      const next = new Map();
      const root = data.layout ? buildLayout(data.layout, byId, next) : buildAutomatic(data.entities, next);
      show(root);
      tiles = next;
    }
    for (const t of tiles.values()) t.refresh(byId);
    drawLock();
  }

  // A tile that follows one entity. Skips updates while the user is mid-change.
  function deviceTile(e, label, into) {
    const tile = makeTile(e, label);
    into.set(`${into.size}:${e.entity_id}`, {
      refresh: (byId) => {
        const fresh = byId.get(e.entity_id);
        if (fresh && !tile.busy) tile.update(fresh);
      },
    });
    return tile.el;
  }

  function sortByName(ids, byId) {
    return ids.filter((id) => byId.has(id))
      .sort((a, b) => byId.get(a).name.localeCompare(byId.get(b).name));
  }

  // No custom layout: devices grouped by area, as before.
  function buildAutomatic(entities, into) {
    const groups = new Map();
    for (const e of entities) {
      const key = e.area || "Other";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(e);
    }
    const root = h("div", { class: "groups" });
    for (const [area, list] of groups) {
      root.append(h("section", { class: "group" }, h("h2", null, area),
        h("div", { class: "grid" }, list.map((e) => deviceTile(e, null, into)))));
    }
    root.append(rearrangeButton());
    return root;
  }

  function buildLayout(layout, byId, into) {
    const pages = layout.pages;
    let current = pages.findIndex((p) => p.id === view.pageId);
    if (current < 0) current = 0;
    const page = pages[current];
    view.pageId = page.id;
    const detailItem = view.detail && page.items.find((i) => i.id === view.detail && i.type === "group");
    if (view.detail && !detailItem) view.detail = null;

    const root = h("div", { class: pages.length > 1 ? "layout has-tabs" : "layout" });
    root.append(detailItem ? buildDetail(detailItem, page, byId, into) : buildPage(page, byId, into));
    if (!detailItem) root.append(rearrangeButton());
    if (pages.length > 1) {
      root.append(h("nav", { class: "tabbar", "aria-label": "Pages" }, pages.map((p, i) =>
        h("button", {
          class: i === current ? "tab active" : "tab",
          "aria-current": i === current ? "page" : null,
          onclick: () => openPage(p.id),
        }, p.name))));
    }
    return root;
  }

  function buildPage(page, byId, into) {
    const root = h("div", { class: "groups" }, h("h1", { class: "page-title" }, page.name));
    let grid = null;
    const gridFor = () => {
      if (!grid) {
        grid = h("div", { class: "grid" });
        root.append(h("section", { class: "group" }, grid));
      }
      return grid;
    }
    for (const item of page.items) {
      // A heading that just repeats the page's own title adds nothing under the big title.
      const repeats = (title) => title?.trim().toLowerCase() === page.name.trim().toLowerCase();
      if (item.type === "heading") {
        grid = null;
        if (!repeats(item.title)) root.append(h("h2", { class: "page-heading" }, item.title));
      } else if (item.type === "area") {
        grid = null;
        const ids = sortByName(item.entity_ids, byId);
        root.append(h("section", { class: "group" }, item.title && !repeats(item.title) ? h("h2", null, item.title) : null,
          h("div", { class: "grid" }, ids.map((id) => deviceTile(byId.get(id), null, into)))));
      } else if (item.type === "device" && byId.has(item.entity_id)) {
        gridFor().append(deviceTile(byId.get(item.entity_id), item.label, into));
      } else if (item.type === "group") {
        gridFor().append(groupCard(item, into));
      }
    }
    return root;
  }

  function memberName(m, byId) {
    return m.label || byId.get(m.entity_id)?.name || m.entity_id;
  }

  // The short value shown for a member on a card or in the readings list.
  function valueText(e) {
    const a = e.attributes;
    if ((e.domain === "climate" || e.domain === "water_heater") && a.current_temperature != null) {
      const target = a.temperature != null ? ` \u2192 ${withUnit(num(a.temperature), "\u00b0")}` : "";
      return withUnit(num(a.current_temperature), "\u00b0") + target;
    }
    if (e.domain === "valve" || e.domain === "cover") return cap(e.state);
    return stateText(e) || cap(e.state);
  }

  // Group card (such as "Tub 1"): title, a few key readings, tap for everything else.
  function groupCard(item, into) {
    const rows = h("div", { class: "card-rows" });
    const card = h("button", { class: "tile group-card", onclick: () => openDetail(item.id) },
      h("div", { class: "tile-head" },
        h("div", { class: "tile-text" }, h("div", { class: "tile-name" }, item.title)),
        h("span", { class: "chevron", "aria-hidden": "true" }, "\u203a")),
      rows);
    const summary = item.entities.filter((m) => m.summary);
    into.set(`${into.size}:card:${item.id}`, {
      refresh: (byId) => {
        const members = item.entities.map((m) => byId.get(m.entity_id)).filter(Boolean);
        card.classList.toggle("alert", members.some(isAlert));
        card.classList.toggle("off", members.length > 0 && members.every((e) => e.state === "unavailable"));
        // Key readings, plus anything raising an alert so the card says why it's highlighted.
        const base = summary.length ? summary : item.entities.filter((m) => m.entity_id).slice(0, 2);
        const alerting = item.entities.filter((m) => !base.includes(m) && byId.has(m.entity_id) &&
          isAlert(byId.get(m.entity_id)));
        const shown = [...base, ...alerting];
        rows.replaceChildren(...shown.filter((m) => byId.has(m.entity_id)).map((m) => {
          const e = byId.get(m.entity_id);
          return h("div", { class: isAlert(e) ? "card-row alert" : "card-row" },
            h("span", { class: "card-label" }, memberName(m, byId)),
            h("span", { class: "card-value" }, valueText(e)));
        }));
      },
    });
    return card;
  }

  // Detail screen for a group card: key readings, the readings, then the controls allowed.
  // A device this phone can operate appears once, as a control (its tile shows its state too).
  // Named dividers split the screen into sections, each with its own readings and controls.
  function buildDetail(item, page, byId, into) {
    const hero = h("div", { class: "hero-values" });
    const sections = [{ title: null, members: [] }];
    for (const m of item.entities) {
      if (m.type === "divider") sections.push({ title: m.title, members: [] });
      else if (byId.has(m.entity_id)) sections[sections.length - 1].members.push(m);
    }
    const divided = sections.length > 1;
    const lists = []; // reading lists to refresh: { box, wrap, members }
    const body = [];
    for (const section of sections) {
      if (!section.members.length) continue;
      const controllable = section.members.filter((m) => {
        const e = byId.get(m.entity_id);
        return e.level === "control" && e.controllable;
      });
      const readOnly = section.members.filter((m) => !controllable.includes(m));
      const box = h("div", { class: "readings" });
      const grid = controllable.length
        ? h("div", { class: "grid" }, controllable.map((m) => deviceTile(byId.get(m.entity_id), memberName(m, byId), into)))
        : null;
      if (divided) {
        const wrap = h("section", { class: "group detail-section" },
          section.title ? h("h2", null, section.title) : null, box, grid);
        lists.push({ box, wrap: grid ? null : wrap, hide: box, members: readOnly });
        body.push(wrap);
      } else {
        const wrap = h("section", { class: "group" }, h("h2", null, "Readings"), box);
        lists.push({ box, wrap, hide: wrap, members: readOnly });
        body.push(wrap, grid ? h("section", { class: "group" }, h("h2", null, "Controls"), grid) : null);
      }
    }
    into.set(`${into.size}:detail:${item.id}`, {
      refresh: (byId2) => {
        const summary = item.entities.filter((m) => m.summary && byId2.has(m.entity_id));
        hero.replaceChildren(...summary.map((m) => {
          const e = byId2.get(m.entity_id);
          return h("div", { class: isAlert(e) ? "hero-value alert" : "hero-value" },
            h("div", { class: "hero-number" }, valueText(e)),
            h("div", { class: "hero-label" }, memberName(m, byId2)));
        }));
        for (const list of lists) {
          const shown = list.members.filter((m) => byId2.has(m.entity_id));
          list.hide.hidden = !shown.length;
          if (list.wrap && list.wrap !== list.hide) list.wrap.hidden = !shown.length;
          list.box.replaceChildren(...shown.map((m) => {
            const e = byId2.get(m.entity_id);
            return h("div", { class: isAlert(e) ? "reading-row alert" : "reading-row" },
              h("span", null, memberName(m, byId2)),
              h("span", { class: "reading-value" }, valueText(e)));
          }));
        }
      },
    });
    return h("div", { class: "detail" },
      h("div", { class: "detail-head" },
        h("button", { class: "back-btn", onclick: () => history.back() }, `\u2039 ${page.name}`),
        h("h1", null, item.title)),
      hero,
      ...body);
  }

  // Redraw from the last data without waiting for the next poll, and keep polling going.
  function rerender() {
    if (!lastData) return;
    render(lastData);
    if (!pollTimer && document.visibilityState === "visible") pollTimer = setTimeout(refresh, POLL_MS);
  }

  function openPage(id) {
    view.pageId = id;
    view.detail = null;
    session.set("page", id);
    history.replaceState(null, "", "/");
    window.scrollTo(0, 0);
    rerender();
  }

  function openDetail(id) {
    view.detail = id;
    history.pushState({ detail: id }, "", "/");
    window.scrollTo(0, 0);
    rerender();
  }

  // ---------------------------------------------------------------- rearrange (this phone only)

  let rearranging = false;
  let activeDrag = false;

  function rearrangeButton() {
    if (!lastData?.can_reorder) return null;
    return h("div", { class: "rearrange-row" },
      h("button", { class: "link-btn", onclick: openRearrange }, "\u21c5 Rearrange"));
  }

  function grip(label) {
    const handle = h("button", { type: "button", class: "drag-handle", "aria-label": label });
    handle.innerHTML = '<svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true">' +
      '<path d="M4 6h12M4 10h12M4 14h12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
    return handle;
  }

  // A list whose rows can be dragged by their grip (finger or mouse) or moved with arrow keys.
  // Rows move in the page as you drag; read the final order from the DOM when done.
  function sortableList(rows) {
    const list = h("div", { class: "sort-list" }, rows.map(({ key, label }) =>
      h("div", { class: "sort-row", "data-key": key }, grip(`Move ${label}`), h("span", null, label))));
    const rowEls = () => [...list.children];

    list.addEventListener("keydown", (ev) => {
      const row = ev.target.closest?.(".drag-handle")?.closest(".sort-row");
      if (!row || (ev.key !== "ArrowUp" && ev.key !== "ArrowDown")) return;
      const sibling = ev.key === "ArrowUp" ? row.previousElementSibling : row.nextElementSibling;
      if (!sibling) return;
      ev.preventDefault();
      list.insertBefore(row, ev.key === "ArrowUp" ? sibling : sibling.nextElementSibling);
      row.querySelector(".drag-handle").focus();
    });

    list.addEventListener("pointerdown", (ev) => {
      const handle = ev.target.closest?.(".drag-handle");
      const row = handle?.closest(".sort-row");
      if (!row || ev.button > 0 || !ev.isPrimary || activeDrag) return;
      ev.preventDefault();
      activeDrag = true;
      const startY = ev.clientY;
      const grabOffset = startY - row.getBoundingClientRect().top;
      let pointerY = startY;
      let dragging = false;
      // Capture on the list: the row itself moves around while dragging.
      list.setPointerCapture(ev.pointerId);
      const middle = (el) => { const r = el.getBoundingClientRect(); return r.top + r.height / 2; };
      const place = () => {
        let prev = row.previousElementSibling;
        while (prev && pointerY < middle(prev)) { list.insertBefore(row, prev); prev = row.previousElementSibling; }
        let next = row.nextElementSibling;
        while (next && pointerY > middle(next)) { list.insertBefore(row, next.nextElementSibling); next = row.nextElementSibling; }
        row.style.transform = "";
        row.style.transform = `translateY(${pointerY - grabOffset - row.getBoundingClientRect().top}px)`;
      };
      let frame = requestAnimationFrame(function autoScroll() {
        if (dragging) {
          const edge = 64;
          let dy = 0;
          if (pointerY < edge) dy = -Math.min(16, Math.ceil((edge - pointerY) / 4));
          else if (pointerY > window.innerHeight - edge) dy = Math.min(16, Math.ceil((pointerY - window.innerHeight + edge) / 4));
          if (dy) { window.scrollBy(0, dy); place(); }
        }
        frame = requestAnimationFrame(autoScroll);
      });
      const move = (e) => {
        if (e.pointerId !== ev.pointerId) return;
        pointerY = e.clientY;
        if (!dragging) {
          if (Math.abs(pointerY - startY) < 5) return;
          dragging = true;
          row.classList.add("dragging");
        }
        place();
      };
      let finished = false;
      const end = (e) => {
        if (finished || e.pointerId !== ev.pointerId) return;
        finished = true;
        activeDrag = false;
        cancelAnimationFrame(frame);
        for (const [type, fn] of listeners) list.removeEventListener(type, fn);
        if (list.hasPointerCapture(ev.pointerId)) list.releasePointerCapture(ev.pointerId);
        row.classList.remove("dragging");
        row.style.transform = "";
      };
      const listeners = [["pointermove", move], ["pointerup", end], ["pointercancel", end], ["lostpointercapture", end]];
      for (const [type, fn] of listeners) list.addEventListener(type, fn);
    });
    return { el: list, keys: () => rowEls().map((r) => r.dataset.key) };
  }

  function itemLabel(item, byId) {
    if (item.type === "device") return item.label || byId.get(item.entity_id)?.name || item.entity_id;
    if (item.type === "heading") return `Heading: ${item.title}`;
    return item.title || "Section";
  }

  function openRearrange() {
    if (!lastData) return;
    const data = lastData;
    const byId = new Map(data.entities.map((e) => [e.entity_id, e]));
    const lists = [];
    let pageId = null;
    let tabs = null;
    let items;
    if (data.layout) {
      const pages = data.layout.pages;
      const page = pages.find((p) => p.id === view.pageId) || pages[0];
      pageId = page.id;
      if (pages.length > 1) {
        tabs = sortableList(pages.map((p) => ({ key: p.id, label: p.name })));
        lists.push(h("section", { class: "group" }, h("h2", null, "Tabs"), tabs.el));
      }
      items = sortableList(page.items.map((i) => ({ key: i.id, label: itemLabel(i, byId) })));
      lists.push(h("section", { class: "group" }, h("h2", null, `On \u201c${page.name}\u201d`), items.el));
    } else {
      pageId = "auto";
      const sections = [...new Set(data.entities.map((e) => e.area || "Other"))];
      items = sortableList(sections.map((name) => ({ key: name, label: name })));
      lists.push(h("section", { class: "group" }, h("h2", null, "Sections"), items.el));
    }
    const startTabs = tabs?.keys().join("|");
    const startItems = items.keys().join("|");

    const close = () => {
      rearranging = false;
      refresh();
    };
    const save = async () => {
      // Only what changed is saved, so the rest keeps following your manager's layout.
      const order = { pages: data.order.pages, items: { ...data.order.items } };
      if (tabs && tabs.keys().join("|") !== startTabs) order.pages = tabs.keys();
      if (items.keys().join("|") !== startItems) order.items[pageId] = items.keys();
      const res = await api("PUT", "/api/order", order);
      if (res.status !== 200) {
        // Sign-in or connection problems switch to another screen, so leave rearrange mode;
        // anything else keeps the list on screen to try again.
        if (handleAuthError(res)) rearranging = false;
        else toast(res.data.error || "Couldn't save the new order.", true);
        return;
      }
      toast(`Saved on this ${word()}.`);
      close();
    };
    const reset = async () => {
      if (!window.confirm(`Put this ${word()} back to your manager's arrangement?`)) return;
      const res = await api("DELETE", "/api/order");
      if (res.status !== 200) {
        if (handleAuthError(res)) rearranging = false;
        else toast("Couldn't reset. Try again.", true);
        return;
      }
      toast("Back to the default arrangement.");
      close();
    };

    rearranging = true;
    show(h("div", { class: "rearrange" },
      h("div", { class: "detail-head" }, h("h1", null, "Rearrange")),
      h("p", { class: "muted" }, `Drag the \u2261 grips to change the order on this ${word()}. ` +
        `It doesn't change anyone else's ${word()}.`),
      lists,
      h("div", { class: "rearrange-actions" },
        h("button", { class: "btn", onclick: close }, "Cancel"),
        h("button", { class: "btn primary", onclick: save }, "Done")),
      data.custom_order
        ? h("button", { class: "link-btn", onclick: reset }, "Reset to default")
        : null));
    window.scrollTo(0, 0);
  }

  // Phone back button / swipe closes the detail screen instead of leaving the app.
  window.addEventListener("popstate", (ev) => {
    view.detail = ev.state?.detail || null;
    rerender();
  });

  async function act(tile, action, params, confirmText) {
    if (controlsLocked()) {
      // Locked while a change was pending (such as a stepper): drop it and show the real state.
      tile.busy = false;
      tile.update(tile.entity);
      lockedNudge();
      return;
    }
    if (confirmText && !window.confirm(confirmText)) return;
    keepUnlocked();
    tile.busy = true;
    tile.el.classList.add("working");
    const res = await api("POST", "/api/action", { entity_id: tile.entity.entity_id, action, params });
    tile.el.classList.remove("working");
    tile.busy = false;
    if (res.status === 200) {
      setTimeout(refresh, 600);
    } else if (!handleAuthError(res)) {
      toast(res.data.error || "That didn't work.", true);
      tile.update(tile.entity);
    }
  }

  const BINARY = {
    door: ["Open", "Closed"], window: ["Open", "Closed"], garage_door: ["Open", "Closed"],
    opening: ["Open", "Closed"], moisture: ["Wet", "Dry"], motion: ["Motion", "Clear"],
    occupancy: ["Occupied", "Clear"], presence: ["Home", "Away"], smoke: ["Smoke", "Clear"],
    gas: ["Gas detected", "Clear"], carbon_monoxide: ["CO detected", "Clear"],
    problem: ["Problem", "OK"], safety: ["Unsafe", "Safe"], battery: ["Low", "OK"],
    lock: ["Unlocked", "Locked"], plug: ["Plugged in", "Unplugged"],
    connectivity: ["Online", "Offline"], heat: ["Hot", "Normal"], cold: ["Cold", "Normal"],
    tamper: ["Tampered", "OK"], vibration: ["Vibration", "Clear"], running: ["Running", "Stopped"],
    power: ["On", "Off"],
  };
  const ALERT_CLASSES = new Set(["moisture", "smoke", "gas", "carbon_monoxide", "problem",
    "safety", "battery", "tamper", "heat", "cold"]);
  const TOGGLES = new Set(["light", "switch", "fan", "input_boolean", "siren", "humidifier"]);
  // Devices whose controls need the full width of the grid.
  const WIDE = new Set(["climate", "water_heater", "number", "input_number", "select", "input_select",
    "cover", "valve", "vacuum"]);

  // Line icons (24px grid, drawn with currentColor).
  const ICONS = {
    power: '<path d="M12 3v8"/><path d="M6.3 6.8a8 8 0 1 0 11.4 0"/>',
    bulb: '<path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-3.6 10.8c.7.6 1.1 1.3 1.1 2.2h5c0-.9.4-1.6 1.1-2.2A6 6 0 0 0 12 3z"/>',
    fan: '<circle cx="12" cy="12" r="1.8"/><path d="M12 10.2C11 6 12.5 3 15 3s3 3.5-1.2 7.4M13.8 12c4.2-1 7.2.5 7.2 3s-3.5 3-7.4-1.2M12 13.8c1 4.2-.5 7.2-3 7.2s-3-3.5 1.2-7.4M10.2 12C6 13 3 11.5 3 9s3.5-3 7.4 1.2"/>',
    thermo: '<path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0z"/><path d="M12 11v6"/>',
    flame: '<path d="M12 3c.8 3.6 5 5.2 5 10a5 5 0 0 1-10 0c0-2.7 1.6-4 2-6 .9 1 1.8 1.9 3 2.2-.3-2.1-.6-4.1 0-6.2z"/>',
    drop: '<path d="M12 3.5s6 6.3 6 10.7a6 6 0 0 1-12 0C6 9.8 12 3.5 12 3.5z"/>',
    valve: '<path d="M3 15h4l2-2h6l2 2h4M3 19h18"/><path d="M12 13V8M8.5 6h7"/>',
    lock: '<rect x="5" y="11" width="14" height="10" rx="2.5"/><path d="M8 11V7.5a4 4 0 0 1 8 0V11"/>',
    unlock: '<rect x="5" y="11" width="14" height="10" rx="2.5"/><path d="M8 11V7.5a4 4 0 0 1 7.6-1.7"/>',
    gauge: '<path d="M4.5 18a8 8 0 1 1 15 0"/><path d="M12 15l3.5-4.5"/><circle cx="12" cy="15.5" r="1"/>',
    list: '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>',
    play: '<path d="M8 5.5v13l10.5-6.5z"/>',
    sparkle: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M18.5 16v4M16.5 18h4"/>',
    press: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.5"/>',
    blinds: '<rect x="4" y="3.5" width="16" height="17" rx="2"/><path d="M4 8.5h16M4 13.5h16"/>',
    alert: '<path d="M12 4l9 15.5H3z"/><path d="M12 10v4M12 17.2v.1"/>',
    door: '<path d="M6 21V4.5A1.5 1.5 0 0 1 7.5 3h9A1.5 1.5 0 0 1 18 4.5V21M3.5 21h17"/><circle cx="14.5" cy="12" r=".8"/>',
    pulse: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
    pump: '<path d="M19.5 12a7.5 7.5 0 0 1-13.4 4.6M4.5 12a7.5 7.5 0 0 1 13.4-4.6"/><path d="M18.4 3.6v4h-4M5.6 20.4v-4h4"/>',
    robot: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2.5"/>',
  };

  function iconFor(e) {
    const d = e.domain;
    const dc = e.attributes.device_class;
    const name = (e.name || "").toLowerCase();
    let key;
    if (d === "lock") key = e.state === "unlocked" ? "unlock" : "lock";
    else if (d === "light") key = "bulb";
    else if (d === "fan") key = "fan";
    else if (d === "climate") key = "thermo";
    else if (d === "water_heater") key = "flame";
    else if (d === "valve") key = "valve";
    else if (d === "cover") key = "blinds";
    else if (d === "vacuum") key = "robot";
    else if (d === "script") key = "play";
    else if (d === "scene") key = "sparkle";
    else if (d === "button" || d === "input_button") key = "press";
    else if (d === "select" || d === "input_select") key = "list";
    else if (d === "number" || d === "input_number") key = /temp|setpoint|heat/.test(name) ? "thermo" : "gauge";
    else if (d === "binary_sensor") {
      key = ALERT_CLASSES.has(dc) ? (dc === "moisture" ? "drop" : "alert")
        : ["door", "window", "garage_door", "opening"].includes(dc) ? "door"
        : dc === "lock" ? "lock" : /level|water|fill/.test(name) ? "drop" : "pulse";
    } else if (d === "sensor") {
      key = dc === "temperature" || /temp/.test(name) ? "thermo"
        : dc === "humidity" || dc === "moisture" || /level|water|flow/.test(name) ? "drop" : "pulse";
    } else if (/pump|circulat/.test(name)) key = "pump";
    else if (/fill|drain|water/.test(name)) key = "drop";
    else if (/heat/.test(name)) key = "flame";
    else key = "power";
    return `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" ` +
      `stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICONS[key]}</svg>`;
  }

  function cap(s) {
    s = String(s).replace(/_/g, " ");
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function num(v) {
    const n = Number(v);
    return Number.isFinite(n) ? (Math.round(n * 10) / 10).toLocaleString() : v;
  }

  function withUnit(v, unit) {
    if (!unit) return String(v);
    return /^(°|%)/.test(unit) ? `${v}${unit}` : `${v} ${unit}`;
  }

  function stateText(e) {
    const s = e.state;
    const a = e.attributes;
    if (s === "unavailable" || s === "unknown") return "Unavailable";
    switch (e.domain) {
      case "binary_sensor": {
        const pair = BINARY[a.device_class];
        return pair ? pair[s === "on" ? 0 : 1] : (s === "on" ? "On" : "Off");
      }
      case "sensor":
      case "input_number":
      case "number":
        return withUnit(num(s), a.unit_of_measurement);
      case "lock":
        return { locked: "Locked", unlocked: "Unlocked", locking: "Locking…",
          unlocking: "Unlocking…", jammed: "Jammed", open: "Open" }[s] || cap(s);
      case "climate":
        return a.hvac_action ? cap(a.hvac_action) : cap(s);
      case "script":
        return s === "on" ? "Running" : "Ready";
      case "button":
      case "input_button":
      case "scene":
        return "";
      default:
        return cap(s);
    }
  }

  function isAlert(e) {
    if (e.domain === "binary_sensor") return e.state === "on" && ALERT_CLASSES.has(e.attributes.device_class);
    if (e.domain === "lock") return e.state === "jammed";
    return false;
  }

  function makeTile(entity, label) {
    const tile = { entity, label, busy: false };
    const name = h("div", { class: "tile-name" });
    const status = h("div", { class: "tile-state" });
    const controls = h("div", { class: "tile-controls" });
    const icon = h("span", { class: "tile-icon", "aria-hidden": "true" });
    tile.el = h("article", { class: `tile ${WIDE.has(entity.domain) ? "wide" : "compact"}`,
      "data-domain": entity.domain },
      h("div", { class: "tile-head" }, icon, h("div", { class: "tile-text" }, name, status)), controls);
    const head = tile.el.firstChild;
    const canControl = () => tile.entity.level === "control" && tile.entity.controllable &&
      tile.entity.state !== "unavailable";

    let build = () => {};
    const d = entity.domain;

    if (TOGGLES.has(d)) {
      const sw = h("button", { class: "switch", role: "switch", "aria-label": "Toggle" },
        h("span", { class: "knob" }));
      const toggle = () => {
        if (!canControl() || controlsLocked()) return; // locked taps get the reminder instead
        const on = tile.entity.state === "on";
        sw.setAttribute("aria-checked", String(!on));
        tile.el.classList.toggle("on", !on);
        act(tile, on ? "turn_off" : "turn_on");
      };
      sw.addEventListener("click", (ev) => { ev.stopPropagation(); toggle(); });
      // The whole tile is the switch's touch target; the brightness slider keeps its own taps.
      tile.el.addEventListener("click", (ev) => { if (!ev.target.closest(".slider")) toggle(); });
      head.append(sw);
      let slider = null;
      build = (e) => {
        sw.hidden = !canControl();
        sw.setAttribute("aria-checked", String(e.state === "on"));
        if (d === "light" && canControl() && e.state === "on" && e.attributes.brightness != null) {
          if (!slider) {
            slider = h("input", { type: "range", min: "1", max: "100", class: "slider",
              "aria-label": "Brightness" });
            slider.addEventListener("pointerdown", () => { tile.busy = true; });
            slider.addEventListener("pointerup", () => setTimeout(() => {
              if (!tile.el.classList.contains("working")) tile.busy = false;
            }, 300));
            slider.addEventListener("change", () =>
              act(tile, "turn_on", { brightness_pct: Number(slider.value) }));
            controls.append(slider);
          }
          slider.value = String(Math.round(e.attributes.brightness / 2.55));
        } else if (slider) {
          slider.remove();
          slider = null;
        }
      };
    } else if (d === "climate" || d === "water_heater") {
      const target = h("span", { class: "target" });
      const step = () => (Number(tile.entity.attributes.max_temp) > 50 ? 1 : 0.5);
      let pending = null;
      let sendTimer = null;
      const nudge = (dir) => {
        const a = tile.entity.attributes;
        const base = pending ?? Number(a.temperature);
        if (!Number.isFinite(base)) return;
        pending = Math.min(Number(a.max_temp ?? 999), Math.max(Number(a.min_temp ?? -999),
          base + dir * step()));
        target.textContent = withUnit(num(pending), "°");
        tile.busy = true;
        clearTimeout(sendTimer);
        sendTimer = setTimeout(() => {
          const value = pending;
          pending = null;
          act(tile, "set_temperature", { temperature: value });
        }, 900);
      };
      const stepper = h("div", { class: "stepper" },
        h("button", { class: "round", "aria-label": "Lower", onclick: () => nudge(-1) }, "−"),
        target,
        h("button", { class: "round", "aria-label": "Raise", onclick: () => nudge(1) }, "+"));
      const modeKey = d === "climate" ? "hvac_modes" : "operation_list";
      const modeAction = d === "climate" ? "set_hvac_mode" : "set_operation_mode";
      const modeParam = d === "climate" ? "hvac_mode" : "operation_mode";
      const mode = h("select", { class: "select", "aria-label": "Mode" });
      mode.addEventListener("change", () => act(tile, modeAction, { [modeParam]: mode.value }));
      const reading = h("div", { class: "reading" });
      controls.append(reading, stepper, mode);
      build = (e) => {
        const a = e.attributes;
        reading.textContent = a.current_temperature != null
          ? `Now ${withUnit(num(a.current_temperature), "°")}` : "";
        const hasTarget = a.temperature != null;
        stepper.hidden = !canControl() || !hasTarget;
        target.textContent = hasTarget ? withUnit(num(a.temperature), "°") : "";
        const modes = a[modeKey] || [];
        mode.hidden = !canControl() || modes.length < 2;
        const current = d === "climate" ? e.state : a.operation_mode;
        if (mode.options.length !== modes.length) {
          mode.replaceChildren(...modes.map((m) => h("option", { value: m }, cap(m))));
        }
        mode.value = current;
        if (!canControl() && hasTarget) reading.textContent += ` · Set to ${withUnit(num(a.temperature), "°")}`;
      };
    } else if (d === "number" || d === "input_number") {
      const value = h("span", { class: "target" });
      const stepSize = () => Number(tile.entity.attributes.step) || 1;
      let pending = null;
      let sendTimer = null;
      const nudge = (dir) => {
        const a = tile.entity.attributes;
        const base = pending ?? Number(tile.entity.state);
        if (!Number.isFinite(base)) return;
        const next = Math.round((base + dir * stepSize()) * 1000) / 1000;
        pending = Math.min(Number(a.max ?? Infinity), Math.max(Number(a.min ?? -Infinity), next));
        value.textContent = withUnit(num(pending), a.unit_of_measurement);
        tile.busy = true;
        clearTimeout(sendTimer);
        sendTimer = setTimeout(() => {
          const v = pending;
          pending = null;
          act(tile, "set_value", { value: v });
        }, 900);
      };
      const stepper = h("div", { class: "stepper" },
        h("button", { class: "round", "aria-label": "Lower", onclick: () => nudge(-1) }, "\u2212"),
        value,
        h("button", { class: "round", "aria-label": "Raise", onclick: () => nudge(1) }, "+"));
      controls.append(stepper);
      build = (e) => {
        stepper.hidden = !canControl();
        if (canControl()) status.textContent = "";
        value.textContent = withUnit(num(e.state), e.attributes.unit_of_measurement);
      };
    } else if (d === "select" || d === "input_select") {
      const choice = h("select", { class: "select", "aria-label": "Choose" });
      choice.addEventListener("change", () => act(tile, "select_option", { option: choice.value }));
      controls.append(choice);
      build = (e) => {
        choice.hidden = !canControl();
        const options = e.attributes.options || [];
        if (choice.options.length !== options.length) {
          choice.replaceChildren(...options.map((o) => h("option", { value: o }, o)));
        }
        choice.value = e.state;
      };
    } else if (d === "lock") {
      const btn = h("button", { class: "btn" });
      btn.addEventListener("click", () => {
        const locked = tile.entity.state === "locked";
        act(tile, locked ? "unlock" : "lock", null,
          locked ? `Unlock ${tile.entity.name}?` : null);
      });
      controls.append(btn);
      build = (e) => {
        btn.hidden = !canControl();
        btn.textContent = e.state === "locked" ? "Unlock" : "Lock";
      };
    } else if (["cover", "valve", "vacuum", "button", "input_button", "script", "scene"].includes(d)) {
      const sets = {
        cover: [["open_cover", "Open"], ["stop_cover", "Stop"], ["close_cover", "Close"]],
        valve: [["open_valve", "Open"], ["close_valve", "Close"]],
        vacuum: [["start", "Start"], ["pause", "Pause"], ["return_to_base", "Dock"]],
        button: [["press", "Press"]],
        input_button: [["press", "Press"]],
        script: [["turn_on", "Run"]],
        scene: [["turn_on", "Activate"]],
      }[d];
      const current = { open_cover: ["open", "opening"], close_cover: ["closed", "closing"],
        open_valve: ["open", "opening"], close_valve: ["closed", "closing"],
        start: ["cleaning"], pause: ["paused"], return_to_base: ["docked", "returning"] };
      const buttons = sets.map(([action, label]) =>
        h("button", { class: "btn", "data-action": action, onclick: () => act(tile, action) }, label));
      const row = h("div", { class: buttons.length > 1 ? "btn-row segmented" : "btn-row" }, buttons);
      controls.append(row);
      build = (e) => {
        row.hidden = !canControl();
        for (const b of buttons) {
          b.classList.toggle("active", (current[b.dataset.action] || []).includes(e.state));
        }
      };
    }

    tile.update = (e) => {
      tile.entity = e;
      const svg = iconFor(e);
      if (tile.icon !== svg) icon.innerHTML = tile.icon = svg; // fixed markup from ICONS, never device data
      name.textContent = tile.label || e.name;
      status.textContent = stateText(e);
      tile.el.classList.toggle("on", ["on", "open", "unlocked", "heat", "cool", "heat_cool"].includes(e.state)
        && e.domain !== "binary_sensor" && e.domain !== "sensor");
      tile.el.classList.toggle("alert", isAlert(e));
      tile.el.classList.toggle("off", e.state === "unavailable");
      build(e);
      tile.el.classList.toggle("controllable", canControl());
    };
    return tile;
  }

  // ---------------------------------------------------------------- control lock

  // Controls stay locked until someone holds "Unlock controls", so a phone used to check
  // readings can't switch something by accident. They lock again after `minutes` with no
  // control used. This guards against slips, not people: the role still decides what's allowed.
  const HOLD_MS = 800;
  const lock = { enabled: false, minutes: 10, until: 0, timer: null, tick: null };
  const lockBar = h("div", { class: "lock-bar", hidden: true });
  document.body.append(lockBar);

  function controlsLocked() {
    return lock.enabled && Date.now() >= lock.until;
  }

  function schedule() {
    clearTimeout(lock.timer);
    clearInterval(lock.tick);
    if (!lock.until) return;
    lock.timer = setTimeout(drawLock, Math.max(0, lock.until - Date.now()) + 50);
    lock.tick = setInterval(drawLock, 15000);
  }

  function unlockControls() {
    lock.until = Date.now() + lock.minutes * 60000;
    schedule();
    drawLock();
    toastEl.hidden = true; // drop any "controls are locked" hint
    navigator.vibrate?.(15);
  }

  function relock() {
    lock.until = 0;
    schedule();
    drawLock();
  }

  // Using a control counts as activity, so they don't lock mid-job.
  function keepUnlocked() {
    if (lock.enabled && lock.until) {
      lock.until = Date.now() + lock.minutes * 60000;
      schedule();
      drawLock();
    }
  }

  function lockedNudge() {
    lockBar.classList.remove("nudge");
    void lockBar.offsetWidth; // restart the animation
    lockBar.classList.add("nudge");
    toast("Controls are locked. Press and hold the lock button at the bottom to unlock.");
  }

  function holdButton() {
    const btn = h("button", { class: "lock-hold", type: "button" },
      h("span", { class: "lock-fill", "aria-hidden": "true" }),
      h("span", { class: "lock-label" }, "\ud83d\udd12 Hold to unlock controls"));
    let timer = null;
    const cancel = () => { clearTimeout(timer); timer = null; btn.classList.remove("holding"); };
    btn.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      btn.classList.add("holding");
      timer = setTimeout(() => { cancel(); unlockControls(); }, HOLD_MS);
    });
    for (const type of ["pointerup", "pointercancel", "pointerleave"]) btn.addEventListener(type, cancel);
    btn.addEventListener("contextmenu", (e) => e.preventDefault());
    // Keyboard and screen readers can't hold; a deliberate key press is enough there.
    btn.addEventListener("click", (e) => { if (e.detail === 0) unlockControls(); });
    return btn;
  }

  function drawLock() {
    const onScreen = !!screen.querySelector("article.tile.controllable");
    if (!lock.enabled || !onScreen || rearranging) {
      lockBar.hidden = true;
      document.body.classList.remove("with-lock", "controls-locked");
      if (!lock.enabled && lock.until) relock();
      return;
    }
    if (lock.until && Date.now() >= lock.until) {
      lock.until = 0;
      schedule(); // stop the countdown
    }
    const locked = controlsLocked();
    lockBar.hidden = false;
    document.body.classList.add("with-lock");
    document.body.classList.toggle("controls-locked", locked);
    lockBar.classList.toggle("unlocked", !locked);
    if (locked) {
      if (!lockBar.querySelector(".lock-hold")) lockBar.replaceChildren(holdButton());
      return;
    }
    const left = Math.max(1, Math.ceil((lock.until - Date.now()) / 60000));
    lockBar.replaceChildren(
      h("span", { class: "lock-status" }, "\ud83d\udd13 Unlocked",
        h("small", null, h("span", { class: "sep" }, " \u00b7 "), `locks in ${left} min`)),
      h("button", { class: "lock-now", type: "button", onclick: relock }, "Lock now"));
  }

  // While locked, controls ignore taps (CSS); a tap on a control's tile explains why.
  document.addEventListener("click", (e) => {
    if (controlsLocked() && e.target.closest?.("article.tile.controllable")) lockedNudge();
  });

  // ---------------------------------------------------------------- wiring

  document.getElementById("lock-btn").addEventListener("click", async () => {
    if (me?.shared) await api("POST", "/api/signout");
    session.del(SESSION_KEY);
    askForPin();
  });

  let screenKeys = null;
  document.addEventListener("keydown", (ev) => {
    if (screenKeys && !ev.ctrlKey && !ev.metaKey && !ev.altKey) screenKeys(ev);
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") drawLock();
    if (document.visibilityState === "visible" && live) refresh();
    else if (document.visibilityState === "hidden") clearTimeout(pollTimer);
  });

  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    installPrompt = e;
  });

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => { /* optional */ });
  }

  boot();
})();
