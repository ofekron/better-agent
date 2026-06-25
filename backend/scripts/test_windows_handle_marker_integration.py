from __future__ import annotations

import os
import sys
import tempfile
import subprocess
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if os.name != "nt":
    print("SKIP Windows-only handle-relative marker integration")
    raise SystemExit(0)

from windows_handle_marker import WindowsNativeOps, write_marker

with tempfile.TemporaryDirectory(prefix="ba-win-marker-") as tmp:
    root = Path(tmp) / "runs"; root.mkdir()
    run = root / "run-1"; run.mkdir()
    marker = write_marker(WindowsNativeOps(), root, "run-1", {"version": 1})
    assert marker.size > 0
    assert (run / "reconciled.marker").read_text(encoding="utf-8")
    assert not list(run.glob(".reconciled.marker.*.tmp"))
    marker2 = write_marker(WindowsNativeOps(), root, "run-1", {"version": 2})
    assert marker2.file_id != 0
    assert '"version": 2' in (run / "reconciled.marker").read_text(encoding="utf-8")
    target = root / "target"; target.mkdir()
    junction = root / "junction-run"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)], check=True, capture_output=True)
    try:
        write_marker(WindowsNativeOps(), root, "junction-run", {"version": 1})
    except OSError:
        pass
    else:
        raise AssertionError("junction run must be rejected")
    assert not (target / "reconciled.marker").exists()

    race_run = root / "race-run"; race_run.mkdir()
    moved = root / "race-run-original"
    race_target = root / "race-target"; race_target.mkdir()
    stop = threading.Event()
    race_errors: list[BaseException] = []
    junction_installs = [0]
    def swap_namespace() -> None:
        while not stop.is_set():
            try:
                race_run.rename(moved)
                subprocess.run(["cmd", "/c", "mklink", "/J", str(race_run), str(race_target)], check=True, capture_output=True)
                junction_installs[0] += 1
                race_run.rmdir()
                moved.rename(race_run)
            except (OSError, subprocess.CalledProcessError):
                try:
                    if race_run.is_dir() and not moved.exists():
                        continue
                    if race_run.exists():
                        race_run.rmdir()
                    if moved.exists():
                        moved.rename(race_run)
                except BaseException as exc:
                    race_errors.append(exc)
                    return
            except BaseException as exc:
                race_errors.append(exc)
                return
    racer = threading.Thread(target=swap_namespace); racer.start()
    try:
        for value in range(50):
            try:
                write_marker(WindowsNativeOps(), root, "race-run", {"value": value})
            except OSError:
                pass
    finally:
        stop.set(); racer.join()
    assert not race_errors, race_errors
    assert junction_installs[0] > 0, "namespace race did not install a junction"
    assert not (race_target / "reconciled.marker").exists()

print("PASS Windows handle-relative marker integration")
