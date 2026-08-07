"""Hermetic unit owner for runtime_principal.

``RuntimePrincipal`` is the frozen security identity carried by every runtime
operation: it binds a ``PrincipalKind`` to a permitted-operation/resource scope,
a grant/availability generation pair, and an issuance/expiry lifetime, then
derives two canonical digests and round-trips through claims/reference dicts.
This owner exhausts every line and branch deterministically with no live
backend or model:

- ``PrincipalKind`` enum surface,
- ``__post_init__``: the happy path plus all three ``ValueError`` guards
  (missing identity/issuer/audience, expiry not following issuance, empty
  permitted operations),
- ``allows``: permitted+unexpired True, unpermitted False, expired False,
- ``scope_digest``: determinism + context_complete sensitivity,
- ``idempotency_scope_digest``: excludes availability generation, timing, and
  context_complete (equal where scope_digest differs),
- ``claims`` shape (permitted_* as lists) and ``reference`` volatile-field
  stripping,
- ``from_claims`` round-trip + optional-field defaults + falsy
  ``context_complete``,
- ``from_reference`` reissue with a fresh lifetime,
- ``compatibility_extension_principal`` factory shape.

conftest engages an isolated per-module ba_home().
"""
from __future__ import annotations

import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-principal-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import runtime_principal as rp  # noqa: E402
from runtime_principal import (  # noqa: E402
    PrincipalKind,
    RuntimePrincipal,
    compatibility_extension_principal,
)

_ISSUED = 1.0
# Far-future expiry (real epoch seconds) so allows() is True on the unexpired path.
_FAR_FUTURE = 10_000_000_000.0


def _principal(**overrides) -> RuntimePrincipal:
    base = dict(
        kind=PrincipalKind.AGENT_RUN,
        principal_id="agent-1",
        issuer="issuer-a",
        audience="audience-b",
        permitted_operations=("op.read", "op.write"),
        permitted_resources=("res-1",),
        grant_generation="g1",
        availability_generation="a1",
        issued_at=_ISSUED,
        expires_at=_FAR_FUTURE,
    )
    base.update(overrides)
    return RuntimePrincipal(**base)


def test_principal_kind_enum_values() -> None:
    assert PrincipalKind.AGENT_RUN.value == "agent_run"
    assert PrincipalKind.EXTENSION_SERVER.value == "extension_server"
    assert PrincipalKind.NODE_RELAY.value == "node_relay"
    assert {kind.value for kind in PrincipalKind} == {
        "agent_run",
        "extension_server",
        "node_relay",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("principal_id", ""),
        ("issuer", ""),
        ("audience", ""),
    ],
)
def test_post_init_requires_identity_fields(field, value) -> None:
    with pytest.raises(ValueError):
        _principal(**{field: value})


def test_post_init_expiry_must_follow_issuance() -> None:
    # equal -> rejected
    with pytest.raises(ValueError):
        _principal(issued_at=_ISSUED, expires_at=_ISSUED)
    # before -> rejected
    with pytest.raises(ValueError):
        _principal(issued_at=_ISSUED, expires_at=_ISSUED - 1.0)


def test_post_init_requires_at_least_one_operation() -> None:
    with pytest.raises(ValueError):
        _principal(permitted_operations=())


def test_post_init_accepts_valid_principal() -> None:
    principal = _principal()
    assert principal.context_complete is True
    assert principal.app_session_id == ""


def test_allows_true_when_permitted_and_unexpired() -> None:
    principal = _principal()
    assert principal.allows("op.read") is True


def test_allows_false_when_operation_not_permitted() -> None:
    principal = _principal()
    assert principal.allows("op.delete") is False


def test_allows_false_when_expired() -> None:
    principal = _principal(issued_at=1.0, expires_at=2.0)
    # operation is permitted but the principal is long expired
    assert principal.allows("op.read") is False


def test_scope_digest_is_deterministic_sha256_hex() -> None:
    principal = _principal()
    digest = principal.scope_digest()
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
    assert _principal().scope_digest() == digest


def test_scope_digest_is_sensitive_to_context_complete() -> None:
    complete = _principal(context_complete=True)
    incomplete = _principal(context_complete=False)
    assert complete.scope_digest() != incomplete.scope_digest()


def test_idempotency_scope_digest_excludes_volatile_fields() -> None:
    base = _principal()
    # differs in availability generation, lifetime, and context_complete
    variant = _principal(
        availability_generation="a2",
        issued_at=_ISSUED + 500.0,
        expires_at=_ISSUED + 560.0,
        context_complete=False,
    )
    assert base.scope_digest() != variant.scope_digest()
    assert base.idempotency_scope_digest() == variant.idempotency_scope_digest()


def test_claims_returns_lists_and_all_fields() -> None:
    claims = _principal().claims()
    assert claims["kind"] == "agent_run"
    assert claims["permitted_operations"] == ["op.read", "op.write"]
    assert claims["permitted_resources"] == ["res-1"]
    assert claims["issued_at"] == _ISSUED
    assert claims["expires_at"] == _FAR_FUTURE
    assert claims["context_complete"] is True


def test_reference_strips_volatile_fields() -> None:
    reference = _principal().reference()
    assert "issued_at" not in reference
    assert "expires_at" not in reference
    assert "availability_generation" not in reference
    assert reference["grant_generation"] == "g1"
    assert reference["permitted_operations"] == ["op.read", "op.write"]


def test_from_claims_roundtrips_principal() -> None:
    principal = _principal(provider_id="claude", run_id="r1")
    rebuilt = RuntimePrincipal.from_claims(principal.claims())
    assert rebuilt.scope_digest() == principal.scope_digest()
    assert rebuilt.provider_id == "claude"
    assert rebuilt.run_id == "r1"
    assert rebuilt.permitted_operations == principal.permitted_operations


def test_from_claims_optional_fields_default_empty() -> None:
    claims = _principal().claims()
    for key in (
        "app_session_id",
        "run_id",
        "provider_id",
        "node_id",
        "cwd",
        "server_id",
    ):
        del claims[key]
    rebuilt = RuntimePrincipal.from_claims(claims)
    assert rebuilt.app_session_id == ""
    assert rebuilt.run_id == ""
    assert rebuilt.provider_id == ""
    assert rebuilt.node_id == ""
    assert rebuilt.cwd == ""
    assert rebuilt.server_id == ""


def test_from_claims_falsy_context_complete_becomes_false() -> None:
    claims = _principal().claims()
    claims["context_complete"] = 0
    rebuilt = RuntimePrincipal.from_claims(claims)
    assert rebuilt.context_complete is False


def test_from_reference_reissues_with_fresh_lifetime() -> None:
    principal = _principal()
    reference = principal.reference()
    reissued = RuntimePrincipal.from_reference(
        reference,
        availability_generation="a9",
        lifetime_seconds=120.0,
    )
    assert reissued.availability_generation == "a9"
    assert reissued.expires_at > reissued.issued_at
    assert reissued.expires_at - reissued.issued_at == pytest.approx(120.0)
    # scope is unchanged: same kind, identity, operations, resources, grant gen
    assert reissued.principal_id == principal.principal_id
    assert reissued.permitted_operations == principal.permitted_operations


def test_compatibility_extension_principal_shape() -> None:
    principal = compatibility_extension_principal(
        extension_id="ext-7",
        operation="op.read",
        grant_generation="g-ext",
    )
    assert principal.kind is PrincipalKind.EXTENSION_SERVER
    assert principal.principal_id == "ext-7"
    assert principal.issuer == "better-agent-capability-api"
    assert principal.audience == "better-agent-operation-runtime"
    assert principal.permitted_operations == ("op.read",)
    assert principal.permitted_resources == ()
    assert principal.grant_generation == "g-ext"
    assert principal.availability_generation == "g-ext"
    assert principal.server_id == "legacy-loopback"
    assert principal.context_complete is False
    assert principal.expires_at - principal.issued_at == pytest.approx(30.0)
    assert principal.allows("op.read") is True


def test_module_exposes_runtime_principal_symbols() -> None:
    assert rp.RuntimePrincipal is RuntimePrincipal
    assert rp.PrincipalKind is PrincipalKind
