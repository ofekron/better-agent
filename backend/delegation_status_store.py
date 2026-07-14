"""Disk-backed status for in-flight delegations (delegate/ask-fork).

Thin binding over `operation_status_store.DELEGATION_STATUS`, keyed by
`delegation_id` (the runner-supplied `client_delegation_id` when
available); one JSON file per delegation under
`<ba_home>/delegate-status/`. Records progress through
resolving/queued/running/complete plus the correlation ids reattach
needs (`provider_run_dir`, `worker_agent_session_id`, ...).
"""

from __future__ import annotations

from operation_status_store import DELEGATION_STATUS as _store

status_path = _store.status_path
write_status = _store.write_status
write_status_async = _store.write_status_async
read_status = _store.read_status
