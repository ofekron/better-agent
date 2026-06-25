"""Processor findings with transcript evidence must be queued for canonical
unit extraction — parse, enqueue, dedup — without touching real state."""
import json
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(REPO / "better-agent-private" / "extensions" / "requirements"))

import paths

TEST_HOME = Path(tempfile.mkdtemp(prefix="ba-test-processor-findings-"))
paths.engage_test_home(TEST_HOME)

import requirement_context
from requirement_analysis import on_demand


def test_parse_clean_json():
    text = json.dumps({"requirements": [{"text": "keep dev stable", "evidence": {"unit_source_key": "k"}}]})
    rows = requirement_context.parse_processor_requirements(text)
    assert len(rows) == 1 and rows[0]["text"] == "keep dev stable", rows


def test_parse_json_with_commentary():
    text = 'Here is the result:\n{"requirements": [{"text": "a"}, {"text": "b"}]}\nDone.'
    rows = requirement_context.parse_processor_requirements(text)
    assert [r["text"] for r in rows] == ["a", "b"], rows


def test_parse_garbage_and_empty():
    assert requirement_context.parse_processor_requirements("") == []
    assert requirement_context.parse_processor_requirements("no json here {broken") == []
    assert requirement_context.parse_processor_requirements('{"other": 1}') == []


def test_record_targeted_windows_enqueues_and_dedups():
    findings = [
        {"path": "/t/session.jsonl", "element_index": 42, "sid": "sid-1", "ts_utc": "2026-07-17T00:00:00Z", "cwd": "/proj"},
    ]
    first = on_demand.record_targeted_windows(findings)
    assert first["recorded"] == 1, first
    def mine() -> list:
        return [w for w in on_demand._load_queue()["windows"] if w["path"] == "/t/session.jsonl"]

    windows = mine()
    assert len(windows) == 1, windows
    entry = windows[0]
    assert entry["start_index"] == 42 - on_demand.WINDOW_BEFORE
    assert entry["end_index"] == 42 + on_demand.WINDOW_AFTER
    assert entry["status"] == "pending"

    second = on_demand.record_targeted_windows(findings)
    assert second["recorded"] == 0, second
    assert len(mine()) == 1, mine()


def test_record_targeted_windows_rejects_incomplete_evidence():
    result = on_demand.record_targeted_windows([
        {"sid": "sid-2"},
        {"path": "", "element_index": 3},
        {"path": "/t/x.jsonl"},
        "not-a-dict",
    ])
    assert result == {"recorded": 0, "reason": "no_transcript_evidence"}, result


def test_persist_skips_unit_backed_and_enqueues_transcript_backed():
    requirement_context._ensure_requirements_importable = lambda: None
    nudges = []
    requirement_context._ensure_on_demand_background_extraction = lambda: nudges.append(1) or {"running": True}
    text = json.dumps({"requirements": [
        {"text": "already durable", "evidence": {"unit_source_key": "user:sid:1:unit:0"}},
        {"text": "only in transcript", "cwd": "/proj",
         "evidence": {"path": "/t/other.jsonl", "element_index": 7, "sid": "sid-3", "ts_utc": "2026-07-17T01:00:00Z"}},
        {"text": "no evidence at all"},
    ]})
    result = requirement_context.persist_processor_findings(text)
    assert result.get("recorded") == 1, result
    queue = json.loads(on_demand.queue_path().read_text())
    matching = [w for w in queue["windows"] if w["path"] == "/t/other.jsonl"]
    assert len(matching) == 1, queue["windows"]
    assert matching[0]["cwd"] == "/proj"
    assert nudges == [1], nudges


def test_persist_no_transcript_evidence_is_noop():
    before = on_demand._load_queue()
    result = requirement_context.persist_processor_findings(
        json.dumps({"requirements": [{"text": "x", "evidence": {"unit_source_key": "k"}}]})
    )
    assert result == {"recorded": 0, "reason": "no_transcript_evidence"}, result
    after = on_demand._load_queue()
    assert before == after


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for test in tests:
            test()
            print(f"PASS {test.__name__}")
    finally:
        shutil.rmtree(TEST_HOME, ignore_errors=True)
    print(f"{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
