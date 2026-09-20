"""Thin client for the Home Assistant Core API, reached through the Supervisor
proxy (http://supervisor/core/api for REST, ws://supervisor/core/websocket for
WebSocket), authenticated with the SUPERVISOR_TOKEN env var every add-on gets
when it declares `homeassistant_api: true` in config.yaml.

Only the handful of calls this add-on needs: setting sensor states, firing a
persistent notification, and importing external long-term statistics (the
`recorder/import_statistics` WebSocket command - there is no REST equivalent).
"""

from __future__ import annotations

import itertools
import logging
import os
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

_REST_BASE = "http://supervisor/core/api"
_WS_URL = "ws://supervisor/core/websocket"

STATUS_ENTITY_ID = "sensor.civii_ovir_status"
# Kept as a small, fixed enum (not free text) so it's usable in automations
# and conditions. Human-readable detail goes in the "detail" attribute.
STATUS_OPTIONS = ["idle", "backfilling", "fetching", "waiting_captcha", "error"]


class HAClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._token = os.environ["SUPERVISOR_TOKEN"]
        self._headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def set_state(
        self, entity_id: str, state: Any, attributes: dict | None = None
    ) -> None:
        url = f"{_REST_BASE}/states/{entity_id}"
        payload = {"state": state, "attributes": attributes or {}}
        async with self._session.post(url, headers=self._headers, json=payload) as resp:
            if resp.status >= 300:
                text = await resp.text()
                _LOGGER.warning("set_state(%s) failed: %s %s", entity_id, resp.status, text)

    async def set_status(self, status: str, detail: str = "", **extra_attrs: Any) -> None:
        """Push the process-state sensor. `status` must be one of STATUS_OPTIONS."""
        if status not in STATUS_OPTIONS:
            raise ValueError(f"Unknown status {status!r}, must be one of {STATUS_OPTIONS}")
        await self.set_state(
            STATUS_ENTITY_ID,
            status,
            {
                "device_class": "enum",
                "options": STATUS_OPTIONS,
                "detail": detail,
                "friendly_name": "Estado (Canal de Isabel II)",
                **extra_attrs,
            },
        )

    async def notify(self, message: str, title: str = "Canal de Isabel II") -> None:
        url = f"{_REST_BASE}/services/persistent_notification/create"
        payload = {"title": title, "message": message, "notification_id": "civii_ovir"}
        async with self._session.post(url, headers=self._headers, json=payload) as resp:
            if resp.status >= 300:
                text = await resp.text()
                _LOGGER.warning("notify() failed: %s %s", resp.status, text)

    async def dismiss_notification(self) -> None:
        url = f"{_REST_BASE}/services/persistent_notification/dismiss"
        payload = {"notification_id": "civii_ovir"}
        async with self._session.post(url, headers=self._headers, json=payload):
            pass

    async def import_statistics(self, metadata: dict, stats: list[dict]) -> None:
        """Import external long-term statistics via the admin-only
        `recorder/import_statistics` WebSocket command. One-shot connection
        per call - this only runs once per fetch cycle, no need to keep a
        persistent socket open."""
        id_counter = itertools.count(1)
        async with self._session.ws_connect(_WS_URL) as ws:
            hello = await ws.receive_json()
            if hello.get("type") != "auth_required":
                raise RuntimeError(f"Unexpected WS handshake: {hello}")

            await ws.send_json({"type": "auth", "access_token": self._token})
            auth_result = await ws.receive_json()
            if auth_result.get("type") != "auth_ok":
                raise RuntimeError(f"WS auth failed: {auth_result}")

            msg_id = next(id_counter)
            await ws.send_json(
                {
                    "id": msg_id,
                    "type": "recorder/import_statistics",
                    "metadata": metadata,
                    "stats": stats,
                }
            )
            result = await ws.receive_json()
            if not result.get("success"):
                raise RuntimeError(f"import_statistics failed: {result}")
