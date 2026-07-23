"""Backend spawn surfaces must make the checkout's sdk/ importable.

The backend imports `better_agent_sdk` (capability_api -> runtime_operations),
so every launcher that spawns it from a source checkout must put
<checkout>/sdk on PYTHONPATH. Locks the dev browser-backend supervisor and
the packaged supervisor env paths.

Run with:
    backend/.venv/bin/python desktop/test_backend_sdk_pythonpath.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="bc-test-sdk-pythonpath-")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ["BETTER_CLAUDE_HOME"] = _TMP_HOME

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
_BACKEND = _REPO / "backend"
for _p in (_HERE, _BACKEND):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from sdk_pythonpath import apply_sdk_pythonpath, sdk_pythonpath

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _checkout_with_sdk(name: str) -> Path:
    root = Path(_TMP_HOME) / name
    (root / "sdk" / "better_agent_sdk").mkdir(parents=True)
    return root


def test_sdk_prepended() -> bool:
    root = _checkout_with_sdk("prepend")
    value = sdk_pythonpath(root, "")
    if value != str(root / "sdk"):
        print(f"  expected bare sdk path, got: {value}")
        return False
    value = sdk_pythonpath(root, "/other")
    if value != str(root / "sdk") + os.pathsep + "/other":
        print(f"  expected sdk prepended, got: {value}")
        return False
    return True


def test_missing_sdk_and_duplicate_are_noops() -> bool:
    root = Path(_TMP_HOME) / "no-sdk"
    root.mkdir()
    if sdk_pythonpath(root, "/other") != "/other":
        print("  missing sdk dir must leave PYTHONPATH unchanged")
        return False
    root = _checkout_with_sdk("dup")
    existing = str(root / "sdk") + os.pathsep + "/other"
    if sdk_pythonpath(root, existing) != existing:
        print("  sdk already present must not be duplicated")
        return False
    env: dict[str, str] = {}
    if apply_sdk_pythonpath(env, Path(_TMP_HOME) / "no-sdk").get("PYTHONPATH"):
        print("  apply with missing sdk must not set PYTHONPATH")
        return False
    return True


def test_browser_backend_supervisor_env_includes_sdk() -> bool:
    from browser_backend_supervisor import backend_launch_env

    root = _checkout_with_sdk("browser-sup")
    env = backend_launch_env({"HOME": "/tmp"}, root, 18765)
    pythonpath = env.get("PYTHONPATH", "")
    if str(root / "sdk") not in pythonpath.split(os.pathsep):
        print(f"  browser supervisor PYTHONPATH missing sdk: {pythonpath!r}")
        return False
    if env.get("BETTER_AGENT_BACKEND_PORT") != "18765":
        print("  browser supervisor env lost its port keys")
        return False
    return True


def test_packaged_supervisor_env_includes_sdk() -> bool:
    from supervisor import backend_child_env

    root = _checkout_with_sdk("packaged-sup")
    env = backend_child_env({"HOME": "/tmp", "PYTHONPATH": "/other"}, root)
    pythonpath = env.get("PYTHONPATH", "")
    entries = pythonpath.split(os.pathsep)
    if str(root / "sdk") not in entries or "/other" not in entries:
        print(f"  packaged supervisor PYTHONPATH wrong: {pythonpath!r}")
        return False
    return True


def main() -> int:
    tests = [
        test_sdk_prepended,
        test_missing_sdk_and_duplicate_are_noops,
        test_browser_backend_supervisor_env_includes_sdk,
        test_packaged_supervisor_env_includes_sdk,
    ]
    failed = 0
    for test in tests:
        try:
            ok = test()
        except Exception as exc:  # noqa: BLE001
            print(f"  {type(exc).__name__}: {exc}")
            ok = False
        print(f"{PASS if ok else FAIL} {test.__name__}")
        failed += 0 if ok else 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
