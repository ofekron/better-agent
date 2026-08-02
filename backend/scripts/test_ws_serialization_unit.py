"""Unit tests for ``ws_serialization`` — WS frame JSON serialization.

Pure unit tests (no FastAPI/pydantic import chain) so they run identically
under the host ``.venv`` (Python 3.14) and the Docker test image (3.13).
"""

from __future__ import annotations

import asyncio
import json

import ws_serialization


# --------------------------------------------------------------------------- #
# metric_event_type
# --------------------------------------------------------------------------- #

def test_metric_event_type_non_dict_is_unknown() -> None:
    assert ws_serialization.metric_event_type("not a dict") == "unknown"
    assert ws_serialization.metric_event_type(42) == "unknown"
    assert ws_serialization.metric_event_type(None) == "unknown"
    assert ws_serialization.metric_event_type([("type", "x")]) == "unknown"


def test_metric_event_type_missing_or_empty_type_is_unknown() -> None:
    assert ws_serialization.metric_event_type({}) == "unknown"
    assert ws_serialization.metric_event_type({"type": ""}) == "unknown"
    assert ws_serialization.metric_event_type({"type": None}) == "unknown"
    assert ws_serialization.metric_event_type({"other_key": 1}) == "unknown"


def test_metric_event_type_non_string_type_is_unknown() -> None:
    assert ws_serialization.metric_event_type({"type": 7}) == "unknown"
    assert ws_serialization.metric_event_type({"type": ["x"]}) == "unknown"
    assert ws_serialization.metric_event_type({"type": {"nested": 1}}) == "unknown"


def test_metric_event_type_unrecognized_type_is_other() -> None:
    # Unknown types classify as "other" without dash normalization.
    assert ws_serialization.metric_event_type({"type": "attacker-value"}) == "other"
    assert ws_serialization.metric_event_type({"type": "no-such-event"}) == "other"


def test_metric_event_type_known_global_type_passes_through() -> None:
    import global_events

    sample = next(iter(global_events.GLOBAL_EVENT_TYPES))
    assert ws_serialization.metric_event_type({"type": sample}) == sample


def test_metric_event_type_known_transport_type_passes_through() -> None:
    for transport in ("turn_complete", "turn_start", "messages_replay", "agent_message"):
        assert ws_serialization.metric_event_type({"type": transport}) == transport


def test_metric_event_type_normalizes_dashes_in_known_types() -> None:
    # No current known event type carries a dash, so register one in the
    # binding ws_serialization actually reads to prove the normalize step
    # runs for recognized types (forward-compat if one is ever added).
    original = ws_serialization.GLOBAL_EVENT_TYPES
    ws_serialization.GLOBAL_EVENT_TYPES = original | {"future-dashed-event"}
    try:
        result = ws_serialization.metric_event_type({"type": "future-dashed-event"})
    finally:
        ws_serialization.GLOBAL_EVENT_TYPES = original
    assert result == "future_dashed_event"


# --------------------------------------------------------------------------- #
# dumps_ws_json
# --------------------------------------------------------------------------- #

def test_dumps_ws_json_roundtrips_compact_and_unicode() -> None:
    async def run() -> None:
        value = {"type": "agent_message", "text": "héllo wörld", "n": 1}
        frame = await ws_serialization.dumps_ws_json(value)
        assert type(frame) is ws_serialization.SerializedWebSocketFrame
        assert isinstance(frame, str)
        assert json.loads(frame) == value
        # Compact separators: no space after the colon.
        assert '"type":"agent_message"' in frame
        # ensure_ascii=False: non-ASCII survives literally.
        assert "héllo wörld" in frame

    asyncio.run(run())


def test_dumps_ws_json_rejects_nan_and_inf() -> None:
    async def expect_value_error(value: dict) -> None:
        try:
            await ws_serialization.dumps_ws_json(value)
        except ValueError:
            return
        raise AssertionError(f"expected ValueError for {value!r}")

    asyncio.run(expect_value_error({"x": float("nan")}))
    asyncio.run(expect_value_error({"x": float("inf")}))


def test_dumps_ws_json_stamps_monotonic_phase_timestamps() -> None:
    async def run() -> None:
        frame = await ws_serialization.dumps_ws_json({"type": "turn_start"})
        assert isinstance(frame.submit_at, float)
        assert isinstance(frame.start_at, float)
        assert isinstance(frame.done_at, float)
        assert frame.submit_at <= frame.start_at <= frame.done_at

    asyncio.run(run())


def test_dumps_ws_json_serializes_many_concurrent_frames() -> None:
    # The serializer runs on a 2-worker executor; a fan-out larger than the
    # pool must still serialize every frame in order.
    async def run() -> None:
        frames = await asyncio.gather(*(
            ws_serialization.dumps_ws_json({"type": "turn_start", "i": i})
            for i in range(20)
        ))
        assert len(frames) == 20
        for i, frame in enumerate(frames):
            assert type(frame) is ws_serialization.SerializedWebSocketFrame
            assert json.loads(frame)["i"] == i

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# executor lifecycle
# --------------------------------------------------------------------------- #

def test_shutdown_makes_dumps_raise_then_reopen_restores() -> None:
    ws_serialization.shutdown_ws_json_executor()

    async def expect_shutdown() -> None:
        try:
            await ws_serialization.dumps_ws_json({"type": "turn_start"})
        except RuntimeError as exc:
            assert "shut down" in str(exc)
            return
        raise AssertionError("expected RuntimeError after shutdown")

    try:
        asyncio.run(expect_shutdown())
        ws_serialization.reopen_ws_json_executor()

        async def ok() -> None:
            frame = await ws_serialization.dumps_ws_json({"type": "turn_start"})
            assert json.loads(frame)["type"] == "turn_start"

        asyncio.run(ok())
    finally:
        # Always leave the executor alive for the rest of the suite.
        ws_serialization.reopen_ws_json_executor()


def test_reopen_is_noop_when_executor_already_alive() -> None:
    # Idempotent: reopening with an executor present does not replace it.
    before = ws_serialization._WS_JSON_EXECUTOR
    ws_serialization.reopen_ws_json_executor()
    assert ws_serialization._WS_JSON_EXECUTOR is before


def test_shutdown_is_idempotent_when_executor_already_none() -> None:
    # Calling shutdown after the executor is already torn down is a no-op:
    # the `executor is not None` guard skips the already-gone executor.
    ws_serialization.shutdown_ws_json_executor()
    assert ws_serialization._WS_JSON_EXECUTOR is None
    try:
        ws_serialization.shutdown_ws_json_executor()
        assert ws_serialization._WS_JSON_EXECUTOR is None
    finally:
        ws_serialization.reopen_ws_json_executor()
