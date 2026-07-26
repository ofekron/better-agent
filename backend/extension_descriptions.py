"""What an extension's individual skills and instruction blocks actually do.

The manifest may declare a `description` per entrypoint, but the authoritative
text already ships inside the artifact itself: a skill's SKILL.md carries the
frontmatter `description` the agent matches against, and an instruction block
opens with the paragraph that states its purpose. Reading those keeps one
source of truth instead of asking authors to restate it in the manifest.

Returns "" when nothing is declared or readable — callers must render the
absence, never a placeholder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import runtime_skills

# A caption, not the document. Long enough for a real sentence, short enough
# that a settings row stays scannable.
_MAX_LENGTH = 300


def _clamp(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _MAX_LENGTH:
        return collapsed
    return collapsed[: _MAX_LENGTH - 1].rstrip() + "…"


def _artifact_path(root: Path | None, relative: str) -> Path | None:
    """Resolve a manifest-declared relative path inside the extension's
    installed package, refusing anything that escapes it."""
    if root is None or not relative:
        return None
    try:
        # Both sides resolved: comparing a resolved path against an
        # unresolved root would reject legitimate paths under a symlinked
        # root (and, worse, could accept an escape in the other direction).
        base = root.resolve()
        resolved = (base / relative).resolve()
        resolved.relative_to(base)
    except (OSError, ValueError):
        return None
    return resolved


def _lead_paragraph(path: Path) -> str:
    """First prose paragraph of a markdown document, skipping headings and
    frontmatter — the part that says what the document is for."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                lines = lines[index + 1 :]
                break
    paragraph: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ">", "---", "```")):
            if paragraph:
                break
            continue
        if stripped.startswith(("-", "*", "+")) or (stripped[:2].rstrip(".").isdigit() and ". " in stripped[:4]):
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    return _clamp(" ".join(paragraph))


def skill_description(root: Path | None, item: dict[str, Any]) -> str:
    declared = str(item.get("description") or "").strip()
    if declared:
        return _clamp(declared)
    skill_md = _artifact_path(root, str(item.get("path") or ""))
    if skill_md is None:
        return ""
    return _clamp(runtime_skills.read_skill_description(skill_md / "SKILL.md"))


def instruction_description(root: Path | None, item: dict[str, Any]) -> str:
    declared = str(item.get("description") or "").strip()
    if declared:
        return _clamp(declared)
    content = _artifact_path(root, str(item.get("path") or ""))
    if content is None or not content.is_file():
        return ""
    return _lead_paragraph(content)
