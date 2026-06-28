"""Regression check for run.sh backend cleanup ordering.

Run with:
    cd backend && .venv/bin/python scripts/test_run_sh_cleanup.py
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUN_SH = ROOT / "run.sh"


def check(name: str, ok: bool, failures: list[str]) -> None:
    print(("  PASS" if ok else "  FAIL") + f": {name}")
    if not ok:
        failures.append(name)


def main() -> int:
    failures: list[str] = []
    text = RUN_SH.read_text(encoding="utf-8")
    zai_start = text.index("run_zai_startup_check() {")
    zai_end = text.index("\nbuild_frontend \"\"", zai_start)
    zai_check = text[zai_start:zai_end]

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
        "collect_descendants" not in text
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
        "restart waits gracefully before force kill",
        "wait_for_backend_exit() {" in text
        and "Restart requested — waiting up to ${GRACEFUL_RESTART_TIMEOUT_SECONDS}s for graceful shutdown..." in text
        and "Graceful restart timeout expired; forcing backend shutdown." in text
        and 'kill -9 "$BACKEND_PID"' in text,
        failures,
    )
    check(
        "main loop uses bounded backend exit wait",
        "wait_for_backend_exit" in text[text.index("while true; do"):],
        failures,
    )
    check(
        "Z.AI startup checker calls claude CLI directly",
        'claude = shutil.which("claude")' in zai_check
        and '"--model", "glm-5.2"' in zai_check
        and "You are the Better Agent run.sh startup checker" in zai_check,
        failures,
    )
    check(
        "Z.AI startup checker does not use Better Agent prompt/session path",
        '"$DIR/backend/cli.py"' not in zai_check
        and "Do not call backend/cli.py, bagent" in zai_check
        and "Better Agent prompt/session path" in zai_check,
        failures,
    )
    check(
        "Z.AI startup checker uses provider config without Better Agent dependency",
        "config_store.list_providers()" in zai_check
        and "ANTHROPIC_API_KEY" in zai_check
        and "ANTHROPIC_BASE_URL" in zai_check
        and "not through Better Agent" in zai_check,
        failures,
    )
    check(
        "Z.AI startup checker verifies run.sh operational success",
        "verify that the run.sh invocation that spawned you actually succeeded" in zai_check
        and "Verify backend health and key REST endpoints using direct shell commands" in zai_check
        and "Verify the frontend is being served from the backend" in zai_check
        and "Inspect recent backend/run logs" in zai_check,
        failures,
    )
    check(
        "Z.AI startup checker reruns without recursive checker",
        "BETTER_AGENT_SKIP_ZAI_STARTUP_CHECK=1" in zai_check,
        failures,
    )
    check(
        "Z.AI startup checker cannot fail run.sh",
        'echo "Z.AI glm-5.2 startup checker failed. See $log_path"' in zai_check
        and "return 0" in zai_check.split('echo "Z.AI glm-5.2 startup checker failed. See $log_path"', 1)[1],
        failures,
    )

    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
