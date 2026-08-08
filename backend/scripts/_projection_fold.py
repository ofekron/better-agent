"""Test-fixture startup step shared by suites that exercise
`native_import.import_session` (or any path that journals events and
expects the SessionProjectionDrainer fold to populate `msg.events`).

Mirrors main.py's real startup: the journal writer's ack coroutine is
scheduled onto the bound loop via `run_coroutine_threadsafe`, so the loop
must actually be running — a dedicated background loop thread for the
lifetime of the test process. Call AFTER `_test_home.isolate` and before
any import/replay work.
"""
from __future__ import annotations

import asyncio
import threading


def start_projection_fold() -> asyncio.AbstractEventLoop:
    import event_bus_subscribers
    from event_journal import bind_event_journal_loop

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    bind_event_journal_loop(loop)
    event_bus_subscribers.register_default_subscribers()
    return loop
