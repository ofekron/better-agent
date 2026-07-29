"""A partial extension sync must report itself as partial.

`export_extension_sync_state` skips a package it cannot build so one bad
package cannot deny every node all the others; the skip has to travel with the
payload, or the node believes it received every extension.
"""
from __future__ import annotations

import atexit
import asyncio
import base64
import hashlib
import json
import shutil
import sys
import tempfile
import threading
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
import node_rpc_handlers  # noqa: E402


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


def _sync_artifact(
    root: Path,
    extension_id: str,
    *,
    permissions: dict | None = None,
    core_roles: list[str] | None = None,
) -> dict[str, str]:
    package = root / extension_id
    package.mkdir()
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": extension_id,
        "surfaces": [],
        "entrypoints": {},
        "permissions": permissions or {},
        "marketplace": {},
    }
    if core_roles is not None:
        manifest["core_roles"] = core_roles
    (package / "better-agent-extension.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    archive = extension_store._build_package_artifact(package)
    digest = hashlib.sha256(archive).hexdigest()
    return {
        "extension_id": extension_id,
        "archive_b64": base64.b64encode(archive).decode("ascii"),
        "artifact_sha256": digest,
        "commit_sha": digest,
    }


def _sync_payload(extension_ids: list[str], artifacts: list[object]) -> dict:
    return {
        "schema_version": extension_store.STORE_SCHEMA_VERSION,
        "store": {
            "schema_version": extension_store.STORE_SCHEMA_VERSION,
            "extensions": {
                extension_id: {
                    "manifest": {"id": extension_id},
                    "enabled": True,
                    "source": {},
                }
                for extension_id in extension_ids
            },
            "deleted_extensions": {},
        },
        "artifacts": artifacts,
    }


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
    if "ok.ext" not in shipped or {"bad.ext", "gone.ext"} & shipped:
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


def test_import_isolates_bad_artifact_and_reports_partial_state() -> None:
    work = Path(tempfile.mkdtemp(prefix="bc-test-sync-import-pkgs-"))
    try:
        first = _sync_artifact(work, "first.ext")
        last = _sync_artifact(work, "last.ext")
        bad = {
            "extension_id": "bad.ext",
            "archive_b64": "not-base64",
            "artifact_sha256": "",
        }
        result = extension_store.import_extension_sync_state(
            _sync_payload(
                ["first.ext", "bad.ext", "last.ext"],
                [first, bad, "not-an-object", last],
            )
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if result["ok"]:
        raise AssertionError(f"partial import reported complete success: {result}")
    if result["artifact_count"] != 2:
        raise AssertionError(f"artifact count was optimistic: {result}")
    if result["imported_artifacts"] != ["first.ext", "last.ext"]:
        raise AssertionError(f"valid packages were not independently imported: {result}")
    failures = result["artifact_failures"]
    if [failure["extension_id"] for failure in failures] != ["bad.ext", "artifact[2]"]:
        raise AssertionError(f"per-item failures were not truthful: {failures}")
    records = extension_store._load()["extensions"]
    for extension_id in ("first.ext", "last.ext"):
        install_path = records[extension_id]["source"].get("install_path")
        if not install_path or not Path(install_path).is_dir():
            raise AssertionError(f"{extension_id} was not installed")
    if records["bad.ext"]["source"].get("install_path"):
        raise AssertionError("failed package was marked installed")


def test_import_refuses_legacy_authority_over_scoped_generation() -> None:
    work = Path(tempfile.mkdtemp(prefix="bc-test-sync-authority-"))
    try:
        current_package = _write_package(work, "current")
        extension_id = "scoped.ext"
        _seed_store({extension_id: str(current_package)})
        with extension_store._store_lock():
            current = extension_store._read_store_unlocked()
            current_record = current["extensions"][extension_id]
            current_record["manifest"] = {
                "id": extension_id,
                "permissions": {"capabilities": ["ask.ensure"]},
                "core_roles": ["requirements"],
            }
            extension_store._write_store_unlocked(current)

        legacy = _sync_artifact(
            work,
            extension_id,
            permissions={"internal_loopback": True},
            core_roles=[],
        )
        rejected_target = (
            extension_store._install_root()
            / extension_id
            / "versions"
            / legacy["commit_sha"]
        )
        result = extension_store.import_extension_sync_state(
            _sync_payload([extension_id], [legacy])
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if result["ok"]:
        raise AssertionError(f"legacy authority replacement reported success: {result}")
    failures = result["artifact_failures"]
    if len(failures) != 1 or failures[0]["extension_id"] != extension_id:
        raise AssertionError(f"legacy authority failure was not isolated: {failures}")
    error = failures[0]["error"]
    if error != "sync artifact cannot replace scoped authority with internal_loopback":
        raise AssertionError(f"authority failure was not stable and sanitized: {error}")
    record = extension_store._load()["extensions"][extension_id]
    if record["manifest"]["permissions"] != {"capabilities": ["ask.ensure"]}:
        raise AssertionError("failed sync replaced the last-known-good capability grants")
    if record["manifest"]["core_roles"] != ["requirements"]:
        raise AssertionError("failed sync replaced the last-known-good core roles")
    if record["source"]["install_path"] != str(current_package):
        raise AssertionError("failed sync discarded the last-known-good package")
    if rejected_target.exists():
        raise AssertionError("failed sync mutated the authoritative package tree")


def test_rpc_import_is_bounded_without_blocking_event_loop() -> None:
    entered = threading.Event()
    loop_progressed = threading.Event()
    release_first = threading.Event()
    state_lock = threading.Lock()
    calls = 0
    active = 0
    max_active = 0
    original = extension_store.import_extension_sync_state

    def blocking_import(payload: dict) -> dict:
        nonlocal active, calls, max_active
        with state_lock:
            calls += 1
            call_number = calls
            active += 1
            max_active = max(max_active, active)
        try:
            entered.set()
            if not loop_progressed.wait(timeout=1):
                raise AssertionError("extension import blocked the event loop")
            if call_number == 1 and not release_first.wait(timeout=1):
                raise AssertionError("first extension import was not released")
        finally:
            with state_lock:
                active -= 1
        return {"ok": True}

    async def run() -> None:
        extension_store.import_extension_sync_state = blocking_import
        try:
            first = asyncio.create_task(
                node_rpc_handlers.dispatch_rpc(
                    "sync_extension_config",
                    {"extension_state": {}},
                )
            )
            if not await asyncio.to_thread(entered.wait, 1):
                raise AssertionError("extension import did not start")
            queued = [
                asyncio.create_task(
                    node_rpc_handlers.dispatch_rpc(
                        "sync_extension_config",
                        {"extension_state": {}},
                    )
                )
                for _ in range(7)
            ]
            loop_progressed.set()
            await asyncio.sleep(0.05)
            with state_lock:
                observed_calls = calls
            if observed_calls != 1:
                raise AssertionError("extension imports were not bounded")
            release_first.set()
            results = await asyncio.gather(first, *queued)
            if results != [{"ok": True}] * 8:
                raise AssertionError("unexpected RPC result")
            if max_active != 1:
                raise AssertionError(f"extension imports overlapped: {max_active}")
        finally:
            release_first.set()
            extension_store.import_extension_sync_state = original

    asyncio.run(run())


def main() -> None:
    tests = [
        test_export_reports_the_package_it_could_not_ship,
        test_import_result_carries_the_missing_packages,
        test_import_isolates_bad_artifact_and_reports_partial_state,
        test_import_refuses_legacy_authority_over_scoped_generation,
        test_rpc_import_is_bounded_without_blocking_event_loop,
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
