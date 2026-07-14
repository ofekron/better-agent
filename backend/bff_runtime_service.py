from __future__ import annotations

from typing import Any, TYPE_CHECKING

import httpx
from bff_runtime_contract import BFF_SERVICE_TOKEN_HEADER, BFF_SERVICE_TOKEN_NAME
from paths import ba_home

if TYPE_CHECKING:
    from bff_runtime_upstream import RuntimeUpstream

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
        self._upstream: RuntimeUpstream | None = None

    def bind(self, upstream: RuntimeUpstream) -> None:
        self._upstream = upstream

    def unbind(self) -> None:
        self._upstream = None

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        upstream = self._upstream
        if upstream is None:
            raise RuntimeServiceError(503, "runtime unavailable")
        try:
            lease = await upstream.acquire()
        except (RuntimeError, OSError) as exc:
            raise RuntimeServiceError(503, "runtime unavailable") from exc
        try:
            response = await lease.client.request(
                method,
                path,
                headers={BFF_SERVICE_TOKEN_HEADER: lease.service_token},
                json=body,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise RuntimeServiceError(503, "runtime unavailable") from exc
        finally:
            await lease.release()
        if response.status_code != 200:
            try:
                detail = str(response.json().get("detail") or "runtime service request failed")
            except (ValueError, AttributeError):
                detail = "runtime service request failed"
            raise RuntimeServiceError(response.status_code, detail)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeServiceError(502, "runtime returned invalid service response") from exc
        if not isinstance(payload, dict):
            raise RuntimeServiceError(502, "runtime returned invalid service response")
        return payload

    async def get_preferences(self) -> dict[str, Any]:
        return await self._request("GET", "/api/bff-runtime/preferences")

    async def patch_preferences(
        self,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._request("PATCH", "/api/bff-runtime/preferences", body)

    async def project_facts(self) -> dict[str, Any]:
        return await self._request("GET", "/api/bff-runtime/projects/facts")

    async def project_status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/bff-runtime/projects/status")

    async def sync_project_catalog(self, projects: list[dict[str, Any]]) -> None:
        await self._request(
            "PUT",
            "/api/bff-runtime/projects/catalog",
            {"projects": projects},
        )

    async def create_session(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/bff-runtime/sessions", body, timeout=300.0
        )

    async def projection_source(
        self, session_id: str, *, after_seq: int = 0, limit: int = 2000,
    ) -> dict[str, Any]:
        if not session_id or any(not (ch.isalnum() or ch in "-_") for ch in session_id):
            raise ValueError("invalid session id")
        return await self._request(
            "GET",
            f"/api/bff-runtime/sessions/{session_id}/projection-source?after_seq={after_seq}&limit={limit}",
            timeout=30.0,
        )


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
