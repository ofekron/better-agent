from __future__ import annotations

import ast
from pathlib import Path


MethodNode = ast.FunctionDef | ast.AsyncFunctionDef
ClassKey = tuple[str, str]
ClassInfo = tuple[dict[str, MethodNode], tuple[ClassKey, ...]]


def _resolve_base(
    base: ast.expr,
    *,
    module: str,
    imported_symbols: dict[str, ClassKey],
    imported_modules: dict[str, str],
) -> ClassKey | None:
    if isinstance(base, ast.Name):
        return imported_symbols.get(base.id, (module, base.id))
    if (
        isinstance(base, ast.Attribute)
        and isinstance(base.value, ast.Name)
        and base.value.id in imported_modules
    ):
        return imported_modules[base.value.id], base.attr
    return None


def class_index(backend_dir: Path) -> dict[ClassKey, ClassInfo]:
    index: dict[ClassKey, ClassInfo] = {}
    for path in sorted(backend_dir.glob("provider*.py")):
        module = path.stem
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_symbols: dict[str, ClassKey] = {}
        imported_modules: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    imported_symbols[alias.asname or alias.name] = (
                        node.module,
                        alias.name,
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules[alias.asname or alias.name] = alias.name
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            methods = {
                child.name: child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            bases = tuple(
                resolved
                for base in node.bases
                if (
                    resolved := _resolve_base(
                        base,
                        module=module,
                        imported_symbols=imported_symbols,
                        imported_modules=imported_modules,
                    )
                )
                is not None
            )
            index[(module, node.name)] = methods, bases
    return index


def _is_abstract(method: MethodNode) -> bool:
    return any(
        (
            isinstance(decorator, ast.Name)
            and decorator.id == "abstractmethod"
        )
        or (
            isinstance(decorator, ast.Attribute)
            and decorator.attr == "abstractmethod"
        )
        for decorator in method.decorator_list
    )


def _c3_mro(
    index: dict[ClassKey, ClassInfo],
    key: ClassKey,
    stack: frozenset[ClassKey] = frozenset(),
) -> tuple[ClassKey, ...] | None:
    if key in stack:
        return None
    info = index.get(key)
    if info is None:
        return (key,)
    bases = info[1]
    sequences: list[list[ClassKey]] = []
    for base in bases:
        base_mro = _c3_mro(index, base, stack | {key})
        if base_mro is None:
            return None
        sequences.append(list(base_mro))
    sequences.append(list(bases))
    result = [key]
    while any(sequences):
        sequences = [sequence for sequence in sequences if sequence]
        candidate = next(
            (
                sequence[0]
                for sequence in sequences
                if all(sequence[0] not in other[1:] for other in sequences)
            ),
            None,
        )
        if candidate is None:
            return None
        result.append(candidate)
        for sequence in sequences:
            if sequence and sequence[0] == candidate:
                sequence.pop(0)
    return tuple(result)


def resolve_method(
    index: dict[ClassKey, ClassInfo],
    key: ClassKey,
    method_name: str,
) -> tuple[ClassKey, MethodNode] | None:
    mro = _c3_mro(index, key)
    if mro is None:
        return None
    for candidate in mro:
        info = index.get(candidate)
        if info is None:
            continue
        method = info[0].get(method_name)
        if method is not None:
            return None if _is_abstract(method) else (candidate, method)
    return None


def method_scope(
    index: dict[ClassKey, ClassInfo],
    concrete: ClassKey,
    method_name: str,
) -> list[tuple[ClassKey, MethodNode]]:
    root = resolve_method(index, concrete, method_name)
    if root is None:
        return []
    scope: list[tuple[ClassKey, MethodNode]] = []
    pending = [root]
    visited: set[int] = set()
    while pending:
        current_owner, current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        scope.append((current_owner, current))
        helper_names = {
            call.func.attr
            for call in ast.walk(current)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
        }
        for helper_name in sorted(helper_names, reverse=True):
            resolved = resolve_method(index, concrete, helper_name)
            if resolved is not None and id(resolved[1]) not in visited:
                pending.append(resolved)
    return scope
