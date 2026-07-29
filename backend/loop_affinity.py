"""Run a coroutine on the loop that owns it, from any thread.

One concern, one owner. Backend state lives on the main event loop, but
plenty of code that needs to touch it runs elsewhere: `to_thread`
workers, the persist scheduler thread, the ingester's executor, the
recovery thread. Each of those needs the same primitive — "hand this
coroutine to the loop it belongs to" — and before this module existed
several subsystems each grew their own copy, with subtly different
behavior when the loop was missing or closed.

`asyncio.get_running_loop().create_task(...)` is the wrong tool for the
job in two distinct ways: it raises when the caller has no loop at all,
and it silently targets the WRONG loop when the caller has one of its
own. `run_coroutine_threadsafe` is the thread-safe primitive; this
module picks between the two and defines one behavior for the failure
case.

Contract:
- `bind_main_loop(loop)` once at startup, from the main loop.
- `schedule_on_main(coro)` returns True if the coroutine was handed off,
  False if there is no usable loop. On False the coroutine is closed, so
  a caller never leaks a "coroutine was never awaited" warning.
- Never raises, never blocks the calling thread, never awaits.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_main_loop: Optional[asyncio.AbstractEventLoop] = None


def bind_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Declare the loop that owns backend state. Call once at startup."""
    global _main_loop
    _main_loop = loop


def main_loop() -> Optional[asyncio.AbstractEventLoop]:
    return _main_loop


def schedule_on_main(coro) -> bool:
    """Hand `coro` to the main loop. False if it could not be scheduled."""
    return schedule_on(_main_loop, coro)


def schedule_on(loop: Optional[asyncio.AbstractEventLoop], coro) -> bool:
    """Hand `coro` to `loop` from whatever thread is calling."""
    if loop is None or loop.is_closed():
        coro.close()
        return False
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        loop.create_task(coro)
        return True
    try:
        asyncio.run_coroutine_threadsafe(coro, loop)
        return True
    except RuntimeError:
        # Loop stopped between the check and the submit.
        coro.close()
        return False


async def call_on_main(factory):
    """Await a coroutine ON the main loop, from whatever loop is calling.

    The fire-and-forget `schedule_on_*` helpers are wrong when the caller
    needs the result, or needs the work to have finished before it
    continues. This is the awaiting counterpart: work that must run on
    the main loop — anything touching a loop-bound engine, lock, or
    queue — but whose caller lives elsewhere.

    Takes a factory so the coroutine is created on the loop that runs
    it. Falls through to running inline when there is no main loop bound
    or the caller is already on it.
    """
    loop = _main_loop
    if loop is None or loop.is_closed():
        return await factory()
    try:
        if asyncio.get_running_loop() is loop:
            return await factory()
    except RuntimeError:
        pass
    future = asyncio.run_coroutine_threadsafe(_invoke(factory), loop)
    return await asyncio.wrap_future(future)


async def _invoke(factory):
    return await factory()


def reset_for_tests() -> None:
    global _main_loop
    _main_loop = None
