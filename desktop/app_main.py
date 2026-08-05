"""Unified PyInstaller entrypoint for the Better Agent macOS app.

One frozen binary, three roles, chosen by argv:
  - `--run-dir <dir>`  → run a worker runner (delegates to `app_entry`).
  - `--serve`          → run the FastAPI backend server (via `app_entry`).
  - `--serve-node`     → run the worker-node backend (via `app_entry`).
  - `--operation-cli`  → run the generated operation CLI dispatcher.
  - `--frozen-artifact-smoke` → verify the built onedir execution artifact.
  - (no args)          → run the desktop shell — what double-clicking the
                         `.app` does.

`backend/` and `desktop/` are both on the bundle's import path (set in
`BetterAgent.spec`'s `pathex`).
"""

from __future__ import annotations

import os
import sys
from types import TracebackType

from deep_link import DeepLinkError, deep_link_from_argv, redact_argv
from paths import ba_home
from private_diagnostics import (
    open_private_diagnostics_log,
    write_private_exception,
)


def _record_exception(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
    *,
    context: str = "uncaught",
) -> None:
    try:
        write_private_exception(
            _fh,
            exc_type,
            exc,
            tb,
            context=context,
        )
    except Exception:
        pass

# Early-startup diagnostics. A windowed `.app` has no stdout/stderr —
# without this, a hang during `import main` leaves us nothing to debug.
# `faulthandler.dump_traceback_later(repeat=True)` writes ALL thread
# Python stacks to disk on a timer, so a stuck process drops a complete
# dump into `ba_home()/faulthandler.log`.
try:
    import faulthandler
    import threading as _th
    _fh = open_private_diagnostics_log()
    _fh.write(
        f"=== app_main pid={os.getpid()} argv={redact_argv(sys.argv)} ===\n"
    )
    faulthandler.enable(file=_fh)
    faulthandler.dump_traceback_later(15, repeat=True, file=_fh)

    def _excepthook(exc_type, exc, tb):
        _record_exception(exc_type, exc, tb)
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = _excepthook

    def _thread_excepthook(args):
        _record_exception(
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            context=f"uncaught in thread {args.thread.name}",
        )
    _th.excepthook = _thread_excepthook
except Exception:
    pass


def _stop_diagnostics() -> None:
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
    try:
        _fh.close()
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
        or "--frozen-artifact-smoke" in argv
    ):
        return "backend"
    return "shell"


def main() -> int:
    argv = sys.argv[1:]
    if _role(argv) == "backend":
        from app_entry import _main as backend_main
        return backend_main(argv)
    try:
        pair_link = deep_link_from_argv(argv)
    except DeepLinkError:
        return 2
    activation = pair_link.as_event() if pair_link is not None else None
    from activation_server import forward_activation
    if forward_activation(ba_home(), activation or {"type": "activate"}):
        return 0
    from shell import main as shell_main
    return shell_main(initial_activation=activation)


def _run_main() -> int:
    try:
        return main()
    except Exception as exc:
        _record_exception(type(exc), exc, exc.__traceback__)
        return 1
    finally:
        _stop_diagnostics()


if __name__ == "__main__":
    sys.exit(_run_main())
