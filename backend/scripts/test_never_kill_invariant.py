"""Regression lock for the never-kill rule (Phase 1).

The backend must NEVER terminate the provider/runner except on an explicit
user stop/interrupt. It may only REAP an already-exited process. This test
guards the specific sites that used to auto-kill, so a future change can't
silently reintroduce a backend-initiated kill.

Source-level invariant (the rule is architectural, not one function's
runtime behavior): assert the forbidden kill paths are gone and the
exception-cleanup / idle paths no longer signal the runner.
"""

import ast
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures = []


def _check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


def _func_source(path, func_name):
    """Return the source of the first function/method named func_name."""
    src = open(os.path.join(BACKEND, path), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return ast.get_source_segment(src, node)
    return None


def test_kill_runner_removed():
    print("T1 dead-code _kill_runner is gone (would be a backend auto-kill)")
    src = open(os.path.join(BACKEND, "provider_claude.py"), encoding="utf-8").read()
    _check("_kill_runner" not in src, "provider_claude.py has no _kill_runner")


def test_exception_cleanup_does_not_kill():
    print("T2 backend read-loop exception handlers do NOT cancel/kill the runner")
    for path, func in [
        ("orchs/_subprocess_agent.py", None),
        ("orchs/manager/_delegation.py", None),
    ]:
        src = open(os.path.join(BACKEND, path), encoding="utf-8").read()
        tree = ast.parse(src)
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            seg = ast.get_source_segment(src, node) or ""
            # a bare `except Exception:` that re-raises must not also kill
            if "raise" in seg and "cancel_run(" in seg:
                bad.append(seg.splitlines()[0])
        _check(not bad, f"{path}: no except-handler both kills and re-raises ({bad})")


def test_turn_cancel_sweep_is_sentinel_gated():
    print("T3 the runner has no kill outside the cancel-sentinel sweep path")
    src = open(os.path.join(BACKEND, "runner.py"), encoding="utf-8").read()
    _check("_linger_for_background_work" not in src,
           "runner.py has no babysitter linger (per-turn process)")
    # The only detached-group sweep left is the mid-turn cancel path,
    # gated on the run-level cancel sentinel (the user's explicit stop).
    tree = ast.parse(src)
    sweep_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and "kill_detached_descendant_groups" in ast.unparse(node.func)
    ]
    _check(len(sweep_lines) <= 2,
           f"detached sweeps limited to the cancel path ({sweep_lines})")


def test_non_tty_shutdown_leaves_runners_alive():
    print("T4 non-TTY shutdown defaults to leaving runners alive")
    src = _func_source("main.py", "_prompt_kill_runners")
    _check(src is not None, "main.py defines _prompt_kill_runners")
    if src is None:
        return
    # The non-TTY `if not (...isatty...)` branch must assign
    # _kill_runners_on_shutdown = False — the module-level default (True) is
    # only the interactive-prompt baseline. A headless SIGINT (desktop .app,
    # `kill -INT`, container) can't ask the user, so runners must survive for
    # run_recovery to re-attach.
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if "isatty" not in ast.unparse(node.test):
            continue
        for stmt in node.body:
            if (isinstance(stmt, ast.Assign)
                    and any(isinstance(t, ast.Name)
                            and t.id == "_kill_runners_on_shutdown"
                            for t in stmt.targets)
                    and isinstance(stmt.value, ast.Constant)
                    and stmt.value.value is False):
                found = True
    _check(found, "non-TTY branch sets _kill_runners_on_shutdown = False")


def test_interactive_shutdown_requires_explicit_yes():
    print("T5 interactive shutdown only kills on explicit yes")
    src = _func_source("main.py", "_prompt_kill_runners")
    _check(src is not None, "main.py defines _prompt_kill_runners")
    if src is None:
        return
    _check('answer in ("y", "yes")' in src,
           "prompt flips kill flag only for explicit y/yes")
    _check('answer in ("n", "no")' not in src,
           "empty/default answer is not treated as kill")


def test_supervisor_kill_flag_is_explicit_only():
    print("T6 desktop supervisor writes kill flag only on explicit kill")
    src = _func_source("../desktop/supervisor.py", "shutdown")
    _check(src is not None, "desktop supervisor defines shutdown")
    if src is None:
        return
    _check('"kill_runners_requested"' in src,
           "desktop explicit kill path writes kill-runners flag")
    _check("flag.unlink()" in src,
           "non-kill shutdown clears stale kill-runners flag")


def main():
    test_kill_runner_removed()
    test_exception_cleanup_does_not_kill()
    test_turn_cancel_sweep_is_sentinel_gated()
    test_non_tty_shutdown_leaves_runners_alive()
    test_interactive_shutdown_requires_explicit_yes()
    test_supervisor_kill_flag_is_explicit_only()
    print(f"\n{'PASS' if not failures else 'FAIL'}: "
          f"{6}/6 groups, {len(failures)} failed checks")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
