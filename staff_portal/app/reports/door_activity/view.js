// Door activity: filters (dates, door, kind of card, result, a name or card number), counts, and
// the records newest first, with a CSV download. Runs inside the staff app (strict CSP: no
// inline styles or handlers); api.h builds elements, and text is always text.
(function () {
  let open = true;   // whether the filters are showing (kept between refreshes)

  StaffReports.register("door_activity", {
    render(el, data, api) {
      const { h } = api;
      const picked = { start: data.start, end: data.end, door: data.door, kind: data.kind,
        result: data.result, q: data.q };

      const dateBox = (key, label) => h("label", { class: "rp-field" }, label,
        h("input", { type: "date", value: picked[key], max: data.today,
          onchange: (ev) => { picked[key] = ev.target.value; } }));
      const choose = (key, label, choices) => h("label", { class: "rp-field" }, label,
        h("select", { onchange: (ev) => { picked[key] = ev.target.value; } },
          choices.map(([value, text]) => h("option", { value, selected: value === picked[key] }, text))));
      const apply = () => api.refresh(picked);
      const search = h("input", { type: "search", value: picked.q, maxlength: "60", enterkeyhint: "search",
        placeholder: data.card_numbers ? "Name or card number" : "Name",
        oninput: (ev) => { picked.q = ev.target.value; },
        onkeydown: (ev) => { if (ev.key === "Enter") { ev.preventDefault(); apply(); } } });
      const reset = () => api.refresh({ start: "", end: "", door: "", kind: "", result: "", q: "" });
      // Quick ranges, ending today.
      const lastDays = (n) => {
        const end = new Date(`${data.today}T12:00:00`);
        const start = new Date(end);
        start.setDate(start.getDate() - (n - 1));
        api.refresh({ ...picked, start: iso(start), end: iso(end) });
      };

      const filters = h("details", { class: "rp-card da-filters rp-noprint", open },
        h("summary", null, "Filters"),
        h("div", { class: "da-quick" },
          h("button", { class: "rp-btn", type: "button", onclick: () => lastDays(1) }, "Today"),
          h("button", { class: "rp-btn", type: "button", onclick: () => lastDays(7) }, "7 days"),
          h("button", { class: "rp-btn", type: "button", onclick: () => lastDays(30) }, "30 days")),
        h("div", { class: "da-grid" },
          dateBox("start", "From"),
          dateBox("end", "To"),
          choose("door", "Door", [["", "All doors"], ...data.doors.map((d) => [String(d.id), d.name])]),
          choose("kind", "Card", Object.entries(data.kinds)),
          choose("result", "Result", Object.entries(data.results)),
          h("label", { class: "rp-field" }, "Search", search)),
        h("div", { class: "da-actions" },
          h("button", { class: "rp-btn primary", type: "button", onclick: apply }, "Apply"),
          h("button", { class: "rp-btn", type: "button", onclick: reset }, "Clear")));
      filters.addEventListener("toggle", () => { open = filters.open; });

      const c = data.counts;
      const kpis = h("div", { class: "rp-kpis" },
        kpi(c.total, c.total === 1 ? "record" : "records"), kpi(c.opened, "opened"),
        kpi(c.refused, "refused"), kpi(c.problems, c.problems === 1 ? "problem" : "problems"));

      const numbers = data.card_numbers;
      const head = ["When", "Door", "Who", "How", "Result", ...(numbers ? ["Card number"] : [])];
      const rows = data.records.map((r) => h("tr", { class: r.problem ? "da-problem" : null },
        h("td", { class: "da-when" }, r.when),
        h("td", null, r.door),
        h("td", null, h("div", null, r.who || "—"),
          r.card || r.alert ? h("small", { class: r.alert ? "da-alert" : "rp-muted" },
            [r.card, r.alert].filter(Boolean).join(" · ")) : null),
        h("td", null, r.how),
        h("td", null, h("span", { class: `da-result ${r.result.toLowerCase()}` }, r.result)),
        numbers ? h("td", { class: "num" }, r.card_number || "—") : null));

      const csv = () => api.download(`door-activity-${data.start}-to-${data.end}.csv`, [
        ["When", "Door", "Who", "Card", "How", "Result", "Note", ...(numbers ? ["Card number"] : [])].join(","),
        ...data.records.map((r) => [new Date(r.at * 1000).toISOString(), r.door, r.who, r.card, r.how,
          r.result, r.alert, ...(numbers ? [r.card_number] : [])].map(api.csvCell).join(","))].join("\n"));

      const range = data.start === data.end ? data.start : `${data.start} to ${data.end}`;
      el.replaceChildren(...[
        filters,
        h("div", { class: "rp-toolbar" },
          h("strong", null, range),
          data.records.length ? h("button", { class: "rp-btn rp-noprint", type: "button", onclick: csv }, "CSV") : null),
        kpis,
        data.capped ? h("p", { class: "rp-muted" },
          `Showing the newest ${data.limit}. Narrow the dates or filters to see the rest.`) : null,
        data.records.length
          ? h("div", { class: "rp-table-wrap" }, h("table", { class: "rp-table" },
            h("thead", null, h("tr", null, head.map((t) => h("th", { class: t === "Card number" ? "num" : null }, t)))),
            h("tbody", null, rows)))
          : h("p", { class: "rp-muted" }, "Nothing matches these filters."),
      ].filter(Boolean));

      function kpi(n, label) {
        return h("div", { class: "rp-kpi" }, h("b", null, String(n)), h("span", null, label));
      }
    },
  });

  function iso(d) {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }
})();
