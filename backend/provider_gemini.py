"""GeminiProvider — `Provider` implementation for Google's Gemini CLI.

Spawns `runner_gemini.py` as a detached subprocess per run. The runner
captures Gemini's `stream-json` output, normalizes to Claude jsonl shape,
and writes to `session_events.jsonl`. The provider tails that file and
pushes events onto the orchestrator queue.

Gemini CLI subscription auth is no longer supported for individual,
Google AI Pro, or Google AI Ultra accounts. Existing Gemini subscription
records fail closed with a clear error until Antigravity/API-key support
is implemented.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar, Optional

from provider import (
    Provider,
    RecoveredPopen,
    StreamEvent,
    await_line_tailer_drained,
    build_better_agent_run_env,
    path_exists_off_loop,
    popen_is_running_off_loop,
    schedule_loop_task,
    runner_argv,
)
from provider_run_config import normalize_provider_run_config
from cli_paths import resolve_cli_binary
from ingestion_versions import marker_matches_current
from proc_control import process_control as _process_control
import config_store
from extension_run_policy import (
    disabled_builtin_extensions_for_run,
    disabled_builtin_tools_for_run,
    disabled_runtime_skills_for_run,
)
from config_store import GEMINI_SUBSCRIPTION_UNSUPPORTED
from runs_dir import (
    atomic_write_json as _atomic_write_json,
    iter_run_dirs,
    pid_alive as _pid_alive,
    prune_old_completed_runs,
    reap_run_dir as _reap_run_dir,
    runs_root as _runs_root,
)

logger = logging.getLogger(__name__)


_RUNNER_PATH = Path(__file__).parent / "runner_gemini.py"
_TAIL_POLL_INTERVAL = 0.05
_RUNNER_EVENT_TYPES = {"agent_message", "worker_start", "worker_event", "worker_complete"}


def runner_event_to_stream_event(event: dict) -> StreamEvent:
    event_type = event.get("type")
    event_data = event.get("data")
    if event_type in _RUNNER_EVENT_TYPES and isinstance(event_data, dict):
        return StreamEvent(event_type, event_data)
    return StreamEvent("agent_message", event)

# Models the `gemini` CLI accepts for `-m`, newest first. Live-probed
# against the user's subscription on 2026-05-22 — every entry returned
# a successful turn. Cross-referenced with gemini-cli 0.42's bundled
# `VALID_GEMINI_MODELS` set and ai.google.dev/gemini-api/docs/models.
#
# Excluded for cause:
#   gemini-3-pro-preview — Google deprecated and shut down 2026-03-09;
#     still answers via a silent server-side redirect to 3.1 but Google
#     can drop the alias anytime. Use gemini-3.1-pro-preview instead.
#   gemini-3.1-flash-lite-preview — superseded by the now-stable
#     `gemini-3.1-flash-lite` (no -preview suffix).
#   gemini-3-pro / gemini-3.1-pro / gemini-3.5-flash — return 404, not
#     real model IDs despite some docs/marketing pages listing them.
#   gemma-* / specialised customtools / tts / live audio / embedding /
#     computer-use / deep-research — not chat-coding models.
#
# Single source of truth: `models.py` imports this rather than keeping
# its own copy. Re-probe whenever the CLI bundle bumps (preview models
# graduate to stable IDs without -preview suffix periodically).
GEMINI_MODELS = [
    "auto-gemini-3",          # CLI auto-router within gemini-3 family
    "gemini-3.1-pro-preview", # current top preview pro
    "gemini-3-flash-preview", # preview flash
    "gemini-3.1-flash-lite",  # STABLE/GA (graduated out of preview)
    "auto-gemini-2.5",        # CLI auto-router within gemini-2.5 family
    "gemini-2.5-pro",         # CLI DEFAULT_GEMINI_MODEL — stable
    "gemini-2.5-flash",       # CLI DEFAULT_GEMINI_FLASH_MODEL — stable
    "gemini-2.5-flash-lite",  # CLI DEFAULT_GEMINI_FLASH_LITE_MODEL — stable
]


# --------------------------------------------------------------------
# Bundle scraper — daily refresh path for the catalog
# --------------------------------------------------------------------
#
# Google's APIs DO NOT expose a usable model-list endpoint for the
# consumer Gemini CLI (generativelanguage scope=generative-language.
# retriever required; cloudcode-pa has no listModels; Vertex publisher
# endpoint is not REST-listable). The CLI ships the authoritative list
# as a hardcoded `VALID_GEMINI_MODELS` Set inside its bundled JS.
#
# We parse the installed CLI bundle (resilient across CLI upgrades:
# filename is `chunk-*.js`, the `var X_MODEL = "..."` constants are
# stable, the Set name is stable). Non-chat families are excluded —
# the curated GEMINI_MODELS list above already documented them.

import re as _re

_GEMINI_EXCLUDE_PATTERNS = [
    _re.compile(p) for p in [
        r"^gemma-",
        r"embedding",
        r"customtools",
        r"computer-use",
        r"-tts(\b|$)",
        r"-live-",
    ]
]


def _resolve_gemini_bundle_dir() -> Optional[Path]:
    """Locate the installed gemini CLI's bundle/ dir. Returns None if
    the CLI is not on PATH or no chunk-*.js files can be found.

    Strategy: resolve symlink chain (Homebrew, nvm, asdf, mise),
    then check the entry's parent for chunk-*.js. If absent (entry is
    a launcher / dist/index.js that doesn't sit in the bundle dir),
    walk up looking for a `bundle/` subdir with chunks."""
    gemini = resolve_cli_binary("gemini")
    if not gemini:
        return None
    real = Path(gemini).resolve()
    # Fast path: entry is `bundle/gemini.js` (Homebrew layout). Verify
    # the parent actually has chunks — a launcher like `dist/index.js`
    # has the same suffix but lives in the wrong directory; fall through
    # to walk-up in that case rather than returning the wrong dir.
    if real.suffix == ".js" and any(real.parent.glob("chunk-*.js")):
        return real.parent
    cur = real.parent
    for _ in range(6):
        cand = cur / "bundle"
        if cand.is_dir() and any(cand.glob("chunk-*.js")):
            return cand
        if any(cur.glob("chunk-*.js")):
            return cur
        cur = cur.parent
    return None


def fetch_gemini_models() -> list[str]:
    """Scrape the installed gemini CLI bundle's `VALID_GEMINI_MODELS`
    Set and return the chat-only literal ids.

    Returns `[]` on:
    - CLI not installed
    - bundle dir not located
    - parse failed (no Set block / no var-MODEL constants found)
    - integrity check: post-filter list has fewer than 3 models
      (caller treats as failure; keeps prior cache)
    """
    bundle = _resolve_gemini_bundle_dir()
    if bundle is None:
        logger.warning("fetch_gemini_models: gemini CLI bundle not found")
        return []

    # Aggregate all chunk-*.js content. Bundle is ~few MB; one-shot read
    # is fine (we already shell out to httpx in Claude's path).
    text_parts: list[str] = []
    for chunk in bundle.glob("chunk-*.js"):
        try:
            text_parts.append(chunk.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    text = "\n".join(text_parts)

    # Strip JS comments BEFORE anything else: var_to_literal must not
    # capture commented-out constants (a coincidental identifier in
    # the Set body would resolve to the commented value), and the Set
    # body bracket-counter must not be confused by `]` inside comments.
    text = _re.sub(r"/\*.*?\*/", " ", text, flags=_re.DOTALL)
    text = _re.sub(r"//[^\n]*", " ", text)

    # Step 1: var X_MODEL = "literal" → dict
    var_to_literal: dict[str, str] = {}
    for m in _re.finditer(r'var ([A-Z_0-9]+_MODEL)\s*=\s*"([^"]+)"', text):
        var_to_literal[m.group(1)] = m.group(2)
    # Also pick up VISUAL_AGENT_MODEL etc. (non-_MODEL suffix). Pattern
    # is permissive but won't pollute the Set lookup which is opt-in.
    for m in _re.finditer(r'var ([A-Z][A-Z_0-9]*)\s*=\s*"([^"]+)"', text):
        var_to_literal.setdefault(m.group(1), m.group(2))

    # Step 2: locate the VALID_GEMINI_MODELS Set body via bracket
    # counting (not a `\[([^\]]*)\]` regex). Robust against a future
    # bundle change that inlines string literals (containing `]`) or
    # nests array literals inside the Set body — both would silently
    # truncate the regex's match.
    set_head = _re.search(
        r"VALID_GEMINI_MODELS\s*=\s*new\s+Set\(\s*\[",
        text,
    )
    if not set_head:
        logger.warning("fetch_gemini_models: VALID_GEMINI_MODELS Set not located")
        return []

    bracket_start = set_head.end() - 1  # position of `[`
    depth = 0
    bracket_end = -1
    for i in range(bracket_start, len(text)):
        c = text[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                bracket_end = i
                break
    if bracket_end < 0:
        logger.warning(
            "fetch_gemini_models: VALID_GEMINI_MODELS Set never closes",
        )
        return []

    body = text[bracket_start + 1:bracket_end]
    # Step 3: extract identifiers from the Set body, look up literals
    raw_ids: list[str] = []
    seen: set[str] = set()
    for tok in _re.findall(r"[A-Z][A-Z_0-9]*", body):
        lit = var_to_literal.get(tok)
        if not lit or lit in seen:
            continue
        seen.add(lit)
        raw_ids.append(lit)

    # Step 4: exclude non-chat families
    filtered = [
        m for m in raw_ids
        if not any(p.search(m) for p in _GEMINI_EXCLUDE_PATTERNS)
    ]

    # Integrity check — guard against future bundle reshape silently
    # nuking the catalog.
    if len(filtered) < 3:
        logger.warning(
            "fetch_gemini_models: post-filter list has %d entries "
            "(raw=%d) — treating as parse failure",
            len(filtered), len(raw_ids),
        )
        return []

    return filtered


# ============================================================================
# RunState — per-run bookkeeping (mirrors ClaudeProvider.RunState)
# ============================================================================
@dataclass
class RunState:
    run_id: str
    run_dir: Path
    popen: subprocess.Popen
    mode: str
    app_session_id: str
    queue: asyncio.Queue
    session_id: Optional[str] = None
    processed_line: int = 0
    tailer: Optional["object"] = None  # GeminiJsonlTailer; typed loosely to avoid import cycle
    tailer_task: Optional[asyncio.Task] = None
    complete_task: Optional[asyncio.Task] = None
    started_at: str = ""
    cancelled: bool = False
    # Where this run's messages PERSIST. In supervisor mode, a worker
    # turn's events route to the worker Better Agent session even though the run
    # is bookkept under the supervisor's app_session_id. Mirrors
    # ClaudeProvider.RunState.persist_to.
    persist_to: str = ""
    target_message_id: Optional[str] = None
    turn_run_id: Optional[str] = None


# ============================================================================
# GeminiProvider
# ============================================================================
class GeminiProvider(Provider):
    """Drives Google's `gemini` CLI via detached `runner_gemini.py`
    subprocesses. Events are read from the runner's
    `session_events.jsonl` and pushed onto the orchestrator queue."""

    KIND: ClassVar[str] = "gemini"

    # gemini-cli 0.42 has no non-interactive fork primitive
    # (issue google-gemini/gemini-cli#22563). Every fork-using feature
    # (fork-and-send, adversarial sync, prompt-engineer refine,
    # manager-mode delegate-fork) must read this flag and
    # disable itself for gemini sessions.
    supports_fork: ClassVar[bool] = False
    # Gemini uses provider-native MCP/settings files, not the in-process
    # SDK MCP registration path that Claude uses for manager mode.
    supports_manager_mode: ClassVar[bool] = False
    # gemini-cli has no rewind primitive, but we can simulate it by
    # starting a fresh turn without --resume if the user wants to
    # abandon a stuck/broken session.
    supports_rewind: ClassVar[bool] = True
    rewind_requires_agent_identity: ClassVar[bool] = False

    def __init__(self, record: dict) -> None:
        super().__init__(record)
        self._runs: dict[str, RunState] = {}

    # ------------------------------------------------------------------
    # Env — minimal for Gemini (subscription mode, no API keys)
    # ------------------------------------------------------------------
    def build_env(self) -> dict[str, str]:
        self.require_runtime_credential()
        env = os.environ.copy()
        # Gemini CLI uses ~/.gemini by default — nothing to configure.
        # Clear any Claude-specific env so they don't interfere.
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", None)
        return self.finalize_env(env)

    # ------------------------------------------------------------------
    # start_run
    # ------------------------------------------------------------------
    def start_run(
        self,
        *,
        run_id: str,
        prompt: str,
        images: Optional[list] = None,
        files: Optional[list] = None,
        cwd: str,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
        model: Optional[str],
        reasoning_effort: Optional[str],
        session_id: Optional[str],
        mode: str,
        app_session_id: str,
        source: Optional[str] = None,
        disallowed_tools: Optional[list[str]] = None,
        setting_sources: Optional[list[str]] = None,
        backend_url: Optional[str] = None,
        internal_token: Optional[str] = None,
        fork: bool = False,
        supervised: bool = False,
        supervisor_agent_session_id: Optional[str] = None,
        worker_agent_session_id: Optional[str] = None,
        mssg_sender_session_id: Optional[str] = None,
        is_worker: bool = False,
        browser_harness_enabled: bool = False,
        open_file_panel_enabled: bool = False,
        working_mode: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
        continuation_chain: Optional[list[str]] = None,
        provider_run_config: Optional[dict] = None,
        capability_contexts: Optional[list[dict]] = None,
        target_message_id: Optional[str] = None,
        resolved_harness_run_config: Optional[dict] = None,
        turn_run_id: Optional[str] = None,
        disabled_builtin_extensions: Optional[list[str]] = None,
        provisioned_tool_profile: str = "",
    ) -> None:
        if mode == "manager":
            mode = "team"
        if mode not in ("native", "team"):
            raise ValueError(f"mode must be 'native' or 'team', got {mode!r}")
        if self.defunct:
            raise RuntimeError(
                f"provider {self.id} is defunct; cannot start new runs"
            )
        self.assert_not_suspended(action="start new runs")
        if self.record.get("mode", "subscription") == "subscription":
            raise RuntimeError(GEMINI_SUBSCRIPTION_UNSUPPORTED)
        if reasoning_effort:
            raise NotImplementedError(
                f"{self.KIND} provider does not support reasoning effort."
            )

        available = self.available_models()
        if model and model not in available:
            raise ValueError(
                f"model {model!r} is not available for the Gemini provider. "
                f"Available: {', '.join(available)}. "
                f"This session's model was likely set while a different "
                f"provider was active."
            )
        if mode == "team" and not self.supports_manager_mode:
            raise NotImplementedError(
                f"{self.KIND} provider does not support team mode."
            )
        # `fork` is gated by the class-level capability
        # `supports_fork=False`. Backend callers (session_manager.fork,
        # adv_sync, prompt-engineer-refine) should check
        # the capability and skip; if one of them still passes
        # fork=True we fail loudly here as the last line of defence.
        # `supervised` is allowed — claude's supervisor isn't a CLI
        # hook either, it's a backend verdict-loop (orchs/supervisor/)
        # which is provider-agnostic. Setting persist_to on the
        # RunState below routes events to the worker BC the same way
        # claude does.
        if fork and not self.supports_fork:
            raise NotImplementedError(
                f"{self.KIND} provider does not support fork."
            )

        run_dir = _runs_root() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        runner_mode = "manager" if mode == "team" else mode
        from session_manager import manager as _sm
        import user_prefs
        _sess_rec = _sm.get(app_session_id) or {}
        _worker_sess_rec = _sm.get(worker_agent_session_id) if worker_agent_session_id else {}
        from permission import resolve_for_run as _resolve_perm
        _permission = _resolve_perm(
            sess_rec=_sess_rec,
            worker_sess_rec=_worker_sess_rec,
            is_worker=is_worker,
            fallback_kind=self.KIND,
        )
        _bare = bool(_sess_rec.get("bare_config")) or bool(
            (resolved_harness_run_config or {}).get("bare_config")
        )
        input_payload = {
            "prompt": prompt,
            "images": images or [],
            "files": files or [],
            "cwd": cwd,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "permission": _permission,
            "session_id": session_id,
            "mode": runner_mode,
            "source": source or "",
            "app_session_id": app_session_id,
            "active_capability_ids": [
                str(cid)
                for cid in (_sess_rec.get("active_capability_ids") or [])
                if str(cid or "").strip()
            ],
            "disallowed_tools": disallowed_tools or [],
            "setting_sources": setting_sources or [],
            "backend_url": backend_url or "",
            "internal_token": "",
            "provider_id": self.id,
            "fork": bool(fork),
            "supervised": bool(supervised),
            "supervisor_agent_session_id": supervisor_agent_session_id,
            "worker_agent_session_id": worker_agent_session_id,
            "mssg_sender_session_id": mssg_sender_session_id,
            "browser_harness_enabled": bool(browser_harness_enabled),
            "open_file_panel_enabled": bool(open_file_panel_enabled),
            "bare_config": _bare,
            "working_mode": _sess_rec.get("working_mode"),
            "worker_working_mode": (_worker_sess_rec or {}).get("working_mode"),
            "context_strategy": user_prefs.get_context_strategy(),
            "continuation_chain": continuation_chain or [],
            "provider_run_config": normalize_provider_run_config(provider_run_config),
            "capability_contexts": capability_contexts or [],
            "resolved_harness_run_config": resolved_harness_run_config or {},
            "target_message_id": target_message_id,
            "turn_run_id": turn_run_id,
            "provisioned_tool_profile": str(provisioned_tool_profile or "").strip(),
            "disabled_builtin_tools": disabled_builtin_tools_for_run(
                session_record=_sess_rec, worker_record=_worker_sess_rec,
            ),
            "disabled_runtime_skills": disabled_runtime_skills_for_run(
                session_record=_sess_rec, worker_record=_worker_sess_rec,
            ),
            "disabled_builtin_extensions": (
                disabled_builtin_extensions_for_run(
                    disabled_builtin_extensions,
                    session_record=_sess_rec,
                    worker_record=_worker_sess_rec,
                )
            ),
        }
        (run_dir / "input.json").write_text(json.dumps(input_payload), encoding="utf-8")

        from containment import containment
        containment().create(run_id)
        stdout_fp = (run_dir / "stdout.log").open("ab")
        stderr_fp = (run_dir / "stderr.log").open("ab")
        try:
            env = self.build_env()
            if extra_env:
                env.update(extra_env)
            env.update(build_better_agent_run_env(
                backend_url=backend_url,
                internal_token=internal_token,
                run_id=run_id,
                app_session_id=app_session_id,
                cwd=cwd,
                model=model,
                provider_id=self.id,
                bare_config=_bare,
                user_facing=bool(open_file_panel_enabled) and not _bare,
                disabled_builtin_extensions=input_payload["disabled_builtin_extensions"],
            ))
            popen = subprocess.Popen(
                runner_argv(run_dir, dev_script=_RUNNER_PATH, kind="gemini"),
                stdin=subprocess.DEVNULL,
                stdout=stdout_fp,
                stderr=stderr_fp,
                cwd=cwd,
                env=env,
                **_process_control().detach_spawn_kwargs(),
                **containment().spawn_kwargs(run_id),
            )
        except Exception:
            stdout_fp.close()
            stderr_fp.close()
            containment().teardown(run_id)
            raise
        finally:
            stdout_fp.close()
            stderr_fp.close()
        containment().after_spawn(run_id, popen.pid)

        logger.info(
            "spawned gemini runner pid=%d mode=%s run_id=%s",
            popen.pid, mode, run_id,
        )

        rs = RunState(
            run_id=run_id,
            run_dir=run_dir,
            popen=popen,
            mode=mode,
            app_session_id=app_session_id,
            queue=queue,
            started_at=datetime.now().isoformat(),
            # In supervisor mode, worker turns persist to the worker BC,
            # not the supervisor's app_session_id. Mirrors ClaudeProvider.
            persist_to=worker_agent_session_id or app_session_id,
            target_message_id=target_message_id,
            turn_run_id=turn_run_id,
        )
        self._runs[run_id] = rs
        self._write_backend_state(rs)

        schedule_loop_task(
            loop,
            self._bootstrap_run(rs),
            name=f"gemini-bootstrap-{run_id[:8]}",
        )

    # ------------------------------------------------------------------
    # _bootstrap_run — wait for state.json, then tail session_events.jsonl
    # ------------------------------------------------------------------
    async def _bootstrap_run(self, rs: RunState) -> None:
        state_path = rs.run_dir / "state.json"
        complete_path = rs.run_dir / "complete.json"
        events_path = rs.run_dir / "session_events.jsonl"

        # 1) Poll for state.json
        runner_state: Optional[dict] = None
        while True:
            if await path_exists_off_loop(state_path):
                try:
                    parsed = json.loads(state_path.read_text(encoding="utf-8"))
                    if parsed.get("session_id"):
                        runner_state = parsed
                        break
                except (json.JSONDecodeError, OSError):
                    pass

            # Runner is dead — enter regardless of state.json existing.
            # state.json with null session_id + dead runner is a pre-run
            # failure (e.g. invalid --resume target); the old
            # `and not state_path.exists()` gate would spin forever.
            if not await popen_is_running_off_loop(rs.popen):
                if await path_exists_off_loop(complete_path):
                    break
                await self._emit_early_failure(
                    rs, f"runner exited early with code {rs.popen.returncode}"
                )
                return
            await asyncio.sleep(_TAIL_POLL_INTERVAL)

        if runner_state is None:
            await self._emit_complete_from_file(rs, complete_path)
            self._cleanup_run(rs.run_id)
            return

        session_id = runner_state["session_id"]
        rs.session_id = session_id
        # Persist the discovered sid into backend_state.json NOW so a
        # crash between session_discovered and the first tailer cursor
        # advance still surfaces the sid to run_recovery on restart.
        self._write_backend_state(rs)

        # 2) Emit session_discovered
        try:
            rs.queue.put_nowait(StreamEvent("session_discovered", {"session_id": session_id}))
        except Exception:
            logger.exception("failed to enqueue session_discovered")

        # 3) Start the polling tailer on session_events.jsonl. Same
        # JsonlEventTailer base as ClaudeJsonlTailer; the concrete
        # `_open_source` / `_next_line` differ (polling read vs tail -F)
        # but cancel + dispatch + cursor are shared.
        from jsonl_tailer import GeminiJsonlTailer

        def _dispatch_to_queue(event: dict, _rs: RunState = rs) -> None:
            try:
                _rs.queue.put_nowait(runner_event_to_stream_event(event))
            except Exception:
                logger.exception(
                    "GeminiJsonlTailer dispatch: put_nowait failed for run %s",
                    _rs.run_id,
                )

        def _on_cursor(n: int, _rs: RunState = rs) -> None:
            # Mirror ClaudeProvider._on_tailer_progress: called
            # synchronously from the tailer's read loop, so this MUST
            # stay non-blocking. In-memory state updates immediately
            # (cheap; this is what the deterministic drain polls); the
            # actual `backend_state.json` write hands off to
            # `cursor_ledger_worker`, off this call path entirely.
            _rs.processed_line = n
            from cursor_ledger_worker import worker as cursor_ledger_worker
            cursor_ledger_worker.note(_rs.run_id, lambda: self._write_backend_state(_rs))

        rs.tailer = GeminiJsonlTailer(
            path=events_path,
            start_offset=rs.processed_line,
            dispatch=_dispatch_to_queue,
            on_cursor_advance=_on_cursor,
        )
        rs.tailer_task = asyncio.get_event_loop().create_task(
            rs.tailer.run(),
            name=f"gemini-tailer-{rs.run_id[:8]}",
        )

        # 4) Schedule completion watcher
        rs.complete_task = asyncio.get_event_loop().create_task(
            self._watch_complete(rs),
            name=f"gemini-complete-{rs.run_id[:8]}",
        )

    # ------------------------------------------------------------------
    # _watch_complete
    # ------------------------------------------------------------------
    async def _watch_complete(self, rs: RunState) -> None:
        complete_path = rs.run_dir / "complete.json"
        try:
            while True:
                if await path_exists_off_loop(complete_path):
                    break
                # INVARIANT: process death MUST end this loop. If the
                # runner is SIGKILLed (OOM, manual kill, OS) it never
                # writes complete.json — the old "complete.json AND
                # process dead" condition would spin forever, leaving
                # the turn stuck in flight forever. Breaking on
                # process-dead alone lets `_emit_complete_from_file`'s
                # built-in fallback (`error="runner exited without
                # writing complete.json"`) synthesize the error
                # complete event. A short grace window lets a normal
                # exit's complete.json land before we synthesize.
                if not await popen_is_running_off_loop(rs.popen):
                    loop = asyncio.get_event_loop()
                    grace_end = loop.time() + (_TAIL_POLL_INTERVAL * 6)
                    while (
                        not await path_exists_off_loop(complete_path)
                        and loop.time() < grace_end
                    ):
                        await asyncio.sleep(_TAIL_POLL_INTERVAL)
                    break
                await asyncio.sleep(_TAIL_POLL_INTERVAL)

            # Deterministic drain: the runner appends every event line
            # BEFORE writing complete.json, so wait until the tailer's
            # line cursor covers the file as it stands now. A fixed
            # sleep guess let `complete` overtake trailing lines when
            # the poll tailer lagged — the turn loop then broke and the
            # lines never reached the render tree (stale-content grabs).
            await await_line_tailer_drained(
                path=rs.run_dir / "session_events.jsonl",
                get_cursor=lambda: rs.processed_line,
                run_id=rs.run_id,
                on_drained=lambda: self._flush_cursor_ledger(rs),
            )
            if rs.tailer is not None:
                rs.tailer.stop()
            if rs.tailer_task is not None:
                try:
                    await asyncio.wait_for(rs.tailer_task, timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "gemini tailer did not exit in time for %s", rs.run_id,
                    )
                except Exception:
                    logger.exception(
                        "gemini tailer task failed for %s", rs.run_id,
                    )
            await self._emit_complete_from_file(rs, complete_path)
        finally:
            self._cleanup_run(rs.run_id)

    # ------------------------------------------------------------------
    # _emit_complete_from_file
    # ------------------------------------------------------------------
    async def _emit_complete_from_file(self, rs: RunState, complete_path: Path) -> None:
        payload: dict[str, Any] = {
            "success": False,
            "error": "runner exited without writing complete.json",
            "session_id": rs.session_id,
            "token_usage": None,
        }
        if complete_path.exists():
            try:
                payload = json.loads(complete_path.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("failed to parse complete.json for %s", rs.run_id)
        try:
            rs.queue.put_nowait(StreamEvent("complete", payload))
        except Exception:
            logger.exception("failed to enqueue complete for %s", rs.run_id)

    # ------------------------------------------------------------------
    # _emit_early_failure
    # ------------------------------------------------------------------
    async def _emit_early_failure(self, rs: RunState, msg: str) -> None:
        logger.warning("gemini bootstrap failure for %s: %s", rs.run_id, msg)
        try:
            rs.queue.put_nowait(StreamEvent("error", {"error": msg}))
            rs.queue.put_nowait(StreamEvent("complete", {
                "success": False, "error": msg,
                "session_id": None, "token_usage": None,
            }))
        except Exception:
            logger.exception("failed to enqueue early failure for %s", rs.run_id)
        self._cleanup_run(rs.run_id)

    # _backend_state_path / _read_backend_state inherited from
    # AbstractStreamingProvider. is_running / cancel_all / active_runs /
    # runs_for_session / _cleanup_run / cancel_run all inherited.

    def _write_backend_state(self, rs: RunState) -> None:
        """Provider-specific backend_state.json contents.
        Mirrors `ClaudeProvider._write_backend_state` (run_id /
        app_session_id / mode / runner_pid / started_at / session_id /
        processed_line / cancelled / provider_id / persist_to /
        jsonl_path) so `run_recovery._integrate_one` reads the same
        keys regardless of provider kind."""
        data = {
            "run_id": rs.run_id,
            "app_session_id": rs.app_session_id,
            "persist_to": rs.persist_to or rs.app_session_id,
            "mode": rs.mode,
            "runner_pid": rs.popen.pid,
            "started_at": rs.started_at,
            "session_id": rs.session_id,
            "jsonl_path": str(rs.run_dir / "session_events.jsonl"),
            "processed_line": rs.processed_line,
            "cancelled": rs.cancelled,
            "target_message_id": rs.target_message_id,
            "turn_run_id": rs.turn_run_id,
            "provider_id": self.id,
            "runner": self.record.get("runner"),
        }
        try:
            _atomic_write_json(self._backend_state_path(rs), data)
            if rs.session_id:
                import spawn_ledger
                spawn_ledger.record_discovered(rs.session_id)
        except Exception:
            logger.exception("failed to write backend_state.json for %s", rs.run_id)

    async def _flush_cursor_ledger(self, rs: RunState) -> None:
        """Block until `cursor_ledger_worker` has written this run's
        latest known cursor to `backend_state.json`, once a drain
        concludes — crash recovery must see the true final cursor, not
        whatever was last coalesced. Off-loop so the event loop itself
        never blocks on the write."""
        from cursor_ledger_worker import worker as cursor_ledger_worker
        await asyncio.to_thread(cursor_ledger_worker.flush_now, rs.run_id)

    def attach_recovered_run(
        self,
        *,
        desc: dict,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        """Re-attach a still-running detached gemini-family runner after
        a backend restart.

        The recovered descriptor proves the runner is still alive. Rebuild the
        in-memory RunState and restart the normal tailer/completion watcher so
        post-restart events are streamed immediately and the turn finalizes in
        this backend lifetime.
        """
        run_id = str(desc.get("run_id") or "")
        pid = desc.get("pid")
        if not run_id or not pid or run_id in self._runs:
            return False
        try:
            runner_pid = int(pid)
        except (TypeError, ValueError):
            return False
        try:
            processed_line = int(desc.get("processed_line") or 0)
        except (TypeError, ValueError):
            processed_line = 0

        rs = RunState(
            run_id=run_id,
            run_dir=_runs_root() / run_id,
            popen=RecoveredPopen(runner_pid),
            mode=desc.get("mode") or "native",
            app_session_id=desc.get("app_session_id") or "",
            queue=queue,
            session_id=desc.get("session_id"),
            processed_line=processed_line,
            started_at=desc.get("started_at") or datetime.now().isoformat(),
            cancelled=bool(desc.get("cancelled", False)),
            persist_to=desc.get("persist_to") or desc.get("app_session_id") or "",
            target_message_id=desc.get("target_message_id"),
            turn_run_id=desc.get("turn_run_id"),
        )
        self._runs[run_id] = rs
        self._write_backend_state(rs)
        schedule_loop_task(
            loop,
            self._bootstrap_run(rs),
            name=f"{self.KIND}-recover-bootstrap-{run_id[:8]}",
        )
        return True

    def _post_cancel_hook(self, rs: RunState) -> None:
        """Wake the tailer's stop_event so it exits its poll-sleep
        promptly rather than waiting up to _POLL_INTERVAL."""
        if rs.tailer is not None:
            try:
                rs.tailer.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # recover_in_flight
    # ------------------------------------------------------------------
    def recover_in_flight(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        run_id_filter: Optional[set[str]] = None,
    ) -> list[dict]:
        """Mirror `ClaudeProvider.recover_in_flight`'s descriptor shape
        so `run_recovery._integrate_one` reads identical keys regardless
        of provider kind.

        DEAD orphans get a synthesized complete.json + a full descriptor
        so the orchestrator can replay events into the assistant
        message. LIVE orphans return descriptors too so startup
        recovery can re-register active runs before accepting new
        prompts for the same session."""
        del loop

        recovered: list[dict] = []
        if not _runs_root().exists():
            return recovered

        if config_store.provider_suspended(self.id):
            return recovered

        for child in iter_run_dirs(run_id_filter):
            marker_path = child / "reconciled.marker"
            if marker_path.exists() and marker_matches_current(marker_path, self.KIND):
                continue
            complete_path = child / "complete.json"
            has_complete_json = complete_path.exists()

            backend_state_path = child / "backend_state.json"
            runner_state_path = child / "state.json"
            bs: dict = {}
            rs_disk: dict = {}
            if backend_state_path.exists():
                try:
                    bs = json.loads(backend_state_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            if runner_state_path.exists():
                try:
                    rs_disk = json.loads(runner_state_path.read_text(encoding="utf-8"))
                except Exception:
                    pass

            pid: Optional[int] = None
            try:
                pid = int(bs.get("runner_pid")) if bs.get("runner_pid") else None
            except (TypeError, ValueError):
                pass

            alive = _pid_alive(pid) if pid else False

            live_orphan = alive and not has_complete_json

            if live_orphan:
                logger.info(
                    "gemini recover_in_flight: live orphan %s (pid=%s) "
                    "still running; re-attaching for recovery",
                    child.name, pid,
                )

            if not live_orphan and not has_complete_json:
                # Dead orphan — synthesize complete.json so future scans
                # skip and the replay path is unambiguous.
                try:
                    complete_path.write_text(json.dumps({
                        "success": False,
                        "session_id": bs.get("session_id") or rs_disk.get("session_id"),
                        "error": "runner died before completion (recovered at startup)",
                        "token_usage": None,
                        "finished_at": datetime.now().isoformat(),
                    }, indent=2), encoding="utf-8")
                    has_complete_json = True
                except Exception:
                    logger.exception(
                        "failed to write recovery complete.json for %s", child.name,
                    )

            try:
                processed_line = int(bs.get("processed_line") or 0)
            except (TypeError, ValueError):
                processed_line = 0

            recovered.append({
                "run_id": child.name,
                "pid": pid,
                "alive": live_orphan,
                "has_complete_json": has_complete_json,
                "session_id": bs.get("session_id") or rs_disk.get("session_id"),
                "jsonl_path": (
                    bs.get("jsonl_path")
                    or rs_disk.get("jsonl_path")
                    or str(child / "session_events.jsonl")
                ),
                "app_session_id": bs.get("app_session_id") or rs_disk.get("app_session_id"),
                "persist_to": bs.get("persist_to") or bs.get("app_session_id"),
                "started_at": bs.get("started_at") or rs_disk.get("started_at") or "",
                "processed_line": processed_line,
                "cancelled": bool(bs.get("cancelled", False)),
                "mode": bs.get("mode") or rs_disk.get("mode") or "native",
                "provider_id": bs.get("provider_id") or self.id,
                "provider_kind": bs.get("provider_kind") or self.KIND,
                "ingestion_version": bs.get("ingestion_version"),
                "target_message_id": bs.get("target_message_id"),
                "turn_run_id": bs.get("turn_run_id"),
                "recovered_as": "live_orphan" if live_orphan else "dead_orphan",
            })

        return recovered

    # ------------------------------------------------------------------
    # prune_old_runs
    # ------------------------------------------------------------------
    def prune_old_runs(self, max_age_days: int = 7) -> int:
        return prune_old_completed_runs(max_age_days)

    # ------------------------------------------------------------------
    # run_headless — one-shot `gemini -p -o json`
    # ------------------------------------------------------------------
    async def run_headless(
        self,
        *,
        prompt: str,
        session_id: Optional[str] = None,
        resume_sid: Optional[str] = None,
        fork: bool = False,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
        no_tools: bool = False,
    ) -> Optional[dict]:
        self.assert_not_suspended(action="run headless work")
        cmd: list[str] = ["gemini", "-p", prompt, "-o", "json"]
        if no_tools:
            # Plan mode = read-only; the model cannot run mutating tools.
            cmd += ["--approval-mode", "plan"]
        resume_target = resume_sid or session_id
        if resume_target:
            cmd += ["-r", resume_target]
        if fork:
            logger.warning("Gemini provider ignores fork flag in run_headless")
        if self.record.get("mode", "subscription") == "subscription":
            logger.error("GeminiProvider.run_headless: %s", GEMINI_SUBSCRIPTION_UNSUPPORTED)
            return None

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.build_env(),
                cwd=cwd,
            )
        except FileNotFoundError:
            logger.error("GeminiProvider.run_headless: `gemini` CLI not found")
            return None
        except Exception:
            logger.exception("GeminiProvider.run_headless: spawn failed")
            return None

        try:
            kw = {"timeout": timeout} if timeout else {}
            stdout_bytes, stderr_bytes = await proc.communicate(**kw)
        except asyncio.TimeoutError:
            logger.error("GeminiProvider.run_headless: timeout after %ss", timeout)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return None

        if proc.returncode != 0:
            logger.error(
                "GeminiProvider.run_headless: exited %s; stderr=%r",
                proc.returncode, stderr_bytes[:500],
            )
            return None

        stdout = stdout_bytes.decode(errors="replace").strip()
        if not stdout:
            return None
        try:
            raw = json.loads(stdout)
        except json.JSONDecodeError:
            logger.error("GeminiProvider.run_headless: not JSON: %r", stdout[:500])
            return None
        # Translate gemini's `{session_id, response, stats}` envelope
        # to the claude-shaped `{result, session_id, usage,
        # total_cost_usd}` every downstream consumer
        # already speaks. INVARIANT: keep both shapes in `raw` so
        # gemini-aware callers can still introspect, but expose the
        # claude keys at the top level. Subscription mode has no
        # billing-cost surface → total_cost_usd is 0.0.
        stats = raw.get("stats") or {}
        usage = {
            "input_tokens": stats.get("input_tokens", 0),
            "output_tokens": stats.get("output_tokens", 0),
            "cache_read_input_tokens": stats.get("cached", 0),
            "total_tokens": stats.get("total_tokens", 0),
        }
        return {
            "result": raw.get("response") or "",
            "session_id": raw.get("session_id"),
            "usage": usage,
            "total_cost_usd": 0.0,
            # Pass through provider-native fields for callers that want them.
            "response": raw.get("response"),
            "stats": stats,
        }

    # ------------------------------------------------------------------
    # Models — inherits `Provider.available_models()` which routes
    # through `models.models_for_provider(self.id)` → the disk-backed
    # catalog written by the daily refresher (`fetch_gemini_models`
    # bundle scrape). NO override here — overriding would re-introduce
    # the cache-vs-gate divergence where the dropdown shows a freshly-
    # scraped model but `start_run` rejects it because the validator
    # reads the static list directly.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Rate-limit parsing — Gemini uses daily quotas + RESOURCE_EXHAUSTED.
    # ------------------------------------------------------------------
    _GEMINI_RATE_LIMIT_KEYWORDS = (
        "rate limit", "quota exceeded", "resource exhausted",
        "exhausted your capacity", "status: 429", "error 429",
        "too many requests",
    )

    def parse_rate_limit(
        self, error: Optional[str], events: list[dict],
    ) -> Optional[datetime]:
        """Parse Gemini rate-limit reset time from error / event text."""
        texts: list[str] = []
        if error:
            texts.append(error[-2000:] if len(error) > 2000 else error)
        extracted = self._extract_text_for_rate_limit(events)
        if extracted:
            texts.append(extracted)
        corpus = "\n".join(texts).lower()
        if not corpus:
            return None

        # Daily quota → reset at midnight UTC tomorrow
        if "daily quota" in corpus:
            tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
            return datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone.utc)

        if not any(kw in corpus for kw in self._GEMINI_RATE_LIMIT_KEYWORDS):
            return None

        return None

    # ------------------------------------------------------------------
    # rewind — we simulate rewind by clearing the session_id so the
    # NEXT turn starts a fresh CLI session.
    # ------------------------------------------------------------------
    async def rewind(self, app_sid: str, message_uuid: str) -> None:
        from session_manager import manager as session_manager
        session_manager.set_agent_sid(app_sid, "native", None)
        session_manager.set_agent_sid(app_sid, "manager", None)
