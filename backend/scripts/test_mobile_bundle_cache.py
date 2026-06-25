from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-mobile-bundle-")

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import mobile_bundle  # noqa: E402


def _dist() -> Path:
    dist = Path(tempfile.mkdtemp(prefix="bc-test-mobile-dist-"))
    (dist / "assets").mkdir()
    (dist / "index.html").write_text(
        '<script type="module" src="/assets/index-abc123.js"></script>',
        encoding="utf-8",
    )
    (dist / "assets" / "index-abc123.js").write_text("console.log('abc')\n", encoding="utf-8")
    (dist / "assets" / "style.css").write_text("body{color:#111}\n", encoding="utf-8")
    return dist


def test_build_bundle_reuses_persisted_zip_after_restart() -> None:
    dist = _dist()
    try:
        first = mobile_bundle.build_bundle(dist)
        if not first:
            raise AssertionError("first bundle build returned nothing")
        script = (
            "import os, sys, pathlib\n"
            f"sys.path.insert(0, {str(_BACKEND)!r})\n"
            "import mobile_bundle\n"
            f"dist = pathlib.Path({str(dist)!r})\n"
            "original = pathlib.Path.rglob\n"
            "def fail_rglob(self, pattern):\n"
            "    raise AssertionError('persisted bundle hit must not rebuild dist')\n"
            "pathlib.Path.rglob = fail_rglob\n"
            "try:\n"
            "    info = mobile_bundle.build_bundle(dist)\n"
            "finally:\n"
            "    pathlib.Path.rglob = original\n"
            "print(info['version'], info['path'], info['checksum'])\n"
        )
        env = {**os.environ, "BETTER_AGENT_HOME": _TMP_HOME}
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
        version, path, checksum = result.stdout.strip().split()
        if (
            version != first["version"]
            or Path(path).resolve() != Path(first["path"]).resolve()
            or checksum != first["checksum"]
        ):
            raise AssertionError(result.stdout)
    finally:
        shutil.rmtree(dist, ignore_errors=True)


def test_build_bundle_rebuilds_corrupt_persisted_zip() -> None:
    dist = _dist()
    try:
        first = mobile_bundle.build_bundle(dist)
        if not first:
            raise AssertionError("first bundle build returned nothing")
        zip_path = Path(first["path"])
        zip_path.write_bytes(b"not a zip")
        mobile_bundle._cache.clear()
        rebuilt = mobile_bundle.build_bundle(dist)
        if not rebuilt:
            raise AssertionError("rebuilt bundle returned nothing")
        if zip_path.read_bytes() == b"not a zip":
            raise AssertionError("corrupt persisted zip was reused")
        with zipfile.ZipFile(rebuilt["path"], "r") as zf:
            names = set(zf.namelist())
        if "index.html" not in names or "assets/index-abc123.js" not in names:
            raise AssertionError(names)
        metadata = json.loads(Path(rebuilt["path"]).with_suffix(".json").read_text(encoding="utf-8"))
        if metadata.get("checksum") != rebuilt["checksum"]:
            raise AssertionError("metadata checksum was not refreshed")
    finally:
        shutil.rmtree(dist, ignore_errors=True)


if __name__ == "__main__":
    try:
        test_build_bundle_reuses_persisted_zip_after_restart()
        test_build_bundle_rebuilds_corrupt_persisted_zip()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print("PASS mobile bundle cache")
