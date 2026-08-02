"""Hermetic unit tests for the ``runs_dir`` delete/reap cluster.

Covers ``delete_runs_for_sessions`` (the security-critical "reap every run
dir attributed to a deleted session" path), ``_harvest_spawn_sid``, and
``reap_run_dir`` — the single owner of run-dir removal that both
session-delete and the per-provider age-prune route through.

What is proved here and nowhere else:
- Empty-sid / missing-runs-root early exits.
- The sqlite indexed fast path returns ALL run dirs for a sid (no
  one-per-session collapse) and spares decoys attributed to a survivor.
- The fast path STILL re-verifies each candidate against
  ``backend_state.json`` before reaping, so a stale index can never reap a
  dir the exhaustive walk would skip.
- The exhaustive walk fallback (index cold/None) reaps the same matches.
- Attribution honors ``persist_to or app_session_id`` exactly, including the
  empty-``persist_to`` fallback to ``app_session_id`` and ``persist_to``
  precedence over a differing ``app_session_id``.
- Corrupt / missing ``backend_state.json`` candidates are skipped.
- ``reap_run_dir`` removes the dir, harvests the spawn sid into the durable
  ledger BEFORE removal, and returns False (keeping the dir) on OSError.
- Idempotency and the removed-count log line.

All filesystem state lives under an isolated ``BETTER_AGENT_HOME`` tempdir
via ``_test_home.isolate``; nothing touches a real home.

Run with:
    cd backend && .venv/bin/python -m pytest scripts/test_runs_dir_delete_reap_unit.py -v
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runs-dir-delete-reap-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import runs_dir  # noqa: E402
import spawn_ledger  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset every runs_dir process-global cache and the on-disk runs root +
    spawn ledger before AND after each test, so order never matters."""
    for cache in (
        runs_dir._RUN_STATE_LEDGER_SEEN,
        runs_dir._RUN_STATE_RECENT_INDEX_CACHE,
        runs_dir._RUN_STATE_LEDGER_CACHE,
        runs_dir._RUN_STATE_RECENT_INDEX_INFLIGHT,
        runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT,
    ):
        cache.clear()
    _rm_runs_root()
    spawn_ledger._path().unlink(missing_ok=True)
    yield
    for cache in (
        runs_dir._RUN_STATE_LEDGER_SEEN,
        runs_dir._RUN_STATE_RECENT_INDEX_CACHE,
        runs_dir._RUN_STATE_LEDGER_CACHE,
        runs_dir._RUN_STATE_RECENT_INDEX_INFLIGHT,
        runs_dir._RUN_STATE_LEDGER_CACHE_REBUILD_INFLIGHT,
    ):
        cache.clear()
    _rm_runs_root()
    spawn_ledger._path().unlink(missing_ok=True)


def _rm_runs_root() -> None:
    root = runs_dir.runs_root()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


def _seed_run(
    run_id: str,
    *,
    persist_to: str | None,
    app_session_id: str,
    provider_sid: str | None = None,
    backend_state: dict | None = None,
    skip_backend_state: bool = False,
) -> Path:
    """Write a run dir whose ``state.json`` (indexed, carries the spawn sid)
    and ``backend_state.json`` (the re-verify target) attribute it to the
    given sessions. ``backend_state`` overrides the default JSON body when
    provided; ``skip_backend_state`` writes no backend_state.json at all."""
    run_dir = runs_dir.runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    provider_sid = provider_sid or f"prov-{run_id}"
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "session_id": provider_sid,
                "jsonl_path": str(run_dir / "stream.jsonl"),
                "app_session_id": app_session_id,
            }
        ),
        encoding="utf-8",
    )
    if not skip_backend_state:
        body = backend_state if backend_state is not None else {
            "persist_to": persist_to,
            "app_session_id": app_session_id,
        }
        (run_dir / "backend_state.json").write_text(
            json.dumps(body), encoding="utf-8"
        )
    return run_dir


# --- delete_runs_for_sessions: early exits ---------------------------------


def test_empty_sids_returns_zero_without_touching_filesystem():
    _seed_run("r1", persist_to="sid-A", app_session_id="sid-A")
    assert runs_dir.delete_runs_for_sessions(set()) == 0
    assert (runs_dir.runs_root() / "r1").exists()


def test_missing_runs_root_returns_zero():
    # _clean_state left runs_root nonexistent; do not create it.
    assert not runs_dir.runs_root().exists()
    assert runs_dir.delete_runs_for_sessions({"sid-A"}) == 0


# --- delete_runs_for_sessions: indexed fast path ---------------------------


def test_indexed_fast_path_reaps_all_matches_and_spares_decoys():
    target = "target-ba-session"
    survivor = "survivor-ba-session"
    for i in range(40):
        _seed_run(f"decoy-{i}", persist_to=survivor, app_session_id=survivor)
    # Two runs for the SAME deleted session: every run dir (one per turn) is
    # reaped, not a collapsed one-per-session.
    _seed_run("match-1", persist_to=target, app_session_id=target, provider_sid="prov-A")
    _seed_run("match-2", persist_to=target, app_session_id=target, provider_sid="prov-B")
    root = runs_dir.runs_root()

    # Prove we are on the indexed fast path, not the exhaustive walk.
    indexed = runs_dir._run_dirs_for_app_sessions_indexed(root, frozenset({target}))
    assert indexed is not None
    assert sorted(p.name for p in indexed) == ["match-1", "match-2"]

    removed = runs_dir.delete_runs_for_sessions({target})
    assert removed == 2
    assert not (root / "match-1").exists()
    assert not (root / "match-2").exists()
    survivors = sorted(d.name for d in root.iterdir() if d.is_dir())
    assert len(survivors) == 40
    assert all(name.startswith("decoy-") for name in survivors)


def test_indexed_candidate_with_mismatched_backend_state_is_skipped():
    """The fast path re-verifies via backend_state.json before reaping: a
    dir the index returns (state.json app_session_id matches) is NOT reaped
    when its backend_state.json points at a different session. This is the
    security guarantee that a stale index can never reap a dir the
    exhaustive walk would skip."""
    target = "target-ba-session"
    other = "other-ba-session"
    # state.json app_session_id == target (so the index returns it), but
    # backend_state.json persist_to == other (verify must reject it).
    _seed_run(
        "stale-index",
        persist_to=other,
        app_session_id=target,
        backend_state={"persist_to": other, "app_session_id": target},
    )

    indexed = runs_dir._run_dirs_for_app_sessions_indexed(
        runs_dir.runs_root(), frozenset({target})
    )
    assert indexed is not None
    assert [p.name for p in indexed] == ["stale-index"]

    assert runs_dir.delete_runs_for_sessions({target}) == 0
    assert (runs_dir.runs_root() / "stale-index").exists()


def test_exhaustive_fallback_reaps_matches(monkeypatch):
    """When the index is cold/stale (returns None) the exhaustive walk is the
    correctness backstop and reaps exactly the matching dirs."""
    target = "target-ba-session"
    survivor = "survivor-ba-session"
    _seed_run("decoy", persist_to=survivor, app_session_id=survivor)
    _seed_run("match-1", persist_to=target, app_session_id=target)
    _seed_run("match-2", persist_to=target, app_session_id=target)
    root = runs_dir.runs_root()

    # A non-dir entry in the runs root must be skipped by the walk.
    (root / "stray-file").write_text("nope", encoding="utf-8")

    monkeypatch.setattr(
        runs_dir, "_run_dirs_for_app_sessions_indexed", lambda _r, _s: None
    )
    removed = runs_dir.delete_runs_for_sessions({target})
    assert removed == 2
    assert not (root / "match-1").exists()
    assert not (root / "match-2").exists()
    assert (root / "decoy").exists()


# --- delete_runs_for_sessions: attribution semantics -----------------------


def test_persist_to_takes_precedence_over_app_session_id(monkeypatch):
    """The verify layer attributes by ``persist_to or app_session_id``: when
    they diverge, ``persist_to`` wins. The indexed fast path keys on
    ``state.json``'s ``app_session_id`` (which coincides with ``persist_to``
    on every real run dir), so to exercise the divergence we force the
    exhaustive walk, where every dir is visited and the verify layer is the
    sole arbiter."""
    target = "target-ba-session"
    _seed_run(
        "r1",
        persist_to=target,
        app_session_id="other-ba-session",
        backend_state={"persist_to": target, "app_session_id": "other-ba-session"},
    )
    root = runs_dir.runs_root()
    monkeypatch.setattr(
        runs_dir, "_run_dirs_for_app_sessions_indexed", lambda _r, _s: None
    )
    # Deleting by the differing app_session_id must NOT reap (persist_to wins).
    assert runs_dir.delete_runs_for_sessions({"other-ba-session"}) == 0
    assert (root / "r1").exists()
    # Deleting by persist_to reaps.
    assert runs_dir.delete_runs_for_sessions({target}) == 1
    assert not (root / "r1").exists()


def test_app_session_id_used_when_persist_to_is_empty():
    # `persist_to or app_session_id`: empty persist_to falls back to app_session_id.
    target = "target-ba-session"
    _seed_run(
        "r1",
        persist_to="",
        app_session_id=target,
        backend_state={"persist_to": "", "app_session_id": target},
    )
    assert runs_dir.delete_runs_for_sessions({target}) == 1
    assert not (runs_dir.runs_root() / "r1").exists()


# --- delete_runs_for_sessions: candidate rejection -------------------------


def test_corrupt_backend_state_json_skipped(monkeypatch):
    target = "target-ba-session"
    run_dir = _seed_run(
        "bad", persist_to=target, app_session_id=target, skip_backend_state=True
    )
    (run_dir / "backend_state.json").write_text("{ not json", encoding="utf-8")
    # Force the exhaustive walk so the corrupt candidate is actually visited.
    monkeypatch.setattr(
        runs_dir, "_run_dirs_for_app_sessions_indexed", lambda _r, _s: None
    )
    assert runs_dir.delete_runs_for_sessions({target}) == 0
    assert run_dir.exists()


def test_missing_backend_state_json_skipped(monkeypatch):
    target = "target-ba-session"
    run_dir = _seed_run(
        "nobstate", persist_to=target, app_session_id=target, skip_backend_state=True
    )
    monkeypatch.setattr(
        runs_dir, "_run_dirs_for_app_sessions_indexed", lambda _r, _s: None
    )
    assert runs_dir.delete_runs_for_sessions({target}) == 0
    assert run_dir.exists()


# --- delete_runs_for_sessions: idempotency + logging -----------------------


def test_idempotent_second_call_is_no_op():
    target = "target-ba-session"
    _seed_run("r1", persist_to=target, app_session_id=target)
    assert runs_dir.delete_runs_for_sessions({target}) == 1
    assert runs_dir.delete_runs_for_sessions({target}) == 0


def test_delete_logs_count_only_when_removed(caplog):
    target = "target-ba-session"
    _seed_run("r1", persist_to=target, app_session_id=target)
    with caplog.at_level(logging.INFO, logger="runs_dir"):
        runs_dir.delete_runs_for_sessions({target})
    assert any(
        "delete_runs_for_sessions: removed 1 run dir(s)" in rec.message
        for rec in caplog.records
    )

    # A zero-removal run must NOT emit the log line.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="runs_dir"):
        runs_dir.delete_runs_for_sessions({target})
    assert not any(
        "delete_runs_for_sessions: removed" in rec.message
        for rec in caplog.records
    )


# --- reap_run_dir + _harvest_spawn_sid -------------------------------------


def test_reap_run_dir_removes_dir_and_returns_true():
    run_dir = _seed_run("r1", persist_to="sid-A", app_session_id="sid-A")
    assert runs_dir.reap_run_dir(run_dir) is True
    assert not run_dir.exists()


def test_reap_run_dir_harvests_spawn_sid_before_removal():
    run_dir = _seed_run(
        "r1", persist_to="sid-A", app_session_id="sid-A", provider_sid="prov-special"
    )
    assert "prov-special" not in spawn_ledger.all_sids()
    assert runs_dir.reap_run_dir(run_dir) is True
    assert "prov-special" in spawn_ledger.all_sids()
    assert not run_dir.exists()


def test_reap_run_dir_oserror_returns_false_and_keeps_dir(monkeypatch):
    run_dir = _seed_run(
        "r1", persist_to="sid-A", app_session_id="sid-A", provider_sid="prov-keep"
    )

    def _boom(_path):  # noqa: ANN001
        raise OSError("simulated")

    monkeypatch.setattr(runs_dir.shutil, "rmtree", _boom)
    assert runs_dir.reap_run_dir(run_dir) is False
    # Dir survives the failed removal...
    assert run_dir.exists()
    # ...but the spawn sid was still harvested BEFORE the attempt.
    assert "prov-keep" in spawn_ledger.all_sids()


def test_harvest_spawn_sid_delegates_to_spawn_ledger(monkeypatch):
    recorded: list[Path] = []

    def _record(child: Path) -> None:
        recorded.append(child)

    monkeypatch.setattr(spawn_ledger, "record_run_dir", _record)
    run_dir = _seed_run("r1", persist_to="sid-A", app_session_id="sid-A")
    runs_dir._harvest_spawn_sid(run_dir)
    assert recorded == [run_dir]
