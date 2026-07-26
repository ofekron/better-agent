"""A skill and an instruction block describe themselves from what they ship.

No installed manifest declares an entrypoint `description`, so the settings UI
had nothing to show per item. The authoritative text already lives in the
artifact: SKILL.md frontmatter, and an instruction block's opening paragraph.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import extension_descriptions


def _package(tmp: Path) -> Path:
    root = tmp / "pkg"
    (root / "skills" / "deploy").mkdir(parents=True)
    (root / "skills" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Commit the intended changes and push\n  the current branch.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "harness.md").write_text(
        "# Ofek Dev Harness\n\nUse these rules as the personal maintainer harness.\n\n- a bullet that must not be picked up\n",
        encoding="utf-8",
    )
    (root / "docs" / "empty.md").write_text("# Only A Heading\n", encoding="utf-8")
    return root


def test_skill_description_comes_from_skill_md() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _package(Path(tmp))
        got = extension_descriptions.skill_description(root, {"name": "deploy", "path": "skills/deploy"})
        # Frontmatter continuation lines are folded into one caption.
        assert got == "Commit the intended changes and push the current branch.", got


def test_manifest_description_wins_over_the_artifact() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _package(Path(tmp))
        got = extension_descriptions.skill_description(
            root, {"name": "deploy", "path": "skills/deploy", "description": "Author's own words."}
        )
        assert got == "Author's own words.", got


def test_instruction_description_is_the_lead_paragraph() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _package(Path(tmp))
        got = extension_descriptions.instruction_description(root, {"name": "h", "path": "docs/harness.md"})
        assert got == "Use these rules as the personal maintainer harness.", got


def test_absent_text_stays_empty_rather_than_placeheld() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _package(Path(tmp))
        assert extension_descriptions.instruction_description(root, {"path": "docs/empty.md"}) == ""
        assert extension_descriptions.instruction_description(root, {"path": "docs/missing.md"}) == ""
        assert extension_descriptions.skill_description(root, {"path": "skills/missing"}) == ""
        assert extension_descriptions.skill_description(None, {"path": "skills/deploy"}) == ""


def test_paths_cannot_escape_the_extension_package() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _package(Path(tmp))
        outside = Path(tmp) / "secret.md"
        outside.write_text("# x\n\nleaked\n", encoding="utf-8")
        assert extension_descriptions.instruction_description(root, {"path": "../secret.md"}) == ""


def main() -> int:
    for test in (
        test_skill_description_comes_from_skill_md,
        test_manifest_description_wins_over_the_artifact,
        test_instruction_description_is_the_lead_paragraph,
        test_absent_text_stays_empty_rather_than_placeheld,
        test_paths_cannot_escape_the_extension_package,
    ):
        test()
    print("PASS extension descriptions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
