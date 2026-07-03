"""Provider-native transcript session miners.

Five native :class:`SessionMinerBase` implementations, one per supported
provider, plus BA-indexed discovery. They are the native-source counterparts to
:class:`session_miner.SessionMiner` (the Better Agent snapshot source);
together that is six SessionMiner implementations of the one abstraction.

- :class:`NativeClaudeSessionMiner` — Claude ``projects/<cwd>/<sid>.jsonl``.
- :class:`NativeCodexSessionMiner` — Codex run-dir ``session_events.jsonl``
  (Claude-shaped, captured by the runner from the Codex stream).
- :class:`NativeGeminiSessionMiner` — Gemini run-dir ``session_events.jsonl``
  (Claude-shaped, pre-normalized by ``runner_gemini``).
- :class:`NativeBetterAgentSessionMiner` — Better Agent's own runner
  (``runner_better_agent``, ``openai`` provider kind) run-dir
  ``session_events.jsonl`` (Claude-shaped).
- Windsurf / Codeium Cascade ``~/.codeium/**/cascade/*.pb`` files
  (AES-GCM encrypted protobuf).

Discovery uses the Better Agent session record only as an index — it gives the
reliable ``cwd``, the ``provider_id`` (→ provider kind), and the
``agent_session_id`` that links to the native transcript. Prompt CONTENT is read
from the native transcript, the raw ground truth (the BA render tree is a
projection that can have gaps after crashes/recovery).

The provider-neutral parsing core (element/candidate shapes, per-format
parsers/extractors, provider-native root helpers) lives in
:mod:`native_elements`; this module owns everything that needs the Better
Agent stack: session records, provider configs, run-dir resolution, and the
:class:`SessionMinerBase` hierarchy.

Each miner namespaces its watermark key (``claude:``/``codex:``/``gemini:``) so
they can share one persisted state dict with the BA snapshot miner without
collision.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from native_elements import (
    NativeCandidate,
    NativeElement,
    _claude_projects_roots,
    _codex_first_cwd,
    _codex_sessions_root,
    _decode_cwd_token,
    _gemini_chats_root,
    _mtime,
    _windsurf_cascade_roots,
)
from paths import bc_home, claude_projects_root_for_session, encode_cwd
from session_miner import SessionMinerBase, SessionVisit, sessions_dir

__all__ = [
    "NativeCandidate",
    "NativeElement",
    "iter_all_native_candidates",
    "NativeClaudeSessionMiner",
    "NativeCodexSessionMiner",
    "NativeGeminiSessionMiner",
    "NativeBetterAgentSessionMiner",
]

_RUNS_DIR_NAME = "runs"


def _ba_session_cwd(sid: str) -> str:
    """Real cwd for a BA app session id, or "" when no record exists.

    Filesystem-first discovery has no BA index, so cwd is recovered from the
    session record when present (the authoritative source) and falls back to
    the encoded dir name otherwise.
    """
    try:
        rec = json.loads((sessions_dir() / f"{sid}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    cwd = rec.get("cwd")
    return cwd if isinstance(cwd, str) else ""


def _provider_kind(data: dict) -> str:
    """Resolve a BA session record's provider kind (claude/codex/gemini).

    Defaults to ``claude`` when the provider is missing or unconfigured, mirroring
    the backend's own default (``config_store`` treats unknown/missing kind as
    claude).
    """
    provider_id = data.get("provider_id")
    if isinstance(provider_id, str) and provider_id:
        try:
            import config_store
            rec = config_store.get_provider(provider_id)
        except Exception:
            rec = None
        if isinstance(rec, dict):
            kind = rec.get("kind")
            if isinstance(kind, str) and kind:
                return kind
    return "claude"


# Codex/Gemini run-dir index: {app_session_id -> run_dir}. Built on demand and
# refreshed when the runs dir mtime changes, so a long-lived process does not
# rescan thousands of run dirs every mining pass.
_RUN_INDEX: dict[str, Path] | None = None
_RUN_INDEX_MTIME: float = 0.0


def _runs_root() -> Path:
    return bc_home() / _RUNS_DIR_NAME


def _run_index() -> dict[str, Path]:
    global _RUN_INDEX, _RUN_INDEX_MTIME
    root = _runs_root()
    try:
        root_mtime = root.stat().st_mtime
    except OSError:
        return {}
    if _RUN_INDEX is not None and root_mtime == _RUN_INDEX_MTIME:
        return _RUN_INDEX
    try:
        from runs_dir import run_dirs_by_app_session
        index = run_dirs_by_app_session(root)
    except Exception:
        index = {}
    if index:
        _RUN_INDEX = index
        _RUN_INDEX_MTIME = root_mtime
        return index
    index = {}
    for state_path in root.glob("*/state.json"):
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        aid = data.get("app_session_id")
        if isinstance(aid, str) and aid:
            index[aid] = state_path.parent
    _RUN_INDEX = index
    _RUN_INDEX_MTIME = root_mtime
    return index


def _iter_ba_session_records(root: Path) -> Iterable[tuple[str, dict]]:
    """Yield (session_filename, data) for every non-summary BA session record."""
    if not root.exists():
        return
    for session_json in sorted(root.glob("*.json")):
        if session_json.name.endswith(".summary.json"):
            continue
        try:
            data = json.loads(session_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            yield session_json.name, data


def iter_all_native_candidates() -> Iterable[NativeCandidate]:
    """Filesystem-first discovery of EVERY native transcript across ALL
    providers, ignoring the Better-Agent session index.

    The BA-indexed miners only see transcripts linked from a BA session record,
    which misses the bulk of raw native sessions — direct CLI usage and
    extension-spawned workers leave no BA record. This walker enumerates every
    provider's native store directly so the raw-grep search covers the whole
    corpus:

    - Claude (every config): all ``<config_dir>/projects/<encoded-cwd>/*.jsonl``
      — covers ``~/.claude``, ``~/.claude-zai``, and any provider ``config_dir``.
    - Codex native: ``~/.codex/sessions/**/*.jsonl`` rollout files.
    - Gemini native: ``~/.gemini/tmp/<cwd>/chats/session-*.jsonl``.
    - BA run-dirs: ``<runs>/<run_id>/session_events.jsonl`` (codex/gemini/ba-runner
      streams BA captured; Claude-shaped, so parsed as claude).
    """
    # Claude — every provider config root.
    for projects_root in _claude_projects_roots():
        if not projects_root.exists():
            continue
        for encoded_dir in projects_root.iterdir():
            if not encoded_dir.is_dir():
                continue
            decoded_cwd = _decode_cwd_token(encoded_dir.name)
            for transcript in encoded_dir.glob("*.jsonl"):
                yield NativeCandidate(
                    key=f"claude-fs:{projects_root.name}/{encoded_dir.name}/{transcript.stem}",
                    sid=transcript.stem,
                    cwd=decoded_cwd,
                    data={},
                    transcript=transcript,
                    mtime=_mtime(transcript),
                    format="claude",
                )

    # Codex native rollout store.
    codex_root = _codex_sessions_root()
    if codex_root.exists():
        for transcript in codex_root.rglob("*.jsonl"):
            yield NativeCandidate(
                key=f"codex-fs:{transcript.name}",
                sid=transcript.stem,
                cwd=_codex_first_cwd(transcript),
                data={},
                transcript=transcript,
                mtime=_mtime(transcript),
                format="codex",
            )

    # Gemini native chat store.
    gemini_root = _gemini_chats_root()
    if gemini_root.exists():
        for transcript in gemini_root.rglob("chats/session-*.jsonl"):
            cwd_dir = transcript.parent.parent
            yield NativeCandidate(
                key=f"gemini-fs:{cwd_dir.name}/{transcript.name}",
                sid=transcript.stem,
                cwd=_decode_cwd_token(cwd_dir.name),
                data={},
                transcript=transcript,
                mtime=_mtime(transcript),
                format="gemini",
            )

    for root in _windsurf_cascade_roots():
        source = "windsurf" if root.parent.name == "windsurf" else "codeium"
        for transcript in root.glob("*.pb"):
            yield NativeCandidate(
                key=f"windsurf-fs:{source}/{transcript.stem}",
                sid=transcript.stem,
                cwd="",
                data={},
                transcript=transcript,
                mtime=_mtime(transcript),
                format="windsurf",
            )

    # BA run-dirs (codex/gemini/ba-runner) — every run, regardless of BA linkage.
    for state_path in _runs_root().glob("*/state.json"):
        run_dir = state_path.parent
        transcript = run_dir / "session_events.jsonl"
        if not transcript.exists():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        sid = state.get("app_session_id") if isinstance(state.get("app_session_id"), str) else run_dir.name
        yield NativeCandidate(
            key=f"run-fs:{run_dir.name}",
            sid=sid,
            cwd=_ba_session_cwd(sid),
            data={},
            transcript=transcript,
            mtime=_mtime(transcript),
            format="claude",
        )


class _NativeMinerBase(SessionMinerBase):
    """Shared discovery + parsing for native miners.

    Subclasses set :attr:`_kind` (the provider kind to mine) and
    :attr:`_key_prefix` (the watermark namespace). :meth:`_resolve_transcript`
    resolves the native transcript path for a BA session record (or None).
    """

    _kind: str = ""
    _key_prefix: str = ""

    def _resolve_transcript(self, data: dict, sid: str) -> Path | None:
        raise NotImplementedError

    def iter_candidates(self) -> Iterable["NativeCandidate"]:
        """Cheap discovery: yield one :class:`NativeCandidate` per transcript
        to parse, WITHOUT reading/parsing it. Splitting discovery from parsing
        lets a caller fan the (expensive) parse out across a thread pool."""
        for session_name, data in _iter_ba_session_records(self._root):
            if _provider_kind(data) != self._kind:
                continue
            sid = Path(session_name).stem
            transcript = self._resolve_transcript(data, sid)
            if transcript is None or not transcript.exists():
                continue
            source_mtime = max(_mtime(self._root / session_name), _mtime(transcript))
            yield NativeCandidate(
                key=f"{self._key_prefix}{session_name}",
                sid=sid,
                cwd=data.get("cwd") if isinstance(data.get("cwd"), str) else "",
                data=data,
                transcript=transcript,
                mtime=source_mtime,
            )

    def _iter_sources(self) -> Iterable[tuple[str, SessionVisit, float]]:
        for candidate in self.iter_candidates():
            if not self.source_changed(candidate.key, candidate.mtime):
                self.scanned_count += 1
                continue
            visit = candidate.parse()
            if visit is None:
                continue
            yield candidate.key, visit, candidate.mtime


class NativeClaudeSessionMiner(_NativeMinerBase):
    """Claude native transcript source (``projects/<cwd>/<sid>.jsonl``)."""

    _kind = "claude"
    _key_prefix = "claude:"

    def _resolve_transcript(self, data: dict, sid: str) -> Path | None:
        agent_session_id = data.get("agent_session_id")
        cwd = data.get("cwd")
        if not isinstance(agent_session_id, str) or not agent_session_id:
            return None
        if not isinstance(cwd, str) or not cwd:
            return None
        return claude_projects_root_for_session(data) / encode_cwd(cwd) / f"{agent_session_id}.jsonl"


class _RunDirNativeMiner(_NativeMinerBase):
    """Codex/Gemini native source: run-dir ``session_events.jsonl``.

    Both providers write Claude-shaped events captured by their runners into a
    per-run dir; the run dir is resolved from the BA app session id (the session
    record's filename stem) via the run-dir index (``state.json.app_session_id``).
    """

    def _resolve_transcript(self, data: dict, sid: str) -> Path | None:
        run_dir = _run_index().get(sid)
        if run_dir is None:
            return None
        return run_dir / "session_events.jsonl"


class NativeCodexSessionMiner(_RunDirNativeMiner):
    _kind = "codex"
    _key_prefix = "codex:"


class NativeGeminiSessionMiner(_RunDirNativeMiner):
    _kind = "gemini"
    _key_prefix = "gemini:"


class NativeBetterAgentSessionMiner(_RunDirNativeMiner):
    """Better Agent's own runner source (``runner_better_agent``).

    The ``openai`` provider kind runs through ``runner_better_agent``, which —
    like Codex/Gemini — writes Claude-shaped ``session_events.jsonl`` into a
    per-run dir. Same run-dir mechanism, distinct kind/key so it is mined as its
    own source alongside the provider-native CLIs.
    """

    _kind = "openai"
    _key_prefix = "ba:"
