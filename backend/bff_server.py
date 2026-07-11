"""Better Agent BFF — the browser-facing process (plan Phase 3).

Serves the built SPA and passes /api, /ws, /healthz, and
/provider-config-sync through to the runtime's internal endpoint
(unix socket on POSIX, 127.0.0.1 on Windows — resolved from the
runtime endpoint descriptor, fail closed when absent).

Owns NO session state and imports none of the runtime core: killing
or restarting this process never touches sessions, runners, or
recovery. Run via `python -m uvicorn bff_server:app` or
`better-agent start-bff`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

import runtime_endpoints
from frontend_assets import (
    NO_CACHE_HEADERS,
    NoCacheIndexStaticFiles,
    frontend_dist_dir,
)

app = FastAPI(title="better-agent-bff")

_client: httpx.AsyncClient | None = None
_descriptor: dict | None = None

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}
_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


def _dist_dir() -> Path:
    override = os.environ.get("BETTER_AGENT_BFF_DIST")
    return Path(override) if override else frontend_dist_dir()


@app.on_event("startup")
async def _startup() -> None:
    global _client, _descriptor
    _descriptor = runtime_endpoints.read_app_endpoint()  # fail closed at boot
    if _descriptor["kind"] == "uds":
        transport = httpx.AsyncHTTPTransport(uds=_descriptor["path"])
        base_url = "http://better-agent-runtime"
    else:
        transport = None
        base_url = f"http://{_descriptor['host']}:{_descriptor['port']}"
    _client = httpx.AsyncClient(
        transport=transport,
        base_url=base_url,
        timeout=httpx.Timeout(300.0, connect=10.0),
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


@app.get("/bff/healthz")
async def bff_health() -> JSONResponse:
    runtime_ok = False
    if _client is not None:
        with contextlib.suppress(httpx.HTTPError):
            runtime_ok = (await _client.get("/healthz", timeout=3.0)).status_code == 200
    return JSONResponse({"ok": True, "runtime": runtime_ok})


async def _proxy(request: Request) -> Response:
    if _client is None:
        return JSONResponse({"detail": "bff not started"}, status_code=503)
    url = httpx.URL(path=request.url.path, query=request.url.query.encode("utf-8"))
    # Strip inbound forwarding headers (spoofable) and stamp the REAL
    # browser peer: the runtime's proxy-headers handling turns this
    # into `request.client`, so its loopback/remote auth decisions stay
    # correct behind the BFF.
    dropped = _HOP_BY_HOP | {"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"}
    headers = [
        (k, v)
        for k, v in request.headers.raw
        if k.decode("latin-1").lower() not in dropped
    ]
    client_host = request.client.host if request.client else "127.0.0.1"
    headers.append((b"x-forwarded-for", client_host.encode("latin-1")))
    headers.append((b"x-forwarded-proto", request.url.scheme.encode("latin-1")))
    upstream_request = _client.build_request(
        request.method, url, headers=headers, content=request.stream()
    )
    try:
        upstream = await _client.send(upstream_request, stream=True)
    except httpx.HTTPError:
        return JSONResponse({"detail": "runtime unavailable"}, status_code=502)
    response_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP
    }
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=response_headers,
        background=BackgroundTask(upstream.aclose),
    )


@app.api_route("/api/{_path:path}", methods=_PROXY_METHODS, include_in_schema=False)
async def proxy_api(request: Request, _path: str) -> Response:
    return await _proxy(request)


@app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
async def proxy_healthz(request: Request) -> Response:
    return await _proxy(request)


@app.api_route(
    "/provider-config-sync{_path:path}", methods=["GET"], include_in_schema=False
)
async def proxy_provider_config_sync(request: Request, _path: str) -> Response:
    return await _proxy(request)


def _ws_forward_headers(websocket: WebSocket) -> list[tuple[str, str]]:
    # Carry the browser's identity to the runtime's WS auth gate: the
    # cookie/bearer it validates, and the Origin it checks. Inbound
    # forwarding headers are dropped and the real peer is re-stamped, so
    # the runtime's loopback/remote decision stays correct behind us.
    forwarded: list[tuple[str, str]] = []
    for name in ("cookie", "authorization", "origin", "sec-websocket-protocol"):
        value = websocket.headers.get(name)
        if value is not None:
            forwarded.append((name, value))
    client_host = websocket.client.host if websocket.client else "127.0.0.1"
    forwarded.append(("x-forwarded-for", client_host))
    forwarded.append(("x-forwarded-proto", websocket.url.scheme))
    return forwarded


@app.websocket("/ws/{_path:path}")
async def proxy_ws(websocket: WebSocket, _path: str) -> None:
    descriptor = _descriptor
    if descriptor is None:
        await websocket.close(code=1013)
        return
    target = websocket.url.path
    if websocket.url.query:
        target += f"?{websocket.url.query}"
    headers = _ws_forward_headers(websocket)
    try:
        if descriptor["kind"] == "uds":
            upstream = await websockets.unix_connect(
                descriptor["path"],
                uri=f"ws://better-agent-runtime{target}",
                additional_headers=headers,
            )
        else:
            upstream = await websockets.connect(
                f"ws://{descriptor['host']}:{descriptor['port']}{target}",
                additional_headers=headers,
            )
    except (OSError, websockets.exceptions.WebSocketException):
        await websocket.close(code=1013)  # runtime unreachable: try later
        return

    await websocket.accept()

    async def browser_to_upstream() -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("text")
            if data is None:
                data = message.get("bytes")
            if data is not None:
                await upstream.send(data)

    async def upstream_to_browser() -> None:
        async for frame in upstream:
            if isinstance(frame, str):
                await websocket.send_text(frame)
            else:
                await websocket.send_bytes(frame)

    pumps = {
        asyncio.create_task(browser_to_upstream()),
        asyncio.create_task(upstream_to_browser()),
    }
    _done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    with contextlib.suppress(Exception):
        await upstream.close()
    close_code = getattr(upstream, "close_code", None)
    with contextlib.suppress(RuntimeError):
        await websocket.close(
            code=close_code if isinstance(close_code, int) and 1000 <= close_code < 5000 else 1000
        )


def _mount_spa() -> None:
    dist = _dist_dir()
    if not dist.exists():
        # No built frontend: the BFF still proxies the API; the SPA 404s
        # honestly instead of serving a stale or fake shell.
        return
    app.mount(
        "/", NoCacheIndexStaticFiles(directory=str(dist), html=True), name="frontend"
    )

    @app.exception_handler(404)
    async def _spa_fallback(request: Request, _exc):  # noqa: ANN001
        path = request.url.path
        if path.startswith("/api/") or path.startswith("/ws/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return FileResponse(dist / "index.html", headers=NO_CACHE_HEADERS)


_mount_spa()
