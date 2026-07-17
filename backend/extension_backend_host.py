from __future__ import annotations

import base64
import importlib.util
import importlib
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI

import extension_venvs

logger = logging.getLogger("extension_backend_host")


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
    venv_dir = extension_venvs.resolve_venv_dir(install_path)
    site_packages = (
        extension_venvs.venv_site_packages_dir(venv_dir) if venv_dir is not None else None
    )
    if site_packages is not None:
        paths.append(str(site_packages))
    for path in reversed(paths):
        if path not in sys.path:
            sys.path.insert(0, path)


async def _run_asgi(
    app: FastAPI, payload: dict[str, Any], concurrency: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int, int, int, int, int, int, int]:
    build_started_ns = time.monotonic_ns()
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
    build_ns = time.monotonic_ns() - build_started_ns
    asgi_started_ns = time.monotonic_ns()
    cohort_cpu_started_ns = time.process_time_ns()
    scheduler_max_delay_ns = 0
    scheduler_done = False
    scheduler_expected = time.monotonic() + 0.01

    async def scheduler_probe() -> None:
        nonlocal scheduler_max_delay_ns, scheduler_expected
        interval = 0.01
        while not scheduler_done:
            await asyncio.sleep(interval)
            now = time.monotonic()
            scheduler_max_delay_ns = max(
                scheduler_max_delay_ns,
                int(max(0.0, now - scheduler_expected) * 1_000_000_000),
            )
            scheduler_expected = now + interval

    import asyncio
    probe = asyncio.create_task(scheduler_probe())
    try:
        await app(scope, receive, send)
    finally:
        final_sample_at = time.monotonic()
        scheduler_max_delay_ns = max(
            scheduler_max_delay_ns,
            int(max(0.0, final_sample_at - scheduler_expected) * 1_000_000_000),
        )
        scheduler_done = True
        probe.cancel()
        await asyncio.gather(probe, return_exceptions=True)
    asgi_ns = time.monotonic_ns() - asgi_started_ns
    cohort_process_cpu_ns = max(0, time.process_time_ns() - cohort_cpu_started_ns)
    collect_started_ns = time.monotonic_ns()
    start = next((message for message in messages if message["type"] == "http.response.start"), None)
    if start is None:
        raise RuntimeError("extension backend did not respond")
    content = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    result = {
        "status": int(start["status"]),
        "headers": [
            [key.decode("latin-1"), value.decode("latin-1")]
            for key, value in start.get("headers", [])
        ],
        "body": base64.b64encode(content).decode("ascii"),
    }
    completed_ns = time.monotonic_ns()
    overlap_ns = int((concurrency or {}).get("overlap_ns", 0))
    overlap_started_ns = (concurrency or {}).get("overlap_started_ns")
    if overlap_started_ns is not None:
        overlap_ns += max(0, completed_ns - int(overlap_started_ns))
    return (
        result,
        build_ns,
        asgi_ns,
        time.monotonic_ns() - collect_started_ns,
        cohort_process_cpu_ns,
        scheduler_max_delay_ns,
        max(1, int((concurrency or {}).get("max", 1))),
        overlap_ns,
    )


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
    result, _, _, _, _, _, _, _ = await _run_asgi(app, payload)
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

    process_epoch_ns = time.monotonic_ns()
    active_concurrency: dict[str, dict[str, Any]] = {}
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
    max_concurrency = spec.get("max_concurrency")
    if (
        isinstance(max_concurrency, bool)
        or not isinstance(max_concurrency, int)
        or not 1 <= max_concurrency <= 64
    ):
        raise RuntimeError("invalid extension host concurrency limit")
    admission = asyncio.Semaphore(max_concurrency)

    async def _handle(line: bytes, accepted_ns: int) -> None:
        request_id = None
        payload: dict[str, Any] | None = None
        dispatch_ns = time.monotonic_ns()
        decode_started_ns = dispatch_ns
        try:
            payload = json.loads(line)
            request_id = payload.get("id")
            concurrency = {"max": len(active_concurrency) + 1}
            joined_ns = time.monotonic_ns()
            for tracker in active_concurrency.values():
                tracker["max"] = max(tracker["max"], concurrency["max"])
                if tracker.get("overlap_started_ns") is None:
                    tracker["overlap_started_ns"] = joined_ns
            concurrency["overlap_ns"] = 0
            concurrency["overlap_started_ns"] = joined_ns if active_concurrency else None
            if isinstance(request_id, str):
                active_concurrency[request_id] = concurrency
            decoded_ns = time.monotonic_ns()
            (
                result,
                build_ns,
                asgi_ns,
                response_collect_ns,
                cohort_process_cpu_ns,
                scheduler_max_delay_ns,
                concurrent_requests,
                cohort_overlap_ns,
            ) = await _run_asgi(app, payload, concurrency)
        except Exception:
            logger.exception(
                "extension backend route failed: request_id=%s method=%s path=%s",
                request_id,
                (payload or {}).get("method"),
                (payload or {}).get("path"),
            )
            decoded_ns = time.monotonic_ns()
            build_ns = 0
            asgi_ns = 0
            response_collect_ns = 0
            cohort_process_cpu_ns = 0
            scheduler_max_delay_ns = 0
            concurrent_requests = max(1, len(active_concurrency))
            cohort_overlap_ns = 0
            result = {
                "status": 500,
                "headers": [["content-type", "text/plain"]],
                "body": base64.b64encode(b"Extension backend failed").decode("ascii"),
            }
        result["id"] = request_id
        encode_started_ns = time.monotonic_ns()
        timing = {
            "version": 3,
            "request_id": request_id,
            "process_epoch_ns": process_epoch_ns,
            "queue_dispatch_ns": max(0, dispatch_ns - accepted_ns),
            "decode_ns": max(0, decoded_ns - decode_started_ns),
            "build_ns": max(0, build_ns),
            "asgi_ns": max(0, asgi_ns),
            "response_collect_ns": max(0, response_collect_ns),
            "cohort_process_cpu_ns": max(0, cohort_process_cpu_ns),
            "scheduler_max_delay_ns": max(0, scheduler_max_delay_ns),
            "concurrent_requests": max(1, concurrent_requests),
            "cohort_overlap_ns": max(0, cohort_overlap_ns),
        }
        result["timing"] = timing
        # No await between write and flush: the event loop will not interleave
        # another task's write mid-line, so each response is emitted whole.
        json.dumps(result, separators=(",", ":"))
        timing["response_encode_ns"] = max(0, time.monotonic_ns() - encode_started_ns)
        encoded = json.dumps(result, separators=(",", ":")) + "\n"
        sys.stdout.write(encoded)
        sys.stdout.flush()
        if isinstance(request_id, str):
            active_concurrency.pop(request_id, None)
            if len(active_concurrency) == 1:
                ended_ns = time.monotonic_ns()
                remaining = next(iter(active_concurrency.values()))
                overlap_started_ns = remaining.get("overlap_started_ns")
                if overlap_started_ns is not None:
                    remaining["overlap_ns"] = int(remaining.get("overlap_ns", 0)) + max(
                        0, ended_ns - int(overlap_started_ns),
                    )
                    remaining["overlap_started_ns"] = None

    async def _handle_admitted(line: bytes, accepted_ns: int) -> None:
        try:
            await _handle(line, accepted_ns)
        finally:
            admission.release()

    tasks: set[asyncio.Task] = set()
    while True:
        await admission.acquire()
        line = await loop.run_in_executor(None, sys.stdin.buffer.readline)
        if not line:
            admission.release()
            break  # stdin closed — host exits cleanly
        accepted_ns = time.monotonic_ns()
        task = asyncio.create_task(_handle_admitted(line, accepted_ns))
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
