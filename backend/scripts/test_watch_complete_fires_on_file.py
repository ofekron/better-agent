"""Provider-side babysitter contract: `_watch_complete` finalizes the
turn off complete.json WHILE THE RUNNER PROCESS IS STILL ALIVE.

Locks (would FAIL on the old process-exit-keyed watcher):
  T1  complete StreamEvent enqueued while popen.poll() is None
  T2  the run STAYS registered — cancel levers resolve it — but is NOT
      marked lingering until the runner touches the `lingering` sentinel
      (a normal turn's brief shutdown window must not flash the UI)
  T3  the tailer is NOT stopped at complete-file time (late CLI flushes
      keep flowing until process exit)
  T4  sentinel appears → lingering=True + run.lingering(True) published
  T5  process exit → run.lingering(False) + _watch_linger_exit deregisters
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-watchfile-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from provider_claude import ClaudeProvider, RunState  # noqa: E402
from event_bus import bus  # noqa: E402

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


class _FakePopen:
    """poll() controllable from the test."""
    def __init__(self):
        self.pid = os.getpid()
        self._rc = None

    def poll(self):
        return self._rc


class _BlockingPopen:
    def __init__(self):
        self.pid = os.getpid()
        self._rc = None
        self.release = threading.Event()

    def poll(self):
        self.release.wait(timeout=0.35)
        return self._rc


class _FakeTailer:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


async def _scenario() -> None:
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=_TMP_HOME))
    prov = ClaudeProvider.__new__(ClaudeProvider)
    prov._runs = {}
    prov.id = "test-prov"

    lingering_facts: list[bool] = []

    async def _spy(ev):
        lingering_facts.append(bool(ev.payload.get("lingering")))

    bus.unsubscribe("test-linger-spy")
    bus.subscribe("run.lingering", _spy, name="test-linger-spy")

    popen = _FakePopen()
    tailer = _FakeTailer()
    rs = RunState(
        run_id="run-x",
        run_dir=run_dir,
        popen=popen,
        mode="native",
        app_session_id="sid-1",
        queue=asyncio.Queue(),
        jsonl_path=None,
        tailer=tailer,
    )
    prov._runs[rs.run_id] = rs

    # Turn ends: the (still-alive) runner writes complete.json and lingers.
    (run_dir / "complete.json").write_text(json.dumps({
        "success": True, "session_id": "agent-sid", "error": None,
        "token_usage": None,
    }))

    watch = asyncio.create_task(prov._watch_complete(rs))
    event = await asyncio.wait_for(rs.queue.get(), timeout=5)
    check(event.type == "complete" and popen.poll() is None,
          "T1 complete event fired while the runner process is alive")
    await watch

    check(prov._runs.get("run-x") is rs,
          "T2 run stays registered while the process is alive")
    await asyncio.sleep(0.3)
    check(not rs.lingering and lingering_facts == [],
          "T2 NOT lingering before the runner's sentinel (no UI flash)")
    check(not tailer.stopped,
          "T3 tailer NOT stopped at complete-file time")

    # The runner decides it is babysitting: it touches the sentinel.
    (run_dir / "lingering").touch()
    deadline = asyncio.get_event_loop().time() + 5
    while not rs.lingering and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
    check(rs.lingering and lingering_facts == [True],
          f"T4 sentinel → lingering=True + fact published ({lingering_facts})")

    # Background work ends → runner exits → linger watcher deregisters.
    popen._rc = 0
    deadline = asyncio.get_event_loop().time() + 5
    while "run-x" in prov._runs and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
    check("run-x" not in prov._runs,
          "T5 run deregistered after the lingering process exited")
    check(tailer.stopped, "T5 tailer stopped at process exit")
    check(lingering_facts == [True, False],
          f"T5 run.lingering False fact published ({lingering_facts})")

    bus.unsubscribe("test-linger-spy")


async def _linger_poll_does_not_block_loop() -> None:
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=_TMP_HOME))
    prov = ClaudeProvider.__new__(ClaudeProvider)
    prov._runs = {}
    prov.id = "test-prov"

    popen = _BlockingPopen()
    rs = RunState(
        run_id="run-slow-poll",
        run_dir=run_dir,
        popen=popen,
        mode="native",
        app_session_id="sid-slow",
        queue=asyncio.Queue(),
        jsonl_path=None,
    )
    prov._runs[rs.run_id] = rs

    timer = threading.Timer(0.35, popen.release.set)
    timer.start()
    watch = asyncio.create_task(prov._watch_linger_exit(rs))
    started = time.perf_counter()
    try:
        await asyncio.sleep(0.05)
        elapsed = time.perf_counter() - started
        check(elapsed < 0.22, f"slow linger poll does not block loop ({elapsed:.3f}s)")
    finally:
        popen._rc = 0
        popen.release.set()
        timer.cancel()
        await asyncio.wait_for(watch, timeout=2)


async def _main_async() -> None:
    await _scenario()
    await _linger_poll_does_not_block_loop()


def main() -> int:
    asyncio.run(_main_async())
    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: _watch_complete fires on complete.json while alive")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
