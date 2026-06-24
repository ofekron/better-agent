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


def test_babysitter_linger_kill_free():
    print("T3 babysitter linger has no kill outside the cancel-sentinel path")
    src = _func_source("runner.py", "_linger_for_background_work")
    _check(src is not None, "runner.py defines _linger_for_background_work")
    if src is None:
        return
    # The ONLY kill in the linger loop is the sweep gated on the
    # run-level cancel sentinel (the user's explicit stop). The signal
    # poll itself must never signal a process.
    tree = ast.parse(src)
    kill_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            if "kill" in name.lower() or "terminate" in name.lower():
                kill_calls.append(name)
    _check(kill_calls == ["pc.kill_detached_descendant_groups"],
           f"linger's only kill is the cancel-path sweep ({kill_calls})")
    _check("cancel_path.exists()" in src,
           "the sweep is gated on the run-level cancel sentinel")
    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assigned |= {
        node.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.arg)
    }
    assigned |= {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    loaded = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    allowed_globals = {
        "Exception",
        "OSError",
        "Path",
        "asyncio",
        "float",
        "frozenset",
        "logger",
        "logging",
        "log",
        "os",
        "process_control",
    }
    undefined = loaded - assigned - allowed_globals
    _check("ignore" not in undefined and not undefined,
           f"linger loop has no undefined locals ({sorted(undefined)})")


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
    test_babysitter_linger_kill_free()
    test_non_tty_shutdown_leaves_runners_alive()
    test_interactive_shutdown_requires_explicit_yes()
    test_supervisor_kill_flag_is_explicit_only()
    print(f"\n{'PASS' if not failures else 'FAIL'}: "
          f"{6}/6 groups, {len(failures)} failed checks")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
