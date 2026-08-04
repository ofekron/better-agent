"""Regression check for run.sh backend cleanup ordering.

Run with:
    cd backend && .venv/bin/python scripts/test_run_sh_cleanup.py
"""
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN_SH = ROOT / "run.sh"
RUN_WINDOWS = ROOT / "run_windows.bat"
APP_ENTRY = ROOT / "backend" / "app_entry.py"
MAIN = ROOT / "backend" / "main.py"
BROWSER_SUPERVISOR = ROOT / "desktop" / "browser_backend_supervisor.py"
RESTART_REQUEST = (
    ROOT / "switch_control_daemon" / "line_switch_runtime" / "restart_request.py"
)
SWITCH_REQUESTS = (
    ROOT / "switch_control_daemon" / "line_switch_runtime" / "requests.py"
)
SERVER_CONFIG = ROOT / "backend" / "server_config.py"
WINDOWS_SOURCE_LAUNCHER = ROOT / "desktop" / "windows_source_launcher.py"
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
    restart_request_text = RESTART_REQUEST.read_text(encoding="utf-8")
    switch_requests_text = SWITCH_REQUESTS.read_text(encoding="utf-8")
    server_config_text = SERVER_CONFIG.read_text(encoding="utf-8")
    windows_source_launcher_text = WINDOWS_SOURCE_LAUNCHER.read_text(encoding="utf-8")
    local_codesign_text = LOCAL_CODESIGN.read_text(encoding="utf-8")
    credential_build_text = CREDENTIAL_BUILD.read_text(encoding="utf-8")

    check(
        "launchctl bootout helper exists",
        "launchctl bootout" in text,
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
    loop_start = text.index(
        "  while true; do",
        text.index("start_credential_backend_supervisor"),
    )
    generation_loop = text[loop_start:]
    activation_call = generation_loop.index("admit_backend_generation")
    backend_start = generation_loop.index("if ! start_backend; then")
    before_activation = generation_loop[:activation_call]
    before_backend_start = generation_loop[activation_call:backend_start]
    check(
        "every backend generation activates and revalidates its checkout before spawn",
        activation_call < backend_start
        and "SWITCH_POINTER_SNAPSHOT=" in before_activation
        and 'if [ "$GENERATION_ADMISSION" -eq 2 ]' in before_backend_start
        and 'if [ "$GENERATION_ADMISSION" -ne 0 ]' in before_backend_start,
        failures,
    )
    check(
        "dependency activation failure reverts only the snapshotted switch before spawn",
        (
            'admit_backend_generation || GENERATION_ADMISSION=$?'
        )
        in before_backend_start
        and backend_start > activation_call,
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
        and "BrowserBackendSupervisor" in windows_source_launcher_text
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
        "all production launchers bound graceful connection drain",
        '"--timeout-graceful-shutdown"' in browser_supervisor_text
        and app_entry_text.count(
            "timeout_graceful_shutdown=timeout_graceful_shutdown"
        ) == 2
        and "GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 8" in server_config_text,
        failures,
    )
    check(
        "Windows source launcher uses the shared supervised recovery path",
        "-m desktop.windows_source_launcher" in windows_text
        and "BrowserBackendSupervisor" in windows_source_launcher_text
        and "decide_recovery" in windows_source_launcher_text
        and "consume_restart_request" in windows_source_launcher_text
        and "append_backend_exit" in windows_source_launcher_text
        and "taskkill /F /IM uvicorn.exe" not in windows_text,
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
        "unexpected backend exits use bounded recovery",
        'BACKEND_CRASH_LIMIT="${BETTER_AGENT_BACKEND_CRASH_LIMIT:-5}"' in text
        and "Backend exited unexpectedly (code $BACKEND_EXIT_CODE)" in text
        and "Backend crash recovery circuit opened" in text,
        failures,
    )
    check(
        "backend respawn waits for terminal generation acknowledgement",
        "Credential backend supervisor did not acknowledge terminal generation." in text
        and '[ "$STATUS_GENERATION_ID" = "$BACKEND_GENERATION_ID" ]' in text
        and "BACKEND_EXIT_CODE=" in text,
        failures,
    )
    check(
        "restart intent uses hardened shared consumer",
        "-m restart_request" in text
        and '"$FLAG" --clear' in text
        and '--not-before "$BACKEND_GENERATION_STARTED_AT"' in text
        and 'PENDING_REFRESH_ID="$(cat "$FLAG")"' not in text,
        failures,
    )
    check(
        "backend and launcher share atomic restart-intent authority",
        "valid_restart_request_id(request_id)" in main_text
        and "await asyncio.to_thread(write_restart_request, flag, request_id)"
        in main_text
        and "def write_restart_request(" in restart_request_text
        and "os.replace(temporary, path)" in restart_request_text,
        failures,
    )
    check(
        "line switch daemon uses canonical restart-intent authority",
        "write_restart_request(restart_request_path(), request_id)"
        in switch_requests_text
        and "write_text(restart_request_path(), request_id)"
        not in switch_requests_text,
        failures,
    )
    check(
        "source crash circuit executes tested recovery policy",
        "-m desktop.backend_recovery_policy" in text
        and 'RECOVERY_ACTION BACKEND_CRASH_ATTEMPTS BACKEND_BACKOFF' in text,
        failures,
    )
    check(
        "browser launcher persists generation exit evidence",
        "-m desktop.backend_exit_journal" in text
        and '--generation-id "$BACKEND_GENERATION_ID"' in text
        and '--pid "$BACKEND_EXIT_PID"' in text,
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


def _shell_function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def test_generation_admission_behavior() -> None:
    text = RUN_SH.read_text(encoding="utf-8")
    function = _shell_function(text, "admit_backend_generation")
    scenarios = (
        (0, 1, "same", 0),
        (0, 1, "changed", 2),
        (1, 0, "same", 2),
        (1, 1, "same", 1),
    )
    for activation_status, revert_status, current_snapshot, expected in scenarios:
        script = f"""
activate_checkout_dependencies() {{ return {activation_status}; }}
revert_snapshotted_switch() {{ return {revert_status}; }}
current_pointer_snapshot() {{ printf '%s' {current_snapshot!r}; }}
ACTIVE_DIR=/target
SWITCH_POINTER_SNAPSHOT=same
{function}
admit_backend_generation >/dev/null 2>&1
printf '%s' "$?"
"""
        result = subprocess.run(
            ["bash", "-c", script],
            check=True,
            capture_output=True,
            text=True,
        )
        assert int(result.stdout) == expected


def test_generation_rollback_uses_snapshotted_request(tmp_path: Path) -> None:
    text = RUN_SH.read_text(encoding="utf-8")
    function = _shell_function(text, "revert_snapshotted_switch")
    capture = tmp_path / "argv"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {str(capture)!r}\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    script = f"""
DIR=/source-checkout
PY={str(fake_python)!r}
SWITCH_REQUEST_ID=request-123
{function}
revert_snapshotted_switch
"""
    subprocess.run(["bash", "-c", script], check=True)
    assert capture.read_text(encoding="utf-8").splitlines() == [
        "-m",
        "daemonhost.pointer",
        "revert-if-switching",
        "--reason",
        "target dependency activation failed",
        "--request-id",
        "request-123",
    ]


if __name__ == "__main__":
    raise SystemExit(main())
