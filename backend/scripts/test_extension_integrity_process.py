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


if __name__ == "__main__":
    asyncio.run(test_hashing_does_not_starve_event_loop())
    test_executor_shutdown_reopens_spawn_worker()
    test_packaged_spawn_and_cross_platform_security_wiring()
    test_crashed_worker_fails_closed_then_reopens()
    test_hanging_worker_is_dead_by_deadline_then_recovers()
    print("ok")
