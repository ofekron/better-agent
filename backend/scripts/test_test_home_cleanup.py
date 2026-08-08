"""Locks the temp-home lifecycle in `_test_home`.

Every isolated home is a full state tree of megabytes, and nothing outside
this module ever collects them: before the exit hook existed, one run of the
suite stranded a home per test file and the system temp dir had accumulated
over ten thousand of them.

Each case runs in a subprocess, because the guarantee under test is what
survives interpreter exit.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _run(body: str) -> tuple[str, str]:
    """Run `body` in a child interpreter; return (printed home path, stderr)."""
    script = f"import sys\nsys.path.insert(0, {str(_HERE)!r})\nimport _test_home\n{body}"
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(_HERE),
    )
    home = ""
    for line in proc.stdout.splitlines():
        if line.startswith("HOME="):
            home = line[len("HOME="):].strip()
    return home, proc.stderr


def test_isolate_home_removed_at_exit() -> None:
    home, stderr = _run('print("HOME=" + _test_home.isolate("ba-cleanup-isolate-"))')
    assert bool(home) and not Path(home).exists(), \
        f"home={home!r} exists={bool(home) and Path(home).exists()} stderr={stderr[-400:]}"
    print(f"{PASS} isolate() home is gone after the process exits")


def test_acquired_home_removed_at_exit() -> None:
    home, stderr = _run(
        'h = _test_home.TestHome.acquire("ba-cleanup-acquire-")\n'
        'print("HOME=" + h.path)'
    )
    assert bool(home) and not Path(home).exists(), \
        f"home={home!r} stderr={stderr[-400:]}"
    print(f"{PASS} an unreleased TestHome is gone after the process exits")


def test_explicit_release_is_not_double_deleted() -> None:
    """release() then exit must stay quiet — the hook runs on a released handle."""
    home, stderr = _run(
        'h = _test_home.TestHome.acquire("ba-cleanup-release-")\n'
        'print("HOME=" + h.path)\n'
        'h.release()'
    )
    assert bool(home) and not Path(home).exists() and "Traceback" not in stderr, \
        f"home={home!r} stderr={stderr[-400:]}"
    print(f"{PASS} explicit release() leaves the exit hook harmless")


def test_writes_land_in_the_home_before_cleanup() -> None:
    """Guards against a hook that fires early: the home must be usable
    for the whole run, and only then removed."""
    home, stderr = _run(
        'home = _test_home.isolate("ba-cleanup-usable-")\n'
        'print("HOME=" + home)\n'
        'import paths\n'
        'p = paths.ba_home() / "marker.txt"\n'
        'p.write_text("x", encoding="utf-8")\n'
        'assert p.is_file()'
    )
    assert bool(home) and not Path(home).exists() and "Traceback" not in stderr, \
        f"home={home!r} stderr={stderr[-400:]}"
    print(f"{PASS} the home stays writable until exit, then is removed")


def main() -> int:
    tests = [
        test_isolate_home_removed_at_exit,
        test_acquired_home_removed_at_exit,
        test_explicit_release_is_not_double_deleted,
        test_writes_land_in_the_home_before_cleanup,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception:
            import traceback
            traceback.print_exc()
            print(f"{FAIL} {t.__name__}")
            failed += 1
            continue
        print(f"{PASS} {t.__name__}")
    print(f"\n{len(tests) - failed} of {len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
