"""Hermetic unit owner for requirements_run_observation.

This module is the pure-logic reducer that turns a requirement-search
processor dispatch (timings + provider event stream) into a structured
observation dict: provider timings, which search tools were used, search
rounds, per-tool result counts, and error/truncation tallies. It owns no
state, does no I/O, and touches no backend module — every branch is exercised
deterministically here:

- ``observe_processor_attempt``: empty/non-dict inputs, full vector happy path,
  off-profile tool (policy invalid + latency short-circuit), decode-failure
  (error/truncation tallies collapse to None), and the error/truncation tally
  branch with retries and result counts,
- ``_tool_activity``: non-list events, empty-name tool_use skip, empty-id
  tool_result skip, and unknown block types,
- ``_event_blocks``: non-dict event, missing data, message-vs-data content
  sources, non-list content, and non-dict block filtering,
- ``_canonical_tool_name``: each search-tool suffix plus the no-match case,
- ``_decode_tool_result``: dict passthrough, list-of-text join+parse, empty
  list, non-str scalars, valid/invalid/non-dict JSON strings,
- ``_optional_number`` / ``_optional_int``: numeric acceptance plus bool/float
  rejection,
- ``_unique``: order-preserving dedupe with falsy filtering,
- ``SEARCH_TOOLS`` surface.

conftest engages an isolated per-module ba_home().
"""
from __future__ import annotations

import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-rrobs-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import requirements_run_observation as obs  # noqa: E402
from requirements_run_observation import (  # noqa: E402
    SEARCH_TOOLS,
    _canonical_tool_name,
    _decode_tool_result,
    _event_blocks,
    _optional_int,
    _optional_number,
    _tool_activity,
    _unique,
    observe_processor_attempt,
)


def _event_with_message(blocks: list) -> dict:
    return {"data": {"message": {"content": blocks}}}


def _event_with_data_content(blocks: list) -> dict:
    return {"data": {"content": blocks}}


def _tool_use(name: str, uid: str = "t1", tool_input: object = None) -> dict:
    return {"type": "tool_use", "id": uid, "name": name, "input": tool_input}


def _tool_result(uid: str, content: object) -> dict:
    return {"type": "tool_result", "tool_use_id": uid, "content": content}


# ---------------------------------------------------------------------------
# observe_processor_attempt


class TestObserveEmptyAndNonDictInputs:
    def test_empty_dispatch_yields_all_defaults(self):
        result = observe_processor_attempt(
            provider_id="claude",
            model="sonnet",
            timings_ms={},
            dispatch_result={},
        )
        assert result == {
            "provider": {
                "provider_id": "claude",
                "model": "sonnet",
                "resolve_ms": None,
                "prepare_ms": None,
                "dispatch_ms": None,
                "dispatch_to_runner_ms": None,
                "runner_to_native_session_ms": None,
                "first_event_ms": None,
                "total_ms": None,
            },
            "tools": {
                "observed_names": [],
                "first_allowed_search_tool": None,
                "first_allowed_search_latency_ms": None,
                "off_profile_names": [],
                "policy_valid": None,
            },
            "search": {
                "rounds": 0,
                "vector_rounds": 0,
                "vector_params": [],
                "calls_by_tool": {},
                "non_vector_retries_by_tool": {},
                "result_counts_by_tool": {},
                "error_count": None,
                "truncated_count": None,
            },
        }

    def test_non_dict_timings_and_dispatch_collapse_to_empty(self):
        result = observe_processor_attempt(
            provider_id=None,
            model=None,
            timings_ms=None,
            dispatch_result="not a dict",
        )
        assert result["provider"]["provider_id"] is None
        assert result["provider"]["model"] is None
        # non-dict timings => every timing field stays None
        assert result["provider"]["resolve_ms"] is None
        assert result["provider"]["prepare_ms"] is None
        # non-dict dispatch => no events => policy unknown
        assert result["tools"]["policy_valid"] is None


class TestObserveFullVectorHappyPath:
    def test_vector_search_roundtrip_with_counts_and_latency(self):
        events = [
            _event_with_message([
                _tool_use("mcp__search_requirement_units_vector", "t1", {"top_k": 5, "min_score": 0.2}),
                _tool_use("mcp__search_requirement_units_vector", "t2", {}),
            ]),
            _event_with_message([
                _tool_result("t1", {"count": 3}),
                _tool_result("t2", [{"text": '{"count": 1}'}]),
            ]),
        ]
        result = observe_processor_attempt(
            provider_id="claude",
            model="haiku",
            timings_ms={
                "resolve_config_ms": 10,
                "ensure_lifecycle_ms": 5,
                "build_prompts_ms": 3,
                "dispatch_ms": 20,
                "dispatch_runner_enqueue_to_first_tool_ms": 8,
                "dispatch_runner_enqueue_to_first_event_ms": 7,
                "total_ms": 100,
            },
            dispatch_result={"events": events},
        )
        # provider timings coerced to float
        assert result["provider"]["resolve_ms"] == 10.0
        assert result["provider"]["prepare_ms"] == 8.0  # 5 + 3
        assert result["provider"]["dispatch_ms"] == 20.0
        assert result["provider"]["first_event_ms"] == 7.0
        assert result["provider"]["total_ms"] == 100.0
        # tools
        assert result["tools"]["observed_names"] == ["mcp__search_requirement_units_vector"]
        assert result["tools"]["first_allowed_search_tool"] == "search_requirement_units_vector"
        assert result["tools"]["first_allowed_search_latency_ms"] == 8.0
        assert result["tools"]["off_profile_names"] == []
        assert result["tools"]["policy_valid"] is True
        # search
        assert result["search"]["rounds"] == 2
        assert result["search"]["vector_rounds"] == 2
        assert result["search"]["vector_params"] == [
            {"top_k": 5, "min_score": 0.2},
            {"top_k": None, "min_score": None},
        ]
        assert result["search"]["calls_by_tool"] == {"search_requirement_units_vector": 2}
        assert result["search"]["result_counts_by_tool"] == {"search_requirement_units_vector": [3, 1]}
        # all results decoded cleanly with no errors/truncation
        assert result["search"]["error_count"] == 0
        assert result["search"]["truncated_count"] == 0
        # vector-only => no non-vector retries recorded
        assert result["search"]["non_vector_retries_by_tool"] == {}


class TestObserveOffProfileTool:
    def test_unknown_tool_marks_policy_invalid_and_skips_latency(self):
        events = [_event_with_message([
            # input is not a dict => coerced to {} inside the tool_use row
            _tool_use("rogue_tool", "t1", "input-not-dict"),
        ])]
        result = observe_processor_attempt(
            provider_id="claude", model="m", timings_ms={}, dispatch_result={"events": events},
        )
        assert result["tools"]["observed_names"] == ["rogue_tool"]
        assert result["tools"]["off_profile_names"] == ["rogue_tool"]
        assert result["tools"]["first_allowed_search_tool"] is None
        # first tool is off-profile => latency short-circuits to None
        assert result["tools"]["first_allowed_search_latency_ms"] is None
        assert result["tools"]["policy_valid"] is False
        assert result["search"]["rounds"] == 0


class TestObserveErrorTruncationTallies:
    def test_failed_and_truncated_results_counted_with_retries(self):
        # rg called twice (=> one retry), results carry success=False / truncated=True
        events = [
            _event_with_data_content([
                _tool_use("p__search_requirement_units_rg", "a1"),
                _tool_use("p__search_requirement_units_rg", "a2"),
            ]),
            _event_with_data_content([
                _tool_result("a1", {"success": False, "count": 0}),
                _tool_result("a2", {"truncated": True}),
            ]),
        ]
        result = observe_processor_attempt(
            provider_id="claude", model="m",
            timings_ms={"ensure_lifecycle_ms": 1, "build_prompts_ms": 2},
            dispatch_result={"events": events},
        )
        assert result["search"]["rounds"] == 2
        assert result["search"]["error_count"] == 1
        assert result["search"]["truncated_count"] == 1
        assert result["search"]["non_vector_retries_by_tool"] == {"search_requirement_units_rg": 1}
        # only a1 carries a count; a2 has no count key => skipped
        assert result["search"]["result_counts_by_tool"] == {"search_requirement_units_rg": [0]}
        assert result["provider"]["prepare_ms"] == 3.0
        assert result["tools"]["policy_valid"] is True

    def test_undecodable_result_collapses_tallies_to_none(self):
        events = [
            _event_with_message([_tool_use("q__search_requirement_units_fts", "b1")]),
            _event_with_message([_tool_result("b1", "not json")]),
        ]
        result = observe_processor_attempt(
            provider_id="claude", model="m", timings_ms={}, dispatch_result={"events": events},
        )
        assert result["search"]["rounds"] == 1
        assert result["search"]["result_counts_by_tool"] == {}
        assert result["search"]["error_count"] is None
        assert result["search"]["truncated_count"] is None


# ---------------------------------------------------------------------------
# _tool_activity


class TestToolActivity:
    def test_non_list_events_returns_empty(self):
        assert _tool_activity(None) == ([], {})
        assert _tool_activity("events") == ([], {})

    def test_skips_empty_name_tool_use_and_empty_id_tool_result(self):
        events = [
            {"data": {"message": {"content": [
                {"type": "tool_use", "id": "x", "name": ""},          # empty name => skip
                {"type": "tool_use", "id": None, "name": "t"},        # name present => kept (id "")
                {"type": "tool_result", "tool_use_id": "", "content": "c"},  # empty id => skip
                {"type": "tool_result", "tool_use_id": "r1", "content": "v"},
                {"type": "other", "id": "z"},                          # unknown type => skip
            ]}}},
        ]
        uses, results = _tool_activity(events)
        assert len(uses) == 1
        assert uses[0]["name"] == "t"
        assert uses[0]["id"] == ""        # id coerced from None -> ""
        assert uses[0]["input"] == {}     # missing input -> {}
        assert results == {"r1": "v"}


# ---------------------------------------------------------------------------
# _event_blocks


class TestEventBlocks:
    def test_non_dict_event_returns_empty(self):
        assert _event_blocks(None) == []
        assert _event_blocks("x") == []

    def test_missing_data_returns_empty(self):
        assert _event_blocks({"other": 1}) == []

    def test_message_content_source(self):
        block = {"type": "tool_use"}
        event = {"data": {"message": {"content": [block, "not-dict"]}}}
        assert _event_blocks(event) == [block]  # non-dict item filtered

    def test_data_content_source_when_no_message(self):
        block = {"type": "tool_result"}
        assert _event_blocks({"data": {"content": [block]}}) == [block]

    def test_non_list_content_returns_empty(self):
        assert _event_blocks({"data": {"message": {"content": None}}}) == []
        assert _event_blocks({"data": {"content": "nope"}}) == []


# ---------------------------------------------------------------------------
# _canonical_tool_name


class TestCanonicalToolName:
    @pytest.mark.parametrize("name,suffix", [
        ("x__search_requirement_units_rg", "search_requirement_units_rg"),
        ("search_requirement_units_fts", "search_requirement_units_fts"),
        ("p__search_requirement_units_vector", "search_requirement_units_vector"),
        ("q__query_provider_native_transcript_index", "query_provider_native_transcript_index"),
    ])
    def test_matches_search_suffix(self, name, suffix):
        assert _canonical_tool_name(name) == suffix

    def test_no_match_returns_none(self):
        assert _canonical_tool_name("rogue_tool") is None
        assert _canonical_tool_name("") is None


# ---------------------------------------------------------------------------
# _decode_tool_result


class TestDecodeToolResult:
    def test_dict_passthrough(self):
        assert _decode_tool_result({"count": 2}) == {"count": 2}

    def test_list_of_text_joined_and_parsed(self):
        assert _decode_tool_result([{"text": '{"a":'}, {"text": "1}"}]) == {"a": 1}

    def test_empty_list_returns_none(self):
        assert _decode_tool_result([]) is None

    def test_list_without_text_items_returns_none(self):
        assert _decode_tool_result([{"type": "image"}]) is None

    def test_non_str_scalar_returns_none(self):
        assert _decode_tool_result(42) is None
        assert _decode_tool_result(None) is None

    def test_valid_json_dict_string(self):
        assert _decode_tool_result('{"k": 1}') == {"k": 1}

    def test_invalid_json_string_returns_none(self):
        assert _decode_tool_result("not json") is None

    def test_valid_json_non_dict_string_returns_none(self):
        assert _decode_tool_result("[1, 2]") is None
        assert _decode_tool_result("5") is None


# ---------------------------------------------------------------------------
# numeric + unique helpers


class TestOptionalNumber:
    def test_int_and_float_coerced_to_float(self):
        assert _optional_number(3) == 3.0
        assert _optional_number(2.5) == 2.5

    def test_bool_string_none_rejected(self):
        assert _optional_number(True) is None
        assert _optional_number(False) is None
        assert _optional_number("3") is None
        assert _optional_number(None) is None


class TestOptionalInt:
    def test_int_kept(self):
        assert _optional_int(7) == 7

    def test_bool_float_string_rejected(self):
        assert _optional_int(True) is None
        assert _optional_int(2.0) is None
        assert _optional_int("7") is None
        assert _optional_int(None) is None


class TestUnique:
    def test_order_preserving_dedupe_with_falsy_filter(self):
        assert _unique(["a", "b", "a", "", None, "b", 0]) == ["a", "b"]


# ---------------------------------------------------------------------------
# surface


class TestSurface:
    def test_search_tools_constant(self):
        assert SEARCH_TOOLS == (
            "search_requirement_units_rg",
            "search_requirement_units_fts",
            "search_requirement_units_vector",
            "query_provider_native_transcript_index",
        )
