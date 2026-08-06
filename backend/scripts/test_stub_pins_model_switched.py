"""Regression: model_switched must survive message stubbing on reload.

The reported bug — model-switch badges vanishing after reload — has two
necessary conditions in the backend:

1. `model_switched` is pinned into the collapsed stub preview
   (`stub.last_events`), since completed assistant messages ship with
   `events: []` and the frontend badge reads the stub tail.
2. The summaries sidecar version is bumped so pre-existing sessions
   (whose cached preview was computed before the pin) invalidate and
   rescan instead of loading a stale unpinned preview forever.

This covers the production summary path (`event_ingester.message_event_summaries`)
and the sidecar-version gate — not just the pure `render_stub.build_stub`
helper, which the snapshot builder does not call when
`use_journal_summaries=True`.
"""
import json
import os
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-stub-pins-model-switched-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import render_stub  # noqa: E402
from event_ingester import EventIngester, _EVENT_SUMMARIES_VERSION  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(cond: bool, name: str, detail: str = "") -> None:
    assert cond, f"{name}{(' -- ' + detail) if detail else ''}"
    print(f"{PASS} {name}")


def _agent_event(idx: int) -> dict:
    return {
        "uuid": f"a-{idx}",
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": f"tok{idx}"}]},
    }


def _model_switch_event(uid: str = "model-switch-1") -> dict:
    return {
        "uuid": uid,
        "model": "model-b",
        "previous_model": "model-a",
    }


def test_build_stub_pins_beyond_tail() -> None:
    """Pure-helper level: pinning keeps model_switched past the 25-tail."""
    events = [
        {"type": "model_switched", "data": _model_switch_event()}
    ] + [
        {"type": "agent_message", "data": _agent_event(i)}
        for i in range(render_stub.STUB_TAIL + 5)
    ]
    stub = render_stub.build_stub({"role": "assistant", "events": events})
    types = [e.get("type") for e in stub["last_events"]]
    _check(
        "model_switched" in types,
        "build_stub pins model_switched beyond tail",
        f"types={types[:3]}... len={len(types)}",
    )


def test_build_stub_caps_pinned_growth() -> None:
    """The pinned set is bounded: when many model_switched fall OUTSIDE the
    25-event tail, only the most recent _STUB_PINNED_CAP are kept."""
    n_switches = render_stub._STUB_PINNED_CAP + 5
    switches = [
        {"type": "model_switched", "data": _model_switch_event(f"ms-{i}")}
        for i in range(n_switches)
    ]
    # Enough agent events that every switch sits before the 25-event tail,
    # forcing them all into the pinned set.
    agents = [
        {"type": "agent_message", "data": _agent_event(i)}
        for i in range(render_stub.STUB_TAIL + 5)
    ]
    stub = render_stub.build_stub({"role": "assistant", "events": switches + agents})
    pinned = [e for e in stub["last_events"] if e.get("type") == "model_switched"]
    _check(
        len(pinned) == render_stub._STUB_PINNED_CAP,
        "build_stub caps pinned model_switched",
        f"pinned={len(pinned)} cap={render_stub._STUB_PINNED_CAP}",
    )


def _ingest_long_turn(ingester: EventIngester, root_id: str, msg_id: str) -> None:
    """One assistant message with a model_switched at the head and >STUB_TAIL
    agent_message events after it."""
    ingester.ingest(
        root_id, root_id, "model_switched", _model_switch_event(),
        source="selector_change", msg_id=msg_id,
    )
    for i in range(render_stub.STUB_TAIL + 5):
        ingester.ingest(
            root_id, root_id, "agent_message", _agent_event(i),
            source="provider", msg_id=msg_id,
        )


def test_summaries_path_pins_model_switched() -> None:
    """The REAL snapshot path: event_ingester.message_event_summaries must
    include model_switched in last_events for a long turn."""
    ingester = EventIngester()
    root_id = "root-summs-pin"
    msg_id = "assistant-1"
    _ingest_long_turn(ingester, root_id, msg_id)
    summaries = ingester.message_event_summaries(root_id, sid_filter=root_id)
    rec = summaries.get(msg_id)
    assert rec is not None, "msg missing from summaries"
    types = [e.get("type") for e in rec.get("last_events", [])]
    _check(
        "model_switched" in types,
        "summaries path pins model_switched",
        f"types={types}",
    )


def test_stale_sidecar_invalidated_by_version() -> None:
    """F1 regression: a sidecar written before the pin (summary_version=6)
    must be REJECTED so a full rescan re-pins model_switched. Without the
    version bump, existing sessions keep a stale unpinned preview forever.

    Proves the gate on a REAL ingester-written sidecar (structurally valid),
    mutated only in `summary_version` — so the rejection is attributable to
    the version, not malformed fields."""
    ingester = EventIngester()
    root_id = "root-sidecar-version"
    msg_id = "assistant-1"
    _ingest_long_turn(ingester, root_id, msg_id)
    # First read writes a valid sidecar at the current version.
    ingester.message_event_summaries(root_id, sid_filter=root_id)

    sidecar_path = ingester._event_summaries_path(root_id)
    events_path = ingester._events_path(root_id)
    tail = render_stub.STUB_TAIL
    original = json.loads(sidecar_path.read_text(encoding="utf-8"))

    # Sanity: the real sidecar loads and carries the pin.
    loaded = ingester._load_event_summaries_sidecar_locked(root_id, events_path, tail)
    assert loaded is not None, "real sidecar failed to load"
    rec = loaded[0].get(msg_id, {})
    assert "model_switched" in [e.get("type") for e in rec.get("last_events", [])], "real sidecar missing pin"

    # Mutate ONLY the version -> the load must now be rejected.
    stale = dict(original)
    stale["summary_version"] = 6
    sidecar_path.write_text(json.dumps(stale), encoding="utf-8")
    rejected = ingester._load_event_summaries_sidecar_locked(root_id, events_path, tail) is None

    # And a fresh reader, with no in-memory cache, rescans and re-pins.
    sidecar_path.write_text(json.dumps(original), encoding="utf-8")  # restore v7 for the rescan check
    fresh = EventIngester()
    fresh.message_event_summaries(root_id, sid_filter=root_id)  # warm
    fresh._summaries_cache.pop(root_id, None)  # force sidecar path
    # Re-stale it and read through message_event_summaries: rejected -> rescan.
    stale2 = json.loads(sidecar_path.read_text(encoding="utf-8"))
    stale2["summary_version"] = 6
    sidecar_path.write_text(json.dumps(stale2), encoding="utf-8")
    rescan = EventIngester()
    summaries = rescan.message_event_summaries(root_id, sid_filter=root_id)
    rec2 = summaries.get(msg_id, {})
    types2 = [e.get("type") for e in rec2.get("last_events", [])]
    rescan_pinned = "model_switched" in types2

    _check(
        rejected and rescan_pinned,
        f"stale sidecar (v6) rejected (gate={rejected}) and rescanned -> pinned ({rescan_pinned}); current v={_EVENT_SUMMARIES_VERSION}",
        f"types={types2}",
    )


TESTS = [
    test_build_stub_pins_beyond_tail,
    test_build_stub_caps_pinned_growth,
    test_summaries_path_pins_model_switched,
    test_stale_sidecar_invalidated_by_version,
]


def main() -> int:
    failures = 0
    for t in TESTS:
        try:
            t()
        except Exception as exc:
            print(f"{FAIL} {t.__name__}: {exc}")
            failures += 1
        else:
            print(f"{PASS} {t.__name__}")
    print(f"\n{'ALL PASS' if not failures else 'FAILURES'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
