"""Minimal Home Assistant REST client.

Inside the add-on it talks to Core through the Supervisor proxy using SUPERVISOR_TOKEN.
Outside an add-on (development, plain Docker) set HA_URL and HA_TOKEN instead.

Phones can also be linked to a Home Assistant user. Those phones' commands are sent straight to
Core with that user's own token, so Home Assistant's logbook credits the user by name.
"""

import ast
import json
import logging
import os
import time

import aiohttp

log = logging.getLogger("staff_portal")

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
CLIENT_ID = "https://github.com/xDayan907/ha-staff-portal"
REDIRECT_URI = CLIENT_ID + "/"


class HomeAssistantError(Exception):
    pass


class LinkError(HomeAssistantError):
    """A linked user's sign-in failed or no longer works."""


class HomeAssistant:
    def __init__(self):
        if os.environ.get("SUPERVISOR_TOKEN"):
            self.base = "http://supervisor/core/api"
            token = os.environ["SUPERVISOR_TOKEN"]
            # Core is reachable directly on the add-on network. Tried with https if it has SSL on.
            self.core_urls = [os.environ.get("HA_CORE_URL", "http://homeassistant:8123")]
            if not os.environ.get("HA_CORE_URL"):
                self.core_urls.append("https://homeassistant:8123")
        else:
            root = os.environ.get("HA_URL", "http://localhost:8123").rstrip("/")
            self.base = root + "/api"
            token = os.environ.get("HA_TOKEN", "")
            self.core_urls = [root]
        self.headers = {"Authorization": f"Bearer {token}"}
        self.session: aiohttp.ClientSession | None = None
        self._states: dict[str, dict] = {}
        self._states_at = 0.0
        self._entity_area: dict[str, str] = {}  # entity_id -> Home Assistant area ID
        self._area_names: dict[str, str] = {}   # Home Assistant area ID -> name
        self._areas_at = 0.0
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
        except aiohttp.ClientError as err:
            raise HomeAssistantError(str(err)) from err

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
            self._areas_at = time.monotonic()
        return self._entity_area, self._area_names

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
        self._states_at = 0.0  # force a fresh read so the phone sees the result

    async def logbook(self, name: str, message: str, entity_id: str | None = None) -> None:
        data = {"name": name, "message": message}
        if entity_id:
            data["entity_id"] = entity_id
        await self._request("POST", "/services/logbook/log", json=data)

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
            except aiohttp.ClientError as err:
                last_error = err
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
        try:
            token = await self._core_request("POST", "/auth/token", data={
                "grant_type": "refresh_token", "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            })
        except HomeAssistantError as err:
            if str(err).startswith(("400", "401", "403")):
                raise LinkError("The linked Home Assistant user's sign-in no longer works.") \
                    from err
            raise
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
