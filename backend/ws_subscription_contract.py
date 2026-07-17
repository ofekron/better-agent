"""Browser WS subscribe-frame contract, shared by the runtime WS endpoint
(main.py) and the BFF proxy/hub (bff_server.py / bff_event_hub.py).

A subscribe frame may carry an optional `priority` field:
  - "opened": the session is actually open in a client view.
  - "warm": the client is cache-warming (e.g. an LRU-cached session) and
    wants the same change-only delta/state frames without counting as
    real use for prioritization.
An absent field means "opened". Any other value is invalid and the frame
must be rejected (fail closed — no coercion).
"""
from __future__ import annotations

from typing import Any, Mapping

PRIORITY_OPENED = "opened"
PRIORITY_WARM = "warm"
SUBSCRIBE_PRIORITIES = frozenset({PRIORITY_OPENED, PRIORITY_WARM})


def resolve_subscribe_priority(frame: Mapping[str, Any]) -> str | None:
    """Effective priority of a subscribe frame, or None when invalid."""
    if "priority" not in frame:
        return PRIORITY_OPENED
    value = frame["priority"]
    if value in SUBSCRIBE_PRIORITIES:
        return value
    return None
