"""User-level preferences (persisted to disk, independent of sessions).

Storage:
  ~/.better-claude/user_prefs.json

Shape:
  {
    "send_mode": "queue" | "interrupt",
    ...
  }
"""

import logging
import threading
from typing import Literal

from json_store import read_json, write_json
from paths import bc_home

logger = logging.getLogger(__name__)

SendMode = Literal["queue", "interrupt"]
ContextStrategy = Literal["native_compact", "continuation"]
FontFamily = Literal["system", "serif", "mono", "inter"]
NetworkBindAddress = Literal["127.0.0.1", "0.0.0.0"]
SessionSort = Literal["updated_at", "last_user_prompt_at", "last_opened_at"]
SESSION_SORT_VALUES: tuple[SessionSort, ...] = (
    "updated_at", "last_user_prompt_at", "last_opened_at",
)
DEFAULT_SESSION_SORT: SessionSort = "updated_at"
SessionTabsSort = Literal["updated_at", "last_user_prompt_at", "last_opened_at"]
SESSION_TABS_SORT_VALUES: tuple[SessionTabsSort, ...] = (
    "updated_at", "last_user_prompt_at", "last_opened_at",
)
DEFAULT_SESSION_TABS_SORT: SessionTabsSort = "last_opened_at"
DEFAULT_SESSION_STATUS_SORT = False
DEFAULT_SESSION_TABS_VISIBLE = True
DEFAULT_VOICE_CLOSE_ON_BACKGROUND = True
DEFAULT_SEND_MODE: SendMode = "queue"
DEFAULT_CROSS_SESSION_DELEGATE_AUTO = False
DEFAULT_CONTEXT_STRATEGY: ContextStrategy = "native_compact"
DEFAULT_SESSION_AUTO_DELETE_DAYS = None
DEFAULT_FONT_FAMILY: FontFamily = "system"
DEFAULT_FONT_SIZE = 14
MIN_FONT_SIZE = 11
MAX_FONT_SIZE = 20
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES: tuple[str, ...] = (
    "en", "he", "es", "fr", "de", "pt", "it", "ru",
    "zh", "ja", "ko", "ar", "hi", "nl",
)
DEFAULT_FIRST_RUN_WIZARD_DONE = False
DEFAULT_FOLDER_VIEW_ENABLED = True
DEFAULT_NETWORK_BIND_ADDRESS: NetworkBindAddress = "127.0.0.1"
# Auto-restart the backend+frontend (via the run.sh supervisor) every time
# the system transitions from busy to idle. Off by default — restarting a
# running server is opt-in.
DEFAULT_AUTO_RESTART_ON_IDLE = False
DEFAULT_SHORTCUT_RESPONSES = [
    "TLDR",
    "Didn't read, but I trust you go ahead",
    "/Adv",
    "Confirmed Go ahead",
]
_PREFS_CACHE_LOCK = threading.Lock()
_PREFS_CACHE: tuple[tuple[int, int], dict] | None = None
_PREFS_PATH = bc_home() / "user_prefs.json"


def _prefs_path():
    return _PREFS_PATH


def _load() -> dict:
    global _PREFS_CACHE
    path = _prefs_path()
    try:
        st = path.stat()
        fingerprint = (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        fingerprint = (0, 0)
    with _PREFS_CACHE_LOCK:
        cached = _PREFS_CACHE
        if cached is not None and cached[0] == fingerprint:
            return dict(cached[1])
    data = read_json(path, {})
    with _PREFS_CACHE_LOCK:
        _PREFS_CACHE = (fingerprint, dict(data))
    return dict(data)


def _save(data: dict) -> None:
    global _PREFS_CACHE
    path = _prefs_path()
    write_json(path, data)
    try:
        st = path.stat()
        fingerprint = (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        fingerprint = (0, 0)
    with _PREFS_CACHE_LOCK:
        _PREFS_CACHE = (fingerprint, dict(data))


def _bool_pref(prefs: dict, key: str, default: bool) -> bool:
    val = prefs.get(key, default)
    return val if isinstance(val, bool) else default


def _choice_pref(prefs: dict, key: str, default: str, choices: tuple[str, ...]) -> str:
    val = prefs.get(key, default)
    return val if val in choices else default


def _bounded_int_pref(prefs: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    val = prefs.get(key, default)
    if isinstance(val, bool) or not isinstance(val, int):
        return default
    if val < minimum or val > maximum:
        return default
    return val


def _optional_positive_int_pref(prefs: dict, key: str, default: int | None) -> int | None:
    val = prefs.get(key, default)
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, int) or val < 1:
        return default
    return val


def get_send_mode() -> SendMode:
    prefs = _load()
    mode = prefs.get("send_mode", DEFAULT_SEND_MODE)
    if mode not in ("queue", "interrupt"):
        return DEFAULT_SEND_MODE
    return mode


def set_send_mode(mode: SendMode) -> SendMode:
    if mode not in ("queue", "interrupt"):
        raise ValueError(f"Invalid send_mode: {mode!r}")
    prefs = _load()
    prefs["send_mode"] = mode
    _save(prefs)
    return mode


def get_language() -> str:
    prefs = _load()
    lang = prefs.get("language", DEFAULT_LANGUAGE)
    if lang not in SUPPORTED_LANGUAGES:
        return DEFAULT_LANGUAGE
    return lang


def set_language(lang: str) -> str:
    if lang not in SUPPORTED_LANGUAGES:
        raise ValueError(f"Invalid language: {lang!r}")
    prefs = _load()
    prefs["language"] = lang
    _save(prefs)
    return lang


def get_shortcut_responses() -> list[str]:
    prefs = _load()
    val = prefs.get("shortcut_responses", DEFAULT_SHORTCUT_RESPONSES)
    if not isinstance(val, list) or not all(isinstance(s, str) for s in val):
        return list(DEFAULT_SHORTCUT_RESPONSES)
    return val


def set_shortcut_responses(shortcuts: list[str]) -> list[str]:
    if not isinstance(shortcuts, list) or not all(isinstance(s, str) for s in shortcuts):
        raise ValueError("shortcut_responses must be a list of strings")
    prefs = _load()
    prefs["shortcut_responses"] = shortcuts
    _save(prefs)
    return shortcuts


def get_cross_session_delegate_auto() -> bool:
    """Whether `delegate_to_session` may run with `approval:"auto"` (no
    picker). Default OFF — fail closed: when this is False, every
    cross-session delegation is gated through the session picker."""
    prefs = _load()
    val = prefs.get("cross_session_delegate_auto", DEFAULT_CROSS_SESSION_DELEGATE_AUTO)
    if not isinstance(val, bool):
        return DEFAULT_CROSS_SESSION_DELEGATE_AUTO
    return val


def set_cross_session_delegate_auto(enabled: bool) -> bool:
    if not isinstance(enabled, bool):
        raise ValueError(f"Invalid cross_session_delegate_auto: {enabled!r}")
    prefs = _load()
    prefs["cross_session_delegate_auto"] = enabled
    _save(prefs)
    return enabled


def get_session_auto_delete_days() -> int | None:
    return _optional_positive_int_pref(
        _load(),
        "session_auto_delete_days",
        DEFAULT_SESSION_AUTO_DELETE_DAYS,
    )


def set_session_auto_delete_days(days: int | None) -> int | None:
    if days is not None and (
        isinstance(days, bool) or not isinstance(days, int) or days < 1
    ):
        raise ValueError(f"Invalid session_auto_delete_days: {days!r}")
    prefs = _load()
    prefs["session_auto_delete_days"] = days
    _save(prefs)
    return days


def get_font_family() -> FontFamily:
    return _choice_pref(
        _load(),
        "font_family",
        DEFAULT_FONT_FAMILY,
        ("system", "serif", "mono", "inter"),
    )


def set_font_family(font_family: FontFamily) -> FontFamily:
    if font_family not in ("system", "serif", "mono", "inter"):
        raise ValueError(f"Invalid font_family: {font_family!r}")
    prefs = _load()
    prefs["font_family"] = font_family
    _save(prefs)
    return font_family


def get_font_size() -> int:
    return _bounded_int_pref(
        _load(),
        "font_size",
        DEFAULT_FONT_SIZE,
        MIN_FONT_SIZE,
        MAX_FONT_SIZE,
    )


def set_font_size(font_size: int) -> int:
    if (
        isinstance(font_size, bool)
        or not isinstance(font_size, int)
        or font_size < MIN_FONT_SIZE
        or font_size > MAX_FONT_SIZE
    ):
        raise ValueError(f"Invalid font_size: {font_size!r}")
    prefs = _load()
    prefs["font_size"] = font_size
    _save(prefs)
    return font_size


def get_first_run_wizard_done() -> bool:
    return _bool_pref(_load(), "first_run_wizard_done", DEFAULT_FIRST_RUN_WIZARD_DONE)


def set_first_run_wizard_done(done: bool) -> bool:
    if not isinstance(done, bool):
        raise ValueError(f"Invalid first_run_wizard_done: {done!r}")
    prefs = _load()
    prefs["first_run_wizard_done"] = done
    _save(prefs)
    return done


def get_network_bind_address() -> NetworkBindAddress:
    return _choice_pref(
        _load(),
        "network_bind_address",
        DEFAULT_NETWORK_BIND_ADDRESS,
        ("127.0.0.1", "0.0.0.0"),
    )


def set_network_bind_address(address: NetworkBindAddress) -> NetworkBindAddress:
    if address not in ("127.0.0.1", "0.0.0.0"):
        raise ValueError(f"Invalid network_bind_address: {address!r}")
    prefs = _load()
    prefs["network_bind_address"] = address
    _save(prefs)
    return address


def get_folder_view_enabled() -> bool:
    """Whether the session list groups sessions into folders (True) or
    shows a flat list (False). Drives the backend sort and the frontend
    tree-vs-flat render."""
    return _bool_pref(_load(), "folder_view_enabled", DEFAULT_FOLDER_VIEW_ENABLED)


def set_folder_view_enabled(enabled: bool) -> bool:
    if not isinstance(enabled, bool):
        raise ValueError(f"Invalid folder_view_enabled: {enabled!r}")
    prefs = _load()
    prefs["folder_view_enabled"] = enabled
    _save(prefs)
    return enabled


def get_session_sort() -> SessionSort:
    """Which timestamp the session list sorts by: last modification
    (`updated_at`) or last user prompt (`last_user_prompt_at`)."""
    return _choice_pref(_load(), "session_sort", DEFAULT_SESSION_SORT, SESSION_SORT_VALUES)


def set_session_sort(value: str) -> SessionSort:
    if value not in SESSION_SORT_VALUES:
        raise ValueError(f"Invalid session_sort: {value!r}")
    prefs = _load()
    prefs["session_sort"] = value
    _save(prefs)
    return value


def get_session_status_sort() -> bool:
    """Whether the sidebar session list groups by status bucket (running >
    needs-decision > has-new > all-tasks-done) as the strongest key (below
    empty-new + pinned), with the chosen timestamp as the tie-break."""
    return _bool_pref(_load(), "session_status_sort", DEFAULT_SESSION_STATUS_SORT)


def set_session_status_sort(enabled: bool) -> bool:
    if not isinstance(enabled, bool):
        raise ValueError(f"Invalid session_status_sort: {enabled!r}")
    prefs = _load()
    prefs["session_status_sort"] = enabled
    _save(prefs)
    return enabled


def get_session_tabs_sort() -> SessionTabsSort:
    """Which timestamp the open-session tabs bar sorts by (descending):
    last modification, last user prompt, or last opened on a client."""
    return _choice_pref(
        _load(),
        "sessions_tabs_sort",
        DEFAULT_SESSION_TABS_SORT,
        SESSION_TABS_SORT_VALUES,
    )


def set_session_tabs_sort(value: str) -> SessionTabsSort:
    if value not in SESSION_TABS_SORT_VALUES:
        raise ValueError(f"Invalid sessions_tabs_sort: {value!r}")
    prefs = _load()
    prefs["sessions_tabs_sort"] = value
    _save(prefs)
    return value


def get_session_tabs_visible() -> bool:
    return _bool_pref(_load(), "sessions_tabs_visible", DEFAULT_SESSION_TABS_VISIBLE)


def set_session_tabs_visible(enabled: bool) -> bool:
    if not isinstance(enabled, bool):
        raise ValueError(f"Invalid sessions_tabs_visible: {enabled!r}")
    prefs = _load()
    prefs["sessions_tabs_visible"] = enabled
    _save(prefs)
    return enabled


def get_voice_close_on_background() -> bool:
    """Whether vocal mode auto-closes when the app goes to the background.
    Default ON: the mic stops listening and vocal mode disables itself on
    visibility loss, so the user does not need to remember to turn it off."""
    return _bool_pref(
        _load(),
        "voice_close_on_background",
        DEFAULT_VOICE_CLOSE_ON_BACKGROUND,
    )


def set_voice_close_on_background(enabled: bool) -> bool:
    if not isinstance(enabled, bool):
        raise ValueError(f"Invalid voice_close_on_background: {enabled!r}")
    prefs = _load()
    prefs["voice_close_on_background"] = enabled
    _save(prefs)
    return enabled


def get_last_models() -> dict:
    """Map of provider_id -> last model the user chose for it."""
    prefs = _load()
    val = prefs.get("last_model_by_provider", {})
    if not isinstance(val, dict):
        return {}
    return {
        k: v
        for k, v in val.items()
        if isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
    }


def set_last_model(provider_id: str, model: str) -> bool:
    """Record the last model chosen for a provider. Returns True if the
    stored value changed."""
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError(f"Invalid provider_id: {provider_id!r}")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"Invalid model: {model!r}")
    prefs = _load()
    current = get_last_models()
    if current.get(provider_id) == model:
        return False
    current[provider_id] = model
    prefs["last_model_by_provider"] = current
    _save(prefs)
    return True


def get_last_reasoning_efforts() -> dict:
    prefs = _load()
    val = prefs.get("last_reasoning_effort_by_provider", {})
    if not isinstance(val, dict):
        return {}
    return {
        k: v
        for k, v in val.items()
        if isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
    }


def set_last_reasoning_effort(provider_id: str, reasoning_effort: str) -> bool:
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError(f"Invalid provider_id: {provider_id!r}")
    if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
        raise ValueError(f"Invalid reasoning_effort: {reasoning_effort!r}")
    prefs = _load()
    current = get_last_reasoning_efforts()
    if current.get(provider_id) == reasoning_effort:
        return False
    current[provider_id] = reasoning_effort
    prefs["last_reasoning_effort_by_provider"] = current
    _save(prefs)
    return True


def get_auto_restart_on_idle() -> bool:
    """Whether the backend auto-fires a supervisor restart every time the
    system goes idle after work (to pick up code changes). Default OFF."""
    return _bool_pref(_load(), "auto_restart_on_idle", DEFAULT_AUTO_RESTART_ON_IDLE)


def set_auto_restart_on_idle(enabled: bool) -> bool:
    if not isinstance(enabled, bool):
        raise ValueError(f"Invalid auto_restart_on_idle: {enabled!r}")
    prefs = _load()
    prefs["auto_restart_on_idle"] = enabled
    _save(prefs)
    return enabled


def get_all() -> dict:
    prefs = _load()
    return {
        "send_mode": prefs.get("send_mode", DEFAULT_SEND_MODE),
        "language": prefs.get("language", DEFAULT_LANGUAGE),
        "shortcut_responses": prefs.get("shortcut_responses", DEFAULT_SHORTCUT_RESPONSES),
        "cross_session_delegate_auto": prefs.get(
            "cross_session_delegate_auto", DEFAULT_CROSS_SESSION_DELEGATE_AUTO
        ),
        "context_strategy": prefs.get("context_strategy", DEFAULT_CONTEXT_STRATEGY),
        "session_auto_delete_days": _optional_positive_int_pref(
            prefs,
            "session_auto_delete_days",
            DEFAULT_SESSION_AUTO_DELETE_DAYS,
        ),
        "font_family": _choice_pref(
            prefs,
            "font_family",
            DEFAULT_FONT_FAMILY,
            ("system", "serif", "mono", "inter"),
        ),
        "font_size": _bounded_int_pref(
            prefs,
            "font_size",
            DEFAULT_FONT_SIZE,
            MIN_FONT_SIZE,
            MAX_FONT_SIZE,
        ),
        "first_run_wizard_done": _bool_pref(
            prefs,
            "first_run_wizard_done",
            DEFAULT_FIRST_RUN_WIZARD_DONE,
        ),
        "network_bind_address": _choice_pref(
            prefs,
            "network_bind_address",
            DEFAULT_NETWORK_BIND_ADDRESS,
            ("127.0.0.1", "0.0.0.0"),
        ),
        "folder_view_enabled": _bool_pref(
            prefs,
            "folder_view_enabled",
            DEFAULT_FOLDER_VIEW_ENABLED,
        ),
        "session_sort": _choice_pref(
            prefs,
            "session_sort",
            DEFAULT_SESSION_SORT,
            SESSION_SORT_VALUES,
        ),
        "session_status_sort": _bool_pref(
            prefs,
            "session_status_sort",
            DEFAULT_SESSION_STATUS_SORT,
        ),
        "sessions_tabs_sort": _choice_pref(
            prefs,
            "sessions_tabs_sort",
            DEFAULT_SESSION_TABS_SORT,
            SESSION_TABS_SORT_VALUES,
        ),
        "sessions_tabs_visible": _bool_pref(
            prefs,
            "sessions_tabs_visible",
            DEFAULT_SESSION_TABS_VISIBLE,
        ),
        "voice_close_on_background": _bool_pref(
            prefs,
            "voice_close_on_background",
            DEFAULT_VOICE_CLOSE_ON_BACKGROUND,
        ),
        "auto_restart_on_idle": _bool_pref(
            prefs,
            "auto_restart_on_idle",
            DEFAULT_AUTO_RESTART_ON_IDLE,
        ),
    }


def get_context_strategy() -> ContextStrategy:
    prefs = _load()
    val = prefs.get("context_strategy", DEFAULT_CONTEXT_STRATEGY)
    if val not in ("native_compact", "continuation"):
        return DEFAULT_CONTEXT_STRATEGY
    return val


def set_context_strategy(strategy: ContextStrategy) -> ContextStrategy:
    if strategy not in ("native_compact", "continuation"):
        raise ValueError(f"Invalid context_strategy: {strategy!r}")
    prefs = _load()
    prefs["context_strategy"] = strategy
    _save(prefs)
    return strategy
