"""The project-structure backend must be served by the extension's own generic
backend dispatch, not by a core id-branch.

Locks the extension-boundary fix: core's `_dispatch_core_builtin_backend` must
not special-case the project-structure extension id, and the extension must be
installed like every other private extension so its declared `backend_module`
resolves through `dispatch_extension_backend_request`.

Run with:
    cd backend && .venv/bin/python scripts/test_project_structure_extension.py
"""

from __future__ import annotations

import json
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
EXTENSION_API = BACKEND / "extension_api.py"
EXTENSION_STORE = BACKEND / "extension_store.py"
REPO_ROOT = BACKEND.parent
MANIFEST_CANDIDATES = [
    REPO_ROOT / "better-agent-private" / "extensions" / "project-structure" / "better-agent-extension.json",
    REPO_ROOT / "extensions" / "project-structure" / "better-agent-extension.json",
]

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _load_manifest() -> dict | None:
    for path in MANIFEST_CANDIDATES:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    return None


def _run() -> bool:
    extension_api = EXTENSION_API.read_text(encoding="utf-8")
    extension_store = EXTENSION_STORE.read_text(encoding="utf-8")
    manifest = _load_manifest()

    results = [
        (
            "core dispatch has no project-structure id-branch",
            "BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID" not in extension_api,
            "extension_api.py still id-branches on the project-structure extension",
        ),
        (
            "core has no project-structure backend handler",
            "_dispatch_project_structure_core_backend" not in extension_api,
            "core still implements project-structure backend logic",
        ),
        (
            "machine-nodes core branch is left intact",
            "_dispatch_machine_nodes_core_backend" in extension_api
            and "BUILTIN_MACHINE_NODES_EXTENSION_ID" in extension_api,
            "machine-nodes core dispatch was wrongly removed",
        ),
        (
            "project-structure installs like every other private extension",
            "if extension_id == BUILTIN_PROJECT_STRUCTURE_EXTENSION_ID:\n            continue"
            not in extension_store,
            "_ensure_private_extensions still skips project-structure",
        ),
        (
            "generic-defer guard remains in core dispatch",
            "if backend_spec is not None:" in extension_api
            and "extension_backend_loader.backend_entrypoint_spec_cached(extension_id)" in extension_api,
            "core no longer defers to the generic backend dispatch when a spec exists",
        ),
    ]

    if manifest is None:
        results.append((
            "project-structure manifest is present", False,
            "could not locate better-agent-extension.json for project-structure",
        ))
    else:
        entrypoints = manifest.get("entrypoints") or {}
        perms = manifest.get("permissions") or {}
        results.append((
            "manifest declares a generic backend module",
            bool(str(entrypoints.get("backend_module") or "")
                 or str(entrypoints.get("backend") or "")),
            "manifest has no backend entrypoint for the generic dispatch",
        ))
        results.append((
            "manifest grants backend_routes so the spec resolves",
            perms.get("backend_routes") is True,
            "manifest lacks backend_routes; backend_entrypoint_spec would return None",
        ))
        page = (entrypoints.get("page") or {})
        badge_ep = ((page.get("badge") or {}).get("endpoint") or "")
        open_ep = ((page.get("open") or {}).get("endpoint") or "")
        prefix = f"/api/extensions/{manifest.get('id')}/backend"
        results.append((
            "page entrypoints target the generic backend namespace",
            badge_ep.startswith(prefix) and open_ep.startswith(prefix),
            "page badge/open endpoints do not use the generic /api/extensions/.../backend prefix",
        ))

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        tag = PASS if ok else FAIL
        print(f"  {tag} {name}{'' if ok else ' - ' + msg}")
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    raise SystemExit(0 if _run() else 1)
