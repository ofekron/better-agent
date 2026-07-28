from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from event_bus import BusEvent, bus
import rate_limit_recovery


def test_waits_for_terminal_persisted_continuation_and_unsubscribes(monkeypatch):
    sessions = [{
        "id": "session-1",
        "provider_id": "zai",
        "model": "glm-5.2",
        "messages": [
            {
                "id": "failed",
                "role": "assistant",
                "retrying_until": "2026-01-01T00:00:00Z",
            },
            {
                "id": "recovered",
                "role": "assistant",
                "content": "done",
                "completed_at": "2026-01-01T00:00:01Z",
                "run_meta": {"provider_id": "zai", "model": "glm-5.2"},
            },
        ],
    }]
    monkeypatch.setattr(
        rate_limit_recovery.session_manager,
        "get",
        lambda _session_id: sessions[-1],
    )

    async def run():
        def start():
            asyncio.create_task(bus.publish(BusEvent(
                type="lifecycle.turn_complete",
                root_id="session-1",
                sid="session-1",
                payload={"reason": "success"},
                persist=False,
            )))
            return {"ok": True, "when": "now"}

        return await rate_limit_recovery.continue_and_wait(
            "session-1",
            "failed",
            start,
            timeout_seconds=1,
        )

    result = asyncio.run(run())

    assert result["assistant_message_id"] == "recovered"
    assert result["provider_id"] == "zai"
    assert result["model"] == "glm-5.2"
    assert result["run_meta"] == {"provider_id": "zai", "model": "glm-5.2"}
    assert not any(
        item.name.startswith("rate-limit-recovery-")
        for item in bus._subs
    )


def test_failed_or_missing_recovery_result_fails_closed_and_unsubscribes(
    monkeypatch,
):
    monkeypatch.setattr(
        rate_limit_recovery.session_manager,
        "get",
        lambda _session_id: {
            "id": "session-1",
            "messages": [
                {"id": "failed", "role": "assistant"},
                {
                    "id": "recovered",
                    "role": "assistant",
                    "completed_at": "2026-01-01T00:00:01Z",
                    "error": True,
                    "errorText": "fallback failed",
                },
            ],
        },
    )

    async def run():
        def start():
            asyncio.create_task(bus.publish(BusEvent(
                type="lifecycle.turn_complete",
                root_id="session-1",
                sid="session-1",
                payload={"reason": "error"},
                persist=False,
            )))
            return {"ok": True}

        return await rate_limit_recovery.continue_and_wait(
            "session-1",
            "failed",
            start,
            timeout_seconds=1,
        )

    try:
        asyncio.run(run())
    except rate_limit_recovery.RateLimitRecoveryError as exc:
        assert str(exc) == "fallback failed"
    else:
        raise AssertionError("failed recovery must fail closed")
    assert not any(
        item.name.startswith("rate-limit-recovery-")
        for item in bus._subs
    )
