"""Post-turn assessor for tasks.

Subscribes to ``lifecycle.turn_complete``. When a turn finishes for a session
that has a still-pending task run, this runs the task's post-scripts
(best-effort) and then its assessment, records a verdict on the run, and
broadcasts ``tasks_changed`` so the UI updates.

Assessment kinds:
  - none:   verdict "skipped" (post-scripts still ran).
  - script: run command; stdout JSON {pass, reason} wins, else exit 0 = pass.
  - llm_judge: needs a one-shot LLM client that does not exist yet in the
    backend — recorded as verdict "error" with an explicit reason until that
    primitive is added (see TODO).
"""

from __future__ import annotations

import json
import logging

import task_script
from stores import task_store

logger = logging.getLogger(__name__)

_LLM_JUDGE_UNWIRED = (
    "llm_judge assessment is not yet wired: the backend has no one-shot LLM "
    "completion primitive. Use a script assessment until it is added."
)


def _parse_script_verdict(res) -> tuple[str, str]:
    """Map a ScriptResult to (verdict, reason). stdout JSON {pass, reason}
    overrides exit-code semantics so an assessment script can fail with
    exit 0 but report pass=false, or vice-versa."""
    if res is None:
        return "error", "assessment script did not run"
    stripped = res.stdout.strip()
    if stripped:
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and "pass" in payload:
            ok = bool(payload.get("pass"))
            reason = str(payload.get("reason") or "").strip()
            return ("pass" if ok else "fail"), (reason or res.stderr.strip())
    if res.ok:
        return "pass", "exit 0"
    return "fail", (res.stderr.strip() or f"exit {res.exit_code}")


def assess(task: dict, session_id: str) -> tuple[str, str, str]:
    """Run post-scripts + assessment for one task run. Returns
    (verdict, reason, verdict_kind). Pure: no store writes."""
    cwd = task.get("cwd") or None
    scripts = task.get("scripts") or {}
    for s in (scripts.get("post") or []):
        # Best-effort: a failing post-script is logged but does not itself
        # determine the verdict — that is the assessment's job.
        res = task_script.run_script(s, fallback_cwd=cwd, timeout=120)
        if res is not None and not res.ok:
            logger.info(
                "task %s post-script failed (exit %s): %s",
                task.get("id"), res.exit_code, res.stderr.strip(),
            )

    assessment = task.get("assessment") or {"kind": "none", "config": {}}
    kind = assessment.get("kind") or "none"
    cfg = assessment.get("config") or {}
    if kind == "none":
        return "skipped", "", "none"
    if kind == "script":
        res = task_script.run_script(cfg, fallback_cwd=cwd, timeout=120)
        verdict, reason = _parse_script_verdict(res)
        return verdict, reason, "script"
    if kind == "llm_judge":
        return "error", _LLM_JUDGE_UNWIRED, "llm_judge"
    return "skipped", "", kind


async def _assess_completed_turn(coordinator, session_id: str) -> None:
    import asyncio

    found = await asyncio.to_thread(task_store.find_pending_run_for_session, session_id)
    if found is None:
        return
    task_id, _run = found
    task = await asyncio.to_thread(task_store.get, task_id)
    if task is None:
        return
    try:
        verdict, reason, verdict_kind = await asyncio.to_thread(assess, task, session_id)
    except Exception:
        logger.exception("task %s assessment raised", task_id)
        verdict, reason, verdict_kind = "error", "assessment raised", "error"
    await asyncio.to_thread(
        task_store.set_run_verdict, task_id, session_id,
        verdict=verdict, reason=reason, verdict_kind=verdict_kind,
    )
    await coordinator.broadcast_global("tasks_changed", {
        "cwd": task.get("cwd") or "",
        "node_id": task.get("node_id") or "primary",
    })


def bind(coordinator) -> None:
    """Subscribe the assessor to turn-complete. Idempotent — re-subscribes
    under a stable name so rebinding replaces, not duplicates."""
    from event_bus import bus, BusEvent  # type: ignore

    async def _on_turn_complete(event: BusEvent) -> None:
        try:
            await _assess_completed_turn(coordinator, event.sid)
        except Exception:
            logger.exception("task assessor failed for %s", event.sid)

    bus.unsubscribe("task_assessor")
    bus.subscribe(
        "lifecycle.turn_complete",
        _on_turn_complete,
        priority=400,
        name="task_assessor",
    )
    logger.info("task_assessor: bound to lifecycle.turn_complete")
