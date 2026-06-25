#!/usr/bin/env python3
"""Regression lock for the credential-broker password-manager 404.

Bug: an extension that ships a backend surface (``entrypoints.backend`` or
``entrypoints.backend_module``) but does NOT declare the ``backend_routes``
permission gets ``backend_entrypoint_spec`` == None, so every call to its
``/api/extensions/{id}/backend/*`` surface 404s. credential-broker shipped a
backend (`backend_module: backend.routes`) without declaring ``backend_routes``
→ the password-manager settings page showed a 404.

Two locks:
- Dispatch-level (primary): the runtime gate itself — a backend-bearing
  manifest yields no spec without ``backend_routes`` and a valid spec with it.
- Static invariant: every shipped extension manifest that declares a backend
  entrypoint also declares ``backend_routes`` (pins the credential-broker fix).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP_HOME = Path(tempfile.mkdtemp(prefix="bc-test-backend-routes-perm-"))
import _test_home
_test_home.isolate("ba-test-")

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

import extension_store  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def _install_backend_gate(extension_id: str, permissions: dict) -> None:
    package = TMP_HOME / "private-fixtures" / extension_id
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": extension_id,
        "surfaces": ["backend_feature"],
        "entrypoints": {"backend_module": "backend.routes"},
        "permissions": permissions,
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    backend_pkg = package / "backend"
    backend_pkg.mkdir(exist_ok=True)
    (backend_pkg / "__init__.py").write_text("", encoding="utf-8")
    (backend_pkg / "routes.py").write_text(
        "from fastapi import APIRouter\n"
        "def create_router(_context):\n"
        "    return APIRouter()\n",
        encoding="utf-8",
    )
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(package.parent),
            "extension_path": package.name,
            "ref": "",
            "commit_sha": extension_id,
        },
        persist=True,
    )


def test_backend_routes_permission_gates_spec() -> None:
    extension_id = "ofek-test.backend-gate"

    _install_backend_gate(extension_id, permissions={"network": True})
    spec = extension_store.backend_entrypoint_spec(extension_id)
    check(spec is None, "backend-bearing manifest WITHOUT backend_routes yields no spec (404 cause)")

    _install_backend_gate(extension_id, permissions={"network": True, "backend_routes": True})
    spec = extension_store.backend_entrypoint_spec(extension_id)
    check(spec is not None, "backend-bearing manifest WITH backend_routes yields a spec")
    check(spec["entrypoint_kind"] == "module", "module entrypoint resolves without a file check")
    check(spec["entrypoint"] == "backend.routes", "spec carries the declared backend module")


def _shipped_manifest_paths() -> list[Path]:
    roots = [REPO_ROOT / "extensions"]
    paths: list[Path] = []
    for root in roots:
        if root.is_dir():
            paths.extend(sorted(root.glob("*/better-agent-extension.json")))
    return paths


def test_shipped_backend_extensions_declare_backend_routes() -> None:
    manifests = _shipped_manifest_paths()
    check(bool(manifests), "found shipped extension manifests to lint")
    for path in manifests:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        entrypoints = manifest.get("entrypoints") or {}
        has_backend = bool(entrypoints.get("backend")) or bool(entrypoints.get("backend_module"))
        if not has_backend:
            continue
        permissions = manifest.get("permissions") or {}
        check(
            bool(permissions.get("backend_routes")),
            f"{manifest.get('id')} declares a backend entrypoint and backend_routes permission",
        )


if __name__ == "__main__":
    try:
        test_backend_routes_permission_gates_spec()
        test_shipped_backend_extensions_declare_backend_routes()
        print("ALL PASS")
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
