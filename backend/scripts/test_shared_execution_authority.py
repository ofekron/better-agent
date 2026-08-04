#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import atexit
import json
import sys
import tempfile
import uuid
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import _test_home  # noqa: E402

TEST_HOME = _test_home.TestHome.acquire("ba-shared-execution-authority-")
atexit.register(TEST_HOME.release)

import config_store  # noqa: E402
from execution_template import (  # noqa: E402
    ExecutionArtifact,
    ExecutionAuthorityError,
    prepare_execution,
)
from provider import Provider  # noqa: E402
from provider_manifest import artifact_family_kinds  # noqa: E402


_CREDENTIALS: dict[str, str] = {}


def _credential_request(action: str, provider_id: str, **kwargs) -> dict:
    if action == "read":
        value = _CREDENTIALS.get(provider_id, "")
        return {
            "status": "available" if value else "missing",
            **({"value": value} if value else {}),
        }
    if action == "status":
        return {
            "status": "available"
            if _CREDENTIALS.get(provider_id)
            else "missing",
        }
    if action == "store":
        _CREDENTIALS[provider_id] = kwargs["value"]
        return {"status": "available"}
    if action == "delete":
        _CREDENTIALS.pop(provider_id, None)
        return {"status": "missing"}
    if action == "compare_set":
        current = _CREDENTIALS.get(provider_id, "")
        if current != kwargs["expected_value"]:
            return {
                "status": "available" if current else "missing",
                "applied": False,
            }
        value = kwargs["value"]
        if value:
            _CREDENTIALS[provider_id] = value
            return {"status": "available", "applied": True}
        _CREDENTIALS.pop(provider_id, None)
        return {"status": "missing", "applied": True}
    raise AssertionError(f"unexpected credential action: {action}")


def _record(kind: str, *, provider_id: str | None = None) -> dict:
    return {
        "id": provider_id or f"{kind}-work",
        "kind": kind,
        "generation": str(uuid.uuid4()),
        "revision": 4,
        "execution_revision": 3,
        "mode": "subscription",
    }


def _arguments(*, run_id: str | None = None, extra_env=None) -> dict:
    return {
        "run_id": run_id or str(uuid.uuid4()),
        "prompt": "finish the task",
        "cwd": "/workspace",
        "model": "model",
        "reasoning_effort": None,
        "session_id": "native-session",
        "mode": "native",
        "app_session_id": "app-session",
        "extra_env": extra_env,
    }


def _input_projection(artifact: ExecutionArtifact) -> dict:
    arguments = artifact.template.arguments()
    return {
        "prompt": arguments["prompt"],
        "cwd": arguments["cwd"],
        "model": arguments["model"],
        "session_id": arguments["session_id"],
        "mode": arguments["mode"],
        "app_session_id": arguments["app_session_id"],
        "provider_id": artifact.provider_id,
    }


class _Provider(Provider):
    KIND = "claude"

    def __init__(self, record: dict) -> None:
        super().__init__(record)
        self.spawned = False
        self.runtime_snapshot: dict | None = None
        self.released = 0

    def build_env(self) -> dict[str, str]:
        self.runtime_snapshot = dict(self.runtime_record())
        return {}

    def prepare_run(self, **arguments):
        from provider_execution_contract import provider_family_contract

        return prepare_execution(
            self.record,
            provider_contract=provider_family_contract(
                self.record,
                payload={"test": "shared-execution-authority"},
            ),
            **arguments,
        )

    def _start_run(self, **kwargs) -> bool:
        del kwargs
        self.build_env()
        self.spawned = True
        return True

    def _write_backend_state(self, rs) -> None:
        del rs

    def _release_execution_authority(self, execution) -> None:
        del execution
        self.released += 1

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


def test_family_contract_codec_is_strict_immutable_and_tamper_bound() -> None:
    from provider_execution_contract import provider_family_contract

    for kind in artifact_family_kinds():
        record = _record(kind)
        prepared = prepare_execution(
            record,
            provider_contract=provider_family_contract(
                record,
                payload={"launch_family": kind},
            ),
            **_arguments(),
        )
        encoded = prepared.artifact.to_dict()
        assert ExecutionArtifact.from_dict(
            json.loads(json.dumps(encoded)),
        ) == prepared.artifact
        encoded["provider_contract"]["contract"]["payload"][
            "launch_family"
        ] = "tampered"
        try:
            ExecutionArtifact.from_dict(encoded)
        except ExecutionAuthorityError:
            pass
        else:
            raise AssertionError(f"tampered {kind} contract was accepted")


def test_atomic_hydration_separates_bound_sensitive_credential() -> None:
    from provider_execution_authority import (
        ProviderExecutionCredential,
        ProviderExecutionHydration,
    )

    config_store.credential_session_client.available = lambda: True
    config_store.credential_session_client.request = _credential_request
    created = config_store.add_provider({
        "name": "Credential-bound Claude",
        "kind": "claude",
        "mode": "api_key",
        "api_key": "sensitive-value",
    })
    hydration = config_store.hydrate_provider_execution(
        created["id"],
        expected_generation=created["generation"],
        expected_execution_revision=created["execution_revision"],
    )
    assert isinstance(hydration, ProviderExecutionHydration)
    snapshot = hydration.provider
    assert snapshot["id"] == created["id"]
    assert "api_key" not in snapshot
    assert "_credential_authoritative" not in snapshot
    assert isinstance(hydration.credential, ProviderExecutionCredential)
    assert hydration.credential.provider_id == created["id"]
    assert hydration.credential.provider_generation == created["generation"]
    assert hydration.credential.provider_kind == created["kind"]
    assert (
        hydration.credential.provider_execution_revision
        == created["execution_revision"]
    )
    assert hydration.credential.api_key == "sensitive-value"


def test_base_admission_allows_metadata_only_prepare_to_start_change() -> None:
    import execution_spawn_authority

    created = config_store.add_provider({
        "name": "Race-bound Claude",
        "kind": "claude",
        "mode": "subscription",
    })
    provider = _Provider(created)
    prepared = provider.prepare_run(**_arguments())
    changed = config_store.update_provider(
        created["id"],
        {"nickname": "changed after prepare"},
        expected_generation=created["generation"],
        expected_revision=created["revision"],
    )
    assert changed is not None
    loop = asyncio.new_event_loop()
    original_attest = execution_spawn_authority.attest_execution_spawn_authority
    execution_spawn_authority.attest_execution_spawn_authority = (
        lambda _artifact: None
    )
    try:
        assert provider.start_run(
            execution=prepared,
            loop=loop,
            queue=asyncio.Queue(),
        )
    finally:
        execution_spawn_authority.attest_execution_spawn_authority = original_attest
        loop.close()
    assert provider.spawned
    assert provider.released == 1


def test_base_admission_rejects_execution_change_after_prepare() -> None:
    import execution_spawn_authority

    created = config_store.add_provider({
        "name": "Execution-bound Claude",
        "kind": "claude",
        "mode": "subscription",
    })
    provider = _Provider(created)
    prepared = provider.prepare_run(**_arguments())
    changed = config_store.update_provider(
        created["id"],
        {"base_url": "https://example.test"},
        expected_generation=created["generation"],
        expected_revision=created["revision"],
    )
    assert changed is not None
    assert (
        changed["execution_revision"]
        == created["execution_revision"] + 1
    )
    loop = asyncio.new_event_loop()
    original_attest = execution_spawn_authority.attest_execution_spawn_authority
    execution_spawn_authority.attest_execution_spawn_authority = (
        lambda _artifact: None
    )
    try:
        try:
            provider.start_run(
                execution=prepared,
                loop=loop,
                queue=asyncio.Queue(),
            )
        except config_store.ProviderConfigConflict:
            pass
        else:
            raise AssertionError("stale execution authority reached spawn")
    finally:
        execution_spawn_authority.attest_execution_spawn_authority = original_attest
        loop.close()
    assert not provider.spawned
    assert provider.released == 1


def test_base_admission_rejects_wrong_provider_instance() -> None:
    owner = _record("claude", provider_id="execution-owner")
    execution = prepare_execution(owner, **_arguments())
    provider = _Provider(_record("claude", provider_id="wrong-provider"))
    loop = asyncio.new_event_loop()
    try:
        try:
            provider.start_run(
                execution=execution,
                loop=loop,
                queue=asyncio.Queue(),
            )
        except ExecutionAuthorityError:
            pass
        else:
            raise AssertionError("wrong provider instance admitted execution")
    finally:
        loop.close()
    assert not provider.spawned
    assert provider.released == 0


def test_extra_env_rejects_launch_runtime_and_authority_collisions() -> None:
    record = _record("claude")
    for key in (
        "PATH",
        "Path",
        "HOME",
        "PYTHONPATH",
        "CLAUDE_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "AMP_API_KEY",
        "AMP_URL",
        "CURSOR_API_KEY",
        "KIMI_API_KEY",
        "GITHUB_TOKEN",
        "BETTER_AGENT_INTERNAL_TOKEN",
    ):
        try:
            prepare_execution(
                record,
                **_arguments(extra_env={key: "override"}),
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"protected extra_env key was accepted: {key}")
    prepared = prepare_execution(
        record,
        **_arguments(extra_env={"BU_CDP_URL": "http://127.0.0.1:9222"}),
    )
    assert prepared.start_arguments()["extra_env"] == {
        "BU_CDP_URL": "http://127.0.0.1:9222",
    }


def test_family_artifact_and_input_projection_fail_closed() -> None:
    from execution_artifact_io import bind_execution_input, load_execution_artifact
    from provider_execution_contract import provider_family_contract

    for kind in ("claude", "agy"):
        record = _record(kind)
        artifact = prepare_execution(
            record,
            provider_contract=provider_family_contract(
                record,
                payload={"launch_family": kind},
            ),
            **_arguments(),
        ).artifact
        with tempfile.TemporaryDirectory() as raw:
            run_dir = Path(raw)
            (run_dir / "execution.json").write_text(
                json.dumps(artifact.to_dict()),
                encoding="utf-8",
            )
            (run_dir / "input.json").write_text(
                json.dumps(bind_execution_input(
                    artifact,
                    _input_projection(artifact),
                )),
                encoding="utf-8",
            )
            assert load_execution_artifact(
                run_dir,
                validate_input=True,
            ) == artifact

            poisoned_input = bind_execution_input(
                artifact,
                _input_projection(artifact),
            )
            poisoned_input["cwd"] = "/tampered"
            (run_dir / "input.json").write_text(
                json.dumps(poisoned_input),
                encoding="utf-8",
            )
            try:
                load_execution_artifact(run_dir, validate_input=True)
            except ExecutionAuthorityError:
                pass
            else:
                raise AssertionError(f"tampered {kind} input was accepted")

            poisoned_artifact = artifact.to_dict()
            poisoned_artifact["template"]["arguments"]["model"] = "tampered"
            (run_dir / "execution.json").write_text(
                json.dumps(poisoned_artifact),
                encoding="utf-8",
            )
            try:
                load_execution_artifact(run_dir, validate_input=True)
            except ExecutionAuthorityError:
                pass
            else:
                raise AssertionError(f"tampered {kind} artifact was accepted")


TESTS = (
    test_family_contract_codec_is_strict_immutable_and_tamper_bound,
    test_atomic_hydration_separates_bound_sensitive_credential,
    test_base_admission_allows_metadata_only_prepare_to_start_change,
    test_base_admission_rejects_execution_change_after_prepare,
    test_base_admission_rejects_wrong_provider_instance,
    test_extra_env_rejects_launch_runtime_and_authority_collisions,
    test_family_artifact_and_input_projection_fail_closed,
)


if __name__ == "__main__":
    failures: list[str] = []
    for test in TESTS:
        try:
            test()
        except BaseException as exc:
            failures.append(f"{test.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        raise AssertionError("\n".join(failures))
    TEST_HOME.release()
    print("PASS shared execution authority")
