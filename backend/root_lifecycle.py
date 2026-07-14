from __future__ import annotations

import functools
import threading
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar


_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()
F = TypeVar("F", bound=Callable)


@contextmanager
def root_lifecycle_gate(root_id: str) -> Iterator[None]:
    with _guard:
        lock = _locks.setdefault(root_id, threading.RLock())
    with lock:
        yield


def serialized_root_argument(*, position: int, keyword: str) -> Callable[[F], F]:
    def decorate(function: F) -> F:
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            root_id = kwargs.get(keyword)
            if root_id is None and len(args) > position:
                root_id = args[position]
            if not root_id:
                return function(*args, **kwargs)
            with root_lifecycle_gate(str(root_id)):
                return function(*args, **kwargs)
        return wrapped  # type: ignore[return-value]
    return decorate
