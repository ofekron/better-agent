from __future__ import annotations

from typing import Optional

# Native CLI permission vocabularies (verified from each CLI's --help, not
# guessed). Permission is per-provider-native (Option B): each provider's real
# options are exposed losslessly, so the shape is kind-specific.
#
#   claude / gemini → {"mode": <value>}        (single axis)
#   codex           → {"approval": ..., "sandbox": ...}  (two independent axes)

CLAUDE_PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto")
CODEX_APPROVAL_POLICIES = ("untrusted", "on-request", "on-failure", "never")
CODEX_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
GEMINI_APPROVAL_MODES = ("auto_edit", "yolo", "plan")

# Per-kind axis → allowed values. Order is the UI display order.
_AXES: dict[str, dict[str, tuple[str, ...]]] = {
    "claude": {"mode": CLAUDE_PERMISSION_MODES},
    "codex": {"approval": CODEX_APPROVAL_POLICIES, "sandbox": CODEX_SANDBOX_MODES},
    "gemini": {"mode": GEMINI_APPROVAL_MODES},
}

# Defaults preserve prior hardcoded behavior: full bypass on every provider.
# Existing flows are unchanged unless the user lowers this in Settings.
DEFAULT_PERMISSION: dict[str, dict[str, str]] = {
    "claude": {"mode": "bypassPermissions"},
    "codex": {"approval": "never", "sandbox": "danger-full-access"},
    "gemini": {"mode": "yolo"},
}


def permission_axes_for_kind(kind: str) -> dict[str, tuple[str, ...]]:
    """Axis → allowed-values map for a provider kind. Empty for unknown kinds
    (no permission surfaced)."""
    return dict(_AXES.get(kind, {}))


def default_permission_for_kind(kind: str) -> dict[str, str]:
    """The kind's default permission dict (a copy). Empty for unknown kinds."""
    return dict(DEFAULT_PERMISSION.get(kind, {}))


def normalize_permission(kind: str, value: object) -> Optional[dict[str, str]]:
    """Validate a permission value against the kind's axes.

    None / "" → None (meaning "inherit the default"). A dict is per-axis
    validated: known axis values are kept, missing/unknown ones fall back to
    the kind default for that axis. Returns None for non-dict input or
    unknown kinds."""
    axes = _AXES.get(kind)
    if not axes:
        return None
    if value is None or value == "" or value == {}:
        return None
    if not isinstance(value, dict):
        return None
    kind_default = DEFAULT_PERMISSION[kind]
    out: dict[str, str] = {}
    for axis, allowed in axes.items():
        raw = value.get(axis)
        out[axis] = raw if isinstance(raw, str) and raw in allowed else kind_default[axis]
    return out


def clean_default_permission(kind: str, value: object) -> dict[str, str]:
    """Persist on the provider record: never empty — always falls back to the
    kind default so every provider carries a valid permission."""
    norm = normalize_permission(kind, value)
    return norm if norm is not None else default_permission_for_kind(kind)


def resolve_permission(
    kind: str, session_value: object, provider_default: object
) -> dict[str, str]:
    """Effective permission for a run: session override → provider default →
    kind default. Always returns a full kind-shaped dict for known kinds."""
    norm = normalize_permission(kind, session_value)
    if norm is not None:
        return norm
    norm = normalize_permission(kind, provider_default)
    if norm is not None:
        return norm
    return default_permission_for_kind(kind)


def resolve_for_run(
    *,
    sess_rec: object,
    worker_sess_rec: object,
    is_worker: bool,
    fallback_kind: str = "",
) -> dict[str, str]:
    """Effective permission for a runner spawn. The owning session is the
    worker session for worker turns (its permission is inherited from the
    parent at create time), else the app session. Falls back through the
    session override → provider-record default → kind default."""
    owner = (
        (worker_sess_rec or sess_rec)
        if (is_worker and worker_sess_rec)
        else sess_rec
    )
    pid = (owner or {}).get("provider_id") if isinstance(owner, dict) else None
    record = None
    if pid:
        try:
            import config_store

            record = config_store.get_provider(pid)
        except Exception:
            record = None
    kind = (record or {}).get("kind") or fallback_kind
    return resolve_permission(
        kind,
        (owner or {}).get("permission") if isinstance(owner, dict) else None,
        (record or {}).get("default_permission"),
    )
