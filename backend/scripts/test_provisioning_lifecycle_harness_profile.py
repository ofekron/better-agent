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


_MISSING = object()


def _install_fakes():
    fake_session_manager_module = type(sys)("session_manager")
    fake_session_manager_module.manager = _FakeSessionManager()

    fake_working_mode_module = type(sys)("working_mode")
    fake_working_mode_module.find_working_session = lambda *a, **kw: None
    fake_working_mode_module.mark_working_mode = lambda *a, **kw: None

    fake_worker_store_module = type(sys)("stores.worker_store")
    fake_worker_store_module.upsert_worker = _FakeWorkerStore.upsert_worker
    real_stores_pkg = sys.modules.get("stores")
    stores_pkg = real_stores_pkg if real_stores_pkg is not None else type(sys)("stores")
    # `stores_pkg` is the REAL package object whenever "stores" was already
    # imported elsewhere in this pytest process (the common case once other
    # test files have run) — mutating its `.worker_store` attribute mutates
    # the shared singleton in place. Save that attribute's prior value
    # explicitly so `_restore` can put it back; restoring only
    # `sys.modules["stores"]` is a no-op here since it's the same object.
    saved_worker_store_attr = getattr(stores_pkg, "worker_store", _MISSING)
    stores_pkg.worker_store = fake_worker_store_module

    saved = {
        "session_manager": sys.modules.get("session_manager"),
        "working_mode": sys.modules.get("working_mode"),
        "stores": real_stores_pkg,
        "stores.worker_store": sys.modules.get("stores.worker_store"),
    }
    sys.modules["session_manager"] = fake_session_manager_module
    sys.modules["working_mode"] = fake_working_mode_module
    sys.modules["stores"] = stores_pkg
    sys.modules["stores.worker_store"] = fake_worker_store_module
    return fake_session_manager_module.manager, saved, (stores_pkg, saved_worker_store_attr)


def _restore(saved: dict, stores_attr: tuple | None = None) -> None:
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    if stores_attr is not None:
        stores_pkg, saved_worker_store_attr = stores_attr
        if saved_worker_store_attr is _MISSING:
            if hasattr(stores_pkg, "worker_store"):
                delattr(stores_pkg, "worker_store")
        else:
            stores_pkg.worker_store = saved_worker_store_attr


def test_spec_has_harness_profile_id_field():
    base_spec = ProvisionedSessionSpec()
    assert base_spec.harness_profile_id == ""


def test_create_session_threads_harness_profile_id():
    fake_manager, saved, stores_attr = _install_fakes()
    try:
        spec = _Spec()
        cfg = _cfg()
        session_id = _create_session(spec, cfg)
        assert session_id == "sess-1"
        assert len(fake_manager.create_calls) == 1
        assert fake_manager.create_calls[0]["harness_profile_id"] == "ofek-dev.requirements.processor"
    finally:
        _restore(saved, stores_attr)


def test_ensure_caller_threads_harness_profile_id():
    fake_manager, saved, stores_attr = _install_fakes()
    try:
        spec = _Spec()
        cfg = _cfg()
        session_id = ensure_caller(spec, cfg)
        assert session_id == "sess-1"
        assert len(fake_manager.create_calls) == 1
        assert fake_manager.create_calls[0]["harness_profile_id"] == "ofek-dev.requirements.processor"
    finally:
        _restore(saved, stores_attr)


def test_default_spec_threads_none_harness_profile_id():
    fake_manager, saved, stores_attr = _install_fakes()
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
        _restore(saved, stores_attr)


def test_install_fakes_does_not_leak_fake_worker_store_onto_real_package() -> None:
    """Regression: `_install_fakes` used to mutate the real `stores` package's
    `.worker_store` attribute in place (when `stores` was already imported
    elsewhere in the process) and `_restore` never put it back — every test
    in later files that did `from stores import worker_store` in the same
    pytest process then got the tiny fake (only `upsert_worker`), not the
    real module, and blew up with AttributeError on any other attribute
    (e.g. `worker_count`)."""
    import types as _types
    import stores as _real_stores_pkg  # noqa: F401  (ensures "stores" is a real, already-imported package)

    probe_worker_store = _types.ModuleType("stores.worker_store")
    probe_worker_store.worker_count = lambda cwd="": 0
    real_stores_pkg = sys.modules["stores"]
    original_pkg_attr = getattr(real_stores_pkg, "worker_store", _MISSING)
    original_sys_modules_entry = sys.modules.get("stores.worker_store")
    real_stores_pkg.worker_store = probe_worker_store
    sys.modules["stores.worker_store"] = probe_worker_store
    try:
        _fake_manager, saved, stores_attr = _install_fakes()
        _restore(saved, stores_attr)

        from stores import worker_store as post_restore_worker_store

        assert post_restore_worker_store is probe_worker_store
        assert hasattr(post_restore_worker_store, "worker_count")
    finally:
        if original_pkg_attr is _MISSING:
            if hasattr(real_stores_pkg, "worker_store"):
                delattr(real_stores_pkg, "worker_store")
        else:
            real_stores_pkg.worker_store = original_pkg_attr
        if original_sys_modules_entry is None:
            sys.modules.pop("stores.worker_store", None)
        else:
            sys.modules["stores.worker_store"] = original_sys_modules_entry


if __name__ == "__main__":
    try:
        test_spec_has_harness_profile_id_field()
        test_create_session_threads_harness_profile_id()
        test_ensure_caller_threads_harness_profile_id()
        test_default_spec_threads_none_harness_profile_id()
        test_install_fakes_does_not_leak_fake_worker_store_onto_real_package()
        print("OK")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
