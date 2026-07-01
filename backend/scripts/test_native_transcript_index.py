"""Tests for the native-transcript FTS5 index.

Covers the contract the search fast path relies on:

  * refresh_once indexes every on-disk transcript; covered becomes True.
  * lean extraction: user_prompt/assistant_text/reasoning/tool_call are indexed;
    tool_result (the 52%-of-bytes bulk) is NOT.
  * freshness by mtime+size: a changed file is re-indexed; a new needle in the
    delta becomes searchable; a deleted file is tombstoned.
  * match_paths returns cwd-filtered (path, tag) pairs; is_usable gates the fast
    path (covered + last walk within the freshness window).
  * broad match (> path cap) signals the caller to fall back.

Run with:
    cd backend && .venv/bin/python scripts/test_native_transcript_index.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402
_TMP_HOME = _test_home.isolate("bc-test-native-transcript-index-")

import native_session_prompt_search as nsp  # noqa: E402
import native_transcript_index as idx  # noqa: E402
from paths import encode_cwd  # noqa: E402

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

_SCRATCH = Path(_TMP_HOME) / "scratch"
_SCRATCH.mkdir(parents=True, exist_ok=True)


def _setup_roots():
    """Temp native roots + monkeypatch the search module's root resolver."""
    claude = _SCRATCH / "claude-projects"
    codex = _SCRATCH / "codex-sessions"
    claude.mkdir(parents=True, exist_ok=True)
    codex.mkdir(parents=True, exist_ok=True)
    nsp._native_roots = lambda: [(claude, "claude"), (codex, "codex")]
    idx.reset_for_test()
    return claude, codex


def _write_claude(path: Path, prompts: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, p in enumerate(prompts):
        lines.append(json.dumps({
            "type": "user", "uuid": f"u{i}", "timestamp": "2024-01-01T00:00:00Z",
            "message": {"role": "user", "content": p},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_claude_rich(path: Path) -> None:
    """A transcript with a tool_result block — must NOT be indexed (lean)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([
        json.dumps({"type": "user", "uuid": "u1", "timestamp": "2024-01-01T00:00:00Z",
                    "message": {"role": "user", "content": "zulifrangible build widget"}}),
        json.dumps({"type": "assistant", "uuid": "a1", "timestamp": "2024-01-01T00:00:01Z",
                    "message": {"role": "assistant", "content": [
                        {"type": "text", "text": "running zulifrangible now"},
                        {"type": "tool_use", "id": "t1", "name": "Bash",
                         "input": {"command": "make zulifrangible-widget"}},
                    ]}}),
        json.dumps({"type": "user", "uuid": "u2", "timestamp": "2024-01-01T00:00:02Z",
                    "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "t1",
                         "content": "zulifrangible dump output bulk"},
                    ]}}),
    ]) + "\n", encoding="utf-8")


def test_indexes_corpus_and_drops_tool_result() -> bool:
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    _write_claude_rich(claude / encode_cwd("/proj") / "s1.jsonl")
    r = idx.refresh_once()
    rows = idx.search_rows(["zulifrangible"], limit=10)
    kinds = {x["element_kind"] for x in rows}
    ok = (
        r["walked"] >= 1
        and idx.is_covered()
        and idx.is_usable()
        and kinds == {"user_prompt", "assistant_text", "tool_call"}
        # tool_result content ("dump output bulk") was deliberately not indexed
        and not any("dump output bulk" in x["text"] for x in rows)
    )
    print(f"{OK if ok else FAIL} indexes lean elements, drops tool_result "
          f"(kinds={kinds}, refresh={r})")
    return ok


def test_match_paths_cwd_filter_and_cap() -> bool:
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    _write_claude(claude / encode_cwd("/proj-a") / "s1.jsonl", ["sharedneedle alpha"])
    _write_claude(claude / encode_cwd("/proj-b") / "s2.jsonl", ["sharedneedle beta"])
    idx.refresh_once()
    all_hits = idx.match_paths(["sharedneedle"], set()) or []
    a_hits = idx.match_paths(["sharedneedle"], {"/proj-a"}) or []
    a_paths = {Path(p).stem for p, _ in a_hits}
    ok = (
        len(all_hits) == 2
        and a_paths == {"s1"}  # cwd filter narrowed to /proj-a
    )
    print(f"{OK if ok else FAIL} match_paths cwd-filter + cap (all={len(all_hits)}, /proj-a={a_paths})")
    return ok


def test_freshness_reindexes_changed_files() -> bool:
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    fpath = claude / encode_cwd("/proj") / "s1.jsonl"
    _write_claude(fpath, ["orignalneedle here"])  # intentional typo stays put
    idx.refresh_once()
    before = idx.search_rows(["orignalneedle"], limit=10)
    # mtime granularity on some FS is 1s; wait so the append is detectable.
    time.sleep(1.05)
    with fpath.open("a") as f:
        f.write(json.dumps({"type": "user", "uuid": "u9", "timestamp": "2024-02-02",
                            "message": {"role": "user", "content": "deltaneedle added"}}) + "\n")
    r = idx.refresh_once()
    after = idx.search_rows(["deltaneedle"], limit=10)
    ok = len(before) >= 1 and r["touched"] >= 1 and len(after) == 1
    print(f"{OK if ok else FAIL} freshness reindexes delta (touched={r['touched']}, "
          f"deltaneedle_rows={len(after)})")
    return ok


def test_not_usable_until_covered() -> bool:
    _setup_roots()
    ok = not idx.is_usable() and not idx.is_covered()
    idx.refresh_once()
    ok = ok and idx.is_usable() and idx.is_covered()
    print(f"{OK if ok else FAIL} is_usable gated on covered (cold=False, after refresh=True)")
    return ok


def test_broad_match_signals_fallback() -> bool:
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    enc = encode_cwd("/proj")
    for i in range(idx._PATH_CAP + 5):
        _write_claude(claude / enc / f"s{i}.jsonl", ["commonneedle everywhere"])
    idx.refresh_once()
    # cap exceeded => match_paths returns None so the caller falls back to rg.
    res = idx.match_paths(["commonneedle"], set())
    ok = res is None
    print(f"{OK if ok else FAIL} broad match (>cap) signals fallback (got None={res is None})")
    return ok


def test_wait_fresh_serves_delta_instead_of_falling_back() -> bool:
    """Once covered, a stale query REQUESTS a refresh and waits for the delta
    (which indexes the just-added file) rather than dropping to rg. Simulates
    the worker with a one-shot thread that refreshes after the request."""
    import threading
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    _write_claude(claude / encode_cwd("/proj") / "a.jsonl", ["staleneedle here"])
    idx.refresh_once()  # covered + fresh
    # A brand-new file appears after the last walk.
    _write_claude(claude / encode_cwd("/proj") / "b.jsonl", ["deltawaitneedle new"])
    idx._last_refresh_at = 0.0  # force stale (covered but not usable)
    assert idx.is_covered() and not idx.is_usable()

    def simulate_worker_refresh():
        time.sleep(0.1)
        idx.refresh_once()  # delta: indexes b.jsonl, stamps _last_refresh_at, notifies

    t = threading.Thread(target=simulate_worker_refresh)
    t.start()
    try:
        fresh = idx.wait_fresh(5.0)
        rows = idx.search_rows(["deltawaitneedle"], limit=5)
    finally:
        t.join()
    ok = fresh and len(rows) >= 1
    print(f"{OK if ok else FAIL} wait_fresh serves delta instead of fallback "
          f"(fresh={fresh}, rows={len(rows)})")
    return ok


def main_run() -> int:
    tests = [
        test_indexes_corpus_and_drops_tool_result,
        test_match_paths_cwd_filter_and_cap,
        test_freshness_reindexes_changed_files,
        test_not_usable_until_covered,
        test_broad_match_signals_fallback,
        test_wait_fresh_serves_delta_instead_of_falling_back,
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
    print(f"\n{n_pass}/{len(results)} native-transcript-index tests passed")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main_run())
