from __future__ import annotations

import json
import os
import shutil
import sys

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runs-delete-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import runs_dir  # noqa: E402


def _seed_run(root, run_id: str, app_sid: str, *, provider_sid: str | None = None) -> None:
    """Write a run dir with both state.json (indexed) and backend_state.json
    (the verify target) attributing it to app_sid."""
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    provider_sid = provider_sid or (f"prov-{run_id}")
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "session_id": provider_sid,
                "jsonl_path": str(run_dir / "stream.jsonl"),
                "app_session_id": app_sid,
            }
        ),
        encoding="utf-8",
    )
    # backend_state.json carries the EXACT key deletion attributes a run by.
    (run_dir / "backend_state.json").write_text(
        json.dumps({"persist_to": app_sid, "app_session_id": app_sid}),
        encoding="utf-8",
    )


def main() -> int:
    root = runs_dir.runs_root()
    root.mkdir(parents=True, exist_ok=True)

    target_sid = "target-ba-session"
    other_sid = "survivor-ba-session"

    # Many decoys so an exhaustive walk would be visibly O(N). They share the
    # other (surviving) session and must NOT be reaped.
    _N_DECOYS = 75
    for i in range(_N_DECOYS):
        _seed_run(root, f"decoy-{i}", other_sid)

    # Two run dirs for the SAME deleted session — proves we keep ALL run dirs
    # per session (one per turn), not a collapsed one-per-session.
    _seed_run(root, "match-turn-1", target_sid, provider_sid="prov-A")
    _seed_run(root, "match-turn-2", target_sid, provider_sid="prov-B")

    checks = []

    # 1. Indexed fast path returns ALL run dirs for the sid (no collapse),
    #    and NONE of the decoys — without walking every dir.
    indexed = runs_dir._run_dirs_for_app_sessions_indexed(
        root, frozenset({target_sid})
    )
    indexed_names = sorted(p.name for p in (indexed or []))
    checks.append((indexed is not None, "indexed fast path available (not None)"))
    checks.append(
        (indexed_names == ["match-turn-1", "match-turn-2"], indexed_names)
    )

    # 2. End-to-end: only the matching run dirs are reaped; decoys survive.
    removed = runs_dir.delete_runs_for_sessions({target_sid})
    checks.append((removed == 2, f"removed count={removed} (want 2)"))
    checks.append(
        (not (root / "match-turn-1").exists() and not (root / "match-turn-2").exists(),
         "both matching run dirs reaped")
    )
    survivors = [d for d in root.iterdir() if d.is_dir()]
    checks.append((len(survivors) == _N_DECOYS, f"decoys survived ({len(survivors)})"))

    # 3. Idempotent: reaping an already-gone session is a no-op.
    again = runs_dir.delete_runs_for_sessions({target_sid})
    checks.append((again == 0, f"second delete is no-op (got {again})"))

    failed = [msg for ok, msg in checks if not ok]
    for ok, msg in checks:
        print(("PASS" if ok else "FAIL") + f": {msg}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
