"""Unit tests for the provisioned-session framework.

Covers the deterministic pieces that don't need a live claude subprocess:
  * `dirty_reason` — clean vs polluted base detection (size / turn-count /
    leak-marker / api-error).
  * `ProvisionedSessionSpec` defaults + subclass overrides; registry.
  * `resolve_config` — app-settings fallback + env overlay + choice
    validation + fork-capability gate.
  * `extract_fork_text` — sdk_output path and jsonl byte-window path.

Dispatch (`run`) and `ensure_session`/`ensure_caller` need a live backend +
claude and are exercised by the integration tests, not here.

Run with:
    cd backend && .venv/bin/python scripts/test_provisioning_framework.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import _test_home
_test_home.isolate("bc-test-provisioning-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import provisioning  # noqa: E402
import provisioning.config as prov_config  # noqa: E402
import provisioning.dispatch as prov_dispatch  # noqa: E402
import provisioning.inline_spec as inline_spec  # noqa: E402
import provisioning.manager as prov_manager  # noqa: E402
import working_mode  # noqa: E402
from provisioning import (  # noqa: E402
    DirtyPolicy,
    ProvisionedConfig,
    ProvisionedSessionSpec,
    dirty_reason,
    expired_reason,
    extract_fork_text,
    register,
    resolve_config,
)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


async def _ready_base_without_provider(spec, cfg, _ctx):
    return await asyncio.to_thread(prov_manager.ensure_session, spec, cfg)


# ── dirty_reason ──────────────────────────────────────────────────────

def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_dirty_reason() -> bool:
    policy = DirtyPolicy(
        max_base_bytes=1000,
        max_user_turns=1,
        max_assistant_turns=1,
        leak_markers=("LEAKED_QUERY_MARKER",),
    )
    cwd = "/tmp/proj"

    # No agent_sid yet → clean (not provisioned).
    if dirty_reason({}, policy, cwd):
        print(f"{FAIL} dirty: no agent_sid should be clean")
        return False

    if "no provider session id" not in dirty_reason(
        {"messages": [{"role": "user", "content": "stuck prep"}]},
        policy,
        cwd,
    ):
        print(f"{FAIL} dirty: failed initialization not detected")
        return False

    # compute_jsonl_path globs real disk; monkeypatch it to map our fake
    # agent_sids to temp jsonl files we control.
    import orchs.jsonl_helpers as jh
    tmp_dir = Path(os.environ["BETTER_CLAUDE_HOME"]) / "fakejsonl"
    paths: dict[str, Path] = {}

    def _fake_compute(_cwd: str, agent_sid: str):
        return paths.get(agent_sid)

    original = jh.compute_jsonl_path
    jh.compute_jsonl_path = _fake_compute  # type: ignore[assignment]
    try:
        def _seed(sid: str, rows: list[dict]) -> str:
            p = tmp_dir / f"{sid}.jsonl"
            _write_jsonl(p, rows)
            paths[sid] = p
            return sid

        # Clean: one provision user-turn + one ready assistant-turn, small.
        clean = _seed("clean-sid", [
            {"type": "user", "message": {"content": "ready prompt"}},
            {"type": "assistant", "message": {"content": "ready"}},
        ])
        if dirty_reason({"agent_session_id": clean}, policy, cwd):
            print(f"{FAIL} dirty: clean base flagged dirty")
            return False

        # Dirty: too big.
        big = _seed("big-sid", [{"type": "user", "message": {"content": "x" * 2000}}])
        if not dirty_reason({"agent_session_id": big}, policy, cwd):
            print(f"{FAIL} dirty: oversized base not flagged")
            return False

        # Dirty: a second user turn (a query leaked into the base).
        two = _seed("two-sid", [
            {"type": "user", "message": {"content": "provision"}},
            {"type": "assistant", "message": {"content": "ready"}},
            {"type": "user", "message": {"content": "second turn leaked"}},
        ])
        if not dirty_reason({"agent_session_id": two}, policy, cwd):
            print(f"{FAIL} dirty: 2 user-turn base not flagged")
            return False

        # Dirty: leak marker in a user turn.
        leak = _seed("leak-sid", [
            {"type": "user", "message": {"content": "LEAKED_QUERY_MARKER stuff"}},
        ])
        if not dirty_reason({"agent_session_id": leak}, policy, cwd):
            print(f"{FAIL} dirty: leak marker not flagged")
            return False

        # Dirty: API-error assistant turn.
        err = _seed("err-sid", [
            {"type": "user", "message": {"content": "provision"}},
            {"type": "assistant", "message": {"content": "x"}, "isApiErrorMessage": True},
        ])
        if not dirty_reason({"agent_session_id": err}, policy, cwd):
            print(f"{FAIL} dirty: api-error turn not flagged")
            return False
    finally:
        jh.compute_jsonl_path = original  # type: ignore[assignment]

    print(f"{PASS} dirty_reason: clean / failed-init / size / turn-count / leak / api-error")
    return True


# ── expired_reason (lifetime recycling) ───────────────────────────────

def test_expired_reason() -> bool:
    class _Fresh(ProvisionedSessionSpec):
        lifetime_seconds = 60.0

    class _NoLifetime(ProvisionedSessionSpec):
        lifetime_seconds = None

    fresh = _Fresh()
    now = time.time()

    # No lifetime configured ⇒ never expired.
    if expired_reason({"working_mode_meta": {"provisioned_at": 0}}, _NoLifetime()):
        print(f"{FAIL} expired: no-lifetime spec flagged expired")
        return False

    # Fresh stamp ⇒ not expired.
    if expired_reason({"working_mode_meta": {"provisioned_at": now}}, fresh):
        print(f"{FAIL} expired: fresh base flagged expired")
        return False

    # Old stamp ⇒ expired.
    old = {"working_mode_meta": {"provisioned_at": now - 120.0}}
    if not expired_reason(old, fresh):
        print(f"{FAIL} expired: aged base not flagged")
        return False

    # Missing stamp (predates lifetime tracking) ⇒ expired so it gets re-stamped.
    if not expired_reason({"working_mode_meta": {}}, fresh):
        print(f"{FAIL} expired: unstamped base not flagged")
        return False

    print(f"{PASS} expired_reason: fresh / aged / unstamped / no-lifetime")
    return True


# ── spec + registry ───────────────────────────────────────────────────

def test_spec_and_registry() -> bool:
    class _S(ProvisionedSessionSpec):
        key = "unit_test_spec"
        version = 7
        name = "unit-test"
        env_prefix = "UNIT_TEST"
        task_key = "session_search_worker"
        machine_completion = False
        bare_config = False

        def build_provision_prompt(self, ctx):
            return "prep"

    s = register(_S())
    if s.machine_completion is not False or s.bare_config is not False:
        print(f"{FAIL} spec: subclass override ignored")
        return False
    if provisioning.get("unit_test_spec") is not s:
        print(f"{FAIL} registry: get did not return registered instance")
        return False
    # Defaults from the base class survive.
    if _S().run_mode != "fork" or _S().dispatch != "http" or _S().ephemeral_forks is not True:
        print(f"{FAIL} spec: base defaults wrong")
        return False
    # build_instructions default = just the query.
    if _S().build_instructions("hello", {}) != "hello":
        print(f"{FAIL} spec: default build_instructions not identity")
        return False
    print(f"{PASS} ProvisionedSessionSpec overrides + registry")
    return True


# ── resolve_config ────────────────────────────────────────────────────

def test_resolve_config_overlay() -> bool:
    class _S(ProvisionedSessionSpec):
        key = "cfg_test_spec"
        env_prefix = "CFG_TEST"
        task_key = "session_search_worker"  # resolves via app-settings
        dispatch = "in_process"
        default_model = "fallback-model"

    # Env overlay overrides model + dispatch.
    os.environ["CFG_TEST_MODEL"] = "overridden-model"
    os.environ["CFG_TEST_DISPATCH"] = "http"
    try:
        cfg = resolve_config(_S())
    finally:
        del os.environ["CFG_TEST_MODEL"]
        del os.environ["CFG_TEST_DISPATCH"]
    if cfg.model != "overridden-model" or cfg.dispatch != "http":
        print(f"{FAIL} resolve_config: env overlay not applied (model={cfg.model}, dispatch={cfg.dispatch})")
        return False

    # Invalid choice raises.
    os.environ["CFG_TEST_DISPATCH"] = "bogus"
    try:
        resolve_config(_S())
        print(f"{FAIL} resolve_config: bogus dispatch did not raise")
        return False
    except RuntimeError:
        pass
    finally:
        del os.environ["CFG_TEST_DISPATCH"]

    class _S2(ProvisionedSessionSpec):
        key = "cfg_test_spec2"
        env_prefix = "CFG_TEST2"
        task_key = ""  # no app-settings resolution
    try:
        resolve_config(_S2())
        print(f"{FAIL} resolve_config: missing model did not raise")
        return False
    except RuntimeError:
        pass
    print(f"{PASS} resolve_config: env overlay + choice validation + missing model rejection")
    return True


def test_fork_capability_checks_never_resolve_credentials() -> bool:
    import config_store
    import provider

    original_resolve = config_store.resolve_provider_ref
    original_list = config_store.list_providers
    original_get_provider = provider.get_provider
    config_store.resolve_provider_ref = lambda provider_id: {
        "id": provider_id,
        "supports_fork": provider_id == "forkable",
    }
    config_store.list_providers = lambda: {"default_provider_id": "forkable"}
    provider.get_provider = lambda _provider_id: (_ for _ in ()).throw(
        AssertionError("fork capability checks must not resolve credentials")
    )
    try:
        values = (
            prov_config.provider_supports_fork("forkable"),
            inline_spec.provider_supports_fork("forkable"),
            prov_config.provider_supports_fork("direct-only"),
            inline_spec.provider_supports_fork("direct-only"),
            prov_config.provider_supports_fork(""),
        )
    finally:
        config_store.resolve_provider_ref = original_resolve
        config_store.list_providers = original_list
        provider.get_provider = original_get_provider
    ok = values == (True, True, False, False, True)
    print(f"{PASS if ok else FAIL} provisioning fork checks avoid credential reads")
    return ok


def test_resolve_config_uses_current_disk_token() -> bool:
    class _S(ProvisionedSessionSpec):
        key = "cfg_token_spec"
        env_prefix = "CFG_TOKEN"
        task_key = ""
        dispatch = "http"
        default_model = "model"

    token_path = Path(os.environ["BETTER_CLAUDE_HOME"]) / "internal_token"
    token_path.write_text("disk-token", encoding="utf-8")
    original_env = {
        "BETTER_AGENT_INTERNAL_TOKEN": os.environ.get("BETTER_AGENT_INTERNAL_TOKEN"),
        "BETTER_CLAUDE_INTERNAL_TOKEN": os.environ.get("BETTER_CLAUDE_INTERNAL_TOKEN"),
        "CFG_TOKEN_INTERNAL_TOKEN": os.environ.get("CFG_TOKEN_INTERNAL_TOKEN"),
    }
    try:
        os.environ["BETTER_AGENT_INTERNAL_TOKEN"] = "stale-agent-env-token"
        os.environ["BETTER_CLAUDE_INTERNAL_TOKEN"] = "stale-env-token"
        os.environ.pop("CFG_TOKEN_INTERNAL_TOKEN", None)
        cfg = resolve_config(_S())
        if cfg.internal_token != "disk-token":
            print(f"{FAIL} resolve_config token: disk token did not beat stale env")
            return False

        os.environ["CFG_TOKEN_INTERNAL_TOKEN"] = "explicit-token"
        cfg = resolve_config(_S())
        if cfg.internal_token != "explicit-token":
            print(f"{FAIL} resolve_config token: explicit spec token did not win")
            return False

        token_path.unlink()
        os.environ.pop("CFG_TOKEN_INTERNAL_TOKEN", None)
        cfg = resolve_config(_S())
        if cfg.internal_token != "stale-agent-env-token":
            print(f"{FAIL} resolve_config token: env fallback did not survive missing disk")
            return False
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        token_path.unlink(missing_ok=True)
    print(f"{PASS} resolve_config token: explicit > disk > env")
    return True


def test_dispatch_sends_resolved_disk_token() -> bool:
    class _S(ProvisionedSessionSpec):
        key = "cfg_dispatch_token_spec"
        env_prefix = "CFG_DISPATCH_TOKEN"
        task_key = ""
        dispatch = "http"
        default_model = "model"

    token_path = Path(os.environ["BETTER_CLAUDE_HOME"]) / "internal_token"
    token_path.write_text("disk-dispatch-token", encoding="utf-8")
    original_env = {
        "BETTER_AGENT_INTERNAL_TOKEN": os.environ.get("BETTER_AGENT_INTERNAL_TOKEN"),
        "BETTER_CLAUDE_INTERNAL_TOKEN": os.environ.get("BETTER_CLAUDE_INTERNAL_TOKEN"),
    }
    captured: list[str] = []
    original_post = prov_dispatch._post_ask_fork

    async def fake_post(cfg, payload, *, timeout):
        captured.append(cfg.internal_token)
        return {"success": True, "sdk_output": "ok"}

    async def run() -> None:
        cfg = resolve_config(_S())
        await prov_dispatch.dispatch(
            _S(), cfg,
            base_session_id="base",
            caller_session_id="caller",
            instructions="work",
            provision_prompt="provision",
        )

    try:
        os.environ["BETTER_CLAUDE_INTERNAL_TOKEN"] = "stale-dispatch-env-token"
        os.environ["BETTER_AGENT_INTERNAL_TOKEN"] = "stale-agent-dispatch-env-token"
        prov_dispatch._post_ask_fork = fake_post  # type: ignore[assignment]
        asyncio.run(run())
    finally:
        prov_dispatch._post_ask_fork = original_post  # type: ignore[assignment]
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        token_path.unlink(missing_ok=True)
    ok = captured == ["disk-dispatch-token"]
    print(f"{PASS if ok else FAIL} dispatch uses resolved disk token (captured={captured!r})")
    return ok


# ── extract_fork_text ─────────────────────────────────────────────────

def test_extract_fork_text() -> bool:
    # sdk_output short-circuits.
    if extract_fork_text({"sdk_output": "  hello  "}) != "hello":
        print(f"{FAIL} extract: sdk_output path")
        return False

    # jsonl byte window: write two assistant rows, sample the second.
    tmp = Path(os.environ["BETTER_CLAUDE_HOME"]) / "fork.jsonl"
    row1 = json.dumps({"type": "assistant", "message": {"content": "first"}}) + "\n"
    row2 = json.dumps({"type": "assistant", "message": {"content": "second"}}) + "\n"
    tmp.write_text(row1 + row2, encoding="utf-8")
    # new_byte_offset is 1-based start; point past row1 into row2.
    start = len(row1.encode("utf-8"))
    text = extract_fork_text({
        "jsonl_path": str(tmp),
        "new_byte_offset": start + 1,
        "total_bytes_now": len((row1 + row2).encode("utf-8")),
    })
    if text != "second":
        print(f"{FAIL} extract: jsonl byte window got {text!r}")
        return False
    print(f"{PASS} extract_fork_text: sdk_output + jsonl byte window")
    return True


def test_run_serializes_lifecycle_creation() -> bool:
    class _S(ProvisionedSessionSpec):
        key = "lifecycle_lock_test"
        env_prefix = "LIFECYCLE_LOCK_TEST"
        name = "worker:lifecycle-lock"

        def build_config(self, *, model=None):
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
                worker_description="worker:lifecycle-lock",
            )

        def build_instructions(self, query, ctx):
            return "instructions"

        def build_provision_prompt(self, ctx):
            return "provision"

    original_ensure_session = prov_manager.ensure_session
    original_ensure_caller = prov_manager.ensure_caller
    original_dispatch = prov_manager.dispatch
    original_ready_base = prov_manager._ensure_ready_base_locked
    active = 0
    max_active = 0
    guard = threading.Lock()

    def fake_ensure_session(spec, cfg):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return "base"

    async def fake_dispatch(*args, **kwargs):
        return {"success": True, "sdk_output": "ok"}

    try:
        prov_manager.ensure_session = fake_ensure_session
        prov_manager.ensure_caller = lambda spec, cfg: "caller"
        prov_manager.dispatch = fake_dispatch
        prov_manager._ensure_ready_base_locked = _ready_base_without_provider
        errors: list[BaseException] = []

        def run_once():
            try:
                prov_manager.run_sync(_S(), "", {})
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        prov_manager.ensure_session = original_ensure_session
        prov_manager.ensure_caller = original_ensure_caller
        prov_manager.dispatch = original_dispatch
        prov_manager._ensure_ready_base_locked = original_ready_base

    if errors:
        print(f"{FAIL} lifecycle lock: concurrent run failed with {errors[0]}")
        return False
    if max_active != 1:
        print(f"{FAIL} lifecycle lock: ensure_session ran concurrently (max_active={max_active})")
        return False
    print(f"{PASS} lifecycle lock: base/caller creation serialized")
    return True


def test_run_lifecycle_runs_off_event_loop() -> bool:
    class _S(ProvisionedSessionSpec):
        key = "lifecycle_off_loop_test"
        env_prefix = "LIFECYCLE_OFF_LOOP_TEST"
        name = "worker:lifecycle-off-loop"
        provision_timeout = 1.0
        retry_attempts = 1

        def build_config(self, *, model=None):
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
                worker_description="worker:lifecycle-off-loop",
            )

        def build_instructions(self, query, ctx):
            return "instructions"

        def build_provision_prompt(self, ctx):
            return "provision"

    original_ensure_session = prov_manager.ensure_session
    original_ensure_caller = prov_manager.ensure_caller
    original_dispatch = prov_manager.dispatch
    original_ready_base = prov_manager._ensure_ready_base_locked
    lifecycle_threads: list[tuple[str, int]] = []
    dispatch_thread: list[int] = []

    def fake_ensure_session(spec, cfg):
        lifecycle_threads.append(("base", threading.get_ident()))
        return "base"

    def fake_ensure_caller(spec, cfg):
        lifecycle_threads.append(("caller", threading.get_ident()))
        return "caller"

    async def fake_dispatch(*args, **kwargs):
        dispatch_thread.append(threading.get_ident())
        return {"success": True, "sdk_output": "ok"}

    try:
        prov_manager.ensure_session = fake_ensure_session
        prov_manager.ensure_caller = fake_ensure_caller
        prov_manager.dispatch = fake_dispatch
        prov_manager._ensure_ready_base_locked = _ready_base_without_provider
        result = asyncio.run(prov_manager.run(_S(), "", {}))
    finally:
        prov_manager.ensure_session = original_ensure_session
        prov_manager.ensure_caller = original_ensure_caller
        prov_manager.dispatch = original_dispatch
        prov_manager._ensure_ready_base_locked = original_ready_base

    if result.base_session_id != "base" or result.caller_session_id != "caller":
        print(f"{FAIL} lifecycle off-loop: wrong lifecycle ids")
        return False
    if len(lifecycle_threads) != 2 or not dispatch_thread:
        print(f"{FAIL} lifecycle off-loop: missing lifecycle/dispatch calls")
        return False
    if any(tid == dispatch_thread[0] for _name, tid in lifecycle_threads):
        print(f"{FAIL} lifecycle off-loop: lifecycle ran on event-loop thread")
        return False
    if lifecycle_threads[0][0] != "base" or lifecycle_threads[1][0] != "caller":
        print(f"{FAIL} lifecycle off-loop: wrong call order {lifecycle_threads}")
        return False
    if lifecycle_threads[0][1] != lifecycle_threads[1][1]:
        print(f"{FAIL} lifecycle off-loop: base/caller split across worker threads")
        return False
    print(f"{PASS} lifecycle off-loop: base/caller creation runs off event loop")
    return True


def test_lifecycle_lock_timeout_surfaces() -> bool:
    class _S(ProvisionedSessionSpec):
        key = "lifecycle_timeout_test"
        env_prefix = "LIFECYCLE_TIMEOUT_TEST"
        name = "worker:lifecycle-timeout"
        provision_timeout = 0.05
        retry_attempts = 1

        def build_config(self, *, model=None):
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
                worker_description="worker:lifecycle-timeout",
            )

        def build_provision_prompt(self, ctx):
            return "provision"

    spec = _S()
    cfg = spec.build_config()
    lock = prov_manager._lifecycle_lock(spec, cfg)
    lock.acquire()
    try:
        started = time.monotonic()
        try:
            prov_manager.run_sync(spec, "", {})
        except TimeoutError as exc:
            elapsed = time.monotonic() - started
            if "lifecycle lock timed out" not in str(exc):
                print(f"{FAIL} lifecycle timeout: wrong error {exc}")
                return False
            if elapsed > 1.0:
                print(f"{FAIL} lifecycle timeout: took too long ({elapsed:.3f}s)")
                return False
            print(f"{PASS} lifecycle lock timeout surfaces")
            return True
        print(f"{FAIL} lifecycle timeout: run_sync did not raise")
        return False
    finally:
        lock.release()


def test_ensure_warm_base_initializes_once() -> bool:
    class _S(ProvisionedSessionSpec):
        key = "warm_base_test"
        env_prefix = "WARM_BASE_TEST"
        name = "worker:warm-base"
        orchestration_mode = "native"
        tool_profile = "warm_base_profile"

        def build_provision_prompt(self, ctx):
            return "provision"

    spec = _S()
    cfg = ProvisionedConfig(
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
        worker_description="worker:warm-base",
    )

    original_ensure_session = prov_manager.ensure_session
    original_session_manager = sys.modules.get("session_manager")
    original_main = sys.modules.get("main")
    calls = 0
    sessions = {"base": {"id": "base", "agent_session_id": None}}

    class FakeSessionManager:
        def get(self, sid):
            return sessions.get(sid)

        def set_agent_sid(self, sid, mode, agent_sid, **_kwargs):
            sessions[sid]["agent_session_id"] = agent_sid

    class FakeCoordinator:
        def __init__(self):
            self.init_cancel_events = {}

        async def _init_target_agent_session(self, **kwargs):
            nonlocal calls
            calls += 1
            if kwargs.get("provision_prompt") != "provision":
                raise AssertionError("wrong provision prompt")
            if kwargs.get("provisioned_tool_profile") != "warm_base_profile":
                raise AssertionError("tool profile did not reach base initializer")
            return "agent-sid"

    fake_sm_mod = type(sys)("session_manager")
    fake_sm_mod.manager = FakeSessionManager()
    fake_main_mod = type(sys)("main")
    fake_main_mod.coordinator = FakeCoordinator()

    try:
        prov_manager.ensure_session = lambda _spec, _cfg: "base"
        sys.modules["session_manager"] = fake_sm_mod
        sys.modules["main"] = fake_main_mod
        first = asyncio.run(prov_manager.ensure_warm_base(spec, cfg, {}))
        second = asyncio.run(prov_manager.ensure_warm_base(spec, cfg, {}))
    finally:
        prov_manager.ensure_session = original_ensure_session
        if original_session_manager is not None:
            sys.modules["session_manager"] = original_session_manager
        else:
            sys.modules.pop("session_manager", None)
        if original_main is not None:
            sys.modules["main"] = original_main
        else:
            sys.modules.pop("main", None)

    if first != "base" or second != "base":
        print(f"{FAIL} warm_base: wrong base ids {first!r}/{second!r}")
        return False
    if calls != 1:
        print(f"{FAIL} warm_base: expected one init call, got {calls}")
        return False
    if sessions["base"].get("agent_session_id") != "agent-sid":
        print(f"{FAIL} warm_base: sid not persisted")
        return False
    print(f"{PASS} ensure_warm_base initializes only unwarmed bases")
    return True


def test_run_sync_times_out_stuck_dispatch() -> bool:
    class _S(ProvisionedSessionSpec):
        key = "dispatch_timeout_test"
        env_prefix = "DISPATCH_TIMEOUT_TEST"
        name = "worker:dispatch-timeout"
        provision_timeout = 0.05
        retry_attempts = 1

        def build_config(self, *, model=None):
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
                worker_description="worker:dispatch-timeout",
            )

        def build_instructions(self, query, ctx):
            return "instructions"

        def build_provision_prompt(self, ctx):
            return "provision"

    original_ensure_session = prov_manager.ensure_session
    original_ensure_caller = prov_manager.ensure_caller
    original_dispatch = prov_manager.dispatch
    original_ready_base = prov_manager._ensure_ready_base_locked

    async def stuck_dispatch(*args, **kwargs):
        await asyncio.sleep(1.0)
        return {"success": True, "sdk_output": "late"}

    try:
        prov_manager.ensure_session = lambda spec, cfg: "base"
        prov_manager.ensure_caller = lambda spec, cfg: "caller"
        prov_manager.dispatch = stuck_dispatch
        prov_manager._ensure_ready_base_locked = _ready_base_without_provider
        started = time.monotonic()
        try:
            prov_manager.run_sync(_S(), "", {})
        except TimeoutError as exc:
            elapsed = time.monotonic() - started
            if "provisioned run timed out" not in str(exc):
                print(f"{FAIL} dispatch timeout: wrong error {exc}")
                return False
            if elapsed > 1.0:
                print(f"{FAIL} dispatch timeout: took too long ({elapsed:.3f}s)")
                return False
            print(f"{PASS} dispatch timeout surfaces")
            return True
        print(f"{FAIL} dispatch timeout: run_sync did not raise")
        return False
    finally:
        prov_manager.ensure_session = original_ensure_session
        prov_manager.ensure_caller = original_ensure_caller
        prov_manager.dispatch = original_dispatch
        prov_manager._ensure_ready_base_locked = original_ready_base


def _budget_spec(provision_timeout: float, dispatch_timeout: float | None, retry_attempts: int = 1):
    class _S(ProvisionedSessionSpec):
        key = "budget_test"
        env_prefix = "BUDGET_TEST"
        name = "worker:budget-test"

        def build_provision_prompt(self, ctx):
            return "provision"

        def build_config(self, *, model=None):
            return ProvisionedConfig(
                cwd="/repo", model="model", provider_id="provider", reasoning_effort="",
                run_mode="fork", dispatch="http", on_no_fork="error", node_id="primary",
                backend_url="http://localhost:8000", internal_token="token",
                provisioned_session_id=None, caller_session_id=None,
                worker_description="worker:budget-test",
            )

    spec = _S()
    object.__setattr__(spec, "provision_timeout", provision_timeout)
    object.__setattr__(spec, "dispatch_timeout", dispatch_timeout)
    object.__setattr__(spec, "retry_attempts", retry_attempts)
    return spec


def test_sync_timeout_composes_lifecycle_and_dispatch_budgets() -> bool:
    total = prov_manager._sync_timeout_seconds(_budget_spec(55.0, 45.0))
    if total != 100.5:
        print(f"{FAIL} budget composition: expected 100.5, got {total}")
        return False
    default_total = prov_manager._sync_timeout_seconds(_budget_spec(10.0, None, retry_attempts=2))
    # lifecycle 10 + dispatch 10×2 + backoff 2.0 + 0.5
    if default_total != 32.5:
        print(f"{FAIL} budget composition default: expected 32.5, got {default_total}")
        return False
    print(f"{PASS} run_sync budget composes lifecycle + dispatch phases")
    return True


def test_dispatch_uses_dispatch_timeout_per_attempt() -> bool:
    import provisioning.dispatch as prov_dispatch

    spec = _budget_spec(55.0, 7.0)
    cfg = ProvisionedConfig(
        cwd="/repo", model="model", provider_id="provider", reasoning_effort="",
        run_mode="fork", dispatch="http", on_no_fork="error", node_id="primary",
        backend_url="http://localhost:8000", internal_token="token",
        provisioned_session_id=None, caller_session_id=None,
        worker_description="worker:budget-test",
    )
    seen: list[tuple[float, str]] = []

    async def fake_post(cfg_, payload, *, timeout):
        seen.append((timeout, payload.get("client_delegation_id")))
        return {"success": True, "sdk_output": "ok"}

    original = prov_dispatch._post_ask_fork
    prov_dispatch._post_ask_fork = fake_post
    try:
        asyncio.run(prov_dispatch.dispatch(
            spec, cfg,
            base_session_id="base", caller_session_id="caller",
            instructions="i", provision_prompt="p",
            client_delegation_id="explicit-delegation",
        ))
    finally:
        prov_dispatch._post_ask_fork = original
    if seen != [(7.0, "explicit-delegation")]:
        print(f"{FAIL} dispatch timeout/id kwargs: got {seen}")
        return False
    print(f"{PASS} dispatch attempts use dispatch_timeout, not provision_timeout")
    return True


def test_in_process_dispatch_uses_explicit_delegation_id() -> bool:
    import provisioning.dispatch as prov_dispatch

    spec = _budget_spec(55.0, 7.0)
    cfg = ProvisionedConfig(
        cwd="/repo", model="model", provider_id="provider", reasoning_effort="",
        run_mode="fork", dispatch="in_process", on_no_fork="error", node_id="primary",
        backend_url="http://localhost:8000", internal_token="token",
        provisioned_session_id=None, caller_session_id=None,
        worker_description="worker:budget-test",
    )
    captured = {}

    class Coordinator:
        async def run_delegation(self, **kwargs):
            captured.update(kwargs)
            return {"success": True, "sdk_output": "ok"}

    fake_main = type(sys)("main")
    fake_main.coordinator = Coordinator()
    original_main = sys.modules.get("main")
    sys.modules["main"] = fake_main
    try:
        asyncio.run(prov_dispatch.dispatch(
            spec, cfg,
            base_session_id="base", caller_session_id="caller",
            instructions="i", provision_prompt="p",
            client_delegation_id="explicit-in-process",
        ))
    finally:
        if original_main is not None:
            sys.modules["main"] = original_main
        else:
            sys.modules.pop("main", None)
    if captured.get("client_delegation_id") != "explicit-in-process":
        print(f"{FAIL} in-process dispatch id: {captured!r}")
        return False
    print(f"{PASS} in-process dispatch uses explicit delegation id")
    return True


def test_run_honors_client_delegation_id_from_ctx() -> bool:
    spec = _budget_spec(55.0, 7.0)
    captured = {}
    original_ensure_session = prov_manager.ensure_session
    original_ensure_caller = prov_manager.ensure_caller
    original_dispatch = prov_manager.dispatch
    original_ready_base = prov_manager._ensure_ready_base_locked

    async def fake_dispatch(*args, **kwargs):
        captured.update(kwargs)
        return {"success": True, "sdk_output": "ok"}

    try:
        prov_manager.ensure_session = lambda spec_, cfg_: "base"
        prov_manager.ensure_caller = lambda spec_, cfg_: "caller"
        prov_manager.dispatch = fake_dispatch
        prov_manager._ensure_ready_base_locked = _ready_base_without_provider
        asyncio.run(prov_manager.run(
            spec,
            "query",
            {
                "_debug_request_id": "request-1",
                "client_delegation_id": "job-owned-id",
            },
        ))
    finally:
        prov_manager.ensure_session = original_ensure_session
        prov_manager.ensure_caller = original_ensure_caller
        prov_manager.dispatch = original_dispatch
        prov_manager._ensure_ready_base_locked = original_ready_base
    if captured.get("client_delegation_id") != "job-owned-id":
        print(f"{FAIL} run client_delegation_id from ctx: {captured!r}")
        return False
    print(f"{PASS} run honors client_delegation_id from ctx")
    return True


def test_run_logs_phase_timings_for_debug_requests() -> bool:
    spec = _budget_spec(55.0, 7.0)
    original_ensure_session = prov_manager.ensure_session
    original_ensure_caller = prov_manager.ensure_caller
    original_dispatch = prov_manager.dispatch
    original_ready_base = prov_manager._ensure_ready_base_locked
    original_info = prov_manager.logger.info
    captured: list[tuple[str, tuple]] = []

    async def fake_dispatch(*args, **kwargs):
        return {
            "success": True,
            "sdk_output": '{"ok": true}',
            "fork_agent_sid": "fork-sid",
            "timings_ms": {
                "runner_enqueue_to_first_event": 1.0,
                "runner_enqueue_to_first_tool": 2.0,
                "runner_enqueue_to_final_answer": 3.0,
                "runner_enqueue_to_terminal_event": 4.0,
            },
        }

    def fake_info(message, *args, **kwargs):
        captured.append((str(message), args))

    try:
        prov_manager.ensure_session = lambda spec_, cfg_: "base"
        prov_manager.ensure_caller = lambda spec_, cfg_: "caller"
        prov_manager.dispatch = fake_dispatch
        prov_manager._ensure_ready_base_locked = _ready_base_without_provider
        prov_manager.logger.info = fake_info
        asyncio.run(prov_manager.run(
            spec,
            "query",
            {"_debug_request_id": "timing-request"},
        ))
    finally:
        prov_manager.ensure_session = original_ensure_session
        prov_manager.ensure_caller = original_ensure_caller
        prov_manager.dispatch = original_dispatch
        prov_manager._ensure_ready_base_locked = original_ready_base
        prov_manager.logger.info = original_info

    timing_rows = [args for message, args in captured if message.startswith("provisioned_run_timing")]
    if not timing_rows:
        print(f"{FAIL} phase timings: no provisioned_run_timing log")
        return False
    timing_text = str(timing_rows[-1][-1])
    expected = (
        "resolve_config_ms=",
        "ensure_lifecycle_ms=",
        "build_prompts_ms=",
        "dispatch_ms=",
        "extract_fork_text_ms=",
        "parse_result_ms=",
        "dispatch_runner_enqueue_to_first_event_ms=",
        "dispatch_runner_enqueue_to_first_tool_ms=",
        "dispatch_runner_enqueue_to_final_answer_ms=",
        "dispatch_runner_enqueue_to_terminal_event_ms=",
        "total_ms=",
    )
    if not all(part in timing_text for part in expected):
        print(f"{FAIL} phase timings: missing fields in {timing_text!r}")
        return False
    print(f"{PASS} run logs phase timings for debug requests")
    return True


def test_delegation_tool_activity_detector_reads_canonical_message_content() -> bool:
    from orchs.manager._delegation import _delegation_event_is_tool_activity

    event = {
        "type": "agent_message",
        "data": {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "checking"},
                    {"type": "tool_use", "name": "search_requirement_units_rg"},
                ],
            },
        },
    }
    if not _delegation_event_is_tool_activity(event):
        print(f"{FAIL} delegation timing: canonical tool_use block not detected")
        return False
    print(f"{PASS} delegation timing detects canonical tool activity")
    return True


def test_run_sync_survives_lifecycle_plus_full_dispatch() -> bool:
    """Lifecycle and dispatch each within their own budget, but their SUM
    above the old provision_timeout+0.5 total — must succeed post-fix."""
    spec = _budget_spec(1.0, 1.0)

    def slow_ensure_session(spec_, cfg_):
        time.sleep(0.9)
        return "base"

    async def slow_dispatch(*args, **kwargs):
        await asyncio.sleep(0.9)
        return {"success": True, "sdk_output": "late-but-legal"}

    original_ensure_session = prov_manager.ensure_session
    original_ensure_caller = prov_manager.ensure_caller
    original_dispatch = prov_manager.dispatch
    original_ready_base = prov_manager._ensure_ready_base_locked
    try:
        prov_manager.ensure_session = slow_ensure_session
        prov_manager.ensure_caller = lambda spec_, cfg_: "caller"
        prov_manager.dispatch = slow_dispatch
        prov_manager._ensure_ready_base_locked = _ready_base_without_provider
        result = prov_manager.run_sync(spec, "", {})
    except TimeoutError as exc:
        print(f"{FAIL} phase budgets: run_sync raised {exc}")
        return False
    finally:
        prov_manager.ensure_session = original_ensure_session
        prov_manager.ensure_caller = original_ensure_caller
        prov_manager.dispatch = original_dispatch
        prov_manager._ensure_ready_base_locked = original_ready_base
    if result.text != "late-but-legal":
        print(f"{FAIL} phase budgets: wrong result {result.text!r}")
        return False
    print(f"{PASS} run_sync tolerates lifecycle + dispatch each using their own budget")
    return True


def test_lifecycle_lock_budget_stays_on_provision_timeout() -> bool:
    spec = _budget_spec(0.1, 30.0)
    cfg = ProvisionedConfig(
        cwd="/repo-lock", model="model", provider_id="provider", reasoning_effort="",
        run_mode="fork", dispatch="http", on_no_fork="error", node_id="primary",
        backend_url="http://localhost:8000", internal_token="token",
        provisioned_session_id=None, caller_session_id=None,
        worker_description="worker:budget-test",
    )
    lock = prov_manager._lifecycle_lock(spec, cfg)
    lock.acquire()
    started = time.monotonic()
    try:
        with prov_manager._acquired_lifecycle_lock(spec, cfg):
            print(f"{FAIL} lifecycle lock: acquired while held")
            return False
    except TimeoutError:
        elapsed = time.monotonic() - started
        if elapsed > 5.0:
            print(f"{FAIL} lifecycle lock: waited {elapsed:.1f}s — used dispatch_timeout?")
            return False
    finally:
        lock.release()
    print(f"{PASS} lifecycle lock budget stays on provision_timeout")
    return True


def test_startup_wires_requirements_processor_prewarm() -> bool:
    import requirement_prewarm

    main_src = (Path(_BACKEND) / "main.py").read_text(encoding="utf-8")
    if "requirements-processor-prewarm" not in main_src:
        print(f"{FAIL} startup wiring: prewarm task not created in main.py")
        return False
    if "run_requirements_prewarm" not in main_src:
        print(f"{FAIL} startup wiring: run_requirements_prewarm not called from main.py")
        return False
    orchestrator_src = main_src[main_src.index("async def _on_startup_bg_orchestrator"):]
    reconcile_index = orchestrator_src.index("list_extensions_with_reconciliation")
    tags_index = orchestrator_src.index("bind_requirement_tags_loop(loop)")
    prewarm_index = orchestrator_src.index('"requirements_processor_prewarm"')
    if not reconcile_index < tags_index < prewarm_index:
        print(f"{FAIL} startup wiring: requirements consumers race extension reconciliation")
        return False
    prewarm_src = Path(requirement_prewarm.__file__).read_text(encoding="utf-8")
    if "ensure_warm_base" not in prewarm_src:
        print(f"{FAIL} prewarm: does not warm the provisioned processor base")
        return False
    print(f"{PASS} startup wires requirements processor base prewarm")
    return True


def test_working_mode_lookup_reads_hidden_sessions() -> bool:
    class _FakeSessionManager:
        def __init__(self) -> None:
            self.iter_all_calls = 0

        def list(self) -> list[dict]:
            return []

        def iter_all(self) -> list[dict]:
            self.iter_all_calls += 1
            return [
                {
                    "id": "target",
                    "working_mode": "target_mode",
                    "working_mode_meta": {"cwd": "/repo", "model": "m"},
                }
            ]

    fake = _FakeSessionManager()
    original = working_mode.session_manager
    working_mode.session_manager = fake  # type: ignore[assignment]
    try:
        found = working_mode.find_working_session(
            "target_mode",
            cwd="/repo",
            model="m",
        )
    finally:
        working_mode.session_manager = original

    if not found or found.get("id") != "target":
        print(f"{FAIL} working-mode lookup: did not return hidden target")
        return False
    if fake.iter_all_calls != 1:
        print(f"{FAIL} working-mode lookup: iter_all calls {fake.iter_all_calls}")
        return False
    print(f"{PASS} working-mode lookup reads hidden sessions")
    return True


# ── entry point ───────────────────────────────────────────────────────

def main_run() -> int:
    tests = [
        test_dirty_reason,
        test_expired_reason,
        test_spec_and_registry,
        test_resolve_config_overlay,
        test_fork_capability_checks_never_resolve_credentials,
        test_resolve_config_uses_current_disk_token,
        test_dispatch_sends_resolved_disk_token,
        test_extract_fork_text,
        test_run_serializes_lifecycle_creation,
        test_run_lifecycle_runs_off_event_loop,
        test_lifecycle_lock_timeout_surfaces,
        test_ensure_warm_base_initializes_once,
        test_run_sync_times_out_stuck_dispatch,
        test_sync_timeout_composes_lifecycle_and_dispatch_budgets,
        test_dispatch_uses_dispatch_timeout_per_attempt,
        test_in_process_dispatch_uses_explicit_delegation_id,
        test_run_honors_client_delegation_id_from_ctx,
        test_run_logs_phase_timings_for_debug_requests,
        test_delegation_tool_activity_detector_reads_canonical_message_content,
        test_run_sync_survives_lifecycle_plus_full_dispatch,
        test_lifecycle_lock_budget_stays_on_provision_timeout,
        test_startup_wires_requirements_processor_prewarm,
        test_working_mode_lookup_reads_hidden_sessions,
    ]
    results = []
    for fn in tests:
        try:
            results.append(fn())
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"{FAIL} {fn.__name__} raised: {e}")
            results.append(False)
    n_pass = sum(1 for r in results if r)
    n_total = len(results)
    print(f"\n{n_pass}/{n_total} provisioning-framework unit tests passed")
    shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"], ignore_errors=True)
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main_run())
