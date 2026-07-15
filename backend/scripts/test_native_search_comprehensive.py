"""Comprehensive ~100-test suite for the native-transcript search/index stack.

Covers three modules end to end:

  * ``native_session_miner`` — per-format parsers + element extractors
    (Claude / Codex / Gemini), discovery, cwd token decode, run-dir index.
  * ``native_session_prompt_search`` — query tokenization, whole-word match,
    Categorizer (every tool class), dedup, ranking, cwd filter, rg filter,
    generalized grep, public facades.
  * ``native_transcript_index`` — FTS5 lean extraction (tool_result dropped),
    mtime+size freshness, covered/is_usable gates, match_paths cwd filter,
    broad-match cap, request_refresh + wait_fresh delta path.

Isolation: every test runs against ``_test_home.isolate()`` (a temp
``BETTER_AGENT_HOME``) plus monkeypatched native roots pointing at temp dirs.
No test scans or writes the real home (``~/.better-claude``, ``~/.claude``,
``~/.codex``, ``~/.gemini``).

Run with:
    cd backend && .venv/bin/python scripts/test_native_search_comprehensive.py
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

# Isolate ~/.better-claude BEFORE any backend import (per CLAUDE.md).
import _test_home  # noqa: E402
_TMP_HOME = _test_home.isolate("bc-test-native-search-comprehensive-")

import native_session_miner as M  # noqa: E402
import native_session_prompt_search as nsp  # noqa: E402
import native_transcript_index as idx  # noqa: E402
from native_elements import (  # noqa: E402
    NativeCandidate, NativeElement, _claude_elements, _codex_elements,
    _codex_first_cwd, _decode_cwd_token, _gemini_elements,
)
from native_session_prompt_search import (  # noqa: E402
    Categorizer, ElementCategory, _query_tokens, _token_patterns,
)
from paths import encode_cwd  # noqa: E402

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

_SCRATCH = Path(_TMP_HOME) / "scratch"
_SCRATCH.mkdir(parents=True, exist_ok=True)
_EMPTY_DEFAULT = _SCRATCH / "empty-default"
_EMPTY_DEFAULT.mkdir(parents=True, exist_ok=True)
_seq = 0


def _install_empty_default_roots() -> None:
    """Belt-and-suspenders (#6): point the four miner root fns AND
    ``nsp._native_roots`` at a temp EMPTY dir so no test can EVER read the real
    home even if it forgets explicit isolation. Individual tests override these
    via ``_isolate_native_roots`` / ``_idx_setup_roots`` / ``_patch_nsp_roots``;
    ``_restore_*`` returns to THIS empty default, never the real home."""
    M._claude_projects_roots = lambda: []
    M._codex_sessions_root = lambda: _EMPTY_DEFAULT / "no-codex"
    M._gemini_chats_root = lambda: _EMPTY_DEFAULT / "no-gemini"
    M._runs_root = lambda: _EMPTY_DEFAULT / "no-runs"
    nsp._native_roots = lambda: []


_install_empty_default_roots()


# ─── fixture writers ───────────────────────────────────────────────────────

def _next_seq() -> int:
    global _seq
    _seq += 1
    return _seq


def _w(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8")
    return path


def _claude_user(text: str, uid: str = "u", ts: str = "2024-01-01T00:00:00Z") -> dict:
    return {"type": "user", "uuid": uid, "timestamp": ts,
            "message": {"role": "user", "content": text}}


def _claude_assistant_blocks(blocks: list[dict], uid: str = "a",
                             ts: str = "2024-01-01T00:00:01Z") -> dict:
    return {"type": "assistant", "uuid": uid, "timestamp": ts,
            "message": {"role": "assistant", "content": blocks}}


def _claude_text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _claude_thinking_block(text: str) -> dict:
    return {"type": "thinking", "thinking": text}


def _claude_tool_use(name: str, inp: dict, tid: str = "t1") -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _claude_tool_result(tid: str, content: str, uid: str = "u2") -> dict:
    return {"type": "user", "uuid": uid, "timestamp": "2024-01-01T00:00:02Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": content}]}}


def _write_claude(prompts: list[tuple[str, str]]) -> Path:
    """Claude-shaped user-prompt transcript (text, ts)."""
    n = _next_seq()
    path = _SCRATCH / f"claude_{n}.jsonl"
    return _w(path, [_claude_user(t, f"u{i}", ts) for i, (t, ts) in enumerate(prompts)])


def _write_claude_raw(records: list[dict]) -> Path:
    n = _next_seq()
    return _w(_SCRATCH / f"claude_raw_{n}.jsonl", records)


def _write_codex(records: list[dict]) -> Path:
    n = _next_seq()
    return _w(_SCRATCH / f"codex_{n}.jsonl", records)


def _write_gemini(records: list[dict]) -> Path:
    n = _next_seq()
    return _w(_SCRATCH / f"gemini_{n}.jsonl", records)


def _candidate(sid: str, cwd: str, transcript: Path, fmt: str = "claude") -> NativeCandidate:
    return NativeCandidate(key=f"{fmt}:{sid}", sid=sid, cwd=cwd, data={},
                           transcript=transcript, mtime=0.0, format=fmt)


def _candidate_from_prompts(sid: str, cwd: str, prompts: list[tuple[str, str]]) -> NativeCandidate:
    return _candidate(sid, cwd, _write_claude(prompts))


# ─── search monkeypatching helpers ─────────────────────────────────────────

_ORIG_MATCHED = nsp._matched_candidates
_ORIG_RG = nsp._rg_filter


def _patch_candidates(items: list[object]) -> None:
    def fake(tokens, allowed):
        # Route through the real _cwd_ok so the encode_cwd cwd-equivalence the
        # production path relies on is exercised, not bypassed by a simpler
        # ``cwd in allowed`` that would silently diverge.
        allowed_encoded = {encode_cwd(c) for c in allowed}
        return [c for c in items if nsp._cwd_ok(getattr(c, "cwd", ""), allowed, allowed_encoded)]
    nsp._matched_candidates = fake


def _reset_candidates() -> None:
    nsp._matched_candidates = _ORIG_MATCHED


def _disable_rg() -> None:
    nsp._rg_filter = lambda tokens: None


def _restore_rg() -> None:
    nsp._rg_filter = _ORIG_RG


def _isolate_native_roots(*, claude: list[Path] | None = None, codex: Path | None = None,
                          gemini: Path | None = None, runs: Path | None = None):
    """Patch BOTH the miner roots (iter_all_native_candidates) AND nsp._native_roots
    (rg_filter / index stat-walk) so rg and the index see the SAME temp dirs the
    search discovery does. Returns a token for _restore_native_roots."""
    orig = (M._claude_projects_roots, M._codex_sessions_root, M._gemini_chats_root,
            M._runs_root, nsp._native_roots)
    M._claude_projects_roots = lambda: list(claude or [])
    M._codex_sessions_root = lambda: codex or _SCRATCH / "no-codex"
    M._gemini_chats_root = lambda: gemini or _SCRATCH / "no-gemini"
    M._runs_root = lambda: runs or _SCRATCH / "no-runs"
    pairs: list[tuple[Path, str]] = []
    for c in (claude or []):
        pairs.append((c, "claude"))
    if codex and codex.exists():
        pairs.append((codex, "codex"))
    if gemini and gemini.exists():
        pairs.append((gemini, "gemini"))
    if runs and runs.exists():
        pairs.append((runs, "runs"))
    nsp._native_roots = lambda: pairs
    return orig


def _restore_native_roots(orig) -> None:
    (M._claude_projects_roots, M._codex_sessions_root, M._gemini_chats_root,
     M._runs_root, nsp._native_roots) = orig


_ORIG_NSP_ROOTS = None
_ORIG_MINER_ROOTS = None


def _patch_nsp_roots(claude: list[Path] | None = None, codex: Path | None = None,
                     gemini: Path | None = None, runs: Path | None = None):
    """Patch ``nsp._native_roots`` (used by the index stat-walk) in parallel with
    the miner roots so the index fast path walks the SAME temp dirs the search
    discovery does. Returns a token for ``_restore_nsp_roots``."""
    global _ORIG_NSP_ROOTS
    if _ORIG_NSP_ROOTS is None:
        _ORIG_NSP_ROOTS = nsp._native_roots
    pairs: list[tuple[Path, str]] = []
    for c in (claude or []):
        pairs.append((c, "claude"))
    if codex and codex.exists():
        pairs.append((codex, "codex"))
    if gemini and gemini.exists():
        pairs.append((gemini, "gemini"))
    if runs and runs.exists():
        pairs.append((runs, "runs"))
    nsp._native_roots = lambda: pairs


def _restore_nsp_roots() -> None:
    global _ORIG_NSP_ROOTS
    if _ORIG_NSP_ROOTS is not None:
        nsp._native_roots = _ORIG_NSP_ROOTS
        _ORIG_NSP_ROOTS = None


_IDX_CLAUDE: Path | None = None


def _idx_setup_roots(claude: Path | None = None):
    """Point BOTH the index stat-walk (``nsp._native_roots``) and the miner roots
    at temp dirs so no idx test can ever read the real home. Returns a restore
    token — every caller MUST call ``_restore_idx_roots(token)`` in its finally.
    Uses a per-call UNIQUE claude dir by default so a test that writes many files
    (e.g. scan-limit) cannot pollute a later empty-roots test. The active claude
    dir is stashed in ``_IDX_CLAUDE`` for callers to write fixtures into."""
    global _ORIG_NSP_ROOTS, _ORIG_MINER_ROOTS, _IDX_CLAUDE
    c = claude or (_SCRATCH / f"idx-claude-{_next_seq()}")
    c.mkdir(parents=True, exist_ok=True)
    co = _SCRATCH / f"idx-codex-{_next_seq()}"
    co.mkdir(parents=True, exist_ok=True)
    if _ORIG_NSP_ROOTS is None:
        _ORIG_NSP_ROOTS = nsp._native_roots
    if _ORIG_MINER_ROOTS is None:
        _ORIG_MINER_ROOTS = (M._claude_projects_roots, M._codex_sessions_root,
                             M._gemini_chats_root, M._runs_root)
    _IDX_CLAUDE = c
    nsp._native_roots = lambda: [(c, "claude"), (co, "codex")]
    M._claude_projects_roots = lambda: [c]
    M._codex_sessions_root = lambda: co
    M._gemini_chats_root = lambda: _SCRATCH / "idx-no-gemini"
    M._runs_root = lambda: _SCRATCH / "idx-no-runs"
    _reset_index()
    return _ORIG_NSP_ROOTS


def _restore_idx_roots(token) -> None:
    """Restore ``nsp._native_roots`` and the miner roots to their pre-test state."""
    global _ORIG_NSP_ROOTS, _ORIG_MINER_ROOTS
    if _ORIG_NSP_ROOTS is not None:
        nsp._native_roots = _ORIG_NSP_ROOTS
        _ORIG_NSP_ROOTS = None
    if _ORIG_MINER_ROOTS is not None:
        (M._claude_projects_roots, M._codex_sessions_root,
         M._gemini_chats_root, M._runs_root) = _ORIG_MINER_ROOTS
        _ORIG_MINER_ROOTS = None


def _reset_index() -> None:
    idx.reset_for_test()


# ===========================================================================
# native_session_miner — parsers + extractors
# ===========================================================================

def test_claude_parse_user_prompt() -> bool:
    visit = _candidate_from_prompts("s1", "/p", [("hello world", "2024-01-01")]).parse()
    msgs = visit.messages
    ok = len(msgs) == 1 and msgs[0]["role"] == "user" and msgs[0]["content"] == "hello world"
    print(f"{OK if ok else FAIL} claude parse keeps user prompt (got {msgs})")
    return ok


def test_claude_parse_drops_tool_result_user_turn() -> bool:
    """A user turn whose only content is a tool_result is NOT a typed prompt."""
    t = _write_claude_raw([
        _claude_assistant_blocks([_claude_tool_use("Bash", {"command": "ls"})]),
        _claude_tool_result("t1", "file.txt"),
    ])
    visit = _candidate("s1", "/p", t).parse()
    ok = visit.messages == []
    print(f"{OK if ok else FAIL} claude parse drops tool-result-only user turn (got {visit.messages})")
    return ok


def test_claude_parse_drops_sidechain() -> bool:
    t = _write_claude_raw([
        {"type": "user", "uuid": "s1", "isSidechain": True,
         "message": {"role": "user", "content": "sidechain noise"}},
        _claude_user("real prompt", "u2"),
    ])
    visit = _candidate("s1", "/p", t).parse()
    texts = [m["content"] for m in visit.messages]
    ok = texts == ["real prompt"]
    print(f"{OK if ok else FAIL} claude parse drops sidechain (got {texts})")
    return ok


def test_claude_parse_drops_meta() -> bool:
    t = _write_claude_raw([
        {"type": "user", "uuid": "m1", "isMeta": True,
         "message": {"role": "user", "content": "meta noise"}},
        _claude_user("real prompt", "u2"),
    ])
    visit = _candidate("s1", "/p", t).parse()
    texts = [m["content"] for m in visit.messages]
    ok = texts == ["real prompt"]
    print(f"{OK if ok else FAIL} claude parse drops meta lines (got {texts})")
    return ok


def test_claude_parse_drops_command_tags() -> bool:
    t = _write_claude_raw([
        _claude_user("<command-name>/help</command-name>", "u1"),
        _claude_user("<local-command-stdout>output</local-command-stdout>", "u2"),
        _claude_user("<system-reminder>be careful</system-reminder>", "u3"),
        _claude_user("real prompt", "u4"),
    ])
    visit = _candidate("s1", "/p", t).parse()
    texts = [m["content"] for m in visit.messages]
    ok = texts == ["real prompt"]
    print(f"{OK if ok else FAIL} claude parse drops CLI command tags (got {texts})")
    return ok


def test_claude_parse_drops_caveat() -> bool:
    t = _write_claude_raw([_claude_user("Caveat: some warning", "u1")])
    visit = _candidate("s1", "/p", t).parse()
    ok = visit.messages == []
    print(f"{OK if ok else FAIL} claude parse drops 'Caveat:' line (got {visit.messages})")
    return ok


def test_claude_parse_assistant_text() -> bool:
    t = _write_claude_raw([
        _claude_assistant_blocks([_claude_text_block("sure thing")]),
    ])
    visit = _candidate("s1", "/p", t).parse()
    ok = len(visit.messages) == 1 and visit.messages[0]["content"] == "sure thing"
    print(f"{OK if ok else FAIL} claude parse keeps assistant text (got {visit.messages})")
    return ok


def test_claude_parse_keeps_assistant_with_edit_tool_only() -> bool:
    """An assistant turn with no text but an Edit tool_use is still kept."""
    t = _write_claude_raw([
        _claude_assistant_blocks([_claude_tool_use("Edit", {"file": "a.py"})]),
    ])
    visit = _candidate("s1", "/p", t).parse()
    ok = len(visit.messages) == 1
    print(f"{OK if ok else FAIL} claude parse keeps edit-only assistant turn (got {len(visit.messages)})")
    return ok


def test_claude_parse_drops_assistant_no_text_no_edit() -> bool:
    t = _write_claude_raw([
        _claude_assistant_blocks([_claude_tool_use("Read", {"file": "a.py"})]),
    ])
    visit = _candidate("s1", "/p", t).parse()
    ok = visit.messages == []
    print(f"{OK if ok else FAIL} claude parse drops no-text no-edit assistant (got {visit.messages})")
    return ok


def test_claude_parse_malformed_json_line_skipped() -> bool:
    path = _SCRATCH / f"bad_{_next_seq()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "{not valid json\n"
        + json.dumps(_claude_user("good prompt", "u1")) + "\n",
        encoding="utf-8",
    )
    visit = _candidate("s1", "/p", path).parse()
    texts = [m["content"] for m in visit.messages]
    ok = texts == ["good prompt"]
    print(f"{OK if ok else FAIL} claude parse skips malformed json line (got {texts})")
    return ok


def test_claude_parse_empty_file() -> bool:
    path = _SCRATCH / f"empty_{_next_seq()}.jsonl"
    path.write_text("", encoding="utf-8")
    visit = _candidate("s1", "/p", path).parse()
    ok = visit.messages == [] and visit.events_by_msg_id == {}
    print(f"{OK if ok else FAIL} claude parse empty file -> empty (got {visit.messages})")
    return ok


def test_claude_parse_missing_file_returns_none() -> bool:
    visit = _candidate("s1", "/p", _SCRATCH / "does_not_exist.jsonl").parse()
    ok = visit is None
    print(f"{OK if ok else FAIL} claude parse missing file -> None (got {visit})")
    return ok


def test_claude_parse_user_text_in_list_blocks() -> bool:
    """A user turn whose content is a list of text blocks (no tool_result) is a prompt."""
    t = _write_claude_raw([
        {"type": "user", "uuid": "u1", "timestamp": "2024-01-01",
         "message": {"role": "user", "content": [
             {"type": "text", "text": "multi"}, {"type": "text", "text": "block"}]}},
    ])
    visit = _candidate("s1", "/p", t).parse()
    ok = len(visit.messages) == 1 and visit.messages[0]["content"] == "multi\nblock"
    print(f"{OK if ok else FAIL} claude parse joins user list text blocks (got {visit.messages})")
    return ok


def test_claude_events_by_msg_id_has_agent_message() -> bool:
    t = _write_claude_raw([
        _claude_assistant_blocks([_claude_text_block("hi")], uid="a1"),
    ])
    visit = _candidate("s1", "/p", t).parse()
    ev = visit.events_by_msg_id.get("a1")
    ok = ev and ev[0]["type"] == "agent_message"
    print(f"{OK if ok else FAIL} claude parse emits agent_message event (got {ev})")
    return ok


# ─── codex parser ──────────────────────────────────────────────────────────

def test_codex_parse_user_message() -> bool:
    t = _write_codex([
        {"type": "session_meta", "timestamp": "t0",
         "payload": {"id": "z", "cwd": "/zapp", "source": "cli"}},
        {"type": "response_item", "timestamp": "t1",
         "payload": {"type": "message", "role": "user", "id": "u1",
                     "content": [{"type": "input_text", "text": "codex prompt"}]}},
    ])
    visit = _candidate("s1", "/p", t, fmt="codex").parse()
    msgs = visit.messages
    ok = len(msgs) == 1 and msgs[0]["content"] == "codex prompt"
    print(f"{OK if ok else FAIL} codex parse keeps user message (got {msgs})")
    return ok


def test_codex_parse_drops_environment_context() -> bool:
    t = _write_codex([
        {"type": "response_item", "timestamp": "t1",
         "payload": {"type": "message", "role": "user", "id": "u1",
                     "content": [{"type": "input_text",
                                  "text": "<environment_context>\n  <cwd>/x</cwd>"}]}},
        {"type": "response_item", "timestamp": "t2",
         "payload": {"type": "message", "role": "user", "id": "u2",
                     "content": [{"type": "input_text", "text": "real codex prompt"}]}},
    ])
    visit = _candidate("s1", "/p", t, fmt="codex").parse()
    texts = [m["content"] for m in visit.messages]
    ok = texts == ["real codex prompt"]
    print(f"{OK if ok else FAIL} codex parse drops environment_context (got {texts})")
    return ok


def test_codex_parse_drops_user_instructions() -> bool:
    t = _write_codex([
        {"type": "response_item", "timestamp": "t1",
         "payload": {"type": "message", "role": "user", "id": "u1",
                     "content": [{"type": "input_text",
                                  "text": "<user_instructions>do stuff"}]}},
    ])
    visit = _candidate("s1", "/p", t, fmt="codex").parse()
    ok = visit.messages == []
    print(f"{OK if ok else FAIL} codex parse drops user_instructions (got {visit.messages})")
    return ok


def test_codex_parse_drops_assistant_in_parse() -> bool:
    """_codex_messages only collects user prompts (parse path); assistant is ignored."""
    t = _write_codex([
        {"type": "response_item", "timestamp": "t1",
         "payload": {"type": "message", "role": "assistant", "id": "a1",
                     "content": [{"type": "output_text", "text": "reply"}]}},
        {"type": "response_item", "timestamp": "t2",
         "payload": {"type": "message", "role": "user", "id": "u1",
                     "content": [{"type": "input_text", "text": "prompt"}]}},
    ])
    visit = _candidate("s1", "/p", t, fmt="codex").parse()
    ok = len(visit.messages) == 1 and visit.messages[0]["content"] == "prompt"
    print(f"{OK if ok else FAIL} codex parse (msgs) only keeps user (got {visit.messages})")
    return ok


def test_codex_parse_string_content() -> bool:
    t = _write_codex([
        {"type": "response_item", "timestamp": "t1",
         "payload": {"type": "message", "role": "user", "id": "u1", "content": "string prompt"}},
    ])
    visit = _candidate("s1", "/p", t, fmt="codex").parse()
    ok = len(visit.messages) == 1 and visit.messages[0]["content"] == "string prompt"
    print(f"{OK if ok else FAIL} codex parse accepts string content (got {visit.messages})")
    return ok


def test_codex_first_cwd_extracts() -> bool:
    t = _SCRATCH / f"cwd_{_next_seq()}.jsonl"
    _w(t, [{"type": "session_meta", "payload": {"cwd": "/my/cwd"}}])
    ok = _codex_first_cwd(t) == "/my/cwd"
    print(f"{OK if ok else FAIL} codex_first_cwd extracts cwd (got {_codex_first_cwd(t)})")
    return ok


def test_codex_first_cwd_missing() -> bool:
    t = _SCRATCH / f"cwd_{_next_seq()}.jsonl"
    _w(t, [{"type": "something_else", "payload": {}}])
    ok = _codex_first_cwd(t) == ""
    print(f"{OK if ok else FAIL} codex_first_cwd missing -> empty (got {_codex_first_cwd(t)!r})")
    return ok


def test_codex_first_cwd_invalid_json() -> bool:
    """A codex file whose first line is not valid JSON yields "" (the _missing
    sibling covers a valid line without cwd; this covers the JSON-decode path)."""
    t = _SCRATCH / f"cwd_{_next_seq()}.jsonl"
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text("{not valid json\n", encoding="utf-8")
    ok = _codex_first_cwd(t) == ""
    print(f"{OK if ok else FAIL} codex_first_cwd invalid-json -> empty (got {_codex_first_cwd(t)!r})")
    return ok


# ─── gemini parser ─────────────────────────────────────────────────────────

def test_gemini_parse_user_turn() -> bool:
    t = _write_gemini([
        {"sessionId": "g", "kind": "main"},
        {"id": "u1", "timestamp": "t1", "type": "user",
         "content": [{"text": "gemini prompt"}]},
    ])
    visit = _candidate("s1", "/p", t, fmt="gemini").parse()
    ok = len(visit.messages) == 1 and visit.messages[0]["role"] == "user"
    print(f"{OK if ok else FAIL} gemini parse keeps user turn (got {visit.messages})")
    return ok


def test_gemini_parse_gemini_turn_is_assistant() -> bool:
    t = _write_gemini([
        {"id": "g1", "timestamp": "t1", "type": "gemini",
         "content": [{"text": "gemini reply"}]},
    ])
    visit = _candidate("s1", "/p", t, fmt="gemini").parse()
    ok = len(visit.messages) == 1 and visit.messages[0]["role"] == "assistant"
    print(f"{OK if ok else FAIL} gemini parse maps gemini turn -> assistant (got {visit.messages})")
    return ok


def test_gemini_parse_drops_metadata_line() -> bool:
    t = _write_gemini([
        {"sessionId": "g", "kind": "main"},
        {"id": "u1", "timestamp": "t1", "type": "user", "content": [{"text": "prompt"}]},
    ])
    visit = _candidate("s1", "/p", t, fmt="gemini").parse()
    ok = len(visit.messages) == 1
    print(f"{OK if ok else FAIL} gemini parse drops metadata line (got {len(visit.messages)})")
    return ok


def test_gemini_parse_drops_set_update_line() -> bool:
    t = _write_gemini([
        {"id": "u1", "timestamp": "t1", "type": "user", "content": [{"text": "prompt"}]},
        {"$set": {"foo": "bar"}},
        {"id": "u2", "timestamp": "t2", "type": "user", "content": [{"text": "second"}]},
    ])
    visit = _candidate("s1", "/p", t, fmt="gemini").parse()
    ok = len(visit.messages) == 2
    print(f"{OK if ok else FAIL} gemini parse drops $set update line (got {len(visit.messages)})")
    return ok


def test_gemini_parse_string_content() -> bool:
    t = _write_gemini([
        {"id": "u1", "timestamp": "t1", "type": "user", "content": "string prompt"},
    ])
    visit = _candidate("s1", "/p", t, fmt="gemini").parse()
    ok = len(visit.messages) == 1 and visit.messages[0]["content"] == "string prompt"
    print(f"{OK if ok else FAIL} gemini parse accepts string content (got {visit.messages})")
    return ok


def test_gemini_parse_empty_content_dropped() -> bool:
    t = _write_gemini([
        {"id": "u1", "timestamp": "t1", "type": "user", "content": [{"text": "   "}]},
    ])
    visit = _candidate("s1", "/p", t, fmt="gemini").parse()
    ok = visit.messages == []
    print(f"{OK if ok else FAIL} gemini parse drops empty content (got {visit.messages})")
    return ok


# ─── claude element extractor ──────────────────────────────────────────────

def test_claude_elements_user_prompt() -> bool:
    t = _write_claude_raw([_claude_user("hello prompt", "u1")])
    els = _claude_elements(t)
    ok = len(els) == 1 and els[0].kind == "user_prompt" and els[0].text == "hello prompt"
    print(f"{OK if ok else FAIL} claude_elements user_prompt (got {[(e.kind,e.text) for e in els]})")
    return ok


def test_claude_elements_command_tag() -> bool:
    t = _write_claude_raw([_claude_user("<command-name>/run</command-name>", "u1")])
    els = _claude_elements(t)
    ok = len(els) == 1 and els[0].kind == "command"
    print(f"{OK if ok else FAIL} claude_elements command tag -> command kind (got {[e.kind for e in els]})")
    return ok


def test_claude_elements_bash_input_tag() -> bool:
    t = _write_claude_raw([_claude_user("<bash-input>ls -la</bash-input>", "u1")])
    els = _claude_elements(t)
    ok = len(els) == 1 and els[0].kind == "command"
    print(f"{OK if ok else FAIL} claude_elements bash-input -> command kind (got {[e.kind for e in els]})")
    return ok


def test_claude_elements_system_reminder_is_meta() -> bool:
    t = _write_claude_raw([_claude_user("<system-reminder>careful</system-reminder>", "u1")])
    els = _claude_elements(t)
    ok = len(els) == 1 and els[0].kind == "meta"
    print(f"{OK if ok else FAIL} claude_elements system-reminder -> meta (got {[e.kind for e in els]})")
    return ok


def test_claude_elements_assistant_text() -> bool:
    t = _write_claude_raw([_claude_assistant_blocks([_claude_text_block("reply text")])])
    els = _claude_elements(t)
    ok = len(els) == 1 and els[0].kind == "assistant_text"
    print(f"{OK if ok else FAIL} claude_elements assistant_text (got {[(e.kind,e.text) for e in els]})")
    return ok


def test_claude_elements_reasoning() -> bool:
    t = _write_claude_raw([_claude_assistant_blocks([_claude_thinking_block("let me think")])])
    els = _claude_elements(t)
    ok = len(els) == 1 and els[0].kind == "reasoning" and els[0].text == "let me think"
    print(f"{OK if ok else FAIL} claude_elements reasoning (got {[(e.kind,e.text) for e in els]})")
    return ok


def test_claude_elements_tool_call() -> bool:
    t = _write_claude_raw([
        _claude_assistant_blocks([_claude_tool_use("Bash", {"command": "make"}, "t1")], uid="a1"),
    ])
    els = _claude_elements(t)
    ok = (len(els) == 1 and els[0].kind == "tool_call" and els[0].tool_name == "Bash"
          and "make" in els[0].text)
    print(f"{OK if ok else FAIL} claude_elements tool_call (got {[(e.kind,e.tool_name,e.text) for e in els]})")
    return ok


def test_claude_elements_tool_result() -> bool:
    t = _write_claude_raw([_claude_tool_result("t1", "output here")])
    els = _claude_elements(t)
    ok = len(els) == 1 and els[0].kind == "tool_result" and els[0].text == "output here"
    print(f"{OK if ok else FAIL} claude_elements tool_result (got {[(e.kind,e.text) for e in els]})")
    return ok


def test_claude_elements_drops_sidechain_and_meta() -> bool:
    t = _write_claude_raw([
        {"type": "user", "uuid": "s1", "isSidechain": True,
         "message": {"role": "user", "content": "sidechain"}},
        {"type": "user", "uuid": "m1", "isMeta": True,
         "message": {"role": "user", "content": "meta"}},
        _claude_user("real", "u1"),
    ])
    els = _claude_elements(t)
    ok = len(els) == 1 and els[0].text == "real"
    print(f"{OK if ok else FAIL} claude_elements drops sidechain+meta (got {[e.text for e in els]})")
    return ok


def test_claude_elements_user_string_content() -> bool:
    t = _write_claude_raw([_claude_user("string body", "u1")])
    els = _claude_elements(t)
    ok = len(els) == 1 and els[0].kind == "user_prompt"
    print(f"{OK if ok else FAIL} claude_elements string user content (got {[e.kind for e in els]})")
    return ok


def test_claude_elements_malformed_line_skipped() -> bool:
    path = _SCRATCH / f"elbad_{_next_seq()}.jsonl"
    path.write_text("{bad json\n" + json.dumps(_claude_user("good", "u1")) + "\n", encoding="utf-8")
    els = _claude_elements(path)
    ok = len(els) == 1 and els[0].text == "good"
    print(f"{OK if ok else FAIL} claude_elements skips malformed line (got {[e.text for e in els]})")
    return ok


# ─── codex element extractor ───────────────────────────────────────────────

def test_codex_elements_user_prompt() -> bool:
    t = _write_codex([
        {"type": "response_item", "timestamp": "t1",
         "payload": {"type": "message", "role": "user", "id": "u1",
                     "content": [{"type": "input_text", "text": "codex prompt"}]}},
    ])
    els = _codex_elements(t)
    ok = len(els) == 1 and els[0].kind == "user_prompt" and els[0].text == "codex prompt"
    print(f"{OK if ok else FAIL} codex_elements user_prompt (got {[(e.kind,e.text) for e in els]})")
    return ok


def test_codex_elements_environment_context_is_meta() -> bool:
    t = _write_codex([
        {"type": "response_item", "timestamp": "t1",
         "payload": {"type": "message", "role": "user", "id": "u1",
                     "content": [{"type": "input_text",
                                  "text": "<environment_context>x"}]}},
    ])
    els = _codex_elements(t)
    ok = len(els) == 1 and els[0].kind == "meta"
    print(f"{OK if ok else FAIL} codex_elements env-context -> meta (got {[e.kind for e in els]})")
    return ok


def test_codex_elements_assistant_text() -> bool:
    t = _write_codex([
        {"type": "response_item", "timestamp": "t1",
         "payload": {"type": "message", "role": "assistant", "id": "a1",
                     "content": [{"type": "output_text", "text": "reply"}]}},
    ])
    els = _codex_elements(t)
    ok = len(els) == 1 and els[0].kind == "assistant_text"
    print(f"{OK if ok else FAIL} codex_elements assistant_text (got {[(e.kind,e.text) for e in els]})")
    return ok


def test_codex_elements_reasoning() -> bool:
    t = _write_codex([
        {"type": "response_item", "timestamp": "t1",
         "payload": {"type": "agent_reasoning", "id": "r1",
                     "content": [{"text": "thinking"}]}},
    ])
    els = _codex_elements(t)
    ok = len(els) == 1 and els[0].kind == "reasoning"
    print(f"{OK if ok else FAIL} codex_elements reasoning (got {[(e.kind,e.text) for e in els]})")
    return ok


def test_codex_elements_function_call() -> bool:
    t = _write_codex([
        {"type": "response_item", "timestamp": "t1",
         "payload": {"type": "function_call", "id": "f1", "name": "shell",
                     "arguments": {"command": "ls"}}},
    ])
    els = _codex_elements(t)
    ok = len(els) == 1 and els[0].kind == "tool_call" and els[0].tool_name == "shell"
    print(f"{OK if ok else FAIL} codex_elements function_call (got {[(e.kind,e.tool_name) for e in els]})")
    return ok


def test_codex_elements_custom_tool_call() -> bool:
    t = _write_codex([
        {"type": "response_item", "timestamp": "t1",
         "payload": {"type": "custom_tool_call", "id": "c1", "name": "edit",
                     "input": {"file": "a"}}},
    ])
    els = _codex_elements(t)
    ok = len(els) == 1 and els[0].kind == "tool_call" and els[0].tool_name == "edit"
    print(f"{OK if ok else FAIL} codex_elements custom_tool_call (got {[(e.kind,e.tool_name) for e in els]})")
    return ok


def test_codex_elements_function_call_output() -> bool:
    t = _write_codex([
        {"type": "response_item", "timestamp": "t1",
         "payload": {"type": "function_call_output", "id": "o1",
                     "output": json.dumps({"output": "result text"})}},
    ])
    els = _codex_elements(t)
    ok = len(els) == 1 and els[0].kind == "tool_result" and els[0].text == "result text"
    print(f"{OK if ok else FAIL} codex_elements function_call_output unwrapped (got {[(e.kind,e.text) for e in els]})")
    return ok


def test_codex_elements_function_call_output_raw_string() -> bool:
    t = _write_codex([
        {"type": "response_item", "timestamp": "t1",
         "payload": {"type": "function_call_output", "id": "o1", "output": "raw string"}},
    ])
    els = _codex_elements(t)
    ok = len(els) == 1 and els[0].text == "raw string"
    print(f"{OK if ok else FAIL} codex_elements raw string output (got {[(e.kind,e.text) for e in els]})")
    return ok


# ─── gemini element extractor ──────────────────────────────────────────────

def test_gemini_elements_user_prompt() -> bool:
    t = _write_gemini([
        {"id": "u1", "timestamp": "t1", "type": "user", "content": [{"text": "hi"}]},
    ])
    els = _gemini_elements(t)
    ok = len(els) == 1 and els[0].kind == "user_prompt"
    print(f"{OK if ok else FAIL} gemini_elements user_prompt (got {[(e.kind,e.text) for e in els]})")
    return ok


def test_gemini_elements_assistant_text() -> bool:
    t = _write_gemini([
        {"id": "g1", "timestamp": "t1", "type": "gemini", "content": [{"text": "reply"}]},
    ])
    els = _gemini_elements(t)
    ok = len(els) == 1 and els[0].kind == "assistant_text"
    print(f"{OK if ok else FAIL} gemini_elements assistant_text (got {[(e.kind,e.text) for e in els]})")
    return ok


def test_gemini_elements_function_call() -> bool:
    t = _write_gemini([
        {"id": "g1", "timestamp": "t1", "type": "gemini",
         "content": [{"functionCall": {"name": "run_shell", "args": {"cmd": "ls"}}}]},
    ])
    els = _gemini_elements(t)
    ok = len(els) == 1 and els[0].kind == "tool_call" and els[0].tool_name == "run_shell"
    print(f"{OK if ok else FAIL} gemini_elements functionCall tool_call (got {[(e.kind,e.tool_name) for e in els]})")
    return ok


def test_gemini_elements_function_response() -> bool:
    t = _write_gemini([
        {"id": "g1", "timestamp": "t1", "type": "gemini",
         "content": [{"functionResponse": {"name": "read_file",
                                           "response": {"ok": "data"}}}]},
    ])
    els = _gemini_elements(t)
    ok = len(els) == 1 and els[0].kind == "tool_result"
    print(f"{OK if ok else FAIL} gemini_elements functionResponse tool_result (got {[(e.kind) for e in els]})")
    return ok


def test_gemini_elements_drops_non_user_gemini() -> bool:
    t = _write_gemini([
        {"id": "x", "type": "system", "content": [{"text": "system note"}]},
    ])
    els = _gemini_elements(t)
    ok = els == []
    print(f"{OK if ok else FAIL} gemini_elements drops non user/gemini type (got {[(e.kind) for e in els]})")
    return ok


# ─── cwd token decode ──────────────────────────────────────────────────────

def test_decode_cwd_token_basic() -> bool:
    ok = _decode_cwd_token("-Users-ofek-proj") == "/Users/ofek/proj"
    print(f"{OK if ok else FAIL} decode_cwd_token basic (got {_decode_cwd_token('-Users-ofek-proj')!r})")
    return ok


def test_decode_cwd_token_empty() -> bool:
    ok = _decode_cwd_token("") == ""
    print(f"{OK if ok else FAIL} decode_cwd_token empty -> empty")
    return ok


def test_decode_cwd_token_all_dashes() -> bool:
    ok = _decode_cwd_token("---") == ""
    print(f"{OK if ok else FAIL} decode_cwd_token all dashes -> empty (got {_decode_cwd_token('---')!r})")
    return ok


def test_decode_cwd_token_leading_dashes() -> bool:
    ok = _decode_cwd_token("--Users-test") == "/Users/test"
    print(f"{OK if ok else FAIL} decode_cwd_token strips leading dashes (got {_decode_cwd_token('--Users-test')!r})")
    return ok


def test_encode_cwd_underscore_becomes_dash() -> bool:
    """encode_cwd maps _ -> -, so underscore paths share a token with dash paths."""
    enc_u = encode_cwd("/foo_bar")
    enc_d = encode_cwd("/foo-bar")
    ok = enc_u == enc_d and "_" not in enc_u
    print(f"{OK if ok else FAIL} encode_cwd underscore->dash (u={enc_u!r}, d={enc_d!r})")
    return ok


def test_decode_cwd_token_dash_ambiguous_documented() -> bool:
    """_decode_cwd_token is intentionally lossy: every '-' becomes '/', so a
    cwd containing a dash cannot be round-tripped (encode_cwd maps both '-' and
    '_' to '-'). This is the documented ambiguity — callers needing an exact
    match compare via encode_cwd, not the decoded string. '/Users/me/proj'
    (dashes only) round-trips; '/proj-x' (a real dash) decodes to '/proj/x'."""
    enc = encode_cwd("/Users/me/proj")
    dec = _decode_cwd_token(enc)
    ok = dec == "/Users/me/proj"
    # And a path WITH a dash does NOT round-trip (the ambiguity in action):
    enc2 = encode_cwd("/proj-x")
    ok = ok and _decode_cwd_token(enc2) == "/proj/x"
    print(f"{OK if ok else FAIL} decode_cwd_token dash-ambiguity documented "
          f"(plain={dec!r}, dashed={_decode_cwd_token(enc2)!r})")
    return ok


# ─── parse_elements dispatch ───────────────────────────────────────────────

def test_parse_elements_dispatch_claude() -> bool:
    t = _write_claude_raw([_claude_user("p", "u1")])
    els = _candidate("s1", "/p", t, fmt="claude").parse_elements()
    ok = len(els) == 1 and els[0].kind == "user_prompt"
    print(f"{OK if ok else FAIL} parse_elements claude dispatch (got {len(els)})")
    return ok


def test_parse_elements_dispatch_codex() -> bool:
    t = _write_codex([{"type": "response_item", "timestamp": "t1",
                       "payload": {"type": "message", "role": "user", "id": "u1",
                                   "content": [{"type": "input_text", "text": "p"}]}}])
    els = _candidate("s1", "/p", t, fmt="codex").parse_elements()
    ok = len(els) == 1 and els[0].kind == "user_prompt"
    print(f"{OK if ok else FAIL} parse_elements codex dispatch (got {len(els)})")
    return ok


def test_parse_elements_dispatch_gemini() -> bool:
    t = _write_gemini([{"id": "u1", "timestamp": "t1", "type": "user", "content": [{"text": "p"}]}])
    els = _candidate("s1", "/p", t, fmt="gemini").parse_elements()
    ok = len(els) == 1 and els[0].kind == "user_prompt"
    print(f"{OK if ok else FAIL} parse_elements gemini dispatch (got {len(els)})")
    return ok


def test_parse_elements_missing_file_returns_empty() -> bool:
    els = _candidate("s1", "/p", _SCRATCH / "nope.jsonl", fmt="claude").parse_elements()
    ok = els == []
    print(f"{OK if ok else FAIL} parse_elements missing file -> [] (got {els})")
    return ok


# ─── discovery ─────────────────────────────────────────────────────────────

def test_iter_all_claude_only() -> bool:
    projects = _SCRATCH / "it-claude"
    cwd = "/proj/alpha"
    sd = projects / encode_cwd(cwd)
    sd.mkdir(parents=True, exist_ok=True)
    _w(sd / "sid1.jsonl", [_claude_user("discover me", "u1")])
    orig = _isolate_native_roots(claude=[projects])
    try:
        cands = list(M.iter_all_native_candidates())
    finally:
        _restore_native_roots(orig)
    ok = len(cands) == 1 and cands[0].sid == "sid1" and cands[0].format == "claude"
    print(f"{OK if ok else FAIL} iter_all finds claude transcript (got {[(c.sid,c.format) for c in cands]})")
    return ok


def test_iter_all_codex_rollout() -> bool:
    codex = _SCRATCH / "it-codex"
    codex.mkdir(parents=True, exist_ok=True)
    _w(codex / "rollout-x.jsonl", [
        {"type": "session_meta", "payload": {"cwd": "/zapp"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "p"}]}},
    ])
    orig = _isolate_native_roots(claude=[], codex=codex)
    try:
        cands = list(M.iter_all_native_candidates())
    finally:
        _restore_native_roots(orig)
    ok = len(cands) == 1 and cands[0].format == "codex" and cands[0].cwd == "/zapp"
    print(f"{OK if ok else FAIL} iter_all finds codex rollout (got {[(c.format,c.cwd) for c in cands]})")
    return ok


def test_iter_all_gemini_chat() -> bool:
    gemini_root = _SCRATCH / "it-gemini-tmp"
    chats = gemini_root / encode_cwd("/gproj") / "chats"
    chats.mkdir(parents=True, exist_ok=True)
    _w(chats / "session-1.jsonl", [{"id": "u1", "type": "user", "content": [{"text": "p"}]}])
    orig = _isolate_native_roots(claude=[], gemini=gemini_root)
    try:
        cands = list(M.iter_all_native_candidates())
    finally:
        _restore_native_roots(orig)
    ok = len(cands) == 1 and cands[0].format == "gemini"
    print(f"{OK if ok else FAIL} iter_all finds gemini chat (got {[(c.format) for c in cands]})")
    return ok


def test_iter_all_runs_dir() -> bool:
    runs = _SCRATCH / "it-runs"
    run_dir = runs / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    _w(run_dir / "state.json", [{"app_session_id": "app-sid-1"}])
    _w(run_dir / "session_events.jsonl", [_claude_user("run prompt", "u1")])
    orig = _isolate_native_roots(claude=[], runs=runs)
    try:
        cands = list(M.iter_all_native_candidates())
    finally:
        _restore_native_roots(orig)
    ok = (len(cands) == 1 and cands[0].format == "claude"
          and cands[0].sid == "app-sid-1")
    print(f"{OK if ok else FAIL} iter_all finds run-dir transcript (got {[(c.format,c.sid) for c in cands]})")
    return ok


def test_iter_all_runs_dir_requires_state_json() -> bool:
    """Run-dir discovery globs ``*/state.json``; a run dir without state.json
    is NOT discovered (the run-dir index is keyed off state.json's
    app_session_id). This is the documented contract — verify it holds so a
    later refactor that drops the glob doesn't silently change discovery."""
    runs = _SCRATCH / "it-runs2"
    run_dir = runs / "run-no-state"
    run_dir.mkdir(parents=True, exist_ok=True)
    _w(run_dir / "session_events.jsonl", [_claude_user("x", "u1")])
    orig = _isolate_native_roots(claude=[], runs=runs)
    try:
        cands = list(M.iter_all_native_candidates())
    finally:
        _restore_native_roots(orig)
    ok = cands == []
    print(f"{OK if ok else FAIL} iter_all run dir requires state.json (got {len(cands)})")
    return ok


# ===========================================================================
# native_session_prompt_search — tokens, categorizer, search
# ===========================================================================

def test_query_tokens_lowercases_and_filters() -> bool:
    toks = _query_tokens("Fix THE Bug in 3D")
    # "the" is a stopword, single chars dropped (min len 2), 3D -> 3d
    ok = toks == ["fix", "bug", "3d"]
    print(f"{OK if ok else FAIL} query_tokens lowercase+filter (got {toks})")
    return ok


def test_query_tokens_drops_single_chars() -> bool:
    ok = _query_tokens("a I x ok") == ["ok"]
    print(f"{OK if ok else FAIL} query_tokens drops <2 char tokens (got {_query_tokens('a I x ok')})")
    return ok


def test_query_tokens_drops_all_stopwords() -> bool:
    ok = _query_tokens("the is in to of") == []
    print(f"{OK if ok else FAIL} query_tokens drops stopwords-only (got {_query_tokens('the is in to of')})")
    return ok


def test_query_tokens_alphanumeric_only() -> bool:
    ok = _query_tokens("foo.bar_baz!qux") == ["foo", "bar", "baz", "qux"]
    print(f"{OK if ok else FAIL} query_tokens splits on non-alnum (got {_query_tokens('foo.bar_baz!qux')})")
    return ok


def test_token_patterns_whole_word() -> bool:
    pats = _token_patterns(["in"])
    # "in" matches the word "in" but not inside "building"
    ok = bool(pats[0].search("the in thing")) and not bool(pats[0].search("building guise"))
    print(f"{OK if ok else FAIL} token_patterns whole-word (got {ok})")
    return ok


def test_search_empty_query_returns_empty() -> bool:
    _patch_candidates([_candidate_from_prompts("s1", "/p", [("hello", "2024-01-01")])])
    try:
        out = nsp.search_native_session_prompts(query="")
    finally:
        _reset_candidates()
    ok = out == []
    print(f"{OK if ok else FAIL} empty query -> empty (got {out})")
    return ok


def test_search_stopword_only_returns_empty() -> bool:
    _patch_candidates([_candidate_from_prompts("s1", "/p", [("the plan is in the doc", "2024-01-01")])])
    try:
        out = nsp.search_native_session_prompts(query="in the")
    finally:
        _reset_candidates()
    ok = out == []
    print(f"{OK if ok else FAIL} stopword-only query -> empty (got {out})")
    return ok


def test_search_whole_word_not_substring() -> bool:
    _patch_candidates([
        _candidate_from_prompts("s1", "/p", [("fix the ui layout", "2024-01-01")]),
        _candidate_from_prompts("s2", "/p", [("rebuilding guise now", "2024-01-02")]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="ui")
    finally:
        _reset_candidates()
    texts = {r["text"] for r in out}
    ok = texts == {"fix the ui layout"}
    print(f"{OK if ok else FAIL} search whole-word not substring (got {texts})")
    return ok


def test_search_ranking_higher_overlap_wins() -> bool:
    _patch_candidates([
        _candidate_from_prompts("s1", "/p", [("offline sync mode broke", "2024-01-01")]),
        _candidate_from_prompts("s2", "/p", [("offline notes only", "2024-01-02")]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="offline sync mode", max_matches=1)
    finally:
        _reset_candidates()
    ok = len(out) == 1 and out[0]["text"] == "offline sync mode broke"
    print(f"{OK if ok else FAIL} ranking higher overlap (got {[r['text'] for r in out]})")
    return ok


def test_search_cwd_filter_restricts() -> bool:
    _patch_candidates([
        _candidate_from_prompts("s1", "/a", [("offline here", "2024-01-01")]),
        _candidate_from_prompts("s2", "/b", [("offline there", "2024-01-02")]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="offline", cwds=("/a",))
    finally:
        _reset_candidates()
    texts = {r["text"] for r in out}
    ok = texts == {"offline here"}
    print(f"{OK if ok else FAIL} cwd filter restricts (got {texts})")
    return ok


def test_search_cwd_filter_encoded_match() -> bool:
    """cwd filter matches via encode_cwd too (underscore/dash equivalence)."""
    _patch_candidates([
        _candidate_from_prompts("s1", "/foo_bar", [("needle x", "2024-01-01")]),
    ])
    try:
        # query cwd uses dash form; candidate uses underscore — encode_cwd makes them equal
        out = nsp.search_native_session_prompts(query="needle", cwds=("/foo-bar",))
    finally:
        _reset_candidates()
    texts = {r["text"] for r in out}
    ok = texts == {"needle x"}
    print(f"{OK if ok else FAIL} cwd filter encode_cwd equivalence (got {texts})")
    return ok


def test_search_is_noise_drops() -> bool:
    _patch_candidates([
        _candidate_from_prompts("s1", "/p", [
            ("NOISE preamble offline", "2024-01-01"),
            ("real offline req", "2024-01-02"),
        ]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="offline", is_noise=lambda t: t.startswith("NOISE"))
    finally:
        _reset_candidates()
    texts = {r["text"] for r in out}
    ok = texts == {"real offline req"}
    print(f"{OK if ok else FAIL} is_noise drops preamble (got {texts})")
    return ok


def test_search_dedup_identical_text() -> bool:
    _patch_candidates([
        _candidate_from_prompts("s1", "/p", [("offline sync survive", "2024-01-01")]),
        _candidate_from_prompts("s2", "/p", [("offline sync survive", "2024-01-02")]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="offline sync")
    finally:
        _reset_candidates()
    ok = len(out) == 1
    print(f"{OK if ok else FAIL} dedup identical text (got {len(out)})")
    return ok


def test_search_oldest_first_presentation() -> bool:
    """After ranking selects, presentation is oldest-first (ts ascending)."""
    _patch_candidates([
        _candidate_from_prompts("s1", "/p", [("offline late", "2024-03-01")]),
        _candidate_from_prompts("s2", "/p", [("offline early", "2024-01-01")]),
        _candidate_from_prompts("s3", "/p", [("offline mid", "2024-02-01")]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="offline", max_matches=10)
    finally:
        _reset_candidates()
    order = [r["text"] for r in out]
    ok = order == ["offline early", "offline mid", "offline late"]
    print(f"{OK if ok else FAIL} oldest-first presentation (got {order})")
    return ok


def test_search_empty_ts_sorts_last_deterministically() -> bool:
    _patch_candidates([
        _candidate_from_prompts("sB", "/p", [("offline beta", "")]),
        _candidate_from_prompts("sA", "/p", [("offline alpha", "")]),
        _candidate_from_prompts("sC", "/p", [("offline gamma", "")]),
    ])
    try:
        out = nsp.search_native_session_prompts(query="offline")
    finally:
        _reset_candidates()
    order = [r["text"] for r in out]
    ok = order == ["offline alpha", "offline beta", "offline gamma"]
    print(f"{OK if ok else FAIL} empty-ts deterministic order (got {order})")
    return ok


def test_search_record_kind_and_source_prompts() -> bool:
    _patch_candidates([_candidate_from_prompts("s1", "/p", [("offline here", "2024-01-01")])])
    try:
        out = nsp.search_native_session_prompts(query="offline")
    finally:
        _reset_candidates()
    ok = (out and out[0]["kind"] == "native_session_prompt"
          and out[0]["source"] == "native_session_fallback"
          and out[0]["category"] == ElementCategory.PROMPT)
    print(f"{OK if ok else FAIL} prompts record labels (got {out[0] if out else None})")
    return ok


def test_search_transcripts_includes_reply() -> bool:
    """search_native_session_transcripts covers prompt + reply categories."""
    t = _write_claude_raw([
        _claude_user("offline question", "u1"),
        _claude_assistant_blocks([_claude_text_block("offline answer")], uid="a1"),
    ])
    _patch_candidates([_candidate("s1", "/p", t)])
    try:
        out = nsp.search_native_session_transcripts(query="offline")
    finally:
        _reset_candidates()
    texts = {r["text"] for r in out}
    ok = texts == {"offline question", "offline answer"}
    print(f"{OK if ok else FAIL} transcripts includes reply (got {texts})")
    return ok


def test_search_transcripts_excludes_tool_call() -> bool:
    """The transcripts facade is prompt+reply only; a tool_call must NOT appear."""
    t = _write_claude_raw([
        _claude_assistant_blocks([
            _claude_text_block("offline note"),
            _claude_tool_use("Bash", {"command": "offline cmd"}),
        ], uid="a1"),
    ])
    _patch_candidates([_candidate("s1", "/p", t)])
    try:
        out = nsp.search_native_session_transcripts(query="offline")
    finally:
        _reset_candidates()
    cats = {r["category"] for r in out}
    ok = cats == {ElementCategory.REPLY}
    print(f"{OK if ok else FAIL} transcripts excludes tool_call (got {cats})")
    return ok


def test_generalized_search_returns_categories_and_tools() -> bool:
    t = _write_claude_raw([
        _claude_assistant_blocks([
            _claude_text_block("running zulifrangible build"),
            _claude_tool_use("Bash", {"command": "make zulifrangible-widget"}),
        ], uid="a1"),
        _claude_tool_result("t1", "zulifrangible widget built ok"),
    ])
    _patch_candidates([_candidate("s1", "/p", t)])
    try:
        out = nsp.search_in_native_session_transcript(query="zulifrangible widget")
    finally:
        _reset_candidates()
    cats = {r["category"] for r in out}
    tools = {r.get("tool_name") for r in out}
    ok = cats == {ElementCategory.REPLY, ElementCategory.SHELL, ElementCategory.TOOL_OUTPUT}
    print(f"{OK if ok else FAIL} generalized search categories+tools (cats={cats}, tools={tools})")
    return ok


def test_generalized_search_category_filter_shell() -> bool:
    t = _write_claude_raw([
        _claude_assistant_blocks([
            _claude_text_block("zulifrangible note"),
            _claude_tool_use("Bash", {"command": "make zulifrangible"}),
        ], uid="a1"),
    ])
    _patch_candidates([_candidate("s1", "/p", t)])
    try:
        out = nsp.search_in_native_session_transcript(query="zulifrangible", categories=("shell",))
    finally:
        _reset_candidates()
    cats = {r["category"] for r in out}
    ok = cats == {ElementCategory.SHELL} and len(out) == 1
    print(f"{OK if ok else FAIL} category filter shell (got {cats}, len={len(out)})")
    return ok


def test_generalized_search_kind_filter_tool_call() -> bool:
    t = _write_claude_raw([
        _claude_assistant_blocks([
            _claude_text_block("zulifrangible text"),
            _claude_tool_use("Edit", {"file": "zulifrangible.py"}),
        ], uid="a1"),
    ])
    _patch_candidates([_candidate("s1", "/p", t)])
    try:
        out = nsp.search_in_native_session_transcript(query="zulifrangible", kinds=("tool_call",))
    finally:
        _reset_candidates()
    eks = {r["element_kind"] for r in out}
    ok = eks == {"tool_call"} and len(out) == 1
    print(f"{OK if ok else FAIL} kind filter tool_call (got {eks}, len={len(out)})")
    return ok


# ─── Categorizer: every tool class ─────────────────────────────────────────

def test_cat_prompt() -> bool:
    ok = Categorizer().categorize(NativeElement("user_prompt", "user", "x")) == ElementCategory.PROMPT
    print(f"{OK if ok else FAIL} categorize prompt")
    return ok


def test_cat_reply() -> bool:
    ok = Categorizer().categorize(NativeElement("assistant_text", "assistant", "x")) == ElementCategory.REPLY
    print(f"{OK if ok else FAIL} categorize reply")
    return ok


def test_cat_reasoning() -> bool:
    ok = Categorizer().categorize(NativeElement("reasoning", "assistant", "x")) == ElementCategory.REASONING
    print(f"{OK if ok else FAIL} categorize reasoning")
    return ok


def test_cat_command() -> bool:
    ok = Categorizer().categorize(NativeElement("command", "user", "/x")) == ElementCategory.COMMAND
    print(f"{OK if ok else FAIL} categorize command")
    return ok


def test_cat_meta() -> bool:
    ok = Categorizer().categorize(NativeElement("meta", "user", "x")) == ElementCategory.META
    print(f"{OK if ok else FAIL} categorize meta")
    return ok


def test_cat_edit_tool() -> bool:
    for name in ("Edit", "MultiEdit", "Write", "write_file", "apply_patch", "str_replace_editor"):
        got = Categorizer().categorize(NativeElement("tool_call", "assistant", "x", name))
        if got != ElementCategory.FILE_EDIT:
            print(f"{FAIL} categorize edit tool {name} -> {got}")
            return False
    print(f"{OK} categorize edit tools -> file_edit")
    return True


def test_cat_shell_tool() -> bool:
    for name in ("Bash", "shell", "exec_command", "terminal"):
        got = Categorizer().categorize(NativeElement("tool_call", "assistant", "x", name))
        if got != ElementCategory.SHELL:
            print(f"{FAIL} categorize shell tool {name} -> {got}")
            return False
    print(f"{OK} categorize shell tools -> shell")
    return True


def test_cat_read_tool() -> bool:
    for name in ("Read", "read_file", "view"):
        got = Categorizer().categorize(NativeElement("tool_call", "assistant", "x", name))
        if got != ElementCategory.FILE_READ:
            print(f"{FAIL} categorize read tool {name} -> {got}")
            return False
    print(f"{OK} categorize read tools -> file_read")
    return True


def test_cat_search_tool() -> bool:
    for name in ("Grep", "Glob", "WebSearch", "search_files", "webfetch"):
        got = Categorizer().categorize(NativeElement("tool_call", "assistant", "x", name))
        if got != ElementCategory.SEARCH:
            print(f"{FAIL} categorize search tool {name} -> {got}")
            return False
    print(f"{OK} categorize search tools -> search")
    return True


def test_cat_subagent_tool() -> bool:
    for name in ("Task", "Agent", "spawn_agent", "delegate_task"):
        got = Categorizer().categorize(NativeElement("tool_call", "assistant", "x", name))
        if got != ElementCategory.SUBAGENT:
            print(f"{FAIL} categorize subagent tool {name} -> {got}")
            return False
    print(f"{OK} categorize subagent tools -> subagent")
    return True


def test_cat_unknown_tool_other() -> bool:
    got = Categorizer().categorize(NativeElement("tool_call", "assistant", "x", "MysteryTool"))
    ok = got == ElementCategory.OTHER
    print(f"{OK if ok else FAIL} categorize unknown tool -> other (got {got})")
    return ok


def test_cat_tool_name_case_insensitive() -> bool:
    ok = (Categorizer().categorize(NativeElement("tool_call", "a", "x", "BASH"))
          == Categorizer().categorize(NativeElement("tool_call", "a", "x", "bash"))
          == ElementCategory.SHELL)
    print(f"{OK if ok else FAIL} categorize case-insensitive")
    return ok


def test_cat_tool_name_slash_normalized() -> bool:
    """str_replace_editor-style names with / are normalized to _."""
    got = Categorizer().categorize(NativeElement("tool_call", "a", "x", "str/replace/editor"))
    # After normalization 'str/replace/editor' -> 'str_replace_editor' which IS an edit tool
    ok = got == ElementCategory.FILE_EDIT
    print(f"{OK if ok else FAIL} categorize slash-normalized (got {got})")
    return ok


def test_cat_tool_result_ok() -> bool:
    ok = Categorizer().categorize(NativeElement("tool_result", "user", "all good")) == ElementCategory.TOOL_OUTPUT
    print(f"{OK if ok else FAIL} categorize tool_result -> tool_output")
    return ok


def test_cat_tool_result_error_traceback() -> bool:
    got = Categorizer().categorize(NativeElement("tool_result", "user", "Traceback (most recent call last)"))
    ok = got == ElementCategory.ERROR
    print(f"{OK if ok else FAIL} categorize tool_result traceback -> error (got {got})")
    return ok


def test_cat_tool_result_error_failed() -> bool:
    got = Categorizer().categorize(NativeElement("tool_result", "user", "command failed to run"))
    ok = got == ElementCategory.ERROR
    print(f"{OK if ok else FAIL} categorize tool_result 'failed' -> error (got {got})")
    return ok


def test_cat_tool_result_error_command_not_found() -> bool:
    got = Categorizer().categorize(NativeElement("tool_result", "user", "foo: command not found"))
    ok = got == ElementCategory.ERROR
    print(f"{OK if ok else FAIL} categorize 'command not found' -> error (got {got})")
    return ok


def test_cat_unknown_kind_other() -> bool:
    got = Categorizer().categorize(NativeElement("weird_kind", "x", "y"))
    ok = got == ElementCategory.OTHER
    print(f"{OK if ok else FAIL} categorize unknown kind -> other (got {got})")
    return ok


def test_cat_empty_tool_name_other() -> bool:
    got = Categorizer().categorize(NativeElement("tool_call", "a", "x", ""))
    ok = got == ElementCategory.OTHER
    print(f"{OK if ok else FAIL} categorize empty tool_name -> other (got {got})")
    return ok


# ─── rg filter + native roots ──────────────────────────────────────────────

def test_rg_filter_none_when_no_roots() -> bool:
    _reset_candidates()
    orig = _isolate_native_roots(claude=[], codex=_SCRATCH / "no-codex",
                                 gemini=_SCRATCH / "no-gemini", runs=_SCRATCH / "no-runs")
    try:
        res = nsp._rg_filter(["anything"])
    finally:
        _restore_native_roots(orig)
    ok = res is None
    print(f"{OK if ok else FAIL} rg_filter None when no roots (got {res})")
    return ok


def test_rg_filter_none_for_empty_tokens() -> bool:
    _reset_candidates()
    orig = _isolate_native_roots(claude=[_SCRATCH / "some"])
    try:
        res = nsp._rg_filter([])
    finally:
        _restore_native_roots(orig)
    ok = res is None
    print(f"{OK if ok else FAIL} rg_filter None for empty tokens (got {res})")
    return ok


def test_rg_filter_finds_needle_files() -> bool:
    _reset_candidates()
    if not shutil.which("rg"):
        print(f"{OK} rg-filter-finds skipped (rg not installed)")
        return True
    projects = _SCRATCH / "rgf-projects"
    sd = projects / encode_cwd("/rgf")
    sd.mkdir(parents=True, exist_ok=True)
    _w(sd / "hit.jsonl", [_claude_user("zulifrangible needle", "u1")])
    _w(sd / "miss.jsonl", [_claude_user("nothing here", "u1")])
    orig = _isolate_native_roots(claude=[projects], codex=_SCRATCH / "no-codex",
                                 gemini=_SCRATCH / "no-gemini", runs=_SCRATCH / "no-runs")
    try:
        hits = nsp._rg_filter(["zulifrangible"])
    finally:
        _restore_native_roots(orig)
    sids = {Path(p).stem for p, _ in (hits or [])}
    ok = sids == {"hit"}
    print(f"{OK if ok else FAIL} rg_filter finds needle files (got {sids})")
    return ok


def test_classify_root_claude() -> bool:
    roots = [(_SCRATCH / "claude", "claude")]
    ok = nsp._classify_root(_SCRATCH / "claude" / "x" / "y.jsonl", roots) == "claude"
    print(f"{OK if ok else FAIL} classify_root claude")
    return ok


def test_classify_root_unknown_defaults_claude() -> bool:
    roots = [(_SCRATCH / "other", "codex")]
    ok = nsp._classify_root(_SCRATCH / "unrelated" / "y.jsonl", roots) == "claude"
    print(f"{OK if ok else FAIL} classify_root unknown -> claude default")
    return ok


# ===========================================================================
# native_transcript_index
# ============================================================================

def test_idx_refresh_marks_covered_and_usable() -> bool:
    token = _idx_setup_roots()
    try:
        ok_pre = not idx.is_covered() and not idx.is_usable()
        idx.refresh_once()
        ok_post = idx.is_covered() and idx.is_usable()
    finally:
        _restore_idx_roots(token)
    ok = ok_pre and ok_post
    print(f"{OK if ok else FAIL} refresh marks covered+usable (pre={ok_pre}, post={ok_post})")
    return ok


def test_idx_indexes_user_prompt() -> bool:
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        _w(claude / encode_cwd("/p") / "s1.jsonl", [_claude_user("needleword alpha", "u1")])
        idx.refresh_once()
        rows = idx.search_rows(["needleword"], limit=10)
    finally:
        _restore_idx_roots(token)
    ok = len(rows) == 1 and "needleword alpha" in rows[0]["text"]
    print(f"{OK if ok else FAIL} idx indexes user_prompt (got {len(rows)})")
    return ok


def test_idx_drops_tool_result_lean() -> bool:
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        _w(claude / encode_cwd("/p") / "s1.jsonl", [
            _claude_assistant_blocks([
                _claude_text_block("leanneedle visible"),
                _claude_tool_use("Bash", {"command": "make leanneedle"}),
            ], uid="a1"),
            _claude_tool_result("t1", "leanneedle bulk dump output"),
        ])
        idx.refresh_once()
        rows = idx.search_rows(["leanneedle"], limit=20)
    finally:
        _restore_idx_roots(token)
    kinds = {r["element_kind"] for r in rows}
    ok = (kinds == {"assistant_text", "tool_call"}
          and not any("bulk dump output" in r["text"] for r in rows))
    print(f"{OK if ok else FAIL} idx lean drops tool_result (kinds={kinds})")
    return ok


def test_idx_freshness_reindexes_changed_file() -> bool:
    """Equal-length content rewrite (same byte count, different needle) so
    freshness differs on MTIME alone, not size. Waits for real mtime granularity
    and asserts touched == 1 exactly (not >= 1)."""
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        f = claude / encode_cwd("/p") / "s1.jsonl"
        # Two needles of EQUAL length so the rewrite keeps byte count identical.
        old_record = _claude_user("firstneedle AAA", "u1")
        new_record = _claude_user("deltaneedle BBB", "u1")
        assert len(json.dumps(old_record)) == len(json.dumps(new_record))
        _w(f, [old_record])
        idx.refresh_once()
        before = idx.search_rows(["firstneedle"], limit=5)
        # Wait for filesystem mtime granularity to tick (at least 1 ns won't do
        # on macOS HFS+/APFS coarse resolution; poll until stat changes).
        deadline = time.monotonic() + 5.0
        prev_mtime = f.stat().st_mtime
        f.write_text(json.dumps(new_record) + "\n", encoding="utf-8")
        os.utime(f, (time.time() + 2, time.time() + 2))
        while time.monotonic() < deadline and f.stat().st_mtime == prev_mtime:
            time.sleep(0.05)
        r = idx.refresh_once()
        after = idx.search_rows(["deltaneedle"], limit=5)
        old_after = idx.search_rows(["firstneedle"], limit=5)
    finally:
        _restore_idx_roots(token)
    ok = (len(before) == 1 and r["touched"] == 1 and len(after) == 1
          and old_after == [])
    print(f"{OK if ok else FAIL} idx freshness reindexes (touched={r['touched']}, "
          f"after={len(after)}, old_gone={old_after == []})")
    return ok


def test_idx_tombstones_deleted_file() -> bool:
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        f = claude / encode_cwd("/p") / "s1.jsonl"
        _w(f, [_claude_user("tombneedle here", "u1")])
        idx.refresh_once()
        assert idx.search_rows(["tombneedle"], limit=5)
        f.unlink()
        r = idx.refresh_once()
        after = idx.search_rows(["tombneedle"], limit=5)
    finally:
        _restore_idx_roots(token)
    ok = after == [] and r["touched"] >= 1
    print(f"{OK if ok else FAIL} idx tombstones deleted file (after={len(after)})")
    return ok


def test_idx_match_paths_returns_pairs() -> bool:
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        _w(claude / encode_cwd("/pa") / "s1.jsonl", [_claude_user("sharedneedle alpha", "u1")])
        _w(claude / encode_cwd("/pb") / "s2.jsonl", [_claude_user("sharedneedle beta", "u1")])
        idx.refresh_once()
        hits = idx.match_paths(["sharedneedle"], set()) or []
    finally:
        _restore_idx_roots(token)
    sids = {Path(p).stem for p, _ in hits}
    ok = sids == {"s1", "s2"}
    print(f"{OK if ok else FAIL} match_paths returns pairs (got {sids})")
    return ok


def test_idx_match_paths_cwd_filter() -> bool:
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        _w(claude / encode_cwd("/pa") / "s1.jsonl", [_claude_user("cwdneedle alpha", "u1")])
        _w(claude / encode_cwd("/pb") / "s2.jsonl", [_claude_user("cwdneedle beta", "u1")])
        idx.refresh_once()
        hits = idx.match_paths(["cwdneedle"], {"/pa"}) or []
    finally:
        _restore_idx_roots(token)
    sids = {Path(p).stem for p, _ in hits}
    ok = sids == {"s1"}
    print(f"{OK if ok else FAIL} match_paths cwd filter (got {sids})")
    return ok


def test_idx_match_paths_broad_returns_none() -> bool:
    """Too many distinct matched FILES (> _PATH_CAP) => None."""
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        enc = encode_cwd("/p")
        for i in range(idx._PATH_CAP + 5):
            _w(claude / enc / f"s{i}.jsonl", [_claude_user(f"broadneedle {i}", "u1")])
        idx.refresh_once()
        res = idx.match_paths(["broadneedle"], set())
    finally:
        _restore_idx_roots(token)
    ok = res is None
    print(f"{OK if ok else FAIL} match_paths broad (files>cap) -> None (got {res})")
    return ok


def test_idx_match_paths_scan_limit_returns_none() -> bool:
    """When the FTS element-row scan hits _MATCHED_SCAN_LIMIT, the result is
    truncated and the deduped path list may be silently incomplete => None so
    the caller falls back to rg. Build > _MATCHED_SCAN_LIMIT matching element
    rows in a SINGLE file (few files, many elements — keeps it fast) and assert
    None. The single file has more than _MATCHED_SCAN_LIMIT matching elements so
    the FTS scan is capped; the path list is trivially incomplete."""
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        enc = encode_cwd("/p")
        # One file with > _MATCHED_SCAN_LIMIT matching element rows. Each
        # assistant turn contributes 4 indexed elements (text+text+tool+think);
        # we need > _MATCHED_SCAN_LIMIT/4 turns.
        n_turns = (idx._MATCHED_SCAN_LIMIT // 4) + 10
        records = []
        for i in range(n_turns):
            records.append(_claude_assistant_blocks([
                _claude_text_block(f"scanlimneedle text {i}"),
                _claude_text_block(f"scanlimneedle more {i}"),
                _claude_tool_use("Bash", {"command": "scanlimneedle cmd"}),
                _claude_thinking_block(f"scanlimneedle think {i}"),
            ], uid=f"a{i}"))
        _w(claude / enc / "s1.jsonl", records)
        idx.refresh_once()
        res = idx.match_paths(["scanlimneedle"], set())
    finally:
        _restore_idx_roots(token)
    ok = res is None
    print(f"{OK if ok else FAIL} match_paths scan-limit -> None (got {res})")
    return ok


def test_idx_not_usable_when_empty_tokens() -> bool:
    token = _idx_setup_roots()
    try:
        idx.refresh_once()
        # match_paths returns None for empty tokens regardless of usability
        res = idx.match_paths([], set())
    finally:
        _restore_idx_roots(token)
    ok = res is None
    print(f"{OK if ok else FAIL} match_paths empty tokens -> None (got {res})")
    return ok


def test_idx_search_rows_empty_when_not_usable() -> bool:
    token = _idx_setup_roots()
    try:
        # Not yet refreshed -> not usable
        rows = idx.search_rows(["anything"], limit=5)
    finally:
        _restore_idx_roots(token)
    ok = rows == []
    print(f"{OK if ok else FAIL} search_rows empty when not usable (got {rows})")
    return ok


def test_idx_search_rows_empty_tokens() -> bool:
    token = _idx_setup_roots()
    try:
        idx.refresh_once()
        rows = idx.search_rows([], limit=5)
    finally:
        _restore_idx_roots(token)
    ok = rows == []
    print(f"{OK if ok else FAIL} search_rows empty tokens -> [] (got {rows})")
    return ok


def test_idx_wait_fresh_serves_delta() -> bool:
    """Drive the REAL worker (ensure_started + request_refresh) and assert a file
    changed after the last refresh becomes searchable once steady refresh fires."""
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        fpath = claude / encode_cwd("/p") / "a.jsonl"
        _w(fpath, [_claude_user("waitneedle here", "u1")])
        idx.refresh_once()
        with fpath.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_claude_user("deltawaitneedle new", "u2")) + "\n")
        # Advance mtime strictly past the stamp the index recorded at refresh
        # time so the append is detectable on 1s-granularity filesystems.
        st = fpath.stat()
        os.utime(fpath, (st.st_atime, st.st_mtime + 1.1))
        idx._last_refresh_at = 0.0  # force stale
        assert idx.is_covered() and not idx.is_usable()
        idx.ensure_started()
        idx.request_refresh()
        fresh = idx.wait_fresh(5.0)
        rows = idx.search_rows(["deltawaitneedle"], limit=5)
    finally:
        # shutdown() signals the worker to stop; reset_for_test() waits for a
        # clean slate (_stop cleared, _worker_started=False) so no worker leaks
        # into the timeout test below.
        idx.shutdown()
        idx.reset_for_test()
        _restore_idx_roots(token)
    ok = fresh and len(rows) >= 1
    print(f"{OK if ok else FAIL} wait_fresh serves delta (fresh={fresh}, rows={len(rows)})")
    return ok


def test_idx_wait_fresh_timeout_returns_false() -> bool:
    """wait_fresh with a tiny timeout and no refresh happening returns False."""
    token = _idx_setup_roots()
    try:
        idx.refresh_once()
        # Make it stale and request NO refresh; tiny timeout must give up.
        idx._last_refresh_at = 0.0
        fresh = idx.wait_fresh(0.05)
    finally:
        _restore_idx_roots(token)
    ok = fresh is False
    print(f"{OK if ok else FAIL} wait_fresh timeout -> False (got {fresh})")
    return ok


def test_idx_request_refresh_sets_flag() -> bool:
    token = _idx_setup_roots()
    try:
        idx.request_refresh()
        flag = idx._refresh_requested is True
    finally:
        _restore_idx_roots(token)
    ok = flag
    print(f"{OK if ok else FAIL} request_refresh sets flag (got {flag})")
    return ok


def test_idx_schema_version_correct() -> bool:
    """After a clean refresh schema_ok() is True; corrupting the schema_version
    row makes it False."""
    token = _idx_setup_roots()
    try:
        idx.refresh_once()
        ok_clean = idx.schema_ok()
        # Corrupt the schema_version row via a writer conn.
        conn = idx._writer_connection()
        conn.execute(
            "INSERT INTO native_corpus_state(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("999",),
        )
        conn.commit()
        ok_corrupt = not idx.schema_ok()
    finally:
        _restore_idx_roots(token)
    ok = ok_clean and ok_corrupt
    print(f"{OK if ok else FAIL} schema_ok correct+corrupt "
          f"(clean={ok_clean}, corrupt={ok_corrupt})")
    return ok


def test_idx_schema_not_ok_before_build() -> bool:
    token = _idx_setup_roots()
    try:
        ok = not idx.schema_ok()
    finally:
        _restore_idx_roots(token)
    print(f"{OK if ok else FAIL} schema not ok before build (got {not ok})")
    return ok


def test_idx_preserves_long_text() -> bool:
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        long_text = "longneedle " + ("x" * 20_000) + " longtail"
        _w(claude / encode_cwd("/p") / "s1.jsonl", [_claude_user(long_text, "u1")])
        idx.refresh_once()
        rows = idx.search_rows(["longneedle"], limit=5)
    finally:
        _restore_idx_roots(token)
    ok = len(rows) == 1 and rows[0]["text"] == long_text
    print(f"{OK if ok else FAIL} long indexed text preserved "
          f"(len={len(rows[0]['text']) if rows else 0})")
    return ok


def test_idx_preserves_long_text_tail() -> bool:
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        long_text = "headneedle " + ("y" * 12_000) + " tailneedle"
        _w(claude / encode_cwd("/p") / "s1.jsonl", [_claude_user(long_text, "u1")])
        idx.refresh_once()
        rows = idx.search_rows(["tailneedle"], limit=5)
    finally:
        _restore_idx_roots(token)
    ok = len(rows) == 1 and rows[0]["text"].endswith("tailneedle")
    print(f"{OK if ok else FAIL} long indexed text tail searchable "
          f"(len={len(rows[0]['text']) if rows else 0})")
    return ok


def test_idx_exact_hash_collapse_metadata() -> bool:
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        repeated = "exacthashneedle repeated harness text"
        _w(claude / encode_cwd("/p") / "a.jsonl", [_claude_user(repeated, "u1")])
        _w(claude / encode_cwd("/p") / "b.jsonl", [_claude_user(repeated, "u1")])
        idx.refresh_once()
        rows = idx.search_rows(["exacthashneedle"], limit=10)
        grouped = idx.run_readonly_sql(
            "SELECT norm_text_sha256, COUNT(*) AS n "
            "FROM native_element_fts "
            "WHERE native_element_fts MATCH 'exacthashneedle' "
            "GROUP BY norm_text_sha256"
        )
    finally:
        _restore_idx_roots(token)
    hashes = {row["text_sha256"] for row in rows}
    norm_hashes = {row["norm_text_sha256"] for row in rows}
    ok = (
        len(rows) == 2
        and len(hashes) == 1
        and len(norm_hashes) == 1
        and grouped.get("rows") == [[next(iter(norm_hashes)), 2]]
    )
    print(f"{OK if ok else FAIL} exact text hash collapse metadata "
          f"(rows={len(rows)}, hashes={len(hashes)}, grouped={grouped.get('rows')})")
    return ok


def test_idx_prefix_hash_collapse_metadata() -> bool:
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        shared_prefix = "prefixhashneedle " + ("shared segment " * 400)
        assert len(" ".join(shared_prefix.split())) > 4096
        _w(claude / encode_cwd("/p") / "a.jsonl", [_claude_user(shared_prefix + " unique alpha", "u1")])
        _w(claude / encode_cwd("/p") / "b.jsonl", [_claude_user(shared_prefix + " unique beta", "u1")])
        idx.refresh_once()
        rows = idx.search_rows(["prefixhashneedle"], limit=10)
        grouped = idx.run_readonly_sql(
            "SELECT prefix_4096_sha256, COUNT(*) AS n "
            "FROM native_element_fts "
            "WHERE native_element_fts MATCH 'prefixhashneedle' "
            "GROUP BY prefix_4096_sha256"
        )
    finally:
        _restore_idx_roots(token)
    prefix_hashes = {row["prefix_4096_sha256"] for row in rows}
    norm_hashes = {row["norm_text_sha256"] for row in rows}
    ok = (
        len(rows) == 2
        and len(prefix_hashes) == 1
        and len(norm_hashes) == 2
        and grouped.get("rows") == [[next(iter(prefix_hashes)), 2]]
    )
    print(f"{OK if ok else FAIL} prefix hash collapse metadata "
          f"(rows={len(rows)}, prefix_hashes={len(prefix_hashes)}, "
          f"norm_hashes={len(norm_hashes)}, grouped={grouped.get('rows')})")
    return ok


def test_idx_repeat_projection_exact_and_prefix() -> bool:
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        repeated = " ".join(["repeatprojectionexactneedle exact repeated text"] * 12)
        shared_prefix = "repeatprojectionprefixneedle " + ("shared projection segment " * 360)
        unique_alpha = "\n    alpha tail"
        unique_beta = "\n    beta tail"
        assert len(" ".join(shared_prefix.split())) > 8192
        exact_a = claude / encode_cwd("/p") / "exact-a.jsonl"
        exact_b = claude / encode_cwd("/p") / "exact-b.jsonl"
        prefix_a = claude / encode_cwd("/p") / "prefix-a.jsonl"
        prefix_b = claude / encode_cwd("/p") / "prefix-b.jsonl"
        _w(exact_a, [_claude_user(repeated, "u1")])
        _w(exact_b, [_claude_user(repeated, "u1")])
        _w(prefix_a, [_claude_user(shared_prefix + unique_alpha, "u1")])
        _w(prefix_b, [_claude_user(shared_prefix + unique_beta, "u1")])
        idx.refresh_once()
        exact = idx.run_readonly_sql(
            "SELECT COUNT(*) FROM native_repeat_group WHERE kind = 'exact_text'"
        )
        prefix = idx.run_readonly_sql(
            "SELECT COUNT(*) FROM native_repeat_group "
            "WHERE kind = 'shared_prefix' AND common_norm_prefix_len = 8192"
        )
        best = idx.run_readonly_sql(
            "SELECT COUNT(*) FROM native_element_repeat_best"
        )
        exact_b.unlink()
        prefix_b.unlink()
        idx.refresh_once()
        stale = idx.run_readonly_sql(
            "SELECT COUNT(*) FROM native_repeat_group"
        )
    finally:
        _restore_idx_roots(token)
    exact_count = exact.get("rows", [[0]])[0][0]
    prefix_count = prefix.get("rows", [[0]])[0][0]
    best_count = best.get("rows", [[0]])[0][0]
    stale_count = stale.get("rows", [[0]])[0][0]
    ok = exact_count >= 1 and prefix_count >= 1 and best_count >= 4 and stale_count == 0
    print(f"{OK if ok else FAIL} repeat projection exact+prefix "
          f"(exact={exact_count}, prefix={prefix_count}, best={best_count}, "
          f"stale={stale_count})")
    return ok


def test_idx_raw_index_checked_returns_tuple_at_whitespace_boundary() -> bool:
    result = idx._raw_index_after_normalized_prefix_checked("ab cd", 3)
    ok = result == (3, True)
    print(f"{OK if ok else FAIL} raw index checked tuple at whitespace boundary "
          f"(result={result})")
    return ok


def test_idx_repeat_projection_rebuild_avoids_fts_text_reads() -> bool:
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        shared_prefix = "largeprefixprojectionneedle " + ("shared projection segment " * 190)
        assert len(" ".join(shared_prefix.split())) > 4096
        events = [
            _claude_user(f"{shared_prefix} unique tail {index}", f"u{index}")
            for index in range(502)
        ]
        _w(claude / encode_cwd("/p") / "large-prefix.jsonl", events)
        idx.refresh_once()
        conn = idx._writer_connection()
        idx._reset_repeat_projection(conn)
        idx._state_set(conn, "repeat_projection_status", "stale")
        conn.commit()
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        idx.refresh_once(full=False)
        conn.set_trace_callback(None)
        prefix = idx.run_readonly_sql(
            "SELECT COUNT(*) FROM native_repeat_group "
            "WHERE kind = 'shared_prefix' AND common_norm_prefix_len = 4096"
        )
        best = idx.run_readonly_sql("SELECT COUNT(*) FROM native_element_repeat_best")
    finally:
        try:
            idx._writer_connection().set_trace_callback(None)
        except Exception:
            pass
        _restore_idx_roots(token)
    fts_text_reads = [
        statement for statement in statements
        if "native_element_fts" in statement and statement.lstrip().upper().startswith("SELECT")
    ]
    prefix_count = prefix.get("rows", [[0]])[0][0]
    best_count = best.get("rows", [[0]])[0][0]
    ok = prefix_count >= 1 and best_count >= 502 and not fts_text_reads
    print(f"{OK if ok else FAIL} repeat projection rebuild avoids FTS text reads "
          f"(prefix={prefix_count}, best={best_count}, fts_reads={len(fts_text_reads)})")
    return ok


def test_idx_repeat_projection_incremental_dirty_buckets() -> bool:
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        repeated = " ".join(["incrementalexactneedle exact repeated text"] * 12)
        shared_prefix = "incrementalprefixneedle " + ("shared projection segment " * 360)
        exact_a = claude / encode_cwd("/p") / "exact-a.jsonl"
        exact_b = claude / encode_cwd("/p") / "exact-b.jsonl"
        prefix_a = claude / encode_cwd("/p") / "prefix-a.jsonl"
        prefix_b = claude / encode_cwd("/p") / "prefix-b.jsonl"
        _w(exact_a, [_claude_user(repeated, "u1")])
        _w(exact_b, [_claude_user(repeated, "u1")])
        _w(prefix_a, [_claude_user(shared_prefix + " alpha", "u1")])
        _w(prefix_b, [_claude_user(shared_prefix + " beta", "u1")])
        idx.refresh_once()
        initial = idx.run_readonly_sql("SELECT COUNT(*) FROM native_element_repeat_best")
        _w(exact_b, [_claude_user("incremental exact unique replacement", "u2")])
        prefix_b.unlink()
        idx.refresh_once(full=False)
        exact = idx.run_readonly_sql(
            "SELECT COUNT(*) FROM native_repeat_group WHERE kind = 'exact_text'"
        )
        prefix = idx.run_readonly_sql(
            "SELECT COUNT(*) FROM native_repeat_group WHERE kind = 'shared_prefix'"
        )
        best = idx.run_readonly_sql("SELECT COUNT(*) FROM native_element_repeat_best")
        dirty = idx.run_readonly_sql("SELECT COUNT(*) FROM native_repeat_dirty")
        status = idx.run_readonly_sql(
            "SELECT value FROM native_corpus_state WHERE key = 'repeat_projection_status'"
        )
        stats = idx.run_readonly_sql(
            "SELECT value FROM native_corpus_state WHERE key = 'last_refresh_repeat_projection_json'"
        )
    finally:
        _restore_idx_roots(token)
    initial_best = initial.get("rows", [[0]])[0][0]
    exact_count = exact.get("rows", [[0]])[0][0]
    prefix_count = prefix.get("rows", [[0]])[0][0]
    best_count = best.get("rows", [[0]])[0][0]
    dirty_count = dirty.get("rows", [[0]])[0][0]
    status_value = status.get("rows", [[""]])[0][0]
    stats_value = stats.get("rows", [["{}"]])[0][0]
    ok = (
        initial_best >= 4
        and exact_count == 0
        and prefix_count == 0
        and best_count == 0
        and dirty_count == 0
        and status_value == "ready"
        and "dirty_buckets" in stats_value
    )
    print(f"{OK if ok else FAIL} repeat projection incremental dirty buckets "
          f"(initial={initial_best}, exact={exact_count}, prefix={prefix_count}, "
          f"best={best_count}, dirty={dirty_count}, status={status_value})")
    return ok


def test_idx_indexed_kinds_set() -> bool:
    """Index a file with one element of EACH indexed kind + a tool_result, then
    assert search_rows returns the indexed kinds and NOT tool_result."""
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        _w(claude / encode_cwd("/p") / "kinds.jsonl", [
            _claude_user("kindsneedle prompt", "u1"),
            _claude_assistant_blocks([
                _claude_text_block("kindsneedle reply"),
                _claude_thinking_block("kindsneedle reasoning"),
                _claude_tool_use("Bash", {"command": "kindsneedle cmd"}),
            ], uid="a1"),
            _claude_tool_result("t1", "kindsneedle result bulk"),
        ])
        idx.refresh_once()
        rows = idx.search_rows(["kindsneedle"], limit=50)
    finally:
        _restore_idx_roots(token)
    kinds = {r["element_kind"] for r in rows}
    expected = {"user_prompt", "assistant_text", "reasoning", "tool_call"}
    ok = kinds == expected and "tool_result" not in kinds
    print(f"{OK if ok else FAIL} indexed kinds set real (kinds={kinds})")
    return ok


def test_idx_no_candidates_empty_roots() -> bool:
    token = _idx_setup_roots()
    try:
        # _idx_setup_roots already points at temp dirs; refresh over empty set.
        r = idx.refresh_once()
        covered = idx.is_covered()
    finally:
        _restore_idx_roots(token)
    ok = r["walked"] == 0 and covered
    print(f"{OK if ok else FAIL} empty roots refresh (walked={r['walked']})")
    return ok


# ===========================================================================
# cross-cutting / integration
# ===========================================================================

def test_integration_search_finds_via_filesystem_walk() -> bool:
    """End-to-end: search finds a claude transcript via real Python discovery."""
    _reset_candidates()
    projects = _SCRATCH / "int-projects"
    cwd = "/int/proj"
    sd = projects / encode_cwd(cwd)
    sd.mkdir(parents=True, exist_ok=True)
    _w(sd / "s1.jsonl", [_claude_user("zulifrangible integration test", "u1")])
    _disable_rg()
    orig = _isolate_native_roots(claude=[projects])
    _reset_index()
    try:
        out = nsp.search_native_session_prompts(query="zulifrangible integration")
    finally:
        _restore_native_roots(orig)
        _restore_rg()
        _reset_index()
    texts = {r["text"] for r in out}
    ok = texts == {"zulifrangible integration test"}
    print(f"{OK if ok else FAIL} integration filesystem search (got {texts})")
    return ok


def test_integration_search_finds_codex() -> bool:
    _reset_candidates()
    codex = _SCRATCH / "int-codex"
    codex.mkdir(parents=True, exist_ok=True)
    _w(codex / "rollout-z.jsonl", [
        {"type": "session_meta", "payload": {"cwd": "/z"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "zulifrangible codex e2e"}]}},
    ])
    _disable_rg()
    orig = _isolate_native_roots(claude=[], codex=codex)
    _reset_index()
    try:
        out = nsp.search_native_session_prompts(query="zulifrangible codex")
    finally:
        _restore_native_roots(orig)
        _restore_rg()
        _reset_index()
    texts = {r["text"] for r in out}
    ok = texts == {"zulifrangible codex e2e"}
    print(f"{OK if ok else FAIL} integration codex search (got {texts})")
    return ok


def test_integration_search_finds_gemini() -> bool:
    _reset_candidates()
    groot = _SCRATCH / "int-gemini-tmp"
    chats = groot / encode_cwd("/g") / "chats"
    chats.mkdir(parents=True, exist_ok=True)
    _w(chats / "session-1.jsonl", [
        {"id": "u1", "timestamp": "t", "type": "user", "content": [{"text": "zulifrangible gemini e2e"}]},
    ])
    _disable_rg()
    orig = _isolate_native_roots(claude=[], gemini=groot)
    _reset_index()
    try:
        out = nsp.search_native_session_prompts(query="zulifrangible gemini")
    finally:
        _restore_native_roots(orig)
        _restore_rg()
        _reset_index()
    texts = {r["text"] for r in out}
    ok = texts == {"zulifrangible gemini e2e"}
    print(f"{OK if ok else FAIL} integration gemini search (got {texts})")
    return ok


def test_integration_index_fastpath_serves() -> bool:
    """Index built + fresh -> query served from FTS even with rg disabled.
    Both the miner roots (search discovery) and ``nsp._native_roots`` (the
    index stat-walk) must point at the SAME temp dir or the index walks a
    different corpus than the search."""
    _reset_candidates()
    projects = _SCRATCH / "fp-projects"
    sd = projects / encode_cwd("/fp")
    sd.mkdir(parents=True, exist_ok=True)
    _w(sd / "s1.jsonl", [_claude_user("zulifrangible fastpath e2e", "u1")])
    _disable_rg()
    orig = _isolate_native_roots(claude=[projects])
    _reset_index()
    try:
        idx.refresh_once()
        assert idx.is_usable()
        out = nsp.search_in_native_session_transcript(query="zulifrangible fastpath")
    finally:
        _restore_native_roots(orig)
        _restore_rg()
        _reset_index()
    texts = {r["text"] for r in out}
    ok = "zulifrangible fastpath e2e" in texts
    print(f"{OK if ok else FAIL} integration index fastpath (got {texts})")
    return ok


def test_integration_codex_env_context_dropped_in_search() -> bool:
    _reset_candidates()
    codex = _SCRATCH / "env-codex"
    codex.mkdir(parents=True, exist_ok=True)
    _w(codex / "rollout-e.jsonl", [
        {"type": "session_meta", "payload": {"cwd": "/e"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "<environment_context>cwd data"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "zulifrangible real prompt"}]}},
    ])
    _disable_rg()
    orig = _isolate_native_roots(claude=[], codex=codex)
    _reset_index()
    try:
        out = nsp.search_native_session_prompts(query="zulifrangible")
    finally:
        _restore_native_roots(orig)
        _restore_rg()
        _reset_index()
    texts = {r["text"] for r in out}
    ok = texts == {"zulifrangible real prompt"} and not any("environment_context" in t for t in texts)
    print(f"{OK if ok else FAIL} integration env-context dropped in search (got {texts})")
    return ok


def test_integration_multi_format_dispatch() -> bool:
    """All three formats discovered and dispatched correctly in one search."""
    _reset_candidates()
    projects = _SCRATCH / "multi-claude"
    sd = projects / encode_cwd("/mc")
    sd.mkdir(parents=True, exist_ok=True)
    _w(sd / "c.jsonl", [_claude_user("multiformat needle claude", "u1")])

    codex = _SCRATCH / "multi-codex"
    codex.mkdir(parents=True, exist_ok=True)
    _w(codex / "rollout-m.jsonl", [
        {"type": "session_meta", "payload": {"cwd": "/mc"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "multiformat needle codex"}]}},
    ])

    groot = _SCRATCH / "multi-gemini-tmp"
    gchats = groot / encode_cwd("/mg") / "chats"
    gchats.mkdir(parents=True, exist_ok=True)
    _w(gchats / "session-m.jsonl", [
        {"id": "u", "type": "user", "content": [{"text": "multiformat needle gemini"}]},
    ])

    _disable_rg()
    orig = _isolate_native_roots(claude=[projects], codex=codex, gemini=groot)
    _reset_index()
    try:
        out = nsp.search_native_session_prompts(query="multiformat needle")
    finally:
        _restore_native_roots(orig)
        _restore_rg()
        _reset_index()
    texts = {r["text"] for r in out}
    ok = texts == {"multiformat needle claude", "multiformat needle codex", "multiformat needle gemini"}
    print(f"{OK if ok else FAIL} integration multi-format dispatch (got {texts})")
    return ok


def test_integration_max_matches_cap() -> bool:
    _reset_candidates()
    items = []
    for i in range(5):
        items.append(_candidate_from_prompts(f"s{i}", "/p", [(f"capneedle item {i}", "2024-01-01")]))
    _patch_candidates(items)
    _disable_rg()
    try:
        out = nsp.search_native_session_prompts(query="capneedle", max_matches=2)
    finally:
        _reset_candidates()
        _restore_rg()
    ok = len(out) == 2
    print(f"{OK if ok else FAIL} integration max_matches cap (got {len(out)})")
    return ok


def test_integration_unmatched_query_returns_empty() -> bool:
    _reset_candidates()
    projects = _SCRATCH / "unmatched-projects"
    sd = projects / encode_cwd("/u")
    sd.mkdir(parents=True, exist_ok=True)
    _w(sd / "s1.jsonl", [_claude_user("completely different content", "u1")])
    _disable_rg()
    orig = _isolate_native_roots(claude=[projects])
    _reset_index()
    try:
        out = nsp.search_native_session_prompts(query="zzznomatchzzz")
    finally:
        _restore_native_roots(orig)
        _restore_rg()
        _reset_index()
    ok = out == []
    print(f"{OK if ok else FAIL} integration unmatched query -> empty (got {out})")
    return ok


# ===========================================================================
# gap coverage — recall divergence, concurrency, error containment, encoding
# ===========================================================================

def test_gap_tool_result_recall_divergence() -> bool:
    """Documented design tradeoff: the lean index drops tool_result but the
    rg/python fallback indexes it → recall diverges. A needle appearing ONLY in
    a tool_result block is found by the fallback path but NOT the index fast
    path. Two clear assertions lock both sides of the contract."""
    _reset_candidates()
    projects = _SCRATCH / "trdiv-projects"
    sd = projects / encode_cwd("/trdiv")
    sd.mkdir(parents=True, exist_ok=True)
    _w(sd / "s1.jsonl", [
        _claude_assistant_blocks([
            _claude_text_block("clean text no needle here"),
        ], uid="a1"),
        _claude_tool_result("t1", "toolresultneedle only in tool output"),
    ])
    # --- Fallback path (rg disabled, index not usable) FINDS the needle ---
    _disable_rg()
    orig = _isolate_native_roots(claude=[projects])
    _reset_index()
    try:
        out_fallback = nsp.search_in_native_session_transcript(query="toolresultneedle")
    finally:
        _restore_native_roots(orig)
        _restore_rg()
    # --- Index fast path does NOT find it (lean drop) ---
    _patch_nsp_roots(claude=[projects])
    _reset_index()
    try:
        idx.refresh_once()
        assert idx.is_usable()
        rows = idx.search_rows(["toolresultneedle"], limit=10)
    finally:
        _restore_nsp_roots()
        _reset_index()
    found_fallback = any("toolresultneedle" in r["text"] for r in out_fallback)
    found_index = len(rows) > 0
    ok = found_fallback and not found_index
    print(f"{OK if ok else FAIL} tool_result recall divergence "
          f"(fallback={found_fallback}, index={found_index})")
    return ok


def test_gap_concurrent_reads_during_refresh() -> bool:
    """N reader threads call search_rows/match_paths in a loop while the main
    thread calls refresh_once repeatedly. WAL + readonly-vs-writer must keep
    this exception-free AND a reader must converge to newly-committed writes:
    partway through the run the writer appends a fresh unique needle and at
    least one reader must eventually observe it."""
    import threading
    token = _idx_setup_roots()
    claude = _IDX_CLAUDE
    f = claude / encode_cwd("/p") / "conv.jsonl"
    _w(f, [_claude_user("convneedle stable", "u1")])
    idx.refresh_once()
    # Unique needle appended AFTER the first refresh so readers must converge.
    new_token = f"convnewneedle{_next_seq()}"
    errors: list[Exception] = []
    saw_new = threading.Event()
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                idx.search_rows(["convneedle"], limit=5)
                idx.match_paths(["convneedle"], set())
                if idx.search_rows([new_token], limit=5):
                    saw_new.set()
            except Exception as e:
                errors.append(e)
                return

    threads = [threading.Thread(target=reader) for _ in range(4)]
    try:
        for th in threads:
            th.start()
        # Append the new needle after the corpus is already covered + being
        # polled, so convergence is the thing under test (not pre-seeding).
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_claude_user(f"{new_token} appended", "u2")) + "\n")
        for _ in range(20):
            idx.refresh_once()
            if saw_new.is_set():
                break
        stop.set()
        for th in threads:
            th.join(timeout=5.0)
    finally:
        _restore_idx_roots(token)
    rows = idx.search_rows(["convneedle"], limit=5)
    ok = not errors and len(rows) >= 1 and saw_new.is_set()
    print(f"{OK if ok else FAIL} concurrent reads during refresh "
          f"(errors={len(errors)}, rows={len(rows)}, converged={saw_new.is_set()})")
    return ok


def test_gap_match_elements_contains_non_oserror() -> bool:
    """A candidate whose parse_elements raises a non-OSError (e.g. RuntimeError)
    must NOT abort the whole search — _match_elements contains it and siblings
    still return."""
    class _RaisingCandidate(NativeCandidate):
        def parse_elements(self):
            raise RuntimeError("boom")

    good = _candidate_from_prompts("sGood", "/p", [("nonoserrorneedle good", "2024-01-01")])
    bad = _RaisingCandidate(key="bad", sid="sBad", cwd="/p", data={},
                            transcript=_SCRATCH / "nope.jsonl", mtime=0.0, format="claude")
    _patch_candidates([bad, good])
    try:
        out = nsp.search_in_native_session_transcript(query="nonoserrorneedle")
    finally:
        _reset_candidates()
    texts = {r["text"] for r in out}
    ok = texts == {"nonoserrorneedle good"}
    print(f"{OK if ok else FAIL} _match_elements contains non-OSError (got {texts})")
    return ok


def test_gap_index_cwd_underscore_dash_match() -> bool:
    """Index path: a file under cwd with an underscore (/proj_x) matches a query
    cwd using the dash form (/proj-x) via encode_cwd equivalence. Mirrors
    test_search_cwd_filter_encoded_match but for the index path."""
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        # encode_cwd maps both /proj_x and /proj-x to the SAME token, so the
        # file is physically stored under encode_cwd("/proj_x").
        _w(claude / encode_cwd("/proj_x") / "s1.jsonl",
           [_claude_user("unddashneedle here", "u1")])
        idx.refresh_once()
        hits = idx.match_paths(["unddashneedle"], {"/proj-x"}) or []
    finally:
        _restore_idx_roots(token)
    sids = {Path(p).stem for p, _ in hits}
    ok = sids == {"s1"}
    print(f"{OK if ok else FAIL} index cwd underscore/dash match (got {sids})")
    return ok


def test_gap_rg_fallback_end_to_end() -> bool:
    """rg ENABLED (real _rg_filter), index reset/not-covered: the query goes
    through rg → _candidate_from_match → cwd-filter and finds the needle. Tiny
    temp root, unique needle, keeps it fast."""
    if not shutil.which("rg"):
        print(f"{OK} rg-fallback-e2e skipped (rg not installed)")
        return True
    _reset_candidates()
    _restore_rg()  # ensure rg is real, not disabled
    projects = _SCRATCH / "rgfb-projects"
    sd = projects / encode_cwd("/rgfb")
    sd.mkdir(parents=True, exist_ok=True)
    _w(sd / "s1.jsonl", [_claude_user("rgfbneedle unique token here", "u1")])
    orig = _isolate_native_roots(claude=[projects])
    _reset_index()
    try:
        out = nsp.search_in_native_session_transcript(query="rgfbneedle")
    finally:
        _restore_native_roots(orig)
        _reset_index()
    texts = {r["text"] for r in out}
    ok = texts == {"rgfbneedle unique token here"}
    print(f"{OK if ok else FAIL} rg fallback end-to-end (got {texts})")
    return ok


def test_gap_wal_inode_staleness_after_reset() -> bool:
    """Build index, read, reset_for_test, write DIFFERENT content, refresh again,
    assert a fresh read sees the NEW data (not stale). Exercises
    _close_readonly_connection on reset."""
    token = _idx_setup_roots()
    try:
        claude = _IDX_CLAUDE
        f = claude / encode_cwd("/p") / "wal.jsonl"
        _w(f, [_claude_user("walneedle old content", "u1")])
        idx.refresh_once()
        assert idx.search_rows(["walneedle"], limit=5)
        # Reset drops the DB + closes readonly conns (new inode on rebuild).
        idx.reset_for_test()
        _w(f, [_claude_user("walneedle new content", "u2")])
        idx.refresh_once()
        rows = idx.search_rows(["walneedle"], limit=5)
    finally:
        _restore_idx_roots(token)
    texts = {r["text"] for r in rows}
    ok = texts == {"walneedle new content"}
    print(f"{OK if ok else FAIL} WAL/inode staleness after reset (got {texts})")
    return ok


def test_gap_fts_injection_harmless() -> bool:
    """Belt-and-suspenders: a token that looks like an FTS operator or quote
    (\", OR, ';) is harmless — _query_tokens restricts to [a-z0-9]+ and tokens
    are quoted. Assert no exception and correct results."""
    _reset_candidates()
    projects = _SCRATCH / "inj-projects"
    sd = projects / encode_cwd("/inj")
    sd.mkdir(parents=True, exist_ok=True)
    _w(sd / "s1.jsonl", [_claude_user("injneedle normal text", "u1")])
    _disable_rg()
    orig = _isolate_native_roots(claude=[projects])
    _reset_index()
    try:
        # The query injection strings are stripped by _query_tokens; what
        # remains is the real token "injneedle".
        out = nsp.search_native_session_prompts(query='injneedle " OR \';--')
        # And a pure-injection query (no alnum token) yields empty cleanly.
        out_empty = nsp.search_native_session_prompts(query='" OR \';--')
    finally:
        _restore_native_roots(orig)
        _restore_rg()
        _reset_index()
    texts = {r["text"] for r in out}
    ok = texts == {"injneedle normal text"} and out_empty == []
    print(f"{OK if ok else FAIL} FTS injection harmless (got {texts})")
    return ok


# ===========================================================================
# runner
# ===========================================================================

def main_run() -> int:
    tests = [
        # native_session_miner — claude parser
        test_claude_parse_user_prompt,
        test_claude_parse_drops_tool_result_user_turn,
        test_claude_parse_drops_sidechain,
        test_claude_parse_drops_meta,
        test_claude_parse_drops_command_tags,
        test_claude_parse_drops_caveat,
        test_claude_parse_assistant_text,
        test_claude_parse_keeps_assistant_with_edit_tool_only,
        test_claude_parse_drops_assistant_no_text_no_edit,
        test_claude_parse_malformed_json_line_skipped,
        test_claude_parse_empty_file,
        test_claude_parse_missing_file_returns_none,
        test_claude_parse_user_text_in_list_blocks,
        test_claude_events_by_msg_id_has_agent_message,
        # codex parser
        test_codex_parse_user_message,
        test_codex_parse_drops_environment_context,
        test_codex_parse_drops_user_instructions,
        test_codex_parse_drops_assistant_in_parse,
        test_codex_parse_string_content,
        test_codex_first_cwd_extracts,
        test_codex_first_cwd_missing,
        test_codex_first_cwd_invalid_json,
        # gemini parser
        test_gemini_parse_user_turn,
        test_gemini_parse_gemini_turn_is_assistant,
        test_gemini_parse_drops_metadata_line,
        test_gemini_parse_drops_set_update_line,
        test_gemini_parse_string_content,
        test_gemini_parse_empty_content_dropped,
        # claude element extractor
        test_claude_elements_user_prompt,
        test_claude_elements_command_tag,
        test_claude_elements_bash_input_tag,
        test_claude_elements_system_reminder_is_meta,
        test_claude_elements_assistant_text,
        test_claude_elements_reasoning,
        test_claude_elements_tool_call,
        test_claude_elements_tool_result,
        test_claude_elements_drops_sidechain_and_meta,
        test_claude_elements_user_string_content,
        test_claude_elements_malformed_line_skipped,
        # codex element extractor
        test_codex_elements_user_prompt,
        test_codex_elements_environment_context_is_meta,
        test_codex_elements_assistant_text,
        test_codex_elements_reasoning,
        test_codex_elements_function_call,
        test_codex_elements_custom_tool_call,
        test_codex_elements_function_call_output,
        test_codex_elements_function_call_output_raw_string,
        # gemini element extractor
        test_gemini_elements_user_prompt,
        test_gemini_elements_assistant_text,
        test_gemini_elements_function_call,
        test_gemini_elements_function_response,
        test_gemini_elements_drops_non_user_gemini,
        # cwd token decode
        test_decode_cwd_token_basic,
        test_decode_cwd_token_empty,
        test_decode_cwd_token_all_dashes,
        test_decode_cwd_token_leading_dashes,
        test_encode_cwd_underscore_becomes_dash,
        test_decode_cwd_token_dash_ambiguous_documented,
        # parse_elements dispatch
        test_parse_elements_dispatch_claude,
        test_parse_elements_dispatch_codex,
        test_parse_elements_dispatch_gemini,
        test_parse_elements_missing_file_returns_empty,
        # discovery
        test_iter_all_claude_only,
        test_iter_all_codex_rollout,
        test_iter_all_gemini_chat,
        test_iter_all_runs_dir,
        test_iter_all_runs_dir_requires_state_json,
        # query tokens
        test_query_tokens_lowercases_and_filters,
        test_query_tokens_drops_single_chars,
        test_query_tokens_drops_all_stopwords,
        test_query_tokens_alphanumeric_only,
        test_token_patterns_whole_word,
        # search core
        test_search_empty_query_returns_empty,
        test_search_stopword_only_returns_empty,
        test_search_whole_word_not_substring,
        test_search_ranking_higher_overlap_wins,
        test_search_cwd_filter_restricts,
        test_search_cwd_filter_encoded_match,
        test_search_is_noise_drops,
        test_search_dedup_identical_text,
        test_search_oldest_first_presentation,
        test_search_empty_ts_sorts_last_deterministically,
        test_search_record_kind_and_source_prompts,
        test_search_transcripts_includes_reply,
        test_search_transcripts_excludes_tool_call,
        test_generalized_search_returns_categories_and_tools,
        test_generalized_search_category_filter_shell,
        test_generalized_search_kind_filter_tool_call,
        # categorizer
        test_cat_prompt,
        test_cat_reply,
        test_cat_reasoning,
        test_cat_command,
        test_cat_meta,
        test_cat_edit_tool,
        test_cat_shell_tool,
        test_cat_read_tool,
        test_cat_search_tool,
        test_cat_subagent_tool,
        test_cat_unknown_tool_other,
        test_cat_tool_name_case_insensitive,
        test_cat_tool_name_slash_normalized,
        test_cat_tool_result_ok,
        test_cat_tool_result_error_traceback,
        test_cat_tool_result_error_failed,
        test_cat_tool_result_error_command_not_found,
        test_cat_unknown_kind_other,
        test_cat_empty_tool_name_other,
        # rg filter + roots
        test_rg_filter_none_when_no_roots,
        test_rg_filter_none_for_empty_tokens,
        test_rg_filter_finds_needle_files,
        test_classify_root_claude,
        test_classify_root_unknown_defaults_claude,
        # native_transcript_index
        test_idx_refresh_marks_covered_and_usable,
        test_idx_indexes_user_prompt,
        test_idx_drops_tool_result_lean,
        test_idx_freshness_reindexes_changed_file,
        test_idx_tombstones_deleted_file,
        test_idx_match_paths_returns_pairs,
        test_idx_match_paths_cwd_filter,
        test_idx_match_paths_broad_returns_none,
        test_idx_match_paths_scan_limit_returns_none,
        test_idx_not_usable_when_empty_tokens,
        test_idx_search_rows_empty_when_not_usable,
        test_idx_search_rows_empty_tokens,
        test_idx_wait_fresh_serves_delta,
        test_idx_wait_fresh_timeout_returns_false,
        test_idx_request_refresh_sets_flag,
        test_idx_schema_version_correct,
        test_idx_schema_not_ok_before_build,
        test_idx_preserves_long_text,
        test_idx_preserves_long_text_tail,
        test_idx_exact_hash_collapse_metadata,
        test_idx_prefix_hash_collapse_metadata,
        test_idx_repeat_projection_exact_and_prefix,
        test_idx_raw_index_checked_returns_tuple_at_whitespace_boundary,
        test_idx_repeat_projection_rebuild_avoids_fts_text_reads,
        test_idx_repeat_projection_incremental_dirty_buckets,
        test_idx_indexed_kinds_set,
        test_idx_no_candidates_empty_roots,
        # cross-cutting / integration
        test_integration_search_finds_via_filesystem_walk,
        test_integration_search_finds_codex,
        test_integration_search_finds_gemini,
        test_integration_index_fastpath_serves,
        test_integration_codex_env_context_dropped_in_search,
        test_integration_multi_format_dispatch,
        test_integration_max_matches_cap,
        test_integration_unmatched_query_returns_empty,
        # gap coverage
        test_gap_tool_result_recall_divergence,
        test_gap_concurrent_reads_during_refresh,
        test_gap_match_elements_contains_non_oserror,
        test_gap_index_cwd_underscore_dash_match,
        test_gap_rg_fallback_end_to_end,
        test_gap_wal_inode_staleness_after_reset,
        test_gap_fts_injection_harmless,
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
    print(f"\n{n_pass}/{n_total} native-search-comprehensive tests passed")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main_run())
