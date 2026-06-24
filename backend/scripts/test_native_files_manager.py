"""Locks NativeFilesManager: tail targets + demand both arrive only as
bus facts, and the manager reconciles OwnedClaudeJsonlTailers (open when
demanded, close when demand drops). Uses a fake tailer so no real file
IO / asyncio tail loops run.

Run: python backend/scripts/test_native_files_manager.py
"""

import os
import sys
import tempfile

import _test_home
_test_home.isolate("nfm-test-")
# Provider-agnostic resolver globs the claude projects dir for an existing
# <sid>.jsonl, so point it at a temp config dir and create the file below.
_CLAUDE_CFG = tempfile.mkdtemp(prefix="nfm-claude-")
os.environ["CLAUDE_CONFIG_DIR"] = _CLAUDE_CFG
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # noqa: E402

import jsonl_tailer  # noqa: E402
from event_bus import BusEvent, bus  # noqa: E402
import native_files_manager as nfm_mod  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


class FakeTailer:
    instances: list = []

    def __init__(self, **kw):
        self.kw = kw
        self.app_session_id = kw.get("app_session_id")
        self.agent_sid = kw.get("agent_sid")
        self.acquired = 0
        self.released = 0
        FakeTailer.instances.append(self)

    def acquire(self):
        self.acquired += 1

    def release(self):
        self.released += 1
        return None

    @property
    def alive(self):
        return self.acquired > self.released


def _patch():
    jsonl_tailer.OwnedClaudeJsonlTailer = FakeTailer


async def _demand(nfm, owning, token, present):
    await bus.publish(BusEvent(
        type="native_files.demand",
        root_id=session_manager._root_id_for(owning) or "",
        sid=owning,
        payload={"owning_session": owning, "token": token, "present": present},
        persist=False,
    ))


def _live_keys(nfm):
    return {k for k, t in nfm._tailers.items() if t.alive}


async def main():
    _patch()
    nfm = nfm_mod.NativeFilesManager()
    nfm.bind()

    # A session with a known primary sid + a worker-fork panel.
    sess = session_manager.create(name="t", cwd="/tmp/proj", orchestration_mode="manager")
    sid = sess["id"]
    # Create the on-disk claude jsonl the resolver globs for.
    proj = os.path.join(_CLAUDE_CFG, "projects", "-tmp-proj")
    os.makedirs(proj, exist_ok=True)
    open(os.path.join(proj, "PRIMARY-SID.jsonl"), "w").close()
    session_manager.set_agent_sid(sid, "manager", "PRIMARY-SID")
    # Add a fork panel onto the LIVE session record (what a delegation
    # turn would persist) so cold-seed picks it up. Include
    # `fork_agent_session_id` and write a prep-skip cursor on that fork BC
    # session — this is the delegation's prep-skip target.
    fork_bc = session_manager.create(name="fbc", cwd="/tmp/proj", orchestration_mode="manager")
    fork_bc_id = fork_bc["id"]
    PREP_SKIP_LINES = 17
    session_manager.advance_processed_lines(
        fork_bc_id, "FORK-SID", PREP_SKIP_LINES, bump_updated_at=False,
    )
    rid0 = session_manager._root_id_for(sid)
    session_manager._roots[rid0].setdefault("messages", []).append({
        "role": "assistant",
        "workers": [{
            "fork_agent_sid": "FORK-SID",
            "fork_agent_session_id": fork_bc_id,
            "jsonl_path": "/tmp/proj/FORK-SID.jsonl",
        }],
    })

    # 1. No demand → nothing tailed.
    await asyncio.sleep(0)
    assert not _live_keys(nfm), "no demand should mean no tailers"

    # 2. Demand from one subscriber → primary + fork tailers open (seeded
    #    from the store on first demand).
    await _demand(nfm, sid, token="tokA", present=True)
    rid = session_manager._root_id_for(sid)
    live = _live_keys(nfm)
    assert (rid, "PRIMARY-SID") in live, f"primary not tailed: {live}"
    assert (rid, "FORK-SID") in live, f"fork not tailed: {live}"
    assert nfm.is_tailing_root(rid) is True

    # 2b. REGRESSION: cold-seed must read the prep-skip cursor off the
    #     FORK Better Agent session record (not the parent's). Without the fix, the
    #     fork tailer opens at offset 0 and re-emits the parent-inherited
    #     prep lines.
    fork_tailer = next(t for t in FakeTailer.instances
                       if t.kw.get("agent_sid") == "FORK-SID")
    assert fork_tailer.kw["start_offset"] == PREP_SKIP_LINES, (
        f"fork opened at offset {fork_tailer.kw['start_offset']}, "
        f"expected {PREP_SKIP_LINES} (prep-skip cursor on fork BC record)"
    )

    # 3. Second subscriber on same session → no duplicate tailers.
    n_before = len(live)
    await _demand(nfm, sid, token="tokB", present=True)
    assert len(_live_keys(nfm)) == n_before, "duplicate demand spawned tailers"

    # 4. One subscriber leaves → still demanded by the other → still open.
    await _demand(nfm, sid, token="tokA", present=False)
    assert nfm.is_tailing_root(rid) is True, "closed while still demanded"

    # 5. Last subscriber leaves → all tailers close.
    await _demand(nfm, sid, token="tokB", present=False)
    assert not _live_keys(nfm), "tailers not closed after last demand dropped"
    assert nfm.is_tailing_root(rid) is False

    # 6. Mid-session live discovery: re-demand, then a NEW fork arrives via
    #    `native_files.fork_target` AFTER demand is already present.
    #    Mirrors the delegation source order: prep-skip cursor is written
    #    on the fork Better Agent session FIRST, then the fork_target event fires.
    #    The opened tailer must start at the post-prep-skip offset.
    await _demand(nfm, sid, token="tokA", present=True)
    fork2_bc = session_manager.create(name="fbc2", cwd="/tmp/proj", orchestration_mode="manager")
    fork2_bc_id = fork2_bc["id"]
    PREP_SKIP_FORK2 = 9
    session_manager.advance_processed_lines(
        fork2_bc_id, "FORK2-SID", PREP_SKIP_FORK2, bump_updated_at=False,
    )
    await bus.publish(BusEvent(
        type="native_files.fork_target",
        root_id=rid,
        sid=sid,
        payload={
            "parent_app_session_id": sid,
            "fork_agent_sid": "FORK2-SID",
            "fork_agent_session_id": fork2_bc_id,
            "jsonl_path": "/tmp/proj/FORK2-SID.jsonl",
        },
        persist=False,
    ))
    assert (rid, "FORK2-SID") in _live_keys(nfm), "live fork target not tailed"
    fork2_tailer = next(t for t in FakeTailer.instances
                        if t.kw.get("agent_sid") == "FORK2-SID")
    assert fork2_tailer.kw["start_offset"] == PREP_SKIP_FORK2, (
        f"live fork opened at offset {fork2_tailer.kw['start_offset']}, "
        f"expected {PREP_SKIP_FORK2}"
    )

    # 7. ws_callback=None sweep (token=None present=False) drops ALL demand.
    await _demand(nfm, sid, token=None, present=False)
    assert not _live_keys(nfm), "token=None sweep did not drop all demand"

    # 8. REGRESSION (file lags agent_sid_set): the primary sid is announced
    #    before its native jsonl is flushed. The glob-based resolver misses,
    #    but the run state.json (written at spawn) carries the path. The
    #    primary MUST still be tailed — not silently dropped.
    from runs_dir import runs_root
    sess2 = session_manager.create(name="t2", cwd="/tmp/proj2", orchestration_mode="manager")
    sid2 = sess2["id"]
    lag_jsonl = "/tmp/proj2/LAGGING-SID.jsonl"  # deliberately NOT created
    run_dir = runs_root() / "run-lagging"
    run_dir.mkdir(parents=True, exist_ok=True)
    import json as _json
    (run_dir / "state.json").write_text(
        _json.dumps({"session_id": "LAGGING-SID", "jsonl_path": lag_jsonl}),
        encoding="utf-8",
    )
    assert not os.path.exists(lag_jsonl), "test setup: jsonl must not exist"
    session_manager.set_agent_sid(sid2, "manager", "LAGGING-SID")
    await _demand(nfm, sid2, token="tokC", present=True)
    rid2 = session_manager._root_id_for(sid2)
    assert (rid2, "LAGGING-SID") in _live_keys(nfm), (
        "primary dropped when jsonl file lags agent_sid_set (BLOCKER regression)"
    )
    # the tailer must have been handed the path the runner recorded.
    opened = next(t for t in FakeTailer.instances
                  if t.kw.get("agent_sid") == "LAGGING-SID")
    assert str(opened.kw["jsonl_path"]) == lag_jsonl, opened.kw["jsonl_path"]

    print("PASS test_native_files_manager")


async def test_local_run_state_skips_expensive_jsonl_scan() -> None:
    from runs_dir import runs_root
    from orchs import jsonl_helpers

    sess = session_manager.create(name="state-first", cwd="/tmp/state-first", orchestration_mode="manager")
    sid = sess["id"]
    agent_sid = "STATE-FIRST-SID"
    lag_jsonl = "/tmp/state-first/STATE-FIRST-SID.jsonl"
    run_dir = runs_root() / "run-state-first"
    run_dir.mkdir(parents=True, exist_ok=True)
    import json as _json
    (run_dir / "state.json").write_text(
        _json.dumps({"session_id": agent_sid, "jsonl_path": lag_jsonl}),
        encoding="utf-8",
    )
    original_compute = jsonl_helpers.compute_jsonl_read_path

    def fail_compute(*_args, **_kwargs):
        raise AssertionError("run-state path should avoid expensive jsonl scan")

    jsonl_helpers.compute_jsonl_read_path = fail_compute
    try:
        path = nfm_mod._resolve_primary_jsonl(sess, agent_sid)
    finally:
        jsonl_helpers.compute_jsonl_read_path = original_compute
    assert str(path) == lag_jsonl, path
    print("PASS test_local_run_state_skips_expensive_jsonl_scan")


async def test_run_state_lookup_is_targeted_and_cached() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    root = runs_root()
    for name, sid in (("run-index-a", "INDEX-A-SID"), ("run-index-b", "INDEX-B-SID")):
        run_dir = root / name
        run_dir.mkdir(parents=True, exist_ok=True)
        import json as _json
        (run_dir / "state.json").write_text(
            _json.dumps({"session_id": sid, "jsonl_path": f"/tmp/{sid}.jsonl"}),
            encoding="utf-8",
        )
    assert str(nfm_mod._scan_run_state_for_jsonl("INDEX-A-SID")) == "/tmp/INDEX-A-SID.jsonl"
    original_state_files = nfm_mod._state_files_for_sid

    def fail_state_files(*_args, **_kwargs):
        raise AssertionError("cached run-state lookup should avoid rescanning runs_root")

    nfm_mod._state_files_for_sid = fail_state_files  # type: ignore
    try:
        assert str(nfm_mod._scan_run_state_for_jsonl("INDEX-A-SID")) == "/tmp/INDEX-A-SID.jsonl"
    finally:
        nfm_mod._state_files_for_sid = original_state_files  # type: ignore
    print("PASS test_run_state_lookup_is_targeted_and_cached")


async def test_run_state_lookup_checks_recent_dirs_before_rg() -> None:
    from runs_dir import runs_root

    nfm_mod._RUN_STATE_LOOKUP_CACHE.clear()
    root = runs_root()
    agent_sid = "RECENT-FIRST-SID"
    for i in range(nfm_mod._RUN_STATE_RECENT_SCAN_LIMIT + 10):
        run_dir = root / f"old-run-{i}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "state.json").write_text(
            '{"session_id":"old","jsonl_path":"/tmp/old.jsonl"}',
            encoding="utf-8",
        )
    run_dir = root / "recent-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(
        '{"session_id":"RECENT-FIRST-SID","jsonl_path":"/tmp/recent-first.jsonl"}',
        encoding="utf-8",
    )
    original_run = nfm_mod.subprocess.run

    def fail_rg(*_args, **_kwargs):
        raise AssertionError("recent run-state lookup should avoid rg")

    nfm_mod.subprocess.run = fail_rg  # type: ignore
    try:
        path = nfm_mod._scan_run_state_for_jsonl(agent_sid)
    finally:
        nfm_mod.subprocess.run = original_run  # type: ignore
    assert str(path) == "/tmp/recent-first.jsonl", path
    print("PASS test_run_state_lookup_checks_recent_dirs_before_rg")


async def test_persisted_native_path_skips_run_state_lookup() -> None:
    _patch()
    nfm = nfm_mod.NativeFilesManager()
    sess = session_manager.create(name="persisted", cwd="/tmp/persisted", orchestration_mode="manager")
    sid = sess["id"]
    target = nfm_mod._Target(
        owning=sid,
        agent_sid="PERSISTED-SID",
        jsonl_path=nfm_mod.Path("/tmp/persisted.jsonl"),
        start_offset=12,
        can_tail=True,
    )
    nfm._append_native_path_target(sid, target)
    original_scan = nfm_mod._scan_run_state_for_jsonl
    original_compute = nfm_mod._resolve_primary_jsonl

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("persisted native_paths should avoid run-state lookup")

    nfm_mod._scan_run_state_for_jsonl = fail_scan  # type: ignore
    nfm_mod._resolve_primary_jsonl = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("persisted native_paths should avoid jsonl resolution")
    )  # type: ignore
    try:
        resolved = await nfm._resolve_primary_target(sid, sess, "PERSISTED-SID")
    finally:
        nfm_mod._scan_run_state_for_jsonl = original_scan  # type: ignore
        nfm_mod._resolve_primary_jsonl = original_compute  # type: ignore
    assert resolved is not None
    assert str(resolved.jsonl_path) == "/tmp/persisted.jsonl"
    assert resolved.start_offset == 12
    print("PASS test_persisted_native_path_skips_run_state_lookup")


async def test_codex_primary_not_tailed_by_claude_tailer() -> None:
    """A Codex rollout must NOT be handed to the claude-shaped
    OwnedClaudeJsonlTailer — it can't normalize raw Codex lines and would
    forward them verbatim as agent_message noise. Codex is covered by its
    run-scoped CodexRolloutTailer + recovery, so the claude backup is
    skipped. Regression for the 'unknown event: agent_message.*' garbage."""
    from pathlib import Path
    import native_files_manager as nfm

    rollout = Path.home() / ".codex" / "sessions" / "2026" / "06" / "16" / "rollout-X.jsonl"
    assert nfm._is_codex_rollout(rollout), rollout
    assert not nfm._is_codex_rollout(Path("/tmp/proj/SID.jsonl"))
    # Name-only detection when not under ~/.codex (e.g. custom CODEX_HOME).
    assert nfm._is_codex_rollout(Path("/custom/home/rollout-abc.jsonl"))

    # Codex rollout paths are tracked for native-path provenance, but
    # marked `can_tail=False` so they are not handed to the Claude-shaped
    # OwnedClaudeJsonlTailer.
    orig_scan = nfm._scan_run_state_for_jsonl
    orig_local = nfm._is_local_session
    nfm._scan_run_state_for_jsonl = lambda sid: rollout  # type: ignore
    nfm._is_local_session = lambda sess: True  # type: ignore
    try:
        resolved = nfm._resolve_primary_jsonl({"id": "s", "cwd": "/repo"}, "CODEX-SID")
        target = await nfm.NativeFilesManager()._resolve_primary_target(
            "s", {"id": "s", "cwd": "/repo"}, "CODEX-SID",
        )
    finally:
        nfm._scan_run_state_for_jsonl = orig_scan  # type: ignore
        nfm._is_local_session = orig_local  # type: ignore
    assert resolved == rollout, f"codex rollout was not recorded: {resolved}"
    assert target is not None, "codex rollout target was not recorded"
    assert target.can_tail is False, "codex rollout leaked to claude tailer"
    print("PASS test_codex_primary_not_tailed_by_claude_tailer")


async def test_demand_seed_does_not_block_event_loop() -> None:
    _patch()
    nfm = nfm_mod.NativeFilesManager()
    nfm.bind()
    sess = session_manager.create(name="slow", cwd="/tmp/slow", orchestration_mode="manager")
    sid = sess["id"]
    original_get = nfm_mod.session_manager.get

    def slow_get(*args, **kwargs):
        import time
        time.sleep(0.2)
        return original_get(*args, **kwargs)

    nfm_mod.session_manager.get = slow_get
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        deadline = asyncio.get_running_loop().time() + 0.12
        while asyncio.get_running_loop().time() < deadline:
            ticks += 1
            await asyncio.sleep(0.01)

    try:
        await asyncio.gather(
            _demand(nfm, sid, token="slow-token", present=True),
            heartbeat(),
        )
    finally:
        nfm_mod.session_manager.get = original_get
    assert ticks >= 5, f"native_files demand blocked event loop, ticks={ticks}"
    print("PASS test_demand_seed_does_not_block_event_loop")


async def test_agent_sid_session_read_does_not_block_event_loop() -> None:
    _patch()
    nfm = nfm_mod.NativeFilesManager()
    nfm.bind()
    sess = session_manager.create(
        name="agent-sid-slow-read",
        cwd="/tmp/agent-sid-slow-read",
        orchestration_mode="manager",
    )
    sid = sess["id"]
    root_id = session_manager._root_id_for(sid) or sid
    agent_sid = "AGENT-SID-SLOW-READ"
    proj = os.path.join(_CLAUDE_CFG, "projects", "-tmp-agent-sid-slow-read")
    os.makedirs(proj, exist_ok=True)
    open(os.path.join(proj, f"{agent_sid}.jsonl"), "w").close()

    original_get_lite = nfm_mod.session_manager.get_lite

    def slow_get_lite(*args, **kwargs):
        import time
        time.sleep(0.2)
        return original_get_lite(*args, **kwargs)

    nfm_mod.session_manager.get_lite = slow_get_lite
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        deadline = asyncio.get_running_loop().time() + 0.12
        while asyncio.get_running_loop().time() < deadline:
            ticks += 1
            await asyncio.sleep(0.01)

    try:
        await asyncio.gather(
            nfm._on_agent_sid(BusEvent(
                type="session.agent_sid_set",
                root_id=root_id,
                sid=sid,
                payload={"agent_sid": agent_sid},
                persist=False,
            )),
            heartbeat(),
        )
    finally:
        nfm_mod.session_manager.get_lite = original_get_lite
    assert ticks >= 5, f"native_files agent_sid read blocked event loop, ticks={ticks}"
    print("PASS test_agent_sid_session_read_does_not_block_event_loop")


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(test_local_run_state_skips_expensive_jsonl_scan())
    asyncio.run(test_run_state_lookup_is_targeted_and_cached())
    asyncio.run(test_run_state_lookup_checks_recent_dirs_before_rg())
    asyncio.run(test_persisted_native_path_skips_run_state_lookup())
    asyncio.run(test_codex_primary_not_tailed_by_claude_tailer())
    asyncio.run(test_demand_seed_does_not_block_event_loop())
    asyncio.run(test_agent_sid_session_read_does_not_block_event_loop())
