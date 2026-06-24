"""Comprehensive test suite for backend/native_import.py.

Spans 1000+ distinct cases across every unit of the importer:

  A. `_is_user_prompt` truth table
  B. `_extract_text` content shapes
  C. segmentation differential — a reference segmenter is checked
     against `_segment_turns` over the full combinatorial space of
     conversation shapes (block kinds × sequence length). ~1500 cases.
  D. `_derive_title` fallbacks
  E. `_codex_iso` timestamp parsing
  F. claude enumeration matrix (cwd/session/non-jsonl/empty/missing)
  G. codex enumeration matrix (sqlite column variants, multi-thread)
  H. registry set/get/overwrite/persist/corrupt
  I. claude end-to-end ingest matrix through session_manager, with
     idempotency, force, and error cases (~60 scenarios)
  J. codex end-to-end ingest (user msgs dropped by the normalizer →
     single-turn collapse invariant)
  K. background job: single-flight, status transitions, counts

Run with:
    cd backend && .venv/bin/python scripts/test_native_import_comprehensive.py
"""

from __future__ import annotations

import itertools
import json
import os
import sqlite3
import logging
import sys
import tempfile
import uuid
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-native-import-comprehensive-")
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import native_import  # noqa: E402
logging.getLogger(native_import.__name__).setLevel(logging.CRITICAL)  # silence intentional error logs
logging.getLogger("config_store").setLevel(logging.CRITICAL)  # silence provider-removal audit logs
logging.getLogger("keyring").setLevel(logging.CRITICAL)
from session_manager import manager as session_manager  # noqa: E402
import session_store  # noqa: E402
import config_store  # noqa: E402

CLAUDE_HOME = Path(_TMP_HOME) / "claude-home"
os.environ["CLAUDE_CONFIG_DIR"] = str(CLAUDE_HOME)

CASES = {"n": 0}


def case() -> None:
    CASES["n"] += 1


def check(cond, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    case()


# --------------------------------------------------------------------------- #
# Wrapped-event builders (claude-shaped, which is also codex's normalized shape)
# --------------------------------------------------------------------------- #

def _ev(data: dict) -> dict:
    return {"type": "agent_message", "data": data}


def u_text(text: str = "hi") -> dict:
    return _ev({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})


def u_text_str(text: str = "plain") -> dict:
    return _ev({"type": "user", "message": {"role": "user", "content": text}})


def u_toolres(tool_id: str = "tu1") -> dict:
    return _ev({"type": "user", "message": {"role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "out"}]}})


def u_meta() -> dict:
    return _ev({"type": "user", "isMeta": True, "message": {"role": "user",
                "content": [{"type": "text", "text": "meta"}]}})


def u_sidechain() -> dict:
    return _ev({"type": "user", "isSidechain": True, "message": {"role": "user",
                "content": [{"type": "text", "text": "side"}]}})


def a_text(text: str = "ok") -> dict:
    return _ev({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "text", "text": text}]}})


def a_tooluse(name: str = "Bash") -> dict:
    return _ev({"type": "assistant", "message": {"role": "assistant",
                "content": [{"type": "tool_use", "id": "tu1", "name": name, "input": {}}]}})


def a_empty() -> dict:
    return _ev({"type": "assistant", "message": {"role": "assistant", "content": []}})


def sys_line() -> dict:
    return _ev({"type": "system", "message": {"role": "system", "content": "sys"}})


# Reference classifier mirroring `_is_user_prompt`.
def is_boundary(event: dict) -> bool:
    d = event.get("data") or {}
    if d.get("isSidechain") or d.get("isMeta"):
        return False
    if d.get("type") != "user":
        return False
    content = (d.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(isinstance(i, dict) and i.get("type") == "tool_result" for i in content)
    return False


# Reference segmenter mirroring `_segment_turns`.
def ref_segment(events: list[dict]) -> list[dict]:
    turns: list[dict] = []
    for e in events:
        if is_boundary(e):
            turns.append({"prompt": native_import._extract_text(e["data"]), "events": []})
        else:
            if not turns:
                turns.append({"prompt": "", "events": []})
            turns[-1]["events"].append(e)
    return turns


# --------------------------------------------------------------------------- #
# A. _is_user_prompt truth table
# --------------------------------------------------------------------------- #

def test_is_user_prompt() -> None:
    T = native_import._is_user_prompt
    pairs = [
        (u_text(), True), (u_text_str(), True),
        (u_toolres(), False), (u_meta(), False), (u_sidechain(), False),
        (a_text(), False), (a_tooluse(), False), (a_empty(), False), (sys_line(), False),
        (_ev({"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "a"}, {"type": "tool_result", "tool_use_id": "x", "content": ""}]}}), False),
        (_ev({"type": "user", "message": {"role": "user", "content": []}}), True),  # empty user → boundary
        (_ev({"type": "user", "message": {"role": "user", "content": [
            {"type": "image", "source": {}}]}}), True),  # non-tool_result block → boundary
        (_ev({"type": "summary", "isMeta": True, "message": {"role": "user", "content": "s"}}), False),
        (_ev({}), False),  # empty data
    ]
    for ev, expected in pairs:
        check(T(ev["data"]) is expected, f"is_user_prompt wrong for {ev['data']}")


# --------------------------------------------------------------------------- #
# B. _extract_text shapes
# --------------------------------------------------------------------------- #

def test_extract_text() -> None:
    X = native_import._extract_text
    check(X({"message": {"content": "hello"}}) == "hello", "string content")
    check(X({"message": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}}) == "a\nb", "text blocks")
    check(X({"message": {"content": [{"type": "tool_result", "content": "x"}, {"type": "text", "text": "keep"}]}}) == "keep", "skips tool_result")
    check(X({"message": {"content": []}}) == "", "empty list")
    check(X({"message": {"content": [{"type": "image", "source": {}}]}}) == "", "non-text only")
    check(X({}) == "", "no message")
    check(X({"message": {"content": [{"type": "text", "text": "   trim me   "}]}}) == "trim me", "trims")
    check(X({"message": {"content": ["raw-str"]}}) == "raw-str", "raw string item")


# --------------------------------------------------------------------------- #
# C. segmentation differential over the combinatorial space
# --------------------------------------------------------------------------- #

TOKEN_FACTORIES = [u_text, u_text_str, u_toolres, u_meta, u_sidechain, a_text, a_tooluse]


def test_segmentation_differential() -> None:
    """For every sequence of block kinds (length 1..4) the real segmenter
    must match the reference segmenter, and per-turn event counts/order
    must be consistent with the source."""
    seg = native_import._segment_turns
    total = 0
    for length in range(1, 5):
        for combo in itertools.product(TOKEN_FACTORIES, repeat=length):
            events = [f() for f in combo]
            got = seg(events)
            ref = ref_segment(events)
            total += 1
            check(len(got) == len(ref), f"turn count mismatch len={length} combo={combo}")
            for gi, ri in zip(got, ref):
                check(gi.prompt == ri["prompt"], f"prompt mismatch {combo}")
                check(len(gi.events) == len(ri["events"]), f"event count mismatch {combo}")
                # ordering preserved: same identity sequence
                check([id(x) for x in gi.events] == [id(x) for x in ri["events"]], f"order mismatch {combo}")
            # boundary count invariant
            boundaries = sum(1 for e in events if is_boundary(e))
            leading_non_boundary = 0 if (events and is_boundary(events[0])) else (
                1 if any(not is_boundary(e) for e in events) else 0
            )
            check(len(got) == boundaries + leading_non_boundary, f"boundary invariant {combo}")
    # sanity: we exercised a large space
    check(total >= 1000, f"segmentation space too small: {total}")
    print(f"  segmentation differential: {total} sequences")


# --------------------------------------------------------------------------- #
# D. _derive_title
# --------------------------------------------------------------------------- #

def test_derive_title() -> None:
    D = native_import._derive_title
    from native_import import NativeSession, _Turn
    base = NativeSession("p", "claude", "xyz", "/x")
    check(D(base, [_Turn(prompt="hello world")]) == "hello world", "first prompt title")
    check(D(base, []) == "claude session xyz"[:80], "fallback title (truncated id)")
    long = "x" * 200
    check(len(D(base, [_Turn(prompt=long)])) == 80, "title truncated to 80")
    titled = NativeSession("p", "codex", "t", "/x", title="from-db")
    check(D(titled, [_Turn(prompt="ignored")]) == "from-db", "db title wins")
    # first non-empty prompt wins when earlier turns are empty
    check(D(base, [_Turn(prompt=""), _Turn(prompt="second")]) == "second", "skip empty prompt")


# --------------------------------------------------------------------------- #
# E. _codex_iso
# --------------------------------------------------------------------------- #

def test_codex_iso() -> None:
    C = native_import._codex_iso
    check(C(None) == "", "None")
    check(C(0) == "", "zero")
    check(C(-5) == "", "negative")
    check(C("garbage") == "", "non-numeric")
    check(C(1700000000).endswith("Z"), "seconds -> iso")
    check(C(1700000000.5).endswith("Z"), "float seconds -> iso")
    check(isinstance(C(1700000000), str) and len(C(1700000000)) > 0, "non-empty iso")


# --------------------------------------------------------------------------- #
# F. claude enumeration matrix
# --------------------------------------------------------------------------- #

def _make_claude_layout(root: Path, encoded_cwds: dict[str, list[str]]) -> None:
    projects = root / "projects"
    for cwd, sids in encoded_cwds.items():
        d = projects / cwd
        d.mkdir(parents=True, exist_ok=True)
        for sid in sids:
            (d / f"{sid}.jsonl").write_text(
                json.dumps({"type": "user", "uuid": str(uuid.uuid4()),
                            "message": {"role": "user", "content": [{"type": "text", "text": "x"}]},
                            "timestamp": "2026-01-01T00:00:00Z"}) + "\n",
                encoding="utf-8",
            )


def test_enumerate_claude() -> None:
    E = native_import._enumerate_claude
    layouts = [
        ({"proj-a": ["s1"]}, {"s1"}),
        ({"proj-a": ["s1", "s2"]}, {"s1", "s2"}),
        ({"proj-a": ["s1"], "proj-b": ["s2", "s3"]}, {"s1", "s2", "s3"}),
        ({}, set()),
        ({"proj-a": []}, set()),  # cwd dir but no sessions
    ]
    for layout, expected_ids in layouts:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_claude_layout(root, layout)
            provider = {"config_dir": str(root), "kind": "claude"}
            found = E("pid", provider)
            ids = {s.native_id for s in found}
            check(ids == expected_ids, f"claude enum ids {ids} != {expected_ids}")
            for s in found:
                check(s.provider_kind == "claude", "kind claude")
                check(s.jsonl_path.endswith(f"{s.native_id}.jsonl"), "jsonl path")
                check(s.cwd == "", "claude cwd not recoverable")
                check(s.created_at.endswith("Z"), "created_at iso")
                check(s.registry_key == f"claude:{s.native_id}", "registry key")

    # non-jsonl files and nested dirs are ignored
    with tempfile.TemporaryDirectory() as td:
        _make_claude_layout(Path(td), {"p": ["only"]})
        (Path(td) / "projects" / "p" / "notes.txt").write_text("noise")
        (Path(td) / "projects" / "p" / "only.jsonl.tmp").write_text("noise")
        found = native_import._enumerate_claude("pid", {"config_dir": td})
        check({s.native_id for s in found} == {"only"}, "non-jsonl ignored")

    # missing projects dir → empty, no crash
    check(native_import._enumerate_claude("pid", {"config_dir": "/nonexistent-xyz-123"}) == [], "missing dir")

    # config_dir empty → falls back to CLAUDE_CONFIG_DIR env
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = td
        try:
            _make_claude_layout(Path(td), {"envproj": ["e1"]})
            found = native_import._enumerate_claude("pid", {"config_dir": ""})
            check({s.native_id for s in found} == {"e1"}, "env fallback")
        finally:
            os.environ["CLAUDE_CONFIG_DIR"] = str(CLAUDE_HOME) if old is None else old


# --------------------------------------------------------------------------- #
# G. codex enumeration matrix
# --------------------------------------------------------------------------- #

def _make_codex_db(db_path: Path, threads: list[dict], columns: str = "full") -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        if columns == "minimal":
            conn.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT)")
            for t in threads:
                conn.execute("INSERT INTO threads (id, rollout_path) VALUES (?,?)",
                             (t["id"], str(t["rollout_path"])))
        elif columns == "partial":
            conn.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, title TEXT)")
            for t in threads:
                conn.execute("INSERT INTO threads (id, rollout_path, cwd, title) VALUES (?,?,?,?)",
                             (t["id"], str(t["rollout_path"]), t.get("cwd", ""), t.get("title", "")))
        else:
            conn.execute(
                "CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, title TEXT, "
                "created_at INTEGER, first_user_message TEXT)"
            )
            for t in threads:
                conn.execute(
                    "INSERT INTO threads (id,rollout_path,cwd,title,created_at,first_user_message) "
                    "VALUES (?,?,?,?,?,?)",
                    (t["id"], str(t["rollout_path"]), t.get("cwd", ""), t.get("title", ""),
                     t.get("created_at"), t.get("first_user_message", "")),
                )
        conn.commit()
    finally:
        conn.close()


def _rollout_file(td: Path, name: str) -> Path:
    p = td / name
    p.write_text("\n".join([
        json.dumps({"type": "session_meta", "payload": {"model": "gpt"}}),
        json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "hello"}}),
    ]) + "\n", encoding="utf-8")
    return p


def test_enumerate_codex() -> None:
    E = native_import._enumerate_codex
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        r1 = _rollout_file(td, "rollout-a.jsonl")
        r2 = _rollout_file(td, "rollout-b.jsonl")
        missing = td / "rollout-gone.jsonl"  # referenced but absent

        # full schema, multiple threads
        _make_codex_db(td / "state_5.sqlite", [
            {"id": "t1", "rollout_path": r1, "cwd": "/work", "title": "T1", "created_at": 1700000000},
            {"id": "t2", "rollout_path": r2, "cwd": "/other", "title": "", "first_user_message": "hi", "created_at": 0},
            {"id": "t3", "rollout_path": missing, "cwd": "/x"},  # rollout absent → skipped
        ], columns="full")
        found = E("pid", {"config_dir": str(td)})
        by_id = {s.native_id: s for s in found}
        check(set(by_id) == {"t1", "t2"}, f"codex enum ids {set(by_id)}")
        check(by_id["t1"].cwd == "/work", "cwd recovered")
        check(by_id["t1"].title == "T1", "title recovered")
        check(by_id["t2"].title == "hi", "first_user_message fallback title")
        check(by_id["t1"].created_at.endswith("Z"), "created_at iso")
        check(by_id["t2"].created_at == "", "zero created_at → empty")

    # partial schema (no created_at)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        r = _rollout_file(td, "r.jsonl")
        _make_codex_db(td / "state_5.sqlite", [{"id": "p1", "rollout_path": r, "cwd": "/c", "title": "PT"}], "partial")
        found = E("pid", {"config_dir": str(td)})
        check(len(found) == 1 and found[0].title == "PT" and found[0].created_at == "", "partial schema")

    # minimal schema (only id + rollout_path)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        r = _rollout_file(td, "r.jsonl")
        _make_codex_db(td / "state_5.sqlite", [{"id": "m1", "rollout_path": r}], "minimal")
        found = E("pid", {"config_dir": str(td)})
        check(len(found) == 1 and found[0].cwd == "" and found[0].title == "", "minimal schema")

    # legacy sqlite subdir
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        r = _rollout_file(td, "r.jsonl")
        _make_codex_db(td / "sqlite" / "state_5.sqlite", [{"id": "l1", "rollout_path": r}], "minimal")
        found = E("pid", {"config_dir": str(td)})
        check({s.native_id for s in found} == {"l1"}, "legacy sqlite subdir")

    # no db at all
    with tempfile.TemporaryDirectory() as td:
        check(E("pid", {"config_dir": td}) == [], "no codex db")


# --------------------------------------------------------------------------- #
# H. registry
# --------------------------------------------------------------------------- #

def test_registry() -> None:
    native_import._registry_save({})  # start clean
    check(native_import._registry_get("claude:nope") is None, "absent → None")
    native_import._registry_set("claude:a", "root-a")
    check(native_import._registry_get("claude:a") == "root-a", "set/get")
    native_import._registry_set("claude:a", "root-a2")  # overwrite
    check(native_import._registry_get("claude:a") == "root-a2", "overwrite")
    native_import._registry_set("codex:b", "root-b")
    check(native_import.already_imported_keys() == {"claude:a", "codex:b"}, "keys set")

    # persists across reload
    loaded = native_import._registry_load()
    check(loaded == {"claude:a": "root-a2", "codex:b": "root-b"}, "persisted json")

    # corrupt registry file → recovers to empty, no crash
    native_import._registry_path().write_text("{not json", encoding="utf-8")
    check(native_import._registry_load() == {}, "corrupt → empty")
    check(native_import._registry_get("claude:a") is None, "corrupt → None get")
    native_import._registry_save({})


# --------------------------------------------------------------------------- #
# I. claude end-to-end ingest matrix
# --------------------------------------------------------------------------- #

def _new_native(nid: str | None = None, kind: str = "claude", lines: list[str] | None = None,
                cwd: str = "", title: str = "") -> native_import.NativeSession:
    nid = nid or uuid.uuid4().hex[:12]
    if lines is not None:
        d = CLAUDE_HOME / "projects" / f"enc-{nid}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{nid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        jp = str(d / f"{nid}.jsonl")
    else:
        jp = ""
    return native_import.NativeSession(
        provider_id="", provider_kind=kind, native_id=nid, jsonl_path=jp, cwd=cwd, title=title,
    )


def _cuser(text: str, parent: str | None = None) -> str:
    return json.dumps({"type": "user", "uuid": str(uuid.uuid4()),
                       **({"parentUuid": parent} if parent else {}),
                       "timestamp": "2026-01-01T00:00:00Z",
                       "message": {"role": "user", "content": [{"type": "text", "text": text}]}})


def _cassistant(content, parent: str | None = None) -> str:
    return json.dumps({"type": "assistant", "uuid": str(uuid.uuid4()),
                       **({"parentUuid": parent} if parent else {}),
                       "timestamp": "2026-01-02T00:00:00Z",
                       "message": {"role": "assistant", "content": content}})


def _assert_session_invariants(root_id: str) -> None:
    sess = session_manager.get(root_id)
    check(sess is not None, "session exists")
    check(sess["orchestration_mode"] == "native", "mode native")
    check(sess["source"] == "cli", "imported sessions tagged cli origin")
    msgs = sess["messages"]
    check(len(msgs) >= 2, "has messages")
    check(msgs[0]["role"] == "user", "starts with user")
    roles = [m["role"] for m in msgs]
    # strict alternation user/assistant
    check(all(roles[i] == ("user" if i % 2 == 0 else "assistant") for i in range(len(roles))), "alternation")
    for u in [m for m in msgs if m["role"] == "user"]:
        check(bool(u["content"]), "user msg non-empty")
    for a in [m for m in msgs if m["role"] == "assistant"]:
        check(a["isStreaming"] is False, "assistant not streaming")


def test_ingest_claude_matrix() -> None:
    native_import._registry_save({})
    scenarios: list[tuple[str, list[str], int]] = [
        # name, lines, expected_turn_count
        ("single turn", [_cuser("hello"), _cassistant([{"type": "text", "text": "hi"}])], 1),
        ("two turns", [_cuser("q1"), _cassistant([{"type": "text", "text": "a1"}]),
                       _cuser("q2"), _cassistant([{"type": "text", "text": "a2"}])], 2),
        ("tool turn", [_cuser("run"), _cassistant([{"type": "tool_use", "id": "tu", "name": "Bash", "input": {}}]),
                       json.dumps({"type": "user", "uuid": str(uuid.uuid4()), "timestamp": "2026-01-01T00:00:00Z",
                                   "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu", "content": "out"}]}}),
                       _cassistant([{"type": "text", "text": "done"}])], 1),
        ("multi-block assistant", [_cuser("q"), _cassistant([
            {"type": "text", "text": "thinking"}, {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
            {"type": "text", "text": "more"}])], 1),
        ("five turns", [v for pair in [
            (_cuser(f"q{i}"), _cassistant([{"type": "text", "text": f"a{i}"}])) for i in range(5)
        ] for v in pair], 5),
        ("unicode prompt", [_cuser("héllo 世界 🚀"), _cassistant([{"type": "text", "text": "resp"}])], 1),
        ("long prompt", [_cuser("x" * 5000), _cassistant([{"type": "text", "text": "ok"}])], 1),
    ]
    for name, lines, expected_turns in scenarios:
        sess = _new_native(lines=lines)
        root_id = native_import.import_session(sess)
        _assert_session_invariants(root_id)
        loaded = session_manager.get(root_id)
        user_count = sum(1 for m in loaded["messages"] if m["role"] == "user")
        check(user_count == expected_turns, f"[{name}] turns {user_count} != {expected_turns}")
        # idempotent re-import
        before = len(session_store.list_sessions())
        check(native_import.import_session(sess) == root_id, f"[{name}] idempotent root")
        check(len(session_store.list_sessions()) == before, f"[{name}] no dup session")
        # force re-import creates a NEW session
        forced = native_import.import_session(sess, force=True)
        check(forced != root_id, f"[{name}] force creates new")
        _assert_session_invariants(forced)

    # Synthetic-turn cases: content with no user prompt still ingests as
    # exactly one placeholder turn (assistant-only, lone tool_result).
    for name, lines in [
        ("only assistant", [_cassistant([{"type": "text", "text": "lonely"}])]),
        ("only tool_result", [json.dumps({"type": "user", "uuid": str(uuid.uuid4()), "timestamp": "2026-01-01T00:00:00Z",
                                          "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "y"}]}})]),
    ]:
        sess = _new_native(lines=lines)
        root_id = native_import.import_session(sess)
        loaded = session_manager.get(root_id)
        check(len(loaded["messages"]) == 2, f"[{name}] one synthetic turn")
        check(loaded["messages"][0]["content"] == "(imported turn)", f"[{name}] placeholder prompt")

    # truly empty session → ValueError, no session created
    before_empty = len(session_store.list_sessions())
    raised = False
    try:
        native_import.import_session(_new_native(lines=[]))
    except ValueError:
        raised = True
    check(raised, "[empty] should raise ValueError")
    check(len(session_store.list_sessions()) == before_empty, "empty created no session")

    # malformed lines are skipped, valid ones still import
    sess = _new_native(lines=["not json at all", "{ broken", _cuser("real"), _cassistant([{"type": "text", "text": "r"}])])
    root_id = native_import.import_session(sess)
    loaded = session_manager.get(root_id)
    check(sum(1 for m in loaded["messages"] if m["role"] == "user") == 1, "malformed skipped, real imported")


# --------------------------------------------------------------------------- #
# J. codex end-to-end ingest (user messages dropped → single turn collapse)
# --------------------------------------------------------------------------- #

def _new_codex_rollout(td: Path, name: str, agent_texts: list[str]) -> Path:
    p = td / name
    lines = [json.dumps({"type": "session_meta", "payload": {"model": "gpt-5"}})]
    for txt in agent_texts:
        lines.append(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": txt}}))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_ingest_codex() -> None:
    native_import._registry_save({})
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for n_texts in (1, 3, 7):
            rollout = _new_codex_rollout(td, f"r{n_texts}.jsonl", [f"answer {i}" for i in range(n_texts)])
            sess = native_import.NativeSession(
                provider_id="", provider_kind="codex", native_id=f"cx{n_texts}",
                jsonl_path=str(rollout), cwd="/repo", title="",
            )
            root_id = native_import.import_session(sess)
            loaded = session_manager.get(root_id)
            msgs = loaded["messages"]
            # codex drops user prompts → exactly one synthetic turn
            check(len(msgs) == 2, f"codex n={n_texts} one turn (2 msgs), got {len(msgs)}")
            check(msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant", "codex roles")
            asst_events = msgs[1]["events"]
            check(len(asst_events) == n_texts, f"codex n={n_texts} events {len(asst_events)} != {n_texts}")
            check(loaded["cwd"] == "/repo", "codex cwd recovered")
            # idempotent
            check(native_import.import_session(sess) == root_id, "codex idempotent")


# --------------------------------------------------------------------------- #
# K. background job + status
# --------------------------------------------------------------------------- #

def test_job() -> None:
    import time
    native_import._registry_save({})

    # Isolated claude config dir with exactly two sessions, scoped to a
    # dedicated provider so the seeded/default providers can't leak in.
    with tempfile.TemporaryDirectory() as job_home:
        _make_claude_layout(Path(job_home), {"jobproj": ["j1", "j2"]})
        provider = config_store.add_provider({
            "name": "t-claude", "kind": "claude", "mode": "subscription", "config_dir": job_home,
        })
        pid = provider["id"]
        try:
            native_import.start_import([pid])
            final = _poll_done(15)
            check(final["status"] == "done", f"claude job done, got {final['status']}")
            check(final["total"] == 2, f"total 2, got {final['total']}")
            check(final["imported"] == 2, f"imported 2, got {final['imported']}")
            check(final["failed"] == 0, f"no failures, got {final['failed']}")

            # re-run → all skipped (idempotent at job level)
            native_import.start_import([pid])
            final = _poll_done(15)
            check(final["imported"] == 0, f"rerun imports 0, got {final['imported']}")
            check(final["skipped"] == 2, f"rerun skips 2, got {final['skipped']}")

            # scoped enumeration returns only this provider's sessions
            sessions = native_import.enumerate_native_sessions([pid])
            check({s.native_id for s in sessions} == {"j1", "j2"}, "scoped enum")
        finally:
            try:
                config_store.delete_provider(pid)
            except Exception:
                pass


def _poll_done(timeout: float) -> dict:
    import time
    deadline = time.time() + timeout
    final = native_import.get_status()
    while final["status"] == "running" and time.time() < deadline:
        time.sleep(0.05)
        final = native_import.get_status()
    return final


def test_unsupported_providers() -> None:
    agy = config_store.add_provider({"name": "my-agy", "kind": "agy", "mode": "subscription"})
    pid = agy["id"]
    try:
        unsup = native_import.unsupported_providers([pid])
        check(len(unsup) == 1 and unsup[0]["id"] == pid, "agy unsupported (scoped)")
        check(native_import.enumerate_native_sessions([pid]) == [], "agy not enumerated (scoped)")
    finally:
        try:
            config_store.delete_provider(pid)
        except Exception:
            pass


# --------------------------------------------------------------------------- #

def main() -> None:
    test_is_user_prompt()
    test_extract_text()
    test_segmentation_differential()
    test_derive_title()
    test_codex_iso()
    test_enumerate_claude()
    test_enumerate_codex()
    test_registry()
    test_ingest_claude_matrix()
    test_ingest_codex()
    test_unsupported_providers()
    test_job()  # last — touches config_store + background threads
    print(f"OK: native_import comprehensive — {CASES['n']} assertions passed")
    if CASES["n"] < 1000:
        raise AssertionError(f"only {CASES['n']} cases exercised, expected >= 1000")


if __name__ == "__main__":
    main()
