"""Schema-versioned migration registry for `paths.scheme_home()` components.

State that lives under `paths.scheme_home(component, version)` never
mutates in place across a schema change — migrating a component to
`version=N+1` copies forward into a fresh `v<N+1>/` directory and leaves
`v<N>/` untouched, so an older binary reading the same BA home
concurrently keeps seeing its own (unmodified) version directory. This is
the project's schema-evolution rule for durable stores applied generically:
explicit contiguous `N -> N+1` migrations, never an in-place rewrite.

`register` records a single edge `from_version -> to_version` for a
component. `ensure` walks the chain from whatever version already exists
on disk up to `target_version`, applying each registered migration
function in order. The full chain is validated BEFORE any migration
function runs, so `ensure` never leaves a component half-migrated: a
missing edge anywhere in the chain fails closed.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

import paths

MigrationFn = Callable[[Path, Path], None]

_MARKER_NAME = "migrated_from"

# Reentrant: a migration function is allowed to call back into `ensure`
# (e.g. for a dependent component) from the same thread without deadlocking.
_LOCK = threading.RLock()
_REGISTRY: dict[tuple[str, int], tuple[int, MigrationFn]] = {}


def register(
    component: str, from_version: int, to_version: int,
) -> Callable[[MigrationFn], MigrationFn]:
    """Decorator: register a migration `from_version -> to_version` for
    `component`. Raises if that (component, from_version) edge is already
    registered — edges are unambiguous, one per source version."""
    if to_version <= from_version:
        raise ValueError(
            f"scheme_migrations.register: to_version ({to_version}) must be "
            f"greater than from_version ({from_version}) for {component!r}",
        )

    def decorator(fn: MigrationFn) -> MigrationFn:
        key = (component, from_version)
        with _LOCK:
            if key in _REGISTRY:
                raise ValueError(
                    f"scheme_migrations: duplicate edge registered for "
                    f"{component!r} v{from_version}",
                )
            _REGISTRY[key] = (to_version, fn)
        return fn

    return decorator


def registered_edges(component: str) -> dict[int, int]:
    """`{from_version: to_version}` for every edge registered for
    `component`. Read-only introspection for version-bump tests that
    assert the chain from v1 stays contiguous up to a component's current
    schema-version constant."""
    with _LOCK:
        return {
            from_v: to_v
            for (comp, from_v), (to_v, _fn) in _REGISTRY.items()
            if comp == component
        }


def _existing_versions(component: str) -> list[int]:
    root = paths.ba_home() / "scheme" / component
    if not root.is_dir():
        return []
    versions = []
    for entry in root.iterdir():
        if entry.is_dir() and entry.name.startswith("v") and entry.name[1:].isdigit():
            versions.append(int(entry.name[1:]))
    return sorted(versions)


def _resolve_chain(
    component: str, start: int, target: int,
) -> list[tuple[int, MigrationFn]]:
    """Contiguous edges `start -> target`, or raise if any hop is missing.
    Called with `_LOCK` already held."""
    chain: list[tuple[int, MigrationFn]] = []
    cur = start
    while cur < target:
        edge = _REGISTRY.get((component, cur))
        if edge is None:
            raise RuntimeError(
                f"scheme_migrations.ensure: no registered migration edge "
                f"{component!r} v{cur} -> v{cur + 1} (target v{target})",
            )
        to_version, fn = edge
        if to_version > target:
            raise RuntimeError(
                f"scheme_migrations.ensure: registered edge {component!r} "
                f"v{cur} -> v{to_version} overshoots target v{target}",
            )
        chain.append((to_version, fn))
        cur = to_version
    return chain


def _write_marker(dst_dir: Path, from_version: int) -> None:
    marker = dst_dir / _MARKER_NAME
    marker.write_text(str(from_version), encoding="utf-8")
    paths.make_private_file(marker)


def ensure(component: str, target_version: int) -> Path:
    """Resolve `component`'s state dir at `target_version`, migrating
    forward from whatever version already exists on disk.

    - No prior version dirs: creates `v<target_version>/` fresh, empty.
    - Newest existing version == target: returns it untouched.
    - Newest existing version < target: validates the full contiguous
      chain of registered edges first, then applies each edge's migration
      function stepwise, copy-forward into a new `v<N+1>/` dir per step.
      Older version dirs are never mutated or deleted.
    - Any missing edge in the chain: raises before touching disk.
    - Newest existing version > target: raises (no downgrade path).
    """
    with _LOCK:
        existing = _existing_versions(component)
        if not existing:
            return paths.scheme_home(component, target_version)
        newest = existing[-1]
        if newest > target_version:
            raise RuntimeError(
                f"scheme_migrations.ensure: {component!r} has a newer "
                f"on-disk version (v{newest}) than requested target "
                f"(v{target_version}); refusing to downgrade",
            )
        if newest == target_version:
            return paths.scheme_home(component, target_version)
        chain = _resolve_chain(component, newest, target_version)
        cur_dir = paths.scheme_home(component, newest)
        cur_version = newest
        for to_version, fn in chain:
            dst_dir = paths.scheme_home(component, to_version)
            fn(cur_dir, dst_dir)
            _write_marker(dst_dir, cur_version)
            cur_dir = dst_dir
            cur_version = to_version
        return cur_dir
