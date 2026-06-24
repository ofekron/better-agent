"""Scan a project's cwd for Claude-related config files."""

import os
from pathlib import Path

# Known config file definitions: (relative_path, display_name, category, description)
_PROJECT_CONFIGS = [
    ("CLAUDE.md", "CLAUDE.md", "instructions",
     "Project-level instructions for Claude Code"),
    (".claude/CLAUDE.md", ".claude/CLAUDE.md", "instructions",
     "Instructions inside the .claude directory"),
    (".claude/settings.json", "Settings (shared)", "settings",
     "Shared project settings — checked into version control"),
    (".claude/settings.local.json", "Settings (local)", "settings",
     "Local project settings — gitignored, personal overrides"),
    (".claude/keybindings.json", "Keybindings", "settings",
     "Custom keyboard shortcuts for this project"),
    (".claude/launch.json", "Launch config", "settings",
     "Claude Code preview / launch configuration"),
]


def scan_project_configs(cwd: str) -> list[dict]:
    """Return all Claude config files for a project directory.

    Each entry: {name, path, category, description, exists, size, modified}.
    """
    root = Path(cwd).expanduser().resolve()
    if not root.is_dir():
        return []

    entries: list[dict] = []

    # Static config files
    for rel, name, category, desc in _PROJECT_CONFIGS:
        full = root / rel
        entries.append(_file_entry(full, name, category, desc))

    # Skills: .claude/skills/*/
    skills_dir = root / ".claude" / "skills"
    if skills_dir.is_dir():
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            # Each skill dir has a .md file matching the dir name or "README.md"
            skill_files = list(skill_dir.glob("*.md"))
            if not skill_files:
                # Dir exists but no .md — show the dir itself
                entries.append(_file_entry(
                    skill_dir,
                    f"Skill: {skill_dir.name}",
                    "skill",
                    "Skill directory (no .md file found)",
                ))
            for sf in skill_files:
                entries.append(_file_entry(
                    sf,
                    f"Skill: {skill_dir.name}/{sf.name}",
                    "skill",
                    f"Claude Code skill: {skill_dir.name}",
                ))

    # Hooks: .claude/hooks/* (if any)
    hooks_dir = root / ".claude" / "hooks"
    if hooks_dir.is_dir():
        for hf in sorted(hooks_dir.iterdir()):
            if hf.is_file():
                entries.append(_file_entry(
                    hf,
                    f"Hook: {hf.name}",
                    "hook",
                    "Claude Code hook script",
                ))

    return entries


def _file_entry(
    path: Path, name: str, category: str, description: str,
) -> dict:
    exists = path.exists()
    stat = path.stat() if exists else None
    return {
        "name": name,
        "path": str(path),
        "category": category,
        "description": description,
        "exists": exists,
        "size": stat.st_size if stat else 0,
        "modified": _iso(stat.st_mtime) if stat else None,
    }


def _iso(ts: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).isoformat()
