from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_NAME = "better-agent" + "-private"


def test_public_core_imports_without_private_sibling() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-public-boundary-") as home, tempfile.TemporaryDirectory(
        prefix="ba-public-source-"
    ) as source:
        isolated_root = Path(source)
        shutil.copytree(ROOT / "backend", isolated_root / "backend")
        shutil.copytree(ROOT / "sdk", isolated_root / "sdk")
        env = {
            **os.environ,
            "BETTER_AGENT_HOME": home,
            "BETTER_AGENT_DISABLE_LOCAL_MARKETPLACE_PACKAGE": "1",
            "PYTHONPATH": os.pathsep.join((str(isolated_root / "backend"), str(isolated_root / "sdk"))),
        }
        script = "import extension_store, requirement_context; assert not hasattr(extension_store, '_PRIVATE_REGISTRY')"
        subprocess.run([sys.executable, "-c", script], cwd=isolated_root, env=env, check=True)


def test_tracked_production_and_tests_do_not_name_private_sibling() -> None:
    tracked = subprocess.check_output(
        ["git", "ls-files", "backend", "frontend/src", "frontend/tests", "extensions", "sdk"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    offenders: list[str] = []
    for relative in tracked:
        path = ROOT / relative
        if not path.is_file() or path == Path(__file__):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if FORBIDDEN_NAME in content:
            offenders.append(relative)
    assert offenders == []


def test_capability_only_extensions_do_not_request_raw_loopback() -> None:
    import json

    capability_only = ("ask", "marketplace", "provider-config-sync", "session-control", "session-bridge", "switch-control")
    for name in capability_only:
        manifest = json.loads((ROOT / "extensions" / name / "better-agent-extension.json").read_text(encoding="utf-8"))
        permissions = manifest["permissions"]
        assert permissions.get("capabilities")
        assert "internal_loopback" not in permissions


def test_core_role_resolution_and_missing_role_fail_closed(monkeypatch) -> None:
    import extension_store

    records = {
        "fixture.requirements": {
            "enabled": True,
            "manifest": {"core_roles": ["requirements"]},
            "entitlement": {"status": "not_required"},
        }
    }
    monkeypatch.setattr(extension_store, "_load", lambda: {"extensions": records})
    assert extension_store.extension_id_for_role("requirements") == "fixture.requirements"
    assert extension_store.extension_id_for_role("assistant") is None


def test_duplicate_active_core_role_fails_closed(monkeypatch) -> None:
    import extension_store

    record = {
        "enabled": True,
        "manifest": {"core_roles": ["requirements"]},
        "entitlement": {"status": "not_required"},
    }
    monkeypatch.setattr(
        extension_store,
        "_load",
        lambda: {"extensions": {"fixture.one": record, "fixture.two": record}},
    )
    try:
        extension_store.extension_id_for_role("requirements")
    except extension_store.ExtensionError as exc:
        assert "multiple active extensions" in str(exc)
    else:
        raise AssertionError("duplicate active core role was accepted")


def test_quick_button_supersession_tracks_assistant_lifecycle(monkeypatch) -> None:
    import extension_store

    records: dict[str, dict] = {}
    monkeypatch.setattr(extension_store, "_load", lambda: {"extensions": records})
    monkeypatch.setattr(extension_store, "get_extension", lambda extension_id: records.get(extension_id))

    ask_id = extension_store.BUILTIN_ASK_EXTENSION_ID
    assert extension_store._quick_button_superseded(ask_id) is False

    assistant_id = "fixture.assistant"
    records[assistant_id] = {
        "enabled": True,
        "manifest": {"core_roles": ["assistant"]},
        "entitlement": {"status": "not_required"},
    }
    assert extension_store._quick_button_superseded(ask_id) is True

    records[assistant_id]["enabled"] = False
    assert extension_store._quick_button_superseded(ask_id) is False

    records.pop(assistant_id)
    assert extension_store._quick_button_superseded(ask_id) is False


def test_manifest_rejects_unknown_core_role() -> None:
    import extension_store

    manifest = json.loads((ROOT / "extensions" / "ask" / "better-agent-extension.json").read_text(encoding="utf-8"))
    manifest["core_roles"] = ["unknown-role"]
    try:
        extension_store.validate_manifest(manifest)
    except extension_store.ExtensionError as exc:
        assert "core_roles contains unknown values" in str(exc)
    else:
        raise AssertionError("unknown core role was accepted")


def test_first_marker_purge_does_not_deadlock() -> None:
    with tempfile.TemporaryDirectory(prefix="ba-marker-lock-") as home:
        env = {**os.environ, "BETTER_AGENT_HOME": home, "PYTHONPATH": str(ROOT / "backend")}
        subprocess.run(
            [sys.executable, "-c", "import session_store; session_store.markers_for_extension_purge('fixture.extension')"],
            cwd=ROOT,
            env=env,
            check=True,
            timeout=5,
        )
