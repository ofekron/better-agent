"""Unit tests for ``spawn_ledger`` — the durable append-only ledger of
BA-spawned provider session_ids.

Isolation: ``spawn_ledger`` resolves ``ba_home()`` at every call via its
module-global reference, so each test monkeypatches ``spawn_ledger.ba_home``
to a fresh tmp dir. The in-process dedup set
``_recorded_this_process`` is cleared per test so ``record_discovered``
behaves as if on a fresh backend. No real home is ever touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import runs_dir
import spawn_ledger


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(spawn_ledger, "ba_home", lambda: tmp_path)
    spawn_ledger._recorded_this_process.clear()
    yield


def _ledger_path() -> Path:
    return spawn_ledger._path()


# --- add / add_many ------------------------------------------------------


@pytest.mark.parametrize("bad", [None, 123, ""])
def test_add_noop_on_empty_or_non_str(bad):
    assert spawn_ledger.add(bad) is True
    assert not _ledger_path().exists()


def test_add_appends_valid_sid():
    assert spawn_ledger.add("sid-1") is True
    assert _ledger_path().read_text(encoding="utf-8") == "sid-1\n"


def test_add_many_noop_when_all_invalid():
    assert spawn_ledger.add_many([None, "", 5]) is True
    assert not _ledger_path().exists()


def test_add_many_filters_invalid_keeps_valid():
    assert spawn_ledger.add_many(["a", None, "", "b"]) is True
    # invalid entries filtered, valid ones appended in order
    assert _ledger_path().read_text(encoding="utf-8") == "a\nb\n"


def test_add_many_appends_to_existing():
    spawn_ledger.add("first")
    spawn_ledger.add_many(["second", "third"])
    assert _ledger_path().read_text(encoding="utf-8") == "first\nsecond\nthird\n"


def test_add_many_returns_false_on_oserror(tmp_path, monkeypatch):
    # ba_home pointing at a regular file makes ``_path().parent.mkdir`` raise
    # FileExistsError (an OSError): the parent exists but is not a directory,
    # which even root cannot override. Portable on host and Docker(root).
    file_home = tmp_path / "iam-a-file"
    file_home.write_text("x", encoding="utf-8")
    monkeypatch.setattr(spawn_ledger, "ba_home", lambda: file_home)
    assert spawn_ledger.add_many(["sid"]) is False


# --- all_sids ------------------------------------------------------------


def test_all_sids_empty_when_no_file():
    assert spawn_ledger.all_sids() == set()


def test_all_sids_reads_dedupes_strips():
    _ledger_path().parent.mkdir(parents=True, exist_ok=True)
    _ledger_path().write_text("a\nb\n  a  \n\nc\n\n", encoding="utf-8")
    # deduped + stripped; blank lines dropped
    assert spawn_ledger.all_sids() == {"a", "b", "c"}


def test_all_sids_returns_empty_on_oserror(tmp_path, monkeypatch):
    # ledger path exists but is a directory -> read_text raises IsADirectoryError
    _ledger_path().parent.mkdir(parents=True, exist_ok=True)
    _ledger_path().mkdir()
    assert spawn_ledger.all_sids() == set()


# --- record_discovered ---------------------------------------------------


def test_record_discovered_ignores_non_str():
    spawn_ledger.record_discovered(None)  # type: ignore[arg-type]
    spawn_ledger.record_discovered("")
    assert not _ledger_path().exists()
    assert spawn_ledger._recorded_this_process == set()


def test_record_discovered_appends_once_per_process():
    spawn_ledger.record_discovered("sid-x")
    spawn_ledger.record_discovered("sid-x")
    spawn_ledger.record_discovered("sid-x")
    assert _ledger_path().read_text(encoding="utf-8") == "sid-x\n"
    assert spawn_ledger._recorded_this_process == {"sid-x"}


def test_record_discovered_distinct_sids_each_appended():
    spawn_ledger.record_discovered("a")
    spawn_ledger.record_discovered("b")
    assert _ledger_path().read_text(encoding="utf-8") == "a\nb\n"


# --- _sid_from_run_dir / record_run_dir ---------------------------------


def test_sid_from_run_dir_state_json():
    d = _ledger_path().parent / "run1"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")
    assert spawn_ledger._sid_from_run_dir(d) == "s1"


def test_sid_from_run_dir_backend_state_fallback():
    d = _ledger_path().parent / "run2"
    d.mkdir(parents=True)
    # state.json present but no usable session_id -> fall through
    (d / "state.json").write_text(json.dumps({"session_id": ""}), encoding="utf-8")
    (d / "backend_state.json").write_text(
        json.dumps({"session_id": "s2"}), encoding="utf-8"
    )
    assert spawn_ledger._sid_from_run_dir(d) == "s2"


def test_sid_from_run_dir_complete_json_fallback():
    d = _ledger_path().parent / "run3"
    d.mkdir(parents=True)
    (d / "complete.json").write_text(json.dumps({"session_id": "s3"}), encoding="utf-8")
    assert spawn_ledger._sid_from_run_dir(d) == "s3"


def test_sid_from_run_dir_malformed_json_skipped():
    d = _ledger_path().parent / "run4"
    d.mkdir(parents=True)
    (d / "state.json").write_text("{not json", encoding="utf-8")
    (d / "complete.json").write_text(json.dumps({"session_id": "s4"}), encoding="utf-8")
    assert spawn_ledger._sid_from_run_dir(d) == "s4"


def test_sid_from_run_dir_non_dict_json():
    d = _ledger_path().parent / "run5"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert spawn_ledger._sid_from_run_dir(d) == ""


def test_sid_from_run_dir_non_str_session_id():
    d = _ledger_path().parent / "run6"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"session_id": 123}), encoding="utf-8")
    assert spawn_ledger._sid_from_run_dir(d) == ""


def test_sid_from_run_dir_no_files():
    d = _ledger_path().parent / "run7"
    d.mkdir(parents=True)
    assert spawn_ledger._sid_from_run_dir(d) == ""


def test_record_run_dir_appends_extracted_sid():
    d = _ledger_path().parent / "run8"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"session_id": "rr"}), encoding="utf-8")
    spawn_ledger.record_run_dir(d)
    assert spawn_ledger.all_sids() == {"rr"}


def test_record_run_dir_no_sid_is_noop():
    d = _ledger_path().parent / "run9"
    d.mkdir(parents=True)
    spawn_ledger.record_run_dir(d)
    assert not _ledger_path().exists()


# --- bootstrap_from_run_dirs_once ----------------------------------------


def _make_run(root: Path, name: str, sid: str | None) -> Path:
    child = root / name
    child.mkdir(parents=True)
    if sid is not None:
        (child / "state.json").write_text(
            json.dumps({"session_id": sid}), encoding="utf-8"
        )
    return child


def test_bootstrap_early_return_when_marker_exists(monkeypatch):
    spawn_ledger._bootstrap_marker_path().parent.mkdir(parents=True, exist_ok=True)
    spawn_ledger._bootstrap_marker_path().write_text("1\n", encoding="utf-8")

    def _fail_if_called():
        raise AssertionError("runs_root must not be consulted once bootstrapped")

    monkeypatch.setattr(runs_dir, "runs_root", _fail_if_called)
    spawn_ledger.bootstrap_from_run_dirs_once()  # marker present -> early return


def test_bootstrap_writes_marker_when_no_sids(tmp_path, monkeypatch):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(runs_dir, "runs_root", lambda: runs_root)
    spawn_ledger.bootstrap_from_run_dirs_once()
    assert spawn_ledger._bootstrap_marker_path().exists()
    assert spawn_ledger.all_sids() == set()


def test_bootstrap_ingests_run_dirs_and_writes_marker(tmp_path, monkeypatch):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    _make_run(runs_root, "run-a", "aid")
    _make_run(runs_root, "run-b", "bid")
    _make_run(runs_root, "run-empty", None)  # no session_id -> skipped
    # a non-directory entry is also skipped (child.is_dir() guard)
    (runs_root / "loose-file").write_text("junk", encoding="utf-8")
    monkeypatch.setattr(runs_dir, "runs_root", lambda: runs_root)
    spawn_ledger.bootstrap_from_run_dirs_once()
    assert spawn_ledger.all_sids() == {"aid", "bid"}
    assert spawn_ledger._bootstrap_marker_path().exists()


def test_bootstrap_idempotent_second_call_no_duplicates(tmp_path, monkeypatch):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    _make_run(runs_root, "run-a", "aid")
    monkeypatch.setattr(runs_dir, "runs_root", lambda: runs_root)
    spawn_ledger.bootstrap_from_run_dirs_once()
    spawn_ledger.bootstrap_from_run_dirs_once()
    assert spawn_ledger.all_sids() == {"aid"}


def test_bootstrap_missing_runs_root_writes_marker(tmp_path, monkeypatch):
    runs_root = tmp_path / "absent"
    monkeypatch.setattr(runs_dir, "runs_root", lambda: runs_root)
    spawn_ledger.bootstrap_from_run_dirs_once()
    assert spawn_ledger._bootstrap_marker_path().exists()
    assert spawn_ledger.all_sids() == set()


def test_bootstrap_skips_marker_when_add_fails(tmp_path, monkeypatch):
    # sids are discovered but add_many fails (ba_home is a regular file ->
    # FileExistsError on mkdir), so bootstrap returns WITHOUT writing marker.
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    _make_run(runs_root, "run-a", "aid")
    monkeypatch.setattr(runs_dir, "runs_root", lambda: runs_root)
    file_home = tmp_path / "file-home"
    file_home.write_text("x", encoding="utf-8")
    monkeypatch.setattr(spawn_ledger, "ba_home", lambda: file_home)
    spawn_ledger.bootstrap_from_run_dirs_once()
    assert not spawn_ledger._bootstrap_marker_path().exists()


def test_bootstrap_swallows_oserror(tmp_path, monkeypatch):
    # ba_home is a regular file; no sids are discovered so execution reaches
    # marker write, whose parent mkdir raises FileExistsError -> except OSError.
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(runs_dir, "runs_root", lambda: runs_root)
    file_home = tmp_path / "file-home2"
    file_home.write_text("x", encoding="utf-8")
    monkeypatch.setattr(spawn_ledger, "ba_home", lambda: file_home)
    # must not raise
    spawn_ledger.bootstrap_from_run_dirs_once()
    assert not spawn_ledger._bootstrap_marker_path().exists()


# --- cross-cutting / persistence shape -----------------------------------


def test_ledger_is_append_only_line_atomic():
    spawn_ledger.add_many(["x", "y", "z"])
    # one sid per line, newline-terminated, no extra framing
    assert _ledger_path().read_text(encoding="utf-8") == "x\ny\nz\n"


def test_module_surface_exposes_documented_callables():
    for name in ("add", "add_many", "all_sids", "record_discovered",
                 "record_run_dir", "bootstrap_from_run_dirs_once"):
        assert callable(getattr(spawn_ledger, name))
