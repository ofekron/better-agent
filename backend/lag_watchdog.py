"""Event-loop lag watchdog: heartbeat sampling, stack dumps, incident reports.

The lag monitor coroutine cannot run while its callback is delayed. This
daemon samples evidence while its heartbeat is stale without asserting a
cause: ready-queue starvation, a synchronous stack, and OS descheduling can
all produce the same stale heartbeat. The backend's stderr is not captured in
bundled/detached runs, so dumps go to ba_home/logs/backend-faulthandler.log.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import lag_incident_queue
import perf
from paths import ba_home
from secret_redaction import redact_secrets

logger = logging.getLogger(__name__)


def _lag_heartbeat_snapshot() -> dict[str, float]:
    return {
        "monotonic": time.monotonic(),
        "process_cpu": time.process_time(),
    }


_LAG_HEARTBEAT: list[dict[str, float]] = [_lag_heartbeat_snapshot()]
_LAG_LAST_DUMP: list[float] = [0.0]
_LAG_LOOP_EVIDENCE: dict[str, object] = {
    "sentinel_at": time.monotonic(),
    "sentinel_latency_ms": 0.0,
    "ready_depth": 0,
    "last_sentinel_callback": "startup",
    "monitor_task": "startup",
    "monitor_task_duration_ms": 0.0,
    "last_sentinel_duration_ms": 0.0,
}
_LAG_REPORT_BODY_LIMIT_BYTES = 18_000
_LAG_REPORT_MAX_EVIDENCE_LINES = 120
_LAG_REPORT_MAX_LINE_CHARS = 512
_LAG_REPORT_TRUNCATED = "[diagnostic evidence truncated]"


def _lag_watchdog_issue_ref(evidence: str) -> str:
    digest = hashlib.sha256(evidence.encode("utf-8")).hexdigest()[:16]
    return f"bug:lag-watchdog:{digest}"


def _lag_report_safe_path(path: Path) -> str:
    value = str(path.expanduser())
    home = str(Path.home())
    if value == home:
        return "~"
    if value.startswith(home + os.sep):
        return "~" + value[len(home):]
    return Path(value).name


def _lag_report_evidence(value: str) -> str:
    redacted = redact_secrets(value)
    lines = redacted.splitlines()[:_LAG_REPORT_MAX_EVIDENCE_LINES]
    return "\n".join(line[:_LAG_REPORT_MAX_LINE_CHARS] for line in lines)


def _serialize_lag_report(payload: dict[str, object]) -> bytes:
    def encode(candidate: dict[str, object]) -> bytes:
        return json.dumps(candidate, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    body = encode(payload)
    if len(body) <= _LAG_REPORT_BODY_LIMIT_BYTES:
        return body
    evidence = str(payload.get("evidence") or "")
    marker = _LAG_REPORT_TRUNCATED
    low, high = 0, len(evidence)
    best: bytes | None = None
    while low <= high:
        keep = (low + high) // 2
        candidate = dict(payload)
        candidate["evidence"] = evidence[:keep].rstrip() + "\n" + marker
        encoded = encode(candidate)
        if len(encoded) <= _LAG_REPORT_BODY_LIMIT_BYTES:
            best = encoded
            low = keep + 1
        else:
            high = keep - 1
    if best is None:
        raise ValueError("lag report metadata exceeds body limit")
    return best


def _safe_extension_error_detail(status: int, content: bytes) -> str:
    del content
    reasons = {
        400: "invalid request",
        401: "authentication required",
        403: "request forbidden",
        404: "endpoint not found",
        409: "request conflict",
        413: "request too large",
        422: "invalid request",
        429: "rate limited",
    }
    reason = reasons.get(status)
    if reason is None:
        reason = "request rejected" if 400 <= status < 500 else "extension backend failed"
    return redact_secrets(reason)


def _report_lag_watchdog_issue(
    *,
    label: str,
    heartbeat_age: float,
    dump_path: Path,
    evidence: str,
    stack_names: list[str],
) -> None:
    safe_evidence = _lag_report_evidence(evidence)
    safe_dump_path = _lag_report_safe_path(dump_path)
    payload = {
        "requirement_ref": _lag_watchdog_issue_ref(evidence),
        "summary": f"Event loop lag: {label} ~{heartbeat_age:.1f}s",
        "assistant_message": (
            "The backend event-loop lag watchdog captured a slowness incident "
            f"and wrote the full traceback dump to {safe_dump_path}."
        ),
        "evidence": safe_evidence,
        "source": "lag_watchdog",
        "severity": "high",
        "dump_path": safe_dump_path,
        "lag_label": label,
        "lag_seconds": heartbeat_age,
        "stack_names": [redact_secrets(str(name))[:120] for name in stack_names[:16]],
    }
    with perf.timed("lag_incident.enqueue"):
        try:
            lag_incident_queue.enqueue(_serialize_lag_report(payload))
        except lag_incident_queue.LagIncidentSpoolFull:
            perf.record_count("lag_incident.spool_full")
            logger.warning(
                "lag-watchdog: assistant bug-report spool full; keeping dump at %s",
                safe_dump_path,
            )


async def _dispatch_lag_watchdog_issue(body: bytes) -> lag_incident_queue.DispatchOutcome:
    import extension_backend_loader

    with perf.timed("lag_incident.assistant_roundtrip"):
        status, content = await asyncio.to_thread(
            extension_backend_loader.invoke_named_core_destination_sync,
            "assistant.lag-report",
            body_bytes=body,
            base_url=os.environ.get("BETTER_CLAUDE_BACKEND_URL", "http://localhost:8000"),
        )
    if status < 400:
        return lag_incident_queue.DispatchOutcome(True)
    if status == 410:
        perf.record_count("lag_incident.suppressed_disabled")
        return lag_incident_queue.DispatchOutcome(True)
    detail = _safe_extension_error_detail(status, content)
    category = "timeout" if status == 504 else "rejected" if status < 500 else "backend_error"
    logger.warning(
        "lag-watchdog: assistant board bug report retry status=%s category=%s detail=%s",
        status,
        category,
        detail,
    )
    retry_after = None
    if status == 503:
        try:
            retry_after = float(json.loads(content).get("retry_after"))
        except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
            retry_after = None
    return lag_incident_queue.DispatchOutcome(
        False,
        retryable=status in {429, 503, 504} or status >= 500,
        retry_after=retry_after,
    )


def _schedule_lag_sentinel(loop: asyncio.AbstractEventLoop) -> None:
    scheduled_at = time.monotonic()

    def sentinel() -> None:
        started = time.monotonic()
        _LAG_LOOP_EVIDENCE.update({
            "sentinel_at": started,
            "sentinel_latency_ms": (started - scheduled_at) * 1000.0,
            "ready_depth": len(getattr(loop, "_ready", ())),
            "last_sentinel_callback": "lag-sentinel",
        })
        _LAG_LOOP_EVIDENCE["last_sentinel_duration_ms"] = (
            time.monotonic() - started
        ) * 1000.0

    loop.call_soon(sentinel)


def _classify_lag_incident(
    *, heartbeat_age: float, incident_process_cpu: float,
    ready_depth: int, stack_names: list[str], stack_frame_ids: list[int],
) -> str:
    cpu_ratio = incident_process_cpu / heartbeat_age if heartbeat_age > 0 else 0.0
    inline = (
        len(set(stack_names)) == 1
        and len(set(stack_frame_ids)) == 1
        and stack_names[0] not in {
        "run_until_complete", "run_forever", "_run_once", "select",
        }
    )
    if ready_depth > 10:
        return "ready-queue CPU starvation candidate"
    if inline:
        return "blocking stack candidate"
    if cpu_ratio >= 0.5:
        return "process CPU/GIL starvation candidate"
    if cpu_ratio < 0.1:
        return "blocking I/O or OS deschedule candidate"
    return "heartbeat starvation candidate"


def _start_lag_watchdog(threshold: float = 1.5, cooldown: float = 5.0) -> None:
    dump_path = ba_home() / "logs" / "backend-faulthandler.log"
    try:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("lag-watchdog: cannot create logs dir")
        return

    loop_thread_id = threading.get_ident()

    def run() -> None:
        sampled_stale_heartbeat: float | None = None
        while True:
            time.sleep(0.5)
            now = time.monotonic()
            heartbeat = _LAG_HEARTBEAT[0]
            heartbeat_age = now - heartbeat["monotonic"]
            if heartbeat_age <= threshold:
                sampled_stale_heartbeat = None
                continue
            heartbeat_generation = heartbeat["monotonic"]
            if sampled_stale_heartbeat == heartbeat_generation or now - _LAG_LAST_DUMP[0] <= cooldown:
                continue
            sampled_stale_heartbeat = heartbeat_generation
            _LAG_LAST_DUMP[0] = now
            try:
                loop_evidence = dict(_LAG_LOOP_EVIDENCE)
                incident_process_cpu = max(0.0, time.process_time() - heartbeat["process_cpu"])
                dump_path.parent.mkdir(parents=True, exist_ok=True)
                mode = "w" if dump_path.exists() and dump_path.stat().st_size > 2_000_000 else "a"
                samples: list[tuple[float, dict[int, object]]] = []
                cpu_started = time.process_time()
                thread_cpu_started = time.thread_time() if hasattr(time, "thread_time") else None
                sample_started = time.monotonic()
                for _ in range(3):
                    samples.append((time.monotonic(), sys._current_frames()))
                    time.sleep(0.05)
                cpu_delta = time.process_time() - cpu_started
                wall_delta = time.monotonic() - sample_started
                thread_cpu_delta = (
                    time.thread_time() - thread_cpu_started
                    if thread_cpu_started is not None else None
                )
                loop_frames = [frames.get(loop_thread_id) for _, frames in samples]
                stack_names = [
                    frame.f_code.co_name if frame is not None else "missing"
                    for frame in loop_frames
                ]
                stack_frame_ids = [id(frame) if frame is not None else 0 for frame in loop_frames]
                sentinel_age = max(0.0, now - float(loop_evidence["sentinel_at"]))
                label = _classify_lag_incident(
                    heartbeat_age=heartbeat_age,
                    incident_process_cpu=incident_process_cpu,
                    ready_depth=int(loop_evidence["ready_depth"]),
                    stack_names=stack_names,
                    stack_frame_ids=stack_frame_ids,
                )
                evidence = (
                    f"event loop lag evidence heartbeat_age={heartbeat_age:.1f}s "
                    f"@ {datetime.now().isoformat()} label={label} "
                    f"sample_age_ms={sentinel_age * 1000.0:.1f} "
                    f"ready_depth={loop_evidence['ready_depth']} "
                    f"sentinel_latency_ms={loop_evidence['sentinel_latency_ms']} "
                        f"last_sentinel_callback={loop_evidence['last_sentinel_callback']} "
                        f"monitor_task={loop_evidence['monitor_task']} "
                        f"monitor_task_duration_ms={loop_evidence['monitor_task_duration_ms']} "
                        f"last_sentinel_duration_ms={loop_evidence['last_sentinel_duration_ms']} "
                    f"incident_process_cpu_ms={incident_process_cpu * 1000.0:.1f} "
                    f"incident_process_cpu_ratio={incident_process_cpu / heartbeat_age if heartbeat_age > 0 else 0.0:.3f} "
                    f"process_cpu_delta_ms={cpu_delta * 1000.0:.1f} "
                    f"watchdog_thread_cpu_delta_ms="
                    f"{thread_cpu_delta * 1000.0 if thread_cpu_delta is not None else -1.0:.1f} "
                    f"sample_overhead_ms={wall_delta * 1000.0:.1f}"
                )
                with open(dump_path, mode, encoding="utf-8") as fh:
                    fh.write(f"\n=== {evidence} ===\n")
                    for index, (sample_at, frames) in enumerate(samples):
                        fh.write(f"--- sample {index + 1} at={sample_at:.6f} ---\n")
                        frame = frames.get(loop_thread_id)
                        if frame is not None:
                            traceback.print_stack(frame, file=fh, limit=40)
                    fh.write("--- all-thread tops ---\n")
                    for thread_id, frame in samples[-1][1].items():
                        fh.write(
                            f"thread={thread_id} name={frame.f_code.co_name} "
                            f"file={frame.f_code.co_filename}:{frame.f_lineno}\n"
                        )
                _report_lag_watchdog_issue(
                    label=label,
                    heartbeat_age=heartbeat_age,
                    dump_path=dump_path,
                    evidence=evidence,
                    stack_names=stack_names,
                )
                logger.warning(
                    "lag-watchdog: %s ~%.1fs, dumped to %s",
                    label,
                    heartbeat_age,
                    dump_path,
                )
            except Exception:
                logger.exception("lag-watchdog dump failed")

    threading.Thread(target=run, daemon=True, name="lag-watchdog").start()


async def _event_loop_lag_monitor() -> None:
    """Heartbeat producer for the watchdog thread, plus the loop-lag warn log."""
    interval = 1.0
    warn_after = 0.5
    expected = time.monotonic() + interval
    loop = asyncio.get_running_loop()
    while True:
        task = asyncio.current_task()
        _LAG_LOOP_EVIDENCE["monitor_task"] = task.get_name() if task is not None else "none"
        _schedule_lag_sentinel(loop)
        await asyncio.sleep(interval)
        body_started = time.monotonic()
        now = time.monotonic()
        lag = now - expected
        if lag > warn_after:
            logger.warning("event loop lag %.3fs", lag)
        # Heartbeat for the lag watchdog thread: proves the loop is
        # alive. A sync blocker starves this coroutine, the heartbeat
        # goes stale, and the watchdog dumps the blocker mid-flight.
        _LAG_HEARTBEAT[0] = _lag_heartbeat_snapshot()
        expected = now + interval
        _LAG_LOOP_EVIDENCE["monitor_task_duration_ms"] = (
            time.monotonic() - body_started
        ) * 1000.0


def start() -> None:
    """Arm the watchdog: reset the heartbeat, run the monitor, start the thread.

    The heartbeat is reset first because the module-level init at import time is
    long stale by the time startup gets here (heavy imports), which would
    otherwise trip one spurious "blocked" dump before the monitor stamps its
    first cycle.
    """
    _LAG_HEARTBEAT[0] = _lag_heartbeat_snapshot()
    asyncio.create_task(_event_loop_lag_monitor(), name="event-loop-lag-monitor")
    _start_lag_watchdog()
