"""Node-session parity tests.

Locks the node_id threading fixes that make node-hosted sessions
behave like local ones:

  P1  fork_session inherits the parent's node_id; the v10 migration
      stamps legacy forks (never-ran → parent's node, ran → primary).
  P3  prompt_engineer eng sessions inherit the parent's node_id and
      route temp-file I/O through the node RPC layer.
  P5  file_ref_resolver skips the local-disk existence check for
      node-hosted sessions (assume_exists).
  P6  primary-side remote run dirs: complete.json synthesis and
      descriptor preparation for node-connect recovery; node-side
      recovery RPC validation (run_id traversal, pe temp confinement,
      raw-range bounds).
  P7  the offline-node submit gate rejects upfront with a clear error.

Run:
    cd backend && .venv/bin/python scripts/test_node_session_parity.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home
_BC_HOME = _test_home.isolate("bc-node-parity-")
# Isolate from the developer's real multi-machine setup — these tests
# assert single-machine semantics (no topology).
os.environ.pop("BETTER_CLAUDE_TOPOLOGY_PATH", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILURES: list[str] = []


def _ok(label: str) -> None:
    print(f"\033[92mPASS\033[0m  {label}")


def _fail(label: str, why: str) -> None:
    FAILURES.append(label)
    print(f"\033[91mFAIL\033[0m  {label}: {why}")


def check(label: str, cond: bool, why: str = "") -> None:
    if cond:
        _ok(label)
    else:
        _fail(label, why or "condition false")


# ── P1: fork_session node_id inheritance ──────────────────────────────


def test_fork_inherits_node_id() -> None:
    import session_store

    root = session_store.create_session(
        name="parent", cwd="/tmp", node_id="node-b",
    )
    root["agent_session_id"] = "sid-parent"
    child = session_store.fork_session(root, root["id"])
    check(
        "fork_session inherits parent node_id",
        child.get("node_id") == "node-b",
        f"got {child.get('node_id')!r}",
    )


def test_v10_migration_stamps_legacy_forks() -> None:
    import session_store

    root = session_store.create_session(
        name="legacy", cwd="/tmp", node_id="node-b",
    )
    root["agent_session_id"] = "sid-parent"
    never_ran = session_store.fork_session(root, root["id"])
    ran = session_store.fork_session(root, root["id"])
    ran["agent_session_id"] = "sid-ran"
    # Simulate pre-v10 disk state: forks lack the node_id key entirely.
    del never_ran["node_id"]
    del ran["node_id"]

    migrated = session_store._migrate_session(root)
    forks = migrated.get("forks") or []
    check(
        "v10: never-ran legacy fork inherits parent node",
        forks[0].get("node_id") == "node-b",
        f"got {forks[0].get('node_id')!r}",
    )
    check(
        "v10: legacy fork with turns stays primary",
        forks[1].get("node_id") == "primary",
        f"got {forks[1].get('node_id')!r}",
    )


# ── P5: file_ref_resolver remote sessions ─────────────────────────────


def test_file_ref_assume_exists() -> None:
    from file_ref_resolver import (
        assume_exists_for_session, rewrite_text,
    )

    missing = "/definitely/not/on/disk/parity_check.py"
    text = f"edited {missing} just now"
    check(
        "rewrite_text leaves missing local file as plain text",
        rewrite_text(text, None) == text,
    )
    rewritten = rewrite_text(text, None, assume_exists=True)
    check(
        "rewrite_text links missing file when assume_exists",
        "bcfile:" in rewritten and missing in rewritten,
        f"got {rewritten!r}",
    )
    check(
        "assume_exists_for_session: primary session → False",
        assume_exists_for_session({"node_id": "primary"}) is False,
    )
    check(
        "assume_exists_for_session: node session → True (no topology)",
        assume_exists_for_session({"node_id": "node-b"}) is True,
    )


# ── P3: prompt_engineer node threading (local single-code path) ───────


def test_prompt_engineer_inherits_node() -> None:
    from session_manager import manager as session_manager
    import prompt_engineer

    proj = tempfile.mkdtemp(prefix="bc-parity-proj-")
    try:
        parent = session_manager.create(
            name="pe-parent", cwd=proj, orchestration_mode="native",
        )
        result = asyncio.run(
            prompt_engineer.start(parent["id"], "draft text", "new")
        )
        eng = result["session"]
        check(
            "eng session inherits parent node_id",
            eng.get("node_id") == (parent.get("node_id") or "primary"),
            f"got {eng.get('node_id')!r}",
        )
        tmp = Path(result["temp_file_path"])
        check(
            "eng temp file written under ba_home/prompt-eng via RPC layer",
            tmp.is_file()
            and tmp.read_text(encoding="utf-8") == "draft text"
            and str(tmp).startswith(os.path.join(_BC_HOME, "prompt-eng")),
            f"path={tmp}",
        )
        content = asyncio.run(prompt_engineer.finalize(eng["id"]))
        check("finalize reads temp via RPC layer", content == "draft text")
        ok = asyncio.run(prompt_engineer.cleanup(eng["id"]))
        check(
            "cleanup removes record + temp dir",
            ok and not tmp.parent.exists(),
        )
    finally:
        shutil.rmtree(proj, ignore_errors=True)


# ── P6: remote run-dir recovery primitives ────────────────────────────


def test_remote_run_dir_finalize_and_prepare() -> None:
    from runs_dir import runs_root
    import provider_remote
    import run_recovery

    rd = runs_root() / "run-parity-1"
    rd.mkdir(parents=True, exist_ok=True)
    provider_remote._finalize_remote_run_dir(
        rd,
        provider_remote._complete_payload("complete", {
            "success": True, "session_id": "sid-x", "token_usage": None,
        }),
        reconciled=True,
    )
    complete = json.loads((rd / "complete.json").read_text(encoding="utf-8"))
    check(
        "finalize writes complete.json + reconciled.marker",
        complete.get("success") is True
        and complete.get("session_id") == "sid-x"
        and (rd / "reconciled.marker").exists(),
    )

    # Vanished-on-node run → synthesized error complete + descriptor.
    rd2 = runs_root() / "run-parity-2"
    rd2.mkdir(parents=True, exist_ok=True)
    bs = {
        "provider_id": "remote:node-b",
        "node_id": "node-b",
        "root_id": "root-x",
        "app_session_id": "app-x",
        "persist_to": "app-x",
        "mode": "native",
        "started_at": "2026-01-01T00:00:00",
    }
    (rd2 / "backend_state.json").write_text(json.dumps(bs), encoding="utf-8")
    desc = asyncio.run(run_recovery._prepare_remote_desc(
        "node-b", rd2, bs, {"exists": False},
    ))
    check(
        "vanished remote run → error complete.json + descriptor",
        desc is not None
        and desc["has_complete_json"] is True
        and desc["app_session_id"] == "app-x"
        and (rd2 / "complete.json").exists()
        and json.loads(
            (rd2 / "complete.json").read_text(encoding="utf-8")
        ).get("success") is False,
        f"desc={desc}",
    )

    # Alive run on a (currently offline) node → rehook attempt, no desc.
    rd3 = runs_root() / "run-parity-3"
    rd3.mkdir(parents=True, exist_ok=True)
    desc3 = asyncio.run(run_recovery._prepare_remote_desc(
        "node-b", rd3, bs,
        {"exists": True, "alive": True, "complete": None},
    ))
    check(
        "alive remote run → rehook path, no descriptor yet",
        desc3 is None,
    )


def test_recovery_rpc_validation() -> None:
    import node_rpc_handlers as rpc

    async def _dispatch(method: str, params: dict):
        return await rpc.dispatch_rpc(method, params)

    # run_id traversal must be rejected, not resolved.
    rejected = False
    try:
        asyncio.run(_dispatch("get_run_status", {"run_ids": ["../escape"]}))
    except ValueError:
        rejected = True
    check("get_run_status rejects traversal run_id", rejected)

    rejected = False
    try:
        asyncio.run(_dispatch(
            "read_run_jsonl", {"run_id": "a/../b", "start_line": 0},
        ))
    except ValueError:
        rejected = True
    check("read_run_jsonl rejects traversal run_id", rejected)

    res = asyncio.run(_dispatch(
        "get_run_status", {"run_ids": ["run-parity-1"]},
    ))
    st = res["runs"]["run-parity-1"]
    check(
        "get_run_status reports completed fixture run",
        st["exists"] is True and st["alive"] is False
        and (st["complete"] or {}).get("session_id") == "sid-x",
        f"st={st}",
    )

    # pe temp RPCs: invalid eng id rejected; roundtrip confined to ba_home.
    rejected = False
    try:
        asyncio.run(_dispatch(
            "pe_temp_write",
            {"eng_session_id": "../../etc", "content": "x"},
        ))
    except ValueError:
        rejected = True
    check("pe_temp_write rejects non-uuid eng id", rejected)

    eng_id = "12345678-1234-1234-1234-1234567890ab"
    written = asyncio.run(_dispatch(
        "pe_temp_write", {"eng_session_id": eng_id, "content": "hello"},
    ))
    check(
        "pe_temp_write confined to ba_home/prompt-eng",
        written["path"].startswith(os.path.join(_BC_HOME, "prompt-eng")),
        f"path={written['path']}",
    )
    read = asyncio.run(_dispatch("pe_temp_read", {"eng_session_id": eng_id}))
    check("pe_temp_read roundtrip", read["content"] == "hello")
    asyncio.run(_dispatch("pe_temp_cleanup", {"eng_session_id": eng_id}))
    read2 = asyncio.run(_dispatch("pe_temp_read", {"eng_session_id": eng_id}))
    check("pe_temp_read after cleanup → in-band None", read2["content"] is None)

    # raw range: oversize length rejected; non-media extension rejected.
    rejected = False
    try:
        asyncio.run(_dispatch(
            "read_file_raw_range",
            {"path": "/tmp/x.mp4", "start": 0, "length": 100 * 1024 * 1024},
        ))
    except ValueError:
        rejected = True
    check("read_file_raw_range rejects oversize chunk", rejected)

    rejected = False
    try:
        asyncio.run(_dispatch(
            "read_file_raw_range",
            {"path": "/etc/passwd", "start": 0, "length": 1024},
        ))
    except Exception:
        rejected = True
    check("read_file_raw_range rejects non-media path", rejected)


# ── P7: offline-node submit gate ──────────────────────────────────────


def test_offline_gate() -> None:
    import main

    check(
        "gate: primary session passes",
        main._node_offline_error({"node_id": "primary"}) is None,
    )
    err = main._node_offline_error({"node_id": "ghost-node"})
    check(
        "gate: disconnected node session rejected with clear error",
        isinstance(err, str) and "ghost-node" in err and "offline" in err,
        f"got {err!r}",
    )


def run_all() -> int:
    test_fork_inherits_node_id()
    test_v10_migration_stamps_legacy_forks()
    test_file_ref_assume_exists()
    test_prompt_engineer_inherits_node()
    test_remote_run_dir_finalize_and_prepare()
    test_recovery_rpc_validation()
    test_offline_gate()
    print()
    if FAILURES:
        print(f"\033[91m{len(FAILURES)} failure(s):\033[0m {FAILURES}")
        return 1
    print("\033[92mall node-session parity tests passed\033[0m")
    return 0


if __name__ == "__main__":
    try:
        rc = run_all()
    finally:
        shutil.rmtree(_BC_HOME, ignore_errors=True)
    sys.exit(rc)
