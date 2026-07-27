from __future__ import annotations

import shutil
from pathlib import Path

import installation_profile

# Providers with a native agent-definition surface. Requests for any other
# provider are a graceful no-op (gemini/agy have no user-defined subagent file).
SUPPORTED_PROVIDERS = frozenset({"claude", "codex"})


def _discover_agents() -> list[dict[str, str]]:
    try:
        import extension_store
    except Exception:
        return []
    try:
        return extension_store.runtime_agent_entries()
    except Exception:
        return []


def has_runtime_agents(provider: str, *, bare_config: bool = False) -> bool:
    if bare_config or not installation_profile.integrations_enabled():
        return False
    if provider not in SUPPORTED_PROVIDERS:
        return False
    return any(provider in entry for entry in _discover_agents())


def materialize_runtime_agents(
    root: Path,
    provider: str,
    *,
    bare_config: bool = False,
) -> int:
    """Write the provider's native agent-definition files from active
    extensions into `root`. Returns the number of files written.

    No-op for providers without a native subagent surface, so callers can
    invoke this unconditionally per provider and keep cross-provider parity
    without branching at the call site."""
    if bare_config or not installation_profile.integrations_enabled():
        return 0
    if provider not in SUPPORTED_PROVIDERS:
        return 0

    count = 0
    root.mkdir(parents=True, exist_ok=True)
    for entry in _discover_agents():
        source = entry.get(provider)
        if not source:
            continue
        source_path = Path(source)
        if not source_path.is_file():
            continue
        target = root / source_path.name
        # Per-run fresh dir: overwrite so the file always reflects the
        # current extension content. Unlink any existing target first — a
        # prior symlink must not be written through into the real home.
        if target.is_symlink() or target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.copyfile(source_path, target)
        count += 1
    return count
