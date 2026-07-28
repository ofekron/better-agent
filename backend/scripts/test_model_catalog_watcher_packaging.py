from __future__ import annotations

import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from scripts import _test_home  # noqa: E402


TEST_HOME = _test_home.TestHome.acquire("ba-test-watcher-packaging-")

import dependency_plan  # noqa: E402


def test_watcher_is_a_direct_base_runtime_dependency() -> None:
    requirements = (BACKEND / "requirements.txt").read_text(encoding="utf-8")

    assert any(
        line.startswith("watchfiles>=")
        for line in requirements.splitlines()
    )
    assert "watchfiles" in dependency_plan.BASE_PROBES


def test_desktop_bundle_collects_native_watcher_runtime() -> None:
    spec = (REPO / "desktop" / "BetterAgent.spec").read_text(encoding="utf-8")

    assert '"watchfiles"' in spec


def test_clean_install_and_platform_matrix_import_watcher() -> None:
    smoke = (REPO / "tests" / "install-smoke" / "Dockerfile").read_text(
        encoding="utf-8",
    )
    workflow = (
        REPO / ".github" / "workflows" / "installation-mode-parity.yml"
    ).read_text(encoding="utf-8")

    assert "import fastapi, uvicorn, argon2, watchfiles" in smoke
    assert "test_model_catalog_watcher_packaging.py" in workflow


if __name__ == "__main__":
    test_watcher_is_a_direct_base_runtime_dependency()
    test_desktop_bundle_collects_native_watcher_runtime()
    test_clean_install_and_platform_matrix_import_watcher()
    print("PASS: model catalog watcher is packaged on supported runtimes")
