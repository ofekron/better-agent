#!/usr/bin/env python3
"""Unit coverage for Package D (ADR 0007 provider/auth surface): D1 live
push, D2 installable catalog, D3 login_flow_state, D4 intents, plus the
smaller orchestration_modes/send_modes read-plane gaps.

Isolation recipe matches `backend/scripts/test_adapter_api.py`:
`paths.engage_test_home` before any backend import so a real BA home is
never touched, even though most tests here monkeypatch `store_access`
directly rather than exercising it.

Run:
    PYTHONPATH=. python3 -m pytest backend/scripts/test_adapter_provider.py -q
    PYTHONPATH=. python3 backend/scripts/test_adapter_provider.py   # __main__ fallback
"""

from __future__ import annotations

import asyncio
import atexit
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(_BACKEND_DIR).parent)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import paths  # noqa: E402

_TEST_HOME = tempfile.mkdtemp(prefix="ba-adapter-provider-test-")
paths.engage_test_home(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import adapter_api  # noqa: E402
import providers_api  # noqa: E402
import runtime_profiles_api  # noqa: E402
from backend.adapters.provider_adapter import (  # noqa: E402
    ProviderConfigSurfaceAdapter,
    _capabilities,
    _config_state,
    _installable_descriptor,
    _map_descriptor,
    _orchestration_modes,
    _send_modes,
    _TEMPLATES,
    _INSTALLABLE_CATALOG,
)
from backend.adapters.store_access import ProviderRecord, store_access  # noqa: E402
from backend.event_bus import BusEvent, bus  # noqa: E402
from backend.surface_contract.descriptors import (  # noqa: E402
    AuthFlow,
    ConfigState,
    CredentialState,
    InstallProgressChanged,
    InstallState,
    LoginFlowFrame,
    LoginPhase,
    ModelCatalogChanged,
    ProviderUpsert,
    RuntimeProfilesChanged,
    RuntimeProfilesSnapshot,
)
from backend.surface_contract.intents import (  # noqa: E402
    BeginLogin,
    CancelLogin,
    CreateProvider,
    DeleteProvider,
    DeleteRuntimeProfile,
    IntentAccepted,
    IntentRejected,
    RefreshModels,
    RetryCredential,
    SaveRuntimeProfile,
    SuspendProvider,
    UpdateProvider,
)

_FAILURES: list[str] = []


def _record(
    id="p1", name="Claude", kind="claude", models=("m1",), capabilities=None,
    suspended=False, mode="subscription",
) -> ProviderRecord:
    return ProviderRecord(
        id=id, name=name, kind=kind, models=models,
        capabilities=capabilities or {}, suspended=suspended, mode=mode,
    )


def _run(coro) -> object:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pure mapping functions: _capabilities / _config_state / orchestration_modes
# / send_modes / _map_descriptor
# ---------------------------------------------------------------------------

def test_capabilities_maps_all_eight_keys():
    record = _record(capabilities={
        "supports_fork": True, "supports_manager_mode": True, "supports_rewind": False,
        "supports_steering": True, "supports_native_subagents": False,
        "supports_reasoning_effort": True,
    })
    caps = _capabilities(record)
    assert caps.fork is True
    assert caps.manager_mode is True
    assert caps.rewind is False
    assert caps.steering is True
    assert caps.native_subagents is False
    assert caps.reasoning_effort is True
    # config_store's capability matrix never carries these two — always False.
    assert caps.usage_reporting is False
    assert caps.startup_monitoring is False


def test_config_state_suspended_wins_over_credential():
    record = _record(mode="api_key", suspended=True)
    with patch.object(store_access, "get_provider_credential_status", side_effect=AssertionError("must not probe when suspended")):
        assert _config_state(record) == ConfigState.SUSPENDED


def test_config_state_subscription_mode_never_probes_credential():
    record = _record(mode="subscription", suspended=False)
    with patch.object(store_access, "get_provider_credential_status", side_effect=AssertionError("subscription providers have no credential-broker status")):
        assert _config_state(record) == ConfigState.ACTIVE


def test_config_state_api_key_missing_maps_to_credential_required():
    record = _record(mode="api_key", suspended=False)
    with patch.object(store_access, "get_provider_credential_status", return_value="missing"):
        assert _config_state(record) == ConfigState.CREDENTIAL_REQUIRED


def test_config_state_api_key_blocked_maps_to_credential_failed():
    record = _record(mode="api_key", suspended=False)
    with patch.object(store_access, "get_provider_credential_status", return_value="blocked"):
        assert _config_state(record) == ConfigState.CREDENTIAL_FAILED


def test_orchestration_modes_native_baseline_and_team_when_manager_mode():
    record = _record(capabilities={})
    caps = _capabilities(record)
    assert _orchestration_modes(caps) == ("native",)
    record2 = _record(capabilities={"supports_manager_mode": True})
    caps2 = _capabilities(record2)
    assert _orchestration_modes(caps2) == ("native", "team")


def test_send_modes_queue_interrupt_baseline_and_steer_when_capability():
    record = _record(capabilities={})
    caps = _capabilities(record)
    assert _send_modes(caps) == ("queue", "interrupt")
    record2 = _record(capabilities={"supports_steering": True})
    caps2 = _capabilities(record2)
    assert _send_modes(caps2) == ("queue", "interrupt", "steer")


def test_map_descriptor_subscription_mode_yields_oauth_auth_flow():
    record = _record(mode="subscription")
    descriptor = _map_descriptor(record)
    assert descriptor.auth_flows == (AuthFlow.OAUTH_SUBSCRIPTION,)
    assert descriptor.provider_id == "p1"
    assert descriptor.model_catalog_ref == "p1"


# ---------------------------------------------------------------------------
# D2: installable catalog
# ---------------------------------------------------------------------------

def test_installable_catalog_covers_every_template_exactly_once():
    kinds = [d.kind for d in _INSTALLABLE_CATALOG]
    assert len(kinds) == len(_TEMPLATES)
    assert len(kinds) == len(set(kinds)), "duplicate InstallableDescriptor.kind"


def test_installable_catalog_every_entry_has_at_least_one_auth_flow():
    for descriptor in _INSTALLABLE_CATALOG:
        assert descriptor.auth_flows, f"{descriptor.kind} has no auth_flows"


def test_installable_catalog_openai_underlying_kind_omits_config_dir_field():
    openai_backed = [t for t in _TEMPLATES if t.kind == "openai"]
    assert openai_backed, "fixture drift: no openai-backed template found"
    descriptor = _installable_descriptor(openai_backed[0])
    field_names = {f.name for f in descriptor.form_schema}
    assert "config_dir" not in field_names


def test_installable_catalog_claude_underlying_kind_includes_config_dir_field():
    descriptor = _installable_descriptor(next(t for t in _TEMPLATES if t.id == "claude"))
    field_names = {f.name for f in descriptor.form_schema}
    assert "config_dir" in field_names


def test_installable_catalog_openai_underlying_kind_uses_openai_flavored_copy():
    # Closure 4 (form-copy regression): the deleted frontend's
    # apiEnvCopyForKind gave openai-backed templates OPENAI_API_KEY/
    # OPENAI_BASE_URL labels + an openai-flavored empty-key placeholder —
    # restored via FormField.placeholder_key, not collapsed to the
    # generic ANTHROPIC_* copy every other kind uses.
    openai_backed = [t for t in _TEMPLATES if t.kind == "openai"]
    assert openai_backed, "fixture drift: no openai-backed template found"
    descriptor = _installable_descriptor(openai_backed[0])
    api_key_field = next(f for f in descriptor.form_schema if f.name == "api_key")
    base_url_field = next(f for f in descriptor.form_schema if f.name == "base_url")
    assert api_key_field.label_key == "setup.apiKeyLabelOpenai"
    assert api_key_field.placeholder_key == "setup.apiKeyPlaceholderEmptyOpenai"
    assert base_url_field.label_key == "setup.baseUrlLabelOpenai"


def test_installable_catalog_non_openai_underlying_kind_uses_generic_anthropic_copy():
    descriptor = _installable_descriptor(next(t for t in _TEMPLATES if t.id == "claude"))
    api_key_field = next(f for f in descriptor.form_schema if f.name == "api_key")
    assert api_key_field.label_key == "setup.apiKeyLabel"
    assert api_key_field.placeholder_key == "setup.apiKeyPlaceholderEmpty"


def test_installable_catalog_codex_and_fugu_share_codex_config_dir_copy():
    # Closure 4: ported verbatim from the deleted frontend's
    # configDirCopyForKind — Fugu deploys into the Codex CLI's own config
    # dir, so both underlying kinds get the SAME codex-flavored copy.
    for template_id, underlying_kind in (("codex", "codex"), ("fugu", "fugu")):
        descriptor = _installable_descriptor(next(t for t in _TEMPLATES if t.id == template_id))
        assert next(t for t in _TEMPLATES if t.id == template_id).kind == underlying_kind
        config_dir_field = next(f for f in descriptor.form_schema if f.name == "config_dir")
        assert config_dir_field.label_key == "setup.configDirLabelCodex"
        assert config_dir_field.placeholder_key == "setup.configDirPlaceholderCodex"
        assert config_dir_field.hint_key == "setup.configDirHintCodex"


def test_installable_catalog_agy_and_copilot_get_their_own_config_dir_copy():
    agy_field = next(
        f for f in _installable_descriptor(next(t for t in _TEMPLATES if t.id == "agy")).form_schema
        if f.name == "config_dir"
    )
    assert agy_field.label_key == "setup.configDirLabelAgy"
    assert agy_field.placeholder_key == "setup.configDirPlaceholderAgy"
    assert agy_field.hint_key == "setup.configDirHintAgy"

    copilot_field = next(
        f for f in _installable_descriptor(next(t for t in _TEMPLATES if t.id == "copilot")).form_schema
        if f.name == "config_dir"
    )
    assert copilot_field.label_key == "setup.configDirLabelCopilot"
    assert copilot_field.placeholder_key == "setup.configDirPlaceholderCopilot"
    assert copilot_field.hint_key == "setup.configDirHintCopilot"


def test_installable_catalog_claude_kind_gets_claude_config_dir_copy():
    field = next(
        f for f in _installable_descriptor(next(t for t in _TEMPLATES if t.id == "claude")).form_schema
        if f.name == "config_dir"
    )
    assert field.label_key == "setup.configDirLabelClaude"
    assert field.placeholder_key == "setup.configDirPlaceholderClaude"
    assert field.hint_key == "setup.configDirHintClaude"


def test_installable_catalog_every_new_placeholder_and_hint_key_exists_in_en_locale():
    # Scoped to closure 4's OWN additions (placeholder_key/hint_key) — every
    # one must resolve to a real, still-present en.json entry (the deleted
    # frontend components' keys were never removed from the locale files).
    # Does not re-verify every pre-existing label_key (a stale one there,
    # e.g. "mode", is a different, pre-existing gap, not this closure's).
    import json
    from pathlib import Path

    en = json.loads((Path(_BACKEND_DIR).parent / "frontend" / "src" / "i18n" / "en.json").read_text())
    checked = 0
    for descriptor in _INSTALLABLE_CATALOG:
        for field in descriptor.form_schema:
            if field.placeholder_key is not None:
                assert field.placeholder_key in en, f"{descriptor.kind}.{field.name}.placeholder_key {field.placeholder_key!r} missing from en.json"
                checked += 1
            if field.hint_key is not None:
                assert field.hint_key in en, f"{descriptor.kind}.{field.name}.hint_key {field.hint_key!r} missing from en.json"
                checked += 1
    assert checked > 0, "fixture drift: no placeholder_key/hint_key populated anywhere in the catalog"


def test_installable_catalog_restricted_kind_offers_single_mode_no_mode_field():
    # "pi" is subscription-only (ported from providerFormShape.ts's
    # PROVIDER_MODES) — the form shouldn't offer a mode toggle at all.
    descriptor = _installable_descriptor(next(t for t in _TEMPLATES if t.id == "pi"))
    field_names = {f.name for f in descriptor.form_schema}
    assert "mode" not in field_names
    assert descriptor.auth_flows == (AuthFlow.OAUTH_SUBSCRIPTION,)


def test_installable_catalog_secret_field_never_carries_a_default_value():
    # Secrets are write-only per ADR 0007 ("never echoed in any read
    # model") — the catalog's own FormField default must stay empty too.
    for descriptor in _INSTALLABLE_CATALOG:
        for field in descriptor.form_schema:
            if field.kind.value == "secret":
                assert field.default in (None, ""), f"{descriptor.kind}.{field.name} leaks a default secret"


def test_provider_config_surface_serves_the_module_level_catalog():
    adapter = ProviderConfigSurfaceAdapter()
    assert adapter.installable_catalog() == _INSTALLABLE_CATALOG


# B7 ruling (parity audit): dev's deleted `frontend/tests/providerFormShape
# .test.ts` pinned the Meta Muse Spark / Hetzner templates' literal values
# against the old static frontend `TEMPLATES` array. That array is gone —
# `provider_adapter.py`'s `_TEMPLATES` is the one source now (see this
# module's own docstring) — so these pin the same literal values against
# the backend-served installable catalog instead, restoring the guard a
# future accidental edit to either row would otherwise silently pass.

def test_installable_catalog_meta_muse_spark_template_pins_literal_values():
    template = next(t for t in _TEMPLATES if t.id == "meta-muse")
    assert template.label == "Meta Muse Spark"
    assert template.kind == "openai"
    assert template.mode == "api_key"
    assert template.base_url == "https://api.meta.ai/v1"
    assert template.default_model == "muse-spark-1.1"

    descriptor = _installable_descriptor(template)
    assert descriptor.kind == "meta-muse"
    assert descriptor.defaults["kind"] == "openai"
    assert descriptor.defaults["base_url"] == "https://api.meta.ai/v1"
    assert descriptor.defaults["default_model"] == "muse-spark-1.1"


def test_installable_catalog_hetzner_template_pins_literal_values():
    template = next(t for t in _TEMPLATES if t.id == "hetzner")
    assert template.label == "Hetzner Inference"
    assert template.kind == "openai"
    assert template.mode == "api_key"
    assert template.base_url == "https://inference.hetzner.com/api/v1"
    assert template.default_model == "Qwen/Qwen3.6-35B-A3B-FP8"

    descriptor = _installable_descriptor(template)
    assert descriptor.kind == "hetzner"
    assert descriptor.defaults["kind"] == "openai"
    assert descriptor.defaults["base_url"] == "https://inference.hetzner.com/api/v1"
    assert descriptor.defaults["default_model"] == "Qwen/Qwen3.6-35B-A3B-FP8"


def test_runtime_profiles_read_plane_builds_full_snapshot():
    from backend.adapters.store_access import DeletedProviderRecord, RuntimeProfileRecord

    live = RuntimeProfileRecord(
        id="rp1", provider_id="p1", runner="better_agent_runner", name="Claude (runner)",
        default_model="m1", default_reasoning_effort="medium",
        created_at="t0", updated_at="t0", deleted_at=None,
    )
    tomb = RuntimeProfileRecord(
        id="rp2", provider_id="p1", runner="native", name="Claude (native)",
        default_model="", default_reasoning_effort="",
        created_at="t0", updated_at="t1", deleted_at="t1",
    )
    grave = DeletedProviderRecord(id="p-old", name="Old", nickname="", kind="claude", deleted_at="t1")
    adapter = ProviderConfigSurfaceAdapter()
    with patch.object(store_access, "list_runtime_profiles", return_value=(live, tomb)) as m_list, \
         patch.object(store_access, "get_default_runtime_profile_id", return_value="rp1"), \
         patch.object(store_access, "get_last_models", return_value={"rp1": "m1"}), \
         patch.object(store_access, "get_last_reasoning_efforts", return_value={"rp1": "high"}), \
         patch.object(store_access, "list_deleted_providers", return_value=(grave,)):
        snapshot = adapter.runtime_profiles()
    m_list.assert_called_once_with(include_deleted=True)
    assert isinstance(snapshot, RuntimeProfilesSnapshot)
    assert {p.runtime_profile_id for p in snapshot.profiles} == {"rp1", "rp2"}
    tomb_out = next(p for p in snapshot.profiles if p.runtime_profile_id == "rp2")
    assert tomb_out.deleted_at == "t1"
    assert tomb_out.name == "Claude (native)"
    live_out = next(p for p in snapshot.profiles if p.runtime_profile_id == "rp1")
    assert live_out.deleted_at is None
    assert snapshot.default_runtime_profile_id == "rp1"
    assert snapshot.last_models == {"rp1": "m1"}
    assert snapshot.last_reasoning_efforts == {"rp1": "high"}
    assert snapshot.deleted_providers[0].provider_id == "p-old"


# ---------------------------------------------------------------------------
# D1: live fact -> frame handlers
# ---------------------------------------------------------------------------

def test_provider_mutated_broadcasts_upsert_and_is_change_only():
    adapter = ProviderConfigSurfaceAdapter()
    frames: list[object] = []
    adapter.subscribe(frames.append)
    record = _record()
    with patch.object(store_access, "list_provider_records", return_value=(record,)):
        _run(adapter._on_provider_mutated(BusEvent(type="provider.mutated", root_id="", sid="", payload={"provider_id": "p1"})))
        _run(adapter._on_provider_mutated(BusEvent(type="provider.mutated", root_id="", sid="", payload={"provider_id": "p1"})))
    upserts = [f for f in frames if isinstance(f, ProviderUpsert)]
    assert len(upserts) == 1, "identical re-derivation must not re-broadcast"
    assert upserts[0].descriptor.provider_id == "p1"


def test_provider_mutated_for_missing_record_evicts_cache_without_broadcasting():
    adapter = ProviderConfigSurfaceAdapter()
    frames: list[object] = []
    adapter.subscribe(frames.append)
    record = _record()
    with patch.object(store_access, "list_provider_records", return_value=(record,)):
        _run(adapter._on_provider_mutated(BusEvent(type="provider.mutated", root_id="", sid="", payload={"provider_id": "p1"})))
    with patch.object(store_access, "list_provider_records", return_value=()):
        _run(adapter._on_provider_mutated(BusEvent(type="provider.mutated", root_id="", sid="", payload={"provider_id": "p1"})))
    # gap (documented in provider_adapter.py): no removal frame exists —
    # only the earlier upsert should ever have fired.
    assert len([f for f in frames if isinstance(f, ProviderUpsert)]) == 1


def test_credential_state_change_broadcasts_credential_state_frame():
    adapter = ProviderConfigSurfaceAdapter()
    frames: list[object] = []
    adapter.subscribe(frames.append)
    record = _record(mode="api_key", suspended=True)
    with patch.object(store_access, "list_provider_records", return_value=(record,)):
        _run(adapter._on_credential_state_changed(BusEvent(type="provider.credential_state_changed", root_id="", sid="", payload={"provider_id": "p1"})))
    states = [f for f in frames if isinstance(f, CredentialState)]
    assert len(states) == 1
    assert states[0].config_state == ConfigState.SUSPENDED
    assert states[0].provider_id == "p1"


def test_model_catalog_changed_fact_always_broadcasts_no_local_dedup():
    adapter = ProviderConfigSurfaceAdapter()
    frames: list[object] = []
    adapter.subscribe(frames.append)
    _run(adapter._on_model_catalog_changed(BusEvent(type="provider.model_catalog_changed", root_id="", sid="", payload={"provider_id": "p1"})))
    _run(adapter._on_model_catalog_changed(BusEvent(type="provider.model_catalog_changed", root_id="", sid="", payload={"provider_id": "p1"})))
    frames2 = [f for f in frames if isinstance(f, ModelCatalogChanged)]
    assert len(frames2) == 2, "source already change-gates this fact; adapter must not re-dedup"


def test_login_flow_fact_maps_payload_to_login_flow_frame():
    adapter = ProviderConfigSurfaceAdapter()
    frames: list[object] = []
    adapter.subscribe(frames.append)
    _run(adapter._on_login_flow_changed(BusEvent(
        type="provider.login_flow_changed", root_id="", sid="",
        payload={"provider_id": "p1", "intent_id": "i1", "phase": "starting", "data": None},
    )))
    login_frames = [f for f in frames if isinstance(f, LoginFlowFrame)]
    assert len(login_frames) == 1
    state = login_frames[0].state
    assert state.provider_id == "p1"
    assert state.intent_id == "i1"
    assert state.phase == LoginPhase.STARTING


def test_login_flow_fact_unknown_phase_is_dropped():
    adapter = ProviderConfigSurfaceAdapter()
    frames: list[object] = []
    adapter.subscribe(frames.append)
    _run(adapter._on_login_flow_changed(BusEvent(
        type="provider.login_flow_changed", root_id="", sid="",
        payload={"provider_id": "p1", "intent_id": "", "phase": "not_a_real_phase", "data": None},
    )))
    assert not any(isinstance(f, LoginFlowFrame) for f in frames)


def test_install_progress_fact_maps_full_run_snapshot():
    adapter = ProviderConfigSurfaceAdapter()
    frames: list[object] = []
    adapter.subscribe(frames.append)
    _run(adapter._on_install_progress_changed(BusEvent(
        type="provider.install_progress_changed", root_id="", sid="",
        payload={
            "kind": "codex", "label": "Codex", "command": "npm install -g codex",
            "state": "running", "lines": [{"s": "stdout", "t": "installing..."}],
            "started_at": "2026-01-01T00:00:00+00:00", "finished_at": None,
            "returncode": None, "installed": None, "message": None,
        },
    )))
    install_frames = [f for f in frames if isinstance(f, InstallProgressChanged)]
    assert len(install_frames) == 1
    run = install_frames[0].run
    assert run.kind == "codex"
    assert run.state == InstallState.RUNNING
    assert [(line.stream, line.text) for line in run.lines] == [("stdout", "installing...")]


def test_install_progress_fact_never_dedups_repeated_ticks():
    adapter = ProviderConfigSurfaceAdapter()
    frames: list[object] = []
    adapter.subscribe(frames.append)
    payload = {
        "kind": "codex", "label": "Codex", "command": "npm install -g codex",
        "state": "running", "lines": [], "started_at": None, "finished_at": None,
        "returncode": None, "installed": None, "message": None,
    }
    _run(adapter._on_install_progress_changed(BusEvent(
        type="provider.install_progress_changed", root_id="", sid="", payload=payload,
    )))
    _run(adapter._on_install_progress_changed(BusEvent(
        type="provider.install_progress_changed", root_id="", sid="", payload=payload,
    )))
    install_frames = [f for f in frames if isinstance(f, InstallProgressChanged)]
    assert len(install_frames) == 2, "every tick is a distinct live event, never dedup'd"


def test_install_progress_fact_unknown_state_is_dropped():
    adapter = ProviderConfigSurfaceAdapter()
    frames: list[object] = []
    adapter.subscribe(frames.append)
    _run(adapter._on_install_progress_changed(BusEvent(
        type="provider.install_progress_changed", root_id="", sid="",
        payload={"kind": "codex", "state": "not_a_real_state"},
    )))
    assert not any(isinstance(f, InstallProgressChanged) for f in frames)


def _runtime_profiles_changed_payload() -> dict:
    return {
        "runtime_profiles": [
            {
                "id": "rp1", "provider_id": "p1", "runner": "better_agent_runner", "name": "Claude",
                "default_model": "m1", "default_reasoning_effort": "medium",
                "created_at": "t0", "updated_at": "t0", "deleted_at": None,
            },
        ],
        "default_runtime_profile_id": "rp1",
        "deleted_providers": [
            {"id": "p-old", "name": "Old", "nickname": "", "kind": "claude", "deleted_at": "t1"},
        ],
        "last_models": {"rp1": "m1"},
        "last_reasoning_efforts": {"rp1": "medium"},
    }


def test_runtime_profiles_changed_fact_maps_full_snapshot():
    adapter = ProviderConfigSurfaceAdapter()
    frames: list[object] = []
    adapter.subscribe(frames.append)
    _run(adapter._on_runtime_profiles_changed(BusEvent(
        type="provider.runtime_profiles_changed", root_id="", sid="",
        payload=_runtime_profiles_changed_payload(),
    )))
    changed = [f for f in frames if isinstance(f, RuntimeProfilesChanged)]
    assert len(changed) == 1
    snap = changed[0].snapshot
    assert snap.default_runtime_profile_id == "rp1"
    assert snap.last_models == {"rp1": "m1"}
    assert snap.last_reasoning_efforts == {"rp1": "medium"}
    assert len(snap.profiles) == 1
    assert snap.profiles[0].runtime_profile_id == "rp1"
    assert snap.profiles[0].name == "Claude"
    assert snap.profiles[0].deleted_at is None
    assert snap.deleted_providers[0].provider_id == "p-old"
    assert snap.deleted_providers[0].name == "Old"


def test_runtime_profiles_changed_fact_carries_tombstones_too():
    adapter = ProviderConfigSurfaceAdapter()
    frames: list[object] = []
    adapter.subscribe(frames.append)
    payload = _runtime_profiles_changed_payload()
    payload["runtime_profiles"].append({
        "id": "rp2", "provider_id": "p1", "runner": "native", "name": "Claude (native)",
        "default_model": "", "default_reasoning_effort": "",
        "created_at": "t0", "updated_at": "t1", "deleted_at": "t1",
    })
    _run(adapter._on_runtime_profiles_changed(BusEvent(
        type="provider.runtime_profiles_changed", root_id="", sid="", payload=payload,
    )))
    snap = next(f for f in frames if isinstance(f, RuntimeProfilesChanged)).snapshot
    tomb = next(p for p in snap.profiles if p.runtime_profile_id == "rp2")
    assert tomb.deleted_at == "t1"


def test_runtime_profiles_changed_fact_never_dedups():
    adapter = ProviderConfigSurfaceAdapter()
    frames: list[object] = []
    adapter.subscribe(frames.append)
    payload = _runtime_profiles_changed_payload()
    _run(adapter._on_runtime_profiles_changed(BusEvent(
        type="provider.runtime_profiles_changed", root_id="", sid="", payload=payload,
    )))
    _run(adapter._on_runtime_profiles_changed(BusEvent(
        type="provider.runtime_profiles_changed", root_id="", sid="", payload=payload,
    )))
    changed = [f for f in frames if isinstance(f, RuntimeProfilesChanged)]
    assert len(changed) == 2, "the record-last-model call sites carry no provider_id to dedup against"


def test_runtime_profiles_changed_fact_missing_runtime_profiles_key_is_dropped():
    adapter = ProviderConfigSurfaceAdapter()
    frames: list[object] = []
    adapter.subscribe(frames.append)
    _run(adapter._on_runtime_profiles_changed(BusEvent(
        type="provider.runtime_profiles_changed", root_id="", sid="", payload={},
    )))
    assert not any(isinstance(f, RuntimeProfilesChanged) for f in frames)


def test_bind_subscribes_through_the_real_bus_end_to_end():
    """One live-bus integration check (not just direct handler calls) to
    prove `bind()` actually wires all six fact patterns."""
    adapter = ProviderConfigSurfaceAdapter()
    adapter.bind()
    frames: list[object] = []
    adapter.subscribe(frames.append)
    record = _record(id="p-bind")
    with patch.object(store_access, "list_provider_records", return_value=(record,)):
        _run(bus.publish(BusEvent(type="provider.mutated", root_id="", sid="", payload={"provider_id": "p-bind"})))
    assert any(isinstance(f, ProviderUpsert) and f.descriptor.provider_id == "p-bind" for f in frames)

    _run(bus.publish(BusEvent(
        type="provider.install_progress_changed", root_id="", sid="",
        payload={
            "kind": "codex", "label": "Codex", "command": "npm i -g codex", "state": "running",
            "lines": [], "started_at": None, "finished_at": None, "returncode": None,
            "installed": None, "message": None,
        },
    )))
    assert any(isinstance(f, InstallProgressChanged) and f.run.kind == "codex" for f in frames)

    _run(bus.publish(BusEvent(
        type="provider.runtime_profiles_changed", root_id="", sid="",
        payload=_runtime_profiles_changed_payload(),
    )))
    assert any(
        isinstance(f, RuntimeProfilesChanged) and f.snapshot.default_runtime_profile_id == "rp1"
        for f in frames
    )


# ---------------------------------------------------------------------------
# providers_api.py: D1 fact decomposition (diff logic on plain dicts)
# ---------------------------------------------------------------------------

def test_provider_fact_slice_tracks_generation_revision_suspended_credential_login():
    slice_ = providers_api._provider_fact_slice({
        "generation": "g1", "revision": 1, "suspended": False,
        "credential_status": "available",
        "login_state": {"status": "login_running", "authenticated": None},
    })
    assert slice_ == {
        "generation": "g1", "revision": 1, "suspended": False,
        "credential_status": "available",
        "login_status": "login_running", "login_authenticated": None,
    }


def test_publish_provider_facts_fires_mutated_and_credential_on_first_sight():
    providers_api._last_provider_projection.clear()
    seen: list[tuple[str, dict]] = []

    async def _capture(event: BusEvent) -> None:
        seen.append((event.type, event.payload))

    bus.subscribe("provider.*", _capture, name="test:publish_provider_facts:first_sight")
    try:
        _run(providers_api._publish_provider_facts({
            "providers": [{
                "id": "pf1", "generation": "g1", "revision": 1, "suspended": False,
                "credential_status": "available", "login_state": {},
            }],
        }))
    finally:
        bus.unsubscribe("test:publish_provider_facts:first_sight")
    types = {t for t, _ in seen}
    assert providers_api.FACT_PROVIDER_MUTATED in types
    assert providers_api.FACT_CREDENTIAL_STATE_CHANGED in types


def test_publish_provider_facts_is_silent_on_a_true_no_op_repeat():
    providers_api._last_provider_projection.clear()
    state = {"providers": [{
        "id": "pf2", "generation": "g1", "revision": 1, "suspended": False,
        "credential_status": "available", "login_state": {},
    }]}
    _run(providers_api._publish_provider_facts(state))
    seen: list[tuple[str, dict]] = []

    async def _capture(event: BusEvent) -> None:
        seen.append((event.type, event.payload))

    bus.subscribe("provider.*", _capture, name="test:publish_provider_facts:repeat")
    try:
        _run(providers_api._publish_provider_facts(state))
    finally:
        bus.unsubscribe("test:publish_provider_facts:repeat")
    assert seen == []


def test_publish_provider_facts_fires_credential_only_on_suspend_toggle():
    providers_api._last_provider_projection.clear()
    base = {
        "id": "pf3", "generation": "g1", "revision": 1, "suspended": False,
        "credential_status": "available", "login_state": {},
    }
    _run(providers_api._publish_provider_facts({"providers": [base]}))
    seen: list[tuple[str, dict]] = []

    async def _capture(event: BusEvent) -> None:
        seen.append((event.type, event.payload))

    bus.subscribe("provider.*", _capture, name="test:publish_provider_facts:suspend_toggle")
    try:
        toggled = {**base, "suspended": True}
        _run(providers_api._publish_provider_facts({"providers": [toggled]}))
    finally:
        bus.unsubscribe("test:publish_provider_facts:suspend_toggle")
    types = {t for t, _ in seen}
    assert providers_api.FACT_CREDENTIAL_STATE_CHANGED in types
    assert providers_api.FACT_PROVIDER_MUTATED not in types, "generation/revision unchanged — must not fire mutated"


def test_publish_provider_facts_fires_mutated_for_a_provider_that_disappears():
    providers_api._last_provider_projection.clear()
    base = {
        "id": "pf4", "generation": "g1", "revision": 1, "suspended": False,
        "credential_status": "available", "login_state": {},
    }
    _run(providers_api._publish_provider_facts({"providers": [base]}))
    seen: list[tuple[str, dict]] = []

    async def _capture(event: BusEvent) -> None:
        seen.append((event.type, event.payload))

    bus.subscribe("provider.*", _capture, name="test:publish_provider_facts:disappear")
    try:
        _run(providers_api._publish_provider_facts({"providers": []}))
    finally:
        bus.unsubscribe("test:publish_provider_facts:disappear")
    assert ("provider.mutated", {"provider_id": "pf4"}) in seen
    assert "pf4" not in providers_api._last_provider_projection


def test_login_status_to_phase_covers_every_provider_auth_state():
    import provider_auth
    expected_states = {
        provider_auth.STATE_LOGIN_RUNNING, provider_auth.STATE_LOGOUT_RUNNING,
        provider_auth.STATE_LOGIN_SUCCESS, provider_auth.STATE_LOGGED_OUT,
        provider_auth.STATE_LOGIN_FAILED, provider_auth.STATE_LOGOUT_FAILED,
    }
    assert set(providers_api._LOGIN_STATUS_TO_PHASE) == expected_states
    # Every mapped value must be a real LoginPhase.
    for phase in providers_api._LOGIN_STATUS_TO_PHASE.values():
        LoginPhase(phase)
    # STATE_IDLE deliberately unmapped — "no active/recent flow" is not a phase.
    assert provider_auth.STATE_IDLE not in providers_api._LOGIN_STATUS_TO_PHASE


def test_broadcast_install_publishes_fact_with_full_run_snapshot():
    async def _broadcast_global(event_type, data):
        return None

    providers_api.configure(_broadcast_global)
    run = {
        "kind": "codex", "label": "Codex", "command": "npm i -g codex", "state": "running",
        "lines": [{"s": "stdout", "t": "hi"}], "started_at": "t0", "finished_at": None,
        "returncode": None, "installed": None, "message": None,
    }
    seen: list[tuple[str, dict]] = []

    async def _capture(event: BusEvent) -> None:
        seen.append((event.type, event.payload))

    bus.subscribe("provider.install_progress_changed", _capture, name="test:broadcast_install:snapshot")
    try:
        with patch.object(providers_api.provider_setup, "get_install_runs", return_value={"codex": run}):
            _run(providers_api._broadcast_install(
                "provider_install_progress", {"kind": "codex", "stream": "stdout", "text": "hi"},
            ))
    finally:
        bus.unsubscribe("test:broadcast_install:snapshot")
    assert len(seen) == 1
    assert seen[0][1] == run, "fact must carry the FULL run snapshot, not the partial event delta"


def test_broadcast_install_publishes_nothing_when_the_run_already_vanished():
    async def _broadcast_global(event_type, data):
        return None

    providers_api.configure(_broadcast_global)
    seen: list[tuple[str, dict]] = []

    async def _capture(event: BusEvent) -> None:
        seen.append((event.type, event.payload))

    bus.subscribe("provider.install_progress_changed", _capture, name="test:broadcast_install:vanished")
    try:
        with patch.object(providers_api.provider_setup, "get_install_runs", return_value={}):
            _run(providers_api._broadcast_install(
                "provider_install_finished", {"kind": "codex", "state": "succeeded"},
            ))
    finally:
        bus.unsubscribe("test:broadcast_install:vanished")
    assert seen == []


def test_cancel_provider_login_unchecked_noop_when_nothing_running():
    providers_api._login_intent_by_provider.pop("no-such-flow", None)
    result = _run(providers_api._cancel_provider_login_unchecked("no-such-flow"))
    assert result == {"cancelled": False}


# ---------------------------------------------------------------------------
# runtime_profiles_api.py: model_catalog_changed reuse for D1
# ---------------------------------------------------------------------------

def test_runtime_profile_broadcast_publishes_model_catalog_changed_when_provider_given():
    seen: list[tuple[str, dict]] = []

    async def _capture(event: BusEvent) -> None:
        seen.append((event.type, event.payload))

    async def _global_broadcast(event: str, payload: dict) -> None:
        return None

    runtime_profiles_api.configure(_global_broadcast)
    bus.subscribe(providers_api.FACT_MODEL_CATALOG_CHANGED, _capture, name="test:runtime_profile_broadcast")
    try:
        _run(runtime_profiles_api._broadcast_changed(provider_id="prov-x"))
    finally:
        bus.unsubscribe("test:runtime_profile_broadcast")
    assert seen == [(providers_api.FACT_MODEL_CATALOG_CHANGED, {"provider_id": "prov-x"})]


def test_runtime_profile_broadcast_silent_when_no_provider_given():
    seen: list[tuple[str, dict]] = []

    async def _capture(event: BusEvent) -> None:
        seen.append((event.type, event.payload))

    async def _global_broadcast(event: str, payload: dict) -> None:
        return None

    runtime_profiles_api.configure(_global_broadcast)
    bus.subscribe(providers_api.FACT_MODEL_CATALOG_CHANGED, _capture, name="test:runtime_profile_broadcast_silent")
    try:
        _run(runtime_profiles_api._broadcast_changed())
    finally:
        bus.unsubscribe("test:runtime_profile_broadcast_silent")
    assert seen == []


# ---------------------------------------------------------------------------
# D4: adapter_api.py intent parser table + command-plane dispatch
# ---------------------------------------------------------------------------

def test_provider_intent_parser_table_round_trips_every_kind():
    base = {"intent_id": "i1", "session_id": None}
    cases = {
        "create_provider": {"provider_kind": "claude", "config": {"name": "x"}},
        "update_provider": {"provider_id": "p1", "config_patch": {"name": "y"}},
        "delete_provider": {"provider_id": "p1"},
        "suspend_provider": {"provider_id": "p1"},
        "resume_provider": {"provider_id": "p1"},
        "retry_credential": {"provider_id": "p1"},
        "begin_login": {"provider_id": "p1", "flow": "oauth_subscription"},
        "cancel_login": {"provider_id": "p1"},
        "refresh_models": {"provider_id": "p1"},
        "save_runtime_profile": {"profile": {"provider_id": "p1", "runner": "r"}},
        "delete_runtime_profile": {"runtime_profile_id": "rp1"},
    }
    assert set(cases) == set(adapter_api._PROVIDER_INTENT_PARSERS)
    for kind, extra in cases.items():
        data = {"kind": kind, **base, **extra}
        intent = adapter_api._parse_provider_intent(data)
        assert intent.intent_id == "i1"


def test_create_provider_wire_key_does_not_collide_with_dispatch_kind():
    # The outer "kind" ("create_provider") selects the parser; the
    # provider's own kind ("claude") must survive under "provider_kind"
    # without ever being shadowed by — or shadowing — the dispatch key.
    intent = adapter_api._parse_provider_intent({
        "kind": "create_provider", "intent_id": "i1", "session_id": None,
        "provider_kind": "claude", "config": {},
    })
    assert isinstance(intent, CreateProvider)
    assert intent.kind == "claude"


def test_suspend_and_resume_wire_kinds_map_to_opposite_suspended_flags():
    base = {"intent_id": "i1", "session_id": None, "provider_id": "p1"}
    suspend = adapter_api._parse_provider_intent({"kind": "suspend_provider", **base})
    resume = adapter_api._parse_provider_intent({"kind": "resume_provider", **base})
    assert isinstance(suspend, SuspendProvider) and suspend.suspended is True
    assert isinstance(resume, SuspendProvider) and resume.suspended is False


def test_parse_provider_intent_unknown_kind_raises():
    try:
        adapter_api._parse_provider_intent({"kind": "not_a_kind", "intent_id": "i1", "session_id": None})
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unknown provider intent kind")


class _StubPort:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def _record(self, name, *args):
        self.calls.append((name, args))

    def __getattr__(self, name):
        async def _method(*args):
            await self._record(name, *args)
        return _method


def test_submit_without_command_port_rejects():
    adapter = ProviderConfigSurfaceAdapter()
    ack = adapter.submit(CreateProvider(cv=1, intent_id="i1", session_id=None, kind="claude", config={}))
    assert isinstance(ack, IntentRejected)
    assert ack.code == "unsupported_contract_phase"


async def _submit_all_and_collect(adapter: ProviderConfigSurfaceAdapter, port: _StubPort) -> None:
    intents = [
        CreateProvider(cv=1, intent_id="i1", session_id=None, kind="claude", config={}),
        UpdateProvider(cv=1, intent_id="i2", session_id=None, provider_id="p1", config_patch={}),
        DeleteProvider(cv=1, intent_id="i3", session_id=None, provider_id="p1"),
        SuspendProvider(cv=1, intent_id="i4", session_id=None, provider_id="p1", suspended=True),
        RetryCredential(cv=1, intent_id="i5", session_id=None, provider_id="p1"),
        BeginLogin(cv=1, intent_id="i6", session_id=None, provider_id="p1", flow="oauth_subscription"),
        CancelLogin(cv=1, intent_id="i7", session_id=None, provider_id="p1"),
        RefreshModels(cv=1, intent_id="i8", session_id=None, provider_id="p1"),
        SaveRuntimeProfile(cv=1, intent_id="i9", session_id=None, profile={}),
        DeleteRuntimeProfile(cv=1, intent_id="i10", session_id=None, runtime_profile_id="rp1"),
    ]
    for intent in intents:
        ack = adapter.submit(intent)
        assert isinstance(ack, IntentAccepted), f"{type(intent).__name__} was rejected: {ack}"
        assert ack.intent_id == intent.intent_id
    # Let the fire-and-forget tasks this loop scheduled actually run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def test_submit_dispatches_every_intent_kind_to_the_command_port():
    adapter = ProviderConfigSurfaceAdapter()
    port = _StubPort()
    adapter._command_port = port
    _run(_submit_all_and_collect(adapter, port))
    dispatched = {name for name, _ in port.calls}
    assert dispatched == {
        "create_provider", "update_provider", "delete_provider", "suspend_provider",
        "retry_credential", "begin_login", "cancel_login", "refresh_models",
        "save_runtime_profile", "delete_runtime_profile",
    }


def test_submit_unsupported_intent_type_is_rejected_synchronously():
    adapter = ProviderConfigSurfaceAdapter()
    adapter._command_port = _StubPort()

    class _NotAProviderIntent:
        intent_id = "zzz"

    ack = adapter.submit(_NotAProviderIntent())  # type: ignore[arg-type]
    assert isinstance(ack, IntentRejected)
    assert ack.code == "unsupported"


def test_loopback_gated_provider_intents_are_exactly_login_start_and_cancel():
    assert adapter_api._LOOPBACK_GATED_PROVIDER_INTENTS == frozenset({"begin_login", "cancel_login"})


def test_provider_command_port_pushes_intent_rejected_on_http_exception():
    from fastapi import HTTPException

    adapter = ProviderConfigSurfaceAdapter()
    port = adapter_api.ProviderCommandPort(adapter)
    received: list[object] = []
    adapter.subscribe(received.append)

    async def _boom(*args, **kwargs):
        raise HTTPException(status_code=404, detail="nope")

    with patch.object(providers_api, "_delete_provider", _boom):
        # Must not raise — ProviderConfigSurfaceAdapter.submit() schedules
        # this fire-and-forget with nothing awaiting the result — but the
        # failure must reach the client as an async IntentRejected instead
        # of vanishing into a log line (the defect this closes).
        _run(port.delete_provider("intent-x", "missing-provider"))

    rejections = [f for f in received if isinstance(f, IntentRejected)]
    assert len(rejections) == 1, received
    assert rejections[0].intent_id == "intent-x"
    assert rejections[0].code == "404"
    assert rejections[0].message == "nope"


def test_provider_command_port_pushes_intent_rejected_on_value_error():
    adapter = ProviderConfigSurfaceAdapter()
    port = adapter_api.ProviderCommandPort(adapter)
    received: list[object] = []
    adapter.subscribe(received.append)

    async def _boom(*args, **kwargs):
        raise ValueError("bad config")

    with patch.object(providers_api, "_create_provider", _boom):
        _run(port.create_provider("intent-y", "claude", {}))

    rejections = [f for f in received if isinstance(f, IntentRejected)]
    assert len(rejections) == 1, received
    assert rejections[0].intent_id == "intent-y"
    assert rejections[0].code == "invalid_config"
    assert rejections[0].message == "bad config"


def test_provider_command_port_pushes_nothing_on_success():
    adapter = ProviderConfigSurfaceAdapter()
    port = adapter_api.ProviderCommandPort(adapter)
    received: list[object] = []
    adapter.subscribe(received.append)

    async def _ok(*args, **kwargs):
        return {"id": "p1"}

    with patch.object(providers_api, "_delete_provider", _ok):
        _run(port.delete_provider("intent-z", "p1"))

    assert not any(isinstance(f, IntentRejected) for f in received)


if __name__ == "__main__":
    import inspect

    module = sys.modules[__name__]
    tests = [
        (name, fn) for name, fn in vars(module).items()
        if name.startswith("test_") and inspect.isfunction(fn)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {exc}")
        else:
            print(f"PASS {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    if failed:
        raise SystemExit(1)
