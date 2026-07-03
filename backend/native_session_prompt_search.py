"""Raw provider-native transcript search.

Generalized grep over EVERY provider's native transcripts (Claude
``projects/<cwd>/<sid>.jsonl``, Codex ``~/.codex/sessions`` rollouts, Gemini
``~/.gemini/tmp`` chats, and the Better-Agent run-dir ``session_events.jsonl``).
Each provider is read by its own element extractor
(:func:`native_session_miner._claude_elements` / ``_codex_elements`` /
``_gemini_elements``) — the only provider-specific code — emitting a shared
:class:`NativeElement` stream. Everything else (discovery, token-overlap grep,
dedup, ranking, the :class:`Categorizer`) is provider-agnostic and reused
across all four.

Entry points all share one optimized core (:func:`_search_elements`):

- :func:`search_in_native_session_transcript` — grep ANYTHING in the transcript
  (prompts, replies, reasoning, tool calls, tool results, …), categorized.
- :func:`search_native_session_prompts` / :func:`search_native_session_transcripts`
  — thin category-filtered facades (prompt-only / prompt+reply).

Discovery (cheap) is separated from parsing (expensive: read + JSON-parse each
transcript). Parsing + matching runs concurrently across a thread pool, since
the workload is I/O bound over many small transcript files. Public requirements
lookup does not use this as a success fallback; requirements still come from
the LLM processor.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from native_elements import (
    NativeCandidate,
    _codex_first_cwd,
    _decode_cwd_token,
    _mtime,
    _windsurf_cascade_roots,
)
from native_session_miner import _ba_session_cwd, iter_all_native_candidates
from paths import encode_cwd

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MAX_WORKERS = min(32, (os.cpu_count() or 4) * 4)
_RG_TIMEOUT_SECONDS = 120

# Common English function words carry no query signal — a prompt matching only
# one of these would score 1 on pure noise, so they are dropped from the tokens.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "for",
    "how", "if", "in", "is", "it", "me", "my", "no", "of", "on", "or", "so",
    "that", "the", "this", "to", "up", "was", "we", "with", "you", "your",
})


def _candidates(allowed: set[str]) -> list[NativeCandidate]:
    """Filesystem-first discovery across all providers, cwd-filtered. The
    pure-Python fallback used when ``rg`` is unavailable; otherwise
    :func:`_matched_candidates` short-circuits this with an ``rg`` pass that
    returns only files containing a needle, so the 55k-file full walk never
    runs for a real query."""
    allowed_encoded = {encode_cwd(c) for c in allowed}
    out: list[NativeCandidate] = []
    for candidate in iter_all_native_candidates():
        if not allowed:
            out.append(candidate)
            continue
        if candidate.cwd in allowed or encode_cwd(candidate.cwd) in allowed_encoded:
            out.append(candidate)
    return out


def _cwd_ok(cwd: str, allowed: set[str], allowed_encoded: set[str]) -> bool:
    if not allowed:
        return True
    return cwd in allowed or encode_cwd(cwd) in allowed_encoded


# ─── rg match-first filter ─────────────────────────────────────────────────
# rg greps the raw transcripts natively (SIMD, parallel) and returns only the
# files containing a needle token. We then build NativeCandidates — resolving
# sid/cwd/format from the matched PATH — and run the expensive parse only on
# those few files. This inverts the old "discover 55k → parse all" flow: match
# first, resolve metadata only for matches. Discovery (the ~69s codex-cwd walk)
# never runs on the rg path.

def _native_roots() -> list[tuple[Path, str]]:
    """(root, format-tag) for every native transcript store. ``runs`` is tagged
    distinctly because its cwd/sid come from ``state.json`` even though the
    transcript itself is Claude-shaped. Root helpers are resolved through the
    module so tests can monkeypatch them."""
    import native_session_miner as nm
    roots: list[tuple[Path, str]] = []
    for projects in nm._claude_projects_roots():
        roots.append((projects, "claude"))
    codex = nm._codex_sessions_root()
    if codex.exists():
        roots.append((codex, "codex"))
    gemini = nm._gemini_chats_root()
    if gemini.exists():
        roots.append((gemini, "gemini"))
    for windsurf in nm._windsurf_cascade_roots():
        roots.append((windsurf, "windsurf"))
    runs = nm._runs_root()
    if runs.exists():
        roots.append((runs, "runs"))
    return roots


def _classify_root(path: Path, roots: list[tuple[Path, str]]) -> str:
    for root, tag in roots:
        try:
            path.relative_to(root)
            return tag
        except ValueError:
            continue
    return "claude"


def _is_native_transcript_path(path: Path, tag: str) -> bool:
    if tag == "runs":
        return path.name == "session_events.jsonl"
    if tag == "windsurf":
        return path.suffix == ".pb"
    return path.suffix == ".jsonl"


def _rg_filter(tokens: list[str]) -> list[tuple[Path, str]] | None:
    """Run ripgrep over every native root for the query tokens; return the
    matched ``(path, format-tag)`` pairs, or ``None`` to signal the caller must
    fall back to pure-Python discovery (rg missing / errored).

    The pattern is a whole-word alternation of ``re.escape``'d tokens; it is a
    safe SUPerset of the authoritative Python ``\\b`` match that
    :func:`_match_elements` applies later, so a loose rg word-boundary edge case
    can only over-include, never drop a real hit."""
    if not tokens:
        return None
    rg = shutil.which("rg")
    if not rg:
        return None
    roots = [(root, tag) for root, tag in _native_roots() if tag != "windsurf"]
    if not roots:
        return None
    pattern = "|".join(re.escape(t) for t in tokens)
    # --max-count 1: one match per file is enough — we only need the PATH (we
    # re-parse the file ourselves); without it a common token emits a JSON
    # record per occurrence across tens of thousands of files, dwarfing the grep.
    argv = [rg, "--json", "-i", "-w", "--max-count", "1", "--no-messages",
            "-g", "*.jsonl", "-e", pattern, *[str(r) for r, _ in roots]]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=_RG_TIMEOUT_SECONDS)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):
        return None
    matched: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for line in proc.stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "match":
            continue
        path = (obj.get("data") or {}).get("path", {}).get("text")
        if not isinstance(path, str) or path in seen:
            continue
        tag = _classify_root(Path(path), roots)
        if not _is_native_transcript_path(Path(path), tag):
            continue
        seen.add(path)
        matched.append((Path(path), tag))
    return matched


def _runs_app_session_id(run_dir: Path) -> str:
    try:
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    aid = state.get("app_session_id")
    return aid if isinstance(aid, str) else ""


def _candidate_from_match(path: Path, tag: str) -> NativeCandidate:
    """Resolve a NativeCandidate from a matched PATH — cheap, no full parse.
    codex cwd needs the first ``session_meta`` line, but only for the handful of
    matched codex files, not all 30k."""
    if tag == "codex":
        return NativeCandidate(key=f"codex-rg:{path.name}", sid=path.stem,
                               cwd=_codex_first_cwd(path), data={}, transcript=path,
                               mtime=_mtime(path), format="codex")
    if tag == "gemini":
        cwd = _decode_cwd_token(path.parent.parent.name)  # tmp/<enc>/chats/<file>
        return NativeCandidate(key=f"gemini-rg:{path.name}", sid=path.stem, cwd=cwd,
                               data={}, transcript=path, mtime=_mtime(path), format="gemini")
    if tag == "runs":
        run_dir = path.parent
        sid = _runs_app_session_id(run_dir) or run_dir.name
        return NativeCandidate(key=f"run-rg:{run_dir.name}", sid=sid,
                               cwd=_ba_session_cwd(sid), data={}, transcript=path,
                               mtime=_mtime(path), format="claude")
    if tag == "windsurf":
        source = "windsurf" if path.parent.parent.name == "windsurf" else "codeium"
        return NativeCandidate(key=f"windsurf-rg:{source}/{path.stem}", sid=path.stem,
                               cwd="", data={}, transcript=path,
                               mtime=_mtime(path), format="windsurf")
    cwd = _decode_cwd_token(path.parent.name)  # projects/<enc>/<file>
    return NativeCandidate(key=f"claude-rg:{path.name}", sid=path.stem, cwd=cwd,
                           data={}, transcript=path, mtime=_mtime(path), format="claude")


def _windsurf_candidates(allowed: set[str]) -> list[NativeCandidate]:
    if allowed:
        return []
    candidates: list[NativeCandidate] = []
    for root in _windsurf_cascade_roots():
        source = "windsurf" if root.parent.name == "windsurf" else "codeium"
        for transcript in root.glob("*.pb"):
            candidates.append(NativeCandidate(
                key=f"windsurf-rg:{source}/{transcript.stem}",
                sid=transcript.stem,
                cwd="",
                data={},
                transcript=transcript,
                mtime=_mtime(transcript),
                format="windsurf",
            ))
    return candidates


def _matched_candidates(tokens: list[str], allowed: set[str]) -> list[NativeCandidate]:
    """Match-first candidate resolution.

    Fast path: the native FTS5 index (:mod:`native_transcript_index`) once it
    has caught up (covered). If covered but momentarily stale, REQUEST a steady
    refresh and wait for it (stat indexed paths + parse changed known files)
    rather than dropping to rg. New external transcript files are picked up by
    the indexer's periodic full reconcile or by the rg fallback when the index
    is not covered.

    Fallback: ``rg`` when the index is not covered (cold start) or the wait
    times out / fails. ``rg`` narrows to files containing a needle; we build
    candidates from those paths only, then cwd-filter. Falls back further to
    :func:`_candidates` (full discovery) when ``rg`` is unavailable."""
    if tokens:
        try:
            import native_transcript_index as _idx
            if _idx.is_covered():
                if not _idx.is_usable():
                    _idx.request_refresh()
                    _idx.wait_fresh()
                if _idx.is_usable():
                    hits = _idx.match_paths(tokens, allowed)
                    if hits is not None:
                        return [_candidate_from_match(p, tag) for p, tag in hits]
        except Exception:
            pass
    hits = _rg_filter(tokens)
    if hits is None:
        return _candidates(allowed)
    allowed_encoded = {encode_cwd(c) for c in allowed}
    candidates = [
        c for c in (_candidate_from_match(p, tag) for p, tag in hits)
        if _cwd_ok(c.cwd, allowed, allowed_encoded)
    ]
    candidates.extend(_windsurf_candidates(allowed))
    return candidates


def _query_tokens(query: str) -> list[str]:
    return [
        tok
        for tok in _TOKEN_RE.findall(query.lower())
        if len(tok) >= 2 and tok not in _STOPWORDS
    ]


def _token_patterns(tokens: list[str]) -> list[re.Pattern]:
    """One whole-word matcher per token so ``in`` matches the word ``in`` and
    not the substring inside ``building``."""
    return [re.compile(r"\b" + re.escape(tok) + r"\b") for tok in tokens]


class ElementCategory:
    """Semantic categories the :class:`Categorizer` maps elements to. Higher
    level than the structural ``element_kind`` — e.g. a ``tool_call`` element
    becomes ``file_edit`` / ``shell`` / ``file_read`` / ``search`` / ``subagent``
    depending on the tool."""

    PROMPT = "prompt"
    REPLY = "reply"
    REASONING = "reasoning"
    FILE_EDIT = "file_edit"
    SHELL = "shell"
    FILE_READ = "file_read"
    SEARCH = "search"
    SUBAGENT = "subagent"
    TOOL_OUTPUT = "tool_output"
    ERROR = "error"
    COMMAND = "command"
    META = "meta"
    OTHER = "other"


_EDIT_TOOLS = frozenset({
    "edit", "multiedit", "write", "notebookedit", "replace", "write_file",
    "apply_patch", "create_file", "str_replace_editor", "federated_write",
})
_SHELL_TOOLS = frozenset({"bash", "shell", "exec_command", "run", "execute", "terminal", "computer"})
_READ_TOOLS = frozenset({"read", "read_file", "view"})
_SEARCH_TOOLS = frozenset({"grep", "glob", "websearch", "toolsearch", "search", "search_files", "webfetch"})
_AGENT_TOOLS = frozenset({"task", "agent", "spawn_agent", "delegate", "delegate_task", "spawnagent"})
# Tool outputs that read like a failure surface as the ERROR category.
_ERROR_RE = re.compile(
    r"\b(traceback|exception|error:|errno|failed|command not found|exited with|fatal)\b",
    re.I,
)


class Categorizer:
    """Provider-agnostic element → category mapping.

    Operates only on the shared :class:`NativeElement` shape (structural
    ``kind``, ``tool_name``, ``text``), so adding a provider never touches this
    — only its extractor. Tool names are matched case-insensitively after
    ``-``/``_``/``/`` normalization so ``WebSearch``, ``exec_command``, and
    ``str_replace_editor`` all resolve regardless of provider casing."""

    def categorize(self, element) -> str:
        kind = element.kind
        if kind == "user_prompt":
            return ElementCategory.PROMPT
        if kind == "command":
            return ElementCategory.COMMAND
        if kind == "assistant_text":
            return ElementCategory.REPLY
        if kind == "reasoning":
            return ElementCategory.REASONING
        if kind == "meta":
            return ElementCategory.META
        if kind == "tool_call":
            return self._tool_category(element.tool_name)
        if kind == "tool_result":
            return ElementCategory.ERROR if _ERROR_RE.search(element.text) else ElementCategory.TOOL_OUTPUT
        return ElementCategory.OTHER

    @staticmethod
    def _tool_category(tool_name: str) -> str:
        norm = re.sub(r"[-/_]", "_", (tool_name or "").lower())
        if norm in _EDIT_TOOLS:
            return ElementCategory.FILE_EDIT
        if norm in _SHELL_TOOLS:
            return ElementCategory.SHELL
        if norm in _READ_TOOLS:
            return ElementCategory.FILE_READ
        if norm in _SEARCH_TOOLS:
            return ElementCategory.SEARCH
        if norm in _AGENT_TOOLS:
            return ElementCategory.SUBAGENT
        return ElementCategory.OTHER


def _match_elements(
    candidate: NativeCandidate,
    patterns: list[re.Pattern],
    is_noise: Callable[[str], bool] | None,
    categorizer: Categorizer,
    categories: frozenset[str] | None,
    kinds: frozenset[str] | None,
    record_kind: str,
    source: str,
) -> list[tuple[int, dict[str, Any]]]:
    """Parse one transcript to its element stream and score each element by
    distinct whole-word token hits, after category/kind/noise filtering. A
    single bad transcript must not sink the whole concurrent search, so any
    parse/scoring error is contained to this candidate."""
    try:
        elements = candidate.parse_elements()
    except Exception:
        return []
    scored: list[tuple[int, dict[str, Any]]] = []
    for element in elements:
        text = element.text.strip()
        if not text or (is_noise is not None and is_noise(text)):
            continue
        if kinds is not None and element.kind not in kinds:
            continue
        category = categorizer.categorize(element)
        if categories is not None and category not in categories:
            continue
        score = sum(1 for pattern in patterns if pattern.search(text.lower()))
        if score == 0:
            continue
        scored.append((score, {
            "text": text,
            "role": element.role,
            "kind": record_kind,
            "source": source,
            "category": category,
            "element_kind": element.kind,
            "tool_name": element.tool_name,
            "cwd": candidate.cwd,
            "sid": candidate.sid,
            "ts": element.timestamp,
        }))
    return scored


def _search_elements(
    *,
    query: str,
    record_kind: str,
    source: str,
    cwds: tuple[str, ...] | list[str] = (),
    categories: tuple[str, ...] | list[str] | None = None,
    kinds: tuple[str, ...] | list[str] | None = None,
    max_matches: int | None = 20,
    is_noise: Callable[[str], bool] | None = None,
    categorizer: Categorizer | None = None,
) -> list[dict[str, Any]]:
    """Single optimized fan-out grep over the raw provider-native transcripts at
    the ELEMENT level — the one core every public entry point reuses.

    ``categories`` / ``kinds`` optionally restrict matches to a semantic category
    set (``ElementCategory``) and/or a structural element-kind set. ``None`` means
    no restriction on that axis (``search_in_native_session_transcript`` passes
    both ``None`` to grep everything). ``record_kind`` / ``source`` label the
    records so consumers can tell scopes apart. Whole-word matching, token-overlap
    ranking, cwd filter, ``is_noise``, dedup, and oldest-first presentation are
    shared by all callers."""
    tokens = _query_tokens(query)
    if not tokens:
        return []
    patterns = _token_patterns(tokens)
    allowed = {c for c in cwds if isinstance(c, str) and c.strip()}
    candidates = _matched_candidates(tokens, allowed)
    if not candidates:
        return []

    categorizer = categorizer or Categorizer()
    cat_set = frozenset(categories) if categories else None
    kind_set = frozenset(kinds) if kinds else None

    scored: list[tuple[int, dict[str, Any]]] = []
    workers = max(1, min(_MAX_WORKERS, len(candidates)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for chunk in pool.map(
            lambda candidate: _match_elements(
                candidate, patterns, is_noise, categorizer, cat_set, kind_set, record_kind, source
            ),
            candidates,
        ):
            scored.extend(chunk)

    # Select the most relevant matches (token-overlap score) up to the cap;
    # sid+text break score/ts ties so the surviving set is deterministic.
    scored.sort(
        key=lambda item: (
            item[0],
            item[1].get("ts") or "",
            item[1].get("sid") or "",
            item[1]["text"],
        ),
        reverse=True,
    )
    matches: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    for _score, record in scored:
        if record["text"] in seen_text:
            continue
        seen_text.add(record["text"])
        matches.append(record)
        if max_matches is not None and len(matches) >= max_matches:
            break
    # ...then present them oldest-first so the consuming LLM can read the
    # requirement's evolution over time (missing timestamps sort last; sid+text
    # keep that order deterministic across the many empty-ts providers).
    matches.sort(
        key=lambda m: (not m.get("ts"), m.get("ts") or "", m.get("sid") or "", m.get("text") or "")
    )
    return matches


def search_in_native_session_transcript(
    *,
    query: str,
    cwds: tuple[str, ...] | list[str] = (),
    categories: tuple[str, ...] | list[str] | None = None,
    kinds: tuple[str, ...] | list[str] | None = None,
    max_matches: int | None = 20,
    is_noise: Callable[[str], bool] | None = None,
    categorizer: Categorizer | None = None,
) -> list[dict[str, Any]]:
    """Grep ANYTHING in the raw provider-native transcripts for ``query`` —
    prompts, assistant replies, reasoning, tool calls, tool results, commands —
    and return categorized matches, highest token-overlap first.

    Each match carries ``category`` (semantic — see :class:`ElementCategory`),
    ``element_kind`` (structural), and ``tool_name`` (for tool calls/results) so
    callers can group/filter results without re-parsing. Narrow the scope with
    ``categories`` (e.g. ``("shell", "file_edit")`` for only actions) or
    ``kinds`` (e.g. ``("tool_call",)``). ``cwds`` restricts to working dirs.
    A match scores at least one query token as a whole word; the score is the
    count of distinct tokens hit."""
    return _search_elements(
        query=query,
        record_kind="native_session_element",
        source="native_transcript",
        cwds=cwds,
        categories=categories,
        kinds=kinds,
        max_matches=max_matches,
        is_noise=is_noise,
        categorizer=categorizer,
    )


def search_native_session_prompts(
    *,
    query: str,
    cwds: tuple[str, ...] | list[str] = (),
    max_matches: int | None = 20,
    is_noise: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Grep the raw provider-native transcripts for ``query`` and return the
    matching typed **user prompts**, highest token-overlap first. Thin facade
    over :func:`search_in_native_session_transcript` restricted to the
    ``prompt`` category.

    ``cwds`` (when non-empty) restricts to those working directories.
    ``is_noise`` is the caller's programmatic-preamble filter (BA-injected
    worker/processor/auditor prompts) so the fallback drops the same noise the
    main corpus does. A match keeps a prompt when at least one query token
    appears in it as a whole word; the score is the count of distinct tokens hit.
    """
    return _search_elements(
        query=query,
        record_kind="native_session_prompt",
        source="native_session_fallback",
        cwds=cwds,
        categories=(ElementCategory.PROMPT,),
        max_matches=max_matches,
        is_noise=is_noise,
    )


def search_native_session_transcripts(
    *,
    query: str,
    cwds: tuple[str, ...] | list[str] = (),
    max_matches: int | None = 20,
    is_noise: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Grep the raw provider-native transcripts for ``query`` over the **whole
    conversation** — typed user prompts and assistant replies — highest
    token-overlap first. Thin facade over
    :func:`search_in_native_session_transcript` restricted to the ``prompt`` and
    ``reply`` categories. Same ranking, cwd filter, dedup, and oldest-first
    presentation."""
    return _search_elements(
        query=query,
        record_kind="native_session_transcript",
        source="native_session_transcript",
        cwds=cwds,
        categories=(ElementCategory.PROMPT, ElementCategory.REPLY),
        max_matches=max_matches,
        is_noise=is_noise,
    )
