"""Assert an adequate soft file-descriptor limit at startup.

The backend legitimately parks many concurrent fds: the event-ingester
append-handle cache (up to `_MAX_OPEN_APPEND_HANDLES`), one byte-follower
per active tailer, WS sockets, the sqlite search index (+WAL/SHM), and the
interpreter's shared-library mappings. macOS launch contexts default the
soft `RLIMIT_NOFILE` to 256 (launchd/desktop, and Terminal-derived shells),
and nothing in `run.sh` or the launcher raises it. Once concurrent
session/subscription load pushes steady-state fds past that ceiling every
`open()` fails with `OSError: [Errno 24] Too many open files`.

Raising the soft limit toward the hard limit at startup makes the fd budget
independent of whichever launcher happened to spawn the process.
"""
import logging

logger = logging.getLogger(__name__)

# 65536 is well under macOS `kern.maxfilesperproc` (typically 92160) and far
# above any realistic concurrent-session fd need.
TARGET_SOFT_NOFILE = 65536


def desired_soft_nofile(soft: int, hard: int, target: int, infinity: int) -> int | None:
    """Return the soft limit to set, or None if no change is warranted.

    Never lowers the current soft limit. When the hard limit is unbounded
    (`RLIM_INFINITY`), `min(target, hard)` would collapse to the -1 sentinel
    and *shrink* the limit — so the unbounded branch uses `target` directly.
    """
    ceiling = target if hard == infinity else min(target, hard)
    if ceiling <= soft:
        return None
    return ceiling


def raise_fd_limit(target: int = TARGET_SOFT_NOFILE) -> None:
    try:
        import resource
    except ImportError:
        return  # non-POSIX (Windows): no RLIMIT_NOFILE to raise.
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    new_soft = desired_soft_nofile(soft, hard, target, resource.RLIM_INFINITY)
    if new_soft is None:
        return
    resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
    logger.info("raise_fd_limit: soft RLIMIT_NOFILE %d -> %d", soft, new_soft)
