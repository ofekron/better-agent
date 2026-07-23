#!/usr/bin/env python3
from __future__ import annotations

import ast
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _provider_paths() -> list[Path]:
    paths: list[Path] = []
    for path in sorted(BACKEND_DIR.glob("provider_*.py")):
        if path.name == "provider_remote.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.ClassDef)
            and any(isinstance(base, ast.Name) and base.id == "Provider" for base in node.bases)
            for node in tree.body
        ):
            paths.append(path)
    return paths


def _runtime_env_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_better_agent_run_env"
    ]


def main() -> None:
    providers_without_runtime_env: list[str] = []
    calls_without_run_id: list[str] = []

    for path in _provider_paths():
        calls = _runtime_env_calls(path)
        if not calls:
            providers_without_runtime_env.append(path.name)
            continue
        for call in calls:
            keywords = {keyword.arg for keyword in call.keywords}
            if "run_id" not in keywords:
                calls_without_run_id.append(f"{path.name}:{call.lineno}")

    assert not providers_without_runtime_env, providers_without_runtime_env
    assert not calls_without_run_id, calls_without_run_id
    print("provider runtime bootstrap parity: OK")


if __name__ == "__main__":
    main()
