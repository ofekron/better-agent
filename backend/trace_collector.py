"""Per-turn trace collection for analytics.

Each CLI call's token usage, timing, and step metadata are captured into a
TraceCollector and appended to ``~/.better-claude/traces/index.jsonl`` as a
compact single-line entry. The index is the substrate for the analytics
page; ``trace_step`` events stream live to the render tree via the WS
callback. Token-usage accounting helpers are reused across the live turn
path (runner / turn_manager / orchestrator).
"""

import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager
from typing import Awaitable, Callable, Iterable, Iterator, Optional

import portable_lock
from paths import ba_home

logger = logging.getLogger(__name__)

TOKEN_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

# Optional cache-write TTL breakdown. Present only when the provider
# reports it (Anthropic nests it as usage.cache_creation.ephemeral_*);
# omitted — never zero-filled — when absent, so the UI can distinguish
# "no 1h writes" from "provider doesn't report the split".
CACHE_BREAKDOWN_KEYS = (
    "cache_creation_5m_tokens",
    "cache_creation_1h_tokens",
)
_NESTED_CACHE_CREATION_FIELDS = {
    "cache_creation_5m_tokens": "ephemeral_5m_input_tokens",
    "cache_creation_1h_tokens": "ephemeral_1h_input_tokens",
}

# Only direct human input counts as a user turn. Delegated turns (mssg /
# team_ask / delegate_task), scheduled, supervisor, and internal/system turns
# are all non-user — they are BA-injected prompts, not a human typing.
USER_TURN_KINDS = frozenset({"direct_user"})
_TEAM_USER_SOURCES = {
    "mssg": "mssg",
    "team_ask": "team_ask",
    "delegate_task": "delegate_task",
}


def classify_turn_kind(
    *,
    source: Optional[str],
    user_initiated: bool,
    user_prompt: str,
) -> str:
    clean_source = str(source or "").strip()
    if clean_source in _TEAM_USER_SOURCES:
        return _TEAM_USER_SOURCES[clean_source]
    if user_initiated and user_prompt.strip():
        return "direct_user"
    if not clean_source and user_prompt.strip():
        return "direct_user"
    return "system"


def is_user_turn_index_entry(entry: dict) -> bool:
    kind = str((entry or {}).get("turn_kind") or "").strip()
    if kind:
        return kind in USER_TURN_KINDS
    source = str((entry or {}).get("turn_source") or (entry or {}).get("source") or "").strip()
    if source:
        return False
    return bool((entry or {}).get("user_prompt_preview"))


def _normalize_token_usage(usage: object) -> Optional[dict]:
    if not isinstance(usage, dict):
        return None
    if not any(k in usage for k in TOKEN_USAGE_KEYS):
        return None
    out = {k: int(usage.get(k) or 0) for k in TOKEN_USAGE_KEYS}
    nested = usage.get("cache_creation")
    nested = nested if isinstance(nested, dict) else {}
    for flat_key in CACHE_BREAKDOWN_KEYS:
        if flat_key in usage:
            out[flat_key] = int(usage.get(flat_key) or 0)
        elif _NESTED_CACHE_CREATION_FIELDS[flat_key] in nested:
            out[flat_key] = int(
                nested.get(_NESTED_CACHE_CREATION_FIELDS[flat_key]) or 0,
            )
    return out


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
        for key in CACHE_BREAKDOWN_KEYS:
            if key in normalized:
                total[key] = total.get(key, 0) + normalized[key]
    return total if saw_any else None


def merge_token_usages(usages: Iterable[dict]) -> Optional[dict]:
    """Public field-wise merge of token_usage dicts (base keys summed,
    cache-write TTL breakdown summed only where present)."""
    return _merge_usage(usages)


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


@contextmanager
def _index_lock() -> Iterator[None]:
    path = ba_home() / "traces_index.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        portable_lock.lock_ex(handle.fileno())
        yield
    finally:
        portable_lock.unlock(handle.fileno())
        handle.close()


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
    def duration_ms(self) -> Optional[int]:
        if self._started_at is not None and self._ended_at is not None:
            return round((self._ended_at - self._started_at) * 1000)
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

    Steps stream live to the render tree as ``trace_step`` WS events; on
    ``save()`` a compact index entry is appended to ``index.jsonl``, the
    substrate the analytics page reads.
    """

    def __init__(
        self,
        session_id: str,
        user_prompt: str,
        *,
        source: Optional[str] = None,
        user_initiated: bool = True,
    ):
        self.trace_id = f"tr_{uuid.uuid4().hex[:12]}"
        self.session_id = session_id
        self.user_prompt = user_prompt
        self.turn_source = str(source or "").strip()
        self.user_initiated = bool(user_initiated)
        self.turn_kind = classify_turn_kind(
            source=self.turn_source,
            user_initiated=self.user_initiated,
            user_prompt=user_prompt,
        )
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
    def total_duration_ms(self) -> Optional[int]:
        if self._ended_at is not None:
            return round((self._ended_at - self._started_at) * 1000)
        return None

    @property
    def total_token_usage(self) -> dict:
        total: dict[str, int] = {}
        for step in self.steps:
            if step.token_usage:
                for key, val in step.token_usage.items():
                    total[key] = total.get(key, 0) + (val or 0)
        return total

    def to_index_entry(self) -> dict:
        """Compact single-line entry appended to the index file."""
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "user_prompt_preview": self.user_prompt[:100],
            "turn_source": self.turn_source,
            "turn_kind": self.turn_kind,
            "user_initiated": self.user_initiated,
            "duration_ms": self.total_duration_ms,
            "step_count": len(self.steps),
            "total_token_usage": self.total_token_usage,
        }

    def save(self):
        """Append this turn's compact index entry to ``index.jsonl``."""
        try:
            index_path = _traces_dir() / "index.jsonl"
            index_path.parent.mkdir(parents=True, exist_ok=True)
            index_line = json.dumps(self.to_index_entry())
            with _index_lock():
                with open(index_path, "a", encoding="utf-8") as f:
                    f.write(index_line + "\n")
            logger.info(
                "Trace saved: %s (session=%s, steps=%d, duration=%sms)",
                self.trace_id, self.session_id, len(self.steps), self.total_duration_ms,
            )
        except Exception:
            logger.exception("Failed to save trace %s", self.trace_id)


# ============================================================================
# Token-usage helpers (consumed by the live turn path)
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
# Index reader (analytics substrate)
# ============================================================================

def iter_trace_index() -> Iterator[dict]:
    """Stream every trace index entry in append order (oldest first).

    Yields the full index so read-only analytics can filter the whole
    history by a timestamp range. Skips unparseable lines defensively.
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
