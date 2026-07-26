"""A partial extension sync must report itself as partial.

`export_extension_sync_state` skips a package it cannot build so one bad
package cannot deny every node all the others; the skip has to travel with the
payload, or the node believes it received every extension.
"""
from __future__ import annotations

import atexit
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if str(_BACKEND / "scripts") not in sys.path:
    sys.path.insert(0, str(_BACKEND / "scripts"))

import _test_home  # noqa: E402

_TEST_HOME = _test_home.TestHome.acquire("bc-test-extension-sync-failures-")
atexit.register(_TEST_HOME.release)

import extension_store  # noqa: E402


def _write_package(root: Path, name: str) -> Path:
    package = root / name
    (package / "src").mkdir(parents=True)
    (package / "better-agent-extension.json").write_text("{}", encoding="utf-8")
    (package / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    return package


def _seed_store(install_paths: dict[str, str]) -> None:
    """Install the records through the store's own writer, so the export reads
    the same state a real install would leave behind."""
    store = {
        "schema_version": extension_store.STORE_SCHEMA_VERSION,
        "deleted_extensions": {},
        "extensions": {
            extension_id: {
                "manifest": {"id": extension_id},
                "enabled": True,
                "activation_id": hashlib.sha256(extension_id.encode("utf-8")).hexdigest()[:32],
                "installed_at": "2026-07-26T00:00:00+00:00",
                "updated_at": "2026-07-26T00:00:00+00:00",
                "source": {
                    "type": "git",
                    "install_path": install_path,
                    "commit_sha": "",
                },
            }
            for extension_id, install_path in install_paths.items()
        },
    }
    with extension_store._store_lock():
        extension_store._write_store_unlocked(store)


def test_export_reports_the_package_it_could_not_ship() -> None:
    work = Path(tempfile.mkdtemp(prefix="bc-test-sync-failure-pkgs-"))
    try:
        good = _write_package(work, "good")
        bad = _write_package(work, "bad")
        (bad / "src" / "escape.py").symlink_to("/etc/passwd")
        _seed_store({
            "ok.ext": str(good),
            "bad.ext": str(bad),
            "gone.ext": str(work / "missing"),
            # A placeholder record was never installed: nothing to ship and
            # nothing to report.
            "placeholder.ext": "",
        })

        payload = extension_store.export_extension_sync_state()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    shipped = {artifact["extension_id"] for artifact in payload["artifacts"]}
    if shipped != {"ok.ext"}:
        raise AssertionError(f"shippable package missing from export: {shipped}")
    failures = {item["extension_id"]: item["error"] for item in payload["artifact_failures"]}
    if set(failures) != {"bad.ext", "gone.ext"}:
        raise AssertionError(f"export did not report its skipped packages: {failures}")
    if "must not contain links" not in failures["bad.ext"]:
        raise AssertionError(failures["bad.ext"])
    if "install_path" not in failures["gone.ext"]:
        raise AssertionError(failures["gone.ext"])


def test_import_result_carries_the_missing_packages() -> None:
    payload = {
        "schema_version": extension_store.STORE_SCHEMA_VERSION,
        "store": {
            "schema_version": extension_store.STORE_SCHEMA_VERSION,
            "extensions": {
                "ok.ext": {"manifest": {"id": "ok.ext"}, "enabled": False, "source": {}},
                "bad.ext": {"manifest": {"id": "bad.ext"}, "enabled": False, "source": {}},
            },
            "deleted_extensions": {},
        },
        "artifacts": [],
        "artifact_failures": [
            {"extension_id": "bad.ext", "error": "extension package must not contain links"},
            "not-an-object",
        ],
    }
    result = extension_store.import_extension_sync_state(payload)
    if result["artifact_failures"] != [
        {"extension_id": "bad.ext", "error": "extension package must not contain links"}
    ]:
        raise AssertionError(f"import hid the missing package: {result}")


def main() -> None:
    tests = [
        test_export_reports_the_package_it_could_not_ship,
        test_import_result_carries_the_missing_packages,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - standalone runner reports every failure
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
        else:
            print(f"ok   {test.__name__}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
