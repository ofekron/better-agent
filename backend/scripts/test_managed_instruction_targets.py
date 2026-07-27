"""Tests for the provider-neutral managed_instruction_targets resolver.

Proves the resolver returns the expected provider config instruction-file
paths for global and project scope, with NO provider_config_sync import.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_home

_test_home.isolate("ba-test-managed-instruction-targets-")

from managed_instruction_targets import managed_instruction_targets  # noqa: E402


def test_global_scope_returns_claude_codex_gemini_paths() -> None:
    providers = [
        {"kind": "claude", "name": "Claude", "config_dir": "~/.claude"},
        {"kind": "codex", "name": "Codex", "config_dir": "~/.codex"},
        {"kind": "gemini", "name": "Gemini", "config_dir": "~/.gemini"},
    ]
    paths = managed_instruction_targets(scope="global", project_root=None, providers=providers)
    names = {p.name for p in paths}
    assert "CLAUDE.md" in names, paths
    assert "AGENTS.md" in names, paths
    # Default gemini context filename when no settings.json is present.
    assert "GEMINI.md" in names, paths


def test_global_scope_agy_kind_uses_gemini_dir() -> None:
    providers = [{"kind": "agy", "name": "AGY", "config_dir": "~/.gemini/antigravity-cli"}]
    paths = managed_instruction_targets(scope="global", project_root=None, providers=providers)
    assert paths and paths[0].name == "GEMINI.md", paths


def test_project_scope_returns_root_relative_paths() -> None:
    with tempfile.TemporaryDirectory(prefix="mit-project-") as raw:
        root = Path(raw)
        providers = [
            {"kind": "claude", "name": "Claude", "config_dir": "~/.claude"},
            {"kind": "codex", "name": "Codex", "config_dir": "~/.codex"},
            {"kind": "gemini", "name": "Gemini", "config_dir": "~/.gemini"},
        ]
        paths = managed_instruction_targets(scope="project", project_root=root, providers=providers)
        resolved_parents = {str(p.parent) for p in paths}
        assert str(root.resolve()) in resolved_parents, (root, paths)
        names = {p.name for p in paths}
        assert "CLAUDE.md" in names
        assert "AGENTS.md" in names
        assert "GEMINI.md" in names


def test_project_scope_gemini_also_includes_agents_md() -> None:
    with tempfile.TemporaryDirectory(prefix="mit-project-agents-") as raw:
        root = Path(raw)
        providers = [{"kind": "gemini", "name": "Gemini", "config_dir": "~/.gemini"}]
        paths = managed_instruction_targets(scope="project", project_root=root, providers=providers)
        names = {p.name for p in paths}
        assert "AGENTS.md" in names, paths


def test_gemini_settings_override_filename() -> None:
    with tempfile.TemporaryDirectory(prefix="mit-gemini-override-") as raw:
        root = Path(raw)
        gemini_home = root / "gemini-home"
        gemini_home.mkdir()
        (gemini_home / "settings.json").write_text(
            '{"context": {"fileName": ["CUSTOM.md", "EXTRA.md"]}}', encoding="utf-8"
        )
        providers = [{"kind": "gemini", "name": "Gemini", "config_dir": str(gemini_home)}]
        paths = managed_instruction_targets(scope="global", project_root=None, providers=providers)
        names = {p.name for p in paths}
        assert names == {"CUSTOM.md", "EXTRA.md"}, paths


def test_invalid_scope_rejected() -> None:
    try:
        managed_instruction_targets(scope="session", project_root=None, providers=[])
    except ValueError:
        return
    raise AssertionError("invalid scope was accepted")


def test_project_scope_requires_project_root() -> None:
    try:
        managed_instruction_targets(scope="project", project_root=None, providers=[])
    except ValueError:
        return
    raise AssertionError("missing project_root was accepted")


def test_codex_home_env_override() -> None:
    with tempfile.TemporaryDirectory(prefix="mit-codex-env-") as raw:
        custom = Path(raw) / "codex-home"
        os.environ["CODEX_HOME"] = str(custom)
        try:
            providers = [{"kind": "codex", "name": "Codex", "config_dir": ""}]
            paths = managed_instruction_targets(scope="global", project_root=None, providers=providers)
            assert len(paths) == 1 and paths[0].name == "AGENTS.md", paths
            assert paths[0].parent.name == "codex-home", paths
        finally:
            os.environ.pop("CODEX_HOME", None)


def test_no_provider_config_sync_import() -> None:
    import sys as _sys
    assert "provider_config_sync_backend" not in _sys.modules, \
        "managed_instruction_targets must not pull in provider_config_sync_backend"


if __name__ == "__main__":
    test_global_scope_returns_claude_codex_gemini_paths()
    test_global_scope_agy_kind_uses_gemini_dir()
    test_project_scope_returns_root_relative_paths()
    test_project_scope_gemini_also_includes_agents_md()
    test_gemini_settings_override_filename()
    test_invalid_scope_rejected()
    test_project_scope_requires_project_root()
    test_codex_home_env_override()
    test_no_provider_config_sync_import()
    print("all managed_instruction_targets tests passed")
