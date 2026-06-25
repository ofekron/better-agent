"""Regression: backend must assert an adequate soft RLIMIT_NOFILE at startup.

Pre-fix there was no `fd_limits` module and nothing raised the limit, so the
backend ran at the launcher's default soft limit (256 on macOS
launchd/Terminal). Once concurrent-session fd use crossed it, every open()
failed with `OSError: [Errno 24] Too many open files` — including the
event-journal tailer read and `extensions.json` parse in the crash report.

Locks the decision logic, especially the `RLIM_INFINITY` hard-limit branch:
a naive `min(target, hard)` returns the -1 sentinel and would SHRINK the
soft limit on Linux, where `RLIM_INFINITY == -1`.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

import resource  # noqa: E402
from fd_limits import (  # noqa: E402
    TARGET_SOFT_NOFILE,
    desired_soft_nofile,
    raise_fd_limit,
)


def test_finite_hard_clamps_to_hard():
    # target above a finite hard limit clamps down to the hard limit.
    assert desired_soft_nofile(256, 1024, 65536, -1) == 1024
    # target below a finite hard limit uses target.
    assert desired_soft_nofile(256, 200000, 65536, -1) == 65536


def test_infinity_hard_uses_target_not_sentinel():
    # THE case that bites: hard == RLIM_INFINITY (-1 on Linux). min(target,
    # -1) would return -1 and shrink the limit; must use target instead.
    assert desired_soft_nofile(256, -1, 65536, -1) == 65536
    # macOS exposes RLIM_INFINITY as a huge int rather than -1 — also fine.
    big = 9223372036854775807
    assert desired_soft_nofile(256, big, 65536, big) == 65536


def test_never_lowers_when_already_adequate():
    assert desired_soft_nofile(65536, -1, 65536, -1) is None
    assert desired_soft_nofile(100000, 200000, 65536, -1) is None
    assert desired_soft_nofile(65536, 65536, 65536, -1) is None


def test_raise_fd_limit_never_lowers_real_process():
    before_soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    raise_fd_limit()
    after_soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert after_soft >= before_soft
    expected = desired_soft_nofile(
        before_soft, hard, TARGET_SOFT_NOFILE, resource.RLIM_INFINITY,
    )
    if expected is not None:
        assert after_soft == expected


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"PASS ({len(fns)} tests)")


if __name__ == "__main__":
    _run()
