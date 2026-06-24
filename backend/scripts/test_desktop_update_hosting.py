from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import _test_home
_test_home.isolate("bc-desktop-host-")

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402
from paths import ba_home  # noqa: E402
import main  # noqa: E402


def test_desktop_status_downloads_and_update_repo_are_hosted_from_ba_home():
    home = ba_home()
    downloads = home / "desktop" / "downloads"
    metadata = home / "desktop" / "updates" / "repository" / "metadata"
    downloads.mkdir(parents=True)
    metadata.mkdir(parents=True)
    (downloads / "BetterAgent.dmg").write_bytes(b"dmg")
    (downloads / "BetterAgentSetup.exe").write_bytes(b"exe")
    (metadata / "root.json").write_text('{"signed": {}}', encoding="utf-8")

    client = TestClient(main.app, base_url="http://127.0.0.1:8123")
    status = client.get("/api/desktop/status")
    assert status.status_code == 200
    body = status.json()
    assert body["macos"] is True
    assert body["windows"] is True
    assert body["update_repo"] is True
    assert body["desktop_shell"] is False
    assert body["update_url"].endswith(":8123/api/desktop/updates")
    assert body["update_url"].startswith("http://")

    mac = client.get("/api/download/desktop/macos")
    assert mac.status_code == 200
    assert mac.content == b"dmg"

    win = client.get("/api/download/desktop/windows")
    assert win.status_code == 200
    assert win.content == b"exe"

    root = client.get("/api/desktop/updates/metadata/root.json")
    assert root.status_code == 200
    assert root.json() == {"signed": {}}


def test_desktop_update_repo_rejects_path_traversal():
    client = TestClient(main.app)
    res = client.get("/api/desktop/updates/missing")
    assert res.status_code == 404
    try:
        main._desktop_update_file("../outside")
    except main.HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("path traversal was not rejected")
