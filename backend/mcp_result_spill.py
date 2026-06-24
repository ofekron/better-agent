from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path
from typing import Any

TOKEN_CHAR_RATIO = 4
DEFAULT_TOKEN_LIMIT = 10_000


def spill_large_result(
    result: dict[str, Any],
    *,
    label: str,
    token_limit: int = DEFAULT_TOKEN_LIMIT,
) -> dict[str, Any]:
    raw = json.dumps(result, ensure_ascii=False, indent=2)
    estimated_tokens = _estimate_tokens(raw)
    if estimated_tokens <= token_limit:
        return result

    path = _write_tmp_result(raw, label=label)
    return _compact_result(result, path=path, estimated_tokens=estimated_tokens, char_count=len(raw))


def _estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / TOKEN_CHAR_RATIO)


def _write_tmp_result(raw: str, *, label: str) -> str:
    safe_label = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in label).strip("-") or "mcp-result"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix=f"{safe_label}-",
        suffix=".json",
        delete=False,
    ) as handle:
        handle.write(raw)
        return str(Path(handle.name))


def _compact_result(
    result: dict[str, Any],
    *,
    path: str,
    estimated_tokens: int,
    char_count: int,
) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "success": result.get("success", True),
        "result_spilled_to_file": True,
        "result_path": path,
        "result_estimated_tokens": estimated_tokens,
        "result_char_count": char_count,
        "message": "Full MCP result was too large and was written to result_path.",
    }
    for key in (
        "error",
        "count",
        "authoritative",
        "authority",
        "returncode",
        "stderr",
        "cwd_filter",
        "all_projects",
        "match_fields",
        "max_matches",
    ):
        if key in result:
            compact[key] = result[key]
    return compact
