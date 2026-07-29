from __future__ import annotations

import ast
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "main_node.py"


def test_node_exposes_supervisor_readiness_route() -> None:
    module = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in module.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            if not isinstance(decorator.func, ast.Attribute):
                continue
            if decorator.func.attr != "get":
                continue
            if isinstance(decorator.args[0], ast.Constant):
                if decorator.args[0].value == "/readyz":
                    return
    raise AssertionError("worker node does not expose /readyz")


if __name__ == "__main__":
    test_node_exposes_supervisor_readiness_route()
    print("node readiness route: ok")
