"""Verify the shared session-mining base: delta watermark, skip rules, and the
one-pass fan-out to multiple consumers that keeps mining cost flat in the number
of extensions."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-session-miner-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from session_miner import (  # noqa: E402
    SessionConsumer,
    SessionMiner,
    SessionVisit,
    clear_consumers,
    mine_registered,
    register_consumer,
    sessions_dir,
)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _write_session(sid: str, *, cwd: str, messages: list[dict]) -> Path:
    root = sessions_dir()
    root.mkdir(parents=True, exist_ok=True)
    (root / sid).mkdir(parents=True, exist_ok=True)
    path = root / f"{sid}.json"
    path.write_text(json.dumps({"id": sid, "cwd": cwd, "messages": messages}), encoding="utf-8")
    return path


def _touch(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


def test_changed_session_yields_visit() -> None:
    state: dict = {}
    path = _write_session("sess-a", cwd="/tmp/a", messages=[{"role": "user", "content": "hi"}])
    _touch(path, 1000.0)

    miner = SessionMiner(state)
    visits = {v.sid: v for v in miner}

    visit = visits.get("sess-a")
    assert visit is not None, "changed session must yield a visit"
    assert visit.cwd == "/tmp/a"
    assert visit.messages == [{"role": "user", "content": "hi"}]
    assert isinstance(visit.events_by_msg_id, dict)
    assert "sess-a.json" not in state
    print(f"{PASS} changed session yields a normalized visit")


def test_unchanged_session_is_delta_skipped() -> None:
    state = {"sess-b.json": {"mtime": 2000.0}}
    path = _write_session("sess-b", cwd="/tmp/b", messages=[])
    _touch(path, 2000.0)

    miner = SessionMiner(state)
    sids = {v.sid for v in miner}

    assert "sess-b" not in sids  # watermark covers it
    assert miner.scanned_count >= 1  # counted even when skipped
    print(f"{PASS} unchanged session is delta-skipped but still counted")


def test_summary_and_unparseable_skipped() -> None:
    root = sessions_dir()
    root.mkdir(parents=True, exist_ok=True)
    (root / "sess-summary.summary.json").write_text(json.dumps({"id": "x"}), encoding="utf-8")
    (root / "sess-broken.json").write_text("{ not valid json", encoding="utf-8")

    state: dict = {}
    miner = SessionMiner(state)
    visits = list(miner)

    sids = {v.sid for v in visits}
    assert "sess-summary" not in sids
    assert "sess-broken" not in sids
    assert "sess-broken.json" not in state  # parse failure writes no watermark
    print(f"{PASS} .summary.json and unparseable sessions are skipped cleanly")


class _RecordingConsumer(SessionConsumer):
    name = "rec"
    all_visited: list[str] = []

    def begin(self) -> None:
        self.began = True
        self.committed = False
        self.visited = []

    def visit(self, visit: SessionVisit) -> None:
        self.visited.append(visit.sid)

    def commit(self) -> int:
        self.committed = True
        type(self).all_visited.extend(self.visited)
        return len(self.visited)


class _ConsumerA(_RecordingConsumer):
    name = "a"


class _ConsumerB(_RecordingConsumer):
    name = "b"


def test_mine_drives_consumer_lifecycle_one_pass() -> None:
    state: dict = {}
    _write_session("sess-c1", cwd="/tmp/c", messages=[{"role": "user", "content": "one"}])
    _write_session("sess-c2", cwd="/tmp/c", messages=[{"role": "user", "content": "two"}])

    a = _ConsumerA()
    b = _ConsumerB()
    counts = SessionMiner(state).mine([a, b])

    assert counts["a"] == counts["b"] == len(a.visited)
    assert a.began and a.committed and b.began and b.committed
    assert {"sess-c1", "sess-c2"} <= set(a.visited)
    assert a.visited == b.visited  # one pass fanned identically to every consumer
    print(f"{PASS} mine() drives begin/visit/commit and fans out to N consumers")


def test_mine_registered_runs_all_registered_consumers() -> None:
    clear_consumers()
    _ConsumerA.all_visited = []
    _ConsumerB.all_visited = []
    state: dict = {}
    _write_session("sess-d1", cwd="/tmp/d", messages=[{"role": "user", "content": "x"}])

    register_consumer(_ConsumerA)
    register_consumer(_ConsumerB)
    counts = mine_registered(state)

    assert counts["a"] == counts["b"]
    assert "sess-d1" in _ConsumerA.all_visited
    assert "sess-d1" in _ConsumerB.all_visited
    clear_consumers()
    print(f"{PASS} mine_registered() runs every registered consumer in one pass")


class _FailingCommitConsumer(SessionConsumer):
    name = "failing"

    def begin(self) -> None:
        self.visited = []

    def visit(self, visit: SessionVisit) -> None:
        self.visited.append(visit.sid)

    def commit(self) -> int:
        raise RuntimeError("commit failed")


class _FailingVisitConsumer(SessionConsumer):
    name = "failing_visit"

    def begin(self) -> None:
        pass

    def visit(self, visit: SessionVisit) -> None:
        raise RuntimeError("visit failed")

    def commit(self) -> int:
        return 0


def test_failed_commit_does_not_advance_watermark() -> None:
    state: dict = {}
    path = _write_session("sess-fail", cwd="/tmp/fail", messages=[{"role": "user", "content": "x"}])
    _touch(path, 3000.0)

    try:
        SessionMiner(state).mine([_FailingCommitConsumer()])
    except RuntimeError:
        pass
    else:
        raise AssertionError("consumer commit failure must propagate")

    assert "sess-fail.json" not in state
    visits = list(SessionMiner(state))
    assert "sess-fail" in {v.sid for v in visits}
    print(f"{PASS} failed consumer commit does not advance session watermark")


def test_failed_visit_does_not_advance_watermark() -> None:
    state: dict = {}
    path = _write_session("sess-visit-fail", cwd="/tmp/fail", messages=[{"role": "user", "content": "x"}])
    _touch(path, 4000.0)

    try:
        SessionMiner(state).mine([_ConsumerA(), _FailingVisitConsumer()])
    except RuntimeError:
        pass
    else:
        raise AssertionError("consumer visit failure must propagate")

    assert "sess-visit-fail.json" not in state
    visits = list(SessionMiner(state))
    assert "sess-visit-fail" in {v.sid for v in visits}
    print(f"{PASS} failed consumer visit does not advance session watermark")


def main() -> int:
    test_changed_session_yields_visit()
    test_unchanged_session_is_delta_skipped()
    test_summary_and_unparseable_skipped()
    test_mine_drives_consumer_lifecycle_one_pass()
    test_mine_registered_runs_all_registered_consumers()
    test_failed_commit_does_not_advance_watermark()
    test_failed_visit_does_not_advance_watermark()
    print("\nOK: session_miner base behaves correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
