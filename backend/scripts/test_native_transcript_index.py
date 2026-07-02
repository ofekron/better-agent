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
    shutil.rmtree(claude, ignore_errors=True)
    shutil.rmtree(codex, ignore_errors=True)
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
    cold = idx.quick_state()
    ok = not idx.is_usable() and not idx.is_covered() and cold == {
        "schema_ok": False,
        "covered": False,
        "usable": False,
    }
    idx.refresh_once()
    covered = idx.quick_state()
    ok = ok and idx.is_usable() and idx.is_covered() and covered == {
        "schema_ok": True,
        "covered": True,
        "usable": True,
    }
    print(f"{OK if ok else FAIL} quick_state/is_usable gated on covered "
          f"(cold={cold}, covered={covered})")
    return ok


def test_cold_full_build_commits_partial_progress_and_resumes() -> bool:
    claude, codex = _setup_roots()
    shutil.rmtree(claude, ignore_errors=True)
    shutil.rmtree(codex, ignore_errors=True)
    claude.mkdir(parents=True, exist_ok=True)
    codex.mkdir(parents=True, exist_ok=True)
    claude = _SCRATCH / "claude-projects"
    for i in range(5):
        _write_claude(claude / encode_cwd("/proj") / f"batch-{i}.jsonl", [f"batchneedle {i}"])

    original_batch = idx._FULL_REFRESH_FILE_BATCH
    idx._FULL_REFRESH_FILE_BATCH = 2
    try:
        first = idx.refresh_once()
        first_state = idx.quick_state()
        conn = idx._readonly_connection()
        first_files = conn.execute("SELECT COUNT(*) FROM native_file_state").fetchone()[0]
        first_rows = conn.execute("SELECT COUNT(*) FROM native_element_fts").fetchone()[0]
        idx.shutdown()
        idx._last_refresh_at = 0.0

        second = idx.refresh_once()
        second_files = idx._readonly_connection().execute(
            "SELECT COUNT(*) FROM native_file_state"
        ).fetchone()[0]
        third = idx.refresh_once()
        fourth = idx.refresh_once()
        final_state = idx.quick_state()
        rows = idx.search_rows(["batchneedle"], limit=10)
    finally:
        idx._FULL_REFRESH_FILE_BATCH = original_batch

    ok = (
        first["partial"] == 1
        and first_files == 2
        and first_rows == 2
        and first_state == {"schema_ok": True, "covered": False, "usable": False}
        and second["partial"] == 1
        and second_files == 4
        and third["partial"] == 0
        and fourth["partial"] == 0
        and final_state == {"schema_ok": True, "covered": True, "usable": True}
        and len(rows) == 5
    )
    print(f"{OK if ok else FAIL} cold full build commits partial progress and resumes "
          f"(first={first}, first_files={first_files}, second={second}, "
          f"second_files={second_files}, third={third}, fourth={fourth}, final={final_state})")
    return ok


def test_default_cold_build_batch_is_bounded() -> bool:
    claude, codex = _setup_roots()
    original_stat_walk = idx._stat_walk
    stat_walks = {"count": 0}
    def counted_stat_walk():
        stat_walks["count"] += 1
        return original_stat_walk()
    try:
        shutil.rmtree(claude, ignore_errors=True)
        shutil.rmtree(codex, ignore_errors=True)
        claude.mkdir(parents=True, exist_ok=True)
        codex.mkdir(parents=True, exist_ok=True)
        for i in range(idx._FULL_REFRESH_FILE_BATCH + 3):
            _write_claude(
                claude / encode_cwd("/proj") / f"default-batch-{i}.jsonl",
                [f"defaultbatchneedle {i}"],
            )

        idx._stat_walk = counted_stat_walk
        first = idx.refresh_once()
        first_state = idx.quick_state()
        first_files = idx._readonly_connection().execute(
            "SELECT COUNT(*) FROM native_file_state"
        ).fetchone()[0]
        queue_after_first = idx._readonly_connection().execute(
            "SELECT COUNT(*) FROM native_full_scan_queue WHERE processed = 0"
        ).fetchone()[0]
        progress_blob = idx._readonly_connection().execute(
            "SELECT value FROM native_corpus_state WHERE key = 'full_reconcile_progress'"
        ).fetchone()
        second = idx.refresh_once()
        final_state = idx.quick_state()
        rows = idx.search_rows(["defaultbatchneedle"], limit=idx._FULL_REFRESH_FILE_BATCH + 5)
    finally:
        idx._stat_walk = original_stat_walk
        shutil.rmtree(claude, ignore_errors=True)
        shutil.rmtree(codex, ignore_errors=True)

    ok = (
        first["partial"] == 1
        and first_files == idx._FULL_REFRESH_FILE_BATCH
        and queue_after_first == 3
        and progress_blob is None
        and first_state == {"schema_ok": True, "covered": False, "usable": False}
        and second["partial"] == 0
        and final_state == {"schema_ok": True, "covered": True, "usable": True}
        and len(rows) == idx._FULL_REFRESH_FILE_BATCH + 3
        and stat_walks["count"] == 1
    )
    print(f"{OK if ok else FAIL} default cold build batch is bounded "
          f"(batch={idx._FULL_REFRESH_FILE_BATCH}, first={first}, "
          f"first_files={first_files}, second={second}, final={final_state}, "
          f"stat_walks={stat_walks['count']}, queue_after_first={queue_after_first})")
    return ok


def test_partial_resume_does_not_scan_entire_queue() -> bool:
    claude, codex = _setup_roots()
    shutil.rmtree(claude, ignore_errors=True)
    shutil.rmtree(codex, ignore_errors=True)
    claude.mkdir(parents=True, exist_ok=True)
    codex.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        _write_claude(
            claude / encode_cwd("/proj") / f"resume-no-scan-{i}.jsonl",
            [f"resumenoscanneedle {i}"],
        )

    class GuardedConn:
        def __init__(self, inner):
            self.inner = inner
            self.full_queue_scans = 0

        def execute(self, sql, *args, **kwargs):
            normalized = " ".join(str(sql).split()).lower()
            if (
                "select path, tag, mtime, size from native_full_scan_queue" in normalized
                and "where" not in normalized
            ):
                self.full_queue_scans += 1
            return self.inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    original_batch = idx._FULL_REFRESH_FILE_BATCH
    idx._FULL_REFRESH_FILE_BATCH = 2
    try:
        first = idx.refresh_once()
        real_conn = idx._writer_conn
        guarded = GuardedConn(real_conn)
        idx._writer_conn = guarded
        second = idx.refresh_once()
        third = idx.refresh_once()
        final_state = idx.quick_state()
        rows = idx.search_rows(["resumenoscanneedle"], limit=10)
    finally:
        if isinstance(idx._writer_conn, GuardedConn):
            idx._writer_conn = idx._writer_conn.inner
        idx._FULL_REFRESH_FILE_BATCH = original_batch

    ok = (
        first["partial"] == 1
        and second["partial"] == 1
        and third["partial"] == 0
        and guarded.full_queue_scans == 0
        and final_state == {"schema_ok": True, "covered": True, "usable": True}
        and len(rows) == 5
    )
    print(f"{OK if ok else FAIL} partial resume avoids full detailed queue scans "
          f"(first={first}, second={second}, third={third}, "
          f"full_queue_scans={guarded.full_queue_scans}, final={final_state})")
    return ok


def test_partial_full_build_reconciles_deletes_before_final_covered() -> bool:
    claude, codex = _setup_roots()
    shutil.rmtree(claude, ignore_errors=True)
    shutil.rmtree(codex, ignore_errors=True)
    claude.mkdir(parents=True, exist_ok=True)
    codex.mkdir(parents=True, exist_ok=True)
    stale = claude / encode_cwd("/proj") / "stale.jsonl"
    _write_claude(stale, ["stalegone here"])
    idx.refresh_once()
    stale.unlink()
    for i in range(5):
        _write_claude(claude / encode_cwd("/proj") / f"delete-batch-{i}.jsonl", [f"deleteneedle {i}"])

    original_batch = idx._FULL_REFRESH_FILE_BATCH
    idx._FULL_REFRESH_FILE_BATCH = 2
    try:
        results = []
        for _ in range(3):
            results.append(idx.refresh_once(full=True))
        stale_rows = idx.search_rows(["stalegone"], limit=10)
        new_rows = idx.search_rows(["deleteneedle"], limit=10)
        final_state = idx.quick_state()
    finally:
        idx._FULL_REFRESH_FILE_BATCH = original_batch

    ok = (
        any(result["partial"] == 1 for result in results)
        and results[-1]["partial"] == 0
        and stale_rows == []
        and len(new_rows) == 5
        and final_state == {"schema_ok": True, "covered": True, "usable": True}
    )
    print(f"{OK if ok else FAIL} partial full build reconciles deletes before final covered "
          f"(results={results}, stale_rows={len(stale_rows)}, new_rows={len(new_rows)}, "
          f"final={final_state})")
    return ok


def test_covered_partial_full_queue_resumes_by_default() -> bool:
    claude, codex = _setup_roots()
    shutil.rmtree(claude, ignore_errors=True)
    shutil.rmtree(codex, ignore_errors=True)
    claude.mkdir(parents=True, exist_ok=True)
    codex.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        _write_claude(
            claude / encode_cwd("/proj") / f"covered-full-resume-{i}.jsonl",
            [f"coveredfullresume {i}"],
        )

    idx.refresh_once()
    original_batch = idx._FULL_REFRESH_FILE_BATCH
    idx._FULL_REFRESH_FILE_BATCH = 2
    try:
        first = idx.refresh_once(full=True)
        queue_after_first = idx._readonly_connection().execute(
            "SELECT COUNT(*) FROM native_full_scan_queue WHERE processed = 0"
        ).fetchone()[0]
        second = idx.refresh_once()
        third = idx.refresh_once()
        final_queue = idx._readonly_connection().execute(
            "SELECT COUNT(*) FROM native_full_scan_queue"
        ).fetchone()[0]
    finally:
        idx._FULL_REFRESH_FILE_BATCH = original_batch

    ok = (
        first["full"] == 1
        and first["partial"] == 1
        and queue_after_first == 3
        and second["full"] == 1
        and second["partial"] == 1
        and third["full"] == 1
        and third["partial"] == 0
        and final_queue == 0
    )
    print(f"{OK if ok else FAIL} covered partial full queue resumes by default "
          f"(first={first}, second={second}, third={third}, "
          f"queue_after_first={queue_after_first}, final_queue={final_queue})")
    return ok


def test_refresh_persists_batch_and_file_timings() -> bool:
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    _write_claude(claude / encode_cwd("/proj") / "timing-a.jsonl", ["timingneedle alpha"])
    _write_claude(claude / encode_cwd("/proj") / "timing-b.jsonl", ["timingneedle beta"])

    result = idx.refresh_once()
    conn = idx._readonly_connection()
    phase_blob = conn.execute(
        "SELECT value FROM native_corpus_state WHERE key = 'last_refresh_phase_timings_json'"
    ).fetchone()
    file_blob = conn.execute(
        "SELECT value FROM native_corpus_state WHERE key = 'last_refresh_slowest_files_json'"
    ).fetchone()
    phase_timings = json.loads(phase_blob[0]) if phase_blob else {}
    file_timings = json.loads(file_blob[0]) if file_blob else []

    required_phase_keys = {
        "plan_s", "fingerprint_s", "partial_decision_s", "index_s",
        "delete_s", "queue_mark_s", "state_s", "commit_s",
        "checkpoint_s", "total_s",
    }
    required_file_keys = {
        "path", "tag", "size", "rows", "total_s",
        "delete_s", "parse_s", "insert_s", "state_s",
    }
    ok = (
        result["touched"] == 2
        and required_phase_keys <= set(phase_timings)
        and all(isinstance(phase_timings[key], (int, float)) for key in required_phase_keys)
        and len(file_timings) == 2
        and all(required_file_keys <= set(row) for row in file_timings)
        and all(row["rows"] == 1 for row in file_timings)
        and file_timings == sorted(file_timings, key=lambda row: row["total_s"], reverse=True)
    )
    print(f"{OK if ok else FAIL} refresh persists batch/file timings "
          f"(result={result}, phases={sorted(phase_timings)}, files={len(file_timings)})")
    return ok


def test_reindex_deletes_fts_rows_by_rowid_not_path_scan() -> bool:
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    fpath = claude / encode_cwd("/proj") / "rowid-delete.jsonl"
    _write_claude(fpath, ["rowiddeleteneedle first"])
    idx.refresh_once()

    class GuardedConn:
        def __init__(self, inner):
            self.inner = inner
            self.fts_path_deletes = 0
            self.fts_rowid_deletes = 0

        def execute(self, sql, *args, **kwargs):
            self._count(sql)
            return self.inner.execute(sql, *args, **kwargs)

        def executemany(self, sql, *args, **kwargs):
            self._count(sql)
            return self.inner.executemany(sql, *args, **kwargs)

        def _count(self, sql):
            normalized = " ".join(str(sql).split()).lower()
            if "delete from native_element_fts where path" in normalized:
                self.fts_path_deletes += 1
            if "delete from native_element_fts where rowid" in normalized:
                self.fts_rowid_deletes += 1

        def __getattr__(self, name):
            return getattr(self.inner, name)

    time.sleep(1.05)
    _write_claude(fpath, ["rowiddeleteneedle second"])
    real_conn = idx._writer_conn
    guarded = GuardedConn(real_conn)
    idx._writer_conn = guarded
    try:
        result = idx.refresh_once()
        rows = idx.search_rows(["rowiddeleteneedle"], limit=10)
        mapped_rows = idx._readonly_connection().execute(
            "SELECT COUNT(*) FROM native_element_path"
        ).fetchone()[0]
    finally:
        if isinstance(idx._writer_conn, GuardedConn):
            idx._writer_conn = idx._writer_conn.inner

    ok = (
        result["touched"] == 1
        and guarded.fts_path_deletes == 0
        and guarded.fts_rowid_deletes >= 1
        and len(rows) == 1
        and rows[0]["text"] == "rowiddeleteneedle second"
        and mapped_rows == 1
    )
    print(f"{OK if ok else FAIL} reindex deletes FTS rows by rowid not path scan "
          f"(result={result}, path_deletes={guarded.fts_path_deletes}, "
          f"rowid_deletes={guarded.fts_rowid_deletes}, rows={len(rows)}, mapped={mapped_rows})")
    return ok


def test_full_walk_ignores_non_transcript_run_jsonl() -> bool:
    claude, codex = _setup_roots()
    runs = _SCRATCH / "runs"
    shutil.rmtree(runs, ignore_errors=True)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "run_state_index.jsonl").write_text(
        json.dumps({"not": "a transcript", "content": "ignoredrunstate"}) + "\n",
        encoding="utf-8",
    )
    run_dir = runs / "run-1"
    _write_claude(run_dir / "session_events.jsonl", ["realrunneedle here"])

    nsp._native_roots = lambda: [(claude, "claude"), (codex, "codex"), (runs, "runs")]
    idx.reset_for_test()
    result = idx.refresh_once()
    ignored = idx.search_rows(["ignoredrunstate"], limit=10)
    found = idx.search_rows(["realrunneedle"], limit=10)
    indexed_paths = {
        row[0] for row in idx._readonly_connection().execute(
            "SELECT path FROM native_file_state"
        )
    }

    ok = (
        result["walked"] == 1
        and ignored == []
        and len(found) == 1
        and str(runs / "run_state_index.jsonl") not in indexed_paths
        and str(run_dir / "session_events.jsonl") in indexed_paths
    )
    print(f"{OK if ok else FAIL} full walk ignores non-transcript run jsonl "
          f"(result={result}, ignored={len(ignored)}, found={len(found)}, indexed={len(indexed_paths)})")
    return ok


def test_steady_refresh_purges_preexisting_non_transcript_run_jsonl() -> bool:
    claude, codex = _setup_roots()
    runs = _SCRATCH / "runs"
    shutil.rmtree(runs, ignore_errors=True)
    runs.mkdir(parents=True, exist_ok=True)
    stale = runs / "run_state_index.jsonl"
    _write_claude(stale, ["stalerunstateneedle"])
    run_dir = runs / "run-1"
    _write_claude(run_dir / "session_events.jsonl", ["keptrunneedle"])

    nsp._native_roots = lambda: [(claude, "claude"), (codex, "codex"), (runs, "runs")]
    idx.reset_for_test()
    conn = idx._writer_connection()
    candidate = nsp._candidate_from_match(stale, "runs")
    idx._replace_candidate(
        conn,
        candidate,
        stale.stat().st_mtime,
        stale.stat().st_size,
        source_tag="runs",
    )
    idx._state_set(conn, "schema_version", str(idx._SCHEMA_VERSION))
    idx._state_set(conn, "covered", "1")
    idx._state_set(conn, "last_walk_at", str(time.time()))
    conn.commit()

    before = idx.search_rows(["stalerunstateneedle"], limit=10)
    result = idx.refresh_once()
    after = idx.search_rows(["stalerunstateneedle"], limit=10)
    indexed_paths = {
        row[0] for row in idx._readonly_connection().execute(
            "SELECT path FROM native_file_state"
        )
    }

    ok = (
        len(before) == 1
        and result["touched"] >= 1
        and after == []
        and str(stale) not in indexed_paths
    )
    print(f"{OK if ok else FAIL} steady refresh purges stale non-transcript run jsonl "
          f"(result={result}, before={len(before)}, after={len(after)}, indexed={len(indexed_paths)})")
    return ok


def test_steady_refresh_is_bounded_over_indexed_paths() -> bool:
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    for i in range(5):
        _write_claude(
            claude / encode_cwd("/proj") / f"bounded-steady-{i}.jsonl",
            [f"boundedsteadyneedle {i}"],
        )
    idx.refresh_once()

    class GuardedConn:
        def __init__(self, inner):
            self.inner = inner
            self.full_file_state_scans = 0

        def execute(self, sql, *args, **kwargs):
            normalized = " ".join(str(sql).split()).lower()
            if (
                "select path, tag, mtime, size from native_file_state" in normalized
                and "limit" not in normalized
            ):
                self.full_file_state_scans += 1
            return self.inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    original_batch = idx._STEADY_REFRESH_FILE_BATCH
    idx._STEADY_REFRESH_FILE_BATCH = 2
    real_conn = idx._writer_conn
    guarded = GuardedConn(real_conn)
    idx._writer_conn = guarded
    try:
        result = idx.refresh_once()
        cursor = idx._state_get(idx._readonly_connection(), "steady_refresh_cursor")
    finally:
        if isinstance(idx._writer_conn, GuardedConn):
            idx._writer_conn = idx._writer_conn.inner
        idx._STEADY_REFRESH_FILE_BATCH = original_batch

    ok = (
        result["full"] == 0
        and result["walked"] == 2
        and guarded.full_file_state_scans == 0
        and isinstance(cursor, str)
        and cursor.endswith("bounded-steady-1.jsonl")
    )
    print(f"{OK if ok else FAIL} steady refresh is bounded over indexed paths "
          f"(result={result}, scans={guarded.full_file_state_scans}, cursor={cursor})")
    return ok


def test_refresh_stamps_freshness_after_index_work() -> bool:
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    _write_claude(claude / encode_cwd("/proj") / "fresh-after-work.jsonl", ["freshafterwork"])

    fake_time = {"now": 1000.0}
    original_time = idx.time.time
    original_replace = idx._replace_candidate

    def fake_now():
        return fake_time["now"]

    def delayed_replace(*args, **kwargs):
        fake_time["now"] = 1010.0
        return original_replace(*args, **kwargs)

    try:
        idx.time.time = fake_now
        idx._replace_candidate = delayed_replace
        result = idx.refresh_once()
        last_walk_at = float(idx._state_get(idx._readonly_connection(), "last_walk_at") or 0)
    finally:
        idx.time.time = original_time
        idx._replace_candidate = original_replace

    ok = result["touched"] == 1 and last_walk_at == 1010.0
    print(f"{OK if ok else FAIL} refresh stamps freshness after index work "
          f"(result={result}, last_walk_at={last_walk_at})")
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
    conn = idx._writer_connection()
    idx._state_set(conn, "last_walk_at", str(time.time() - 60.0))
    conn.commit()
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


def test_request_refresh_persists_cross_process_marker() -> bool:
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    _write_claude(claude / encode_cwd("/proj") / "a.jsonl", ["markerneedle here"])
    idx.refresh_once()
    idx.request_refresh()
    conn = idx._readonly_connection()
    requested_at = idx._state_float(conn, idx._REFRESH_REQUESTED_AT_KEY)
    handled_at = idx._state_float(conn, idx._REFRESH_HANDLED_AT_KEY)
    ok = requested_at > handled_at and idx._refresh_request_pending()
    print(f"{OK if ok else FAIL} request_refresh persists cross-process marker "
          f"(requested={requested_at}, handled={handled_at})")
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


def test_ensure_started_spawns_external_worker_process() -> bool:
    _setup_roots()
    calls = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))
            self.pid = 424242
            self._terminated = False

        def poll(self):
            return None if not self._terminated else 0

        def terminate(self):
            self._terminated = True

        def wait(self, timeout=None):
            self._terminated = True
            return 0

        def kill(self):
            self._terminated = True

    original_popen = idx.subprocess.Popen
    try:
        idx.subprocess.Popen = FakePopen
        idx.ensure_started()
        spawned = idx._worker_process
        idx.shutdown()
    finally:
        idx.subprocess.Popen = original_popen

    ok = (
        len(calls) == 1
        and calls[0][0][-1] == idx._WORKER_ARG
        and spawned is not None
        and idx._worker_thread is None
        and idx._worker_process is None
        and not idx._worker_started
    )
    print(f"{OK if ok else FAIL} ensure_started spawns external worker process "
          f"(calls={len(calls)}, worker_thread={idx._worker_thread})")
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
        test_cold_full_build_commits_partial_progress_and_resumes,
        test_default_cold_build_batch_is_bounded,
        test_partial_resume_does_not_scan_entire_queue,
        test_partial_full_build_reconciles_deletes_before_final_covered,
        test_covered_partial_full_queue_resumes_by_default,
        test_refresh_persists_batch_and_file_timings,
        test_reindex_deletes_fts_rows_by_rowid_not_path_scan,
        test_full_walk_ignores_non_transcript_run_jsonl,
        test_steady_refresh_purges_preexisting_non_transcript_run_jsonl,
        test_steady_refresh_is_bounded_over_indexed_paths,
        test_refresh_stamps_freshness_after_index_work,
        test_broad_match_signals_fallback,
        test_wait_fresh_serves_delta_instead_of_falling_back,
        test_request_refresh_persists_cross_process_marker,
        test_refresh_reports_locked_instead_of_colliding,
        test_ensure_started_spawns_external_worker_process,
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
