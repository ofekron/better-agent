"""Unit tests for raw provider-native prompt search.

Covers the raw provider-native grep (`native_session_prompt_search`) and the
public `requirement_context.get_processed_requirements` fail-closed contract:

  * whole-word matching — a query token does NOT match inside a longer word
    (`ui` matches "fix the ui" but not "rebuilding guise").
  * stopword drop — a query reducing to only stopwords yields no matches.
  * token-overlap ranking selects the higher-scoring prompt under the cap.
  * cwd filter restricts to the requested working directories.
  * `is_noise` drops programmatic-preamble prompts.
  * dedup collapses the identical prompt seen across sessions.
  * resilience — one candidate whose parse raises a non-OSError does NOT abort
    the whole concurrent search; the other providers' matches still return.
  * determinism — equal-score / empty-ts matches order identically across runs.
  * wiring: processor error + empty requirements fails closed instead of
    returning raw native grep hits as curated requirements.
  * wiring: processor returning real requirements is NOT replaced by the
    fallback even when it also carries a truthy error (locks the empty-guard).

Run with:
    cd backend && .venv/bin/python scripts/test_native_session_prompt_search.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Per CLAUDE.md: isolate ~/.better-claude state to a tempdir BEFORE importing
# any state-touching backend module (session_store / requirement_context / …).
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-prompt-search-")

import native_session_prompt_search as nsp  # noqa: E402
import requirement_context  # noqa: E402
from native_session_miner import NativeCandidate  # noqa: E402

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

_SCRATCH = Path(_TMP_HOME) / "scratch"
_SCRATCH.mkdir(parents=True, exist_ok=True)
_seq = 0


def _write_transcript(prompts: list[tuple[str, str]]) -> Path:
    """Write a Claude-shaped native transcript of user prompts (text, ts)."""
    global _seq
    _seq += 1
    path = _SCRATCH / f"transcript_{_seq}.jsonl"
    lines = []
    for i, (text, ts) in enumerate(prompts):
        lines.append(json.dumps({
            "type": "user",
            "uuid": f"u{_seq}_{i}",
            "timestamp": ts,
            "message": {"role": "user", "content": text},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _candidate(sid: str, cwd: str, prompts: list[tuple[str, str]]) -> NativeCandidate:
    return NativeCandidate(
        key=f"claude:{sid}",
        sid=sid,
        cwd=cwd,
        data={},
        transcript=_write_transcript(prompts),
        mtime=0.0,
    )


class _RaisingCandidate:
    """A candidate whose parse raises a non-OSError — the crash `parse()`'s own
    `except OSError` would NOT swallow, exercising the pool.map abort guard."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd

    def parse(self):
        raise ValueError("boom: malformed transcript")


def _patch_candidates(monkeypatch_list: list[object]) -> None:
    def fake(allowed: set[str]):
        return [
            c for c in monkeypatch_list
            if not allowed or getattr(c, "cwd", "") in allowed
        ]
    nsp._candidates = fake


_ORIG_CANDIDATES = nsp._candidates


def _reset_candidates() -> None:
    nsp._candidates = _ORIG_CANDIDATES


def test_whole_word_match_not_substring() -> bool:
    _patch_candidates([
        _candidate("s1", "/proj", [("fix the ui layout", "2024-01-01")]),
        _candidate("s2", "/proj", [("rebuilding guise now", "2024-01-02")]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="ui")
    finally:
        _reset_candidates()
    texts = {r["text"] for r in out}
    ok = texts == {"fix the ui layout"}
    print(f"{OK if ok else FAIL} whole-word match ignores substring (got {texts})")
    return ok


def test_stopword_only_query_returns_empty() -> bool:
    _patch_candidates([_candidate("s1", "/proj", [("the plan is in the doc", "2024-01-01")])])
    try:
        out = nsp.search_native_session_prompts(query="in the")
    finally:
        _reset_candidates()
    ok = out == []
    print(f"{OK if ok else FAIL} stopword-only query yields no matches (got {out})")
    return ok


def test_ranking_selects_higher_overlap_under_cap() -> bool:
    _patch_candidates([
        _candidate("s1", "/proj", [("offline sync mode broke", "2024-01-01")]),
        _candidate("s2", "/proj", [("offline notes only", "2024-01-02")]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="offline sync mode", max_matches=1)
    finally:
        _reset_candidates()
    ok = len(out) == 1 and out[0]["text"] == "offline sync mode broke"
    print(f"{OK if ok else FAIL} ranking selects higher-overlap prompt under cap (got {out})")
    return ok


def test_cwd_filter() -> bool:
    _patch_candidates([
        _candidate("s1", "/proj-a", [("offline sync here", "2024-01-01")]),
        _candidate("s2", "/proj-b", [("offline sync there", "2024-01-02")]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="offline", cwds=("/proj-a",))
    finally:
        _reset_candidates()
    texts = {r["text"] for r in out}
    ok = texts == {"offline sync here"}
    print(f"{OK if ok else FAIL} cwd filter restricts results (got {texts})")
    return ok


def test_is_noise_drops_preamble() -> bool:
    _patch_candidates([
        _candidate("s1", "/proj", [
            ("NOISE injected worker preamble offline", "2024-01-01"),
            ("real offline requirement", "2024-01-02"),
        ]),
    ])
    try:
        out = nsp.search_native_session_prompts(
            query="offline", is_noise=lambda t: t.startswith("NOISE")
        )
    finally:
        _reset_candidates()
    texts = {r["text"] for r in out}
    ok = texts == {"real offline requirement"}
    print(f"{OK if ok else FAIL} is_noise drops programmatic preamble (got {texts})")
    return ok


def test_dedup_across_sessions() -> bool:
    _patch_candidates([
        _candidate("s1", "/proj", [("offline sync must survive", "2024-01-01")]),
        _candidate("s2", "/proj", [("offline sync must survive", "2024-01-02")]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="offline sync")
    finally:
        _reset_candidates()
    ok = len(out) == 1 and out[0]["text"] == "offline sync must survive"
    print(f"{OK if ok else FAIL} identical prompt across sessions deduped (got {len(out)})")
    return ok


def test_bad_transcript_does_not_abort_search() -> bool:
    _patch_candidates([
        _RaisingCandidate("/proj"),
        _candidate("s2", "/proj", [("offline sync survivor", "2024-01-02")]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="offline sync")
    finally:
        _reset_candidates()
    texts = {r["text"] for r in out}
    ok = texts == {"offline sync survivor"}
    print(f"{OK if ok else FAIL} non-OSError parse does not abort search (got {texts})")
    return ok


def test_deterministic_order_for_empty_ts_ties() -> bool:
    # All empty ts, equal score → the sid+text tiebreaker must fix the order.
    def build():
        return [
            _candidate("sB", "/proj", [("offline beta", "")]),
            _candidate("sA", "/proj", [("offline alpha", "")]),
            _candidate("sC", "/proj", [("offline gamma", "")]),
        ]
    _patch_candidates(build())
    try:
        first = [r["text"] for r in nsp.search_native_session_prompts(query="offline")]
    finally:
        _reset_candidates()
    _patch_candidates(build())
    try:
        second = [r["text"] for r in nsp.search_native_session_prompts(query="offline")]
    finally:
        _reset_candidates()
    ok = first == second and len(first) == 3
    print(f"{OK if ok else FAIL} empty-ts ties order deterministically (got {first} vs {second})")
    return ok


def test_wiring_fails_closed_on_processor_error() -> bool:
    orig_prepare = requirement_context.prepare_requirements_local_read_context
    orig_proc = requirement_context._run_requirements_processor
    requirement_context.prepare_requirements_local_read_context = lambda: None
    requirement_context._run_requirements_processor = lambda **kw: {
        "requirements": [], "error": "processor_failed"
    }
    try:
        resp = requirement_context.get_processed_requirements(query="offline sync")
    finally:
        requirement_context.prepare_requirements_local_read_context = orig_prepare
        requirement_context._run_requirements_processor = orig_proc
    ok = (
        resp.get("success") is False
        and resp.get("error") == "processor_failed"
        and resp.get("requirements") == []
        and "fallback" not in resp
        and "processor_error" not in resp
    )
    print(f"{OK if ok else FAIL} processor error fails closed without raw fallback (got {resp})")
    return ok


def test_wiring_real_requirements_not_replaced_by_fallback() -> bool:
    orig_prepare = requirement_context.prepare_requirements_local_read_context
    orig_proc = requirement_context._run_requirements_processor
    requirement_context.prepare_requirements_local_read_context = lambda: None
    requirement_context._run_requirements_processor = lambda **kw: {
        "requirements": [{"text": "real processor requirement"}],
        "error": "partial",
    }
    try:
        resp = requirement_context.get_processed_requirements(query="offline sync")
    finally:
        requirement_context.prepare_requirements_local_read_context = orig_prepare
        requirement_context._run_requirements_processor = orig_proc
    ok = (
        [r["text"] for r in resp.get("requirements", [])] == ["real processor requirement"]
        and resp.get("error") == "partial"
    )
    print(f"{OK if ok else FAIL} real requirements not replaced by fallback (got {resp})")
    return ok


def main_run() -> int:
    tests = [
        test_whole_word_match_not_substring,
        test_stopword_only_query_returns_empty,
        test_ranking_selects_higher_overlap_under_cap,
        test_cwd_filter,
        test_is_noise_drops_preamble,
        test_dedup_across_sessions,
        test_bad_transcript_does_not_abort_search,
        test_deterministic_order_for_empty_ts_ties,
        test_wiring_fails_closed_on_processor_error,
        test_wiring_real_requirements_not_replaced_by_fallback,
    ]
    results = []
    for fn in tests:
        try:
            results.append(fn())
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"{FAIL} {fn.__name__} raised: {e}")
            results.append(False)
    n_pass = sum(1 for r in results if r)
    n_total = len(results)
    print(f"\n{n_pass}/{n_total} native-prompt-search tests passed")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main_run())
