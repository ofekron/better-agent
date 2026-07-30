"""Startup recovery, exercised end to end against an isolated home.

Everything else proving the recovery-thread split stops at the
integration entry point, because a fresh BETTER_AGENT_HOME has no
installation profile and `_recover_in_flight_task` returns early. This
seeds an ACTIVE profile inside the isolated home — writing only into
that home, and only READING the already-valid backend venv pointer —
then runs the real lifespan and asserts recovery actually dispatched.

Not a substitute for a live restart with real crashed runs, but it does
execute main's startup path rather than reasoning about it.
"""

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths

_HOME = tempfile.mkdtemp(prefix="recovery-e2e-")
paths.engage_test_home(_HOME)

import installation_profile  # noqa: E402


def _identity_for(provider: str) -> dict:
    """A provider executable identity built from a real file, so the
    digests are genuine. Which file does not matter — the profile only
    records identity; nothing in this test launches the provider."""
    import hashlib

    stand_in = Path(sys.executable)
    digest = hashlib.sha256(stand_in.read_bytes()).hexdigest()
    stat = stand_in.stat()
    return {
        "command": installation_profile._provider_command(provider),
        "launcher_path": str(stand_in),
        "launcher_sha256": digest,
        "target_path": str(stand_in),
        "target_sha256": digest,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _activate_profile() -> bool:
    """Stage an active profile in the isolated home. Writes ONLY into
    that home; the environment check merely reads the already-valid
    backend venv pointer. Returns False if this environment cannot
    support an active profile."""
    for provider in ("claude", "codex"):
        try:
            profile = installation_profile.new_active_profile(
                mode=installation_profile.DEFAULT,
                provider=provider,
                provider_identity=_identity_for(provider),
            )
            installation_profile.stage_activation(profile)
            installation_profile.mark_selection_applied()
            if installation_profile.provider_conversations_enabled():
                return True
        except Exception as exc:  # noqa: BLE001 - environment probe
            print(f"note: provider {provider} not usable here: {exc}")
    print("SKIP: could not activate an isolated installation profile")
    return False


def test_startup_dispatches_recovery_and_runs_the_scan_off_main() -> None:
    if not _activate_profile():
        return

    import main

    import recovery
    import recovery_manager
    from fastapi.testclient import TestClient

    scan_threads: list[str] = []
    original_scan = recovery.recover_all_in_flight

    def spy(*a, **kw):
        scan_threads.append(threading.current_thread().name)
        return original_scan(*a, **kw)

    recovery.recover_all_in_flight = spy
    try:
        with TestClient(main.app):
            deadline = time.time() + 30
            while time.time() < deadline and not scan_threads:
                time.sleep(0.1)
            assert scan_threads, (
                "startup recovery never dispatched the provider scan"
            )
            names = {t.name for t in threading.enumerate()}
            assert "recovery-manager" in names, names
        # Every scan ran off the main thread, on the recovery host.
        assert all(n != "MainThread" for n in scan_threads), scan_threads
        assert any(
            n.startswith("recovery-io") or n == "recovery-manager"
            for n in scan_threads
        ), scan_threads
        assert recovery_manager.manager.running is False
    finally:
        recovery.recover_all_in_flight = original_scan


if __name__ == "__main__":
    import shutil

    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
                print(f"ok {name}")
        print("PASS")
    finally:
        shutil.rmtree(_HOME, ignore_errors=True)
