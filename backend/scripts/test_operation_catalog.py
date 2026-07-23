#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile
import shutil

from pydantic import BaseModel

import operation_catalog
import operation_authority
from runtime_principal import PrincipalKind, RuntimePrincipal
from scoped_runtime_client import ScopedRuntimeClient


class Request(BaseModel):
    value: str


def _load_handler(path: Path, body: str):
    path.write_text(body, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"catalog_fixture_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.handle


def _principal(operation: str) -> RuntimePrincipal:
    return RuntimePrincipal(
        kind=PrincipalKind.AGENT_RUN,
        principal_id="run-1",
        issuer="test",
        audience="better-agent-operation-runtime",
        permitted_operations=(operation,),
        permitted_resources=("session:one",),
        grant_generation="grant-1",
        availability_generation="available-1",
        issued_at=1.0,
        expires_at=4_000_000_000.0,
        app_session_id="session-one",
        run_id="run-one",
        provider_id="provider-one",
        node_id="primary",
        cwd="/tmp/project",
    )


def _assert_generated_runtime_files_excluded() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "AGENTS.md").touch()
        backend = root / "backend"
        backend.mkdir()
        (backend / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
        (backend / "requirements.txt").write_text("", encoding="utf-8")
        generated = backend / ".venvs" / "runtime" / ".stage"
        generated.mkdir(parents=True)
        transient = generated / "activate_this.py"
        transient.write_text("VALUE = 1\n", encoding="utf-8")

        first = operation_catalog._artifact_digest(root)
        transient.write_text("VALUE = 2\n", encoding="utf-8")
        transient.unlink()

        assert operation_catalog._artifact_digest(root) == first


def main() -> None:
    _assert_generated_runtime_files_excluded()
    state_root = Path(tempfile.mkdtemp(prefix="better-agent-catalog-state-"))
    with tempfile.TemporaryDirectory() as raw:
        os.environ["BETTER_AGENT_HOME"] = str(state_root)
        root = Path(raw)
        handler = _load_handler(
            root / "handler.py",
            "def handle(request):\n    return {'value': request.value}\n",
        )
        manager = operation_catalog.CatalogManager()
        descriptor = manager.register_capability(
            "example",
            "read",
            Request,
            handler,
            policy=operation_catalog.OperationPolicy(
                side_effect=operation_catalog.SideEffectClass.READ,
                owner=operation_catalog.ExecutionOwner.PRIMARY,
                recovery=operation_catalog.RecoveryPolicy.FAIL,
                durable=False,
                cancel_supported=False,
                context_required=True,
                resource_fields=("value",),
            ),
        )
        principal = _principal(descriptor.key)
        principal = RuntimePrincipal(
            **{
                **principal.__dict__,
                "permitted_resources": ("value:ok",),
            }
        )
        previous_validator = operation_authority.register_validator(
            PrincipalKind.AGENT_RUN,
            lambda candidate: candidate.principal_id == "run-1",
        )
        first = manager.publish()
        seal = (
            Path(os.environ["BETTER_AGENT_HOME"])
            / "operation_catalog"
            / "generations"
            / f"{first.generation}.json"
        )
        assert seal.is_file()
        assert manager.publish() is first
        assert first.generation == manager.current().generation
        assert asyncio.run(
            ScopedRuntimeClient(operation_authority.issue(principal), first).invoke(
                descriptor.key,
                {"value": "ok"},
            )
        ) == {"value": "ok"}
        try:
            asyncio.run(
                ScopedRuntimeClient(operation_authority.issue(principal), first).invoke(
                    descriptor.key,
                    {"value": "other"},
                )
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("resource scope was not enforced")
        try:
            first.snapshot.get(descriptor.key).handler(Request(value="bad"))
        except RuntimeError as exc:
            assert "catalog executor" in str(exc)
        else:
            raise AssertionError("snapshot exposed the executable handler")
        manager.pin(first.generation)
        assert manager.pin_count(first.generation) == 1
        manager.unpin(first.generation)
        assert manager.pin_count(first.generation) == 0
        (root / "handler.py").write_text(
            "def handle(request):\n    return {'value': 'tampered'}\n",
            encoding="utf-8",
        )
        try:
            first.verify_artifacts()
        except operation_catalog.OperationArtifactError as exc:
            assert descriptor.key in str(exc)
        else:
            raise AssertionError("artifact tampering was accepted")
        # Disk drift never blocks the already-imported in-process handler:
        # invoke stays on the registration-time code.
        assert asyncio.run(
            ScopedRuntimeClient(operation_authority.issue(principal), first).invoke(
                descriptor.key,
                {"value": "ok"},
            )
        ) == {"value": "ok"}
        # The recovery boundary stays fail-closed: pinning a generation whose
        # artifact drifted must refuse.
        try:
            manager.pin(first.generation)
        except operation_catalog.OperationArtifactError:
            assert manager.pin_count(first.generation) == 0
        else:
            raise AssertionError("pin accepted a drifted artifact")
        operation_authority.restore_validator(PrincipalKind.AGENT_RUN, previous_validator)
    shutil.rmtree(state_root)
    print("operation catalog tests passed")


if __name__ == "__main__":
    main()
