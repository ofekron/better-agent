"""Unit tests for the whole-transcript grep sibling.

`search_native_session_transcripts` is the peer of
`search_native_session_prompts`: same fan-out / ranking / dedup, but it greps
BOTH user prompts and assistant replies, where the prompts search greps user
prompts only. These lock that scope difference:

  * a term that appears ONLY in an assistant reply is found by the transcript
    search but NOT by the prompts search.
  * a term in a user prompt is found by both.
  * each transcript match carries its ``role`` so the consumer can tell whose
    line matched.
  * the shared machinery (cwd filter, dedup) still applies.

Run with:
    cd backend && .venv/bin/python scripts/test_native_session_transcripts_search.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Per CLAUDE.md: isolate ~/.better-claude state to a tempdir BEFORE importing
# any state-touching backend module.
import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-transcripts-search-")

import native_session_prompt_search as nsp  # noqa: E402
from native_session_miner import NativeCandidate  # noqa: E402

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

_SCRATCH = Path(_TMP_HOME) / "scratch"
_SCRATCH.mkdir(parents=True, exist_ok=True)
_seq = 0


def _write_transcript(turns: list[tuple[str, str, str]]) -> Path:
    """Write a Claude-shaped native transcript of (role, text, ts) turns."""
    global _seq
    _seq += 1
    path = _SCRATCH / f"transcript_{_seq}.jsonl"
    lines = []
    for i, (role, text, ts) in enumerate(turns):
        lines.append(json.dumps({
            "type": role,
            "uuid": f"{role[0]}{_seq}_{i}",
            "timestamp": ts,
            "message": {"role": role, "content": text},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _candidate(sid: str, cwd: str, turns: list[tuple[str, str, str]]) -> NativeCandidate:
    return NativeCandidate(
        key=f"claude:{sid}",
        sid=sid,
        cwd=cwd,
        data={},
        transcript=_write_transcript(turns),
        mtime=0.0,
    )


_ORIG_MATCHED = nsp._matched_candidates


def _patch_candidates(cands: list[object]) -> None:
    def fake(tokens: list[str], allowed: set[str]):
        return [c for c in cands if not allowed or getattr(c, "cwd", "") in allowed]
    nsp._matched_candidates = fake


def _reset_candidates() -> None:
    nsp._matched_candidates = _ORIG_MATCHED


def test_term_only_in_assistant_reply() -> bool:
    # "hydration" appears only in the assistant reply, never in a user prompt.
    _patch_candidates([
        _candidate("s1", "/proj", [
            ("user", "why is the panel blank", "2024-01-01T00:00:00"),
            ("assistant", "The blank panel is a hydration mismatch on mount", "2024-01-01T00:00:01"),
        ]),
    ])
    try:
        transcripts = nsp.search_native_session_transcripts(query="hydration mismatch")
        prompts = nsp.search_native_session_prompts(query="hydration mismatch")
    finally:
        _reset_candidates()
    t_texts = {r["text"] for r in transcripts}
    ok = (
        t_texts == {"The blank panel is a hydration mismatch on mount"}
        and prompts == []
    )
    print(f"{OK if ok else FAIL} assistant-only term: transcript finds it, prompts do not "
          f"(transcript={t_texts}, prompts={prompts})")
    return ok


def test_term_in_user_prompt_found_by_both() -> bool:
    _patch_candidates([
        _candidate("s1", "/proj", [
            ("user", "the offline backlog keeps dropping actions", "2024-01-02T00:00:00"),
            ("assistant", "acknowledged, will look at the queue", "2024-01-02T00:00:01"),
        ]),
    ])
    try:
        transcripts = nsp.search_native_session_transcripts(query="offline backlog")
        prompts = nsp.search_native_session_prompts(query="offline backlog")
    finally:
        _reset_candidates()
    want = "the offline backlog keeps dropping actions"
    ok = (
        want in {r["text"] for r in transcripts}
        and {r["text"] for r in prompts} == {want}
    )
    print(f"{OK if ok else FAIL} user-prompt term found by both scopes "
          f"(transcript_has={want in {r['text'] for r in transcripts}}, prompts={[r['text'] for r in prompts]})")
    return ok


def test_match_records_carry_role() -> bool:
    _patch_candidates([
        _candidate("s1", "/proj", [
            ("user", "deploy the caching layer", "2024-01-03T00:00:00"),
            ("assistant", "caching layer deployed to prod", "2024-01-03T00:00:01"),
        ]),
    ])
    try:
        out = nsp.search_native_session_transcripts(query="caching layer")
    finally:
        _reset_candidates()
    by_role = {r.get("role") for r in out}
    kinds = {r.get("kind") for r in out}
    ok = by_role == {"user", "assistant"} and kinds == {"native_session_transcript"}
    print(f"{OK if ok else FAIL} records carry role + transcript kind (roles={by_role}, kinds={kinds})")
    return ok


def test_cwd_filter_still_applies() -> bool:
    _patch_candidates([
        _candidate("s1", "/proj-a", [("assistant", "widget rendered", "2024-01-04T00:00:00")]),
        _candidate("s2", "/proj-b", [("assistant", "widget crashed", "2024-01-04T00:00:01")]),
    ])
    try:
        out = nsp.search_native_session_transcripts(query="widget", cwds=("/proj-a",))
    finally:
        _reset_candidates()
    texts = {r["text"] for r in out}
    ok = texts == {"widget rendered"}
    print(f"{OK if ok else FAIL} cwd filter restricts transcript search (got {texts})")
    return ok


def main_run() -> int:
    tests = [
        test_term_only_in_assistant_reply,
        test_term_in_user_prompt_found_by_both,
        test_match_records_carry_role,
        test_cwd_filter_still_applies,
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
    print(f"\n{n_pass}/{n_total} native-transcript-search tests passed")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main_run())
