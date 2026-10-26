# Staff Portal for Home Assistant

A Home Assistant add-on that gives your staff a branded app, on their phones and at front-desk
computers, showing only the devices their role allows. You manage everything from a
**Staff Access** dashboard in the Home Assistant sidebar. Staff never get a Home Assistant
login.

<p align="center">
  <img src="docs/screenshots/phone-page.png" alt="Staff app on a phone" height="460">
  &nbsp;
  <img src="docs/screenshots/desktop-page.png" alt="Staff app on a front desk computer" height="460">
</p>

Built for properties like hotels, resorts and spas, where maintenance, pool and front-desk staff
need a few controls (tub setpoints, fill valves, lights, door locks) without access to the whole
system.

## Features

### Staff app (phones and computers)

- **Phones:** set up by scanning a QR code, installed to the home screen with your logo and name,
  and signed in until you revoke them.
- **Computers** such as front-desk PCs get a desktop layout:
  - a sidebar with tools (such as Wristbands) at the top, then the pages and keyboard shortcuts
    (1 to 9);
  - compact device rows;
  - card details in a side panel.

  They're set up with a link instead of a QR code.
- **Shared devices:** each person taps or clicks their name and enters their own PIN, so every
  action is recorded under their name, or (per device) just types their PIN with no names
  shown. New shared computers hide names by default.
- **Quick sign-in:** the PIN pad signs in by itself as soon as the PIN is typed, with no OK
  button to press.
- **Idle sign-out** after a time you set, separately for phones (which stay with their owner)
  and computers (which people walk away from).
- **Hold to unlock controls:** controls stay locked until someone presses and holds, and lock
  again after 10 quiet minutes or as soon as the screen turns off. Checking a temperature can't
  switch something by accident.
- **Changes stay put:** with devices that report back slowly (such as minisplits), the new
  setting stays on screen, marked "Updating…", until the device confirms it.
- **Group cards** (such as "Tub 1") show key readings at a glance and open into a screen with all
  of its readings and controls. Named dividers split that screen into sections.
- **Foldable headings:** a folded heading shows how many cards it holds, and turns orange if one
  needs attention.
- **Personal arrangement:** staff can drag their own tabs and cards into the order that suits
  them, without changing anyone else's.
- **Dark and light mode**, following the device.

### Access control

- **Roles** give each device **Hidden**, **View** or **Control**, one by one or a whole area at
  a time, and choose pages. Roles can be duplicated to make variations.
- **Kiosk or role-based devices:** a kiosk device shows what its own role allows. On a
  role-based device each person signs in and gets their own role's access and pages, so one
  shared computer can show different things to Maintenance and Housekeeping.
- **Per-person settings:** on role-based devices, raise or lower access to an area or a single
  device, or choose different pages, for one person without changing their role.
- **Areas:** group devices your way ("Soaking area", "Mechanical"), from whole Home Assistant
  areas and/or single devices.
- **Layout overrides:** an item on a page can raise or lower access for every device that shows
  that page.
- **The add-on enforces all access, not just the screen.** Every action is checked against the
  device's role (or the signed-in person's, on role-based devices) and validated, with sensible
  ranges for setpoints and the entity's own options for dropdowns.
- **Connecteam (optional):**
  - add everyone from your Connecteam user list automatically, with their kiosk code as their
    PIN, and turn people off when they're archived there;
  - **departments** follow your Connecteam Smart Groups and give people their role;
  - only let people sign in while they're clocked in, set per department (Must be clocked in,
    Doesn't need to, or each person's own setting), with a per-person switch that still wins and
    says where each person's rule comes from.
- **Activity log** of every action and every blocked attempt. Actions can also show in Home
  Assistant's logbook, or be credited to a linked Home Assistant user.

### Wristbands and key cards (optional, for TT Hotel locks)

Staff at a front-desk computer make RFID wristbands and key cards on the desk's TT Hotel card
encoder, without opening TT Hotel's own software.

- **Day passes:** enter the guest's name and party size; the wristbands open the doors you
  choose until the end time you set (8 PM by default). People you allow can pick a later time,
  up to 11:59 PM, even after the usual end time.
- **Overnight wristbands:** pick the room and checkout date; they work until your checkout
  time (11 AM by default).
- **Public doors and guest rooms:** mark each door, one at a time or a whole floor or building
  at once. Rooms are picked from the guest rooms and shared doors from the public doors, and
  the add-on refuses a room picked as a shared door (or the other way round).
- **Staff cards:** a single door, whole floors, whole buildings, or every door, until a date.
- **Look up a wristband:** set it on the encoder to see what's on the card, who it was made
  for, by whom and until when, and wipe it with **Revoke**.
- **Card log** of every card made, with who made it, searchable by name or the start of a card
  number.
- **Permissions per role and person:** which kinds of card someone may make, and which doors
  (all, a building, a floor or single doors), down to the longest stay and how long a staff
  card may last.
- **Make key card** from the Staff tab, using the person's department template (doors, how many
  days, deadbolt override), to replace their card or add another. Choose **This computer** to
  make it on the encoder plugged into the computer you're using, with nothing to set up.
- **Key cards list** with **Revoke** (wipe a card that's handed back) and **Report lost**: a lost
  card notice written on a spare card with a door-by-door checklist, a replacement card that
  cancels the old one at each door it's used, or (experimental, gateway doors only, after a
  one-time test) remote lock-out.
- **Door activity** (doors with a TT Hotel gateway): every unlock and refused card as it happens,
  through TT Hotel's Callback URL: when, which door, who and how. Each card shows where it was
  **last used**, and a lost or revoked card tried at a door is flagged in the Activity tab and as
  a Home Assistant notification.
- **Connecting the encoder:** **This computer (USB)**, the default, connects through the
  Wristbands page on the computer the encoder is plugged into, so nothing has to be set up on
  the network. A fixed address, or **Automatic** search with an encoder you confirm, are there
  for other setups. Key cards made from the dashboard can use the encoder on the administrator's
  own computer.
- **TT Hotel requests stay well inside its free plan:** at most one request per batch of cards,
  doors synced every few hours, a monthly cap far below TT Hotel's limit, and requests stop at
  once if TT Hotel says to.

### Admin dashboard (Home Assistant sidebar → Staff Access)

- **Phones & computers:** add, revoke, re-pair, unlock, link to a Home Assistant user, and choose
  which pages or page groups each one shows.
- **Staff:** people with personal PINs for shared devices, each with a role and department, a
  clock-in switch, and **Make key card**; plus the **Key cards** list, with where each card was
  last used.
- **Departments:** linked to Connecteam Smart Groups, each with a role, a clock-in rule and a key
  card template.
- **Roles:** each role's access and pages, with its people listed under it; click a person to
  give them their own settings.
- **Layout editor:** pages, headings, single devices, whole areas and group cards. Drag to
  reorder, duplicate items or whole pages, and set per-item access.
- **Page groups:** bundle pages (such as "Pool staff") and give them to devices together. A
  device also gets pages added to its groups later. Duplicate a whole group with its pages.
- **Preview:** see the layout as a phone or a computer would, unsaved changes included: everything
  on it, or exactly what one device, role or person would see. Tapping a control there sends
  nothing.
- **Wristbands:** the door list (rename or hide doors, and mark them as public doors or guest
  rooms), day-pass and overnight doors, the end and checkout times, staff card defaults, Door
  activity, and every card made.
- **Settings → Connections:** TT Hotel and Connecteam, each folded to one line saying whether it
  works and how many requests Staff Access has used against its limit, with its own Save button.
- **Branding:** property name (shown in the staff app instead of "Staff Portal"), home-screen
  name, brand color and logo.
- **Backup tab:**
  - a full backup and restore, with automatic undo copies. Backups include the activity log and
    the card history, which a restore merges (adding what's missing, removing nothing);
  - **Migrate:** move everything to another install of the add-on (such as another repository)
    with an encrypted file and a one-time code, so phones and computers stay signed in and no
    secrets need typing again;
  - export and import of the layout, roles and staff on their own.

### Security

- Only Home Assistant **administrators** can open the dashboard.
- **Staff devices** hold a random token, which is stored hashed on the server.
- **PINs** are scrambled with scrypt and a salt, and never stored readable.
- **Wrong PINs** lock a person temporarily, or the device after repeated failures, including a
  daily limit that slow, paced guessing can't get around.
- **Speed limits** apply to sign-ins, actions, failed setup codes and error reports, and idle or
  slow connections are closed.
- **Strict security headers:** a Content Security Policy and protection against cross-site
  requests.
- **Optional:** require a PIN on every device, and only allow phones in through your Cloudflare
  Tunnel (with the tunnel's address detected for you).
- **Key cards:** each role and person may only make the cards and doors they're allowed, checked
  by the add-on on every request, and every card is recorded with who made it. TT Hotel's
  encoder key never reaches a browser except the front-desk computer the encoder is plugged
  into (or an administrator's own dashboard making a key card on its computer's encoder), and
  only while it's making or looking up a card. The add-on never sends it to an encoder you
  haven't set up or confirmed.
- **Door activity** arrives at an address with a long random secret that only TT Hotel is given
  (and that you can replace at any time), with limits of its own that wrong guesses can't use
  up; it's never in backups, passcodes are never stored, and text from it never reaches a Home
  Assistant notification as a link.
- **Backup files** include the activity log and card history, but never Home Assistant
  sign-ins, pending setup codes, the Connecteam secret, the TT Hotel secret and password, or the
  Door activity address. A
  restore only keeps phones signed in that this system set up and hasn't revoked, so an old or
  edited backup can't bring back or add a working phone.
- **Migration files** do carry device sign-ins and the secrets, encrypted with AES-256-GCM under a
  key made (scrypt) from a one-time code of 20 letters and numbers. The code is shown once and
  never stored or logged, a file works for 24 hours, and it only restores onto a fresh install.

## Screenshots

| | |
|---|---|
| ![Tub 1 controls on a phone](docs/screenshots/phone-controls.png) | ![Card details in a side panel on a computer](docs/screenshots/desktop-panel.png) |
| Phone: a group card's readings and controls, unlocked | Computer: card details open in a side panel |
| ![Front desk page in light mode](docs/screenshots/desktop-frontdesk-light.png) | ![Shared computer sign-in](docs/screenshots/desktop-signin.png) |
| Computer: a front-desk page in light mode | Shared computer: each person signs in with their PIN |
| ![Phones and computers](docs/screenshots/admin-devices.png) | ![Role editor](docs/screenshots/admin-roles.png) |
| Dashboard: phones and computers | Dashboard: a role's access by area and device |
| ![Layout editor](docs/screenshots/admin-layout.png) | ![Group card editor](docs/screenshots/admin-group-card.png) |
| Dashboard: the layout editor | Dashboard: a group card with dividers and per-device access |
| ![Backup tab](docs/screenshots/admin-backup.png) | |
| Dashboard: backup, restore, export and import | |

The screenshots use demo data.

## Getting started

You need Home Assistant OS (or Supervised) to install it as an add-on. To make the staff app
reachable from outside, a Cloudflare Tunnel (or another reverse proxy with HTTPS) is
recommended.

1. In Home Assistant go to **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, and add
   `https://github.com/liamdayan/ha-staff-portal`.
2. Install **Staff Portal**, start it, and turn on **Show in sidebar**.
3. Point your tunnel at port **8787**, and enter its https address under
   **Staff Access → Settings**.
4. Create a role, add a phone or computer, and scan the QR code or open the setup link.
5. For wristbands and key cards (optional): install TT Hotel's free **SN Web Socket** on the
   front-desk computer the card encoder is plugged into, turn on **Wristbands**, enter your TT
   Hotel integration details under **Settings → Connections**, and give your front-desk role
   wristband permissions.

The full guide is in [staff_portal/DOCS.md](staff_portal/DOCS.md), which is also shown on the
add-on's **Documentation** tab. It covers tunnels, roles, areas, layouts, shared devices, locked
controls, wristbands, linking Home Assistant users, security settings and backups.

## How it works

```
Phones / computers ──https──▶ Cloudflare Tunnel ──▶ Staff Portal :8787 (staff app + API)
                                                         │  checks role, PIN, limits
                                                         ▼
Home Assistant sidebar ──ingress──▶ Staff Access :8099 ──▶ Home Assistant API
```

The add-on runs two small web servers in one process. The staff app talks only to the add-on,
never to Home Assistant directly. The add-on holds the Home Assistant connection, decides what
each device may see and do, and sends allowed actions on. Everything is stored in SQLite (with a
write-ahead log, so a busy shift never waits on the disk) in the add-on's `/data` folder, which
Home Assistant includes in its backups.

## Repository layout

```
repository.yaml              Home Assistant add-on repository manifest
docs/screenshots/            Images used in this README
staff_portal/
  config.yaml                Add-on manifest (ingress for the dashboard, port 8787 for staff)
  Dockerfile
  DOCS.md                    Setup guide (shown in the add-on's Documentation tab)
  CHANGELOG.md               What changed in each version
  app/
    main.py                  Web servers, sign-in, admin and staff APIs, security checks
    permissions.py           What each device type allows, and validation of every action
    layout.py                Pages, group cards, per-item access, per-device arrangement
    transfer.py              Export, import, backup and restore
    migrate.py               Encrypted migration files for moving to another install
    connecteam.py            Connecteam users and time clock (OAuth 2.0)
    db.py                    SQLite storage
    doorlog.py               Door activity: TT Hotel's lock records from its Callback URL
    checkpoint.py            Run just before Home Assistant backs the add-on up (backup_pre)
    ha.py                    Home Assistant REST and WebSocket client
    branding.py              Logo handling and home-screen icons
    admin/                   Admin dashboard (plain HTML, JS and CSS; no build step)
    staff/                   Staff app (plain HTML, JS and CSS; no build step)
```

## Local development

```sh
pip install -r staff_portal/requirements.txt
cd staff_portal/app
DATA_DIR=./.dev-data HA_URL=http://homeassistant.local:8123 HA_TOKEN=<long-lived token> \
  ADMIN_ALLOWED_IPS=127.0.0.1 python3 main.py
```

Admin dashboard: http://127.0.0.1:8099 · Staff app: http://127.0.0.1:8787

Outside Home Assistant OS, the dashboard only accepts connections from the addresses in
`ADMIN_ALLOWED_IPS`, so keep it behind something you trust. See "Running without Home Assistant
OS" in [DOCS.md](staff_portal/DOCS.md).

## Versions

The add-on is still under 1.0 while features settle. Each release is listed in
[CHANGELOG.md](staff_portal/CHANGELOG.md), which Home Assistant also shows on the add-on page.

## License

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 Hot Lake Springs Resort.
