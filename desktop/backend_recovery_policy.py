from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    attempts: int
    backoff_seconds: int


def decide_recovery(
    *,
    attempts: int,
    healthy_seconds: int,
    limit: int,
    stability_seconds: int,
) -> RecoveryDecision:
    if attempts < 0 or healthy_seconds < 0:
        raise ValueError("recovery counters must not be negative")
    if not 1 <= limit <= 20:
        raise ValueError("recovery limit must be in 1..20")
    if not 1 <= stability_seconds <= 3600:
        raise ValueError("stability window must be in 1..3600")
    if healthy_seconds >= stability_seconds:
        attempts = 0
    if attempts >= limit:
        return RecoveryDecision("circuit_open", attempts, 0)
    attempts += 1
    return RecoveryDecision("restart", attempts, min(1 << (attempts - 1), 8))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", required=True, type=int)
    parser.add_argument("--healthy-seconds", required=True, type=int)
    parser.add_argument("--limit", required=True, type=int)
    parser.add_argument("--stability-seconds", required=True, type=int)
    args = parser.parse_args(argv)
    decision = decide_recovery(
        attempts=args.attempts,
        healthy_seconds=args.healthy_seconds,
        limit=args.limit,
        stability_seconds=args.stability_seconds,
    )
    print(
        f"{decision.action}|{decision.attempts}|{decision.backoff_seconds}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
