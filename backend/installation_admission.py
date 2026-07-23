from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import installation_profile

ASGIApp = Callable[[dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]], Awaitable[None]]

_BOOTSTRAP_EXACT = frozenset({
    "/healthz",
    "/readyz",
    "/api/build-info",
    "/api/installation-profile",
    "/api/provider-setup/installs",
    "/api/provider-setup/status",
})
_BOOTSTRAP_PREFIXES = (
    "/api/auth",
    "/api/download/desktop",
    "/api/desktop",
    "/api/admin/restart",
    "/api/logs/frontend",
    "/api/startup_tasks",
)
_MOBILE_PREFIXES = (
    "/api/mobile",
    "/api/download/android",
    "/api/download/ios",
)
_INTEGRATION_PREFIXES = (
    "/api/extensions",
    "/api/provider-config-sync",
    "/api/internal/capabilities",
    "/api/internal/coordination",
    "/api/internal/extension-",
    "/api/internal/marketplace",
    "/api/internal/runtime-operations",
    "/api/internal/session-control",
    "/api/internal/workers",
)
_PROVIDER_INTERNAL_EXACT = frozenset({
    "/api/internal/credential/execute",
    "/api/internal/credential/request",
})


def capability_for_scope(scope: dict[str, Any]) -> str | None:
    scope_type = scope.get("type")
    if scope_type == "lifespan":
        return None
    if scope_type not in ("http", "websocket"):
        return installation_profile.BOOTSTRAP
    path = str(scope.get("path") or "")
    if not path.startswith("/api/") and path != "/ws/chat":
        return installation_profile.BOOTSTRAP
    if path in _BOOTSTRAP_EXACT or path.startswith(_BOOTSTRAP_PREFIXES):
        return installation_profile.BOOTSTRAP
    if path.startswith(_MOBILE_PREFIXES):
        return installation_profile.MOBILE
    if path in _PROVIDER_INTERNAL_EXACT:
        return installation_profile.PROVIDER_CONVERSATIONS
    if (
        path.startswith(_INTEGRATION_PREFIXES)
        or path.startswith("/api/internal/")
    ):
        return installation_profile.INTEGRATIONS
    return installation_profile.PROVIDER_CONVERSATIONS


class InstallationAdmissionMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
    ) -> None:
        capability = capability_for_scope(scope)
        if capability is None or installation_profile.allows(capability):
            await self.app(scope, receive, send)
            return
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": "installation capability unavailable"})
            return
        status = 404 if capability in (installation_profile.MOBILE, installation_profile.INTEGRATIONS) else 503
        body = json.dumps({
            "detail": (
                "installation setup is required"
                if status == 503
                else "installation capability is unavailable"
            )
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
