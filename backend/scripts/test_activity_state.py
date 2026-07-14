#!/usr/bin/env python3
from activity_state import transition_activity


state = {
    "foreground_status": "running",
    "background_work_ids": ["b", "a", "a"],
    "activity_revision": 4,
    "provider_field": "preserved",
}

normalized = transition_activity(state, foreground_status="completed")
assert normalized is not None
assert normalized["background_work_ids"] == ["a", "b"]
assert normalized["foreground_status"] == "completed"
assert normalized["activity_revision"] == 5
assert normalized["provider_field"] == "preserved"
assert state["background_work_ids"] == ["b", "a", "a"]

unchanged = transition_activity(normalized)
assert unchanged is None

failed = transition_activity(
    normalized,
    foreground_status="failed",
    background_work_ids={"worker-2", "worker-1"},
)
assert failed is not None
assert failed["foreground_status"] == "failed"
assert failed["background_work_ids"] == ["worker-1", "worker-2"]
assert failed["activity_revision"] == 6

try:
    transition_activity(failed, foreground_status="unknown")
except ValueError:
    pass
else:
    raise AssertionError("unknown foreground status must be rejected")

print("activity state tests passed")
