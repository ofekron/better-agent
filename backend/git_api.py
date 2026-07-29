from __future__ import annotations

from fastapi import APIRouter, Query

from git_status_cache import cache as git_status_cache
from node_op import node_op

router = APIRouter()


@router.get("/api/git-status")
async def get_git_status(
    cwd: str = Query(...),
    node_id: str = Query("primary"),
):
    return await git_status_cache.get(node_id, cwd)


@router.get("/api/git-tree")
async def get_git_tree(
    cwd: str = Query(...),
    node_id: str = Query("primary"),
    limit: int = Query(200, ge=1, le=500),
):
    return await node_op(node_id, "get_git_tree", {"cwd": cwd, "limit": limit})


@router.get("/api/git-diff")
async def get_git_diff(
    path: str = Query(...),
    cwd: str = Query(...),
    node_id: str = Query("primary"),
):
    # rpc handler returns {"diff": str|None}; pass through unchanged.
    return await node_op(node_id, "get_file_diff", {"file_path": path, "cwd": cwd})


@router.post("/api/git-commit")
async def post_git_commit(body: dict):
    return await _commit(body, "git_commit")


@router.post("/api/git-commit-and-push")
async def post_git_commit_and_push(body: dict):
    return await _commit(body, "git_commit_and_push")


async def _commit(body: dict, method: str):
    node_id = body.get("node_id") or "primary"
    cwd = body.get("cwd", "")
    git_status_cache.invalidate(node_id, cwd)
    result = await node_op(
        node_id, method, {"cwd": cwd, "message": body.get("message", "")},
    )
    git_status_cache.invalidate(node_id, cwd)
    return result
