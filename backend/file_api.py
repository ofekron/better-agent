"""HTTP surface for the file browsing / viewing / preview domain.

INVARIANT: every endpoint takes an optional `node_id` (default
"primary" — the sentinel for the local node). They funnel through
`node_op`, which dispatches locally OR via `node_link.rpc_call` for
remote nodes. Single-machine deploys never see a topology lookup on
this path.

Blocking local reads belong to `file_delivery.host`, never to the
request loop.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query, Request
from starlette.responses import FileResponse, StreamingResponse

import file_delivery
import file_panel_drafts
import file_preview_urls
from node_op import node_op

router = APIRouter(tags=["files"])

_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")
_REMOTE_CHUNK_BYTES = 4 * 1024 * 1024


# ---- browsing ----

@router.get("/api/files")
async def get_files(
    path: str = Query(...),
    node_id: str = Query("primary"),
    max_depth: int = Query(1, ge=0, le=5),
):
    return await node_op(node_id, "get_file_tree", {"root": path, "max_depth": max_depth})


@router.get("/api/browse")
async def browse_dir(
    path: str = Query(""),
    node_id: str = Query("primary"),
):
    return await node_op(node_id, "list_directories", {"path": path})


@router.get("/api/files/search")
async def search_files(
    root: str = Query(...),
    q: str = Query(""),
    kind: str = Query("file"),
    methods: str = Query("path"),
    node_id: str = Query("primary"),
):
    if kind not in ("file", "dir"):
        kind = "file"
    sel = [m for m in (s.strip() for s in methods.split(",")) if m in ("path", "name", "symbols")]
    return await node_op(
        node_id,
        "search_tree",
        {"root": root, "query": q, "kind": kind, "methods": sel},
    )


@router.post("/api/files/create")
async def create_file_entry(body: dict = Body(...)):
    path = body.get("path")
    kind = body.get("kind")
    node_id = body.get("node_id") or "primary"
    if not isinstance(path, str) or not path.strip():
        raise HTTPException(status_code=400, detail="path must be a non-empty string")
    if not isinstance(node_id, str) or not node_id.strip():
        raise HTTPException(status_code=400, detail="node_id must be a non-empty string")
    if kind not in ("file", "directory"):
        raise HTTPException(status_code=400, detail="kind must be file or directory")
    method = "create_file" if kind == "file" else "create_directory"
    return await node_op(node_id, method, {"path": path})


# ---- content ----

@router.get("/api/file")
async def get_file(
    path: str = Query(...),
    node_id: str = Query("primary"),
):
    return await node_op(node_id, "get_file_content", {"path": path})


@router.get("/api/file/metadata")
async def get_file_metadata(
    path: str = Query(...),
    node_id: str = Query("primary"),
):
    return await node_op(node_id, "get_file_metadata", {"path": path})


@router.post("/api/file")
async def save_file(body: dict = Body(...)):
    return await node_op(
        body.get("node_id") or "primary",
        "write_file_content",
        {"path": body["path"], "content": body["content"]},
    )


@router.post("/api/file-before-edit")
async def get_file_before_edit(body: dict = Body(...)):
    return await node_op(
        body.get("node_id") or "primary",
        "reconstruct_before_edit",
        {
            "file_path": body.get("file_path", ""),
            "old_string": body.get("old_string", ""),
            "new_string": body.get("new_string", ""),
        },
    )


# ---- editor drafts ----

@router.get("/api/file/draft")
async def get_file_draft(
    path: str = Query(...),
    node_id: str = Query("primary"),
):
    return await file_delivery.host.run_blocking(
        file_panel_drafts.read_draft, path, node_id,
    )


@router.post("/api/file/draft")
async def save_file_draft(body: dict = Body(...)):
    node_id = body.get("node_id") or "primary"

    def _write():
        return file_panel_drafts.write_draft(
            path=body["path"],
            node_id=node_id,
            content=body["content"],
            base_identity=body.get("base_identity"),
        )

    return await file_delivery.host.run_blocking(_write)


@router.delete("/api/file/draft")
async def delete_file_draft(
    path: str = Query(...),
    node_id: str = Query("primary"),
):
    return await file_delivery.host.run_blocking(
        file_panel_drafts.delete_draft, path, node_id,
    )


# ---- raw bytes + preview ----

@router.get("/api/file/raw")
async def get_raw_file(
    request: Request,
    path: str = Query(...),
    node_id: str = Query("primary"),
):
    """Serve a binary file (PDF, video, audio) with correct Content-Type.
    Supports HTTP Range requests for video seeking. For sessions hosted
    on a worker-node the bytes are pulled over the node WS in base64
    chunks (`read_file_raw_range`); local files stream straight off disk."""
    return await serve_raw_file(request, path, node_id)


@router.get("/api/file/preview-url")
async def get_file_preview_url(
    path: str = Query(...),
    node_id: str = Query("primary"),
):
    """Mint a signed, expiring, directory-scoped preview URL (authed).
    The preview iframe runs in an opaque origin that cannot send the
    session cookie, so the URL's signature is the credential for the
    /api/file/preview/ route."""
    try:
        return {"url": file_preview_urls.mint(path, node_id)}
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid preview path")


@router.get("/api/file/preview/{token}/{node_id}/{file_path:path}")
async def preview_file(request: Request, token: str, node_id: str, file_path: str):
    """Serve a file for in-panel/new-tab HTML preview. Path-based routing
    (unlike the query-based /api/file/raw) so relative asset URLs inside
    the page resolve to sibling files naturally. Gated by the signed
    token, confined to the signed directory tree. HTML/SVG responses
    carry a CSP sandbox — scripts run in an opaque origin and cannot
    reach Better Agent's origin, cookies, or DOM."""
    try:
        norm_path = file_preview_urls.verify(token, node_id, f"/{file_path}")
    except ValueError:
        raise HTTPException(status_code=403, detail="invalid preview token")
    response = await serve_raw_file(
        request, norm_path, node_id, allow_preview_types=True,
    )
    response.headers["content-disposition"] = "inline"
    mime = response.headers.get("content-type", "")
    if mime.startswith("text/html") or mime.startswith("image/svg"):
        response.headers["content-security-policy"] = (
            "sandbox allow-scripts allow-popups allow-forms allow-modals allow-downloads"
        )
        response.headers["x-content-type-options"] = "nosniff"
    return response


async def serve_raw_file(
    request: Request,
    path: str,
    node_id: str,
    allow_preview_types: bool = False,
):
    """Shared raw-file streamer for /api/file/raw and the signed preview
    route. `allow_preview_types` widens the extension allowlist to web
    assets and is only ever set by the token-gated preview route — it is
    deliberately not a query param on /api/file/raw."""
    try:
        from topology import local_node_id as _lid
        is_local = node_id in ("primary", _lid())
    except Exception:
        is_local = node_id == "primary"

    info = await node_op(
        node_id, "get_raw_file_info",
        {"path": path, "allow_preview_types": allow_preview_types},
    )
    size = info["size"]
    mime = info["mime_type"]
    file_path = Path(info["path"])

    start, end, status = 0, size - 1, 200
    range_header = request.headers.get("range") if hasattr(request, "headers") else None
    if range_header:
        match = _RANGE_RE.match(range_header)
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else size - 1
            end = min(end, size - 1)
            status = 206
    content_length = end - start + 1

    if is_local and status == 200:
        # Starlette reads whole-file responses on its own threadpool.
        return FileResponse(file_path, media_type=mime, filename=file_path.name)

    if is_local:
        sender = file_delivery.host.stream_range(file_path, start, content_length)
    else:
        sender = _remote_sender(node_id, path, start, content_length, allow_preview_types)

    headers = {
        "Content-Length": str(content_length),
        "Accept-Ranges": "bytes",
        "Content-Type": mime,
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    return StreamingResponse(sender, status_code=status, headers=headers)


async def _remote_sender(
    node_id: str,
    path: str,
    start: int,
    content_length: int,
    allow_preview_types: bool,
):
    offset = start
    remaining = content_length
    while remaining > 0:
        res = await node_op(
            node_id, "read_file_raw_range",
            {"path": path, "start": offset,
             "length": min(_REMOTE_CHUNK_BYTES, remaining),
             "allow_preview_types": allow_preview_types},
        )
        chunk = base64.b64decode(res["data_b64"])
        if not chunk:
            break
        offset += len(chunk)
        remaining -= len(chunk)
        yield chunk
