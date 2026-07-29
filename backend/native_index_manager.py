"""Owning entity for the native-transcript domain.

Two subsystems live behind this manager: the FTS index over native CLI
transcripts (`native_transcript_index`) and the importer that turns
native CLI sessions into Better Agent sessions (`native_import`). Both
are blocking, both are slow, and neither belongs on the request loop.

The manager owns their lifecycle and is their single off-loop execution
home. Routes await it; they never touch either module directly, and they
never decide where the work runs.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from thread_loop_host import ThreadLoopHost

logger = logging.getLogger(__name__)


def _index_disabled() -> bool:
    """Test backends never scan the developer's real ~/.claude / ~/.codex."""
    return bool(os.environ.get("BETTER_AGENT_TEST_MODE"))


class NativeIndexManager(ThreadLoopHost):
    """Runs native index + import work on a thread of its own."""

    def __init__(self) -> None:
        super().__init__(name="native-index", executor_workers=2)

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Bring up the thread and spawn the index daemon on it.

        The daemon spawn touches disk and forks a subprocess, so it runs
        on this manager's own executor rather than the caller's loop —
        `on_startup` must return within milliseconds.
        """
        super().start()
        loop = self.loop
        if loop is None:
            return
        loop.call_soon_threadsafe(
            lambda: loop.run_in_executor(None, self._start_index_worker),
        )

    def _start_index_worker(self) -> None:
        """Spawn the daemon that keeps the FTS index covered + fresh.

        Skipped in test mode so test backends never scan the developer's
        real ~/.claude / ~/.codex corpus.
        """
        if _index_disabled():
            return
        try:
            import native_transcript_index
            native_transcript_index.ensure_started()
        except Exception:
            logger.debug("native transcript index worker failed to start", exc_info=True)

    def on_stopping(self) -> None:
        """Tear the index daemon down.

        Gated on the same condition as startup rather than on a
        "did it start" flag: the spawn is scheduled asynchronously, so a
        flag can still be unset while the worker is already coming up.
        `shutdown()` is idempotent, so an unnecessary call is harmless
        while a skipped one would leak the worker subprocess.
        """
        if _index_disabled():
            return
        try:
            import native_transcript_index
            native_transcript_index.shutdown()
        except Exception:
            logger.exception("native transcript index shutdown failed")

    # -- import work ----------------------------------------------------

    async def start_import(
        self,
        provider_ids: Optional[list[str]],
        limit: Optional[int],
        project_paths: Optional[list[str]],
    ) -> dict:
        """Start the single-flight import job and return its status.

        Runs inside the backend process on purpose: a separate process
        writing session.json races the backend's in-memory cache and
        clobbers the render tree.
        """
        def _work():
            import native_import
            return native_import.start_import(provider_ids, limit, project_paths)

        return await self.run_blocking(_work)

    async def import_status(self) -> dict:
        def _work():
            import native_import
            return native_import.get_status()

        return await self.run_blocking(_work)

    async def import_summary(
        self,
        provider_ids: Optional[list[str]],
        all_projects: bool,
    ) -> dict:
        """Counts-only preview of importable native sessions, grouped by
        provider. Read-only; does not start a job. Grouped rather than
        one row per session — a full Claude+Codex history is tens of
        thousands of sessions and the row dump reached hundreds of MB.
        """
        import native_import
        paths = None if all_projects else await self.loaded_project_paths()
        return await self.run(
            lambda: native_import.count_native_sessions_async(provider_ids, paths),
        )

    async def resume_interrupted_import(self) -> None:
        """Resume an import that a restart interrupted.

        The idempotency registry makes resume duplicate-free. Best-effort:
        a failed resume must never block startup.
        """
        def _work():
            import native_import
            native_import.resume_if_interrupted()

        try:
            await self.run_blocking(_work)
        except Exception:
            logger.exception("native_import: resume-on-startup failed")

    async def loaded_project_paths(self) -> list[str]:
        def _work():
            import native_import
            return native_import.loaded_project_paths()

        return await self.run_blocking(_work)


manager = NativeIndexManager()
