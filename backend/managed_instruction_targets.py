"""Provider-neutral resolver for managed-instruction target files.

For each configured provider (from ``config_store.list_provider_metadata()``)
this returns the canonical instruction file path(s) the harness writes
managed-instruction blocks into:

- ``global``  scope -> the provider's home instruction file
  (``~/.claude/CLAUDE.md``, ``~/.codex/AGENTS.md``, gemini context file).
- ``project`` scope -> the project-root instruction file
  (``<root>/CLAUDE.md``, ``<root>/AGENTS.md``, ``<root>/<gemini-context>``).

This is a pure path resolver. It does NOT read or write instruction content;
callers own the splice/sweep logic. Per-provider file-format details live here
so no provider-config-sync package coupling remains.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_DEFAULT_CONFIG_DIR = {
    "claude": "~/.claude",
    "gemini": "~/.gemini",
    "codex": "~/.codex",
    "agy": "~/.gemini/antigravity-cli",
}
_GOOGLE_AGENT_KINDS = {"gemini", "agy"}


def _expand_path(raw: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(raw)))).absolute()


def _provider_config_dir(provider: dict) -> Path:
    kind = provider.get("kind", "")
    raw = provider.get("config_dir") or _DEFAULT_CONFIG_DIR.get(kind, "")
    return _expand_path(raw).resolve()


def _codex_home(provider: dict) -> Path:
    if provider.get("config_dir"):
        return _provider_config_dir(provider)
    return _expand_path(os.environ.get("CODEX_HOME") or _DEFAULT_CONFIG_DIR["codex"])


def _read_json_dict(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_basenames(values: object) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    return [
        value
        for value in values
        if isinstance(value, str)
        and value not in {"", ".", ".."}
        and Path(value).name == value
    ]


def _gemini_context_names(
    config_dir: Path | None = None,
    project_root: Path | None = None,
) -> list[str]:
    settings_paths = [(config_dir or _expand_path(_DEFAULT_CONFIG_DIR["gemini"])) / "settings.json"]
    if project_root is not None:
        settings_paths.append(project_root / ".gemini" / "settings.json")
    for settings_path in reversed(settings_paths):
        context = _read_json_dict(settings_path).get("context")
        configured = context.get("fileName") if isinstance(context, dict) else None
        names = _safe_basenames(configured)
        if names:
            return names
    return ["GEMINI.md"]


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def managed_instruction_targets(
    *,
    scope: str,
    project_root: Path | None,
    providers: list[dict],
) -> list[Path]:
    """Canonical instruction file(s) per configured provider for a scope."""
    if scope not in {"global", "project"}:
        raise ValueError(f"invalid scope: {scope}")
    if scope == "project" and not project_root:
        raise ValueError("project scope requires project_root")
    root = _expand_path(project_root).resolve() if project_root else None
    paths: list[Path] = []
    for provider in providers:
        kind = provider.get("kind", "")
        if kind == "claude":
            container = _provider_config_dir(provider) if scope == "global" else root
            paths.append(container / "CLAUDE.md")
        elif kind == "codex":
            container = _codex_home(provider) if scope == "global" else root
            paths.append(container / "AGENTS.md")
        elif kind in _GOOGLE_AGENT_KINDS:
            config_dir = _provider_config_dir(provider)
            names = _gemini_context_names(config_dir, root if scope == "project" else None)
            container = config_dir if scope == "global" else root
            paths.extend(container / name for name in names)
            if scope == "project":
                paths.append(root / "AGENTS.md")
    return _dedupe_paths(paths)
