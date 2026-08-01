"""Full unit coverage for extension_package_loader.py.

The loader resolves an extension's runtime package directory and exposes it
to Python imports / prompt lookups. It is a security-sensitive boundary: it
validates the extension id, requires the extension to be runtime-ready,
rejects package names containing path separators (only top-level packages),
and confines prompt lookups to the prompts directories via path-relative
checks. These tests cover every validation, traversal-rejection, and
resolution branch.
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

_HOME = _test_home.isolate("ba-extension-package-loader-")

import extension_package_loader as loader  # noqa: E402
import extension_store  # noqa: E402
from extension_package_loader import ExtensionPackageUnavailable  # noqa: E402


def _patch_store(monkeypatch, *, record, ready, package_root):
    monkeypatch.setattr(extension_store, "get_extension", lambda eid: record)
    monkeypatch.setattr(
        extension_store, "is_extension_runtime_ready", lambda eid: ready
    )
    monkeypatch.setattr(
        extension_store, "runtime_package_root", lambda eid: package_root
    )


# --------------------------------------------------------------------------- #
# package_root
# --------------------------------------------------------------------------- #
def test_package_root_requires_nonempty_id(monkeypatch):
    _patch_store(monkeypatch, record={"id": "ext-1"}, ready=True, package_root=None)
    for bad in ("", "   ", None):
        with pytest.raises(ExtensionPackageUnavailable):
            loader.package_root(bad)  # type: ignore[arg-type]


def test_package_root_requires_active_record(monkeypatch):
    _patch_store(monkeypatch, record=None, ready=True, package_root=None)
    with pytest.raises(ExtensionPackageUnavailable):
        loader.package_root("ext-1")


def test_package_root_requires_runtime_ready(monkeypatch):
    _patch_store(monkeypatch, record={"id": "ext-1"}, ready=False, package_root=None)
    with pytest.raises(ExtensionPackageUnavailable):
        loader.package_root("ext-1")


def test_package_root_requires_real_dir(monkeypatch):
    _patch_store(
        monkeypatch, record={"id": "ext-1"}, ready=True, package_root=Path("/no/such/dir")
    )
    with pytest.raises(ExtensionPackageUnavailable):
        loader.package_root("ext-1")


def test_package_root_resolves_active_dir(monkeypatch, tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    _patch_store(monkeypatch, record={"id": "ext-1"}, ready=True, package_root=root)
    assert loader.package_root("ext-1") == root.resolve()


# --------------------------------------------------------------------------- #
# ensure_package_importable
# --------------------------------------------------------------------------- #
def test_ensure_rejects_bad_package_name(monkeypatch, tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    _patch_store(monkeypatch, record={"id": "ext-1"}, ready=True, package_root=root)
    for bad in ("", "   ", None, "a/b", "a\\b"):
        with pytest.raises(ExtensionPackageUnavailable):
            loader.ensure_package_importable("ext-1", bad)  # type: ignore[arg-type]


def test_ensure_rejects_missing_package_dir(monkeypatch, tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    _patch_store(monkeypatch, record={"id": "ext-1"}, ready=True, package_root=root)
    with pytest.raises(ExtensionPackageUnavailable):
        loader.ensure_package_importable("ext-1", "mypkg")


def test_ensure_inserts_root_on_syspath(monkeypatch, tmp_path):
    root = tmp_path / "pkg"
    pkg = root / "mypkg"
    pkg.mkdir(parents=True)
    _patch_store(monkeypatch, record={"id": "ext-1"}, ready=True, package_root=root)
    root_text = str(root)
    sys.path[:] = [p for p in sys.path if p != root_text]
    try:
        returned = loader.ensure_package_importable("ext-1", "mypkg")
        assert returned == root
        assert root_text in sys.path
    finally:
        sys.path[:] = [p for p in sys.path if p != root_text]


def test_ensure_idempotent_when_root_already_on_syspath(monkeypatch, tmp_path):
    root = tmp_path / "pkg"
    pkg = root / "mypkg"
    pkg.mkdir(parents=True)
    _patch_store(monkeypatch, record={"id": "ext-1"}, ready=True, package_root=root)
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    before = list(sys.path)
    try:
        loader.ensure_package_importable("ext-1", "mypkg")
        # No duplicate insertion.
        assert sys.path.count(root_text) == 1
    finally:
        sys.path[:] = before


# --------------------------------------------------------------------------- #
# prompt_path
# --------------------------------------------------------------------------- #
def test_prompt_path_returns_none_when_extension_inactive(monkeypatch):
    _patch_store(monkeypatch, record=None, ready=True, package_root=None)
    assert loader.prompt_path("ext-1", "welcome.md") is None


def test_prompt_path_finds_prompt_under_prompts(monkeypatch, tmp_path):
    root = tmp_path / "pkg"
    prompts = root / "prompts"
    prompts.mkdir(parents=True)
    f = prompts / "hello.md"
    f.write_text("hi", encoding="utf-8")
    _patch_store(monkeypatch, record={"id": "ext-1"}, ready=True, package_root=root)
    assert loader.prompt_path("ext-1", "hello.md") == f.resolve()


def test_prompt_path_finds_prompt_under_provisioning(monkeypatch, tmp_path):
    root = tmp_path / "pkg"
    prompts = root / "provisioning" / "prompts"
    prompts.mkdir(parents=True)
    f = prompts / "setup.md"
    f.write_text("hi", encoding="utf-8")
    _patch_store(monkeypatch, record={"id": "ext-1"}, ready=True, package_root=root)
    assert loader.prompt_path("ext-1", "setup.md") == f.resolve()


def test_prompt_path_rejects_traversal_name(monkeypatch, tmp_path):
    root = tmp_path / "pkg"
    prompts = root / "prompts"
    secret = root / "secret.txt"
    prompts.mkdir(parents=True)
    secret.write_text("nope", encoding="utf-8")
    _patch_store(monkeypatch, record={"id": "ext-1"}, ready=True, package_root=root)
    # "../secret.txt" resolves outside the prompts base -> skipped, not found.
    assert loader.prompt_path("ext-1", "../secret.txt") is None


def test_prompt_path_returns_none_when_missing(monkeypatch, tmp_path):
    root = tmp_path / "pkg"
    (root / "prompts").mkdir(parents=True)
    _patch_store(monkeypatch, record={"id": "ext-1"}, ready=True, package_root=root)
    assert loader.prompt_path("ext-1", "nope.md") is None
