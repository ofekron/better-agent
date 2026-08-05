from __future__ import annotations

import ast
import glob
import os
from pathlib import Path


def should_ignore_test_module(collection_path, config):
    name = collection_path.name
    if not name.startswith("test_") or not name.endswith(".py"):
        return None
    explicit_paths = _explicit_python_test_paths(config)
    if explicit_paths is not None:
        try:
            candidate = _normalized_physical_path(collection_path)
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        else:
            if candidate not in explicit_paths:
                return None
    try:
        tree = ast.parse(collection_path.read_text(encoding="utf-8"))
    except (SyntaxError, ValueError):
        return None
    if _is_unguarded_runner(tree):
        return True
    has_async_test = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            return None
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            return None
        # Async tests are `ast.AsyncFunctionDef`, a sibling of FunctionDef
        # rather than a subclass, so they need their own branch.
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("test_"):
            has_async_test = True
    # An async-only module runs under pytest only via an async plugin marker,
    # which requires importing pytest. Without that import the module is a
    # standalone runner script (it drives its own `asyncio.run` under a
    # `__main__` guard), so collecting it would report every coroutine as an
    # unsupported-async failure.
    if has_async_test and _imports_pytest(tree):
        return None
    return True


def _imports_pytest(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "pytest" or alias.name.startswith("pytest.")
                   for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "pytest" or (node.module or "").startswith("pytest."):
                return True
    return False


def _explicit_python_test_paths(config):
    if config is None:
        return None
    try:
        if config.getoption("pyargs"):
            return None
        selectors = config.getoption("file_or_dir")
        invocation_dir = Path(config.invocation_params.dir)
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return None
    if not selectors:
        return None

    paths = set()
    for selector in selectors:
        if not isinstance(selector, (str, os.PathLike)):
            return None
        path_text = os.fspath(selector).split("::", 1)[0]
        if not path_text.casefold().endswith(".py") or glob.has_magic(path_text):
            return None
        path = Path(path_text)
        if not path.is_absolute():
            path = invocation_dir / path
        try:
            paths.add(_normalized_physical_path(path))
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
    return paths


def _normalized_physical_path(path):
    return os.path.normcase(os.fspath(path.resolve(strict=False)))


def has_main_entry(source: str) -> bool:
    """True if the module has a top-level ``if __name__ == "__main__":`` guard.

    Used to tell dual-purpose standalone-runner scripts (a ``__main__`` driver
    that supplies arguments to ``test_*`` helpers) from pure pytest modules.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    return any(_is_main_guard_node(node) for node in tree.body)


def _is_main_guard_node(node):
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and any(
            isinstance(c, ast.Constant) and c.value == "__main__"
            for c in node.test.comparators
        )
    )


def _is_unguarded_runner(tree):
    exit_names = ("exit", "sys.exit", "os._exit")

    def _exit_call_name(node):
        fn = node.func
        if isinstance(fn, ast.Name):
            return fn.id
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            return f"{fn.value.id}.{fn.attr}"
        return None

    def _walk(node, under_main):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            this_under_main = under_main or _is_main_guard_node(child)
            if isinstance(child, ast.Call) and _exit_call_name(child) in exit_names:
                if not this_under_main:
                    return True
            if _walk(child, this_under_main):
                return True
        return False

    return _walk(tree, False)
