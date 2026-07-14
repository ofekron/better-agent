"""Disk-backed status for in-flight `ask` team-message calls.

Thin binding over `operation_status_store.ASK_STATUS`: lets a runner's
`ask` tool re-attach to the target turn it started after a backend
restart, instead of re-queueing a duplicate prompt. Keyed by a stable
client-side `ask_id`; one JSON file per in-flight ask under
`<ba_home>/ask-status/`.

A record holds the correlation ids needed to re-attach
(`lifecycle_msg_id`, `target_session_id`, `sender_session_id`) and, once
the target turn resolves, the `result` payload the runner's `recover`
path returns without re-POSTing.
"""

from __future__ import annotations

from operation_status_store import ASK_STATUS as _store

status_path = _store.status_path
write_status = _store.write_status
write_status_async = _store.write_status_async
read_status = _store.read_status
delete_status = _store.delete_status
