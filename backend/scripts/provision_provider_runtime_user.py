#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import project_store  # noqa: E402
import provider_runtime  # noqa: E402


def _sudo_runner(argv):
    command = tuple(argv)
    if os.geteuid() == 0:
        run_argv = command
    else:
        sudo = shutil.which("sudo")
        if not sudo:
            return subprocess.CompletedProcess(command, 126, "", "sudo not found")
        run_argv = (sudo, *command)
    return subprocess.run(run_argv, capture_output=True, text=True, timeout=300)


def _print_plan(commands: list[provider_runtime.RuntimeCommand]) -> None:
    for command in commands:
        prefix = "sudo " if command.requires_privilege else ""
        print(prefix + " ".join(command.argv))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default="betteragent")
    parser.add_argument("--group", default="betteragent")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    user_commands = provider_runtime.plan_isolated_user_commands(args.username, args.group)
    projects = [
        p for p in project_store.list_projects()
        if (p.get("node_id") or "primary") == "primary" and p.get("path")
    ]

    if not args.apply:
        print("user commands:")
        _print_plan(user_commands)
        print("project access commands:")
        for project in projects:
            if not provider_runtime.project_access_allowed(project["path"]):
                print(f"skip broad/sensitive project path: {project['path']}")
                continue
            _print_plan(provider_runtime.plan_project_access_commands(project["path"], args.group))
        print("dry-run only; pass --apply to execute")
        return 0

    provider_runtime.apply_isolated_user(args.username, args.group, runner=_sudo_runner)
    synced = provider_runtime.sync_loaded_project_access(projects)
    print(f"isolated user enabled: {args.username}")
    print(f"project access synced: {synced}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
