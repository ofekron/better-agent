from __future__ import annotations

import hashlib
import json
from typing import Any


def logical_request_id(provider: str, run_id: str, questions: list[dict[str, Any]]) -> str | None:
    provider = str(provider or "").strip()
    run_id = str(run_id or "").strip()
    if not provider or not run_id:
        return None
    question_ids = [
        str(question.get("id") or "").strip()
        for question in questions
        if isinstance(question, dict)
    ]
    digest = hashlib.sha256(
        json.dumps(question_ids, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"{provider}:{run_id}:request_user_input:{digest}"
