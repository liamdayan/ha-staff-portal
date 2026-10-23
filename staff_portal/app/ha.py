"""Minimal Home Assistant REST client.

Inside the add-on it talks to Core through the Supervisor proxy using SUPERVISOR_TOKEN.
Outside an add-on (development, plain Docker) set HA_URL and HA_TOKEN instead.
"""

import os
import time

import aiohttp

AREA_TEMPLATE = (
    "{%- for s in states -%}"
    "{{ s.entity_id }}\t{{ area_name(s.entity_id) or '' }}\n"
    "{%- endfor -%}"
)


class HomeAssistantError(Exception):
    pass


class HomeAssistant:
    def __init__(self):
        if os.environ.get("SUPERVISOR_TOKEN"):
            self.base = "http://supervisor/core/api"
            token = os.environ["SUPERVISOR_TOKEN"]
        else:
            self.base = os.environ.get("HA_URL", "http://localhost:8123").rstrip("/") + "/api"
            token = os.environ.get("HA_TOKEN", "")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.session: aiohttp.ClientSession | None = None
        self._states: dict[str, dict] = {}
        self._states_at = 0.0
        self._areas: dict[str, str] = {}
        self._areas_at = 0.0

    async def start(self):
        self.session = aiohttp.ClientSession(
            headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)
        )

    async def close(self):
        if self.session:
            await self.session.close()

    async def _request(self, method: str, path: str, **kwargs):
        try:
            async with self.session.request(method, self.base + path, **kwargs) as resp:
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

    async def areas(self, max_age: float = 300.0) -> dict[str, str]:
        if time.monotonic() - self._areas_at > max_age:
            try:
                text = await self._request("POST", "/template", json={"template": AREA_TEMPLATE})
            except HomeAssistantError:
                text = ""
            areas = {}
            for line in str(text).splitlines():
                entity_id, _, area = line.partition("\t")
                if entity_id:
                    areas[entity_id.strip()] = area.strip()
            self._areas = areas
            self._areas_at = time.monotonic()
        return self._areas

    async def call_service(self, domain: str, service: str, data: dict) -> None:
        await self._request("POST", f"/services/{domain}/{service}", json=data)
        self._states_at = 0.0  # force a fresh read so the phone sees the result
