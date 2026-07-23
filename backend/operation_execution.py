from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class OperationExecutionContext:
    request_id: str
    operation: str
    deadline_at: float | None
    record_receipt: Callable[[str], None]


_CURRENT: contextvars.ContextVar[OperationExecutionContext | None] = contextvars.ContextVar(
    "operation_execution_context",
    default=None,
)


@contextmanager
def bind(context: OperationExecutionContext):
    token = _CURRENT.set(context)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current() -> OperationExecutionContext:
    context = _CURRENT.get()
    if context is None:
        raise RuntimeError("durable operation execution context is not bound")
    return context
