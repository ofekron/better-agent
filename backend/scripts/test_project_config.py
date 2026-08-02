"""Coverage for backend/project_config.py — project Claude-config scanner.

Pure path logic exercised against a real tempdir: static config entries
(existent + absent), the .claude/skills tree (md files, md-less dirs,
non-dir entries skipped, sorting), and the .claude/hooks directory
(files only). No state home, no mocking.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from project_config import _file_entry, _iso, scan_project_configs  # noqa: E402


def _names(entries: list[dict]) -> list[str]:
    return [e["name"] for e in entries]


# --- scan: directory validity -----------------------------------------------


def test_scan_returns_empty_when_cwd_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert scan_project_configs(str(missing)) == []


def test_scan_returns_empty_when_cwd_is_a_file(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_text("x")
    assert scan_project_configs(str(target)) == []


# --- scan: static configs ---------------------------------------------------


def test_scan_marks_present_and_absent_static_configs(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("instructions")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}")

    entries = {e["name"]: e for e in scan_project_configs(str(tmp_path))}

    present = entries["CLAUDE.md"]
    assert present["exists"] is True
    assert present["category"] == "instructions"
    assert present["size"] == len("instructions")
    assert present["modified"] is not None
    assert present["path"].endswith("CLAUDE.md")

    absent = entries["Settings (local)"]
    assert absent["exists"] is False
    assert absent["size"] == 0
    assert absent["modified"] is None

    # every static config is represented exactly once
    assert len(_names(list(entries.values()))) == 6


# --- scan: skills -----------------------------------------------------------


def test_scan_lists_skill_markdown_files(tmp_path: Path) -> None:
    skills = tmp_path / ".claude" / "skills"
    (skills / "alpha").mkdir(parents=True)
    (skills / "alpha" / "alpha.md").write_text("a")
    (skills / "alpha" / "README.md").write_text("r")

    entries = scan_project_configs(str(tmp_path))
    skill_entries = [e for e in entries if e["category"] == "skill"]
    assert _names(skill_entries) == [
        "Skill: alpha/alpha.md",
        "Skill: alpha/README.md",
    ]
    assert all(e["exists"] for e in skill_entries)


def test_scan_skill_dir_without_markdown_shows_dir_entry(tmp_path: Path) -> None:
    skills = tmp_path / ".claude" / "skills"
    (skills / "empty").mkdir(parents=True)

    entries = [e for e in scan_project_configs(str(tmp_path)) if e["category"] == "skill"]
    assert _names(entries) == ["Skill: empty"]
    assert entries[0]["description"] == "Skill directory (no .md file found)"


def test_scan_skills_iteration_is_sorted_and_skips_non_dirs(tmp_path: Path) -> None:
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "zeta").mkdir()
    (skills / "zeta" / "zeta.md").write_text("z")
    (skills / "alpha").mkdir()
    (skills / "alpha" / "alpha.md").write_text("a")
    (skills / "stray.md").write_text("not a dir")  # non-dir entry skipped

    entries = [e for e in scan_project_configs(str(tmp_path)) if e["category"] == "skill"]
    assert _names(entries) == [
        "Skill: alpha/alpha.md",
        "Skill: zeta/zeta.md",
    ]


def test_scan_without_skills_dir_emits_no_skill_entries(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("x")
    entries = [e for e in scan_project_configs(str(tmp_path)) if e["category"] == "skill"]
    assert entries == []


# --- scan: hooks ------------------------------------------------------------


def test_scan_lists_hook_files_only(tmp_path: Path) -> None:
    hooks = tmp_path / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre_tool.sh").write_text("#!/bin/sh")
    (hooks / "subdir").mkdir()  # non-file entry skipped
    (hooks / "post_tool.py").write_text("print(1)")

    entries = [e for e in scan_project_configs(str(tmp_path)) if e["category"] == "hook"]
    assert _names(entries) == ["Hook: post_tool.py", "Hook: pre_tool.sh"]
    assert all(e["exists"] for e in entries)


def test_scan_without_hooks_dir_emits_no_hook_entries(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("x")
    entries = [e for e in scan_project_configs(str(tmp_path)) if e["category"] == "hook"]
    assert entries == []


# --- _file_entry / _iso -----------------------------------------------------


def test_file_entry_present_and_absent(tmp_path: Path) -> None:
    target = tmp_path / "f"
    target.write_text("hello")

    present = _file_entry(target, "n", "cat", "desc")
    assert present == {
        "name": "n",
        "path": str(target),
        "category": "cat",
        "description": "desc",
        "exists": True,
        "size": 5,
        "modified": _iso(target.stat().st_mtime),
    }

    missing = _file_entry(tmp_path / "ghost", "n", "cat", "desc")
    assert missing["exists"] is False
    assert missing["size"] == 0
    assert missing["modified"] is None


def test_iso_round_trips_through_fromisoformat() -> None:
    ts = 1_700_000_123.0
    rendered = _iso(ts)
    assert rendered == datetime.fromtimestamp(ts).isoformat()
    assert datetime.fromisoformat(rendered).timestamp() == pytest.approx(ts, abs=1)
