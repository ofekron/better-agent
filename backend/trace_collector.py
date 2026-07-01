"""Trace collection for orchestration visibility.

Captures every CLI call's input prompt, raw output, parsed output,
token usage, timing, and step metadata into a structured trace.
Traces are persisted to ~/.better-claude/traces/ as JSON files
with a JSONL index for fast grep.
"""

import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from paths import ba_home
from typing import Awaitable, Callable, Iterable, Iterator, Optional
import trace_grep_index

logger = logging.getLogger(__name__)

TOKEN_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _normalize_token_usage(usage: object) -> Optional[dict]:
    if not isinstance(usage, dict):
        return None
    if not any(k in usage for k in TOKEN_USAGE_KEYS):
        return None
    return {k: int(usage.get(k) or 0) for k in TOKEN_USAGE_KEYS}


def _merge_usage(usages: Iterable[dict]) -> Optional[dict]:
    total = {k: 0 for k in TOKEN_USAGE_KEYS}
    saw_any = False
    for usage in usages:
        normalized = _normalize_token_usage(usage)
        if normalized is None:
            continue
        saw_any = True
        for key in TOKEN_USAGE_KEYS:
            total[key] += normalized[key]
    return total if saw_any else None


def aggregate_claude_usage_snapshots(
    snapshots: Iterable[tuple[Optional[str], object]],
) -> Optional[dict]:
    keyed: dict[str, dict] = {}
    unkeyed: list[dict] = []
    for message_id, usage in snapshots:
        normalized = _normalize_token_usage(usage)
        if normalized is None:
            continue
        if message_id:
            keyed[str(message_id)] = normalized
        else:
            unkeyed.append(normalized)
    return _merge_usage([*keyed.values(), *unkeyed])


def aggregate_claude_turn_usage(
    assistant_snapshots: Iterable[tuple[Optional[str], object]],
    result_usage: object = None,
) -> Optional[dict]:
    normalized_result = _normalize_token_usage(result_usage)
    if normalized_result is not None:
        return normalized_result
    return aggregate_claude_usage_snapshots(assistant_snapshots)


def _traces_dir() -> Path:
    """Resolve the traces root lazily. Per CLAUDE.md's BETTER_CLAUDE_HOME
    isolation rule we must NOT cache this at module-load time — tests
    override the env var inside the test process and a cached Path
    would point at the developer's real `~/.better-claude/traces/`.
    A12: single root helper per store — every read/write below
    funnels through this one."""
    return ba_home() / "traces"


class TraceStep:
    """One step in an orchestration trace."""

    def __init__(
        self,
        step_type: str,
        thread_id: Optional[str] = None,
        thread_name: Optional[str] = None,
        ephemeral: bool = False,
    ):
        self.step_type = step_type
        self.thread_id = thread_id
        self.thread_name = thread_name
        self.ephemeral = ephemeral
        self.input_prompt: str = ""
        self.raw_output: str = ""
        self.parsed_output: Optional[dict] = None
        self.parse_error: Optional[str] = None
        self.token_usage: Optional[dict] = None
        self.error: Optional[str] = None
        self.subagent_types: list[str] = []
        self._started_at: Optional[float] = None
        self._ended_at: Optional[float] = None

    def start(self):
        self._started_at = time.monotonic()

    def end(self):
        self._ended_at = time.monotonic()

    @property
    def duration_ms(self) -> Optional[float]:
        if self._started_at is not None and self._ended_at is not None:
            return round((self._ended_at - self._started_at) * 1000, 1)
        return None

    def to_dict(self) -> dict:
        return {
            "step_type": self.step_type,
            "thread_id": self.thread_id,
            "thread_name": self.thread_name,
            "ephemeral": self.ephemeral,
            "input_prompt": self.input_prompt,
            "raw_output": self.raw_output,
            "parsed_output": self.parsed_output,
            "parse_error": self.parse_error,
            "token_usage": self.token_usage,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "subagent_types": self.subagent_types,
        }


class TraceCollector:
    """Collects trace data for a single orchestration run.

    Usage:
        trace = TraceCollector(session_id, user_prompt)
        trace.set_ws_callback(ws_callback)

        step = trace.start_step("routing")
        step.input_prompt = routing_prompt
        result = await self._run_cli(...)
        step.raw_output = result["output"]
        step.parsed_output = parsed_decision
        step.token_usage = extract_token_usage(result["events"])
        await trace.end_step(step)

        trace.finalize()
        trace.save()
    """

    def __init__(self, session_id: str, user_prompt: str):
        self.trace_id = f"tr_{uuid.uuid4().hex[:12]}"
        self.session_id = session_id
        self.user_prompt = user_prompt
        self.timestamp = datetime.now().isoformat()
        self.steps: list[TraceStep] = []
        self._started_at = time.monotonic()
        self._ended_at: Optional[float] = None
        self._ws_callback: Optional[Callable[[dict], Awaitable[None]]] = None

    def set_ws_callback(self, callback: Callable[[dict], Awaitable[None]]):
        """Set the WebSocket callback for real-time trace_step events."""
        self._ws_callback = callback

    def start_step(
        self,
        step_type: str,
        thread_id: Optional[str] = None,
        thread_name: Optional[str] = None,
        ephemeral: bool = False,
    ) -> TraceStep:
        step = TraceStep(step_type, thread_id, thread_name, ephemeral)
        step.start()
        return step

    async def end_step(self, step: TraceStep):
        """End a step, add it to the trace, and stream it via WebSocket."""
        step.end()
        self.steps.append(step)

        if self._ws_callback:
            try:
                await self._ws_callback({
                    "type": "trace_step",
                    "data": {
                        "trace_id": self.trace_id,
                        "step_index": len(self.steps) - 1,
                        **step.to_dict(),
                    },
                })
            except Exception:
                logger.warning("Failed to stream trace_step event", exc_info=True)

    def finalize(self):
        self._ended_at = time.monotonic()

    @property
    def total_duration_ms(self) -> Optional[float]:
        if self._ended_at is not None:
            return round((self._ended_at - self._started_at) * 1000, 1)
        return None

    @property
    def total_token_usage(self) -> dict:
        total: dict[str, int] = {}
        for step in self.steps:
            if step.token_usage:
                for key, val in step.token_usage.items():
                    total[key] = total.get(key, 0) + (val or 0)
        return total

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "user_prompt": self.user_prompt,
            "timestamp": self.timestamp,
            "duration_ms": self.total_duration_ms,
            "total_token_usage": self.total_token_usage,
            "step_count": len(self.steps),
            "steps": [s.to_dict() for s in self.steps],
        }

    def to_index_entry(self) -> dict:
        """Compact single-line entry for the index file."""
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "user_prompt_preview": self.user_prompt[:100],
            "duration_ms": self.total_duration_ms,
            "step_count": len(self.steps),
            "total_token_usage": self.total_token_usage,
        }

    def save(self):
        """Persist trace to disk: full trace file + index entry."""
        try:
            session_trace_dir = _traces_dir() / self.session_id
            session_trace_dir.mkdir(parents=True, exist_ok=True)

            # Full trace
            trace_path = session_trace_dir / f"{self.trace_id}.json"
            trace_path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

            # Index (append)
            index_path = _traces_dir() / "index.jsonl"
            with open(index_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(self.to_index_entry()) + "\n")

            try:
                trace_grep_index.index_trace(self.to_dict(), trace_path)
            except Exception:
                logger.debug("Failed to update trace grep index %s", self.trace_id, exc_info=True)

            logger.info(
                "Trace saved: %s (session=%s, steps=%d, duration=%sms)",
                self.trace_id, self.session_id, len(self.steps), self.total_duration_ms,
            )
        except Exception:
            logger.exception("Failed to save trace %s", self.trace_id)


# ============================================================================
# Helpers
# ============================================================================

def extract_token_usage(events: list[dict]) -> Optional[dict]:
    """Extract aggregate token_usage from a collected event stream.

    Preference order:

    1. The synthesized ``complete`` envelope event's ``token_usage``
       field (written by the runner into ``complete.json`` and forwarded
       by ``ClaudeProvider._emit_complete_from_file``). This is the
       authoritative roll-up for the whole turn.
    2. Legacy path: sum per-message ``usage`` fields on ``agent_message``
       events whose inner ``type == "assistant"``. Used when ``complete``
       is missing (partial turn, mid-stream render, or old pre-refactor
       persisted events that had their own shape).
    """
    # 1) Authoritative: complete envelope.
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get("type") == "complete":
            tu = (e.get("data") or {}).get("token_usage")
            if tu:
                return tu

    # 2) Fallback: aggregate assistant.message.usage across agent_message events.
    snapshots: list[tuple[Optional[str], object]] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get("type") != "agent_message":
            continue
        data = e.get("data") or {}
        if data.get("type") != "assistant":
            continue
        message = data.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        snapshots.append((message.get("id"), usage))
    return aggregate_claude_usage_snapshots(snapshots)


def extract_provider_result_token_usage(result: dict) -> Optional[dict]:
    usage = _normalize_token_usage(result.get("token_usage"))
    if usage is not None:
        return usage
    return extract_token_usage(result.get("events", []))


# ============================================================================
# Query functions
# ============================================================================

def list_traces(session_id: Optional[str] = None, limit: int = 100) -> list[dict]:
    """Read index.jsonl and return trace entries, newest first."""
    if limit <= 0:
        return []
    index_path = _traces_dir() / "index.jsonl"
    if not index_path.exists():
        return []

    entries = []
    lines = (
        _iter_file_lines_reverse(index_path)
        if session_id is None
        else reversed(index_path.read_text(encoding="utf-8").splitlines())
    )
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if session_id and entry.get("session_id") != session_id:
                continue
            entries.append(entry)
            if len(entries) >= limit:
                break
        except json.JSONDecodeError:
            continue
    return entries


def _iter_file_lines_reverse(path: Path, *, _chunk_size: int = 65536) -> Iterator[str]:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        buffer = b""
        trailing_newline = True
        while position > 0:
            size = min(_chunk_size, position)
            position -= size
            handle.seek(position)
            chunk = handle.read(size)
            if not chunk:
                break
            buffer = chunk + buffer
            parts = buffer.split(b"\n")
            buffer = parts[0]
            for line in reversed(parts[1:]):
                if trailing_newline and line == b"":
                    trailing_newline = False
                    continue
                trailing_newline = False
                yield line.rstrip(b"\r").decode("utf-8")
        if buffer:
            yield buffer.rstrip(b"\r").decode("utf-8")


def iter_trace_index() -> Iterator[dict]:
    """Stream every trace index entry in append order (oldest first).

    Unlike ``list_traces`` (newest-first, capped at 100), this yields the
    full index so read-only analytics can filter the whole history by a
    timestamp range. Skips unparseable lines defensively.
    """
    index_path = _traces_dir() / "index.jsonl"
    if not index_path.exists():
        return
    for line in index_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def get_trace(trace_id: str) -> Optional[dict]:
    """Find and load a full trace by trace_id."""
    if not _traces_dir().exists():
        return None
    for session_dir in _traces_dir().iterdir():
        if not session_dir.is_dir():
            continue
        trace_path = session_dir / f"{trace_id}.json"
        if trace_path.exists():
            try:
                return json.loads(trace_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
    return None


def search_traces(query: str, limit: int = 50) -> list[dict]:
    """Grep across index.jsonl for matching entries."""
    index_path = _traces_dir() / "index.jsonl"
    if not index_path.exists():
        return []

    results = []
    query_lower = query.lower()
    for line in reversed(index_path.read_text(encoding="utf-8").splitlines()):
        if query_lower in line.lower():
            try:
                results.append(json.loads(line.strip()))
                if len(results) >= limit:
                    break
            except json.JSONDecodeError:
                continue
    return results


def grep_traces(
    pattern: str,
    field: str = "all",
    session_id: Optional[str] = None,
    step_type: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Deep search into full trace files, matching against prompts/outputs.

    Args:
        pattern: text to search for (case-insensitive)
        field: "prompts", "outputs", "all"
        session_id: filter to a specific session
        step_type: filter to a specific step type (routing, thread_execution, etc.)
        limit: max results

    Returns list of matches: {trace_id, step_index, step_type, field, match_context, ...}
    """
    return trace_grep_index.search(
        pattern,
        traces_dir=_traces_dir(),
        field=field,
        session_id=session_id,
        step_type=step_type,
        limit=limit,
    )


def get_latest_trace(session_id: Optional[str] = None) -> Optional[dict]:
    """Get the most recent full trace, optionally for a specific session."""
    entries = list_traces(session_id=session_id, limit=1)
    if not entries:
        return None
    return get_trace(entries[0]["trace_id"])


def get_trace_stats(session_id: Optional[str] = None) -> dict:
    """Aggregate stats across all traces (or for a session)."""
    entries = list_traces(session_id=session_id, limit=10000)
    if not entries:
        return {"count": 0}

    total_duration = 0
    total_tokens: dict[str, int] = {}
    total_steps = 0
    step_type_counts: dict[str, int] = {}

    for entry in entries:
        total_duration += entry.get("duration_ms") or 0
        total_steps += entry.get("step_count", 0)
        for key, val in entry.get("total_token_usage", {}).items():
            total_tokens[key] = total_tokens.get(key, 0) + (val or 0)

    # For step type breakdown, load a sample of full traces
    sample_traces = entries[:20]
    for entry in sample_traces:
        trace = get_trace(entry["trace_id"])
        if trace:
            for step in trace.get("steps", []):
                st = step.get("step_type", "unknown")
                step_type_counts[st] = step_type_counts.get(st, 0) + 1

    return {
        "count": len(entries),
        "total_duration_ms": total_duration,
        "avg_duration_ms": round(total_duration / len(entries)) if entries else 0,
        "total_token_usage": total_tokens,
        "total_steps": total_steps,
        "step_type_counts": step_type_counts,
    }
