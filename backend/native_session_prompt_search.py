"""Raw provider-native session prompt search.

Each provider is read by its own native miner — Claude ``projects/<cwd>/<sid>.jsonl``,
Codex / Gemini / Better Agent run-dir ``session_events.jsonl`` — so this is the
"every provider with its own grepping method" fan-out. It reuses the canonical
:mod:`native_session_miner` readers (no watermark → scan every transcript) and
greps the typed user prompts for the query tokens. Public requirements lookup
does not use this as a success fallback; requirements still come from the LLM
processor.

Discovery (cheap) is separated from parsing (expensive: read + JSON-parse each
transcript). Parsing + matching runs concurrently across a thread pool, since
the workload is I/O bound over many small transcript files.
"""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from native_session_miner import (
    NativeBetterAgentSessionMiner,
    NativeCandidate,
    NativeClaudeSessionMiner,
    NativeCodexSessionMiner,
    NativeGeminiSessionMiner,
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MAX_WORKERS = min(32, (os.cpu_count() or 4) * 4)

# Common English function words carry no query signal — a prompt matching only
# one of these would score 1 on pure noise, so they are dropped from the tokens.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "for",
    "how", "if", "in", "is", "it", "me", "my", "no", "of", "on", "or", "so",
    "that", "the", "this", "to", "up", "was", "we", "with", "you", "your",
})


def _native_miners() -> list:
    """One miner per provider, each with an empty state so nothing is skipped.

    An empty watermark dict makes every native transcript a candidate — this
    search wants the whole raw corpus, not the delta since the last mining pass.
    """
    return [
        NativeClaudeSessionMiner({}),
        NativeCodexSessionMiner({}),
        NativeGeminiSessionMiner({}),
        NativeBetterAgentSessionMiner({}),
    ]


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


def _candidates(allowed: set[str]) -> list[NativeCandidate]:
    """Cheap discovery across all providers, cwd-filtered before any parse."""
    out: list[NativeCandidate] = []
    for miner in _native_miners():
        try:
            for candidate in miner.iter_candidates():
                if allowed and candidate.cwd not in allowed:
                    continue
                out.append(candidate)
        except Exception:
            # One provider's discovery failing must not sink the others.
            continue
    return out


def _match_candidate(
    candidate: NativeCandidate,
    patterns: list[re.Pattern],
    is_noise: Callable[[str], bool] | None,
    roles: tuple[str, ...],
    kind: str,
    source: str,
) -> list[tuple[int, dict[str, Any]]]:
    """Parse one transcript and score its messages (of the requested ``roles``)
    by distinct whole-word token hits. A single bad transcript must not sink the
    whole concurrent search, so any parse/scoring error is contained to this
    candidate."""
    try:
        visit = candidate.parse()
    except Exception:
        return []
    if visit is None:
        return []
    scored: list[tuple[int, dict[str, Any]]] = []
    for msg in visit.messages:
        if not isinstance(msg, dict) or msg.get("role") not in roles:
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        text = content.strip()
        if not text or (is_noise is not None and is_noise(text)):
            continue
        lowered = text.lower()
        score = sum(1 for pattern in patterns if pattern.search(lowered))
        if score == 0:
            continue
        scored.append((score, {
            "text": text,
            "role": msg.get("role"),
            "kind": kind,
            "source": source,
            "cwd": visit.cwd,
            "sid": visit.sid,
            "ts": msg.get("timestamp") if isinstance(msg.get("timestamp"), str) else "",
        }))
    return scored


def _search(
    *,
    query: str,
    roles: tuple[str, ...],
    kind: str,
    source: str,
    cwds: tuple[str, ...] | list[str] = (),
    max_matches: int | None = 20,
    is_noise: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Single fan-out grep over the raw provider-native transcripts.

    ``roles`` selects which turns are grepped (``("user",)`` for typed prompts;
    ``("user", "assistant")`` for the whole conversation transcript). ``kind`` /
    ``source`` label the returned records so consumers can tell the two scopes
    apart. All other behavior (whole-word matching, token-overlap ranking, cwd
    filter, ``is_noise``, dedup, oldest-first presentation) is shared."""
    tokens = _query_tokens(query)
    if not tokens:
        return []
    patterns = _token_patterns(tokens)
    allowed = {c for c in cwds if isinstance(c, str) and c.strip()}
    candidates = _candidates(allowed)
    if not candidates:
        return []

    scored: list[tuple[int, dict[str, Any]]] = []
    workers = max(1, min(_MAX_WORKERS, len(candidates)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for chunk in pool.map(
            lambda candidate: _match_candidate(
                candidate, patterns, is_noise, roles, kind, source
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


def search_native_session_prompts(
    *,
    query: str,
    cwds: tuple[str, ...] | list[str] = (),
    max_matches: int | None = 20,
    is_noise: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Grep the raw provider-native transcripts for ``query`` and return the
    matching typed **user prompts**, highest token-overlap first.

    ``cwds`` (when non-empty) restricts to those working directories.
    ``is_noise`` is the caller's programmatic-preamble filter (BA-injected
    worker/processor/auditor prompts) so the fallback drops the same noise the
    main corpus does. A match keeps a prompt when at least one query token
    appears in it as a whole word; the score is the count of distinct tokens hit.
    """
    return _search(
        query=query,
        roles=("user",),
        kind="native_session_prompt",
        source="native_session_fallback",
        cwds=cwds,
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
    conversation** — both typed user prompts and assistant replies — highest
    token-overlap first. Peer to :func:`search_native_session_prompts`; the only
    difference is scope (all turns vs. user prompts only). Same ranking, cwd
    filter, dedup, and oldest-first presentation."""
    return _search(
        query=query,
        roles=("user", "assistant"),
        kind="native_session_transcript",
        source="native_session_transcript",
        cwds=cwds,
        max_matches=max_matches,
        is_noise=is_noise,
    )
