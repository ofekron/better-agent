from __future__ import annotations

from typing import Any

import httpx

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

    def bind(self, client: httpx.AsyncClient) -> None:
        self._client = client

    def unbind(self) -> None:
        self._client = None

    async def _preferences_request(
        self,
        method: str,
        headers: list[tuple[bytes, bytes]],
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._client
        if client is None:
            raise RuntimeServiceError(503, "runtime unavailable")
        try:
            response = await client.request(
                method,
                "/api/bff-runtime/preferences",
                headers=headers,
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

    async def get_preferences(
        self,
        headers: list[tuple[bytes, bytes]],
    ) -> dict[str, Any]:
        return await self._preferences_request("GET", headers)

    async def patch_preferences(
        self,
        headers: list[tuple[bytes, bytes]],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._preferences_request("PATCH", headers, body)


runtime_service = BffRuntimeService()
