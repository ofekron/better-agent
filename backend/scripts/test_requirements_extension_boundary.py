"""Requirements MCP and analysis logic must be supplied by the private extension.

Run with:
    cd backend && .venv/bin/python scripts/test_requirements_extension_boundary.py
"""

from __future__ import annotations

from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
RUNNER = BACKEND / "runner.py"
EXTENSION_STORE = BACKEND / "extension_store.py"
EXTENSION_PACKAGE_LOADER = BACKEND / "extension_package_loader.py"
REQUIREMENT_ANALYSIS = BACKEND / "requirement_analysis"

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _run() -> bool:
    runner = RUNNER.read_text(encoding="utf-8")
    extension_store = EXTENSION_STORE.read_text(encoding="utf-8")
    extension_package_loader = EXTENSION_PACKAGE_LOADER.read_text(encoding="utf-8")
    results = [
        (
            "runner has no in-process get-requirements builder",
            "_build_get_requirements_tool" not in runner
            and "_build_get_requirements_internal_tool" not in runner
            and 'name="get-requirements"' not in runner,
            "legacy builder still present",
        ),
        (
            "requirements extension may replace reserved MCP server",
            'BUILTIN_REQUIREMENTS_EXTENSION_ID: frozenset({"get-requirements"})' in extension_store,
            "missing replacement allow-list entry",
        ),
        (
            "proprietary requirement analysis package is not in public core",
            not REQUIREMENT_ANALYSIS.exists(),
            "backend/requirement_analysis still exists",
        ),
        (
            "public core has no requirements-specific extension loader",
            not (BACKEND / "requirements_extension.py").exists(),
            "backend/requirements_extension.py still exists",
        ),
        (
            "generic extension package loader validates packages before import",
            "def ensure_package_importable" in extension_package_loader
            and "ExtensionPackageUnavailable" in extension_package_loader,
            "generic loader can expose an install root without validating package availability",
        ),
        (
            "requirements runtime files are an extension readiness gate",
            "BUILTIN_RUNTIME_REQUIRED_PATHS" in extension_store
            and 'BUILTIN_REQUIREMENTS_EXTENSION_ID: ("requirement_analysis",)' in extension_store,
            "requirements MCP can be runtime-ready without requirement_analysis",
        ),
    ]
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' - ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    raise SystemExit(0 if _run() else 1)
