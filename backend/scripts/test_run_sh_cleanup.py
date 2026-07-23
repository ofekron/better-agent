"""Regression check for run.sh backend cleanup ordering.

Run with:
    cd backend && .venv/bin/python scripts/test_run_sh_cleanup.py
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN_SH = ROOT / "run.sh"
RUN_WINDOWS = ROOT / "run_windows.bat"
APP_ENTRY = ROOT / "backend" / "app_entry.py"
MAIN = ROOT / "backend" / "main.py"
BROWSER_SUPERVISOR = ROOT / "desktop" / "browser_backend_supervisor.py"
LOCAL_CODESIGN = ROOT / "desktop" / "local_codesign.sh"
CREDENTIAL_BUILD = ROOT / "desktop" / "build_credential_authority.sh"


def check(name: str, ok: bool, failures: list[str]) -> None:
    print(("  PASS" if ok else "  FAIL") + f": {name}")
    if not ok:
        failures.append(name)


def main() -> int:
    failures: list[str] = []
    text = RUN_SH.read_text(encoding="utf-8")
    windows_text = RUN_WINDOWS.read_text(encoding="utf-8")
    app_entry_text = APP_ENTRY.read_text(encoding="utf-8")
    main_text = MAIN.read_text(encoding="utf-8")
    browser_supervisor_text = BROWSER_SUPERVISOR.read_text(encoding="utf-8")
    local_codesign_text = LOCAL_CODESIGN.read_text(encoding="utf-8")
    credential_build_text = CREDENTIAL_BUILD.read_text(encoding="utf-8")

    check(
        "launchctl bootout helper exists",
        "launchctl bootout" in text,
        failures,
    )
    check(
        "known launchctl backend label is stopped",
        'bootout_launchctl_job "better-claude-provider-config-sync"' in text,
        failures,
    )
    check(
        "launchctl path includes Homebrew and local CLIs",
        'export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"' in text,
        failures,
    )
    deps_start = text.index("sync_backend_deps() {")
    deps_end = text.index("# --- Install the `bagent` CLI command", deps_start)
    deps_source = text[deps_start:deps_end]
    check(
        "backend dependencies activate through the atomic plan",
        '"$DIR/backend/dependency_plan.py" activate --uv "$UV"' in deps_source
        and 'export BETTER_AGENT_BACKEND_PYTHON="$PY"' in deps_source
        and ".requirements.stamp" not in deps_source,
        failures,
    )
    check(
        "lock holder cleanup exists",
        "kill_backend_lock_holder()" in text,
        failures,
    )
    check(
        "lock holder cleanup runs before startup port resolution",
        text.index("kill_backend_lock_holder\nBACKEND_PORT=") < text.index("resolve_port_conflict \"$BACKEND_PORT\" \"backend\""),
        failures,
    )
    check(
        "lock holder cleanup runs before backend respawn port resolution",
        text.index("kill_backend_lock_holder\n  BACKEND_PORT=") < text.index("resolve_port_conflict \"$BACKEND_PORT\" \"backend\"", text.index("start_backend()")),
        failures,
    )
    check(
        "backend wrapper process pattern is covered",
        "cd $DIR/backend && .*uvicorn main:app.*--port $port" in text,
        failures,
    )
    check(
        "lock holder command is scoped to this checkout",
        '*"$DIR/backend"*uvicorn*"main:app"*|*"$DIR/backend/app_entry.py"*"--serve"*)' in text,
        failures,
    )
    lock_start = text.index("kill_backend_lock_holder() {")
    lock_end = text.index("\nstop_known_better_agent_port_users()", lock_start)
    lock_cleanup = text[lock_start:lock_end]
    check(
        "lock holder cleanup does not kill descendants",
        "collect_descendants" not in lock_cleanup
        and "pgrep -P" not in lock_cleanup
        and "xargs kill" not in lock_cleanup,
        failures,
    )
    check(
        "cleanup does not use pipefail-sensitive pgrep pipeline",
        "pgrep -f \"$pattern\" 2>/dev/null | grep -v" not in text,
        failures,
    )
    check(
        "cleanup tolerates no matching process",
        "pgrep -f \"$pattern\" 2>/dev/null || true" in text,
        failures,
    )
    check(
        "graceful restart timeout is configurable",
        'GRACEFUL_RESTART_TIMEOUT_SECONDS="${BETTER_AGENT_GRACEFUL_RESTART_TIMEOUT_SECONDS:-8}"' in text
        and "restart_limit=$((GRACEFUL_RESTART_TIMEOUT_SECONDS * 4))" in text,
        failures,
    )
    check(
        "supervised uvicorn launch skips proxy header parsing",
        "--no-proxy-headers" in browser_supervisor_text
        and "--no-proxy-headers" in windows_text
        and app_entry_text.count("proxy_headers=False") >= 2
        and "proxy_headers=False" in main_text,
        failures,
    )
    check(
        "supervised uvicorn launch disables websocket compression",
        '"--ws-per-message-deflate",\n                "false"' in browser_supervisor_text
        and app_entry_text.count("ws_per_message_deflate=False") >= 2
        and "ws_per_message_deflate=False" in main_text,
        failures,
    )
    check(
        "restart waits gracefully before force kill",
        "wait_for_backend_exit() {" in text
        and "Restart requested — waiting up to ${GRACEFUL_RESTART_TIMEOUT_SECONDS}s for graceful shutdown..." in text
        and "Graceful restart timeout expired; forcing backend shutdown." in text
        and "credential_backend_control signal --signal KILL" in text,
        failures,
    )
    check(
        "main loop uses bounded backend exit wait",
        "wait_for_backend_exit" in text[text.index("while true; do"):],
        failures,
    )
    check(
        "credential control client stays a direct launcher child",
        "credential_backend_control start" in text
        and '> "$pid_file"' in text
        and 'BACKEND_PID="$(credential_backend_control' not in text,
        failures,
    )
    check(
        "macOS run.sh uses the signed Better Agent credential authority",
        'CREDENTIAL_AUTHORITY="$DIR/desktop/dist/BetterAgentCredentialAuthority/BetterAgentCredentialAuthority"' in text
        and '"$DIR/desktop/build_credential_authority.sh" >/dev/null' in text
        and '"$CREDENTIAL_AUTHORITY" \\' in text,
        failures,
    )
    check(
        "local credential authority signing is stable and least-privilege",
        'IDENTIFIER="com.betteragent.app"' in local_codesign_text
        and 'security find-identity -v -p codesigning' in local_codesign_text
        and '-x -T /usr/bin/codesign' in local_codesign_text
        and ' -A ' not in local_codesign_text
        and 'codesign --verify --deep --strict' in local_codesign_text,
        failures,
    )
    check(
        "credential authority rebuilds when credential code changes",
        '"$DIR/browser_backend_supervisor.py"' in credential_build_text
        and '"$DIR/credential_session.py"' in credential_build_text
        and '"$REPO/backend/provider_credentials.py"' in credential_build_text
        and '"$REPO/backend/oskeychain.py"' in credential_build_text,
        failures,
    )
    check(
        "run.sh never reads provider credentials outside the broker",
        "get_provider_with_key" not in text
        and "ANTHROPIC_API_KEY" not in text
        and "run_zai_startup_check" not in text,
        failures,
    )
    loop_cleanup = text[text.index("\nreap_completed_children\n", text.index("while true; do")) :]
    check(
        "normal run.sh loop exit cleans owned long-lived children",
        'stop_child_process "frontend build" "$FRONTEND_BUILD_PID"' in loop_cleanup
        and 'stop_child_process "daemon host" "$DAEMON_HOST_PID"' in loop_cleanup
        and "stop_credential_backend_supervisor" in loop_cleanup
        and 'stop_child_process "startup checker" "$ZAI_STARTUP_CHECK_PID"' not in loop_cleanup,
        failures,
    )

    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
