# Staff Portal

A branded, locked-down web app for staff phones and front desk computers. You decide which
devices each staff role can **see** and which they can **control**, from a dashboard in your
Home Assistant sidebar.

- Staff never get a Home Assistant login. Their phones and computers talk only to this add-on,
  and the add-on refuses anything their role doesn't allow, even if someone pokes at it directly.
- Phones are set up by scanning a QR code once, computers by opening a link once. Both stay
  signed in until you revoke them.
- Optional PIN per device, or per person on shared devices (or required, see
  [Security settings](#security-settings)). Too many wrong PINs locks it until you unlock it.
- Every action (and every blocked attempt) is recorded in the Activity tab.

## 1. Install

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**, open the **⋮** menu, choose
   **Repositories**, and add this repository's GitHub URL.
2. Find **Staff Portal** in the store, click **Install**, then **Start**.
3. Turn on **Show in sidebar**. A **Staff Access** entry appears in the sidebar. Only Home
   Assistant administrators can open it.

## 2. Make the staff app reachable through Cloudflare Tunnel

The staff app listens on port **8787**. Add a public hostname for it in your tunnel:

**If you use the Cloudflared add-on**, add to its configuration:

```yaml
additional_hosts:
  - hostname: staff.yourdomain.com
    service: http://172.30.32.1:8787
```

(`172.30.32.1` is the Home Assistant host as seen from other add-ons. Your HA box's local IP
address, such as `http://192.168.1.50:8787`, also works.)

**If you manage the tunnel in the Cloudflare dashboard**, go to **Zero Trust → Networks →
Tunnels → your tunnel → Public Hostname → Add**, and set the service to
`HTTP` and `<your HA IP>:8787`.

Then open **Staff Access → Settings** and enter `https://staff.yourdomain.com` as the
**staff app address**. Setup QR codes use this address.

If `cloudflared` runs outside Home Assistant (another computer, VM or Docker host), also set
**Settings → Phone security → Tunnel address** to the IP address it connects from (see
[Security settings](#security-settings)). Without it, Staff Access sees every phone as that one
computer, so one phone's wrong setup codes can briefly block the others.

> Don't put a Cloudflare Access login page in front of the staff hostname. The app does its
> own sign-in, and an Access page would make staff log in again every few days.

## 3. Brand it

In **Settings**, set your property name, the short name shown under the home-screen icon, your
brand color, and upload a logo (a square PNG works best). Phones pick up the name and colors
right away. iOS caches the home-screen icon itself, so a new logo shows up on phones that are
set up after the change, or after re-pairing.

## Areas (optional)

Areas let you group devices the way your staff think about the property, such as
"Soaking Area" or "Mechanical", and give a role access to a whole area at once.

In **Settings → Areas**, click **Add area**, name it, and choose what's in it:

- **Whole Home Assistant areas.** Everything in them is included, and devices you add to
  that Home Assistant area later join automatically.
- **Individual devices.** Search and tick any device, even one from a different area.

A device can be in more than one area. The staff app lists devices under these headings in
the order shown in Settings (drag the ≡ grip to reorder). A device shows under its first area;
devices in no area show under their Home Assistant area.

## 4. Create roles

In **Roles**, create a role such as *Maintenance*. A role is used in two ways: a **kiosk** phone or
computer shows what its own role allows, and on a **role-based** one each person who signs in
sees what *their* role allows (see [Role-based devices](#role-based-devices-each-persons-own-access)).

Every device in Home Assistant is listed under **Individual devices**, grouped by area. The list
is folded when you open a role: click **Individual devices** to open it (its line says how many
devices are set). For each one choose:

| Setting | What staff get |
|---|---|
| **Hidden** | Nothing. The phone can't see or use it. |
| **View** | Status only (for example, "Locked," "Dry," "61.4 °F"). |
| **Control** | Status plus the controls for it. |

Sensors can only be set to View.

If you've set up areas, the role also has an **Areas** section at the top. Setting an area to
View or Control covers every device in it, including devices added to the area later. Each
device gets whichever is higher: its area's setting or its own setting, and the device list
shows when access comes from an area ("Control via Soaking Area").

**Pages** decides which layout pages (and page groups) people with this role see when they sign
in on a role-based device. **All pages** includes pages you add later. Kiosk devices don't use
this: each one chooses its own pages.

Click **Save role**. Kiosk devices with that role, and people with it on role-based devices,
update within a few seconds.

**Duplicate** makes a copy of the role under a new name, with the same device, area and page
settings, so you can make a variation (say *Pool tech, nights*) without changing anyone else's
phone. No phones or people move to the copy until you choose it for them (**Phones & computers →
Edit**, or a person's **Role** on the Staff tab). If you've made changes you haven't saved yet,
the copy gets them and the original stays as it was last saved.

What **Control** allows depends on the device type:

| Type | Staff can |
|---|---|
| Lights | On/off, brightness |
| Switches, fans, input booleans | On/off |
| Thermostats | Set temperature (within the thermostat's own limits), change mode |
| Water heaters | Set temperature (within its limits), change operation mode |
| Locks | Lock/unlock (unlock asks for confirmation) |
| Covers, valves | Open/close/stop |
| Scripts, scenes, buttons | Run/press |
| Vacuums | Start/pause/dock |
| Numbers (setpoints) | Set a value within the entity's min and max |
| Dropdowns (select) | Choose one of the entity's options |

## Phone layout (optional)

By default the phone lists every allowed device grouped by area. In **Layout** you can design
it yourself instead:

- **Pages.** Click **Add page** (for example "Soaking" and "Mechanical"). With more than one
  page, the phone shows tabs along the bottom.
- **Group cards.** For something like "Tub 1", add a group card, pick every device that
  belongs to it (water, inlet and outlet temperatures, setpoint, fill valve, pump, mode…),
  give each a short label, and tick up to 4 to show **on the card**. Tapping the card opens a
  screen with the key readings at the top, then the readings, then the controls. Each device
  appears once: if the phone can operate it, it shows as a control (with its current state),
  otherwise in the readings list.
  To split a big card into sections (say "Cold plunge" and "Fills"), click **+ Add divider**
  under the device list, name it, and drag it by its **≡** grip to where that section starts.
  On the phone, each section gets its name as a heading with its own readings and controls
  underneath. A section whose devices the phone can't see is left out.
- **Device.** A single device tile, with an optional label.
- **Whole area.** Every device in one of your areas, kept up to date automatically.
- **Heading.** A section title such as "Tubs". On phones and computers, tapping a heading folds
  away everything under it, up to the next heading, so long pages stay manageable. Each phone or
  computer remembers what was folded. A folded heading shows how many cards it holds, and turns
  orange with "1 needs attention" if something under it has a problem. Tick **Start collapsed**
  on a heading (or a whole-area item) to have it start folded.

**Duplicate** makes a copy of an item (it appears right below and opens so you can rename it and
swap its devices, handy for turning "Tub 1" into "Tub 2"). **Duplicate page** copies a whole page
with everything on it. A copied page shows on every device set to **All pages**; devices set to
specific pages need it added with **Choose devices** (or put it in a page group they show).

Reorder pages, page groups, page items and the devices in a group card by dragging their **≡**
grip (with a mouse or a finger), or click the grip and use the arrow keys. Then click
**Save layout**.

**Which devices show which pages.** Every phone and computer shows every page by default,
including pages you add later. To change that for one device, open **Phones & computers → Edit**
and use **Pages on this phone** (or computer): untick **All pages** and pick the pages and page
groups it should show (including the **More** page). You can also do it from the Layout tab:
each page and page group shows **Shown on: …** with a **Choose devices** button. Device cards
show their pages as **Pages: …**. A device set to specific pages won't pick up new pages
automatically, except pages added to a group it shows; tick **All pages** again for that.

On a **role-based** device, pages come from each person's role (or their own pages) instead.
Once you have one, **Shown on** and **Choose devices** also list roles.

By default the layout only decides how things are arranged, and **roles decide what each phone
can see and do**. A group card only shows the devices that phone's role allows (and disappears if
it allows none of them), and controls appear only for devices the role has set to **Control**.

**Access on a layout item.** Device, group card and whole-area items have an **Access on phones**
choice when you edit them:

| Choice | What happens |
|---|---|
| **Use role** (default) | Each phone gets whatever its role allows. |
| **Hidden** | Hidden on every phone that shows this page, even if the role allows it. |
| **View** | Status only on every phone that shows this page, even if the role has it Hidden or Control. |
| **Control** | Status and controls on every phone that shows this page, even if the role doesn't include it. Sensors stay View. |

In a group card, each device also has its own choice (**Same as card**, Hidden, View or Control),
which wins over the card's. For example: set the Tub 1 card to **View** and just its setpoint to
**Control**.

Things to know:

- An override applies on **every phone that shows that page**, whatever its role. To give only
  some phones the extra access, put the item on a page that only those phones show
  (**Choose devices**), or use a role instead.
- The override is for that device on the whole phone, not just that spot. A device set to
  **Hidden** doesn't show up anywhere on those phones, including the More page.
- If a device has different overrides in more than one place, the higher one wins.
- Overrides are enforced by the add-on, the same as roles, not just hidden from view.
- Items with an override say so in the Layout tab ("Access: Control on every phone").

With **Put devices… on a "More" page** ticked, anything a role allows that isn't placed on a
page still shows up on a "More" page. Delete every page to go back to the automatic layout.

Setpoints (number entities) and dropdowns (select entities) can be controlled too, always
within the entity's own minimum, maximum and options.

### Page groups

A page group bundles pages that go together, such as "Pool staff" (Tubs, Spa, Chemicals) or
"Front desk" (Lobby, Doors), so you can give them to phones and computers in one go instead of
page by page.

- **Add group** creates one. To fill it, open the group and use **Move a page here…** or
  **New page**, or open a page and pick its **Group**. A group's pages stay together in the page
  list, under the group. Drag the group by its grip to move all its pages at once.
- **Choose devices** on the group picks which phones and computers show it. A page in a group
  doesn't have its own **Choose devices**: it's shown wherever its group is. **Take out** (in the
  group) makes it an ordinary page again, so devices that only showed it through the group stop
  showing it.
- A device that shows a group also gets pages you add to the group later.
- **Duplicate group** copies the group with every page in it, right below the original, ready to
  rename ("Pool staff" into "Spa staff", say) and give to other devices. The copy isn't on any
  device yet, except devices set to **All pages**.
- **Delete group** asks what to do with its pages: **Keep its pages** leaves them in the layout,
  outside any group, and devices that showed the group keep showing them. **Delete its pages
  too** removes them.
- Staff don't see groups: a group's pages show as ordinary tabs (or sidebar pages on a computer),
  in layout order.

A layout can have up to 12 groups. Group changes take effect when you click **Save layout**,
except **Choose devices**, which is saved straight away.

### Preview

**Preview**, at the top of the Layout tab, shows the layout the way staff will see it, including
changes you haven't saved yet. Try things out, then **Save layout** or **Discard changes**.

- **Phone** shows it at phone size, with pages as tabs along the bottom. **Desktop** shows the
  computer layout, with the sidebar.
- **As** picks whose view to show. **Everything on the layout** shows every page with everything
  on it, as a device allowed to control all of it would. Pick a kiosk phone or computer to see
  exactly what it would show: its pages, its role's access and your access settings on items.
  For role-based devices, pick a **role** (what someone with it sees, before anyone's own
  settings) or a **person** (their role plus their own saved settings).
- Readings are live, but nothing in the preview changes anything: tapping a control just says
  "Preview: nothing was sent to your equipment." Signing in and rearranging aren't part of the
  preview.

## Locked controls on the phone

So someone checking temperatures can't switch something by accident, controls on the phone are
locked until they're unlocked on purpose. Readings always work.

- While locked, switches, buttons, setpoints and dropdowns are dimmed and ignore taps. Tapping
  one shows a reminder.
- To unlock, **press and hold** the 🔒 **Hold to unlock controls** button at the bottom of the
  screen for about a second. A quick tap doesn't unlock.
- Controls lock again after 10 minutes with no control used (each use restarts the 10 minutes),
  when someone taps **Lock now**, or when the phone asks for a PIN or someone signs out.
- They also lock straight away when the screen turns off or the app goes to the background
  (switching to another app, or another browser tab on a computer), so a phone put in a pocket
  comes back locked.
- The lock button only shows on screens that have controls.
- Change the time, or turn this off, under **Settings → Staff phones → Lock controls until staff
  unlock them**. Phones pick up the change within a few seconds.

This guards against slips, not people: what a phone may do still comes from its role.

**Devices that report changes late.** Some devices, such as minisplit heat pumps, only tell Home
Assistant about a new setting at their next check-in, seconds later. Until they do, the phone keeps
showing the setting staff chose (temperature, mode, level or on/off), marked **Updating…**, and
checks more often until the device confirms. If the device still hasn't confirmed after 90
seconds, the phone shows the device's actual setting again and says the change wasn't confirmed.

## Rearranging on the phone

Staff can tap **⇅ Rearrange** at the bottom of the phone screen to drag their tabs and the items
on the current page into the order that suits them (or, with no custom layout, the order of the
area sections). It only changes that phone, or that person on a shared phone; your Layout tab
and every other phone stay as they are. Items you add later appear at the end.

- Turn it off under **Settings → Staff phones**. Every phone then goes back to your layout
  (their own order is kept, and comes back if you turn it on again).
- To put one phone back to your layout, open **Phones → Edit** and click **Reset to your
  layout**. For a person on a shared phone, do the same on the **Staff** tab. Phones that have
  been rearranged say so on their card.
- Staff can also tap **Reset to default** on the Rearrange screen.

## 5. Add phones

1. In **Phones & computers**, click **Add phone**, name it (such as *Maintenance phone*), leave
   **Mode** on **Kiosk**, pick a role, and optionally set a 4- to 8-digit PIN. (For a phone that
   several people share, each with their own access, choose **Role-based**: see
   [Role-based devices](#role-based-devices-each-persons-own-access).)
2. A QR code appears. On the phone, open the **Camera**, scan it, and tap the link.
3. Follow the on-screen prompt to **Add to Home Screen**, then open the app from the new icon.

Each QR code works once and expires (72 hours by default, changeable in Settings).

**Managing phones:**

- **Revoke** signs the phone out immediately, such as for a lost phone or a departing employee.
- **Re-pair** signs the phone out and makes a new QR code, for example after resetting a phone.
- **Unlock** clears a lock after too many wrong PINs.
- Changing a phone's role takes effect within a few seconds.

## Computers (front desk)

Front desk computers can use the same staff app in a web browser, with the same limited access as
phones: a role, layout pages, PINs, per-person sign-in and locked controls all work the same way.

1. In **Phones & computers**, click **Add computer**, name it (such as *Front desk computer*) and
   pick a role. For a computer several people use, set **Who uses it** to **Shared** so each
   person signs in with their own PIN (they can type it on the keyboard). A new Shared or
   Role-based computer starts with **Hide names** ticked (see below), so the screen doesn't list
   everyone; untick it to show names.
2. A setup link appears instead of a QR code. Click **Copy link** and open it in the browser on
   that computer (Chrome, Edge, Safari or Firefox). Whoever sets it up first gets access, so only
   open it on the computer you're setting up.
3. Click **Set up this computer** on the page that opens. It waits for that click, so an email
   security scanner that opens links can't use up the one-time link. In Chrome or Edge, use
   **Install app** from the browser's menu to give it its own window and icon, or just bookmark
   it. It stays signed in in that browser until you revoke it.

On a computer the staff app works like a desktop app rather than a big phone:

- A sidebar lists the pages (press **1** to **9** to jump between them), shows who's signed in
  with **Sign out**, and holds the control lock button.
- Devices are compact rows (name, state and the control on one line), several to a row.
- Clicking a group card opens its details in a panel on the right, with the page still visible
  behind it. **Esc** or clicking outside closes it.
- The page header shows when the readings were last updated, and turns orange if the connection
  drops.

A computer's browser window narrower than a tablet falls back to the phone layout.
To give the front desk its own page (lobby lights, say), add a page in **Layout** and use
**Choose devices** to show it only on the front desk computer.

## Shared phones: each person signs in

If several people share one phone (a maintenance phone passed between shifts, say), you can have
each person sign in with their own PIN so every action is recorded under their name.

1. In **Staff**, click **Add staff member** for each person, with a name and a 4- to 8-digit PIN.

   The Staff tab lists one thin row per person: name, role, departments, anything that needs a look
   (inactive, locked, not clocked in) and when they last signed in. Click a row for everything
   else and its buttons. Search by name, role or department, and sort by **Department** (each
   department folds away with a click; someone in several shows under each), **Name** or **Role**;
   this browser remembers the sort and the folded departments. People you've archived are folded away under the list (see
   [Archiving people](#archiving-people)).
2. In **Phones**, click **Edit** on the shared phone and set **Who uses it** to
   **Shared: each person signs in with their own PIN**. Leave **Everyone on the Staff tab**
   ticked, or untick it and pick who may use that phone.

On the phone, the app now asks **"Who's using this phone?"**. The person taps their name,
enters their PIN, and the header shows who's signed in. **Sign out** returns to the name list,
and they're signed out automatically after the idle time set in **Settings** (phones and
computers each have their own, see [Security settings](#security-settings)).

**Signing in without OK.** When every PIN that can sign in on a device has the same number of
digits (everyone allowed on a shared device, or the device's own PIN), the PIN pad signs in as
soon as the last digit is typed. Otherwise it signs in once the typing stops for a moment, with
at least 4 digits typed: half a second to start with, set in **Settings → Security** as **Sign in
after the last PIN digit (ms)** (200 to 5,000). Too short, and someone who types slowly is signed
in before they've finished, as a wrong PIN. **OK** (or **Enter** on a keyboard) always works too. The
device is only ever told that shared number of digits: never a PIN, and never one person's own
length. Staff Access can only know a PIN's length from when it's set (they're stored scrambled),
so PINs set before 0.8.3, and PINs from older backups or staff files, count as unknown until
they're set again, and devices with any of them wait for the pause. For people linked to
Connecteam whose PIN is their kiosk code, the staff sync fills in the length by itself.

- What they can control still comes from the **phone's role** (it's a kiosk device). To give
  each person their own access instead, make it role-based (next section).
- The Activity tab shows entries like "Bob on Maintenance phone", and Home Assistant logbook
  entries read "set to 140° by Bob (Maintenance phone) via Staff Portal".
- Too many wrong PINs locks that **person** for 15 minutes (or until you unlock them on the
  Staff tab). The lock only stops new sign-ins; if they're already signed in on another phone,
  that stays signed in. Wrong PINs across several names lock the **phone** until you unlock it,
  so PINs can't be guessed by switching names.
- However slowly they come, **20 wrong PINs on one shared phone or computer within 24 hours lock
  it** until you unlock it on the **Phones & computers** tab (the activity log says why). This stops
  someone with the device guessing a few PINs at a time, forever.
- Turning a person off (**Edit → Can sign in**) or changing their PIN signs them out right away.
  For someone who has left, **Archive** them instead (see [Archiving people](#archiving-people)).
- Everyone needs their own PIN: a PIN someone else already uses is refused.

**Hide names (PIN only).** Tick **Hide names: people just type their PIN** under **Who uses it**
to skip the list of names. The device shows only a PIN pad, and the PIN says who's signing in.
The device never receives the list of names. Because a wrong guess could match anyone, wrong
PINs count against the device, which locks after **Lock phone after wrong PINs** wrong PINs in
a row (within 15 minutes), or 20 in a day, until you unlock it on the **Phones & computers** tab.
If two people still share a PIN from before, that PIN can't sign in; give one of them a new PIN.
New Shared and Role-based **computers** start with this ticked, since a front desk screen is in
view of guests; new phones start with names shown. Devices you already have keep their setting.

## Role-based devices: each person's own access

On a **role-based** phone or computer everyone signs in with their own PIN, as on a shared kiosk
device, but then sees what **their own role** allows, on **their role's pages**. So one front desk
computer can show the boiler controls to Maintenance and only the cabin locks to Housekeeping.

| | Kiosk (the default) | Role-based |
|---|---|---|
| Access comes from | the device's role | the signed-in person's role, plus their own settings |
| Pages come from | the device (**Pages on this phone**) | the person's role, or their own pages |
| Who signs in | one person or team (optional PIN), or each person with their own PIN | each person, with their own PIN |

To set one up:

1. **Give people roles.** On the **Staff** tab, open each person's row and pick their **Role** (or in
   **Edit**). Roles work as staff groups: each person has one.
2. **Choose each role's pages.** On the **Roles** tab, open the role and use **Pages**. (On the
   **Layout** tab, **Choose devices** on a page or group also lists roles once you have a
   role-based device.)
3. **Make the device role-based.** In **Phones & computers → Edit**, set **Mode** to
   **Role-based**, choose who may sign in, and tick **Hide names** if you want only a PIN pad.
   Changing the mode signs everyone out of that device.

**One person's own settings.** On the **Roles** tab, people are listed under their role (and under
**People without a role**). Click someone, or **Access** on their Staff card, to change things just
for them:

- **Role**: which role they have.
- **Pages**: **Same as their role**, or untick it and choose their own.
- **Areas** and **Individual devices**: **Role** (the default: whatever their role gives), or
  **Hidden**, **View** or **Control** just for them. Each device says what they get and why
  ("Control from their Soaking setting", "Their role gives Control").
- **Clear their settings** puts everything back to their role's. Click **Save** to keep changes.

What someone gets, from least to most specific (the most specific wins):

1. their role (a device's own setting or its area's, whichever is higher);
2. items' own **Access on phones** on the layout pages they see;
3. their own setting for an area the device is in (the higher one, if it's in several);
4. their own setting for that device.

Good to know:

- A person's role and own settings only count on role-based devices. On a kiosk device the
  device's role decides, whoever signs in.
- Someone with no role sees only what their own settings give them. Deleting a role leaves its
  people with no role.
- The header shows who's signed in and their role ("Bob · Maintenance"), and the activity log
  records their role.
- Everything else about signing in works as on shared kiosk devices: hiding names, who may sign
  in, wrong-PIN locks, signing out after the idle time, and Connecteam's clocked-in check.
- **Preview** on the Layout tab can show a role or a person: pick them under **As**.

## Archiving people

When someone leaves, open their row on the **Staff** tab and click **Archive**. Their own access
ends at once, but nothing of theirs is deleted: their activity, the cards they made and their
settings stay, and you can bring them back.

The confirmation lists what stops working, and what archiving can't reach (their key cards, any
Kiosk-mode phone or computer they use, and their Home Assistant user):

- They're signed out on every phone and computer, and their PIN no longer signs them in anywhere
  (by name, PIN only or role-based). They're left out of every name list, and out of the people
  you can pick for a phone or computer.
- They can't make wristbands, contractor badges or key cards, whatever their role or their own
  wristband rules allow (a card job of theirs that's still running stops), and no key card can
  be made for them: not with **Make key card**, not as a replacement for a lost card, and not in
  the staff app (a staff card for their exact name is refused). A key card for them that was
  still being made (by anyone, from the dashboard or the staff app) stops too, and can't be tried
  again; revoking one of their cards, or a lost card notice, carries on. Wristbands and contractor
  badges they made before keep working until they end.
- Staff Access keeps their Home Assistant user link but never uses it. The Home Assistant user
  itself can still sign in to Home Assistant: turn it off there, under **Settings → People**.
- The Connecteam sync leaves them archived, even if they're still active in Connecteam (see
  [Add everyone from Connecteam](#add-everyone-from-connecteam)).

**Kiosk-mode phones and computers keep working.** One in **Kiosk** mode has its own PIN and isn't
tied to anyone, so archiving someone doesn't sign it out. The confirmation lists the Kiosk-mode
phones and computers that look like theirs (named after them, or using their Home Assistant user),
each with **Revoke**; revoke any other that's theirs under **Phones & computers**.

**Their key cards keep working.** TT Hotel locks check cards on their own, offline, so archiving
can't switch a key card off: it opens its doors until it expires. The confirmation lists their key
cards that still open doors, and once they're archived the **Key cards** list opens on them. For
each one:

- If you have the card, click **Revoke** and put it on the encoder: it's cleared and opens nothing.
- If you don't, click **Report lost**, then write a lost card notice and tap it on each door (or,
  where remote lock-out is on, lock it out remotely on doors with a gateway). See
  [Lost cards](#lost-cards). A replacement card isn't offered for someone archived.

Until that's done, [Door activity](#door-activity) flags the card if it's tried at a door with a
gateway ("Archived person's card tried at Staff Office").

Archived people are hidden from the Staff tab's list. Click **Show archived** under it to see
them (a search shows the archived people it matches too). Each one has:

- **Restore**: back as they were, with their PIN, role, department, devices and own settings.
  They sign in with their PIN again (someone who was turned off before they were archived comes
  back turned off). Their PIN stays theirs while they're archived: nobody else can be given it.
- **Delete for good**: removes them completely (it asks first). Only archived people can be
  deleted, and only once none of their key cards still opens doors: revoke them or report them
  lost first (a lost card goes on alerting at doors as lost), or wait until they expire. Their
  past activity and the cards they made stay in the logs.

Archiving, restoring and deleting are in **Activity** ("Archived Ana Lopez"). Archived people are
in backups and migration files (and stay archived when restored), and staff exports mark them.
Restoring a backup made before someone was archived keeps them archived (the restore warning
names them, and so does Activity): bring them back with **Restore** if you want them. Copying
staff in from a file skips anyone archived here.

## The dashboard updates by itself

While the dashboard is open, it checks every 12 seconds whether anything it shows has changed:
another admin's change, a Connecteam sync in the background, someone signing in. Changes appear
without reloading, and the header says when it last checked ("Updated 3:41 PM").

- It never gets in your way: nothing is redrawn while a dialog is open, while you're typing or
  saving, or while you drag something. The change appears as soon as you're done. Your scroll
  position, opened rows and folds stay as they were.
- If you have unsaved changes (a role, the layout, Wristbands or Settings), they're never
  overwritten: the header says "Changed elsewhere meanwhile" with **Show it**, which shows the
  change and keeps your edits.
- The Activity tab adds new lines at the top as they happen.
- It only checks while the page is showing, and once straight away when you come back to it.
- The checks only read Staff Access's own records: they never ask Connecteam, TT Hotel or Home
  Assistant anything (apart from Home Assistant's usual check that you're an administrator, at
  most every 10 minutes).
- The Tasks, Reports and HR portal tabs are read when you open them.

## Connections: TT Hotel and Connecteam

**Settings → Connections** holds the two outside services Staff Access can sign in to: **TT
Hotel** (for wristbands and key cards) and **Connecteam** (for staff and the time clock). Each is
folded to one line until you click it, showing:

- **Connected**, **Not set up**, **Turned off** (Connecteam), **Wristbands off** (TT Hotel's
  details are saved, but Wristbands is off, so TT Hotel isn't used), **Not checked yet**, or a
  problem in a few words: **Stopped: access refused** (it stopped asking until you fix the
  details), **Paused until** a time or date (a request limit), or **Last try failed**.
- The requests Staff Access made against its own limit: for TT Hotel this month, such as
  "1,240 requests this month (limit 7,500)", and for Connecteam today, such as "35 requests today
  (limit 5,000)". Showing them costs no requests.

Open one for its details, **Test connection**, the full status (why it's stopped or paused) and
how its requests are kept low. Each has its own **Save** button, which saves only that
connection's settings, so saving one never changes the other or the rest of Settings. Secrets
stay hidden: leave the box blank to keep the saved one.

One more is used only by the [HR portal](#hr-portal), and you can set it up before or after
turning it on: **Google Drive (HR files)**, where it keeps employee files (see [Google Drive for
HR files](#google-drive-for-hr-files)). The HR portal reads its people through the **Connecteam**
connection above, only ever reading, in a small share of its own (see [Connecteam and the HR
portal](#connecteam-and-the-hr-portal)). The HR portal tab shows how both are doing.

TT Hotel's **Callback URL**, which sends Staff Access each unlock at doors with a gateway, isn't
one of these details: its address is under **Door activity** on the Wristbands tab (see [Door
activity](#door-activity)).

## Wristbands (TT Hotel key cards)

If your doors use **TT Hotel** (TTLock) locks, staff signed in on a front-desk computer can make
RFID wristbands and key cards from the staff app, on the card encoder plugged into that computer:

- **Day passes:** a guest name and party size. Every wristband opens the doors you chose for day
  passes (such as the pool gate and locker rooms) until the day-pass end time today (8:00 PM
  unless you change it). People whose role allows it can pick a later time (see below).
- **Overnight guests:** a guest name, party size, their room or rooms and a checkout date. Each
  wristband opens the rooms plus the shared doors you chose (such as the main entrance) until
  checkout time on the checkout date (11:00 AM unless you change it). Rooms are picked from the
  doors marked as guest rooms (see **Public doors, guest rooms and staff doors** below). The room
  is optional: for rooms that open with a key, leave it empty and the wristband opens only the
  shared doors.
- **Contractor badges:** a contractor's name (and their company, if you like), how many badges,
  the doors they need and when the badges stop working, at most 7 days ahead (see **Contractor
  badges** below).
- **Staff cards:** a staff member's name, how many cards, what they open (every door, whole
  buildings, whole floors or single doors) and the last day they work (until 11:59 PM that day).
- **Look up a wristband:** put it on the encoder to see what's on it first (its card number,
  which doors it opens and until when), then our record of it: who it was made for and by whom,
  its state and where it was last used. A card with nothing on it says **This card has no doors
  on it** (see below).
- **Card log:** every wristband and card made, with who made it, when and the card numbers, and
  where each card was last used (with [Door activity](#door-activity) set up).
  Search finds names holding what's typed, and card numbers starting with it (six digits or
  more search card numbers only).

Every card has a named maker: it's recorded under the person signed in, so wristbands are only
offered on shared or role-based computers where each person signs in with their own PIN.

### Setting up the front-desk computer

1. On the computer the card encoder is plugged into, install TT Hotel's free **SN Web Socket**
   from [tthotelpro.net/download](https://www.tthotelpro.net/download) and leave it running
   whenever staff make wristbands: Staff Access reaches the encoder through it. Plug the encoder
   in and let Windows install its driver.
2. Chrome or Edge is recommended for the staff app on that computer (see **This computer
   (USB)** below).
3. In TT Hotel on that computer, sign in as the hotel's admin and open **Settings → Integration
   credentials**. You'll copy four things from there: the Client ID, Client Secret, username and
   password. The password there is already scrambled (32 letters and numbers); use it exactly as
   shown, never your real password.

### Turning it on in Staff Access

1. Open the **Wristbands** tab, tick **Make wristbands and key cards** and click **Save wristband
   settings**. Then open **Settings → Connections → TT Hotel** (the **Settings → Connections**
   link on the Wristbands tab goes straight there), paste the four TT Hotel details, click **Save
   TT Hotel settings**, then **Test connection**. It reports how many doors and buildings TT Hotel
   has. **Sync from TT Hotel** (Wristbands tab, under **Doors**) fetches the door list again
   (it's also checked every 6 hours on its own).
2. Under **Day passes**, tick the doors every day pass opens and set **Day passes stop working
   at**. Under **Overnight guests**, tick the shared doors and set the **Checkout time**. Both
   times can be changed at any time; cards already made keep the time they were made with.
   Both door lists show only public doors (and doors not marked yet). Under **Contractor
   badges**, set **Most badges at once**.
3. Under **Doors**, rename doors to what staff call them (TT Hotel keeps its own names) and hide
   any staff should never pick. A hidden door can still be one of the day-pass or overnight
   doors. Mark each door as a **Public door**, a **Guest room** or a **Staff door** (see below).
4. In **Phones & computers**, click **Edit** on the front-desk computer and check its **Card
   encoder**. New computers start on **This computer (USB)**, the one to use for a desk with the
   encoder plugged into it. The computer must be **Shared** or **Role-based**. **Test encoder**
   checks that Staff Access can reach the encoder.
   - **This computer (USB)** (recommended): Staff Access reaches the encoder through the staff
     app open on that same computer, so nothing is set up on the network: no IP address, no
     search, and Staff Access never connects to the computer over your network.
     - **Wristbands must be open on that computer**, with someone signed in who may make
       wristbands. The encoder connects then, and the Wristbands header says **Encoder ready**.
       A card started for that encoder from elsewhere (such as **Make key card** on the Staff
       tab) waits until it's open (for **Wait for each card**), then needs attention. **Test
       encoder** goes through it too, so open Wristbands there first.
     - To make staff key cards from the dashboard on the computer you're using, none of this is
       needed: choose **This computer (the one you're using)** in **Make key card** (see
       [Make key card](#make-key-card)). The dashboard then does what Wristbands does here, for
       your own computer, while its card dialog is open.
     - **The first time**, Chrome or Edge asks whether the staff app may **access other apps and
       services on this device** (that's SN Web Socket): choose **Allow**. It only asks once SN
       Web Socket is running on that computer, so start it first; until then the header says
       **Encoder offline**. If nobody answers, or the question is closed, the header says
       **Encoder not allowed**: click **Try again** and choose **Allow**. It isn't asked again by
       itself, because Chrome can stop asking a site for a while after its question is closed
       a few times.
     - If it was blocked, the header says **Encoder blocked**: click the icon at the left of the
       address bar and allow **Apps on device** for the site (**Local network access** in Chrome
       or Edge before version 145). The page connects by itself once it's allowed.
     - Keep Wristbands open in one window or tab at a time. A newer one takes the encoder over;
       the older one says **Encoder in another window**, with **Use it here** to take it back.
     - **The trade-off:** while a card is made or looked up, the encoder's key from TT Hotel
       (it's valid for 10 minutes) passes through that signed-in browser on its way to SN Web
       Socket on the same computer. The page passes on only the requests for making and reading
       cards, and never keeps or logs the key. Keep that computer for staff, signed in to
       Windows as staff, without browser extensions you don't trust.
     - Open the staff app on that computer at its https address, as its setup link does. It
       reaches the add-on encrypted then, through your tunnel. At `http://<HA IP>:8787` the key
       would cross your network unencrypted; **Only allow phones through your Cloudflare
       Tunnel** ([Security settings](#security-settings)) rules that out. (If `cloudflared`
       runs on another computer, its hop to Home Assistant is on your network, as for all
       staff app traffic.)
   - **Automatic** and **Address** are for other setups, such as an encoder shared by two desks
     or a computer where the staff app isn't kept open. Staff Access then connects to SN Web
     Socket over your network itself, and SN Web Socket accepts commands from anything on your
     network with no password: keep the front-desk computer on the staff network (not guest
     Wi-Fi) and give it a fixed IP address (a DHCP reservation in your router).
   - **Automatic:** click **Find encoders**. Staff Access looks for SN Web Socket on your
     network and lists each one it finds with **Use this encoder**. Click it on the address of
     the computer the encoder is plugged into (in Windows: **Settings → Network & internet**).
     Nothing is used until you do, even when only one is found, because the encoder is sent TT
     Hotel's key. From then on Staff Access uses only that address. If the computer's address
     changes, its card says the encoder wasn't found (Staff Access never switches to another one
     by itself): click **Find encoders**, then **Use this encoder** on the new address. Leave
     **Encoder port** on Automatic unless the encoder isn't found.
     - **Which networks are searched:** the ones in Home Assistant's network setting (**Settings →
       System → Network**, under Network adapter), private IPv4 networks only, and at most the 254
       addresses around Home Assistant's own address on each. To search others, enter them under
       **Wristbands → Advanced → Networks to search for encoders** (such as `192.168.20.0/24`).
       Only SN Web Socket's port (8102) is tried, and only its greeting is read, so nothing on the
       network (the encoder included) is changed by a search.
     - **When it searches:** when the add-on starts, when an Automatic computer's encoder isn't
       found or stops answering (at most every 10 minutes), and when you click **Find encoders**
       (on the computer's Edit screen or the Wristbands tab). It never searches while no computer
       uses Automatic, except when you click Find encoders.
   - **Address:** enter it yourself for a computer on another network, or to pin a desk to one
     computer: just its IP address (such as `192.168.1.50`) is enough, or
     `ws://192.168.1.50:8102`. An address always wins over the other choices. If two computers
     share one encoder, enter the same address on both (the same IP address, not an IP on one and
     a name on the other), so they take turns with it.

   Each computer's card says why Wristbands won't show on it, if it won't: Wristbands turned
   off, no card encoder (or none found or confirmed yet), Kiosk mode for one person or team (it
   needs **Shared** or **Role-based**), or a shared computer whose role can't make wristbands.
   For **This computer (USB)** it also says whether the encoder is connected now.
5. On the **Roles** tab, under **Wristbands**, choose what each role may make: day passes,
   overnight wristbands, contractor badges, staff cards (and whether those may open single
   doors, floors, buildings or every door), which doors they may put on cards (all doors, or
   chosen buildings, floors and doors), the longest overnight stay and staff card, whether they
   may change the doors on a day pass or overnight wristband, whether they **may change when
   day passes stop working**, and whether they see everyone's cards in the card log and
   look-ups. A role is warned if its doors don't cover the day-pass or overnight doors, because
   those cards would then be refused. On a role-based computer, a person can have their own
   wristband rules (click them under their role); otherwise they get their role's.

Staff then see **Wristbands** in the sidebar under **Tools** (or as a tab on a narrow window).
Each kind they may make is listed; one they may make but can't right now (a day pass after the
end time, say) is greyed out and says why. They fill in the form, check the summary, click
**Make**, and the screen walks them through each wristband: place it on the encoder, wait for the
beep, take it off. If one doesn't work they can **Try again**, **Skip this one** or **Stop**.
Wristbands already made keep working.

The add-on checks every request against the person's permissions again, so a role that can only
make day passes for the Bathhouse can't make anything else, even by sending requests by hand.
Refusals show up in **Activity** as **Blocked: wristbands**.

**Time zone.** Cards stop working at these times in Home Assistant's time zone (**Settings →
System → General** in Home Assistant), or the zone you enter under **Time zone** on the
Wristbands tab. The front-desk computer's Windows time zone must be the same: the encoder works
out each door's time from that computer's clock.

### Public doors, guest rooms and staff doors

Each door is a **public door** (the pool gate, the main entrance, the gym), a **guest room** or a
**staff door** (the office, the boiler room, the laundry). On the **Wristbands** tab, under
**Doors**:

- Pick **Public door**, **Guest room**, **Staff door** or **Not marked** next to each door. It's
  saved straight away.
- **Mark a whole floor or building** marks every door there at once: pick the floor or building,
  pick what to mark them as and click **Mark**. If some of those doors are marked another way,
  or are day-pass or overnight shared doors about to become staff doors, it asks first.
- The line above the list says how many doors aren't marked yet, and how many there are of each
  kind.

What it changes:

- Overnight guests' rooms are picked from the guest rooms. The shared doors (the doors every day
  pass opens, the shared doors on overnight wristbands, and the doors people who may change
  them can pick from) are the public doors.
- Staff doors are only for staff cards and contractor badges. They never show as rooms or shared
  doors, in the staff app or under **Day passes** and **Overnight guests** here.
- A door not marked yet shows as both a room and a shared door, so nothing disappears until
  you've marked it.
- The add-on checks it too: a guest room or a staff door picked as a shared door, or a public
  door or a staff door picked as a room, is refused with a message saying so. So is a staff door
  ticked under **Day passes** or **Overnight guests**.
- **Marking a door as a staff door takes it off Day passes and Overnight guests** (it asks first
  when it's ticked there, and Activity says so): from then on no day pass or overnight wristband
  opens it. One still listed there (from a restored backup, say) is left off every card, and
  unticked when you save.
- A door already ticked under **Day passes** or **Overnight guests** keeps going on those cards
  if you later mark it a guest room, as a hidden door does, but the Wristbands tab warns about it
  under that list. Untick it there, or mark it as a public door.
- Staff cards and contractor badges can open any door, whatever it's marked as.
- Syncing doors from TT Hotel never changes what a door is marked as.

### Contractor badges

A contractor badge lets a plumber or an electrician into the doors they need for a few days.

- **Who may make them:** tick **Contractor badges** for a role on the **Roles** tab (or for one
  person, on a role-based computer). Nobody may until you do. They then see **Contractor badge**
  next to **Overnight** on the Wristbands screen.
- **What it opens:** the doors picked when it's made, from any door the person making it has
  access to (the **Doors they may put on cards** for their role): public doors, guest rooms,
  staff doors and doors not marked yet. Hidden doors aren't offered.
- **How long:** it works from when it's made until the date and time picked, which must be later
  than now and at most 7 days away (until the same time of day, 7 days from today). A later
  one is refused with "Contractor badges can last at most 7 days." The form starts at 11:59 PM
  today.
- **How many:** up to **Most badges at once** (5 unless you change it, at most 20), set on the
  **Wristbands** tab under **Contractor badges**, with whether they open doors that are deadbolted
  from inside (off unless you change it).
- The card log, **Look up**, the activity log ("Made a contractor badge for Sam Lee (Acme
  Plumbing)") and [Door activity](#door-activity) show it as a contractor badge, with the
  contractor's name and company. It isn't a staff key card, so it isn't on the Staff tab's **Key
  cards** list; wipe one on the encoder with **Look up**, then **Revoke (wipe this card)**. A wiped
  card is blank, so you can make a new wristband or card on it straight away, without lifting it
  off. (A card just *made* still has to come off the encoder before the next one, so it's never
  written over by mistake.)

### Changing when a day pass stops working

Day passes normally stop working at the time set under **Day passes stop working at** (8:00 PM
unless you change it). To let someone pick another time, tick **May change when day passes stop
working** for their role on the **Roles** tab (or for that person, on a role-based computer).

- They then see **Works until** on the day pass form, set to the usual time. They can pick any
  time later than now, today, up to 11:59 PM (earlier than usual too). The summary, the card log
  and the wristbands all use the time picked.
- After the usual time has passed they can still make day passes: the time is left empty, and
  they pick a later one. Everyone else sees why day passes can't be made.
- Without it there's no time to pick, and a different time sent by hand is refused (and shows in
  **Activity** as **Blocked: wristbands**).

### TT Hotel requests

TT Hotel's free plan allows 30,000 requests a month, counting every request. Staff Access is
built to use a small share and never get the account blocked:

- Making cards or looking one up needs a short-lived key for the encoder from TT Hotel (valid 10
  minutes). Staff Access fetches one when it's needed and reuses it for about 8 minutes, so a
  busy desk uses at most about one request every 8 minutes, however many wristbands it makes.
  The card log and [Door activity](#door-activity) cost nothing.
- If TT Hotel can't be reached, day passes and overnight wristbands can still be made with the
  last key; blank wristbands (never used before) and staff cards for whole floors, buildings or
  every door need a fresh one, so they need the internet.
- The door list is synced every 6 hours (change it under **Check for new doors every**), only
  while Wristbands is on and something uses it: a computer has a card encoder, a key card was made
  on **This computer** from the dashboard in the last 90 days (or its card dialog is open), or
  [Door activity](#door-activity) is coming in. A sync is one or two requests, plus signing in
  about once every 90 days.
- Its own hard limits: 7,500 requests a month (a quarter of the free plan), counted across
  add-on restarts, and 30 a minute. Automatic door syncs stop at half of that, keeping the rest
  for making cards.
- If TT Hotel says the account has reached its limit, Staff Access stops asking until the limit
  resets and never retries straight away. If it rejects the Client ID, secret, username or
  password, it stops all automatic requests until you save the TT Hotel settings again or
  click **Test connection**. Both last through add-on restarts, and a restart doesn't sync the
  doors again before they're due.
- **Settings → Connections** shows how many requests Staff Access made this month on TT Hotel's
  line. Open **TT Hotel** for when the doors were last synced and, while stopped or paused, why.
  The Wristbands tab says whether the account is connected, next to its **Make wristbands and key
  cards** switch.
- Wristbands makes **no Connecteam requests**: it uses the same sign-in and clock-in checks as
  the rest of the app.

### Looking up a card

**Look up** reads the card itself, so it shows the truth even when our record is out of date:

- **This card has no doors on it.** The card was cleared, or cancelled in TT Hotel's own program
  or app, or it's blank. It opens nothing. If our record still said **Active**, it's marked
  **Revoked** ("Found cleared on the encoder") and the activity log says so. A card that only
  reads as blank or another hotel's is left as it is in our records, because a changed **Card
  sectors** setting can make a working card read that way.
- **Revoke (wipe this card)** clears the card on the encoder (it's already there), with the usual
  progress screen, and marks it revoked. It's offered to people whose role sees everyone's cards,
  and to whoever made that card. The dashboard's **Revoke** works as before.
- Staff Portal can't see a card reported lost in the TT Hotel app. Active cards say so: use
  **Report lost** in Staff Access (Staff tab → **Key cards**) so the card is tracked and locked
  out.
- **Last used** (with [Door activity](#door-activity) set up) says where and when the card was
  last tried at a door, such as "Pool Gate, today 3:12 PM", with "refused" if the door didn't open.

### Door activity

For doors connected to a TT Hotel gateway, TT Hotel can tell Staff Access every time a door is
opened, or a card is refused, as it happens. Staff Access then shows:

- **Door activity** on the Wristbands tab, newest first: when, which door, who (the card made here
  and its number, or TT Hotel's own name for it: the app user, or the card's or passcode's name),
  how (card, app, passcode, fingerprint…) and whether the door **opened** or **refused**. Choose
  one door, or tick **Problems only** for refused tries, alarms (tamper, locked after wrong tries)
  and the lost cards below.
- **Last used** for each card: in the staff app's card log and Look up ("Pool Gate, today 3:12
  PM"), and in the dashboard's Key cards list. A contractor badge is listed with the
  contractor's name and **Contractor badge**, and an alert about one calls it a contractor badge
  ("Contractor badge 3661413478 (Sam Lee (Acme Plumbing))").
- **Lost or revoked cards tried at a door.** When a card marked **Lost** or **Revoked** here is
  tried at a door after it was marked, or TT Hotel reports that a card it deleted was tried, the
  row is highlighted, the activity log says so ("Lost card tried at Pool Gate", or "Lost card
  opened Pool Gate"), and Home Assistant shows a notification naming the card and the door. Each
  card alerts at most once every 10 minutes, and its next notification replaces the last one; at
  most 20 alerts go out a minute in all (Door activity still lists every try).
  Untick **Tell Home Assistant when a lost or revoked card is tried** to keep it to the activity
  log.
- **An archived person's card tried at a door.** A key card of someone you've
  [archived](#archiving-people), tried after they were archived and before it expires, alerts the
  same way ("Archived person's card tried at Staff Office"), until it's revoked or reported lost.
- **A card that was cleared is done with.** A card revoked on an encoder, found blank by **Look
  up**, or written over goes back in the stack and may be given out again, here or in TT Hotel's
  own program. From then on its number is whoever has it now: those records aren't matched to the
  old card (no **Last used**, no name) and never alert. A card marked revoked because it was
  replaced, and never handed back, still alerts, and so does a lost card until it's found and
  cleared.

To set it up:

1. Set the **staff app address** in **Settings** (see section 2): TT Hotel reaches Staff Access
   through your Cloudflare Tunnel, like the phones do.
2. On the **Wristbands** tab, under **Door activity**, click **Copy** next to the address. It looks
   like `https://staff.yourdomain.com/tthotel/records/…`.
3. In TT Hotel, signed in as the hotel's admin, open **Settings → Integration**, paste it into
   **Callback URL** and save.
4. Open a door that has a gateway, then click **Refresh** under Door activity: it's listed, and
   the section says when the last record arrived.

What to know:

- Only doors connected to a gateway report, while the gateway is online (one that was offline
  sends what it missed when it's back). Doors without a gateway never show up here.
- Records for a door Staff Access doesn't know are left out, and the section says so: click **Sync
  from TT Hotel**.
- The address is the only key, so give it only to TT Hotel. **Make a new address** replaces it
  (it asks first): the old one stops working at once, so paste the new one into TT Hotel straight
  away. Anything sent to a wrong address gets "not found".
- Staff Access only stores what TT Hotel sends, matches the cards and sends the alerts. It makes no
  TT Hotel requests for it and never opens or changes anything because of it.
- A card is matched whether TT Hotel writes its number with or without leading zeros, or with its
  bytes the other way round (TT Hotel says card readers of different makes show the same card
  either way). A card number that was cleared and made again is the newer card from when it was
  made.
- Passcodes are never stored: a passcode is shown by its name in TT Hotel.
- Each door's battery level is updated from its records.
- The time shown is the lock's own clock, unless it's clearly wrong (more than 30 days behind TT
  Hotel's, or 5 minutes ahead), when TT Hotel's is used. A record more than an hour in the future
  is left out.
- Door activity keeps 50,000 records and 180 days. It isn't in backups (TT Hotel keeps its own
  six months). Neither is the address, and a restore never changes it; a migration file carries it,
  so TT Hotel's Callback URL keeps working on the new install.
- The address takes up to 300 deliveries and stores up to 1,000 records a minute, from wherever
  they come; past that, TT Hotel is asked to send them again later, so nothing real is lost and a
  leaked address can't wipe the door history in minutes. Staff Access reads at most 300 records
  (64 KB) from one delivery, and otherwise always answers TT Hotel "success", so TT Hotel never
  sends the same ones again. Requests to a wrong address are limited separately (30 a minute from
  one place) and never hold up TT Hotel's.
- The Home Assistant notification only names cards made here and shows a card number only when
  it's digits; TT Hotel's own names (which whoever has the address could set to anything) stay in
  the activity log and Door activity, where they're shown as plain text.

### Troubleshooting

- **"The card encoder isn't answering"** (Wristbands screen): click **Reconnect** to have Staff
  Access connect to the encoder now; the screen otherwise shows the last problem until the
  encoder is next used. Messages from the encoder end with its own code (**Encoder error 1004**,
  say): mention it when asking for help.
- **"The card encoder wasn't found"**, or **the encoder types card numbers like a keyboard**
  (into the PIN pad, a name or the address bar) and no card gets made: SN Web Socket can't reach
  the encoder on that computer, which then acts as a plain card reader. SN Web Socket looks for it
  as a COM port with a Silicon Labs CP210x chip:
  1. Open **Device Manager** on that computer (right-click **Start**) with the encoder plugged in,
     and look under **Ports (COM & LPT)** for **Silicon Labs CP210x USB to UART Bridge (COM…)**.
  2. If it isn't there (or shows under **Other devices** with a warning sign), install the CP210x
     driver: it comes with SN Web Socket (`CP210xVCPInstaller_x64.exe` in the `resources\driver`
     folder where SN Web Socket is installed), or download **CP210x Universal Windows Driver**
     from Silicon Labs. Then unplug the encoder and plug it back in.
  3. Close TT Hotel on that computer, restart SN Web Socket, and click **Test encoder** in
     **Phones & computers**.

  Staff Access ignores the typed card numbers on its own pages either way (nobody types that
  fast, so it can tell); elsewhere they're typed as usual.
- **"Open Wristbands on Front Desk PC to connect the encoder."** (This computer (USB)) Sign in
  on that computer and open **Wristbands**: a job waiting for it carries on by itself, and one
  that needs attention goes on with **Try again**.
- **"Can't reach SN Web Socket on this computer: is it running?"** (This computer (USB), in the
  Wristbands header) Start SN Web Socket on that computer, the one the browser is on; the page
  connects to it by itself, and the browser then asks to **access other apps and services on
  this device**: choose **Allow**. The browser never asks while SN Web Socket isn't running. If
  it is running and the browser still doesn't ask, check that **Apps on device** isn't blocked
  for the site (the icon at the left of the address bar).
- **"This browser is blocking the card encoder"** (**Encoder blocked**): allow **Apps on device**
  for the site (the icon at the left of the address bar; **Local network access** in older
  versions). **"Allow this site to reach the card encoder"** (**Encoder not allowed**): nobody
  answered the browser's question; click **Try again** and choose **Allow**.
- **"Open Home Assistant at its https address to use this computer's encoder"** (the dashboard's
  **This computer**): the browser only lets a secure page reach the encoder. Open Home Assistant
  at its https address rather than `http://<its IP>:8123`, or choose a computer's encoder. Its
  other messages are listed under [Make key card](#make-key-card).
- **"Can't reach the card encoder's computer."** (Automatic or Address) Check that the computer
  is on, SN Web Socket is running, the address in **Phones & computers → Edit** is right, and
  nothing (such as Windows Firewall) blocks port 8102 from Home Assistant.
- **"Automatic: no encoder found on 192.168.1.0/24."** The same checks, and that the computer is
  on that network: if it's on another one (a separate VLAN for the front desk, say), enter that
  network under **Wristbands → Advanced → Networks to search for encoders**, or enter the
  computer's address instead of Automatic. **"Couldn't read Home Assistant's network setting"**
  means Home Assistant didn't answer; enter the network there too, or try again later.
- **"Automatic: found 192.168.1.50. Click Use this encoder to confirm it."** Nothing is used
  until you confirm it: **Edit** the computer and click **Use this encoder** on its address.
  **"Automatic: 192.168.1.50 not found"** means the encoder you confirmed wasn't found: check the
  computer, or if its address changed, confirm the new one.
- **"The card encoder isn't plugged in"** or **"Couldn't connect to the card encoder."** Check
  the USB cable. The classic TT Hotel program and SN Web Socket can't hold the encoder at the
  same time: Staff Access lets go of it after each batch, so close the TT Hotel program's card
  screen while staff make wristbands. If the encoder still isn't found, enter its COM port
  (from Windows Device Manager) under **Encoder port**.
- **"Center it on the encoder"** (codes 101, 102): the card wasn't read; put it flat in the
  middle of the encoder.
- **"There isn't room on the card for that many doors"** (code 104): use floor or building access
  for staff cards, or fewer doors. **Most doors on one card** (Advanced) limits this up front.
- **"Belongs to another hotel or can't be set up"** (105, 106, 109): the wristband was set up for
  another property, or the **Card sectors** (Advanced) don't match TT Hotel's sector setting. Use
  the same 16 digits as TT Hotel (its default is `0000000000011111`).
- **"The authorization from TT Hotel expired"** (13): click **Try again**; Staff Access fetches a
  new key.
- **The card is made but the door says it's expired.** Lock clocks drift, especially after the
  batteries run out. Sync the lock's time in the TT Hotel app. Also check that the front-desk
  computer's time zone matches the property's.
- **Cards always start now.** TT Hotel cards have no start time, so there's no "valid from
  tomorrow": make them when the guest arrives.
- A job left needing attention for 15 minutes is stopped, and a restart of the add-on stops any
  unfinished job (the card log shows how many were made).

**Security.** The TT Hotel details never reach a browser or phone: the add-on talks to TT Hotel
itself. The encoder's short-lived key from TT Hotel goes only where cards are made: with **This
computer (USB)**, through the signed-in staff app on that computer while a card is made or looked up
(open it there at its https address); with the dashboard's **This computer**, through your own
browser while a key card you started is made (only an administrator's dashboard, through Home
Assistant, gets one); with **Automatic** or **Address**, from the add-on to SN Web Socket over your
network, and only to an address you entered or confirmed. SN Web Socket accepts
commands from anything on your network, with no password, so keep the front-desk computer on the
staff network (not guest Wi-Fi). The Client Secret and password are never shown again after saving
(leave the boxes blank to keep them) and are never in backups (only a migration file
carries them, encrypted).

## Departments and staff key cards

### Departments

On the **Staff** tab, under **Departments**, click **Add department** (Housekeeping, Front Desk,
Maintenance…). Each one has:

- **Connecteam smart group** (optional): people linked to Connecteam who are in that smart group
  are in this department (see [Departments from smart groups](#departments-from-smart-groups)).
- **Role**: given to people in it (or **Leave their role as it is**).
- **Clock-in**: for people linked to Connecteam, **Must be clocked in** (everyone in it must be
  clocked in to sign in), **Doesn't need to clock in**, or **Each person's own setting**. Someone
  whose own setting you've changed by hand keeps theirs (see **Clocking in, by department or
  person** in [the Connecteam section](#add-everyone-from-connecteam)).
- **Start page**: where the staff app opens when someone in it signs in (a page of the layout,
  Tasks, Reports or Wristbands), from the first of their departments that sets one, unless it's
  something they can't see there or they already opened something else since signing in.
- **Key card**: what a key card made with **Make key card** opens (every door, whole buildings,
  whole floors or single doors, like a staff card), how many days it lasts, and whether it opens
  doors that are deadbolted from inside.

**Someone in several departments** is in all of them: they show under each one on the Staff tab,
and every one counts for reports and tasks. Their role, key card and clock-in rule each come from
the first department in the list that sets it; one that leaves it alone is skipped. So an "All
users group" department with **Leave their role as it is** never gets in the way of Front Desk's
role. Drag departments to change the order. Their own role and clock-in setting, changed by hand,
still win.

Each department's row says how many people are in it, where they come from (**Connecteam:**
and the smart group, or **Added by hand**) and what it sets, for example **Role: Front Desk ·
Key card: Staff doors, 30 days · Clock-in required**, or **Sets nothing**. Two smart groups with
the same name (Front Desk and Front Desk (2)) also show each one's Connecteam group ID and how
many people Connecteam has in it, so you can tell which is which. Each person's row lists their
departments, with **(role)** after the one their role comes from.

Pick each person's departments in **Edit** (tick any number). For someone linked to Connecteam,
that sets them by hand (see below). Deleting a department leaves its people in their other
departments. Departments are in backups, and can be exported and imported on the Backup tab (roles
are matched by name).

**Hiding a department.** A department that follows a smart group has **Hide** instead of
**Delete**: deleting it would only have the sync make it again. A hidden department gives nothing
(no role, key card, clock-in rule, reports or tasks), isn't offered anywhere (the staff app, the
dashboard and the HR portal), and is never made again by the sync. Its people stay in it. Hidden
departments are folded away under the list, in **Hidden smart groups**, each with **Show** to bring
it back as it was. A department used by a task form can't be hidden (or deleted) until you take
it out of the form's departments on the Tasks tab. Departments you made by hand (no smart group)
keep **Delete**, and so does one whose smart group was removed in Connecteam. Hiding and showing
are in **Activity**, and hidden departments stay hidden in backups and migration files.

### Make key card

Every person's card on the Staff tab says what **Make key card** would make for them, from the
first of their departments that has a key card. Click it, have a blank card (or a card to reuse) ready at the encoder, and the
dashboard walks you through it like the staff app: place the card, wait, take it off, with **Try
again** and **Stop** if it doesn't work. The card is recorded as made by you (your Home Assistant
name), from the dashboard, for that person.

It asks which card encoder to use, with **This computer (the one you're using)** first:

- **This computer (the one you're using):** the encoder plugged into the computer you have Staff
  Access open on. Nothing to set up in Phones & computers: just plug the encoder in and start TT
  Hotel's **SN Web Socket** on that computer (see [Setting up the front-desk
  computer](#setting-up-the-front-desk-computer)). The dialog connects to it and says how it's
  going:
  - **Connecting to the card encoder on this computer…** Wait a moment; the card starts as soon as
    it's ready.
  - **Choose Allow when the browser asks.** The first time, Chrome or Edge asks whether the site
    may **access other apps and services on this device** (that's SN Web Socket): choose
    **Allow**. **Allow this site to reach the card encoder** means nobody answered: click **Try
    again**, then **Allow**.
  - **This browser is blocking the card encoder.** Click the icon at the left of the address bar
    and allow **Apps on device** for the site (**Local network access** in older versions). It
    connects by itself once it's allowed.
  - **SN Web Socket isn't running on this computer.** Start it, with the encoder plugged in, then
    click **Try again**.
  - **Open Home Assistant at its https address to use this computer's encoder.** Browsers only let
    a secure page reach the encoder, so it doesn't work with Home Assistant opened at
    `http://<its IP>:8123`. Open Home Assistant at its https address (your Home Assistant Cloud
    or own domain address), or choose another encoder.
  - **Connected in another window.** A card dialog of yours is using it in another window or tab
    (or on another computer): **Use it here** takes it back.

  Each Home Assistant administrator has their own: only the cards they start from the dashboard go
  to their own computer. Keep the dialog open until the card is done. If the page is closed
  mid-card, the card needs attention, and the next card you start on this computer shows that one
  first, to **Try again** or **Stop**. On a front-desk computer that also has Wristbands open,
  make one card at a time: the staff app and the dashboard share its encoder.

  **One account on two computers** (a shared front-desk login): a card is only ever made on the
  encoder of the window that started it. If another window or computer opens a card dialog
  meanwhile, it takes the encoder link, and the card waiting in the first window needs attention
  ("The card encoder link was taken by another window"): click **Use it here** there, then **Try
  again**. The other window is shown that unfinished card, but can only **Stop** it (then start
  its own). Reloading the page keeps the window's card; a new tab is another window.
- **A computer's card encoder** (set up in Phones & computers). One set to **This computer (USB)**
  is reached through that computer, so **Wristbands** must be open there (the card waits for it).

With no computer's encoder set up, This computer is used without asking. **Revoke**, **Write a
notice card**, **Make a replacement card** and the remote lock-out test card ask the same way.

If the person already has an active key card, you choose:

- **Replace their current card:** the new card also carries a cancellation of the old one. The
  first time it's tapped on a door, that door refuses the old card. The old card is marked
  revoked. Best for a lost or broken card.
- **Add another card:** their current card keeps working too.

### The Key cards list

**Key cards** on the Staff tab lists every staff key card, made here or at a front-desk computer
(cards made in the staff app are matched to the person by name), newest first. The section is
folded at first, showing only its title and a count of the cards that still open doors ("Key
cards: 14 active, 2 lost"; a lost card counts until it expires). Click it to open the list; this
browser remembers whether it's open. Filter by
**Active**, **Expired**, **Revoked**, **Lost** or **All**, and search by name or by the start of a
card number. Each
row shows who it's for, the card number, what it opens, who made it and when, when it stops
working, its state and, for a lost card, how many of its doors it's been locked out of, and
where it was last used (with [Door activity](#door-activity) set up). A card whose holder is
archived says **Archived** under their name.

- **Revoke** (you have the card): choose the encoder (**This computer** for the one plugged into
  the computer you're using), put that card on it, and it's cleared, so it opens nothing; it can
  be used again for a new card. Only that exact card is cleared: any
  other card gets "That's card 123…, not 456…" and is left as it is.
- **Report lost** (you don't have it): the card is marked lost and the next screen shows each
  door it opens, which it keeps opening until it expires unless it's locked out there.

People whose role may **see everyone's cards** can also revoke a staff card (with it in hand)
from the card log in the staff app, which shows each staff card as Active, Expired, Revoked or
Lost. Reporting a card lost and locking it out are done in the dashboard.

### Lost cards

TT Hotel locks check cards on their own, offline, so a lost card can't simply be switched off: each
door has to be told to refuse it. A door is safe once any one of these has been done there. The
lost card's screen (**Lock out…** in the Key cards list) lists its doors with what's been done.

1. **Lost card notice.** **Write a notice card** writes a cancellation of the lost card onto a spare
   card (a blank card, or a hotel card nobody uses; it's cleared first, so the notice opens no
   doors). Walk it to each door on the list and tap it once, then tick **Notice tapped** for that
   door. From then on that door refuses the lost card. **Look up** in the staff app shows what a
   notice card cancels.
2. **Replacement card.** **Make a replacement card** makes the person a new key card (their
   department's, or what the lost card opened) that also carries the cancellation. Each door
   refuses the lost card once the new card has been tapped there: tick **Replacement tapped** as
   that happens. Not for someone archived: use a notice card.
3. **Remote lock-out (experimental, off by default).** For doors with a gateway, TT Hotel can tell
   the door to refuse the card number without anyone walking there. See below.

Once the card's end date has passed it opens nothing, so there's nothing more to do.

### Remote lock-out (experimental)

TT Hotel's documentation says that deleting a card from a lock through its API makes the lock
refuse that card number from then on, encoder-made cards included. Staff Access uses that: for
each of the lost card's doors with a gateway, it adds the card number to the lock (starting a
year from now and lasting a day, so the card can't open anything while it's briefly there), then
deletes it.

**It's permanent.** A card number locked out remotely is retired on that door for good: that
card can never open it again, even if it's found, cleared and made again. Throw such cards away.

To turn it on:

1. On the **Wristbands** tab, under **Lost key cards: remote lock-out**, tick **Offer remote
   lock-out for lost cards** and save.
2. Click **Test remote lock-out** and follow the steps with a blank card at a door with a gateway:
   make a test card for that door, tap it (it should open), click **Lock the test card out**, wait
   a minute and tap it again. If the door refused it, click **It was refused**: remote lock-out
   can now be used. If it still opened, click **It still opened**: Staff Access switches the
   **Card number format** (reversed, which TT Hotel recommends for encoder cards, or as printed)
   and you test again. If neither works, these locks don't support it: use notice or replacement
   cards.

Until it's been tested (and again after the number format changes, including by restoring a
backup with the other format), the button on a lost card
is shown but greyed out, saying why. Once tested, **Lock out of N gateway doors remotely** works
door by door in the background, and each door shows how it went.

TT Hotel requests: 2 per gateway door, only when you click it, within the same monthly limits as
the rest of Wristbands. Doors go one at a time, at most about 10 a minute, leaving room for
wristbands and key cards. A door whose gateway is busy or offline is tried again after the others,
at most 3 times, 5 minutes apart. A door tried before is checked for a leftover enrolment first
(one more request), so a card is never added to a lock twice. A restart of the add-on stops a
lock-out that's running; start it again from the lost card's screen.

## Connecteam: add staff and require clocking in (optional)

If you use [Connecteam](https://connecteam.com) for time tracking, Staff Access can add staff from
your Connecteam user list, use their **kiosk code** as their PIN, and only let them sign in while
they're **clocked in**. Staff you add by hand keep working exactly as before.

API access needs Connecteam's **Expert plan or higher** (with the Operations hub for the time clock).

1. In Connecteam, open **your name → Integration Center → OAuth 2.0** and click **Create app**.
   Give it the **users.read**, **time_clock.read**, **schedule.read** and **forms.read** scopes,
   and **jobs.read** if you like (see [Connecteam permissions](#connecteam-permissions)). Scopes
   can't be changed later: to add one, create a new app and enter its keys. Staff Access, the HR
   portal, reports and tasks only ever read from Connecteam, so no write permissions are needed.
2. Copy the **Client ID** and **Client Secret**. Connecteam only shows the secret once.
3. In Staff Access, open **Settings → Connections → Connecteam**, tick **Link staff to
   Connecteam**, paste both keys, choose **Australia** under **Account hosted in** if your
   Connecteam account is hosted there, and click **Save Connecteam settings**, then **Test
   connection**. It shows each step with Connecteam's own words: **Getting an access token**
   (and the permissions the app has), **Reading users**, **Reading the time clock** (and how many
   people are clocked in now) and **Reading smart groups**. If a step fails, it says which
   permission the app is missing, or that it's your Connecteam plan.
4. On the **Staff** tab, click **Add from Connecteam** and pick a person. People already added are
   greyed out (archived people too: restore them instead).
   - **Use their Connecteam kiosk code as their PIN** is ticked when Connecteam has a kiosk code
     of 4 to 8 digits for them. The add-on reads it straight from Connecteam; it's never shown
     in the dashboard. Otherwise, type a PIN.
   - **Only allow sign-in while clocked in** is ticked by default. Untick it for someone who should
     be able to sign in at any time.
5. Make sure they're allowed on the shared phone or computer (**Phones & computers → Edit → Who
   uses it**), the same as for any staff member.

How clocked-in sign-in works:

- One check covers everyone. Staff Access checks once an hour (change it under **Check the time
  clock every**), only while someone must be clocked in. A shift that has started and not ended
  means "clocked in" (a shift left open for more than 24 hours doesn't count).
- Clocked-in-only people must be on the clock to **sign in** and to **use a control**. Reading
  (temperatures and other states) never checks the clock.
- If the last check found them clocked in, signing in or using a control just works, with no
  request to Connecteam. If it didn't (they've just clocked in, say), Staff Access checks right
  away, which also updates everyone else. That check replaces the next hourly one if it's within
  20 minutes of it.
- If they're still not clocked in, they can't sign in, and a control signs them out with a
  message saying why. The sign-in list shows **Not clocked in** under their name.
- So someone who clocks out can keep reading until their session ends, but the first control
  they use after the next check signs them out.
- If Connecteam can't be reached, the last answer is kept until 15 minutes after the next check
  was due. After that, clocked-in-only people can't sign in or use controls until Connecteam
  answers again. **Settings → Connections** and the add-on log show what went wrong.
- The Staff tab shows each linked person's clock status. **Edit** lets you turn clocked-in-only
  on or off, change their PIN to their current kiosk code (if it changed in Connecteam), or pick
  another **Connecteam user** (search by name) or **Not linked**. One Connecteam user can be
  linked to one person only. Each link, change and unlink is in **Activity**.
- Turning off **Link staff to Connecteam** lets everyone sign in with their PIN again, without
  removing the links.

### Linking staff you already have

People you added by hand before connecting Connecteam aren't linked to anyone there. To link
them, click **Link existing staff** (Staff tab, or **Settings → Connections → Connecteam**; it's
also offered after the first **Test connection** or **Save** that reads your users).

- Everyone here who isn't linked (not archived people) is listed with a **suggested Connecteam
  user** when their name is a clear match: the same name (accents, capitals and punctuation
  ignored), first and last swapped, a short first name ("Bob Smith" for "Robert Smith", "Chris"
  for "Christopher"), or a first name alone ("Bob"). A suggestion is only made when it's the one
  match for that person, nobody else here matches that user as well, and that user isn't linked to
  someone else.
- Check each row: pick someone else from the searchable list, or **Don't link**. Tick the rows to
  link and click **Link selected**; nothing changes until you confirm.
- Linking keeps their PIN and role. **Put them in their smart group's department** is ticked;
  untick it to keep their department (it then counts as set by hand). **Also use their kiosk code
  as their PIN** and **Also require them to be clocked in to sign in** are up to you: without
  that last tick, linking never makes someone clock in, even in a department that requires it
  (their own setting says so, and you can change it on their row).
- **Relink everyone** lists everyone, linked or not, with their link now beside the best match.
  Rows where they differ stand out, and a link to someone Connecteam no longer lists (archived or
  removed there) shows as **Link broken**. Tick the changes to make; two people's links can be
  swapped in one go. Links that already match are left alone.
- Linked people then follow the usual rules: the clock-in rule, and their smart group's
  department (below). People you added by hand are never turned off or given a new PIN by the
  sync, even if they're archived in Connecteam: Relink everyone shows that as a broken link.
- With **Add everyone from Connecteam** on, a Connecteam user with the same name as someone here
  who isn't linked (first and last swapped counts) is never added as a second person: they wait
  as a suggestion in Link existing staff. If they really are two people, rename one here, or add
  the other with **Add from Connecteam** under another name.

### Departments from smart groups

**Make a department for each smart group** (Settings → Connections → Connecteam; ticked for new
setups) makes a department for each Connecteam smart group that has none, about once an hour, or
at once with **Add departments now**. New departments have no role, no key card and **Each
person's own setting** for clock-in, until you set them. A department you made by hand with the
same name as a smart group (and no smart group) follows that group instead of getting a twin.
Connecteam's own **All users group** and **All admins group**, which everyone is in, get departments
that are made hidden. An install from before hiding hides its departments for those two once, if
they still follow their smart group and you haven't given them a role, key card or clock-in rule.

- Its name follows the smart group's when the group is renamed in Connecteam, unless you rename
  it here.
- A smart group removed in Connecteam leaves its department, marked **Smart group removed in
  Connecteam**. It's never deleted automatically.
- Everyone linked to Connecteam (added by the sync or by hand) is in the department of each smart
  group they're in, straight away when linked, and the hourly sync keeps that up to date as their
  groups change. Departments the sync can't know about (made by hand, or whose smart group was
  removed) stay. Someone in no matching group stays where they are.
- Change a linked person's departments in **Edit** and they're **Set by hand**: the sync leaves
  them alone. **Follow Connecteam** on their row goes back to their smart groups' departments.
- A change of departments doesn't change their own clock-in setting. If the rule they get from
  their departments changes, that counts (unless their own was set by hand), and the change's line
  in **Activity** says so.

This reads the smart group list Staff Access already keeps (asked again at most every 6 hours)
and the user list the sync reads anyway. Each department made or renamed, and each person whose
departments change, is in **Activity**.

### Connecteam permissions

One Connecteam app (one Client ID and Secret) serves everything. Its permissions (scopes):

| Permission | What needs it |
|---|---|
| **users.read** | Staff Access (people, kiosk codes, smart groups), the HR portal, and reports and tasks (names and departments) |
| **time_clock.read** | Staff Access (who's clocked in) and reports (timesheets) |
| **schedule.read** | reports (scheduled shifts) |
| **forms.read** | reports (required forms) and [tasks from forms](#tasks) |
| **jobs.read** (optional) | job names in reports; without it, jobs show without names |
| **time_off.read** (optional) | time off policy names in reports (Sick leave, Vacation); without it, leave shows as "Time off" |
| **quick_tasks.read**, **quick_tasks.write**, **quick_tasks.delete** (optional) | the [two-way sync with Connecteam Quick Tasks](#tasks) |
| **forms.write** (optional) | [forms that follow a status](#tasks) writing it back; without it, they only read it |

Staff Access itself needs only the first two. **Test connection** says if reports and tasks are
missing one, and a report or a form's card names the permission Connecteam refused, in
Connecteam's own words. A permission Connecteam refused isn't asked again for 30 minutes (or
until you save the Connecteam settings), so a missing one doesn't use up the reports' requests.

### Add everyone from Connecteam

Instead of adding people one by one, tick **Add everyone from Connecteam** under **Settings →
Connections → Connecteam** and click **Save Connecteam settings**. Every active Connecteam user
then becomes a staff member here:

- Named as in Connecteam ("First Last"). Someone here with that name who isn't linked is never
  doubled: they wait to be linked (see [Linking staff you already have](#linking-staff-you-already-have)).
  If only an archived person has the name, "(2)" is added.
- Their PIN is their Connecteam kiosk code when it's 4 to 8 digits and nobody else here uses it.
  Otherwise they're added **turned off**, and their card says **No usable kiosk code in
  Connecteam**: give them a PIN with **Edit** and tick **Can sign in**. If their kiosk code changes
  in Connecteam, their PIN follows it (and they're signed out), unless you've given them a
  different PIN.
- Their departments come from their Connecteam smart groups (see [Departments from smart
  groups](#departments-from-smart-groups)), and their role from the first of them that sets one.
  Someone in no matching smart group keeps their departments; ones you pick by hand stay. Once you
  change someone's role by hand, the sync leaves their role alone.
- People archived or removed in Connecteam are turned off (and signed out), never deleted. If they
  come back, they're turned on again. Someone you turn off yourself stays off.
- Someone you [archive](#archiving-people) here stays archived and is left exactly as they are,
  whatever Connecteam says, and is never added again. Once restored, the sync keeps them up to
  date again.
- People you added by hand are never changed, apart from their departments once they're linked.
  If you delete someone the sync added, the next sync
  adds them again while they're active in Connecteam: archive them instead. For the same reason
  they can't be unlinked from Connecteam while this is ticked.

The sync runs about once an hour while this is ticked (or while departments are made from smart
groups, or anyone linked follows their smart groups' departments), soon after you save the
settings, and when
you click **Sync staff now** (on the Staff tab or in Settings; at most once a minute). It uses the
same request limits as the time clock checks (below) and never runs while Connecteam is paused or
stopped. The Staff tab says when it last ran and what changed, and each sync that changes
something is in **Activity**.

**Clocking in, by department or person.** **People added this way must be clocked in to sign in**
(ticked by default) sets this for people the sync adds. A department can set it for everyone in
it instead (Staff tab, **Departments**, **Edit**, **Clock-in**): **Must be clocked in**,
**Doesn't need to clock in**, or **Each person's own setting**. You can also change it for anyone
linked to Connecteam, however they were added, with the **Must be clocked in to sign in** switch
on their row on the Staff tab (or in **Edit**).

What counts for each person:

1. Their own setting, if you've changed it by hand. Their departments and the sync never change
   it back.
2. Otherwise the first of their departments, in the department order, that sets one.
3. Otherwise their own setting (for people the sync added, the one it gave them).

Their row shows **Must be clocked in** or **Clock-in not required**, and where that comes from:
**From Housekeeping** (their department), **Set by hand**, or, for people the sync added,
**Default** (the setting above). When it's set by hand and their department sets one, **Use the
department's setting** goes back to the department's. Changing a department's setting counts
from the next sign-in (and, for anyone already signed in, from the next control they use).
Changing someone's departments in **Edit** leaves the switch following their new departments,
unless you change the switch too.

### Staying within Connecteam's API limits

Connecteam limits API requests **per account**, shared by every integration you use (Expert plan:
100 a minute and 10,000 a day; going over gets requests refused). Staff Access is built to use
a small, steady share:

- It only checks the time clock while it's needed: Connecteam is turned on and at least one
  active person must be clocked in. Otherwise it sends nothing.
- Each check is one request per active time clock. The list of time clocks is reused for 6 hours
  and the user list for 5 minutes. The access token lasts 24 hours (one more request a day).
- **Add everyone from Connecteam** reads the user list (one request for up to 500 people) about
  once an hour, after the add-on starts, after saving the settings and on **Sync staff now** (at
  most once a minute). The list of smart groups is only read when you edit a department, and
  reused for 6 hours. Nothing is asked on page loads.
- The dashboard keeps itself up to date while it's open (see [The dashboard updates by
  itself](#the-dashboard-updates-by-itself)) from what Staff Access already knows: that never
  asks Connecteam anything.
- With one time clock, hourly checks are 24 requests a day. Add one for each sign-in or control
  by someone the last check didn't find clocked in, typically the start of each shift. With many
  time clocks it checks less often if needed, so regular checks stay under 15% of your daily limit.
- Those extra checks happen only after someone types their correct PIN (or uses a control while
  signed in), so a wrong PIN, or someone just holding a phone, never costs a request. They happen
  at most every 30 seconds (less often while Connecteam is failing) and stop once three quarters
  of the day's share is used, keeping the rest for the hourly checks.
- Its own hard limits: 60 requests a minute, and 9,000 a day (never more than 90% of your
  account's daily limit, so your other integrations always keep 1,000 a day on Expert), counted
  across add-on restarts. At the cap it pauses until midnight UTC.
- It reads Connecteam's rate-limit headers. If your account (from any integration) has less than
  10% of its daily requests left, or is close to the per-minute limit, Staff Access pauses until
  the limit resets.
- If Connecteam says "too many requests", it waits as long as Connecteam asks (at least a
  minute, doubling each time, and never more than 24 hours) and never retries straight away.
- If Connecteam refuses a request (401 or 403), it asks once more with a fresh access token (an
  expired or replaced token is refused too). Refused again, it stops all automatic requests,
  with Connecteam's own words and what's missing (a permission, or your plan). It tries again by
  itself after 30 minutes, up to 3 times a day; after that it waits until you save the
  Connecteam settings again or click **Test connection**.
- Requests identify themselves as Staff Portal (in the User-Agent).
- The HR portal, once you turn it on, reads through this same connection with its own smaller
  share inside these limits: at most 6 requests a minute and 500 a day, and none once Staff
  Access has used half of its day, so the time clock always has room. If Connecteam refuses an HR
  request, or says "too many requests" to one, only the HR portal waits: the time clock checks
  carry on.
- Reports and tasks have a share of their own in the same way: at most 40 requests a minute and
  3,000 a day, and none once Staff Access has used half of its day. Nothing is asked until
  someone opens a report (or a form's tasks are due), and the same month is asked again at most
  every 10 minutes. Task checks (forms and boards) are planned at about 1,500 of the 3,000 at
  most, and changes the task sync sends at most 200 (see [Tasks](#tasks)), so reports keep the
  rest. If Connecteam refuses a report's request or says "too many requests", only
  reports and tasks wait; the time clock checks carry on. See [Reports](#reports).
- How the shares add up (Expert: 100 a minute and 10,000 a day for your whole account): Staff
  Access takes at most 60 a minute and 9,000 a day; inside that, the HR portal at most 6 and 500,
  and reports and tasks at most 40 and 3,000. Together, those two never take the last 10 of a
  minute, and they stop at half of Staff Access's day. So the time clock always keeps at least 10
  a minute and 4,500 a day, and your other integrations keep 40 a minute and 1,000 a day.

**Settings → Connections** shows how many requests Staff Access made today on Connecteam's line.
Open **Connecteam** for how many your account has left (once Connecteam has answered), how many
of today's were the HR portal's, and, while paused or stopped, why (in Connecteam's own words)
and when it tries again by itself. Remember that after 15 minutes without an answer, people who
must be clocked in can't sign in.

The Client Secret is never shown again after saving (leave the box blank to keep it), and it isn't
included in backups. After restoring a backup on a new system, enter it again (a migration file
brings it across, encrypted).

## Reports

Reports show in the staff app under **Reports**, next to the pages, for people who signed in with
their own PIN (on a role-based or shared device). A kiosk device nobody signs in to never shows
them. Who gets which report depends on their **department**.

1. Open the **Reports** tab in Staff Access.
2. For each report, tick **Turn on**, then pick the departments that may **view** it, and the
   departments that may **manage** it. People who manage a report can also upload its files
   (such as a Gusto time-off export) and change its settings; they see it too.
3. Click **Save**. Staff see the change within a few seconds.

Someone in several departments gets every report any of them may view (and manages it if any of
them may). Hidden departments aren't offered and give no access. Someone without a department sees
no reports. Access is checked on every request, so moving someone to another department, turning a report off or turning the person off takes effect at
once.

**Where the data comes from.** Reports read Connecteam (timesheets, schedules and forms) through
Staff Access's own Connecteam connection, only when someone opens one. They read a whole
calendar month at a time, so moving between a week, its pay period and its month asks nothing
new. Each month is kept for a day, and read afresh every morning at 7 AM while the Attendance
report is on (kept on disk, so a restart doesn't read it again); **Refresh now** at the top
of a report reads it again (at most every 2 minutes).
The first time a month is opened it can take a few seconds longer, because reports
may only ask 40 things a minute (see [Staying within Connecteam's API
limits](#staying-within-connecteams-api-limits)). For everything a report can show, the
Connecteam app needs **users.read**, **time_clock.read**, **schedule.read**, **forms.read**, and
optionally **jobs.read** (see [Connecteam permissions](#connecteam-permissions)). If one is
missing, the report says which, with Connecteam's own words. Scopes can't be added to an existing
Connecteam app: make a new app with all of them, and paste its keys in Settings.

**Uploaded files.** Files uploaded to a report are kept on the add-on (the newest 5 of each kind),
so everyone who opens the report sees the latest. Only text files are taken (CSV and the like),
up to 2 MB.

**Backups.** A Staff Access backup carries which reports are on, who may view and manage them, and
their settings. It doesn't carry uploaded files: they hold staff data and change every week, so
upload the newest one again after restoring a backup. A migration file carries them too.

### Adding a report

Each report is a folder of code in the add-on (`app/reports/<name>/`). Adding one needs no other
change: see [REPORTS.md](REPORTS.md), the guide for Claude or a developer.

## Attendance & Leave report

The first report on the staff app's **Reports** page. For a week, a pay period (1st to 15th, 16th
to month end) or a month, it shows who was late, left early, missed a lunch or a shift, or didn't
fill in a required form, and whether their sick time and PTO in Gusto cover the shifts they
missed. Turn it on and choose who sees it on the dashboard's **Reports** tab, like any report.

- **Where the numbers come from.** Timesheets (every time clock), schedules and form submissions
  come from Connecteam, read a whole month at a time when someone opens the report, so switching
  between Week, Pay period and Month is instant. Months are kept for a day and read afresh at 7
  AM; **Refresh now** reads them again, and the report says when its data was read. Reports have their own small Connecteam allowance, so they never slow the
  clock-in checks. If Connecteam refuses (a missing scope, the allowance used up, Connecteam
  down), the report says so in plain words and shows the rest. It needs the Connecteam
  permissions [Connecteam permissions](#connecteam-permissions) lists for reports.
- **Gusto balances.** In Gusto, open **Reports → Time Off Balances** and download it as CSV. On
  the report's **Leave balances** tab, someone who manages the report clicks **Upload Gusto CSV**.
  It's kept on the add-on, so everyone who can see the report sees the newest one (up to 256 KB:
  Gusto's own file is a few KB). Columns are
  found by their names, whichever layout Gusto uses (one row per person, or one per policy).
- **Matching people.** Connecteam people are matched by their Connecteam ID. Gusto has no
  Connecteam ID, so Gusto names are matched by name: "Patel, Dev" matches "Dev Patel", and common
  short names (Tom and Thomas, Nate and Nathan) match on their own, but only when one person fits:
  not when that person has a Gusto row of their own, or two Gusto names point at them. Only the
  name column is read (never a Manager or Approved by column). The Leave balances tab lists anyone
  who didn't match; add the pair under **Rules → Name matches** (one per line, Gusto name =
  Connecteam name).
- **Departments.** Each person shows under their Staff Access departments: the ones they have on
  the Staff tab when they're linked to Connecteam (chosen by hand or by sync, with or without a
  smart group), otherwise every department whose Connecteam smart group they're in. Their row
  shows the first of them, and the department filter finds them under each. People without one
  show under their Gusto title.
- **Who can see whom.** On the **Rules** tab, people who manage the report choose:
  - **Everyone sees all departments** (the default): everyone who may view the report sees
    everybody.
  - **Viewers see only their own department**: people who view the report see only the people in
    their own departments (their timesheets, schedules, forms and Gusto rows), and the name matches
    for them. People who manage the report still see everyone. The add-on leaves the others out
    before anything is sent, so they can't be seen by any means. Someone whose department isn't
    known (not linked here and in no smart group) is only shown to managers.
- **Rules and required forms.** The **Rules** tab holds the grace minutes, the lunch rule (Oregon:
  30 minutes for shifts of 6 hours or more), the low-balance flags, when someone needs action,
  which jobs are checked for early clock-outs, and the name matches. Under **Required forms**, pick
  Connecteam forms and say when each is due (every shift worked, once a week, or on a timer by
  whoever is on shift, such as an hourly tub check) and for whom. A timed form has its own checked
  hours (**Checked from** and **Until**, 6 AM to 10 PM unless changed) and how often in them, and
  how often the rest of the night: 0, the default for new forms, means not checked, and those
  hours are left off its grid. People who **manage** the report
  change these and click **Save**; everyone else sees them read-only.
- **Reading it.** Six numbers at the top, then everyone who worked or was scheduled, worst first
  (**Action**, **Watch**, **Clear**). Tap a name for their day-by-day punches. Tap a form for its
  grid, hour by hour for timed checks; tap a square to see who was on and who submitted.
- **Sharing it.** **Copy summary** copies a short text version. **Download CSV** saves the staff
  table. **Print or PDF** opens the browser's print window (choose **Save as PDF** there) with
  everyone's days, only the rows you opened, or just the summary.

Report settings (rules and required forms) are in backups and migration files. The Gusto file is
in migration files but not in backups, like other report uploads.

## Tasks

Everyone who signs in with their own PIN (on a shared or role-based phone or computer) has a
**Tasks** list in the staff app, next to the pages. It's each department's to-do list. A kiosk
phone nobody signs in to has no tasks.

**Who sees what.** People see the tasks for each of their departments (the ones set on the
Staff tab, or by their Connecteam smart groups; hidden departments don't count), the tasks for
**Everyone**, the tasks given to them, and the
tasks they made. Anyone who sees a task can change its status. Only the person who made a task,
or someone whose role has **Manage tasks** (Roles tab → the role → Tasks), can edit or delete it.
On a shared kiosk computer, someone without a role of their own gets the computer's role here.
All of this is checked by the add-on on every request, not by the phone.

**A task** has a title, optional notes, a department (or Everyone; a form's task can be in several),
optionally a person it's
given to, an optional due date and time, Normal or Urgent, and a status: **To do**, **In
progress** or **Done** (tasks marked Won't do before 0.12.19 keep it). Each task keeps who made it, when, where it came from (a
quick task or a Connecteam form) and a history of every change.

**On the phone.** Tasks are listed by status, with coloured status chips, and urgent and overdue
tasks marked. Tap **+ Quick task**, type a title and tap **Add task**: it goes to your own
department (the first of yours) unless you pick another (or Everyone), a person, or a due date. Tap a task's status
chip to move it on (To do → In progress → Done); press and hold it (or right-click on a
computer) to pick any status, or **Reopen** a done one. Tap a task to see its notes and history, and to edit
or delete it. The filters show only **Mine**, one department, or one status. Done tasks fold
away after a day (**Show older**). On a tablet or computer the list becomes a board with a column
for each status. The list updates by itself every few seconds.

**The bin.** A deleted task goes to the **Bin** (at the bottom of the list) for 30 days, where
the person who made it, or a manager who could see it (its department, Everyone, or given to
them), can put it back. Finished tasks are kept for a year.

**Deleting a department.** A department can't be deleted while a form still sends its tasks
there, or a Connecteam board's tasks belong to it: take it out of the form's departments (or pick
another department for the board) first. Tasks already in a deleted department show
as **Old department**: the people who made them or were given them still see them, and so does
anyone whose role has **Manage tasks**, so they can move them to another department.

**Tasks from Connecteam forms.** On the dashboard's **Tasks** tab, **Choose a form** lists your
Connecteam forms. For each one you pick:

- which departments its tasks go to (tick any number, or Everyone). A task in several departments
  is one task: everyone in any of them sees it, a department filter in the staff app finds it,
  and its card names them ("Maintenance, Front Desk"). Changing the departments moves the form's
  open tasks (To do and In progress, followed ones too) to the new ones; finished ones stay
  where they were. Editing a task in the staff app keeps its departments unless you pick one;
- which question's answer becomes the task's title (**List its questions** reads them from the
  form in Connecteam). Leave it blank for "Form name: who sent it";
- whether the person who sent it gets the task (when they're linked to Connecteam here).

Each new submission becomes one task, with every answer in its notes. Only submissions sent after
you chose the form count, and none becomes a task twice, even after its task is deleted.

**When Connecteam is checked, and what it costs.** Forms and the synced boards are checked on a
schedule you set on the Tasks tab (**Checking Connecteam**): in business hours, 9 AM to 6 PM in
the property's time zone, every 3 minutes, and outside them every 30 minutes. Each check of a
form or board is usually one Connecteam request (a form reads what was sent since an hour before
the last good check). With one form and one board that's about 180 checks in business hours and
30 outside, so about 444 requests a day (with each board's drafts read once an hour). With many
forms or boards, both intervals stretch alike, so all the checks together stay within about
1,500 requests a day of the 3,000 reports and tasks share. The Tasks tab shows how often and
about how many requests a day. Nothing is asked of Connecteam while no form is chosen, and reading the
form list costs a few more a day. The checks use the reports share of Staff Access's Connecteam
allowance, so they never hold up the time clock checks; if Connecteam can't be reached or the allowance is used up, the form's card
says so and it tries again later. Staff Access's Connecteam app needs the **forms.read**
permission (see [Connecteam permissions](#connecteam-permissions)).

**Forms that follow a status.** If a form has a **Status** manager field in Connecteam (like a
Maintenance Request with "Working on it" and "Done"), its tasks can follow it. On the form's card,
click **Follow a manager field**, pick the field, and choose the task status for each of its
values (and for no value chosen yet: To do or In progress). Then:

- every submission from the last month (31 days) that isn't done shows as one task for the form's
  departments, even ones sent before you chose the form. Done ones don't show;
- when its status changes in Connecteam, the task follows: Done there is Done here. An In
  progress task here stays In progress while Connecteam has no status yet;
- when someone changes the task here, its value is written to the submission in Connecteam (this
  needs the **forms.write** permission). Connecteam can't clear a status through its API, so
  moving a task back to To do (no status) leaves
  Connecteam's status as it is, and the task says so;
- the manager's **Note** shows on the task, read only.

These tasks are for everyone in the form's departments, and stay with their form: they aren't made as Quick
Tasks. Each check of a followed form reads back to its oldest open task (or the last two days
when none is open, never more than 31 days), afresh, so it's one request like any other form's.
Its first check after you choose the field reads the whole month.

**Photos.** Photos sent with a form (and image files) show on its task in the staff app: tap one
to see it big. The add-on fetches each photo from Connecteam itself, only from Connecteam's own
addresses, only pictures of at most 10 MB, and keeps a smaller copy for two weeks, so the phone
never loads anything from another site and a photo still shows after Connecteam's link to it
expires. One that can't be had says **Photo unavailable**.

**Two-way sync with Connecteam Quick Tasks.** Off until you turn it on, on the **Tasks** tab:

1. Check that Staff Access's Connecteam app has **quick_tasks.read** and **quick_tasks.write**
   (and **forms.write** for forms that follow a status). **quick_tasks.delete** isn't needed: it
   only lets the bin remove a task nobody is assigned to (Connecteam can't close
   those); without it they stay on the board. **Test** shows
   which it has, and reads the list of boards. A Connecteam app's permissions can't be changed:
   if one is missing, make a new OAuth app with all of them and enter its keys in Settings →
   Connections.
2. **Choose a board** for each department whose tasks should be in Connecteam (or one for
   Everyone), and pick the department its tasks belong to.
3. Tick **Sync tasks with Connecteam** and **Save**.

From then on:

- a quick task made here goes to the board of its department, for the person it's given to when
  they're linked to Connecteam (otherwise it's an unassigned draft there, which Connecteam
  requires). A department with no board keeps its tasks here, and they say so;
- changes to the title, notes, due date, person and status go both ways. To do and In progress
  are open in Connecteam (In progress is kept here); Done is completed;
- the bin archives the task in Connecteam, and putting it back unarchives it. An
  unassigned draft can't be archived in Connecteam, so it's deleted there instead and made again
  if it's reopened or put back;
- tasks made on those boards in Connecteam show up here for the board's department, given to
  the linked person. Each read takes only a board's open tasks, not its whole past (one made and
  finished in Connecteam between two checks doesn't come here). Archived or deleted in
  Connecteam, a task goes to the bin here;
- when the same thing is changed on both sides before they've caught up, the newer change wins,
  and the task's history says where each change came from. A change that came from Connecteam is
  never sent back;
- a task moved to another department here stays on the board it was made on. A due date can't be
  cleared in Connecteam through its API, so removing one here leaves Connecteam's as it is;
- tasks made before the sync was turned on, and tasks from forms, stay here only.

Changes are sent as they're made (at most 200 a day; more wait for the next day). The boards are
read for changes on the same schedule as the forms, and share their budget: each read costs one
request per 100 open tasks on the board, however many finished ones pile up. A board's drafts
(tasks nobody is assigned to) are read with it while it has open ones linked here, and otherwise
once an hour. A linked task that was open and has left the list is looked up by its ID (one
request for up to 50) to tell completed from archived or deleted. A task finished on both sides
isn't read again, so deleting it in Connecteam later leaves it here. If Connecteam refuses a
permission or asks for fewer requests, only the sync pauses, with the reason on the Tasks tab,
and it tries again later; **Test** or **Save** tries again at once. The time clock checks always
carry on. Nothing is asked while the sync is off.

**Backups.** Full backups and migration files carry the tasks (with their history and the bin),
the forms that make tasks, each role's Manage tasks, the sync's boards and each task's Connecteam
link. A restore turns the sync off, so a restored copy never sends anything to Connecteam on its
own: check the boards and turn it on again; it reads Connecteam's copy of each task first. A
backup from before tasks leaves the tasks here as they are.

## Seeing staff actions in Home Assistant

Every staff action is always recorded in **Staff Access → Activity**. You can also have them show
up in Home Assistant's own **Logbook**, in one of two ways.

**Logbook entries (on by default).** The add-on adds an entry for each action, for example
"Main Boiler set to 140° by Bob (Maintenance phone) via Staff Portal", for everyone, linked or
not. Turn this off under **Settings → Home Assistant logbook**.

**Link a phone or a staff member to a Home Assistant user.** Home Assistant then credits that
user directly,
the same as if they had tapped it in the Home Assistant app: "Porch Light turned off triggered
by action light.turn_off by Maintenance." The add-on's own logbook entry (with the device and
exact setting) is added as well, unless you turn logbook entries off.

1. In Home Assistant, create a user for it under **Settings → People → Users → Add user**
   (for a shared phone, something like *Maintenance*). Leave **Administrator** off.
2. In **Staff Access → Phones**, click **Link HA user** on the phone and enter that user's
   username and password. On shared phones, link each person on the **Staff** tab instead.
   A shared phone always uses the signed-in person's link and never the phone's, so Home
   Assistant can't credit the wrong person.

How it works and what to know:

- The password is used once to sign in and isn't saved. The add-on keeps the sign-in token on
  your Home Assistant box (in the add-on's data, which is included in backups). It shows up in
  that user's profile under **Refresh tokens**.
- The phone's role still decides what it can do. The Home Assistant user only changes whose
  name shows in the logbook.
- Users with two-factor login turned on can't be linked.
- If the user is deleted or the sign-in stops working, staff aren't locked out. The add-on
  switches back to its own access and adds logbook entries instead, and the phone shows
  **link broken** until you relink it.
- **Unlink** or deleting the phone signs the add-on out of that user's account.
- If you've turned on Home Assistant's IP banning for failed logins, typing the wrong password
  here several times counts against the add-on.

## Security settings

PINs are never stored as typed. Each PIN is scrambled with scrypt and its own random salt, so
they can't simply be read back. PINs are short, though, so someone with a copy of a backup or
staff export could work them out by trying every combination on their own computer, where the
wrong-PIN limits don't apply. Keep backups and exports private, like passwords (backups also hold
the activity log).

Under **Settings → Phone security**:

- **Require a PIN on every phone.** Phones without a PIN (other than shared phones, where each
  person has their own) stop working until you give them one (**Phones → Edit**). Turn this on if a lost phone shouldn't open without a PIN.
- **Only allow phones through your Cloudflare Tunnel.** Blocks the staff app on your local network
  (`http://<HA IP>:8787`), so phones can only reach it through your secure https address.
  - With the **Cloudflared add-on**, just tick it.
  - If `cloudflared` runs anywhere else (another computer, a VM, or a Docker container on another
    host), set the **Tunnel address** first, to the IP address that computer connects from. For
    a Docker container this is usually the Docker host's own LAN address (such as the VM's IP in
    Proxmox), not the container's internal address. The easy way: open the staff app once
    through your https address, wait a few seconds on Settings, and click **Use this address**
    next to the address shown under "Cloudflare traffic recently came from". Then tick the box
    and save.
  - Staff Access won't turn this on until it has seen tunnel traffic from an allowed address, so
    it can't cut every phone off. If the tunnel computer's address changes later, phones are
    blocked until you update the Tunnel address. Give that computer a fixed IP.

Always on:

- Only Home Assistant **administrators** can open Staff Access. Other Home Assistant users get
  "Only Home Assistant administrators can open Staff Access."
- Phones and computers with a PIN ask for it again after **Ask for PIN again after (minutes)**
  without being touched, even with the app left open on the screen. Phones and computers each have
  their own (under **Settings → Security**: **phones**, 15 minutes by default, and **computers**,
  5 minutes by default, since a desk is walked away from more often). Both are 1 to 1440 minutes.
- Each phone can send up to 60 actions a minute, and one action per device each second, so a
  stuck button or a script can't flood your equipment.
- Each phone or computer can try to sign in at most 15 times a minute, and shared ones lock after
  20 wrong PINs in a day. Checking a PIN is deliberately slow work, done off to the side so a
  stream of sign-ins can't slow everything else down.
- Connections that sit idle for 15 seconds, or that send a request too slowly, are closed, so
  nobody can tie the add-on up by opening lots of connections and doing nothing with them.
- A setup code can't be guessed (and each works once). Repeated wrong setup codes slow down that
  address, and 200 in 10 minutes from anywhere pause setup for everyone for a while.
- Double taps are ignored: the same command from the same phone within 3 seconds isn't sent
  again or logged again, and switching something on or off that already is does nothing. The
  app also ignores taps on a control while its last command is still being sent.
- Giving a role a whole area doesn't include a device's hidden or configuration controls (like
  "Restart controller" or "Child lock"). To give access to one of those, add it to the area by
  hand in **Settings → Areas**, or set it on the role directly.

## Tips for a locked-down maintenance phone

- Don't sign in to the regular Home Assistant app on staff phones. Staff Portal is all they need.
- On iPhone, **Settings → Accessibility → Guided Access** can pin the phone to the staff app.
- If you manage phones with MDM (Jamf, Intune, Mosyle, and so on), you can push the staff app
  as a web clip and restrict other apps.

## Running without Home Assistant OS

The app also runs as a plain Docker container. Set `HA_URL` (such as
`http://192.168.1.50:8123`) and `HA_TOKEN` (a long-lived token from an admin user), mount a
volume at `/data`, and publish port 8787 for staff. The admin dashboard listens on port 8099
and only accepts connections from the addresses in `ADMIN_ALLOWED_IPS` (default
`172.30.32.2,127.0.0.1`), so put it behind something you trust before exposing it. Every change
(anything other than reading) must carry the header `X-Requested-With: staff-portal`, as the
dashboard itself does, so another website can't make changes through an admin's browser.

## Backups, export and import

Everything is stored in the add-on's `/data` folder, which is included in normal Home Assistant
backups. The **Backup** tab adds backups of just Staff Access, and a way to move parts of it
between systems.

**Full backup.** **Download backup** saves one file with your settings, logo, areas, roles,
layout, phones, staff (with their roles and own settings), each phone's own arrangement, the
activity log and the wristband and key card history (every card made, and lost-card lock-outs).
The file is compressed (`.json.gz`); **Restore from a backup…** takes it, and plain `.json`
backups from older versions too. Restoring puts the setup back:

- Phones that were set up on this system keep working without re-pairing.
- A phone or computer that wasn't signed in on this system needs setting up again after the
  restore: one from another system's backup, one you've revoked since the backup was made, or
  one added to the file by someone else. The restore warning lists them first. So an old backup
  can't bring back a lost phone you revoked, and an edited file can't add a working phone.
- Phones still waiting for setup get new QR codes, since setup codes are never saved in files.
- The Connecteam Client Secret, the TT Hotel Client Secret and password, and the Door activity
  address are never in a backup, and a restore never changes them. Wristband settings, door
  names, whether each door is a public door, a guest room or a staff door (a backup from before
  0.9.0 leaves that as it is), roles' wristband rules, departments, each person's department
  and Connecteam sync details, and how many digits each PIN has (when that's known) are
  included, and so are departments' clock-in settings (a backup from before 0.9.0 has none, so
  each person's own setting counts). Whether remote lock-out was tested is never restored: test
  it again on the new system.
- Links to Home Assistant users are never saved in files. Restoring onto the same system keeps
  the links for phones and staff that still match; elsewhere, link them again.
- [Reports](#reports) (which are on, who may view and manage them, and their settings) and
  [tasks](#tasks) (with their history and the bin, the forms that make them, and each role's
  Manage tasks) are included. Files uploaded to reports aren't: upload the newest again after a
  restore (a migration file carries them). A backup from before 0.11.0 leaves the reports and
  tasks here as they are.
- **The activity log and the card history are merged, not replaced.** Entries in the backup that
  aren't here yet are added, and nothing here is removed, so restoring the same backup twice
  never adds anything twice. The usual limits still apply: the activity log keeps 50,000 lines and 180
  days, and the card history 2 years. A card job that was still running when the backup was made
  comes back as stopped, and a remote lock-out that was still waiting comes back as failed, so
  you can start them again. A backup from before 0.8.6 has no log or card history, and both stay
  as they are.
- Before replacing anything, Staff Access saves a copy of everything here, logs included. It's
  listed under **Saved before restoring** (the last 5 are kept), so **Restore this** undoes a
  restore.
- A damaged file or one from a newer version is refused, and nothing changes. Backups from 0.9.1
  on (which carry archived people, staff doors and contractor badges) are refused by 0.9.0 and
  older, rather than read wrongly.
- People archived here since the backup was made stay archived (see
  [Archiving people](#archiving-people)).
- The staff app carries on while a backup is made. A restore holds it up for a moment (about a
  second with years of card history), then everyone signs in again.

**The database.** Staff Access keeps everything in one SQLite file, `staff_portal.db`, with a
write-ahead log beside it (`staff_portal.db-wal`) that's put back into the file now and then,
when the add-on stops, and just before Home Assistant backs the add-on up. A change waits for the
log rather than the disk, so busy shifts don't hold anyone up on an SD card or eMMC; a power cut
can lose the last few changes, but never the database.

**Migrate to another install.** This moves everything to another install of the add-on, for
example when you switch Home Assistant from one add-on repository to another (such as from a test
branch to main): Home Assistant treats that as a different add-on, with its own empty data.
Unlike a backup, a migration file carries everything, so nothing needs pairing or typing again:

- phones and computers stay signed in, and setup QR codes already handed out keep working;
- staff PINs, phone PINs and links to Home Assistant users;
- the TT Hotel Client Secret, password and sign-in, the Connecteam Client Secret, both request
  counts, and the Door activity address (TT Hotel's Callback URL keeps working);
- the whole door list (with what each door is marked as), the activity log and the card history;
- the files uploaded to reports (such as the Gusto export).

Door activity itself isn't carried: the new install's list starts empty (TT Hotel keeps its own
six months).

The steps (they're also on the **Backup** tab):

1. On the old install, click **Create migration file**. Save the file, and write down the code
   shown with it: 20 letters and numbers in 5 groups of 4. **The code is shown only once.** Staff
   Access never stores it, and nothing else opens the file: if it's lost, make a new file.
2. Stop the old add-on: **Settings → Add-ons → Staff Portal → Stop**. Both installs use port
   8787, so only one can run at a time. Changes made on the old one after step 1 aren't in the file.
3. Install the new add-on and start it.
4. On the new one's **Backup** tab, click **Restore from migration file…**, choose the file and
   type the code (dashes, spaces and capitals don't matter).
5. Check that a phone or two, the front desk computers and the dashboard work. Staff sign in again
   with their PINs.
6. Uninstall the old add-on and remove its repository. Don't start it again: it knows the same
   phones and computers. The Cloudflare Tunnel needs no change, since it still points at port 8787.

A few rules keep a migration file safe:

- **It holds your secrets: treat the file and the code like passwords**, and keep them apart (not
  in the same email or folder). The file is encrypted (AES-256-GCM, with a key made from the code
  by scrypt), so without the code it's unreadable. Delete both once the migration is done.
- **It works for 24 hours** after it's made. After that, make a new one: start the old install
  again, click **Create migration file**, then stop it again.
- **It only goes onto a fresh install**, one with no staff, phones, computers, roles, areas,
  departments, card jobs or layout pages yet (changed settings or a logo don't count). If the new
  install has been set up, uninstall and reinstall it first. This, with the 24 hours, is what
  keeps a file to one use: the new install can't know whether the code was used somewhere else,
  but once a file is restored, that install isn't fresh any more.
- A wrong code and a changed or damaged file look the same: both are refused. So is a file made
  by a newer version of Staff Portal (update the new install first). Nothing changes when a file
  is refused.
- The new install saves a copy of what it had first (**Saved before restoring**), and the
  activity log says **Migrated from another install**. The old one logs **Created a migration
  file**. The code is never logged.
- A migration can't be undone by restoring that copy: it's of the fresh install, and a restore
  never removes the secrets, activity log or card history the migration brought. To start over,
  uninstall the new add-on and install it again (the old one still has everything until you
  uninstall it).

**Layout, roles, staff and departments** can each be exported and imported on their own:

- **Import layout** loads the file into the Layout tab so you can check it, then click
  **Save layout**. Whole-area items are matched to your areas by name.
- **Import roles**, **Import staff** and **Import departments** add to what's there. If a name
  already exists, you choose to **skip** it (the choice that's selected), **keep both** (the
  imported one gets "(imported)" after its name), or **replace** it. Areas, roles, pages and page
  groups are matched by name, and so are phones and departments (for staff). Anything with no
  match here is listed after the import. Pages with no match are left out, never replaced by every
  page.
- Department files carry each department's clock-in rule; replacing a department from a file
  made before 0.9.0 leaves its rule as it is. Staff files carry whether someone's clock-in setting
  was changed by hand, and people linked to Connecteam who are added from one keep that.
- Replacing staff uses the file's PINs, roles, pages and own settings, and anyone whose PIN
  changes is signed out. Someone you've turned off stays off, even if the file has them on, and
  someone you've archived stays archived. People archived in the file are archived here too. A file
  from before 0.7.0 has no roles or own settings for people, so replacing from one leaves theirs as
  they are.

Keep these files private. Staff and phone PINs are included only in scrambled form (they can't
be read back directly), but PINs are short, so someone with the file could work them out by
trying every combination offline.

## HR portal

The HR portal is a second, private website built into Staff Access, at its own address, for
employee files: documents, certificates, discipline and probation, and performance reviews. It
takes its people and departments from Connecteam. What each person sees depends on their role,
and everything they do is written to an audit log only HR admins see. Files go to a Google
shared drive, one folder per employee.

It's **off until you turn it on**. While it's off, nothing changes for anyone, nothing extra is
asked of Connecteam or Google, and its address only says "The HR portal is turned off."

### Before you start

1. **Turn on Connecteam** in **Settings → Connections**. The HR portal reads its people from the
   same user list Staff Access already uses.
2. **Add an address for it in your Cloudflare Tunnel**, such as `hr.yourdomain.com`, pointing at
   port **8788**. With the Cloudflared add-on, add to its configuration:

   ```yaml
   additional_hosts:
     - hostname: hr.yourdomain.com
       service: http://172.30.32.1:8788
   ```

   It must be its own address, not the staff app's. The HR portal refuses anything that doesn't
   come through your tunnel (there's no setting to turn that off), and anything sent to another
   address.
3. **In the Cloudflare dashboard, for the HR address only:**
   - add a Cache Rule **Bypass cache**;
   - turn off **Rocket Loader** and **Email Obfuscation** (they would break the pages);
   - optionally, block countries other than the US, and add a rate limit on the sign-in page.
   Don't put Cloudflare Access in front of it: most staff have no company account to sign in
   with.
4. **Turn on Home Assistant's backup encryption** for every backup location. HR records are in
   Home Assistant's backups (see [The HR key and backups](#the-hr-key-and-backups)).
5. **Connect Google Drive** for employee files (see [Google Drive for HR
   files](#google-drive-for-hr-files)). Connecteam needs nothing more: the HR portal reads its
   people through the Connecteam connection you already have (see [Connecteam and the HR
   portal](#connecteam-and-the-hr-portal)).

### Turning it on

Open the **HR portal** tab, tick **Turn on the HR portal**, type the HR address and click
**Save**. The tab then shows:

- **HR admins.** Click **Add HR admin** and choose yourself (people come from Connecteam). Two is
  best, you and your HR lead, so neither depends on the other. Only you can make someone an HR
  admin, and only here.
- **Setup QR code**, next to each HR admin. Scan it with that person's phone: it opens the HR
  portal's setup page (see [Setting up an authenticator](#setting-up-an-authenticator)). It works
  once, for 15 minutes.
- **Sync people now**, and when people were last read from Connecteam.
- **Emergency controls** and **Security events** (below).

Turning it off signs everyone out of it. Nothing in it is deleted, and turning it on again
brings it all back.

The tab also shows how its two connections are doing, **Google Drive (HR files)** and
**Connecteam** (read only, the same connection as Staff Access's), with a link to each in
**Settings → Connections** (below).

### Google Drive for HR files

The HR portal keeps employee files (documents, certificates, signed write-ups and final reviews)
in a Google **shared drive**, one folder per employee, with a subfolder for each: **Documents**,
**Certificates**, **Discipline** and **Reviews**. Staff Access reaches the drive through a small
script that runs in **your own Google account**. There's no Google Cloud project, no key file
and nothing to change in the Google Workspace admin console. You set it up once, under
**Settings → Connections → Google Drive (HR files)**, before or after turning the HR portal on.

Until it's set up, nothing is sent to Google. After that, Google is only asked when you open,
save or test this connection, or when the HR portal works with files.

#### Setting it up (about 5 minutes)

1. In Google Drive, create a shared drive named **HR Employee Files**. If you like, create a
   second one, **HR Restricted**, for restricted records (see [The drives](#the-drives)).
2. In Staff Access, open **Settings → Connections → Google Drive (HR files)** and click **Copy
   the script**. The script has a secret made for your Staff Access written into it.
3. Go to **script.google.com**, signed in **only** as the owner (sign out of other Google accounts,
   or use a private window: the editor otherwise opens as whichever account signed in first), and
   click **New project**. Delete what's there, paste the script and save it (Ctrl+S). You can name
   the project, for example "Staff Access HR". If the editor doesn't respond, refresh the page.
4. Click the gear (**Project Settings**) and tick **Show "appsscript.json" manifest file in
   editor**. Back in the editor (`< >`), open **appsscript.json**, replace everything in it with
   what **Copy the settings file** copies, and save. This turns on Google Drive for the script and
   asks for full Drive access. (Don't use **Services → +** instead: Google then guesses the access
   from the code and may give the script read-only access.)
5. Choose **authorize** in the list of functions at the top and click **Run**. Google asks you to
   allow the script your Drive, your Google Docs (to read the text on cards) and your email
   address (to show which account it is). **Tick Select all**, or every box including Google
   Drive, then **Continue**. If Google says it hasn't verified the app, that's because you made it
   yourself: click **Advanced**, then **Go to** your project. The log at the bottom then lists the
   checks; lines starting **OK** mean that part works (the drive checks pass once you've picked the
   drives in step 7).
   Then click **Deploy → New deployment**. Choose the type **Web app**, set **Execute as: Me** and
   **Who has access: Anyone**, then click **Deploy**.
6. Copy the **Web app URL** (it ends in `/exec`), paste it into Staff Access and click **Save**.
   Staff Access checks it straight away and lists your shared drives.
7. Pick **HR Employee Files** (and the restricted drive, if you made one), click **Save**, then
   **Test connection**.

Deploy it from the **owner's** Google account, one that isn't going away: the script works as
whoever deployed it, and stops if that account is removed.

The folded line shows **Connected as** your email address, **Not set up**, **Not checked yet**,
**Pick the shared drive**, **Script out of date**, or a problem in a few words: **Secret doesn't
match**, **Access refused**, **Google not answering**, **Google busy**, **Unexpected reply** or
**Test failed**. Open it for the details.

#### Test connection

**Test connection** runs four checks, each with a plain result:

- **Sign-in**: the script answers and accepts Staff Access's secret, and which Google account it
  works as.
- **Shared drive**: the drives you picked can be used.
- **Make and remove a test folder**: in the HR drive.
- **Read text from a test image**: Google Drive reads the words on a small picture, as it will
  read the dates on staff cards.

#### What the script can and can't do

- It **only works inside the shared drives you picked**. It checks every file and folder it's
  asked about, and refuses anything elsewhere: your My Drive, other shared drives, Gmail, and
  everything else in your account.
- In those drives it can make folders, upload, rename, move and download files, and move files
  to the drive's trash (Google empties it after 30 days). It never deletes a file for good,
  except its own temporary copies (below) and test folders.
- To read a card, Google Drive turns a copy of the photo into a Google Doc in a folder named
  **_OCR scratch** in the HR drive, Staff Access takes its text, and the copy is deleted at once.
- To make a PDF (a signed write-up or a final review), Staff Access sends plain text laid out as
  a simple page; Google Drive turns it into a Google Doc in **_OCR scratch**, saves that as a PDF,
  and the Doc is deleted at once. The PDF is then kept in the person's folder like any other file
  (see [PDFs of write-ups and reviews](#pdfs-of-write-ups-and-reviews)).
- It **only answers Staff Access**. "Who has access: Anyone" lets Staff Access reach it without a
  Google sign-in, but every request must be signed with the secret only your Staff Access has,
  and each one works for 5 minutes and only once. Opened in a web browser, it just says it's
  working. The secret never leaves Staff Access except inside the script you copy, so keep the
  script private.
- Google's limits: a script runs for at most 6 minutes, and reading cards is limited to a few
  thousand a day. Staff Access sends at most 2 requests at a time, and files of up to 25 MB.

#### The drives

- **HR Employee Files**: keep its members to the owner, the HR lead, the CEO and the GM.
  Managers and employees never need Drive access: anyone in the drive sees every folder, which
  would get around the HR portal's rules.
- **Restricted records** (I-9s and reasonable accommodation) are kept apart. Best: a second
  shared drive, **HR Restricted**, with only you and the HR lead. Otherwise they go in a
  **Restricted** folder in the HR drive, which every member of that drive can open.
- An I-9 must be kept for 3 years after hire or 1 year after someone leaves, whichever is later.
  Keeping records 5 years after leaving covers that.

#### If Test connection fails

- **"The script doesn't have full Google Drive access"** (or Google saying "You do not have
  permission to call drive.files.create"): the script was allowed less than it needs, usually
  because a box was left unticked on Google's permission screen, or the settings file wasn't
  pasted before it was first allowed. Fix:
  1. Check **appsscript.json** holds the settings from **Copy the settings file**.
  2. Go to **myaccount.google.com/connections** (Google Account → Security → Third-party apps &
     services), open your script project and choose **Delete all connections** (or **Remove
     access**).
  3. In the script editor, choose **authorize**, click **Run**, and tick **Select all** when
     Google asks.
  4. **Deploy → Manage deployments → Edit (the pencil) → Version: New version → Deploy**.
  5. **Test connection** again.
- **Signed in to Google as someone else**: the Sign-in check names the account the script runs
  as. If it isn't the owner's, open script.google.com signed in only as the owner, paste and deploy
  the script there, and paste its new Web app URL into Staff Access.
- **Google Drive refused** with Google's own words in brackets: those words say why. The account
  that deployed the script must be a **Manager** (or Content manager) of the HR shared drive.
- The **Activity** tab lists every check that failed, not only the first.

#### If "Anyone" isn't offered

On Google Workspace, the **Who has access** choices follow your Drive sharing settings: **Anyone**
is hidden when sharing outside your organization is turned off. The bridge can't work without it.
Ask whoever manages your Google Workspace to allow sharing outside your organization for your
account (Google Admin console: **Apps → Google Workspace → Drive and Docs → Sharing settings**).
They can allow it for an organizational unit with only your account in it, so nothing changes for
anyone else.

#### When the script is out of date

After an update, Staff Access may need a newer script. The connection then says **Script out of
date**. (Script version 2 adds making PDFs of write-ups and reviews. Files keep going to Drive
with version 1 meanwhile; PDFs wait until the new version is deployed. Version 3 shows Google's own
words when Drive refuses something, and Test connection no longer fails just because Google won't
delete its test folder. Version 4 adds the **authorize** function and a plainer message when the
script is missing Drive access.) To update it (the Web app URL stays the same):

1. Click **Copy the script**.
2. In script.google.com, open the project, select everything in **Code.gs**, paste over it and
   save. Leave **appsscript.json** as it is.
3. Click **Deploy → Manage deployments**, then **Edit** (the pencil). Under **Version**, choose
   **New version**, then click **Deploy**.

Pasting alone isn't enough: Google keeps running the deployed version until you deploy a new one.

#### Making a new secret

If you think someone else saw the script, click **Make a new secret**. The script in Google stops
working at once: paste the new one there and deploy a new version, as above.

If you ever make a new project instead of updating the old one, paste its new Web app URL into
Staff Access and click **Save**. Staff Access tells the new script which drives to use, and it
finds the folders the old one made.

The Web app URL, the secret and the drives you picked are never in Staff Access's own backups (a
restore keeps the ones saved). A migration file carries them, encrypted, so the script keeps
working after a move.

### Connecteam and the HR portal

The HR portal only **reads** from Connecteam: its people (names, mobile numbers, emails, kiosk
codes and profile fields), its archived users and its smart groups. It sends no chat messages
and copies nothing to Connecteam, so no write permissions are needed, and there's nothing to set
up for it: it reads through the one **Connecteam** connection in **Settings → Connections**
(its app needs **users.read**, which Staff Access needs anyway). Google Drive keeps every file.

Its reads count in a small share of their own inside Staff Access's limits (see [Staying within
Connecteam's API limits](#staying-within-connecteams-api-limits)). The Connecteam connection
shows how many of today's requests were the HR portal's, and the HR portal tab shows whether it's
reading or waiting. If Connecteam refuses an HR request or says "too many requests", only the HR
portal waits; the time clock checks are never affected. While the connection itself is stopped
(Connecteam refused Staff Access's keys or a permission), the HR portal waits too, until it
works again.

### Signing in

Everyone signs in on one page with:

1. **Mobile number**, as it is in Connecteam. HR can set a **sign-in name** for the few people
   without one.
2. **PIN**: their **Connecteam kiosk code**. The HR portal only keeps a scrambled copy of it
   (never the code itself), refreshed every hour from Connecteam. When someone's kiosk code
   changes, they're signed out of the HR portal. Someone without a kiosk code can't sign in, and
   HR sees "No kiosk code in Connecteam" on their file.
3. **Authenticator code**: the 6-digit code from an authenticator app (Google Authenticator,
   Microsoft Authenticator or similar), for those who use one. Each code works once.

A failed sign-in always says the same thing, "That didn't work. Check your number, PIN and
code.", whichever part was wrong, and the page never lists names.

**Who must use an authenticator code** is an HR setting: everyone; **HR admins, directors and
managers** (the default); HR admins only; or nobody. Anyone else can turn one on for themselves
in **My account**.

**Locks.** 5 wrong tries in a row lock someone's HR sign-in for 15 minutes; 15 in a day lock it
until an HR admin unlocks it. A lock never signs out someone already signed in, and it's counted
apart from the staff app's PIN locks. Too many failures from one internet address, or from
everywhere at once, pause new sign-ins for a few minutes.

**Staying signed in.** People are signed out after **15 minutes** without use (HR can set 5 to
30) and after **8 hours** whatever happens. There's no "remember this device", because
front-desk computers are shared. Each person can be signed in on up to 5 devices; **My account**
lists them, with **Sign out** for each and **Sign out everywhere**. The home page shows when and
from where they last signed in, so misuse gets noticed.

**Sensitive actions** (resetting or unlocking someone, ending access, changing roles,
departments or HR settings, turning off an authenticator, and changing anyone's points) ask for a
fresh authenticator code first, or the PIN for someone without an authenticator.

### Setting up an authenticator

**People who must use one** set it up with a one-time setup code from an HR admin (or, for HR
admins, from you on the dashboard), so someone who only knows a kiosk code can't add their own
phone to a manager's account. On the person's page, HR clicks **Setup QR code** (works once, for
15 minutes, on HR's screen) or **Setup link** (works once, for 72 hours; send it by hand in a
Connecteam chat). Setup takes about two minutes:

1. Confirm the name.
2. Type the PIN (the kiosk code).
3. Scan the QR code into the authenticator app (or type the key shown under it).
4. Type the code the app shows.
5. Save the 8 **recovery codes**. Each works once instead of a 6-digit code, if the phone is
   lost.

**Everyone else** can turn one on in **My account → Turn on**, re-typing their PIN. That one
stops counting once they must use one (HR makes them a manager or director, you make them an HR
admin, or HR's setting comes to cover them): it's removed, they're signed out, and they need a
setup code like everyone else who must use one.

**New phone.** In **My account**, **Set up a new phone** (after a fresh code from the old one).
The old phone stops working. Someone who lost their phone signs in with a recovery code: it's
logged, HR admins are told in their inbox, and the person is asked to set up their
authenticator again.

**Reset by HR.** **Reset sign-in** on someone's page removes their authenticator and recovery
codes and signs them out everywhere. They then sign in with their kiosk code, or set up again
with a new setup code if their role needs one.

### Roles

| Role | Who gives it | Who they see |
|---|---|---|
| **HR admin** | You, on the HR portal tab | Everyone, including former employees, HR settings and sign-ins |
| **Director** | An HR admin | Everyone except HR admins and other directors (unless they're the director's direct reports in Connecteam) |
| **Manager** | An HR admin, with the departments HR assigns | The people in those departments, but not other managers, directors or HR admins unless they're their direct reports |
| **Employee** | Automatic | Only their own file |

Rules over every role:

- Nobody acts on their own file except as an employee. Another HR admin, or you, handles an HR
  admin's own file.
- Access follows the person: when someone moves department in Connecteam, their old manager loses
  access at once and the new one gains it. A role change counts from the person's next click.
- Every piece of text has a level saying who can see it, and text someone can't see never leaves
  Home Assistant.
- Records, once issued, are never rewritten: corrections are added as new entries, with a reason.
- Nobody approves their own work.

Being a manager in Connecteam doesn't make someone a manager here: HR chooses, in **HR settings
→ Roles**, then ticks each manager's departments (one manager per department can be the
**primary** one).

### People from Connecteam

Every hour the HR portal reads Connecteam's user list, the same one Staff Access already reads,
so it usually costs no extra request. Around sign-ins it reads it at most every 5 minutes, so a
new hire can sign in soon after being added. Once a day it reads Connecteam's archived users and
the names of its profile fields. For each person it keeps:

- name, mobile number and email (the last two encrypted);
- departments: the Staff Access departments, which follow your Connecteam smart groups;
- **start date**, from the profile field HR picks in **HR settings → Connecteam profile fields**
  (until then, a field called "Employment Start Date"). If it's empty, the day they were added
  to Connecteam is used, marked **estimated**, and HR gets a to-do to confirm it. Check that
  dates are read the right way round (month/day or day/month) with one test employee;
- **direct manager**, from the "Direct manager" profile field, shown as a hint;
- whether they're archived.

Kiosk codes, birthdays and other profile fields are never stored.

**When someone leaves.** Archived in Connecteam (or gone from its list), their HR access ends
within the hour, and their records stay. For same-day departures, HR has **End HR access now**
on their page; any setup code or link made for them before stops working too. Coming back isn't
automatic: if someone is restored in Connecteam, HR clicks **Allow sign-in again** (or makes them
a new setup code).

**Safety catch.** If Connecteam suddenly lists far fewer people (nobody, or more than a quarter
fewer), nobody is cut off: HR and the dashboard get a warning instead, and the next sync tries
again.

**Connecteam's request limits.** The HR portal only reads, in one small share inside Staff
Access's: at most 6 requests a minute and 500 a day, and none once Staff Access has used half of
its day, so clock checks always have room. Normal use is a few requests a day. If Connecteam
refuses an HR request (a missing permission or plan feature), only that HR task stops, and if it
says "too many requests", only the HR portal waits: Staff Access's own time clock checks keep
running.

### A person's page

Everything about one person is on their page in the HR portal, in this order, each part showing
only what the person looking may see:

1. **Details**: name, departments, start date and direct manager (contact details for HR and the
   person themselves).
2. **Documents**: their files (see [Documents](#documents)).
3. **Certificates**: the cards their job needs and their status (see
   [Certificates](#certificates)), when they need one or have one on file.
4. **Probation**: their introductory period or disciplinary probation (see
   [Probation](#probation)).
5. **Discipline**: points, incidents and write-ups (see [Discipline and
   probation](#discipline-and-probation)). Employees see it once the point system is set up or
   they have a write-up.
6. **Reviews**: their performance reviews (see [Performance reviews](#performance-reviews)).
7. **Sign-in** (HR admins only): set up, reset or unlock their sign-in, and end their access.

An HR admin sees their own page only as an employee would.

### Documents

Each person's page in the HR portal has a **Documents** section: the files in their file that
the person looking may see, with **Open** (photos open in the portal) or **Download** (PDFs are
always downloaded, never opened inside the portal). Every file is kept in Google Drive, in the
person's folder, with the date first in its name, such as `2026-10-25 Handbook
acknowledgement.pdf`, so folders sort in order.

**Kinds of document** (HR settings → Kinds of document). Each kind has who can see it, its folder
in Google Drive, and who may add one. HR can rename kinds and add more. The kinds at the start:

| Kind | Who can see it | Who may add one |
|---|---|---|
| Signed policies | The employee, their managers, directors and HR | HR |
| Training records | The employee, their managers, directors and HR | Managers, directors and HR |
| Hiring paperwork | Directors and HR | HR |
| Other | HR admins only | HR |
| I-9 | Restricted: HR admins only, kept apart | HR |
| Reasonable accommodation | Restricted: HR admins only, kept apart | HR |

Certificates, signed write-ups (with photos of pages signed on paper) and final reviews are kinds
too, but they're shown in their own sections, which decide who sees them; HR can rename them and
change their Drive folder only. Changing who can see a kind takes effect at once. Changing its
Drive folder only affects new files.

**Adding a file.** Open someone's page, choose the kind, a title and the date on the document,
and the file: a photo (JPEG, PNG or WebP) or a PDF, up to 15 MB (25 MB for HR, for scanned
paperwork). The type is checked from the file itself, not its name. Photos are saved afresh
without their camera details or location; PDFs are kept exactly as they are. Files that anyone
but HR adds wait for HR to **Accept** or **Don't accept** them (with a reason, which whoever
added it sees). A file that isn't accepted never reaches Google Drive and is removed after 30
days. Nobody checks their own upload: an HR admin's own file is checked by another HR admin.

**Restricted files (I-9, Reasonable accommodation).** Only HR admins see them; managers,
directors and the employee never learn one exists. On a person's page HR sees how many there are,
and **Show restricted files** lists them. Every look is in the audit log. They're kept apart in
Google Drive: in the **HR Restricted** shared drive if you picked one (see [The
drives](#the-drives)), otherwise in a **Restricted** folder in the HR drive, and HR settings warns
that every member of that drive can open it.

**Getting files to Google Drive.** Nothing is copied to Connecteam (the HR portal only reads
from it): Google Drive keeps every file. Each file is kept encrypted on the Home
Assistant computer until it's in Google Drive, then 30 more days for quick opening; after that
it's fetched from Drive when someone opens it. Each file shows where it is, for HR: **Google
Drive: Sent**, **Waiting** or **Needs attention**. If Google doesn't answer, the file is tried
again after 1 minute, 5, 30, 2 hours and 6 hours, then daily. After 8 tries it goes on HR's
**Needs attention** page with **Retry**. A retry never makes a second copy. While Google Drive
isn't set up, or its script is out of date, files simply wait (without using up tries), and HR's
to-do list says so. A migration file can't be made while files are waiting to reach Google Drive
or waiting for HR.

**Deleting.** HR deletes a file with a fresh 6-digit code (or PIN). It stays in the person's
**Recycle bin** for 30 days and can be **Restored**; then it's removed from the computer and
moved to the shared drive's trash, which Google empties 30 days later. The audit log keeps the
line.

**Legal hold.** On a person's page, HR can **Put on legal hold** (with a reason, and a fresh
code). Nothing in that file can then be deleted (certificate photos included), the recycle bin
isn't emptied for them, and they're left off the retention list, until HR lifts it.

**After someone leaves.** Records are kept 5 years after they leave (HR settings → Records).
After that the person is listed on **Needs attention** under **Past the time records are kept**,
and HR gets a note in the inbox. Nothing is deleted by itself: HR reviews the person's files and
presses **Delete their files** (a fresh code; they go to the recycle bin first). When HR confirms
someone has left, **Move Drive folder to Former employees** on their page moves their folder
there; it never happens by itself. When someone's name changes in Connecteam, their Drive folder
is renamed overnight. Folders are always found by their Connecteam ID, never by name, so two
people with the same name never get mixed up.

**Needs attention** (HR admins, in the menu) lists files waiting for HR to check, uploads and PDFs
that couldn't be sent or made, those still waiting to be tried again, records past the time
they're kept, and legal holds. HR's own file is left off it:
another HR admin sees to it.

#### PDFs of write-ups and reviews

Signed write-ups and closed reviews become PDFs, made by Google Drive through the script and kept
like any other file, in the person's **Discipline** or **Reviews** folder. They're made in the
background as soon as the write-up is signed or the review closed. While Google Drive isn't set
up, or its script is out of date, they wait (without using up tries) and are made once it is. If
Google fails, they're tried again like uploads, and after 8 tries they go on **Needs attention**
with **Retry** (also on the write-up itself, **File the PDF again**). Asking again never makes a
second copy. A PDF holds only what the employee was shown: never managers' or HR's notes, and
never an end-of-introductory-period recommendation.

### Certificates

The HR portal keeps track of the cards people's jobs need, such as the **Oregon Food Handler
Card** and the **OLCC Service Permit**, reads the dates off a photo, and reminds people before
and after they run out.

**Who needs which card.** HR sets this up in **HR settings → Certificates**. It starts with:

| Certificate | Lasts | Needed within | While it's expired or missing |
|---|---|---|---|
| Oregon Food Handler Card | 3 years | 30 days of starting | "May not handle food" |
| OLCC Service Permit | 5 years | 7 days of starting | "May not serve alcohol", also while it's due |
| Food manager certificate | As printed | (nobody by default) | Accepted in place of a Food Handler Card |

- The first time, the departments whose names contain "Pub" (both cards) or "Front Desk" (OLCC)
  are ticked as a suggestion. **Nobody needs a card until HR checks the departments and clicks
  Save**; until then HR's to-do list says so.
- For each certificate HR can change how long it lasts, the days a new hire has, the heads-up
  days before it runs out (for example 60, 30, 14, 7), the message shown while it's lapsed, the
  departments and individual people who need it, and other certificates accepted in its place.
- **Add a kind of certificate** (such as CPR and First Aid) at the bottom of the section. Untick
  **Track this certificate** to stop tracking one; its cards stay on file.
- **Waive** a certificate for someone (on their page, HR only): a reason and an end date are
  needed, and only HR sees the reason.
- These changes, waivers and the spreadsheet download ask for a fresh code (or PIN).

**Adding or renewing a card.** On a phone, **My certificates → Add my card** (or **Renew**):

1. Choose the card, then **Take a photo** (the camera opens; iPhones are asked for a JPEG). A
   photo in Apple's HEIC format is refused with "take the photo again with the camera button".
   **Choose a file** takes a photo or the PDF from an online course.
2. Add the back too if it has dates on it. Both sides become one picture.
3. Photo tips on the screen: the card flat on a table, no glare on the lamination, filling the
   frame about 6 to 8 inches (15 to 20 cm) away.
4. A few seconds later: "We read: expires 6/14/2029. HR will confirm." The card shows **Waiting
   for HR**.

A manager or director can add a card for someone on their team (helpful for staff without a
smartphone), and HR for anyone. HR's own uploads go straight to the check screen. An HR admin's
own card waits for another HR admin.

**How the date is read.** The photo is cleaned (its location and other details removed) and kept
on the Home Assistant computer, encrypted. A smaller copy (2 MB or less) goes to Google Drive's
free reader through the script (see [Google Drive for HR files](#google-drive-for-hr-files)),
which sends the text back and deletes its copy at once. The text read is kept encrypted until HR
decides, and for 30 days at most. **While Google Drive isn't set up, HR types the date.**

**HR checks every card.** On **Certificates**, the **to check** button lists them, oldest first
(HR's to-do list and inbox say when one comes in). The check screen
shows the photo (zoom in, zoom out, turn) next to the text Google read, with the dates
highlighted and any warnings (the name isn't on the card, it looks like the OLCC course
certificate rather than the permit, a temporary permit, someone under 21). HR then:

- **Confirm**, after correcting the date if it's wrong, with the issue date, card or permit
  number, "temporary permit", "under 21" and **Checked with OLCC** (or the issuer) as needed. The
  card then goes to the person's **Certificates** folder in Google Drive, named like
  `2026-10-25 OLCC Service Permit - expires 2031-10-24.jpg` (nothing goes to Connecteam). The
  old card is marked replaced, reminders start again, and the
  person is told.
- **Don't accept**, with a reason the person sees ("The photo is blurry. Retake it."). The upload
  never reaches their folder and is deleted after 30 days (kept while the person is under a
  legal hold).

**Statuses.** Each card someone needs shows one of: **Valid**, **Expiring** (inside the first
heads-up), **Expired**, **Due** (a new hire still inside their days), **Missing**, **Waiting for
HR** or **Waived**. A lapsed card's message ("May not serve alcohol") shows on every board and
the person's page. Nothing else happens automatically: schedules aren't changed.

- **Certificates** (HR): everyone's cards, filtered by department, certificate, status ("Needs
  attention") or "runs out within 60, 30, 14 or 7 days", and **Download (spreadsheet)** as CSV.
- **Team certificates** (managers and directors): the same for the people they see, with banners
  such as "2 OLCC Service Permits expired: may not serve alcohol". Managers see statuses and
  dates, never the photos or card numbers.
- **My certificates**: everyone's own cards, photos and what HR said.

**Reminders** are sent in the nightly run (2:15 AM Pacific); running it twice sends nothing more.

- **Expired or missing:** the person, their managers and HR are reminded every 2 days (**Remind
  about expired or missing cards every**, 1 to 14 days). The person gets one message per card;
  managers and HR get one message each listing how many.
- **Heads-ups before a card runs out**, at each heads-up day (by default 6 months, 60, 30, 14
  and 7 days for OLCC; 60, 30, 14 and 7 days for the others), in the person's inbox. Managers
  and HR get one message each listing how many.
- **A new hire** is told once which cards they need and by when.

**Connecteam.** Cards are never copied to Connecteam (the HR portal only reads from it), so
Connecteam doesn't remind anyone about them: the portal's own heads-ups do. Tell staff to add
their cards in the HR portal.

Once confirmed, a card's photo opens as the document it became (from this computer for 30 days,
then from Google Drive), for the person and HR only, and every look is in the audit log. A
migration file can't be made while cards are waiting for HR (their photos are only on this
computer): check them first.

### Discipline and probation

The HR portal runs your points-based discipline system: incidents, write-ups that stop counting
after a while, appeals, and probation. The point system itself (offences, points, steps and when
points expire) is set up by HR in the portal (see [The point
system](#the-point-system-discipline-setup)). **Until HR publishes a point system, nobody can
record an incident**, and the Discipline sections say so. Probation works from the start.

The portal recommends; people decide. Reaching a step makes a task for a person, never a warning
by itself, and a termination is always HR's decision.

#### The point system (Discipline setup)

Your written point system is entered as settings, not programming, and saved in versions. HR
admins set it up under **Point system** in the HR portal (**HR settings → Discipline** links to it
too). Everyone else can read the version in force under **Point system**.

**Until a version is published, nothing counts points.** Managers can't record incidents, the
Discipline pages say the point system isn't set up yet, and HR has a to-do about it. The portal
ships the sample from the design as a draft marked "not in use", so the screens aren't empty:
one Discipline total; Coaching at 3 points, Verbal at 5, Written at 7, Final written at 9 and
Termination review at 11; points stop counting after 12 months or when their write-up ends,
whichever comes first; HR approves every incident; a 90-day introductory period where 3 points
means Final written and 5 points (or any serious offence) means Termination review.

**Setting it up:**

1. **Load it.** Either import the spreadsheet (**Import a spreadsheet**: choose the CSV file, or
   paste straight from a spreadsheet), or edit a draft on screen. The spreadsheet has sections
   (Settings, Point totals, Steps, Ladder, Escalation, Offences, Repeats, Probation, Probation days
   by department, Probation ladder, Probation offences and Protected leave); **Export spreadsheet**
   on any version shows the layout. Anything the import had to guess comes back as a list of
   questions, such as "Rolling window: '12' has no unit, so it was read as 12 months. Is that
   right?"
2. **Check it on the setup screens.** Offences are an editable table (name, points, and the range
   a manager may choose; **More** for the category, attendance, serious, minimum step, repeat rule
   and the handbook wording quoted on write-ups). Each ladder is a bar showing which point total
   leads to which step. Then steps (how long each stays active, HR meeting, HR only), escalation,
   when points stop counting, earning points back, probation policies, what employees and managers
   see, what HR approves, and the protected-leave list. Problems are listed in plain English at the
   top; a draft can be saved with problems, but not published.
3. **Test it in the simulator.** Type made-up incidents for a test person (dates, offences,
   protected leave, a manager's or HR's points, the day HR approved it) and press **Run**. You get
   a chart of points over time, with the thresholds and the probation period, and a timeline of
   every change: points added, steps due and why, points that stop counting, credits. Nothing in
   the simulator reaches anyone's file.
4. **Publish it with the day it starts.** It asks for your 6-digit code (or PIN) again. From that
   day, new incidents are scored with it. The first version can start on an earlier day, to load
   past incidents; later ones start today or later, after the one before. A version that hasn't
   started yet can be **withdrawn**.

**Versions.** A published version is never changed: to change the point system, make a new draft
from it, test it and publish it. Old versions are kept, and every incident keeps the version it was
scored under, so earlier incidents keep their points and expiry. To move people onto the new
rules, use **Re-score people**: choose who, look at the preview (each incident's points and the day
it stops counting, before and after), give a reason and confirm with your code. Each change is
added as an adjustment, so the original stays visible. Points HR set for an incident (at approval,
by changing them, or on appeal) are kept, and the preview says so.

**How points are counted.** Every change to someone's points is a dated line, like a bank
statement: an incident, an adjustment, a credit or a void. Totals are always added up from the
lines, for any day ("as of"). Points can be whole, half or quarter points, and add up exactly.

- **One incident:** the offence's points; a repeat within its period adds points (for example the
  3rd late arrival within 90 days); on probation, the probation policy's multiplier, or its own
  points for that offence (rounded to the nearest allowed point). A manager may choose within the
  offence's range, and HR may set any value, including 0. An absence that was protected leave
  always scores 0 and never leads to a step.
- **When they count:** from HR's approval. Their expiry is dated from the day it happened. An
  incident is scored when HR approves it, so a repeat counts the earlier incidents HR approved
  since it was recorded.
- **When they stop counting:** on the end date of the write-up they led to; after a rolling window
  (12 months after January 31 is January 31); or whichever comes first. A write-up's end date
  applies to all the points that led to its step: the points counting when HR approved its
  incident, unless an earlier write-up already covers them (the simulator works it out the same
  way). Under "on the write-up's end date" alone, points that never led to a write-up keep
  counting. Optionally, a clean stretch with no new incident clears a total, and any new incident
  restarts the clock. HR can change when any one entry stops counting, and that always wins. A
  day is a calendar day in Pacific time.
- **Adjustments and voids:** HR can add or take off points from a start day (with an optional end
  day), change one incident's points, or void an incident, adjustment or credit, each from the day
  it's recorded and with a reason. An incident is voided from its own page; an adjustment or a
  credit from the person's **Points history**. Points taken off, and credits, take the oldest points
  first and go when those would have, so they never build up against later incidents, and a total
  never goes below 0.
- **Earning points back** (off by default): after a clean stretch (say 90 days), some points come
  off, a few times a year at most. The nightly run adds them.

**Steps.** When HR approves an incident, the step it calls for is the highest of: the step its point
total reaches (on the probation ladder during probation), the offence's minimum step (theft goes
straight to Termination review), a repeat's step, the probation step for serious offences, and
escalation while a step is still active (a new incident while on a Final written warning goes to
Termination review). Escalation counts only a write-up delivered by the day the incident happened,
never one approved but not delivered yet. When steps go in order, points alone only reach the next
step up, and the reason says how far they reached ("Points 5.5 ≥ 5 → Verbal warning. Steps go in
order, so the next step is Coaching"). Nothing is issued by itself: it's a task for the manager and
HR.

#### Recording an incident

Managers (and directors and HR) open the person's page and click **Record an incident**. The one
form asks:

- **the offence** and **the day it happened** (within the last year);
- **what happened**, in plain facts (their managers and HR see this);
- for attendance offences, **"Was this protected leave?"**: Oregon sick time, Paid Leave Oregon,
  OFLA/FMLA, jury duty, military leave, domestic violence leave, work injury or other. Any answer
  but **No** records it with 0 points, and it never leads to a step. Ask your employment lawyer
  to confirm the list;
- **points**: the offence's default, or other points within the range the point system allows for
  it, with a reason.

As you fill it in, a preview says what it means, for example "Maria: 4 → 6 points. Reaches Written
warning. A write-up is needed." When it reaches a step, the same form takes the write-up: the
expected improvement, what happens if it happens again, support offered, a follow-up date and a
note for managers and HR (the employee never sees that note). The policy broken is quoted
automatically. You can ask for a lower step than the point system calls for, with a reason; HR
decides, and it's recorded as a downgrade. When the step is one HR handles alone (a suspension or
a termination review), the preview only says HR decides.

**HR approves every incident before it counts** or the employee sees anything (the point system
can relax this for minor offences later). Points count from HR's approval, and expire counting
from the day it happened.

#### What HR does

HR admins find everything waiting under **Discipline**: incidents and write-ups to approve,
appeals, tasks, probation decisions, write-ups not signed yet, and signed write-ups whose PDF
couldn't be made (also on **Needs attention**).

- **Approve** an incident, or set other points with a reason (other points than the rules give
  ask for a fresh 6-digit code, or PIN), or **return it** to the manager with a note; they change
  it and send it again. The approval shows the points it counts now when they
  differ from when it was recorded (a repeat of incidents approved since), and whether earlier
  incidents of theirs are still waiting. Nobody approves their own work, and an HR admin's own file
  is handled by another HR admin.
- **A step is due.** When an approved incident takes someone to a higher step, their managers and
  HR get a task with a write-up already filled in. **Suspensions and termination reviews go to HR
  only**: the manager and the employee see nothing of them until HR acts.
- **A termination review is never automatic.** HR chooses **terminate** (recorded here; then
  archive them in Connecteam by hand, which becomes a to-do), **a last-chance agreement** (you may
  also place them on disciplinary probation) or **no action**, each with a reason.
- **Adjusting**, each with a reason and a fresh 6-digit code (or PIN), and all logged: add or take
  away points for someone (from a date, optionally until a date), change one incident's points (an
  adjustment line, so the original stays visible), void an incident or a write-up, change the day a
  write-up stops counting, and, on the **Points history** in their Discipline section, **Void** an
  adjustment or a credit, or **Change when it stops** for one incident or added points (the first
  day it no longer counts; this wins over the expiry rules). **As of a date** on someone's page
  shows their points and records as they were on any past day.
- After any void or reduction, later write-ups are checked again, as each was worked out (that
  day's points, the step they were on, which repeat it was). Where a later step would now be
  lower, HR gets a "check this" task. Nothing is undone by itself.

#### Write-ups

A write-up goes **Draft → With HR → Approved → Delivered → Active → Expired**. HR can send a
draft back with a note, and a write-up can be voided or be under appeal. Once approved, its words
are never changed.

Delivering it, once HR has approved it:

- **In person.** Click **Deliver in person** and hand your phone or computer to the employee. It
  shows only what they may see. They can add a comment, type their name and confirm with their
  PIN (their Connecteam kiosk code), plus the 6-digit code if they use an authenticator. Wrong
  tries count towards locking their HR sign-in, as at sign-in.
- **Through the portal.** Click **Send to sign in the portal**: they're told something is waiting,
  and sign it there.
- **On paper.** On the same in-person page, **Print** it (the printout has signature lines), have
  it signed, then **Add the photo** of the signed page. The photo goes to the employee's
  **Discipline** folder in Google Drive with the write-up, and opens from it (**Signed page
  (photo)**) for the employee, their managers and HR.
- **Refused to sign.** Record it with a witness: another HR portal user confirming with their
  mobile number and PIN, or the witness's name typed in. It still counts.

The signing page says plainly that signing confirms they received it, not that they agree. If a
write-up sent through the portal isn't signed within 3 days (a setting), the manager is reminded,
then every 3 days; it counts either way. The signed PDF goes to the employee's **Discipline**
folder in Google Drive (only what the employee sees is in it; see [PDFs of write-ups and
reviews](#pdfs-of-write-ups-and-reviews)).

#### Appeals

An employee can appeal a write-up within **7 days** of receiving it (a setting), once. HR decides,
with a note the employee and their manager both see: **upheld** (no change), **amended** (other
points, through an adjustment line, or a lower step) or **voided** (the write-up and its incident
stop counting). The HR admin who wrote or approved the write-up doesn't decide its appeal.

#### Who sees what

| | Employee | Their managers and directors | HR admins |
|---|---|---|---|
| Their write-ups | Once delivered, without managers' or HR's notes | Yes, without HR's notes | All |
| Incidents | No (their write-ups say what happened) | Yes, not voided ones | All |
| Point total and history | If the point system's switches allow | Yes | Yes |
| Expired write-ups and points | If the switches allow | Yes | Yes |
| Suspensions and termination reviews | Nothing until HR acts | Nothing until HR acts | Yes |
| Voided write-ups | "Withdrawn on [date]" if it was delivered | The same | Everything |

HR's reasons for changing an incident's points, or when something stops counting, are HR's only:
managers and directors see in its history that it changed, not why.

Each manager's **My team** lists everyone whose file they see, with points, current step,
probation, and what needs doing (steps due, drafts, write-ups to deliver or not signed yet).

#### Probation

- **Introductory period (new hires).** It starts on the start date from Connecteam and lasts 90
  days, or the length the published point system sets for their department (the longest, if they're
  in several). Its end date is its last day: 90 days from January 1 ends on March 31. It's made by
  itself for new hires only: people whose period was already over when the HR portal met them never
  get one. If HR confirms a different start date, it moves with it. A rehire gets a new one; a
  transfer doesn't.
- **Disciplinary probation.** HR places someone on it from their page, with an end date and a
  reason, for example as part of a last-chance agreement.
- **Extending** is HR's, with a reason and a new end date: one extension of up to 30 days, and 120
  days in all (the point system can set other limits). Extending answers any recommendation waiting,
  which leaves HR's queue (it stays in the probation's history). A leave of absence is an extension
  marked as leave, which doesn't count against those limits. HR can also move the end earlier. The
  employee is told and asked to **acknowledge** it.
- **The end-of-probation review** is due 14 days before the end. Its writer is reminded by the
  review itself (see [Performance reviews](#performance-reviews)); with end-of-introductory-period
  reviews turned off, the manager and HR are told instead. The manager recommends **pass**,
  **extend** (with a date) or **end employment**, from the review or on the person's page. HR
  decides (or waives it), on the person's page, where the recommendation shows which review it came
  from. **End employment** becomes a to-do: archive them in Connecteam by hand, then mark it done.
- **If the end date passes with no decision**, it stays open and the manager and HR are reminded
  daily (or, if the point system says so, it passes by itself, unless a recommendation to extend
  or end employment is waiting for HR).
- **Clearing points on passing** (if the point system says so): the incidents from during the
  probation stop counting the day it's passed. Points from before it are left as they were.
- An incident that happened during probation uses the probation rules, even if HR approves it
  later.

Screens say **"introductory period"** for new hires (or "probation", a setting), and employees see
their end date (a setting). Employees see their probation while it runs, and as **Passed** once HR
passes it (they're told, and the message opens their file); an ending or a waiver isn't shown to
them. Probation doesn't change Oregon at-will employment; have your employment lawyer check the
wording.

#### Settings

In **HR settings → Write-ups and probation**: days to appeal (7), days before the manager is
reminded of an unsigned write-up (3), whether screens say "introductory period" or "probation",
and whether employees see their probation end date.

### Performance reviews

Managers write reviews in the HR portal, on a phone or a computer, under **Reviews**. Every rule
below is an HR setting (**HR settings → Reviews**); the defaults are the ones recommended in the
design.

**Kinds of review**

- **End of introductory period**, from each person's introductory period (see
  [Probation](#probation)). If HR moves the end date before the review's recommendation has gone
  to HR, the review moves with it. If HR extends the period after the review closed, another
  review opens for the new end, however short the extension. A disciplinary probation gets one
  too, called **End of disciplinary probation**, covering the probation itself.
- **Annual**, on each person's work anniversary (or, if you prefer, in one month for everyone:
  it opens on the 1st and is due at the end of the month; people who started in the last six
  months wait until next year).
- **30- and 60-day check-ins**: off unless you turn them on.
- **Extra reviews**: a manager, director or HR can start one from a person's page (**Start a
  review**).

Scheduled reviews appear 14 days before they're due (the nightly run opens them; one missed for
up to a week still opens). When another review for the same person is due within 3 weeks, the
less important one is skipped: an end-of-introductory-period review wins over a check-in, and a
check-in nobody has started is cancelled to make way for it.

**Forms.** HR builds them under **Reviews → Review forms**. The starter form rates Guest service;
Teamwork; Reliability and attendance; Job knowledge and quality; Cleanliness and safety;
Communication; and Follows policy (including alcohol and food safety) on a 1-to-5 scale, then
asks for goals, an overall rating and a recommendation. HR can change the scale (2 to 10 points,
named), which low ratings need a comment, the sections (rated, comment only, or goals), the
recommendations, and add extra sections for a department. Saving a form makes a new version:
reviews already open keep the version they started with. Choose which form each kind of review
uses in HR settings.

**Who writes it.** The primary manager of the person's main department. Their main department is
the first of theirs with a primary manager, unless HR picks one on their page. HR writes it when
there's no such manager, when that manager is the person themselves, or when the manager can't
see them (a manager's own review, say). HR can hand any review to another manager, a director,
or HR. If someone moves department in Connecteam, their review goes to the new department's
manager at once (unless HR handed it to someone). Nobody reviews themselves.

**Everyone else who sees the person** (a second department's manager, a director, HR) can read
it and add comments, choosing who may see each one. Everyone working on a review can keep a
**private note**: only they and HR admins see it, and it's never part of the review.

**The steps**

1. **Self-review** (annual reviews only, by default): the employee rates themselves and adds
   anything they'd like. The writer can carry on without it if it doesn't come.
2. **The writer fills in the form.** Tap a number to rate; everything saves as you go. A
   read-only **Background** panel shows the points and write-ups in the period, certificate
   status (with flags such as "May not serve alcohol"), and the goals from the last review: points
   as they are today, write-ups delivered in the review's period. Anything not set up yet, or that
   can't be read just then, says "Not available".
   The overall rating is worked out from the ratings (their average); the writer can choose
   another, with a reason. **Finish** lists anything still missing.
3. **HR's approval** (end-of-introductory-period reviews only, by default). HR approves it or
   sends it back with a note. Nobody approves a review they wrote.
4. **Share**: the writer goes through it with the employee and shares it, with the meeting date.
5. **The employee reads it**, can comment, and **acknowledges** it. The page says plainly that
   acknowledging means they received it, not that they agree. They can also refuse. For someone
   who can't use the HR portal, the writer can record that they acknowledged on paper, refused
   in the meeting, or didn't answer.
6. **Closed.** The final PDF goes to the person's **Reviews** folder in Google Drive (see [PDFs of
   write-ups and reviews](#pdfs-of-write-ups-and-reviews)).

An end-of-introductory-period review recommends **pass**, **extend** (with a new end date) or
**end employment**. Once it's final (approved by HR), the writer's recommendation goes to the
person's introductory period, where HR decides the outcome (see [Probation](#probation)). If it
can't (HR decided already, or moved the end past the date recommended), the review says why, HR
and the writer are told, HR has a to-do while the period is undecided, and it isn't tried again.
The employee doesn't see that recommendation, in the portal or in the PDF.

**What the employee sees:** nothing until it's shared (except their self-review), then the
ratings, comments, goals and overall rating, and only the comments marked for them. Never the
private notes, comments for managers or HR, or the Background panel. A shared review is never
changed afterwards.

**Reminders**

| When | Who |
|---|---|
| A review opens | The writer |
| 7 days before it's due, on the day, then every 7 days late | The writer |
| 14 days late | Every HR admin |
| A self-review is asked for, 7 days before it's due, and on the day | The employee |
| Waiting for approval | HR admins (again every 7 days once it's late) |
| Shared | The employee, then every 7 days without an answer (the writer is told once) |
| Acknowledged or closed | The writer and HR |

**Review settings** (HR settings → Reviews): which kinds of review, annual on anniversaries or in
one month, self-reviews (annual only, every review, or none), HR's approval (end of introductory
period only, every review, or none), how many days before the due date reviews open (14), the
gap that skips a review (21 days), the reminder timing above, and which form each kind uses.
Reviews aren't linked to pay, and Connecteam's own "Performance" feature isn't used.

### Notifications

- Everything new goes into the person's **inbox** in the HR portal, on their home page, with
  their to-do list. Nothing is sent outside the portal: the HR portal only reads from Connecteam,
  so it sends no Connecteam chat messages.
- Each note is added once, however often the nightly run (2:15 AM Pacific) runs.
- Managers, directors and HR get one note each listing how many of their team's cards are
  running out or expired, rather than one per card.

**Who is told what**

| What happened | Who is told |
|---|---|
| A document added by someone other than HR; one not accepted | HR; whoever added it |
| An upload or PDF that keeps failing; records past the time they're kept | HR |
| A recovery code used to sign in; a sign-in reset | HR; the person |
| A card added | HR |
| A card confirmed; not accepted | The person; the person and whoever added it for them |
| A card running out (heads-ups) | The person; managers and HR (one note listing how many) |
| A card expired or missing (every 2 days) | The person, their managers and HR |
| An incident or write-up sent to HR; returned | HR; the manager |
| A step is due; a termination review | The manager and HR, straight away; HR only |
| A write-up ready to deliver; waiting for a signature; not signed in time | The manager; the employee; the manager |
| An appeal; decided | HR; the employee and the manager |
| A probation review due (only with end-of-introductory-period reviews off), or the end passed undecided | The manager and HR |
| A probation extended, passed or placed; a recommendation from a review | The employee, the manager and HR; HR |
| A review opened, due soon, due or late | The writer (HR too once it's 14 days late) |
| A review to approve; approved or sent back | HR; the writer |
| A self-review asked for; a review shared | The employee |
| A review acknowledged or closed | The writer and HR |

### HR settings

HR admins find these in the HR portal under **HR settings**, in this order:

- **Roles** and managers' departments (who is primary), who must use an authenticator code, how
  soon people are signed out, **Notifications** (they're the portal's inbox only), which
  Connecteam profile fields hold the start date and direct manager, and **Records** (how long
  records of people who left are kept).
- **Kinds of document** (see [Documents](#documents)).
- **Certificates**: who needs which card, the kinds of certificate and reminders (see
  [Certificates](#certificates)).
- **Discipline**: the point system, its versions and the simulator (see [The point
  system](#the-point-system-discipline-setup)).
- **Write-ups and probation**: days to appeal, the unsigned write-up reminder, "introductory
  period" or "probation", and whether employees see their end date (see [Discipline and
  probation](#discipline-and-probation)).
- **Reviews**: kinds, timing, self-reviews, HR's approval, and the forms (see [Performance
  reviews](#performance-reviews)).
- **HR key** (see [The HR key and backups](#the-hr-key-and-backups)).

Changing settings asks for a fresh code (or PIN), and each change is in the audit log.

### The audit log

Every sign-in, failure, lock, reset, look at a file and change goes into the HR portal's audit
log: who did it, what, to whose file, when and from where. It never holds PINs, codes or what
records say. Lines can't be changed: each one is chained to the one before, so **Check audit
log** (HR admins, **Audit log** page) shows if any line was changed or removed, including lines
cut off the end.

HR events never go to Staff Access's **Activity** tab or the Home Assistant logbook, which other
Home Assistant users can see. On the dashboard's HR portal tab you see **Security events** only
(sign-ins, locks, resets and settings changes), never who looked at which file.

### Emergency controls

On the dashboard's **HR portal** tab:

- **Find someone** to **Unlock** them or **Reset sign-in** (their authenticator and recovery codes
  go, and they're signed out everywhere). This works for anyone, HR admins included.
- **Sign everyone out** of the HR portal.
- **Turn the HR portal off** (untick it and save).

### The HR key and backups

Private details are encrypted on the Home Assistant computer: mobile numbers, emails,
authenticator secrets, sign-in names, and the files and texts the HR portal keeps. Names, dates
and statuses stay readable so lists can be sorted. The HR records live in the add-on's `/data`
folder (`/data/hr`), apart from Staff Access's own.

- **Staff Access's own Download backup never holds anything from the HR portal**, and restoring
  one never touches it.
- **Home Assistant's backups include the HR portal.** By default its key is included too, so a
  restore just works. Turn on Home Assistant's backup encryption.
- **Keep the HR key out of backups** (HR settings → HR key) is off by default. Turning it on
  shows an **HR recovery key** once: 20 letters and numbers. **Print it and keep it in the
  safe.** The key itself is then left out of Home Assistant's backups, and the secrets that reach
  HR's files (the Google script's Web app URL and secret) are kept locked with it. A stolen
  backup on its own can't open the encrypted details or reach the files in Google Drive. After a
  restore, the HR portal (and its Google Drive connection) stays locked until you type the
  recovery key on the dashboard's HR portal tab (**Unlock with the HR recovery key**). Turning the
  setting off puts the key back in backups, and the secrets with it.
- **Backups made before you turned the setting on still hold the HR key.** Turning it on only
  protects later backups: delete the older ones (or keep them as safely as before) once a new
  backup has been made.
- If the key is kept out of backups and both the computer and the recovery key are lost, the HR
  records on the computer can't be opened. Files kept in Google Drive survive.
- Anyone who is a Home Assistant administrator, or who has the running computer in hand, could
  get at everything. Keep that list short.

**Moving to new hardware.** A migration file (Backup tab → Migrate) carries the HR portal too:
its settings (including the Google Drive connection: the script's Web app URL and secret, and
the drives), its records and its key. A file from 0.10.0 to 0.10.3 may also carry the HR
portal's own Connecteam keys and its chat and copy switches: they're ignored. With the key kept
out of backups, only the locked copy travels, and the new install asks for the HR recovery key.
A migration file can't be made while files are still waiting to reach Google Drive, or uploads
and certificate photos are waiting for HR (they're only on this computer): the dashboard says
which, so wait for them or check them, then try again. PDFs still to be made
travel with the records and are made on the new computer. HR's records can be up to 64 MB in a
migration file; a bigger one is refused when the file is made, in plain words. If HR's records
can't be put in place on the new computer, the HR portal stays off there (it never starts empty),
and the dashboard says so: keep the old install.

### Keeping records

Records of people who have left are kept for 5 years after they leave (an HR setting), then
listed for HR to review and delete; nothing is ever deleted automatically, and a legal hold
stops it. Your employment lawyer should confirm the period. For reference, an I-9 must be kept
for 3 years after hire or 1 year after leaving, whichever is later; 5 years after leaving covers
that.
