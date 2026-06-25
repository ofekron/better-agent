"""SPA fallback must NOT serve index.html for missing /assets/* bundles.

A missing content-hashed chunk served as 200 text/html makes the browser
reject it as a module and lazyWithRetry force-reloads the whole app on every
file-panel open. /assets/* misses must be real 404s.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths

_home = tempfile.mkdtemp(prefix="ba-spa-fallback-test-")
paths.engage_test_home(_home)
os.environ["BETTER_CLAUDE_API_ONLY"] = "1"

import main  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def run() -> None:
    with tempfile.TemporaryDirectory() as dist:
        dist_dir = Path(dist)
        (dist_dir / "assets").mkdir()
        (dist_dir / "index.html").write_text("<html>shell</html>", encoding="utf-8")
        (dist_dir / "assets" / "real-abc123.js").write_text("export default 1;", encoding="utf-8")

        app = FastAPI()
        main.mount_frontend(app, dist_dir=dist_dir)
        client = TestClient(app)

        r = client.get("/assets/missing-chunk-zzz.js")
        assert r.status_code == 404, f"missing asset must 404, got {r.status_code}"
        assert "text/html" not in r.headers.get("content-type", ""), (
            "missing asset must not be served as HTML"
        )

        r = client.get("/assets/real-abc123.js")
        assert r.status_code == 200, f"existing asset must serve, got {r.status_code}"
        assert "shell" not in r.text

        r = client.get("/s/some-session-id")
        assert r.status_code == 200 and "shell" in r.text, "SPA routes must serve index.html"

        r = client.get("/api/definitely-not-a-route-zzz")
        assert r.status_code == 404
        assert r.json() == {"detail": "Not Found"}

        r = client.get("/")
        assert r.status_code == 200 and "shell" in r.text
        assert "no-store" in r.headers.get("cache-control", ""), "index.html must be no-cache"

    print("test_spa_fallback_assets_404: OK")


if __name__ == "__main__":
    try:
        run()
    finally:
        shutil.rmtree(_home, ignore_errors=True)
