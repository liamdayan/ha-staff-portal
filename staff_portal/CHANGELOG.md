# Changelog

## 0.9.2

**Roles tab**

- **Individual devices now folds away**, in a role and in a person's own settings. It's folded
  when you open one, showing how many devices are set; click it to open the list. It stays open
  while you work on that role or person, including after **Save**.

## 0.9.1

**Archiving people**

- **Staff tab: Archive replaces Delete.** An archived person's own access ends at once: they're
  signed out, can't sign in with their PIN on any shared or role-based phone or computer, aren't
  offered in sign-in lists or pickers, can't make wristbands or key cards (a card job of theirs
  still running stops), no key card can be made for them (one for them still being made, by
  anyone, stops and can't be tried again), and Staff Access stops using their Home Assistant
  link. Nothing of theirs is deleted.
- The archive dialog says what archiving can't reach: Kiosk-mode phones and computers (each has
  its own PIN): the ones that look like theirs are listed with **Revoke**; and their Home
  Assistant user, which can still sign in to Home Assistant (turn it off there).
- Archiving shows the person's key cards that still open doors (locks check cards on their own)
  and what to do: **Revoke** with the card in hand, or **Report lost** and write a lost card
  notice (or remote lock-out where it's on). The Key cards list then opens on their cards.
- Archived people are folded away under **Show archived**, each with **Restore** (back as they
  were, signing in with their PIN again) and **Delete for good**. Only archived people can be
  deleted, and only once none of their key cards still opens doors (revoke or report lost first).
- **Door activity:** an archived person's still-valid key card tried at a door alerts like a lost
  card ("Archived person's card tried at Staff Office"), in Activity and, if that setting is on,
  in Home Assistant, at most once per card every 10 minutes.
- **Add everyone from Connecteam** never brings back or re-adds someone you've archived, even
  while they're active in Connecteam.
- Archiving and restoring are in Activity. Backups, migration files and staff exports carry who's
  archived, and imports keep them archived. Restoring a backup made before someone was archived
  keeps them archived (the restore warning and Activity name them), and copying staff in from a
  file skips anyone archived here.
- Backups are now schema 5, so 0.9.0 and older refuse them ("made by a newer version") instead of
  bringing archived people back active and staff doors back unmarked.
- Archived people can't make contractor badges either, whatever their role or their own wristband
  rules allow.

**Staff tab: Key cards**

- **The Key cards section is now folded at first**, showing "Key cards: 14 active, 2 lost". This
  browser remembers whether it's open.

**Staff doors**

- **Doors can now be marked as Staff doors**, as well as Public doors and Guest rooms. Staff doors
  only go on staff key cards and contractor badges. They never show up as guest rooms or as
  day-pass or overnight doors.
- Marking a day-pass or overnight shared door as a staff door takes it off those lists in the same
  step (the dashboard asks first, and Activity says so), so no guest's card opens it from then on.
  A staff door still listed there (from a restored backup, say) is left off every card.
- The doors list on the Wristbands tab now counts public doors, guest rooms and staff doors.

**Contractor badges**

- **New: contractor badges.** Tick **Contractor badges** for a role on the Roles tab, and people
  with that role see a **Contractor badge** button next to **Overnight** on the Wristbands
  screen. They enter the contractor's name and company, pick the doors (any door they have access
  to, staff doors included) and choose when the badge stops working, up to 7 days ahead.
- Wristbands tab: new **Contractor badges** settings for the most badges made at once (5 to start)
  and whether badges open doors that are deadbolted from inside (off to start).
- The card log, the activity log and Door activity show contractor badges with the contractor's
  name and company, and a Door activity alert about one calls it a contractor badge.

**Fixes**

- Staff app, Wristbands: a problem shown after pressing **Make** (under the field it's about) no
  longer disappears when the check of a change made just before it answers late.
- Dashboard: a tick box whose label runs onto a second line (such as **Contractor badges** on a
  phone) keeps its full size.

## 0.9.0

**Make staff key cards from the dashboard**

- **New: This computer (the one you're using).** Make staff key cards straight from the dashboard
  on the card encoder plugged into the computer you're using. It's offered first in **Make key
  card**, **Revoke**, lost card notices and replacement cards. Nothing to set up in **Phones &
  computers**: SN Web Socket just has to be running on that computer.
- The dialog says how this computer's encoder is doing in plain words: connecting, choose
  **Allow** when the browser asks, blocked by the browser, SN Web Socket isn't running, or ready.
  It also says so when Home Assistant isn't open at its https address, which this needs.
- Each Home Assistant administrator has their own: cards are made only on their own computer,
  recorded under their name as made from the dashboard, and listed in **Key cards** and the card
  log.
- If the page is closed mid-card, the next card you start on that computer brings the unfinished
  one back, to **Try again** or **Stop**.
- One account signed in on two computers (a shared front-desk login): a card is only ever made
  on the encoder of the window that started it. A card dialog opened on the other computer takes
  the link but can't carry on (or **Try again**) that card there; **Use it here**, then **Try
  again**, in the first window finishes it.
- TT Hotel's door list keeps being synced when key cards are made only on This computer (or Door
  activity comes in), so new doors still show up.

**Door activity**

- **New: Door activity** on the Wristbands tab. Copy its address into TT Hotel's **Callback URL**
  (Settings → Integration in TT Hotel), and every unlock at a door with a gateway shows up as it
  happens: when, which door, who (the card made here, or TT Hotel's name for it), how (card, app,
  passcode…) and whether the door opened or refused. Filter by door, or show problems only.
- **Last used:** each card says where and when it was last used ("Pool Gate, today 3:12 PM"), in
  the staff app's card log and Look up, and in a new **Last used** column in the Key cards list.
- **Lost card alerts:** a lost or revoked card tried at a door (or a card TT Hotel has deleted) is
  highlighted in Door activity, noted in the Activity tab, and sent to Home Assistant as a
  notification, at most once per card every 10 minutes. Untick **Tell Home Assistant when a lost
  or revoked card is tried** to keep it to the Activity tab.
- Door battery levels now update with each lock record.
- A card cleared here (revoked on an encoder, found blank by Look up, or written over) is done
  with: when it's given out again, here or in TT Hotel's own program, its taps aren't counted as
  the old card's and never alert. A replaced card that was never handed back still alerts.
- The Home Assistant notification names only cards made here (TT Hotel's own names stay in the
  activity log), and shows any link or formatting marks as plain text.
- The address has limits of its own: 300 deliveries and 1,000 records a minute from wherever they
  come (TT Hotel sends anything over that again later). Requests to a wrong address are limited
  separately and never hold up TT Hotel's. Records dated more than an hour ahead are left out.
- The Door activity address is never in backups (migration files carry it), and **Make a new
  address** replaces it if it ever gets out. Staff Access makes no TT Hotel requests for any of
  this, and passcodes are never stored.

**Doors: public doors and guest rooms**

- **New: mark each door as a Public door or a Guest room** (Wristbands tab, **Doors**), one at a
  time or a whole floor or building at once. A line above the list says how many doors aren't
  marked yet.
- Overnight guests' rooms are now picked from the guest rooms, and the shared doors (day-pass
  doors, overnight shared doors, and the doors staff may add) from the public doors. Doors not
  marked yet show in both lists, so nothing disappears after the update.
- Picking a guest room as a shared door, or a public door as a room, is refused with a plain
  message. A door already chosen for day passes or overnight wristbands keeps working if you mark
  it the other way later, and the Wristbands tab warns about it.
- Syncing doors from TT Hotel never changes what a door is marked as. Backups and migration files
  carry it, and restoring an older backup leaves it as it is.

**Day passes: a later end time**

- **New wristband rule for roles and people: "May change when day passes stop working".** People
  with it get a **Works until** time on the day pass form (any time later than now, up to 11:59
  PM), and can still make day passes after the usual end time by picking a later one. The
  wristbands, the card log and the activity log use the time picked.

**Clocking in, by department**

- **Departments can now set the Connecteam clock-in rule** for everyone in them: **Must be clocked
  in**, **Doesn't need to clock in**, or **Each person's own setting** (Staff tab, Departments,
  Edit).
- A person's own clock-in setting, changed by hand, still beats their department's. Their row on
  the Staff tab says where their rule comes from (**From Housekeeping**, **Set by hand** or
  **Default**) and offers **Use the department's setting**.
- A change to a department's rule counts from the next sign-in. The staff app's list of names
  shows **Not clocked in** by the same rule, and the time clock is only checked while someone
  actually needs it.
- Moving someone to another department, or saving **Edit** without touching the switch, no longer
  turns their clock-in rule into a by-hand one.
- Backups, migration files and department exports carry each department's clock-in rule. Staff
  exports keep a person's by-hand setting.

**Settings → Connections**

- **TT Hotel and Connecteam are now together under Settings → Connections.** Each is folded to
  one line showing whether it works (**Connected**, **Not set up**, **Paused until…**, **Stopped:
  access refused** and so on) and how many requests Staff Access has used, such as "1,240 requests
  this month (limit 7,500)". Click one to see its details.
- Each connection has its own Save button that saves only that connection, so saving one never
  changes the other or the rest of Settings. **Test connection** asks you to save unsaved changes
  first, and says so when Wristbands is off.
- The Wristbands tab keeps its on/off switch, times, doors and card options. A line under the
  switch shows whether the TT Hotel account is connected, with a link that opens it in Settings.
  **Sync from TT Hotel** is still next to the door list.
- The Activity tab now says "Updated TT Hotel settings" or "Updated Connecteam settings" when only
  that connection was saved.
- **Fixed:** after you removed the saved Connecteam Client Secret, a new secret typed in without
  reloading the page could be removed again on the next save.

## 0.8.10

**Staff tab**

- **A thin row per person**: their name, role and department, anything that needs a look
  (inactive, locked, not clocked in, a broken Home Assistant link) and when they last signed
  in. Click a row for everything else and its buttons (Edit, Make key card, Access and so on).
- **Search** by name, role, department or Home Assistant user.
- **Sort** by department, name or role. By department, each department is a group with a count
  that folds away with a click. The sort and the folded departments are remembered in that
  browser.

**Signing in**

- **New setting: how long the PIN pad waits after the last digit**, in milliseconds (Settings →
  Security; 500 to start with, 200 to 5,000). It's used where PINs differ in length; where they
  all have the same number of digits, the PIN pad still signs in at the last one. Phones and
  computers use a new value the next time they show the PIN pad.

## 0.8.9

**Card encoders**

- **New: Reconnect.** When the Wristbands screen says **The card encoder isn't answering**,
  **Reconnect** has the add-on connect to the encoder straight away and say how it is now.
  Before, the last problem stayed on show until the encoder was next used, such as for a
  look-up. It's the same check as **Test encoder** on the dashboard.
- **The encoder's error code is on screen.** A problem the encoder or SN Web Socket reports ends
  with its own code ("Encoder error 1004."), and the dashboard's messages say it too. A code
  the add-on has no words for says "The card encoder reported a problem" with the code, rather
  than claiming a card couldn't be written.
- A look-up that can't connect to the encoder now shows that on the Wristbands screen until the
  encoder is reached again, as a card that can't connect does.

## 0.8.8

- **Clearer: "The card encoder wasn't found."** When SN Web Socket can't find the encoder, staff
  were told it wasn't plugged in. It's just as often plugged in without its driver (Silicon Labs
  CP210x), and then it acts as a plain card reader that types card numbers like a keyboard. The
  message now says to check both, and the documentation's Troubleshooting walks through
  checking and installing the driver.

## 0.8.7

**Card encoders**

- **Fixed: an encoder that types card numbers like a keyboard.** Some encoders also type each
  card's number, often with Enter after it, when a card is put on them. That could fill in the
  PIN pad (and count as a wrong PIN), add the number to a guest's name, or press whichever
  button was highlighted, such as **Try again** or a dashboard dialog's **OK**. Nobody types
  that fast, so the staff app and the dashboard now recognise it and ignore it: what it typed is
  taken back and its Enter does nothing. On the PIN pad it says "That was a card on the
  encoder."
- The card log says **What happened** when a card wasn't made: the encoder's last problem with
  it, or how the job ended (such as "Cancelled before any wristbands were made").

## 0.8.6

**Migrate to another install**

- **New: Migrate** on the **Backup** tab moves everything to another install of the add-on, such
  as when you switch Home Assistant from one add-on repository to another (Home Assistant treats
  that as a different add-on). **Create migration file** on the old install saves a file and shows
  a one-time code; **Restore from migration file…** on the new install takes both. Phones and
  computers stay signed in and nothing needs typing again: PINs, the TT Hotel and Connecteam
  secrets, the door list, links to Home Assistant users, the activity log and the card history
  all come across. The Cloudflare Tunnel needs no change.
- The file is encrypted (AES-256-GCM, with a key made from the code by scrypt). The code is 20
  letters and numbers, shown once and never stored or logged: nothing else opens the file. A file
  works for 24 hours and only on a fresh install. A wrong code, a changed or expired file, or one
  made by a newer version is refused, and nothing changes. The new install saves a copy of what
  it had first.

**Backups**

- **Changed: backups include the activity log and the card history** (wristbands and key cards
  made, and lost-card lock-outs). Restoring merges them: entries not already here are added, and
  nothing here is removed, within the usual limits. Card jobs that were still running when the
  backup was made come back as stopped. Backups from before 0.8.6 restore as before.
- Backups download compressed (`.json.gz`, about a tenth of the size). **Restore from a backup…**
  takes `.json.gz` and `.json` files, and uploads the file as it is. Copies saved before a restore
  are compressed too and include the logs; **Restore this** restores one straight from where it's
  saved. A backup file may now be up to 96 MB as uploaded, and 192 MB unpacked.
- Only one backup, restore or migration runs at a time; another one is asked to wait.

**Faster under load**

- **The database no longer waits for the disk on every change** (SQLite's write-ahead log). On
  an SD card or eMMC, common in Home Assistant boxes, every change could hold up the staff app and
  the dashboard for 40–200 ms, adding up to seconds in a busy shift; now it takes under a
  millisecond. A power cut can lose the last few changes, never the database. Just before Home
  Assistant backs the add-on up, the log is put back into the database file.
- Making a backup, the copy saved before a restore, and a migration file no longer hold up the
  staff app: with years of card history, about two seconds before, a tenth of a second now.
- Faster with a long card history: starting a card, each person's own card log, the key card
  counts on the dashboard, and finding a card by its number.
- **Changed: card numbers are found by how they start**, in the card log and the Key cards list,
  not by digits in the middle. Six digits or more search card numbers only.

## 0.8.5

**Card encoders: This computer (USB)**

- The Wristbands header now says exactly why the encoder can't connect, in the browser's own
  words, with **Try again**:
  - **Encoder offline**: SN Web Socket isn't running on this computer. Chrome and Edge only ask
    for permission once it is, so the page says to start it first.
  - **Connecting encoder…** while the browser asks whether the site may **access other apps and
    services on this device**: choose **Allow**.
  - **Encoder not allowed**: nobody answered, or the question was closed. It isn't asked again
    by itself (Chrome can stop asking a site for a while after that happens a few times): **Try
    again** asks again.
  - **Encoder blocked**: names the setting to change, **Apps on device** (**Local network
    access** before Chrome 145).
- Fixed: a site blocked under the setting's old name showed **Encoder offline** ("is it
  running?") instead of **Encoder blocked**.
- Allowing it in the site settings connects straight away, even while the browser's question is
  still showing.
- The dashboard's and the add-on's messages for a blocked encoder name the same setting.

## 0.8.4

**Card encoders**

- **New: This computer (USB), now the default for new computers.** The card encoder plugged into
  a front-desk computer connects through the staff app open on that same computer, so nothing
  has to be set up on the network: no IP address, no search, and Staff Access never connects to
  the computer over the network. It connects while someone signed in there has **Wristbands**
  open, with TT Hotel's SN Web Socket running. The first time, Chrome or Edge asks whether the
  staff app may access devices on your local network: choose **Allow**. New phones still have no
  card encoder, and computers already set up keep theirs.
- The Wristbands header on that computer says **Encoder ready**, or what's wrong: SN Web Socket
  isn't running, the browser is blocking local network access, or Wristbands is open in another
  window (**Use it here** takes it back).
- A job started while Wristbands isn't open on that computer, including **Make key card** from
  the Staff tab, waits for it ("Open Wristbands on Front Desk PC to connect the encoder"), then
  needs attention. Closing that tab during a job leaves it needing attention too; **Try again**
  carries on once Wristbands is open there again.
- **Test encoder** works for This computer (USB) too, through that computer's browser, and says
  to open Wristbands there when it isn't open.
- The dashboard shows each such computer's encoder as **connected** or **not connected** (with
  when it was last connected), and updates it by itself while Phones & computers or Wristbands is
  open.
- **Changed: Automatic only uses an encoder you've confirmed.** **Find encoders** lists the SN
  Web Socket helpers found, each with **Use this encoder**, and none is used until you click it,
  even when only one is found. If the one you confirmed isn't found later, the computer says so
  instead of switching to another. A safety improvement: the encoder is sent TT Hotel's encoder
  key, so it should only ever go to one you've checked. Automatic computers set up before 0.8.4
  that had picked one of several keep it; ones using the only encoder found need **Use this
  encoder** once.

**Backups**

- Backups include each computer's This computer (USB) setting. Older backups still restore, with
  each computer's card encoder as it was.

**Signing in**

- **Faster sign-in:** when a PIN's length isn't known, the PIN pad now signs in half a second
  after the last digit instead of a second.
- **The PIN pad signs in instantly for more people:** for people linked to Connecteam whose PIN is
  their kiosk code, the staff sync now fills in their PIN's length, which wasn't recorded for PINs
  set before 0.8.3.

## 0.8.3

**Signing in**

- **New: separate idle times for phones and computers.** **Settings → Security** now has **Ask
  for PIN again after (minutes)** for **phones** (the existing setting, still 15 minutes) and for
  **computers** (5 minutes by default). Each device signs out after its own, on the add-on and in
  the app.
- **Sign in without pressing OK.** The PIN pad signs in as soon as the last digit is typed when
  every PIN that can sign in on that device has the same number of digits, and otherwise about a
  second after the typing stops (once at least 4 digits are typed). OK and Enter still work. Only
  that shared number of digits is ever sent to the device, never anyone's own, and never a PIN.
  PINs set before 0.8.3 count as "not known" until they're set again, so devices with them wait
  for the pause.
- **New computers hide names.** Adding a computer that's **Shared** or **Role-based** now ticks
  **Hide names: people just type their PIN**, so a front desk screen doesn't list everyone. Phones
  still show names, and existing devices are unchanged.
- Messages such as "Can't reach …" in the staff app use your **Property name** instead of "Staff
  Portal".

**Look up a wristband**

- **Look up shows what's on the card first.** A card with no doors on it (cleared, or cancelled in
  TT Hotel's own program or app) now says **This card has no doors on it**, and if our record
  still said Active it's marked **Revoked** ("Found cleared on the encoder") and logged. Otherwise
  the card's own doors and end time come first, then our record.
- **New: Revoke (wipe this card)** on a looked-up card, for people who see everyone's cards or who
  made it: the card is already on the encoder, so it's cleared straight away, with the usual
  progress screen.
- Active cards say that a card reported lost in the TT Hotel app isn't known to Staff Portal: use
  **Report lost** in Staff Access so it's tracked.

**Staff key cards, departments and lost cards**

- **New: Key cards list** on the Staff tab: every staff key card (made in the dashboard or the
  staff app), filtered by Active, Expired, Revoked or Lost and searchable by name or card number,
  with who made it, what it opens and when it stops working.
- **New: Make key card** on each person's card on the Staff tab, from their department's key card
  template, with the same live progress as the front desk. If they already have one, choose
  **Replace their current card** (the new card cancels the old one at each door it's tapped on)
  or **Add another card**.
- **New: Revoke** a card you have in hand: the encoder waits for that exact card and clears it.
  Any other card is left alone ("That's card 123…, not 456…"). People who may see everyone's
  cards can revoke from the staff app's card log too, which now shows each staff card's state.
- **New: Report lost**, then lock the card out door by door with a **lost card notice** (a spare
  card carrying a cancellation of the lost number, ticked off per door) or a **replacement card**.
  **Look up** shows what a notice card cancels.
- **New, experimental: remote lock-out** of a lost card on doors with a gateway, through TT Hotel
  (2 requests a door, a busy gateway tried again at most 3 times, minutes apart). Off by default,
  and usable only after the **Test remote lock-out** walkthrough confirms it works with your
  locks. A number locked out this way is retired on that door for good.
- **New: Departments** (Staff tab): a Connecteam smart group, a role and a key card template
  (doors, days, deadbolt) each. In backups and exports.

**Connecteam**

- **New: Add everyone from Connecteam.** Every active Connecteam user becomes a staff member, with
  their kiosk code as their PIN (or turned off, with a note, when it can't be used), in the
  department of their smart group and with its role. Checked about once an hour within the
  existing request limits, plus **Sync staff now**. People archived in Connecteam are turned off,
  never deleted; people added by hand are never changed.
- **New: clock-in per person.** People the sync adds must be clocked in by default (a setting).
  A **Must be clocked in to sign in** switch on each linked person's Staff card (and in **Edit**)
  turns it off or on for just them, and the sync never changes it back.

**Backups**

- Backups include departments, each person's department and Connecteam sync details, the new
  idle time for computers and how many digits each PIN has, never whether remote lock-out was
  tested. Older backups still restore (with no departments, and PIN lengths not known).

## 0.8.2

- **Fixed: Staff Portal now stops cleanly** when Home Assistant updates or restarts it, letting go
  of the card encoder first (Home Assistant used to report that it "did not handle SIGTERM").

## 0.8.1

- **Fixed: a wristband you just looked up couldn't be written.** Making wristbands right after
  looking one up waited for it to be lifted off the encoder first. A looked-up wristband can now be
  reused straight away.
- **Fixed: the encoder sometimes wouldn't reconnect after TT Hotel's own program used it** until
  its cable was replugged. Staff Portal now releases any old connection in SN Web Socket before
  connecting.

## 0.8.0

**Wristbands (TT Hotel key cards)**

- **New: make TT Hotel wristbands and key cards at the front desk.** Staff signed in on a
  front-desk computer with a card encoder open **Wristbands** and make **day passes** (a party's
  wristbands for the day-pass doors, until 8:00 PM), **overnight** wristbands (their rooms plus
  shared doors, until checkout at 11:00 AM) and **staff cards** (single doors, floors, buildings
  or every door, until a chosen day). The screen walks them through each wristband on the
  encoder, with **Try again**, **Skip this one** and **Stop** if one doesn't work.
- **New: adjustable times.** Set when day passes stop working and the checkout time on the new
  **Wristbands** tab. After the end time, the day pass is greyed out and says why.
- **New: Look up a wristband** shows its card number, who it was made for and by whom, when it
  stops working and which doors it opens. The **Card log** lists every card made, with its
  numbers; the **Wristbands** tab's **Cards made** shows everyone's.
- **New: Wristbands tab** for the TT Hotel account (**Test connection**), the day-pass and
  overnight doors, staff-card defaults, the time zone, the card encoders and the door list
  (**Sync from TT Hotel**, rename or hide doors).
- **New: who may make what.** On the **Roles** tab, each role (and on role-based computers, each
  person) gets card types, which doors, the longest stay and staff card, and whether they see
  everyone's cards. The add-on checks every request again, and refusals are logged.
- **New: Card encoder** on a computer in **Phones & computers → Edit**: **Off** (the default),
  **Automatic** or an **Address**, with **Test encoder**. The add-on talks to the encoder through
  TT Hotel's free SN Web Socket on that computer, and lets go of it after each batch so the TT
  Hotel program can use it too.
- **New: Automatic card encoders.** Staff Access finds SN Web Socket on the networks in Home
  Assistant's network setting (private networks, at most 254 addresses each, or the networks
  entered under **Wristbands → Advanced**), so the desk computer no longer needs a fixed IP
  address. It looks at startup, when an Automatic encoder isn't found or stops answering (at
  most every 10 minutes) and when you click **Find encoders**, and never while nothing uses
  Automatic. With several front-desk computers, choose which one each desk uses. A typed address
  always wins.
- **Tools now sit above the pages** in the staff app's sidebar and in the phone's tab bar, so
  Wristbands is the first thing staff see at the desk.
- **New: why Wristbands won't show.** Each computer's card says so when Wristbands won't appear
  there: Wristbands turned off, no card encoder (or none found), Kiosk mode for one person or
  team, or a shared computer whose role can't make wristbands.
- TT Hotel requests are kept to a small share of the free 30,000 a month: at most about one
  every 8 minutes while cards are being made (the encoder's key is reused), a door sync every 6
  hours, and a hard cap of 7,500 a month. When TT Hotel says stop, Staff Access stays stopped
  across restarts. Wristbands makes no Connecteam requests.

**Backups**

- Backups include wristband settings, door names, roles' and people's wristband rules and each
  computer's card encoder setting, never the TT Hotel Client Secret or password. Older backups
  still restore (with no wristbands). An older version of the add-on refuses a 0.8.0 backup.

## 0.7.1

- **Controls lock when the screen turns off.** As soon as the phone's screen goes off or the app
  goes to the background (another app, or another browser tab on a computer), the controls lock,
  so a phone that's been in a pocket comes back locked.
- **Fixed: thermostat changes looked slow or ignored.** Devices that report a new setting late,
  such as minisplits, made the tile jump back to the old temperature or mode until the device
  checked in, sometimes many seconds later. The tile now keeps the setting staff chose, marked
  **Updating…**, checks every 1.5 seconds until the device confirms it, and says so if it still
  hasn't after 90 seconds. The same goes for setpoints, dropdowns and on/off switches.

## 0.7.0

**Kiosk and role-based devices**

- **New: a Mode for each phone and computer.** **Kiosk** works as before: the device's own role
  and pages, for one person or team, or for people who each sign in. **Role-based** devices have
  each person sign in with their own PIN and then show what *their* role allows, on their role's
  pages, so a shared device shows each person something different. Existing devices are kiosk
  devices and work exactly as before.
- **New: people have roles.** Pick each person's role on the Staff tab (on their card or in
  **Edit**). Roles work as staff groups.
- **New: roles have pages**, for the people who have them on role-based devices. On the Layout
  tab, **Shown on** and **Choose devices** also list roles once you have a role-based device.
- **New: one person's own settings.** On the Roles tab, people are listed under their role; click
  someone (or **Access** on their Staff card) to raise or lower their access to an area or a single
  device, or give them their own pages, without changing their role. Their own settings beat their
  role and items' access on the layout.
- On role-based devices the header shows who's signed in and their role, and the activity log
  records their role.
- **Preview** can show a role or a person, as well as a kiosk device.
- Every control is still checked by the add-on, now against the signed-in person's access on
  role-based devices.

**Backups and imports**

- Backups include people's roles, pages and own settings, and roles' pages. Backups from earlier
  versions still restore. An older version of the add-on refuses a 0.7.0 backup instead of
  quietly dropping these.
- Exported roles and staff carry their role, pages and areas by name, since IDs differ between
  systems. Anything with no match is listed after the import, and pages with no match are left
  out, never replaced by every page.
- **Fixed:** a backup couldn't be restored once all the roles together had more than 5,000 device
  settings (a few big roles on a large system). Settings like these can now number 200,000.

## 0.6.20

**Layout**

- **New: page groups.** Put pages that go together in a group, such as "Pool staff", and give
  the whole group to phones and computers with **Choose devices** instead of page by page.
  Devices that show a group also get pages added to it later. A group's pages stay together in
  the page list and move together. Staff don't see groups, just their pages.
- **New: Duplicate group** copies a group with all its pages, ready to rename and give to other
  devices.
- **Delete group** asks whether to keep its pages or delete them too. Kept pages stay on the
  devices that showed the group.
- **New: Preview** shows the layout as a phone or as a computer would, including unsaved
  changes and live readings. Preview everything on the layout, or exactly what one phone or
  computer would show. Tapping a control in the preview sends nothing.
- The device dialog lists a group once instead of each of its pages. Taking one page away from a
  device that showed every page keeps its groups as groups, so it still gets their new pages.
- The docs now call the button **Choose devices**, as the dashboard does.

**Staff app**

- **Fixed:** on phones not allowed to rearrange, the word "null" showed at the bottom of each
  page.

## 0.6.19

Fixes from a security review.

**Sign-in**

- **Fixed:** wrong PINs on shared phones and computers could be paced (a few every 15 minutes) so
  the device never locked, which let someone holding the device keep guessing PINs indefinitely.
  20 wrong PINs on one device within 24 hours now lock it until you unlock it, however slowly
  they come. Unlocking it clears the count.
- **Fixed:** checking PINs ran on the add-on's main thread, so repeated sign-ins could make the
  whole add-on (staff app and dashboard) unresponsive. PIN checks now run separately, one at a
  time per device, and each device can try to sign in at most 15 times a minute.
- **Fixed:** with Connecteam on, anyone holding a phone could make the add-on ask Connecteam
  about a clocked-out person without knowing their PIN, using up your daily share of requests.
  Connecteam is now only asked after a correct PIN, and sign-ins stop asking once three quarters
  of the day's share is used, keeping the rest for the hourly checks.

**Backups and imports**

- **Fixed:** restoring a backup trusted every phone credential in the file, so an edited backup
  could add a working phone, and an older backup brought back phones you had revoked since (a lost
  phone, for example). A restore now keeps a phone signed in only if this system set it up and
  hasn't revoked it. Any other phone needs setting up again, and the restore warning names them
  first. Undoing a restore still keeps phones signed in.
- **Fixed:** a restore could set or clear the Connecteam Client Secret. It never changes it now.
- **Fixed:** importing staff with **Replace** turned people you had turned off back on. They stay
  off now, and anyone whose PIN the import changes is signed out. **Skip** is now the selected
  choice, and **Replace** is listed last, in red.

**Staff app**

- **Fixed:** a command that failed in Home Assistant, retried within 3 seconds, was taken for a
  double tap and showed as done without being sent.
- **Fixed:** idle connections were held open for up to an hour, so someone on your network could
  tie up the add-on by opening a lot of them. Idle and slow connections are now closed after
  15 seconds, and a request must arrive within 10 seconds (2 minutes on the dashboard, for large
  backups).
- **Fixed:** opening a computer's setup link set it up immediately, so an email link scanner (or
  anyone who opened the link first) could use up the one-time link. Computers now show a
  **Set up this computer** button.
- **Fixed:** a device whose options Home Assistant reported in an unexpected format could accept
  a partial match, give a server error, or stop the screen updating. One unusual device no longer
  affects the rest of the screen.
- Error responses now carry the same security headers as everything else.

**Dashboard**

- Every change needs the dashboard's anti-forgery header, including from the add-on's own
  computer (this only matters if you run it in plain Docker behind a proxy).
- Requests from Home Assistant users who aren't admins no longer each cause a lookup of all
  users in Home Assistant.
- More than 200 wrong setup codes in 10 minutes from any addresses pause setup briefly.

**Add-on**

- Updated to Python 3.13 on Alpine 3.24 (the previous base image hadn't been updated since
  May 2025) and Pillow 13.0.0, which fixes many image-decoding security issues, including
  critical ones in JPEG decoding.
- Logos can be PNG, JPEG or WebP. GIF uploads are no longer accepted.
- Connecteam: a "slow down" reply makes Staff Access wait at most 24 hours, and an unexpected
  reply from Connecteam can no longer cause a server error.
- The docs now say clearly that backups and staff exports contain PIN hashes that short PINs
  can be worked out from offline, so keep those files private.

## 0.6.18

- Repeated taps are ignored for 3 seconds instead of 5, so the same command can be sent again
  sooner.

## 0.6.17

- **Hide names (PIN only)** for shared phones and computers (**Edit → Who uses it**): the
  sign-in screen shows only a PIN pad, and the PIN says who's signing in. The device never
  receives the list of names. Wrong PINs lock the device after the usual limit.
- Each person must have their own PIN: a PIN someone else already uses is refused.
- **Fixed:** double taps sent a command twice (two "turn off" entries a second or two apart).
  The same command from the same device within 5 seconds is now ignored and not logged, an
  on/off command for something already in that state does nothing, and the app ignores taps on
  a control while its command is being sent.
- Home Assistant logbook entries ("… by Bob (Maintenance phone) via Staff Portal") are written for
  people and phones linked to a Home Assistant user too, not only unlinked ones.

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
