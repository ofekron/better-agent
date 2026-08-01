"""Full unit coverage for prompt_templates.py.

``render_prompt`` resolves a named Markdown prompt template from a
process-scoped snapshot of the bundled prompt tree, with two fallbacks for
``requirement_analysis/*`` names: a packaged extension prompt, then a
``sys.path`` scan. It then substitutes params via ``string.Template``.

The existing snapshot tests in ``test_prompt_template_generation.py`` drive
``render_prompt`` inside *subprocesses*, so the parent pytest process never
imports the module and captures no coverage of it. These tests exercise every
resolution and substitution branch in-process and deterministically by
controlling the snapshot table and the extension boundary (the same pattern
``test_extension_package_loader.py`` uses).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home  # noqa: E402

_HOME = _test_home.isolate("ba-prompt-templates-")

import extension_package_loader  # noqa: E402
import prompt_templates  # noqa: E402


# --------------------------------------------------------------------------- #
# render_prompt — snapshot hit + Template substitution
# --------------------------------------------------------------------------- #
def test_render_prompt_substitutes_params(monkeypatch):
    monkeypatch.setattr(
        prompt_templates, "_CORE_PROMPTS", {"t/hello.md": "hi $name ${val}"}
    )
    assert (
        prompt_templates.render_prompt("t/hello.md", {"name": "a", "val": "b"})
        == "hi a b"
    )


def test_render_prompt_without_params_returns_raw_template(monkeypatch):
    monkeypatch.setattr(prompt_templates, "_CORE_PROMPTS", {"t/hello.md": "raw $name"})
    assert prompt_templates.render_prompt("t/hello.md") == "raw $name"
    assert prompt_templates.render_prompt("t/hello.md", None) == "raw $name"


def test_render_prompt_missing_name_raises_file_not_found(monkeypatch):
    monkeypatch.setattr(prompt_templates, "_CORE_PROMPTS", {})
    with pytest.raises(FileNotFoundError):
        prompt_templates.render_prompt("does/not/exist.md")


# --------------------------------------------------------------------------- #
# render_prompt — requirement_analysis/* fallback chain
# --------------------------------------------------------------------------- #
def test_render_prompt_requirement_analysis_uses_extension_path(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt_templates, "_CORE_PROMPTS", {})
    ext_prompt = tmp_path / "req.md"
    ext_prompt.write_text("ext $x", encoding="utf-8")
    # In the isolated home there is no requirements extension, so the real
    # extension_id_for_role returns None; replace prompt_path to hand back a
    # packaged prompt file and exercise the extension-read branch.
    monkeypatch.setattr(
        extension_package_loader, "prompt_path", lambda eid, name: ext_prompt
    )
    assert (
        prompt_templates.render_prompt("requirement_analysis/x.md", {"x": "1"})
        == "ext 1"
    )


def test_render_prompt_requirement_analysis_falls_back_to_sys_path(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(prompt_templates, "_CORE_PROMPTS", {})
    # Real extension lookup returns None in the isolated home (no requirements
    # extension); stage a candidate on sys.path so the fallback loop resolves it.
    staged = tmp_path / "prompts" / "requirement_analysis"
    staged.mkdir(parents=True)
    (staged / "fb.md").write_text("fb $n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert (
        prompt_templates.render_prompt("requirement_analysis/fb.md", {"n": "2"})
        == "fb 2"
    )


def test_render_prompt_requirement_analysis_missing_raises(monkeypatch):
    monkeypatch.setattr(prompt_templates, "_CORE_PROMPTS", {})
    # No requirements extension and no candidate on sys.path -> raise.
    with pytest.raises(FileNotFoundError):
        prompt_templates.render_prompt("requirement_analysis/none.md")


# --------------------------------------------------------------------------- #
# Frozen-bundle (PyInstaller _MEIPASS) path resolution
# --------------------------------------------------------------------------- #
def test_frozen_bundle_paths_when_meipass_set(monkeypatch):
    monkeypatch.setattr(sys, "_MEIPASS", "/frozen/app", raising=False)
    assert prompt_templates._prompts_dir() == Path("/frozen/app/prompts")
    assert prompt_templates._prompt_roots() == [("", Path("/frozen/app/prompts"))]


# --------------------------------------------------------------------------- #
# _snapshot_core_prompts — duplicate-key detection
# --------------------------------------------------------------------------- #
def test_snapshot_core_prompts_detects_key_collision(monkeypatch, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "dup.md").write_text("a", encoding="utf-8")
    (b / "dup.md").write_text("b", encoding="utf-8")
    monkeypatch.setattr(prompt_templates, "_prompt_roots", lambda: [("", a), ("", b)])
    with pytest.raises(ValueError):
        prompt_templates._snapshot_core_prompts()


def test_snapshot_core_prompts_skips_non_file_md_entries(monkeypatch, tmp_path):
    root = tmp_path / "rootskip"
    root.mkdir()
    (root / "real.md").write_text("ok", encoding="utf-8")
    # rglob("*.md") matches by name regardless of type; a directory named *.md
    # is yielded but is_file() is False, so it must be skipped, not read.
    (root / "dir.md").mkdir()
    monkeypatch.setattr(prompt_templates, "_prompt_roots", lambda: [("", root)])
    snap = prompt_templates._snapshot_core_prompts()
    assert dict(snap) == {"real.md": "ok"}


if __name__ == "__main__":
    pytest.main([__file__, "-q", "-p", "no:cacheprovider"])
