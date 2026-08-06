"""Structural guards for the route registry as routes migrate out of main.py.

These are invariants, not a frozen snapshot: adding a new route never
fails them, but losing one, double-registering one, or re-growing a
route back into main.py after its domain moved out does.
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import sys

import pytest

import _test_home
from _test_routes import walk_routes
_TMP_HOME = _test_home.isolate("bc-test-route-registry-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

# Domains whose routes have moved into their own router module. Each
# entry maps a router module to the path prefixes it now owns; main.py
# must not declare those paths again.
EXTRACTED_DOMAINS = {
    "misc_api": ("/api/version", "/api/analytics"),
    "ws_chat": ("/ws/chat", "/{_unknown_ws_path:path}"),
    "git_api": ("/api/git-",),
    "pending_approvals_api": ("/api/internal/pending-approvals/",),
    "internal_messaging_api": (
        "/api/internal/mssg",
        "/api/internal/stop-turn",
        "/api/internal/testape/",
        "/api/internal/test/force-context-overflow",
        "/api/internal/credential/",
    ),
    "user_input_api": (
        "/api/user-input/",
        "/api/internal/user-input/",
        "/api/internal/open-file-panel",
        "/api/internal/open-config-panel",
    ),
    "memory_api": ("/api/memory/", "/api/internal/memory/"),
    "schedules_api": ("/api/schedules", "/api/internal/schedules"),
    "tasks_api": (
        "/api/internal/tasks",
        "/api/internal/task-outputs",
        "/api/task-outputs/",
        "/api/task-output/",
    ),
    "session_bridge_api": (
        "/api/internal/session-bridge/",
        "/api/internal/ask-propose",
        "/api/internal/agent-board/",
    ),
    "coordination_api": ("/api/internal/coordination/",),
    "mobile_desktop_api": (
        "/api/installation-profile",
        "/api/download/",
        "/api/mobile/status",
        "/api/desktop/",
    ),
    "workers_api": ("/api/internal/workers/",),
    "worker_pools_api": ("/api/internal/worker-pools/",),
    "credential_ui_api": ("/api/internal/credential-ui/",),
    "tool_approvals_api": ("/api/internal/tool-approvals/",),
}


def _app_routes(app) -> list[tuple[str, frozenset[str]]]:
    return [
        (route.path, frozenset(getattr(route, "methods", None) or {"WEBSOCKET"}) - {"HEAD"})
        for route in walk_routes(app.routes)
    ]


def _served_paths(app) -> set[tuple[str, str]]:
    served = set()
    for path, methods in _app_routes(app):
        for method in methods:
            served.add((method, path))
    return served


@pytest.fixture(scope="module")
def app():
    import main as main_module
    return main_module.app


def test_no_duplicate_route_registrations(app) -> None:
    seen: dict[tuple[str, str], int] = {}
    for path, methods in _app_routes(app):
        for method in methods:
            seen[(method, path)] = seen.get((method, path), 0) + 1
    dupes = sorted(k for k, count in seen.items() if count > 1)
    assert not dupes, f"duplicate route registrations (a move that left the original behind?): {dupes}"


def test_included_router_routes_are_reachable(app) -> None:
    """Every route an extracted router declares must actually be served.

    Catches a router that was written but never included, and a router
    whose include was dropped by a bad merge.
    """
    served = _served_paths(app)
    missing: list[str] = []
    for module_name in sorted(EXTRACTED_DOMAINS):
        module = __import__(module_name)
        for route in module.router.routes:
            for method in (frozenset(getattr(route, "methods", None) or {"WEBSOCKET"}) - {"HEAD"}):
                if (method, route.path) not in served:
                    missing.append(f"{module_name}: {method} {route.path}")
    assert not missing, f"router routes declared but not served: {missing}"


def test_extracted_domains_do_not_regrow_in_main() -> None:
    """main.py must not re-declare a path a router already owns."""
    source = open(os.path.join(_BACKEND, "main.py"), encoding="utf-8").read()
    tree = ast.parse(source)
    declared: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            func = getattr(deco, "func", None)
            if func is None or getattr(func, "attr", None) not in (
                "get", "post", "put", "patch", "delete", "websocket",
            ):
                continue
            if getattr(getattr(func, "value", None), "id", None) != "app":
                continue
            for arg in deco.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    declared.append(arg.value)
    offenders: list[str] = []
    for module_name, prefixes in sorted(EXTRACTED_DOMAINS.items()):
        for path in declared:
            if path.startswith(prefixes):
                offenders.append(f"{path} belongs to {module_name}")
    assert not offenders, f"main.py re-declares extracted routes: {offenders}"


def test_route_paths_are_well_formed(app) -> None:
    bad = [
        path for path, _ in _app_routes(app)
        if path.startswith("/api") and re.search(r"//|\s", path)
    ]
    assert not bad, f"malformed route paths: {bad}"


def main() -> int:
    import main as main_module

    app = main_module.app
    checks = [
        ("no duplicate (method, path) registrations", lambda: test_no_duplicate_route_registrations(app)),
        ("every extracted router's routes are served by the app", lambda: test_included_router_routes_are_reachable(app)),
        ("main.py declares no route owned by an extracted router", test_extracted_domains_do_not_regrow_in_main),
        ("route paths are well formed", lambda: test_route_paths_are_well_formed(app)),
    ]
    passed = 0
    for label, check in checks:
        try:
            check()
            print(f"{PASS} {label}")
            passed += 1
        except AssertionError as exc:
            print(f"{FAIL} {label}: {exc}")
    print(f"\n{passed}/{len(checks)} subtests passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
