#!/usr/bin/env python3
"""Runner for the live cross-vendor built-in-MCP suite.

This suite spawns real vendor CLI subprocesses and spends real model quota on
every run, which is why it lives outside `backend/scripts` and is never picked
up by an ordinary test run. Enabling it is the user's call:

    RUN_LLM_TESTS=1 backend/<active-venv>/bin/python \
        needs-user-approval-tests/run_live_mcp_tests.py

`RUN_LLM_TESTS` is the repo's existing single gate for live-LLM tests
(`backend/scripts/live_llm_test_guard.py`); this suite deliberately does not
invent a second one. Narrow the spend with either selector:

    BETTER_AGENT_LIVE_VENDORS=claude,gemini      # only these provider kinds
    BETTER_AGENT_LIVE_SERVERS=ui,capabilities    # only these MCP servers

Everything runs against an isolated `BETTER_AGENT_HOME` tempdir with its own
activated installation profile, so no real session, provider, lock, or
extension state is ever read or written.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
BACKEND = REPO_ROOT / "backend"
SCRIPTS = BACKEND / "scripts"

for path in (str(BACKEND), str(SCRIPTS), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

CASE_MODULES = (
    "test_ui_mcp",
    "test_config_panel_mcp",
    "test_capabilities_mcp",
    "test_communicate_mcp",
    "test_bundled_mcp",
)


def _selector(name: str) -> set[str] | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


def _collect(vendors: set[str] | None, servers: set[str] | None) -> list:
    cases = []
    for module_name in CASE_MODULES:
        cases.extend(importlib.import_module(module_name).cases())
    if vendors is not None:
        cases = [c for c in cases if c.vendor.kind in vendors]
    if servers is not None:
        cases = [c for c in cases if c.server in servers]
    return cases


def _list_cases(home: Path) -> int:
    """Show the matrix without spawning a single agent.

    Runs the same activation the real suite does, because the built-in server
    set is empty until the installation profile is active — listing against an
    inactive profile would report a misleadingly empty matrix.
    """
    import _live_agent
    import _test_installation
    import cli_paths

    real_claude = cli_paths.resolve_cli_binary(
        "claude", respect_installation_profile=False
    )
    _test_installation.activate(home, provider="claude", launcher_path=real_claude)

    cases = _collect(
        _selector("BETTER_AGENT_LIVE_VENDORS"), _selector("BETTER_AGENT_LIVE_SERVERS")
    )
    for case in cases:
        installed = "" if case.vendor.cli_path() else "  (CLI not installed — will skip)"
        print(f"{case.name:<52} model={case.vendor.model}{installed}")
    kinds = sorted({c.vendor.kind for c in cases})
    print(f"\n{len(cases)} cases across {len(kinds)} vendors: {', '.join(kinds)}")
    uncovered = sorted(
        v.kind for v in _live_agent.VENDORS if v.kind not in set(kinds)
    )
    if uncovered:
        print(f"no built-in MCP servers wired, nothing to test: {', '.join(uncovered)}")
    return 0


async def _run(home: Path) -> int:
    import _live_agent
    import _test_installation
    import cli_paths

    # The profile pins one provider executable and `resolve_cli_binary`
    # returns that pinned path, so it must be the REAL binary — a stub would
    # silently no-op every claude run in the suite.
    real_claude = cli_paths.resolve_cli_binary(
        "claude", respect_installation_profile=False
    )
    if not real_claude:
        print("SKIP - claude CLI is required to activate a live installation profile")
        return 0
    _test_installation.activate(home, provider="claude", launcher_path=real_claude)

    cases = _collect(
        _selector("BETTER_AGENT_LIVE_VENDORS"), _selector("BETTER_AGENT_LIVE_SERVERS")
    )
    if not cases:
        print("no cases selected")
        return 0

    cwd = home / "agent-cwd"
    cwd.mkdir(parents=True, exist_ok=True)

    backend = _live_agent.LiveBackend()
    backend.start()

    passed: list[str] = []
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    try:
        for case in cases:
            started = time.monotonic()
            try:
                await case.run(case.vendor, backend, cwd)
            except _live_agent.Skip as skip:
                skipped.append((case.name, str(skip)))
                print(f"SKIP {case.name}: {skip}")
            except Exception:
                failed.append((case.name, traceback.format_exc()))
                print(f"FAIL {case.name}")
            else:
                elapsed = time.monotonic() - started
                passed.append(case.name)
                print(f"PASS {case.name} ({elapsed:.1f}s)")
    finally:
        backend.stop()

    print(
        f"\n{len(passed)} passed, {len(skipped)} skipped, {len(failed)} failed "
        f"of {len(cases)} cases"
    )
    for name, detail in failed:
        print(f"\n--- {name} ---\n{detail}")
    return 1 if failed else 0


def main() -> int:
    import _test_home
    import live_llm_test_guard

    listing = "--list" in sys.argv[1:]
    if not listing and not live_llm_test_guard.require_live_llm_tests(
        "the live built-in-MCP suite"
    ):
        return 0

    home = Path(str(_test_home.isolate(prefix="live-mcp-suite-")))
    os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"
    try:
        status = _list_cases(home) if listing else asyncio.run(_run(home))
    except Exception:
        print(f"\nhome preserved for triage: {home}")
        raise
    if status == 0:
        shutil.rmtree(home, ignore_errors=True)
    else:
        print(f"\nhome preserved for triage: {home}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
