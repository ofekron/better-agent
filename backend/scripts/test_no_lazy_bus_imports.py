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

import os
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


# Directory names that never contain project source: virtualenvs,
# vendored dependencies and bytecode caches.
_EXCLUDED_DIRS = {"__pycache__", ".venv", ".venvs", "venv", "venvs", "site-packages"}


def _is_project_source(rel: tuple[str, ...]) -> bool:
    """True when a BACKEND-relative path is a production module to scan."""
    if any(part in _EXCLUDED_DIRS for part in rel):
        return False
    # Test scripts are allowed to do anything.
    if rel[0] == "scripts":
        return False
    # The bus itself is exempt.
    return rel[-1] != "event_bus.py"


def _iter_production_files() -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(BACKEND):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            p = Path(dirpath, name)
            if _is_project_source(p.relative_to(BACKEND).parts):
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


def test_excludes_virtualenvs_and_vendored_packages() -> None:
    excluded = [
        (".venv", "lib", "python3.12", "site-packages", "joblib", "x.py"),
        (".venvs", "default", "lib", "site-packages", "joblib", "x.py"),
        ("venv", "lib", "site-packages", "x.py"),
        ("some", "site-packages", "joblib", "test", "x.py"),
        ("__pycache__", "x.py"),
        ("scripts", "x.py"),
        ("event_bus.py",),
    ]
    for rel in excluded:
        assert not _is_project_source(rel), rel
    assert _is_project_source(("event_bus_subscribers.py",))
    assert _is_project_source(("sub", "module.py"))


def test_scan_covers_only_project_source() -> None:
    files = _iter_production_files()
    assert files
    for p in files:
        parts = p.relative_to(BACKEND).parts
        assert not (set(parts) & _EXCLUDED_DIRS), p


def test_guard_runs_without_decode_errors() -> None:
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
