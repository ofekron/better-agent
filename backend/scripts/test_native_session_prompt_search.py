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
from native_session_miner import NativeCandidate, NativeElement  # noqa: E402
from native_session_prompt_search import Categorizer, ElementCategory  # noqa: E402
from paths import encode_cwd  # noqa: E402

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
    """Inject scripted candidates at the match-resolution seam so unit tests
    never touch rg or the filesystem."""
    def fake(tokens: list[str], allowed: set[str]):
        return [
            c for c in monkeypatch_list
            if not allowed or getattr(c, "cwd", "") in allowed
        ]
    nsp._matched_candidates = fake


_ORIG_MATCHED = nsp._matched_candidates


def _reset_candidates() -> None:
    nsp._matched_candidates = _ORIG_MATCHED


# Integration-style tests exercise the real Python discovery path; disable rg so
# they stay deterministic and isolated (rg would search the real home too).
_ORIG_RG = nsp._rg_filter


def _disable_rg() -> None:
    nsp._rg_filter = lambda tokens: None


def _restore_rg() -> None:
    nsp._rg_filter = _ORIG_RG


def _isolate_native_roots(*, claude: list[Path] | None = None, codex: Path | None = None,
                          gemini: Path | None = None, runs: Path | None = None):
    """Point every native-root helper in native_session_miner at temp dirs so
    filesystem-walk tests neither scan the real home (~/.claude*, ~/.codex,
    ~/.gemini — tens of thousands of files) nor read real data. Returns the
    original callables for restoration."""
    import native_session_miner as M
    orig = (M._claude_projects_roots, M._codex_sessions_root, M._gemini_chats_root, M._runs_root)
    M._claude_projects_roots = lambda: list(claude or [])
    M._codex_sessions_root = lambda: codex or _SCRATCH / "no-codex"
    M._gemini_chats_root = lambda: gemini or _SCRATCH / "no-gemini"
    M._runs_root = lambda: runs or _SCRATCH / "no-runs"
    return orig


def _restore_native_roots(orig) -> None:
    import native_session_miner as M
    (M._claude_projects_roots, M._codex_sessions_root, M._gemini_chats_root, M._runs_root) = orig


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
    # Reverse the input order for the second run: with a bare stable sort the
    # output would follow input order and diverge — only the sid+text tiebreaker
    # forces both runs to the same concrete order.
    _patch_candidates(list(reversed(build())))
    try:
        second = [r["text"] for r in nsp.search_native_session_prompts(query="offline")]
    finally:
        _reset_candidates()
    expected = ["offline alpha", "offline beta", "offline gamma"]
    ok = first == expected and second == expected
    print(f"{OK if ok else FAIL} empty-ts ties order deterministically (got {first} vs {second})")
    return ok


def test_unlinked_transcript_found_via_filesystem_walk() -> bool:
    """Regression: a claude native transcript with NO Better Agent session
    record (direct CLI / extension-spawned) must be found by the search.

    Before the filesystem-first discovery fix, `_candidates` was BA-index-gated
    and an empty `sessions/` dir yielded zero candidates — so this prompt was
    missed. Now `iter_all_native_candidates` walks the projects dir directly."""
    _reset_candidates()  # ensure the REAL _candidates is in place
    projects = _SCRATCH / "claude-projects"
    cwd = "/Users/test/unlinked-proj"
    session_dir = projects / encode_cwd(cwd)
    session_dir.mkdir(parents=True, exist_ok=True)
    sid = "deadbeef-0000-0000-0000-unlinked0001"
    (session_dir / f"{sid}.jsonl").write_text(
        json.dumps({
            "type": "user",
            "uuid": "u-unlinked-1",
            "timestamp": "2024-01-01T00:00:00Z",
            "message": {"role": "user", "content": "zulifrangible task widget"},
        }) + "\n",
        encoding="utf-8",
    )
    # Deliberately NO sessions/<sid>.json — the transcript is unlinked.
    _disable_rg()
    orig = _isolate_native_roots(claude=[projects])
    try:
        out = nsp.search_native_session_prompts(query="zulifrangible task widget")
    finally:
        _restore_native_roots(orig)
        _restore_rg()
    texts = {r["text"] for r in out}
    ok = "zulifrangible task widget" in texts
    print(f"{OK if ok else FAIL} unlinked claude transcript found via filesystem walk (got {texts})")
    return ok


def test_codex_and_gemini_native_transcripts_found() -> bool:
    """The native stores of every provider are covered — a codex rollout and a
    gemini chat with no BA record must both be found, and codex's injected
    ``<environment_context>`` block must be dropped (not treated as a prompt)."""
    _reset_candidates()

    codex_root = _SCRATCH / "codex-sessions"
    codex_root.mkdir(parents=True, exist_ok=True)
    (codex_root / "rollout-test-zapp.jsonl").write_text(
        json.dumps({"type": "session_meta", "timestamp": "2024-01-01T00:00:00Z",
                    "payload": {"id": "z", "cwd": "/Users/test/zapp", "source": "cli"}}) + "\n"
        + json.dumps({"type": "response_item", "timestamp": "2024-01-01T00:00:01Z",
                      "payload": {"type": "message", "role": "user",
                                  "content": [{"type": "input_text",
                                               "text": "zulifrangible codex widget"}]}}) + "\n"
        + json.dumps({"type": "response_item", "timestamp": "2024-01-01T00:00:02Z",
                      "payload": {"type": "message", "role": "user",
                                  "content": [{"type": "input_text",
                                               "text": "<environment_context>\n  <cwd>/x</cwd>"}]}}) + "\n",
        encoding="utf-8",
    )

    gemini_root = _SCRATCH / "gemini-tmp" / "zapp-proj" / "chats"
    gemini_root.mkdir(parents=True, exist_ok=True)
    (gemini_root / "session-2024-01-01-zapp.jsonl").write_text(
        json.dumps({"sessionId": "z", "startTime": "2024-01-01T00:00:00Z", "kind": "main"}) + "\n"
        + json.dumps({"id": "u1", "timestamp": "2024-01-01T00:00:01Z", "type": "user",
                      "content": [{"text": "zulifrangible gemini widget"}]}) + "\n",
        encoding="utf-8",
    )

    _disable_rg()
    orig = _isolate_native_roots(
        claude=[],
        codex=codex_root,
        gemini=gemini_root.parent.parent,
    )
    try:
        out = nsp.search_native_session_prompts(query="zulifrangible widget")
    finally:
        _restore_native_roots(orig)
        _restore_rg()
    texts = {r["text"] for r in out}
    ok = (
        "zulifrangible codex widget" in texts
        and "zulifrangible gemini widget" in texts
        and not any("<environment_context>" in t for t in texts)
    )
    print(f"{OK if ok else FAIL} codex+gemini native transcripts found, env-context dropped (got {texts})")
    return ok


def test_categorizer_maps_elements_to_categories() -> bool:
    """The shared Categorizer maps structural kind + tool name → semantic
    category, provider-agnostic. Tool-name casing/spacing is normalized."""
    cat = Categorizer()
    cases = [
        (NativeElement("user_prompt", "user", "fix it"), ElementCategory.PROMPT),
        (NativeElement("assistant_text", "assistant", "ok"), ElementCategory.REPLY),
        (NativeElement("reasoning", "assistant", "hmm"), ElementCategory.REASONING),
        (NativeElement("command", "user", "/foo"), ElementCategory.COMMAND),
        (NativeElement("meta", "user", "title"), ElementCategory.META),
        (NativeElement("tool_call", "assistant", "x", "Bash"), ElementCategory.SHELL),
        (NativeElement("tool_call", "assistant", "x", "exec_command"), ElementCategory.SHELL),
        (NativeElement("tool_call", "assistant", "x", "Edit"), ElementCategory.FILE_EDIT),
        (NativeElement("tool_call", "assistant", "x", "apply_patch"), ElementCategory.FILE_EDIT),
        (NativeElement("tool_call", "assistant", "x", "Read"), ElementCategory.FILE_READ),
        (NativeElement("tool_call", "assistant", "x", "WebSearch"), ElementCategory.SEARCH),
        (NativeElement("tool_call", "assistant", "x", "Task"), ElementCategory.SUBAGENT),
        (NativeElement("tool_call", "assistant", "x", "MysteryTool"), ElementCategory.OTHER),
        (NativeElement("tool_result", "user", "ran fine"), ElementCategory.TOOL_OUTPUT),
        (NativeElement("tool_result", "user", "Traceback (most recent call last)"), ElementCategory.ERROR),
    ]
    bad = [(el, got, want) for el, want in cases if (got := cat.categorize(el)) != want]
    ok = not bad
    print(f"{OK if ok else FAIL} categorizer maps kind+tool -> category (mismatches={bad})")
    return ok


def _write_raw_transcript(records: list[dict]) -> Path:
    """Write arbitrary Claude-shaped jsonl lines (tool_use/tool_result/etc)."""
    global _seq
    _seq += 1
    path = _SCRATCH / f"raw_{_seq}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_rg_filter_narrows_to_files_containing_needle() -> bool:
    """The rg match-first path finds only files containing the needle and builds
    candidates from those paths. Skipped when rg isn't installed."""
    _reset_candidates()
    if not nsp._rg_filter(["zulifrangible"]):
        # rg unavailable — the None fallback is covered by the other tests.
        print(f"{OK} rg-filter test skipped (rg not installed)")
        return True
    import native_session_miner as M
    projects = _SCRATCH / "rg-projects"
    cwd = "/Users/test/rg-proj"
    session_dir = projects / encode_cwd(cwd)
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "rg-hit.jsonl").write_text(
        json.dumps({"type": "user", "uuid": "u", "timestamp": "2024-01-01T00:00:00Z",
                    "message": {"role": "user", "content": "zulifrangible rg needle hunt"}}) + "\n",
        encoding="utf-8",
    )
    (session_dir / "rg-miss.jsonl").write_text(
        json.dumps({"type": "user", "uuid": "u", "timestamp": "2024-01-01T00:00:00Z",
                    "message": {"role": "user", "content": "nothing relevant here"}}) + "\n",
        encoding="utf-8",
    )
    orig = _isolate_native_roots(claude=[projects], codex=_SCRATCH / "no-codex",
                                 gemini=_SCRATCH / "no-gemini", runs=_SCRATCH / "no-runs")
    try:
        cands = nsp._matched_candidates(["zulifrangible"], set())
    finally:
        _restore_native_roots(orig)
    sids = {c.sid for c in cands}
    ok = sids == {"rg-hit"}
    print(f"{OK if ok else FAIL} rg filter narrows to needle files (got sids={sids})")
    return ok


def test_index_fast_path_serves_query_with_rg_disabled() -> bool:
    """With the native index built + fresh, a query is served from FTS even when
    rg is disabled — proving the fast path (not the rg fallback) answers."""
    import native_transcript_index as idx
    projects = _SCRATCH / "idx-projects"
    cwd = "/Users/test/idx-proj"
    session_dir = projects / encode_cwd(cwd)
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "s1.jsonl").write_text(
        json.dumps({"type": "user", "uuid": "u", "timestamp": "2024-01-01T00:00:00Z",
                    "message": {"role": "user", "content": "zulifrangible fastpath needle"}}) + "\n",
        encoding="utf-8",
    )
    _reset_candidates()
    _disable_rg()
    orig = _isolate_native_roots(claude=[projects])
    idx.reset_for_test()
    try:
        idx.refresh_once()  # build + mark covered
        assert idx.is_usable()
        out = nsp.search_in_native_session_transcript(query="zulifrangible fastpath")
    finally:
        _restore_native_roots(orig)
        _restore_rg()
        idx.reset_for_test()
    texts = {r["text"] for r in out}
    ok = "zulifrangible fastpath needle" in texts
    print(f"{OK if ok else FAIL} index fast path serves query with rg disabled (got {texts})")
    return ok


def test_generalized_search_greps_tool_calls_and_results() -> bool:
    """search_in_native_session_transcript greps EVERYTHING — tool calls and
    tool results, not just prompts/replies — and labels each match with its
    category + tool_name. The category filter narrows the scope."""
    transcript = _write_raw_transcript([
        {"type": "assistant", "uuid": "a1", "timestamp": "2024-01-01T00:00:00Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "running the zulifrangible build now"},
             {"type": "tool_use", "id": "t1", "name": "Bash",
              "input": {"command": "make zulifrangible-widget"}},
         ]}},
        {"type": "user", "uuid": "u1", "timestamp": "2024-01-01T00:00:01Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": "zulifrangible widget built ok"},
         ]}},
    ])
    cand = NativeCandidate(key="claude:s1", sid="s1", cwd="/proj", data={},
                           transcript=transcript, mtime=0.0, format="claude")
    _patch_candidates([cand])
    try:
        all_hits = nsp.search_in_native_session_transcript(query="zulifrangible widget")
        shell_only = nsp.search_in_native_session_transcript(
            query="zulifrangible widget", categories=("shell",))
    finally:
        _reset_candidates()
    cats = {r["category"] for r in all_hits}
    tools = {r.get("tool_name") for r in all_hits}
    shell_cats = {r["category"] for r in shell_only}
    ok = (
        cats == {ElementCategory.REPLY, ElementCategory.SHELL, ElementCategory.TOOL_OUTPUT}
        and tools == {"Bash", ""}
        and shell_cats == {ElementCategory.SHELL}
        and len(shell_only) == 1
    )
    print(f"{OK if ok else FAIL} generalized search greps tools+results, category filter works "
          f"(cats={cats}, tools={tools}, shell_only={shell_cats})")
    return ok


def test_wiring_fails_closed_on_processor_error() -> bool:
    orig_prepare = requirement_context.prepare_requirements_local_read_context
    orig_proc = requirement_context._run_requirements_processor
    requirement_context.prepare_requirements_local_read_context = lambda: None
    requirement_context._run_requirements_processor = lambda **kw: {
        "text": "", "error": "processor_failed"
    }
    try:
        resp = requirement_context.get_processed_requirements(query="offline sync")
    finally:
        requirement_context.prepare_requirements_local_read_context = orig_prepare
        requirement_context._run_requirements_processor = orig_proc
    ok = (
        resp.get("success") is False
        and resp.get("error") == "processor_failed"
        and resp.get("text") == ""
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
        "text": "real processor requirement",
        "error": "partial",
    }
    try:
        resp = requirement_context.get_processed_requirements(query="offline sync")
    finally:
        requirement_context.prepare_requirements_local_read_context = orig_prepare
        requirement_context._run_requirements_processor = orig_proc
    ok = (
        resp.get("text") == "real processor requirement"
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
        test_unlinked_transcript_found_via_filesystem_walk,
        test_codex_and_gemini_native_transcripts_found,
        test_categorizer_maps_elements_to_categories,
        test_generalized_search_greps_tool_calls_and_results,
        test_rg_filter_narrows_to_files_containing_needle,
        test_index_fast_path_serves_query_with_rg_disabled,
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
