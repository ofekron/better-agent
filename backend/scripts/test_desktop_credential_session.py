from __future__ import annotations

import atexit
import json
import os
import struct
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST_HOME = Path(tempfile.mkdtemp(prefix="ba-credential-session-"))
atexit.register(shutil.rmtree, TEST_HOME, ignore_errors=True)
os.environ["BETTER_AGENT_HOME"] = str(TEST_HOME)
os.environ["BETTER_CLAUDE_HOME"] = str(TEST_HOME)
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "backend"))

import credential_session  # noqa: E402


def request(session, op: str, provider_id: str, value: str | None = None) -> dict:
    payload = {"op": op, "provider_id": provider_id, "request_id": "0" * 32}
    if value is not None:
        payload["value"] = value
    session._backend_connection.send_bytes(json.dumps(payload).encode())
    response = json.loads(
        session._backend_connection.recv_bytes(maxlength=128 * 1024).decode()
    )
    response.pop("request_id")
    return response


def backend_request(session, op: str, provider_id: str) -> dict:
    env = {**os.environ, **session.backend_env(), "PYTHONPATH": str(ROOT / "backend")}
    code = (
        "import json, credential_session_client as client; "
        f"print(json.dumps(client.request({op!r}, {provider_id!r})))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        **session.backend_popen_kwargs(),
    )
    return json.loads(proc.stdout)


def assert_unrelated_process_cannot_connect(session) -> None:
    env = {**os.environ, **session.backend_env(), "PYTHONPATH": str(ROOT / "backend")}
    code = (
        "import credential_session_client as client; "
        "assert not client.available()"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        close_fds=True,
        check=True,
    )


def main() -> None:
    real_get = credential_session.oskeychain.get
    real_store = credential_session.oskeychain.store
    real_delete = credential_session.oskeychain.delete
    reads = 0
    stores = 0
    deletes = 0
    values: dict[tuple[str, str], str] = {}
    blocked = True
    reasons: list[str] = []
    (TEST_HOME / "config.json").write_text(json.dumps({
        "providers": [{"id": "provider-1", "name": "Friendly Provider"}],
    }), encoding="utf-8")

    def get(service: str, account: str, *, reason: str | None = None):
        nonlocal reads
        reads += 1
        if reason:
            reasons.append(reason)
        if blocked:
            raise RuntimeError("blocked")
        return values.get((service, account))

    credential_session.oskeychain.get = get
    def store(
        service: str, account: str, value: str, *, reason: str | None = None,
    ) -> None:
        nonlocal stores
        stores += 1
        if blocked:
            raise RuntimeError("blocked")
        values[(service, account)] = value

    def delete(service: str, account: str, *, reason: str | None = None) -> None:
        nonlocal deletes
        deletes += 1
        if blocked:
            raise RuntimeError("blocked")
        values.pop((service, account), None)

    credential_session.oskeychain.store = store
    credential_session.oskeychain.delete = delete
    broker = credential_session.ProviderCredentialBroker()
    session = broker.open_session()
    session.start()
    try:
        backend_env = session.backend_env()
        assert set(backend_env) == {"BETTER_AGENT_CREDENTIAL_SESSION_FD"}
        assert "ADDRESS" not in json.dumps(backend_env)
        assert "AUTH" not in json.dumps(backend_env)
        assert_unrelated_process_cannot_connect(session)
        session._backend_connection.send_bytes(json.dumps({
            "op": "status",
            "provider_id": "provider-stale",
            "request_id": "1" * 32,
        }).encode())
        assert backend_request(session, "status", "provider-1") == {"status": "unknown"}
        assert backend_request(session, "read", "provider-1") == {"status": "blocked"}
        assert reads == 1
        assert reasons == ["Better Agent needs the API key for Friendly Provider"]
        first_connection = session._backend_connection
        if os.name != "nt":
            os.write(first_connection.fileno(), struct.pack("!i", 64) + b"partial")
        session.stop()
        session = broker.open_session()
        session.start()
        assert first_connection.closed
        assert session._backend_connection is not first_connection
        assert backend_request(session, "read", "provider-1") == {"status": "blocked"}
        assert reads == 1
        assert request(session, "store", "provider-1", "replacement") == {"status": "blocked"}
        assert request(session, "delete", "provider-1") == {"status": "blocked"}
        assert stores == 0
        assert deletes == 0

        assert request(session, "store", "provider-store-blocked", "replacement") == {
            "status": "blocked"
        }
        assert request(session, "read", "provider-store-blocked") == {"status": "blocked"}
        assert stores == 1
        assert reads == 1

        assert request(session, "delete", "provider-delete-blocked") == {"status": "blocked"}
        assert request(session, "read", "provider-delete-blocked") == {"status": "blocked"}
        assert deletes == 1
        assert reads == 1

        blocked = False
        assert request(session, "retry", "provider-1") == {"status": "missing"}
        assert reads == 3
        assert request(session, "read", "provider-1") == {"status": "missing"}
        assert reads == 3

        assert request(session, "store", "provider-1", "replacement") == {
            "status": "available"
        }
        assert request(session, "read", "provider-1") == {
            "status": "available",
            "value": "replacement",
        }
        assert reads == 3
        assert request(session, "delete", "provider-1") == {"status": "missing"}
    finally:
        session.stop()
        broker.clear()
        credential_session.oskeychain.get = real_get
        credential_session.oskeychain.store = real_store
        credential_session.oskeychain.delete = real_delete
    assert broker._states == {}
    shutil.rmtree(TEST_HOME)
    print("OK: desktop credential session")


if __name__ == "__main__":
    main()
