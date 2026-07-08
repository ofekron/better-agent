from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime

import _test_home
_test_home.isolate("bc-test-llm-call-log-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import llm_call_log  # noqa: E402


def test_append_call_writes_single_jsonl_record():
    record = llm_call_log.append_call(
        source="turn",
        reason="manager",
        provider_id="p1",
        provider_kind="claude",
        provider_name="Claude",
        model="sonnet",
        reasoning_effort="high",
        app_session_id="sess",
        provider_session_id="claude-sid",
        trace_id="tr_1",
        run_id="run_1",
        prompt="hello\nworld",
        token_usage={"input_tokens": "10", "output_tokens": 2, "bad": 99},
        success=True,
        error=None,
        metadata={"delegation_id": "d1", "secret_like": "x" * 300},
        timestamp=datetime(2026, 6, 1, 12, 0, 0),
    )
    rows = list(llm_call_log.iter_calls())
    assert len(rows) == 1
    assert rows[0]["id"] == record["id"]
    assert rows[0]["prompt_preview"] == "hello world"
    assert rows[0]["token_usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert rows[0]["metadata"]["delegation_id"] == "d1"
    assert len(rows[0]["metadata"]["secret_like"]) == 160


def test_iter_calls_skips_malformed_rows():
    path = llm_call_log._log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    with path.open("a", encoding="utf-8") as f:
        f.write("{bad json\n")
    llm_call_log.append_call(source="prompt_engineer", reason="refine", success=False)
    rows = list(llm_call_log.iter_calls())
    assert len(rows) == 1
    assert rows[0]["source"] == "prompt_engineer"


_TESTS = [v for k, v in sorted(globals().items())
          if k.startswith("test_") and callable(v)]


def main() -> int:
    failed = 0
    for t in _TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(_TESTS) - failed}/{len(_TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
