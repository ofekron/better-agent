"""Inter-extension call endpoint (``POST /api/internal/extension-call``) gates.

Locks: token required; calling extension must be active; target must be an active
declared dependency and differ from caller; malformed routing -> 400; target
with no backend surface -> 404. Core only routes — it never bakes in feature logic.

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
import extension_backend_loader  # noqa: E402
from better_agent_sdk import Client  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

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
UNDECLARED = "test.icall-undeclared"


def _seed(extension_id: str, *, enabled: bool, dependencies: list[str] | None = None) -> None:
    data = extension_store._load()
    data["extensions"][extension_id] = {
        "manifest": {
            "id": extension_id,
            "permissions": {},
            "dependencies": list(dependencies or []),
        },
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


def _post_raw(content: bytes, *, content_type: str | None = "application/json", token=_SENTINEL):
    if token is _SENTINEL:
        token = extension_token_registry.mint(CALLER)
    headers = {"X-Internal-Token": token}
    if content_type is not None:
        headers["Content-Type"] = content_type
    return CLIENT.post("/api/internal/extension-call", content=content, headers=headers)


def main_test() -> int:
    _seed(CALLER, enabled=True, dependencies=[TARGET, INACTIVE])
    _seed(TARGET, enabled=True)
    _seed(INACTIVE, enabled=False)
    _seed(UNDECLARED, enabled=True)

    print("X1 token + active caller required")
    check(_post({"target_extension_id": TARGET, "path": "/x"}, extension_id=None).status_code in (403, 422),
          "missing token/extension rejected")
    check(_post({"target_extension_id": TARGET, "path": "/x"}, token="wrong").status_code == 403,
          "wrong token -> 403")
    check(_post({"target_extension_id": TARGET, "path": "/x"}, extension_id=INACTIVE).status_code == 403,
          "inactive caller -> 403")
    check(_post({"target_extension_id": TARGET, "path": "/x"}, extension_id=None).status_code == 403,
          "core token cannot act as an extension caller")

    print("X2 target validation")
    check(_post({"target_extension_id": CALLER, "path": "/x"}).status_code == 400,
          "target == caller -> 400")
    check(_post({"path": "/x"}).status_code == 400, "missing target -> 400")
    check(_post({"target_extension_id": TARGET}).status_code == 400, "missing path -> 400")
    check(_post({"target_extension_id": UNDECLARED, "path": "/x"}).status_code == 403,
          "active undeclared target -> 403")
    check(_post({"target_extension_id": "not-installed", "path": "/x"}).status_code == 403,
          "unknown undeclared target -> 403 without target enumeration")
    check(_post({"target_extension_id": INACTIVE, "path": "/x"}).status_code == 404,
          "declared inactive target -> 404")
    check(_post({"target_extension_id": TARGET, "path": "/x", "method": "BOGUS"}).status_code == 400,
          "bad method -> 400")
    check(_post({"target_extension_id": TARGET, "path": "/x", "body": []}).status_code == 400,
          "non-object inner body -> 400")
    check(_post({"target_extension_id": TARGET, "path": "/x", "extra": "smuggled"}).status_code == 400,
          "unexpected routing field -> 400")

    print("X3 canonical path, nesting, and serialized body bounds")
    captured: list[tuple[str, str, bytes]] = []
    original_invoke = extension_backend_loader.invoke_extension_backend

    async def _capture_invoke(target, path, *, method, body_bytes, base_url):
        captured.append((method, path, body_bytes))
        return JSONResponse({"ok": True})

    extension_backend_loader.invoke_extension_backend = _capture_invoke
    try:
        good = _post({"target_extension_id": TARGET, "path": "/cards/upsert-v1", "body": {"a": 1}})
        check(good.status_code == 200 and captured[-1][:2] == ("POST", "/cards/upsert-v1"),
              "canonical absolute extension-local path routes")
        invalid_paths = (
            "relative", "//x", "/x/", "/x//y", "/./x", "/x/../y",
            "/x\\y", "/x?query", "/x#fragment", "/x%2fy", "/x\x00y",
            "/ cafe", "/cafe ", "/cafe\u0301",
        )
        check(all(
            _post({"target_extension_id": TARGET, "path": path}).status_code == 400
            for path in invalid_paths
        ), "non-canonical and ambiguous paths -> 400")

        permitted_body: dict = {}
        for _ in range(30):
            permitted_body = {"x": permitted_body}
        too_deep_body = {"x": permitted_body}
        check(_post({"target_extension_id": TARGET, "path": "/x", "body": permitted_body}).status_code == 200,
              "raw envelope JSON depth 32 accepted")
        check(_post({"target_extension_id": TARGET, "path": "/x", "body": too_deep_body}).status_code == 400,
              "raw envelope JSON depth 33 rejected")

        import json
        raw_depth_33 = json.dumps({
            "target_extension_id": TARGET,
            "path": "/x",
            "body": too_deep_body,
        }).encode("utf-8")
        check(_post_raw(raw_depth_33).status_code == 400,
              "deeply nested raw JSON -> 400 before routing")
        check(_post_raw(b"").status_code == 400, "empty raw body -> 400")
        check(_post_raw(b'{"target_extension_id":').status_code == 400,
              "malformed raw JSON -> 400")
        check(_post_raw(b'[]').status_code == 400, "non-object raw JSON -> 400")
        check(_post_raw(b'{"x":NaN}').status_code == 400, "non-standard JSON constant -> 400")
        valid_raw = json.dumps({"target_extension_id": TARGET, "path": "/x"}).encode("utf-8")
        check(_post_raw(valid_raw, content_type=None).status_code == 400,
              "missing content-type -> 400")
        check(_post_raw(valid_raw, content_type="text/plain").status_code == 400,
              "wrong content-type -> 400")
        check(_post_raw(valid_raw, content_type="application/json; charset=utf-8").status_code == 200,
              "JSON content-type parameters accepted")

        limit = extension_backend_loader.REQUEST_BODY_MAX_BYTES
        under = {"x": "a" * (limit - len(b'{"x":""}') - 1)}
        boundary = {"x": "a" * (limit - len(b'{"x":""}'))}
        over = {"x": "a" * (limit - len(b'{"x":""}') + 1)}
        raw_oversize = json.dumps({
            "target_extension_id": TARGET,
            "path": "/x",
            "body": under,
        }, separators=(",", ":")).encode("utf-8")
        check(len(raw_oversize) > limit and _post_raw(raw_oversize).status_code == 413,
              "raw outer body over shared limit -> 413 before JSON parsing")
        check(_post_raw(raw_oversize, token="wrong").status_code == 403,
              "caller authentication precedes oversized-body disclosure")

        check(len(main._encode_extension_call_body(under)) == limit - 1,
              "serialized relay body one byte below limit is accepted")
        relay_statuses = []
        for value in (boundary, over):
            try:
                main._encode_extension_call_body(value)
            except Exception as exc:
                relay_statuses.append(getattr(exc, "status_code", None))
        check(relay_statuses == [413, 413], "serialized relay body at/over limit -> 413")
    finally:
        extension_backend_loader.invoke_extension_backend = original_invoke

    print("X4 active target without a backend surface -> 404 (graceful)")
    check(_post({"target_extension_id": TARGET, "path": "/anything", "body": {"a": 1}}).status_code == 404,
          "target has no backend -> 404 (not 500)")

    print("X5 SDK client call_extension hits the right path + payload")
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
