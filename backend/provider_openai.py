"""OpenAIProvider — `Provider` implementation for the BA-owned Better Agent runner.

Unlike claude/codex (which drive an external CLI subprocess), the
`openai` provider runs the agent loop inside BA itself: `runner_better_agent.py`
makes HTTP Chat Completions calls and executes tools in-process. It
normalizes its events to the Claude-jsonl shape and writes them to
`session_events.jsonl`; this provider tails that file (reusing
`SessionEventsJsonlTailer` verbatim — it is provider-agnostic, only the file
path differs) and pushes events onto the orchestrator queue.

Mirror of `provider_session_events.py` section-by-section: same RunState, same
bootstrap/complete lifecycle, same recovery classification, same
`_write_backend_state` shape so `run_recovery._integrate_one` reads
identical keys regardless of provider kind.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Optional

import httpx
import config_store
from provider_session_events import SessionEventsProvider
from reasoning_effort import (
    ALL_REASONING_EFFORTS,
    DEFAULT_REASONING_EFFORT,
)

logger = logging.getLogger(__name__)


_HEADLESS_TIMEOUT_S = 60.0


async def _openai_headless_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    timeout_s: float,
) -> tuple[str, dict]:
    """Small non-streaming Chat Completions call used by run_headless."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "stream": False}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(connect=15.0, read=timeout_s, write=30.0, pool=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
    choices = body.get("choices") or []
    message = (choices[0].get("message") if choices and isinstance(choices[0], dict) else {}) or {}
    content = message.get("content")
    if isinstance(content, list):
        text = "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in ("text", "output_text")
        )
    else:
        text = str(content or "")
    return text, body.get("usage") or {}


# ============================================================================
# OpenAIProvider
# ============================================================================
class OpenAIProvider(SessionEventsProvider):
    uses_managed_api_key = True
    """Drives the BA-owned `runner_better_agent.py` subprocess. The runner
    performs Chat Completions calls + in-process tool execution itself
    and writes normalized events to `session_events.jsonl`; this provider
    tails that file and pushes events onto the orchestrator queue."""

    KIND: ClassVar[str] = "openai"

    # The Better Agent runner owns the agent loop/history, so features that are
    # awkward CLI-specific hacks elsewhere are implemented directly here:
    # fork = copy BA-owned message history to a fresh agent session,
    # manager mode = expose the same loopback orchestration tools, and
    # steering = append an in-flight user steering message on the next round.
    supports_fork: ClassVar[bool] = True
    supports_manager_mode: ClassVar[bool] = True
    supports_rewind: ClassVar[bool] = True
    rewind_requires_agent_identity: ClassVar[bool] = False
    supports_steering: ClassVar[bool] = True
    supports_native_subagents: ClassVar[bool] = True
    supports_reasoning_effort: ClassVar[bool] = True
    reasoning_effort_options: ClassVar[tuple[str, ...]] = ALL_REASONING_EFFORTS
    default_reasoning_effort: ClassVar[str] = DEFAULT_REASONING_EFFORT
    supports_headless_no_tools: ClassVar[bool] = True


    # ------------------------------------------------------------------
    # Env — copy os.environ, strip foreign-provider vars, add OpenAI auth
    # ------------------------------------------------------------------
    def build_env(self) -> dict[str, str]:
        self.require_runtime_credential()
        env = os.environ.copy()
        # Clear foreign-provider env so it can't interfere with the runner.
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", None)
        env.pop("GEMINI_CLI_HOME", None)
        env.pop("GEMINI_API_KEY", None)
        env.pop("GOOGLE_API_KEY", None)
        env.pop("CODEX_HOME", None)
        env.pop("OPENAI_API_KEY", None)
        env.pop("OPENAI_BASE_URL", None)
        # Snapshot the record atomically (provider.py record setter
        # replaces the whole dict).
        rec = self.runtime_record()
        if rec.get("kind") == "codex" and rec.get("mode") == "subscription":
            # codex + better_agent_runner + subscription: the runner speaks
            # OpenAI's Codex ResponsesAPI directly over the ChatGPT-subscription
            # OAuth credential the `codex` CLI's own login produces. It reads
            # that credential from CODEX_HOME/auth.json (isolated per-account
            # exactly like the native codex runner — see
            # config_store.provider_credential_env), never from
            # OPENAI_API_KEY/OPENAI_BASE_URL.
            cred = config_store.provider_credential_env(rec)
            if cred:
                env[cred[0]] = cred[1]
            else:
                from paths import user_home
                env["CODEX_HOME"] = str(user_home() / ".codex")
            return self.finalize_env(env)
        # Pass api_key + base_url through to the runner so it can
        # authenticate against Chat Completions.
        api_key = rec.get("api_key")
        base_url = rec.get("base_url")
        if api_key:
            env["OPENAI_API_KEY"] = str(api_key)
        if base_url:
            env["OPENAI_BASE_URL"] = str(base_url)
        return self.finalize_env(env)

    # ------------------------------------------------------------------
    # start_run
    # ------------------------------------------------------------------
    def _start_run(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
        internal_token: Optional[str] = None,
        extra_env: Optional[dict[str, str]] = None,
        _execution,
        **_unused: Any,
    ) -> bool:
        del _unused
        self.start_session_events_execution(
            execution=_execution,
            loop=loop,
            queue=queue,
            internal_token=internal_token,
            extra_env=extra_env,
        )
        return True

    def steer_run(self, run_id: str, prompt: str, images: Optional[list] = None) -> bool:
        """Append a steering message for a live OpenAI turn.

        Chat Completions has no mid-token native steering primitive, but because
        BA owns the loop we can cleanly append the user's steer payload as the
        next user message before the next model round. This works during
        tool-heavy/long-running turns and avoids provider-CLI hacks.
        """
        rs = self._runs.get(run_id)
        images = images or []
        if rs is None or rs.popen.poll() is not None or (not prompt.strip() and not images):
            return False
        state_path = rs.run_dir / "state.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not state.get("session_id"):
            return False
        inbox = rs.run_dir / "steer.jsonl"
        try:
            with inbox.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"prompt": prompt, "images": images}) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError:
            logger.exception("openai steer_run failed for %s", run_id)
            return False

    # ------------------------------------------------------------------
    # run_headless — direct one-shot Chat Completions call.
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
        """Run one tool-less OpenAI completion and return a Claude-shaped
        headless envelope.

        `fork=True` copies BA-owned OpenAI history to a fresh sid before the
        prompt is appended, preserving composer guarantees that the
        source session is not mutated. `no_tools` is accepted for parity — this
        path never sends tools.
        """
        self.assert_not_suspended(action="run headless work")
        self.require_runtime_credential()
        del cwd, no_tools
        rec = self.runtime_record()
        base_url = str(rec.get("base_url") or "").strip()
        api_key = str(rec.get("api_key") or "").strip()
        model = str(rec.get("default_model") or "").strip()
        if not base_url or not api_key or not model:
            logger.error("OpenAIProvider.run_headless: base_url/api_key/default_model missing")
            return None

        try:
            import runner_better_agent as _ro
            parent_sid = resume_sid or session_id
            if fork:
                sid, messages = _ro._load_history_for_run(parent_sid, fork=True)
            else:
                sid, messages = _ro._load_history(session_id or resume_sid)
            if session_id and not resume_sid and sid != session_id:
                sid = session_id
            if not messages or messages[0].get("role") != "system":
                messages.insert(0, {"role": "system", "content": _ro._SYSTEM_PROMPT})
            messages.append({"role": "user", "content": prompt})

            text, usage = await _openai_headless_completion(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                timeout_s=timeout or _HEADLESS_TIMEOUT_S,
            )
            messages.append({"role": "assistant", "content": text})
            _ro._save_history(sid, messages)
            mapped_usage = {
                "input_tokens": int((usage or {}).get("prompt_tokens") or 0),
                "output_tokens": int((usage or {}).get("completion_tokens") or 0),
                "cache_read_input_tokens": int(((usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
                "total_tokens": int((usage or {}).get("total_tokens") or 0),
            }
            return {
                "result": text,
                "session_id": sid,
                "usage": mapped_usage,
                "total_cost_usd": 0.0,
                "is_error": False,
            }
        except Exception:
            logger.exception("OpenAIProvider.run_headless failed")
            return None

    # ------------------------------------------------------------------
    # Rate-limit parsing — unblocks the orchestrator's rate-limit retry
    # loop (turn_manager). Without this, a 429 from the Chat Completions
    # endpoint raises AttributeError at turn_manager's parse_rate_limit
    # call site and aborts the turn instead of retrying.
    # ------------------------------------------------------------------
    _OPENAI_RATE_LIMIT_KEYWORDS = (
        "rate limit", "quota exceeded", "resource exhausted",
        "status: 429", "error 429", "too many requests",
        "usage limit", "capacity", "subscription window",
    )
    # Long-reset quota exhaustion (e.g. Sakana's "Subscription window is
    # exceeded") vs a short per-minute throttle: the orchestrator clamps
    # the wait to 600s either way, but the reset time is surfaced to the
    # UI as retrying_until, so keep it honest.
    _OPENAI_RATE_LIMIT_LONG_KEYWORDS = (
        "subscription window", "quota exceeded", "usage limit",
    )

    def parse_rate_limit(
        self, error: Optional[str], events: list[dict],
    ) -> Optional[datetime]:
        texts: list[str] = []
        if error:
            texts.append(error[-2000:] if len(error) > 2000 else error)
        extracted = self._extract_text_for_rate_limit(events)
        if extracted:
            texts.append(extracted)
        corpus = "\n".join(texts).lower()
        if not corpus:
            return None
        if not any(kw in corpus for kw in self._OPENAI_RATE_LIMIT_KEYWORDS):
            return None
        if any(kw in corpus for kw in self._OPENAI_RATE_LIMIT_LONG_KEYWORDS):
            return self._fallback_rate_limit(hours=1)
        return datetime.now(timezone.utc) + timedelta(minutes=1)

    # ------------------------------------------------------------------
    # rewind — we simulate rewind by clearing the session_id so the
    # NEXT turn starts a fresh Chat Completions history.
    # ------------------------------------------------------------------
    async def rewind(self, app_sid: str, message_uuid: str) -> None:
        del message_uuid
        from session_manager import manager as session_manager
        session_manager.set_agent_sid(app_sid, "native", None)
        session_manager.set_agent_sid(app_sid, "manager", None)
