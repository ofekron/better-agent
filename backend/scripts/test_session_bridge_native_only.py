"""Regression: session-bridge MCP is private-extension owned.

Run with:
    cd backend && .venv/bin/python scripts/test_session_bridge_native_only.py
"""

from __future__ import annotations

from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / "runner.py"
EXTENSION_REGISTRY = Path(__file__).resolve().parents[1] / "extension_registry.py"


def main() -> int:
    src = RUNNER.read_text()
    registry = EXTENSION_REGISTRY.read_text()
    checks = [
        (
            "session-bridge" not in registry,
            "public registry has no session-bridge fallback",
        ),
        (
            '"session-bridge" in _active_builtin_mcp_servers' not in src,
            "runner has no in-process session-bridge fallback",
        ),
        (
            'if (app_session_id or "") != _ASK_SINGLETON_ID:',
            "legacy all-user-facing session-bridge gate is absent",
            False,
        ),
    ]
    ok = True
    for item in checks:
        needle, label = item[0], item[1]
        should_exist = item[2] if len(item) > 2 else True
        found = needle if isinstance(needle, bool) else needle in src
        if found is should_exist:
            print(f"PASS {label}")
            continue
        print(f"FAIL {label}")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
