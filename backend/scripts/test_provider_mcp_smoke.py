#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.request import urlopen


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1.0) as response:
                if response.status < 500:
                    return
        except Exception as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"server did not become ready: {last_error}")


def _panel_path(provider: str) -> str:
    return f"/tmp/better-claude-{provider}-open-file-panel-smoke.txt"


def _prompt(provider: str) -> str:
    path = _panel_path(provider)
    return (
        "This is an integration smoke test for Better Agent tool injection. "
        "You must call the available open-file-panel MCP tool named "
        "open_file_panel exactly once before answering. If you need to search "
        "for the tool, search for open-file-panel. Use mode='panel', path="
        f"{path!r}, start_line=1, end_line=1. After the tool call, reply only "
        "with: done"
    )


async def _run_provider_smoke(provider_name: str, provider_cls, main, session_store, session_manager, port: int) -> None:
    session = session_store.create_session(
        name=f"{provider_name} MCP smoke",
        model="",
        cwd="/tmp",
        orchestration_mode="native",
        source="cli",
        provider_id=f"smoke-{provider_name}",
        browser_harness_enabled=False,
    )
    sid = session["id"]
    queue: asyncio.Queue = asyncio.Queue()
    provider = provider_cls({"id": f"smoke-{provider_name}"})
    run_id = f"smoke-{provider_name}-{uuid.uuid4().hex[:12]}"
    provider.start_run(
        run_id=run_id,
        prompt=_prompt(provider_name),
        cwd="/tmp",
        loop=asyncio.get_running_loop(),
        queue=queue,
        model=None,
        reasoning_effort=None,
        session_id=None,
        mode="native",
        app_session_id=sid,
        backend_url=f"http://127.0.0.1:{port}",
        internal_token=main.coordinator.internal_token,
        browser_harness_enabled=False,
        open_file_panel_enabled=True,
        provider_run_config={},
        capability_contexts=[],
        setting_sources=[],
    )
    run_dir = provider._runs[run_id].run_dir

    complete = None
    events: list[str] = []
    deadline = time.monotonic() + 240.0
    while time.monotonic() < deadline:
        timeout = min(5.0, max(0.1, deadline - time.monotonic()))
        try:
            event = await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            continue
        events.append(event.type)
        if event.type == "complete":
            complete = event.data
            break
    if complete is None:
        raise AssertionError(f"{provider_name}: timed out waiting for complete; events={events}")
    if complete.get("error"):
        raise AssertionError(
            f"{provider_name}: runner error: {complete.get('error')} run_dir={run_dir}"
        )

    record = session_manager.get(sid) or {}
    panels = record.get("open_file_panels") or []
    expected = _panel_path(provider_name)
    if not any(panel.get("path") == expected for panel in panels):
        raise AssertionError(
            f"{provider_name}: open_file_panel was not called for {expected}; "
            f"panels={panels} complete={complete} run_dir={run_dir}"
        )
    print(f"PASS {provider_name}: model called open_file_panel")


async def _main() -> None:
    import uvicorn

    import main
    import session_store
    from session_manager import manager as session_manager
    from provider_codex import CodexProvider
    from provider_gemini import GeminiProvider

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            main.app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="on",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_for_server(f"http://127.0.0.1:{port}/api/auth/needs_setup")
        await _run_provider_smoke("codex", CodexProvider, main, session_store, session_manager, port)
        await _run_provider_smoke("gemini", GeminiProvider, main, session_store, session_manager, port)
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


if __name__ == "__main__":
    home = Path(tempfile.mkdtemp(prefix="bc-provider-mcp-smoke-"))
    os.environ["BETTER_CLAUDE_HOME"] = str(home)
    os.environ["BETTER_AGENT_HOME"] = str(home)
    os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"
    try:
        asyncio.run(_main())
    except Exception:
        print(f"FAILED home preserved at {home}")
        raise
    else:
        shutil.rmtree(home, ignore_errors=True)
