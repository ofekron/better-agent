"""Hermetic unit tests for the defensive error/edge branches in ``runs_dir``
the happy-path suite leaves out. Every assertion is fail-on-bug: removing the
guarded branch it targets makes the test raise instead of pass.

Three clusters, all deterministic:

  * ``read_best_complete`` per-turn fallback (lines 1348-1354):
      - ``turns/<sub>/`` with no ``complete.json`` -> ``cj.stat()`` raises
        ``OSError`` -> the entry is skipped and the search continues to a
        valid sibling (1348-1349). Without ``except OSError`` the fault
        escapes and the valid payload is never found.
      - ``turns/<sub>/complete.json`` holding unparseable JSON -> ``json.loads``
        raises ``ValueError`` -> skipped, function falls through to ``None``
        (1353-1354). Without the ``except`` the fault escapes.

  * ``cli_liveness_corroborated`` input-coercion branches (lines 1597-1611):
      - ``jsonl_inode is None`` -> the inode check is skipped and freshness
        falls to mtime alone (1597->1606). Without the ``is not None`` guard
        ``int(None)`` raises ``TypeError``.
      - a non-int ``jsonl_inode`` -> ``int()`` raises ``ValueError`` -> fail
        closed to ``False`` (1601-1602). Without ``except`` it escapes.
      - a non-int ``processed_byte`` (with a matching inode) -> ``int()``
        raises ``ValueError`` -> swallowed, freshness falls to mtime (1610-1611).
        Without ``except`` it escapes.

  * ``delete_runs_for_sessions`` reap-failure branch (line 1529->1522): when
    ``reap_run_dir`` returns ``False`` (``shutil.rmtree`` raised ``OSError``),
    the failed dir is NOT counted and the loop continues to the next
    candidate. A correct run therefore reports ``removed == 0`` and leaves the
    dirs on disk. Reusing the proven ``monkeypatch``-of-``runs_dir.shutil.rmtree``
    technique from ``test_runs_dir_delete_reap_unit.py``.

All filesystem state lives under an isolated ``BETTER_AGENT_HOME`` tempdir via
``_test_home.isolate``; nothing touches a real home. Run dirs for the reap test
are seeded under ``runs_dir.runs_root()`` exactly like the delete-for-sessions
script. Run with:

    cd backend && .venv/bin/python -m pytest scripts/test_runs_dir_defensive_branches_unit.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-runs-dir-defensive-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import runs_dir  # noqa: E402
from runs_dir import (  # noqa: E402
    cli_liveness_corroborated,
    delete_runs_for_sessions,
    read_best_complete,
)


@pytest.fixture
def fresh_root():
    path = Path(tempfile.mkdtemp(prefix="bc-rd-defensive-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _seed_run(root: Path, run_id: str, app_sid: str) -> Path:
    """Write a run dir with backend_state.json attributing it to app_sid."""
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "session_id": f"prov-{run_id}",
                "jsonl_path": str(run_dir / "stream.jsonl"),
                "app_session_id": app_sid,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "backend_state.json").write_text(
        json.dumps({"persist_to": app_sid, "app_session_id": app_sid}),
        encoding="utf-8",
    )
    return run_dir


# --------------------------------------------------------------------------- #
# read_best_complete: per-turn fallback error branches (1348-1354)
# --------------------------------------------------------------------------- #


def test_read_best_complete_skips_turn_missing_complete_json(fresh_root):
    """Lines 1348-1349: a ``turns/<sub>/`` with NO ``complete.json`` makes
    ``cj.stat()`` raise ``OSError``; the except skips it and the search
    continues to the valid sibling. Without ``except OSError`` the fault
    escapes and the valid payload is lost. Run-level complete.json is absent so
    execution reaches the turns walk."""
    run_dir = fresh_root / "run-1"
    (run_dir / "turns" / "empty-turn").mkdir(parents=True)  # no complete.json
    valid_payload = {"success": True, "session_id": "good"}
    good = run_dir / "turns" / "good-turn" / "complete.json"
    good.parent.mkdir(parents=True)
    good.write_text(json.dumps(valid_payload), encoding="utf-8")

    assert read_best_complete(run_dir) == valid_payload


def test_read_best_complete_unparseable_turn_complete_returns_none(fresh_root):
    """Lines 1353-1354: a ``turns/<sub>/complete.json`` holding unparseable
    JSON makes ``json.loads`` raise ``ValueError``; the except skips it and,
    with no other candidate, the function falls through to ``None``. Without
    the ``except`` the fault escapes. No run-level file, no valid sibling."""
    run_dir = fresh_root / "run-1"
    bad = run_dir / "turns" / "bad-turn" / "complete.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{not json", encoding="utf-8")  # stat ok, parse fails

    assert read_best_complete(run_dir) is None


# --------------------------------------------------------------------------- #
# cli_liveness_corroborated: input-coercion branches (1597-1611)
# --------------------------------------------------------------------------- #
#
# A live pid is required to reach the jsonl branches. The test process itself
# is guaranteed alive for the duration of the call, so ``os.getpid()`` is a
# deterministic, non-flaky live pid.


def test_cli_liveness_inode_none_falls_to_mtime(fresh_root):
    """Branch 1597->1606: when ``jsonl_inode`` is ``None`` the inode check is
    skipped and freshness falls to mtime alone. A just-written jsonl is fresh,
    so the result is ``True``. Removing the ``if jsonl_inode is not None:``
    guard makes ``int(None)`` raise ``TypeError``."""
    jsonl = fresh_root / "stream.jsonl"
    jsonl.write_text("{}", encoding="utf-8")  # fresh by construction

    assert cli_liveness_corroborated(os.getpid(), str(jsonl), None, None) is True


def test_cli_liveness_non_int_inode_returns_false(fresh_root):
    """Lines 1601-1602: a ``jsonl_inode`` that is not int-coercible makes
    ``int(jsonl_inode)`` raise ``ValueError``; the except fails closed to
    ``False``. Without the ``except`` the fault escapes."""
    jsonl = fresh_root / "stream.jsonl"
    jsonl.write_text("{}", encoding="utf-8")

    assert cli_liveness_corroborated(os.getpid(), str(jsonl), "not-an-int", None) is False


def test_cli_liveness_non_int_processed_byte_falls_to_mtime(fresh_root):
    """Lines 1610-1611: with a MATCHING inode but a ``processed_byte`` that is
    not int-coercible, ``int(processed_byte)`` raises ``ValueError``; the
    except swallows it and freshness falls to mtime. A just-written jsonl is
    fresh, so the result is ``True``. Without the ``except`` the fault escapes."""
    jsonl = fresh_root / "stream.jsonl"
    jsonl.write_text("{}", encoding="utf-8")
    real_inode = jsonl.stat().st_ino  # matches -> passes the inode check

    assert (
        cli_liveness_corroborated(os.getpid(), str(jsonl), real_inode, "garbage")
        is True
    )


# --------------------------------------------------------------------------- #
# delete_runs_for_sessions: reap-failure keeps the loop going (1529->1522)
# --------------------------------------------------------------------------- #


def test_delete_runs_for_sessions_reap_failure_not_counted(monkeypatch):
    """Branch 1529->1522: when ``reap_run_dir`` returns ``False`` (``shutil.rmtree``
    raised ``OSError``) the failed dir is NOT counted and the loop continues.
    Two matching runs both fail to reap, so ``removed == 0`` and BOTH dirs
    survive. If the ``if reap_run_dir(child):`` guard were dropped (counting
    unconditionally), ``removed`` would be 2 while the dirs still exist -> the
    combined assertion catches it. Candidates are injected directly so the
    run-state index does not influence the result."""
    root = runs_dir.runs_root()
    root.mkdir(parents=True, exist_ok=True)
    target = "target-ba-session"
    a = _seed_run(root, "match-1", target)
    b = _seed_run(root, "match-2", target)

    def _boom(_path, *args, **kwargs):
        raise OSError("simulated reap failure")

    monkeypatch.setattr(runs_dir.shutil, "rmtree", _boom)
    monkeypatch.setattr(
        runs_dir, "_run_dirs_for_app_sessions_indexed", lambda _root, _sids: [a, b]
    )

    removed = delete_runs_for_sessions({target})

    assert removed == 0
    assert a.exists() and b.exists()  # both survived the failed reap


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
