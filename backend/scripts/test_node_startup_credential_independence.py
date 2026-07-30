from __future__ import annotations

import asyncio
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import config_store
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
    started_at = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
            **popen_kwargs,
        )
    finally:
        os.set_inheritable(handle, False)
        backend_connection.close()
        server_connection.close()
    assert result.stdout.strip() == "IMPORTED"
    assert time.monotonic() - started_at < 3


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


def test_node_startup_prunes_without_instantiating_provider() -> None:
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
