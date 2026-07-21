"""Provisioned-session lifecycle: dirty-check + ensure base/caller sessions.

`ensure_session` finds a clean primed base on the `working_mode` registry
(or creates + registers one). `dirty_reason` decides whether an existing
base is still safe to fork from. Generalized from the requirement-analysis
worker plumbing.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from provisioning.config import ProvisionedConfig
from provisioning.spec import DirtyPolicy, ProvisionedSessionSpec


def dirty_reason(
    session: dict, policy: DirtyPolicy, cwd: str
) -> str:
    """Non-empty string ⇒ the base transcript is polluted (a real query leaked
    in, or it bloated) and must be re-minted. Empty ⇒ clean.

    KNOWN LIMITATION: reads the base jsonl from the PRIMARY's local
    filesystem. A base whose `node_id` is a remote node stores its
    transcript on that node, not here, so this returns "" (can't read
    it) — i.e. a remote base is treated as clean. Fork-mode bases aren't
    polluted by forks (each fork is a copy), so this is safe in steady
    state; undetected direct-write bloat on a remote base is a follow-up
    that would fetch the transcript via the node RPC."""
    agent_sid = str(session.get("agent_session_id") or "").strip()
    if not agent_sid:
        if session.get("messages"):
            return "base has messages but no provider session id"
        return ""  # not yet provisioned
    try:
        from orchs.jsonl_helpers import compute_jsonl_path
    except Exception:
        return ""
    path = compute_jsonl_path(cwd, agent_sid)
    if path is None or not path.exists():
        return ""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size > policy.max_base_bytes:
        return f"base jsonl is {size} bytes"

    user_turns = 0
    assistant_turns = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                typ = row.get("type")
                if typ == "user":
                    user_turns += 1
                    content = str((row.get("message") or {}).get("content") or "")
                    for marker in policy.leak_markers:
                        if marker and marker in content:
                            return f"base jsonl contains a leaked query ({marker!r})"
                elif typ == "assistant":
                    assistant_turns += 1
                    if row.get("isApiErrorMessage"):
                        return "base jsonl contains an API error assistant turn"
                user_limit_exceeded = (
                    policy.max_user_turns is not None
                    and user_turns > policy.max_user_turns
                )
                assistant_limit_exceeded = (
                    policy.max_assistant_turns is not None
                    and assistant_turns > policy.max_assistant_turns
                )
                if user_limit_exceeded or assistant_limit_exceeded:
                    return (
                        f"base jsonl has {user_turns} user turns and "
                        f"{assistant_turns} assistant turns"
                    )
    except OSError:
        return ""
    return ""


def expired_reason(session: dict, spec: ProvisionedSessionSpec) -> str:
    """Non-empty string ⇒ the base has outlived its `lifetime_seconds` and
    must be recycled (deleted + re-provisioned). Empty ⇒ still fresh, or no
    lifetime configured. A base without a `provisioned_at` stamp predates
    lifetime tracking and is treated as expired so it gets stamped on the
    next re-provision."""
    lifetime = spec.lifetime_seconds
    if not lifetime or lifetime <= 0:
        return ""
    meta = session.get("working_mode_meta") or {}
    provisioned_at = meta.get("provisioned_at")
    if not provisioned_at:
        return "base has no provisioned_at timestamp"
    age = time.time() - float(provisioned_at)
    if age > lifetime:
        return f"base is {age:.0f}s old (lifetime {lifetime:.0f}s)"
    return ""


def _storage_scope_matches(session: dict, spec: ProvisionedSessionSpec) -> bool:
    return (session.get("storage_scope") or None) == (spec.storage_scope or None)


def ensure_session(spec: ProvisionedSessionSpec, cfg: ProvisionedConfig) -> str:
    """Return a clean provisioned base bc-session id for `spec`."""
    pinned = cfg.provisioned_session_id
    if pinned:
        session = _session(pinned)
        if session is None:
            raise RuntimeError(f"provisioned session not found: {pinned}")
        _validate_provider(session, cfg)
        if not _storage_scope_matches(session, spec):
            raise RuntimeError(f"{spec.env_prefix} pinned session storage scope mismatch")
        reason = dirty_reason(session, spec.dirty_policy, cfg.cwd) or expired_reason(session, spec)
        if reason:
            raise RuntimeError(f"{spec.env_prefix} pinned session is not clean: {reason}")
        _upsert_worker(cfg.cwd, session)
        return pinned

    existing = _find(spec, cfg)
    if existing and existing.get("id"):
        _validate_provider(existing, cfg)
        reason = (
            "" if _storage_scope_matches(existing, spec) else "storage scope mismatch"
        ) or dirty_reason(existing, spec.dirty_policy, cfg.cwd) or expired_reason(existing, spec)
        if not reason:
            _upsert_worker(cfg.cwd, existing)
            return str(existing["id"])
        # Discard the stale base (polluted or expired) and mint a fresh one.
        _delete_session(str(existing["id"]))

    return _create_session(spec, cfg)


def ensure_caller(spec: ProvisionedSessionSpec, cfg: ProvisionedConfig) -> str:
    """Return a stable caller bc-session id (the fork's app_session_id)."""
    if cfg.caller_session_id:
        return cfg.caller_session_id
    try:
        import working_mode
        from session_manager import manager as session_manager
    except Exception as exc:
        raise RuntimeError("provisioning cannot create caller session") from exc
    existing = working_mode.find_working_session(
        spec.caller_key, cwd=cfg.cwd, provider_id=cfg.provider_id,
        model=cfg.model, runner=cfg.runner, node_id=cfg.node_id,
    )
    if existing and existing.get("id"):
        if _storage_scope_matches(existing, spec):
            return str(existing["id"])
        session_manager.delete(str(existing["id"]))
    sess = session_manager.create(
        name=spec.caller_name,
        orchestration_mode=spec.orchestration_mode,
        cwd=cfg.cwd,
        model=cfg.model,
        source="internal",
        provider_id=cfg.provider_id,
        runner=cfg.runner or None,
        reasoning_effort=cfg.reasoning_effort or None,
        node_id=cfg.node_id,
        worker_creation_policy=spec.worker_creation_policy,
        bare_config=spec.bare_config,
        storage_scope=spec.storage_scope,
    )
    working_mode.mark_working_mode(
        sess["id"],
        mode=spec.caller_key,
        meta={"cwd": cfg.cwd, "provider_id": cfg.provider_id, "model": cfg.model,
              "runner": cfg.runner,
              "node_id": cfg.node_id},
    )
    return str(sess["id"])


# ── helpers ───────────────────────────────────────────────────────────

def _session(session_id: str) -> Optional[dict]:
    try:
        from session_manager import manager as session_manager
    except Exception:
        return None
    return session_manager.get(session_id)


def _delete_session(session_id: str) -> None:
    """Drop a stale base. Cascades worker_store cleanup via the session
    delete bus subscriber (`event_bus_subscribers`)."""
    try:
        from session_manager import manager as session_manager
    except Exception:
        return
    session_manager.delete(session_id)


def _find(spec: ProvisionedSessionSpec, cfg: ProvisionedConfig) -> Optional[dict]:
    try:
        import working_mode
    except Exception:
        return None
    existing = working_mode.find_working_session(
        spec.key,
        cwd=cfg.cwd,
        provider_id=cfg.provider_id,
        model=cfg.model,
        runner=cfg.runner,
        machine_completion=spec.machine_completion,
        version=spec.version,
        node_id=cfg.node_id,
    )
    return existing if (existing and existing.get("id")) else None


def _validate_provider(session: dict, cfg: ProvisionedConfig) -> None:
    if (
        session.get("provider_id") != cfg.provider_id
        or session.get("model") != cfg.model
        or session.get("runner") != cfg.runner
    ):
        raise RuntimeError(
            f"{cfg.worker_description}: runtime profile mismatch "
            f"(session={session.get('id')} provider={session.get('provider_id')} "
            f"model={session.get('model')} runner={session.get('runner')}; "
            f"required provider={cfg.provider_id} model={cfg.model} runner={cfg.runner})"
        )
    # Routing is keyed off the session's node_id at dispatch time, so a
    # mismatch here would silently run on the wrong node — reject it.
    if (session.get("node_id") or "primary") != cfg.node_id:
        raise RuntimeError(
            f"{cfg.worker_description}: node_id mismatch "
            f"(session={session.get('id')} node={session.get('node_id') or 'primary'}; "
            f"required node={cfg.node_id})"
        )


def _create_session(spec: ProvisionedSessionSpec, cfg: ProvisionedConfig) -> str:
    try:
        import working_mode
        from session_manager import manager as session_manager
    except Exception as exc:
        raise RuntimeError(f"provisioning cannot create {spec.key} session") from exc
    sess = session_manager.create(
        name=spec.name,
        orchestration_mode=spec.orchestration_mode,
        cwd=cfg.cwd,
        model=cfg.model,
        source="internal",
        provider_id=cfg.provider_id,
        runner=cfg.runner or None,
        reasoning_effort=cfg.reasoning_effort or None,
        node_id=cfg.node_id,
        worker_creation_policy=spec.worker_creation_policy,
        bare_config=spec.bare_config,
        storage_scope=spec.storage_scope,
    )
    working_mode.mark_working_mode(
        sess["id"],
        mode=spec.key,
        meta={
            "cwd": cfg.cwd,
            "provider_id": cfg.provider_id,
            "model": cfg.model,
            "runner": cfg.runner,
            "machine_completion": spec.machine_completion,
            "version": spec.version,
            "node_id": cfg.node_id,
            "provisioned_at": time.time(),
        },
    )
    _upsert_worker(cfg.cwd, sess)
    return str(sess["id"])


def _upsert_worker(cwd: str, session: dict) -> None:
    try:
        from stores import worker_store
    except Exception:
        return
    worker_store.upsert_worker(
        cwd=cwd,
        agent_session_id=str(session["id"]),
        orchestration_mode=session.get("orchestration_mode") or "native",
        agent_sid=session.get("agent_session_id"),
        node_id=session.get("node_id") or "primary",
    )
