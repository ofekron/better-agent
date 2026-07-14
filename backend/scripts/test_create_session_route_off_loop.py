from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _is_awaited_to_thread(node: ast.AST) -> bool:
    if not isinstance(node, ast.Await):
        return False
    call = node.value
    return isinstance(call, ast.Call) and _call_name(call.func) == "asyncio.to_thread"


def _find_create_session(tree: ast.Module) -> ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "create_session":
            return node
    raise AssertionError("create_session endpoint not found")


def test_create_session_route_keeps_disk_work_off_loop() -> None:
    tree = ast.parse(MAIN.read_text())
    endpoint = _find_create_session(tree)
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(endpoint):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    required_to_thread_args = {
        "session_manager.create": False,
    }
    violations: list[str] = []
    for node in ast.walk(endpoint):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node.func) == "asyncio.to_thread" and node.args:
            arg_name = _call_name(node.args[0])
            if arg_name in required_to_thread_args:
                required_to_thread_args[arg_name] = True
            continue
        name = _call_name(node.func)
        if name not in required_to_thread_args:
            continue
        violations.append(name)

    missing = [name for name, seen in required_to_thread_args.items() if not seen]
    if violations or missing:
        raise AssertionError(
            f"create_session must offload disk-heavy calls with asyncio.to_thread; "
            f"violations={violations} missing={missing}"
        )


if __name__ == "__main__":
    test_create_session_route_keeps_disk_work_off_loop()
    print("PASS create_session route keeps disk work off loop")
