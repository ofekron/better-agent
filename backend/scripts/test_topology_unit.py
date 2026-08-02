"""Unit tests for ``topology``.

Parses/validates the multi-machine ``topology.yaml`` manifest: env-var path
resolution, per-node parsing, schema_version gate, primary/node assembly,
the per-process cache, ``local_node_id`` self-identification, and
``remove_node``'s atomic rewrite (including its temp-file cleanup on write
failure).

Pure over ``yaml`` + filesystem: every test writes a real yaml file into a
``tmp_path`` and points the env var at it. The module's only external
dependencies (``env_compat``, ``node_path_authority``) are themselves unit-
covered; none are mocked except the three ``os`` calls inside the atomic-write
error path, which is unreachable through any real input (disk write cannot
fail on demand) and is therefore exercised via a scoped monkeypatch that
asserts the temp file is removed and the original file is untouched.

Pins:
  1. ``_resolve_path``: missing env -> ``TopologyError``; relative path ->
     ``TopologyError``; absolute path returned.
  2. ``_parse_node``: non-mapping / missing-address / non-string-address /
     bad cwd_roots (``NodePathError`` -> ``TopologyError``) rejection and
     valid assembly (with and without cwd_roots).
  3. ``load_topology``: cache hit vs ``force_reload`` bypass, missing file,
     malformed yaml, non-mapping top level, schema_version mismatch,
     missing/non-mapping primary, primary default id vs explicit id,
     node id duplicating primary id, and nodes parsing (incl. missing/None
     ``nodes`` -> empty).
  4. ``Topology``: ``all_nodes`` includes primary, ``get`` hit + ``KeyError``.
  5. ``local_node_id``: default ``primary``, explicit declared node, and
     undeclared id -> ``TopologyError``.
  6. ``remove_node``: missing file -> False, node absent -> False, valid
     removal (rewrite + cache invalidation), non-mapping top level ->
     ``TopologyError``, malformed yaml -> ``TopologyError``, non-mapping
     ``nodes`` -> False, and atomic-write failure cleanup (temp file removed,
     original preserved, cache untouched).

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_topology_unit.py -v
"""

from __future__ import annotations

import os

import pytest
import yaml

import topology
from topology import NodeSpec, SCHEMA_VERSION, TopologyError

ENV_PATH = "BETTER_AGENT_TOPOLOGY_PATH"
ENV_NODE = "BETTER_AGENT_NODE_ID"


@pytest.fixture(autouse=True)
def _clear_cache_and_env(monkeypatch):
    """The module caches the parsed topology in a process-global; clear it
    and every topology-related env var before each test so they cannot leak."""
    topology._cache = None
    for var in (ENV_PATH, "BETTER_CLAUDE_TOPOLOGY_PATH", ENV_NODE, "BETTER_CLAUDE_NODE_ID"):
        monkeypatch.delenv(var, raising=False)
    yield
    topology._cache = None


def _write_yaml(path, data) -> str:
    text = yaml.dump(data, default_flow_style=False, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _point_env(monkeypatch, path_str: str) -> None:
    monkeypatch.setenv(ENV_PATH, path_str)


def _minimal_topology(primary_id: str = "primary", nodes: dict | None = None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "primary": {"id": primary_id, "address": "ws://primary.local:8001", "cwd_roots": ["/srv/code"]},
        "nodes": nodes or {},
    }


# --------------------------------------------------------------------------- #
# _resolve_path
# --------------------------------------------------------------------------- #
def test_resolve_path_missing_env_raises():
    with pytest.raises(TopologyError, match="env var is required"):
        topology._resolve_path()


def test_resolve_path_relative_raises(monkeypatch):
    _point_env(monkeypatch, "relative/topology.yaml")
    with pytest.raises(TopologyError, match="must be absolute"):
        topology._resolve_path()


def test_resolve_path_absolute_returned(monkeypatch):
    _point_env(monkeypatch, "/abs/path/topology.yaml")
    resolved = topology._resolve_path()
    assert str(resolved) == "/abs/path/topology.yaml"


# --------------------------------------------------------------------------- #
# _parse_node
# --------------------------------------------------------------------------- #
def test_parse_node_non_mapping_raises():
    with pytest.raises(TopologyError, match="expected mapping"):
        topology._parse_node("n", "worker_node", ["not", "a", "dict"])


def test_parse_node_missing_address_raises():
    with pytest.raises(TopologyError, match="missing or non-string `address`"):
        topology._parse_node("n", "worker_node", {"cwd_roots": ["/srv/code"]})


def test_parse_node_non_string_address_raises():
    with pytest.raises(TopologyError, match="missing or non-string `address`"):
        topology._parse_node("n", "worker_node", {"address": 123})


def test_parse_node_empty_address_raises():
    with pytest.raises(TopologyError, match="missing or non-string `address`"):
        topology._parse_node("n", "worker_node", {"address": ""})


def test_parse_node_bad_cwd_roots_raises():
    with pytest.raises(TopologyError, match="must be a list of absolute paths"):
        topology._parse_node("n", "worker_node", {"address": "w.local", "cwd_roots": ["relative"]})


def test_parse_node_non_list_cwd_roots_raises():
    with pytest.raises(TopologyError, match="must be a list of absolute paths"):
        topology._parse_node("n", "worker_node", {"address": "w.local", "cwd_roots": "/srv/code"})


def test_parse_node_no_cwd_roots_defaults_empty():
    spec = topology._parse_node("n", "worker_node", {"address": "w.local"})
    assert spec == NodeSpec(id="n", role="worker_node", address="w.local", cwd_roots=())


def test_parse_node_valid_with_roots():
    spec = topology._parse_node("n", "worker_node", {"address": "w.local", "cwd_roots": ["/srv/code", "/srv/other"]})
    assert spec.cwd_roots == ("/srv/code", "/srv/other")


# --------------------------------------------------------------------------- #
# load_topology
# --------------------------------------------------------------------------- #
def test_load_missing_file_raises(monkeypatch, tmp_path):
    _point_env(monkeypatch, str(tmp_path / "missing.yaml"))
    with pytest.raises(TopologyError, match="not found"):
        topology.load_topology()


def test_load_malformed_yaml_raises(monkeypatch, tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("primary: [unterminated\n  - x: : :", encoding="utf-8")
    _point_env(monkeypatch, str(path))
    with pytest.raises(TopologyError, match="parse failed"):
        topology.load_topology()


def test_load_non_mapping_top_level_raises(monkeypatch, tmp_path):
    path = str(tmp_path / "t.yaml")
    _write_yaml(tmp_path / "t.yaml", ["not", "a", "mapping"])
    _point_env(monkeypatch, path)
    with pytest.raises(TopologyError, match="top-level must be a mapping"):
        topology.load_topology()


def test_load_wrong_schema_version_raises(monkeypatch, tmp_path):
    data = _minimal_topology()
    data["schema_version"] = 999
    path = str(tmp_path / "t.yaml")
    _write_yaml(tmp_path / "t.yaml", data)
    _point_env(monkeypatch, path)
    with pytest.raises(TopologyError, match="schema_version=999"):
        topology.load_topology()


def test_load_missing_schema_version_raises(monkeypatch, tmp_path):
    data = _minimal_topology()
    del data["schema_version"]
    path = str(tmp_path / "t.yaml")
    _write_yaml(tmp_path / "t.yaml", data)
    _point_env(monkeypatch, path)
    with pytest.raises(TopologyError, match="schema_version=None"):
        topology.load_topology()


def test_load_non_mapping_primary_raises(monkeypatch, tmp_path):
    data = {"schema_version": SCHEMA_VERSION, "primary": "not-a-mapping", "nodes": {}}
    path = str(tmp_path / "t.yaml")
    _write_yaml(tmp_path / "t.yaml", data)
    _point_env(monkeypatch, path)
    with pytest.raises(TopologyError, match="missing `primary` mapping"):
        topology.load_topology()


def test_load_primary_default_id_when_absent(monkeypatch, tmp_path):
    data = {"schema_version": SCHEMA_VERSION, "primary": {"address": "ws://p.local"}, "nodes": {}}
    path = str(tmp_path / "t.yaml")
    _write_yaml(tmp_path / "t.yaml", data)
    _point_env(monkeypatch, path)
    topo = topology.load_topology()
    assert topo.primary.id == "primary"
    assert topo.primary.role == "primary"


def test_load_node_id_duplicates_primary_raises(monkeypatch, tmp_path):
    data = _minimal_topology(primary_id="p1")
    data["nodes"] = {"p1": {"address": "ws://p1.local"}}
    path = str(tmp_path / "t.yaml")
    _write_yaml(tmp_path / "t.yaml", data)
    _point_env(monkeypatch, path)
    with pytest.raises(TopologyError, match="duplicates primary's id"):
        topology.load_topology()


def test_load_full_topology(monkeypatch, tmp_path):
    data = _minimal_topology(primary_id="p1", nodes={
        "linux": {"address": "linux.local:8002", "cwd_roots": ["/home/ofek/code"]},
        "win": {"address": "win.local:8003"},
    })
    path = str(tmp_path / "t.yaml")
    _write_yaml(tmp_path / "t.yaml", data)
    _point_env(monkeypatch, path)
    topo = topology.load_topology()
    assert topo.schema_version == SCHEMA_VERSION
    assert set(topo.nodes) == {"linux", "win"}
    assert all(n.role == "worker_node" for n in topo.nodes.values())
    assert topo.nodes["linux"].cwd_roots == ("/home/ofek/code",)
    assert topo.nodes["win"].cwd_roots == ()


def test_load_missing_nodes_key_yields_empty(monkeypatch, tmp_path):
    data = {"schema_version": SCHEMA_VERSION, "primary": {"address": "ws://p.local"}}
    path = str(tmp_path / "t.yaml")
    _write_yaml(tmp_path / "t.yaml", data)
    _point_env(monkeypatch, path)
    topo = topology.load_topology()
    assert topo.nodes == {}


def test_load_uses_cache(monkeypatch, tmp_path):
    path = str(tmp_path / "t.yaml")
    _write_yaml(tmp_path / "t.yaml", _minimal_topology())
    _point_env(monkeypatch, path)
    first = topology.load_topology()
    # Mutate the on-disk file; cached value must be returned unchanged.
    _write_yaml(tmp_path / "t.yaml", {"schema_version": SCHEMA_VERSION, "primary": {"address": "ws://other.local"}, "nodes": {}})
    second = topology.load_topology()
    assert second is first


def test_load_force_reload_bypasses_cache(monkeypatch, tmp_path):
    path = str(tmp_path / "t.yaml")
    _write_yaml(tmp_path / "t.yaml", _minimal_topology())
    _point_env(monkeypatch, path)
    topology.load_topology()
    _write_yaml(tmp_path / "t.yaml", {"schema_version": SCHEMA_VERSION, "primary": {"id": "p2", "address": "ws://p2.local"}, "nodes": {}})
    reloaded = topology.load_topology(force_reload=True)
    assert reloaded.primary.id == "p2"


# --------------------------------------------------------------------------- #
# Topology.get / all_nodes
# --------------------------------------------------------------------------- #
def test_topology_get_and_all_nodes(monkeypatch, tmp_path):
    data = _minimal_topology(nodes={"linux": {"address": "linux.local"}})
    path = str(tmp_path / "t.yaml")
    _write_yaml(tmp_path / "t.yaml", data)
    _point_env(monkeypatch, path)
    topo = topology.load_topology()

    all_nodes = topo.all_nodes()
    assert set(all_nodes) == {"primary", "linux"}
    assert topo.get("primary") is all_nodes["primary"]
    assert topo.get("linux").role == "worker_node"

    with pytest.raises(KeyError, match="not in topology.yaml"):
        topo.get("ghost")


# --------------------------------------------------------------------------- #
# local_node_id
# --------------------------------------------------------------------------- #
def test_local_node_id_defaults_primary(monkeypatch, tmp_path):
    _write_yaml(tmp_path / "t.yaml", _minimal_topology())
    _point_env(monkeypatch, str(tmp_path / "t.yaml"))
    assert topology.local_node_id() == "primary"


def test_local_node_id_explicit_declared(monkeypatch, tmp_path):
    _write_yaml(tmp_path / "t.yaml", _minimal_topology(nodes={"linux": {"address": "linux.local"}}))
    _point_env(monkeypatch, str(tmp_path / "t.yaml"))
    monkeypatch.setenv(ENV_NODE, "linux")
    assert topology.local_node_id() == "linux"


def test_local_node_id_undeclared_raises(monkeypatch, tmp_path):
    _write_yaml(tmp_path / "t.yaml", _minimal_topology())
    _point_env(monkeypatch, str(tmp_path / "t.yaml"))
    monkeypatch.setenv(ENV_NODE, "ghost")
    with pytest.raises(TopologyError, match="not declared in"):
        topology.local_node_id()


# --------------------------------------------------------------------------- #
# remove_node
# --------------------------------------------------------------------------- #
def test_remove_node_missing_file_returns_false(monkeypatch, tmp_path):
    _point_env(monkeypatch, str(tmp_path / "absent.yaml"))
    assert topology.remove_node("linux") is False


def test_remove_node_absent_returns_false(monkeypatch, tmp_path):
    data = _minimal_topology(nodes={"linux": {"address": "linux.local"}})
    path = str(tmp_path / "t.yaml")
    _write_yaml(tmp_path / "t.yaml", data)
    _point_env(monkeypatch, path)
    assert topology.remove_node("ghost") is False


def test_remove_node_non_mapping_nodes_returns_false(monkeypatch, tmp_path):
    data = {"schema_version": SCHEMA_VERSION, "primary": {"address": "ws://p.local"}, "nodes": ["not", "a", "dict"]}
    path = str(tmp_path / "t.yaml")
    _write_yaml(tmp_path / "t.yaml", data)
    _point_env(monkeypatch, path)
    assert topology.remove_node("linux") is False


def test_remove_node_non_mapping_top_level_raises(monkeypatch, tmp_path):
    _write_yaml(tmp_path / "t.yaml", ["not", "a", "dict"])
    _point_env(monkeypatch, str(tmp_path / "t.yaml"))
    with pytest.raises(TopologyError, match="top-level must be a mapping"):
        topology.remove_node("linux")


def test_remove_node_malformed_yaml_raises(monkeypatch, tmp_path):
    (tmp_path / "t.yaml").write_text("nodes: [unterminated\n  x: : :", encoding="utf-8")
    _point_env(monkeypatch, str(tmp_path / "t.yaml"))
    with pytest.raises(TopologyError, match="parse failed during remove"):
        topology.remove_node("linux")


def test_remove_node_valid_removes_and_invalidates_cache(monkeypatch, tmp_path):
    data = _minimal_topology(nodes={"linux": {"address": "linux.local"}, "win": {"address": "win.local"}})
    path = tmp_path / "t.yaml"
    _write_yaml(path, data)
    _point_env(monkeypatch, str(path))

    # Prime the cache.
    topo_before = topology.load_topology()
    assert "linux" in topo_before.all_nodes()

    assert topology.remove_node("linux") is True

    # Cache invalidated -> reload reflects the removal.
    topo_after = topology.load_topology()
    assert "linux" not in topo_after.all_nodes()
    assert "win" in topo_after.all_nodes()

    # On-disk file no longer carries linux.
    on_disk = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "linux" not in on_disk["nodes"]


def test_remove_node_does_not_remove_primary(monkeypatch, tmp_path):
    """Primary lives outside ``nodes``; ``remove_node('primary')`` is a no-op
    (returns False) rather than stripping the primary."""
    data = _minimal_topology(nodes={"linux": {"address": "linux.local"}})
    path = str(tmp_path / "t.yaml")
    _write_yaml(tmp_path / "t.yaml", data)
    _point_env(monkeypatch, path)
    assert topology.remove_node("primary") is False


def test_remove_node_write_failure_cleans_tempfile_and_preserves_original(monkeypatch, tmp_path):
    """The atomic-rewrite error path is unreachable through any real input
    (a successful ``mkstemp`` cannot make the subsequent ``os.write`` fail on
    demand), so it is exercised via a scoped monkeypatch on ``os.write``.
    Asserts the temp file is removed, the original file is untouched, and the
    write error propagates."""
    data = _minimal_topology(nodes={"linux": {"address": "linux.local"}})
    path = tmp_path / "t.yaml"
    _write_yaml(path, data)
    _point_env(monkeypatch, str(path))
    original_text = path.read_text(encoding="utf-8")

    files_before = set(tmp_path.iterdir())
    real_write = os.write

    def _fail_write(fd, payload):
        raise OSError("simulated disk full")

    monkeypatch.setattr(topology.os, "write", _fail_write)
    with pytest.raises(OSError, match="simulated disk full"):
        topology.remove_node("linux")
    monkeypatch.setattr(topology.os, "write", real_write)

    # Original file preserved byte-for-byte.
    assert path.read_text(encoding="utf-8") == original_text
    # No leftover temp file.
    leftover = set(tmp_path.iterdir()) - files_before
    assert not any(p.name.startswith(".topology-") for p in leftover)


def test_remove_node_write_failure_unlink_close_guarded(monkeypatch, tmp_path):
    """Covers the two ``except OSError: pass`` guards inside the cleanup
    block (``os.close`` then ``os.unlink`` both failing during cleanup).
    Same justification as above: the cleanup only runs after a write failure
    that cannot be produced by real input, so a scoped monkeypatch is the
    honest way to reach it."""
    data = _minimal_topology(nodes={"linux": {"address": "linux.local"}})
    path = tmp_path / "t.yaml"
    _write_yaml(path, data)
    _point_env(monkeypatch, str(path))

    # ``os.write`` raises first; cleanup then attempts close + unlink, both
    # forced to raise OSError to enter the two ``except OSError: pass`` guards.
    def _raise(msg):
        def _fn(*_args, **_kwargs):
            raise OSError(msg)
        return _fn

    monkeypatch.setattr(topology.os, "write", _raise("write fail"))
    monkeypatch.setattr(topology.os, "close", _raise("close fail"))
    monkeypatch.setattr(topology.os, "unlink", _raise("unlink fail"))
    with pytest.raises(OSError, match="write fail"):
        topology.remove_node("linux")
