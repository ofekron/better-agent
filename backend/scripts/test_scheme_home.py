#!/usr/bin/env python3
"""Unit coverage for backend/paths.py:scheme_home, backend/scheme_migrations.py,
and the optional persisted-incarnation path in backend/adapters/projection.py.

Isolated via `_test_home` before any backend import (no real
`~/.better-claude` touched). Each test that mutates on-disk scheme state
acquires its own `TestHome` so registry/migration side effects in one test
never leak into another via a shared BA home; `scheme_migrations`'s
in-process registry is itself keyed by component name, so each test also
uses a component name unique to itself.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_scheme_home.py -q
    PYTHONPATH=. python3 backend/scripts/test_scheme_home.py   # __main__ fallback
"""

from __future__ import annotations

import sys
from pathlib import Path

import _test_home
_test_home.isolate("ba-test-scheme-home-")

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
_REPO_ROOT = _BACKEND.parent
for _p in (str(_BACKEND), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402
import scheme_migrations  # noqa: E402
from backend.adapters.projection import CURRENT_SCHEME_VERSION, SurfaceProjection  # noqa: E402


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------- #
# paths.scheme_home
# --------------------------------------------------------------------------- #
def test_scheme_home_creates_versioned_private_dir() -> None:
    home = _test_home.TestHome.acquire("ba-test-scheme-home-basic-")
    try:
        target = paths.scheme_home("widget", 1)

        check(
            target == paths.ba_home() / "scheme" / "widget" / "v1",
            f"scheme_home resolves under ba_home()/scheme/<component>/v<version>, got {target}",
        )
        check(target.is_dir(), "scheme_home creates the target directory")
        paths.require_private_directory(target)  # raises if not private

        # Idempotent: calling again just re-verifies, doesn't error or recreate.
        again = paths.scheme_home("widget", 1)
        check(again == target, "scheme_home is idempotent across calls")
    finally:
        home.release()


def test_scheme_home_rejects_traversal_component() -> None:
    home = _test_home.TestHome.acquire("ba-test-scheme-home-traversal-")
    try:
        for bad in ("..", ".", "a/b", "a\\b", ""):
            try:
                paths.scheme_home(bad, 1)
            except ValueError:
                continue
            raise AssertionError(f"scheme_home accepted unsafe component {bad!r}")

        try:
            paths.scheme_home("widget", 0)
        except ValueError:
            pass
        else:
            raise AssertionError("scheme_home accepted version < 1")
    finally:
        home.release()


# --------------------------------------------------------------------------- #
# scheme_migrations.ensure
# --------------------------------------------------------------------------- #
_MIGRATE_COMPONENT = "test_widget_migrate"


@scheme_migrations.register(_MIGRATE_COMPONENT, 1, 2)
def _migrate_widget_v1_to_v2(src_dir: Path, dst_dir: Path) -> None:
    for entry in src_dir.iterdir():
        if entry.is_file():
            (dst_dir / entry.name).write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")


def test_ensure_migrates_forward_and_leaves_older_version_intact() -> None:
    home = _test_home.TestHome.acquire("ba-test-scheme-migrate-")
    try:
        v1_dir = paths.scheme_home(_MIGRATE_COMPONENT, 1)
        seed = v1_dir / "seed.txt"
        seed.write_text("hello", encoding="utf-8")
        paths.make_private_file(seed)

        target = scheme_migrations.ensure(_MIGRATE_COMPONENT, 2)

        check(
            target == paths.ba_home() / "scheme" / _MIGRATE_COMPONENT / "v2",
            f"ensure returns the target-version dir, got {target}",
        )
        check((target / "seed.txt").read_text(encoding="utf-8") == "hello",
              "migration fn copied seed.txt forward into v2")
        check((v1_dir / "seed.txt").read_text(encoding="utf-8") == "hello",
              "v1 dir is left untouched after migrating to v2")
        marker = target / "migrated_from"
        check(marker.is_file(), "ensure records a migrated_from marker in the new dir")
        check(marker.read_text(encoding="utf-8").strip() == "1",
              "migrated_from marker records the source version")

        # Re-running ensure at the same target is a no-op that returns v2 again.
        again = scheme_migrations.ensure(_MIGRATE_COMPONENT, 2)
        check(again == target, "ensure at an already-reached target returns it unchanged")
    finally:
        home.release()


_MISSING_EDGE_COMPONENT = "test_widget_missing_edge"


@scheme_migrations.register(_MISSING_EDGE_COMPONENT, 1, 2)
def _migrate_missing_edge_v1_to_v2(src_dir: Path, dst_dir: Path) -> None:
    pass  # no edge registered 2 -> 3; deliberately incomplete for the test below


def test_ensure_raises_on_missing_chain_edge_before_touching_disk() -> None:
    home = _test_home.TestHome.acquire("ba-test-scheme-missing-edge-")
    try:
        paths.scheme_home(_MISSING_EDGE_COMPONENT, 1)

        try:
            scheme_migrations.ensure(_MISSING_EDGE_COMPONENT, 3)
        except RuntimeError:
            pass
        else:
            raise AssertionError("ensure() should raise when the 2->3 edge is unregistered")

        v2_dir = paths.ba_home() / "scheme" / _MISSING_EDGE_COMPONENT / "v2"
        check(not v2_dir.exists(),
              "ensure() validates the full chain before running any migration fn "
              "(fail closed, no partial side effects)")
    finally:
        home.release()


def test_ensure_no_prior_dirs_creates_target_fresh() -> None:
    home = _test_home.TestHome.acquire("ba-test-scheme-fresh-")
    try:
        target = scheme_migrations.ensure("test_widget_fresh", 1)
        check(target == paths.ba_home() / "scheme" / "test_widget_fresh" / "v1",
              "ensure with no prior dirs creates the target version fresh")
        check(target.is_dir(), "fresh target dir exists")
        check(not (target / "migrated_from").exists(),
              "fresh target has no migration marker (nothing migrated)")
    finally:
        home.release()


def test_ensure_refuses_downgrade() -> None:
    home = _test_home.TestHome.acquire("ba-test-scheme-downgrade-")
    try:
        paths.scheme_home("test_widget_downgrade", 2)
        try:
            scheme_migrations.ensure("test_widget_downgrade", 1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("ensure() should refuse to downgrade an existing newer version")
    finally:
        home.release()


# --------------------------------------------------------------------------- #
# Version-bump guard (project rule: "explicit contiguous N -> N+1 migrations
# and a version-bump test that fails when an edge is missing")
# --------------------------------------------------------------------------- #
def test_surface_projection_scheme_version_has_contiguous_chain_from_v1() -> None:
    """Fails the moment someone bumps CURRENT_SCHEME_VERSION in
    backend/adapters/projection.py without registering the matching
    contiguous scheme_migrations edge(s) for the "surface_projection"
    component, starting from v1."""
    edges = scheme_migrations.registered_edges("surface_projection")
    cur = 1
    target = CURRENT_SCHEME_VERSION
    check(target >= 1, "CURRENT_SCHEME_VERSION must be >= 1")
    while cur < target:
        check(
            cur in edges,
            f"missing scheme_migrations.register('surface_projection', {cur}, ...) "
            f"edge: CURRENT_SCHEME_VERSION={target} has no contiguous chain from v1",
        )
        cur = edges[cur]
    check(cur == target, "surface_projection migration chain lands exactly on CURRENT_SCHEME_VERSION")


# --------------------------------------------------------------------------- #
# SurfaceProjection persistence
# --------------------------------------------------------------------------- #
def test_surface_projection_persists_incarnation_across_constructions() -> None:
    home = _test_home.TestHome.acquire("ba-test-scheme-projection-persist-")
    try:
        first = SurfaceProjection(component="surface_projection")
        second = SurfaceProjection(component="surface_projection")

        check(
            first.snapshot().incarnation == second.snapshot().incarnation,
            "SurfaceProjection(component=...) reuses the persisted incarnation "
            "token across constructions in the same BA home",
        )

        incarnation_path = (
            paths.ba_home() / "scheme" / "surface_projection"
            / f"v{CURRENT_SCHEME_VERSION}" / "incarnation"
        )
        check(incarnation_path.is_file(), "incarnation token is persisted to disk")
        paths.require_private_file(incarnation_path)  # raises if not private
    finally:
        home.release()


def test_surface_projection_without_component_is_fresh_random_each_time() -> None:
    home = _test_home.TestHome.acquire("ba-test-scheme-projection-inmemory-")
    try:
        first = SurfaceProjection()
        second = SurfaceProjection()

        check(
            first.snapshot().incarnation != second.snapshot().incarnation,
            "plain SurfaceProjection() (no component) still gives a fresh "
            "random incarnation per construction",
        )
        check(
            not (paths.ba_home() / "scheme").exists(),
            "plain SurfaceProjection() (no component) persists nothing to disk",
        )
    finally:
        home.release()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
            else:
                print(f"PASS {name}")
    if failures:
        raise SystemExit(f"{failures} test(s) failed")
    print("all tests passed")
