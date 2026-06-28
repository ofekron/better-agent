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
    original_grant = provider_runtime.grant_project_access_if_enabled
    work = Path(tempfile.mkdtemp(prefix="bc-provider-runtime-project-"))
    try:
        provider_runtime.platform.system = lambda: "Darwin"  # type: ignore[assignment]
        commands = provider_runtime.plan_project_access_commands(str(work), "betteragent")
        check("project access uses chmod argv", commands[0].argv[:3] == ("chmod", "-R", "+a"))
        check("project ACL targets group", "group:betteragent" in commands[0].argv[3])

        granted: list[str] = []

        def fake_grant(project: dict) -> bool:
            granted.append(project["path"])
            return True

        provider_runtime.grant_project_access_if_enabled = fake_grant  # type: ignore[assignment]
        rec = project_store.add_project(str(work), node_id="primary")
        check("project_store returns project", rec is not None)
        check("project_store grants local project access", granted == [str(work.resolve())])
        granted.clear()
        remote = project_store.add_project(str(work / "remote"), node_id="remote")
        check("remote project is still recorded", remote is not None)
        check("project_store does not grant remote project access", granted == [])
    finally:
        provider_runtime.platform.system = original_system  # type: ignore[assignment]
        provider_runtime.grant_project_access_if_enabled = original_grant  # type: ignore[assignment]
        shutil.rmtree(work, ignore_errors=True)


def test_broad_paths_rejected() -> None:
    check("home directory access rejected", not provider_runtime.project_access_allowed(paths.user_home()))
    try:
        provider_runtime.plan_project_access_commands(str(paths.user_home()), "betteragent")
    except ValueError:
        check("home directory access plan rejected", True)
    else:
        check("home directory access plan rejected", False)


try:
    test_config_and_user_plan()
    test_invalid_names_rejected()
    test_project_access_plans_and_project_store_hook()
    test_broad_paths_rejected()
finally:
    shutil.rmtree(_TMP_HOME, ignore_errors=True)

if failures:
    print(f"\n{failures} check(s) failed")
    sys.exit(1)
print("\nall checks passed")
