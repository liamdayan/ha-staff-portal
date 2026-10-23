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

  function toast(text, isError) {
    toastEl.textContent = text;
    toastEl.classList.toggle("error", !!isError);
    toastEl.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { toastEl.hidden = true; }, 3500);
  }

  async function api(method, path, body) {
    const opts = { method, headers: {} };
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
      await fn();
      if (okText) toast(okText);
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
    if (name === "activity") loadActivity(true);
    if (name === "settings") fillSettings();
    if (name === "roles" && !entities) loadEntities();
    try { sessionStorage.setItem("tab", name); } catch { /* ignore */ }
  }

  document.querySelectorAll("[role=tab]").forEach((b) =>
    b.addEventListener("click", () => {
      if (editing?.dirty) {
        if (!confirm("Discard unsaved role changes?")) return;
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
          if (!confirm(`Unlink ${d.name} from Home Assistant user ${d.ha_user}?`)) return;
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
          if (!confirm(`Delete ${d.name} from the list?`)) return;
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
              h("span", null, `Last used ${ago(d.last_seen).toLowerCase()}`))),
          h("label", { class: "inline-select" }, "Role", roleSelect)),
        h("div", { class: "card-actions" }, actions));
    }));
  }

  async function deviceAction(d, action, confirmText, okText) {
    if (confirmText && !confirm(confirmText)) return;
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

  function editPhone(d) {
    const dialog = $("#phone-dialog");
    const form = $("#phone-form");
    form.reset();
    $("[data-title]", dialog).textContent = d ? `Edit ${d.name}` : "Add phone";
    form.elements["name"].value = d ? d.name : "";
    fillRoleOptions(form.elements["role_id"], d ? d.role_id : state.roles[0]?.id);
    $("[data-pin-hint]", dialog).textContent = d
      ? (d.has_pin ? "Enter a new PIN to change it, or leave blank and tick below to remove it."
        : "Optional, 4 to 8 digits.")
      : "Optional, 4 to 8 digits. Staff enter it when opening the app.";
    const mode = form.elements["signin_mode"];
    mode.value = d?.signin_mode || "phone";
    form.elements["staff_all"].checked = d ? !!d.staff_all : true;
    const pick = $("[data-staff-list]", dialog);
    pick.replaceChildren(...state.staff.map((m) => h("label", { class: "check" },
      h("input", { type: "checkbox", value: m.id, checked: !!d?.staff_ids.includes(m.id) }),
      m.status === "disabled" ? `${m.name} (inactive)` : m.name)));
    $("[data-no-staff]", dialog).hidden = state.staff.length > 0;
    const syncMode = () => {
      const shared = mode.value === "staff";
      form.elements["pin"].closest("label").hidden = shared;
      $("#clear-pin")?.closest("label").toggleAttribute("hidden", shared);
      $("[data-staff-pick]", dialog).hidden = !shared;
      pick.hidden = form.elements["staff_all"].checked;
    };
    mode.onchange = syncMode;
    form.elements["staff_all"].onchange = syncMode;
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
      const status = { active: ["Active", "active"], locked: ["Locked (wrong PINs)", "locked"],
        disabled: ["Inactive", "revoked"] }[m.status] || [m.status, ""];
      const phones = phonesFor(m);
      const actions = [h("button", { class: "btn", onclick: () => editStaff(m) }, "Edit")];
      if (m.status === "locked") {
        actions.unshift(h("button", { class: "btn primary", onclick: () => run(async () => {
          await api("POST", `api/staff/${m.id}/unlock`);
          await load();
        }, `${m.name} can sign in again.`) }, "Unlock"));
      }
      if (m.ha_user) {
        actions.push(h("button", { class: "btn", onclick: () => linkUser(m, "staff") },
          m.ha_link_error ? "Relink HA user" : "Change HA user"));
        actions.push(h("button", { class: "btn subtle", onclick: () => run(async () => {
          if (!confirm(`Unlink ${m.name} from Home Assistant user ${m.ha_user}?`)) return;
          await api("DELETE", `api/staff/${m.id}/link`);
          await load();
        }, "Unlinked.") }, "Unlink"));
      } else {
        actions.push(h("button", { class: "btn", onclick: () => linkUser(m, "staff") }, "Link HA user"));
      }
      actions.push(h("button", { class: "btn danger", onclick: () => run(async () => {
        if (!confirm(`Delete ${m.name}? Their past activity stays in the log.`)) return;
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
  }

  function openRole(id) {
    if (editing?.dirty && editing.id !== id && !confirm("Discard unsaved changes?")) return;
    const role = state.roles.find((r) => r.id === id);
    editing = { id, name: role.name, entities: { ...role.entities }, areas: { ...role.areas },
      dirty: false, filter: "", area: "", onlySelected: false };
    renderRoleList();
    renderRoleEditor();
  }

  $("#add-role").addEventListener("click", () => {
    const name = prompt("Role name (for example Maintenance or Housekeeping):");
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

  function segmented(options, current, label, onPick) {
    const wrap = h("div", { class: "seg", role: "radiogroup", "aria-label": label });
    for (const opt of options) {
      const btn = h("button", {
        type: "button", role: "radio", "aria-checked": String(current === opt), class: `seg-${opt}`,
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
    const current = editing.entities[e.entity_id] || "none";
    const options = e.controllable ? ["none", "view", "control"] : ["none", "view"];
    const labels = { none: "Hidden", view: "View", control: "Control" };
    const wrap = h("div", { class: "seg", role: "radiogroup", "aria-label": `Access to ${e.name}` });
    for (const opt of options) {
      const btn = h("button", {
        type: "button", role: "radio", "aria-checked": String(current === opt), class: `seg-${opt}`,
      }, labels[opt]);
      btn.addEventListener("click", () => {
        setLevel(e.entity_id, opt);
        wrap.querySelectorAll("button").forEach((b) => b.setAttribute("aria-checked", String(b === btn)));
        updateCount();
      });
      wrap.append(btn);
    }
    return wrap;
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
    const del = h("button", { class: "btn danger" }, "Delete role");
    del.addEventListener("click", () => {
      const role = state.roles.find((r) => r.id === editing.id);
      const extra = role?.device_count ? ` ${role.device_count} phone(s) will lose all access.` : "";
      if (!confirm(`Delete the ${editing.name} role?${extra}`)) return;
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
        h("div", { class: "editor-actions" }, del, save)),
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
    if (editing?.dirty) e.preventDefault();
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
    const move = (index, dir) => run(async () => {
      const ids = state.areas.map((a) => a.id);
      [ids[index], ids[index + dir]] = [ids[index + dir], ids[index]];
      await api("PUT", "api/areas/order", { order: ids });
      await load();
    });
    list.replaceChildren(...state.areas.map((a, i) => h("div", { class: "area-row" },
      h("div", { class: "entity-text" },
        h("div", { class: "entity-name" }, a.name),
        h("div", { class: "entity-id" }, areaSummary(a))),
      h("div", { class: "area-actions" },
        h("button", { type: "button", class: "btn subtle", "aria-label": `Move ${a.name} up`,
          disabled: i === 0, onclick: () => move(i, -1) }, "\u2191"),
        h("button", { type: "button", class: "btn subtle", "aria-label": `Move ${a.name} down`,
          disabled: i === state.areas.length - 1, onclick: () => move(i, 1) }, "\u2193"),
        h("button", { type: "button", class: "btn", onclick: () => editArea(a) }, "Edit"),
        h("button", { type: "button", class: "btn danger", onclick: () => run(async () => {
          if (!confirm(`Delete the ${a.name} area? Roles lose any access they had through it.`)) return;
          await api("DELETE", `api/areas/${a.id}`);
          await load();
        }, "Area deleted.") }, "Delete")))));
  }

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

  // ---------------------------------------------------------------- start

  load().then(() => {
    let tab = "phones";
    try { tab = sessionStorage.getItem("tab") || tab; } catch { /* ignore */ }
    selectTab(tab);
  }).catch((err) => toast(err.message, true));
})();
