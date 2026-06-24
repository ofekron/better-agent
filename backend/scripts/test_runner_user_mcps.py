"""Runner user-MCP merge: fail loud on malformed mcps.json.

Locks `runner._merge_user_mcps`: valid `{mcpServers: {...}}` content merges
without clobbering BC-internal servers; malformed JSON or a wrong shape
RAISES (the run fails loud with an error complete.json) instead of silently
dropping every user MCP; an empty file is vacuous, not malformed.

Run:
    cd backend && .venv/bin/python scripts/test_runner_user_mcps.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-runner-mcps-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import runner  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond:
        FAILURES.append(msg)


def _write(content: str) -> str:
    f = tempfile.NamedTemporaryFile(
        "w", suffix=".json", dir=_TMP_HOME, delete=False, encoding="utf-8"
    )
    f.write(content)
    f.close()
    return f.name


def t_valid_merge() -> None:
    servers = {"internal": "BC"}
    runner._merge_user_mcps(
        _write('{"mcpServers":{"user-a":{"command":"echo"},"internal":{"command":"evil"}}}'),
        servers,
    )
    check(servers["user-a"] == {"command": "echo"}, "user MCP merged")
    check(servers["internal"] == "BC", "BC-internal MCP never clobbered")


def t_empty_file_is_vacuous() -> None:
    servers: dict = {}
    runner._merge_user_mcps(_write("  \n"), servers)
    check(servers == {}, "empty mcps.json merges nothing and does not raise")


def t_malformed_raises() -> None:
    raised = False
    try:
        runner._merge_user_mcps(_write("{not json"), {})
    except Exception:
        raised = True
    check(raised, "invalid JSON raises (fails the run loud)")

    for content, label in [
        ('{"servers": {}}', "missing mcpServers key"),
        ('{"mcpServers": []}', "non-object mcpServers"),
        ("[1,2]", "top-level list"),
        ('"text"', "top-level string"),
    ]:
        raised = False
        try:
            runner._merge_user_mcps(_write(content), {})
        except ValueError:
            raised = True
        check(raised, f"wrong shape raises ValueError: {label}")


def main() -> int:
    for name, fn in [
        ("valid merge + internal precedence", t_valid_merge),
        ("empty file is vacuous", t_empty_file_is_vacuous),
        ("malformed content raises", t_malformed_raises),
    ]:
        print(f"\n--- {name} ---")
        try:
            fn()
        except Exception as e:
            FAILURES.append(f"{name}: {e!r}")
            import traceback
            traceback.print_exc()
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} assertion(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
