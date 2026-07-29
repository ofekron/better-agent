#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import uuid
import asyncio
from dataclasses import replace
from pathlib import Path


TEST_HOME = tempfile.mkdtemp(prefix="ba-provider-model-admission-")
os.environ["BETTER_AGENT_HOME"] = TEST_HOME
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from execution_template import ExecutionArtifact, ExecutionTemplate  # noqa: E402
from execution_template import prepare_execution  # noqa: E402
from codex_execution import build_codex_execution_contract  # noqa: E402
from codex_execution_runtime import codex_provider_contract  # noqa: E402
from model_catalog_authority import build_catalog_authority  # noqa: E402
from model_catalog_cache import build_catalog_snapshot  # noqa: E402
from model_catalog_refresh_state import (  # noqa: E402
    CatalogProjection,
    changed_fact,
    removed_fact,
)
import model_catalog_read_projection  # noqa: E402
from model_execution_admission import (  # noqa: E402
    ModelAdmissionError,
    admit_execution_model,
    issue_model_admission,
)
from provider_execution_contract import provider_family_contract  # noqa: E402
from provider_family_launch_attestation import (  # noqa: E402
    FamilyLaunchAttestation,
    capture_cli_launch,
    capture_config_scope,
    capture_runner_launch,
)
from provider_family_runtime_capabilities import (  # noqa: E402
    family_runtime_capability_payload,
    snapshot_family_runtime_capabilities,
)
from provider_manifest import SPECS  # noqa: E402
from provider import Provider  # noqa: E402


def _record(kind: str) -> dict:
    return {
        "id": f"{kind}-provider",
        "kind": kind,
        "generation": str(uuid.uuid4()),
        "revision": 3,
    }


def _template(model: str | None = "model") -> ExecutionTemplate:
    return ExecutionTemplate.create({
        "run_id": str(uuid.uuid4()),
        "prompt": "prove admission",
        "cwd": TEST_HOME,
        "model": model,
        "reasoning_effort": None,
        "session_id": None,
        "mode": "native",
        "app_session_id": "session",
    })


def _family_contract(record: dict) -> dict:
    return provider_family_contract(record, payload={"proof": record["kind"]})


def _artifact(
    record: dict,
    *,
    descriptor: dict | None,
    model: str | None = "model",
    contract_payload: dict | None = None,
) -> ExecutionArtifact:
    contract = (
        _family_contract(record)
        if contract_payload is None
        else provider_family_contract(record, payload=contract_payload)
    )
    return ExecutionArtifact.create(
        record,
        _template(model),
        runtime_policy=(
            {"model_admission": descriptor}
            if descriptor is not None
            else {}
        ),
        provider_contract=contract,
    )


def _spawn_attested_artifact(
    record: dict,
    *,
    descriptor: dict | None,
) -> ExecutionArtifact:
    root = Path(tempfile.mkdtemp(
        prefix="spawn-authority-",
        dir=TEST_HOME,
    ))
    run_dir = root / "run"
    config_dir = root / "config"
    downstream = root / "claude"
    run_dir.mkdir(parents=True)
    config_dir.mkdir()
    _write_executable(downstream)
    launch = FamilyLaunchAttestation.capture(
        family="claude",
        runner=capture_runner_launch(
            run_dir=run_dir,
            executable_path=sys.executable,
            runner_entry=BACKEND / "runner.py",
            runner_kind="claude",
            runner_module="runner",
            frozen=False,
        ),
        downstream=capture_cli_launch(
            logical_command="claude",
            launcher_path=downstream,
        ),
        config=capture_config_scope(root_path=config_dir),
    )
    capabilities = snapshot_family_runtime_capabilities(
        family="claude",
        skill_sources={},
        agent_sources={},
        resolved_plan={"harness": {}, "tools": [], "mcp_servers": []},
        extension_state={},
        installation_decisions={},
    )
    return _artifact(
        record,
        descriptor=descriptor,
        contract_payload={
            **launch.to_payload(),
            **family_runtime_capability_payload(capabilities),
        },
    )


def test_all_non_catalog_providers_require_exact_frozen_descriptor() -> None:
    non_catalog = tuple(
        kind
        for kind, spec in SPECS.items()
        if not spec.virtual and kind not in {"codex", "fugu"}
    )
    assert len(non_catalog) == 10
    for kind in non_catalog:
        record = _record(kind)
        contract = _family_contract(record)
        descriptor = issue_model_admission(
            provider=record,
            selected_model="model",
            provider_contract=contract,
        )
        admit_execution_model(_artifact(record, descriptor=descriptor))

        missing = _artifact(record, descriptor=None)
        try:
            admit_execution_model(missing)
        except ModelAdmissionError:
            pass
        else:
            raise AssertionError(f"{kind} accepted a missing descriptor")

        mismatches = (
            {"provider_revision": descriptor["provider_revision"] + 1},
            {"provider_kind": "wrong-kind"},
            {"selected_model": "wrong-model"},
            {"provider_contract_fingerprint": "0" * 64},
        )
        for mutation in mismatches:
            mismatched = {**descriptor, **mutation}
            try:
                admit_execution_model(
                    _artifact(record, descriptor=mismatched),
                )
            except ModelAdmissionError:
                pass
            else:
                raise AssertionError(
                    f"{kind} accepted descriptor mismatch: {mutation}",
                )


def test_descriptor_binds_provider_normalized_runtime_model() -> None:
    record = _record("copilot")
    prepared = prepare_execution(
        record,
        runtime_policy={"runner_input": {"model": "auto"}},
        provider_contract=_family_contract(record),
        **_template("gpt-5.4").arguments(),
    )
    descriptor = prepared.artifact.runtime_policy["model_admission"]
    assert descriptor["selected_model"] == "auto"
    admit_execution_model(prepared.artifact)


def test_rejection_precedes_credential_hydration_and_spawn() -> None:
    class ProbeProvider(Provider):
        KIND = "claude"

        def __init__(self, record: dict) -> None:
            super().__init__(record)
            self.spawns = 0
            self.releases = 0

        def build_env(self) -> dict[str, str]:
            return {}

        def _start_run(self, **kwargs) -> None:
            del kwargs
            self.spawns += 1

        def _write_backend_state(self, rs) -> None:
            del rs

        def _release_execution_authority(self, execution) -> None:
            del execution
            self.releases += 1

        def recover_in_flight(self, loop=None, run_id_filter=None) -> list[dict]:
            del loop, run_id_filter
            return []

        def prune_old_runs(self, max_age_days: int = 7) -> int:
            del max_age_days
            return 0

        async def run_headless(self, *args, **kwargs) -> str:
            del args, kwargs
            return ""

        async def rewind(self, rewind_session_id: str, message_uuid: str) -> None:
            del rewind_session_id, message_uuid

    record = _record("claude")
    provider = ProbeProvider(record)
    execution = _spawn_attested_artifact(record, descriptor=None)
    from execution_spawn_authority import attest_execution_spawn_authority

    attest_execution_spawn_authority(execution)
    import config_store

    hydration_calls = 0
    original_hydrate = config_store.hydrate_provider_execution

    def forbidden_hydration(*args, **kwargs):
        del args, kwargs
        nonlocal hydration_calls
        hydration_calls += 1
        raise AssertionError("model rejection must precede credential hydration")

    config_store.hydrate_provider_execution = forbidden_hydration
    loop = asyncio.new_event_loop()
    try:
        try:
            provider.start_run(
                execution=type("Prepared", (), {})(),
                loop=loop,
                queue=asyncio.Queue(),
            )
        except TypeError:
            pass
        else:
            raise AssertionError("invalid execution was accepted")

        from execution_template import PreparedExecution

        prepared = PreparedExecution(execution, "{}")
        try:
            provider.start_run(
                execution=prepared,
                loop=loop,
                queue=asyncio.Queue(),
            )
        except ModelAdmissionError:
            pass
        else:
            raise AssertionError("missing descriptor was accepted")
    finally:
        loop.close()
        config_store.hydrate_provider_execution = original_hydrate
    assert hydration_calls == 0
    assert provider.spawns == 0
    assert provider.releases == 1


def _write_executable(path: Path) -> None:
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    path.chmod(0o700)


def _codex_fixture(
    root: Path,
    *,
    kind: str = "codex",
    model: str | None = "active-model",
) -> tuple[ExecutionArtifact, object, object]:
    root.mkdir(parents=True, exist_ok=True)
    record = _record(kind)
    config = root / f"{kind}-config"
    config.mkdir()
    (config / "config.toml").write_text("", encoding="utf-8")
    executable = root / "codex"
    _write_executable(executable)
    record.update({
        "config_dir": str(config),
        "mode": "subscription",
    })
    contract = build_codex_execution_contract(
        record,
        launcher_path=str(executable),
    )
    authority = build_catalog_authority(
        provider_id=record["id"],
        provider_generation=record["generation"],
        provider_revision=record["revision"],
        provider_state_authority={
            "generation": str(uuid.uuid4()),
            "revision": 1,
            "digest": "a" * 64,
        },
        execution_contract=contract,
        discovery_method="integration",
        discovery_version=1,
    )
    snapshot = build_catalog_snapshot(
        authority=authority,
        models=["active-model"],
        retired=[{"id": "retired-model", "first_absent_at": 1.0}],
        last_refreshed_at=1.0,
        last_fetch_state="ok",
    )
    prepared = prepare_execution(
        record,
        provider_contract=codex_provider_contract(contract),
        **_template(model).arguments(),
    )
    return prepared.artifact, authority, snapshot


def _project(
    artifact: ExecutionArtifact,
    authority,
    snapshot,
    *,
    status: str = "current",
    current: bool = True,
    reason: str = "",
) -> None:
    model_catalog_read_projection.apply_fact(changed_fact(
        CatalogProjection(
            provider_id=artifact.provider_id,
            provider_generation=artifact.provider_generation,
            status=status,
            models=snapshot.models,
            models_current=current,
            retired=snapshot.retired,
            last_refreshed_at=snapshot.last_refreshed_at,
            reason=reason,
            authority_fingerprint=authority.fingerprint,
        ),
        snapshot,
    ))


def test_codex_and_fugu_require_current_exact_active_catalog() -> None:
    with tempfile.TemporaryDirectory(dir=TEST_HOME) as raw:
        root = Path(raw)
        for kind in ("codex", "fugu"):
            artifact, authority, snapshot = _codex_fixture(
                root / kind,
                kind=kind,
            )
            _project(artifact, authority, snapshot)
            admit_execution_model(artifact)
            retry = prepare_execution(
                {
                    "id": artifact.provider_id,
                    "kind": artifact.provider_kind,
                    "generation": artifact.provider_generation,
                    "revision": artifact.provider_revision,
                },
                provider_contract=artifact.provider_contract,
                runtime_policy=artifact.runtime_policy,
                **artifact.template.arguments(),
            )
            admit_execution_model(retry.artifact)

            for status, current, reason in (
                ("pending", False, "catalog_pending"),
                ("stale", False, "source_changed"),
                ("error", False, "fetch_failed"),
            ):
                _project(
                    artifact,
                    authority,
                    snapshot,
                    status=status,
                    current=current,
                    reason=reason,
                )
                try:
                    admit_execution_model(artifact)
                except ModelAdmissionError:
                    pass
                else:
                    raise AssertionError(f"{kind} accepted {reason}")

            model_catalog_read_projection.apply_fact(
                removed_fact(
                    artifact.provider_id,
                    artifact.provider_generation,
                ),
            )
            try:
                admit_execution_model(artifact)
            except ModelAdmissionError:
                pass
            else:
                raise AssertionError(f"{kind} accepted warming catalog")


def test_old_generation_removal_cannot_invalidate_new_generation() -> None:
    with tempfile.TemporaryDirectory(dir=TEST_HOME) as raw:
        root = Path(raw)
        old_artifact, old_authority, old_snapshot = _codex_fixture(
            root / "old",
        )
        new_artifact, new_authority, new_snapshot = _codex_fixture(
            root / "new",
        )
        assert old_artifact.provider_id == new_artifact.provider_id
        _project(new_artifact, new_authority, new_snapshot)
        model_catalog_read_projection.apply_fact(removed_fact(
            old_artifact.provider_id,
            old_artifact.provider_generation,
        ))
        admit_execution_model(new_artifact)


def test_codex_rejects_retired_absent_default_and_authority_drift() -> None:
    with tempfile.TemporaryDirectory(dir=TEST_HOME) as raw:
        root = Path(raw)
        for selected in ("retired-model", "custom-model", "static-model", None):
            case = root / str(selected)
            case.mkdir()
            artifact, authority, snapshot = _codex_fixture(
                case,
                model=selected,
            )
            _project(artifact, authority, snapshot)
            try:
                admit_execution_model(artifact)
            except ModelAdmissionError:
                pass
            else:
                raise AssertionError(f"Codex accepted {selected!r}")

        case = root / "drift"
        case.mkdir()
        artifact, authority, snapshot = _codex_fixture(case)
        drifted = replace(
            snapshot,
            authority=replace(
                authority,
                provider_revision=authority.provider_revision + 1,
            ),
        )
        _project(artifact, authority, drifted)
        try:
            admit_execution_model(artifact)
        except ModelAdmissionError:
            pass
        else:
            raise AssertionError("Codex accepted catalog revision drift")


if __name__ == "__main__":
    try:
        test_all_non_catalog_providers_require_exact_frozen_descriptor()
        test_descriptor_binds_provider_normalized_runtime_model()
        test_rejection_precedes_credential_hydration_and_spawn()
        test_codex_and_fugu_require_current_exact_active_catalog()
        test_old_generation_removal_cannot_invalidate_new_generation()
        test_codex_rejects_retired_absent_default_and_authority_drift()
        print("PASS provider runtime model admission")
    finally:
        import shutil

        shutil.rmtree(TEST_HOME, ignore_errors=True)
