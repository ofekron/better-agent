"""Locks the `_has_final` owner-dict cache added to close the
`has_final_answer_event` O(N^2) hot path.

Pre-fix: `_refresh_message_content_from_event_projection` (called from
`apply_event` on EVERY event append/replace) called
`event_shape.has_final_answer_event(events)` — a full linear scan of the
message's entire event list — on every non-final event, making
ingestion of a message with N events O(N^2).

Post-fix: `_has_final` mirrors the existing `_uid_idx` cache pattern —
lazily built once on the events-list owner (`msg` for the single
NativeStrategy), read in O(1) thereafter, and invalidated at every site
that bypasses `apply_event` and mutates the events list directly
(`set_native_events`, `_strip_synthetic_events_from_tree`,
`_merge_render_rows`, worker-panel resets).

This test asserts:
  1. Appending a final-marked event sets `_has_final=True`.
  2. A later append of a NON-final event does not un-set it (monotonic).
  3. Same-uuid REPLACE that ADDS final_answer=True (e.g. a Gemini-style
     streaming update mutating the last chunk in place) correctly
     flips the cache from unset/False to True — not stale.
  4. Lazy fallback: absent cache + a real scan populates it correctly.
  5. `set_native_events` invalidates `_has_final` (external mutation
     bypassing apply_event).
  6. Perf: `has_final_answer_event` (the O(N) fallback scan) is called
     O(1) times across N non-final appends to the same message, not
     O(N) times.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_has_final_cache.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-has-final-")

from orchs import ApplyEventCtx, get_strategy  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def teardown_module() -> None:
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def _make_session(name: str) -> tuple[str, dict]:
    sess = session_manager.create(
        name=name, model="sonnet", cwd="/tmp/has-final-test",
        orchestration_mode="native", source="cli",
    )
    sid = sess["id"]
    session_manager.append_assistant_msg(
        sid, {"id": "msg-1", "role": "assistant", "content": "", "events": []},
    )
    msg = session_manager.get_ref(sid)["messages"][-1]
    return sid, msg


def _ev(uid: str, text: str = "x", *, final: bool = False) -> dict:
    data = {
        "uuid": uid,
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }
    if final:
        data["final_answer"] = True
    return {"type": "agent_message", "data": data}


def test_append_final_sets_cache_true() -> None:
    sid, msg = _make_session("has-final-append")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid, run_id="r")
    strategy.apply_event(
        app_session_id=sid, msg=msg, event=_ev("u0", "hello", final=True),
        ctx=ctx, source_is_provider_stream=False,
    )
    assert msg.get("_has_final") is True


def test_append_non_final_after_final_stays_true() -> None:
    sid, msg = _make_session("has-final-monotonic")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid, run_id="r")
    strategy.apply_event(
        app_session_id=sid, msg=msg, event=_ev("u0", "first", final=True),
        ctx=ctx, source_is_provider_stream=False,
    )
    strategy.apply_event(
        app_session_id=sid, msg=msg, event=_ev("u1", "second", final=False),
        ctx=ctx, source_is_provider_stream=False,
    )
    assert msg.get("_has_final") is True


def test_replace_in_place_adds_final_flips_cache() -> None:
    """Same-uuid REPLACE (streaming update) whose new snapshot ADDS
    final_answer=True must flip the cache — not leave it stale."""
    sid, msg = _make_session("has-final-replace-add")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid, run_id="r")
    strategy.apply_event(
        app_session_id=sid, msg=msg, event=_ev("u-stream", "chunk-1", final=False),
        ctx=ctx, source_is_provider_stream=False,
    )
    # Cache lazily populated as False by the content-projection read path
    # (there was content to consider projecting).
    assert msg.get("_has_final") is False, f"expected False, got {msg.get('_has_final')!r}"
    strategy.apply_event(
        app_session_id=sid, msg=msg, event=_ev("u-stream", "chunk-1 chunk-2", final=True),
        ctx=ctx, source_is_provider_stream=False,
    )
    assert msg.get("_has_final") is True
    assert len(msg["events"]) == 1, "same uuid must replace, not append"


def test_lazy_scan_populates_cache_when_absent() -> None:
    from event_shape import has_final_answer_event

    sid, msg = _make_session("has-final-lazy-scan")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid, run_id="r")
    strategy.apply_event(
        app_session_id=sid, msg=msg, event=_ev("u0", "final chunk", final=True),
        ctx=ctx, source_is_provider_stream=False,
    )
    # Simulate a cold-loaded / externally-hydrated msg with no cache yet.
    msg.pop("_has_final", None)
    assert has_final_answer_event(msg["events"]) is True
    strategy.apply_event(
        app_session_id=sid, msg=msg, event=_ev("u1", "trailing", final=False),
        ctx=ctx, source_is_provider_stream=False,
    )
    assert msg.get("_has_final") is True, "lazy rescan must repopulate the cache correctly"


def test_set_native_events_invalidates_has_final() -> None:
    sid, msg = _make_session("has-final-invalidate")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid, run_id="r")
    strategy.apply_event(
        app_session_id=sid, msg=msg, event=_ev("u0", "final", final=True),
        ctx=ctx, source_is_provider_stream=False,
    )
    assert msg.get("_has_final") is True
    session_manager.set_native_events(sid, "msg-1", [_ev("y-1", "plain")])
    assert "_has_final" not in msg, "external mutation must invalidate the stale flag"


def test_has_final_scan_is_o1_not_on_per_event() -> None:
    """Perf lock: N non-final appends to the SAME message must call the
    O(N) fallback scan O(1) times (cache populated once), not N times."""
    import event_shape

    sid, msg = _make_session("has-final-perf")
    strategy = get_strategy("native")
    ctx = ApplyEventCtx(root_id=sid, run_id="r")

    scan_calls = {"n": 0}
    real_scan = event_shape.has_final_answer_event

    def _counting_scan(events):
        scan_calls["n"] += 1
        return real_scan(events)

    event_shape.has_final_answer_event = _counting_scan
    try:
        N = 200
        for i in range(N):
            strategy.apply_event(
                app_session_id=sid, msg=msg,
                event=_ev(f"u-{i}", f"chunk {i}", final=False),
                ctx=ctx, source_is_provider_stream=False,
            )
    finally:
        event_shape.has_final_answer_event = real_scan

    ok = scan_calls["n"] <= 1
    print(
        f"{PASS if ok else FAIL} {N} non-final appends triggered "
        f"has_final_answer_event {scan_calls['n']} time(s) (expected <= 1, was O(N) pre-fix)",
    )
    assert ok
    assert len(msg["events"]) == N


if __name__ == "__main__":
    tests = [
        test_append_final_sets_cache_true,
        test_append_non_final_after_final_stays_true,
        test_replace_in_place_adds_final_flips_cache,
        test_lazy_scan_populates_cache_when_absent,
        test_set_native_events_invalidates_has_final,
        test_has_final_scan_is_o1_not_on_per_event,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"{PASS} {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"{FAIL} {t.__name__}: {exc}")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    sys.exit(1 if failures else 0)
