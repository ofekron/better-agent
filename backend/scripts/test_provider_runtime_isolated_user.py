from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-provider-runtime-")
_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import project_store  # noqa: E402
import provider_runtime  # noqa: E402
import paths  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"
failures = 0


def check(label: str, cond: bool) -> None:
    global failures
    print(f"{PASS if cond else FAIL} {label}")
    if not cond:
        failures += 1


def completed(argv: tuple[str, ...], returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(argv, returncode, "", "")


def test_config_and_user_plan() -> None:
    original = provider_runtime.platform.system
    seen: list[tuple[str, ...]] = []
    try:
        provider_runtime.platform.system = lambda: "Darwin"  # type: ignore[assignment]

        def runner(argv):
            seen.append(tuple(argv))
            if argv[:3] in (("dscl", ".", "-read"),):
                return completed(tuple(argv), 1)
            return completed(tuple(argv))

        config = provider_runtime.apply_isolated_user(
            "betteragent",
            "betteragent",
            current_user="ofek",
            runner=runner,
        )
        check("isolated user config enabled", config.isolated_user_enabled)
        check("config persisted", provider_runtime.load_config().username == "betteragent")
        check("macOS user plan uses argv", all(isinstance(cmd, tuple) for cmd in seen))
        check("macOS plan creates group", any(cmd[:3] == ("dseditgroup", "-o", "create") for cmd in seen))
        check("macOS plan creates user", any(cmd and cmd[0] == "sysadminctl" for cmd in seen))
    finally:
        provider_runtime.platform.system = original  # type: ignore[assignment]


def test_invalid_names_rejected() -> None:
    try:
        provider_runtime.plan_isolated_user_commands("../bad")
    except ValueError:
        check("invalid username rejected", True)
    else:
        check("invalid username rejected", False)


def test_project_access_plans_and_project_store_hook() -> None:
    original_system = provider_runtime.platform.system
    work = Path(tempfile.mkdtemp(prefix="bc-provider-runtime-project-"))
    try:
        provider_runtime.platform.system = lambda: "Darwin"  # type: ignore[assignment]
        commands = provider_runtime.plan_project_access_commands(str(work), "betteragent")
        check("project access uses chmod argv", commands[0].argv[:3] == ("chmod", "-R", "+a"))
        check("project ACL targets group", "group:betteragent" in commands[0].argv[3])

        rec = project_store.add_project(str(work), node_id="primary")
        check("project_store returns project", rec is not None)
        remote = project_store.add_project(str(work / "remote"), node_id="remote")
        check("remote project is still recorded", remote is not None)
    finally:
        provider_runtime.platform.system = original_system  # type: ignore[assignment]
        shutil.rmtree(work, ignore_errors=True)


def test_broad_paths_rejected() -> None:
    check("home directory access rejected", not provider_runtime.project_access_allowed(paths.user_home()))
    check("root child access rejected", not provider_runtime.project_access_allowed(paths.user_home().parent))
    check("tmp root access rejected", not provider_runtime.project_access_allowed(Path("/tmp")))
    try:
        provider_runtime.plan_project_access_commands(str(paths.user_home()), "betteragent")
    except ValueError:
        check("home directory access plan rejected", True)
    else:
        check("home directory access plan rejected", False)


def test_isolated_runner_spawn_wrapper() -> None:
    original_system = provider_runtime.platform.system
    original_popen = provider_runtime.subprocess.Popen
    original_runtime_access = provider_runtime.grant_runtime_path_access
    original_project_access = provider_runtime.grant_project_access
    run_dir = paths.ba_home() / "runs" / "run-1"
    project = Path(tempfile.mkdtemp(prefix="bc-provider-runtime-spawn-"))
    run_dir.mkdir(parents=True)
    calls: dict[str, object] = {}
    try:
        provider_runtime.platform.system = lambda: "Darwin"  # type: ignore[assignment]
        provider_runtime.enable_isolated_user("betteragent", "betteragent")

        def fake_runtime(path, *, group="betteragent", runner=None):
            calls["runtime"] = (str(Path(path).resolve()), group)

        def fake_project(path, *, group="betteragent", runner=None):
            calls["project"] = (str(Path(path).resolve()), group)

        class FakePopen:
            def __init__(self, argv, **kwargs):
                calls["argv"] = tuple(argv)
                calls["kwargs"] = kwargs

        provider_runtime.grant_runtime_path_access = fake_runtime  # type: ignore[assignment]
        provider_runtime.grant_project_access = fake_project  # type: ignore[assignment]
        provider_runtime.subprocess.Popen = FakePopen  # type: ignore[assignment]

        provider_runtime.popen_runner(
            ("python3", "runner.py"),
            run_dir=run_dir,
            project_cwd=str(project),
            cwd=str(project),
            env={
                "BETTER_CLAUDE_INTERNAL_TOKEN": "secret-token",
                "BETTER_AGENT_BACKEND_URL": "http://127.0.0.1:8000",
                "BETTER_AGENT_HOME": "do-not-preserve",
                "ANTHROPIC_API_KEY": "secret-key",
                "OPENAI_BASE_URL": "https://openai-compatible.example",
                "GEMINI_CLI_HOME": "/tmp/gemini-home",
                "UNSAFE_ENV": "do-not-preserve",
            },
        )
        check("runner argv uses sudo", calls["argv"][:3] == ("sudo", "-n", "-H"))
        check("runner argv uses isolated user", "-u" in calls["argv"] and "betteragent" in calls["argv"])
        argv_text = " ".join(calls["argv"])
        check("runner argv preserves Better Agent env name", "BETTER_CLAUDE_INTERNAL_TOKEN" in argv_text)
        check("runner argv preserves current Better Agent env alias", "BETTER_AGENT_BACKEND_URL" in argv_text)
        check("runner argv preserves provider auth env name", "ANTHROPIC_API_KEY" in argv_text)
        check("runner argv preserves OpenAI base URL env name", "OPENAI_BASE_URL" in argv_text)
        check("runner argv preserves Gemini CLI home env name", "GEMINI_CLI_HOME" in argv_text)
        check("runner argv does not include secret values", "secret-token" not in argv_text and "secret-key" not in argv_text)
        check("runner argv does not preserve unknown env", "UNSAFE_ENV" not in argv_text)
        check("runner argv does not preserve Better Agent state home", "BETTER_AGENT_HOME" not in argv_text)
        check("runtime path access prepared", calls["runtime"] == (str(run_dir.resolve()), "betteragent"))
        check("project path access prepared", calls["project"] == (str(project.resolve()), "betteragent"))
    finally:
        provider_runtime.platform.system = original_system  # type: ignore[assignment]
        provider_runtime.subprocess.Popen = original_popen  # type: ignore[assignment]
        provider_runtime.grant_runtime_path_access = original_runtime_access  # type: ignore[assignment]
        provider_runtime.grant_project_access = original_project_access  # type: ignore[assignment]
        provider_runtime.disable_isolated_user()
        shutil.rmtree(project, ignore_errors=True)


def test_runtime_path_access_grants_ancestors() -> None:
    original_system = provider_runtime.platform.system
    run_dir = paths.ba_home() / "runs" / "run-ancestor"
    run_dir.mkdir(parents=True)
    seen: list[tuple[str, ...]] = []
    try:
        provider_runtime.platform.system = lambda: "Darwin"  # type: ignore[assignment]

        def runner(argv):
            seen.append(tuple(argv))
            return completed(tuple(argv))

        provider_runtime.grant_runtime_path_access(run_dir, group="betteragent", runner=runner)
        targets = [cmd[-1] for cmd in seen]
        check("runtime access grants ba_home search", str(paths.ba_home().resolve()) in targets)
        check("runtime access grants runs search", str((paths.ba_home() / "runs").resolve()) in targets)
        check("runtime access grants run dir rw", str(run_dir.resolve()) in targets)
        check("runtime run dir grant is recursive", any(cmd[:3] == ("chmod", "-R", "+a") and cmd[-1] == str(run_dir.resolve()) for cmd in seen))
    finally:
        provider_runtime.platform.system = original_system  # type: ignore[assignment]


try:
    test_config_and_user_plan()
    test_invalid_names_rejected()
    test_project_access_plans_and_project_store_hook()
    test_broad_paths_rejected()
    test_isolated_runner_spawn_wrapper()
    test_runtime_path_access_grants_ancestors()
finally:
    shutil.rmtree(_TMP_HOME, ignore_errors=True)

if failures:
    print(f"\n{failures} check(s) failed")
    sys.exit(1)
print("\nall checks passed")
