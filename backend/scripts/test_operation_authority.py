from __future__ import annotations

import hashlib
import os
import sys
import time

import pytest
from pydantic import BaseModel

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402

_TEST_HOME = _test_home.TestHome.acquire("bc-test-operation-authority-")

import extension_store  # noqa: E402
import operation_authority  # noqa: E402
import operation_catalog  # noqa: E402
from runtime_principal import PrincipalKind, RuntimePrincipal  # noqa: E402


AUDIENCE = operation_authority._AUDIENCE


def _principal(
    *,
    kind: PrincipalKind = PrincipalKind.AGENT_RUN,
    principal_id: str = "run-1",
    audience: str = AUDIENCE,
    expires_in: float = 60.0,
    permitted_operations: tuple[str, ...] = ("example_mutate",),
    grant_generation: str = "grant-1",
    issued_at: float | None = None,
    expires_at: float | None = None,
) -> RuntimePrincipal:
    now = time.time()
    return RuntimePrincipal(
        kind=kind,
        principal_id=principal_id,
        issuer="test",
        audience=audience,
        permitted_operations=permitted_operations,
        permitted_resources=("session:one",),
        grant_generation=grant_generation,
        availability_generation="one",
        issued_at=now if issued_at is None else issued_at,
        expires_at=(now + expires_in) if expires_at is None else expires_at,
        app_session_id="session-one",
        run_id=principal_id,
        provider_id="provider-one",
        node_id="primary",
        cwd="/tmp/project",
    )


def _expired_principal() -> RuntimePrincipal:
    # Constructed valid (expiry follows issuance) but already in the past.
    now = time.time()
    return _principal(issued_at=now - 120, expires_at=now - 60)


class _Request(BaseModel):
    value: str = ""


@pytest.fixture(autouse=True)
def _restore_validators():
    snapshot = dict(operation_authority._VALIDATORS)
    yield
    operation_authority._VALIDATORS.clear()
    operation_authority._VALIDATORS.update(snapshot)


@pytest.fixture
def catalog_manager():
    """Swap in a fresh CatalogManager so operation_catalog.current() is controllable."""
    original = operation_catalog._MANAGER
    manager = operation_catalog.CatalogManager()
    operation_catalog._MANAGER = manager
    try:
        yield manager
    finally:
        operation_catalog._MANAGER = original


def _register_agent_run_validator() -> None:
    operation_authority.register_validator(PrincipalKind.AGENT_RUN, lambda principal: True)


def _seed_extension(extension_id: str, record: dict) -> None:
    data = extension_store._load()
    data["extensions"][extension_id] = record
    extension_store._save(data)


# --- VerifiedPrincipal sentinel ------------------------------------------


def test_verified_principal_rejects_forged_sentinel():
    with pytest.raises(PermissionError, match="issued by operation_authority"):
        operation_authority.VerifiedPrincipal(_principal(), object())


# --- register_validator / restore_validator -------------------------------


def test_register_validator_returns_previous_and_restores():
    first = operation_authority.register_validator(
        PrincipalKind.NODE_RELAY, lambda principal: True
    )
    assert first is None  # no prior NODE_RELAY validator
    second = operation_authority.register_validator(
        PrincipalKind.NODE_RELAY, lambda principal: False
    )
    assert second is not None  # previous returned

    operation_authority.restore_validator(PrincipalKind.NODE_RELAY, second)
    assert operation_authority._VALIDATORS[PrincipalKind.NODE_RELAY] is second

    operation_authority.restore_validator(PrincipalKind.NODE_RELAY, None)
    assert PrincipalKind.NODE_RELAY not in operation_authority._VALIDATORS


def test_restore_validator_overwrites_when_given_value():
    operation_authority.restore_validator(PrincipalKind.NODE_RELAY, lambda p: True)
    assert PrincipalKind.NODE_RELAY in operation_authority._VALIDATORS


# --- issue / verify / resolve --------------------------------------------


def test_issue_wraps_principal():
    _register_agent_run_validator()
    verified = operation_authority.issue(_principal())
    assert isinstance(verified, operation_authority.VerifiedPrincipal)
    assert verified.principal.principal_id == "run-1"


def test_verify_roundtrip_returns_principal():
    _register_agent_run_validator()
    verified = operation_authority.issue(_principal())
    assert operation_authority.verify(verified) is verified


def test_verify_rejects_non_verified_principal():
    with pytest.raises(PermissionError, match="not issued by operation authority"):
        operation_authority.verify(_principal())  # type: ignore[arg-type]


def test_resolve_issues_principal_from_reference():
    _register_agent_run_validator()
    reference = _principal().reference()
    verified = operation_authority.resolve(reference, availability_generation="gen-x")
    assert isinstance(verified, operation_authority.VerifiedPrincipal)
    assert verified.principal.availability_generation == "gen-x"
    assert verified.principal.permitted_operations == ("example_mutate",)


# --- _validate_principal branches (driven via issue) ---------------------


def test_validate_rejects_wrong_audience():
    _register_agent_run_validator()
    with pytest.raises(PermissionError, match="audience is invalid"):
        operation_authority.issue(_principal(audience="wrong"))


def test_validate_rejects_expired_principal():
    _register_agent_run_validator()
    with pytest.raises(PermissionError, match="principal expired"):
        operation_authority.issue(_expired_principal())


def test_validate_rejects_when_no_validator_registered():
    # NODE_RELAY has no validator registered by default.
    with pytest.raises(PermissionError, match="no longer authorized"):
        operation_authority.issue(_principal(kind=PrincipalKind.NODE_RELAY))


def test_validate_rejects_when_validator_returns_false():
    operation_authority.register_validator(PrincipalKind.AGENT_RUN, lambda principal: False)
    with pytest.raises(PermissionError, match="no longer authorized"):
        operation_authority.issue(_principal())


def test_validate_accepts_when_validator_returns_true():
    _register_agent_run_validator()
    assert operation_authority.issue(_principal()).principal.kind == PrincipalKind.AGENT_RUN


# --- bind / current_principal --------------------------------------------


def test_bind_exposes_current_principal():
    principal = _principal()
    with operation_authority.bind(principal):
        assert operation_authority.current_principal() is principal


def test_current_principal_raises_when_unbound():
    with pytest.raises(PermissionError, match="principal is not bound"):
        operation_authority.current_principal()


# --- current_extension_generation ----------------------------------------


def test_generation_from_install_path():
    _seed_extension("gen-path", {"source": {"install_path": "/srv/ext"}})
    expected = hashlib.sha256(b"/srv/ext").hexdigest()[:16]
    assert operation_authority.current_extension_generation("gen-path") == expected


def test_generation_falls_back_to_source_version():
    _seed_extension("gen-src-ver", {"source": {"version": "1.2.3"}})
    expected = hashlib.sha256(b"1.2.3").hexdigest()[:16]
    assert operation_authority.current_extension_generation("gen-src-ver") == expected


def test_generation_falls_back_to_record_version():
    _seed_extension("gen-rec-ver", {"version": "9.9.9"})
    expected = hashlib.sha256(b"9.9.9").hexdigest()[:16]
    assert operation_authority.current_extension_generation("gen-rec-ver") == expected


def test_generation_unknown_when_blank_or_missing():
    _seed_extension("gen-blank", {"source": {}})
    assert operation_authority.current_extension_generation("gen-blank") == "unknown"


def test_generation_unknown_when_no_record():
    assert operation_authority.current_extension_generation("gen-missing") == "unknown"


# --- _validate_extension_server ------------------------------------------


def _active_extension_record(grant: str, *, capabilities, enabled=True) -> dict:
    return {
        "enabled": enabled,
        "entitlement": {"status": "not_required"},
        "version": "0",
        "source": {"install_path": grant},
        "manifest": {"permissions": {"capabilities": capabilities}},
    }


def _extension_principal(grant: str, ext_id: str, *, operations) -> RuntimePrincipal:
    return _principal(
        kind=PrincipalKind.EXTENSION_SERVER,
        principal_id=ext_id,
        grant_generation=hashlib.sha256(grant.encode()).hexdigest()[:16],
        permitted_operations=operations,
    )


def test_extension_server_validates_when_grants_match(catalog_manager):
    descriptor = catalog_manager.register_capability(
        "example", "mutate", _Request, lambda req: None
    )
    catalog_manager.publish()
    grant = "/srv/ext"
    _seed_extension(
        "ext-ok", _active_extension_record(grant, capabilities=[f"{descriptor.capability}.{descriptor.action}"])
    )
    principal = _extension_principal(grant, "ext-ok", operations=(descriptor.key,))
    assert operation_authority._validate_extension_server(principal) is True


def test_extension_server_false_when_extension_missing(catalog_manager):
    catalog_manager.register_capability("example", "mutate", _Request, lambda req: None)
    catalog_manager.publish()
    principal = _extension_principal("/srv/ext", "ext-missing", operations=("example_mutate",))
    assert operation_authority._validate_extension_server(principal) is False


def test_extension_server_false_when_not_active(catalog_manager):
    descriptor = catalog_manager.register_capability(
        "example", "mutate", _Request, lambda req: None
    )
    catalog_manager.publish()
    grant = "/srv/ext"
    _seed_extension(
        "ext-disabled",
        _active_extension_record(grant, capabilities=[f"{descriptor.capability}.{descriptor.action}"], enabled=False),
    )
    principal = _extension_principal(grant, "ext-disabled", operations=(descriptor.key,))
    assert operation_authority._validate_extension_server(principal) is False


def test_extension_server_false_when_generation_mismatch(catalog_manager):
    descriptor = catalog_manager.register_capability(
        "example", "mutate", _Request, lambda req: None
    )
    catalog_manager.publish()
    grant = "/srv/ext"
    _seed_extension(
        "ext-gen", _active_extension_record(grant, capabilities=[f"{descriptor.capability}.{descriptor.action}"])
    )
    # grant_generation does not match the seeded install_path digest.
    principal = _extension_principal("/srv/different", "ext-gen", operations=(descriptor.key,))
    assert operation_authority._validate_extension_server(principal) is False


def test_extension_server_false_when_grants_insufficient(catalog_manager):
    descriptor = catalog_manager.register_capability(
        "example", "mutate", _Request, lambda req: None
    )
    catalog_manager.publish()
    grant = "/srv/ext"
    _seed_extension("ext-nogrant", _active_extension_record(grant, capabilities=["unrelated.capability"]))
    principal = _extension_principal(grant, "ext-nogrant", operations=(descriptor.key,))
    assert operation_authority._validate_extension_server(principal) is False


def test_extension_server_false_when_operation_unknown_in_catalog(catalog_manager):
    grant = "/srv/ext"
    _seed_extension("ext-unknownop", _active_extension_record(grant, capabilities=["example.mutate"]))
    catalog_manager.publish()  # catalog published but no descriptor for the op
    principal = _extension_principal(grant, "ext-unknownop", operations=("example_missing",))
    assert operation_authority._validate_extension_server(principal) is False
