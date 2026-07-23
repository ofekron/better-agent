from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Optional

import installation_profile

MAX_SKILLS = 50
CLAUDE_RUNTIME_SKILLS_PLUGIN_NAME = "better-agent-runtime-skills"
_DISCOVERY_CACHE_LOCK = threading.Lock()
_DISCOVERY_CACHE: dict[tuple, tuple[tuple, list[dict]]] = {}
_DISCOVERY_CACHE_MAX = 32


def runtime_skill_contexts(cwd: str, *, bare_config: bool = False) -> list[dict]:
    if bare_config or not installation_profile.integrations_enabled():
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


def has_runtime_skills(cwd: str, *, bare_config: bool = False) -> bool:
    if bare_config or not installation_profile.integrations_enabled():
        return False
    return bool(_discover_skills(cwd))


def materialize_runtime_skills(root: Path, cwd: str, *, bare_config: bool = False) -> int:
    if bare_config or not installation_profile.integrations_enabled():
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
    cache_key = _discovery_cache_key(roots)
    with _DISCOVERY_CACHE_LOCK:
        cached = _DISCOVERY_CACHE.get(cache_key)
        if cached is not None:
            skill_fingerprint, cached_skills = cached
            if _skills_fingerprint(cached_skills) == skill_fingerprint:
                return [dict(skill) for skill in cached_skills]
    seen: set[str] = set()
    skills: list[dict] = []
    for skill in _extension_runtime_skills():
        if len(skills) >= MAX_SKILLS:
            return _cache_discovered_skills(cache_key, skills)
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
                return _cache_discovered_skills(cache_key, skills)
            if not skill_dir.is_dir():
                continue
            name = skill_dir.name.strip()
            # Dot-dirs are never skills; extension_store stages replacement
            # trees under dot-prefixed siblings before swapping them in.
            if not name or name.startswith(".") or name in seen:
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
    return _cache_discovered_skills(cache_key, skills)


def _cache_discovered_skills(cache_key: tuple, skills: list[dict]) -> list[dict]:
    cached = [dict(skill) for skill in skills]
    with _DISCOVERY_CACHE_LOCK:
        _DISCOVERY_CACHE[cache_key] = (_skills_fingerprint(cached), cached)
        if len(_DISCOVERY_CACHE) > _DISCOVERY_CACHE_MAX:
            _DISCOVERY_CACHE.pop(next(iter(_DISCOVERY_CACHE)))
    return [dict(skill) for skill in cached]


def _discovery_cache_key(roots: list[Path]) -> tuple:
    return (
        _extension_runtime_skills_fingerprint(),
        tuple((str(root), _directory_fingerprint(root)) for root in roots),
    )


def _extension_runtime_skills_fingerprint() -> tuple:
    try:
        import extension_store
    except Exception:
        return ()
    try:
        store_fp = extension_store.store_fingerprint()
    except Exception:
        store_fp = ()
    try:
        settings_fp = extension_store.extension_settings_fingerprint()
    except Exception:
        settings_fp = ()
    return (store_fp, settings_fp)


def _directory_fingerprint(root: Path) -> tuple[int, int, int]:
    try:
        root_stat = root.stat()
    except OSError:
        return (0, 0, 0)
    latest = root_stat.st_mtime_ns
    count = 1
    try:
        for skill_dir in root.iterdir():
            try:
                skill_stat = skill_dir.stat()
            except OSError:
                continue
            latest = max(latest, skill_stat.st_mtime_ns)
            count += 1
            skill_md = skill_dir / "SKILL.md"
            try:
                skill_md_stat = skill_md.stat()
            except OSError:
                continue
            latest = max(latest, skill_md_stat.st_mtime_ns)
            count += 1
    except OSError:
        pass
    return (root_stat.st_mtime_ns, latest, count)


def _skills_fingerprint(skills: list[dict]) -> tuple:
    return tuple(
        (
            str(skill.get("path") or ""),
            _file_fingerprint(Path(str(skill.get("path") or ""))),
            str(skill.get("dir") or ""),
            _path_fingerprint(Path(str(skill.get("dir") or ""))),
        )
        for skill in skills
    )


def _file_fingerprint(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


def _path_fingerprint(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


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
