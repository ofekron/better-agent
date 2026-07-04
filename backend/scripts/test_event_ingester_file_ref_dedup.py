from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-file-ref-dedup-")

import event_ingester as event_ingester_module  # noqa: E402
from event_ingester import event_ingester  # noqa: E402

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def _event(uid: str, text: str = "Success. Updated the following files:\nA backend/new_file.py\n") -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "call_file_ref",
                "content": text,
            }],
        },
        "uuid": uid,
    }


def _rows(root_id: str) -> list[dict]:
    path = Path(_TMP_HOME) / "sessions" / root_id / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _with_ref_ctx(repo: Path, assume_exists: bool):
    original = event_ingester_module._ref_ctx_for_root
    event_ingester_module._ref_ctx_for_root = lambda _root_id: (str(repo), assume_exists)
    return original


def _restore_ref_ctx(original) -> None:
    event_ingester_module._ref_ctx_for_root = original


def test_single_ingest_dedupes_file_ref_only_rewrite() -> bool:
    root_id = sid = str(uuid.uuid4())
    msg_id = str(uuid.uuid4())
    repo = Path(_TMP_HOME) / "repo-single"
    uid = str(uuid.uuid4())
    data = _event(uid)

    first = event_ingester.ingest(
        root_id, sid, "agent_message", copy.deepcopy(data),
        source="test", msg_id=msg_id, cwd_override=str(repo),
    )
    event_ingester.close(root_id)

    original = _with_ref_ctx(repo, True)
    try:
        second = event_ingester.ingest(
            root_id, sid, "agent_message", copy.deepcopy(data),
            source="test", msg_id=msg_id,
        )
    finally:
        _restore_ref_ctx(original)
    rows = _rows(root_id)
    ok = first == 1 and second == -1 and len(rows) == 1
    if not ok:
        print(f"  first={first} second={second} rows={len(rows)}")
    event_ingester.close(root_id)
    return ok


def test_batch_ingest_dedupes_file_ref_only_rewrite() -> bool:
    root_id = sid = str(uuid.uuid4())
    msg_id = str(uuid.uuid4())
    repo = Path(_TMP_HOME) / "repo-batch"
    uid = str(uuid.uuid4())
    data = _event(uid)

    original = _with_ref_ctx(repo, False)
    try:
        first = event_ingester.ingest_batch(
            root_id,
            [(sid, "agent_message", copy.deepcopy(data), "test", None, msg_id)],
        )
    finally:
        _restore_ref_ctx(original)
    event_ingester.close(root_id)

    original = _with_ref_ctx(repo, True)
    try:
        second = event_ingester.ingest_batch(
            root_id,
            [(sid, "agent_message", copy.deepcopy(data), "test", None, msg_id)],
        )
    finally:
        _restore_ref_ctx(original)
    rows = _rows(root_id)
    ok = first == [1] and second == [-1] and len(rows) == 1
    if not ok:
        print(f"  first={first} second={second} rows={len(rows)}")
    event_ingester.close(root_id)
    return ok


def test_same_uuid_real_content_mutation_still_appends() -> bool:
    root_id = sid = str(uuid.uuid4())
    msg_id = str(uuid.uuid4())
    repo = Path(_TMP_HOME) / "repo-mutation"
    uid = str(uuid.uuid4())

    first = event_ingester.ingest(
        root_id, sid, "agent_message", _event(uid, "first backend/file.py"),
        source="test", msg_id=msg_id, cwd_override=str(repo),
    )
    second = event_ingester.ingest(
        root_id, sid, "agent_message", _event(uid, "second backend/file.py"),
        source="test", msg_id=msg_id, cwd_override=str(repo),
    )
    rows = _rows(root_id)
    ok = first == 1 and second == 2 and len(rows) == 2
    if not ok:
        print(f"  first={first} second={second} rows={len(rows)}")
    event_ingester.close(root_id)
    return ok


TESTS = [
    ("single ingest dedupes file-ref-only rewrite drift", test_single_ingest_dedupes_file_ref_only_rewrite),
    ("batch ingest dedupes file-ref-only rewrite drift", test_batch_ingest_dedupes_file_ref_only_rewrite),
    ("same uuid real content mutation still appends", test_same_uuid_real_content_mutation_still_appends),
]


def main() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = fn()
            except Exception as exc:
                ok = False
                print(f"  exception: {exc}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
        event_ingester.close_all()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
