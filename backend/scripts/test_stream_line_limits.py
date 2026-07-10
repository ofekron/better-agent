"""Regression locks for subprocess line limits (stream_limits.py).

Bug: a live Claude turn died with "JSON message exceeded maximum buffer size
of 1048576 bytes" — runner.py built ClaudeAgentOptions without
max_buffer_size, so the SDK's 1 MiB default killed the run when the CLI
emitted a single stdout line >1 MiB (an image tool_result whose base64 is
embedded twice per line). Locks:

1. runner.py passes max_buffer_size=SUBPROCESS_LINE_LIMIT_BYTES to
   ClaudeAgentOptions (fails before the fix, passes after).
2. Drift lock: every create_subprocess_exec limit= kwarg in backend/runner*.py
   references SUBPROCESS_LINE_LIMIT_BYTES — no per-runner literals.
3. SDK contract canary: SubprocessCLITransport raises on a >1 MiB line at its
   default, and parses the same line with max_buffer_size we now pass.
"""

import ast
import asyncio
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import paths  # noqa: E402

_TEST_HOME = tempfile.mkdtemp(prefix="ba-test-stream-limits-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

from stream_limits import SUBPROCESS_LINE_LIMIT_BYTES  # noqa: E402


def _is_shared_constant(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "SUBPROCESS_LINE_LIMIT_BYTES"


def test_runner_options_carry_max_buffer_size() -> None:
    tree = ast.parse((BACKEND / "runner.py").read_text())
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "ClaudeAgentOptions"
    ]
    assert calls, "runner.py no longer constructs ClaudeAgentOptions"
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        assert "max_buffer_size" in kw, (
            "ClaudeAgentOptions is missing max_buffer_size — the SDK's 1 MiB "
            "default kills turns on large image tool_result lines"
        )
        assert _is_shared_constant(kw["max_buffer_size"]), (
            "max_buffer_size must reference SUBPROCESS_LINE_LIMIT_BYTES"
        )


def _imports_shared_constant(tree: ast.Module) -> bool:
    return any(
        isinstance(n, ast.ImportFrom)
        and n.module == "stream_limits"
        and any(a.name == "SUBPROCESS_LINE_LIMIT_BYTES" for a in n.names)
        for n in tree.body
    )


def test_runner_subprocess_limits_use_shared_constant() -> None:
    offenders = []
    for path in sorted(BACKEND.glob("runner*.py")):
        tree = ast.parse(path.read_text())
        uses_constant = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else ""
            )
            if name not in ("create_subprocess_exec", "create_subprocess_shell"):
                continue
            for kw in node.keywords:
                if kw.arg != "limit":
                    continue
                if _is_shared_constant(kw.value):
                    uses_constant = True
                else:
                    offenders.append(f"{path.name}:{node.lineno}")
        if uses_constant and not _imports_shared_constant(tree):
            offenders.append(f"{path.name}: shadows SUBPROCESS_LINE_LIMIT_BYTES")
    assert not offenders, (
        "subprocess limit= must reference SUBPROCESS_LINE_LIMIT_BYTES "
        "imported from stream_limits, found: "
        f"{offenders}"
    )


_STUB_CLI = """#!/usr/bin/env python3
import json, sys
big = "x" * (2 * 1024 * 1024)
line = json.dumps({
    "type": "assistant",
    "message": {"role": "assistant", "content": [{"type": "text", "text": big}]},
    "session_id": "stub",
})
sys.stdout.write(line + "\\n")
sys.stdout.flush()
"""


async def _read_one_message(max_buffer_size) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    stub_dir = Path(_TEST_HOME) / "stub-cli"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "claude"
    stub.write_text(_STUB_CLI)
    stub.chmod(0o755)

    options = ClaudeAgentOptions(
        cli_path=str(stub),
        max_buffer_size=max_buffer_size,
        env={**os.environ, "PATH": os.environ.get("PATH", "")},
    )
    transport = SubprocessCLITransport(prompt="hi", options=options)
    await transport.connect()
    try:
        async for message in transport.read_messages():
            return message
        raise AssertionError("stub CLI produced no messages")
    finally:
        await transport.close()


def test_sdk_honors_configured_buffer_and_chokes_at_default() -> None:
    from claude_agent_sdk._errors import CLIJSONDecodeError

    message = asyncio.run(_read_one_message(SUBPROCESS_LINE_LIMIT_BYTES))
    assert message.get("type") == "assistant", message.get("type")

    try:
        asyncio.run(_read_one_message(None))
    except CLIJSONDecodeError as exc:
        assert "maximum buffer size" in str(exc), str(exc)
    else:
        raise AssertionError(
            "SDK default no longer chokes on >1MiB lines — the max_buffer_size "
            "override may be removable; re-evaluate stream_limits usage"
        )


def main() -> int:
    test_runner_options_carry_max_buffer_size()
    print("ok: runner.py ClaudeAgentOptions carries max_buffer_size")
    test_runner_subprocess_limits_use_shared_constant()
    print("ok: all runner subprocess limit= kwargs use the shared constant")
    test_sdk_honors_configured_buffer_and_chokes_at_default()
    print("ok: SDK honors configured buffer; default still chokes >1MiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
