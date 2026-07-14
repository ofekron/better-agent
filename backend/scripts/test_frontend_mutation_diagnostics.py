from fastapi.testclient import TestClient

import auth
import main


def _client() -> tuple[TestClient, dict[str, str]]:
    return TestClient(main.app), {"Authorization": f"Bearer {auth.create_token('test')}"}


def test_accepts_bounded_structured_mutation_failure() -> None:
    client, headers = _client()
    response = client.post("/api/logs/frontend-mutation", json={
        "event": "mutation_failed",
        "action_key": "session.rename",
        "correlation_id": "00000000-0000-4000-8000-000000000123",
        "failure_kind": "rejected",
    }, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_drops_extra_or_entity_bearing_fields() -> None:
    client, headers = _client()
    response = client.post("/api/logs/frontend-mutation", json={
        "event": "mutation_failed",
        "action_key": "session.rename",
        "correlation_id": "00000000-0000-4000-8000-000000000123",
        "failure_kind": "rejected",
        "url": "/api/sessions/opaque-id/rename",
    }, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "dropped": True}
