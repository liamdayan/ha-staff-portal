# Staff Portal for Home Assistant

A Home Assistant add-on that gives your staff a branded, home-screen web app showing only the
devices their role allows, with a dashboard in Home Assistant for managing access.

- **Admin dashboard** (Home Assistant sidebar → Staff Access): roles, per-device View/Control
  access, phone setup by QR code, one-click revoke, PIN lockouts, activity log, branding.
- **Staff app** (served on port 8787 through your Cloudflare Tunnel): installs to the home
  screen with your logo, stays signed in, and access rules are enforced on the server.

See [staff_portal/DOCS.md](staff_portal/DOCS.md) for installation and setup.

## Layout

```
repository.yaml            Home Assistant add-on repository manifest
staff_portal/
  config.yaml              Add-on manifest (ingress for admin, port 8787 for staff)
  Dockerfile
  DOCS.md                  Setup guide (shown in the add-on's Documentation tab)
  app/
    main.py                Web servers, auth, admin and staff APIs
    permissions.py         What each device type allows, plus parameter validation
    db.py                  SQLite storage
    ha.py                  Home Assistant REST client
    branding.py            Logo handling and home-screen icons
    admin/                 Admin dashboard (plain HTML/JS/CSS)
    staff/                 Staff web app (plain HTML/JS/CSS)
```

## Local development

```sh
pip install aiohttp segno Pillow
cd staff_portal/app
DATA_DIR=./.dev-data HA_URL=http://homeassistant.local:8123 HA_TOKEN=<long-lived token> python3 main.py
```

Admin dashboard: http://127.0.0.1:8099 · Staff app: http://127.0.0.1:8787
