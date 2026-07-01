#!/usr/bin/env python3
"""Extension/runtime skills must materialize natively for the Gemini CLI family
(gemini + agy), not just Claude. Before the fix, `_materialize_gemini_run_home`
and `_materialize_agy_run_home` returned early when a run had no MCP servers and
no `provider_run_config.skills`, so locally/extension-discovered skills never
landed in the per-run overlay the CLI actually reads. These tests fail on that
old behavior and pass once `materialize_runtime_skills` is wired in.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import paths  # noqa: E402

_TEST_HOME = paths.engage_test_home(tempfile.mkdtemp(prefix="ba-runner-skills-ba-"))

import runner_gemini  # noqa: E402
import runner_agy  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def fresh_home() -> str:
    home = tempfile.mkdtemp(prefix="ba-runner-skills-home-")
    os.environ["HOME"] = home
    return home


def write_local_skill(home: str, name: str) -> None:
    skill = Path(home) / ".agents" / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        f"---\nname: {name}\ndescription: parity test skill\n---\n# {name}\n",
        encoding="utf-8",
    )


def t_gemini_no_skills_no_mcp_returns_none() -> None:
    home = fresh_home()
    run_dir = Path(tempfile.mkdtemp(prefix="ba-gemini-run-"))
    env = runner_gemini._materialize_gemini_run_home(run_dir, {}, cwd=home)
    check(env is None, "gemini overlay is skipped when no skills/mcp are configured")


def t_gemini_materializes_runtime_skill() -> None:
    home = fresh_home()
    write_local_skill(home, "parity-gemini-skill")
    run_dir = Path(tempfile.mkdtemp(prefix="ba-gemini-run-"))
    env = runner_gemini._materialize_gemini_run_home(run_dir, {}, cwd=home)
    check(env is not None, "gemini overlay is built when a runtime skill exists")
    overlay = Path(env["GEMINI_CLI_HOME"])
    check(
        (overlay / ".gemini" / "skills" / "parity-gemini-skill" / "SKILL.md").is_file(),
        "gemini runtime skill lands in .gemini/skills",
    )
    check(
        (overlay / ".agents" / "skills" / "parity-gemini-skill" / "SKILL.md").is_file(),
        "gemini runtime skill lands in .agents/skills",
    )


def t_agy_no_skills_no_mcp_returns_none() -> None:
    home = fresh_home()
    run_dir = Path(tempfile.mkdtemp(prefix="ba-agy-run-"))
    env = runner_agy._materialize_agy_run_home(run_dir, {}, cwd=home)
    check(env is None, "agy overlay is skipped when no skills/mcp are configured")


def t_agy_materializes_runtime_skill() -> None:
    home = fresh_home()
    write_local_skill(home, "parity-agy-skill")
    run_dir = Path(tempfile.mkdtemp(prefix="ba-agy-run-"))
    env = runner_agy._materialize_agy_run_home(run_dir, {}, cwd=home)
    check(env is not None, "agy overlay is built when a runtime skill exists")
    overlay_home = Path(env["HOME"])
    check(
        (
            overlay_home / ".gemini" / "antigravity-cli" / "builtin" / "skills"
            / "parity-agy-skill" / "SKILL.md"
        ).is_file(),
        "agy runtime skill lands in antigravity-cli/builtin/skills",
    )
    check(
        (overlay_home / ".agents" / "skills" / "parity-agy-skill" / "SKILL.md").is_file(),
        "agy runtime skill lands in .agents/skills",
    )


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("t_") and callable(fn):
            fn()
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
