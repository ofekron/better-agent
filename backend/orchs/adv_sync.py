"""Adversarial sync — drive two role-playing forks to converge on a final text.

Triggered when a user selects text in a chat message and chooses
"Adversarial sync". The backend forks the parent session twice
(current-head, `kind="adv_sync_fork"`), one supportive and one
adversarial of the selected text, and runs a ping-pong loop:

  round 1: both forks receive the selected text + their role brief.
  round 2+: each fork receives the OTHER's prior `<FINAL>...</FINAL>`
            block and is asked to either defend its position or
            update it.

Convergence: identical whitespace-normalized FINAL between supportive
and adversarial AND the FINAL differs from the original selected
text (anti-collusion on the trivial "OK"). Capped at
`MAX_ADV_SYNC_ROUNDS=6`; non-convergence sets status="failed".

Recovery on backend restart: overlays with `status="running"` are
flipped to `"interrupted"` by `recover_running_overlays_on_startup`
since the in-memory driver tasks are gone.

WS protocol: every transition fires the `adv_sync_updated` change
event through `session_manager`; the broadcaster ships the full
post-mutation `adv_sync_overlays` list via `session_metadata_updated`
on the parent session id.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from prompt_templates import render_prompt
from session_manager import manager as session_manager

if TYPE_CHECKING:
    from orchestrator import Coordinator

logger = logging.getLogger(__name__)


MAX_ADV_SYNC_ROUNDS = 6


# ── Convergence parser ────────────────────────────────────────────────


_FINAL_RE = re.compile(
    r"<FINAL>(.*?)</FINAL>",
    re.IGNORECASE | re.DOTALL,
)


def _normalize(text: str) -> str:
    """Whitespace-normalize for convergence comparison.

    Collapses any run of whitespace (including newlines) to a single
    space, strips leading/trailing. Code-fence backticks are stripped
    so a fork that wraps its FINAL in ``` doesn't break agreement.
    """
    if not text:
        return ""
    t = text.replace("`", "")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def extract_final(text: str) -> Optional[str]:
    """Return the LAST `<FINAL>...</FINAL>` block in `text`, or None.

    Last-match wins so a model that quotes its prior draft inside
    reasoning doesn't break convergence on the actual concluding block.
    """
    if not text:
        return None
    matches = _FINAL_RE.findall(text)
    if not matches:
        return None
    return matches[-1]


def is_converged(
    supportive_text: Optional[str],
    adversarial_text: Optional[str],
    original_text: str,
) -> tuple[bool, Optional[str]]:
    """Return (converged, agreed_text).

    Convergence requires:
      - Both forks emitted a FINAL block.
      - The two FINAL blocks are whitespace-normalized equal.
      - The agreed FINAL is NOT equal (normalized) to the original
        text — prevents the trivial "both agree to do nothing".
    """
    if supportive_text is None or adversarial_text is None:
        return False, None
    s_final = extract_final(supportive_text)
    a_final = extract_final(adversarial_text)
    if s_final is None or a_final is None:
        return False, None
    s_norm = _normalize(s_final)
    a_norm = _normalize(a_final)
    if not s_norm or s_norm != a_norm:
        return False, None
    if s_norm == _normalize(original_text):
        return False, None
    # Return the supportive FINAL verbatim (preserving its formatting)
    # since both forks agreed on the same normalized content.
    return True, s_final.strip()


# ── Prompts ───────────────────────────────────────────────────────────


def _role_brief(role: str, original_text: str) -> str:
    """First-round brief for each fork.

    Each fork sees the parent session's full history (they're forked
    off the current head) plus this brief which fixes their role.
    """
    if role == "supportive":
        stance = (
            "You SUPPORT the text below. Defend it: explain why it is "
            "correct, what it gets right, and what's at stake if it "
            "were changed. Use the project context already in this "
            "conversation — read code, files, or prior messages as "
            "needed to ground your defence. If after research you "
            "find a concrete improvement, propose a refined version "
            "that still preserves the original intent."
        )
    else:
        stance = (
            "You ATTACK the text below. Find what is wrong, ambiguous, "
            "imprecise, or out of step with the project. Use the "
            "project context already in this conversation — read code, "
            "files, or prior messages as needed to ground your "
            "critique. Then propose a concrete replacement."
        )
    verb = "DEFEND" if role == "supportive" else "ATTACK"
    return render_prompt(
        "adv_sync/initial.md",
        {
            "role": role,
            "stance": stance,
            "verb": verb,
            "original_text": original_text,
        },
    )


def _exchange_prompt(role: str, original_text: str, other_text: str) -> str:
    """Round 2+ prompt feeding the OTHER fork's last reply back."""
    if role == "supportive":
        instruction = (
            "Your adversarial counterpart just produced the reply "
            "below. Read it. If their critique is valid in places, "
            "update your FINAL to absorb the improvements while "
            "preserving the original intent. If their critique is "
            "wrong, defend more sharply. Either way, end with a "
            "fresh `<FINAL>...</FINAL>` block."
        )
    else:
        instruction = (
            "Your supportive counterpart just produced the reply "
            "below. Read it. If their defence holds up against your "
            "previous critique, update your FINAL to converge toward "
            "theirs. If their defence is weak, attack more sharply. "
            "Either way, end with a fresh `<FINAL>...</FINAL>` block."
        )
    return render_prompt(
        "adv_sync/exchange.md",
        {
            "role": role,
            "instruction": instruction,
            "original_text": original_text,
            "other_text": other_text,
        },
    )


# ── Public API ────────────────────────────────────────────────────────


def _last_assistant_text(session: dict) -> str:
    """Read the most recent assistant message text from a fork session.

    Skips any messages with `source` set (supervisor/worker artifacts)
    so the value reflects the fork's own role-played reply.
    """
    for m in reversed(session.get("messages") or []):
        if m.get("role") != "assistant":
            continue
        if m.get("source"):
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif isinstance(c, str):
                    parts.append(c)
            return "\n".join(parts)
    return ""


async def start_adv_sync(
    coordinator: "Coordinator",
    *,
    parent_session_id: str,
    message_id: str,
    selected_text: str,
) -> dict:
    """Create the two forks, persist the overlay, kick off the driver.

    Returns the overlay record. The ping-pong runs as a background
    asyncio task — the caller (REST handler) returns immediately and
    the WS event stream feeds live status updates.
    """
    parent = session_manager.get(parent_session_id)
    if parent is None:
        raise KeyError(parent_session_id)
    if not selected_text or not selected_text.strip():
        raise ValueError("selected_text empty")
    if not message_id:
        raise ValueError("message_id required")

    # Forks inherit parent's orchestration_mode (manager/native). The
    # ping-pong drives them with mode="native" to skip the MCP wrapper
    # — these role-play sessions shouldn't spawn sub-workers.
    short = selected_text.strip()[:48].replace("\n", " ")
    sup_fork = session_manager.fork(
        parent_session_id,
        name=f"adv-sync ⊕ {short}",
        kind="adv_sync_fork",
    )
    adv_fork = session_manager.fork(
        parent_session_id,
        name=f"adv-sync ⊖ {short}",
        kind="adv_sync_fork",
    )

    overlay_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    overlay = {
        "id": overlay_id,
        "message_id": message_id,
        "original_text": selected_text,
        "agreed_text": None,
        "status": "running",
        "supportive_fork_id": sup_fork["id"],
        "adversarial_fork_id": adv_fork["id"],
        "rounds_completed": 0,
        "max_rounds": MAX_ADV_SYNC_ROUNDS,
        "created_at": now,
        "updated_at": now,
        "error": None,
    }
    session_manager.add_adv_sync_overlay(parent_session_id, overlay)

    drivers = _drivers(coordinator)
    if overlay_id in drivers:
        logger.warning("start_adv_sync: overlay %s already running", overlay_id)
        return overlay
    task = asyncio.create_task(
        _run_ping_pong(
            coordinator,
            parent_session_id=parent_session_id,
            overlay_id=overlay_id,
            sup_fork_id=sup_fork["id"],
            adv_fork_id=adv_fork["id"],
            original_text=selected_text,
        ),
        name=f"adv-sync-{overlay_id[:8]}",
    )
    drivers[overlay_id] = task
    task.add_done_callback(lambda _t, _oid=overlay_id: drivers.pop(_oid, None))
    return overlay


async def cancel_adv_sync(
    coordinator: "Coordinator",
    *,
    parent_session_id: str,
    overlay_id: str,
) -> bool:
    """Stop a running ping-pong. Cancels the driver task, cancels any
    in-flight CLI runs on both forks, marks overlay status="stopped".
    Returns True if anything was actually cancelled."""
    drivers = _drivers(coordinator)
    task = drivers.pop(overlay_id, None)
    parent = session_manager.get(parent_session_id) or {}
    overlay = _find_overlay(parent, overlay_id)
    if overlay is None:
        return False
    if task is not None and not task.done():
        task.cancel()
    sup_id = overlay.get("supportive_fork_id")
    adv_id = overlay.get("adversarial_fork_id")
    for fid in (sup_id, adv_id):
        if fid:
            try:
                await coordinator.cancel_session(fid)
            except Exception:
                logger.exception("cancel_adv_sync: cancel_session %s failed", fid)
    session_manager.update_adv_sync_overlay(
        parent_session_id, overlay_id, {"status": "stopped"},
    )
    return True


def recover_running_overlays_on_startup() -> int:
    """Walk every session on disk; flip overlays stuck in `running` to
    `interrupted`. Called once at backend startup before any new turns
    run, so the disk state matches the in-memory truth (no driver
    tasks exist after restart).
    """
    flipped = session_manager.recover_running_adv_sync_overlays()
    if flipped:
        logger.info("adv_sync recovery: flipped %d overlay(s) → interrupted", flipped)
    return flipped


# ── Internal driver ───────────────────────────────────────────────────


# Module-owned: driver-task map keyed by overlay id. Used to be lazy-
# stashed on the coordinator; subsystem owns its own state now.
_driver_tasks: dict = {}


def _drivers(coordinator: "Coordinator") -> dict:
    """Driver-task map keyed by overlay id. `coordinator` is unused but
    kept in the signature so call sites don't have to thread None — the
    map is process-singleton like other registries.
    """
    _ = coordinator
    return _driver_tasks


def _find_overlay(session: dict, overlay_id: str) -> Optional[dict]:
    for ov in session.get("adv_sync_overlays") or []:
        if ov.get("id") == overlay_id:
            return ov
    return None


async def _run_one_turn(
    coordinator: "Coordinator",
    *,
    fork_id: str,
    prompt: str,
) -> str:
    """Run a single role-play turn on `fork_id` and return its last
    assistant text. Uses a registry-based ws_callback so any client
    subscribed to this fork id receives live events (mirroring the
    per-session-processor's dispatch_ws pattern).
    """
    fork = session_manager.get(fork_id)
    if fork is None:
        raise KeyError(fork_id)
    mode_field = session_manager.agent_sid_field_for_mode(
        fork.get("orchestration_mode") or "team"
    )

    async def dispatch_ws(event_dict, _sid=fork_id):
        await coordinator.dispatch_raw(_sid, event_dict)

    await coordinator.turn_manager.run_turn(
        session=fork,
        prompt=prompt,
        cli_prompt=prompt,
        app_session_id=fork_id,
        model=fork.get("model") or "",
        cwd=fork.get("cwd") or "",
        ws_callback=dispatch_ws,
        images=None,
        trace_step_name="adv_sync_turn",
        session_id_field=mode_field,
        mode="native",
        source="adv_sync",
    )
    fresh = session_manager.get(fork_id) or fork
    return _last_assistant_text(fresh)


async def _run_ping_pong(
    coordinator: "Coordinator",
    *,
    parent_session_id: str,
    overlay_id: str,
    sup_fork_id: str,
    adv_fork_id: str,
    original_text: str,
) -> None:
    """Alternate turns between the two forks until convergence or cap."""
    sup_last: Optional[str] = None
    adv_last: Optional[str] = None
    try:
        for round_idx in range(1, MAX_ADV_SYNC_ROUNDS + 1):
            # Supportive turn first each round.
            if round_idx == 1:
                sup_prompt = _role_brief("supportive", original_text)
            else:
                # adv_last is non-None at this point (round 1 set it).
                sup_prompt = _exchange_prompt(
                    "supportive", original_text, adv_last or "",
                )
            sup_last = await _run_one_turn(
                coordinator, fork_id=sup_fork_id, prompt=sup_prompt,
            )

            # Adversarial turn — sees supportive's reply this round.
            if round_idx == 1:
                adv_prompt = _role_brief("adversarial", original_text)
                # Round 1: adversarial doesn't yet have supportive's
                # reply to attack — give it the original text only,
                # same as supportive. Round 2+ folds supportive's
                # latest in via _exchange_prompt.
            else:
                adv_prompt = _exchange_prompt(
                    "adversarial", original_text, sup_last or "",
                )
            adv_last = await _run_one_turn(
                coordinator, fork_id=adv_fork_id, prompt=adv_prompt,
            )

            session_manager.update_adv_sync_overlay(
                parent_session_id, overlay_id,
                {"rounds_completed": round_idx},
            )

            converged, agreed = is_converged(
                sup_last, adv_last, original_text,
            )
            if converged:
                session_manager.update_adv_sync_overlay(
                    parent_session_id, overlay_id,
                    {
                        "status": "converged",
                        "agreed_text": agreed,
                        "rounds_completed": round_idx,
                    },
                )
                return

        # Loop finished without convergence.
        session_manager.update_adv_sync_overlay(
            parent_session_id, overlay_id,
            {"status": "failed", "error": "max rounds reached without convergence"},
        )
    except asyncio.CancelledError:
        # Cancellation flow sets status=stopped elsewhere (cancel_adv_sync).
        # Re-raise so the task is cleanly cancelled.
        raise
    except Exception as e:
        logger.exception("adv_sync ping-pong crashed: %s", e)
        session_manager.update_adv_sync_overlay(
            parent_session_id, overlay_id,
            {"status": "failed", "error": str(e)[:500]},
        )
