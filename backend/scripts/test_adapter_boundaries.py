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
# The composition-side implementation of ChatCommandPort (see
# backend/adapters/command_port.py's module docstring): it imports
# backend.adapters.command_port to implement the Protocol, so it needs
# the same permitted-importer exemption as main.py/adapter_api.py.
SURFACE_COMMANDS_PY = BACKEND_DIR / "surface_commands.py"

SURFACE_CONTRACT_PREFIX = "backend.surface_contract"
ADAPTERS_PREFIX = "backend.adapters"

# Only the infra actually imported by some backend/adapters/*.py file
# belongs here — an allowlist entry nothing imports is a disguised future
# hook, not a real boundary crossing, and silently rots (see
# test_adapters_allowlist_has_no_dead_entries below, which fails the
# instant an entry here isn't referenced by any adapters file).
ADAPTERS_ALLOWLIST = (
    SURFACE_CONTRACT_PREFIX,
    ADAPTERS_PREFIX,
    "backend.event_bus",
    "backend.event_journal",
    "backend.paths",
    "backend.scheme_migrations",
)

# backend/adapters/__init__.py is the ONE sanctioned canonicalization site
# (see its module docstring): it bare-imports the infra modules above so it
# can alias them onto their "backend.*" sys.modules keys before any other
# backend/adapters/*.py file's own dotted import runs. A per-file extension
# of the general adapters allowlist, granting exactly the bare (undotted)
# names of the infra this package canonicalizes — derived from
# ADAPTERS_ALLOWLIST itself so the two lists can never drift apart.
ADAPTERS_INIT_PY = ADAPTERS_DIR / "__init__.py"
ADAPTERS_INIT_BARE_ALLOWLIST = tuple(
    prefix.rsplit(".", 1)[-1]
    for prefix in ADAPTERS_ALLOWLIST
    if prefix not in (SURFACE_CONTRACT_PREFIX, ADAPTERS_PREFIX)
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

# The two files permitted to mutate sys.modules directly (the
# canonicalization sites above) — every other backend/adapters/*.py file
# must not (see test_no_adapter_mutates_sys_modules_outside_sanctioned_site).
_SANCTIONED_SYS_MODULES_MUTATORS = frozenset({STORE_ACCESS_PY, ADAPTERS_INIT_PY})

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
        elif path == ADAPTERS_INIT_PY:
            allowed = ADAPTERS_ALLOWLIST + ADAPTERS_INIT_BARE_ALLOWLIST
        violations += _collect_violations(path, allowed)
    return violations


def _adapters_allowlist_dead_entries() -> list[str]:
    """Every ADAPTERS_ALLOWLIST entry must be reached by at least one real
    (static) import somewhere under backend/adapters/ — an entry nothing
    imports is a disguised future hook, not a live boundary crossing, and
    silently drifts out of sync with what the code actually needs."""
    reached: set[str] = set()
    for path in _iter_py_files(ADAPTERS_DIR):
        tree = ast.parse(path.read_text(), filename=str(path))
        package = _package_for(path)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for candidate in _import_candidates(node, package):
                for prefix in ADAPTERS_ALLOWLIST:
                    if _matches(candidate, prefix):
                        reached.add(prefix)
    dead = [
        prefix for prefix in ADAPTERS_ALLOWLIST
        if prefix not in (SURFACE_CONTRACT_PREFIX, ADAPTERS_PREFIX) and prefix not in reached
    ]
    return [f"ADAPTERS_ALLOWLIST entry {prefix!r} is never imported by any backend/adapters/*.py file" for prefix in dead]


# ---- dynamic-import evasion: importlib/__import__ close an AST-invisible
# back door around every rule above unless explicitly policed. Only
# store_access.py may use it (see its module docstring), and even there
# only with a statically-verifiable string-literal target.

def _is_importlib_import_module(func: ast.expr) -> bool:
    return (
        isinstance(func, ast.Attribute) and func.attr == "import_module"
        and isinstance(func.value, ast.Name) and func.value.id == "importlib"
    )


def _is_dunder_import(func: ast.expr) -> bool:
    return isinstance(func, ast.Name) and func.id == "__import__"


def _collect_dynamic_import_evasion(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    rel = path.relative_to(REPO_ROOT)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_importlib_import_module(node.func) or _is_dunder_import(node.func):
            violations.append(
                f"{rel}:{node.lineno}: dynamic import via "
                f"{ast.unparse(node.func)}() is only permitted in "
                f"{STORE_ACCESS_PY.relative_to(REPO_ROOT)}",
            )
    return violations


def _dynamic_import_evasion_violations() -> list[str]:
    violations = []
    for path in _iter_py_files(ADAPTERS_DIR):
        if path == STORE_ACCESS_PY:
            continue
        violations += _collect_dynamic_import_evasion(path)
    return violations


def _store_access_dynamic_target_violations() -> list[str]:
    """store_access.py's `_resolve(name)` calls are the one sanctioned
    dynamic-import site — but "dynamic" only refers to the import
    *mechanism*; every call site passes a string literal, so the actual
    *target* is fully static. Extract those literals via AST and check them
    against STORE_ACCESS_ALLOWLIST exactly like a normal import would be
    checked, closing the "AST checker can't see importlib" gap for this
    exception. A non-literal argument (a computed name) is rejected
    outright — allowing one would reopen the gap this check exists to
    close."""
    tree = ast.parse(STORE_ACCESS_PY.read_text(), filename=str(STORE_ACCESS_PY))
    rel = STORE_ACCESS_PY.relative_to(REPO_ROOT)
    allowed_names = {prefix.rsplit(".", 1)[-1] for prefix in STORE_ACCESS_ALLOWLIST}
    violations = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_resolve"
        ):
            continue
        arg = node.args[0] if node.args else None
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            violations.append(
                f"{rel}:{node.lineno}: _resolve() must be called with a string "
                f"literal (found a computed/non-literal argument), so its "
                f"target stays statically verifiable",
            )
            continue
        if arg.value not in allowed_names:
            violations.append(
                f"{rel}:{node.lineno}: _resolve({arg.value!r}) is not in "
                f"STORE_ACCESS_ALLOWLIST",
            )
    return violations


# ---- sys.modules mutation: confine it to the two sanctioned
# canonicalization sites (backend/adapters/__init__.py, store_access.py).

def _is_sys_modules_expr(expr: ast.expr) -> bool:
    return (
        isinstance(expr, ast.Attribute) and expr.attr == "modules"
        and isinstance(expr.value, ast.Name) and expr.value.id == "sys"
    )


def _collect_sys_modules_mutations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    rel = path.relative_to(REPO_ROOT)
    violations = []
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AugAssign):
            target = node.target
        if isinstance(target, ast.Subscript) and _is_sys_modules_expr(target.value):
            violations.append(f"{rel}:{node.lineno}: direct sys.modules[...] assignment")
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("setdefault", "update", "pop", "popitem", "clear")
            and _is_sys_modules_expr(node.func.value)
        ):
            violations.append(f"{rel}:{node.lineno}: sys.modules.{node.func.attr}(...) mutation")
    return violations


def _sys_modules_mutation_violations() -> list[str]:
    violations = []
    for path in _iter_py_files(ADAPTERS_DIR):
        if path in _SANCTIONED_SYS_MODULES_MUTATORS:
            continue
        violations += _collect_sys_modules_mutations(path)
    return violations


def _external_adapters_import_violations() -> list[str]:
    exempt = {SURFACE_CONTRACT_DIR, ADAPTERS_DIR}
    exempt_files = {MAIN_PY, ADAPTER_API_PY, SURFACE_COMMANDS_PY}
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


def test_adapters_allowlist_has_no_dead_entries() -> None:
    violations = _adapters_allowlist_dead_entries()
    assert not violations, "ADAPTERS_ALLOWLIST has dead entries:\n" + "\n".join(violations)


def test_no_dynamic_import_evasion_outside_store_access() -> None:
    violations = _dynamic_import_evasion_violations()
    assert not violations, "dynamic-import evasion of the adapters boundary:\n" + "\n".join(violations)


def test_store_access_dynamic_targets_are_statically_verifiable() -> None:
    violations = _store_access_dynamic_target_violations()
    assert not violations, "store_access.py dynamic import target violations:\n" + "\n".join(violations)


def test_no_adapter_mutates_sys_modules_outside_sanctioned_site() -> None:
    violations = _sys_modules_mutation_violations()
    assert not violations, "unsanctioned sys.modules mutation in backend/adapters:\n" + "\n".join(violations)


if __name__ == "__main__":
    all_violations = (
        _surface_contract_violations()
        + _adapters_violations()
        + _external_adapters_import_violations()
        + _adapters_allowlist_dead_entries()
        + _dynamic_import_evasion_violations()
        + _store_access_dynamic_target_violations()
        + _sys_modules_mutation_violations()
    )
    if all_violations:
        print("\n".join(all_violations))
        raise SystemExit(1)
    print("adapter boundaries OK")
