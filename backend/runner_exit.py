from __future__ import annotations

import logging
import os
import sys
from typing import NoReturn

logger = logging.getLogger(__name__)


def hard_exit(code: int) -> NoReturn:
    """Exit a runner process immediately, bypassing interpreter shutdown.

    A runner is strictly per-turn: by the time this is called the
    completion artifacts are durable and the CLI is closed, so there is
    nothing durable left to lose. Normal shutdown cannot be trusted to
    get that far — `asyncio.run` joins the default executor with a 300 s
    timeout and `threading._shutdown` then joins the same non-daemon
    threads with no timeout at all. A runtime-operation POST still
    blocked in `urlopen` (24 h timeout) therefore pins the process
    forever, and a pinned runner stays registered with the backend and
    wedges the wind-down gate for every later turn on the same native
    session.

    This is only half the defense. It skips interpreter shutdown's
    unbounded join, but `asyncio.run`'s close() performs its own 300 s
    executor join BEFORE returning, so hard-exiting after `asyncio.run`
    still stalls. Callers must drive the loop directly
    (`new_event_loop().run_until_complete(...)`) and then call this.

    Explicit teardown replaces what `os._exit` skips: the operation
    host's listener socket (normally an atexit hook) and the stdio /
    logging buffers holding the tail of `stderr.log`.
    """
    try:
        from runner_operation_host import stop_active_host
        stop_active_host()
    except Exception:
        logger.exception("hard_exit: operation host teardown failed")
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(code)
