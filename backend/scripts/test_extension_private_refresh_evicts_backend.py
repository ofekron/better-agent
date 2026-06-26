"""A private local extension whose repo HEAD advanced MUST evict its persistent
backend subprocess on store reconcile. The persistent proc is spawned with the
old env baked in (permissions, minted internal token), so without eviction a
manifest change that affects the subprocess env stays stale until a full backend
restart.

This locks the fix in ``_ensure_private_extensions``: when the
``better_agent_local`` re-snapshot branch refreshes a record (changed=True), it
calls ``evict_persistent_backend(extension_id)``. No real subprocess is spawned
here — we spy on the loader function and assert it was invoked for the refreshed
extension, and that the stored record's manifest updated. The test fails before
the fix (no eviction) and passes after.

Run: python backend/scripts/test_extension_private_refresh_evicts_backend.py
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import _test_home
_test_home.isolate("ba-test-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extension_store  # noqa: E402

FAILURES: list[str] = []
EXT_ID = "ofek.refresh.evict"
EXT_DIR_NAME = "refresh-evict"
NEW_HEAD_SHA = "new-head-sha-after-commit"
STALE_SHA = "stale-old-sha-before-commit"


def check(cond: bool, msg: str) -> None:
    print(("  ok:" if cond else "  FAIL:") + " " + msg)
    if not cond:
        FAILURES.append(msg)


def _seed_repo() -> Path:
    repo_root = Path(tempfile.mkdtemp(prefix="bc-test-private-repo-"))
    pkg = repo_root / "extensions" / EXT_DIR_NAME
    pkg.mkdir(parents=True)
    # Minimal manifest — only needs an `id` for discovery; the snapshot call is
    # mocked so validation of the on-disk manifest never runs.
    (pkg / "better-agent-extension.json").write_text(
        json.dumps({"id": EXT_ID, "name": "RefreshEvict", "version": "1.0.0"}),
        encoding="utf-8",
    )
    return repo_root


def _record(manifest_permissions: dict) -> dict:
    return {
        "manifest": {
            "kind": extension_store.MANIFEST_KIND, "id": EXT_ID, "name": "RefreshEvict",
            "version": "1.0.0", "description": "", "surfaces": ["backend_feature"],
            "entrypoints": {"backend": "backend/routes.py", "frontend": "", "mcp": [], "provider_capabilities": []},
            "permissions": manifest_permissions,
            "marketplace": {"product_id": "", "subscription_required": False, "entitlement_url": ""},
        },
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {"type": "better_agent_local", "commit_sha": STALE_SHA,
                   "extension_path": f"extensions/{EXT_DIR_NAME}",
                   "install_path": ""},
        "entitlement": {"status": "not_required", "product_id": "", "token_present": False,
                        "last_checked_at": "", "expires_at": ""},
    }


def main() -> None:
    import os
    repo_root = _seed_repo()
    os.environ["BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH"] = str(repo_root)

    # OLD record: no internal_loopback. Env-affecting change in refresh: adds it.
    data = {"extensions": {EXT_ID: _record({"backend_routes": True})},
            "deleted_extensions": {}}

    refreshed_record = _record({"backend_routes": True, "internal_loopback": True})
    refreshed_record["source"]["commit_sha"] = NEW_HEAD_SHA

    import extension_backend_loader as L

    with patch.object(extension_store, "_install_private_package_snapshot", return_value=refreshed_record) as snap, \
         patch.object(extension_store, "_private_extension_commit_sha", return_value=NEW_HEAD_SHA), \
         patch.object(L, "evict_persistent_backend") as evict:
        changed = extension_store._ensure_private_extensions(data)

    try:
        check(changed is True, "reconcile reports changed=True")
        check(snap.called and snap.call_args.args[0] == EXT_ID,
              "re-snapshot invoked for the advanced extension")
        check(evict.called and evict.call_args.args[0] == EXT_ID,
              "evict_persistent_backend invoked for the refreshed extension")
        stored = data["extensions"][EXT_ID]
        check(stored["source"]["commit_sha"] == NEW_HEAD_SHA,
              "stored record advanced to the new commit_sha")
        check(stored["manifest"]["permissions"].get("internal_loopback") is True,
              "stored manifest reflects the env-affecting permission change")
    finally:
        from shutil import rmtree
        rmtree(repo_root, ignore_errors=True)

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
