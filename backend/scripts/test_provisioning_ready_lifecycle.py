from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path


BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from provisioning import manager
from provisioning.config import ProvisionedConfig
from provisioning.spec import ProvisionedSessionSpec


class _Spec(ProvisionedSessionSpec):
    key = "ready_lifecycle_test"
    env_prefix = "READY_LIFECYCLE_TEST"
    name = "worker:ready-lifecycle"
    provision_timeout = 2.0
    retry_attempts = 1

    def build_config(self, *, model=None):
        return ProvisionedConfig(
            cwd="/repo",
            model=model or "model",
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
            worker_description="worker:ready-lifecycle",
        )

    def build_instructions(self, query, ctx):
        return query

    def build_provision_prompt(self, ctx):
        return "provision"

    def parse_result(self, text, ctx):
        return text


class _SessionManager:
    def __init__(self) -> None:
        self.base = {
            "id": "base",
            "agent_session_id": None,
            "orchestration_mode": "native",
        }
        self.block_persist = False
        self.persist_started = threading.Event()
        self.persist_release = threading.Event()

    def get(self, session_id):
        return dict(self.base) if session_id == "base" else None

    def set_agent_sid(self, session_id, _mode, sid, **_kwargs):
        assert session_id == "base"
        if self.block_persist:
            self.persist_started.set()
            self.persist_release.wait()
        self.base["agent_session_id"] = sid


class _Coordinator:
    def __init__(self, sessions: _SessionManager) -> None:
        self.sessions = sessions
        self.init_cancel_events = {}
        self.init_calls = 0
        self.fail_once = True
        self.block_init: asyncio.Event | None = None

    async def _init_target_agent_session(self, **_kwargs):
        self.init_calls += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("runtime dependency unavailable")
        if self.block_init is not None:
            await self.block_init.wait()
        return "provider-sid"


async def _run_tests() -> None:
    sessions = _SessionManager()
    coordinator = _Coordinator(sessions)
    fake_session_module = type(sys)("session_manager")
    fake_session_module.manager = sessions
    fake_main_module = type(sys)("main")
    fake_main_module.coordinator = coordinator
    saved_modules = (sys.modules.get("session_manager"), sys.modules.get("main"))
    saved = (
        manager.ensure_session,
        manager.ensure_caller,
        manager.dispatch,
    )
    dispatches: list[str] = []

    async def _dispatch(_spec, _cfg, *, base_session_id, caller_session_id, **_kwargs):
        assert sessions.base["agent_session_id"] == "provider-sid"
        assert base_session_id == "base"
        assert caller_session_id == "caller"
        dispatches.append(base_session_id)
        return {"success": True, "sdk_output": "ok", "session_id": "fork-sid"}

    try:
        sys.modules["session_manager"] = fake_session_module
        sys.modules["main"] = fake_main_module
        manager.ensure_session = lambda _spec, _cfg: "base"
        manager.ensure_caller = lambda _spec, _cfg: "caller"
        manager.dispatch = _dispatch
        spec = _Spec()

        try:
            await manager.run(spec, "first")
        except RuntimeError as exc:
            assert str(exc) == "runtime dependency unavailable"
        else:
            raise AssertionError("failed initialization unexpectedly dispatched")
        assert dispatches == []
        assert sessions.base["agent_session_id"] is None

        repaired = await manager.run(spec, "second")
        assert repaired.value == "ok"
        assert coordinator.init_calls == 2
        assert dispatches == ["base"]
        assert sessions.base["agent_session_id"] == "provider-sid"

        reused = await manager.run(spec, "third")
        assert reused.value == "ok"
        assert coordinator.init_calls == 2

        sessions.base["agent_session_id"] = None
        coordinator.block_init = asyncio.Event()
        first = asyncio.create_task(manager.run(spec, "concurrent-1"))
        await asyncio.sleep(0)
        second = asyncio.create_task(manager.ensure_warm_base(spec, spec.build_config()))
        await asyncio.sleep(0.05)
        assert coordinator.init_calls == 3
        coordinator.block_init.set()
        result, base_id = await asyncio.gather(first, second)
        assert result.value == "ok"
        assert base_id == "base"
        assert coordinator.init_calls == 3

        sessions.base["agent_session_id"] = None
        sessions.block_persist = True
        sessions.persist_started.clear()
        sessions.persist_release.clear()
        coordinator.block_init = None
        cancelled = asyncio.create_task(manager.run(spec, "cancelled"))
        await asyncio.wait_for(
            asyncio.to_thread(sessions.persist_started.wait),
            timeout=1,
        )
        cancelled.cancel()
        follower = asyncio.create_task(manager.ensure_warm_base(spec, spec.build_config()))
        await asyncio.sleep(0.05)
        assert not follower.done()
        sessions.persist_release.set()
        try:
            await cancelled
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled lifecycle unexpectedly completed")
        assert await follower == "base"
        assert sessions.base["agent_session_id"] == "provider-sid"
        assert coordinator.init_calls == 4
        sessions.block_persist = False

        loop_thread = threading.get_ident()
        lifecycle_threads: list[int] = []
        sessions.base["agent_session_id"] = "provider-sid"

        def _threaded_base(_spec, _cfg):
            lifecycle_threads.append(threading.get_ident())
            return "base"

        manager.ensure_session = _threaded_base
        await manager.run(spec, "off-loop")
        assert lifecycle_threads and lifecycle_threads[-1] != loop_thread
    finally:
        manager.ensure_session, manager.ensure_caller, manager.dispatch = saved
        old_session, old_main = saved_modules
        if old_session is None:
            sys.modules.pop("session_manager", None)
        else:
            sys.modules["session_manager"] = old_session
        if old_main is None:
            sys.modules.pop("main", None)
        else:
            sys.modules["main"] = old_main


def main() -> int:
    asyncio.run(_run_tests())
    print("PASS: provisioned runs enforce provider readiness and self-heal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
