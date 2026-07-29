from __future__ import annotations

import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "desktop"))

from node_credential_store import node_provider_credential_store


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
