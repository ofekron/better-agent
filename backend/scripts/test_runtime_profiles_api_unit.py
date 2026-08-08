"""Dedicated owner for runtime_profiles_api.py — runtime-profile CRUD and
activation routes plus the prefill/snapshot helpers.

Unit tier: config_store and user_prefs are collaborators patched at the module
boundary to drive every branch deterministically; the late-imported models and
provider_validation collaborators in model_for_profile_switch are patched on
their real modules. Routes are async, driven via asyncio.run (no pytest-asyncio).
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import runtime_profiles_api


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_broadcast():
    runtime_profiles_api._broadcast_global = None
    yield
    runtime_profiles_api._broadcast_global = None


def _configure(captured):
    async def _bcast(event, payload):
        captured.append((event, payload))

    runtime_profiles_api.configure(_bcast)


def _fake_config(**overrides):
    base = dict(
        list_runtime_profiles=lambda include_deleted=True: [],
        get_default_runtime_profile_id=lambda: "default",
        list_deleted_providers=lambda: [],
        provider_execution_defaults=lambda pid, runner: {
            "runtime_profile_id": None,
            "default_model": "",
        },
        add_runtime_profile=lambda data: {"id": "new"},
        update_runtime_profile=lambda pid, data: {"id": pid},
        delete_runtime_profile=lambda pid: (True, ""),
        activate_runtime_profile=lambda pid: {"id": pid},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_prefs(**overrides):
    base = dict(
        get_last_models=lambda: {},
        get_last_reasoning_efforts=lambda: {},
        set_last_model=lambda pid, model: True,
        set_last_reasoning_effort=lambda pid, effort: True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _patch_model_collabs(monkeypatch, *, available, validate):
    import models
    import provider_validation

    monkeypatch.setattr(models, "available_models", available)
    monkeypatch.setattr(provider_validation, "validate_provider_model", validate)


# --- configure / _require_configured --------------------------------------

def test_route_raises_503_when_not_configured():
    with pytest.raises(HTTPException) as exc:
        _run(runtime_profiles_api.list_runtime_profiles())
    assert exc.value.status_code == 503


def test_require_configured_returns_bound_callable():
    captured = []
    _configure(captured)
    bound = runtime_profiles_api._broadcast_global
    assert runtime_profiles_api._require_configured() is bound


# --- _snapshot / list route ------------------------------------------------

def test_snapshot_filters_last_maps_to_known_profiles(monkeypatch):
    _configure([])
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(
            list_runtime_profiles=lambda include_deleted=True: [
                {"id": "p1"},
                {"id": "p2"},
            ],
            get_default_runtime_profile_id=lambda: "p1",
            list_deleted_providers=lambda: ["ghost"],
        ),
    )
    monkeypatch.setattr(
        runtime_profiles_api,
        "user_prefs",
        _fake_prefs(
            get_last_models=lambda: {"p1": "m1", "p3": "m3"},
            get_last_reasoning_efforts=lambda: {"p2": "low", "p4": "high"},
        ),
    )
    snap = runtime_profiles_api._snapshot()
    assert snap["runtime_profiles"] == [{"id": "p1"}, {"id": "p2"}]
    assert snap["default_runtime_profile_id"] == "p1"
    assert snap["deleted_providers"] == ["ghost"]
    assert snap["last_models"] == {"p1": "m1"}
    assert snap["last_reasoning_efforts"] == {"p2": "low"}


def test_list_route_returns_snapshot_without_broadcast(monkeypatch):
    captured = []
    _configure(captured)
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(list_runtime_profiles=lambda include_deleted=True: [{"id": "p1"}]),
    )
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs())
    result = _run(runtime_profiles_api.list_runtime_profiles())
    assert result["runtime_profiles"] == [{"id": "p1"}]
    assert captured == []


# --- runtime_profile_id_for_session ---------------------------------------

def test_profile_id_for_session_returns_none_without_provider(monkeypatch):
    monkeypatch.setattr(runtime_profiles_api, "config_store", _fake_config())
    assert runtime_profiles_api.runtime_profile_id_for_session({}) is None
    assert runtime_profiles_api.runtime_profile_id_for_session({"provider_id": ""}) is None


def test_profile_id_for_session_resolves_from_defaults(monkeypatch):
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(provider_execution_defaults=lambda pid, runner: {"runtime_profile_id": "rp-1"}),
    )
    session = {"provider_id": "p", "runner": "native"}
    assert runtime_profiles_api.runtime_profile_id_for_session(session) == "rp-1"


# --- record_last_model -----------------------------------------------------

def test_record_last_model_skips_when_missing(monkeypatch):
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs())
    _run(runtime_profiles_api.record_last_model(None, "m"))
    _run(runtime_profiles_api.record_last_model("rp", None))
    _run(runtime_profiles_api.record_last_model("", ""))


def test_record_last_model_broadcasts_on_change(monkeypatch):
    captured = []
    _configure(captured)
    monkeypatch.setattr(runtime_profiles_api, "config_store", _fake_config())
    monkeypatch.setattr(
        runtime_profiles_api,
        "user_prefs",
        _fake_prefs(set_last_model=lambda pid, model: True),
    )
    _run(runtime_profiles_api.record_last_model("rp", "m"))
    assert len(captured) == 1
    assert captured[0][0] == "runtime_profiles_changed"


def test_record_last_model_silent_when_unchanged(monkeypatch):
    captured = []
    _configure(captured)
    monkeypatch.setattr(runtime_profiles_api, "config_store", _fake_config())
    monkeypatch.setattr(
        runtime_profiles_api,
        "user_prefs",
        _fake_prefs(set_last_model=lambda pid, model: False),
    )
    _run(runtime_profiles_api.record_last_model("rp", "m"))
    assert captured == []


# --- record_last_reasoning_effort -----------------------------------------

def test_record_last_effort_skips_when_missing(monkeypatch):
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs())
    _run(runtime_profiles_api.record_last_reasoning_effort(None, "low"))
    _run(runtime_profiles_api.record_last_reasoning_effort("rp", None))
    _run(runtime_profiles_api.record_last_reasoning_effort("", ""))


def test_record_last_effort_broadcasts_on_change(monkeypatch):
    captured = []
    _configure(captured)
    monkeypatch.setattr(runtime_profiles_api, "config_store", _fake_config())
    monkeypatch.setattr(
        runtime_profiles_api,
        "user_prefs",
        _fake_prefs(set_last_reasoning_effort=lambda pid, effort: True),
    )
    _run(runtime_profiles_api.record_last_reasoning_effort("rp", "low"))
    assert len(captured) == 1 and captured[0][0] == "runtime_profiles_changed"


def test_record_last_effort_silent_when_unchanged(monkeypatch):
    captured = []
    _configure(captured)
    monkeypatch.setattr(runtime_profiles_api, "config_store", _fake_config())
    monkeypatch.setattr(
        runtime_profiles_api,
        "user_prefs",
        _fake_prefs(set_last_reasoning_effort=lambda pid, effort: False),
    )
    _run(runtime_profiles_api.record_last_reasoning_effort("rp", "low"))
    assert captured == []


# --- model_for_profile_switch ---------------------------------------------

def test_model_for_profile_switch_prefers_last_model(monkeypatch):
    _configure([])
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(provider_execution_defaults=lambda pid, runner: {
            "runtime_profile_id": "rp",
            "default_model": "def-model",
        }),
    )
    monkeypatch.setattr(
        runtime_profiles_api,
        "user_prefs",
        _fake_prefs(get_last_models=lambda: {"rp": "last-model"}),
    )
    _patch_model_collabs(
        monkeypatch,
        available=lambda pid: ["extra"],
        validate=lambda pid, model, flag: None,
    )
    chosen = _run(runtime_profiles_api.model_for_profile_switch("p", {"name": "P"}))
    assert chosen == "last-model"


def test_model_for_profile_switch_uses_default_without_profile(monkeypatch):
    _configure([])
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(provider_execution_defaults=lambda pid, runner: {
            "runtime_profile_id": None,
            "default_model": "def-model",
        }),
    )
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs(get_last_models=lambda: {}))
    _patch_model_collabs(
        monkeypatch,
        available=lambda pid: [],
        validate=lambda pid, model, flag: None,
    )
    chosen = _run(runtime_profiles_api.model_for_profile_switch("p", {}))
    assert chosen == "def-model"


def test_model_for_profile_switch_available_failure_swallowed(monkeypatch):
    _configure([])
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(provider_execution_defaults=lambda pid, runner: {
            "runtime_profile_id": None,
            "default_model": "def-model",
        }),
    )
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs(get_last_models=lambda: {}))

    def _boom(pid):
        raise RuntimeError("nope")

    _patch_model_collabs(monkeypatch, available=_boom, validate=lambda pid, model, flag: None)
    chosen = _run(runtime_profiles_api.model_for_profile_switch("p", {}))
    assert chosen == "def-model"


def test_model_for_profile_switch_skips_invalid_then_picks_valid(monkeypatch):
    _configure([])
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(provider_execution_defaults=lambda pid, runner: {
            "runtime_profile_id": None,
            "default_model": "bad",
        }),
    )
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs(get_last_models=lambda: {}))

    def _validate(pid, model, flag):
        if model == "bad":
            raise HTTPException(status_code=400, detail="bad")

    _patch_model_collabs(monkeypatch, available=lambda pid: ["good"], validate=_validate)
    chosen = _run(runtime_profiles_api.model_for_profile_switch("p", {}))
    assert chosen == "good"


def test_model_for_profile_switch_skips_empty_candidate_values(monkeypatch):
    _configure([])
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(provider_execution_defaults=lambda pid, runner: {
            "runtime_profile_id": "rp",
            "default_model": "",
        }),
    )
    monkeypatch.setattr(
        runtime_profiles_api,
        "user_prefs",
        _fake_prefs(get_last_models=lambda: {"rp": ""}),
    )
    _patch_model_collabs(
        monkeypatch,
        available=lambda pid: ["good"],
        validate=lambda pid, model, flag: None,
    )
    chosen = _run(runtime_profiles_api.model_for_profile_switch("p", {}))
    assert chosen == "good"


def test_model_for_profile_switch_dedups_available_candidates(monkeypatch):
    _configure([])
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(provider_execution_defaults=lambda pid, runner: {
            "runtime_profile_id": "rp",
            "default_model": "def-model",
        }),
    )
    monkeypatch.setattr(
        runtime_profiles_api,
        "user_prefs",
        _fake_prefs(get_last_models=lambda: {"rp": "last-model"}),
    )
    # last-model + def-model already candidates; "" empty skipped; only fresh kept.
    _patch_model_collabs(
        monkeypatch,
        available=lambda pid: ["last-model", "def-model", "", "fresh"],
        validate=lambda pid, model, flag: None,
    )
    chosen = _run(runtime_profiles_api.model_for_profile_switch("p", {}))
    assert chosen == "last-model"


def test_model_for_profile_switch_raises_with_name_when_no_valid(monkeypatch):
    _configure([])
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(provider_execution_defaults=lambda pid, runner: {
            "runtime_profile_id": None,
            "default_model": "bad",
        }),
    )
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs(get_last_models=lambda: {}))

    def _validate(pid, model, flag):
        raise HTTPException(status_code=400, detail="x")

    _patch_model_collabs(monkeypatch, available=lambda pid: ["alsobad"], validate=_validate)
    with pytest.raises(HTTPException) as exc:
        _run(runtime_profiles_api.model_for_profile_switch("p", {"name": "MyProvider"}))
    assert exc.value.status_code == 400
    assert "MyProvider" in exc.value.detail


def test_model_for_profile_switch_raises_with_provider_id_when_nameless(monkeypatch):
    _configure([])
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(provider_execution_defaults=lambda pid, runner: {
            "runtime_profile_id": None,
            "default_model": "bad",
        }),
    )
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs(get_last_models=lambda: {}))

    def _validate(pid, model, flag):
        raise HTTPException(status_code=400, detail="x")

    _patch_model_collabs(monkeypatch, available=lambda pid: [], validate=_validate)
    with pytest.raises(HTTPException) as exc:
        _run(runtime_profiles_api.model_for_profile_switch("prov-7", {}))
    assert exc.value.status_code == 400
    assert "prov-7" in exc.value.detail


# --- create route ----------------------------------------------------------

def test_create_route_returns_profile_and_broadcasts(monkeypatch):
    captured = []
    _configure(captured)
    created = {"id": "rp1", "default_model": "m"}
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(
            add_runtime_profile=lambda data: created,
            list_runtime_profiles=lambda include_deleted=True: [created],
        ),
    )
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs())
    body = runtime_profiles_api.RuntimeProfileCreate(provider_id="p", runner="native")
    result = _run(runtime_profiles_api.create_runtime_profile(body))
    assert result == created
    assert captured and captured[-1][0] == "runtime_profiles_changed"


def test_create_route_maps_value_error_to_400(monkeypatch):
    _configure([])

    def _add(data):
        raise ValueError("dup")

    monkeypatch.setattr(runtime_profiles_api, "config_store", _fake_config(add_runtime_profile=_add))
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs())
    body = runtime_profiles_api.RuntimeProfileCreate(provider_id="p", runner="native")
    with pytest.raises(HTTPException) as exc:
        _run(runtime_profiles_api.create_runtime_profile(body))
    assert exc.value.status_code == 400 and "dup" in exc.value.detail


# --- patch route -----------------------------------------------------------

def test_patch_route_updates_and_broadcasts(monkeypatch):
    captured = []
    _configure(captured)
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(
            update_runtime_profile=lambda pid, data: {"id": pid, "name": "X"},
            list_runtime_profiles=lambda include_deleted=True: [],
        ),
    )
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs())
    body = runtime_profiles_api.RuntimeProfilePatch(name="X")
    result = _run(runtime_profiles_api.patch_runtime_profile("rp1", body))
    assert result["name"] == "X"
    assert captured


def test_patch_route_maps_value_error_to_400(monkeypatch):
    _configure([])

    def _upd(pid, data):
        raise ValueError("bad")

    monkeypatch.setattr(runtime_profiles_api, "config_store", _fake_config(update_runtime_profile=_upd))
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs())
    body = runtime_profiles_api.RuntimeProfilePatch(name="X")
    with pytest.raises(HTTPException) as exc:
        _run(runtime_profiles_api.patch_runtime_profile("rp1", body))
    assert exc.value.status_code == 400


def test_patch_route_returns_404_when_missing(monkeypatch):
    _configure([])
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(update_runtime_profile=lambda pid, data: None),
    )
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs())
    body = runtime_profiles_api.RuntimeProfilePatch(name="X")
    with pytest.raises(HTTPException) as exc:
        _run(runtime_profiles_api.patch_runtime_profile("rp1", body))
    assert exc.value.status_code == 404


# --- delete route ----------------------------------------------------------

def test_delete_route_succeeds_and_broadcasts(monkeypatch):
    captured = []
    _configure(captured)
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(
            delete_runtime_profile=lambda pid: (True, ""),
            list_runtime_profiles=lambda include_deleted=True: [],
        ),
    )
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs())
    result = _run(runtime_profiles_api.delete_runtime_profile("rp1"))
    assert result == {"deleted": True}
    assert captured


def test_delete_route_returns_404_when_missing(monkeypatch):
    _configure([])
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(delete_runtime_profile=lambda pid: (False, "missing")),
    )
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs())
    with pytest.raises(HTTPException) as exc:
        _run(runtime_profiles_api.delete_runtime_profile("rp1"))
    assert exc.value.status_code == 404


def test_delete_route_returns_409_with_reason(monkeypatch):
    _configure([])
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(delete_runtime_profile=lambda pid: (False, "in_use")),
    )
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs())
    with pytest.raises(HTTPException) as exc:
        _run(runtime_profiles_api.delete_runtime_profile("rp1"))
    assert exc.value.status_code == 409 and "in_use" in exc.value.detail


# --- activate route --------------------------------------------------------

def test_activate_route_succeeds_and_broadcasts(monkeypatch):
    captured = []
    _configure(captured)
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(
            activate_runtime_profile=lambda pid: {"id": pid},
            list_runtime_profiles=lambda include_deleted=True: [],
        ),
    )
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs())
    result = _run(runtime_profiles_api.activate_runtime_profile("rp1"))
    assert result == {"id": "rp1"}
    assert captured


def test_activate_route_maps_value_error_to_400(monkeypatch):
    _configure([])

    def _act(pid):
        raise ValueError("bad")

    monkeypatch.setattr(runtime_profiles_api, "config_store", _fake_config(activate_runtime_profile=_act))
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs())
    with pytest.raises(HTTPException) as exc:
        _run(runtime_profiles_api.activate_runtime_profile("rp1"))
    assert exc.value.status_code == 400


def test_activate_route_maps_runtime_error_to_409(monkeypatch):
    _configure([])

    def _act(pid):
        raise RuntimeError("busy")

    monkeypatch.setattr(runtime_profiles_api, "config_store", _fake_config(activate_runtime_profile=_act))
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs())
    with pytest.raises(HTTPException) as exc:
        _run(runtime_profiles_api.activate_runtime_profile("rp1"))
    assert exc.value.status_code == 409 and "busy" in exc.value.detail


def test_activate_route_returns_404_when_missing(monkeypatch):
    _configure([])
    monkeypatch.setattr(
        runtime_profiles_api,
        "config_store",
        _fake_config(activate_runtime_profile=lambda pid: None),
    )
    monkeypatch.setattr(runtime_profiles_api, "user_prefs", _fake_prefs())
    with pytest.raises(HTTPException) as exc:
        _run(runtime_profiles_api.activate_runtime_profile("rp1"))
    assert exc.value.status_code == 404
