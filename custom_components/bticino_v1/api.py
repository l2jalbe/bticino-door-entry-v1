from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .auth import AuthHandler
from .const import API_BASE, APIM_SUBSCRIPTION_KEY

_LOGGER = logging.getLogger(__name__)

_CATALOG_V3 = f"{API_BASE}/servicecatalog/api/v3.0"
_DEVMGMT_V2 = f"{API_BASE}/devicemanagement/api/v2.0"

_MAX_RETRIES = 2
_RETRY_DELAY = 1.0


class ApiError(Exception):
    def __init__(self, status: int, body: Any = None) -> None:
        super().__init__(f"[{status}] {body}")
        self.status = status
        self.body = body


class LegrandApiClientV1:
    def __init__(
        self,
        auth: AuthHandler,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._auth = auth
        self._session = session
        self._owns_session = session is None
        self._client_session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None:
            return self._session
        if self._client_session is None or self._client_session.closed:
            self._client_session = aiohttp.ClientSession()
        return self._client_session

    async def _headers(self) -> dict[str, str]:
        token = await self._auth.get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Ocp-Apim-Subscription-Key": APIM_SUBSCRIPTION_KEY,
            "Content-Type": "application/json",
        }

    async def _get(self, url: str) -> Any:
        for attempt in range(_MAX_RETRIES + 1):
            headers = await self._headers()
            session = self._get_session()
            _LOGGER.debug("GET %s (attempt %d)", url, attempt + 1)
            async with session.get(url, headers=headers) as resp:
                if resp.status == 401:
                    _LOGGER.debug("GET %s -> 401, re-authenticating", url)
                    await self._auth.authenticate()
                    continue
                if resp.status >= 500:
                    if attempt < _MAX_RETRIES:
                        _LOGGER.debug("GET %s -> %s, retrying", url, resp.status)
                        await asyncio.sleep(_RETRY_DELAY * (2**attempt))
                        continue
                    body = await resp.json(content_type=None)
                    _LOGGER.debug("GET %s -> %s %s", url, resp.status, body)
                    raise ApiError(resp.status, body)
                if resp.status >= 400:
                    body = await resp.json(content_type=None)
                    _LOGGER.debug("GET %s -> %s %s", url, resp.status, body)
                    raise ApiError(resp.status, body)
                data = await resp.json(content_type=None)
                _LOGGER.debug("GET %s -> %s", url, resp.status)
                return data
        raise ApiError(401, "Max retries exceeded after reauth")

    async def _post(self, url: str, payload: dict) -> Any:
        for attempt in range(_MAX_RETRIES + 1):
            headers = await self._headers()
            session = self._get_session()
            _LOGGER.debug("POST %s payload=%s (attempt %d)", url, payload, attempt + 1)
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status == 401:
                    _LOGGER.debug("POST %s -> 401, re-authenticating", url)
                    await self._auth.authenticate()
                    continue
                if resp.status >= 500:
                    if attempt < _MAX_RETRIES:
                        _LOGGER.debug("POST %s -> %s, retrying", url, resp.status)
                        await asyncio.sleep(_RETRY_DELAY * (2**attempt))
                        continue
                    body = await resp.json(content_type=None)
                    _LOGGER.debug("POST %s -> %s %s", url, resp.status, body)
                    raise ApiError(resp.status, body)
                if resp.status >= 400:
                    body = await resp.json(content_type=None)
                    _LOGGER.debug("POST %s -> %s %s", url, resp.status, body)
                    raise ApiError(resp.status, body)
                if resp.status == 204:
                    _LOGGER.debug("POST %s -> 204 (no content)", url)
                    return {}
                data = await resp.json(content_type=None)
                _LOGGER.debug("POST %s -> %s %s", url, resp.status, data)
                return data
        raise ApiError(401, "Max retries exceeded after reauth")

    async def get_plants(self) -> list[dict]:
        data = await self._get(f"{_CATALOG_V3}/plants")
        return data if isinstance(data, list) else data.get("plants", data.get("value", []))

    async def get_modules(self, plant_id: str) -> list[dict]:
        data = await self._get(f"{_CATALOG_V3}/plants/{plant_id}/modules")
        return data if isinstance(data, list) else data.get("modules", data.get("value", []))

    async def open_lock(self, gateway_id: str, lock_module_id: str) -> dict:
        url = f"{_DEVMGMT_V2}/modules/{gateway_id}/commands"
        return await self._post(url, {"command": {"name": "open", "moduleId": lock_module_id}})

    async def set_light(self, gateway_id: str, light_module_id: str, on: bool) -> dict:
        url = f"{_DEVMGMT_V2}/modules/{gateway_id}/commands"
        name = "on" if on else "off"
        return await self._post(url, {"command": {"name": name, "moduleId": light_module_id}})

    async def close(self) -> None:
        if self._owns_session and self._client_session and not self._client_session.closed:
            await self._client_session.close()
