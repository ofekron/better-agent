"""Test-only authentication helpers.

Tests must exercise the normal bearer-token verification path instead
of disabling auth. These helpers mint a signed token using the same
`auth.create_token` function as `/api/auth/login`, then attach it to
test clients. They do not add any server-side bypass.
"""

from __future__ import annotations

from typing import Any

TEST_USERNAME = "test"
TEST_PASSWORD = "test-password"


def authenticate_client(client: Any) -> str:
    """Attach a valid bearer token to a synchronous TestClient."""
    import auth

    token = auth.create_token(TEST_USERNAME)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return token


async def authenticate_async_client(client: Any) -> str:
    """Attach a valid bearer token to an async HTTP client."""
    import auth

    token = auth.create_token(TEST_USERNAME)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return token


async def internal_post(
    client: Any,
    path: str,
    payload: dict | None,
    internal_token: str,
    timeout: Any = None,
) -> Any:
    """POST to a runtime `/api/internal/*` route with HMAC request signing.

    The auth gate rejects a raw core bearer; possession of the internal
    token must be proven via signature headers over method|path|body
    (see internal_request_auth). The body is serialized here so the
    signed hash matches the exact bytes sent."""
    import json

    import internal_request_auth

    body = json.dumps(payload or {}).encode("utf-8")
    headers = {
        "X-Internal-Token": internal_token,
        "Content-Type": "application/json",
        **internal_request_auth.sign(internal_token, "POST", path, body),
    }
    kwargs: dict[str, Any] = {"content": body, "headers": headers}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return await client.post(path, **kwargs)
