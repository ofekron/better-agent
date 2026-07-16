"""Grok Build runner — detached per-run executable.

Spawned by `GrokProvider.start_run` as a subprocess with
`start_new_session=True`. Handles one Grok CLI run via
`grok -p ... --output-format streaming-json`. Verified against
crates/codegen/xai-grok-pager/src/headless.rs in the grok-build repo:
the stream carries ONLY `text` / `thought` DELTA chunks plus a final
`end` (success) or `error` event — no `tool_use` event exists in this
wire format, so tool calls are invisible to the render tree; only the
running text/thinking commentary streams through.

Run-dir protocol is byte-identical to runner_gemini (input.json ->
state.json -> session_events.jsonl -> complete.json, `cancel`
sentinel, `pid` file), so `GeminiProvider._bootstrap_run`, the
`GeminiJsonlTailer`, and recovery_family="gemini" replay work
unchanged.

Streaming semantics: `text`/`thought` are incremental deltas, not full
messages, so the runner accumulates them into a running assistant
message and re-emits the SAME uuid on every delta (gemini-family
"rewrite in place" semantics — mirrors runner_pi's message_start/
message_update/message_end handling). `apply_event`'s per-uuid dedup
in the render tree collapses the repeated rows; `event_ingester`'s
uid+sha256(data) dedup keeps every distinct delta as its own
`session_events.jsonl` row.

Prompt is written to a file and passed via `--prompt-file` rather than
`-p <prompt>` on argv — keeps prompt content out of `ps` and away from
argv length limits (same rationale as kimi/qwen's stdin-based prompts;
grok's headless mode does not read piped stdin, so `--prompt-file` is
the equivalent off-argv channel).

Sessions: a fresh turn (no incoming session_id) pre-generates a uuid4
and passes `-s <uuid>` (create-only, mirrors kimi's immediate-persist
pattern); a follow-up turn passes `-r <session_id>` (resume, errors if
missing — no mismatch-guard needed, the CLI fails closed on its own);
fork passes `-r <session_id> --fork-session` and the NEW forked id is
discovered from the `end` event's `sessionId`. Stderr/error
classification goes through `runner_errors`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from capability_contexts import prepend_capability_context
from proc_control import process_control as _process_control
from runner_errors import stderr_error
from runner_guard import (
    GHOST_RETRY_BACKOFF_S,
    GHOST_RETRY_MAX,
    apply_ghost_completion_guard,
    should_retry_ghost,
)
from runs_dir import atomic_write_json
from stream_limits import SUBPROCESS_LINE_LIMIT_BYTES

logger = logging.getLogger(__name__)

# Event types that carry no renderable text and are surfaced as
# diagnostic rows (never silently dropped — same contract as
# runner_gemini's `_normalize_unknown`).
_DIAGNOSTIC_EVENT_TYPES = {
    "max_turns_reached",
    "auto_compact_started",
    "auto_compact_completed",
    "auto_compact_failed",
    "auto_compact_cancelled",
    "auto_continue_completed",
    "image_compressed",
}


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _resolve_grok_cli() -> Optional[str]:
    from cli_paths import resolve_cli_binary

    return resolve_cli_binary("grok")


def usage_from_grok_event(raw: dict) -> dict:
    """Map a grok `end`/`error`/`json`-format usage block to the
    token_usage shape the backend already speaks. Token fields are
    uncached-only for input per the CLI's documented field policy;
    `total_tokens` includes cache + output."""
    usage = raw.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    out: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "total_tokens": int(usage.get("total_tokens") or (input_tokens + cache_read + output_tokens)),
    }
    reasoning_tokens = usage.get("reasoning_tokens")
    if isinstance(reasoning_tokens, (int, float)):
        out["reasoning_tokens"] = int(reasoning_tokens)
    cost = raw.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        out["total_cost_usd"] = cost
    return out


def build_grok_argv(
    grok_bin: str,
    *,
    model: str,
    reasoning_effort: str,
    cwd: str,
    prompt_file: Path,
    resume_session_id: Optional[str] = None,
    fork: bool = False,
    create_session_id: Optional[str] = None,
) -> list[str]:
    """argv for one headless turn. The prompt is passed via
    `--prompt-file` (never argv) — keeps it out of `ps`. No credential
    material ever appears here."""
    argv = [
        grok_bin,
        "--prompt-file", str(prompt_file),
        "--output-format", "streaming-json",
        "--cwd", str(cwd),
        "--yolo",
        "--no-auto-update",
    ]
    if model:
        argv += ["-m", model]
    if reasoning_effort:
        argv += ["--reasoning-effort", reasoning_effort]
    if resume_session_id:
        argv += ["-r", resume_session_id]
        if fork:
            argv += ["--fork-session"]
    elif create_session_id:
        argv += ["-s", create_session_id]
    return argv


def _assistant_event(
    *,
    text_buf: str,
    thought_buf: str,
    uuid_str: str,
    parent_uuid: str,
    model: str,
) -> Optional[dict]:
    """Full accumulated assistant message (grok's text/thought are
    deltas, not full chunks) — same uuid on every call so downstream
    dedup replaces in place."""
    blocks: list[dict] = []
    if thought_buf:
        blocks.append({"type": "thinking", "thinking": thought_buf})
    if text_buf:
        blocks.append({"type": "text", "text": text_buf})
    if not blocks:
        return None
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": blocks, "model": model},
        "uuid": uuid_str,
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _diagnostic_event(raw: dict, parent_uuid: str) -> dict:
    return {
        "type": "unknown_event",
        "raw_type": raw.get("type"),
        "raw": raw,
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }


def _write_prompt_file(run_dir: Path, prompt: str) -> Path:
    path = run_dir / "grok_prompt.txt"
    path.write_text(prompt, encoding="utf-8")
    return path


def _materialize_images(run_dir: Path, images: list) -> list[Path]:
    att_dir = run_dir / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, img in enumerate(images):
        try:
            ext = str(img.get("media_type") or "image/png").split("/")[-1].replace("jpeg", "jpg")
            fpath = att_dir / f"attachment_{i}.{ext}"
            fpath.write_bytes(base64.b64decode(img.get("data", "")))
            paths.append(fpath)
        except Exception:
            logger.warning("Skipping malformed image attachment %d", i)
    return paths


def _inline_file_attachments(prompt: str, files: list) -> str:
    sections: list[str] = []
    for f in files:
        try:
            raw = base64.b64decode(f.get("data", ""))
            name = f.get("name", "unknown")
        except Exception:
            logger.warning("Skipping malformed file attachment: %s", f.get("name", "?"))
            continue
        try:
            text = raw.decode("utf-8")
            sections.append(f"<file name=\"{name}\">\n{text}\n</file>")
        except UnicodeDecodeError:
            sections.append(
                f"<file name=\"{name}\">[binary file, {f.get('size', len(raw))} bytes]</file>"
            )
    if not sections:
        return prompt
    preamble = "\n\n".join(sections)
    return f"{preamble}\n\n{prompt}" if prompt else preamble


def _fail(run_dir: Path, error: str) -> None:
    logger.error("runner_grok fatal: %s", error)
    try:
        (run_dir / "complete.json").write_text(json.dumps({
            "success": False,
            "session_id": None,
            "error": error,
            "token_usage": None,
            "finished_at": datetime.now().isoformat(),
        }, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("failed to write error complete.json")


async def _run(run_dir: Path, inputs: dict) -> int:
    log = logging.getLogger("runner_grok")

    prompt = inputs.get("prompt")
    images = inputs.get("images") or []
    files = inputs.get("files") or []
    cwd = inputs.get("cwd")
    if not cwd:
        _fail(run_dir, "missing required field: cwd")
        return 1
    if not prompt and not images and not files:
        _fail(run_dir, "missing required field: prompt")
        return 1
    prompt = prepend_capability_context(prompt or "", inputs)

    if files:
        prompt = _inline_file_attachments(prompt, files)

    attachment_paths = _materialize_images(run_dir, images) if images else []
    if attachment_paths:
        refs = "\n".join(f'<image path="{p}"/>' for p in attachment_paths)
        prompt = f"{prompt}\n\n{refs}" if prompt else refs

    model = str(inputs.get("model") or "").strip()
    reasoning_effort = str(inputs.get("reasoning_effort") or "").strip()
    session_id = str(inputs.get("session_id") or "").strip() or None
    fork = bool(inputs.get("fork"))

    grok_bin = _resolve_grok_cli()
    if not grok_bin:
        _fail(run_dir, "grok CLI not found on PATH")
        return 1

    if fork and not session_id:
        _fail(run_dir, "fork requested without a source session id")
        return 1

    create_session_id = None if session_id else str(uuid.uuid4())
    prompt_file = _write_prompt_file(run_dir, prompt)

    cmd = build_grok_argv(
        grok_bin,
        model=model,
        reasoning_effort=reasoning_effort,
        cwd=cwd,
        prompt_file=prompt_file,
        resume_session_id=session_id,
        fork=fork,
        create_session_id=create_session_id,
    )

    run_env = os.environ.copy()

    state: dict = {
        "run_id": run_dir.name,
        "mode": inputs.get("mode", "native"),
        "runner_pid": os.getpid(),
        "app_session_id": inputs.get("app_session_id"),
        "started_at": datetime.now().isoformat(),
        # Resume and create-fresh both know their session id up front;
        # fork's NEW id is unknown until the `end` event.
        "session_id": create_session_id or (session_id if not fork else None),
        "jsonl_path": str(run_dir / "session_events.jsonl"),
        "complete": False,
    }
    state_path = run_dir / "state.json"
    events_path = run_dir / "session_events.jsonl"
    atomic_write_json(state_path, state)

    _retry_backoff = 2.0
    _ghost_attempts = 0
    _accumulated_usage: dict = {}
    _cancel_path = run_dir / "cancel"

    async def _retry_sleep(seconds: float) -> None:
        import time as _time
        deadline = _time.monotonic() + seconds
        while _time.monotonic() < deadline:
            if _cancel_path.exists():
                raise asyncio.CancelledError()
            await asyncio.sleep(min(0.5, deadline - _time.monotonic()))

    while True:
        discovered_sid: Optional[str] = state.get("session_id")
        parent_uuid = _new_uuid()
        current_uuid: Optional[str] = None
        text_buf = ""
        thought_buf = ""
        total_usage: dict = {}
        success = False
        error: Optional[str] = None
        cancelled = False
        result_seen = False
        assistant_seen = False
        resolved_model = model or "grok"

        try:
            if events_path.exists():
                events_path.unlink()
        except OSError:
            pass
        try:
            _stderr_log = run_dir / "grok_stderr.log"
            if _stderr_log.exists():
                _stderr_log.unlink()
        except OSError:
            pass

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=run_env,
                **_process_control().detach_spawn_kwargs(),
                limit=SUBPROCESS_LINE_LIMIT_BYTES,
            )

            cancel_seen = asyncio.Event()

            async def _drain_stderr() -> None:
                try:
                    with (run_dir / "grok_stderr.log").open("ab") as f:
                        while True:
                            chunk = await proc.stderr.read(8192)
                            if not chunk:
                                return
                            f.write(chunk)
                            f.flush()
                except Exception:
                    log.exception("grok stderr drain failed")

            stderr_task = asyncio.create_task(_drain_stderr())

            async def _cancel_watcher() -> None:
                nonlocal cancelled
                while not cancel_seen.is_set():
                    if _cancel_path.exists():
                        cancelled = True
                        log.info("cancel sentinel seen, terminating grok tree")
                        _pc = _process_control()
                        _pc.signal_stop(proc.pid)
                        for _ in range(30):
                            if proc.returncode is not None:
                                break
                            await asyncio.sleep(0.1)
                        if proc.returncode is None:
                            _pc.force_kill(proc.pid)
                        cancel_seen.set()
                        return
                    try:
                        await asyncio.wait_for(cancel_seen.wait(), timeout=0.15)
                    except asyncio.TimeoutError:
                        pass

            cancel_task = asyncio.create_task(_cancel_watcher())

            try:
                with events_path.open("a", encoding="utf-8") as events_file:
                    async for raw_line in proc.stdout:
                        if cancelled:
                            break
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            raw_event = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        etype = raw_event.get("type")

                        if etype == "text":
                            text_buf += str(raw_event.get("data") or "")
                            assistant_seen = True
                            if current_uuid is None:
                                current_uuid = _new_uuid()
                            ev = _assistant_event(
                                text_buf=text_buf, thought_buf=thought_buf,
                                uuid_str=current_uuid, parent_uuid=parent_uuid,
                                model=resolved_model,
                            )
                            if ev is not None:
                                events_file.write(json.dumps(ev) + "\n")
                                events_file.flush()
                            continue

                        if etype == "thought":
                            thought_buf += str(raw_event.get("data") or "")
                            if current_uuid is None:
                                current_uuid = _new_uuid()
                            ev = _assistant_event(
                                text_buf=text_buf, thought_buf=thought_buf,
                                uuid_str=current_uuid, parent_uuid=parent_uuid,
                                model=resolved_model,
                            )
                            if ev is not None:
                                events_file.write(json.dumps(ev) + "\n")
                                events_file.flush()
                            continue

                        if etype == "end":
                            result_seen = True
                            success = True
                            discovered_sid = raw_event.get("sessionId") or discovered_sid
                            total_usage = usage_from_grok_event(raw_event)
                            if current_uuid:
                                parent_uuid = current_uuid
                            break

                        if etype == "error":
                            result_seen = True
                            success = False
                            error = str(raw_event.get("message") or "grok run failed")
                            total_usage = usage_from_grok_event(raw_event)
                            if current_uuid:
                                parent_uuid = current_uuid
                            break

                        if etype in _DIAGNOSTIC_EVENT_TYPES:
                            diag = _diagnostic_event(raw_event, parent_uuid)
                            events_file.write(json.dumps(diag) + "\n")
                            events_file.flush()
                            parent_uuid = diag["uuid"]
                            continue

                        # Unrecognized type — surface, never silently drop.
                        diag = _diagnostic_event(raw_event, parent_uuid)
                        events_file.write(json.dumps(diag) + "\n")
                        events_file.flush()
                        parent_uuid = diag["uuid"]

            finally:
                cancel_seen.set()
                if not cancel_task.done():
                    cancel_task.cancel()
                    try:
                        await cancel_task
                    except asyncio.CancelledError:
                        pass

            await proc.wait()

            try:
                await asyncio.wait_for(stderr_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                stderr_task.cancel()

            if proc.returncode != 0 and not error and not cancelled:
                try:
                    stderr_log = run_dir / "grok_stderr.log"
                    if stderr_log.exists():
                        error = stderr_error("grok", stderr_log.read_text(encoding="utf-8"))
                    if not error:
                        error = f"grok CLI exited with code {proc.returncode}"
                except Exception:
                    log.exception("failed to extract error from stderr")
                    error = f"grok CLI exited with code {proc.returncode}"

            if not result_seen and not error and not cancelled:
                error = "grok CLI exited without emitting an end or error event"

        except asyncio.CancelledError:
            error = "cancelled"
        except Exception as e:
            log.exception("grok runner failed")
            error = f"{type(e).__name__}: {e}"

        success, error = apply_ghost_completion_guard(
            success=success,
            cancelled=cancelled,
            error=error,
            prompt=prompt,
            assistant_seen=assistant_seen,
            total_usage=total_usage,
            result_seen=result_seen,
        )

        if should_retry_ghost(error, cancelled=cancelled, attempts=_ghost_attempts):
            _ghost_attempts += 1
            log.warning(
                "grok ghost completion (prompt_not_executed); retry %d/%d after %.1fs",
                _ghost_attempts, GHOST_RETRY_MAX, GHOST_RETRY_BACKOFF_S,
            )
            await _retry_sleep(GHOST_RETRY_BACKOFF_S)
            continue

        total_usage = {**_accumulated_usage, **total_usage} if _accumulated_usage else total_usage
        break

    if cancelled and not error:
        error = "cancelled"

    final_success = success and not cancelled and not error

    if error and not final_success:
        try:
            error_event = {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"Error: {error}"}],
                    "model": resolved_model,
                },
                "uuid": _new_uuid(),
                "parentUuid": parent_uuid,
                "timestamp": datetime.now().isoformat(),
                "isApiErrorMessage": True,
            }
            with events_path.open("a", encoding="utf-8") as ef:
                ef.write(json.dumps(error_event) + "\n")
                ef.flush()
        except Exception:
            log.exception("failed to emit error event to session_events.jsonl")

    complete = {
        "success": final_success,
        "session_id": discovered_sid,
        "error": error,
        "token_usage": total_usage or None,
        "finished_at": datetime.now().isoformat(),
    }
    try:
        (run_dir / "complete.json").write_text(json.dumps(complete, indent=2), encoding="utf-8")
    except Exception:
        log.exception("failed to write complete.json")

    state["complete"] = True
    state["finished_at"] = complete["finished_at"]
    if discovered_sid:
        state["session_id"] = discovered_sid
    try:
        atomic_write_json(state_path, state)
    except Exception:
        log.exception("failed to finalize state.json")

    return 0 if final_success else 1


def main(run_dir: Path) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[runner_grok %(process)d] %(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("runner_grok").info("starting for run_dir=%s", run_dir)

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")

    try:
        inputs = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
    except Exception as e:
        _fail(run_dir, f"failed to read input.json: {e}")
        return 1

    try:
        return asyncio.run(_run(run_dir, inputs))
    except Exception as e:
        logger.exception("runner_grok top-level failure")
        _fail(run_dir, f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    sys.exit(main(args.run_dir))
