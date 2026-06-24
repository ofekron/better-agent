"""Provenance capture + idempotency (Phase 4).

Locks: (1) extract pulls the tool_use + the WHY (preceding thinking/text);
(2) re-recording the SAME event does NOT double-write the log — the
idempotency guard the convergence invariant requires (recovery replays the
same events; the live gate plus uuid-dedup must keep provenance.jsonl
single-rowed).
"""

import os
import sys
import tempfile

import _test_home
_test_home.isolate("bc_provtest_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stores import provenance_store  # noqa: E402

SID = "prov-test-sess"
failures = []


def _check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


def _event(tool_id, tool, why):
    return {
        "uuid": "evt-" + tool_id,
        "timestamp": "2026-06-05T00:00:00",
        "data": {"message": {"id": "msg-1", "content": [
            {"type": "thinking", "thinking": why},
            {"type": "tool_use", "id": tool_id, "name": tool,
             "input": {"command": "echo hi"}},
        ]}},
    }


def main():
    print("T1 extract: tool + WHY from preceding reasoning")
    rows = provenance_store.extract(_event("toolu_1", "Bash", "I need to print hi"))
    _check(len(rows) == 1, "one row per tool_use")
    if rows:
        r = rows[0]
        _check(r["tool"] == "Bash", "tool name captured")
        _check(r["why"] == "I need to print hi", "WHY captured from thinking block")
        _check(r["input"] == {"command": "echo hi"}, "input captured")
        _check(r["uuid"] == "toolu_1", "dedup key is the tool_use id")

    print("T2 idempotency: re-recording the SAME event does not double-write")
    ev = _event("toolu_2", "Bash", "again")
    w1 = provenance_store.record_from_event(SID, ev)
    w2 = provenance_store.record_from_event(SID, ev)  # replay
    _check(w1 == 1, "first record writes 1 row")
    _check(w2 == 0, "replay writes 0 rows (deduped by tool_use id)")
    stored = provenance_store.read(SID)
    _check(len([r for r in stored if r["uuid"] == "toolu_2"]) == 1,
           "exactly one row on disk for the replayed tool_use")

    print("T3 distinct tool_use ids accumulate")
    provenance_store.record_from_event(SID, _event("toolu_3", "Read", "read a file"))
    ids = {r["uuid"] for r in provenance_store.read(SID)}
    _check({"toolu_2", "toolu_3"} <= ids, "both distinct tool calls present")

    print("T4 restart: dedup survives process restart (recovery replays source_is_provider_stream=True)")
    before = len(provenance_store.read(SID))
    provenance_store._seen.clear()  # simulate a fresh process (in-memory set gone)
    # recovery re-runs the SAME events through apply_event(source_is_provider_stream=True)
    w_recovery = provenance_store.record_from_event(SID, _event("toolu_2", "Bash", "again"))
    w_recovery += provenance_store.record_from_event(SID, _event("toolu_3", "Read", "read a file"))
    after = len(provenance_store.read(SID))
    _check(w_recovery == 0, "recovery replay writes 0 rows (dedup hydrated from disk)")
    _check(after == before, f"row count unchanged across restart ({before} -> {after})")

    print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed checks")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
