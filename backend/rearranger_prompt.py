from prompt_templates import render_prompt
"""Prompts + prompt helpers for the experimental Rearranger side session.

The Rearranger is a separate `claude -p` subprocess (not a manager, not a
worker) whose only job is to read a better-agent session's messages and
emit a hierarchical JSON tree describing what the user is trying to
achieve, across several depth levels.

Lifecycle (described here so the prompt stays self-contained):
  1. ONE global bootstrap session is created lazily the first time any
     better-agent session enables this feature. That session gets
     BOOTSTRAP_PROMPT and nothing else — it carries only the system
     framing, so the fork source stays small.
  2. Each better-agent session forks off the bootstrap once, sending the
     FULL initial messages as its first diff. The returned session id is
     the session's per-session rearranger sid.
  3. Every subsequent rearrangement for that session forks off the
     previous per-session sid, sending only the NEW messages since the
     last rearrangement. Because the fork carries the prior session's
     history, the rearranger "remembers" the prior tree — so each
     emission is a fresh FULL tree, not a patch.
"""


BOOTSTRAP_PROMPT = render_prompt("rearranger/bootstrap.md")


# Preview budgets for inlined trace-step input/output — keep prompts
# tight so the rearranger can run on small context.
_TRACE_PREVIEW_CHARS = 300


def _truncate(text: str, limit: int = _TRACE_PREVIEW_CHARS) -> str:
    if not isinstance(text, str):
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def project_trace_steps(traces: list[dict]) -> list[dict]:
    """Flatten a list of full trace dicts into compact step projections.

    Each returned entry is the shape consumed by the rearranger's
    <trace_steps_delta> block: trace_id, step_index, step_type,
    thread_name, duration_ms, input_preview, output_preview. Input and
    output are truncated so the prompt stays small.
    """
    out: list[dict] = []
    for trace in traces:
        trace_id = trace.get("trace_id")
        if not trace_id:
            continue
        for i, step in enumerate(trace.get("steps") or []):
            out.append({
                "trace_id": trace_id,
                "step_index": i,
                "step_type": step.get("step_type") or "unknown",
                "thread_name": step.get("thread_name"),
                "duration_ms": step.get("duration_ms"),
                "input_preview": _truncate(step.get("input_prompt") or ""),
                "output_preview": _truncate(step.get("raw_output") or ""),
            })
    return out


def build_diff_prompt(
    source_path: str,
    new_messages: list[dict],
    total_message_count: int,
    previous_message_count: int,
    new_trace_steps: list[dict],
) -> str:
    """Format the per-turn prompt sent to the rearranger.

    Inlines: (a) the diff of new better-agent messages since the last
    rearrangement, and (b) the projection of every new trace step
    referenced by those messages (one trace per assistant message).
    The rearranger uses the trace step list as the "leaves" to arrange
    under the user-intent hierarchy — `new_trace_steps` must therefore
    be the output of `project_trace_steps`.
    """
    import json

    # Strip the noisy fields that don't help infer user intent. We keep
    # `role`, `content`, `timestamp`, `trace_id` (so the rearranger can
    # match messages to trace_refs), and a trimmed assistant text so
    # the rearranger sees the conversation without drowning in tool_use
    # bytes and worker event arrays.
    def _trim(msg: dict) -> dict:
        trimmed: dict = {
            "role": msg.get("role"),
            "content": msg.get("content", ""),
        }
        if msg.get("timestamp"):
            trimmed["timestamp"] = msg["timestamp"]
        if msg.get("trace_id"):
            trimmed["trace_id"] = msg["trace_id"]
        if msg.get("stopped_at"):
            trimmed["stopped_at"] = msg["stopped_at"]
        return trimmed

    trimmed_messages = [_trim(m) for m in new_messages]

    parts: list[str] = []
    parts.append(f"<source_path>{source_path}</source_path>")
    parts.append(
        f"<messages_delta previous_count={previous_message_count} "
        f"total_count={total_message_count} new_count={len(new_messages)}>"
    )
    parts.append(json.dumps(trimmed_messages, indent=2, ensure_ascii=False))
    parts.append("</messages_delta>")
    parts.append(
        f"<trace_steps_delta new_count={len(new_trace_steps)}>"
    )
    parts.append(json.dumps(new_trace_steps, indent=2, ensure_ascii=False))
    parts.append("</trace_steps_delta>")
    return "\n".join(parts) + "\n"
