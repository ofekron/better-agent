"""Codex/Gemini provider contract: complete.json finalizes a turn even
while the runner process is still alive.

Run with:
    cd backend && .venv/bin/python scripts/test_non_claude_watch_complete_fires_on_file.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-nonclaude-watchfile-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from provider_codex import CodexProvider, RunState as CodexRunState  # noqa: E402
from provider_gemini import GeminiProvider, RunState as GeminiRunState  # noqa: E402

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


class _FakePopen:
    def __init__(self):
        self.pid = os.getpid()
        self._rc = None

    def poll(self):
        return self._rc


class _FakeTailer:
    def __init__(self):
        self.stopped = False
        self.normalizer = SimpleNamespace(
            context_window=None,
            context_tokens=None,
        )

    def stop(self):
        self.stopped = True


async def _run_provider_case(provider_cls, run_state_cls, label: str) -> None:
    run_dir = Path(tempfile.mkdtemp(prefix=f"{label}-run-", dir=_TMP_HOME))
    provider = provider_cls.__new__(provider_cls)
    provider._runs = {}
    provider.id = f"{label}-test"

    popen = _FakePopen()
    tailer = _FakeTailer()
    run_state_kwargs = {
        "run_id": f"{label}-run",
        "run_dir": run_dir,
        "popen": popen,
        "mode": "native",
        "app_session_id": f"{label}-sid",
        "queue": asyncio.Queue(),
        "tailer": tailer,
    }
    if label == "codex":
        run_state_kwargs["jsonl_path"] = None
    rs = run_state_cls(
        **run_state_kwargs,
    )
    provider._runs[rs.run_id] = rs

    (run_dir / "complete.json").write_text(json.dumps({
        "success": True,
        "session_id": f"{label}-agent-sid",
        "error": None,
        "token_usage": None,
    }), encoding="utf-8")

    watch = asyncio.create_task(provider._watch_complete(rs))
    event = await asyncio.wait_for(rs.queue.get(), timeout=5)
    check(
        event.type == "complete" and event.data.get("success") is True,
        f"{label}: complete event emitted from complete.json",
    )
    check(popen.poll() is None, f"{label}: runner still alive when complete emitted")
    await watch
    check(tailer.stopped, f"{label}: tailer stopped after finalization")
    check(rs.run_id not in provider._runs, f"{label}: run cleaned up")


async def _scenario() -> None:
    await _run_provider_case(CodexProvider, CodexRunState, "codex")
    await _run_provider_case(GeminiProvider, GeminiRunState, "gemini")


def main() -> int:
    asyncio.run(_scenario())
    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: Codex/Gemini _watch_complete fires on complete.json while alive")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
