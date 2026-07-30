"""SPA static-file mount, its cold-clone placeholder, and the SPA 404 fallback."""

import asyncio
import logging
import sys as _sys
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import FastAPI, Request as _Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse as _HTMLResponse,
    JSONResponse as _JSONResponse,
)
from fastapi.staticfiles import StaticFiles

from env_compat import get_env
from ops_api import _trigger_supervisor_restart

logger = logging.getLogger(__name__)

# Make `index.html` non-cacheable so a reload (browser ↻ or Capacitor
# WebView reload after the in-app restart button) always re-fetches
# the SPA shell. The shell references content-hashed JS/CSS bundles
# (Vite default), so once HTML is fresh the WebView pulls the new
# bundles via normal cache-miss. Web tabs get the same guarantee on
# top of the SW skipWaiting+clientsClaim flow. WITHOUT this header,
# WKWebView's HTTP cache can serve a stale index.html that still
# points at the OLD hashed bundles, leaving the user on the previous
# build even after the refresh button completes.
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


class _NoCacheIndexStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        # `path` is the path RELATIVE to the mount root; the bare-mount
        # root "" and the explicit "index.html" both resolve to the SPA
        # shell. Everything else (hashed bundles, icons, manifest) keeps
        # the default long-cache behaviour StaticFiles already grants.
        if path in ("", ".", "index.html"):
            for k, v in _NO_CACHE_HEADERS.items():
                response.headers[k] = v
        return response


def frontend_dist_dir() -> Path:
    if getattr(_sys, "frozen", False):
        # PyInstaller bundle: the built frontend is bundled as data under the
        # extraction root `sys._MEIPASS` (see desktop/BetterAgent.spec).
        return Path(_sys._MEIPASS) / "frontend_dist"
    return Path(__file__).resolve().parent.parent / "frontend" / "dist"


# Placeholder served while run.sh builds the frontend on a cold clone (no built
# dist/ yet). Auto-refreshes so the browser picks up the real app once the build
# lands and the supervisor restarts the backend. English-only by design — it is
# a pre-React bootstrap page, not part of the i18n surface.
_COLD_BUILDING_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Building frontend…</title>
<style>
  html,body{height:100%;margin:0}
  body{display:flex;align-items:center;justify-content:center;
       font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
       color:#1f2937;background:#f8fafc}
  .card{padding:1.75rem 2.25rem;border-radius:12px;background:#fff;
        box-shadow:0 1px 3px rgba(0,0,0,.08);text-align:center}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;
       background:#3b82f6;margin-right:9px;vertical-align:middle;
       animation:_p 1s ease-in-out infinite}
  @keyframes _p{0%,100%{opacity:.3}50%{opacity:1}}
</style>
</head>
<body><div class="card"><span class="dot"></span>Building frontend…</div></body>
</html>
"""

_COLD_BUILD_CHECK_INTERVAL = 1.0

# Startup tasks armed during module load (the cold-build watcher) but launched
# by `app_lifespan` after `on_startup`. Starlette raises when a lifespan is set
# and a legacy router event handler is registered, so this list is the single
# drain point.
_deferred_startup_tasks: list[Callable[[], Awaitable[None]]] = []
_cold_build_watcher_armed = False


def _mount_cold_build_stub(target_app: FastAPI) -> None:
    """Serve a placeholder while the frontend builds on a cold clone.

    Registered last, so every real API/WS route above still matches first.
    The supervisor restarts the backend once the build lands, after which
    mount_frontend mounts the real dist instead.
    """

    @target_app.get("/{full_path:path}", include_in_schema=False)
    async def _cold_build_placeholder(full_path: str):
        return _HTMLResponse(_COLD_BUILDING_HTML, headers=_NO_CACHE_HEADERS)


def _arm_cold_build_restart(target_app: FastAPI, dist_index: Path) -> None:
    """Restart the backend once the frontend build lands a real dist.

    The run.sh supervisor is the only thing that can respawn us, so this is a
    no-op (placeholder served until a manual restart) without it. One-shot:
    after the restart, mount_frontend sees the dist and takes the normal
    StaticFiles branch, so the watcher never re-arms.
    """
    global _cold_build_watcher_armed
    if get_env("BETTER_CLAUDE_RUN_SH_SUPERVISOR") != "1":
        logger.info(
            "cold frontend build: supervisor absent; placeholder served until manual restart"
        )
        return

    if _cold_build_watcher_armed:
        return
    _cold_build_watcher_armed = True

    async def _watch_for_build():
        try:
            while not dist_index.exists():
                await asyncio.sleep(_COLD_BUILD_CHECK_INTERVAL)
            # Empty request id: the build already completed (that is why we
            # are restarting), so run.sh must not rebuild again on the way
            # back up — an empty PENDING_REFRESH_ID skips start_frontend_build.
            logger.info("cold frontend build landed; requesting supervisor restart")
            await _trigger_supervisor_restart("")
        except Exception:
            logger.exception("cold-build restart watcher failed")

    async def _start_watcher():
        asyncio.create_task(_watch_for_build())

    _deferred_startup_tasks.append(_start_watcher)


def mount_frontend(target_app: FastAPI, *, dist_dir: Path | None = None) -> None:
    """Mount the built React frontend onto an already-registered API app.

    Registered AFTER every `@app.get("/api/...")` / `@app.websocket(...)`
    route above so explicit routes still match first; only unmatched paths
    fall through to StaticFiles.
    """
    resolved_dist_dir = dist_dir or frontend_dist_dir()
    if not resolved_dist_dir.exists():
        if dist_dir is not None:
            # An explicit caller (e.g. tests) asked for a specific dist and did
            # not provide one — fail loudly rather than silently serving a stub.
            raise RuntimeError(
                f"frontend dist directory not found at {resolved_dist_dir}. "
                "Run `cd frontend && npm run build` (or use ./run.sh which does it)."
            )
        # Cold clone with no built frontend yet: serve a placeholder so the API
        # is usable immediately while run.sh builds in the background, then arm
        # a one-shot supervisor restart to swap in the real dist when it lands.
        _mount_cold_build_stub(target_app)
        _arm_cold_build_restart(target_app, resolved_dist_dir / "index.html")
        return

    target_app.mount(
        "/",
        _NoCacheIndexStaticFiles(directory=str(resolved_dist_dir), html=True),
        name="frontend",
    )

    # SPA fallback. StaticFiles returns 404 for any path that isn't an
    # actual file in dist/, so direct navigation to a client-side route
    # (e.g. refresh on /s/<id>, or any future route) breaks. Catch 404
    # on non-API paths and serve index.html instead — React then mounts
    # and `useRoute` parses the URL.
    #
    # /api/* and /ws/* 404s keep returning JSON so REST clients aren't
    # fooled by an HTML body on a missing endpoint.
    @target_app.exception_handler(404)
    async def _spa_fallback(request: _Request, _exc):
        p = request.url.path
        if p.startswith("/api/") or p.startswith("/ws/"):
            # Preserve handler-raised diagnostics (e.g. internal-guard
            # "X is not installed") instead of flattening every API 404
            # to a generic body that hides the real refusal reason.
            detail = getattr(_exc, "detail", None) or "Not Found"
            return _JSONResponse({"detail": detail}, status_code=404)
        # Hashed bundles must 404 for real: serving index.html as a module
        # script makes the browser throw an opaque MIME error instead of a
        # clean missing-chunk failure.
        if p.startswith("/assets/"):
            return _JSONResponse({"detail": "Not Found"}, status_code=404)
        return FileResponse(resolved_dist_dir / "index.html", headers=_NO_CACHE_HEADERS)
