"""Supervisor verdict — ask the supervisor whether the primary's output is done.

After the primary agent (manager or native) completes a turn on the
user-facing session, the supervisor (a separate Claude session living
in the same session record under `supervisor_agent_session_id`) reviews
the primary's output against the original user request and returns one
of:

  - DONE — primary fully addressed the request, OR correctly handed
    back to the user (asked a real-ambiguity clarification, requested
    approval on a decision the user owns). Handoff IS a valid completion.
  - AWAIT_USER: <what user must answer> — primary legitimately blocked
    on user input. Loop terminates; the question is surfaced to the user.
  - CONTINUE: <instructions> — primary is on track but unfinished;
    a specific piece is missing with evidence.
  - FIX: <instructions> — primary did something specifically wrong;
    cite the defect.

The verdict is run through ``coordinator.turn_manager.run_turn`` with
``session_id_field="supervisor_agent_session_id"`` so the supervisor's
prompt and response are persisted to the same session record under the
supervisor sid slot, tagged ``source="supervisor"`` so the frontend
routes them to the supervisor panel.

The verdict loop lives in ``maybe_run_verdict_loop`` (orchs/supervisor/
__init__.py) — it runs the primary, asks the supervisor, and feeds
CONTINUE/FIX instructions back as another PRIMARY turn (manager or
native, whichever the session uses). DONE and AWAIT_USER terminate the
loop. Up to ``MAX_VERDICTS_PER_TURN`` cycles per user prompt.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from i18n import t
from orchs.jsonl_helpers import compute_jsonl_path, count_jsonl_lines
from orchs.supervisor._primary import run_primary_turn
from prompt_templates import render_prompt
from session_manager import manager as session_manager
from prompt_templates import render_prompt

if TYPE_CHECKING:
    from orchestrator import Coordinator


async def _run_supervisor_turn(
    coordinator: "Coordinator",
    *,
    session: dict,
    prompt: str,
    app_session_id: str,
    ws_callback: Callable[[dict], Awaitable[None]],
    trace_step_name: str,
) -> None:
    """Single source for supervisor run_turn invocations.
    INVARIANT: always runs as native-mode primary on the session's
    supervisor sid slot — never as manager/supervisor mode itself.
    """
    await coordinator.turn_manager.run_turn(
        session=session,
        prompt=prompt,
        cli_prompt=prompt,
        app_session_id=app_session_id,
        model=session.get("model") or "",
        cwd=session.get("cwd") or "",
        ws_callback=ws_callback,
        images=None,
        trace_step_name=trace_step_name,
        session_id_field="supervisor_agent_session_id",
        mode="native",
        source="supervisor",
    )

logger = logging.getLogger(__name__)

MAX_VERDICTS_PER_TURN = 3


def _extract_text(message: Optional[dict]) -> str:
    if not message:
        return ""
    content = message.get("content")
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
    return str(content or "")


def _last_message_text(session: Optional[dict], role: str) -> str:
    if not session:
        return ""
    for m in reversed(session.get("messages") or []):
        if m.get("role") == role and m.get("source") != "supervisor":
            return _extract_text(m)
    return ""


def _last_user_request(session: Optional[dict]) -> str:
    """The most recent real user prompt — skip supervisor-sourced ones."""
    if not session:
        return ""
    for m in reversed(session.get("messages") or []):
        if m.get("role") == "user" and not m.get("source"):
            return _extract_text(m)
    return ""


def _primary_jsonl_info(session: dict) -> tuple[Optional[str], Optional[int]]:
    """Return (jsonl_path, line_count) for the primary agent's claude
    session (`agent_session_id`)."""
    sid = session.get("agent_session_id")
    if not sid:
        return None, None
    cwd = session.get("cwd") or ""
    path = compute_jsonl_path(cwd, sid)
    if not path:
        return None, None
    return str(path), count_jsonl_lines(path)


def _format_todos_block(todos: Optional[list[dict]]) -> str:
    """Format current_todos into an XML block for the supervisor prompt.

    Deduplicates by content — keeps only the latest entry per unique
    content string (covers repeated TodoWrite snapshots within a turn).
    Returns empty string when no todos exist.
    """
    if not todos:
        return ""
    seen: dict[str, dict] = {}
    for t in todos:
        seen[t.get("content", "")] = t
    lines = []
    for t in seen.values():
        status = t.get("status", "pending")
        icon = {"completed": "☑", "in_progress": "▶"}.get(status, "☐")
        text = t.get("content", "")
        active = t.get("activeForm")
        line = f"{icon} [{status}] {text}"
        if active and active != text:
            line += f" — {active}"
        lines.append(line)
    return f"<todos>\n" + "\n".join(lines) + "\n</todos>"


_VERDICT_RESPONSE_SCHEMA = render_prompt("supervisor/verdict_response_schema.md")


# Default body for the per-session custom supervisor prompt. The
# `<verdict-prompt>` wrapper and `_VERDICT_RESPONSE_SCHEMA` are appended
# automatically by `_expand_custom_prompt`, so this string is JUST the
# editable inner body the supervisor extension pre-fills into the modal.
DEFAULT_SUPERVISOR_CUSTOM_PROMPT = render_prompt("supervisor/default.md")


def _build_verdict_prompt(
    primary_last_text: str,
    original_user_request: str,
    primary_session_path: Optional[str] = None,
    primary_session_lines: Optional[int] = None,
    compact: bool = False,
    todos: Optional[list[dict]] = None,
) -> str:
    """Build the supervisor verdict prompt.

    `compact=False` (first call on a fresh supervisor sub-session):
    full adversarial preamble that teaches the role + every common
    cut to look for. Stays in the supervisor's claude session
    context for subsequent turns.

    `compact=True` (any subsequent call): one-line role anchor +
    context blocks + verbatim response schema. The role anchor is
    kept so a context-compaction event inside the supervisor
    sub-session can't strip the framing entirely. The schema is
    NEVER compressed — it's the contract `_VERDICT_RE` parses.
    """
    todos_block = _format_todos_block(todos)
    if compact:
        jsonl_block = ""
        if primary_session_path:
            line_info = f"1-{primary_session_lines}" if primary_session_lines else "all"
            jsonl_block = (
                f"<agent-jsonl>{primary_session_path} (lines {line_info})"
                "</agent-jsonl>\n"
            )
        todos_section = f"{todos_block}\n" if todos_block else ""
        context_block = (
            f"<original-request>{original_user_request}</original-request>\n"
            f"<agent-last-output>{primary_last_text}</agent-last-output>\n"
            f"{jsonl_block}"
            f"{todos_section}"
        )
        return render_prompt(
            "supervisor/compact_verdict.md",
            {
                "context_block": context_block,
                "verdict_response_schema": _VERDICT_RESPONSE_SCHEMA,
            },
        )
    # Non-compact: derive from the same body template the modal shows
    # and the custom-prompt path uses. Single source of truth — editing
    # `DEFAULT_SUPERVISOR_CUSTOM_PROMPT` updates both the system default
    # AND the modal seed.
    return _expand_custom_prompt(
        DEFAULT_SUPERVISOR_CUSTOM_PROMPT,
        original_user_request,
        primary_last_text,
        primary_session_path,
        primary_session_lines,
        todos=todos,
    )


def _choose_verdict_prompt(
    session: dict,
    primary_last_text: str,
    original_user_request: str,
    primary_session_path: Optional[str],
    primary_session_lines: Optional[int],
) -> str:
    """Pure helper — selects full vs compact prompt based on the
    session's `supervisor_bootstrap_received` flag. The flag is True
    only AFTER a prior verdict turn completed successfully; first
    calls (and retries after a failed first call) get the full form.

    If the session has a custom supervisor prompt, it replaces the
    default adversarial preamble. Template variables are substituted:
      {{user_message}}  — the original user request
      {{agent_output}}  — the primary agent's last text output
      {{jsonl_path}}    — path to the primary agent's session jsonl
      {{todos}}         — formatted current todos (or empty)
    """
    todos = session.get("current_todos")
    custom = (session.get("supervisor_custom_prompt") or "").strip()
    if custom:
        return _expand_custom_prompt(
            custom, original_user_request, primary_last_text,
            primary_session_path, primary_session_lines,
            todos=todos,
        )
    compact = bool(session.get("supervisor_bootstrap_received"))
    return _build_verdict_prompt(
        primary_last_text,
        original_user_request,
        primary_session_path=primary_session_path,
        primary_session_lines=primary_session_lines,
        compact=compact,
        todos=todos,
    )


def _expand_custom_prompt(
    template: str,
    user_message: str,
    agent_output: str,
    jsonl_path: Optional[str],
    jsonl_lines: Optional[int],
    todos: Optional[list[dict]] = None,
) -> str:
    """Expand template variables in a custom supervisor prompt and
    append the verdict response schema (required for parsing).

    Template variables:
      {{user_message}}  — the original user request
      {{agent_output}}  — the primary agent's last text output
      {{jsonl_path}}    — path to the primary agent's session jsonl
      {{todos}}         — formatted current todos block (or empty)

    When `jsonl_path` is falsy, the entire `<agent-jsonl>...</agent-jsonl>`
    line is stripped from the template (rather than left as empty
    tags) so the supervisor's "inspect the jsonl" instruction isn't
    pointed at a non-existent artifact. Same for empty todos."""
    if jsonl_path:
        line_info = f"1-{jsonl_lines}" if jsonl_lines else "all"
        jsonl_block = f"{jsonl_path} (lines {line_info})"
    else:
        jsonl_block = ""
        # Drop the whole framed jsonl line — empty tags are noise.
        template = re.sub(
            r"<agent-jsonl>\s*\{\{jsonl_path\}\}\s*</agent-jsonl>\n?",
            "",
            template,
        )
    todos_block = _format_todos_block(todos)
    if not todos_block:
        template = template.replace("{{todos}}\n", "")
    expanded = (
        template.replace("{{user_message}}", user_message)
        .replace("{{agent_output}}", agent_output)
        .replace("{{jsonl_path}}", jsonl_block)
        .replace("{{todos}}", todos_block)
    )
    return render_prompt(
        "supervisor/expanded_verdict.md",
        {
            "expanded": expanded,
            "verdict_response_schema": _VERDICT_RESPONSE_SCHEMA,
        },
    )


_VERDICT_RE = re.compile(
    r"(AWAIT_USER|CONTINUE|FIX|DONE)\b[\s:=-]*\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)


def _parse_verdict(text: str) -> tuple[str, str]:
    """Return (verdict, instructions). Verdict ∈ {DONE, AWAIT_USER,
    CONTINUE, FIX}. Falls back to DONE on unparseable output (fail-open
    so a broken supervisor can't block the primary)."""
    m = _VERDICT_RE.search(text or "")
    if not m:
        return "DONE", ""
    return m.group(1).upper(), (m.group(2) or "").strip()


async def request_verdict(
    coordinator: "Coordinator",
    *,
    primary_session: dict,
    ws_callback: Callable[[dict], Awaitable[None]],
) -> tuple[str, str]:
    """Ask the supervisor for a verdict on the primary's last turn.

    Reads the primary's last (non-supervisor) assistant message and
    jsonl path, builds a verdict prompt, and runs it on the supervisor
    sub-session identified by ``primary_session["supervisor_agent_session_id"]``.
    The supervisor sid is lazy-spawned on first call; on subsequent calls
    the same sid is resumed so context accumulates.

    Returns ``(verdict, instructions)`` where *verdict* ∈ ``{"DONE",
    "AWAIT_USER", "CONTINUE", "FIX"}`` and *instructions* is the
    supervisor's free-text guidance (empty on DONE).

    Fails open — any error returns ``("DONE", "")`` so the primary
    isn't blocked by a broken supervisor.
    """
    app_session_id = primary_session.get("id")
    if not app_session_id:
        logger.warning("verdict: primary session missing id")
        return "DONE", ""

    # Refetch from the manager so a stale `primary_session` arg can't
    # mis-classify the bootstrap-received gate (e.g. the caller is
    # holding a snapshot from before the previous successful verdict
    # flipped the flag).
    fresh_for_gate = session_manager.get(app_session_id) or primary_session
    primary_last = _last_message_text(fresh_for_gate, "assistant")
    original_request = _last_user_request(fresh_for_gate)
    ws_path, ws_lines = _primary_jsonl_info(fresh_for_gate)

    prompt = _choose_verdict_prompt(
        fresh_for_gate,
        primary_last,
        original_request,
        ws_path,
        ws_lines,
    )

    try:
        await _run_supervisor_turn(
            coordinator,
            session=primary_session,
            prompt=prompt,
            app_session_id=app_session_id,
            ws_callback=ws_callback,
            trace_step_name="supervisor_verdict",
        )
    except Exception as e:
        logger.exception("verdict: supervisor turn failed — failing open")
        await coordinator.broadcast_session(
            app_session_id,
            "supervisor_event",
            {
                "session_id": app_session_id,
                "kind": "verdict_failed",
                "error": str(e),
                "message": t("supervisor.verdict_failed_message"),
            },
            source="supervisor.verdict_failed",
        )
        return "DONE", ""

    # Flip the gate flag — the supervisor sub-session has now seen the
    # full adversarial preamble (assuming this was a first call) and
    # accumulated it in its claude session context. Subsequent
    # `_choose_verdict_prompt` calls return the compact form.
    # NEVER reached on the except path above, so a failed first call
    # leaves the flag False → next attempt resends the full preamble.
    session_manager.mark_supervisor_bootstrap_received(app_session_id)

    # If the session was cancelled while the supervisor was running,
    # don't try to parse the (likely garbage) partial response —
    # short-circuit to DONE so the verdict loop terminates.
    if coordinator.is_session_cancelled(app_session_id):
        logger.info("verdict: session cancelled — short-circuiting to DONE")
        return "DONE", ""

    # Read the verdict from the last supervisor-sourced assistant message
    # on the same session record (where run_turn just persisted it).
    fresh = session_manager.get(app_session_id) or primary_session
    response_text = ""
    for m in reversed(fresh.get("messages") or []):
        if m.get("role") == "assistant" and m.get("source") == "supervisor":
            response_text = _extract_text(m)
            break

    verdict, instructions = _parse_verdict(response_text)
    logger.info("supervisor verdict: %s", verdict)
    return verdict, instructions


def _build_review_prompt(
    primary_session_path: Optional[str],
    primary_session_lines: Optional[int],
    original_user_request: str,
    todos: Optional[list[dict]] = None,
) -> str:
    if primary_session_path:
        line_info = f"1-{primary_session_lines}" if primary_session_lines else "all"
        session_block = render_prompt(
            "supervisor/session_block.md",
            {"primary_session_path": primary_session_path, "line_info": line_info},
        )
    else:
        session_block = "Agent session log unavailable — review from context.\n\n"
    todos_block = _format_todos_block(todos)
    todos_section = f"{todos_block}\n\n" if todos_block else ""
    return render_prompt(
        "supervisor/review.md",
        {
            "original_user_request": original_user_request,
            "session_block": session_block,
            "todos_section": todos_section,
        },
    )


async def request_review(
    coordinator: "Coordinator",
    *,
    app_session_id: str,
    ws_callback: Callable[[dict], Awaitable[None]],
) -> None:
    """User-triggered adversarial review of the primary's last work.

    Reads the primary session's last assistant message + its claude
    jsonl path, runs a review turn on the supervisor sub-session, then
    hands the review off to the PRIMARY as a new turn — so the primary
    acts on the review.
    """
    primary = session_manager.get(app_session_id)
    if not primary:
        logger.warning("review: missing session %s", app_session_id)
        return

    ws_path, ws_lines = _primary_jsonl_info(primary)
    original_request = _last_user_request(primary)
    todos = primary.get("current_todos")

    prompt = _build_review_prompt(ws_path, ws_lines, original_request, todos=todos)

    try:
        await _run_supervisor_turn(
            coordinator,
            session=primary,
            prompt=prompt,
            app_session_id=app_session_id,
            ws_callback=ws_callback,
            trace_step_name="supervisor_review",
        )
    except Exception:
        logger.exception("review: supervisor review turn failed")
        return

    # Bail if cancelled during the supervisor turn — don't parse garbage
    # or feed it to the primary.
    if coordinator.is_session_cancelled(app_session_id):
        logger.info("review: session cancelled — skipping primary handoff")
        return

    # Read the review text (last supervisor-sourced assistant msg) and
    # hand it off to the primary as a new turn.
    fresh = session_manager.get(app_session_id) or primary
    review_text = ""
    for m in reversed(fresh.get("messages") or []):
        if m.get("role") == "assistant" and m.get("source") == "supervisor":
            review_text = _extract_text(m)
            break
    if not review_text:
        logger.warning("review: no review text to hand off")
        return

    try:
        await run_primary_turn(
            coordinator,
            app_session_id=app_session_id,
            prompt=review_text,
            ws_callback=ws_callback,
            source="supervisor",
        )
    except Exception:
        logger.exception("review: primary turn after review failed")
