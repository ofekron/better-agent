"""Unit tests for file_api — HTTP surface for file browsing/viewing/preview.

The path-traversal confinement itself lives downstream (signed-token verify,
containment, file_browser). file_api.py is thin wiring whose own testable
behavior is: input-validation branches, search kind/methods normalization,
HTTP Range-header parsing, local-vs-remote streaming selection, the base64
chunked remote sender, and CSP/inline header application. All deterministic
-> unit tier. Route handlers are called directly as async functions with
monkeypatched dispatch (node_op / file_delivery.host / file_panel_drafts /
file_preview_urls / topology).
"""
from __future__ import annotations

import base64
import os
import sys

import _test_home  # noqa: F401  (engages prod-home guard before backend import)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest

pytestmark = pytest.mark.anyio

import file_api  # noqa: E402
from starlette.responses import FileResponse, StreamingResponse  # noqa: E402


def _http_status(excinfo) -> int:
    return excinfo.value.status_code


class _FakeRequest:
    """Minimal stand-in for starlette Request — only `.headers` is read."""

    def __init__(self, range_header=None):
        if range_header is None:
            self.headers = {}
        else:
            self.headers = {"range": range_header}


class _NoHeadersRequest:
    """No `headers` attribute -> exercises the `hasattr` guard."""


@pytest.fixture
def calls():
    """Records every node_op dispatch as (node_id, method, params)."""
    return []


@pytest.fixture
def fake_node_op(monkeypatch, calls):
    """Replace file_api.node_op with a recorder that returns canned payloads
    keyed on method. Individual tests extend `returns` as needed."""

    state = {
        "get_raw_file_info": {"size": 100, "mime_type": "application/octet-stream", "path": "/tmp/x"},
        "read_file_raw_range": {"data_b64": ""},
    }

    async def _node_op(node_id, method, params):
        calls.append((node_id, method, params))
        if method in state:
            return state[method]
        return {"ok": True, "method": method, "params": params}

    monkeypatch.setattr(file_api, "node_op", _node_op)
    return state


@pytest.fixture
def local_file(tmp_path):
    """A real on-disk file so FileResponse construction is realistic."""
    p = tmp_path / "blob.bin"
    p.write_bytes(b"x" * 100)
    return str(p)


# --------------------------------------------------------------------------- #
# browsing routes
# --------------------------------------------------------------------------- #
async def test_get_files_dispatches(fake_node_op, calls):
    res = await file_api.get_files(path="/root", node_id="primary", max_depth=2)
    assert calls == [("primary", "get_file_tree", {"root": "/root", "max_depth": 2})]
    assert res["method"] == "get_file_tree"


async def test_browse_dir_dispatches(fake_node_op, calls):
    await file_api.browse_dir(path="/sub", node_id="n1")
    assert calls == [("n1", "list_directories", {"path": "/sub"})]


async def test_search_files_normalizes_kind_and_methods(fake_node_op, calls):
    await file_api.search_files(root="/r", q="abc", kind="dir", methods="path, name ,symbols, junk,")
    assert calls[0][2] == {
        "root": "/r",
        "query": "abc",
        "kind": "dir",
        "methods": ["path", "name", "symbols"],
    }


async def test_search_files_invalid_kind_defaults_to_file(fake_node_op, calls):
    await file_api.search_files(root="/r", q="", kind="weird", methods="")
    assert calls[0][2]["kind"] == "file"
    assert calls[0][2]["methods"] == []


async def test_search_files_strips_unknown_methods(fake_node_op, calls):
    await file_api.search_files(root="/r", methods="unknown,only")
    assert calls[0][2]["methods"] == []  # "only" is not a valid method either


# --------------------------------------------------------------------------- #
# create_file_entry validation
# --------------------------------------------------------------------------- #
async def test_create_file_entry_path_invalid(fake_node_op):
    with pytest.raises(Exception) as exc:
        await file_api.create_file_entry({"path": "  ", "kind": "file"})
    assert _http_status(exc) == 400


async def test_create_file_entry_path_not_string(fake_node_op):
    with pytest.raises(Exception) as exc:
        await file_api.create_file_entry({"path": 5, "kind": "file"})
    assert _http_status(exc) == 400


async def test_create_file_entry_node_id_invalid(fake_node_op):
    with pytest.raises(Exception) as exc:
        await file_api.create_file_entry({"path": "/p", "kind": "file", "node_id": "  "})
    assert _http_status(exc) == 400


async def test_create_file_entry_node_id_not_string(fake_node_op):
    with pytest.raises(Exception) as exc:
        await file_api.create_file_entry({"path": "/p", "kind": "file", "node_id": 7})
    assert _http_status(exc) == 400


async def test_create_file_entry_bad_kind(fake_node_op):
    with pytest.raises(Exception) as exc:
        await file_api.create_file_entry({"path": "/p", "kind": "symlink"})
    assert _http_status(exc) == 400


async def test_create_file_entry_file_ok(fake_node_op, calls):
    await file_api.create_file_entry({"path": "/p", "kind": "file", "node_id": "n1"})
    assert calls == [("n1", "create_file", {"path": "/p"})]


async def test_create_file_entry_directory_ok(fake_node_op, calls):
    await file_api.create_file_entry({"path": "/d", "kind": "directory"})
    assert calls == [("primary", "create_directory", {"path": "/d"})]


# --------------------------------------------------------------------------- #
# content routes
# --------------------------------------------------------------------------- #
async def test_get_file_dispatches(fake_node_op, calls):
    await file_api.get_file(path="/f", node_id="n1")
    assert calls == [("n1", "get_file_content", {"path": "/f"})]


async def test_get_file_metadata_dispatches(fake_node_op, calls):
    await file_api.get_file_metadata(path="/f", node_id="primary")
    assert calls == [("primary", "get_file_metadata", {"path": "/f"})]


async def test_save_file_dispatches(fake_node_op, calls):
    await file_api.save_file({"path": "/f", "content": "hi", "node_id": "n1"})
    assert calls == [("n1", "write_file_content", {"path": "/f", "content": "hi"})]


async def test_save_file_default_node(fake_node_op, calls):
    await file_api.save_file({"path": "/f", "content": "hi"})
    assert calls[0][0] == "primary"


async def test_get_file_before_edit_dispatches(fake_node_op, calls):
    await file_api.get_file_before_edit(
        {"file_path": "/f", "old_string": "a", "new_string": "b", "node_id": "n1"}
    )
    assert calls == [
        ("n1", "reconstruct_before_edit",
         {"file_path": "/f", "old_string": "a", "new_string": "b"})
    ]


async def test_get_file_before_edit_defaults(fake_node_op, calls):
    await file_api.get_file_before_edit({})
    assert calls == [
        ("primary", "reconstruct_before_edit",
         {"file_path": "", "old_string": "", "new_string": ""})
    ]


# --------------------------------------------------------------------------- #
# editor drafts (file_delivery.host.run_blocking)
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_host(monkeypatch):
    """run_blocking actually invokes the passed callable (realistic); records
    each (fn, args) so tests can assert dispatch. stream_range yields a chunk."""
    invoked = []

    async def _run_blocking(fn, *args):
        invoked.append((getattr(fn, "__name__", repr(fn)), args))
        return fn(*args)

    async def _stream_range(file_path, start, content_length):
        yield b"chunk"

    monkeypatch.setattr(file_api.file_delivery.host, "run_blocking", _run_blocking)
    monkeypatch.setattr(file_api.file_delivery.host, "stream_range", _stream_range)
    return invoked


async def test_get_file_draft(fake_host, monkeypatch):
    def _read_draft(p, n):
        return {"draft": "x"}

    monkeypatch.setattr(file_api.file_panel_drafts, "read_draft", _read_draft)
    res = await file_api.get_file_draft(path="/d", node_id="n1")
    assert res == {"draft": "x"}
    assert fake_host == [("_read_draft", ("/d", "n1"))]


async def test_save_file_draft(fake_host, monkeypatch):
    written = {}

    def _write_draft(path, node_id, content, base_identity=None):
        written.update(path=path, node_id=node_id, content=content, base_identity=base_identity)
        return {"saved": True}

    monkeypatch.setattr(file_api.file_panel_drafts, "write_draft", _write_draft)
    res = await file_api.save_file_draft(
        {"path": "/d", "content": "c", "node_id": "n1", "base_identity": "id1"}
    )
    assert res == {"saved": True}
    assert written == {"path": "/d", "node_id": "n1", "content": "c", "base_identity": "id1"}


async def test_save_file_draft_default_node(fake_host, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        file_api.file_panel_drafts,
        "write_draft",
        lambda path, node_id, content, base_identity=None: seen.update(node_id=node_id) or {"ok": 1},
    )
    await file_api.save_file_draft({"path": "/d", "content": "c"})
    assert seen == {"node_id": "primary"}  # body omitted node_id -> "primary"


async def test_delete_file_draft(fake_host, monkeypatch):
    def _delete_draft(p, n):
        return {"deleted": True}

    monkeypatch.setattr(file_api.file_panel_drafts, "delete_draft", _delete_draft)
    res = await file_api.delete_file_draft(path="/d", node_id="n1")
    assert res == {"deleted": True}
    assert fake_host == [("_delete_draft", ("/d", "n1"))]


# --------------------------------------------------------------------------- #
# preview URL mint
# --------------------------------------------------------------------------- #
async def test_get_file_preview_url_ok(monkeypatch):
    monkeypatch.setattr(file_api.file_preview_urls, "mint", lambda path, node_id: "https://signed")
    assert await file_api.get_file_preview_url(path="/f", node_id="n1") == {"url": "https://signed"}


async def test_get_file_preview_url_invalid(monkeypatch):
    def _boom(path, node_id):
        raise ValueError("bad path")

    monkeypatch.setattr(file_api.file_preview_urls, "mint", _boom)
    with pytest.raises(Exception) as exc:
        await file_api.get_file_preview_url(path="/f", node_id="n1")
    assert _http_status(exc) == 400


# --------------------------------------------------------------------------- #
# get_raw_file (delegates to serve_raw_file)
# --------------------------------------------------------------------------- #
async def test_get_raw_file_delegates_to_serve(monkeypatch, fake_node_op, local_file):
    import topology
    monkeypatch.setattr(topology, "local_node_id", lambda: "primary")
    fake_node_op["get_raw_file_info"] = {
        "size": 100, "mime_type": "application/pdf", "path": local_file,
    }
    resp = await file_api.get_raw_file(_FakeRequest(), path=local_file, node_id="primary")
    assert isinstance(resp, FileResponse)


# --------------------------------------------------------------------------- #
# preview_file (token verify -> 403 / CSP headers)
# --------------------------------------------------------------------------- #
async def test_preview_file_bad_token(monkeypatch):
    def _verify(token, node_id, path):
        raise ValueError("bad token")

    monkeypatch.setattr(file_api.file_preview_urls, "verify", _verify)
    with pytest.raises(Exception) as exc:
        await file_api.preview_file(_FakeRequest(), "tok", "n1", "sub/file.html")
    assert _http_status(exc) == 403


async def test_preview_file_sets_csp_for_html(monkeypatch, fake_node_op, local_file):
    monkeypatch.setattr(file_api.file_preview_urls, "verify", lambda token, node_id, path: local_file)
    fake_node_op["get_raw_file_info"] = {
        "size": 100, "mime_type": "text/html", "path": local_file,
    }
    resp = await file_api.preview_file(_FakeRequest(), "tok", "primary", "index.html")
    assert resp.headers["content-disposition"] == "inline"
    assert resp.headers["content-security-policy"].startswith("sandbox allow-scripts")
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_preview_file_no_csp_for_binary(monkeypatch, fake_node_op, local_file):
    monkeypatch.setattr(file_api.file_preview_urls, "verify", lambda token, node_id, path: local_file)
    fake_node_op["get_raw_file_info"] = {
        "size": 100, "mime_type": "image/png", "path": local_file,
    }
    resp = await file_api.preview_file(_FakeRequest(), "tok", "primary", "pic.png")
    assert resp.headers["content-disposition"] == "inline"
    assert "content-security-policy" not in resp.headers


async def test_preview_file_csp_for_svg(monkeypatch, fake_node_op, local_file):
    monkeypatch.setattr(file_api.file_preview_urls, "verify", lambda token, node_id, path: local_file)
    fake_node_op["get_raw_file_info"] = {
        "size": 100, "mime_type": "image/svg+xml", "path": local_file,
    }
    resp = await file_api.preview_file(_FakeRequest(), "tok", "primary", "d.svg")
    assert "content-security-policy" in resp.headers


# --------------------------------------------------------------------------- #
# serve_raw_file — Range parsing + local/remote streaming
# --------------------------------------------------------------------------- #
async def test_serve_raw_local_full_file_response(monkeypatch, fake_node_op, local_file):
    import topology
    monkeypatch.setattr(topology, "local_node_id", lambda: "primary")
    fake_node_op["get_raw_file_info"] = {
        "size": 100, "mime_type": "video/mp4", "path": local_file,
    }
    resp = await file_api.serve_raw_file(_FakeRequest(), local_file, "primary")
    assert isinstance(resp, FileResponse)
    assert resp.media_type == "video/mp4"


async def test_serve_raw_local_range_streaming(monkeypatch, fake_node_op, local_file):
    import topology
    monkeypatch.setattr(topology, "local_node_id", lambda: "primary")
    fake_node_op["get_raw_file_info"] = {
        "size": 100, "mime_type": "video/mp4", "path": local_file,
    }
    resp = await file_api.serve_raw_file(
        _FakeRequest("bytes=10-50"), local_file, "primary"
    )
    assert isinstance(resp, StreamingResponse)
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == "bytes 10-50/100"
    assert resp.headers["Content-Length"] == "41"
    assert resp.headers["Accept-Ranges"] == "bytes"


async def test_serve_raw_range_open_ended(monkeypatch, fake_node_op, local_file):
    import topology
    monkeypatch.setattr(topology, "local_node_id", lambda: "primary")
    fake_node_op["get_raw_file_info"] = {
        "size": 100, "mime_type": "video/mp4", "path": local_file,
    }
    resp = await file_api.serve_raw_file(_FakeRequest("bytes=10-"), local_file, "primary")
    # open-ended -> end defaults to size-1 -> 206
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == "bytes 10-99/100"
    assert resp.headers["Content-Length"] == "90"


async def test_serve_raw_range_clamped(monkeypatch, fake_node_op, local_file):
    import topology
    monkeypatch.setattr(topology, "local_node_id", lambda: "primary")
    fake_node_op["get_raw_file_info"] = {
        "size": 100, "mime_type": "video/mp4", "path": local_file,
    }
    resp = await file_api.serve_raw_file(
        _FakeRequest("bytes=0-200"), local_file, "primary"
    )
    # end clamped to size-1=99 -> content_length 100
    assert resp.status_code == 206
    assert resp.headers["Content-Range"] == "bytes 0-99/100"
    assert resp.headers["Content-Length"] == "100"


async def test_serve_raw_non_matching_range_is_full(monkeypatch, fake_node_op, local_file):
    import topology
    monkeypatch.setattr(topology, "local_node_id", lambda: "primary")
    fake_node_op["get_raw_file_info"] = {
        "size": 100, "mime_type": "application/pdf", "path": local_file,
    }
    resp = await file_api.serve_raw_file(_FakeRequest("invalid"), local_file, "primary")
    # non-matching range header -> no 206 -> local full-file FileResponse
    assert isinstance(resp, FileResponse)


async def test_serve_raw_request_without_headers(monkeypatch, fake_node_op, local_file):
    import topology
    monkeypatch.setattr(topology, "local_node_id", lambda: "primary")
    fake_node_op["get_raw_file_info"] = {
        "size": 100, "mime_type": "application/pdf", "path": local_file,
    }
    resp = await file_api.serve_raw_file(_NoHeadersRequest(), local_file, "primary")
    assert isinstance(resp, FileResponse)


async def test_serve_raw_topology_failure_falls_back_to_primary(monkeypatch, fake_node_op, local_file):
    import topology
    # _lid() raises -> except branch -> is_local = node_id == "primary"
    def _boom():
        raise RuntimeError("topology unavailable")

    monkeypatch.setattr(topology, "local_node_id", _boom)
    fake_node_op["get_raw_file_info"] = {
        "size": 100, "mime_type": "application/pdf", "path": local_file,
    }
    resp = await file_api.serve_raw_file(_FakeRequest(), local_file, "primary")
    assert isinstance(resp, FileResponse)  # is_local True (node_id == primary)


async def test_serve_raw_remote_streaming(monkeypatch, fake_node_op):
    import topology
    monkeypatch.setattr(topology, "local_node_id", lambda: "primary-node")
    fake_node_op["get_raw_file_info"] = {
        "size": 100, "mime_type": "video/mp4", "path": "/remote/f",
    }
    # Single base64 chunk covering the full content_length terminates the sender.
    fake_node_op["read_file_raw_range"] = {"data_b64": base64.b64encode(b"z" * 100).decode()}
    resp = await file_api.serve_raw_file(_FakeRequest(), "/remote/f", "remote-node")
    assert isinstance(resp, StreamingResponse)
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "video/mp4"


# --------------------------------------------------------------------------- #
# _remote_sender — base64 chunk loop, empty-chunk break
# --------------------------------------------------------------------------- #
async def test_remote_sender_streams_chunks(monkeypatch, fake_node_op):
    # Two chunks then exhaustion by content_length.
    fake_node_op["read_file_raw_range"] = {"data_b64": base64.b64encode(b"AAAA").decode()}
    chunks = []
    async for c in file_api._remote_sender("n1", "/f", 0, 8, False):
        chunks.append(c)
    # two 4-byte chunks -> 8 bytes total -> remaining hits 0
    assert b"".join(chunks) == b"AAAAAAAA"


async def test_remote_sender_breaks_on_empty_chunk(monkeypatch, fake_node_op):
    fake_node_op["read_file_raw_range"] = {"data_b64": ""}
    chunks = []
    async for c in file_api._remote_sender("n1", "/f", 0, 100, False):
        chunks.append(c)
    assert chunks == []  # empty chunk -> immediate break
