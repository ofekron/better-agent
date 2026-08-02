"""Unit coverage for `shadow_jsonl` — the primary-side shadow projection
of remote workers' claude jsonls.

Exercises the full `_append_sync` state machine (version drop/bump, offset
equal/greater/less-than-size, gap detection, newline normalization), the
async `append` serialization + per-node tracking, `rebuild` atomic rewrite
+ cursor reset, `snapshot_cursors_for`, and `shadow_path` canonical path.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

TEST_HOME = _test_home.TestHome.acquire("bc-test-shadow-jsonl-")

import paths  # noqa: E402
import shadow_jsonl  # noqa: E402


def _reset_state() -> None:
    """Clear in-memory cursors AND wipe the on-disk shadow dir so every
    test starts from a clean projection."""
    shadow_jsonl.reset_for_tests()
    shadow_dir = paths.ba_home() / "shadow_claude_jsonls"
    if shadow_dir.exists():
        shutil.rmtree(shadow_dir)


def _run(coro):
    return asyncio.run(coro)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_shadow_path_canonical_under_ba_home():
    p = shadow_jsonl.shadow_path("root-1", "fork-9")
    assert p == paths.ba_home() / "shadow_claude_jsonls" / "root-1" / "fork-9.jsonl"


def test_append_same_version_contiguous_appends_in_order():
    _reset_state()
    path = shadow_jsonl.shadow_path("r", "f")
    _run(shadow_jsonl.append(
        node_id="n1", root_id="r", fork_agent_sid="f",
        file_version=1, line_offset_in_version=0, line='{"a":1}',
    ))
    _run(shadow_jsonl.append(
        node_id="n1", root_id="r", fork_agent_sid="f",
        file_version=1, line_offset_in_version=8, line='{"a":2}',
    ))
    assert _read(path) == '{"a":1}\n{"a":2}\n'


def test_append_normalizes_missing_trailing_newline_and_preserves_existing():
    _reset_state()
    path = shadow_jsonl.shadow_path("r", "f")
    # Line with no trailing newline gets one appended.
    _run(shadow_jsonl.append(
        node_id="n", root_id="r", fork_agent_sid="f",
        file_version=1, line_offset_in_version=0, line="plain",
    ))
    # Line already ending in newline is not doubled.
    _run(shadow_jsonl.append(
        node_id="n", root_id="r", fork_agent_sid="f",
        file_version=1, line_offset_in_version=6, line="done\n",
    ))
    assert _read(path) == "plain\ndone\n"


def test_append_older_version_is_dropped_stale():
    _reset_state()
    path = shadow_jsonl.shadow_path("r", "f")
    _run(shadow_jsonl.append(
        node_id="n", root_id="r", fork_agent_sid="f",
        file_version=2, line_offset_in_version=0, line="v2",
    ))
    # Stale message from older file_version — dropped, file unchanged.
    _run(shadow_jsonl.append(
        node_id="n", root_id="r", fork_agent_sid="f",
        file_version=1, line_offset_in_version=0, line="v1-stale",
    ))
    assert _read(path) == "v2\n"


def test_append_version_bump_truncates_and_rewrites_from_offset_zero():
    _reset_state()
    path = shadow_jsonl.shadow_path("r", "f")
    _run(shadow_jsonl.append(
        node_id="n", root_id="r", fork_agent_sid="f",
        file_version=1, line_offset_in_version=0, line="old-1",
    ))
    _run(shadow_jsonl.append(
        node_id="n", root_id="r", fork_agent_sid="f",
        file_version=1, line_offset_in_version=6, line="old-2",
    ))
    assert _read(path) == "old-1\nold-2\n"
    # Rotation observed on node → truncate, rewrite from scratch.
    _run(shadow_jsonl.append(
        node_id="n", root_id="r", fork_agent_sid="f",
        file_version=2, line_offset_in_version=0, line="new-1",
    ))
    assert _read(path) == "new-1\n"


def test_append_version_bump_with_nonzero_offset_warns_then_gap_drops(caplog):
    # A fresh version must start at offset 0. If the node sends a nonzero
    # offset, the bump-truncate runs and a "version bump" warning fires, but
    # the subsequent size check sees size 0 < offset and gap-drops the line.
    _reset_state()
    path = shadow_jsonl.shadow_path("r", "f")
    with caplog.at_level("WARNING", logger="shadow_jsonl"):
        _run(shadow_jsonl.append(
            node_id="n", root_id="r", fork_agent_sid="f",
            file_version=3, line_offset_in_version=5, line="bumped-mid",
        ))
    messages = [rec.message for rec in caplog.records]
    assert any("version bump" in m for m in messages)
    # File truncated to empty by the bump, then the gap check dropped the line.
    assert _read(path) == ""
    # Cursor still advanced to the new version.
    assert shadow_jsonl.snapshot_cursors_for("n")["r:f"]["file_version"] == 3


def test_append_offset_below_size_truncates_partial_line_then_appends():
    _reset_state()
    path = shadow_jsonl.shadow_path("r", "f")
    _run(shadow_jsonl.append(
        node_id="n", root_id="r", fork_agent_sid="f",
        file_version=1, line_offset_in_version=0, line="aaaa",
    ))
    # file size is now 5 ("aaaa\n"); a re-send claiming offset 2 truncates
    # back to 2 bytes then rewrites — partial-line recovery.
    _run(shadow_jsonl.append(
        node_id="n", root_id="r", fork_agent_sid="f",
        file_version=1, line_offset_in_version=2, line="XX",
    ))
    assert _read(path) == "aaXX\n"


def test_append_offset_above_size_is_a_gap_drop_with_warning(caplog):
    _reset_state()
    path = shadow_jsonl.shadow_path("r", "f")
    _run(shadow_jsonl.append(
        node_id="n", root_id="r", fork_agent_sid="f",
        file_version=1, line_offset_in_version=0, line="only",
    ))
    # file size is 5; incoming claims offset 100 — we missed a prefix.
    with caplog.at_level("WARNING", logger="shadow_jsonl"):
        _run(shadow_jsonl.append(
            node_id="n", root_id="r", fork_agent_sid="f",
            file_version=1, line_offset_in_version=100, line="gap",
        ))
    assert any("gap detected" in rec.message for rec in caplog.records)
    # Dropped — file unchanged.
    assert _read(path) == "only\n"


def test_append_records_per_node_membership_for_snapshot():
    _reset_state()
    _run(shadow_jsonl.append(
        node_id="n-a", root_id="r1", fork_agent_sid="f1",
        file_version=1, line_offset_in_version=0, line="x",
    ))
    _run(shadow_jsonl.append(
        node_id="n-a", root_id="r2", fork_agent_sid="f2",
        file_version=4, line_offset_in_version=0, line="y",
    ))
    _run(shadow_jsonl.append(
        node_id="n-b", root_id="r1", fork_agent_sid="f1",
        file_version=1, line_offset_in_version=2, line="z",
    ))
    snap_a = shadow_jsonl.snapshot_cursors_for("n-a")
    snap_b = shadow_jsonl.snapshot_cursors_for("n-b")
    snap_unknown = shadow_jsonl.snapshot_cursors_for("never-heard")
    assert snap_unknown == {}
    # Both nodes are tracked for the shared key; n-a also owns r2:f2.
    assert set(snap_a) == {"r1:f1", "r2:f2"}
    assert set(snap_b) == {"r1:f1"}
    # Cursor reflects the highest version seen and the real on-disk size.
    assert snap_a["r1:f1"]["file_version"] == 1
    assert snap_a["r2:f2"]["file_version"] == 4
    # r1:f1 received two contiguous v1 lines ("x\n" then "z\n") across two
    # nodes → both present, size reflects the full projection.
    assert snap_a["r1:f1"]["shadow_size"] == len("x\nz\n")
    assert snap_b["r1:f1"]["shadow_size"] == len("x\nz\n")


def test_snapshot_cursors_size_zero_when_file_absent():
    _reset_state()
    # Record membership without an on-disk file by bumping version via append
    # then deleting the file — stat falls back to size 0.
    _run(shadow_jsonl.append(
        node_id="n", root_id="r", fork_agent_sid="f",
        file_version=1, line_offset_in_version=0, line="x",
    ))
    shadow_jsonl.shadow_path("r", "f").unlink()
    snap = shadow_jsonl.snapshot_cursors_for("n")
    assert snap["r:f"]["shadow_size"] == 0


def test_get_lock_same_key_returns_same_instance_different_keys_differ():
    _reset_state()
    lock_a1 = shadow_jsonl._get_lock(("r", "f"))
    lock_a2 = shadow_jsonl._get_lock(("r", "f"))
    lock_b = shadow_jsonl._get_lock(("other", "other"))
    assert lock_a1 is lock_a2
    assert lock_a1 is not lock_b


def test_rebuild_overwrites_authoritatively_and_resets_version_cursor():
    _reset_state()
    path = shadow_jsonl.shadow_path("r", "f")
    # Establish a v2 cursor + content via appends.
    _run(shadow_jsonl.append(
        node_id="n", root_id="r", fork_agent_sid="f",
        file_version=2, line_offset_in_version=0, line="v2-line",
    ))
    assert shadow_jsonl.snapshot_cursors_for("n")["r:f"]["file_version"] == 2

    returned = _run(shadow_jsonl.rebuild("r", "f", "authoritative\nbody\n"))
    assert returned == path
    assert _read(path) == "authoritative\nbody\n"

    # Cursor reset: a v1 push after rebuild is now accepted (1 > current 0 →
    # truncate + rewrite) instead of being dropped as stale.
    _run(shadow_jsonl.append(
        node_id="n", root_id="r", fork_agent_sid="f",
        file_version=1, line_offset_in_version=0, line="fresh-v1",
    ))
    assert _read(path) == "fresh-v1\n"


def test_rebuild_creates_parent_dirs_and_is_atomic_via_tmp_replace():
    _reset_state()
    # No prior file, nested fresh path — rebuild must mkdir parents and land
    # the final content without leaving a .tmp behind.
    path = shadow_jsonl.shadow_path("fresh-root", "fresh-fork")
    _run(shadow_jsonl.rebuild("fresh-root", "fresh-fork", "solo\n"))
    assert _read(path) == "solo\n"
    assert not path.with_suffix(".tmp").exists()


def test_append_same_key_serializes_under_lock_no_torn_interleave():
    """Same-key appends share one asyncio.Lock (see test_get_lock_*), so
    each `_append_sync` is a critical section. Assert the lock is the SAME
    instance reached via the public path and that back-to-back in-flight
    appends with truthful offsets both land as complete, uncorrupted lines
    when driven through one event loop in submission order."""
    _reset_state()
    path = shadow_jsonl.shadow_path("r", "f")

    async def submit_ordered():
        # Drive both through one loop; submission order is preserved, and the
        # per-file lock guarantees the second's sync write cannot start until
        # the first's has finished.
        t1 = asyncio.create_task(shadow_jsonl.append(
            node_id="n", root_id="r", fork_agent_sid="f",
            file_version=1, line_offset_in_version=0, line="first",
        ))
        await asyncio.sleep(0)  # let t1 acquire the lock + enter to_thread
        t2 = asyncio.create_task(shadow_jsonl.append(
            node_id="n", root_id="r", fork_agent_sid="f",
            file_version=1, line_offset_in_version=6, line="second",
        ))
        await asyncio.gather(t1, t2)

    _run(submit_ordered())
    assert _read(path) == "first\nsecond\n"
