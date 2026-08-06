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
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import _test_home
_test_home.isolate("bc-test-provisioning-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import provisioning  # noqa: E402
import config_store  # noqa: E402
import delegation_status_store  # noqa: E402
import session_store  # noqa: E402
import provisioning.config as prov_config  # noqa: E402
import provisioning.dispatch as prov_dispatch  # noqa: E402
import provisioning.inline_spec as inline_spec  # noqa: E402
import provisioning.lifecycle as prov_lifecycle  # noqa: E402
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


def _test_provider_resolver(resolve_provider_ref):
    def resolve(provider_id: str):
        if provider_id == "provider":
            return {"id": provider_id, "supports_fork": True}
        return resolve_provider_ref(provider_id)

    return resolve


@pytest.fixture(autouse=True)
def _fork_capable_test_provider(monkeypatch):
    monkeypatch.setattr(
        config_store,
        "resolve_provider_ref",
        _test_provider_resolver(config_store.resolve_provider_ref),
    )


async def _ready_base_without_provider(
    spec, cfg, _ctx, *, milestone_callback=None,
):
    return await asyncio.to_thread(prov_manager.ensure_session, spec, cfg)


# ── dirty_reason ──────────────────────────────────────────────────────

def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_dirty_reason() -> None:
    policy = DirtyPolicy(
        max_base_bytes=1000,
        max_user_turns=1,
        max_assistant_turns=1,
        leak_markers=("LEAKED_QUERY_MARKER",),
    )
    cwd = "/tmp/proj"

    # No agent_sid yet → clean (not provisioned).
    assert not dirty_reason({}, policy, cwd), "dirty: no agent_sid should be clean"

    assert "no provider session id" in dirty_reason(
        {"messages": [{"role": "user", "content": "stuck prep"}]},
        policy,
        cwd,
    ), "dirty: failed initialization not detected"

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
        assert not dirty_reason({"agent_session_id": clean}, policy, cwd), "dirty: clean base flagged dirty"

        # Dirty: too big.
        big = _seed("big-sid", [{"type": "user", "message": {"content": "x" * 2000}}])
        assert dirty_reason({"agent_session_id": big}, policy, cwd), "dirty: oversized base not flagged"

        # Dirty: a second user turn (a query leaked into the base).
        two = _seed("two-sid", [
            {"type": "user", "message": {"content": "provision"}},
            {"type": "assistant", "message": {"content": "ready"}},
            {"type": "user", "message": {"content": "second turn leaked"}},
        ])
        assert dirty_reason({"agent_session_id": two}, policy, cwd), "dirty: 2 user-turn base not flagged"

        # Dirty: leak marker in a user turn.
        leak = _seed("leak-sid", [
            {"type": "user", "message": {"content": "LEAKED_QUERY_MARKER stuff"}},
        ])
        assert dirty_reason({"agent_session_id": leak}, policy, cwd), "dirty: leak marker not flagged"

        # Dirty: API-error assistant turn.
        err = _seed("err-sid", [
            {"type": "user", "message": {"content": "provision"}},
            {"type": "assistant", "message": {"content": "x"}, "isApiErrorMessage": True},
        ])
        assert dirty_reason({"agent_session_id": err}, policy, cwd), "dirty: api-error turn not flagged"
    finally:
        jh.compute_jsonl_path = original  # type: ignore[assignment]

    print(f"{PASS} dirty_reason: clean / failed-init / size / turn-count / leak / api-error")


# ── expired_reason (lifetime recycling) ───────────────────────────────

def test_expired_reason() -> None:
    class _Fresh(ProvisionedSessionSpec):
        lifetime_seconds = 60.0

    class _NoLifetime(ProvisionedSessionSpec):
        lifetime_seconds = None

    fresh = _Fresh()
    now = time.time()

    # No lifetime configured ⇒ never expired.
    assert not expired_reason({"working_mode_meta": {"provisioned_at": 0}}, _NoLifetime()), "expired: no-lifetime spec flagged expired"

    # Fresh stamp ⇒ not expired.
    assert not expired_reason({"working_mode_meta": {"provisioned_at": now}}, fresh), "expired: fresh base flagged expired"

    # Old stamp ⇒ expired.
    old = {"working_mode_meta": {"provisioned_at": now - 120.0}}
    assert expired_reason(old, fresh), "expired: aged base not flagged"

    # Missing stamp (predates lifetime tracking) ⇒ expired so it gets re-stamped.
    assert expired_reason({"working_mode_meta": {}}, fresh), "expired: unstamped base not flagged"

    print(f"{PASS} expired_reason: fresh / aged / unstamped / no-lifetime")


# ── spec + registry ───────────────────────────────────────────────────

def test_spec_and_registry() -> None:
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
    assert s.machine_completion is False and s.bare_config is False, "spec: subclass override ignored"
    assert provisioning.get("unit_test_spec") is s, "registry: get did not return registered instance"
    # Defaults from the base class survive.
    assert _S().run_mode == "fork" and _S().dispatch == "http" and _S().ephemeral_forks is True, "spec: base defaults wrong"
    # build_instructions default = just the query.
    assert _S().build_instructions("hello", {}) == "hello", "spec: default build_instructions not identity"
    print(f"{PASS} ProvisionedSessionSpec overrides + registry")


# ── resolve_config ────────────────────────────────────────────────────

def test_resolve_config_overlay() -> None:
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
    assert cfg.model == "overridden-model" and cfg.dispatch == "http", f"resolve_config: env overlay not applied (model={cfg.model}, dispatch={cfg.dispatch})"

    # Invalid choice raises.
    os.environ["CFG_TEST_DISPATCH"] = "bogus"
    try:
        with pytest.raises(RuntimeError):
            resolve_config(_S())
    finally:
        del os.environ["CFG_TEST_DISPATCH"]

    class _S2(ProvisionedSessionSpec):
        key = "cfg_test_spec2"
        env_prefix = "CFG_TEST2"
        task_key = ""  # no app-settings resolution
    with pytest.raises(RuntimeError):
        resolve_config(_S2())
    print(f"{PASS} resolve_config: env overlay + choice validation + missing model rejection")


def test_resolve_config_uses_runtime_profile_authority_and_typed_errors() -> None:
    import config_store

    class _S(ProvisionedSessionSpec):
        key = "runtime_profile_config_spec"
        env_prefix = "RUNTIME_PROFILE_CONFIG"
        task_key = "requirement_analysis"

    original_resolve_task = config_store.resolve_internal_llm
    original_resolve_provider = config_store.resolve_provider_ref
    resolved = {
        "provider_id": "profile-provider",
        "model": "glm-5.2",
        "reasoning_effort": "medium",
        "runner": "native",
        "runtime_profile_id": "profile-id",
    }
    config_store.resolve_internal_llm = lambda _task: dict(resolved)
    config_store.resolve_provider_ref = lambda provider_id: {
        "id": provider_id,
        "supports_fork": True,
        # Schema v3 owns the model on the runtime profile, not here.
        "custom_models": [],
    }
    try:
        cfg = resolve_config(_S())
        assert (cfg.provider_id, cfg.model, cfg.runner) == (
            "profile-provider", "glm-5.2", "native",
        )

        resolved["provider_id"] = ""
        with pytest.raises(
            getattr(prov_config, "ProvisionedConfigurationError"),
            match="has no provider configured",
        ) as missing_provider:
            resolve_config(_S())
        assert missing_provider.value.code == "missing_provider"

        resolved["provider_id"] = "direct-only"
        config_store.resolve_provider_ref = lambda provider_id: {
            "id": provider_id,
            "supports_fork": False,
        }
        with pytest.raises(
            getattr(prov_config, "ProvisionedConfigurationError"),
            match="requires a fork-capable provider",
        ) as fork_unsupported:
            resolve_config(_S())
        assert fork_unsupported.value.code == "fork_unsupported"
    finally:
        config_store.resolve_internal_llm = original_resolve_task
        config_store.resolve_provider_ref = original_resolve_provider
    print(f"{PASS} resolve_config: runtime-profile authority + typed failures")


def test_custom_config_rejects_unsupported_fork_provider() -> None:
    class _S(ProvisionedSessionSpec):
        key = "custom_config_fork_validation"
        env_prefix = "CUSTOM_CONFIG_FORK_VALIDATION"

        def build_config(self, *, model=None):
            return ProvisionedConfig(
                cwd="/repo",
                model=model or "model",
                provider_id="custom-direct-only",
                reasoning_effort="",
                run_mode="fork",
                dispatch="http",
                on_no_fork="error",
                node_id="primary",
                backend_url="http://localhost:8000",
                internal_token="token",
                provisioned_session_id=None,
                caller_session_id=None,
                worker_description="worker:custom-config-fork-validation",
            )

    original_resolve_provider = config_store.resolve_provider_ref
    config_store.resolve_provider_ref = lambda provider_id: {
        "id": provider_id,
        "supports_fork": False,
    }
    try:
        with pytest.raises(
            getattr(prov_config, "ProvisionedConfigurationError"),
            match="requires a fork-capable provider",
        ) as fork_unsupported:
            resolve_config(_S())
    finally:
        config_store.resolve_provider_ref = original_resolve_provider
    assert fork_unsupported.value.code == "fork_unsupported"


def test_fork_capability_checks_never_resolve_credentials() -> None:
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
    assert values == (True, True, False, False, True), "provisioning fork checks avoid credential reads"
    print(f"{PASS} provisioning fork checks avoid credential reads")


def test_resolve_config_uses_current_disk_token() -> None:
    class _S(ProvisionedSessionSpec):
        key = "cfg_token_spec"
        env_prefix = "CFG_TOKEN"
        task_key = ""
        dispatch = "http"
        default_model = "model"
        run_mode = "direct"

    token_path = Path(os.environ["BETTER_CLAUDE_HOME"]) / "internal_token"
    disk_token = "d" * 32
    token_path.write_text(disk_token, encoding="utf-8")
    original_env = {
        "BETTER_AGENT_INTERNAL_TOKEN": os.environ.get("BETTER_AGENT_INTERNAL_TOKEN"),
        "BETTER_CLAUDE_INTERNAL_TOKEN": os.environ.get("BETTER_CLAUDE_INTERNAL_TOKEN"),
        "CFG_TOKEN_INTERNAL_TOKEN": os.environ.get("CFG_TOKEN_INTERNAL_TOKEN"),
        "CFG_TOKEN_PROVIDER_ID": os.environ.get("CFG_TOKEN_PROVIDER_ID"),
    }
    try:
        os.environ["BETTER_AGENT_INTERNAL_TOKEN"] = "a" * 32
        os.environ["BETTER_CLAUDE_INTERNAL_TOKEN"] = "l" * 32
        os.environ["CFG_TOKEN_PROVIDER_ID"] = "provider"
        os.environ.pop("CFG_TOKEN_INTERNAL_TOKEN", None)
        cfg = resolve_config(_S())
        assert cfg.internal_token == disk_token, "resolve_config token: disk token did not beat stale env"

        explicit_token = "x" * 32
        os.environ["CFG_TOKEN_INTERNAL_TOKEN"] = explicit_token
        cfg = resolve_config(_S())
        assert cfg.internal_token == explicit_token, "resolve_config token: explicit spec token did not win"

        token_path.unlink()
        os.environ.pop("CFG_TOKEN_INTERNAL_TOKEN", None)
        cfg = resolve_config(_S())
        assert cfg.internal_token == "a" * 32, "resolve_config token: env fallback did not survive missing disk"
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        token_path.unlink(missing_ok=True)
    print(f"{PASS} resolve_config token: explicit > disk > env")


def test_dispatch_sends_resolved_disk_token() -> None:
    class _S(ProvisionedSessionSpec):
        key = "cfg_dispatch_token_spec"
        env_prefix = "CFG_DISPATCH_TOKEN"
        task_key = ""
        dispatch = "http"
        default_model = "model"

    token_path = Path(os.environ["BETTER_CLAUDE_HOME"]) / "internal_token"
    disk_token = "d" * 32
    token_path.write_text(disk_token, encoding="utf-8")
    original_env = {
        "BETTER_AGENT_INTERNAL_TOKEN": os.environ.get("BETTER_AGENT_INTERNAL_TOKEN"),
        "BETTER_CLAUDE_INTERNAL_TOKEN": os.environ.get("BETTER_CLAUDE_INTERNAL_TOKEN"),
        "CFG_DISPATCH_TOKEN_PROVIDER_ID": os.environ.get("CFG_DISPATCH_TOKEN_PROVIDER_ID"),
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
        os.environ["CFG_DISPATCH_TOKEN_PROVIDER_ID"] = "provider"
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
    assert captured == [disk_token], f"dispatch uses resolved disk token (captured={captured!r})"
    print(f"{PASS} dispatch uses resolved disk token (captured={captured!r})")


# ── extract_fork_text ─────────────────────────────────────────────────

def test_extract_fork_text() -> None:
    # sdk_output short-circuits.
    assert extract_fork_text({"sdk_output": "  hello  "}) == "hello", "extract: sdk_output path"

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
    assert text == "second", f"extract: jsonl byte window got {text!r}"
    print(f"{PASS} extract_fork_text: sdk_output + jsonl byte window")


def test_run_serializes_lifecycle_creation() -> None:
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

    def fake_ensure_session(spec, cfg, **_kwargs):
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

    assert not errors, f"lifecycle lock: concurrent run failed with {errors[0]}"
    assert max_active == 1, f"lifecycle lock: ensure_session ran concurrently (max_active={max_active})"
    print(f"{PASS} lifecycle lock: base/caller creation serialized")


def test_run_lifecycle_runs_off_event_loop() -> None:
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

    def fake_ensure_session(spec, cfg, **_kwargs):
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

    assert result.base_session_id == "base" and result.caller_session_id == "caller", "lifecycle off-loop: wrong lifecycle ids"
    assert len(lifecycle_threads) == 2 and dispatch_thread, "lifecycle off-loop: missing lifecycle/dispatch calls"
    assert not any(tid == dispatch_thread[0] for _name, tid in lifecycle_threads), "lifecycle off-loop: lifecycle ran on event-loop thread"
    assert lifecycle_threads[0][0] == "base" and lifecycle_threads[1][0] == "caller", f"lifecycle off-loop: wrong call order {lifecycle_threads}"
    assert lifecycle_threads[0][1] == lifecycle_threads[1][1], "lifecycle off-loop: base/caller split across worker threads"
    print(f"{PASS} lifecycle off-loop: base/caller creation runs off event loop")


def test_lifecycle_lock_timeout_surfaces() -> None:
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
        with pytest.raises(TimeoutError) as excinfo:
            prov_manager.run_sync(spec, "", {})
        elapsed = time.monotonic() - started
        assert "lifecycle lock timed out" in str(excinfo.value), f"lifecycle timeout: wrong error {excinfo.value}"
        assert elapsed <= 1.0, f"lifecycle timeout: took too long ({elapsed:.3f}s)"
        print(f"{PASS} lifecycle lock timeout surfaces")
    finally:
        lock.release()


def test_ensure_warm_base_initializes_once() -> None:
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
    milestones: list[str] = []

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
        prov_manager.ensure_session = lambda _spec, _cfg, **_kwargs: "base"
        sys.modules["session_manager"] = fake_sm_mod
        sys.modules["main"] = fake_main_mod
        first = asyncio.run(prov_manager.ensure_warm_base(
            spec,
            cfg,
            {},
            milestone_callback=lambda name, _fields: milestones.append(name),
        ))
        second = asyncio.run(prov_manager.ensure_warm_base(
            spec,
            cfg,
            {},
            milestone_callback=lambda name, _fields: milestones.append(name),
        ))
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

    assert first == "base" and second == "base", f"warm_base: wrong base ids {first!r}/{second!r}"
    assert calls == 1, f"warm_base: expected one init call, got {calls}"
    assert sessions["base"].get("agent_session_id") == "agent-sid", "warm_base: sid not persisted"
    expected = [
        "lifecycle_lock_waiting",
        "lifecycle_lock_acquired",
        "base_session_resolving",
        "base_session_resolved",
        "base_session_warming",
        "base_session_warmed",
        "base_session_persisting",
        "base_session_ready",
        "lifecycle_lock_waiting",
        "lifecycle_lock_acquired",
        "base_session_resolving",
        "base_session_resolved",
        "base_session_ready",
    ]
    assert milestones == expected, f"warm_base milestones: {milestones!r}"
    print(f"{PASS} ensure_warm_base initializes only unwarmed bases")


def test_run_sync_times_out_stuck_dispatch() -> None:
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
        prov_manager.ensure_session = lambda spec, cfg, **_kwargs: "base"
        prov_manager.ensure_caller = lambda spec, cfg: "caller"
        prov_manager.dispatch = stuck_dispatch
        prov_manager._ensure_ready_base_locked = _ready_base_without_provider
        started = time.monotonic()
        with pytest.raises(TimeoutError) as excinfo:
            prov_manager.run_sync(_S(), "", {})
        elapsed = time.monotonic() - started
        assert "provisioned run timed out" in str(excinfo.value), f"dispatch timeout: wrong error {excinfo.value}"
        assert elapsed <= 1.0, f"dispatch timeout: took too long ({elapsed:.3f}s)"
        print(f"{PASS} dispatch timeout surfaces")
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


def test_sync_timeout_composes_lifecycle_and_dispatch_budgets() -> None:
    total = prov_manager._sync_timeout_seconds(_budget_spec(55.0, 45.0))
    assert total == 100.5, f"budget composition: expected 100.5, got {total}"
    default_total = prov_manager._sync_timeout_seconds(_budget_spec(10.0, None, retry_attempts=2))
    # lifecycle 10 + dispatch 10×2 + backoff 2.0 + 0.5
    assert default_total == 32.5, f"budget composition default: expected 32.5, got {default_total}"
    print(f"{PASS} run_sync budget composes lifecycle + dispatch phases")


def test_dispatch_uses_dispatch_timeout_per_attempt() -> None:
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
    assert seen == [(7.0, "explicit-delegation")], f"dispatch timeout/id kwargs: got {seen}"
    print(f"{PASS} dispatch attempts use dispatch_timeout, not provision_timeout")


def test_in_process_dispatch_uses_explicit_delegation_id() -> None:
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
    assert captured.get("client_delegation_id") == "explicit-in-process", f"in-process dispatch id: {captured!r}"
    print(f"{PASS} in-process dispatch uses explicit delegation id")


def test_run_honors_client_delegation_id_from_ctx() -> None:
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
        prov_manager.ensure_session = lambda spec_, cfg_, **_kwargs: "base"
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
    assert captured.get("client_delegation_id") == "job-owned-id", f"run client_delegation_id from ctx: {captured!r}"
    print(f"{PASS} run honors client_delegation_id from ctx")


def test_run_emits_provider_neutral_milestones() -> None:
    spec = _budget_spec(55.0, 7.0)
    original_ensure_session = prov_manager.ensure_session
    original_ensure_caller = prov_manager.ensure_caller
    original_dispatch = prov_manager.dispatch
    original_ready_base = prov_manager._ensure_ready_base_locked
    observed: list[tuple[str, dict]] = []

    async def fake_dispatch(*args, **kwargs):
        callback = kwargs["milestone_callback"]
        callback("delegation_resolving", {"delegation_id": "job-owned-id"})
        callback("runner_started", {"provider_run_id": "run-1", "worker_pid": 123})
        return {"success": True, "sdk_output": "ok"}

    try:
        prov_manager.ensure_session = lambda spec_, cfg_, **_kwargs: "base"
        prov_manager.ensure_caller = lambda spec_, cfg_: "caller"
        prov_manager.dispatch = fake_dispatch
        prov_manager._ensure_ready_base_locked = _ready_base_without_provider
        asyncio.run(prov_manager.run(
            spec,
            "query",
            {"client_delegation_id": "job-owned-id"},
            milestone_callback=lambda name, fields: observed.append((name, fields)),
        ))
    finally:
        prov_manager.ensure_session = original_ensure_session
        prov_manager.ensure_caller = original_ensure_caller
        prov_manager.dispatch = original_dispatch
        prov_manager._ensure_ready_base_locked = original_ready_base

    assert [name for name, _fields in observed] == [
        "provisioning_started",
        "configuration_resolved",
        "lifecycle_started",
        "lifecycle_lock_waiting",
        "lifecycle_lock_acquired",
        "caller_session_resolving",
        "caller_session_ready",
        "lifecycle_ready",
        "dispatch_started",
        "delegation_resolving",
        "runner_started",
        "dispatch_complete",
        "result_parsed",
    ]
    assert observed[1][1]["provider_id"] == spec.build_config().provider_id
    assert observed[7][1] == {
        "base_session_id": "base",
        "caller_session_id": "caller",
    }


def test_ensure_session_emits_registry_milestones() -> None:
    spec = _budget_spec(55.0, 7.0)
    cfg = spec.build_config()
    existing = {
        "id": "base",
        "provider_id": cfg.provider_id,
        "model": cfg.model,
        "runner": cfg.runner,
        "node_id": cfg.node_id,
        "storage_scope": spec.storage_scope,
        "working_mode_meta": {"provisioned_at": time.time()},
    }
    observed: list[str] = []
    original_find = prov_lifecycle._find
    original_dirty_reason = prov_lifecycle.dirty_reason
    original_expired_reason = prov_lifecycle.expired_reason
    original_upsert_worker = prov_lifecycle._upsert_worker
    try:
        prov_lifecycle._find = lambda _spec, _cfg: existing
        prov_lifecycle.dirty_reason = lambda _session, _policy, _cwd: ""
        prov_lifecycle.expired_reason = lambda _session, _spec: ""
        prov_lifecycle._upsert_worker = lambda _cwd, _session: None
        session_id = prov_lifecycle.ensure_session(
            spec,
            cfg,
            milestone_callback=lambda name, _fields: observed.append(name),
        )
    finally:
        prov_lifecycle._find = original_find
        prov_lifecycle.dirty_reason = original_dirty_reason
        prov_lifecycle.expired_reason = original_expired_reason
        prov_lifecycle._upsert_worker = original_upsert_worker

    assert session_id == "base"
    assert observed == [
        "base_registry_lookup",
        "base_cleanliness_check",
        "base_worker_projection_sync",
    ]


def test_run_ignores_milestone_callback_failures() -> None:
    spec = _budget_spec(55.0, 7.0)
    original_ensure_session = prov_manager.ensure_session
    original_ensure_caller = prov_manager.ensure_caller
    original_dispatch = prov_manager.dispatch
    original_ready_base = prov_manager._ensure_ready_base_locked

    async def fake_dispatch(*_args, **_kwargs):
        return {"success": True, "sdk_output": "ok"}

    def failing_callback(_name: str, _fields: dict) -> None:
        raise RuntimeError("milestone sink unavailable")

    try:
        prov_manager.ensure_session = lambda spec_, cfg_, **_kwargs: "base"
        prov_manager.ensure_caller = lambda spec_, cfg_: "caller"
        prov_manager.dispatch = fake_dispatch
        prov_manager._ensure_ready_base_locked = _ready_base_without_provider
        result = asyncio.run(prov_manager.run(
            spec,
            "query",
            milestone_callback=failing_callback,
        ))
    finally:
        prov_manager.ensure_session = original_ensure_session
        prov_manager.ensure_caller = original_ensure_caller
        prov_manager.dispatch = original_dispatch
        prov_manager._ensure_ready_base_locked = original_ready_base

    assert result.text == "ok"


def test_dispatch_projects_delegation_status_to_milestones() -> None:
    spec = _budget_spec(55.0, 7.0)
    cfg = spec.build_config()
    cfg.dispatch = "in_process"
    observed: list[tuple[str, dict]] = []
    original_main = sys.modules.get("main")

    class _Coordinator:
        async def run_delegation(self, **kwargs):
            delegation_id = kwargs["client_delegation_id"]
            delegation_status_store.write_status(delegation_id, status="resolving")
            delegation_status_store.write_status(delegation_id, status="queued")
            delegation_status_store.write_status(
                delegation_id,
                stage="delegation_run_state_registering",
            )
            delegation_status_store.write_status(
                delegation_id,
                stage="delegation_lock_waiting",
            )
            delegation_status_store.write_status(
                delegation_id,
                stage="delegation_lock_acquired",
            )
            for stage in (
                "delegation_fork_resolving",
                "delegation_provider_resolving",
                "delegation_team_context_resolving",
                "delegation_recovery_waiting",
                "delegation_root_persist_flushing",
                "delegation_runner_starting",
            ):
                delegation_status_store.write_status(delegation_id, stage=stage)
            delegation_status_store.write_status(
                delegation_id,
                status="running",
                provider_id="zai",
                provider_run_id="run-1",
                worker_pid=321,
            )
            delegation_status_store.write_status(
                delegation_id,
                status="running",
                fork_agent_sid="native-1",
                jsonl_path="/native/session.jsonl",
            )
            return {"success": True, "sdk_output": "ok"}

    sys.modules["main"] = SimpleNamespace(coordinator=_Coordinator())
    try:
        asyncio.run(prov_dispatch.dispatch(
            spec,
            cfg,
            base_session_id="base",
            caller_session_id="caller",
            instructions="instructions",
            provision_prompt="provision",
            client_delegation_id="milestone-dispatch",
            milestone_callback=lambda name, fields: observed.append((name, fields)),
        ))
    finally:
        if original_main is None:
            sys.modules.pop("main", None)
        else:
            sys.modules["main"] = original_main

    assert [name for name, _fields in observed] == [
        "delegation_resolving",
        "delegation_queued",
        "delegation_run_state_registering",
        "delegation_lock_waiting",
        "delegation_lock_acquired",
        "delegation_fork_resolving",
        "delegation_provider_resolving",
        "delegation_team_context_resolving",
        "delegation_recovery_waiting",
        "delegation_root_persist_flushing",
        "delegation_runner_starting",
        "runner_started",
        "native_session_started",
    ]
    assert observed[-1][1]["native_session_id"] == "native-1"
    assert observed[-1][1]["native_session_file_path"] == "/native/session.jsonl"
    assert "milestone-dispatch" not in delegation_status_store._LISTENERS


def test_http_dispatch_projects_delegation_status_to_milestones() -> None:
    spec = _budget_spec(55.0, 7.0)
    cfg = spec.build_config()
    observed: list[tuple[str, dict]] = []
    original_post = prov_dispatch._post_ask_fork

    async def fake_post(_cfg, payload, *, timeout):
        assert timeout == spec.effective_dispatch_timeout
        delegation_id = payload["client_delegation_id"]
        delegation_status_store.write_status(delegation_id, status="resolving")
        delegation_status_store.write_status(delegation_id, status="queued")
        delegation_status_store.write_status(
            delegation_id,
            stage="delegation_run_state_registering",
        )
        delegation_status_store.write_status(
            delegation_id,
            stage="delegation_lock_waiting",
        )
        delegation_status_store.write_status(
            delegation_id,
            stage="delegation_lock_acquired",
        )
        for stage in (
            "delegation_fork_resolving",
            "delegation_provider_resolving",
            "delegation_team_context_resolving",
            "delegation_recovery_waiting",
            "delegation_root_persist_flushing",
            "delegation_runner_starting",
        ):
            delegation_status_store.write_status(delegation_id, stage=stage)
        delegation_status_store.write_status(
            delegation_id,
            status="running",
            provider_run_id="run-http",
            worker_pid=654,
        )
        return {"success": True, "sdk_output": "ok"}

    try:
        prov_dispatch._post_ask_fork = fake_post
        asyncio.run(prov_dispatch.dispatch(
            spec,
            cfg,
            base_session_id="base",
            caller_session_id="caller",
            instructions="instructions",
            provision_prompt="provision",
            client_delegation_id="milestone-http-dispatch",
            milestone_callback=lambda name, fields: observed.append((name, fields)),
        ))
    finally:
        prov_dispatch._post_ask_fork = original_post

    assert [name for name, _fields in observed] == [
        "delegation_resolving",
        "delegation_queued",
        "delegation_run_state_registering",
        "delegation_lock_waiting",
        "delegation_lock_acquired",
        "delegation_fork_resolving",
        "delegation_provider_resolving",
        "delegation_team_context_resolving",
        "delegation_recovery_waiting",
        "delegation_root_persist_flushing",
        "delegation_runner_starting",
        "runner_started",
    ]
    assert "milestone-http-dispatch" not in delegation_status_store._LISTENERS


def test_run_logs_phase_timings_for_debug_requests() -> None:
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
        prov_manager.ensure_session = lambda spec_, cfg_, **_kwargs: "base"
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
    assert timing_rows, "phase timings: no provisioned_run_timing log"
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
    assert all(part in timing_text for part in expected), f"phase timings: missing fields in {timing_text!r}"
    print(f"{PASS} run logs phase timings for debug requests")


def test_delegation_tool_activity_detector_reads_canonical_message_content() -> None:
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
    assert _delegation_event_is_tool_activity(event), "delegation timing: canonical tool_use block not detected"
    print(f"{PASS} delegation timing detects canonical tool activity")


def test_run_sync_survives_lifecycle_plus_full_dispatch() -> None:
    """Lifecycle and dispatch each within their own budget, but their SUM
    above the old provision_timeout+0.5 total — must succeed post-fix."""
    spec = _budget_spec(1.0, 1.0)

    def slow_ensure_session(spec_, cfg_, **_kwargs):
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
    finally:
        prov_manager.ensure_session = original_ensure_session
        prov_manager.ensure_caller = original_ensure_caller
        prov_manager.dispatch = original_dispatch
        prov_manager._ensure_ready_base_locked = original_ready_base
    assert result.text == "late-but-legal", f"phase budgets: wrong result {result.text!r}"
    print(f"{PASS} run_sync tolerates lifecycle + dispatch each using their own budget")


def test_lifecycle_lock_budget_stays_on_provision_timeout() -> None:
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
        with pytest.raises(TimeoutError):
            with prov_manager._acquired_lifecycle_lock(spec, cfg):
                pass
        elapsed = time.monotonic() - started
        assert elapsed <= 5.0, f"lifecycle lock: waited {elapsed:.1f}s — used dispatch_timeout?"
    finally:
        lock.release()
    print(f"{PASS} lifecycle lock budget stays on provision_timeout")


def test_startup_wires_requirements_processor_prewarm() -> None:
    import requirement_prewarm

    main_src = (Path(_BACKEND) / "app_lifecycle.py").read_text(encoding="utf-8")
    assert "requirements-processor-prewarm" in main_src, "startup wiring: prewarm task not created in main.py"
    assert "run_requirements_prewarm" in main_src, "startup wiring: run_requirements_prewarm not called from main.py"
    orchestrator_src = main_src[main_src.index("async def _on_startup_bg_orchestrator"):]
    reconcile_index = orchestrator_src.index("list_extensions_with_reconciliation")
    tags_index = orchestrator_src.index("bind_requirement_tags_loop(loop)")
    prewarm_index = orchestrator_src.index('"requirements_processor_prewarm"')
    assert reconcile_index < tags_index < prewarm_index, "startup wiring: requirements consumers race extension reconciliation"
    prewarm_src = Path(requirement_prewarm.__file__).read_text(encoding="utf-8")
    assert "ensure_warm_base" in prewarm_src, "prewarm: does not warm the provisioned processor base"
    print(f"{PASS} startup wires requirements processor base prewarm")


def test_working_mode_lookup_uses_explicit_entity_scopes() -> None:
    class _FakeSessionManager:
        def __init__(self) -> None:
            self.root_calls = 0
            self.full_root_calls = 0
            self.get_calls: list[str] = []
            self.fork_calls = 0
            self.any_calls = 0

        def list(self) -> list[dict]:
            return []

        def find_root_working_session_summaries(
            self, mode: str, match: dict[str, object],
        ) -> list[dict]:
            self.root_calls += 1
            if mode != "target_mode" or match != {"cwd": "/repo", "model": "root"}:
                return []
            return [
                {
                    "id": "root-stale",
                    "working_mode": "target_mode",
                    "working_mode_meta": {"cwd": "/repo", "model": "root"},
                },
                {
                    "id": "root-target",
                    "working_mode": "target_mode",
                    "working_mode_meta": {"cwd": "/repo", "model": "root"},
                }
            ]

        def iter_root_sessions(self) -> list[dict]:
            self.full_root_calls += 1
            raise AssertionError("root lookup must not enumerate full session trees")

        def get(self, session_id: str) -> dict | None:
            self.get_calls.append(session_id)
            if session_id == "root-stale":
                return {
                    "id": "root-stale",
                    "working_mode": "other_mode",
                    "working_mode_meta": {"cwd": "/repo", "model": "root"},
                }
            if session_id != "root-target":
                return None
            return {
                "id": "root-target",
                "working_mode": "target_mode",
                "working_mode_meta": {"cwd": "/repo", "model": "root"},
                "messages": [],
            }

        def iter_fork_sessions(self) -> list[dict]:
            self.fork_calls += 1
            return [
                {
                    "id": "fork-target",
                    "working_mode": "target_mode",
                    "working_mode_meta": {"cwd": "/repo", "model": "fork"},
                }
            ]

        def iter_all_entities(self) -> list[dict]:
            self.any_calls += 1
            return [
                {
                    "id": "any-target",
                    "working_mode": "target_mode",
                    "working_mode_meta": {"cwd": "/repo", "model": "any"},
                }
            ]

    fake = _FakeSessionManager()
    original = working_mode.session_manager
    working_mode.session_manager = fake  # type: ignore[assignment]
    try:
        root = working_mode.find_working_session(
            "target_mode",
            cwd="/repo",
            model="root",
        )
        fork = working_mode.find_working_session(
            "target_mode",
            scope="forks",
            cwd="/repo",
            model="fork",
        )
        any_entity = working_mode.find_working_session(
            "target_mode",
            scope="any",
            cwd="/repo",
            model="any",
        )
    finally:
        working_mode.session_manager = original

    assert root and root.get("id") == "root-target", "working-mode lookup: did not return root target"
    assert fork and fork.get("id") == "fork-target", "working-mode lookup: did not return fork target"
    assert any_entity and any_entity.get("id") == "any-target", "working-mode lookup: did not return any-entity target"
    assert (
        fake.root_calls,
        fake.full_root_calls,
        fake.get_calls,
        fake.fork_calls,
        fake.any_calls,
    ) == (1, 0, ["root-stale", "root-target"], 1, 1), (
        f"working-mode lookup: calls "
        f"{(fake.root_calls, fake.full_root_calls, fake.get_calls, fake.fork_calls, fake.any_calls)!r}"
    )
    print(f"{PASS} working-mode lookup uses explicit entity scopes")


def test_working_mode_index_stays_compact_across_restart_update_and_delete() -> None:
    sessions_dir = session_store._sessions_dir()
    sessions_dir.mkdir(parents=True, exist_ok=True)
    sid = "working-index-test"
    root_path = sessions_dir / f"{sid}.json"
    summary_path = sessions_dir / f"{sid}.summary.json"
    index_path = sessions_dir / ".working-session-index.json"
    assert session_store._is_sidecar_json(index_path.name), "working-mode index: cache is visible as a session root"
    root_path.write_text(json.dumps({
        "id": sid,
        "working_mode": "target_mode",
        "working_mode_meta": {"cwd": "/old"},
    }), encoding="utf-8")
    summary_path.write_text(json.dumps({
        "id": sid,
        "working_mode": "target_mode",
        "working_mode_meta": {"cwd": "/old"},
    }), encoding="utf-8")
    index_path.unlink(missing_ok=True)
    session_store._reset_home_scoped_caches()
    original_builder = session_store._do_build_summary_index_unsafe

    def forbidden_summary_build() -> None:
        raise AssertionError("working-mode lookup built the full summary index")

    session_store._do_build_summary_index_unsafe = forbidden_summary_build
    try:
        cold = session_store.find_root_working_session_summaries(
            "target_mode", {"cwd": "/old"},
        )
        root_path.write_text(json.dumps({
            "id": sid,
            "working_mode": "target_mode",
            "working_mode_meta": {"cwd": "/new"},
        }), encoding="utf-8")
        os.utime(index_path, ns=(1, 1))
        session_store._reset_home_scoped_caches()
        restarted = session_store.find_root_working_session_summaries(
            "target_mode", {"cwd": "/new"},
        )
        session_store._upsert_working_session_projection({
            "id": sid,
            "working_mode": "target_mode",
            "working_mode_meta": {"cwd": "/new"},
        })
        updated = session_store.find_root_working_session_summaries(
            "target_mode", {"cwd": "/new"},
        )
        stale = session_store.find_root_working_session_summaries(
            "target_mode", {"cwd": "/old"},
        )
        session_store._remove_working_session_projection(sid)
        deleted = session_store.find_root_working_session_summaries(
            "target_mode", {"cwd": "/new"},
        )
    finally:
        session_store._do_build_summary_index_unsafe = original_builder
        root_path.unlink(missing_ok=True)
        summary_path.unlink(missing_ok=True)
        index_path.unlink(missing_ok=True)
        session_store._reset_home_scoped_caches()
    assert [entry.get("id") for entry in cold] == [sid], "working-mode index: cold sidecar projection missing"
    assert (
        [entry.get("id") for entry in restarted] == [sid]
        and [entry.get("id") for entry in updated] == [sid]
        and not stale
        and not deleted
    ), "working-mode index: update/delete projection drifted"
    print(f"{PASS} working-mode index avoids full-tree hydration")


# ── entry point ───────────────────────────────────────────────────────

def main_run() -> int:
    original_resolve_provider = config_store.resolve_provider_ref
    config_store.resolve_provider_ref = _test_provider_resolver(
        original_resolve_provider,
    )
    tests = [
        test_dirty_reason,
        test_expired_reason,
        test_spec_and_registry,
        test_resolve_config_overlay,
        test_resolve_config_uses_runtime_profile_authority_and_typed_errors,
        test_custom_config_rejects_unsupported_fork_provider,
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
        test_working_mode_lookup_uses_explicit_entity_scopes,
        test_working_mode_index_stays_compact_across_restart_update_and_delete,
    ]
    try:
        results = []
        for fn in tests:
            try:
                result = fn()
                results.append(True if result is None else result)
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"{FAIL} {fn.__name__} raised: {e}")
                results.append(False)
        n_pass = sum(1 for r in results if r)
        n_total = len(results)
        print(f"\n{n_pass}/{n_total} provisioning-framework unit tests passed")
        return 0 if n_pass == n_total else 1
    finally:
        config_store.resolve_provider_ref = original_resolve_provider
        shutil.rmtree(os.environ["BETTER_CLAUDE_HOME"], ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main_run())
