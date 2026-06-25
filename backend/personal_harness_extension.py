from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from paths import ba_home

import extension_instructions
import extension_store

PERSONAL_HARNESS_EXTENSION_ID = "personal.harness"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,79}$")
_MANAGED_BLOCK_RE = re.compile(
    r"<!-- BEGIN better-(?:agent|claude):[^\n]*?-->.*?<!-- END better-(?:agent|claude):[^\n]*?-->",
    re.DOTALL,
)


def create(*, project_paths: list[str] | None = None) -> dict[str, Any]:
    projects = _project_paths(project_paths)
    tmp_parent = ba_home() / "extensions" / "tmp"
    tmp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="personal-harness-", dir=tmp_parent) as tmp:
        package_dir = Path(tmp) / "package"
        package_dir.mkdir(parents=True)
        global_content = _instruction_content("global", projects)
        project_content = _instruction_content("project", projects)
        skills = _copy_skills(package_dir, projects)
        instructions: list[dict[str, str]] = []
        required_paths = ["better-agent-extension.json"]
        if global_content:
            path = package_dir / "instructions" / "global.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(global_content, encoding="utf-8")
            instructions.append({
                "name": "personal-global",
                "path": "instructions/global.md",
                "level": "global",
            })
            required_paths.append("instructions/global.md")
        if project_content:
            path = package_dir / "instructions" / "project.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(project_content, encoding="utf-8")
            instructions.append({
                "name": "personal-project",
                "path": "instructions/project.md",
                "level": "project",
            })
            required_paths.append("instructions/project.md")
        if not instructions and not skills:
            raise extension_store.ExtensionError("No personal instructions or skills were found")
        for skill in skills:
            required_paths.append(f"{skill['path']}/SKILL.md")
        manifest = {
            "kind": extension_store.MANIFEST_KIND,
            "id": PERSONAL_HARNESS_EXTENSION_ID,
            "name": "Personal Harness",
            "version": "1.0.0",
            "description": "Personal global/project instructions and skills captured from this Better Agent install.",
            "surfaces": [
                surface
                for surface, enabled in (
                    ("instructions", bool(instructions)),
                    ("skills", bool(skills)),
                )
                if enabled
            ],
            "entrypoints": {
                "instructions": instructions,
                "skills": skills,
            },
            "permissions": {},
            "marketplace": {},
            "protocol": {
                "version": 1,
                "smoke_test": {
                    "required_paths": required_paths,
                    "python_modules": [],
                },
            },
        }
        (package_dir / "better-agent-extension.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        existing = extension_store._load()["extensions"].get(PERSONAL_HARNESS_EXTENSION_ID)
        record = extension_store._install_from_package_dir(
            package_dir=package_dir,
            source={
                "type": "personal_harness",
                "repo_url": "",
                "extension_path": "",
                "ref": "",
                "commit_sha": _directory_content_sha256(package_dir),
            },
            persist=True,
            existing_record=existing,
        )
    return _enable_projects(record, projects, has_project_instructions=bool(project_content))


def _project_paths(project_paths: list[str] | None) -> list[Path]:
    if project_paths is None:
        import project_store

        raw_paths = [
            str(project.get("path") or "")
            for project in project_store.list_projects()
            if (project.get("node_id") or "primary") == "primary"
        ]
    else:
        raw_paths = project_paths
    projects: list[Path] = []
    seen: set[Path] = set()
    for raw in raw_paths:
        path = Path(str(raw or "")).expanduser().resolve()
        if not path.is_dir() or path in seen:
            continue
        projects.append(path)
        seen.add(path)
    return projects


def _instruction_content(scope: str, projects: list[Path]) -> str:
    sections: list[str] = []
    for path in _instruction_paths(scope, projects):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        cleaned = _strip_managed_blocks(text).strip()
        if cleaned:
            sections.append(f"# {path}\n\n{cleaned}")
    return "\n\n".join(sections).strip() + ("\n" if sections else "")


def _instruction_paths(scope: str, projects: list[Path]) -> list[Path]:
    import config_store
    from provider_config_sync_backend import api as pcs_api

    providers = config_store.list_provider_metadata()
    paths: list[Path] = []
    if scope == "global":
        paths = pcs_api.managed_instruction_targets(
            scope="global",
            project_root=None,
            providers=providers,
        )
    elif scope == "project":
        for project in projects:
            paths.extend(
                pcs_api.managed_instruction_targets(
                    scope="project",
                    project_root=project,
                    providers=providers,
                )
            )
    else:
        raise extension_store.ExtensionError("invalid instruction scope")
    return _dedupe_existing_paths(paths)


def _strip_managed_blocks(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", _MANAGED_BLOCK_RE.sub("", text)).strip() + "\n"


def _dedupe_existing_paths(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved in seen or not resolved.is_file():
            continue
        out.append(resolved)
        seen.add(resolved)
    return out


def _copy_skills(package_dir: Path, projects: list[Path]) -> list[dict[str, str]]:
    roots = [Path.home() / ".agents" / "skills", *(project / ".agents" / "skills" for project in projects)]
    skills: list[dict[str, str]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for source in sorted(root.iterdir(), key=lambda p: p.name):
            name = source.name.strip()
            if name in seen or not _ID_RE.fullmatch(name):
                continue
            if not source.is_dir() or not (source / "SKILL.md").is_file():
                continue
            target = package_dir / "skills" / name
            _copy_skill_tree(source, target)
            skills.append({"name": name, "path": f"skills/{name}"})
            seen.add(name)
    return skills


def _copy_skill_tree(source: Path, target: Path) -> None:
    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {name for name in names if name == extension_store._RUNTIME_SKILL_OWNER_FILE}

    shutil.copytree(source, target, ignore=ignore, symlinks=False)
    for path in target.rglob("*"):
        if path.is_symlink():
            raise extension_store.ExtensionError("personal harness skills must not contain symlinks")


def _directory_content_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _enable_projects(
    record: dict[str, Any],
    projects: list[Path],
    *,
    has_project_instructions: bool,
) -> dict[str, Any]:
    if not has_project_instructions:
        return record
    data = extension_store._load()
    stored = data["extensions"].get(PERSONAL_HARNESS_EXTENSION_ID)
    if not stored:
        return record
    state = extension_instructions.normalize_state(stored)
    for project in projects:
        state["projects"][str(project)] = True
    stored["instructions_enabled"] = state
    stored["updated_at"] = extension_store._now()
    extension_store._save(data, resurrect_extension_ids={PERSONAL_HARNESS_EXTENSION_ID})
    extension_instructions.reconcile_blocks(stored)
    return extension_store.get_extension(PERSONAL_HARNESS_EXTENSION_ID) or stored
