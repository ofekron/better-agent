from __future__ import annotations

import json
import sys
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "backend"))

import credential_session  # noqa: E402


def request(session, op: str, provider_id: str, value: str | None = None) -> dict:
    conn = Client(session._address, family=session._family, authkey=session._authkey)
    try:
        payload = {"op": op, "provider_id": provider_id}
        if value is not None:
            payload["value"] = value
        conn.send_bytes(json.dumps(payload).encode())
        return json.loads(conn.recv_bytes(maxlength=128 * 1024).decode())
    finally:
        conn.close()


def main() -> None:
    real_get = credential_session.oskeychain.get
    real_store = credential_session.oskeychain.store
    real_delete = credential_session.oskeychain.delete
    reads = 0
    stores = 0
    deletes = 0
    values: dict[tuple[str, str], str] = {}
    blocked = True

    def get(service: str, account: str):
        nonlocal reads
        reads += 1
        if blocked:
            raise RuntimeError("blocked")
        return values.get((service, account))

    credential_session.oskeychain.get = get
    def store(service: str, account: str, value: str) -> None:
        nonlocal stores
        stores += 1
        if blocked:
            raise RuntimeError("blocked")
        values[(service, account)] = value

    def delete(service: str, account: str) -> None:
        nonlocal deletes
        deletes += 1
        if blocked:
            raise RuntimeError("blocked")
        values.pop((service, account), None)

    credential_session.oskeychain.store = store
    credential_session.oskeychain.delete = delete
    session = credential_session.ProviderCredentialSession()
    session.start()
    try:
        assert request(session, "status", "provider-1") == {"status": "unknown"}
        assert request(session, "read", "provider-1") == {"status": "blocked"}
        assert request(session, "read", "provider-1") == {"status": "blocked"}
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

        try:
            Client(session._address, family=session._family, authkey=b"wrong-auth-key")
        except AuthenticationError:
            pass
        else:
            raise AssertionError("wrong broker auth must be rejected")

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
        credential_session.oskeychain.get = real_get
        credential_session.oskeychain.store = real_store
        credential_session.oskeychain.delete = real_delete
    assert session._states == {}
    print("OK: desktop credential session")


if __name__ == "__main__":
    main()
