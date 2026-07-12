from __future__ import annotations

import asyncio
import json
import shutil
import sys
import uuid
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home

_TMP_HOME = _test_home.isolate("ba-test-credential-pending-fast-")

from starlette.requests import Request  # noqa: E402

import extension_api  # noqa: E402
from credential_broker import consent_store  # noqa: E402


def _request(app_session_id: str) -> Request:
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/credentials/pending",
        "raw_path": b"/credentials/pending",
        "query_string": f"app_session_id={app_session_id}".encode(),
        "headers": [],
        "client": ("test", 0),
        "server": ("test", 80),
    })


def _create_pending(app_session_id: str, label: str) -> None:
    consent_store.create(
        consent_id=uuid.uuid4().hex,
        app_session_id=app_session_id,
        provider_id="provider-test",
        descriptor={"label": label, "secret_names": ["token"]},
        descriptor_hash=uuid.uuid4().hex,
        sink_public={"kind": "test"},
    )


def test_pending_credentials_use_role_gated_core_fast_path() -> None:
    source = Path(extension_api.__file__).read_text(encoding="utf-8")
    core_start = source.index("async def _dispatch_core_builtin_backend(")
    core_end = source.index("async def _dispatch_credential_broker_core_backend(", core_start)
    core_source = source[core_start:core_end]
    assert '("credential-broker", _dispatch_credential_broker_core_backend)' in core_source
    assert "if role not in owned_roles:" in core_source
    assert "if not enabled:" in core_source

    _create_pending("session-a", "Visible")
    _create_pending("session-b", "Hidden")
    response = asyncio.run(
        extension_api._dispatch_credential_broker_core_backend(
            "credentials/pending",
            _request("session-a"),
        )
    )
    assert response is not None
    assert response.status_code == 200
    body = json.loads(response.body)
    assert [record["label"] for record in body["consents"]] == ["Visible"]
    assert body["consents"][0]["secret_names"] == ["token"]
    assert "descriptor" not in body["consents"][0]

    mutation = asyncio.run(
        extension_api._dispatch_credential_broker_core_backend(
            "credentials/example/approve",
            _request("session-a"),
        )
    )
    assert mutation is None


if __name__ == "__main__":
    try:
        test_pending_credentials_use_role_gated_core_fast_path()
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print("credential pending core-fast regression passed")
