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

from session_miner import SessionMiner, SessionVisit, sessions_dir  # noqa: E402

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
    assert state["sess-a.json"]["mtime"] == 1000.0
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


def test_mine_fans_out_one_pass_to_many_consumers() -> None:
    state: dict = {}
    _write_session("sess-c1", cwd="/tmp/c", messages=[{"role": "user", "content": "one"}])
    _write_session("sess-c2", cwd="/tmp/c", messages=[{"role": "user", "content": "two"}])

    seen_a: list[str] = []
    seen_b: list[str] = []

    def consumer_a(visit: SessionVisit) -> None:
        seen_a.append(visit.sid)

    def consumer_b(visit: SessionVisit) -> None:
        seen_b.append(visit.sid)

    miner = SessionMiner(state)
    scanned = miner.mine(consumer_a, consumer_b)

    assert scanned == miner.scanned_count
    assert "sess-c1" in seen_a and "sess-c1" in seen_b
    assert "sess-c2" in seen_a and "sess-c2" in seen_b
    assert seen_a == seen_b  # one pass fanned identically to every consumer
    print(f"{PASS} mine() fans one pass out to N consumers")


def main() -> int:
    test_changed_session_yields_visit()
    test_unchanged_session_is_delta_skipped()
    test_summary_and_unparseable_skipped()
    test_mine_fans_out_one_pass_to_many_consumers()
    print("\nOK: session_miner base behaves correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
