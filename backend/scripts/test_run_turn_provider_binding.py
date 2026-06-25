"""Regression guard: the fork-first-turn branch of run_turn must bind
`provider` before stamping it into set_agent_sid.

The selector-change-continuation work added
`provider_id=provider.id, model=model` to the set_agent_sid call inside
run_turn's fork branch, but `provider` was never assigned in run_turn's
scope (the canonical `provider = self._c.provider_for_session(...)` lives
in the separate `_drive_cli_run` method). So the name was a bare global
read and raised `NameError: name 'provider' is not defined` on every
fork-first turn that produced a new agent sid.

The fix resolves it inline in the fork branch. This guard fails before
the fix (the name is a bare global read, absent from the code object's
locals) and passes after (it becomes a bound local).

Run with:
    cd backend && .venv/bin/python scripts/test_run_turn_provider_binding.py
"""
import os
import sys
from pathlib import Path

import _test_home
_test_home.isolate("bc_test_run_turn_provider_")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from turn_manager import TurnManager  # noqa: E402


def test_run_turn_binds_provider() -> None:
    code = TurnManager.run_turn.__code__
    assert "provider" in code.co_varnames, (
        "run_turn references provider (set_agent_sid in the fork-first-turn "
        "branch) but never binds it in scope — every fork-first turn that "
        "produces a new agent sid NameErrors. Resolve it via "
        "self._c.provider_for_session(app_session_id) in that branch."
    )


if __name__ == "__main__":
    test_run_turn_binds_provider()
    print("OK: run_turn binds provider")
