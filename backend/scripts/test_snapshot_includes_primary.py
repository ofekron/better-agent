"""Regression: node_store.snapshot() always includes the local/primary host.

Bug: in a dynamic-only deploy (no topology.yaml — nodes come from
node_registry_store), `snapshot()` omitted the primary host. /api/nodes
listed only remote workers, so the "run on" picker offered no primary
option, its default `node_id="primary"` matched no <option> (a
controlled <select> silently rendering the first worker), and every
session landed on "primary" regardless of the visible choice. No
machine ever showed the "(host)" badge.

Fix: snapshot() emits a role="primary" entry (id matching
/api/local_node_id, state "connected") whenever no primary is present.
"""

import os
import sys
import tempfile
from pathlib import Path

# ── Isolate state ──────────────────────────────────────────────────────────
import _test_home
_tmp = _test_home.isolate("ba-test-")
# NO topology.yaml: dynamic-only deploy (the bug's precondition). Point
# the path at a file that does NOT exist so load_topology() raises.
os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"] = str(Path(_tmp) / "absent.yaml")
os.environ.pop("BETTER_CLAUDE_NODE_ID", None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import node_store
import node_registry_store
from node_store import _local_node_id_or_primary


def _reset():
    node_store._conns.clear()
    node_store._state.clear()


def test_snapshot_has_primary_without_topology():
    """Dynamic-only deploy: snapshot still lists the primary host."""
    _reset()
    node_registry_store.add(
        node_id="remote-worker",
        address="10.0.0.9:8000",
        cwd_roots=["/home"],
        secret_hash="$argon2id$fake",
    )
    snap = {n["id"]: n for n in node_store.snapshot()}

    expected_local = _local_node_id_or_primary()
    assert expected_local in snap, (
        f"primary host {expected_local!r} missing from snapshot; "
        f"got {sorted(snap)}"
    )
    primary = snap[expected_local]
    assert primary["role"] == "primary", primary
    # Host is always connected by construction — never "unknown"/offline.
    assert primary["state"] == "connected", primary
    # Remote worker still present.
    assert "remote-worker" in snap


def test_no_duplicate_primary_when_registry_has_host_id():
    """A registry node must not duplicate the synthesized primary even
    if it happened to share the host's id."""
    _reset()
    local_id = _local_node_id_or_primary()
    node_registry_store.add(
        node_id=local_id,
        address="10.0.0.9:8000",
        cwd_roots=[],
        secret_hash="$argon2id$fake",
    )
    snap = node_store.snapshot()
    primaries = [n for n in snap if n["role"] == "primary"]
    assert len(primaries) == 1, f"expected one primary, got {primaries}"
    assert primaries[0]["id"] == local_id


def test_local_id_matches_api_helper():
    """snapshot's primary id == what /api/local_node_id returns, so the
    frontend `m.id === localNodeId` host badge lights up."""
    _reset()
    snap = {n["id"]: n for n in node_store.snapshot()}
    assert _local_node_id_or_primary() in snap


if __name__ == "__main__":
    test_snapshot_has_primary_without_topology()
    print("PASS: snapshot includes primary without topology")
    test_no_duplicate_primary_when_registry_has_host_id()
    print("PASS: no duplicate primary")
    test_local_id_matches_api_helper()
    print("PASS: primary id matches /api/local_node_id")

    import shutil
    shutil.rmtree(_tmp)
    print("\nAll tests passed.")
