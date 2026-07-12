from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN_SH = ROOT / "run.sh"
BOOTSTRAP_WINDOWS = ROOT / "scripts" / "bootstrap-windows.ps1"
BUILD_MACOS = ROOT / "desktop" / "build_macos.sh"
BUILD_WINDOWS = ROOT / "desktop" / "build_windows.ps1"
DESKTOP_SPEC = ROOT / "desktop" / "BetterAgent.spec"


def _run_sh() -> str:
    return RUN_SH.read_text(encoding="utf-8")


def test_run_sh_uses_non_standard_backend_port_by_default() -> None:
    source = _run_sh()

    assert "DEFAULT_BACKEND_PORT=18765" in source
    assert "BETTER_CLAUDE_BACKEND_PORT:-8000" not in source


def test_run_sh_initializes_provider_config_sync_before_frontend_build() -> None:
    source = _run_sh()

    submodule_call = source.index("ensure_provider_config_sync_submodule")
    frontend_build = source.index("build_frontend()")

    assert submodule_call < frontend_build
    assert "git -C \"$DIR\" submodule update --init provider-config-sync" in source


def test_run_sh_installs_node_dependencies_before_frontend_build() -> None:
    source = _run_sh()

    provider_install = source.index(
        'sync_npm_project_deps "$DIR/provider-config-sync" "provider-config-sync"'
    )
    frontend_install = source.index('sync_npm_project_deps "$DIR/frontend" "frontend"')
    frontend_build = source.index("build_frontend()")

    assert provider_install < frontend_build
    assert frontend_install < frontend_build
    assert "(cd \"$project_dir\" && npm ci)" in source


def test_backend_dependency_stamp_hashes_local_artifacts() -> None:
    source = _run_sh()

    assert 'candidate = (path.parent / requirement).resolve()' in source
    assert 'digest.update(hashlib.sha256(candidate.read_bytes()).digest())' in source


def test_desktop_builds_sync_runtime_dependencies_on_both_platforms() -> None:
    macos = BUILD_MACOS.read_text(encoding="utf-8")
    windows = BUILD_WINDOWS.read_text(encoding="utf-8")

    assert '( cd "$REPO/backend" && "$VENV/bin/pip" install -q -r requirements.txt )' in macos
    assert 'Push-Location (Join-Path $Repo "backend")' in windows
    assert '& $Pip install -q -r requirements.txt' in windows
    assert '"provider_config_sync_backend")' in DESKTOP_SPEC.read_text(encoding="utf-8")


def test_run_sh_exports_backend_port_for_mobile_candidate_generation() -> None:
    source = _run_sh()

    assert 'export BA_BACKEND_PORT="$BACKEND_PORT"' in source


def test_run_sh_checks_base_prereqs_before_startup_work() -> None:
    source = _run_sh()

    prereq_call = source.index("ensure_base_prereqs")
    port_check = source.index('echo "Checking startup ports..."')
    submodule_call = source.index("ensure_provider_config_sync_submodule")

    assert prereq_call < port_check
    assert prereq_call < submodule_call
    assert "Run ./scripts/bootstrap-macos.sh, then run ./run.sh again." in source
    assert "git npm node curl" in source
    assert "port_in_use()" in source
    assert "listener details are unavailable because lsof is not installed" in source
    assert 'if [ "$(uname -s)" = "Darwin" ] && { ! kc_has username' in source
    assert 'const { createHash } = require("node:crypto");' in source


def test_windows_bootstrap_installs_base_prereqs_with_winget() -> None:
    source = BOOTSTRAP_WINDOWS.read_text(encoding="utf-8")

    assert 'Install-WingetPackage -Id "Git.Git" -Command "git"' in source
    assert 'Install-WingetPackage -Id "Python.Python.3.13" -Command "python"' in source
    assert 'Install-WingetPackage -Id "astral-sh.uv" -Command "uv"' in source
    assert 'Install-WingetPackage -Id "OpenJS.NodeJS.LTS" -Command "node"' in source
    assert 'npm install -g "@anthropic-ai/claude-code"' in source
    assert 'npm install -g "@openai/codex"' in source


if __name__ == "__main__":
    test_run_sh_uses_non_standard_backend_port_by_default()
    test_run_sh_initializes_provider_config_sync_before_frontend_build()
    test_run_sh_installs_node_dependencies_before_frontend_build()
    test_backend_dependency_stamp_hashes_local_artifacts()
    test_desktop_builds_sync_runtime_dependencies_on_both_platforms()
    test_run_sh_exports_backend_port_for_mobile_candidate_generation()
    test_run_sh_checks_base_prereqs_before_startup_work()
    test_windows_bootstrap_installs_base_prereqs_with_winget()
