from __future__ import annotations

from typing import Any, Optional


def token_usage_from_codex_usage(usage_data: Any) -> Optional[dict[str, int]]:
    if not isinstance(usage_data, dict) or not usage_data:
        return None

    def token_count(key: str) -> int:
        value = usage_data.get(key)
        return value if type(value) is int and value >= 0 else 0

    input_tokens = token_count("input_tokens")
    output_tokens = token_count("output_tokens")
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": token_count("cached_input_tokens"),
        "total_tokens": input_tokens + output_tokens,
    }
