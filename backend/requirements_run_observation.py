from __future__ import annotations

import json
from collections import Counter
from typing import Any

SEARCH_TOOLS = (
    "search_requirement_units_rg",
    "search_requirement_units_fts",
    "search_requirement_units_vector",
    "query_provider_native_transcript_index",
)


def observe_processor_attempt(
    *,
    provider_id: str | None,
    model: str | None,
    timings_ms: Any,
    dispatch_result: Any,
) -> dict[str, Any]:
    timings = timings_ms if isinstance(timings_ms, dict) else {}
    dispatch = dispatch_result if isinstance(dispatch_result, dict) else {}
    tool_uses, tool_results = _tool_activity(dispatch.get("events"))
    allowed_uses = [row for row in tool_uses if row["canonical_name"] is not None]
    observed_names = _unique(row["name"] for row in tool_uses)
    off_profile_names = _unique(
        row["name"] for row in tool_uses if row["canonical_name"] is None
    )
    first_allowed = allowed_uses[0]["canonical_name"] if allowed_uses else None
    first_search_latency = None
    if tool_uses and tool_uses[0]["canonical_name"] is not None:
        first_search_latency = _optional_number(
            timings.get("dispatch_runner_enqueue_to_first_tool_ms")
        )
    calls = Counter(row["canonical_name"] for row in allowed_uses)
    vector_uses = [
        row for row in allowed_uses
        if row["canonical_name"] == "search_requirement_units_vector"
    ]
    decoded_results: list[tuple[dict[str, Any], dict[str, Any]]] = []
    complete_results = True
    for use in allowed_uses:
        result = _decode_tool_result(tool_results.get(use["id"]))
        if result is None:
            complete_results = False
            continue
        decoded_results.append((use, result))
    result_counts_by_tool: dict[str, list[int]] = {}
    for use, result in decoded_results:
        count = _optional_int(result.get("count"))
        if count is not None:
            result_counts_by_tool.setdefault(use["canonical_name"], []).append(count)
    error_count = (
        sum(1 for _use, row in decoded_results if row.get("success") is False or bool(row.get("error")))
        if allowed_uses and complete_results and len(decoded_results) == len(allowed_uses)
        else None
    )
    truncated_count = (
        sum(1 for _use, row in decoded_results if row.get("truncated") is True)
        if allowed_uses and complete_results and len(decoded_results) == len(allowed_uses)
        else None
    )
    prepare_parts = [
        _optional_number(timings.get("ensure_lifecycle_ms")),
        _optional_number(timings.get("build_prompts_ms")),
    ]
    prepare_ms = (
        sum(value for value in prepare_parts if value is not None)
        if all(value is not None for value in prepare_parts)
        else None
    )
    return {
        "provider": {
            "provider_id": provider_id,
            "model": model,
            "resolve_ms": _optional_number(timings.get("resolve_config_ms")),
            "prepare_ms": prepare_ms,
            "dispatch_ms": _optional_number(timings.get("dispatch_ms")),
            "dispatch_to_runner_ms": None,
            "runner_to_native_session_ms": None,
            "first_event_ms": _optional_number(
                timings.get("dispatch_runner_enqueue_to_first_event_ms")
            ),
            "total_ms": _optional_number(timings.get("total_ms")),
        },
        "tools": {
            "observed_names": observed_names,
            "first_allowed_search_tool": first_allowed,
            "first_allowed_search_latency_ms": first_search_latency,
            "off_profile_names": off_profile_names,
            "policy_valid": not off_profile_names if tool_uses else None,
        },
        "search": {
            "rounds": max(calls.values(), default=0),
            "vector_rounds": len(vector_uses),
            "vector_params": [
                {
                    "top_k": _optional_int(row["input"].get("top_k")),
                    "min_score": _optional_number(row["input"].get("min_score")),
                }
                for row in vector_uses
            ],
            "calls_by_tool": dict(sorted(calls.items())),
            "non_vector_retries_by_tool": {
                name: count - 1
                for name, count in sorted(calls.items())
                if name != "search_requirement_units_vector" and count > 1
            },
            "result_counts_by_tool": dict(sorted(result_counts_by_tool.items())),
            "error_count": error_count,
            "truncated_count": truncated_count,
        },
    }


def _tool_activity(events: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    uses: list[dict[str, Any]] = []
    results: dict[str, Any] = {}
    if not isinstance(events, list):
        return uses, results
    for event in events:
        for block in _event_blocks(event):
            block_type = block.get("type")
            if block_type == "tool_use":
                name = str(block.get("name") or "")
                if not name:
                    continue
                tool_input = block.get("input")
                uses.append({
                    "id": str(block.get("id") or ""),
                    "name": name,
                    "canonical_name": _canonical_tool_name(name),
                    "input": tool_input if isinstance(tool_input, dict) else {},
                })
            elif block_type == "tool_result":
                tool_use_id = str(block.get("tool_use_id") or "")
                if tool_use_id:
                    results[tool_use_id] = block.get("content")
    return uses, results


def _event_blocks(event: Any) -> list[dict[str, Any]]:
    if not isinstance(event, dict):
        return []
    data = event.get("data")
    if not isinstance(data, dict):
        return []
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else data.get("content")
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _canonical_tool_name(name: str) -> str | None:
    return next((tool for tool in SEARCH_TOOLS if name.endswith(tool)), None)


def _decode_tool_result(content: Any) -> dict[str, Any] | None:
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        texts = [
            item.get("text")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return _decode_tool_result("".join(texts)) if texts else None
    if not isinstance(content, str):
        return None
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _optional_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value))
