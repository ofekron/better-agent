"""Generic provisioned-session framework.

A *provisioned session* is a base Better Agent session primed once with a **provision
prompt** (its methodology baked in), then queried through forks. Specs use
fresh temporary forks by default; consumers that need follow-up continuity can
reuse the same per-caller fork. Consumers declare a `ProvisionedSessionSpec`
and call `provisioning.run(spec, query, ctx)`.

Builds on the existing `working_mode` registry, `worker_store`, and the fork
engine (`coordinator.run_delegation` / `/api/internal/ask-fork`).
"""
from provisioning.config import ProvisionedConfig, resolve_config
from provisioning.dispatch import extract_fork_text
from provisioning.lifecycle import dirty_reason, ensure_caller, ensure_session, expired_reason
from provisioning.manager import ProvisionedResult, ensure_warm_base, run, run_sync
from provisioning.spec import (
    DirtyPolicy,
    ProvisionedSessionSpec,
    all_specs,
    get,
    register,
)

__all__ = [
    "DirtyPolicy",
    "ProvisionedConfig",
    "ProvisionedResult",
    "ProvisionedSessionSpec",
    "all_specs",
    "dirty_reason",
    "ensure_caller",
    "ensure_session",
    "ensure_warm_base",
    "expired_reason",
    "extract_fork_text",
    "get",
    "register",
    "resolve_config",
    "run",
    "run_sync",
]
