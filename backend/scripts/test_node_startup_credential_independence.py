from __future__ import annotations

import asyncio
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import config_store
import local_machine_identity
import main_node


def _projected_api_key_state() -> dict:
    return {
        "default_provider_id": "zai",
        "provider_state_projected": True,
        "providers": [
            {
                "id": "zai",
                "kind": "claude",
                "mode": "api_key",
                "base_url": "https://api.z.ai/api/anthropic",
                "config_dir": "",
                "suspended": False,
            }
        ],
    }


def test_non_secret_startup_env_does_not_read_credentials() -> None:
    state = _projected_api_key_state()
    with (
        patch.object(config_store, "_load_state", return_value=state),
        patch.object(
            config_store.credential_session_client,
            "request",
            side_effect=AssertionError("startup read a credential"),
        ),
        patch.object(config_store, "_write_engine_env"),
        patch.dict(os.environ, {}, clear=False),
    ):
        config_store.apply_provider_config_env_vars()
        assert os.environ["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"


def test_main_node_import_does_not_use_credential_channel() -> None:
    server_connection, backend_connection = multiprocessing.Pipe(duplex=True)
    handle = backend_connection.fileno()
    os.set_inheritable(handle, True)
    env = {
        **os.environ,
        "BETTER_AGENT_CREDENTIAL_SESSION_FD": str(handle),
        "PYTHONPATH": str(ROOT / "backend"),
    }
    code = (
        "import config_store; "
        f"config_store._load_state=lambda: {_projected_api_key_state()!r}; "
        "import main_node; print('IMPORTED')"
    )
    popen_kwargs: dict[str, object]
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.lpAttributeList = {"handle_list": [handle]}
        popen_kwargs = {"close_fds": True, "startupinfo": startupinfo}
    else:
        popen_kwargs = {"pass_fds": (handle,)}
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            # A cold `import main_node` (a large FastAPI app importing dozens
            # of modules) measures 7-9.5s in this suite's Docker test
            # container, so this is a generous outer bound, not the proof of
            # correctness: if the import actually blocked reading the
            # (unanswered) credential channel pipe, it would hang
            # indefinitely rather than just run slow, and this still catches
            # that as a hard TimeoutExpired. The actual proof that the
            # channel was never used is `server_connection.poll(0)` below —
            # a direct, timing-independent check of the pipe's own state.
            timeout=30,
            check=True,
            **popen_kwargs,
        )
        assert result.stdout.strip() == "IMPORTED"
        assert not server_connection.poll(0), (
            "main_node import wrote a request onto the credential channel"
        )
    finally:
        os.set_inheritable(handle, False)
        backend_connection.close()
        server_connection.close()


def test_execution_default_resolution_still_reads_credentials() -> None:
    requests: list[tuple[str, str]] = []

    def request(op: str, provider_id: str, **_kwargs: object) -> dict:
        requests.append((op, provider_id))
        return {"status": "missing"}

    with (
        patch.object(
            config_store.credential_session_client,
            "available",
            return_value=True,
        ),
        patch.object(
            config_store.credential_session_client,
            "request",
            side_effect=request,
        ),
    ):
        resolved = config_store._runtime_default_provider_id(
            _projected_api_key_state()
        )
    assert resolved is None
    assert requests == [("read", "zai")]


def test_node_startup_prunes_without_instantiating_provider(monkeypatch) -> None:
    # local_machine_identity._local_machine_id is a process-global singleton
    # with no test-home reset hook: initialize_local_machine_id() raises if
    # it's already set to a DIFFERENT node_id than the one being initialized
    # now. Any earlier test/module in the same pytest process that already
    # initialized it (to a node_id other than "lenovo" below) would make
    # main_node._on_startup()'s own initialization call raise here. This
    # test is specifically about a from-scratch startup, so clear it first.
    monkeypatch.setattr(local_machine_identity, "_local_machine_id", None)

    class Client:
        async def start(self) -> None:
            started.append(True)

    started: list[bool] = []
    identity = type("Identity", (), {"node_id": "lenovo"})()
    topology = type(
        "Topology",
        (),
        {
            "primary": type(
                "Primary",
                (),
                {"id": "primary", "address": "ws://127.0.0.1:18765"},
            )()
        },
    )()
    with (
        patch.object(main_node, "acquire_backend_instance_lock"),
        patch.object(main_node, "load_topology", return_value=topology),
        patch.object(main_node.node_identity, "load_or_create", return_value=identity),
        patch.object(main_node.runs_dir, "prune_old_completed_runs") as prune,
        patch.object(
            main_node,
            "default_provider",
            side_effect=AssertionError("startup instantiated a provider"),
        ),
        patch.object(main_node, "NodeClient", return_value=Client()),
        patch.object(main_node, "set_singleton"),
    ):
        asyncio.run(main_node._on_startup())
    prune.assert_called_once_with()
    assert started == [True]
