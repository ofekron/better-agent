from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
HOME = tempfile.mkdtemp(prefix="ba-switch-web-")
os.environ["BETTER_AGENT_HOME"] = HOME

from switch_control_daemon.line_switch_runtime import pointer  # noqa: E402
from switch_control_daemon.line_switch_runtime.control import _REQUIRED_CHECKOUT_FILES  # noqa: E402
from switch_control_daemon.line_switch_runtime.jsonio import write_json  # noqa: E402
from switch_control_daemon.line_switch_runtime.web import create_server  # noqa: E402


def checkout(path: Path) -> str:
    (path / "backend" / ".venv" / "bin").mkdir(parents=True)
    (path / "backend" / "main.py").write_text("", encoding="utf-8")
    (path / "backend" / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    for relative in _REQUIRED_CHECKOUT_FILES:
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    return str(path.resolve())


def request(
    base: str,
    path: str,
    *,
    token: str = "",
    body: object | None = None,
    origin: str = "",
    method: str | None = None,
) -> tuple[int, dict, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(req, timeout=2)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), dict(exc.headers)
    raw = response.read()
    return response.status, json.loads(raw) if raw else {}, dict(response.headers)


try:
    dev = checkout(Path(HOME) / "app")
    qa = checkout(Path(HOME) / "app-qa")
    main = checkout(Path(HOME) / "app-main")
    write_json(Path(HOME) / "switch_lines.json", {"dev": dev, "qa": qa, "main": main})
    pointer.set_active(dev, "seed")
    pointer.confirm_healthy(dev, "seed")

    token = "test-token-with-enough-entropy-for-auth"
    server = create_server(host="127.0.0.1", port=0, token=token)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        response = urllib.request.urlopen(base + "/", timeout=2)
        page = response.read().decode("utf-8")
        assert response.status == 200 and "Independent Better Agent control" in page
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert "unsafe-inline" not in response.headers["Content-Security-Policy"]
        assert "nonce-" in response.headers["Content-Security-Policy"]

        status, payload, _ = request(base, "/api/state")
        assert status == 401 and "authentication" in payload["error"]

        status, payload, _ = request(base, "/api/state", token=token)
        assert status == 200 and payload["active_line"] == "dev"
        assert sorted(payload["lines"]) == ["dev", "main", "qa"]

        status, payload, _ = request(base, "/api/switch", token=token, body={"target": "qa", "extra": True})
        assert status == 400 and "only" in payload["error"]

        status, payload, _ = request(base, "/api/switch", token=token, body={"target": "unknown"})
        assert status == 409 and "unknown line" in payload["error"]

        status, payload, _ = request(base, "/api/switch", token=token, body={"target": "qa"})
        assert status == 202 and payload["target"] == "qa" and payload["status"] == "pending"

        status, payload, _ = request(base, "/api/state", token=token)
        assert status == 200 and payload["request"]["target"] == "qa"

        status, _, headers = request(
            base,
            "/api/state",
            origin="http://localhost",
            method="OPTIONS",
        )
        assert status == 204 and headers["Access-Control-Allow-Origin"] == "http://localhost"
        assert "Authorization" in headers["Access-Control-Allow-Headers"]

        status, payload, _ = request(
            base,
            "/api/state",
            origin="https://attacker.example",
            method="OPTIONS",
        )
        assert status == 403 and "origin" in payload["error"]

        manifest = json.loads(urllib.request.urlopen(base + "/manifest.webmanifest", timeout=2).read())
        assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
        assert [icon["sizes"] for icon in manifest["icons"]] == ["192x192", "512x512"]
        icon = urllib.request.urlopen(base + "/icon-192.png", timeout=2).read()
        assert icon.startswith(b"\x89PNG\r\n\x1a\n")
        service_worker = urllib.request.urlopen(base + "/sw.js", timeout=2).read().decode("utf-8")
        assert "caches.match('/')" in service_worker and "/api/" in service_worker and "/icon-512.png" in service_worker
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    access = json.loads((Path(HOME) / "switch_control_web.json").read_text(encoding="utf-8"))
    assert len(access["token"]) >= 43 and access["port"] == 18768
    assert (Path(HOME) / "switch_control_web.json").stat().st_mode & 0o077 == 0
    print("OK test_switch_control_web")
finally:
    shutil.rmtree(HOME)
