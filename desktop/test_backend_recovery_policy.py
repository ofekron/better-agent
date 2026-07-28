from __future__ import annotations

from backend_recovery_policy import decide_recovery


def test_unstable_exits_back_off_then_open_circuit() -> None:
    attempts = 0
    backoffs = []
    for _ in range(5):
        decision = decide_recovery(
            attempts=attempts,
            healthy_seconds=0,
            limit=5,
            stability_seconds=60,
        )
        assert decision.action == "restart"
        attempts = decision.attempts
        backoffs.append(decision.backoff_seconds)

    assert backoffs == [1, 2, 4, 8, 8]
    assert decide_recovery(
        attempts=attempts,
        healthy_seconds=0,
        limit=5,
        stability_seconds=60,
    ).action == "circuit_open"


def test_stable_generation_resets_attempts() -> None:
    decision = decide_recovery(
        attempts=5,
        healthy_seconds=60,
        limit=5,
        stability_seconds=60,
    )
    assert decision.action == "restart"
    assert decision.attempts == 1
    assert decision.backoff_seconds == 1
