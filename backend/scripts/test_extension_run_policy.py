from __future__ import annotations

import copy
import os
import shutil
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-extension-run-policy-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import config_store  # noqa: E402
import harness_profile_resolver  # noqa: E402
import session_store  # noqa: E402
from extension_run_policy import (  # noqa: E402
    disabled_builtin_extensions_for_run,
    resolve_extension_run_policy,
)


def main() -> int:
    ok = True

    def check(label: str, condition: bool) -> None:
        nonlocal ok
        print(("PASS " if condition else "FAIL ") + label)
        ok = ok and condition

    config_store.set_disabled_builtin_extensions(["global.disabled"])
    ordinary = session_store.create_session(
        name="ordinary",
        cwd="/tmp/ordinary-extension-policy",
    )
    check(
        "missing session policy uses global default",
        disabled_builtin_extensions_for_run(
            None,
            session_record={},
            worker_record={},
        ) == ["global.disabled"],
    )
    check(
        "ordinary created session uses global default",
        disabled_builtin_extensions_for_run(
            None,
            session_record=ordinary,
            worker_record={},
        ) == ["global.disabled"],
    )
    check(
        "session policy wins over global default",
        disabled_builtin_extensions_for_run(
            None,
            session_record={"disabled_builtin_extensions": ["session.disabled"]},
            worker_record={},
        ) == ["session.disabled"],
    )
    check(
        "worker policy wins over session policy",
        disabled_builtin_extensions_for_run(
            None,
            session_record={"disabled_builtin_extensions": ["session.disabled"]},
            worker_record={"disabled_builtin_extensions": ["worker.disabled"]},
        ) == ["worker.disabled"],
    )
    check(
        "empty worker policy explicitly clears disabled extensions",
        disabled_builtin_extensions_for_run(
            None,
            session_record={"disabled_builtin_extensions": ["session.disabled"]},
            worker_record={"disabled_builtin_extensions": []},
        ) == [],
    )
    check(
        "explicit turn override wins over worker policy",
        disabled_builtin_extensions_for_run(
            ["turn.disabled"],
            session_record={"disabled_builtin_extensions": ["session.disabled"]},
            worker_record={"disabled_builtin_extensions": ["worker.disabled"]},
        ) == ["turn.disabled"],
    )
    check(
        "explicit empty turn override clears disabled extensions",
        disabled_builtin_extensions_for_run(
            [],
            session_record={"disabled_builtin_extensions": ["session.disabled"]},
            worker_record={"disabled_builtin_extensions": ["worker.disabled"]},
        ) == [],
    )

    resolved_for: list[dict] = []
    original_resolve = harness_profile_resolver.resolve_for_session
    harness_profile_resolver.resolve_for_session = lambda record, **_kwargs: (
        resolved_for.append(record)
        or {
            "profile_id": record.get("harness_profile_id") or "default",
            "bare_config": False,
            "capability_contexts": [],
            "provider_run_config": {
                "mcp_servers": {"profile": {"command": "profile"}},
            },
            "extra_mcp_servers": ["profile-server", "shared-server"],
            "active_capability_ids": [],
            "disabled_builtin_extensions": ["profile.disabled"],
            "disabled_builtin_tools": ["mssg"],
            "disabled_runtime_skills": ["profile.skill"],
            "launcher_projection": {
                "extension_mcp_servers": {},
                "disabled_builtin_extensions": ["profile.disabled"],
                "disabled_builtin_tools": ["mssg"],
                "disabled_runtime_skills": ["profile.skill"],
            },
        }
    )
    try:
        policy = resolve_extension_run_policy(
            resolved_harness_run_config=None,
            session_record={
                "harness_profile_id": "session-profile",
                "extra_mcp_servers": ["session-server"],
                "disabled_builtin_extensions": ["session.disabled"],
                "disabled_builtin_tools": ["ask"],
                "disabled_runtime_skills": ["session.skill"],
            },
            worker_record={
                "harness_profile_id": "worker-profile",
                "extra_mcp_servers": ["worker-server", "shared-server"],
                "disabled_builtin_extensions": ["worker.disabled"],
                "disabled_builtin_tools": ["delegate_task"],
                "disabled_runtime_skills": ["worker.skill"],
            },
            provider_kind="codex",
            provider_run_config={
                "fork_parent_line_count": 17,
                "mcp_servers": {"caller": {"command": "caller"}},
            },
            capability_contexts=[],
            disabled_builtin_extensions=None,
        )
    finally:
        harness_profile_resolver.resolve_for_session = original_resolve

    check(
        "snapshot-free worker run resolves worker profile",
        resolved_for
        == [{
            "harness_profile_id": "worker-profile",
            "extra_mcp_servers": ["worker-server", "shared-server"],
            "disabled_builtin_extensions": ["worker.disabled"],
            "disabled_builtin_tools": ["delegate_task"],
            "disabled_runtime_skills": ["worker.skill"],
        }],
    )
    check(
        "profile MCPs union with worker-first explicit opt-ins",
        policy["extra_mcp_servers"]
        == ["profile-server", "shared-server", "worker-server"],
    )
    check(
        "profile restrictions union with worker restrictions",
        policy["disabled_builtin_extensions"]
        == ["profile.disabled", "worker.disabled"],
    )
    check(
        "profile tool restrictions union with worker and global restrictions",
        {"mssg", "delegate_task"}.issubset(set(policy["disabled_builtin_tools"])),
    )
    check(
        "profile runtime skill restrictions union with session and worker restrictions",
        policy["disabled_runtime_skills"] == ["profile.skill", "session.skill", "worker.skill"],
    )
    check(
        "dynamic provider config survives profile projection",
        policy["provider_run_config"]["fork_parent_line_count"] == 17
        and set(policy["provider_run_config"]["mcp_servers"]) == {"profile", "caller"},
    )
    check(
        "forwarded launcher projection carries effective restrictions",
        policy["resolved_harness_run_config"]["launcher_projection"]
        ["disabled_builtin_extensions"]
        == ["profile.disabled", "worker.disabled"],
    )

    supplied_policy = resolve_extension_run_policy(
        resolved_harness_run_config={
            "profile_id": "capture-profile",
            "bare_config": False,
            "capability_contexts": [],
            "provider_run_config": {},
            "extra_mcp_servers": [],
            "active_capability_ids": [],
            "disabled_builtin_extensions": ["profile.disabled"],
            "disabled_builtin_tools": [],
            "disabled_runtime_skills": [],
            "launcher_projection": {},
        },
        session_record={
            "harness_profile_id": "capture-profile",
            "disabled_builtin_extensions": ["stale.default.disabled"],
        },
        provider_kind="codex",
        provider_run_config={},
        capability_contexts=[],
        disabled_builtin_extensions=["ofek.testape-internal"],
    )
    check(
        "resolved profile preserves explicit turn extension restrictions",
        supplied_policy["disabled_builtin_extensions"]
        == ["profile.disabled", "ofek.testape-internal"],
    )
    check(
        "launcher projection preserves explicit turn extension restrictions",
        supplied_policy["resolved_harness_run_config"]["launcher_projection"]
        ["disabled_builtin_extensions"]
        == ["profile.disabled", "ofek.testape-internal"],
    )
    for invalid_snapshot_value in (None, "profile.disabled"):
        invalid_snapshot = copy.deepcopy(
            supplied_policy["resolved_harness_run_config"]
        )
        if invalid_snapshot_value is None:
            invalid_snapshot.pop("disabled_builtin_extensions", None)
        else:
            invalid_snapshot["disabled_builtin_extensions"] = invalid_snapshot_value
        guarded_policy = resolve_extension_run_policy(
            resolved_harness_run_config=invalid_snapshot,
            session_record={
                "harness_profile_id": "capture-profile",
                "disabled_builtin_extensions": ["durable.disabled"],
            },
            provider_kind="codex",
            provider_run_config={},
            capability_contexts=[],
            disabled_builtin_extensions=["caller.disabled"],
        )
        check(
            f"invalid supplied extension selection fails closed: {invalid_snapshot_value!r}",
            guarded_policy["disabled_builtin_extensions"] == ["caller.disabled"],
        )
    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
