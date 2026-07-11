from __future__ import annotations

from typing import Any

import httpx
from bff_runtime_contract import BFF_SERVICE_TOKEN_HEADER, BFF_SERVICE_TOKEN_NAME
from paths import ba_home

RUNTIME_PREFERENCE_KEYS = frozenset({
    "send_mode",
    "shortcut_responses",
    "cross_session_delegate_auto",
    "context_strategy",
    "session_auto_delete_days",
    "network_bind_address",
    "folder_view_enabled",
    "session_sort",
    "session_status_sort",
    "sessions_tabs_sort",
    "sessions_tabs_visible",
    "auto_restart_on_idle",
})


class RuntimeServiceError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class BffRuntimeService:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._service_token = ""

    def bind(self, client: httpx.AsyncClient, service_token: str) -> None:
        if not service_token:
            raise RuntimeServiceError(503, "BFF service token unavailable")
        self._client = client
        self._service_token = service_token

    def unbind(self) -> None:
        self._client = None
        self._service_token = ""

    async def _preferences_request(
        self,
        method: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._client
        if client is None:
            raise RuntimeServiceError(503, "runtime unavailable")
        try:
            response = await client.request(
                method,
                "/api/bff-runtime/preferences",
                headers={BFF_SERVICE_TOKEN_HEADER: self._service_token},
                json=body,
                timeout=5.0,
            )
        except httpx.HTTPError as exc:
            raise RuntimeServiceError(503, "runtime unavailable") from exc
        if response.status_code != 200:
            try:
                detail = str(response.json().get("detail") or "runtime preference request failed")
            except (ValueError, AttributeError):
                detail = "runtime preference request failed"
            raise RuntimeServiceError(response.status_code, detail)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeServiceError(502, "runtime returned invalid preferences") from exc
        if not isinstance(payload, dict):
            raise RuntimeServiceError(502, "runtime returned invalid preferences")
        return payload

    async def get_preferences(self) -> dict[str, Any]:
        return await self._preferences_request("GET")

    async def patch_preferences(
        self,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._preferences_request("PATCH", body)


def read_service_token() -> str:
    try:
        token = (ba_home() / "runtime" / BFF_SERVICE_TOKEN_NAME).read_text(
            encoding="utf-8"
        ).strip()
    except OSError as exc:
        raise RuntimeServiceError(503, "BFF service token unavailable") from exc
    if not token:
        raise RuntimeServiceError(503, "BFF service token unavailable")
    return token


runtime_service = BffRuntimeService()
