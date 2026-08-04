from __future__ import annotations

import json
import sys
from pathlib import Path

import _test_home
import pytest

_TEST_HOME = Path(_test_home.isolate("ba-test-node-provider-credential-grants-"))
_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from stores import node_provider_credential_grants as grants  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_store():
    for path in (grants._path(), Path(str(grants._path()) + ".lock")):
        path.unlink(missing_ok=True)
    yield
    for path in (grants._path(), Path(str(grants._path()) + ".lock")):
        path.unlink(missing_ok=True)


def _grant(
    *,
    node_authority: str = "node-authority-a",
    provider_generation: str = "provider-generation-a",
) -> dict:
    return grants.authorize(
        node_id="lenovo",
        node_authority=node_authority,
        provider_id="zai-claude",
        provider_generation=provider_generation,
        provider_name="Z.AI Claude",
        operation_id="operation-a",
    )


def test_authorization_persists_without_secret_material() -> None:
    created = _grant()
    assert created["status"] == "pending"

    loaded = grants.valid_for_node(
        "lenovo",
        "node-authority-a",
        {"zai-claude": "provider-generation-a"},
    )
    assert loaded == [created]

    raw = grants._path().read_text(encoding="utf-8")
    assert "api_key" not in raw
    assert "secret_hash" not in raw
    assert "secret-value" not in raw


def test_exact_authority_acknowledgement_updates_status() -> None:
    _grant()
    assert grants.mark_status(
        node_id="lenovo",
        node_authority="wrong-node-authority",
        provider_id="zai-claude",
        provider_generation="provider-generation-a",
        operation_id="operation-a",
        status="synced",
        acknowledged_execution_revision=3,
    ) is None

    synced = grants.mark_status(
        node_id="lenovo",
        node_authority="node-authority-a",
        provider_id="zai-claude",
        provider_generation="provider-generation-a",
        operation_id="operation-a",
        status="synced",
        acknowledged_execution_revision=3,
    )
    assert synced is not None
    assert synced["status"] == "synced"
    assert synced["acknowledged_execution_revision"] == 3
    assert synced["updated_at"] >= synced["authorized_at"]


def test_stale_node_or_provider_authority_is_pruned() -> None:
    _grant()
    assert grants.valid_for_node(
        "lenovo",
        "node-authority-b",
        {"zai-claude": "provider-generation-a"},
    ) == []
    assert grants.public_for_node("lenovo") == []

    _grant(
        node_authority="node-authority-b",
        provider_generation="provider-generation-a",
    )
    assert grants.valid_for_node(
        "lenovo",
        "node-authority-b",
        {"zai-claude": "provider-generation-b"},
    ) == []
    assert grants.public_for_node("lenovo") == []


def test_remove_node_clears_all_authorizations() -> None:
    _grant()
    grants.authorize(
        node_id="lenovo",
        node_authority="node-authority-a",
        provider_id="other-provider",
        provider_generation="other-generation",
        provider_name="Other",
        operation_id="operation-b",
    )
    assert grants.remove_node("lenovo") is True
    assert grants.public_for_node("lenovo") == []
    assert grants.remove_node("lenovo") is False


def test_invalid_store_shape_fails_closed() -> None:
    grants._path().parent.mkdir(parents=True, exist_ok=True)
    grants._path().write_text(
        json.dumps({"schema_version": 999, "grants": [{"api_key": "leak"}]}),
        encoding="utf-8",
    )
    assert grants.public_for_node("lenovo") == []

    grants._path().write_text(
        json.dumps(
            {
                "schema_version": 1,
                "grants": [{**_grant(), "api_key": "must-not-persist"}],
            }
        ),
        encoding="utf-8",
    )
    assert grants.public_for_node("lenovo") == []
