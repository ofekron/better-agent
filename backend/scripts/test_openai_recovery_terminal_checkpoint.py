from __future__ import annotations

import json
import os
import shutil
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-openai-terminal-recovery-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from provider_openai import OpenAIProvider  # noqa: E402
from runs_dir import runs_root  # noqa: E402


def test_terminal_checkpoint_restores_success_complete() -> bool:
    provider = OpenAIProvider({
        "id": "openai-test",
        "kind": "openai",
        "base_url": "http://127.0.0.1:1/v1",
        "api_key": "test",
    })
    run_id = "openai-terminal-window"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    terminal = {
        "success": True,
        "session_id": "openai-session",
        "error": None,
        "token_usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        "finished_at": "2026-06-28T00:00:00",
    }
    (run_dir / "terminal.json").write_text(json.dumps(terminal), encoding="utf-8")
    (run_dir / "state.json").write_text(json.dumps({
        "run_id": run_id,
        "mode": "native",
        "app_session_id": "app-session",
        "session_id": "openai-session",
        "jsonl_path": str(run_dir / "session_events.jsonl"),
    }), encoding="utf-8")
    (run_dir / "backend_state.json").write_text(json.dumps({
        "run_id": run_id,
        "provider_id": "openai-test",
        "provider_kind": "openai",
        "app_session_id": "app-session",
        "session_id": "openai-session",
    }), encoding="utf-8")

    recovered = provider.recover_in_flight(run_id_filter={run_id})
    if len(recovered) != 1:
        print(f"  expected one descriptor, got {recovered!r}")
        return False
    complete = json.loads((run_dir / "complete.json").read_text(encoding="utf-8"))
    return complete == terminal and recovered[0].get("has_complete_json") is True


def run() -> int:
    try:
        ok = test_terminal_checkpoint_restores_success_complete()
        print(("PASS" if ok else "FAIL") + " terminal checkpoint restores success complete")
        return 0 if ok else 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(run())
