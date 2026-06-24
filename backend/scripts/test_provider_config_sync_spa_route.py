from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-provider-config-sync-spa-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
_ROOT = _BACKEND.parent
_DIST = _ROOT / "frontend" / "dist"
_DIST.mkdir(parents=True, exist_ok=True)
_TEST_DIST = Path(_TMP_HOME) / "dist"
_TEST_DIST.mkdir(parents=True, exist_ok=True)
(_TEST_DIST / "index.html").write_text("<!doctype html><title>Better Agent</title>", encoding="utf-8")
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

main._dist_dir = _TEST_DIST


def main_run() -> int:
    ok = True

    def check(label: str, condition: bool) -> None:
        nonlocal ok
        print(("PASS " if condition else "FAIL ") + label)
        ok = ok and condition

    with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
        headers = {"X-Internal-Token": getattr(main.coordinator, "internal_token", "")}
        auth_headers = {"Authorization": f"Bearer {main.auth.create_token('test')}"}
        for path in ("/provider-config-sync", "/provider-config-sync/"):
            response = client.get(path)
            check(f"{path} serves SPA", response.status_code == 200)
            check(f"{path} has html", "Better Agent" in response.text)
            check(f"{path} is no-cache", response.headers.get("cache-control") == "no-cache, no-store, must-revalidate")
        response = client.post("/api/provider-config-sync/capability/transfer", headers=auth_headers, json={})
        check("provider config sync public transfer API route is gone", response.status_code in {404, 405})
        response = client.get("/api/provider-config-sync/repository", headers=auth_headers)
        check("provider config sync public repository API route is gone", response.status_code == 404)
        response = client.get("/api/internal/provider-config-sync/repository", headers=headers)
        check("provider config sync internal repository API is runtime-gated", response.status_code == 404)

    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main_run())
