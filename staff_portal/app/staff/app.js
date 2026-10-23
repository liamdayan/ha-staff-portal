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
    tiles = new Map();
    screen.replaceChildren(node);
  }

  function toast(text, isError) {
    toastEl.textContent = text;
    toastEl.classList.toggle("error", !!isError);
    toastEl.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { toastEl.hidden = true; }, 3000);
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
          "This phone no longer has access. Ask your manager if you think this is a mistake.");
      } else {
        message("This phone isn't set up", "Ask your manager for a setup QR code.");
      }
      return true;
    }
    if (res.status === 423) {
      session.del(SESSION_KEY);
      message("Phone locked",
        "Too many wrong PINs. Ask your manager to unlock this phone from the admin dashboard.");
      return true;
    }
    if (res.status === 403 && res.data.error === "pin_required") {
      session.del(SESSION_KEY);
      askForPin();
      return true;
    }
    if (res.status === 0) {
      message("Can't connect", "Check this phone's Wi-Fi or cell signal.", true);
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
      if (isStandalone() || session.get("use_browser")) return enroll(code);
      return showInstall(code);
    }
    message("This phone isn't set up", "Ask your manager for a setup QR code.");
  }

  async function enroll(code) {
    const res = await api("POST", "/api/enroll", { code });
    if (res.status !== 200) {
      if (res.status === 0) return message("Can't connect", "Check this phone's connection.", true);
      return message("Setup link didn't work",
        (res.data.error || "Please try again.") + " Ask your manager for a new QR code.");
    }
    local.set(TOKEN_KEY, res.data.token);
    history.replaceState(null, "", "/");
    boot();
  }

  function showInstall(code) {
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
    if (me?.shared) showWho();
    else showPin(null);
  }

  // Shared phone: pick your name, then enter your own PIN.
  async function showWho() {
    const res = await api("GET", "/api/me");
    if (res.status !== 200) {
      handleAuthError(res) || message("Something went wrong", "Please try again.", true);
      return;
    }
    me = res.data;
    if (!me.shared) return showPin(null);
    if (!me.staff.length) {
      return message("No one can sign in yet",
        "Ask your manager to add you as a staff member for this phone.", true);
    }
    const node = fromTemplate("tpl-who");
    const list = node.querySelector("[data-names]");
    for (const member of me.staff) {
      list.append(h("button", { class: "who", onclick: () => showPin(member) },
        h("span", { class: "who-initial" }, member.name.charAt(0).toUpperCase()), member.name));
    }
    document.getElementById("bar").hidden = true;
    show(node);
  }

  function showPin(member) {
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
    stopPolling();
    const res = await api("GET", "/api/entities");
    if (res.status !== 200) {
      if (res.status === 503 || res.status === 0) {
        if (!tiles.size) {
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
    setSubtitle(res.data.staff_name);
    render(res.data);
    if (document.visibilityState === "visible") pollTimer = setTimeout(refresh, POLL_MS);
  }

  // view: which layout page is open, and which group card's detail screen (if any).
  const view = { page: Number(session.get("page")) || 0, detail: null };
  let lastData = null;

  function render(data) {
    lastData = data;
    const byId = new Map(data.entities.map((e) => [e.entity_id, e]));
    const shape = JSON.stringify(data.layout
      ? [data.layout, view.page, view.detail]
      : data.entities.map((e) => [e.entity_id, e.area]));
    if (shape !== render.shape || !tiles.size) {
      render.shape = shape;
      if (!data.entities.length) {
        show(h("div", { class: "center message" },
          h("h1", null, "Nothing here yet"),
          h("p", null, "Your manager hasn't given this phone access to anything.")));
        render.shape = null;
        return;
      }
      if (data.layout && !data.layout.pages.length) {
        show(h("div", { class: "center message" },
          h("h1", null, "Nothing on this phone yet"),
          h("p", null, "None of this phone's pages have anything it's allowed to see. " +
            "Ask your manager to check the phone's pages and role.")));
        render.shape = null;
        return;
      }
      const next = new Map();
      const root = data.layout ? buildLayout(data.layout, byId, next) : buildAutomatic(data.entities, next);
      show(root);
      tiles = next;
    }
    for (const t of tiles.values()) t.refresh(byId);
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
    return root;
  }

  function buildLayout(layout, byId, into) {
    const pages = layout.pages;
    if (view.page >= pages.length) view.page = 0;
    const page = pages[view.page];
    const detailItem = view.detail && page.items.find((i) => i.id === view.detail && i.type === "group");
    if (view.detail && !detailItem) view.detail = null;

    const root = h("div", { class: pages.length > 1 ? "layout has-tabs" : "layout" });
    root.append(detailItem ? buildDetail(detailItem, page, byId, into) : buildPage(page, byId, into));
    if (pages.length > 1) {
      root.append(h("nav", { class: "tabbar", "aria-label": "Pages" }, pages.map((p, i) =>
        h("button", {
          class: i === view.page ? "tab active" : "tab",
          "aria-current": i === view.page ? "page" : null,
          onclick: () => openPage(i),
        }, p.name))));
    }
    return root;
  }

  function buildPage(page, byId, into) {
    const root = h("div", { class: "groups" });
    let grid = null;
    const gridFor = () => {
      if (!grid) {
        grid = h("div", { class: "grid" });
        root.append(h("section", { class: "group" }, grid));
      }
      return grid;
    }
    for (const item of page.items) {
      if (item.type === "heading") {
        grid = null;
        root.append(h("h2", { class: "page-heading" }, item.title));
      } else if (item.type === "area") {
        grid = null;
        const ids = sortByName(item.entity_ids, byId);
        root.append(h("section", { class: "group" }, item.title ? h("h2", null, item.title) : null,
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
        const base = summary.length ? summary : item.entities.slice(0, 2);
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

  // Detail screen for a group card: key readings, every reading, then the controls allowed.
  function buildDetail(item, page, byId, into) {
    const readings = h("div", { class: "readings" });
    const hero = h("div", { class: "hero-values" });
    const controlsGrid = h("div", { class: "grid" });
    const controllable = item.entities.filter((m) => {
      const e = byId.get(m.entity_id);
      return e && e.level === "control" && e.controllable;
    });
    for (const m of controllable) controlsGrid.append(deviceTile(byId.get(m.entity_id), memberName(m, byId), into));
    into.set(`${into.size}:detail:${item.id}`, {
      refresh: (byId2) => {
        const summary = item.entities.filter((m) => m.summary && byId2.has(m.entity_id));
        hero.replaceChildren(...summary.map((m) => {
          const e = byId2.get(m.entity_id);
          return h("div", { class: isAlert(e) ? "hero-value alert" : "hero-value" },
            h("div", { class: "hero-number" }, valueText(e)),
            h("div", { class: "hero-label" }, memberName(m, byId2)));
        }));
        readings.replaceChildren(...item.entities.filter((m) => byId2.has(m.entity_id)).map((m) => {
          const e = byId2.get(m.entity_id);
          return h("div", { class: isAlert(e) ? "reading-row alert" : "reading-row" },
            h("span", null, memberName(m, byId2)),
            h("span", { class: "reading-value" }, valueText(e)));
        }));
      },
    });
    return h("div", { class: "detail" },
      h("div", { class: "detail-head" },
        h("button", { class: "back-btn", onclick: () => history.back() }, `\u2039 ${page.name}`),
        h("h1", null, item.title)),
      hero,
      h("section", { class: "group" }, h("h2", null, "All readings"), readings),
      controllable.length
        ? h("section", { class: "group" }, h("h2", null, "Controls"), controlsGrid)
        : null);
  }

  // Redraw from the last data without waiting for the next poll, and keep polling going.
  function rerender() {
    if (!lastData) return;
    render(lastData);
    if (!pollTimer && document.visibilityState === "visible") pollTimer = setTimeout(refresh, POLL_MS);
  }

  function openPage(index) {
    view.page = index;
    view.detail = null;
    session.set("page", String(index));
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

  // Phone back button / swipe closes the detail screen instead of leaving the app.
  window.addEventListener("popstate", (ev) => {
    view.detail = ev.state?.detail || null;
    rerender();
  });

  async function act(tile, action, params, confirmText) {
    if (confirmText && !window.confirm(confirmText)) return;
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
    tile.el = h("article", { class: "tile" }, h("div", { class: "tile-head" },
      h("div", { class: "tile-text" }, name, status)), controls);
    const head = tile.el.firstChild;
    const canControl = () => tile.entity.level === "control" && tile.entity.controllable &&
      tile.entity.state !== "unavailable";

    let build = () => {};
    const d = entity.domain;

    if (TOGGLES.has(d)) {
      const sw = h("button", { class: "switch", role: "switch", "aria-label": "Toggle" },
        h("span", { class: "knob" }));
      sw.addEventListener("click", () => {
        const on = tile.entity.state === "on";
        sw.setAttribute("aria-checked", String(!on));
        act(tile, on ? "turn_off" : "turn_on");
      });
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
      const row = h("div", { class: "btn-row" }, sets.map(([action, label]) =>
        h("button", { class: "btn", onclick: () => act(tile, action) }, label)));
      controls.append(row);
      build = () => { row.hidden = !canControl(); };
    }

    tile.update = (e) => {
      tile.entity = e;
      name.textContent = tile.label || e.name;
      status.textContent = stateText(e);
      tile.el.classList.toggle("on", ["on", "open", "unlocked", "heat", "cool", "heat_cool"].includes(e.state)
        && e.domain !== "binary_sensor" && e.domain !== "sensor");
      tile.el.classList.toggle("alert", isAlert(e));
      tile.el.classList.toggle("off", e.state === "unavailable");
      build(e);
    };
    return tile;
  }

  // ---------------------------------------------------------------- wiring

  document.getElementById("lock-btn").addEventListener("click", async () => {
    if (me?.shared) await api("POST", "/api/signout");
    session.del(SESSION_KEY);
    askForPin();
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && tiles.size) refresh();
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
