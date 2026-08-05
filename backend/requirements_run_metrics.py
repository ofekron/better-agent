from __future__ import annotations

import logging
import time
from collections import Counter
from typing import Any, Callable

from requirements_run_observation import observe_processor_attempt

SCHEMA_VERSION = 1
PROJECTION_KEY = "requirements_metrics"
logger = logging.getLogger(__name__)


def new_metrics(created_at: float | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "coverage": "partial",
        "wall": {
            "created_at": created_at,
            "completed_at": None,
            "elapsed_ms": None,
        },
        "provider": {
            "provider_id": None,
            "model": None,
            "resolve_ms": None,
            "prepare_ms": None,
            "dispatch_ms": None,
            "dispatch_to_runner_ms": None,
            "runner_to_native_session_ms": None,
            "first_event_ms": None,
            "total_ms": None,
            "milestones": [],
        },
        "tools": {
            "observed_names": [],
            "first_allowed_search_tool": None,
            "first_allowed_search_latency_ms": None,
            "off_profile_names": [],
            "policy_valid": None,
        },
        "search": {
            "rounds": 0,
            "vector_rounds": 0,
            "vector_params": [],
            "calls_by_tool": {},
            "non_vector_retries_by_tool": {},
            "result_counts_by_tool": {},
            "error_count": None,
            "truncated_count": None,
        },
        "output": {
            "raw_count": None,
            "final_count": None,
            "dedup_removed": None,
            "schema_valid": None,
            "provenance_valid": None,
            "identified_evidence_count": None,
            "unit_evidence_count": None,
            "transcript_evidence_count": None,
        },
        "attempts": [],
    }


def record_milestone(
    request_id: str,
    milestone: str,
    fields: dict[str, Any],
    *,
    observed_at: float | None = None,
) -> None:
    if not request_id:
        return
    _persist(request_id, lambda current, created_at: apply_milestone(
        current,
        milestone,
        fields,
        observed_at=observed_at or time.time(),
        created_at=created_at,
    ))


def record_processor_attempt(
    request_id: str,
    attempt: int,
    *,
    result: Any | None = None,
    error: str | None = None,
    observed_at: float | None = None,
) -> None:
    if not request_id:
        return
    config = getattr(result, "config", None)
    observation = observe_processor_attempt(
        provider_id=str(getattr(config, "provider_id", "") or "") or None,
        model=str(getattr(config, "model", "") or "") or None,
        timings_ms=getattr(result, "timings_ms", None),
        dispatch_result=getattr(result, "dispatch_result", None),
    )
    observation.update(
        attempt=attempt,
        observed_at=observed_at or time.time(),
        error=error,
    )
    _persist(request_id, lambda current, created_at: apply_processor_attempt(
        current,
        observation,
        created_at=created_at,
    ))


def record_output(
    request_id: str,
    report: dict[str, Any],
    *,
    observed_at: float | None = None,
) -> None:
    if not request_id:
        return
    _persist(request_id, lambda current, created_at: apply_output_report(
        current,
        report,
        observed_at=observed_at or time.time(),
        created_at=created_at,
    ))


def _persist(
    request_id: str,
    reducer: Callable[[dict[str, Any], float | None], dict[str, Any]],
) -> None:
    import extension_jobs

    try:
        record = extension_jobs.read_record_strict("requirements", "processed", request_id)
        if record is None:
            return
        created_at = _optional_number(record.get("created_at"))
        extension_jobs.persist_projection(
            "requirements",
            "processed",
            request_id,
            PROJECTION_KEY,
            lambda current: reducer(current, created_at),
        )
    except Exception:
        logger.warning(
            "requirements_metrics_persist_failed request_id=%s",
            request_id,
            exc_info=True,
        )


def apply_milestone(
    current: dict[str, Any],
    milestone: str,
    fields: dict[str, Any],
    *,
    observed_at: float,
    created_at: float | None,
) -> dict[str, Any]:
    metrics = _coerce_metrics(current, created_at)
    provider = metrics["provider"]
    attempt = fields.get("attempt")
    if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0:
        metrics["current_attempt"] = attempt
    else:
        attempt = metrics.get("current_attempt")
    milestone_row: dict[str, Any] = {"name": milestone, "at": observed_at}
    if isinstance(attempt, int):
        milestone_row["attempt"] = attempt
    provider["milestones"].append(milestone_row)
    provider_id = fields.get("provider_id")
    model = fields.get("model")
    if isinstance(provider_id, str) and provider_id:
        provider["provider_id"] = provider_id
    if isinstance(model, str) and model:
        provider["model"] = model
    phase_pair = None
    output_field = ""
    if milestone == "runner_started":
        phase_pair = "dispatch_started"
        output_field = "dispatch_to_runner_ms"
    elif milestone == "native_session_started":
        phase_pair = "runner_started"
        output_field = "runner_to_native_session_ms"
    if phase_pair is not None:
        phase_started = next((
            row
            for row in reversed(provider["milestones"][:-1])
            if row.get("name") == phase_pair
            and row.get("attempt") == milestone_row.get("attempt")
        ), None)
        if phase_started is not None:
            provider[output_field] = max(
                0.0,
                (observed_at - float(phase_started["at"])) * 1000.0,
            )
    metrics["coverage"] = _coverage(metrics)
    return metrics


def apply_processor_attempt(
    current: dict[str, Any],
    observation: dict[str, Any],
    *,
    created_at: float | None,
) -> dict[str, Any]:
    metrics = _coerce_metrics(current, created_at)
    attempts = metrics["attempts"]
    attempt_number = int(observation.get("attempt") or len(attempts) + 1)
    attempts[:] = [row for row in attempts if row.get("attempt") != attempt_number]
    attempts.append(observation)
    attempts.sort(key=lambda row: int(row.get("attempt") or 0))
    _aggregate_attempts(metrics)
    metrics["coverage"] = _coverage(metrics)
    return metrics


def apply_output_report(
    current: dict[str, Any],
    report: dict[str, Any],
    *,
    observed_at: float,
    created_at: float | None,
) -> dict[str, Any]:
    metrics = _coerce_metrics(current, created_at)
    metrics["output"].update({
        key: report.get(key)
        for key in metrics["output"]
    })
    wall = metrics["wall"]
    wall["completed_at"] = observed_at
    if wall["created_at"] is not None:
        wall["elapsed_ms"] = max(0.0, (observed_at - wall["created_at"]) * 1000.0)
    metrics.pop("current_attempt", None)
    metrics["coverage"] = _coverage(metrics)
    return metrics


def _coerce_metrics(current: dict[str, Any], created_at: float | None) -> dict[str, Any]:
    if current.get("schema_version") != SCHEMA_VERSION:
        return new_metrics(created_at)
    metrics = current
    if metrics["wall"].get("created_at") is None:
        metrics["wall"]["created_at"] = created_at
    return metrics


def _aggregate_attempts(metrics: dict[str, Any]) -> None:
    attempts = metrics["attempts"]
    observed = [row for row in attempts if isinstance(row, dict)]
    if not observed:
        return
    latest = observed[-1]
    milestones = metrics["provider"]["milestones"]
    latest_provider = next((
        row["provider"]
        for row in reversed(observed)
        if row["provider"].get("provider_id")
    ), latest["provider"])
    milestone_timings = {
        key: metrics["provider"].get(key)
        for key in ("dispatch_to_runner_ms", "runner_to_native_session_ms")
    }
    metrics["provider"] = {**latest_provider, "milestones": milestones}
    metrics["provider"].update({
        key: value
        for key, value in milestone_timings.items()
        if value is not None
    })
    names = _unique(
        name
        for row in observed
        for name in row["tools"]["observed_names"]
    )
    off_profile = _unique(
        name
        for row in observed
        for name in row["tools"]["off_profile_names"]
    )
    first_row = next(
        (row for row in observed if row["tools"]["first_allowed_search_tool"]),
        None,
    )
    policies = [
        row["tools"]["policy_valid"]
        for row in observed
        if row["tools"]["policy_valid"] is not None
    ]
    metrics["tools"] = {
        "observed_names": names,
        "first_allowed_search_tool": (
            first_row["tools"]["first_allowed_search_tool"] if first_row else None
        ),
        "first_allowed_search_latency_ms": (
            first_row["tools"]["first_allowed_search_latency_ms"] if first_row else None
        ),
        "off_profile_names": off_profile,
        "policy_valid": False if False in policies else (True if policies else None),
    }
    calls: Counter[str] = Counter()
    non_vector_retries: Counter[str] = Counter()
    vector_params: list[dict[str, Any]] = []
    result_counts_by_tool: dict[str, list[int]] = {}
    error_counts: list[int | None] = []
    truncated_counts: list[int | None] = []
    rounds = 0
    for row in observed:
        search = row["search"]
        calls.update(search["calls_by_tool"])
        non_vector_retries.update(search["non_vector_retries_by_tool"])
        vector_params.extend(search["vector_params"])
        rounds += int(search["rounds"])
        for name, counts in search["result_counts_by_tool"].items():
            result_counts_by_tool.setdefault(name, []).extend(counts)
        error_counts.append(search["error_count"])
        truncated_counts.append(search["truncated_count"])
    metrics["search"] = {
        "rounds": rounds,
        "vector_rounds": len(vector_params),
        "vector_params": vector_params,
        "calls_by_tool": dict(sorted(calls.items())),
        "non_vector_retries_by_tool": {
            name: count
            for name, count in sorted(non_vector_retries.items())
            if count > 0
        },
        "result_counts_by_tool": dict(sorted(result_counts_by_tool.items())),
        "error_count": _sum_if_complete(error_counts),
        "truncated_count": _sum_if_complete(truncated_counts),
    }


def _coverage(metrics: dict[str, Any]) -> str:
    if not metrics["attempts"] or metrics["wall"]["completed_at"] is None:
        return "partial"
    if metrics["tools"]["policy_valid"] is None:
        return "partial"
    if metrics["search"]["error_count"] is None:
        return "partial"
    if metrics["output"]["schema_valid"] is None:
        return "partial"
    return "complete"


def _optional_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _unique(values: Any) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value))


def _sum_if_complete(values: list[int | None]) -> int | None:
    return sum(value for value in values if value is not None) if values and all(
        value is not None for value in values
    ) else None
