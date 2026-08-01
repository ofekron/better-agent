"""Unit tests for provider_validation — pure validation/resolution helpers.

Every dependency (config_store, runtime_profile, models, permission, user_prefs,
extension_store, session_lite, hot_path, auth_routes loopback, provisioning
dispatch, i18n) is monkeypatched so each branch is exercised deterministically
with no disk, no keychain, no real provider. Async helpers run under anyio.
"""
from __future__ import annotations

import os
import sys

import _test_home  # noqa: F401  (engages prod-home guard before backend import)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest

pytestmark = pytest.mark.anyio

import auth_routes  # noqa: E402
import config_store  # noqa: E402
import extension_store  # noqa: E402
import models  # noqa: E402
import permission as permission_mod  # noqa: E402
import provider_validation as pv  # noqa: E402
import runtime_profile  # noqa: E402
import user_prefs  # noqa: E402
from provisioning import dispatch as provisioning_dispatch  # noqa: E402


def _http_status(exc: Exception) -> int:
    return exc.value.status_code


# --------------------------------------------------------------------------- #
# is_loopback_request
# --------------------------------------------------------------------------- #
def test_is_loopback_request_delegates(monkeypatch):
    sentinel = object()
    captured = {}

    def fake(request):
        captured["request"] = request
        return True

    monkeypatch.setattr(auth_routes, "_is_loopback_request", fake)
    assert pv.is_loopback_request(sentinel) is True
    assert captured["request"] is sentinel


def test_is_loopback_request_false(monkeypatch):
    monkeypatch.setattr(auth_routes, "_is_loopback_request", lambda request: False)
    assert pv.is_loopback_request(object()) is False


# --------------------------------------------------------------------------- #
# provider_auth_result_response
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "error",
    ["busy", "binary_missing", "spawn_failed"],
)
def test_provider_auth_result_response_error_codes(error):
    with pytest.raises(Exception) as exc:
        pv.provider_auth_result_response({"ok": False, "error": error})
    assert _http_status(exc) == 409


def test_provider_auth_result_response_generic_not_ok():
    with pytest.raises(Exception) as exc:
        pv.provider_auth_result_response({"ok": False, "error": "whatever"})
    assert _http_status(exc) == 400


def test_provider_auth_result_response_ok_returns_state():
    assert pv.provider_auth_result_response({"ok": True, "state": "ready"}) == {
        "login_state": "ready"
    }


# --------------------------------------------------------------------------- #
# provider_not_suspended
# --------------------------------------------------------------------------- #
def test_provider_not_suspended_no_id_is_noop():
    # provider_id None -> config_store never consulted.
    raised = False
    try:
        pv.provider_not_suspended(None)
    except Exception:
        raised = True
    assert not raised


def test_provider_not_suspended_passes_when_not_suspended(monkeypatch):
    monkeypatch.setattr(config_store, "provider_suspended", lambda pid: False)
    monkeypatch.setattr(pv, "t", lambda key, **kw: "x")
    assert pv.provider_not_suspended("p") is None


def test_provider_not_suspended_raises_when_suspended(monkeypatch):
    monkeypatch.setattr(config_store, "provider_suspended", lambda pid: True)
    monkeypatch.setattr(pv, "t", lambda key, **kw: f"suspended:{kw.get('action')}")
    with pytest.raises(Exception) as exc:
        pv.provider_not_suspended("p", action="do thing")
    assert _http_status(exc) == 409
    assert "do thing" in exc.value.detail


# --------------------------------------------------------------------------- #
# api_reasoning_effort
# --------------------------------------------------------------------------- #
def test_api_reasoning_effort_none():
    assert pv.api_reasoning_effort(None) is None


def test_api_reasoning_effort_blank_string_returns_empty():
    assert pv.api_reasoning_effort("   ") == ""


def test_api_reasoning_effort_valid_normalizes():
    assert pv.api_reasoning_effort("High") == "high"
    assert pv.api_reasoning_effort("max") == "xhigh"


def test_api_reasoning_effort_invalid_raises():
    with pytest.raises(Exception) as exc:
        pv.api_reasoning_effort("bogus")
    assert _http_status(exc) == 400


def test_api_reasoning_effort_non_string_raises():
    with pytest.raises(Exception) as exc:
        pv.api_reasoning_effort(123)
    assert _http_status(exc) == 400


# --------------------------------------------------------------------------- #
# api_optional_provision_prompt
# --------------------------------------------------------------------------- #
def test_provision_prompt_none():
    assert pv.api_optional_provision_prompt(None) is None


def test_provision_prompt_valid():
    assert pv.api_optional_provision_prompt("hello") == "hello"


@pytest.mark.parametrize("bad", ["", "   ", 5, []])
def test_provision_prompt_invalid(bad):
    with pytest.raises(Exception) as exc:
        pv.api_optional_provision_prompt(bad)
    assert _http_status(exc) == 400


# --------------------------------------------------------------------------- #
# api_optional_pool_affinity_key
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [None, "", "    "])
def test_pool_affinity_key_blank(value):
    assert pv.api_optional_pool_affinity_key(value) == ""


def test_pool_affinity_key_strips():
    assert pv.api_optional_pool_affinity_key("  key  ") == "key"


def test_pool_affinity_key_too_long():
    with pytest.raises(Exception) as exc:
        pv.api_optional_pool_affinity_key("k" * 201)
    assert _http_status(exc) == 400


# --------------------------------------------------------------------------- #
# api_optional_provisioned_tool_profile
# --------------------------------------------------------------------------- #
def test_provisioned_tool_profile_none():
    assert pv.api_optional_provisioned_tool_profile(None) == ""


def test_provisioned_tool_profile_empty_string():
    assert pv.api_optional_provisioned_tool_profile("   ") == ""


def test_provisioned_tool_profile_non_string():
    with pytest.raises(Exception) as exc:
        pv.api_optional_provisioned_tool_profile(5)
    assert _http_status(exc) == 400


def test_provisioned_tool_profile_unsupported():
    with pytest.raises(Exception) as exc:
        pv.api_optional_provisioned_tool_profile("codex_agent")
    assert _http_status(exc) == 400


def test_provisioned_tool_profile_requirements_unauthorized(monkeypatch):
    monkeypatch.setattr(
        pv, "is_authorized_provisioned_tool_profile", lambda body, profile: False
    )
    with pytest.raises(Exception) as exc:
        pv.api_optional_provisioned_tool_profile("requirements_processor", {})
    assert _http_status(exc) == 400


def test_provisioned_tool_profile_requirements_authorized(monkeypatch):
    monkeypatch.setattr(
        pv, "is_authorized_provisioned_tool_profile", lambda body, profile: True
    )
    assert (
        pv.api_optional_provisioned_tool_profile(
            "requirements_processor", {"client_delegation_id": "id"}
        )
        == "requirements_processor"
    )


# --------------------------------------------------------------------------- #
# is_authorized_provisioned_tool_profile
# --------------------------------------------------------------------------- #
def test_authorized_profile_body_not_dict():
    assert pv.is_authorized_provisioned_tool_profile(None, "x") is False
    assert pv.is_authorized_provisioned_tool_profile("nope", "x") is False


def test_authorized_profile_delegation_not_string():
    assert pv.is_authorized_provisioned_tool_profile({"client_delegation_id": 5}, "x") is False


def test_authorized_profile_dispatch_false(monkeypatch):
    monkeypatch.setattr(
        provisioning_dispatch, "is_authorized_tool_profile_dispatch", lambda cid, p: False
    )
    assert (
        pv.is_authorized_provisioned_tool_profile({"client_delegation_id": "id"}, "x")
        is False
    )


def test_authorized_profile_dispatch_true(monkeypatch):
    monkeypatch.setattr(
        provisioning_dispatch, "is_authorized_tool_profile_dispatch", lambda cid, p: True
    )
    assert (
        pv.is_authorized_provisioned_tool_profile({"client_delegation_id": "id"}, "x")
        is True
    )


# --------------------------------------------------------------------------- #
# _resolved_provider_record
# --------------------------------------------------------------------------- #
def test_resolved_provider_record_named_found(monkeypatch):
    rec = {"id": "p", "name": "P"}
    monkeypatch.setattr(config_store, "get_provider", lambda pid: rec)
    assert pv._resolved_provider_record("p") is rec


def test_resolved_provider_record_falls_back_to_default(monkeypatch):
    default = {"id": "d"}
    monkeypatch.setattr(config_store, "get_provider", lambda pid: {"id": "d"} if pid == "d" else None)
    monkeypatch.setattr(config_store, "get_default_provider", lambda: default)
    assert pv._resolved_provider_record(None) == {"id": "d"}
    # named-but-missing also falls back to default
    assert pv._resolved_provider_record("missing") == {"id": "d"}


def test_resolved_provider_record_no_default(monkeypatch):
    monkeypatch.setattr(config_store, "get_provider", lambda pid: None)
    monkeypatch.setattr(config_store, "get_default_provider", lambda: None)
    assert pv._resolved_provider_record("missing") is None
    assert pv._resolved_provider_record(None) is None


# --------------------------------------------------------------------------- #
# provider_reasoning_effort
# --------------------------------------------------------------------------- #
def test_provider_reasoning_effort_none():
    assert pv.provider_reasoning_effort("p", None) is None


def test_provider_reasoning_effort_empty():
    assert pv.provider_reasoning_effort("p", "") == ""


def test_provider_reasoning_effort_supported(monkeypatch):
    monkeypatch.setattr(config_store, "get_provider", lambda pid: {"id": "p", "name": "P"})
    monkeypatch.setattr(
        runtime_profile, "reasoning_efforts", lambda rec, runner, *, model="": ("low", "high")
    )
    assert pv.provider_reasoning_effort("p", "high") == "high"


def test_provider_reasoning_effort_unsupported_uses_name(monkeypatch):
    monkeypatch.setattr(config_store, "get_provider", lambda pid: {"id": "p", "name": "P"})
    monkeypatch.setattr(
        runtime_profile, "reasoning_efforts", lambda rec, runner, *, model="": ("low",)
    )
    with pytest.raises(Exception) as exc:
        pv.provider_reasoning_effort("p", "high")
    assert _http_status(exc) == 400
    assert "P" in exc.value.detail


def test_provider_reasoning_effort_unsupported_falls_back_to_provider_id(monkeypatch):
    monkeypatch.setattr(config_store, "get_provider", lambda pid: None)
    monkeypatch.setattr(config_store, "get_default_provider", lambda: None)
    monkeypatch.setattr(
        runtime_profile, "reasoning_efforts", lambda rec, runner, *, model="": ()
    )
    with pytest.raises(Exception) as exc:
        pv.provider_reasoning_effort("p", "high")
    assert _http_status(exc) == 400
    assert "p does not support" in exc.value.detail


# --------------------------------------------------------------------------- #
# inherited_reasoning_effort
# --------------------------------------------------------------------------- #
def test_inherited_reasoning_effort_delegates(monkeypatch):
    rec = {"id": "p"}
    monkeypatch.setattr(config_store, "get_provider", lambda pid: rec)
    captured = {}

    def fake_fit(record, effort, runner, *, model=""):
        captured.update(record=record, effort=effort, runner=runner, model=model)
        return "fitted"

    monkeypatch.setattr(runtime_profile, "fit_reasoning_effort", fake_fit)
    assert pv.inherited_reasoning_effort("p", "high", runner="native", model="m") == "fitted"
    assert captured == {
        "record": rec,
        "effort": "high",
        "runner": "native",
        "model": "m",
    }


# --------------------------------------------------------------------------- #
# provider_runner
# --------------------------------------------------------------------------- #
def test_provider_runner_unknown(monkeypatch):
    monkeypatch.setattr(config_store, "get_provider", lambda pid: None)
    monkeypatch.setattr(config_store, "get_default_provider", lambda: None)
    with pytest.raises(Exception) as exc:
        pv.provider_runner("p")
    assert _http_status(exc) == 400


def test_provider_runner_resolved(monkeypatch):
    monkeypatch.setattr(config_store, "get_provider", lambda pid: {"id": "p"})
    monkeypatch.setattr(runtime_profile, "resolve_runner", lambda rec, runner: "native")
    assert pv.provider_runner("p", runner="native") == "native"


def test_provider_runner_value_error_mapped(monkeypatch):
    monkeypatch.setattr(config_store, "get_provider", lambda pid: {"id": "p"})
    monkeypatch.setattr(
        runtime_profile, "resolve_runner", lambda rec, runner: (_ for _ in ()).throw(ValueError("bad runner"))
    )
    with pytest.raises(Exception) as exc:
        pv.provider_runner("p")
    assert _http_status(exc) == 400
    assert "bad runner" in exc.value.detail


# --------------------------------------------------------------------------- #
# api_permission
# --------------------------------------------------------------------------- #
def test_api_permission_none():
    assert pv.api_permission(None) is None


def test_api_permission_inherit_empty():
    assert pv.api_permission("") == {}
    assert pv.api_permission({}) == {}


def test_api_permission_non_dict():
    with pytest.raises(Exception) as exc:
        pv.api_permission(["a"])
    assert _http_status(exc) == 400


def test_api_permission_dict_passthrough():
    assert pv.api_permission({"mode": "default"}) == {"mode": "default"}


# --------------------------------------------------------------------------- #
# provider_permission
# --------------------------------------------------------------------------- #
def test_provider_permission_none():
    assert pv.provider_permission("p", None) is None


def test_provider_permission_empty_inherit():
    assert pv.provider_permission("p", {}) == {}


def test_provider_permission_no_options(monkeypatch):
    monkeypatch.setattr(config_store, "get_provider", lambda pid: {"id": "p", "name": "P"})
    monkeypatch.setattr(runtime_profile, "runtime_kind", lambda rec, runner: "claude")
    monkeypatch.setattr(permission_mod, "permission_axes_for_kind", lambda kind: {})
    with pytest.raises(Exception) as exc:
        pv.provider_permission("p", {"mode": "default"})
    assert _http_status(exc) == 400


def test_provider_permission_invalid_axis(monkeypatch):
    monkeypatch.setattr(config_store, "get_provider", lambda pid: {"id": "p", "name": "P"})
    monkeypatch.setattr(runtime_profile, "runtime_kind", lambda rec, runner: "claude")
    monkeypatch.setattr(
        permission_mod, "permission_axes_for_kind", lambda kind: {"mode": ("default", "plan")}
    )
    with pytest.raises(Exception) as exc:
        pv.provider_permission("p", {"mode": "bogus"})
    assert _http_status(exc) == 400


def test_provider_permission_valid_normalizes(monkeypatch):
    monkeypatch.setattr(config_store, "get_provider", lambda pid: {"id": "p", "name": "P"})
    monkeypatch.setattr(runtime_profile, "runtime_kind", lambda rec, runner: "claude")
    monkeypatch.setattr(
        permission_mod, "permission_axes_for_kind", lambda kind: {"mode": ("default", "plan")}
    )
    assert pv.provider_permission("p", {"mode": "plan"}) == {"mode": "plan"}


def test_provider_permission_name_fallback_to_provider_id(monkeypatch):
    monkeypatch.setattr(config_store, "get_provider", lambda pid: None)
    monkeypatch.setattr(config_store, "get_default_provider", lambda: None)
    monkeypatch.setattr(runtime_profile, "runtime_kind", lambda rec, runner: "claude")
    monkeypatch.setattr(permission_mod, "permission_axes_for_kind", lambda kind: {})
    with pytest.raises(Exception) as exc:
        pv.provider_permission("p", {"mode": "default"})
    assert "p has no permission options" in exc.value.detail


# --------------------------------------------------------------------------- #
# provider_for_required_model
# --------------------------------------------------------------------------- #
def test_provider_for_required_model_suspended(monkeypatch):
    monkeypatch.setattr(config_store, "get_provider", lambda pid: None)
    monkeypatch.setattr(config_store, "provider_suspended", lambda pid: True)
    monkeypatch.setattr(pv, "t", lambda key, **kw: "suspended")
    with pytest.raises(Exception) as exc:
        pv.provider_for_required_model("p")
    assert _http_status(exc) == 409


def test_provider_for_required_model_not_found(monkeypatch):
    monkeypatch.setattr(config_store, "get_provider", lambda pid: None)
    monkeypatch.setattr(config_store, "provider_suspended", lambda pid: False)
    with pytest.raises(Exception) as exc:
        pv.provider_for_required_model("p")
    assert _http_status(exc) == 404


def test_provider_for_required_model_no_active(monkeypatch):
    monkeypatch.setattr(config_store, "get_default_provider", lambda: None)
    with pytest.raises(Exception) as exc:
        pv.provider_for_required_model(None)
    assert _http_status(exc) == 400


def test_provider_for_required_model_ok(monkeypatch):
    provider = {"id": "p", "name": "P"}
    monkeypatch.setattr(config_store, "get_provider", lambda pid: provider)
    monkeypatch.setattr(config_store, "provider_suspended", lambda pid: False)
    assert pv.provider_for_required_model("p") is provider


# --------------------------------------------------------------------------- #
# profile_prefill_model
# --------------------------------------------------------------------------- #
def test_profile_prefill_model_no_provider():
    assert pv.profile_prefill_model(None) == ""


def test_profile_prefill_model_last_used(monkeypatch):
    monkeypatch.setattr(
        config_store,
        "provider_execution_defaults",
        lambda pid, runner=None: {
            "runtime_profile_id": "prof",
            "runner": "",
            "default_model": "dm",
            "default_reasoning_effort": "",
        },
    )
    monkeypatch.setattr(user_prefs, "get_last_models", lambda: {"prof": "last-model"})
    assert pv.profile_prefill_model("p") == "last-model"


def test_profile_prefill_model_default_when_no_last(monkeypatch):
    monkeypatch.setattr(
        config_store,
        "provider_execution_defaults",
        lambda pid, runner=None: {
            "runtime_profile_id": "prof",
            "runner": "",
            "default_model": "dm",
            "default_reasoning_effort": "",
        },
    )
    monkeypatch.setattr(user_prefs, "get_last_models", lambda: {})
    assert pv.profile_prefill_model("p") == "dm"


def test_profile_prefill_model_default_when_no_profile(monkeypatch):
    monkeypatch.setattr(
        config_store,
        "provider_execution_defaults",
        lambda pid, runner=None: {
            "runtime_profile_id": None,
            "runner": "",
            "default_model": "dm",
            "default_reasoning_effort": "",
        },
    )
    assert pv.profile_prefill_model("p") == "dm"


# --------------------------------------------------------------------------- #
# required_model_from_body_or_provider
# --------------------------------------------------------------------------- #
def test_required_model_explicit(monkeypatch):
    called = {}

    def fake_validate(pid, model, include_retired=False):
        called["args"] = (pid, model)

    monkeypatch.setattr(pv, "validate_provider_model", fake_validate)
    assert pv.required_model_from_body_or_provider({"model": "m"}, {"id": "p"}) == "m"
    assert called["args"] == ("p", "m")


def test_required_model_default_in_available(monkeypatch):
    monkeypatch.setattr(pv, "profile_prefill_model", lambda pid, runner=None: "dm")
    monkeypatch.setattr(models, "available_models", lambda pid: ["dm", "other"])
    assert pv.required_model_from_body_or_provider({}, {"id": "p", "name": "P"}) == "dm"


def test_required_model_first_available_when_default_absent(monkeypatch):
    monkeypatch.setattr(pv, "profile_prefill_model", lambda pid, runner=None: "dm")
    monkeypatch.setattr(models, "available_models", lambda pid: ["a", "b"])
    assert pv.required_model_from_body_or_provider({}, {"id": "p", "name": "P"}) == "a"


def test_required_model_no_default_raises(monkeypatch):
    monkeypatch.setattr(pv, "profile_prefill_model", lambda pid, runner=None: "")
    monkeypatch.setattr(models, "available_models", lambda pid: ["a"])
    with pytest.raises(Exception) as exc:
        pv.required_model_from_body_or_provider({}, {"id": "p", "name": "P"})
    assert _http_status(exc) == 400


def test_required_model_default_set_but_none_available_raises(monkeypatch):
    monkeypatch.setattr(pv, "profile_prefill_model", lambda pid, runner=None: "dm")
    monkeypatch.setattr(models, "available_models", lambda pid: [])
    with pytest.raises(Exception) as exc:
        pv.required_model_from_body_or_provider({}, {"id": "p", "name": "P"})
    assert _http_status(exc) == 400


def test_required_model_skips_blank_candidates(monkeypatch):
    # default absent from available, available holds only empty/whitespace entries
    # -> loop exhausts without yielding -> 400.
    monkeypatch.setattr(pv, "profile_prefill_model", lambda pid, runner=None: "dm")
    monkeypatch.setattr(models, "available_models", lambda pid: ["", "   "])
    with pytest.raises(Exception) as exc:
        pv.required_model_from_body_or_provider({}, {"id": "p", "name": "P"})
    assert _http_status(exc) == 400


# --------------------------------------------------------------------------- #
# validate_provider_model
# --------------------------------------------------------------------------- #
def test_validate_provider_model_empty_ok():
    pv.validate_provider_model("p", "")


def test_validate_provider_model_present_ok(monkeypatch):
    monkeypatch.setattr(models, "available_models", lambda pid: {"m"})
    pv.validate_provider_model("p", "m")


def test_validate_provider_model_present_ok_with_retired(monkeypatch):
    monkeypatch.setattr(models, "available_models_including_retired", lambda pid: {"m"})
    pv.validate_provider_model("p", "m", include_retired=True)


def test_validate_provider_model_unknown_no_models(monkeypatch):
    monkeypatch.setattr(models, "available_models", lambda pid: set())
    monkeypatch.setattr(config_store, "get_provider", lambda pid: {"name": "P"})
    with pytest.raises(Exception) as exc:
        pv.validate_provider_model("p", "m")
    assert _http_status(exc) == 400
    assert "no known models" in exc.value.detail


def test_validate_provider_model_unknown(monkeypatch):
    monkeypatch.setattr(models, "available_models", lambda pid: {"other"})
    monkeypatch.setattr(config_store, "get_provider", lambda pid: None)
    monkeypatch.setattr(config_store, "get_default_provider", lambda: None)
    with pytest.raises(Exception) as exc:
        pv.validate_provider_model(None, "m")
    assert _http_status(exc) == 400
    assert "does not support" in exc.value.detail


# --------------------------------------------------------------------------- #
# resolve_provider_id_ref (async)
# --------------------------------------------------------------------------- #
async def test_resolve_provider_id_ref_empty():
    assert await pv.resolve_provider_id_ref("") == ""


async def test_resolve_provider_id_ref_value_error(monkeypatch):
    def boom(ref):
        raise ValueError("bad ref")

    monkeypatch.setattr(config_store, "resolve_provider_ref", boom)
    with pytest.raises(Exception) as exc:
        await pv.resolve_provider_id_ref("x")
    assert _http_status(exc) == 400
    assert "bad ref" in exc.value.detail


async def test_resolve_provider_id_ref_not_found(monkeypatch):
    monkeypatch.setattr(config_store, "resolve_provider_ref", lambda ref: None)
    with pytest.raises(Exception) as exc:
        await pv.resolve_provider_id_ref("x")
    assert _http_status(exc) == 400


async def test_resolve_provider_id_ref_ok(monkeypatch):
    monkeypatch.setattr(config_store, "resolve_provider_ref", lambda ref: {"id": "p"})
    assert await pv.resolve_provider_id_ref("x") == "p"


# --------------------------------------------------------------------------- #
# resolve_auto_search_provider_id (async)
# --------------------------------------------------------------------------- #
async def test_resolve_auto_search_any(monkeypatch):
    assert await pv.resolve_auto_search_provider_id({"provider_id": "any"}, "s") == ""
    assert await pv.resolve_auto_search_provider_id({"provider_id": "Any"}, "s") == ""


async def test_resolve_auto_search_requested(monkeypatch):
    async def fake_ref(ref):
        return "resolved"

    monkeypatch.setattr(pv, "resolve_provider_id_ref", fake_ref)
    assert await pv.resolve_auto_search_provider_id({"provider_id": "x"}, "s") == "resolved"


async def test_resolve_auto_search_from_session(monkeypatch):
    async def fake_session(sid):
        return {"provider_id": "sess-p"}

    monkeypatch.setattr(pv, "session_lite", fake_session)
    assert await pv.resolve_auto_search_provider_id({}, "s") == "sess-p"


# --------------------------------------------------------------------------- #
# validate_optional_run_selector (async)
# --------------------------------------------------------------------------- #
async def test_validate_run_selector_noop_when_empty():
    # No provider and no model -> returns immediately, nothing else called.
    await pv.validate_optional_run_selector("s", "", "")


async def test_validate_run_selector_explicit_provider_and_model(monkeypatch):
    calls = {}

    async def fake_hot_path_run(label, fn, *args, **kw):
        calls[label] = (fn.__name__, args)
        return fn(*args, **kw)

    monkeypatch.setattr(pv.hot_path, "run", fake_hot_path_run)
    monkeypatch.setattr(pv, "validate_provider_model", lambda pid, model: None)
    await pv.validate_optional_run_selector("s", "p", "m")
    assert "communication.validate_run_selector.validate_provider_model" in calls


async def test_validate_run_selector_provider_without_model_prefill(monkeypatch):
    async def fake_hot_path_run(label, fn, *args, **kw):
        if "profile_prefill_model" in label:
            return "prefilled"
        if "get_provider" in label:
            return {"name": "P"}
        return fn(*args, **kw)

    monkeypatch.setattr(pv.hot_path, "run", fake_hot_path_run)
    monkeypatch.setattr(pv, "validate_provider_model", lambda pid, model: None)
    await pv.validate_optional_run_selector("s", "p", "")


async def test_validate_run_selector_provider_no_default_model(monkeypatch):
    async def fake_hot_path_run(label, fn, *args, **kw):
        if "profile_prefill_model" in label:
            return ""
        if "get_provider" in label:
            return {"name": "P"}
        return fn(*args, **kw)

    monkeypatch.setattr(pv.hot_path, "run", fake_hot_path_run)
    with pytest.raises(Exception) as exc:
        await pv.validate_optional_run_selector("s", "p", "")
    assert _http_status(exc) == 400
    assert "P" in exc.value.detail


async def test_validate_run_selector_model_only_resolves_provider_from_sender(monkeypatch):
    calls = {}

    async def fake_hot_path_run(label, fn, *args, **kw):
        calls[label] = args
        return fn(*args, **kw)

    async def fake_session(sid):
        return {"provider_id": "sess-p"}

    monkeypatch.setattr(pv.hot_path, "run", fake_hot_path_run)
    monkeypatch.setattr(pv, "session_lite", fake_session)
    monkeypatch.setattr(pv, "validate_provider_model", lambda pid, model: None)
    await pv.validate_optional_run_selector("s", "", "m")
    validate_args = calls[
        "communication.validate_run_selector.validate_provider_model"
    ]
    assert validate_args == ("sess-p", "m")


# --------------------------------------------------------------------------- #
# validate_provider_default_reasoning_effort
# --------------------------------------------------------------------------- #
def test_validate_default_reasoning_effort_none():
    assert pv.validate_provider_default_reasoning_effort({"name": "P"}, None) == ""


def test_validate_default_reasoning_effort_empty():
    assert pv.validate_provider_default_reasoning_effort({"name": "P"}, "") == ""


def test_validate_default_reasoning_effort_supported(monkeypatch):
    monkeypatch.setattr(
        config_store, "reasoning_effort_options_for_provider", lambda rec: ["high"]
    )
    assert (
        pv.validate_provider_default_reasoning_effort({"name": "P"}, "high") == "high"
    )


def test_validate_default_reasoning_effort_unsupported(monkeypatch):
    monkeypatch.setattr(
        config_store, "reasoning_effort_options_for_provider", lambda rec: ["low"]
    )
    with pytest.raises(Exception) as exc:
        pv.validate_provider_default_reasoning_effort({"name": "P"}, "high")
    assert _http_status(exc) == 400


def test_validate_default_reasoning_effort_name_fallback(monkeypatch):
    monkeypatch.setattr(
        config_store, "reasoning_effort_options_for_provider", lambda rec: ["low"]
    )
    with pytest.raises(Exception) as exc:
        pv.validate_provider_default_reasoning_effort({"kind": "claude"}, "high")
    assert "claude" in exc.value.detail


# --------------------------------------------------------------------------- #
# api_extra_mcp_servers
# --------------------------------------------------------------------------- #
def test_extra_mcp_servers_none():
    assert pv.api_extra_mcp_servers(None) == []


def test_extra_mcp_servers_not_list():
    with pytest.raises(Exception) as exc:
        pv.api_extra_mcp_servers("x")
    assert _http_status(exc) == 400


def test_extra_mcp_servers_empty_entry():
    with pytest.raises(Exception) as exc:
        pv.api_extra_mcp_servers(["ok", "  "])
    assert _http_status(exc) == 400


def test_extra_mcp_servers_dedupes_and_validates(monkeypatch):
    monkeypatch.setattr(extension_store, "all_extension_mcp_server_names", lambda: {"a", "b"})
    assert pv.api_extra_mcp_servers(["b", "a", "b"]) == ["b", "a"]


def test_extra_mcp_servers_unknown(monkeypatch):
    monkeypatch.setattr(extension_store, "all_extension_mcp_server_names", lambda: {"a"})
    with pytest.raises(Exception) as exc:
        pv.api_extra_mcp_servers(["z"])
    assert _http_status(exc) == 400


# --------------------------------------------------------------------------- #
# api_disallowed_tools
# --------------------------------------------------------------------------- #
def test_disallowed_tools_none():
    assert pv.api_disallowed_tools(None) == []


def test_disallowed_tools_not_list():
    with pytest.raises(Exception) as exc:
        pv.api_disallowed_tools({"x": 1})
    assert _http_status(exc) == 400


def test_disallowed_tools_empty_entry():
    with pytest.raises(Exception) as exc:
        pv.api_disallowed_tools(["ok", ""])
    assert _http_status(exc) == 400


def test_disallowed_tools_dedupes():
    assert pv.api_disallowed_tools(["a", "b", "a"]) == ["a", "b"]


# --------------------------------------------------------------------------- #
# api_disabled_builtin_extensions
# --------------------------------------------------------------------------- #
def test_disabled_builtin_extensions_none():
    assert pv.api_disabled_builtin_extensions(None) == []


def test_disabled_builtin_extensions_not_list():
    with pytest.raises(Exception) as exc:
        pv.api_disabled_builtin_extensions("x")
    assert _http_status(exc) == 400


def test_disabled_builtin_extensions_empty_entry():
    with pytest.raises(Exception) as exc:
        pv.api_disabled_builtin_extensions(["ok", ""])
    assert _http_status(exc) == 400


def test_disabled_builtin_extensions_dedupes():
    assert pv.api_disabled_builtin_extensions(["a", "b", "a"]) == ["a", "b"]
