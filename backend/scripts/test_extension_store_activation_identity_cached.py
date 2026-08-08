"""Regression: extension_store.activation_identity must be fingerprint-cached
so the per-extension-backend-request auth chain
(extension_backend_loader._invoke_backend) doesn't take the cross-process
fcntl.flock + disk read on the event loop.

`activation_identity` was added later (commit b33a9a918) without being wired
into the fingerprint cache that `get_extension`/`is_extension_enabled_cached`
already use (commit 9105ef966, added because `_store_lock` was the #3
event-loop blocker). It called `_load()` directly on every call. This pins
the fix: a warm read never re-enters `_load()`, and a writer that rotates
`activation_id` is observed on the very next call — the generation fence
`_record_backend_incident` relies on to refuse stale-activation attribution
stays correct.
"""
from __future__ import annotations

import json
import os
import sys

import _test_home
_test_home.isolate("bc-test-ext-activation-cached-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_store  # noqa: E402


def _write_store(extensions: dict) -> None:
    path = extension_store._store_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": extension_store.STORE_SCHEMA_VERSION,
                "extensions": extensions,
                "deleted_extensions": {},
            }
        ),
        encoding="utf-8",
    )


def _record(ext_id: str, *, enabled: bool = True, activation_id: str = "a" * 32) -> dict:
    return {
        "manifest": {"id": ext_id, "permissions": {}},
        "enabled": enabled,
        "activation_id": activation_id,
        "entitlement": {},
    }


def _rotate_via_real_writer(extension_id: str) -> str:
    """Rotate activation_id through the real writer path (_load -> mutate ->
    _save -> _write_store_unlocked), exactly like every production rotation
    site (set_enabled, cohort auto-activation, reinstall, health
    auto-disable, reconcile backfill). _write_store_unlocked always calls
    _clear_projection_cache(), which is how real writers stay ahead of
    store_fingerprint's TTL window — unlike a raw file write."""
    data = extension_store._load()
    record = data["extensions"][extension_id]
    new_activation_id = extension_store._rotate_activation_identity(record)
    extension_store._save(data)
    return new_activation_id


class _LoadCounter:
    """Wrap the uncached extension_store._load() reader to count calls."""

    def __init__(self) -> None:
        self.count = 0
        self._real = extension_store._load

    def __enter__(self):
        extension_store._load = self._counting
        return self

    def __exit__(self, *a):
        extension_store._load = self._real

    def _counting(self):
        self.count += 1
        return self._real()


def test_activation_identity_does_not_reenter_load_when_store_unchanged() -> None:
    extension_store._clear_projection_cache()
    _write_store({"a": _record("a", activation_id="a" * 32)})

    with _LoadCounter() as lc:
        first = extension_store.activation_identity("a")
        assert first == "a" * 32
        assert lc.count == 1, f"cold read takes _load() once, got {lc.count}"

        for _ in range(5):
            again = extension_store.activation_identity("a")
            assert again == "a" * 32
        assert lc.count == 1, (
            f"repeated calls against an unchanged store must not re-enter "
            f"_load(), got {lc.count} calls"
        )


def test_activation_identity_reflects_rotation_after_writer_rotates_it() -> None:
    extension_store._clear_projection_cache()
    _write_store({"a": _record("a", activation_id="a" * 32)})
    first_id = extension_store.activation_identity("a")
    assert first_id == "a" * 32

    new_id = _rotate_via_real_writer("a")
    assert new_id != first_id
    assert extension_store.activation_identity("a") == new_id, (
        "a writer rotating activation_id must be observed on the next call "
        "(fingerprint cache must not serve a stale generation fence)"
    )


def test_activation_identity_disabled_or_missing_returns_empty() -> None:
    extension_store._clear_projection_cache()
    _write_store({"a": _record("a", enabled=False, activation_id="a" * 32)})
    assert extension_store.activation_identity("a") == ""
    assert extension_store.activation_identity("missing") == ""


if __name__ == "__main__":
    test_activation_identity_does_not_reenter_load_when_store_unchanged()
    test_activation_identity_reflects_rotation_after_writer_rotates_it()
    test_activation_identity_disabled_or_missing_returns_empty()
    print("PASS extension_store activation_identity cached")
