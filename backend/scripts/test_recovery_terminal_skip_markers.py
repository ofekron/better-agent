import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _test_home  # noqa: E402
_test_home.isolate("bc_test_terminal_skip_")

import run_recovery  # noqa: E402
from ingestion_versions import current_ingestion_version  # noqa: E402
from runs_dir import runs_root  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool) -> None:
    print(("  PASS" if ok else "  FAIL") + f": {name}")
    if not ok:
        failures.append(name)


class _FakeSessionManager:
    def __init__(self, sess: dict) -> None:
        self.sess = sess

    def get(self, sid: str):
        return self.sess

    def _root_id_for(self, sid: str):
        return sid


def _marker(run_id: str) -> Path:
    return runs_root() / run_id / "reconciled.marker"


def _integrate(desc: dict, sess: dict) -> None:
    original_sm = run_recovery.session_manager
    try:
        run_recovery.session_manager = _FakeSessionManager(sess)
        asyncio.run(
            run_recovery._integrate_one_locked(
                None,
                None,
                desc,
                summary=None,
                recovery_root_id=None,
                root_lease=None,
            )
        )
    finally:
        run_recovery.session_manager = original_sm


def test_missing_target_message_id_marks_reconciled() -> None:
    print("T1 dead+complete run with no attachable message gets marker")
    run_id = "run-missing-target"
    (runs_root() / run_id).mkdir(parents=True, exist_ok=True)
    desc = {
        "run_id": run_id,
        "app_session_id": "sid-1",
        "has_complete_json": True,
        "alive": False,
        "cancelled": False,
        "ingestion_version": current_ingestion_version("claude"),
        "provider_kind": "claude",
    }
    sess = {"messages": []}
    _integrate(desc, sess)
    check("reconciled.marker written", _marker(run_id).exists())


def test_version_stale_native_missing_tombstones() -> None:
    print("T2 version-stale run with native source gone gets tombstoned")
    run_id = "run-version-stale"
    (runs_root() / run_id).mkdir(parents=True, exist_ok=True)
    desc = {
        "run_id": run_id,
        "app_session_id": "sid-2",
        "has_complete_json": True,
        "alive": False,
        "cancelled": False,
        "ingestion_version": 0,
        "jsonl_path": str(runs_root() / "does-not-exist.jsonl"),
        "target_message_id": "msg-1",
        "provider_kind": "claude",
    }
    sess = {"messages": [{"id": "msg-1", "role": "assistant", "events": []}]}
    _integrate(desc, sess)
    check("tombstone marker written", _marker(run_id).exists())


def test_live_run_without_target_is_not_marked() -> None:
    print("T3 live in-flight run without target stays eligible")
    run_id = "run-live-no-target"
    (runs_root() / run_id).mkdir(parents=True, exist_ok=True)
    desc = {
        "run_id": run_id,
        "app_session_id": "sid-3",
        "has_complete_json": False,
        "alive": True,
        "cancelled": False,
        "ingestion_version": current_ingestion_version("claude"),
        "provider_kind": "claude",
    }
    sess = {"messages": []}
    try:
        _integrate(desc, sess)
    except Exception:
        # A live run proceeds past the skip guards into reattach paths
        # that need real infrastructure; the assertion below only cares
        # that no terminal marker was written on the way.
        pass
    check("live run not marked", not _marker(run_id).exists())


def test_live_version_stale_run_is_not_tombstoned() -> None:
    print("T3b live version-stale run with native source gone stays eligible")
    run_id = "run-live-version-stale"
    (runs_root() / run_id).mkdir(parents=True, exist_ok=True)
    desc = {
        "run_id": run_id,
        "app_session_id": "sid-3b",
        "has_complete_json": False,
        "alive": True,
        "cancelled": False,
        "ingestion_version": 0,
        "jsonl_path": str(runs_root() / "does-not-exist-live.jsonl"),
        "target_message_id": "msg-1",
        "provider_kind": "claude",
    }
    sess = {"messages": [{"id": "msg-1", "role": "assistant", "events": []}]}
    _integrate(desc, sess)
    check("live version-stale run not marked", not _marker(run_id).exists())


def test_batch_runs_by_session_keeps_sessions_whole() -> None:
    print("T4 cold-run batching never splits a session across batches")
    recovered = (
        [{"run_id": f"a{i}", "app_session_id": "sess-a"} for i in range(5)]
        + [{"run_id": f"b{i}", "app_session_id": "sess-b"} for i in range(5)]
        + [{"run_id": f"c{i}", "app_session_id": "sess-c"} for i in range(12)]
    )
    batches = run_recovery.batch_runs_by_session(recovered, 8)
    seen: dict[str, int] = {}
    for index, batch in enumerate(batches):
        for desc in batch:
            sid = desc["app_session_id"]
            if sid in seen:
                check(
                    f"session {sid} stays in one batch",
                    seen[sid] == index,
                )
            seen[sid] = index
    flat = [d["run_id"] for b in batches for d in b]
    check("all runs batched exactly once", sorted(flat) == sorted(
        d["run_id"] for d in recovered
    ))
    check(
        "oversized session gets its own batch",
        any(len(b) == 12 and all(d["app_session_id"] == "sess-c" for d in b)
            for b in batches),
    )
    check(
        "small sessions pack within batch_max",
        all(len(b) <= 8 or all(d["app_session_id"] == "sess-c" for d in b)
            for b in batches),
    )


if __name__ == "__main__":
    test_missing_target_message_id_marks_reconciled()
    test_version_stale_native_missing_tombstones()
    test_live_run_without_target_is_not_marked()
    test_live_version_stale_run_is_not_tombstoned()
    test_batch_runs_by_session_keeps_sessions_whole()
    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")
