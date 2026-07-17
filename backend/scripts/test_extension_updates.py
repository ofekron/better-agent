from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-extension-updates-home-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import extension_store  # noqa: E402

_EXTENSION_ID = "ofek.update-check"
_KEY = Ed25519PrivateKey.generate()
_PUBLIC_KEY = _KEY.public_key().public_bytes_raw().hex()

# In-process mock marketplace: path -> response bytes, plus request counters.
_server_lock = threading.Lock()
_server_routes: dict[str, tuple[str, bytes]] = {}
_server_hits: dict[str, int] = {}


class _MarketplaceHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        with _server_lock:
            _server_hits[self.path] = _server_hits.get(self.path, 0) + 1
            route = _server_routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            return
        content_type, body = route
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence request logging
        pass


def _hits(path: str) -> int:
    with _server_lock:
        return _server_hits.get(path, 0)


def _build_package(work: Path, version: str) -> Path:
    package = work / f"package-{version}"
    if package.exists():
        shutil.rmtree(package)
    (package / "ui").mkdir(parents=True)
    manifest = {
        "kind": "better-agent-extension",
        "id": _EXTENSION_ID,
        "name": "Update Check Fixture",
        "version": version,
        "description": "Marketplace update-check test fixture.",
        "surfaces": ["frontend_feature"],
        "entrypoints": {"frontend": "ui/index.html"},
        "protocol": {
            "version": 1,
            "smoke_test": {
                "required_paths": ["better-agent-extension.json"],
                "python_modules": [],
            },
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "ui" / "index.html").write_text(f"<!doctype html><!-- v{version} -->\n", encoding="utf-8")
    return package


def _sign(artifact_sha256: str, version: str) -> str:
    return base64.b64encode(
        _KEY.sign(
            json.dumps(
                {
                    "artifact_sha256": artifact_sha256,
                    "extension_id": _EXTENSION_ID,
                    "version": version,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    ).decode("ascii")


def _publish(base_url: str, work: Path, version: str, *, corrupt_artifact: bool = False) -> dict[str, str]:
    """Publish version `version` on the mock server: artifact + metadata."""
    package = _build_package(work, version)
    artifact_path = work / f"artifact-{version}.tar.gz"
    with tarfile.open(artifact_path, "w:gz") as archive:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(package).as_posix(), recursive=False)
    artifact_bytes = artifact_path.read_bytes()
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    served_bytes = b"corrupt" if corrupt_artifact else artifact_bytes
    artifact_route = f"/artifacts/{_EXTENSION_ID}-{version}.tar.gz"
    metadata = {
        "extension_id": _EXTENSION_ID,
        "version": version,
        "artifact_url": f"{base_url}{artifact_route}",
        "artifact_sha256": artifact_sha256,
        "signature": _sign(artifact_sha256, version),
        "signature_alg": "ed25519",
    }
    metadata_route = f"/extensions/{_EXTENSION_ID}/metadata"
    with _server_lock:
        _server_routes[artifact_route] = ("application/gzip", served_bytes)
        _server_routes[metadata_route] = (
            "application/json",
            json.dumps(metadata).encode("utf-8"),
        )
    return metadata


def _installed_record() -> dict:
    record = extension_store.get_extension(_EXTENSION_ID)
    if not record:
        raise AssertionError("extension not installed")
    return record


def test_check_reports_up_to_date_then_available(base_url: str, work: Path) -> None:
    metadata_url = f"{base_url}/extensions/{_EXTENSION_ID}/metadata"
    _publish(base_url, work, "1.0.0")
    record = extension_store.install_from_marketplace_metadata(metadata_url=metadata_url)
    if record["manifest"]["version"] != "1.0.0":
        raise AssertionError(record["manifest"])

    snapshot = extension_store.check_extension_updates(refresh=True)
    rows = {row["extension_id"]: row for row in snapshot["results"]}
    row = rows[_EXTENSION_ID]
    if row["update_available"] is not False or snapshot["available"]:
        raise AssertionError(snapshot)

    _publish(base_url, work, "2.0.0")
    snapshot = extension_store.check_extension_updates(refresh=True)
    row = {r["extension_id"]: r for r in snapshot["results"]}[_EXTENSION_ID]
    if row["update_available"] is not True:
        raise AssertionError(snapshot)
    if row["available_version"] != "2.0.0" or row["installed_version"] != "1.0.0":
        raise AssertionError(row)
    if snapshot["available"] != [_EXTENSION_ID]:
        raise AssertionError(snapshot)

    # Cached read must not hit the marketplace again.
    metadata_route = f"/extensions/{_EXTENSION_ID}/metadata"
    hits_before = _hits(metadata_route)
    cached = extension_store.check_extension_updates()
    if _hits(metadata_route) != hits_before:
        raise AssertionError("cached check re-fetched marketplace metadata")
    if cached["available"] != [_EXTENSION_ID]:
        raise AssertionError(cached)


def test_check_survives_unreachable_source(base_url: str, work: Path) -> None:
    data = extension_store._load()
    record = data["extensions"][_EXTENSION_ID]
    broken = json.loads(json.dumps(record))
    broken_id = "ofek.update-unreachable"
    broken["manifest"] = dict(broken["manifest"], id=broken_id)
    broken["source"] = dict(
        broken["source"],
        metadata_url=f"{base_url}/extensions/{broken_id}/metadata",
    )
    data["extensions"][broken_id] = broken
    extension_store._save(data, resurrect_extension_ids={broken_id})
    try:
        snapshot = extension_store.check_extension_updates(refresh=True)
        rows = {row["extension_id"]: row for row in snapshot["results"]}
        broken_row = rows[broken_id]
        if broken_row["update_available"] is not False or "error" not in broken_row:
            raise AssertionError(broken_row)
        # The healthy row is still reported alongside the broken one.
        if rows[_EXTENSION_ID]["update_available"] is not True:
            raise AssertionError(rows)
    finally:
        data = extension_store._load()
        data["extensions"].pop(broken_id, None)
        extension_store._save(data, deleted_extension_ids={broken_id})


def test_apply_update_installs_new_version(base_url: str, work: Path) -> None:
    result = extension_store.apply_extension_update(_EXTENSION_ID)
    if result["updated"] is not True or result["version"] != "2.0.0":
        raise AssertionError(result)
    record = _installed_record()
    if record["manifest"]["version"] != "2.0.0":
        raise AssertionError(record["manifest"])
    if not Path(record["source"]["install_path"]).is_dir():
        raise AssertionError(record["source"])
    # A successful install drops the stale "available" cache row.
    cached = extension_store.cached_extension_updates()
    if cached is None or _EXTENSION_ID in cached["available"]:
        raise AssertionError(cached)


def test_apply_update_when_current_skips(base_url: str, work: Path) -> None:
    result = extension_store.apply_extension_update(_EXTENSION_ID)
    if result["updated"] is not False or result.get("skipped") != "up_to_date":
        raise AssertionError(result)


def test_apply_update_failure_leaves_current_version_active(base_url: str, work: Path) -> None:
    _publish(base_url, work, "3.0.0", corrupt_artifact=True)
    before = _installed_record()
    try:
        extension_store.apply_extension_update(_EXTENSION_ID)
    except extension_store.ExtensionError:
        pass
    else:
        raise AssertionError("corrupt artifact update did not fail")
    after = _installed_record()
    if after["manifest"]["version"] != before["manifest"]["version"]:
        raise AssertionError(after["manifest"])
    if after["source"]["install_path"] != before["source"]["install_path"]:
        raise AssertionError(after["source"])
    if not Path(after["source"]["install_path"]).is_dir():
        raise AssertionError(after["source"])


def test_apply_update_rejects_unknown_and_unsupported(base_url: str, work: Path) -> None:
    try:
        extension_store.apply_extension_update("ofek.not-installed")
    except extension_store.ExtensionError:
        pass
    else:
        raise AssertionError("unknown extension did not fail")
    data = extension_store._load()
    local = json.loads(json.dumps(data["extensions"][_EXTENSION_ID]))
    local_id = "ofek.update-local-source"
    local["manifest"] = dict(local["manifest"], id=local_id)
    local["source"] = dict(local["source"], type="better_agent_local")
    data["extensions"][local_id] = local
    extension_store._save(data, resurrect_extension_ids={local_id})
    try:
        extension_store.apply_extension_update(local_id)
    except extension_store.ExtensionError:
        pass
    else:
        raise AssertionError("non-remote source did not fail")
    finally:
        data = extension_store._load()
        data["extensions"].pop(local_id, None)
        extension_store._save(data, deleted_extension_ids={local_id})


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MarketplaceHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    work = Path(tempfile.mkdtemp(prefix="bc-test-extension-updates-work-"))
    os.environ["BETTER_AGENT_MARKETPLACE_BASE_URL"] = base_url
    os.environ["BETTER_AGENT_MARKETPLACE_PUBLIC_KEY"] = _PUBLIC_KEY
    os.environ["BETTER_AGENT_ALLOW_INSECURE_MARKETPLACE_ARTIFACTS"] = "1"
    tests = [
        test_check_reports_up_to_date_then_available,
        test_check_survives_unreachable_source,
        test_apply_update_installs_new_version,
        test_apply_update_when_current_skips,
        test_apply_update_failure_leaves_current_version_active,
        test_apply_update_rejects_unknown_and_unsupported,
    ]
    try:
        for test in tests:
            test(base_url, work)
            print(f"PASS {test.__name__}")
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print("OK")


if __name__ == "__main__":
    main()
