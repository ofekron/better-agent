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
    test_symlinked_ancestor_is_rejected()
    test_package_outside_trusted_root_is_rejected()
    print("ok")
