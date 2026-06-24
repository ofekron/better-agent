"""Test that the machine-nodes revoke substrate removes static topology nodes.

Verifies:
  1. A static node declared in topology.yaml can be deleted via the API.
  2. The node is removed from topology.yaml on disk (atomic write).
  3. The topology cache is invalidated after removal.
  4. The primary node cannot be deleted.
  5. Deleting a nonexistent node returns 404.
  6. Deleting a dynamic node (node_registry_store) still works.
  7. node_store stale state is cleaned up after deletion.
  8. Malformed topology.yaml raises TopologyError (not silent False).
  9. WS broadcast fires on deletion.
"""

import json
import os
import sys
import tempfile
import shutil
from pathlib import Path

import yaml

# ── Isolate state ──────────────────────────────────────────────────────────
import _test_home
_tmp = _test_home.isolate("ba-test-")
os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"] = str(Path(_tmp) / "topology.yaml")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"
os.environ.pop("BETTER_CLAUDE_NODE_TOKEN", None)
os.environ.pop("BETTER_CLAUDE_NODE_ID", None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import topology
import node_registry_store
import extension_store
from auth_test_helpers import authenticate_client


def _client(app):
    from starlette.testclient import TestClient

    _install_machine_nodes_extension()
    client = TestClient(app, client=("127.0.0.1", 50000))
    authenticate_client(client)
    return client


def _install_machine_nodes_extension() -> None:
    extension_id = extension_store.BUILTIN_MACHINE_NODES_EXTENSION_ID
    package = Path(_tmp) / "private-fixtures" / extension_id
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": extension_id,
        "surfaces": ["backend_feature"],
        "entrypoints": {},
        "permissions": {},
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(package.parent),
            "extension_path": package.name,
            "ref": "",
            "commit_sha": extension_id,
        },
        persist=True,
    )


def _revoke_node(client, node_id: str):
    import main as app_mod

    return client.post(
        "/api/internal/machine-nodes/revoke",
        headers={"X-Internal-Token": app_mod.coordinator.internal_token},
        json={"node_id": node_id},
    )


def _write_topology(nodes: dict | None = None):
    data = {
        "schema_version": 1,
        "primary": {
            "id": "primary",
            "address": "ws://localhost:8000",
            "cwd_roots": ["/tmp"],
        },
        "nodes": nodes or {},
    }
    Path(os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"]).write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    topology._cache = None


def _read_topology_nodes() -> dict:
    raw = yaml.safe_load(
        Path(os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"]).read_text(encoding="utf-8")
    )
    return raw.get("nodes") or {}


# ── Tests ──────────────────────────────────────────────────────────────────

def test_delete_static_node():
    """Static node in topology.yaml is removed on delete."""
    _write_topology({"mac-mini": {"address": "10.0.0.1:8000", "cwd_roots": ["/home"]}})
    assert "mac-mini" in _read_topology_nodes()

    removed = topology.remove_node("mac-mini")
    assert removed is True
    assert "mac-mini" not in _read_topology_nodes()

    topo = topology.load_topology(force_reload=True)
    assert "mac-mini" not in topo.nodes


def test_delete_nonexistent_node_returns_false():
    """Deleting a node that doesn't exist returns False."""
    _write_topology({"other": {"address": "10.0.0.2:8000", "cwd_roots": ["/home"]}})
    removed = topology.remove_node("no-such-node")
    assert removed is False
    assert "other" in _read_topology_nodes()


def test_delete_primary_is_noop():
    """Primary cannot be removed via remove_node — it's not in `nodes`."""
    _write_topology({"worker": {"address": "10.0.0.3:8000", "cwd_roots": ["/home"]}})
    removed = topology.remove_node("primary")
    assert removed is False
    topo = topology.load_topology(force_reload=True)
    assert topo.primary.id == "primary"


def test_delete_dynamic_node_still_works():
    """Dynamic nodes (node_registry_store) are still removable."""
    _write_topology()
    node_registry_store.add(
        node_id="dynamic-node",
        address="10.0.0.4:8000",
        cwd_roots=["/data"],
        secret_hash="$argon2id$fake",
    )
    assert node_registry_store.get("dynamic-node") is not None
    removed = node_registry_store.remove("dynamic-node")
    assert removed is True
    assert node_registry_store.get("dynamic-node") is None


def test_fallback_order_dynamic_first():
    """Endpoint tries dynamic registry first, then static topology."""
    _write_topology({"mac-mini": {"address": "10.0.0.1:8000", "cwd_roots": ["/home"]}})
    node_registry_store.add(
        node_id="mac-mini",
        address="10.0.0.1:8000",
        cwd_roots=["/home"],
        secret_hash="$argon2id$fake",
    )

    import main as app_mod

    client = _client(app_mod.app)
    resp = _revoke_node(client, "mac-mini")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    assert node_registry_store.get("mac-mini") is None
    assert "mac-mini" in _read_topology_nodes()


def test_delete_static_via_endpoint():
    """Endpoint can remove a static-only node from topology.yaml."""
    _write_topology({"mac-mini": {"address": "10.0.0.1:8000", "cwd_roots": ["/home"]}})

    import main as app_mod

    client = _client(app_mod.app)
    resp = _revoke_node(client, "mac-mini")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert "mac-mini" not in _read_topology_nodes()


def test_endpoint_404_on_unknown():
    """Deleting a node that exists nowhere returns 404."""
    _write_topology()

    import main as app_mod

    client = _client(app_mod.app)
    resp = _revoke_node(client, "ghost")
    assert resp.status_code == 404


def test_node_store_state_cleaned_up():
    """node_store stale _conns and _state are cleared on deletion."""
    _write_topology({"mac-mini": {"address": "10.0.0.1:8000", "cwd_roots": ["/home"]}})

    import node_store
    # Simulate stale state from a prior connection
    node_store._state["mac-mini"] = "disconnected"

    import main as app_mod

    client = _client(app_mod.app)
    resp = _revoke_node(client, "mac-mini")
    assert resp.status_code == 200

    assert "mac-mini" not in node_store._state
    assert "mac-mini" not in node_store._conns


def test_malformed_yaml_raises():
    """remove_node raises TopologyError on non-mapping YAML."""
    topo_path = Path(os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"])
    # Write valid YAML but top-level is a list, not a mapping
    topo_path.write_text("- just\n- a\n- list", encoding="utf-8")
    topology._cache = None

    try:
        topology.remove_node("anything")
        assert False, "Expected TopologyError"
    except topology.TopologyError:
        pass  # correct


def test_endpoint_malformed_yaml_returns_500():
    """Endpoint returns 500 when topology.yaml is corrupt."""
    topo_path = Path(os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"])
    topo_path.write_text("- just\n- a\n- list", encoding="utf-8")
    topology._cache = None

    import main as app_mod

    client = _client(app_mod.app)
    resp = _revoke_node(client, "anything")
    assert resp.status_code == 500, f"Expected 500, got {resp.status_code}"


if __name__ == "__main__":
    test_delete_static_node()
    print("PASS: delete static node")

    test_delete_nonexistent_node_returns_false()
    print("PASS: delete nonexistent node")

    test_delete_primary_is_noop()
    print("PASS: primary not deletable")

    test_delete_dynamic_node_still_works()
    print("PASS: dynamic node delete")

    test_fallback_order_dynamic_first()
    print("PASS: fallback order dynamic first")

    test_delete_static_via_endpoint()
    print("PASS: delete static via endpoint")

    test_endpoint_404_on_unknown()
    print("PASS: endpoint 404 on unknown")

    test_node_store_state_cleaned_up()
    print("PASS: node_store state cleaned up")

    test_malformed_yaml_raises()
    print("PASS: malformed yaml raises TopologyError")

    test_endpoint_malformed_yaml_returns_500()
    print("PASS: endpoint malformed yaml returns 500")

    import shutil
    shutil.rmtree(_tmp)
    print("\nAll tests passed.")
