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


def test_hook_lists_skip_runtime_ready_without_requested_hook() -> None:
    original_list = extension_store.list_extensions
    original_active = extension_store._record_active
    original_ready = extension_store._record_runtime_ready
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

        extension_store._record_runtime_ready = ready

        assert extension_store.post_turn_hooks() == []
        assert ready_calls == []
    finally:
        extension_store.list_extensions = original_list
        extension_store._record_active = original_active
        extension_store._record_runtime_ready = original_ready


def test_hook_lists_check_runtime_ready_for_requested_hook() -> None:
    original_list = extension_store.list_extensions
    original_active = extension_store._record_active
    original_ready = extension_store._record_runtime_ready
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

        extension_store._record_runtime_ready = ready

        assert extension_store.post_turn_hooks() == [("post", "hooks/post.py")]
        assert extension_store.pre_turn_hooks() == []
        assert extension_store.session_event_hooks() == [("session", "hooks/session.py")]
        assert ready_calls == ["post", "pre", "session"]
    finally:
        extension_store.list_extensions = original_list
        extension_store._record_active = original_active
        extension_store._record_runtime_ready = original_ready


def test_hook_lists_filter_extension_without_backend_routes() -> None:
    """A hook-bearing extension whose backend spec is unresolvable (no
    ``backend_routes`` permission) must be filtered out of every hook fan-out.
    Otherwise each fan-out 404s via ``backend_entrypoint_spec`` and logs a
    traceback on the hot path (regression: ofek-dev.usage pre_send_advisory)."""
    original_list = extension_store.list_extensions
    original_active = extension_store._record_active
    original_ready = extension_store._record_runtime_ready
    original_has_permission = extension_store.has_permission
    try:
        extension_store.list_extensions = lambda: [
            _record("gated", {"pre_send_advisory": "/pre-send-advisory"}),
            _record("live", {"pre_send_advisory": "/pre-send-advisory"}),
        ]
        extension_store._record_active = lambda record: True
        extension_store._record_runtime_ready = lambda record: True

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
        extension_store._record_runtime_ready = original_ready
        extension_store.has_permission = original_has_permission


if __name__ == "__main__":
    test_hook_lists_skip_runtime_ready_without_requested_hook()
    test_hook_lists_check_runtime_ready_for_requested_hook()
    test_hook_lists_filter_extension_without_backend_routes()
    print("ok")
