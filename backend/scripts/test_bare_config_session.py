"""Behavioral: a session created with `bare_config=True` round-trips the
flag through persistence; the default is False; legacy sessions migrate to
False. Fails before the change (no `bare_config` field) and passes after.

Run with:
    cd backend && .venv/bin/python scripts/test_bare_config_session.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="bc-bare-session-")
    os.environ["BETTER_CLAUDE_HOME"] = tmp
    os.environ["BETTER_AGENT_HOME"] = tmp
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    import session_store

    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + label)
        ok = ok and cond

    bare = session_store.create_session(
        name="bare-one", cwd=tmp, orchestration_mode="manager", bare_config=True,
    )
    plain = session_store.create_session(
        name="plain-one", cwd=tmp, orchestration_mode="manager",
    )

    check("created bare session persists bare_config=True", bare.get("bare_config") is True)
    check("default session has bare_config=False", plain.get("bare_config") is False)
    check("default session has no disabled extension policy", "disabled_builtin_extensions" not in plain)
    policy = session_store.create_session(
        name="policy-one",
        cwd=tmp,
        orchestration_mode="manager",
        disabled_builtin_extensions=["ofek.testape-internal", "ofek.testape-internal"],
    )
    check(
        "created session persists disabled_builtin_extensions",
        policy.get("disabled_builtin_extensions") == ["ofek.testape-internal"],
    )

    # Re-read from disk to prove it survives persistence.
    reloaded = session_store.get_session(bare["id"])
    check("bare_config survives reload", bool(reloaded.get("bare_config")) is True)
    policy_reloaded = session_store.get_session(policy["id"])
    check(
        "disabled_builtin_extensions survives reload",
        policy_reloaded.get("disabled_builtin_extensions") == ["ofek.testape-internal"],
    )

    # Legacy session without the field migrates to False.
    legacy = dict(plain)
    legacy.pop("bare_config", None)
    legacy.pop("disabled_builtin_extensions", None)
    migrated = session_store._migrate_session(legacy)
    check("legacy session migrates bare_config→False", migrated.get("bare_config") is False)
    check("legacy session keeps disabled extension policy unset", "disabled_builtin_extensions" not in migrated)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
