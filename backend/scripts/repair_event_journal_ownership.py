"""Repair event-journal ownership for an existing session.

Three mutually exclusive modes:

  Reconcile orphans (rows with no owner at write time) using currently
  known facts:

    cd backend
    .venv/bin/python scripts/repair_event_journal_ownership.py SESSION_ID

  Correct specific ALREADY-owned rows (e.g. a stale recovery-replay that
  attributed a row to the wrong turn) to the correct owner:

    cd backend
    .venv/bin/python scripts/repair_event_journal_ownership.py SESSION_ID \\
        --correct-seq 1717 --correct-seq 1718 --correct-msg-id 10e6a5a9-... \\
        --reason "stale replay misattribution" [--apply]

    (dry run by default — pass --apply to actually append the correction)

  Bump the root's canonical-projection generation (forces a clean
  single-pass re-derivation from the current event journal, discarding
  any stale/conflicting facts committed under the previous generation —
  e.g. after a stuck cutover left facts derived under a fixed bug still
  sitting in the store) and reset the BFF-side projection cache + feed
  cursor for this root so it re-pulls from the new generation instead of
  under-reading with a stale seq cursor:

    cd backend
    .venv/bin/python scripts/repair_event_journal_ownership.py SESSION_ID \\
        --bump-generation [--apply]

    (dry run by default — pass --apply to actually perform the reset)

Takes the same instance lock the runtime holds for the whole process
lifetime (`backend_instance_lock`) so this can never run concurrently
with a live backend against the same home — it either waits for the
running backend to exit or fails fast with a clear error. The BFF
process must also be stopped — this script does not coordinate with a
live BFF's own in-memory cursor cache, which would otherwise overwrite
the cursor reset on its next periodic persist.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from backend_instance_lock import (  # noqa: E402
    acquire_backend_instance_lock,
    release_backend_instance_lock,
)
from event_ingester import event_ingester  # noqa: E402
from event_journal import EventJournalReader, EventJournalWriter  # noqa: E402


def _run_reconcile(args: argparse.Namespace) -> int:
    reader = EventJournalReader()
    before = len(reader.read_orphan_events(args.session_id))
    writer = EventJournalWriter()
    try:
        resolved = writer.reconcile_ownership_sync(args.session_id)
    finally:
        writer.close()
        event_ingester.close_all()
    after = len(EventJournalReader().read_orphan_events(args.session_id))
    print(
        f"{args.session_id}: resolved={resolved} "
        f"effective_orphans_before={before} effective_orphans_after={after}",
    )
    return 0


def _run_correct(args: argparse.Namespace) -> int:
    if not args.correct_msg_id:
        print("--correct-msg-id is required with --correct-seq")
        return 1
    if not args.reason:
        print("--reason is required with --correct-seq")
        return 1
    reader = EventJournalReader()
    targets: list[tuple[int, dict]] = []
    for seq in args.correct_seq:
        page, _, _ = event_ingester.read_events(
            args.session_id, after_seq=seq - 1, limit=1,
        )
        if not page or int(page[0].get("seq") or 0) != seq:
            print(f"seq {seq} not found at the expected position — aborting")
            return 1
        row = page[0]
        current_msg_id = row.get("msg_id")
        print(
            f"seq={seq} type={row.get('type')} source={row.get('source')} "
            f"current_msg_id={current_msg_id!r} -> {args.correct_msg_id!r}",
        )
        targets.append((seq, row))
    if not args.apply:
        print("dry run only — pass --apply to append the correction(s)")
        return 0
    writer = EventJournalWriter()
    try:
        for seq, row in targets:
            writer.correct_event_ownership_sync(
                args.session_id, seq, row, args.correct_msg_id,
                reason=args.reason,
            )
        print(f"appended {len(targets)} correction(s)")
    finally:
        writer.close()
        event_ingester.close_all()
    return 0


def _cursors_json_path():
    from paths import ba_home
    return ba_home() / "app-state" / "chat-feed-cache" / "cursors.json"


def _pop_bff_cursor(session_id: str) -> bool:
    """Mirrors `bff_chat_feed.ChatFeedClient._persist_cursors`'s atomic-
    write pattern. Returns True if an entry was actually removed."""
    path = _cursors_json_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        print(f"  cursors.json unreadable at {path}; leaving untouched")
        return False
    if not isinstance(raw, dict) or session_id not in raw:
        return False
    raw.pop(session_id, None)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return True


def _run_bump_generation(args: argparse.Namespace) -> int:
    from canonical_runtime_journal import canonical_runtime_journal
    from chat_projection_service import CanonicalChatProjectionService
    from chat_projection_source_catalog import ChatProjectionSourceCatalog
    from chat_projection_store_jsonl import JsonlChatProjectionStore
    import session_store

    session_id = args.session_id
    journal = canonical_runtime_journal()
    current = journal.current_authority(session_id)
    if current is None:
        print(f"{session_id}: no canonical authority row found — nothing to bump")
        return 1
    print(
        f"current authority: generation={current.root_generation} "
        f"authority={current.authority!r} "
        f"canonical_through_seq={current.canonical_through_seq} "
        f"journal_through_seq={current.journal_through_seq}",
    )

    session = session_store.get_session(session_id)
    provider_id = session.get("provider_id") if isinstance(session, dict) else None
    provider = "claude"
    if isinstance(provider_id, str) and provider_id:
        from config_store import get_provider
        provider_record = get_provider(provider_id)
        kind = provider_record.get("kind") if isinstance(provider_record, dict) else None
        if kind in {"claude", "codex", "gemini"}:
            provider = kind

    catalog = ChatProjectionSourceCatalog()
    service = CanonicalChatProjectionService()
    bff_generation = catalog.root_generation(session_id)
    bff_authority = service.register(
        provider=provider, session_id=session_id, root_id=session_id,
        root_generation=bff_generation, store_kind="jsonl",
    )
    bff_store_path = bff_authority.store_path
    cursor_path = _cursors_json_path()
    print(f"  BFF projection store: {bff_store_path}")
    print(f"  BFF feed cursor file: {cursor_path}")

    if not args.apply:
        print("dry run only — pass --apply to bump generation + reset BFF cache/cursor")
        return 0

    new_generation = journal.begin_delete_root(session_id)
    if new_generation is None:
        print(f"{session_id}: begin_delete_root found no authority — aborting")
        return 1
    try:
        journal.finish_delete_root(session_id, new_generation)
    except Exception:
        journal.abort_delete_root(session_id, new_generation)
        raise
    after = journal.current_authority(session_id)
    print(
        f"bumped to generation={after.root_generation if after else '?'} "
        f"authority={after.authority if after else '?'!r}",
    )

    bff_store = JsonlChatProjectionStore(bff_store_path)
    try:
        bff_store.delete_root(session_id)
        print(f"  invalidated BFF projection store for {session_id}")
    finally:
        bff_store.close()

    if _pop_bff_cursor(session_id):
        print(f"  reset BFF feed cursor for {session_id}")
    else:
        print(f"  no BFF feed cursor entry for {session_id} (nothing to reset)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id")
    parser.add_argument("--correct-seq", type=int, action="append", default=[])
    parser.add_argument("--correct-msg-id")
    parser.add_argument("--reason")
    parser.add_argument("--bump-generation", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    acquire_backend_instance_lock()
    try:
        if args.bump_generation:
            return _run_bump_generation(args)
        if args.correct_seq:
            return _run_correct(args)
        return _run_reconcile(args)
    finally:
        release_backend_instance_lock()


if __name__ == "__main__":
    sys.exit(main())
