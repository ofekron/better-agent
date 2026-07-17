from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException

from session_manager import manager as session_manager


_BRANCH = re.compile(r"^(?!-)(?!.*\.\.)(?!.*[~^:?*\[\\\s])[^/]+(?:/[^/]+)*$")
_BASE_ARGS = [
    "git",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "commit.gpgSign=false",
    "-c", "credential.helper=",
    "-c", "protocol.ext.allow=never",
    "-c", "protocol.file.allow=never",
]
_ENV = {
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "GIT_EDITOR": "true",
    "XDG_CONFIG_HOME": os.devnull,
}


def _run(cwd: Path, args: list[str], *, timeout: float = 60.0) -> dict[str, Any]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run(
        [*_BASE_ARGS, *args],
        cwd=cwd,
        env={**env, **_ENV},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    stdout = result.stdout[-1_000_000:]
    stderr = result.stderr[-1_000_000:]
    if result.returncode != 0:
        return {"result": "error", "error": stderr.strip() or stdout.strip(), "returncode": result.returncode}
    return {"result": "ok", "output": stdout.strip()}


def _repo(payload: dict[str, Any]) -> Path:
    actor_session_id = payload["actor_session_id"]
    if not session_manager.exists(actor_session_id):
        raise HTTPException(status_code=404, detail="unknown actor session")
    requested = Path(payload["cwd"]).expanduser().resolve()
    session_cwd = Path(
        str(session_manager.get_field(actor_session_id, "cwd") or ""),
    ).expanduser().resolve()
    if requested != session_cwd:
        raise HTTPException(status_code=403, detail="cwd does not match actor session")
    if not requested.is_dir():
        raise HTTPException(status_code=400, detail="cwd is not a directory")
    root = _run(requested, ["rev-parse", "--show-toplevel"], timeout=10.0)
    if root["result"] != "ok":
        raise HTTPException(status_code=400, detail="cwd is not a git repository")
    resolved_root = Path(root["output"]).resolve()
    if not requested.is_relative_to(resolved_root):
        raise HTTPException(status_code=403, detail="cwd escapes repository")
    return resolved_root


def _paths(root: Path, values: list[str]) -> list[str]:
    clean: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or value.startswith(":") or "\x00" in value:
            raise HTTPException(status_code=422, detail="invalid git path")
        path = Path(value)
        if path.is_absolute() or not (root / path).resolve().is_relative_to(root):
            raise HTTPException(status_code=403, detail="git path escapes repository")
        clean.append(value)
    return clean


def _branch(value: str) -> str:
    if not _BRANCH.fullmatch(value) or value.endswith((".", "/")) or "@{" in value:
        raise HTTPException(status_code=422, detail="invalid git ref")
    return value


def _execute(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    root = _repo(payload)
    if action == "status":
        return _run(root, ["status", "--short", "--branch"])
    if action == "diff":
        args = ["diff", "--no-ext-diff", "--no-textconv"]
        if payload["staged"]:
            args.append("--cached")
        return _run(root, args)
    if action == "log":
        return _run(root, ["log", f"--max-count={payload['limit']}", "--oneline", "--decorate=no"])
    if action == "add":
        filters = _run(root, ["config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|process)$"])
        if filters["result"] == "ok" and filters["output"]:
            raise HTTPException(status_code=403, detail="repository has executable clean filters")
        return _run(root, ["add", "--", *_paths(root, payload["paths"])])
    if action == "commit":
        return _run(root, ["commit", "--no-verify", "-m", payload["message"]])
    if action == "branch":
        name = _branch(payload["name"])
        return _run(root, ["branch", name] if payload["create"] else ["branch", "--list", name])
    if action == "push":
        remote = payload["remote"]
        url = _run(root, ["remote", "get-url", "--push", remote], timeout=10.0)
        if url["result"] != "ok" or urlparse(url["output"]).scheme != "https":
            raise HTTPException(status_code=403, detail="push remote must use https")
        args = ["push", remote]
        if payload["ref"]:
            args.append(_branch(payload["ref"]))
        return _run(root, args, timeout=120.0)
    raise HTTPException(status_code=404, detail="unknown git action")


async def execute(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_execute, action, payload)
