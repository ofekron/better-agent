from __future__ import annotations

import pytest
import yaml

import local_machine_identity
import topology


@pytest.fixture(autouse=True)
def _reset_identity_and_topology(monkeypatch):
    monkeypatch.setattr(local_machine_identity, "_local_machine_id", None)
    topology._cache = None
    for name in (
        "BETTER_AGENT_TOPOLOGY_PATH",
        "BETTER_CLAUDE_TOPOLOGY_PATH",
        "BETTER_AGENT_NODE_ID",
        "BETTER_CLAUDE_NODE_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    topology._cache = None


def test_identity_is_required_before_read() -> None:
    with pytest.raises(RuntimeError, match="not initialized"):
        local_machine_identity.require_local_machine_id()


def test_identity_is_validated_and_immutable() -> None:
    assert local_machine_identity.initialize_local_machine_id("worker-1") == "worker-1"
    assert local_machine_identity.initialize_local_machine_id("worker-1") == "worker-1"
    assert local_machine_identity.require_local_machine_id() == "worker-1"

    with pytest.raises(RuntimeError, match="already initialized"):
        local_machine_identity.initialize_local_machine_id("worker-2")

    assert local_machine_identity.require_matching_local_machine_id("worker-1") == "worker-1"
    with pytest.raises(ValueError, match="does not match"):
        local_machine_identity.require_matching_local_machine_id("primary")


@pytest.mark.parametrize("node_id", ["", " worker", "worker/1", "a" * 129, None])
def test_identity_rejects_invalid_node_ids(node_id) -> None:
    with pytest.raises(ValueError, match="invalid"):
        local_machine_identity.initialize_local_machine_id(node_id)


def test_primary_identity_defaults_only_when_topology_is_absent() -> None:
    assert local_machine_identity.initialize_primary_machine_id() == "primary"


def test_primary_identity_defaults_when_launcher_path_is_absent(
    monkeypatch, tmp_path,
) -> None:
    topology_path = tmp_path / "topology.yaml"
    monkeypatch.setenv("BETTER_AGENT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("BETTER_CLAUDE_TOPOLOGY_PATH", str(topology_path))

    assert local_machine_identity.initialize_primary_machine_id() == "primary"


def test_primary_identity_uses_declared_topology_id(monkeypatch, tmp_path) -> None:
    topology_path = tmp_path / "topology.yaml"
    topology_path.write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "primary": {"id": "studio-mac", "address": "ws://studio:8001"},
            "nodes": {},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("BETTER_AGENT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("BETTER_AGENT_NODE_ID", "studio-mac")

    assert local_machine_identity.initialize_primary_machine_id() == "studio-mac"


def test_configured_invalid_topology_fails_without_initializing(monkeypatch, tmp_path) -> None:
    topology_path = tmp_path / "topology.yaml"
    topology_path.write_text("schema_version: 999\n", encoding="utf-8")
    monkeypatch.setenv("BETTER_AGENT_TOPOLOGY_PATH", str(topology_path))

    with pytest.raises(topology.TopologyError):
        local_machine_identity.initialize_primary_machine_id()
    with pytest.raises(RuntimeError, match="not initialized"):
        local_machine_identity.require_local_machine_id()


def test_configured_topology_rejects_undeclared_local_node(
    monkeypatch, tmp_path,
) -> None:
    topology_path = tmp_path / "topology.yaml"
    topology_path.write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "primary": {"id": "studio-mac", "address": "ws://studio:8001"},
            "nodes": {},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("BETTER_AGENT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("BETTER_AGENT_NODE_ID", "undeclared")

    with pytest.raises(topology.TopologyError, match="not declared"):
        local_machine_identity.initialize_primary_machine_id()
    with pytest.raises(RuntimeError, match="not initialized"):
        local_machine_identity.require_local_machine_id()
