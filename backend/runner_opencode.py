"""OpenCode CLI runner — detached per-run executable.

Spawned by `OpencodeProvider.start_run` as a subprocess with
`start_new_session=True`. Handles one OpenCode run via
`opencode run --format json`, which emits raw JSON events (one per line)
on stdout: `{"type": ..., "sessionID": ..., "part": {...}}`. Each event
is normalized to Claude jsonl shape and appended to
`session_events.jsonl` so the backend can tail it (GeminiProvider path).

Life of a run:
  1. Backend creates run dir, writes input.json.
  2. Backend spawns `python runner_opencode.py --run-dir <path>` detached.
  3. This script reads input.json, spawns `opencode run --format json`
     with the prompt piped over STDIN (never argv — no `ps` leakage, no
     argv size limits).
  4. On the first event: captures sessionID, writes state.json.
  5. On process exit: writes complete.json.

Resume: `-s <sessionID>` continues an existing OpenCode session.
Fork: `-s <sessionID> --fork` forks it into a new session (the new
sessionID is discovered from the forked run's own events).

Permission (fail closed): input `permission` is `{"mode": <value>}`.
  auto     → `--auto` (auto-approve anything not explicitly denied)
  default  → no flag; OpenCode's own permission config governs
  readonly → OPENCODE_PERMISSION denies bash/edit/write/patch
             (verified: denied tools are removed from the model's tool set)
Any other mode fails the run before spawning the CLI.

Cancel sentinel: backend writes `run_dir/cancel`, runner terminates the
opencode subprocess.
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
from cli_paths import resolve_cli_binary
from proc_control import process_control as _process_control
from runner_errors import classify, resume_session_mismatch
from runs_dir import atomic_write_json

logger = logging.getLogger(__name__)

# Deterministic UUID namespace: OpenCode streams in-place updates of the
# same part (same part id, growing text / tool state transitions). Keying
# the render uuid on the part id makes every update of one part collide in
# the render tree's uuid dedup (replace-in-place) instead of duplicating.
_OPENCODE_UUID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_DNS, "better-agent.runner_opencode.events"
)

_PERMISSION_MODES = ("default", "auto", "readonly")

# OPENCODE_PERMISSION JSON for readonly mode. Verified against opencode
# 1.17.18: a "deny" entry removes the tool from the model's available set
# entirely (no prompt, no partial access).
_READONLY_PERMISSION = {
    "bash": "deny",
    "edit": "deny",
    "write": "deny",
    "patch": "deny",
}


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _part_uuid(session_id: Optional[str], part_id: Optional[str], suffix: str = "") -> str:
    """Deterministic render uuid for an OpenCode part (+ optional suffix
    for derived events like the tool_result of a tool part). Random when
    there is no stable handle."""
    if session_id and part_id:
        return str(uuid.uuid5(_OPENCODE_UUID_NAMESPACE, f"{session_id}|{part_id}|{suffix}"))
    return _new_uuid()


# ============================================================================
# Tool name / input-key mapping — OpenCode → Claude
# ============================================================================
# Map opencode's built-in tool names to claude's canonical names so the
# frontend's existing renderers (icons, diffs, expanders) light up.
# Unmapped names pass through verbatim (generic tool card).
_TOOL_NAME_MAP = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "patch": "Edit",
    "grep": "Grep",
    "glob": "Glob",
    "list": "LS",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "task": "Task",
    "skill": "Skill",
    "todowrite": "TodoWrite",
    "todoread": "TodoRead",
}

# Per-tool input key translation (keys only — values pass through; keys
# not listed are forwarded verbatim).
_TOOL_INPUT_KEY_MAP = {
    "Read": {"filePath": "file_path"},
    "Write": {"filePath": "file_path"},
    "Edit": {"filePath": "file_path", "oldString": "old_string", "newString": "new_string"},
    "LS": {"path": "path"},
}


def _map_tool(raw_name: str, raw_input: Any) -> tuple[str, dict]:
    claude_name = _TOOL_NAME_MAP.get(raw_name, raw_name)
    if not isinstance(raw_input, dict):
        return claude_name, {"value": raw_input}
    key_map = _TOOL_INPUT_KEY_MAP.get(claude_name, {})
    return claude_name, {key_map.get(k, k): v for k, v in raw_input.items()}


# ============================================================================
# Event normalization — OpenCode `--format json` → Claude jsonl shape
# ============================================================================
def _agent_message(
    *,
    role: str,
    content: list[dict[str, Any]],
    parent_uuid: str,
    model: str,
    uuid_str: str,
    timestamp: Optional[Any] = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": role, "content": content}
    if role == "assistant":
        message["model"] = model
    if isinstance(timestamp, (int, float)):
        ts = datetime.fromtimestamp(timestamp / 1000.0).isoformat()
    else:
        ts = str(timestamp) if timestamp else datetime.now().isoformat()
    return {
        "type": role,
        "message": message,
        "uuid": uuid_str,
        "parentUuid": parent_uuid,
        "timestamp": ts,
    }


def _stringify_output(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(output)


def normalize_opencode_event(
    event: dict[str, Any],
    *,
    session_id: Optional[str],
    parent_uuid: str,
    model: str,
) -> list[dict[str, Any]]:
    """Map one OpenCode JSON event to 0..2 Claude-shaped jsonl events.

    text      → one assistant text event
    reasoning → one assistant thinking event
    tool_use  → assistant tool_use; plus a user tool_result once the
                part's state reports completed/error
    step_start / step_finish → bookkeeping, no render events
    error     → one assistant error text event
    unknown   → surfaced verbatim as an `unknown_event` diagnostic
                (INVARIANT: no silent drops)
    """
    etype = event.get("type")
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    part_id = part.get("id")
    ts = event.get("timestamp")

    if etype in ("step_start", "step_finish"):
        return []

    if etype == "text":
        text = str(part.get("text") or "")
        if not text:
            return []
        return [_agent_message(
            role="assistant",
            content=[{"type": "text", "text": text}],
            parent_uuid=parent_uuid,
            model=model,
            uuid_str=_part_uuid(session_id, part_id),
            timestamp=ts,
        )]

    if etype == "reasoning":
        text = str(part.get("text") or "")
        if not text:
            return []
        return [_agent_message(
            role="assistant",
            content=[{"type": "thinking", "thinking": text}],
            parent_uuid=parent_uuid,
            model=model,
            uuid_str=_part_uuid(session_id, part_id),
            timestamp=ts,
        )]

    if etype == "tool_use":
        call_id = str(part.get("callID") or part_id or _new_uuid())
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        name, mapped_input = _map_tool(
            str(part.get("tool") or "tool"), state.get("input") or {},
        )
        out: list[dict[str, Any]] = [_agent_message(
            role="assistant",
            content=[{
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": mapped_input,
            }],
            parent_uuid=parent_uuid,
            model=model,
            uuid_str=_part_uuid(session_id, part_id, "use"),
            timestamp=ts,
        )]
        status = str(state.get("status") or "")
        if status in ("completed", "error"):
            out.append(_agent_message(
                role="user",
                content=[{
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": _stringify_output(
                        state.get("output") if status == "completed"
                        else state.get("error") or state.get("output")
                    ),
                    "is_error": status == "error",
                }],
                parent_uuid=parent_uuid,
                model=model,
                uuid_str=_part_uuid(session_id, part_id, "result"),
                timestamp=ts,
            ))
        return out

    if etype == "error":
        message = event.get("error") or part.get("error") or event.get("message")
        return [_agent_message(
            role="assistant",
            content=[{
                "type": "text",
                "text": f"Error: {_stringify_output(message) or 'unknown error'}",
            }],
            parent_uuid=parent_uuid,
            model=model,
            uuid_str=_part_uuid(session_id, part_id, "error"),
            timestamp=ts,
        )]

    # Unknown event type — surface as a diagnostic, never drop silently.
    return [{
        "type": "unknown_event",
        "raw_type": etype,
        "raw": event,
        "uuid": _part_uuid(session_id, part_id, f"unknown|{etype}"),
        "parentUuid": parent_uuid,
        "timestamp": datetime.now().isoformat(),
    }]


_unknown_event_types_seen: set[str] = set()


def _log_unknown_event(log: logging.Logger, etype: str) -> None:
    if etype in _unknown_event_types_seen:
        return
    _unknown_event_types_seen.add(etype)
    log.warning(
        "runner_opencode: unknown JSON event type %r — surfaced as "
        "unknown_event. Add a normalizer if it carries data.", etype,
    )


# ============================================================================
# Permission mapping (fail closed)
# ============================================================================
def resolve_permission_spawn(permission: Any) -> tuple[list[str], dict[str, str]]:
    """(extra argv, extra env) for the run's permission. Raises ValueError
    on any unrecognized mode — fail closed, never fall through to a more
    permissive spawn."""
    mode = "default"
    if isinstance(permission, dict) and permission.get("mode"):
        mode = str(permission["mode"])
    if mode not in _PERMISSION_MODES:
        raise ValueError(
            f"unsupported opencode permission mode {mode!r}; "
            f"allowed: {', '.join(_PERMISSION_MODES)}"
        )
    if mode == "auto":
        return ["--auto"], {}
    if mode == "readonly":
        return [], {"OPENCODE_PERMISSION": json.dumps(_READONLY_PERMISSION)}
    return [], {}


# ============================================================================
# Argv construction
# ============================================================================
def build_opencode_argv(
    *,
    opencode_bin: str,
    model: Optional[str],
    reasoning_effort: Optional[str],
    session_id: Optional[str],
    fork: bool,
    permission_argv: list[str],
    attachment_paths: Optional[list[Path]] = None,
    cwd: Optional[str] = None,
) -> list[str]:
    """argv for `opencode run`. The prompt is NOT part of argv — it is
    piped over stdin (no `ps` leakage, no argv size limits). `--dir`
    pins opencode's project directory explicitly: the bun-built CLI
    resolves its directory from `$PWD`, which the detached runner
    inherits from the backend, not from the subprocess spawn cwd."""
    if fork and not session_id:
        raise ValueError("opencode --fork requires a session id to fork from")
    argv: list[str] = [opencode_bin, "run", "--format", "json"]
    if cwd:
        argv += ["--dir", cwd]
    if model:
        argv += ["-m", model]
    if reasoning_effort:
        argv += ["--variant", reasoning_effort]
    if session_id:
        argv += ["-s", session_id]
        if fork:
            argv += ["--fork"]
    for path in attachment_paths or []:
        argv += ["-f", str(path)]
    argv += permission_argv
    return argv


# ============================================================================
# Attachments
# ============================================================================
def _materialize_images(run_dir: Path, images: list) -> list[Path]:
    """Decode base64 image attachments to disk under run_dir/attachments;
    passed to the CLI via `-f/--file`."""
    att_dir = run_dir / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, img in enumerate(images):
        try:
            ext = str(img.get("media_type") or "image/png").split("/")[-1].replace("jpeg", "jpg")
            fpath = att_dir / f"attachment_{i}.{ext}"
            fpath.write_bytes(base64.b64decode(img["data"]))
            paths.append(fpath)
        except Exception:
            logger.warning("skipping malformed image attachment %d", i)
    return paths


def _inline_file_attachments(prompt: str, files: list) -> str:
    sections: list[str] = []
    for f in files:
        try:
            raw = base64.b64decode(f.get("data", ""))
            name = f.get("name", "unknown")
        except Exception:
            logger.warning("skipping malformed file attachment: %s", f.get("name", "?"))
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


def _prepend_capability_context(prompt: str, inputs: dict) -> str:
    return prepend_capability_context(prompt, inputs)


def _sum_tokens(acc: dict[str, int], tokens: Any) -> dict[str, int]:
    """Accumulate a step_finish `tokens` payload into claude-shaped usage."""
    if not isinstance(tokens, dict):
        return acc
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    for key, value in (
        ("input_tokens", tokens.get("input")),
        ("output_tokens", tokens.get("output")),
        ("total_tokens", tokens.get("total")),
        ("reasoning_tokens", tokens.get("reasoning")),
        ("cache_read_input_tokens", cache.get("read")),
        ("cache_creation_input_tokens", cache.get("write")),
    ):
        if isinstance(value, (int, float)):
            acc[key] = int(acc.get(key, 0)) + int(value)
    return acc


# ============================================================================
# Main async runner
# ============================================================================
async def _run(run_dir: Path, inputs: dict) -> int:
    log = logging.getLogger("runner_opencode")

    cwd = str(inputs.get("cwd") or "")
    if not cwd:
        _fail(run_dir, "missing required field: cwd")
        return 1
    prompt = str(inputs.get("prompt") or "")
    images = inputs.get("images") or []
    files = inputs.get("files") or []
    if not prompt and not images and not files:
        _fail(run_dir, "missing required field: prompt")
        return 1

    prompt = _prepend_capability_context(prompt, inputs)
    prompt = _inline_file_attachments(prompt, files)
    attachment_paths = _materialize_images(run_dir, images) if images else []

    opencode_bin = resolve_cli_binary("opencode")
    if not opencode_bin:
        _fail(run_dir, "opencode CLI not found on PATH")
        return 1

    requested_sid = str(inputs.get("session_id") or "").strip() or None
    fork = bool(inputs.get("fork"))
    try:
        permission_argv, permission_env = resolve_permission_spawn(
            inputs.get("permission")
        )
        argv = build_opencode_argv(
            opencode_bin=opencode_bin,
            model=str(inputs.get("model") or "").strip() or None,
            reasoning_effort=str(inputs.get("reasoning_effort") or "").strip() or None,
            session_id=requested_sid,
            fork=fork,
            permission_argv=permission_argv,
            attachment_paths=attachment_paths,
            cwd=cwd,
        )
    except ValueError as exc:
        _fail(run_dir, str(exc))
        return 1

    model = str(inputs.get("model") or "opencode")
    run_env = os.environ.copy()
    run_env.update(permission_env)
    # Keep $PWD coherent with the spawn cwd (bun-built CLIs prefer it).
    run_env["PWD"] = cwd

    state: dict[str, Any] = {
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
    cancel_path = run_dir / "cancel"

    discovered_sid: Optional[str] = None
    parent_uuid = _new_uuid()
    total_usage: dict[str, int] = {}
    error: Optional[str] = None
    cancelled = False
    assistant_seen = False
    session_lost = False

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=run_env,
        **_process_control().detach_spawn_kwargs(),
        limit=16 * 1024 * 1024,
    )

    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()
    await proc.stdin.wait_closed()

    cancel_seen = asyncio.Event()

    async def _drain_stderr() -> None:
        try:
            with (run_dir / "opencode_stderr.log").open("ab") as f:
                while True:
                    chunk = await proc.stderr.read(8192)
                    if not chunk:
                        return
                    f.write(chunk)
                    f.flush()
        except Exception:
            log.exception("opencode stderr drain failed")

    stderr_task = asyncio.create_task(_drain_stderr())

    async def _cancel_watcher() -> None:
        nonlocal cancelled
        while not cancel_seen.is_set():
            if cancel_path.exists():
                cancelled = True
                log.info("cancel sentinel seen, terminating opencode tree")
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
                if not isinstance(raw_event, dict):
                    continue

                sid = raw_event.get("sessionID")
                if sid and not discovered_sid:
                    discovered_sid = str(sid)
                    if requested_sid and not fork:
                        mismatch = resume_session_mismatch(
                            "opencode", requested_sid, discovered_sid,
                        )
                        if mismatch:
                            error = mismatch.message
                            session_lost = True
                            break
                    state["session_id"] = discovered_sid
                    state["jsonl_path"] = str(events_path)
                    atomic_write_json(state_path, state)

                etype = raw_event.get("type")
                if etype == "step_finish":
                    part = raw_event.get("part") if isinstance(raw_event.get("part"), dict) else {}
                    total_usage = _sum_tokens(total_usage, part.get("tokens"))
                elif etype == "error":
                    msg = raw_event.get("error") or raw_event.get("message")
                    if msg and not error:
                        error = _stringify_output(msg)

                normalized = normalize_opencode_event(
                    raw_event,
                    session_id=discovered_sid,
                    parent_uuid=parent_uuid,
                    model=model,
                )
                for norm in normalized:
                    if norm.get("type") == "unknown_event":
                        _log_unknown_event(log, str(etype))
                    elif norm.get("type") == "assistant":
                        assistant_seen = True
                    events_file.write(json.dumps(norm) + "\n")
                    events_file.flush()
                if normalized:
                    parent_uuid = normalized[-1].get("uuid") or parent_uuid
    except asyncio.CancelledError:
        error = error or "cancelled"
    except Exception as exc:
        log.exception("opencode runner failed")
        error = error or f"{type(exc).__name__}: {exc}"
    finally:
        cancel_seen.set()
        if not cancel_task.done():
            cancel_task.cancel()
            try:
                await cancel_task
            except asyncio.CancelledError:
                pass

    # Session-loss guard tripped mid-stream: fail closed — kill the CLI
    # instead of letting the wrong session's turn run out.
    if session_lost and proc.returncode is None:
        _process_control().force_kill(proc.pid)

    await proc.wait()
    try:
        await asyncio.wait_for(stderr_task, timeout=2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        stderr_task.cancel()

    if cancelled and not error:
        error = "cancelled"
    if proc.returncode != 0 and not error and not cancelled:
        stderr_text = ""
        try:
            stderr_log = run_dir / "opencode_stderr.log"
            if stderr_log.exists():
                stderr_text = stderr_log.read_text(encoding="utf-8")
        except OSError:
            pass
        hit = classify("opencode", stderr_text)
        if hit:
            error = hit.message
        else:
            lines = [ln.strip() for ln in stderr_text.splitlines() if ln.strip()]
            stderr_tail = f": {lines[-1]}" if lines else ""
            error = f"opencode CLI exited with code {proc.returncode}{stderr_tail}"
    if not error and not cancelled and not assistant_seen:
        error = "opencode CLI exited without emitting any assistant output"

    final_success = proc.returncode == 0 and not cancelled and not error

    # The error IS the run's final answer — persist it as a regular
    # assistant text event so content derivation surfaces it.
    if error and not final_success:
        try:
            error_event = _agent_message(
                role="assistant",
                content=[{"type": "text", "text": f"Error: {error}"}],
                parent_uuid=parent_uuid,
                model=model,
                uuid_str=_new_uuid(),
            )
            error_event["isApiErrorMessage"] = True
            with events_path.open("a", encoding="utf-8") as ef:
                ef.write(json.dumps(error_event) + "\n")
        except Exception:
            log.exception("failed to emit error event to session_events.jsonl")

    atomic_write_json(run_dir / "complete.json", {
        "success": final_success,
        "session_id": discovered_sid,
        "error": None if final_success else error,
        "token_usage": total_usage or None,
        "finished_at": datetime.now().isoformat(),
    })
    state["complete"] = True
    state["finished_at"] = datetime.now().isoformat()
    if discovered_sid and not state.get("session_id"):
        state["session_id"] = discovered_sid
    try:
        atomic_write_json(state_path, state)
    except Exception:
        log.exception("failed to finalize state.json")

    return 0 if final_success else 1


def _fail(run_dir: Path, error: str) -> None:
    logger.error("runner_opencode fatal: %s", error)
    try:
        atomic_write_json(run_dir / "complete.json", {
            "success": False,
            "session_id": None,
            "error": error,
            "token_usage": None,
            "finished_at": datetime.now().isoformat(),
        })
    except Exception:
        logger.exception("failed to write error complete.json")


def main(run_dir: Path) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[runner_opencode %(process)d] %(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("runner_opencode").info("starting for run_dir=%s", run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
    try:
        inputs = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
    except Exception as exc:
        _fail(run_dir, f"failed to read input.json: {exc}")
        return 1
    try:
        return asyncio.run(_run(run_dir, inputs))
    except Exception as exc:
        logger.exception("runner_opencode top-level failure")
        _fail(run_dir, f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    sys.exit(main(args.run_dir))
