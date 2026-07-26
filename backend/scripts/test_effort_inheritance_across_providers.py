"""A reasoning_effort inherited from another session must be fitted onto the
target provider instead of rejected.

Locks the Antigravity case: the sender runs at a real effort, the target
provider exposes no efforts at all, and creating a session or sub-session on it
must succeed with an empty effort. Explicitly requested unsupported efforts must
still 400, so a caller asking for something real never gets a silent downgrade.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-effort-inheritance-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Extensions are only active under an activated installation, and a core role
# has no owner until its extension is active. Activate before importing main.
import _test_installation  # noqa: E402
_test_installation.activate(Path(_TMP_HOME))

from starlette.testclient import TestClient  # noqa: E402

import config_store  # noqa: E402
import extension_store  # noqa: E402
import main  # noqa: E402
import runtime_profile  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


def _install_core_role_extension(role: str) -> str:
    """Install a fixture extension that OWNS `role`.

    An isolated test home starts with no extensions, so the core role has no
    owner and every role-gated endpoint 404s. The manifest must declare
    `core_roles` for extension_id_for_role(role) to resolve.
    """
    extension_id = f"test-fixture-{role}"
    package = Path(_TMP_HOME) / "private-fixtures" / extension_id
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": extension_id,
        "surfaces": ["backend_feature"],
        "core_roles": [role],
        "entrypoints": {},
        "permissions": {},
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(
        json.dumps(manifest), encoding="utf-8",
    )
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(package.parent),
            "extension_path": package.name,
            "ref": "",
            "commit_sha": extension_id,
        },
        persist=True,
    )
    # Extensions install disabled, and only active ones own a core role.
    extension_store.set_enabled(extension_id, True)
    owner = extension_store.extension_id_for_role(role)
    assert owner == extension_id, f"role {role!r} resolved to {owner!r}"
    return extension_id


def _configure_internal_llm_defaults(*tasks: str) -> None:
    provider = config_store.list_providers()["providers"][0]
    assignments = config_store.get_internal_llm_assignments()
    for task in tasks:
        assignments[task] = {
            "provider_id": provider["id"],
            "model": provider["default_model"],
            "reasoning_effort": provider.get("default_reasoning_effort") or "",
        }
    config_store.set_internal_llm_assignments(assignments)


def _post(client: TestClient, path: str, body: dict):
    return client.post(
        path, json=body,
        headers={"X-Internal-Token": main.coordinator.internal_token},
    )


def main_test() -> int:
    _configure_internal_llm_defaults("default_session")
    _install_core_role_extension("team-orchestration")
    client = TestClient(main.app, client=("127.0.0.1", 50000))

    source = config_store.get_default_provider()
    source_record = config_store.get_provider(source["id"]) or {}
    source_efforts = runtime_profile.reasoning_efforts(source_record)
    assert source_efforts, "default provider must expose efforts for this test"
    sender_effort = source_efforts[0]

    effortless = config_store.add_provider({
        "name": "Effortless Test Provider",
        "kind": "agy",
        "mode": source.get("mode") or "subscription",
        "default_model": "effortless-model",
        "custom_models": ["effortless-model"],
    })
    effortless_id = effortless["id"]
    assert not runtime_profile.reasoning_efforts(
        config_store.get_provider(effortless_id) or {},
    ), "fixture provider must expose no reasoning efforts"

    sender = session_manager.create(
        name="sender",
        cwd="/repo",
        orchestration_mode="native",
        model="sender-model",
        provider_id=source["id"],
        reasoning_effort=sender_effort,
    )

    # Inherited effort the target cannot honor -> resolved, not rejected.
    created = _post(client, "/api/internal/create-session", {
        "sender_session_id": sender["id"],
        "name": "inherited onto effortless provider",
        "cwd": "/repo",
        "orchestration_mode": "native",
        "provider_id": effortless_id,
    })
    assert created.status_code == 200, created.text
    created_session = session_manager.get(created.json()["session_id"]) or {}
    assert created_session["provider_id"] == effortless_id
    assert created_session["reasoning_effort"] == "", created_session

    # Same for hidden sub-sessions.
    created_sub = _post(client, "/api/internal/create-sub-session", {
        "sender_session_id": sender["id"],
        "description": "sub onto effortless provider",
        "provider_id": effortless_id,
    })
    assert created_sub.status_code == 200, created_sub.text
    sub_session = session_manager.get(created_sub.json()["target_session_id"]) or {}
    assert sub_session["provider_id"] == effortless_id
    assert sub_session["reasoning_effort"] == "", sub_session

    # An explicitly requested unsupported effort is still a hard error.
    explicit = _post(client, "/api/internal/create-session", {
        "sender_session_id": sender["id"],
        "name": "explicit unsupported effort",
        "cwd": "/repo",
        "orchestration_mode": "native",
        "provider_id": effortless_id,
        "reasoning_effort": sender_effort,
    })
    assert explicit.status_code == 400, explicit.text
    assert "does not support reasoning_effort" in explicit.text

    # "No effort chosen" stays unset instead of being promoted to a real one.
    assert runtime_profile.fit_reasoning_effort(source_record, "") == ""
    assert runtime_profile.fit_reasoning_effort(
        config_store.get_provider(effortless_id) or {}, "",
    ) == ""

    # A supported inherited effort passes through untouched.
    same_provider = _post(client, "/api/internal/create-session", {
        "sender_session_id": sender["id"],
        "name": "inherited onto same provider",
        "cwd": "/repo",
        "orchestration_mode": "native",
    })
    assert same_provider.status_code == 200, same_provider.text
    same_session = session_manager.get(same_provider.json()["session_id"]) or {}
    assert same_session["reasoning_effort"] == sender_effort, same_session

    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main_test())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
