#!/usr/bin/env python3
"""Locks the runtime-broker env contract for brokered extension MCPs.

A live turn's structural plan is built in the backend process, where no
per-run operation broker exists yet. Snapshotting the backend's own env into
`BETTER_CLAUDE_RUNTIME_BROKER` froze an explicit empty broker into every
brokered server/launcher env — overriding process-env inheritance — so each
tools/call failed with "Better Agent runtime broker is unavailable". This
locks the fix: plan-time configs carry `RUNNER_OPERATION_BROKER_REF`, and
`hydrate_runner_operation_broker` swaps in the real address runner-side.

Run with:
    cd backend && .venv/bin/python scripts/test_live_turn_brokered_mcp_env.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
_REPO = _BACKEND.parent
sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(_REPO / "sdk"))

import paths  # noqa: E402  (must engage the test home before other imports)

_HOME = Path(tempfile.mkdtemp(prefix="bc-brokered-mcp-env-"))
paths.engage_test_home(str(_HOME))

OK = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

EXTENSION_ID = "ofek-dev.coordination"
SERVER_NAME = "ofek-dev-coordination"
SERVER_ID = "better-agent-coordination"
BROKER_ADDRESS = "unix:/tmp/bc-test-broker.sock"


def _live_turn_inputs() -> dict:
    return {
        "app_session_id": "live-sid",
        "backend_url": "http://127.0.0.1:9",
        "internal_token": "",
        "cwd": "/tmp",
        "user_facing": True,
        "bare_config": False,
        "extension_mcp_launcher_context": True,
        "extra_mcp_servers": [SERVER_NAME],
    }


def _install_and_grant() -> None:
    import _test_installation
    import extension_store

    _test_installation.activate(_HOME, provider="claude")
    package_dir = _REPO / "extensions" / "coordination"
    extension_store._install_from_package_dir(
        package_dir=package_dir,
        source={"kind": "path", "path": str(package_dir)},
        force_enabled=True,
        persist=True,
    )
    extension_store.grant_native_mcp_server(EXTENSION_ID, SERVER_ID, "global")


def _coordination_env(configs: dict) -> dict:
    for name, config in configs.items():
        if "coordination" in name:
            return dict(config.get("env") or {})
    raise AssertionError(f"coordination server missing from configs: {sorted(configs)}")


def test_plan_time_configs_carry_broker_ref() -> bool:
    """Both deliveries must stamp the hydration placeholder, not the backend's
    (empty) env, when the plan source declares runtime_broker."""
    import extension_store
    from provider_runtime_plan_hydration import RUNNER_OPERATION_BROKER_REF

    inputs = {**_live_turn_inputs(), "runtime_broker": RUNNER_OPERATION_BROKER_REF}
    results = []
    for label, fn in (
        ("native", extension_store.native_mcp_server_configs),
        ("launcher", extension_store.native_mcp_launcher_server_configs),
    ):
        env = _coordination_env(fn(inputs, user_facing=True, bare=False))
        value = env.get("BETTER_CLAUDE_RUNTIME_BROKER")
        ok = value == RUNNER_OPERATION_BROKER_REF
        print(f"{OK if ok else FAIL} {label} delivery stamps broker hydration ref "
              f"(got {value!r})")
        results.append(ok)
    return all(results)


def test_structural_plan_hydrates_to_real_address() -> bool:
    """The claude structural plan carries the ref end-to-end and the runner-side
    hydration resolves it to the actual per-run address."""
    from provider_runtime_plan_hydration import RUNNER_OPERATION_BROKER_REF
    from provider_runtime_plan_source import (
        hydrate_runner_operation_broker,
        structural_provider_runtime_plan,
    )

    plan = structural_provider_runtime_plan(_live_turn_inputs(), "claude")["resolved_plan"]
    servers = {
        server["name"]: server
        for server in plan.get("mcp_servers") or []
        if isinstance(server, dict)
    }
    entry = next(
        (server for name, server in servers.items() if "coordination" in name), None
    )
    if entry is None:
        print(f"{FAIL} coordination server missing from structural plan "
              f"(servers={sorted(servers)})")
        return False
    frozen_envs = {
        delivery: dict(config.get("env") or {})
        for delivery, config in (entry.get("config") or {}).items()
    }
    ok_frozen = all(
        env.get("BETTER_CLAUDE_RUNTIME_BROKER") == RUNNER_OPERATION_BROKER_REF
        for env in frozen_envs.values()
    )
    print(f"{OK if ok_frozen else FAIL} frozen plan deliveries carry the broker ref "
          f"({ {d: e.get('BETTER_CLAUDE_RUNTIME_BROKER') for d, e in frozen_envs.items()} })")

    hydrated = hydrate_runner_operation_broker(plan, BROKER_ADDRESS)
    hydrated_entry = next(
        server
        for server in hydrated["mcp_servers"]
        if "coordination" in server["name"]
    )
    hydrated_envs = {
        delivery: dict(config.get("env") or {})
        for delivery, config in hydrated_entry["config"].items()
    }
    ok_hydrated = all(
        env.get("BETTER_CLAUDE_RUNTIME_BROKER") == BROKER_ADDRESS
        and env.get("BETTER_AGENT_RUNTIME_BROKER") == BROKER_ADDRESS
        for env in hydrated_envs.values()
    )
    print(f"{OK if ok_hydrated else FAIL} hydration swaps the ref for the real "
          f"address in every delivery env")
    return ok_frozen and ok_hydrated


def test_runtime_env_address_wins_over_ref() -> bool:
    """In runtime contexts (runner rebuilds, the launcher stub) the process env
    carries the real address; it must win even when inputs still carry the ref."""
    import extension_store
    from provider_runtime_plan_hydration import RUNNER_OPERATION_BROKER_REF

    inputs = {**_live_turn_inputs(), "runtime_broker": RUNNER_OPERATION_BROKER_REF}
    os.environ["BETTER_CLAUDE_RUNTIME_BROKER"] = BROKER_ADDRESS
    try:
        env = _coordination_env(
            extension_store.native_mcp_server_configs(inputs, user_facing=True, bare=False)
        )
    finally:
        os.environ.pop("BETTER_CLAUDE_RUNTIME_BROKER", None)
    ok = env.get("BETTER_CLAUDE_RUNTIME_BROKER") == BROKER_ADDRESS
    print(f"{OK if ok else FAIL} process-env broker address wins over the ref "
          f"(got {env.get('BETTER_CLAUDE_RUNTIME_BROKER')!r})")
    return ok


if __name__ == "__main__":
    try:
        _install_and_grant()
        results = [
            test_plan_time_configs_carry_broker_ref(),
            test_structural_plan_hydrates_to_real_address(),
            test_runtime_env_address_wins_over_ref(),
        ]
        print(f"\n{sum(1 for r in results if r)}/{len(results)} brokered MCP env tests passed")
        success = all(results)
    finally:
        shutil.rmtree(_HOME, ignore_errors=True)
    raise SystemExit(0 if success else 1)
