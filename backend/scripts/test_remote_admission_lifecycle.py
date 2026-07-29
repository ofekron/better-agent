from __future__ import annotations

import asyncio
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

import _test_home

TEST_HOME = Path(_test_home.isolate("ba-test-remote-admission-"))

from provider_remote import (  # noqa: E402
    RemoteProviderProxy,
    _RemoteRunState,
    _on_run_control,
    fail_connection_admissions,
    resolve_remote_admission,
    remote_admission_lease_seconds,
)
import provider_remote  # noqa: E402
import node_rpc_handlers  # noqa: E402
import execution_spawn_authority  # noqa: E402
from execution_template import prepare_execution  # noqa: E402
from runs_dir import runs_root  # noqa: E402


def state(run_id: str) -> _RemoteRunState:
    return _RemoteRunState(
        run_id=run_id,
        run_dir=TEST_HOME / run_id,
        mode="native",
        app_session_id="session",
        queue=asyncio.Queue(),
        node_id="node",
        loop=asyncio.new_event_loop(),
    )


def main() -> None:
    os.environ["BETTER_AGENT_REMOTE_ADMISSION_LEASE_SECONDS"] = "0"
    try:
        remote_admission_lease_seconds()
    except RuntimeError:
        pass
    else:
        raise AssertionError("invalid remote admission lease was accepted")
    os.environ.pop("BETTER_AGENT_REMOTE_ADMISSION_LEASE_SECONDS")
    created: list[_RemoteRunState] = []
    def create(run_id: str) -> _RemoteRunState:
        item = state(run_id)
        created.append(item)
        return item

    admitted = create("admitted")
    assert resolve_remote_admission(admitted, admitted=True)
    assert admitted.admission.result() is True

    failed = create("failed")
    assert resolve_remote_admission(failed, error=RuntimeError("node error"))
    try:
        failed.admission.result()
    except RuntimeError:
        pass
    else:
        raise AssertionError("node error did not fail admission")

    cancelled = create("cancelled")
    assert resolve_remote_admission(cancelled, admitted=False)
    assert not resolve_remote_admission(cancelled, admitted=True)
    assert cancelled.admission.result() is False

    disconnected = create("disconnected")
    conn = type("Connection", (), {"runs": {"disconnected": disconnected}})()
    fail_connection_admissions(conn, "connection authority expired")
    try:
        disconnected.admission.result()
    except RuntimeError:
        pass
    else:
        raise AssertionError("disconnect did not fail admission")
    assert not resolve_remote_admission(disconnected, admitted=True)

    concurrent = create("concurrent")
    barrier = threading.Barrier(9)
    results: list[bool] = []

    def resolve(index: int) -> None:
        barrier.wait()
        results.append(resolve_remote_admission(
            concurrent,
            admitted=index == 0,
            error=None if index == 0 else RuntimeError(str(index)),
        ))

    writers = [threading.Thread(target=resolve, args=(index,)) for index in range(8)]
    for writer in writers:
        writer.start()
    barrier.wait()
    for writer in writers:
        writer.join()
    assert results.count(True) == 1

    for item in created:
        item.loop.close()
    asyncio.run(executable_dispatch_and_expiry())
    asyncio.run(executable_malformed_handler())
    asyncio.run(executable_handler_exits())
    asyncio.run(executable_real_proxy_start())
    print("PASS remote admission lifecycle")


async def executable_dispatch_and_expiry() -> None:
    current_loop = asyncio.get_running_loop()
    pending = _RemoteRunState(
        run_id="pending",
        run_dir=TEST_HOME / "pending",
        mode="native",
        app_session_id="session",
        queue=asyncio.Queue(),
        node_id="node-test",
        loop=current_loop,
    )
    renewals: list[str] = []

    class Proxy:
        node_id = "node-test"

        def __init__(self) -> None:
            self._runs = {"pending": pending}
            self._lock = threading.Lock()

        def _renew_admission_lease(self, _state, token) -> bool:
            renewals.append(token)
            return token == pending.admission_lease_token

    proxy = Proxy()
    conn = type("Connection", (), {"runs": {"pending": pending}})()
    original_proxy = provider_remote._proxies.get("node-test")
    original_connection = provider_remote.node_store.get_connection
    provider_remote._proxies["node-test"] = proxy
    provider_remote.node_store.get_connection = lambda _node_id: conn
    try:
        await _on_run_control(
            node_id="node-test",
            run_id="pending",
            control_type="error",
            data={"error": "stale", "lease_token": "wrong"},
        )
        assert not pending.admission.done()
        await _on_run_control(
            node_id="node-test",
            run_id="pending",
            control_type="admission_renew",
            data={"lease_token": "wrong"},
        )
        assert renewals == ["wrong"]
        await _on_run_control(
            node_id="node-test",
            run_id="pending",
            control_type="admitted",
            data={"lease_token": pending.admission_lease_token},
        )
        assert pending.admission.result() is True

        scheduled: list[object] = []
        cancelled: list[str] = []

        class FakeHandle:
            def cancel(self) -> None:
                pass

        class FakeLoop:
            def call_later(self, _delay, callback):
                scheduled.append(callback)
                return FakeHandle()

        expiry = _RemoteRunState(
            run_id="expiry",
            run_dir=TEST_HOME / "expiry",
            mode="native",
            app_session_id="session",
            queue=asyncio.Queue(),
            node_id="node-test",
            loop=FakeLoop(),
        )
        proxy._runs["expiry"] = expiry
        conn.runs["expiry"] = expiry
        original_cancel = provider_remote.node_link.send_cancel_run

        async def cancel(_node_id: str, run_id: str) -> bool:
            cancelled.append(run_id)
            return True

        provider_remote.node_link.send_cancel_run = cancel
        try:
            RemoteProviderProxy._arm_admission_lease(proxy, expiry, conn)
            scheduled.pop()()
            await expiry.admission_expiry_cancel_task
            assert expiry.admission.done()
            assert "expiry" not in proxy._runs
            assert "expiry" not in conn.runs
            assert cancelled == ["expiry"]
            assert expiry.admission_expiry_cancel_task.done()
        finally:
            provider_remote.node_link.send_cancel_run = original_cancel

        cleanup = _RemoteRunState(
            run_id="cleanup",
            run_dir=TEST_HOME / "cleanup",
            mode="native",
            app_session_id="session",
            queue=asyncio.Queue(),
            node_id="node-test",
            loop=current_loop,
        )
        proxy._runs["cleanup"] = cleanup
        conn.runs["cleanup"] = cleanup
        fail_connection_admissions(conn, "disconnect")
        assert "cleanup" not in proxy._runs
        assert "cleanup" not in conn.runs
    finally:
        provider_remote.node_store.get_connection = original_connection
        if original_proxy is None:
            provider_remote._proxies.pop("node-test", None)
        else:
            provider_remote._proxies["node-test"] = original_proxy


async def executable_malformed_handler() -> None:
    controls: list[tuple[str, str, dict]] = []

    class Client:
        async def send_run_control(
            self,
            *,
            run_id: str,
            control_type: str,
            data: dict,
        ) -> None:
            controls.append((run_id, control_type, data))

    before = dict(node_rpc_handlers._ctx_by_run)
    await node_rpc_handlers.handle_spawn_run(Client(), {
        "run_id": "malformed-lease",
        "root_id": "root",
        "app_session_id": "session",
        "cwd": "/tmp",
        "admission_lease": {"token": "token", "seconds": 0},
    })
    assert node_rpc_handlers._ctx_by_run == before
    assert controls == [(
        "malformed-lease",
        "error",
        {
            "error": "invalid remote admission lease",
            "lease_token": "token",
        },
    )]


async def executable_handler_exits() -> None:
    class Artifact:
        provider_id = "provider"
        template = type("Template", (), {"arguments": staticmethod(lambda: {
            "run_id": "handler-run",
            "cwd": "/tmp",
            "app_session_id": "session",
        })})()

    class Execution:
        def __init__(self, admitted: bool) -> None:
            self.admitted = admitted

        def wait_for_admission(self) -> bool:
            return self.admitted

    class Provider:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        def cancel_run(self, run_id: str) -> None:
            self.cancelled.append(run_id)

    class Client:
        def __init__(self) -> None:
            self.controls: list[str] = []

        async def send_run_control(self, **kwargs) -> None:
            self.controls.append(kwargs["control_type"])

    original = {
        "from_dict": node_rpc_handlers.ExecutionArtifact.from_dict,
        "restore": node_rpc_handlers.restore_prepared_execution,
        "provider": node_rpc_handlers.get_provider,
        "start": node_rpc_handlers.start_prepared_run,
        "flush": node_rpc_handlers.session_manager.flush_pending_persists,
    }
    provider = Provider()
    node_rpc_handlers.ExecutionArtifact.from_dict = lambda _value: Artifact()
    node_rpc_handlers.restore_prepared_execution = lambda *_args, **_kwargs: Execution(True)
    node_rpc_handlers.get_provider = lambda _provider_id: provider
    node_rpc_handlers.session_manager.flush_pending_persists = lambda: None

    async def run_case(start_result, admitted: bool) -> tuple[Client, BaseException | None]:
        client = Client()
        node_rpc_handlers.restore_prepared_execution = (
            lambda *_args, **_kwargs: Execution(admitted)
        )
        if isinstance(start_result, BaseException):
            def start(*_args, **_kwargs):
                raise start_result
            node_rpc_handlers.start_prepared_run = start
        else:
            node_rpc_handlers.start_prepared_run = lambda *_args, **_kwargs: start_result
        error = None
        try:
            await node_rpc_handlers.handle_spawn_run(client, {
                "run_id": "handler-run",
                "root_id": "root",
                "app_session_id": "session",
                "cwd": "/tmp",
                "execution_artifact": {},
                "admission_lease": {"token": "lease", "seconds": 300},
            })
        except BaseException as exc:
            error = exc
        renewals = [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("admission-renew-handler")
        ]
        assert not renewals
        return client, error

    try:
        deferred, error = await run_case(False, False)
        assert error is None and deferred.controls == ["error"]
        failed, error = await run_case(RuntimeError("start failed"), True)
        assert error is None and failed.controls == ["error"]
        cancelled, error = await run_case(asyncio.CancelledError(), True)
        assert isinstance(error, asyncio.CancelledError)
        assert cancelled.controls == ["error"]
        success, error = await run_case(True, True)
        assert error is None and success.controls == ["admitted"]
        ctx = node_rpc_handlers._ctx_by_run.pop("handler-run", None)
        if ctx is not None and ctx.drain_task is not None:
            ctx.drain_task.cancel()
            await asyncio.gather(ctx.drain_task, return_exceptions=True)
    finally:
        node_rpc_handlers.ExecutionArtifact.from_dict = original["from_dict"]
        node_rpc_handlers.restore_prepared_execution = original["restore"]
        node_rpc_handlers.get_provider = original["provider"]
        node_rpc_handlers.start_prepared_run = original["start"]
        node_rpc_handlers.session_manager.flush_pending_persists = original["flush"]
        node_rpc_handlers._ctx_by_run.pop("handler-run", None)


async def executable_real_proxy_start() -> None:
    authority = {
        "id": "provider-authority",
        "kind": "claude",
        "generation": str(uuid.uuid4()),
        "revision": 1,
    }
    proxy = RemoteProviderProxy("node-proxy-proof")
    proxy.execution_authority_record = lambda _arguments: authority
    @contextmanager
    def authority_context(_execution, _arguments):
        yield authority
    proxy._execution_authority_context = authority_context
    conn = type("Connection", (), {"runs": {}})()
    original_connection = provider_remote.node_store.get_connection
    original_spawn = provider_remote.node_link.send_spawn_run
    original_cancel = provider_remote.node_link.send_cancel_run
    original_attest = execution_spawn_authority.attest_execution_spawn_authority
    original_root = __import__(
        "session_manager",
    ).manager._root_id_for
    provider_remote.node_store.get_connection = lambda _node_id: conn
    __import__("session_manager").manager._root_id_for = lambda _sid: "root"
    provider_remote._proxies[proxy.node_id] = proxy
    cancelled: list[str] = []
    cancel_completed: dict[str, asyncio.Event] = {}

    async def cancel(_node_id: str, run_id: str) -> bool:
        cancelled.append(run_id)
        cancel_completed.setdefault(run_id, asyncio.Event()).set()
        return True

    provider_remote.node_link.send_cancel_run = cancel
    execution_spawn_authority.attest_execution_spawn_authority = lambda _artifact: None

    def execution(run_id: str):
        return prepare_execution(
            authority,
            routing_session_id="session",
            run_id=run_id,
            prompt="proof",
            cwd="/tmp",
            mode="native",
            app_session_id="session",
            model=None,
            reasoning_effort=None,
            session_id=None,
        )

    async def run_scenario(kind: str):
        run_id = f"proxy-{kind}-{uuid.uuid4()}"
        sent = asyncio.Event()
        scheduled: list[object] = []
        lease_token = ""

        class Handle:
            def cancel(self) -> None:
                pass

        proxy._admission_scheduler = lambda _delay, callback: (
            scheduled.append(callback) or Handle()
        )

        async def spawn(_node_id: str, payload: dict) -> None:
            nonlocal lease_token
            lease_token = payload["admission_lease"]["token"]
            sent.set()
            if kind == "admitted":
                await provider_remote._on_run_control(
                    node_id=proxy.node_id,
                    run_id=run_id,
                    control_type="admitted",
                    data={"lease_token": lease_token},
                )
            elif kind == "error":
                await provider_remote._on_run_control(
                    node_id=proxy.node_id,
                    run_id=run_id,
                    control_type="error",
                    data={"error": "rejected", "lease_token": lease_token},
                )
            elif kind == "send-failure":
                raise RuntimeError("send failed")

        provider_remote.node_link.send_spawn_run = spawn
        prepared = execution(run_id)
        run_dir = runs_root() / run_id
        task = asyncio.create_task(asyncio.to_thread(
            proxy.start_run,
            execution=prepared,
            loop=asyncio.get_running_loop(),
            queue=asyncio.Queue(),
        ))
        sent_task = asyncio.create_task(sent.wait())
        done, _ = await asyncio.wait(
            {sent_task, task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done and not sent.is_set():
            sent_task.cancel()
            await task
            raise AssertionError("proxy start ended before spawn dispatch")
        sent_task.cancel()
        await asyncio.gather(sent_task, return_exceptions=True)
        state = proxy._runs.get(run_id)
        if kind == "cancel":
            cancel_completed.setdefault(run_id, asyncio.Event())
            assert proxy.cancel_run(run_id)
        elif kind == "expiry":
            assert state is not None
            scheduled.pop()()
        try:
            result = await task
        except RuntimeError:
            result = "raised"
        if kind == "admitted":
            assert result is True
            await provider_remote._on_run_control(
                node_id=proxy.node_id,
                run_id=run_id,
                control_type="complete",
                data={"success": True, "lease_token": lease_token},
            )
            shutil.rmtree(run_dir)
        elif kind == "cancel":
            assert result is False
            await cancel_completed[run_id].wait()
        else:
            assert result == "raised"
        if kind == "expiry":
            assert state is not None
            await state.admission_expiry_cancel_task
            assert state.admission_expiry_cancel_task.done()
        await provider_remote._on_run_control(
            node_id=proxy.node_id,
            run_id=run_id,
            control_type="admitted",
            data={"lease_token": lease_token},
        )
        assert run_id not in proxy._runs
        assert run_id not in conn.runs
        assert not run_dir.exists()

    try:
        for kind in ("admitted", "error", "send-failure", "cancel", "expiry"):
            await run_scenario(kind)
        assert any(run_id.startswith("proxy-expiry-") for run_id in cancelled)
    finally:
        provider_remote.node_store.get_connection = original_connection
        provider_remote.node_link.send_spawn_run = original_spawn
        provider_remote.node_link.send_cancel_run = original_cancel
        execution_spawn_authority.attest_execution_spawn_authority = original_attest
        __import__("session_manager").manager._root_id_for = original_root
        provider_remote._proxies.pop(proxy.node_id, None)


if __name__ == "__main__":
    main()
