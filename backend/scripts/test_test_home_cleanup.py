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


def test_isolate_home_removed_at_exit() -> bool:
    home, stderr = _run('print("HOME=" + _test_home.isolate("ba-cleanup-isolate-"))')
    ok = bool(home) and not Path(home).exists()
    print(f"{PASS if ok else FAIL} isolate() home is gone after the process exits")
    if not ok:
        print(f"  home={home!r} exists={bool(home) and Path(home).exists()} stderr={stderr[-400:]}")
    return ok


def test_acquired_home_removed_at_exit() -> bool:
    home, stderr = _run(
        'h = _test_home.TestHome.acquire("ba-cleanup-acquire-")\n'
        'print("HOME=" + h.path)'
    )
    ok = bool(home) and not Path(home).exists()
    print(f"{PASS if ok else FAIL} an unreleased TestHome is gone after the process exits")
    if not ok:
        print(f"  home={home!r} stderr={stderr[-400:]}")
    return ok


def test_explicit_release_is_not_double_deleted() -> bool:
    """release() then exit must stay quiet — the hook runs on a released handle."""
    home, stderr = _run(
        'h = _test_home.TestHome.acquire("ba-cleanup-release-")\n'
        'print("HOME=" + h.path)\n'
        'h.release()'
    )
    ok = bool(home) and not Path(home).exists() and "Traceback" not in stderr
    print(f"{PASS if ok else FAIL} explicit release() leaves the exit hook harmless")
    if not ok:
        print(f"  home={home!r} stderr={stderr[-400:]}")
    return ok


def test_writes_land_in_the_home_before_cleanup() -> bool:
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
    ok = bool(home) and not Path(home).exists() and "Traceback" not in stderr
    print(f"{PASS if ok else FAIL} the home stays writable until exit, then is removed")
    if not ok:
        print(f"  home={home!r} stderr={stderr[-400:]}")
    return ok


def main() -> int:
    tests = [
        test_isolate_home_removed_at_exit,
        test_acquired_home_removed_at_exit,
        test_explicit_release_is_not_double_deleted,
        test_writes_land_in_the_home_before_cleanup,
    ]
    results = [t() for t in tests]
    failed = results.count(False)
    print(f"\n{len(results) - failed} of {len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
