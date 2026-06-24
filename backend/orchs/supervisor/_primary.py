"""Run one primary turn on a session (native or manager dispatch).

Lives apart from `__init__.py` so `_verdict.request_review` can call it
without re-introducing the supervisor↔_verdict lazy-import cycle:
both files import `run_primary_turn` from here top-level.

INVARIANT: this is the SINGLE source for dispatching a primary turn
under either orchestration mode. The verdict loop and `request_review`
both go through it; per-mode entry points in `orchs.manager` /
`orchs.native` use `coordinator.run_turn` directly (they need
fork-first-turn / user_initiated knobs this wrapper deliberately
hides — those are user-prompt-path concerns, not supervisor-internal).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from session_manager import manager as session_manager

if TYPE_CHECKING:
    from orchestrator import Coordinator

logger = logging.getLogger(__name__)


async def run_primary_turn(
    coordinator: "Coordinator",
    *,
    app_session_id: str,
    prompt: str,
    ws_callback: Callable[[dict], Awaitable[None]],
    images: Optional[list] = None,
    files: Optional[list] = None,
    source: Optional[str] = None,
) -> None:
    """Run one primary turn (native or manager) on the given session.

    Used by:
      - the verdict loop, to feed CONTINUE/FIX instructions back as
        another primary iteration.
      - request_review's handoff, to run the supervisor's review text
        as a primary turn so the agent acts on the review.

    Dispatches by the session's orchestration_mode. Manager mode wraps
    the prompt via ``manager.bootstrap.build_wrapped_prompt``; native
    mode passes the prompt through raw.
    """
    session = session_manager.get(app_session_id)
    if not session:
        logger.warning("run_primary_turn: missing session %s", app_session_id)
        return
    mode = session.get("orchestration_mode") or "native"

    # Single dispatch: the strategy supplies session_id_field / mode /
    # trace_step_name / prompt-wrapping. No `user_initiated` → defaults
    # False, so supervisor re-entries never gain the open_file_panel
    # whitelist (locked by test_open_file_panels.py).
    from orchs import get_strategy
    await get_strategy(mode).run_primary(
        coordinator,
        session=session,
        prompt=prompt,
        app_session_id=app_session_id,
        model=session.get("model") or "",
        cwd=session.get("cwd") or "",
        ws_callback=ws_callback,
        images=images,
        files=files,
        source=source,
    )
