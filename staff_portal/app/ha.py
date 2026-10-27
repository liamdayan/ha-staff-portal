"""Minimal Home Assistant REST client.

Inside the add-on it talks to Core through the Supervisor proxy using SUPERVISOR_TOKEN.
Outside an add-on (development, plain Docker) set HA_URL and HA_TOKEN instead.

Phones can also be linked to a Home Assistant user. Those phones' commands are sent straight to
Core with that user's own token, so Home Assistant's logbook credits the user by name.
"""

import ast
import asyncio
import json
import logging
import os
import time

import aiohttp

log = logging.getLogger("staff_portal")

WS_MAX_MESSAGE = 64 * 1024 * 1024  # aiohttp's default (4 MB) is too small for big registries
ADMIN_REFRESH_GAP = 5      # seconds between lookups of Home Assistant's users
NOT_ADMIN_SECONDS = 10     # how long "this user isn't an admin" is remembered

# Rendered as JSON so Home Assistant's template whitespace handling can't change the layout.
# Lists line up by position: "areas"[i] is the area ID (or null) of "ids"[i].
AREA_TEMPLATE = (
    '{{ {"ids": states | map(attribute="entity_id") | list,'
    ' "areas": states | map(attribute="entity_id") | map("area_id") | list,'
    ' "area_ids": areas() | list,'
    ' "area_names": areas() | map("area_name") | list} | tojson }}'
)

# Identifies this add-on to Home Assistant's login system. It appears under the linked user's
# profile as the app their sign-in belongs to. It must stay the same, or refreshes stop working.
CLIENT_ID = "https://github.com/liamdayan/ha-staff-portal"
REDIRECT_URI = CLIENT_ID + "/"
# Earlier client IDs. Users linked under one of these can only refresh with that same ID.
LEGACY_CLIENT_IDS = ("https://github.com/xDayan907/ha-staff-portal",)


class HomeAssistantError(Exception):
    pass


class LinkError(HomeAssistantError):
    """A linked user's sign-in failed or no longer works."""


class HomeAssistant:
    def __init__(self):
        if os.environ.get("SUPERVISOR_TOKEN"):
            self.base = "http://supervisor/core/api"
            self.ws_url = "ws://supervisor/core/websocket"
            token = os.environ["SUPERVISOR_TOKEN"]
            # Core is reachable directly on the add-on network. Tried with https if it has SSL on.
            self.core_urls = [os.environ.get("HA_CORE_URL", "http://homeassistant:8123")]
            if not os.environ.get("HA_CORE_URL"):
                self.core_urls.append("https://homeassistant:8123")
        else:
            root = os.environ.get("HA_URL", "http://localhost:8123").rstrip("/")
            self.base = root + "/api"
            self.ws_url = root.replace("http", "ws", 1) + "/api/websocket"
            token = os.environ.get("HA_TOKEN", "")
            self.core_urls = [root]
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}"}
        self.session: aiohttp.ClientSession | None = None
        self._states: dict[str, dict] = {}
        self._states_at = float("-inf")  # never fetched; uptime can be under max_age
        self._entity_area: dict[str, str] = {}  # entity_id -> Home Assistant area ID
        self._area_names: dict[str, str] = {}   # Home Assistant area ID -> name
        self._areas_at = float("-inf")
        # Entities Home Assistant itself tucks away (config/diagnostic or hidden), such as
        # restart buttons, child locks and calibration numbers. Area grants skip these.
        self._tucked_away: set[str] = set()
        self._admins: set[str] = set()
        self._admins_at = float("-inf")
        self._not_admins: dict[str, float] = {}   # user ID -> when we last found it isn't one
        self._admin_lookup = asyncio.Lock()
        self._core: str | None = None
        self._user_tokens: dict[str, tuple[str, float]] = {}  # refresh token -> (access, expiry)

    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self):
        if self.session:
            await self.session.close()

    async def _request(self, method: str, path: str, **kwargs):
        try:
            async with self.session.request(
                method, self.base + path, headers=self.headers, **kwargs
            ) as resp:
                if resp.status >= 400:
                    raise HomeAssistantError(f"{resp.status}: {(await resp.text())[:200]}")
                if resp.content_type == "application/json":
                    return await resp.json()
                return await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            # Timeouts included: a slow Home Assistant is "can't reach it", never a crash.
            raise HomeAssistantError(str(err) or type(err).__name__) from err

    async def states(self, max_age: float = 2.0) -> dict[str, dict]:
        if time.monotonic() - self._states_at > max_age:
            data = await self._request("GET", "/states")
            self._states = {s["entity_id"]: s for s in data}
            self._states_at = time.monotonic()
        return self._states

    async def area_info(self, max_age: float = 300.0) -> tuple[dict[str, str], dict[str, str]]:
        """Return ({entity_id: HA area ID}, {HA area ID: name}). Empty if HA can't tell us."""
        if time.monotonic() - self._areas_at > max_age:
            try:
                raw = await self._request("POST", "/template", json={"template": AREA_TEMPLATE})
                data = parse_template_result(raw)
                self._entity_area = {
                    eid: aid for eid, aid in zip(data["ids"], data["areas"]) if eid and aid}
                self._area_names = {
                    aid: name or aid for aid, name in zip(data["area_ids"], data["area_names"])}
            except (HomeAssistantError, KeyError, TypeError, ValueError, SyntaxError) as err:
                log.warning("Couldn't read Home Assistant areas: %s", err)
            try:
                self._tucked_away = await self._read_tucked_away()
            except (HomeAssistantError, KeyError, TypeError) as err:
                log.warning("Couldn't read the entity registry: %s", err)
            self._areas_at = time.monotonic()
        return self._entity_area, self._area_names

    def tucked_away(self) -> set[str]:
        """Config/diagnostic/hidden entities (as of the last area refresh)."""
        return self._tucked_away

    # ------------------------------------------------------------ websocket API

    async def ws_command(self, payload: dict, timeout: float = 10.0):
        """Run one Home Assistant WebSocket command with the add-on's own token."""
        try:
            # Large systems send big replies (a full entity registry can be several MB).
            async with self.session.ws_connect(self.ws_url, timeout=timeout,
                                               max_msg_size=WS_MAX_MESSAGE) as ws:
                async def receive():
                    return await asyncio.wait_for(ws.receive_json(), timeout)
                if (await receive()).get("type") != "auth_required":
                    raise HomeAssistantError("unexpected websocket greeting")
                await ws.send_json({"type": "auth", "access_token": self.token})
                if (await receive()).get("type") != "auth_ok":
                    raise HomeAssistantError("websocket authentication failed")
                await ws.send_json({"id": 1, **payload})
                reply = await receive()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as err:
            raise HomeAssistantError(f"websocket: {err}") from err
        if not reply.get("success"):
            raise HomeAssistantError(f"websocket: {reply.get('error')}")
        return reply.get("result")

    async def _read_tucked_away(self) -> set[str]:
        """Entities Home Assistant marks as configuration/diagnostic or hidden, so whole-area
        grants can leave them out.

        Uses the compact listing Home Assistant's own frontend uses (a fraction of the size on
        big systems), and falls back to the full registry if that isn't available."""
        try:
            data = await self.ws_command({"type": "config/entity_registry/list_for_display"})
            entries = data["entities"]
            if not isinstance(entries, list):
                raise TypeError("unexpected entity list")
            # Compact keys: "ei" entity ID, "ec" category index (present only when set, and
            # 0 is a real category), "hb" true when hidden.
            return {e["ei"] for e in entries
                    if isinstance(e, dict) and isinstance(e.get("ei"), str) and ("ec" in e or e.get("hb"))}
        except (HomeAssistantError, KeyError, TypeError) as err:
            log.info("Compact entity registry not available (%s); reading the full one", err)
        registry = await self.ws_command({"type": "config/entity_registry/list"})
        return {e["entity_id"] for e in registry
                if isinstance(e, dict) and (e.get("entity_category") or e.get("hidden_by"))}

    async def is_admin(self, user_id: str, max_age: float = 60.0) -> bool:
        """Whether a Home Assistant user is an active administrator. Admins are cached for a
        minute (max_age: longer for the dashboard's poll). Anyone else triggers a fresh lookup (they may have just been made admin), but
        at most one lookup runs at a time, none within ADMIN_REFRESH_GAP of the last, and a "no"
        is remembered for NOT_ADMIN_SECONDS: requests from non-admins can't make the add-on
        query Home Assistant over and over."""
        now = time.monotonic()
        if user_id in self._admins and now - self._admins_at <= max_age:
            return True
        if now - self._not_admins.get(user_id, float("-inf")) < NOT_ADMIN_SECONDS:
            return False
        async with self._admin_lookup:
            if time.monotonic() - self._admins_at > ADMIN_REFRESH_GAP:
                users = await self.ws_command({"type": "config/auth/list"})
                self._admins = {
                    u["id"] for u in users
                    if isinstance(u, dict) and u.get("is_active") and not u.get("system_generated")
                    and (u.get("is_owner") or "system-admin" in (u.get("group_ids") or []))}
                self._admins_at = time.monotonic()
        if user_id in self._admins:
            self._not_admins.pop(user_id, None)
            return True
        if len(self._not_admins) > 1000:
            self._not_admins.clear()
        self._not_admins[user_id] = time.monotonic()
        return False

    async def areas(self, max_age: float = 300.0) -> dict[str, str]:
        """{entity_id: Home Assistant area name} for entities that have an area."""
        entity_area, names = await self.area_info(max_age)
        return {eid: names.get(aid, aid) for eid, aid in entity_area.items()}

    async def call_service(self, domain: str, service: str, data: dict,
                           refresh_token: str | None = None) -> None:
        if refresh_token:
            path = f"/api/services/{domain}/{service}"
            access = await self._user_access_token(refresh_token)
            try:
                await self._core_request("POST", path, json=data,
                                         headers={"Authorization": f"Bearer {access}"})
            except HomeAssistantError as err:
                if not str(err).startswith("401"):
                    raise
                # Cached sign-in was rejected (user removed, or HA restarted): refresh once.
                self._user_tokens.pop(refresh_token, None)
                access = await self._user_access_token(refresh_token)
                try:
                    await self._core_request("POST", path, json=data,
                                             headers={"Authorization": f"Bearer {access}"})
                except HomeAssistantError as err2:
                    if str(err2).startswith("401"):
                        raise LinkError("The linked Home Assistant user isn't allowed in.") \
                            from err2
                    raise
        else:
            await self._request("POST", f"/services/{domain}/{service}", json=data)
        self._states_at = float("-inf")  # force a fresh read so the phone sees the result

    async def logbook(self, name: str, message: str, entity_id: str | None = None) -> None:
        data = {"name": name, "message": message}
        if entity_id:
            data["entity_id"] = entity_id
        await self._request("POST", "/services/logbook/log", json=data)

    async def config(self) -> dict:
        """Home Assistant's general settings (Wristbands reads its time zone from these)."""
        if not self.session:
            raise HomeAssistantError("not started")
        data = await self._request("GET", "/config")
        if not isinstance(data, dict):
            raise HomeAssistantError("unexpected answer to /config")
        return data

    # ------------------------------------------------------------ linked users

    async def _core_request(self, method: str, path: str, **kwargs):
        """Request straight to Core (not through Supervisor), finding a working URL once."""
        urls = [self._core] if self._core else self.core_urls
        last_error = None
        for url in urls:
            try:
                async with self.session.request(
                    method, url + path, ssl=False if url.startswith("https") else True, **kwargs
                ) as resp:
                    self._core = url
                    body = await resp.json() if resp.content_type == "application/json" \
                        else {"message": (await resp.text())[:200]}
                    if resp.status >= 400:
                        raise HomeAssistantError(f"{resp.status}: {body}")
                    return body
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                last_error = err if str(err) else type(err).__name__
        raise HomeAssistantError(f"Can't reach Home Assistant directly: {last_error}")

    async def link_user(self, username: str, password: str) -> str:
        """Sign in as a Home Assistant user and return a refresh token. The password isn't kept."""
        flow = await self._core_request("POST", "/auth/login_flow", json={
            "client_id": CLIENT_ID,
            "handler": ["homeassistant", None],
            "redirect_uri": REDIRECT_URI,
        })
        if flow.get("type") != "form" or "flow_id" not in flow:
            raise LinkError("Home Assistant's username and password login isn't available.")
        step = await self._core_request("POST", f"/auth/login_flow/{flow['flow_id']}", json={
            "client_id": CLIENT_ID, "username": username, "password": password,
        })
        if step.get("type") == "form" and step.get("step_id") == "mfa":
            raise LinkError("This user has two-factor login turned on. Turn it off for this "
                            "user (their Security settings) or use a different user.")
        if step.get("type") != "create_entry":
            raise LinkError("Wrong username or password.")
        token = await self._core_request("POST", "/auth/token", data={
            "grant_type": "authorization_code", "code": step["result"], "client_id": CLIENT_ID,
        })
        refresh = token.get("refresh_token")
        if not refresh:
            raise LinkError("Home Assistant didn't return a sign-in token.")
        self._user_tokens[refresh] = (token["access_token"],
                                      time.monotonic() + token.get("expires_in", 1800) - 60)
        return refresh

    async def unlink_user(self, refresh_token: str) -> None:
        """Sign the add-on out of the linked user's account. Errors are ignored."""
        self._user_tokens.pop(refresh_token, None)
        try:
            await self._core_request("POST", "/auth/token",
                                     data={"token": refresh_token, "action": "revoke"})
        except HomeAssistantError:
            pass

    async def _user_access_token(self, refresh_token: str) -> str:
        cached = self._user_tokens.get(refresh_token)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        for client_id in (CLIENT_ID, *LEGACY_CLIENT_IDS):
            try:
                token = await self._core_request("POST", "/auth/token", data={
                    "grant_type": "refresh_token", "refresh_token": refresh_token,
                    "client_id": client_id,
                })
                break
            except HomeAssistantError as err:
                if not str(err).startswith(("400", "401", "403")):
                    raise
                last_error = err
        else:
            raise LinkError("The linked Home Assistant user's sign-in no longer works.") \
                from last_error
        self._user_tokens[refresh_token] = (token["access_token"],
                                            time.monotonic() + token.get("expires_in", 1800) - 60)
        return token["access_token"]


def parse_template_result(raw) -> dict:
    """Template results come back as text, or already parsed on some Home Assistant versions."""
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    try:
        return json.loads(text)
    except ValueError:
        return ast.literal_eval(text)  # a Python-style dict if HA re-printed the parsed result
