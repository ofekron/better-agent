from __future__ import annotations

import os
import sys
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-provider-suspension-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from starlette.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import config_store  # noqa: E402
import provider as provider_mod  # noqa: E402
import main  # noqa: E402
from runs_dir import runs_root  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


_RECOVERY_PROVIDER_CASES = (
    {"name": "Claude", "kind": "claude", "mode": "subscription", "default_model": "model-claude"},
    {"name": "Codex", "kind": "codex", "mode": "subscription", "default_model": "model-codex"},
    {"name": "Gemini", "kind": "gemini", "mode": "api_key", "default_model": "model-gemini"},
    {
        "name": "OpenAI",
        "kind": "openai",
        "mode": "api_key",
        "default_model": "model-openai",
        "base_url": "https://example.test/v1",
        "runner": "better_agent_runner",
    },
)


def _client() -> TestClient:
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    client.headers.update({"Authorization": f"Bearer {auth.create_token('test')}"})
    return client


def _add_provider(name: str, model: str) -> dict:
    return config_store.add_provider({
        "name": name,
        "kind": "claude",
        "mode": "subscription",
        "default_model": model,
        "custom_models": [model],
    })


def test_suspended_provider_is_not_default_or_selectable() -> None:
    client = _client()
    first = _add_provider("First", "model-first")
    second = _add_provider("Second", "model-second")
    config_store.set_default_provider(first["id"])

    r = client.post(f"/api/providers/{first['id']}/suspended", json={"suspended": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["default_provider_id"] != first["id"]
    listed = {p["id"]: p for p in body["providers"]}
    assert listed[first["id"]]["suspended"] is True
    assert not listed[body["default_provider_id"]]["suspended"]

    r = client.post(f"/api/providers/{first['id']}/set-default")
    assert r.status_code == 409, r.text

    session = session_manager.create(
        name="selector",
        cwd="/tmp",
        orchestration_mode="native",
        model="model-second",
        provider_id=second["id"],
    )
    r = client.patch(
        f"/api/sessions/{session['id']}/selectors",
        json={"provider_id": first["id"], "model": "model-first"},
    )
    assert r.status_code == 409, r.text


def test_suspended_provider_instance_blocks_runs() -> None:
    p = _add_provider("Suspended", "model-suspended")
    prov = provider_mod.get_provider(p["id"])
    config_store.set_provider_suspended(p["id"], True)

    try:
        prov.start_run(
            run_id="run-suspended",
            prompt="hi",
            cwd="/tmp",
            loop=None,  # type: ignore[arg-type]
            queue=None,  # type: ignore[arg-type]
            model="model-suspended",
            reasoning_effort=None,
            session_id=None,
            mode="native",
            app_session_id="app",
        )
    except provider_mod.ProviderSuspendedError:
        pass
    else:
        raise AssertionError("suspended provider did not reject start_run")


def test_suspended_provider_recovery_guards_are_import_safe() -> None:
    runs_root().mkdir(parents=True, exist_ok=True)
    for payload in _RECOVERY_PROVIDER_CASES:
        provider = config_store.add_provider({
            **payload,
            "custom_models": [payload["default_model"]],
        })
        prov = provider_mod.get_provider(provider["id"])
        config_store.set_provider_suspended(provider["id"], True)
        assert prov.recover_in_flight() == [], payload["kind"]


def test_suspended_provider_has_no_models() -> None:
    p = _add_provider("Models", "model-hidden")
    import models
    assert "model-hidden" in models.available_models(p["id"])
    config_store.set_provider_suspended(p["id"], True)
    assert models.available_models(p["id"]) == []
    assert models.models_catalog(p["id"])["models"] == []


if __name__ == "__main__":
    test_suspended_provider_is_not_default_or_selectable()
    test_suspended_provider_instance_blocks_runs()
    test_suspended_provider_recovery_guards_are_import_safe()
    test_suspended_provider_has_no_models()
    print("OK: provider suspension")
