from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop"))
sys.path.insert(0, str(ROOT / "backend"))

import config_store  # noqa: E402
import credential_clone_api  # noqa: E402

# desktop/ is excluded from the backend-only test image (.dockerignore); skip
# cleanly when credential_session (desktop) is not on path.
import pytest  # noqa: E402

pytest.importorskip("credential_session")
import credential_session  # noqa: E402


def test_authority_compare_set_is_atomic_and_secret_free() -> None:
    broker = credential_session.ProviderCredentialBroker()
    credentials = {"target": "old-secret"}

    with (
        patch.object(
            broker._credential_store,
            "read",
            side_effect=lambda provider_id: credentials.get(provider_id, ""),
        ),
        patch.object(
            broker._credential_store,
            "store",
            side_effect=lambda provider_id, value: credentials.__setitem__(
                provider_id,
                value,
            ),
        ),
        patch.object(
            broker._credential_store,
            "delete",
            side_effect=lambda provider_id: credentials.pop(provider_id, None),
        ),
    ):
        applied = broker.handle({
            "op": "compare_set",
            "provider_id": "target",
            "expected_value": "old-secret",
            "value": "new-secret",
            "request_id": "a" * 32,
        })
        conflict = broker.handle({
            "op": "compare_set",
            "provider_id": "target",
            "expected_value": "old-secret",
            "value": "stale-secret",
            "request_id": "b" * 32,
        })
        deleted = broker.handle({
            "op": "compare_set",
            "provider_id": "target",
            "expected_value": "new-secret",
            "value": "",
            "request_id": "c" * 32,
        })

    assert applied == {"status": "available", "applied": True}
    assert conflict == {"status": "available", "applied": False}
    assert deleted == {"status": "missing", "applied": True}
    assert credentials == {}
    responses = repr((applied, conflict, deleted))
    assert "old-secret" not in responses
    assert "new-secret" not in responses
    assert "stale-secret" not in responses


def test_compare_set_and_clone_have_one_target_write_order() -> None:
    broker = credential_session.ProviderCredentialBroker()
    credentials = {
        "source": "clone-secret",
        "target": "old-secret",
    }
    compare_reading = threading.Event()
    release_compare = threading.Event()
    results: dict[str, dict] = {}

    def read(provider_id: str) -> str:
        if (
            provider_id == "target"
            and threading.current_thread().name == "compare-writer"
        ):
            compare_reading.set()
            assert release_compare.wait(2)
        return credentials.get(provider_id, "")

    with (
        patch.object(broker._credential_store, "read", side_effect=read),
        patch.object(
            broker._credential_store,
            "store",
            side_effect=lambda provider_id, value: credentials.__setitem__(
                provider_id,
                value,
            ),
        ),
    ):
        compare = threading.Thread(
            name="compare-writer",
            target=lambda: results.__setitem__(
                "compare",
                broker.handle({
                    "op": "compare_set",
                    "provider_id": "target",
                    "expected_value": "old-secret",
                    "value": "incoming-secret",
                    "request_id": "d" * 32,
                }),
            ),
        )
        clone = threading.Thread(
            name="clone-writer",
            target=lambda: results.__setitem__(
                "clone",
                broker.handle({
                    "op": "clone",
                    "provider_id": "source",
                    "target_provider_id": "target",
                    "request_id": "e" * 32,
                }),
            ),
        )
        compare.start()
        assert compare_reading.wait(2)
        clone.start()
        release_compare.set()
        compare.join(2)
        clone.join(2)

    assert not compare.is_alive()
    assert not clone.is_alive()
    assert results == {
        "compare": {"status": "available", "applied": True},
        "clone": {"status": "available"},
    }
    assert credentials["target"] == "clone-secret"


def test_authority_clone_never_returns_plaintext() -> None:
    broker = credential_session.ProviderCredentialBroker()
    credentials = {"source": "super-secret"}

    with (
        patch.object(
            broker._credential_store,
            "read",
            side_effect=lambda provider_id: credentials.get(provider_id, ""),
        ),
        patch.object(
            broker._credential_store,
            "store",
            side_effect=lambda provider_id, value: credentials.__setitem__(
                provider_id, value
            ),
        ),
    ):
        response = broker.handle(
            {
                "op": "clone",
                "provider_id": "source",
                "target_provider_id": "isolated",
                "request_id": "0" * 32,
            }
        )

    assert response == {"status": "available"}
    assert credentials["isolated"] == "super-secret"
    assert "super-secret" not in repr(response)
    assert "value" not in response


def test_authority_clone_fails_closed_when_source_is_missing() -> None:
    broker = credential_session.ProviderCredentialBroker()
    stored: list[tuple[str, str]] = []

    with (
        patch.object(broker._credential_store, "read", return_value=""),
        patch.object(
            broker._credential_store,
            "store",
            side_effect=lambda provider_id, value: stored.append(
                (provider_id, value)
            ),
        ),
    ):
        response = broker.handle(
            {
                "op": "clone",
                "provider_id": "missing",
                "target_provider_id": "isolated",
                "request_id": "1" * 32,
            }
        )

    assert response == {"status": "missing"}
    assert stored == []


def test_clone_api_requires_internal_token_and_returns_metadata_only() -> None:
    app = FastAPI()
    credential_clone_api.configure(lambda token: token == "internal-secret")
    app.include_router(credential_clone_api.router)
    client = TestClient(app)
    payload = {
        "source_provider_id": "source",
        "target_provider_id": "isolated",
    }

    assert client.post(
        "/api/internal/provider-credentials/clone", json=payload
    ).status_code == 422
    assert client.post(
        "/api/internal/provider-credentials/clone",
        json=payload,
        headers={"X-Internal-Token": "wrong"},
    ).status_code == 403

    with patch.object(
        config_store,
        "clone_provider_credential",
        return_value="available",
    ) as clone:
        response = client.post(
            "/api/internal/provider-credentials/clone",
            json=payload,
            headers={"X-Internal-Token": "internal-secret"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "available"}
    assert clone.call_args.args == ("source", "isolated")
    assert "secret" not in response.text
    assert client.post(
        "/api/internal/provider-credentials/clone",
        json={
            "source_provider_id": "same",
            "target_provider_id": "same",
        },
        headers={"X-Internal-Token": "internal-secret"},
    ).status_code == 422


def test_failed_clone_preserves_target_cache_projection() -> None:
    config_store._api_key_cache["isolated"] = "existing-target-secret"
    config_store._credential_status["isolated"] = "available"
    try:
        with (
            patch.object(
                config_store.credential_session_client,
                "available",
                return_value=True,
            ),
            patch.object(
                config_store.credential_session_client,
                "request",
                return_value={"status": "missing"},
            ) as request,
        ):
            status = config_store.clone_provider_credential(
                "missing", "isolated"
            )

        assert status == "missing"
        assert (
            config_store._api_key_cache["isolated"]
            == "existing-target-secret"
        )
        assert config_store._credential_status["isolated"] == "available"
        assert request.call_args.args == ("read", "missing")
        assert request.call_args.kwargs == {}
    finally:
        config_store._api_key_cache.pop("isolated", None)
        config_store._credential_status.pop("isolated", None)
        config_store._credential_status.pop("missing", None)


def test_clone_api_maps_authority_failures_without_secret_content() -> None:
    app = FastAPI()
    credential_clone_api.configure(lambda token: token == "internal-secret")
    app.include_router(credential_clone_api.router)
    client = TestClient(app)
    payload = {
        "source_provider_id": "source",
        "target_provider_id": "isolated",
    }
    expected = {"missing": 404, "blocked": 503}

    for authority_status, http_status in expected.items():
        with patch.object(
            config_store,
            "clone_provider_credential",
            return_value=authority_status,
        ):
            response = client.post(
                "/api/internal/provider-credentials/clone",
                json=payload,
                headers={"X-Internal-Token": "internal-secret"},
            )
        assert response.status_code == http_status
        assert "value" not in response.text


if __name__ == "__main__":
    os.environ.setdefault("BETTER_AGENT_HOME", str(ROOT / ".better-claude"))
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
