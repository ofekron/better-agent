from __future__ import annotations

import re
from typing import Any


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_PROTECTED_KEYS = frozenset({
    "AMP_API_KEY",
    "AMP_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "COMSPEC",
    "CURSOR_API_KEY",
    "DYLD_INSERT_LIBRARIES",
    "GEMINI_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GOOGLE_API_KEY",
    "HOME",
    "LD_PRELOAD",
    "KIMI_API_KEY",
    "KIMI_BASE_URL",
    "NODE_OPTIONS",
    "NODE_PATH",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "PATH",
    "PATHEXT",
    "PYTHONHOME",
    "PYTHONPATH",
    "SHELL",
    "SYSTEMROOT",
    "USERPROFILE",
    "VIRTUAL_ENV",
})
_PROTECTED_PREFIXES = (
    "AGY_",
    "BETTER_AGENT_",
    "BETTER_CLAUDE_",
    "CLAUDE_CODE_",
    "CODEX_",
)
def validate_extra_env(value: Any) -> None:
    if value is None:
        return
    if type(value) is not dict or any(
        type(key) is not str or type(item) is not str
        for key, item in value.items()
    ):
        raise ValueError("extra_env must be a string map or null")
    for key, item in value.items():
        normalized = key.upper()
        if (
            not _ENV_KEY_RE.fullmatch(key)
            or normalized in _PROTECTED_KEYS
            or normalized.startswith(_PROTECTED_PREFIXES)
        ):
            raise ValueError(f"extra_env key is protected: {key}")
        if "\x00" in item or len(item) > 32768:
            raise ValueError(f"extra_env value is invalid: {key}")
