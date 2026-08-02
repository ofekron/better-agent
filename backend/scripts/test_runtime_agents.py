"""Unit coverage for `runtime_agents` — projection of active extensions'
native agent-definition files (Claude `.claude/agents/*`, Codex equivalent)
into a per-run provider config dir.

Focuses on the security-critical target-replacement logic: a pre-existing
target that is a symlink must be UNLINKED, never written through, so an
extension-supplied path can never clobber a file in the real home via a
planted link. Also covers the provider/integration gates, the
`_discover_agents` defensive try/except, and the graceful no-op for
providers without a native subagent surface.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

_TEST_HOME = _test_home.TestHome.acquire("bc-test-runtime-agents-")

import installation_profile  # noqa: E402
import runtime_agents  # noqa: E402


def _enable_integrations(monkeypatch, enabled: bool) -> None:
    # runtime_agents holds the same `installation_profile` module object, so
    # patching the attribute once on the shared module is visible to it.
    monkeypatch.setattr(installation_profile, "integrations_enabled", lambda: enabled)


def _stub_extensions(monkeypatch, entries_fn) -> None:
    """Inject a lightweight `extension_store` into sys.modules so
    `_discover_agents`'s lazy `import extension_store` resolves to a
    controllable stub instead of the real heavy module."""
    monkeypatch.setitem(
        sys.modules, "extension_store",
        types.SimpleNamespace(runtime_agent_entries=entries_fn),
    )


# --- _discover_agents defensive branches -------------------------------------

def test_discover_agents_import_failure_is_empty(monkeypatch):
    # sys.modules[name] = None makes `import name` raise ModuleNotFoundError.
    monkeypatch.setitem(sys.modules, "extension_store", None)
    assert runtime_agents._discover_agents() == []


def test_discover_agents_entries_raise_is_empty(monkeypatch):
    def boom():
        raise RuntimeError("extension store blew up")
    _stub_extensions(monkeypatch, boom)
    assert runtime_agents._discover_agents() == []


def test_discover_agents_returns_entries(monkeypatch):
    entries = [{"claude": "/x/a.md"}, {"codex": "/y/b.md"}]
    _stub_extensions(monkeypatch, lambda: entries)
    assert runtime_agents._discover_agents() == entries


# --- has_runtime_agents gates ------------------------------------------------

def test_has_runtime_agents_bare_config_short_circuits_false(monkeypatch):
    _enable_integrations(monkeypatch, True)
    _stub_extensions(monkeypatch, lambda: [{"claude": "/x/a.md"}])
    assert runtime_agents.has_runtime_agents("claude", bare_config=True) is False


def test_has_runtime_agents_integrations_disabled_false(monkeypatch):
    _enable_integrations(monkeypatch, False)
    _stub_extensions(monkeypatch, lambda: [{"claude": "/x/a.md"}])
    assert runtime_agents.has_runtime_agents("claude") is False


def test_has_runtime_agents_unsupported_provider_false(monkeypatch):
    _enable_integrations(monkeypatch, True)
    # gemini/agy have no native subagent surface.
    _stub_extensions(monkeypatch, lambda: [{"claude": "/x/a.md"}])
    assert runtime_agents.has_runtime_agents("gemini") is False


def test_has_runtime_agents_no_matching_entry_false(monkeypatch):
    _enable_integrations(monkeypatch, True)
    _stub_extensions(monkeypatch, lambda: [{"codex": "/x/a.md"}])
    assert runtime_agents.has_runtime_agents("claude") is False


def test_has_runtime_agents_matching_entry_true(monkeypatch):
    _enable_integrations(monkeypatch, True)
    _stub_extensions(monkeypatch, lambda: [{"codex": "/x"}, {"claude": "/y/b.md"}])
    assert runtime_agents.has_runtime_agents("claude") is True


# --- materialize_runtime_agents gates ---------------------------------------

def test_materialize_bare_config_noop(monkeypatch, tmp_path):
    _enable_integrations(monkeypatch, True)
    _stub_extensions(monkeypatch, lambda: [{"claude": "/x/a.md"}])
    assert runtime_agents.materialize_runtime_agents(tmp_path, "claude", bare_config=True) == 0


def test_materialize_integrations_disabled_noop(monkeypatch, tmp_path):
    _enable_integrations(monkeypatch, False)
    _stub_extensions(monkeypatch, lambda: [{"claude": "/x/a.md"}])
    assert runtime_agents.materialize_runtime_agents(tmp_path, "claude") == 0


def test_materialize_unsupported_provider_noop(monkeypatch, tmp_path):
    _enable_integrations(monkeypatch, True)
    _stub_extensions(monkeypatch, lambda: [{"gemini": "/x/a.md"}])
    assert runtime_agents.materialize_runtime_agents(tmp_path, "gemini") == 0


# --- materialize copy semantics ---------------------------------------------

def test_materialize_copies_valid_entries_and_creates_nested_root(monkeypatch, tmp_path):
    _enable_integrations(monkeypatch, True)
    src_a = tmp_path / "src" / "claude_a.md"
    src_a.parent.mkdir(parents=True)
    src_a.write_text("agent A body", encoding="utf-8")
    src_b = tmp_path / "src" / "claude_b.md"
    src_b.write_text("agent B body", encoding="utf-8")
    _stub_extensions(monkeypatch, lambda: [
        {"claude": str(src_a)},
        {"codex": str(tmp_path / "src" / "codex.md")},  # wrong provider -> skip
        {"claude": str(tmp_path / "missing.md")},        # absent source -> skip
        {"claude": str(src_b)},
    ])
    # Non-existent nested root — must be created (parents=True, exist_ok=True).
    root = tmp_path / "run" / "config" / "agents"
    count = runtime_agents.materialize_runtime_agents(root, "claude")
    assert count == 2
    assert (root / "claude_a.md").read_text(encoding="utf-8") == "agent A body"
    assert (root / "claude_b.md").read_text(encoding="utf-8") == "agent B body"


def test_materialize_overwrites_existing_regular_target(monkeypatch, tmp_path):
    _enable_integrations(monkeypatch, True)
    src = tmp_path / "new.md"
    src.write_text("fresh", encoding="utf-8")
    root = tmp_path / "agents"
    root.mkdir()
    (root / "new.md").write_text("stale", encoding="utf-8")
    _stub_extensions(monkeypatch, lambda: [{"claude": str(src)}])
    assert runtime_agents.materialize_runtime_agents(root, "claude") == 1
    assert (root / "new.md").read_text(encoding="utf-8") == "fresh"


def test_materialize_replaces_directory_target(monkeypatch, tmp_path):
    _enable_integrations(monkeypatch, True)
    src = tmp_path / "d.md"
    src.write_text("dir-replaced", encoding="utf-8")
    root = tmp_path / "agents"
    target = root / "d.md"
    target.mkdir(parents=True)  # target is a real directory, not a symlink
    (target / "junk").write_text("x", encoding="utf-8")
    _stub_extensions(monkeypatch, lambda: [{"claude": str(src)}])
    assert runtime_agents.materialize_runtime_agents(root, "claude") == 1
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "dir-replaced"


def test_materialize_unlinks_symlink_target_without_writing_through(monkeypatch, tmp_path):
    """Security property: a pre-existing target that is a symlink must be
    UNLINKED and replaced with a fresh copy — the copy must never be written
    THROUGH the link into the link's victim. A planted symlink cannot reach
    into the real home or any other file."""
    _enable_integrations(monkeypatch, True)
    src = tmp_path / "ext.md"
    src.write_text("extension-content", encoding="utf-8")

    # The victim the planted symlink points at.
    victim = tmp_path / "victim" / "secret.md"
    victim.parent.mkdir(parents=True)
    victim.write_text("MUST-NOT-CHANGE", encoding="utf-8")

    root = tmp_path / "agents"
    root.mkdir()
    # Plant a symlink named like the source file, pointing at the victim.
    planted = root / "ext.md"
    os.symlink(victim, planted)

    _stub_extensions(monkeypatch, lambda: [{"claude": str(src)}])
    assert runtime_agents.materialize_runtime_agents(root, "claude") == 1

    # Target is now a real file holding the extension content...
    assert not planted.is_symlink()
    assert planted.read_text(encoding="utf-8") == "extension-content"
    # ...and the victim was never written through the link.
    assert victim.read_text(encoding="utf-8") == "MUST-NOT-CHANGE"


def test_materialize_skips_entry_without_provider_key(monkeypatch, tmp_path):
    _enable_integrations(monkeypatch, True)
    src = tmp_path / "c.md"
    src.write_text("x", encoding="utf-8")
    _stub_extensions(monkeypatch, lambda: [{"codex": str(src)}])  # no "claude" key
    root = tmp_path / "agents"
    assert runtime_agents.materialize_runtime_agents(root, "claude") == 0
    # root is created but nothing copied into it.
    assert list(root.iterdir()) == []


def test_materialize_skips_absent_source_file(monkeypatch, tmp_path):
    _enable_integrations(monkeypatch, True)
    _stub_extensions(monkeypatch, lambda: [{"claude": str(tmp_path / "nope.md")}])
    root = tmp_path / "agents"
    assert runtime_agents.materialize_runtime_agents(root, "claude") == 0
