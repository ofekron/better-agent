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
_ORIGINAL_HOST_ENV = L._host_env

_ROUTES = (
    "import os\n"
    "from pathlib import Path\n"
    "from fastapi import APIRouter\n"
    "_perf_marker = Path(__file__).parents[1] / 'perf-imported.marker'\n"
    "_perf_imported_during_validation = _perf_marker.exists()\n"
    "import paths as extension_paths\n"
    "_count = 0\n"
    "def create_router(ctx):\n"
    "    r = APIRouter()\n"
    "    @r.get('/imports')\n"
    "    def imports():\n"
    "        return {'paths_source': extension_paths.SOURCE, "
    "'perf_imported_during_validation': _perf_imported_during_validation}\n"
    "    @r.get('/pid')\n"
    "    def pid(): return {'pid': os.getpid()}\n"
    "    @r.get('/bump')\n"
    "    def bump():\n"
    "        global _count; _count += 1\n"
    "        return {'count': _count}\n"
    "    @r.post('/exit-once')\n"
    "    def exit_once():\n"
    "        marker = Path(__file__).with_name('exit-once.marker')\n"
    "        if not marker.exists():\n"
    "            marker.write_text('1', encoding='utf-8')\n"
    "            os._exit(0)\n"
    "        return {'ok': True, 'pid': os.getpid()}\n"
    "    @r.post('/exit-never-retry')\n"
    "    def exit_never_retry():\n"
    "        os._exit(0)\n"
    "    return r\n"
)


def check(cond: bool, msg: str) -> None:
    print(("  ok:" if cond else "  FAIL:") + " " + msg)
    if not cond:
        FAILURES.append(msg)


def _isolated_host_env() -> dict[str, str]:
    env = _ORIGINAL_HOST_ENV()
    for key in ("BETTER_AGENT_HOME", "BETTER_CLAUDE_HOME", "BETTER_AGENT_TEST_MODE"):
        env[key] = os.environ[key]
    return env


def _seed() -> None:
    pkg = TMP / "pkg"
    (pkg / "backend").mkdir(parents=True)
    (pkg / "backend" / "routes.py").write_text(_ROUTES, encoding="utf-8")
    (pkg / "paths.py").write_text("SOURCE = 'extension'\n", encoding="utf-8")
    (pkg / "perf.py").write_text(
        "from pathlib import Path\n"
        "(Path(__file__).with_name('perf-imported.marker')).write_text('1', encoding='utf-8')\n",
        encoding="utf-8",
    )
    data = extension_store._load()
    data["extensions"]["ofek.persist"] = {
        "manifest": {
            "kind": extension_store.MANIFEST_KIND, "id": "ofek.persist", "name": "P",
            "version": "1.0.0", "description": "", "surfaces": ["backend_feature"],
            "entrypoints": {
                "backend": "backend/routes.py",
                "frontend": "",
                "mcp": [],
                "provider_capabilities": [],
                "backend_retry_on_exit": ["exit-once"],
            },
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
        extension_store.installation_profile.integrations_enabled = lambda: True
        L._host_env = _isolated_host_env
        _seed()
        app = FastAPI()
        app.include_router(extension_api.router)
        client = TestClient(app)

        imports = client.get("/api/extensions/ofek.persist/backend/imports")
        check(
            imports.status_code == 200
            and imports.json() == {
                "paths_source": "extension",
                "perf_imported_during_validation": False,
            },
            "host validation cannot import extension modules and extension paths stays isolated",
        )

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
        handle.channel.proc.kill()
        r4 = client.get("/api/extensions/ofek.persist/backend/pid")
        check(r4.status_code == 200, "request succeeds after process crash (auto-restart)")

        r5 = client.post("/api/extensions/ofek.persist/backend/exit-once")
        check(r5.status_code == 200 and r5.json().get("ok") is True,
              "declared backend_retry_on_exit route retries once after mid-request process exit")

        r6 = client.post("/api/extensions/ofek.persist/backend/exit-never-retry")
        check(r6.status_code == 500,
              "undeclared route keeps fail-closed process-exit behavior")
    finally:
        L.shutdown_persistent_backends()
        L._host_env = _ORIGINAL_HOST_ENV
        from shutil import rmtree
        rmtree(TMP, ignore_errors=True)
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S)")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
