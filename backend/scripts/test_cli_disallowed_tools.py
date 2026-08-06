from __future__ import annotations

import asyncio
import os
import shutil
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-cli-disallowed-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import cli  # noqa: E402
import runner  # noqa: E402


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class FakeBackend(cli.Backend):
    def __init__(self) -> None:
        self.seen = None

    async def send_prompt(self, **kwargs) -> str:
        self.seen = kwargs
        return "turn_complete"


class FakeRenderer(cli.Renderer):
    def handle(self, event: dict) -> None:
        pass


def test_drive_turn_passes_disallowed_tools() -> None:
    backend = FakeBackend()
    session = {"id": "session-1"}
    result = asyncio.run(cli._drive_turn(
        backend=backend,
        renderer=FakeRenderer(),
        prompt="do work",
        session=session,
        model="model",
        cwd="/tmp",
        mode="manager",
        disallowed_tools=["Bash"],
    ))
    assert result == "turn_complete"
    assert backend.seen["disallowed_tools"] == ["Bash"]


def test_drive_turn_passes_disabled_builtin_extensions() -> None:
    backend = FakeBackend()
    session = {"id": "session-1"}
    result = asyncio.run(cli._drive_turn(
        backend=backend,
        renderer=FakeRenderer(),
        prompt="do work",
        session=session,
        model="model",
        cwd="/tmp",
        mode="manager",
        disabled_builtin_extensions=["ofek.testape-internal"],
    ))
    assert result == "turn_complete"
    assert backend.seen["disabled_builtin_extensions"] == ["ofek.testape-internal"]


def test_parse_repeated_disallowed_tool() -> bool:
    old_argv = sys.argv[:]
    try:
        sys.argv = [
            "cli.py",
            "-p",
            "do work",
            "--disallowed-tool",
            "Bash",
            "--disallowed-tool",
            "Write",
        ]
        args = cli._parse_args()
        assert args.disallowed_tools == ["Bash", "Write"]
    finally:
        sys.argv = old_argv


def test_parse_repeated_disabled_builtin_extension() -> bool:
    old_argv = sys.argv[:]
    try:
        sys.argv = [
            "cli.py",
            "-p",
            "do work",
            "--disabled-builtin-extension",
            "ofek.testape-internal",
            "--disabled-builtin-extension",
            "ofek-dev.canvas",
        ]
        args = cli._parse_args()
        assert args.disabled_builtin_extensions == [
            "ofek.testape-internal",
            "ofek-dev.canvas",
        ]
    finally:
        sys.argv = old_argv


def test_parse_node_target() -> bool:
    old_argv = sys.argv[:]
    try:
        sys.argv = ["cli.py", "--node", "lenovo", "--cwd", r"C:\Users\Lenovo\repo"]
        args = cli._parse_args()
        assert args.node_id == "lenovo"
        assert args.cwd == r"C:\Users\Lenovo\repo"
    finally:
        sys.argv = old_argv


def test_tool_success_result_accepts_payload_without_success() -> None:
    result = runner._tool_success_result({
        "session_id": "worker-1",
        "final_message": "done",
        "turn_id": "turn-1",
    })
    assert result["is_error"] is False


def main() -> int:
    tests = [
        ("drive_turn_passes_disallowed_tools", test_drive_turn_passes_disallowed_tools),
        ("drive_turn_passes_disabled_builtin_extensions", test_drive_turn_passes_disabled_builtin_extensions),
        ("parse_repeated_disallowed_tool", test_parse_repeated_disallowed_tool),
        ("parse_repeated_disabled_builtin_extension", test_parse_repeated_disabled_builtin_extension),
        ("parse_node_target", test_parse_node_target),
        ("tool_success_result_accepts_payload_without_success", test_tool_success_result_accepts_payload_without_success),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"{FAIL} {name}: {exc}")
            continue
        except Exception as exc:
            failed += 1
            print(f"{FAIL} {name}: {type(exc).__name__}: {exc}")
            continue
        print(f"{PASS} {name}")
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
