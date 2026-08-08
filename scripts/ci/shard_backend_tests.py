#!/usr/bin/env python3
"""Deterministic, time-balanced sharding for the backend pytest suite.

Replaces the old `grep -l "def test_" scripts/test_*.py | sort | awk
'NR % N == s'` split (.github/workflows/backend-tests-selfhosted.yml), which
balanced by FILE COUNT, not measured time — a shard could randomly land both
multi-minute files (test_ws_outbox, test_installation_lifespan_policy) and
blow the critical path past every other shard.

Algorithm: longest-processing-time-first (LPT) bin packing. Sort files by
measured duration descending, greedily drop each one onto the currently
lightest shard. LPT is a well-known 4/3-approximation of optimal makespan for
this exact problem (minimize the max bin sum) and is trivial to implement
deterministically with the stdlib alone.

Timing input: scripts/ci/backend_test_timings.json, a committed snapshot of
per-file durations in seconds (keyed by basename without ".py", matching
JUnit's <testsuite><testcase file=...> naming) collected from a real CI run.
Files not present in the map (new since the snapshot was taken) get a default
weight of the map's median duration — a neutral guess that's neither "free"
(would make a shard silently overweight when several new files land in it)
nor "as expensive as the slowest file" (would over-penalize for no evidence).
The map itself is derived from real per-file durations (e.g. aggregated from
the `--junitxml` output backend-tests-selfhosted.yml already uploads per
shard); regenerate and commit a fresh copy here whenever timings drift enough
to matter — this script only consumes the map, it does not collect it.

Usage:
    python3 scripts/ci/shard_backend_tests.py --shard 0 --shards 2
    python3 scripts/ci/shard_backend_tests.py --shard 1 --shards 2 --predict

Prints one file path per line (relative to backend/, e.g. "scripts/test_foo.py")
for the requested shard index. --predict additionally prints the predicted
wall-clock sum for every shard to stderr, for use when tuning shard count.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "backend" / "scripts"
TIMINGS_PATH = Path(__file__).resolve().parent / "backend_test_timings.json"


def discover_test_files() -> list[str]:
    """Same universe as the old `grep -l "def test_" scripts/test_*.py | sort`.

    backend/scripts/ also holds standalone runner scripts (`test_*.py` with no
    `def test_`, exercised by their own harnesses, not pytest) — excluded here
    exactly like the old awk pipeline excluded them.
    """
    files = []
    for path in sorted(SCRIPTS_DIR.glob("test_*.py")):
        if "def test_" in path.read_text(encoding="utf-8", errors="surrogateescape"):
            files.append(path.name)
    return files


def load_timings() -> dict[str, float]:
    return json.loads(TIMINGS_PATH.read_text(encoding="utf-8"))


def weight_for(filename: str, timings: dict[str, float], default: float) -> float:
    key = filename[:-3] if filename.endswith(".py") else filename
    return timings.get(key, default)


def pack(
    files: list[str], timings: dict[str, float], shard_count: int
) -> tuple[list[list[str]], list[float]]:
    default = statistics.median(timings.values()) if timings else 0.0
    weighted = sorted(
        ((weight_for(f, timings, default), f) for f in files),
        key=lambda item: (-item[0], item[1]),
    )
    shards: list[list[str]] = [[] for _ in range(shard_count)]
    totals = [0.0] * shard_count
    for weight, filename in weighted:
        target = min(range(shard_count), key=lambda i: (totals[i], i))
        shards[target].append(filename)
        totals[target] += weight
    return shards, totals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=int, required=True, help="0-based shard index")
    parser.add_argument("--shards", type=int, required=True, help="total shard count")
    parser.add_argument(
        "--predict",
        action="store_true",
        help="print predicted per-shard wall-clock totals (seconds) to stderr",
    )
    args = parser.parse_args()

    if args.shards < 1:
        parser.error("--shards must be >= 1")
    if not (0 <= args.shard < args.shards):
        parser.error("--shard must be in [0, --shards)")

    files = discover_test_files()
    timings = load_timings()
    shards, totals = pack(files, timings, args.shards)

    if args.predict:
        for i, total in enumerate(totals):
            print(f"shard {i}: {len(shards[i])} files, {total:.1f}s", file=sys.stderr)

    for filename in shards[args.shard]:
        print(f"scripts/{filename}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
