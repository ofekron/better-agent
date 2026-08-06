from __future__ import annotations

import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate_installed("bc-test-virtual-sessions-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
import virtual_session_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _wipe() -> None:
    """Reset virtual_session_store to a clean slate for the next test.

    virtual_session_store keeps module-global read caches (_cache_data and a
    1s-TTL _summary_cache) that are only invalidated by its own _save. Tests
    that hand-write virtual_sessions.json to simulate legacy/malformed on-disk
    state bypass _save, so the caches must be cleared or later reads return
    stale data. Each test wipes the file and the caches so functions are
    independent and read on-disk truth.
    """
    virtual_session_store._cache_data = None
    virtual_session_store._cache_signature = None
    virtual_session_store._summary_cache = None
    virtual_session_store._summary_cache_signature = None
    virtual_session_store._summary_cache_fresh_until = 0.0
    try:
        virtual_session_store._path().unlink()
    except FileNotFoundError:
        pass


def test_generated_ids_are_namespaced() -> None:
    _wipe()
    session = virtual_session_store.upsert("ofek-dev.test", {"name": "Virtual"})
    assert session["id"].startswith("virtual:ofek-dev.test:")
    assert virtual_session_store.get(session["id"]) is not None


def test_rejects_real_session_id_shadow() -> None:
    _wipe()
    real = session_store.create_session(
        name="real",
        model="claude-sonnet-4-6",
        cwd="/tmp/project",
        orchestration_mode="native",
    )
    try:
        virtual_session_store.upsert("ofek-dev.test", {"id": real["id"], "name": "shadow"})
    except ValueError as exc:
        assert "extension namespace" in str(exc) or "collides" in str(exc)
    else:
        raise AssertionError("virtual upsert accepted a real session id")


def test_rejects_other_extension_namespace() -> None:
    _wipe()
    try:
        virtual_session_store.upsert(
            "ofek-dev.test",
            {"id": "virtual:ofek-dev.other:abc", "name": "wrong owner"},
        )
    except ValueError as exc:
        assert "extension namespace" in str(exc)
    else:
        raise AssertionError("virtual upsert accepted another extension namespace")


def test_legacy_bad_ids_do_not_shadow_reads() -> None:
    _wipe()
    real = session_store.create_session(
        name="real 2",
        model="claude-sonnet-4-6",
        cwd="/tmp/project",
        orchestration_mode="native",
    )
    path = virtual_session_store._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            '{"version":1,"sessions":{'
            f'"{real["id"]}":{{"id":"{real["id"]}","extension_id":"ofek-dev.test","name":"bad"}}'
            "}}"
        ),
        encoding="utf-8",
    )
    assert virtual_session_store.get(real["id"]) is None
    assert virtual_session_store.list_all() == []


def test_malformed_virtual_prefix_is_rejected() -> None:
    _wipe()
    path = virtual_session_store._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"version":1,"sessions":{"virtual:test":{"id":"virtual:test","name":"bad"}}}',
        encoding="utf-8",
    )
    assert virtual_session_store.get("virtual:test") is None
    assert virtual_session_store.list_all() == []


def main() -> int:
    tests = [
        ("generated ids are namespaced", test_generated_ids_are_namespaced),
        ("rejects real session id shadow", test_rejects_real_session_id_shadow),
        ("rejects other extension namespace", test_rejects_other_extension_namespace),
        ("legacy bad ids do not shadow reads", test_legacy_bad_ids_do_not_shadow_reads),
        ("malformed virtual prefix is rejected", test_malformed_virtual_prefix_is_rejected),
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            print(f"{FAIL} {name}: {exc}")
            failures += 1
            continue
        print(f"{PASS} {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
