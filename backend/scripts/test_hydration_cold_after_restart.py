"""Repro for cold=1 full rebuilds despite an existing warm SQLite index.

Root cause: `event_chain.json` (written by
`EventIngester._persist_chain_head_locked`) and the SQLite hydration-index
writer projection (`hydration_index_store.flush_writer_projection`) are
only durably updated periodically -- on the background-flusher's
notify-driven cadence, or an explicit close/fsync. Every write is
immediately visible on disk (`_ingest_impl` calls `fh.flush()` per
write), but there is always a window where bytes already sit in
events.jsonl *ahead of* what event_chain.json / the SQLite index know
about (e.g. a write burst -- crash-recovery replay, a fast turn --
outpacing the flusher). If the backend process restarts inside that
window, a handful of trailing events exist on disk that neither
durability artifact has caught up to.

On restart, `hydration_index_store._growth_is_authoritative` must prove
the new process is looking at a gap-free append of the exact
previously-indexed content:
  - the in-memory shortcut (`_append_receipts`) can't help -- it's
    process-local and starts empty;
  - the durable shortcut requires event_chain.json's `identity` (dev,
    ino, ctime_ns, mtime_ns, size) to equal the file's CURRENT stat
    exactly -- but event_chain.json's last durable write, from before
    the gap, only describes the file as of that last flush.

Both checks fail, `_valid_append` returns False, and `load()` is forced
into `_publish_cold`: a full rescan of the entire journal from offset 0,
discarding the whole existing SQLite index -- even though only the few
events written since the last flush are actually unverified.

Uses the real write path (event_ingester.ingest) and the real read path
(hydration_index_store.load); no mocks, no hand-written jsonl. Each
phase runs as its own subprocess so module-level in-memory state
(`_append_receipts`, event_ingester's per-root caches, etc.) is
genuinely fresh, exactly as it would be after restarting the backend.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = "root"


def _append(value: int) -> None:
    from event_ingester import event_ingester
    event_ingester.ingest(
        ROOT, ROOT, "agent_message",
        {"uuid": f"event-{value}", "message": {"role": "assistant", "content": []}},
        source="hydration-cold-restart-test", msg_id="message",
    )


def _phase_build_with_gap() -> None:
    """Build+warm the SQLite index, checkpoint at 500 events, then append
    5 more with the background flusher disabled so they're durably on
    disk but not yet reflected in event_chain.json / the SQLite meta."""
    from event_ingester import event_ingester
    import hydration_index_store

    for value in range(1, 501):
        _append(value)
    journal = event_ingester._events_path(ROOT)
    hydration_index_store.load(ROOT, journal)
    event_ingester.close_all()

    # Deterministically reproduce writes outpacing the periodic
    # chain-meta/SQLite-projection flush, instead of racing a real clock.
    event_ingester._fsync_stop.set()

    for value in range(501, 506):
        _append(value)
    os._exit(0)  # skip close_all/atexit -- the gap must remain unflushed


def _phase_restart_append_and_load() -> None:
    """Fresh interpreter = "the backend after restart": append one more
    event (the first write since restart), then load() -- exactly what
    happens when the restarted backend serves its first UI request."""
    from event_ingester import event_ingester
    import hydration_index_store

    _append(506)
    journal = event_ingester._events_path(ROOT)
    _, metrics = hydration_index_store.load(ROOT, journal)
    event_ingester.close_all()
    print(json.dumps(metrics))


def _run_harness() -> int:
    home = tempfile.mkdtemp(prefix="ba-hydration-cold-restart-")
    try:
        built = subprocess.run(
            [sys.executable, __file__, "--build-with-gap", home],
            check=False, capture_output=True, text=True,
        )
        assert built.returncode == 0, (built.returncode, built.stdout, built.stderr)

        restarted = subprocess.run(
            [sys.executable, __file__, "--restart-append-and-load", home],
            check=False, capture_output=True, text=True,
        )
        assert restarted.returncode == 0, (restarted.returncode, restarted.stdout, restarted.stderr)
        metrics = json.loads(restarted.stdout.strip().splitlines()[-1])

        journal_size = (Path(home) / "sessions" / ROOT / "events.jsonl").stat().st_size

        print("POST-RESTART LOAD METRICS:", metrics)
        assert metrics["cold"] == 0, (
            "hydration_index_store.load() forced a full cold rebuild after "
            "restart despite a warm, valid, persistent SQLite index; only "
            f"6 events (of 506) were unindexed at restart. metrics={metrics}"
        )
        assert metrics["scanned_bytes"] < journal_size, (
            "expected only the post-restart tail to be scanned, not the "
            f"whole {journal_size}-byte journal; metrics={metrics}"
        )
        print("PASS: hydration index stays warm across a backend restart")
        return 0
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] in {"--build-with-gap", "--restart-append-and-load"}:
        os.environ["BETTER_AGENT_HOME"] = sys.argv[2]
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        if sys.argv[1] == "--build-with-gap":
            _phase_build_with_gap()
        else:
            _phase_restart_append_and_load()
        raise SystemExit(0)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(_run_harness())
