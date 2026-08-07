from __future__ import annotations

import os
import sys
import types

import pytest

import _test_home

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_test_home.isolate("bc-test-provider-catalog-mcp-unit-")

import provider_catalog_mcp as mod  # noqa: E402
from provider_catalog_mcp import (  # noqa: E402
    _fuzzy_matches,
    _matching_values,
    _normalized,
    _text,
    available_provider_models_response,
)


def _record(
    rid: str,
    name: str = "",
    kind: str = "",
    nickname: str | None = None,
    suspended: bool = False,
) -> dict:
    return {"id": rid, "name": name, "kind": kind, "nickname": nickname, "suspended": suspended}


def _defaults(runner: str = "native", model: str = "m-default", effort: str = "medium") -> dict:
    return {"runner": runner, "default_model": model, "default_reasoning_effort": effort}


def _all_efforts(_record, _runner, model=None):
    return ["low", "medium", "high"]


def _install(
    monkeypatch,
    *,
    providers: list[dict],
    default_id: str = "p1",
    runners: dict[str, list[str]] | None = None,
    models: dict[str, list[str]] | None = None,
    efforts,
    defaults: dict[str, dict] | None = None,
) -> None:
    runners = runners or {}
    models = models or {}
    defaults = defaults or {}

    store = types.SimpleNamespace(
        list_providers=lambda: {"providers": providers, "default_provider_id": default_id},
        provider_execution_defaults=lambda pid: defaults.get(pid, _defaults()),
    )
    runtime_profile = types.SimpleNamespace(
        supported_runners=lambda rec: runners.get(rec["id"], ["native"]),
        reasoning_efforts=efforts,
    )
    models_mod = types.SimpleNamespace(
        available_models=lambda pid: models.get(pid, ["m-default"]),
    )
    monkeypatch.setattr(mod, "config_store", store)
    monkeypatch.setattr(mod, "runtime_profile", runtime_profile)
    monkeypatch.setattr(mod, "models_mod", models_mod)


@pytest.fixture
def install(monkeypatch):
    def _apply(**kwargs):
        _install(monkeypatch, **kwargs)

    return _apply


# --- helpers ---------------------------------------------------------------


def test_text_strips_and_falsies_to_empty() -> None:
    assert _text("   Sonnet  ") == "Sonnet"
    assert _text(None) == ""
    assert _text(0) == ""
    assert _text([]) == ""


def test_normalized_lowercases_and_drops_non_alnum() -> None:
    assert _normalized("Router Lab!") == "routerlab"
    assert _normalized("C++ 4.5") == "c45"
    assert _normalized(None) == ""
    assert _normalized("   ") == ""


def test_fuzzy_matches_empty_needle_matches_all() -> None:
    assert _fuzzy_matches("", ["anything"]) is True
    assert _fuzzy_matches("   ", ["anything"]) is True


def test_fuzzy_matches_no_candidates_is_false() -> None:
    assert _fuzzy_matches("x", []) is False


def test_fuzzy_matches_empty_candidates_skipped_then_no_match_false() -> None:
    assert _fuzzy_matches("x", ["", None, "   "]) is False


def test_fuzzy_matches_needle_in_haystack() -> None:
    assert _fuzzy_matches("son", ["sonnet"]) is True


def test_fuzzy_matches_haystack_in_needle() -> None:
    assert _fuzzy_matches("sonnet45", ["sonnet"]) is True


def test_fuzzy_matches_ratio_above_threshold() -> None:
    assert _fuzzy_matches("cluade", ["claude"]) is True


def test_fuzzy_matches_no_match_false() -> None:
    assert _fuzzy_matches("zzz", ["abc"]) is False


def test_fuzzy_matches_skips_empty_then_matches() -> None:
    assert _fuzzy_matches("p1", ["", None, "p1"]) is True


def test_matching_values_empty_query_returns_all() -> None:
    values = ["sonnet", "haiku"]
    assert _matching_values(values, "") is values
    assert _matching_values(values, "   ") is values


def test_matching_values_filters_by_fuzzy() -> None:
    assert _matching_values(["sonnet", "haiku"], "son") == ["sonnet"]
    assert _matching_values(["sonnet", "haiku"], "zzz") == []


# --- response: happy path / assembly --------------------------------------


def test_assembles_all_non_suspended_providers_with_full_structure(install) -> None:
    install(
        providers=[
            _record("p1", name="Claude", kind="claude", nickname="Personal"),
            _record("p2", name="Codex", kind="openai", nickname=""),
        ],
        default_id="p1",
        runners={"p1": ["native"], "p2": ["native"]},
        models={"p1": ["sonnet", "haiku"], "p2": ["gpt-5"]},
        efforts=_all_efforts,
        defaults={"p1": _defaults(runner="native", model="sonnet", effort="high")},
    )

    result = available_provider_models_response()

    assert result["success"] is True
    assert result["count"] == 2
    assert result["filters"] == {
        "provider": "",
        "model": "",
        "reasoning_effort": "",
        "runner": "",
    }

    by_id = {p["provider_id"]: p for p in result["providers"]}
    p1 = by_id["p1"]
    assert p1["name"] == "Claude"
    assert p1["nickname"] == "Personal"
    assert p1["kind"] == "claude"
    assert p1["is_default"] is True
    assert p1["runner"] == "native"
    assert p1["default_model"] == "sonnet"
    assert p1["default_reasoning_effort"] == "high"
    assert [rp["runner"] for rp in p1["runtime_profiles"]] == ["native"]
    profiles = p1["runtime_profiles"][0]["model_profiles"]
    assert {mp["model"] for mp in profiles} == {"sonnet", "haiku"}
    assert all(mp["reasoning_efforts"] == ["low", "medium", "high"] for mp in profiles)

    assert by_id["p2"]["is_default"] is False
    assert by_id["p2"]["nickname"] == ""
    assert by_id["p2"]["default_model"] == "m-default"


# --- response: negative filter branches -----------------------------------


def test_suspended_provider_excluded(install) -> None:
    install(providers=[_record("p1", name="Ghost", suspended=True)], models={"p1": ["m1"]}, efforts=_all_efforts)
    assert available_provider_models_response()["count"] == 0


def test_provider_fuzzy_filter_rejects_non_match(install) -> None:
    install(providers=[_record("p1", name="Claude", kind="claude")], models={"p1": ["m1"]}, efforts=_all_efforts)
    assert available_provider_models_response(provider="zzznomatch")["count"] == 0


def test_runner_filter_rejects_provider_with_no_matching_runner(install) -> None:
    install(
        providers=[_record("p1", name="Claude", kind="claude")],
        runners={"p1": ["native"]},
        models={"p1": ["m1"]},
        efforts=_all_efforts,
    )
    assert available_provider_models_response(runner="zzznomatch")["count"] == 0


def test_model_filter_rejects_provider_with_no_matching_model(install) -> None:
    install(
        providers=[_record("p1", name="Claude", kind="claude")],
        models={"p1": ["sonnet"]},
        efforts=_all_efforts,
    )
    result = available_provider_models_response(model="zzznomatch")
    assert result["count"] == 0
    assert result["filters"]["model"] == "zzznomatch"


def test_runner_filter_skips_non_matching_runner_keeps_matching(install) -> None:
    install(
        providers=[_record("p1", name="Claude", kind="claude")],
        runners={"p1": ["native", "better_agent_runner"]},
        models={"p1": ["m1"]},
        efforts=_all_efforts,
    )
    result = available_provider_models_response(runner="better agent")
    provider = result["providers"][0]
    assert [rp["runner"] for rp in provider["runtime_profiles"]] == ["better_agent_runner"]


def test_effort_filter_skips_non_matching_model_keeps_matching(install) -> None:
    def efforts(_rec, _runner, model=None):
        return {"m1": ["high"], "m2": ["low"]}.get(model, [])

    install(
        providers=[_record("p1", name="Claude", kind="claude")],
        runners={"p1": ["native"]},
        models={"p1": ["m1", "m2"]},
        efforts=efforts,
    )
    result = available_provider_models_response(reasoning_effort="high")
    profiles = result["providers"][0]["runtime_profiles"][0]["model_profiles"]
    assert [mp["model"] for mp in profiles] == ["m1"]
    assert profiles[0]["reasoning_efforts"] == ["high"]


def test_effort_filter_rejects_provider_with_no_matching_effort(install) -> None:
    def efforts(_rec, _runner, model=None):
        return {"m1": ["low"]}.get(model, [])

    install(
        providers=[_record("p1", name="Claude", kind="claude")],
        runners={"p1": ["native"]},
        models={"p1": ["m1"]},
        efforts=efforts,
    )
    assert available_provider_models_response(reasoning_effort="high")["count"] == 0


# --- response: provider match modes ---------------------------------------


def test_provider_match_via_name_substring(install) -> None:
    install(
        providers=[
            _record("p1", name="Sonnet-Provider", kind="claude"),
            _record("p2", name="Other", kind="openai"),
        ],
        models={"p1": ["m1"], "p2": ["m2"]},
        efforts=_all_efforts,
    )
    result = available_provider_models_response(provider="sonnet")
    assert [p["provider_id"] for p in result["providers"]] == ["p1"]


def test_provider_match_via_fuzzy_ratio_skips_empty_candidates(install) -> None:
    install(
        providers=[
            _record("p1", name="Claude", kind="", nickname=None),
            _record("p2", name="Codex", kind="openai"),
        ],
        models={"p1": ["m1"], "p2": ["m2"]},
        efforts=_all_efforts,
    )
    result = available_provider_models_response(provider="cluade")
    assert [p["provider_id"] for p in result["providers"]] == ["p1"]


def test_filters_echo_stripped_queries(install) -> None:
    install(
        providers=[_record("p1", name="Claude", kind="claude")],
        runners={"p1": ["native"]},
        models={"p1": ["sonnet"]},
        efforts=_all_efforts,
    )
    result = available_provider_models_response(
        provider="  claude ", model=" sonnet ", reasoning_effort=" high ", runner=" native ",
    )
    assert result["filters"] == {
        "provider": "claude",
        "model": "sonnet",
        "reasoning_effort": "high",
        "runner": "native",
    }
    assert result["count"] == 1
