"""Dynamic model discovery, scoped to the active provider.

`/api/models` MUST never block on a provider HTTP call. This module is
the disk-backed catalog that serves the model dropdown instantly. The
catalog is refreshed in the background by the daily refresher task
(`_models_catalog_refresher` in `main.py`) — never inline on a request.

Refresh paths per provider kind/mode:
  - Claude api_key      → HTTP /v1/models with x-api-key
  - Claude subscription → HTTP /v1/models with OAuth bearer
                          (token read from macOS Keychain entry
                          `Claude Code-credentials`)
  - Gemini              → parse the installed `gemini` CLI bundle's
                          `VALID_GEMINI_MODELS` Set (no usable HTTP API)
  - AGY                 → `agy models`

Cache file (`ba_home()/models_cache.{provider_id}.json`):
    {
      "schema": 2,
      "models": ["claude-...","..."],          # fetched list (order preserved)
      "retired": [
        {"id": "claude-3-opus-20240229", "first_absent_at": 1717000000.0}
      ],
      "last_refreshed_at": 1717000000.0,
      "last_fetch_state": "ok" | "failing"
    }

`retired[]`: models absent from the most recent successful fetch but
kept visible for `RETIRED_STICKY_DAYS` so pinned sessions still resolve.
`first_absent_at` is RESET when the model reappears — a model that
flaps every day will NEVER expire from the list. Semantic is
"continuously absent for 7d", not "cumulatively absent for 7d".
Intentional: flapping means still-being-served.

`available_models()` returns ONLY the active list. Callers that want
the inclusive view (e.g. `default_model` validation against a freshly-
retired model) call `available_models_including_retired()` explicitly.
"""

import asyncio
import copy
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import httpx

from env_compat import get_env
from config_store import get_default_provider, get_provider_with_key, list_providers
from json_store import write_json
from paths import ba_home

logger = logging.getLogger(__name__)

# Subscription-mode aliases: include Claude Code-specific variants
# (`[1m]` 1M-context activation) and cold-start data for subscription
# providers before the first refresh writes the cache. After refresh,
# the cache list is unioned with these so legacy/special variants
# survive even when the API drops them.
_SUBSCRIPTION_ALIASES = [
    "best",
    "fable",
    "opus",
    "opus[1m]",
    "sonnet",
    "sonnet[1m]",
    "haiku",
    "claude-fable-5",
    "claude-opus-4-8[1m]",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
]

SCHEMA_VERSION = 2
RETIRED_STICKY_DAYS = 7
RETIRED_CAP = 100
REFRESH_THRESHOLD_SECONDS = 86400  # 24h — overdue check in refresh_all_due

# Per-provider single-flight. Pre-warmed on startup; lazy-created for
# providers added after boot. Single-event-loop + no-await read-check-
# write makes the lazy path safe; eager pre-warm is belt+suspenders.
_refresh_locks: dict[str, asyncio.Lock] = {}
_cache_lock = threading.Lock()
_cache_by_path: dict[Path, tuple[tuple[int, int], dict]] = {}


def _lock_for(pid: str) -> asyncio.Lock:
    if pid not in _refresh_locks:
        _refresh_locks[pid] = asyncio.Lock()
    return _refresh_locks[pid]


def _models_cache_path(provider_id: str) -> Path:
    config_dir = get_env("BETTER_CLAUDE_CONFIG_DIR")
    if not config_dir:
        config_dir = str(ba_home())
    return Path(config_dir) / f"models_cache.{provider_id}.json"


def _read_cache(pid: str) -> Optional[dict]:
    """Returns parsed cache dict or None on missing/corrupt/wrong-schema.
    On corruption: logs WARNING and unlinks the file (no silent overwrite)."""
    path = _models_cache_path(pid)
    try:
        stat = path.stat()
    except FileNotFoundError:
        with _cache_lock:
            _cache_by_path.pop(path, None)
        return None
    fingerprint = (stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        cached = _cache_by_path.get(path)
        if cached is not None and cached[0] == fingerprint:
            return copy.deepcopy(cached[1])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(
            "models cache for %s is corrupt — wiping "
            "(last_refreshed_at, retired[], models all lost). Error: %s",
            pid, e,
        )
        path.unlink(missing_ok=True)
        with _cache_lock:
            _cache_by_path.pop(path, None)
        return None
    if data.get("schema") != SCHEMA_VERSION or not isinstance(data.get("models"), list):
        logger.warning(
            "models cache for %s has wrong schema (got schema=%r, "
            "models type=%s) — wiping",
            pid, data.get("schema"), type(data.get("models")).__name__,
        )
        path.unlink(missing_ok=True)
        with _cache_lock:
            _cache_by_path.pop(path, None)
        return None
    data.setdefault("retired", [])
    data.setdefault("last_refreshed_at", 0.0)
    data.setdefault("last_fetch_state", "ok")
    with _cache_lock:
        _cache_by_path[path] = (fingerprint, copy.deepcopy(data))
    return data


def _update_cache(
    pid: str,
    *,
    models: list[str] | None = None,
    retired: list[dict] | None = None,
    last_fetch_state: str | None = None,
) -> None:
    """Partial merge — only updates fields explicitly passed. Always
    bumps `last_refreshed_at` to now(). Always stamps schema=SCHEMA_VERSION."""
    cur = _read_cache(pid) or {
        "schema": SCHEMA_VERSION,
        "models": [],
        "retired": [],
        "last_fetch_state": "ok",
    }
    if models is not None:
        cur["models"] = models
    if retired is not None:
        cur["retired"] = retired
    if last_fetch_state is not None:
        cur["last_fetch_state"] = last_fetch_state
    cur["last_refreshed_at"] = time.time()
    cur["schema"] = SCHEMA_VERSION
    path = _models_cache_path(pid)
    write_json(path, cur)
    try:
        stat = path.stat()
    except OSError:
        with _cache_lock:
            _cache_by_path.pop(path, None)
    else:
        with _cache_lock:
            _cache_by_path[path] = ((stat.st_mtime_ns, stat.st_size), copy.deepcopy(cur))


def _merge_retired(
    prev_retired: list[dict],
    removed_now: list[str],
    reappeared: list[str],
    now: float,
) -> tuple[list[dict], list[str]]:
    """Returns (new_retired, evicted_ids). See module docstring for the
    7-day continuous-absence semantic."""
    survivors = [r for r in prev_retired if r["id"] not in reappeared]
    timer_expired = [
        r["id"] for r in survivors
        if now - r.get("first_absent_at", 0) > RETIRED_STICKY_DAYS * 86400
    ]
    survivors = [r for r in survivors if r["id"] not in timer_expired]
    survivors.extend({"id": m, "first_absent_at": now} for m in removed_now)
    survivors.sort(key=lambda r: r.get("first_absent_at", 0))
    overflow_evicted = (
        [r["id"] for r in survivors[:-RETIRED_CAP]]
        if len(survivors) > RETIRED_CAP else []
    )
    survivors = survivors[-RETIRED_CAP:]
    return survivors, timer_expired + overflow_evicted


# ---------------------------------------------------------------------
# Auth + fetch — per-provider mechanics.
# ---------------------------------------------------------------------

def _read_claude_subscription_token() -> Optional[str]:
    """Read the Claude CLI's OAuth access_token from macOS Keychain.

    Claude Code stores subscription creds in a Keychain item named
    `Claude Code-credentials` as JSON:
        {"claudeAiOauth": {"accessToken": "...", ...}}
    Returns the bearer token, or None if missing / malformed / not on
    macOS. The CLI refreshes the token internally; we just read whatever
    it most recently wrote.

    NOTE: macOS prompts (GUI dialog) the first time a non-owner process
    reads the item. uvicorn has no UI — the dialog blocks until the
    user (a) clicks "Always Allow" in the prompt OR (b) the 5s
    subprocess timeout fires (caught below). Workaround for a CI / dev
    box: `security add-generic-password -T <uvicorn_path>` to pre-add
    ACL access, OR open the Claude CLI once and click Allow on the
    first dialog. After that, the read is silent.
    """
    try:
        proc = subprocess.run(
            ["security", "find-generic-password",
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        # Likely a first-run Keychain dialog. Surface it so the user
        # knows why their subscription provider isn't refreshing.
        logger.warning(
            "keychain read for Claude subscription timed out (5s) — "
            "macOS may be showing a permission dialog; click Always "
            "Allow once to make subsequent reads silent",
        )
        return None
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.debug("keychain read unavailable (not macOS?): %s", e)
        return None
    if proc.returncode != 0:
        logger.debug(
            "Claude subscription keychain entry missing "
            "(security exit=%d)", proc.returncode,
        )
        return None
    try:
        data = json.loads(proc.stdout)
        return data["claudeAiOauth"]["accessToken"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("Claude keychain entry shape unexpected: %s", e)
        return None


def _fetch_api_models(
    base_url: str,
    *,
    api_key: Optional[str] = None,
    bearer_token: Optional[str] = None,
) -> list[str]:
    """Sync HTTP. Only call from a worker thread (via asyncio.to_thread).

    Exactly one of `api_key` / `bearer_token` must be set.
    - `api_key`     → standard Anthropic api-key flow.
    - `bearer_token` → subscription-OAuth flow. Requires the
      `anthropic-beta: oauth-2025-04-20` header to gate `/v1/models`.
    """
    if (api_key is None) == (bearer_token is None):
        raise ValueError("Pass exactly one of api_key / bearer_token")
    url = f"{base_url.rstrip('/')}/v1/models"
    headers: dict[str, str] = {"anthropic-version": "2023-06-01"}
    if api_key is not None:
        headers["x-api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {bearer_token}"
        headers["anthropic-beta"] = "oauth-2025-04-20"
    models: list[str] = []
    try:
        with httpx.Client(timeout=10) as client:
            params: dict = {"limit": 100}
            while True:
                r = client.get(url, headers=headers, params=params)
                r.raise_for_status()
                body = r.json()
                for m in body.get("data", []):
                    mid = m.get("id")
                    if mid:
                        models.append(mid)
                if not body.get("has_more"):
                    break
                last = body.get("last_id")
                if not last:
                    break
                params["after"] = last
    except httpx.HTTPStatusError as e:
        # Surface status + body excerpt — silent failures here become
        # invisible `last_fetch_state=failing` with no actionable signal
        # (e.g. Anthropic rotating the `anthropic-beta: oauth-...`
        # gate would 401 every subscription refresh forever).
        excerpt = ((e.response.text or "")[:300]).replace("\n", " ")
        logger.warning(
            "Models fetch HTTP %d from %s: %s",
            e.response.status_code, url, excerpt,
        )
    except Exception as e:
        logger.warning("Failed to fetch models from API: %s", e)
    return models


def fetch_openai_models(base_url: str, api_key: str) -> list[str]:
    """List models from an OpenAI-compatible endpoint (GET {base_url}/models).

    Sync HTTP — call only from a worker thread (via asyncio.to_thread).
    base_url is used verbatim (Sakana/Z.AI/etc. already include the /v1
    segment), so the endpoint is `{base_url}/models`. Returns [] on any
    failure so the catalog falls back to the configured default model.
    """
    if not base_url or not api_key:
        return []
    url = f"{base_url.rstrip('/')}/models"
    models: list[str] = []
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
            r.raise_for_status()
            for m in r.json().get("data", []):
                mid = m.get("id")
                if mid:
                    models.append(mid)
    except httpx.HTTPStatusError as e:
        excerpt = ((e.response.text or "")[:300]).replace("\n", " ")
        logger.warning(
            "OpenAI-compatible models fetch HTTP %d from %s: %s",
            e.response.status_code, url, excerpt,
        )
    except Exception as e:
        logger.warning("Failed to fetch OpenAI-compatible models from %s: %s", url, e)
    return models


def _runtime_kind_for_provider(provider: dict) -> str:
    if str(provider.get("runner") or "").strip() == "better_agent_runner":
        return "openai"
    return provider.get("kind", "claude")


def _resolve_refresh_fetch(rec: dict) -> Optional[Callable[[], list[str]]]:
    """For a provider record, return a zero-arg callable that fetches
    the live model list (sync, intended for `asyncio.to_thread`), or
    None if this provider is not refreshable right now.

    - Claude api_key       → HTTP with x-api-key
    - Claude subscription  → HTTP with OAuth bearer (keychain read)
    - Gemini               → scrape the installed CLI bundle
    - AGY                  → ask the installed CLI for models

    The closure captures `api_key` / `bearer_token` at resolve-time and
    uses it at fetch-time (within `refresh_one`'s `asyncio.to_thread`).
    Gap is microseconds in practice; OAuth tokens last 1h+ so the
    captured value stays valid. Intentional — we don't want to re-read
    the keychain after the per-provider lock is held.
    """
    kind = _runtime_kind_for_provider(rec)
    if kind == "claude":
        base_url = rec.get("base_url") or "https://api.anthropic.com"
        mode = rec.get("mode", "subscription")
        if mode == "api_key":
            api_key = rec.get("api_key") or ""
            if not api_key:
                return None
            return lambda: _fetch_api_models(base_url, api_key=api_key)
        if mode == "subscription":
            token = _read_claude_subscription_token()
            if not token:
                return None
            return lambda: _fetch_api_models(base_url, bearer_token=token)
        return None
    if kind == "openai":
        # OpenAI-compatible endpoint (sakana, z.ai, custom): list models via
        # the standard GET {base_url}/models. BA owns the agent loop; the key
        # is in the record (api_key mode only).
        base_url = rec.get("base_url") or ""
        api_key = rec.get("api_key") or ""
        if not base_url or not api_key:
            return None
        return lambda: fetch_openai_models(base_url, api_key)
    if kind == "gemini":
        from provider_gemini import fetch_gemini_models
        return fetch_gemini_models
    if kind == "codex":
        from provider_codex import fetch_codex_models
        return fetch_codex_models
    if kind == "fugu":
        from provider_fugu import fetch_fugu_models
        return fetch_fugu_models
    if kind == "agy":
        from provider_agy import fetch_agy_models
        return fetch_agy_models
    if kind == "copilot":
        from provider_copilot import fetch_copilot_models
        return fetch_copilot_models
    if kind == "pi":
        from provider_pi import fetch_pi_models
        return fetch_pi_models
    if kind == "qwen":
        from provider_qwen import fetch_qwen_models
        return fetch_qwen_models
    if kind == "cursor":
        from provider_cursor import fetch_cursor_models
        return fetch_cursor_models
    if kind == "kimi":
        from provider_kimi import fetch_kimi_models
        return fetch_kimi_models
    if kind == "amp":
        from provider_amp import fetch_amp_models
        return fetch_amp_models
    if kind == "opencode":
        from provider_opencode import fetch_opencode_models
        return fetch_opencode_models
    if kind == "grok":
        from provider_grok import fetch_grok_models
        return fetch_grok_models
    return None


# ---------------------------------------------------------------------
# Read-side: cache-only. NEVER inline-fetch from these. The refresher
# task owns network/disk I/O; readers only see disk state.
# ---------------------------------------------------------------------

def _dedupe_preserve_order(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in seq:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _static_cold_start(provider: dict) -> list[str]:
    """Cold-start data when no cache exists. Subscription Claude →
    `_SUBSCRIPTION_ALIASES`. Gemini → curated `GEMINI_MODELS`. Other
    cases → []. Explicit kind+mode pairing — no implicit fallthrough."""
    kind = _runtime_kind_for_provider(provider)
    if kind == "gemini":
        from provider_gemini import GEMINI_MODELS
        return list(GEMINI_MODELS)
    if kind == "codex":
        from provider_codex import CODEX_MODELS
        return list(CODEX_MODELS)
    if kind == "fugu":
        from provider_fugu import FUGU_MODELS
        return list(FUGU_MODELS)
    if kind == "agy":
        from provider_agy import AGY_MODELS
        return list(AGY_MODELS)
    if kind == "copilot":
        from provider_copilot import COPILOT_MODELS
        return list(COPILOT_MODELS)
    if kind == "pi":
        from provider_pi import PI_MODELS
        return list(PI_MODELS)
    if kind == "qwen":
        from provider_qwen import QWEN_MODELS
        return list(QWEN_MODELS)
    if kind == "cursor":
        from provider_cursor import CURSOR_MODELS
        return list(CURSOR_MODELS)
    if kind == "kimi":
        from provider_kimi import KIMI_MODELS
        return list(KIMI_MODELS)
    if kind == "amp":
        from provider_amp import AMP_MODELS
        return list(AMP_MODELS)
    if kind == "opencode":
        from provider_opencode import OPENCODE_MODELS
        return list(OPENCODE_MODELS)
    if kind == "grok":
        from provider_grok import GROK_MODELS
        return list(GROK_MODELS)
    if kind == "claude" and provider.get("mode", "subscription") == "subscription":
        return list(_SUBSCRIPTION_ALIASES)
    return []


def _read_catalog_models(provider: dict) -> tuple[list[str], list[str], bool, dict | None]:
    """Single source of truth for `_models_for` + `models_catalog`.
    Returns `(active_models, retired_ids, has_cache, cache_record)`.

    Semantics:
    - Cache present → use cache. For subscription Claude, also union
      with `_SUBSCRIPTION_ALIASES` so `[1m]` variants survive.
    - Cache absent → fall back to static cold-start data (subscription
      aliases or curated Gemini list). api_key Claude returns [].
    """
    cached = _read_cache(provider["id"])
    has_cache = cached is not None
    cached_models = list(cached.get("models") or []) if cached else []
    cached_retired = (
        [r["id"] for r in (cached.get("retired") or [])] if cached else []
    )

    kind = _runtime_kind_for_provider(provider)
    static_seed = _static_cold_start(provider) if not has_cache else []
    if has_cache:
        if (
            kind == "claude"
            and provider.get("mode", "subscription") == "subscription"
        ):
            models = _dedupe_preserve_order(
                list(_SUBSCRIPTION_ALIASES) + cached_models,
            )
        else:
            models = cached_models
    else:
        models = static_seed

    return models, cached_retired, has_cache, cached


def _models_for(provider: dict, *, include_retired: bool = False) -> list[str]:
    """Cache-only read. Never makes an HTTP call. See `_read_catalog_models`
    for per-kind semantics. The configured `default_model` is always unioned
    in so the selector is never empty before/without a successful fetch (e.g.
    a fresh openai provider whose first /models fetch hasn't run yet)."""
    custom = list(provider.get("custom_models") or [])
    models, retired_ids, _has_cache, _cached = _read_catalog_models(provider)
    if include_retired:
        models = models + retired_ids
    default_model = provider.get("default_model") or ""
    seed = []
    if default_model:
        known = set(models + custom + retired_ids)
        kind = _runtime_kind_for_provider(provider)
        if kind != "codex" or not _cached or default_model in known:
            seed = [default_model]
    return _dedupe_preserve_order(models + custom + seed)


def available_models(provider_id: Optional[str] = None) -> list[str]:
    """Active models only. Subscription/Gemini providers return their
    static list on cold start, or the refreshed cache after first
    refresh. Does NOT include retired models.

    Returns [] for no-active-provider / unknown-provider-id — NEVER
    fabricates aliases for a context that has no real provider behind
    it (project rule: "Never use hardcoded values that will be shown
    when no valid values available — very confusing").
    """
    if provider_id is None:
        active = get_default_provider()
        if not active:
            return []
        return _models_for(active)
    rec = get_provider_with_key(provider_id)
    if not rec:
        return []
    return _models_for(rec)


def models_for_provider(provider_id: str) -> list[str]:
    """Backwards-compat alias for any-provider active-models read."""
    return available_models(provider_id)


def available_models_including_retired(provider_id: Optional[str] = None) -> list[str]:
    """Active + recently-retired (within RETIRED_STICKY_DAYS). Use for
    `default_model` validation so a session pinned to a just-retired
    model still resolves."""
    if provider_id is None:
        active = get_default_provider()
        if not active:
            return []
        return _models_for(active, include_retired=True)
    rec = get_provider_with_key(provider_id)
    if not rec:
        return []
    return _models_for(rec, include_retired=True)


def models_catalog(provider_id: Optional[str] = None) -> dict:
    """Full catalog payload for `/api/models` / `/api/providers/{id}/models`.
    Cache-only.

    State resolution:
    - Cache present → `last_fetch_state` from disk.
    - Cache absent + provider has static cold-start data (subscription
      Claude, Gemini) → state="ok" (cold-start data IS canonical).
    - Cache absent + no cold-start data (api_key Claude pre-refresh) →
      state="warming". Frontend shows a loading banner.
    """
    if provider_id is None:
        rec = get_default_provider()
    else:
        rec = get_provider_with_key(provider_id)
    if not rec:
        # No active provider configured OR unknown pid → empty payload,
        # NOT fabricated aliases. Frontend handles empty + ok-state as
        # "no models for this context" rather than masking the issue.
        return {
            "models": [],
            "retired": [],
            "last_fetch_state": "ok",
            "last_refreshed_at": 0.0,
        }
    custom = list(rec.get("custom_models") or [])
    models, retired, has_cache, cached = _read_catalog_models(rec)

    if cached is not None:
        state = cached.get("last_fetch_state") or "ok"
        last_refreshed_at = cached.get("last_refreshed_at") or 0.0
    elif _static_cold_start(rec):
        state = "ok"
        last_refreshed_at = 0.0
    else:
        state = "warming"
        last_refreshed_at = 0.0

    return {
        "models": models + custom,
        "retired": retired,
        "last_fetch_state": state,
        "last_refreshed_at": last_refreshed_at,
    }


# ---------------------------------------------------------------------
# Write-side: refresher. Acquires per-provider lock, runs the fetch
# callable in a worker thread, diffs, persists, returns a transition
# diff the caller broadcasts via WS.
# ---------------------------------------------------------------------

async def refresh_one(pid: str) -> Optional[dict]:
    """Refresh one provider's catalog. Returns the four-disjoint-set
    transition payload if anything changed, else None.

    No-op (returns None without acquiring the lock) when the provider
    is not refreshable right now: Gemini CLI not installed, Claude
    subscription Keychain entry missing, api_key empty, etc.
    """
    import config_store
    if config_store.provider_suspended(pid):
        return None
    rec = get_provider_with_key(pid)
    if not rec:
        return None
    fetch = _resolve_refresh_fetch(rec)
    if fetch is None:
        return None

    async with _lock_for(pid):
        fetched = await asyncio.to_thread(fetch)
        prev = _read_cache(pid) or {
            "schema": SCHEMA_VERSION, "models": [],
            "retired": [], "last_fetch_state": "ok",
        }
        prev_models = list(prev.get("models") or [])
        prev_retired = list(prev.get("retired") or [])
        prev_retired_ids = {r["id"] for r in prev_retired}

        if not fetched:
            if prev_models:
                logger.warning(
                    "empty model fetch for %s — keeping previous catalog, "
                    "marking last_fetch_state=failing", pid,
                )
                _update_cache(pid, last_fetch_state="failing")
                return None
            _update_cache(pid, models=[], last_fetch_state="failing")
            return None

        fetched_set = set(fetched)
        prev_models_set = set(prev_models)
        removed_now = sorted(prev_models_set - fetched_set)
        reappeared = sorted(prev_retired_ids & fetched_set)

        now = time.time()
        new_retired_records, evicted_ids = _merge_retired(
            prev_retired, removed_now, reappeared, now,
        )
        new_retired_ids = {r["id"] for r in new_retired_records}

        models_unchanged = sorted(fetched) == sorted(prev_models)
        retired_unchanged = sorted(new_retired_ids) == sorted(prev_retired_ids)
        state_unchanged = prev.get("last_fetch_state") == "ok"
        if models_unchanged and retired_unchanged and state_unchanged:
            _update_cache(pid, last_fetch_state="ok")
            return None

        _update_cache(
            pid,
            models=fetched,
            retired=new_retired_records,
            last_fetch_state="ok",
        )

        newly_added = sorted(
            fetched_set - prev_models_set - prev_retired_ids,
        )
        became_active = list(reappeared)
        went_retired = list(removed_now)
        truly_removed = sorted(evicted_ids)
        return {
            "newly_added": newly_added,
            "became_active": became_active,
            "went_retired": went_retired,
            "truly_removed": truly_removed,
        }


async def refresh_all_due(threshold_seconds: int = REFRESH_THRESHOLD_SECONDS):
    """Iterate every configured provider; refresh those whose
    `last_refreshed_at + threshold_seconds < now`. Yields
    `(provider_id, diff_or_None)` per refreshed provider so the caller
    can broadcast per-provider as each completes.
    """
    now = time.time()
    state = list_providers()
    for rec_public in state.get("providers", []):
        if rec_public.get("suspended"):
            continue
        pid = rec_public["id"]
        # Threshold check FIRST — cheap (disk read of small JSON).
        # `_resolve_refresh_fetch` may shell out to `security` /
        # `which gemini` so we MUST NOT call it for providers that
        # aren't due. `refresh_one` does its own resolve under the
        # per-provider lock; returns None gracefully when the provider
        # can't be refreshed right now.
        cached = _read_cache(pid) or {}
        last = cached.get("last_refreshed_at", 0)
        if last + threshold_seconds > now:
            continue
        try:
            diff = await refresh_one(pid)
        except Exception:
            logger.exception("refresh_one failed for %s", pid)
            continue
        yield pid, diff


def prewarm_locks() -> None:
    """Eagerly construct an asyncio.Lock per known provider. Called
    once at startup inside the event loop. New providers added later
    fall back to lazy creation in `_lock_for`."""
    for rec in list_providers().get("providers", []):
        _lock_for(rec["id"])
