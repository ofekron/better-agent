from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_test_home.isolate("bc-test-extension-hook-hot-path-")

import extension_store  # noqa: E402
import extension_integrity  # noqa: E402


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


def test_integrity_worker_detects_metadata_spoof_and_symlink(tmp_path=None) -> None:
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="ready-integrity-"))
    marker = root / "backend.py"
    marker.write_bytes(b"original")
    spec = {"root": str(root), "relative_paths": ["backend.py"], "modules": []}
    baseline = extension_integrity.fingerprint_package(spec)["digest"]
    original_stat = marker.stat()
    marker.write_bytes(b"tampered")
    os.utime(marker, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert extension_integrity.fingerprint_package(spec)["digest"] != baseline
    marker.unlink()
    target = root / "target.py"
    target.write_bytes(b"original")
    marker.symlink_to(target)
    assert extension_integrity.fingerprint_package(spec)["digest"] is None
    marker.unlink()
    directory_target = root / "directory-target"
    directory_target.mkdir()
    (directory_target / "nested.py").write_text("nested", encoding="utf-8")
    directory_link = root / "directory-link"
    directory_link.symlink_to(directory_target, target_is_directory=True)
    directory_spec = {"root": str(root), "relative_paths": ["directory-link"], "modules": []}
    assert extension_integrity.fingerprint_package(directory_spec)["digest"] is None


def test_concurrent_refreshes_share_one_integrity_scan() -> None:
    import concurrent.futures
    import tempfile
    import threading
    import time
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="ready-singleflight-"))
    marker = root / "backend.py"
    marker.write_bytes(b"x" * (8 * 1024 * 1024))
    record = _record("singleflight")
    record["source"] = {"install_path": str(root)}
    record["manifest"]["protocol"] = {
        "version": 1,
        "smoke_test": {"required_paths": ["backend.py"], "python_modules": []},
    }
    original_list = extension_store.list_extensions
    original_active = extension_store._record_active
    original_fingerprint = extension_store._runtime_package_fingerprints
    calls = 0
    call_lock = threading.Lock()

    def measured(candidates: list[dict]) -> list[str | None]:
        nonlocal calls
        with call_lock:
            calls += 1
        time.sleep(0.05)
        return original_fingerprint(candidates)

    try:
        extension_store.list_extensions = lambda: [record]
        extension_store._record_active = lambda _record: True
        extension_store._runtime_package_fingerprints = measured
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _item: extension_store.refresh_runtime_readiness_projection(), range(8)))
        assert all(result["singleflight"] for result in results)
        assert calls == 1
    finally:
        extension_store.list_extensions = original_list
        extension_store._record_active = original_active
        extension_store._runtime_package_fingerprints = original_fingerprint
        extension_store._RUNTIME_READY_PROJECTION.pop("singleflight", None)
        extension_store._RUNTIME_PACKAGE_FINGERPRINTS.pop("singleflight", None)


def test_first_integrity_failure_is_not_runtime_ready() -> None:
    record = _record("unverified")
    original_list = extension_store.list_extensions
    original_active = extension_store._record_active
    original_fingerprint = extension_store._runtime_package_fingerprints
    try:
        extension_store.list_extensions = lambda: [record]
        extension_store._record_active = lambda _record: True
        extension_store._runtime_package_fingerprints = lambda records: [None for _ in records]
        assert not extension_store.refresh_runtime_readiness_projection()["unverified"]
    finally:
        extension_store.list_extensions = original_list
        extension_store._record_active = original_active
        extension_store._runtime_package_fingerprints = original_fingerprint
        extension_store._RUNTIME_READY_PROJECTION.pop("unverified", None)
        extension_store._RUNTIME_PACKAGE_FINGERPRINTS.pop("unverified", None)


def test_persisted_install_digest_is_authoritative_on_first_refresh() -> None:
    record = _record("persisted-digest")
    record["smoke_test"] = {"runtime_package_sha256": "expected"}
    original_list = extension_store.list_extensions
    original_active = extension_store._record_active
    original_fingerprint = extension_store._runtime_package_fingerprints
    try:
        extension_store.list_extensions = lambda: [record]
        extension_store._record_active = lambda _record: True
        extension_store._runtime_package_fingerprints = lambda records: ["tampered" for _ in records]
        assert not extension_store.refresh_runtime_readiness_projection()["persisted-digest"]
    finally:
        extension_store.list_extensions = original_list
        extension_store._record_active = original_active
        extension_store._runtime_package_fingerprints = original_fingerprint
        extension_store._RUNTIME_READY_PROJECTION.pop("persisted-digest", None)
        extension_store._RUNTIME_PACKAGE_FINGERPRINTS.pop("persisted-digest", None)


def test_store_mutation_during_scan_retries_before_publish() -> None:
    original_list = extension_store.list_extensions
    original_build = extension_store._build_runtime_readiness_projection
    original_store_fingerprint = extension_store._refresh_store_fingerprint_cache
    calls = 0
    fingerprints = iter([("path", "a"), ("path", "b"), ("path", "b"), ("path", "b")])
    try:
        extension_store.list_extensions = lambda: []
        extension_store._refresh_store_fingerprint_cache = lambda *_args: next(fingerprints)

        def build(records: list[dict]) -> tuple[dict[str, bool], dict[str, str]]:
            nonlocal calls
            calls += 1
            if calls == 1:
                extension_store._clear_projection_cache()
            return original_build(records)

        extension_store._build_runtime_readiness_projection = build
        assert extension_store.refresh_runtime_readiness_projection() == {}
        assert calls == 2
    finally:
        extension_store.list_extensions = original_list
        extension_store._build_runtime_readiness_projection = original_build
        extension_store._refresh_store_fingerprint_cache = original_store_fingerprint


def test_store_invalidation_wakes_readiness_audit() -> None:
    import threading
    import time

    extension_store._RUNTIME_READINESS_CHANGE.clear()
    observed: list[bool] = []
    waiter = threading.Thread(
        target=lambda: observed.append(extension_store.wait_for_runtime_readiness_change(1.0))
    )
    waiter.start()
    time.sleep(0.01)
    extension_store._clear_projection_cache()
    waiter.join(timeout=0.2)
    assert observed == [True]


def test_integrity_timeout_kills_worker_and_recovers() -> None:
    class TimedOutWorker:
        killed = False

        def run(self, _specs: list[dict], *, timeout: float):
            assert timeout == 2.0
            raise TimeoutError()

        def close(self, *, force: bool = False):
            self.killed = force

    worker = TimedOutWorker()
    original = extension_store._RUNTIME_INTEGRITY_WORKER
    try:
        extension_store._RUNTIME_INTEGRITY_WORKER = worker
        record = _record("timeout")
        record["source"] = {"install_path": "/not-used"}
        record["manifest"]["protocol"] = {
            "version": 1,
            "smoke_test": {"required_paths": ["backend.py"], "python_modules": []},
        }
        assert extension_store._runtime_package_fingerprint(record) is None
        assert worker.killed
        assert extension_store._RUNTIME_INTEGRITY_WORKER is None
    finally:
        extension_store._RUNTIME_INTEGRITY_WORKER = original


def test_root_and_nested_mutation_during_hash_fail_closed() -> None:
    import tempfile
    from pathlib import Path

    for mutation in ("root", "file", "in_place", "nested"):
        parent = Path(tempfile.mkdtemp(prefix="ready-race-"))
        root = parent / "package"
        nested = root / "nested"
        nested.mkdir(parents=True)
        marker = nested / "backend.py"
        marker.write_bytes(b"x" * (2 * 1024 * 1024))
        marker_stat = marker.stat()
        spec = {"root": str(root), "relative_paths": ["nested"], "modules": []}
        original_read = extension_integrity.os.read
        fired = False

        def mutate(fd: int, size: int) -> bytes:
            nonlocal fired
            chunk = original_read(fd, size)
            if chunk and not fired:
                fired = True
                if mutation == "root":
                    root.rename(parent / "old-package")
                    (root / "nested").mkdir(parents=True)
                    (root / "nested" / "backend.py").write_bytes(b"y")
                elif mutation == "file":
                    marker.rename(nested / "old.py")
                    marker.write_bytes(b"y" * len(chunk))
                elif mutation == "in_place":
                    with marker.open("r+b") as target:
                        target.seek(0)
                        target.write(b"y" * len(chunk))
                        target.flush()
                        os.fsync(target.fileno())
                    os.utime(marker, ns=(marker_stat.st_atime_ns, marker_stat.st_mtime_ns))
                else:
                    (nested / "added.py").write_text("added", encoding="utf-8")
            return chunk

        try:
            extension_integrity.os.read = mutate
            assert extension_integrity.fingerprint_package(spec)["digest"] is None
            assert fired
        finally:
            extension_integrity.os.read = original_read


def test_windows_directory_membership_mutation_during_enumeration_fails_closed() -> None:
    if os.name != "nt":
        return
    import extension_integrity_windows
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="ready-windows-enumeration-race-"))
    nested = root / "nested"
    nested.mkdir()
    (nested / "backend.py").write_text("stable", encoding="utf-8")
    spec = {"root": str(root), "relative_paths": ["nested"], "modules": []}
    original_entries = extension_integrity_windows._directory_entries
    fired = False

    def mutate(handle: int):
        nonlocal fired
        entries = original_entries(handle)
        if entries is not None and not fired:
            fired = True
            (nested / "added.py").write_text("added", encoding="utf-8")
        return entries

    try:
        extension_integrity_windows._directory_entries = mutate
        assert extension_integrity.fingerprint_package(spec)["digest"] is None
        assert fired
    finally:
        extension_integrity_windows._directory_entries = original_entries


if __name__ == "__main__":
    test_startup_schedules_readiness_refresh_without_awaiting_it()
    test_hook_lists_skip_runtime_ready_without_requested_hook()
    test_hook_lists_check_runtime_ready_for_requested_hook()
    test_hook_lists_filter_extension_without_backend_routes()
    test_persisted_smoke_projection_does_not_touch_filesystem()
    test_verified_projection_fails_closed_after_runtime_file_removed()
    test_projection_fingerprints_python_module_outside_required_paths()
    test_integrity_worker_detects_metadata_spoof_and_symlink()
    test_concurrent_refreshes_share_one_integrity_scan()
    test_first_integrity_failure_is_not_runtime_ready()
    test_persisted_install_digest_is_authoritative_on_first_refresh()
    test_store_mutation_during_scan_retries_before_publish()
    test_store_invalidation_wakes_readiness_audit()
    test_integrity_timeout_kills_worker_and_recovers()
    test_root_and_nested_mutation_during_hash_fail_closed()
    test_windows_directory_membership_mutation_during_enumeration_fails_closed()
    print("ok")
