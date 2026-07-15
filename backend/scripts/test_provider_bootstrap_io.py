#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import sys
import subprocess
import tempfile
import threading
from pathlib import Path

os.environ["BETTER_AGENT_HOME"] = tempfile.mkdtemp(prefix="ba-provider-bootstrap-io-")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import active_run_catalog
import provider
import runs_dir
import session_manager as session_manager_module


def test_dirty_gap_rebuilds_valid_stale_catalog() -> None:
    root = runs_dir.runs_root()
    old_dir = root / "old-run"
    old_dir.mkdir(parents=True)
    old_state = {"run_id": "old-run", "provider_id": "claude"}
    runs_dir.atomic_write_json(old_dir / "backend_state.json", old_state)
    assert active_run_catalog.load(root) == {"old-run": {"provider_id": "claude"}}

    new_dir = root / "new-run"
    new_dir.mkdir()
    new_state = {"run_id": "new-run", "provider_id": "codex"}
    original_register = active_run_catalog.CatalogTransaction.register
    active_run_catalog.CatalogTransaction.register = lambda *_args: (_ for _ in ()).throw(SystemExit("crash"))
    try:
        try:
            runs_dir.atomic_write_json(new_dir / "backend_state.json", new_state)
        except SystemExit:
            pass
    finally:
        active_run_catalog.CatalogTransaction.register = original_register

    assert (root / "active_run_catalog.dirty").exists()
    rebuilt, did_rebuild = active_run_catalog.load_or_rebuild(root)
    assert did_rebuild
    assert set(rebuilt) == {"old-run", "new-run"}
    assert not (root / "active_run_catalog.dirty").exists()


def test_process_death_between_authority_and_catalog_recovers() -> None:
    root = runs_dir.runs_root()
    run_dir = root / "crash-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    code = """
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from active_run_catalog import mark_dirty
from json_store import write_json_durable
path = Path(sys.argv[2])
mark_dirty(path.parent.parent)
write_json_durable(path, {"run_id":"crash-run","provider_id":"gemini"})
os._exit(91)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(ROOT), str(run_dir / "backend_state.json")],
        env={**os.environ, "BETTER_AGENT_HOME": os.environ["BETTER_AGENT_HOME"]},
        check=False,
    )
    assert result.returncode == 91
    assert (root / "active_run_catalog.dirty").exists()
    recovered, rebuilt = active_run_catalog.load_or_rebuild(root)
    assert rebuilt and recovered["crash-run"]["provider_id"] == "gemini"


def test_repeated_backend_state_updates_do_not_mutate_catalog() -> None:
    root = runs_dir.runs_root()
    run_dir = root / "cursor-run"
    run_dir.mkdir(parents=True)
    path = run_dir / "backend_state.json"
    initial = {"run_id": "cursor-run", "provider_id": "codex", "processed_byte": 1}
    runs_dir.atomic_write_json(path, initial)
    expected_catalog = active_run_catalog.load(root)

    original_transaction = active_run_catalog.transaction
    active_run_catalog.transaction = lambda *_args: (_ for _ in ()).throw(
        AssertionError("existing run-state update entered catalog transaction")
    )
    try:
        updated = {**initial, "processed_byte": 2}
        runs_dir.atomic_write_json(path, updated)
    finally:
        active_run_catalog.transaction = original_transaction

    assert json.loads(path.read_text(encoding="utf-8")) == updated
    assert active_run_catalog.load(root) == expected_catalog


async def test_blocking_provider_io_does_not_block_loop() -> None:
    entered = threading.Event()
    release = threading.Event()
    ticks = 0

    def blocking() -> str:
        entered.set()
        release.wait(2)
        return "done"

    async def ticker() -> None:
        nonlocal ticks
        while not release.is_set():
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(provider.run_provider_io_off_loop(blocking))
    tick_task = asyncio.create_task(ticker())
    assert await asyncio.to_thread(entered.wait, 1)
    await asyncio.sleep(0.02)
    assert ticks > 10
    release.set()
    assert await task == "done"
    await tick_task


def test_all_bootstraps_use_provider_io_boundary() -> None:
    for filename in (
        "provider_claude.py",
        "provider_codex.py",
        "provider_gemini.py",
        "provider_openai.py",
    ):
        source = (ROOT / filename).read_text(encoding="utf-8")
        start = source.index("async def _bootstrap_run(")
        end = source.find("async def ", start + 10)
        bootstrap = source[start : end if end >= 0 else len(source)]
        assert "run_provider_io_phase_off_loop" in bootstrap, filename
        assert ".read_text(encoding=\"utf-8\")" not in bootstrap, filename
        assert "self._write_backend_state(rs)" not in bootstrap, filename


def test_run_start_flushes_only_its_root() -> None:
    delegation = (ROOT / "orchs" / "manager" / "_delegation.py").read_text(
        encoding="utf-8",
    )
    node_rpc = (ROOT / "node_rpc_handlers.py").read_text(encoding="utf-8")
    for source, root_argument in (
        (delegation, "app_session_id"),
        (node_rpc, "root_id"),
    ):
        start = source.index("provider_start_run.recovery_gate")
        end = source.index("provider_start_run.provider_call", start)
        launch_fence = source[start:end]
        assert "flush_pending_persists" not in launch_fence
        assert "flush_root_persist" in launch_fence
        assert root_argument in launch_fence


def test_root_flush_is_target_only_and_fails_closed() -> None:
    manager = session_manager_module.SessionManager()
    manager._ensure_home_current()
    target = {"id": "target", "messages": [], "forks": []}
    unrelated = {"id": "unrelated", "messages": [], "forks": []}
    with session_manager_module._persist_state_changed:
        session_manager_module._persist_pending["target"] = target
        session_manager_module._persist_pending["unrelated"] = unrelated

    writes: list[str] = []
    original_write = session_manager_module.session_store.write_session_full
    session_manager_module.session_store.write_session_full = (
        lambda tree, **_kwargs: writes.append(tree["id"])
    )
    try:
        manager.flush_root_persist("target")
        assert writes == ["target"], writes
        assert "unrelated" in session_manager_module._persist_pending

        session_manager_module._persist_pending["target"] = target
        session_manager_module.session_store.write_session_full = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full"))
        )
        try:
            manager.flush_root_persist("target")
            raise AssertionError("target durability failure was swallowed")
        except OSError as exc:
            assert str(exc) == "disk full"
        assert session_manager_module._persist_pending["target"]["id"] == "target"
    finally:
        session_manager_module.session_store.write_session_full = original_write
        with session_manager_module._persist_state_changed:
            session_manager_module._persist_pending.pop("target", None)
            session_manager_module._persist_pending.pop("unrelated", None)


async def test_run_state_publication_is_loop_owned() -> None:
    class Owner:
        def __init__(self):
            self._runs = {}

    class State:
        run_id = "loop-owned"

    owner = Owner()
    state = State()
    observed_thread = None

    async def bootstrap(current) -> None:
        nonlocal observed_thread
        assert owner._runs[current.run_id] is current
        observed_thread = threading.get_ident()

    loop_thread = threading.get_ident()
    await provider.publish_run_state_and_bootstrap(owner, state, bootstrap)
    assert observed_thread == loop_thread


def main() -> None:
    test_dirty_gap_rebuilds_valid_stale_catalog()
    test_process_death_between_authority_and_catalog_recovers()
    test_repeated_backend_state_updates_do_not_mutate_catalog()
    asyncio.run(test_blocking_provider_io_does_not_block_loop())
    test_all_bootstraps_use_provider_io_boundary()
    test_run_start_flushes_only_its_root()
    test_root_flush_is_target_only_and_fails_closed()
    asyncio.run(test_run_state_publication_is_loop_owned())
    asyncio.run(provider.shutdown_provider_tasks())
    provider.reopen_provider_tasks()
    asyncio.run(provider.shutdown_provider_tasks())
    print("PASS: provider bootstrap I/O boundary, dirty-gap recovery, parity, lifecycle")


if __name__ == "__main__":
    main()
