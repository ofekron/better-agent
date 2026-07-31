#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.anyio


TEST_HOME = tempfile.mkdtemp(prefix="ba-headless-callers-")
os.environ["BETTER_AGENT_HOME"] = TEST_HOME
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import headless_admission  # noqa: E402
import provider as provider_module  # noqa: E402
from headless_request_contract import (  # noqa: E402
    HeadlessAuthority,
    HeadlessRequest,
    admit_headless_request,
)


class FakeExecutor:
    KIND = "claude"
    supports_fork = True
    supports_headless_no_tools = True
    reasoning_effort_options = ("high",)
    calls = []

    async def run_admitted_headless(self, admitted):
        self.calls.append(admitted.to_dict())
        return {"result": "ok", "session_id": "forked"}


def _session() -> dict:
    return {
        "id": "session-1",
        "provider_id": "provider-1",
        "model": "claude-model",
        "reasoning_effort": "high",
        "runner": "native",
        "node_id": "primary",
        "cwd": "/tmp/project",
        "agent_session_id": "agent-parent",
    }


def _provider(kind: str = "claude") -> dict:
    return {
        "id": "provider-1",
        "kind": kind,
        "generation": str(uuid.uuid4()),
        "revision": 3,
        "runner": "native",
    }


async def test_session_owner_is_frozen_for_every_caller_class() -> None:
    session = _session()
    provider = _provider()
    executor = FakeExecutor()
    original_snapshot = headless_admission._session_snapshot
    original_provider = headless_admission._provider_record
    original_executor = headless_admission._session_executor
    original_capabilities = headless_admission._provider_capabilities
    headless_admission._session_snapshot = lambda _sid: dict(session)
    headless_admission._provider_record = lambda _pid: dict(provider)
    headless_admission._session_executor = lambda *_args: executor
    headless_admission._provider_capabilities = lambda *_args: executor
    try:
        for permission_scope in (
            "internal_generation",
            "spawn_runs",
            "task_assessment",
        ):
            result = await headless_admission.run_session_headless(
                "session-1",
                prompt="prompt",
                fork=permission_scope == "internal_generation",
                resume=permission_scope == "spawn_runs",
                no_tools=permission_scope != "spawn_runs",
                timeout=30,
                permission_scope=permission_scope,
            )
            assert result["result"] == "ok"
    finally:
        headless_admission._session_snapshot = original_snapshot
        headless_admission._provider_record = original_provider
        headless_admission._session_executor = original_executor
        headless_admission._provider_capabilities = original_capabilities

    assert len(executor.calls) == 3
    for call in executor.calls:
        assert call["owner"] == {"kind": "session", "id": "session-1"}
        assert call["provider"]["id"] == "provider-1"
        assert call["model"] == "claude-model"
        assert call["runner"] == "native"
        assert call["routing"] == {"node_id": "primary"}
    assert executor.calls[0]["resume_sid"] == "agent-parent"
    assert executor.calls[1]["resume_sid"] == "agent-parent"
    assert executor.calls[2]["resume_sid"] is None


async def test_stale_selectors_and_missing_authority_fail_closed() -> None:
    session = _session()
    provider = _provider()
    original_snapshot = headless_admission._session_snapshot
    original_provider = headless_admission._provider_record
    original_executor = headless_admission._session_executor
    original_capabilities = headless_admission._provider_capabilities
    headless_admission._session_snapshot = lambda _sid: dict(session)
    headless_admission._provider_record = lambda _pid: dict(provider)
    headless_admission._session_executor = lambda *_args: FakeExecutor()
    headless_admission._provider_capabilities = lambda *_args: FakeExecutor()
    try:
        for expected, message in (
            ({"expected_provider_id": "stale"}, "provider"),
            ({"expected_model": "stale"}, "model"),
        ):
            try:
                await headless_admission.run_session_headless(
                    "session-1",
                    prompt="prompt",
                    fork=False,
                    resume=False,
                    no_tools=False,
                    timeout=30,
                    permission_scope="spawn_runs",
                    **expected,
                )
            except ValueError as exc:
                assert message in str(exc)
            else:
                raise AssertionError(f"expected stale {message} rejection")
        session["provider_id"] = ""
        try:
            await headless_admission.run_session_headless(
                "session-1",
                prompt="prompt",
                fork=False,
                resume=False,
                no_tools=False,
                timeout=30,
                permission_scope="spawn_runs",
            )
        except ValueError as exc:
            assert "provider" in str(exc)
        else:
            raise AssertionError("missing provider must not use a default")
    finally:
        headless_admission._session_snapshot = original_snapshot
        headless_admission._provider_record = original_provider
        headless_admission._session_executor = original_executor
        headless_admission._provider_capabilities = original_capabilities


async def test_generic_provider_consumes_admitted_authority() -> None:
    record = _provider() | {
        "default_model": "claude-model",
        "custom_models": ["claude-model"],
    }

    class LegacyProvider:
        KIND = "claude"
        id = record["id"]
        supports_fork = True
        supports_headless_no_tools = True
        supports_reasoning_effort = True
        reasoning_effort_options = ("high",)
        run_admitted_headless = provider_module.Provider.run_admitted_headless
        _run_admitted_headless_legacy = (
            provider_module.Provider._run_admitted_headless_legacy
        )

        def __init__(self):
            self.record = dict(record)
            self._execution_record = threading.local()
            self._headless_authority_lock = asyncio.Lock()
            self.seen = None

        async def run_headless(self, **kwargs):
            self.seen = (
                kwargs,
                dict(self._execution_record.value),
            )
            return {"result": "legacy-ok"}

    def admitted(model: str):
        request = HeadlessRequest.from_dict({
            "schema": 1,
            "prompt": "prompt",
            "owner": {"kind": "session", "id": "session-1"},
            "fork": False,
            "no_tools": True,
            "timeout": 30,
        })
        return admit_headless_request(
            request,
            HeadlessAuthority.create(
                owner_kind="session",
                owner_id="session-1",
                provider={
                    key: record[key]
                    for key in ("id", "kind", "generation", "revision")
                },
                model=model,
                reasoning_effort="high",
                runner="native",
                permission_scope="internal_generation",
                routing={"node_id": "primary"},
                cwd="/tmp/project",
                resume_sid=None,
                supports_fork=True,
                supports_no_tools=True,
            ),
        )

    class Hydration:
        def runtime_record(self):
            return dict(record) | {"api_key": "runtime-secret"}

    original_hydrate = provider_module.config_store.hydrate_provider_execution
    hydration_calls = 0

    def hydrate(*_args, **_kwargs):
        nonlocal hydration_calls
        hydration_calls += 1
        return Hydration()

    provider_module.config_store.hydrate_provider_execution = hydrate
    instance = LegacyProvider()
    try:
        result = await instance.run_admitted_headless(admitted("claude-model"))
        assert result == {"result": "legacy-ok"}
        assert instance.seen[0]["no_tools"] is True
        assert instance.seen[1]["default_model"] == "claude-model"
        assert "runtime-secret" not in str(result)
        try:
            await instance.run_admitted_headless(admitted("stale-model"))
        except ValueError as exc:
            assert "model" in str(exc)
        else:
            raise AssertionError("stale model must fail")
    finally:
        provider_module.config_store.hydrate_provider_execution = original_hydrate
    assert hydration_calls == 1


def test_legacy_caller_bypasses_are_removed() -> None:
    backend = Path(__file__).resolve().parents[1]
    main_source = (backend / "main.py").read_text(encoding="utf-8")
    assessor_source = (backend / "task_assessor.py").read_text(encoding="utf-8")
    rpc_source = (backend / "node_rpc_handlers.py").read_text(encoding="utf-8")
    sdk_source = (
        backend.parent / "sdk" / "better_agent_sdk" / "client.py"
    ).read_text(encoding="utf-8")
    assert "default_provider().run_headless" not in main_source
    assert "provider.run_headless(" not in main_source
    assert "_resolve_judge_provider" not in assessor_source
    assert ".run_headless(" not in assessor_source
    assert "def _rpc_run_headless(" not in rpc_source
    assert '"run_headless": _rpc_run_headless' not in rpc_source
    run_headless = sdk_source[sdk_source.index("    def run_headless("):]
    run_headless = run_headless[:run_headless.index("\n    def ", 5)]
    assert '"app_session_id": self.app_session_id' in run_headless
    assert "resume_sid" not in run_headless
    assert "session_id:" not in run_headless


async def test_tombstoned_profile_never_blocks_turns() -> None:
    """R1.3(c): turns run off stamped session fields — a session whose
    runtime profile was deleted keeps admitting headless runs."""
    session = {**_session(), "runtime_profile_id": "deleted-profile-id"}
    provider = _provider()
    executor = FakeExecutor()
    original_snapshot = headless_admission._session_snapshot
    original_provider = headless_admission._provider_record
    original_executor = headless_admission._session_executor
    original_capabilities = headless_admission._provider_capabilities
    headless_admission._session_snapshot = lambda _sid: dict(session)
    headless_admission._provider_record = lambda _pid: dict(provider)
    headless_admission._session_executor = lambda *_args: executor
    headless_admission._provider_capabilities = lambda *_args: executor
    try:
        result = await headless_admission.run_session_headless(
            "session-1",
            prompt="prompt",
            fork=False,
            resume=True,
            no_tools=True,
            timeout=30,
            permission_scope="internal_generation",
        )
        assert result["result"] == "ok"
    finally:
        headless_admission._session_snapshot = original_snapshot
        headless_admission._provider_record = original_provider
        headless_admission._session_executor = original_executor
        headless_admission._provider_capabilities = original_capabilities
    assert executor.calls[-1]["runner"] == "native"


async def test_non_claude_kind_headless_uses_profile_default_model() -> None:
    """Cross-provider parity: the no-model-override guard for generic kinds
    compares against the PROFILE default (provider records no longer carry
    default_model), so a matching admitted model must pass and a mismatched
    one must fail."""
    import config_store

    created = config_store.add_provider({
        "name": "Kimi Headless",
        "kind": "kimi",
        "mode": "api_key",
        "default_model": "kimi-headless-model",
        "custom_models": ["kimi-headless-model"],
    })
    stored = next(
        p
        for p in config_store._load_state()["providers"]
        if p["id"] == created["id"]
    )
    record = dict(stored) | {"runner": "native"}
    profile_model = config_store.provider_execution_defaults(created["id"])[
        "default_model"
    ]
    assert profile_model == "kimi-headless-model"

    class KimiProvider:
        KIND = "kimi"
        id = record["id"]
        supports_fork = True
        supports_headless_no_tools = True
        supports_reasoning_effort = False
        reasoning_effort_options = ()
        run_admitted_headless = provider_module.Provider.run_admitted_headless
        _run_admitted_headless_legacy = (
            provider_module.Provider._run_admitted_headless_legacy
        )

        def __init__(self):
            self.record = dict(record)
            self._execution_record = threading.local()
            self._headless_authority_lock = asyncio.Lock()

        async def run_headless(self, **kwargs):
            return {"result": "kimi-ok"}

    def admitted(model: str):
        request = HeadlessRequest.from_dict({
            "schema": 1,
            "prompt": "prompt",
            "owner": {"kind": "session", "id": "session-1"},
            "fork": False,
            "no_tools": True,
            "timeout": 30,
        })
        return admit_headless_request(
            request,
            HeadlessAuthority.create(
                owner_kind="session",
                owner_id="session-1",
                provider={
                    key: record[key]
                    for key in ("id", "kind", "generation", "revision")
                },
                model=model,
                reasoning_effort="",
                runner="native",
                permission_scope="internal_generation",
                routing={"node_id": "primary"},
                cwd="/tmp/project",
                resume_sid=None,
                supports_fork=True,
                supports_no_tools=True,
            ),
        )

    class Hydration:
        def runtime_record(self):
            return dict(record)

    original_hydrate = provider_module.config_store.hydrate_provider_execution
    provider_module.config_store.hydrate_provider_execution = (
        lambda *a, **k: Hydration()
    )
    instance = KimiProvider()
    try:
        import models as models_mod

        original_models = models_mod.models_for_record
        models_mod.models_for_record = lambda _r: ["kimi-headless-model"]
        try:
            result = await instance.run_admitted_headless(
                admitted("kimi-headless-model")
            )
            assert result == {"result": "kimi-ok"}
            try:
                await instance.run_admitted_headless(admitted("other-model"))
            except ValueError as exc:
                assert "model" in str(exc)
            else:
                raise AssertionError("mismatched model must be rejected")

            # R1.3(c): tombstoning the pair profile must not block turns
            # whose admitted model matches the (still resolvable) tombstone.
            profile_id = config_store.provider_execution_defaults(created["id"])[
                "runtime_profile_id"
            ]
            deleted_ok, deleted_reason = config_store.delete_runtime_profile(
                profile_id
            )
            assert deleted_ok, deleted_reason
            result = await instance.run_admitted_headless(
                admitted("kimi-headless-model")
            )
            assert result == {"result": "kimi-ok"}, (
                "tombstoned pair profile must not block admitted turns"
            )

            # A pair with no profile history has no configured model to
            # protect — the guard is vacuous, not a rejection. Fabricate
            # zero-history by dropping the provider's profile rows from the
            # persisted store.
            import json as _json

            config_path = config_store._config_path()
            raw = _json.loads(Path(config_path).read_text())
            raw["runtime_profiles"] = [
                p
                for p in raw["runtime_profiles"]
                if p["provider_id"] != created["id"]
            ]
            Path(config_path).write_text(_json.dumps(raw))
            with config_store._state_cache_lock:
                config_store._state_cache = None
            result = await instance.run_admitted_headless(
                admitted("kimi-headless-model")
            )
            assert result == {"result": "kimi-ok"}, (
                "absent pair profile must not block admitted turns"
            )
        finally:
            models_mod.models_for_record = original_models
    finally:
        provider_module.config_store.hydrate_provider_execution = original_hydrate


async def main_async() -> None:
    await test_session_owner_is_frozen_for_every_caller_class()
    await test_stale_selectors_and_missing_authority_fail_closed()
    await test_generic_provider_consumes_admitted_authority()
    test_legacy_caller_bypasses_are_removed()
    await test_tombstoned_profile_never_blocks_turns()
    await test_non_claude_kind_headless_uses_profile_default_model()


def main() -> None:
    try:
        asyncio.run(main_async())
    finally:
        shutil.rmtree(TEST_HOME, ignore_errors=True)
    print("headless caller admission: ok")


if __name__ == "__main__":
    main()
