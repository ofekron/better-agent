"""Gate for sessions awaiting a provisioned fork binding.

A file-editing session is created and returned to the client BEFORE its
provisioned base is warm, so `POST /api/sessions` does not block on a
20-28s provider warm-up. Until that warm completes the session's
`forked_from_agent_sid` is unbound — and that field is the ONLY signal
`turn_manager` uses to run turn 1 with `--fork-session`. Dispatching a
turn before it lands starts a brand-new UNFORKED provider session in the
user's project cwd without the provisioned tool profile, which loses the
provisioned context AND widens the session's privileges. So the gate is
authoritative and fail-closed: while a binding is pending, prompts wait;
if it fails, prompts fail rather than run unforked.

The fact is deliberately narrow. Presence of `fork_provisioning` on the
session record means "awaiting a provisioned fork binding"; absence means
nothing is pending. It is not a general-purpose session-readiness flag —
a generic field would hand every future caller a fail-open default.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from session_manager import manager as session_manager

logger = logging.getLogger(__name__)

FIELD = "fork_provisioning"

STATE_PENDING = "pending"
STATE_FAILED = "failed"

# Resolution outcomes returned by `wait_until_resolved`.
RESOLVED_READY = "ready"
RESOLVED_FAILED = "failed"

# Upstream only bounds the lifecycle-lock ACQUIRE; the provision turn
# itself (`_init_target_agent_session`) is unbounded, so a wedged provider
# CLI would otherwise park a queued prompt forever with no failure. A warm
# runs 20-30s healthy; this is generous headroom for a loaded machine
# while still resolving to a visible failure rather than silence.
WARM_TIMEOUT_S = 300.0

_MAX_ERROR_CHARS = 200

# One resolution event per pending sid. Popped on resolution: a waiter
# that already holds the object still sees it set, and a later waiter
# reads the durable record (no longer pending) and never needs it.
_EVENTS: dict[str, asyncio.Event] = {}


def pending_fact() -> dict:
    """The value stamped on a session at create time. Written atomically
    with the session record so no window exists where the session is
    dispatchable but unbound."""
    return {
        "state": STATE_PENDING,
        "error": None,
        "updated_at": datetime.now().isoformat(),
    }


def state_of(session: Optional[dict]) -> Optional[str]:
    """Pending/failed state carried by a session record, or None when the
    session has no provisioned-fork binding outstanding."""
    if not session:
        return None
    fact = session.get(FIELD)
    if not isinstance(fact, dict):
        return None
    state = fact.get("state")
    return state if state in (STATE_PENDING, STATE_FAILED) else None


def _state_for_sid(sid: str) -> Optional[str]:
    """Synchronous BY DESIGN — do not convert to `asyncio.to_thread`.

    `wait_until_resolved` reads this and then registers on the resolution
    event with no await in between, which is what closes the lost-wakeup
    window: an await here would let `_wake` pop the event between the read
    and the `.wait()`, parking a queued prompt forever.
    """
    if not sid:
        return None
    return state_of(session_manager.get_fields(sid, (FIELD,)))


def gate_state(sid: str) -> str:
    """THE shared readiness read. Tri-state on purpose: a boolean
    "is pending" reads naturally as "otherwise runnable" and so falls OPEN
    on FAILED, releasing a prompt into an unforked dispatch — the exact
    failure this module exists to prevent. Every call site switches on all
    three outcomes.
    """
    state = _state_for_sid(sid)
    if state == STATE_PENDING:
        return STATE_PENDING
    if state == STATE_FAILED:
        return STATE_FAILED
    return RESOLVED_READY


def is_awaiting_fork_provisioning(sid: str) -> bool:
    """Admission-gate helper: should a prompt sent right now be reported
    as queued? True only while pending — a FAILED session's prompt is not
    queued, it is rejected by the dispatch gate."""
    return gate_state(sid) == STATE_PENDING


def fork_provisioning_failed(sid: str) -> bool:
    return gate_state(sid) == STATE_FAILED


def failure_reason(sid: str) -> Optional[str]:
    """Sanitized reason a provisioned fork binding failed, for surfacing
    on the prompt that can no longer run."""
    fact = (session_manager.get_fields(sid, (FIELD,)) or {}).get(FIELD)
    if not isinstance(fact, dict) or fact.get("state") != STATE_FAILED:
        return None
    return fact.get("error") or None


def _event_for(sid: str) -> asyncio.Event:
    event = _EVENTS.get(sid)
    if event is None:
        event = asyncio.Event()
        _EVENTS[sid] = event
    return event


async def wait_until_resolved(sid: str) -> str:
    """Block until `sid`'s provisioned fork binding resolves, then report
    how it resolved. Purely event-driven — the caller is woken by the
    binding itself, never by a poll or a sleep.

    Re-reads the durable record before waiting so a resolution that
    landed between the caller's check and this call cannot be missed.
    """
    state = _state_for_sid(sid)
    if state != STATE_PENDING:
        return RESOLVED_FAILED if state == STATE_FAILED else RESOLVED_READY
    await _event_for(sid).wait()
    return (
        RESOLVED_FAILED
        if _state_for_sid(sid) == STATE_FAILED
        else RESOLVED_READY
    )


def _wake(sid: str) -> None:
    event = _EVENTS.pop(sid, None)
    if event is not None:
        event.set()


async def mark_ready(sid: str) -> None:
    """Clear the pending fact once the binding is committed. Call only
    AFTER `forked_from_agent_sid` is persisted — waking waiters first
    would let a turn dispatch unforked."""
    await asyncio.to_thread(session_manager.set_fork_provisioning, sid, None)
    _wake(sid)


async def mark_failed(sid: str, error: BaseException | str) -> None:
    """Record that provisioning failed and wake waiters so they fail the
    queued work loudly instead of hanging or running it unforked."""
    await asyncio.to_thread(
        session_manager.set_fork_provisioning,
        sid,
        {
            "state": STATE_FAILED,
            "error": _sanitize_error(error),
            "updated_at": datetime.now().isoformat(),
        },
    )
    _wake(sid)


async def mark_pending(sid: str) -> None:
    """Re-arm the gate for another provisioning attempt. Waiters are NOT
    woken — a fresh attempt is exactly what they are waiting for."""
    await asyncio.to_thread(
        session_manager.set_fork_provisioning, sid, pending_fact(),
    )


def _sanitize_error(error: BaseException | str) -> str:
    """Provider failures can carry command lines and environment detail.
    Keep the type and a truncated message so the UI stays truthful
    without leaking secrets into session state."""
    if isinstance(error, BaseException):
        text = f"{type(error).__name__}: {error}"
    else:
        text = str(error)
    text = " ".join(text.split())
    if len(text) > _MAX_ERROR_CHARS:
        return text[:_MAX_ERROR_CHARS] + "…"
    return text


def forget(sid: str) -> None:
    """Drop any resolution event for a session that is going away."""
    _EVENTS.pop(sid, None)
