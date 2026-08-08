from __future__ import annotations

import asyncio
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-provider-cancel-")

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from provider import Provider  # noqa: E402
from run_recovery import _recovered_run_cancelled  # noqa: E402
from runs_dir import runs_root  # noqa: E402


@dataclass
class _FakeRunState:
    run_id: str
    run_dir: Path
    turn_run_id: Optional[str] = None
    cancelled: bool = False
    turn_cancelled: bool = False


class _Provider(Provider):
    KIND = "test"

    def __init__(self, record: dict) -> None:
        super().__init__(record)
        self._runs = {}
        self.persisted_states = []

    def build_env(self) -> dict[str, str]:
        return {}

    def _start_run(self, **kwargs) -> None:
        raise NotImplementedError

    def _write_backend_state(self, rs) -> None:
        self.persisted_states.append({
            "cancelled": rs.cancelled,
            "turn_cancelled": rs.turn_cancelled,
        })

    def recover_in_flight(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        run_id_filter: Optional[set[str]] = None,
    ) -> list[dict]:
        return []

    def prune_old_runs(self, max_age_days: int = 7) -> int:
        return 0

    async def run_headless(self, **kwargs) -> Optional[dict]:
        return None

    async def rewind(self, agent_sid: str, message_uuid: str) -> None:
        pass


def test_unknown_registered_run_dir_gets_cancel_sentinel() -> None:
    run_id = "run-lost-registry"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "backend_state.json").write_text(
        '{"run_id":"run-lost-registry","cancelled":false}',
        encoding="utf-8",
    )
    provider = _Provider({"id": "provider-test"})

    assert provider.cancel_turn(run_id), "cancel_turn returned False"
    assert (run_dir / "cancel").exists(), "cancel sentinel was not written"
    state = json.loads(
        (run_dir / "backend_state.json").read_text(encoding="utf-8")
    )
    assert state.get("turn_cancelled") is True, f"detached soft cancel was not persisted: {state!r}"


def test_unknown_missing_run_stays_noop() -> None:
    provider = _Provider({"id": "provider-test"})
    assert not provider.cancel_turn("missing-run"), "missing run returned True"
    assert not (runs_root() / "missing-run" / "cancel").exists(), "missing run created a cancel file"


def test_turn_run_id_resolves_to_provider_run() -> None:
    """`active_run_ids`/`_run_state` register live turns under the
    orchestrator-level turn_run_id (turn_manager.py), not this
    provider's own run id — the id `_cancel_turn_fanout` actually fans
    out with. Every RunState stamps `turn_run_id` at spawn time; a
    cancel arriving with that id must resolve through it instead of
    logging "unknown run_id" and never touching the sentinel."""
    run_id = "run-provider-native-id"
    turn_run_id = "turn-orchestrator-id"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True)
    provider = _Provider({"id": "provider-test"})
    provider._runs[run_id] = _FakeRunState(
        run_id=run_id, run_dir=run_dir, turn_run_id=turn_run_id,
    )

    assert provider.cancel_turn(turn_run_id), "cancel_turn(turn_run_id) returned False"
    assert (run_dir / "cancel").exists(), "cancel sentinel was not written for the resolved run"
    assert provider._runs[run_id].turn_cancelled, "soft cancel intent was not retained on the run"
    assert not provider._runs[run_id].cancelled, "soft cancel was incorrectly promoted to a hard cancel"
    assert provider.persisted_states == [{
        "cancelled": False,
        "turn_cancelled": True,
    }], f"soft cancel intent was not persisted: {provider.persisted_states!r}"


def test_path_escape_is_rejected() -> None:
    provider = _Provider({"id": "provider-test"})
    assert not provider.cancel_turn("../escape"), "path escape returned True"
    assert not (runs_root().parent / "escape" / "cancel").exists(), "path escape wrote outside runs root"


def test_recovery_reads_runner_soft_cancel() -> None:
    run_id = "run-completed-soft-cancel"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "complete.json").write_text(
        '{"success":false,"cancelled":true,"error":"error_during_execution"}',
        encoding="utf-8",
    )
    assert _recovered_run_cancelled({"run_id": run_id}), "recovery ignored the runner's durable soft cancel"
    assert not _recovered_run_cancelled({"run_id": "missing-run"}), "missing run was spuriously classified as cancelled"

    late_run_id = "run-late-soft-cancel"
    late_run_dir = runs_root() / late_run_id
    late_run_dir.mkdir(parents=True)
    (late_run_dir / "cancel").touch()
    (late_run_dir / "complete.json").write_text(
        '{"success":true,"cancelled":false,"error":null}',
        encoding="utf-8",
    )
    assert not _recovered_run_cancelled({
        "run_id": late_run_id,
        "turn_cancelled": True,
    }), "late soft cancel overrode authoritative successful completion"

    legacy_run_id = "run-legacy-soft-cancel"
    legacy_run_dir = runs_root() / legacy_run_id
    legacy_run_dir.mkdir(parents=True)
    (legacy_run_dir / "cancel").touch()
    (legacy_run_dir / "complete.json").write_text(
        '{"success":false,"error":"error_during_execution"}',
        encoding="utf-8",
    )
    assert _recovered_run_cancelled({"run_id": legacy_run_id}), "durable cancel sentinel was ignored for a legacy completion"


TESTS = [
    ("unknown registered run dir gets cancel sentinel", test_unknown_registered_run_dir_gets_cancel_sentinel),
    ("unknown missing run stays noop", test_unknown_missing_run_stays_noop),
    ("turn_run_id resolves to provider run", test_turn_run_id_resolves_to_provider_run),
    ("path escape is rejected", test_path_escape_is_rejected),
    ("recovery reads runner soft cancel", test_recovery_reads_runner_soft_cancel),
]


def main() -> int:
    failures = []
    try:
        for name, fn in TESTS:
            try:
                fn()
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"FAIL: {name}  ({e})")
                failures.append(name)
                continue
            print(f"PASS: {name}")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if failures:
        print("Failures:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
