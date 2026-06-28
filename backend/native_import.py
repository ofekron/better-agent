"""Import native provider CLI sessions into Better Agent.

Enumerates each provider's native on-disk sessions and ingests each as
a Better Agent session by replaying its events through the same
`apply_event` funnel recovery uses:

  - claude: `<config_dir>/projects/*/*.jsonl` (recovery replay funcs)
  - codex:  `state_5.sqlite` threads table → rollout files
  - agy:    `~/.gemini/antigravity-cli/conversations/*.db` (main-thread
            extractor in runner_agy.extract_main_conversation_events)
  - gemini:`~/.gemini/tmp/*/chats/session-*.jsonl` chat-history normalizer

Runs as a single-flight background job; progress is exposed via
`get_status()` for the REST layer. agy/gemini only carry what the native
format stores (e.g. gemini tool calls are text-embedded) — imported
faithfully within that constraint.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import config_store
import paths
from session_manager import manager as session_manager

logger = logging.getLogger(__name__)



def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically (temp file + replace) so a crash
    mid-write cannot leave a truncated job/registry file — the two pieces
    of state that make import survive restarts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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


def _is_junk_cwd(cwd: str) -> bool:
    """Temp / BA-internal working directories that aren't real projects —
    system temp, the BA state home, and their parents. Empty (unknown)
    cwd is NOT junk (caller decides via the project filter)."""
    if not cwd:
        return False
    try:
        p = Path(cwd).expanduser().resolve()
    except OSError:
        return False
    roots = [paths.ba_home(), Path("/tmp"), Path("/private/tmp"),
             Path("/var/folders"), Path("/private/var/folders")]
    for r in roots:
        try:
            rr = r.resolve()
        except OSError:
            continue
        if p == rr or rr in p.parents:
            return True
    return False


def _under_projects(cwd: str, project_paths: list[str]) -> bool:
    if not cwd:
        return False
    try:
        p = Path(cwd).expanduser().resolve()
    except OSError:
        return False
    for pp in project_paths:
        try:
            base = Path(pp).expanduser().resolve()
        except OSError:
            continue
        if p == base or base in p.parents:
            return True
    return False


def _ba_managed_native_ids() -> set[str]:
    """Native session ids Better Agent itself spawned or manages — its own
    user sessions plus internal agent sessions (delegate forks, sub-sessions,
    adv-sync review runs, workers, supervisor sessions). These already live
    in BA (or did), so importing their native transcripts would duplicate
    agent/internal sessions rather than recover a real user conversation.

    Detected two ways: every BA-spawned provider session has a run dir whose
    `state.json` records its `session_id`; and current BA session trees
    reference their provider sid as `agent_session_id`. A native session
    matching either is BA-managed and skipped at import."""
    ids: set[str] = set()
    try:
        from runs_dir import runs_root
        root = runs_root()
        if root.exists():
            for d in root.iterdir():
                st = d / "state.json" if d.is_dir() else None
                if not st or not st.exists():
                    continue
                try:
                    o = json.loads(st.read_text(encoding="utf-8"))
                except Exception:
                    continue
                sid = o.get("session_id") if isinstance(o, dict) else None
                if isinstance(sid, str) and sid:
                    ids.add(sid)
    except Exception:
        logger.exception("native_import: runs scan failed")
    try:
        import session_store
        for s in session_store.list_sessions():
            for k in ("agent_session_id", "supervisor_agent_session_id"):
                v = s.get(k)
                if isinstance(v, str) and v:
                    ids.add(v)
    except Exception:
        logger.exception("native_import: session_store scan failed")
    return ids


def enumerate_native_sessions(
    provider_ids: Optional[list[str]] = None,
    project_paths: Optional[list[str]] = None,
    hydrate: bool = True,
) -> list[NativeSession]:
    """List native sessions for the given providers (all if None).

    `project_paths` opts into a project filter: only sessions whose cwd is
    under one of those project roots are returned, and junk cwds (system
    temp, the BA state home) are excluded. None disables both (legacy
    "import everything" behavior). Unknown provider kinds are skipped.

    `hydrate=False` skips the per-session display reads that are not needed
    to identify a session (claude's first-`cwd` jsonl scan). Identity fields
    (`provider_kind`, `native_id`) are always populated. Used by
    `count_native_sessions` so a counts-only preview avoids reading every
    jsonl on disk. MUST stay True whenever `project_paths` is set — the
    project filter needs `cwd`.
    """
    out: list[NativeSession] = []
    for provider in _provider_records(provider_ids):
        kind = (provider.get("kind") or "claude").lower()
        pid = provider.get("id") or ""
        try:
            if kind == "claude":
                out.extend(_enumerate_claude(pid, provider, hydrate=hydrate))
            elif kind == "codex":
                out.extend(_enumerate_codex(pid, provider))
            elif kind == "agy":
                out.extend(_enumerate_agy(pid, provider))
            elif kind == "gemini":
                out.extend(_enumerate_gemini(pid, provider))
        except Exception:
            logger.exception("native_import: enumerate failed for provider %s (%s)", pid, kind)
    if project_paths is not None:
        out = [s for s in out if _under_projects(s.cwd, project_paths) and not _is_junk_cwd(s.cwd)]
    # Always skip native sessions Better Agent itself spawned/manages — they
    # are agent/internal sessions (or already-in-BA user sessions), not real
    # external conversations worth importing.
    managed = _ba_managed_native_ids()
    if managed:
        out = [s for s in out if s.native_id not in managed]
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


def _first_cwd_from_jsonl(path: Path, *, max_lines: int = 40) -> str:
    """Read the first `cwd` field from a claude session jsonl. Each line is
    a full SDK record and most carry the working directory; we stop at the
    first hit so huge transcripts aren't fully parsed."""
    try:
        with path.open(encoding="utf-8") as f:
            for _ in range(max_lines):
                raw = f.readline()
                if not raw:
                    break
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                cwd = obj.get("cwd") if isinstance(obj, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        pass
    return ""


def _enumerate_claude(provider_id: str, provider: dict, *, hydrate: bool = True) -> list[NativeSession]:
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
            cwd=_first_cwd_from_jsonl(jsonl_path) if hydrate else "",
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


# ---------------------------------- agy ------------------------------------ #

def _agy_home(provider: dict) -> Path:
    # agy hard-wires $HOME/.gemini/antigravity-cli (no config-dir env);
    # honor a provider config_dir override as the HOME root when set.
    cfg = provider.get("config_dir") or ""
    return Path(os.path.expandvars(cfg)).expanduser() if cfg else Path.home()


def _agy_cwd_map(home: Path) -> dict[str, str]:
    """Reverse agy's `last_conversations.json` (cwd → conversation_id)
    into conversation_id → cwd so each imported session recovers its
    original working directory."""
    path = home / ".gemini" / "antigravity-cli" / "cache" / "last_conversations.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(v): str(k) for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _enumerate_agy(provider_id: str, provider: dict) -> list[NativeSession]:
    home = _agy_home(provider)
    convs = home / ".gemini" / "antigravity-cli" / "conversations"
    if not convs.exists():
        return []
    cwd_map = _agy_cwd_map(home)
    out: list[NativeSession] = []
    for db_path in convs.glob("*.db"):
        try:
            st = db_path.stat()
        except OSError:
            continue
        conv_id = db_path.stem
        out.append(NativeSession(
            provider_id=provider_id,
            provider_kind="agy",
            native_id=conv_id,
            jsonl_path=str(db_path),
            cwd=cwd_map.get(conv_id, ""),
            created_at=datetime.utcfromtimestamp(st.st_mtime).isoformat() + "Z",
        ))
    return out


# --------------------------------- gemini ---------------------------------- #

def _gemini_home(provider: dict) -> Path:
    cfg = provider.get("config_dir") or ""
    if cfg:
        return Path(os.path.expandvars(cfg)).expanduser()
    raw = os.environ.get("GEMINI_CLI_HOME", "")
    return Path(os.path.expandvars(raw)).expanduser() if raw else Path.home()


def _gemini_read_meta(path: Path) -> tuple[str, str, str]:
    """First-line metadata → (session_id, start_time_iso, first_user_prompt).

    The gemini CLI writes one `session-*.jsonl` per conversation under
    `~/.gemini/tmp/<project>/chats/`. Line 1 is metadata; subsequent
    lines are `{type: user|gemini|$set, ...}` events. We stream only
    until the first user prompt to avoid loading large transcripts.
    """
    session_id = path.stem
    started_at = ""
    first_prompt = ""
    try:
        with path.open(encoding="utf-8") as f:
            for raw in f:
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if "sessionId" in obj and not started_at:
                    session_id = str(obj.get("sessionId") or session_id)
                    started_at = str(obj.get("startTime") or "")
                    continue
                if obj.get("type") == "user" and not first_prompt:
                    content = obj.get("content")
                    if isinstance(content, list):
                        parts = [str(i.get("text", "")) for i in content if isinstance(i, dict)]
                        first_prompt = "\n".join(p for p in parts if p).strip()
                    elif isinstance(content, str):
                        first_prompt = content.strip()
                    if first_prompt:
                        break
    except OSError:
        pass
    return session_id, started_at, first_prompt


def _enumerate_gemini(provider_id: str, provider: dict) -> list[NativeSession]:
    tmp = _gemini_home(provider) / ".gemini" / "tmp"
    if not tmp.exists():
        return []
    out: list[NativeSession] = []
    for project_dir in tmp.iterdir():
        if not project_dir.is_dir():
            continue
        cwd = ""
        root_file = project_dir / ".project_root"
        if root_file.exists():
            try:
                cwd = root_file.read_text(encoding="utf-8").strip()
            except OSError:
                cwd = ""
        chats = project_dir / "chats"
        if not chats.is_dir():
            continue
        for session_path in chats.glob("session-*.jsonl"):
            session_id, started_at, first_prompt = _gemini_read_meta(session_path)
            out.append(NativeSession(
                provider_id=provider_id,
                provider_kind="gemini",
                native_id=session_id,
                jsonl_path=str(session_path),
                cwd=cwd,
                title=first_prompt[:80],
                created_at=started_at,
            ))
    return out


# --------------------------------------------------------------------------- #
# Replay -> segment -> apply (reuses the recovery funnel)
# --------------------------------------------------------------------------- #

def _replay_events(sess: NativeSession) -> list[dict]:
    """Read the whole native session and return the flat event list that
    the segmenter consumes. Claude/codex reuse the recovery replay
    functions via a synthesized run_dir; agy/gemini have their own
    native-format normalizers."""
    if sess.provider_kind == "agy":
        import runner_agy
        return runner_agy.extract_main_conversation_events(Path(sess.jsonl_path))
    if sess.provider_kind == "gemini":
        return _gemini_native_events(Path(sess.jsonl_path))

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


_GEMINI_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "better-agent.native_import.gemini")


def _wrapped(role: str, content: list[dict], *, uid: str) -> dict:
    """Build a Claude-shaped agent_message envelope. `apply_event` only
    appends an event to the render tree when it carries a `uuid`, so the
    caller MUST supply a stable one (deterministic → re-import safe)."""
    return {
        "type": "agent_message",
        "data": {
            "type": role,
            "message": {"role": role, "content": content},
            "uuid": uid,
            "parentUuid": "root",
        },
    }


def _gemini_native_events(path: Path) -> list[dict]:
    """Normalize a gemini-CLI chat-history transcript (`session-*.jsonl`)
    into Claude-shaped events: `user` lines → user-prompt turn
    boundaries, `gemini` lines → assistant text. Metadata and `$set`
    lines are skipped. Tool calls are text-embedded in this format, so
    they ride along inside the assistant text."""
    events: list[dict] = []
    try:
        with path.open(encoding="utf-8") as f:
            for raw in f:
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                etype = obj.get("type")
                if etype == "user":
                    content = obj.get("content")
                    if isinstance(content, list):
                        parts = [str(i.get("text", "")) for i in content if isinstance(i, dict)]
                        text = "\n".join(p for p in parts if p).strip()
                    elif isinstance(content, str):
                        text = content.strip()
                    else:
                        text = ""
                    if text:
                        uid = str(uuid.uuid5(_GEMINI_UUID_NAMESPACE, f"user|{text}"))
                        events.append(_wrapped("user", [{"type": "text", "text": text}], uid=uid))
                elif etype == "gemini":
                    content = obj.get("content")
                    if isinstance(content, str):
                        text = content.strip()
                    elif isinstance(content, list):
                        text = "\n".join(
                            str(i.get("text", "")) for i in content if isinstance(i, dict)
                        ).strip()
                    else:
                        text = ""
                    if text:
                        uid = str(uuid.uuid5(_GEMINI_UUID_NAMESPACE, f"assistant|{text}"))
                        events.append(_wrapped("assistant", [{"type": "text", "text": text}], uid=uid))
    except OSError:
        logger.exception("native_import: gemini read failed for %s", path)
    return events


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
    """Group a flat event stream into user-prompt → assistant-event turns.

    Two real-data cases handled (both surfaced by importing live sessions):

      - Leading metadata (ai-title / queue-operation / file-history-snapshot)
        arrives BEFORE the first user prompt. Creating a synthetic turn for
        it yields an empty leading assistant bubble, so leading non-boundary
        events are buffered and prepended to the first real turn (where
        apply_event still fires their side effects, e.g. ai-title rename).
      - The same prompt emitted more than once (agy re-emits; replays) would
        fork consecutive prompt-only turns with empty assistants. Consecutive
        boundaries collapse into the latest prompt instead.
    """
    turns: list[_Turn] = []
    leading: list[dict] = []
    for event in events:
        data = _event_data(event)
        if _is_user_prompt(data):
            prompt = _extract_text(data)
            if turns and not turns[-1].events:
                turns[-1].prompt = prompt  # collapse consecutive prompt-only turn
            else:
                turns.append(_Turn(prompt=prompt))
                if leading:
                    turns[-1].events[:0] = leading
                    leading = []
        elif turns:
            turns[-1].events.append(event)
        else:
            leading.append(event)
    if not turns and leading:
        turns.append(_Turn(events=leading))  # no prompt at all → one synthetic turn
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
    with _import_lock_for(sess.registry_key):
        if not force:
            existing = _registry_get(sess.registry_key)
            if existing:
                return existing
        return _import_session_locked(sess)


def _import_session_locked(sess: NativeSession) -> str:
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
        # "import" is a first-class source so imported sessions are
        # distinguishable in advanced search (web / cli / import). The
        # idempotency registry is the source of truth for "already imported".
        source="import",
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
            # A turn whose every event bypassed the render tree (pure
            # metadata) leaves an empty assistant bubble — drop the pair.
            if not assistant_msg.get("events"):
                messages.pop()
                messages.pop()

    # Every turn collapsed to pure metadata → nothing renderable. Tear down
    # the just-created empty session rather than leave an orphan bubble.
    if not (session_manager.get(root_id) or {}).get("messages"):
        try:
            session_manager.delete(root_id)
        except Exception:
            logger.exception("native_import: failed to delete empty session %s", root_id)
        raise ValueError("native session has no importable events")

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


# Per-key import lock: closes the TOCTOU window between the idempotency
# registry check and session creation so two concurrent imports of the same
# native session can't create duplicate Better Agent sessions.
_IMPORT_LOCKS_GUARD = threading.Lock()
_IMPORT_LOCKS: dict[str, threading.Lock] = {}


def _import_lock_for(key: str) -> threading.Lock:
    with _IMPORT_LOCKS_GUARD:
        lock = _IMPORT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _IMPORT_LOCKS[key] = lock
        return lock


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
    _atomic_write_text(_registry_path(), json.dumps(data, indent=2))


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


def count_native_sessions(provider_ids: Optional[list[str]] = None) -> dict:
    """Counts-only preview of importable native sessions, grouped by provider.

    Returns `{total, imported, pending, by_provider:{kind:{total,imported,
    pending}}}`. Drives the settings panel without shipping one row per
    session — the per-session list can reach hundreds of MB across a full
    Claude+Codex history. Uses `hydrate=False` so identifying the sessions
    never reads their jsonl bodies.
    """
    sessions = enumerate_native_sessions(provider_ids, hydrate=False)
    imported = already_imported_keys()
    by_provider: dict[str, dict] = {}
    for s in sessions:
        g = by_provider.setdefault(
            s.provider_kind, {"total": 0, "imported": 0, "pending": 0}
        )
        g["total"] += 1
        if s.registry_key in imported:
            g["imported"] += 1
        else:
            g["pending"] += 1
    total = sum(g["total"] for g in by_provider.values())
    imported_n = sum(g["imported"] for g in by_provider.values())
    return {
        "total": total,
        "imported": imported_n,
        "pending": total - imported_n,
        "by_provider": by_provider,
    }


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


def _job_state_path() -> Path:
    return paths.ba_home() / "native_import_job.json"


def _persist_job(status: JobStatus) -> None:
    """Checkpoint job state to disk so an interrupted import survives a
    backend restart (see `resume_if_interrupted`). Best-effort: a write
    failure must never abort the in-memory job."""
    try:
        _atomic_write_text(_job_state_path(), json.dumps(status.to_dict()))
    except Exception:
        logger.exception("native_import: failed to persist job state")


def _load_persisted_job() -> Optional[dict]:
    path = _job_state_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def get_status() -> dict:
    with _JOB_LOCK:
        if _JOB is not None:
            return _JOB.to_dict()
    # No in-memory job (e.g. right after restart, before resume fires) —
    # surface the last persisted state so the UI isn't blank.
    persisted = _load_persisted_job()
    return persisted if persisted else JobStatus().to_dict()


def start_import(
    provider_ids: Optional[list[str]] = None,
    limit: Optional[int] = None,
    project_paths: Optional[list[str]] = None,
) -> dict:
    """Start the background import. Single-flight: a second call while
    running returns the current status instead of starting a new job.

    `limit` caps the number of NEW sessions imported (already-imported
    sessions are still skipped, not counted against the limit).
    `project_paths` opts into the project filter (see
    `enumerate_native_sessions`); resume ignores it and finishes all."""
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
    _persist_job(status_ref)
    thread = threading.Thread(
        target=_run_import, args=(status_ref, provider_ids, limit, project_paths),
        daemon=True, name="native-import",
    )
    thread.start()
    return status_ref.to_dict()


def resume_if_interrupted() -> None:
    """Resume an import that a backend restart interrupted.

    A persisted job with status "running" can only exist if the process
    died mid-import — a live job is held in memory only. Re-running for
    its provider scope is safe: the idempotency registry skips every
    session already imported before the crash, so the resume finishes the
    remainder without duplicates. Called from backend startup. Resume
    ignores any prior limit (it finishes everything remaining).
    """
    persisted = _load_persisted_job()
    if not persisted or persisted.get("status") != "running":
        return
    provider_ids = persisted.get("provider_ids") or None
    logger.info("native_import: resuming interrupted import (providers=%s)", provider_ids)
    start_import(provider_ids)


def _run_import(
    status: JobStatus, provider_ids: Optional[list[str]], limit: Optional[int],
    project_paths: Optional[list[str]] = None,
) -> None:
    imported_keys = already_imported_keys()
    sessions = enumerate_native_sessions(provider_ids, project_paths)
    # Import newest first so the most recent conversations land first and a
    # `limit` cap keeps the most recent N. created_at is ISO-8601 (sorts
    # chronologically); unknown ("") timestamps fall to the end.
    sessions.sort(key=lambda s: s.created_at or "", reverse=True)
    status.total = len(sessions)
    _persist_job(status)
    try:
        for sess in sessions:
            if limit is not None and status.imported >= limit:
                break  # cap reached — stop importing new sessions
            if sess.registry_key in imported_keys:
                status.skipped += 1
            else:
                status.current = sess.registry_key
                try:
                    import_session(sess)
                    status.imported += 1
                    imported_keys.add(sess.registry_key)
                except Exception as exc:
                    status.failed += 1
                    status.errors.append({"key": sess.registry_key, "error": str(exc)})
                    logger.exception("native_import: failed %s", sess.registry_key)
            _persist_job(status)  # checkpoint so a crash here resumes cleanly
        status.status = "done"
    except Exception:
        status.status = "error"
        logger.exception("native_import: job crashed")
    finally:
        status.current = ""
        status.finished_at = datetime.now().isoformat()
        _persist_job(status)
