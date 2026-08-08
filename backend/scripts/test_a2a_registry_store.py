"""A2A registry store: CRUD + secret redaction. Isolated via
_test_home.isolate() before any backend import, per CLAUDE.md's
BETTER_AGENT_HOME rule — this must never touch real user state."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import _test_home
_test_home.isolate("bc-test-a2a-registry-")

import a2a_registry_store as registry  # noqa: E402


def _add(name="Weather Agent", secret="s3cr3t") -> dict:
    return registry.add(
        name=name,
        base_url="https://agent.example.com/a2a",
        auth_header_name="Authorization",
        auth_secret=secret,
        agent_card={"name": name, "version": "1.0"},
    )


def test_add_mints_id_and_persists():
    record = _add()
    assert record["id"].startswith("a2a_")
    assert record["name"] == "Weather Agent"
    fetched = registry.get(record["id"])
    assert fetched is not None
    assert fetched["auth_secret"] == "s3cr3t"


def test_list_all_contains_added_record():
    record = _add(name="List Agent")
    ids = [r["id"] for r in registry.list_all()]
    assert record["id"] in ids


def test_redact_never_includes_secret():
    record = _add(name="Secret Agent", secret="hunter2")
    redacted = registry.redact(record)
    assert "auth_secret" not in redacted
    assert redacted["has_auth_secret"] is True
    assert "hunter2" not in str(redacted)


def test_redact_no_secret_case():
    record = registry.add(
        name="No Secret Agent",
        base_url="https://agent.example.com/a2a",
        auth_header_name="",
        auth_secret="",
        agent_card={"name": "No Secret Agent", "version": "1.0"},
    )
    redacted = registry.redact(record)
    assert redacted["has_auth_secret"] is False


def test_remove_deletes_record():
    record = _add(name="Removable")
    assert registry.remove(record["id"]) is True
    assert registry.get(record["id"]) is None


def test_remove_unknown_id_returns_false():
    assert registry.remove("a2a_does_not_exist") is False


def test_update_probe_result_updates_fields():
    record = _add(name="Probed Agent")
    updated = registry.update_probe_result(
        record["id"], ok=True, error=None,
        agent_card={"name": "Probed Agent", "version": "2.0"},
    )
    assert updated is not None
    assert updated["last_probe_ok"] is True
    assert updated["last_probe_error"] is None
    assert updated["agent_card"]["version"] == "2.0"
    assert updated["last_probed_at"] is not None


def test_update_probe_result_records_failure():
    record = _add(name="Failing Agent")
    updated = registry.update_probe_result(record["id"], ok=False, error="connection refused")
    assert updated is not None
    assert updated["last_probe_ok"] is False
    assert updated["last_probe_error"] == "connection refused"


def test_invalid_id_rejected_by_get_and_remove():
    assert registry.get("../etc/passwd") is None
    assert registry.remove("../etc/passwd") is False
