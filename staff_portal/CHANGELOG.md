# Changelog

## 0.6.16

Connecteam: made sure Staff Access stays well inside Connecteam's API limits, which apply per
account and are shared with your other integrations.

- **Fixed:** it checked every minute even when no one needed it, and asked for the list of time
  clocks on every check: about 2,900 requests a day or more. Now:
  - One check covers everyone, **once an hour** (new setting), and only while someone must be
    clocked in. With one time clock that's 24 requests a day, plus one when someone the last
    check didn't find clocked in signs in or uses a control.
  - Signing in or using a control costs nothing when the last check found that person clocked
    in. Otherwise it checks right away (updating everyone), and that check replaces the next
    hourly one if it's within 20 minutes of it.
  - Reading never checks. Clocked-in-only people must be on the clock to sign in and to use
    controls. They're no longer signed out while only reading.
  - The time clock list is reused for 6 hours and the user list for 5 minutes. With many time
    clocks, checks slow down automatically to stay under 15% of your daily limit.
- Hard limits of its own: 20 requests a minute and a fifth of your account's daily limit,
  counted across restarts.
- Reads Connecteam's rate-limit headers and pauses when your account is running low on
  requests from any integration. A "too many requests" reply makes it wait as long as Connecteam
  asks, with exponential backoff.
- Rejected keys or missing scopes stop automatic requests until the settings are saved again or
  **Test connection** is clicked, instead of retrying every minute.
- Extra checks happen at most every 30 seconds (was 10), and less often while Connecteam is
  failing. **Test connection** can be clicked at most every 10 seconds.
- Settings shows today's request count, your account's remaining requests, and why checks are
  paused or stopped.
- New **Account hosted in** setting for Australian Connecteam accounts (api-au.connecteam.com).
- Requests identify themselves as Staff Portal.

Dashboard

- **Fixed:** pressing **Enter** in a dialog (such as **Add staff member**) quietly cancelled it
  instead of saving, so nothing was added and no message appeared. Enter now saves, and shows
  what's missing if something is.
- **Fixed:** some error messages from the add-on showed only as "Request failed (400)". The
  actual reason now shows.
- The layout editor says **Shown on: all devices** and **Choose devices**, since pages can be
  on computers as well as phones.

## 0.6.15

- **Connecteam** (optional, **Settings → Connecteam**):
  - Paste the Client ID and Secret of a Connecteam OAuth 2.0 app (scopes users.read and
    time_clock.read), then use **Test connection**.
  - **Staff → Add from Connecteam** lists your Connecteam users. Picking someone fills in their
    name and uses their Connecteam kiosk code as their PIN. The code goes straight from Connecteam
    to the add-on and is never shown in the dashboard. Without a usable kiosk code, type a PIN.
  - **Only allow sign-in while clocked in** is on by default for people added from Connecteam and
    can be turned off per person. They can't sign in while clocked out, and are signed out
    within about a minute of clocking out. The sign-in list shows **Not clocked in**.
  - Clock status is checked every minute, and again right away when someone who looks clocked
    out enters their PIN. If Connecteam can't be reached, the last status is kept for 15 minutes.
  - The Staff tab shows each linked person's clock status. **Edit** can turn clocked-in-only
    on or off, switch their PIN to their current kiosk code, or unlink them.
  - Staff added by hand work as before.
- The Client Secret is never sent back to the dashboard and isn't included in backups. Backups
  and staff exports keep each person's Connecteam link.

## 0.6.14

- **Fixed:** on systems with many entities, Staff Access couldn't read Home Assistant's entity
  registry ("Message size … exceeds limit" in the log). Until it could, whole-area access also
  included the configuration and hidden controls it should leave out, such as "Restart
  controller" or "Child lock". It now reads Home Assistant's compact entity list (much smaller),
  and falls back to the full list with a much higher size limit.

## 0.6.13

Fixes from a bug and security review.

**Security**

- **Fixed:** the wrong-PIN limit could be dodged by sending many PIN guesses at the same instant;
  they counted as one wrong attempt, so the phone never locked. On shared devices this also
  dodged the limit on guessing across names. Every guess now counts.
- **Fixed:** after someone signed out of a shared computer, or a phone locked, resizing the
  window or pressing Back could briefly show the previous person's dashboard behind the
  sign-in screen.
- **Fixed:** restoring a backup that no longer included a phone or person linked to a Home
  Assistant user left that sign-in valid in Home Assistant. It's now revoked during the restore.

**Phones and computers**

- **Fixed:** signing out or locking from the Rearrange screen left the next person stuck on a
  loading spinner after entering their PIN.
- **Fixed:** an update arriving just as Rearrange opened could replace it with the dashboard,
  after which readings stopped updating and the lock button disappeared.
- **Fixed:** switching a device to shared sign-in while it was open showed a PIN pad that never
  worked; it now shows the list of names.
- **Fixed:** on computers, **Sign out** and the page list couldn't be clicked while a card's side
  panel was open.
- When Home Assistant is slow, a command that worked no longer shows an error (which led to it
  being pressed twice); the logbook entry is written in the background.

**Dashboard and backups**

- **Fixed:** having names that differ only in capitals (such as "Bob" and "bob") made every
  backup impossible to restore. Those backups now restore, and new names that differ from an
  existing one only in capitals are refused for roles, areas and staff.
- **Fixed:** importing staff or roles with long names using **Keep both** could fail partway
  and leave a half-finished import. Names are now shortened correctly, and an import is
  all-or-nothing.
- **Fixed:** a backup with an unusual date in it restored, but then showed an error and wasn't
  recorded in the activity log.
- **Fixed:** after importing roles with **Replace them**, the role open in the editor kept
  showing the old access, and saving it put that back. It now reloads.

**Add-on**

- Timeouts talking to Home Assistant are handled as "can't reach the system" instead of
  causing server errors.
- The add-on stops promptly when restarted or updated, instead of waiting about 10 seconds and
  being force-stopped.

## 0.6.12

- Shared computers say "Click your name, then type your PIN."
- The dashboard's subtitle mentions computers as well as phones.
- README rewritten with every current feature and screenshots of the phone app, the computer
  layout and the dashboard.

## 0.6.11

- Computers get a real desktop layout instead of a stretched phone one:
  - A full-height sidebar with your logo, the pages, who's signed in with **Sign out**, and the
    control lock.
  - A page header showing when readings last updated.
  - Compact device rows, with the control on the same line.
  - Group card details open in a side panel over the page.
  - Smaller, desktop-sized type and hover states.
- Keyboard shortcuts on computers: **1** to **9** switch pages, **Esc** closes a card's panel.
- Tablets keep the touch-friendly layout.

## 0.6.10

- Headings on phone and computer pages fold away everything under them (up to the next heading)
  with a tap. Each phone or computer remembers what's folded.
- A folded heading shows how many cards it holds, and turns orange if anything under it needs
  attention.
- **Start collapsed** option for headings and whole-area items in the Layout tab. Area sections in
  the automatic layout fold too.

## 0.6.9

- **Computers** (such as front desk PCs) can use the staff app in a browser with the same limited
  access as phones. **Add computer** on the **Phones & computers** tab gives a setup link to copy
  instead of a QR code; computers skip the "add to home screen" step.
- Desktop layout for wide screens: pages in a sidebar, more tiles per row, group card readings
  beside their controls, and the control lock at the bottom of the sidebar.
- PINs can be typed on a keyboard (digits, Backspace, Enter).
- Messages on the staff app say "computer" or "phone" to match the device.

## 0.6.8

- New **Backup** tab:
  - **Full backup and restore** of everything in Staff Access. Phones stay paired after a
    restore, and the setup being replaced is saved first so a restore can be undone.
  - **Export and import** for the layout, roles and staff on their own. Areas and phones are
    matched by name, and you choose whether existing names are replaced, kept alongside, or skipped.
- Backup and export files never include Home Assistant sign-ins or pending setup codes; PINs are
  only included scrambled. Every file is checked before anything changes.

## 0.6.7

- Redesigned admin dashboard to match the phone app: frosted sticky tab bar, rounded cards,
  softer fields and buttons, pill-shaped access pickers, and your brand color throughout.
- **Duplicate** for layout items (the copy opens for editing, ready to become "Tub 2") and
  **Duplicate page** for whole pages.

## 0.6.6

- Group cards can have named dividers (**+ Add divider** in the card editor) to split the card's
  phone screen into sections, each with its own readings and controls. Sections with nothing
  the phone can see are hidden.

## 0.6.5

- Redesigned phone app: frosted-glass header, large page titles, a floating tab bar, and rounded
  tiles with an icon for each device that lights up when it's on.
- Switches and lights toggle by tapping anywhere on their tile. Small tiles sit two to a row;
  thermostats, setpoints, valves and dropdowns get a full-width tile.
- Setpoints use a single pill-shaped − / + control; Open / Close buttons show which state the
  valve is in.
- Group cards show their values as large numbers with labels underneath.
- Readings show their value large on their tile.
- The phone's status bar matches the app background instead of the brand color.

## 0.6.4

- Group card screens show each device once. Devices the phone can operate appear only under
  Controls (showing their state), not also in the readings list.
- Controls on phones are locked until staff press and hold **Hold to unlock controls** at the
  bottom of the screen, and lock again after 10 minutes with no control used. Readings always
  work. Change the time or turn it off in **Settings → Staff phones**.

## 0.6.3

- **Fixed:** "Only allow phones through your Cloudflare Tunnel" blocked every phone when cloudflared
  runs outside Home Assistant (another computer, VM or Docker host). New **Tunnel address** setting
  under **Settings → Phone security** for the address cloudflared connects from.
- Settings shows where Cloudflare traffic has come from recently, with a **Use this address**
  button.
- Tunnel-only can't be turned on until Staff Access has seen tunnel traffic from an allowed
  address, so it can't lock every phone out.
- **Fixed:** with cloudflared on another computer, all phones looked like one address, so one
  phone's failed attempts could rate-limit every phone. Setting the tunnel address fixes this.

## 0.6.2

- Duplicate a role (**Roles → Duplicate**) to make a variation without changing other phones.
- Layout items (devices, group cards, whole areas) can override access: **Use role**, **Hidden**,
  **View** or **Control** on every phone that shows the page. Devices inside a group card can each
  have their own override.

## 0.6.1

Security hardening:

- Only Home Assistant administrators can open Staff Access.
- Dashboard protected against cross-site requests and injected scripts.
- Wrong PINs lock a person for 15 minutes instead of ending their sessions elsewhere. Guessing
  across several names still locks the phone.
- Phones with a PIN ask for it again after the idle time, even with the app left open.
- Speed limits on phone actions: 60 a minute per phone, one per second per device.
- Giving a role a whole area no longer includes hidden or configuration controls.
- New settings: **Require a PIN on every phone**, and **Only allow phones through your
  Cloudflare Tunnel**.
- Logo uploads, the activity log and error reports are size-limited.
- Updated aiohttp, Pillow and segno.

## 0.6.0

- Staff can rearrange their own phone (**⇅ Rearrange**). Per phone, or per person on shared
  phones. Can be turned off in Settings.

## 0.5.7

- More reliable drag to reorder.

## 0.5.6

- Drag the **≡** grip to reorder pages, items, group card devices and areas.
- Fixes to choosing pages per phone.

## 0.5.5

- Choose which layout pages each phone shows.

## 0.5.4

- Fixed areas sometimes being empty after Home Assistant restarts.
- Dashboard loads reliably inside Home Assistant.

## 0.5.3

- Dashboard errors show in a panel and in the add-on log.

## 0.5.2

- Fixed browsers using an old copy of the dashboard after an update.

## 0.5.1

- Fixed **Add page** and other buttons that relied on browser pop-ups Home Assistant blocks.

## 0.5.0

- Phone layout editor: pages, headings, devices, whole areas and group cards (such as "Tub 1")
  with a detail screen.
- Setpoints (numbers) and dropdowns (selects) can be controlled.

## 0.4.2

- Access buttons line up in the role editor.

## 0.4.1

- Repository moved to github.com/liamdayan/ha-staff-portal.

## 0.4.0

- Areas: group devices your own way and give roles access by area.

## 0.3.1

- The role editor fits the screen.

## 0.3.0

- Staff members with their own PINs for shared phones.

## 0.2.0

- Staff actions show in Home Assistant's logbook.
- Link a phone to a Home Assistant user.

## 0.1.0

- First release: branded staff web app, roles, phone setup by QR code, activity log.
