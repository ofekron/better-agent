from __future__ import annotations

import os
import sys

import _test_home

_test_home.isolate("bc-test-extension-hook-hot-path-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_store  # noqa: E402


def _record(extension_id: str, hooks: dict[str, str] | None = None) -> dict:
    return {
        "manifest": {
            "id": extension_id,
            "entrypoints": {"hooks": hooks or {}},
            "permissions": {"backend_routes": True},
        }
    }


def test_startup_schedules_readiness_refresh_without_awaiting_it() -> None:
    source = (extension_store.Path(extension_store.__file__).with_name("main.py")).read_text(
        encoding="utf-8",
    )
    refresh_await = (
        "await asyncio.to_thread("
        "extension_store.refresh_runtime_readiness_projection)"
    )
    assert source.count(refresh_await) == 1
    create_at = source.index('name="extension-readiness-refresher"')
    refresher_at = source.index("async def _extension_readiness_refresher")
    assert refresher_at < source.index(refresh_await) < create_at


def test_hook_lists_skip_runtime_ready_without_requested_hook() -> None:
    original_list = extension_store.list_extensions
    original_active = extension_store._record_active
    original_ready = extension_store._record_runtime_ready_projected
    ready_calls: list[str] = []
    try:
        extension_store.list_extensions = lambda: [
            _record("no-hooks"),
            _record("other-hook", {"pre_turn": "hooks/pre.py"}),
        ]
        extension_store._record_active = lambda record: True

        def ready(record: dict) -> bool:
            ready_calls.append(record["manifest"]["id"])
            return True

        extension_store._record_runtime_ready_projected = ready

        assert extension_store.post_turn_hooks() == []
        assert ready_calls == []
    finally:
        extension_store.list_extensions = original_list
        extension_store._record_active = original_active
        extension_store._record_runtime_ready_projected = original_ready


def test_hook_lists_check_runtime_ready_for_requested_hook() -> None:
    original_list = extension_store.list_extensions
    original_active = extension_store._record_active
    original_ready = extension_store._record_runtime_ready_projected
    ready_calls: list[str] = []
    try:
        extension_store.list_extensions = lambda: [
            _record("post", {"post_turn": "hooks/post.py"}),
            _record("pre", {"pre_turn": "hooks/pre.py"}),
            _record("session", {"session_event": "hooks/session.py"}),
        ]
        extension_store._record_active = lambda record: True

        def ready(record: dict) -> bool:
            ready_calls.append(record["manifest"]["id"])
            return record["manifest"]["id"] != "pre"

        extension_store._record_runtime_ready_projected = ready

        assert extension_store.post_turn_hooks() == [("post", "hooks/post.py")]
        assert extension_store.pre_turn_hooks() == []
        assert extension_store.session_event_hooks() == [("session", "hooks/session.py")]
        assert ready_calls == ["post", "pre", "session"]
    finally:
        extension_store.list_extensions = original_list
        extension_store._record_active = original_active
        extension_store._record_runtime_ready_projected = original_ready


def test_hook_lists_filter_extension_without_backend_routes() -> None:
    """A hook-bearing extension whose backend spec is unresolvable (no
    ``backend_routes`` permission) must be filtered out of every hook fan-out.
    Otherwise each fan-out 404s via ``backend_entrypoint_spec`` and logs a
    traceback on the hot path (regression: ofek-dev.usage pre_send_advisory)."""
    original_list = extension_store.list_extensions
    original_active = extension_store._record_active
    original_ready = extension_store._record_runtime_ready_projected
    original_has_permission = extension_store.has_permission
    try:
        extension_store.list_extensions = lambda: [
            _record("gated", {"pre_send_advisory": "/pre-send-advisory"}),
            _record("live", {"pre_send_advisory": "/pre-send-advisory"}),
        ]
        extension_store._record_active = lambda record: True
        extension_store._record_runtime_ready_projected = lambda record: True

        def has_perm(record: dict, permission: str) -> bool:
            if permission != "backend_routes":
                return True
            # "gated" lacks backend_routes (spec -> None -> 404); "live" has it.
            return record["manifest"]["id"] != "gated"

        extension_store.has_permission = has_perm

        assert extension_store.pre_send_advisory_hooks() == [("live", "/pre-send-advisory")]
    finally:
        extension_store.list_extensions = original_list
        extension_store._record_active = original_active
        extension_store._record_runtime_ready_projected = original_ready
        extension_store.has_permission = original_has_permission


def test_persisted_smoke_projection_does_not_touch_filesystem() -> None:
    record = _record("projected", {"pre_turn": "hooks/pre.py"})
    record["manifest"]["protocol"] = {
        "version": 1,
        "smoke_test": {"required_paths": ["backend.py"], "python_modules": []},
    }
    record["smoke_test"] = {
        "status": "passed", "protocol_version": 1,
        "required_paths": ["backend.py"], "python_modules": [],
    }
    original_resolve = extension_store.Path.resolve
    original_exists = extension_store.Path.exists
    try:
        extension_store._RUNTIME_READY_PROJECTION["projected"] = True
        extension_store.Path.resolve = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runtime readiness touched resolve")
        )
        extension_store.Path.exists = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runtime readiness touched exists")
        )
        assert extension_store._record_runtime_ready_projected(record)
    finally:
        extension_store._RUNTIME_READY_PROJECTION.pop("projected", None)
        extension_store.Path.resolve = original_resolve
        extension_store.Path.exists = original_exists


def test_verified_projection_fails_closed_after_runtime_file_removed(tmp_path=None) -> None:
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="ready-projection-"))
    marker = root / "backend.py"
    marker.write_text("ok", encoding="utf-8")
    record = _record("projected-delete", {"pre_turn": "hooks/pre.py"})
    record["source"] = {"install_path": str(root)}
    record["manifest"]["protocol"] = {
        "version": 1,
        "smoke_test": {"required_paths": ["backend.py"], "python_modules": []},
    }
    record["smoke_test"] = {
        "status": "passed", "protocol_version": 1,
        "required_paths": ["backend.py"], "python_modules": [],
    }
    original_list = extension_store.list_extensions
    original_active = extension_store._record_active
    try:
        extension_store.list_extensions = lambda: [record]
        extension_store._record_active = lambda _record: True
        assert extension_store.refresh_runtime_readiness_projection()["projected-delete"]
        marker.write_text("corrupt", encoding="utf-8")
        assert not extension_store.refresh_runtime_readiness_projection()["projected-delete"]
        marker.unlink()
        assert not extension_store.refresh_runtime_readiness_projection()["projected-delete"]
        assert not extension_store._record_runtime_ready_projected(record)
    finally:
        extension_store.list_extensions = original_list
        extension_store._record_active = original_active
        extension_store._RUNTIME_READY_PROJECTION.pop("projected-delete", None)
        extension_store._RUNTIME_PACKAGE_FINGERPRINTS.pop("projected-delete", None)


def test_projection_fingerprints_python_module_outside_required_paths() -> None:
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="ready-module-projection-"))
    module = root / "worker.py"
    module.write_text("VALUE = 1", encoding="utf-8")
    (root / "better-agent-extension.json").write_text("{}", encoding="utf-8")
    record = _record("projected-module", {"pre_turn": "hooks/pre.py"})
    record["source"] = {"install_path": str(root)}
    record["manifest"]["entrypoints"]["backend_module"] = "worker"
    record["manifest"]["protocol"] = {
        "version": 1,
        "smoke_test": {
            "required_paths": ["better-agent-extension.json"],
            "python_modules": ["worker"],
        },
    }
    record["smoke_test"] = {
        "status": "passed", "protocol_version": 1,
        "required_paths": ["better-agent-extension.json"],
        "python_modules": ["worker"],
    }
    original_list = extension_store.list_extensions
    original_active = extension_store._record_active
    try:
        extension_store.list_extensions = lambda: [record]
        extension_store._record_active = lambda _record: True
        assert extension_store.refresh_runtime_readiness_projection()["projected-module"]
        module.write_text("VALUE = 2", encoding="utf-8")
        assert not extension_store.refresh_runtime_readiness_projection()["projected-module"]
        module.unlink()
        assert not extension_store.refresh_runtime_readiness_projection()["projected-module"]
    finally:
        extension_store.list_extensions = original_list
        extension_store._record_active = original_active
        extension_store._RUNTIME_READY_PROJECTION.pop("projected-module", None)
        extension_store._RUNTIME_PACKAGE_FINGERPRINTS.pop("projected-module", None)


if __name__ == "__main__":
    test_startup_schedules_readiness_refresh_without_awaiting_it()
    test_hook_lists_skip_runtime_ready_without_requested_hook()
    test_hook_lists_check_runtime_ready_for_requested_hook()
    test_hook_lists_filter_extension_without_backend_routes()
    test_persisted_smoke_projection_does_not_touch_filesystem()
    test_verified_projection_fails_closed_after_runtime_file_removed()
    test_projection_fingerprints_python_module_outside_required_paths()
    print("ok")
