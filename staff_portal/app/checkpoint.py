"""Run by Home Assistant just before it backs up the add-on (backup_pre in config.yaml): puts
the database's write-ahead log back into staff_portal.db, so the backup copies an up-to-date
file. It never fails the backup: if the database is busy, the log is copied as it is, which
SQLite reads back just the same."""
import os
import sqlite3

from db import BUSY_SECONDS, DB_FILE

path = os.path.join(os.environ.get("DATA_DIR", "/data"), DB_FILE)
try:
    if os.path.exists(path):
        conn = sqlite3.connect(path, timeout=BUSY_SECONDS)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
except Exception as e:   # never fails the backup
    print(f"Staff Portal: couldn't checkpoint the database before the backup: {e}")
