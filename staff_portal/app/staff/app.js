(() => {
  "use strict";

  // No pinch zoom on phones (Safari ignores the page's user-scalable=no; double taps are stopped
  // in app.css).
  for (const type of ["gesturestart", "gesturechange"]) {
    document.addEventListener(type, (ev) => ev.preventDefault(), { passive: false });
  }
  document.addEventListener("touchmove", (ev) => {
    if (ev.touches.length > 1) ev.preventDefault();
  }, { passive: false });

  const TOKEN_KEY = "staff_device_token";
  const SESSION_KEY = "staff_pin_session";
  const POLL_MS = 5000;
  const FAST_POLL_MS = 1500;   // while a change is waiting to be confirmed by the device
  const CONFIRM_MS = 90000;    // how long a change may take to show up before we say so
  const PIN_PAUSE_MS = 500;    // a PIN of unknown length signs in once typing stops this long
                               // (unless Settings says otherwise: me.pin_pause_ms)
  const PIN_LENGTHS = new Set([4, 5, 6, 7, 8]);

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

  // Some card encoders also type each card's number, as a keyboard would, when a card is put on
  // them, often with Enter after it. Nobody types that fast: once enough keys have come that
  // close together, they and the Enter or Tab after them are kept from the page, and what the
  // first few typed into a box is taken back, so a card can't fill in the PIN pad or a name,
  // or press a button.
  const TYPED_GAP_MS = 50;     // keys this close together...
  const TYPED_KEYS = 5;        // ...this many in a row are the encoder (a card number has 8 or more)
  const TYPED_END_MS = 200;    // its Enter or Tab comes within this of its last key
  const typing = { last: -Infinity, count: 0, card: false, field: null, value: "", from: 0, to: 0 };
  let onCardTyped = null;      // what the screen on show does about one (the PIN pad forgets it)
  window.addEventListener("keydown", (ev) => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.isComposing) return;
    const gap = ev.timeStamp - typing.last;
    if (ev.key.length === 1 && !ev.repeat) {   // a held-down key repeats quickly too: a person
      if (gap > TYPED_GAP_MS) {
        // Perhaps a card number starting: remember the box as it was before it.
        const field = /^(INPUT|TEXTAREA)$/.test(ev.target.tagName) ? ev.target : null;
        Object.assign(typing, { count: 0, card: false, field, value: field?.value ?? "",
          from: field?.selectionStart ?? 0, to: field?.selectionEnd ?? 0 });
      }
      typing.last = ev.timeStamp;
      typing.count += 1;
      if (typing.count < TYPED_KEYS) return;
      if (!typing.card) {
        typing.card = true;
        cardTyped();
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

  // Whether the key just pressed came too soon after the one before for a person: perhaps a
  // card number on its way.
  const typingFast = () => typing.count > 1;

  // A card number was typed: undo what its first keys did.
  function cardTyped() {
    const field = typing.field;
    if (field?.isConnected && field.value !== typing.value) {
      field.value = typing.value;
      try {
        field.setSelectionRange(typing.from, typing.to);
      } catch { /* a box without a cursor */ }
      field.dispatchEvent(new Event("input", { bubbles: true }));
    }
    onCardTyped?.();
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
    onCardTyped = null;
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

  // The property name from Settings, for messages ("Can't reach Hot Lake Lodge").
  function portalName() {
    return me?.property_name || "Staff Portal";
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
    rpReset();
    tkReset();
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
    let pause = null;
    // Every PIN that can sign in here has this many digits (the server says so only when that's
    // true for everyone): sign in as soon as it's typed. Otherwise sign in after a short pause.
    let length = PIN_LENGTHS.has(me?.pin_length) ? me.pin_length : null;

    const draw = () => {
      dots.classList.toggle("checking", busy);
      dots.replaceChildren(...Array.from({ length: Math.max(length || 4, pin.length) }, (_, i) =>
        h("span", { class: i < pin.length ? "dot on" : "dot" })));
    };
    // auto: signed in by itself at the known length, which the server checks is still right.
    const submit = async (auto = false) => {
      clearTimeout(pause);
      if (busy || pin.length < 4) return;
      busy = true;
      draw();
      const body = member ? { pin, staff_id: member.id } : { pin };
      if (auto && length) body.length = length;
      const res = await api("POST", "/api/unlock", body);
      busy = false;
      if (!pad.isConnected) return;   // another screen took over meanwhile
      if (res.status === 200) {
        session.set(SESSION_KEY, res.data.session);
        return boot();
      }
      if (res.status === 423 || res.status === 401 || res.status === 0) return handleAuthError(res);
      // 404: the device was switched to shared sign-in (or the person was removed): ask again.
      if (res.status === 404) return showWho();
      if (res.status === 409 && res.data.error === "pin_length_changed") {
        // Someone's PIN has a different length now: nothing was checked, so keep what's typed
        // and let them finish (then OK, or a pause, signs in).
        length = PIN_LENGTHS.has(res.data.pin_length) ? res.data.pin_length : null;
        if (me) me.pin_length = length;
        draw();
        return typed();
      }
      pin = "";
      draw();
      error.textContent = res.data.error || "Wrong PIN.";
    };
    // After each digit: sign in at the known length, else once they stop typing (or at 8 digits,
    // the most a PIN has). Never with fewer than 4, and never again by itself after a failure.
    const typed = () => {
      clearTimeout(pause);
      if (busy || pin.length < 4) return;
      if (length) {
        if (pin.length !== length) return;
        // Typed faster than a person can: wait a moment, in case it's a card number on the
        // encoder (which clears the digits instead).
        if (typingFast()) pause = setTimeout(() => { if (pad.isConnected) submit(true); }, TYPED_END_MS);
        else submit(true);
        return;
      }
      if (pin.length >= 8) return submit();
      const wait = Number.isInteger(me?.pin_pause_ms) ? me.pin_pause_ms : PIN_PAUSE_MS;
      pause = setTimeout(() => { if (pad.isConnected) submit(); }, wait);
    };
    const press = (k) => {
      if (busy) return;   // the PIN typed is being checked
      if (k === "ok") return submit();
      if (k === "del") pin = pin.slice(0, -1);
      else if (pin.length < 8) pin += k;
      error.textContent = "";
      draw();
      if (k !== "del") typed();
      else clearTimeout(pause);
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
    onCardTyped = () => {
      if (busy) return;
      clearTimeout(pause);
      pin = "";
      draw();
      error.textContent = "That was a card on the encoder. Type your PIN.";
    };
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
  // ("wristbands", "tasks" or "reports"), or null.
  const view = { pageId: session.get("page"), detail: null,
    tool: ["wristbands", "tasks", "reports"].includes(session.get("tool")) ? session.get("tool") : null };
  // The sign-in a page or tool was last chosen in (or the start page applied in): a department's
  // start page opens once per sign-in, and never over something chosen in it.
  const START_KEY = "start_for";
  const signInMark = () => session.get(SESSION_KEY) || "device";
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

  // Where to open: their department's start page (the server sends it only when they can see
  // it), once per sign-in (mark), and never over a page or tool chosen in it (chosenIn: the
  // sign-in the last choice was made in). One they can't open here: null, as it always was.
  function startChoice(start, chosenIn, mark, tools, pageIds) {
    if (!start || chosenIn === mark) return null;
    if (start.tool && tools.includes(start.tool)) return { tool: start.tool };
    if (start.page && pageIds.includes(start.page)) return { page: start.page };
    return null;
  }

  function render(data) {
    data.entities = data.entities.map(withExpected);
    lastData = data;
    // Their department's start page, once per sign-in.
    const start = startChoice(data.start, session.get(START_KEY), signInMark(), toolsFor(data),
      (data.layout?.pages || []).map((p) => p.id));
    session.set(START_KEY, signInMark());
    if (start?.tool) {
      view.tool = start.tool;
      session.set("tool", start.tool);
    } else if (start?.page) {
      view.tool = null;
      session.del("tool");
      view.pageId = start.page;
      session.set("page", start.page);
    }
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
      if (wb || tk || rp) toast(`${TOOL_NAMES[view.tool]} isn't available on this ${word()} any more.`, true);
      wbReset();
      tkReset();
      rpReset();
      view.tool = null;
      session.del("tool");
    }
    const nothingElse = !data.entities.length || (data.layout && !data.layout.pages.length);
    if (!view.tool && nothingElse && tools.length) view.tool = tools[0];
    wbLinkSync();   // this computer's encoder link is open only while Wristbands is
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
    if (tk) tkCloseSheet(true);
    view.tool = null;
    session.del("tool");
    view.pageId = id;
    view.detail = null;
    session.set("page", id);
    session.set(START_KEY, signInMark());
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
          h("div", { class: "desk-name" }, portalName()),
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
  // card; Tasks to anyone signed in with their own PIN; Reports to someone signed in with a
  // department that may see at least one. The server says so with every poll, and checks again on
  // every request.
  const TOOL_NAMES = { wristbands: "Wristbands", tasks: "Tasks", reports: "Reports" };
  const TOOL_PAUSE_MS = 60000;   // after the server turns a tool down, how long before offering it again
  let toolsPaused = 0;

  function toolsFor(data) {
    if (PREVIEW || !data) return [];
    const paused = Date.now() < toolsPaused;
    return [data.wristbands && !paused ? "wristbands" : null,
      data.tasks && Date.now() >= tkPaused ? "tasks" : null,
      data.reports && !paused ? "reports" : null].filter(Boolean);
  }

  function openTool(name) {
    if (view.tool === name) {
      // Already open: back to its first screen, the way a tab bar's current tab does.
      if (name === "wristbands" && wb && wb.screen !== "home") wbBack();
      if (name === "tasks") window.scrollTo({ top: 0, behavior: "smooth" });
      if (name === "reports" && rp?.open) rpBack();
      return;
    }
    if (wbHolding()) {
      wbLeave().then((ok) => { if (ok) openTool(name); });
      return;
    }
    if (view.tool === "wristbands") wbAway();
    if (tk) tkCloseSheet(true);
    view.tool = name;
    view.detail = null;
    session.set("tool", name);
    session.set(START_KEY, signInMark());
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
    if (tk) tkCloseSheet(true);
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
    const tasks = view.tool === "tasks";
    into.set(`tool:${view.tool}`, { refresh: () => (tasks ? tkPoll() : view.tool === "wristbands" ? wbPoll() : null) });
    // Kept across redraws, so what's typed survives them.
    const content = tasks ? tkState().root : view.tool === "reports" ? rpState().root : wbState().root;
    if (tasks) tkDraw();   // a phone turned sideways, say: the right layout for the new width
    if (isDesk()) {
      const nav = h("nav", { class: "desk-nav", "aria-label": "Pages" },
        toolNav(), data.layout ? pageNav(data.layout.pages, -1) : areaNav(data.entities));
      return deskShell({ nav, title: TOOL_NAMES[view.tool], content, actions: null });
    }
    const tabs = tabBar(data, -1);
    return h("div", { class: tabs ? "layout has-tabs" : "layout" }, content, tabs);
  }

  // ---------------------------------------------------------------- reports

  // Reports (app/reports/<key>/ in the add-on, see REPORTS.md): the ones this person may see, and
  // one open at a time. A report's view.js registers itself with StaffReports and draws into the
  // box it's given; the server checks access on every request. What's on screen is forgotten at
  // sign-out (rpReset), so the next person never sees it.
  const reportViews = new Map();     // key -> { render } from its view.js
  const reportWaiting = new Map();   // key -> resolve, while its view.js loads
  let rp = null;

  window.StaffReports = Object.freeze({
    register(key, def) {
      if (typeof key !== "string" || typeof def?.render !== "function" || reportViews.has(key)) return;
      reportViews.set(key, def);
      reportWaiting.get(key)?.(true);
    },
  });

  function rpState() {
    if (!rp) {
      rp = { root: h("div", { class: "rp" }), list: null, open: null, params: {}, seq: 0 };
      rpHome();
    }
    return rp;
  }

  function rpReset() {
    rp = null;
  }

  // The server says this person may not see reports here (any more): back to the pages.
  function rpNoAccess(text) {
    toast(text || "You don't have access to reports here.", true);
    rpReset();
    toolsPaused = Date.now() + TOOL_PAUSE_MS;
    view.tool = null;
    session.del("tool");
    rerender();
  }

  // Sign-in problems go to their own screens; anything else is the caller's to show.
  function rpAuthError(res) {
    if (res.status !== 0 && handleAuthError(res)) return true;
    if (res.status === 403 && /Sign in with your own PIN/.test(res.data.error || "")) {
      rpNoAccess(res.data.error);
      return true;
    }
    return false;
  }

  function rpMessage(title, text, retry) {
    return h("div", { class: "rp-msg" }, h("h2", null, title), h("p", null, text),
      retry ? h("button", { class: "btn", type: "button", onclick: retry }, "Try again") : null);
  }

  async function rpHome() {
    const state = rp;
    state.open = null;
    const seq = ++state.seq;
    fill(state.root, h("h1", { class: "page-title" }, "Reports"),
      h("div", { class: "center" }, h("div", { class: "spinner", "aria-label": "Loading" })));
    const res = await api("GET", "/api/reports");
    if (rp !== state || seq !== state.seq || rpAuthError(res)) return;
    if (res.status !== 200) {
      fill(state.root, h("h1", { class: "page-title" }, "Reports"),
        rpMessage("Reports couldn't be loaded", res.data.error || "Check the connection.", () => rpHome()));
      return;
    }
    state.list = res.data.reports;
    fill(state.root, h("h1", { class: "page-title" }, "Reports"),
      state.list.length ? h("div", { class: "rp-list" }, state.list.map((r) =>
        h("button", { class: "rp-item", type: "button", onclick: () => rpOpen(r) },
          h("span", { class: "rp-item-name" }, r.name),
          r.description ? h("span", { class: "rp-item-sub" }, r.description) : null,
          h("span", { class: "rp-item-go", "aria-hidden": "true" }, "›"))))
        : rpMessage("No reports yet", "Your manager hasn't shared any reports with you."));
  }

  // Load a report's view.css and view.js once (same address as the app, so the page's strict
  // rules still hold). Resolves true once its view has registered.
  function rpLoad(report) {
    if (reportViews.has(report.key)) return Promise.resolve(true);
    if (report.view_css && !document.querySelector(`link[data-report="${report.key}"]`)) {
      document.head.append(h("link", { rel: "stylesheet", href: report.view_css, "data-report": report.key }));
    }
    return new Promise((resolve) => {
      const done = (ok) => { reportWaiting.delete(report.key); clearTimeout(timer); resolve(ok); };
      const timer = setTimeout(() => done(false), 20000);
      reportWaiting.set(report.key, done);
      document.querySelector(`script[data-report="${report.key}"]`)?.remove();   // a failed try
      document.head.append(h("script", { src: report.view_js, "data-report": report.key,
        onerror: () => done(false) }));
    });
  }

  async function rpOpen(report, params = {}) {
    const state = rp;
    state.open = report;
    state.params = params;
    state.body = h("div", { class: "rp-body" });
    history.pushState({ rp: report.key }, "", "/");
    window.scrollTo(0, 0);
    fill(state.root, h("div", { class: "rp-head" },
      h("button", { class: "back-btn", type: "button", onclick: () => rpBack() }, "‹ Reports"),
      h("h1", { class: "page-title rp-title" }, report.name)), state.body);
    const seq = ++state.seq;
    fill(state.body, h("div", { class: "center" }, h("div", { class: "spinner", "aria-label": "Loading" })));
    if (!await rpLoad(report)) {
      if (rp === state && seq === state.seq) {
        fill(state.body, rpMessage("This report couldn't be loaded", "Check the connection.",
          () => rpOpen(report, params)));
      }
      return;
    }
    if (rp === state && seq === state.seq) rpRun(params);
  }

  function rpBack() {
    if (history.state?.rp) history.back();
    else rpHome();
  }

  // Run the open report's data() on the server and draw it.
  async function rpRun(params) {
    const state = rp;
    const report = state.open;
    state.params = params;
    const seq = ++state.seq;
    state.body.classList.add("rp-loading");
    // Reports may ask Connecteam only a few things a minute, so a first look can take a while.
    const slow = setTimeout(() => {
      if (rp === state && seq === state.seq) {
        state.body.prepend(h("p", { class: "rp-muted rp-slow", role: "status" },
          "Still asking Connecteam. The first look at a month can take a few minutes."));
      }
    }, 5000);
    const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v != null)).toString();
    // A long first look carries on on the add-on (202): ask again until it's ready.
    let res = await api("GET", `/api/reports/${report.key}${query ? `?${query}` : ""}`);
    for (let tries = 0; res.status === 202 && res.data.pending && tries < 60; tries++) {
      await new Promise((ok) => setTimeout(ok, 3000));
      if (rp !== state || seq !== state.seq || state.open !== report) { clearTimeout(slow); return; }
      res = await api("GET", `/api/reports/${report.key}${query ? `?${query}` : ""}`);
    }
    clearTimeout(slow);
    if (rp !== state || seq !== state.seq || state.open !== report || rpAuthError(res)) return;
    state.body.classList.remove("rp-loading");
    if (res.status === 403) {
      toast(res.data.error || "You don't have access to that report.", true);
      rpHome();
      return;
    }
    if (res.status !== 200) {
      fill(state.body, rpMessage(res.status === 503 ? "Connecteam can't answer right now"
        : "This report couldn't be made", res.data.error || "Check the connection.", () => rpRun(params)));
      return;
    }
    const box = h("div", { class: "rp-view" });
    fill(state.body, box);
    try {
      reportViews.get(report.key).render(box, res.data.data, rpApi(report, res.data));
    } catch (err) {
      console.error(`Report ${report.key} couldn't be drawn`, err);
      fill(state.body, rpMessage("This report couldn't be shown", "Try again, or tell your manager.",
        () => rpRun(params)));
    }
  }

  // What a report's render(el, data, api) can use.
  function rpApi(report, answer) {
    const state = rp;
    const current = () => rp === state && state.open === report;
    return Object.freeze({
      h, toast,
      manage: !!answer.manage,
      settings: answer.settings || {},
      params: answer.params || {},
      report,
      refresh: (params = {}) => { if (current()) rpRun({ ...state.params, ...params }); },
      async upload(key, file) {
        if (!current() || !(file instanceof Blob)) return { ok: false };
        const res = await rpSend("POST", `/api/reports/${report.key}/uploads/${encodeURIComponent(key)}`, file);
        if (!current() || rpAuthError(res)) return { ok: false };
        if (res.status !== 200) {
          toast(res.data.error || "The file couldn't be uploaded.", true);
          return { ok: false, error: res.data.error };
        }
        toast("Uploaded.");
        rpRun(state.params);
        return { ok: true };
      },
      async saveSettings(obj) {
        if (!current()) return { ok: false };
        const res = await api("PUT", `/api/reports/${report.key}/settings`, obj);
        if (!current() || rpAuthError(res)) return { ok: false };
        if (res.status !== 200) {
          toast(res.data.error || "The settings couldn't be saved.", true);
          return { ok: false, error: res.data.error };
        }
        toast("Saved.");
        rpRun(state.params);
        return { ok: true, settings: res.data.settings };
      },
      download: rpDownload,
      csvCell: (v) => (/[",\n\r]/.test(String(v ?? "")) ? `"${String(v).replace(/"/g, '""')}"` : String(v ?? "")),
    });
  }

  // A file upload, with the app's sign-in (api() only sends JSON).
  async function rpSend(method, path, file) {
    const headers = { "X-Active": "1" };
    const token = local.get(TOKEN_KEY);
    if (token) headers.Authorization = "Bearer " + token;
    const pinSession = session.get(SESSION_KEY);
    if (pinSession) headers["X-Session"] = pinSession;
    const form = new FormData();
    form.append("file", file, file.name || "upload");
    let res;
    try {
      res = await fetch(path, { method, headers, body: form });
    } catch {
      return { status: 0, data: {} };
    }
    let data = {};
    try { data = await res.json(); } catch { /* empty body */ }
    return { status: res.status, data };
  }

  // Save text as a file on this device (a CSV, say).
  function rpDownload(name, text, type = "text/csv") {
    const link = h("a", {
      href: URL.createObjectURL(new Blob([text], { type: `${type};charset=utf-8` })),
      download: String(name || "report.csv").replace(/[\\/:*?"<>|\u0000-\u001f]+/g, "-").slice(0, 120),
    });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 30000);
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
    if (tk?.sheet && !ev.state?.tk) {   // Back closes a task's sheet
      tkCloseSheet(true);
      return;
    }
    if (view.tool === "wristbands" && wb && wb.screen !== "home" && !ev.state?.wb) {
      wbPopped();
      return;
    }
    if (view.tool === "reports" && rp?.open && !ev.state?.rp) {
      rpHome();
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
  // overnight guests), contractor badges and staff key cards. Staff Portal talks to the encoder
  // itself, so the TT Hotel sign-in details never reach this page; this screen asks for cards and
  // follows along.
  // (In This computer (USB) mode the encoder's requests pass through this page on their way to
  // the encoder: see wbLink.) What it offers is only a guide: the server checks every request
  // against the person's permissions.
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
    contractor: { title: "Contractor badge", noun: "badge", icon: "badge" },
    staff: { title: "Staff card", noun: "card", icon: "card" },
  };
  const WB_CARD_WORDS = { written: "Made", failed: "Didn't work", skipped: "Skipped",
    cancelled: "Not made", pending: "Not made", cleared: "Cleared" };
  // A staff card's state, as the card log and look-up show it. "expired" isn't stored: it's an
  // active card past its last day. A state not listed here (damaged data) shows as unknown and
  // offers no Revoke.
  const WB_STATE_WORDS = { active: "Active", expired: "Expired", revoked: "Revoked", lost: "Lost" };
  // Line icons (24px grid, drawn with currentColor).
  const WB_ICONS = {
    band: '<rect x="2.5" y="8.5" width="19" height="7" rx="3.5"/><rect x="8.5" y="6" width="7" height="12" rx="2"/>',
    moon: '<path d="M19.5 14.6A7.5 7.5 0 0 1 9.4 4.5a7.5 7.5 0 1 0 10.1 10.1z"/>',
    card: '<rect x="3" y="5.5" width="18" height="13" rx="2.5"/><circle cx="9" cy="11" r="2"/>' +
      '<path d="M5.8 15.8c.7-1.4 1.8-2 3.2-2s2.5.6 3.2 2M14.5 10h4M14.5 13h3"/>',
    badge: '<rect x="5" y="4.5" width="14" height="17" rx="2.5"/><rect x="9.5" y="2.5" width="5" height="4" rx="1"/>' +
      '<circle cx="12" cy="11.5" r="2"/><path d="M8.8 17.5c.6-1.3 1.7-1.9 3.2-1.9s2.6.6 3.2 1.9"/>',
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
        linkNote: h("div", { class: "wb-link" }), noteKey: null,
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
    if (wb) {
      wbQuiet();
      wb = null;
    }
    wbLinkSync();   // the encoder link goes with the screen
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
    wb.noteKey = null;   // the note under the header was taken off with the rest: draw it again
    wbDrawPill();
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
    if (job.kind === "revoke") return `Stop revoking card ${job.target_card}? It keeps working until it's revoked.`;
    if (job.kind === "notice") return "Stop making the lost card notice?";
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

  // The add-on's last problem with the encoder, with Reconnect (Automatic and Address modes; in
  // This computer (USB) mode wbDrawLinkNote shows it).
  function wbEncoderNote(error) {
    const box = wbNote("error", error, "The card encoder isn't answering");
    box.append(h("button", { type: "button", class: "link-btn wb-note-action", onclick: wbReconnect },
      "Reconnect"));
    return box;
  }

  // Reconnect: the add-on connects to the encoder now and reads it, so the status says how it
  // is now. (Otherwise a problem stays on show until the encoder is next used.)
  async function wbReconnect(ev) {
    const button = ev.currentTarget;
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "Reconnecting…";
    const res = await api("POST", "/api/wristbands/encoder/reconnect");
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = label;
    }
    if (wbAuthError(res)) return;
    if (res.status === 200) toast("The card encoder is ready.");
    else toast(res.data.error || "Couldn't reach the card encoder. Try again.", true);
    if (wb && !wb.loading) wbRefreshOptions();
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
      wbLinkSync();
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
        wbNote("error", res.status === 0 ? `Can't reach ${portalName()}. Check this ${word()}'s connection.`
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
    // In This computer (USB) mode this page knows best whether the encoder can be reached; the
    // add-on's own last problem (the encoder unplugged, say) counts once it can.
    const local = wbLinkView();
    const [cls, text] = local?.cls === "error" ? [local.cls, local.text]
      : enc?.error && !making && (!local || local.cls === "ok") ? ["error", "Encoder offline"]
      : enc?.busy || making ? ["busy", "Encoder busy"]
      : local ? [local.cls, local.text]
      : enc?.connected ? ["ok", "Encoder ready"]
      : ["", "Checking encoder…"];
    wb.pill.className = cls ? `wb-pill ${cls}` : "wb-pill";
    wb.pill.title = (local ? local.title || (local.cls === "ok" && enc?.error) : enc?.error) || "";
    fill(wb.pill, h("span", { class: "wb-pill-dot", "aria-hidden": "true" }), text);
    wbDrawLinkNote(local);
  }

  // The encoder status sits in the page header on a computer, beside the title on a phone.
  function wbPlacePill() {
    if (!wb?.root.isConnected) return;
    const slot = isDesk() ? screen.querySelector(".desk-actions") : wb.root.querySelector(".wb-pill-slot");
    if (slot && wb.pill.parentNode !== slot) slot.append(wb.pill);
  }

  // In This computer (USB) mode, a note under the header on every Wristbands screen while
  // something keeps this computer's encoder from connecting, and on the list the add-on's own
  // last encoder problem once it does connect. (Otherwise the list shows that problem itself.)
  function wbDrawLinkNote(local) {
    const enc = wb.options?.encoder;
    const [tone, title, text, action] = local?.title
      ? [local.cls, local.title, local.note, local.action]
      : local?.cls === "ok" && enc?.error && wb.screen === "home"
        ? ["error", "The card encoder isn't answering", enc.error, ["Reconnect", wbReconnect]]
        : [];
    const note = wb.linkNote;
    const key = title ? JSON.stringify([tone, title, text, action?.[0]]) : "";
    if (key === wb.noteKey) return;   // unchanged: not announced again
    wb.noteKey = key;
    if (!title) {
      note.remove();
      return;
    }
    const box = wbNote(tone, text, title);
    if (action) {
      box.append(h("button",
        { type: "button", class: "link-btn wb-note-action", onclick: action[1] }, action[0]));
    }
    fill(note, box);
    const head = wb.root.querySelector(".wb-head");
    if (head) head.after(note);
    else wb.root.prepend(note);
  }

  // ---- this computer's encoder: This computer (USB) mode

  // In This computer (USB) mode the add-on never connects to the desk computer: this page does
  // it for the add-on. While Wristbands is open here, it holds a link to the add-on (a WebSocket
  // on this app's own address, so it works through Cloudflare Tunnel) and a connection to TT
  // Hotel's SN Web Socket on this computer, and passes each encoder request from the one to the
  // other, one at a time. Only the requests the add-on's encoder runner makes (encoder.py) are
  // passed on, each checked against WB_RELAYED; anything else is dropped. The encoder's key from
  // TT Hotel (hotelInfo) passes through this page in those requests: it's never logged or kept,
  // and SN Web Socket's copy of it in each answer is left out of what goes back.
  //
  // The messages (encoder.LocalRelay has the add-on's side). From the add-on: {"type": "call",
  // "request": {"function", "requestId", ...}}. To it: {"type": "reply", "reply": <SN Web
  // Socket's answer>}, and {"type": "status", "helper", "version"} on linking, on every change,
  // every WB_STATUS_MS and whenever a request can't be passed on. helper is "ready",
  // "unreachable", "blocked" (the browser won't let this page reach it) or null (connecting, or
  // the browser is asking whether it may); either of the first two problems makes the add-on
  // stop waiting for the request in hand.

  // SN Web Socket on this computer: localhost as well, for one that only listens on IPv6 (::1).
  const WB_HELPER_URLS = ["ws://127.0.0.1:8102", "ws://localhost:8102"];
  const WB_TICKET_PATH = "/api/encoder/ticket";   // a one-time pass for the link (about 30 seconds)
  const WB_LINK_PATH = "/api/encoder/link";       // the link: a WebSocket on this app's own address
  const WB_LINK_RETRY_MS = [1000, 30000];     // waits before linking again: the first, the longest
  const WB_HELPER_RETRY_MS = [2000, 30000];   // ...and before trying SN Web Socket again
  const WB_LINK_STEADY_MS = 15000;    // lasted this long: the next retry waits the first wait again
  const WB_HELPER_OPEN_MS = 8000;     // SN Web Socket gets this long to accept a connection...
  const WB_ASKING_OPEN_MS = 60000;    // ...or this long where the browser may ask whether it may
  const WB_ASKING_AFTER_MS = 1500;    // a connection still waiting this long: the browser is asking
  const WB_ANSWERED_MS = 2000;        // answered in the site settings instead: start over after this
  const WB_CALL_MS = 15000;           // ...and this long to answer (as long as the add-on waits)
  const WB_STATUS_MS = 30000;         // the add-on hears how SN Web Socket is at least this often
  const WB_RELAY_MAX = 256 * 1024;    // largest message passed on either way (the add-on's limit)
  // Longest text in a request: an encoder key is a few hundred characters, but TT Hotel doesn't
  // say how long one may be, and one cut off here would stop every card.
  const WB_RELAY_TEXT = 16384;
  const WB_RELAY_QUEUE = 8;           // requests waiting their turn (the add-on sends one at once)
  const WB_LINK_REPLACED = 4000;      // the add-on's close code: another window's link took over
  // Chrome and Edge ask once whether a site may "access other apps and services on this device"
  // (Apps on device in the site settings). Its name since version 145, then the one before it:
  // a site blocked under the old name stays blocked while the new one says "prompt".
  const WB_NETWORK_PERMISSIONS = ["loopback-network", "local-network-access"];
  // The SN Web Socket requests the add-on's encoder runner makes, with the type of each of their
  // fields. Keep in step with encoder.py: a request or field not listed here is never passed on.
  const WB_RELAYED = {
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

  // The link while this page holds it (wbLinkSync opens and closes it): to the add-on (ws; state
  // "connecting", "open", "lost", "refused" or "replaced") and to SN Web Socket (helper).
  let wbLink = null;
  let wbPermission = null;   // the browser's local network permission, once asked (a promise)

  // Whether this page should hold the link now: Wristbands open, on a computer whose card
  // encoder is This computer (USB). The Wristbands screen only exists while someone is signed in.
  function wbLinkWanted() {
    return !PREVIEW && view.tool === "wristbands" && wbLocalEncoder(wb?.options);
  }

  // Whether the Wristbands status says this computer's encoder is This computer (USB): its own
  // flag, or the encoder's status (which says so for an encoder reached through this page).
  function wbLocalEncoder(options) {
    return options?.encoder_local === true || options?.encoder?.local === true;
  }

  // Open or close the link to match. Called whenever that may have changed (each poll and status
  // check, leaving Wristbands, signing out); does nothing when it already matches.
  function wbLinkSync() {
    if (wbLinkWanted() === !!wbLink) return;
    if (wbLink) {
      wbLinkStop();
      return;
    }
    wbLink = {
      ws: null, state: "connecting", reason: null, timer: null, delay: WB_LINK_RETRY_MS[0],
      openedAt: 0, beat: null, queue: [], calling: false,
      helper: { ws: null, url: null, state: "connecting", version: null, timer: null,
        delay: WB_HELPER_RETRY_MS[0], trying: null, pending: null, permission: null,
        opening: null },
    };
    wbLinkConnect(wbLink);
    wbHelperConnect(wbLink);
    wbLinkDrawn();
  }

  function wbLinkStop() {
    const link = wbLink;
    wbLink = null;
    clearTimeout(link.timer);
    clearInterval(link.beat);
    clearTimeout(link.helper.timer);
    link.queue = [];
    const ws = link.ws;
    link.ws = null;
    try { ws?.close(1000); } catch { /* already closing */ }
    link.helper.opening?.close();   // a connection still waiting (on the browser's question, say)
    wbHelperDrop(link);
    wbLinkDrawn();
  }

  // Something about the link changed: show it.
  function wbLinkDrawn() {
    if (wb) wbDrawPill();
  }

  // Link to the add-on: a one-time pass first (an ordinary request, so it's checked as this
  // computer and this person's sign-in), then the WebSocket with it, since a browser can't send
  // a WebSocket any other credentials.
  async function wbLinkConnect(link) {
    clearTimeout(link.timer);
    const res = await api("POST", WB_TICKET_PATH);
    if (wbLink !== link || link.ws) return;   // stopped, or linked by another try meanwhile
    if (res.status !== 200 || typeof res.data.ticket !== "string" || !res.data.ticket) {
      if (wbAuthError(res)) return;   // signed out: its own screen, which closes the link
      if (res.status === 403 || res.status === 404 || res.status === 409) {
        // Not this computer's encoder to link any more (its setting was changed, say). Try again
        // later, and check the Wristbands status now: that closes the link if so.
        link.state = "refused";
        link.reason = typeof res.data.error === "string" ? res.data.error : null;
        link.delay = WB_LINK_RETRY_MS[1];
        if (wb && !wb.loading) wbRefreshOptions();
      } else {
        link.state = "lost";
      }
      wbLinkRetry(link);
      return;
    }
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    const address = `${scheme}//${location.host}${WB_LINK_PATH}?ticket=` +
      encodeURIComponent(res.data.ticket);
    let ws;
    try {
      ws = new WebSocket(address);
    } catch {
      link.state = "lost";
      wbLinkRetry(link);
      return;
    }
    link.ws = ws;
    ws.addEventListener("open", () => {
      if (link.ws !== ws) return;
      link.state = "open";
      link.reason = null;
      link.openedAt = Date.now();
      wbLinkReport(link);
      clearInterval(link.beat);
      link.beat = setInterval(() => wbLinkReport(link), WB_STATUS_MS);
      wbLinkDrawn();
    });
    ws.addEventListener("message", (ev) => {
      if (link.ws === ws) wbLinkMessage(link, ev.data);
    });
    ws.addEventListener("close", (ev) => {
      if (link.ws === ws) wbLinkClosed(link, ev.code);
    });
  }

  function wbLinkClosed(link, code) {
    link.ws = null;
    clearInterval(link.beat);
    link.queue = [];   // nobody to answer now: the add-on gives up on them by itself
    if (code === WB_LINK_REPLACED) {
      // Wristbands was opened in another window here, which has the encoder now. This one only
      // takes it back when asked (Use it here: wbLinkResume), never by itself, since that would
      // fail a card being made in the other window.
      link.state = "replaced";
      wbLinkDrawn();
      return;
    }
    if (link.openedAt && Date.now() - link.openedAt >= WB_LINK_STEADY_MS) {
      link.delay = WB_LINK_RETRY_MS[0];
    }
    link.openedAt = 0;
    link.state = "lost";
    wbLinkRetry(link);
  }

  // Link again after a wait that doubles each time, with a little jitter so several computers
  // don't all come back at the same moment after the add-on restarts.
  function wbLinkRetry(link) {
    clearTimeout(link.timer);
    const wait = link.delay * (0.75 + Math.random() / 2);
    link.delay = Math.min(link.delay * 2, WB_LINK_RETRY_MS[1]);
    link.timer = setTimeout(() => wbLinkConnect(link), wait);
    wbLinkDrawn();
  }

  // Use it here: take the encoder back from the window that took it over.
  function wbLinkResume() {
    const link = wbLink;
    if (link?.state !== "replaced") return;
    link.state = "connecting";
    link.delay = WB_LINK_RETRY_MS[0];
    wbLinkConnect(link);
    wbLinkDrawn();
  }

  function wbLinkSend(link, message) {
    if (link.ws?.readyState !== WebSocket.OPEN) return;
    try {
      link.ws.send(JSON.stringify(message));
    } catch { /* closing: its close event follows */ }
  }

  // Tell the add-on how SN Web Socket is (and that this link is still here), in its words:
  // connecting, or waiting for the browser's permission, is null (not known yet), and a question
  // nobody answered is blocked (until someone does).
  function wbLinkReport(link) {
    const state = link.helper.state === "unanswered" ? "blocked" : link.helper.state;
    wbLinkSend(link, { type: "status", version: link.helper.version,
      helper: ["ready", "unreachable", "blocked"].includes(state) ? state : null });
  }

  // A message from the add-on. A request that isn't allowed is dropped, never passed on.
  function wbLinkMessage(link, text) {
    const message = wbParseObject(text);
    if (message?.type === "call") {
      const request = wbRelayRequest(message.request);
      if (!request || link.queue.length >= WB_RELAY_QUEUE) {
        // Never the request itself: it may hold the encoder's key.
        console.warn(request ? "Wristbands: too many encoder requests at once; one was dropped"
          : "Wristbands: dropped an encoder request that isn't allowed");
        return;
      }
      link.queue.push({ request, ws: link.ws });
      wbRelayNext(link);
    }
  }

  // A JSON object from a WebSocket message, or null for anything else (or anything too big).
  function wbParseObject(text) {
    if (typeof text !== "string" || text.length > WB_RELAY_MAX) return null;
    try {
      const value = JSON.parse(text);
      return value && typeof value === "object" && !Array.isArray(value) ? value : null;
    } catch {
      return null;
    }
  }

  function wbRequestId(id) {
    return typeof id === "string" && id.length > 0 && id.length <= 100;
  }

  // The request to pass on to SN Web Socket, rebuilt from only what WB_RELAYED allows, or null
  // if it asks for anything else (another function, another field, a value of the wrong type).
  function wbRelayRequest(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const name = raw.function;
    if (typeof name !== "string" || !Object.prototype.hasOwnProperty.call(WB_RELAYED, name) ||
        !wbRequestId(raw.requestId)) {
      return null;
    }
    const fields = WB_RELAYED[name];
    const request = { function: name, requestId: raw.requestId };
    for (const [key, value] of Object.entries(raw)) {
      if (key === "function" || key === "requestId") continue;
      const type = Object.prototype.hasOwnProperty.call(fields, key) ? fields[key] : null;
      if (!type || typeof value !== type) return null;
      if (type === "string" && value.length > WB_RELAY_TEXT) return null;
      if (type === "number" && !Number.isFinite(value)) return null;
      request[key] = value;
    }
    return request;
  }

  // Pass the waiting requests to SN Web Socket one at a time, each answer back to the add-on
  // (over the link the request came on: a newer link never gets an older link's answers).
  // Without an answer, the add-on hears how SN Web Socket is instead: out of reach, it stops
  // waiting for this one; otherwise it waits out its own time limit.
  async function wbRelayNext(link) {
    if (link.calling) return;
    link.calling = true;
    while (wbLink === link && link.queue.length) {
      const { request, ws } = link.queue.shift();
      const reply = await wbHelperCall(link, request);
      if (link.ws !== ws) continue;
      if (reply) wbLinkSend(link, { type: "reply", reply });
      else wbLinkReport(link);
    }
    link.calling = false;
  }

  // One request to SN Web Socket: its answer, or null when it couldn't be asked or didn't
  // answer in time.
  async function wbHelperCall(link, request) {
    const helper = link.helper;
    if (!(await wbHelperReady(link))) return null;
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
        if (helper.ws === ws) wbHelperDrop(link, true);
      }, WB_CALL_MS);
      helper.pending = { name: request.function, id: request.requestId, finish };
      try {
        ws.send(JSON.stringify(request));
      } catch {
        finish(null);
      }
    });
  }

  // Something from SN Web Socket: the answer to the request waiting for one (by its requestId,
  // or by function for SN Web Socket's own errors, which leave that out), else ignored (its
  // greeting, say). Its version is noted from whatever says it.
  function wbHelperMessage(link, text) {
    const reply = wbParseObject(text);
    if (!reply) return;
    const helper = link.helper;
    const version = reply.snWebSocketVersion;
    if (typeof version === "string" && version !== helper.version &&
        /^[\x20-\x7e]{1,40}$/.test(version)) {
      helper.version = version;
      wbLinkReport(link);
    }
    const pending = helper.pending;
    if (!pending) return;
    const ownError = !("requestId" in reply) && reply.function === pending.name &&
      reply.errorCode != null && reply.errorCode !== 0;
    if (reply.requestId !== pending.id && !ownError) return;
    delete reply.hotelInfo;   // SN Web Socket repeats the request, encoder key and all
    pending.finish(reply);
  }

  // Whether SN Web Socket is connected, connecting now (not at the next retry) if it isn't.
  async function wbHelperReady(link) {
    return !!link.helper.ws || wbHelperConnect(link);
  }

  // Connect to SN Web Socket: the address that worked last time first, then the other. Resolves
  // to whether it worked; a failure is tried again later, waiting longer each time, except a
  // question from the browser that nobody answered.
  function wbHelperConnect(link) {
    const helper = link.helper;
    if (helper.trying) return helper.trying;
    clearTimeout(helper.timer);
    helper.trying = (async () => {
      // "prompt" only means the browser would ask if it needs to (it says so where it never
      // does, too). One that's asking holds the connection until it's answered, while an SN Web
      // Socket that isn't running is refused at once (the browser only asks once something is
      // there): so a connection still waiting after a moment means it's asking.
      const askable = wbNetworkState(await wbNetworkPermission()) === "prompt";
      const asking = askable ? setTimeout(() => wbHelperSet(link, "asking"), WB_ASKING_AFTER_MS) : 0;
      let asked = false;
      try {
        for (const address of new Set([helper.url, ...WB_HELPER_URLS].filter(Boolean))) {
          const tried = Date.now();
          const ws = await wbHelperOpen(helper, address,
            askable ? WB_ASKING_OPEN_MS : WB_HELPER_OPEN_MS);
          if (wbLink !== link) {
            ws?.close();
            return false;
          }
          if (ws) {
            wbHelperAttach(link, ws, address);
            return true;
          }
          // Held, then refused: the browser asked and nobody answered, or the question was
          // closed. The other address would only ask again.
          if (askable && Date.now() - tried >= WB_ASKING_AFTER_MS) {
            asked = true;
            break;
          }
        }
      } finally {
        clearTimeout(asking);
      }
      helper.permission = wbNetworkState(await wbNetworkPermission());   // Block answers it too
      if (wbLink !== link) return false;
      if (helper.permission === "denied") {
        wbHelperSet(link, "blocked");
      } else if (asked && helper.permission !== "granted") {
        // Not asked again by itself: Chrome can stop asking a site for a while once its question
        // has been closed a few times. Try again asks again, and allowing it in the site
        // settings connects straight away.
        wbHelperSet(link, "unanswered");
        return false;
      } else if (asked) {
        helper.delay = WB_HELPER_RETRY_MS[0];   // allowed in the site settings meanwhile
        wbHelperSet(link, "connecting");
      } else {
        wbHelperSet(link, "unreachable");
      }
      wbHelperRetry(link);
      return false;
    })().finally(() => { helper.trying = null; });
    return helper.trying;
  }

  // Try again (on a note): connect to SN Web Socket now, starting over from the first wait.
  function wbHelperTryAgain() {
    const link = wbLink;
    if (!link || link.helper.ws || link.helper.trying) return;
    link.helper.delay = WB_HELPER_RETRY_MS[0];
    wbHelperSet(link, "connecting");
    wbHelperConnect(link);
  }

  // A WebSocket to SN Web Socket once it's open, or null if it can't be opened in time (or
  // helper.opening, the connection waiting meanwhile, is closed).
  function wbHelperOpen(helper, address, wait) {
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

  function wbHelperAttach(link, ws, address) {
    const helper = link.helper;
    const openedAt = Date.now();
    helper.ws = ws;
    helper.url = address;
    ws.addEventListener("message", (ev) => {
      if (helper.ws === ws) wbHelperMessage(link, ev.data);
    });
    ws.addEventListener("close", () => {
      if (helper.ws !== ws) return;   // closed on purpose
      helper.ws = null;
      helper.pending?.finish(null);
      if (wbLink !== link) return;
      // SN Web Socket closed or restarted: connect again straight away, unless it closed as soon
      // as it opened (something else on its port, say), which waits longer each time instead.
      wbHelperSet(link, "connecting");
      if (Date.now() - openedAt >= WB_LINK_STEADY_MS) {
        helper.delay = WB_HELPER_RETRY_MS[0];
        wbHelperConnect(link);
      } else {
        wbHelperRetry(link);
      }
    });
    wbHelperSet(link, "ready");
  }

  // Close the connection to SN Web Socket (the link closing, or a request it never answered),
  // and with `again`, connect afresh. A request still waiting gets no answer.
  function wbHelperDrop(link, again = false) {
    const helper = link.helper;
    const ws = helper.ws;
    helper.ws = null;
    try { ws?.close(); } catch { /* already closing */ }
    helper.pending?.finish(null);
    if (again && wbLink === link) {
      wbHelperSet(link, "connecting");
      wbHelperConnect(link);
    }
  }

  function wbHelperRetry(link) {
    const helper = link.helper;
    clearTimeout(helper.timer);
    const wait = helper.delay * (0.75 + Math.random() / 2);
    helper.delay = Math.min(helper.delay * 2, WB_HELPER_RETRY_MS[1]);
    helper.timer = setTimeout(() => {
      if (wbLink === link) wbHelperConnect(link);
    }, wait);
  }

  function wbHelperSet(link, state) {
    if (link.helper.state === state) return;
    link.helper.state = state;
    wbLinkReport(link);
    wbLinkDrawn();
  }

  // The browser's permission for this site to reach this computer's own apps, under each name it
  // knows (each one's state is "granted", "denied" or "prompt"; none in browsers without it).
  // Allowing it connects straight away.
  function wbNetworkPermission() {
    if (!wbPermission) {
      wbPermission = (async () => {
        const found = [];
        for (const name of WB_NETWORK_PERMISSIONS) {
          try {
            const status = await navigator.permissions.query({ name });
            status.addEventListener("change", wbNetworkChanged);
            found.push(status);
          } catch { /* not a permission this browser has */ }
        }
        return found;
      })();
    }
    return wbPermission;
  }

  // Allowed or blocked: connect now. Answering the browser's question opens the connection
  // waiting for it; changing the site settings instead leaves it waiting for the question, so
  // it's started over if it's still waiting a moment later.
  function wbNetworkChanged() {
    const link = wbLink;
    if (!link || link.helper.ws) return;
    const opening = link.helper.opening;
    if (!opening) {
      wbHelperConnect(link);
      return;
    }
    setTimeout(() => {
      if (link.helper.opening === opening) opening.close();
    }, WB_ANSWERED_MS);
  }

  // Those as one state: blocked under any name is blocked, allowed only when allowed under
  // every name, and null in browsers that have none of them.
  function wbNetworkState(statuses) {
    const states = statuses.map((status) => status.state);
    if (!states.length) return null;
    return states.includes("denied") ? "denied"
      : states.every((state) => state === "granted") ? "granted" : "prompt";
  }

  // This computer's encoder as this page sees it, in This computer (USB) mode (null otherwise):
  // the pill's class and words, and a note's title, words and button when something's wrong.
  function wbLinkView() {
    const link = wbLink;
    if (!link) return null;
    const helper = link.helper.state;
    if (link.state === "replaced") {
      return { cls: "", text: "Encoder in another window",
        title: "The encoder is connected in another window",
        note: "Wristbands is open in another window or tab on this computer, and the encoder " +
          "works there.",
        action: ["Use it here", wbLinkResume] };
    }
    if (helper === "blocked") {
      return { cls: "error", text: "Encoder blocked",
        title: "This browser is blocking the card encoder",
        note: "Click the icon at the left of the address bar and allow Apps on device for this " +
          "site (Local network access in older versions). The page connects by itself once " +
          "it's allowed.",
        action: ["Try again", wbHelperTryAgain] };
    }
    if (helper === "unanswered") {
      return { cls: "error", text: "Encoder not allowed",
        title: "Allow this site to reach the card encoder",
        note: "The browser asked whether this site may access other apps and services on this " +
          "device, which is how it reaches the card encoder. Click Try again, then choose Allow.",
        action: ["Try again", wbHelperTryAgain] };
    }
    if (helper === "asking") {
      return { cls: "", text: "Connecting encoder…",
        title: "Choose Allow when the browser asks",
        note: "The browser is asking whether this site may access other apps and services on " +
          "this device, which is how it reaches the card encoder: choose Allow. If the question " +
          "has gone, click the icon at the left of the address bar." };
    }
    if (helper === "unreachable") {
      // The browser only asks once SN Web Socket is running, and one blocking the site without
      // saying so refuses just as quickly: unless it's allowed, it may be either.
      const maybeBlocked = link.helper.permission !== "granted";
      return { cls: "error", text: "Encoder offline",
        title: "Can't reach SN Web Socket on this computer: is it running?",
        note: "Start TT Hotel's SN Web Socket on this computer (the one this browser is on), " +
          "with the card encoder plugged in. This page connects to it by itself once it's " +
          "running." + (maybeBlocked ? " The browser then asks whether this site may access " +
            "other apps and services on this device: choose Allow. If SN Web Socket is " +
            "running already, check that Apps on device isn't blocked for this site (the icon " +
            "at the left of the address bar)." : ""),
        action: ["Try again", wbHelperTryAgain] };
    }
    if (link.state === "refused") {
      return { cls: "error", text: "Encoder offline", title: "The card encoder can't be connected",
        note: link.reason || "Reload this page. If it keeps happening, ask your manager to check " +
          `this ${word()}'s card encoder setting.` };
    }
    if (link.state !== "open" || helper !== "ready") {
      return { cls: "", text: "Connecting encoder…" };
    }
    return { cls: "ok", text: "Encoder ready" };
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
        : type === "day_pass" ? (t.end_time_passed ? "Pick when they stop working" : `Works until ${t.expires_label}`)
        : type === "overnight" ? `Room wristbands until checkout at ${wbClock(t.checkout_time)}`
        : type === "contractor" ? `Doors you pick, for up to ${wbPlural(t.max_days || 7, "day")}`
        : "Key cards for staff";
      return wbChoice(type, WB_TYPES[type].title, sub, WB_TYPES[type].icon,
        t.allowed ? () => wbOpenForm(type) : null);
    });
    wbShow("home", wbHomeHead(),
      // In This computer (USB) mode the note under the header says it (wbDrawLinkNote).
      o.encoder?.error && !wbLocalEncoder(o) ? wbEncoderNote(o.encoder.error) : null,
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

  // ---- the forms: day pass, overnight, contractor badge, staff card

  function wbOpenForm(type) {
    const o = wb.options;
    const t = o?.types?.[type];
    if (!t?.allowed) return toast(t?.reason || "That can't be made right now.");
    const f = {
      type, t, name: "", company: "", count: 1, touched: new Set(), submitted: false, busy: false,
      doors: new Set(t.door_ids || []), rooms: new Set(),
      checkout: t.default_checkout || "", expiresOn: t.default_expires_on || "",
      // A day pass's end time for people who may change it; none to start with once the usual
      // time has passed, so they pick one.
      endTime: type === "day_pass" && t.can_edit_end_time ? (t.end_time_passed ? "" : t.end_time || "")
        : type === "contractor" ? t.default_end_time || "" : null,
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
    const contractor = type === "contractor";
    const guests = !staff && !contractor;
    const noun = WB_TYPES[type].noun;
    const name = h("input", { class: "field", type: "text", maxlength: "80", autocomplete: "off",
      autocapitalize: "words", spellcheck: "false", enterkeyhint: "go",
      placeholder: guests ? "Guest or party name" : "Their full name" });
    name.addEventListener("input", () => {
      f.name = name.value;
      wbChanged(f, "name", false);
    });
    const company = contractor ? h("input", { class: "field", type: "text", maxlength: "60", autocomplete: "off",
      autocapitalize: "words", spellcheck: "false", enterkeyhint: "go", placeholder: "Such as Acme Plumbing" }) : null;
    company?.addEventListener("input", () => {
      f.company = company.value;
      wbChanged(f, "company", false);
    });
    const people = [
      wbField(f, "name", staff ? "Staff member's name" : contractor ? "Contractor's name" : "Guest name", name),
      company ? wbField(f, "company", "Company (optional)", company) : null,
      wbField(f, "count", guests ? "Party size" : `How many ${noun}s`,
        wbStepper(f, guests ? "Party size" : `How many ${noun}s`, t.max || 1, guests ? "guest" : noun),
        guests ? h("p", { class: "wb-hint" }, "One wristband per guest.") : null),
    ];
    const sections = [h("section", { class: "wb-card" }, people)];
    if (type === "day_pass") {
      sections.push(h("section", { class: "wb-card" }, f.endTime !== null ? wbEndTimeField(f) : null,
        wbDoorsField(f, "Doors", "Every day pass opens these doors.")));
    } else if (type === "overnight") {
      const shared = new Set(t.door_ids || []);
      // Guest rooms, and doors not marked yet (a manager marks each door on the dashboard).
      const rooms = (o.doors || []).filter((d) => !shared.has(d.id) && (!d.kind || d.kind === "room"));
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
          h("p", { class: "wb-hint" }, "Pick every room in the booking that opens with a wristband. " +
            "Leave it empty for rooms with keys."))),
        h("section", { class: "wb-card" },
          wbField(f, "checkout", "Checkout date", checkout, f.els.checkoutHint),
          shared.size || t.can_edit_doors
            ? wbDoorsField(f, "Shared doors", "Every overnight wristband opens these too.") : null));
    } else if (contractor) {
      sections.push(
        h("section", { class: "wb-card" }, wbField(f, "door_ids", "Doors",
          wbDoorPicker(f.doors, "doors", o.doors || [], () => wbChanged(f, "door_ids")),
          h("p", { class: "wb-hint" }, "Every badge opens these doors. Only doors you have access to are listed."))),
        h("section", { class: "wb-card" }, wbEndField(f)));
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
  // them can, for this card only, from the public doors (and doors not marked yet). Doors
  // hidden from the lists, or marked as rooms since, stay on it either way. Staff doors never
  // do: the add-on leaves them out of t.door_ids.
  function wbDoorsField(f, label, hint) {
    const listed = f.doorById;
    const usual = new Set(f.t.door_ids || []);
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
          const shared = [...listed.values()].filter((d) => !d.kind || d.kind === "public" || usual.has(d.id));
          picker = wbDoorPicker(f.doors, "doors", shared, () => {
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

  // When a contractor badge stops working: a date and a time, later than now and at most
  // t.max_days days away (the server's limit; the hint says how far that is).
  function wbEndField(f) {
    const { t } = f;
    const today = wb.options.today;
    const day = h("input", { class: "field", type: "date", value: f.expiresOn, min: today,
      max: t.latest_on || wbAddDays(today, t.max_days || 7) });
    const at = h("input", { class: "field", type: "time", step: "60", value: f.endTime });
    f.els.expiresHint = h("p", { class: "wb-hint", "aria-live": "polite" });
    day.addEventListener("input", () => {
      f.expiresOn = day.value;
      wbDrawDates(f);
      wbChanged(f, "expires_on");
    });
    at.addEventListener("input", () => {
      f.endTime = at.value.slice(0, 5);
      wbDrawDates(f);
      wbChanged(f, "end_time");
    });
    return h("div", { class: "wb-field" }, h("div", { class: "wb-when" },
      wbField(f, "expires_on", "Works until", day), wbField(f, "end_time", "Time", at)), f.els.expiresHint);
  }

  // When a day pass stops working, for people who may change it: any time later than now,
  // today (up to 11:59 PM). It starts at the usual time, or empty once that has passed.
  function wbEndTimeField(f) {
    const input = h("input", { class: "field", type: "time", step: "60", value: f.endTime });
    f.els.endHint = h("p", { class: "wb-hint", "aria-live": "polite" });
    input.addEventListener("input", () => {
      f.endTime = input.value.slice(0, 5);
      wbDrawDates(f);
      wbChanged(f, "end_time");
    });
    return wbField(f, "end_time", "Works until", input, f.els.endHint);
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
    } else if (f.type === "day_pass" && f.endTime !== null) {
      const usual = wbClock(t.end_time);
      f.els.endHint.textContent = !f.endTime && t.end_time_passed
        ? `It's past ${usual}, when day passes usually stop working. Pick a later time, up to 11:59 PM.`
        : `Usually ${usual}. Any time later than now, up to 11:59 PM tonight.`;
    } else if (f.type === "contractor") {
      f.els.expiresHint.textContent = (f.expiresOn && f.endTime
        ? `Badges stop working at ${wbExpiryLabel(f.expiresOn, f.endTime)}.` : "Pick the day and time they stop working.") +
        ` At most ${wbPlural(t.max_days || 7, "day")} from now${t.latest_label ? `: until ${t.latest_label}` : ""}.`;
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
    if (f.type === "day_pass" && f.endTime) body.end_time = f.endTime;
    if (f.type === "staff") {
      Object.assign(body, { access: wbAccessEntries(f.access, f.doorById), expires_on: f.expiresOn });
    }
    if (f.type === "contractor") {
      Object.assign(body, { company: f.company.trim(), door_ids: [...f.doors], expires_on: f.expiresOn,
        end_time: f.endTime });
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
      : type === "day_pass" && f.endTime !== null ? (f.endTime ? wbExpiryLabel(wb.options.today, f.endTime) : "—")
      : type === "day_pass" ? t.expires_label
      : type === "overnight" ? (f.checkout ? wbExpiryLabel(f.checkout, t.checkout_time) : "—")
      : type === "contractor" ? (f.expiresOn && f.endTime ? wbExpiryLabel(f.expiresOn, f.endTime) : "—")
      : f.expiresOn ? wbExpiryLabel(f.expiresOn, WB_STAFF_UNTIL) : "—";
    const company = type === "contractor" ? f.company.trim() : "";
    fill(f.els.summary, [...wbRows([
      ["For", company ? `${f.name.trim() || "—"} (${company})` : f.name.trim() || "—"],
      [`${cap(WB_TYPES[type].noun)}s`, String(f.count)],
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
    const contractor = f.type === "contractor";
    // Quick checks first: they save a trip and point at the field. The server checks it all.
    const missing = !f.name.trim() ? ["name", staff ? "Enter the staff member's name."
      : contractor ? "Enter the contractor's name." : "Enter the guest's name."]
      : f.type === "day_pass" && f.endTime === "" ? ["end_time", "Pick when these wristbands stop working."]
      : f.type === "overnight" && !f.rooms.size && !f.doors.size ? ["room_ids", "Pick a room, or a shared door."]
      : f.type === "overnight" && !f.checkout ? ["checkout", "Pick a checkout date."]
      : staff && !wbAccessEntries(f.access, f.doorById).length ? ["access", "Choose what this card opens."]
      : staff && !f.expiresOn ? ["expires_on", "Pick the last day it works."]
      : contractor && !f.doors.size ? ["door_ids", "Pick at least one door."]
      : contractor && !f.expiresOn ? ["expires_on", "Pick the last day it works."]
      : contractor && !f.endTime ? ["end_time", "Pick the time it stops working."]
      : null;
    if (missing) {
      wbSetError(f, ...missing);
      wbFocusField(f, missing[0]);
      return;
    }
    f.busy = true;
    // Make's answer is the latest word on this form: a preview still to come (from a change made
    // just before) mustn't clear the problem it shows.
    clearTimeout(f.timer);
    f.ask += 1;
    f.els.summary.classList.remove("updating");
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
    const text = res.status === 0 ? `Can't reach ${portalName()}. Check this ${word()}'s connection, then try again.`
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
        `Can't reach ${portalName()} right now. Still trying…`),
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
    const write = (job.kind || "write") === "write";
    setText(v.eyebrow, `${wbJobTitle(job)} · ${job.name}`);
    setText(v.step, active && job.current && total > 1 ? `${Noun} ${job.current} of ${total}` : "");
    setText(v.prompt, job.message || "");
    if (job.phase !== v.phase) {
      v.phase = job.phase;
      const [icon, cls] = {
        waiting_for_card: [kind.icon === "moon" ? "band" : kind.icon, "pulse"], remove_card: ["up", "lift"],
        needs_attention: ["alert", "alert"],
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
    setText(v.meta, job.kind === "revoke" ? `Only card ${job.target_card} is cleared. Any other card is left as it is.`
      : !write ? `For card ${job.target_card}`
      : [job.expires_label && `Works until ${job.expires_label}`, job.summary].filter(Boolean).join(" · "));
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
      fill(v.results, write ? h("section", { class: "group" }, h("h2", null, `${Noun}s`),
        wbRows(job.cards.map((c) => [`${Noun} ${c.seq}`,
          c.status === "written" ? c.card_number || "Made" : WB_CARD_WORDS[c.status] || cap(c.status),
          c.status === "failed"])))
        : h("section", { class: "group" }, h("h2", null, "Result"), wbRows([wbOtherResult(job)])));
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
      toast(res.status === 0 ? `Can't reach ${portalName()}. Check this ${word()}'s connection.`
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
          message: `Can't reach ${portalName()}. Check this ${word()}'s connection, then try again.` };
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
    const doors = look.card || [];
    // What the card itself says comes first: the encoder just read it. Our record can be out of
    // date (a card cancelled in TT Hotel's own app, say).
    const last = doors.reduce((a, r) => (Number(r.expires_at) > Number(a?.expires_at || 0) ? r : a), null);
    const hero = (value, label, alert) => h("div", { class: alert ? "hero-value alert" : "hero-value" },
      h("div", { class: "hero-number" }, value), h("div", { class: "hero-label" }, label));
    // Our record of this very card: its state, and the lost card it also cancels if it was made
    // as a replacement.
    const mine = job && (job.kind || "write") === "write"
      ? job.cards.find((c) => c.status === "written" && wbSameCard(c.card_number, look.card_number)) : null;
    const state = mine ? wbCardState(job, mine) : null;
    const done = look.state === "done";
    const noun = job ? (WB_TYPES[job.type] || WB_TYPES.day_pass).noun : "card";
    // The server says who may wipe it (people who see every card, or whoever made it), and
    // checks again when asked.
    const revoke = done && look.can_revoke && look.card_number
      ? h("button", { type: "button", class: "btn wb-stop",
        onclick: (ev) => wbLookupRevoke(look.card_number, job?.name, ev.currentTarget) },
      "Revoke (wipe this card)") : null;
    fill(v.body,
      look.state === "error" ? wbNote("error", look.message || "That didn't work. Try again.") : null,
      done ? h("div", { class: "hero-values wb-hero" },
        hero(look.card_number || "—", "Card number"),
        look.empty ? hero("No doors", "On the card", true)
          : last?.expires_label ? hero(last.expires_label, "Works until") : null) : null,
      done && look.empty ? wbNote("error", look.blank ? "It's blank, or set up for another hotel."
        : "It opens nothing: it's been cleared, or cancelled in TT Hotel's own program or app.",
      `This ${noun} has no doors on it`) : null,
      done && look.message ? wbNote("", look.message) : null,
      doors.length ? h("section", { class: "group" },
        h("h2", null, `On the ${job ? noun : "wristband"}`),
        wbRows(doors.map((r) => [r.label, r.expires_label ? `Until ${r.expires_label}` : "—"]))) : null,
      done && (!job || job.type === "staff") ? wbNoticeSection(look.cancellations) : null,
      job ? h("section", { class: "group" }, h("h2", null, "Our record"), wbRows([
        ["Made for", job.name],
        ["Kind", `${kind.title} · ${wbPlural(job.count, kind.noun)}`],
        ["Made by", job.made_by],
        ["Made", wbWhen(job.created_at)],
        ["Opens", job.summary || null],
        ["Works until", job.expires_label || null],
        ["State", mine ? WB_STATE_WORDS[state] || "Unknown" : null, !!mine && state !== "active"],
        ["Revoked", mine && state === "revoked" && mine.state_by || null],
        ["Replaces lost card", mine?.cancelled_card || null],
        ["Last used", mine ? wbUsed(mine) : null, mine ? wbUsedAlert(mine) : false],
      ]), done && mine && !look.empty ? wbStateNote(state, doors.length > 0) : null,
      done && state === "active" ? h("p", { class: "muted wb-sub wb-record-note" },
        "Reported lost in the TT Hotel app? Staff Portal can't see that \u2014 use Report lost in Staff " +
        "Access so it's tracked.") : null) : null,
      h("div", { class: "wb-actions wb-actions-start" }, h("button", { type: "button", class: "btn primary",
        onclick: () => wbOpenLookup(true) }, look.state === "error" ? "Try again" : "Look up another"), revoke));
  }

  // What a revoked, lost or expired card on the encoder means for the person holding it.
  function wbStateNote(state, hasDoors) {
    if (state === "revoked") {
      return wbNote("error", hasDoors ? "Doors are still written on it, so keep it out of use."
        : "It's been cleared and can be used again.", "This card was revoked");
    }
    if (state === "lost") {
      return wbNote("error", "Keep it out of use and tell a manager it's been found.", "This card was reported lost");
    }
    if (state === "expired") return wbNote("", "Its last day has passed, so it no longer opens doors.", "Expired");
    if (!state) return wbNote("error", "Its record couldn't be read. Treat it as not working.", "Unknown state");
    return null;
  }

  // The lost card notice on a card read off the encoder (CE_ReadCancellationInfo): the card numbers
  // that locks refuse once it's tapped on them. null: the encoder didn't read it.
  function wbNoticeSection(cancellations) {
    if (!Array.isArray(cancellations)) return null;
    if (!cancellations.length) {
      return h("section", { class: "group" }, h("h2", null, "Lost card notice"),
        wbRows([["Lost card notice", "None on this card"]]));
    }
    return h("section", { class: "group" }, h("h2", null, "Lost card notice"),
      h("p", { class: "muted wb-sub" }, "Each lock this card is tapped on stops opening for these cards."),
      wbRows(cancellations.slice(0, 20).map((c) => [
        c?.card_number ? `Cancels card ${c.card_number}` : "Cancels a card the encoder couldn't read",
        Number(c?.expires_at) > 0 ? `Until ${wbWhen(c.expires_at)}` : "—", true])));
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
        ? "Every wristband and card made here, newest first. Open a staff card to revoke it (have the card in hand)."
        : "The wristbands and cards you've made, newest first."),
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
      log.error = res.status === 0 ? `Can't reach ${portalName()}. Check this ${word()}'s connection.`
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
    const write = (job.kind || "write") === "write";
    const made = job.cards.filter((c) => c.status === "written" || c.status === "cleared").length;
    // A staff card's state goes on the row when all its cards share one (usually there's one card).
    const states = wbKeyCard(job)
      ? [...new Set(job.cards.filter((c) => c.status === "written").map((c) => wbCardState(job, c)))] : [];
    const state = states.length === 1 ? states[0] : null;
    const [badge, tone] = job.status === "needs_attention" ? ["Needs attention", "alert"]
      : WB_ACTIVE.has(job.status) ? [!write ? (job.kind === "revoke" ? "Revoking…" : "Working…") : "Making…", "live"]
      : !write && !made ? [job.kind === "revoke" ? "Not revoked" : "Not made", "alert"]
      : made < job.count ? [made ? `${made} of ${job.count} made` : "Not made", "alert"]
      : states.length ? [state ? WB_STATE_WORDS[state] || "Unknown" : "Mixed", wbStateTone(state)]
      : [null, null];
    const sub = [write ? `${kind.title} · ${wbPlural(job.count, kind.noun)}` : wbJobTitle(job),
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
    if (!write) {
      return h("div", { class: "wb-log-item open" }, row, h("div", { class: "wb-log-detail" },
        wbRows([
          ["Card", job.target_card || null],
          ["Done by", job.made_by],
          ["When", wbWhen(job.created_at)],
          WB_ACTIVE.has(job.status) ? ["Result", "Not finished yet"] : wbOtherResult(job),
          ["What happened", wbWhy(job)],
        ], "wb-rows"),
        WB_ACTIVE.has(job.status)
          ? h("button", { type: "button", class: "btn primary", onclick: () => wbShowJob(job) }, "Show progress")
          : null));
    }
    const key = wbKeyCard(job);
    const cardValue = (c) => {
      if (c.status !== "written") return WB_CARD_WORDS[c.status] || cap(c.status);
      const number = c.card_number || "Made";
      return key ? `${number} · ${WB_STATE_WORDS[wbCardState(job, c)] || "Unknown"}` : number;
    };
    const cardAlert = (c) => c.status === "failed" || (key && c.status === "written"
      && !["active", "expired"].includes(wbCardState(job, c)));
    // People who may see every card can revoke an active staff card here, with it in hand.
    const revocable = WB_ACTIVE.has(job.status) ? [] : job.cards.filter((c) => wbCanRevoke(job, c));
    return h("div", { class: "wb-log-item open" }, row, h("div", { class: "wb-log-detail" },
      wbRows([
        ["Opens", job.summary || null],
        ["Works until", job.expires_label || null],
        ["Made by", job.made_by],
        ["Made", wbWhen(job.created_at)],
        ...job.cards.flatMap((c) => [[`${Noun} ${c.seq}`, cardValue(c), cardAlert(c)],
          [`${Noun} ${c.seq} replaces lost card`, c.status === "written" && c.cancelled_card || null],
          [job.cards.length > 1 ? `${Noun} ${c.seq} last used` : "Last used", wbUsed(c), wbUsedAlert(c)]]),
        ["What happened", wbWhy(job)],
      ], "wb-rows"),
      WB_ACTIVE.has(job.status)
        ? h("button", { type: "button", class: "btn primary", onclick: () => wbShowJob(job) }, "Show progress")
        : null,
      revocable.length ? h("div", { class: "wb-log-actions" }, revocable.map((c) =>
        h("button", { type: "button", class: "btn wb-stop", onclick: (ev) => wbRevoke(c, job.name, ev.currentTarget) },
          revocable.length > 1 || job.cards.length > 1 ? `Revoke card ${c.card_number}` : "Revoke this card")))
        : null));
  }

  // ---- staff cards: their state, and revoking one (card in hand)

  // "Staff card", or what a job that didn't make cards did: revoked a card, or made a lost
  // card notice for one.
  function wbJobTitle(job) {
    if (job.kind === "revoke") return `Revoke card ${job.target_card}`;
    if (job.kind === "notice") return `Lost card notice for card ${job.target_card}`;
    return (WB_TYPES[job.type] || WB_TYPES.day_pass).title;
  }

  // Why a finished job didn't do all it was asked: the encoder's last problem with a card, else
  // how the job ended ("Cancelled before any wristbands were made", say). Null when it did.
  function wbWhy(job) {
    if (WB_ACTIVE.has(job.status)) return null;
    const missed = job.cards.filter((c) => c.status !== "written" && c.status !== "cleared");
    if (!missed.length) return null;
    return [...missed].reverse().find((c) => c.error)?.error || job.message || null;
  }

  // How a finished revoke or lost card notice job went, as a [label, value, alert?] row.
  function wbOtherResult(job) {
    const done = job.cards.some((c) => c.status === "written" || c.status === "cleared");
    if (job.kind === "revoke") return ["Result", done ? "Revoked and cleared" : "Not revoked", !done];
    return ["Result", done ? "Lost card notice made" : "Not made", !done];
  }

  // Staff cards made (not a revoke or a lost card notice job, which made no key card).
  function wbKeyCard(job) {
    return job.type === "staff" && (job.kind || "write") === "write";
  }

  // Where and when a card was last used at a door, as TT Hotel reported it ("Pool Gate, today
  // 3:12 PM"), or null when it hasn't been seen. The server writes it in the property's time.
  function wbUsed(card) {
    const used = card?.last_used;
    if (!used) return null;
    return used.label || `${used.door}, ${wbWhen(used.at)}${used.ok ? "" : ", refused"}`;
  }

  const wbUsedAlert = (card) => !!card?.last_used && !card.last_used.ok;

  // Card numbers as the encoder and the record may spell them (leading zeros aside).
  function wbSameCard(a, b) {
    const norm = (n) => String(n ?? "").trim().replace(/^0+(?=\d)/, "");
    return a != null && b != null && norm(a) !== "" && norm(a) === norm(b);
  }

  // active, expired (active, past its last day), revoked or lost; null when the record is damaged.
  function wbCardState(job, card) {
    const state = card.state ?? "active";
    if (state === "active") {
      const ends = Number(job.expires_at);
      return Number.isFinite(ends) && ends > 0 && ends * 1000 <= Date.now() ? "expired" : "active";
    }
    return state === "revoked" || state === "lost" ? state : null;
  }

  function wbStateTone(state) {
    return state === "active" ? "ok" : state === "expired" ? "" : "alert";
  }

  // Only people who may see every card, only an active card (an expired one too: it's still
  // cleared for reuse), and only one this app can name. The server checks all of it again.
  function wbCanRevoke(job, card) {
    if (!wb.options?.see_all_cards || !wbKeyCard(job) || card.status !== "written") return false;
    const state = wbCardState(job, card);
    return (state === "active" || state === "expired") && Number.isInteger(card.id) && !!card.card_number;
  }

  // Revoke a staff card with it in hand: the encoder waits for that exact card, clears it, and
  // it's marked revoked. The encoding screen shows how it goes.
  async function wbRevoke(card, name, button) {
    const who = name ? ` (${name})` : "";
    return wbStartRevoke(`/api/wristbands/cards/${card.id}/revoke`, button,
      `Revoke card ${card.card_number}${who}?\n\nYou'll put the card on the encoder next. ` +
      "It's cleared so it opens nothing, and it can be given out again afterwards.");
  }

  // Wipe the card just looked up: it's still on the encoder. The server takes the card number
  // from the look-up, and checks this person may revoke it.
  async function wbLookupRevoke(number, name, button) {
    const who = name ? ` (${name})` : "";
    return wbStartRevoke("/api/wristbands/lookup/revoke", button,
      `Revoke card ${number}${who}?\n\nLeave it on the encoder. It's wiped so it opens no doors, ` +
      "and it can be given out again afterwards.");
  }

  async function wbStartRevoke(path, button, question) {
    const state = wb;
    if (!state || state.acting) return;
    if (!window.confirm(question)) return;
    state.acting = true;
    button?.classList.add("working");
    button?.setAttribute("aria-busy", "true");
    const res = await api("POST", path);
    if (wb !== state) return;
    state.acting = false;
    button?.classList.remove("working");
    button?.removeAttribute("aria-busy");
    if (res.status === 200 && res.data.job) return wbShowJob(res.data.job);
    if (wbAuthError(res)) return;
    toast(res.status === 0 ? `Can't reach ${portalName()}. Check this ${word()}'s connection, then try again.`
      : res.data.error || "That card couldn't be revoked. Try again.", true);
    if (res.status === 409 && state.screen === "log") wbLoadLog(false);   // its state may have changed
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

  // ---------------------------------------------------------------- tasks

  // Each department's to-do list. Everyone signed in with their own PIN has it: tasks for each of
  // their departments, for Everyone, and given to them. One tap on a status chip moves a task on; a long
  // press (or right click) on it offers every status. Phones get a list grouped by status, wider
  // screens a board. The server checks every request; what's offered here is only a guide.
  const TK_STATUS = {
    todo: { name: "To do", short: "To do" },
    doing: { name: "In progress", short: "Doing" },
    done: { name: "Done", short: "Done" },
    wont: { name: "Won't do", short: "Won't do" },
  };
  const TK_NEXT = { todo: "doing", doing: "done", done: "todo", wont: "todo" };
  const TK_COLUMNS = [["todo", "To do"], ["doing", "In progress"], ["done", "Done"]];
  const TK_FOLD_MS = 24 * 3600 * 1000;   // finished tasks fold away after a day
  const TK_HOLD_MS = 450;                // a press this long on a chip opens its menu
  const TK_FILTER_KEY = "tk_filter";
  // Line icons (24px grid, drawn with currentColor).
  const TK_ICONS = {
    todo: '<circle cx="12" cy="12" r="8"/>',
    doing: '<circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor" stroke="none"/>',
    done: '<circle cx="12" cy="12" r="8" fill="currentColor" stroke="none"/><path d="M8.2 12.3l2.6 2.6 5-5.2" stroke="var(--tk-check)" stroke-width="2.2"/>',
    wont: '<circle cx="12" cy="12" r="8"/><path d="M6.6 17.4L17.4 6.6"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    flag: '<path d="M6 21V4.5M6 4.5h10.5l-2 4 2 4H6"/>',
    clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    person: '<circle cx="12" cy="8.5" r="3.5"/><path d="M5 20c.9-3.6 3.6-5.5 7-5.5s6.1 1.9 7 5.5"/>',
    form: '<rect x="5" y="3.5" width="14" height="17" rx="2.5"/><path d="M9 8.5h6M9 12h6M9 15.5h3.5"/>',
    team: '<circle cx="9" cy="9" r="3"/><circle cx="16.5" cy="10" r="2.5"/><path d="M3.5 19c.7-3 2.8-4.6 5.5-4.6s4.8 1.6 5.5 4.6M15 14.6c2.6-.3 4.6 1.2 5.3 4.4"/>',
    bin: '<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13"/>',
    close: '<path d="M6 6l12 12M18 6L6 18"/>',
    chevron: '<path d="M9 6l6 6-6 6"/>',
    link: '<path d="M14 4.5h5.5V10M19.5 4.5L11 13M17 14v4.5a1 1 0 0 1-1 1H5.5a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1H10"/>',
    sync: '<path d="M19 9a7.5 7.5 0 0 0-13.5-2.5M5 15a7.5 7.5 0 0 0 13.5 2.5"/><path d="M5 3.5v3.5h3.5M19 20.5V17h-3.5"/>',
    photo: '<rect x="3.5" y="5.5" width="17" height="13" rx="2.5"/><circle cx="9" cy="10.5" r="1.8"/><path d="M20.5 16l-5-5-8 7.5"/>',
  };

  function tkIcon(name, cls = "tk-icon") {
    const span = h("span", { class: cls, "aria-hidden": "true" });
    span.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ` +
      `stroke-linecap="round" stroke-linejoin="round">${TK_ICONS[name]}</svg>`;
    return span;
  }

  // The Tasks screen's state, from when it's first opened. Forgotten at sign-out (leaveDashboard).
  let tk = null;

  function tkState() {
    if (!tk) {
      let saved = {};
      try { saved = JSON.parse(session.get(TK_FILTER_KEY) || "{}"); } catch { /* damaged */ }
      tk = {
        root: h("div", { class: "tk" }),
        data: null, v: null, loading: false, seq: 0, minute: 0, people: null,
        filter: { mine: !!saved.mine, dept: saved.dept ?? "all", status: saved.status || "all" },
        showOlder: new Set(), sheet: null, pending: new Map(),
      };
      fill(tk.root, h("div", { class: "wb-loading" }, h("div", { class: "spinner", "aria-label": "Loading" })));
      tkLoad();
    }
    return tk;
  }

  function tkReset() {
    if (!tk) return;
    tkCloseSheet(true);
    tk = null;
  }

  // The server says this person has no tasks here: back to the pages, saying why.
  function tkNoAccess(text) {
    toast(text || "Tasks aren't available here.", true);
    tkReset();
    tkPaused = Date.now() + TOOL_PAUSE_MS;
    view.tool = null;
    session.del("tool");
    rerender();
  }
  let tkPaused = 0;

  // A failed request: sign-in problems go to their own screens; anything else is said in a toast.
  // Returns true when the screen is gone.
  function tkFailed(res, fallback) {
    if (res.status !== 0 && handleAuthError(res)) return true;
    if (res.status === 403 && !res.data.task && /sign in/i.test(res.data.error || "")) {
      tkNoAccess(res.data.error);
      return true;
    }
    toast(res.status === 0 ? "Can't connect. Check the connection and try again." : res.data.error || fallback, true);
    return false;
  }

  // With each dashboard poll while Tasks is on screen.
  function tkPoll() {
    if (tk && !tk.loading) tkLoad(true);
  }

  async function tkLoad(quiet) {
    const state = tk;
    state.loading = true;
    const res = await api("GET", "/api/tasks" + (state.v && quiet ? `?v=${encodeURIComponent(state.v)}` : ""));
    if (tk !== state) return;
    state.loading = false;
    if (res.status !== 200) {
      if (quiet && res.status === 0) return;   // the dashboard's own poll reports a lost connection
      tkFailed(res, "Tasks couldn't be loaded.");
      if (!state.data && tk === state) fill(state.root, tkEmpty("Tasks couldn't be loaded", "Try again in a moment."));
      return;
    }
    const minute = Math.floor(Date.now() / 60000);
    if (res.data.unchanged && minute === state.minute) return;
    if (!res.data.unchanged) {
      state.data = res.data;
      state.v = res.data.v;
    }
    state.minute = minute;
    tkDraw();
  }

  // ---- what's shown

  function tkSaveFilter() {
    session.set(TK_FILTER_KEY, JSON.stringify(tk.filter));
  }

  // The tasks the filters let through, with changes still on their way filled in.
  function tkShown() {
    const f = tk.filter;
    return tk.data.tasks.map((t) => (tk.pending.has(t.id) ? { ...t, status: tk.pending.get(t.id) } : t))
      .filter((t) => (!f.mine || t.mine) &&
        (f.dept === "all" || (f.dept === "everyone" ? t.department_id == null : String(t.department_id) === String(f.dept))) &&
        (f.status === "all" || t.status === f.status));
  }

  const tkFinished = (t) => t.status === "done" || t.status === "wont";

  // When a task is due, as a time here (a day without a time: the end of that day), or null.
  function tkDueAt(t) {
    if (!t.due_date) return null;
    const [y, m, d] = t.due_date.split("-").map(Number);
    const [hh, mm] = (t.due_time || "23:59").split(":").map(Number);
    return new Date(y, m - 1, d, hh, mm, t.due_time ? 0 : 59);
  }

  const tkOverdue = (t) => !tkFinished(t) && !!t.due_date && tkDueAt(t) < new Date();

  function tkClock(hhmm) {
    const [hh, mm] = hhmm.split(":").map(Number);
    return new Date(2000, 0, 1, hh, mm).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function tkDueText(t) {
    const at = tkDueAt(t);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const day = new Date(at);
    day.setHours(0, 0, 0, 0);
    const diff = Math.round((day - today) / 86400000);
    const name = diff === 0 ? "Today" : diff === 1 ? "Tomorrow" : diff === -1 ? "Yesterday"
      : at.toLocaleDateString([], { weekday: diff > 1 && diff < 7 ? "long" : "short", month: "short", day: "numeric",
        year: at.getFullYear() === today.getFullYear() ? undefined : "numeric" })
        .replace(/^(\w+), (\w+ \d+)$/, (all, wd, md) => (diff > 1 && diff < 7 ? wd : all));
    return t.due_time ? `${name}, ${tkClock(t.due_time)}` : name;
  }

  function tkWhen(seconds) {
    const at = new Date(seconds * 1000);
    const same = at.toDateString() === new Date().toDateString();
    const time = at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    return same ? `Today ${time}` : `${at.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`;
  }

  const tkInitial = (name) => (name || "?").trim().charAt(0).toUpperCase() || "?";

  function tkEmpty(title, text) {
    return h("div", { class: "tk-empty" }, tkIcon("done", "tk-empty-icon"), h("strong", null, title), h("span", null, text));
  }

  // Redraw the screen from tk.data (the open sheet stays as it is).
  function tkDraw() {
    if (!tk?.data) return;
    const data = tk.data;
    const shown = tkShown();
    const open = data.tasks.filter((t) => !tkFinished(t));
    const urgent = open.filter((t) => t.priority === "urgent").length;
    const overdue = open.filter(tkOverdue).length;
    const mine = open.filter((t) => t.mine).length;
    const stats = [
      [String(open.length), "open", ""],
      urgent ? [String(urgent), "urgent", "urgent"] : null,
      overdue ? [String(overdue), "overdue", "overdue"] : null,
      mine ? [String(mine), "for you", "mine"] : null,
    ].filter(Boolean);
    const wide = isDesk();
    const head = h("div", { class: "tk-head" },
      wide ? null : h("h1", { class: "page-title tk-title" }, "Tasks"),
      h("div", { class: "tk-stats" }, stats.map(([n, label, cls]) =>
        h("span", { class: `tk-stat ${cls}` }, h("b", null, n), ` ${label}`))));
    const board = h("div", { class: "tk-board" }, TK_COLUMNS.map(([status, name]) => tkColumn(status, name, shown)));
    const nothing = !data.tasks.length
      ? tkEmpty("Nothing to do yet", "Tap + Quick task to add the first one.")
      : !shown.length ? tkEmpty("No tasks match", "Try another filter.") : null;
    fill(tk.root, head, tkFilters(), nothing || board,
      data.bin ? h("button", { class: "tk-bin-link", type: "button", onclick: tkOpenBin },
        tkIcon("bin"), `Bin (${data.bin})`) : null,
      wide ? null : h("button", { class: "tk-fab", type: "button", onclick: () => tkOpenForm(null) },
        tkIcon("plus"), "Quick task"));
  }

  // Mine, department and status.
  function tkFilters() {
    const f = tk.filter;
    const depts = tk.data.departments;
    const used = new Set(tk.data.tasks.map((t) => t.department_id));
    const deptSelect = h("select", { class: "tk-filter-select", "aria-label": "Department" },
      h("option", { value: "all" }, "Department"),
      h("option", { value: "everyone" }, "Everyone"),
      depts.filter((d) => used.has(d.id) || (tk.data.me.department_ids || []).includes(d.id))
        .map((d) => h("option", { value: String(d.id) }, d.name)));
    deptSelect.value = [...deptSelect.options].some((o) => o.value === String(f.dept)) ? String(f.dept) : "all";
    deptSelect.addEventListener("change", () => { f.dept = deptSelect.value; tkSaveFilter(); tkDraw(); });
    const statusSelect = h("select", { class: "tk-filter-select", "aria-label": "Status" },
      h("option", { value: "all" }, "Status"),
      Object.entries(TK_STATUS).map(([k, s]) => h("option", { value: k }, s.name)));
    statusSelect.value = f.status;
    statusSelect.addEventListener("change", () => { f.status = statusSelect.value; tkSaveFilter(); tkDraw(); });
    return h("div", { class: "tk-filters", role: "group", "aria-label": "Filters" },
      h("button", { class: f.mine ? "tk-filter on" : "tk-filter", type: "button", "aria-pressed": f.mine ? "true" : "false",
        onclick: () => { f.mine = !f.mine; tkSaveFilter(); tkDraw(); } }, tkIcon("person"), "Mine"),
      h("label", { class: f.dept !== "all" ? "tk-filter on" : "tk-filter" }, tkIcon("team"), deptSelect),
      h("label", { class: f.status !== "all" ? "tk-filter on" : "tk-filter" }, tkIcon("todo"), statusSelect),
      isDesk() ? h("button", { class: "tk-add-wide", type: "button", onclick: () => tkOpenForm(null) },
        tkIcon("plus"), "Quick task") : null);
  }

  // One status's group of tasks: a section of the list on a phone, a column on the board.
  // Done holds Won't do too; finished tasks older than a day fold away.
  function tkColumn(status, name, shown) {
    const items = shown.filter((t) => (status === "done" ? tkFinished(t) : t.status === status));
    if (status === "done") items.sort((a, b) => b.status_at - a.status_at);
    const cutoff = Date.now() - TK_FOLD_MS;
    const recent = status === "done" ? items.filter((t) => t.status_at * 1000 >= cutoff) : items;
    const older = items.length - recent.length;
    const showAll = tk.showOlder.has(status);
    const list = showAll ? items : recent;
    return h("section", { class: `tk-col tk-col-${status}${items.length ? "" : " is-empty"}`, "aria-label": name },
      h("h2", { class: "tk-col-head" }, h("span", { class: `tk-dot ${status}`, "aria-hidden": "true" }),
        h("span", { class: "tk-col-name" }, name), h("span", { class: "tk-count" }, String(items.length))),
      h("div", { class: "tk-cards" },
        list.length ? list.map(tkCard)
          : h("p", { class: "tk-col-empty" }, status === "done" && older ? "Nothing finished today." : "Nothing here."),
        older ? h("button", { class: "tk-older", type: "button", onclick: () => {
          if (showAll) tk.showOlder.delete(status); else tk.showOlder.add(status);
          tkDraw();
        } }, showAll ? "Hide older" : `Show ${older} older`) : null));
  }

  function tkCard(t) {
    const overdue = tkOverdue(t);
    const meta = [
      t.priority === "urgent" ? h("span", { class: "tk-tag urgent" }, tkIcon("flag"), "Urgent") : null,
      t.due_date ? h("span", { class: overdue ? "tk-tag overdue" : "tk-tag" }, tkIcon("clock"),
        overdue ? `Overdue · ${tkDueText(t)}` : tkDueText(t)) : null,
      h("span", { class: "tk-tag" }, tkIcon("team"), t.department),
      t.assigned_name ? h("span", { class: t.mine ? "tk-tag tk-who mine" : "tk-tag tk-who" },
        h("span", { class: "tk-avatar", "aria-hidden": "true" }, tkInitial(t.assigned_name)),
        t.mine ? "You" : t.assigned_name) : null,
      t.source === "form" ? h("span", { class: "tk-tag" }, tkIcon("form"), t.form_name || "Form") : null,
      t.photos?.length ? h("span", { class: "tk-tag" }, tkIcon("photo"), String(t.photos.length)) : null,
      t.connecteam ? h("span", { class: "tk-tag tk-ct", title: "In Connecteam" }, tkIcon("sync"), "Connecteam") : null,
    ];
    const card = h("article", { class: `tk-card ${t.status}${t.priority === "urgent" ? " is-urgent" : ""}${overdue ? " is-overdue" : ""}`,
      "data-id": String(t.id) },
      h("button", { class: "tk-card-main", type: "button", onclick: () => tkOpenTask(t.id) },
        h("span", { class: "tk-card-title" }, t.title),
        t.notes ? h("span", { class: "tk-card-notes" }, t.notes.split("\n").find((l) => l.trim()) || "") : null,
        h("span", { class: "tk-meta" }, meta)),
      tkChip(t));
    return card;
  }

  // The status chip: a tap moves the task on, a long press (or right click) offers every status.
  function tkChip(t) {
    const s = TK_STATUS[t.status] || TK_STATUS.todo;
    const chip = h("button", { class: `tk-chip ${t.status}`, type: "button",
      "aria-label": `${s.name}. Tap for ${TK_STATUS[TK_NEXT[t.status]].name}; hold for more.`,
      title: `${s.name} · tap for ${TK_STATUS[TK_NEXT[t.status]].name}, hold for more` },
    tkIcon(t.status, "tk-chip-icon"), h("span", { class: "tk-chip-text" }, s.short));
    let timer = null;
    let held = false;
    chip.addEventListener("pointerdown", (ev) => {
      if (ev.button > 0) return;
      held = false;
      clearTimeout(timer);
      timer = setTimeout(() => {
        held = true;
        if (navigator.vibrate) navigator.vibrate(10);
        tkStatusMenu(t);
      }, TK_HOLD_MS);
    });
    for (const type of ["pointerup", "pointerleave", "pointercancel"]) {
      chip.addEventListener(type, () => clearTimeout(timer));
    }
    chip.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      clearTimeout(timer);
      held = true;
      tkStatusMenu(t);
    });
    chip.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (held) { held = false; return; }
      if (t.status === "wont") return tkStatusMenu(t);
      tkSetStatus(t, TK_NEXT[t.status]);
    });
    return chip;
  }

  // Change a task's status: shown straight away, saved at once. If someone else changed it
  // meanwhile, theirs stays and the screen catches up.
  async function tkSetStatus(t, status) {
    if (!tk || status === t.status) return;
    const state = tk;
    state.pending.set(t.id, status);
    tkDraw();
    tkRedrawSheet(t.id);
    const res = await api("POST", `/api/tasks/${t.id}/status`, { status, from: t.status });
    if (tk !== state) return;
    state.pending.delete(t.id);
    if (res.status === 200) {
      tkPut(res.data.task);
      if (status === "done") toast(`Done: ${t.title}`);
    } else {
      if (res.data.task) tkPut(res.data.task);
      if (tkFailed(res, "Couldn't change the status.")) return;
      tkDraw();
    }
    tkRedrawSheet(t.id);
    state.v = null;   // the next poll brings the whole list again
  }

  // Put a task the server sent back into the list.
  function tkPut(task) {
    if (!tk?.data) return;
    const list = tk.data.tasks;
    const at = list.findIndex((x) => x.id === task.id);
    if (at >= 0) list[at] = task; else list.unshift(task);
    tkDraw();
  }

  // ---- sheets: a task, the quick task form, the status menu and the bin

  function tkSheet(kind, id, title, body, { wide = false, label, big = false } = {}) {
    tkCloseSheet(true);
    const close = h("button", { class: "tk-sheet-close", type: "button", "aria-label": "Close", onclick: () => tkCloseSheet() },
      tkIcon("close"));
    const panel = h("div", { class: wide ? "tk-sheet wide" : "tk-sheet", role: "dialog", "aria-modal": "true",
      "aria-label": label || title || "Task" },
    h("div", { class: "tk-grabber", "aria-hidden": "true" }),
    h("div", { class: "tk-sheet-head" }, title ? h("h2", { class: big ? "tk-detail-title" : null }, title) : h("span"), close),
    h("div", { class: "tk-sheet-body" }, body));
    const scrim = h("div", { class: "tk-scrim", onclick: (ev) => { if (ev.target === scrim) tkCloseSheet(); } }, panel);
    scrim.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); tkCloseSheet(); }
    });
    document.body.append(scrim);
    document.body.classList.add("tk-sheet-open");
    tk.sheet = { kind, id, el: scrim, panel, returnTo: document.activeElement };
    // Back closes it: an entry of its own (one sheet replacing another reuses it).
    if (history.state?.tk) history.replaceState({ tk: kind }, "", "/");
    else history.pushState({ tk: kind }, "", "/");
    requestAnimationFrame(() => {
      (panel.querySelector("[data-autofocus]") || close).focus({ preventScroll: true });
    });
    return panel;
  }

  // fromBack: Back was pressed (its history entry is gone already), or the screen is going.
  function tkCloseSheet(fromBack) {
    const sheet = tk?.sheet;
    if (!sheet) return;
    tk.sheet = null;
    sheet.el.remove();
    document.body.classList.remove("tk-sheet-open");
    if (!fromBack && history.state?.tk) history.back();
    if (sheet.returnTo?.isConnected) sheet.returnTo.focus({ preventScroll: true });
  }

  function tkStatusMenu(t) {
    const current = tk.pending.get(t.id) || t.status;
    const choice = (status, label) => h("button", {
      class: `tk-menu-item ${status}${status === current ? " current" : ""}`, type: "button",
      disabled: status === current,
      onclick: () => { tkCloseSheet(); tkSetStatus({ ...t, status: current }, status); },
    }, tkIcon(status, "tk-chip-icon"), h("span", null, label), status === current ? h("small", null, "Now") : null);
    const finished = current === "done" || current === "wont";
    tkSheet("menu", t.id, t.title, [
      h("div", { class: "tk-menu" },
        finished ? choice("todo", "Reopen") : choice("todo", "To do"),
        choice("doing", "In progress"), choice("done", "Done"), choice("wont", "Won't do")),
    ], { label: `Status of ${t.title}` });
  }

  async function tkOpenTask(id) {
    const state = tk;
    const res = await api("GET", `/api/tasks/${id}`);
    if (tk !== state) return;
    if (res.status !== 200) {
      if (!tkFailed(res, "That task couldn't be opened.") && res.status === 404) tkLoad();
      return;
    }
    tkShowTask(res.data.task, res.data.history);
  }

  // The open task's sheet again, after its status changed (it keeps its history).
  function tkRedrawSheet(id) {
    if (tk?.sheet?.kind !== "task" || tk.sheet.id !== id) return;
    const task = tk.data.tasks.find((x) => x.id === id);
    if (task) tkShowTask(task, tk.sheet.history, true);
  }

  function tkShowTask(t, history, again) {
    const status = tk.pending.get(t.id) || t.status;
    const overdue = tkOverdue({ ...t, status });
    const row = (icon, label, value, cls) => h("div", { class: `tk-fact ${cls || ""}` }, tkIcon(icon),
      h("span", { class: "tk-fact-label" }, label), h("span", { class: "tk-fact-value" }, value));
    const statusRow = h("div", { class: "tk-status-row", role: "group", "aria-label": "Status" },
      Object.entries(TK_STATUS).map(([k, s]) => h("button", {
        class: `tk-status-btn ${k}${k === status ? " on" : ""}`, type: "button", "aria-pressed": k === status ? "true" : "false",
        onclick: () => tkSetStatus({ ...t, status }, k) }, tkIcon(k, "tk-chip-icon"), s.name)));
    const facts = h("div", { class: "tk-facts" },
      row("team", "For", t.department),
      row("person", "Given to", t.assigned_name ? (t.mine ? `${t.assigned_name} (you)` : t.assigned_name) : "Anyone"),
      t.due_date ? row("clock", "Due", overdue ? `${tkDueText(t)} · overdue` : tkDueText(t), overdue ? "overdue" : "") : null,
      t.priority === "urgent" ? row("flag", "Priority", "Urgent", "urgent") : null,
      row(t.source === "form" ? "form" : t.source === "connecteam" ? "sync" : "plus", "Made", t.source === "form"
        ? `From the ${t.form_name || "Connecteam"} form · ${tkWhen(t.created_at)}`
        : t.source === "connecteam" ? `In Connecteam · ${tkWhen(t.created_at)}`
          : `By ${t.made_by_me ? "you" : t.created_by || "someone"} · ${tkWhen(t.created_at)}`),
      t.connecteam ? row("sync", "Connecteam", "Changes go both ways") : null,
      t.ct_note ? h("p", { class: "tk-ct-note" }, t.ct_note) : null);
    const link = t.source_url ? h("a", { class: "tk-source-link", href: t.source_url, target: "_blank", rel: "noopener noreferrer" },
      tkIcon("link"), "Open in Connecteam") : null;
    const notes = t.notes ? h("div", { class: "tk-notes" }, t.notes) : null;
    const managerNote = t.manager_note ? h("div", { class: "tk-notes tk-manager-note" },
      h("span", { class: "tk-fact-label" }, "Note in Connecteam"), t.manager_note) : null;
    const photos = t.photos?.length ? h("div", { class: "tk-photos" }, t.photos.map((name, i) => tkPhoto(t.id, i, name))) : null;
    const trail = (history || []).slice().reverse().map((e) => h("li", null,
      h("span", { class: `tk-dot ${e.to_status || "todo"}`, "aria-hidden": "true" }),
      h("span", { class: "tk-trail-text" }, tkHistoryText(e)),
      h("time", null, tkWhen(e.at))));
    const actions = t.can_edit ? h("div", { class: "tk-sheet-actions" },
      h("button", { class: "btn tk-danger", type: "button", onclick: () => tkDelete(t) }, "Delete"),
      h("button", { class: "btn primary", type: "button", onclick: () => tkOpenForm(t) }, "Edit")) : null;
    const body = [statusRow, facts, link, notes, managerNote, photos,
      trail.length ? h("details", { class: "tk-trail", open: again ? tk.sheet?.panel.querySelector(".tk-trail")?.open : null },
        h("summary", null, `History (${trail.length})`), h("ol", null, trail)) : null,
      actions];
    if (again && tk.sheet?.kind === "task") {
      fill(tk.sheet.panel.querySelector(".tk-sheet-body"), body);
    } else {
      tkSheet("task", t.id, t.title, body, { label: t.title, big: true });
    }
    tk.sheet.history = history;
  }

  // A photo on a task: fetched with the sign-in like any request (so not as a plain <img src>),
  // shown from a data: address (the page's CSP allows only those and its own); a tap opens it big.
  function tkPhoto(taskId, index, name) {
    const label = name || `Photo ${index + 1}`;
    const box = h("button", { class: "tk-photo", type: "button", "aria-label": label, disabled: true },
      tkIcon("photo"));
    tkPhotoData(taskId, index).then((data) => {
      if (!data) return void box.replaceChildren(h("span", { class: "tk-photo-none" }, "Photo unavailable"));
      box.replaceChildren(h("img", { src: data, alt: label }));
      box.disabled = false;
      box.addEventListener("click", () => tkPhotoBig(data, label));
    });
    return box;
  }

  async function tkPhotoData(taskId, index) {
    const headers = {};
    const token = local.get(TOKEN_KEY);
    if (token) headers.Authorization = "Bearer " + token;
    const pinSession = session.get(SESSION_KEY);
    if (pinSession) headers["X-Session"] = pinSession;
    try {
      const res = await fetch(`/api/tasks/${taskId}/photos/${index}`, { headers });
      const blob = res.ok ? await res.blob() : null;
      if (!blob || blob.type !== "image/jpeg") return null;
      return await new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).startsWith("data:image/jpeg") ? reader.result : null);
        reader.onerror = () => resolve(null);
        reader.readAsDataURL(blob);
      });
    } catch {
      return null;
    }
  }

  function tkPhotoBig(data, label) {
    const close = () => { view.remove(); window.removeEventListener("keydown", onKey, true); };
    const onKey = (ev) => { if (ev.key === "Escape") { ev.stopPropagation(); close(); } };
    const view = h("div", { class: "tk-photo-big", role: "dialog", "aria-label": label, onclick: close },
      h("img", { src: data, alt: label }),
      h("button", { class: "tk-photo-close", type: "button", "aria-label": "Close" }, tkIcon("close")));
    window.addEventListener("keydown", onKey, true);
    document.body.append(view);
    view.querySelector("button").focus();
  }

  function tkHistoryText(e) {
    if (e.note?.startsWith("kept")) return `Staff Access kept its change (${e.note.slice(6)})`;
    const why = e.note && e.note !== "in Connecteam" ? ` (${e.note.replace(/^in Connecteam, /, "")})` : "";
    return tkHistoryWhat(e) + why;
  }

  function tkHistoryWhat(e) {
    const who = e.by_name || "Someone";
    if (e.action === "created") return `${who} added it`;
    if (e.action === "edited") return `${who} changed it`;
    if (e.action === "deleted") return `${who} deleted it`;
    if (e.action === "restored") return `${who} put it back`;
    const to = TK_STATUS[e.to_status]?.name || "?";
    return e.from_status && (e.from_status === "done" || e.from_status === "wont") && e.to_status === "todo"
      ? `${who} reopened it` : `${who}: ${TK_STATUS[e.from_status]?.name || "?"} → ${to}`;
  }

  async function tkDelete(t) {
    if (!window.confirm(`Delete “${t.title}”? It stays in the bin for 30 days.`)) return;
    const state = tk;
    const res = await api("DELETE", `/api/tasks/${t.id}`);
    if (tk !== state) return;
    if (res.status !== 200) return void tkFailed(res, "Couldn't delete the task.");
    tkCloseSheet();
    toast("Deleted. It's in the bin for 30 days.");
    state.v = null;
    tkLoad();
  }

  async function tkPeople() {
    if (tk.people) return tk.people;
    const res = await api("GET", "/api/tasks/people");
    if (res.status === 200 && tk) tk.people = res.data.people;
    return tk?.people || [];
  }

  // The quick task form, or editing a task (t). A title is all it needs; the rest is optional.
  async function tkOpenForm(t) {
    const state = tk;
    if (!state?.data) return;
    const people = await tkPeople();
    if (tk !== state) return;
    const me = state.data.me;
    const f = t ? { title: t.title, notes: t.notes, dept: t.department_id, who: t.assigned_id,
      date: t.due_date || "", time: t.due_time || "", urgent: t.priority === "urgent" }
      : { title: "", notes: "", dept: me.department_ids?.[0] ?? null, who: null, date: "", time: "", urgent: false };
    const title = h("input", { class: "field tk-title-input", type: "text", maxlength: "120", value: f.title,
      placeholder: "What needs doing?", "aria-label": "Title", enterkeyhint: "done", autocomplete: "off", "data-autofocus": "" });
    const dept = h("select", { class: "field", "aria-label": "Department" },
      h("option", { value: "" }, "Everyone"),
      state.data.departments.map((d) => h("option", { value: String(d.id) }, d.name)),
      // A task's own department, hidden or gone since, stays as it is unless changed.
      t && t.department_id != null && !state.data.departments.some((d) => d.id === t.department_id)
        ? h("option", { value: String(t.department_id) }, t.department) : null);
    dept.value = f.dept == null ? "" : String(f.dept);
    const who = h("select", { class: "field", "aria-label": "Given to" });
    const fillPeople = () => {
      const chosen = who.value || (f.who == null ? "" : String(f.who));
      const d = dept.value ? Number(dept.value) : null;
      const inDept = people.filter((p) => d != null && (p.department_ids || []).includes(d));
      const others = people.filter((p) => !inDept.includes(p));
      who.replaceChildren(h("option", { value: "" }, "Anyone"),
        inDept.length ? h("optgroup", { label: state.data.departments.find((x) => x.id === d)?.name || "Department" },
          inDept.map((p) => h("option", { value: String(p.id) }, p.id === me.id ? `${p.name} (you)` : p.name))) : null,
        others.length ? h("optgroup", { label: inDept.length ? "Everyone else" : "People" },
          others.map((p) => h("option", { value: String(p.id) }, p.id === me.id ? `${p.name} (you)` : p.name))) : null);
      if (t?.assigned_id && !people.some((p) => p.id === t.assigned_id)) {
        who.append(h("option", { value: String(t.assigned_id) }, t.assigned_name || "Someone"));
      }
      who.value = chosen;
    };
    fillPeople();
    dept.addEventListener("change", fillPeople);
    const dueDate = h("input", { class: "field", type: "date", value: f.date, "aria-label": "Due date" });
    const dueTime = h("input", { class: "field", type: "time", value: f.time, "aria-label": "Due time", disabled: !f.date });
    dueDate.addEventListener("input", () => { dueTime.disabled = !dueDate.value; if (!dueDate.value) dueTime.value = ""; });
    const quick = (label, days) => h("button", { class: "tk-quick-date", type: "button", onclick: () => {
      const d = new Date();
      d.setDate(d.getDate() + days);
      dueDate.value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      dueDate.dispatchEvent(new Event("input"));
    } }, label);
    const urgent = h("button", { class: f.urgent ? "tk-urgent-toggle on" : "tk-urgent-toggle", type: "button",
      "aria-pressed": f.urgent ? "true" : "false" }, tkIcon("flag"), "Urgent");
    urgent.addEventListener("click", () => {
      const on = urgent.getAttribute("aria-pressed") !== "true";
      urgent.setAttribute("aria-pressed", on ? "true" : "false");
      urgent.classList.toggle("on", on);
    });
    const notes = h("textarea", { class: "field tk-notes-input", rows: "3", maxlength: "4000",
      placeholder: "Notes (optional)", "aria-label": "Notes" });
    notes.value = f.notes;
    const error = h("p", { class: "wb-error", role: "alert", hidden: true });
    const save = h("button", { class: "btn primary tk-save", type: "submit" }, t ? "Save" : "Add task");
    const form = h("form", { class: "tk-form", novalidate: true },
      title,
      h("div", { class: "tk-form-grid" },
        h("label", { class: "tk-field" }, h("span", null, "For"), dept),
        h("label", { class: "tk-field" }, h("span", null, "Given to"), who)),
      h("div", { class: "tk-field" }, h("span", null, "Due"),
        h("div", { class: "tk-due" }, dueDate, dueTime),
        h("div", { class: "tk-quick-dates" }, quick("Today", 0), quick("Tomorrow", 1), urgent)),
      notes, error,
      h("div", { class: "tk-sheet-actions" },
        h("button", { class: "btn", type: "button", onclick: () => tkCloseSheet() }, "Cancel"), save));
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!title.value.trim()) {
        error.textContent = "Give the task a title.";
        error.hidden = false;
        title.setAttribute("aria-invalid", "true");
        title.focus();
        return;
      }
      save.disabled = true;
      const body = { title: title.value, notes: notes.value, department_id: dept.value ? Number(dept.value) : null,
        assigned_id: who.value ? Number(who.value) : null, due_date: dueDate.value, due_time: dueTime.value,
        priority: urgent.getAttribute("aria-pressed") === "true" ? "urgent" : "normal" };
      const res = await api(t ? "PUT" : "POST", t ? `/api/tasks/${t.id}` : "/api/tasks", body);
      if (tk !== state) return;
      save.disabled = false;
      if (res.status !== 200) {
        if (res.status === 400) {
          error.textContent = res.data.error || "Check the task and try again.";
          error.hidden = false;
        } else tkFailed(res, "Couldn't save the task.");
        return;
      }
      tkCloseSheet();
      tkPut(res.data.task);
      state.v = null;
      toast(t ? "Saved." : "Task added.");
    });
    tkSheet("form", t?.id ?? null, t ? "Edit task" : "Quick task", form, { label: t ? "Edit task" : "Quick task" });
  }

  async function tkOpenBin() {
    const state = tk;
    const res = await api("GET", "/api/tasks/bin");
    if (tk !== state) return;
    if (res.status !== 200) return void tkFailed(res, "The bin couldn't be opened.");
    const list = res.data.tasks.map((t) => h("li", { class: "tk-bin-row" },
      h("div", { class: "tk-bin-text" }, h("strong", null, t.title),
        h("small", null, `Deleted by ${t.deleted_by || "someone"} · ${tkWhen(t.deleted_at)}`)),
      h("button", { class: "btn", type: "button", onclick: async (ev) => {
        ev.currentTarget.disabled = true;
        const put = await api("POST", `/api/tasks/${t.id}/restore`);
        if (tk !== state) return;
        if (put.status !== 200) return void tkFailed(put, "Couldn't put it back.");
        toast("Put back.");
        state.v = null;
        await tkLoad();
        if (tk === state && tk.sheet?.kind === "bin") tkOpenBin();
      } }, "Put back")));
    tkSheet("bin", null, "Bin", [
      h("p", { class: "muted tk-bin-hint" }, `Deleted tasks stay here for ${res.data.days} days.`),
      list.length ? h("ul", { class: "tk-bin" }, list) : h("p", { class: "muted" }, "The bin is empty.")]);
  }

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
