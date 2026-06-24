"""Persistent state for the Rearranger feature.

Holds a single piece of state — the global bootstrap session id — in
`~/.better-claude/rearranger_state.json`. Created lazily the first time
any better-agent session enables the rearranger feature.

The bootstrap is a shared Claude CLI session that carries only the
Rearranger system prompt (see rearranger_prompt.BOOTSTRAP_PROMPT). Each
better-agent session forks off this bootstrap exactly once to create
its own per-session rearranger; the per-session sid then lives on the
session record itself (not here). Keeping this state tiny: one field.
"""

from pathlib import Path
from typing import Callable, Optional

from json_store import read_json, write_json
from paths import ba_home


def _state_path() -> Path:
    """Resolve the rearranger-state file path lazily. Per A12's
    "single ba_home() helper per store" convention (and CLAUDE.md's
    BETTER_CLAUDE_HOME isolation rule) — never cache at module-load
    time, because tests override `BETTER_CLAUDE_HOME` mid-process
    and a cached path would point at the developer's real
    `~/.better-claude/rearranger_state.json`. Single helper here
    means every read/write below funnels through one place that
    re-reads `ba_home()` on every call."""
    return ba_home() / "rearranger_state.json"


def _get_str(key: str) -> Optional[str]:
    """Load the state file and return data[key] iff it is a non-empty str."""
    val = read_json(_state_path(), {}).get(key)
    return val if isinstance(val, str) and val else None


def _mutate(fn: Callable[[dict], Optional[bool]]) -> None:
    """Read the state file, hand the dict to `fn` for in-place
    mutation, write back. INVARIANT: skips the write iff `fn`
    explicitly returns `False` (None / True / any truthy → write).
    Lets the no-op-write optimization in `clear_bootstrap_session_id`
    survive the dedup.
    """
    p = _state_path()
    data = read_json(p, {})
    if fn(data) is not False:
        write_json(p, data)


def get_bootstrap_session_id() -> Optional[str]:
    """Return the persisted bootstrap sid, or None if never bootstrapped."""
    return _get_str("bootstrap_session_id")


def set_bootstrap_session_id(session_id: str, provider_id: Optional[str] = None) -> None:
    """Persist the bootstrap sid + the provider that minted it.

    `provider_id` pins the bootstrap to the provider whose
    `CLAUDE_CONFIG_DIR` actually contains the bootstrap's claude jsonl.
    Without this, switching the active provider would orphan the
    bootstrap (claude CLI looks under the new provider's config dir
    and finds nothing). All subsequent rearranger one-shots resume
    from this sid, so they must run under the same provider.
    """
    def _set(data: dict) -> None:
        data["bootstrap_session_id"] = session_id
        if provider_id is not None:
            data["provider_id"] = provider_id
    _mutate(_set)


def get_bootstrap_provider_id() -> Optional[str]:
    """Return the provider_id pinned at bootstrap-mint time, or None
    for legacy state files that predate this field."""
    return _get_str("provider_id")


def clear_bootstrap_session_id() -> None:
    """Forget the bootstrap sid (e.g. if it was rejected by the CLI).

    The next call to `get_bootstrap_session_id` returns None, forcing a
    fresh bootstrap on next use. Also clears the pinned provider so
    the next bootstrap re-pins to whatever's active then.
    """
    def _clear(data: dict) -> bool:
        changed = False
        if "bootstrap_session_id" in data:
            data.pop("bootstrap_session_id")
            changed = True
        if "provider_id" in data:
            data.pop("provider_id")
            changed = True
        return changed
    _mutate(_clear)
