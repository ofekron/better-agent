from __future__ import annotations

import ast
import os
from pathlib import Path
import tempfile

import _test_home
_test_home.isolate("bc-test-remote-spawn-")
os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="bc-test-remote-spawn-os-home-"))

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
_PROVIDER_SOURCE = Path(_BACKEND, "provider_remote.py").read_text()
_PROVIDER_TREE = ast.parse(_PROVIDER_SOURCE)


def _start_run_node() -> ast.FunctionDef:
    for node in ast.walk(_PROVIDER_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == "start_run":
            return node
    raise AssertionError("RemoteProviderProxy.start_run not found")


def test_start_run_binds_extension_policy_records_locally() -> None:
    """Regression: `start_run` referenced `session_record`/`worker_record`
    when building the spawn_run payload but never assigned them, so every
    remote spawn raised NameError at payload construction. They must be
    local names of the function, not free/global lookups."""
    start_run = _start_run_node()
    assigned = {
        node.id
        for node in ast.walk(start_run)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    loaded = {
        node.id
        for node in ast.walk(start_run)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    for name in ("session_record", "worker_record"):
        if name in loaded and name not in assigned:
            raise AssertionError(
                f"{name!r} is referenced as a free/global name in start_run; "
                f"it must be assigned locally before the spawn_run payload"
            )
        if name not in assigned:
            raise AssertionError(f"{name!r} is not bound in start_run")


def test_start_run_does_not_send_internal_token_field() -> None:
    source = ast.get_source_segment(_PROVIDER_SOURCE, _start_run_node()) or ""
    assert '"internal_token":' not in source


def test_node_run_uses_node_local_backend_proxy() -> None:
    source = Path(_BACKEND, "node_rpc_handlers.py").read_text()
    assert 'backend_url=get_env(' in source
    assert 'backend_url=msg.get("backend_url")' not in source


if __name__ == "__main__":
    test_start_run_binds_extension_policy_records_locally()
    test_start_run_does_not_send_internal_token_field()
    test_node_run_uses_node_local_backend_proxy()
    print("provider_remote spawn-payload test passed")
