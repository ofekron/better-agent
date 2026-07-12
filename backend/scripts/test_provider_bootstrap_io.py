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
    asyncio.run(test_blocking_provider_io_does_not_block_loop())
    test_all_bootstraps_use_provider_io_boundary()
    asyncio.run(test_run_state_publication_is_loop_owned())
    asyncio.run(provider.shutdown_provider_tasks())
    provider.reopen_provider_tasks()
    asyncio.run(provider.shutdown_provider_tasks())
    print("PASS: provider bootstrap I/O boundary, dirty-gap recovery, parity, lifecycle")


if __name__ == "__main__":
    main()
