# Adding a report

A guide for Claude or a developer. A report is one folder of code; dropping it in is all it
takes. Nothing else in the add-on changes: the folder is found at startup, the owner turns it on
and picks departments in the dashboard's **Reports** tab, and staff open it under **Reports** in
the staff app.

## 1. Copy the template

```
cp -r app/reports/_template app/reports/<key>
```

`<key>` is the report's name in code: lower-case letters, digits and `_`, starting with a letter
(`attendance`, `pool_checks`). Folders starting with `_` are hidden, which is why the template
never shows.

In the copy, change `key="template"` in `report.py` and `StaffReports.register("template", ...)`
in `view.js` to the new key. The folder holds:

| File | What it is |
|---|---|
| `report.py` | `REPORT = dict(...)` and an async `data(ctx, params)` that returns a dict |
| `view.js` | `StaffReports.register("<key>", {render(el, data, api)})`: draws the dict |
| `view.css` | optional styles, for this report only |

## 2. report.py

```python
async def data(ctx, params):
    rows = await ctx.ct.timesheets(start, end)
    return {"people": [...]}          # anything JSON can hold

REPORT = dict(
    key="<key>",                       # the folder's name
    name="Hours this week",            # shown in the list
    description="One line under the name.",
    data=data,                         # must be async
    params={"week": ""},               # query params data() gets, with defaults (others ignored)
    uploads=[{"key": "gusto", "name": "Gusto time off", "accept": ".csv",
              "help": "Reports > Time Off Balances, as CSV."}],
    settings=[{"key": "rules", "default": {}}],   # keys managers may save, with defaults
    # check_settings=fn,               # optional: fn(settings) -> settings, raise ValueError
)
```

- Import only the standard library (`datetime`, `csv`, `io`, `json`...). Everything from the
  add-on comes in through `ctx`.
- `params` values arrive as text (at most 200 characters each): check them. Raise `ValueError`
  with a short, plain sentence for bad input; the app shows it as it is.
- Return plain data (dicts, lists, text, numbers). Do the presentation in `view.js`.
- A report's own bug never takes the app down: it shows "This report couldn't be made", and the
  error goes to the add-on's log.

### What ctx has

| | |
|---|---|
| `ctx.ct` | Connecteam data (`app/ctdata.py`), below |
| `ctx.uploads(key)` | that upload's files, newest first: `[{name, text, uploaded_at, by}]` (at most 5) |
| `ctx.settings` | the report's settings: the declared defaults, with what managers saved over them |
| `ctx.person` | `{id, name, connecteam_user_id, departments: [{id, name}]}`: every department they're in |
| `ctx.departments` | every department, in the Staff tab's order: `[{id, name, smart_group_id}]` (Connecteam smart groups; hidden ones are left out) |
| `ctx.staff` | each person linked to Connecteam and their departments here: `[{connecteam_user_id, department_ids, department_id}]` (`department_id`: the first of them) |
| `ctx.tz` | the property's time zone (a `ZoneInfo`) |
| `ctx.today` | today's date there |
| `ctx.manage` | whether this person may manage the report |
| `ctx.report` | the report's own details (key, name, uploads, settings declared) |

A Connecteam user's departments here: first `ctx.staff`, the departments of the staff member
linked to that Connecteam user (chosen by hand or by sync). For people not linked, `await
ctx.ct.users()` gives each Connecteam user's smart `groups`, and each department in
`ctx.departments` follows one smart group (`smart_group_id`, or None); someone in several mapped
groups is in each of those departments, as in Staff Access itself (the first in the list is the
one to show them under). `app/reports/attendance/report.py` (`department_lists`) does exactly
this.

### ctx.ct: Connecteam

All async, all cached (timesheets, shifts and form submissions are read a calendar month at a
time and kept by month for a day; a report's Refresh now (`?fresh=1`) reads them again, and
`data()`'s answer gets `read_at`, when its oldest month was read), all in the
property's time zone. Times are `"YYYY-MM-DDTHH:MM"`, dates `"YYYY-MM-DD"`, user IDs text.
`start` and `end` are dates (or `"YYYY-MM-DD"`), both included, at most 400 days apart.

```python
await ctx.ct.timesheets(start, end)
# [{user_id, name, date, clock_in, clock_out, break_minutes, job, type, hours,
#   sched_start, sched_end}]  type: "shift", "break" (a manual break; its length in
#   break_minutes) or "time_off". clock_out and hours are None while still on the clock.
await ctx.ct.shifts(start, end)
# [{user_id, name, date, start, end, job, title, draft, open}]  one row per person; an open
#   shift nobody took has user_id None
await ctx.ct.forms()                       # [{id, name, questions: [{id, title, type}]}]
await ctx.ct.form_submissions(form_id, start, end)
# [{id, user_id, name, submitted_at, answers: [{question, answer}]}]  answers as plain text;
#   start may be a datetime (only submissions from then on)
await ctx.ct.users()                       # [{user_id, name, groups}]
```

Each raises `ctx.ct.Unavailable(message)` when Connecteam can't answer: a missing permission
(the message names it and quotes Connecteam), the reports budget, or no connection. Let it pass:
the app shows the message with **Try again**. Catch it only to carry on without optional data
(the attendance report catches it per part, so the rest of the report still shows).

The permissions (scopes) each needs: `users()` users.read, `timesheets()` time_clock.read,
`shifts()` schedule.read, `forms()` and `form_submissions()` forms.read; job names need
jobs.read, and come back empty without it. DOCS.md, "Connecteam permissions", lists them for the
owner.

### Uploads

Declare each kind in `uploads`. `accept` lists text types only (`.csv`, `.tsv`, `.txt`, `.json`);
files are at most 2 MB unless you set `max_bytes` (up to 5 MB). Only people who may manage the
report can upload. The newest 5 of each kind are kept under `DATA_DIR/reports/<key>/uploads/`;
use `ctx.uploads(key)[0]` for the latest. Uploads go in migration files, not in backups.

### Settings

Declare the keys in `settings`. Managers save them from the view with `api.saveSettings({...})`;
only declared keys are kept, at most 48 KB as JSON (the staff app takes request bodies up to
64 KB). Add `check_settings` to clean them (return the cleaned dict) or refuse them (raise
`ValueError` with a plain sentence, which the person sees). Clean them again when you read
`ctx.settings` too: a backup or an older version may have stored something else. Everyone else
sees them read-only (`api.manage` is false). If settings name people some viewers mustn't see,
return a trimmed copy as `settings` in what `data()` returns: someone who can't manage the
report then gets that as `api.settings`, never what's stored.

### Who sees whom

Access decides who may open a report; what's in it is up to `data()`. When a report shows people,
think about whether a viewer should see everyone. The attendance report has a setting for it
(`who_sees`: `"all"`, the default, or `"own"`): with `"own"`, someone who only views it gets the
people in their own departments (`ctx.person["departments"]`), and `ctx.manage` sees everyone.
Filter in `data()`, before returning: anything returned reaches the browser, whatever the view
shows.

## 3. view.js

```js
StaffReports.register("<key>", {
  render(el, data, api) {
    // el: the box to draw in. data: what data() returned. Called again, with a new box, after
    // refresh(), an upload or saved settings: keep what the person picked (a tab, say) in a
    // variable outside render().
    el.replaceChildren(api.h("p", null, `${data.people.length} people`));
  },
});
```

`el.replaceChildren(a, null)` prints the word "null": leave out empty parts first
(`...[a, b].filter(Boolean)`). `api.h` skips null and false children itself.

`api` has:

| | |
|---|---|
| `h(tag, attrs, ...children)` | builds elements; children are always text, never HTML |
| `refresh(params)` | runs data() again with these params (merged with the current ones), then calls `render()` again with the new data |
| `upload(key, file)` | uploads a `File` (from an `<input type="file">`); resolves to `{ok, error?}`, shows a message itself, and calls `render()` again when it worked |
| `saveSettings(obj)` | saves settings; resolves to `{ok, settings?, error?}`, shows a message itself, and calls `render()` again when it worked |
| `toast(text, isError)` | a short message |
| `download(name, text)` | saves text as a file (CSV) |
| `csvCell(value)` | one CSV cell, quoted when needed |
| `manage`, `settings`, `params`, `report` | who's looking, the settings, the params used, the report's details |

Shared classes in the app's styles, light and dark: `rp-card`, `rp-toolbar`, `rp-btn` (and
`rp-btn primary`), `rp-table-wrap` with `rp-table` (and `num` cells), `rp-kpis` with `rp-kpi`
(`<b>` value, `<span>` label), `rp-field`, `rp-muted`, `rp-noprint`. Use the app's colours:
`var(--accent)`, `var(--text)`, `var(--muted)`, `var(--card)`, `var(--card-2)`, `var(--line)`,
`var(--alert)`. Printing (the browser's Print, or Save as PDF) shows only the report; add an
`@media print` block to `view.css` for more.

## 4. The rules

- **Budget.** Connecteam is only asked through `ctx.ct`, which has its own lane (shared with
  tasks from forms): at most 40 requests a minute and 2,000 a day, none once Staff Access has used
  half its daily share, and never the clock checks' last 10 of a minute. When the minute is full,
  `ctx.ct` waits for room (up to about a minute) rather than failing. Ask for the range the person
  is looking at, not more. Never ask Connecteam (or anything else) directly, and don't fetch
  anything in the background.
- **CSP.** The staff app's strict rules apply: no inline scripts, `onclick=` attributes,
  `style=` attributes, `eval`, or scripts and fonts from other sites. Use `api.h` with `on...`
  handlers in attrs, classes in `view.css`, and the app's own fonts. (`el.style.x = ...` from
  script is fine.)
- **Text is text.** Never use `innerHTML` with anything from data: names and form answers are
  typed by people.
- **Access.** The server checks who may view and manage on every request; the view only hides
  controls (`api.manage`). Don't send data a viewer shouldn't see: data() decides what goes out.
- **Phone first.** It must work at 390 px wide with no sideways scrolling (wide tables scroll
  inside `rp-table-wrap`), in light and dark mode.
- **Wording.** Short, plain English.

## 5. Test it

Copy `tests/test_report_template.py` to `tests/test_report_<key>.py`. It loads the folder as the
add-on does and calls `data()` with a `FakeCtx` whose `ct` is a `StubCt` (both in
`tests/report_helpers.py`): give it timesheet, shift and form rows, uploads and settings, and
check what comes back. `StubCt(unavailable="...")` makes every call raise `Unavailable`. To test
against a fake Connecteam instead, `tests/hr_helpers.py` has `FakeConnecteam` with time
activities, schedules, jobs, forms and submissions (`ct_activity`, `ct_shift`, `ct_form`,
`ct_submission`). `tests/test_report_attendance.py` is a fuller example: a `StubCt` that refuses
some calls, the view's calculations run under Node, and the report opened through the staff
server with the real `ctdata` against `FakeConnecteam`.

```
cd staff_portal && /path/to/venv/bin/python -B -m pytest -q -p no:cacheprovider tests/test_report_<key>.py
node --check app/reports/<key>/view.js
```

Then restart the add-on, turn the report on in the **Reports** tab, pick departments, and open it
in the staff app on a phone-sized window, in light and dark mode.
