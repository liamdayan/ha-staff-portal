// Template report's view: a week stepper, a table, a CSV download, and for managers the
// targets upload and the title setting. Runs inside the staff app (strict CSP: no inline
// styles or handlers, no other scripts); api.h builds elements, and text is always text.
StaffReports.register("template", {
  render(el, data, api) {
    const { h } = api;
    const step = (days) => {
      const d = new Date(`${data.week}T12:00:00`);
      d.setDate(d.getDate() + days);
      api.refresh({ week: d.toISOString().slice(0, 10) });
    };
    const rows = data.people.map((p) => h("tr", null,
      h("td", null, p.name),
      h("td", { class: "num" }, p.hours.toFixed(2)),
      h("td", { class: p.target != null && p.hours < p.target ? "num tpl-short" : "num" },
        p.target == null ? "–" : String(p.target))));
    const csv = () => api.download(`hours-${data.week}.csv`, ["Name,Hours,Target",
      ...data.people.map((p) => [p.name, p.hours, p.target ?? ""].map(api.csvCell).join(","))].join("\n"));
    const manage = api.manage ? h("div", { class: "rp-card" },
      h("h2", null, "Manage"),
      h("label", { class: "rp-field" }, "Weekly targets (CSV)",
        h("input", { type: "file", accept: ".csv", onchange: (ev) => ev.target.files[0] &&
          api.upload("targets", ev.target.files[0]) })),
      h("p", { class: "rp-muted" }, data.targets_file ? `Using ${data.targets_file}` : "No targets yet."),
      h("button", { class: "rp-btn", type: "button", onclick: () => {
        const title = window.prompt("Report title", data.title);
        if (title) api.saveSettings({ title });
      } }, "Rename")) : null;
    // replaceChildren would print null as text: leave out what isn't there.
    el.replaceChildren(...[
      h("div", { class: "rp-toolbar" },
        h("button", { class: "rp-btn", type: "button", onclick: () => step(-7) }, "‹"),
        h("strong", null, `${data.title} · week of ${data.week}`),
        h("button", { class: "rp-btn", type: "button", onclick: () => step(7) }, "›"),
        h("button", { class: "rp-btn", type: "button", onclick: csv }, "CSV")),
      data.people.length ? h("div", { class: "rp-table-wrap" }, h("table", { class: "rp-table" },
        h("thead", null, h("tr", null, h("th", null, "Name"), h("th", { class: "num" }, "Hours"),
          h("th", { class: "num" }, "Target"))),
        h("tbody", null, rows))) : h("p", { class: "rp-muted" }, "No hours this week."),
      manage].filter(Boolean));
  },
});
