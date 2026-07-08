"""Regression test for orphaned-live-CLI recovery (fix A).

When a runner WRAPPER dies uncontrolled (crash/OOM) but the provider CLI it
spawned is still alive and writing its session jsonl, restart recovery must
RE-ATTACH (classify `live_no_rehook`, keep the session running) instead of
declaring the run a dead orphan and synthesizing a "runner died" complete.json.

Pins three cases:
  1. wrapper dead + CLI pid alive + jsonl corroborates → live_no_rehook, NO
     synthetic complete.json (the bug: a still-running session was marked
     stopped).
  2. wrapper dead + CLI pid alive but jsonl STALE (recycled-pid hazard) →
     dead_orphan (corroboration rejects the recycled pid).
  3. wrapper dead + CLI pid dead → dead_orphan (unchanged baseline).

Run with:
    cd backend && .venv/bin/python scripts/test_recover_orphaned_live_cli.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-orphan-cli-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from provider import default_provider  # noqa: E402
from provider_claude import _runs_root  # noqa: E402
from provider_codex import CodexProvider  # noqa: E402
from ingestion_versions import CLAUDE_INGESTION_VERSION  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"

_LIVE_PROCS: list[subprocess.Popen] = []


def _spawn_live_pid() -> int:
    """A real, alive pid (a sleeping subprocess) — NEVER os.getpid(), so no
    kill path can ever target the test runner's own group."""
    p = subprocess.Popen(["sleep", "300"])
    _LIVE_PROCS.append(p)
    return p.pid


def _spawn_dead_pid() -> int:
    """A pid that is reaped and therefore not alive."""
    p = subprocess.Popen(["sleep", "300"])
    p.terminate()
    p.wait()
    return p.pid


def _seed_claude_run(
    *, runner_pid: int, cli_pid: int, stale_jsonl: bool,
) -> str:
    """Synthesize a claude run dir shaped like an in-flight run whose wrapper
    is dead. `stale_jsonl` makes the session jsonl fully-ingested + old-mtime
    (no corroboration); otherwise it has fresh un-ingested bytes."""
    run_id = str(uuid.uuid4())
    run_dir = _runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    claude_sid = str(uuid.uuid4())
    jsonl = run_dir / "fake_claude" / f"{claude_sid}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        json.dumps({"type": "assistant", "uuid": str(uuid.uuid4())}) + "\n",
        encoding="utf-8",
    )
    size = jsonl.stat().st_size
    inode = jsonl.stat().st_ino
    # Corroboration: fresh run has un-ingested bytes (processed_byte=0 < size);
    # stale run is fully ingested (processed_byte=size) AND old mtime.
    processed_byte = size if stale_jsonl else 0
    if stale_jsonl:
        old = time.time() - 3600
        os.utime(jsonl, (old, old))

    (run_dir / "state.json").write_text(json.dumps({
        "run_id": run_id,
        "mode": "native",
        "runner_pid": runner_pid,
        "session_id": claude_sid,
        "jsonl_path": str(jsonl),
        "cli_pid": cli_pid,
        "complete": False,
    }), encoding="utf-8")
    (run_dir / "backend_state.json").write_text(json.dumps({
        "run_id": run_id,
        "app_session_id": str(uuid.uuid4()),
        "mode": "native",
        "runner_pid": runner_pid,
        "session_id": claude_sid,
        "jsonl_path": str(jsonl),
        "jsonl_inode": inode,
        "processed_byte": processed_byte,
        "cancelled": False,
        "ingestion_version": CLAUDE_INGESTION_VERSION,
    }), encoding="utf-8")
    (run_dir / "pid").write_text(str(runner_pid), encoding="utf-8")
    return run_id


def _descriptor_for(run_id: str) -> dict | None:
    recovered = default_provider().recover_in_flight(run_id_filter={run_id})
    return next((d for d in recovered if d.get("run_id") == run_id), None)


def test_orphaned_live_cli_reattaches() -> bool:
    run_id = _seed_claude_run(
        runner_pid=_spawn_dead_pid(), cli_pid=_spawn_live_pid(), stale_jsonl=False,
    )
    desc = _descriptor_for(run_id)
    if desc is None:
        print("  no descriptor returned")
        return False
    if desc.get("recovered_as") != "live_no_rehook":
        print(f"  expected live_no_rehook, got {desc.get('recovered_as')!r}")
        return False
    if not desc.get("orphaned_cli"):
        print("  orphaned_cli flag not set")
        return False
    if (_runs_root() / run_id / "complete.json").exists():
        print("  synthetic complete.json written for a still-running CLI")
        return False
    return True


def test_reused_pid_not_reattached() -> bool:
    # Live pid but a stale jsonl: corroboration must reject it (a recycled pid
    # is an unrelated process that never touched this session file).
    run_id = _seed_claude_run(
        runner_pid=_spawn_dead_pid(), cli_pid=_spawn_live_pid(), stale_jsonl=True,
    )
    desc = _descriptor_for(run_id)
    if desc is None:
        print("  no descriptor returned")
        return False
    if desc.get("recovered_as") != "dead_orphan":
        print(f"  expected dead_orphan (reuse defense), got {desc.get('recovered_as')!r}")
        return False
    if desc.get("orphaned_cli"):
        print("  orphaned_cli set despite stale jsonl (reuse not defended)")
        return False
    if not (_runs_root() / run_id / "complete.json").exists():
        print("  dead_orphan must synthesize complete.json")
        return False
    return True


def test_inode_mismatch_not_reattached() -> bool:
    # Live pid + fresh jsonl, but the recorded inode no longer matches (the
    # session file was rotated/replaced): corroboration must reject it.
    run_id = _seed_claude_run(
        runner_pid=_spawn_dead_pid(), cli_pid=_spawn_live_pid(), stale_jsonl=False,
    )
    bs_path = _runs_root() / run_id / "backend_state.json"
    bs = json.loads(bs_path.read_text())
    bs["jsonl_inode"] = int(bs["jsonl_inode"]) + 999999  # force a mismatch
    bs_path.write_text(json.dumps(bs), encoding="utf-8")
    desc = _descriptor_for(run_id)
    if desc is None:
        print("  no descriptor returned")
        return False
    if desc.get("recovered_as") != "dead_orphan":
        print(f"  expected dead_orphan (inode mismatch), got {desc.get('recovered_as')!r}")
        return False
    if desc.get("orphaned_cli"):
        print("  orphaned_cli set despite inode mismatch")
        return False
    return True


def _seed_codex_run(*, runner_pid: int, cli_pid: int) -> str:
    """Codex run dir whose wrapper is dead but whose CLI (app-server) is alive
    and whose codex-owned rollout jsonl has un-ingested bytes."""
    run_id = str(uuid.uuid4())
    run_dir = _runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    codex_sid = str(uuid.uuid4())
    rollout = run_dir / "codex-rollout.jsonl"
    rollout.write_text(
        json.dumps({"type": "response_item", "payload": {"type": "message"}}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "state.json").write_text(json.dumps({
        "run_id": run_id,
        "mode": "native",
        "runner_pid": runner_pid,
        "session_id": codex_sid,
        "jsonl_path": str(rollout),
        "rollout_path": str(rollout),
        "cli_pid": cli_pid,
        "complete": False,
    }), encoding="utf-8")
    (run_dir / "backend_state.json").write_text(json.dumps({
        "run_id": run_id,
        "app_session_id": str(uuid.uuid4()),
        "mode": "native",
        "runner_pid": runner_pid,
        "session_id": codex_sid,
        "jsonl_path": str(rollout),
        "processed_byte_offset": 0,  # < rollout size → un-ingested bytes
        "processed_line": 0,
        "cancelled": False,
        "provider_id": "codex-test",
    }), encoding="utf-8")
    (run_dir / "pid").write_text(str(runner_pid), encoding="utf-8")
    return run_id


def test_codex_orphaned_live_cli_reattaches() -> bool:
    run_id = _seed_codex_run(runner_pid=_spawn_dead_pid(), cli_pid=_spawn_live_pid())
    recovered = CodexProvider({"id": "codex-test"}).recover_in_flight(
        run_id_filter={run_id},
    )
    desc = next((d for d in recovered if d.get("run_id") == run_id), None)
    if desc is None:
        print("  no descriptor returned")
        return False
    if desc.get("recovered_as") != "live_orphan":
        print(f"  expected live_orphan, got {desc.get('recovered_as')!r}")
        return False
    if not desc.get("orphaned_cli"):
        print("  orphaned_cli flag not set")
        return False
    if (_runs_root() / run_id / "complete.json").exists():
        print("  synthetic complete.json written for a still-running codex CLI")
        return False
    return True


def test_both_dead_is_dead_orphan() -> bool:
    run_id = _seed_claude_run(
        runner_pid=_spawn_dead_pid(), cli_pid=_spawn_dead_pid(), stale_jsonl=False,
    )
    desc = _descriptor_for(run_id)
    if desc is None:
        print("  no descriptor returned")
        return False
    if desc.get("recovered_as") != "dead_orphan":
        print(f"  expected dead_orphan, got {desc.get('recovered_as')!r}")
        return False
    return True


TESTS = [
    ("claude orphaned live CLI re-attaches (not dead_orphan)", test_orphaned_live_cli_reattaches),
    ("recycled pid + stale jsonl → dead_orphan", test_reused_pid_not_reattached),
    ("inode mismatch → dead_orphan", test_inode_mismatch_not_reattached),
    ("codex orphaned live CLI re-attaches (not dead_orphan)", test_codex_orphaned_live_cli_reattaches),
    ("wrapper + CLI both dead → dead_orphan", test_both_dead_is_dead_orphan),
]


def main_run() -> int:
    failed = 0
    try:
        for name, fn in TESTS:
            try:
                ok = fn()
            except Exception as e:
                ok = False
                import traceback
                traceback.print_exc()
                print(f"  exception: {e}")
            print(f"{PASS if ok else FAIL}  {name}")
            if not ok:
                failed += 1
    finally:
        for p in _LIVE_PROCS:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    print()
    print(f"all {len(TESTS)} tests passed" if not failed
          else f"{failed} of {len(TESTS)} test(s) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_run())
