"""Full unit coverage for hook_store.py.

The store validates/normalizes user-supplied hook config and projects it onto
an atomic JSON file under ba_home()/hooks/hooks.json, with an mtime+size
fingerprint cache. This file covers every normalization branch (id/name/
pattern/command/cwd/timeout/env), the delete + upsert mutators, the load-state
failure modes (missing/malformed/non-dict/wrong-schema/non-list), the fingerprint
OSError path, and the cache hit/miss/isolation contract.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402

_HOME = _test_home.isolate("ba-hook-store-")

import hook_store  # noqa: E402
from json_store import write_json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402


CONFIG_PATH = hook_store._store_path()


@pytest.fixture(autouse=True)
def _reset_cache_and_file():
    """Each test starts from an empty store + cold cache."""
    hook_store._state_cache = None
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
    yield
    hook_store._state_cache = None
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()


def _valid(**overrides) -> dict:
    base = {"id": "h1", "name": "hook one", "pattern": "x.*", "command": ["echo", "hi"]}
    base.update(overrides)
    return base


# --- normalization success ---

def test_normalize_minimal_generates_id_and_defaults():
    hooks = hook_store.replace_hooks([{
        "pattern": "p",
        "command": ["c"],
    }])
    h = hooks[0]
    assert isinstance(h["id"], str) and h["id"]  # uuid generated (line 129)
    assert h["name"] == h["id"]  # name defaults to id
    assert h["enabled"] is True
    assert h["cwd"] is None
    assert h["timeout_seconds"] == hook_store.DEFAULT_TIMEOUT_SECONDS
    assert h["env"] == {}


def test_normalize_explicit_disabled_and_values():
    hooks = hook_store.replace_hooks([{
        "id": "z", "name": "n", "pattern": "p", "command": ["c"],
        "enabled": 0, "cwd": "/tmp", "timeout_seconds": 5, "env": {"K": "v"},
    }])
    h = hooks[0]
    assert h["enabled"] is False
    assert h["cwd"] == "/tmp"
    assert h["timeout_seconds"] == 5.0
    assert h["env"] == {"K": "v"}


def test_normalize_cwd_empty_string_is_none_and_expanduser():
    hooks = hook_store.replace_hooks([{
        "id": "z", "pattern": "p", "command": ["c"], "cwd": "",
    }])
    assert hooks[0]["cwd"] is None  # empty string -> None (line 163)
    hooks = hook_store.replace_hooks([{
        "id": "z", "pattern": "p", "command": ["c"], "cwd": "/tmp/foo/..",
    }])
    assert hooks[0]["cwd"] == str(Path("/tmp/foo/..").expanduser())


# --- normalization failures ---

@pytest.mark.parametrize("raw,expected", [
    (123, "hook\\[0\\] must be an object"),
    ({"id": "", "pattern": "p", "command": ["c"]}, "hook id must be a non-empty string"),
    ({"id": 5, "pattern": "p", "command": ["c"]}, "hook id must be a non-empty string"),
    ({"id": "z", "name": "", "pattern": "p", "command": ["c"]}, "hook name must be a non-empty string"),
    ({"id": "z", "name": 5, "pattern": "p", "command": ["c"]}, "hook name must be a non-empty string"),
    ({"id": "z", "pattern": "", "command": ["c"]}, "hook pattern must be a non-empty string"),
    ({"id": "z", "pattern": "p"}, "hook command must be a non-empty argv list"),
    ({"id": "z", "pattern": "p", "command": []}, "hook command must be a non-empty argv list"),
    ({"id": "z", "pattern": "p", "command": [123]}, "hook command entries must be non-empty strings"),
    ({"id": "z", "pattern": "p", "command": [""]}, "hook command entries must be non-empty strings"),
    ({"id": "z", "pattern": "p", "command": ["a\x00b"]}, "hook command entries cannot contain NUL bytes"),
])
def test_normalize_failures(raw, expected):
    with pytest.raises(hook_store.HookConfigError, match=expected):
        hook_store.replace_hooks([raw])


def test_normalize_cwd_failures():
    for raw, expected in [
        ({"id": "z", "pattern": "p", "command": ["c"], "cwd": 5}, "non-empty string when supplied"),
        ({"id": "z", "pattern": "p", "command": ["c"], "cwd": "   "}, "non-empty string when supplied"),
        ({"id": "z", "pattern": "p", "command": ["c"], "cwd": "relative/path"}, "must be absolute"),
    ]:
        with pytest.raises(hook_store.HookConfigError, match=expected):
            hook_store.replace_hooks([raw])


def test_normalize_timeout_failures():
    for raw, expected in [
        ({"id": "z", "pattern": "p", "command": ["c"], "timeout_seconds": "x"}, "must be a number"),
        ({"id": "z", "pattern": "p", "command": ["c"], "timeout_seconds": True}, "must be a number"),
        ({"id": "z", "pattern": "p", "command": ["c"], "timeout_seconds": 0}, "must be > 0"),
        ({"id": "z", "pattern": "p", "command": ["c"], "timeout_seconds": -1}, "must be > 0"),
        ({"id": "z", "pattern": "p", "command": ["c"], "timeout_seconds": 301}, "must be > 0"),
    ]:
        with pytest.raises(hook_store.HookConfigError, match=expected):
            hook_store.replace_hooks([raw])


def test_normalize_env_failures():
    base = {"id": "z", "pattern": "p", "command": ["c"]}
    for env, expected in [
        (["a"], "hook env must be an object"),
        ({"": "v"}, "env keys must be non-empty strings"),
        ({1: "v"}, "env keys must be non-empty strings"),
        ({"K=V": "x"}, "env keys cannot contain"),
        ({"K\x00": "x"}, "env keys cannot contain"),
        ({"K": 5}, "env values must be strings"),
        ({"K": "v\x00"}, "env values cannot contain NUL bytes"),
    ]:
        with pytest.raises(hook_store.HookConfigError, match=expected):
            hook_store.replace_hooks([{**base, "env": env}])


def test_duplicate_ids_rejected():
    with pytest.raises(hook_store.HookConfigError, match="duplicate hook id 'dup'"):
        hook_store.replace_hooks([_valid(id="dup"), _valid(id="dup")])


# --- load-state failure modes ---

def test_load_missing_returns_empty():
    assert hook_store.list_hooks() == []


def test_load_malformed_fails_closed():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text("{broken", encoding="utf-8")
    with pytest.raises(hook_store.HookConfigError, match="failed to parse"):
        hook_store.list_hooks()


def test_load_non_object_fails_closed():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(hook_store.HookConfigError, match="must be an object"):
        hook_store.list_hooks()


def test_load_wrong_schema_fails_closed():
    write_json(CONFIG_PATH, {"schema_version": 99, "hooks": []})
    with pytest.raises(hook_store.HookConfigError, match="unsupported hooks schema_version"):
        hook_store.list_hooks()


def test_load_hooks_not_list_fails_closed():
    write_json(CONFIG_PATH, {"schema_version": 1, "hooks": {"a": 1}})
    with pytest.raises(hook_store.HookConfigError, match="hooks must be a list"):
        hook_store.list_hooks()


def test_load_valid_cold_cache_normalizes_and_caches():
    # Written directly (bypassing replace_hooks) so the in-memory cache is cold:
    # exercises _load_state's parse -> normalize -> assert-unique -> cache-store path.
    write_json(CONFIG_PATH, {
        "schema_version": 1,
        "hooks": [{"id": "a", "pattern": "p", "command": ["c"]}],
    })
    hooks = hook_store.list_hooks()
    assert hooks[0]["name"] == "a"  # name defaulted from id during load normalization
    # The loaded state is now cached by fingerprint.
    assert hook_store._state_cache is not None
    assert hook_store._state_cache[0] == hook_store._store_fingerprint()


# --- mutators ---

def test_upsert_replaces_existing_by_id():
    hook_store.replace_hooks([_valid(id="a"), _valid(id="b")])
    result = hook_store.upsert_hook({"id": "a", "name": "renamed", "pattern": "p", "command": ["c"]})
    assert result["name"] == "renamed"
    hooks = hook_store.list_hooks()
    assert [h["id"] for h in hooks] == ["b", "a"]
    assert len(hooks) == 2


def test_delete_invalid_id_raises():
    with pytest.raises(hook_store.HookConfigError):
        hook_store.delete_hook("")
    with pytest.raises(hook_store.HookConfigError):
        hook_store.delete_hook("   ")
    with pytest.raises(hook_store.HookConfigError):
        hook_store.delete_hook(123)  # type: ignore[arg-type]


def test_delete_no_match_returns_false():
    hook_store.replace_hooks([_valid(id="a")])
    assert hook_store.delete_hook("absent") is False


def test_delete_match_removes_and_returns_true():
    hook_store.replace_hooks([_valid(id="a"), _valid(id="b")])
    assert hook_store.delete_hook("a") is True
    assert [h["id"] for h in hook_store.list_hooks()] == ["b"]


# --- cache contract ---

def test_fingerprint_oserror_returns_zero(monkeypatch):
    # A non-existent path makes stat() raise OSError -> (0, 0) sentinel.
    monkeypatch.setattr(hook_store, "_store_path", lambda: Path("/nonexistent/hooks.json"))
    assert hook_store._store_fingerprint() == (0, 0)


def test_cache_hit_avoids_reread_and_isolates_callers():
    hook_store.replace_hooks([_valid()])
    # Prime the cache by loading once.
    first = hook_store.list_hooks()
    original_loads = hook_store.json.loads
    counts = {"n": 0}

    def counting_loads(*a, **k):
        counts["n"] += 1
        return original_loads(*a, **k)

    hook_store.json.loads = counting_loads
    try:
        second = hook_store.list_hooks()
        third = hook_store.list_hooks()
        assert counts["n"] == 0  # fingerprint matched cache, no disk parse
    finally:
        hook_store.json.loads = original_loads
    # Returned objects are deep copies: mutating one does not poison the cache.
    first[0]["name"] = "tampered"
    assert second[0]["name"] != "tampered"
    assert third[0]["name"] != "tampered"


def test_write_invalidates_cache_for_next_read():
    hook_store.replace_hooks([_valid(id="a", name="orig")])
    assert hook_store.list_hooks()[0]["name"] == "orig"
    hook_store.replace_hooks([_valid(id="a", name="next")])
    assert hook_store.list_hooks()[0]["name"] == "next"
