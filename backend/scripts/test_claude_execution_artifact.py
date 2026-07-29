from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

TEST_HOME = _test_home.TestHome.acquire(
    "bc-test-claude-execution-artifact-",
)

import config_store  # noqa: E402
from codex_execution_identity import file_identity_to_dict  # noqa: E402
from execution_artifact_io import bind_execution_input  # noqa: E402
from provider_claude import ClaudeProvider  # noqa: E402
from provider_claude_execution import (  # noqa: E402
    attest_embedded_claude_sdk,
    attest_materialized_claude_sdk_package,
    capture_claude_sdk_package,
    materialize_claude_sdk_package,
)
from provider_family_execution_runtime import (  # noqa: E402
    family_capability_manifest_from_artifact,
    family_launch_from_artifact,
    restore_family_runner_runtime,
)
from provider_family_runtime_capabilities import (  # noqa: E402
    cleanup_installed_family_runtime_capabilities,
)
from provider_runtime_plan_source import (  # noqa: E402
    hydrate_frozen_provider_runtime_plan,
)
import runner  # noqa: E402
from runs_dir import atomic_write_json, runs_root  # noqa: E402
import session_manager  # noqa: E402
import working_mode  # noqa: E402


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _provider(root: Path) -> tuple[ClaudeProvider, dict]:
    config_dir = root / "claude-config"
    config_dir.mkdir()
    shared_settings = root / "shared-settings.json"
    shared_settings.write_text(
        '{"permissions":{"defaultMode":"dontAsk"}}',
        encoding="utf-8",
    )
    (config_dir / "settings.json").symlink_to(shared_settings)
    record = config_store.add_provider({
        "kind": "claude",
        "name": "Claude artifact",
        "mode": "subscription",
        "base_url": "",
        "config_dir": str(config_dir),
        "runner": "native",
    })
    session = session_manager.manager.create(
        name="Claude artifact",
        cwd=str(root),
        orchestration_mode="native",
        source="test",
        provider_id=record["id"],
        model="claude-opus-4-6",
        permission={"mode": "dontAsk"},
        disabled_builtin_tools=["create_session"],
        disabled_runtime_skills=["disabled-at-prepare"],
    )
    session = working_mode.mark_working_mode(
        session["id"],
        mode="file_editing",
        meta={},
    )
    assert session is not None
    return ClaudeProvider(record), session


def _arguments(root: Path, session_id: str, run_id: str) -> dict:
    return {
        "run_id": run_id,
        "prompt": "preserve Claude authority",
        "images": None,
        "files": None,
        "cwd": str(root),
        "model": "claude-opus-4-6",
        "reasoning_effort": "high",
        "session_id": None,
        "mode": "native",
        "app_session_id": session_id,
        "source": "web",
        "disallowed_tools": None,
        "setting_sources": None,
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "volatile-internal-token",
        "fork": False,
        "supervised": False,
        "supervisor_agent_session_id": None,
        "worker_agent_session_id": None,
        "mssg_sender_session_id": None,
        "is_worker": False,
        "browser_harness_enabled": False,
        "user_facing": True,
        "working_mode": None,
        "extra_env": {"BU_CDP_URL": "http://127.0.0.1:9222"},
        "continuation_chain": ["first"],
        "provider_run_config": {
            "mcp_servers": {
                "frozen": {
                    "command": "frozen-mcp",
                    "args": ["serve"],
                },
            },
            "skills": {"frozen-skill": "Use the frozen skill."},
        },
        "capability_contexts": [{
            "name": "Frozen capability",
            "category": "system",
            "content": "Use the prepared capability.",
        }],
        "target_message_id": None,
        "resolved_harness_run_config": {
            "profile_id": "frozen-profile",
            "profile_revision": "revision-1",
            "provider_run_config": {},
            "secret_refs": {
                "example.extension": [
                    "extension-setting:example.extension:access_token",
                    "extension-setting:example.extension:api_key",
                ],
            },
            "launcher_projection": {
                "secret_refs": {
                    "example.extension": [
                        "extension-setting:example.extension:access_token",
                        "extension-setting:example.extension:api_key",
                    ],
                },
            },
        },
        "turn_run_id": None,
        "disabled_builtin_extensions": [],
        "provisioned_tool_profile": "frozen-tools",
    }


def test_prepare_freezes_claude_semantics_and_retry_authority() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        bin_dir = root / "bin"
        _write_executable(bin_dir / "claude", "#!/bin/sh\nexit 0\n")
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = os.pathsep.join((str(bin_dir), old_path))
        try:
            provider, session = _provider(root)
            arguments = _arguments(root, session["id"], str(uuid.uuid4()))
            prepared = provider.prepare_run(**arguments)
            launch = family_launch_from_artifact(prepared.artifact)
            manifest = family_capability_manifest_from_artifact(
                prepared.artifact,
            )
            runner_input = prepared.artifact.runtime_policy["runner_input"]

            assert launch.family == "claude"
            assert launch.attest()
            assert any(
                package.package_name == "claude_agent_sdk"
                for package in launch.critical_packages
            )
            assert manifest["family"] == "claude"
            assert runner_input["run_id"] == arguments["run_id"]
            assert runner_input["model"] == "claude-opus-4-6"
            assert runner_input["reasoning_effort"] == "high"
            assert runner_input["permission"] == {"mode": "dontAsk"}
            assert runner_input["working_mode"] == "file_editing"
            assert runner_input["disabled_runtime_skills"] == [
                "disabled-at-prepare",
            ]
            assert runner_input["provider_run_config"]["mcp_servers"][
                "frozen"
            ]
            assert runner_input["resolved_harness_run_config"][
                "profile_id"
            ] == "frozen-profile"
            assert runner_input["resolved_harness_run_config"][
                "secret_refs"
            ]["example.extension"] == [
                "extension-setting:example.extension:access_token",
                "extension-setting:example.extension:api_key",
            ]

            provisioning_arguments = _arguments(
                root,
                session["id"],
                str(uuid.uuid4()),
            )
            provisioning_arguments["source"] = "routine"
            provisioning_arguments["user_facing"] = False
            provisioning_arguments["provisioned_tool_profile"] = (
                "scheduled-tools"
            )
            provisioning = provider.prepare_run(
                **provisioning_arguments,
            )
            assert provisioning.artifact.runtime_policy["runner_input"][
                "source"
            ] == "routine"
            assert provisioning.artifact.runtime_policy["runner_input"][
                "resolved_harness_run_config"
            ]["secret_refs"] == runner_input[
                "resolved_harness_run_config"
            ]["secret_refs"]

            arguments["prompt"] = "mutated after prepare"
            arguments["provider_run_config"]["mcp_servers"].clear()
            persisted = json.dumps(
                prepared.artifact.to_dict(),
                sort_keys=True,
            )
            assert "volatile-internal-token" not in persisted
            assert "BU_CDP_URL" not in persisted
            assert prepared.artifact.runtime_policy["runner_input"][
                "prompt"
            ] == "preserve Claude authority"
            retry = prepared.retry(run_id=str(uuid.uuid4()))
            assert retry.artifact.provider_contract == (
                prepared.artifact.provider_contract
            )
            assert retry.artifact.runtime_policy == (
                prepared.artifact.runtime_policy
            )
        finally:
            os.environ["PATH"] = old_path


def test_run_local_payload_rejects_tamper_and_materializes_sdk_cli() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        bin_dir = root / "bin"
        launcher = bin_dir / "claude"
        _write_executable(
            launcher,
            f"#!{sys.executable}\nprint('frozen cli', end='')\n",
        )
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = os.pathsep.join((str(bin_dir), old_path))
        try:
            provider, session = _provider(root)
            run_id = str(uuid.uuid4())
            prepared = provider.prepare_run(
                **_arguments(root, session["id"], run_id),
            )
            run_dir = runs_root() / run_id
            run_dir.mkdir(parents=True)
            atomic_write_json(
                run_dir / "execution.json",
                prepared.artifact.to_dict(),
            )
            atomic_write_json(
                run_dir / "input.json",
                bind_execution_input(
                    prepared.artifact,
                    prepared.artifact.runtime_policy["runner_input"],
                ),
            )
            provider._install_execution_payloads(prepared, run_dir)

            runtime = restore_family_runner_runtime(run_dir)
            (run_dir / "claude-cli").mkdir(mode=0o700)
            materialized_cli = runtime.launch.materialize_sdk(
                run_dir / "claude-cli",
            )
            package_root = materialize_claude_sdk_package(
                next(
                    package
                    for package in runtime.launch.critical_packages
                    if package.package_name == "claude_agent_sdk"
                ),
                run_dir / "claude-sdk",
            )
            assert materialized_cli.attest()
            assert package_root.name == "claude_agent_sdk"
            assert (package_root / "__init__.py").is_file()

            poison = root / "poison"
            poison.mkdir()
            sitecustomize_marker = root / "sitecustomize-loaded"
            sdk_marker = root / "shadow-sdk-loaded"
            (poison / "sitecustomize.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(sitecustomize_marker)!r}).touch()\n",
                encoding="utf-8",
            )
            shadow_sdk = poison / "claude_agent_sdk"
            shadow_sdk.mkdir()
            (shadow_sdk / "__init__.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(sdk_marker)!r}).touch()\n",
                encoding="utf-8",
            )
            isolated_env = os.environ.copy()
            isolated_env["PYTHONPATH"] = str(poison)
            isolated_env.pop("BETTER_CLAUDE_RUNTIME_BROKER", None)
            with runtime.launch.open_runner() as pinned:
                assert pinned.argv[1] == "-I"
                isolated = subprocess.run(
                    list(pinned.argv),
                    cwd=root,
                    env=isolated_env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )
            assert isolated.returncode != 0
            assert not sitecustomize_marker.exists()
            assert not sdk_marker.exists()
            assert (run_dir / "complete.json").is_file(), isolated.stderr
            isolated_completion = json.loads(
                (run_dir / "complete.json").read_text(encoding="utf-8"),
            )
            assert "runtime bootstrap is unavailable" in (
                isolated_completion["error"]
            )

            hydration = {
                "agent_files": {
                    name: str(path)
                    for name, path in runtime.capabilities.agent_files.items()
                },
                "capability_plan": hydrate_frozen_provider_runtime_plan(
                    runtime.capabilities.plan,
                ),
                "claude_cli": {
                    "executable_path": materialized_cli.executable_path,
                    "files": [
                        file_identity_to_dict(identity)
                        for identity in materialized_cli.files
                    ],
                },
                "prewarm_status": runtime.capabilities.prewarm_status,
                "sdk_authority": {
                    "kind": "materialized_package",
                    "package_root": str(package_root),
                },
                "skill_dirs": {
                    name: str(path)
                    for name, path in runtime.capabilities.skill_dirs.items()
                },
            }
            runner_inputs = dict(runtime.inputs)
            runner_inputs["_runtime_hydration"] = hydration
            old_broker = os.environ.get("BETTER_CLAUDE_RUNTIME_BROKER")
            old_sdk_file = runner.claude_agent_sdk.__file__
            os.environ["BETTER_CLAUDE_RUNTIME_BROKER"] = (
                "unix:/tmp/claude-artifact-test.sock"
            )
            runner.claude_agent_sdk.__file__ = str(
                package_root / "__init__.py",
            )
            try:
                plan, skills, agents, cli_path = (
                    runner._runtime_capabilities(
                        run_dir,
                        runner_inputs,
                        runtime,
                    )
                )
                assert plan["mcp_servers"]
                assert skills == runtime.capabilities.skill_dirs
                assert agents == runtime.capabilities.agent_files
                assert cli_path == materialized_cli.executable_path
                assert runner._claude_mcp_variants(plan)["frozen"][
                    "explicit"
                ]["command"] == "frozen-mcp"

                tampered_inputs = dict(runtime.inputs)
                tampered_inputs["_runtime_hydration"] = {
                    **hydration,
                    "skill_dirs": {"injected": str(root)},
                }
                try:
                    runner._runtime_capabilities(
                        run_dir,
                        tampered_inputs,
                        runtime,
                    )
                except RuntimeError:
                    pass
                else:
                    raise AssertionError(
                        "tampered Claude hydration was accepted",
                    )
            finally:
                runner.claude_agent_sdk.__file__ = old_sdk_file
                if old_broker is None:
                    os.environ.pop(
                        "BETTER_CLAUDE_RUNTIME_BROKER",
                        None,
                    )
                else:
                    os.environ["BETTER_CLAUDE_RUNTIME_BROKER"] = old_broker

            poisoned = json.loads(
                (run_dir / "input.json").read_text(encoding="utf-8"),
            )
            poisoned["model"] = "mutated-model"
            atomic_write_json(run_dir / "input.json", poisoned)
            try:
                restore_family_runner_runtime(run_dir)
            except Exception:
                pass
            else:
                raise AssertionError("tampered Claude input was accepted")
        finally:
            if "run_dir" in locals():
                cleanup_installed_family_runtime_capabilities(run_dir)
            os.environ["PATH"] = old_path


def test_sdk_package_materialization_is_run_local_and_source_bound() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "source" / "claude_agent_sdk"
        source.mkdir(parents=True)
        (source / "__init__.py").write_text(
            "MARKER = 'prepared-sdk'\n",
            encoding="utf-8",
        )
        (source / "client.py").write_text(
            "VALUE = 'client'\n",
            encoding="utf-8",
        )
        identity = capture_claude_sdk_package(source)
        target_parent = root / "target"
        package_root = materialize_claude_sdk_package(
            identity,
            target_parent,
        )
        (source / "__init__.py").write_text(
            "MARKER = 'mutated-sdk'\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(package_root.parent)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import claude_agent_sdk; print(claude_agent_sdk.MARKER)",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "prepared-sdk"
        try:
            materialize_claude_sdk_package(
                identity,
                root / "rejected-after-source-drift",
            )
        except Exception:
            pass
        else:
            raise AssertionError("mutated Claude SDK source was accepted")
        (package_root / "injected.py").write_text(
            "VALUE = 'not-prepared'\n",
            encoding="utf-8",
        )
        assert not attest_materialized_claude_sdk_package(
            identity,
            package_root,
        )
        (package_root / "injected.py").unlink()
        (package_root / "__init__.py").chmod(0o600)
        (package_root / "__init__.py").write_text(
            "MARKER = 'tampered-run-local-sdk'\n",
            encoding="utf-8",
        )
        assert not attest_materialized_claude_sdk_package(
            identity,
            package_root,
        )


def test_frozen_runner_is_the_explicit_sdk_authority() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        bin_dir = root / "bin"
        _write_executable(bin_dir / "claude", "#!/bin/sh\nexit 0\n")
        old_path = os.environ.get("PATH", "")
        old_frozen = getattr(sys, "frozen", None)
        had_frozen = hasattr(sys, "frozen")
        old_meipass = getattr(sys, "_MEIPASS", None)
        had_meipass = hasattr(sys, "_MEIPASS")
        old_executable = sys.executable
        old_broker = os.environ.get("BETTER_CLAUDE_RUNTIME_BROKER")
        os.environ["PATH"] = os.pathsep.join((str(bin_dir), old_path))
        os.environ["BETTER_CLAUDE_RUNTIME_BROKER"] = (
            "unix:/tmp/claude-frozen-artifact-test.sock"
        )
        if sys.platform == "darwin":
            bundle_root = (
                root / "Better Agent.app" / "Contents"
            )
            executable = bundle_root / "MacOS" / "Better Agent"
            sidecar = bundle_root / "Frameworks"
        else:
            bundle_root = root / "Better Agent"
            executable = bundle_root / "Better Agent.exe"
            sidecar = bundle_root / "_internal"
        _write_executable(executable, "frozen-executable")
        embedded_sdk = sidecar / "claude_agent_sdk" / "__init__.py"
        embedded_sdk.parent.mkdir(parents=True)
        embedded_sdk.write_text("EMBEDDED = True\n", encoding="utf-8")
        sys.frozen = True
        sys._MEIPASS = str(sidecar)
        sys.executable = str(executable)
        try:
            provider, session = _provider(root)
            prepared = provider.prepare_run(
                **_arguments(root, session["id"], str(uuid.uuid4())),
            )
            launch = family_launch_from_artifact(prepared.artifact)
            assert launch.runner.frozen
            assert len(launch.critical_packages) == 1
            embedded = launch.critical_packages[0]
            assert embedded.package_name == (
                "claude_agent_sdk.embedded_runner"
            )
            assert attest_embedded_claude_sdk(
                embedded,
                launch.runner,
            )

            run_id = prepared.start_arguments()["run_id"]
            run_dir = runs_root() / run_id
            run_dir.mkdir(parents=True)
            atomic_write_json(
                run_dir / "execution.json",
                prepared.artifact.to_dict(),
            )
            atomic_write_json(
                run_dir / "input.json",
                bind_execution_input(
                    prepared.artifact,
                    prepared.artifact.runtime_policy["runner_input"],
                ),
            )
            provider._install_execution_payloads(prepared, run_dir)
            runtime = restore_family_runner_runtime(run_dir)
            (run_dir / "claude-cli").mkdir(mode=0o700)
            materialized_cli = runtime.launch.materialize_sdk(
                run_dir / "claude-cli",
            )
            hydration = {
                "agent_files": {
                    name: str(path)
                    for name, path in runtime.capabilities.agent_files.items()
                },
                "capability_plan": hydrate_frozen_provider_runtime_plan(
                    runtime.capabilities.plan,
                ),
                "claude_cli": {
                    "executable_path": materialized_cli.executable_path,
                    "files": [
                        file_identity_to_dict(identity)
                        for identity in materialized_cli.files
                    ],
                },
                "prewarm_status": runtime.capabilities.prewarm_status,
                "sdk_authority": {
                    "kind": "embedded_runner",
                    "runner_sha256": (
                        runtime.launch.runner.launch.components[0].sha256
                    ),
                },
                "skill_dirs": {
                    name: str(path)
                    for name, path in runtime.capabilities.skill_dirs.items()
                },
            }
            inputs = dict(runtime.inputs)
            inputs["_runtime_hydration"] = hydration
            runner._runtime_capabilities(run_dir, inputs, runtime)

            rejected = dict(runtime.inputs)
            rejected["_runtime_hydration"] = {
                **hydration,
                "sdk_authority": {
                    "kind": "embedded_runner",
                    "runner_sha256": "0" * 64,
                },
            }
            try:
                runner._runtime_capabilities(
                    run_dir,
                    rejected,
                    runtime,
                )
            except RuntimeError:
                pass
            else:
                raise AssertionError(
                    "mismatched embedded SDK authority was accepted",
                )
        finally:
            if "run_dir" in locals():
                cleanup_installed_family_runtime_capabilities(run_dir)
            os.environ["PATH"] = old_path
            if old_broker is None:
                os.environ.pop("BETTER_CLAUDE_RUNTIME_BROKER", None)
            else:
                os.environ["BETTER_CLAUDE_RUNTIME_BROKER"] = old_broker
            sys.executable = old_executable
            if had_frozen:
                sys.frozen = old_frozen
            else:
                del sys.frozen
            if had_meipass:
                sys._MEIPASS = old_meipass
            else:
                del sys._MEIPASS


def test_runner_has_no_mutable_runtime_authority_reads() -> None:
    tree = ast.parse(
        (ROOT / "runner.py").read_text(encoding="utf-8"),
    )
    imported_modules: set[str] = set()
    referenced_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Name):
            referenced_names.add(node.id)
    assert not imported_modules.intersection({
        "extension_store",
        "harness_run_projection",
        "installation_profile",
        "runtime_agents",
        "runtime_skills",
    })
    assert not referenced_names.intersection({
        "_resolve_claude_cli",
        "materialize_runtime_agents",
        "materialize_runtime_skills",
    })


TESTS = (
    test_prepare_freezes_claude_semantics_and_retry_authority,
    test_run_local_payload_rejects_tamper_and_materializes_sdk_cli,
    test_sdk_package_materialization_is_run_local_and_source_bound,
    test_frozen_runner_is_the_explicit_sdk_authority,
    test_runner_has_no_mutable_runtime_authority_reads,
)


if __name__ == "__main__":
    try:
        for test in TESTS:
            test()
    finally:
        TEST_HOME.release()
    print("PASS Claude execution artifact")
