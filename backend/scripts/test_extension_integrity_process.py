from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home

_test_home.isolate("bc-test-extension-integrity-process-")

import extension_integrity  # noqa: E402
import extension_store  # noqa: E402


def _hang_worker(_connection) -> None:
    time.sleep(60)


def _record(root: Path) -> dict:
    return {
        "enabled": True,
        "manifest": {
            "id": "integrity-process",
            "entrypoints": {},
            "permissions": {},
            "protocol": {
                "version": 1,
                "smoke_test": {"required_paths": ["payload.bin"], "python_modules": []},
            },
        },
        "source": {"install_path": str(root)},
    }


async def test_hashing_does_not_starve_event_loop() -> None:
    root = Path(tempfile.mkdtemp(prefix="integrity-process-tree-"))
    (root / "payload.bin").write_bytes(os.urandom(64 * 1024 * 1024))
    record = _record(root)
    original_list = extension_store.list_extensions
    original_active = extension_store._record_active
    stop = asyncio.Event()
    delays: list[float] = []

    async def ticker() -> None:
        expected = time.perf_counter() + 0.01
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            delays.append(now - expected)
            expected = now + 0.01

    try:
        extension_store.list_extensions = lambda: [record]
        extension_store._record_active = lambda _record: True
        ticker_task = asyncio.create_task(ticker())
        result = await asyncio.to_thread(extension_store.refresh_runtime_readiness_projection)
        stop.set()
        await ticker_task
        assert result["integrity-process"]
        assert delays and max(delays) < 0.1, max(delays)
    finally:
        extension_store.list_extensions = original_list
        extension_store._record_active = original_active
        extension_store._reset_runtime_integrity_executor()


def test_executor_shutdown_reopens_spawn_worker() -> None:
    first = extension_store._runtime_integrity_worker()
    assert first.process.name == "extension-integrity-worker"
    extension_store.shutdown_runtime_integrity_executor()
    assert extension_store._RUNTIME_INTEGRITY_WORKER is None
    second = extension_store._runtime_integrity_worker()
    assert second is not first
    assert second.process.name == "extension-integrity-worker"
    extension_store.shutdown_runtime_integrity_executor()


def test_worker_protocol_attributes_compute_and_roundtrip_residual() -> None:
    class Process:
        def is_alive(self) -> bool:
            return True

    class Connection:
        request_id = 0

        def send(self, message) -> None:
            self.request_id = message[0]

        def poll(self, _timeout: float) -> bool:
            time.sleep(0.03)
            return True

        def recv(self):
            return self.request_id, [{"digest": "ok"}], 5.0

    worker = extension_store._RuntimeIntegrityWorker.__new__(
        extension_store._RuntimeIntegrityWorker
    )
    worker._connection = Connection()
    worker._process = Process()
    worker._request_id = 0
    original_record = extension_store.perf.record
    timings: dict[str, float] = {}
    try:
        extension_store.perf.record = lambda name, value: timings.__setitem__(name, float(value))
        assert worker.run([{}], timeout=1.0) == [{"digest": "ok"}]
    finally:
        extension_store.perf.record = original_record
    assert timings["extension.integrity.worker_roundtrip"] >= 25.0
    assert timings["extension.integrity.worker_compute"] == 5.0
    assert timings["extension.integrity.worker_outside_compute"] >= 20.0


def test_packaged_spawn_and_cross_platform_security_wiring() -> None:
    main_source = (_BACKEND / "main.py").read_text(encoding="utf-8")
    store_source = (_BACKEND / "extension_store.py").read_text(encoding="utf-8")
    windows_source = (_BACKEND / "extension_integrity_windows.py").read_text(encoding="utf-8")
    assert "shutdown_runtime_integrity_executor" in main_source[main_source.index("async def on_shutdown()") :]
    assert 'multiprocessing.get_context("spawn")' in store_source
    assert "CreateFileW" in windows_source
    assert "NtCreateFile" in windows_source
    assert "RootDirectory" in windows_source
    assert "GetFileInformationByHandleEx" in windows_source
    assert "_OPEN_REPARSE" in windows_source
    assert "open_osfhandle" in windows_source
    assert "info.size_high" in windows_source
    assert "info.write_high" in windows_source


def test_crashed_worker_fails_closed_then_reopens() -> None:
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="integrity-crash-recovery-"))
    (root / "payload.bin").write_bytes(b"healthy")
    record = _record(root)
    worker = extension_store._runtime_integrity_worker()
    worker.process.terminate()
    worker.process.join(timeout=1.0)
    assert extension_store._runtime_package_fingerprint(record) is None
    assert extension_store._RUNTIME_INTEGRITY_WORKER is None
    assert extension_store._runtime_package_fingerprint(record) is not None
    extension_store.shutdown_runtime_integrity_executor()


def test_hanging_worker_is_dead_by_deadline_then_recovers() -> None:
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="integrity-timeout-recovery-"))
    (root / "payload.bin").write_bytes(b"healthy")
    record = _record(root)
    worker = extension_store._RuntimeIntegrityWorker(target=_hang_worker)
    extension_store._RUNTIME_INTEGRITY_WORKER = worker
    started = time.perf_counter()
    assert extension_store._runtime_package_fingerprint(record) is None
    assert time.perf_counter() - started < 2.5
    assert not worker.process.is_alive()
    assert extension_store._runtime_package_fingerprint(record) is not None
    extension_store.shutdown_runtime_integrity_executor()


def test_symlinked_ancestor_is_rejected() -> None:
    base = Path(tempfile.mkdtemp(prefix="integrity-symlink-parent-"))
    actual_parent = base / "actual"
    package = actual_parent / "package"
    package.mkdir(parents=True)
    (package / "payload.bin").write_bytes(b"payload")
    linked_parent = base / "linked"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    result = extension_integrity.fingerprint_package({
        "root": str(linked_parent / "package"),
        "trusted_root": str(base),
        "relative_paths": ["payload.bin"],
        "static_modules": {},
        "modules": [],
    })
    assert result["digest"] is None


def test_package_outside_trusted_root_is_rejected() -> None:
    base = Path(tempfile.mkdtemp(prefix="integrity-trusted-boundary-"))
    trusted = base / "trusted"
    trusted.mkdir()
    outside = base / "outside"
    outside.mkdir()
    (outside / "payload.bin").write_bytes(b"payload")
    common = {
        "trusted_root": str(trusted),
        "relative_paths": ["payload.bin"],
        "static_modules": {},
        "modules": [],
    }
    assert extension_integrity.fingerprint_package({
        **common,
        "root": str(outside),
    })["digest"] is None
    assert extension_integrity.fingerprint_package({
        **common,
        "root": str(trusted / ".." / "outside"),
    })["digest"] is None


if __name__ == "__main__":
    asyncio.run(test_hashing_does_not_starve_event_loop())
    test_executor_shutdown_reopens_spawn_worker()
    test_worker_protocol_attributes_compute_and_roundtrip_residual()
    test_packaged_spawn_and_cross_platform_security_wiring()
    test_crashed_worker_fails_closed_then_reopens()
    test_hanging_worker_is_dead_by_deadline_then_recovers()
    test_symlinked_ancestor_is_rejected()
    test_package_outside_trusted_root_is_rejected()
    print("ok")
