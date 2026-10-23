# Staff Portal

A branded, locked-down web app for staff phones. You decide which devices each staff role can
**see** and which they can **control**, from a dashboard in your Home Assistant sidebar.

- Staff phones never get a Home Assistant login. They talk only to this add-on, and the add-on
  refuses anything their role doesn't allow, even if someone pokes at it directly.
- Phones are set up by scanning a QR code once and stay signed in until you revoke them.
- Optional PIN per phone. Too many wrong PINs locks the phone until you unlock it.
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

> Don't put a Cloudflare Access login page in front of the staff hostname. The app does its
> own sign-in, and an Access page would make staff log in again every few days.

## 3. Brand it

In **Settings**, set your property name, the short name shown under the home-screen icon, your
brand color, and upload a logo (a square PNG works best). Phones pick up the name and colors
right away. iOS caches the home-screen icon itself, so a new logo shows up on phones that are
set up after the change, or after re-pairing.

## 4. Create roles

In **Roles**, create a role such as *Maintenance*. Every device in Home Assistant is listed,
grouped by area. For each one choose:

| Setting | What staff get |
|---|---|
| **Hidden** | Nothing. The phone can't see or use it. |
| **View** | Status only (for example, "Locked," "Dry," "61.4 °F"). |
| **Control** | Status plus the controls for it. |

Sensors can only be set to View. Click **Save role**. Phones with that role update within a few
seconds.

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

## 5. Add phones

1. In **Phones**, click **Add phone**, name it (such as *Maintenance phone*), pick a role, and
   optionally set a 4- to 8-digit PIN.
2. A QR code appears. On the phone, open the **Camera**, scan it, and tap the link.
3. Follow the on-screen prompt to **Add to Home Screen**, then open the app from the new icon.

Each QR code works once and expires (72 hours by default, changeable in Settings).

**Managing phones:**

- **Revoke** signs the phone out immediately, such as for a lost phone or a departing employee.
- **Re-pair** signs the phone out and makes a new QR code, for example after resetting a phone.
- **Unlock** clears a lock after too many wrong PINs.
- Changing a phone's role takes effect within a few seconds.

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

## Backups

All settings, roles, phones and the activity log are stored in the add-on's `/data`
folder, which is included in normal Home Assistant backups.
