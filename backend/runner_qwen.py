"""Qwen Code runner — detached per-run executable.

Spawned by `QwenProvider.start_run` as a subprocess with
`start_new_session=True`. Handles one Qwen Code CLI run via
`qwen -o stream-json`. Qwen Code (a Gemini CLI fork) emits CLAUDE-shaped
stream-json messages (type system/assistant/user/result with
`message.content` block lists) — NOT Gemini's `init/message/tool_use`
event vocabulary — so normalization here is a thin passthrough: chain
`parentUuid`, stamp `timestamp`, and route tool names/inputs through
runner_gemini's `_map_tool` (Qwen keeps Gemini's tool names, e.g.
`run_shell_command`).

Run-dir protocol is byte-identical to runner_gemini (input.json →
state.json → session_events.jsonl → complete.json, `cancel` sentinel,
`pid` file), so `GeminiProvider._bootstrap_run`, the
`GeminiJsonlTailer`, and recovery_family="gemini" replay work unchanged.

Reused from runner_gemini (single source of truth, no copies):
`_map_tool`, `_apply_image_attachments`, `_is_network_error_message`,
`_sum_usage`, `_extract_error_message`, `_normalize_unknown`,
`_new_uuid`. Stderr/error classification goes through `runner_errors`.

DUPLICATED-PENDING-SEAM: the stderr-drain / cancel-watcher / file-preamble
blocks below mirror runner_gemini's inline versions. runner_gemini keeps
them inside its `_run` closure, so they cannot be imported without an
edit. Proposed refactor (see provider_qwen module docstring): extract
them into a shared `runner_stream_common.py` and have both runners
import it.
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from capability_contexts import prepend_capability_context
from continuation import normalize_context_overflow_error
from proc_control import process_control as _process_control
from runner_guard import (
    GHOST_RETRY_BACKOFF_S,
    GHOST_RETRY_MAX,
    apply_ghost_completion_guard,
    should_retry_ghost,
)
from runner_errors import resume_session_mismatch, stderr_error
from runner_gemini import (
    _apply_image_attachments,
    _extract_error_message,
    _is_network_error_message,
    _map_tool,
    _new_uuid,
    _normalize_unknown,
    _sum_usage,
)
from runs_dir import atomic_write_json
from stream_limits import SUBPROCESS_LINE_LIMIT_BYTES

logger = logging.getLogger(__name__)


def _resolve_qwen_cli() -> Optional[str]:
    from cli_paths import resolve_cli_binary

    return resolve_cli_binary("qwen")


# Better Agent stores the gemini-style permission mode with an underscore
# ("auto_edit"); qwen's --approval-mode vocabulary is hyphenated
# ("auto-edit"). plan/yolo are identical. "default" is intentionally
# absent: like gemini, qwen's non-interactive mode has no approval
# round-trip — a confirmation-requiring tool under "default" fails the
# turn instead of asking.
_APPROVAL_MODE_MAP = {
    "auto_edit": "auto-edit",
    "auto-edit": "auto-edit",
    "yolo": "yolo",
    "plan": "plan",
}


def resolve_approval_mode(permission: Any) -> str:
    mode = permission.get("mode") if isinstance(permission, dict) else None
    return _APPROVAL_MODE_MAP.get(str(mode or ""), "yolo")


# Provider record mode → qwen `--auth-type`. Explicit because a fresh
# ~/.qwen/settings.json has no auth type selected and the CLI fails
# closed ("No auth type is selected") in non-interactive mode.
_AUTH_TYPE_MAP = {
    "subscription": "qwen-oauth",
    "api_key": "openai",
}


def resolve_auth_type(record_mode: str) -> str:
    return _AUTH_TYPE_MAP.get(str(record_mode or "").strip(), "qwen-oauth")


# ============================================================================
# Event normalization — qwen (Claude-shaped) stream-json → Claude jsonl
# ============================================================================
def _map_content_blocks(blocks: Any) -> list:
    """Route tool_use blocks through the shared gemini→claude tool map;
    text / thinking / tool_result blocks pass through verbatim."""
    if not isinstance(blocks, list):
        return [{"type": "text", "text": str(blocks)}]
    out: list = []
    for block in blocks:
        if not isinstance(block, dict):
            out.append(block)
            continue
        if block.get("type") == "tool_use":
            name, mapped_input = _map_tool(
                block.get("name", "unknown"), block.get("input") or {},
            )
            out.append({
                "type": "tool_use",
                "id": block.get("id", _new_uuid()),
                "name": name,
                "input": mapped_input,
            })
        elif block.get("type") == "tool_result":
            out.append({
                "type": "tool_result",
                "tool_use_id": block.get("tool_use_id", ""),
                "content": block.get("content", ""),
                **({"is_error": True} if block.get("is_error") else {}),
            })
        else:
            out.append(block)
    return out


def normalize_qwen_event(raw: dict, parent_uuid: str, resolved_model: str) -> Optional[dict]:
    """Normalize one qwen stream-json message to the Claude jsonl shape
    the render tree and recovery_family="gemini" replay expect.

    Returns None for messages handled elsewhere (system/init → state.json,
    result → complete.json). Unknown types surface as diagnostic events —
    never silently dropped (same contract as runner_gemini)."""
    etype = raw.get("type")
    if etype in ("system", "result"):
        return None
    if etype in ("assistant", "user"):
        message = raw.get("message") or {}
        normalized_message: dict = {
            "role": message.get("role") or etype,
            "content": _map_content_blocks(message.get("content")),
        }
        if etype == "assistant":
            normalized_message["model"] = message.get("model") or resolved_model
        return {
            "type": etype,
            "message": normalized_message,
            "uuid": raw.get("uuid") or _new_uuid(),
            "parentUuid": parent_uuid,
            "timestamp": raw.get("timestamp", datetime.now().isoformat()),
        }
    return _normalize_unknown(raw, parent_uuid)


def _qwen_terminal_error(raw: dict) -> Optional[str]:
    """Extract the terminal error from a qwen `result` message."""
    if not raw.get("is_error"):
        return None
    err = _extract_error_message(raw.get("error"))
    if err:
        return normalize_context_overflow_error(err) or err
    return f"qwen run failed (subtype={raw.get('subtype') or 'unknown'})"


def usage_from_result(raw: dict) -> dict:
    """Map a qwen result message's `usage` to the token_usage shape the
    backend already speaks (same keys as runner_gemini's mapping)."""
    usage = raw.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": int(
            usage.get("cache_read_input_tokens") or usage.get("cached") or 0
        ),
        "total_tokens": int(usage.get("total_tokens") or (input_tokens + output_tokens)),
        "duration_ms": raw.get("duration_ms"),
    }


def _text_from_blocks(blocks: Any) -> str:
    if not isinstance(blocks, list):
        return str(blocks or "")
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


# ============================================================================
# Main async runner — same run-dir protocol as runner_gemini
# ============================================================================
async def _run(run_dir: Path, inputs: dict) -> int:
    log = logging.getLogger("runner_qwen")

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

    # DUPLICATED-PENDING-SEAM (runner_gemini file preamble): inline
    # non-image attachments into the prompt.
    if files:
        file_sections: list[str] = []
        for f in files:
            try:
                raw = base64.b64decode(f.get("data", ""))
                name = f.get("name", "unknown")
            except Exception:
                log.warning("Skipping malformed file attachment: %s", f.get("name", "?"))
                continue
            try:
                text = raw.decode("utf-8")
                file_sections.append(f"<file name=\"{name}\">\n{text}\n</file>")
            except UnicodeDecodeError:
                file_sections.append(
                    f"<file name=\"{name}\">[binary file, {f.get('size', len(raw))} bytes]</file>"
                )
        file_preamble = "\n\n".join(file_sections)
        prompt = f"{file_preamble}\n\n{prompt}" if prompt else file_preamble

    # Image attachments: qwen keeps gemini's headless @path resolution
    # (handleAtCommand → read_many_files inlineData), so the shared
    # materialize-and-reference helper applies verbatim.
    prompt, attachment_dir = _apply_image_attachments(run_dir, prompt, images)

    model = inputs.get("model")
    session_id = inputs.get("session_id")
    run_env = os.environ.copy()

    qwen_bin = _resolve_qwen_cli()
    if not qwen_bin:
        _fail(run_dir, "qwen CLI not found on PATH")
        return 1

    approval_mode = resolve_approval_mode(inputs.get("permission"))
    auth_type = resolve_auth_type(inputs.get("provider_mode") or "subscription")
    # --chat-recording is explicit so `-r <sid>` resume keeps working.
    # NOTE (vs runner_gemini): no `--skip-trust` — qwen 0.10 does not
    # ship that flag. Prompt goes over stdin (qwen appends stdin to the
    # prompt args; with no prompt arg, stdin IS the prompt).
    cmd: list[str] = [
        qwen_bin,
        "--auth-type", auth_type,
        "--approval-mode", approval_mode,
        "--chat-recording",
        "-o", "stream-json",
        "--include-directories", "/",
    ]
    if attachment_dir:
        cmd += ["--include-directories", str(attachment_dir)]
    if model:
        cmd += ["-m", model]
    if session_id:
        cmd += ["-r", session_id]

    state: dict = {
        "run_id": run_dir.name,
        "mode": inputs.get("mode", "native"),
        "runner_pid": os.getpid(),
        "app_session_id": inputs.get("app_session_id"),
        "started_at": datetime.now().isoformat(),
        "session_id": None,
        "jsonl_path": None,
        "complete": False,
    }
    state_path = run_dir / "state.json"
    events_path = run_dir / "session_events.jsonl"

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
        discovered_sid: Optional[str] = None
        parent_uuid = _new_uuid()
        total_usage: dict = {}
        success = False
        error: Optional[str] = None
        cancelled = False
        result_seen = False
        assistant_seen = False
        session_lost = False
        resolved_model = str(model or "qwen")

        state["session_id"] = None
        state["jsonl_path"] = None
        state["complete"] = False
        try:
            if events_path.exists():
                events_path.unlink()
        except OSError:
            pass
        try:
            _stderr_log = run_dir / "qwen_stderr.log"
            if _stderr_log.exists():
                _stderr_log.unlink()
        except OSError:
            pass

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=run_env,
                **_process_control().detach_spawn_kwargs(),
                limit=SUBPROCESS_LINE_LIMIT_BYTES,
            )

            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            await proc.stdin.wait_closed()

            cancel_seen = asyncio.Event()

            # DUPLICATED-PENDING-SEAM (runner_gemini stderr drain).
            async def _drain_stderr() -> None:
                try:
                    with (run_dir / "qwen_stderr.log").open("ab") as f:
                        while True:
                            chunk = await proc.stderr.read(8192)
                            if not chunk:
                                return
                            f.write(chunk)
                            f.flush()
                except Exception:
                    log.exception("qwen stderr drain failed")

            stderr_task = asyncio.create_task(_drain_stderr())

            # DUPLICATED-PENDING-SEAM (runner_gemini cancel watcher).
            async def _cancel_watcher() -> None:
                nonlocal cancelled
                while not cancel_seen.is_set():
                    if _cancel_path.exists():
                        cancelled = True
                        log.info("cancel sentinel seen, terminating qwen tree")
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

                        if etype == "system" and raw_event.get("subtype") == "init":
                            sid = raw_event.get("session_id")
                            if sid:
                                discovered_sid = sid
                                if session_id:
                                    mismatch = resume_session_mismatch(
                                        "qwen", session_id, discovered_sid,
                                    )
                                    if mismatch:
                                        error = mismatch.message
                                        session_lost = True
                                        break
                                state["session_id"] = sid
                                state["jsonl_path"] = str(events_path)
                                atomic_write_json(state_path, state)
                            init_model = raw_event.get("model")
                            if init_model:
                                resolved_model = init_model
                            continue

                        if etype == "result":
                            result_seen = True
                            success = not raw_event.get("is_error")
                            total_usage = usage_from_result(raw_event)
                            err = _qwen_terminal_error(raw_event)
                            if err:
                                error = err
                            elif success:
                                error = None
                            break

                        if etype == "assistant":
                            msg = raw_event.get("message") or {}
                            if _text_from_blocks(msg.get("content")).strip():
                                assistant_seen = True

                        normalized = normalize_qwen_event(raw_event, parent_uuid, resolved_model)
                        if normalized is not None:
                            events_file.write(json.dumps(normalized) + "\n")
                            events_file.flush()
                            new_uuid = normalized.get("uuid")
                            if new_uuid:
                                parent_uuid = new_uuid

            finally:
                cancel_seen.set()
                if not cancel_task.done():
                    cancel_task.cancel()
                    try:
                        await cancel_task
                    except asyncio.CancelledError:
                        pass

            # Session-loss guard tripped mid-stream: fail closed — kill the
            # CLI instead of letting the wrong session's turn run out.
            if session_lost and proc.returncode is None:
                _process_control().force_kill(proc.pid)

            await proc.wait()

            try:
                await asyncio.wait_for(stderr_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                stderr_task.cancel()

            if proc.returncode != 0 and not error and not cancelled:
                try:
                    stderr_log = run_dir / "qwen_stderr.log"
                    if stderr_log.exists():
                        error = stderr_error("qwen", stderr_log.read_text(encoding="utf-8"))
                    if not error:
                        error = f"qwen CLI exited with code {proc.returncode}"
                except Exception:
                    log.exception("failed to extract error from stderr")
                    error = f"qwen CLI exited with code {proc.returncode}"

            if not result_seen and not error and not cancelled:
                error = "qwen CLI exited without emitting a result event"

        except asyncio.CancelledError:
            error = "cancelled"
        except Exception as e:
            log.exception("qwen runner failed")
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

        if error and not cancelled and _is_network_error_message(error):
            if total_usage:
                _accumulated_usage = _sum_usage(_accumulated_usage, total_usage)
            log.warning("qwen network error, retry %.1fs: %s", _retry_backoff, error)
            await _retry_sleep(_retry_backoff)
            _retry_backoff = min(_retry_backoff * 2, 60.0)
            continue

        if should_retry_ghost(error, cancelled=cancelled, attempts=_ghost_attempts):
            _ghost_attempts += 1
            log.warning(
                "qwen ghost completion (prompt_not_executed); retry %d/%d after %.1fs",
                _ghost_attempts, GHOST_RETRY_MAX, GHOST_RETRY_BACKOFF_S,
            )
            await _retry_sleep(GHOST_RETRY_BACKOFF_S)
            continue

        total_usage = _sum_usage(_accumulated_usage, total_usage)
        break

    if cancelled and not error:
        error = "cancelled"

    final_success = success and not cancelled and not error

    # The error IS the run's final answer — emit it as regular assistant
    # text so content derivation keeps it (parity with runner_gemini).
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
    if discovered_sid and not state.get("session_id"):
        state["session_id"] = discovered_sid
    try:
        atomic_write_json(state_path, state)
    except Exception:
        log.exception("failed to finalize state.json")

    return 0 if final_success else 1


def _fail(run_dir: Path, error: str) -> None:
    logger.error("runner_qwen fatal: %s", error)
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


def main(run_dir: Path) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[runner_qwen %(process)d] %(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("runner_qwen").info("starting for run_dir=%s", run_dir)

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")

    try:
        inputs = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
        from runner_operation_host import hydrate_runner_inputs
        inputs = hydrate_runner_inputs(inputs, run_dir)
    except Exception as e:
        _fail(run_dir, f"failed to read input.json: {e}")
        return 1

    try:
        return asyncio.run(_run(run_dir, inputs))
    except Exception as e:
        logger.exception("runner_qwen top-level failure")
        _fail(run_dir, f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    sys.exit(main(args.run_dir))
