"""ADVERSARIAL PROBE (not a kept test): import REAL native sessions from
this machine's ~/.claude, ~/.codex, ~/.gemini into an isolated temp home
and surface crashes / invariant violations my synthetic fixtures missed.

Run with:
    cd backend && .venv/bin/python scripts/probe_native_import_real.py
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-probe-native-import-")
os.environ["BETTER_AGENT_TEST_MODE"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import native_import  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

SAMPLE = int(os.environ.get("PROBE_SAMPLE", "25"))


def _check_session(root_id: str, native_id: str) -> list[str]:
    """Return list of invariant violations for an imported session."""
    problems: list[str] = []
    sess = session_manager.get(root_id)
    if sess is None:
        return [f"{native_id}: session missing after import"]
    msgs = sess.get("messages") or []
    if not msgs:
        return [f"{native_id}: no messages"]
    roles = [m.get("role") for m in msgs]
    if roles[0] != "user":
        problems.append(f"{native_id}: first role {roles[0]!r} not user")
    # alternation
    for i, r in enumerate(roles):
        expected = "user" if i % 2 == 0 else "assistant"
        if r != expected:
            problems.append(f"{native_id}: non-alternating at {i}: {roles}")
            break
    empty_user = [i for i, m in enumerate(msgs) if m.get("role") == "user" and not (m.get("content") or "").strip()]
    if empty_user:
        problems.append(f"{native_id}: empty user msgs at {empty_user}")
    # assistant msgs with zero events are suspicious (lost content)
    empty_asst = [i for i, m in enumerate(msgs) if m.get("role") == "assistant" and not (m.get("events") or [])]
    if empty_asst:
        problems.append(f"{native_id}: assistant msgs with NO events at idx {empty_asst}")
    # still-streaming assistant is a bug
    streaming = [i for i, m in enumerate(msgs) if m.get("role") == "assistant" and m.get("isStreaming")]
    if streaming:
        problems.append(f"{native_id}: assistant still isStreaming at {streaming}")
    # title sanity
    if not (sess.get("name") or "").strip():
        problems.append(f"{native_id}: empty session name/title")
    return problems


def _run_kind(kind: str, sessions: list[native_import.NativeSession]) -> None:
    sessions = sessions[:SAMPLE]
    print(f"\n=== {kind}: {len(sessions)} real sessions ===")
    crashes = 0
    problems_all: list[str] = []
    for sess in sessions:
        try:
            root_id = native_import.import_session(sess)
            problems_all.extend(_check_session(root_id, sess.registry_key))
        except ValueError as e:
            # "no importable events" is expected for some sessions
            print(f"  SKIP {sess.registry_key}: {e}")
        except Exception as e:
            crashes += 1
            print(f"  CRASH {sess.registry_key}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"  -> {crashes} crashes, {len(problems_all)} invariant violations")
    for p in problems_all:
        print("    !", p)


def main() -> None:
    # claude: real ~/.claude/projects
    real_claude = Path.home() / ".claude" / "projects"
    claude_sessions: list[native_import.NativeSession] = []
    if real_claude.exists():
        for jp in real_claude.glob("*/*.jsonl"):
            claude_sessions.append(native_import.NativeSession(
                provider_id="", provider_kind="claude", native_id=jp.stem, jsonl_path=str(jp),
            ))
    print(f"found {len(claude_sessions)} real claude sessions")
    _run_kind("claude", claude_sessions)

    # agy: real conversations dbs
    real_agy = Path.home() / ".gemini" / "antigravity-cli" / "conversations"
    agy_sessions: list[native_import.NativeSession] = []
    if real_agy.exists():
        for db in real_agy.glob("*.db"):
            agy_sessions.append(native_import.NativeSession(
                provider_id="", provider_kind="agy", native_id=db.stem, jsonl_path=str(db),
            ))
    print(f"found {len(agy_sessions)} real agy sessions")
    _run_kind("agy", agy_sessions)

    # gemini: real tmp chats
    real_gem = Path.home() / ".gemini" / "tmp"
    gem_sessions: list[native_import.NativeSession] = []
    if real_gem.exists():
        for proj in real_gem.iterdir():
            chats = proj / "chats"
            if not chats.is_dir():
                continue
            for jp in chats.glob("session-*.jsonl"):
                sid, _, _ = native_import._gemini_read_meta(jp)
                gem_sessions.append(native_import.NativeSession(
                    provider_id="", provider_kind="gemini", native_id=sid, jsonl_path=str(jp),
                ))
    print(f"found {len(gem_sessions)} real gemini sessions")
    _run_kind("gemini", gem_sessions)

    # codex: real rollout db (skip if absent)
    try:
        from codex_native import codex_state_db_paths
        codex_sessions: list[native_import.NativeSession] = []
        for db in codex_state_db_paths():
            if not db.exists():
                continue
            codex_sessions = native_import._enumerate_codex("", {"config_dir": ""})
            break
        print(f"found {len(codex_sessions)} real codex sessions")
        _run_kind("codex", codex_sessions)
    except Exception as e:
        print(f"codex probe skipped: {e}")


if __name__ == "__main__":
    main()
