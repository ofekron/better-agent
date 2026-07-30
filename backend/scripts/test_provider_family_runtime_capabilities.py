from __future__ import annotations

import json
import os
import sys
import tempfile
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
import provider_runtime_payload_store  # noqa: E402
import provider_runtime_resolver  # noqa: E402
import provider  # noqa: E402


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


def test_snapshot_is_immutable_across_every_authority_drift() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        state_home = root / "state"
        run_dir = state_home / "runs" / "run-a"
        run_dir.mkdir(parents=True)
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
        run_dir.mkdir(parents=True)
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
        run_dir.mkdir(parents=True)
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


def test_restart_clone_preserves_posix_and_windows_configs() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        state_home = root / "state"
        source_run = state_home / "runs" / "source"
        target_run = state_home / "runs" / "target"
        source_run.mkdir(parents=True)
        target_run.mkdir(parents=True)
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
        assert secured == [root]

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


TESTS = (
    test_snapshot_is_immutable_across_every_authority_drift,
    test_artifact_manifest_and_hydration_are_secret_free,
    test_harness_secret_refs_survive_artifact_round_trip,
    test_path_symlink_and_payload_tamper_fail_closed,
    test_prewarm_success_and_failure_preserve_capability_semantics,
    test_restart_clone_preserves_posix_and_windows_configs,
    test_private_root_creation_and_nested_tree_security,
    test_provider_secures_run_directory_before_use,
)


if __name__ == "__main__":
    for test in TESTS:
        test()
    print("PASS provider family runtime capabilities")
