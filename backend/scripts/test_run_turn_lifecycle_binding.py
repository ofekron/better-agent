"""Regression guard for the c096ecd NameError.

c096ecd added a `lifecycle_msg_id` param to `_init_turn_messages` and passed
`lifecycle_msg_id=lifecycle_msg_id` at the call site inside
`Coordinator.run_turn`, but never bound `lifecycle_msg_id` in run_turn's
scope. run_turn has no such parameter and no local assignment, so the name
resolves as a global lookup and raises `NameError` on EVERY native turn
(the tagger singleton and every normal session alike).

The fix sources it from the authoritative store
`self.in_flight_lifecycle_msg_id.get(app_session_id)` — the same store the
prompt processor populates before the turn and `handle_prompt` reads.

This guard fails before the fix (the name is a bare global read, absent from
the code object's locals) and passes after (it becomes a bound local). It is
robust to either fix shape: sourcing it locally OR threading it as a
parameter both put the name in `co_varnames`.
"""
import os
import tempfile

import _test_home
_test_home.isolate("bc_test_lifecycle_")

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from turn_manager import TurnManager  # noqa: E402


def test_run_turn_binds_lifecycle_msg_id() -> None:
    # `run_turn` lives on `TurnManager` post-cutover. Same invariant
    # applies: `lifecycle_msg_id` must be a bound local/arg, not a
    # bare global read.
    code = TurnManager.run_turn.__code__
    assert "lifecycle_msg_id" in code.co_varnames, (
        "run_turn references lifecycle_msg_id but never binds it in scope — "
        "every native turn NameErrors. Source it from "
        "self.in_flight_lifecycle_msg_id.get(app_session_id) (or add it as a "
        "run_turn parameter)."
    )


if __name__ == "__main__":
    test_run_turn_binds_lifecycle_msg_id()
    print("OK: run_turn binds lifecycle_msg_id")
