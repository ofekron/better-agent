"""Frontend asset serving shared by the runtime app and the BFF.

Single source for the dist-dir resolution and the no-cache SPA-shell
static handler; `main.mount_frontend` (monolith, plus its cold-build
supervisor machinery) and `bff_server` (decoupled browser process)
both build on these.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.staticfiles import StaticFiles

# Make `index.html` non-cacheable so a reload (browser ↻ or Capacitor
# WebView reload after the in-app restart button) always re-fetches
# the SPA shell. The shell references content-hashed JS/CSS bundles
# (Vite default), so once HTML is fresh the WebView pulls the new
# bundles via normal cache-miss. WITHOUT this header, WKWebView's HTTP
# cache can serve a stale index.html that still points at the OLD
# hashed bundles, leaving the user on the previous build even after
# the refresh button completes.
NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


class NoCacheIndexStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        # `path` is the path RELATIVE to the mount root; the bare-mount
        # root "" and the explicit "index.html" both resolve to the SPA
        # shell. Everything else (hashed bundles, icons, manifest) keeps
        # the default long-cache behaviour StaticFiles already grants.
        if path in ("", ".", "index.html"):
            for k, v in NO_CACHE_HEADERS.items():
                response.headers[k] = v
        return response


def frontend_dist_dir() -> Path:
    if getattr(sys, "frozen", False):
        # PyInstaller bundle: the built frontend is bundled as data under the
        # extraction root `sys._MEIPASS` (see desktop/BetterAgent.spec).
        return Path(sys._MEIPASS) / "frontend_dist"
    return Path(__file__).resolve().parent.parent / "frontend" / "dist"
