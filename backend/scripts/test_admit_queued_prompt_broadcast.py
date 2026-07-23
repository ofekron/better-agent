"""Regression: admit_queued_prompt must broadcast the populated queue.

Bug: a prompt queued on one client (mobile) wasn't visible on another
connected client (desktop) until a manual refresh. Root cause:
`SessionManager.admit_queued_prompt` fired `{"kind": "queued_prompts_updated"}`
without a `queued_prompts` key, unlike its sibling mutators
(`update_queued_prompt`/`remove_queued_prompt`), which enrich the change dict
via `_queue_projection_enricher`. `SessionWSBroadcaster.on_change` builds the
broadcast patch as `list(change.get("queued_prompts") or [])`, so the missing
key collapsed to an empty list and other clients never learned about the new
queued prompt over WS.

Run with:
    cd backend && .venv/bin/python scripts/test_admit_queued_prompt_broadcast.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _test_home
_TMP = _test_home.isolate("bc_admit_queued_")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"


def _check(cond: bool, label: str, failures: list[str]) -> None:
    print(f"  {'OK' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


async def _run(failures: list[str]) -> None:
    import main

    sm = main.session_manager
    sm.bind_loop(asyncio.get_running_loop())

    hits: list[tuple[str, dict]] = []
    sm.add_listener(lambda sid, change: hits.append((sid, change)))

    sess = sm.create(name="admit-queued-test", cwd=_TMP, orchestration_mode="native")
    sid = sess["id"]

    sm.admit_queued_prompt(
        sid, {"id": "p1", "text": "hello", "client_id": "mobile-1"},
    )

    queue_hits = [c for s, c in hits if s == sid and c.get("kind") == "queued_prompts_updated"]
    _check(
        len(queue_hits) >= 1,
        f"queued_prompts_updated change fired (got {len(queue_hits)})",
        failures,
    )
    if queue_hits:
        broadcast_queue = queue_hits[-1].get("queued_prompts")
        _check(
            broadcast_queue is not None,
            "change dict includes a 'queued_prompts' key (was missing pre-fix)",
            failures,
        )
        ids = [p.get("id") for p in (broadcast_queue or [])]
        _check(
            ids == ["p1"],
            f"broadcast queued_prompts contains the newly admitted prompt (got {ids})",
            failures,
        )

    sm.delete(sid)


def main_entry() -> int:
    failures: list[str] = []
    try:
        asyncio.run(_run(failures))
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} FAILURES")
        return 1
    print("\nadmit_queued_prompt broadcast check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())
