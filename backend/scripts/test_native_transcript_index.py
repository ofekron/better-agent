"""Tests for the native-transcript FTS5 index.

Covers the contract the search fast path relies on:

  * refresh_once indexes every on-disk transcript; covered becomes True.
  * lean extraction: user_prompt/assistant_text/reasoning/tool_call are indexed;
    tool_result (the 52%-of-bytes bulk) is NOT.
  * freshness by mtime+size: a changed file is re-indexed; a new needle in the
    delta becomes searchable; forced full reconcile discovers external files.
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
    metadata_ok = all(
        {"role", "element_id", "element_index"} <= set(row)
        for row in rows
    )
    ordered_indexes = [row["element_index"] for row in rows]
    ok = (
        r["walked"] >= 1
        and idx.is_covered()
        and idx.is_usable()
        and kinds == {"user_prompt", "assistant_text", "tool_call"}
        and metadata_ok
        and ordered_indexes == [0, 1, 2]
        # tool_result content ("dump output bulk") was deliberately not indexed
        and not any("dump output bulk" in x["text"] for x in rows)
    )
    print(f"{OK if ok else FAIL} indexes lean elements, drops tool_result "
          f"(kinds={kinds}, refresh={r})")
    return ok


def test_old_schema_cache_rebuilds() -> bool:
    _setup_roots()
    conn = idx._writer_connection()
    conn.execute("DROP TABLE native_element_fts")
    conn.executescript(
        """
        CREATE VIRTUAL TABLE native_element_fts USING fts5(
            text,
            path UNINDEXED,
            sid UNINDEXED,
            cwd UNINDEXED,
            tag UNINDEXED,
            element_kind UNINDEXED,
            tool_name UNINDEXED,
            ts UNINDEXED,
            tokenize='unicode61'
        );
        """
    )
    conn.execute(
        "INSERT INTO native_file_state(path, mtime, size, tag, sid, cwd, indexed_at) "
        "VALUES ('stale.jsonl', 1, 1, 'claude', 'stale', '/stale', 1)"
    )
    conn.commit()
    idx.shutdown()

    conn = idx._writer_connection()
    columns = tuple(row[1] for row in conn.execute("PRAGMA table_info(native_element_fts)"))
    stale_rows = conn.execute("SELECT count(*) FROM native_file_state").fetchone()[0]
    ok = columns == idx._FTS_COLUMNS and stale_rows == 0
    print(f"{OK if ok else FAIL} old schema cache rebuilds (columns={columns}, stale_rows={stale_rows})")
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


def test_covered_refresh_does_not_full_walk() -> bool:
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    fpath = claude / encode_cwd("/proj") / "s1.jsonl"
    _write_claude(fpath, ["knownneedle here"])
    idx.refresh_once()

    called = {"stat_walk": 0}
    original = idx._stat_walk
    idx._stat_walk = lambda: called.__setitem__("stat_walk", called["stat_walk"] + 1) or original()
    try:
        time.sleep(1.05)
        with fpath.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "uuid": "u9", "timestamp": "2024-02-02",
                                "message": {"role": "user", "content": "steadyneedle added"}}) + "\n")
        r = idx.refresh_once()
        rows = idx.search_rows(["steadyneedle"], limit=10)
    finally:
        idx._stat_walk = original
    ok = called["stat_walk"] == 0 and r["full"] == 0 and r["touched"] >= 1 and len(rows) == 1
    print(f"{OK if ok else FAIL} covered refresh avoids full walk "
          f"(stat_walk={called['stat_walk']}, refresh={r}, rows={len(rows)})")
    return ok


def test_forced_full_reconcile_discovers_external_files() -> bool:
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    _write_claude(claude / encode_cwd("/proj") / "a.jsonl", ["firstneedle here"])
    idx.refresh_once()
    _write_claude(claude / encode_cwd("/proj") / "b.jsonl", ["externalneedle new"])
    steady = idx.refresh_once()
    before = idx.search_rows(["externalneedle"], limit=10)
    full = idx.refresh_once(full=True)
    after = idx.search_rows(["externalneedle"], limit=10)
    ok = steady["full"] == 0 and len(before) == 0 and full["full"] == 1 and len(after) == 1
    print(f"{OK if ok else FAIL} forced full reconcile discovers external files "
          f"(steady={steady}, full={full}, before={len(before)}, after={len(after)})")
    return ok


def test_restart_covered_worker_does_not_immediately_full_walk() -> bool:
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    _write_claude(claude / encode_cwd("/proj") / "a.jsonl", ["restartneedle here"])
    idx.refresh_once()
    idx._last_full_reconcile_at = 0.0
    ok = idx.is_covered() and not idx._full_reconcile_due()
    print(f"{OK if ok else FAIL} covered restart reads full-reconcile timestamp "
          f"(last_full={idx._last_full_reconcile_at:.1f})")
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
    over indexed paths rather than dropping to rg. Simulates the worker with a
    one-shot thread that refreshes after the request."""
    import threading
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    _write_claude(claude / encode_cwd("/proj") / "a.jsonl", ["staleneedle here"])
    idx.refresh_once()  # covered + fresh
    # A known indexed file grows after the last walk.
    fpath = claude / encode_cwd("/proj") / "a.jsonl"
    time.sleep(1.05)
    with fpath.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "uuid": "u9", "timestamp": "2024-02-02",
                            "message": {"role": "user", "content": "deltawaitneedle new"}}) + "\n")
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


def test_refresh_reports_locked_instead_of_colliding() -> bool:
    _setup_roots()
    lock_path = idx._writer_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        import portable_lock
        portable_lock.lock_ex(handle.fileno())
        result = idx.refresh_once()
    finally:
        portable_lock.unlock(handle.fileno())
        handle.close()
    ok = result == {"walked": 0, "touched": 0, "locked": 1}
    print(f"{OK if ok else FAIL} refresh reports locked instead of colliding (result={result})")
    return ok


def main_run() -> int:
    tests = [
        test_indexes_corpus_and_drops_tool_result,
        test_old_schema_cache_rebuilds,
        test_match_paths_cwd_filter_and_cap,
        test_freshness_reindexes_changed_files,
        test_covered_refresh_does_not_full_walk,
        test_forced_full_reconcile_discovers_external_files,
        test_restart_covered_worker_does_not_immediately_full_walk,
        test_not_usable_until_covered,
        test_broad_match_signals_fallback,
        test_wait_fresh_serves_delta_instead_of_falling_back,
        test_refresh_reports_locked_instead_of_colliding,
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
