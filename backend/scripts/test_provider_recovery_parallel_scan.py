"""Regression test for bounded-parallel provider recovery classification.

Run with:
    cd backend && PYTHONPATH=. python3 scripts/test_provider_recovery_parallel_scan.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home  # noqa: E402
_TMP_HOME = Path(_test_home.isolate("bc-test-provider-recovery-scan-"))

import provider  # noqa: E402
from runs_dir import runs_root  # noqa: E402

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class FakeProvider:
    def __init__(
        self,
        provider_id: str,
        active_state: dict[str, int],
        *,
        fail: bool = False,
    ) -> None:
        self.id = provider_id
        self.defunct = False
        self.suspended = False
        self._active_state = active_state
        self._fail = fail

    def recover_in_flight(self, *, loop=None, run_id_filter=None):
        del loop
        self._active_state["active"] += 1
        self._active_state["max_active"] = max(
            self._active_state["max_active"], self._active_state["active"],
        )
        try:
            time.sleep(0.15)
            if self._fail:
                raise RuntimeError(f"injected failure for {self.id}")
            return [
                {"run_id": run_id, "provider_id": self.id}
                for run_id in sorted(run_id_filter or [])
            ]
        finally:
            self._active_state["active"] -= 1


def _seed_run(run_id: str, provider_id: str) -> None:
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "backend_state.json").write_text(
        json.dumps({"provider_id": provider_id}),
        encoding="utf-8",
    )


def _with_fake_providers(fake_providers: dict[str, FakeProvider], fn):
    original_get_provider = provider.get_provider
    old_env = os.environ.get(provider._RECOVERY_SCAN_PARALLELISM_ENV)

    def fake_get_provider(provider_id: str):
        return fake_providers[provider_id]

    provider.get_provider = fake_get_provider
    os.environ[provider._RECOVERY_SCAN_PARALLELISM_ENV] = "4"
    try:
        return fn()
    finally:
        provider.get_provider = original_get_provider
        if old_env is None:
            os.environ.pop(provider._RECOVERY_SCAN_PARALLELISM_ENV, None)
        else:
            os.environ[provider._RECOVERY_SCAN_PARALLELISM_ENV] = old_env


def test_provider_buckets_scan_in_parallel() -> bool:
    active_state = {"active": 0, "max_active": 0}
    fake_providers = {
        f"provider-{index}": FakeProvider(f"provider-{index}", active_state)
        for index in range(4)
    }
    for index in range(4):
        _seed_run(f"run-{index}", f"provider-{index}")

    def run_scan():
        started = time.monotonic()
        recovered = provider.recover_all_in_flight()
        return recovered, time.monotonic() - started

    recovered, elapsed = _with_fake_providers(fake_providers, run_scan)

    recovered_ids = sorted(row.get("run_id") for row in recovered)
    if recovered_ids != ["run-0", "run-1", "run-2", "run-3"]:
        print(f"{FAIL} unexpected recovered ids: {recovered_ids!r}")
        return False
    if active_state["max_active"] < 2:
        print(
            f"{FAIL} expected provider scans to overlap, "
            f"max_active={active_state['max_active']}",
        )
        return False
    if elapsed >= 0.45:
        print(f"{FAIL} provider scans appear serial, elapsed={elapsed:.3f}s")
        return False
    print(f"{PASS} provider recovery buckets scan in parallel")
    return True


def test_provider_scan_failure_propagates() -> bool:
    active_state = {"active": 0, "max_active": 0}
    fake_providers = {
        "provider-0": FakeProvider("provider-0", active_state),
        "provider-1": FakeProvider("provider-1", active_state, fail=True),
        "provider-2": FakeProvider("provider-2", active_state),
        "provider-3": FakeProvider("provider-3", active_state),
    }
    _seed_run("run-fail", "provider-1")

    try:
        _with_fake_providers(fake_providers, provider.recover_all_in_flight)
    except RuntimeError as exc:
        if "provider-1" not in str(exc):
            print(f"{FAIL} failure did not name provider-1: {exc}")
            return False
        print(f"{PASS} provider scan failures propagate")
        return True
    print(f"{FAIL} expected provider scan failure to propagate")
    return False


if __name__ == "__main__":
    ok = test_provider_buckets_scan_in_parallel()
    ok = test_provider_scan_failure_propagates() and ok
    raise SystemExit(0 if ok else 1)
