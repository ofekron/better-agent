from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_execution_common import ExecutionContractError  # noqa: E402
from provider_family_launch_attestation import (  # noqa: E402
    capture_critical_package,
)
from provider_family_runtime_capabilities import (  # noqa: E402
    PrewarmConnectionHydration,
    RuntimeHydrationRefs,
    SecretHydrationRef,
    cleanup_installed_family_runtime_capabilities,
    cleanup_staged_family_runtime_capabilities,
    clone_family_runtime_capabilities,
    hydrate_spawn_capabilities,
    install_staged_family_runtime_capabilities,
    resolve_run_local_capabilities,
    snapshot_family_runtime_capabilities,
    stage_family_runtime_capabilities,
)
from provider_runtime_capability_model import (  # noqa: E402
    frozen_manifest_json,
)
from provider_runtime_payload_codec import (  # noqa: E402
    decode_runtime_capability_payload,
    validate_runtime_capability_manifest,
)
import provider_runtime_payload_store  # noqa: E402
import provider_runtime_resolver  # noqa: E402
import provider  # noqa: E402
import local_machine_identity  # noqa: E402
from runtime_skill_templates import RuntimeSkillSource  # noqa: E402

import dataclasses  # noqa: E402

import provider_runtime_capability_snapshot as _snapshot_module  # noqa: E402
from codex_execution_identity import FileIdentity  # noqa: E402
from provider_runtime_capability_model import (  # noqa: E402
    MAX_FILE_BYTES,
    MAX_PAYLOAD_BYTES,
    MAX_TOTAL_FILE_BYTES,
)
from provider_runtime_capability_snapshot import (  # noqa: E402
    _agent_entries,
    _package_payload,
    _read_identity,
    _skill_entries,
    _tree_files,
)


def _write(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)


def _plan(*, required: bool = False) -> dict:
    return {
        "harness": {
            "instructions": ["Better Agent runtime"],
            "tool_policy": {"allow": ["Read", "scheduler.create"]},
        },
        "tools": ["Read", "scheduler.create"],
        "mcp_servers": [
            {
                "name": "scheduler",
                "transport": "stdio",
                "config": {
                    "argv": ["/opt/better-agent/scheduler", "--stdio"],
                    "env_refs": {"IDENTITY": "extension:scheduler"},
                },
                "tool_names": ["scheduler.create"],
                "prewarm": {
                    "eligible": True,
                    "readiness_required": required,
                },
            },
        ],
    }


def _snapshot(
    root: Path,
    *,
    plan: dict | None = None,
    prewarm_results: dict | None = None,
):
    skill = root / "sources" / "skills" / "planning"
    _write(
        skill / "SKILL.md",
        b"---\nname: planning\ndescription: Plan work\n---\nbody\n",
    )
    _write(skill / "scripts" / "run.sh", b"#!/bin/sh\nexit 0\n")
    _write(
        skill / "assets" / "binary.bin",
        b"lf\ncrlf\r\nsub\x1a\x00non-utf8\xff",
    )
    agent = root / "sources" / "agents" / "reviewer.md"
    _write(agent, b"Review changes adversarially.\n")
    package_root = root / "sources" / "packages" / "runtime_pkg"
    package_file = package_root / "__init__.py"
    _write(package_file, b"VERSION = 1\n")
    extensions = {
        "scheduler": {
            "installed": True,
            "enabled": True,
            "grants": ["sessions.read", "scheduler.write"],
            "predicates": {
                "installation_allowed": True,
                "session_allowed": True,
            },
            "settings": {
                "timezone": "UTC",
                "identity_ref": "extension:scheduler",
            },
        },
    }
    installation = {
        "integrations_enabled": True,
        "provider_conversations_enabled": True,
        "mode": "default",
    }
    critical_package = capture_critical_package(
        package_name="runtime_pkg",
        package_root=package_root,
        relative_paths=("__init__.py",),
    )
    selected_plan = plan or _plan()
    prepared = snapshot_family_runtime_capabilities(
        family="claude",
        skill_sources={"planning": skill},
        agent_sources={"reviewer.md": agent},
        resolved_plan=selected_plan,
        extension_state=extensions,
        installation_decisions=installation,
        package_identities=(critical_package,),
        prewarm_results=prewarm_results or {},
    )
    return prepared, {
        "skill": skill,
        "agent": agent,
        "package": package_file,
        "plan": selected_plan,
        "extensions": extensions,
        "installation": installation,
    }


def test_machine_skill_snapshot_specializes_only_declared_source() -> None:
    with tempfile.TemporaryDirectory() as raw:
        original_machine_id = local_machine_identity._local_machine_id
        local_machine_identity._local_machine_id = "worker-1"
        root = Path(raw)
        machine_skill = root / "machine-skill"
        literal_skill = root / "literal-skill"
        template = b"---\nname: skill\ndescription: {{better_agent.machine_id}}\n---\n"
        _write(machine_skill / "SKILL.md", template)
        _write(literal_skill / "SKILL.md", template)

        try:
            prepared = snapshot_family_runtime_capabilities(
                family="agy",
                skill_sources={
                    "machine": RuntimeSkillSource(
                        root=machine_skill,
                        template_variables=("machine_id",),
                    ),
                    "literal": RuntimeSkillSource(root=literal_skill),
                },
                agent_sources={},
                resolved_plan=_plan(),
                extension_state={},
                installation_decisions={"integrations_enabled": True},
                machine_id="worker-1",
            )
            files = json.loads(prepared.payload)["files"]
            contents = {
                item["owner"]: base64.b64decode(item["contents"])
                for item in files
                if item["path"] == "SKILL.md"
            }
            assert b"worker-1" in contents["machine"]
            assert b"{{better_agent.machine_id}}" not in contents["machine"]
            assert b"{{better_agent.machine_id}}" in contents["literal"]

            for machine_id in (None, "primary"):
                try:
                    snapshot_family_runtime_capabilities(
                        family="agy",
                        skill_sources={
                            "machine": RuntimeSkillSource(
                                root=machine_skill,
                                template_variables=("machine_id",),
                            ),
                        },
                        agent_sources={},
                        resolved_plan=_plan(),
                        extension_state={},
                        installation_decisions={"integrations_enabled": True},
                        machine_id=machine_id,
                    )
                except ExecutionContractError:
                    pass
                else:
                    raise AssertionError(
                        f"machine skill snapshot accepted mismatched identity {machine_id!r}"
                    )
        finally:
            local_machine_identity._local_machine_id = original_machine_id


def test_snapshot_is_immutable_across_every_authority_drift() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        state_home = root / "state"
        run_dir = state_home / "runs" / "run-a"
        provider._ensure_execution_run_dir(run_dir)
        prepared, sources = _snapshot(root)
        original_manifest = json.loads(json.dumps(prepared.manifest))

        _write(sources["skill"] / "SKILL.md", b"changed skill")
        _write(sources["agent"], b"changed agent")
        _write(sources["package"], b"VERSION = 2\n")
        sources["plan"]["harness"]["instructions"].clear()
        sources["plan"]["tools"].clear()
        sources["plan"]["mcp_servers"][0]["config"]["argv"].clear()
        sources["extensions"]["scheduler"]["enabled"] = False
        sources["extensions"]["scheduler"]["grants"].clear()
        sources["extensions"]["scheduler"]["predicates"][
            "session_allowed"
        ] = False
        sources["extensions"]["scheduler"]["settings"]["timezone"] = "Asia/Tokyo"
        sources["installation"]["integrations_enabled"] = False

        previous = os.environ.get("BETTER_AGENT_HOME")
        os.environ["BETTER_AGENT_HOME"] = str(state_home)
        try:
            stage_family_runtime_capabilities("run-a", prepared)
            install_staged_family_runtime_capabilities(
                run_dir,
                run_id="run-a",
                manifest=prepared.manifest,
            )
            resolved = resolve_run_local_capabilities(
                run_dir,
                prepared.manifest,
            )
        finally:
            if previous is None:
                os.environ.pop("BETTER_AGENT_HOME", None)
            else:
                os.environ["BETTER_AGENT_HOME"] = previous

        assert prepared.manifest == original_manifest
        assert resolved.extension_state["scheduler"]["enabled"] is True
        assert resolved.extension_state["scheduler"]["grants"] == [
            "sessions.read",
            "scheduler.write",
        ]
        assert resolved.extension_state["scheduler"]["predicates"][
            "session_allowed"
        ] is True
        assert resolved.extension_state["scheduler"]["settings"][
            "timezone"
        ] == "UTC"
        assert resolved.installation_decisions["integrations_enabled"] is True
        assert resolved.plan["tools"] == ["Read", "scheduler.create"]
        assert resolved.plan["mcp_servers"][0]["config"]["argv"] == [
            "/opt/better-agent/scheduler",
            "--stdio",
        ]
        assert (resolved.skill_dirs["planning"] / "SKILL.md").read_bytes().endswith(
            b"body\n",
        )
        assert (
            resolved.skill_dirs["planning"] / "assets" / "binary.bin"
        ).read_bytes() == b"lf\ncrlf\r\nsub\x1a\x00non-utf8\xff"
        assert resolved.agent_files["reviewer.md"].read_bytes() == (
            b"Review changes adversarially.\n"
        )
        assert "source" not in json.dumps(prepared.manifest).lower()


def test_artifact_manifest_and_hydration_are_secret_free() -> None:
    with tempfile.TemporaryDirectory() as raw:
        prepared, _sources = _snapshot(Path(raw))
        provider_secret = "provider-secret-value"
        extension_secret = "extension-secret-value"
        broker_secret = "broker-secret-value"
        transport_secret = "transport-secret-value"
        prewarm_secret = "prewarm-secret-value"
        hydration = RuntimeHydrationRefs(
            provider_identity=SecretHydrationRef(
                "provider_identity",
                provider_secret,
            ),
            extension_identities={
                "scheduler": SecretHydrationRef(
                    "extension_identity",
                    extension_secret,
                ),
            },
            runtime_broker=SecretHydrationRef("runtime_broker", broker_secret),
            backend_transport=SecretHydrationRef(
                "backend_transport",
                transport_secret,
            ),
            prewarm_connections={
                "scheduler": PrewarmConnectionHydration(
                    endpoint="tcp://127.0.0.1:43123",
                    connect_secret=prewarm_secret,
                ),
            },
        )
        encoded = json.dumps(prepared.manifest, sort_keys=True)
        representation = repr(hydration)
        for secret in (
            provider_secret,
            extension_secret,
            broker_secret,
            transport_secret,
            prewarm_secret,
        ):
            assert secret not in encoded
            assert secret not in representation

        bad_plan = _plan()
        bad_plan["mcp_servers"][0]["config"]["api_key"] = "must-not-persist"
        try:
            _snapshot(Path(raw) / "bad-plan", plan=bad_plan)
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("secret MCP configuration was persisted")


def test_harness_secret_refs_survive_artifact_round_trip() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        state_home = root / "state"
        run_dir = state_home / "runs" / "secret-ref-run"
        provider._ensure_execution_run_dir(run_dir)
        refs = {
            "example.extension": [
                "extension-setting:example.extension:access_token",
                "extension-setting:example.extension:api_key",
            ],
        }
        plan = _plan()
        plan["harness"]["secret_refs"] = refs
        plan["harness"]["launcher_projection"] = {
            "secret_refs": refs,
        }
        prepared, _sources = _snapshot(root, plan=plan)

        assert prepared.plan["harness"]["secret_refs"] == refs
        assert json.loads(prepared.payload)["plan"]["harness"][
            "secret_refs"
        ] == refs

        previous = os.environ.get("BETTER_AGENT_HOME")
        os.environ["BETTER_AGENT_HOME"] = str(state_home)
        try:
            stage_family_runtime_capabilities(
                "secret-ref-run",
                prepared,
            )
            install_staged_family_runtime_capabilities(
                run_dir,
                run_id="secret-ref-run",
                manifest=prepared.manifest,
            )
            resolved = resolve_run_local_capabilities(
                run_dir,
                prepared.manifest,
            )
        finally:
            if previous is None:
                os.environ.pop("BETTER_AGENT_HOME", None)
            else:
                os.environ["BETTER_AGENT_HOME"] = previous

        assert resolved.plan["harness"]["secret_refs"] == refs
        assert resolved.plan["harness"]["launcher_projection"][
            "secret_refs"
        ] == refs


def test_path_symlink_and_payload_tamper_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        skill = root / "skill"
        _write(skill / "SKILL.md", b"skill")
        outside = root / "outside"
        _write(outside, b"outside")
        (skill / "escape").symlink_to(outside)
        try:
            snapshot_family_runtime_capabilities(
                family="agy",
                skill_sources={"skill": skill},
                agent_sources={},
                resolved_plan=_plan(),
                extension_state={},
                installation_decisions={"integrations_enabled": True},
            )
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("skill symlink escape was accepted")

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        state_home = root / "state"
        run_dir = state_home / "runs" / "run-tamper"
        provider._ensure_execution_run_dir(run_dir)
        prepared, _sources = _snapshot(root)
        previous = os.environ.get("BETTER_AGENT_HOME")
        os.environ["BETTER_AGENT_HOME"] = str(state_home)
        try:
            stage_family_runtime_capabilities("run-tamper", prepared)
            staged = next(
                (state_home / "prepared-execution-payloads").glob("*/*.json"),
            )
            staged.write_bytes(staged.read_bytes() + b"x")
            try:
                install_staged_family_runtime_capabilities(
                    run_dir,
                    run_id="run-tamper",
                    manifest=prepared.manifest,
                )
            except ExecutionContractError:
                pass
            else:
                raise AssertionError("tampered capability payload was installed")
            cleanup_staged_family_runtime_capabilities("run-tamper")

            stage_family_runtime_capabilities("cleanup-redirect", prepared)
            staged_dir = provider_runtime_payload_store._stage_dir(
                "cleanup-redirect",
            )
            (
                staged_dir
                / provider_runtime_payload_store.CAPABILITY_PAYLOAD_NAME
            ).unlink()
            staged_dir.rmdir()
            outside_dir = root / "outside-cleanup"
            outside_dir.mkdir()
            marker = outside_dir / "marker"
            marker.write_text("preserve")
            staged_dir.symlink_to(outside_dir, target_is_directory=True)
            try:
                cleanup_staged_family_runtime_capabilities(
                    "cleanup-redirect",
                )
            except ExecutionContractError:
                pass
            else:
                raise AssertionError("redirected staged cleanup was accepted")
            assert marker.read_text() == "preserve"
            staged_dir.unlink()
        finally:
            if previous is None:
                os.environ.pop("BETTER_AGENT_HOME", None)
            else:
                os.environ["BETTER_AGENT_HOME"] = previous


def test_prewarm_success_and_failure_preserve_capability_semantics() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        ready, _ = _snapshot(
            root / "ready",
            prewarm_results={
                "scheduler": {"status": "ready", "error": None},
            },
        )
        failed, _ = _snapshot(
            root / "failed",
            prewarm_results={
                "scheduler": {
                    "status": "failed",
                    "error": "connection refused",
                },
            },
        )

        assert ready.semantic_fingerprint == failed.semantic_fingerprint
        assert ready.tool_names == failed.tool_names
        assert ready.mcp_configs == failed.mcp_configs
        assert ready.prewarm_status["scheduler"] == {
            "status": "ready",
            "error": None,
            "launch_mode": "prewarmed",
        }
        assert failed.prewarm_status["scheduler"] == {
            "status": "failed",
            "error": "connection refused",
            "launch_mode": "normal",
        }

        hydration = RuntimeHydrationRefs(
            provider_identity=SecretHydrationRef("provider", "identity"),
            extension_identities={},
            runtime_broker=None,
            backend_transport=None,
            prewarm_connections={
                "scheduler": PrewarmConnectionHydration(
                    endpoint="tcp://127.0.0.1:43123",
                    connect_secret="secret",
                ),
            },
        )
        hydrated = hydrate_spawn_capabilities(ready, hydration)
        assert hydrated.plan == ready.plan
        assert hydrated.prewarm_status == ready.prewarm_status

        try:
            _snapshot(
                root / "required",
                plan=_plan(required=True),
                prewarm_results={
                    "scheduler": {
                        "status": "failed",
                        "error": "connection refused",
                    },
                },
            )
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("required prewarm readiness failed open")


def test_secret_worded_server_name_is_not_treated_as_secret() -> None:
    with tempfile.TemporaryDirectory() as raw:
        plan = _plan()
        plan["tools"].append("credential-broker.request")
        plan["mcp_servers"].append({
            "name": "credential-broker",
            "transport": "stdio",
            "config": {
                "argv": ["/opt/better-agent/credential-broker", "--stdio"],
                "env_refs": {},
            },
            "tool_names": ["credential-broker.request"],
            "prewarm": {
                "eligible": False,
                "readiness_required": False,
            },
        })
        prepared, _sources = _snapshot(Path(raw), plan=plan)

        assert prepared.prewarm_status["credential-broker"]["status"] == (
            "not_eligible"
        )
        assert validate_runtime_capability_manifest(prepared.manifest)
        decoded, _files = decode_runtime_capability_payload(
            prepared.payload,
            prepared.manifest,
        )
        assert decoded["prewarm_status"]["credential-broker"]["status"] == (
            "not_eligible"
        )


def test_manifest_prewarm_gate_still_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        prepared, _sources = _snapshot(Path(raw))

        leaked = prepared.manifest
        leaked["prewarm_status"]["scheduler"] = {
            "status": "failed",
            "error": "upstream rejected the token",
            "launch_mode": "normal",
        }
        malformed = prepared.manifest
        malformed["prewarm_status"]["scheduler"]["extra"] = True
        unsafe_name = prepared.manifest
        unsafe_name["prewarm_status"]["bad name!"] = {
            "status": "not_eligible",
            "error": None,
            "launch_mode": "normal",
        }
        for manifest in (leaked, malformed, unsafe_name):
            try:
                validate_runtime_capability_manifest(manifest)
            except ExecutionContractError:
                pass
            else:
                raise AssertionError("invalid prewarm status was accepted")

        smuggled = {
            "prewarm_status": {
                "scheduler": {
                    "status": "ready",
                    "error": None,
                    "launch_mode": "prewarmed",
                },
            },
            "api_key": "must-not-freeze",
        }
        try:
            frozen_manifest_json(smuggled, label="test manifest")
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("secret manifest field was accepted")

        try:
            snapshot_family_runtime_capabilities(
                family="claude",
                skill_sources={},
                agent_sources={},
                resolved_plan=_plan(),
                extension_state={"token": "must-not-freeze"},
                installation_decisions={},
            )
        except ExecutionContractError:
            pass
        else:
            raise AssertionError("secret extension state was accepted")


def test_restart_clone_preserves_posix_and_windows_configs() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        state_home = root / "state"
        source_run = state_home / "runs" / "source"
        target_run = state_home / "runs" / "target"
        provider._ensure_execution_run_dir(source_run)
        provider._ensure_execution_run_dir(target_run)
        plan = _plan()
        plan["tools"].append("windows-tool.run")
        plan["mcp_servers"].append({
            "name": "windows-tool",
            "transport": "stdio",
            "config": {
                "argv": [
                    "C:\\Program Files\\Better Agent\\tool.exe",
                    "--stdio",
                ],
                "env_refs": {},
            },
            "tool_names": ["windows-tool.run"],
            "prewarm": {
                "eligible": False,
                "readiness_required": False,
            },
        })
        prepared, _sources = _snapshot(root, plan=plan)
        previous = os.environ.get("BETTER_AGENT_HOME")
        os.environ["BETTER_AGENT_HOME"] = str(state_home)
        try:
            stage_family_runtime_capabilities("source", prepared)
            install_staged_family_runtime_capabilities(
                source_run,
                run_id="source",
                manifest=prepared.manifest,
            )
            clone_family_runtime_capabilities(
                source_run,
                target_run_id="target",
                manifest=prepared.manifest,
            )
            install_staged_family_runtime_capabilities(
                target_run,
                run_id="target",
                manifest=prepared.manifest,
            )
            source = resolve_run_local_capabilities(
                source_run,
                prepared.manifest,
            )
            target = resolve_run_local_capabilities(
                target_run,
                prepared.manifest,
            )
            assert source.plan == target.plan == plan
            cleanup_installed_family_runtime_capabilities(source_run)
            cleanup_installed_family_runtime_capabilities(target_run)
        finally:
            if previous is None:
                os.environ.pop("BETTER_AGENT_HOME", None)
            else:
                os.environ["BETTER_AGENT_HOME"] = previous


def test_resolved_executable_is_cleanup_safe() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "runtime-capabilities"
        root.mkdir(mode=0o700)
        provider_runtime_resolver.make_private_directory(root)
        executable = root / "skills" / "planning" / "scripts" / "run.sh"
        provider_runtime_resolver._write_resolved_file(
            root,
            executable,
            b"#!/bin/sh\nexit 0\n",
            0o500,
        )

        provider_runtime_resolver.require_private_file(executable)
        assert executable.read_bytes() == b"#!/bin/sh\nexit 0\n"
        observed = executable.stat()
        if os.name == "nt":
            assert not (
                getattr(observed, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_READONLY", 1)
            )
        else:
            assert stat.S_IMODE(observed.st_mode) == 0o500

        shutil.rmtree(root)
        assert not root.exists()


def test_private_root_creation_and_nested_tree_security() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "prepared"
        original_make_private = provider_runtime_payload_store.make_private_directory
        original_require_private = (
            provider_runtime_payload_store.require_private_directory
        )
        secured: list[Path] = []
        provider_runtime_payload_store.make_private_directory = secured.append
        provider_runtime_payload_store.require_private_directory = lambda _path: None
        try:
            resolved = provider_runtime_payload_store._ensure_secure_directory(root)
            provider_runtime_payload_store._ensure_secure_directory(root)
        finally:
            provider_runtime_payload_store.make_private_directory = original_make_private
            provider_runtime_payload_store.require_private_directory = (
                original_require_private
            )

        assert resolved == root.resolve()
        assert secured == [root, root]

        tree_root = Path(raw) / "tree"
        tree_root.mkdir()
        original_make_private = provider_runtime_resolver.make_private_directory
        original_require_private = provider_runtime_resolver.require_private_directory
        secured = []
        verified: list[Path] = []
        provider_runtime_resolver.make_private_directory = secured.append
        provider_runtime_resolver.require_private_directory = verified.append
        nested = tree_root / "skills" / "owner" / "scripts"
        try:
            provider_runtime_resolver._ensure_private_parent_tree(
                tree_root,
                nested,
            )
        finally:
            provider_runtime_resolver.make_private_directory = original_make_private
            provider_runtime_resolver.require_private_directory = (
                original_require_private
            )
        expected = [
            tree_root / "skills",
            tree_root / "skills" / "owner",
            nested,
        ]
        assert secured == expected
        assert verified == expected


def test_provider_secures_run_directory_before_use() -> None:
    with tempfile.TemporaryDirectory() as raw:
        run_dir = Path(raw) / "run"
        original_make_private = provider.make_private_directory
        original_require_private = provider.require_private_directory
        events: list[tuple[str, Path]] = []
        provider.make_private_directory = (
            lambda path: events.append(("secure", path))
        )
        provider.require_private_directory = (
            lambda path: events.append(("verify", path))
        )
        try:
            assert provider._ensure_execution_run_dir(run_dir)
            assert not provider._ensure_execution_run_dir(run_dir)
        finally:
            provider.make_private_directory = original_make_private
            provider.require_private_directory = original_require_private

        assert events == [
            ("secure", run_dir),
            ("verify", run_dir),
            ("verify", run_dir),
        ]


def test_legacy_staging_root_migrates_only_on_write() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        state_home = root / "state"
        prepared_root = state_home / "prepared-execution-payloads"
        prepared_root.mkdir(parents=True)
        prepared_root.chmod(0o777)
        prepared, _sources = _snapshot(root)
        previous = os.environ.get("BETTER_AGENT_HOME")
        os.environ["BETTER_AGENT_HOME"] = str(state_home)
        try:
            barrier = threading.Barrier(3)
            failures: list[BaseException] = []

            def stage(run_id: str) -> None:
                barrier.wait()
                try:
                    stage_family_runtime_capabilities(run_id, prepared)
                except BaseException as exc:
                    failures.append(exc)

            workers = [
                threading.Thread(target=stage, args=(run_id,))
                for run_id in ("legacy-a", "legacy-b")
            ]
            for worker in workers:
                worker.start()
            barrier.wait()
            for worker in workers:
                worker.join()

            assert failures == []
            provider_runtime_payload_store.require_private_directory(
                prepared_root,
            )
            if os.name != "nt":
                assert stat.S_IMODE(prepared_root.stat().st_mode) == 0o700
            cleanup_staged_family_runtime_capabilities("legacy-a")
            cleanup_staged_family_runtime_capabilities("legacy-b")

            original_make_private = (
                provider_runtime_payload_store.make_private_directory
            )
            original_require_private = (
                provider_runtime_payload_store.require_private_directory
            )
            mutations: list[Path] = []

            def reject_private_directory(_path: Path) -> None:
                raise PermissionError("unsafe")

            provider_runtime_payload_store.make_private_directory = mutations.append
            provider_runtime_payload_store.require_private_directory = (
                reject_private_directory
            )
            try:
                try:
                    cleanup_staged_family_runtime_capabilities("missing")
                except ExecutionContractError:
                    pass
                else:
                    raise AssertionError("insecure cleanup root was accepted")
            finally:
                provider_runtime_payload_store.make_private_directory = (
                    original_make_private
                )
                provider_runtime_payload_store.require_private_directory = (
                    original_require_private
                )
            assert mutations == []
        finally:
            if previous is None:
                os.environ.pop("BETTER_AGENT_HOME", None)
            else:
                os.environ["BETTER_AGENT_HOME"] = previous


def _expect_reject(callable_, *args, **kwargs) -> None:
    try:
        callable_(*args, **kwargs)
    except ExecutionContractError:
        return
    raise AssertionError("invalid runtime capability input was accepted")


def test_read_identity_rejects_tampered_sha256() -> None:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "f"
        path.write_bytes(b"hello")
        identity = dataclasses.replace(FileIdentity.capture(path), sha256="0" * 64)
        _expect_reject(_read_identity, identity)


def test_read_identity_rejects_unavailable_source() -> None:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "f"
        path.write_bytes(b"hello")
        identity = dataclasses.replace(
            FileIdentity.capture(path),
            resolved_path=str(Path(raw) / "missing"),
        )
        _expect_reject(_read_identity, identity)


def test_read_identity_rejects_change_during_read(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "f"
        path.write_bytes(b"hello")
        identity = FileIdentity.capture(path)
        real_read = os.read
        # For a small file, sha256_fd does exactly one non-empty os.read at the
        # identity-match check; the snapshot read loop does the second. Mutate
        # the file during that second read so the after-read fstat diverges.
        data_reads = {"n": 0}

        def reading(fd, n):
            data = real_read(fd, n)
            if data:
                data_reads["n"] += 1
                if data_reads["n"] == 2:
                    os.truncate(str(path), 0)
            return data

        monkeypatch.setattr(os, "read", reading)
        _expect_reject(_read_identity, identity)


def test_tree_files_rejects_non_absolute_root() -> None:
    _expect_reject(_tree_files, Path("relative"))


def test_tree_files_rejects_unresolvable_root() -> None:
    with tempfile.TemporaryDirectory() as raw:
        blocker = Path(raw) / "file"
        blocker.write_text("x")
        _expect_reject(_tree_files, blocker / "skill")


def test_tree_files_rejects_file_root() -> None:
    with tempfile.TemporaryDirectory() as raw:
        file_root = Path(raw) / "file"
        file_root.write_text("x")
        _expect_reject(_tree_files, file_root)


def test_tree_files_rejects_non_regular_entry() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "SKILL.md").write_text("x")
        os.mkfifo(str(root / "pipe"))
        _expect_reject(_tree_files, root)


def test_tree_files_rejects_missing_skill_md() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "other.md").write_text("x")
        _expect_reject(_tree_files, root)


def test_agent_entry_rejects_symlink_source() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        target = root / "real.md"
        target.write_text("real")
        link = root / "link.md"
        link.symlink_to(target)
        _expect_reject(
            snapshot_family_runtime_capabilities,
            family="claude",
            skill_sources={},
            agent_sources={"link.md": link},
            resolved_plan=_plan(),
            extension_state={},
            installation_decisions={},
        )


def test_skill_entries_rejects_non_dict_selection() -> None:
    _expect_reject(_skill_entries, ["not", "a", "dict"])


def test_skill_entries_rejects_invalid_name() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "SKILL.md").write_text("x")
        _expect_reject(_skill_entries, {"bad name!": root})


def test_skill_entries_rejects_change_during_snapshot(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "SKILL.md").write_text("x")
        original = _snapshot_module._tree_files
        calls = {"n": 0}

        def mutating(root_arg):
            result = original(root_arg)
            calls["n"] += 1
            if calls["n"] == 1:
                (root_arg / "added").write_text("x")
            return result

        monkeypatch.setattr(_snapshot_module, "_tree_files", mutating)
        _expect_reject(_skill_entries, {"skill": root})


def test_agent_entries_rejects_non_dict_selection() -> None:
    _expect_reject(_agent_entries, ("not", "dict"))


def test_agent_entries_rejects_invalid_name() -> None:
    with tempfile.TemporaryDirectory() as raw:
        src = Path(raw) / "a.md"
        src.write_text("x")
        _expect_reject(_agent_entries, {"bad/name": src})


def test_agent_entries_rejects_unavailable_source() -> None:
    _expect_reject(_agent_entries, {"agent.md": Path("/nonexistent/agent.md")})


def test_package_payload_rejects_non_identity_entry() -> None:
    _expect_reject(_package_payload, ("not-an-identity",))


def test_package_payload_rejects_duplicate_package() -> None:
    with tempfile.TemporaryDirectory() as raw:
        proot = Path(raw) / "pkg"
        proot.mkdir()
        (proot / "__init__.py").write_text("V=1")
        pkg = capture_critical_package(
            package_name="pkg",
            package_root=proot,
            relative_paths=("__init__.py",),
        )
        _expect_reject(_package_payload, (pkg, pkg))


def test_snapshot_rejects_unsupported_family() -> None:
    _expect_reject(
        snapshot_family_runtime_capabilities,
        family="nope",
        skill_sources={},
        agent_sources={},
        resolved_plan=_plan(),
        extension_state={},
        installation_decisions={},
    )


def test_snapshot_rejects_agy_agent_surface() -> None:
    with tempfile.TemporaryDirectory() as raw:
        agent = Path(raw) / "a.md"
        agent.write_text("x")
        _expect_reject(
            snapshot_family_runtime_capabilities,
            family="agy",
            skill_sources={},
            agent_sources={"a.md": agent},
            resolved_plan=_plan(),
            extension_state={},
            installation_decisions={},
        )


def test_snapshot_rejects_non_dict_capability_state() -> None:
    _expect_reject(
        snapshot_family_runtime_capabilities,
        family="claude",
        skill_sources={},
        agent_sources={},
        resolved_plan=_plan(),
        extension_state=["not", "dict"],
        installation_decisions={},
    )


def test_snapshot_rejects_oversize_files() -> None:
    with tempfile.TemporaryDirectory() as raw:
        skill = Path(raw) / "skill"
        _write(skill / "SKILL.md", b"x")
        blob = b"x" * MAX_FILE_BYTES
        count = (MAX_TOTAL_FILE_BYTES // MAX_FILE_BYTES) + 1
        for index in range(count):
            _write(skill / f"f{index}", blob)
        _expect_reject(
            snapshot_family_runtime_capabilities,
            family="claude",
            skill_sources={"skill": skill},
            agent_sources={},
            resolved_plan=_plan(),
            extension_state={},
            installation_decisions={},
        )


def test_snapshot_rejects_oversize_payload() -> None:
    huge = "x" * (MAX_PAYLOAD_BYTES + 1)
    _expect_reject(
        snapshot_family_runtime_capabilities,
        family="claude",
        skill_sources={},
        agent_sources={},
        resolved_plan=_plan(),
        extension_state={"blob": huge},
        installation_decisions={},
    )


TESTS = (
    test_snapshot_is_immutable_across_every_authority_drift,
    test_artifact_manifest_and_hydration_are_secret_free,
    test_harness_secret_refs_survive_artifact_round_trip,
    test_path_symlink_and_payload_tamper_fail_closed,
    test_prewarm_success_and_failure_preserve_capability_semantics,
    test_secret_worded_server_name_is_not_treated_as_secret,
    test_manifest_prewarm_gate_still_fails_closed,
    test_restart_clone_preserves_posix_and_windows_configs,
    test_resolved_executable_is_cleanup_safe,
    test_private_root_creation_and_nested_tree_security,
    test_provider_secures_run_directory_before_use,
    test_legacy_staging_root_migrates_only_on_write,
)


if __name__ == "__main__":
    for test in TESTS:
        test()
    print("PASS provider family runtime capabilities")
