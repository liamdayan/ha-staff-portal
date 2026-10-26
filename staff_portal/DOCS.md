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

Every device in Home Assistant is listed, grouped by area. For each one choose:

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

   The Staff tab lists one thin row per person: name, role, department, anything that needs a look
   (inactive, locked, not clocked in) and when they last signed in. Click a row for everything
   else and its buttons. Search by name, role or department, and sort by **Department** (each
   department folds away with a click), **Name** or **Role**; this browser remembers the sort
   and the folded departments.
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
  (limit 2,000)". Showing them costs no requests.

Open one for its details, **Test connection**, the full status (why it's stopped or paused) and
how its requests are kept low. Each has its own **Save** button, which saves only that
connection's settings, so saving one never changes the other or the rest of Settings. Secrets
stay hidden: leave the box blank to keep the saved one.

Two more are used only by the [HR portal](#hr-portal), and you can set them up before or after
turning it on: **Google Drive (HR files)**, where it keeps employee files (see [Google Drive for
HR files](#google-drive-for-hr-files)), and the optional **Connecteam for HR**, for its chat
messages and certificate copies, which counts its requests against the HR portal's own small
share (see [Connecteam for HR](#connecteam-for-hr)). The HR portal tab shows how both are doing.

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
  doors marked as guest rooms (see **Public doors and guest rooms** below).
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
   Both door lists show only public doors (and doors not marked yet).
3. Under **Doors**, rename doors to what staff call them (TT Hotel keeps its own names) and hide
   any staff should never pick. A hidden door can still be one of the day-pass or overnight
   doors. Mark each door as a **Public door** or a **Guest room** (see below).
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
   overnight wristbands, staff cards (and whether those may open single doors, floors, buildings
   or every door), which doors they may put on cards (all doors, or chosen buildings, floors and
   doors), the longest overnight stay and staff card, whether they may change the doors on a
   day pass or overnight wristband, whether they **may change when day passes stop working**,
   and whether they see everyone's cards in the card log and look-ups. A role is warned if its
   doors don't cover the day-pass or overnight doors, because those cards would then be refused.
   On a role-based computer, a person can have their own wristband rules (click them under their
   role); otherwise they get their role's.

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

### Public doors and guest rooms

Each door is either a **public door** (the pool gate, the main entrance, the gym) or a **guest
room**. On the **Wristbands** tab, under **Doors**:

- Pick **Public door**, **Guest room** or **Not marked** next to each door. It's saved straight
  away.
- **Mark a whole floor or building** marks every door there at once: pick the floor or building,
  pick what to mark them as and click **Mark**. If some of those doors are marked the other way,
  it asks first.
- The line above the list says how many doors aren't marked yet.

What it changes:

- Overnight guests' rooms are picked from the guest rooms. The shared doors (the doors every day
  pass opens, the shared doors on overnight wristbands, and the doors people who may change
  them can pick from) are the public doors.
- A door not marked yet shows in both lists, so nothing disappears until you've marked it.
- The add-on checks it too: a guest room picked as a shared door, or a public door picked as a
  room, is refused with a message saying so.
- A door already ticked under **Day passes** or **Overnight guests** keeps going on those cards
  if you later mark it a guest room, as a hidden door does, but the Wristbands tab warns about
  it under that list. Untick it there, or mark it as a public door.
- Staff cards can open any door, whatever it's marked as.
- Syncing doors from TT Hotel never changes what a door is marked as.

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
  PM"), and in the dashboard's Key cards list.
- **Lost or revoked cards tried at a door.** When a card marked **Lost** or **Revoked** here is
  tried at a door after it was marked, or TT Hotel reports that a card it deleted was tried, the
  row is highlighted, the activity log says so ("Lost card tried at Pool Gate", or "Lost card
  opened Pool Gate"), and Home Assistant shows a notification naming the card and the door. Each
  card alerts at most once every 10 minutes, and its next notification replaces the last one; at
  most 20 alerts go out a minute in all (Door activity still lists every try).
  Untick **Tell Home Assistant when a lost or revoked card is tried** to keep it to the activity
  log.
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

- **Connecteam smart group** (optional): with **Add everyone from Connecteam** on (see the
  Connecteam section), people in that smart group join this department. Someone in several of
  these smart groups gets the first department in the list: drag departments to change the order.
- **Role**: given to people who join it (or **Leave their role as it is**).
- **Clock-in**: for people linked to Connecteam, **Must be clocked in** (everyone in it must be
  clocked in to sign in), **Doesn't need to clock in**, or **Each person's own setting**. Someone
  whose own setting you've changed by hand keeps theirs (see **Clocking in, by department or
  person** in [the Connecteam section](#add-everyone-from-connecteam)).
- **Key card**: what a key card made with **Make key card** opens (every door, whole buildings,
  whole floors or single doors, like a staff card), how many days it lasts, and whether it opens
  doors that are deadbolted from inside.

Pick each person's department on their card on the Staff tab (or in **Edit**). Deleting a
department leaves its people with no department. Departments are in backups, and can be exported
and imported on the Backup tab (roles are matched by name).

### Make key card

Every person's card on the Staff tab says what **Make key card** would make for them, from their
department. Click it, have a blank card (or a card to reuse) ready at the encoder, and the
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
(cards made in the staff app are matched to the person by name), newest first. Filter by
**Active**, **Expired**, **Revoked**, **Lost** or **All**, and search by name or by the start of a
card number. Each
row shows who it's for, the card number, what it opens, who made it and when, when it stops
working, its state and, for a lost card, how many of its doors it's been locked out of, and
where it was last used (with [Door activity](#door-activity) set up).

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
   that happens.
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
   Give it the **users.read** and **time_clock.read** scopes. Scopes can't be changed later.
2. Copy the **Client ID** and **Client Secret**. Connecteam only shows the secret once.
3. In Staff Access, open **Settings → Connections → Connecteam**, tick **Link staff to
   Connecteam**, paste both keys, choose **Australia** under **Account hosted in** if your
   Connecteam account is hosted there, and click **Save Connecteam settings**, then **Test
   connection**. It reports how many users and time clocks it found and how many people are
   clocked in now.
4. On the **Staff** tab, click **Add from Connecteam** and pick a person. People already added are
   greyed out.
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
  on or off, change their PIN to their current kiosk code (if it changed in Connecteam), or
  **Unlink** them.
- Turning off **Link staff to Connecteam** lets everyone sign in with their PIN again, without
  removing the links.

### Add everyone from Connecteam

Instead of adding people one by one, tick **Add everyone from Connecteam** under **Settings →
Connections → Connecteam** and click **Save Connecteam settings**. Every active Connecteam user
then becomes a staff member here:

- Named as in Connecteam ("First Last"; if someone here already has that name, "(2)" is added).
- Their PIN is their Connecteam kiosk code when it's 4 to 8 digits and nobody else here uses it.
  Otherwise they're added **turned off**, and their card says **No usable kiosk code in
  Connecteam**: give them a PIN with **Edit** and tick **Can sign in**. If their kiosk code changes
  in Connecteam, their PIN follows it (and they're signed out), unless you've given them a
  different PIN.
- Their department comes from their Connecteam smart groups (Staff tab, **Departments**), and
  their role from the department, if it sets one. Someone who leaves the smart group leaves its
  department (and the role it gave them); a department without a smart group, chosen by hand,
  stays. Once you change someone's role by hand, the sync leaves their role alone.
- People archived or removed in Connecteam are turned off (and signed out), never deleted. If they
  come back, they're turned on again. Someone you turn off yourself stays off.
- People you added by hand are never changed. If you delete someone the sync added, the next sync
  adds them again: turn them off instead. For the same reason they can't be unlinked from
  Connecteam while this is ticked.

The sync runs about once an hour while this is ticked, soon after you save the settings, and when
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

1. Their own setting, if you've changed it by hand. Their department and the sync never change
   it back.
2. Otherwise their department's, if it sets one.
3. Otherwise their own setting (for people the sync added, the one it gave them).

Their row shows **Must be clocked in** or **Clock-in not required**, and where that comes from:
**From Housekeeping** (their department), **Set by hand**, or, for people the sync added,
**Default** (the setting above). When it's set by hand and their department sets one, **Use the
department's setting** goes back to the department's. Changing a department's setting counts
from the next sign-in (and, for anyone already signed in, from the next control they use).
Moving someone to another department in **Edit** leaves the switch following the new
department, unless you change the switch too.

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
- With one time clock, hourly checks are 24 requests a day. Add one for each sign-in or control
  by someone the last check didn't find clocked in, typically the start of each shift. With many
  time clocks it checks less often if needed, so regular checks stay under 15% of your daily limit.
- Those extra checks happen only after someone types their correct PIN (or uses a control while
  signed in), so a wrong PIN, or someone just holding a phone, never costs a request. They happen
  at most every 30 seconds (less often while Connecteam is failing) and stop once three quarters
  of the day's share is used, keeping the rest for the hourly checks.
- Its own hard limits: 20 requests a minute, and a fifth of your account's daily limit (2,000 on
  Expert) per day, counted across add-on restarts. At the cap it pauses until midnight UTC.
- It reads Connecteam's rate-limit headers. If your account (from any integration) has less than
  10% of its daily requests left, or is close to the per-minute limit, Staff Access pauses until
  the limit resets.
- If Connecteam says "too many requests", it waits as long as Connecteam asks (at least a
  minute, doubling each time, and never more than 24 hours) and never retries straight away.
- If Connecteam rejects the keys or the app is missing a scope, it stops all automatic requests
  until you save the Connecteam settings again or click **Test connection**.
- Requests identify themselves as Staff Portal (in the User-Agent).
- The HR portal, once you turn it on, has its own smaller share inside these limits: at most 6
  requests a minute and 500 a day, and none once Staff Access has used half of its daily share, so
  the time clock always has room. A chat message is one request; copying a one-page card to
  Connecteam is three. If Connecteam refuses an HR request (a missing permission or plan feature,
  or "too many requests"), only that HR feature stops or waits: the time clock checks carry on.

**Settings → Connections** shows how many requests Staff Access made today on Connecteam's line.
Open **Connecteam** for how many your account has left (once Connecteam has answered) and, while
paused or stopped, why. Remember that after 15 minutes without an answer, people who must be
clocked in can't sign in.

The Client Secret is never shown again after saving (leave the box blank to keep it), and it isn't
included in backups. After restoring a backup on a new system, enter it again (a migration file
brings it across, encrypted).

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
    through your https address, reload Settings, and click **Use this address** next to the
    address shown under "Cloudflare traffic recently came from". Then tick the box and save.
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
  names, whether each door is a public door or a guest room (a backup from before 0.9.0 leaves
  that as it is), roles' wristband rules, departments, each person's department and Connecteam
  sync details, and how many digits each PIN has (when that's known) are included, and so are
  departments' clock-in settings (a backup from before 0.9.0 has none, so each person's own
  setting counts). Whether remote lock-out was tested is never restored: test it again on the
  new system.
- Links to Home Assistant users are never saved in files. Restoring onto the same system keeps
  the links for phones and staff that still match; elsewhere, link them again.
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
- A damaged file or one from a newer version is refused, and nothing changes.
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
- the whole door list (with what each door is marked as), the activity log and the card history.

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
  changes is signed out. Someone you've turned off stays off, even if the file has them on. A file
  from before 0.7.0 has no roles or own settings for people, so replacing from one leaves theirs as
  they are.

Keep these files private. Staff and phone PINs are included only in scrambled form (they can't
be read back directly), but PINs are short, so someone with the file could work them out by
trying every combination offline.

## HR portal

The HR portal is a second, private website built into Staff Access, at its own address, for
employee files, documents and certificates. It takes its people and departments from
Connecteam. What each person sees depends on their role, and everything they do is written to
an audit log only HR admins see.

It's **off until you turn it on**. While it's off, nothing changes for anyone, nothing extra is
asked of Connecteam, and its address only says "The HR portal is turned off."

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
   files](#google-drive-for-hr-files)) and, if you like, **Connecteam for HR** (see [Connecteam
   for HR](#connecteam-for-hr)).

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
**Connecteam for HR**, with a link to each in **Settings → Connections** (below).

### Google Drive for HR files

The HR portal keeps employee files, such as certificates and signed policies, in a Google
**shared drive**, one folder per employee. Staff Access reaches the drive through a small script
that runs in **your own Google account**. There's no Google Cloud project, no key file and
nothing to change in the Google Workspace admin console. You set it up once, under **Settings →
Connections → Google Drive (HR files)**, before or after turning the HR portal on.

Until it's set up, nothing is sent to Google. After that, Google is only asked when you open,
save or test this connection, or when the HR portal works with files.

#### Setting it up (about 5 minutes)

1. In Google Drive, create a shared drive named **HR Employee Files**. If you like, create a
   second one, **HR Restricted**, for restricted records (see [The drives](#the-drives)).
2. In Staff Access, open **Settings → Connections → Google Drive (HR files)** and click **Copy
   the script**. The script has a secret made for your Staff Access written into it.
3. Go to **script.google.com**, signed in as the owner, and click **New project**. Delete what's
   there and paste the script. You can name the project, for example "Staff Access HR".
4. Next to **Services**, click **+**, choose **Drive API** (keep version v3) and click **Add**.
5. Click **Deploy → New deployment**. Choose the type **Web app**, set **Execute as: Me** and
   **Who has access: Anyone**, then click **Deploy**. Click **Authorize** and allow access: Google
   asks for your Drive, your Google Docs (to read the text on cards) and your email address (to
   show which account it is). If Google says it hasn't verified the app, that's because you made
   it yourself: click **Advanced**, then **Go to** your project.
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
  and the Doc is deleted at once. The PDF is then kept in the person's folder like any other file.
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
with version 1 meanwhile; PDFs wait until the new version is deployed.) To update it (the Web app
URL stays the same):

1. Click **Copy the script**.
2. In script.google.com, open the project, select everything in the editor, paste over it and
   save.
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

### Connecteam for HR

Optional. In **Settings → Connections → Connecteam for HR**:

- **Client ID and Client Secret** of a second Connecteam app, made by you as the account owner
  (**your name → Integration Center → OAuth 2.0 → Create app**, named "Staff Access HR"), so your
  time clock connection stays as it is. Give it **users.read**, plus **chat.write** and
  **publishers.read** for chat messages, and **documents.read**, **documents.write** and
  **attachments.write** for certificate copies. Leave the keys blank to use the Connecteam
  connection above instead, but only if its app has these permissions too.
- **Send Connecteam chat messages** (off at first). Needs Connecteam's Communications hub, and a
  custom publisher named "Hot Lake HR" under **Settings → Feed settings → Custom publishers** in
  Connecteam. See [Notifications](#notifications).
- **Copy confirmed certificates to Connecteam** (off at first). Each card HR confirms also goes
  onto the person's document in Connecteam with its expiry date, so Connecteam reminds them
  before it runs out. Needs Connecteam's **HR & Skills hub on the Expert plan**. Google Drive
  keeps every file either way.
- **Test connection** checks both: whether chat messages can be sent as "Hot Lake HR", and
  whether Connecteam's documents can be read. Each check gives a plain result.

If Connecteam refuses chat or certificate copies (a missing permission or plan feature, or keys
it doesn't accept), only that part stops, and the connection, the HR portal tab and HR settings
say why. People still see everything in the portal's inbox, and messages wait. Fix it in
Connecteam (or the keys here), then click **Save** or **Test connection** to start again. The
time clock checks are never affected.

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
departments or HR settings, turning off an authenticator) ask for a fresh authenticator code
first, or the PIN for someone without an authenticator.

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

**Everyone else** can turn one on in **My account → Turn on**, re-typing their PIN.

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
on their page. Coming back isn't automatic: if someone is restored in Connecteam, HR clicks
**Allow sign-in again** (or makes them a new setup code).

**Safety catch.** If Connecteam suddenly lists far fewer people (nobody, or more than a quarter
fewer), nobody is cut off: HR and the dashboard get a warning instead, and the next sync tries
again.

**Connecteam's request limits.** The HR portal has one small share inside Staff Access's, for
reading people, chat messages and certificate copies together: at most 6 requests a minute and
500 a day, and none once Staff Access has used half of its daily share, so clock checks always
have room. Normal use is a few requests a day. If Connecteam refuses an HR request (a missing
permission or plan feature), only that HR task stops, and if it says "too many requests", only
the HR portal waits: Staff Access's own time clock checks keep running.

### Notifications

- **The portal's inbox** always works, for everyone, and the home page lists to-dos.
- **Connecteam chat messages** are optional: tick **Send Connecteam chat messages** in
  **Settings → Connections → Connecteam for HR** (see [Connecteam for HR](#connecteam-for-hr)).
  They need the **chat.write** and **publishers.read** permissions (Connecteam's Communications
  hub), and a custom publisher named "Hot Lake HR" in Connecteam. Without them, people see things
  in the inbox only, and HR settings say why.
- Messages never say what it's about, only that there's something new in the HR portal, with its
  address.
- Employees' messages wait out **quiet hours** (9 PM to 7 AM by default), and each person gets at
  most one message a day unless it's urgent. Managers, directors and HR get urgent things straight
  away and everything else in one **daily summary** (8 AM by default). HR sets these times in
  **HR settings → Notifications**, in Pacific time.
- Each message is one Connecteam request. A message that can't be sent is tried again after 1
  minute, 5, 30, 2 hours and 6 hours, then daily; after 8 tries HR sees it in **HR settings →
  Notifications** with a **Retry** button, and on their to-do list.

### HR settings

HR admins find these in the HR portal under **HR settings**: roles, managers' departments, who
must use an authenticator code, how soon people are signed out, notifications, which
Connecteam profile fields hold the start date and direct manager, and the HR key (below).

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

Certificates, signed write-ups and final reviews are kinds too, but they're shown in their own
sections, which decide who sees them. Changing who can see a kind takes effect at once. Changing
its Drive folder only affects new files.

**Adding a file.** Open someone's page, choose the kind, a title and the date on the document,
and the file: a photo (JPEG, PNG or WebP) or a PDF, up to 15 MB (25 MB for HR, for scanned
paperwork). The type is checked from the file itself, not its name. Photos are saved afresh
without their camera details or location; PDFs are kept exactly as they are. Files that anyone
but HR adds wait for HR to **Accept** or **Don't accept** them (with a reason, which whoever
added it sees). A file that isn't accepted never reaches Google Drive and is removed after 30
days. Nobody checks their own upload: an HR admin's own file is checked by another HR admin.

**Restricted files (I-9, Reasonable accommodation).** Only HR admins see them; managers,
directors and the employee never learn one exists, and they're never copied to Connecteam. On a
person's page HR sees how many there are, and **Show restricted files** lists them. Every look is
in the audit log. They're kept apart in Google Drive: in the **HR Restricted** shared drive if
you picked one (see [The drives](#the-drives)), otherwise in a **Restricted** folder in the HR
drive, and HR settings warns that every member of that drive can open it.

**Getting files to Google Drive (and Connecteam).** Each file is kept encrypted on the Home
Assistant computer until it's in Google Drive, then 30 more days for quick opening; after that
it's fetched from Drive when someone opens it. Each file shows where it is, for HR: **Google
Drive: Sent**, **Waiting** or **Needs attention**. If Google doesn't answer, the file is tried
again after 1 minute, 5, 30, 2 hours and 6 hours, then daily. After 8 tries it goes on HR's
**Needs attention** page with **Retry**. A retry never makes a second copy. While Google Drive
isn't set up, or its script is out of date, files simply wait (without using up tries), and HR's
to-do list says so. A migration file can't be made while files are waiting to reach Google Drive
or waiting for HR.

**Connecteam copies.** A kind can also be copied to a Connecteam document, but only while you've
turned on **Copy confirmed certificates to Connecteam** (Settings → Connections → Connecteam for
HR), and only where "latest file only, and the employee sees it there" is fine. Restricted files
never go. While HR's share of Connecteam requests is used up, copies wait. Problems only a person
can fix ("not assigned this document's pack in Connecteam") go on **Needs attention** at once,
with **Retry** and **Don't copy to Connecteam**.

**Deleting.** HR deletes a file with a fresh 6-digit code (or PIN). It stays in the person's
**Recycle bin** for 30 days and can be **Restored**; then it's removed from the computer and
moved to the shared drive's trash, which Google empties 30 days later. Connecteam can't remove
files for Staff Access, so a deleted file that was copied there becomes a to-do on **Needs
attention**: remove it in Connecteam, then press **Done**. The audit log keeps the line.

**Legal hold.** On a person's page, HR can **Put on legal hold** (with a reason, and a fresh
code). Nothing in that file can then be deleted, the recycle bin isn't emptied for them, and
they're left off the retention list, until HR lifts it.

**After someone leaves.** Records are kept 5 years after they leave (HR settings → Records).
After that the person is listed on **Needs attention** under **Past the time records are kept**,
and HR gets a note in the inbox. Nothing is deleted by itself: HR reviews the person's files and
presses **Delete their files** (a fresh code; they go to the recycle bin first). When HR confirms
someone has left, **Move Drive folder to Former employees** on their page moves their folder
there; it never happens by itself. When someone's name changes in Connecteam, their Drive folder
is renamed overnight. Folders are always found by their Connecteam ID, never by name, so two
people with the same name never get mixed up.

**Needs attention** (HR admins, in the menu) lists files waiting for HR to check, uploads that
couldn't be sent, uploads still waiting to be tried again, copies to remove in Connecteam,
records past the time they're kept, and legal holds.

### The audit log

Every sign-in, failure, lock, reset, look at a file and change goes into the HR portal's audit
log: who did it, what, to whose file, when and from where. It never holds PINs, codes or what
records say. Lines can't be changed: each one is chained to the one before, so **Check audit
log** (HR admins, **Audit log** page) shows if any line was changed or removed.

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
  safe.** The key itself is then left out of Home Assistant's backups, so a stolen backup on its
  own reveals nothing. After a restore, the HR portal stays locked until you type the recovery
  key on the dashboard's HR portal tab (**Unlock with the HR recovery key**). Turning the setting
  off puts the key back in backups.
- If the key is kept out of backups and both the computer and the recovery key are lost, the HR
  records on the computer can't be opened. Files kept in Google Drive survive.
- Anyone who is a Home Assistant administrator, or who has the running computer in hand, could
  get at everything. Keep that list short.

**Moving to new hardware.** A migration file (Backup tab → Migrate) carries the HR portal too:
its settings (including both HR connections: the Google script's Web app URL and secret, the
drives, and the Connecteam for HR keys), its records and its key. With the key kept out of backups, only the locked copy
travels, and the new install asks for the HR recovery key. A migration file can't be made while
uploads are still waiting to reach Google Drive or Connecteam: wait for them to go, then try
again.

### Keeping records

Records of people who have left are kept for 5 years after they leave (an HR setting), then
listed for HR to review and delete; nothing is ever deleted automatically, and a legal hold
stops it. Your employment lawyer should confirm the period. For reference, an I-9 must be kept
for 3 years after hire or 1 year after leaving, whichever is later; 5 years after leaving covers
that.
