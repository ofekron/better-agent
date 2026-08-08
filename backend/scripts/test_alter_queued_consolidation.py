#!/usr/bin/env python3
"""Failing-first unit coverage for backend/orchestrator.py's
`Coordinator.update_queued`/`update_latest_queued` consolidation (ADR 0006
§5 alter/edit_queued ruling): the two intents stay distinct on the wire,
but an alter that lands on a STILL-QUEUED prompt must route through the
SAME queued-item mutation `edit_queued` uses — never a second, parallel
implementation.

Isolated via `paths.engage_test_home` before any backend import, matching
backend/scripts/test_command_port.py's recipe. `update_queued`/
`update_latest_queued` touch only `self._prompt_queues` — a bare
`Coordinator.__new__(Coordinator)` with that one attribute set is a
complete, isolated fixture; no session_manager/journal/coordinator
wiring needed.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_alter_queued_consolidation.py -q
    PYTHONPATH=. python3 backend/scripts/test_alter_queued_consolidation.py
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import sys
import tempfile
from pathlib import Path

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402  (bare — matches sibling backend/scripts tests)

_TEST_HOME = tempfile.mkdtemp(prefix="ba-alter-queued-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

from orchestrator import Coordinator  # noqa: E402


def _bare_coordinator() -> Coordinator:
    """A Coordinator with none of its normal __init__ wiring — the two
    methods under test read/write only `self._prompt_queues`."""
    coord = Coordinator.__new__(Coordinator)
    coord._prompt_queues = {}
    return coord


def _seed_queue(coord: Coordinator, sid: str, items: list[dict]) -> "asyncio.Queue":
    q: asyncio.Queue = asyncio.Queue()
    for item in items:
        q.put_nowait(item)
    coord._prompt_queues[sid] = q
    return q


# ---- edit_queued's exact call shape (no alter-only kwargs) ----------------


def test_edit_queued_style_call_overwrites_prompt_and_set_cli_prompt() -> None:
    """Mirrors the pre-consolidation `update_queued` behavior exactly:
    `cli_prompt` is overwritten with the new `content` ONLY when it was
    already present; `client_id`/`lifecycle_msg_id`/`capability_contexts`
    are untouched — edit_queued's caller never had those concepts."""
    async def _run() -> None:
        coord = _bare_coordinator()
        item = {"_queued_id": "q1", "prompt": "old", "cli_prompt": "old-cli", "client_id": "orig-cid"}
        q = _seed_queue(coord, "sid-1", [item])
        updated = await coord.update_queued("sid-1", "q1", "new text")
        assert updated is True
        out = q.get_nowait()
        assert out["prompt"] == "new text"
        assert out["cli_prompt"] == "new text"
        assert out["client_id"] == "orig-cid"
        assert "lifecycle_msg_id" not in out
        assert "capability_contexts" not in out
    asyncio.run(_run())


def test_edit_queued_style_call_leaves_absent_cli_prompt_absent() -> None:
    async def _run() -> None:
        coord = _bare_coordinator()
        item = {"_queued_id": "q1", "prompt": "old"}
        q = _seed_queue(coord, "sid-1", [item])
        await coord.update_queued("sid-1", "q1", "new text")
        out = q.get_nowait()
        assert "cli_prompt" not in out
    asyncio.run(_run())


def test_edit_queued_style_call_on_unknown_queued_id_reports_not_updated() -> None:
    async def _run() -> None:
        coord = _bare_coordinator()
        item = {"_queued_id": "q1", "prompt": "old"}
        q = _seed_queue(coord, "sid-1", [item])
        updated = await coord.update_queued("sid-1", "q-missing", "new text")
        assert updated is False
        # queue contents preserved unchanged, in order
        assert q.get_nowait() == item
    asyncio.run(_run())


# ---- alter's still-queued branch: routes through update_queued -----------


def test_alter_resend_on_queued_prompt_routes_through_update_queued() -> None:
    """`update_latest_queued` (alter's queued-prompt branch,
    `backend/surface_commands.py`'s `send_prompt` `send_mode == "alter"`
    territory) finds the LATEST item's id then delegates the actual
    mutation to `update_queued` — no second implementation."""
    async def _run() -> None:
        coord = _bare_coordinator()
        item1 = {"_queued_id": "q1", "prompt": "first"}
        item2 = {"_queued_id": "q2", "prompt": "second", "cli_prompt": "second-cli"}
        q = _seed_queue(coord, "sid-1", [item1, item2])
        queued_id = await coord.update_latest_queued(
            "sid-1", "altered text", "altered-cli", "new-cid", "lifecycle-1", [{"k": "v"}],
        )
        assert queued_id == "q2"
        out1 = q.get_nowait()
        out2 = q.get_nowait()
        assert out1 == item1  # the non-latest item is untouched
        assert out2["prompt"] == "altered text"
        assert out2["cli_prompt"] == "altered-cli"
        assert out2["client_id"] == "new-cid"
        assert out2["lifecycle_msg_id"] == "lifecycle-1"
        assert out2["capability_contexts"] == [{"k": "v"}]
    asyncio.run(_run())


def test_alter_resend_empty_cli_content_falls_back_to_new_prompt_content() -> None:
    """Preserves `update_latest_queued`'s original `cli_content or content`
    semantics (an empty-string `cli_content` is treated as absent, exactly
    like the pre-consolidation implementation)."""
    async def _run() -> None:
        coord = _bare_coordinator()
        item = {"_queued_id": "q1", "prompt": "old"}
        q = _seed_queue(coord, "sid-1", [item])
        await coord.update_latest_queued("sid-1", "altered", "", "cid", "lc-1")
        out = q.get_nowait()
        assert out["cli_prompt"] == "altered"
    asyncio.run(_run())


def test_alter_resend_on_empty_queue_returns_none() -> None:
    async def _run() -> None:
        coord = _bare_coordinator()
        coord._prompt_queues["sid-1"] = asyncio.Queue()
        result = await coord.update_latest_queued("sid-1", "x", None, None, "lc-1")
        assert result is None
    asyncio.run(_run())


def test_alter_resend_skips_none_placeholder_items_when_finding_latest() -> None:
    """`_prompt_queues` items can be `None` (a `cancel_session` sentinel,
    per `_run_session_processor`'s docstring) — `update_latest_queued`
    must find the latest REAL item, not treat a trailing `None` as it."""
    async def _run() -> None:
        coord = _bare_coordinator()
        item = {"_queued_id": "q1", "prompt": "first"}
        q = _seed_queue(coord, "sid-1", [item, None])
        queued_id = await coord.update_latest_queued("sid-1", "altered", None, "cid", "lc-1")
        assert queued_id == "q1"
        out1 = q.get_nowait()
        out2 = q.get_nowait()
        assert out1["prompt"] == "altered"
        assert out2 is None
    asyncio.run(_run())


_TESTS = [
    test_edit_queued_style_call_overwrites_prompt_and_set_cli_prompt,
    test_edit_queued_style_call_leaves_absent_cli_prompt_absent,
    test_edit_queued_style_call_on_unknown_queued_id_reports_not_updated,
    test_alter_resend_on_queued_prompt_routes_through_update_queued,
    test_alter_resend_empty_cli_content_falls_back_to_new_prompt_content,
    test_alter_resend_on_empty_queue_returns_none,
    test_alter_resend_skips_none_placeholder_items_when_finding_latest,
]


def _run_standalone() -> int:
    failures = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if _run_standalone() else 0)
