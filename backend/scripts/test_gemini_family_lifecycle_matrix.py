#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="gemini-family-lifecycle-")
os.environ["BETTER_AGENT_HOME"] = HOME
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from provider import terminate_failed_run_process
from provider_agy import AgyProvider
from provider_amp import AmpProvider
from provider_copilot import CopilotProvider
from provider_cursor import CursorProvider
from provider_gemini import GeminiProvider, RunState
from provider_grok import GrokProvider
from provider_kimi import KimiProvider
from provider_lifecycle import RunLifecycleCoordinator
from provider_opencode import OpencodeProvider
from provider_pi import PiProvider
from provider_qwen import QwenProvider
from runs_dir import runs_root


FAMILY = (
    ("gemini", GeminiProvider), ("agy", AgyProvider), ("amp", AmpProvider),
    ("copilot", CopilotProvider), ("cursor", CursorProvider),
    ("kimi", KimiProvider), ("opencode", OpencodeProvider),
    ("pi", PiProvider), ("qwen", QwenProvider), ("grok", GrokProvider),
)


def kwargs(kind: str, run_id: str, queue: asyncio.Queue) -> dict:
    return {
        "run_id": run_id, "prompt": f"prompt-{kind}", "images": None,
        "files": None, "cwd": HOME, "loop": asyncio.get_running_loop(),
        "queue": queue, "model": None, "reasoning_effort": None,
        "session_id": None, "mode": "native", "app_session_id": "app",
    }


class FakePopen:
    pid = 424242
    returncode = None

    def poll(self):
        return self.returncode


class FakeContainment:
    def __init__(self):
        self.created: list[str] = []
        self.spawned: list[tuple[str, int]] = []

    def create(self, run_id):
        self.created.append(run_id)

    def spawn_kwargs(self, run_id):
        return {}

    def after_spawn(self, run_id, pid):
        self.spawned.append((run_id, pid))

    def teardown(self, run_id):
        return None


class FakeProcessControl:
    def detach_spawn_kwargs(self):
        return {}


def exercise_concrete_spawn(kind: str, provider_cls) -> None:
    module = sys.modules[provider_cls.__module__]
    provider = provider_cls({"id": f"{kind}-adapter", "kind": kind, "mode": "api_key"})
    run_id = f"{kind}-adapter"
    captured: dict = {}
    fake_containment = FakeContainment()

    def fake_runner_argv(run_dir, *, dev_script, kind):
        captured["runner"] = (Path(run_dir), Path(dev_script), kind)
        return ["controlled-runner", kind]

    def fake_spawn(argv, **spawn_kwargs):
        captured["argv"] = list(argv)
        captured["spawn_kwargs"] = dict(spawn_kwargs)
        return FakePopen()

    def fake_persist(write, rs):
        captured["persisted"] = rs

    originals = {}
    seams = {
        "runner_argv": fake_runner_argv,
        "containment": lambda: fake_containment,
        "_process_control": lambda: FakeProcessControl(),
        "persist_seed_or_terminate": fake_persist,
    }
    for name, value in seams.items():
        if hasattr(module, name):
            originals[name] = getattr(module, name)
            setattr(module, name, value)
    import containment as containment_module
    original_containment = containment_module.containment
    containment_module.containment = lambda: fake_containment
    original_popen = module.subprocess.Popen
    module.subprocess.Popen = fake_spawn
    runtime_popen = None
    if hasattr(module, "provider_runtime"):
        runtime_popen = module.provider_runtime.popen_runner
        module.provider_runtime.popen_runner = fake_spawn
    try:
        fields = kwargs(kind, run_id, asyncio.Queue())
        rs = provider_cls._spawn_run(provider, **fields)
    finally:
        containment_module.containment = original_containment
        module.subprocess.Popen = original_popen
        if runtime_popen is not None:
            module.provider_runtime.popen_runner = runtime_popen
        for name, value in originals.items():
            setattr(module, name, value)
    assert isinstance(rs, RunState) and rs.run_id == run_id and rs.popen.pid == 424242
    assert captured["persisted"] is rs
    assert captured["runner"][2] == kind
    assert captured["runner"][1].name == f"runner_{kind}.py"
    assert captured["argv"] == ["controlled-runner", kind]
    spawn = captured["spawn_kwargs"]
    assert spawn["cwd"] == HOME and spawn["env"]["BETTER_AGENT_PROVIDER_ID"] == provider.id
    assert fake_containment.created == [run_id]
    assert fake_containment.spawned == [(run_id, 424242)]
    import shutil
    shutil.rmtree(rs.run_dir, ignore_errors=True)


async def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition timed out")
        await asyncio.sleep(0.01)


def assert_catalog_retired(run_id: str) -> None:
    import active_run_catalog
    assert run_id not in (active_run_catalog.load(runs_root()) or {})


def install_controlled_spawn(
    provider, captured: list, children: list, *, seed_fail=False, reject_publish=False,
):
    def spawn(self, **fields):
        captured.append(dict(fields))
        run_dir = runs_root() / fields["run_id"]
        run_dir.mkdir(parents=True, exist_ok=True)
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        children.append(child)
        rs = RunState(
            fields["run_id"], run_dir, child, fields["mode"],
            fields["app_session_id"], fields["queue"],
            started_at=datetime.now().isoformat(),
        )
        if seed_fail:
            from provider import persist_seed_or_terminate
            persist_seed_or_terminate(
                lambda _: (_ for _ in ()).throw(OSError("seed failed")), rs
            )
        (run_dir / "backend_state.json").write_text("{}", encoding="utf-8")
        import active_run_catalog
        active_run_catalog.register(
            run_dir / "backend_state.json",
            {"run_id": fields["run_id"], "provider_id": self.id},
        )
        if reject_publish:
            future = asyncio.run_coroutine_threadsafe(
                self._lifecycle.cancel(fields["run_id"]), fields["loop"]
            )
            future.result(timeout=5)
        return rs
    provider._spawn_run = types.MethodType(spawn, provider)


async def clean_external(provider, rs: RunState) -> None:
    terminate_failed_run_process(rs)
    provider._cleanup_lifecycle_artifacts(rs)


async def exercise_cleanup_retry(kind: str, provider_cls) -> None:
    import active_run_catalog
    import provider_gemini as provider_gemini_module
    provider = provider_cls({"id": f"{kind}-retry", "kind": kind, "mode": "api_key"})
    provider._lifecycle = RunLifecycleCoordinator(asyncio.get_running_loop())
    admission = await provider._lifecycle.admit(f"{kind}-retry")
    run_dir = runs_root() / f"{kind}-retry"
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "backend_state.json"
    state_path.write_text("{}", encoding="utf-8")
    active_run_catalog.register(
        state_path, {"run_id": f"{kind}-retry", "provider_id": provider.id}
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE, start_new_session=True,
    )
    rs = RunState(
        f"{kind}-retry", run_dir, child, "native", "app", asyncio.Queue()
    )
    terminate_calls = 0
    catalog_calls = 0
    reap_calls = 0
    original_terminate = provider_gemini_module.terminate_failed_run_process
    original_retire = active_run_catalog.retire
    original_reap = provider_gemini_module._reap_run_dir
    def terminate_once(state):
        nonlocal terminate_calls
        terminate_calls += 1
        return original_terminate(state)
    def retire_retry(root, run_id, **options):
        nonlocal catalog_calls
        catalog_calls += 1
        if catalog_calls == 1:
            raise OSError("catalog transient")
        return original_retire(root, run_id, **options)
    def reap_retry(path):
        nonlocal reap_calls
        reap_calls += 1
        if reap_calls == 1:
            raise OSError("reap transient")
        return original_reap(path)
    provider_gemini_module.terminate_failed_run_process = terminate_once
    active_run_catalog.retire = retire_retry
    provider_gemini_module._reap_run_dir = reap_retry
    try:
        await provider._terminal_failure_cleanup(
            provider._lifecycle, admission.token, rs, None
        )
    finally:
        provider_gemini_module.terminate_failed_run_process = original_terminate
        active_run_catalog.retire = original_retire
        provider_gemini_module._reap_run_dir = original_reap
    assert terminate_calls == 1 and catalog_calls == 2 and reap_calls == 2
    assert child.poll() is not None and not run_dir.exists()
    catalog = active_run_catalog.load(runs_root()) or {}
    assert f"{kind}-retry" not in catalog
    assert not provider._runs and not provider._lifecycle_runs
    assert await provider._lifecycle.get(f"{kind}-retry") is None


async def exercise(kind: str, provider_cls) -> None:
    provider = provider_cls({"id": f"{kind}-test", "kind": kind, "mode": "api_key"})
    original_spawn = provider_cls._spawn_run
    inspect.signature(original_spawn).bind_partial(provider, **kwargs(kind, "signature", asyncio.Queue()))
    assert provider.build_env() is not os.environ
    assert "runner_argv" in inspect.getsource(original_spawn)
    captured: list[dict] = []
    children: list[subprocess.Popen] = []
    install_controlled_spawn(provider, captured, children)

    async def bootstrap(self, rs):
        return None
    provider._bootstrap_run = types.MethodType(bootstrap, provider)

    queue = asyncio.Queue()
    provider.start_run(**kwargs(kind, f"{kind}-live", queue))
    await wait_until(lambda: f"{kind}-live" in provider._runs)
    live = provider._runs[f"{kind}-live"]
    assert captured[-1]["prompt"] == f"prompt-{kind}"
    assert captured[-1]["cwd"] == HOME and captured[-1]["mode"] == "native"
    assert provider_cls.start_run is GeminiProvider.start_run
    provider.start_run(**kwargs(kind, f"{kind}-live", queue))
    await asyncio.sleep(0.05)
    assert len([item for item in captured if item["run_id"] == f"{kind}-live"]) == 1
    assert provider.cancel_run(f"{kind}-live")
    await wait_until(lambda: live.popen.poll() is not None)
    await wait_until(lambda: f"{kind}-live" not in provider._runs)
    assert not live.run_dir.exists()
    assert_catalog_retired(f"{kind}-live")

    provider.start_run(**kwargs(kind, f"{kind}-reload", queue))
    await wait_until(lambda: f"{kind}-reload" in provider._runs)
    reload_rs = provider._runs[f"{kind}-reload"]
    await provider.shutdown_lifecycle(terminate_runs=False)
    assert reload_rs.popen.poll() is None and reload_rs.run_dir.exists()
    await clean_external(provider, reload_rs)

    failed = provider_cls({"id": f"{kind}-seed", "kind": kind, "mode": "api_key"})
    failed_children: list[subprocess.Popen] = []
    install_controlled_spawn(failed, [], failed_children, seed_fail=True)
    failed._bootstrap_run = types.MethodType(bootstrap, failed)
    failed.start_run(**kwargs(kind, f"{kind}-seed", queue))
    await wait_until(lambda: bool(failed_children))
    await wait_until(lambda: failed_children[0].poll() is not None)
    await wait_until(lambda: not (runs_root() / f"{kind}-seed").exists())
    assert_catalog_retired(f"{kind}-seed")
    assert not failed._runs and not failed._lifecycle_runs

    broken = provider_cls({"id": f"{kind}-boot", "kind": kind, "mode": "api_key"})
    broken_children: list[subprocess.Popen] = []
    install_controlled_spawn(broken, [], broken_children)
    async def fail_bootstrap(self, rs):
        raise OSError("bootstrap failed")
    broken._bootstrap_run = types.MethodType(fail_bootstrap, broken)
    broken.start_run(**kwargs(kind, f"{kind}-boot", queue))
    await wait_until(lambda: bool(broken_children))
    await wait_until(lambda: broken_children[0].poll() is not None)
    await wait_until(lambda: not (runs_root() / f"{kind}-boot").exists())
    assert_catalog_retired(f"{kind}-boot")
    assert not broken._runs and not broken._lifecycle_runs

    rejected = provider_cls({"id": f"{kind}-publish", "kind": kind, "mode": "api_key"})
    rejected_children: list[subprocess.Popen] = []
    install_controlled_spawn(rejected, [], rejected_children, reject_publish=True)
    rejected._bootstrap_run = types.MethodType(bootstrap, rejected)
    rejected.start_run(**kwargs(kind, f"{kind}-publish", queue))
    await wait_until(lambda: bool(rejected_children))
    await wait_until(lambda: rejected_children[0].poll() is not None)
    await wait_until(lambda: not (runs_root() / f"{kind}-publish").exists())
    assert_catalog_retired(f"{kind}-publish")
    assert not rejected._runs and not rejected._lifecycle_runs

    recovered = provider_cls({"id": f"{kind}-recover", "kind": kind, "mode": "api_key"})
    recovered._bootstrap_run = types.MethodType(bootstrap, recovered)
    run_id = f"{kind}-recover"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE, start_new_session=True,
    )
    recovery_reaper = asyncio.create_task(asyncio.to_thread(child.wait))
    assert recovered.attach_recovered_run(
        desc={"run_id": run_id, "pid": child.pid, "app_session_id": "app"},
        queue=queue, loop=asyncio.get_running_loop(),
    )
    await wait_until(lambda: run_id in recovered._runs)
    assert recovered.cancel_run(run_id)
    await wait_until(lambda: child.poll() is not None)
    await recovery_reaper
    await wait_until(lambda: not run_dir.exists())
    assert_catalog_retired(run_id)

    recovery_seed = provider_cls({
        "id": f"{kind}-recovery-seed", "kind": kind, "mode": "api_key"
    })
    recovery_seed._bootstrap_run = types.MethodType(bootstrap, recovery_seed)
    def fail_recovery_seed(self, rs):
        raise OSError("recovery seed failed")
    recovery_seed._write_backend_state = types.MethodType(fail_recovery_seed, recovery_seed)
    run_id = f"{kind}-recovery-seed"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE, start_new_session=True,
    )
    seed_reaper = asyncio.create_task(asyncio.to_thread(child.wait))
    recovery_receipt = recovery_seed.attach_recovered_run(
        desc={"run_id": run_id, "pid": child.pid, "app_session_id": "app"},
        queue=queue, loop=asyncio.get_running_loop(),
    )
    assert recovery_receipt
    assert not await recovery_receipt.wait()
    assert child.poll() is None
    assert run_dir.exists()
    child.stdin.close()
    await seed_reaper
    shutil.rmtree(run_dir)
    assert not recovery_seed._runs and not recovery_seed._lifecycle_runs
    assert not recovery_seed._recovery_pending_states

    shutdown = provider_cls({"id": f"{kind}-shutdown", "kind": kind, "mode": "api_key"})
    shutdown._bootstrap_run = types.MethodType(bootstrap, shutdown)
    shutdown._lifecycle = RunLifecycleCoordinator(asyncio.get_running_loop())
    await shutdown._lifecycle.quiesce()
    run_id = f"{kind}-shutdown"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE, start_new_session=True,
    )
    shutdown_reaper = asyncio.create_task(asyncio.to_thread(child.wait))
    assert shutdown.attach_recovered_run(
        desc={"run_id": run_id, "pid": child.pid, "app_session_id": "app"},
        queue=queue, loop=asyncio.get_running_loop(),
    )
    await shutdown.shutdown_lifecycle(terminate_runs=True)
    await shutdown_reaper
    assert child.poll() is not None
    assert not run_dir.exists()
    assert_catalog_retired(run_id)
    assert not shutdown._recovery_pending_states


async def main_async() -> None:
    selected = sys.argv[1] if len(sys.argv) > 1 else None
    for kind, provider_cls in FAMILY:
        if selected is not None and kind != selected:
            continue
        exercise_concrete_spawn(kind, provider_cls)
        await exercise_cleanup_retry(kind, provider_cls)
        await exercise(kind, provider_cls)


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
        print("PASS gemini-family real lifecycle matrix")
    finally:
        import shutil
        shutil.rmtree(HOME, ignore_errors=True)
