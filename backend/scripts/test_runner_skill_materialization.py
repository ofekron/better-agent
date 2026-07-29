#!/usr/bin/env python3
"""AGY must materialize only the runtime skills pinned in the run artifact."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import paths  # noqa: E402

_TEST_HOME = paths.engage_test_home(tempfile.mkdtemp(prefix="ba-runner-skills-ba-"))
_ORIGINAL_HOME = os.environ.get("HOME")

import runner_agy  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def fresh_home() -> str:
    home = tempfile.mkdtemp(prefix="home-", dir=_TEST_HOME)
    os.environ["HOME"] = home
    return home


def fresh_run_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="run-", dir=_TEST_HOME))


def write_frozen_skill(home: str, name: str) -> Path:
    skill = Path(home) / "frozen-skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        f"---\nname: {name}\ndescription: parity test skill\n---\n# {name}\n",
        encoding="utf-8",
    )
    return skill.parent


def write_ambient_skill(home: str, name: str) -> None:
    skill = Path(home) / ".agents" / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        f"---\nname: {name}\ndescription: ambient skill\n---\n# {name}\n",
        encoding="utf-8",
    )


def materialize(
    run_dir: Path,
    home: str,
    *,
    skill_dirs: dict[str, Path],
) -> dict[str, str]:
    config_root = Path(home) / ".gemini" / "antigravity-cli"
    config_root.mkdir(parents=True, exist_ok=True)
    env = runner_agy._materialize_agy_run_home(
        run_dir,
        {},
        config_root=config_root,
        settings={},
        mcp_servers={},
        skill_dirs=skill_dirs,
    )
    check(env is not None, "agy authentication overlay is always materialized")
    return env


def t_agy_no_capabilities_still_materializes_auth_overlay() -> None:
    home = fresh_home()
    write_ambient_skill(home, "ambient-must-not-leak")
    run_dir = fresh_run_dir()
    env = materialize(run_dir, home, skill_dirs={})
    overlay_home = Path(env["HOME"])
    check(overlay_home == run_dir / "agy-home", "agy overlay is scoped to the run")
    check(
        not (overlay_home / ".agents" / "skills").exists(),
        "empty artifact does not materialize ambient skills",
    )


def t_agy_materializes_frozen_runtime_skill() -> None:
    home = fresh_home()
    write_ambient_skill(home, "parity-agy-skill")
    skill_dir = write_frozen_skill(home, "parity-agy-skill")
    run_dir = fresh_run_dir()
    env = materialize(
        run_dir,
        home,
        skill_dirs={"parity-agy-skill": skill_dir},
    )
    overlay_home = Path(env["HOME"])
    targets = [
        overlay_home / ".gemini" / "antigravity-cli" / "builtin" / "skills"
        / "parity-agy-skill" / "SKILL.md",
        overlay_home / ".agents" / "skills" / "parity-agy-skill" / "SKILL.md",
    ]
    check(targets[0].is_file(), "agy runtime skill lands in antigravity-cli/builtin/skills")
    check(targets[1].is_file(), "agy runtime skill lands in .agents/skills")
    check(
        all("parity test skill" in target.read_text(encoding="utf-8") for target in targets),
        "agy overlays copy the frozen skill instead of same-named ambient state",
    )


def main() -> int:
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("t_") and callable(fn):
                fn()
        print("ALL PASS")
        return 0
    finally:
        if _ORIGINAL_HOME is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = _ORIGINAL_HOME
        shutil.rmtree(_TEST_HOME, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
