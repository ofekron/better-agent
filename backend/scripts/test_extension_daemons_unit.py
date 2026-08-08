"""Unit owner for backend/extension_daemons.py.

The projection layer (publish_registry / daemons_projection / ui_only_quiescent
/ _drain_or_remove / _is_drained / _declared_daemons) is exercised against REAL
files: the conftest engages an isolated BETTER_AGENT_HOME, and daemonhost's
ba_home() honors that env var, so registry.json / state.json / lifecycle.json
resolve into the tempdir and read_json/write_json are left untouched.

The collaborators that would otherwise reach real installation/extension state
(extension_store, installation_profile, scrubbed_env, subprocess.Popen, os.kill)
are monkeypatched so the unit is isolated and deterministic.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import extension_daemons
import installation_profile
from daemonhost.jsonio import read_json, write_json
from daemonhost.paths import daemon_root, registry_path, state_path


# --- collaborators ---------------------------------------------------------


def _spec(
    *,
    name: str = "sup",
    module: str = "ext.daemon",
    lifecycle: str = "supervisor",
    retire_policy: str = "immediate",
    env_allowlist=None,
    ports=None,
    restart_policy=None,
) -> dict:
    return {
        "name": name,
        "module": module,
        "lifecycle": lifecycle,
        "retire_policy": retire_policy,
        "env_allowlist": env_allowlist or [],
        "ports": ports or [],
        "restart_policy": restart_policy or {},
    }


def _record(extension_id: str, specs, *, hidden: bool = False) -> dict:
    return {
        "manifest": {"id": extension_id, "entrypoints": {"daemons": list(specs)}},
        "hidden": hidden,
    }


class _FakeProc:
    """Minimal subprocess.Popen double for the daemon lifecycle."""

    def __init__(self, *, pid: int = 100, alive: bool = True,
                 wait_raises_timeout: bool = False, spawn_raises: bool = False):
        self.pid = pid
        self._alive = alive
        self.terminated = False
        self.killed = False
        self.wait_count = 0
        self._wait_raises = wait_raises_timeout
        self._spawn_raises = spawn_raises

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        self.wait_count += 1
        if self._wait_raises and not self.killed:
            raise subprocess.TimeoutExpired([], timeout)
        self._alive = False
        return 0


def _patch_store(monkeypatch, *, records, active_ids, runtime_ready_ids,
                 roots, supervisor_roots, get_ext_returns, validated=None):
    """Bind the extension_store collaborators extension_daemons calls."""
    monkeypatch.setattr(extension_daemons.extension_store, "list_extensions",
                       lambda include_hidden=False: list(records), raising=False)
    monkeypatch.setattr(extension_daemons.extension_store, "is_extension_active",
                       lambda eid: eid in active_ids, raising=False)
    monkeypatch.setattr(extension_daemons.extension_store, "is_extension_runtime_ready",
                       lambda eid: eid in runtime_ready_ids, raising=False)
    monkeypatch.setattr(extension_daemons.extension_store, "runtime_package_root",
                        lambda eid: roots.get(eid), raising=False)
    monkeypatch.setattr(extension_daemons.extension_store, "supervisor_daemon_package_root",
                        lambda eid, root: supervisor_roots.get(eid, root), raising=False)
    monkeypatch.setattr(extension_daemons.extension_store, "get_extension",
                        lambda eid: get_ext_returns.get(eid), raising=False)
    if validated is not None:
        monkeypatch.setattr(extension_daemons.extension_store, "validate_manifest",
                            lambda raw: validated, raising=False)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_daemon_globals(monkeypatch):
    """Reset module-global proc table + wipe daemon files between tests.

    The conftest engages ONE test home per module (shared across this file's
    tests), so registry.json / state.json / lifecycle.json would otherwise leak
    between tests. Wipe the daemons root before each test for a clean slate.
    """
    daemons_root = registry_path().parent
    if daemons_root.exists():
        shutil.rmtree(daemons_root)
    saved_procs = extension_daemons._backend_procs
    extension_daemons._backend_procs = {}
    # scrubbed_env must return a fresh mutable dict each call.
    monkeypatch.setattr(extension_daemons, "scrubbed_env", lambda allow: {})
    yield
    extension_daemons._backend_procs = saved_procs


def _integrations(monkeypatch, enabled: bool):
    monkeypatch.setattr(installation_profile, "integrations_enabled", lambda: enabled)


# --- _daemon_key -----------------------------------------------------------


def test_daemon_key_format():
    assert extension_daemons._daemon_key("ext", "sup") == "ext:sup"


# --- _is_drained -----------------------------------------------------------


def test_is_drained_no_generation_is_false():
    assert extension_daemons._is_drained("k", {"extension_id": "e", "name": "n"}) is False


def test_is_drained_missing_lifecycle_file_is_false():
    entry = {"extension_id": "e", "name": "n", "generation": "g1"}
    assert extension_daemons._is_drained("k", entry) is False


def test_is_drained_generation_mismatch_is_false():
    write_json(daemon_root("e", "n") / "lifecycle.json",
               {"generation": "other", "state": "drained"})
    assert extension_daemons._is_drained("k", {"extension_id": "e", "name": "n", "generation": "g1"}) is False


def test_is_drained_wrong_state_is_false():
    write_json(daemon_root("e", "n") / "lifecycle.json",
               {"generation": "g1", "state": "active"})
    assert extension_daemons._is_drained("k", {"extension_id": "e", "name": "n", "generation": "g1"}) is False


def test_is_drained_matches_is_true():
    write_json(daemon_root("e", "n") / "lifecycle.json",
               {"generation": "g1", "state": "drained"})
    assert extension_daemons._is_drained("k", {"extension_id": "e", "name": "n", "generation": "g1"}) is True


def test_is_drained_read_json_oserror_is_false(monkeypatch):
    # read_json normally swallows OSError; the except in _is_drained is defense
    # against a collaborator that raises it. Force that path directly.
    def _raise(_path):
        raise OSError("unreadable")

    monkeypatch.setattr(extension_daemons, "read_json", _raise)
    assert extension_daemons._is_drained("k", {"extension_id": "e", "name": "n", "generation": "g1"}) is False


# --- _drain_or_remove ------------------------------------------------------


def test_drain_or_remove_non_dict_entry_is_noop():
    entries = {"k": "not-a-dict"}
    extension_daemons._drain_or_remove(entries, "k")
    assert entries == {"k": "not-a-dict"}


def test_drain_or_remove_immediate_policy_pops():
    entries = {"k": {"retire_policy": "immediate"}}
    extension_daemons._drain_or_remove(entries, "k")
    assert "k" not in entries


def test_drain_or_remove_drain_not_yet_drained_marks_draining():
    entries = {"k": {"retire_policy": "drain", "extension_id": "e", "name": "n", "generation": "g1"}}
    extension_daemons._drain_or_remove(entries, "k")
    assert entries["k"]["desired_state"] == "draining"


def test_drain_or_remove_drain_already_drained_pops():
    write_json(daemon_root("e", "n") / "lifecycle.json",
               {"generation": "g1", "state": "drained"})
    entries = {"k": {"retire_policy": "drain", "extension_id": "e", "name": "n", "generation": "g1"}}
    extension_daemons._drain_or_remove(entries, "k")
    assert "k" not in entries


# --- _declared_daemons ------------------------------------------------------


def test_declared_daemons_skips_missing_id(monkeypatch):
    _patch_store(monkeypatch,
                 records=[{"manifest": {"entrypoints": {"daemons": [_spec()]}}}],
                 active_ids=set(), runtime_ready_ids=set(), roots={},
                 supervisor_roots={}, get_ext_returns={})
    assert extension_daemons._declared_daemons() == []


def test_declared_daemons_yields_each_spec(monkeypatch):
    _patch_store(monkeypatch,
                 records=[_record("ext", [_spec(name="a"), _spec(name="b")]),
                          _record("other", [_spec(name="c")])],
                 active_ids=set(), runtime_ready_ids=set(), roots={},
                 supervisor_roots={}, get_ext_returns={})
    triples = extension_daemons._declared_daemons()
    assert [(t[0], t[2]["name"]) for t in triples] == [("ext", "a"), ("ext", "b"), ("other", "c")]


def test_declared_daemons_builtin_switch_revalidates_manifest(monkeypatch, tmp_path):
    builtin = extension_daemons.extension_store.BUILTIN_SWITCH_CONTROL_EXTENSION_ID
    manifest_file = tmp_path / "better-agent-extension.json"
    manifest_file.write_text(json.dumps({"id": builtin, "entrypoints": {"daemons": [_spec(name="switch")]}}),
                             encoding="utf-8")
    monkeypatch.setattr(extension_daemons, "_BUILTIN_SWITCH_MANIFEST", manifest_file)
    _patch_store(monkeypatch,
                 records=[{"manifest": {"id": builtin, "entrypoints": {"daemons": [_spec(name="stale")]}}}],
                 active_ids=set(), runtime_ready_ids=set(), roots={},
                 supervisor_roots={}, get_ext_returns={}, validated={"id": builtin,
                                                                    "entrypoints": {"daemons": [_spec(name="switch")]}})
    triples = extension_daemons._declared_daemons()
    assert triples and triples[0][0] == builtin and triples[0][2]["name"] == "switch"


def test_declared_daemons_no_entrypoints_is_empty(monkeypatch):
    _patch_store(monkeypatch,
                 records=[{"manifest": {"id": "ext"}}],
                 active_ids=set(), runtime_ready_ids=set(), roots={},
                 supervisor_roots={}, get_ext_returns={})
    assert extension_daemons._declared_daemons() == []


# --- publish_registry -------------------------------------------------------


def test_publish_registry_disabled_writes_empty(monkeypatch):
    _integrations(monkeypatch, False)
    result = extension_daemons.publish_registry()
    assert result == {}
    assert read_json(registry_path()) == {"daemons": {}}


def test_publish_registry_emits_active_supervisor_entry(monkeypatch):
    _integrations(monkeypatch, True)
    _patch_store(monkeypatch,
                 records=[_record("ext", [_spec(name="sup", restart_policy={"max": 1})])],
                 active_ids={"ext"}, runtime_ready_ids=set(),
                 roots={"ext": "/pkg/ext"}, supervisor_roots={"ext": "/pkg/ext/sup"},
                 get_ext_returns={})
    # pre-existing generation is preserved
    write_json(registry_path(), {"daemons": {"ext:sup": {"generation": "GEN"}}})
    result = extension_daemons.publish_registry()
    entry = result["ext:sup"]
    assert entry["generation"] == "GEN"
    assert entry["lifecycle"] == "supervisor"
    assert entry["desired_state"] == "active"
    assert entry["source_root"] == "/pkg/ext/sup"
    assert entry["restart_policy"] == {"max": 1}
    # written to disk
    assert read_json(registry_path())["daemons"]["ext:sup"] == entry


def test_publish_registry_inactive_supervisor_is_drained(monkeypatch):
    _integrations(monkeypatch, True)
    _patch_store(monkeypatch,
                 records=[_record("ext", [_spec(name="sup", retire_policy="drain")])],
                 active_ids=set(), runtime_ready_ids=set(), roots={},
                 supervisor_roots={}, get_ext_returns={})
    write_json(registry_path(), {"daemons": {"ext:sup": {"retire_policy": "drain",
                                                         "extension_id": "ext", "name": "sup",
                                                         "generation": "g1"}}})
    result = extension_daemons.publish_registry()
    assert result["ext:sup"]["desired_state"] == "draining"


def test_publish_registry_package_unavailable_keeps_entry(monkeypatch):
    _integrations(monkeypatch, True)
    _patch_store(monkeypatch,
                 records=[_record("ext", [_spec(name="sup")])],
                 active_ids={"ext"}, runtime_ready_ids=set(),
                 roots={"ext": None}, supervisor_roots={},
                 get_ext_returns={})
    write_json(registry_path(), {"daemons": {"ext:sup": {"extension_id": "ext", "kept": True}}})
    result = extension_daemons.publish_registry()
    # source_root None → entry left untouched (survives a line switch)
    assert result["ext:sup"] == {"extension_id": "ext", "kept": True}


def test_publish_registry_skips_non_supervisor_lifecycle(monkeypatch):
    _integrations(monkeypatch, True)
    _patch_store(monkeypatch,
                 records=[_record("ext", [_spec(name="be", lifecycle="backend")])],
                 active_ids={"ext"}, runtime_ready_ids=set(),
                 roots={"ext": "/pkg"}, supervisor_roots={},
                 get_ext_returns={})
    result = extension_daemons.publish_registry()
    assert result == {}


def test_publish_registry_prunes_ghost_extension(monkeypatch):
    _integrations(monkeypatch, True)
    _patch_store(monkeypatch,
                 records=[], active_ids=set(), runtime_ready_ids=set(),
                 roots={}, supervisor_roots={}, get_ext_returns={})
    write_json(registry_path(), {"daemons": {"ghost:sup": {"extension_id": "ghost", "retire_policy": "immediate"}}})
    result = extension_daemons.publish_registry()
    assert "ghost:sup" not in result


def test_publish_registry_prunes_undesired_available_entry(monkeypatch):
    _integrations(monkeypatch, True)
    _patch_store(monkeypatch,
                 records=[_record("ext", [_spec(name="sup")])],
                 active_ids={"ext"}, runtime_ready_ids=set(),
                 roots={"ext": "/pkg"}, supervisor_roots={"ext": "/pkg/sup"},
                 get_ext_returns={})
    write_json(registry_path(), {"daemons": {"ext:stale": {"extension_id": "ext", "retire_policy": "immediate"}}})
    result = extension_daemons.publish_registry()
    assert "ext:stale" not in result
    assert "ext:sup" in result  # the desired one survives


def test_publish_registry_existing_non_dict_daemons_is_ignored(monkeypatch):
    _integrations(monkeypatch, True)
    _patch_store(monkeypatch,
                 records=[_record("ext", [_spec(name="sup")])],
                 active_ids={"ext"}, runtime_ready_ids=set(),
                 roots={"ext": "/pkg"}, supervisor_roots={"ext": "/pkg/sup"},
                 get_ext_returns={})
    write_json(registry_path(), {"daemons": ["not", "a", "dict"]})
    result = extension_daemons.publish_registry()
    assert "ext:sup" in result


# --- reconcile_backend_daemons + _stop + shutdown --------------------------


def test_reconcile_backend_daemons_disabled_starts_nothing(monkeypatch):
    _integrations(monkeypatch, False)
    _patch_store(monkeypatch,
                 records=[_record("ext", [_spec(name="be", lifecycle="backend")])],
                 active_ids={"ext"}, runtime_ready_ids={"ext"},
                 roots={"ext": "/pkg"}, supervisor_roots={}, get_ext_returns={})
    spawned = []

    def fake_popen(cmd, **kw):
        spawned.append((cmd, kw))
        return _FakeProc()

    monkeypatch.setattr(extension_daemons.subprocess, "Popen", fake_popen)
    extension_daemons.reconcile_backend_daemons()
    assert spawned == []
    assert extension_daemons._backend_procs == {}


def test_reconcile_backend_daemons_spawns_backend_daemon(monkeypatch):
    _integrations(monkeypatch, True)
    _patch_store(monkeypatch,
                 records=[_record("ext", [_spec(name="be", module="ext.run", lifecycle="backend",
                                                env_allowlist=["FOO"])])],
                 active_ids={"ext"}, runtime_ready_ids={"ext"},
                 roots={"ext": "/pkg/ext"}, supervisor_roots={},
                 get_ext_returns={})
    captured = {}

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return _FakeProc(pid=4242)

    monkeypatch.setattr(extension_daemons.subprocess, "Popen", fake_popen)
    extension_daemons.reconcile_backend_daemons()
    assert set(extension_daemons._backend_procs) == {"ext:be"}
    assert captured["cmd"] == [sys.executable, "-m", "ext.run"]
    assert captured["kw"]["cwd"] == "/pkg/ext"
    assert captured["kw"]["env"]["PYTHONPATH"] == "/pkg/ext"
    assert captured["kw"]["env"]["BETTER_AGENT_DAEMON"] == "ext:be"
    assert captured["kw"]["stdin"] == subprocess.DEVNULL
    assert extension_daemons._backend_procs["ext:be"].pid == 4242


def test_reconcile_backend_daemons_skips_not_runtime_ready(monkeypatch):
    _integrations(monkeypatch, True)
    _patch_store(monkeypatch,
                 records=[_record("ext", [_spec(name="be", lifecycle="backend")])],
                 active_ids={"ext"}, runtime_ready_ids=set(),  # not ready
                 roots={"ext": "/pkg"}, supervisor_roots={}, get_ext_returns={})
    monkeypatch.setattr(extension_daemons.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("must not spawn"))
    extension_daemons.reconcile_backend_daemons()
    assert extension_daemons._backend_procs == {}


def test_reconcile_backend_daemons_skips_missing_package_root(monkeypatch):
    _integrations(monkeypatch, True)
    _patch_store(monkeypatch,
                 records=[_record("ext", [_spec(name="be", lifecycle="backend")])],
                 active_ids={"ext"}, runtime_ready_ids={"ext"},
                 roots={"ext": None}, supervisor_roots={}, get_ext_returns={})
    monkeypatch.setattr(extension_daemons.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("must not spawn"))
    extension_daemons.reconcile_backend_daemons()
    assert extension_daemons._backend_procs == {}


def test_reconcile_backend_daemons_spawn_oserror_is_swallowed(monkeypatch):
    _integrations(monkeypatch, True)
    _patch_store(monkeypatch,
                 records=[_record("ext", [_spec(name="be", lifecycle="backend")])],
                 active_ids={"ext"}, runtime_ready_ids={"ext"},
                 roots={"ext": "/pkg"}, supervisor_roots={}, get_ext_returns={})

    def raising(*a, **k):
        raise OSError("boom")

    monkeypatch.setattr(extension_daemons.subprocess, "Popen", raising)
    extension_daemons.reconcile_backend_daemons()
    assert extension_daemons._backend_procs == {}


def test_reconcile_backend_daemons_stops_and_removes_dead_proc(monkeypatch):
    _integrations(monkeypatch, True)
    _patch_store(monkeypatch, records=[], active_ids=set(), runtime_ready_ids=set(),
                 roots={}, supervisor_roots={}, get_ext_returns={})
    dead = _FakeProc(alive=False)
    extension_daemons._backend_procs["ext:be"] = dead
    extension_daemons.reconcile_backend_daemons()
    assert "ext:be" not in extension_daemons._backend_procs
    assert dead.terminated is False  # already dead → _stop returns early


def test_reconcile_backend_daemons_stops_proc_no_longer_desired(monkeypatch):
    _integrations(monkeypatch, True)
    _patch_store(monkeypatch,
                 records=[_record("ext", [_spec(name="be", lifecycle="backend")])],
                 active_ids={"ext"}, runtime_ready_ids={"ext"},
                 roots={"ext": "/pkg"}, supervisor_roots={}, get_ext_returns={})
    orphan = _FakeProc(alive=True)
    extension_daemons._backend_procs["ext:gone"] = orphan
    # First poll() of the desired spawn returns a fresh proc; orphan must be stopped.
    monkeypatch.setattr(extension_daemons.subprocess, "Popen", lambda *a, **k: _FakeProc())
    extension_daemons.reconcile_backend_daemons()
    assert "ext:gone" not in extension_daemons._backend_procs
    assert orphan.terminated is True


def test_reconcile_backend_daemons_keeps_existing_live_proc(monkeypatch):
    _integrations(monkeypatch, True)
    _patch_store(monkeypatch,
                 records=[_record("ext", [_spec(name="be", lifecycle="backend")])],
                 active_ids={"ext"}, runtime_ready_ids={"ext"},
                 roots={"ext": "/pkg"}, supervisor_roots={}, get_ext_returns={})
    alive = _FakeProc(alive=True)
    extension_daemons._backend_procs["ext:be"] = alive
    monkeypatch.setattr(extension_daemons.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("must not respawn"))
    extension_daemons.reconcile_backend_daemons()
    assert extension_daemons._backend_procs["ext:be"] is alive


def test_stop_dead_proc_is_noop():
    proc = _FakeProc(alive=False)
    extension_daemons._stop(proc)
    assert proc.terminated is False and proc.killed is False


def test_stop_live_proc_terminates_and_waits():
    proc = _FakeProc(alive=True)
    extension_daemons._stop(proc)
    assert proc.terminated is True
    assert proc.killed is False
    assert proc.wait_count == 1


def test_stop_timeout_falls_back_to_kill():
    proc = _FakeProc(alive=True, wait_raises_timeout=True)
    extension_daemons._stop(proc)
    assert proc.terminated is True
    assert proc.killed is True
    assert proc.wait_count == 2


def test_shutdown_backend_daemons_stops_all_and_clears():
    a = _FakeProc(alive=True)
    b = _FakeProc(alive=False)
    extension_daemons._backend_procs = {"a": a, "b": b}
    extension_daemons.shutdown_backend_daemons()
    assert extension_daemons._backend_procs == {}
    assert a.terminated is True


# --- reconcile -------------------------------------------------------------


def test_reconcile_invokes_both_phases(monkeypatch):
    calls = []
    monkeypatch.setattr(extension_daemons, "publish_registry",
                        lambda: calls.append("publish") or {})
    monkeypatch.setattr(extension_daemons, "reconcile_backend_daemons",
                        lambda: calls.append("backend"))
    extension_daemons.reconcile()
    assert calls == ["publish", "backend"]


# --- daemons_projection ----------------------------------------------------


def test_daemons_projection_reads_state_and_procs(monkeypatch):
    write_json(registry_path(), {"daemons": {"ext:sup": {"extension_id": "ext"}}})
    write_json(state_path(), {"daemons": {"ext:sup": {"pid": 7}}})
    extension_daemons._backend_procs["ext:be"] = _FakeProc(alive=True, pid=99)
    proj = extension_daemons.daemons_projection()
    assert proj["registry"] == {"ext:sup": {"extension_id": "ext"}}
    assert proj["supervisor_state"] == {"daemons": {"ext:sup": {"pid": 7}}}
    assert proj["backend_daemons"]["ext:be"] == {"status": "running", "pid": 99}


def test_daemons_projection_empty_when_no_files():
    proj = extension_daemons.daemons_projection()
    assert proj == {"registry": {}, "supervisor_state": {}, "backend_daemons": {}}


# --- ui_only_quiescent -----------------------------------------------------


def test_ui_only_quiescent_true_when_all_idle():
    assert extension_daemons.ui_only_quiescent() is True


def test_ui_only_quiescent_false_when_registry_nonempty():
    write_json(registry_path(), {"daemons": {"ext:sup": {"extension_id": "ext"}}})
    assert extension_daemons.ui_only_quiescent() is False


def test_ui_only_quiescent_false_when_backend_proc_running():
    extension_daemons._backend_procs["ext:be"] = _FakeProc(alive=True)
    assert extension_daemons.ui_only_quiescent() is False


def test_ui_only_quiescent_true_when_supervisor_pid_dead():
    write_json(state_path(), {"daemons": {"ext:sup": {"pid": 999999}}})  # no such pid
    assert extension_daemons.ui_only_quiescent() is True


def test_ui_only_quiescent_false_when_supervisor_pid_alive():
    import os
    write_json(state_path(), {"daemons": {"ext:sup": {"pid": os.getpid()}}})
    assert extension_daemons.ui_only_quiescent() is False


def test_ui_only_quiescent_false_when_supervisor_pid_permission(monkeypatch):
    def _perm(*_args, **_kw):
        raise PermissionError
    monkeypatch.setattr(extension_daemons.os, "kill", _perm)
    write_json(state_path(), {"daemons": {"ext:sup": {"pid": 1}}})
    assert extension_daemons.ui_only_quiescent() is False


def test_ui_only_quiescent_ignores_non_dict_supervisor_entries():
    write_json(state_path(), {"daemons": {"junk": "not-a-dict", "nopid": {"pid": "x"}}})
    assert extension_daemons.ui_only_quiescent() is True


def test_ui_only_quiescent_handles_non_dict_supervisor_state():
    write_json(state_path(), {"daemons": "not-a-dict"})
    assert extension_daemons.ui_only_quiescent() is True
