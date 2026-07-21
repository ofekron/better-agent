"""Requirements MCP and analysis logic must be supplied by the private extension.

Run with:
    cd backend && .venv/bin/python scripts/test_requirements_extension_boundary.py
"""

from __future__ import annotations

from pathlib import Path
import sys


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import extension_store as es  # noqa: E402

RUNNER = BACKEND / "runner.py"
EXTENSION_STORE = BACKEND / "extension_store.py"
EXTENSION_PACKAGE_LOADER = BACKEND / "extension_package_loader.py"
REQUIREMENT_CONTEXT = BACKEND / "requirement_context.py"
REQUIREMENT_ANALYSIS = BACKEND / "requirement_analysis"
PROVISIONING = BACKEND / "provisioning"

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _run() -> bool:
    runner = RUNNER.read_text(encoding="utf-8")
    extension_store = EXTENSION_STORE.read_text(encoding="utf-8")
    extension_package_loader = EXTENSION_PACKAGE_LOADER.read_text(encoding="utf-8")
    requirement_context = REQUIREMENT_CONTEXT.read_text(encoding="utf-8")
    processor_worker_name = "worker:" + "requirements:" + "query-processor"
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
            es._MCP_REPLACEMENT_CORE_ROLES.get("get-requirements") == "requirements",
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
            "processor spec implementation is not in public requirement_context",
            "class GetRequirementsProcessorSpec" not in requirement_context
            and processor_worker_name not in requirement_context
            and "request.search_hints" not in requirement_context,
            "private processor spec payload still lives in requirement_context.py",
        ),
        (
            "public requirement_context loads processor spec through provisioning registry",
            "_get_provisioned_spec" in requirement_context
            and "requirement_analysis.processor_spec" in requirement_context
            and "provisioning.get" in requirement_context,
            "processor spec is not resolved through the generic registry",
        ),
        (
            "provisioning has no duplicate prompt renderer",
            not (PROVISIONING / "prompts.py").exists(),
            "duplicate render_prompt still lives in provisioning/prompts.py",
        ),
        (
            "generic extension package loader validates packages before import",
            "def ensure_package_importable" in extension_package_loader
            and "ExtensionPackageUnavailable" in extension_package_loader,
            "generic loader can expose an install root without validating package availability",
        ),
        (
            "extension protocol owns runtime module readiness",
            not es._BUILTIN_RUNTIME_REQUIRED_PATHS
            and "python_modules" in extension_store
            and "def _run_extension_smoke_test" in extension_store,
            "runtime readiness still depends on private id maps",
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
