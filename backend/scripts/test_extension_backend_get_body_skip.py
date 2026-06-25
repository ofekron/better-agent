from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-extension-get-body-skip-")
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import extension_backend_loader as loader  # noqa: E402


class _Request:
    method = "GET"
    scope = {"query_string": b""}
    base_url = "http://testserver/"

    @property
    def headers(self):
        class _Headers:
            raw: list[tuple[bytes, bytes]] = []

        return _Headers()

    async def stream(self):
        raise AssertionError("GET extension backend dispatch read the request body")
        yield b""


async def _main() -> int:
    captured: dict[str, bytes] = {}

    async def fake_invoke(spec, **kwargs):
        captured["body"] = kwargs["body_bytes"]
        return object()

    original_spec = loader.backend_entrypoint_spec_cached
    original_invoke = loader._invoke_backend
    loader.backend_entrypoint_spec_cached = lambda extension_id: {"extension_id": extension_id}  # type: ignore[assignment]
    loader._invoke_backend = fake_invoke  # type: ignore[assignment]
    try:
        await loader.dispatch_extension_backend_request("ext.get", "poll", _Request())  # type: ignore[arg-type]
    finally:
        loader.backend_entrypoint_spec_cached = original_spec  # type: ignore[assignment]
        loader._invoke_backend = original_invoke  # type: ignore[assignment]

    if captured.get("body") != b"":
        print(f"unexpected body: {captured!r}")
        return 1
    print("PASS test_extension_backend_get_body_skip")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
