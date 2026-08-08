"""Regression: `_drive_cli_run`'s mark-sent site must use the
`lifecycle_message_id` parameter it was called with, never re-derive it
from the mutable per-session `in_flight_lifecycle_msg_id` slot.

Bug (session 0281e1e7-0a80-4e35-ba35-1b6b0f8e7568): `TurnManager.
run_turn` computes `owning_lifecycle_message_id` (the turn's own
authoritative lifecycle id — the same value `_init_turn_messages`
persisted the user message under) and passes it into
`_drive_cli_run(..., lifecycle_message_id=owning_lifecycle_message_id)`.
But `_drive_cli_run`'s mark-sent block ignored that parameter and instead
called `user_prompt_manager.get_in_flight_lifecycle_msg_id(app_session_id)`
— a fresh read of a mutable, single-slot-per-session dict — assigning
the result to a differently-named local (`lifecycle_msg_id`) and using
THAT to call `mark_sent`. That slot can already hold a different value
by the time provider spawn completes (anything else touching the
session between turn start and spawn completion, including the next
queued turn's own dispatch, overwrites it), so `mark_sent` could record
the wrong id — leaving THIS turn's own `lifecycle_message_id` never
marked sent even though its provider genuinely started (a `turn_start`
event with `source=provider_stream` already fired for it). On a later
cancel/crash, `UserPromptManager.emit_user_msg_cancel_terminal` checks
`was_sent(lifecycle_msg_id)`, found it false, and reported
`failed(reason="aborted_before_send")` for a turn that had, in fact,
been sent — leaving the assistant message frozen at
`isStreaming: true` forever (no terminal event ever reached the
frontend).

Fix: use the `lifecycle_message_id` parameter directly; delete the
mutable-slot re-fetch entirely.

This is a static-source AST guard, not a live integration test —
reproducing the exact interleaving window (a second turn's dispatch
landing between this turn's provider-spawn completion and its mark-sent
call) deterministically, without sleep-based timing, would require
invasive mocking of the CLI subprocess spawn machinery. The AST guard
instead locks the actual defect class directly at its source: the
mark-sent block inside `_drive_cli_run` must reference the
`lifecycle_message_id` parameter, and must not call
`get_in_flight_lifecycle_msg_id` anywhere in its body.

Pre-fix this test FAILS (the mark-sent block calls
`get_in_flight_lifecycle_msg_id` and never references the
`lifecycle_message_id` parameter). Post-fix it passes.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_turn_manager_lifecycle_msg_id_scoping.py -q
    PYTHONPATH=. python3 backend/scripts/test_turn_manager_lifecycle_msg_id_scoping.py   # __main__ fallback
"""
from __future__ import annotations

import ast
from pathlib import Path

_TURN_MANAGER_PATH = Path(__file__).resolve().parent.parent / "turn_manager.py"


def _find_method(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"TurnManager.{name} not found — has it been renamed?")


def _param_names(func: ast.AsyncFunctionDef) -> set[str]:
    args = func.args
    names = {a.arg for a in args.args}
    names |= {a.arg for a in args.kwonlyargs}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _calls_get_in_flight_lifecycle_msg_id(func: ast.AsyncFunctionDef) -> list[int]:
    """Line numbers of any `get_in_flight_lifecycle_msg_id(...)` call
    anywhere in `func`'s body (including nested blocks)."""
    hits: list[int] = []
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_in_flight_lifecycle_msg_id"
        ):
            hits.append(node.lineno)
    return hits


def _references_name(func: ast.AsyncFunctionDef, name: str) -> bool:
    return any(
        isinstance(node, ast.Name) and node.id == name for node in ast.walk(func)
    )


def test_drive_cli_run_receives_lifecycle_message_id_parameter() -> None:
    """Sanity: the parameter this fix relies on must still exist and
    still be threaded in from run_turn's authoritative id."""
    tree = ast.parse(_TURN_MANAGER_PATH.read_text(), filename=str(_TURN_MANAGER_PATH))
    drive = _find_method(tree, "_drive_cli_run")
    assert "lifecycle_message_id" in _param_names(drive), (
        "_drive_cli_run must accept a lifecycle_message_id parameter — "
        "has the signature changed?"
    )

    run_turn = _find_method(tree, "run_turn")
    call_hits = [
        node
        for node in ast.walk(run_turn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_drive_cli_run"
    ]
    assert call_hits, "run_turn must call self._drive_cli_run(...)"
    passed_owning_id = any(
        kw.arg == "lifecycle_message_id"
        and isinstance(kw.value, ast.Name)
        and kw.value.id == "owning_lifecycle_message_id"
        for call in call_hits
        for kw in call.keywords
    )
    assert passed_owning_id, (
        "run_turn must call _drive_cli_run(lifecycle_message_id="
        "owning_lifecycle_message_id, ...) — the turn's own authoritative "
        "lifecycle id, not a value re-derived from mutable session state"
    )


def test_drive_cli_run_does_not_refetch_in_flight_lifecycle_msg_id() -> None:
    """The actual regression lock: the mark-sent site must not re-derive
    the lifecycle id from the mutable in-flight slot."""
    tree = ast.parse(_TURN_MANAGER_PATH.read_text(), filename=str(_TURN_MANAGER_PATH))
    drive = _find_method(tree, "_drive_cli_run")

    call_hits = _calls_get_in_flight_lifecycle_msg_id(drive)
    assert not call_hits, (
        "_drive_cli_run must not call get_in_flight_lifecycle_msg_id — "
        f"found call(s) at line(s) {call_hits}, re-deriving the mark-sent "
        "id from mutable per-session state instead of using the "
        "lifecycle_message_id parameter this reintroduces the "
        "aborted_before_send misreport bug"
    )
    assert _references_name(drive, "lifecycle_message_id"), (
        "_drive_cli_run must actually use its lifecycle_message_id "
        "parameter somewhere in its body (e.g. the mark-sent block) — "
        "an unused parameter would mean the fix regressed silently"
    )


def _run_standalone() -> None:
    test_drive_cli_run_receives_lifecycle_message_id_parameter()
    test_drive_cli_run_does_not_refetch_in_flight_lifecycle_msg_id()
    print(
        "OK: _drive_cli_run receives + uses lifecycle_message_id; "
        "no mutable-slot re-fetch at the mark-sent site"
    )


if __name__ == "__main__":
    _run_standalone()
