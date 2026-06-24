from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-virtual-sessions-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
import virtual_session_store  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def test_generated_ids_are_namespaced() -> bool:
    session = virtual_session_store.upsert("ofek-dev.test", {"name": "Virtual"})
    expected_prefix = "virtual:ofek-dev.test:"
    if not session["id"].startswith(expected_prefix):
        print(f"  generated id missing namespace: {session['id']}")
        return False
    return virtual_session_store.get(session["id"]) is not None


def test_rejects_real_session_id_shadow() -> bool:
    real = session_store.create_session(
        name="real",
        model="claude-sonnet-4-6",
        cwd="/tmp/project",
        orchestration_mode="native",
        provider_id="claude",
    )
    try:
        virtual_session_store.upsert("ofek-dev.test", {"id": real["id"], "name": "shadow"})
    except ValueError as exc:
        return "extension namespace" in str(exc) or "collides" in str(exc)
    print("  virtual upsert accepted a real session id")
    return False


def test_rejects_other_extension_namespace() -> bool:
    try:
        virtual_session_store.upsert(
            "ofek-dev.test",
            {"id": "virtual:ofek-dev.other:abc", "name": "wrong owner"},
        )
    except ValueError as exc:
        return "extension namespace" in str(exc)
    print("  virtual upsert accepted another extension namespace")
    return False


def test_legacy_bad_ids_do_not_shadow_reads() -> bool:
    real = session_store.create_session(
        name="real 2",
        model="claude-sonnet-4-6",
        cwd="/tmp/project",
        orchestration_mode="native",
        provider_id="claude",
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
    if virtual_session_store.get(real["id"]) is not None:
        print("  legacy invalid virtual id shadowed a real session")
        return False
    return virtual_session_store.list_all() == []


def test_malformed_virtual_prefix_is_rejected() -> bool:
    path = virtual_session_store._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"version":1,"sessions":{"virtual:test":{"id":"virtual:test","name":"bad"}}}',
        encoding="utf-8",
    )
    if virtual_session_store.get("virtual:test") is not None:
        print("  malformed virtual id was readable")
        return False
    return virtual_session_store.list_all() == []


def run_test(name: str, fn) -> bool:
    try:
        ok = fn()
    except Exception as exc:
        print(f"{FAIL} {name}: {exc}")
        return False
    print(f"{PASS if ok else FAIL} {name}")
    return ok


def main() -> int:
    tests = [
        ("generated ids are namespaced", test_generated_ids_are_namespaced),
        ("rejects real session id shadow", test_rejects_real_session_id_shadow),
        ("rejects other extension namespace", test_rejects_other_extension_namespace),
        ("legacy bad ids do not shadow reads", test_legacy_bad_ids_do_not_shadow_reads),
        ("malformed virtual prefix is rejected", test_malformed_virtual_prefix_is_rejected),
    ]
    try:
        return 0 if all(run_test(name, fn) for name, fn in tests) else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
