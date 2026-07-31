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


def _run_projection_assertion(
    frontend_dir: Path, platforms: list[str]
) -> subprocess.CompletedProcess[str]:
    script = (
        f"import {{ assertNativeRunnerProjection }} from {json.dumps(CAP_SYNC.as_uri())};"
        f"assertNativeRunnerProjection({json.dumps(str(frontend_dir))}, "
        f"{json.dumps(platforms)});"
    )
    return subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=frontend_dir,
        capture_output=True,
        text=True,
    )


def _write_runner_projection_fixture(frontend_dir: Path, contents: str) -> None:
    paths = [
        frontend_dir / "dist/runners/offline-sync.js",
        frontend_dir / "android/app/src/main/assets/public/runners/offline-sync.js",
        frontend_dir / "ios/App/App/public/runners/offline-sync.js",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)


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


def test_native_runner_projections_match_canonical_bundle() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        frontend_dir = Path(tmp)
        _write_runner_projection_fixture(frontend_dir, "canonical runner")
        completed = _run_projection_assertion(frontend_dir, ["android", "ios"])
        assert completed.returncode == 0, completed.stderr


def test_stale_native_runner_projection_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        frontend_dir = Path(tmp)
        _write_runner_projection_fixture(frontend_dir, "canonical runner")
        (
            frontend_dir
            / "android/app/src/main/assets/public/runners/offline-sync.js"
        ).write_text("stale runner")
        completed = _run_projection_assertion(frontend_dir, ["android"])
        assert completed.returncode != 0
        assert "does not match the canonical mobile bundle" in completed.stderr


def test_missing_native_runner_projection_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        frontend_dir = Path(tmp)
        _write_runner_projection_fixture(frontend_dir, "canonical runner")
        (
            frontend_dir / "ios/App/App/public/runners/offline-sync.js"
        ).unlink()
        completed = _run_projection_assertion(frontend_dir, ["ios"])
        assert completed.returncode != 0
        assert "ENOENT" in completed.stderr


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


def test_rebuild_android_apk_builds_vite_in_mobile_mode() -> None:
    # vite.config.ts aliases every @capacitor/* import to a no-op web shim
    # (isNativePlatform() => false, empty plugin implementations) unless
    # built with `--mode mobile`. rebuild-android-apk.mjs previously ran a
    # bare `npx vite build`, so the shipped Android bundle always got the
    # web shims -- the app could never detect it was native or receive
    # real plugin events (e.g. the deep-link server-URL handoff), no
    # matter what was registered in the native Android project.
    source = REBUILD_ANDROID_APK.read_text()
    vite_build_calls = [
        m.group(0) for m in re.finditer(r'execSync\(\s*"npx vite build[^"]*"', source)
    ]
    assert vite_build_calls, "expected an `npx vite build` invocation in the file"
    for call in vite_build_calls:
        assert "--mode mobile" in call, (
            f"vite build call must pass --mode mobile, got: {call!r}"
        )


def test_rebuild_android_apk_verifies_runner_after_sync_before_packaging() -> None:
    source = REBUILD_ANDROID_APK.read_text()
    sync_position = source.find('execSync("npx cap sync android"')
    verify_position = source.find('assertNativeRunnerProjection(FRONTEND, ["android"])')
    package_position = source.find("./gradlew assembleDebug")
    assert sync_position >= 0
    assert verify_position >= 0
    assert package_position >= 0
    assert sync_position < verify_position < package_position


def test_rebuild_android_apk_installs_mobile_dependencies_before_vite() -> None:
    source = REBUILD_ANDROID_APK.read_text()
    install_call = re.search(
        r'installFrontendDependencies\(\s*"mobile"\s*\)',
        source,
    )
    vite_call = re.search(r'execSync\(\s*"npx vite build --mode mobile"', source)
    assert install_call, "APK rebuild must activate the canonical mobile dependency profile"
    assert vite_call, "expected the APK rebuild's mobile Vite build"
    assert install_call.start() < vite_call.start(), (
        "mobile dependencies must be installed before Vite resolves native imports"
    )
    assert r"^frontend\/package-lock\.mobile\.json$" in source
    assert r"^frontend\/mobile-dependencies\.json$" in source


def test_mobile_lock_matches_merged_manifest() -> None:
    frontend = ROOT / "frontend"
    manifest = json.loads((frontend / "package.json").read_text())
    mobile_dependencies = json.loads(
        (frontend / "mobile-dependencies.json").read_text()
    )
    lock_root = json.loads(
        (frontend / "package-lock.mobile.json").read_text()
    )["packages"][""]
    assert lock_root["dependencies"] == {
        **manifest["dependencies"],
        **mobile_dependencies,
    }
    assert lock_root["devDependencies"] == manifest["devDependencies"]


def test_rebuild_android_apk_creates_release_destination_before_copy() -> None:
    source = REBUILD_ANDROID_APK.read_text()
    mkdir_position = source.find("mkdirSync(dirname(RELEASES_APK)")
    copy_position = source.find("copyFileSync(APK_OUT, RELEASES_APK)")
    assert mkdir_position >= 0, "APK rebuild must create the gitignored releases directory"
    assert copy_position >= 0, "expected APK copy into the releases directory"
    assert mkdir_position < copy_position


if __name__ == "__main__":
    test_mobile_deps_merged_in_during_the_call()
    test_package_json_restored_after_success()
    test_package_json_restored_after_failure()
    test_native_runner_projections_match_canonical_bundle()
    test_stale_native_runner_projection_fails_closed()
    test_missing_native_runner_projection_fails_closed()
    test_rebuild_android_apk_wraps_cap_sync_in_mobile_package_json()
    test_rebuild_android_apk_builds_vite_in_mobile_mode()
    test_rebuild_android_apk_verifies_runner_after_sync_before_packaging()
    test_rebuild_android_apk_installs_mobile_dependencies_before_vite()
    test_mobile_lock_matches_merged_manifest()
    test_rebuild_android_apk_creates_release_destination_before_copy()
    print("cap-sync tests passed")
