from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any, Callable, Optional

from codex_native import CodexRolloutNormalizer
from codex_native import codex_subagent_sources_from_event
from codex_native import resolve_rollout_path
from codex_normalize import _codex_terminal_state


def _process_returncode(proc: Any) -> Optional[int]:
    return getattr(
        getattr(proc, "_proc", None),
        "returncode",
        getattr(proc, "returncode", None),
    )


async def resolve_rollout_path_for_join(
    thread_id: str,
    *,
    cancel_path: Path,
    proc: Any,
) -> Path:
    while True:
        if cancel_path.exists():
            raise asyncio.CancelledError()
        if _process_returncode(proc) is not None:
            raise RuntimeError("Codex app-server exited before rollout was available")
        path = await asyncio.to_thread(resolve_rollout_path, thread_id)
        if path is not None:
            return path
        await asyncio.sleep(0.05)


def _read_complete_rows(path: Path, start_byte: int) -> tuple[list[bytes], int]:
    rows: list[bytes] = []
    cursor = start_byte
    try:
        with path.open("rb") as file:
            file.seek(start_byte)
            while True:
                raw = file.readline()
                if not raw or not raw.endswith(b"\n"):
                    break
                cursor = file.tell()
                rows.append(raw)
    except OSError:
        return [], start_byte
    return rows, cursor


def _turn_started(record: dict) -> bool:
    if record.get("type") == "task_started":
        return True
    payload = record.get("payload")
    return (
        record.get("type") == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") == "task_started"
    )


async def wait_for_agent_tree_terminal(
    root_path: Path,
    *,
    start_byte: int,
    resolve_path: Optional[Callable[[str], Any]] = None,
    cancel_path: Optional[Path] = None,
    proc: Optional[Any] = None,
) -> None:
    resolver = resolve_path or resolve_rollout_path
    nodes: dict[str, dict[str, Any]] = {
        "__root__": {
            "path": root_path,
            "cursor": max(0, start_byte),
            "normalizer": CodexRolloutNormalizer(namespace="__root__"),
            "terminal": True,
            "is_root": True,
        },
    }
    unresolved: set[str] = set()

    while True:
        if cancel_path is not None and cancel_path.exists():
            raise asyncio.CancelledError()
        if proc is not None and _process_returncode(proc) is not None:
            raise RuntimeError("Codex app-server exited before agent tree completed")

        progressed = False
        node_batch = tuple(nodes.values())
        row_batches = await asyncio.gather(*(
            asyncio.to_thread(_read_complete_rows, node["path"], node["cursor"])
            for node in node_batch
        ))
        for node, (rows, cursor) in zip(node_batch, row_batches):
            if rows:
                progressed = True
                node["cursor"] = cursor
            for raw in rows:
                try:
                    record = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if not node["is_root"]:
                    if _turn_started(record):
                        node["terminal"] = None
                    terminal = _codex_terminal_state(record)
                    if terminal is not None:
                        node["terminal"] = terminal
                for event in node["normalizer"].normalize_line(
                    raw.decode("utf-8", errors="replace")
                ):
                    for source in codex_subagent_sources_from_event(event):
                        child_id = source["child_id"]
                        if child_id not in nodes:
                            unresolved.add(child_id)

        unresolved_batch = tuple(unresolved)
        resolved_paths = await asyncio.gather(*(
            (
                resolver(child_id)
                if inspect.iscoroutinefunction(resolver)
                else asyncio.to_thread(resolver, child_id)
            )
            for child_id in unresolved_batch
        ))
        for child_id, path in zip(unresolved_batch, resolved_paths):
            if inspect.isawaitable(path):
                path = await path
            if path is None:
                continue
            nodes[child_id] = {
                "path": Path(path),
                "cursor": 0,
                "normalizer": CodexRolloutNormalizer(namespace=child_id),
                "terminal": None,
                "is_root": False,
            }
            unresolved.remove(child_id)
            progressed = True

        if not unresolved and all(
            node["terminal"] is not None for node in nodes.values()
        ):
            return
        if not progressed:
            await asyncio.sleep(0.05)
