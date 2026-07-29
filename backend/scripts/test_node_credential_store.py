from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "desktop"))

import node_credential_store
from node_credential_store import node_provider_credential_store
from credential_session import ProviderCredentialBroker


def test_node_credentials_survive_restart_and_remain_encrypted(tmp_path) -> None:
    first = node_provider_credential_store(tmp_path)
    first.store("provider-a", "credential-plaintext")

    second = node_provider_credential_store(tmp_path)
    assert second.read("provider-a") == "credential-plaintext"

    authority = tmp_path / "node-credential-authority"
    seed = authority / "seed-v1"
    keyring = authority / "credentials-v1.cfg"
    assert b"credential-plaintext" not in keyring.read_bytes()
    assert stat.S_IMODE(authority.stat().st_mode) == 0o700
    assert stat.S_IMODE(seed.stat().st_mode) == 0o600
    assert stat.S_IMODE(keyring.stat().st_mode) == 0o600


def test_keyring_file_is_secured_during_store_construction(
    tmp_path,
    monkeypatch,
) -> None:
    secured: list[Path] = []
    original = node_credential_store.make_private_file

    def secure(path: Path) -> None:
        secured.append(path)
        original(path)

    monkeypatch.setattr(node_credential_store, "make_private_file", secure)
    node_provider_credential_store(tmp_path)

    assert tmp_path / "node-credential-authority" / "credentials-v1.cfg" in secured


def test_node_credential_session_subprocess_round_trip(tmp_path) -> None:
    broker = ProviderCredentialBroker(node_provider_credential_store(tmp_path))
    session = broker.open_session()
    session.start()
    env = {
        **os.environ,
        **session.backend_env(),
        "PYTHONPATH": str(ROOT / "backend"),
    }
    code = (
        "import json, credential_session_client as client; "
        "stored = client.request('store', 'provider-a', value='secret-a'); "
        "read = client.request('read', 'provider-a'); "
        "print(json.dumps({'stored': stored, 'read': read}))"
    )
    try:
        process = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            **session.backend_popen_kwargs(),
        )
        session.revoke_backend_inheritance()
    finally:
        session.stop()
    assert json.loads(process.stdout) == {
        "stored": {"status": "available"},
        "read": {"status": "available", "value": "secret-a"},
    }


def test_concurrent_node_credential_store_creation_publishes_one_seed(
    tmp_path,
) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = list(
            pool.map(
                lambda _index: node_provider_credential_store(tmp_path),
                range(16),
            )
        )

    assert len(stores) == 16
    authority = tmp_path / "node-credential-authority"
    assert len((authority / "seed-v1").read_bytes()) == 32
    assert list(authority.glob(".seed-v1.*")) == []


def test_node_credential_seed_symlink_is_rejected(tmp_path) -> None:
    authority = tmp_path / "node-credential-authority"
    authority.mkdir(mode=0o700)
    outside = tmp_path / "outside-seed"
    outside.write_bytes(b"x" * 32)
    (authority / "seed-v1").symlink_to(outside)

    try:
        node_provider_credential_store(tmp_path)
    except PermissionError:
        pass
    else:
        raise AssertionError("node credential seed accepted a symlink")

    assert outside.read_bytes() == b"x" * 32


def test_node_credential_authority_directory_symlink_is_rejected(tmp_path) -> None:
    outside = tmp_path / "outside-authority"
    outside.mkdir()
    authority = tmp_path / "node-credential-authority"
    authority.symlink_to(outside, target_is_directory=True)

    try:
        node_provider_credential_store(tmp_path)
    except PermissionError:
        pass
    else:
        raise AssertionError("node credential authority accepted a symlink")

    assert list(outside.iterdir()) == []


def test_node_credential_keyring_symlink_is_rejected(tmp_path) -> None:
    store = node_provider_credential_store(tmp_path)
    store.store("provider-a", "first")
    authority = tmp_path / "node-credential-authority"
    keyring = authority / "credentials-v1.cfg"
    keyring.unlink()
    outside = tmp_path / "outside-keyring"
    outside.write_text("unchanged", encoding="utf-8")
    keyring.symlink_to(outside)

    try:
        node_provider_credential_store(tmp_path)
    except PermissionError:
        pass
    else:
        raise AssertionError("node credential keyring accepted a symlink")

    assert outside.read_text(encoding="utf-8") == "unchanged"
