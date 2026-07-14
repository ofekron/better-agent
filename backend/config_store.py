"""Provider-based auth/config for the Better Agent backend.

Storage:
  - ~/.better-claude/config.json       — list of providers + active id
  - macOS Keychain (via `keyring`)     — per-provider API keys

Each provider record:
    {
      "id":            str,    # uuid
      "name":          str,    # user-facing label
      "mode":          "subscription" | "api_key",
      "base_url":      str,    # ANTHROPIC_BASE_URL (api_key mode only)
      "config_dir":    str,    # provider config root
      "custom_models": list[str],
      "default_model": str,    # default model id for new sessions / fallback
      "default_reasoning_effort": str,
      "runner":        "native" | "better_agent_runner",
      "suspended":     bool,   # hard usage stop: no turns / bg work while true
    }

The api_key for an api_key-mode provider is stored in the OS keychain under
service="better-agent", username=f"provider:{id}", with legacy
service="better-claude" fallback.

The "active provider" is the one whose env vars are applied to os.environ —
read at user-prompt send time so the next CLI spawn picks them up. Switching
providers re-applies env; previous providers' settings stay intact.
"""

import logging
import copy
import os
import re
import threading
import traceback
import uuid
from typing import Any, Callable, Optional

import keyring

# Captured at import time so `_get_password_with_reason`/`_set_password_with_reason`
# can detect a test replacing `keyring.get_password`/`set_password` directly
# (e.g. to mock the keychain) and fall back to calling those — rather than
# the macOS ctypes path below, which would silently bypass the mock and hit
# the real Keychain.
_ORIGINAL_KEYRING_GET_PASSWORD = keyring.get_password
_ORIGINAL_KEYRING_SET_PASSWORD = keyring.set_password

from json_store import read_json, write_json
from keychain_names import LEGACY_SERVICE, PRIMARY_SERVICE, service_names
from paths import ba_home, resolve_claude_config_dir, resolve_provider_config_dir, user_home
from provider_env import is_ollama_base_url
from reasoning_effort import (
    ALL_REASONING_EFFORTS,
    DEFAULT_REASONING_EFFORT,
    normalize_reasoning_effort,
)
from permission import (
    clean_default_permission,
    default_permission_for_kind,
    permission_axes_for_kind,
)

logger = logging.getLogger(__name__)

_state_cache_lock = threading.RLock()
_state_cache: tuple[tuple[int, int], dict] | None = None


def _config_path():
    return ba_home() / "config.json"


def _config_fingerprint() -> tuple[int, int]:
    try:
        stat = _config_path().stat()
    except FileNotFoundError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


def _engine_env_path():
    return ba_home() / "engine.env"


def _uses_claude_env(provider: dict) -> bool:
    # Claude routes creds through the .env path; every other kind uses the OS
    # keyring. Missing kind defaults to claude (True); unknown kind is False.
    import provider_manifest
    kind = provider.get("kind") or "claude"
    spec = provider_manifest.spec_for(kind)
    return bool(spec and spec.uses_claude_env)


KEYRING_SERVICE = PRIMARY_SERVICE
LEGACY_KEYRING_SERVICE = LEGACY_SERVICE
LEGACY_KEYRING_USERNAME = "anthropic-api-key"  # pre-providers-refactor

# Sentinel returned from the frontend to mean "keep the existing key".
KEEP_SENTINEL = "__keep__"

SAKANA_FUGU_API_BASE_URLS = ("https://api.sakana.ai/v1",)
SAKANA_FUGU_REASONING_EFFORTS = ("high", "xhigh")
ZAI_ANTHROPIC_CONFIG_DIR = "~/.claude-zai"


# ----------------------------------------------------------------------------
# Keychain helpers (per-provider)
# ----------------------------------------------------------------------------


def _keyring_username(provider_id: str) -> str:
    return f"provider:{provider_id}"


def _keyring_services() -> tuple[str, ...]:
    return service_names(KEYRING_SERVICE, LEGACY_KEYRING_SERVICE)


# `keyring`'s macOS backend never sets `kSecUseOperationPrompt`, so the OS
# "allow access" prompt shows only the generic calling-binary identity
# (e.g. "python" or "login") with no indication of what wants the item or
# why — a real source of accidental "Deny" clicks (see the caching fix
# above this module makes safe to recover from). macOS's underlying
# `SecItemCopyMatching`/`SecItemAdd` DO support a custom operation prompt;
# `keyring`'s own `backends.macOS.api` module already builds the CFDictionary
# query via ctypes; reuse those bindings here and add the one extra key
# `keyring` omits, rather than duplicating the ctypes plumbing.
def _macos_security_api():
    try:
        import platform
        if platform.system() != "Darwin":
            return None
        from keyring.backends import macOS as _mac_backend
        if not isinstance(keyring.get_keyring(), _mac_backend.Keyring):
            return None
        from keyring.backends.macOS import api as _mac_api
        return _mac_api
    except Exception:
        return None


def _keychain_reason(provider_id: str, verb: str) -> str:
    return f"Better Agent needs {verb} the API key for AI provider {provider_id!r}"


def _macos_get_password_with_reason(service: str, username: str, reason: str) -> str | None:
    """Like `keyring.get_password`, but the OS prompt (if shown) states
    `reason` instead of just the calling binary's generic identity."""
    import ctypes

    api = _macos_security_api()
    if api is None:
        raise RuntimeError("macOS Security API unavailable")
    query = api.create_query(
        kSecClass=api.k_("kSecClassGenericPassword"),
        kSecMatchLimit=api.k_("kSecMatchLimitOne"),
        kSecAttrService=service,
        kSecAttrAccount=username,
        kSecReturnData=True,
        kSecUseOperationPrompt=reason,
    )
    data = ctypes.c_void_p()
    status = api.SecItemCopyMatching(query, ctypes.byref(data))
    if status == api.error.item_not_found:
        return None
    api.Error.raise_for_status(status)
    return api.cfstr_to_str(data)


def _macos_set_password_with_reason(
    service: str, username: str, password: str, reason: str,
) -> None:
    """Like `keyring.set_password`, but the OS prompt (if shown, e.g. when
    overwriting an item this binary doesn't already own the ACL for)
    states `reason` instead of just the calling binary's generic identity."""
    import ctypes
    from contextlib import suppress

    api = _macos_security_api()
    if api is None:
        raise RuntimeError("macOS Security API unavailable")
    with suppress(api.NotFound):
        api.delete_generic_password(None, service, username)
    query = api.create_query(
        kSecClass=api.k_("kSecClassGenericPassword"),
        kSecAttrService=service,
        kSecAttrAccount=username,
        kSecValueData=password,
        kSecUseOperationPrompt=reason,
    )
    status = api.SecItemAdd(query, None)
    api.Error.raise_for_status(status)


def _get_password_with_reason(service: str, username: str, reason: str) -> str | None:
    """`keyring.get_password`, using a descriptive macOS Keychain prompt
    reason where the platform/backend supports it; falls back to the
    plain call (generic prompt) everywhere else, including when a caller
    (test code) has replaced `keyring.get_password` itself."""
    if (
        keyring.get_password is _ORIGINAL_KEYRING_GET_PASSWORD
        and _macos_security_api() is not None
    ):
        return _macos_get_password_with_reason(service, username, reason)
    return keyring.get_password(service, username)


def _set_password_with_reason(
    service: str, username: str, password: str, reason: str,
) -> None:
    """`keyring.set_password`, using a descriptive macOS Keychain prompt
    reason where the platform/backend supports it; falls back to the
    plain call (generic prompt) everywhere else, including when a caller
    (test code) has replaced `keyring.set_password` itself."""
    if (
        keyring.set_password is _ORIGINAL_KEYRING_SET_PASSWORD
        and _macos_security_api() is not None
    ):
        _macos_set_password_with_reason(service, username, password, reason)
    else:
        keyring.set_password(service, username, password)


# `keyring` on macOS calls `SecItemCopyMatching` via ctypes. When the
# caller binary lacks the keychain item's ACL — e.g. items added by the
# dev Python venv read from the PyInstaller-frozen `.app` (a different
# binary) — macOS shows a blocking "allow access" GUI prompt, and a
# launchd-launched `.app` may never get focus to answer it. So every
# keyring call below runs on a daemon thread with a hard timeout; on
# timeout we proceed without the value rather than block startup.
_KEYRING_TIMEOUT = 2.0
# Set to True the first time a keyring call times out — every subsequent
# call in this process short-circuits to its `default`. Without this,
# code that hits keyring in a loop (e.g. `recover_all_in_flight` per
# run-dir) accumulates 2s per call and minutes of startup latency.
_keyring_blocked = False


def _keyring_call(
    fn: Callable[..., Any], *args: Any, default: Any = None,
    failure_flag: list[bool] | None = None,
) -> Any:
    """Run a keyring operation with a `_KEYRING_TIMEOUT` deadline. After
    the first timeout the keychain is treated as inaccessible for the
    rest of the process lifetime — the worker thread is still blocked
    in `SecItemCopyMatching` and cannot be cancelled, so we don't waste
    additional 2s windows on every caller.

    If `failure_flag` is given, an item is appended to it whenever the
    call did not complete successfully (raised — e.g. the user denied a
    one-off Keychain access prompt — or timed out). Callers that cache
    the result (`_read_api_key` et al.) use this to avoid caching a
    denied/failed read as if it were a confirmed value: a single
    accidental "Deny" click must not permanently disable the provider
    for the rest of the process's life."""
    global _keyring_blocked
    if _keyring_blocked:
        if failure_flag is not None:
            failure_flag.append(True)
        return default
    result: list[Any] = [default]
    done = threading.Event()

    def worker() -> None:
        try:
            result[0] = fn(*args)
        except Exception as e:
            logger.warning("keyring %s failed: %s", fn.__name__, e)
            if failure_flag is not None:
                failure_flag.append(True)
        finally:
            done.set()

    threading.Thread(
        target=worker, daemon=True, name=f"keyring-{fn.__name__}",
    ).start()
    if not done.wait(timeout=_KEYRING_TIMEOUT):
        _keyring_blocked = True
        logger.warning(
            "keyring %s timed out after %.1fs — disabling keyring for "
            "this process. (The frozen .app's signature likely lacks "
            "the keychain ACL of items written by the dev Python; "
            "re-enter API keys via the app UI.)",
            fn.__name__, _KEYRING_TIMEOUT,
        )
        if failure_flag is not None:
            failure_flag.append(True)
    return result[0]


# Per-process api_key cache, keyed by provider_id (or the
# `_LEGACY_CACHE_KEY` sentinel for the pre-multi-provider slot). Turns
# every steady-state keyring read into an O(1) dict lookup.
#
# Why: `_keyring_call` spawns a worker thread + waits up to 2s on macOS
# Keychain access. The event loop reaches `_read_api_key` from every
# async endpoint that resolves the active provider (`POST /api/sessions`
# via `get_default_provider`, `GET /api/providers` via `_strip`'s
# `has_api_key` probe, `apply_env_vars` at user-prompt send time...).
# Without this cache each request paid that thread-spawn + Event.wait
# on the asyncio loop; if Keychain ACLs went weird (the .app vs dev
# Python case in `_keyring_call`'s comment) the loop stalled the full
# 2s. With the cache, only the very first read per provider can stall;
# `warm_keyring_cache` moves even that first stall to startup, OFF the
# request hot path.
#
# INVARIANT — coherency: every write (`_write_api_key`, `_delete_api_key`,
# `_delete_legacy_api_key`) updates the cache, so the in-memory copy
# matches what `keyring.get_password` would return for the rest of this
# process. Manual `/usr/bin/security` edits from outside the backend are
# NOT reflected; restart the backend to pick them up. (Same constraint
# already applied implicitly via the post-timeout `_keyring_blocked`
# short-circuit — once that fires, the cache value is whatever was read
# successfully; this is strictly an improvement.)
_LEGACY_CACHE_KEY = "__legacy__"
_api_key_cache: dict[str, str] = {}
_api_key_cache_lock = threading.Lock()


def _read_api_key(provider_id: str) -> str:
    with _api_key_cache_lock:
        if provider_id in _api_key_cache:
            return _api_key_cache[provider_id]
    value, ok = _read_api_key_uncached(provider_id)
    if ok:
        with _api_key_cache_lock:
            _api_key_cache[provider_id] = value
    return value


def _read_api_key_uncached(provider_id: str) -> tuple[str, bool]:
    """Returns `(value, ok)`. `ok` is False when the underlying keyring
    read did not complete successfully (denied/raised, or timed out) —
    the caller must not cache that as a confirmed "no key" result, since
    a retry once the transient condition clears could still find the
    real key."""
    reason = _keychain_reason(provider_id, "to read")
    for service in _keyring_services():
        failure: list[bool] = []
        value = _keyring_call(
            _get_password_with_reason,
            service, _keyring_username(provider_id), reason,
            default="",
            failure_flag=failure,
        ) or ""
        if failure:
            return "", False
        if value:
            return value, True
    return "", True


def _write_api_key(provider_id: str, api_key: str) -> None:
    # `_write_api_key("", ...)` and `_delete_api_key(...)` reach the
    # same on-keychain end state (no entry). Mirror that in the cache by
    # popping rather than storing `""`, so the cache distinguishes
    # "known empty" from "absent" only via dict membership — which today
    # nobody reads, but a future caller using `cache.get(pid)` vs.
    # `pid in cache` won't see asymmetry between the two write paths.
    if api_key:
        reason = _keychain_reason(provider_id, "to save")
        for service in _keyring_services():
            _keyring_call(
                _set_password_with_reason,
                service, _keyring_username(provider_id), api_key, reason,
            )
        with _api_key_cache_lock:
            _api_key_cache[provider_id] = api_key
    else:
        for service in _keyring_services():
            try:
                _keyring_call(
                    keyring.delete_password,
                    service, _keyring_username(provider_id),
                )
            except keyring.errors.PasswordDeleteError:
                pass
        with _api_key_cache_lock:
            _api_key_cache.pop(provider_id, None)


def _delete_api_key(provider_id: str) -> None:
    for service in _keyring_services():
        try:
            _keyring_call(
                keyring.delete_password,
                service, _keyring_username(provider_id),
            )
        except keyring.errors.PasswordDeleteError:
            pass
    with _api_key_cache_lock:
        _api_key_cache.pop(provider_id, None)


def _read_legacy_api_key() -> str:
    with _api_key_cache_lock:
        if _LEGACY_CACHE_KEY in _api_key_cache:
            return _api_key_cache[_LEGACY_CACHE_KEY]
    value = ""
    ok = True
    reason = "Better Agent needs to read the legacy AI provider API key"
    for service in _keyring_services():
        failure: list[bool] = []
        value = _keyring_call(
            _get_password_with_reason,
            service, LEGACY_KEYRING_USERNAME, reason,
            default="",
            failure_flag=failure,
        ) or ""
        if failure:
            ok = False
            value = ""
            break
        if value:
            break
    if ok:
        with _api_key_cache_lock:
            _api_key_cache[_LEGACY_CACHE_KEY] = value
    return value


def _delete_legacy_api_key() -> None:
    for service in _keyring_services():
        _keyring_call(
            keyring.delete_password,
            service, LEGACY_KEYRING_USERNAME,
        )
    with _api_key_cache_lock:
        _api_key_cache.pop(_LEGACY_CACHE_KEY, None)


def warm_keyring_cache() -> None:
    """Pre-read every api_key provider's key (and the legacy slot) into
    the in-process cache. Called from `main.py` import-time alongside
    `apply_env_vars()` so the first `POST /api/sessions` or
    `GET /api/providers` after backend startup hits the cache instead
    of macOS Keychain. Idempotent; safe to call more than once."""
    try:
        state = _load_state()
    except Exception:
        logger.warning("warm_keyring_cache: _load_state failed", exc_info=True)
        return

    providers = [p for p in (state.get("providers", []) or []) if p.get("mode") == "api_key"]
    if not providers:
        return

    # Parallelize keyring reads so multiple slow/timing-out keychain
    # calls don't stack their 2s windows sequentially. 10 workers is
    # enough for a typical provider list.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=10, thread_name_prefix="warm-keyring") as executor:
        for p in providers:
            executor.submit(_read_api_key, p["id"])

    # Legacy slot is read at runtime ONLY by `_load_state`'s
    # flat→providers migration path, which has already run by the time
    # this function returns (we just called `_load_state` above). Once
    # migrated, the legacy keychain entry is deleted and no production
    # code path reads it again — so no warm-up needed. Calling
    # `_read_legacy_api_key()` here would just pay one extra keychain
    # call to cache an empty string nobody consumes.


# ----------------------------------------------------------------------------
# Migration & defaults
# ----------------------------------------------------------------------------


def _detect_provider_name(mode: str, base_url: str) -> str:
    if mode == "subscription":
        return "Claude"
    if "z.ai" in (base_url or "").lower():
        return "Z.AI"
    return "Custom API"


def _default_model_for(mode: str, base_url: str) -> str:
    if mode == "subscription":
        return "opus"
    if "z.ai" in (base_url or "").lower():
        return "glm-4.6"
    if is_ollama_base_url(base_url):
        return "qwen3-coder"
    return ""


def _is_zai_claude_provider(kind: str, mode: str, base_url: str) -> bool:
    return (
        kind == "claude"
        and mode == "api_key"
        and "z.ai" in (base_url or "").lower()
    )


GEMINI_SUBSCRIPTION_UNSUPPORTED = (
    "Gemini CLI subscription auth is no longer supported for individual, "
    "Google AI Pro, or Google AI Ultra accounts. Use Antigravity CLI or a "
    "supported Gemini API-key provider instead."
)

OPENAI_SUBSCRIPTION_UNSUPPORTED = (
    "OpenAI-compatible providers run on Better Agent's own agent loop over "
    "an API key; there is no subscription auth. Use api_key mode."
)


def _runtime_kind_for_provider(provider: dict) -> str:
    if str(provider.get("runner") or "").strip() == "better_agent_runner":
        return "openai"
    return provider.get("kind", "claude")


def _runtime_kind_for_config(kind: str, runner: object) -> str:
    if str(runner or "").strip() == "better_agent_runner":
        return "openai"
    return kind


def _provider_is_suspended(provider: dict | None) -> bool:
    return bool((provider or {}).get("suspended") is True)


def provider_suspended(provider_id: str | None) -> bool:
    if not provider_id:
        return False
    state = _load_state()
    for provider in state.get("providers", []):
        if provider.get("id") == provider_id:
            return _provider_is_suspended(provider)
    return False


def assert_provider_not_suspended(provider_id: str | None, *, action: str = "start runs") -> None:
    if provider_id and provider_suspended(provider_id):
        raise RuntimeError(f"provider {provider_id} is suspended; cannot {action}")


def _reject_unsupported_provider_config(kind: str, mode: str, runner: object = "") -> None:
    runtime_kind = _runtime_kind_for_config(kind, runner)
    if kind == "gemini" and mode == "subscription":
        raise ValueError(GEMINI_SUBSCRIPTION_UNSUPPORTED)
    if runtime_kind == "openai" and mode == "subscription":
        raise ValueError(OPENAI_SUBSCRIPTION_UNSUPPORTED)


def _runner_choices_for_kind(kind: str) -> list[str]:
    import provider_manifest
    return list(provider_manifest.runner_choices_for(kind))


def _clean_runner(kind: str, value: object) -> str:
    import provider_manifest
    runner = str(value or "").strip()
    choices = _runner_choices_for_kind(kind)
    if runner in choices:
        return runner
    return provider_manifest.default_runner_for(kind)


def _seed_default_state() -> dict:
    """Fresh-install default providers, with Claude active."""
    claude_pid = str(uuid.uuid4())
    codex_pid = str(uuid.uuid4())
    return {
        "default_provider_id": claude_pid,
        "providers": [
            {
                "id": claude_pid,
                "name": "Claude",
                "kind": "claude",
                "mode": "subscription",
                "base_url": "",
                "config_dir": "",
                "custom_models": [],
                "default_model": "opus",
                "default_reasoning_effort": DEFAULT_REASONING_EFFORT,
                "runner": _clean_runner("claude", ""),
                "default_permission": default_permission_for_kind("claude"),
                "suspended": False,
            },
            {
                "id": codex_pid,
                "name": "Codex",
                "kind": "codex",
                "mode": "subscription",
                "base_url": "",
                "config_dir": "",
                "custom_models": [],
                "default_model": "gpt-5.5",
                "default_reasoning_effort": DEFAULT_REASONING_EFFORT,
                "runner": _clean_runner("codex", ""),
                "default_permission": default_permission_for_kind("codex"),
                "suspended": False,
            },
        ],
    }


def _migrate_flat_to_providers(flat: dict) -> dict:
    """Convert the pre-providers config shape into the new schema.

    Copies the legacy keychain entry into the new provider's slot but
    does NOT delete the legacy slot here — that happens after the new
    schema is persisted (see `_load_state`) so a crash mid-migration
    can't lose the key."""
    mode = flat.get("mode", "subscription")
    base_url = flat.get("base_url", "") or ""
    normalized_mode = mode if mode in ("subscription", "api_key") else "subscription"
    config_dir = _clean_provider_config_dir(
        kind="claude",
        mode=normalized_mode,
        base_url=base_url,
        value=flat.get("config_dir", ""),
    )
    custom_models = flat.get("custom_models", []) or []
    pid = str(uuid.uuid4())
    provider = {
        "id": pid,
        "name": _detect_provider_name(mode, base_url),
        "kind": "claude",
        "mode": normalized_mode,
        "base_url": base_url,
        "config_dir": config_dir,
        "custom_models": list(custom_models),
        "default_model": _default_model_for(mode, base_url),
        "default_reasoning_effort": _clean_default_reasoning_effort("claude", None),
        "runner": _clean_runner("claude", ""),
        "suspended": False,
    }
    legacy_key = _read_legacy_api_key()
    if legacy_key and provider["mode"] == "api_key":
        _write_api_key(pid, legacy_key)
    return {"default_provider_id": pid, "providers": [provider]}


def _normalize_loaded_state(raw: dict) -> dict:
    providers = [
        {
            **p,
            "config_dir": _clean_provider_config_dir(
                kind=str(p.get("kind") or "claude").strip() or "claude",
                mode=p.get("mode", "subscription")
                if p.get("mode") in ("subscription", "api_key")
                else "subscription",
                base_url=str(p.get("base_url") or "").strip(),
                value=p.get("config_dir"),
            ),
            "suspended": _provider_is_suspended(p),
        }
        for p in raw["providers"]
        if isinstance(p, dict)
    ]
    active = raw.get("default_provider_id")
    active_record = next((p for p in providers if p.get("id") == active), None)
    if active_record is None or _provider_is_suspended(active_record):
        active = next(
            (p["id"] for p in providers if not _provider_is_suspended(p)),
            None,
        )
    return {
        "default_provider_id": active,
        "providers": providers,
        "delegate_task_policy": _normalize_delegate_task_policy(
            raw.get("delegate_task_policy")
        ),
        "disabled_builtin_tools": _normalize_disabled_builtin_tools(
            raw.get("disabled_builtin_tools")
        ),
        "disabled_builtin_extensions": _normalize_disabled_builtin_extensions(
            raw.get("disabled_builtin_extensions")
        ),
        "internal_llm": _normalize_internal_llm(raw.get("internal_llm")),
    }


def _clean_config_dir(value) -> str:
    """Canonicalize a provider `config_dir`; bare relative paths become `~/…`.

    Stored relative paths are ambiguous: the claude CLI resolves them
    against the session cwd (scattering a native store per project) while
    backend ingestion resolves them against the backend cwd, so the two
    never agree on where transcripts live. Anchoring at write time keeps
    the record portable across OSes (`~` expands per-platform) and spares
    every consumer from re-normalizing (paths.resolve_claude_config_dir
    remains the read-side safety net for pre-existing records).
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("~") or raw.startswith("$") or "%" in raw:
        return raw
    from pathlib import PureWindowsPath, PurePosixPath
    if PureWindowsPath(raw).is_absolute() or PurePosixPath(raw).is_absolute():
        return raw
    cleaned = raw.replace("\\", "/")
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return "~/" + cleaned


def _clean_provider_config_dir(
    *,
    kind: str,
    mode: str,
    base_url: str,
    value: object,
) -> str:
    cleaned = _clean_config_dir(value)
    if not _is_zai_claude_provider(kind, mode, base_url):
        return cleaned
    normalized = cleaned.replace("\\", "/").rstrip("/")
    if normalized in ("", "$HOME/.claude-zai", "${HOME}/.claude-zai"):
        return ZAI_ANTHROPIC_CONFIG_DIR
    return cleaned


def _resolved_provider_config_dir(value: str) -> str:
    return str(resolve_claude_config_dir(value))


# Default per-kind credential dir, keyed by the selecting env var. A record
# whose resolved config_dir equals this sits on the shared default account, so
# no per-account override is emitted and the ambient env is left untouched.
_CRED_ENV_DEFAULT_SUBDIR: dict[str, str] = {
    "CLAUDE_CONFIG_DIR": ".claude",
    "CODEX_HOME": ".codex",
}


def provider_credential_env(provider: dict) -> Optional[tuple[str, str]]:
    """`(env_var, absolute_dir)` selecting a provider's per-account credential
    directory, or None when the kind has no env-selectable dir, no config_dir
    is set, or config_dir resolves to the kind's shared default.

    Single source of truth for CLAUDE_CONFIG_DIR / CODEX_HOME per-account
    isolation, shared by provider spawn env (`build_env`) and `engine.env`."""
    import provider_manifest
    spec = provider_manifest.spec_for(provider.get("kind") or "claude")
    env_var = spec.credential_config_env if spec else None
    if not env_var:
        return None
    cfg_dir = (provider.get("config_dir") or "").strip()
    if not cfg_dir:
        return None
    resolved = resolve_provider_config_dir(cfg_dir)
    default_sub = _CRED_ENV_DEFAULT_SUBDIR.get(env_var)
    if default_sub and resolved.resolve() == (user_home() / default_sub).resolve():
        return None
    return env_var, str(resolved)


def _clean_provider_record(provider: dict) -> dict:
    kind = str(provider.get("kind") or "claude").strip() or "claude"
    runner = _clean_runner(kind, provider.get("runner"))
    mode = provider.get("mode", "subscription")
    if mode not in ("subscription", "api_key"):
        mode = "subscription"
    base_url = str(provider.get("base_url") or "").strip()
    _reject_unsupported_provider_config(kind, mode, runner)
    clean = {
        "id": str(provider.get("id") or uuid.uuid4()),
        "name": str(provider.get("name") or "").strip() or "Provider",
        "kind": kind,
        "mode": mode,
        "base_url": base_url,
        "config_dir": _clean_provider_config_dir(
            kind=kind,
            mode=mode,
            base_url=base_url,
            value=provider.get("config_dir"),
        ),
        "custom_models": [
            str(model).strip()
            for model in (provider.get("custom_models") or [])
            if str(model or "").strip()
        ],
        "default_model": str(provider.get("default_model") or "").strip(),
        "runner": runner,
        "default_permission": _clean_default_permission(
            _runtime_kind_for_config(kind, runner),
            provider.get("default_permission"),
        ),
        "suspended": provider.get("suspended") is True,
        "allowed_sinks": _clean_allowed_sinks(provider.get("allowed_sinks")),
        "capabilities": _clean_capabilities(provider.get("capabilities")),
    }
    clean["default_reasoning_effort"] = clean_default_reasoning_effort_for_provider(
        clean, provider.get("default_reasoning_effort"),
    )
    return clean


def _load_state() -> dict:
    global _state_cache
    fingerprint = _config_fingerprint()
    with _state_cache_lock:
        if _state_cache is not None and _state_cache[0] == fingerprint:
            return copy.deepcopy(_state_cache[1])
        # Cold path runs INSIDE the lock so a restart-time thundering herd
        # performs ONE disk read + parse instead of N. The faulthandler
        # watchdog ranked config_store._load_state -> read_json the #2
        # event-loop blocker (137 dumps; 120 in a single restart hour)
        # precisely because the read sat OUTSIDE the lock and every
        # concurrent first-access caller hit disk on the loop. The
        # fast-path check above doubles as the post-lock re-check: a herd
        # member that blocked acquiring the lock finds the leader's
        # populated cache (identical mtime/size fingerprint during a
        # restart) and returns it without touching disk. `_state_cache_lock`
        # is an RLock, so the `_save_state()` re-entry below is safe; lock
        # order is always _state_cache_lock -> _api_key_cache_lock (the
        # legacy-migration branch), never the reverse, so no deadlock.
        raw = read_json(_config_path(), {})
        if not raw:
            state = _seed_default_state()
            _save_state(state)
            return copy.deepcopy(_state_cache[1])
        # New schema?
        if "providers" in raw and isinstance(raw.get("providers"), list):
            state = _normalize_loaded_state(raw)
            _state_cache = (fingerprint, copy.deepcopy(state))
            return state
        # Old flat schema → migrate, persist, then drop the legacy keychain
        # slot. Order matters: save first so a crash during
        # _delete_legacy_api_key leaves the new schema in place; the new
        # keychain slot was populated before save inside
        # _migrate_flat_to_providers.
        state = _migrate_flat_to_providers(raw)
        _save_state(state)
        _delete_legacy_api_key()
        return copy.deepcopy(_state_cache[1])


def _log_removed_providers(new_providers: list) -> None:
    """Warn (with stack) whenever a provider present on disk is about to be
    dropped from the persisted set. Single chokepoint to catch whatever caller
    removes a provider (delete/update/config-sync/migration)."""
    try:
        old = read_json(_config_path(), {}) or {}
        old_list = old.get("providers", []) if isinstance(old, dict) else []
        if not isinstance(old_list, list):
            return
        new_ids = {p.get("id") for p in new_providers if isinstance(p, dict)}
        dropped = [
            p for p in old_list
            if isinstance(p, dict) and p.get("id") not in new_ids
        ]
        if not dropped:
            return
        for p in dropped:
            logger.warning(
                "PROVIDER REMOVED id=%s name=%r kind=%s base_url=%r — caller stack:\n%s",
                p.get("id"), p.get("name"), p.get("kind"), p.get("base_url"),
                "".join(traceback.format_stack()),
            )
    except Exception:
        logger.warning("_log_removed_providers failed", exc_info=True)


def _save_state(state: dict) -> None:
    global _state_cache
    new_providers = state.get("providers", [])
    _log_removed_providers(new_providers)
    payload = {
        "default_provider_id": state.get("default_provider_id"),
        "providers": state.get("providers", []),
        "delegate_task_policy": state.get("delegate_task_policy", "auto"),
        "disabled_builtin_tools": _normalize_disabled_builtin_tools(
            state.get("disabled_builtin_tools")
        ),
        "disabled_builtin_extensions": _normalize_disabled_builtin_extensions(
            state.get("disabled_builtin_extensions")
        ),
        "internal_llm": _normalize_internal_llm(state.get("internal_llm")),
    }
    write_json(_config_path(), payload)
    with _state_cache_lock:
        _state_cache = (_config_fingerprint(), copy.deepcopy(_normalize_loaded_state(payload)))


# ----------------------------------------------------------------------------
# Public API: delegate_task policy (global setting)
# ----------------------------------------------------------------------------
_DELEGATE_TASK_POLICIES = ("auto", "manual", "always_new", "always_new_approve")


def _normalize_delegate_task_policy(value) -> str:
    v = str(value or "").strip()
    return v if v in _DELEGATE_TASK_POLICIES else "auto"


def get_delegate_task_policy() -> str:
    """Global policy for the `delegate_task` tool:
    auto (search→first suggestion→dispatch), manual (same + approval),
    always_new (skip search, create fresh), always_new_approve (create + approval)."""
    return _normalize_delegate_task_policy(_load_state().get("delegate_task_policy"))


def set_delegate_task_policy(policy: str) -> str:
    normalized = _normalize_delegate_task_policy(policy)
    state = _load_state()
    state["delegate_task_policy"] = normalized
    _save_state(state)
    return normalized


# ----------------------------------------------------------------------------
# Public API: globally disabled built-in provider tools
# ----------------------------------------------------------------------------
DISABLEABLE_BUILTIN_TOOLS = frozenset({
    "ask",
    "create_session",
    "create_sub_session",
    "delegate_task",
    "mssg",
})


def _normalize_disabled_builtin_tools(value) -> list[str]:
    if not isinstance(value, list):
        return []
    tools = {
        str(item).strip()
        for item in value
        if str(item or "").strip() in DISABLEABLE_BUILTIN_TOOLS
    }
    return sorted(tools)


def get_disabled_builtin_tools() -> list[str]:
    return _normalize_disabled_builtin_tools(
        _load_state().get("disabled_builtin_tools")
    )


def set_disabled_builtin_tools(tools: list[str]) -> list[str]:
    normalized = _normalize_disabled_builtin_tools(tools)
    state = _load_state()
    state["disabled_builtin_tools"] = normalized
    _save_state(state)
    return normalized


_DISABLEABLE_EXTENSION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,79}$")


def _normalize_disabled_builtin_extensions(value) -> list[str]:
    if not isinstance(value, list):
        return []
    extension_ids = {
        str(item).strip()
        for item in value
        if _DISABLEABLE_EXTENSION_ID_RE.fullmatch(str(item or "").strip())
    }
    return sorted(extension_ids)


def get_disabled_builtin_extensions() -> list[str]:
    return _normalize_disabled_builtin_extensions(
        _load_state().get("disabled_builtin_extensions")
    )


def set_disabled_builtin_extensions(extension_ids: list[str]) -> list[str]:
    normalized = _normalize_disabled_builtin_extensions(extension_ids)
    state = _load_state()
    state["disabled_builtin_extensions"] = normalized
    _save_state(state)
    import extension_store
    extension_store.reconcile_native_mcp_servers()
    return normalized


# ----------------------------------------------------------------------------
# Public API: internal-LLM task assignments (global setting)
# ----------------------------------------------------------------------------
# Which provider + model + reasoning effort runs each backend-internal LLM
# task. A task with no assignment (or empty fields) inherits from the active
# provider at resolve time — so the unconfigured state is never a hardcode.
#
# `default_session` is the model/provider/effort stamped on every newly
# created user-facing session when the caller doesn't specify one.
# Core tasks owned by the backend itself. Extension-contributed tasks
# (public builtins and private-registry extensions) come from
# extension_store.all_internal_llm_task_keys() — no extension task name is
# hard-coded here.
_CORE_INTERNAL_LLM_TASKS = (
    "default_session",
    "delegation_task",
    "delegation_message",
    "delegation_ask",
    "delegation_session_bridge",
)
_INTERNAL_LLM_FIELDS = ("provider_id", "model", "reasoning_effort")


def internal_llm_tasks() -> tuple[str, ...]:
    """All known internal-LLM task keys: core tasks plus every
    extension-contributed task (absent extensions contribute nothing, so a
    pure-public checkout fails closed on private task keys)."""
    import extension_store
    seen = list(_CORE_INTERNAL_LLM_TASKS)
    for key in extension_store.all_internal_llm_task_keys():
        if key not in seen:
            seen.append(key)
    return tuple(seen)


def _normalize_internal_llm(raw) -> dict:
    """Coerce a raw mapping into `{task: {provider_id?, model?,
    reasoning_effort?}}` with only known tasks and non-empty string fields."""
    out: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return out
    known = internal_llm_tasks()
    for key, val in raw.items():
        if key not in known or not isinstance(val, dict):
            continue
        entry: dict[str, str] = {}
        for field in _INTERNAL_LLM_FIELDS:
            v = val.get(field)
            if isinstance(v, str) and v.strip():
                entry[field] = v.strip()
        if entry:
            out[key] = entry
    return out


def get_internal_llm_assignments() -> dict:
    """Raw stored assignments (task → optional fields). Returned verbatim;
    missing fields mean "inherit" at resolve time."""
    return _normalize_internal_llm(_load_state().get("internal_llm"))


def set_internal_llm_assignments(value: dict) -> dict:
    """Replace the whole assignment map. Unknown task keys / fields are
    dropped (fail closed) rather than persisted."""
    normalized = _normalize_internal_llm(value)
    state = _load_state()
    state["internal_llm"] = normalized
    _save_state(state)
    return normalized


def get_internal_llm_task(task_key: str) -> dict:
    """Raw stored assignment for one task (empty dict if unset)."""
    if task_key not in internal_llm_tasks():
        return {}
    return dict(get_internal_llm_assignments().get(task_key, {}))


def resolve_internal_llm(task_key: str) -> dict:
    """Concrete `{provider_id, model, reasoning_effort}` for a task.

    Each field falls back to the active provider's value when the assignment
    doesn't pin it, so a fully-unconfigured task resolves to the active
    provider + its default model + its default effort. `reasoning_effort`
    is "" when the resolved provider has no effort support."""
    state = _load_state()
    raw_assignments = _normalize_internal_llm(state.get("internal_llm"))
    assignment = dict(raw_assignments.get(task_key, {})) if task_key in internal_llm_tasks() else {}
    provider = None
    provider_id = assignment.get("provider_id")
    if provider_id:
        provider = next(
            (p for p in state.get("providers", []) if p.get("id") == provider_id),
            None,
        )
        if provider and _provider_is_suspended(provider):
            provider = None
            provider_id = None
    if provider is None:
        active_id = state.get("default_provider_id")
        provider = next(
            (p for p in state.get("providers", []) if p.get("id") == active_id),
            None,
        )
        if provider and _provider_is_suspended(provider):
            provider = None
        provider_id = provider["id"] if provider else None
    model = assignment.get("model") or (provider.get("default_model") if provider else "")
    effort = ""
    if provider and _capabilities_for(provider).get("supports_reasoning_effort"):
        options = reasoning_effort_options_for_provider(provider)
        chosen = assignment.get("reasoning_effort")
        if chosen in options:
            effort = chosen
        else:
            effort = provider.get("default_reasoning_effort") or (options[0] if options else "")
    return {"provider_id": provider_id, "model": model, "reasoning_effort": effort}


def default_session_model() -> str:
    return resolve_internal_llm("default_session")["model"]


def default_session_provider_id() -> Optional[str]:
    return resolve_internal_llm("default_session")["provider_id"]


def default_session_reasoning_effort() -> str:
    return resolve_internal_llm("default_session")["reasoning_effort"]


# ----------------------------------------------------------------------------
# Public API: providers
# ----------------------------------------------------------------------------


def _strip(provider: dict) -> dict:
    """Public view of a provider: includes has_api_key and the full
    capability matrix. Never the api_key itself."""
    kind = provider.get("kind", "claude")
    runtime_kind = _runtime_kind_for_provider(provider)
    caps = _capabilities_for(provider)
    # Effort options only exist where the (possibly overridden) capability
    # says reasoning effort is supported.
    effort_options = (
        reasoning_effort_options_for_provider(provider)
        if caps.get("supports_reasoning_effort")
        else []
    )
    default_effort = clean_default_reasoning_effort_for_provider(
        provider, provider.get("default_reasoning_effort")
    )
    permission_options = _kind_permission_options(runtime_kind)
    default_perm = (
        _clean_default_permission(runtime_kind, provider.get("default_permission"))
        if permission_options
        else {}
    )
    return {
        "id": provider["id"],
        "name": provider.get("name", ""),
        "kind": kind,
        "mode": provider.get("mode", "subscription"),
        "base_url": provider.get("base_url", ""),
        "config_dir": provider.get("config_dir", ""),
        "custom_models": provider.get("custom_models", []),
        "default_model": provider.get("default_model", ""),
        "runner": _clean_runner(kind, provider.get("runner")),
        "runner_options": _runner_choices_for_kind(kind),
        "suspended": _provider_is_suspended(provider),
        "reasoning_effort_options": effort_options,
        "default_reasoning_effort": default_effort if effort_options else "",
        "permission_options": permission_options,
        "default_permission": default_perm,
        # Credential-broker identity pin: host patterns this provider may
        # target with a user secret. Empty list = broker rejects all
        # credential requests from this provider (fail-closed).
        "allowed_sinks": list(provider.get("allowed_sinks", [])),
        "has_api_key": bool(_read_api_key(provider["id"])) if provider.get("mode") == "api_key" else False,
        # Capabilities — kind defaults overridden by the per-provider
        # `capabilities` map (kind is not the only decider). Frontend
        # reads these to gate buttons (Fork, Adv-Sync, Prompt-Engineer
        # refine, OrchestrationSelector "manager"
        # option, Rewind button) per-provider.
        **caps,
        # Raw per-provider overrides (only explicitly-set keys). The
        # resolved `**caps` above already bake these in; this map lets the
        # provider editor render the tri-state (inherit / force-on /
        # force-off) without confusing an override with a kind default.
        "capability_overrides": _clean_capabilities(provider.get("capabilities")),
    }


# INVARIANT: when adding a new `supports_*` flag on `Provider`, add it
# here too AND on `frontend/src/types.ts:Provider`. The frontend reads
# this matrix to gate UI per-provider.
_CAPABILITY_KEYS = (
    "supports_fork",
    "supports_manager_mode",
    "supports_rewind",
    "supports_steering",
    "supports_native_subagents",
    "supports_reasoning_effort",
)


def _kind_capabilities(kind: str) -> dict[str, bool]:
    """Static capability lookup. Mirrors the `Provider.supports_*` class
    attributes without instantiating. Lazy import dodges the
    config_store ↔ provider startup cycle."""
    try:
        from provider import _resolve_class
        cls = _resolve_class(kind)
        return {k: bool(getattr(cls, k)) for k in _CAPABILITY_KEYS}
    except Exception:
        # Unknown kind — assume capable; the runner will fail loudly if
        # the assumption is wrong.
        return {k: True for k in _CAPABILITY_KEYS}


def _clean_capabilities(raw) -> dict[str, bool]:
    """Per-provider capability overrides: only known `supports_*` keys with
    boolean values survive; everything else is dropped (fail closed)."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, bool] = {}
    for key in _CAPABILITY_KEYS:
        value = raw.get(key)
        if isinstance(value, bool):
            out[key] = value
    return out


def _capabilities_for(provider: dict) -> dict[str, bool]:
    """Resolved capability matrix for a provider record: kind defaults with
    the per-provider `capabilities` overrides applied on top. Kind is the
    default, not the only decider."""
    caps = _kind_capabilities(_runtime_kind_for_provider(provider))
    caps.update(_clean_capabilities(provider.get("capabilities")))
    return caps


def _kind_reasoning_effort_options(kind: str) -> list[str]:
    try:
        from provider import _resolve_class
        cls = _resolve_class(kind)
        raw = getattr(cls, "reasoning_effort_options", ())
        return [
            effort
            for effort in raw
            if isinstance(effort, str) and effort in ALL_REASONING_EFFORTS
        ]
    except Exception:
        return list(ALL_REASONING_EFFORTS)


def _normalized_base_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/").lower()


def _is_sakana_fugu_api(provider: dict) -> bool:
    if _runtime_kind_for_provider(provider) != "openai":
        return False
    return _normalized_base_url(provider.get("base_url")) in SAKANA_FUGU_API_BASE_URLS


def reasoning_effort_options_for_provider(provider: dict) -> list[str]:
    if _is_sakana_fugu_api(provider):
        return list(SAKANA_FUGU_REASONING_EFFORTS)
    return _kind_reasoning_effort_options(_runtime_kind_for_provider(provider))


def _kind_default_reasoning_effort(kind: str) -> str:
    try:
        from provider import _resolve_class
        cls = _resolve_class(kind)
        raw = getattr(cls, "default_reasoning_effort", "")
    except Exception:
        raw = DEFAULT_REASONING_EFFORT
    effort = normalize_reasoning_effort(raw)
    options = _kind_reasoning_effort_options(kind)
    if effort and effort in options:
        return effort
    return options[0] if options else ""


def _provider_default_reasoning_effort(provider: dict) -> str:
    if _is_sakana_fugu_api(provider):
        return SAKANA_FUGU_REASONING_EFFORTS[0]
    return _kind_default_reasoning_effort(_runtime_kind_for_provider(provider))


def _clean_default_reasoning_effort(kind: str, value: object) -> str:
    options = _kind_reasoning_effort_options(kind)
    if not options:
        return ""
    effort = normalize_reasoning_effort(value)
    if effort and effort in options:
        return effort
    return _kind_default_reasoning_effort(kind)


def clean_default_reasoning_effort_for_provider(provider: dict, value: object) -> str:
    options = reasoning_effort_options_for_provider(provider)
    if not options:
        return ""
    effort = normalize_reasoning_effort(value)
    if effort and effort in options:
        return effort
    return _provider_default_reasoning_effort(provider)


def _kind_permission_options(kind: str) -> dict[str, list[str]]:
    """Axis → allowed-values map for the frontend permission selector(s)."""
    return {
        axis: list(values) for axis, values in permission_axes_for_kind(kind).items()
    }


def _clean_default_permission(kind: str, value: object) -> dict:
    return clean_default_permission(kind, value)


def list_providers() -> dict:
    state = _load_state()
    return {
        "default_provider_id": state.get("default_provider_id"),
        "providers": [_strip(p) for p in state.get("providers", [])],
    }


def _clean_provider_sync_api_key_ids(provider_api_key_ids: object) -> tuple[str, ...]:
    if provider_api_key_ids is None:
        return ()
    if not isinstance(provider_api_key_ids, list | tuple):
        raise ValueError("provider_api_key_ids must be a list")
    ids: list[str] = []
    seen: set[str] = set()
    for item in provider_api_key_ids:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("provider_api_key_ids must contain non-empty strings")
        provider_id = item.strip()
        if provider_id in seen:
            continue
        seen.add(provider_id)
        ids.append(provider_id)
    return tuple(ids)


def _export_provider_sync_api_keys(
    providers: list[dict],
    provider_api_key_ids: tuple[str, ...],
) -> list[dict]:
    providers_by_id = {
        str(provider.get("id") or ""): provider
        for provider in providers
        if str(provider.get("id") or "")
    }
    out: list[dict] = []
    for provider_id in provider_api_key_ids:
        provider = providers_by_id.get(provider_id)
        if provider is None:
            raise ValueError(f"provider {provider_id!r} is not configured")
        if provider.get("mode") != "api_key":
            raise ValueError(f"provider {provider_id!r} does not use API-key credentials")
        api_key = _read_api_key(provider_id)
        if not api_key:
            raise ValueError(f"provider {provider_id!r} has no local API key")
        out.append({"provider_id": provider_id, "api_key": api_key})
    return out


def export_provider_sync_state(provider_api_key_ids: object = None) -> dict:
    """Provider configuration that is safe to send to an approved node.

    API keys are omitted by default. A caller may explicitly request selected
    api_key provider credentials after it has passed the machine-node approval
    and transport checks.
    """
    state = _load_state()
    providers = [_strip(p) for p in state.get("providers", [])]
    payload = {
        "default_provider_id": state.get("default_provider_id"),
        "providers": providers,
    }
    api_key_ids = _clean_provider_sync_api_key_ids(provider_api_key_ids)
    if api_key_ids:
        payload["provider_api_keys"] = _export_provider_sync_api_keys(
            providers,
            api_key_ids,
        )
    return payload


def _provider_has_local_runtime_auth(provider: dict) -> bool:
    if _provider_is_suspended(provider):
        return False
    if provider.get("mode") != "api_key":
        return True
    provider_id = str(provider.get("id") or "")
    return bool(provider_id and _read_api_key(provider_id))


def _clean_provider_sync_record(provider: dict) -> dict:
    clean = _clean_provider_record(provider)
    if clean.get("mode") == "api_key" and not _provider_has_local_runtime_auth(clean):
        clean["suspended"] = True
    return clean


def _provider_sync_default_provider_id(
    providers: list[dict],
    requested_default: str,
) -> str | None:
    providers_by_id = {
        str(provider.get("id") or ""): provider
        for provider in providers
        if str(provider.get("id") or "")
    }
    requested = providers_by_id.get(requested_default)
    if requested and _provider_has_local_runtime_auth(requested):
        return requested_default
    for provider in providers:
        if _provider_has_local_runtime_auth(provider):
            return provider.get("id")
    return None


def _import_provider_sync_api_keys(payload: dict, providers: list[dict]) -> int:
    raw_api_keys = payload.get("provider_api_keys", [])
    if raw_api_keys in (None, []):
        return 0
    if not isinstance(raw_api_keys, list):
        raise ValueError("provider_api_keys must be a list")
    providers_by_id = {
        str(provider.get("id") or ""): provider
        for provider in providers
        if str(provider.get("id") or "")
    }
    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw_api_keys:
        if not isinstance(item, dict):
            raise ValueError("provider_api_keys entries must be objects")
        provider_id = item.get("provider_id")
        api_key = item.get("api_key")
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("provider_api_keys entries must include provider_id")
        provider_id = provider_id.strip()
        provider = providers_by_id.get(provider_id)
        if provider is None:
            raise ValueError(f"provider credential {provider_id!r} is not in provider sync payload")
        if provider.get("mode") != "api_key":
            raise ValueError(f"provider credential {provider_id!r} is not for an API-key provider")
        if not isinstance(api_key, str) or not api_key:
            raise ValueError(f"provider credential {provider_id!r} is missing an API key")
        if provider_id in seen:
            raise ValueError(f"provider credential {provider_id!r} is duplicated")
        seen.add(provider_id)
        normalized.append((provider_id, api_key))

    for provider_id, api_key in normalized:
        _write_api_key(provider_id, api_key)
        verified_value, verified_ok = _read_api_key_uncached(provider_id)
        if not verified_ok or verified_value != api_key:
            with _api_key_cache_lock:
                _api_key_cache.pop(provider_id, None)
            raise ValueError(f"provider credential {provider_id!r} could not be stored")
    return len(normalized)


def import_provider_sync_state(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("provider sync payload must be an object")
    providers = payload.get("providers")
    if not isinstance(providers, list):
        raise ValueError("provider sync payload must include providers")
    clean_providers = [
        _clean_provider_record(dict(provider))
        for provider in providers
        if isinstance(provider, dict)
    ]
    imported_api_key_count = _import_provider_sync_api_keys(payload, clean_providers)
    state = _load_state()
    next_state = dict(state)
    next_state["providers"] = [
        _clean_provider_sync_record(provider)
        for provider in clean_providers
    ]
    requested_default = str(payload.get("default_provider_id") or "")
    next_state["default_provider_id"] = _provider_sync_default_provider_id(
        next_state["providers"],
        requested_default,
    )
    _save_state(next_state)
    result = list_providers()
    result["provider_api_key_count"] = imported_api_key_count
    return result


def list_provider_metadata() -> list[dict]:
    """Provider identity/config paths without capabilities or Keychain reads."""
    state = _load_state()
    return [
        {
            "id": p.get("id", ""),
            "name": p.get("name", ""),
            "kind": p.get("kind", "claude"),
            "config_dir": p.get("config_dir", ""),
        }
        for p in state.get("providers", [])
    ]


def get_provider(provider_id: str) -> Optional[dict]:
    state = _load_state()
    for p in state.get("providers", []):
        if p.get("id") == provider_id:
            return _strip(p)
    return None


def resolve_provider_ref(provider_ref: str) -> Optional[dict]:
    ref = str(provider_ref or "").strip()
    if not ref:
        return None
    state = _load_state()
    providers = list(state.get("providers", []))
    for p in providers:
        if p.get("id") == ref:
            return _strip(p)
    matches = [p for p in providers if str(p.get("name") or "") == ref]
    if len(matches) == 1:
        return _strip(matches[0])
    if len(matches) > 1:
        raise ValueError(f"provider name {ref!r} is ambiguous")
    folded = ref.casefold()
    matches = [
        p for p in providers
        if str(p.get("name") or "").casefold() == folded
    ]
    if len(matches) == 1:
        return _strip(matches[0])
    if len(matches) > 1:
        raise ValueError(f"provider name {ref!r} is ambiguous")
    return None


def get_provider_with_key(provider_id: str) -> Optional[dict]:
    """Internal: provider record INCLUDING its api_key (from keychain).
    Used by models.py to fetch a non-active provider's model list."""
    state = _load_state()
    for p in state.get("providers", []):
        if p.get("id") == provider_id:
            if _provider_is_suspended(p):
                return None
            cp = dict(p)
            cp["api_key"] = _read_api_key(provider_id) if p.get("mode") == "api_key" else ""
            return cp
    return None


def get_default_provider() -> Optional[dict]:
    """Return the active provider record INCLUDING its api_key (from keychain).

    Backend-internal callers (models.py, env application) read this. The
    HTTP layer never returns the api_key — see `list_providers` / `_strip`.
    """
    state = _load_state()
    active_id = state.get("default_provider_id")
    if not active_id:
        return None
    for p in state.get("providers", []):
        if p.get("id") == active_id:
            if _provider_is_suspended(p):
                return None
            cp = dict(p)
            cp["api_key"] = _read_api_key(active_id) if p.get("mode") == "api_key" else ""
            return cp
    return None


def _clean_allowed_sinks(raw) -> list[str]:
    """Normalize an allowed_sinks list from a request body: strings only,
    trimmed, lowercased, de-duped, capped. Rejects junk silently rather
    than persisting it."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw[:64]:
        if isinstance(item, str):
            s = item.strip().lower()
            if s and s not in out:
                out.append(s)
    return out


def get_allowed_sinks(provider_id: str) -> list[str]:
    """The credential-broker host pin for a provider. Unknown provider →
    empty list (fail-closed: the broker then rejects every request)."""
    state = _load_state()
    for p in state.get("providers", []):
        if p.get("id") == provider_id:
            return list(p.get("allowed_sinks", []))
    return []


def add_provider(payload: dict) -> dict:
    """Create a new provider. Body fields: name, kind, mode, base_url, config_dir,
    default_model, api_key (only persisted in keychain if mode=='api_key').
    Returns the public view of the new provider."""
    state = _load_state()
    pid = str(uuid.uuid4())
    mode = payload.get("mode", "subscription")
    if mode not in ("subscription", "api_key"):
        mode = "subscription"
    kind = (payload.get("kind") or "claude").strip()
    runner = _clean_runner(kind, payload.get("runner"))
    base_url = (payload.get("base_url") or "").strip()
    _reject_unsupported_provider_config(kind, mode, runner)
    provider = {
        "id": pid,
        "name": (payload.get("name") or "").strip() or "Provider",
        "kind": kind,
        "mode": mode,
        "base_url": base_url,
        "config_dir": _clean_provider_config_dir(
            kind=kind,
            mode=mode,
            base_url=base_url,
            value=payload.get("config_dir"),
        ),
        "custom_models": list(payload.get("custom_models") or []),
        "default_model": (payload.get("default_model") or "").strip(),
        "runner": runner,
        "default_permission": _clean_default_permission(
            _runtime_kind_for_config(kind, runner),
            payload.get("default_permission"),
        ),
        "suspended": payload.get("suspended") is True,
        "allowed_sinks": _clean_allowed_sinks(payload.get("allowed_sinks")),
        "capabilities": _clean_capabilities(payload.get("capabilities")),
    }
    provider["default_reasoning_effort"] = clean_default_reasoning_effort_for_provider(
        provider, payload.get("default_reasoning_effort")
    )
    if mode == "api_key":
        api_key = payload.get("api_key", "")
        if api_key and api_key != KEEP_SENTINEL:
            _write_api_key(pid, api_key)
    state["providers"].append(provider)
    _save_state(state)
    return _strip(provider)


def update_provider(provider_id: str, payload: dict) -> Optional[dict]:
    """Patch fields on an existing provider. `api_key=KEEP_SENTINEL` preserves
    the existing keychain entry. Pass empty string to clear it."""
    state = _load_state()
    target: Optional[dict] = None
    for p in state.get("providers", []):
        if p.get("id") == provider_id:
            target = p
            break
    if not target:
        return None
    if "name" in payload:
        target["name"] = (payload.get("name") or "").strip() or target.get("name", "")
    if "kind" in payload:
        target["kind"] = (payload.get("kind") or "claude").strip()
    if "mode" in payload and payload["mode"] in ("subscription", "api_key"):
        target["mode"] = payload["mode"]
    if "base_url" in payload:
        target["base_url"] = (payload.get("base_url") or "").strip()
    if "config_dir" in payload:
        target["config_dir"] = _clean_provider_config_dir(
            kind=target.get("kind", "claude"),
            mode=target.get("mode", "subscription"),
            base_url=target.get("base_url", ""),
            value=payload.get("config_dir"),
        )
    if "default_model" in payload:
        target["default_model"] = (payload.get("default_model") or "").strip()
    if "runner" in payload or "kind" in payload:
        target["runner"] = _clean_runner(
            target.get("kind", "claude"),
            payload.get("runner", target.get("runner")),
        )
    target["config_dir"] = _clean_provider_config_dir(
        kind=target.get("kind", "claude"),
        mode=target.get("mode", "subscription"),
        base_url=target.get("base_url", ""),
        value=target.get("config_dir"),
    )
    _reject_unsupported_provider_config(
        target.get("kind", "claude"),
        target.get("mode", "subscription"),
        target.get("runner"),
    )
    if "default_reasoning_effort" in payload:
        target["default_reasoning_effort"] = clean_default_reasoning_effort_for_provider(
            target, payload.get("default_reasoning_effort")
        )
    elif "kind" in payload or "base_url" in payload or "runner" in payload:
        target["default_reasoning_effort"] = clean_default_reasoning_effort_for_provider(
            target, target.get("default_reasoning_effort")
        )
    if "default_permission" in payload:
        target["default_permission"] = _clean_default_permission(
            _runtime_kind_for_provider(target), payload.get("default_permission")
        )
    elif "kind" in payload or "runner" in payload:
        target["default_permission"] = _clean_default_permission(
            _runtime_kind_for_provider(target), target.get("default_permission")
        )
    if "custom_models" in payload and isinstance(payload["custom_models"], list):
        target["custom_models"] = list(payload["custom_models"])
    if "suspended" in payload:
        target["suspended"] = payload.get("suspended") is True
        if target["suspended"] and state.get("default_provider_id") == provider_id:
            state["default_provider_id"] = next(
                (
                    p.get("id")
                    for p in state.get("providers", [])
                    if p.get("id") != provider_id and not _provider_is_suspended(p)
                ),
                None,
            )
    if "allowed_sinks" in payload:
        target["allowed_sinks"] = _clean_allowed_sinks(payload["allowed_sinks"])
    if "capabilities" in payload:
        target["capabilities"] = _clean_capabilities(payload["capabilities"])
    if "api_key" in payload:
        new_key = payload["api_key"]
        if new_key != KEEP_SENTINEL:
            _write_api_key(provider_id, new_key)
    # Monotonic edit counter: spawn-time snapshots (e.g. the Claude
    # handoff eligibility check) compare against it to detect that a
    # live process was configured under a different record.
    target["record_version"] = int(target.get("record_version") or 0) + 1
    _save_state(state)
    # If we just updated the active provider, re-apply env so changes take.
    if state.get("default_provider_id") == provider_id:
        apply_env_vars()
    return _strip(target)


def provider_record_version(provider_id: str) -> Optional[int]:
    """Monotonic edit counter for a provider record — bumped on every
    update_provider. 0 for a never-edited record; None when the provider
    does not exist (callers fail closed)."""
    state = _load_state()
    for p in state.get("providers", []):
        if p.get("id") == provider_id:
            return int(p.get("record_version") or 0)
    return None


def delete_provider(provider_id: str) -> tuple[bool, str]:
    """Returns (deleted, reason). Refuses to delete the active provider —
    the UI should activate another first."""
    state = _load_state()
    if state.get("default_provider_id") == provider_id:
        return False, "default"
    before = len(state.get("providers", []))
    state["providers"] = [p for p in state.get("providers", []) if p.get("id") != provider_id]
    if len(state["providers"]) == before:
        return False, "missing"
    _delete_api_key(provider_id)
    _save_state(state)
    return True, "ok"


def set_default_provider(provider_id: str) -> Optional[dict]:
    state = _load_state()
    target = next((p for p in state.get("providers", []) if p.get("id") == provider_id), None)
    if target is None:
        return None
    if _provider_is_suspended(target):
        raise RuntimeError("provider is suspended")
    state["default_provider_id"] = provider_id
    _save_state(state)
    apply_env_vars()
    return list_providers()


def set_provider_suspended(provider_id: str, suspended: bool) -> Optional[dict]:
    state = _load_state()
    target: Optional[dict] = None
    for p in state.get("providers", []):
        if p.get("id") == provider_id:
            target = p
            break
    if target is None:
        return None
    target["suspended"] = bool(suspended)
    if suspended and state.get("default_provider_id") == provider_id:
        replacement = next(
            (
                p.get("id")
                for p in state.get("providers", [])
                if p.get("id") != provider_id and not _provider_is_suspended(p)
            ),
            None,
        )
        state["default_provider_id"] = replacement
    _save_state(state)
    apply_env_vars()
    return list_providers()


def add_custom_model_to_default(name: str) -> Optional[dict]:
    """Append a custom model to the currently-active provider's list.
    Used by ModelSelector's "+ custom" affordance."""
    state = _load_state()
    active_id = state.get("default_provider_id")
    if not active_id:
        return None
    for p in state.get("providers", []):
        if p.get("id") == active_id:
            cm = list(p.get("custom_models") or [])
            if name and name not in cm:
                cm.append(name)
                p["custom_models"] = cm
                _save_state(state)
            return _strip(p)
    return None


# ----------------------------------------------------------------------------
# Env application — sourced from the active provider
# ----------------------------------------------------------------------------


def apply_env_vars(provider_id: Optional[str] = None) -> None:
    """Mutate os.environ + write engine.env from a provider's settings."""
    active = (
        get_provider_with_key(provider_id)
        if provider_id is not None
        else get_default_provider()
    )
    if not active or _provider_is_suspended(active):
        # No provider (or the selected provider is suspended) — clear any
        # leftover env so we don't leak stale auth into a fresh CLI spawn.
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        _write_engine_env({})
        return

    if active.get("mode") == "api_key":
        api_key = active.get("api_key") or ""
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
            if is_ollama_base_url(active.get("base_url") or ""):
                os.environ["ANTHROPIC_AUTH_TOKEN"] = api_key
            else:
                os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        else:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        base_url = active.get("base_url") or ""
        if base_url:
            os.environ["ANTHROPIC_BASE_URL"] = base_url
        else:
            os.environ.pop("ANTHROPIC_BASE_URL", None)
    else:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        os.environ.pop("ANTHROPIC_BASE_URL", None)

    cfg_dir = active.get("config_dir") or ""
    if _uses_claude_env(active) and cfg_dir:
        os.environ["CLAUDE_CONFIG_DIR"] = _resolved_provider_config_dir(cfg_dir)
    else:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    _write_engine_env(active)


def _write_engine_env(active: dict) -> None:
    lines: list[str] = []
    if active.get("mode") == "api_key":
        if active.get("api_key"):
            lines.append(f"export ANTHROPIC_API_KEY='{active['api_key']}'")
            if is_ollama_base_url(active.get("base_url") or ""):
                lines.append(f"export ANTHROPIC_AUTH_TOKEN='{active['api_key']}'")
            else:
                lines.append("unset ANTHROPIC_AUTH_TOKEN")
        if active.get("base_url"):
            lines.append(f"export ANTHROPIC_BASE_URL='{active['base_url']}'")
    else:
        lines.append("unset ANTHROPIC_API_KEY")
        lines.append("unset ANTHROPIC_AUTH_TOKEN")
        lines.append("unset ANTHROPIC_BASE_URL")
    # Export the active provider's per-account credential dir
    # (CLAUDE_CONFIG_DIR / CODEX_HOME) so a user can `source engine.env`
    # and run the provider's own login against the right account; unset the
    # others so a stale value from a previous source can't leak across.
    cred = provider_credential_env(active)
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        if cred and cred[0] == var:
            lines.append(f"export {var}='{cred[1]}'")
        else:
            lines.append(f"unset {var}")
    _engine_env_path().write_text("\n".join(lines) + "\n", encoding="utf-8")
