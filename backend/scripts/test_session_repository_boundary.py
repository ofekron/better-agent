from __future__ import annotations

import ast
import json
import shutil
import tempfile
from pathlib import Path

import _test_home

_PRIMARY_HOME = _test_home.isolate("ba-test-session-repository-")

import paths  # noqa: E402
import session_manager as session_manager_module  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager  # noqa: E402


class RecordingRepository:
    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self.calls: list[str] = []

    def _call(self, name: str, *args, **kwargs):
        self.calls.append(name)
        return getattr(self._delegate, name)(*args, **kwargs)

    def storage_identity(self):
        return self._call("storage_identity")

    def register_writer_guard(self, guard):
        return self._call("register_writer_guard", guard)

    def resolve_root_ids(self, session_ids):
        return self._call("resolve_root_ids", session_ids)

    def root_version(self, session_id: str):
        return self._call("root_version", session_id)

    def read_root(self, session_id: str):
        return self._call("read_root", session_id)

    def copy_persistable_root(self, root: dict):
        return self._call("copy_persistable_root", root)

    def write_root(self, root: dict, **kwargs):
        return self._call("write_root", root, **kwargs)

    def delete_root(self, root_id: str):
        return self._call("delete_root", root_id)


def _assert_no_kernel_fallbacks() -> None:
    source_path = Path(session_manager_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    forbidden = {
        "register_root_writer_guard",
        "_loaded_root_id_for",
        "_resolve_root_id",
        "session_file_fingerprint",
        "get_root_tree",
        "copy_persistable_tree",
        "write_session_full",
        "delete_session",
    }
    found = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "session_store"
        and node.func.attr in forbidden
    }
    assert not found, f"SessionManager bypasses SessionRootRepository: {sorted(found)}"


def main() -> int:
    secondary_home = tempfile.mkdtemp(prefix="ba-test-session-repository-switch-")
    original_repository = manager._root_repository
    original_guard = session_store._root_writer_guard
    manager.flush_pending_persists()
    recording = RecordingRepository(original_repository)
    manager._root_repository = recording
    try:
        _assert_no_kernel_fallbacks()
        assert getattr(original_guard, "__self__", None) is manager

        root = manager.create(
            name="repository-root",
            model="sonnet",
            cwd="/tmp/session-repository",
            orchestration_mode="native",
            source="cli",
        )
        root_id = root["id"]
        manager.reload_root_from_disk(root_id)
        assert manager.get(root_id)["name"] == "repository-root"

        manager.rename(root_id, "repository-live-ref")
        manager.flush_root_persist(root_id)
        manager.reload_root_from_disk(root_id)
        assert manager.get(root_id)["name"] == "repository-live-ref"

        fork = manager.create_sub_session(
            parent_session_id=root_id,
            name="repository-fork",
            provider_id=root.get("provider_id"),
            cwd=root["cwd"],
        )
        root_path = Path(session_store.session_file_path(root_id))
        on_disk = json.loads(root_path.read_text(encoding="utf-8"))
        assert on_disk["_schema_version"] == session_store.SCHEMA_VERSION
        assert on_disk["forks"][0]["id"] == fork["id"]
        assert manager.delete(fork["id"])

        paths.engage_test_home(secondary_home)
        assert manager.get(root_id) is None
        secondary = manager.create(
            name="secondary-home",
            model="sonnet",
            cwd="/tmp/session-repository-secondary",
            orchestration_mode="native",
            source="cli",
        )
        assert manager.get(secondary["id"]) is not None

        paths.engage_test_home(_PRIMARY_HOME)
        assert manager.get(root_id)["name"] == "repository-live-ref"
        assert manager.delete(root_id)

        required_calls = {
            "storage_identity",
            "loaded_root_id_for",
            "resolve_root_id",
            "root_version",
            "read_root",
            "copy_persistable_root",
            "write_root",
            "delete_root",
        }
        assert required_calls <= set(recording.calls), (
            required_calls - set(recording.calls)
        )
        assert session_store._root_writer_guard is original_guard
        print("PASS session root repository boundary")
        return 0
    finally:
        try:
            try:
                manager.flush_pending_persists()
            finally:
                manager._root_repository = original_repository
                paths.engage_test_home(_PRIMARY_HOME)
                manager._ensure_home_current()
        finally:
            shutil.rmtree(secondary_home, ignore_errors=True)
            shutil.rmtree(_PRIMARY_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
