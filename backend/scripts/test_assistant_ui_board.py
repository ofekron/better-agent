"""Regression guard: the assistant board analyzer tier is deleted."""

from __future__ import annotations

import sys
from pathlib import Path

import _test_home

_test_home.isolate("bc-test-assistant-analyzer-deleted-")

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import assistant_ui  # noqa: E402


def check(cond: bool, msg: str, failures: list[str]) -> None:
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


def main() -> int:
    failures: list[str] = []
    source = Path(assistant_ui.__file__).read_text(encoding="utf-8")
    main_source = (_BACKEND / "main.py").read_text(encoding="utf-8")
    check(not hasattr(assistant_ui, "AssistantBoardSpec"), "AssistantBoardSpec absent", failures)
    check("AssistantBoardSpec" not in source, "assistant_ui has no analyzer spec source", failures)
    for path in (
        "/api/internal/assistant-ui/classify",
        "/api/internal/assistant-ui/extract-status",
        "/api/internal/assistant-ui/rank",
    ):
        check(path not in main_source, f"{path} endpoint absent", failures)
    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(f" - {failure}")
        return 1
    print("\nPASS: assistant analyzer tier deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
