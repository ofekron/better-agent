from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402
import codex_execution_runtime as execution_runtime  # noqa: E402
import config_store  # noqa: E402
import extension_store  # noqa: E402
import installation_profile  # noqa: E402
import provider_codex as provider_codex_module  # noqa: E402
import runtime_skills  # noqa: E402

_TEST_HOME = _test_home.isolate("bc-test-codex-execution-artifact-")

from codex_execution_runtime import (  # noqa: E402
    cleanup_installed_codex_runtime_agent_payload,
    cleanup_staged_codex_runtime_agent_payload,
    clone_codex_runtime_agent_payload,
    codex_contract_from_artifact,
    codex_runner_launch_from_artifact,
    codex_runtime_agent_manifest,
    install_staged_codex_runtime_agent_payload,
    read_codex_runtime_agent_payload,
    restore_codex_runner_inputs,
    retry_codex_execution,
    snapshot_codex_runtime_agents,
    stage_codex_runtime_agent_payload,
)
from execution_spawn_authority import attest_execution_spawn_authority  # noqa: E402
from execution_template import (  # noqa: E402
    ExecutionArtifact,
    ExecutionAuthorityError,
)
from provider_codex import CodexProvider  # noqa: E402
from provider_execution_authority import ProviderExecutionHydration  # noqa: E402
from provider_fugu import FuguProvider  # noqa: E402
import runner_codex  # noqa: E402
from runs_dir import runs_root  # noqa: E402
from model_catalog_authority import build_catalog_authority  # noqa: E402
from model_catalog_cache import build_catalog_snapshot  # noqa: E402
from model_catalog_refresh_state import CatalogProjection, changed_fact  # noqa: E402
import model_catalog_read_projection  # noqa: E402
from native_sid_compatibility import admitted_native_node  # noqa: E402


def _write_executable(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _record(*, kind: str, config_dir: Path, revision: int = 7) -> dict:
    return {
        "id": f"{kind}-provider",
        "kind": kind,
        "generation": str(uuid.uuid4()),
        "revision": revision,
        "execution_revision": revision,
        "name": kind,
        "mode": "api_key" if kind == "fugu" else "subscription",
        "base_url": "",
        "config_dir": str(config_dir),
        "runner": "native",
        "api_key": "secret-not-persisted" if kind == "fugu" else "",
        "_credential_authoritative": kind == "fugu",
    }


def _nonsecret_record(record: dict) -> dict:
    return {
        key: value
        for key, value in record.items()
        if key not in {"api_key", "_credential_authoritative"}
    }


def _arguments(root: Path, *, model: str) -> dict:
    return {
        "run_id": str(uuid.uuid4()),
        "prompt": "complete the task",
        "cwd": str(root),
        "model": model,
        "reasoning_effort": "high",
        "session_id": None,
        "mode": "native",
        "app_session_id": "app-session",
        "internal_token": "volatile-token",
        "backend_url": "http://127.0.0.1:8000",
        "extra_env": None,
        "provider_run_config": {
            "mcp_servers": {
                "example": {"command": "example-mcp", "args": ["serve"]},
            },
        },
        "resolved_harness_run_config": {
            "profile_id": "reviewer",
            "profile_revision": "revision-1",
            "provider_run_config": {},
        },
    }


def _prepare(
    provider: CodexProvider,
    root: Path,
    *,
    model: str,
    node_id: str = "primary",
):
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join((str(root / "bin"), old_path))
    try:
        with (
            admitted_native_node(node_id),
            patch("local_machine_identity._local_machine_id", node_id),
        ):
            prepared = provider.prepare_run(**_arguments(root, model=model))
    finally:
        os.environ["PATH"] = old_path
    contract = codex_contract_from_artifact(prepared.artifact)
    authority = build_catalog_authority(
        provider_id=prepared.artifact.provider_id,
        provider_generation=prepared.artifact.provider_generation,
        provider_revision=prepared.artifact.provider_revision,
        provider_state_authority={
            "generation": str(uuid.uuid4()),
            "revision": 1,
            "digest": "a" * 64,
        },
        execution_contract=replace(contract, runtime_args=()),
        discovery_method="artifact_test",
        discovery_version=1,
    )
    snapshot = build_catalog_snapshot(
        authority=authority,
        models=[model],
        retired=[],
        last_refreshed_at=1.0,
        last_fetch_state="ok",
    )
    model_catalog_read_projection.apply_fact(changed_fact(
        CatalogProjection(
            provider_id=prepared.artifact.provider_id,
            provider_generation=prepared.artifact.provider_generation,
            status="current",
            models=snapshot.models,
            models_current=True,
            retired=(),
            last_refreshed_at=snapshot.last_refreshed_at,
            reason="",
            authority_fingerprint=authority.fingerprint,
        ),
        snapshot,
    ))
    return prepared


def test_codex_prepare_binds_runtime_policy_and_contract() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        config = config_dir / "config.toml"
        config.write_text('model_provider = "openai"\n', encoding="utf-8")
        _write_executable(root / "bin" / "codex", b"native-codex")
        provider = CodexProvider(_record(kind="codex", config_dir=config_dir))

        prepared = _prepare(provider, root, model="gpt-5.5")
        contract = codex_contract_from_artifact(prepared.artifact)
        encoded = prepared.artifact.to_dict()
        serialized = json.dumps(encoded, sort_keys=True)

        assert contract.provider_id == provider.id
        assert contract.provider_kind == "codex"
        assert contract.provider_revision == prepared.artifact.provider_revision
        assert (
            contract.provider_execution_revision
            == prepared.artifact.provider_execution_revision
        )
        assert (
            contract.credential_generation
            == contract.provider_execution_revision
        )
        assert contract.launch_chain.argv_prefix
        runner_launch = codex_runner_launch_from_artifact(prepared.artifact)
        assert runner_launch.attest()
        assert runner_launch.launch.argv[-2:] == (
            "--run-dir",
            str(runs_root() / prepared.start_arguments()["run_id"]),
        )
        assert contract.config
        assert prepared.artifact.runtime_policy["permission"]
        compatibility = prepared.artifact.runtime_policy[
            "native_sid_compatibility"
        ]
        assert compatibility == {
            "schema": 1,
            "engine": "codex-native",
            "node_id": "primary",
            "thread_store_root": str((config_dir / "sessions").resolve()),
            "claude_project_namespace": None,
        }
        assert prepared.artifact.runtime_policy["run_policy"][
            "resolved_harness_run_config"
        ]["profile_id"] == "reviewer"
        assert "volatile-token" not in serialized
        assert "secret-not-persisted" not in serialized
        assert (
            ExecutionArtifact.from_dict(json.loads(json.dumps(encoded)))
            == prepared.artifact
        )
        retry_run_id = str(uuid.uuid4())
        retry = retry_codex_execution(
            prepared,
            run_id=retry_run_id,
            session_id="resume-exact",
            fork=True,
        )
        assert retry.artifact.provider_contract == prepared.artifact.provider_contract
        retry_runner = codex_runner_launch_from_artifact(retry.artifact)
        assert retry_runner.launch.argv[-2:] == (
            "--run-dir",
            str(runs_root() / retry_run_id),
        )
        assert retry.start_arguments()["session_id"] == "resume-exact"
        assert retry.start_arguments()["fork"] is True
        assert (
            retry.artifact.runtime_policy["runner_launch"]
            != prepared.artifact.runtime_policy["runner_launch"]
        )

        config.write_text('model_provider = "changed"\n', encoding="utf-8")
        assert not contract.attest()


def test_runner_drift_is_rejected_before_credentials_or_spawn() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("", encoding="utf-8")
        _write_executable(root / "bin" / "codex", b"native-codex")
        runner_copy = root / "runner_codex.py"
        runner_copy.write_bytes(provider_codex_module._RUNNER_PATH.read_bytes())
        original_runner = provider_codex_module._RUNNER_PATH
        original_hydrate = config_store.hydrate_provider_execution
        original_popen = provider_codex_module.subprocess.Popen
        provider_codex_module._RUNNER_PATH = runner_copy
        try:
            provider = CodexProvider(
                _record(kind="codex", config_dir=config_dir),
            )
            prepared = _prepare(provider, root, model="gpt-5.5")
            observed = runner_copy.stat()
            runner_copy.write_bytes(b"x" * observed.st_size)
            os.utime(
                runner_copy,
                ns=(observed.st_atime_ns, observed.st_mtime_ns),
            )
            calls = {"hydrate": 0, "spawn": 0}

            def _hydrate(*_args, **_kwargs):
                calls["hydrate"] += 1
                raise AssertionError("credentials were hydrated after drift")

            def _popen(*_args, **_kwargs):
                calls["spawn"] += 1
                raise AssertionError("subprocess spawned after drift")

            config_store.hydrate_provider_execution = _hydrate
            provider_codex_module.subprocess.Popen = _popen
            try:
                attest_execution_spawn_authority(prepared.artifact)
            except ExecutionAuthorityError:
                pass
            else:
                raise AssertionError("same-stat runner replacement was accepted")
            loop = asyncio.new_event_loop()
            try:
                try:
                    provider.start_run(
                        execution=prepared,
                        loop=loop,
                        queue=asyncio.Queue(),
                    )
                except ExecutionAuthorityError:
                    pass
                else:
                    raise AssertionError("drifted runner execution was admitted")
            finally:
                loop.close()
            assert calls == {"hydrate": 0, "spawn": 0}
        finally:
            provider_codex_module._RUNNER_PATH = original_runner
            config_store.hydrate_provider_execution = original_hydrate
            provider_codex_module.subprocess.Popen = original_popen


def test_nested_contract_must_match_enclosing_artifact() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("", encoding="utf-8")
        _write_executable(root / "bin" / "codex", b"native-codex")
        prepared = _prepare(
            CodexProvider(_record(kind="codex", config_dir=config_dir)),
            root,
            model="gpt-5.5",
        )
        encoded = prepared.artifact.to_dict()
        nested = encoded["provider_contract"]["contract"]
        nested["provider_kind"] = "fugu"
        nested_payload = dict(nested)
        nested_payload.pop("fingerprint")
        nested["fingerprint"] = hashlib.sha256(
            json.dumps(
                nested_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()
        outer_payload = dict(encoded)
        outer_payload.pop("fingerprint")
        encoded["fingerprint"] = hashlib.sha256(
            json.dumps(
                outer_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()

        try:
            ExecutionArtifact.from_dict(encoded)
        except Exception:
            pass
        else:
            raise AssertionError("cross-provider nested authority was accepted")


def test_runner_restores_artifact_authority_not_poisoned_input() -> None:
    from execution_artifact_io import bind_execution_input

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        run_dir = root / "run"
        run_dir.mkdir()
        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("", encoding="utf-8")
        _write_executable(root / "bin" / "codex", b"native-codex")
        prepared = _prepare(
            CodexProvider(_record(kind="codex", config_dir=config_dir)),
            root,
            model="gpt-5.5",
            node_id="worker-eu-2",
        )
        (run_dir / "execution.json").write_text(
            json.dumps(prepared.artifact.to_dict()),
            encoding="utf-8",
        )
        poisoned = bind_execution_input(prepared.artifact, {
            **prepared.artifact.template.arguments(),
            "codex_config_overrides": ['model_provider="sakana"'],
            "internal_token": "volatile-token",
            "backend_url": "http://127.0.0.1:8000",
        })
        poisoned["model"] = "poisoned-model"
        poisoned["provider_kind"] = "fugu"

        try:
            restore_codex_runner_inputs(run_dir, poisoned)
        except Exception:
            pass
        else:
            raise AssertionError("poisoned runner input was accepted")

        valid = bind_execution_input(prepared.artifact, {
            **prepared.artifact.template.arguments(),
            "internal_token": "volatile-token",
            "backend_url": "http://127.0.0.1:8000",
        })
        restored, contract = restore_codex_runner_inputs(run_dir, valid)
        assert restored["model"] == "gpt-5.5"
        assert restored["permission"] == prepared.artifact.runtime_policy["permission"]
        assert restored["native_sid_compatibility"]["node_id"] == "worker-eu-2"
        assert restored["provider_kind"] == "codex"
        assert contract == codex_contract_from_artifact(prepared.artifact)
        valid["_mcp_prewarm_ready"] = {
            "selected-server": None,
        }
        restored, _ = restore_codex_runner_inputs(run_dir, valid)
        assert restored["_mcp_prewarm_ready"] == {
            "selected-server": None,
        }
        valid["_mcp_prewarm_ready"] = {
            "selected-server": "/tmp/poisoned.sock",
        }
        try:
            restore_codex_runner_inputs(run_dir, valid)
        except ExecutionAuthorityError:
            pass
        else:
            raise AssertionError("poisoned MCP prewarm endpoint was accepted")


def test_codex_runner_initializes_machine_before_skill_materialization() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "source"
        source.mkdir()
        (source / "SKILL.md").write_text(
            "---\nname: machine\ndescription: Use {{better_agent.machine_id}}.\n---\n",
            encoding="utf-8",
        )
        compatibility = {
            "schema": 1,
            "engine": "codex-native",
            "node_id": "worker-eu-2",
            "thread_store_root": str((root / "sessions").resolve()),
            "claude_project_namespace": None,
        }
        contract = SimpleNamespace(
            environment_selectors={"CODEX_HOME": str(root / "config")},
        )

        def materialize(run_name: str) -> Path:
            run_dir = root / run_name
            with (
                patch.object(runner_codex, "symlink_home_overlay"),
                patch.object(runner_codex, "_overlay_codex_home_children"),
                patch.object(
                    execution_runtime,
                    "attested_config_files",
                    return_value=(),
                ),
                patch.object(
                    execution_runtime,
                    "read_codex_runtime_agent_payload",
                    return_value=(),
                ),
                patch.object(
                    installation_profile,
                    "integrations_enabled",
                    return_value=True,
                ),
                patch.object(
                    runtime_skills,
                    "_extension_runtime_skills",
                    return_value=[{
                        "name": "machine",
                        "dir": str(source),
                        "path": str(source / "SKILL.md"),
                        "template_variables": ["machine_id"],
                    }],
                ),
            ):
                runtime_skills._DISCOVERY_CACHE.clear()
                try:
                    runner_codex._materialize_codex_run_home(
                        run_dir,
                        {},
                        execution_contract=contract,
                        cwd=str(root),
                        machine_id=compatibility["node_id"],
                    )
                finally:
                    runtime_skills._DISCOVERY_CACHE.clear()
            return run_dir / "codex-home" / ".agents" / "skills" / "machine" / "SKILL.md"

        with patch("local_machine_identity._local_machine_id", None):
            try:
                materialize("uninitialized")
            except RuntimeError:
                pass
            else:
                raise AssertionError("uninitialized Codex runner identity was accepted")

            assert (
                runner_codex._initialize_codex_runner_machine_identity({
                    "native_sid_compatibility": compatibility,
                })
                == "worker-eu-2"
            )
            installed = materialize("matched").read_text(encoding="utf-8")
            assert "worker-eu-2" in installed
            assert "{{better_agent.machine_id}}" not in installed

        with patch("local_machine_identity._local_machine_id", "worker-us-1"):
            try:
                runner_codex._initialize_codex_runner_machine_identity({
                    "native_sid_compatibility": compatibility,
                })
            except RuntimeError:
                pass
            else:
                raise AssertionError("mismatched Codex runner identity was accepted")


def test_fugu_isolation_and_invalid_model_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("", encoding="utf-8")
        _write_executable(root / "bin" / "codex", b"native-codex")
        provider = FuguProvider(_record(kind="fugu", config_dir=config_dir))

        prepared = _prepare(provider, root, model="fugu-ultra")
        contract = codex_contract_from_artifact(prepared.artifact)
        assert contract.provider_kind == "fugu"
        assert prepared.artifact.runtime_policy[
            "native_sid_compatibility"
        ]["engine"] == "codex-native"
        assert contract.runtime_args == (
            "-c",
            'model_provider="sakana"',
            "-c",
            'model="fugu-ultra"',
            "-c",
            "features.image_generation=false",
            "-c",
            "features.shell_snapshot=false",
            "-c",
            'shell_environment_policy.exclude=["SAKANA_API_KEY"]',
        )

        try:
            _prepare(provider, root, model="gpt-5.5")
        except ValueError:
            pass
        else:
            raise AssertionError("Fugu silently fell back from an invalid model")


def test_recovery_consumes_execution_artifact_authority() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("", encoding="utf-8")
        _write_executable(root / "bin" / "codex", b"native-codex")
        provider = CodexProvider(
            _record(kind="codex", config_dir=config_dir),
        )
        prepared = _prepare(provider, root, model="gpt-5.5")
        arguments = prepared.artifact.template.arguments()
        run_dir = runs_root() / arguments["run_id"]
        run_dir.mkdir(parents=True)
        (run_dir / "execution.json").write_text(
            json.dumps(prepared.artifact.to_dict()),
            encoding="utf-8",
        )
        (run_dir / "state.json").write_text(
            json.dumps({
                "app_session_id": "poisoned-session",
                "mode": "manager",
                "session_id": "native-thread",
            }),
            encoding="utf-8",
        )
        (run_dir / "backend_state.json").write_text(
            json.dumps({
                "app_session_id": "poisoned-session",
                "mode": "manager",
                "provider_id": "poisoned-provider",
                "target_message_id": "poisoned-message",
            }),
            encoding="utf-8",
        )
        (run_dir / "complete.json").write_text(
            json.dumps({
                "success": True,
                "session_id": "native-thread",
            }),
            encoding="utf-8",
        )

        recovered = provider.recover_in_flight(
            run_id_filter={arguments["run_id"]},
        )

        assert len(recovered) == 1
        assert recovered[0]["app_session_id"] == arguments["app_session_id"]
        assert recovered[0]["provider_id"] == provider.id
        assert recovered[0]["provider_kind"] == "codex"
        assert recovered[0]["mode"] == arguments["mode"]
        assert recovered[0]["target_message_id"] == arguments[
            "target_message_id"
        ]

        encoded = prepared.artifact.to_dict()
        encoded.pop("fingerprint")
        (run_dir / "execution.json").write_text(
            json.dumps(encoded),
            encoding="utf-8",
        )
        assert provider.recover_in_flight(
            run_id_filter={arguments["run_id"]},
        ) == []


def test_completed_recovery_does_not_require_current_spawn_authority() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("", encoding="utf-8")
        codex_binary = root / "bin" / "codex"
        _write_executable(codex_binary, b"native-codex")
        provider = CodexProvider(
            _record(kind="codex", config_dir=config_dir),
        )
        prepared = _prepare(provider, root, model="gpt-5.5")
        arguments = prepared.artifact.template.arguments()
        run_dir = runs_root() / arguments["run_id"]
        run_dir.mkdir(parents=True)
        (run_dir / "execution.json").write_text(
            json.dumps(prepared.artifact.to_dict()),
            encoding="utf-8",
        )
        (run_dir / "state.json").write_text(
            json.dumps({"session_id": "native-thread"}),
            encoding="utf-8",
        )
        (run_dir / "complete.json").write_text(
            json.dumps({"success": True, "session_id": "native-thread"}),
            encoding="utf-8",
        )

        codex_binary.write_bytes(b"updated-native-codex")
        recovered = provider.recover_in_flight(
            run_id_filter={arguments["run_id"]},
        )
        assert len(recovered) == 1
        assert recovered[0]["has_complete_json"] is True

        (run_dir / "complete.json").unlink()
        assert provider.recover_in_flight(
            run_id_filter={arguments["run_id"]},
        ) == []


def test_prepare_to_spawn_provider_mutation_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("", encoding="utf-8")
        _write_executable(root / "bin" / "codex", b"native-codex")
        provider = CodexProvider(
            _record(kind="codex", config_dir=config_dir),
        )
        prepared = _prepare(provider, root, model="gpt-5.5")
        provider.record = {
            **provider.record,
            "execution_revision": (
                provider.record["execution_revision"] + 1
            ),
        }
        original_hydrate = config_store.hydrate_provider_execution
        config_store.hydrate_provider_execution = (
            lambda *_args, **_kwargs: ProviderExecutionHydration.create(
                _nonsecret_record(provider.record),
            )
        )
        loop = asyncio.new_event_loop()
        try:
            try:
                provider.start_run(
                    execution=prepared,
                    loop=loop,
                    queue=asyncio.Queue(),
                )
            except ExecutionAuthorityError:
                pass
            else:
                raise AssertionError(
                    "provider mutation after prepare reached spawn",
                )
        finally:
            loop.close()
            config_store.hydrate_provider_execution = original_hydrate


def test_runtime_agent_payload_is_immutable_confined_and_secret_free() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("", encoding="utf-8")
        _write_executable(root / "bin" / "codex", b"native-codex")
        source = root / "extension" / "reviewer.toml"
        source.parent.mkdir()
        secret_marker = "RUNTIME_AGENT_SECRET_MARKER"
        source.write_text(secret_marker, encoding="utf-8")
        original_entries = extension_store.runtime_agent_entries
        extension_store.runtime_agent_entries = lambda: [
            {"name": "reviewer", "codex": str(source)},
        ]
        try:
            prepared = _prepare(
                CodexProvider(
                    _record(kind="codex", config_dir=config_dir),
                ),
                root,
                model="gpt-5.5",
            )
            manifest = codex_runtime_agent_manifest(prepared.artifact)
            assert manifest is not None
            serialized = json.dumps(prepared.artifact.to_dict())
            assert secret_marker not in serialized

            source.write_text("mutated-after-prepare", encoding="utf-8")
            run_dir = root / "run"
            run_dir.mkdir()
            run_id = prepared.artifact.template.arguments()["run_id"]
            install_staged_codex_runtime_agent_payload(
                run_dir,
                run_id=run_id,
                manifest=manifest,
            )
            payload_path = run_dir / manifest["path"]
            assert stat.S_IMODE(payload_path.stat().st_mode) & 0o077 == 0
            assert read_codex_runtime_agent_payload(
                run_dir,
                manifest,
            ) == (("reviewer.toml", secret_marker.encode()),)
            run_env = runner_codex._materialize_codex_run_home(
                run_dir,
                {},
                execution_contract=codex_contract_from_artifact(
                    prepared.artifact,
                ),
                runtime_agent_manifest=manifest,
                cwd=str(root),
            )
            assert (
                Path(run_env["CODEX_HOME"])
                / "agents"
                / "reviewer.toml"
            ).read_bytes() == secret_marker.encode()
            assert payload_path.exists()

            encoded = prepared.artifact.to_dict()
            encoded["runtime_policy"]["runtime_agent_manifest"]["path"] = (
                "../escape"
            )
            outer = dict(encoded)
            outer.pop("fingerprint")
            encoded["fingerprint"] = hashlib.sha256(
                json.dumps(
                    outer,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode(),
            ).hexdigest()
            poisoned = ExecutionArtifact.from_dict(encoded)
            try:
                codex_runtime_agent_manifest(poisoned)
            except ExecutionAuthorityError:
                pass
            else:
                raise AssertionError(
                    "runtime agent payload traversal was accepted",
                )
        finally:
            extension_store.runtime_agent_entries = original_entries
            cleanup_staged_codex_runtime_agent_payload(
                prepared.artifact.template.arguments()["run_id"],
            )


def test_runtime_agent_payload_rejects_tamper_symlink_and_atomic_failure() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "agent.toml"
        source.write_text("prepared-agent", encoding="utf-8")
        original_entries = extension_store.runtime_agent_entries
        extension_store.runtime_agent_entries = lambda: [
            {"name": "agent", "codex": str(source)},
        ]
        try:
            manifest, payload = snapshot_codex_runtime_agents(enabled=True)
            assert manifest is not None
            assert payload is not None

            tamper_run_id = str(uuid.uuid4())
            stage_codex_runtime_agent_payload(
                run_id=tamper_run_id,
                manifest=manifest,
                payload=payload,
            )
            tamper_dir = root / "tamper"
            tamper_dir.mkdir()
            install_staged_codex_runtime_agent_payload(
                tamper_dir,
                run_id=tamper_run_id,
                manifest=manifest,
            )
            payload_path = tamper_dir / manifest["path"]
            payload_path.write_bytes(b"tampered")
            try:
                read_codex_runtime_agent_payload(tamper_dir, manifest)
            except ExecutionAuthorityError:
                pass
            else:
                raise AssertionError("tampered runtime agent payload was accepted")
            cleanup_installed_codex_runtime_agent_payload(tamper_dir)

            symlink_run_id = str(uuid.uuid4())
            stage_codex_runtime_agent_payload(
                run_id=symlink_run_id,
                manifest=manifest,
                payload=payload,
            )
            symlink_dir = root / "symlink"
            symlink_dir.mkdir()
            install_staged_codex_runtime_agent_payload(
                symlink_dir,
                run_id=symlink_run_id,
                manifest=manifest,
            )
            installed = symlink_dir / manifest["path"]
            installed.unlink()
            target = root / "outside"
            target.write_bytes(payload)
            target.chmod(0o600)
            installed.symlink_to(target)
            try:
                read_codex_runtime_agent_payload(symlink_dir, manifest)
            except ExecutionAuthorityError:
                pass
            else:
                raise AssertionError("symlinked runtime agent payload was accepted")
            cleanup_installed_codex_runtime_agent_payload(symlink_dir)

            failed_run_id = str(uuid.uuid4())
            original_replace = execution_runtime.os.replace
            execution_runtime.os.replace = (
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    OSError("atomic replacement failed"),
                )
            )
            try:
                try:
                    stage_codex_runtime_agent_payload(
                        run_id=failed_run_id,
                        manifest=manifest,
                        payload=payload,
                    )
                except OSError:
                    pass
                else:
                    raise AssertionError("atomic payload failure was ignored")
            finally:
                execution_runtime.os.replace = original_replace
            cleanup_staged_codex_runtime_agent_payload(failed_run_id)
        finally:
            extension_store.runtime_agent_entries = original_entries


def test_runtime_agent_payload_is_removed_on_failed_admission() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("", encoding="utf-8")
        _write_executable(root / "bin" / "codex", b"native-codex")
        source = root / "agent.toml"
        source.write_text("prepared-agent", encoding="utf-8")
        original_entries = extension_store.runtime_agent_entries
        extension_store.runtime_agent_entries = lambda: [
            {"name": "agent", "codex": str(source)},
        ]
        original_hydrate = config_store.hydrate_provider_execution
        try:
            provider = CodexProvider(
                _record(kind="codex", config_dir=config_dir),
            )
            prepared = _prepare(provider, root, model="gpt-5.5")
            run_id = prepared.artifact.template.arguments()["run_id"]
            provider.record = {
                **provider.record,
                "execution_revision": (
                    provider.record["execution_revision"] + 1
                ),
            }
            config_store.hydrate_provider_execution = (
                lambda *_args, **_kwargs: ProviderExecutionHydration.create(
                    _nonsecret_record(provider.record),
                )
            )
            loop = asyncio.new_event_loop()
            try:
                try:
                    provider.start_run(
                        execution=prepared,
                        loop=loop,
                        queue=asyncio.Queue(),
                    )
                except ExecutionAuthorityError:
                    pass
                else:
                    raise AssertionError("mutated provider was admitted")
            finally:
                loop.close()
            from paths import bc_home

            staging_key = hashlib.sha256(run_id.encode()).hexdigest()
            staged = (
                bc_home()
                / "prepared-execution-payloads"
                / staging_key
                / "codex-runtime-agents.json"
            )
            assert not staged.exists()
        finally:
            extension_store.runtime_agent_entries = original_entries
            config_store.hydrate_provider_execution = original_hydrate


def test_recovery_reuses_exact_runtime_agent_payload_and_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("", encoding="utf-8")
        _write_executable(root / "bin" / "codex", b"native-codex")
        source = root / "reviewer.toml"
        original_contents = b"prepared-runtime-agent"
        source.write_bytes(original_contents)
        original_entries = extension_store.runtime_agent_entries
        extension_store.runtime_agent_entries = lambda: [
            {"name": "reviewer", "codex": str(source)},
        ]
        target_run_id = str(uuid.uuid4())
        reconcile_run_id = str(uuid.uuid4())
        run_id = None
        try:
            provider = CodexProvider(
                _record(kind="codex", config_dir=config_dir),
            )
            prepared = _prepare(provider, root, model="gpt-5.5")
            run_id = prepared.artifact.template.arguments()["run_id"]
            manifest = codex_runtime_agent_manifest(prepared.artifact)
            assert manifest is not None
            run_dir = runs_root() / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "execution.json").write_text(
                json.dumps(prepared.artifact.to_dict()),
                encoding="utf-8",
            )
            install_staged_codex_runtime_agent_payload(
                run_dir,
                run_id=run_id,
                manifest=manifest,
            )
            source.write_bytes(b"mutated-extension-source")

            assert len(provider.recover_in_flight(
                run_id_filter={run_id},
            )) == 1
            clone_codex_runtime_agent_payload(
                run_dir,
                target_run_id=target_run_id,
                manifest=manifest,
            )
            target_dir = root / "retry"
            target_dir.mkdir()
            install_staged_codex_runtime_agent_payload(
                target_dir,
                run_id=target_run_id,
                manifest=manifest,
            )
            assert read_codex_runtime_agent_payload(
                target_dir,
                manifest,
            ) == (("reviewer.toml", original_contents),)

            clone_codex_runtime_agent_payload(
                run_dir,
                target_run_id=reconcile_run_id,
                manifest=manifest,
            )
            reconcile_dir = runs_root() / reconcile_run_id
            reconcile_dir.mkdir()
            install_staged_codex_runtime_agent_payload(
                reconcile_dir,
                run_id=reconcile_run_id,
                manifest=manifest,
            )
            import run_recovery

            assert run_recovery._mark_reconciled_terminal(
                reconcile_run_id,
                {"provider_kind": "codex"},
                "test",
            )
            assert not (reconcile_dir / manifest["path"]).exists()
            clone_codex_runtime_agent_payload(
                run_dir,
                target_run_id=reconcile_run_id,
                manifest=manifest,
            )
            install_staged_codex_runtime_agent_payload(
                reconcile_dir,
                run_id=reconcile_run_id,
                manifest=manifest,
            )
            assert provider.recover_in_flight(
                run_id_filter={reconcile_run_id},
            ) == []
            assert not (reconcile_dir / manifest["path"]).exists()

            payload_path = run_dir / manifest["path"]
            payload_path.write_bytes(b"tampered")
            assert provider.recover_in_flight(
                run_id_filter={run_id},
            ) == []
            payload_path.unlink()
            assert provider.recover_in_flight(
                run_id_filter={run_id},
            ) == []
        finally:
            extension_store.runtime_agent_entries = original_entries
            cleanup_staged_codex_runtime_agent_payload(target_run_id)
            cleanup_staged_codex_runtime_agent_payload(reconcile_run_id)
            shutil.rmtree(
                runs_root() / reconcile_run_id,
                ignore_errors=True,
            )
            if run_id is not None:
                shutil.rmtree(runs_root() / run_id, ignore_errors=True)


TESTS = (
    test_codex_prepare_binds_runtime_policy_and_contract,
    test_runner_drift_is_rejected_before_credentials_or_spawn,
    test_nested_contract_must_match_enclosing_artifact,
    test_runner_restores_artifact_authority_not_poisoned_input,
    test_fugu_isolation_and_invalid_model_fail_closed,
    test_recovery_consumes_execution_artifact_authority,
    test_completed_recovery_does_not_require_current_spawn_authority,
    test_prepare_to_spawn_provider_mutation_fails_closed,
    test_runtime_agent_payload_is_immutable_confined_and_secret_free,
    test_runtime_agent_payload_rejects_tamper_symlink_and_atomic_failure,
    test_runtime_agent_payload_is_removed_on_failed_admission,
    test_recovery_reuses_exact_runtime_agent_payload_and_fails_closed,
)


if __name__ == "__main__":
    try:
        for test in TESTS:
            test()
    finally:
        shutil.rmtree(_TEST_HOME, ignore_errors=True)
    print("PASS codex execution artifact")
