"""A route raising inside an extension backend must not vanish as a silent,
unlogged 500 — this is the root cause shared by every extension's "500 with
no error trail" bug (ask-search, routines, etc): extension_backend_host.py's
persistent request handler caught every exception with a bare `except
Exception:` and zero logging.

Drives the real subprocess entrypoint (`extension_backend_host.py
--persistent`) against a throwaway fixture extension whose route raises, and
asserts (a) the caller still gets a clean 500 response and (b) the
subprocess's stderr actually contains a traceback for it.

Run with:
    cd backend && .venv/bin/python scripts/test_extension_backend_host_error_visibility.py
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_HOST = Path(__file__).resolve().parents[1] / "extension_backend_host.py"

_FIXTURE_ROUTES = '''
from fastapi import APIRouter

def create_router(_context):
    router = APIRouter()

    @router.get("/boom")
    def boom():
        raise RuntimeError("fixture boom")

    return router
'''


def _request(path: str, request_id: str = "req-1") -> dict:
    return {
        "id": request_id,
        "method": "GET",
        "path": path,
        "query_string": "",
        "headers": [],
        "body": "",
    }


def test_route_exception_is_logged_and_returns_clean_500() -> bool:
    with tempfile.TemporaryDirectory(prefix="ba-ext-host-fixture-") as tmp:
        install_path = Path(tmp)
        (install_path / "routes.py").write_text(_FIXTURE_ROUTES)
        spec = {
            "extension_id": "test.boom-fixture",
            "install_path": str(install_path),
            "entrypoint": str(install_path / "routes.py"),
            "entrypoint_kind": "file",
            "source": {},
            "max_concurrency": 4,
        }
        proc = subprocess.run(
            [sys.executable, str(_HOST), "--persistent"],
            input=(json.dumps(spec) + "\n" + json.dumps(_request("/boom")) + "\n").encode("utf-8"),
            capture_output=True,
            timeout=30,
        )
        stdout_lines = [line for line in proc.stdout.decode("utf-8").splitlines() if line.strip()]
        if len(stdout_lines) != 1:
            print(f"{FAIL} expected exactly one response line, got {stdout_lines!r}")
            return False
        response = json.loads(stdout_lines[0])
        if response.get("status") != 500:
            print(f"{FAIL} expected status 500, got {response.get('status')!r}")
            return False
        body = base64.b64decode(response.get("body") or "").decode("utf-8", "replace")
        if body != "Extension backend failed":
            print(f"{FAIL} unexpected body: {body!r}")
            return False
        stderr = proc.stderr.decode("utf-8", "replace")
        if "extension backend route failed" not in stderr:
            print(f"{FAIL} expected route-failure log line in stderr, got: {stderr!r}")
            return False
        if "RuntimeError: fixture boom" not in stderr:
            print(f"{FAIL} expected the real exception traceback in stderr, got: {stderr!r}")
            return False
        print(f"{PASS} route exception -> clean 500 response + logged traceback in stderr")
        return True


def main_run() -> int:
    tests = [test_route_exception_is_logged_and_returns_clean_500]
    results = []
    for fn in tests:
        try:
            results.append(fn())
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"{FAIL} {fn.__name__} raised: {e}")
            results.append(False)
    n_pass = sum(1 for r in results if r)
    n_total = len(results)
    print(f"\n{n_pass}/{n_total} extension-backend-host error-visibility tests passed")
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main_run())
