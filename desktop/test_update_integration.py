"""Live integration test for the auto-update loop.

Stands up a REAL tufup repository (real keys, real signed metadata),
serves it over a local HTTP server, and drives the actual
`updater.check()` code path against it — no mocks on the tufup client.
Proves the trust-root bootstrap, transport, and version detection wire
together against a genuine repo.

Not validated here (requires a built .app / onedir + GUI): the
restart-to-update prompt threading and tufup apply→relaunch.

Run with:
    backend/.venv/bin/python desktop/test_update_integration.py
"""

from __future__ import annotations

import functools
import os
import shutil
import socket
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_STATE_HOME = tempfile.TemporaryDirectory(prefix="bc-upd-int-")
os.environ["BETTER_AGENT_HOME"] = _STATE_HOME.name
os.environ["BETTER_CLAUDE_HOME"] = _STATE_HOME.name

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import release  # noqa: E402
import updater  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_bundle(parent: Path, version: str) -> Path:
    """A minimal onedir-shaped bundle: a folder with one versioned file."""
    d = parent / f"BetterAgent-{version}"
    d.mkdir(parents=True)
    (d / "VERSION.txt").write_text(version)
    (d / "payload.bin").write_text("x" * 1024)
    return d


def _serve(directory: Path):
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(directory))
    httpd = ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    return httpd, thread, f"http://127.0.0.1:{port}"


def _stop_server(httpd: ThreadingHTTPServer, thread: threading.Thread) -> None:
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _build_repo(root: Path) -> Path:
    """Init a repo and publish v0.1.0 then v0.2.0. Returns the exported
    trusted root.json path."""
    repo_dir = root / "repository"
    keys_dir = root / "keystore"
    bundles = root / "bundles"
    repo = release.ReleaseRepo(repo_dir, keys_dir)
    repo.initialize()
    repo.publish_bundle(_make_bundle(bundles, "0.1.0"), "0.1.0")
    # Second release via a FRESH ReleaseRepo — mimics a later, separate
    # `release.py publish` process; exercises the on-disk role reload.
    repo2 = release.ReleaseRepo(repo_dir, keys_dir)
    repo2.publish_bundle(_make_bundle(bundles, "0.2.0"), "0.2.0")
    trusted_root = repo2.export_trusted_root(root / "tufup_root.json")
    return repo_dir, trusted_root


def _run_with_repo(current_version: str):
    """Set up repo+server+updater config, return updater.check() result."""
    root = Path(tempfile.mkdtemp(prefix="bc-upd-repo-"))
    repo_dir, trusted_root = _build_repo(root)
    httpd, server_thread, base_url = _serve(repo_dir)

    original_url = os.environ.get("BA_UPDATE_URL")
    os.environ["BA_UPDATE_URL"] = base_url
    orig_ver = updater._current_version
    orig_root = updater._bundled_root
    updater._current_version = lambda: current_version
    updater._bundled_root = lambda: trusted_root
    # Fresh client metadata dir per call so the trusted-root bootstrap runs.
    md = updater._metadata_dir()
    if md.exists():
        shutil.rmtree(md)
    try:
        return updater.check()
    finally:
        if original_url is None:
            os.environ.pop("BA_UPDATE_URL", None)
        else:
            os.environ["BA_UPDATE_URL"] = original_url
        updater._current_version = orig_ver
        updater._bundled_root = orig_root
        _stop_server(httpd, server_thread)
        shutil.rmtree(root)


def _apply_with_repo() -> bool:
    root = Path(tempfile.mkdtemp(prefix="bc-upd-repo-"))
    repo_dir, trusted_root = _build_repo(root)
    httpd, server_thread, base_url = _serve(repo_dir)
    original_url = os.environ.get("BA_UPDATE_URL")
    os.environ["BA_UPDATE_URL"] = base_url
    orig_ver = updater._current_version
    orig_root = updater._bundled_root
    updater._current_version = lambda: "0.1.0"
    updater._bundled_root = lambda: trusted_root

    metadata_dir = updater._metadata_dir()
    target_dir = updater._target_dir()
    shutil.rmtree(metadata_dir, ignore_errors=True)
    shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.mkdir(parents=True)
    shutil.copy2(
        repo_dir / "targets" / "BetterAgent-0.1.0.tar.gz",
        target_dir / "BetterAgent-0.1.0.tar.gz",
    )

    installed = {}

    def _install(src_dir, dst_dir):
        installed["version"] = (Path(src_dir) / "VERSION.txt").read_text()
        installed["destination"] = Path(dst_dir)

    try:
        if not updater._ensure_trusted_root():
            return False
        client = updater._build_client()
        if client.check_for_updates() is None:
            return False
        client.download_and_apply_update(
            skip_confirmation=True,
            install=_install,
        )
        return (
            installed.get("version") == "0.2.0"
            and installed.get("destination") == updater._app_install_dir()
        )
    finally:
        if original_url is None:
            os.environ.pop("BA_UPDATE_URL", None)
        else:
            os.environ["BA_UPDATE_URL"] = original_url
        updater._current_version = orig_ver
        updater._bundled_root = orig_root
        _stop_server(httpd, server_thread)
        shutil.rmtree(root)


def test_detects_newer_version() -> bool:
    return _run_with_repo("0.1.0") == "0.2.0"


def test_none_when_on_latest() -> bool:
    return _run_with_repo("0.2.0") is None


def test_applies_xdelta_patch() -> bool:
    return _apply_with_repo()


TESTS = [
    ("client detects newer published version (0.1.0 -> 0.2.0)",
     test_detects_newer_version),
    ("client reports None when already on latest",
     test_none_when_on_latest),
    ("client downloads and applies a signed xdelta patch",
     test_applies_xdelta_patch),
]


def main_run() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            ok = fn()
        except Exception as e:
            ok = False
            import traceback
            traceback.print_exc()
            print(f"  exception: {e}")
        print(f"{PASS if ok else FAIL}  {name}")
        if not ok:
            failed += 1
    print()
    print(f"{failed} of {len(TESTS)} test(s) FAILED" if failed
          else f"all {len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
