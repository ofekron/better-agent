"""Regression: manager sessions must register primary runs as kind=manager.

Run with:
    cd backend && .venv/bin/python scripts/test_manager_handler_run_kind.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-manager-kind-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from orchs.native import handle_turn  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class _TurnManager:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    async def run_turn(self, **kwargs) -> None:
        self.kwargs = kwargs


class _Coordinator:
    def __init__(self) -> None:
        self.turn_manager = _TurnManager()

    def is_session_cancelled(self, _sid: str) -> bool:
        return False


async def _noop_ws(_event: dict) -> None:
    return None


async def test_manager_session_passes_manager_run_mode() -> None:
    coord = _Coordinator()
    await handle_turn(
        coord,
        session={"id": "sid", "orchestration_mode": "manager", "bare_config": True},
        prompt="go",
        app_session_id="sid",
        model="sonnet",
        cwd="/tmp",
        ws_callback=_noop_ws,
        images=None,
    )
    assert coord.turn_manager.kwargs is not None
    assert coord.turn_manager.kwargs["mode"] == "manager", coord.turn_manager.kwargs
    print(f"{PASS} manager_session_passes_manager_run_mode")


def main() -> int:
    try:
        asyncio.run(test_manager_session_passes_manager_run_mode())
        print("ALL PASSED")
        return 0
    except AssertionError as exc:
        print(f"{FAIL}: {exc}")
        return 1
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
