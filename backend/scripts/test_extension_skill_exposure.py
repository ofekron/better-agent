#!/usr/bin/env python3
"""Per-skill session exposure toggles: a skill unchecked in extension settings
(or declared with default_enabled: false) must not reach sessions via
runtime_skill_entries, and must not materialize natively via
reconcile_runtime_skills even when native-exposed. These tests fail without the
is_runtime_skill_enabled gate and pass with it."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import paths  # noqa: E402

_TEST_HOME = paths.engage_test_home(tempfile.mkdtemp(prefix="ba-skill-exposure-"))

import extension_store  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def install_skill_extension(
    extension_id: str,
    *,
    skills: list[dict],
    mcp: list[dict] | None = None,
) -> None:
    package = Path(_TEST_HOME) / "fixtures" / extension_id
    if package.exists():
        shutil.rmtree(package)
    for item in skills:
        skill_md = package / item["path"] / "SKILL.md"
        skill_md.parent.mkdir(parents=True, exist_ok=True)
        skill_md.write_text(
            f"---\nname: {item['name']}\ndescription: exposure test skill\n---\n# {item['name']}\n",
            encoding="utf-8",
        )
    package.mkdir(parents=True, exist_ok=True)
    for item in mcp or []:
        server_py = package / item["python"]
        server_py.parent.mkdir(parents=True, exist_ok=True)
        server_py.write_text("print('stub mcp server')\n", encoding="utf-8")
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": extension_id,
        "surfaces": ["skills"] + (["runtime_mcp"] if mcp else []),
        "entrypoints": {"skills": skills, **({"mcp": mcp} if mcp else {})},
        "permissions": {},
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(package.parent),
            "extension_path": package.name,
            "ref": "",
            "commit_sha": extension_id,
        },
        persist=True,
    )


def entry_names() -> set[str]:
    return {entry["name"] for entry in extension_store.runtime_skill_entries()}


def t_default_enabled_skill_is_exposed() -> None:
    install_skill_extension("test.skill-default-on", skills=[{"name": "on-by-default", "path": "skills/on-by-default"}])
    check("on-by-default" in entry_names(), "skill without default_enabled is exposed to sessions")
    check(
        extension_store.is_runtime_skill_enabled("test.skill-default-on", "on-by-default"),
        "is_runtime_skill_enabled defaults to True",
    )


def t_default_disabled_skill_is_hidden_until_checked() -> None:
    install_skill_extension(
        "test.skill-default-off",
        skills=[{"name": "off-by-default", "path": "skills/off-by-default", "default_enabled": False}],
    )
    check("off-by-default" not in entry_names(), "default_enabled:false skill is hidden from sessions")
    extension_store.set_runtime_skill_enabled("test.skill-default-off", "off-by-default", True)
    check("off-by-default" in entry_names(), "checking the skill exposes it to sessions")
    skills = extension_store.extension_runtime_skills("test.skill-default-off")
    check(skills == [{"name": "off-by-default", "enabled": True}], "settings payload reflects checked state")


def t_unchecking_hides_skill_and_purges_native() -> None:
    install_skill_extension("test.skill-toggle", skills=[{"name": "toggle-skill", "path": "skills/toggle-skill"}])
    extension_store.set_native_harness_exposed("test.skill-toggle", "skill", "toggle-skill", True)
    native_target = Path.home() / ".agents" / "skills" / "toggle-skill" / "SKILL.md"
    check(native_target.is_file(), "native-exposed skill materializes under ~/.agents/skills")
    extension_store.set_runtime_skill_enabled("test.skill-toggle", "toggle-skill", False)
    check("toggle-skill" not in entry_names(), "unchecked skill is hidden from sessions")
    check(not native_target.exists(), "unchecked skill is purged from native skills dir")
    detail = next(
        item["detail"]
        for item in extension_store.extension_harness_additions(extension_store.get_extension("test.skill-toggle"))
        if item["kind"] == "skill" and item["name"] == "toggle-skill"
    )
    check(detail == "disabled", "harness addition detail reflects disabled state")
    extension_store.set_runtime_skill_enabled("test.skill-toggle", "toggle-skill", True)
    check("toggle-skill" in entry_names(), "re-checking restores session exposure")
    check(native_target.is_file(), "re-checking restores native materialization")


def t_enabled_skill_forces_extension_mcp_on() -> None:
    ext = "test.skill-forces-mcp"
    install_skill_extension(
        ext,
        skills=[{"name": "needy-skill", "path": "skills/needy-skill", "default_enabled": False, "requires_mcp": True}],
        mcp=[{"name": "needy-server", "python": "mcp/server.py"}],
    )
    extension_store.set_mcp_server_enabled(ext, "needy-server", False)
    check(not extension_store.is_mcp_server_enabled(ext, "needy-server"), "explicitly disabled MCP server is off")
    extension_store.set_runtime_skill_enabled(ext, "needy-skill", True)
    check(
        extension_store.is_mcp_server_enabled(ext, "needy-server"),
        "enabling a requires_mcp skill forces the extension MCP server on",
    )
    servers = extension_store.extension_mcp_servers(ext)
    check(
        servers[0]["enabled"] is True and servers[0]["forced_by_skills"] == ["needy-skill"],
        "settings payload reports forced-by-skill state",
    )
    try:
        extension_store.set_mcp_server_enabled(ext, "needy-server", False)
    except extension_store.ExtensionError:
        print("PASS disabling a forced MCP server is rejected")
    else:
        raise AssertionError("disabling a forced MCP server must raise")
    extension_store.set_runtime_skill_enabled(ext, "needy-skill", False)
    check(
        not extension_store.is_mcp_server_enabled(ext, "needy-server"),
        "disabling the skill restores the explicit MCP disable",
    )
    check(
        extension_store.extension_mcp_servers(ext)[0]["forced_by_skills"] == [],
        "forced-by-skill state clears when the skill is disabled",
    )


def t_unknown_skill_rejected() -> None:
    try:
        extension_store.set_runtime_skill_enabled("test.skill-toggle", "missing-skill", True)
    except extension_store.ExtensionError:
        print("PASS unknown skill name is rejected")
    else:
        raise AssertionError("unknown skill name must raise ExtensionError")


def main() -> None:
    t_default_enabled_skill_is_exposed()
    t_default_disabled_skill_is_hidden_until_checked()
    t_unchecking_hides_skill_and_purges_native()
    t_enabled_skill_forces_extension_mcp_on()
    t_unknown_skill_rejected()
    print("OK")


if __name__ == "__main__":
    main()
