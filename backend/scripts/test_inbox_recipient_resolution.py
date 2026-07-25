"""Regression lock: a hidden sub-session is a valid inbox recipient.

Sub-sessions live as embedded nodes inside their parent's root file, so a
process resolving them has only the fork index to go on. When the per-root
summary sidecar published a kind-filtered fork list, every reader that
trusted it concluded the root had no forks, `_resolve_root_id` returned
None, and `inbox` rejected a live session with "recipient session does not
exist" while `mssg`/`ask` (which resolve through the backend's resident
session tree) accepted the same id.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import sys

import _test_home
# Session creation starts a spawn-based pool whose workers re-import this
# module; they must attach to the parent's isolated home instead of minting
# (and leaking) one of their own.
if multiprocessing.parent_process() is None:
    _TMP_HOME = _test_home.isolate("bc-test-inbox-recipient-")
else:
    # Attach to the parent's home through the inherited env instead of
    # engaging it: engaging would recreate the state root a parent that has
    # already finished cleanup just deleted.
    _test_home.install_deletion_guard()
    _TMP_HOME = os.environ["BETTER_AGENT_HOME"]

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import inbox_store  # noqa: E402
import session_store  # noqa: E402
from session_manager import manager  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _forget_process_caches() -> None:
    """Model a reader that must rebuild the fork index from the sessions
    directory: no resident roots, no in-process index, and no fork-index
    sidecar (a disposable cache that any unrelated session write in a busy
    home invalidates). Only the durable root files and their per-root
    summaries survive — exactly what a runner or a restarted backend has."""
    # Bring the per-root summaries to the same state a running backend
    # leaves them in, so the reader below trusts them exactly as production
    # does instead of falling back to a full root parse by accident.
    session_store._ensure_summary_index(blocking=True)
    session_store._get_durability_writer().drain(timeout=30)
    session_store._index_sidecar_write_queue.join()
    session_store._get_durability_writer().drain(timeout=30)
    sidecar = session_store._fork_index_path()
    if sidecar.exists():
        sidecar.unlink()
    with session_store._index_lock:
        session_store._fork_index.clear()
        session_store._root_forks.clear()
        session_store._root_index_signatures.clear()
        session_store._index_loaded = False
        session_store._index_fingerprint = None
    session_store._dir_fingerprint_cache = None
    session_store._summary_index.clear()
    session_store._summary_index_loaded = False
    manager._roots.clear()
    manager._node_root_id.clear()
    manager._node_root_missing_until.clear()


def _make_root_and_sub() -> tuple[str, str]:
    root = manager.create(
        name="parent",
        model="claude-opus-5",
        cwd=_TMP_HOME,
        orchestration_mode="native",
    )
    sub = manager.create_sub_session(
        parent_session_id=root["id"],
        name="hidden sub-session",
    )
    return root["id"], sub["id"]


def test_sub_session_resolves_in_a_cold_reader() -> bool:
    root_id, sub_id = _make_root_and_sub()
    _forget_process_caches()

    resolved = session_store._resolve_root_id(sub_id)
    ok = resolved == root_id
    print(
        f"{PASS if ok else FAIL} sub-session resolves to its root in a cold reader"
        f"{'' if ok else f' resolved={resolved!r} expected={root_id!r}'}"
    )
    return ok


def test_sub_session_is_a_valid_inbox_recipient() -> bool:
    root_id, sub_id = _make_root_and_sub()
    _forget_process_caches()

    try:
        receipt = inbox_store.send(
            sender_session_id=root_id,
            recipient_session_id=sub_id,
            message="delivered to a hidden sub-session",
        )
        error = ""
    except inbox_store.InboxStoreError as exc:
        receipt = {}
        error = str(exc)

    ok = bool(receipt.get("sent")) and not error
    print(
        f"{PASS if ok else FAIL} hidden sub-session accepted as inbox recipient"
        f"{'' if ok else f' error={error!r}'}"
    )
    return ok


def test_sub_session_reads_its_own_inbox() -> bool:
    root_id, sub_id = _make_root_and_sub()
    inbox_store.send(
        sender_session_id=root_id,
        recipient_session_id=sub_id,
        message="async result for the sub-session",
    )
    _forget_process_caches()

    try:
        unread = inbox_store.read_new(recipient_session_id=sub_id)
        error = ""
    except inbox_store.InboxStoreError as exc:
        unread = {}
        error = str(exc)

    ok = unread.get("count") == 1 and not error
    print(
        f"{PASS if ok else FAIL} hidden sub-session reads its own inbox"
        f"{'' if ok else f' error={error!r} unread={unread!r}'}"
    )
    return ok


def test_unknown_recipient_is_still_rejected() -> bool:
    root_id, _ = _make_root_and_sub()
    _forget_process_caches()

    rejected = False
    try:
        inbox_store.send(
            sender_session_id=root_id,
            recipient_session_id="00000000-0000-4000-8000-000000000000",
            message="should not be delivered",
        )
    except inbox_store.InboxStoreError:
        rejected = True

    print(f"{PASS if rejected else FAIL} unknown recipient is still rejected")
    return rejected


def test_exists_and_get_lite_agree_inside_negative_window() -> bool:
    """`exists` and `get_lite` must answer from one resolution ladder. They
    used to diverge on negative caching: a miss made `get_lite` cache "not
    here" for the negative TTL while `exists` kept answering out-of-band, so
    the same sid was simultaneously a live session and a nonexistent one."""
    root_id, sub_id = _make_root_and_sub()
    _forget_process_caches()
    # A miss on the unresolved sub-session arms the negative entry.
    missed = manager.get_lite(sub_id)
    agree = manager.exists(sub_id) == (manager.get_lite(sub_id) is not None)

    ok = missed is not None and agree
    print(
        f"{PASS if ok else FAIL} exists and get_lite agree inside the negative window"
        f"{'' if ok else f' resolved={missed is not None} agree={agree}'}"
    )
    return ok


def test_fork_set_survives_repeated_cold_builds() -> bool:
    """The summary's complete fork set must not flip off when a cold build
    repairs the summary in place: an absent field forces a full parse of
    every root, so losing it every other build silently reinstates the
    startup cost the summary sidecar exists to avoid."""
    root_id, _ = _make_root_and_sub()
    summary_path = (
        session_store._root_file_path(root_id).with_name(f"{root_id}.summary.json")
    )
    present: list[bool] = []
    for _ in range(3):
        _forget_process_caches()
        present.append(
            "all_fork_ids" in json.loads(summary_path.read_text(encoding="utf-8")),
        )

    ok = all(present)
    print(
        f"{PASS if ok else FAIL} fork set survives repeated cold summary builds"
        f"{'' if ok else f' present={present!r}'}"
    )
    return ok


def main() -> int:
    try:
        checks = [
            test_sub_session_resolves_in_a_cold_reader(),
            test_sub_session_is_a_valid_inbox_recipient(),
            test_sub_session_reads_its_own_inbox(),
            test_unknown_recipient_is_still_rejected(),
            test_exists_and_get_lite_agree_inside_negative_window(),
            test_fork_set_survives_repeated_cold_builds(),
        ]
        passed = sum(1 for ok in checks if ok)
        print(f"\n{passed}/{len(checks)} checks passed")
        return 0 if passed == len(checks) else 1
    finally:
        session_store._get_durability_writer().drain(timeout=30)
        shutil.rmtree(_TMP_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
