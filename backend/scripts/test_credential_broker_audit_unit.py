"""Unit coverage for credential_broker/audit.py — the append-only audit log.

The audit log is itself a leak surface, so it is scrubbed by construction:
only an allowlist of metadata fields may ever be persisted, NEVER the secret
value, the operation result body, or a raw template string (which carries a
templated secret position). These tests own every branch of `record()` and
prove the security guarantees directly: scrubbed-by-construction, fail-closed
allowlist, append-only, restricted file mode.

`record()` is the sole public entry; `_path()` is driven through it. No test
imports `record` indirectly — nothing else in the repo imports this module,
which is exactly why a dedicated owner exists.
"""
from __future__ import annotations

import json
import stat

import pytest

from credential_broker import audit
from paths import ba_home

_FORBIDDEN_KEYS = ("secret", "value", "result", "body", "template", "url_template")


@pytest.fixture(autouse=True)
def _isolated_audit_log():
    """Each test starts from an empty log. The module-home audit.jsonl persists
    across tests in this module (the conftest per-module home is not wiped), so
    truncate explicitly for hermetic isolation."""
    p = audit._path()
    if p.exists():
        p.unlink()
    yield


def _read_entries() -> list[dict]:
    p = audit._path()
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


# ── minimal write ──────────────────────────────────────────────


def test_record_minimal_writes_valid_jsonl_with_ts_and_event():
    audit.record("requested")
    entries = _read_entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["event"] == "requested"
    assert isinstance(entry["ts"], str) and entry["ts"]
    # No optional key present when none supplied.
    assert set(entry) == {"ts", "event"}


# ── optional fields: included when set, omitted when None ──────


def test_record_includes_all_optional_fields_when_provided():
    audit.record(
        "executed",
        consent_id="c-1",
        provider_id="github",
        app_session_id="sess-9",
        computed_host="api.github.com",
        computed_target="POST https://api.github.com/repos",
        risk="high",
        outcome="success",
        status_code=200,
    )
    (entry,) = _read_entries()
    assert entry["consent_id"] == "c-1"
    assert entry["provider_id"] == "github"
    assert entry["app_session_id"] == "sess-9"
    assert entry["computed_host"] == "api.github.com"
    assert entry["computed_target"] == "POST https://api.github.com/repos"
    assert entry["risk"] == "high"
    assert entry["outcome"] == "success"
    assert entry["status_code"] == 200


def test_record_omits_optional_fields_when_none():
    # Every optional defaults to None → none of their keys may appear.
    audit.record("requested")
    (entry,) = _read_entries()
    for k in (
        "consent_id",
        "provider_id",
        "app_session_id",
        "computed_host",
        "computed_target",
        "risk",
        "outcome",
        "status_code",
    ):
        assert k not in entry


def test_record_preserves_status_code_int_type():
    audit.record("executed", status_code=404)
    (entry,) = _read_entries()
    assert entry["status_code"] == 404
    assert isinstance(entry["status_code"], int)


# ── append-only accumulation ───────────────────────────────────


def test_record_appends_each_call_as_a_new_line_in_order():
    audit.record("requested", consent_id="c-1")
    audit.record("approved", consent_id="c-1")
    audit.record("executed", consent_id="c-1", status_code=200)
    entries = _read_entries()
    assert [e["event"] for e in entries] == ["requested", "approved", "executed"]
    assert [e["consent_id"] for e in entries] == ["c-1", "c-1", "c-1"]


# ── scrubbed-by-construction: secrets/results never persisted ──


def test_record_never_persists_secret_or_result_keys():
    # The public API has no secret/result parameter, but prove by construction
    # that even a fully-populated record carries none of the leak-surface keys.
    audit.record(
        "executed",
        consent_id="c-1",
        provider_id="github",
        computed_host="api.github.com",
        computed_target="POST https://api.github.com/repos",
        risk="high",
        outcome="success",
        status_code=200,
    )
    raw = audit._path().read_text()
    (entry,) = _read_entries()
    for forbidden in _FORBIDDEN_KEYS:
        assert forbidden not in entry
        assert forbidden not in raw


def test_allowlist_filter_drops_any_non_allowlisted_key(monkeypatch):
    # White-box: the defensive allowlist filter is scrubbed-by-construction, so
    # its drop-branch is unreachable through the public API (every real key is
    # allowlisted). Empty the allowlist to prove the filter actively rejects any
    # key that is not "ts" — the security mechanism the comment describes.
    monkeypatch.setattr(audit, "_ALLOWED_FIELDS", ())
    audit.record(
        "executed",
        consent_id="c-1",
        provider_id="github",
        status_code=200,
    )
    (entry,) = _read_entries()
    # "ts" is special-cased; "event" and every optional are now filtered out.
    assert set(entry) == {"ts"}


# ── restricted file mode + directory creation ──────────────────


def test_record_creates_file_with_restricted_permissions():
    audit.record("requested")
    mode = stat.S_IMODE(audit._path().stat().st_mode)
    assert mode == 0o600


def test_path_creates_credential_broker_directory_under_home():
    audit.record("requested")
    assert (ba_home() / "credential_broker").is_dir()
    assert audit._path().name == "audit.jsonl"
