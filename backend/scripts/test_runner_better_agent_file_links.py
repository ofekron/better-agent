#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import _test_home

_test_home.isolate("bc-test-runner-ba-file-links-")

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import runner_better_agent  # noqa: E402


def check(cond: bool, label: str) -> None:
    if not cond:
        raise AssertionError(label)
    print(f"PASS {label}")


def test_read_accepts_bcfile_markdown_link_inside_cwd() -> None:
    with tempfile.TemporaryDirectory() as cwd:
        cwdp = Path(cwd)
        target = cwdp / "linked.txt"
        target.write_text("linked content", encoding="utf-8")
        link = f"[linked.txt](bcfile:{target}?L=1)"

        result = runner_better_agent._tool_read({"file_path": link}, cwdp)

    check("linked content" in result, "Read unwraps bcfile markdown links before confinement")


def test_write_keeps_unwrapped_bcfile_confined_to_cwd() -> None:
    with tempfile.TemporaryDirectory() as cwd:
        cwdp = Path(cwd)
        outside = Path(tempfile.gettempdir()) / "ba-file-link-escape.txt"
        link = f"[escape](bcfile:{outside})"

        result = runner_better_agent._tool_write({"file_path": link, "content": "x"}, cwdp)

    check(result.startswith("Error: path escapes cwd"), "Write still rejects unwrapped paths outside cwd")


def main() -> int:
    test_read_accepts_bcfile_markdown_link_inside_cwd()
    test_write_keeps_unwrapped_bcfile_confined_to_cwd()
    print("OK: runner_better_agent file-link paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
