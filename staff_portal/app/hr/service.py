"""The running HR portal: started when the owner turns it on (or at startup if it's on), stopped
when they turn it off. While it's off, nothing here runs: no requests, no background work, and
nothing kept (its web server just says it's turned off).

Background work, every half minute while it runs:
- people from Connecteam hourly (and, around sign-ins, at most every 5 minutes for someone new;
  and soon again when HR's request lane had no room);
- Connecteam chat messages that are due;
- once a day at 2:15 AM (HR's time zone): Connecteam's profile field names, its archived users,
  and the daily jobs other parts of the HR portal add (daily_jobs);
- the daily summary for managers and HR at the time HR set;
- hourly: forgetting ended sessions and setup codes;
- the background work other parts add (tick_jobs), such as the documents' delivery queue.

main.py makes one HRService and hands it what it needs from the rest of the add-on, so nothing
in hr/ imports main.py.
"""

import asyncio
import base64
import json
import logging
import os
import shutil
import sqlite3
import time
import urllib.parse
from datetime import datetime

from connecteam import ConnecteamError

from . import access, audit, crypto, ctapi, gdrive, hrdb, notify, people, signin

log = logging.getLogger("staff_portal")

TICK = 30
SYNC_EVERY = 3600
SYNC_SOON_GAP = 5 * 60          # a sync for someone new signing in, at most this often...
SOON_A_DAY = 48                 # ...and this many a day, so strangers typing numbers can't use up
                                # HR's Connecteam requests
BUSY_RETRY = 90                 # a sync HR's lane had no room for is tried again this soon
DAILY_AT = (2, 15)              # the nightly run, in HR's time zone
MAX_MIGRATED_DB = 64 * 1024 * 1024
NO_CONNECTEAM = ("Connecteam is turned off in Settings → Connections, so the HR portal can't "
                 "read its people.")

# The parts of the HR portal beyond its foundation (documents.py, ...): each is fn(service), called
# when a service is made, and adds its hooks (on_start, tick_jobs, daily_jobs, ...). A part adds
# itself here when it's imported.
PARTS: list = []


class HRService:
    def __init__(self, data_dir: str, *, main_settings, connecteam, departments=lambda: [],
                 group_names=lambda: {}, from_tunnel=lambda remote: False,
                 client_ip=lambda request: request.remote or "", property_name=lambda: "",
                 icon=lambda: None, version: str = "dev"):
        self.folder = os.path.join(data_dir, "hr")
        self.main_settings = main_settings        # () -> Staff Access's settings
        self.connecteam = connecteam
        self.departments = departments            # () -> Staff Access's departments
        self.group_names = group_names            # () -> {smart group ID: name}, never asks
        self.from_tunnel = from_tunnel
        self.client_ip = client_ip
        self.property_name = property_name
        self.icon = icon                          # () -> PNG bytes of the add-on's icon
        self.version = version
        self.keyring = crypto.Keyring(self.folder)
        self.db: hrdb.HRDatabase | None = None
        self.throttle = signin.Throttle()
        self.sync_state = {"at": None, "error": None, "tried": 0.0, "result": None, "soon": False,
                           "soon_day": "", "soon_count": 0, "retry_at": None}
        self.daily_state = {"day": None, "summary_day": None, "hourly": 0.0}
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        # Other parts of the HR portal hook in here:
        self.on_start: list = []            # fn(service) after the database opens (their tables)
        self.daily_jobs: list = []          # async fn(service), in the nightly run
        self.migration_blockers: list = []  # fn(service) -> why a migration must wait, or None
        self.tick_jobs: list = []           # async fn(service, now), every half minute
        self.on_stop: list = []             # async fn(service), before the database closes
        for part in PARTS:
            part(self)

    # ------------------------------------------------------------ settings and state

    def enabled(self) -> bool:
        return self.main_settings().get("hr_enabled") == "1"

    def host(self) -> str:
        return self.main_settings().get("hr_host", "")

    def url(self) -> str:
        return f"https://{self.host()}" if self.host() else ""

    @property
    def running(self) -> bool:
        return self.db is not None and self.keyring.ready

    def status(self) -> str:
        """"off", "on", "locked" (needs the recovery key) or "lost" (no key at all)."""
        if not self.enabled():
            return "off"
        if self.running:
            return "on"
        state = self.keyring.status()
        return state if state in ("locked", "lost") else "off"

    def chat_wanted(self) -> bool:
        """Whether the owner turned on Connecteam chat messages (Settings → Connections →
        Connecteam for HR) with keys to send them: the HR connection's own app, or else Staff
        Access's (hr/ctapi.py uses that when HR has none)."""
        s = self.main_settings()
        own = s.get("hr_connecteam_client_id") and s.get("hr_connecteam_client_secret")
        main = s.get("connecteam_client_id") and s.get("connecteam_client_secret")
        return (s.get("hr_connecteam_chat") == "1" and s.get("connecteam_enabled") == "1"
                and bool(own or main))

    def chat_problem(self) -> str | None:
        """Why chat messages aren't going out although they're on: Connecteam refused them (a
        missing permission or plan feature, or the keys), until the owner saves the connection
        again or presses its Test."""
        lane = ctapi.default
        return lane.stopped["chat"] if self.chat_wanted() and lane is not None else None

    def chat_sender(self):
        """ctapi.send_chat while chat is on and Connecteam hasn't refused it; else None (the
        portal inbox only, and queued messages wait)."""
        if not self.chat_wanted() or ctapi.default is None or self.chat_problem():
            return None
        return ctapi.send_chat

    def google(self):
        """The Google Drive bridge for HR files (hr/gdrive.py), or None while HR isn't running or
        the owner hasn't set it up (Settings → Connections → Google Drive (HR files)). Its
        drives: gdrive.drive_ids(main_settings()), the HR drive first."""
        return gdrive.bridge_from_settings(self.main_settings()) if self.running else None

    def refresh_chat(self) -> None:
        notify.chat = self.chat_sender() if self.running else None

    # ------------------------------------------------------------ start and stop

    def start(self, background: bool = True) -> str:
        """Open the HR portal (making its key the first time). Returns status(); "locked" or
        "lost" when the key isn't here. background: start the background work too (tests run it
        by hand with tick()). Needs a running event loop."""
        if self.running:
            return "on"
        if self.keyring.load(create=True) != "ready":
            return self.status()
        crypto.use(self.keyring)
        self.db = hrdb.current = hrdb.HRDatabase(self.folder)
        for hook in self.on_start:
            hook(self)
        notify.url = self.url
        self.refresh_chat()
        self.sync_state.update(tried=0.0, soon=False, retry_at=None)
        if background:
            self._task = asyncio.create_task(self.run())
        return "on"

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - stopping regardless
                pass
            self._task = None
        for hook in self.on_stop:
            try:
                await hook(self)
            except Exception as err:  # noqa: BLE001 - stopping regardless
                log.warning("HR portal: stopping a part failed: %r", err)
        if self.db is not None:
            self.db.close()
        self.db = hrdb.current = None
        notify.chat = None
        crypto.use(None)
        self.keyring.forget()
        self.throttle.clear()
        people.kiosk_seen.clear()

    # ------------------------------------------------------------ background work

    async def run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - the loop must keep running
                log.warning("HR portal: background work failed: %r", err)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=TICK)
            except asyncio.TimeoutError:
                pass

    async def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if not self.running:
            return
        self.refresh_chat()
        local = datetime.fromtimestamp(now, notify._zone(self.db))
        day = local.date().isoformat()
        if self.daily_state["day"] is None:
            self.daily_state["day"] = self.db.setting("daily_day") or None
        if self.daily_state["day"] != day and (local.hour, local.minute) >= DAILY_AT:
            self.daily_state["day"] = day
            self.db.set_settings({"daily_day": day})
            await self.nightly()
        if self.sync_due(now):
            await self.sync_people(max_age=SYNC_SOON_GAP if self.sync_state["soon"] else SYNC_EVERY)
        if now - self.daily_state["hourly"] >= 3600:
            self.daily_state["hourly"] = now
            signin.prune(self.db, now)
        summary_at = notify._minutes(self.db.setting("summary_time"), 8 * 60)
        if (self.db.setting("summary_day") != day and local.hour * 60 + local.minute >= summary_at):
            self.db.set_settings({"summary_day": day})
            notify.summaries(self.db, now)
        await notify.send_due(self.db, now)
        for job in self.tick_jobs:
            try:
                await job(self, now)
            except Exception as err:  # noqa: BLE001 - one part mustn't stop the others
                log.warning("HR portal: background work of a part failed: %r", err)

    def sync_due(self, now: float) -> bool:
        state = self.sync_state
        gap = SYNC_SOON_GAP if state["soon"] else SYNC_EVERY
        return self.main_settings().get("connecteam_enabled") == "1" and (
            now - state["tried"] >= gap or (state["retry_at"] is not None and now >= state["retry_at"]))

    def sync_soon(self) -> None:
        """Someone unknown is signing in: they may have just been added in Connecteam."""
        today = time.strftime("%Y-%m-%d")
        state = self.sync_state
        if state["soon_day"] != today:
            state.update(soon_day=today, soon_count=0)
        if state["soon"] or state["soon_count"] >= SOON_A_DAY:
            return   # one is already waiting (it runs 5 minutes after the last), or enough today
        state["soon_count"] += 1
        state["soon"] = True
        self._wake.set()

    async def sync_people(self, fresh: bool = False, max_age: float = SYNC_EVERY) -> dict | None:
        """People from Connecteam: its cached list when it's at most max_age old (a minute, when
        fresh). Never raises: problems are kept in sync_state for the dashboard and HR."""
        self.sync_state.update(tried=time.time(), soon=False, retry_at=None)
        if self.main_settings().get("connecteam_enabled") != "1":
            self.sync_state["error"] = NO_CONNECTEAM
            return None
        if not self.db.setting("ct_fields"):
            try:
                await self.read_fields()
            except ConnecteamError as err:   # the start date then comes from the field HR picks
                log.info("HR portal: couldn't read Connecteam's profile fields: %s", err)
        try:
            # Department names (smart groups): reused for 6 hours, so a few requests a day.
            await self.connecteam.smart_groups(hr=True)
        except ConnecteamError as err:
            log.info("HR portal: couldn't read Connecteam's smart groups: %s", err)
        try:
            users = await self.connecteam.users(max_age=60 if fresh else max_age, hr=True)
            if not self.running:
                return None
            names = self.group_names()
            result = await people.apply_users(self.db, users, group_name=lambda g: names.get(g, g))
        except ConnecteamError as err:
            self.sync_state["error"] = str(err)
            if getattr(err, "budget", False):
                # HR's lane was full (say, just after testing the HR connection): soon, not in an
                # hour, so turning HR on still brings its people in straight away.
                paused = getattr(self.connecteam, "hr_paused_until", 0.0)
                self.sync_state["retry_at"] = max(time.time() + BUSY_RETRY, paused + 1)
            return None
        except Exception as err:  # noqa: BLE001 - a bad reply mustn't stop the background work
            log.warning("HR portal: reading people from Connecteam failed: %r", err)
            self.sync_state["error"] = "Something went wrong reading people from Connecteam."
            return None
        self.sync_state.update(at=time.time(), error=None, result=result)
        return result

    async def read_fields(self) -> None:
        """Connecteam's profile field names (one request, through HR's lane)."""
        fields = await self.connecteam.profile_fields()
        if self.running:
            self.db.set_settings({"ct_fields": json.dumps(fields)})

    async def nightly(self) -> None:
        if self.main_settings().get("connecteam_enabled") == "1":
            try:
                await self.read_fields()
                archived = await self.connecteam.archived_users()
                if self.running:
                    people.mark_archived(self.db, archived)
            except Exception as err:  # noqa: BLE001 - the nightly jobs below still run
                log.info("HR portal: nightly Connecteam check: %s", err)
        for job in self.daily_jobs:
            try:
                await job(self)
            except Exception as err:  # noqa: BLE001 - one job mustn't stop the others
                log.warning("HR portal: a nightly job failed: %r", err)

    # ------------------------------------------------------------ actions (portal and dashboard)

    def person_name(self, person_id) -> str:
        p = self.db.person(person_id) if self.db else None
        return p["name"] if p else str(person_id)

    def reset_signin(self, person_id, actor_id, actor: str, ip: str | None = None) -> None:
        """Clear the authenticator, recovery codes and locks, and sign them out everywhere. They
        sign in again with their kiosk code, and set up a new authenticator with a setup code."""
        signin.disable_totp(self.db, person_id)
        signin.unlock(self.db, person_id)
        signin.end_sessions(self.db, person_id)
        self.db.conn.execute("DELETE FROM setup_tokens WHERE person_id = ?", (str(person_id),))
        self.db.conn.commit()
        audit.record(self.db, "Reset HR sign-in", actor_id=actor_id, actor=actor,
                     target_id=person_id, target=self.person_name(person_id), ip=ip, security=True)
        notify.notify(person_id, "signin_reset", urgent=True, db=self.db)

    def unlock_person(self, person_id, actor_id, actor: str, ip: str | None = None) -> None:
        signin.unlock(self.db, person_id)
        audit.record(self.db, "Unlocked HR sign-in", actor_id=actor_id, actor=actor,
                     target_id=person_id, target=self.person_name(person_id), ip=ip, security=True)

    def end_access(self, person_id, actor_id, actor: str, ip: str | None = None) -> None:
        self.db.update_person(person_id, access_ended=1)
        signin.end_sessions(self.db, person_id)
        audit.record(self.db, "Ended HR access now", actor_id=actor_id, actor=actor,
                     target_id=person_id, target=self.person_name(person_id), ip=ip, security=True)

    def allow_access(self, person_id, actor_id, actor: str, ip: str | None = None) -> None:
        self.db.update_person(person_id, access_ended=0)
        signin.unlock(self.db, person_id)
        audit.record(self.db, "Allowed HR sign-in again", actor_id=actor_id, actor=actor,
                     target_id=person_id, target=self.person_name(person_id), ip=ip, security=True)

    def sign_out_everyone(self, actor_id, actor: str) -> int:
        count = signin.end_sessions(self.db)
        audit.record(self.db, "Signed everyone out of HR", actor_id=actor_id, actor=actor,
                     detail=f"{count} session(s) ended", security=True)
        return count

    def set_admins(self, person_ids: list[str], actor: str) -> tuple[list[str], list[str]]:
        """The owner's choice of HR admins (dashboard). Returns (added, removed)."""
        now = set(self.db.with_role(access.HR_ADMIN))
        wanted = {str(p) for p in person_ids}
        for pid in wanted - now:
            self.db.set_role(pid, access.HR_ADMIN, True, by="owner")
            audit.record(self.db, "Made HR admin", actor_id="owner", actor=actor, target_id=pid,
                         target=self.person_name(pid), security=True)
        for pid in now - wanted:
            self.db.set_role(pid, access.HR_ADMIN, False)
            audit.record(self.db, "Removed HR admin", actor_id="owner", actor=actor, target_id=pid,
                         target=self.person_name(pid), security=True)
        return sorted(wanted - now), sorted(now - wanted)

    async def unlock_key(self, code: str) -> None:
        """The owner typed the recovery key (dashboard): the key comes back and HR starts."""
        crypto.clean_recovery_key(code)
        await asyncio.to_thread(self.keyring.unlock, code)
        if self.enabled():
            self.start()
            audit.record(self.db, "Unlocked HR with the recovery key", actor_id="owner",
                         actor="Owner", security=True)

    async def keep_key_out(self, on: bool, actor_id, actor: str) -> str | None:
        code = await asyncio.to_thread(self.keyring.keep_out, on)
        audit.record(self.db, "Kept the HR key out of backups" if on else
                     "Put the HR key back in backups", actor_id=actor_id, actor=actor, security=True)
        return code

    # ------------------------------------------------------------ migration files

    def migration_blocker(self) -> str | None:
        """Why a migration file can't be made now (uploads still waiting; each part's reason),
        else None."""
        return " ".join(r for check in self.migration_blockers if (r := check(self))) or None

    def export(self) -> dict | None:
        """What a migration file carries of HR (None when HR was never turned on): its database,
        and its key (or, kept out of backups, the key locked with the recovery key). Read on a
        connection of its own, so this runs in a worker thread."""
        path = os.path.join(self.folder, hrdb.DB_FILE)
        if not os.path.exists(path):
            return None
        ring = crypto.Keyring(self.folder)   # its own copy: this runs in a worker thread
        if ring.load() == "lost":
            raise ValueError(crypto.LOST)
        conn = sqlite3.connect(f"file:{urllib.parse.quote(path)}?mode=ro", uri=True,
                               timeout=hrdb.BUSY_SECONDS)
        try:
            data = conn.serialize()
        finally:
            conn.close()
        # Nothing from Staff Access's settings here: they travel with its other secrets, and
        # its database connection belongs to the event loop.
        return {"db": base64.b64encode(data).decode(), **ring.export()}

    def take_migration(self, carried: dict) -> None:
        """Put a migration file's HR in place, on the new computer, while HR is stopped. Anything
        HR already had here is moved aside (never deleted). Raises ValueError."""
        if self.running:
            raise ValueError("Turn the HR portal off before restoring.")
        raw = base64.b64decode(str(carried.get("db") or ""), validate=True)
        if not raw.startswith(b"SQLite format 3\x00") or len(raw) > MAX_MIGRATED_DB:
            raise ValueError("The HR part of that migration file is damaged.")
        if os.path.exists(self.folder):
            shutil.move(self.folder, f"{self.folder}-replaced-{time.strftime('%Y%m%d-%H%M%S')}")
        os.makedirs(self.folder)
        crypto.write_private(os.path.join(self.folder, hrdb.DB_FILE), raw)
        self.keyring = crypto.Keyring(self.folder)
        self.keyring.take(carried)
