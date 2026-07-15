"""Better Agent BFF — the browser-facing process (plan Phase 3).

Serves the built SPA and passes /api, /ws, /healthz, and
/provider-config-sync through to the runtime's internal endpoint
(unix socket on POSIX, 127.0.0.1 on Windows — resolved from the
runtime endpoint descriptor, fail closed when absent).

Owns Better Agent application state and imports none of the runtime
core: killing or restarting this process never touches execution
sessions, runners, or recovery. Run via `python -m uvicorn bff_server:app` or
`better-agent start-bff`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from bff_app_routes import (
    chat_draft_session_id,
    initialize_app_projects,
    owns_path as app_owns_path,
    router as app_router,
)
from bff_event_hub import hub
from bff_runtime_service import RuntimeServiceError, runtime_service
from bff_runtime_upstream import RuntimeUpstreamUnavailable, runtime_upstream
import bff_chat_feed
from bff_chat_tree import router as chat_tree_router
import bff_projection
import app_chat_draft_store
from frontend_assets import (
    NO_CACHE_HEADERS,
    NoCacheIndexStaticFiles,
    frontend_dist_dir,
)

app = FastAPI(title="better-agent-bff")
app.include_router(app_router)
app.include_router(chat_tree_router)

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
    lease = await runtime_upstream.acquire()
    await lease.release()
    runtime_service.bind(runtime_upstream)
    await initialize_app_projects()
    await bff_chat_feed.feed_client.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await bff_chat_feed.feed_client.stop()
    runtime_service.unbind()
    await runtime_upstream.shutdown()


def _browser_identity_headers(request: Request) -> list[tuple[bytes, bytes]]:
    dropped = _HOP_BY_HOP | {"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"}
    headers = [
        (key, value)
        for key, value in request.headers.raw
        if key.decode("latin-1").lower() not in dropped
    ]
    if request.headers.get("cookie") is None:
        headers.append((b"cookie", b""))
    client_host = request.client.host if request.client else "127.0.0.1"
    headers.append((b"x-forwarded-for", client_host.encode("latin-1")))
    headers.append((b"x-forwarded-proto", request.url.scheme.encode("latin-1")))
    # The runtime's own Host header reflects this loopback hop (e.g.
    # 127.0.0.1:<runtime-port>), not what the browser actually addressed —
    # httpx sets Host from the upstream connection, and the inbound Host is
    # dropped above (spoofable, like the other _HOP_BY_HOP entries). Without
    # this, backend/browser_trust.py's Origin-vs-Host same-origin check
    # compares the browser's real port against the runtime's internal port
    # and always fails. Trusted the same way X-Forwarded-For already is:
    # the runtime binds loopback-only, so only a same-uid local process
    # (this BFF) can ever be the one setting it.
    browser_host = request.headers.get("host") or request.url.netloc
    if browser_host:
        headers.append((b"x-forwarded-host", browser_host.encode("latin-1")))
    return headers


@app.middleware("http")
async def authenticate_app_routes(request: Request, call_next):
    if not app_owns_path(request.method, request.url.path):
        return await call_next(request)
    identity_headers = _browser_identity_headers(request)
    lease = None
    try:
        lease = await runtime_upstream.acquire()
        response = await lease.client.get(
            "/api/auth/me",
            headers=identity_headers,
            timeout=5.0,
        )
    except (
        httpx.HTTPError,
        RuntimeServiceError,
        RuntimeUpstreamUnavailable,
        OSError,
    ):
        return JSONResponse({"detail": "runtime unavailable"}, status_code=503)
    finally:
        if lease is not None:
            await lease.release()
    if response.status_code != 200:
        return JSONResponse({"detail": "unauthenticated"}, status_code=401)
    try:
        auth_user = response.json()
    except ValueError:
        return JSONResponse({"detail": "invalid runtime identity"}, status_code=502)
    if not isinstance(auth_user, dict):
        return JSONResponse({"detail": "invalid runtime identity"}, status_code=502)
    request.state.auth_user = auth_user
    session_id = chat_draft_session_id(request.method, request.url.path)
    if session_id is not None:
        lease = None
        try:
            lease = await runtime_upstream.acquire()
            exists = await lease.client.get(
                f"/api/sessions/{session_id}/stats",
                headers=identity_headers,
                timeout=5.0,
            )
        except (
            httpx.HTTPError,
            RuntimeServiceError,
            RuntimeUpstreamUnavailable,
            OSError,
        ):
            return JSONResponse({"detail": "runtime unavailable"}, status_code=503)
        finally:
            if lease is not None:
                await lease.release()
        if exists.status_code == 404:
            return JSONResponse({"detail": "session not found"}, status_code=404)
        if exists.status_code != 200:
            return JSONResponse({"detail": "session validation failed"}, status_code=502)
    return await call_next(request)


@app.get("/bff/healthz")
async def bff_health() -> JSONResponse:
    runtime_ok = False
    lease = None
    with contextlib.suppress(
        httpx.HTTPError,
        RuntimeServiceError,
        RuntimeUpstreamUnavailable,
        OSError,
    ):
        lease = await runtime_upstream.acquire()
        runtime_ok = (
            await lease.client.get("/healthz", timeout=3.0)
        ).status_code == 200
    if lease is not None:
        await lease.release()
    return JSONResponse({"ok": True, "runtime": runtime_ok})


async def _proxy(request: Request) -> Response:
    try:
        lease = await runtime_upstream.acquire()
    except (RuntimeServiceError, RuntimeUpstreamUnavailable, OSError):
        return JSONResponse({"detail": "runtime unavailable"}, status_code=503)
    url = httpx.URL(path=request.url.path, query=request.url.query.encode("utf-8"))
    # Strip inbound forwarding headers (spoofable) and stamp the REAL
    # browser peer: the runtime's proxy-headers handling turns this
    # into `request.client`, so its loopback/remote auth decisions stay
    # correct behind the BFF.
    headers = _browser_identity_headers(request)
    upstream_request = lease.client.build_request(
        request.method, url, headers=headers, content=request.stream()
    )
    try:
        upstream = await lease.client.send(upstream_request, stream=True)
    except httpx.HTTPError:
        await lease.release()
        return JSONResponse({"detail": "runtime unavailable"}, status_code=502)
    response_headers = [
        (key, value)
        for key, value in upstream.headers.raw
        if key.decode("latin-1").lower() not in _HOP_BY_HOP
    ]
    if (
        upstream.status_code < 400
        and bff_projection.needs_json_projection(request.url.path)
        and "application/json" in upstream.headers.get("content-type", "")
    ):
        raw = await upstream.aread()
        await upstream.aclose()
        await lease.release()
        payload = bff_projection.project_json(
            request.url.path,
            json.loads(raw),
        )
        response = JSONResponse(payload, status_code=upstream.status_code)
        response.raw_headers.extend(
            (key, value)
            for key, value in response_headers
            if key.decode("latin-1").lower()
            not in {"content-encoding", "content-length", "content-type"}
        )
        return response
    if (
        request.method == "DELETE"
        and upstream.status_code < 400
        and request.url.path.startswith("/api/sessions/")
        and request.url.path.count("/") == 3
    ):
        session_id = request.url.path.rsplit("/", 1)[-1]
        await asyncio.to_thread(app_chat_draft_store.delete, session_id)
    response = StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        background=BackgroundTask(_close_upstream_response, upstream, lease),
    )
    response.raw_headers = response_headers
    return response


async def _close_upstream_response(
    response: httpx.Response, lease
) -> None:
    await response.aclose()
    await lease.release()


@app.api_route(
    "/api/bff-runtime/{_path:path}", methods=_PROXY_METHODS, include_in_schema=False
)
async def block_runtime_contract(_path: str) -> JSONResponse:
    return JSONResponse({"detail": "Not Found"}, status_code=404)


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
    # See the matching comment in _browser_identity_headers: the runtime's
    # own Host reflects this loopback hop, not what the browser addressed,
    # so browser_trust's Origin-vs-Host check needs the real one.
    browser_host = websocket.headers.get("host")
    if browser_host:
        forwarded.append(("x-forwarded-host", browser_host))
    return forwarded


@app.websocket("/ws/{_path:path}")
async def proxy_ws(websocket: WebSocket, _path: str) -> None:
    try:
        lease = await runtime_upstream.acquire()
    except (RuntimeServiceError, RuntimeUpstreamUnavailable, OSError):
        await websocket.close(code=1013)
        return
    descriptor = lease.descriptor
    await lease.release()
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
    connection = hub.attach(websocket)

    async def browser_to_upstream() -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("text")
            if data is None:
                data = message.get("bytes")
            if data is not None:
                if isinstance(data, str):
                    with contextlib.suppress(json.JSONDecodeError):
                        message = json.loads(data)
                        message_type = message.get("type")
                        session_id = message.get("app_session_id")
                        if message_type == "subscribe" and isinstance(session_id, str):
                            hub.subscribe(connection, session_id)
                        elif message_type == "unsubscribe" and isinstance(session_id, str):
                            hub.unsubscribe(connection, session_id)
                await upstream.send(data)

    async def upstream_to_browser() -> None:
        async for frame in upstream:
            await connection.send_frame(frame)

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
    hub.detach(connection)
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
