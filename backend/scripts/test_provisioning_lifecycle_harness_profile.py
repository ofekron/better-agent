#!/usr/bin/env python3
"""`ProvisionedSessionSpec.harness_profile_id` threads through
`provisioning.lifecycle._create_session` / `ensure_caller` into
`session_manager.create`'s `harness_profile_id` kwarg.

Before the fix, `ProvisionedSessionSpec` had no `harness_profile_id` field
and neither lifecycle call site passed one, so a spec-scoped harness profile
(e.g. the get-requirements processor's minimal profile) could never be
pinned — every provisioned session silently inherited the full Default
profile's skills/instructions."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="provisioning_harness_profile_test_home_")
os.environ["BETTER_AGENT_HOME"] = _TMP_HOME
os.environ.setdefault("BETTER_CLAUDE_HOME", _TMP_HOME)
os.environ["BETTER_AGENT_TEST_MODE"] = "1"

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND))

from provisioning.config import ProvisionedConfig  # noqa: E402
from provisioning.lifecycle import _create_session, ensure_caller  # noqa: E402
from provisioning.spec import ProvisionedSessionSpec  # noqa: E402


class _Spec(ProvisionedSessionSpec):
    key = "harness_profile_thread_test"
    env_prefix = "HARNESS_PROFILE_THREAD_TEST"
    name = "worker:harness-profile-thread-test"
    storage_scope = None
    harness_profile_id = "ofek-dev.requirements.processor"


def _cfg() -> ProvisionedConfig:
    return ProvisionedConfig(
        cwd="/repo",
        model="model",
        provider_id="provider",
        reasoning_effort="",
        run_mode="fork",
        dispatch="http",
        on_no_fork="error",
        node_id="primary",
        backend_url="http://localhost:8000",
        internal_token="token",
        provisioned_session_id=None,
        caller_session_id=None,
        worker_description="worker:harness-profile-thread-test",
    )


class _FakeSessionManager:
    def __init__(self) -> None:
        self.create_calls: list[dict] = []
        self._next_id = 0

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        self._next_id += 1
        return {
            "id": f"sess-{self._next_id}",
            "orchestration_mode": kwargs.get("orchestration_mode") or "native",
            "agent_session_id": None,
            "node_id": kwargs.get("node_id") or "primary",
        }

    def delete(self, _session_id):
        pass


class _FakeWorkerStore:
    @staticmethod
    def upsert_worker(**_kwargs):
        pass


def _install_fakes():
    fake_session_manager_module = type(sys)("session_manager")
    fake_session_manager_module.manager = _FakeSessionManager()

    fake_working_mode_module = type(sys)("working_mode")
    fake_working_mode_module.find_working_session = lambda *a, **kw: None
    fake_working_mode_module.mark_working_mode = lambda *a, **kw: None

    fake_worker_store_module = type(sys)("stores.worker_store")
    fake_worker_store_module.upsert_worker = _FakeWorkerStore.upsert_worker
    fake_stores_pkg = sys.modules.get("stores") or type(sys)("stores")
    fake_stores_pkg.worker_store = fake_worker_store_module

    saved = {
        "session_manager": sys.modules.get("session_manager"),
        "working_mode": sys.modules.get("working_mode"),
        "stores": sys.modules.get("stores"),
        "stores.worker_store": sys.modules.get("stores.worker_store"),
    }
    sys.modules["session_manager"] = fake_session_manager_module
    sys.modules["working_mode"] = fake_working_mode_module
    sys.modules["stores"] = fake_stores_pkg
    sys.modules["stores.worker_store"] = fake_worker_store_module
    return fake_session_manager_module.manager, saved


def _restore(saved: dict) -> None:
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def test_spec_has_harness_profile_id_field():
    base_spec = ProvisionedSessionSpec()
    assert base_spec.harness_profile_id == ""


def test_create_session_threads_harness_profile_id():
    fake_manager, saved = _install_fakes()
    try:
        spec = _Spec()
        cfg = _cfg()
        session_id = _create_session(spec, cfg)
        assert session_id == "sess-1"
        assert len(fake_manager.create_calls) == 1
        assert fake_manager.create_calls[0]["harness_profile_id"] == "ofek-dev.requirements.processor"
    finally:
        _restore(saved)


def test_ensure_caller_threads_harness_profile_id():
    fake_manager, saved = _install_fakes()
    try:
        spec = _Spec()
        cfg = _cfg()
        session_id = ensure_caller(spec, cfg)
        assert session_id == "sess-1"
        assert len(fake_manager.create_calls) == 1
        assert fake_manager.create_calls[0]["harness_profile_id"] == "ofek-dev.requirements.processor"
    finally:
        _restore(saved)


def test_default_spec_threads_none_harness_profile_id():
    fake_manager, saved = _install_fakes()
    try:
        class _DefaultSpec(ProvisionedSessionSpec):
            key = "harness_profile_thread_default_test"
            env_prefix = "HARNESS_PROFILE_THREAD_DEFAULT_TEST"
            name = "worker:harness-profile-thread-default-test"
            storage_scope = None

        spec = _DefaultSpec()
        cfg = _cfg()
        _create_session(spec, cfg)
        assert fake_manager.create_calls[0]["harness_profile_id"] is None
    finally:
        _restore(saved)


if __name__ == "__main__":
    try:
        test_spec_has_harness_profile_id_field()
        test_create_session_threads_harness_profile_id()
        test_ensure_caller_threads_harness_profile_id()
        test_default_spec_threads_none_harness_profile_id()
        print("OK")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
