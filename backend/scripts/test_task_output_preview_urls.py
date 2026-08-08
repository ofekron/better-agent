from __future__ import annotations

import shutil

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.testclient import TestClient

import _test_home

_TMP_HOME = _test_home.isolate_installed("ba-task-output-preview-")

import auth  # noqa: E402
import main  # noqa: E402
import task_output_preview_urls  # noqa: E402
from secret_redaction import redact_secrets  # noqa: E402
from stores import task_output_store  # noqa: E402


def _client() -> TestClient:
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    client.headers.update({"Authorization": f"Bearer {auth.create_token('test')}"})
    return client


def _publish() -> dict:
    return task_output_store.publish(
        task_id="task1",
        task_cwd=_TMP_HOME,
        title="Report",
        content_type="text/html",
        content="<html><body>ok</body></html>",
    )


def test_authenticated_mint_and_signed_redemption() -> None:
    output = _publish()
    client = _client()

    mint = client.get(f"/api/task-outputs/task1/{output['id']}/preview-url")
    assert mint.status_code == 200, mint.text
    preview_url = mint.json()["url"]
    assert preview_url.startswith("/api/task-output/preview/")

    anonymous = TestClient(main.app, client=("127.0.0.1", 50001))
    preview = anonymous.get(
        preview_url,
        headers={
            "Origin": "https://untrusted.example",
            "Referer": "http://localhost/routines",
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.text == "<html><body>ok</body></html>"
    assert preview.headers["content-type"].startswith("text/html")
    assert "sandbox" in preview.headers["content-security-policy"]
    assert preview.headers["x-content-type-options"] == "nosniff"
    assert preview.headers["referrer-policy"] == "no-referrer"
    assert preview.headers["cache-control"] == "private, no-store"
    assert token_not_present(preview_url)


def token_not_present(preview_url: str) -> bool:
    token = preview_url.split("/")[4]
    redacted = redact_secrets(f'GET {preview_url} HTTP/1.1')
    return token not in redacted and "[REDACTED]" in redacted


def test_preview_access_fails_closed() -> None:
    output = _publish()
    client = _client()
    anonymous = TestClient(main.app, client=("127.0.0.1", 50002))
    mint_path = f"/api/task-outputs/task1/{output['id']}/preview-url"

    assert anonymous.get(mint_path).status_code == 401
    minted = client.get(mint_path).json()["url"]
    token = minted.split("/")[4]
    assert anonymous.post(minted).status_code == 401
    assert anonymous.get(f"/api/task-output/preview/not-a-token/task1/{output['id']}").status_code == 403
    assert anonymous.get(f"/api/task-output/preview/{'x' * 4097}/task1/{output['id']}").status_code == 403
    assert anonymous.get(f"/api/task-output/preview/{token}/task2/{output['id']}").status_code == 403
    assert anonymous.get(f"/api/task-output/preview/{token}/task1/deadbeef0000").status_code == 403

    task_output_store.delete_for_task("task1")
    assert anonymous.get(minted).status_code == 404


def test_preview_token_expiry_and_secret_scope(monkeypatch) -> None:
    token = task_output_preview_urls.mint("task1", "0123456789ab")
    original_secret = auth.get_session_secret()
    monkeypatch.setattr(auth, "get_session_secret", lambda: f"rotated-{original_secret}")
    with pytest.raises(ValueError):
        task_output_preview_urls.verify(token, "task1", "0123456789ab")

    monkeypatch.setattr(auth, "get_session_secret", lambda: original_secret)
    monkeypatch.setattr(task_output_preview_urls, "MAX_AGE_SECONDS", -1)
    with pytest.raises(ValueError):
        task_output_preview_urls.verify(token, "task1", "0123456789ab")


_VALID_TASK = "task1"
_VALID_OUTPUT = "0123456789ab"


def _forge_token(payload) -> str:
    # Sign an arbitrary payload with the real secret + salt so verify() reaches
    # the type guards past the signature check.
    serializer = URLSafeTimedSerializer(
        auth.get_session_secret(), salt=task_output_preview_urls._SALT
    )
    return serializer.dumps(payload)


def test_validate_ids_rejects_invalid_task_id() -> None:
    with pytest.raises(ValueError):
        task_output_preview_urls.mint(None, _VALID_OUTPUT)
    with pytest.raises(ValueError):
        task_output_preview_urls.mint("has space", _VALID_OUTPUT)
    with pytest.raises(ValueError):
        task_output_preview_urls.mint("x" * 65, _VALID_OUTPUT)


def test_validate_ids_rejects_invalid_output_id() -> None:
    with pytest.raises(ValueError):
        task_output_preview_urls.mint(_VALID_TASK, None)
    with pytest.raises(ValueError):
        task_output_preview_urls.mint(_VALID_TASK, "too short")
    with pytest.raises(ValueError):
        task_output_preview_urls.mint(_VALID_TASK, "ABCDEF012345")


def test_verify_rejects_validly_signed_non_dict_payload() -> None:
    token = _forge_token("not-a-dict")
    with pytest.raises(ValueError):
        task_output_preview_urls.verify(token, _VALID_TASK, _VALID_OUTPUT)


def test_verify_rejects_validly_signed_dict_with_non_string_fields() -> None:
    token = _forge_token({"task_id": 123, "output_id": _VALID_OUTPUT})
    with pytest.raises(ValueError):
        task_output_preview_urls.verify(token, _VALID_TASK, _VALID_OUTPUT)
    token = _forge_token({"task_id": _VALID_TASK, "output_id": 456})
    with pytest.raises(ValueError):
        task_output_preview_urls.verify(token, _VALID_TASK, _VALID_OUTPUT)


if __name__ == "__main__":
    try:
        test_authenticated_mint_and_signed_redemption()
        task_output_store.delete_for_task("task1")
        test_preview_access_fails_closed()
        print("PASS")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
