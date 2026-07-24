from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CAP_SYNC = ROOT / "frontend" / "scripts" / "cap-sync.mjs"
REBUILD_ANDROID_APK = ROOT / "scripts" / "rebuild-android-apk.mjs"

BASE_PACKAGE_JSON = {
    "name": "frontend",
    "dependencies": {"react": "^19.2.0"},
}
MOBILE_DEPENDENCIES = {"@capacitor/core": "^8.3.4", "@capacitor/app": "^8.1.0"}


def _write_fixture(frontend_dir: Path) -> None:
    (frontend_dir / "package.json").write_text(
        f"{json.dumps(BASE_PACKAGE_JSON, indent=2)}\n"
    )
    (frontend_dir / "mobile-dependencies.json").write_text(
        f"{json.dumps(MOBILE_DEPENDENCIES, indent=2)}\n"
    )


def _run_with_mobile_package_json(frontend_dir: Path, during_script: str) -> str:
    script = (
        f"import {{ withMobilePackageJson }} from {json.dumps(CAP_SYNC.as_uri())};"
        "import { readFileSync } from 'node:fs';"
        f"withMobilePackageJson({json.dumps(str(frontend_dir))}, () => {{{during_script}}});"
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=frontend_dir,
        capture_output=True,
        text=True,
    )
    return completed


def test_mobile_deps_merged_in_during_the_call() -> None:
    # This is the failure this fixes: `cap sync` reads package.json to
    # discover native plugins, and frontend/package.json intentionally
    # excludes them (installation dependency isolation) -- so a bare
    # `cap sync` sees none and strips every plugin include. The manifest
    # must contain the mobile deps for the duration of the wrapped call.
    with tempfile.TemporaryDirectory() as tmp:
        frontend_dir = Path(tmp)
        _write_fixture(frontend_dir)
        completed = _run_with_mobile_package_json(
            frontend_dir,
            "console.log(readFileSync('package.json', 'utf8'));",
        )
        assert completed.returncode == 0, completed.stderr
        during = json.loads(completed.stdout)
        assert during["dependencies"]["@capacitor/core"] == "^8.3.4"
        assert during["dependencies"]["@capacitor/app"] == "^8.1.0"
        assert during["dependencies"]["react"] == "^19.2.0"


def test_package_json_restored_after_success() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        frontend_dir = Path(tmp)
        _write_fixture(frontend_dir)
        original = (frontend_dir / "package.json").read_text()
        completed = _run_with_mobile_package_json(frontend_dir, "")
        assert completed.returncode == 0, completed.stderr
        assert (frontend_dir / "package.json").read_text() == original


def test_package_json_restored_after_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        frontend_dir = Path(tmp)
        _write_fixture(frontend_dir)
        original = (frontend_dir / "package.json").read_text()
        completed = _run_with_mobile_package_json(
            frontend_dir, "throw new Error('sync failed')"
        )
        assert completed.returncode != 0
        assert (frontend_dir / "package.json").read_text() == original


def test_rebuild_android_apk_wraps_cap_sync_in_mobile_package_json() -> None:
    # The APK actually shipped to mobile devices is built by
    # rebuild-android-apk.mjs (via the pre-commit hook), not by running
    # cap-sync.mjs directly. That script previously called `npx cap sync
    # android` bare, silently stripping every native plugin include (e.g.
    # @capacitor/app, which backs the deep-link server-URL handoff on
    # first launch) from the generated Android project, breaking mobile
    # login. Lock that its cap sync call is nested inside a
    # withMobilePackageJson(...) block, not called bare.
    source = REBUILD_ANDROID_APK.read_text()
    assert "cap-sync.mjs" in source, (
        "rebuild-android-apk.mjs must import the withMobilePackageJson wrapper"
    )

    # Every `cap sync android` invocation in the file must fall lexically
    # inside a withMobilePackageJson(FRONTEND, () => { ... }); block -- a
    # bare call anywhere in the file reintroduces the plugin-stripping bug.
    wrapped_blocks = [
        m.span()
        for m in re.finditer(
            r"withMobilePackageJson\(FRONTEND,\s*\(\)\s*=>\s*\{.*?\}\s*\);",
            source,
            re.DOTALL,
        )
    ]
    assert wrapped_blocks, "no withMobilePackageJson(FRONTEND, ...) block found"

    cap_sync_calls = [m.start() for m in re.finditer(r"cap sync android", source)]
    assert cap_sync_calls, "expected a `cap sync android` invocation in the file"
    for call_pos in cap_sync_calls:
        assert any(start <= call_pos <= end for start, end in wrapped_blocks), (
            "found a `cap sync android` invocation outside any "
            "withMobilePackageJson(FRONTEND, ...) block"
        )


if __name__ == "__main__":
    test_mobile_deps_merged_in_during_the_call()
    test_package_json_restored_after_success()
    test_package_json_restored_after_failure()
    test_rebuild_android_apk_wraps_cap_sync_in_mobile_package_json()
    print("cap-sync tests passed")
