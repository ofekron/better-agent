from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN_SH = ROOT / "run.sh"


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


def test_run_sh_exports_backend_port_for_mobile_candidate_generation() -> None:
    source = _run_sh()

    assert 'export BA_BACKEND_PORT="$BACKEND_PORT"' in source


if __name__ == "__main__":
    test_run_sh_uses_non_standard_backend_port_by_default()
    test_run_sh_initializes_provider_config_sync_before_frontend_build()
    test_run_sh_installs_node_dependencies_before_frontend_build()
    test_run_sh_exports_backend_port_for_mobile_candidate_generation()
