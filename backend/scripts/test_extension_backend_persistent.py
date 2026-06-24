"""Locks persistent extension backends: one long-lived process per extension
serves many requests (NOT a fresh subprocess per request), and restarts after
eviction or crash. Sidesteps the internal-LLM runtime-ready gate by seeding an
extension whose only permission is backend_routes (no provider required).

Run: python backend/scripts/test_extension_backend_persistent.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="bc-test-ext-backend-persistent-"))
import _test_home
_test_home.isolate("ba-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import extension_api  # noqa: E402
import extension_backend_loader as L  # noqa: E402
import extension_store  # noqa: E402

FAILURES: list[str] = []

_ROUTES = (
    "import os\n"
    "from fastapi import APIRouter\n"
    "_count = 0\n"
    "def create_router(ctx):\n"
    "    r = APIRouter()\n"
    "    @r.get('/pid')\n"
    "    def pid(): return {'pid': os.getpid()}\n"
    "    @r.get('/bump')\n"
    "    def bump():\n"
    "        global _count; _count += 1\n"
    "        return {'count': _count}\n"
    "    return r\n"
)


def check(cond: bool, msg: str) -> None:
    print(("  ok:" if cond else "  FAIL:") + " " + msg)
    if not cond:
        FAILURES.append(msg)


def _seed() -> None:
    pkg = TMP / "pkg"
    (pkg / "backend").mkdir(parents=True)
    (pkg / "backend" / "routes.py").write_text(_ROUTES, encoding="utf-8")
    data = extension_store._load()
    data["extensions"]["ofek.persist"] = {
        "manifest": {
            "kind": extension_store.MANIFEST_KIND, "id": "ofek.persist", "name": "P",
            "version": "1.0.0", "description": "", "surfaces": ["backend_feature"],
            "entrypoints": {"backend": "backend/routes.py", "frontend": "", "mcp": [], "provider_capabilities": []},
            "permissions": {"backend_routes": True},
            "marketplace": {"product_id": "", "subscription_required": False, "entitlement_url": ""},
        },
        "enabled": True,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source": {"type": "git", "repo_url": "https://x/y.git", "extension_path": "extensions/p",
                   "ref": "", "commit_sha": "abc", "install_path": str(pkg)},
        "entitlement": {"status": "not_required", "product_id": "", "token_present": False,
                        "last_checked_at": "", "expires_at": ""},
    }
    extension_store._save(data)


def main() -> None:
    try:
        _seed()
        app = FastAPI()
        app.include_router(extension_api.router)
        client = TestClient(app)

        r = client.get("/api/extensions/ofek.persist/backend/pid")
        check(r.status_code == 200, "first request dispatches")
        pid1 = r.json().get("pid")

        # Persistence proof #1: same process serves the next request.
        r2 = client.get("/api/extensions/ofek.persist/backend/pid")
        check(r2.status_code == 200 and r2.json().get("pid") == pid1,
              "second request served by the SAME process (not a fresh subprocess)")

        # Persistence proof #2: a module-level counter survives across requests.
        b1 = client.get("/api/extensions/ofek.persist/backend/bump").json().get("count")
        b2 = client.get("/api/extensions/ofek.persist/backend/bump").json().get("count")
        check(b1 == 1 and b2 == 2, f"module counter persists across requests ({b1},{b2})")

        # Eviction restarts a fresh process.
        L.evict_persistent_backend("ofek.persist")
        time.sleep(0.1)
        r3 = client.get("/api/extensions/ofek.persist/backend/pid")
        check(r3.status_code == 200 and r3.json().get("pid") != pid1,
              "evict_persistent_backend restarts a new process")

        # Crash recovery: kill the live process, next request restarts it.
        handle = L._PERSISTENT_PROCS["ofek.persist"]
        handle.proc.kill()
        r4 = client.get("/api/extensions/ofek.persist/backend/pid")
        check(r4.status_code == 200, "request succeeds after process crash (auto-restart)")
    finally:
        L.shutdown_persistent_backends()
        from shutil import rmtree
        rmtree(TMP, ignore_errors=True)
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
