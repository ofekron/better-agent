"""Full unit coverage for node_registry_store.py — complements
test_node_registry_store_cache.py (which covers the warm-cache / projection
paths) by exercising the error, schema-mismatch, invalidation, and argon2
verify branches that the cache test never reaches.

Every test isolates a fresh BETTER_AGENT_HOME via `_test_home.isolate` and
resets the on-disk registry + in-memory cache at entry, so co-collection
with the other node test files cannot leak state.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402

_HOME = _test_home.isolate("ba-node-registry-store-")

import node_registry_store  # noqa: E402

_GOOD_HASH = node_registry_store.hash_secret("s3cret")  # real argon2 hash once


def _registry_dir() -> Path:
    # Route through the module's own resolver (ba_home()) so writes always
    # land in the same home reads use — a captured import-time `_HOME` would
    # drift under co-collection (env var is last-import-wins).
    path = node_registry_store._dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _version_path() -> Path:
    return node_registry_store._version_path()


def _write_file(name: str, content: str) -> None:
    (_registry_dir() / name).write_text(content, encoding="utf-8")


def _write_record(
    node_id: str,
    *,
    schema_version: int = node_registry_store.SCHEMA_VERSION,
    secret_hash: str = _GOOD_HASH,
    cwd_roots: list[str] | None = None,
    approved_at: str = "2026-01-01T00:00:00",
) -> None:
    rec = {
        "schema_version": schema_version,
        "node_id": node_id,
        "address": "ws://x",
        "cwd_roots": ["/tmp"] if cwd_roots is None else cwd_roots,
        "secret_hash": secret_hash,
        "approved_at": approved_at,
    }
    _write_file(f"{node_id}.json", json.dumps(rec))


def _reset() -> None:
    """Clear on-disk registry + version file + the in-memory cache."""
    d = _registry_dir()
    for p in d.glob("*.json"):
        p.unlink()
    if _version_path().exists():
        _version_path().unlink()
    node_registry_store._reset_cache_for_tests()


# --- _path / id validation --------------------------------------------------


def test_path_rejects_invalid_node_id() -> None:
    _reset()
    # Invalid id (space) fails the regex → ValueError at _path.
    raised = False
    try:
        node_registry_store._path("bad id!")
    except ValueError:
        raised = True
    assert raised
    # `add` does not catch it either — it propagates.
    try:
        node_registry_store.add(
            node_id="bad id!", address="ws://x", cwd_roots=["/t"], secret_hash="h"
        )
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


# --- _copy_record branches -------------------------------------------------


def test_copy_record_without_list_cwd_roots() -> None:
    _reset()
    # cwd_roots absent → not a list → left untouched (branch False).
    out = node_registry_store._copy_record({"node_id": "x"})
    assert out == {"node_id": "x"}
    # cwd_roots present as a list → deep-copied (branch True).
    out2 = node_registry_store._copy_record({"node_id": "y", "cwd_roots": ["/a"]})
    assert out2["cwd_roots"] == ["/a"]
    out2["cwd_roots"].append("/b")
    assert out2["cwd_roots"] == ["/a", "/b"]


# --- _rebuild_cache_locked error paths -------------------------------------


def test_rebuild_swallows_iter_oserror(monkeypatch) -> None:
    _reset()
    _write_record("node-1")

    def boom():
        raise OSError("io")

    monkeypatch.setattr(node_registry_store, "_iter_registry_paths", boom)
    # OSError → paths=[] → empty cache, no raise.
    assert node_registry_store.list_all() == []


def test_rebuild_skips_unparseable_nondict_and_wrong_schema() -> None:
    _reset()
    _write_record("node-1")
    _write_file("broken.json", "{not json")  # JSONDecodeError → skip
    _write_file("list.json", "[1, 2, 3]")  # non-dict → skip
    _write_file(
        "stale.json", json.dumps({"schema_version": 999, "node_id": "stale"})
    )  # wrong schema → skip
    listed = node_registry_store.list_all()
    assert [r["node_id"] for r in listed] == ["node-1"]


# --- _read_generation_locked branches --------------------------------------


def test_read_generation_locked_malformed_nondict_and_invalid() -> None:
    _reset()
    # No file → 0.
    assert node_registry_store._read_generation_locked() == 0
    # Malformed JSON → 0.
    _version_path().write_text("{bad", encoding="utf-8")
    assert node_registry_store._read_generation_locked() == 0
    # Non-dict JSON → 0.
    _version_path().write_text("[1]", encoding="utf-8")
    assert node_registry_store._read_generation_locked() == 0
    # generation present but negative → 0.
    _version_path().write_text(json.dumps({"generation": -5}), encoding="utf-8")
    assert node_registry_store._read_generation_locked() == 0
    # generation present but non-int → 0.
    _version_path().write_text(json.dumps({"generation": "x"}), encoding="utf-8")
    assert node_registry_store._read_generation_locked() == 0
    # Valid generation → returned as-is.
    _version_path().write_text(json.dumps({"generation": 7}), encoding="utf-8")
    assert node_registry_store._read_generation_locked() == 7


# --- _set_cached_record_locked guard ---------------------------------------


def test_set_cached_record_skips_non_str_node_id() -> None:
    _reset()
    # Warm the cache with a valid record so the warm path is live.
    _write_record("node-1")
    node_registry_store.list_all()
    # A record whose node_id is not a str is ignored by the cache setter.
    with node_registry_store._cache_lock:
        node_registry_store._set_cached_record_locked({"node_id": 123})
    assert [r["node_id"] for r in node_registry_store.list_all()] == ["node-1"]


# --- add / remove cache-not-loaded branches --------------------------------


def test_add_writes_when_cache_not_loaded() -> None:
    _reset()
    # Cache is cold (not loaded) → add must still persist + bump generation
    # without touching the warm-cache setter.
    rec = node_registry_store.add(
        node_id="node-1", address="ws://a", cwd_roots=["/t"], secret_hash="h"
    )
    assert rec["node_id"] == "node-1"
    assert rec["schema_version"] == node_registry_store.SCHEMA_VERSION
    assert rec["cwd_roots"] == ["/t"]
    # On-disk file exists and is readable via the authority path.
    assert node_registry_store.get("node-1")["address"] == "ws://a"


def test_add_updates_warm_cache_and_keeps_sorted() -> None:
    _reset()
    _write_record("node-1", approved_at="2026-01-01T00:00:00")
    # Warm the cache, then add while it is loaded → exercises the warm-cache
    # setter + the re-sort path.
    assert [r["node_id"] for r in node_registry_store.list_all()] == ["node-1"]
    node_registry_store.add(
        node_id="node-2", address="ws://two", cwd_roots=["/t"], secret_hash="h"
    )
    ids = [r["node_id"] for r in node_registry_store.list_all()]
    assert set(ids) == {"node-1", "node-2"}


def test_remove_when_cache_not_loaded_and_absent_and_invalid() -> None:
    _reset()
    _write_record("node-1")
    # remove with a cold cache (no list_all first).
    assert node_registry_store.remove("node-1") is True
    # Now absent → False (path.exists() False branch).
    assert node_registry_store.remove("node-1") is False
    # Invalid id → _path ValueError → False.
    assert node_registry_store.remove("bad id!") is False


def test_remove_swallows_unlink_oserror(monkeypatch) -> None:
    _reset()
    _write_record("node-1")

    def raising_unlink(self, *a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(pathlib.Path, "unlink", raising_unlink)
    assert node_registry_store.remove("node-1") is False


def test_remove_updates_warm_cache() -> None:
    _reset()
    _write_record("node-1")
    _write_record("node-2")
    # Warm the cache, then remove while loaded → exercises the warm-cache
    # pop + re-sort path.
    assert {r["node_id"] for r in node_registry_store.list_all()} == {
        "node-1",
        "node-2",
    }
    assert node_registry_store.remove("node-1") is True
    assert [r["node_id"] for r in node_registry_store.list_all()] == ["node-2"]


def test_iter_registry_paths_returns_empty_when_dir_absent() -> None:
    _reset()
    # Remove the registry dir entirely → _iter_registry_paths short-circuits.
    d = _registry_dir()
    shutil.rmtree(d)
    assert not d.exists()
    assert node_registry_store._iter_registry_paths() == []
    # And a cold rebuild over the absent dir yields an empty list (no raise).
    node_registry_store._reset_cache_for_tests()
    assert node_registry_store.list_all() == []


# --- get error branches -----------------------------------------------------


def test_get_invalid_malformed_and_wrong_schema() -> None:
    _reset()
    # Invalid id → ValueError caught → None.
    assert node_registry_store.get("bad id!") is None
    # Absent → None.
    assert node_registry_store.get("absent") is None
    # Malformed JSON → None.
    _write_file("broken.json", "{not json")
    assert node_registry_store.get("broken") is None
    # Wrong schema_version → warning + None.
    _write_file(
        "stale.json", json.dumps({"schema_version": 999, "node_id": "stale"})
    )
    assert node_registry_store.get("stale") is None
    # Good record → returned.
    _write_record("node-1")
    assert node_registry_store.get("node-1")["node_id"] == "node-1"


# --- verify_secret ----------------------------------------------------------


def test_verify_secret_unknown_wrong_and_correct() -> None:
    _reset()
    # Unknown node → False (rec is None branch).
    assert node_registry_store.verify_secret("absent", "x") is False
    # Correct secret → True.
    _write_record("node-1", secret_hash=_GOOD_HASH)
    assert node_registry_store.verify_secret("node-1", "s3cret") is True
    # Wrong secret → argon2 mismatch → False.
    assert node_registry_store.verify_secret("node-1", "wrong") is False
    # Stored hash garbage → InvalidHashError / Exception → False.
    _write_record("node-2", secret_hash="not-a-hash")
    assert node_registry_store.verify_secret("node-2", "x") is False


def test_hash_secret_is_argon2() -> None:
    h = node_registry_store.hash_secret("pw")
    assert h.startswith("$argon2")


# --- spec projection --------------------------------------------------------


def test_to_spec_known_and_unknown_and_partial_record() -> None:
    _reset()
    # Unknown → None.
    assert node_registry_store.to_spec("absent") is None
    _write_record("node-1")
    spec = node_registry_store.to_spec("node-1")
    assert spec is not None
    assert spec.id == "node-1"
    assert spec.role == "worker_node"
    assert spec.address == "ws://x"
    assert tuple(spec.cwd_roots) == ("/tmp",)
    # spec_from_record tolerates missing fields (`.get(...)` defaults).
    partial = node_registry_store.spec_from_record({"node_id": "p"})
    assert partial.id == "p"
    assert partial.address == ""
    assert partial.cwd_roots == ()


# --- version_token syncs from disk ------------------------------------------


def test_version_token_picks_up_external_generation_bump() -> None:
    _reset()
    base = node_registry_store.version_token()
    # Simulate another process writing a newer generation file.
    _version_path().write_text(json.dumps({"generation": base[0] + 3}), encoding="utf-8")
    assert node_registry_store.version_token()[0] == base[0] + 3


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
