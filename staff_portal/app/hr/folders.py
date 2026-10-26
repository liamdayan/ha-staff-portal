"""Where HR files go in Google Drive (through the bridge, gdrive.py), one folder per employee:

  HR Employee Files (the HR shared drive)
    Employees
      Smith, Jane (CT 123456)
        Certificates, Documents, ...   made the first time a file needs one (Discipline and
                                       Reviews only once something goes there)
    Former employees                   a folder moves here when HR confirms someone has left
  Restricted files (I-9, accommodation): in the "HR Restricted" shared drive if the owner picked
  one (Smith, Jane (CT 123456) at its top, and its own Former employees), otherwise in a
  "Restricted" folder in the HR drive, which every member of that drive can open.

Every folder is remembered by its ID (drive_folders) and tagged with appProperties: the person's
folder with their Connecteam user ID. Nothing is ever found by name, so two people called Jane
Smith can't get mixed up, and a folder HR moved by hand is still used. When someone's name
changes in Connecteam, their folder is renamed.

Only the delivery queue (delivery.py) calls this, one request at a time.
"""

import re

from . import gdrive

TAG = "staff_access"
SUBFOLDERS = ("Certificates", "Documents", "Discipline", "Reviews")
MAIN, RESTRICTED = "main", "restricted"
ROOT_NAMES = {"employees": "Employees", "former": "Former employees", "restricted": "Restricted"}

SCHEMA = """
-- Google Drive folders, remembered by ID: a person's folder (sub ''), its subfolders, and the
-- top folders (person_id '': sub is employees, former or restricted).
CREATE TABLE IF NOT EXISTS drive_folders (
    person_id TEXT NOT NULL,
    place TEXT NOT NULL,                 -- main, or restricted (I-9s and accommodation)
    sub TEXT NOT NULL,
    drive TEXT NOT NULL,                 -- the shared drive it's in
    folder_id TEXT NOT NULL,
    name TEXT NOT NULL,                  -- the name it was given (a person's: renamed with them)
    former INTEGER NOT NULL DEFAULT 0,   -- 1: moved to Former employees
    PRIMARY KEY (person_id, place, sub, drive)
);
"""


def folder_name(person: dict) -> str:
    """"Smith, Jane (CT 123456)": the last word of their Connecteam name first (Connecteam's
    first and last names arrive joined), and their Connecteam user ID, which never changes."""
    words = " ".join(str(person.get("name") or "").split()).split(" ")
    shown = f"{words[-1]}, {' '.join(words[:-1])}" if len(words) > 1 else words[0]
    shown = re.sub(r"[\x00-\x1f\x7f/\\]+", " ", shown).strip()[:120] or "Unnamed"
    return f"{shown} (CT {person['id']})"


def clean_subfolder(value) -> str:
    """A kind of document's Drive subfolder, as HR typed it. Raises ValueError."""
    name = " ".join(str(value or "").split())
    if not 1 <= len(name) <= 60 or not re.fullmatch(r"[A-Za-z0-9 &()',.-]+", name):
        raise ValueError("A Drive folder name is 1 to 60 letters, numbers and spaces.")
    return name


def place_drive(settings: dict, place: str) -> tuple[str, bool]:
    """(the shared drive for a place, whether that's a drive of its own for restricted files).
    "" when the HR drive isn't picked yet."""
    main = settings.get("hr_google_drive") or ""
    own = settings.get("hr_google_restricted_drive") or ""
    if place == RESTRICTED and own and main and own != main:
        return own, True
    return main, False


# ---------------------------------------------------------------- remembered folders

def _row(db, person_id: str, place: str, sub: str, drive: str):
    return db.conn.execute("SELECT * FROM drive_folders WHERE person_id = ? AND place = ? AND "
                           "sub = ? AND drive = ?", (person_id, place, sub, drive)).fetchone()


def _remember(db, person_id: str, place: str, sub: str, drive: str, folder_id: str,
              name: str) -> None:
    db.conn.execute("INSERT OR REPLACE INTO drive_folders (person_id, place, sub, drive, folder_id, "
                    "name) VALUES (?, ?, ?, ?, ?, ?)", (person_id, place, sub, drive, folder_id, name))
    db.conn.commit()


def forget(db, drive: str | None = None) -> None:
    """Forget remembered folders (all, or one drive's): they're found again by their tags. For
    when one was deleted or moved out of the drives by hand."""
    if drive is None:
        db.conn.execute("DELETE FROM drive_folders")
    else:
        db.conn.execute("DELETE FROM drive_folders WHERE drive = ?", (drive,))
    db.conn.commit()


def person_rows(db, person_id: str) -> list[dict]:
    """The person's own folders (not subfolders), in every place and drive."""
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM drive_folders WHERE person_id = ? AND sub = ''", (str(person_id),))]


def needs_rename(db, person: dict) -> bool:
    return any(r["name"] != folder_name(person) for r in person_rows(db, person["id"]))


def in_employees(db, person_id: str) -> bool:
    """Whether they have a Drive folder still under Employees (not moved to Former employees)."""
    return any(not r["former"] for r in person_rows(db, person_id))


# ---------------------------------------------------------------- finding and making folders

async def _root(db, bridge, place: str, drive: str, own: bool, which: str) -> str:
    """The folder a place keeps people in ("employees") or former employees in ("former")."""
    if place == RESTRICTED and own and which == "employees":
        return drive   # a restricted drive of its own: people's folders at its top
    found = _row(db, "", place, which, drive)
    if found:
        return found["folder_id"]
    if place == RESTRICTED and not own:
        base = _row(db, "", place, "restricted", drive)
        if base:
            base_id = base["folder_id"]
        else:
            base_id = await bridge.ensure_folder(drive, ROOT_NAMES["restricted"], {TAG: "restricted"})
            _remember(db, "", place, "restricted", drive, base_id, ROOT_NAMES["restricted"])
        if which == "employees":
            return base_id
        parent = base_id
    else:
        parent = drive
    folder_id = await bridge.ensure_folder(parent, ROOT_NAMES[which], {TAG: which})
    _remember(db, "", place, which, drive, folder_id, ROOT_NAMES[which])
    return folder_id


async def person_folder(db, bridge, settings: dict, person: dict, place: str) -> str:
    """The person's folder in a place, found or made, and renamed if their name changed."""
    drive, own = place_drive(settings, place)
    if not drive:
        raise ValueError("No HR shared drive picked.")
    pid, name = str(person["id"]), folder_name(person)
    found = _row(db, pid, place, "", drive)
    if found:
        if found["name"] != name:
            await bridge.rename(found["folder_id"], name)
            db.conn.execute("UPDATE drive_folders SET name = ? WHERE person_id = ? AND place = ? "
                            "AND sub = '' AND drive = ?", (name, pid, place, drive))
            db.conn.commit()
        return found["folder_id"]
    parent = await _root(db, bridge, place, drive, own, "employees")
    folder_id = await bridge.ensure_folder(parent, name, {TAG: "person", "ct_user": pid})
    _remember(db, pid, place, "", drive, folder_id, name)
    return folder_id


async def subfolder(db, bridge, settings: dict, person: dict, place: str, sub: str) -> str:
    """A subfolder of the person's folder (Documents, Certificates, ...), found or made."""
    drive, _ = place_drive(settings, place)
    parent = await person_folder(db, bridge, settings, person, place)
    found = _row(db, str(person["id"]), place, sub, drive)
    if found:
        return found["folder_id"]
    folder_id = await bridge.ensure_folder(parent, sub, {TAG: "folder", "folder": sub[:60]})
    _remember(db, str(person["id"]), place, sub, drive, folder_id, sub)
    return folder_id


async def rename_person(db, bridge, settings: dict, person: dict) -> int:
    """Rename the person's folders whose name is out of date. Returns how many."""
    count = 0
    for row in person_rows(db, person["id"]):
        if row["name"] != folder_name(person) and row["drive"] == place_drive(settings, row["place"])[0]:
            await person_folder(db, bridge, settings, person, row["place"])
            count += 1
    return count


async def move_to_former(db, bridge, settings: dict, person: dict) -> int:
    """HR confirmed they've left: their folders go into Former employees. Returns how many."""
    count = 0
    for row in person_rows(db, person["id"]):
        drive, own = place_drive(settings, row["place"])
        if row["former"] or row["drive"] != drive:
            continue
        await person_folder(db, bridge, settings, person, row["place"])   # renamed if needed
        parent = await _root(db, bridge, row["place"], drive, own, "former")
        await bridge.move(row["folder_id"], parent)
        db.conn.execute("UPDATE drive_folders SET former = 1 WHERE person_id = ? AND place = ? AND "
                        "sub = '' AND drive = ?", (row["person_id"], row["place"], drive))
        db.conn.commit()
        count += 1
    return count


def drive_ok(settings: dict) -> bool:
    """Whether files can go to Drive: the bridge is set up and the HR drive picked."""
    return gdrive.bridge_from_settings(settings) is not None and bool(settings.get("hr_google_drive"))
