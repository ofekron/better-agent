"""Build Starlette Requests for direct route-function calls in tests."""

from __future__ import annotations

from starlette.requests import Request


def http_request(
    path: str = "/",
    *,
    query: str = "",
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: bytes = b"",
) -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": [
                (key.lower().encode(), value.encode())
                for key, value in (headers or {}).items()
            ],
            "client": ("test", 0),
            "server": ("test", 80),
        },
        receive=receive,
    )
