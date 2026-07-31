"""File-edit create does not block on the provisioned base warm.

Locks the contract the user asked for: create returns immediately, the
base warms in the background, prompts sent meanwhile QUEUE, and they are
handed to the session only once the fork binding is committed.

Run with:
    cd backend && .venv/bin/python scripts/test_file_edit_background_provisioning.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import _test_home
TMP_HOME = Path(_test_home.isolate("bc-test-bg-provision-"))

import config_store  # noqa: E402
import file_editor  # noqa: E402
import models  # noqa: E402
import session_readiness  # noqa: E402
import working_mode  # noqa: E402
from orchestrator import Coordinator  # noqa: E402
from provisioning import ProvisionedConfig  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402

_RELEASE_WARM = asyncio.Event()
_WARM_SHOULD_FAIL = False
_BASE_IDS: dict[str, str] = {}


async def _blocking_warm(cfg) -> str:
    """Stand-in for the real provider warm. Blocks until the test releases
    it, so 'create did not wait' is provable rather than a timing guess."""
    await _RELEASE_WARM.wait()
    if _WARM_SHOULD_FAIL:
        raise RuntimeError("provider CLI never returned a sid")
    key = cfg.cwd
    sid = _BASE_IDS.get(key)
    if sid and session_manager.get(sid):
        return sid
    base = session_manager.create(
        name="file-editing-base",
        model=cfg.model,
        cwd=cfg.cwd,
        orchestration_mode="native",
        source="internal",
        provider_id=cfg.provider_id,
        node_id=cfg.node_id,
        worker_creation_policy="deny",
    )
    session_manager._run(
        base["id"],
        lambda s: s.__setitem__("agent_session_id", f"fake-base-sid-{len(_BASE_IDS)}"),
        {"kind": "test_agent_sid_set"},
    )
    working_mode.mark_working_mode(
        base["id"],
        mode=file_editor.BASE_MODE,
        meta={
            "cwd": cfg.cwd,
            "provider_id": cfg.provider_id,
            "model": cfg.model,
            "machine_completion": False,
            "version": file_editor.FILE_EDIT_BASE_SPEC.version,
            "node_id": cfg.node_id,
            "provisioned_at": time.time(),
        },
    )
    _BASE_IDS[key] = base["id"]
    return base["id"]


file_editor._ensure_file_edit_base = _blocking_warm  # type: ignore[assignment]

_FAKE_PROVIDER = {
    "id": "test-provider",
    "name": "Test Provider",
    "kind": "claude",
    "default_model": "test-model",
    "custom_models": ["test-model"],
    "runner": "native",
    "supports_reasoning_effort": False,
    "supports_fork": True,
}
config_store.get_provider = (  # type: ignore[assignment]
    lambda provider_id: dict(_FAKE_PROVIDER) if provider_id == "test-provider" else None
)
models.available_models = (  # type: ignore[assignment]
    lambda provider_id=None: ["test-model"] if provider_id == "test-provider" else []
)
models.available_models_including_retired = models.available_models  # type: ignore[assignment]


def _fake_file_edit_config(*, project_cwd: str, node_id: str = "primary", **_ignored):
    """Bypass provider/runner resolution — this test exercises the
    provisioning lifecycle, not config resolution."""
    return ProvisionedConfig(
        cwd=project_cwd,
        model="test-model",
        provider_id="test-provider",
        reasoning_effort="",
        run_mode="fork",
        dispatch="in_process",
        on_no_fork="error",
        node_id=node_id,
        backend_url="",
        internal_token="",
        provisioned_session_id=None,
        caller_session_id=None,
        worker_description="file editing",
        runner="native",
    )


file_editor._file_edit_config = _fake_file_edit_config  # type: ignore[assignment]


def check(cond: bool, label: str) -> None:
    if not cond:
        raise AssertionError(label)
    print(f"  ok: {label}")


def _project(label: str) -> Path:
    d = TMP_HOME / "proj" / label
    d.mkdir(parents=True, exist_ok=True)
    return d


async def test_create_returns_before_the_warm_completes() -> str:
    _RELEASE_WARM.clear()
    project = _project("nonblocking")
    result = await asyncio.wait_for(
        file_editor.start_empty(cwd=str(project), persistent=True), timeout=10
    )
    sid = result["session_id"]
    check(True, "create returned while the warm was still blocked")

    session = session_manager.get(sid) or {}
    check(
        session_readiness.state_of(session) == session_readiness.STATE_PENDING,
        "the returned session is marked as awaiting its provisioned fork",
    )
    check(
        session_readiness.is_awaiting_fork_provisioning(sid),
        "the dispatch gate holds prompts for it",
    )
    check(
        session.get("forked_from_agent_sid") in (None, ""),
        "the fork binding is genuinely not there yet",
    )
    meta = session.get("working_mode_meta") or {}
    check(
        session.get("working_mode") == file_editor.MODE
        and meta.get("project_cwd") == str(project.resolve()),
        "working_mode + meta are bound AT CREATE, so a listing snapshot is never half-formed",
    )
    check("base_session_id" not in meta, "only the warm-dependent key is deferred")
    return sid


async def test_binding_lands_and_gate_opens(sid: str) -> None:
    _RELEASE_WARM.set()
    for _ in range(200):
        if not session_readiness.is_awaiting_fork_provisioning(sid):
            break
        await asyncio.sleep(0.01)
    check(
        not session_readiness.is_awaiting_fork_provisioning(sid),
        "the gate opens once the warm completes",
    )
    session = session_manager.get(sid) or {}
    check(
        bool(session.get("forked_from_agent_sid")),
        "forked_from_agent_sid is bound (turn 1 will fork, not start fresh)",
    )
    check(
        bool((session.get("working_mode_meta") or {}).get("base_session_id")),
        "base_session_id is merged into the existing meta, not replacing it",
    )
    check(
        (session.get("working_mode_meta") or {}).get("project_cwd") is not None,
        "the create-time meta survived the merge",
    )
    check(
        session_readiness.state_of(session) is None,
        "the fact is removed once resolved",
    )


async def test_gate_defers_then_releases() -> None:
    """The core hand-off: a queued prompt does not dispatch while pending,
    and the SAME item is still queued (never dropped) when it does."""
    coordinator = Coordinator()
    sid = str(uuid.uuid4())
    session_manager.create(
        name="gate",
        cwd=str(_project("gate")),
        orchestration_mode="native",
        provider_id="test-provider",
        model="test-model",
        id=sid,
        fork_provisioning=session_readiness.pending_fact(),
    )
    q: asyncio.Queue = asyncio.Queue()
    params = {"_queued_id": "item-1", "lifecycle_msg_id": "life-1", "prompt": "hi"}

    gate = asyncio.create_task(
        coordinator._gate_on_fork_provisioning(sid, q, params)
    )
    for _ in range(5):
        await asyncio.sleep(0)
    check(not gate.done(), "the gate holds the prompt while provisioning is pending")
    check(q.qsize() == 1, "the held item is put BACK on the queue, not consumed")

    await session_readiness.mark_ready(sid)
    deferred = await asyncio.wait_for(gate, timeout=5)
    check(deferred is True, "the gate reports the item must be re-dequeued")
    check(
        await asyncio.wait_for(coordinator._gate_on_fork_provisioning(sid, q, params), timeout=5)
        is False,
        "on the next pass the gate lets the prompt through",
    )


async def test_failed_provisioning_never_dispatches() -> None:
    """Fail-closed: a failed binding must NOT fall through to a normal
    dispatch, which would run the turn unforked in the user's real cwd."""
    coordinator = Coordinator()
    sid = str(uuid.uuid4())
    session_manager.create(
        name="failed",
        cwd=str(_project("failed")),
        orchestration_mode="native",
        provider_id="test-provider",
        model="test-model",
        id=sid,
        fork_provisioning=session_readiness.pending_fact(),
    )
    session_manager.admit_queued_prompt(
        sid, {"id": "item-x", "content": "hello", "kind": "queued_behind"},
    )
    await session_readiness.mark_failed(sid, RuntimeError("no provider"))

    q: asyncio.Queue = asyncio.Queue()
    params = {"_queued_id": "item-x", "lifecycle_msg_id": "life-x", "prompt": "hello"}
    handled = await asyncio.wait_for(
        coordinator._gate_on_fork_provisioning(sid, q, params), timeout=5
    )
    check(handled is True, "a failed session's prompt is intercepted, never released")
    queued = (session_manager.get(sid) or {}).get("queued_prompts") or []
    check(
        not any(p.get("id") == "item-x" for p in queued),
        "the durable queue row is dropped so restart recovery cannot resurrect it",
    )


async def test_restart_rearms_pending_sessions() -> None:
    """A session persisted mid-warm has no live task after a restart; its
    prompts would wait on a resolution nobody would ever deliver."""
    _RELEASE_WARM.clear()
    project = _project("restart")
    result = await file_editor.start_empty(cwd=str(project), persistent=True)
    sid = result["session_id"]
    # Simulate the process dying mid-warm: drop the in-flight task.
    task = file_editor._PROVISIONING_TASKS.pop(sid, None)
    if task is not None:
        task.cancel()
    check(
        session_readiness.is_awaiting_fork_provisioning(sid),
        "the session is still pending with no live warm (the restart state)",
    )

    _RELEASE_WARM.set()
    outcome = await file_editor.recover_pending_fork_provisioning()
    check(sid in outcome["rearmed"], "startup recovery re-arms the interrupted warm")
    for _ in range(200):
        if not session_readiness.is_awaiting_fork_provisioning(sid):
            break
        await asyncio.sleep(0.01)
    check(
        bool((session_manager.get(sid) or {}).get("forked_from_agent_sid")),
        "the re-armed warm completes the binding",
    )


async def test_failed_session_can_be_rearmed() -> None:
    """`failed` must not be terminal — the user sending another prompt is
    the natural retry."""
    global _WARM_SHOULD_FAIL
    _RELEASE_WARM.set()
    _WARM_SHOULD_FAIL = True
    project = _project("rearm")
    result = await file_editor.start_empty(cwd=str(project), persistent=True)
    sid = result["session_id"]
    for _ in range(200):
        if session_readiness.fork_provisioning_failed(sid):
            break
        await asyncio.sleep(0.01)
    check(session_readiness.fork_provisioning_failed(sid), "a failing warm marks the session failed")

    _WARM_SHOULD_FAIL = False
    check(
        await file_editor.rearm_fork_provisioning_if_failed(sid) is True,
        "a failed session accepts another provisioning attempt",
    )
    for _ in range(200):
        if not session_readiness.is_awaiting_fork_provisioning(sid):
            break
        await asyncio.sleep(0.01)
    check(
        bool((session_manager.get(sid) or {}).get("forked_from_agent_sid")),
        "the retry binds the fork, so the session is usable again",
    )


async def run_all() -> None:
    sid = await test_create_returns_before_the_warm_completes()
    await test_binding_lands_and_gate_opens(sid)
    await test_gate_defers_then_releases()
    await test_failed_provisioning_never_dispatches()
    await test_restart_rearms_pending_sessions()
    await test_failed_session_can_be_rearmed()


def main_run() -> int:
    try:
        asyncio.run(run_all())
    finally:
        shutil.rmtree(TMP_HOME, ignore_errors=True)
    print("PASS file edit background provisioning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_run())
