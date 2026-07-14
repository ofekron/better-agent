"""Bind a fake coordinator as the active runtime for tests.

Production callers resolve the runtime through
`orchestrator.get_active_coordinator()` (via `runtime_client.runtime`).
Tests bind their fake through the same canonical accessor using the
per-task ContextVar, so nothing leaks across tests.
"""

from contextlib import contextmanager
from typing import Any, Iterator

# Import at module load time, before tests stub sys.modules entries
# orchestrator depends on (event_bus, event_ingester, ...).
import orchestrator


def activate(coordinator: Any) -> Any:
    """Bind `coordinator` as the active runtime; returns a reset token."""
    return orchestrator._active_coordinator_var.set(coordinator)


def deactivate(token: Any) -> None:
    orchestrator._active_coordinator_var.reset(token)


@contextmanager
def bind_coordinator(coordinator: Any) -> Iterator[Any]:
    token = activate(coordinator)
    try:
        yield coordinator
    finally:
        deactivate(token)
