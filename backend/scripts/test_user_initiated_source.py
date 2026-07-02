"""Locks the `user_initiated` taxonomy: a session is `user_initiated`
ONLY when the user is aware of having created it (UI/CLI create, import,
file-edit, a user fork, or a worker approved via the popup). Sessions the
system or an agent spins up on its own (provisioning, agent
create_session / create_sub_session, auto-approved workers, internal
forks) are NOT user-initiated.

This is orthogonal to `source` — an agent's standalone session reuses
source="cli", identical to a real human CLI session, so `source` alone
can never make the distinction.

Run with:
    cd backend && .venv/bin/python scripts/test_user_initiated_source.py
"""

from __future__ import annotations

import os
import sys

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-user-initiated-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import session_store  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"

_ok = True


def check(label: str, condition: bool) -> None:
    global _ok
    print((PASS if condition else FAIL) + " " + label)
    _ok = _ok and condition


def _mig(record: dict) -> dict:
    return session_store._migrate_session(
        dict(record), {"dirty": [False], "providers": {}}
    )


def _summary(sid: str) -> dict:
    return next(
        (s for s in session_store.list_sessions() if s.get("id") == sid), {}
    )


def test_create_defaults_fail_closed() -> None:
    """A caller that forgets `user_initiated` gets False — a hidden helper
    must never leak into user-facing surfaces by omission."""
    agent = session_manager.create(
        name="agent-standalone", cwd="/tmp/ui-a",
        orchestration_mode="native", source="cli",
    )
    check("default create is NOT user_initiated", agent.get("user_initiated") is False)
    check(
        "default create summary is NOT user_initiated",
        _summary(agent["id"]).get("user_initiated") is False,
    )


def test_explicit_user_initiated() -> None:
    """UI/CLI POST /api/sessions passes user_initiated=True."""
    ui = session_manager.create(
        name="ui-session", cwd="/tmp/ui-b",
        orchestration_mode="native", source="web", user_initiated=True,
    )
    check("explicit user create is user_initiated", ui.get("user_initiated") is True)
    check(
        "user create summary is user_initiated",
        _summary(ui["id"]).get("user_initiated") is True,
    )


def test_sub_session_not_user_initiated() -> None:
    parent = session_manager.create(
        name="parent", cwd="/tmp/ui-c",
        orchestration_mode="native", source="web", user_initiated=True,
    )
    sub = session_manager.create_sub_session(
        parent_session_id=parent["id"],
        name="hidden-sub",
        disallowed_tools=["Bash", "Bash"],
        disabled_builtin_extensions=["ofek-dev.ask", "ofek-dev.ask"],
    )
    check("sub_session is NOT user_initiated", sub.get("user_initiated") is False)
    check("sub_session stores disallowed tools", sub.get("disallowed_tools") == ["Bash"])
    check(
        "sub_session stores disabled extensions",
        sub.get("disabled_builtin_extensions") == ["ofek-dev.ask"],
    )


def test_user_fork_inherits_and_internal_fork_forced_false() -> None:
    parent = session_manager.create(
        name="forkable", cwd="/tmp/ui-d",
        orchestration_mode="native", source="web", user_initiated=True,
    )
    session_manager.set_agent_sid(parent["id"], "native", "native_sid_d")

    user_fork = session_manager.fork(parent["id"], name="user-fork")
    check("user fork inherits user_initiated=True", user_fork.get("user_initiated") is True)

    bridge_fork = session_manager.fork(
        parent["id"], name="bridge-fork", user_initiated=False,
    )
    check(
        "agent-requested fork from user session can be forced NOT user_initiated",
        bridge_fork.get("user_initiated") is False,
    )

    adv_fork = session_manager.fork(
        parent["id"], name="adv-fork", kind="adv_sync_fork",
    )
    check(
        "adv_sync fork is forced NOT user_initiated",
        adv_fork.get("user_initiated") is False,
    )

    agent = session_manager.create(
        name="agent-forkable", cwd="/tmp/ui-e",
        orchestration_mode="native", source="cli",
    )
    session_manager.set_agent_sid(agent["id"], "native", "native_sid_e")
    agent_fork = session_manager.fork(agent["id"], name="agent-fork")
    check(
        "fork of a non-user session stays NOT user_initiated",
        agent_fork.get("user_initiated") is False,
    )


def test_migration_backfill() -> None:
    cases = [
        ({"id": "m1", "_schema_version": 10, "source": "web", "kind": "user"},
         True, "legacy human web root"),
        ({"id": "m2", "_schema_version": 10, "source": "cli", "kind": "user"},
         True, "legacy human cli root"),
        ({"id": "m3", "_schema_version": 10, "source": "import", "kind": "user"},
         True, "legacy imported root"),
        ({"id": "m4", "_schema_version": 10, "source": "internal", "kind": "user"},
         False, "legacy provisioned (source=internal)"),
        ({"id": "m5", "_schema_version": 10, "source": "extension", "kind": "user"},
         False, "legacy extension-created"),
        ({"id": "m5b", "_schema_version": 10, "source": "internal", "kind": "user"},
         False, "legacy internal helper"),
        ({"id": "m6", "_schema_version": 10, "source": "web", "kind": "delegate_fork"},
         False, "legacy delegate_fork"),
        ({"id": "m6b", "_schema_version": 10, "source": "web", "is_delegate_fork": True},
         False, "legacy is_delegate_fork"),
        ({"id": "m7", "_schema_version": 10, "source": "cli", "kind": "sub_session"},
         False, "legacy sub_session"),
        ({"id": "m8", "_schema_version": 10, "source": "web", "kind": "adv_sync_fork"},
         False, "legacy adv_sync_fork"),
        ({"id": "m9", "_schema_version": 10, "source": "web", "kind": "supervisor_worker"},
         False, "legacy supervisor_worker"),
        ({"id": "m10", "_schema_version": 10, "source": "web", "kind": "user",
          "working_mode": "search_worker"}, False, "legacy search worker"),
        ({"id": "m11", "_schema_version": 10, "source": "web", "kind": "user",
          "user_initiated": False}, False, "explicit False preserved"),
        ({"id": "m12", "_schema_version": 10, "source": "internal", "kind": "user",
          "user_initiated": True}, True, "explicit True preserved over source"),
    ]
    for record, expected, label in cases:
        migrated = _mig(record)
        check(
            f"migration: {label}",
            migrated.get("user_initiated") is expected,
        )


def main() -> None:
    test_create_defaults_fail_closed()
    test_explicit_user_initiated()
    test_sub_session_not_user_initiated()
    test_user_fork_inherits_and_internal_fork_forced_false()
    test_migration_backfill()

    internal = session_store.create_session(
        name="internal-helper", cwd="/tmp/ui-f", source="internal",
    )
    extension = session_store.create_session(
        name="extension-helper", cwd="/tmp/ui-g", source="extension",
    )
    check("internal source is preserved", internal.get("source") == "internal")
    check("extension source is preserved", extension.get("source") == "extension")
    check("internal source defaults NOT user_initiated", internal.get("user_initiated") is False)
    check("extension source defaults NOT user_initiated", extension.get("user_initiated") is False)

    if not _ok:
        print("FAILURES")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
