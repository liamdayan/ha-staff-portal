// HR portal. Plain script, no inline handlers (the page's Content-Security-Policy allows only
// scripts from this address). Everything shown is plain text: h() makes text nodes, never HTML.
// Other parts of the portal add scripts (hr-*.js) that load after this one and use
// window.HRPortal: registerSection, registerPage, registerSettings, h, api, toast, ask, and for the
// look (hr.css's design system): pageHead(title, sub, ...extra), emptyState(text, more), initial(name).
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const main = $("#main");
  const H = (window.HRPortal = { sections: {}, pages: {}, settings: {}, me: null });
  let me = null;
  let setupToken = null;

  // ---------------------------------------------------------------- basics

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === false || v == null) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  function show(...nodes) {
    main.replaceChildren(...nodes.flat().filter(Boolean));
    main.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }

  function toast(text, bad) {
    const el = $("#toast");
    el.textContent = text;
    el.classList.toggle("bad", !!bad);
    el.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { el.hidden = true; }, 4000);
  }

  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString([], {
    month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }) : "Never");
  const firstName = (name) => String(name || "").split(" ")[0];

  // A page's title (one per page), its line underneath, and anything after it (badges, buttons).
  function pageHead(title, sub, ...extra) {
    return h("header", { class: "page-head" }, h("h1", { class: "page-title" }, title),
      sub ? h("p", { class: "page-sub" }, sub) : null, extra);
  }

  // A card's empty state: "Nothing here yet." (and a line under it).
  const emptyState = (text, more) => (more
    ? h("div", { class: "empty" }, h("strong", null, text), h("span", null, more))
    : h("p", { class: "empty" }, text));

  // Signed out (sign-in, setup): the portal's logo and name above a card.
  function authPage(title, sub, ...nodes) {
    return h("div", { class: "auth" },
      h("div", { class: "auth-hero" }, h("img", { class: "auth-logo", src: "/icon.png", alt: "" }),
        h("div", { class: "brand-line" }, h("span", null, portalName), h("span", { class: "hr-mark" }, "HR")),
        h("h1", null, title), sub ? h("p", null, sub) : null),
      nodes);
  }

  class SignedOut extends Error {}
  // A page's data that arrived after the person had already moved to another page: dropped, so
  // a late redraw can't paint over the page they're on now.
  class Stale extends Error {}
  let viewSeq = 0;
  const quiet = (err) => err instanceof SignedOut || err instanceof Stale;
  // A page's own redraw after an action, unless the person has moved to another page meanwhile.
  const redrawer = (fn) => {
    const seq = viewSeq;
    return (...args) => (seq === viewSeq ? fn(...args) : undefined);
  };

  // Every request says it comes from this page (another site can't add that header), and sends
  // JSON. A sensitive action may first ask for a fresh code (or PIN), then is sent again once.
  async function api(method, path, body, { background = false, retried = false } = {}) {
    const seq = viewSeq;
    const opts = { method, credentials: "same-origin", headers: { "X-Requested-With": "hr-portal" } };
    if (background) opts.headers["X-Background"] = "1";
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    const text = await res.text().catch(() => "");
    let data = {};
    try { data = JSON.parse(text); } catch { data = { error: text.trim().slice(0, 200) || `Something went wrong (${res.status}).` }; }
    if (res.status === 401 && data.error === "signed_out") {
      showSignIn(me ? "You were signed out. Sign in again to carry on." : "");
      throw new SignedOut("Signed out");
    }
    if (res.status === 401 && data.error === "reconfirm" && !retried) {
      if (!(await reconfirm(data.method))) throw new Error("Not confirmed.");
      return api(method, path, body, { background, retried: true });
    }
    if (!res.ok) {
      const err = new Error(data.error || `Something went wrong (${res.status}).`);
      err.status = res.status;
      throw err;
    }
    if (method === "GET" && !background && seq !== viewSeq) throw new Stale("Moved on");
    return data;
  }

  async function run(fn, okText) {
    try {
      const result = await fn();
      if (result !== false && okText) toast(okText);
      return result;
    } catch (err) {
      if (!quiet(err)) toast(err.message, true);
      return false;
    }
  }

  // ---------------------------------------------------------------- dialogs

  const dialog = $("#dialog");

  // A dialog built from nodes; resolves with the value of the button pressed ("" if closed).
  function openDialog(title, nodes, buttons) {
    return new Promise((resolve) => {
      // A bottom sheet on phones, a panel on bigger screens (hr.css). The close button is the same
      // as Escape: the dialog closes with "".
      const form = h("form", { method: "dialog" },
        h("div", { class: "sheet-head" }, h("h2", null, title),
          h("button", { type: "button", class: "sheet-close", "aria-label": "Close", onclick: () => dialog.close() })),
        nodes,
        h("div", { class: "actions" }, buttons.map((b) =>
          h("button", { value: b.value, class: b.class || "", type: "submit" }, b.label))));
      dialog.replaceChildren(form);
      dialog.returnValue = "";
      dialog.onclose = () => resolve(dialog.returnValue);
      dialog.showModal();
      (form.querySelector("input, select") || form.querySelector("button.primary"))?.focus();
    });
  }

  async function confirmDialog(message, okLabel = "OK", danger = false) {
    const choice = await openDialog("Are you sure?", h("p", null, message), [
      { label: "Cancel", value: "cancel" },
      { label: okLabel, value: "ok", class: danger ? "primary danger" : "primary" }]);
    return choice === "ok";
  }

  async function ask(title, label, { type = "text", value = "", inputmode = null } = {}) {
    const input = h("input", { type, value, inputmode, autocomplete: "off" });
    const choice = await openDialog(title, h("label", null, label, input), [
      { label: "Cancel", value: "cancel" }, { label: "OK", value: "ok", class: "primary" }]);
    return choice === "ok" ? input.value.trim() : null;
  }

  // A sensitive action: type a fresh authenticator code (or the PIN, without an authenticator).
  async function reconfirm(method) {
    for (;;) {
      const code = method === "code";
      const value = await ask("Confirm it's you",
        code ? "The 6-digit code from your authenticator app" : "Your PIN (your Connecteam kiosk code)",
        { type: code ? "text" : "password", inputmode: "numeric" });
      if (value === null) return false;
      try {
        await api("POST", "/api/reconfirm", code ? { code: value } : { pin: value }, { retried: true });
        return true;
      } catch (err) {
        if (err instanceof SignedOut) return false;
        toast(err.message, true);
      }
    }
  }

  function codesList(codes) {
    return h("ol", { class: "codes code" }, codes.map((c) => h("li", null, c)));
  }

  async function showRecoveryCodes(codes) {
    const copy = h("button", { type: "button", onclick: async () => {
      try { await navigator.clipboard.writeText(codes.join("\n")); toast("Codes copied."); }
      catch { toast("Couldn't copy. Write them down instead.", true); }
    } }, "Copy codes");
    await openDialog("Save your recovery codes", [
      h("p", null, "Each code works once, instead of a 6-digit code, if you lose your phone. " +
        "Keep them somewhere safe, away from your phone. They won't be shown again."),
      codesList(codes), h("div", { class: "actions" }, copy)],
    [{ label: "I've saved them", value: "ok", class: "primary" }]);
  }

  async function showQr(result, title = "Setup code") {
    const copy = h("button", { type: "button", onclick: async () => {
      try { await navigator.clipboard.writeText(result.url); toast("Link copied."); }
      catch { toast("Couldn't copy the link.", true); }
    } }, "Copy link");
    await openDialog(title, [
      result.message ? h("p", null, result.message) : null,
      h("img", { class: "qr", src: result.qr, alt: "Setup QR code" }),
      h("p", { class: "code small" }, result.url),
      result.expires_at ? h("p", { class: "muted small" }, `Works until ${when(result.expires_at)}.`) : null,
      h("div", { class: "actions" }, copy)],
    [{ label: "Done", value: "ok", class: "primary" }]);
  }

  // ---------------------------------------------------------------- navigation

  // Phones: the top bar, a floating tab bar (Home, People or My file, More) and the whole menu in a
  // sheet that More opens above the tab bar. Desk width (1024 px): the menu is a sidebar instead.
  const nav = $("#nav");
  const menuBtn = $("#menu-btn");
  const tabbar = $("#tabbar");
  const scrim = $("#nav-scrim");
  const deskQuery = window.matchMedia("(min-width: 1024px)");
  const portalName = document.title.replace(/\s*HR$/, "") || document.title;
  const initial = (name) => String(name || "?").trim().charAt(0).toUpperCase() || "?";
  $("#brand-name").textContent = portalName;

  function setMenu(open) {
    nav.hidden = !open;
    scrim.hidden = !open || deskQuery.matches;
    menuBtn.setAttribute("aria-expanded", String(open && !deskQuery.matches));
  }
  menuBtn.addEventListener("click", () => setMenu(nav.hidden));
  scrim.addEventListener("click", () => setMenu(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !deskQuery.matches && !nav.hidden) setMenu(false);
  });
  deskQuery.addEventListener("change", () => { if (me) setMenu(deskQuery.matches); });

  function drawNav() {
    document.body.classList.toggle("signed-in", !!me);
    const avatar = $("#bar-avatar");
    if (!me) {
      setMenu(false);
      nav.replaceChildren();
      tabbar.hidden = true;
      menuBtn.hidden = true;
      avatar.hidden = true;
      $("#brand-sub").textContent = "HR portal";
      return;
    }
    const mine = me.staff ? ["#/people", "People"] : ["#/people/" + me.id, "My file"];
    const links = [["#/", "Home", me.unread], mine,
      ...Object.entries(H.pages).filter(([, p]) => p.nav && (!p.who || p.who(me))).map(([route, p]) => ["#/" + route, p.nav]),
      ["#/account", "My account"]];
    const admin = me.hr ? [["#/settings", "HR settings"], ["#/audit", "Audit log"]] : [];
    const current = location.hash || "#/";
    const isCurrent = (href) => (href === "#/" ? current === "#/" || current === "#" : current.startsWith(href));
    const link = ([href, label, count], cls = "nav-link") => h("a", { href, class: cls, "aria-current": isCurrent(href) ? "page" : null },
      h("span", { class: "nav-text" }, label), count ? h("span", { class: "count", "aria-label": `${count} unread` }, String(count)) : null);
    nav.replaceChildren(...[
      h("div", { class: "nav-brand" }, h("img", { src: "/icon.png", alt: "", width: "36", height: "36" }),
        h("div", { class: "nav-brand-text" }, h("div", { class: "nav-name" }, portalName), h("div", { class: "nav-sub" }, "HR portal")),
        h("span", { class: "hr-mark" }, "HR")),
      h("div", { class: "nav-links" }, links.map((l) => link(l))),
      admin.length ? h("div", { class: "nav-label" }, "HR admin") : null,
      admin.length ? h("div", { class: "nav-links" }, admin.map((l) => link(l))) : null,
      h("div", { class: "nav-foot" }, h("span", { class: "avatar sm", "aria-hidden": "true" }, initial(me.name)),
        h("div", { class: "nav-who" }, h("div", { class: "nav-who-name" }, me.name), h("div", { class: "nav-who-sub" }, me.role_word || "")),
        h("a", { href: "#/signout", class: "nav-signout" }, "Sign out"))].filter(Boolean));   // no HR admin links: nothing, not "null"
    // The tab bar: Home and People (or My file), then More for everything else.
    const onTab = isCurrent("#/") || isCurrent(mine[0]);
    tabbar.replaceChildren(link(links[0], "tab"), link(mine, "tab"), menuBtn);
    menuBtn.classList.toggle("active", !onTab);
    tabbar.hidden = false;
    menuBtn.hidden = false;
    avatar.hidden = false;
    avatar.textContent = initial(me.name);
    $("#brand-sub").textContent = [me.name, me.role_word].filter(Boolean).join(" · ");
    if (deskQuery.matches) setMenu(true);
  }

  window.addEventListener("hashchange", () => {
    if (!deskQuery.matches) setMenu(false);
    route();
  });

  async function route() {
    if (!me) return;
    viewSeq += 1;
    drawNav();
    const [path, query] = (location.hash.slice(1) || "/").split("?");
    const parts = path.split("/").filter(Boolean);
    const params = new URLSearchParams(query || "");
    try {
      if (parts[0] === "signout") return await signOut();
      if (!parts.length) return await showHome();
      if (parts[0] === "people" && parts[1]) return await showPerson(decodeURIComponent(parts[1]));
      if (parts[0] === "people") return await showPeople(params);
      if (parts[0] === "account") return await showAccount();
      if (parts[0] === "settings" && me.hr) return await showSettings();
      if (parts[0] === "audit" && me.hr) return await showAudit();
      const page = H.pages[parts[0]];
      if (page && (!page.who || page.who(me))) return await page.render(main, parts.slice(1), params);
      show(h("section", { class: "card" }, emptyState("That page isn't here."),
        h("div", { class: "actions" }, h("a", { class: "button", href: "#/" }, "Go home"))));
    } catch (err) {
      if (quiet(err)) return;
      show(h("section", { class: "card" }, h("p", { class: "note bad" }, err.message),
        h("div", { class: "actions" }, h("a", { class: "button", href: "#/" }, "Go home"))));
    }
  }

  async function refreshMe() {
    me = H.me = await api("GET", "/api/me");
    return me;
  }

  // The unread count, now and then, without keeping an idle session alive.
  setInterval(async () => {
    if (!me || document.hidden) return;
    try {
      const fresh = await api("GET", "/api/me", undefined, { background: true });
      if (fresh.unread !== me.unread) {
        me = H.me = fresh;
        drawNav();
      }
    } catch { /* shown by api() when signed out */ }
  }, 60000);

  // ---------------------------------------------------------------- signing in

  function showSignIn(note) {
    me = H.me = null;
    drawNav();
    const error = h("p", { class: "error", role: "alert", hidden: true });
    // Password managers read the standard names: the mobile number is the username, the PIN the
    // password and the authenticator code a one-time code.
    const code = h("input", { name: "otp", id: "otp", inputmode: "numeric", autocomplete: "one-time-code",
      maxlength: "11", autocapitalize: "off", spellcheck: "false" });
    const useRecovery = h("button", { type: "button", class: "linkish", onclick: () => {
      code.setAttribute("inputmode", "text");
      code.setAttribute("autocapitalize", "characters");
      code.placeholder = "ABCDE-FGHJK";
      useRecovery.hidden = true;
      code.focus();
    } }, "Use a recovery code instead");
    const button = h("button", { class: "primary", type: "submit" }, "Sign in");
    const form = h("form", { onsubmit: async (e) => {
      e.preventDefault();
      error.hidden = true;
      button.disabled = true;
      try {
        const f = form.elements;
        await api("POST", "/api/signin", { mobile: f.username.value, pin: f.password.value, code: f.otp.value });
        // (not cleared here: a password manager offers to save what was typed as the page moves on)
        await refreshMe();
        if (!location.hash || location.hash === "#/signout") location.hash = "#/";
        await route();
      } catch (err) {
        if (!(err instanceof SignedOut)) {
          error.textContent = err.message;
          error.hidden = false;
          form.elements.password.value = "";
          form.elements.otp.value = "";
        }
      } finally {
        button.disabled = false;
      }
    } },
    h("label", null, "Mobile number", h("small", null, "As it is in Connecteam"),
      h("input", { name: "username", id: "username", type: "tel", autocomplete: "username", required: true,
        maxlength: "60", autocapitalize: "off", spellcheck: "false" })),
    h("label", null, "PIN", h("small", null, "Your Connecteam kiosk code"),
      h("input", { name: "password", id: "password", type: "password", inputmode: "numeric",
        autocomplete: "current-password", required: true, maxlength: "8" })),
    h("label", null, "Authenticator code", h("small", null, "The 6-digit code from your authenticator app, if you use one"), code),
    useRecovery, error, h("div", { class: "actions full" }, button));
    show(authPage("Sign in to HR", "With your Connecteam mobile number and kiosk code.",
      note ? h("p", { class: "warn" }, note) : null, h("section", { class: "card" }, form),
      h("p", { class: "muted small" }, "Managers, directors and HR need an authenticator code. If yours isn't set up yet, ask HR for a setup code."),
      h("p", { class: "muted small" }, "After 5 wrong tries, sign-in pauses for 15 minutes.")));
    form.elements.username.focus();
  }

  async function signOut() {
    await run(() => api("POST", "/api/signout"));
    location.hash = "#/";
    showSignIn("You're signed out.");
  }

  // ---------------------------------------------------------------- setting up an authenticator

  // Steps: confirm the name, type the PIN, scan the QR code, type a code, save recovery codes.
  async function showSetup(token) {
    setupToken = token;
    history.replaceState(null, "", "/");   // the code isn't left in the address bar or history
    let info;
    try {
      info = await api("GET", `/api/setup/${token}`);
    } catch (err) {
      return show(authPage("Set up", null, h("section", { class: "card" }, h("p", { class: "error" }, err.message))));
    }
    if (!info.has_code) {
      return show(authPage("Set up", null, h("section", { class: "card" },
        h("p", null, "Connecteam has no kiosk code for you yet, so this can't be set up. Ask HR."))));
    }
    show(authPage("Set up your HR sign-in",
      "This takes about two minutes. You'll need your phone with an authenticator app, such as Google Authenticator or Microsoft Authenticator.",
      h("section", { class: "card" }, setupSteps(1),
        h("h2", null, "Is this you?"), h("p", { class: "code who-name" }, info.name),
        h("div", { class: "actions full" },
          h("button", { class: "primary", type: "button", onclick: setupPin }, "Yes, that's me"))),
      h("p", { class: "muted small" }, "Not you? Close this page and tell HR.")));
  }

  function setupPin() {
    const error = h("p", { class: "error", role: "alert", hidden: true });
    const form = h("form", { onsubmit: async (e) => {
      e.preventDefault();
      error.hidden = true;
      try {
        const r = await api("POST", `/api/setup/${setupToken}/pin`, { pin: form.elements.pin.value });
        setupScan(r);
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
      }
    } }, h("label", null, "Your PIN", h("small", null, "Your Connecteam kiosk code"),
      h("input", { name: "pin", type: "password", inputmode: "numeric", autocomplete: "current-password", required: true, maxlength: "8" })),
    error, h("div", { class: "actions full" }, h("button", { class: "primary", type: "submit" }, "Next")));
    show(authPage("Type your PIN", null, h("section", { class: "card" }, setupSteps(2), form)));
    form.elements.pin.focus();
  }

  function scanStep(r, onCode) {
    const error = h("p", { class: "error", role: "alert", hidden: true });
    const form = h("form", { onsubmit: async (e) => {
      e.preventDefault();
      error.hidden = true;
      try {
        await onCode(form.elements.code.value);
      } catch (err) {
        if (!(err instanceof SignedOut)) {
          error.textContent = err.message;
          error.hidden = false;
        }
      }
    } },
    h("p", null, "Open your authenticator app, add an account, and scan this code."),
    h("img", { class: "qr", src: r.qr, alt: "QR code for your authenticator app" }),
    h("p", { class: "muted small" }, "Can't scan it? Add it by hand with this key:"),
    h("p", { class: "code" }, r.secret),
    h("label", null, "Type the 6-digit code the app shows",
      h("input", { name: "code", inputmode: "numeric", autocomplete: "one-time-code", required: true, maxlength: "7" })),
    error, h("div", { class: "actions full" }, h("button", { class: "primary", type: "submit" }, "Check the code")));
    return form;
  }

  // Where someone is in setting up: three steps (name, PIN, authenticator).
  const setupSteps = (at) => h("ol", { class: "steps", "aria-label": `Step ${at} of 3` },
    [1, 2, 3].map((n) => h("li", { class: n <= at ? "on" : null })));

  function setupScan(r) {
    show(authPage("Scan with your phone", null, h("section", { class: "card" }, setupSteps(3),
      scanStep(r, async (code) => {
        const done = await api("POST", `/api/setup/${setupToken}/finish`, { code });
        setupToken = null;
        await showRecoveryCodes(done.recovery_codes);
        await refreshMe();
        location.hash = "#/";
        await route();
      }))));
  }

  // ---------------------------------------------------------------- home

  async function showHome() {
    const again = redrawer(showHome);
    const home = await api("GET", "/api/home");
    const last = me.last_signin?.at
      ? `Last sign-in: ${when(me.last_signin.at)}${me.last_signin.ip ? `, from ${me.last_signin.ip}` : ""}. Not you? Tell HR.`
      : "This is your first sign-in.";
    const inbox = home.inbox || [];
    const unread = inbox.filter((i) => !i.read_at).length;
    show(
      pageHead(`Hello, ${firstName(me.name)}`, last),
      h("div", { class: home.todos.length ? "cols" : "stack" },
        home.todos.length ? h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", null, "To do"), h("span", { class: "badge warn" }, String(home.todos.length))),
          h("ul", { class: "list" }, home.todos.map((t) => h("li", null, t.link
            ? h("a", { class: "item", href: t.link }, h("span", { class: "title" }, t.text))
            : h("div", { class: "item" }, h("span", { class: "title" }, t.text)))))) : null,
        h("section", { class: "card" },
          h("div", { class: "card-head" }, h("h2", null, "Inbox"),
            unread ? h("button", { type: "button", class: "small-btn", onclick: () => run(async () => {
              const r = await api("POST", "/api/inbox/read", {});
              me.unread = r.unread;
              await again();
              drawNav();
            }) }, "Mark all read") : null),
          inbox.length ? h("ul", { class: "list" }, inbox.map(inboxItem))
            : emptyState("Nothing here yet.", "Messages from HR and the portal show here."))));
  }

  function inboxItem(i) {
    const open = async (e) => {
      e.preventDefault();
      if (!i.read_at) {
        const r = await run(() => api("POST", "/api/inbox/read", { id: i.id }));
        if (r) me.unread = r.unread;
      }
      if (i.link) location.hash = i.link;
      else await showHome();
      drawNav();
    };
    return h("li", { class: i.read_at ? "" : "unread" }, h("a", { class: "item", href: i.link || "#/", onclick: open },
      h("div", { class: "title" }, i.title), h("div", { class: "sub" }, when(i.created_at))));
  }

  // ---------------------------------------------------------------- people

  async function showPeople(params) {
    const former = params.get("former") === "1";
    const q = params.get("q") || "";
    const data = await api("GET", `/api/people?${new URLSearchParams({ q, former: former ? "1" : "0" })}`);
    const todoFilter = params.get("todo");
    let people = data.people;
    if (todoFilter === "start") people = people.filter((p) => (p.flags || []).includes("Start date to confirm"));
    if (todoFilter === "code") people = people.filter((p) => (p.flags || []).includes("No kiosk code in Connecteam"));
    if (todoFilter === "access") people = people.filter((p) => (p.flags || []).includes("Access ended"));
    const search = h("input", { type: "search", name: "q", value: q, placeholder: "Search by name", "aria-label": "Search by name" });
    const form = h("form", { class: "search", role: "search", onsubmit: (e) => {
      e.preventDefault();
      location.hash = `#/people?${new URLSearchParams({ q: search.value, ...(former ? { former: "1" } : {}) })}`;
    } }, search, h("button", { type: "submit" }, "Search"));
    show(pageHead(former ? "Former employees" : "People",
      people.length ? `${people.length} ${people.length === 1 ? "person" : "people"}${q ? ` matching “${q}”` : ""}` : null),
    form,
    me.hr || todoFilter ? h("div", { class: "toolbar" },
      me.hr ? h("nav", { class: "seg", "aria-label": "Current or former employees" },
        h("a", { href: "#/people", "aria-current": former ? null : "page" }, "Current"),
        h("a", { href: "#/people?former=1", "aria-current": former ? "page" : null }, "Former")) : null,
      todoFilter ? h("a", { class: "button small-btn", href: "#/people" }, "Show everyone") : null) : null,
    h("section", { class: "card flush" },
      people.length ? h("ul", { class: "list" }, people.map((p) => h("li", null,
        h("a", { class: "item with-avatar", href: `#/people/${encodeURIComponent(p.id)}` },
          h("span", { class: "avatar", "aria-hidden": "true" }, initial(p.name)),
          h("div", { class: "item-text" },
            h("div", { class: "title" }, p.name),
            h("div", { class: "sub" }, [p.departments.join(", ") || "No department", p.role !== "Employee" ? p.role : null].filter(Boolean).join(" · ")),
            (p.flags || []).length ? h("div", { class: "chips" }, p.flags.map((f) => h("span", { class: "badge bad" }, f))) : null)))))
        : emptyState(q ? "No one by that name." : "No one to show.")));
  }

  async function showPerson(id) {
    const again = redrawer(() => showPerson(id));
    const data = await api("GET", `/api/people/${encodeURIComponent(id)}`);
    const p = data.person;
    const sections = data.sections.map((s) => {
      const custom = H.sections[s.name];
      const box = h("section", { class: "card", "data-section": s.name });
      if (custom) custom(box, s, p, again);
      else drawSection(box, s, p, again);
      return box;
    });
    const flags = [p.archived ? "Former employee" : null, ...(p.flags || [])].filter(Boolean);
    show(h("header", { class: "page-head with-avatar" },
      me.staff ? h("a", { class: "back-link", href: p.archived ? "#/people?former=1" : "#/people" }, "People") : null,
      h("span", { class: "avatar lg", "aria-hidden": "true" }, initial(p.name)),
      h("h1", { class: "page-title" }, p.name),
      h("p", { class: "page-sub" }, [p.departments.join(", ") || "No department", p.role].join(" · ")),
      flags.length ? h("div", { class: "chips" }, flags.map((f) => h("span", { class: "badge bad" }, f))) : null),
    sections);
  }

  // A section as the server describes it: rows, notes and action buttons.
  function drawSection(box, s, person, redraw) {
    box.append(h("h2", null, s.title));
    if (s.rows?.length) {
      box.append(h("dl", { class: "rows" }, s.rows.flatMap((r) => [h("dt", null, r.label), h("dd", null, r.value ?? "")])));
    }
    for (const note of s.notes || []) box.append(h("p", { class: "warn" }, note));
    if (s.actions?.length) {
      box.append(h("div", { class: "actions" }, s.actions.map((a) =>
        h("button", { type: "button", class: a.danger ? "danger" : "", onclick: () => doAction(a, redraw) }, a.label))));
    }
  }

  async function doAction(a, redraw) {
    const seq = viewSeq;
    let body = a.body;
    if (a.confirm && !(await confirmDialog(a.confirm, a.label, a.danger))) return;
    if (a.ask_text !== undefined) {
      const value = await ask(a.label, a.ask_text);
      if (value === null) return;
      body = { ...(body || {}), value };
    }
    if (a.ask_date !== undefined) {
      const value = await ask(a.label, "Start date", { type: "date", value: a.ask_date });
      if (value === null) return;
      body = { ...(body || {}), value };
    }
    const result = await run(() => api(a.method, a.path, body));
    if (!result) return;
    if (result.qr) await showQr(result);
    else if (result.message) toast(result.message);
    else toast("Done.");
    if (seq === viewSeq) await Promise.resolve(redraw()).catch((err) => { if (!quiet(err)) toast(err.message, true); });
  }

  // ---------------------------------------------------------------- my account

  async function showAccount() {
    const again = redrawer(showAccount);
    const acc = await api("GET", "/api/account");
    const totp = h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Authenticator code"),
      acc.totp_on ? h("span", { class: "badge ok" }, "On") : h("span", { class: acc.totp_required ? "badge bad" : "badge quiet" }, "Off")));
    if (acc.totp_on) {
      totp.append(h("p", null, "On. You type a code from your authenticator app when you sign in."),
        h("p", { class: "muted small" }, `${acc.recovery_left} recovery code${acc.recovery_left === 1 ? "" : "s"} left.`),
        h("div", { class: "actions" },
          h("button", { type: "button", onclick: () => newPhone() }, "Set up a new phone"),
          h("button", { type: "button", onclick: () => run(async () => {
            const r = await api("POST", "/api/account/recovery-codes", {});
            await showRecoveryCodes(r.recovery_codes);
            await again();
          }) }, "Make new recovery codes"),
          acc.totp_required ? null : h("button", { type: "button", class: "danger", onclick: async () => {
            if (!(await confirmDialog("Turn off your authenticator code? Signing in will only need your number and PIN.", "Turn off", true))) return;
            if (await run(() => api("DELETE", "/api/account/authenticator"), "Authenticator turned off.")) await again();
          } }, "Turn off")));
    } else if (acc.totp_required) {
      totp.append(h("p", { class: "warn" }, "Your role needs an authenticator code. Ask HR for a setup code."));
    } else {
      totp.append(h("p", null, "Off. Turn it on so that someone who learns your PIN still can't get in."),
        h("div", { class: "actions" }, h("button", { type: "button", class: "primary", onclick: () => newPhone(true) }, "Turn on")));
    }
    const devices = h("section", { class: "card" }, h("h2", null, "Signed-in devices"),
      h("ul", { class: "list" }, acc.sessions.map((s) => h("li", null, h("div", { class: "item split" },
        h("div", { class: "item-text" },
          h("div", { class: "title" }, s.current ? "This device" : deviceName(s.agent),
            s.current ? h("span", { class: "badge quiet" }, "Now") : null),
          h("div", { class: "sub" }, `Signed in ${when(s.created_at)} · last used ${when(s.last_used)}${s.ip ? ` · ${s.ip}` : ""}`)),
        s.current ? null : h("button", { type: "button", class: "small-btn", onclick: () => run(async () => {
          await api("DELETE", `/api/account/sessions/${s.id}`);
          await again();
        }, "Signed out on that device.") }, "Sign out"))))),
      h("div", { class: "actions" }, h("button", { type: "button", class: "danger", onclick: async () => {
        if (!(await confirmDialog("Sign out on every device, including this one?", "Sign out everywhere", true))) return;
        if (await run(() => api("POST", "/api/signout-everywhere", {}))) showSignIn("You're signed out everywhere.");
      } }, "Sign out everywhere")));
    show(h("header", { class: "page-head with-avatar" },
      h("span", { class: "avatar lg", "aria-hidden": "true" }, initial(me.name)),
      h("h1", { class: "page-title" }, "My account"),
      h("p", { class: "page-sub" }, `${me.name} · ${me.role_word}`),
      h("p", { class: "page-sub small" }, `You're signed out after ${me.idle_minutes} minutes without use, and after 8 hours.`)),
    totp, devices);
  }

  function deviceName(agent) {
    const a = String(agent || "");
    const os = /iPhone|iPad/.test(a) ? "iPhone or iPad" : /Android/.test(a) ? "Android" : /Mac OS/.test(a) ? "Mac"
      : /Windows/.test(a) ? "Windows" : "Another device";
    const browser = /Edg\//.test(a) ? "Edge" : /Chrome\//.test(a) ? "Chrome" : /Safari\//.test(a) ? "Safari"
      : /Firefox\//.test(a) ? "Firefox" : "";
    return browser ? `${browser} on ${os}` : os;
  }

  async function newPhone(firstTime = false) {
    let body = {};
    if (firstTime) {
      const pin = await ask("Turn on an authenticator", "Your PIN (your Connecteam kiosk code)", { type: "password", inputmode: "numeric" });
      if (pin === null) return;
      body = { pin };
    }
    const r = await run(() => api("POST", "/api/account/authenticator", body));
    if (!r) return;
    show(pageHead(firstTime ? "Turn on an authenticator" : "Set up a new phone",
      firstTime ? null : "Once this is done, your old phone's codes stop working."),
    h("section", { class: "card narrow" },
      scanStep(r, async (code) => {
        const done = await api("POST", "/api/account/authenticator/finish", { token: r.token, code });
        await showRecoveryCodes(done.recovery_codes);
        await refreshMe();
        await showAccount();
      }),
      h("div", { class: "actions" }, h("a", { class: "button", href: "#/account" }, "Cancel"))));
  }

  // ---------------------------------------------------------------- HR settings

  async function showSettings() {
    const again = redrawer(showSettings);
    const s = await api("GET", "/api/settings");
    const g = s.general;
    const nameOf = (id) => s.people.find((p) => p.id === id)?.name || id;
    const personSelect = (name) => h("select", { name, required: true },
      h("option", { value: "" }, "Choose someone"),
      s.people.filter((p) => p.id !== s.me).map((p) => h("option", { value: p.id }, p.name)));

    const setRole = async (personId, role) => {
      if (await run(() => api("PUT", "/api/settings/role", { person_id: personId, role }), "Saved.")) await again();
    };
    const roleForm = h("form", { class: "row", onsubmit: (e) => {
      e.preventDefault();
      setRole(roleForm.elements.person.value, roleForm.elements.role.value);
    } }, personSelect("person"), h("select", { name: "role" },
      h("option", { value: "manager" }, "Manager"), h("option", { value: "director" }, "Director")),
    h("button", { type: "submit", class: "primary" }, "Give role"));
    const roleList = (title, list, role) => [h("h3", null, title), list.length
      ? h("ul", { class: "list" }, list.map((p) => h("li", null, h("div", { class: "item split" }, h("span", { class: "title" }, p.name),
        p.id === s.me ? null : h("button", { type: "button", class: "linkish", onclick: async () => {
          if (await confirmDialog(`Take ${p.name}'s ${role} role away?`, "Take away", true)) await setRole(p.id, "none");
        } }, "Take away")))))
      : h("p", { class: "muted small" }, "None yet.")];
    const roles = h("section", { class: "card" }, h("h2", null, "Roles"),
      h("p", { class: "muted small" }, "Directors see everyone except HR admins and other directors. Managers see the people in the departments you give them. Only the owner makes HR admins, in Home Assistant."),
      h("h3", null, "HR admins"), s.sections.roles.hr_admins.length
        ? h("div", { class: "chips" }, s.sections.roles.hr_admins.map((p) => h("span", { class: "badge" }, p.name)))
        : h("p", null, "None"),
      roleList("Directors", s.sections.roles.directors, "director"),
      roleList("Managers", s.sections.roles.managers, "manager"), h("h3", null, "Give someone a role"), roleForm);

    const departments = h("section", { class: "card" }, h("h2", null, "Managers' departments"),
      h("p", { class: "muted small" }, "Departments are your Connecteam smart groups. Each department can have one primary manager. The direct manager in Connecteam is only a hint."),
      s.sections.roles.managers.length ? s.sections.roles.managers.map((m) => managerForm(m, s)) : h("p", { class: "muted" }, "Give someone the Manager role first."));

    const policy = h("form", { onsubmit: (e) => {
      e.preventDefault();
      const f = policy.elements;
      run(async () => {
        await api("PUT", "/api/settings/general", { totp_policy: f.totp_policy.value, session_minutes: f.session_minutes.value });
        await refreshMe();
        await again();
      }, "Saved.");
    } }, h("p", { class: "muted small" }, "Anyone can also turn one on for themselves in My account."),
    Object.entries(s.policies).map(([value, label]) => h("label", { class: "check" },
      h("input", { type: "radio", name: "totp_policy", value, checked: g.totp_policy === value }), label)),
    h("label", null, "Sign out after this many minutes without use", h("small", null, "5 to 30. Everyone is also signed out after 8 hours."),
      h("input", { name: "session_minutes", type: "number", min: "5", max: "30", value: g.session_minutes })),
    h("div", { class: "actions" }, h("button", { type: "submit", class: "primary" }, "Save")));
    const signinBox = h("section", { class: "card" }, h("h2", null, "Who must use an authenticator code"), policy);

    const notifications = h("section", { class: "card" }, h("h2", null, "Notifications"),
      h("p", { class: "muted small" }, "Everything new shows in each person's inbox on their home page here. Nothing is sent outside the HR portal: it only reads from Connecteam."));

    const fieldOptions = (current) => [h("option", { value: "" }, "Find it by its name"),
      ...s.fields.map((f) => h("option", { value: f.id, selected: f.id === current }, f.name))];
    const fieldsForm = h("form", { onsubmit: (e) => {
      e.preventDefault();
      const f = fieldsForm.elements;
      run(() => api("PUT", "/api/settings/general", { start_field: f.start_field.value, manager_field: f.manager_field.value,
        date_order: f.date_order.value }), "Saved. People are read again in a few minutes.");
    } },
    s.fields.length ? null : h("p", { class: "warn" }, "Connecteam's profile fields haven't been read yet. They're read once a day."),
    h("label", null, "Start date", h("small", null, "Without a date here, the day someone was added to Connecteam is used, marked estimated."),
      h("select", { name: "start_field" }, fieldOptions(g.start_field))),
    h("label", null, "Direct manager", h("select", { name: "manager_field" }, fieldOptions(g.manager_field))),
    h("label", null, "Connecteam writes dates as", h("select", { name: "date_order" },
      h("option", { value: "mdy", selected: g.date_order === "mdy" }, "Month/day/year (10/25/2026)"),
      h("option", { value: "dmy", selected: g.date_order === "dmy" }, "Day/month/year (25/10/2026)"))),
    h("div", { class: "actions" }, h("button", { type: "submit", class: "primary" }, "Save")));
    const fields = h("section", { class: "card" }, h("h2", null, "Connecteam profile fields"), fieldsForm);

    const recordsForm = h("form", { onsubmit: (e) => {
      e.preventDefault();
      run(() => api("PUT", "/api/settings/general", { retention_years: recordsForm.elements.retention_years.value }), "Saved.");
    } }, h("label", null, "Keep records of people who left for (years)",
      h("small", null, "Then they're listed for HR to review and delete. Nothing is deleted automatically, and a legal hold stops it. Ask your employment lawyer to confirm the period."),
      h("input", { name: "retention_years", type: "number", min: "1", max: "30", value: g.retention_years })),
    h("div", { class: "actions" }, h("button", { type: "submit", class: "primary" }, "Save")));
    const records = h("section", { class: "card" }, h("h2", null, "Records"), recordsForm);

    const keyBox = h("section", { class: "card" }, h("h2", null, "HR key"),
      h("p", null, s.key.kept_out
        ? "The HR key is kept out of Home Assistant backups. After restoring a backup, HR stays locked until the owner types the HR recovery key."
        : "The HR key is in Home Assistant backups, so restoring a backup just works. Turn on Home Assistant's backup encryption too."),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: s.key.kept_out, onchange: async (e) => {
        const on = e.target.checked;
        if (on && !(await confirmDialog("A recovery key will be shown once. Print it and keep it in the safe: without it, HR records can't be opened after a restore.", "Show the recovery key"))) {
          e.target.checked = false;
          return;
        }
        const r = await run(() => api("POST", "/api/settings/key", { keep_out: on }));
        if (!r) { e.target.checked = !on; return; }
        if (r.recovery_key) {
          await openDialog("Your HR recovery key", [h("p", null, "Print this or write it down, and keep it in the safe. It isn't shown again."),
            h("p", { class: "code" }, r.recovery_key)], [{ label: "I've kept it", value: "ok", class: "primary" }]);
        }
        await again();
      } }), "Keep the HR key out of backups"));

    const extra = Object.entries(s.sections).filter(([name]) => H.settings[name]).map(([name, data]) => {
      const box = h("section", { class: "card", "data-settings": name });
      H.settings[name](box, data, again);
      return box;
    });
    show(pageHead("HR settings", "Roles, sign-in, Connecteam fields, records and each part of the portal."),
      roles, departments, signinBox, notifications, fields, records, ...extra, keyBox);

    function managerForm(m) {
      const form = h("form", { onsubmit: (e) => {
        e.preventDefault();
        const groups = {};
        for (const d of s.departments) {
          if (form.elements[`g-${d.group}`]?.checked) groups[d.group] = !!form.elements[`p-${d.group}`]?.checked;
        }
        run(async () => {
          await api("PUT", `/api/people/${encodeURIComponent(m.id)}/departments`, { groups });
          await again();
        }, "Saved.");
      } }, h("h3", null, m.name),
      s.departments.length ? s.departments.map((d) => h("div", { class: "row" },
        h("label", { class: "check" }, h("input", { type: "checkbox", name: `g-${d.group}`, checked: d.group in m.groups }), d.name),
        h("label", { class: "check" }, h("input", { type: "checkbox", name: `p-${d.group}`, checked: !!m.groups[d.group] }), "Primary")))
        : h("p", { class: "muted" }, "No departments yet: they come from Staff Access's departments and Connecteam's smart groups."),
      m.id === s.me ? h("p", { class: "muted small" }, "Another HR admin changes your own departments.")
        : h("div", { class: "actions" }, h("button", { type: "submit" }, `Save ${firstName(nameOf(m.id))}'s departments`)));
      return form;
    }
  }

  // ---------------------------------------------------------------- audit log

  async function showAudit() {
    const data = await api("GET", "/api/audit");
    const lines = h("ul", { class: "list lines" });
    let oldest = null;
    const add = (rows) => {
      for (const l of rows) {
        lines.append(h("li", null, h("div", { class: "what" }, l.action, l.target ? ` · ${l.target}` : ""),
          h("div", { class: "muted small" }, [when(l.ts), l.actor, l.ip ? `from ${l.ip}` : null].filter(Boolean).join(" · ")),
          l.detail ? h("div", { class: "small" }, l.detail) : null));
      }
      oldest = rows.at(-1)?.id ?? oldest;
    };
    add(data.lines);
    const more = h("button", { type: "button", hidden: data.lines.length < 100, onclick: () => run(async () => {
      const next = await api("GET", `/api/audit?before=${oldest}`);
      add(next.lines);
      more.hidden = next.lines.length < 100;
    }) }, "Show older");
    const result = h("p", { class: "muted small" }, checkText(data.last_check));
    show(pageHead("Audit log", "Every sign-in, look at a file and change. Lines can't be changed: Check audit log shows whether any were."),
      h("section", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, "Check"),
        h("button", { type: "button", class: "primary", onclick: () => run(async () => {
          const r = await api("POST", "/api/audit/check", {});
          result.textContent = checkText(r);
          toast(r.ok ? "No changes found." : "Something was changed. See the note.", !r.ok);
        }) }, "Check audit log")), result),
      h("section", { class: "card" }, data.lines.length ? lines : emptyState("Nothing yet."),
        h("div", { class: "actions" }, more)));
  }

  function checkText(r) {
    if (!r) return "Not checked yet.";
    if (r.ok) return `Last checked ${when(r.checked_at)}: ${r.lines} lines, no changes found.`;
    return r.missing_end
      ? `Last checked ${when(r.checked_at)}: lines are missing from the end. Someone may have removed them; tell the owner.`
      : `Last checked ${when(r.checked_at)}: line ${r.first_bad} doesn't match. Someone may have changed the log; tell the owner.`;
  }

  // ---------------------------------------------------------------- for other parts

  Object.assign(H, {
    h, api, toast, ask, confirmDialog, openDialog, showQr, show, when, route, pageHead, emptyState, initial,
    registerSection(name, render) { H.sections[name] = render; },
    registerPage(route_, page) {
      H.pages[route_] = page;
      if (me) drawNav();   // a part that loaded after the first page was drawn: show it in the menu
    },
    registerSettings(name, render) { H.settings[name] = render; },
  });

  // ---------------------------------------------------------------- start

  async function start() {
    const setup = location.pathname.match(/^\/setup\/([A-Za-z0-9_-]{20,64})$/);
    if (setup) return showSetup(setup[1]);
    try {
      await refreshMe();
    } catch (err) {
      if (!(err instanceof SignedOut)) show(h("p", { class: "error center" }, err.message));
      return;
    }
    await route();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
