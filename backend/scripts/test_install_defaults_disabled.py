"""Installing an extension must not also start running it.

Locks the fresh-install default (disabled), the update path (keeps whatever the
user already chose), and the opt-in `default_enabled` escape hatch used by
internally synthesized extensions like the personal harness.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-install-default-home-")
_TMP_OS_HOME = tempfile.mkdtemp(prefix="bc-test-install-default-os-home-")
os.environ["HOME"] = _TMP_OS_HOME
_TRUSTED_TEST_ROOT = Path(tempfile.mkdtemp(prefix="bc-test-install-default-trusted-"))
os.environ["BETTER_AGENT_TRUSTED_EXTENSION_FILE_ROOTS"] = str(_TRUSTED_TEST_ROOT)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_store  # noqa: E402

EXTENSION_ID = "ofek.install-default"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_repo(root: Path, version: str) -> Path:
    repo = root / f"repo-{version}"
    package = repo / "extensions" / "pkg"
    package.mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": EXTENSION_ID,
        "name": "Install default",
        "version": version,
        "surfaces": ["backend_feature"],
        "entrypoints": {},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
        },
        "permissions": {},
        "dependencies": [],
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "extensions/pkg/better-agent-extension.json")
    _git(repo, "commit", "-m", f"add {version}")
    return repo


def _work() -> Path:
    root = _TRUSTED_TEST_ROOT / ".test-repos"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=root))


def _install(repo: Path) -> dict:
    return extension_store.install_from_repo(
        repo_url=repo.as_uri(),
        extension_path="extensions/pkg",
    )


def test_fresh_install_is_disabled() -> None:
    work = _work()
    try:
        record = _install(_make_repo(work, "1.0.0"))
        if record["enabled"] is not False:
            raise AssertionError(f"fresh install must land disabled, got {record['enabled']!r}")
        live = extension_store.get_extension(EXTENSION_ID)
        if live.get("enabled") is not False:
            raise AssertionError("persisted record is not disabled")
    finally:
        extension_store.uninstall(EXTENSION_ID)
        shutil.rmtree(work, ignore_errors=True)


def test_update_preserves_user_enable() -> None:
    work = _work()
    try:
        _install(_make_repo(work, "1.0.0"))
        extension_store.set_enabled(EXTENSION_ID, True)
        updated = _install(_make_repo(work, "2.0.0"))
        if updated["enabled"] is not True:
            raise AssertionError("update wrongly reset a user-enabled extension to disabled")
    finally:
        extension_store.uninstall(EXTENSION_ID)
        shutil.rmtree(work, ignore_errors=True)


def test_update_preserves_user_disable() -> None:
    work = _work()
    try:
        _install(_make_repo(work, "1.0.0"))
        extension_store.set_enabled(EXTENSION_ID, True)
        extension_store.set_enabled(EXTENSION_ID, False)
        updated = _install(_make_repo(work, "2.0.0"))
        if updated["enabled"] is not False:
            raise AssertionError("update wrongly re-enabled an extension the user disabled")
    finally:
        extension_store.uninstall(EXTENSION_ID)
        shutil.rmtree(work, ignore_errors=True)


def test_default_enabled_opt_in_starts_active() -> None:
    """The personal harness is synthesized from the user's own instruction
    files, so it opts in to starting active — but an explicit disable sticks."""
    work = _work()
    try:
        package = _make_repo(work, "1.0.0") / "extensions" / "pkg"
        source = {"type": "artifact", "repo_url": "", "extension_path": "", "ref": "", "commit_sha": "seed"}
        record = extension_store._install_from_package_dir(
            package_dir=package,
            source=source,
            default_enabled=True,
        )
        if record["enabled"] is not True:
            raise AssertionError("default_enabled=True must start active on a fresh install")
        extension_store.set_enabled(EXTENSION_ID, False)
        again = extension_store._install_from_package_dir(
            package_dir=package,
            source={**source, "commit_sha": "seed-2"},
            default_enabled=True,
        )
        if again["enabled"] is not False:
            raise AssertionError("default_enabled must not override an explicit user disable")
    finally:
        extension_store.uninstall(EXTENSION_ID)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    test_fresh_install_is_disabled()
    test_update_preserves_user_enable()
    test_update_preserves_user_disable()
    test_default_enabled_opt_in_starts_active()
    print("install-defaults-disabled tests passed")
