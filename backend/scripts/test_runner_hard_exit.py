"""Runner hard-exit regression test.

A runner that has written its completion artifacts must exit promptly
even when a runtime-operation thread is still blocked. Runtime ops post
with a 24 h timeout through `asyncio.to_thread`, which runs them on
asyncio's default (non-daemon) executor. A runner that exits the ordinary
way then joins that thread twice: `asyncio.run`'s close waits 300 s, and
`threading._shutdown` waits forever. The pinned runner stays registered
with the backend and parks every later turn on the same native session at
the wind-down gate.

Three child processes pin the three shapes: returning normally hangs
forever, `asyncio.run` + hard exit still eats the 300 s join, and only
driving the loop directly + hard exit — what `main` actually does — exits
promptly.
"""

import ast
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

_CHILD = """
import asyncio, sys, threading
sys.path.insert(0, {backend!r})

_never = threading.Event()


async def _run():
    # A runtime-operation POST parked on the default executor, exactly
    # like an outstanding ask/approval when the cancel sentinel lands.
    asyncio.get_running_loop().run_in_executor(None, _never.wait)
    await asyncio.sleep(0.2)
    return 7


{driver}
"""

# Returning through interpreter shutdown — the production wedge.
_CONTROL = "sys.exit(asyncio.run(_run()))"

# Hard exit, but AFTER asyncio.run: its close() still performs the 300 s
# executor join before control comes back, so the exit is not prompt.
_ASYNCIO_RUN_THEN_EXIT = """from runner_exit import hard_exit
hard_exit(asyncio.run(_run()))"""

# What `main` does: drive the loop directly, then hard-exit.
_FIXED = """from runner_exit import hard_exit
_loop = asyncio.new_event_loop()
hard_exit(_loop.run_until_complete(_run()))"""


def _spawn(driver: str) -> subprocess.Popen:
    script = Path(tempfile.mkstemp(suffix=".py")[1])
    script.write_text(
        _CHILD.format(backend=str(BACKEND), driver=driver), encoding="utf-8"
    )
    return subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait(proc: subprocess.Popen, seconds: float):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return proc.returncode
        time.sleep(0.05)
    return None


def _assert_hangs(driver: str, label: str) -> None:
    proc = _spawn(driver)
    try:
        assert _wait(proc, 8.0) is None, (
            f"{label} exited — the blocked-executor repro no longer "
            "reproduces, so this test proves nothing"
        )
        print(f"PASS {label} hangs (defect reproduced)")
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_returning_normally_never_exits() -> None:
    _assert_hangs(_CONTROL, "returning through interpreter shutdown")


def test_asyncio_run_still_stalls() -> None:
    """Pins why `main` must not use `asyncio.run`: the executor join
    happens inside it, before any hard exit can run."""
    _assert_hangs(_ASYNCIO_RUN_THEN_EXIT, "asyncio.run then hard_exit")


def test_direct_loop_plus_hard_exit_is_prompt() -> None:
    proc = _spawn(_FIXED)
    try:
        code = _wait(proc, 15.0)
        assert code == 7, (
            "driving the loop directly then hard-exiting must return the "
            f"run's status immediately, got {code}"
        )
        print("PASS direct loop + hard_exit exits promptly with the right code")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def _own_nodes(fn, kinds):
    """Nodes belonging to `fn` itself, not to a nested function."""
    nested = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not fn:
            nested.update(id(n) for n in ast.walk(node))
    return [
        n for n in ast.walk(fn)
        if isinstance(n, kinds) and id(n) not in nested and n is not fn
    ]


def test_main_always_exits_and_avoids_asyncio_run() -> None:
    """Structural guarantee, not an incidental one.

    `main` must never hand control back to interpreter shutdown, and must
    never route through `asyncio.run`, whose close() performs the 300 s
    executor join. Asserted over the real call graph — a synthetic child
    cannot catch someone reintroducing either shape.
    """
    for name in ("runner.py", "runner_codex.py"):
        tree = ast.parse((BACKEND / name).read_text(encoding="utf-8"))
        mains = [
            fn for fn in tree.body
            if isinstance(fn, ast.FunctionDef) and fn.name == "main"
        ]
        assert len(mains) == 1, f"{name}: expected one top-level main"
        fn = mains[0]

        returns = [r.lineno for r in _own_nodes(fn, (ast.Return,))]
        assert not returns, (
            f"{name}::main returns at line(s) {returns} instead of exiting "
            "— that path falls back into interpreter shutdown and can pin "
            "the runner forever"
        )

        calls = _own_nodes(fn, (ast.Call,))
        bad = [
            c.lineno for c in calls
            if isinstance(c.func, ast.Attribute)
            and c.func.attr == "run"
            and isinstance(c.func.value, ast.Name)
            and c.func.value.id == "asyncio"
        ]
        assert not bad, (
            f"{name}::main calls asyncio.run at line(s) {bad} — its close() "
            "joins the default executor for 300 s before any hard exit runs"
        )

        exits = [
            c for c in calls
            if isinstance(c.func, ast.Name) and c.func.id == "_exit_runner"
        ]
        assert exits, f"{name}::main never calls _exit_runner"

        # …and that _exit_runner actually terminates. Without this, an
        # _exit_runner that returns would let main fall off the end into
        # interpreter shutdown with every other assertion still green.
        helpers = [
            fn for fn in tree.body
            if isinstance(fn, ast.FunctionDef) and fn.name == "_exit_runner"
        ]
        assert len(helpers) == 1, f"{name}: expected one _exit_runner"
        terminates = [
            c for c in _own_nodes(helpers[0], (ast.Call,))
            if isinstance(c.func, ast.Name) and c.func.id == "hard_exit"
        ]
        assert terminates, f"{name}::_exit_runner never calls hard_exit"
    print("PASS main always exits and avoids asyncio.run")


if __name__ == "__main__":
    test_returning_normally_never_exits()
    test_asyncio_run_still_stalls()
    test_direct_loop_plus_hard_exit_is_prompt()
    test_main_always_exits_and_avoids_asyncio_run()
    print("ALL RUNNER HARD-EXIT TESTS PASSED")
