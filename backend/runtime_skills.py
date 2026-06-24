from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

MAX_SKILLS = 50
CLAUDE_RUNTIME_SKILLS_PLUGIN_NAME = "better-agent-runtime-skills"


def runtime_skill_contexts(cwd: str, *, bare_config: bool = False) -> list[dict]:
    if bare_config:
        return []

    skills = _discover_skills(cwd)
    if not skills:
        return []

    lines = [
        "The following skills are available in this session. Use them when their trigger applies.",
        "Before using a skill, read its SKILL.md from the listed path.",
        (
            "When calling Claude's native Skill tool for one of these runtime skills, "
            f"use {CLAUDE_RUNTIME_SKILLS_PLUGIN_NAME}:<skill-name>."
        ),
        "",
    ]
    for skill in skills:
        description = f": {skill['description']}" if skill["description"] else ""
        claude_id = f"{CLAUDE_RUNTIME_SKILLS_PLUGIN_NAME}:{skill['name']}"
        lines.append(
            f"- {skill['name']}{description} "
            f"(Claude Skill id: {claude_id}; file: {skill['path']})"
        )

    return [{
        "name": "Runtime Skills",
        "category": "skills",
        "content_kind": "skills",
        "content": "\n".join(lines),
    }]


def materialize_runtime_skills(root: Path, cwd: str, *, bare_config: bool = False) -> int:
    if bare_config:
        return 0

    count = 0
    root.mkdir(parents=True, exist_ok=True)
    for skill in _discover_skills(cwd):
        source = Path(skill["dir"])
        target = root / skill["name"]
        if target.exists() or target.is_symlink():
            continue
        shutil.copytree(source, target, symlinks=True)
        count += 1
    return count


def _discover_skills(cwd: str) -> list[dict]:
    roots = _skill_roots(cwd)
    seen: set[str] = set()
    skills: list[dict] = []
    for skill in _extension_runtime_skills():
        if len(skills) >= MAX_SKILLS:
            return skills
        name = skill["name"].strip()
        if not name or name in seen:
            continue
        skill_md = Path(skill["path"])
        if not skill_md.is_file():
            continue
        skills.append({
            "name": name,
            "description": _read_description(skill_md),
            "dir": skill["dir"],
            "path": str(skill_md),
        })
        seen.add(name)
    for root in roots:
        if not root.is_dir():
            continue
        for skill_dir in sorted(root.iterdir(), key=lambda p: p.name):
            if len(skills) >= MAX_SKILLS:
                return skills
            if not skill_dir.is_dir():
                continue
            name = skill_dir.name.strip()
            if not name or name in seen:
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            description = _read_description(skill_md)
            skills.append({
                "name": name,
                "description": description,
                "dir": str(skill_dir),
                "path": str(skill_md),
            })
            seen.add(name)
    return skills


def _extension_runtime_skills() -> list[dict[str, str]]:
    try:
        import extension_store
    except Exception:
        return []
    try:
        return extension_store.runtime_skill_entries()
    except Exception:
        return []


def _skill_roots(cwd: str) -> list[Path]:
    roots = [Path.home() / ".agents" / "skills"]
    project_root = _trusted_project_root(cwd)
    if project_root is not None:
        roots.extend(parent / ".agents" / "skills" for parent in _cwd_chain(project_root))
    return roots


def _trusted_project_root(cwd: str) -> Optional[Path]:
    if not cwd:
        return None
    root = Path(cwd).expanduser().resolve()
    if not root.is_dir():
        return None
    return root


def _cwd_chain(cwd: Path) -> list[Path]:
    home = Path.home().resolve()
    out: list[Path] = []
    current = cwd
    while True:
        out.append(current)
        if current == home or current.parent == current:
            return out
        current = current.parent


def _read_description(skill_md: Path) -> str:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    frontmatter = _frontmatter(text)
    if frontmatter is None:
        return ""
    desc = frontmatter.get("description", "")
    return desc.strip()


def _frontmatter(text: str) -> Optional[dict[str, str]]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    out: dict[str, str] = {}
    current_key: Optional[str] = None
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        if line.startswith((" ", "\t")) and current_key:
            continuation = line.strip()
            if continuation:
                out[current_key] = f"{out[current_key]} {continuation}".strip()
            continue
        key, sep, value = line.partition(":")
        if not sep:
            current_key = None
            continue
        current_key = key.strip()
        out[current_key] = value.strip().strip('"').strip("'")
    return None
