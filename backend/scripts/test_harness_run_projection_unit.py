"""Unit tests for ``harness_run_projection``.

Pure-logic projection helpers that fold a resolved harness-run config
snapshot into provider-launch inputs, and read back launcher projections.
No ``ba_home`` dependency at import time.

Pins:
  1. ``_merge_names``: skips non-list values, coerces falsy items to ``""``,
     strips, drops empties and duplicates, preserves first-seen order, and
     stringifies non-str items.
  2. ``apply_to_inputs``: returns the input object unchanged when the
     snapshot is inactive; otherwise projects ``bare_config``,
     ``capability_contexts``, the merged provider-run config, and the five
     merged name-lists from snapshot+inputs.
  3. ``is_active``: truthy only for a dict carrying a truthy ``profile_id``.
  4. ``launcher_projection``: ``{}`` when the snapshot is not a dict or holds
     no dict projection; otherwise an independent deep copy.
  5. ``selected_mcp_servers``: empty set for every degenerate shape, and a
     stripped/deduped set for a real per-extension server list.
  6. ``selected_extension_ids``: empty set for degenerate shapes, and a
     stripped set of extension-revision keys otherwise.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_harness_run_projection_unit.py -v
"""

from __future__ import annotations

import os
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-harness-run-projection-unit-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from harness_run_projection import (  # noqa: E402
    _merge_names,
    apply_to_inputs,
    is_active,
    launcher_projection,
    selected_extension_ids,
    selected_mcp_servers,
)


# --- _merge_names -------------------------------------------------------


def test_merge_names_skips_non_list_values():
    # None / str / int / object are silently skipped; only lists contribute.
    assert _merge_names(None, "x", 5, object(), ["a"]) == ["a"]


def test_merge_names_empty_inputs():
    assert _merge_names() == []
    assert _merge_names(None, []) == []


def test_merge_names_strips_and_dedupes_preserving_order():
    assert _merge_names(["  a ", "b"], ["a", " c ", "b", "d"]) == ["a", "b", "c", "d"]


def test_merge_names_falsy_items_become_empty_and_are_dropped():
    # `item or ""` -> None/0/False all coerce to "" and are dropped after strip.
    assert _merge_names([None, 0, False, "x"]) == ["x"]


def test_merge_names_stringifies_non_str_items():
    assert _merge_names([1, 2, 3]) == ["1", "2", "3"]


# --- is_active ----------------------------------------------------------


@pytest.mark.parametrize("snapshot", [None, "x", 5, [], object(), {}, {"profile_id": ""}])
def test_is_active_false(snapshot):
    assert is_active(snapshot) is False


def test_is_active_true():
    assert is_active({"profile_id": "prof-1"}) is True


# --- apply_to_inputs ----------------------------------------------------


def test_apply_to_inputs_inactive_returns_same_object():
    # No resolved snapshot -> not active -> inputs returned unchanged (identity).
    inputs = {"provider_run_config": {"x": 1}}
    assert apply_to_inputs(inputs) is inputs

    # Snapshot present but inactive (no profile_id) -> still identity passthrough.
    inactive = {"resolved_harness_run_config": {"bare_config": True}}
    assert apply_to_inputs(inactive) is inactive


def _active_snapshot() -> dict:
    return {
        "profile_id": "prof-1",
        "bare_config": "yes",  # truthy -> bool True
        "capability_contexts": ["ctx-a"],
        "provider_run_config": {"model": "snap-model", "skills": {"s1": {}}},
        "extra_mcp_servers": ["snap-mcp"],
        "active_capability_ids": ["snap-cap"],
        "disabled_builtin_extensions": ["snap-ext"],
        "disabled_builtin_tools": ["snap-tool"],
        "disabled_runtime_skills": ["snap-skill"],
    }


def test_apply_to_inputs_active_projects_all_fields():
    inputs = {
        "resolved_harness_run_config": _active_snapshot(),
        "provider_run_config": {"model": "in-model", "skills": {"s2": {}}},
        "extra_mcp_servers": ["in-mcp"],
        "active_capability_ids": ["in-cap"],
        "disabled_builtin_extensions": ["in-ext"],
        "disabled_builtin_tools": ["in-tool"],
        "disabled_runtime_skills": ["in-skill"],
        "untouched": "keep-me",
    }
    out = apply_to_inputs(inputs)

    # Original input is not mutated; result is a new dict carrying untouched keys.
    assert out is not inputs
    assert out["untouched"] == "keep-me"

    assert out["bare_config"] is True
    assert out["capability_contexts"] == ["ctx-a"]
    # provider_run_config: override (inputs) wins for scalar keys, skills dicts merge.
    assert out["provider_run_config"]["model"] == "in-model"
    assert out["provider_run_config"]["skills"] == {"s1": {}, "s2": {}}
    # Each name-list merges snapshot-first, inputs-second, deduped.
    assert out["extra_mcp_servers"] == ["snap-mcp", "in-mcp"]
    assert out["active_capability_ids"] == ["snap-cap", "in-cap"]
    assert out["disabled_builtin_extensions"] == ["snap-ext", "in-ext"]
    assert out["disabled_builtin_tools"] == ["snap-tool", "in-tool"]
    assert out["disabled_runtime_skills"] == ["snap-skill", "in-skill"]


def test_apply_to_inputs_tolerates_missing_optional_snapshot_fields():
    # Minimal active snapshot: only profile_id. Everything else falls back.
    inputs = {"resolved_harness_run_config": {"profile_id": "prof-1"}}
    out = apply_to_inputs(inputs)

    assert out["bare_config"] is False
    assert out["capability_contexts"] == []
    assert out["provider_run_config"] == {}
    assert out["extra_mcp_servers"] == []
    assert out["active_capability_ids"] == []
    assert out["disabled_builtin_extensions"] == []
    assert out["disabled_builtin_tools"] == []
    assert out["disabled_runtime_skills"] == []


# --- launcher_projection ------------------------------------------------


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"resolved_harness_run_config": "nope"},
        {"resolved_harness_run_config": {}},
        {"resolved_harness_run_config": {"launcher_projection": None}},
        {"resolved_harness_run_config": {"launcher_projection": "nope"}},
        {"resolved_harness_run_config": {"launcher_projection": {}}},
    ],
)
def test_launcher_projection_empty(inputs):
    assert launcher_projection(inputs) == {}


def test_launcher_projection_returns_independent_deepcopy():
    projection = {"extension_mcp_servers": {"ext": ["a"]}}
    inputs = {"resolved_harness_run_config": {"launcher_projection": projection}}
    out = launcher_projection(inputs)
    assert out == projection
    # Mutating the result must not touch the source snapshot.
    out["extension_mcp_servers"]["ext"].append("b")
    assert projection["extension_mcp_servers"]["ext"] == ["a"]


# --- selected_mcp_servers -----------------------------------------------


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"resolved_harness_run_config": {}},
        {"resolved_harness_run_config": {"launcher_projection": {"extension_mcp_servers": "nope"}}},
        {
            "resolved_harness_run_config": {
                "launcher_projection": {"extension_mcp_servers": {"ext": "nope"}}
            }
        },
    ],
)
def test_selected_mcp_servers_empty(inputs):
    assert selected_mcp_servers(inputs, "ext") == set()


def test_selected_mcp_servers_strips_and_filters():
    inputs = {
        "resolved_harness_run_config": {
            "launcher_projection": {
                "extension_mcp_servers": {"ext": ["  a ", "", None, "a", "b"]},
            }
        }
    }
    assert selected_mcp_servers(inputs, "ext") == {"a", "b"}
    # Unknown extension id -> empty.
    assert selected_mcp_servers(inputs, "missing") == set()


# --- selected_extension_ids ---------------------------------------------


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"resolved_harness_run_config": {"launcher_projection": {"extension_revisions": "nope"}}},
    ],
)
def test_selected_extension_ids_empty(inputs):
    assert selected_extension_ids(inputs) == set()


def test_selected_extension_ids_strips_keys():
    inputs = {
        "resolved_harness_run_config": {
            "launcher_projection": {
                "extension_revisions": {" ext-1 ": {}, "ext-2": {}, "": {}},
            }
        }
    }
    assert selected_extension_ids(inputs) == {"ext-1", "ext-2"}
