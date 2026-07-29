from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from provider_runtime_bootstrap_ast import (
    MethodNode,
    class_index,
    method_scope,
)


@dataclass(frozen=True)
class ProviderTarget:
    kind: str
    module: str
    cls: str


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _runtime_env_calls(method: MethodNode) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(method):
        if (
            isinstance(node, ast.Call)
            and _call_name(node) == "build_better_agent_run_env"
        ):
            calls.append(node)
    return calls


def _contains_call(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(child, ast.Call) and _call_name(child) == name
        for child in ast.walk(node)
    )


def _call_arguments(call: ast.Call) -> list[ast.AST]:
    return [*call.args, *(keyword.value for keyword in call.keywords)]


def _runner_launch_calls(method: MethodNode) -> list[ast.Call]:
    launches: list[ast.Call] = []
    for node in ast.walk(method):
        if not isinstance(node, ast.Call) or _call_name(node) == "runner_argv":
            continue
        arguments = _call_arguments(node)
        argv_names = _runner_argv_names_before(method, node)
        pinned_names = _enclosing_pinned_runner_names(method, node)
        if any(
            _contains_call(argument, "runner_argv")
            or any(
                isinstance(child, ast.Name) and child.id in argv_names
                for child in ast.walk(argument)
            )
            or any(
                isinstance(child, ast.Attribute)
                and child.attr == "argv"
                and isinstance(child.value, ast.Name)
                and child.value.id in pinned_names
                for child in ast.walk(argument)
            )
            for argument in arguments
        ):
            launches.append(node)
    return [
        launch
        for launch in launches
        if not any(
            id(launch) != id(outer)
            and any(
                id(child) == id(launch)
                for argument in _call_arguments(outer)
                for child in ast.walk(argument)
            )
            for outer in launches
        )
    ]


def _enclosing_pinned_runner_names(
    method: MethodNode,
    launch: ast.Call,
) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(method):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        if not any(
            id(child) == id(launch)
            for statement in node.body
            for child in ast.walk(statement)
        ):
            continue
        for item in node.items:
            if (
                isinstance(item.context_expr, ast.Call)
                and _call_name(item.context_expr) == "open_runner"
                and isinstance(item.optional_vars, ast.Name)
            ):
                names.add(item.optional_vars.id)
    return names


def _statement_path(
    root: ast.AST,
    target: ast.AST,
) -> list[tuple[list[ast.stmt], int]]:
    for _field, value in ast.iter_fields(root):
        if not isinstance(value, list):
            continue
        statements = [item for item in value if isinstance(item, ast.stmt)]
        for index, statement in enumerate(statements):
            if not any(id(child) == id(target) for child in ast.walk(statement)):
                continue
            return [
                (statements, index),
                *_statement_path(statement, target),
            ]
    return []


def _preceding_statements(
    method: MethodNode,
    target: ast.AST,
) -> list[ast.stmt]:
    return [
        statement
        for statements, target_index in _statement_path(method, target)
        for statement in statements[:target_index]
    ]


def _runner_argv_names_before(
    method: MethodNode,
    launch: ast.Call,
) -> set[str]:
    trusted: set[str] = set()
    for statement in _preceding_statements(method, launch):
        if isinstance(statement, ast.Assign):
            value_is_trusted = _contains_call(
                statement.value,
                "runner_argv",
            ) or (
                isinstance(statement.value, ast.Name)
                and statement.value.id in trusted
            )
            for target in statement.targets:
                if not isinstance(target, ast.Name):
                    continue
                if value_is_trusted:
                    trusted.add(target.id)
                else:
                    trusted.discard(target.id)
            continue
        if not isinstance(statement, ast.AnnAssign) or not isinstance(
            statement.target,
            ast.Name,
        ):
            continue
        value = statement.value
        value_is_trusted = value is not None and (
            _contains_call(value, "runner_argv")
            or (isinstance(value, ast.Name) and value.id in trusted)
        )
        if value_is_trusted:
            trusted.add(statement.target.id)
        else:
            trusted.discard(statement.target.id)
    return trusted


def _runtime_env_names_before(
    method: MethodNode,
    launch: ast.Call,
    runtime_calls: list[ast.Call],
) -> set[str]:
    call_ids = {id(call) for call in runtime_calls}

    def is_trusted_value(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and id(node) in call_ids
        ) or (
            isinstance(node, ast.Name)
            and node.id in trusted
        )

    trusted: set[str] = set()
    for statement in _preceding_statements(method, launch):
        if isinstance(statement, ast.Assign):
            value_is_trusted = is_trusted_value(statement.value)
            for target in statement.targets:
                if not isinstance(target, ast.Name):
                    continue
                if value_is_trusted:
                    trusted.add(target.id)
                else:
                    trusted.discard(target.id)
            continue
        if isinstance(statement, ast.AnnAssign) and isinstance(
            statement.target,
            ast.Name,
        ):
            value = statement.value
            value_is_trusted = value is not None and is_trusted_value(value)
            if value_is_trusted:
                trusted.add(statement.target.id)
            else:
                trusted.discard(statement.target.id)
            continue
        if not isinstance(statement, ast.Expr) or not isinstance(
            statement.value,
            ast.Call,
        ):
            continue
        call = statement.value
        if not isinstance(call.func, ast.Attribute) or not isinstance(
            call.func.value,
            ast.Name,
        ):
            continue
        target = call.func.value.id
        if call.func.attr == "clear":
            trusted.discard(target)
            continue
        if call.func.attr != "update":
            continue
        if (
            len(call.args) == 1
            and not call.keywords
            and is_trusted_value(call.args[0])
        ):
            trusted.add(target)
        else:
            trusted.discard(target)
    return trusted


def _launch_uses_runtime_env(
    method: MethodNode,
    launch: ast.Call,
    runtime_calls: list[ast.Call],
) -> bool:
    env = next(
        (keyword.value for keyword in launch.keywords if keyword.arg == "env"),
        None,
    )
    if env is None:
        return False
    runtime_names = _runtime_env_names_before(method, launch, runtime_calls)
    if isinstance(env, ast.Name) and env.id in runtime_names:
        return True
    call_ids = {id(call) for call in runtime_calls}
    return any(
        isinstance(child, ast.Call) and id(child) in call_ids
        for child in ast.walk(env)
    )


def targets_from_specs(specs: dict[str, Any]) -> list[ProviderTarget]:
    return [
        ProviderTarget(kind, spec.module, spec.cls)
        for kind, spec in specs.items()
        if spec.runner_module is not None
    ]


def manifest_targets() -> list[ProviderTarget]:
    import provider_manifest

    return targets_from_specs(provider_manifest.SPECS)


def audit_targets(
    backend_dir: Path,
    targets: list[ProviderTarget],
) -> tuple[list[str], list[str], list[str]]:
    index = class_index(backend_dir)
    missing_launchers: list[str] = []
    missing_runtime_env: list[str] = []
    calls_without_run_id: list[str] = []
    for target in targets:
        scope = method_scope(
            index,
            (target.module, target.cls),
            "_start_run",
        )
        if not scope:
            missing_launchers.append(
                f"{target.kind}:{target.module}.{target.cls}"
            )
            continue
        found_launch = False
        for owner, scoped_method in scope:
            launches = _runner_launch_calls(scoped_method)
            if not launches:
                continue
            found_launch = True
            runtime_calls = _runtime_env_calls(scoped_method)
            for call in runtime_calls:
                keywords = {keyword.arg for keyword in call.keywords}
                if "run_id" not in keywords:
                    calls_without_run_id.append(f"{owner[0]}.py:{call.lineno}")
            for launch in launches:
                if not _launch_uses_runtime_env(
                    scoped_method,
                    launch,
                    runtime_calls,
                ):
                    missing_runtime_env.append(
                        f"{owner[0]}.py:{launch.lineno}"
                    )
        if not found_launch:
            missing_launchers.append(
                f"{target.kind}:{target.module}.{target.cls}"
            )
    return (
        sorted(set(missing_launchers)),
        sorted(set(missing_runtime_env)),
        sorted(set(calls_without_run_id)),
    )
