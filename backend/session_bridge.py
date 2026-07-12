"""Session Bridge — cross-session delegate, usable by ANY session.

`search_sessions` (the cross-session search tool) is implemented in
`runner.py` + `main.py` as a search worker (`session_search.run_search_sessions_session`),
NOT here. This module owns only the delegate capability, exposed to
genuine user-facing turns via the `session-bridge` SDK MCP server (built
in `runner.py`):

  - `delegate(...)` — run a prompt against a chosen session (fork or
    continue) or a brand-new session, and return its final message.
    Gated by the Ask session picker unless `auto` is explicitly enabled
    AND the run is a `fork` targeting an existing session. New-session
    creation always goes through the picker so the user can review the
    full prompt.

Security (CLAUDE.md, fail closed):
  - Only listable sessions are valid delegate targets; unknown/hidden/
    singleton ids are rejected.
  - `approval:"auto"` runs without a picker ONLY when the user_prefs
    flag `cross_session_delegate_auto` is ON *and* `run_mode == "fork"`.
    `continue` mutates an existing session in place and ALWAYS goes
    through the picker. Any ambiguity → picker.
  - The picker-resolve path only accepts a chosen id that was actually
    proposed (no injecting an arbitrary target at approval time).

The delegation runs on the plain `submit_prompt` path (NOT the manager
`run_delegation` dance) and preserves the target's own orchestration
mode — it never adds manager orchestration to the target.
"""

import asyncio
import logging
import uuid
from typing import Any, Optional

import config_store
from event_bus import bus
import session_search
import user_prefs
from session_manager import manager as session_manager

logger = logging.getLogger(__name__)

# Match the manager-delegate budget: a delegation may block on user
# approval (picker) for a long time before the turn even starts.
_TURN_TIMEOUT = 24 * 3600

# Sentinel for "create a new session" in the picker flow.
_NEW_SESSION_SENTINEL = "__new__"


# ── text helpers ────────────────────────────────────────────────────────

def _msg_text(m: dict) -> str:
    content = (m or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts).strip()
    return ""


def _last_assistant(sid: str) -> Optional[dict]:
    sess = session_manager.get(sid) or {}
    for m in reversed(sess.get("messages") or []):
        if m.get("role") == "assistant":
            return m
    return None


# ── delegate ────────────────────────────────────────────────────────────

# delegation_id -> {future, caller_sid, target_sid, prompt, run_mode, proposed_ids}
_pending: dict[str, dict] = {}


class BridgeError(Exception):
    """Recoverable error surfaced to the delegate tool as is_error text."""


async def _run_turn(
    sid: str,
    prompt: str,
    *,
    display_prompt: str | None = None,
    source: str | None = None,
    client_id: str | None = None,
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
) -> dict:
    """Submit `prompt` to session `sid`, await turn completion, return the
    final assistant message text + id. Reuses session_search's in-process
    turn driver (register_ws watermark + lifecycle Future)."""
    from main import coordinator as _coordinator
    from event_ingester import event_ingester as _ingester

    config_store.apply_env_vars()
    sess = session_manager.get(sid) or {}
    lifecycle_msg_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    subscriber_name = f"session_bridge_run_turn_{lifecycle_msg_id}"

    def _finish_from_frame(frame: dict[str, Any]) -> None:
        if not done.done():
            done.set_result(frame)

    async def _ws(frame: dict[str, Any]) -> None:
        f = frame or {}
        if f.get("type") not in ("user_message_done", "user_message_failed"):
            return
        if (f.get("data") or {}).get("lifecycle_msg_id") != lifecycle_msg_id:
            return
        _finish_from_frame(f)

    async def _lifecycle(event) -> None:
        if event.sid != sid or event.msg_id != lifecycle_msg_id:
            return
        if event.type not in ("user_message_done", "user_message_failed"):
            return
        _finish_from_frame({
            "type": event.type,
            "data": event.payload or {},
        })

    seq_map = _ingester.max_seq_by_sid(sid)
    from_seq = max(seq_map.values(), default=0) if seq_map else 0
    _coordinator.register_ws(sid, _ws, from_seq=from_seq)
    bus.subscribe(
        "user_message_*",
        _lifecycle,
        priority=90,
        name=subscriber_name,
    )
    try:
        _coordinator.submit_prompt(sid, {
            "prompt": display_prompt or prompt,
            "app_session_id": sid,
            "provider_id": provider_id or sess.get("provider_id") or "",
            "model": model or sess.get("model"),
            "reasoning_effort": reasoning_effort or sess.get("reasoning_effort") or "",
            "allow_model_override": bool(provider_id or model or reasoning_effort),
            "cwd": sess.get("cwd"),
            "ws_callback": _ws,
            "lifecycle_msg_id": lifecycle_msg_id,
            "client_id": client_id,
            "cli_prompt": prompt,
            "source": source,
            "orchestration_mode": sess.get("orchestration_mode"),
            # Delegate-tool runs leave user_initiated at its True default (the
            # picker was user-approved) but the prompt itself is agent-authored,
            # so a continuation must label it as agent, not user.
            "prompt_origin": "agent",
        })
        frame = await asyncio.wait_for(done, timeout=_TURN_TIMEOUT)
    finally:
        bus.unsubscribe(subscriber_name)
        _coordinator.unregister_ws(sid, _ws)

    fdata = (frame or {}).get("data") or {}
    if frame.get("type") == "user_message_failed" or fdata.get("success") is False:
        return {"error": fdata.get("error") or "turn_failed"}

    # Safe to read "last assistant": fork targets are private children
    # (no concurrent writer) and `continue` targets were rejected if busy
    # (see `_run`), so the turn we just awaited produced the last message.
    m = _last_assistant(sid)
    return {"text": _msg_text(m) if m else "", "turn_id": (m or {}).get("id", "")}


def _resolve_bridge_run_config(
    *,
    caller: dict,
    target: dict | None = None,
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
) -> dict[str, str]:
    provider_id = str(provider_id or "").strip()
    model = str(model or "").strip()
    reasoning_effort = str(reasoning_effort or "").strip()
    assignment = config_store.get_internal_llm_task("delegation_session_bridge")
    if assignment:
        resolved = config_store.resolve_internal_llm("delegation_session_bridge")
        provider_id = provider_id or str(resolved.get("provider_id") or "").strip()
        model = model or str(resolved.get("model") or "").strip()
        reasoning_effort = (
            reasoning_effort
            or str(resolved.get("reasoning_effort") or "").strip()
        )
    if provider_id and not model:
        provider = config_store.get_provider(provider_id) or {}
        model = str(provider.get("default_model") or "").strip()
    target = target or {}
    return {
        "provider_id": provider_id or str(target.get("provider_id") or caller.get("provider_id") or "").strip(),
        "model": model or str(target.get("model") or caller.get("model") or "").strip(),
        "reasoning_effort": reasoning_effort or str(target.get("reasoning_effort") or caller.get("reasoning_effort") or "").strip(),
    }


async def run_for_extension(target_sid: str, prompt: str, *, source: str) -> dict:
    """Public entry for a trusted builtin extension to deliver a prompt to an
    existing session and run its turn (continue mode). Unlike `delegate`, there
    is no caller turn — the trigger is a direct user action surfaced by the
    extension. Refuses if the target is busy (continue-mode contract)."""
    return await _run(target_sid, prompt, "continue", source=source)


async def _run(
    target_sid: str,
    prompt: str,
    run_mode: str,
    *,
    display_prompt: str | None = None,
    source: str | None = None,
    client_id: str | None = None,
    caller_sid: str = "",
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
) -> dict:
    caller = session_manager.get(caller_sid) or {}
    target_session = session_manager.get(target_sid) or {}
    run_config = _resolve_bridge_run_config(
        caller=caller,
        target=target_session,
        provider_id=provider_id,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    if run_mode == "fork":
        config_store.apply_env_vars()
        try:
            # This fork is spawned by an agent delegation into another
            # session, not by the user opening the Fork action.
            child = session_manager.fork(target_sid, user_initiated=False)
        except KeyError:
            return {"error": "unknown_session"}
        except ValueError as e:
            return {"error": str(e)}
        run_sid = child["id"]
    else:
        # `continue` appends to a live session — refuse if a turn is already
        # in flight there. Avoids a 24h block waiting behind the other turn
        # AND the wrong-turn "last assistant" read (the only correlation we
        # have post-turn is order, since the done frame carries no msg id).
        if _target_busy(target_sid):
            return {"error": "target_busy"}
        run_sid = target_sid

    final = await _run_turn(
        run_sid,
        prompt,
        display_prompt=display_prompt,
        source=source,
        client_id=client_id,
        provider_id=run_config.get("provider_id") or "",
        model=run_config.get("model") or "",
        reasoning_effort=run_config.get("reasoning_effort") or "",
    )
    if final.get("error"):
        return {"error": final["error"]}
    return {
        "session_id": run_sid,
        "run_mode": run_mode,
        "final_message": final.get("text", ""),
        "turn_id": final.get("turn_id", ""),
    }


async def _run_new(
    caller_sid: str,
    prompt: str,
    *,
    display_prompt: str | None = None,
    source: str | None = None,
    client_id: str | None = None,
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
) -> dict:
    """Create a brand-new session inheriting the caller's config and run
    the prompt in it. Returns the same shape as `_run`."""
    caller = session_manager.get(caller_sid) or {}
    run_config = _resolve_bridge_run_config(
        caller=caller,
        provider_id=provider_id,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    config_store.apply_env_vars()
    sess = session_manager.create(
        name=f"Delegated from {caller.get('name', '') or 'session'}".strip()[:80],
        model=run_config.get("model") or caller.get("model", ""),
        cwd=caller.get("cwd", ""),
        orchestration_mode=caller.get("orchestration_mode", "native"),
        provider_id=run_config.get("provider_id") or caller.get("provider_id"),
        reasoning_effort=run_config.get("reasoning_effort") or caller.get("reasoning_effort"),
        # New-session mode always reaches here only after the user approves
        # the picker, so this session is user-aware.
        user_initiated=True,
    )
    run_sid = sess["id"]
    final = await _run_turn(
        run_sid,
        prompt,
        display_prompt=display_prompt,
        source=source,
        client_id=client_id,
        provider_id=run_config.get("provider_id") or "",
        model=run_config.get("model") or "",
        reasoning_effort=run_config.get("reasoning_effort") or "",
    )
    if final.get("error"):
        return {"error": final["error"]}
    return {
        "session_id": run_sid,
        "run_mode": "new",
        "final_message": final.get("text", ""),
        "turn_id": final.get("turn_id", ""),
    }


async def _await_picker(
    caller_sid: str,
    caller_msg_id: str,
    target_sid: str,
    prompt: str,
    run_mode: str,
    *,
    create_new: bool = False,
) -> dict[str, Optional[str]]:
    """Stamp a `delegate_approval` picker on the caller's in-flight
    assistant message and block until the user approves (returns the
    target sid or the new-session sentinel) or cancels/times-out.
    Cancellation may include user feedback for the calling agent. When
    `create_new` is True the picker shows a
    "Create new session" action instead of a session list."""
    delegation_id = "sbd_" + uuid.uuid4().hex[:12]
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    proposed_ids = [_NEW_SESSION_SENTINEL] if create_new else [target_sid]

    _pending[delegation_id] = {
        "future": fut,
        "caller_sid": caller_sid,
        "caller_msg_id": caller_msg_id,
        "target_sid": target_sid,
        "prompt": prompt,
        "run_mode": run_mode,
        "proposed_ids": proposed_ids,
    }
    result: dict[str, Any] = {
        "purpose": "delegate_approval",
        "session_ids": [] if create_new else [target_sid],
        "reasoning": "",
        "delegation_id": delegation_id,
        "run_mode": run_mode,
        "prompt_preview": prompt,
        "create_new": create_new,
        "proposed_project_path": "",
        "proposed_project_node_id": "",
    }
    session_manager.set_msg_ask_result(caller_sid, caller_msg_id, result)
    try:
        return await asyncio.wait_for(fut, timeout=_TURN_TIMEOUT)
    except asyncio.TimeoutError:
        _mark_picker_resolved(delegation_id, None, "")
        return {"chosen_session_id": None, "cancellation_text": ""}
    finally:
        _pending.pop(delegation_id, None)


def _mark_picker_resolved(
    delegation_id: str,
    chosen_session_id: Optional[str],
    cancellation_text: str,
) -> None:
    """Re-stamp the caller's `delegate_approval` ask_result as resolved so
    every open tab's footer clears (broadcasts `message_ask_result_changed`).
    No-op if the pending record is gone."""
    rec = _pending.get(delegation_id)
    if not rec:
        return
    is_new = _NEW_SESSION_SENTINEL in rec.get("proposed_ids", [])
    session_manager.set_msg_ask_result(
        rec["caller_sid"],
        rec["caller_msg_id"],
        {
            "purpose": "delegate_approval",
            "session_ids": [] if is_new else rec["proposed_ids"],
            "reasoning": "",
            "delegation_id": delegation_id,
            "run_mode": rec["run_mode"],
            "resolved": True,
            "chosen_session_id": chosen_session_id or "",
            "cancellation_text": cancellation_text,
            "create_new": is_new,
            "proposed_project_path": "",
            "proposed_project_node_id": "",
        },
    )


def resolve_delegation(
    delegation_id: str,
    chosen_session_id: Optional[str],
    cancellation_text: str = "",
) -> bool:
    """Frontend picker callback. `chosen_session_id=None` cancels and may
    carry user feedback for the calling agent.
    Returns False (no-op) for unknown/already-resolved ids, or when the
    chosen id was not among the proposed set (fail closed)."""
    rec = _pending.get(delegation_id)
    if not rec:
        return False
    fut = rec["future"]
    if fut.done():
        return False
    if chosen_session_id is not None and chosen_session_id not in rec["proposed_ids"]:
        return False
    if not isinstance(cancellation_text, str) or len(cancellation_text) > 10_000:
        return False
    cancellation_text = cancellation_text.strip()
    _mark_picker_resolved(delegation_id, chosen_session_id, cancellation_text)
    fut.set_result({
        "chosen_session_id": chosen_session_id,
        "cancellation_text": cancellation_text,
    })
    return True


def _caller_in_flight_msg_id(caller_sid: str) -> Optional[str]:
    """The id of the caller turn's in-flight assistant message, or None.

    Defense-in-depth: every delegate path requires the caller to be a
    genuine in-flight turn (the runner only hands the tool to user-facing
    turns; this re-checks server-side so a stray internal-token holder
    can't drive a delegation for a session with no live turn). Used for
    BOTH the picker stamp target AND the auto-path gate, so `auto` is not
    weaker than `require`."""
    from main import coordinator as _coordinator

    in_flight = _coordinator.turn_manager.get_in_flight_assistant_msg(caller_sid)
    if not in_flight or not in_flight.get("id"):
        return None
    return in_flight["id"]


def _target_busy(target_sid: str) -> bool:
    from main import coordinator as _coordinator

    return bool(_coordinator.turn_manager.get_in_flight_assistant_msg(target_sid))


def _target_is_registered_worker(caller_sid: str, target_sid: str) -> bool:
    try:
        from stores import worker_store
        return worker_store.get_worker("", target_sid) is not None
    except Exception:
        logger.debug(
            "session_bridge: registered-worker check failed",
            exc_info=True,
        )
        return False


async def delegate(
    *,
    caller_sid: str,
    target_sid: str,
    prompt: str,
    run_mode: str,
    approval: str,
    display_prompt: str | None = None,
    source: str | None = None,
    client_id: str | None = None,
    provider_id: str = "",
    model: str = "",
    reasoning_effort: str = "",
) -> dict:
    """Entry point for the `delegate_to_session` MCP tool. Returns either
    `{session_id, run_mode, final_message, turn_id}` or `{error: ...}`.
    Empty `target_sid` triggers new-session creation mode."""
    prompt = (prompt or "").strip()
    if not prompt:
        return {"error": "prompt_required"}
    if run_mode not in ("fork", "continue"):
        return {"error": "invalid_run_mode"}
    if approval not in ("auto", "require"):
        return {"error": "invalid_approval"}
    if not isinstance(caller_sid, str) or not caller_sid:
        return {"error": "missing_caller"}

    is_new_session = not target_sid
    known_sessions = (
        {}
        if is_new_session
        else await asyncio.to_thread(session_search.index_stub_map)
    )
    if not is_new_session and target_sid not in known_sessions:
        return {"error": "unknown_session"}

    # Fail closed: the caller MUST be a live in-flight turn — gates every
    # path (auto included), closing the auto-bypass / confused-deputy hole.
    caller_msg_id = _caller_in_flight_msg_id(caller_sid)
    if not caller_msg_id:
        return {"error": "caller_not_in_flight"}

    if is_new_session:
        # New-session creation always requires picker approval (even with
        # auto flag) so the user can review the full prompt.
        try:
            resolution = await _await_picker(
                caller_sid, caller_msg_id, "", prompt, run_mode,
                create_new=True,
            )
        except BridgeError as e:
            return {"error": str(e)}
        if not resolution["chosen_session_id"]:
            return {
                "error": "cancelled",
                "cancelled": True,
                "user_feedback": resolution["cancellation_text"],
            }
        return await _run_new(
            caller_sid,
            prompt,
            display_prompt=display_prompt,
            source=source,
            client_id=client_id,
            provider_id=provider_id,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    auto_ok = (
        approval == "auto"
        and run_mode == "fork"
        and (
            user_prefs.get_cross_session_delegate_auto()
            or _target_is_registered_worker(caller_sid, target_sid)
        )
    )
    if auto_ok:
        return await _run(
            target_sid,
            prompt,
            run_mode,
            display_prompt=display_prompt,
            source=source,
            client_id=client_id,
            caller_sid=caller_sid,
            provider_id=provider_id,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    try:
        resolution = await _await_picker(
            caller_sid, caller_msg_id, target_sid, prompt, run_mode
        )
    except BridgeError as e:
        return {"error": str(e)}
    chosen = resolution["chosen_session_id"]
    if chosen is None:
        return {
            "error": "cancelled",
            "cancelled": True,
            "user_feedback": resolution["cancellation_text"],
        }
    return await _run(
        chosen,
        prompt,
        run_mode,
        display_prompt=display_prompt,
        source=source,
        client_id=client_id,
        caller_sid=caller_sid,
        provider_id=provider_id,
        model=model,
        reasoning_effort=reasoning_effort,
    )
