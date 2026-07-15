#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP_HOME = Path(tempfile.mkdtemp(prefix="bc-runtime-skills-"))
os.environ["HOME"] = str(TMP_HOME)

import runtime_skills  # noqa: E402
from turn_manager import _provider_capability_contexts  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def write_skill(root: Path, name: str, description: str) -> Path:
    skill = root / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n"
        f"# {name}\n",
        encoding="utf-8",
    )
    return skill


def write_raw_skill(root: Path, name: str, frontmatter: str) -> Path:
    skill = root / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(f"---\n{frontmatter}---\n# {name}\n", encoding="utf-8")
    return skill


def t_global_runtime_skill_context_includes_get_requirements() -> None:
    write_skill(
        TMP_HOME / ".agents" / "skills",
        "get-requirements",
        "Search requirements before planning.",
    )
    write_skill(
        TMP_HOME / ".agents" / "skills",
        "local-runtime-test-skill",
        "Local test skill.",
    )
    contexts = runtime_skills.runtime_skill_contexts(str(TMP_HOME))
    check(len(contexts) == 1, "runtime skills create one capability context")
    content = contexts[0]["content"]
    check("get-requirements" in content, "runtime skill list includes get-requirements")
    check(
        "better-agent-runtime-skills:get-requirements" in content,
        "runtime skill list includes Claude native skill id",
    )
    check(str(TMP_HOME / ".agents" / "skills" / "local-runtime-test-skill" / "SKILL.md") in content, "runtime skill list includes file path")


def t_multiline_description_is_not_truncated() -> None:
    write_raw_skill(
        TMP_HOME / ".agents" / "skills",
        "create-skill",
        "name: create-skill\n"
        "description: Create or update a Claude Code skill (SKILL.md). Use when the user asks\n"
        "  to create, add, or write a new skill, or update an existing one.\n",
    )
    content = runtime_skills.runtime_skill_contexts(str(TMP_HOME))[0]["content"]
    check(
        "Use when the user asks to create, add, or write a new skill" in content,
        "runtime skill descriptions include YAML continuation lines",
    )


def t_long_description_is_not_clipped() -> None:
    tail = "TAIL_MARKER_COMPLETE"
    write_skill(
        TMP_HOME / ".agents" / "skills",
        "long-skill",
        f"{'Long description. ' * 80}{tail}",
    )
    content = runtime_skills.runtime_skill_contexts(str(TMP_HOME))[0]["content"]
    check(tail in content, "runtime skill descriptions are not character-clipped")


def t_project_skill_context_is_included_after_global() -> None:
    project = TMP_HOME / "project"
    write_skill(project / ".agents" / "skills", "project-structure", "Project map.")
    contexts = runtime_skills.runtime_skill_contexts(str(project))
    content = contexts[0]["content"]
    check("get-requirements" in content, "global skill remains included for project cwd")
    check("project-structure" in content, "project skill is included")
    check(content.index("get-requirements") < content.index("project-structure"), "global skills precede project skills")


def t_bare_config_skips_runtime_skills() -> None:
    contexts = runtime_skills.runtime_skill_contexts(str(TMP_HOME), bare_config=True)
    check(contexts == [], "bare config skips runtime skills")


def t_runtime_skill_discovery_is_cached_until_roots_change() -> None:
    runtime_skills._DISCOVERY_CACHE.clear()
    calls = 0
    original_extension_skills = runtime_skills._extension_runtime_skills
    original_read_description = runtime_skills._read_description

    def counted_extension_skills():
        nonlocal calls
        calls += 1
        return []

    def fail_read_description(_path):
        raise AssertionError("cached runtime skills should not reread SKILL.md")

    try:
        runtime_skills._extension_runtime_skills = counted_extension_skills
        first = runtime_skills.runtime_skill_contexts(str(TMP_HOME))
        runtime_skills._read_description = fail_read_description
        second = runtime_skills.runtime_skill_contexts(str(TMP_HOME))
    finally:
        runtime_skills._extension_runtime_skills = original_extension_skills
        runtime_skills._read_description = original_read_description

    check(first == second, "runtime skill discovery cache preserves context")
    check(calls == 1, "runtime skill discovery cache skips extension lookup")


def t_runtime_skill_cache_invalidates_on_skill_edit() -> None:
    runtime_skills._DISCOVERY_CACHE.clear()
    skill_md = write_skill(
        TMP_HOME / ".agents" / "skills",
        "cache-edit-skill",
        "Before edit.",
    )
    before = runtime_skills.runtime_skill_contexts(str(TMP_HOME))[0]["content"]
    skill_md.write_text(
        "---\n"
        "name: cache-edit-skill\n"
        "description: After edit.\n"
        "---\n"
        "# cache-edit-skill\n",
        encoding="utf-8",
    )
    after = runtime_skills.runtime_skill_contexts(str(TMP_HOME))[0]["content"]
    check("Before edit." in before, "runtime skill cache captures initial description")
    check("After edit." in after, "runtime skill cache invalidates on skill edit")


def t_materialize_runtime_skills_copies_skill_dirs() -> None:
    root = TMP_HOME / "materialized"
    count = runtime_skills.materialize_runtime_skills(root, str(TMP_HOME))
    check(count >= 1, "runtime skills materializer copies discovered skills")
    check(
        (root / "get-requirements" / "SKILL.md").is_file(),
        "runtime skills materializer writes SKILL.md",
    )


def t_runtime_context_survives_provider_filtering() -> None:
    runtime_context = runtime_skills.runtime_skill_contexts(str(TMP_HOME))[0]
    selected = [
        runtime_context,
        *_provider_capability_contexts([
            {
                "source_id": "manual",
                "capability_id": "manual",
                "name": "Manual",
                "outputs": [{"provider_kind": "codex", "content": "Codex only"}],
            }
        ], "codex"),
    ]
    check(selected[0]["name"] == "Runtime Skills", "runtime context is prepended before provider-specific contexts")
    check("get-requirements" in selected[0]["content"], "runtime context content survives provider filtering")


def main() -> None:
    try:
        t_global_runtime_skill_context_includes_get_requirements()
        t_multiline_description_is_not_truncated()
        t_long_description_is_not_clipped()
        t_project_skill_context_is_included_after_global()
        t_bare_config_skips_runtime_skills()
        t_runtime_skill_discovery_is_cached_until_roots_change()
        t_runtime_skill_cache_invalidates_on_skill_edit()
        t_materialize_runtime_skills_copies_skill_dirs()
        t_runtime_context_survives_provider_filtering()
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
