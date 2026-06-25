"""Streaming provider-CLI install: line-by-line subprocess output is
funneled through a broadcast callback, the run registry tracks state, and
concurrent same-kind calls collapse to the in-flight task.

Run: python3 scripts/test_provider_install_streaming.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402
_test_home.isolate("bc-provider-install-streaming-")

import provider_setup as ps  # noqa: E402


def check(label: str, cond: bool) -> None:
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        raise AssertionError(label)


async def test_streaming_argv_captures_lines() -> None:
    received: list[tuple[str, str]] = []

    async def on_line(stream: str, text: str) -> None:
        received.append((stream, text))

    py = sys.executable
    result = await ps._run_argv_streaming(
        (py, "-c", "import sys; print('hello out'); print('oops', file=sys.stderr)"),
        timeout=10,
        on_line=on_line,
    )
    check("streaming argv exits ok", result["ok"] is True)
    texts = " ".join(t for _, t in received)
    check("stdout line streamed", "hello out" in texts)
    check("stderr line streamed", "oops" in texts)


async def test_run_install_lifecycle() -> None:
    py = sys.executable
    installer = ps.ProviderInstaller(
        kind="mockkind",
        label="Mock",
        command=py,
        install_argv=(py, "-c", "print('installing mock')"),
        verify_argv=(py, "-c", "import sys; sys.exit(0)"),
        prerequisite_argv=(py, "--version"),
    )
    run = ps._new_run(installer)
    events: list[tuple[str, dict]] = []

    async def broadcast(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    await ps._run_install(installer, run, broadcast)

    check("run reached succeeded", run["state"] == "succeeded")
    check("run flagged installed", run["installed"] is True)
    check("run captured install line", any("installing mock" in l["t"] for l in run["lines"]))
    types = [e for e, _ in events]
    check("progress broadcast per line", "provider_install_progress" in types)
    check("finished broadcast once", types.count("provider_install_finished") == 1)
    check("finished payload carries state", events[-1][1].get("state") == "succeeded")


async def test_failed_verify_marks_failed() -> None:
    py = sys.executable
    installer = ps.ProviderInstaller(
        kind="mockfail",
        label="MockFail",
        command="nope",
        install_argv=(py, "-c", "print('x')"),
        verify_argv=(py, "-c", "import sys; sys.exit(2)"),
        prerequisite_argv=(py, "--version"),
    )
    run = ps._new_run(installer)

    async def broadcast(event_type: str, data: dict) -> None:
        return None

    await ps._run_install(installer, run, broadcast)
    check("failed verify -> failed state", run["state"] == "failed")
    check("failed verify -> installed False", run["installed"] is False)


async def test_start_install_concurrent_collapse() -> None:
    py = sys.executable
    installer = ps.ProviderInstaller(
        kind="cctmp",
        label="CCTmp",
        command=py,
        install_argv=(py, "-c", "import time; time.sleep(0.2)"),
        verify_argv=(py, "-c", "import sys; sys.exit(0)"),
        prerequisite_argv=(py, "--version"),
    )
    ps.INSTALLERS["cctmp"] = installer
    try:
        events: list[dict] = []

        async def broadcast(event_type: str, data: dict) -> None:
            events.append({"type": event_type, **data})

        first = await ps.start_install("cctmp", broadcast)
        check("first start is running", first["state"] == "running")
        second = await ps.start_install("cctmp", broadcast)
        check("second start collapses to running", second["state"] == "running")
        # Let the background task finish + drain its broadcasts.
        task = ps._INSTALL_TASKS.get("cctmp")
        if task:
            await task
        final = ps.get_install_runs().get("cctmp")
        check("background task reached succeeded", final is not None and final["state"] == "succeeded")
        # Different kind runs concurrently without waiting on cctmp.
        installer2 = ps.ProviderInstaller(
            kind="cctmp2",
            label="CCTmp2",
            command=py,
            install_argv=(py, "-c", "print('other')"),
            verify_argv=(py, "-c", "import sys; sys.exit(0)"),
            prerequisite_argv=(py, "--version"),
        )
        ps.INSTALLERS["cctmp2"] = installer2
        r2 = await ps.start_install("cctmp2", broadcast)
        check("second kind starts while first finishes independently", r2["state"] in ("running", "succeeded"))
        t2 = ps._INSTALL_TASKS.get("cctmp2")
        if t2:
            await t2
    finally:
        ps.INSTALLERS.pop("cctmp", None)
        ps.INSTALLERS.pop("cctmp2", None)


async def main() -> int:
    await test_streaming_argv_captures_lines()
    await test_run_install_lifecycle()
    await test_failed_verify_marks_failed()
    await test_start_install_concurrent_collapse()
    print("\nAll provider-install-streaming tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
