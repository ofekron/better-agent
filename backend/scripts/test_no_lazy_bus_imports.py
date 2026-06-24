"""Regression test: ban lazy `from event_bus import bus` (or `BusEvent`)
inside function/method bodies in production backend modules.

Per A1's spec: the event bus is the single notification spine. Lazy
inline imports of `bus` from inside hot paths are a leftover from the
days when the bus didn't exist and importing it would have caused a
cycle — `event_bus.py` has zero project imports today, so any module
can import it at the top.

Lazy bus imports are bad for two reasons:
  1. They make the dependency invisible to a casual reader of the
     module's imports — you have to grep the function bodies to see
     who depends on the bus.
  2. They pay a per-call dict-lookup overhead on hot paths (every
     `set_agent_sid`, every `persist_and_dispatch_raw`).

Exemptions:
  - `backend/scripts/` — test scripts can do whatever they want.
  - `__pycache__/` — bytecode dirs.
  - The `event_bus` module itself.
  - `event_bus_subscribers.py` and `user_msg_lifecycle.py` already
    import at the top (this test confirms they stay that way too —
    the rule is "top-level OK, lazy not OK"; we only flag the lazy form).

Run with:
    cd backend && .venv/bin/python scripts/test_no_lazy_bus_imports.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Production backend root (parent of `scripts/`).
BACKEND = Path(__file__).resolve().parent.parent

# Pattern matches `from event_bus import ...` or `import event_bus`
# at any indentation > 0 (i.e. inside a function/method body).
# `^(\s+)` is the indentation; the second group is the import.
_LAZY_BUS_IMPORT = re.compile(
    r"^(\s+)(from event_bus import [^\n]+|import event_bus(?:\s|$))",
)


def _violations_in(path: Path) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = _LAZY_BUS_IMPORT.match(line)
        if m:
            out.append((i, line.rstrip()))
    return out


def _iter_production_files() -> list[Path]:
    files: list[Path] = []
    for p in BACKEND.rglob("*.py"):
        rel = p.relative_to(BACKEND).parts
        # Skip caches.
        if any(part == "__pycache__" for part in rel):
            continue
        if any(part in {".venv", "venv"} for part in rel):
            continue
        # Skip test scripts — they're allowed to do anything.
        if rel and rel[0] == "scripts":
            continue
        # Skip the bus itself.
        if rel and rel[-1] == "event_bus.py":
            continue
        files.append(p)
    return files


def main() -> int:
    violations: list[tuple[Path, list[tuple[int, str]]]] = []
    for f in _iter_production_files():
        v = _violations_in(f)
        if v:
            violations.append((f, v))

    if not violations:
        print("OK — no lazy `event_bus` imports in production backend modules.")
        return 0

    print("FAIL — found lazy `event_bus` imports inside function bodies:")
    print()
    for path, vs in violations:
        rel = path.relative_to(BACKEND.parent)
        for line_no, line in vs:
            print(f"  {rel}:{line_no}: {line}")
    print()
    print(
        "Lift these to top-of-file imports. `event_bus.py` has no "
        "project-internal imports, so it cannot cause a cycle. See "
        "the docstring at the top of this test for rationale.",
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
