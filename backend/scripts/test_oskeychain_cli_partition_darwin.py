#!/usr/bin/env python3
"""Real-keychain proof of the canonical credential partition contract.

Items created through /usr/bin/security get a `partition_id` of
`apple-tool:` — trust bound to Apple's stable signed CLI, not to any
cdhash of ours — so reads survive every rebuild of the credential
authority binary and every python upgrade. All operations here are
prompt-free by construction (creation and reads both go through the
same Apple tool), so this is safe on a developer machine and in CI.
"""
from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import oskeychain  # noqa: E402

_LOGIN_KEYCHAIN = Path.home() / "Library" / "Keychains" / "login.keychain-db"


def _keychain_usable() -> bool:
    if sys.platform != "darwin":
        return False
    probe = subprocess.run(
        ["/usr/bin/security", "show-keychain-info", str(_LOGIN_KEYCHAIN)],
        capture_output=True,
        timeout=10,
    )
    return probe.returncode == 0


def _partition_ids(service: str) -> str:
    dump = subprocess.run(
        ["/usr/bin/security", "dump-keychain", "-a", str(_LOGIN_KEYCHAIN)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    block_start = dump.index(f'"svce"<blob>="{service}"')
    partition_marker = dump.index("partition_id", block_start)
    description_start = dump.index("description: ", partition_marker)
    description_end = dump.index("\n", description_start)
    return dump[description_start + len("description: "):description_end]


def test_cli_partition_survives_store_update_and_reads_silently() -> None:
    if not _keychain_usable():
        print("SKIP: no unlocked darwin login keychain")
        return
    service = f"ba-test-cli-partition-{uuid.uuid4().hex[:12]}"
    account = "provider:integration-test"
    try:
        oskeychain.store(service, account, "secret-one")
        assert oskeychain.get(service, account).rstrip("\n") == "secret-one"
        assert "apple-tool:" in _partition_ids(service)

        # In-place update (-U) must keep the apple-tool partition.
        oskeychain.store(service, account, "secret-two")
        assert oskeychain.get(service, account).rstrip("\n") == "secret-two"
        assert "apple-tool:" in _partition_ids(service)

        oskeychain.delete(service, account)
        assert oskeychain.get(service, account) is None
    finally:
        subprocess.run(
            ["/usr/bin/security", "delete-generic-password", "-s", service, "-a", account],
            capture_output=True,
            timeout=10,
        )


if __name__ == "__main__":
    test_cli_partition_survives_store_update_and_reads_silently()
    print("OK: oskeychain cli partition")
