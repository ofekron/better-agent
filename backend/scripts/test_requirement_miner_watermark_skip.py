from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import native_session_miner  # noqa: E402
import session_miner  # noqa: E402


def test_ba_session_miner_skips_unchanged_snapshot_before_json_parse(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    session_path = sessions / "s1.json"
    session_path.write_text(
        json.dumps({"id": "s1", "cwd": "/repo", "messages": []}),
        encoding="utf-8",
    )
    current_fp = (
        *session_miner._mtime(session_path),
        *session_miner._mtime(sessions / "s1" / "events.jsonl"),
    )
    miner = session_miner.SessionMiner({"s1.json": {"fp": current_fp}}, root=sessions)

    with mock.patch.object(session_miner.json, "loads", side_effect=AssertionError("parsed unchanged session")):
        assert list(miner) == []

    assert miner.scanned_count == 1


def test_ba_session_miner_parses_changed_snapshot(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    session_path = sessions / "s1.json"
    session_path.write_text(
        json.dumps({"id": "s1", "cwd": "/repo", "messages": []}),
        encoding="utf-8",
    )
    miner = session_miner.SessionMiner({}, root=sessions)

    visits = list(miner)

    assert len(visits) == 1
    assert visits[0].sid == "s1"


class _NativeTestMiner(native_session_miner._NativeMinerBase):
    def __init__(self, state: dict, candidate: native_session_miner.NativeCandidate) -> None:
        super().__init__(state)
        self._candidate = candidate

    def _resolve_transcript(self, data: dict, sid: str) -> Path | None:
        return None

    def iter_candidates(self):
        yield self._candidate


def _native_candidate(tmp_path: Path, mtime: float = 10.0) -> native_session_miner.NativeCandidate:
    transcript = tmp_path / "native.jsonl"
    transcript.write_text("", encoding="utf-8")
    return native_session_miner.NativeCandidate(
        key="native:s1",
        sid="s1",
        cwd="/repo",
        data={},
        transcript=transcript,
        mtime=mtime,
    )


def test_native_miner_skips_unchanged_candidate_before_parse(tmp_path: Path) -> None:
    candidate = _native_candidate(tmp_path)
    miner = _NativeTestMiner({"native:s1": {"fp": candidate.mtime}}, candidate)

    with mock.patch.object(candidate, "parse", side_effect=AssertionError("parsed unchanged native transcript")):
        assert list(miner) == []

    assert miner.scanned_count == 1


def test_native_miner_parses_changed_candidate(tmp_path: Path) -> None:
    candidate = _native_candidate(tmp_path)
    visit = session_miner.SessionVisit(
        sid="s1",
        cwd="/repo",
        data={},
        messages=[],
        events_by_msg_id={},
    )
    miner = _NativeTestMiner({}, candidate)

    with mock.patch.object(candidate, "parse", return_value=visit) as parse:
        visits = list(miner)

    assert visits == [visit]
    parse.assert_called_once_with()
