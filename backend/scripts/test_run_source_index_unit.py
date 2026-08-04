"""Unit coverage for run_source_index.py.

Maps native transcript paths -> session turn-source by cross-referencing BA's
per-run records (runs/<id>/state.json + input.json). Pure logic: classification
precedence, per-run parsing, the per-path aggregation, and a TTL-cached map.

Home is engaged to an isolated tempdir at import time so runs_root() points at
a throwaway tree; each test wipes that tree and resets the module cache so the
runs on disk are exactly what the test installed.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import _test_home

_test_home.isolate("run-source-index-test-")

import run_source_index as rsi  # noqa: E402
import runs_dir  # noqa: E402


def _write_run(
    run_id: str,
    *,
    jsonl_path: str = "/t/a.jsonl",
    source: str | None = None,
    fork: bool = False,
    working_mode: str | None = None,
) -> Path:
    """Install a well-formed run dir under the engaged runs root."""
    root = runs_dir.runs_root()
    root.mkdir(parents=True, exist_ok=True)
    rd = root / run_id
    rd.mkdir(parents=True, exist_ok=True)
    inp: dict = {}
    if source is not None:
        inp["source"] = source
    if fork:
        inp["fork"] = True
    if working_mode:
        inp["working_mode"] = working_mode
    (rd / "input.json").write_text(json.dumps(inp), encoding="utf-8")
    (rd / "state.json").write_text(
        json.dumps({"jsonl_path": jsonl_path, "started_at": 1}), encoding="utf-8"
    )
    return rd


def _reset_cache() -> None:
    """Force path_source_map to rebuild on its next call."""
    rsi._CACHE["data"] = None
    rsi._CACHE["built_at"] = 0.0


@pytest.fixture
def clean_runs() -> Path:
    """Empty runs root + reset module cache so path_source_map rebuilds."""
    root = runs_dir.runs_root()
    if root.exists():
        for entry in root.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    root.mkdir(parents=True, exist_ok=True)
    _reset_cache()
    return root


# --------------------------------------------------------------------------- #
# _run_kind
# --------------------------------------------------------------------------- #
def test_run_kind_working_mode_is_internal() -> None:
    assert rsi._run_kind({"working_mode": "something"}) == rsi.INTERNAL


def test_run_kind_fork_takes_precedence_over_source() -> None:
    assert rsi._run_kind({"fork": True, "source": "team_ask"}) == rsi.FORK


def test_run_kind_non_direct_source_is_team() -> None:
    assert rsi._run_kind({"source": "delegate_task"}) == rsi.TEAM


def test_run_kind_explicit_direct_is_direct_user() -> None:
    assert rsi._run_kind({"source": "direct"}) == rsi.DIRECT_USER


def test_run_kind_missing_source_is_direct_user() -> None:
    assert rsi._run_kind({}) == rsi.DIRECT_USER


def test_run_kind_whitespace_direct_is_direct_user() -> None:
    assert rsi._run_kind({"source": "  direct  "}) == rsi.DIRECT_USER


def test_run_kind_whitespace_non_direct_is_team() -> None:
    assert rsi._run_kind({"source": "  team_ask  "}) == rsi.TEAM


# --------------------------------------------------------------------------- #
# _classify
# --------------------------------------------------------------------------- #
def test_classify_any_direct_wins() -> None:
    assert rsi._classify([rsi.FORK, rsi.DIRECT_USER, rsi.TEAM]) == rsi.DIRECT_USER


def test_classify_internal_without_direct() -> None:
    assert rsi._classify([rsi.INTERNAL, rsi.TEAM]) == rsi.INTERNAL


def test_classify_fork_without_direct_or_internal() -> None:
    assert rsi._classify([rsi.FORK, rsi.TEAM]) == rsi.FORK


def test_classify_only_team_falls_through_to_team() -> None:
    assert rsi._classify([rsi.TEAM]) == rsi.TEAM


def test_classify_empty_kinds_defaults_to_team() -> None:
    assert rsi._classify([]) == rsi.TEAM


# --------------------------------------------------------------------------- #
# _read_one
# --------------------------------------------------------------------------- #
def test_read_one_parses_well_formed_run(clean_runs: Path) -> None:
    rd = _write_run("r1", jsonl_path="/t/a.jsonl", source="direct")
    assert rsi._read_one(rd) == ("/t/a.jsonl", rsi.DIRECT_USER)


def test_read_one_returns_none_when_input_json_missing(clean_runs: Path) -> None:
    rd = _write_run("r1")
    (rd / "input.json").unlink()
    assert rsi._read_one(rd) is None


def test_read_one_returns_none_when_state_json_missing(clean_runs: Path) -> None:
    rd = _write_run("r1")
    (rd / "state.json").unlink()
    assert rsi._read_one(rd) is None


def test_read_one_returns_none_on_malformed_json(clean_runs: Path) -> None:
    rd = _write_run("r1")
    (rd / "input.json").write_text("{not json", encoding="utf-8")
    assert rsi._read_one(rd) is None


def test_read_one_returns_none_when_jsonl_path_absent(clean_runs: Path) -> None:
    rd = _write_run("r1")
    (rd / "state.json").write_text(json.dumps({}), encoding="utf-8")
    assert rsi._read_one(rd) is None


# --------------------------------------------------------------------------- #
# _build
# --------------------------------------------------------------------------- #
def test_build_empty_when_runs_root_missing() -> None:
    root = runs_dir.runs_root()
    if root.exists():
        shutil.rmtree(root)
    _reset_cache()
    assert rsi._build() == {}


def test_build_skips_non_directory_entries(clean_runs: Path) -> None:
    (clean_runs / "stray.txt").write_text("x", encoding="utf-8")
    _write_run("r1", jsonl_path="/t/a.jsonl", source="direct")
    assert rsi._build() == {"/t/a.jsonl": rsi.DIRECT_USER}


def test_build_skips_unusable_run_dirs(clean_runs: Path) -> None:
    _write_run("r1", jsonl_path="/t/a.jsonl", source="direct")
    bad = clean_runs / "bad"
    bad.mkdir()
    (bad / "input.json").write_text("{not json", encoding="utf-8")
    assert rsi._build() == {"/t/a.jsonl": rsi.DIRECT_USER}


def test_build_aggregates_multiple_runs_on_same_path_by_precedence(
    clean_runs: Path,
) -> None:
    # Two BA-injected runs on the same transcript, plus one direct run: the
    # direct run wins the per-path classification.
    _write_run("r1", jsonl_path="/t/shared.jsonl", source="team_ask")
    _write_run("r2", jsonl_path="/t/shared.jsonl", fork=True)
    _write_run("r3", jsonl_path="/t/shared.jsonl", source="direct")
    assert rsi._build() == {"/t/shared.jsonl": rsi.DIRECT_USER}


def test_build_classifies_all_injected_session_as_non_user(clean_runs: Path) -> None:
    _write_run("r1", jsonl_path="/t/fork.jsonl", fork=True)
    _write_run("r2", jsonl_path="/t/team.jsonl", source="delegate_task")
    _write_run("r3", jsonl_path="/t/internal.jsonl", working_mode="autopilot")
    assert rsi._build() == {
        "/t/fork.jsonl": rsi.FORK,
        "/t/team.jsonl": rsi.TEAM,
        "/t/internal.jsonl": rsi.INTERNAL,
    }


# --------------------------------------------------------------------------- #
# path_source_map (TTL cache)
# --------------------------------------------------------------------------- #
def test_path_source_map_builds_and_caches_identity(clean_runs: Path) -> None:
    _write_run("r1", jsonl_path="/t/a.jsonl", source="direct")
    first = rsi.path_source_map()
    second = rsi.path_source_map()
    assert first == {"/t/a.jsonl": rsi.DIRECT_USER}
    # Within TTL the cached dict object is returned unchanged.
    assert second is first


def test_path_source_map_rebuilds_after_ttl_expiry(clean_runs: Path) -> None:
    _write_run("r1", jsonl_path="/t/a.jsonl", source="direct")
    first = rsi.path_source_map()
    assert first == {"/t/a.jsonl": rsi.DIRECT_USER}

    # New run appears on disk, and the cache is aged past its TTL.
    _write_run("r2", jsonl_path="/t/b.jsonl", fork=True)
    rsi._CACHE["built_at"] = time.monotonic() - rsi._TTL_SECONDS - 1

    rebuilt = rsi.path_source_map()
    assert rebuilt == {
        "/t/a.jsonl": rsi.DIRECT_USER,
        "/t/b.jsonl": rsi.FORK,
    }
    assert rebuilt is not first


# --------------------------------------------------------------------------- #
# classify_path
# --------------------------------------------------------------------------- #
def test_classify_path_none_is_external() -> None:
    assert rsi.classify_path(None) == rsi.EXTERNAL


def test_classify_path_empty_is_external() -> None:
    assert rsi.classify_path("") == rsi.EXTERNAL


def test_classify_path_absent_is_external(clean_runs: Path) -> None:
    _write_run("r1", jsonl_path="/t/a.jsonl", source="direct")
    assert rsi.classify_path("/t/never-seen.jsonl") == rsi.EXTERNAL


def test_classify_path_uses_explicit_source_map() -> None:
    explicit = {"/t/x.jsonl": rsi.TEAM}
    assert rsi.classify_path("/t/x.jsonl", explicit) == rsi.TEAM
    assert rsi.classify_path("/t/missing.jsonl", explicit) == rsi.EXTERNAL


def test_classify_path_reads_live_map(clean_runs: Path) -> None:
    _write_run("r1", jsonl_path="/t/a.jsonl", fork=True)
    assert rsi.classify_path("/t/a.jsonl") == rsi.FORK


# --------------------------------------------------------------------------- #
# is_user_source
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value",
    [None, "", rsi.DIRECT_USER, rsi.EXTERNAL],
)
def test_is_user_source_true_for_user(value: str | None) -> None:
    assert rsi.is_user_source(value) is True


@pytest.mark.parametrize("value", [rsi.INTERNAL, rsi.FORK, rsi.TEAM])
def test_is_user_source_false_for_injected(value: str) -> None:
    assert rsi.is_user_source(value) is False
