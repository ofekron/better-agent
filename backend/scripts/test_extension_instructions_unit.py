"""Unit tests for ``extension_instructions``.

Two of the four functions are security-relevant file-IO paths:
``runtime_instruction_blocks`` reads instruction files from an extension's
install root and MUST confine reads to that root (path-traversal guard via
``is_relative_to``); ``_local_project_paths`` walks the project store.
``instruction_items_from_entrypoints`` is the single reader for instruction
sections including the legacy ``provider_capabilities`` alias.

Pins:
  1. ``instruction_items_from_entrypoints``: modern ``instructions`` returned
     verbatim; legacy ``provider_capabilities`` lifted to ``level="global"``
     with non-dict entries dropped; an absent/``None`` ``instructions``
     always falls through to the legacy path and yields a list (never
     ``None``); an empty ``instructions`` list is returned as-is.
  2. ``runtime_instruction_blocks``: empty id / unresolved package -> [];
     valid file -> formatted block; ``providers`` -> scope suffix;
     traversal / missing / empty-content items skipped.
  3. ``normalize_state``: defaults (``global`` defaults True), project map
     coerced to ``str -> bool``.
  4. ``_local_project_paths``: only primary-node projects with a path are
     included, resolved and expanded.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_extension_instructions_unit.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-extension-instructions-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_instructions as ei  # noqa: E402


@pytest.fixture
def stub_extension_store():
    """Inject a fake ``extension_store`` into sys.modules.

    ``runtime_instruction_blocks`` imports it locally, so the patch takes
    effect at call time. ``_install(install_root)`` makes the resolver return
    that path for any record; ``_install(None)`` makes the package report as
    unavailable.
    """
    saved = sys.modules.get("extension_store")

    def _install(install_root):
        sys.modules["extension_store"] = SimpleNamespace(
            runtime_package_root_for_record=lambda record: install_root
        )

    yield _install
    if saved is not None:
        sys.modules["extension_store"] = saved
    else:
        sys.modules.pop("extension_store", None)


@pytest.fixture
def stub_project_store():
    """Inject a fake ``project_store`` returning ``projects``."""
    saved = sys.modules.get("project_store")

    def _install(projects):
        sys.modules["project_store"] = SimpleNamespace(list_projects=lambda: projects)

    yield _install
    if saved is not None:
        sys.modules["project_store"] = saved
    else:
        sys.modules.pop("project_store", None)


# --- instruction_items_from_entrypoints --------------------------------------

def test_instruction_items_returns_modern_instructions_verbatim():
    items = [{"name": "a", "path": "a.md", "level": "turn"}]
    assert ei.instruction_items_from_entrypoints({"instructions": items}) is items


def test_instruction_items_empty_list_is_returned_not_fallen_back_from():
    # An explicit empty list is a real value: must not trigger the legacy alias.
    assert ei.instruction_items_from_entrypoints({"instructions": []}) == []


def test_instruction_items_lifts_legacy_provider_capabilities_to_global():
    legacy = [{"name": "a", "path": "a.md"}, {"name": "b", "path": "b.md"}]
    got = ei.instruction_items_from_entrypoints({"provider_capabilities": legacy})
    assert got == [
        {"name": "a", "path": "a.md", "level": "global"},
        {"name": "b", "path": "b.md", "level": "global"},
    ]


def test_instruction_items_drops_non_dict_legacy_entries():
    got = ei.instruction_items_from_entrypoints(
        {"provider_capabilities": [{"name": "a", "path": "a.md"}, "junk", 7, None]}
    )
    assert got == [{"name": "a", "path": "a.md", "level": "global"}]


def test_instruction_items_absent_instructions_falls_through_to_empty():
    # An absent/None ``instructions`` always takes the legacy path and yields
    # a list (never None), even when provider_capabilities is also absent.
    assert ei.instruction_items_from_entrypoints({}) == []
    assert ei.instruction_items_from_entrypoints({"instructions": None}) == []


# --- runtime_instruction_blocks ----------------------------------------------

def _record(root: Path, items, extension_id="ext-id"):
    return {
        "manifest": {
            "id": extension_id,
            "entrypoints": {"instructions": items},
        },
        "source": {"install_path": str(root)},
    }


def test_runtime_blocks_empty_when_no_extension_id(stub_extension_store, tmp_path):
    stub_extension_store(tmp_path)
    record = {"manifest": {"id": "", "entrypoints": {"instructions": []}}}
    assert ei.runtime_instruction_blocks(record) == []


def test_runtime_blocks_empty_when_package_unresolved(stub_extension_store, tmp_path):
    # Force the resolver to report the package as unavailable.
    stub_extension_store(None)
    record = _record(tmp_path, [{"name": "a", "path": "a.md"}])
    assert ei.runtime_instruction_blocks(record) == []


def test_runtime_blocks_formats_valid_instruction_file(stub_extension_store, tmp_path):
    (tmp_path / "guide.md").write_text("Do the thing.", encoding="utf-8")
    stub_extension_store(tmp_path)
    record = _record(tmp_path, [{"name": "Guide", "path": "guide.md"}])
    [block] = ei.runtime_instruction_blocks(record)
    assert block == "### Guide (ext-id).\nDo the thing."


def test_runtime_blocks_appends_providers_scope(stub_extension_store, tmp_path):
    (tmp_path / "guide.md").write_text("Claude only rule.", encoding="utf-8")
    stub_extension_store(tmp_path)
    record = _record(
        tmp_path, [{"name": "Guide", "path": "guide.md", "providers": ["claude", "codex"]}]
    )
    [block] = ei.runtime_instruction_blocks(record)
    assert block == "### Guide (ext-id). Providers: claude, codex only.\nClaude only rule."


def test_runtime_blocks_rejects_path_traversal(stub_extension_store, tmp_path):
    # Install root is a subdir of tmp_path; a secret file lives OUTSIDE it but
    # inside the pytest-managed tmp_path (auto-cleaned, no shared-dir litter).
    root = tmp_path / "pkg"
    root.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("STOLEN", encoding="utf-8")
    stub_extension_store(root)
    # Relative escape and absolute path both must be refused.
    rel_escape = "../secret.md"
    record = _record(
        root,
        [
            {"name": "Rel", "path": rel_escape},
            {"name": "Abs", "path": str(secret)},
        ],
    )
    assert ei.runtime_instruction_blocks(record) == []


def test_runtime_blocks_skips_missing_and_empty_content(stub_extension_store, tmp_path):
    (tmp_path / "blank.md").write_text("   \n  ", encoding="utf-8")
    stub_extension_store(tmp_path)
    record = _record(
        tmp_path,
        [
            {"name": "Missing", "path": "nope.md"},
            {"name": "Blank", "path": "blank.md"},
            {"name": "Good", "path": "g.md"},
        ],
    )
    (tmp_path / "g.md").write_text("kept", encoding="utf-8")
    [block] = ei.runtime_instruction_blocks(record)
    assert block == "### Good (ext-id).\nkept"


def test_runtime_blocks_empty_when_manifest_has_no_entrypoints(stub_extension_store, tmp_path):
    stub_extension_store(tmp_path)
    assert ei.runtime_instruction_blocks({"manifest": {"id": "ext-id"}}) == []


# --- normalize_state ---------------------------------------------------------

def test_normalize_state_defaults_global_true_and_empty_projects():
    assert ei.normalize_state({}) == {"global": True, "projects": {}}
    assert ei.normalize_state({"instructions_enabled": None}) == {
        "global": True,
        "projects": {},
    }


def test_normalize_state_preserves_disabled_global_and_coerces_projects():
    got = ei.normalize_state(
        {"instructions_enabled": {"global": False, "projects": {"/x": 1, "/y": False}}}
    )
    assert got == {"global": False, "projects": {"/x": True, "/y": False}}


# --- _local_project_paths ----------------------------------------------------

def test_local_project_paths_filters_to_primary_and_resolves(stub_project_store, tmp_path):
    primary_a = tmp_path / "proj-a"
    primary_a.mkdir()
    primary_b = tmp_path / "proj-b"
    primary_b.mkdir()
    stub_project_store(
        [
            {"node_id": "primary", "path": str(primary_a)},
            {"node_id": "worker-2", "path": "/should/be/skipped"},
            {"node_id": "primary"},  # no path -> skipped
            {"path": str(primary_b)},  # missing node_id defaults to primary
        ]
    )
    got = ei._local_project_paths()
    assert got == [primary_a.resolve(), primary_b.resolve()]


def test_local_project_paths_empty_when_no_primary_projects(stub_project_store):
    stub_project_store([{"node_id": "worker-2", "path": "/x"}])
    assert ei._local_project_paths() == []
