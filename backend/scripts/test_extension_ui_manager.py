"""Locks ExtensionUIManager's contract: it publishes a UI frame only
when the extension UI projection actually moved, and names exactly the
extensions that moved."""

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="ext-ui-mgr-")
paths.engage_test_home(_TMP)

import asyncio  # noqa: E402
import shutil  # noqa: E402

import event_bus  # noqa: E402
import extension_ui_manager  # noqa: E402

FRAMES: list[dict] = []
_FRAME_ARRIVED = threading.Event()
PROJECTION: list[dict] = []


def _fake_read_entrypoints() -> list[dict]:
    return [dict(entry) for entry in PROJECTION]


def _fake_broadcast(payload: dict) -> None:
    FRAMES.append(payload)
    _FRAME_ARRIVED.set()


def entry(extension_id: str, module_url: str) -> dict:
    return {
        "extension_id": extension_id,
        "name": extension_id,
        "frontend_modules": [
            {"slot": "sidebar", "id": "main", "label": "Main",
             "kind": "module", "module": "m.js", "module_url": module_url},
        ],
    }


def mutate_and_settle() -> None:
    _FRAME_ARRIVED.clear()
    extension_ui_manager.manager.publish_fact(
        extension_ui_manager.EXTENSION_MUTATED, payload={"extension_id": ""}
    )
    # A frame is the observable outcome; when none is expected the wait
    # still has to bound the no-frame case, so give the reconcile room
    # to run and then assert on what arrived.
    _FRAME_ARRIVED.wait(timeout=5.0)


async def main() -> int:
    loop = asyncio.get_running_loop()
    event_bus.bus.bind_unpinned_to_current_loop()

    extension_ui_manager._read_entrypoints = _fake_read_entrypoints
    manager = extension_ui_manager.manager
    manager._broadcast = _fake_broadcast  # type: ignore[method-assign]
    manager.start()
    manager.bind(loop)

    failures: list[str] = []
    try:
        # First projection: everything is new, so everything is changed.
        PROJECTION[:] = [entry("a.ext", "/a.js"), entry("b.ext", "/b.js")]
        await asyncio.to_thread(mutate_and_settle)
        if len(FRAMES) != 1:
            failures.append(f"expected 1 initial frame, got {len(FRAMES)}")
        elif sorted(FRAMES[0]["changed_extension_ids"]) != ["a.ext", "b.ext"]:
            failures.append(f"bad initial changed ids: {FRAMES[0]}")

        # A mutation that does not move the projection must be silent.
        FRAMES.clear()
        await asyncio.to_thread(mutate_and_settle)
        if FRAMES:
            failures.append(f"unchanged projection broadcast anyway: {FRAMES}")

        # One extension moves: only that one is named.
        FRAMES.clear()
        PROJECTION[:] = [entry("a.ext", "/a2.js"), entry("b.ext", "/b.js")]
        await asyncio.to_thread(mutate_and_settle)
        if len(FRAMES) != 1:
            failures.append(f"expected 1 frame after change, got {len(FRAMES)}")
        elif FRAMES[0]["changed_extension_ids"] != ["a.ext"]:
            failures.append(f"bad changed ids: {FRAMES[0]['changed_extension_ids']}")
        elif len(FRAMES[0]["entrypoints"]) != 2:
            failures.append("frame must carry the full projection")

        # Removal counts as a change for the removed extension.
        FRAMES.clear()
        PROJECTION[:] = [entry("a.ext", "/a2.js")]
        await asyncio.to_thread(mutate_and_settle)
        if len(FRAMES) != 1 or FRAMES[0]["changed_extension_ids"] != ["b.ext"]:
            failures.append(f"removal not reported: {FRAMES}")

        # The work must not run on the main loop.
        FRAMES.clear()
        PROJECTION[:] = [entry("a.ext", "/a3.js")]
        seen_threads: list[str] = []
        original = extension_ui_manager._read_entrypoints

        def _tracking_read() -> list[dict]:
            seen_threads.append(threading.current_thread().name)
            return original()

        extension_ui_manager._read_entrypoints = _tracking_read
        await asyncio.to_thread(mutate_and_settle)
        extension_ui_manager._read_entrypoints = original
        main_thread = threading.main_thread().name
        if not seen_threads:
            failures.append("projection was never read")
        elif any(name == main_thread for name in seen_threads):
            failures.append(f"projection read on the main thread: {seen_threads}")
    finally:
        await asyncio.to_thread(manager.stop)
        shutil.rmtree(_TMP, ignore_errors=True)

    if failures:
        for line in failures:
            print(f"FAIL: {line}")
        return 1
    print("PASS: extension UI manager publishes only real UI changes")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
