from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import node_connection_status  # noqa: E402
from node_client import NodeClient  # noqa: E402
import paths  # noqa: E402


def test_connection_projection_is_private_atomic_and_current(tmp_path: Path) -> None:
    paths.make_private_directory(tmp_path)
    first = node_connection_status.publish(
        "unreachable",
        generation=2,
        detail="ConnectionRefusedError",
        state_root=tmp_path,
    )
    second = node_connection_status.publish(
        "connected",
        generation=3,
        state_root=tmp_path,
    )
    assert first["state"] == "unreachable"
    assert second["state"] == "connected"
    assert node_connection_status.read(tmp_path) == second
    path = node_connection_status.status_path(tmp_path)
    paths.require_private_file(path)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("state", ["", "ready", "failed"])
def test_connection_projection_rejects_unknown_state(
    tmp_path: Path,
    state: str,
) -> None:
    with pytest.raises(ValueError):
        node_connection_status.publish(state, generation=0, state_root=tmp_path)


def test_read_returns_empty_for_invalid_or_missing_projection(tmp_path: Path) -> None:
    # no file written yet -> read_json yields the default {}
    assert node_connection_status.read(tmp_path) == {}
    path = node_connection_status.status_path(tmp_path)
    # unknown schema version
    path.write_text('{"schema_version": 2, "state": "connected", "generation": 1}')
    assert node_connection_status.read(tmp_path) == {}
    # known schema version but unrecognized state
    path.write_text('{"schema_version": 1, "state": "bogus", "generation": 1}')
    assert node_connection_status.read(tmp_path) == {}


def test_connection_projection_rejects_negative_generation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        node_connection_status.publish(
            "connected",
            generation=-1,
            state_root=tmp_path,
        )


@pytest.mark.anyio
async def test_projection_write_failure_does_not_break_node_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(node_connection_status, "publish", fail)
    await NodeClient()._publish_connection_state("starting")
