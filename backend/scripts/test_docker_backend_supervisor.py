from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_docker_pid_one_supervisor_owns_admission() -> None:
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    supervisor = (ROOT / "backend" / "docker_backend_supervisor.py").read_text(
        encoding="utf-8"
    )
    assert 'exec "$PY" -m docker_backend_supervisor' in entrypoint
    assert "reserve_primary_backend_instance_lock(lease)" in supervisor
    assert "reservation.await_adoption(process)" in supervisor
    assert "reservation.detach_after_transfer()" in supervisor
    assert "lease.release()" in supervisor
    assert "scripts/install.py" not in entrypoint
    assert supervisor.index("PrimaryLauncherLease.acquire") < supervisor.index(
        "_activate_installation_profile(checkout)"
    )


def test_docker_primary_bootstrap_isolated_before_site_imports() -> None:
    supervisor = (ROOT / "backend" / "docker_backend_supervisor.py").read_text(
        encoding="utf-8"
    )
    assert 'sys.executable, "-S"' in supervisor
    assert "backend_bootstrap.py" in supervisor
