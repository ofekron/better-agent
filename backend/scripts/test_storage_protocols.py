"""A12 regression: per-store `ba_home()` discipline + Protocol surface.

Pins:
  1. `trace_collector` does NOT cache `ba_home() / "traces"` at module
     load time. The path resolves lazily on every call, so a test that
     sets `BETTER_CLAUDE_HOME` after import sees the override. (Was
     the latent `TRACES_DIR = ba_home() / "traces"` bug A12 fixed.)
  2. Every Protocol declared in `backend/stores/protocols.py` is
     importable.
  3. The legacy `trace_collector.TRACES_DIR` symbol is gone — callers
     must use `trace_collector._traces_dir()`.

Run with:
    cd backend && .venv/bin/python scripts/test_storage_protocols.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# This test deliberately exercises the LEGACY BETTER_CLAUDE_HOME override path,
# so an inherited BETTER_AGENT_HOME (which takes precedence) must not shadow it.
os.environ.pop("BETTER_AGENT_HOME", None)


def _check(cond: bool, label: str, failures: list[str]) -> None:
    print(f"  {'OK' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def main() -> int:
    failures: list[str] = []

    # ── 1. `_traces_dir()` honors a mid-process env override ───────
    tmp1 = tempfile.mkdtemp(prefix="bc_a12_traces1_")
    os.environ["BETTER_CLAUDE_HOME"] = tmp1
    import trace_collector
    first = trace_collector._traces_dir()
    _check(
        first == Path(tmp1) / "traces",
        f"_traces_dir() resolves to first BETTER_CLAUDE_HOME ({first})",
        failures,
    )
    tmp2 = tempfile.mkdtemp(prefix="bc_a12_traces2_")
    os.environ["BETTER_CLAUDE_HOME"] = tmp2
    second = trace_collector._traces_dir()
    _check(
        second == Path(tmp2) / "traces",
        f"_traces_dir() re-resolves after env change ({second})",
        failures,
    )
    _check(
        first != second,
        "first and second resolutions differ (lazy resolution works)",
        failures,
    )

    # ── 2. Legacy module-load constants are gone ───────────────────
    _check(
        not hasattr(trace_collector, "TRACES_DIR"),
        "trace_collector.TRACES_DIR removed (no module-load caching)",
        failures,
    )

    # ── 3. Protocols module imports + every Protocol present ───────
    from stores import protocols
    expected = [
        "SessionsStorage", "WorkersStorage", "ApprovalsStorage",
        "NodesStorage", "ProjectsStorage", "ConfigStorage",
        "TracesStorage",
    ]
    for name in expected:
        _check(
            hasattr(protocols, name),
            f"stores.protocols.{name} declared",
            failures,
        )

    # ── 4. trace_cli.py uses the new helper ────────────────────────
    import trace_cli
    cli_source = Path(trace_cli.__file__).read_text(encoding="utf-8")
    _check(
        "trace_collector._traces_dir()" in cli_source,
        "trace_cli.py uses trace_collector._traces_dir()",
        failures,
    )
    _check(
        "trace_collector.TRACES_DIR" not in cli_source,
        "trace_cli.py no longer reads trace_collector.TRACES_DIR",
        failures,
    )

    # Cleanup
    import shutil
    shutil.rmtree(tmp1, ignore_errors=True)
    shutil.rmtree(tmp2, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} FAILURES")
        return 1
    print("\nall A12 checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
