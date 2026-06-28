from __future__ import annotations

import os
import shutil
import sys
import tempfile

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-session-create-api-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_BACKEND)
os.makedirs(os.path.join(_ROOT, "frontend", "dist"), exist_ok=True)
open(os.path.join(_ROOT, "frontend", "dist", "index.html"), "a").close()
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import auth  # noqa: E402
import config_store  # noqa: E402
import session_store  # noqa: E402


def main_run() -> int:
    ok = True

    def check(label: str, condition: bool) -> None:
        nonlocal ok
        print(("PASS " if condition else "FAIL ") + label)
        ok = ok and condition

    with TestClient(main.app, client=("127.0.0.1", 50000)) as client:
        api_headers = {"Authorization": f"Bearer {auth.create_token('test')}"}
        response = client.post(
            "/api/sessions",
            headers=api_headers,
            json={
                "name": "backend-owned",
                "cwd": "/tmp/repo",
                "orchestration_mode": "native",
                "source": "cli",
                "worker_creation_policy": "approve",
                "bare_config": True,
                "node_id": "primary",
            },
        )
        check("create accepted", response.status_code == 200)
        body = response.json()
        sid = body.get("id")
        stored = session_store.get_session(sid)
        summary = next((s for s in session_store.list_sessions() if s.get("id") == sid), {})
        check("response has approve policy", body.get("worker_creation_policy") == "approve")
        check("response has bare_config true", body.get("bare_config") is True)
        check("stored has approve policy", stored.get("worker_creation_policy") == "approve")
        check("stored has bare_config true", stored.get("bare_config") is True)
        check("summary has approve policy", summary.get("worker_creation_policy") == "approve")
        check("summary has bare_config true", summary.get("bare_config") is True)
        check("summary has node_id", summary.get("node_id") == "primary")
        check("summary has kind", summary.get("kind") == "user")
        check("UI/API create is user_initiated", body.get("user_initiated") is True)
        check("stored UI/API create is user_initiated", stored.get("user_initiated") is True)
        check("summary UI/API create is user_initiated", summary.get("user_initiated") is True)

        invalid_policy = client.post(
            "/api/sessions",
            headers=api_headers,
            json={"worker_creation_policy": "whatever"},
        )
        check("invalid policy rejected", invalid_policy.status_code == 400)

        invalid_bare = client.post(
            "/api/sessions",
            headers=api_headers,
            json={"bare_config": "yes"},
        )
        check("invalid bare_config rejected", invalid_bare.status_code == 400)

        internal = client.post(
            "/api/internal/create-session",
            headers={"X-Internal-Token": main.coordinator.internal_token},
            json={
                "name": "sdk-owned",
                "cwd": "/tmp/repo",
                "orchestration_mode": "native",
                "bare_config": True,
                "capability_contexts": [
                    {
                        "source_id": "testape:ctx",
                        "capability_id": "testape:ctx",
                        "name": "TestApe context",
                        "outputs": [
                            {
                                "provider_kind": "claude",
                                "content_kind": "instructions",
                                "content": "Use TestApe internals.",
                            }
                        ],
                    }
                ],
            },
        )
        check("internal create accepted", internal.status_code == 200)
        internal_body = internal.json()
        internal_sid = internal_body.get("session_id")
        internal_stored = main.session_manager.get(internal_sid)
        check("internal response has bare_config true", internal_body.get("bare_config") is True)
        check("internal stored has bare_config true", internal_stored.get("bare_config") is True)
        check("internal agent-created session is NOT user_initiated", internal_stored.get("user_initiated") is False)
        check(
            "internal stored has capability contexts",
            internal_stored.get("capability_contexts") == [
                {
                    "source_id": "testape:ctx",
                    "capability_id": "testape:ctx",
                    "name": "TestApe context",
                    "category": "",
                    "outputs": [
                        {
                            "provider_kind": "claude",
                            "provider_name": "",
                            "content_kind": "instructions",
                            "content": "Use TestApe internals.",
                        }
                    ],
                }
            ],
        )
        invalid_internal_bare = client.post(
            "/api/internal/create-session",
            headers={"X-Internal-Token": main.coordinator.internal_token},
            json={"name": "bad", "cwd": "/tmp/repo", "bare_config": "yes"},
        )
        check("internal invalid bare_config rejected", invalid_internal_bare.status_code == 400)
        invalid_internal_context = client.post(
            "/api/internal/create-session",
            headers={"X-Internal-Token": main.coordinator.internal_token},
            json={
                "name": "bad-context",
                "cwd": "/tmp/repo",
                "capability_contexts": [{"source_id": "missing-outputs"}],
            },
        )
        check("internal invalid capability_contexts rejected", invalid_internal_context.status_code == 400)

        active = config_store.get_default_provider()
        assert active is not None
        config_store.update_provider(active["id"], {"default_model": ""})
        missing_model = client.post(
            "/api/sessions",
            headers=api_headers,
            json={"name": "missing-model", "cwd": "/tmp/repo"},
        )
        check("missing provider default model rejected", missing_model.status_code == 400)
        explicit_model = client.post(
            "/api/sessions",
            headers=api_headers,
            json={"name": "explicit-model", "cwd": "/tmp/repo", "model": "manual-model"},
        )
        check("explicit model still accepted", explicit_model.status_code == 200)
        config_store.update_provider(active["id"], {"default_model": active["default_model"]})

    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main_run())
