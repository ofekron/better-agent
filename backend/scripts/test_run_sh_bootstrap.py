from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN_SH = ROOT / "run.sh"
BOOTSTRAP_WINDOWS = ROOT / "scripts" / "install-windows.ps1"


def _run_sh() -> str:
    return RUN_SH.read_text(encoding="utf-8")


def test_run_sh_uses_non_standard_backend_port_by_default() -> None:
    source = _run_sh()

    assert "DEFAULT_BACKEND_PORT=18765" in source
    assert "BETTER_CLAUDE_BACKEND_PORT:-8000" not in source


def test_run_sh_without_bas_prefers_main_then_uses_current_checkout() -> None:
    source = _run_sh()

    guard = source.index('if [ "${BETTER_AGENT_RUN_SH_SERVICE_CHILD:-0}" != "1" ] && ! bas_available')
    home_setup = source.index('BA_HOME="${BETTER_AGENT_HOME:-${BETTER_CLAUDE_HOME:-$HOME/.better-claude}}"')

    service_dispatch = source.index('--install-service|--uninstall-service|--service-status')
    assert home_setup < service_dispatch < guard
    assert 'command -v bas >/dev/null 2>&1 || [ -x "$HOME/ba-switch/bas" ]' in source
    assert 'case "$(basename "$DIR")" in' in source
    assert 'candidate="$parent/${name%-qa}-main"' in source
    assert 'candidate="$DIR-main"' in source
    assert 'if [ -n "$MAIN_CHECKOUT" ]; then' in source
    assert 'exec "$MAIN_CHECKOUT/run.sh" "$@"' in source
    assert "launching current checkout at $DIR" in source
    assert "Refusing to launch a non-main line" not in source
    assert "BETTER_AGENT_RUN_SH_SERVICE_CHILD" in source


def test_plain_run_sh_delegates_matching_checkout_to_bas() -> None:
    source = _run_sh()
    delegation = source.index('BAS_LINE="$("$BAS_BIN" resolve-line "$DIR"')
    fallback = source.index('if [ "${BETTER_AGENT_RUN_SH_SERVICE_CHILD:-0}" != "1" ] && ! bas_available')
    assert delegation < fallback
    assert 'exec "$BAS_BIN" exec-line "$BAS_LINE"' in source
    assert '[[ "$BAS_LINE" =~ ^[a-z0-9][a-z0-9_.-]{0,31}$ ]]' in source


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


def test_run_sh_exports_backend_port_for_mobile_candidate_generation() -> None:
    source = _run_sh()

    assert 'export BA_BACKEND_PORT="$BACKEND_PORT"' in source


def test_run_sh_does_not_prompt_for_an_unserved_frontend_port() -> None:
    source = _run_sh()

    assert "FRONTEND_PORT" not in source
    assert 'resolve_port_conflict "$BACKEND_PORT" "backend"' in source


def test_run_sh_checks_base_prereqs_before_startup_work() -> None:
    source = _run_sh()

    prereq_call = source.index("ensure_base_prereqs")
    port_check = source.index('echo "Checking startup ports..."')
    submodule_call = source.index("ensure_provider_config_sync_submodule")

    assert prereq_call < port_check
    assert prereq_call < submodule_call
    assert "Run ./scripts/install-macos.sh, then run ./run.sh again." in source
    assert "git npm node curl" in source
    assert "port_in_use()" in source
    assert "listener details are unavailable because lsof is not installed" in source
    assert 'if [ "$(uname -s)" = "Darwin" ] && { ! kc_has username' in source
    assert 'const { createHash } = require("node:crypto");' in source


def test_windows_installer_installs_base_prereqs_and_runs_shared_flow() -> None:
    source = BOOTSTRAP_WINDOWS.read_text(encoding="utf-8")

    assert 'Install-WingetPackage -Id "Git.Git" -Command "git"' in source
    assert 'Install-WingetPackage -Id "Python.Python.3.13" -Command "python"' in source
    assert 'Install-WingetPackage -Id "astral-sh.uv" -Command "uv"' in source
    assert 'Install-WingetPackage -Id "OpenJS.NodeJS.LTS" -Command "node"' in source
    assert '$installerArgs = @("$PSScriptRoot\\install.py")' in source
    assert '& python @installerArgs' in source


if __name__ == "__main__":
    test_run_sh_uses_non_standard_backend_port_by_default()
    test_run_sh_without_bas_prefers_main_then_uses_current_checkout()
    test_plain_run_sh_delegates_matching_checkout_to_bas()
    test_run_sh_initializes_provider_config_sync_before_frontend_build()
    test_run_sh_installs_node_dependencies_before_frontend_build()
    test_run_sh_exports_backend_port_for_mobile_candidate_generation()
    test_run_sh_does_not_prompt_for_an_unserved_frontend_port()
    test_run_sh_checks_base_prereqs_before_startup_work()
    test_windows_installer_installs_base_prereqs_and_runs_shared_flow()
