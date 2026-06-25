"""Locks the signed HTML-preview URL contract: minting is
directory-scoped, verification fails closed (expiry, traversal,
outside-tree, malformed), and the /api/file/preview/ route serves
signed files with the CSP sandbox while rejecting everything else."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_home = tempfile.mkdtemp(prefix="ba-preview-test-")
import paths  # noqa: E402

paths.engage_test_home(_home)

import time  # noqa: E402

import file_preview_urls  # noqa: E402


def test_mint_and_verify_roundtrip():
    url = file_preview_urls.mint("/repo/site/index.html", "primary")
    assert url.startswith("/api/file/preview/")
    token = url.split("/")[4]
    assert file_preview_urls.verify(token, "primary", "/repo/site/index.html") == "/repo/site/index.html"
    # Relative assets anywhere under the signed directory are covered.
    assert file_preview_urls.verify(token, "primary", "/repo/site/img/logo.png") == "/repo/site/img/logo.png"


def test_verify_fails_closed():
    url = file_preview_urls.mint("/repo/site/index.html", "primary")
    token = url.split("/")[4]
    for bad in [
        ("primary", "/repo/other/secret.txt"),      # outside signed tree
        ("primary", "/repo/site/../../etc/passwd"),  # traversal
        ("primary", "/etc/passwd"),                  # ancestor escape
        ("node-2", "/repo/site/index.html"),         # different node
    ]:
        try:
            file_preview_urls.verify(token, *bad)
            raise AssertionError(f"verify accepted {bad}")
        except ValueError:
            pass
    exp = int(time.time()) - 10
    sig = file_preview_urls._sig("primary", "/repo/site", exp)
    try:
        file_preview_urls.verify(f"{exp}.{sig}", "primary", "/repo/site/index.html")
        raise AssertionError("verify accepted expired token")
    except ValueError:
        pass
    try:
        file_preview_urls.verify("garbage", "primary", "/repo/site/index.html")
        raise AssertionError("verify accepted malformed token")
    except ValueError:
        pass


def test_preview_route_serves_signed_tree_only():
    from fastapi.testclient import TestClient
    import main

    client = TestClient(main.app)
    site = tempfile.mkdtemp(prefix="ba-preview-site-")
    Path(site, "index.html").write_text("<html><body><h1>ok</h1></body></html>")
    Path(site, "app.js").write_text("console.log('hi')")
    outside = tempfile.mkdtemp(prefix="ba-preview-outside-")
    Path(outside, "secret.txt").write_text("secret")

    url = file_preview_urls.mint(f"{site}/index.html", "primary")

    r = client.get(url)
    assert r.status_code == 200, r.text
    assert "ok" in r.text
    assert r.headers["content-type"].startswith("text/html")
    assert "sandbox" in r.headers.get("content-security-policy", "")
    assert r.headers.get("content-disposition") == "inline"

    token = url.split("/")[4]
    r2 = client.get(f"/api/file/preview/{token}/primary{site}/app.js")
    assert r2.status_code == 200
    assert "console" in r2.text
    assert "content-security-policy" not in r2.headers

    r3 = client.get(f"/api/file/preview/{token}/primary{outside}/secret.txt")
    assert r3.status_code == 403

    # The widened extension allowlist must NOT leak to /api/file/raw:
    # html stays unsupported there (401 auth-gated first anyway, and the
    # extension gate itself is exercised via file_browser directly).
    import file_browser
    try:
        file_browser.get_raw_file_info(f"{site}/index.html")
        raise AssertionError("raw allowlist accepted .html without preview flag")
    except ValueError:
        pass

    # The minting endpoint itself must stay behind auth.
    r4 = client.get(f"/api/file/preview-url?path={site}/index.html&node_id=primary")
    assert r4.status_code == 401


if __name__ == "__main__":
    test_mint_and_verify_roundtrip()
    test_verify_fails_closed()
    test_preview_route_serves_signed_tree_only()
    print("PASS")
