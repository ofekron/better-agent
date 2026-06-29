"""Post-turn assessor for tasks.

Subscribes to ``lifecycle.turn_complete``. When a turn finishes for a session
that has a still-pending task run, this runs the task's post-scripts
(best-effort) and then its assessment, records a verdict on the run, and
broadcasts ``tasks_changed`` so the UI updates.

Assessment kinds:
  - none:     verdict "skipped" (post-scripts still ran).
  - script:   run command; stdout JSON {pass, reason} wins, else exit 0 = pass.
  - llm_judge: grade the run's assistant output against the goal + criteria
    via a one-shot ``provider.run_headless`` call (provider-agnostic,
    no_tools=True). Spends one real LLM call per assessed run.
"""

from __future__ import annotations

import json
import logging

import task_script
from stores import task_store

logger = logging.getLogger(__name__)

_JUDGE_TIMEOUT = 120
_MAX_RUN_TEXT = 8000


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


def _extract_run_text(session_id: str) -> str:
    """Last assistant message text from the completed run's render tree.
    Reuses the shared extractor so there's one definition of 'assistant
    text' across the backend."""
    try:
        from event_bus_subscribers import _last_assistant_text
        from session_manager import manager as session_manager
    except Exception:
        return ""
    sess = session_manager.get(session_id)
    if not isinstance(sess, dict):
        return ""
    return _last_assistant_text(sess)


def _resolve_judge_provider(cfg: dict):
    """Provider instance for the judge: the task's configured provider_id if
    set, else the default provider. run_headless has no per-call model
    override, so the provider's default_model is used."""
    import provider as provider_mod
    pid = (cfg or {}).get("provider_id")
    if pid:
        try:
            return provider_mod.get_provider(pid)
        except Exception:
            logger.info("task_assessor: provider %s unavailable, using default", pid)
    return provider_mod.default_provider()


def _parse_judge_json(text: str) -> tuple[bool, str] | None:
    """Pull a {pass, reason} object out of the model's reply. Tolerates
    surrounding prose / fences. Returns None if no usable verdict."""
    if not text:
        return None
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            payload = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "pass" in payload:
            return bool(payload.get("pass")), str(payload.get("reason") or "").strip()
    return None


async def _llm_judge(task: dict, session_id: str) -> tuple[str, str]:
    """Grade the run output against goal + criteria via run_headless.
    Returns (verdict, reason). Fails closed on any error or unparseable
    reply — never silently passes."""
    assessment = task.get("assessment") or {}
    cfg = assessment.get("config") or {}
    criteria = cfg.get("criteria", "")
    goal = task.get("goal", "")
    run_text = _extract_run_text(session_id)
    if not run_text.strip():
        return "error", "llm_judge: no assistant output captured to judge"

    prompt = (
        "You are a strict task assessor. Decide if the agent's run achieved "
        "the goal per the criteria.\n\n"
        f"GOAL:\n{goal or '(none stated)'}\n\n"
        f"CRITERIA:\n{criteria}\n\n"
        f"AGENT RUN OUTPUT (last assistant message):\n\"\"\"\n{run_text[:_MAX_RUN_TEXT]}\n\"\"\"\n\n"
        "Respond with ONLY compact JSON, no prose: "
        '{"pass": true|false, "reason": "one short sentence"}'
    )
    try:
        provider = _resolve_judge_provider(cfg)
        result = await provider.run_headless(
            prompt=prompt,
            cwd=task.get("cwd") or None,
            timeout=_JUDGE_TIMEOUT,
            no_tools=True,
        )
    except Exception as exc:
        return "error", f"llm_judge call failed: {exc}"
    if not result or result.get("is_error"):
        return "error", "llm_judge: model call returned an error"
    reply = str(result.get("result") or "")
    parsed = _parse_judge_json(reply)
    if parsed is None:
        return "error", f"llm_judge: unparseable verdict: {reply[:200]}"
    ok, reason = parsed
    return ("pass" if ok else "fail"), (reason or ("met criteria" if ok else "failed criteria"))


async def assess(task: dict, session_id: str) -> tuple[str, str, str]:
    """Run post-scripts + assessment for one task run. Returns
    (verdict, reason, verdict_kind). Pure: no store writes."""
    import asyncio

    cwd = task.get("cwd") or None
    scripts = task.get("scripts") or {}

    async def _run_post(s: dict) -> None:
        res = await asyncio.to_thread(
            lambda: task_script.run_script(s, fallback_cwd=cwd, timeout=120))
        if res is not None and not res.ok:
            logger.info(
                "task %s post-script failed (exit %s): %s",
                task.get("id"), res.exit_code, res.stderr.strip(),
            )

    for s in (scripts.get("post") or []):
        await _run_post(s)

    assessment = task.get("assessment") or {"kind": "none", "config": {}}
    kind = assessment.get("kind") or "none"
    cfg = assessment.get("config") or {}
    if kind == "none":
        return "skipped", "", "none"
    if kind == "script":
        res = await asyncio.to_thread(lambda: task_script.run_script(cfg, fallback_cwd=cwd, timeout=120))
        verdict, reason = _parse_script_verdict(res)
        return verdict, reason, "script"
    if kind == "llm_judge":
        verdict, reason = await _llm_judge(task, session_id)
        return verdict, reason, "llm_judge"
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
        verdict, reason, verdict_kind = await assess(task, session_id)
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
