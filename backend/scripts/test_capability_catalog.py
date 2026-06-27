"""Capability catalog (Build #2 core half): manifest `entrypoints.capabilities`
validation + `capability_catalog()` discovery composing full ids
(`<extension_id>:<cap_id>`) with descriptor metadata that drives load
validation, scope gating, and the release sweep.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="cap_catalog_test_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

import extension_store  # noqa: E402
from extension_store import _validate_capabilities  # noqa: E402


def test_validate_good_descriptor():
    items = _validate_capabilities(
        [{
            "id": "testape",
            "scope": "session",
            "skill": ["use-testape"],
            "mcp": ["testape"],
            "release": {"timeout_s": 300, "after_task": True},
        }],
        extension_id="ofek.testape",
    )
    assert len(items) == 1
    d = items[0]
    assert d["id"] == "testape"
    assert d["scope"] == "session"
    assert d["bare_allowed"] is False          # default-deny
    assert d["scope_gate"] == "internal"       # default internal-only
    assert d["release"] == {"timeout_s": 300, "after_task": True}
    assert d["skill"] == ["use-testape"]
    assert d["mcp"] == ["testape"]


def test_validate_rejects_bad():
    bad_cases = [
        {"id": "bad id", "scope": "session"},        # invalid id chars
        {"id": "x", "scope": "galaxy"},              # unknown scope
        {"id": "x", "scope": "session", "scope_gate": "public"},  # unknown gate
        {"id": "x", "scope": "session", "release": {"timeout_s": 0}},   # non-positive
        {"id": "x", "scope": "session", "release": {"timeout_s": -1}},
        {"id": "x", "scope": "session", "release": {"bogus": 1}},  # unknown release key
    ]
    for case in bad_cases:
        try:
            _validate_capabilities([case], extension_id="ofek.x")
        except Exception:
            pass
        else:
            raise AssertionError(f"expected rejection for {case!r}")
    # duplicate id within one extension
    try:
        _validate_capabilities(
            [{"id": "a", "scope": "session"}, {"id": "a", "scope": "turn"}],
            extension_id="ofek.x",
        )
    except Exception:
        pass
    else:
        raise AssertionError("expected duplicate-id rejection")


def test_catalog_composes_full_ids(monkeypatch=None):
    record = {
        "manifest": {
            "id": "ofek.testape",
            "entrypoints": {
                "capabilities": [
                    {"id": "testape", "scope": "session", "skill": ["use-testape"]},
                ]
            },
        }
    }
    orig = extension_store._active_records
    extension_store._active_records = lambda: [record]
    try:
        catalog = extension_store.capability_catalog()
        assert "ofek.testape:testape" in catalog
        d = catalog["ofek.testape:testape"]
        assert d["extension_id"] == "ofek.testape"
        assert d["id"] == "ofek.testape:testape"
        assert extension_store.get_capability("ofek.testape:testape") == d
        assert extension_store.get_capability("nope") is None
    finally:
        extension_store._active_records = orig


if __name__ == "__main__":
    import shutil

    try:
        test_validate_good_descriptor()
        test_validate_rejects_bad()
        test_catalog_composes_full_ids()
        print("OK")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
