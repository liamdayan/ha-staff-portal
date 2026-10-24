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

In **Roles**, create a role such as *Maintenance*. Every device in Home Assistant is listed,
grouped by area. For each one choose:

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

Click **Save role**. Phones with that role update within a few
seconds.

**Duplicate** makes a copy of the role under a new name, with the same device and area settings,
so you can make a variation (say *Pool tech, nights*) without changing anyone else's phone. No
phones move to the copy until you pick it for a phone in **Phones → Edit**. If you've made changes
you haven't saved yet, the copy gets them and the original stays as it was last saved.

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
with everything on it. A copied page shows on every phone set to **All pages**; phones set to
specific pages need it added with **Choose phones**.

Reorder pages, page items and the devices in a group card by dragging their **≡** grip
(with a mouse or a finger), or click the grip and use the arrow keys. Then click **Save layout**.

**Which phones show which pages.** Every phone shows every page by default, including pages
you add later. To change that for one phone, open **Phones → Edit** and use **Pages on this
phone**: untick **All pages** and pick the pages it should show (including the **More** page).
You can also do it from the Layout tab: each page shows **Shown on: …** with a **Choose phones**
button. Phone cards show their pages as **Pages: …**. A phone set to specific pages won't pick
up new pages automatically; tick **All pages** again for that.

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
  (**Choose phones**), or use a role instead.
- The override is for that device on the whole phone, not just that spot. A device set to
  **Hidden** doesn't show up anywhere on those phones, including the More page.
- If a device has different overrides in more than one place, the higher one wins.
- Overrides are enforced by the add-on, the same as roles, not just hidden from view.
- Items with an override say so in the Layout tab ("Access: Control on every phone").
With **Put devices… on a "More" page** ticked, anything a role allows that isn't placed on a
page still shows up on a "More" page. Delete every page to go back to the automatic layout.

Setpoints (number entities) and dropdowns (select entities) can be controlled too, always
within the entity's own minimum, maximum and options.

## Locked controls on the phone

So someone checking temperatures can't switch something by accident, controls on the phone are
locked until they're unlocked on purpose. Readings always work.

- While locked, switches, buttons, setpoints and dropdowns are dimmed and ignore taps. Tapping
  one shows a reminder.
- To unlock, **press and hold** the 🔒 **Hold to unlock controls** button at the bottom of the
  screen for about a second. A quick tap doesn't unlock.
- Controls lock again after 10 minutes with no control used (each use restarts the 10 minutes),
  when someone taps **Lock now**, or when the phone asks for a PIN or someone signs out.
- The lock button only shows on screens that have controls.
- Change the time, or turn this off, under **Settings → Staff phones → Lock controls until staff
  unlock them**. Phones pick up the change within a few seconds.

This guards against slips, not people: what a phone may do still comes from its role.

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

1. In **Phones & computers**, click **Add phone**, name it (such as *Maintenance phone*), pick a role, and
   optionally set a 4- to 8-digit PIN.
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
   person signs in with their own PIN (they can type it on the keyboard).
2. A setup link appears instead of a QR code. Click **Copy link** and open it in the browser on
   that computer (Chrome, Edge, Safari or Firefox). Whoever opens it first gets access, so only
   open it on the computer you're setting up.
3. Staff Portal opens, signed in. In Chrome or Edge, use **Install app** from the browser's menu
   to give it its own window and icon, or just bookmark it. It stays signed in in that browser
   until you revoke it.

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
**Choose phones** to show it only on the front desk computer.

## Shared phones: each person signs in

If several people share one phone (a maintenance phone passed between shifts, say), you can have
each person sign in with their own PIN so every action is recorded under their name.

1. In **Staff**, click **Add staff member** for each person, with a name and a 4- to 8-digit PIN.
2. In **Phones**, click **Edit** on the shared phone and set **Who uses it** to
   **Shared: each person signs in with their own PIN**. Leave **Everyone on the Staff tab**
   ticked, or untick it and pick who may use that phone.

On the phone, the app now asks **"Who's using this phone?"**. The person taps their name,
enters their PIN, and the header shows who's signed in. **Sign out** returns to the name list,
and they're signed out automatically after the idle time set in **Settings**.

- What they can control still comes from the **phone's role**.
- The Activity tab shows entries like "Bob on Maintenance phone", and Home Assistant logbook
  entries read "set to 140° by Bob (Maintenance phone) via Staff Portal".
- Too many wrong PINs locks that **person** for 15 minutes (or until you unlock them on the
  Staff tab). The lock only stops new sign-ins; if they're already signed in on another phone,
  that stays signed in. Wrong PINs across several names lock the **phone** until you unlock it,
  so PINs can't be guessed by switching names.
- Turning a person off (**Edit → Can sign in**) or changing their PIN signs them out right away.

## Connecteam: add staff and require clocking in (optional)

If you use [Connecteam](https://connecteam.com) for time tracking, Staff Access can add staff from
your Connecteam user list, use their **kiosk code** as their PIN, and only let them sign in while
they're **clocked in**. Staff you add by hand keep working exactly as before.

API access needs Connecteam's **Expert plan or higher** (with the Operations hub for the time clock).

1. In Connecteam, open **your name → Integration Center → OAuth 2.0** and click **Create app**.
   Give it the **users.read** and **time_clock.read** scopes. Scopes can't be changed later.
2. Copy the **Client ID** and **Client Secret**. Connecteam only shows the secret once.
3. In Staff Access, open **Settings → Connecteam**, tick **Link staff to Connecteam**, paste both
   keys, choose **Australia** under **Account hosted in** if your Connecteam account is hosted
   there, and click **Save settings**, then **Test connection**. It reports how many users and time
   clocks it found and how many people are clocked in now.
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
  answers again. The Settings page and the add-on log show what went wrong.
- The Staff tab shows each linked person's clock status. **Edit** lets you turn clocked-in-only
  on or off, change their PIN to their current kiosk code (if it changed in Connecteam), or
  **Unlink** them.
- Turning off **Link staff to Connecteam** lets everyone sign in with their PIN again, without
  removing the links.

### Staying within Connecteam's API limits

Connecteam limits API requests **per account**, shared by every integration you use (Expert plan:
100 a minute and 10,000 a day; going over gets requests refused). Staff Access is built to use
a small, steady share:

- It only checks the time clock while it's needed: Connecteam is turned on and at least one
  active person must be clocked in. Otherwise it sends nothing.
- Each check is one request per active time clock. The list of time clocks is reused for 6 hours
  and the user list for 5 minutes. The access token lasts 24 hours (one more request a day).
- With one time clock, hourly checks are 24 requests a day. Add one for each sign-in or control
  by someone the last check didn't find clocked in, typically the start of each shift. With many
  time clocks it checks less often if needed, so regular checks stay under 15% of your daily limit.
- Those extra checks happen at most every 30 seconds, and less often while Connecteam is failing.
- Its own hard limits: 20 requests a minute, and a fifth of your account's daily limit (2,000 on
  Expert) per day, counted across add-on restarts. At the cap it pauses until midnight UTC.
- It reads Connecteam's rate-limit headers. If your account (from any integration) has less than
  10% of its daily requests left, or is close to the per-minute limit, Staff Access pauses until
  the limit resets.
- If Connecteam says "too many requests", it waits as long as Connecteam asks (at least a
  minute, doubling each time) and never retries straight away.
- If Connecteam rejects the keys or the app is missing a scope, it stops all automatic requests
  until you save the Connecteam settings again or click **Test connection**.
- Requests identify themselves as Staff Portal (in the User-Agent).

**Settings → Connecteam** shows how many requests Staff Access made today and, once Connecteam
has answered, how many your account has left. While paused or stopped it says why. Remember that
after 15 minutes without an answer, people who must be clocked in can't sign in.

The Client Secret is never shown again after saving (leave the box blank to keep it), and it isn't
included in backups. After restoring a backup on a new system, enter it again.

## Seeing staff actions in Home Assistant

Every staff action is always recorded in **Staff Access → Activity**. You can also have them show
up in Home Assistant's own **Logbook**, in one of two ways.

**Logbook entries (on by default).** The add-on adds an entry for each action, for example
"Main Boiler set to 140° by Maintenance phone (Staff Portal)". Turn this off under
**Settings → Home Assistant logbook**.

**Link a phone or a staff member to a Home Assistant user.** Home Assistant then credits that
user directly,
the same as if they had tapped it in the Home Assistant app: "Porch Light turned off triggered
by action light.turn_off by Maintenance." Linked phones don't get the extra logbook entry.

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
nobody can read PINs back, even from a backup.

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
- Phones with a PIN ask for it again after **Ask for PIN again after** minutes without being
  touched, even with the app left open on the screen.
- Each phone can send up to 60 actions a minute, and one action per device each second, so a
  stuck button or a script can't flood your equipment.
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
`172.30.32.2,127.0.0.1`), so put it behind something you trust before exposing it.

## Backups, export and import

Everything is stored in the add-on's `/data` folder, which is included in normal Home Assistant
backups. The **Backup** tab adds backups of just Staff Access, and a way to move parts of it
between systems.

**Full backup.** **Download backup** saves one file with your settings, logo, areas, roles,
layout, phones, staff and each phone's own arrangement. **Restore from a backup…** puts all of
that back:

- Phones that were set up keep working without re-pairing.
- Phones still waiting for setup get new QR codes, since setup codes are never saved in files.
- Links to Home Assistant users are never saved in files. Restoring onto the same system keeps
  the links for phones and staff that still match; elsewhere, link them again.
- The activity log stays as it is.
- Before replacing anything, Staff Access saves a copy of the current setup. It's listed under
  **Saved before restoring** (the last 5 are kept), so **Restore this** undoes a restore.
- A damaged file or one from a newer version is refused, and nothing changes.

**Layout, roles and staff** can each be exported and imported on their own:

- **Import layout** loads the file into the Layout tab so you can check it, then click
  **Save layout**. Whole-area items are matched to your areas by name.
- **Import roles** and **Import staff** add to what's there. If a name already exists, you
  choose to **replace** it, **keep both** (the imported one gets "(imported)" after its name), or
  **skip** it. Areas (for roles) and phones (for staff) are matched by name.

Keep these files private. Staff and phone PINs are included only in scrambled form (they can't
be read back), but someone with the file could try to guess them offline.
