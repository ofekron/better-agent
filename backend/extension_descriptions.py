"""What an extension's individual skills, MCP servers and instruction blocks do.

The manifest may declare a `description` per entrypoint, but the authoritative
text belongs next to the thing it describes, in markdown the author already
maintains. Every entrypoint kind resolves the same way:

    manifest `description`  →  the entrypoint's markdown

and every markdown is read the same way: frontmatter `description` if present,
otherwise the opening prose paragraph. One rule for skills, MCP servers and
instruction blocks, so adding documentation never means learning a new slot.

Where each kind's markdown lives:

* skill        — ``<skill path>/SKILL.md``
* instructions — the declared ``path`` itself
* mcp          — ``<server's own directory>/<server name>.md``

Returns "" when nothing is declared or readable. Callers must render the
absence; never substitute a placeholder.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
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


def _lead_paragraph(text: str) -> str:
    """First prose paragraph of a markdown document, skipping frontmatter,
    headings and lists — the part that says what the document is for."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                lines = lines[index + 1 :]
                break
    paragraph: list[str] = []
    for line in lines:
        stripped = line.strip()
        skip = (
            not stripped
            or stripped.startswith(("#", ">", "---", "```", "-", "*", "+"))
            or (stripped[:2].rstrip(".").isdigit() and ". " in stripped[:4])
        )
        if skip:
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    return " ".join(paragraph)


def _markdown_description(path: Path | None) -> str:
    """One reading rule for every entrypoint's markdown: the frontmatter
    `description` an author opted into, else the document's opening prose."""
    if path is None or not path.is_file():
        return ""
    declared = runtime_skills.read_skill_description(path)
    if declared:
        return _clamp(declared)
    try:
        return _clamp(_lead_paragraph(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return ""


def _resolved(root: Path | None, item: dict[str, Any], relative: str) -> str:
    declared = str(item.get("description") or "").strip()
    if declared:
        return _clamp(declared)
    return _markdown_description(_artifact_path(root, relative))


def _server_directory(item: dict[str, Any]) -> str:
    """Directory holding an MCP server, from however it is launched — a
    relative script path, or a dotted module whose top package is a directory
    in the extension package."""
    script = str(item.get("python") or "").strip()
    if script:
        return PurePosixPath(script).parent.as_posix()
    module = str(item.get("module") or "").strip()
    if module:
        return module.split(".", 1)[0]
    return ""


def skill_description(root: Path | None, item: dict[str, Any]) -> str:
    path = str(item.get("path") or "")
    return _resolved(root, item, f"{path}/SKILL.md" if path else "")


def instruction_description(root: Path | None, item: dict[str, Any]) -> str:
    return _resolved(root, item, str(item.get("path") or ""))


def mcp_description(root: Path | None, item: dict[str, Any]) -> str:
    name = str(item.get("name") or "").strip()
    directory = _server_directory(item)
    return _resolved(root, item, f"{directory}/{name}.md" if name and directory else "")
