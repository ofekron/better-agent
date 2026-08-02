"""Coverage for desktop/release.py — the tufup update-repo publisher.

tufup is broken in the dev venv (`bsdiff4.core` missing), so release.py is
imported against a fake `tufup` package injected into sys.modules. The real
update_delta.create_patch and updater.APP_NAME are used as-is.
"""
from __future__ import annotations

import runpy
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SUFFIX_PATCH = ".patch"


def _install_fake_tufup() -> None:
    """Inject a minimal tufup package so release.py + update_delta import
    without the broken bsdiff4 native extension."""
    tufup = types.ModuleType("tufup")
    tufup.__ba_fake__ = True
    common = types.ModuleType("tufup.common")
    common.SUFFIX_PATCH = _SUFFIX_PATCH
    common.TargetMeta = object
    client = types.ModuleType("tufup.client")
    client.SUFFIX_FAILED = ".failed"
    client.Client = object
    repo = types.ModuleType("tufup.repo")
    # Repository(...) must return a mock-able instance for both the cached
    # module and the runpy-executed __main__ guard.
    repo.Repository = MagicMock()
    tufup.common = common
    tufup.repo = repo
    tufup.client = client
    sys.modules.update({
        "tufup": tufup,
        "tufup.common": common,
        "tufup.repo": repo,
        "tufup.client": client,
    })


_install_fake_tufup()

import release  # noqa: E402
from release import ReleaseRepo, _current_branch, _main  # noqa: E402
from updater import APP_NAME  # noqa: E402

DESKTOP_DIR = Path(__file__).resolve().parent
RELEASE_PY = DESKTOP_DIR / "release.py"
REPO_ROOT = DESKTOP_DIR.parent


@pytest.fixture(autouse=True)
def _mock_repository(monkeypatch):
    """Fresh Repository mock per test for the cached release module."""
    monkeypatch.setattr(release, "Repository", MagicMock())


class _Archive:
    def __init__(self, version, path):
        self.version = version
        self.path = path


def _repo_with(inner: MagicMock) -> ReleaseRepo:
    """Build a ReleaseRepo whose _repo is the given mock (skipping the
    real Repository construction side effects under test)."""
    r = ReleaseRepo.__new__(ReleaseRepo)
    r.repo_dir = Path("/tmp/repo")
    r.keys_dir = Path("/tmp/keys")
    r._repo = inner
    return r


# --- ReleaseRepo.__init__ ----------------------------------------------------

def test_init_constructs_repository(tmp_path, monkeypatch):
    seen = {}

    def ctor(**kwargs):
        seen.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(release, "Repository", ctor)
    repo_dir = tmp_path / "repo"
    keys_dir = tmp_path / "keys"
    r = ReleaseRepo(repo_dir, keys_dir)

    assert seen == {
        "app_name": APP_NAME,
        "repo_dir": str(repo_dir),
        "keys_dir": str(keys_dir),
    }
    assert r.repo_dir == repo_dir
    assert r.keys_dir == keys_dir


# --- _pinned_cwd -------------------------------------------------------------

def test_pinned_cwd_creates_missing_parent_and_restores_cwd(tmp_path):
    cwd_before = Path.cwd()
    # parent does not exist yet -> mkdir(parents=True) must create it
    repo_dir = tmp_path / "a" / "b" / "repo"

    r = _repo_with(MagicMock())
    r.repo_dir = repo_dir
    with r._pinned_cwd():
        cwd_inside = Path.cwd()

    assert (tmp_path / "a" / "b").is_dir()
    assert cwd_inside == repo_dir.parent
    # contextlib.chdir restores the original cwd on exit
    assert Path.cwd() == cwd_before


def test_pinned_cwd_existing_parent(tmp_path):
    # exist_ok branch: parent already present
    repo_dir = tmp_path / "repo"
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    r = _repo_with(MagicMock())
    r.repo_dir = repo_dir
    with r._pinned_cwd():
        pass
    assert repo_dir.parent.is_dir()


# --- initialize --------------------------------------------------------------

def test_initialize_delegates_under_pinned_cwd(tmp_path):
    inner = MagicMock()
    r = ReleaseRepo(tmp_path / "x" / "repo", tmp_path / "keys")
    r._repo = inner
    r.initialize()
    inner.initialize.assert_called_once_with()


# --- export_trusted_root -----------------------------------------------------

def test_export_trusted_root_writes_bytes(tmp_path):
    repo_dir = tmp_path / "repo"
    (repo_dir / "metadata").mkdir(parents=True)
    src = repo_dir / "metadata" / "root.json"
    src.write_bytes(b"\x00ROOT\x01")

    dest = tmp_path / "out" / "nested" / "root.json"
    r = _repo_with(MagicMock())
    r.repo_dir = repo_dir

    returned = r.export_trusted_root(dest)

    assert returned == dest
    assert dest.read_bytes() == b"\x00ROOT\x01"


# --- publish_bundle ----------------------------------------------------------

def _make_inner(tmp_path, roles, targets_dir):
    inner = MagicMock()
    inner.roles = roles
    inner.targets_dir = targets_dir
    return inner


def test_publish_bundle_loads_roles_when_none(tmp_path, monkeypatch):
    targets_dir = tmp_path / "targets"
    roles = MagicMock()
    roles.get_latest_archive.return_value = None  # no previous, no latest

    inner = MagicMock()
    inner.targets_dir = targets_dir
    inner.roles = None  # fresh repo: roles not loaded yet

    def _load(create_keys=False):
        inner.roles = roles

    inner._load_keys_and_roles.side_effect = _load

    r = ReleaseRepo(tmp_path / "repo", tmp_path / "keys")
    r._repo = inner
    r.publish_bundle(tmp_path / "bundle", "1.0")

    inner._load_keys_and_roles.assert_called_once_with(create_keys=False)
    inner.add_bundle.assert_called_once_with(
        new_bundle_dir=str(tmp_path / "bundle"), new_version="1.0", skip_patch=True
    )
    roles.add_or_update_target.assert_not_called()
    inner.publish_changes.assert_called_once_with(private_key_dirs=[str(tmp_path / "keys")])


def test_publish_bundle_no_patch_when_no_previous(tmp_path, monkeypatch):
    roles = MagicMock()
    roles.get_latest_archive.return_value = None
    inner = _make_inner(tmp_path, roles, tmp_path / "targets")
    create_patch = MagicMock()
    monkeypatch.setattr(release, "create_patch", create_patch)

    r = ReleaseRepo(tmp_path / "repo", tmp_path / "keys")
    r._repo = inner
    r.publish_bundle(tmp_path / "bundle", "1.0")

    # roles already set -> no load
    inner._load_keys_and_roles.assert_not_called()
    create_patch.assert_not_called()
    roles.add_or_update_target.assert_not_called()
    inner.publish_changes.assert_called_once()


def test_publish_bundle_creates_patch(tmp_path, monkeypatch):
    targets_dir = tmp_path / "targets"
    roles = MagicMock()
    prev = _Archive(version=1, path="v1.tar.gz")
    new = _Archive(version=2, path="v2.tar.gz")
    roles.get_latest_archive.side_effect = [prev, new]
    inner = _make_inner(tmp_path, roles, targets_dir)

    patch_metadata = {"size": 42}
    create_patch = MagicMock(return_value=patch_metadata)
    monkeypatch.setattr(release, "create_patch", create_patch)

    r = ReleaseRepo(tmp_path / "repo", tmp_path / "keys")
    r._repo = inner
    r.publish_bundle(tmp_path / "bundle", "2.0")

    source_path = targets_dir / "v1.tar.gz"
    target_path = targets_dir / "v2.tar.gz"
    patch_path = target_path.with_suffix("").with_suffix(_SUFFIX_PATCH)
    create_patch.assert_called_once_with(source_path, target_path, patch_path)
    roles.add_or_update_target.assert_called_once_with(
        local_path=patch_path, custom=dict(user=None, tufup=patch_metadata)
    )
    inner.publish_changes.assert_called_once_with(private_key_dirs=[str(tmp_path / "keys")])


def test_publish_bundle_skips_patch_when_version_not_greater(tmp_path, monkeypatch):
    roles = MagicMock()
    prev = _Archive(version=3, path="v3.tar.gz")
    same = _Archive(version=3, path="v3b.tar.gz")  # not strictly greater
    roles.get_latest_archive.side_effect = [prev, same]
    inner = _make_inner(tmp_path, roles, tmp_path / "targets")

    create_patch = MagicMock()
    monkeypatch.setattr(release, "create_patch", create_patch)

    r = ReleaseRepo(tmp_path / "repo", tmp_path / "keys")
    r._repo = inner
    r.publish_bundle(tmp_path / "bundle", "3.0")

    create_patch.assert_not_called()
    roles.add_or_update_target.assert_not_called()
    inner.publish_changes.assert_called_once()


# --- _current_branch ---------------------------------------------------------

def test_current_branch_ok(monkeypatch):
    captured = {}
    cp = MagicMock(returncode=0, stdout="main\n")

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return cp

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    assert _current_branch() == "main"
    assert captured["cwd"] == REPO_ROOT


def test_current_branch_failure_returns_empty(monkeypatch):
    cp = MagicMock(returncode=128, stdout="whatever")
    monkeypatch.setattr(release.subprocess, "run", lambda *a, **k: cp)
    assert _current_branch() == ""


# --- _main -------------------------------------------------------------------

def _stub_release_repo(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(release, "ReleaseRepo", lambda rd, kd: fake)
    return fake


def test_main_init(tmp_path, monkeypatch):
    fake = _stub_release_repo(monkeypatch)
    rc = _main(["init", str(tmp_path / "r"), str(tmp_path / "k")])
    assert rc == 0
    fake.initialize.assert_called_once()


def test_main_export_root(tmp_path, monkeypatch):
    fake = _stub_release_repo(monkeypatch)
    dest = tmp_path / "root.json"
    rc = _main([
        "export-root", str(tmp_path / "r"), str(tmp_path / "k"), str(dest)
    ])
    assert rc == 0
    fake.export_trusted_root.assert_called_once_with(str(dest))


def test_main_publish_skips_on_non_main_branch(tmp_path, monkeypatch, capsys):
    fake = _stub_release_repo(monkeypatch)
    rc = _main(
        ["publish", str(tmp_path / "r"), str(tmp_path / "k"),
         str(tmp_path / "bundle"), "1.0"],
        branch_name="dev",
    )
    assert rc == 0
    fake.publish_bundle.assert_not_called()
    assert "skipped on dev" in capsys.readouterr().out


def test_main_publish_skip_label_for_detached_head(tmp_path, monkeypatch, capsys):
    _stub_release_repo(monkeypatch)
    rc = _main(
        ["publish", str(tmp_path / "r"), str(tmp_path / "k"),
         str(tmp_path / "bundle"), "1.0"],
        branch_name="",
    )
    assert rc == 0
    assert "detached HEAD" in capsys.readouterr().out


def test_main_publish_runs_on_main(tmp_path, monkeypatch):
    fake = _stub_release_repo(monkeypatch)
    bundle = tmp_path / "bundle"
    rc = _main(
        ["publish", str(tmp_path / "r"), str(tmp_path / "k"),
         str(bundle), "1.0"],
        branch_name="main",
    )
    assert rc == 0
    fake.publish_bundle.assert_called_once_with(str(bundle), "1.0")


def test_main_publish_with_export_root(tmp_path, monkeypatch):
    fake = _stub_release_repo(monkeypatch)
    dest = tmp_path / "root.json"
    rc = _main(
        ["publish", str(tmp_path / "r"), str(tmp_path / "k"),
         str(tmp_path / "bundle"), "1.0", "--export-root", str(dest)],
        branch_name="main",
    )
    assert rc == 0
    fake.publish_bundle.assert_called_once()
    fake.export_trusted_root.assert_called_once_with(str(dest))


def test_main_publish_uses_current_branch_when_unspecified(tmp_path, monkeypatch):
    # branch_name is None -> _current_branch() is consulted
    monkeypatch.setattr(release, "_current_branch", lambda: "main")
    fake = _stub_release_repo(monkeypatch)
    rc = _main(
        ["publish", str(tmp_path / "r"), str(tmp_path / "k"),
         str(tmp_path / "bundle"), "1.0"],
    )
    assert rc == 0
    fake.publish_bundle.assert_called_once()


def test_main_publish_skips_when_current_branch_not_main(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(release, "_current_branch", lambda: "feature")
    fake = _stub_release_repo(monkeypatch)
    rc = _main(
        ["publish", str(tmp_path / "r"), str(tmp_path / "k"),
         str(tmp_path / "bundle"), "1.0"],
    )
    assert rc == 0
    fake.publish_bundle.assert_not_called()
    assert "skipped on feature" in capsys.readouterr().out


# --- __main__ guard ----------------------------------------------------------

def test_main_guard_runs_init(tmp_path, monkeypatch):
    # runpy re-executes release.py with __name__ == "__main__"; the fake
    # tufup in sys.modules supplies a constructible Repository.
    monkeypatch.setattr(
        "sys.argv",
        ["release.py", "init", str(tmp_path / "r"), str(tmp_path / "k")],
    )
    with pytest.raises(SystemExit) as ei:
        runpy.run_path(str(RELEASE_PY), run_name="__main__")
    assert ei.value.code == 0
