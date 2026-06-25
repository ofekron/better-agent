import json
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_tmp = _test_home.isolate("ba-node-registry-cache-")

import node_registry_store


def _registry_dir() -> Path:
    path = Path(_tmp) / "node_registry"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_record(node_id: str, *, address: str = "ws://node") -> None:
    (_registry_dir() / f"{node_id}.json").write_text(
        json.dumps(
            {
                "schema_version": node_registry_store.SCHEMA_VERSION,
                "node_id": node_id,
                "address": address,
                "cwd_roots": ["/tmp"],
                "secret_hash": "$argon2id$fake",
                "approved_at": f"2026-01-01T00:00:{node_id[-1] if node_id[-1].isdigit() else '0'}",
            }
        ),
        encoding="utf-8",
    )


def _reset() -> None:
    for path in _registry_dir().glob("*.json"):
        path.unlink()
    version_path = _registry_dir() / ".version"
    if version_path.exists():
        version_path.unlink()
    node_registry_store._reset_cache_for_tests()


def test_version_and_list_share_cached_projection() -> None:
    _reset()
    _write_record("node-1")
    _write_record("node-2")
    scans = 0
    original = node_registry_store._iter_registry_paths

    def counted():
        nonlocal scans
        scans += 1
        return original()

    node_registry_store._iter_registry_paths = counted
    try:
        first = node_registry_store.version_token()
        assert first == (0, 0, 0)
        assert scans == 0
        assert [rec["node_id"] for rec in node_registry_store.list_all()] == ["node-1", "node-2"]
        assert scans == 1
        assert node_registry_store.version_token() == first
        assert scans == 1
    finally:
        node_registry_store._iter_registry_paths = original


def test_add_remove_and_overwrite_update_warm_cache_without_scan() -> None:
    _reset()
    assert node_registry_store.list_all() == []
    original = node_registry_store._iter_registry_paths

    def fail_scan():
        raise AssertionError("warm cache should not rescan")

    node_registry_store._iter_registry_paths = fail_scan
    try:
        before = node_registry_store.version_token()
        node_registry_store.add(
            node_id="node-1",
            address="ws://one",
            cwd_roots=["/one"],
            secret_hash="$argon2id$one",
        )
        after_add = node_registry_store.version_token()
        assert after_add != before
        assert [rec["address"] for rec in node_registry_store.list_all()] == ["ws://one"]

        node_registry_store.add(
            node_id="node-1",
            address="ws://one-replaced",
            cwd_roots=["/replaced"],
            secret_hash="$argon2id$two",
        )
        listed = node_registry_store.list_all()
        assert len(listed) == 1
        assert listed[0]["address"] == "ws://one-replaced"
        assert listed[0]["cwd_roots"] == ["/replaced"]

        token_after_replace = node_registry_store.version_token()
        assert token_after_replace != after_add
        assert node_registry_store.remove("node-1") is True
        assert node_registry_store.version_token() != token_after_replace
        assert node_registry_store.list_all() == []
    finally:
        node_registry_store._iter_registry_paths = original


def test_rebuild_skips_malformed_and_wrong_schema() -> None:
    _reset()
    _write_record("node-1")
    (_registry_dir() / "bad.json").write_text("{", encoding="utf-8")
    (_registry_dir() / "wrong.json").write_text(
        json.dumps({"schema_version": 999, "node_id": "wrong"}),
        encoding="utf-8",
    )
    assert [rec["node_id"] for rec in node_registry_store.list_all()] == ["node-1"]
    assert node_registry_store.version_token() == (0, 0, 0)


def test_list_all_returns_copies_and_get_reads_authority_file() -> None:
    _reset()
    _write_record("node-1", address="ws://old")
    listed = node_registry_store.list_all()
    listed[0]["address"] = "mutated"
    listed[0]["cwd_roots"].append("/mutated")
    again = node_registry_store.list_all()[0]
    assert again["address"] == "ws://old"
    assert again["cwd_roots"] == ["/tmp"]

    _write_record("node-1", address="ws://direct-edit")
    assert node_registry_store.list_all()[0]["address"] == "ws://old"
    assert node_registry_store.get("node-1")["address"] == "ws://direct-edit"


def test_node_snapshot_invalidation_after_registry_writes() -> None:
    _reset()
    os.environ["BETTER_CLAUDE_TOPOLOGY_PATH"] = str(Path(_tmp) / "absent.yaml")
    import node_store

    node_store._snapshot_static_cache_key = None
    node_store._snapshot_static_cache = None
    first = {node["id"] for node in node_store.snapshot()}
    assert "node-1" not in first

    node_registry_store.add(
        node_id="node-1",
        address="ws://one",
        cwd_roots=["/tmp"],
        secret_hash="$argon2id$fake",
    )
    second = {node["id"] for node in node_store.snapshot()}
    assert "node-1" in second

    assert node_registry_store.remove("node-1") is True
    third = {node["id"] for node in node_store.snapshot()}
    assert "node-1" not in third


if __name__ == "__main__":
    test_version_and_list_share_cached_projection()
    print("PASS: version/list share cached projection")
    test_add_remove_and_overwrite_update_warm_cache_without_scan()
    print("PASS: add/remove/overwrite update warm cache")
    test_rebuild_skips_malformed_and_wrong_schema()
    print("PASS: malformed registry files skipped")
    test_list_all_returns_copies_and_get_reads_authority_file()
    print("PASS: list copies and direct get authority")
    test_node_snapshot_invalidation_after_registry_writes()
    print("PASS: node snapshot invalidates after registry writes")
