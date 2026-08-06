"""Ingestion boundary for proxy-observed model-traffic thread facts
(Phase G — parallel, flag-gated path; not yet consumed by any adapter).

The `ofek-dev.model-traffic` extension (better-agent-private) observes raw
provider HTTP traffic through its MITM/gateway interception and mints
wire-level `thread_id`s (session-header + prefix-chain clustering, see
`proxy-research.md`'s "Thread attribution deep-dive"). It has no way to
mutate core's event bus directly — this module is the smallest addition
that lets it push what it observed in as ordinary bus facts, over the
extension's EXISTING authenticated channel into core
(`better_agent_sdk.Client._post`, the same `X-Internal-Token` /
`/api/internal/*` pattern every other extension-to-core call already uses
— see `backend/internal_messaging_api.py` for the sibling handlers this
one mirrors).

Gated by `BA_SURFACE_TRAFFIC_SOURCE` (default OFF): `backend/main.py` only
mounts this router when the flag is on, so with it off the route does not
exist at all (a plain 404, not "exists but refuses") — same shape as the
`node_link` conditional mount in main.py. The handler re-checks the flag
itself too, so a standalone test (or a future caller that mounts this
router without going through main.py's gate) gets the same 404 rather
than silently accepting facts.

Republishes each validated fact as a `persist=False` BusEvent — a
backend-internal notification with no durable session record backing it
(the extension's own capture store is where the raw traffic still lives).
This file owns no state, performs no adapter-layer composition, and is
not a consumer: nothing folds these facts into the render tree yet. It
must stay outside `backend/adapters/` and import nothing from it — see
`backend/scripts/test_adapter_boundaries.py`, which only allows
`backend/main.py`, `backend/adapter_api.py`, and `backend/surface_commands.py`
to import `backend.adapters`.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException

import internal_guards
from event_bus import BusEvent, bus, register_event_schema
from i18n import t

router = APIRouter()

ENV_FLAG = "BA_SURFACE_TRAFFIC_SOURCE"

# Free-form dot-namespaced fact types (event_bus.py has no central type
# registry — see EventBus.publish's fnmatch-glob routing); these four are
# this module's own vocabulary, declared once at import like every other
# fact producer (see backend/provider_runtime_facts.py).
THREAD_STARTED = "traffic.thread_started"
THREAD_REQUEST = "traffic.thread_request"
THREAD_COMPLETED = "traffic.thread_completed"
THREAD_JOINED = "traffic.thread_joined"
_FACT_TYPES = (THREAD_STARTED, THREAD_REQUEST, THREAD_COMPLETED, THREAD_JOINED)

for _type in _FACT_TYPES:
    register_event_schema(_type, 1)

_MAX_FACTS_PER_CALL = 64
_MAX_STRING_LEN = 512
_MAX_TS_LEN = 64
_MAX_USAGE_KEYS = 16


def feature_enabled() -> bool:
    return os.environ.get(ENV_FLAG) == "1"


def _clean_str(value: Any, *, field: str, max_len: int = _MAX_STRING_LEN) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError(f"{field} must be a non-empty string of at most {max_len} chars")
    return value


def _clean_optional_str(value: Any, *, field: str, max_len: int = _MAX_STRING_LEN) -> str | None:
    if value is None:
        return None
    return _clean_str(value, field=field, max_len=max_len)


def _clean_usage(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > _MAX_USAGE_KEYS:
        raise ValueError("usage must be an object with at most " f"{_MAX_USAGE_KEYS} keys")
    for key, inner in value.items():
        if not isinstance(key, str) or not isinstance(inner, (int, float, str, bool, type(None))):
            raise ValueError("usage values must be scalar")
    return value


def _fact_to_event(raw: Any) -> BusEvent:
    if not isinstance(raw, dict):
        raise ValueError("each fact must be an object")
    fact_type = raw.get("type")
    if fact_type not in _FACT_TYPES:
        raise ValueError(f"unknown fact type {fact_type!r}")
    thread_id = _clean_str(raw.get("thread_id"), field="thread_id")
    payload: dict[str, Any] = {"thread_id": thread_id}
    wire_session_id = _clean_optional_str(raw.get("wire_session_id"), field="wire_session_id")
    if wire_session_id is not None:
        payload["wire_session_id"] = wire_session_id
    parent_thread_id = _clean_optional_str(raw.get("parent_thread_id"), field="parent_thread_id")
    if parent_thread_id is not None:
        payload["parent_thread_id"] = parent_thread_id
    provider_id = _clean_optional_str(raw.get("provider_id"), field="provider_id")
    if provider_id is not None:
        payload["provider_id"] = provider_id
    model = _clean_optional_str(raw.get("model"), field="model")
    if model is not None:
        payload["model"] = model
    usage = _clean_usage(raw.get("usage"))
    if usage is not None:
        payload["usage"] = usage
    ts = _clean_optional_str(raw.get("ts"), field="ts", max_len=_MAX_TS_LEN)
    if ts is not None:
        payload["ts"] = ts
    return BusEvent(type=fact_type, root_id="", sid=thread_id, payload=payload, persist=False)


@router.post("/api/internal/traffic-facts")
async def post_traffic_facts(
    body: dict[str, Any],
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
) -> dict[str, Any]:
    if not feature_enabled():
        raise HTTPException(status_code=404, detail="traffic facts ingestion is disabled")
    if not internal_guards.authority_is_valid():
        raise HTTPException(status_code=403, detail=t("error.invalid_internal_token"))
    facts = (body or {}).get("facts")
    if not isinstance(facts, list) or not facts:
        raise HTTPException(status_code=422, detail="facts must be a non-empty list")
    if len(facts) > _MAX_FACTS_PER_CALL:
        raise HTTPException(status_code=422, detail=f"at most {_MAX_FACTS_PER_CALL} facts per call")
    try:
        events = [_fact_to_event(raw) for raw in facts]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    for event in events:
        await bus.publish(event)
    return {"accepted": len(events)}
