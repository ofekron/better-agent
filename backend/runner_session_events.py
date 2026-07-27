"""Shared normalization helpers for runners in the session-events family.

Runners whose CLI speaks a gemini-cli-style `stream-json` dialect reuse
these to map tool names/inputs onto Claude's canonical shapes, surface
unknown events instead of dropping them, and fold image attachments into
the prompt before writing Claude-shaped `session_events.jsonl`.
"""

from __future__ import annotations

import base64
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def _new_uuid() -> str:
    return str(uuid.uuid4())


# ============================================================================
# Tool name mapping — native → Claude
# ============================================================================
# Mapping the CLI's tool names to claude's so the existing ToolCall.tsx
# icon/diff/expanding code paths render these tool_uses identically to
# claude's. Anything not in this table passes through verbatim (rendered
# as a generic tool card). When a CLI adds a new built-in, extend the map
# — and prefer claude's canonical name so one branch of frontend
# rendering covers both providers.
_TOOL_NAME_MAP = {
    "run_shell_command": "Bash",
    "read_file": "Read",
    "read_many_files": "Read",
    "write_file": "Write",
    "replace": "Edit",
    "grep_search": "Grep",
    "glob_search": "Glob",
    "list_directory": "LS",
    "web_fetch": "WebFetch",
    "web_search": "WebSearch",
    "invoke_agent": "Task",
    "activate_skill": "Skill",
    "update_topic": "TodoWrite",
}


# Mapping native tool-input keys to claude's canonical input schema.
# Per-tool because each tool has a different key namespace. INVARIANT:
# only translates KEYS — values pass through. Keys not listed for a
# tool are forwarded verbatim. Lets the frontend's claude-shaped
# renderers (BashToolCall reads `command`, EditToolCall reads
# `file_path`/`old_string`/`new_string`, etc.) light up here too.
_TOOL_INPUT_KEY_MAP = {
    "Bash":      {"shell_command": "command", "cmd": "command"},
    "Read":      {"path": "file_path"},
    "Write":     {"path": "file_path", "contents": "content"},
    "Edit":      {"path": "file_path", "old": "old_string", "new": "new_string"},
    "Grep":      {"pattern": "pattern", "dir_path": "path"},
    "Glob":      {"pattern": "pattern"},
    "LS":        {"dir_path": "path", "directory": "path"},
    "WebFetch":  {"url": "url"},
    "WebSearch": {"query": "query"},
}


def _map_tool(raw_name: str, raw_input: dict) -> tuple[str, dict]:
    """Return (claude_tool_name, claude_input_dict) for a native tool_use.
    Unmapped tool names and unmapped input keys pass through verbatim
    so a new CLI tool still renders as a card with raw fields."""
    claude_name = _TOOL_NAME_MAP.get(raw_name, raw_name)
    if not isinstance(raw_input, dict):
        return claude_name, {"value": raw_input}
    key_map = _TOOL_INPUT_KEY_MAP.get(claude_name, {})
    mapped = {key_map.get(k, k): v for k, v in raw_input.items()}
    return claude_name, mapped


def _normalize_unknown(raw: dict, parent_uuid: str) -> dict:
    """Surface a stream-json event whose `type` we don't know HOW to
    interpret. We still emit it — wrapped as an `agent_message` with
    an `unknown_event` data type — so the frontend renders a
    diagnostic card instead of pretending the event never happened.
    INVARIANT: every byte the CLI emits is either normalized to a
    structured shape OR surfaced verbatim through this path. No silent
    drops. Same contract on the claude side is enforced by the
    frontend's DiagnosticEvent fallback."""
    return {
        "type": "unknown_event",
        "raw_type": raw.get("type"),
        "raw": raw,
        "uuid": _new_uuid(),
        "parentUuid": parent_uuid,
        "timestamp": raw.get("timestamp", datetime.now().isoformat()),
    }


def _extract_error_message(err: Any) -> Optional[str]:
    """Unified error extractor for both 'result' and 'error' events."""
    if not err:
        return None
    if isinstance(err, dict):
        return err.get("message") or err.get("error") or str(err)
    return str(err)


_NETWORK_ERROR_PATTERN = re.compile(
    r"(?:"
    r"ECONNREFUSED|ECONNRESET|ETIMEDOUT|EPIPE|"
    r"ENOTFOUND|EAI_NONAME|getaddrinfo|could not resolve|"
    r"socket hang up|network error|"
    r"connect ETIMEDOUT|connect ECONNREFUSED|"
    r"TLS handshake|SSL handshake|"
    r"HTTP 50[23]|HTTP 429|"
    r"rate.?limit|overloaded|temporarily unavailable|"
    r"service unavailable|bad gateway"
    r")",
    re.IGNORECASE,
)


def _is_network_error_message(msg: str) -> bool:
    """Check if an error message indicates a transient network failure."""
    return bool(_NETWORK_ERROR_PATTERN.search(msg))


def _sum_usage(a: Optional[dict], b: Optional[dict]) -> dict:
    out: dict[str, int] = {}
    for d in ((a or {}), (b or {})):
        for k, v in (d or {}).items():
            if isinstance(v, (int, float)):
                out[k] = int(out.get(k, 0)) + int(v)
    return out


# ============================================================================
# Image attachments
# ============================================================================
def _materialize_attachments(run_dir: Path, images: list) -> list[Path]:
    """Decode base64 image attachments to disk under run_dir/attachments.

    Returns the absolute file paths. These CLIs' headless paths resolve
    `@path` references and emit inlineData parts for image mime types —
    that is the only supported way to attach images to a `-p` invocation.
    """
    att_dir = run_dir / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, img in enumerate(images):
        ext = img["media_type"].split("/")[-1].replace("jpeg", "jpg")
        fpath = att_dir / f"attachment_{i}.{ext}"
        fpath.write_bytes(base64.b64decode(img["data"]))
        paths.append(fpath)
    return paths


def _apply_image_attachments(
    run_dir: Path, prompt: Optional[str], images: list
) -> tuple[Optional[str], Optional[Path]]:
    """Fold image attachments into the prompt.

    Materializes images to disk and appends a `@path` reference for each
    so the CLI's headless at-command handling inlines them as image parts.
    Returns (prompt_with_refs, attachment_dir). attachment_dir is None
    when there are no images; callers add it via `--include-directories`
    so the absolute `@path` resolves inside a trusted workspace dir.
    """
    if not images:
        return prompt, None
    paths = _materialize_attachments(run_dir, images)
    at_refs = "\n".join(f"@{p}" for p in paths)
    new_prompt = f"{prompt}\n\n{at_refs}" if prompt else at_refs
    return new_prompt, paths[0].parent
