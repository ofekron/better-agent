"""Unit coverage for native_index_manager.

The manager is a ThreadLoopHost subclass. Its async methods delegate to
``run_blocking``/``run``; when the host is NOT started (``loop is None``)
those execute the work INLINE on the caller's loop via the default
executor. So every async method is exercised with the host left unstarted
and the dynamically-imported ``native_import`` swapped for a seam mock via
``sys.modules``. ``_start_index_worker``/``on_stopping`` and the
``_index_disabled`` gate are covered directly.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import native_index_manager as nim
from thread_loop_host import ThreadLoopHost


def _native_import_mock() -> MagicMock:
    mod = MagicMock()
    mod.start_import = MagicMock(return_value={"started": True})
    mod.get_status = MagicMock(return_value={"state": "idle"})
    mod.loaded_project_paths = MagicMock(return_value=["/a", "/b"])
    mod.resume_if_interrupted = MagicMock()
    mod.count_native_sessions_async = AsyncMock(return_value={"claude": 5})
    return mod


# --- _index_disabled ---------------------------------------------------------

def test_index_disabled_true_when_test_mode(monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_TEST_MODE", "1")
    assert nim._index_disabled() is True


def test_index_disabled_false_when_unset(monkeypatch):
    monkeypatch.delenv("BETTER_AGENT_TEST_MODE", raising=False)
    assert nim._index_disabled() is False


# --- __init__ ----------------------------------------------------------------

def test_init_binds_name_and_executor_workers():
    m = nim.NativeIndexManager()
    assert m._name == "native-index"
    assert m._executor_workers == 2
    assert nim.manager.__class__ is nim.NativeIndexManager


# --- start / scheduling ------------------------------------------------------

def test_start_returns_early_when_loop_is_none(monkeypatch):
    """super().start() a no-op => loop stays None => no scheduling."""
    m = nim.NativeIndexManager()
    monkeypatch.setattr(ThreadLoopHost, "start", lambda self: None)
    m._loop = None
    m.start()  # must return cleanly without scheduling.
    # Nothing to assert on a None loop; the value is that no AttributeError raised.


def test_start_schedules_index_worker_on_loop(monkeypatch):
    m = nim.NativeIndexManager()
    monkeypatch.setattr(ThreadLoopHost, "start", lambda self: None)
    fake_loop = MagicMock()
    m._loop = fake_loop
    m.start()
    fake_loop.call_soon_threadsafe.assert_called_once()
    scheduled = fake_loop.call_soon_threadsafe.call_args.args[0]
    # The scheduled callable drives run_in_executor with the worker.
    scheduled()
    fake_loop.run_in_executor.assert_called_once()
    # Bound methods are fresh objects per attribute access; compare by value.
    assert fake_loop.run_in_executor.call_args.args[1] == m._start_index_worker


# --- _start_index_worker -----------------------------------------------------

def test_start_index_worker_skips_when_test_mode(monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_TEST_MODE", "1")
    m = nim.NativeIndexManager()
    with patch.dict(sys.modules, {"native_transcript_index": MagicMock()}):
        m._start_index_worker()  # early return; nothing imported.


def test_start_index_worker_ensures_started(monkeypatch):
    monkeypatch.delenv("BETTER_AGENT_TEST_MODE", raising=False)
    nti = MagicMock()
    m = nim.NativeIndexManager()
    with patch.dict(sys.modules, {"native_transcript_index": nti}):
        m._start_index_worker()
    nti.ensure_started.assert_called_once_with()


def test_start_index_worker_swallows_start_failure(monkeypatch):
    monkeypatch.delenv("BETTER_AGENT_TEST_MODE", raising=False)
    nti = MagicMock()
    nti.ensure_started.side_effect = RuntimeError("boom")
    m = nim.NativeIndexManager()
    with patch.dict(sys.modules, {"native_transcript_index": nti}):
        m._start_index_worker()  # must not raise.


# --- on_stopping -------------------------------------------------------------

def test_on_stopping_skips_when_test_mode(monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_TEST_MODE", "1")
    m = nim.NativeIndexManager()
    with patch.dict(sys.modules, {"native_transcript_index": MagicMock()}):
        m.on_stopping()


def test_on_stopping_shuts_down(monkeypatch):
    monkeypatch.delenv("BETTER_AGENT_TEST_MODE", raising=False)
    nti = MagicMock()
    m = nim.NativeIndexManager()
    with patch.dict(sys.modules, {"native_transcript_index": nti}):
        m.on_stopping()
    nti.shutdown.assert_called_once_with()


def test_on_stopping_swallows_shutdown_failure(monkeypatch):
    monkeypatch.delenv("BETTER_AGENT_TEST_MODE", raising=False)
    nti = MagicMock()
    nti.shutdown.side_effect = RuntimeError("boom")
    m = nim.NativeIndexManager()
    with patch.dict(sys.modules, {"native_transcript_index": nti}):
        m.on_stopping()  # must not raise.


# --- start_import ------------------------------------------------------------

def test_start_import_forwards_args_and_returns_result():
    m = nim.NativeIndexManager()
    ni = _native_import_mock()
    with patch.dict(sys.modules, {"native_import": ni}):
        result = asyncio.run(m.start_import(["claude"], 7, ["/proj"]))
    assert result == {"started": True}
    ni.start_import.assert_called_once_with(["claude"], 7, ["/proj"])


def test_start_import_none_args_pass_through():
    m = nim.NativeIndexManager()
    ni = _native_import_mock()
    with patch.dict(sys.modules, {"native_import": ni}):
        result = asyncio.run(m.start_import(None, None, None))
    assert result == {"started": True}
    ni.start_import.assert_called_once_with(None, None, None)


# --- import_status -----------------------------------------------------------

def test_import_status_returns_native_status():
    m = nim.NativeIndexManager()
    ni = _native_import_mock()
    with patch.dict(sys.modules, {"native_import": ni}):
        result = asyncio.run(m.import_status())
    assert result == {"state": "idle"}
    ni.get_status.assert_called_once_with()


# --- import_summary ----------------------------------------------------------

def test_import_summary_all_projects_passes_none_paths():
    m = nim.NativeIndexManager()
    ni = _native_import_mock()
    with patch.dict(sys.modules, {"native_import": ni}):
        result = asyncio.run(m.import_summary(["claude"], all_projects=True))
    assert result == {"claude": 5}
    ni.count_native_sessions_async.assert_awaited_once_with(["claude"], None)
    ni.loaded_project_paths.assert_not_called()


def test_import_summary_scoped_projects_loads_paths():
    m = nim.NativeIndexManager()
    ni = _native_import_mock()
    with patch.dict(sys.modules, {"native_import": ni}):
        result = asyncio.run(m.import_summary(["codex"], all_projects=False))
    assert result == {"claude": 5}
    ni.loaded_project_paths.assert_called_once_with()
    ni.count_native_sessions_async.assert_awaited_once_with(["codex"], ["/a", "/b"])


# --- resume_interrupted_import -----------------------------------------------

def test_resume_interrupted_import_runs_resume(monkeypatch):
    m = nim.NativeIndexManager()
    ni = _native_import_mock()
    with patch.dict(sys.modules, {"native_import": ni}):
        asyncio.run(m.resume_interrupted_import())
    ni.resume_if_interrupted.assert_called_once_with()


def test_resume_interrupted_import_swallows_failure():
    m = nim.NativeIndexManager()
    ni = _native_import_mock()
    ni.resume_if_interrupted.side_effect = RuntimeError("boom")
    with patch.dict(sys.modules, {"native_import": ni}):
        asyncio.run(m.resume_interrupted_import())  # must not raise.


# --- loaded_project_paths ----------------------------------------------------

def test_loaded_project_paths_returns_native_paths():
    m = nim.NativeIndexManager()
    ni = _native_import_mock()
    with patch.dict(sys.modules, {"native_import": ni}):
        result = asyncio.run(m.loaded_project_paths())
    assert result == ["/a", "/b"]
    ni.loaded_project_paths.assert_called_once_with()
