"""Lock the dead-runner complete.json salvage.

The backend used to fabricate "runner exited without delivering a
complete event" whenever the runner process was dead and no complete
EVENT had crossed the queue — even when the runner had succeeded and
written complete.json seconds earlier (a provider-event-vs-dead-check
race, proven by 4/4 recent "failures" whose complete.json read
success=True 3-4s before the synthesis fired). `turn_manager` now
trusts the on-disk complete.json first.

Locks both the salvage helper (`runs_dir.salvage_complete_payload`) and
that `turn_manager`'s dead-runner path actually calls it.
"""
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import _test_home
_test_home.isolate("bc-salvage-")

from runs_dir import runs_root, salvage_complete_payload

failures = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


def write_complete(run_id, payload, per_turn=False):
    d = runs_root() / run_id
    d.mkdir(parents=True, exist_ok=True)
    if per_turn:
        t = d / "turns" / (run_id + "-turn")
        t.mkdir(parents=True, exist_ok=True)
        (t / "complete.json").write_text(json.dumps(payload), encoding="utf-8")
    else:
        (d / "complete.json").write_text(json.dumps(payload), encoding="utf-8")


# 1. successful run-level complete.json -> salvaged as success
write_complete("r-success", {"success": True, "error": None, "session_id": "s1",
                             "token_usage": {"input_tokens": 10}})
p = salvage_complete_payload("r-success")
check(p is not None and p["success"] is True and p["error"] is None,
      "success complete.json salvaged as success=True")

# 2. failed complete.json -> salvaged with the REAL error (accurate messaging)
write_complete("r-fail", {"success": False, "error": "rate limit exceeded",
                          "session_id": "s2", "token_usage": None})
p = salvage_complete_payload("r-fail")
check(p is not None and p["success"] is False and p["error"] == "rate limit exceeded",
      "failed complete.json salvaged with accurate error")

# 3. no complete.json -> None (genuine no-output death keeps the synthetic msg)
none_dir = runs_root() / "r-none"
none_dir.mkdir(parents=True, exist_ok=True)
check(salvage_complete_payload("r-none") is None,
      "no complete.json -> None (synthetic failure still applies)")

# 4. only per-turn complete.json (run-level missing — the exact gap case)
write_complete("r-turn", {"success": True, "error": None, "session_id": "s4",
                          "token_usage": None}, per_turn=True)
p = salvage_complete_payload("r-turn")
check(p is not None and p["success"] is True,
      "per-turn-only complete.json salvaged via read_best_complete fallback")

# 5. missing run dir -> None
check(salvage_complete_payload("does-not-exist") is None, "missing run dir -> None")

# 6. wiring: turn_manager's dead-runner path must call the salvage before
#    falling back to the fabricated failure. (Fails before the fix.)
tm_src = open(BACKEND / "turn_manager.py", encoding="utf-8").read()
check("salvage_complete_payload(run_id)" in tm_src
      and "runner exited without delivering a complete event" in tm_src,
      "turn_manager dead-runner path calls salvage_complete_payload")

print(f"\n{'PASS' if not failures else 'FAIL'}: {6 - len(failures)}/6 groups, "
      f"{len(failures)} failed checks")
sys.exit(1 if failures else 0)
