"""File tree and git status utilities."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".next", ".nuxt", "dist", "build", ".cache", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "egg-info",
}


def _norm(p) -> str:
    """Render a filesystem path with forward slashes on every OS.

    The directory picker (and everything it feeds: project cwd, session
    cwd, the frontend's many `cwd.split("/")` / `${cwd}/${path}` helpers,
    JSON/JS/shell round-trips) assumes `/` separators. On Windows
    ``str(Path(...))`` yields backslashes, which are an escape character
    in those contexts — so a picked path like ``C:\\Users\\me\\proj``
    breaks downstream. Forward slashes are accepted natively by Windows
    APIs (``Path``, ``subprocess(cwd=...)``) and Node, so normalizing here
    is the single fix for the whole class of path-divider/escaping bugs."""
    return Path(p).as_posix()

# Extensions allowed for raw binary serving (media files only).
MEDIA_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
    ".pdf": "application/pdf",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".m4v": "video/mp4",
    ".ogv": "video/ogg",
    ".3gp": "video/3gpp",
}


def get_raw_file_info(file_path: str) -> dict:
    """Validate a path for raw binary serving. Returns
    {path, mime_type, size} or raises FileNotFoundError/ValueError."""
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")
    mime_type = MEDIA_EXTENSIONS.get(path.suffix.lower())
    if not mime_type:
        raise ValueError(f"Unsupported media type: {path.suffix}")
    return {
        "path": str(path),
        "mime_type": mime_type,
        "size": path.stat().st_size,
    }


LANGUAGE_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".jsx": "javascript",
    ".json": "json", ".html": "html", ".css": "css",
    ".md": "markdown", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".rs": "rust", ".go": "go",
    ".java": "java", ".c": "c", ".cpp": "cpp", ".h": "c",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".sql": "sql", ".xml": "xml", ".svg": "xml",
    ".txt": "plaintext", ".env": "plaintext",
    ".gitignore": "plaintext", ".dockerfile": "dockerfile",
}


def list_directories(path: str) -> dict:
    """List immediate sub-directories of `path` for the directory picker.

    Returns `{path, parent, entries: [{name, path}], exists}`. Hidden
    directories (starting with ".") are excluded.

    Empty `path` falls back to the user's home dir. An explicitly-typed
    path that doesn't resolve to a directory is returned AS-IS with
    `exists: False` and no entries — so the picker can offer to create
    it on select instead of silently bouncing back to home.
    """
    if not path:
        p = Path.home()
    else:
        p = Path(path).expanduser().resolve()

    if not p.is_dir():
        parent = _norm(p.parent) if p.parent != p else None
        return {"path": _norm(p), "parent": parent, "entries": [], "exists": False}

    entries: list[dict] = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):
                continue
            if not child.is_dir():
                continue
            entries.append({"name": child.name, "path": _norm(child)})
    except PermissionError:
        entries = []

    parent = _norm(p.parent) if p.parent != p else None
    return {"path": _norm(p), "parent": parent, "entries": entries, "exists": True}


def get_file_tree(root: str, max_depth: int = 3) -> dict:
    """Return nested dict representing the file tree."""
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        return {"name": root_path.name, "path": _norm(root_path), "type": "file"}

    def walk(path: Path, depth: int) -> dict:
        node = {
            "name": path.name,
            "path": _norm(path),
            "type": "directory",
        }
        if depth >= max_depth:
            node["children"] = []
            return node

        children = []
        try:
            entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            entries = []

        for entry in entries:
            if entry.is_dir():
                if entry.name in SKIP_DIRS:
                    continue
                children.append(walk(entry, depth + 1))
            else:
                children.append({
                    "name": entry.name,
                    "path": _norm(entry),
                    "type": "file",
                })
        node["children"] = children
        return node

    return walk(root_path, 0)


def create_file(path: str) -> dict:
    target = Path(path).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"Path already exists: {path}")
    if not target.parent.is_dir():
        raise FileNotFoundError(f"Parent directory not found: {target.parent}")
    target.touch()
    return {"path": _norm(target), "type": "file"}


def create_directory(path: str) -> dict:
    target = Path(path).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"Path already exists: {path}")
    if not target.parent.is_dir():
        raise FileNotFoundError(f"Parent directory not found: {target.parent}")
    target.mkdir()
    return {"path": _norm(target), "type": "directory"}


_VALID_METHODS = ("path", "name", "symbols")

# Directories that are noisy in searches but don't start with '.' — paired
# with SKIP_DIRS during the search walk so they are pruned early.
_SEARCH_SKIP_DIRS = SKIP_DIRS | {"Library"}


def _ripgrep_files(root_path: Path, needle: str, timeout: int = 10) -> Optional[set[str]]:
    """Return the set of absolute file paths under `root_path` whose CONTENT
    contains `needle` (case-insensitive literal), via ripgrep.

    INVARIANT: `root_path` MUST be the already `.resolve()`d Path. rg prints
    matched paths verbatim from the root argument, so raw-string membership
    against `str(entry)` from a walk under the SAME resolved root is exact —
    pass anything unresolved and symlinked path components break equality.
    Returns None iff `rg` is not installed (caller surfaces a notice); an
    empty set on timeout / OS error. Filenames containing a newline yield a
    false negative (acceptable — the walk still gates final inclusion).
    """
    rg = shutil.which("rg")
    if not rg:
        return None
    try:
        proc = subprocess.run(
            [rg, "--files-with-matches", "--fixed-strings", "--ignore-case",
             "--no-messages", "--", needle, str(root_path)],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return set()
    return {line for line in proc.stdout.splitlines() if line}


def _mdfind_dirs(root_path: Path, q: str, max_results: int) -> Optional[list[str]]:
    """Instant directory search via macOS Spotlight (``mdfind``).

    Returns list of absolute directory paths or ``None`` when ``mdfind`` is
    unavailable (non-macOS).
    """
    mdfind = shutil.which("mdfind")
    if not mdfind:
        return None
    try:
        proc = subprocess.run(
            [mdfind, "-name", q, "-onlyin", str(root_path)],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return [l for l in proc.stdout.splitlines()
            if l and Path(l).is_dir()][:max_results]


def _depth_limited_walk(
    root_path: Path, q: str, methods: set[str],
    max_depth: int = 6, max_visited: int = 8000,
) -> Optional[tuple[list[str], bool]]:
    """Breadth-first depth-limited walk — fast fallback when ``mdfind`` is
    absent. Returns ``(matching_abs_paths, was_truncated)``."""
    matches: list[str] = []
    visited = 0
    truncated = False
    # (path, relative_parts, depth)
    queue: list[tuple[Path, list[str], int]] = [(root_path, [], 0)]

    while queue:
        path, rel_parts, depth = queue.pop(0)
        visited += 1
        if visited > max_visited:
            truncated = True
            break
        try:
            entries = list(path.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if not entry.is_dir() or entry.is_symlink():
                continue
            if entry.name in _SEARCH_SKIP_DIRS:
                continue
            parts = rel_parts + [entry.name]
            rel = "/".join(parts)
            hit = False
            if "name" in methods and q in entry.name.lower():
                hit = True
            if "path" in methods and q in rel.lower():
                hit = True
            if hit:
                matches.append(str(entry))
            if depth < max_depth:
                queue.append((entry, parts, depth + 1))

    truncated = truncated or len(matches) > 500
    return matches[:500], truncated


def _build_dir_tree(root_path: Path, matches: list[str]) -> dict:
    """Build a pruned directory tree containing only the paths from
    *root_path* down to each directory in *matches*."""
    root_str = _norm(root_path)
    root_node: dict = {
        "name": root_path.name, "path": root_str,
        "type": "directory", "children": [],
    }
    nodes: dict[str, dict] = {root_str: root_node}

    for match_str in sorted(matches):
        match_path = Path(match_str)
        try:
            rel = match_path.relative_to(root_path)
        except ValueError:
            continue
        current = root_path
        for part in rel.parts:
            child_path = current / part
            child_str = _norm(child_path)
            if child_str not in nodes:
                node: dict = {
                    "name": part, "path": child_str,
                    "type": "directory", "children": [],
                }
                nodes[child_str] = node
                nodes[_norm(current)]["children"].append(node)
            current = child_path

    return root_node


def search_tree(
    root: str,
    query: str,
    kind: str = "file",
    methods: Iterable[str] = ("path",),
    max_results: int = 500,
    max_visited: int = 50000,
) -> dict:
    """Recursively search `root` for nodes matching `query` by one or more
    `methods` (OR-combined). Methods: "path" (query in the path relative to
    root), "name" (query in the basename), "symbols" (query in file CONTENT
    via ripgrep — files only). Returns a PRUNED FileNode tree:
    `{root, truncated, count, symbols_unavailable}` where `root` is None
    when nothing matched.

    INVARIANT: a node is a *match* iff its type == `kind` and at least one
    selected method matches. "symbols" is dropped when `kind == "dir"`;
    an empty/all-invalid method set matches nothing (no masked default).
    A directory appears iff it is itself a match (kind=="dir") or has a
    descendant match; the root container is never itself a match. The walk
    skips SKIP_DIRS, Library, and directory symlinks, and stops once
    `max_results` matches or `max_visited` dirs are seen.

    For ``kind="dir"``, a fast path is attempted first: ``mdfind`` (macOS
    Spotlight, instant via prebuilt index) or a depth-limited walk as
    fallback. Both bypass the unbounded recursive walk entirely, making
    searches against large home directories (50K+ subdirs) instant.
    """
    q = query.strip().lower()
    root_path = Path(root).resolve()
    sel = [m for m in _VALID_METHODS if m in set(methods)]
    if kind == "dir" and "symbols" in sel:
        sel.remove("symbols")

    if not q or not sel or not root_path.is_dir():
        return {"root": None, "truncated": False, "count": 0,
                "symbols_unavailable": False}

    # ── Fast path for directory searches ────────────────────────
    if kind == "dir":
        sel_set = set(sel)
        matches: list[str] = []
        truncated = False

        # mdfind (macOS Spotlight) — instant via prebuilt index.
        mdfind_hits = _mdfind_dirs(root_path, q, max_results)
        if mdfind_hits is not None:
            for p in mdfind_hits:
                entry = Path(p)
                try:
                    rel = str(entry.relative_to(root_path))
                except ValueError:
                    continue
                hit = False
                if "name" in sel_set and q in entry.name.lower():
                    hit = True
                if "path" in sel_set and q in rel.lower():
                    hit = True
                if hit:
                    matches.append(p)

        # Depth-limited walk — fallback when mdfind is unavailable
        # (non-macOS) or returned nothing (unindexed temp dirs).
        if not matches:
            walk_result = _depth_limited_walk(root_path, q, sel_set)
            if walk_result is not None:
                matches, truncated = walk_result

        truncated = truncated or len(matches) > max_results
        matches = matches[:max_results]
        if not matches:
            return {"root": None, "truncated": truncated, "count": 0,
                    "symbols_unavailable": False}
        tree = _build_dir_tree(root_path, matches)
        return {"root": tree, "truncated": truncated, "count": len(matches),
                "symbols_unavailable": False}

    # ── Slow path: recursive walk (files, or find unavailable) ─────
    want_path = "path" in sel
    want_name = "name" in sel
    want_symbols = kind == "file" and "symbols" in sel
    sym_set: set[str] = set()
    symbols_unavailable = False
    if want_symbols:
        found = _ripgrep_files(root_path, q)
        if found is None:
            symbols_unavailable = True
        else:
            sym_set = found

    def is_hit(entry: Path, child_rel: str) -> bool:
        if want_path and q in child_rel.lower():
            return True
        if want_name and q in entry.name.lower():
            return True
        if want_symbols and str(entry) in sym_set:
            return True
        return False

    state = {"count": 0, "visited": 0, "truncated": False}

    def walk(path: Path, rel: str) -> Optional[dict]:
        if state["count"] >= max_results or state["visited"] >= max_visited:
            state["truncated"] = True
            return None
        state["visited"] += 1

        try:
            entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except (PermissionError, OSError):
            return None

        children: list[dict] = []
        for entry in entries:
            if state["count"] >= max_results or state["visited"] >= max_visited:
                state["truncated"] = True
                break
            child_rel = f"{rel}/{entry.name}" if rel else entry.name

            if entry.is_dir():
                if entry.name in _SEARCH_SKIP_DIRS or entry.is_symlink():
                    continue
                is_match = kind == "dir" and is_hit(entry, child_rel)
                sub = walk(entry, child_rel)
                sub_children = sub["children"] if sub else []
                if sub_children or is_match:
                    if is_match:
                        state["count"] += 1
                    children.append({
                        "name": entry.name,
                        "path": _norm(entry),
                        "type": "directory",
                        "children": sub_children,
                    })
            elif kind == "file" and is_hit(entry, child_rel):
                state["count"] += 1
                children.append({
                    "name": entry.name,
                    "path": _norm(entry),
                    "type": "file",
                })

        return {
            "name": path.name,
            "path": _norm(path),
            "type": "directory",
            "children": children,
        }

    tree = walk(root_path, "")
    if state["count"] == 0:
        return {"root": None, "truncated": state["truncated"], "count": 0,
                "symbols_unavailable": symbols_unavailable}
    return {"root": tree, "truncated": state["truncated"], "count": state["count"],
            "symbols_unavailable": symbols_unavailable}


def get_file_content(file_path: str) -> dict:
    """Return file content with detected language."""
    path = Path(file_path)
    suffix = path.suffix.lower()
    language = LANGUAGE_MAP.get(suffix, "plaintext")

    # Special cases
    if path.name == "Dockerfile":
        language = "dockerfile"
    elif path.name == "Makefile":
        language = "makefile"

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        content = f"Error reading file: {e}"

    meta = get_file_metadata(file_path)
    return {"content": content, "language": language, **meta}


def get_file_metadata(file_path: str) -> dict:
    path = Path(file_path)
    st = path.stat()
    return {
        "path": _norm(path),
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
    }


def write_file_content(file_path: str, content: str) -> dict:
    """Overwrite a file on disk with `content`."""
    path = Path(file_path)
    path.write_text(content, encoding="utf-8")
    return {"path": _norm(path), "bytes": len(content.encode("utf-8"))}


def reconstruct_before_edit(file_path: str, old_string: str, new_string: str) -> dict:
    """Return file content before an edit was applied, plus the current (after) content."""
    result = get_file_content(file_path)
    after_content = result.get("content", "")
    language = result.get("language", "plaintext")

    # Reverse the edit: replace new_string with old_string (first occurrence)
    if new_string and new_string in after_content:
        before_content = after_content.replace(new_string, old_string, 1)
    else:
        # Fallback: can't reverse, use current content as both sides
        before_content = after_content

    return {
        "before_content": before_content,
        "after_content": after_content,
        "language": language,
    }


def get_git_status(cwd: str) -> dict:
    """Return parsed git status."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "-b"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {"is_git": False}

        lines = result.stdout.strip().splitlines()
        branch = ""
        modified, added, deleted, untracked = [], [], [], []

        for line in lines:
            if line.startswith("##"):
                branch = line[3:].split("...")[0]
                continue
            status = line[:2]
            filepath = line[3:]
            if "M" in status:
                modified.append(filepath)
            elif "A" in status:
                added.append(filepath)
            elif "D" in status:
                deleted.append(filepath)
            elif "?" in status:
                untracked.append(filepath)

        return {
            "is_git": True,
            "branch": branch,
            "modified": modified,
            "added": added,
            "deleted": deleted,
            "untracked": untracked,
        }
    except Exception:
        return {"is_git": False}


def git_commit(cwd: str, message: str) -> dict:
    """Stage all tracked changes and commit."""
    try:
        # Stage modified, added, deleted (not untracked by default)
        subprocess.run(
            ["git", "add", "-u"],
            cwd=cwd, capture_output=True, text=True, timeout=30,
        )
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=cwd, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or result.stdout.strip()}
        return {"ok": True, "output": result.stdout.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def git_commit_and_push(cwd: str, message: str) -> dict:
    """Stage all tracked changes, commit, and push."""
    commit_result = git_commit(cwd, message)
    if not commit_result["ok"]:
        return commit_result
    try:
        result = subprocess.run(
            ["git", "push"],
            cwd=cwd, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or result.stdout.strip(), "committed": True}
        return {"ok": True, "output": commit_result["output"] + "\n" + result.stdout.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e), "committed": True}


def get_file_diff(file_path: str, cwd: str) -> Optional[str]:
    """Return git diff for a file."""
    try:
        result = subprocess.run(
            ["git", "diff", file_path],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        return result.stdout if result.returncode == 0 else None
    except Exception:
        return None
