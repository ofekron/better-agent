"""Declarative specs for provisioned sessions.

A `ProvisionedSessionSpec` describes one *kind* of provisioned session: how
to identify it, how to shape the session, how to prime it (the one-time
provision prompt that bakes in its methodology), how to query it (the
per-fork instructions), and how to parse its reply. The framework
(`provisioning.manager`) owns the lifecycle — find/create a clean primed
base, then dispatch the real query through a fork. Specs use fresh temporary
forks unless they opt into per-caller fork reuse.

Subclass `ProvisionedSessionSpec` per consumer and register an instance with
`provisioning.register(spec)`. Identity fields (`key`, `version`, `name`,
`env_prefix`) are set as class attributes on the subclass; everything else
has a framework default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from provisioning.config import ProvisionedConfig


@dataclass
class DirtyPolicy:
    """Rule for deciding whether a provisioned *base* is still safe to fork
    from. Inspected against the base's provider transcript (jsonl) before
    each fork. A non-empty reason string ⇒ the base is polluted and must be
    discarded + re-minted."""

    max_base_bytes: int = 256_000
    max_user_turns: int | None = 1
    max_assistant_turns: int | None = 1
    leak_markers: tuple[str, ...] = field(default_factory=tuple)


class ProvisionedSessionSpec:
    """Base class. Override the identity attributes + the build/parse hooks
    per consumer; keep the rest unless you need to change session shape or
    lifecycle."""

    # ── Identity (set per subclass) ──────────────────────────────────
    key: str = ""              # working_mode mode tag; also the registry id
    version: int = 1           # bump to invalidate stale bases
    name: str = ""             # Better Agent session display name
    env_prefix: str = ""       # config env-overlay prefix, e.g. REQ_ANALYSIS
    task_key: str = ""         # app-settings internal-LLM task key (config_store.resolve_internal_llm)

    # ── Session shape ────────────────────────────────────────────────
    orchestration_mode: str = "native"
    bare_config: bool = True            # False ⇒ load skills/CLAUDE.md (e.g. tool-using sessions)
    worker_creation_policy: str = "deny"
    machine_completion: bool = True     # True ⇒ raw-instructions prompt (no tools expected)

    # ── Lifecycle ────────────────────────────────────────────────────
    run_mode: str = "fork"              # "fork" | "direct"
    ephemeral_forks: bool = True        # True ⇒ fresh temporary fork per call
    dispatch: str = "http"              # "http" | "in_process"
    on_no_fork: str = "error"           # "error" | "fallback_native"
    default_model: str = ""
    default_cwd: str = ""               # base cwd (e.g. repo root to load a skill); "" ⇒ project cwd
    node_id: str = "primary"            # target node; "primary" runs locally, else routes via RemoteProviderProxy
    dirty_policy: DirtyPolicy = DirtyPolicy()
    lifetime_seconds: float | None = None   # None ⇒ never recycle by age; else recycle the base after this many seconds since provisioning

    # ── Timing ───────────────────────────────────────────────────────
    # provision_timeout budgets the lifecycle phase (base/caller ensure,
    # lifecycle locks, warm-up). dispatch_timeout budgets one dispatch
    # attempt; None ⇒ same as provision_timeout.
    provision_timeout: float = 24.0 * 60.0 * 60.0
    dispatch_timeout: float | None = None
    retry_attempts: int = 3
    retry_backoff: tuple[float, ...] = (2.0, 8.0)

    @property
    def effective_dispatch_timeout(self) -> float:
        return float(self.dispatch_timeout if self.dispatch_timeout is not None else self.provision_timeout)

    # ── Hooks (override) ─────────────────────────────────────────────
    def build_provision_prompt(self, ctx: dict) -> str:
        """The one-time priming turn. Bakes in the session's methodology so
        each fork only needs the real query. End with a 'ready' contract."""
        raise NotImplementedError

    def build_instructions(self, query: str, ctx: dict) -> str:
        """The per-fork payload — normally just the query, since the
        methodology lives in the provision prompt."""
        return query

    def parse_result(self, text: str, ctx: dict) -> Any:
        """Extract the consumer's result from the fork's assistant text."""
        return text

    def build_config(self, *, model: str | None = None) -> "ProvisionedConfig | None":
        """Override to supply a fully-resolved `ProvisionedConfig` when a
        consumer needs resolution semantics the default app-settings
        resolver doesn't provide (e.g. conservative provider resolution +
        model→provider matching). Return None to use the default resolver."""
        return None

    # ── Derived caller identity ──────────────────────────────────────
    @property
    def caller_name(self) -> str:
        return f"{self.name} Caller"

    @property
    def caller_key(self) -> str:
        return f"{self.key}:caller"


_REGISTRY: dict[str, ProvisionedSessionSpec] = {}


def register(spec: ProvisionedSessionSpec) -> ProvisionedSessionSpec:
    """Register a spec instance under its `key`. Idempotent."""
    if not spec.key:
        raise ValueError("ProvisionedSessionSpec.key must be set")
    _REGISTRY[spec.key] = spec
    return spec


def get(key: str) -> ProvisionedSessionSpec:
    if key not in _REGISTRY:
        raise KeyError(f"no provisioned-session spec registered for {key!r}")
    return _REGISTRY[key]


def all_specs() -> list[ProvisionedSessionSpec]:
    return list(_REGISTRY.values())
