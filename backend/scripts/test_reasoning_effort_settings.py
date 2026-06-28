from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-reasoning-effort-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import auth  # noqa: E402
import config_store  # noqa: E402
import user_prefs  # noqa: E402
from paths import ba_home  # noqa: E402
from reasoning_effort import claude_sdk_effort  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _providers(client: TestClient) -> dict:
    r = client.get("/api/providers")
    assert r.status_code == 200, r.text
    return r.json()


def _provider_by_name(client: TestClient, name: str) -> dict:
    return next(p for p in _providers(client)["providers"] if p["name"] == name)


def _add_gemini_api_provider(name: str = "Gemini API") -> dict:
    return config_store.add_provider({
        "name": name,
        "kind": "gemini",
        "mode": "api_key",
        "default_model": "gemini-2.5-pro",
        "custom_models": ["gemini-3-pro"],
    })


def _add_sakana_api_provider(name: str = "Sakana Fugu API") -> dict:
    return config_store.add_provider({
        "name": name,
        "kind": "openai",
        "mode": "api_key",
        "base_url": "https://api.sakana.ai/v1",
        "default_model": "fugu-ultra-20260615",
        "default_reasoning_effort": "",
    })


def test_default_provider_capabilities(client: TestClient) -> bool:
    claude = _provider_by_name(client, "Claude")
    codex = _provider_by_name(client, "Codex")
    if claude["reasoning_effort_options"] != ["low", "medium", "high", "xhigh"]:
        print(f"  claude options mismatch: {claude['reasoning_effort_options']!r}")
        return False
    if claude["default_reasoning_effort"] != "medium":
        print(f"  claude default mismatch: {claude['default_reasoning_effort']!r}")
        return False
    if codex["reasoning_effort_options"] != ["none", "minimal", "low", "medium", "high", "xhigh"]:
        print(f"  codex options mismatch: {codex['reasoning_effort_options']!r}")
        return False
    if codex["default_reasoning_effort"] != "medium":
        print(f"  codex default mismatch: {codex['default_reasoning_effort']!r}")
        return False
    return True


def test_sakana_api_provider_uses_fugu_efforts(client: TestClient) -> bool:
    sakana = _add_sakana_api_provider()
    if sakana["reasoning_effort_options"] != ["high", "xhigh"]:
        print(f"  sakana options mismatch: {sakana['reasoning_effort_options']!r}")
        return False
    if sakana["default_reasoning_effort"] != "high":
        print(f"  sakana default mismatch: {sakana['default_reasoning_effort']!r}")
        return False
    r = client.post(
        "/api/sessions",
        json={
            "cwd": "/tmp",
            "provider_id": sakana["id"],
            "orchestration_mode": "native",
            "reasoning_effort": "medium",
        },
    )
    if r.status_code != 400:
        print(f"  sakana accepted medium: {r.status_code} {r.text}")
        return False
    r = client.post(
        "/api/sessions",
        json={
            "cwd": "/tmp",
            "provider_id": sakana["id"],
            "orchestration_mode": "native",
            "reasoning_effort": "high",
        },
    )
    if r.status_code != 200:
        print(f"  sakana rejected high: {r.status_code} {r.text}")
        return False
    user_prefs.set_last_reasoning_effort(sakana["id"], "medium")
    sakana_from_api = next(
        p for p in _providers(client)["providers"] if p["id"] == sakana["id"]
    )
    if "last_reasoning_effort" in sakana_from_api:
        print(f"  sakana exposed stale last effort: {sakana_from_api['last_reasoning_effort']!r}")
        return False
    user_prefs.set_last_reasoning_effort(sakana["id"], "high")
    sakana_from_api = next(
        p for p in _providers(client)["providers"] if p["id"] == sakana["id"]
    )
    if sakana_from_api.get("last_reasoning_effort") != "high":
        print(f"  sakana hid valid last effort: {sakana_from_api.get('last_reasoning_effort')!r}")
        return False
    return True


def test_sakana_api_default_rejects_medium(client: TestClient) -> bool:
    r = client.post(
        "/api/providers",
        json={
            "name": "Sakana Medium",
            "kind": "openai",
            "mode": "api_key",
            "base_url": "https://api.sakana.ai/v1",
            "default_model": "fugu-ultra-20260615",
            "default_reasoning_effort": "medium",
        },
    )
    if r.status_code != 400:
        print(f"  create accepted medium default: {r.status_code} {r.text}")
        return False
    sakana = _add_sakana_api_provider("Sakana Patch")
    r = client.patch(
        f"/api/providers/{sakana['id']}",
        json={"default_reasoning_effort": "medium"},
    )
    if r.status_code != 400:
        print(f"  patch accepted medium default: {r.status_code} {r.text}")
        return False
    r = client.patch(
        f"/api/providers/{sakana['id']}",
        json={"default_reasoning_effort": "xhigh"},
    )
    if r.status_code != 200 or r.json().get("default_reasoning_effort") != "xhigh":
        print(f"  patch rejected xhigh default: {r.status_code} {r.text}")
        return False
    return True


def test_generic_openai_keeps_generic_efforts(client: TestClient) -> bool:
    provider = config_store.add_provider({
        "name": "Generic OpenAI",
        "kind": "openai",
        "mode": "api_key",
        "base_url": "https://example.test/v1",
        "default_model": "model",
        "default_reasoning_effort": "medium",
    })
    expected = ["none", "minimal", "low", "medium", "high", "xhigh"]
    if provider["reasoning_effort_options"] != expected:
        print(f"  generic options mismatch: {provider['reasoning_effort_options']!r}")
        return False
    if provider["default_reasoning_effort"] != "medium":
        print(f"  generic default mismatch: {provider['default_reasoning_effort']!r}")
        return False
    return True


def test_provider_default_persists_and_new_session_inherits(client: TestClient) -> bool:
    codex = _provider_by_name(client, "Codex")
    r = client.patch(
        f"/api/providers/{codex['id']}",
        json={"default_reasoning_effort": "xhigh"},
    )
    if r.status_code != 200:
        print(f"  provider patch failed: {r.status_code} {r.text}")
        return False
    codex = _provider_by_name(client, "Codex")
    if codex.get("default_reasoning_effort") != "xhigh":
        print(f"  provider default mismatch: {codex.get('default_reasoning_effort')!r}")
        return False
    r = client.post(
        "/api/sessions",
        json={
            "cwd": "/tmp",
            "provider_id": codex["id"],
            "orchestration_mode": "native",
        },
    )
    if r.status_code != 200:
        print(f"  create failed: {r.status_code} {r.text}")
        return False
    if r.json().get("reasoning_effort") != "xhigh":
        print(f"  inherited effort mismatch: {r.json().get('reasoning_effort')!r}")
        return False
    return True


def test_explicit_create_and_selector_patch_record_last_effort(client: TestClient) -> bool:
    codex = _provider_by_name(client, "Codex")
    r = client.post(
        "/api/sessions",
        json={
            "cwd": "/tmp",
            "provider_id": codex["id"],
            "orchestration_mode": "native",
            "reasoning_effort": "low",
        },
    )
    if r.status_code != 200:
        print(f"  create failed: {r.status_code} {r.text}")
        return False
    sid = r.json()["id"]
    codex = _provider_by_name(client, "Codex")
    if codex.get("last_reasoning_effort") != "low":
        print(f"  create last effort mismatch: {codex.get('last_reasoning_effort')!r}")
        return False
    r = client.patch(
        f"/api/sessions/{sid}/selectors",
        json={"reasoning_effort": "high"},
    )
    if r.status_code != 200:
        print(f"  selector patch failed: {r.status_code} {r.text}")
        return False
    codex = _provider_by_name(client, "Codex")
    if codex.get("last_reasoning_effort") != "high":
        print(f"  selector last effort mismatch: {codex.get('last_reasoning_effort')!r}")
        return False
    r = client.get(f"/api/sessions/{sid}")
    if r.status_code != 200 or r.json().get("reasoning_effort") != "high":
        print(f"  persisted effort mismatch: {r.status_code} {r.text}")
        return False
    return True


def test_provider_switch_to_unsupported_clears_effort(client: TestClient) -> bool:
    codex = _provider_by_name(client, "Codex")
    gemini = _add_gemini_api_provider("Gemini API Switch")
    r = client.post(
        "/api/sessions",
        json={
            "model": "gpt-5.5",
            "cwd": "/tmp",
            "provider_id": codex["id"],
            "orchestration_mode": "native",
            "reasoning_effort": "high",
        },
    )
    sid = r.json()["id"]
    r = client.patch(
        f"/api/sessions/{sid}/selectors",
        json={"provider_id": gemini["id"], "model": "gemini-3-pro"},
    )
    if r.status_code != 200:
        print(f"  provider switch failed: {r.status_code} {r.text}")
        return False
    if r.json()["updates"].get("reasoning_effort") != "":
        print(f"  updates did not clear effort: {r.json()['updates']!r}")
        return False
    r = client.get(f"/api/sessions/{sid}")
    if r.status_code != 200 or r.json().get("reasoning_effort") != "":
        print(f"  persisted clear mismatch: {r.status_code} {r.text}")
        return False
    return True


def test_unsupported_explicit_effort_is_rejected(client: TestClient) -> bool:
    gemini = _add_gemini_api_provider("Gemini API Effort")
    r = client.post(
        "/api/sessions",
        json={
            "cwd": "/tmp",
            "provider_id": gemini["id"],
            "orchestration_mode": "native",
            "reasoning_effort": "low",
        },
    )
    if r.status_code != 400:
        print(f"  unsupported effort status mismatch: {r.status_code} {r.text}")
        return False
    r = client.patch(
        f"/api/providers/{gemini['id']}",
        json={"default_reasoning_effort": "low"},
    )
    if r.status_code != 400:
        print(f"  unsupported default status mismatch: {r.status_code} {r.text}")
        return False
    return True


def test_junk_prefs_shape_is_ignored(client: TestClient) -> bool:
    prefs_path = ba_home() / "user_prefs.json"
    prefs = json.loads(prefs_path.read_text()) if prefs_path.exists() else {}
    prefs["last_reasoning_effort_by_provider"] = ["not", "a", "dict"]
    prefs_path.write_text(json.dumps(prefs))
    if user_prefs.get_last_reasoning_efforts() != {}:
        print("  junk list not ignored")
        return False
    prefs["last_reasoning_effort_by_provider"] = {"pid": 42, "": "x", "ok": "high"}
    prefs_path.write_text(json.dumps(prefs))
    if user_prefs.get_last_reasoning_efforts() != {"ok": "high"}:
        print(f"  junk entries not filtered: {user_prefs.get_last_reasoning_efforts()}")
        return False
    r = client.get("/api/providers")
    if r.status_code != 200:
        print(f"  providers failed on junk prefs: {r.status_code}")
        return False
    return True


def test_claude_sdk_effort_mapping(client: TestClient) -> bool:
    del client
    if claude_sdk_effort("xhigh") != "max":
        print("  xhigh did not map to max")
        return False
    try:
        claude_sdk_effort("minimal")
    except ValueError:
        return True
    print("  unsupported Claude effort was accepted")
    return False


TESTS = [
    ("default provider capabilities", test_default_provider_capabilities),
    ("Sakana API provider uses Fugu efforts", test_sakana_api_provider_uses_fugu_efforts),
    ("Sakana API default rejects medium", test_sakana_api_default_rejects_medium),
    ("generic OpenAI keeps generic efforts", test_generic_openai_keeps_generic_efforts),
    ("provider default persists and new session inherits", test_provider_default_persists_and_new_session_inherits),
    ("explicit create and selector patch record last effort", test_explicit_create_and_selector_patch_record_last_effort),
    ("provider switch to unsupported clears effort", test_provider_switch_to_unsupported_clears_effort),
    ("unsupported explicit effort is rejected", test_unsupported_explicit_effort_is_rejected),
    ("junk prefs shape is ignored", test_junk_prefs_shape_is_ignored),
    ("Claude SDK effort mapping", test_claude_sdk_effort_mapping),
]


def main_run() -> int:
    with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
        client.headers.update({"Authorization": f"Bearer {auth.create_token('test')}"})
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
