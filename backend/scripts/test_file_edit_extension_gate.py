"""File Edit is folded onto a builtin extension identity
(extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID). POST /api/sessions with
file_edit_enabled/file_edit_path must 403 when that extension is not
present/enabled in the session's effective harness (Default synthesis),
per the harness-profile v2 reshape (section 6).

Run with:
    cd backend && .venv/bin/python scripts/test_file_edit_extension_gate.py
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home
TMP_HOME = Path(_test_home.isolate("bc-test-file-edit-gate-"))

dist_dir = BACKEND.parent / "frontend" / "dist"
created_dist = not dist_dir.exists()
if created_dist:
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text("<!doctype html><title>stub</title>", encoding="utf-8")

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import extension_store  # noqa: E402
import file_editor  # noqa: E402
import harness_profile_resolver  # noqa: E402
import harness_profile_store  # noqa: E402
import installation_profile  # noqa: E402
import main  # noqa: E402
import working_mode  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

# Bypass the installation-setup gate (unrelated to this test) so the request
# reaches the file-edit extension gate under test. Mirrors the pattern
# already used by scripts/test_session_control.py.
installation_profile.allows = lambda _capability: True

FILE_EDIT_GATE_DETAIL = "File Edit is disabled for this harness"

_FAKE_BASES: dict[tuple[str, str, str, str], str] = {}


async def _fake_ensure_file_edit_base(cfg) -> str:
    """Stand in for file_editor._ensure_file_edit_base so the positive-control
    test does not spawn a real provider subprocess. Mirrors the fixture used
    by scripts/test_file_edit_session_persistent.py."""
    key = (cfg.cwd, cfg.provider_id, cfg.model, cfg.node_id)
    sid = _FAKE_BASES.get(key)
    if sid and session_manager.get(sid):
        return sid
    base = session_manager.create(
        name="file-editing-base",
        model=cfg.model,
        cwd=cfg.cwd,
        orchestration_mode="native",
        source="internal",
        provider_id=cfg.provider_id,
        reasoning_effort=cfg.reasoning_effort or None,
        node_id=cfg.node_id,
        bare_config=False,
        worker_creation_policy="deny",
    )
    fake_agent_sid = f"fake-base-sid-{len(_FAKE_BASES)}"
    session_manager._run(
        base["id"],
        lambda s: s.__setitem__("agent_session_id", fake_agent_sid),
        {"kind": "test_agent_sid_set"},
    )
    working_mode.mark_working_mode(
        base["id"],
        mode=file_editor.BASE_MODE,
        meta={
            "cwd": cfg.cwd,
            "provider_id": cfg.provider_id,
            "model": cfg.model,
            "machine_completion": False,
            "version": file_editor.FILE_EDIT_BASE_SPEC.version,
            "node_id": cfg.node_id,
            "provisioned_at": time.time(),
        },
    )
    _FAKE_BASES[key] = base["id"]
    return base["id"]


file_editor._ensure_file_edit_base = _fake_ensure_file_edit_base  # type: ignore[assignment]


def _install_and_enable_file_edit_extension() -> None:
    """Installs + enables the builtin File Edit extension identity directly
    via the store internals, mirroring the fixture pattern used by
    scripts/test_builtin_extension_gates.install_gate_extension and
    scripts/test_harness_profile_resolver_default._install_browser_harness_extension_with_headless_setting."""
    package = TMP_HOME / "private-fixtures" / extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID,
        "name": extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID,
        "version": "1.0.0",
        "description": extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID,
        "surfaces": ["backend_feature"],
        "entrypoints": {},
        "permissions": {},
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(package.parent),
            "extension_path": package.name,
            "ref": "",
            "commit_sha": extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID,
        },
        persist=True,
    )
    extension_store.set_enabled(extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID, True)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def test_file_edit_disabled_via_extension_blocks_session_creation(client: TestClient) -> None:
    # A fresh test home never ran _ensure_public_extensions (it early-returns
    # when installation_profile.integrations_enabled() is False), so
    # BUILTIN_FILE_EDIT_EXTENSION_ID is simply not installed here — the
    # Default synthesis skips it, matching "disabled" for the gate's purposes.
    check(
        extension_store.get_extension(extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID) is None,
        "file-edit extension is not installed in a fresh test home",
    )
    response = client.post(
        "/api/sessions",
        json={"cwd": str(TMP_HOME), "file_edit_path": str(TMP_HOME / "a.txt")},
    )
    check(response.status_code == 403, "disabled File Edit extension blocks session creation via file_edit_path")
    check(
        response.json().get("detail") == FILE_EDIT_GATE_DETAIL,
        "403 via file_edit_path is caused by the file-edit gate specifically",
    )

    response = client.post(
        "/api/sessions",
        json={"cwd": str(TMP_HOME), "file_edit_enabled": True},
    )
    check(response.status_code == 403, "disabled File Edit extension blocks session creation via file_edit_enabled")
    check(
        response.json().get("detail") == FILE_EDIT_GATE_DETAIL,
        "403 via file_edit_enabled is caused by the file-edit gate specifically",
    )


def test_file_edit_enabled_via_extension_allows_session_creation(client: TestClient) -> None:
    """Positive control: proves the 403s above are actually caused by the
    file-edit gate (not some unconditional 403), by showing session creation
    with file_edit_path SUCCEEDS once the File Edit extension is installed
    + enabled in the session's effective harness."""
    _install_and_enable_file_edit_extension()
    check(
        extension_store.get_extension(extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID) is not None,
        "file-edit extension is installed after enabling it",
    )

    project_dir = TMP_HOME / "enabled-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    target_file = project_dir / "a.txt"
    target_file.write_text("hello world\n", encoding="utf-8")

    response = client.post(
        "/api/sessions",
        json={"cwd": str(project_dir), "file_edit_path": str(target_file)},
    )
    check(
        response.status_code == 200,
        f"enabled File Edit extension allows session creation via file_edit_path (got {response.status_code}: {response.text})",
    )
    body = response.json()
    session_id = body["id"]
    record = session_manager.get(session_id) or {}
    check(
        record.get("working_mode") == file_editor.MODE,
        "created session is marked as a file-editing session",
    )


def test_noop_override_on_uninstalled_extension_does_not_bypass_gate(client: TestClient) -> None:
    """Hole A: a profile override that merely references
    BUILTIN_FILE_EDIT_EXTENSION_ID (with an effectively no-op delta) must
    NOT cause the extension to appear "present" in the resolved harness
    when it isn't actually installed/enabled in live Default. Otherwise a
    profile author could bypass the File Edit gate without the extension
    ever being runtime-ready."""
    check(
        extension_store.get_extension(extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID) is None,
        "file-edit extension is still not installed for this sub-test",
    )
    harness_profile_store.create_profile({"id": "phantom.override", "name": "Phantom Override"})
    harness_profile_store.apply_override_patch(
        "phantom.override",
        [{
            "path": ["extension_instances", extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID, "skills"],
            "op": "set",
            "value": {"add": [], "remove": []},
        }],
    )
    resolved = harness_profile_resolver.resolve_profile("phantom.override")
    check(
        extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID not in resolved["extension_instances"],
        "a no-op override referencing an uninstalled extension does not conjure it into the resolved view",
    )

    response = client.post(
        "/api/sessions",
        json={
            "cwd": str(TMP_HOME),
            "file_edit_path": str(TMP_HOME / "phantom.txt"),
            "harness_profile_id": "phantom.override",
        },
    )
    check(response.status_code == 403, "phantom-override profile still 403s session creation via file_edit_path")
    check(
        response.json().get("detail") == FILE_EDIT_GATE_DETAIL,
        "403 for phantom-override profile is caused by the file-edit gate specifically",
    )


def test_disabled_builtin_extensions_override_blocks_gate(client: TestClient) -> None:
    """Hole B: enabling File Edit at Default level, then explicitly disabling
    it via a named profile's disabled_builtin_extensions override, must
    still 403 session creation under that profile — presence in
    extension_instances alone is not sufficient."""
    _install_and_enable_file_edit_extension()
    check(
        extension_store.get_extension(extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID) is not None,
        "file-edit extension is genuinely live-enabled at Default level",
    )
    harness_profile_store.create_profile({"id": "disables.file-edit", "name": "Disables File Edit"})
    harness_profile_store.apply_override_patch(
        "disables.file-edit",
        [{
            "path": ["disabled_builtin_extensions"],
            "op": "set",
            "value": {"add": [extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID], "remove": []},
        }],
    )
    resolved = harness_profile_resolver.resolve_profile("disables.file-edit")
    check(
        extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID in resolved["extension_instances"],
        "file-edit extension is still present in extension_instances (it IS live-enabled)",
    )
    check(
        extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID in resolved["disabled_builtin_extensions"]["resolved"],
        "file-edit extension shows up in the resolved disabled_builtin_extensions list",
    )

    project_dir = TMP_HOME / "disabled-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    target_file = project_dir / "b.txt"
    target_file.write_text("hello\n", encoding="utf-8")
    response = client.post(
        "/api/sessions",
        json={
            "cwd": str(project_dir),
            "file_edit_path": str(target_file),
            "harness_profile_id": "disables.file-edit",
        },
    )
    check(
        response.status_code == 403,
        f"explicitly-disabled File Edit still 403s session creation (got {response.status_code}: {response.text})",
    )
    check(
        response.json().get("detail") == FILE_EDIT_GATE_DETAIL,
        "403 for disabled-override profile is caused by the file-edit gate specifically",
    )


def main_run() -> int:
    try:
        with TestClient(main.app) as client:
            client.headers.update({
                "Authorization": f"Bearer {auth.create_token('test')}",
            })
            test_file_edit_disabled_via_extension_blocks_session_creation(client)
            test_noop_override_on_uninstalled_extension_does_not_bypass_gate(client)
            test_file_edit_enabled_via_extension_allows_session_creation(client)
            test_disabled_builtin_extensions_override_blocks_gate(client)
    finally:
        if created_dist:
            shutil.rmtree(dist_dir, ignore_errors=True)
        shutil.rmtree(TMP_HOME, ignore_errors=True)
    print("PASS file edit extension gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_run())
