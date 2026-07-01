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

import hashlib
import json
import logging
import os
import re
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
        return f"{self.provider_id or self.provider_kind}:{self.provider_kind}:{self.source_identity_hash}"

    @property
    def legacy_registry_key(self) -> str:
        return f"{self.provider_kind}:{self.native_id}"

    @property
    def source_identity_hash(self) -> str:
        return hashlib.sha256(self.source_identity.encode("utf-8")).hexdigest()[:24]

    @property
    def source_identity(self) -> str:
        if self.jsonl_path:
            try:
                return str(Path(self.jsonl_path).expanduser().resolve())
            except OSError:
                return str(Path(self.jsonl_path).expanduser())
        return self.native_id


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


# Encoded-cwd prefixes for system-temp roots. Claude stores each session
# under projects/<encoded-cwd>/, encoding path separators as '-', so a temp
# cwd like /private/tmp/... becomes a '-private-tmp-...' dir. Trailing '-'
# avoids matching real dirs like /tmpwork.
_JUNK_DIR_PREFIXES = ("-tmp-", "-private-tmp-", "-var-folders-", "-private-var-folders-")
_UUID_PATH_RE = re.compile(r"/agents/agents_workspaces/[0-9a-fA-F-]{36}$")
_AGENT_STRATEGY_PATH_RE = re.compile(
    r"/(?:agents|technical_analysis)/agents_strategies/[^/]+$"
)


def _is_junk_session(sess: NativeSession) -> bool:
    """Temp / BA-internal sessions that aren't real conversations. Uses the
    real cwd when hydrated; for claude's un-hydrated count path (cwd=""),
    infers junk from the encoded project-dir name so counts stay cheap."""
    if sess.cwd:
        return _is_junk_cwd(sess.cwd)
    if sess.provider_kind == "claude":
        name = Path(sess.jsonl_path).parent.name
        return any(name.startswith(p) for p in _JUNK_DIR_PREFIXES)
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


def _is_generated_project_path(project_path: str) -> bool:
    try:
        p = Path(project_path).expanduser().resolve()
    except OSError:
        return True
    text = str(p)
    if p == Path.home().resolve():
        return True
    if _is_junk_cwd(text):
        return True
    return bool(_UUID_PATH_RE.search(text) or _AGENT_STRATEGY_PATH_RE.search(text))


def loaded_project_paths() -> list[str]:
    try:
        import project_store
        projects = project_store.list_projects()
    except Exception:
        logger.exception("native_import: project_store scan failed")
        return []
    out: list[str] = []
    seen: set[str] = set()
    for project in projects:
        if not isinstance(project, dict):
            continue
        if (project.get("node_id") or "primary") != "primary":
            continue
        path = project.get("path") or project.get("cwd")
        if not isinstance(path, str) or not path:
            continue
        if _is_generated_project_path(path):
            continue
        try:
            norm = str(Path(path).expanduser().resolve())
        except OSError:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _ba_managed_native_ids() -> set[str]:
    """Native session ids Better Agent itself spawned or manages — its own
    user sessions plus internal agent sessions (delegate forks, sub-sessions,
    adv-sync review runs, workers, supervisor sessions). These already live
    in BA (or did), so importing their native transcripts would duplicate
    agent/internal sessions rather than recover a real user conversation.

    Detected two ways: the durable spawn ledger records BA-spawned provider
    session ids, and current BA session trees reference their provider sid as
    `agent_session_id`. A native session matching either is BA-managed and
    skipped at import."""
    ids: set[str] = set()
    import spawn_ledger
    spawn_ledger.bootstrap_from_run_dirs_once()
    try:
        import session_store
        for s in session_store.list_sessions():
            # Only provider native sids that list_sessions() actually projects.
            # (caller_agent_session_id is a BA app-session id, not a native sid;
            # forked_from_supervisor_agent_sid isn't in the summary — both would
            # be dead/wrong here. The prior supervisor sid is captured anyway via
            # its run dir / the durable ledger.)
            for k in ("agent_session_id", "supervisor_agent_session_id",
                      "forked_from_agent_sid"):
                v = s.get(k)
                if isinstance(v, str) and v:
                    ids.add(v)
    except Exception:
        logger.exception("native_import: session_store scan failed")
    # Durable provenance: sids harvested at run-dir reap time survive the
    # 7-day prune / session delete that erase live run dirs above.
    ids |= spawn_ledger.all_sids()
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
        out = [s for s in out if _under_projects(s.cwd, project_paths)]
    # Always skip junk (system-temp / BA-internal) and BA-spawned sessions —
    # both are orchestration artifacts, not real external conversations. The
    # junk filter runs regardless of `project_paths`; the global import path
    # used to skip it and offered BA's own integration-test runs for import.
    out = [s for s in out if not _is_junk_session(s)]
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
        base = Path.home() / ".claude"
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
    timestamp: str = ""
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


def _event_timestamp(event: dict) -> str:
    data = _event_data(event)
    containers = [event, data]
    message = data.get("message")
    if isinstance(message, dict):
        containers.append(message)
    for container in containers:
        raw = container.get("timestamp") or container.get("created_at")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def _local_iso(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat()


def _fallback_prompt(sess: NativeSession) -> str:
    return _normalize_import_prompt(sess.title or "")


_INTERNAL_IMPORT_PROMPT_SIGNATURES = (
    "Better Agent run.sh startup checker",
    "startup checker for Z.AI",
    "direct Claude Code CLI process configured for the Z.AI Claude-compatible provider",
    "machine completion worker for the requirement-analysis pipeline",
    "You are an adversarial reviewer for Better Agent",
    "You are a HOSTILE adversarial code reviewer.",
    "You are adversarial reviewer for a Better Agent RCA.",
    "You are worker:testape",
    "Better Agent requires a parent-session reply after subagent work.",
)


_INTERNAL_IMPORT_PROMPT_PREFIXES = (
    "<self>",
    "<worker-prep>",
    "<machine-completion-prep>",
    "<search-worker-provision>",
    "<get-requirements-processor-prep>",
    "<file-editor-provision>",
    "<project-structure-maintainer-provision>",
    "<verdict-prompt>",
    "<command-name>",
    "<system_bootstrap>",
)

_INTERNAL_IMPORT_PROMPT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"^use these selected capabilities for this run only\.",
        r"^the following injected context is from better agent, not from the user\.",
        r"^---\s*name:\s*test-ui-expert\b",
        r"^=== your workspace ===",
        r"^you are a technical analysis expert and a super ninja trader\b",
        r"^(adversarial(ly)? (re-)?review|read-only adversarial|final adversarial review|second adversarial review)",
        r"^you are a hostile adversarial code reviewer\b",
        r"^use hostile adversarial review stance",
        r"^#\s*testape/[a-z0-9_/-]+\b",
        r"^▶\s*👤\s*user\b",
        r"^in /users/[^,\n]+,\s*adversarially review",
        r"^(read-only:|read-only adversarial validation|in /users/[^,\n]+,\s*read-only:|in /users/[^,\n]+,\s*audit\b)",
        r"^please review the following git diff representing",
        r"^investigate this testape product bug\b.*\breturn commit-ready facts",
        r"you are the dedicated testape ui-testing expert",
        r"^using the testape\b",
        r"^(navigate to|open) https?://.*--- you are the dedicated testape ui-testing expert",
        r"^(a device worker|a sign-in form has two fields|audit this testape learned state graph)",
        r"^(convert these verified discoveries|analyze these detector/run measurements|preserve observed analytics confirmations)",
        r"^runtime ui test only\. do not inspect files\.",
        r"^reply with exactly:\s*testape_ok$",
    )
)


_INJECTED_PROMPT_SUFFIX_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"\n\s*# global preferences\s*\n\s*## ",
        r"\n\s*# agents\.md instructions for /users/",
        r"\n\s*use these selected capabilities for this run only\. they are active context",
        r"\n\s*the following injected context is from better agent, not from the user\.",
    )
)


class SkippedNativeSession(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _normalize_import_prompt(prompt: str) -> str:
    text = (prompt or "").strip()
    cut_at: Optional[int] = None
    for pattern in _INJECTED_PROMPT_SUFFIX_PATTERNS:
        match = pattern.search(text)
        if match and match.start() > 0:
            cut_at = match.start() if cut_at is None else min(cut_at, match.start())
    if cut_at is None:
        return text
    return text[:cut_at].rstrip()


def _is_internal_import_prompt(prompt: str) -> bool:
    text = _normalize_import_prompt(prompt)
    return (
        text.startswith(_INTERNAL_IMPORT_PROMPT_PREFIXES)
        or any(sig in text for sig in _INTERNAL_IMPORT_PROMPT_SIGNATURES)
        or any(pattern.search(text) for pattern in _INTERNAL_IMPORT_PROMPT_PATTERNS)
    )


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
            prompt = _normalize_import_prompt(_extract_text(data))
            timestamp = _event_timestamp(event)
            if turns and not turns[-1].events:
                turns[-1].prompt = prompt  # collapse consecutive prompt-only turn
                turns[-1].timestamp = timestamp
            else:
                turns.append(_Turn(prompt=prompt, timestamp=timestamp))
                if leading:
                    turns[-1].events[:0] = leading
                    leading = []
        elif turns:
            turns[-1].events.append(event)
        else:
            leading.append(event)
    return turns


def _native_created_iso(sess: NativeSession, turns: Optional[list[_Turn]] = None) -> Optional[str]:
    """The native conversation's creation time as a naive-local ISO string
    matching the session-record convention, so analytics bucket imported
    sessions under their REAL date, not the import time. Native timestamps
    are UTC (ISO, often trailing 'Z'); convert to local. None when unknown."""
    if turns:
        for turn in turns:
            created = _local_iso(turn.timestamp)
            if created:
                return created
    return _local_iso(sess.created_at)


def _max_native_iso(values: list[str]) -> Optional[str]:
    best = ""
    for raw in values:
        value = _local_iso(raw)
        if value and value > best:
            best = value
    return best or None


def _native_turn_activity_iso(turn: _Turn) -> Optional[str]:
    return _max_native_iso([turn.timestamp, *[_event_timestamp(ev) for ev in turn.events]])


def _native_last_activity_iso(sess: NativeSession, turns: list[_Turn]) -> Optional[str]:
    values = [sess.created_at]
    for turn in turns:
        values.append(turn.timestamp)
        values.extend(_event_timestamp(ev) for ev in turn.events)
    return _max_native_iso(values)


def _imported_last_user_prompt_iso(session: dict) -> Optional[str]:
    values = [
        str(m.get("timestamp") or "")
        for m in (session.get("messages") or [])
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    return _max_native_iso(values)


def _imported_first_user_prompt_iso(session: dict) -> Optional[str]:
    best = ""
    for message in session.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        value = _local_iso(str(message.get("timestamp") or ""))
        if value and (not best or value < best):
            best = value
    return best or None


def _repair_imported_root_timestamps(root_id: str, fallback: Optional[str] = None) -> None:
    session_manager.flush_pending_persists()
    root = session_manager.get_ref(root_id)
    if not isinstance(root, dict):
        return
    first_prompt = _imported_first_user_prompt_iso(root)
    last_activity = _imported_last_user_prompt_iso(root) or fallback
    if not first_prompt and not last_activity:
        return
    if first_prompt:
        root["created_at"] = first_prompt
    if last_activity:
        root["updated_at"] = last_activity
    import session_store
    session_store.write_session_full(
        root,
        bump_updated_at=False,
        preserve_projection_fields=True,
    )


def repair_imported_roots(project_paths: Optional[list[str]] = None) -> dict:
    import session_store
    repaired = 0
    deleted = 0
    removed_projects = 0
    project_filtered = project_paths if project_paths is not None else loaded_project_paths()
    try:
        import project_store
        for project in project_store.list_projects():
            path = project.get("path") if isinstance(project, dict) else None
            node_id = project.get("node_id") if isinstance(project, dict) else None
            if isinstance(path, str) and _is_generated_project_path(path):
                if project_store.remove_project(path, node_id=node_id or "primary"):
                    removed_projects += 1
    except Exception:
        logger.exception("native_import: imported-project cleanup failed")
    registry = _registry_load()
    deleted_roots: set[str] = set()
    for path in (paths.ba_home() / "sessions").glob("*.json"):
        if path.name.endswith(".summary.json"):
            continue
        try:
            root = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if root.get("source") != "import":
            continue
        if project_filtered and not _under_projects(root.get("cwd") or "", project_filtered):
            session_manager.delete(root["id"])
            deleted_roots.add(root["id"])
            deleted += 1
            continue
        users = [
            m for m in (root.get("messages") or [])
            if isinstance(m, dict) and m.get("role") == "user"
        ]
        first_raw_prompt = _extract_text({"message": {"content": users[0].get("content")}}) if users else ""
        first_prompt = _normalize_import_prompt(first_raw_prompt)
        first_user_ts = _imported_first_user_prompt_iso(root)
        last_user_ts = _imported_last_user_prompt_iso(root)
        if not first_prompt or _is_internal_import_prompt(first_prompt) or not last_user_ts:
            session_manager.delete(root["id"])
            deleted_roots.add(root["id"])
            deleted += 1
            continue
        changed = False
        if first_raw_prompt != first_prompt:
            users[0]["content"] = first_prompt
            changed = True
            if not root.get("name") or root.get("name") == first_raw_prompt[:80]:
                root["name"] = first_prompt[:80]
        if first_user_ts and root.get("created_at") != first_user_ts:
            root["created_at"] = first_user_ts
            changed = True
        if root.get("updated_at") != last_user_ts:
            root["updated_at"] = last_user_ts
            changed = True
        if changed:
            session_store.write_session_full(
                root,
                bump_updated_at=False,
                preserve_projection_fields=True,
            )
            repaired += 1
    if deleted_roots:
        _registry_save({k: v for k, v in registry.items() if v not in deleted_roots})
    return {"repaired": repaired, "deleted": deleted, "removed_projects": removed_projects}


def _derive_title(sess: NativeSession, turns: list[_Turn]) -> str:
    title = _normalize_import_prompt(sess.title or "")
    if title and not _is_internal_import_prompt(title):
        return title[:80]
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
            existing = _registry_get_for(sess)
            if existing:
                return existing
        return _import_session_locked(sess)


def _import_session_locked(sess: NativeSession) -> str:
    events = _replay_events(sess)
    turns = _segment_turns(events)
    fallback_prompt = _fallback_prompt(sess)
    if not turns and fallback_prompt and events:
        turns = [_Turn(prompt=fallback_prompt, timestamp=sess.created_at, events=events)]
    if fallback_prompt and turns and not turns[0].prompt:
        turns[0].prompt = fallback_prompt
    turns = [t for t in turns if t.prompt]
    if not turns:
        raise SkippedNativeSession("native session has no recovered user prompt")
    first_prompt = next((t.prompt for t in turns if t.prompt), "")
    if _is_internal_import_prompt(first_prompt):
        raise SkippedNativeSession("internal Better Agent native session")

    created = session_manager.create(
        name=_derive_title(sess, turns),
        cwd=sess.cwd,
        orchestration_mode="native",
        # "import" is a first-class source so imported sessions are
        # distinguishable in advanced search (web / cli / import). The
        # idempotency registry is the source of truth for "already imported".
        source="import",
        provider_id=sess.provider_id or None,
        # The user explicitly triggered the import; they are aware of and
        # own the resulting session.
        user_initiated=True,
        # Preserve the native conversation's date so usage analytics bucket it
        # under when it actually happened, not the import moment.
        created_at=_native_created_iso(sess, turns),
    )
    root_id = created["id"]

    from orchs import ApplyEventCtx, get_strategy
    strategy = get_strategy("native")
    failures = 0
    with session_manager.batch(root_id, bump_updated_at=False):
        session = session_manager.get_ref(root_id)
        if session is None:
            raise RuntimeError("created session vanished before import")
        messages = session.setdefault("messages", [])
        for turn in turns:
            user_msg = {
                "id": str(__import__("uuid").uuid4()),
                "role": "user",
                "content": turn.prompt,
                "events": [],
                "timestamp": _native_created_iso(sess, [turn]) or datetime.now().isoformat(),
                "isStreaming": False,
                "agent_message_uuid": None,
                "source": "native_import",
            }
            assistant_msg = strategy.build_assistant_scaffold()
            assistant_msg["timestamp"] = _native_turn_activity_iso(turn) or user_msg["timestamp"]
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
        last_activity = _imported_last_user_prompt_iso(session) or _native_last_activity_iso(sess, turns)
        if last_activity:
            session["updated_at"] = last_activity

    # Every turn collapsed to pure metadata → nothing renderable. Tear down
    # the just-created empty session rather than leave an orphan bubble.
    if not (session_manager.get(root_id) or {}).get("messages"):
        try:
            session_manager.delete(root_id)
        except Exception:
            logger.exception("native_import: failed to delete empty session %s", root_id)
        raise ValueError("native session has no importable events")

    _repair_imported_root_timestamps(root_id, _native_last_activity_iso(sess, turns))
    _registry_set_for(sess, root_id)
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
        data = _registry_load()
        root_id = data.get(key)
        if not root_id:
            return None
        if _registry_root_exists(root_id):
            return root_id
        data.pop(key, None)
        _registry_save(data)
        return None


def _registry_set(key: str, root_id: str) -> None:
    with _REGISTRY_LOCK:
        data = _registry_load()
        data[key] = root_id
        _registry_save(data)


def _registry_root_exists(root_id: str) -> bool:
    if not root_id:
        return False
    return (paths.ba_home() / "sessions" / f"{root_id}.json").exists()


def _prune_stale_registry_locked(data: dict) -> bool:
    stale = [
        key for key, root_id in data.items()
        if not isinstance(root_id, str) or not _registry_root_exists(root_id)
    ]
    for key in stale:
        data.pop(key, None)
    return bool(stale)


def _registry_get_for(sess: NativeSession) -> Optional[str]:
    with _REGISTRY_LOCK:
        data = _registry_load()
        for key in (sess.registry_key, sess.legacy_registry_key):
            root_id = data.get(key)
            if not root_id:
                continue
            if _registry_root_exists(root_id):
                return root_id
            data.pop(key, None)
            _registry_save(data)
        return None


def _registry_set_for(sess: NativeSession, root_id: str) -> None:
    with _REGISTRY_LOCK:
        data = _registry_load()
        data[sess.registry_key] = root_id
        data.pop(sess.legacy_registry_key, None)
        _registry_save(data)


def _is_imported(sess: NativeSession, keys: set[str]) -> bool:
    return sess.registry_key in keys or sess.legacy_registry_key in keys


def already_imported_keys() -> set[str]:
    with _REGISTRY_LOCK:
        data = _registry_load()
        if _prune_stale_registry_locked(data):
            _registry_save(data)
        return set(data.keys())


def count_native_sessions(
    provider_ids: Optional[list[str]] = None,
    project_paths: Optional[list[str]] = None,
) -> dict:
    """Counts-only preview of importable native sessions, grouped by provider.

    Returns `{total, imported, pending, by_provider:{kind:{total,imported,
    pending}}}`. Drives the settings panel without shipping one row per
    session — the per-session list can reach hundreds of MB across a full
    Claude+Codex history. Uses `hydrate=False` so identifying the sessions
    never reads their jsonl bodies.
    """
    sessions = enumerate_native_sessions(
        provider_ids,
        project_paths,
        hydrate=project_paths is not None,
    )
    imported = already_imported_keys()
    by_provider: dict[str, dict] = {}
    for s in sessions:
        g = by_provider.setdefault(
            s.provider_kind, {"total": 0, "imported": 0, "pending": 0}
        )
        g["total"] += 1
        if _is_imported(s, imported):
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
    project_paths: list[str] = field(default_factory=list)
    all_projects: bool = True
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
            "project_paths": list(self.project_paths),
            "all_projects": bool(self.all_projects),
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
    `enumerate_native_sessions`)."""
    with _JOB_LOCK:
        global _JOB
        if _JOB is not None and _JOB.status == "running":
            return _JOB.to_dict()
        _JOB = JobStatus(
            status="running",
            provider_ids=list(provider_ids or []),
            project_paths=list(project_paths or []),
            all_projects=project_paths is None,
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
    if "all_projects" not in persisted and "project_paths" not in persisted:
        project_paths = None
    else:
        project_paths = None if persisted.get("all_projects") else persisted.get("project_paths")
    logger.info(
        "native_import: resuming interrupted import (providers=%s project_paths=%s)",
        provider_ids, project_paths,
    )
    start_import(provider_ids, project_paths=project_paths)


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
            if _is_imported(sess, imported_keys):
                status.skipped += 1
            else:
                status.current = sess.registry_key
                try:
                    rid = import_session(sess)
                    status.imported += 1
                    imported_keys.add(sess.registry_key)
                    # Unpin the just-imported root so the resident cache stays
                    # bounded — without this the loop would hold every imported
                    # session in RAM and OOM on a large import.
                    session_manager.trim_resident_roots(keep_rid=rid)
                except SkippedNativeSession as exc:
                    status.skipped += 1
                    logger.info("native_import: skipped %s: %s", sess.registry_key, exc.reason)
                except Exception as exc:
                    status.failed += 1
                    status.errors.append({"key": sess.registry_key, "error": str(exc)})
                    logger.exception("native_import: failed %s", sess.registry_key)
            _persist_job(status)  # checkpoint so a crash here resumes cleanly
        repair_imported_roots()
        status.status = "done"
    except Exception:
        status.status = "error"
        logger.exception("native_import: job crashed")
    finally:
        status.current = ""
        status.finished_at = datetime.now().isoformat()
        _persist_job(status)
