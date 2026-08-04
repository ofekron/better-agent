from __future__ import annotations

import math


UNIT_VECTOR_MAX_TOP_K = 200


def validate_unit_vector_bounds(
    top_k: object,
    min_score: object,
) -> tuple[int, float]:
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        raise ValueError("top_k must be an integer")
    if top_k < 1 or top_k > UNIT_VECTOR_MAX_TOP_K:
        raise ValueError(f"top_k must be between 1 and {UNIT_VECTOR_MAX_TOP_K}")
    if isinstance(min_score, bool) or not isinstance(min_score, (int, float)):
        raise ValueError("min_score must be a number")
    try:
        normalized_min_score = float(min_score)
    except OverflowError as exc:
        raise ValueError("min_score must be finite") from exc
    if not math.isfinite(normalized_min_score):
        raise ValueError("min_score must be finite")
    if normalized_min_score < 0.0 or normalized_min_score > 1.0:
        raise ValueError("min_score must be between 0 and 1")
    return top_k, normalized_min_score
