"""Raw provider-native session prompt search — the get-requirements fallback.

When the requirement processor (the LLM-backed curation path) is unavailable,
answer from the rawest data we have: the provider-native session transcripts.
Each provider is read by its own native miner — Claude ``projects/<cwd>/<sid>.jsonl``,
Codex / Gemini / Better Agent run-dir ``session_events.jsonl`` — so this is the
"every provider with its own grepping method" fan-out. It reuses the canonical
:mod:`native_session_miner` readers (no watermark → scan every transcript) and
greps the typed user prompts for the query tokens.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from native_session_miner import (
    NativeBetterAgentSessionMiner,
    NativeClaudeSessionMiner,
    NativeCodexSessionMiner,
    NativeGeminiSessionMiner,
)
from session_miner import SessionVisit

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _native_miners() -> list:
    """One miner per provider, each with an empty state so nothing is skipped.

    An empty watermark dict makes every native transcript a candidate — the
    fallback wants the whole raw corpus, not the delta since the last mining
    pass.
    """
    return [
        NativeClaudeSessionMiner({}),
        NativeCodexSessionMiner({}),
        NativeGeminiSessionMiner({}),
        NativeBetterAgentSessionMiner({}),
    ]


def _query_tokens(query: str) -> list[str]:
    return [tok for tok in _TOKEN_RE.findall(query.lower()) if len(tok) >= 2]


def _iter_visits() -> Iterable[SessionVisit]:
    for miner in _native_miners():
        try:
            for _key, visit, _mtime in miner._iter_sources():
                yield visit
        except Exception:
            # One provider's transcripts being unreadable must not sink the
            # whole fallback — the other providers still answer.
            continue


def search_native_session_prompts(
    *,
    query: str,
    cwds: tuple[str, ...] | list[str] = (),
    max_matches: int | None = 20,
    is_noise: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Grep the raw provider-native transcripts for ``query`` and return the
    matching typed user prompts, highest token-overlap first.

    ``cwds`` (when non-empty) restricts to those working directories.
    ``is_noise`` is the caller's programmatic-preamble filter (BA-injected
    worker/processor/auditor prompts) so the fallback drops the same noise the
    main corpus does. A match keeps a prompt when at least one query token is a
    substring of it; the score is the count of distinct tokens hit.
    """
    tokens = _query_tokens(query)
    if not tokens:
        return []
    allowed = {c for c in cwds if isinstance(c, str) and c.strip()}
    scored: list[tuple[int, dict[str, Any]]] = []
    for visit in _iter_visits():
        if allowed and visit.cwd not in allowed:
            continue
        for msg in visit.messages:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            text = content.strip()
            if not text or (is_noise is not None and is_noise(text)):
                continue
            lowered = text.lower()
            score = sum(1 for tok in tokens if tok in lowered)
            if score == 0:
                continue
            scored.append((score, {
                "text": text,
                "kind": "native_session_prompt",
                "source": "native_session_fallback",
                "cwd": visit.cwd,
                "sid": visit.sid,
                "ts": msg.get("timestamp") if isinstance(msg.get("timestamp"), str) else "",
            }))
    scored.sort(key=lambda item: (item[0], item[1].get("ts") or ""), reverse=True)
    matches: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    for _score, record in scored:
        if record["text"] in seen_text:
            continue
        seen_text.add(record["text"])
        matches.append(record)
        if max_matches is not None and len(matches) >= max_matches:
            break
    return matches
