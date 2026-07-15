from __future__ import annotations

from dataclasses import dataclass
import hmac
import secrets
import threading
import time
from typing import Callable, Iterable


_DEFAULT_TTL_SECONDS = 5 * 60.0
_MIN_TTL_SECONDS = 5.0
_MAX_TTL_SECONDS = 15 * 60.0


@dataclass(frozen=True, slots=True)
class AmbientPrincipal:
    principal_id: str
    extension_id: str
    server_name: str
    permissions: frozenset[str]
    os_user_id: str
    provider_id: str
    cwd: str
    pid: int
    issued_at: float
    expires_at: float
    connection_bound: bool
    source_kind: str
    core_server: str

    def permits(self, permission: str) -> bool:
        return permission in self.permissions


@dataclass(slots=True)
class _Record:
    token: str
    principal: AmbientPrincipal


class AmbientPrincipalRegistry:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[str, _Record] = {}

    def issue(
        self,
        *,
        extension_id: str,
        server_name: str,
        permissions: Iterable[str],
        os_user_id: str,
        provider_id: str = "",
        cwd: str = "",
        pid: int = 0,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        connection_bound: bool = False,
        source_kind: str = "extension",
        core_server: str = "",
    ) -> tuple[str, AmbientPrincipal]:
        extension_id = extension_id.strip()
        server_name = server_name.strip()
        os_user_id = os_user_id.strip()
        source_kind = source_kind.strip()
        core_server = core_server.strip()
        if source_kind not in {"extension", "core"}:
            raise ValueError("ambient principal source_kind is invalid")
        if not extension_id or not server_name or not os_user_id:
            raise ValueError("extension_id, server_name, and os_user_id are required")
        if source_kind == "core" and not core_server:
            raise ValueError("core_server is required for core ambient principals")
        ttl = min(max(float(ttl_seconds), _MIN_TTL_SECONDS), _MAX_TTL_SECONDS)
        now = self._clock()
        principal = AmbientPrincipal(
            principal_id=secrets.token_urlsafe(24),
            extension_id=extension_id,
            server_name=server_name,
            permissions=frozenset(item.strip() for item in permissions if item.strip()),
            os_user_id=os_user_id,
            provider_id=provider_id.strip(),
            cwd=cwd.strip(),
            pid=max(0, int(pid)),
            issued_at=now,
            expires_at=now + ttl,
            connection_bound=connection_bound,
            source_kind=source_kind,
            core_server=core_server,
        )
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._expire_locked(now)
            self._records[principal.principal_id] = _Record(token=token, principal=principal)
        return token, principal

    def active_tokens(self) -> list[str]:
        """Snapshot of live credential tokens — signature-verification
        candidates for signed internal requests."""
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            return [record.token for record in self._records.values()]

    def resolve(self, token: str | None, *, permission: str = "") -> AmbientPrincipal | None:
        if not token:
            return None
        now = self._clock()
        with self._lock:
            self._expire_locked(now)
            for record in self._records.values():
                if not hmac.compare_digest(record.token, token):
                    continue
                if permission and not record.principal.permits(permission):
                    return None
                return record.principal
        return None

    def revoke(self, principal_id: str) -> AmbientPrincipal | None:
        with self._lock:
            record = self._records.pop(principal_id, None)
        return record.principal if record else None

    def revoke_extension(self, extension_id: str, *, server_name: str = "") -> list[AmbientPrincipal]:
        with self._lock:
            removed = [
                record.principal
                for record in self._records.values()
                if record.principal.extension_id == extension_id
                and (not server_name or record.principal.server_name == server_name)
            ]
            for principal in removed:
                self._records.pop(principal.principal_id, None)
        return removed

    def clear(self) -> list[AmbientPrincipal]:
        with self._lock:
            removed = [record.principal for record in self._records.values()]
            self._records.clear()
        return removed

    def _expire_locked(self, now: float) -> None:
        expired = [
            principal_id
            for principal_id, record in self._records.items()
            if not record.principal.connection_bound and record.principal.expires_at <= now
        ]
        for principal_id in expired:
            self._records.pop(principal_id, None)


registry = AmbientPrincipalRegistry()
