from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scripts import _test_home  # noqa: E402

TEST_HOME = _test_home.TestHome.acquire("ba-test-codex-discovery-format-")

from codex_model_discovery_format import (  # noqa: E402
    MAX_MODEL_ID_LENGTH,
    MAX_MODELS,
    CatalogOutputError,
    parse_models,
)


def _catalog_bytes(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _rejects(raw: object) -> None:
    """Assert that parse_models rejects the given payload with CatalogOutputError."""
    data = raw if isinstance(raw, bytes) else _catalog_bytes(raw)
    with pytest.raises(CatalogOutputError):
        parse_models(data)


def test_empty_models_list_returns_empty_tuple():
    assert parse_models(b'{"models": []}') == ()


def test_orders_by_priority_then_id():
    raw = {
        "models": [
            {"slug": "charlie", "visibility": "list", "priority": 5},
            {"slug": "alpha", "visibility": "list", "priority": 1},
            {"slug": "bravo", "visibility": "list", "priority": 1},
        ]
    }
    assert parse_models(_catalog_bytes(raw)) == ("alpha", "bravo", "charlie")


def test_default_priority_when_absent_sorts_last():
    raw = {
        "models": [
            {"slug": "explicit", "visibility": "list", "priority": 3},
            {"slug": "implicit", "visibility": "list"},
        ]
    }
    assert parse_models(_catalog_bytes(raw)) == ("explicit", "implicit")


def test_duplicate_slug_keeps_lowest_priority():
    raw = {
        "models": [
            {"slug": "dup", "visibility": "list", "priority": 9},
            {"slug": "dup", "visibility": "list", "priority": 2},
        ]
    }
    assert parse_models(_catalog_bytes(raw)) == ("dup",)


def test_hide_visibility_is_skipped():
    raw = {
        "models": [
            {"slug": "hidden", "visibility": "hide"},
            {"slug": "shown", "visibility": "list", "priority": 1},
        ]
    }
    assert parse_models(_catalog_bytes(raw)) == ("shown",)


def test_slug_is_nfc_normalized():
    # U+0041 U+0300 (decomposed) normalizes to U+00C0 (composed "À")
    raw = {"models": [{"slug": "À", "visibility": "list"}]}
    assert parse_models(_catalog_bytes(raw)) == ("À",)


def test_slug_whitespace_is_stripped():
    raw = {"models": [{"slug": "  gpt-x  ", "visibility": "list"}]}
    assert parse_models(_catalog_bytes(raw)) == ("gpt-x",)


def test_non_finite_number_rejected():
    # JSON allows Infinity/NaN as constants; the parser must reject them.
    _rejects(b'{"models": Infinity}')


def test_invalid_utf8_bytes_rejected():
    _rejects(b"\xff\xfe not utf-8")


def test_malformed_json_rejected():
    _rejects(b'{"models": [')


def test_payload_not_dict_rejected():
    _rejects(b'["models"]')


def test_models_not_list_rejected():
    _rejects({"models": "not-a-list"})


def test_too_many_models_rejected():
    _rejects(
        {"models": [{"slug": f"m{i}", "visibility": "list"} for i in range(MAX_MODELS + 1)]}
    )


def test_model_at_max_count_accepted():
    raw = {"models": [{"slug": f"m{i}", "visibility": "list"} for i in range(MAX_MODELS)]}
    assert len(parse_models(_catalog_bytes(raw))) == MAX_MODELS


def test_non_dict_item_rejected():
    _rejects({"models": ["not-a-dict"]})


def test_invalid_visibility_rejected():
    _rejects({"models": [{"slug": "x", "visibility": "public"}]})


def test_non_string_slug_rejected():
    _rejects({"models": [{"slug": 123, "visibility": "list"}]})


def test_empty_slug_rejected():
    _rejects({"models": [{"slug": "   ", "visibility": "list"}]})


def test_overlong_slug_rejected():
    _rejects({"models": [{"slug": "a" * (MAX_MODEL_ID_LENGTH + 1), "visibility": "list"}]})


def test_slug_at_max_length_accepted():
    slug = "a" * MAX_MODEL_ID_LENGTH
    raw = {"models": [{"slug": slug, "visibility": "list"}]}
    assert parse_models(_catalog_bytes(raw)) == (slug,)


def test_control_character_slug_rejected():
    # U+0000 has Unicode category "Cc" (control), which starts with "C".
    _rejects({"models": [{"slug": "bad\x00id", "visibility": "list"}]})


def test_non_integer_priority_rejected():
    _rejects({"models": [{"slug": "x", "visibility": "list", "priority": "1"}]})


def test_negative_priority_rejected():
    _rejects({"models": [{"slug": "x", "visibility": "list", "priority": -1}]})


def test_zero_priority_accepted():
    raw = {"models": [{"slug": "x", "visibility": "list", "priority": 0}]}
    assert parse_models(_catalog_bytes(raw)) == ("x",)


def test_bool_priority_rejected():
    # bool is a subclass of int, but the parser requires type(...) is int exactly.
    _rejects({"models": [{"slug": "x", "visibility": "list", "priority": True}]})


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
