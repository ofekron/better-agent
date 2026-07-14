from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from canonical_event import CommittedFact


@dataclass
class CachedProjection:
    root_id: str
    root_generation: int
    source_cursor: int
    facts: list[CommittedFact]
    snapshot: dict
    delta: dict | None
    weight_bytes: int


class ChatProjectionCache:
    def __init__(self, *, max_roots: int = 20, max_bytes: int = 64 * 1024 * 1024) -> None:
        if max_roots < 1 or max_bytes < 1:
            raise ValueError("cache limits must be positive")
        self._max_roots = max_roots
        self._max_bytes = max_bytes
        self._entries: OrderedDict[str, CachedProjection] = OrderedDict()
        self._bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, root_id: str) -> CachedProjection | None:
        entry = self._entries.get(root_id)
        if entry is None:
            self._misses += 1
            return None
        self._hits += 1
        self._entries.move_to_end(root_id)
        return entry

    def put(self, entry: CachedProjection) -> None:
        previous = self._entries.pop(entry.root_id, None)
        if previous is not None:
            self._bytes -= previous.weight_bytes
        if entry.weight_bytes > self._max_bytes:
            return
        self._entries[entry.root_id] = entry
        self._bytes += entry.weight_bytes
        while len(self._entries) > self._max_roots or self._bytes > self._max_bytes:
            _, evicted = self._entries.popitem(last=False)
            self._bytes -= evicted.weight_bytes
            self._evictions += 1

    def discard(self, root_id: str) -> None:
        entry = self._entries.pop(root_id, None)
        if entry is not None:
            self._bytes -= entry.weight_bytes

    def stats(self) -> dict[str, int]:
        return {
            "roots": len(self._entries),
            "bytes": self._bytes,
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "max_roots": self._max_roots,
            "max_bytes": self._max_bytes,
        }
