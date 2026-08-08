"""Hermetic unit owner for provider_runtime_plan_hydration.

This module is the security-valued runtime-value hydration layer: it lets a
provider runtime plan carry opaque *references* (extension identity, file-path
hash, extension-setting hash) instead of the real secret, then swaps the real
value back in only at apply time. Every reference kind and every protective
guard is exercised deterministically here, with no live backend or model:

- ``hydration_key`` / ``_canonical_json_text`` determinism + NaN rejection,
- ``capture_runtime_hydration``: None-store short-circuit, JSON-incompatible
  value rejection, fresh write, idempotent re-write, ambiguous-write refusal,
- ``_is_hydration_reference``: every True/False sub-condition for all three
  reference kinds plus non-dict and unknown-shape inputs,
- ``apply_runtime_hydration``: available/missing/invalid reference resolution,
  list recursion, scalar passthrough, empty dict, ``_ref`` key stripping,
  plain keys, the ``_ref``-but-not-a-reference case, and ambiguous-target
  refusal,
- ``RUNNER_OPERATION_BROKER_REF`` shape and ``__all__`` surface.

conftest engages an isolated per-module ba_home().
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-hydr-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from codex_execution_common import ExecutionContractError  # noqa: E402
import provider_runtime_plan_hydration as hydr  # noqa: E402
from provider_runtime_plan_hydration import (  # noqa: E402
    RUNNER_OPERATION_BROKER_REF,
    _canonical_json_text,
    apply_runtime_hydration,
    capture_runtime_hydration,
    hydration_key,
)

_HEX64 = "0" * 64


def _ext_identity_ref(extension_id: str = "ext-1") -> dict:
    return {"kind": "extension_identity", "extension_id": extension_id}


def _path_ref(digest: str = _HEX64) -> dict:
    return {"kind": "runtime_value", "path_sha256": digest}


def _setting_ref(extension_id: str = "ext-1", digest: str = _HEX64) -> dict:
    return {
        "kind": "extension_setting",
        "extension_id": extension_id,
        "key_sha256": digest,
    }


# ---------------------------------------------------------------------------

class TestCanonicalJsonText:
    def test_deterministic_regardless_of_dict_key_order(self):
        assert _canonical_json_text({"b": 1, "a": 2}) == _canonical_json_text({"a": 2, "b": 1})

    def test_compact_separators(self):
        assert _canonical_json_text({"a": 1}) == '{"a":1}'

    def test_rejects_nan(self):
        # allow_nan=False -> ValueError; this is the contract capture relies on.
        with pytest.raises(ValueError):
            _canonical_json_text(float("nan"))


class TestHydrationKey:
    def test_is_sixtyfour_hex_and_stable(self):
        key = hydration_key(_ext_identity_ref())
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)
        assert key == hydration_key(_ext_identity_ref())

    def test_differs_for_different_reference(self):
        assert hydration_key(_ext_identity_ref()) != hydration_key(_path_ref())


class TestCaptureRuntimeHydration:
    def test_none_store_is_noop(self):
        before = {"x": 1}
        capture_runtime_hydration(None, _ext_identity_ref(), {"secret": "v"})
        assert before == {"x": 1}

    def test_fresh_write_stores_canonical_value(self):
        hydration: dict = {}
        capture_runtime_hydration(hydration, _ext_identity_ref(), {"secret": "v"})
        key = hydration_key(_ext_identity_ref())
        assert hydration[key] == _canonical_json_text({"secret": "v"})

    def test_idempotent_rewrite_same_value_is_allowed(self):
        hydration: dict = {}
        ref = _path_ref()
        capture_runtime_hydration(hydration, ref, "same")
        capture_runtime_hydration(hydration, ref, "same")  # no raise, identical
        assert hydration[hydration_key(ref)] == _canonical_json_text("same")

    def test_typeerror_value_rejected_as_contract_error(self):
        hydration: dict = {}
        with pytest.raises(ExecutionContractError, match="JSON-compatible"):
            capture_runtime_hydration(hydration, _ext_identity_ref(), {1, 2, 3})

    def test_valueerror_value_rejected_as_contract_error(self):
        hydration: dict = {}
        with pytest.raises(ExecutionContractError, match="JSON-compatible"):
            capture_runtime_hydration(hydration, _ext_identity_ref(), math.nan)

    def test_ambiguous_conflicting_value_refused(self):
        hydration: dict = {}
        ref = _setting_ref()
        capture_runtime_hydration(hydration, ref, "first")
        with pytest.raises(ExecutionContractError, match="ambiguous"):
            capture_runtime_hydration(hydration, ref, "second")
        # original value is preserved, not overwritten by the conflicting one
        assert hydration[hydration_key(ref)] == _canonical_json_text("first")


class TestIsHydrationReference:
    # --- non-dict / unknown shape ---
    def test_non_dict_is_false(self):
        assert hydr._is_hydration_reference(["kind"]) is False
        assert hydr._is_hydration_reference("extension_identity") is False
        assert hydr._is_hydration_reference(None) is False

    def test_unknown_shape_is_false(self):
        assert hydr._is_hydration_reference({}) is False
        assert hydr._is_hydration_reference({"unrelated": 1}) is False

    # --- extension_identity kind ---
    def test_extension_identity_valid(self):
        assert hydr._is_hydration_reference(_ext_identity_ref()) is True

    def test_extension_identity_wrong_kind(self):
        ref = _ext_identity_ref()
        ref["kind"] = "not-it"
        assert hydr._is_hydration_reference(ref) is False

    def test_extension_identity_non_str_id(self):
        ref = _ext_identity_ref()
        ref["extension_id"] = 5
        assert hydr._is_hydration_reference(ref) is False

    def test_extension_identity_empty_id(self):
        ref = _ext_identity_ref()
        ref["extension_id"] = ""
        assert hydr._is_hydration_reference(ref) is False

    # --- runtime_value (path_sha256) kind ---
    def test_runtime_value_valid(self):
        assert hydr._is_hydration_reference(_path_ref()) is True

    def test_runtime_value_wrong_kind(self):
        ref = _path_ref()
        ref["kind"] = "not-it"
        assert hydr._is_hydration_reference(ref) is False

    def test_runtime_value_non_str_digest(self):
        ref = _path_ref()
        ref["path_sha256"] = 64
        assert hydr._is_hydration_reference(ref) is False

    def test_runtime_value_bad_digest_format(self):
        ref = _path_ref()
        ref["path_sha256"] = "g" * 64  # not hex
        assert hydr._is_hydration_reference(ref) is False

    # --- extension_setting kind ---
    def test_extension_setting_valid(self):
        assert hydr._is_hydration_reference(_setting_ref()) is True

    def test_extension_setting_wrong_shape(self):
        # exactly the third set's keys but wrong kind
        ref = _setting_ref()
        ref["kind"] = "not-it"
        assert hydr._is_hydration_reference(ref) is False

    def test_extension_setting_non_str_id(self):
        ref = _setting_ref()
        ref["extension_id"] = 7
        assert hydr._is_hydration_reference(ref) is False

    def test_extension_setting_empty_id(self):
        ref = _setting_ref()
        ref["extension_id"] = ""
        assert hydr._is_hydration_reference(ref) is False

    def test_extension_setting_non_str_digest(self):
        ref = _setting_ref()
        ref["key_sha256"] = 64
        assert hydr._is_hydration_reference(ref) is False

    def test_extension_setting_bad_digest_format(self):
        ref = _setting_ref()
        ref["key_sha256"] = "z" * 64
        assert hydr._is_hydration_reference(ref) is False


class TestApplyRuntimeHydration:
    def test_reference_resolves_to_captured_value(self):
        hydration: dict = {}
        ref = _ext_identity_ref()
        capture_runtime_hydration(hydration, ref, {"secret": "v"})
        assert apply_runtime_hydration(ref, hydration) == {"secret": "v"}

    def test_missing_reference_raises_unavailable(self):
        with pytest.raises(ExecutionContractError, match="unavailable"):
            apply_runtime_hydration(_ext_identity_ref(), {})

    def test_invalid_stored_json_raises_invalid(self):
        ref = _path_ref()
        hydration = {hydration_key(ref): "{not json"}
        with pytest.raises(ExecutionContractError, match="invalid"):
            apply_runtime_hydration(ref, hydration)

    def test_list_recurses_elementwise(self):
        hydration: dict = {}
        ref = _setting_ref()
        capture_runtime_hydration(hydration, ref, "s")
        assert apply_runtime_hydration([ref, "plain", 3], hydration) == ["s", "plain", 3]

    def test_scalar_passthrough(self):
        assert apply_runtime_hydration(42, {}) == 42
        assert apply_runtime_hydration("text", {}) == "text"

    def test_empty_dict_returns_empty(self):
        assert apply_runtime_hydration({}, {}) == {}

    def test_dict_plain_keys_recursed(self):
        hydration: dict = {}
        inner = _ext_identity_ref()
        capture_runtime_hydration(hydration, inner, "deep")
        out = apply_runtime_hydration({"keep": inner, "n": 1}, hydration)
        assert out == {"keep": "deep", "n": 1}

    def test_ref_key_is_stripped_when_value_is_reference(self):
        hydration: dict = {}
        ref = _ext_identity_ref()
        capture_runtime_hydration(hydration, ref, "tok")
        out = apply_runtime_hydration({"token_ref": ref}, hydration)
        assert out == {"token": "tok"}

    def test_ref_key_kept_when_value_is_not_a_reference(self):
        # key ends in _ref but the value isn't a hydration reference -> key unchanged
        out = apply_runtime_hydration({"data_ref": "literal"}, {})
        assert out == {"data_ref": "literal"}

    def test_non_ref_suffix_key_kept(self):
        out = apply_runtime_hydration({"name": "x"}, {})
        assert out == {"name": "x"}

    def test_ambiguous_target_after_strip_refused(self):
        hydration: dict = {}
        ref = _ext_identity_ref()
        capture_runtime_hydration(hydration, ref, "tok")
        # "a_ref" strips to "a"; the later plain "a" collides -> ambiguous
        with pytest.raises(ExecutionContractError, match="ambiguous"):
            apply_runtime_hydration({"a_ref": ref, "a": 1}, hydration)


class TestSurface:
    def test_runner_operation_broker_ref_shape(self):
        assert RUNNER_OPERATION_BROKER_REF == {"kind": "runner_operation_broker"}

    def test_all_exports(self):
        assert set(hydr.__all__) == {
            "RUNNER_OPERATION_BROKER_REF",
            "apply_runtime_hydration",
            "capture_runtime_hydration",
            "hydration_key",
        }

    def test_capture_and_apply_roundtrip_json_types(self):
        hydration: dict = {}
        ref = _path_ref()
        payload = {"num": 7, "list": [1, 2], "nested": {"k": True}, "none": None}
        capture_runtime_hydration(hydration, ref, payload)
        assert apply_runtime_hydration(ref, hydration) == payload
        # stored form is canonical JSON text, not the raw object
        assert isinstance(hydration[hydration_key(ref)], str)
        assert json.loads(hydration[hydration_key(ref)]) == payload
