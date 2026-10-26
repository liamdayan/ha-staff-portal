"""Run by Home Assistant just before it backs up the add-on (backup_pre in config.yaml): puts
each database's write-ahead log back into its file (staff_portal.db, and the HR portal's
hr/hr.db), so the backup copies up-to-date files. It never fails the backup: if a database is
busy, its log is copied as it is, which SQLite reads back just the same."""
import os
import sqlite3

from db import BUSY_SECONDS, DB_FILE

DATA = os.environ.get("DATA_DIR", "/data")
for path in (os.path.join(DATA, DB_FILE), os.path.join(DATA, "hr", "hr.db")):
    try:
        if os.path.exists(path):
            conn = sqlite3.connect(path, timeout=BUSY_SECONDS)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
    except Exception as e:   # never fails the backup
        print(f"Staff Portal: couldn't checkpoint {os.path.basename(path)} before the backup: {e}")
