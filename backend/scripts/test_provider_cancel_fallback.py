from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-provider-cancel-")

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from provider import Provider  # noqa: E402
from runs_dir import runs_root  # noqa: E402


@dataclass
class _FakeRunState:
    run_id: str
    run_dir: Path
    turn_run_id: Optional[str] = None


class _Provider(Provider):
    KIND = "test"

    def __init__(self, record: dict) -> None:
        super().__init__(record)
        self._runs = {}

    def build_env(self) -> dict[str, str]:
        return {}

    def start_run(self, **kwargs) -> None:
        raise NotImplementedError

    def _persists_backend_state(self, rs) -> bool:
        return False

    def _backend_state_fields(self, rs) -> dict:
        return {}

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


def test_unknown_registered_run_dir_gets_cancel_sentinel() -> bool:
    run_id = "run-lost-registry"
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True)
    provider = _Provider({"id": "provider-test"})

    if not provider.cancel_turn(run_id):
        print("cancel_turn returned False")
        return False
    if not (run_dir / "cancel").exists():
        print("cancel sentinel was not written")
        return False
    return True


def test_unknown_missing_run_stays_noop() -> bool:
    provider = _Provider({"id": "provider-test"})
    if provider.cancel_turn("missing-run"):
        print("missing run returned True")
        return False
    if (runs_root() / "missing-run" / "cancel").exists():
        print("missing run created a cancel file")
        return False
    return True


def test_turn_run_id_resolves_to_provider_run() -> bool:
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

    if not provider.cancel_turn(turn_run_id):
        print("cancel_turn(turn_run_id) returned False")
        return False
    if not (run_dir / "cancel").exists():
        print("cancel sentinel was not written for the resolved run")
        return False
    return True


def test_path_escape_is_rejected() -> bool:
    provider = _Provider({"id": "provider-test"})
    if provider.cancel_turn("../escape"):
        print("path escape returned True")
        return False
    if (runs_root().parent / "escape" / "cancel").exists():
        print("path escape wrote outside runs root")
        return False
    return True


TESTS = [
    ("unknown registered run dir gets cancel sentinel", test_unknown_registered_run_dir_gets_cancel_sentinel),
    ("unknown missing run stays noop", test_unknown_missing_run_stays_noop),
    ("turn_run_id resolves to provider run", test_turn_run_id_resolves_to_provider_run),
    ("path escape is rejected", test_path_escape_is_rejected),
]


def main() -> int:
    failures = []
    try:
        for name, fn in TESTS:
            ok = fn()
            print(("PASS" if ok else "FAIL") + f": {name}")
            if not ok:
                failures.append(name)
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if failures:
        print("Failures:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
