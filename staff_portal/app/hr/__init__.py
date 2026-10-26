"""The HR portal: a third web server in the add-on (HR_PORT, 8788), reached only through the
Cloudflare Tunnel at its own address, for employee files, documents and certificates.

It's off until the owner turns it on in the dashboard; while off it answers a plain "turned off"
page, asks nothing of Connecteam or Google, and keeps nothing.

  hrdb.py     its own SQLite database, DATA_DIR/hr/hr.db (people keyed by Connecteam user ID)
  crypto.py   the master key and AES-256-GCM for fields and files; the HR recovery key
  totp.py     authenticator codes (RFC 6238)
  signin.py   sign-in: mobile number + kiosk code (+ authenticator code), sessions, lockouts
  access.py   roles, who may see whom, visibility levels, and the rule on every route
  people.py   people from Connecteam's user list (hourly), with the safety catch
  audit.py    the audit log, chained so changes show up
  notify.py   portal inbox, and Connecteam chat messages through a durable outbox
  files.py    file type checks, cleaned images, safe file responses
  service.py  the running HR portal: start, stop, background work, dashboard actions
  server.py   the HR web app and its pages (web/)
  ctapi.py    HR's own Connecteam requests: chat messages, certificate copies (HR's lane)
  gdrive.py   Google Drive through the owner's Apps Script (bridge.gs), and its OCR
  ocr_dates.py  the dates on a card, from the text Google read
  documents.py  kinds of document and who sees each, files, recycle bin, retention, legal holds,
                and other parts' kinds (register_kind, add_generated, make_pdf)
  folders.py    each person's folders in Google Drive, found by ID and tag, never by name
  delivery.py   the queue of work for Google Drive and Connecteam (retries, Needs attention)
  docroutes.py  the Documents section, opening and adding files, Needs attention, kinds settings
  pdfdoc.py     plain, escaped HTML that Google Drive turns into PDFs (write-ups, reviews)

Nothing here imports main.py: main.py hands the service what it needs.
"""
