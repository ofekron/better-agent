from __future__ import annotations

import threading
from typing import Any

from json_store import read_json, write_json
from paths import ba_home


DEFAULT_FONT_FAMILY = "system"
DEFAULT_FONT_SIZE = 14
MIN_FONT_SIZE = 11
MAX_FONT_SIZE = 20
DEFAULT_LANGUAGE = "en"
DEFAULT_FIRST_RUN_WIZARD_DONE = False
DEFAULT_VOICE_CLOSE_ON_BACKGROUND = True
MAX_USER_DISPLAY_NAME_LENGTH = 80
SUPPORTED_LANGUAGES = (
    "en", "he", "es", "fr", "de", "pt", "it", "ru",
    "zh", "ja", "ko", "ar", "hi", "nl",
)
APP_PREFERENCE_KEYS = frozenset({
    "user_display_name",
    "language",
    "font_family",
    "font_size",
    "first_run_wizard_done",
    "voice_close_on_background",
})

_LOCK = threading.RLock()


def _path():
    return ba_home() / "app-state" / "user-prefs.json"


def _load() -> dict[str, Any]:
    data = read_json(_path(), {})
    return data if isinstance(data, dict) else {}


def _clean_display_name(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("user_display_name must be a string or null")
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        return None
    if len(cleaned) > MAX_USER_DISPLAY_NAME_LENGTH:
        raise ValueError(
            f"user_display_name must be {MAX_USER_DISPLAY_NAME_LENGTH} characters or fewer"
        )
    return cleaned


def _snapshot(data: dict[str, Any], login_username: str | None = None) -> dict[str, Any]:
    display_name = _clean_display_name(data.get("user_display_name"))
    if display_name is None and isinstance(login_username, str) and login_username.strip():
        display_name = " ".join(login_username.strip().split())
    language = data.get("language")
    font_family = data.get("font_family")
    font_size = data.get("font_size")
    first_run = data.get("first_run_wizard_done")
    voice_close = data.get("voice_close_on_background")
    return {
        "user_display_name": display_name,
        "language": language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE,
        "font_family": (
            font_family
            if font_family in ("system", "serif", "mono", "inter")
            else DEFAULT_FONT_FAMILY
        ),
        "font_size": (
            font_size
            if isinstance(font_size, int)
            and not isinstance(font_size, bool)
            and MIN_FONT_SIZE <= font_size <= MAX_FONT_SIZE
            else DEFAULT_FONT_SIZE
        ),
        "first_run_wizard_done": (
            first_run if isinstance(first_run, bool) else DEFAULT_FIRST_RUN_WIZARD_DONE
        ),
        "voice_close_on_background": (
            voice_close
            if isinstance(voice_close, bool)
            else DEFAULT_VOICE_CLOSE_ON_BACKGROUND
        ),
    }


def get_all(login_username: str | None = None) -> dict[str, Any]:
    with _LOCK:
        return _snapshot(_load(), login_username)


def _validated_updates(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("preferences patch must be an object")
    updates: dict[str, Any] = {}
    if "user_display_name" in body:
        updates["user_display_name"] = _clean_display_name(body["user_display_name"])
    if "language" in body:
        language = body["language"]
        if language not in SUPPORTED_LANGUAGES:
            raise ValueError(f"Invalid language: {language!r}")
        updates["language"] = language
    if "font_family" in body:
        font_family = body["font_family"]
        if font_family not in ("system", "serif", "mono", "inter"):
            raise ValueError("font_family must be system, serif, mono, or inter")
        updates["font_family"] = font_family
    if "font_size" in body:
        font_size = body["font_size"]
        if (
            isinstance(font_size, bool)
            or not isinstance(font_size, int)
            or font_size < MIN_FONT_SIZE
            or font_size > MAX_FONT_SIZE
        ):
            raise ValueError(
                f"font_size must be an integer between {MIN_FONT_SIZE} and {MAX_FONT_SIZE}"
            )
        updates["font_size"] = font_size
    if "first_run_wizard_done" in body:
        first_run = body["first_run_wizard_done"]
        if not isinstance(first_run, bool):
            raise ValueError("first_run_wizard_done must be a boolean")
        updates["first_run_wizard_done"] = first_run
    if "voice_close_on_background" in body:
        voice_close = body["voice_close_on_background"]
        if not isinstance(voice_close, bool):
            raise ValueError("voice_close_on_background must be a boolean")
        updates["voice_close_on_background"] = voice_close
    return updates


def validate_patch(body: dict[str, Any]) -> None:
    _validated_updates(body)


def patch(body: dict[str, Any], login_username: str | None = None) -> dict[str, Any]:
    updates = _validated_updates(body)
    with _LOCK:
        data = _load()
        data.update(updates)
        write_json(_path(), data)
        return _snapshot(data, login_username)
