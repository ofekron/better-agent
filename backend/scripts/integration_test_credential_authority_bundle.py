from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = ROOT / "desktop" / "build_credential_authority.sh"
AUTHORITY = (
    ROOT
    / "desktop"
    / "dist"
    / "BetterAgentCredentialAuthority"
    / "BetterAgentCredentialAuthority"
)


def main() -> int:
    subprocess.run([str(BUILD_SCRIPT)], cwd=ROOT, check=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="ba-ca-test.", dir="/tmp"))
    temp_dir.chmod(0o700)
    control_path = temp_dir / "control.sock"
    proc = subprocess.Popen(
        [
            str(AUTHORITY),
            "--control",
            str(control_path),
            "--launcher-root",
            str(ROOT),
            "--controller-pid",
            str(os.getpid()),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if control_path.is_socket():
                break
            returncode = proc.poll()
            if returncode is not None:
                output = proc.stdout.read() if proc.stdout is not None else ""
                raise AssertionError(
                    f"credential authority exited before readiness ({returncode}): {output}"
                )
            time.sleep(0.05)
        else:
            output = proc.stdout.read() if proc.stdout is not None else ""
            raise AssertionError(
                f"credential authority did not create its control socket: {output}"
            )

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
        if proc.returncode != 0:
            raise AssertionError(
                f"credential authority stopped with return code {proc.returncode}"
            )
        print("credential authority bundle startup: PASS")
        return 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
