# Changelog

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
