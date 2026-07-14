#!/usr/bin/env python3
from types import SimpleNamespace

from provider import Provider


owner = SimpleNamespace(id="provider-1")
run = SimpleNamespace(
    run_id="run-1",
    app_session_id="session-1",
    persist_to=None,
    mode="native",
    popen=SimpleNamespace(pid=1234),
    started_at="2026-07-14T00:00:00",
    session_id="native-1",
    cancelled=False,
    target_message_id="message-1",
    turn_run_id="turn-1",
    lifecycle_msg_id="lifecycle-1",
)

state = Provider._common_backend_state(owner, run, processed_line=7)
assert state == {
    "run_id": "run-1",
    "app_session_id": "session-1",
    "persist_to": "session-1",
    "mode": "native",
    "runner_pid": 1234,
    "started_at": "2026-07-14T00:00:00",
    "session_id": "native-1",
    "cancelled": False,
    "target_message_id": "message-1",
    "turn_run_id": "turn-1",
    "lifecycle_msg_id": "lifecycle-1",
    "provider_id": "provider-1",
    "processed_line": 7,
}

try:
    Provider._common_backend_state(owner, run, run_id="override")
except ValueError:
    pass
else:
    raise AssertionError("provider extras must not override common fields")

run.popen.pid = 0
try:
    Provider._common_backend_state(owner, run)
except ValueError:
    pass
else:
    raise AssertionError("invalid pid must be rejected")

print("provider backend-state template tests passed")
