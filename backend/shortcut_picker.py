"""Pick contextually-relevant shortcut responses using the active provider's cheapest model.

Uses whatever provider is currently active in config_store, preferring a
haiku-class model.  Any error (no provider, API failure, parse error)
causes the endpoint to signal "unfiltered" — the caller returns ALL
shortcuts so the user always sees something.
"""

import asyncio
import hashlib
import json
import logging
import time

import httpx

import config_store
import user_prefs
from prompt_templates import render_prompt

logger = logging.getLogger(__name__)

_CACHE_TTL_SECS = 300.0
_CACHE_MAX = 128
_PICK_WAIT_TIMEOUT_SECS = 0.25
_cache: dict[str, tuple[float, list[str]]] = {}
_inflight: dict[str, asyncio.Task[list[str]]] = {}
_lock = asyncio.Lock()

_SYSTEM_PROMPT = render_prompt("shortcut_picker/system.md")

# Prefer cheap models for classification; fall back to provider default.
_CHEAP_MODELS = [
    "claude-haiku-4-5-20251001",
    "claude-3-5-haiku-latest",
    "claude-3-5-haiku-20241022",
]


def prewarm_http_stack() -> None:
    async def _open_close() -> None:
        client = httpx.AsyncClient(timeout=0.001)
        await client.aclose()

    asyncio.run(_open_close())


def _pick_model(provider: dict) -> str:
    """Return the cheapest available model for the active provider."""
    available = list(provider.get("custom_models") or [])
    default = provider.get("default_model") or ""
    if default:
        available.append(default)
    for cheap in _CHEAP_MODELS:
        if cheap in available:
            return cheap
    return default or _CHEAP_MODELS[0]


def _cache_key(
    *,
    provider: dict,
    model: str,
    shortcuts_json: str,
    assistant_text: str,
) -> str:
    payload = json.dumps(
        {
            "provider_id": provider.get("id") or "",
            "base_url": provider.get("base_url") or "",
            "model": model,
            "shortcuts": shortcuts_json,
            "assistant_text": assistant_text,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _shortcut_picker_inputs(assistant_text: str) -> tuple[list[str], dict | None, str, str, str]:
    all_shortcuts = user_prefs.get_shortcut_responses()
    if not all_shortcuts:
        return [], None, "", "", ""
    provider = config_store.get_default_provider()
    if not provider:
        return all_shortcuts, None, "", "", ""
    model = _pick_model(provider)
    shortcuts_json = json.dumps(all_shortcuts)
    assistant_excerpt = assistant_text[:4000]
    return all_shortcuts, provider, model, shortcuts_json, assistant_excerpt


async def _cached_pick(key: str, factory) -> list[str]:
    now = time.monotonic()
    owner = False
    async with _lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL_SECS:
            return list(cached[1])
        task = _inflight.get(key)
        if task is None:
            task = asyncio.create_task(factory())
            _inflight[key] = task
            owner = True

    try:
        result = await task
    finally:
        if owner:
            async with _lock:
                _inflight.pop(key, None)

    async with _lock:
        _cache[key] = (time.monotonic(), list(result))
        while len(_cache) > _CACHE_MAX:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)
    return list(result)


async def pick_shortcuts(assistant_text: str) -> list[str]:
    """Return a subset of configured shortcuts relevant to the last assistant message.

    Returns ALL shortcuts on any error so the caller can always show something.
    """
    fallback_shortcuts: list[str] | None = None

    async def _pick_with_inputs() -> list[str]:
        nonlocal fallback_shortcuts
        all_shortcuts, provider, model, shortcuts_json, assistant_excerpt = await asyncio.to_thread(
            _shortcut_picker_inputs,
            assistant_text,
        )
        fallback_shortcuts = list(all_shortcuts)
        if not all_shortcuts:
            return []

        if not provider:
            logger.debug("No active provider, returning all shortcuts")
            return all_shortcuts

        base_url = (provider.get("base_url") or "").rstrip("/")
        api_key = provider.get("api_key", "")
        if not api_key:
            logger.debug("Active provider has no API key, returning all shortcuts")
            return all_shortcuts

        key = _cache_key(
            provider=provider,
            model=model,
            shortcuts_json=shortcuts_json,
            assistant_text=assistant_excerpt,
        )

        async def _pick_uncached() -> list[str]:
            user_msg = f"SHORTCUTS:\n{shortcuts_json}\n\nLAST ASSISTANT MESSAGE:\n{assistant_excerpt}"

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{base_url}/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": 128,
                        "system": _SYSTEM_PROMPT,
                        "messages": [{"role": "user", "content": user_msg}],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data.get("content", [])
                if not content:
                    return all_shortcuts

                text = content[0].get("text", "[]").strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

                indices = json.loads(text)
                if not isinstance(indices, list):
                    return all_shortcuts

                result = [
                    all_shortcuts[i]
                    for i in indices
                    if isinstance(i, int) and 0 <= i < len(all_shortcuts)
                ]
                return result if result else all_shortcuts

        return await asyncio.shield(_cached_pick(key, _pick_uncached))

    try:
        return await asyncio.wait_for(_pick_with_inputs(), timeout=_PICK_WAIT_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        logger.debug("Shortcut picker call still running, returning all shortcuts")
        if fallback_shortcuts is not None:
            return list(fallback_shortcuts)
        return await asyncio.to_thread(user_prefs.get_shortcut_responses)
    except Exception:
        logger.debug("Shortcut picker call failed, returning all shortcuts", exc_info=True)
        if fallback_shortcuts is not None:
            return list(fallback_shortcuts)
        return await asyncio.to_thread(user_prefs.get_shortcut_responses)
