from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-last-model-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

from auth_test_helpers import authenticate_client  # noqa: E402
import main  # noqa: E402
import user_prefs  # noqa: E402
from paths import ba_home  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _providers(client: TestClient) -> dict:
    r = client.get("/api/providers")
    assert r.status_code == 200, r.text
    return r.json()


def _provider_by_name(client: TestClient, name: str) -> dict:
    return next(p for p in _providers(client)["providers"] if p["name"] == name)


def _other_provider(client: TestClient, current_id: str) -> dict:
    return next(p for p in _providers(client)["providers"] if p["id"] != current_id)


def _provider_model(client: TestClient, provider: dict) -> str:
    r = client.get(f"/api/providers/{provider['id']}/models")
    assert r.status_code == 200, r.text
    models = r.json().get("models") or []
    return models[0] if models else provider.get("default_model") or ""


def _provider_models(client: TestClient, provider: dict) -> list[str]:
    r = client.get(f"/api/providers/{provider['id']}/models")
    assert r.status_code == 200, r.text
    return [m for m in r.json().get("models") or [] if isinstance(m, str) and m]


def test_create_session_records_last_model(client: TestClient) -> bool:
    claude = _provider_by_name(client, "Claude")
    r = client.post(
        "/api/sessions",
        json={"model": "claude-sonnet-4-6", "cwd": "/tmp", "provider_id": claude["id"]},
    )
    if r.status_code != 200:
        print(f"  create failed: {r.status_code} {r.text}")
        return False
    claude = _provider_by_name(client, "Claude")
    if claude.get("last_model") != "claude-sonnet-4-6":
        print(f"  last_model mismatch: {claude.get('last_model')!r}")
        return False
    return True


def test_create_without_explicit_model_does_not_record(client: TestClient) -> bool:
    codex = _provider_by_name(client, "Codex")
    r = client.post(
        "/api/sessions",
        json={"cwd": "/tmp", "provider_id": codex["id"], "orchestration_mode": "native"},
    )
    if r.status_code != 200:
        print(f"  create failed: {r.status_code} {r.text}")
        return False
    codex = _provider_by_name(client, "Codex")
    if "last_model" in codex:
        print(f"  default fallback was recorded: {codex.get('last_model')!r}")
        return False
    return True


def test_selectors_model_patch_updates_last_model(client: TestClient) -> bool:
    claude = _provider_by_name(client, "Claude")
    model = _provider_model(client, claude)
    r = client.post(
        "/api/sessions",
        json={"cwd": "/tmp", "provider_id": claude["id"], "orchestration_mode": "native"},
    )
    sid = r.json()["id"]
    r = client.patch(
        f"/api/sessions/{sid}/selectors", json={"model": model},
    )
    if r.status_code != 200:
        print(f"  patch failed: {r.status_code} {r.text}")
        return False
    claude = _provider_by_name(client, "Claude")
    if claude.get("last_model") != model:
        print(f"  last_model mismatch: {claude.get('last_model')!r}")
        return False
    return True


def test_combined_provider_and_model_patch_records_under_new_provider(
    client: TestClient,
) -> bool:
    claude = _provider_by_name(client, "Claude")
    other = _other_provider(client, claude["id"])
    other_model = _provider_model(client, other)
    r = client.post(
        "/api/sessions",
        json={
            "model": _provider_model(client, claude),
            "cwd": "/tmp",
            "provider_id": claude["id"],
            "orchestration_mode": "native",
        },
    )
    sid = r.json()["id"]
    r = client.patch(
        f"/api/sessions/{sid}/selectors",
        json={"provider_id": other["id"], "model": other_model},
    )
    if r.status_code != 200:
        print(f"  patch failed: {r.status_code} {r.text}")
        return False
    refreshed_other = next(p for p in _providers(client)["providers"] if p["id"] == other["id"])
    if refreshed_other.get("last_model") != other_model:
        print(f"  new provider last_model mismatch: {refreshed_other.get('last_model')!r}")
        return False
    claude = _provider_by_name(client, "Claude")
    if claude.get("last_model") == other_model:
        print("  recorded under OLD provider")
        return False
    return True


def test_provider_model_patch_allowed_with_active_run_marker(client: TestClient) -> bool:
    claude = _provider_by_name(client, "Claude")
    other = _other_provider(client, claude["id"])
    other_model = _provider_model(client, other)
    r = client.post(
        "/api/sessions",
        json={
            "model": _provider_model(client, claude),
            "cwd": "/tmp",
            "provider_id": claude["id"],
            "orchestration_mode": "native",
        },
    )
    sid = r.json()["id"]
    main.coordinator.turn_manager.active_run_ids[sid] = ["run-still-owned-by-old-provider"]
    try:
        r = client.patch(
            f"/api/sessions/{sid}/selectors",
            json={"provider_id": other["id"], "model": other_model},
        )
    finally:
        main.coordinator.turn_manager.active_run_ids.pop(sid, None)
    if r.status_code != 200:
        print(f"  patch failed despite active run marker: {r.status_code} {r.text}")
        return False
    body = r.json().get("updates") or {}
    if body.get("provider_id") != other["id"] or body.get("model") != other_model:
        print(f"  update body mismatch: {body!r}")
        return False
    return True


def test_selectors_patch_rejects_unknown_provider(client: TestClient) -> bool:
    claude = _provider_by_name(client, "Claude")
    r = client.post(
        "/api/sessions",
        json={
            "model": _provider_model(client, claude),
            "cwd": "/tmp",
            "provider_id": claude["id"],
            "orchestration_mode": "native",
        },
    )
    sid = r.json()["id"]
    r = client.patch(
        f"/api/sessions/{sid}/selectors",
        json={"provider_id": "missing-provider", "model": "ghost-model"},
    )
    if r.status_code != 400:
        print(f"  unknown provider accepted: {r.status_code} {r.text}")
        return False
    session = client.get(f"/api/sessions/{sid}").json()
    if session.get("provider_id") != claude["id"]:
        print(f"  provider changed despite rejection: {session.get('provider_id')!r}")
        return False
    return True


def test_provider_patch_without_model_uses_new_provider_default(client: TestClient) -> bool:
    claude = _provider_by_name(client, "Claude")
    other = _other_provider(client, claude["id"])
    other_default = other.get("default_model")
    if not other_default:
        print("  fixture provider has no default_model")
        return False
    r = client.post(
        "/api/sessions",
        json={
            "model": _provider_model(client, claude),
            "cwd": "/tmp",
            "provider_id": claude["id"],
            "orchestration_mode": "native",
        },
    )
    sid = r.json()["id"]
    r = client.patch(
        f"/api/sessions/{sid}/selectors",
        json={"provider_id": other["id"]},
    )
    if r.status_code != 200:
        print(f"  patch failed: {r.status_code} {r.text}")
        return False
    body = r.json().get("updates") or {}
    if body.get("provider_id") != other["id"] or body.get("model") != other_default:
        print(f"  update body mismatch: {body!r}")
        return False
    session = client.get(f"/api/sessions/{sid}").json()
    if session.get("model") != other_default:
        print(f"  persisted model mismatch: {session.get('model')!r}")
        return False
    return True


def test_provider_patch_without_model_prefers_last_model(client: TestClient) -> bool:
    claude = _provider_by_name(client, "Claude")
    candidates = [p for p in _providers(client)["providers"] if p["id"] != claude["id"]]
    target = None
    remembered = ""
    for provider in candidates:
        default = provider.get("default_model")
        alternate = next((m for m in _provider_models(client, provider) if m != default), "")
        if alternate:
            target = provider
            remembered = alternate
            break
    if not target:
        print("  no provider exposes an alternate valid model")
        return False
    if not user_prefs.set_last_model(target["id"], remembered):
        print("  failed to seed last_model")
        return False
    r = client.post(
        "/api/sessions",
        json={
            "model": _provider_model(client, claude),
            "cwd": "/tmp",
            "provider_id": claude["id"],
            "orchestration_mode": "native",
        },
    )
    sid = r.json()["id"]
    r = client.patch(
        f"/api/sessions/{sid}/selectors",
        json={"provider_id": target["id"]},
    )
    if r.status_code != 200:
        print(f"  patch failed: {r.status_code} {r.text}")
        return False
    body = r.json().get("updates") or {}
    if body.get("model") != remembered:
        print(f"  last_model not preferred: {body!r}, expected {remembered!r}")
        return False
    return True


def test_junk_prefs_shape_is_ignored(client: TestClient) -> bool:
    prefs_path = ba_home() / "user_prefs.json"
    prefs = json.loads(prefs_path.read_text()) if prefs_path.exists() else {}
    prefs["last_model_by_provider"] = ["not", "a", "dict"]
    prefs_path.write_text(json.dumps(prefs))
    if user_prefs.get_last_models() != {}:
        print("  junk list not ignored")
        return False
    prefs["last_model_by_provider"] = {"pid": 42, "": "x", "ok": "model-1"}
    prefs_path.write_text(json.dumps(prefs))
    if user_prefs.get_last_models() != {"ok": "model-1"}:
        print(f"  junk entries not filtered: {user_prefs.get_last_models()}")
        return False
    r = client.get("/api/providers")
    if r.status_code != 200:
        print(f"  providers failed on junk prefs: {r.status_code}")
        return False
    return True


def test_set_last_model_change_detection(client: TestClient) -> bool:
    if not user_prefs.set_last_model("prov-x", "m1"):
        print("  first set not reported as change")
        return False
    if user_prefs.set_last_model("prov-x", "m1"):
        print("  no-op set reported as change")
        return False
    if not user_prefs.set_last_model("prov-x", "m2"):
        print("  value change not reported")
        return False
    return True


TESTS = [
    ("create session records last_model", test_create_session_records_last_model),
    ("create without explicit model does not record", test_create_without_explicit_model_does_not_record),
    ("selectors model PATCH updates last_model", test_selectors_model_patch_updates_last_model),
    ("combined provider+model PATCH records under new provider", test_combined_provider_and_model_patch_records_under_new_provider),
    ("provider+model PATCH allowed with active run marker", test_provider_model_patch_allowed_with_active_run_marker),
    ("selectors PATCH rejects unknown provider", test_selectors_patch_rejects_unknown_provider),
    ("provider PATCH without model uses new provider default", test_provider_patch_without_model_uses_new_provider_default),
    ("provider PATCH without model prefers last_model", test_provider_patch_without_model_prefers_last_model),
    ("junk prefs shape is ignored", test_junk_prefs_shape_is_ignored),
    ("set_last_model change detection", test_set_last_model_change_detection),
]


def main_run() -> int:
    with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
        authenticate_client(client)
        failed = 0
        try:
            for name, fn in TESTS:
                try:
                    ok = fn(client)
                except Exception as e:
                    ok = False
                    import traceback
                    traceback.print_exc()
                    print(f"  exception: {e}")
                print(f"{PASS if ok else FAIL}  {name}")
                if not ok:
                    failed += 1
        finally:
            shutil.rmtree(_TMP_HOME, ignore_errors=True)
        print()
        if failed:
            print(f"{failed} of {len(TESTS)} test(s) FAILED")
            return 1
        print(f"all {len(TESTS)} tests passed")
        return 0


if __name__ == "__main__":
    sys.exit(main_run())
