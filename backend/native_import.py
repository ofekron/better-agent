"""Import native provider CLI sessions into Better Agent.

Enumerates each provider's native on-disk sessions (claude projects
jsonl, codex rollout files via its sqlite registry) and ingests each as
a Better Agent session by replaying its events through the same
`apply_event` funnel recovery uses. Runs as a single-flight background
job; progress is exposed via `get_status()` for the REST layer.

Scope: claude + codex are supported (their native storage is enumerable
and the recovery replay functions reuse cleanly). agy/gemini native
sessions live in per-conversation sqlite DBs with no shared normalizer
yet — `enumerate_native_sessions` reports them as unsupported so the
caller can surface that honestly rather than silently skipping.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import config_store
import paths
from session_manager import manager as session_manager

logger = logging.getLogger(__name__)

SUPPORTED_KINDS = {"claude", "codex"}


# --------------------------------------------------------------------------- #
# Per-provider native-session descriptors
# --------------------------------------------------------------------------- #

@dataclass
class NativeSession:
    provider_id: str
    provider_kind: str
    native_id: str
    jsonl_path: str
    cwd: str = ""
    title: str = ""
    created_at: str = ""  # ISO-8601; "" when unknown

    @property
    def registry_key(self) -> str:
        return f"{self.provider_kind}:{self.native_id}"


def _provider_records(provider_ids: Optional[list[str]]) -> list[dict]:
    providers = config_store.list_providers().get("providers", [])
    if not provider_ids:
        return providers
    wanted = set(provider_ids)
    return [p for p in providers if p.get("id") in wanted]


def enumerate_native_sessions(
    provider_ids: Optional[list[str]] = None,
) -> list[NativeSession]:
    """List native sessions for the given providers (all if None).

    Unsupported provider kinds are skipped here; the caller learns about
    them via `unsupported_providers()`.
    """
    out: list[NativeSession] = []
    for provider in _provider_records(provider_ids):
        kind = (provider.get("kind") or "claude").lower()
        pid = provider.get("id") or ""
        try:
            if kind == "claude":
                out.extend(_enumerate_claude(pid, provider))
            elif kind == "codex":
                out.extend(_enumerate_codex(pid, provider))
        except Exception:
            logger.exception("native_import: enumerate failed for provider %s (%s)", pid, kind)
    return out


def unsupported_providers(provider_ids: Optional[list[str]] = None) -> list[dict]:
    """Providers whose native sessions we cannot enumerate yet."""
    out: list[dict] = []
    for provider in _provider_records(provider_ids):
        kind = (provider.get("kind") or "claude").lower()
        if kind not in SUPPORTED_KINDS:
            out.append({"id": provider.get("id"), "name": provider.get("name"), "kind": kind})
    return out


# ---------------------------------- claude --------------------------------- #

def _claude_projects_dir(provider: dict) -> Path:
    cfg = provider.get("config_dir") or ""
    if cfg:
        base = Path(os.path.expandvars(cfg)).expanduser()
    else:
        raw = os.environ.get("CLAUDE_CONFIG_DIR", "")
        base = Path(os.path.expandvars(raw)).expanduser() if raw else Path.home() / ".claude"
    return base / "projects"


def _enumerate_claude(provider_id: str, provider: dict) -> list[NativeSession]:
    projects = _claude_projects_dir(provider)
    out: list[NativeSession] = []
    if not projects.exists():
        return out
    for jsonl_path in projects.glob("*/*.jsonl"):
        try:
            st = jsonl_path.stat()
        except OSError:
            continue
        out.append(NativeSession(
            provider_id=provider_id,
            provider_kind="claude",
            native_id=jsonl_path.stem,
            jsonl_path=str(jsonl_path),
            # encode_cwd maps both "/" and "_" to "-", so the encoded
            # dirname is NOT reversible to the real cwd — leave blank.
            cwd="",
            created_at=datetime.utcfromtimestamp(st.st_mtime).isoformat() + "Z",
        ))
    return out


# ---------------------------------- codex ---------------------------------- #

def _codex_db_paths(provider: dict) -> list[Path]:
    cfg = provider.get("config_dir") or ""
    if cfg:
        base = Path(os.path.expandvars(cfg)).expanduser()
        return [base / "state_5.sqlite", base / "sqlite" / "state_5.sqlite"]
    try:
        from codex_native import codex_state_db_paths
        return codex_state_db_paths()
    except Exception:
        return [Path.home() / ".codex" / "state_5.sqlite"]


def _enumerate_codex(provider_id: str, provider: dict) -> list[NativeSession]:
    out: list[NativeSession] = []
    for db_path in _codex_db_paths(provider):
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                # Column set varies across codex versions; read what exists.
                cols = {row[1] for row in conn.execute("pragma table_info(threads)").fetchall()}
                select = ", ".join(c for c in (
                    "id", "rollout_path", "cwd", "title", "created_at", "first_user_message",
                ) if c in cols)
                if "id" not in cols or "rollout_path" not in cols:
                    continue
                rows = conn.execute(f"select {select} from threads").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            logger.exception("native_import: codex enumerate failed for %s", db_path)
            continue
        col_names = [c for c in (
            "id", "rollout_path", "cwd", "title", "created_at", "first_user_message",
        ) if c in cols]
        for row in rows:
            rec = dict(zip(col_names, row))
            rollout = rec.get("rollout_path")
            if not rollout or not Path(rollout).exists():
                continue
            title = (rec.get("title") or rec.get("first_user_message") or "").strip()
            out.append(NativeSession(
                provider_id=provider_id,
                provider_kind="codex",
                native_id=str(rec.get("id") or ""),
                jsonl_path=str(rollout),
                cwd=str(rec.get("cwd") or ""),
                title=title,
                created_at=_codex_iso(rec.get("created_at")),
            ))
    return out


def _codex_iso(value) -> str:
    if value is None:
        return ""
    try:
        secs = float(value)
        if secs <= 0:
            return ""
        return datetime.utcfromtimestamp(secs).isoformat() + "Z"
    except (TypeError, ValueError):
        return ""


# --------------------------------------------------------------------------- #
# Replay -> segment -> apply (reuses the recovery funnel)
# --------------------------------------------------------------------------- #

def _replay_events(sess: NativeSession) -> list[dict]:
    """Read the whole native session and return the flat enriched event
    list the recovery replay functions produce. Synthesizes a minimal
    run_dir with a state.json so the existing readers work unchanged."""
    from run_recovery import _replay_from_claude_jsonl, _replay_from_codex_rollout

    jsonl_path = Path(sess.jsonl_path)
    try:
        inode = jsonl_path.stat().st_ino
    except OSError:
        inode = 0

    with tempfile.TemporaryDirectory(prefix="nativeimport-") as tmp:
        run_dir = Path(tmp)
        state = {
            "session_id": sess.native_id,
            "jsonl_path": str(jsonl_path),
            "pre_query_byte_offset": 0,
            "pre_query_jsonl_inode": inode,
        }
        (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        if sess.provider_kind == "codex":
            events, _ctx = _replay_from_codex_rollout(run_dir)
            return events
        return _replay_from_claude_jsonl(run_dir)


@dataclass
class _Turn:
    prompt: str = ""
    events: list[dict] = field(default_factory=list)


def _event_data(event: dict) -> dict:
    data = event.get("data") if isinstance(event, dict) else None
    return data if isinstance(data, dict) else {}


def _is_user_prompt(data: dict) -> bool:
    if data.get("isSidechain") or data.get("isMeta"):
        return False
    if data.get("type") != "user":
        return False
    content = (data.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(
            isinstance(i, dict) and i.get("type") == "tool_result" for i in content
        )
    return False


def _extract_text(data: dict) -> str:
    content = (data.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and item.get("text"):
                    parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
    return "\n".join(parts).strip()


def _segment_turns(events: list[dict]) -> list[_Turn]:
    turns: list[_Turn] = []
    for event in events:
        data = _event_data(event)
        if _is_user_prompt(data):
            turns.append(_Turn(prompt=_extract_text(data)))
        else:
            if not turns:
                turns.append(_Turn())  # leading assistant content with no prompt
            turns[-1].events.append(event)
    return turns


def _derive_title(sess: NativeSession, turns: list[_Turn]) -> str:
    if sess.title:
        return sess.title[:80]
    for turn in turns:
        if turn.prompt:
            return turn.prompt[:80]
    return f"{sess.provider_kind} session {sess.native_id[:8]}"


def import_session(sess: NativeSession, *, force: bool = False) -> str:
    """Ingest one native session as a new Better Agent session.

    Idempotent: a repeat call for an already-imported native session is
    a no-op (returns the existing root_id) unless `force=True`. Returns
    the Better Agent root session id.
    """
    if not force:
        existing = _registry_get(sess.registry_key)
        if existing:
            return existing

    events = _replay_events(sess)
    turns = _segment_turns(events)
    # Drop turns that carry neither a prompt nor any events (noise).
    turns = [t for t in turns if t.prompt or t.events]
    if not turns:
        raise ValueError("native session has no importable events")

    created = session_manager.create(
        name=_derive_title(sess, turns),
        cwd=sess.cwd,
        orchestration_mode="native",
        # session_store only accepts "web"/"cli"; "cli" is the closest
        # origin for a session imported from a native CLI transcript. The
        # idempotency registry is the source of truth for "imported".
        source="cli",
        provider_id=sess.provider_id or None,
    )
    root_id = created["id"]

    from orchs import ApplyEventCtx, get_strategy
    from run_recovery import _max_event_timestamp, _repair_updated_at_to_last_activity

    strategy = get_strategy("native")
    failures = 0
    with session_manager.batch(root_id):
        session = session_manager.get_ref(root_id)
        if session is None:
            raise RuntimeError("created session vanished before import")
        messages = session.setdefault("messages", [])
        for turn in turns:
            user_msg = {
                "id": str(__import__("uuid").uuid4()),
                "role": "user",
                "content": turn.prompt or "(imported turn)",
                "events": [],
                "timestamp": datetime.now().isoformat(),
                "isStreaming": False,
                "agent_message_uuid": None,
                "source": "native_import",
            }
            assistant_msg = strategy.build_assistant_scaffold()
            messages.append(user_msg)
            messages.append(assistant_msg)
            ctx = ApplyEventCtx(
                manager_sid_holder={"id": sess.native_id},
                workers_list=list(assistant_msg.get("workers") or []),
                user_msg=user_msg,
                root_id=root_id,
            )
            for ev in turn.events:
                try:
                    strategy.apply_event(
                        app_session_id=root_id,
                        msg=assistant_msg,
                        event=ev,
                        ctx=ctx,
                        source_is_provider_stream=True,
                    )
                except Exception:
                    failures += 1
                    logger.exception(
                        "native_import: apply_event failed for %s (uuid=%s)",
                        sess.registry_key, _event_data(ev).get("uuid"),
                    )
            assistant_msg["isStreaming"] = False

    _repair_updated_at_to_last_activity(root_id, _max_event_timestamp(events))
    _registry_set(sess.registry_key, root_id)
    if failures:
        logger.warning(
            "native_import: %s applied with %d failed event(s)", sess.registry_key, failures,
        )
    return root_id


# --------------------------------------------------------------------------- #
# Idempotency registry (single source of truth for "already imported")
# --------------------------------------------------------------------------- #

_REGISTRY_LOCK = threading.Lock()


def _registry_path() -> Path:
    return paths.ba_home() / "native_imports.json"


def _registry_load() -> dict:
    path = _registry_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("native_import: registry read failed at %s", path)
        return {}


def _registry_save(data: dict) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _registry_get(key: str) -> Optional[str]:
    with _REGISTRY_LOCK:
        return _registry_load().get(key) or None


def _registry_set(key: str, root_id: str) -> None:
    with _REGISTRY_LOCK:
        data = _registry_load()
        data[key] = root_id
        _registry_save(data)


def already_imported_keys() -> set[str]:
    with _REGISTRY_LOCK:
        return set(_registry_load().keys())


# --------------------------------------------------------------------------- #
# Background single-flight job
# --------------------------------------------------------------------------- #

@dataclass
class JobStatus:
    status: str = "idle"  # idle | running | done | error
    total: int = 0
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    current: str = ""
    started_at: str = ""
    finished_at: str = ""
    provider_ids: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "total": self.total,
            "imported": self.imported,
            "skipped": self.skipped,
            "failed": self.failed,
            "current": self.current,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "provider_ids": list(self.provider_ids),
            "errors": list(self.errors),
        }


_JOB_LOCK = threading.Lock()
_JOB: Optional[JobStatus] = None


def get_status() -> dict:
    with _JOB_LOCK:
        return (_JOB or JobStatus()).to_dict()


def start_import(provider_ids: Optional[list[str]] = None) -> dict:
    """Start the background import. Single-flight: a second call while
    running returns the current status instead of starting a new job."""
    with _JOB_LOCK:
        global _JOB
        if _JOB is not None and _JOB.status == "running":
            return _JOB.to_dict()
        _JOB = JobStatus(
            status="running",
            provider_ids=list(provider_ids or []),
            started_at=datetime.now().isoformat(),
        )
        status_ref = _JOB
    thread = threading.Thread(
        target=_run_import, args=(status_ref, provider_ids), daemon=True, name="native-import",
    )
    thread.start()
    return status_ref.to_dict()


def _run_import(status: JobStatus, provider_ids: Optional[list[str]]) -> None:
    imported_keys = already_imported_keys()
    sessions = enumerate_native_sessions(provider_ids)
    status.total = len(sessions)
    try:
        for sess in sessions:
            if sess.registry_key in imported_keys:
                status.skipped += 1
                continue
            status.current = sess.registry_key
            try:
                import_session(sess)
                status.imported += 1
                imported_keys.add(sess.registry_key)
            except Exception as exc:
                status.failed += 1
                status.errors.append({"key": sess.registry_key, "error": str(exc)})
                logger.exception("native_import: failed %s", sess.registry_key)
    except Exception:
        status.status = "error"
        logger.exception("native_import: job crashed")
    else:
        status.status = "done"
    finally:
        status.current = ""
        status.finished_at = datetime.now().isoformat()
