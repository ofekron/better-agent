from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths  # noqa: E402
from node_launcher_lease import (  # noqa: E402
    NodeLauncherBusyError,
    NodeLauncherLease,
)
from primary_launcher_lease import PrimaryLauncherLease  # noqa: E402


def _private_home(root: Path) -> Path:
    home = root / "home"
    home.mkdir(mode=0o700)
    paths.make_private_directory(home)
    return home


def test_node_launchers_contend_without_blocking_primary_launcher() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = _private_home(root)
        node = NodeLauncherLease.acquire(home, checkout=root)
        try:
            with pytest.raises(NodeLauncherBusyError):
                NodeLauncherLease.acquire(home, checkout=root)
            primary = PrimaryLauncherLease.acquire(home, checkout=root)
            primary.release()
        finally:
            node.release()


def test_node_launcher_metadata_has_distinct_private_authority() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = _private_home(root)
        lease = NodeLauncherLease.acquire(home, checkout=root)
        try:
            path = home / "node-launcher.lock"
            paths.require_private_file(path)
            metadata = json.loads(path.read_text(encoding="utf-8"))
            assert metadata["lease_id"] == lease.lease_id
            assert metadata["home"] == os.path.normcase(str(home.resolve()))
        finally:
            lease.release()
