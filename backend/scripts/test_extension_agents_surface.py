"""Locks the `agents` extension surface: manifest validation accepts a per-
provider subagent declaration and rejects malformed ones, and the declared
agent materializes into each provider's native surface via runtime_agents
(claude/codex) while no-oping for providers without one (gemini).

Run: backend/.venv/bin/python backend/scripts/test_extension_agents_surface.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import paths  # noqa: E402

_TEST_HOME = paths.engage_test_home(tempfile.mkdtemp(prefix="ba-agents-surface-"))

import _test_installation  # noqa: E402
import installation_profile  # noqa: E402

_test_installation.activate(Path(_TEST_HOME), provider="claude")
installation_profile.capture_active_capabilities()

import extension_store  # noqa: E402
import runtime_agents  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def _rejects(manifest, needle: str) -> None:
    try:
        extension_store.validate_manifest(manifest)
    except extension_store.ExtensionError as exc:
        assert needle in str(exc), f"expected {needle!r} in {exc}"
        print(f"PASS rejects: {needle}")
        return
    raise AssertionError(f"manifest unexpectedly valid (wanted {needle!r})")


def _agents_manifest(agents: list[dict]) -> dict:
    return {
        "kind": extension_store.MANIFEST_KIND,
        "id": "test.agents-surface",
        "name": "Agents Surface Test",
        "version": "0.1.0",
        "surfaces": ["agents"],
        "entrypoints": {"agents": agents},
        "permissions": {},
        "marketplace": {},
    }


AGENT = {
    "name": "relevant-context-search",
    "providers": {"claude": "agents/claude/relevant-context-search.md",
                  "codex": "agents/codex/relevant-context-search.toml"},
}

# Surface is allowed.
validated = extension_store.validate_manifest(_agents_manifest([AGENT]))
check("agents" in extension_store._ALLOWED_SURFACES, "agents is an allowed surface")
check(validated["entrypoints"]["agents"] == [AGENT], "valid agent entry round-trips")

# Shape validation.
check(extension_store.validate_manifest(_agents_manifest([]))["entrypoints"]["agents"] == [],
      "empty agents list is valid")
_rejects({**_agents_manifest([AGENT]), "entrypoints": {"agents": {"name": "x"}}}, "must be a list")
_rejects(_agents_manifest([{"name": "my-agent"}]), "providers must be a non-empty object")
_rejects(
    _agents_manifest([{"name": "my-agent", "providers": {"gemini": "g.md"}}]),
    "unsupported provider",
)
_rejects(
    _agents_manifest([{"name": "Bad Name", "providers": {"claude": "c.md"}}]),
    "invalid characters",
)
_rejects(_agents_manifest([AGENT, AGENT]), "duplicate")
_rejects(
    _agents_manifest([{"name": "my-agent", "providers": {"claude": "../escape.md"}}]),
    "safe relative path",
)

# --- end-to-end materialization ------------------------------------------
PACKAGE = Path(_TEST_HOME) / "fixtures" / "test.agents-install"
CLAUDE_SRC = PACKAGE / "agents" / "claude" / "relevant-context-search.md"
CODEX_SRC = PACKAGE / "agents" / "codex" / "relevant-context-search.toml"
CLAUDE_SRC.parent.mkdir(parents=True, exist_ok=True)
CODEX_SRC.parent.mkdir(parents=True, exist_ok=True)
CLAUDE_SRC.write_text("---\nname: relevant-context-search\n---\nbody\n", encoding="utf-8")
CODEX_SRC.write_text('name = "relevant-context-search"\n', encoding="utf-8")
manifest = {
    **_agents_manifest([AGENT]),
    "id": "test.agents-install",
}
(PACKAGE / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
extension_store._install_from_package_dir(  # type: ignore[attr-defined]
    package_dir=PACKAGE,
    source={"type": "better_agent_local", "repo_url": str(PACKAGE.parent),
            "extension_path": PACKAGE.name, "ref": "", "commit_sha": "agents-install"},
    persist=True,
    force_enabled=True,
)

entries = extension_store.runtime_agent_entries()
check(len(entries) == 1 and entries[0]["name"] == "relevant-context-search", "runtime_agent_entries resolves the agent")
check(entries[0].get("claude", "").endswith("agents/claude/relevant-context-search.md")
      and Path(entries[0]["claude"]).is_file(), "claude source resolved to install root")
check(entries[0].get("codex", "").endswith("agents/codex/relevant-context-search.toml")
      and Path(entries[0]["codex"]).is_file(), "codex source resolved to install root")

out = Path(_TEST_HOME) / "out"
n_claude = runtime_agents.materialize_runtime_agents(out / "claude", "claude")
check(n_claude == 1, "claude materializes one agent file")
check((out / "claude" / "relevant-context-search.md").is_file(), "claude agent file written")

n_codex = runtime_agents.materialize_runtime_agents(out / "codex", "codex")
check(n_codex == 1, "codex materializes one agent file")
check((out / "codex" / "relevant-context-search.toml").is_file(), "codex agent file written")

n_gemini = runtime_agents.materialize_runtime_agents(out / "gemini", "gemini")
check(n_gemini == 0, "gemini is a graceful no-op")
check(not (out / "gemini").exists() or not any((out / "gemini").iterdir()), "gemini writes nothing")

# Overwrite + symlink-safety: a pre-existing symlink must not be written through.
link_root = out / "link-target"
link_root.mkdir(parents=True, exist_ok=True)
sibling = link_root / "victim.md"
sibling.write_text("real-home-content", encoding="utf-8")
slot = out / "claude-link"
slot.mkdir(parents=True, exist_ok=True)
(slot / "relevant-context-search.md").symlink_to(sibling)
runtime_agents.materialize_runtime_agents(slot, "claude")
check((slot / "relevant-context-search.md").read_text() != "real-home-content",
      "materialize unlinks a prior symlink instead of writing through it")
check(sibling.read_text() == "real-home-content", "real home file behind symlink is untouched")

print("OK test_extension_agents_surface")
