from __future__ import annotations

import copy
import json
import os
import sys
import time
import uuid
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-canonical-narrow-copy-")

from event_ingester import event_ingester  # noqa: E402
import file_ref_resolver  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

_CWD = os.path.join(_TMP_HOME, "repo")
_FILE_REL = "backend/new_file.py"
_FILE_ABS = os.path.join(_CWD, _FILE_REL)
# Create the file so rewrite_text's disk check (assume_exists=False on the
# ingest path) fires and the persisted row carries a bcfile link.
os.makedirs(os.path.dirname(_FILE_ABS), exist_ok=True)
Path(_FILE_ABS).write_text("x", encoding="utf-8")


def _agent_message(text: str, *, uid: str | None = None, meta_rows: int = 0) -> dict:
    data: dict = {
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }
    if uid:
        data["uuid"] = uid
    if meta_rows:
        # Large payload that rewrite never touches — the old full deepcopy
        # paid O(meta_rows) here; the narrow copy shares it by reference.
        data["meta"] = {"rows": [f"row-{i}-" + "x" * 60 for i in range(meta_rows)]}
    return data


def _old_canonical(event_type: str, data: dict, cwd: str) -> dict:
    """Reference path: the prior full-deepcopy-then-rewrite semantics."""
    canonical = copy.deepcopy(data)
    file_ref_resolver.rewrite_event_data(event_type, canonical, cwd, assume_exists=True)
    return canonical


def test_canonical_equivalence_and_caller_isolation() -> bool:
    data = _agent_message(f"Updated {_FILE_ABS} with the fix.", uid=str(uuid.uuid4()))
    original_text = data["message"]["content"][0]["text"]
    expected = _old_canonical("agent_message", data, _CWD)

    got = event_ingester._canonical_data_for_storage(
        "agent_message", data, _CWD, True,
    )

    # Same logical result as the old full-deepcopy path, including the rewrite.
    if got != expected:
        print(f"{FAIL} canonical shape diverged from old deepcopy path")
        return False
    if "bcfile:" not in got["message"]["content"][0]["text"]:
        print(f"{FAIL} file ref was not rewritten in canonical")
        return False
    # Caller's live data must be unmutated.
    if data["message"]["content"][0]["text"] != original_text:
        print(f"{FAIL} caller's data was mutated by _canonical_data_for_storage")
        return False
    print(f"{PASS} canonical equivalence + caller isolation")
    return True


def test_legacy_frame_isolation() -> bool:
    # Legacy/orchestrator frames: rewrite reassigns top-level text/output/
    # thought/error/content strings. The isolator's `dict(data)` shallow copy
    # must cover those, locking the lockstep contract for the legacy branch.
    data = {
        "text": f"Updated {_FILE_ABS}.",
        "output": f"wrote {_FILE_ABS}",
        "meta": {"rows": ["x" * 40 for _ in range(1000)]},
    }
    original = copy.deepcopy(data)
    expected = _old_canonical("legacy_output_frame", data, _CWD)

    got = event_ingester._canonical_data_for_storage(
        "legacy_output_frame", data, _CWD, True,
    )
    if got != expected:
        print(f"{FAIL} legacy frame canonical diverged")
        return False
    if "bcfile:" not in got["text"]:
        print(f"{FAIL} legacy text not rewritten")
        return False
    if data != original:
        print(f"{FAIL} legacy frame caller data mutated")
        return False
    print(f"{PASS} legacy frame isolation")
    return True


def test_manager_event_isolation() -> bool:
    inner = _agent_message(f"Edited {_FILE_ABS}.", uid=str(uuid.uuid4()))
    data = {"event": {"type": "agent_message", "data": inner}}
    original = copy.deepcopy(data)
    expected = _old_canonical("manager_event", data, _CWD)

    got = event_ingester._canonical_data_for_storage(
        "manager_event", data, _CWD, True,
    )
    if got != expected:
        print(f"{FAIL} manager_event canonical diverged")
        return False
    if data != original:
        print(f"{FAIL} manager_event caller data mutated")
        return False
    print(f"{PASS} manager_event isolation")
    return True


def test_dedup_hash_equivalence() -> bool:
    data = _agent_message(f"Updated {_FILE_ABS}.", uid=str(uuid.uuid4()))
    old = _old_canonical("agent_message", data, _CWD)
    new = event_ingester._canonical_data_for_storage("agent_message", data, _CWD, True)
    if event_ingester._dedup_data_for_hash(old) != event_ingester._dedup_data_for_hash(new):
        print(f"{FAIL} dedup hash differs between old and new canonical path")
        return False
    print(f"{PASS} dedup hash equivalence")
    return True


def test_canonical_perf_gate() -> bool:
    # ~6 MB agent_message: a content block with a file ref plus a large meta
    # payload the old full deepcopy copied per-event. The narrow copy shares
    # meta by reference, so `_canonical_data_for_storage` is dominated by the
    # tiny rewrite instead of an O(payload) deepcopy.
    data = _agent_message(
        f"Updated {_FILE_ABS}.", uid=str(uuid.uuid4()), meta_rows=60000,
    )
    # Warm any first-call side effects.
    event_ingester._canonical_data_for_storage("agent_message", data, _CWD, True)
    timings = []
    for _ in range(7):
        t0 = time.perf_counter()
        event_ingester._canonical_data_for_storage("agent_message", data, _CWD, True)
        timings.append((time.perf_counter() - t0) * 1000.0)
    median = sorted(timings)[len(timings) // 2]
    # Before the fix this was a full copy.deepcopy of ~6 MB (~500 ms, and it
    # blocked the asyncio ingest loop). 50 ms is a clear fail-before/pass-after
    # gate: an accidental reintroduction of a full payload copy regresses it
    # far past this bound.
    if median >= 50.0:
        print(f"{FAIL} _canonical_data_for_storage median {median:.1f}ms >= 50ms (timings={timings})")
        return False
    print(f"{PASS} _canonical_data_for_storage median {median:.2f}ms < 50ms (timings={[round(t, 2) for t in timings]})")
    return True


def test_persisted_row_has_rewrite() -> bool:
    data = _agent_message(f"Updated {_FILE_ABS}.", uid=str(uuid.uuid4()))
    root_id = sid = str(uuid.uuid4())
    event_ingester.ingest(
        root_id, sid, "agent_message", data,
        source="test", msg_id=str(uuid.uuid4()), cwd_override=_CWD,
    )
    rows_path = Path(_TMP_HOME) / "sessions" / root_id / "events.jsonl"
    rows = [json.loads(ln) for ln in rows_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not rows or "bcfile:" not in json.dumps(rows[-1]):
        print(f"{FAIL} persisted row missing rewritten file ref")
        return False
    print(f"{PASS} persisted row carries rewritten file ref")
    return True


def main() -> int:
    tests = [
        test_canonical_equivalence_and_caller_isolation,
        test_legacy_frame_isolation,
        test_manager_event_isolation,
        test_dedup_hash_equivalence,
        test_canonical_perf_gate,
        test_persisted_row_has_rewrite,
    ]
    failed = 0
    for t in tests:
        if not t():
            failed += 1
    if failed:
        print(f"\n{FAIL} {failed}/{len(tests)} failed")
        return 1
    print(f"\n{PASS} {len(tests)}/{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
