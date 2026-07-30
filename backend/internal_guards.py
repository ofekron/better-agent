"""The authority checks that gate `/api/internal/*`.

These are security boundaries, so there is exactly one implementation of
each and every internal router shares it. Two distinct checks compose
here, and conflating them would widen access:

- **authority** — a principal was bound to this request by the auth
  gate. The `X-Internal-Token` header is declared on internal routes so
  a missing header is rejected before the handler runs, but the header
  value itself is not what grants authority.
- **runtime readiness** — the builtin extension that owns the route's
  role is installed and running. A 404, not a 403: the caller is
  allowed, the capability just isn't up.

Fails closed: an unconfigured module grants nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from fastapi import HTTPException

import extension_store
from i18n import t

_principal_provider: Optional[Callable[[], Any]] = None


def configure(principal_provider: Callable[[], Any]) -> None:
    """Bind the request-principal source (the coordinator)."""
    global _principal_provider
    _principal_provider = principal_provider


def authority_is_valid() -> bool:
    if _principal_provider is None:
        return False
    return _principal_provider() is not None


def internal_authority_extension_id() -> Optional[str]:
    """The calling extension's id, if the bound principal is an extension.
    None for the core/runner principal or when unauthenticated."""
    if _principal_provider is None:
        return None
    principal = _principal_provider()
    if principal is None or getattr(principal, "kind", None) != "extension":
        return None
    return principal.extension_id


def require_internal() -> None:
    if not authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))


def require_builtin_runtime_extension(extension_id: str) -> None:
    not_ready_msg = extension_store.runtime_not_ready_message(extension_id)
    if not_ready_msg is not None:
        raise HTTPException(status_code=404, detail=not_ready_msg)


def require_role_internal(role: str) -> None:
    """Authority first, then the owning extension's runtime readiness."""
    require_internal()
    require_builtin_runtime_extension(extension_store.extension_id_for_role(role))
