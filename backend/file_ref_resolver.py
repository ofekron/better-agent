"""Rewrites file references in user-visible text into markdown links of the
form `[token](bcfile:<abs-path>?L=<start>-<end>)`.

Resolution policy (locked in with the user):
- ONLY check existence as `cwd + path`. No project search, no fuzzy match.
- If the file exists → emit a bcfile: markdown link.
- If not → leave the matched token verbatim.

This module is invoked from `event_ingester.ingest` so every persisted /
broadcast event flows through one rewrite pass before reaching the frontend.
The frontend's `<a>` override unwraps `bcfile:` hrefs into clickable buttons.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Iterable
from urllib.parse import quote

# A reasonable allow-list of extensions a file reference is plausibly
# pointing at. Limits the number of stat() calls we make on every chunk
# of prose. Add to this list if a missing extension shows up in practice.
_EXT_ALLOWLIST = frozenset({
    "py", "pyi", "ts", "tsx", "js", "jsx", "mjs", "cjs",
    "md", "markdown", "rst", "txt", "log",
    "json", "yaml", "yml", "toml", "ini", "cfg", "env", "tsv", "csv",
    "sh", "bash", "zsh", "fish",
    "go", "rs", "c", "h", "cc", "cpp", "hpp", "cxx",
    "java", "kt", "scala", "rb", "php", "swift",
    "html", "htm", "css", "scss", "sass", "less",
    "sql", "graphql", "proto",
    "lock", "dockerfile", "makefile",
    "pdf", "mp4", "webm", "mov", "avi", "mkv", "m4v", "ogv", "3gp",
    "mp3", "wav", "ogg", "flac", "m4a", "aac",
})

# Combined regex with four top-level alternatives:
# 1. `existing_bt` — a `[label](bcfile:href)` link wrapped in backticks
#    (markdown inline code). Backticks suppress link rendering, so we
#    strip them and keep just the link.
# 2. `existing` — a `[label](bcfile:href)` markdown link we already
#    produced. Skipped verbatim so re-running the rewriter is idempotent.
# 3. `bpath` — a candidate file token wrapped in single backticks. The
#    whole match (incl. backticks) is replaced with the bcfile link.
# 4. `path` — a candidate file token NOT wrapped in backticks. `lines`
#    captures an optional `:N` or `:N-M` line range; `ext` is the
#    extension.
_FILE_RE = re.compile(
    r"(?P<existing_bt>`\[[^\]\n]+\]\(bcfile:[^)\s]+\)`)"
    r"|(?P<existing>\[[^\]\n]+\]\(bcfile:[^)\s]+\))"
    r"|`(?P<bpath>(?:[A-Za-z]:[\\/]|[\\/]{0,2})(?:[\w.\-]+[\\/])*[\w.\-]+\.(?P<bext>[A-Za-z0-9]{1,8}))(?::(?P<blines>\d+(?:-\d+)?))?`"
    r"|(?<![\w.\\/+-])(?P<path>(?:[A-Za-z]:[\\/]|[\\/]{0,2})(?:[\w.\-]+[\\/])*[\w.\-]+\.(?P<ext>[A-Za-z0-9]{1,8}))"
    r"(?::(?P<lines>\d+(?:-\d+)?))?"
)


# Recognizes a Windows drive-qualified absolute path (`C:\` or `C:/`).
_WIN_ABS_RE = re.compile(r"[A-Za-z]:[\\/]")


def _is_absolute(path: str) -> bool:
    """True for POSIX (`/x`), Windows drive (`C:\\x`, `C:/x`) and UNC
    (`\\\\server\\share`) absolute paths. A bare drive-relative `C:x`
    never reaches here — `_FILE_RE` only matches a drive prefix when a
    separator follows it, so such a token is captured as its relative
    tail instead and resolved against cwd."""
    return (
        path.startswith("/")
        or path.startswith("\\")
        or bool(_WIN_ABS_RE.match(path))
    )


class _ExistsCache:
    """Bounded existence cache, keyed by absolute path. Resolves the same
    path repeatedly across many events without re-statting the filesystem.
    Values are booleans; eviction is LRU-ish via dict-insertion order.
    """
    _MAX_ENTRIES = 4096

    def __init__(self) -> None:
        self._d: dict[str, bool] = {}

    def exists(self, abs_path: str) -> bool:
        cached = self._d.get(abs_path)
        if cached is not None:
            return cached
        try:
            ok = os.path.isfile(abs_path)
        except OSError:
            ok = False
        if len(self._d) >= self._MAX_ENTRIES:
            # Drop the oldest 25%.
            drop = list(self._d.keys())[: self._MAX_ENTRIES // 4]
            for k in drop:
                self._d.pop(k, None)
        self._d[abs_path] = ok
        return ok

    def invalidate_path(self, abs_path: str) -> None:
        self._d.pop(abs_path, None)


_cache = _ExistsCache()


class _CwdPathCache:
    _MAX_ENTRIES = 256

    def __init__(self) -> None:
        self._d: dict[str, Path] = {}

    def resolve(self, cwd: str) -> Path:
        cached = self._d.get(cwd)
        if cached is not None:
            return cached
        resolved = Path(cwd).resolve()
        if len(self._d) >= self._MAX_ENTRIES:
            drop = list(self._d.keys())[: self._MAX_ENTRIES // 4]
            for key in drop:
                self._d.pop(key, None)
        self._d[cwd] = resolved
        return resolved


_cwd_path_cache = _CwdPathCache()


# ─── Extension tag rules (declarative, auto-reverting) ──────────────────
#
# Installed/enabled extensions can declare `applied_config.tag_rules`: tags
# like NEEDS_USER_DECISION that the agent wraps around user-visible prose.
# The core strips the wrapper from the rendered text and optionally styles
# the inner text. This registry is a disposable in-memory projection of the
# enabled-extension set (single source of truth = extensions.json); it is
# rebuilt on every enable/disable and on backend startup by
# `extension_applied_config`.
#
# A rule: {"tag": "NEEDS_USER_DECISION", "bold": True, "font_scale": 1.3,
# "highlight": {"color": "#ff8c00", "alpha": 0.18}}.
# All styling (bold, font scaling, background highlight) is carried as a
# frontend-recognized sentinel span the conversation renderer maps to CSS —
# never raw HTML (the markdown pipeline escapes raw HTML by design) and never
# raw markdown like **…**, which would collide with markdown the agent already
# wrote inside the tag and corrupt the emphasis parse.

_tag_rules: dict[str, dict] = {}
_tag_scan_re: Optional[re.Pattern[str]] = None

# Sentinel the frontend conversation renderer recognizes to style inner text
# (font scale + transparent background highlight). Attrs are `key=value`
# joined by `;`: `s=SCALE` and `bg=HEX` / `a=ALPHA`. Kept ASCII + bracketed
# so it round-trips through markdown untouched and is trivially matched by a
# frontend component override.
_STYLE_SENTINEL_OPEN = "⁣[[bcstyle:{attrs}]]"
_STYLE_SENTINEL_CLOSE = "[[/bcstyle]]⁣"


def _style_attrs(rule: dict) -> str:
    """Serialize a rule's inline-style attrs into the bcstyle sentinel's
    attribute string. Only emits attrs the rule actually declares."""
    parts: list[str] = []
    if rule.get("bold"):
        parts.append("b=1")
    scale = rule.get("font_scale")
    if isinstance(scale, (int, float)) and scale and scale != 1:
        parts.append(f"s={scale}")
    highlight = rule.get("highlight")
    if isinstance(highlight, dict):
        color = highlight.get("color")
        if isinstance(color, str) and color:
            parts.append(f"bg={color}")
        alpha = highlight.get("alpha")
        if isinstance(alpha, (int, float)):
            parts.append(f"a={alpha}")
    return ";".join(parts)


def set_tag_rules(rules: list[dict]) -> None:
    """Replace the whole tag-rule registry atomically. `rules` is the merged
    set across every enabled extension; an empty list disables the pass
    entirely (fast-path returns input unchanged)."""
    global _tag_rules, _tag_scan_re
    by_tag: dict[str, dict] = {}
    for r in rules:
        tag = r.get("tag")
        if isinstance(tag, str) and tag:
            by_tag[tag] = r
    _tag_rules = by_tag
    if by_tag:
        alt = "|".join(re.escape(t) for t in by_tag)
        _tag_scan_re = re.compile(rf"<({alt})>(.*?)</\1>", re.DOTALL)
    else:
        _tag_scan_re = None


def _apply_tag_rules(text: str) -> str:
    """Strip declared tag wrappers and apply their styling. Hot path: a
    cheap fast-path bails before any regex work when no rules are
    registered or the text cannot contain a tag."""
    if "<" not in text:
        return text
    text = strip_session_name_tag(text)
    if not _tag_scan_re:
        return text

    def _sub(m: re.Match[str]) -> str:
        rule = _tag_rules.get(m.group(1)) or {}
        inner = m.group(2)
        if not rule.get("strip_wrapper", True):
            return m.group(0)
        inner = inner.strip()
        attrs = _style_attrs(rule)
        if attrs:
            inner = _STYLE_SENTINEL_OPEN.format(attrs=attrs) + inner + _STYLE_SENTINEL_CLOSE
        return inner

    return _tag_scan_re.sub(_sub, text)


# Core (non-extension) session-name tag: the agent wraps a proposed session
# name in <SESSION_NAME>…</SESSION_NAME>. Detection runs on RAW provider text
# (orchs/base.apply_event) and triggers a session rename; the whole tag —
# including its inner text — is stripped from the rendered message since the
# name is metadata, not prose.
_SESSION_NAME_TAG = "SESSION_NAME"
_SESSION_NAME_RE = re.compile(
    rf"<{_SESSION_NAME_TAG}>(.*?)</{_SESSION_NAME_TAG}>\n?", re.DOTALL
)


def extract_session_name(text: str) -> Optional[str]:
    """First ``<SESSION_NAME>…</SESSION_NAME>`` inner text in RAW provider
    text, or None. Must run pre-strip — `_apply_tag_rules` removes the tag."""
    if not text or f"<{_SESSION_NAME_TAG}>" not in text:
        return None
    m = _SESSION_NAME_RE.search(text)
    if not m:
        return None
    name = m.group(1).strip()
    return name or None


def strip_session_name_tag(text: str) -> str:
    if f"<{_SESSION_NAME_TAG}>" not in text:
        return text
    return _SESSION_NAME_RE.sub("", text)


def tag_names() -> frozenset[str]:
    """Currently-registered tag names."""
    return frozenset(_tag_rules)


def detect_markers(text: str) -> list[tuple[str, dict]]:
    """Return ``(extension_id, marker)`` for every marker-bearing tag whose
    opening ``<TAG>`` appears in RAW (pre-strip) text. The detection MUST run
    on raw provider text — once ``_apply_tag_rules`` strips the wrapper the
    signal is gone, so the render-tree copy can no longer be scanned."""
    if not _tag_rules or "<" not in text:
        return []
    out: list[tuple[str, dict]] = []
    for tag, rule in _tag_rules.items():
        marker = rule.get("marker")
        if marker and f"<{tag}>" in text:
            # New dict — never mutate the shared rule["marker"] ref. The tag
            # rides the projection so consumers (status sort) classify by tag,
            # not by drifting color/tooltip.
            out.append((rule.get("_extension_id", ""), {**marker, "tag": tag}))
    return out


def invalidate_path(abs_path: str) -> None:
    """Drop cached existence for a path (call when a file is created /
    deleted via the editor). Best-effort."""
    _cache.invalidate_path(abs_path)


def assume_exists_for_session(sess: Optional[dict]) -> bool:
    """Single home for the rule: sessions hosted on a worker-node skip
    the local-disk existence check (their files live on the node)."""
    node_id = (sess or {}).get("node_id") or "primary"
    return assume_exists_for_node(node_id)


def assume_exists_for_node(node_id: Optional[str]) -> bool:
    node_id = node_id or "primary"
    if node_id == "primary":
        return False
    try:
        from topology import local_node_id
        return node_id != local_node_id()
    except Exception:
        return True


def _build_link(label: str, abs_path: str, lines: Optional[str]) -> str:
    qpath = quote(abs_path, safe="/:")
    href = f"bcfile:{qpath}"
    if lines:
        href = f"{href}?L={lines}"
    # Escape `]` in the label so we don't break markdown link parsing.
    safe_label = label.replace("]", r"\]")
    return f"[{safe_label}]({href})"


def rewrite_text(
    text: str, cwd: Optional[str], *, assume_exists: bool = False,
) -> str:
    """Replace recognized file refs in `text` with bcfile: markdown links
    when the referenced file exists. `cwd` is the absolute project root
    (typically the session's cwd); when None, only absolute matches are
    checked.

    `assume_exists=True` skips the on-disk existence check — used for
    sessions hosted on a worker-node, whose files live on the node's
    filesystem, not this one. The regex + extension allowlist still
    gate what becomes a link; the file viewer resolves the path on the
    session's node when the user clicks."""
    if not text or not isinstance(text, str):
        return text
    if "." not in text:  # cheap negative early-out
        return text

    cwd_path = _cwd_path_cache.resolve(cwd) if cwd else None

    def _sub(m: re.Match[str]) -> str:
        if m.group("existing_bt"):
            # Strip wrapping backticks; keep just the link.
            return m.group(0)[1:-1]
        if m.group("existing"):
            return m.group(0)
        # Pick the right alternative: backticked or bare. The label of
        # the rendered link is the path+lines portion only — the
        # backticks (when present) are consumed so the link can render
        # outside <code>.
        if m.group("bpath") is not None:
            path = m.group("bpath")
            ext = (m.group("bext") or "").lower()
            lines = m.group("blines")
            label_token = path + (f":{lines}" if lines else "")
        elif m.group("path") is not None:
            path = m.group("path")
            ext = (m.group("ext") or "").lower()
            lines = m.group("lines")
            label_token = m.group(0)
        else:
            return m.group(0)
        if ext not in _EXT_ALLOWLIST:
            return m.group(0)
        if _is_absolute(path):
            abs_path = path
        else:
            if cwd_path is None:
                return m.group(0)
            abs_path = str(cwd_path / path)
        if not assume_exists and not _cache.exists(abs_path):
            return m.group(0)
        return _build_link(label_token, abs_path, lines)

    return _FILE_RE.sub(_sub, text)


# ─── Event-shape-aware rewriting ────────────────────────────────────────
#
# `event_ingester` ingests events with varying `data` shapes. We only
# touch fields known to carry user-visible prose; tool inputs and other
# machine-readable fields are left untouched so we never corrupt a
# round-tripped command or argument.

_TEXT_FIELD_TYPES = {"text", "thinking", "tool_result"}


def _rewrite_content_blocks(
    blocks: Iterable, cwd: Optional[str], *, assume_exists: bool = False,
) -> None:
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and isinstance(block.get("text"), str):
            block["text"] = _apply_tag_rules(rewrite_text(
                block["text"], cwd, assume_exists=assume_exists))
        elif btype == "thinking" and isinstance(block.get("thinking"), str):
            block["thinking"] = rewrite_text(
                block["thinking"], cwd, assume_exists=assume_exists)
        elif btype == "tool_result":
            content = block.get("content")
            if isinstance(content, str):
                block["content"] = rewrite_text(
                    content, cwd, assume_exists=assume_exists)
            elif isinstance(content, list):
                _rewrite_content_blocks(
                    content, cwd, assume_exists=assume_exists)


def rewrite_event_data(
    event_type: str, data: dict, cwd: Optional[str],
    *, assume_exists: bool = False,
) -> dict:
    """Mutates and returns `data` with file refs in user-visible text
    fields rewritten to bcfile: markdown links. Safe to call on any event;
    unknown shapes are left untouched. `assume_exists` — see
    `rewrite_text` (node-hosted sessions skip the local disk check)."""
    if not isinstance(data, dict):
        return data

    if event_type == "manager_event":
        inner = data.get("event")
        if isinstance(inner, dict):
            inner_type = inner.get("type", "")
            inner_data = inner.get("data")
            if isinstance(inner_data, dict):
                rewrite_event_data(
                    inner_type, inner_data, cwd, assume_exists=assume_exists)
        return data

    if event_type == "agent_message":
        message = data.get("message")
        if isinstance(message, dict):
            _rewrite_content_blocks(
                message.get("content"), cwd, assume_exists=assume_exists)
        return data

    # Legacy / orchestrator-emitted output frames.
    for key in ("text", "output", "thought", "error", "content"):
        val = data.get(key)
        if isinstance(val, str):
            data[key] = rewrite_text(val, cwd, assume_exists=assume_exists)
    return data


def _isolate_content_blocks(blocks: list) -> list:
    """Shallow-copy each content block dict so rewrites that reassign a
    block field (`text`, `thinking`, or string `content`) land on owned
    objects, and recurse into `tool_result.content` lists (rewritten in
    place by `_rewrite_content_blocks`). Shares immutable leaf values."""
    out = []
    for block in blocks:
        if isinstance(block, dict):
            block = dict(block)
            if block.get("type") == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    block["content"] = _isolate_content_blocks(inner)
        out.append(block)
    return out


def _isolate_for_rewrite(event_type: str, data: dict) -> dict:
    """Narrow copy-on-write of exactly the containers `rewrite_event_data`
    mutates, sharing the rest of the payload by reference. This replaces a
    full `copy.deepcopy(data)` on the per-event ingest path, which copied
    entire large payloads (multi-MB worker/message transcripts) just to
    protect a few leaf strings and blocked the asyncio loop for seconds.

    MUST stay in lockstep with `rewrite_event_data`'s mutation set: the
    top-level shallow copy covers the legacy `text/output/thought/error/
    content` reassignments; the `agent_message` branch copies
    `message -> content blocks`; the `manager_event` branch copies
    `event -> inner data` and recurses. If `rewrite_event_data` gains a
    new mutated field, extend this copier too."""
    top = dict(data)
    if event_type == "manager_event":
        inner = top.get("event")
        if isinstance(inner, dict):
            inner = dict(inner)
            top["event"] = inner
            inner_data = inner.get("data")
            if isinstance(inner_data, dict):
                inner["data"] = _isolate_for_rewrite(
                    inner.get("type", ""), inner_data,
                )
        return top
    if event_type == "agent_message":
        message = top.get("message")
        if isinstance(message, dict):
            message = dict(message)
            top["message"] = message
            content = message.get("content")
            if isinstance(content, list):
                message["content"] = _isolate_content_blocks(content)
        return top
    return top


def rewrite_event_data_isolated(
    event_type: str, data: dict, cwd: Optional[str],
    *, assume_exists: bool = False,
) -> dict:
    """Isolated variant of `rewrite_event_data`: returns rewritten data
    WITHOUT mutating the caller's `data`, using narrow copy-on-write
    (`_isolate_for_rewrite`) instead of a full deepcopy. Used on the ingest
    hot path where the caller's live event feeds the render tree / WS /
    dedup and must not be mutated."""
    if not isinstance(data, dict):
        return data
    isolated = _isolate_for_rewrite(event_type, data)
    rewrite_event_data(event_type, isolated, cwd, assume_exists=assume_exists)
    return isolated


# ─── One-time migration ─────────────────────────────────────────────────
#
# Rewrites historical events on disk in-place so existing sessions get
# bcfile: links without waiting for new turns. Two surfaces:
#   1. Session JSON files (`<ba_home>/sessions/*.json`) — message
#      `content` strings + each `events[].data` payload + recursively
#      every embedded fork's messages/events.
#   2. Per-root events JSONL files beside each session root.
# Run once; gated by a sentinel file so a backend restart doesn't
# repeat the work. The resolver itself is idempotent, so an interrupted
# migration can be safely resumed by removing the sentinel.

_MIGRATION_SENTINEL = "bcfile_migrated_v2"


def _migrate_message_node(msg: dict, cwd: Optional[str]) -> bool:
    """Rewrite a single message dict in place. Returns True if changed."""
    changed = False
    content = msg.get("content")
    if isinstance(content, str):
        new = rewrite_text(content, cwd)
        if new != content:
            msg["content"] = new
            changed = True
    events = msg.get("events")
    if isinstance(events, list):
        for ev in events:
            if not isinstance(ev, dict):
                continue
            etype = ev.get("type") or ""
            edata = ev.get("data")
            if isinstance(edata, dict):
                before = repr(edata)
                rewrite_event_data(etype, edata, cwd)
                if repr(edata) != before:
                    changed = True
    return changed


def _migrate_session_node(node: dict) -> bool:
    """Recursively rewrite a session node + all embedded forks. Returns
    True if any text changed."""
    changed = False
    cwd = node.get("cwd") if isinstance(node.get("cwd"), str) else None
    msgs = node.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if isinstance(m, dict) and _migrate_message_node(m, cwd):
                changed = True
    forks = node.get("forks")
    if isinstance(forks, list):
        for fk in forks:
            if isinstance(fk, dict) and _migrate_session_node(fk):
                changed = True
    return changed


def _atomic_write_tmp(path: Path, text: str) -> None:
    """Atomic write via `.bcfile.tmp` sibling + `os.replace`. The tmp
    is unlinked in `finally` so a crash mid-write doesn't leave debris.
    INVARIANT: same recipe used by both migration helpers in this
    module — they must not diverge in atomicity guarantees."""
    tmp = path.with_suffix(path.suffix + ".bcfile.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _migrate_session_file(path: Path) -> bool:
    """Atomically rewrite a single session JSON file. Returns True iff
    the file was modified."""
    import json
    try:
        raw = path.read_text(encoding="utf-8")
        node = json.loads(raw)
    except Exception:
        return False
    if not isinstance(node, dict):
        return False
    if not _migrate_session_node(node):
        return False
    _atomic_write_tmp(path, json.dumps(node, ensure_ascii=False))
    return True


def _migrate_events_jsonl(
    root_id: str, path: Path, cwd: Optional[str],
) -> bool:
    """Atomically rewrite a per-root events.jsonl. Returns True iff any
    line changed."""
    import json
    import hydration_index_store

    with hydration_index_store.journal_guard(root_id, path):
        if not path.exists():
            return False
        out_lines: list[str] = []
        changed = False
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.rstrip("\n")
                if not stripped.strip():
                    out_lines.append(line)
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    out_lines.append(line)
                    continue
                etype = entry.get("type") or ""
                edata = entry.get("data")
                if isinstance(edata, dict):
                    before = repr(edata)
                    rewrite_event_data(etype, edata, cwd)
                    if repr(edata) != before:
                        changed = True
                        entry["data"] = edata
                        out_lines.append(
                            json.dumps(entry, ensure_ascii=False) + "\n"
                        )
                        continue
                out_lines.append(line)
        if not changed:
            return False
        _atomic_write_tmp(path, "".join(out_lines))
        hydration_index_store.invalidate(root_id, path)
        return True


def migrate_all(ba_home_dir: Path) -> dict:
    """Walk every session JSON + per-root events.jsonl under `ba_home_dir`
    and rewrite recognized file refs to bcfile: links. Idempotent (safe
    to re-run). Returns {"sessions_changed", "events_files_changed"}."""
    import json
    sessions_changed = 0
    events_changed = 0

    # Build cwd-by-root-id index from the JSON files first so we have it
    # before rewriting the per-root events.jsonl files.
    cwd_by_root: dict[str, Optional[str]] = {}

    from session_store import _session_json_files
    session_files = list(_session_json_files())
    if not session_files:
        return {"sessions_changed": 0, "events_files_changed": 0}
    for jpath in session_files:
        try:
            node = json.loads(jpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(node, dict):
            continue
        rid = node.get("id") or jpath.stem
        cwd = node.get("cwd")
        cwd_by_root[str(rid)] = cwd if isinstance(cwd, str) else None
        # Walk forks for their cwds too (forks usually inherit, but each
        # carries its own field).
        def _collect_forks(n: dict) -> None:
            for fk in n.get("forks", []) or []:
                if not isinstance(fk, dict):
                    continue
                fid = fk.get("id")
                if fid:
                    cwd_by_root.setdefault(
                        str(fid),
                        fk.get("cwd") if isinstance(fk.get("cwd"), str) else cwd,
                    )
                _collect_forks(fk)
        _collect_forks(node)

    for jpath in session_files:
        try:
            if _migrate_session_file(jpath):
                sessions_changed += 1
        except Exception:
            continue

    for jpath in session_files:
        sub = jpath.parent / jpath.stem
        events_path = sub / "events.jsonl"
        if not events_path.exists():
            continue
        cwd = cwd_by_root.get(sub.name)
        try:
            if _migrate_events_jsonl(sub.name, events_path, cwd):
                events_changed += 1
        except Exception:
            continue

    return {
        "sessions_changed": sessions_changed,
        "events_files_changed": events_changed,
    }


def run_migration_once(ba_home_dir: Path) -> Optional[dict]:
    """Idempotent entry-point. Skips if the sentinel exists. Returns the
    migration stats on first run, None when already done."""
    sessions_dir = ba_home_dir / "sessions"
    sentinel = sessions_dir / _MIGRATION_SENTINEL
    if sentinel.exists():
        return None
    sessions_dir.mkdir(parents=True, exist_ok=True)
    stats = migrate_all(ba_home_dir)
    try:
        sentinel.write_text("done", encoding="utf-8")
    except OSError:
        pass
    return stats
