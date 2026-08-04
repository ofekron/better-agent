from __future__ import annotations

import ast
import os
from pathlib import Path
import tempfile
import uuid

import pytest

import _test_home
_test_home.isolate("bc-test-remote-spawn-")
os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="bc-test-remote-spawn-os-home-"))

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
_PROVIDER_SOURCE = Path(_BACKEND, "provider_remote.py").read_text()
_PROVIDER_TREE = ast.parse(_PROVIDER_SOURCE)

from execution_template import prepare_execution
from lifecycle_command_model import SelectorAuthoritySnapshot, SelectorIdentity
from native_sid_compatibility import derive_admitted_native_sid_compatibility
from native_sid_compatibility import NativeSidCompatibilityChanged
import node_rpc_handlers
import node_runtime_auth
from node_rpc_handlers import _local_node_backend_url, _prepare_node_execution
from provider_remote import RemoteProviderProxy


def _start_run_node() -> ast.FunctionDef:
    for node in ast.walk(_PROVIDER_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == "_start_run":
            return node
    raise AssertionError("RemoteProviderProxy._start_run not found")


def test_start_run_does_not_reread_mutable_harness_policy() -> None:
    source = ast.get_source_segment(_PROVIDER_SOURCE, _start_run_node()) or ""
    assert "resolve_extension_run_policy" not in source
    assert "session_record" not in source
    assert "worker_record" not in source


def test_start_run_does_not_send_internal_token_field() -> None:
    source = ast.get_source_segment(_PROVIDER_SOURCE, _start_run_node()) or ""
    assert '"internal_token":' not in source


def test_node_run_uses_node_local_backend_proxy() -> None:
    source = Path(_BACKEND, "node_rpc_handlers.py").read_text()
    assert "backend_url=_local_node_backend_url()" in source
    assert 'backend_url=msg.get("backend_url")' not in source


def test_provider_secures_run_directory_before_payload_install() -> None:
    source = Path(_BACKEND, "provider.py").read_text()
    tree = ast.parse(source)
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_persist_and_start_execution"
    )
    handler_source = ast.get_source_segment(source, handler) or ""
    assert handler_source.index("_ensure_execution_run_dir(run_dir)") < (
        handler_source.index("atomic_write_json(")
    )
    assert handler_source.index("_ensure_execution_run_dir(run_dir)") < (
        handler_source.index("self._install_execution_payloads(")
    )


def test_node_execution_preparation_uses_thread_boundary() -> None:
    source = Path(_BACKEND, "node_rpc_handlers.py").read_text()
    tree = ast.parse(source)
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "handle_spawn_run"
    )
    handler_source = ast.get_source_segment(source, handler) or ""
    assert "execution = await asyncio.to_thread(" in handler_source
    assert "_prepare_node_execution," in handler_source
    assert handler_source.index("renewal_task = asyncio.create_task(") < (
        handler_source.index("execution = await asyncio.to_thread(")
    )
    assert handler_source.index("_ctx_by_run[run_id] = ctx") < (
        handler_source.index("execution = await asyncio.to_thread(")
    )


def test_node_cancel_uses_authoritative_run_context() -> None:
    source = Path(_BACKEND, "node_rpc_handlers.py").read_text()
    tree = ast.parse(source)
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "handle_cancel_run"
    )
    handler_source = ast.get_source_segment(source, handler) or ""
    assert "_ctx_by_run.get(run_id)" in handler_source
    assert "ctx.cancel_requested = True" in handler_source
    assert "_dispatch_ctx_cancel(ctx)" in handler_source
    assert "default_provider()" not in handler_source


def test_node_backend_url_defaults_to_node_listener(monkeypatch) -> None:
    monkeypatch.delenv("BETTER_CLAUDE_BACKEND_URL", raising=False)
    monkeypatch.delenv("BETTER_AGENT_BACKEND_URL", raising=False)
    monkeypatch.delenv("BETTER_CLAUDE_NODE_PORT", raising=False)
    monkeypatch.delenv("BETTER_AGENT_NODE_PORT", raising=False)
    assert _local_node_backend_url() == "http://localhost:8002"


def test_remote_spawn_carries_strict_provider_authority() -> None:
    provider_source = ast.get_source_segment(_PROVIDER_SOURCE, _start_run_node()) or ""
    handler_module_source = Path(_BACKEND, "node_rpc_handlers.py").read_text()
    handler_tree = ast.parse(handler_module_source)
    handler = next(
        node
        for node in ast.walk(handler_tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "handle_spawn_run"
    )
    handler_source = ast.get_source_segment(handler_module_source, handler) or ""
    assert '"execution_artifact": node_execution.artifact.to_dict()' in provider_source
    assert "_execution.retry(" not in provider_source
    assert 'ExecutionArtifact.from_dict(msg["execution_artifact"])' in handler_source
    assert ").retry(" not in handler_source
    assert "provider = get_provider(artifact.provider_id)" in handler_source
    assert "_prepare_node_execution," in handler_source
    assert "default_provider" not in handler_source
    authority = next(
        node
        for node in ast.walk(_PROVIDER_TREE)
        if isinstance(node, ast.FunctionDef)
        and node.name == "execution_authority_record"
    )
    authority_source = ast.get_source_segment(_PROVIDER_SOURCE, authority) or ""
    assert 'start_arguments.get("worker_agent_session_id")' in authority_source
    prepare = next(
        node
        for node in ast.walk(_PROVIDER_TREE)
        if isinstance(node, ast.FunctionDef)
        and node.name == "prepare_run"
    )
    prepare_source = ast.get_source_segment(_PROVIDER_SOURCE, prepare) or ""
    assert '"app_session_id": execution_session_id' in prepare_source


def test_node_prepares_platform_local_execution_contract() -> None:
    provider_record = {
        "id": "zai-provider",
        "kind": "z.ai",
        "generation": str(uuid.uuid4()),
        "revision": 3,
        "execution_revision": 0,
    }
    arguments = {
        "run_id": "remote-run",
        "prompt": "prove remote execution",
        "cwd": "C:\\Users\\Lenovo\\better-agent",
        "model": "glm-5.2",
        "reasoning_effort": "medium",
        "session_id": None,
        "mode": "native",
        "app_session_id": "remote-session",
    }
    authority = prepare_execution(provider_record, **arguments).artifact

    class Provider:
        def execution_authority_record(self, _arguments):
            return provider_record

        def prepare_run(self, **start_arguments):
            return prepare_execution(
                provider_record,
                runtime_policy={"prepared_on": "node"},
                **start_arguments,
            )

        def _cleanup_failed_execution_payloads(self, _execution, _run_dir):
            raise AssertionError("valid node-local preparation was cleaned up")

    execution = _prepare_node_execution(
        authority,
        Provider(),
        internal_token="node-runtime-token",
        extra_env=None,
        backend_url="http://localhost:8002",
    )

    assert execution.artifact.runtime_policy == {"prepared_on": "node"}
    assert execution.artifact.template.arguments() == authority.template.arguments()


def test_primary_proxy_defers_spawn_attestation_to_node() -> None:
    provider_record = {
        "id": "zai-provider",
        "kind": "z.ai",
        "generation": str(uuid.uuid4()),
        "revision": 1,
        "execution_revision": 0,
    }
    arguments = {
        "run_id": "remote-authority",
        "prompt": "route only",
        "cwd": "C:\\Users\\Lenovo\\better-agent",
        "model": "glm-5.2",
        "reasoning_effort": "medium",
        "session_id": None,
        "mode": "native",
        "app_session_id": "remote-session",
    }
    execution = prepare_execution(provider_record, **arguments)
    proxy = object.__new__(RemoteProviderProxy)
    proxy.execution_authority_record = lambda _arguments: provider_record

    with proxy._execution_authority_context(execution, arguments) as authority:
        assert authority == provider_record


def test_remote_second_turn_reuses_node_owned_compatibility(tmp_path) -> None:
    provider_record = {
        "id": "remote-provider",
        "kind": "claude",
        "generation": str(uuid.uuid4()),
        "revision": 4,
        "execution_revision": 0,
    }
    compatibility = derive_admitted_native_sid_compatibility(
        engine="claude-native",
        node_id="worker-remote",
        thread_store_root=tmp_path / "projects",
        claude_project_namespace="-remote-work",
    )
    proxy = object.__new__(RemoteProviderProxy)
    proxy.node_id = "worker-remote"
    proxy.execution_authority_record = lambda _arguments: provider_record
    proxy._node_native_sid_compatibility = lambda _execution: compatibility
    arguments = {
        "run_id": "remote-second-turn",
        "prompt": "continue",
        "cwd": "/remote/work",
        "model": "claude-sonnet",
        "reasoning_effort": "medium",
        "session_id": "remote-thread-1",
        "mode": "native",
        "app_session_id": "remote-session",
    }
    first_execution = proxy.prepare_run(**arguments)
    second_execution = proxy.prepare_run(**arguments)
    expected = compatibility.to_dict()
    assert first_execution.artifact.runtime_policy == {
        "native_sid_compatibility": expected
    }
    assert second_execution.artifact.runtime_policy == {
        "native_sid_compatibility": expected
    }

    selector = SelectorIdentity("remote-provider", "claude-sonnet", "native")
    first, decision = SelectorAuthoritySnapshot().admit_attempt(
        selector,
        expected,
        primary_native_sid="remote-thread-1",
        supervisor_native_sid=None,
        primary_native_sid_compatibility=expected,
        supervisor_native_sid_compatibility=None,
    )
    assert decision == "admitted"
    second, decision = first.admit_attempt(
        selector,
        second_execution.artifact.runtime_policy["native_sid_compatibility"],
        primary_native_sid="remote-thread-1",
        supervisor_native_sid=None,
        primary_native_sid_compatibility=expected,
        supervisor_native_sid_compatibility=None,
    )
    assert decision == "admitted"
    assert second.primary_native_sid == "remote-thread-1"


def test_remote_compatibility_is_prepared_and_released_on_node(
    tmp_path,
    monkeypatch,
) -> None:
    provider_record = {
        "id": "node-provider",
        "kind": "claude",
        "generation": str(uuid.uuid4()),
        "revision": 5,
        "execution_revision": 0,
    }
    arguments = {
        "run_id": "node-compatibility",
        "prompt": "prepare only",
        "cwd": "/node/work",
        "model": "claude-sonnet",
        "reasoning_effort": "medium",
        "session_id": None,
        "mode": "native",
        "app_session_id": "node-session",
    }
    authority = prepare_execution(provider_record, **arguments).artifact
    compatibility = derive_admitted_native_sid_compatibility(
        engine="claude-native",
        node_id=node_rpc_handlers._local_node_id(),
        thread_store_root=tmp_path / "projects",
        claude_project_namespace="-node-work",
    )

    class Provider:
        released = False

        def execution_authority_record(self, _arguments):
            return provider_record

        def prepare_run(self, **start_arguments):
            return prepare_execution(
                provider_record,
                runtime_policy={
                    "native_sid_compatibility": compatibility.to_dict()
                },
                **start_arguments,
            )

        def _cleanup_failed_execution_payloads(self, _execution, _run_dir):
            raise AssertionError("valid node compatibility was cleaned as failed")

        def discard_prepared_execution(self, _execution):
            self.released = True

    provider = Provider()
    monkeypatch.setattr(node_rpc_handlers, "get_provider", lambda _id: provider)
    monkeypatch.setattr(node_runtime_auth, "token", lambda: "node-token")
    result = node_rpc_handlers._rpc_prepare_remote_native_sid_compatibility(
        {"execution_artifact": authority.to_dict()}
    )
    assert result == {"native_sid_compatibility": compatibility.to_dict()}
    assert provider.released


def test_remote_coordinator_transports_node_projection_without_path_inference() -> None:
    prepare = next(
        node
        for node in ast.walk(_PROVIDER_TREE)
        if isinstance(node, ast.FunctionDef) and node.name == "prepare_run"
    )
    source = ast.get_source_segment(_PROVIDER_SOURCE, prepare) or ""
    assert "_node_native_sid_compatibility(provisional)" in source
    assert "thread_store_root" not in source
    assert "config_dir" not in source
    assert "ba_home" not in source


def test_reconnect_config_root_mismatch_releases_then_reprepares(tmp_path) -> None:
    provider_record = {
        "id": "reconnected-provider",
        "kind": "claude",
        "generation": str(uuid.uuid4()),
        "revision": 6,
        "execution_revision": 0,
    }
    arguments = {
        "run_id": "reconnected-compatibility",
        "prompt": "continue",
        "cwd": "/node/work",
        "model": "claude-sonnet",
        "reasoning_effort": "medium",
        "session_id": "native-thread",
        "mode": "native",
        "app_session_id": "node-session",
    }
    expected = derive_admitted_native_sid_compatibility(
        engine="claude-native",
        node_id=node_rpc_handlers._local_node_id(),
        thread_store_root=tmp_path / "before-reconnect" / "projects",
        claude_project_namespace="-node-work",
    )
    changed = derive_admitted_native_sid_compatibility(
        engine="claude-native",
        node_id=node_rpc_handlers._local_node_id(),
        thread_store_root=tmp_path / "after-reconnect" / "projects",
        claude_project_namespace="-node-work",
    )
    authority = prepare_execution(
        provider_record,
        runtime_policy={"native_sid_compatibility": expected.to_dict()},
        **arguments,
    ).artifact

    class Provider:
        def __init__(self):
            self.compatibilities = [changed, expected]
            self.released = 0

        def execution_authority_record(self, _arguments):
            return provider_record

        def prepare_run(self, **start_arguments):
            compatibility = self.compatibilities.pop(0)
            return prepare_execution(
                provider_record,
                runtime_policy={
                    "native_sid_compatibility": compatibility.to_dict()
                },
                **start_arguments,
            )

        def discard_prepared_execution(self, _execution):
            self.released += 1

    provider = Provider()
    with pytest.raises(NativeSidCompatibilityChanged):
        _prepare_node_execution(
            authority,
            provider,
            internal_token="node-token",
            extra_env=None,
            backend_url="http://localhost:8002",
        )
    assert provider.released == 1
    retried = _prepare_node_execution(
        authority,
        provider,
        internal_token="node-token",
        extra_env=None,
        backend_url="http://localhost:8002",
    )
    assert retried.artifact.runtime_policy["native_sid_compatibility"] == (
        expected.to_dict()
    )


def test_remote_spawn_forwards_effective_harness_policy() -> None:
    provider_source = ast.get_source_segment(_PROVIDER_SOURCE, _start_run_node()) or ""
    handler_source = Path(_BACKEND, "node_rpc_handlers.py").read_text()
    protocol_source = Path(_BACKEND, "node_protocol.py").read_text()
    assert "**node_execution.artifact.template.arguments()" in provider_source
    assert "payload.update(run_policy)" not in provider_source
    for field in (
        "extra_mcp_servers",
        "disabled_builtin_tools",
        "provider_run_config",
        "capability_contexts",
        "resolved_harness_run_config",
    ):
        assert f"{field}:" in protocol_source
    assert 'provider_run_config=msg.get("provider_run_config")' not in handler_source
    assert 'capability_contexts=msg.get("capability_contexts")' not in handler_source
    assert 'resolved_harness_run_config=msg.get(' not in handler_source
    assert "if not started:" in handler_source
    assert "execution.wait_for_admission" in handler_source
    assert handler_source.index("execution.wait_for_admission") < handler_source.index(
        'atomic_write_json(rd / "remote_ctx.json"',
    )


def test_remote_registration_requires_connection_and_send_failure_cleans_up() -> None:
    source = ast.get_source_segment(_PROVIDER_SOURCE, _start_run_node()) or ""
    assert source.index("node_store.get_connection") < source.index(
        'atomic_write_json(run_dir / "backend_state.json"',
    )
    assert "self._dispatch_admission_or_fail" in source
    assert "routing_session_id = _execution.artifact.routing_session_id" in source
    assert '"app_session_id": routing_session_id' in source
    assert '"persist_to": app_session_id' in source


if __name__ == "__main__":
    test_start_run_does_not_reread_mutable_harness_policy()
    test_start_run_does_not_send_internal_token_field()
    test_node_run_uses_node_local_backend_proxy()
    test_provider_secures_run_directory_before_payload_install()
    test_node_execution_preparation_uses_thread_boundary()
    test_node_cancel_uses_authoritative_run_context()
    test_remote_spawn_carries_strict_provider_authority()
    test_remote_spawn_forwards_effective_harness_policy()
    test_remote_registration_requires_connection_and_send_failure_cleans_up()
    print("provider_remote spawn-payload test passed")
