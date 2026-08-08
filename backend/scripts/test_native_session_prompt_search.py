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
                          runs: Path | None = None, pi: Path | None = None):
    """Point every native-root helper in native_session_miner at temp dirs so
    filesystem-walk tests neither scan the real home (~/.claude*, ~/.codex,
    ~/.pi — tens of thousands of files) nor read real data. Returns
    the original callables for restoration."""
    import native_session_miner as M
    orig = (M._claude_projects_roots, M._codex_sessions_root,
            M._runs_root, M._pi_sessions_root, M._windsurf_cascade_roots,
            nsp._windsurf_cascade_roots)
    M._claude_projects_roots = lambda: list(claude or [])
    M._codex_sessions_root = lambda: codex or _SCRATCH / "no-codex"
    M._runs_root = lambda: runs or _SCRATCH / "no-runs"
    M._pi_sessions_root = lambda: pi or _SCRATCH / "no-pi"
    M._windsurf_cascade_roots = lambda: []
    # nsp imports the helper by name, so the miner-side patch alone leaves the
    # real ~/.codeium cascade store reachable through `_windsurf_candidates`.
    nsp._windsurf_cascade_roots = lambda: []
    return orig


def _restore_native_roots(orig) -> None:
    import native_session_miner as M
    (M._claude_projects_roots, M._codex_sessions_root,
     M._runs_root, M._pi_sessions_root, M._windsurf_cascade_roots,
     nsp._windsurf_cascade_roots) = orig


def test_whole_word_match_not_substring() -> None:
    _patch_candidates([
        _candidate("s1", "/proj", [("fix the ui layout", "2024-01-01")]),
        _candidate("s2", "/proj", [("rebuilding guise now", "2024-01-02")]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="ui")
    finally:
        _reset_candidates()
    texts = {r["text"] for r in out}
    assert texts == {"fix the ui layout"}, f"whole-word match ignores substring (got {texts})"
    print(f"{OK} whole-word match ignores substring (got {texts})")


def test_stopword_only_query_returns_empty() -> None:
    _patch_candidates([_candidate("s1", "/proj", [("the plan is in the doc", "2024-01-01")])])
    try:
        out = nsp.search_native_session_prompts(query="in the")
    finally:
        _reset_candidates()
    assert out == [], f"stopword-only query yields no matches (got {out})"
    print(f"{OK} stopword-only query yields no matches (got {out})")


def test_ranking_selects_higher_overlap_under_cap() -> None:
    _patch_candidates([
        _candidate("s1", "/proj", [("offline sync mode broke", "2024-01-01")]),
        _candidate("s2", "/proj", [("offline notes only", "2024-01-02")]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="offline sync mode", max_matches=1)
    finally:
        _reset_candidates()
    assert len(out) == 1, f"ranking cap kept one result (got {out})"
    assert out[0]["text"] == "offline sync mode broke", \
        f"ranking selects higher-overlap prompt under cap (got {out})"
    print(f"{OK} ranking selects higher-overlap prompt under cap (got {out})")


def test_cwd_filter() -> None:
    _patch_candidates([
        _candidate("s1", "/proj-a", [("offline sync here", "2024-01-01")]),
        _candidate("s2", "/proj-b", [("offline sync there", "2024-01-02")]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="offline", cwds=("/proj-a",))
    finally:
        _reset_candidates()
    texts = {r["text"] for r in out}
    assert texts == {"offline sync here"}, f"cwd filter restricts results (got {texts})"
    print(f"{OK} cwd filter restricts results (got {texts})")


def test_is_noise_drops_preamble() -> None:
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
    assert texts == {"real offline requirement"}, f"is_noise drops programmatic preamble (got {texts})"
    print(f"{OK} is_noise drops programmatic preamble (got {texts})")


def test_dedup_across_sessions() -> None:
    _patch_candidates([
        _candidate("s1", "/proj", [("offline sync must survive", "2024-01-01")]),
        _candidate("s2", "/proj", [("offline sync must survive", "2024-01-02")]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="offline sync")
    finally:
        _reset_candidates()
    assert len(out) == 1, f"identical prompt across sessions deduped (got {len(out)})"
    assert out[0]["text"] == "offline sync must survive", \
        f"identical prompt across sessions deduped (got {out})"
    print(f"{OK} identical prompt across sessions deduped (got {len(out)})")


def test_bad_transcript_does_not_abort_search() -> None:
    _patch_candidates([
        _RaisingCandidate("/proj"),
        _candidate("s2", "/proj", [("offline sync survivor", "2024-01-02")]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="offline sync")
    finally:
        _reset_candidates()
    texts = {r["text"] for r in out}
    assert texts == {"offline sync survivor"}, f"non-OSError parse does not abort search (got {texts})"
    print(f"{OK} non-OSError parse does not abort search (got {texts})")


def test_deterministic_order_for_empty_ts_ties() -> None:
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
    assert first == expected, f"empty-ts ties order deterministically (first got {first})"
    assert second == expected, f"empty-ts ties order deterministically (second got {second})"
    print(f"{OK} empty-ts ties order deterministically (got {first} vs {second})")


def test_unlinked_transcript_found_via_filesystem_walk() -> None:
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
    assert "zulifrangible task widget" in texts, \
        f"unlinked claude transcript found via filesystem walk (got {texts})"
    print(f"{OK} unlinked claude transcript found via filesystem walk (got {texts})")


def test_codex_native_transcripts_found() -> None:
    """The native stores of every provider are covered — a codex rollout with
    no BA record must be found, and codex's injected ``<environment_context>``
    block must be dropped (not treated as a prompt)."""
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

    _disable_rg()
    orig = _isolate_native_roots(
        claude=[],
        codex=codex_root,
    )
    try:
        out = nsp.search_native_session_prompts(query="zulifrangible widget")
    finally:
        _restore_native_roots(orig)
        _restore_rg()
    texts = {r["text"] for r in out}
    assert "zulifrangible codex widget" in texts, \
        f"codex native transcripts found (got {texts})"
    assert not any("<environment_context>" in t for t in texts), \
        f"codex env-context dropped (got {texts})"
    print(f"{OK} codex native transcripts found, env-context dropped (got {texts})")


def test_categorizer_maps_elements_to_categories() -> None:
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
    assert not bad, f"categorizer maps kind+tool -> category (mismatches={bad})"
    print(f"{OK} categorizer maps kind+tool -> category (mismatches={bad})")


def _write_raw_transcript(records: list[dict]) -> Path:
    """Write arbitrary Claude-shaped jsonl lines (tool_use/tool_result/etc)."""
    global _seq
    _seq += 1
    path = _SCRATCH / f"raw_{_seq}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_rg_filter_narrows_to_files_containing_needle() -> None:
    """The rg match-first path finds only files containing the needle and builds
    candidates from those paths. Skipped when rg isn't installed."""
    _reset_candidates()
    if not nsp._rg_filter(["zulifrangible"]):
        # rg unavailable — the None fallback is covered by the other tests.
        print(f"{OK} rg-filter test skipped (rg not installed)")
        return
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
    import native_transcript_index as idx
    orig = _isolate_native_roots(claude=[projects], codex=_SCRATCH / "no-codex",
                                 runs=_SCRATCH / "no-runs")
    # `_matched_candidates` prefers the FTS index over rg; drop the index so
    # this test exercises the rg path it is named for (and cannot serve rows
    # indexed from outside the isolated roots).
    idx.reset_for_test()
    try:
        cands = nsp._matched_candidates(["zulifrangible"], set())
    finally:
        _restore_native_roots(orig)
        idx.reset_for_test()
    sids = {c.sid for c in cands}
    assert sids == {"rg-hit"}, f"rg filter narrows to needle files (got sids={sids})"
    print(f"{OK} rg filter narrows to needle files (got sids={sids})")


def test_index_fast_path_serves_query_with_rg_disabled() -> None:
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
    assert "zulifrangible fastpath needle" in texts, \
        f"index fast path serves query with rg disabled (got {texts})"
    print(f"{OK} index fast path serves query with rg disabled (got {texts})")


def test_generalized_search_greps_tool_calls_and_results() -> None:
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
    assert cats == {ElementCategory.REPLY, ElementCategory.SHELL, ElementCategory.TOOL_OUTPUT}, \
        f"generalized search cats (got {cats})"
    assert tools == {"Bash", ""}, f"generalized search tools (got {tools})"
    assert shell_cats == {ElementCategory.SHELL}, f"category filter shell-only (got {shell_cats})"
    assert len(shell_only) == 1, f"category filter keeps one shell hit (got {shell_only})"
    print(f"{OK} generalized search greps tools+results, category filter works "
          f"(cats={cats}, tools={tools}, shell_only={shell_cats})")


def test_sids_scopes_to_exactly_one_session() -> None:
    """``sids`` resolves candidates via filename lookup, bypassing
    ``_matched_candidates`` (and therefore rg/token discovery) entirely —
    only the requested session's own content is returned, from a project
    directory containing multiple sessions with distinct content, including
    a tool_result-only match (proving no category/kind exclusion applies)."""
    _reset_candidates()
    projects = _SCRATCH / "claude-projects-sids"
    cwd = "/Users/test/sids-proj"
    session_dir = projects / encode_cwd(cwd)
    session_dir.mkdir(parents=True, exist_ok=True)

    wanted_sid = "wanted-0000-0000-0000-000000000001"
    other_sid = "other-0000-0000-0000-000000000002"
    (session_dir / f"{wanted_sid}.jsonl").write_text(
        "\n".join([
            json.dumps({
                "type": "user", "uuid": "u1", "timestamp": "2024-01-01T00:00:00Z",
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "kranzelbeep tool output"},
                ]},
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    (session_dir / f"{other_sid}.jsonl").write_text(
        json.dumps({
            "type": "user", "uuid": "u2", "timestamp": "2024-01-01T00:00:00Z",
            "message": {"role": "user", "content": "kranzelbeep from the wrong session"},
        }) + "\n",
        encoding="utf-8",
    )

    _disable_rg()
    orig = _isolate_native_roots(claude=[projects])
    try:
        scoped = nsp.search_in_native_session_transcript(query="kranzelbeep", sids=(wanted_sid,))
        unscoped = nsp.search_in_native_session_transcript(query="kranzelbeep")
    finally:
        _restore_native_roots(orig)
        _restore_rg()

    scoped_sids = {r["sid"] for r in scoped}
    scoped_kinds = {r["element_kind"] for r in scoped}
    unscoped_sids = {r["sid"] for r in unscoped}
    assert scoped_sids == {wanted_sid}, f"sids scopes to wanted session (got {scoped_sids})"
    assert "tool_result" in scoped_kinds, f"sids keeps tool_result match (got {scoped_kinds})"
    assert unscoped_sids == {wanted_sid, other_sid}, \
        f"unscoped covers both sessions (got {unscoped_sids})"
    print(f"{OK} sids scopes to exactly one session incl. tool_result "
          f"(scoped_sids={scoped_sids}, scoped_kinds={scoped_kinds}, unscoped_sids={unscoped_sids})")


def test_sids_covers_pi_via_filename_and_content_fallback() -> None:
    """pi sessions are covered by sids two ways: the fast filename glob when
    the filename already matches the true id, and _pi_candidate_by_content_sid
    when it doesn't (the true id then only lives inside the file, per
    _pi_meta) — both must resolve to the requested session's own content."""
    _reset_candidates()
    pi_root = _SCRATCH / "pi-sessions"
    pi_root.mkdir(parents=True, exist_ok=True)

    # Filename already matches the true id (the common case).
    filename_match_sid = "pi-filename-match-0001"
    (pi_root / f"{filename_match_sid}.jsonl").write_text(
        "\n".join([
            json.dumps({"type": "session", "id": filename_match_sid, "cwd": "/proj-a",
                        "timestamp": "2024-01-01T00:00:00Z"}),
            json.dumps({"type": "message", "message": {"role": "user",
                        "content": "kranzelbeep from filename-matched pi session"}}),
        ]) + "\n",
        encoding="utf-8",
    )

    # Filename does NOT match the true id — only discoverable via content.
    true_sid = "pi-true-id-0002"
    (pi_root / "some-other-filename.jsonl").write_text(
        "\n".join([
            json.dumps({"type": "session", "id": true_sid, "cwd": "/proj-b",
                        "timestamp": "2024-01-01T00:00:00Z"}),
            json.dumps({"type": "message", "message": {"role": "user",
                        "content": "kranzelbeep from content-only pi session"}}),
        ]) + "\n",
        encoding="utf-8",
    )

    _disable_rg()
    orig = _isolate_native_roots(claude=[], pi=pi_root)
    try:
        by_filename = nsp.search_in_native_session_transcript(
            query="kranzelbeep", sids=(filename_match_sid,))
        by_content = nsp.search_in_native_session_transcript(
            query="kranzelbeep", sids=(true_sid,))
    finally:
        _restore_native_roots(orig)
        _restore_rg()

    filename_sids = {r["sid"] for r in by_filename}
    content_sids = {r["sid"] for r in by_content}
    assert filename_sids == {filename_match_sid}, \
        f"sids covers pi via filename match (got {filename_sids})"
    assert content_sids == {true_sid}, \
        f"sids covers pi via content fallback (got {content_sids})"
    print(f"{OK} sids covers pi via filename match and content fallback "
          f"(filename_sids={filename_sids}, content_sids={content_sids})")


def test_wiring_fails_closed_on_processor_error() -> None:
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
    assert resp.get("success") is False, f"processor error fails closed (got {resp})"
    assert resp.get("error") == "processor_failed", f"processor error surfaced (got {resp})"
    assert resp.get("text") == "", f"empty text on processor error (got {resp})"
    assert "fallback" not in resp, f"no raw fallback on processor error (got {resp})"
    assert "processor_error" not in resp, f"no processor_error key (got {resp})"
    print(f"{OK} processor error fails closed without raw fallback (got {resp})")


def test_wiring_real_requirements_not_replaced_by_fallback() -> None:
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
    assert resp.get("text") == "real processor requirement", \
        f"real requirements not replaced by fallback (got {resp})"
    assert resp.get("error") == "partial", f"truthy error preserved (got {resp})"
    print(f"{OK} real requirements not replaced by fallback (got {resp})")


def main_run() -> None:
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
        test_codex_native_transcripts_found,
        test_categorizer_maps_elements_to_categories,
        test_generalized_search_greps_tool_calls_and_results,
        test_sids_scopes_to_exactly_one_session,
        test_sids_covers_pi_via_filename_and_content_fallback,
        test_rg_filter_narrows_to_files_containing_needle,
        test_index_fast_path_serves_query_with_rg_disabled,
        test_wiring_fails_closed_on_processor_error,
        test_wiring_real_requirements_not_replaced_by_fallback,
    ]
    failures = []
    try:
        for fn in tests:
            try:
                fn()
            except AssertionError as e:
                print(f"{FAIL} {fn.__name__}: {e}")
                failures.append(fn.__name__)
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"{FAIL} {fn.__name__} raised: {e}")
                failures.append(fn.__name__)
            else:
                print(f"{OK} {fn.__name__}")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    n_pass = len(tests) - len(failures)
    n_total = len(tests)
    print(f"\n{n_pass}/{n_total} native-prompt-search tests passed")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main_run()
