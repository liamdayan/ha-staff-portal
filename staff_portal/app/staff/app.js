(() => {
  "use strict";

  const TOKEN_KEY = "staff_device_token";
  const SESSION_KEY = "staff_pin_session";
  const POLL_MS = 5000;
  const FAST_POLL_MS = 1500;   // while a change is waiting to be confirmed by the device
  const CONFIRM_MS = 90000;    // how long a change may take to show up before we say so

  const local = storage(() => window.localStorage);
  const session = storage(() => window.sessionStorage);
  const screen = document.getElementById("screen");
  const toastEl = document.getElementById("toast");

  // Preview: the dashboard's Layout tab shows this app with the layout being edited. It runs on
  // the dashboard's address (so links are relative), reads the draft from the dashboard page
  // around it, and never sends a command.
  const PREVIEW = !!document.querySelector('meta[name="preview"]');
  const previewQuery = new URLSearchParams(location.search).toString();
  const url = (path) => (PREVIEW ? path.replace(/^\//, "") : path);

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
    document.body.classList.toggle("desk-shell", !!node.classList?.contains("desk-shell"));
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
    if (PREVIEW) return previewApi(path);
    const headers = { "Content-Type": "application/json" };
    const token = local.get(TOKEN_KEY);
    if (token) headers.Authorization = "Bearer " + token;
    const pinSession = session.get(SESSION_KEY);
    if (pinSession) headers["X-Session"] = pinSession;
    // Tell the server someone is actually using the phone, so their PIN session stays open.
    // Background refreshes on an untouched phone don't, and it signs out when idle. Making
    // wristbands counts too: staff are at the encoder then, not touching the screen.
    if (Date.now() - lastTouch < 60_000 || wbKeepAwake()) headers["X-Active"] = "1";
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

  async function previewApi(path) {
    if (path === "/api/action") {
      toast("Preview: nothing was sent to your equipment.");
      return { status: 200, data: { ok: true, unchanged: true } };   // so nothing waits for it
    }
    const headers = { "Content-Type": "application/json", "X-Requested-With": "staff-portal" };
    let request;
    if (path === "/api/me") {
      request = fetch(`api/preview/me?${previewQuery}`, { headers });
    } else if (path === "/api/entities") {
      let draft = {};
      try { draft = window.parent.staffPortalPreview?.() || {}; } catch { /* not in the dashboard */ }
      request = fetch("api/preview/entities", { method: "POST", headers, body: JSON.stringify(draft) });
    } else {
      return { status: 200, data: {} }; // signing in and rearranging aren't part of a preview
    }
    let res;
    try { res = await request; } catch { return { status: 0, data: {} }; }
    let data = {};
    try { data = await res.json(); } catch { /* empty body */ }
    return { status: res.status, data };
  }

  // Leaving the dashboard (sign-out, lock, an error screen): forget what was on it, so nothing
  // (a late poll, Back, a window resize) can draw the previous person's screen again, and
  // reset Rearrange so the next sign-in starts clean. The Wristbands screen is forgotten too
  // (a name typed, a job on screen); which tool was open is kept, so a front-desk computer
  // stays on Wristbands for the next person who may use it.
  let screenEpoch = 0;
  function leaveDashboard() {
    screenEpoch += 1;
    lastData = null;
    render.shape = null;
    view.detail = null;
    rearranging = false;
    activeDrag = false;
    stopPolling();
    wbReset();
    session.del(WB_JOB_KEY);
  }

  function message(title, body, retry, retryLabel = "Try again") {
    live = false;
    leaveDashboard();
    const node = fromTemplate("tpl-message");
    node.querySelector("[data-title]").textContent = title;
    node.querySelector("[data-body]").textContent = body;
    if (retry) {
      node.querySelector(".message").append(
        h("button", { class: "btn primary", onclick: () => boot() }, retryLabel));
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
    if (res.status === 403 && (res.data.error === "clocked_out" || res.data.error === "clock_unknown")) {
      // Only allowed on while clocked in on Connecteam: this person's session has ended.
      session.del(SESSION_KEY);
      message(res.data.error === "clocked_out" ? "Signed out" : "Signed out for now",
        res.data.message || "You're not clocked in.", true, "OK");
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
    if (PREVIEW) {
      const res = await api("GET", "/api/me");
      if (res.status !== 200) {
        return message("Preview isn't available", res.data.error || "Close the preview and try again.", true);
      }
      return start(res.data);
    }
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
      // Set up straight away only when a person clearly started it: the installed app's first
      // launch, or after tapping "Use it in this browser". Anywhere else it takes a click, so
      // something that merely opens links (an email security scanner, say) can't use up the
      // one-time code.
      if (isStandalone() || session.get("use_browser")) return enroll(code);
      if (isDesktop()) return showSetup(code);
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

  function showSetup(code) {
    live = false;
    const node = fromTemplate("tpl-message");
    node.querySelector("[data-title]").textContent = `Set up this ${word()}`;
    node.querySelector("[data-body]").textContent =
      `This setup link works once. After setup, this ${word()} opens your team's controls ` +
      "with the access your manager chose.";
    node.querySelector(".message").append(h("button", {
      class: "btn primary",
      onclick: (ev) => { ev.currentTarget.disabled = true; enroll(code); },
    }, `Set up this ${word()}`));
    document.getElementById("bar").hidden = true;
    show(node);
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
    leaveDashboard();
    relock(); // whoever signs in next starts with controls locked
    // showWho asks the server how this device signs in now (it may have just been switched
    // to shared), then shows the name list or the device PIN pad.
    showWho();
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
    if (!me.shared || me.pin_only) return showPin(null);
    if (!me.staff.length) {
      return message("No one can sign in yet",
        `Ask your manager to add you as a staff member for this ${word()}.`, true);
    }
    const node = fromTemplate("tpl-who");
    node.querySelector("h1").textContent = `Who's using this ${word()}?`;
    if (isDesktop()) node.querySelector(".muted").textContent = "Click your name, then type your PIN.";
    const list = node.querySelector("[data-names]");
    for (const member of me.staff) {
      list.append(h("button", { class: "who", onclick: () => showPin(member) },
        h("span", { class: "who-initial" }, member.name.charAt(0).toUpperCase()),
        member.off_clock
          ? h("span", { class: "who-name" }, member.name, h("small", null, "Not clocked in"))
          : member.name));
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
    } else if (me?.pin_only) {
      // Shared device without names: the PIN says who's signing in.
      node.querySelector("h1").textContent = "Enter your PIN";
      node.querySelector("[data-sub]").textContent = `${me.device} \u00b7 your own PIN signs you in`;
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
      // 404: the device was switched to shared sign-in (or the person was removed): ask again.
      if (res.status === 404) return showWho();
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

  // role: the role behind what's on screen. On a role-based device that's the signed-in
  // person's own (sent with the readings); before anyone signs in there, just the device's name.
  function setSubtitle(staffName, role = me.role) {
    const roleText = role || "No role assigned";
    const parts = staffName && (me.shared || me.role_based) ? [staffName, roleText]
      : me.role_based ? [me.device]
      : [roleText, me.device];
    document.getElementById("bar-sub").textContent = parts.filter(Boolean).join(" · ");
  }

  const currentRole = () => (lastData && "role" in lastData ? lastData.role : me?.role);

  function start(info) {
    me = info;
    document.documentElement.style.setProperty("--accent", info.accent_color);
    document.getElementById("bar-title").textContent = info.property_name;
    setSubtitle(null);
    const logo = document.getElementById("bar-logo");
    logo.hidden = !info.has_logo;
    if (info.has_logo) logo.src = url("/logo.png");
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
    const epoch = screenEpoch;
    const res = await api("GET", "/api/entities");
    // While this was in flight someone may have opened Rearrange, signed out or locked:
    // then this answer is for a screen that's gone.
    if (epoch !== screenEpoch || rearranging) return;
    if (res.status !== 200) {
      if (res.status === 503 || res.status === 0) {
        if (!live) {
          message("Can't reach the system", "It may be restarting. Try again in a minute.", true);
        } else {
          toast("Connection lost. Retrying…", true);
          screen.querySelector(".desk-live")?.classList.add("stale");
          pollTimer = setTimeout(refresh, POLL_MS);
        }
        return;
      }
      handleAuthError(res) || message("Something went wrong", "Please try again.", true);
      return;
    }
    live = true;
    setSubtitle(res.data.staff_name, "role" in res.data ? res.data.role : me.role);
    try {
      render(res.data);
    } finally {
      // Keep updating even if drawing this answer went wrong.
      if (document.visibilityState === "visible") pollTimer = setTimeout(refresh, expected.size ? FAST_POLL_MS : POLL_MS);
    }
  }

  // view: which layout page is open, and which group card's detail screen (if any).
  // The open page is remembered by its ID, so changing which pages a phone has doesn't
  // silently switch staff to a different page. tool: a tool open instead of a page
  // ("wristbands"), or null.
  const view = { pageId: session.get("page"), detail: null,
    tool: session.get("tool") === "wristbands" ? "wristbands" : null };
  let lastData = null;

  // Changes sent but not reported back yet. Some devices (minisplits, for one) only report a
  // new setting at their next check-in, seconds later. Until then the tile keeps showing what
  // was asked for, marked "Updating…", instead of jumping back to the old setting.
  // entity_id -> { fields: Map(key -> { want, was, earlier }), until }, where key is "state"
  // or "a:<attribute>".
  const expected = new Map();
  const latest = new Map();   // entity_id -> the entity as Home Assistant last reported it

  // What a command changes when it works, as [[key, value]], or [] for commands whose result
  // shows straight away or can't be predicted.
  function changeOf(entity, action, params) {
    switch (action) {
      case "set_temperature": return [["a:temperature", params.temperature]];
      case "set_hvac_mode": return [["state", params.hvac_mode]];
      case "set_operation_mode": return [["state", params.operation_mode], ["a:operation_mode", params.operation_mode]];
      case "set_value": return [["state", params.value]];
      case "select_option": return [["state", params.option]];
      case "turn_on":
      case "turn_off": return TOGGLES.has(entity.domain) ? [["state", action === "turn_on" ? "on" : "off"]] : [];
      default: return [];
    }
  }

  const fieldOf = (e, key) => (key === "state" ? e.state : e.attributes?.[key.slice(2)]);
  const sameValue = (a, b) => a === b ||
    (a != null && b != null && a !== "" && b !== "" && Number(a) === Number(b));

  function expectChange(entity, action, params) {
    const reported = latest.get(entity.entity_id);
    const change = changeOf(entity, action, params);
    if (!reported || !change.length) return;
    const entry = expected.get(entity.entity_id) || { fields: new Map() };
    for (const [key, want] of change) {
      const old = entry.fields.get(key);
      entry.fields.set(key, { want, was: old ? old.was : fieldOf(reported, key),
        earlier: old ? [...old.earlier, old.want] : [] });
    }
    entry.until = Date.now() + CONFIRM_MS;
    expected.set(entity.entity_id, entry);
  }

  // The entity as tiles should show it, with changes still on their way filled in. A change
  // counts as arrived once the device reports what was asked, or anything new that isn't an
  // earlier step (a unit may round a setpoint, or someone may have changed it since).
  function withExpected(e) {
    latest.set(e.entity_id, e);
    const entry = expected.get(e.entity_id);
    if (!entry) return e;
    for (const [key, f] of entry.fields) {
      const now = fieldOf(e, key);
      if (sameValue(now, f.want) || (!sameValue(now, f.was) && !f.earlier.some((v) => sameValue(now, v)))) {
        entry.fields.delete(key);
      }
    }
    if (!entry.fields.size || e.state === "unavailable") {
      expected.delete(e.entity_id);
      return e;
    }
    if (Date.now() > entry.until) {
      expected.delete(e.entity_id);
      toast(`${e.name} hasn't confirmed the change yet. Check it again in a moment.`, true);
      return e;
    }
    const shown = { ...e, attributes: { ...e.attributes }, waiting: true };
    for (const [key, f] of entry.fields) {
      if (key === "state") shown.state = String(f.want);
      else shown.attributes[key.slice(2)] = f.want;
    }
    return shown;
  }

  function render(data) {
    data.entities = data.entities.map(withExpected);
    lastData = data;
    lock.enabled = !!data.control_lock;
    lock.minutes = data.control_lock_minutes || 10;
    const byId = new Map(data.entities.map((e) => [e.entity_id, e]));
    // Settle which page is open before comparing shapes, so a remembered page that's gone
    // (or the first load) doesn't cause an extra full redraw on the next poll.
    if (data.layout?.pages.length && !data.layout.pages.some((p) => p.id === view.pageId)) {
      view.pageId = data.layout.pages[0].id;
    }
    // A tool that's no longer offered here closes (saying so, if this person had it open); a
    // tool that's all this person has opens by itself.
    const tools = toolsFor(data);
    if (view.tool && !tools.includes(view.tool)) {
      if (wb) toast(`${TOOL_NAMES[view.tool]} isn't available on this ${word()} any more.`, true);
      wbReset();
      view.tool = null;
      session.del("tool");
    }
    const nothingElse = !data.entities.length || (data.layout && !data.layout.pages.length);
    if (!view.tool && nothingElse && tools.length) view.tool = tools[0];
    const shape = JSON.stringify([isDesk(), view.tool, tools, data.layout
      ? [data.layout, view.pageId, view.detail]
      : data.entities.map((e) => [e.entity_id, e.area])]);
    if (shape !== render.shape || !tiles.size) {
      render.shape = shape;
      if (view.tool) {
        const next = new Map();
        show(buildTool(data, next));
        tiles = next;
        render.pageId = null;
        wbPlacePill();
      } else if (!data.entities.length) {
        show(h("div", { class: "center message" },
          h("h1", null, "Nothing here yet"),
          h("p", null, me?.role_based ? "Your manager hasn't given you access to anything yet."
            : `Your manager hasn't given this ${word()} access to anything.`)));
        render.shape = null;
        return;
      } else if (data.layout && !data.layout.pages.length) {
        show(h("div", { class: "center message" },
          h("h1", null, `Nothing on this ${word()} yet`),
          h("p", null, me?.role_based
            ? "None of your pages have anything you're allowed to see. Ask your manager to check your role and pages."
            : `None of this ${word()}'s pages have anything it's allowed to see. ` +
              `Ask your manager to check the ${word()}'s pages and role.`)));
        render.shape = null;
        return;
      } else {
        const next = new Map();
        // On a computer, opening or closing a card's panel redraws the page; keep its scroll position.
        const keepScroll = isDesk() && render.pageId === view.pageId ? window.scrollY : null;
        const root = data.layout ? buildLayout(data.layout, byId, next) : buildAutomatic(data.entities, next);
        show(root);
        tiles = next;
        render.pageId = view.pageId;
        if (keepScroll) window.scrollTo(0, keepScroll);
      }
    }
    for (const t of tiles.values()) {
      // One device with odd data mustn't blank the rest of the screen.
      try { t.refresh(byId); } catch (err) { console.error("Couldn't update a tile", err); }
    }
    updateFolds();
    drawLock();
    const updated = screen.querySelector("[data-updated]");
    if (updated) {
      updated.textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}`;
      updated.closest(".desk-live").classList.remove("stale");
    }
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
    const sections = [];
    for (const [area, list] of groups) {
      const fold = foldSection(`auto:${area}`, area, false, "fold-area");
      fold.body.append(h("section", { class: "group" },
        h("div", { class: "grid" }, list.map((e) => deviceTile(e, null, into)))));
      root.append(fold.el);
      sections.push([area, fold.el]);
    }
    autoSections = new Map(sections);
    if (isDesk()) {
      // No pages: the sidebar jumps to each area instead.
      const nav = h("nav", { class: "desk-nav", "aria-label": "Areas" }, toolNav(), areaNav(entities));
      return deskShell({ nav, title: "All devices", content: root });
    }
    root.append(...rearrangeButton());
    const tabs = tabBar(lastData, -1);
    return tabs ? h("div", { class: "layout has-tabs" }, root, tabs) : root;
  }

  function buildLayout(layout, byId, into) {
    const pages = layout.pages;
    let current = pages.findIndex((p) => p.id === view.pageId);
    if (current < 0) current = 0;
    const page = pages[current];
    view.pageId = page.id;
    const detailItem = view.detail && page.items.find((i) => i.id === view.detail && i.type === "group");
    if (view.detail && !detailItem) view.detail = null;

    if (isDesk()) {
      const nav = h("nav", { class: "desk-nav", "aria-label": "Pages" }, toolNav(), pageNav(pages, current));
      // On a computer the card's details open in a panel beside the page instead of replacing it.
      const drawer = detailItem ? h("div", { class: "drawer-wrap" },
        h("div", { class: "drawer-backdrop", onclick: closeDetail }),
        h("aside", { class: "drawer", role: "dialog", "aria-label": detailItem.title },
          buildDetail(detailItem, page, byId, into, true))) : null;
      return deskShell({ nav, title: page.name, content: buildPage(page, byId, into), drawer });
    }

    const tabs = tabBar(lastData, current);
    const root = h("div", { class: tabs ? "layout has-tabs" : "layout" });
    root.append(detailItem ? buildDetail(detailItem, page, byId, into) : buildPage(page, byId, into));
    if (!detailItem) root.append(...rearrangeButton());
    if (tabs) root.append(tabs);
    return root;
  }

  function buildPage(page, byId, into) {
    const root = h("div", { class: "groups" }, h("h1", { class: "page-title" }, page.name));
    let target = root; // the page itself, or the body of the heading above
    let grid = null;
    const gridFor = () => {
      if (!grid) {
        grid = h("div", { class: "grid" });
        target.append(h("section", { class: "group" }, grid));
      }
      return grid;
    };
    // A heading that just repeats the page's own title adds nothing under the big title.
    const repeats = (title) => title?.trim().toLowerCase() === page.name.trim().toLowerCase();
    for (const item of page.items) {
      if (item.type === "heading") {
        grid = null;
        if (repeats(item.title)) {
          target = root;
          continue;
        }
        // Everything up to the next heading folds away under this one.
        const fold = foldSection(`${page.id}:${item.id}`, item.title, item.collapsed);
        root.append(fold.el);
        target = fold.body;
      } else if (item.type === "area") {
        grid = null;
        const ids = sortByName(item.entity_ids, byId);
        const tiles = h("div", { class: "grid" }, ids.map((id) => deviceTile(byId.get(id), null, into)));
        if (item.title && !repeats(item.title)) {
          const fold = foldSection(`${page.id}:${item.id}`, item.title, item.collapsed, "fold-area");
          fold.body.append(h("section", { class: "group" }, tiles));
          target.append(fold.el);
        } else {
          target.append(h("section", { class: "group" }, tiles));
        }
      } else if (item.type === "device" && byId.has(item.entity_id)) {
        gridFor().append(deviceTile(byId.get(item.entity_id), item.label, into));
      } else if (item.type === "group") {
        gridFor().append(groupCard(item, into));
      }
    }
    return root;
  }

  // A heading that folds the cards under it away. Each phone remembers what it folded.
  let foldCount = 0;
  function foldSection(key, title, startCollapsed, extraClass = "") {
    const stored = local.get(`fold:${key}`);
    let open = stored ? stored === "open" : !startCollapsed;
    const body = h("div", { class: "fold-body", id: `fold-${(foldCount += 1)}` });
    const head = h("button", { class: "fold-head", type: "button", "aria-controls": body.id },
      h("span", { class: "fold-title" }, title),
      h("span", { class: "fold-count" }),
      h("span", { class: "fold-chev", "aria-hidden": "true" }));
    // Fixed markup, never device data.
    head.querySelector(".fold-chev").innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" ' +
      'stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10l5 5 5-5"/></svg>';
    const el = h("section", { class: `fold ${extraClass}`.trim() }, head, body);
    const apply = () => {
      el.classList.toggle("closed", !open);
      head.setAttribute("aria-expanded", String(open));
      body.hidden = !open;
    };
    head.addEventListener("click", () => {
      open = !open;
      local.set(`fold:${key}`, open ? "open" : "closed");
      apply();
      updateFolds();
    });
    apply();
    return { el, body };
  }

  // A folded section says how much it holds, and stands out if anything in it needs attention.
  function updateFolds() {
    for (const fold of screen.querySelectorAll(".fold")) {
      const body = fold.querySelector(":scope > .fold-body");
      const count = fold.querySelector(":scope > .fold-head .fold-count");
      const alerts = body.querySelectorAll(".tile.alert").length;
      const cards = body.querySelectorAll(".tile").length;
      fold.classList.toggle("has-alert", alerts > 0);
      count.textContent = alerts ? `${alerts} need${alerts === 1 ? "s" : ""} attention` : String(cards);
    }
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
  function buildDetail(item, page, byId, into, inDrawer = false) {
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
      inDrawer
        ? h("div", { class: "detail-head drawer-head" }, h("h1", null, item.title),
          h("button", { class: "drawer-close", type: "button", "aria-label": "Close", title: "Close (Esc)",
            onclick: closeDetail }, "\u2715"))
        : h("div", { class: "detail-head" },
          h("button", { class: "back-btn", onclick: () => history.back() }, `\u2039 ${page.name}`),
          h("h1", null, item.title)),
      hero,
      ...body);
  }

  // Redraw from the last data without waiting for the next poll, and keep polling going.
  function rerender() {
    if (!lastData || rearranging) return;
    render(lastData);
    if (!pollTimer && document.visibilityState === "visible") pollTimer = setTimeout(refresh, POLL_MS);
  }

  function openPage(id) {
    // Leaving Wristbands while cards are being made stops them, so that's asked first.
    if (wbHolding()) {
      wbLeave().then((ok) => { if (ok) openPage(id); });
      return;
    }
    if (view.tool) wbAway();
    view.tool = null;
    session.del("tool");
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
    if (!isDesk()) window.scrollTo(0, 0); // on a computer the page stays put behind the panel
    rerender();
  }

  function closeDetail() {
    if (history.state?.detail) history.back();
    else { view.detail = null; rerender(); }
  }

  // ---------------------------------------------------------------- computer layout

  // A computer-sized screen with a mouse (or any wide screen on a device set up as a computer)
  // gets a desktop app: sidebar, page header, dense rows and a side panel for card details.
  const deskQuery = window.matchMedia("(min-width: 1024px)");
  const finePointer = window.matchMedia("(hover: hover) and (pointer: fine)");
  function isDesk() {
    return deskQuery.matches && (finePointer.matches || me?.kind === "computer");
  }
  deskQuery.addEventListener("change", () => { render.shape = null; rerender(); });

  // actions: what goes at the right of the page header (left out: Rearrange, if allowed).
  function deskShell({ nav, title, content, drawer = null, actions }) {
    const logo = me?.has_logo ? h("img", { src: url("/logo.png"), alt: "" }) :
      h("span", { class: "desk-mark" }, (me?.property_name || "S").trim().charAt(0).toUpperCase());
    const who = lastData?.staff_name || me?.device || "";
    const lockBtn = me?.pin_required ? h("button", { class: "desk-foot-btn", type: "button",
      onclick: () => document.getElementById("lock-btn").click() }, me.shared ? "Sign out" : "Lock") : null;
    const side = h("aside", { class: "desk-side" },
      h("div", { class: "desk-brand" }, logo,
        h("div", { class: "desk-brand-text" },
          h("div", { class: "desk-name" }, me?.property_name || "Staff Portal"),
          h("div", { class: "desk-sub" }, me?.app_name || "Staff"))),
      nav,
      h("div", { class: "desk-foot" },
        h("span", { class: "desk-avatar", "aria-hidden": "true" }, who.trim().charAt(0).toUpperCase() || "?"),
        h("div", { class: "desk-who" }, h("div", { class: "desk-who-name" }, who),
          h("div", { class: "desk-who-sub" }, [me?.shared ? me?.device : null, currentRole()].filter(Boolean).join(" \u00b7 "))),
        lockBtn));
    if (actions === undefined) {
      actions = lastData?.can_reorder
        ? h("button", { class: "desk-action", type: "button", onclick: openRearrange }, "\u21c5 Rearrange") : null;
    }
    const main = h("main", { class: "desk-main" },
      h("header", { class: "desk-head" },
        h("h1", { class: "desk-title" }, title),
        h("span", { class: "desk-live" }, h("span", { class: "desk-dot", "aria-hidden": "true" }),
          h("span", { "data-updated": "" }, "Live")),
        h("div", { class: "desk-actions" }, actions)),
      content);
    return h("div", { class: "desk-shell" }, side, main, drawer);
  }

  // The sidebar's pages; 1 to 9 on the keyboard open them. None is highlighted while a tool is
  // open. Returned as a flat list to spread into the nav.
  function pageNav(pages, current) {
    if (!pages.length) return [];
    return [h("div", { class: "desk-nav-label" }, "Pages"), ...pages.map((p, i) => {
      const on = i === current && !view.tool;
      return h("button", {
        type: "button",
        class: on ? "active" : null,
        "aria-current": on ? "page" : null,
        title: i < 9 ? `${p.name} (press ${i + 1})` : p.name,
        onclick: () => openPage(p.id),
      }, h("span", null, p.name), i < 9 ? h("kbd", null, String(i + 1)) : null);
    })];
  }

  // Without pages, the sidebar jumps to each area of the device list.
  let autoSections = new Map();   // area -> its section on screen
  function areaNav(entities) {
    const areas = [...new Set(entities.map((e) => e.area || "Other"))];
    if (!areas.length) return [];
    return [h("div", { class: "desk-nav-label" }, "Areas"), ...areas.map((area) =>
      h("button", { type: "button", onclick: () => jumpToArea(area) }, h("span", null, area)))];
  }

  function jumpToArea(area) {
    if (view.tool) return closeTool(() => jumpToArea(area));
    autoSections.get(area)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------------------------------------------------------------- tools

  // Screens beside the pages that don't come from the layout. Wristbands is offered only on a
  // front-desk computer with a card encoder, to someone allowed to make at least one kind of
  // card. The server says so with every poll, and checks again on every Wristbands request.
  const TOOL_NAMES = { wristbands: "Wristbands" };
  const TOOL_PAUSE_MS = 60000;   // after the server turns a tool down, how long before offering it again
  let toolsPaused = 0;

  function toolsFor(data) {
    return !PREVIEW && data?.wristbands && Date.now() >= toolsPaused ? ["wristbands"] : [];
  }

  function openTool(name) {
    if (view.tool === name) {
      // Already open: back to its first screen, the way a tab bar's current tab does.
      if (wb && wb.screen !== "home") wbBack();
      return;
    }
    view.tool = name;
    view.detail = null;
    session.set("tool", name);
    history.replaceState(null, "", "/");
    window.scrollTo(0, 0);
    rerender();
  }

  // Back from a tool to the device list (no pages), then `then` (jumping to an area, say).
  function closeTool(then) {
    if (wbHolding()) {
      wbLeave().then((ok) => { if (ok) closeTool(then); });
      return;
    }
    wbAway();
    view.tool = null;
    session.del("tool");
    history.replaceState(null, "", "/");
    window.scrollTo(0, 0);
    rerender();
    then?.();
  }

  // The sidebar's tools, under their own heading above the pages. A flat list (empty when there
  // are none).
  function toolNav() {
    const tools = toolsFor(lastData);
    if (!tools.length) return [];
    return [h("div", { class: "desk-nav-label" }, "Tools"), ...tools.map((name) => h("button", {
      type: "button",
      class: view.tool === name ? "active" : null,
      "aria-current": view.tool === name ? "page" : null,
      onclick: () => openTool(name),
    }, h("span", null, TOOL_NAMES[name])))];
  }

  // The phone's tab bar: the tools first, then the pages (or the device list, without pages).
  // Only there when there's something to switch between.
  function tabBar(data, current) {
    const tabs = toolsFor(data).map((name) => [TOOL_NAMES[name], view.tool === name, () => openTool(name)]);
    if (data?.layout) {
      data.layout.pages.forEach((p, i) => tabs.push([p.name, !view.tool && i === current, () => openPage(p.id)]));
    } else if (data?.entities.length) {
      tabs.push(["Devices", !view.tool, () => closeTool()]);
    }
    if (tabs.length < 2) return null;
    return h("nav", { class: "tabbar", "aria-label": "Pages" }, tabs.map(([label, on, go]) =>
      h("button", { class: on ? "tab active" : "tab", "aria-current": on ? "page" : null, onclick: go }, label)));
  }

  // A tool's screen, framed like the pages: the same sidebar on a computer, tab bar on a phone.
  function buildTool(data, into) {
    // A stand-in tile, so polls don't rebuild the screen (render rebuilds whenever there are no
    // tiles, which would wipe a form being typed in). Each poll lets the tool update itself.
    into.set(`tool:${view.tool}`, { refresh: () => wbPoll() });
    const content = wbState().root;   // kept across redraws, so what's typed survives them
    if (isDesk()) {
      const nav = h("nav", { class: "desk-nav", "aria-label": "Pages" },
        toolNav(), data.layout ? pageNav(data.layout.pages, -1) : areaNav(data.entities));
      return deskShell({ nav, title: TOOL_NAMES[view.tool], content, actions: null });
    }
    const tabs = tabBar(data, -1);
    return h("div", { class: tabs ? "layout has-tabs" : "layout" }, content, tabs);
  }

  // ---------------------------------------------------------------- rearrange (this phone only)

  let rearranging = false;
  let activeDrag = false;

  // The Rearrange link, if this phone may rearrange: as a list to spread into append(), since
  // append(null) would put the word "null" on the screen.
  function rearrangeButton() {
    if (!lastData?.can_reorder) return [];
    return [h("div", { class: "rearrange-row" },
      h("button", { class: "link-btn", onclick: openRearrange }, "\u21c5 Rearrange"))];
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

  // Phone back button / swipe closes the detail screen instead of leaving the app, and goes
  // back to the list from a Wristbands screen.
  window.addEventListener("popstate", (ev) => {
    if (view.tool === "wristbands" && wb && wb.screen !== "home" && !ev.state?.wb) {
      wbPopped();
      return;
    }
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
    // One command at a time per tile: extra taps while it's being sent are ignored.
    if (tile.sending) return;
    if (confirmText && !window.confirm(confirmText)) return;
    keepUnlocked();
    tile.sending = true;
    tile.busy = true;
    tile.el.classList.add("working");
    const res = await api("POST", "/api/action", { entity_id: tile.entity.entity_id, action, params });
    tile.el.classList.remove("working");
    tile.busy = false;
    tile.sending = false;
    if (res.status === 200) {
      if (!res.data.unchanged) expectChange(tile.entity, action, params);
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

  // A list of options from Home Assistant, keeping only proper text entries: a malformed
  // attribute (a string, a number) mustn't break the tile.
  function strings(list) {
    return Array.isArray(list) ? list.filter((s) => typeof s === "string") : [];
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
        const modes = strings(a[modeKey]);
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
        const options = strings(e.attributes.options);
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
      tile.el.classList.toggle("waiting", !!e.waiting);
      if (e.waiting) status.textContent = [status.textContent, "Updating\u2026"].filter(Boolean).join(" \u00b7 ");
    };
    return tile;
  }

  // ---------------------------------------------------------------- wristbands

  // A front-desk computer with a TT Hotel card encoder makes wristbands (day passes and
  // overnight guests) and staff key cards. Staff Portal talks to the encoder itself, so the TT
  // Hotel keys never reach this page; this screen asks for cards and follows along. What it
  // offers is only a guide: the server checks every request against the person's permissions.
  const WB_POLL_MS = 700;            // the encoding screen follows the encoder this closely
  const WB_HIDDEN_POLL_MS = 3000;    // ...and more slowly while the window is in the background
  const WB_OPTIONS_MS = 15000;       // how often the encoder status and what can be made are checked
  const WB_PREVIEW_MS = 400;         // a pause in changes before the summary is worked out again
  const WB_SEARCH_MS = 350;          // a pause in typing before the card log is searched
  const WB_LOG_PAGE = 50;            // jobs per page of the card log (the server's page size)
  const WB_LOST_AFTER = 4;           // failed checks in a row before the screen says it can't connect
  const WB_SEARCH_FROM = 9;          // doors in a list before it gets a search box
  const WB_PREVIEW_NAME = "Guest";   // stands in for a name not typed yet, so the summary shows early
  const WB_STAFF_UNTIL = "23:59";    // staff cards work to the end of their last day (the server's rule)
  const WB_JOB_KEY = "wb_job";       // the job on screen, so reloading the page goes back to it
  const WB_ACTIVE = new Set(["queued", "running", "needs_attention"]);
  const WB_TYPES = {
    day_pass: { title: "Day pass", noun: "wristband", icon: "band" },
    overnight: { title: "Overnight", noun: "wristband", icon: "moon" },
    staff: { title: "Staff card", noun: "card", icon: "card" },
  };
  const WB_CARD_WORDS = { written: "Made", failed: "Didn't work", skipped: "Skipped",
    cancelled: "Not made", pending: "Not made" };
  // Line icons (24px grid, drawn with currentColor).
  const WB_ICONS = {
    band: '<rect x="2.5" y="8.5" width="19" height="7" rx="3.5"/><rect x="8.5" y="6" width="7" height="12" rx="2"/>',
    moon: '<path d="M19.5 14.6A7.5 7.5 0 0 1 9.4 4.5a7.5 7.5 0 1 0 10.1 10.1z"/>',
    card: '<rect x="3" y="5.5" width="18" height="13" rx="2.5"/><circle cx="9" cy="11" r="2"/>' +
      '<path d="M5.8 15.8c.7-1.4 1.8-2 3.2-2s2.5.6 3.2 2M14.5 10h4M14.5 13h3"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
    list: ICONS.list,
    up: '<path d="M12 19V5.5M6.5 11L12 5.5l5.5 5.5"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    stop: '<rect x="7" y="7" width="10" height="10" rx="2"/>',
    alert: ICONS.alert,
  };

  // The Wristbands screen's state, from when it's first opened. Forgotten at sign-out (see
  // leaveDashboard), so the next person never sees a name or a job from before.
  let wb = null;

  function wbState() {
    if (!wb) {
      wb = {
        root: h("div", { class: "wb" }),
        pill: h("span", { class: "wb-pill" }),
        screen: "home", seq: 0, timer: null, rev: 0, acting: false, lost: 0,
        options: null, optionsAt: 0, loading: false, homeKey: null,
        form: null, job: null, jobView: null, lookup: null, lookView: null, log: null,
      };
      wbDrawPill();
      wbShow("home", wbSpinner());
      wbStart(wb);
    }
    return wb;
  }

  function wbReset() {
    if (!wb) return;
    wbQuiet();
    wb = null;
  }

  // Stop whatever the screen on show was waiting to do (a poll, a summary, a search).
  function wbQuiet() {
    clearTimeout(wb.timer);
    clearTimeout(wb.form?.timer);
    clearTimeout(wb.log?.timer);
  }

  // Another page was opened. A form keeps what was typed for when Wristbands is opened again;
  // anything else (a finished job, a look-up) starts again from the list.
  function wbAway() {
    if (!wb) return;
    clearTimeout(wb.timer);
    if (!WB_TYPES[wb.screen] && wb.screen !== "home") wbHome();
  }

  // Cards are being made (or one needs a decision) on the screen in front of us.
  function wbHolding() {
    return view.tool === "wristbands" && wb?.screen === "job" && !!wb.job && WB_ACTIVE.has(wb.job.status);
  }

  // Staff are at the encoder, not the screen, while cards are written or a wristband is looked
  // up: that counts as using the computer, so it doesn't sign out halfway through.
  function wbKeepAwake() {
    if (!wb || view.tool !== "wristbands") return false;
    if (wb.screen === "job") return wb.job?.status === "queued" || wb.job?.status === "running";
    return wb.screen === "lookup" && wb.lookup?.state === "waiting";
  }

  // Sign-in problems go to their own screens as usual. A lost connection doesn't: the
  // dashboard's own poll reports that, and a card job on screen mustn't vanish over a blip.
  function wbAuthError(res) {
    return res.status !== 0 && handleAuthError(res);
  }

  // The server says this person can't use Wristbands here any more (their role changed, or the
  // encoder was taken off this computer): back to the pages, saying why.
  function wbNoAccess(text) {
    toast(text || `You can't make wristbands on this ${word()}.`, true);
    wbReset();
    toolsPaused = Date.now() + TOOL_PAUSE_MS;
    view.tool = null;
    session.del("tool");
    rerender();
  }

  // replaceChildren, leaving out null and false (which it would print as text).
  function fill(el, ...items) {
    el.replaceChildren(...items.flat().filter((c) => c != null && c !== false));
  }

  function setText(el, text) {
    if (el.textContent !== text) el.textContent = text;   // unchanged text isn't announced again
  }

  // Put up one of the Wristbands screens. Answers still on their way for the screen before
  // are dropped when they arrive (they check wb.seq).
  function wbShow(name, ...nodes) {
    wbQuiet();
    wb.seq += 1;
    wb.screen = name;
    wb.root.dataset.screen = name;
    fill(wb.root, nodes);
    wbPlacePill();
    if (wb.root.isConnected) window.scrollTo(0, 0);
    return wb.seq;
  }

  // Screens past the list get a history entry, so Back (a phone's, or the browser's) returns
  // to the list instead of leaving the app.
  function wbEnter(name) {
    if (history.state?.wb) history.replaceState({ wb: name }, "", "/");
    else history.pushState({ wb: name }, "", "/");
  }

  // Back to the list, through history when the screen has an entry so the two stay in step.
  function wbBack() {
    if (history.state?.wb) history.back();
    else wbLeave().then((ok) => { if (ok && wb) wbHome(); });
  }

  async function wbPopped() {
    const name = wb.screen;
    if (!(await wbLeave())) {
      history.pushState({ wb: name }, "", "/");   // staying: put the entry back
      return;
    }
    if (wb) wbHome();
  }

  function wbStopQuestion(job) {
    const noun = (WB_TYPES[job.type] || WB_TYPES.day_pass).noun;
    const made = job.cards.filter((c) => c.status === "written").length;
    return `Stop making ${noun}s?` + (made ? ` The ${wbPlural(made, noun)} already made will still work.` : "");
  }

  // Leaving the encoding screen while cards are being made stops the job, so ask first.
  // Resolves to whether to go ahead.
  async function wbLeave() {
    if (!wbHolding()) return true;
    const job = wb.job;
    if (!window.confirm(wbStopQuestion(job))) return false;
    clearTimeout(wb.timer);
    wb.job = null;
    session.del(WB_JOB_KEY);
    await api("POST", `/api/wristbands/jobs/${job.id}/cancel`);
    return true;
  }

  // Esc on a computer: clear a search box, or step back where nothing would be lost. It never
  // stops a job (that takes the Stop button) or drops what's been typed into a form.
  function wbEscape(ev) {
    const target = ev.target;
    if (target?.type === "search" && target.value) {
      target.value = "";
      target.dispatchEvent(new Event("input"));
      return true;
    }
    if (target?.type === "date" || wb.screen === "home" || wbHolding()) return false;
    if (wb.form && (wb.form.name.trim() || wb.form.touched.size)) return false;
    wbBack();
    return true;
  }

  // ---- what can be made here, and the encoder's status

  // First look: what this person may make here, and the job that was on screen before the
  // page was reloaded, if it's still going.
  async function wbStart(state) {
    state.loading = true;
    const saved = Number(session.get(WB_JOB_KEY)) || 0;
    const [res, again] = await Promise.all([api("GET", "/api/wristbands"),
      saved ? api("GET", `/api/wristbands/jobs/${saved}`) : null]);
    if (wb !== state) return;
    state.loading = false;
    state.optionsAt = Date.now();
    if (!wbTakeOptions(res)) return;
    const job = again?.status === 200 ? again.data.job : null;
    if (job && WB_ACTIVE.has(job.status) && state.screen === "home") return wbShowJob(job);
    session.del(WB_JOB_KEY);
    if (state.screen === "home") wbHome();
  }

  // With each dashboard poll while Wristbands is on screen: keep the encoder status, and what
  // can be made right now (no day passes after closing, say), up to date.
  function wbPoll() {
    if (wb && !wb.loading && Date.now() - wb.optionsAt >= WB_OPTIONS_MS) wbRefreshOptions();
  }

  async function wbRefreshOptions() {
    const state = wb;
    state.loading = true;
    const res = await api("GET", "/api/wristbands");
    if (wb !== state) return;
    state.loading = false;
    state.optionsAt = Date.now();   // a failed check waits as long as a good one before the next
    if (wbTakeOptions(res, true) && state.screen === "home") wbHome(true);
  }

  // Take a GET /api/wristbands answer. Returns whether it arrived. `quiet`: a background check,
  // which leaves the screen alone if it fails.
  function wbTakeOptions(res, quiet = false) {
    if (res.status === 200) {
      wb.options = res.data;
      wbDrawPill();
      return true;
    }
    if (wbAuthError(res)) return false;
    if (res.status === 403) {
      wbNoAccess(res.data.error);
      return false;
    }
    if (!quiet) {
      wbShow("home", wbHomeHead(),
        wbNote("error", res.status === 0 ? `Can't reach Staff Portal. Check this ${word()}'s connection.`
          : res.data.error || "Wristbands couldn't be loaded."),
        h("button", { type: "button", class: "btn primary", onclick: () => wbHome() }, "Try again"));
      wb.homeKey = null;
    }
    return false;
  }

  function wbDrawPill() {
    const enc = wb.options?.encoder;
    // The job on screen is fresher than the last status check (every WB_OPTIONS_MS).
    const making = wb.screen === "job" && WB_ACTIVE.has(wb.job?.status);
    const [cls, text] = enc?.error && !making ? ["error", "Encoder offline"]
      : enc?.busy || making ? ["busy", "Encoder busy"]
      : enc?.connected ? ["ok", "Encoder ready"]
      : ["", "Checking encoder…"];
    wb.pill.className = cls ? `wb-pill ${cls}` : "wb-pill";
    wb.pill.title = enc?.error || "";
    fill(wb.pill, h("span", { class: "wb-pill-dot", "aria-hidden": "true" }), text);
  }

  // The encoder status sits in the page header on a computer, beside the title on a phone.
  function wbPlacePill() {
    if (!wb?.root.isConnected) return;
    const slot = isDesk() ? screen.querySelector(".desk-actions") : wb.root.querySelector(".wb-pill-slot");
    if (slot && wb.pill.parentNode !== slot) slot.append(wb.pill);
  }

  // ---- the list: what to make, look up or check

  // Whether to list a kind of card at all. One this person isn't allowed to make is left out;
  // one that can't be made just now (after the day-pass cut-off, say) shows why. The options
  // don't say which is which, so the server's wording for "not allowed" tells them apart.
  function wbOffered(t) {
    if (!t) return false;
    if (typeof t.permitted === "boolean") return t.permitted;
    return !!t.allowed || !/^You don't have permission/.test(t.reason || "");
  }

  function wbHomeHead() {
    return h("div", { class: "wb-head wb-home-head" },
      h("h1", { class: "page-title" }, "Wristbands"), h("span", { class: "wb-pill-slot" }));
  }

  // quiet: redrawn after a background check, only if something on it changed.
  function wbHome(quiet = false) {
    const o = wb.options;
    if (!o) {
      wbShow("home", wbSpinner());
      if (!wb.loading) wbStart(wb);
      return;
    }
    const key = JSON.stringify([o.enabled, o.reason, o.types, o.encoder?.error, o.see_all_cards]);
    if (quiet && key === wb.homeKey) return;
    wb.homeKey = key;
    wbQuiet();
    wb.form = wb.job = wb.lookup = wb.log = null;
    const focused = quiet ? document.activeElement?.dataset?.choice : null;
    const scrolled = window.scrollY;

    const kinds = o.enabled ? Object.keys(WB_TYPES).filter((type) => wbOffered(o.types?.[type])) : [];
    const make = kinds.map((type) => {
      const t = o.types[type];
      const sub = !t.allowed ? t.reason || "Can't be made right now."
        : type === "day_pass" ? `Works until ${t.expires_label}`
        : type === "overnight" ? `Room wristbands until checkout at ${wbClock(t.checkout_time)}`
        : "Key cards for staff";
      return wbChoice(type, WB_TYPES[type].title, sub, WB_TYPES[type].icon,
        t.allowed ? () => wbOpenForm(type) : null);
    });
    wbShow("home", wbHomeHead(),
      o.encoder?.error ? wbNote("error", o.encoder.error, "The card encoder isn't answering") : null,
      !o.enabled && o.reason ? wbNote("", o.reason) : null,
      make.length ? h("section", { class: "group" }, h("h2", null, "Make"),
        h("div", { class: "wb-choices" }, make)) : null,
      h("section", { class: "group" }, h("h2", null, "Check"),
        h("div", { class: "wb-choices secondary" },
          wbChoice("lookup", "Look up a wristband", "Who it's for and which doors it opens", "search",
            () => wbOpenLookup()),
          wbChoice("log", "Card log", o.see_all_cards ? "Every wristband and card made"
            : "The wristbands and cards you've made", "list", () => wbOpenLog()))));
    if (quiet) window.scrollTo(0, scrolled);
    if (focused) wb.root.querySelector(`[data-choice="${focused}"]`)?.focus();
  }

  // One big button on the list. Without `go` it can't be used right now: it stays focusable
  // and says why when pressed.
  function wbChoice(key, title, sub, icon, go) {
    return h("button", {
      type: "button",
      class: go ? "wb-choice" : "wb-choice disabled",
      "aria-disabled": go ? null : "true",
      "data-choice": key,
      onclick: () => (go ? go() : toast(sub)),
    }, wbIcon(icon, "wb-choice-icon"),
    h("span", { class: "wb-choice-text" }, h("span", { class: "wb-choice-title" }, title),
      h("span", { class: "wb-choice-sub" }, sub)),
    go ? h("span", { class: "chevron", "aria-hidden": "true" }, "›") : null);
  }

  function wbSubHead(title) {
    return h("div", { class: "detail-head wb-head" },
      h("button", { class: "back-btn", type: "button", onclick: () => wbBack() }, "‹ Wristbands"),
      h("div", { class: "wb-title-row" }, h("h1", null, title), h("span", { class: "wb-pill-slot" })));
  }

  // ---- the forms: day pass, overnight, staff card

  function wbOpenForm(type) {
    const o = wb.options;
    const t = o?.types?.[type];
    if (!t?.allowed) return toast(t?.reason || "That can't be made right now.");
    const f = {
      type, t, name: "", count: 1, touched: new Set(), submitted: false, busy: false,
      doors: new Set(t.door_ids || []), rooms: new Set(),
      checkout: t.default_checkout || "", expiresOn: t.default_expires_on || "",
      access: { all: false, buildings: new Set(), floors: new Set(), doors: new Set() },
      doorById: new Map((o.doors || []).map((d) => [d.id, d])),
      plan: null, planOk: false, note: "", noteShown: false, ask: 0, timer: null, fields: {}, els: {},
    };
    wb.form = f;
    wbEnter(type);
    wbShow(type, wbSubHead(WB_TYPES[type].title), wbFormView(f));
    wbDrawSummary(f);
    wbPreview(f);
    if (isDesk()) f.fields.name.control.focus({ preventScroll: true });
  }

  function wbFormView(f) {
    const { type, t } = f;
    const o = wb.options;
    const staff = type === "staff";
    const name = h("input", { class: "field", type: "text", maxlength: "80", autocomplete: "off",
      autocapitalize: "words", spellcheck: "false", enterkeyhint: "go",
      placeholder: staff ? "Their full name" : "Guest or party name" });
    name.addEventListener("input", () => {
      f.name = name.value;
      wbChanged(f, "name", false);
    });
    const people = [
      wbField(f, "name", staff ? "Staff member's name" : "Guest name", name),
      wbField(f, "count", staff ? "How many cards" : "Party size",
        wbStepper(f, staff ? "How many cards" : "Party size", t.max || 1, staff ? "card" : "guest"),
        staff ? null : h("p", { class: "wb-hint" }, "One wristband per guest.")),
    ];
    const sections = [h("section", { class: "wb-card" }, people)];
    if (type === "day_pass") {
      sections.push(h("section", { class: "wb-card" },
        wbDoorsField(f, "Doors", "Every day pass opens these doors.")));
    } else if (type === "overnight") {
      const shared = new Set(t.door_ids || []);
      const rooms = (o.doors || []).filter((d) => !shared.has(d.id));
      const checkout = h("input", { class: "field", type: "date", value: f.checkout, min: o.today,
        max: t.max_nights ? wbAddDays(o.today, t.max_nights) : null });
      f.els.checkoutHint = h("p", { class: "wb-hint", "aria-live": "polite" });
      checkout.addEventListener("input", () => {
        f.checkout = checkout.value;
        wbDrawDates(f);
        wbChanged(f, "checkout");
      });
      sections.push(
        h("section", { class: "wb-card" }, wbField(f, "room_ids", "Rooms",
          wbDoorPicker(f.rooms, "rooms", rooms, () => wbChanged(f, "room_ids")),
          h("p", { class: "wb-hint" }, "Pick every room in the booking."))),
        h("section", { class: "wb-card" },
          wbField(f, "checkout", "Checkout date", checkout, f.els.checkoutHint),
          shared.size || t.can_edit_doors
            ? wbDoorsField(f, "Shared doors", "Every overnight wristband opens these too.") : null));
    } else {
      const expires = h("input", { class: "field", type: "date", value: f.expiresOn, min: o.today,
        max: t.max_days ? wbAddDays(o.today, t.max_days) : null });
      f.els.expiresHint = h("p", { class: "wb-hint", "aria-live": "polite" });
      expires.addEventListener("input", () => {
        f.expiresOn = expires.value;
        wbDrawDates(f);
        wbChanged(f, "expires_on");
      });
      sections.push(
        h("section", { class: "wb-card" }, wbField(f, "access", "Opens", wbAccessPicker(f))),
        h("section", { class: "wb-card" }, wbField(f, "expires_on", "Works until", expires, f.els.expiresHint)));
    }
    wbDrawDates(f);
    f.els.summary = h("div", { class: "readings wb-summary" });
    f.els.note = h("p", { class: "wb-plan-note", hidden: true });
    f.els.problem = h("p", { class: "wb-form-error", role: "alert", hidden: true });
    f.els.submit = h("button", { class: "btn primary wb-submit", type: "submit" });
    return h("form", { class: "wb-form", novalidate: true,
      onsubmit: (ev) => { ev.preventDefault(); wbSubmit(f); } },
    h("div", { class: "wb-main" }, sections),
    h("section", { class: "wb-side", "aria-label": "Summary" }, h("h2", null, "Summary"),
      f.els.summary, f.els.note, f.els.problem, f.els.submit));
  }

  // A labelled form field with room for an error underneath. `control` is an input (labelled
  // with for=) or a group of controls (labelled with aria-labelledby).
  function wbField(f, key, label, control, hint = null) {
    const id = `wb-${key}`;
    const error = h("p", { class: "wb-error", id: `${id}-error`, hidden: true });
    let title;
    if (control.matches("input, select, textarea")) {
      control.id = id;
      title = h("label", { class: "wb-label", for: id }, label);
    } else {
      title = h("div", { class: "wb-label", id: `${id}-label` }, label);
      control.setAttribute("role", "group");
      control.setAttribute("aria-labelledby", title.id);
    }
    const el = h("div", { class: "wb-field" }, title, control, hint, error);
    f.fields[key] = { el, control, error };
    return el;
  }

  // Shows an error under a field. Returns false if this form has no such field.
  function wbSetError(f, key, text) {
    const field = f.fields[key];
    if (!field) return false;
    field.error.textContent = text;
    field.error.hidden = false;
    field.control.setAttribute("aria-invalid", "true");
    field.control.setAttribute("aria-describedby", field.error.id);
    return true;
  }

  function wbClearError(f, key) {
    const field = f.fields[key];
    if (!field || field.error.hidden) return;
    field.error.hidden = true;
    field.control.removeAttribute("aria-invalid");
    field.control.removeAttribute("aria-describedby");
  }

  function wbFocusField(f, key) {
    const control = f.fields[key]?.control;
    const target = control?.matches("input") ? control
      : control?.querySelector("input:not(:disabled), button");
    target?.focus();
  }

  // Something on the form changed: its error goes, and the summary is worked out again (a
  // new name doesn't change what's made, so that's only shown).
  function wbChanged(f, key, preview = true) {
    f.touched.add(key);
    wbClearError(f, key);
    f.els.problem.hidden = true;
    if (preview) wbSchedulePreview(f);
    wbDrawSummary(f);
  }

  // Party size or number of cards: big - and + buttons, and the number can be typed as well.
  function wbStepper(f, label, max, unit) {
    const input = h("input", { class: "target wb-count", type: "number", inputmode: "numeric",
      min: "1", max: String(max), value: String(f.count), "aria-label": label });
    const less = h("button", { type: "button", class: "round", "aria-label": `One fewer ${unit}` }, "−");
    const more = h("button", { type: "button", class: "round", "aria-label": `One more ${unit}` }, "+");
    const set = (n, typed = false) => {
      n = Math.min(max, Math.max(1, n));
      if (!typed) input.value = String(n);
      // aria-disabled rather than disabled, so the button keeps keyboard focus at the limit.
      less.setAttribute("aria-disabled", String(n <= 1));
      more.setAttribute("aria-disabled", String(n >= max));
      if (n !== f.count) {
        f.count = n;
        wbChanged(f, "count");
      }
    };
    less.addEventListener("click", () => set(f.count - 1));
    more.addEventListener("click", () => set(f.count + 1));
    input.addEventListener("input", () => {
      const n = parseInt(input.value, 10);
      if (Number.isFinite(n)) set(n, true);
    });
    input.addEventListener("change", () => { input.value = String(f.count); });
    set(f.count);
    return h("div", { class: "stepper wb-stepper" }, less, input, more);
  }

  // The doors every card of this kind opens, chosen by a manager. Someone allowed to change
  // them can, for this card only. Doors hidden from the lists stay on it either way.
  function wbDoorsField(f, label, hint) {
    const listed = f.doorById;
    const unlisted = [...f.doors].filter((id) => !listed.has(id)).length;
    const text = h("p", { class: "wb-doors-text" });
    const draw = () => {
      const names = [...f.doors].filter((id) => listed.has(id)).map((id) => listed.get(id).name);
      if (unlisted) names.push(wbPlural(unlisted, names.length ? "other door" : "door"));
      text.textContent = names.length ? names.join(", ") : "None";
    };
    draw();
    const line = h("div", { class: "wb-doors-line" }, text);
    const box = h("div", { class: "wb-doors-box" }, line);
    if (f.t.can_edit_doors) {
      let picker = null;
      const toggle = h("button", { type: "button", class: "btn wb-change", "aria-expanded": "false" }, "Change");
      toggle.addEventListener("click", () => {
        if (!picker) {
          picker = wbDoorPicker(f.doors, "doors", [...listed.values()], () => {
            draw();
            wbChanged(f, "door_ids");
          });
          box.append(picker);
        } else {
          picker.hidden = !picker.hidden;
        }
        toggle.setAttribute("aria-expanded", String(!picker.hidden));
        toggle.textContent = picker.hidden ? "Change" : "Hide list";
      });
      line.append(toggle);
    }
    return wbField(f, "door_ids", label, box, h("p", { class: "wb-hint" }, hint));
  }

  // Doors grouped as building > floor, in the server's order (building, floor, then name).
  function wbGroups(doors) {
    const buildings = new Map();
    for (const d of doors) {
      const bKey = d.building ?? "none";
      let b = buildings.get(bKey);
      if (!b) {
        b = { key: bKey, building: d.building ?? null, floors: new Map(),
          name: d.building == null ? "Other doors" : d.building_name || `Building ${d.building}` };
        buildings.set(bKey, b);
      }
      const fKey = d.floor ?? "none";
      let fl = b.floors.get(fKey);
      if (!fl) {
        fl = { floor: d.floor ?? null, doors: [],
          name: d.floor == null ? "No floor" : d.floor_name || `Floor ${d.floor}` };
        b.floors.set(fKey, fl);
      }
      fl.doors.push(d);
    }
    return [...buildings.values()].map((b) => ({ ...b, floors: [...b.floors.values()] }));
  }

  function wbMatches(d, q) {
    return [d.name, d.building_name, d.floor_name].some((s) => String(s || "").toLowerCase().includes(q));
  }

  function wbSearchBox(what) {
    return h("input", { class: "field wb-search", type: "search", autocomplete: "off",
      placeholder: `Search ${what}`, "aria-label": `Search ${what}` });
  }

  function wbChip(label, remove) {
    return h("button", { type: "button", class: "wb-chip", "aria-label": `Remove ${label}`, onclick: remove },
      label, h("span", { "aria-hidden": "true" }, "×"));
  }

  // Doors to tick, grouped by building and floor, with a search box for big properties.
  // `selected` (a Set of lock IDs) is changed in place; onChange runs after each change.
  function wbDoorPicker(selected, what, doors, onChange) {
    const byId = new Map(doors.map((d) => [d.id, d]));
    const all = wbGroups(doors);
    const manyFloors = new Set(all.filter((g) => g.floors.length > 1).map((g) => g.key));
    const boxes = new Map();
    const search = doors.length >= WB_SEARCH_FROM ? wbSearchBox(what) : null;
    const chips = h("div", { class: "wb-chips" });
    const tree = h("div", { class: "wb-tree" });
    const set = (id, on) => {
      if (on) selected.add(id);
      else selected.delete(id);
      const box = boxes.get(id);
      if (box) box.checked = on;
      drawChips();
      onChange();
    };
    const drawChips = () => {
      const picked = [...selected].filter((id) => byId.has(id));
      fill(chips, picked.length ? picked.map((id) => wbChip(byId.get(id).name, () => set(id, false)))
        : h("span", { class: "muted" }, `No ${what} picked yet`));
    };
    const drawTree = () => {
      const q = search?.value.trim().toLowerCase() || "";
      boxes.clear();
      const groups = wbGroups(q ? doors.filter((d) => wbMatches(d, q)) : doors);
      fill(tree, groups.length ? groups.map((g) => h("section", { class: "wb-building" },
        all.length > 1 ? h("h3", { class: "wb-building-name" }, g.name) : null,
        g.floors.map((fl) => h("div", { class: "wb-floor" },
          manyFloors.has(g.key) ? h("h4", { class: "wb-floor-name" }, fl.name) : null,
          h("div", { class: "wb-doors" }, fl.doors.map((d) => {
            const box = h("input", { type: "checkbox", checked: selected.has(d.id) });
            box.addEventListener("change", () => set(d.id, box.checked));
            boxes.set(d.id, box);
            return h("label", { class: "wb-check" }, box, h("span", { class: "wb-check-text" }, d.name));
          })))))) : h("p", { class: "muted wb-empty" },
        q ? `Nothing matches “${search.value.trim()}”.` : `There are no ${what} to pick from.`));
    };
    search?.addEventListener("input", drawTree);
    search?.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter") return;
      ev.preventDefault();   // Enter here ticks the one door found; it doesn't make the card
      if (boxes.size === 1) {
        const [id] = boxes.keys();
        set(id, !selected.has(id));
      }
    });
    drawChips();
    drawTree();
    return h("div", { class: "wb-picker" }, search, chips, tree);
  }

  // The access list to send for a staff card: only the widest choices (a ticked building
  // already opens its floors and doors).
  function wbAccessEntries(a, byId) {
    if (a.all) return [{ level: "all" }];
    const out = [...a.buildings].map((b) => ({ level: "building", building: b }));
    for (const key of a.floors) {
      const [b, fl] = key.split(":").map(Number);
      if (!a.buildings.has(b)) out.push({ level: "floor", building: b, floor: fl });
    }
    for (const id of a.doors) {
      const d = byId.get(id);
      if (d && d.building != null && (a.buildings.has(d.building) ||
        (d.floor != null && a.floors.has(`${d.building}:${d.floor}`)))) continue;
      out.push({ level: "door", door: id });
    }
    return out;
  }

  // What a staff card opens: every door, whole buildings, whole floors or single doors, as far
  // as this person's own access reaches. Ticking a building covers its floors and doors.
  function wbAccessPicker(f) {
    const levels = new Set(f.t.levels || []);
    const a = f.access;
    const doors = wb.options.doors || [];
    const byId = f.doorById;
    const all = wbGroups(doors);
    const buildingName = new Map();
    const floorName = new Map();
    for (const g of all) {
      if (g.building == null) continue;
      buildingName.set(g.building, g.name);
      for (const fl of g.floors) {
        if (fl.floor != null) floorName.set(`${g.building}:${fl.floor}`, `${g.name}, ${fl.name}`);
      }
    }
    const rows = [];   // { input, el, isOn, isCovered }
    const chips = h("div", { class: "wb-chips" });
    const changed = () => {
      sync();
      wbChanged(f, "access");
    };
    const toggleIn = (set, key) => (on) => (on ? set.add(key) : set.delete(key));
    const row = (cls, text, sub, isOn, setOn, isCovered) => {
      const input = h("input", { type: "checkbox" });
      input.addEventListener("change", () => {
        setOn(input.checked);
        changed();
      });
      const el = h("label", { class: cls ? `wb-check ${cls}` : "wb-check" }, input,
        h("span", { class: "wb-check-text" }, text, sub ? h("small", null, sub) : null));
      rows.push({ input, el, isOn, isCovered });
      return el;
    };
    const floorCovered = (b) => a.all || a.buildings.has(b);
    const doorCovered = (d) => a.all || (d.building != null && (a.buildings.has(d.building) ||
      (d.floor != null && a.floors.has(`${d.building}:${d.floor}`))));
    const sync = () => {
      for (const r of rows) {
        const covered = r.isCovered();
        r.input.disabled = covered;
        r.input.checked = covered || r.isOn();
        r.el.classList.toggle("covered", covered);
      }
      const entries = wbAccessEntries(a, byId);
      fill(chips, entries.length ? entries.map((e) => {
        const label = e.level === "all" ? "All doors"
          : e.level === "building" ? buildingName.get(e.building) || `Building ${e.building}`
          : e.level === "floor" ? floorName.get(`${e.building}:${e.floor}`) || `Floor ${e.floor}`
          : byId.get(e.door)?.name || "A door";
        return wbChip(label, () => {
          if (e.level === "all") a.all = false;
          else if (e.level === "building") a.buildings.delete(e.building);
          else if (e.level === "floor") a.floors.delete(`${e.building}:${e.floor}`);
          else a.doors.delete(e.door);
          changed();
        });
      }) : h("span", { class: "muted" }, "Nothing picked yet"));
    };

    const allRow = levels.has("all")
      ? row("wb-all", "All doors", "Every door, including ones added later", () => a.all,
        (on) => { a.all = on; }, () => false) : null;
    const treeLevels = ["building", "floor", "door"].some((l) => levels.has(l));
    const search = treeLevels && doors.length >= WB_SEARCH_FROM ? wbSearchBox("doors") : null;
    const tree = treeLevels ? h("div", { class: "wb-tree" }) : null;
    const drawTree = () => {
      rows.length = allRow ? 1 : 0;
      const q = search?.value.trim().toLowerCase() || "";
      const groups = wbGroups(q ? doors.filter((d) => wbMatches(d, q)) : doors);
      fill(tree, groups.length ? groups.map((g) => h("section", { class: "wb-building" },
        levels.has("building") && g.building != null
          ? row("wb-whole", g.name, "Whole building", () => a.buildings.has(g.building),
            toggleIn(a.buildings, g.building), () => a.all)
          : h("h3", { class: "wb-building-name" }, g.name),
        g.floors.map((fl) => {
          const key = `${g.building}:${fl.floor}`;
          return h("div", { class: "wb-floor" },
            levels.has("floor") && g.building != null && fl.floor != null
              ? row("wb-whole", fl.name, "Whole floor", () => a.floors.has(key), toggleIn(a.floors, key),
                () => floorCovered(g.building))
              : h("h4", { class: "wb-floor-name" }, fl.name),
            levels.has("door") ? h("div", { class: "wb-doors" }, fl.doors.map((d) =>
              row("", d.name, null, () => a.doors.has(d.id), toggleIn(a.doors, d.id), () => doorCovered(d))))
              : null);
        }))) : h("p", { class: "muted wb-empty" },
        q ? `Nothing matches “${search.value.trim()}”.` : "There are no doors you can give access to."));
      sync();
    };
    search?.addEventListener("input", () => { if (tree) drawTree(); });
    search?.addEventListener("keydown", (ev) => { if (ev.key === "Enter") ev.preventDefault(); });
    if (tree) drawTree();
    else sync();
    return h("div", { class: "wb-picker" }, allRow, search, chips, tree);
  }

  // When the cards stop working, worked out here as dates change (the summary then gets the
  // server's own words for it).
  function wbDrawDates(f) {
    const { t } = f;
    const today = wb.options.today;
    if (f.type === "overnight") {
      const nights = f.checkout ? wbDaysBetween(today, f.checkout) : NaN;
      f.els.checkoutHint.textContent = !f.checkout ? "Pick the day they check out."
        : !(nights >= 0) ? "That date has already passed."
        : `Wristbands stop working at ${wbExpiryLabel(f.checkout, t.checkout_time)} · ` +
          (nights ? wbPlural(nights, "night") : "checking out today");
    } else if (f.type === "staff") {
      f.els.expiresHint.textContent = (f.expiresOn
        ? `Cards stop working at ${wbExpiryLabel(f.expiresOn, WB_STAFF_UNTIL)}.` : "Pick the last day it works.") +
        (t.max_days ? ` Up to ${wbPlural(t.max_days, "day")} from today.` : "");
    }
  }

  // The request as the server takes it (see the design's request JSON). For the summary, a name
  // not typed yet is stood in for, so the doors and end time show straight away.
  function wbRequest(f, forPreview) {
    const body = { type: f.type, name: f.name.trim() || (forPreview ? WB_PREVIEW_NAME : ""), count: f.count };
    // The manager's doors go only when changed, so someone who may not change them never trips.
    const defaults = f.t.door_ids || [];
    if (f.doors.size !== new Set(defaults).size || defaults.some((id) => !f.doors.has(id))) {
      body.door_ids = [...f.doors];
    }
    if (f.type === "overnight") Object.assign(body, { room_ids: [...f.rooms], checkout: f.checkout });
    if (f.type === "staff") {
      Object.assign(body, { access: wbAccessEntries(f.access, f.doorById), expires_on: f.expiresOn });
    }
    return body;
  }

  function wbSchedulePreview(f) {
    clearTimeout(f.timer);
    f.els.summary.classList.add("updating");
    f.timer = setTimeout(() => wbPreview(f), WB_PREVIEW_MS);
  }

  // Ask the server what this would make: the doors in words and when the cards stop working.
  // Only the latest answer counts. A problem shows under its field once that field has been
  // used (or Make was pressed), and as a quiet note in the summary until then.
  async function wbPreview(f) {
    clearTimeout(f.timer);
    const ask = (f.ask += 1);
    f.els.summary.classList.add("updating");
    const res = await api("POST", "/api/wristbands/preview", wbRequest(f, true));
    if (wb?.form !== f || ask !== f.ask) return;
    f.els.summary.classList.remove("updating");
    f.noteShown = false;
    if (res.status === 200) {
      f.plan = res.data.plan;
      f.planOk = true;
      f.note = "";
      for (const key of Object.keys(f.fields)) if (key !== "name") wbClearError(f, key);
    } else if (wbAuthError(res)) {
      return;
    } else {
      f.planOk = false;
      f.note = res.status === 0 ? "Can't check this right now."
        : res.status === 429 ? "Checking again when you next change something."
        : res.data.error || "This can't be made as it is.";
      const field = res.data.field;
      if (field && (f.submitted || f.touched.has(field))) f.noteShown = wbSetError(f, field, f.note);
    }
    wbDrawSummary(f);
  }

  function wbDrawSummary(f) {
    const { type, t } = f;
    const until = f.planOk ? f.plan.expires_label
      : type === "day_pass" ? t.expires_label
      : type === "overnight" ? (f.checkout ? wbExpiryLabel(f.checkout, t.checkout_time) : "—")
      : f.expiresOn ? wbExpiryLabel(f.expiresOn, WB_STAFF_UNTIL) : "—";
    fill(f.els.summary, [...wbRows([
      ["For", f.name.trim() || "—"],
      [type === "staff" ? "Cards" : "Wristbands", String(f.count)],
      ["Opens", f.planOk ? f.plan.summary || "—" : "—"],
      ["Works until", until],
    ]).children]);
    f.els.note.textContent = f.note;
    f.els.note.hidden = !f.note || f.noteShown;
    setText(f.els.submit, `Make ${wbPlural(f.count, WB_TYPES[type].noun)}`);
  }

  async function wbSubmit(f) {
    if (f.busy) return;
    f.submitted = true;
    f.els.problem.hidden = true;
    const staff = f.type === "staff";
    // Quick checks first: they save a trip and point at the field. The server checks it all.
    const missing = !f.name.trim() ? ["name", staff ? "Enter the staff member's name." : "Enter the guest's name."]
      : f.type === "overnight" && !f.rooms.size ? ["room_ids", "Pick at least one room."]
      : f.type === "overnight" && !f.checkout ? ["checkout", "Pick a checkout date."]
      : staff && !wbAccessEntries(f.access, f.doorById).length ? ["access", "Choose what this card opens."]
      : staff && !f.expiresOn ? ["expires_on", "Pick the last day it works."]
      : null;
    if (missing) {
      wbSetError(f, ...missing);
      wbFocusField(f, missing[0]);
      return;
    }
    f.busy = true;
    const button = f.els.submit;
    button.classList.add("working");
    button.setAttribute("aria-busy", "true");
    const res = await api("POST", "/api/wristbands/jobs", wbRequest(f, false));
    if (wb?.form !== f) return;
    f.busy = false;
    button.classList.remove("working");
    button.removeAttribute("aria-busy");
    if (res.status === 200) return wbShowJob(res.data.job);
    if (wbAuthError(res)) return;
    const text = res.status === 0 ? `Can't reach Staff Portal. Check this ${word()}'s connection, then try again.`
      : res.data.error || "That didn't work. Try again.";
    if (res.data.field && wbSetError(f, res.data.field, text)) return wbFocusField(f, res.data.field);
    f.els.problem.textContent = text;
    f.els.problem.hidden = false;
  }

  // ---- the encoding screen: one card after another on the encoder

  function wbShowJob(job) {
    wbQuiet();
    wb.form = null;
    wb.job = job;
    wb.lost = 0;
    wb.rev += 1;
    session.set(WB_JOB_KEY, String(job.id));
    wbEnter("job");
    const v = wb.jobView = {
      eyebrow: h("div", { class: "wb-eyebrow" }),
      visual: h("div", { class: "wb-visual", "aria-hidden": "true" }),
      step: h("div", { class: "wb-step" }),
      prompt: h("h1", { class: "wb-prompt", tabindex: "-1" }),
      dots: h("ol", { class: "wb-dots", "aria-label": "Progress" }),
      meta: h("p", { class: "wb-meta" }),
      conn: h("p", { class: "wb-conn", role: "status", hidden: true },
        "Can't reach Staff Portal right now. Still trying…"),
      actions: h("div", { class: "wb-actions" }),
      results: h("div", { class: "wb-results" }),
      box: null, phase: null, mode: null,
    };
    v.box = h("section", { class: "wb-job" }, v.eyebrow, v.visual,
      h("div", { class: "wb-say", "aria-live": "polite" }, v.step, v.prompt), v.dots, v.meta, v.conn, v.actions);
    wbShow("job", h("div", { class: "wb-head wb-job-head" },
      h("button", { class: "back-btn", type: "button", onclick: () => wbBack() }, "‹ Wristbands"),
      h("span", { class: "wb-pill-slot" })), v.box, v.results);
    v.prompt.focus({ preventScroll: true });
    wbDrawJob();
    wbScheduleJob();
  }

  function wbDrawJob() {
    const job = wb.job;
    const v = wb.jobView;
    const kind = WB_TYPES[job.type] || WB_TYPES.day_pass;
    const Noun = cap(kind.noun);
    const active = WB_ACTIVE.has(job.status);
    const total = job.cards.length || job.count;
    setText(v.eyebrow, `${kind.title} · ${job.name}`);
    setText(v.step, active && job.current && total > 1 ? `${Noun} ${job.current} of ${total}` : "");
    setText(v.prompt, job.message || "");
    if (job.phase !== v.phase) {
      v.phase = job.phase;
      const [icon, cls] = {
        waiting_for_card: ["band", "pulse"], remove_card: ["up", "lift"], needs_attention: ["alert", "alert"],
        done: ["check", "ok"], cancelled: ["stop", "quiet"],
      }[job.phase] || [null, ""];   // queued, writing: a spinner
      v.visual.className = cls ? `wb-visual ${cls}` : "wb-visual";
      fill(v.visual, icon ? wbIcon(icon) : h("div", { class: "spinner" }));
    }
    v.box.classList.toggle("attention", job.status === "needs_attention");
    v.box.classList.toggle("finished", !active);
    v.dots.hidden = total < 2;
    fill(v.dots, job.cards.map((c) => {
      const now = active && c.seq === job.current && c.status === "pending";
      const words = now ? "in progress" : c.status === "pending" ? "waiting"
        : (WB_CARD_WORDS[c.status] || c.status).toLowerCase();
      return h("li", { class: `wb-dot ${now ? "current" : c.status}` },
        h("span", { "aria-hidden": "true" }, c.status === "written" ? "✓" : String(c.seq)),
        h("span", { class: "wb-sr" }, `${Noun} ${c.seq}: ${words}`));
    }));
    setText(v.meta, [job.expires_label && `Works until ${job.expires_label}`, job.summary]
      .filter(Boolean).join(" · "));
    v.conn.hidden = wb.lost < WB_LOST_AFTER;
    wbDrawPill();

    const mode = job.status === "needs_attention" ? "attention" : active ? "active" : "finished";
    if (mode === v.mode) return;
    v.mode = mode;
    const button = (label, cls, onclick) => h("button", { type: "button", class: cls, onclick }, label);
    if (mode === "attention") {
      const retry = button("Try again", "btn primary", () => wbDecide("retry"));
      fill(v.actions, retry, button("Skip this one", "btn", () => wbDecide("skip")),
        button("Stop", "btn wb-stop", () => wbStop()));
      retry.focus({ preventScroll: true });
    } else if (mode === "active") {
      fill(v.actions, button("Stop", "btn wb-stop", () => wbStop()));
    } else {
      session.del(WB_JOB_KEY);
      if (!wb.loading) wbRefreshOptions();   // the encoder is free again: say so now
      const done = button("Done", "btn primary", () => wbBack());
      fill(v.actions, done);
      fill(v.results, h("section", { class: "group" }, h("h2", null, `${Noun}s`),
        wbRows(job.cards.map((c) => [`${Noun} ${c.seq}`,
          c.status === "written" ? c.card_number || "Made" : WB_CARD_WORDS[c.status] || cap(c.status),
          c.status === "failed"]))));
      done.focus({ preventScroll: true });
    }
  }

  function wbScheduleJob() {
    clearTimeout(wb.timer);
    if (wb.screen === "job" && wb.job && WB_ACTIVE.has(wb.job.status) && !wb.acting) {
      wb.timer = setTimeout(wbPollJob, document.visibilityState === "visible" ? WB_POLL_MS : WB_HIDDEN_POLL_MS);
    }
  }

  async function wbPollJob() {
    const state = wb;
    const job = state?.job;
    if (!job) return;
    const rev = state.rev;
    const res = await api("GET", `/api/wristbands/jobs/${job.id}`);
    // Signed out, left, or a button was pressed meanwhile (its answer is newer): drop this one.
    if (wb !== state || state.job?.id !== job.id || state.screen !== "job" || rev !== state.rev) return;
    if (res.status === 200) {
      state.lost = 0;
      state.job = res.data.job;
    } else if (wbAuthError(res)) {
      return;
    } else if (res.status === 403) {
      return wbNoAccess(res.data.error);
    } else if (res.status === 404) {
      session.del(WB_JOB_KEY);
      toast("That card job isn't available any more.", true);
      return wbHome();
    } else {
      state.lost += 1;
    }
    wbDrawJob();
    wbScheduleJob();
  }

  function wbStop() {
    if (wb.job && window.confirm(wbStopQuestion(wb.job))) wbDecide("cancel");
  }

  // Try again, Skip this one or Stop.
  async function wbDecide(action) {
    const state = wb;
    const job = state.job;
    if (state.acting || !job) return;
    state.acting = true;
    state.rev += 1;   // a check already on its way would show the job from before this
    clearTimeout(state.timer);
    state.jobView.actions.classList.add("working");
    const res = await api("POST", `/api/wristbands/jobs/${job.id}/${action}`);
    if (wb !== state || state.job?.id !== job.id) return;
    state.acting = false;
    state.jobView.actions.classList.remove("working");
    if (res.status === 200) {
      state.job = res.data.job;
      wbDrawJob();
    } else if (wbAuthError(res)) {
      return;
    } else {
      toast(res.status === 0 ? `Can't reach Staff Portal. Check this ${word()}'s connection.`
        : res.data.error || "That didn't work. Try again.", true);
    }
    wbScheduleJob();
  }

  // ---- look up a wristband

  // again: "Look up another" (already on this screen, so no new history entry).
  async function wbOpenLookup(again = false) {
    if (!again) wbEnter("lookup");
    wb.lookView = { body: h("div", { class: "wb-lookup" }), key: null };
    wb.lookup = { state: "waiting", message: "Getting the encoder ready…", card: [], job: null };
    wb.lost = 0;
    const seq = wbShow("lookup", wbSubHead("Look up a wristband"), wb.lookView.body);
    wbDrawLookup();
    const res = await api("POST", "/api/wristbands/lookup");
    if (wb?.seq === seq) wbTakeLookup(res);
  }

  async function wbPollLookup() {
    const seq = wb.seq;
    const res = await api("GET", "/api/wristbands/lookup");
    if (wb?.seq === seq) wbTakeLookup(res);
  }

  function wbTakeLookup(res) {
    if (res.status === 200) {
      wb.lost = 0;
      wb.lookup = res.data;
    } else if (wbAuthError(res)) {
      return;
    } else if (res.status === 403) {
      return wbNoAccess(res.data.error);
    } else if (res.status === 0 || res.status >= 500) {
      wb.lost += 1;
      if (wb.lost >= WB_LOST_AFTER) {
        wb.lookup = { state: "error", card: [], job: null,
          message: `Can't reach Staff Portal. Check this ${word()}'s connection, then try again.` };
      }
    } else {
      wb.lookup = { state: "error", card: [], job: null, message: res.data.error || "That didn't work. Try again." };
    }
    wbDrawLookup();
    if (wb.lookup.state === "waiting") wb.timer = setTimeout(wbPollLookup, WB_POLL_MS);
  }

  function wbDrawLookup() {
    const look = wb.lookup;
    const v = wb.lookView;
    const key = JSON.stringify(look);
    if (key === v.key) return;   // the same answer again: leave the screen (and its animation) be
    v.key = key;
    if (look.state === "waiting") {
      fill(v.body, h("section", { class: "wb-job" },
        h("div", { class: "wb-visual pulse", "aria-hidden": "true" }, wbIcon("band")),
        h("h1", { class: "wb-prompt", "aria-live": "polite" }, look.message || "Place the wristband on the encoder"),
        h("p", { class: "wb-meta" }, "Its number, who it was made for and which doors it opens will show here.")));
      return;
    }
    const job = look.job;
    const kind = job && (WB_TYPES[job.type] || WB_TYPES.day_pass);
    const until = job?.expires_label || (look.card || []).find((r) => r.expires_label)?.expires_label;
    const hero = (value, label) => h("div", { class: "hero-value" },
      h("div", { class: "hero-number" }, value), h("div", { class: "hero-label" }, label));
    fill(v.body,
      look.state === "error" ? wbNote("error", look.message || "That didn't work. Try again.") : null,
      look.state === "done" ? h("div", { class: "hero-values wb-hero" },
        hero(look.card_number || "—", "Card number"), until ? hero(until, "Works until") : null) : null,
      look.state === "done" && look.message ? wbNote("", look.message) : null,
      job ? h("section", { class: "group" }, h("h2", null, "Our record"), wbRows([
        ["Made for", job.name],
        ["Kind", `${kind.title} · ${wbPlural(job.count, kind.noun)}`],
        ["Made by", job.made_by],
        ["Made", wbWhen(job.created_at)],
        ["Opens", job.summary || null],
        ["Works until", job.expires_label || null],
      ])) : null,
      look.card?.length ? h("section", { class: "group" }, h("h2", null, "On the wristband"),
        wbRows(look.card.map((r) => [r.label, r.expires_label ? `Until ${r.expires_label}` : "—"]))) : null,
      h("div", { class: "wb-actions wb-actions-start" }, h("button", { type: "button", class: "btn primary",
        onclick: () => wbOpenLookup(true) }, look.state === "error" ? "Try again" : "Look up another")));
  }

  // ---- the card log

  function wbOpenLog() {
    wbEnter("log");
    const log = wb.log = { q: "", jobs: [], hasMore: false, loading: false, error: "", open: new Set(),
      ask: 0, timer: null, list: h("div", { class: "wb-log" }),
      moreBtn: h("button", { type: "button", class: "btn wb-more", hidden: true, onclick: () => wbLoadLog(true) },
        "Show older") };
    const search = h("input", { class: "field wb-search", type: "search", autocomplete: "off", maxlength: "60",
      placeholder: "Search by name or card number", "aria-label": "Search the card log" });
    const go = () => {
      clearTimeout(log.timer);
      log.q = search.value.trim();
      wbLoadLog(false);
    };
    search.addEventListener("input", () => {
      clearTimeout(log.timer);
      log.timer = setTimeout(go, WB_SEARCH_MS);
    });
    search.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        go();
      }
    });
    wbShow("log", wbSubHead("Card log"),
      h("p", { class: "muted wb-sub" }, wb.options?.see_all_cards
        ? "Every wristband and card made here, newest first." : "The wristbands and cards you've made, newest first."),
      search, log.list, log.moreBtn);
    if (isDesk()) search.focus({ preventScroll: true });
    wbLoadLog(false);
  }

  // older: the next page down, after what's shown.
  async function wbLoadLog(older) {
    const state = wb;
    const log = state?.log;
    if (!log) return;
    const seq = state.seq;
    const ask = (log.ask += 1);
    const params = new URLSearchParams();
    if (log.q) params.set("q", log.q);
    if (older && log.jobs.length) params.set("before", String(log.jobs[log.jobs.length - 1].created_at));
    log.loading = true;
    wbDrawLog();
    const query = params.toString();
    const res = await api("GET", `/api/wristbands/log${query ? `?${query}` : ""}`);
    if (wb !== state || state.seq !== seq || ask !== log.ask) return;
    log.loading = false;
    if (res.status === 200) {
      const jobs = Array.isArray(res.data.jobs) ? res.data.jobs : [];
      log.jobs = older ? [...log.jobs, ...jobs] : jobs;
      log.hasMore = jobs.length >= WB_LOG_PAGE;
      log.error = "";
    } else if (wbAuthError(res)) {
      return;
    } else if (res.status === 403) {
      return wbNoAccess(res.data.error);
    } else {
      log.error = res.status === 0 ? `Can't reach Staff Portal. Check this ${word()}'s connection.`
        : res.data.error || "The card log couldn't be loaded.";
    }
    wbDrawLog();
  }

  function wbDrawLog() {
    const log = wb.log;
    const focused = document.activeElement?.dataset?.job;
    const days = [];
    for (const job of log.jobs) {
      const day = wbDayName(job.created_at);
      if (days[days.length - 1]?.day !== day) days.push({ day, jobs: [] });
      days[days.length - 1].jobs.push(job);
    }
    log.list.classList.toggle("updating", log.loading && log.jobs.length > 0);
    fill(log.list,
      log.error ? wbNote("error", log.error) : null,
      days.map((d) => h("section", { class: "group" }, h("h2", null, d.day),
        h("div", { class: "readings wb-log-list" }, d.jobs.map(wbLogItem)))),
      !log.loading && !log.error && !log.jobs.length
        ? h("p", { class: "muted wb-empty" }, log.q ? `Nothing matches “${log.q}”.` : "No cards made yet.") : null,
      log.loading && !log.jobs.length ? wbSpinner() : null);
    log.moreBtn.hidden = !log.hasMore || !!log.error;
    log.moreBtn.classList.toggle("working", log.loading);
    if (focused) log.list.querySelector(`[data-job="${focused}"]`)?.focus();
  }

  function wbLogItem(job) {
    const log = wb.log;
    const kind = WB_TYPES[job.type] || WB_TYPES.day_pass;
    const open = log.open.has(job.id);
    const made = job.cards.filter((c) => c.status === "written").length;
    const [badge, tone] = job.status === "needs_attention" ? ["Needs attention", "alert"]
      : WB_ACTIVE.has(job.status) ? ["Making…", "live"]
      : made < job.count ? [made ? `${made} of ${job.count} made` : "Not made", "alert"]
      : [null, null];
    const sub = [`${kind.title} · ${wbPlural(job.count, kind.noun)}`,
      wb.options?.see_all_cards ? job.made_by : null].filter(Boolean).join(" · ");
    const row = h("button", { type: "button", class: "wb-log-row", "aria-expanded": String(open),
      "data-job": String(job.id), onclick: () => {
        if (open) log.open.delete(job.id);
        else log.open.add(job.id);
        wbDrawLog();
      } },
    h("span", { class: "wb-log-main" }, h("span", { class: "wb-log-name" }, job.name), h("small", null, sub)),
    h("span", { class: "wb-log-side" }, badge ? h("span", { class: `wb-badge ${tone}` }, badge) : null,
      h("span", null, wbTime(job.created_at))));
    if (!open) return h("div", { class: "wb-log-item" }, row);
    const Noun = cap(kind.noun);
    return h("div", { class: "wb-log-item open" }, row, h("div", { class: "wb-log-detail" },
      wbRows([
        ["Opens", job.summary || null],
        ["Works until", job.expires_label || null],
        ["Made by", job.made_by],
        ["Made", wbWhen(job.created_at)],
        ...job.cards.map((c) => [`${Noun} ${c.seq}`,
          c.status === "written" ? c.card_number || "Made" : WB_CARD_WORDS[c.status] || cap(c.status),
          c.status === "failed"]),
      ], "wb-rows"),
      WB_ACTIVE.has(job.status)
        ? h("button", { type: "button", class: "btn primary", onclick: () => wbShowJob(job) }, "Show progress")
        : null));
  }

  // ---- small pieces

  function wbSpinner() {
    return h("div", { class: "wb-loading" }, h("div", { class: "spinner", "aria-label": "Loading" }));
  }

  function wbIcon(name, cls = null) {
    const el = h("span", { class: cls, "aria-hidden": "true" });
    // Fixed markup from WB_ICONS, never data.
    el.innerHTML = '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" ' +
      `stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${WB_ICONS[name]}</svg>`;
    return el;
  }

  function wbNote(tone, text, title = null) {
    return h("div", { class: tone ? `wb-note ${tone}` : "wb-note", role: tone === "error" ? "alert" : null },
      title ? h("strong", null, title) : null, text);
  }

  // Label/value rows, as [label, value, alert?]. Rows whose value is null are left out.
  function wbRows(rows, cls = "readings") {
    return h("div", { class: cls }, rows.filter(([, value]) => value != null).map(([label, value, alert]) =>
      h("div", { class: alert ? "reading-row alert" : "reading-row" },
        h("span", null, label), h("span", { class: "reading-value" }, value))));
  }

  function wbPlural(n, noun) {
    return `${n} ${noun}${n === 1 ? "" : "s"}`;
  }

  // "8:00 PM" for "20:00", as the server writes times.
  function wbClock(hhmm) {
    const [hours, minutes] = String(hhmm || "").split(":").map(Number);
    if (!Number.isInteger(hours) || !Number.isInteger(minutes)) return String(hhmm || "");
    return `${hours % 12 || 12}:${String(minutes).padStart(2, "0")} ${hours < 12 ? "AM" : "PM"}`;
  }

  // Dates are YYYY-MM-DD at the property; noon UTC keeps the arithmetic clear of time zones.
  function wbAddDays(iso, days) {
    const when = new Date(`${iso}T12:00:00Z`);
    if (Number.isNaN(when.getTime())) return iso;
    when.setUTCDate(when.getUTCDate() + days);
    return when.toISOString().slice(0, 10);
  }

  function wbDaysBetween(from, to) {
    return Math.round((Date.parse(`${to}T12:00:00Z`) - Date.parse(`${from}T12:00:00Z`)) / 86400000);
  }

  // "Fri, Oct 17" for a YYYY-MM-DD date, with the year if it isn't this year.
  function wbDateText(iso) {
    const when = new Date(`${iso}T12:00:00Z`);
    if (Number.isNaN(when.getTime())) return iso;
    const options = { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" };
    if (iso.slice(0, 4) !== (wb.options?.today || "").slice(0, 4)) options.year = "numeric";
    return new Intl.DateTimeFormat([], options).format(when);
  }

  // "11:00 AM tomorrow": when a card made for that date stops working, in the server's words.
  function wbExpiryLabel(iso, hhmm) {
    const today = wb.options.today;
    const day = iso === today ? "today" : iso === wbAddDays(today, 1) ? "tomorrow" : wbDateText(iso);
    return `${wbClock(hhmm)} ${day}`;
  }

  // Times as the property's clock shows them (the server says which time zone that is).
  function wbFormat(ts, options, locale = []) {
    const when = new Date(Number(ts) * 1000);
    try {
      const timeZone = wb?.options?.time_zone || undefined;
      return new Intl.DateTimeFormat(locale, { ...options, timeZone }).format(when);
    } catch {
      return new Intl.DateTimeFormat(locale, options).format(when);   // a time zone this browser doesn't know
    }
  }

  const wbTime = (ts) => wbFormat(ts, { hour: "numeric", minute: "2-digit" });
  const wbWhen = (ts) => wbFormat(ts, { weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit" });

  // "Today", "Yesterday" or "Fri, Oct 17": the day at the property a job was made on.
  function wbDayName(ts) {
    const iso = wbFormat(ts, { year: "numeric", month: "2-digit", day: "2-digit" }, "en-CA");   // YYYY-MM-DD
    const today = wb.options?.today;
    if (iso === today) return "Today";
    if (today && iso === wbAddDays(today, -1)) return "Yesterday";
    return wbDateText(iso);
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
    // Signing out while wristbands are being made stops them first (after asking).
    if (!(await wbLeave())) return;
    if (me?.shared) await api("POST", "/api/signout");
    session.del(SESSION_KEY);
    askForPin();
  });

  let screenKeys = null;
  document.addEventListener("keydown", (ev) => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (screenKeys) return screenKeys(ev);
    // Esc steps back on the Wristbands screens (where nothing would be lost), even from a field.
    if (ev.key === "Escape" && view.tool === "wristbands" && wb && wbEscape(ev)) {
      ev.preventDefault();
      return;
    }
    // Computer shortcuts: 1-9 switch pages, Esc closes a card's panel.
    if (!document.body.classList.contains("desk-shell") || /INPUT|SELECT|TEXTAREA/.test(ev.target.tagName)) return;
    if (ev.key === "Escape" && view.detail) {
      ev.preventDefault();
      closeDetail();
    } else if (/^[1-9]$/.test(ev.key)) {
      const page = lastData?.layout?.pages?.[Number(ev.key) - 1];
      if (page && (page.id !== view.pageId || view.tool)) openPage(page.id);
    }
  });

  // Closing or reloading the page while cards are being made: the browser asks first. (The job
  // carries on either way, and the page goes back to it after a reload.)
  window.addEventListener("beforeunload", (ev) => {
    if (wbHolding()) {
      ev.preventDefault();
      ev.returnValue = "";
    }
  });

  // The screen turning off (into a pocket, say) or switching to another app locks the controls
  // straight away, so nothing can be pressed by accident while it's out of sight.
  const lockWhenHidden = () => { if (lock.enabled && lock.until) relock(); };
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") drawLock();
    if (document.visibilityState === "visible" && live) refresh();
    else if (document.visibilityState === "hidden") {
      clearTimeout(pollTimer);
      lockWhenHidden();
    }
  });
  window.addEventListener("pagehide", lockWhenHidden);

  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    installPrompt = e;
  });

  if (!PREVIEW && "serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => { /* optional */ });
  }

  boot();
})();
