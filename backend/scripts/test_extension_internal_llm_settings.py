from __future__ import annotations

import sys
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-extension-internal-llm-settings-")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import config_store  # noqa: E402
import extension_store  # noqa: E402
import main  # noqa: E402
import auth  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


def main_test() -> None:
    client = TestClient(main.app)
    client.headers.update({"Authorization": f"Bearer {auth.create_token('test')}"})
    config_store.set_internal_llm_assignments({
        "default_session": {"provider_id": "core-provider"},
        "assistant": {"provider_id": "extension-provider"},
    })
    extension_store.list_extensions_with_reconciliation(include_hidden=True)

    global_get = client.get("/api/settings/internal-llm")
    check(global_get.status_code == 200, "global internal-LLM settings load")
    global_body = global_get.json()
    check("assistant" not in global_body["tasks"], "global tasks hide extension-owned task")
    check("assistant" not in global_body["assignments"], "global assignments hide extension-owned task")
    check("assistant" not in global_body["labels"], "global labels hide extension-owned task")

    global_put = client.put(
        "/api/settings/internal-llm",
        json={"assignments": {"assistant": {"provider_id": "leak"}}},
    )
    check(global_put.status_code == 403, "global settings reject extension-owned task writes")

    preserve_put = client.put(
        "/api/settings/internal-llm",
        json={"assignments": {"default_session": {"provider_id": "new-core"}}},
    )
    check(preserve_put.status_code == 200, "global settings can write core task")
    stored = config_store.get_internal_llm_assignments()
    check(stored["default_session"]["provider_id"] == "new-core", "core assignment updated")
    check(stored["assistant"]["provider_id"] == "extension-provider", "extension assignment preserved")

    ext_get = client.get(f"/api/extensions/{extension_store.extension_id_for_role('assistant')}/internal-llm")
    check(ext_get.status_code == 200, "extension internal-LLM settings load")
    ext_body = ext_get.json()
    check(ext_body["tasks"] == ["assistant"], "extension settings expose owned task")
    check(ext_body["assignments"]["assistant"]["provider_id"] == "extension-provider", "extension settings expose owned assignment")

    ext_put = client.put(
        f"/api/extensions/{extension_store.extension_id_for_role('assistant')}/internal-llm",
        json={"assignments": {"assistant": {"provider_id": "new-extension"}}},
    )
    check(ext_put.status_code == 200, "extension settings can write owned task")
    stored = config_store.get_internal_llm_assignments()
    check(stored["assistant"]["provider_id"] == "new-extension", "extension assignment updated")
    check(stored["default_session"]["provider_id"] == "new-core", "core assignment preserved")

    ext_forbidden = client.put(
        f"/api/extensions/{extension_store.extension_id_for_role('assistant')}/internal-llm",
        json={"assignments": {"default_session": {"provider_id": "bad"}}},
    )
    check(ext_forbidden.status_code == 403, "extension settings reject unowned task writes")


if __name__ == "__main__":
    main_test()
