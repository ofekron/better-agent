from __future__ import annotations

import os
import sys

import pytest

import _test_home

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_test_home.isolate("bc-test-extension-run-policy-unit-")

import capability_contexts  # noqa: E402
import config_store  # noqa: E402
import harness_profile_resolver  # noqa: E402
from extension_run_policy import (  # noqa: E402
    _effective_profile_record,
    _merge_names,
    _provider_capability_contexts,
    disabled_builtin_extensions_for_run,
    disabled_builtin_tools_for_run,
    disabled_runtime_skills_for_run,
    extra_mcp_servers_for_run,
    normalize_disabled_builtin_extensions,
    normalize_extra_mcp_servers,
    resolve_extension_run_policy,
)


@pytest.fixture
def globals_(monkeypatch):
    """Pin config_store global defaults so record-only logic is deterministic."""
    monkeypatch.setattr(config_store, "get_disabled_builtin_tools", lambda: ["mssg"])
    monkeypatch.setattr(
        config_store, "get_disabled_builtin_extensions", lambda: ["global.ext"]
    )
    return None


# --- normalize_disabled_builtin_extensions ---------------------------------


def test_normalize_disabled_builtin_extensions_none_returns_none():
    assert normalize_disabled_builtin_extensions(None) is None


@pytest.mark.parametrize("value", [123, "a,b", {"x": 1}])
def test_normalize_disabled_builtin_extensions_non_list_returns_none(value):
    assert normalize_disabled_builtin_extensions(value) is None


def test_normalize_disabled_builtin_extensions_dedup_strip_filter():
    # order-preserving dedup, per-item strip, falsy/blank dropped
    assert normalize_disabled_builtin_extensions(["a", " a ", "", "  ", "b", "a"]) == [
        "a",
        "b",
    ]


def test_normalize_disabled_builtin_extensions_empty_list():
    assert normalize_disabled_builtin_extensions([]) == []


# --- normalize_extra_mcp_servers -------------------------------------------


@pytest.mark.parametrize("value", [None, "server", 5, {"x": 1}])
def test_normalize_extra_mcp_servers_non_list_returns_empty(value):
    assert normalize_extra_mcp_servers(value) == []


def test_normalize_extra_mcp_servers_dedup_strip_filter():
    assert normalize_extra_mcp_servers(["s1", " s1 ", "", "s2"]) == ["s1", "s2"]


# --- extra_mcp_servers_for_run ---------------------------------------------


def test_extra_mcp_servers_worker_wins():
    out = extra_mcp_servers_for_run(
        session_record={"extra_mcp_servers": ["session-srv"]},
        worker_record={"extra_mcp_servers": ["worker-srv", "shared"]},
    )
    assert out == ["worker-srv", "shared"]


def test_extra_mcp_servers_session_when_worker_missing():
    out = extra_mcp_servers_for_run(
        session_record={"extra_mcp_servers": ["session-srv"]},
        worker_record=None,
    )
    assert out == ["session-srv"]


def test_extra_mcp_servers_falsy_worker_falls_through_to_session():
    # worker record present but key absent/falsy -> session path
    out = extra_mcp_servers_for_run(
        session_record={"extra_mcp_servers": ["session-srv"]},
        worker_record={},
    )
    assert out == ["session-srv"]


def test_extra_mcp_servers_neither_returns_empty():
    assert (
        extra_mcp_servers_for_run(session_record={}, worker_record={}) == []
    )


# --- disabled_builtin_tools_for_run ----------------------------------------


def test_disabled_builtin_tools_global_only(globals_):
    assert disabled_builtin_tools_for_run(session_record={}, worker_record=None) == [
        "mssg"
    ]


def test_disabled_builtin_tools_record_filtered_by_disableable_set(globals_):
    # only entries in DISABLEABLE_BUILTIN_TOOLS survive; bogus dropped
    out = disabled_builtin_tools_for_run(
        session_record={"disabled_builtin_tools": ["ask", "bogus", "delegate_task"]},
        worker_record=None,
    )
    assert out == ["ask", "delegate_task", "mssg"]


def test_disabled_builtin_tools_raw_not_list_skipped(globals_):
    out = disabled_builtin_tools_for_run(
        session_record={"disabled_builtin_tools": "ask"},
        worker_record=None,
    )
    assert out == ["mssg"]


def test_disabled_builtin_tools_worker_union(globals_):
    out = disabled_builtin_tools_for_run(
        session_record={"disabled_builtin_tools": ["ask"]},
        worker_record={"disabled_builtin_tools": ["create_session", "ask"]},
    )
    assert out == ["ask", "create_session", "mssg"]


def test_disabled_builtin_tools_worker_not_dict_skipped(globals_):
    # worker_record not a dict -> continue; only session contributes
    out = disabled_builtin_tools_for_run(
        session_record={"disabled_builtin_tools": ["ask"]},
        worker_record="nope",
    )
    assert out == ["ask", "mssg"]


# --- disabled_runtime_skills_for_run ---------------------------------------


def test_disabled_runtime_skills_worker_session_union():
    out = disabled_runtime_skills_for_run(
        session_record={"disabled_runtime_skills": ["a", "b"]},
        worker_record={"disabled_runtime_skills": ["b", "c"]},
    )
    assert out == ["a", "b", "c"]


def test_disabled_runtime_skills_worker_none():
    out = disabled_runtime_skills_for_run(
        session_record={"disabled_runtime_skills": ["a"]},
        worker_record=None,
    )
    assert out == ["a"]


def test_disabled_runtime_skills_raw_not_list_skipped():
    assert (
        disabled_runtime_skills_for_run(
            session_record={"disabled_runtime_skills": "x"},
            worker_record=None,
        )
        == []
    )


def test_disabled_runtime_skills_wildcard_and_blank_filtered():
    out = disabled_runtime_skills_for_run(
        session_record={"disabled_runtime_skills": ["*", "", "  ", "real"]},
        worker_record=None,
    )
    assert out == ["*", "real"]


# --- disabled_builtin_extensions_for_run -----------------------------------


def test_disabled_extensions_explicit_list_wins(globals_):
    assert disabled_builtin_extensions_for_run(
        ["turn.ext"],
        session_record={"disabled_builtin_extensions": ["session.ext"]},
        worker_record={"disabled_builtin_extensions": ["worker.ext"]},
    ) == ["turn.ext"]


def test_disabled_extensions_explicit_empty_clears(globals_):
    assert (
        disabled_builtin_extensions_for_run(
            [],
            session_record={"disabled_builtin_extensions": ["session.ext"]},
            worker_record={"disabled_builtin_extensions": ["worker.ext"]},
        )
        == []
    )


def test_disabled_extensions_explicit_normalizes(globals_):
    # explicit list goes through normalize: dedup/strip/filter
    assert disabled_builtin_extensions_for_run(
        ["a", " a ", "", "b"],
        session_record={},
        worker_record={},
    ) == ["a", "b"]


def test_disabled_extensions_worker_wins_when_no_explicit(globals_):
    assert disabled_builtin_extensions_for_run(
        None,
        session_record={"disabled_builtin_extensions": ["session.ext"]},
        worker_record={"disabled_builtin_extensions": ["worker.ext"]},
    ) == ["worker.ext"]


def test_disabled_extensions_worker_explicit_none_falls_to_session(globals_):
    # worker has the key but it is None -> skip, session wins
    assert disabled_builtin_extensions_for_run(
        None,
        session_record={"disabled_builtin_extensions": ["session.ext"]},
        worker_record={"disabled_builtin_extensions": None},
    ) == ["session.ext"]


def test_disabled_extensions_empty_record_list_clears(globals_):
    assert (
        disabled_builtin_extensions_for_run(
            None,
            session_record={"disabled_builtin_extensions": []},
            worker_record={},
        )
        == []
    )


def test_disabled_extensions_global_default(globals_):
    assert disabled_builtin_extensions_for_run(
        None,
        session_record={},
        worker_record={},
    ) == ["global.ext"]


# --- _merge_names ----------------------------------------------------------


def test_merge_names_skips_non_list_dedups_orders():
    # non-list arg skipped, blanks dropped, dups collapsed, order preserved
    assert _merge_names(["a", " b ", "", "a"], "nope", ["b", "c"]) == ["a", "b", "c"]


def test_merge_names_all_non_list_empty():
    assert _merge_names(None, 5, "x") == []


# --- _effective_profile_record ---------------------------------------------


def test_effective_profile_worker_truthy_wins():
    rec = {"id": "worker"}
    assert _effective_profile_record({"id": "session"}, rec) is rec


def test_effective_profile_empty_worker_falls_to_session():
    sess = {"id": "session"}
    assert _effective_profile_record(sess, {}) is sess


def test_effective_profile_none_worker_uses_session():
    sess = {"id": "session"}
    assert _effective_profile_record(sess, None) is sess


def test_effective_profile_both_unusable_returns_empty():
    assert _effective_profile_record(None, None) == {}
    assert _effective_profile_record("x", 5) == {}


# --- _provider_capability_contexts -----------------------------------------


def test_capability_contexts_non_list_returns_empty():
    assert _provider_capability_contexts(None, "codex") == []
    assert _provider_capability_contexts("x", "codex") == []


def test_capability_contexts_no_outputs_returns_deepcopy():
    value = [{"name": "x"}, {"outputs": "not-a-list"}]
    out = _provider_capability_contexts(value, "codex")
    assert out == value
    assert out is not value
    assert out[0] is not value[0]


def test_capability_contexts_with_outputs_delegates(monkeypatch):
    captured = {}

    def spy(contexts, provider_kind):
        captured["contexts"] = contexts
        captured["provider_kind"] = provider_kind
        return [{"projected": True}]

    monkeypatch.setattr(capability_contexts, "provider_capability_contexts", spy)
    value = [{"name": "x", "outputs": [{"provider_kind": "codex", "content": "c"}]}]
    out = _provider_capability_contexts(value, "codex")
    assert out == [{"projected": True}]
    assert captured["contexts"] is value
    assert captured["provider_kind"] == "codex"


# --- resolve_extension_run_policy ------------------------------------------


_BASE_SNAPSHOT = {
    "profile_id": "p1",
    "bare_config": False,
    "capability_contexts": [{"name": "no-outputs"}],
    "provider_run_config": {"skills": {"a": 1}},
    "extra_mcp_servers": ["snap-server"],
    "active_capability_ids": ["snap-cap"],
    "disabled_builtin_extensions": ["snap.ext"],
    "disabled_builtin_tools": ["snap-tool"],
    "disabled_runtime_skills": ["snap.skill"],
    "launcher_projection": {"extension_mcp_servers": {}},
}


def test_resolve_snapshot_supplied_merges_supplied_contexts(monkeypatch, globals_):
    delegated = {}

    def spy(contexts, provider_kind):
        delegated["called"] = (contexts, provider_kind)
        return [{"name": "projected-cap", "content": "c"}]

    monkeypatch.setattr(capability_contexts, "provider_capability_contexts", spy)

    policy = resolve_extension_run_policy(
        resolved_harness_run_config=dict(_BASE_SNAPSHOT),
        session_record={
            "extra_mcp_servers": ["session-srv"],
            "active_capability_ids": ["session-cap"],
            "disabled_builtin_tools": ["ask"],
            "disabled_runtime_skills": ["session.skill"],
        },
        worker_record={
            "disabled_builtin_tools": ["delegate_task"],
            "disabled_runtime_skills": ["worker.skill"],
        },
        provider_kind="codex",
        provider_run_config={"fork_parent_line_count": 9, "skills": {"b": 2}},
        capability_contexts=[{"name": "ctx", "outputs": [{"provider_kind": "codex", "content": "c"}]}],
        disabled_builtin_extensions=["explicit.ext"],
    )

    # supplied (non-empty) contexts win over snapshot because snapshot_supplied
    assert delegated["called"][1] == "codex"
    assert policy["capability_contexts"] == [{"name": "projected-cap", "content": "c"}]
    # snapshot disabled_builtin_extensions is a list -> True branch merges snapshot + explicit
    assert policy["disabled_builtin_extensions"] == ["snap.ext", "explicit.ext"]
    # mcp servers merge snapshot + explicit opt-in
    assert policy["extra_mcp_servers"] == ["snap-server", "session-srv"]
    # tools merge snapshot + record opt-ins (ask/delegate in DISABLEABLE set) + global mssg
    assert set(policy["disabled_builtin_tools"]) == {
        "snap-tool",
        "ask",
        "delegate_task",
        "mssg",
    }
    # runtime skills merge snapshot first, then sorted record opt-ins
    assert policy["disabled_runtime_skills"] == ["snap.skill", "session.skill", "worker.skill"]
    # capability ids merge snapshot + profile_record (worker non-empty wins as profile)
    # provider config deep-merges skills dicts and overrides scalars
    assert policy["provider_run_config"]["fork_parent_line_count"] == 9
    assert policy["provider_run_config"]["skills"] == {"a": 1, "b": 2}
    # launcher projection carries effective restrictions
    lp = policy["resolved_harness_run_config"]["launcher_projection"]
    assert lp["disabled_builtin_extensions"] == ["snap.ext", "explicit.ext"]
    assert lp["active_capability_ids"] == policy["active_capability_ids"]
    # snapshot is returned under resolved_harness_run_config and mutated in place
    snap = policy["resolved_harness_run_config"]
    assert snap["extra_mcp_servers"] == ["snap-server", "session-srv"]
    assert snap["launcher_projection"] is lp


def test_resolve_snapshot_supplied_empty_contexts_falls_to_snapshot(monkeypatch, globals_):
    spy_called = {"n": 0}

    def spy(contexts, provider_kind):  # pragma: no cover
        spy_called["n"] += 1
        return []

    monkeypatch.setattr(capability_contexts, "provider_capability_contexts", spy)

    snapshot = dict(_BASE_SNAPSHOT)
    snapshot["disabled_builtin_extensions"] = "not-a-list"  # -> False branch
    policy = resolve_extension_run_policy(
        resolved_harness_run_config=snapshot,
        session_record={"disabled_builtin_extensions": ["session.ext"]},
        worker_record={},
        provider_kind="codex",
        provider_run_config={},
        capability_contexts=[],  # falsy -> falls to snapshot.get
        disabled_builtin_extensions=["explicit.ext"],
    )
    # empty supplied contexts -> snapshot capability_contexts used (no outputs -> deepcopy, no spy)
    assert spy_called["n"] == 0
    assert policy["capability_contexts"] == [{"name": "no-outputs"}]
    # False branch: snapshot_disabled not list -> disabled_builtin_extensions_for_run(explicit,...)
    assert policy["disabled_builtin_extensions"] == ["explicit.ext"]


def test_resolve_snapshot_none_resolves_profile(monkeypatch, globals_):
    resolved = {}

    def fake_resolve(record, *, turn_capability_contexts=None):
        resolved["record"] = record
        resolved["ctx"] = turn_capability_contexts
        return {
            "profile_id": "resolved",
            "bare_config": True,
            "capability_contexts": [],
            "provider_run_config": {},
            "extra_mcp_servers": [],
            "active_capability_ids": ["resolved-cap"],
            "disabled_builtin_extensions": [],
            "disabled_builtin_tools": [],
            "disabled_runtime_skills": [],
            "launcher_projection": {},
        }

    monkeypatch.setattr(
        harness_profile_resolver, "resolve_for_session", fake_resolve
    )

    session = {"harness_profile_id": "sp", "active_capability_ids": ["session-cap"]}
    worker = {"harness_profile_id": "wp", "active_capability_ids": ["worker-cap"]}
    policy = resolve_extension_run_policy(
        resolved_harness_run_config=None,
        session_record=session,
        worker_record=worker,
        provider_kind="claude",
        provider_run_config={},
        capability_contexts=None,
        disabled_builtin_extensions=None,
    )
    # worker (truthy) wins as the profile record
    assert resolved["record"] is worker
    assert resolved["ctx"] is None
    # bare_config from resolved snapshot
    assert policy["bare_config"] is True
    # snapshot None branch -> False branch -> disabled_builtin_extensions_for_run(None,...) -> global default
    assert policy["disabled_builtin_extensions"] == ["global.ext"]
    # capability ids merge snapshot (resolved-cap) + profile_record (worker-cap)
    assert policy["active_capability_ids"] == ["resolved-cap", "worker-cap"]
    assert policy["resolved_harness_run_config"]["profile_id"] == "resolved"


def test_resolve_returns_full_keyset(monkeypatch, globals_):
    monkeypatch.setattr(
        capability_contexts, "provider_capability_contexts", lambda c, k: []
    )
    policy = resolve_extension_run_policy(
        resolved_harness_run_config=dict(_BASE_SNAPSHOT),
        session_record={},
        worker_record={},
        provider_kind="codex",
        provider_run_config={},
        capability_contexts=[],
        disabled_builtin_extensions=None,
    )
    assert set(policy) == {
        "bare_config",
        "capability_contexts",
        "provider_run_config",
        "extra_mcp_servers",
        "active_capability_ids",
        "disabled_builtin_extensions",
        "disabled_builtin_tools",
        "disabled_runtime_skills",
        "resolved_harness_run_config",
    }
