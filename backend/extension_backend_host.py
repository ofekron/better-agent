from __future__ import annotations

import base64
import importlib.util
import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI


@dataclass(frozen=True)
class ExtensionBackendContext:
    extension_id: str
    install_path: Path
    source: dict[str, str]


def _load_router(
    extension_id: str,
    install_path: Path,
    entrypoint: str,
    entrypoint_kind: str,
    source: dict[str, str],
) -> APIRouter:
    install_path = install_path.resolve()
    _prepare_import_path(install_path)

    if entrypoint_kind == "module":
        module = importlib.import_module(entrypoint)
    else:
        entrypoint_path = Path(entrypoint).resolve()
        if not entrypoint_path.is_relative_to(install_path) or not entrypoint_path.is_file():
            raise RuntimeError("backend entrypoint escapes extension package")
        module_name = f"_better_agent_extension_{extension_id.replace('.', '_').replace('-', '_')}"
        module_spec = importlib.util.spec_from_file_location(module_name, entrypoint_path)
        if module_spec is None or module_spec.loader is None:
            raise RuntimeError("backend entrypoint could not be loaded")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)

    create_router = getattr(module, "create_router", None)
    if not callable(create_router):
        raise RuntimeError("backend entrypoint must export create_router(context)")
    router = create_router(
        ExtensionBackendContext(
            extension_id=extension_id,
            install_path=install_path,
            source=source,
        )
    )
    if not isinstance(router, APIRouter):
        raise RuntimeError("create_router(context) must return fastapi.APIRouter")
    return router


def _prepare_import_path(install_path: Path) -> None:
    backend_dir = Path(__file__).resolve().parent
    sys.path = [
        item for item in sys.path
        if Path(item or ".").resolve() != backend_dir
    ]
    paths = [str(install_path)]
    site_packages = _venv_site_packages(install_path / ".venv")
    if site_packages is not None:
        paths.append(str(site_packages))
    for path in reversed(paths):
        if path not in sys.path:
            sys.path.insert(0, path)


def _venv_site_packages(venv_dir: Path) -> Path | None:
    if sys.platform == "win32":
        candidate = venv_dir / "Lib" / "site-packages"
        return candidate if candidate.is_dir() else None
    lib_dir = venv_dir / "lib"
    if not lib_dir.is_dir():
        return None
    for candidate in sorted(lib_dir.glob("python*/site-packages")):
        if candidate.is_dir():
            return candidate
    return None


async def _run_asgi(app: FastAPI, payload: dict[str, Any]) -> dict[str, Any]:
    body = base64.b64decode(str(payload.get("body") or ""))
    query_string = base64.b64decode(str(payload.get("query_string") or ""))
    sent_body = False

    async def receive() -> dict[str, Any]:
        nonlocal sent_body
        if sent_body:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent_body = True
        return {"type": "http.request", "body": body, "more_body": False}

    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    headers = [
        (str(key).encode("latin-1"), str(value).encode("latin-1"))
        for key, value in payload.get("headers", [])
    ]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": str(payload["method"]),
        "scheme": "http",
        "path": str(payload["path"]),
        "raw_path": str(payload["path"]).encode("utf-8"),
        "query_string": query_string,
        "headers": headers,
        "client": ("extension-host", 0),
        "server": ("extension-host", 0),
        "root_path": "",
        "app": app,
    }
    await app(scope, receive, send)
    start = next((message for message in messages if message["type"] == "http.response.start"), None)
    if start is None:
        raise RuntimeError("extension backend did not respond")
    content = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return {
        "status": int(start["status"]),
        "headers": [
            [key.decode("latin-1"), value.decode("latin-1")]
            for key, value in start.get("headers", [])
        ],
        "body": base64.b64encode(content).decode("ascii"),
    }


async def _main_async() -> int:
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    install_path = Path(str(payload["install_path"]))
    entrypoint = str(payload["entrypoint"])
    entrypoint_kind = str(payload.get("entrypoint_kind") or "file")
    source = {str(key): str(value) for key, value in dict(payload.get("source") or {}).items()}
    app = FastAPI()
    app.include_router(
        _load_router(str(payload["extension_id"]), install_path, entrypoint, entrypoint_kind, source)
    )
    result = await _run_asgi(app, payload)
    sys.stdout.write(json.dumps(result, separators=(",", ":")))
    return 0


async def _serve_persistent() -> int:
    """Long-lived mode: read the extension spec on the first stdin line, load
    the router once, then serve newline-delimited JSON requests until stdin
    EOF. One load amortizes many requests — the precondition for moving hot
    substrate into extensions without a per-request subprocess spawn.

    Requests are multiplexed: each is handled in its own task so a slow route
    does not block others, and each response echoes the request ``id`` so the
    core demuxes them back to the right caller. Responses arrive in completion
    order, not request order."""
    import asyncio

    loop = asyncio.get_running_loop()
    spec_line = await loop.run_in_executor(None, sys.stdin.buffer.readline)
    if not spec_line:
        return 0
    spec = json.loads(spec_line)
    app = FastAPI()
    app.include_router(
        _load_router(
            str(spec["extension_id"]),
            Path(str(spec["install_path"])),
            str(spec["entrypoint"]),
            str(spec.get("entrypoint_kind") or "file"),
            {str(key): str(value) for key, value in dict(spec.get("source") or {}).items()},
        )
    )

    async def _handle(line: bytes) -> None:
        request_id = None
        try:
            payload = json.loads(line)
            request_id = payload.get("id")
            result = await _run_asgi(app, payload)
        except Exception:
            result = {
                "status": 500,
                "headers": [["content-type", "text/plain"]],
                "body": base64.b64encode(b"Extension backend failed").decode("ascii"),
            }
        result["id"] = request_id
        # No await between write and flush: the event loop will not interleave
        # another task's write mid-line, so each response is emitted whole.
        sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    tasks: set[asyncio.Task] = set()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.buffer.readline)
        if not line:
            break  # stdin closed — host exits cleanly
        task = asyncio.create_task(_handle(line))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return 0


def main() -> int:
    import asyncio

    if "--persistent" in sys.argv:
        return asyncio.run(_serve_persistent())
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
