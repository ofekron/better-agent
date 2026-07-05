"""Generic private-extension directory scan (refactor A).

Private extensions are discovered by scanning <repo_root>/extensions/*/better-agent-extension.json
manifests, not by a hardcoded id map. New private extensions (e.g. assistant)
load with zero public-code id entry. Hardcoded map entries are preserved
(setdefault), so special-case ids like marketplace keep their handling.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("dirscan-test-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extension_store as es  # noqa: E402


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"ok - {msg}")


def _make_ext(root: Path, dir_name: str, ext_id: str) -> None:
    pkg = root / "extensions" / dir_name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "better-agent-extension.json").write_text(
        json.dumps({"kind": "better-agent-extension", "id": ext_id, "name": ext_id,
                    "version": "0.0.1", "surfaces": [], "entrypoints": {},
                    "permissions": {}, "marketplace": {}, "protocol": {"version": 1}})
    )


def test_scan_discovers_unmapped_extension() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_ext(root, "assistant", "ofek-dev.assistant")
        discovered = es._discover_private_extensions(root)
        check(discovered.get("ofek-dev.assistant") == "extensions/assistant",
              "scan discovers an unmapped private extension by manifest id")


def test_scan_none_repo_is_empty() -> None:
    check(es._discover_private_extensions(None) == {}, "None repo root -> empty discovery")


def test_scan_skips_invalid_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        good = root / "extensions" / "good"
        good.mkdir(parents=True)
        (good / "better-agent-extension.json").write_text(
            json.dumps({"id": "ofek-dev.good", "kind": "better-agent-extension"})
        )
        bad = root / "extensions" / "bad"
        bad.mkdir(parents=True)
        (bad / "better-agent-extension.json").write_text("{not json")
        discovered = es._discover_private_extensions(root)
        check("ofek-dev.good" in discovered and "extensions/bad" not in str(discovered),
              "scan skips unreadable/invalid manifests")


def test_ensure_uses_scan_so_new_extension_loads() -> None:
    """A private extension NOT in _PRIVATE_EXTENSION_PATHS must still reconcile
    into the store when its manifest is on disk under the private repo root."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_ext(root, "assistant", "ofek-dev.assistant")
        os.environ["BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH"] = str(root)
        os.environ["BETTER_AGENT_DISABLE_LOCAL_MARKETPLACE_PACKAGE"] = "0"
        try:
            data = {"extensions": {}, "deleted_extensions": {}}
            es._ensure_private_extensions(data)
            check("ofek-dev.assistant" in data["extensions"],
                  "_ensure_private_extensions loads a scanned-only extension")
        finally:
            os.environ.pop("BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH", None)


def test_public_marketplace_fallback_does_not_change_private_scan_root() -> None:
    """The special local marketplace fallback uses the public repo package, but
    it must not mutate the private repo root used for later scanned extensions."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        private_root = (tmp_root / "private").resolve()
        public_root = (tmp_root / "public").resolve()
        _make_ext(private_root, "scanned", "ofek-dev.scanned")
        (public_root / "extensions" / "marketplace").mkdir(parents=True)

        orig_paths = es._PRIVATE_EXTENSION_PATHS
        orig_repo = es._local_private_extension_repo_root
        orig_required = es._required_marketplace_repo_root
        orig_public = es._repo_root
        orig_snap = es._install_private_package_snapshot
        installed: list[tuple[str, Path]] = []

        def fake_snapshot(extension_id: str, package_dir: Path, **_kwargs) -> dict:
            installed.append((extension_id, package_dir))
            return {
                "manifest": {"id": extension_id, "entrypoints": {}, "marketplace": {}},
                "enabled": True,
                "installed_at": "now",
                "updated_at": "now",
                "source": {"type": "better_agent_local", "install_path": str(package_dir)},
                "entitlement": {},
                "smoke_test": {"status": "passed"},
            }

        try:
            es._PRIVATE_EXTENSION_PATHS = {
                es.MARKETPLACE_EXTENSION_ID: "extensions/marketplace",
            }
            es._local_private_extension_repo_root = lambda: private_root
            es._required_marketplace_repo_root = lambda: None
            es._repo_root = lambda: public_root
            es._install_private_package_snapshot = fake_snapshot
            data = {"extensions": {}, "deleted_extensions": {}}
            es._ensure_private_extensions(data)
            check(es.MARKETPLACE_EXTENSION_ID in data["extensions"],
                  "marketplace fallback still installs from public package")
            check("ofek-dev.scanned" in data["extensions"],
                  "later scanned extension still uses private scan root")
            check(any(ext_id == "ofek-dev.scanned" and private_root in path.parents
                      for ext_id, path in installed),
                  "scanned extension package path is under private root")
        finally:
            es._PRIVATE_EXTENSION_PATHS = orig_paths
            es._local_private_extension_repo_root = orig_repo
            es._required_marketplace_repo_root = orig_required
            es._repo_root = orig_public
            es._install_private_package_snapshot = orig_snap


if __name__ == "__main__":
    test_scan_discovers_unmapped_extension()
    test_scan_none_repo_is_empty()
    test_scan_skips_invalid_manifest()
    test_ensure_uses_scan_so_new_extension_loads()
    test_public_marketplace_fallback_does_not_change_private_scan_root()
    print("all dirscan tests passed")
