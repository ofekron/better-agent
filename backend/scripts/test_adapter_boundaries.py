"""Static import-boundary enforcement for backend/surface_contract/ and
backend/adapters/. Stdlib-only (ast + pathlib) so it runs without any
backend import — the boundary it checks is exactly what would make such
an import unsafe to take for granted.

Rules enforced:
  1. backend/surface_contract/*.py imports only stdlib + backend.surface_contract.*
  2. backend/adapters/*.py imports only stdlib + backend.surface_contract.* +
     backend.adapters.* + a fixed shared-infra allowlist.
  3. No other backend module imports backend.adapters.*, except the
     composition root (backend/main.py) and the transport module
     (backend/adapter_api.py)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent

SURFACE_CONTRACT_DIR = BACKEND_DIR / "surface_contract"
ADAPTERS_DIR = BACKEND_DIR / "adapters"
SCRIPTS_DIR = BACKEND_DIR / "scripts"
MAIN_PY = BACKEND_DIR / "main.py"
ADAPTER_API_PY = BACKEND_DIR / "adapter_api.py"

SURFACE_CONTRACT_PREFIX = "backend.surface_contract"
ADAPTERS_PREFIX = "backend.adapters"

ADAPTERS_ALLOWLIST = (
    SURFACE_CONTRACT_PREFIX,
    ADAPTERS_PREFIX,
    "backend.event_bus",
    "backend.event_journal",
    "backend.event_ingester",
    "backend.jsonl_tailer",
    "backend.paths",
    "backend.i18n",
    "backend.user_msg_lifecycle",
)

# backend/adapters/store_access.py is the ONE place adapters may reach
# persistent stores (see its module docstring) — a per-file extension of
# the general adapters allowlist above, which every other adapter file
# still must not exceed.
STORE_ACCESS_PY = ADAPTERS_DIR / "store_access.py"
STORE_ACCESS_ALLOWLIST = (
    "backend.session_store",
    "backend.config_store",
    "backend.project_store",
    "backend.runs_dir",
)

_STDLIB = set(sys.stdlib_module_names) | {"__future__"}
_SKIP_DIR_NAMES = {"__pycache__", "node_modules"}


def _iter_py_files(root: Path) -> list[Path]:
    out = []
    for path in sorted(root.rglob("*.py")):
        rel_parts = path.relative_to(root).parts[:-1]
        if any(p.startswith(".") or p in _SKIP_DIR_NAMES for p in rel_parts):
            continue
        out.append(path)
    return out


def _package_for(path: Path) -> str:
    """Dotted __package__ of `path`, e.g. backend/adapters/x.py -> 'backend.adapters'.
    Same formula covers plain modules and __init__.py (both have
    __package__ equal to their containing directory's dotted path)."""
    rel = path.relative_to(REPO_ROOT)
    return ".".join(rel.parts[:-1])


def _resolve_relative(package: str, level: int, module: str | None) -> str:
    parts = package.split(".") if package else []
    cut = level - 1
    if cut > 0:
        parts = parts[: len(parts) - cut] if len(parts) >= cut else []
    base = ".".join(parts)
    if module:
        return f"{base}.{module}" if base else module
    return base


def _import_candidates(node: ast.stmt, package: str) -> list[str]:
    """Dotted module paths this import statement can reach. For
    `from X import a, b` this yields 'X.a' and 'X.b' (not bare 'X') so an
    allowed re-export like `from backend import surface_contract` isn't
    mistaken for a bare, unqualified 'backend' import."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if node.level:
        full = _resolve_relative(package, node.level, node.module)
    else:
        full = node.module or ""
    star = any(alias.name == "*" for alias in node.names)
    if star or not node.names:
        return [full] if full else []
    return [f"{full}.{alias.name}" if full else alias.name for alias in node.names]


def _is_allowed(candidate: str, allowed_prefixes: tuple[str, ...]) -> bool:
    root = candidate.split(".", 1)[0]
    if root in _STDLIB:
        return True
    return any(
        candidate == prefix or candidate.startswith(prefix + ".")
        for prefix in allowed_prefixes
    )


def _matches(candidate: str, prefix: str) -> bool:
    return candidate == prefix or candidate.startswith(prefix + ".")


def _collect_violations(path: Path, allowed_prefixes: tuple[str, ...]) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    package = _package_for(path)
    rel = path.relative_to(REPO_ROOT)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for candidate in _import_candidates(node, package):
            if not candidate or _is_allowed(candidate, allowed_prefixes):
                continue
            violations.append(
                f"{rel}:{node.lineno}: forbidden import {candidate!r}",
            )
    return violations


def _collect_forbidden_adapters_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    package = _package_for(path)
    rel = path.relative_to(REPO_ROOT)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for candidate in _import_candidates(node, package):
            if candidate and _matches(candidate, ADAPTERS_PREFIX):
                violations.append(
                    f"{rel}:{node.lineno}: forbidden import {candidate!r} "
                    f"(only backend/main.py and backend/adapter_api.py may "
                    f"import backend.adapters)",
                )
    return violations


def _surface_contract_violations() -> list[str]:
    violations = []
    for path in _iter_py_files(SURFACE_CONTRACT_DIR):
        violations += _collect_violations(path, (SURFACE_CONTRACT_PREFIX,))
    return violations


def _adapters_violations() -> list[str]:
    violations = []
    for path in _iter_py_files(ADAPTERS_DIR):
        allowed = ADAPTERS_ALLOWLIST
        if path == STORE_ACCESS_PY:
            allowed = ADAPTERS_ALLOWLIST + STORE_ACCESS_ALLOWLIST
        violations += _collect_violations(path, allowed)
    return violations


def _external_adapters_import_violations() -> list[str]:
    exempt = {SURFACE_CONTRACT_DIR, ADAPTERS_DIR}
    exempt_files = {MAIN_PY, ADAPTER_API_PY}
    violations = []
    for path in _iter_py_files(BACKEND_DIR):
        if path in exempt_files:
            continue
        if any(d in path.parents for d in exempt):
            continue
        # backend/scripts/test_*.py files legitimately exercise
        # backend.adapters directly (unit coverage for the adapters
        # themselves) — exempt only that filename pattern, not the whole
        # scripts/ directory.
        if path.parent == SCRIPTS_DIR and path.name.startswith("test_"):
            continue
        violations += _collect_forbidden_adapters_imports(path)
    return violations


def test_surface_contract_imports_only_stdlib_and_self() -> None:
    violations = _surface_contract_violations()
    assert not violations, "backend/surface_contract boundary violated:\n" + "\n".join(
        violations,
    )


def test_adapters_imports_only_allowlisted_infra() -> None:
    violations = _adapters_violations()
    assert not violations, "backend/adapters boundary violated:\n" + "\n".join(
        violations,
    )


def test_only_composition_root_imports_adapters() -> None:
    violations = _external_adapters_import_violations()
    assert not violations, "backend.adapters import leaked outside its allowed callers:\n" + "\n".join(
        violations,
    )


if __name__ == "__main__":
    all_violations = (
        _surface_contract_violations()
        + _adapters_violations()
        + _external_adapters_import_violations()
    )
    if all_violations:
        print("\n".join(all_violations))
        raise SystemExit(1)
    print("adapter boundaries OK")
