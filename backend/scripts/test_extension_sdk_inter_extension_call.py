"""Inter-extension call endpoint (``POST /api/internal/extension-call``) gates.

Locks: token required; calling extension must be active; target must be active
and differ from caller; missing target/path or bad method -> 400; target with
no backend surface -> 404. Core only routes — it never bakes in feature logic.

Run standalone:  python scripts/test_extension_sdk_inter_extension_call.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-sdkcall-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"
os.environ["BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH"] = _TMP_HOME

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
_REPO = os.path.dirname(_BACKEND)
for _p in (_BACKEND, os.path.join(_REPO, "sdk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from starlette.testclient import TestClient  # noqa: E402
import main  # noqa: E402
import extension_store  # noqa: E402
import extension_token_registry  # noqa: E402
from better_agent_sdk import Client  # noqa: E402

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


CLIENT = TestClient(main.app, client=("127.0.0.1", 50004))
TOKEN = main.coordinator.internal_token
CALLER = "test.icall-caller"
TARGET = "test.icall-target"
INACTIVE = "test.icall-inactive"


def _seed(extension_id: str, *, enabled: bool) -> None:
    data = extension_store._load()
    data["extensions"][extension_id] = {
        "manifest": {"id": extension_id, "permissions": {}},
        "enabled": enabled,
        "source": {"type": "git", "install_path": ""},
        "entitlement": {"status": "not_required"},
    }
    extension_store._save(data)


_SENTINEL = object()


def _post(body, extension_id=CALLER, token=_SENTINEL):
    # Identity is derived from the token alone. Acting as a given extension
    # means sending THAT extension's minted token. extension_id=None means
    # "no extension identity" -> use the core token (principal "core", no ext).
    if token is _SENTINEL:
        token = TOKEN if extension_id is None else extension_token_registry.mint(extension_id)
    headers = {"X-Internal-Token": token}
    return CLIENT.post("/api/internal/extension-call", json=body, headers=headers)


def main_test() -> int:
    _seed(CALLER, enabled=True)
    _seed(TARGET, enabled=True)
    _seed(INACTIVE, enabled=False)

    print("X1 token + active caller required")
    check(_post({"target_extension_id": TARGET, "path": "/x"}, extension_id=None).status_code in (403, 422),
          "missing token/extension rejected")
    check(_post({"target_extension_id": TARGET, "path": "/x"}, token="wrong").status_code == 403,
          "wrong token -> 403")
    check(_post({"target_extension_id": TARGET, "path": "/x"}, extension_id=INACTIVE).status_code == 403,
          "inactive caller -> 403")

    print("X2 target validation")
    check(_post({"target_extension_id": CALLER, "path": "/x"}).status_code == 400,
          "target == caller -> 400")
    check(_post({"path": "/x"}).status_code == 400, "missing target -> 400")
    check(_post({"target_extension_id": TARGET}).status_code == 400, "missing path -> 400")
    check(_post({"target_extension_id": "not-installed", "path": "/x"}).status_code == 404,
          "inactive/unknown target -> 404")
    check(_post({"target_extension_id": TARGET, "path": "/x", "method": "BOGUS"}).status_code == 400,
          "bad method -> 400")

    print("X3 active target without a backend surface -> 404 (graceful)")
    check(_post({"target_extension_id": TARGET, "path": "/anything", "body": {"a": 1}}).status_code == 404,
          "target has no backend -> 404 (not 500)")

    print("X4 SDK client call_extension hits the right path + payload")
    import json
    import urllib.request
    captured: dict = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"success": true}'

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = json.loads(req.data.decode("utf-8")) if req.data else None
        return _FakeResp()

    original_urlopen = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen
    try:
        Client(internal_token="tok", extension_id=CALLER, backend_url="http://core").call_extension(
            TARGET, "/foo", {"a": 1}
        )
        check(captured["url"].endswith("/api/internal/extension-call")
              and captured["data"] == {"target_extension_id": TARGET, "path": "/foo", "method": "POST", "body": {"a": 1}},
              "call_extension -> right path + payload")
    finally:
        urllib.request.urlopen = original_urlopen

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: extension sdk inter-extension call endpoint")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main_test())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
