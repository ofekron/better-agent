from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal


NativeEngine = Literal["claude-native", "codex-native", "agy-native"]
_ENGINES = {"claude-native", "codex-native", "agy-native"}
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_CODEX_EVIDENCE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class NativeSidCompatibility:
    engine: NativeEngine
    node_id: str
    thread_store_root: str
    claude_project_namespace: str | None

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "schema": 1,
            "engine": self.engine,
            "node_id": self.node_id,
            "thread_store_root": self.thread_store_root,
            "claude_project_namespace": self.claude_project_namespace,
        }


def _canonical_root(value: str | Path, *, strict: bool) -> str:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("native thread-store root must be absolute")
    try:
        return str(path.resolve(strict=strict))
    except OSError as exc:
        raise ValueError("native thread-store root is unavailable") from exc


def derive_admitted_native_sid_compatibility(
    *,
    engine: NativeEngine,
    node_id: str,
    thread_store_root: str | Path,
    claude_project_namespace: str | None = None,
) -> NativeSidCompatibility:
    if engine not in _ENGINES:
        raise ValueError("native SID engine is invalid")
    if type(node_id) is not str or not _NODE_ID_RE.fullmatch(node_id):
        raise ValueError("native SID node is invalid")
    if engine == "claude-native":
        if (
            type(claude_project_namespace) is not str
            or not claude_project_namespace
            or claude_project_namespace in {".", ".."}
            or Path(claude_project_namespace).name
            != claude_project_namespace
        ):
            raise ValueError("Claude native project namespace is invalid")
    elif claude_project_namespace is not None:
        raise ValueError(
            "Claude native project namespace is invalid for this engine"
        )
    return NativeSidCompatibility(
        engine=engine,
        node_id=node_id,
        thread_store_root=_canonical_root(thread_store_root, strict=False),
        claude_project_namespace=claude_project_namespace,
    )


def admitted_native_routing_node_id(
    *,
    app_session_id: str,
    worker_session_id: str | None,
) -> str:
    from session_manager import manager as session_manager

    routing_session_id = worker_session_id or app_session_id
    session = session_manager.get_fields(
        routing_session_id,
        ("node_id",),
    )
    if type(session) is not dict:
        raise ValueError("native SID admitted routing is unavailable")
    node_id = str(session.get("node_id") or "primary")
    if not _NODE_ID_RE.fullmatch(node_id):
        raise ValueError("native SID admitted routing is invalid")
    return node_id


def _safe_native_sid(native_sid: str) -> bool:
    return (
        type(native_sid) is str
        and bool(native_sid)
        and "\x00" not in native_sid
        and Path(native_sid).name == native_sid
        and native_sid not in {".", ".."}
    )


def _codex_rollout_matches(path: Path, native_sid: str) -> bool:
    consumed = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                consumed += len(line.encode("utf-8"))
                if consumed > _MAX_CODEX_EVIDENCE_BYTES:
                    return False
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    type(event) is dict
                    and event.get("type") == "session_meta"
                    and type(event.get("payload")) is dict
                    and event["payload"].get("id") == native_sid
                ):
                    return True
    except (OSError, UnicodeError):
        return False
    return False


def _sessions_root(path: Path) -> Path | None:
    candidates = [parent for parent in path.parents if parent.name == "sessions"]
    return candidates[0] if len(candidates) == 1 else None


def _compatibility_from_artifact(
    *,
    engine: NativeEngine,
    node_id: str,
    native_sid: str,
    artifact_path: str | Path,
) -> NativeSidCompatibility | None:
    try:
        artifact = Path(artifact_path).resolve(strict=True)
    except OSError:
        return None
    if not artifact.is_file():
        return None
    if engine == "claude-native":
        if (
            artifact.name != f"{native_sid}.jsonl"
            or artifact.parent.parent.name != "projects"
        ):
            return None
        root = artifact.parent.parent
        namespace = artifact.parent.name
    elif engine == "agy-native":
        if (
            artifact.name != f"{native_sid}.db"
            or artifact.parent.name != "conversations"
        ):
            return None
        root = artifact.parent
        namespace = None
    else:
        root = _sessions_root(artifact)
        if root is None or not _codex_rollout_matches(artifact, native_sid):
            return None
        namespace = None
    try:
        return derive_admitted_native_sid_compatibility(
            engine=engine,
            node_id=node_id,
            thread_store_root=root,
            claude_project_namespace=namespace,
        )
    except ValueError:
        return None


def resolve_legacy_native_sid_compatibility(
    *,
    engine: NativeEngine,
    node_id: str,
    native_sid: str,
    artifact_paths: Iterable[str | Path],
) -> NativeSidCompatibility | None:
    if engine not in _ENGINES or not _safe_native_sid(native_sid):
        return None
    resolved: set[NativeSidCompatibility] = set()
    for artifact_path in artifact_paths:
        compatibility = _compatibility_from_artifact(
            engine=engine,
            node_id=node_id,
            native_sid=native_sid,
            artifact_path=artifact_path,
        )
        if compatibility is not None:
            resolved.add(compatibility)
    if len(resolved) != 1:
        return None
    return next(iter(resolved))


__all__ = [
    "NativeSidCompatibility",
    "admitted_native_routing_node_id",
    "derive_admitted_native_sid_compatibility",
    "resolve_legacy_native_sid_compatibility",
]
