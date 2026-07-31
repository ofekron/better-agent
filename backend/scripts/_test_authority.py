"""Shared stub for the internal-token / runtime-readiness guard.

Tests that exercise internal-endpoint HANDLER logic (request validation,
delegation plumbing) rather than the guard itself wrap their call in this stub.
The guard has its own dedicated coverage (test_api_auth_surface and friends);
stubbing it here keeps handler tests focused and independent of
team-orchestration runtime state and bound-request principals.
"""
from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def stub_internal_authority():
    """Skip `internal_guards.require_role_internal` and force authority valid.

    `require_role_internal` composes the authority check and the owning
    extension's runtime-readiness check; `internal_ask_fork` additionally
    re-checks `authority_is_valid`. Stubbing both lets a direct handler call
    proceed without a bound principal or a runtime-ready team-orchestration
    extension. Restores originals on exit so the patch never leaks across
    test modules in a shared session.
    """
    import internal_guards

    original_role = internal_guards.require_role_internal
    original_valid = internal_guards.authority_is_valid
    internal_guards.require_role_internal = lambda *_args, **_kwargs: None
    internal_guards.authority_is_valid = lambda: True
    try:
        yield
    finally:
        internal_guards.require_role_internal = original_role
        internal_guards.authority_is_valid = original_valid
