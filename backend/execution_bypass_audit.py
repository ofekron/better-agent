from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True, order=True)
class ExecutionSurface:
    category: str
    module: str
    owner: str
    call: str


_PROCESS_CALLS = frozenset({
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "os.execv",
    "os.execve",
    "os.execvp",
    "os.execvpe",
    "os.system",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.run",
})
_DISCOVERY_CALLS = frozenset({
    "resolve_cli_binary",
})
_STATIC_AUTHORITY_CALLS = frozenset({
    "config_store.get_default_provider",
    "config_store.get_provider",
    "config_store.get_provider_with_key",
    "config_store.list_providers",
})

ALLOWED_SURFACES = frozenset({
    ExecutionSurface(
        "discovery",
        "codex_headless_execution.py",
        "prepare_codex_headless",
        "resolve_cli_binary",
    ),
    ExecutionSurface(
        "discovery",
        "codex_model_discovery.py",
        "_build_context",
        "resolve_cli_binary",
    ),
    ExecutionSurface(
        "discovery",
        "provider_codex.py",
        "CodexProvider.prepare_run",
        "resolve_cli_binary",
    ),
    ExecutionSurface(
        "process",
        "codex_model_discovery_process.py",
        "run_catalog_command",
        "subprocess.Popen",
    ),
    ExecutionSurface(
        "process",
        "provider_codex.py",
        "CodexProvider._start_run",
        "subprocess.Popen",
    ),
    ExecutionSurface(
        "process",
        "runner_codex.py",
        "_start_app_server",
        "asyncio.create_subprocess_exec",
    ),
    ExecutionSurface(
        "static_authority",
        "codex_model_discovery.py",
        "_provider_snapshot",
        "config_store.list_providers",
    ),
})


def _call_name(node: ast.Call) -> str:
    parts: list[str] = []
    current: ast.expr = node.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


class _SurfaceVisitor(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.aliases: dict[str, str] = {}
        self.owners: list[str] = []
        self.surfaces: list[ExecutionSurface] = []

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            self.aliases[item.asname or item.name] = item.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is None:
            return
        for item in node.names:
            self.aliases[item.asname or item.name] = f"{node.module}.{item.name}"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.owners.append(node.name)
        self.generic_visit(node)
        self.owners.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self.owners.append(node.name)
        if self.module.startswith("provider_") and node.name == "start_run":
            self.surfaces.append(ExecutionSurface(
                "public_start_run",
                self.module,
                ".".join(self.owners),
                "start_run",
            ))
        self.generic_visit(node)
        self.owners.pop()

    def visit_Call(self, node: ast.Call) -> None:
        call = _call_name(node)
        head, separator, tail = call.partition(".")
        if head in self.aliases:
            call = self.aliases[head] + (separator + tail if separator else "")
        short_call = call.rsplit(".", 1)[-1]
        category = ""
        if call in _PROCESS_CALLS:
            category = "process"
        elif short_call in _DISCOVERY_CALLS:
            category = "discovery"
            call = short_call
        elif call in _STATIC_AUTHORITY_CALLS:
            category = "static_authority"
        if category:
            self.surfaces.append(ExecutionSurface(
                category,
                self.module,
                ".".join(self.owners) or "<module>",
                call,
            ))
        self.generic_visit(node)


def inventory_source(source: str, module: str) -> frozenset[ExecutionSurface]:
    visitor = _SurfaceVisitor(module)
    visitor.visit(ast.parse(source, filename=module))
    return frozenset(visitor.surfaces)


def production_modules(backend: Path) -> Iterable[Path]:
    scoped = {
        "codex_headless_execution.py",
        "codex_model_discovery.py",
        "codex_model_discovery_process.py",
        "provider_codex.py",
        "provider_fugu.py",
        "run_recovery.py",
        "runner_codex.py",
    }
    yield from (backend / name for name in sorted(scoped))


def audit_backend(backend: Path) -> frozenset[ExecutionSurface]:
    observed = frozenset(
        surface
        for path in production_modules(backend)
        for surface in inventory_source(
            path.read_text(encoding="utf-8"),
            path.name,
        )
    )
    unmanaged = observed - ALLOWED_SURFACES
    stale = ALLOWED_SURFACES - observed
    if unmanaged or stale:
        raise AssertionError(
            f"unmanaged={sorted(unmanaged)!r}; stale={sorted(stale)!r}",
        )
    return observed
