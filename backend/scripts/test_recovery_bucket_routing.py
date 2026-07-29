"""Which loop a recovered session bucket integrates on.

A bucket reaches loop-bound state in exactly two cases, both decidable
before integration starts:
  - a live run with no completion payload takes the reattach branch;
  - a completed run whose payload is a provider-capability-change error
    is retried as a FRESH run.
Everything else is replay/reconcile/marker work and belongs on the
recovery thread. Misrouting a live bucket off-main would build its
reattach queue on the wrong loop, so the predicate fails CLOSED.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths

paths.engage_test_home(tempfile.mkdtemp(prefix="bucket-routing-"))

import run_recovery  # noqa: E402


def _desc(**kw):
    base = {"run_id": "run-1", "started_at": "2026-07-29T00:00:00Z"}
    base.update(kw)
    return base


def test_live_run_without_completion_stays_on_main() -> None:
    descs = [_desc(alive=True, has_complete_json=False)]
    assert run_recovery._bucket_needs_main_loop(descs) is True


def test_orphaned_cli_counts_as_live() -> None:
    descs = [_desc(orphaned_cli=True, has_complete_json=False)]
    assert run_recovery._bucket_needs_main_loop(descs) is True


def test_dead_completed_run_goes_to_the_recovery_thread(monkey=None) -> None:
    original = run_recovery._is_provider_capability_change
    run_recovery._is_provider_capability_change = lambda run_id: False
    try:
        descs = [_desc(alive=False, has_complete_json=True)]
        assert run_recovery._bucket_needs_main_loop(descs) is False
    finally:
        run_recovery._is_provider_capability_change = original


def test_capability_change_retry_stays_on_main_even_though_completed() -> None:
    """The subtle case: looks dead, but is retried as a fresh live run."""
    original = run_recovery._is_provider_capability_change
    run_recovery._is_provider_capability_change = lambda run_id: True
    try:
        descs = [_desc(alive=False, has_complete_json=True)]
        assert run_recovery._bucket_needs_main_loop(descs) is True
    finally:
        run_recovery._is_provider_capability_change = original


def test_dead_incomplete_run_goes_to_the_recovery_thread() -> None:
    descs = [_desc(alive=False, has_complete_json=False)]
    assert run_recovery._bucket_needs_main_loop(descs) is False


def test_empty_bucket_fails_closed() -> None:
    assert run_recovery._bucket_needs_main_loop([]) is True


def test_unreadable_bucket_fails_closed() -> None:
    original = run_recovery._is_provider_capability_change

    def boom(run_id):
        raise OSError("disk gone")

    run_recovery._is_provider_capability_change = boom
    try:
        descs = [_desc(alive=False, has_complete_json=True)]
        assert run_recovery._bucket_needs_main_loop(descs) is True
    finally:
        run_recovery._is_provider_capability_change = original


def test_latest_run_decides_the_bucket() -> None:
    """Only the latest run reaches _integrate_one, so it decides."""
    original = run_recovery._is_provider_capability_change
    run_recovery._is_provider_capability_change = lambda run_id: False
    try:
        descs = [
            _desc(run_id="old", started_at="2026-07-01T00:00:00Z",
                  alive=False, has_complete_json=True),
            _desc(run_id="new", started_at="2026-07-29T00:00:00Z",
                  alive=True, has_complete_json=False),
        ]
        assert run_recovery._bucket_needs_main_loop(descs) is True

        descs[1] = _desc(run_id="new", started_at="2026-07-29T00:00:00Z",
                         alive=False, has_complete_json=True)
        assert run_recovery._bucket_needs_main_loop(descs) is False
    finally:
        run_recovery._is_provider_capability_change = original


def test_replay_only_buckets_are_routed_through_the_recovery_manager() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "run_recovery.py"
    ).read_text(encoding="utf-8")
    idx = source.index("async def _drain(worker: int)")
    body = source[idx:idx + 1800]
    assert "_bucket_needs_main_loop(descs)" in body
    assert "recovery_manager.manager.run(" in body


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("PASS")
