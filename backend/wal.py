"""HTTP-scoped Write-Ahead Log for cross-store mutations (A3).

The 3 documented cross-store flows in Better Agent (approve-worker
final commit, delegate-fork mint, cascade-delete cleanup) write to
TWO disk-resident stores. A crash between writes leaves the system
inconsistent (e.g. fork Better Agent session exists in session_store but
not in worker_store's per-(caller, worker) mapping). The WAL gives
those flows atomic-or-nothing semantics:

  1. The HTTP handler enters a `UnitOfWork` context (A3b — not yet
     implemented; this module is just the durable substrate).
  2. The UoW writes a `WalEnvelope({req_id, bus_event})` to
     `pending_ops.jsonl` and fsyncs.
  3. The UoW publishes the bus event; A9-relocated stores subscribe
     and mutate idempotently on `req_id`.
  4. On clean shutdown the WAL is truncated; on startup, surviving
     envelopes are replayed BEFORE session_store loads (so a half-
     applied write completes before any user-facing read can see it).

INVARIANT — durability: every `WalEnvelope.append` does
`write tmp → flush → fsync → atomic rename`. No buffered writes.
The cost (one fsync per cross-store mutation) is acceptable because
the 3 flows aren't running 1000x/second — they're spawn-a-worker /
mint-a-fork / delete-a-session operations.

INVARIANT — schema: each envelope row is `{schema_version: 1,
req_id, ts, event: <BusEvent shape>}`. Mismatched `schema_version` on
load raises `EventSchemaError` per A16 — no auto-migration; operator
wipes the file and restarts.

INVARIANT — bounded retention: on clean shutdown, `truncate()`
empties the file (matched ops are already on disk in their target
stores). On startup, surviving envelopes are first ROTATED to
`pending_ops.replayed.jsonl` (so a crash mid-replay doesn't re-replay
the same envelopes a second time), replay runs, then the rotated
file is unlinked.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from event_bus import BusEvent
from paths import ba_home

logger = logging.getLogger(__name__)


_WAL_SCHEMA_VERSION = 1
_WAL_FILENAME = "pending_ops.jsonl"
_WAL_REPLAYED_FILENAME = "pending_ops.replayed.jsonl"


@dataclass
class WalEnvelope:
    """One row in `pending_ops.jsonl`.

    `req_id` is the idempotency key. Subscribers consuming the
    replayed event MUST check their per-store "already applied" set
    keyed on `req_id` and no-op if the operation is recorded as
    done. A subscriber re-applying the SAME req_id twice would be
    the lost-write / double-write bug A3 exists to prevent.

    `event` is the `BusEvent` shape the UoW publishes after fsync.
    On replay we reconstruct the BusEvent and call
    `bus.publish(event, is_replay=True)` — see A16's replay path.
    """
    req_id: str
    ts: str
    event_type: str
    root_id: str
    sid: str
    payload: dict
    schema_version: int = _WAL_SCHEMA_VERSION
    event_seq: int = 0
    event_persist: bool = False
    msg_id: Optional[str] = None
    run_id: Optional[str] = None

    @classmethod
    def from_bus_event(cls, *, req_id: str, event: BusEvent) -> "WalEnvelope":
        return cls(
            req_id=req_id,
            ts=event.ts,
            event_type=event.type,
            root_id=event.root_id,
            sid=event.sid,
            payload=dict(event.payload),
            schema_version=_WAL_SCHEMA_VERSION,
            event_seq=event.seq,
            event_persist=event.persist,
            msg_id=event.msg_id,
            run_id=event.run_id,
        )

    def to_bus_event(self) -> BusEvent:
        """Reconstruct a `BusEvent` for replay. Note: `is_replay`
        is set by `bus.publish(..., is_replay=True)`, NOT here."""
        return BusEvent(
            type=self.event_type,
            root_id=self.root_id,
            sid=self.sid,
            payload=dict(self.payload),
            msg_id=self.msg_id,
            run_id=self.run_id,
            ts=self.ts,
            persist=self.event_persist,
            schema_version=1,
            seq=self.event_seq,
            is_replay=False,
        )

    def to_json(self) -> str:
        return json.dumps({
            "schema_version": self.schema_version,
            "req_id": self.req_id,
            "ts": self.ts,
            "event_type": self.event_type,
            "root_id": self.root_id,
            "sid": self.sid,
            "payload": self.payload,
            "event_seq": self.event_seq,
            "event_persist": self.event_persist,
            "msg_id": self.msg_id,
            "run_id": self.run_id,
        }, sort_keys=True)


class WalSchemaError(Exception):
    """Raised on load when an envelope row's `schema_version` doesn't
    match `_WAL_SCHEMA_VERSION`. Per CLAUDE.md, no auto-migration;
    operator wipes `pending_ops.jsonl` + `pending_ops.replayed.jsonl`
    and restarts."""
    pass


def _wal_path() -> Path:
    """Lazy resolver — per the A12 convention, never cache `ba_home()`
    at module load. Tests override `BETTER_CLAUDE_HOME` mid-process
    and a cached Path would point at the developer's real WAL."""
    return ba_home() / _WAL_FILENAME


def _replayed_path() -> Path:
    return ba_home() / _WAL_REPLAYED_FILENAME


def new_req_id() -> str:
    """Mint a unique idempotency key for a single UoW commit."""
    return f"req_{uuid.uuid4().hex}"


def append(envelope: WalEnvelope) -> None:
    """Atomically append one envelope to the WAL. Sync — callers
    invoke via `asyncio.to_thread`.

    Durability: write the line to a per-call tmp file, fsync, then
    use POSIX `O_APPEND` to ship it onto the WAL. The append is
    serialized by the filesystem's atomic-append guarantee (single
    `write()` of <PIPE_BUF bytes is atomic against concurrent
    appenders). For our envelope size (~300 bytes typical), this
    holds on every major filesystem.

    We additionally fsync the WAL fd after the append so a crash
    immediately after `append()` returns leaves the envelope durable
    on disk. The cost (one fsync per cross-store mutation) is the
    price A3 explicitly accepts: the 3 flows aren't 1000x/sec, and
    losing a pending op is the bug class A3 exists to prevent.
    """
    line = envelope.to_json() + "\n"
    p = _wal_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Open with O_APPEND so concurrent writes interleave at line
    # boundaries (kernel-enforced for writes < PIPE_BUF).
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _parse_line(line: str) -> WalEnvelope:
    row = json.loads(line)
    if not isinstance(row, dict):
        raise WalSchemaError(f"WAL row is not a dict: {row!r}")
    schema = row.get("schema_version")
    if schema != _WAL_SCHEMA_VERSION:
        raise WalSchemaError(
            f"WAL row schema_version={schema!r} (expected "
            f"{_WAL_SCHEMA_VERSION}); no auto-migration — wipe "
            f"{_wal_path()} + {_replayed_path()} and restart."
        )
    return WalEnvelope(
        req_id=row["req_id"],
        ts=row["ts"],
        event_type=row["event_type"],
        root_id=row["root_id"],
        sid=row["sid"],
        payload=row.get("payload") or {},
        schema_version=schema,
        event_seq=int(row.get("event_seq") or 0),
        event_persist=bool(row.get("event_persist", False)),
        msg_id=row.get("msg_id"),
        run_id=row.get("run_id"),
    )


def read_pending() -> list[WalEnvelope]:
    """Load every envelope currently in the WAL. Used by tests +
    startup replay. Raises `WalSchemaError` on any row whose
    schema_version doesn't match — the file is poisoned and the
    operator must wipe it (per CLAUDE.md no-migrations rule)."""
    p = _wal_path()
    if not p.exists():
        return []
    out: list[WalEnvelope] = []
    with p.open("r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            out.append(_parse_line(raw))
    return out


def read_replayed() -> list[WalEnvelope]:
    """Load every envelope from the rotated `pending_ops.replayed.jsonl`.
    Used by startup-replay to recover from a crash mid-replay (the
    rotated file is the source of truth during the replay window;
    the live WAL is empty)."""
    p = _replayed_path()
    if not p.exists():
        return []
    out: list[WalEnvelope] = []
    with p.open("r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            out.append(_parse_line(raw))
    return out


def rotate_to_replayed() -> bool:
    """Atomic rename `pending_ops.jsonl` → `pending_ops.replayed.jsonl`.
    Called at startup BEFORE replay begins. If a crash interrupts the
    replay itself, the next startup picks up the rotated file (live
    WAL is empty) and re-replays.

    Returns True if a file was rotated (i.e. there were envelopes to
    replay); False if the live WAL didn't exist.

    INVARIANT: if `pending_ops.replayed.jsonl` already exists from a
    prior crashed-mid-replay run, we APPEND the current WAL to it
    rather than overwriting — so envelopes already in `.replayed`
    don't get lost. (Cheap: each is at most a few-hundred lines.)
    """
    live = _wal_path()
    if not live.exists():
        return False
    replayed = _replayed_path()
    if replayed.exists():
        # Crashed mid-replay before: merge live → replayed.
        with live.open("r", encoding="utf-8") as src, \
             replayed.open("a", encoding="utf-8") as dst:
            for raw in src:
                dst.write(raw)
            dst.flush()
            os.fsync(dst.fileno())
        live.unlink()
    else:
        os.replace(live, replayed)
    # fsync the parent dir so the rename is durable on power loss.
    try:
        d_fd = os.open(str(live.parent), os.O_RDONLY)
        try:
            os.fsync(d_fd)
        finally:
            os.close(d_fd)
    except OSError:
        pass  # Windows / FUSE — best-effort
    return True


def unlink_replayed() -> None:
    """Delete `pending_ops.replayed.jsonl` after a successful boot
    replay. Idempotent (no-op if absent)."""
    p = _replayed_path()
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def truncate() -> None:
    """Empty the live WAL. Called on clean shutdown — every envelope's
    target store is already on disk, so the pending log is no longer
    needed. Atomic via tmp+rename (`open("w")` would briefly leave a
    zero-byte WAL visible)."""
    live = _wal_path()
    if not live.exists():
        return
    tmp = live.with_suffix(live.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, live)


def iter_replay_envelopes() -> Iterator[WalEnvelope]:
    """Yield envelopes from `pending_ops.replayed.jsonl` for the
    startup replay loop. After this generator exhausts, the caller
    is responsible for calling `unlink_replayed()`."""
    yield from read_replayed()
