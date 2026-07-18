#!/usr/bin/env python3
"""Backend-launched requirements CLI modes must exist in this line's extension CLI.

Regression: the backend startup trigger spawned `--extract-manual --background`,
but this checkout's requirements extension CLI has no `--extract-manual` flag
(the manual-requirements git-history miner lives on the dev line). The detached
child died on argparse every backend startup, silently. Lock in that every flag
the backend passes to `_launch_requirements_background` is a real argparse
option of the bundled extension CLI.

Run with:
    cd backend && python scripts/test_requirements_cli_launch_parity.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT.parent / "better-agent-private" / "extensions" / "requirements" / "requirement_analysis" / "cli.py"


def test_backend_launch_flags_exist_in_extension_cli() -> None:
    source = (ROOT / "requirement_context.py").read_text(encoding="utf-8")
    launched_flags: set[str] = set()
    for call in re.findall(r"_launch_requirements_background\(\s*\[(.*?)\]", source, re.S):
        launched_flags.update(re.findall(r"\"(--[a-z0-9-]+)\"", call))
    assert launched_flags, "expected at least one _launch_requirements_background call"

    cli_source = CLI_PATH.read_text(encoding="utf-8")
    cli_flags = set(re.findall(r"add_argument\(\s*\"(--[a-z0-9-]+)\"", cli_source))
    missing = sorted(launched_flags - cli_flags)
    assert not missing, (
        f"backend launches requirements CLI flags missing from {CLI_PATH.name}: {missing}. "
        "Either port the CLI mode into this line's extension or drop the backend launch."
    )


if __name__ == "__main__":
    test_backend_launch_flags_exist_in_extension_cli()
    print("PASS requirements CLI launch parity")
