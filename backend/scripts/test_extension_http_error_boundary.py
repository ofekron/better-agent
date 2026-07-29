from __future__ import annotations

import io
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
SDK = BACKEND.parent / "sdk"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(SDK))

import extension_backend_host  # noqa: E402
from better_agent_sdk import BetterAgentHTTPError, Client  # noqa: E402


def _http_error(status: int, detail: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://core.test/api/internal/capabilities/invoke",
        status,
        "error",
        {},
        io.BytesIO(detail),
    )


def test_sdk_preserves_safe_http_status_and_detail() -> None:
    client = Client(backend_url="http://core.test", internal_token="not-a-secret")
    with patch(
        "better_agent_sdk.client._urlopen_with_retry",
        side_effect=_http_error(403, b'{"detail":"capability action is not granted"}'),
    ):
        try:
            client.invoke_capability("ask", "ensure")
        except BetterAgentHTTPError as exc:
            assert exc.status_code == 403
            assert exc.detail == "capability action is not granted"
            assert "not-a-secret" not in str(exc)
        else:
            raise AssertionError("SDK flattened a core HTTP permission denial")

    with patch(
        "better_agent_sdk.client._urlopen_with_retry",
        side_effect=_http_error(
            422,
            b'{"detail":[{"msg":"invalid","input":"secret-value"}]}',
        ),
    ):
        try:
            client.invoke_capability("ask", "ensure")
        except BetterAgentHTTPError as exc:
            assert exc.status_code == 422
            assert exc.detail == "error"
            assert "secret-value" not in str(exc)
        else:
            raise AssertionError("SDK reflected structured validation input")


def test_extension_host_preserves_4xx_and_sanitizes_5xx() -> None:
    app: FastAPI = extension_backend_host._new_app()

    @app.get("/denied")
    async def denied() -> None:
        raise BetterAgentHTTPError(403, "capability action is not granted")

    @app.get("/fault")
    async def fault() -> None:
        raise BetterAgentHTTPError(500, "secret traceback /tmp/private")

    client = TestClient(app)
    denied_response = client.get("/denied")
    assert denied_response.status_code == 403
    assert denied_response.json() == {
        "detail": "capability action is not granted",
    }
    fault_response = client.get("/fault")
    assert fault_response.status_code == 502
    assert fault_response.json() == {
        "detail": "Extension core capability failed",
    }
    assert "secret" not in fault_response.text.lower()
    assert "traceback" not in fault_response.text.lower()


def main() -> None:
    test_sdk_preserves_safe_http_status_and_detail()
    test_extension_host_preserves_4xx_and_sanitizes_5xx()
    print("PASS test_extension_http_error_boundary")


if __name__ == "__main__":
    main()
