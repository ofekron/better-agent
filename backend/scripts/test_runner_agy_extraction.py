"""Regression test for runner_agy answer extraction.

Locked against the four bugs found in session 643e771e (Antigravity / agy
provider, "Gemini 3.5 Flash"), where the assistant's answer was reconstructed
from agy's protobuf-ish step blobs incorrectly:

  (A) the model's internal "thought" was shown INSTEAD of the answer, because
      the answer is fragmented across many blob strings while the thought is
      one long string and the old picker chose max-by-length,
  (B) a CLI-injected `[Message] sender=system ... [Notice] ...` was rendered
      as the assistant's answer,
  (C) the answer was printed twice with a stray marker byte (`...mind.gLet...`),
  (D) on resume, prior turns were re-emitted from the cumulative DB.

Fixtures mirror the real printable strings extracted from
~/.gemini/antigravity-cli/conversations/<sid>.db for that session.

Run with:
    cd backend && .venv/bin/python scripts/test_runner_agy_extraction.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

# Per CLAUDE.md: isolate ~/.better-claude state BEFORE importing any backend module.
import _test_home

_TMP_HOME = _test_home.isolate("bc-test-agy-extraction-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import runner_agy  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_SID = "11111111-0000-0000-0000-000000000001"

# Fragments are separated by a non-printable protobuf field byte so
# _strings_from_blob splits them into separate strings, exactly as agy stores
# them. Reproduced from the real conversation DB for session 643e771e.
_SEP = b"\x02"


def _blob(parts: list[str]) -> bytes:
    return _SEP.join(p.encode("utf-8") for p in parts)


# idx=2 of the real DB: the answer fragmented across many strings, the model
# "thought" as one long string, a bot-id separator, then the clean duplicate.
_IDX2 = _blob([
    "Here are a few ways to rephrase the message, depending on the tone you want:",
    "### Option 1 (Punchy & Direct)",
    "> The future of AI belongs to us. That",
    "s why I open-sourced Better Agent core. Join us on GitHub and let",
    "s build the future we want, without compromises: https://github.com/ofekron/better-agent",
    "### Option 2 (Mission-Driven & Inspiring)",
    "> AI's future should be shaped by the community, not locked behind artificial limits.",
    "### Option 3 (Short & Action-Oriented)",
    "> We should own the future of AI. That's why Better Agent core is open source.",
    "**Defining AI Control**",
    "I'm currently focused on expressing the user's desire to maintain control over the future of AI.",
    "2(bot-83ea2ce1-3ff9-4e6d-b0d4-6d98a83866cdB",
    # clean final copy (no thought):
    "Here are a few ways to rephrase the message, depending on the tone you want:",
    "### Option 1 (Punchy & Direct)",
    "> The future of AI belongs to us. That",
    "s why I open-sourced Better Agent core. Join us on GitHub and let",
    "s build the future we want, without compromises: https://github.com/ofekron/better-agent",
    "### Option 2 (Mission-Driven & Inspiring)",
    "> AI's future should be shaped by the community, not locked behind artificial limits.",
    "### Option 3 (Short & Action-Oriented)",
    "> We should own the future of AI. That's why Better Agent core is open source.",
])

# idx=5: a CLI-injected system notice (sender=system, not a uuid).
_IDX5 = _blob([
    "[Message] timestamp=2026-06-26T20:55:16Z sender=system priority=MESSAGE_PRIORITY_LOW "
    "content=[Notice] All your subagents and background tasks have been stopped due to server restart.",
    "System Message",
    "[Notice] All your subagents and background tasks have been stopped due to server restart.",
])

# idx=6: the answer stored twice in one string, separated by a bot-id, each
# copy prefixed with a 'g' marker byte, trailing backtick.
_IDX6 = (
    "gLet me know if you want me to refine any of those options or if you have a different "
    "direction in mind.2(bot-e87a5968-c735-4342-841b-fda20b69f113BgLet me know if you want "
    "me to refine any of those options or if you have a different direction in mind.`"
).encode("utf-8")


def _strings(blob: bytes) -> list[str]:
    return runner_agy._strings_from_blob(blob)


def main() -> int:
    failures = 0

    # ----- (A) fragmented answer reassembled, thought dropped -----
    text2 = runner_agy._reassemble_answer(_strings(_IDX2))
    ok = (
        text2 is not None
        and "Option 1" in text2
        and "Option 3" in text2
        and "We should own the future of AI" in text2
        and "currently focused" not in text2        # the thought must be gone
        and "Defining AI Control" not in text2       # the thought header too
        and text2.count("Option 1") == 1            # duplicate copy collapsed
        and "That's why" in text2                   # contraction restored, not "Thats"
        and "let's build" in text2                  # contraction restored, not "lets"
    )
    if ok:
        print(f"{PASS}  (A) fragmented answer reassembled, thought dropped, contractions restored")
    else:
        print(f"{FAIL}  (A) wrong reassembly: {text2!r}")
        failures += 1

    # Fragment-join edge cases: number+word must NOT glue; contractions restored.
    if runner_agy._join_fragments(["You can see option 3", "for more details"]) == \
            "You can see option 3 for more details" and \
       runner_agy._join_fragments(["That", "s why", "we let", "s go"]) == \
            "That's why we let's go":
        print(f"{PASS}  (A2) fragment join: no number-glue, contractions restored")
    else:
        print(f"{FAIL}  (A2) fragment join regressed")
        failures += 1

    # ----- (B) CLI system notice dropped, never rendered as the answer -----
    text5 = runner_agy._reassemble_answer(_strings(_IDX5))
    step5 = {"idx": 5, "step_type": 101, "status": 3, "has_subtrajectory": False,
             "strings": _strings(_IDX5), "json": None}
    events5 = runner_agy._ParentMainState("root").events_for_step(step5)
    if text5 is None and events5 == []:
        print(f"{PASS}  (B) CLI system notice dropped (not rendered as answer)")
    else:
        print(f"{FAIL}  (B) system notice leaked: text={text5!r} events={events5!r}")
        failures += 1

    # ----- (C) duplicated answer collapsed, stray marker byte gone -----
    text6 = runner_agy._reassemble_answer(_strings(_IDX6))
    expected6 = (
        "Let me know if you want me to refine any of those options "
        "or if you have a different direction in mind."
    )
    if text6 == expected6:
        print(f"{PASS}  (C) duplicated answer collapsed, stray 'g' marker gone")
    else:
        print(f"{FAIL}  (C) expected {expected6!r}, got {text6!r}")
        failures += 1

    # ----- (D) resume does not re-emit prior turns from the cumulative DB -----
    home = Path(tempfile.mkdtemp(prefix="bc-test-agy-resume-home-"))
    events_path = home / "session_events.jsonl"
    try:
        db = runner_agy._conversation_db(home, _SID)
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db))
        con.execute(
            "create table steps (idx integer, step_type integer, status integer, "
            "has_subtrajectory integer, metadata blob, step_payload blob, render_info blob)"
        )
        con.executemany(
            "insert into steps values (?, ?, 0, 0, ?, ?, ?)",
            [
                (0, 14, b"original user prompt", b"", b""),     # suppressed (user msg)
                (1, 15, b"", _IDX6, b""),                        # one assistant answer
            ],
        )
        con.commit()
        con.close()

        # First (live) run writes the turn's events.
        emitted = {"seen": runner_agy._existing_event_uuids(events_path)}
        runner_agy._stream_new_events(
            events_path, agy_home=home, conversation_id=_SID,
            parent_uuid=_SID, emitted=emitted,
        )
        first = events_path.read_text(encoding="utf-8").splitlines()
        if not first:
            print(f"{FAIL}  (D) first run wrote no events")
            failures += 1
        else:
            # Resume: a fresh process re-seeds the cursor from the existing file.
            emitted2 = {"seen": runner_agy._existing_event_uuids(events_path)}
            runner_agy._stream_new_events(
                events_path, agy_home=home, conversation_id=_SID,
                parent_uuid=_SID, emitted=emitted2,
            )
            second = events_path.read_text(encoding="utf-8").splitlines()
            if second == first:
                print(f"{PASS}  (D) resume did not re-emit prior turn ({len(first)} event(s) unchanged)")
            else:
                print(f"{FAIL}  (D) resume rewrote events: {len(first)} -> {len(second)}")
                failures += 1
    finally:
        import shutil
        shutil.rmtree(home, ignore_errors=True)

    if failures:
        print(f"\nFAILED: {failures} check(s)")
        return 1
    print("\nAll extraction checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
