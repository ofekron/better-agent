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
