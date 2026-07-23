from __future__ import annotations

import contextvars
from contextlib import contextmanager
import hashlib
import time
from typing import Callable

from runtime_principal import PrincipalKind, RuntimePrincipal

_AUDIENCE = "better-agent-operation-runtime"
_VALIDATORS: dict[PrincipalKind, Callable[[RuntimePrincipal], bool]] = {}
_VERIFIED_SENTINEL = object()
_CURRENT_PRINCIPAL: contextvars.ContextVar[RuntimePrincipal | None] = contextvars.ContextVar(
    "operation_runtime_principal",
    default=None,
)


class VerifiedPrincipal:
    def __init__(
        self,
        principal: RuntimePrincipal,
        sentinel: object,
    ) -> None:
        if sentinel is not _VERIFIED_SENTINEL:
            raise PermissionError("verified principals are issued by operation_authority")
        self.principal = principal


def register_validator(
    kind: PrincipalKind,
    validator: Callable[[RuntimePrincipal], bool],
) -> Callable[[RuntimePrincipal], bool] | None:
    previous = _VALIDATORS.get(kind)
    _VALIDATORS[kind] = validator
    return previous


def restore_validator(
    kind: PrincipalKind,
    validator: Callable[[RuntimePrincipal], bool] | None,
) -> None:
    if validator is None:
        _VALIDATORS.pop(kind, None)
        return
    _VALIDATORS[kind] = validator


def issue(principal: RuntimePrincipal) -> VerifiedPrincipal:
    _validate_principal(principal)
    return VerifiedPrincipal(principal, _VERIFIED_SENTINEL)


def verify(principal: VerifiedPrincipal) -> VerifiedPrincipal:
    if not isinstance(principal, VerifiedPrincipal):
        raise PermissionError("runtime principal was not issued by operation authority")
    _validate_principal(principal.principal)
    return principal


def resolve(
    reference: dict[str, object],
    *,
    availability_generation: str,
) -> VerifiedPrincipal:
    return issue(
        RuntimePrincipal.from_reference(
            reference,
            availability_generation=availability_generation,
        )
    )


@contextmanager
def bind(principal: RuntimePrincipal):
    token = _CURRENT_PRINCIPAL.set(principal)
    try:
        yield
    finally:
        _CURRENT_PRINCIPAL.reset(token)


def current_principal() -> RuntimePrincipal:
    principal = _CURRENT_PRINCIPAL.get()
    if principal is None:
        raise PermissionError("operation runtime principal is not bound")
    return principal


def current_extension_generation(extension_id: str) -> str:
    import extension_store

    record = extension_store.get_extension(extension_id) or {}
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    value = (
        source.get("install_path")
        or source.get("version")
        or record.get("version")
        or ""
    )
    if not isinstance(value, str) or not value:
        return "unknown"
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _validate_principal(principal: RuntimePrincipal) -> None:
    if principal.audience != _AUDIENCE:
        raise PermissionError("principal audience is invalid")
    if principal.expires_at <= time.time():
        raise PermissionError("principal expired")
    validator = _VALIDATORS.get(principal.kind)
    if validator is None or not validator(principal):
        raise PermissionError("principal is no longer authorized")


def _validate_extension_server(principal: RuntimePrincipal) -> bool:
    import extension_store
    import operation_catalog

    record = extension_store.get_extension(principal.principal_id)
    if not record or not extension_store.is_extension_active(principal.principal_id):
        return False
    if current_extension_generation(principal.principal_id) != principal.grant_generation:
        return False
    grants = set(extension_store.declared_permissions(record).get("capabilities") or [])
    try:
        catalog = operation_catalog.current()
        required = {
            (
                catalog.descriptor(operation).capability
                + "."
                + catalog.descriptor(operation).action
            )
            for operation in principal.permitted_operations
        }
    except KeyError:
        return False
    return required <= grants


register_validator(PrincipalKind.EXTENSION_SERVER, _validate_extension_server)
