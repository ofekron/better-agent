"""Provider-native sync API security and behavior checks.

Run:
    cd backend && .venv/bin/python scripts/test_provider_config_sync_api_security.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-provider-config-sync-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi import HTTPException  # noqa: E402
import provider_config_sync_api as api  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'} {msg}")
    if not cond:
        FAILURES.append(msg)


def rejects(fn, status: int, msg: str) -> None:
    try:
        fn()
    except HTTPException as e:
        check(e.status_code == status, f"{msg} ({status})")
        return
    check(False, f"{msg} — expected HTTPException")


async def _noop(*_args, **_kwargs):
    return None


def t_project_instructions_sync_creates_missing_provider_file() -> None:
    wipe = Path(tempfile.mkdtemp(prefix="bc-provider-config-sync-project-"))
    project = (wipe / "project").resolve()
    project.mkdir()
    claude = project / "CLAUDE.md"
    claude.write_text("PROJECT INSTRUCTIONS", encoding="utf-8")

    api.project_store.list_projects = lambda: [{"path": str(project), "node_id": "primary"}]
    api.config_store.list_provider_metadata = lambda: [
        {"id": "claude", "name": "Claude", "kind": "claude", "config_dir": ""},
        {"id": "gemini", "name": "Gemini", "kind": "gemini", "config_dir": ""},
    ]
    api.configure(broadcast_changed=_noop)

    payload = api._discover(str(project))
    instructions = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "instructions")
    files = {entry["path"]: entry for entry in instructions["specifics"]}
    gemini = project / "GEMINI.md"
    check(str(claude) in files, "project Claude instructions discovered")
    check(str(gemini) in files, "missing project Gemini instructions are offered as native target")
    check(files[str(gemini)]["exists"] is False, "missing target is marked absent")
    check(files[str(gemini)]["writable"] is True, "missing target is writable")

    to_unified = api.ApplyNativeFileRequest(
        cwd=str(project),
        capability_id="instructions",
        source_path=str(claude),
        target_path=instructions["unified"]["path"],
        expected_source="PROJECT INSTRUCTIONS",
        expected_target=None,
    )
    asyncio.run(api.apply_native_file(to_unified))
    payload = api._discover(str(project))
    instructions = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "instructions")
    check(
        instructions["unified"]["content"] == "PROJECT INSTRUCTIONS",
        "provider instructions apply into unified tracking file",
    )

    to_gemini = api.ApplyNativeFileRequest(
        cwd=str(project),
        capability_id="instructions",
        source_path=instructions["unified"]["path"],
        target_path=str(gemini),
        expected_source="PROJECT INSTRUCTIONS",
        expected_target=None,
    )
    asyncio.run(api.apply_native_file(to_gemini))
    check(gemini.read_text(encoding="utf-8") == "PROJECT INSTRUCTIONS", "apply creates missing provider instructions")
    shutil.rmtree(wipe)


def t_project_claude_auto_memory_is_separate_capability() -> None:
    wipe = Path(tempfile.mkdtemp(prefix="bc-provider-config-sync-auto-memory-"))
    project = (wipe / "project").resolve()
    project.mkdir()
    config_dir = wipe / "claude"
    memory = config_dir / "projects" / api.encode_cwd(str(project)) / "memory" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("CLAUDE AUTO MEMORY", encoding="utf-8")

    api.project_store.list_projects = lambda: [{"path": str(project), "node_id": "primary"}]
    api.config_store.list_provider_metadata = lambda: [
        {"id": "claude", "name": "Claude", "kind": "claude", "config_dir": str(config_dir)},
        {"id": "gemini", "name": "Gemini", "kind": "gemini", "config_dir": ""},
        {"id": "codex", "name": "Codex", "kind": "codex", "config_dir": ""},
    ]

    payload = api._discover(str(project))
    auto_memory = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "memory")
    instructions = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "instructions")
    check(auto_memory["name"] == "Memory", "Claude auto memory is its own capability")
    check(
        Path(auto_memory["specifics"][0]["path"]).resolve() == memory.resolve(),
        "Claude MEMORY.md path is discovered",
    )
    check(instructions["name"] == "General instructions", "instructions remain separate from memory")
    shutil.rmtree(wipe)


def t_project_skills_sync_offers_missing_provider_targets() -> None:
    wipe = Path(tempfile.mkdtemp(prefix="bc-provider-config-sync-skills-"))
    project = (wipe / "project").resolve()
    project.mkdir()
    claude_skill = project / ".claude" / "skills" / "reviewer" / "SKILL.md"
    claude_skill.parent.mkdir(parents=True)
    claude_skill.write_text("---\ndescription: Review code\n---\nReview carefully.\n", encoding="utf-8")

    api.project_store.list_projects = lambda: [{"path": str(project), "node_id": "primary"}]
    api.config_store.list_provider_metadata = lambda: [
        {"id": "claude", "name": "Claude", "kind": "claude", "config_dir": ""},
        {"id": "gemini", "name": "Gemini", "kind": "gemini", "config_dir": ""},
        {"id": "codex", "name": "Codex", "kind": "codex", "config_dir": ""},
    ]

    payload = api._discover(str(project))
    skill = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "skill-reviewer")
    by_path = {entry["path"]: entry for entry in skill["specifics"]}
    shared_target = project / ".agents" / "skills" / "reviewer" / "SKILL.md"
    check(str(claude_skill) in by_path, "Claude skill is discovered")
    check(str(shared_target) in by_path, "missing shared Gemini/Codex skill target is offered")
    check(set(by_path[str(shared_target)]["provider_kinds"]) == {"gemini", "codex"}, "shared skill target maps to Gemini and Codex")
    check(by_path[str(shared_target)]["writable"] is True, "missing shared skill target is writable")
    content = json.loads(by_path[str(claude_skill)]["content"])
    check(content["name"] == "reviewer", "skill name is normalized from directory")
    check(content["description"] == "Review code", "skill description is normalized")
    check(content["instructions"] == "Review carefully.\n", "skill body is normalized")
    asyncio.run(
        api.upsert_unified_capability_item(
            api.UpsertUnifiedCapabilityItemRequest(
                cwd=str(project),
                capability_id="skill-reviewer",
                item={
                    "name": "reviewer",
                    "description": "Review code",
                    "instructions": "Review carefully.\n",
                    "metadata": {"allowed-tools": ["Read"]},
                },
            )
        )
    )
    payload = api._discover(str(project))
    skill = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "skill-reviewer")
    unified_item = json.loads(skill["unified"]["content"])
    check(unified_item["metadata"]["allowed-tools"] == ["Read"], "tool upsert writes unified skill metadata")

    asyncio.run(
        api.apply_native_file(
            api.ApplyNativeFileRequest(
                cwd=str(project),
                capability_id="skill-reviewer",
                source_entry_id=by_path[str(claude_skill)]["entry_id"],
                target_entry_id=skill["unified"]["entry_id"],
                expected_source=by_path[str(claude_skill)]["content"],
                expected_target=skill["unified"]["content"],
            )
        )
    )
    payload = api._discover(str(project))
    skill = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "skill-reviewer")
    by_path = {entry["path"]: entry for entry in skill["specifics"]}
    asyncio.run(
        api.apply_native_file(
            api.ApplyNativeFileRequest(
                cwd=str(project),
                capability_id="skill-reviewer",
                source_entry_id=skill["unified"]["entry_id"],
                target_entry_id=by_path[str(shared_target)]["entry_id"],
                expected_source=skill["unified"]["content"],
                expected_target=None,
            )
        )
    )
    shared_content = shared_target.read_text(encoding="utf-8")
    check("name: reviewer" in shared_content, "apply writes shared skill name frontmatter")
    check("description: Review code" in shared_content, "apply writes shared skill description frontmatter")
    check("Review carefully." in shared_content, "apply writes shared skill body")
    shutil.rmtree(wipe)


def t_delete_capability_removes_unified_and_provider_files() -> None:
    wipe = Path(tempfile.mkdtemp(prefix="bc-provider-config-sync-delete-"))
    project = (wipe / "project").resolve()
    project.mkdir()
    claude_skill = project / ".claude" / "skills" / "reviewer" / "SKILL.md"
    shared_skill = project / ".agents" / "skills" / "reviewer" / "SKILL.md"
    claude_skill.parent.mkdir(parents=True)
    shared_skill.parent.mkdir(parents=True)
    claude_skill.write_text("---\ndescription: Review code\n---\nReview carefully.\n", encoding="utf-8")
    shared_skill.write_text("---\nname: reviewer\ndescription: Review code\n---\nReview carefully.\n", encoding="utf-8")

    api.project_store.list_projects = lambda: [{"path": str(project), "node_id": "primary"}]
    api.config_store.list_provider_metadata = lambda: [
        {"id": "claude", "name": "Claude", "kind": "claude", "config_dir": ""},
        {"id": "gemini", "name": "Gemini", "kind": "gemini", "config_dir": ""},
        {"id": "codex", "name": "Codex", "kind": "codex", "config_dir": ""},
    ]
    api.configure(broadcast_changed=_noop)

    payload = api._discover(str(project))
    skill = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "skill-reviewer")
    asyncio.run(
        api.upsert_unified_capability_item(
            api.UpsertUnifiedCapabilityItemRequest(
                cwd=str(project),
                capability_id="skill-reviewer",
                item={
                    "name": "reviewer",
                    "description": "Review code",
                    "instructions": "Review carefully.\n",
                    "metadata": {},
                },
            )
        )
    )
    payload = api._discover(str(project))
    skill = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "skill-reviewer")
    entries = [skill["unified"], *skill["specifics"]]
    result = asyncio.run(
        api.delete_capability(
            api.DeleteCapabilityRequest(
                cwd=str(project),
                scope="project",
                capability_id="skill-reviewer",
                expected_contents={entry["entry_id"]: entry["content"] if entry["exists"] else None for entry in entries},
            )
        )
    )
    check(result["ok"] is True, "delete capability returns ok")
    check(not claude_skill.exists(), "delete capability removes Claude provider file")
    check(not shared_skill.exists(), "delete capability removes shared provider file")
    check(not Path(skill["unified"]["path"]).exists(), "delete capability removes unified file")
    check(claude_skill.with_name("SKILL.md.bc-sync-backup").is_file(), "delete capability backs up provider file")
    payload = api._discover(str(project))
    ids = {capability["capability_id"] for capability in payload["groups"]["project"]}
    check("skill-reviewer" not in ids, "deleted custom capability disappears from discovery")
    shutil.rmtree(wipe)


def t_create_capability_adds_provider_file() -> None:
    wipe = Path(tempfile.mkdtemp(prefix="bc-provider-config-sync-create-"))
    project = (wipe / "project").resolve()
    project.mkdir()

    api.project_store.list_projects = lambda: [{"path": str(project), "node_id": "primary"}]
    api.config_store.list_provider_metadata = lambda: [
        {"id": "claude", "name": "Claude", "kind": "claude", "config_dir": ""},
        {"id": "gemini", "name": "Gemini", "kind": "gemini", "config_dir": ""},
    ]
    api.configure(broadcast_changed=_noop)

    provider_keys = [provider["key"] for provider in api._discover("")["providers"]]
    result = asyncio.run(
        api.create_capability(
            api.CreateCapabilityRequest(
                cwd=str(project),
                scope="project",
                category="skill",
                provider_keys=provider_keys,
                name="new-reviewer",
                description="Review code",
                instructions="Review carefully.",
            )
        )
    )
    created = project / ".claude" / "skills" / "new-reviewer" / "SKILL.md"
    shared = project / ".agents" / "skills" / "new-reviewer" / "SKILL.md"
    unified = Path(result["capability"]["unified"]["path"])
    check(result["ok"] is True, "create capability returns ok")
    check(unified.is_file(), "create capability writes unified file")
    check(created.is_file(), "create capability writes Claude provider file")
    check(shared.is_file(), "create capability writes shared provider file")
    check("Review carefully." in unified.read_text(encoding="utf-8"), "create capability writes unified instructions")
    check("Review carefully." in created.read_text(encoding="utf-8"), "create capability writes provider instructions")
    check(result["capability"]["capability_id"] == "skill-new-reviewer", "create capability returns discovered capability")
    shutil.rmtree(wipe)


def t_project_custom_agent_sync_converts_provider_formats() -> None:
    wipe = Path(tempfile.mkdtemp(prefix="bc-provider-config-sync-agents-"))
    project = (wipe / "project").resolve()
    project.mkdir()
    claude_agent = project / ".claude" / "agents" / "reviewer.md"
    claude_agent.parent.mkdir(parents=True)
    claude_agent.write_text(
        "---\n"
        "name: reviewer\n"
        "description: Reviews code for correctness.\n"
        "model: sonnet\n"
        "---\n"
        "Review carefully.\n",
        encoding="utf-8",
    )

    api.project_store.list_projects = lambda: [{"path": str(project), "node_id": "primary"}]
    api.config_store.list_provider_metadata = lambda: [
        {"id": "claude", "name": "Claude", "kind": "claude", "config_dir": ""},
        {"id": "gemini", "name": "Gemini", "kind": "gemini", "config_dir": ""},
        {"id": "codex", "name": "Codex", "kind": "codex", "config_dir": ""},
    ]
    api.configure(broadcast_changed=_noop)

    payload = api._discover(str(project))
    agent = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "agent-reviewer")
    by_kind = {entry["provider_kinds"][0]: entry for entry in agent["specifics"]}
    check(set(by_kind) == {"claude", "gemini", "codex"}, "custom agent offers Claude, Gemini, and Codex targets")
    check(by_kind["claude"]["exists"] is True, "Claude custom agent is discovered")
    check(by_kind["gemini"]["exists"] is False, "missing Gemini custom agent target is offered")
    check(by_kind["codex"]["exists"] is False, "missing Codex custom agent target is offered")
    check(json.loads(by_kind["claude"]["content"])["metadata"]["model"] == "sonnet", "agent metadata is normalized")

    asyncio.run(
        api.apply_native_file(
            api.ApplyNativeFileRequest(
                cwd=str(project),
                capability_id="agent-reviewer",
                source_entry_id=by_kind["claude"]["entry_id"],
                target_entry_id=agent["unified"]["entry_id"],
                expected_source=by_kind["claude"]["content"],
                expected_target=None,
            )
        )
    )
    payload = api._discover(str(project))
    agent = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "agent-reviewer")
    by_kind = {entry["provider_kinds"][0]: entry for entry in agent["specifics"]}
    unified = agent["unified"]
    for kind in ("gemini", "codex"):
        asyncio.run(
            api.apply_native_file(
                api.ApplyNativeFileRequest(
                    cwd=str(project),
                    capability_id="agent-reviewer",
                    source_entry_id=unified["entry_id"],
                    target_entry_id=by_kind[kind]["entry_id"],
                    expected_source=unified["content"],
                    expected_target=None,
                )
            )
        )

    gemini_agent = project / ".gemini" / "agents" / "reviewer.md"
    codex_agent = project / ".codex" / "agents" / "reviewer.toml"
    check("name: reviewer" in gemini_agent.read_text(encoding="utf-8"), "Gemini agent is written as markdown frontmatter")
    codex_data = tomllib.loads(codex_agent.read_text(encoding="utf-8"))
    check(codex_data["name"] == "reviewer", "Codex agent TOML gets name")
    check(codex_data["developer_instructions"] == "Review carefully.\n", "Codex agent TOML gets instructions")
    check(codex_data["model"] == "sonnet", "Codex agent TOML preserves normalized metadata")
    shutil.rmtree(wipe)


def t_project_mcp_sync_offers_missing_provider_sections() -> None:
    wipe = Path(tempfile.mkdtemp(prefix="bc-provider-config-sync-mcp-"))
    project = (wipe / "project").resolve()
    project.mkdir()
    claude_mcp = project / ".mcp.json"
    claude_mcp.write_text(
        '{\n  "mcpServers": {\n    "demo": {\n      "command": "echo",\n      "args": ["hello"]\n    }\n  }\n}\n',
        encoding="utf-8",
    )
    gemini_settings = project / ".gemini" / "settings.json"
    gemini_settings.parent.mkdir()
    gemini_settings.write_text('{"theme": "Default"}\n', encoding="utf-8")

    api.project_store.list_projects = lambda: [{"path": str(project), "node_id": "primary"}]
    api.config_store.list_provider_metadata = lambda: [
        {"id": "claude", "name": "Claude", "kind": "claude", "config_dir": ""},
        {"id": "gemini", "name": "Gemini", "kind": "gemini", "config_dir": ""},
        {"id": "codex", "name": "Codex", "kind": "codex", "config_dir": ""},
    ]
    api.configure(broadcast_changed=_noop)

    payload = api._discover(str(project))
    mcp = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "mcp")
    by_kind = {entry["provider_kinds"][0]: entry for entry in mcp["specifics"]}
    check(set(by_kind) == {"claude", "gemini", "codex"}, "project MCP offers Claude, Gemini, and Codex targets")
    check(by_kind["gemini"]["exists"] is False, "Gemini MCP section is marked absent when settings lacks mcpServers")
    check(by_kind["gemini"]["writable"] is True, "Gemini MCP section is writable")
    check(by_kind["codex"]["exists"] is False, "missing Codex MCP config is marked absent")
    check(by_kind["codex"]["writable"] is True, "missing Codex MCP config is writable")
    asyncio.run(
        api.upsert_unified_capability_item(
            api.UpsertUnifiedCapabilityItemRequest(
                cwd=str(project),
                capability_id="mcp",
                item_name="demo",
                item={"command": "echo", "args": ["hello"]},
            )
        )
    )
    payload = api._discover(str(project))
    mcp = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "mcp")
    check(json.loads(mcp["unified"]["content"])["mcpServers"]["demo"]["command"] == "echo", "tool upsert writes unified MCP item")
    asyncio.run(
        api.remove_unified_capability_item(
            api.RemoveUnifiedCapabilityItemRequest(
                cwd=str(project),
                capability_id="mcp",
                item_name="demo",
            )
        )
    )
    payload = api._discover(str(project))
    mcp = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "mcp")
    check("demo" not in json.loads(mcp["unified"]["content"])["mcpServers"], "tool remove deletes unified MCP item")

    asyncio.run(
        api.apply_native_file(
            api.ApplyNativeFileRequest(
                cwd=str(project),
                capability_id="mcp",
                source_entry_id=by_kind["claude"]["entry_id"],
                target_entry_id=mcp["unified"]["entry_id"],
                expected_source=by_kind["claude"]["content"],
                expected_target=mcp["unified"]["content"],
            )
        )
    )
    payload = api._discover(str(project))
    mcp = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "mcp")
    by_kind = {entry["provider_kinds"][0]: entry for entry in mcp["specifics"]}
    unified = mcp["unified"]

    for kind in ("gemini", "codex"):
        asyncio.run(
            api.apply_native_file(
                api.ApplyNativeFileRequest(
                    cwd=str(project),
                    capability_id="mcp",
                    source_entry_id=unified["entry_id"],
                    target_entry_id=by_kind[kind]["entry_id"],
                    expected_source=unified["content"],
                    expected_target=None,
                )
            )
        )

    gemini_data = json.loads(gemini_settings.read_text(encoding="utf-8"))
    codex_data = tomllib.loads((project / ".codex" / "config.toml").read_text(encoding="utf-8"))
    check(gemini_data["theme"] == "Default", "Gemini settings outside MCP are preserved")
    check(gemini_data["mcpServers"]["demo"]["command"] == "echo", "Gemini MCP section receives unified servers")
    check(codex_data["mcp_servers"]["demo"]["command"] == "echo", "Codex MCP table receives unified servers")
    shutil.rmtree(wipe)


def t_claude_projects_registry_is_not_sync_capability() -> None:
    wipe = Path(tempfile.mkdtemp(prefix="bc-provider-config-sync-claude-registry-"))
    config_dir = wipe / "claude"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text("{}\n", encoding="utf-8")
    (config_dir / ".claude.json").write_text('{"projects": {}}\n', encoding="utf-8")

    api.project_store.list_projects = lambda: []
    api.config_store.list_provider_metadata = lambda: [
        {"id": "claude", "name": "Claude", "kind": "claude", "config_dir": str(config_dir)},
    ]

    payload = api._discover("")
    ids = {capability["capability_id"] for capability in payload["groups"]["global"]}
    check("settings" in ids, "Claude settings still appears")
    check("claude-projects" not in ids, "Claude projects registry is not offered as a sync capability")
    shutil.rmtree(wipe)


def t_symlinked_global_file_reads_and_writes_real_target() -> None:
    wipe = Path(tempfile.mkdtemp(prefix="bc-provider-config-sync-symlink-"))
    config_dir = wipe / "claude"
    target_dir = wipe / "real"
    config_dir.mkdir()
    target_dir.mkdir()
    target = target_dir / "CLAUDE.global.md"
    link = config_dir / "CLAUDE.md"
    target.write_text("REAL", encoding="utf-8")
    link.symlink_to(target)

    api.project_store.list_projects = lambda: []
    api.config_store.list_provider_metadata = lambda: [
        {"id": "claude", "name": "Claude", "kind": "claude", "config_dir": str(config_dir)},
    ]
    api.configure(broadcast_changed=_noop)

    payload = api._discover("")
    instructions = next(capability for capability in payload["groups"]["global"] if capability["capability_id"] == "instructions")
    specific = instructions["specifics"][0]
    check(Path(specific["path"]).is_symlink(), "symlink path remains the displayed provider path")
    check(specific["content"] == "REAL", "symlinked provider file reads real target")
    check(specific["writable"] is True, "symlinked provider file is writable")

    asyncio.run(
        api.write_native_file(
            api.WriteNativeFileRequest(
                cwd="",
                entry_id=specific["entry_id"],
                expected_content="REAL",
                content="UPDATED",
            )
        )
    )
    check(link.is_symlink(), "configured symlink remains a symlink")
    check(target.read_text(encoding="utf-8") == "UPDATED", "write updates real symlink target")
    check((target_dir / "CLAUDE.global.md.bc-sync-backup").read_text(encoding="utf-8") == "REAL", "backup is beside real target")
    shutil.rmtree(wipe)


def t_stale_write_rejected_and_backup_created() -> None:
    wipe = Path(tempfile.mkdtemp(prefix="bc-provider-config-sync-write-"))
    project = (wipe / "project").resolve()
    project.mkdir()
    target = project / "AGENTS.md"
    target.write_text("OLD", encoding="utf-8")
    target.chmod(0o600)

    api.project_store.list_projects = lambda: [{"path": str(project), "node_id": "primary"}]
    api.config_store.list_provider_metadata = lambda: [
        {"id": "codex", "name": "Codex", "kind": "codex", "config_dir": ""},
    ]
    api.configure(broadcast_changed=_noop)

    stale = api.WriteNativeFileRequest(
        cwd=str(project),
        path=str(target),
        expected_content="STALE",
        content="NEW",
    )
    rejects(lambda: asyncio.run(api.write_native_file(stale)), 409, "stale write rejected")
    check(target.read_text(encoding="utf-8") == "OLD", "stale write leaves file unchanged")

    ok = api.WriteNativeFileRequest(
        cwd=str(project),
        path=str(target),
        expected_content="OLD",
        content="NEW",
    )
    asyncio.run(api.write_native_file(ok))
    backup = target.with_name("AGENTS.md.bc-sync-backup")
    marker = target.with_name("AGENTS.md.bc-sync-backup.sha256")
    check(target.read_text(encoding="utf-8") == "NEW", "write updates native file")
    check(backup.read_text(encoding="utf-8") == "OLD", "overwrite creates original backup")
    check(marker.is_file(), "backup marker created")
    check((backup.stat().st_mode & 0o777) == 0o600, "backup preserves permissions")
    shutil.rmtree(wipe)


def t_restore_backup_reverts_native_file() -> None:
    wipe = Path(tempfile.mkdtemp(prefix="bc-provider-config-sync-restore-"))
    project = (wipe / "project").resolve()
    project.mkdir()
    target = project / "AGENTS.md"
    target.write_text("OLD", encoding="utf-8")

    api.project_store.list_projects = lambda: [{"path": str(project), "node_id": "primary"}]
    api.config_store.list_provider_metadata = lambda: [
        {"id": "codex", "name": "Codex", "kind": "codex", "config_dir": ""},
    ]
    api.configure(broadcast_changed=_noop)

    asyncio.run(
        api.write_native_file(
            api.WriteNativeFileRequest(
                cwd=str(project),
                path=str(target),
                expected_content="OLD",
                content="NEW",
            )
        )
    )
    payload = api._discover(str(project))
    instructions = next(capability for capability in payload["groups"]["project"] if capability["capability_id"] == "instructions")
    codex = next(entry for entry in instructions["specifics"] if entry["path"] == str(target))
    check(codex["backup_exists"] is True, "written file reports rollback backup")

    stale = api.RestoreNativeFileRequest(
        cwd=str(project),
        entry_id=codex["entry_id"],
        expected_content="STALE",
    )
    rejects(lambda: asyncio.run(api.restore_native_file(stale)), 409, "stale restore rejected")
    check(target.read_text(encoding="utf-8") == "NEW", "stale restore leaves file unchanged")

    asyncio.run(
        api.restore_native_file(
            api.RestoreNativeFileRequest(
                cwd=str(project),
                entry_id=codex["entry_id"],
                expected_content="NEW",
            )
        )
    )
    check(target.read_text(encoding="utf-8") == "OLD", "restore writes original backup content")
    shutil.rmtree(wipe)


def t_unknown_and_remote_project_rejected() -> None:
    wipe = Path(tempfile.mkdtemp(prefix="bc-provider-config-sync-reject-"))
    project = (wipe / "project").resolve()
    project.mkdir()
    source = project / "CLAUDE.md"
    source.write_text("OK", encoding="utf-8")
    outside = wipe / "outside.md"
    outside.write_text("NO", encoding="utf-8")

    api.config_store.list_provider_metadata = lambda: [
        {"id": "claude", "name": "Claude", "kind": "claude", "config_dir": ""},
    ]
    api.project_store.list_projects = lambda: [{"path": str(project), "node_id": "remote-node"}]
    rejects(lambda: api._local_project_root(str(project)), 400, "remote project rejected")

    api.project_store.list_projects = lambda: [{"path": str(project), "node_id": "primary"}]
    bad = api.WriteNativeFileRequest(
        cwd=str(project),
        path=str(outside),
        expected_content="NO",
        content="YES",
    )
    rejects(lambda: asyncio.run(api.write_native_file(bad)), 400, "non-discovered path rejected")
    check(outside.read_text(encoding="utf-8") == "NO", "rejected path unchanged")
    shutil.rmtree(wipe)


def t_llm_reviewer_uses_glm51_and_redacts_payload() -> None:
    import sys
    import types
    import extension_package_loader

    original_ensure_package_importable = extension_package_loader.ensure_package_importable
    original_requirement_analysis = sys.modules.get("requirement_analysis")
    original_requirement_llm = sys.modules.get("requirement_analysis.llm")
    original_internal_llm = api.config_store.get_internal_llm_assignments()
    seen: dict = {}

    def fake_complete(system: str, user: str, *, model: str | None = None, max_tokens: int = 8192) -> str:
        seen["system"] = system
        seen["user"] = user
        seen["model"] = model
        seen["max_tokens"] = max_tokens
        return '{"approve_hunk_ids":["h1"]}'

    try:
        api.config_store.set_internal_llm_assignments({
            **original_internal_llm,
            "provider_config_sync_review": {"model": "glm-5.1"},
        })
        extension_package_loader.ensure_package_importable = lambda extension_id, package_name: None
        fake_package = types.ModuleType("requirement_analysis")
        fake_llm = types.ModuleType("requirement_analysis.llm")
        fake_llm.complete = fake_complete
        fake_package.llm = fake_llm
        sys.modules["requirement_analysis"] = fake_package
        sys.modules["requirement_analysis.llm"] = fake_llm
        approved = api._review_provider_config_sync_hunks_with_zai({
            "candidates": [{
                "hunk_id": "h1",
                "operation": "change",
                "rows": [{"unifiedText": "api_key: SECRET", "specificText": "token=VALUE"}],
            }],
        })
    finally:
        api.config_store.set_internal_llm_assignments(original_internal_llm)
        extension_package_loader.ensure_package_importable = original_ensure_package_importable
        if original_requirement_analysis is None:
            sys.modules.pop("requirement_analysis", None)
        else:
            sys.modules["requirement_analysis"] = original_requirement_analysis
        if original_requirement_llm is None:
            sys.modules.pop("requirement_analysis.llm", None)
        else:
            sys.modules["requirement_analysis.llm"] = original_requirement_llm

    check(approved == ["h1"], "Better Agent LLM reviewer returns approved hunk ids")
    check(seen["model"] == "glm-5.1", "Better Agent LLM reviewer pins glm-5.1")
    check("SECRET" not in seen["user"] and "VALUE" not in seen["user"], "Better Agent LLM reviewer redacts obvious secrets")


def t_prompt_templates_are_grouped_and_path_confined() -> None:
    wipe = Path(tempfile.mkdtemp(prefix="bc-provider-config-sync-prompts-"))
    runtime = wipe / "runtime"
    provisioning = wipe / "provisioning"
    (runtime / "manager").mkdir(parents=True)
    provisioning.mkdir(parents=True)
    manager_prompt = runtime / "manager" / "bootstrap.md"
    worker_prompt = provisioning / "worker_prep.md"
    manager_prompt.write_text("MANAGER", encoding="utf-8")
    worker_prompt.write_text("WORKER", encoding="utf-8")

    original_roots = api._prompt_roots
    original_broadcast = api._broadcast_better_agent_changed
    try:
        api._prompt_roots = lambda: {
            "runtime": ("Runtime prompts", runtime),
            "provisioning": ("Provisioning prompts", provisioning),
        }
        api._broadcast_better_agent_changed = _noop
        payload = api._discover_prompt_templates()
        roots = {root["id"]: root for root in payload["roots"]}
        runtime_folders = {folder["path"]: folder for folder in roots["runtime"]["folders"]}
        check("manager" in runtime_folders, "runtime prompts are grouped by folder")
        check(
            runtime_folders["manager"]["items"][0]["rel_path"] == "manager/bootstrap.md",
            "prompt item keeps root-relative path",
        )

        req = api.PromptTemplateWriteRequest(
            root_id="runtime",
            rel_path="manager/bootstrap.md",
            content="UPDATED",
        )
        result = asyncio.run(api.write_provider_config_sync_prompt(req))
        check(result["item"]["content"] == "UPDATED", "prompt write returns updated item")
        check(manager_prompt.read_text(encoding="utf-8") == "UPDATED", "prompt write updates selected file")
        rejects(
            lambda: api._resolve_prompt_template("runtime", "../outside.md"),
            400,
            "prompt traversal path rejected",
        )
        rejects(
            lambda: api._resolve_prompt_template("runtime", "manager/bootstrap.txt"),
            400,
            "non-Markdown prompt path rejected",
        )
    finally:
        api._prompt_roots = original_roots
        api._broadcast_better_agent_changed = original_broadcast
        shutil.rmtree(wipe)


def main() -> int:
    for name, fn in [
        ("project instructions sync creates missing provider file", t_project_instructions_sync_creates_missing_provider_file),
        ("project Claude auto memory is separate capability", t_project_claude_auto_memory_is_separate_capability),
        ("project skills sync offers missing provider targets", t_project_skills_sync_offers_missing_provider_targets),
        ("delete capability removes unified and provider files", t_delete_capability_removes_unified_and_provider_files),
        ("create capability adds provider file", t_create_capability_adds_provider_file),
        ("project custom agent sync converts provider formats", t_project_custom_agent_sync_converts_provider_formats),
        ("project MCP sync offers missing provider sections", t_project_mcp_sync_offers_missing_provider_sections),
        ("Claude projects registry is not sync capability", t_claude_projects_registry_is_not_sync_capability),
        ("symlinked global file reads and writes real target", t_symlinked_global_file_reads_and_writes_real_target),
        ("stale write rejected and backup created", t_stale_write_rejected_and_backup_created),
        ("restore backup reverts native file", t_restore_backup_reverts_native_file),
        ("unknown and remote project rejected", t_unknown_and_remote_project_rejected),
        ("LLM reviewer uses glm-5.1 and redacts payload", t_llm_reviewer_uses_glm51_and_redacts_payload),
        ("prompt templates are grouped and path confined", t_prompt_templates_are_grouped_and_path_confined),
    ]:
        print(f"\n--- {name} ---")
        try:
            fn()
        except Exception as e:
            FAILURES.append(f"{name}: {e!r}")
            import traceback
            traceback.print_exc()
    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)}")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
