from __future__ import annotations

import select
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LOCK_RUNNER = ROOT / "desktop" / "credential_build_lock.py"
HOLDER = (
    "import sys; "
    "print('locked', flush=True); "
    "sys.stdin.readline()"
)
REPORTER = "print('acquired', flush=True)"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ba-credential-build-lock-") as temp:
        lock_path = Path(temp) / "authority.lock"
        first = subprocess.Popen(
            [sys.executable, str(LOCK_RUNNER), str(lock_path), sys.executable, "-c", HOLDER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        second: subprocess.Popen[str] | None = None
        try:
            assert first.stdout is not None
            assert first.stdout.readline() == "locked\n"
            second = subprocess.Popen(
                [
                    sys.executable,
                    str(LOCK_RUNNER),
                    str(lock_path),
                    sys.executable,
                    "-c",
                    REPORTER,
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
            assert second.stdout is not None
            readable, _, _ = select.select([second.stdout], [], [], 0.2)
            assert readable == []
            assert first.stdin is not None
            first.stdin.write("release\n")
            first.stdin.flush()
            assert first.wait(timeout=5) == 0
            assert second.stdout.readline() == "acquired\n"
            assert second.wait(timeout=5) == 0
        finally:
            if first.poll() is None:
                first.kill()
                first.wait(timeout=5)
            if second is not None and second.poll() is None:
                second.kill()
                second.wait(timeout=5)
    print("OK: credential authority build lock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
