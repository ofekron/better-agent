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

import config_store  # noqa: E402
import native_session_prompt_search as nsp  # noqa: E402
import native_transcript_index as idx  # noqa: E402
import native_session_miner as nm  # noqa: E402
import paths  # noqa: E402
from paths import encode_cwd  # noqa: E402

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

_SCRATCH = Path(_TMP_HOME) / "scratch"
_SCRATCH.mkdir(parents=True, exist_ok=True)


def _setup_roots():
    """Temp native roots + monkeypatch the search module's root resolver."""
    claude = _SCRATCH / "claude-projects"
    codex = _SCRATCH / "codex-sessions"
    pi = _SCRATCH / "pi-sessions"
    shutil.rmtree(claude, ignore_errors=True)
    shutil.rmtree(codex, ignore_errors=True)
    shutil.rmtree(pi, ignore_errors=True)
    claude.mkdir(parents=True, exist_ok=True)
    codex.mkdir(parents=True, exist_ok=True)
    pi.mkdir(parents=True, exist_ok=True)
    nsp._native_roots = lambda: [(claude, "claude"), (codex, "codex"), (pi, "pi")]
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


def _write_claude_events(path: Path, events: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({
            "type": "user", "uuid": uid, "timestamp": ts,
            "message": {"role": "user", "content": text},
        })
        for uid, ts, text in events
    ]
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


def _write_pi_rich(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([
        json.dumps({"type": "session", "version": 3, "id": "pi-session",
                    "timestamp": "2026-01-01T00:00:00Z", "cwd": "/proj-pi"}),
        json.dumps({"type": "message", "id": "p1", "parentId": None,
                    "timestamp": "2026-01-01T00:00:01Z",
                    "message": {"role": "user", "content": "pizulifrangible inspect"}}),
        json.dumps({"type": "message", "id": "p2", "parentId": "p1",
                    "timestamp": "2026-01-01T00:00:02Z",
                    "message": {"role": "assistant", "content": [
                        {"type": "thinking", "thinking": "pizulifrangible reasoning"},
                        {"type": "text", "text": "pizulifrangible answer"},
                        {"type": "toolCall", "id": "tool-1", "name": "bash",
                         "arguments": {"command": "echo pizulifrangible"}},
                    ]}}),
        json.dumps({"type": "message", "id": "p3", "parentId": "p2",
                    "timestamp": "2026-01-01T00:00:03Z",
                    "message": {"role": "toolResult", "toolCallId": "tool-1",
                                "toolName": "bash", "content": [{"type": "text", "text": "pizulifrangible bulk"}],
                                "isError": False}}),
    ]) + "\n", encoding="utf-8")


def _write_old_codex(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "timestamp": "2025-10-20T07:05:35.594Z",
            "type": "event_msg",
            "payload": {"type": "session_meta", "id": "codex-old", "cwd": "/old-codex"},
        },
        {
            "timestamp": "2025-10-20T07:05:35.594Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<environment_context>meta</environment_context>"}],
            },
        },
        {
            "timestamp": "2025-10-20T07:05:55.926Z",
            "type": "response_item",
            "payload": {
                "id": "old-user",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "oldcodexneedle build the thing"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


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


def test_indexes_pi_sessions() -> bool:
    _setup_roots()
    pi = _SCRATCH / "pi-sessions"
    _write_pi_rich(pi / "--proj-pi--" / "2026-01-01T00-00-00-000Z_pi-session.jsonl")
    r = idx.refresh_once()
    rows = idx.search_rows(["pizulifrangible"], limit=20)
    kinds = {x["element_kind"] for x in rows}
    by_kind = {x["element_kind"]: x for x in rows}
    ok = (
        r["walked"] >= 1
        and idx.is_covered()
        and kinds == {"user_prompt", "assistant_text", "reasoning", "tool_call"}
        and by_kind["user_prompt"]["tag"] == "pi"
        and by_kind["user_prompt"]["sid"] == "pi-session"
        and by_kind["user_prompt"]["cwd"] == "/proj-pi"
        and not any("bulk" in x["text"] for x in rows)
    )
    print(f"{OK if ok else FAIL} indexes pi sessions leanly (kinds={kinds}, refresh={r})")
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
    meta_columns = tuple(row[1] for row in conn.execute("PRAGMA table_info(native_element_meta)"))
    stale_rows = conn.execute("SELECT count(*) FROM native_file_state").fetchone()[0]
    ok = (
        columns == idx._FTS_COLUMNS
        and meta_columns == ("rowid", *idx._META_COLUMNS)
        and stale_rows == 0
    )
    print(f"{OK if ok else FAIL} old schema cache rebuilds "
          f"(columns={columns}, meta_columns={meta_columns}, stale_rows={stale_rows})")
    return ok


def test_timestamp_utc_orders_offsets_chronologically() -> bool:
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    _write_claude_events(claude / encode_cwd("/proj") / "time.jsonl", [
        ("old-offset", "2026-01-01T12:00:00+03:00", "chrononeedle older offset"),
        ("new-z", "2026-01-01T10:00:00Z", "chrononeedle newer zulu"),
    ])
    idx.refresh_once()
    out = idx.run_readonly_sql(
        "SELECT element_id, ts_utc FROM native_element_fts "
        "WHERE native_element_fts MATCH 'chrononeedle' ORDER BY ts_utc DESC"
    )
    rows = out.get("rows") or []
    ok = (
        out.get("error") is None
        and [row[0] for row in rows] == ["new-z", "old-offset"]
        and rows[0][1] == "2026-01-01T10:00:00.000000Z"
        and rows[1][1] == "2026-01-01T09:00:00.000000Z"
        and "ts" not in idx._FTS_COLUMNS
    )
    print(f"{OK if ok else FAIL} ts_utc orders offset timestamps chronologically (rows={rows})")
    return ok


def test_provider_roots_ignore_spoofed_home() -> bool:
    real_home = _SCRATCH / "real-native-home"
    fake_home = _SCRATCH / "fake-native-home"
    shutil.rmtree(real_home, ignore_errors=True)
    shutil.rmtree(fake_home, ignore_errors=True)
    (real_home / ".codex" / "sessions").mkdir(parents=True)
    (real_home / ".gemini" / "tmp").mkdir(parents=True)
    (real_home / ".pi" / "agent" / "sessions").mkdir(parents=True)
    (real_home / ".codeium" / "cascade").mkdir(parents=True)
    (real_home / ".claude-old" / "projects").mkdir(parents=True)
    (fake_home / ".codex" / "sessions").mkdir(parents=True)
    (fake_home / ".claude-fake" / "projects").mkdir(parents=True)

    old_home = os.environ.get("HOME")
    old_pi_session_dir = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    old_pi_agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
    old_user_home = paths._USER_HOME
    old_list_providers = config_store.list_providers
    try:
        os.environ["HOME"] = str(fake_home)
        os.environ.pop("PI_CODING_AGENT_SESSION_DIR", None)
        os.environ.pop("PI_CODING_AGENT_DIR", None)
        paths._USER_HOME = real_home
        config_store.list_providers = lambda: {"providers": [
            {"id": "claude-old", "kind": "claude", "config_dir": str(real_home / ".claude-old")},
        ]}

        claude_roots = nm._claude_projects_roots()
        ok = (
            nm._codex_sessions_root() == real_home / ".codex" / "sessions"
            and nm._gemini_chats_root() == real_home / ".gemini" / "tmp"
            and nm._pi_sessions_root() == real_home / ".pi" / "agent" / "sessions"
            and nm._windsurf_cascade_roots() == [real_home / ".codeium" / "cascade"]
            and real_home / ".claude-old" / "projects" in claude_roots
            and fake_home / ".claude-fake" / "projects" not in claude_roots
            and paths.resolve_claude_config_dir("~/.claude-zai") == real_home / ".claude-zai"
            and paths.resolve_claude_config_dir(".claude-rel") == real_home / ".claude-rel"
        )
    finally:
        config_store.list_providers = old_list_providers
        paths._USER_HOME = old_user_home
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_pi_session_dir is None:
            os.environ.pop("PI_CODING_AGENT_SESSION_DIR", None)
        else:
            os.environ["PI_CODING_AGENT_SESSION_DIR"] = old_pi_session_dir
        if old_pi_agent_dir is None:
            os.environ.pop("PI_CODING_AGENT_DIR", None)
        else:
            os.environ["PI_CODING_AGENT_DIR"] = old_pi_agent_dir

    print(f"{OK if ok else FAIL} provider roots ignore spoofed HOME")
    return ok


def test_native_roots_dedupes_symlinked_real_path() -> bool:
    real_config = _SCRATCH / "real-zai"
    real_root = real_config / "projects"
    overlay_home = _SCRATCH / "codex-overlay-home"
    alias_root = overlay_home / ".claude-zai" / "projects"
    shutil.rmtree(real_config, ignore_errors=True)
    shutil.rmtree(overlay_home, ignore_errors=True)
    real_root.mkdir(parents=True)
    alias_root.parent.parent.mkdir(parents=True)
    alias_root.parent.symlink_to(real_config, target_is_directory=True)

    roots = nsp._dedupe_roots_by_real_path([
        (alias_root, "claude"),
        (real_root, "claude"),
    ])
    ok = roots == [(alias_root, "claude")]
    print(f"{OK if ok else FAIL} native roots dedupe symlinked real path (roots={roots})")
    return ok


def test_old_codex_prompt_timestamp_indexes_from_raw_session() -> bool:
    _setup_roots()
    codex = _SCRATCH / "codex-sessions"
    transcript = codex / "2025" / "10" / "20" / "rollout-2025-10-20T10-05-35-old.jsonl"
    _write_old_codex(transcript)

    result = idx.refresh_once()
    out = idx.run_readonly_sql(
        """
        SELECT path, tag, sid, cwd, element_kind, ts_utc
        FROM native_element_meta
        WHERE path = ?
        ORDER BY rowid
        """,
        (str(transcript),),
    )
    rows = out.get("rows") or []
    ok = (
        result["walked"] >= 1
        and len(rows) == 1
        and rows[0] == [
            str(transcript),
            "codex",
            "rollout-2025-10-20T10-05-35-old",
            "/old-codex",
            "user_prompt",
            "2025-10-20T07:05:55.926000Z",
        ]
    )
    print(f"{OK if ok else FAIL} old codex prompt timestamp indexes from raw session (rows={rows})")
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


def test_incomplete_full_scan_state_overrides_stale_covered_bit() -> bool:
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    _write_claude(claude / encode_cwd("/proj") / "a.jsonl", ["stalecoveredneedle here"])
    idx.refresh_once()
    conn = idx._writer_connection()
    idx._state_set(conn, "covered", "1")
    idx._set_full_scan_state(conn, {
        "roots": [{"path": str(claude), "tag": "claude"}],
        "root_index": 0,
        "stack": [{"path": str(claude), "tag": "claude", "cursor": ""}],
        "complete": False,
    })
    conn.commit()

    state = idx.quick_state()
    ok = state == {"schema_ok": True, "covered": False, "usable": False} and not idx.is_covered()
    print(f"{OK if ok else FAIL} incomplete full scan state overrides stale covered bit "
          f"(state={state})")
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
    original_path_stat = idx.Path.stat
    stat_calls = {"count": 0}
    def counted_stat(path_self, *args, **kwargs):
        stat_calls["count"] += 1
        return original_path_stat(path_self, *args, **kwargs)
    try:
        shutil.rmtree(claude, ignore_errors=True)
        shutil.rmtree(codex, ignore_errors=True)
        claude.mkdir(parents=True, exist_ok=True)
        codex.mkdir(parents=True, exist_ok=True)
        total_files = idx._FULL_REFRESH_FILE_BATCH * 3 + 3
        for i in range(total_files):
            _write_claude(
                claude / encode_cwd("/proj") / f"default-batch-{i}.jsonl",
                [f"defaultbatchneedle {i}"],
            )

        idx.Path.stat = counted_stat
        first = idx.refresh_once()
        stat_calls_after_first = stat_calls["count"]
        first_state = idx.quick_state()
        first_files = idx._readonly_connection().execute(
            "SELECT COUNT(*) FROM native_file_state"
        ).fetchone()[0]
        queue_after_first = idx._readonly_connection().execute(
            "SELECT COUNT(*) FROM native_full_scan_queue WHERE processed = 0"
        ).fetchone()[0]
        progress_blob = idx._readonly_connection().execute(
            "SELECT value FROM native_corpus_state WHERE key = 'full_scan_state_json'"
        ).fetchone()
        second = idx.refresh_once()
        third = idx.refresh_once()
        fourth = idx.refresh_once()
        final_state = idx.quick_state()
        rows = idx.search_rows(["defaultbatchneedle"], limit=idx._FULL_REFRESH_FILE_BATCH + 5)
    finally:
        idx.Path.stat = original_path_stat
        shutil.rmtree(claude, ignore_errors=True)
        shutil.rmtree(codex, ignore_errors=True)

    ok = (
        first["partial"] == 1
        and first_files == idx._FULL_REFRESH_FILE_BATCH
        and queue_after_first == 0
        and progress_blob is not None
        and first_state == {"schema_ok": True, "covered": False, "usable": False}
        and second["partial"] == 1
        and third["partial"] == 1
        and fourth["partial"] == 0
        and final_state == {"schema_ok": True, "covered": True, "usable": True}
        and len(rows) == idx._FULL_REFRESH_FILE_BATCH + 5
        and stat_calls_after_first < total_files
    )
    print(f"{OK if ok else FAIL} default cold build batch is bounded "
          f"(batch={idx._FULL_REFRESH_FILE_BATCH}, first={first}, "
          f"first_files={first_files}, second={second}, third={third}, fourth={fourth}, final={final_state}, "
          f"stat_calls_after_first={stat_calls_after_first}, queue_after_first={queue_after_first})")
    return ok


def test_queue_empty_incomplete_full_scan_resumes() -> bool:
    claude, codex = _setup_roots()
    shutil.rmtree(claude, ignore_errors=True)
    shutil.rmtree(codex, ignore_errors=True)
    claude.mkdir(parents=True, exist_ok=True)
    codex.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        _write_claude(
            claude / encode_cwd("/proj") / f"incomplete-scan-{i}.jsonl",
            [f"incompletescanneedle {i}"],
        )

    original_batch = idx._FULL_REFRESH_FILE_BATCH
    original_discovery = idx._FULL_SCAN_DISCOVERY_BATCH
    idx._FULL_REFRESH_FILE_BATCH = 2
    idx._FULL_SCAN_DISCOVERY_BATCH = 2
    try:
        first = idx.refresh_once()
        queue_after_first = idx._readonly_connection().execute(
            "SELECT COUNT(*) FROM native_full_scan_queue WHERE processed = 0"
        ).fetchone()[0]
        scan_state_after_first = idx._readonly_connection().execute(
            "SELECT value FROM native_corpus_state WHERE key = 'full_scan_state_json'"
        ).fetchone()
        second = idx.refresh_once()
        third = idx.refresh_once()
        final_state = idx.quick_state()
        rows = idx.search_rows(["incompletescanneedle"], limit=10)
    finally:
        idx._FULL_REFRESH_FILE_BATCH = original_batch
        idx._FULL_SCAN_DISCOVERY_BATCH = original_discovery

    ok = (
        first["partial"] == 1
        and queue_after_first == 0
        and scan_state_after_first is not None
        and second["partial"] == 1
        and third["partial"] == 0
        and final_state == {"schema_ok": True, "covered": True, "usable": True}
        and len(rows) == 5
    )
    print(f"{OK if ok else FAIL} queue-empty incomplete full scan resumes "
          f"(first={first}, second={second}, third={third}, "
          f"queue_after_first={queue_after_first}, final={final_state})")
    return ok


def test_full_scan_completes_despite_live_touched_directory() -> bool:
    """Directory metadata changes must not reset positional resume."""
    claude, codex = _setup_roots()
    shutil.rmtree(claude, ignore_errors=True)
    shutil.rmtree(codex, ignore_errors=True)
    claude.mkdir(parents=True, exist_ok=True)
    codex.mkdir(parents=True, exist_ok=True)
    live_dir = claude / encode_cwd("/live-proj")
    for i in range(6):
        _write_claude(live_dir / f"live-scan-{i:02d}.jsonl", [f"livescanneedle {i}"])

    original_batch = idx._FULL_REFRESH_FILE_BATCH
    original_discovery = idx._FULL_SCAN_DISCOVERY_BATCH
    idx._FULL_REFRESH_FILE_BATCH = 2
    idx._FULL_SCAN_DISCOVERY_BATCH = 2
    original_refresh = idx.refresh_once

    def refresh_and_touch_dir(*args, **kwargs):
        result = original_refresh(*args, **kwargs)
        os.utime(live_dir, None)
        return result

    try:
        results = [refresh_and_touch_dir()]
        while results[-1]["partial"] == 1 and len(results) < 40:
            results.append(refresh_and_touch_dir())
        final_state = idx.quick_state()
        rows = idx.search_rows(["livescanneedle"], limit=20)
    finally:
        idx._FULL_REFRESH_FILE_BATCH = original_batch
        idx._FULL_SCAN_DISCOVERY_BATCH = original_discovery
        shutil.rmtree(claude, ignore_errors=True)
        shutil.rmtree(codex, ignore_errors=True)

    ok = (
        results[-1]["partial"] == 0
        and len(results) < 40
        and final_state["covered"] is True
        and len(rows) == 6
    )
    print(f"{OK if ok else FAIL} full scan completes despite live touched directory "
          f"(passes={len(results)}, last={results[-1]}, rows={len(rows)}, "
          f"final={final_state})")
    return ok


def test_incremental_full_scan_preserves_sibling_directories() -> bool:
    claude, codex = _setup_roots()
    shutil.rmtree(claude, ignore_errors=True)
    shutil.rmtree(codex, ignore_errors=True)
    claude.mkdir(parents=True, exist_ok=True)
    codex.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        _write_claude(
            claude / encode_cwd("/proj-a") / f"sibling-scan-a-{i}.jsonl",
            [f"siblingscanneedle a {i}"],
        )
    _write_claude(
        claude / encode_cwd("/proj-b") / "sibling-scan-b.jsonl",
        ["siblingscanneedle b"],
    )

    original_batch = idx._FULL_REFRESH_FILE_BATCH
    original_discovery = idx._FULL_SCAN_DISCOVERY_BATCH
    idx._FULL_REFRESH_FILE_BATCH = 2
    idx._FULL_SCAN_DISCOVERY_BATCH = 2
    try:
        first = idx.refresh_once()
        second = idx.refresh_once()
        third = idx.refresh_once()
        final_state = idx.quick_state()
        rows = idx.search_rows(["siblingscanneedle"], limit=10)
        proj_b_rows = idx.search_rows(["siblingscanneedle b"], limit=10)
    finally:
        idx._FULL_REFRESH_FILE_BATCH = original_batch
        idx._FULL_SCAN_DISCOVERY_BATCH = original_discovery
        shutil.rmtree(claude, ignore_errors=True)
        shutil.rmtree(codex, ignore_errors=True)

    ok = (
        first["partial"] == 1
        and second["partial"] == 1
        and third["partial"] == 0
        and final_state == {"schema_ok": True, "covered": True, "usable": True}
        and len(rows) == 4
        and len(proj_b_rows) == 1
    )
    print(f"{OK if ok else FAIL} incremental full scan preserves sibling directories "
          f"(first={first}, second={second}, third={third}, "
          f"rows={len(rows)}, proj_b_rows={len(proj_b_rows)}, final={final_state})")
    return ok


def _full_scan_stack() -> list[dict]:
    row = idx._readonly_connection().execute(
        "SELECT value FROM native_corpus_state WHERE key = 'full_scan_state_json'"
    ).fetchone()
    if not row:
        return []
    return json.loads(row[0]).get("stack") or []


def test_incremental_full_scan_keeps_one_parent_continuation() -> bool:
    claude, codex = _setup_roots()
    shutil.rmtree(claude, ignore_errors=True)
    shutil.rmtree(codex, ignore_errors=True)
    claude.mkdir(parents=True, exist_ok=True)
    codex.mkdir(parents=True, exist_ok=True)
    project_paths = [
        claude / encode_cwd(f"/sibling-{i}")
        for i in range(4)
    ]
    for i, project_path in enumerate(project_paths):
        _write_claude(project_path / f"one-parent-{i}.jsonl", [f"oneparentneedle {i}"])

    original_batch = idx._FULL_REFRESH_FILE_BATCH
    original_discovery = idx._FULL_SCAN_DISCOVERY_BATCH
    idx._FULL_REFRESH_FILE_BATCH = 1
    idx._FULL_SCAN_DISCOVERY_BATCH = 1
    try:
        first = idx.refresh_once()
        stack = _full_scan_stack()
        parent_count = sum(1 for item in stack if item.get("path") == str(claude))
        results = [first]
        while results[-1]["partial"] == 1 and len(results) < 12:
            results.append(idx.refresh_once())
        rows = idx.search_rows(["oneparentneedle"], limit=10)
        final_state = idx.quick_state()
    finally:
        idx._FULL_REFRESH_FILE_BATCH = original_batch
        idx._FULL_SCAN_DISCOVERY_BATCH = original_discovery
        shutil.rmtree(claude, ignore_errors=True)
        shutil.rmtree(codex, ignore_errors=True)

    ok = (
        first["partial"] == 1
        and parent_count <= 1
        and results[-1]["partial"] == 0
        and len(rows) == 4
        and final_state == {"schema_ok": True, "covered": True, "usable": True}
    )
    print(f"{OK if ok else FAIL} incremental full scan keeps one parent continuation "
          f"(first={first}, parent_count={parent_count}, passes={len(results)}, "
          f"rows={len(rows)}, final={final_state})")
    return ok


def test_corrupt_duplicate_full_scan_state_restarts() -> bool:
    claude, codex = _setup_roots()
    shutil.rmtree(claude, ignore_errors=True)
    shutil.rmtree(codex, ignore_errors=True)
    claude.mkdir(parents=True, exist_ok=True)
    codex.mkdir(parents=True, exist_ok=True)
    for i in range(2):
        _write_claude(claude / encode_cwd(f"/restart-{i}") / f"restart-{i}.jsonl", [f"restartneedle {i}"])

    original_batch = idx._FULL_REFRESH_FILE_BATCH
    original_discovery = idx._FULL_SCAN_DISCOVERY_BATCH
    idx._FULL_REFRESH_FILE_BATCH = 1
    idx._FULL_SCAN_DISCOVERY_BATCH = 1
    try:
        idx._ensure_schema(idx._writer_connection())
        duplicate = {"path": str(claude), "tag": "claude", "cursor": ""}
        idx._set_full_scan_state(idx._writer_connection(), {
            "roots": [{"path": str(claude), "tag": "claude"}],
            "root_index": 1,
            "stack": [duplicate, dict(duplicate)],
            "complete": False,
        })
        idx._writer_connection().commit()
        first = idx.refresh_once()
        stack = _full_scan_stack()
        duplicate_free = len({
            (item.get("path"), item.get("tag"), item.get("cursor"))
            for item in stack
        }) == len(stack)
        results = [first]
        while results[-1]["partial"] == 1 and len(results) < 10:
            results.append(idx.refresh_once())
        rows = idx.search_rows(["restartneedle"], limit=10)
        final_state = idx.quick_state()
    finally:
        idx._FULL_REFRESH_FILE_BATCH = original_batch
        idx._FULL_SCAN_DISCOVERY_BATCH = original_discovery
        shutil.rmtree(claude, ignore_errors=True)
        shutil.rmtree(codex, ignore_errors=True)

    ok = (
        first["full"] == 1
        and duplicate_free
        and results[-1]["partial"] == 0
        and len(rows) == 2
        and final_state == {"schema_ok": True, "covered": True, "usable": True}
    )
    print(f"{OK if ok else FAIL} corrupt duplicate full scan state restarts "
          f"(first={first}, duplicate_free={duplicate_free}, passes={len(results)}, "
          f"rows={len(rows)}, final={final_state})")
    return ok


def test_full_scan_entry_budget_yields_without_candidates() -> bool:
    claude, codex = _setup_roots()
    shutil.rmtree(claude, ignore_errors=True)
    shutil.rmtree(codex, ignore_errors=True)
    claude.mkdir(parents=True, exist_ok=True)
    codex.mkdir(parents=True, exist_ok=True)
    project_path = claude / encode_cwd("/entry-budget")
    project_path.mkdir(parents=True, exist_ok=True)
    for i in range(20):
        (project_path / f"ignored-{i:02d}.txt").write_text("skip\n", encoding="utf-8")
    _write_claude(project_path / "needle.jsonl", ["entrybudgetneedle"])

    original_entry_budget = idx._FULL_SCAN_ENTRY_BUDGET
    original_discovery = idx._FULL_SCAN_DISCOVERY_BATCH
    original_scandir = idx.os.scandir
    yielded_entries = {"count": 0}

    class CountingScandir:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            close = getattr(self.inner, "close", None)
            if close:
                close()
            return False

        def __iter__(self):
            return self

        def __next__(self):
            yielded_entries["count"] += 1
            return next(self.inner)

    def counted_scandir(path):
        return CountingScandir(original_scandir(path))

    idx._FULL_SCAN_ENTRY_BUDGET = 3
    idx._FULL_SCAN_DISCOVERY_BATCH = 128
    idx.os.scandir = counted_scandir
    try:
        first = idx.refresh_once()
        yielded_after_first = yielded_entries["count"]
        stack_after_first = _full_scan_stack()
        batch_after_first = idx._readonly_connection().execute(
            "SELECT value FROM native_corpus_state WHERE key = 'last_refresh_batch_size'"
        ).fetchone()[0]
        remaining_after_first = idx._readonly_connection().execute(
            "SELECT value FROM native_corpus_state WHERE key = 'last_refresh_remaining'"
        ).fetchone()[0]
        _write_claude(project_path / "aaa-inserted.jsonl", ["entrybudgetinserted"])
        results = [first]
        while results[-1]["partial"] == 1 and len(results) < 20:
            results.append(idx.refresh_once())
        rows = idx.search_rows(["entrybudgetneedle"], limit=10)
        inserted_rows = idx.search_rows(["entrybudgetinserted"], limit=10)
        final_state = idx.quick_state()
    finally:
        idx._FULL_SCAN_ENTRY_BUDGET = original_entry_budget
        idx._FULL_SCAN_DISCOVERY_BATCH = original_discovery
        idx.os.scandir = original_scandir
        shutil.rmtree(claude, ignore_errors=True)
        shutil.rmtree(codex, ignore_errors=True)

    ok = (
        first["partial"] == 1
        and batch_after_first == "0"
        and remaining_after_first == "1"
        # Bounded well under the 21-entry dir: budget=3 visited entries + the
        # one terminal StopIteration from fully draining the single-entry root
        # dir (the scanner now drains each dir in one scandir pass instead of
        # breaking + re-scanning per subdir).
        and yielded_after_first <= 4
        and stack_after_first
        and results[-1]["partial"] == 0
        and len(rows) == 1
        and len(inserted_rows) == 1
        and final_state == {"schema_ok": True, "covered": True, "usable": True}
    )
    print(f"{OK if ok else FAIL} full scan entry budget yields without candidates "
          f"(first={first}, stack={stack_after_first}, batch={batch_after_first}, "
          f"remaining={remaining_after_first}, yielded_first={yielded_after_first}, "
          f"passes={len(results)}, rows={len(rows)}, inserted={len(inserted_rows)}, "
          f"final={final_state})")
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
        meta_rows = idx._readonly_connection().execute(
            "SELECT COUNT(*) FROM native_element_meta"
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
        and meta_rows == 1
    )
    print(f"{OK if ok else FAIL} reindex deletes FTS rows by rowid not path scan "
          f"(result={result}, path_deletes={guarded.fts_path_deletes}, "
          f"rowid_deletes={guarded.fts_rowid_deletes}, rows={len(rows)}, "
          f"mapped={mapped_rows}, meta={meta_rows})")
    return ok


def test_metadata_projection_tracks_refresh_and_delete() -> bool:
    _setup_roots()
    claude = _SCRATCH / "claude-projects"
    first = claude / encode_cwd("/proj") / "meta-a.jsonl"
    second = claude / encode_cwd("/proj") / "meta-b.jsonl"
    _write_claude(first, ["metaneedle first"])
    _write_claude(second, ["metaneedle second"])
    idx.refresh_once()

    conn = idx._readonly_connection()
    before = conn.execute("SELECT COUNT(*) FROM native_element_fts").fetchone()[0]
    before_meta = conn.execute("SELECT COUNT(*) FROM native_element_meta").fetchone()[0]

    second.unlink()
    result = idx.refresh_once(full=True)
    conn = idx._readonly_connection()
    after = conn.execute("SELECT COUNT(*) FROM native_element_fts").fetchone()[0]
    after_meta = conn.execute("SELECT COUNT(*) FROM native_element_meta").fetchone()[0]
    deleted_meta = conn.execute(
        "SELECT COUNT(*) FROM native_element_meta WHERE path = ?",
        (str(second),),
    ).fetchone()[0]

    ok = (
        before == 2
        and before_meta == 2
        and result["touched"] >= 1
        and after == 1
        and after_meta == 1
        and deleted_meta == 0
    )
    print(f"{OK if ok else FAIL} metadata projection tracks refresh and delete "
          f"(before={before}/{before_meta}, after={after}/{after_meta}, "
          f"deleted_meta={deleted_meta}, result={result})")
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


def test_worker_short_throttles_partial_covered_refresh() -> bool:
    _setup_roots()
    calls = {"refresh": 0, "wait": [], "covered": 0}

    original_refresh_once = idx.refresh_once
    original_is_covered = idx.is_covered
    original_wait = idx._stop.wait
    try:
        def fake_refresh_once(*, full=None):
            calls["refresh"] += 1
            return {"walked": 128, "touched": 0, "locked": 0, "full": 1, "partial": 1}

        def fake_is_covered():
            calls["covered"] += 1
            return True

        def fake_wait(timeout=None):
            calls["wait"].append(timeout)
            idx._stop.set()
            return True

        idx.refresh_once = fake_refresh_once
        idx.is_covered = fake_is_covered
        idx._stop.wait = fake_wait
        idx._worker_main()
    finally:
        idx.refresh_once = original_refresh_once
        idx.is_covered = original_is_covered
        idx._stop.wait = original_wait
        idx._stop.clear()

    ok = calls == {"refresh": 1, "wait": [0.2], "covered": 1}
    print(f"{OK if ok else FAIL} worker short-throttles partial covered refresh "
          f"(calls={calls})")
    return ok


def main_run() -> int:
    tests = [
        test_indexes_corpus_and_drops_tool_result,
        test_indexes_pi_sessions,
        test_old_schema_cache_rebuilds,
        test_timestamp_utc_orders_offsets_chronologically,
        test_provider_roots_ignore_spoofed_home,
        test_native_roots_dedupes_symlinked_real_path,
        test_old_codex_prompt_timestamp_indexes_from_raw_session,
        test_match_paths_cwd_filter_and_cap,
        test_freshness_reindexes_changed_files,
        test_covered_refresh_does_not_full_walk,
        test_forced_full_reconcile_discovers_external_files,
        test_restart_covered_worker_does_not_immediately_full_walk,
        test_not_usable_until_covered,
        test_incomplete_full_scan_state_overrides_stale_covered_bit,
        test_cold_full_build_commits_partial_progress_and_resumes,
        test_default_cold_build_batch_is_bounded,
        test_queue_empty_incomplete_full_scan_resumes,
        test_full_scan_completes_despite_live_touched_directory,
        test_incremental_full_scan_preserves_sibling_directories,
        test_incremental_full_scan_keeps_one_parent_continuation,
        test_corrupt_duplicate_full_scan_state_restarts,
        test_full_scan_entry_budget_yields_without_candidates,
        test_partial_resume_does_not_scan_entire_queue,
        test_partial_full_build_reconciles_deletes_before_final_covered,
        test_covered_partial_full_queue_resumes_by_default,
        test_refresh_persists_batch_and_file_timings,
        test_reindex_deletes_fts_rows_by_rowid_not_path_scan,
        test_metadata_projection_tracks_refresh_and_delete,
        test_full_walk_ignores_non_transcript_run_jsonl,
        test_steady_refresh_purges_preexisting_non_transcript_run_jsonl,
        test_steady_refresh_is_bounded_over_indexed_paths,
        test_refresh_stamps_freshness_after_index_work,
        test_broad_match_signals_fallback,
        test_wait_fresh_serves_delta_instead_of_falling_back,
        test_request_refresh_persists_cross_process_marker,
        test_refresh_reports_locked_instead_of_colliding,
        test_ensure_started_spawns_external_worker_process,
        test_worker_short_throttles_partial_covered_refresh,
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
