from __future__ import annotations

import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mobile_bundle_ticket
from secret_redaction import SecretRedactionFilter, redact_secrets


def test_ticket_is_bound_to_exact_bundle(monkeypatch) -> None:
    monkeypatch.setattr(mobile_bundle_ticket.auth, "get_session_secret", lambda: "test-secret")
    ticket = mobile_bundle_ticket.create_ticket("v1", "sha-v1")
    assert mobile_bundle_ticket.verify_ticket(ticket, "v1", "sha-v1")
    assert not mobile_bundle_ticket.verify_ticket(ticket, "v2", "sha-v1")
    assert not mobile_bundle_ticket.verify_ticket(ticket, "v1", "sha-v2")
    assert not mobile_bundle_ticket.verify_ticket(ticket + "tampered", "v1", "sha-v1")


def test_diagnostic_redaction_removes_url_and_header_secrets() -> None:
    raw = (
        "https://host/api?token=BEARER_SENTINEL&ticket=TICKET_SENTINEL "
        "Authorization: Bearer HEADER_SENTINEL refresh_token=REFRESH_SENTINEL"
    )
    redacted = redact_secrets(raw)
    for sentinel in ("BEARER_SENTINEL", "TICKET_SENTINEL", "HEADER_SENTINEL", "REFRESH_SENTINEL"):
        assert sentinel not in redacted
    assert "[REDACTED]" in redacted


def test_uvicorn_access_record_redacts_ticket() -> None:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/api/mobile/bundle/download?ticket=TICKET_SENTINEL", "1.1", 200),
        None,
    )
    assert SecretRedactionFilter().filter(record)
    rendered = record.getMessage()
    assert "TICKET_SENTINEL" not in rendered
    assert "ticket=[REDACTED]" in rendered
