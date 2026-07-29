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

import _test_home
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
    "git_api": ("/api/git-",),
    "pending_approvals_api": ("/api/internal/pending-approvals/",),
}


def _app_routes(app) -> list[tuple[str, frozenset[str]]]:
    """Flatten the app's routes.

    FastAPI keeps an included router as a single lazy entry with an
    empty path, exposing the router it wrapped as `original_router`,
    so a router's own routes live one level down and must be walked.
    """
    out: list[tuple[str, frozenset[str]]] = []

    def _walk(routes) -> None:
        for route in routes:
            path = getattr(route, "path", None)
            nested = getattr(route, "original_router", None) or (
                getattr(route, "routes", None) if not path else None
            )
            if not path and nested is not None:
                _walk(getattr(nested, "routes", nested))
                continue
            if not path:
                continue
            methods = getattr(route, "methods", None)
            out.append((path, frozenset(methods or {"WEBSOCKET"}) - {"HEAD"}))

    _walk(app.routes)
    return out


def _served_paths(app) -> set[tuple[str, str]]:
    served = set()
    for path, methods in _app_routes(app):
        for method in methods:
            served.add((method, path))
    return served


def test_no_duplicate_route_registrations(app) -> bool:
    seen: dict[tuple[str, str], int] = {}
    for path, methods in _app_routes(app):
        for method in methods:
            seen[(method, path)] = seen.get((method, path), 0) + 1
    dupes = sorted(k for k, count in seen.items() if count > 1)
    if dupes:
        print(f"{FAIL} duplicate route registrations (a move that left the original behind?): {dupes}")
        return False
    print(f"{PASS} no duplicate (method, path) registrations")
    return True


def test_included_router_routes_are_reachable(app) -> bool:
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
    if missing:
        print(f"{FAIL} router routes declared but not served: {missing}")
        return False
    print(f"{PASS} every extracted router's routes are served by the app")
    return True


def test_extracted_domains_do_not_regrow_in_main() -> bool:
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
    if offenders:
        print(f"{FAIL} main.py re-declares extracted routes: {offenders}")
        return False
    print(f"{PASS} main.py declares no route owned by an extracted router")
    return True


def test_route_paths_are_well_formed(app) -> bool:
    bad = [
        path for path, _ in _app_routes(app)
        if path.startswith("/api") and re.search(r"//|\s", path)
    ]
    if bad:
        print(f"{FAIL} malformed route paths: {bad}")
        return False
    print(f"{PASS} route paths are well formed")
    return True


def main() -> bool:
    import main as main_module

    app = main_module.app
    results = [
        test_no_duplicate_route_registrations(app),
        test_included_router_routes_are_reachable(app),
        test_extracted_domains_do_not_regrow_in_main(),
        test_route_paths_are_well_formed(app),
    ]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} subtests passed")
    return all(results)


if __name__ == "__main__":
    try:
        ok = main()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    raise SystemExit(0 if ok else 1)
