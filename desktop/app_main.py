"""Unified PyInstaller entrypoint for the Better Agent macOS app.

One frozen binary, three roles, chosen by argv:
  - `--run-dir <dir>`  → run a worker runner (delegates to `app_entry`).
  - `--serve`          → run the FastAPI backend server (via `app_entry`).
  - `--serve-node`     → run the worker-node backend (via `app_entry`).
  - `--operation-cli`  → run the generated operation CLI dispatcher.
  - (no args)          → run the desktop shell — what double-clicking the
                         `.app` does.

`backend/` and `desktop/` are both on the bundle's import path (set in
`BetterAgent.spec`'s `pathex`).
"""

from __future__ import annotations

import os
import sys

from paths import ba_home

# Early-startup diagnostics. A windowed `.app` has no stdout/stderr —
# without this, a hang during `import main` leaves us nothing to debug.
# `faulthandler.dump_traceback_later(repeat=True)` writes ALL thread
# Python stacks to disk on a timer, so a stuck process drops a complete
# dump into `ba_home()/faulthandler.log`.
try:
    _home = ba_home()
    import faulthandler
    import threading as _th
    import traceback as _tb
    _fh = (_home / "faulthandler.log").open("a", buffering=1)
    _fh.write(f"=== app_main pid={os.getpid()} argv={sys.argv} ===\n")
    faulthandler.enable(file=_fh)
    faulthandler.dump_traceback_later(15, repeat=True, file=_fh)

    def _excepthook(exc_type, exc, tb):
        try:
            _fh.write(f"=== uncaught {exc_type.__name__}: {exc} ===\n")
            _tb.print_exception(exc_type, exc, tb, file=_fh)
            _fh.write("\n")
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = _excepthook

    def _thread_excepthook(args):
        try:
            _fh.write(
                f"=== uncaught in thread {args.thread.name}: "
                f"{args.exc_type.__name__} ===\n"
            )
            _tb.print_exception(
                args.exc_type, args.exc_value, args.exc_traceback, file=_fh,
            )
            _fh.write("\n")
        except Exception:
            pass
    _th.excepthook = _thread_excepthook
except Exception:
    pass


def _role(argv: list[str]) -> str:
    """Classify the invocation. `--run-dir` or `--serve` → 'backend'
    (server/runner, both handled by `app_entry`); otherwise → 'shell'."""
    if (
        "--run-dir" in argv
        or "--serve" in argv
        or "--serve-node" in argv
        or "--operation-cli" in argv
    ):
        return "backend"
    return "shell"


def main() -> int:
    argv = sys.argv[1:]
    if _role(argv) == "backend":
        from app_entry import _main as backend_main
        return backend_main(argv)
    from shell import main as shell_main
    return shell_main()


if __name__ == "__main__":
    sys.exit(main())
