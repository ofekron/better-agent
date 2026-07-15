from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time

HOME = tempfile.mkdtemp(prefix="bc-test-import-off-loop-")
os.environ["BETTER_AGENT_HOME"] = HOME

import main  # noqa: E402


def test_import_off_loop_does_not_block_event_loop() -> None:
    """Regression for the 22:09-22:12 lag-watchdog stalls where
    `_on_startup_bg_orchestrator` ran a plain `import
    requirement_analysis.session_tags` synchronously on the event loop
    (faulthandler dump: main.py -> importlib._bootstrap_external.get_data,
    ~2.7s). `_import_off_loop` must run `importlib.import_module` via
    `asyncio.to_thread` so a concurrent coroutine keeps making progress
    while the (slow, simulated) import executes.
    """
    import sys
    import types

    slow_module_name = "bc_test_import_off_loop_slow_module"
    import_started = asyncio.Event()
    import_finished = asyncio.Event()

    def fake_slow_import(name: str):
        assert name == slow_module_name
        # Runs on a worker thread (via to_thread); block that thread only.
        time.sleep(0.3)
        mod = types.ModuleType(name)
        return mod

    async def run() -> tuple[float, int]:
        loop = asyncio.get_running_loop()
        heartbeat_ticks = 0
        stop = False

        async def heartbeat() -> None:
            nonlocal heartbeat_ticks
            while not stop:
                await asyncio.sleep(0.02)
                heartbeat_ticks += 1

        hb_task = asyncio.create_task(heartbeat())
        started = time.perf_counter()
        import importlib
        original_import_module = importlib.import_module
        importlib.import_module = fake_slow_import
        try:
            result = await main._import_off_loop(slow_module_name)
        finally:
            importlib.import_module = original_import_module
        elapsed = time.perf_counter() - started
        stop = True
        await hb_task
        assert isinstance(result, types.ModuleType)
        return elapsed, heartbeat_ticks

    elapsed, ticks = asyncio.run(run())
    assert elapsed >= 0.3, f"fake import should have taken >=0.3s, took {elapsed}"
    # If the import ran synchronously on the loop, the heartbeat (sleeping
    # 20ms between ticks) would get ~0 ticks during the 300ms blocking call.
    # Off-loop, it should tick roughly elapsed/0.02 times (generous floor).
    assert ticks >= 8, (
        f"event loop starved during import_off_loop: only {ticks} heartbeat "
        f"ticks in {elapsed:.3f}s (expected the loop to stay responsive)"
    )


def main_test() -> None:
    test_import_off_loop_does_not_block_event_loop()
    print("ALL PASS")


if __name__ == "__main__":
    try:
        main_test()
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
