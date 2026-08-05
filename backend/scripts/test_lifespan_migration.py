"""Regression: @app.on_event lifecycle hooks migrated to FastAPI lifespan.

Before the migration, `@app.on_event("startup"/"shutdown")` emitted a
DeprecationWarning at decoration time, so importing `main` / `main_node`
under `-W error::DeprecationWarning` failed before any test could run.
Passing `lifespan=` to the FastAPI factory removes the warning and leaves
`app.router.on_startup` / `on_shutdown` empty (the lifespan owns both).

The import runs in a fresh subprocess so the `-W error::DeprecationWarning`
filter is applied cleanly to module load. The subprocess mirrors the
import harness proven by `test_installation_lifespan_policy.py`: an
isolated `BETTER_AGENT_HOME` tempdir with an activated installation, so
the module-load side effects of `main` resolve.

Run with:
    cd backend && .venv/bin/python scripts/test_lifespan_migration.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
SDK = ROOT / "sdk"


def _child(home: Path) -> None:
    """Runs in the subprocess: isolate home, activate an installation, then
    import both apps under the parent's `-W error::DeprecationWarning` filter
    and assert the lifespan owns startup/shutdown."""
    os.environ["BETTER_AGENT_HOME"] = str(home)
    os.environ["BETTER_CLAUDE_HOME"] = str(home)
    os.environ["BETTER_AGENT_TEST_MODE"] = "1"
    os.environ["BETTER_CLAUDE_API_ONLY"] = "1"
    sys.path.insert(0, str(BACKEND))
    sys.path.insert(0, str(SDK))
    (home / "runs").mkdir()

    # Activate a real installation profile inside the temp home so the
    # module-load side effects of `main` / `main_node` resolve cleanly.
    import installation_profile
    import provider_setup

    profile_backend = home / "backend"
    installation_profile.BACKEND_ROOT = profile_backend
    environment = profile_backend / ".venvs" / "test"
    environment.mkdir(parents=True)
    (environment / ".dependency-plan.json").write_text(
        '{"schema_version": 1, "hash": "default"}', encoding="utf-8"
    )
    (profile_backend / ".active-venv").write_text(".venvs/test", encoding="utf-8")
    launcher = home / ("claude.cmd" if os.name == "nt" else "claude")
    launcher.write_bytes(
        b"@echo off\r\nexit /b 0\r\n" if os.name == "nt" else b"#!/bin/sh\nexit 0\n"
    )
    launcher.chmod(0o700)
    profile = installation_profile.new_active_profile(
        mode=installation_profile.DEFAULT,
        provider="claude",
        provider_identity=provider_setup.executable_identity(str(launcher.absolute())),
    )
    installation_profile.stage_activation(profile)
    import config_store
    config_store.apply_installation_profile_selection(make_default=True)

    # This import is the point of the test: under -W error::DeprecationWarning
    # it would raise if any @app.on_event decorator remained.
    import main
    import main_node

    for label, app in (("main", main.app), ("main_node", main_node.app)):
        assert app.router.on_startup == [], f"{label}: on_startup not empty"
        assert app.router.on_shutdown == [], f"{label}: on_shutdown not empty"
        assert app.router.lifespan_context is not None, f"{label}: no lifespan"


def test_lifespan_migration_no_deprecation_warning() -> None:
    """Importing main + main_node under -W error::DeprecationWarning must
    succeed and leave the on_event router lists empty (lifespan owns them)."""
    with tempfile.TemporaryDirectory(prefix="ba-lifespan-migration-") as tmp:
        result = subprocess.run(
            [
                sys.executable,
                "-W",
                "error::DeprecationWarning",
                __file__,
                "--child",
                tmp,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"lifespan migration subprocess failed (rc={result.returncode}):\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        _child(Path(sys.argv[2]))
    else:
        test_lifespan_migration_no_deprecation_warning()
        print("lifespan migration tests passed")
