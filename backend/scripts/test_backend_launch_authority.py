from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import backend_launch_authority as bla
from backend_instance_lock import reserve_primary_backend_instance_lock
from primary_launcher_lease import PrimaryLauncherLease


@pytest.fixture
def checkout() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def real_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("BETTER_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("BETTER_CLAUDE_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def bound_launch(real_home: Path, checkout: Path, monkeypatch):
    lease = PrimaryLauncherLease.acquire(real_home, checkout=checkout)
    reservation = reserve_primary_backend_instance_lock(lease)
    env = bla.issue_primary_backend_launch(
        checkout=checkout,
        state_root=real_home,
        launcher_lease=lease,
        backend_reservation=reservation,
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    try:
        yield lease, reservation, env
    finally:
        reservation.release()
        lease.release()


def _payload(root: Path) -> dict:
    return json.loads((root / bla._AUTHORITY_FILE).read_text(encoding="utf-8"))


def test_launch_env_keys_have_no_secret() -> None:
    keys = set(bla.launch_env_keys())
    assert "BETTER_AGENT_BACKEND_LAUNCH_TOKEN" not in keys
    assert bla._GENERATION_ENV in keys
    assert bla._LAUNCHER_LEASE_ID_ENV in keys
    assert bla._BACKEND_RESERVATION_ID_ENV in keys


def test_issue_binds_lease_reservation_home_and_lock_identity(
    real_home: Path,
    checkout: Path,
) -> None:
    lease = PrimaryLauncherLease.acquire(real_home, checkout=checkout)
    reservation = reserve_primary_backend_instance_lock(lease)
    try:
        env = bla.issue_primary_backend_launch(
            checkout=checkout,
            state_root=real_home,
            launcher_lease=lease,
            backend_reservation=reservation,
        )
        payload = _payload(real_home)
        assert set(payload) == bla._RECORD_KEYS
        assert payload["version"] == 2
        assert payload["launcher_lease_id"] == lease.lease_id
        assert payload["backend_reservation_id"] == reservation.reservation_id
        assert payload["backend_lock_identity"] == reservation.lock_identity
        assert payload["state_root"] == str(real_home.resolve())
        assert payload["checkout"] == str(checkout.resolve())
        assert env[bla._LAUNCHER_LEASE_ID_ENV] == lease.lease_id
        assert env[bla._BACKEND_RESERVATION_ID_ENV] == reservation.reservation_id
    finally:
        reservation.release()
        lease.release()


def test_issue_rejects_duck_typed_owners(real_home: Path, checkout: Path) -> None:
    lease = PrimaryLauncherLease.acquire(real_home, checkout=checkout)
    reservation = reserve_primary_backend_instance_lock(lease)
    try:
        with pytest.raises(RuntimeError, match="concrete launcher lease"):
            bla.issue_primary_backend_launch(
                checkout=checkout,
                state_root=real_home,
                launcher_lease=object(),  # type: ignore[arg-type]
                backend_reservation=reservation,
            )
        with pytest.raises(RuntimeError, match="concrete backend reservation"):
            bla.issue_primary_backend_launch(
                checkout=checkout,
                state_root=real_home,
                launcher_lease=lease,
                backend_reservation=object(),  # type: ignore[arg-type]
            )
    finally:
        reservation.release()
        lease.release()


@pytest.mark.parametrize("generation", ["short", "z" * 32, "A" * 32])
def test_issue_rejects_invalid_generation(
    real_home: Path,
    checkout: Path,
    generation: str,
) -> None:
    lease = PrimaryLauncherLease.acquire(real_home, checkout=checkout)
    reservation = reserve_primary_backend_instance_lock(lease)
    try:
        with pytest.raises(ValueError, match="32 lowercase hex"):
            bla.issue_primary_backend_launch(
                checkout=checkout,
                state_root=real_home,
                generation=generation,
                launcher_lease=lease,
                backend_reservation=reservation,
            )
    finally:
        reservation.release()
        lease.release()


def test_issue_rejects_reservation_from_other_home(
    real_home: Path,
    checkout: Path,
    tmp_path: Path,
) -> None:
    lease = PrimaryLauncherLease.acquire(real_home, checkout=checkout)
    reservation = reserve_primary_backend_instance_lock(lease)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    try:
        with pytest.raises(Exception):
            bla.issue_primary_backend_launch(
                checkout=checkout,
                state_root=other,
                launcher_lease=lease,
                backend_reservation=reservation,
            )
    finally:
        reservation.release()
        lease.release()


def test_consume_is_single_use_and_clears_transfer_selectors(
    real_home: Path,
    checkout: Path,
    bound_launch,
) -> None:
    lease, reservation, _ = bound_launch
    authority = bla.consume_primary_backend_launch_authority(
        launcher_lease_id=lease.lease_id,
        backend_reservation_id=reservation.reservation_id,
        backend_lock_identity=reservation.lock_identity,
        executing_checkout=checkout,
    )
    assert authority.launcher_lease_id == lease.lease_id
    assert authority.backend_reservation_id == reservation.reservation_id
    assert not (real_home / bla._AUTHORITY_FILE).exists()
    assert bla._GENERATION_ENV not in os.environ
    assert bla._LAUNCHER_LEASE_ID_ENV not in os.environ
    assert bla._BACKEND_RESERVATION_ID_ENV not in os.environ
    with pytest.raises(RuntimeError, match="environment is missing"):
        bla.consume_primary_backend_launch_authority(
            launcher_lease_id=lease.lease_id,
            backend_reservation_id=reservation.reservation_id,
            backend_lock_identity=reservation.lock_identity,
            executing_checkout=checkout,
        )


@pytest.mark.parametrize(
    "override",
    [
        {"launcher_lease_id": "f" * 32},
        {"backend_reservation_id": "f" * 32},
        {"backend_lock_identity": "wrong"},
    ],
)
def test_consume_rejects_stale_binding_without_consuming(
    real_home: Path,
    checkout: Path,
    bound_launch,
    override: dict[str, str],
) -> None:
    lease, reservation, _ = bound_launch
    values = {
        "launcher_lease_id": lease.lease_id,
        "backend_reservation_id": reservation.reservation_id,
        "backend_lock_identity": reservation.lock_identity,
        **override,
    }
    with pytest.raises(RuntimeError, match="binding does not match"):
        bla.consume_primary_backend_launch_authority(
            **values,
            executing_checkout=checkout,
        )
    assert (real_home / bla._AUTHORITY_FILE).exists()


def test_consume_rejects_checkout_mismatch(
    real_home: Path,
    bound_launch,
    tmp_path: Path,
) -> None:
    lease, reservation, _ = bound_launch
    with pytest.raises(RuntimeError, match="checkout does not match"):
        bla.consume_primary_backend_launch_authority(
            launcher_lease_id=lease.lease_id,
            backend_reservation_id=reservation.reservation_id,
            backend_lock_identity=reservation.lock_identity,
            executing_checkout=tmp_path,
        )
    assert (real_home / bla._AUTHORITY_FILE).exists()


def test_consume_validates_active_pointer(
    real_home: Path,
    checkout: Path,
    bound_launch,
    tmp_path: Path,
) -> None:
    lease, reservation, _ = bound_launch
    (real_home / "active_checkout.json").write_text(
        json.dumps({"status": "active", "active": str(tmp_path)}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="does not match active checkout"):
        bla.consume_primary_backend_launch_authority(
            launcher_lease_id=lease.lease_id,
            backend_reservation_id=reservation.reservation_id,
            backend_lock_identity=reservation.lock_identity,
            executing_checkout=checkout,
        )


def test_private_json_is_atomic_and_private(tmp_path: Path) -> None:
    target = tmp_path / "record.json"
    bla._write_private_json(target, {"b": 2, "a": 1})
    assert target.read_text(encoding="utf-8") == '{"a":1,"b":2}'
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".*.tmp"))


def test_read_object_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    bla._write_private_json(target, {"value": 1})
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(RuntimeError, match="invalid"):
        bla._read_object(link, "record")


def test_read_object_rejects_oversize(tmp_path: Path) -> None:
    target = tmp_path / "large.json"
    target.write_text(json.dumps({"value": "x" * (65 * 1024)}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid"):
        bla._read_object(target, "record")
