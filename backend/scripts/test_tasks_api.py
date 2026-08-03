"""Unit coverage for tasks_api.py.

The module is an HTTP dispatch layer: it validates untrusted bodies,
delegates to stores / task_runner / routine_memory, and returns plain
dicts. We exercise it by calling the async handlers directly with faked
dependencies (no live backend, no provider turn). The gate, every action
branch, and every error path are covered with real assertions.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile

import pytest
from fastapi import HTTPException

import extension_store
import internal_guards
import routine_memory
import stores
import task_runner
import task_output_preview_urls
import tasks_api

_HEADER = {"X-Internal-Token": "tok"}


def _run(coro):
    return asyncio.run(coro)


class FakeTaskStore:
    def __init__(self):
        self.list_for_project = lambda cwd, node_id: [{"id": "t1", "cwd": cwd, "node_id": node_id}]
        self.get = lambda task_id: {"id": task_id, "cwd": "/p", "node_id": "primary"}
        self._create_fn = None
        self.created_kwargs = None

    def create(self, **kwargs):
        if self._create_fn is not None:
            return self._create_fn(**kwargs)
        self.created_kwargs = kwargs
        return {"id": "new", "cwd": kwargs["cwd"], "node_id": kwargs["node_id"]}

    def update(self, task_id, patch):
        if self._update_fn is not None:
            return self._update_fn(task_id, patch)
        return {"id": task_id, "cwd": "/p", "node_id": "primary"}

    def set_update(self, fn):
        self._update_fn = fn


class FakeTriggerStore:
    def __init__(self):
        self.registered = []
        self.unregistered = []

    def register_for_task(self, rec):
        self.registered.append(rec["id"])

    def unregister_task(self, task_id):
        self.unregistered.append(task_id)


class FakeOutputStore:
    def __init__(self):
        self.deleted_for = []
        self.published = None
        self._list = [{"id": "out2"}, {"id": "out1"}]
        self._content = (str(tempfile.gettempdir()), "text/plain")

    def list_for_task(self, task_id, limit=50):
        return self._list

    def publish(self, **kwargs):
        if self._publish_raises:
            raise ValueError(self._publish_msg)
        self.published = kwargs
        return {"id": "outpub", **kwargs}

    def delete_for_task(self, task_id):
        self.deleted_for.append(task_id)

    def content_path(self, task_id, output_id):
        if self._content_raises is not None:
            raise self._content_raises
        return self._content

    _publish_raises = False
    _publish_msg = "bad"
    _content_raises = None


@pytest.fixture
def fakes(monkeypatch):
    """Wire fakes for every dependency tasks_api imports in-function."""
    fake_task = FakeTaskStore()
    fake_trigger = FakeTriggerStore()
    fake_output = FakeOutputStore()
    monkeypatch.setitem(sys.modules, "stores.task_store", fake_task)
    monkeypatch.setattr(stores, "task_store", fake_task, raising=False)
    monkeypatch.setitem(sys.modules, "stores.task_trigger_store", fake_trigger)
    monkeypatch.setattr(stores, "task_trigger_store", fake_trigger, raising=False)
    monkeypatch.setitem(sys.modules, "stores.task_output_store", fake_output)
    monkeypatch.setattr(stores, "task_output_store", fake_output, raising=False)

    # Authority valid + routines extension ready + coordinator bound.
    internal_guards.configure(lambda: object())
    tasks_api.configure(coordinator=object())
    monkeypatch.setattr(extension_store, "runtime_not_ready_message", lambda eid: None)
    monkeypatch.setattr(extension_store, "extension_id_for_role", lambda role: "routines-ext")

    # task_runner surface as async no-ops; overridable per test.
    async def _broadcast(coordinator, cwd, node_id="primary"):
        return None

    async def _launch(task_id, *, coordinator, prompt_override=None, client_id=None,
                      source="manual", event_receipt_id=None):
        return {"session_id": "s1"}

    async def _stop(task_id, *, coordinator):
        return {"stopped": [task_id]}

    monkeypatch.setattr(task_runner, "broadcast_tasks_changed", _broadcast)
    monkeypatch.setattr(task_runner, "launch_task", _launch)
    monkeypatch.setattr(task_runner, "stop_task", _stop)

    yield {"task": fake_task, "trigger": fake_trigger, "output": fake_output}


# ------------------------------------------------------------------ gate

def test_gate_rejects_when_authority_absent(monkeypatch):
    """Unconfigured authority => 403, fail closed."""
    monkeypatch.setattr(internal_guards, "_principal_provider", None)
    with pytest.raises(HTTPException) as exc:
        _run(tasks_api.internal_tasks({"action": "list"}, "tok"))
    assert exc.value.status_code == 403


def test_gate_rejects_when_extension_not_ready(monkeypatch):
    """Authority valid but routines extension not running => 404."""
    internal_guards.configure(lambda: object())
    monkeypatch.setattr(extension_store, "extension_id_for_role", lambda role: "routines-ext")
    monkeypatch.setattr(extension_store, "runtime_not_ready_message", lambda eid: "not ready")
    with pytest.raises(HTTPException) as exc:
        _run(tasks_api.internal_tasks({"action": "list"}, "tok"))
    assert exc.value.status_code == 404


def test_coordinator_unconfigured_raises_503(monkeypatch):
    """_coordinator() fails closed (503) until configure() binds one."""
    internal_guards.configure(lambda: object())
    monkeypatch.setattr(extension_store, "runtime_not_ready_message", lambda eid: None)
    monkeypatch.setattr(extension_store, "extension_id_for_role", lambda role: "routines-ext")
    monkeypatch.setattr(tasks_api, "_coordinator_ref", None)
    with pytest.raises(HTTPException) as exc:
        tasks_api._coordinator()
    assert exc.value.status_code == 503


# -------------------------------------------------------- internal_tasks

def test_list_requires_cwd(fakes):
    res = _run(tasks_api.internal_tasks({"action": "list"}, "tok"))
    assert res == {"success": False, "error": "cwd is required"}


def test_list_success(fakes):
    res = _run(tasks_api.internal_tasks({"action": "list", "cwd": "/proj"}, "tok"))
    assert res["success"] is True
    assert res["tasks"][0]["cwd"] == "/proj"
    assert res["tasks"][0]["node_id"] == "primary"


def test_list_node_id_normalized(fakes):
    res = _run(tasks_api.internal_tasks(
        {"action": "list", "cwd": "/p", "node_id": "  worker-1  "}, "tok"))
    assert res["tasks"][0]["node_id"] == "worker-1"


def test_list_empty_node_id_defaults_primary(fakes):
    res = _run(tasks_api.internal_tasks(
        {"action": "list", "cwd": "/p", "node_id": "   "}, "tok"))
    assert res["tasks"][0]["node_id"] == "primary"


def test_get_unknown(fakes):
    fakes["task"].get = lambda task_id: None
    res = _run(tasks_api.internal_tasks({"action": "get", "task_id": "nope"}, "tok"))
    assert res == {"success": False, "error": "unknown task_id"}


def test_get_success(fakes):
    res = _run(tasks_api.internal_tasks({"action": "get", "task_id": "t1"}, "tok"))
    assert res["success"] is True
    assert res["task"]["id"] == "t1"


def test_create_value_error(fakes):
    fakes["task"]._create_fn = lambda **kw: (_ for _ in ()).throw(ValueError("bad name"))
    res = _run(tasks_api.internal_tasks({"action": "create", "cwd": "/p", "name": "x"}, "tok"))
    assert res == {"success": False, "error": "bad name"}


def test_create_success_registers_and_broadcasts(fakes):
    res = _run(tasks_api.internal_tasks(
        {"action": "create", "cwd": "/p", "name": "mytask", "prompt": "do"}, "tok"))
    assert res["success"] is True
    assert res["task"]["id"] == "new"
    assert fakes["trigger"].registered == ["new"]
    assert fakes["task"].created_kwargs["singleton"] is False
    assert fakes["task"].created_kwargs["orchestration_mode"] == "native"
    assert fakes["task"].created_kwargs["description"] == ""


def test_create_singleton_flag_passed(fakes):
    _run(tasks_api.internal_tasks(
        {"action": "create", "cwd": "/p", "name": "s", "prompt": "p", "singleton": True}, "tok"))
    assert fakes["task"].created_kwargs["singleton"] is True


def test_update_patch_not_object(fakes):
    res = _run(tasks_api.internal_tasks(
        {"action": "update", "task_id": "t1", "patch": "notdict"}, "tok"))
    assert res == {"success": False, "error": "patch must be an object"}


def test_update_value_error(fakes):
    def raise_ve(task_id, patch):
        raise ValueError("nope")
    fakes["task"].set_update(raise_ve)
    res = _run(tasks_api.internal_tasks(
        {"action": "update", "task_id": "t1", "patch": {"a": 1}}, "tok"))
    assert res == {"success": False, "error": "nope"}


def test_update_unknown(fakes):
    fakes["task"].set_update(lambda task_id, patch: None)
    res = _run(tasks_api.internal_tasks(
        {"action": "update", "task_id": "t1", "patch": {"a": 1}}, "tok"))
    assert res == {"success": False, "error": "unknown task_id"}


def test_update_success(fakes):
    fakes["task"].set_update(lambda task_id, patch: {"id": task_id, "cwd": "/p", "node_id": "primary"})
    res = _run(tasks_api.internal_tasks(
        {"action": "update", "task_id": "t1", "patch": {"name": "z"}}, "tok"))
    assert res["success"] is True
    assert res["task"]["id"] == "t1"
    assert fakes["trigger"].registered == ["t1"]


def test_delete_busy_error(fakes, monkeypatch):
    async def raise_busy(task_id):
        raise routine_memory.RoutineMemoryBusyError("locked")
    monkeypatch.setattr(routine_memory, "delete_task", raise_busy)
    res = _run(tasks_api.internal_tasks({"action": "delete", "task_id": "t1"}, "tok"))
    assert res == {"success": False, "error": "locked"}


def test_delete_access_error(fakes, monkeypatch):
    async def raise_access(task_id):
        raise routine_memory.RoutineMemoryAccessError("no db")
    monkeypatch.setattr(routine_memory, "delete_task", raise_access)
    res = _run(tasks_api.internal_tasks({"action": "delete", "task_id": "t1"}, "tok"))
    assert res == {"success": False, "error": "no db"}


def test_delete_unknown(fakes, monkeypatch):
    async def returns_none(task_id):
        return None
    monkeypatch.setattr(routine_memory, "delete_task", returns_none)
    res = _run(tasks_api.internal_tasks({"action": "delete", "task_id": "t1"}, "tok"))
    assert res == {"success": False, "error": "unknown task_id"}


def test_delete_success_cleans_outputs_and_triggers(fakes, monkeypatch):
    async def returns_removed(task_id):
        return {"cwd": "/p", "node_id": "worker-1"}
    monkeypatch.setattr(routine_memory, "delete_task", returns_removed)
    res = _run(tasks_api.internal_tasks({"action": "delete", "task_id": "t1"}, "tok"))
    assert res == {"success": True}
    assert fakes["output"].deleted_for == ["t1"]
    assert fakes["trigger"].unregistered == ["t1"]


def test_run_launch_error(fakes, monkeypatch):
    async def boom(task_id, *, coordinator, prompt_override=None, client_id=None,
                   source="manual", event_receipt_id=None):
        raise task_runner.TaskLaunchError("unknown task", status=404)
    monkeypatch.setattr(task_runner, "launch_task", boom)
    res = _run(tasks_api.internal_tasks({"action": "run", "task_id": "t1"}, "tok"))
    assert res == {"success": False, "error": "unknown task"}


def test_run_success(fakes, monkeypatch):
    async def ok(task_id, *, coordinator, prompt_override=None, client_id=None,
                 source="manual", event_receipt_id=None):
        assert prompt_override == "go"
        assert client_id == "c1"
        return {"session_id": "sx"}
    monkeypatch.setattr(task_runner, "launch_task", ok)
    res = _run(tasks_api.internal_tasks(
        {"action": "run", "task_id": "t1", "prompt": "go", "client_id": "c1"}, "tok"))
    assert res == {"success": True, "session_id": "sx"}


def test_stop_launch_error(fakes, monkeypatch):
    async def boom(task_id, *, coordinator):
        raise task_runner.TaskLaunchError("unknown task", status=404)
    monkeypatch.setattr(task_runner, "stop_task", boom)
    res = _run(tasks_api.internal_tasks({"action": "stop", "task_id": "t1"}, "tok"))
    assert res == {"success": False, "error": "unknown task"}


def test_stop_success(fakes, monkeypatch):
    async def ok(task_id, *, coordinator):
        return {"stopped": ["t1"]}
    monkeypatch.setattr(task_runner, "stop_task", ok)
    res = _run(tasks_api.internal_tasks({"action": "stop", "task_id": "t1"}, "tok"))
    assert res == {"success": True, "stopped": ["t1"]}


def test_unknown_action(fakes):
    res = _run(tasks_api.internal_tasks({"action": "frobnicate"}, "tok"))
    assert res["success"] is False
    assert "list|get|create|update|delete|run|stop" in res["error"]


# ------------------------------------------------------ internal_task_outputs

def test_outputs_requires_task_id(fakes):
    res = _run(tasks_api.internal_task_outputs({"action": "list"}, "tok"))
    assert res == {"success": False, "error": "task_id is required"}


def test_outputs_unknown_task(fakes):
    fakes["task"].get = lambda task_id: None
    res = _run(tasks_api.internal_task_outputs({"action": "list", "task_id": "x"}, "tok"))
    assert res == {"success": False, "error": "unknown task_id"}


def test_outputs_list_with_latest(fakes):
    res = _run(tasks_api.internal_task_outputs({"action": "list", "task_id": "t1"}, "tok"))
    assert res["success"] is True
    assert res["latest"] == {"id": "out2"}
    assert len(res["outputs"]) == 2


def test_outputs_list_empty_latest_none(fakes):
    fakes["output"]._list = []
    res = _run(tasks_api.internal_task_outputs({"action": "list", "task_id": "t1"}, "tok"))
    assert res["latest"] is None
    assert res["outputs"] == []


def test_outputs_publish_value_error(fakes):
    fakes["output"]._publish_raises = True
    res = _run(tasks_api.internal_task_outputs(
        {"action": "publish", "task_id": "t1", "title": "T"}, "tok"))
    assert res == {"success": False, "error": "bad"}


def test_outputs_publish_success(fakes):
    res = _run(tasks_api.internal_task_outputs(
        {"action": "publish", "task_id": "t1", "title": "T", "kind": "report",
         "content": "<b>", "session_id": "ss"}, "tok"))
    assert res["success"] is True
    assert res["output"]["id"] == "outpub"
    assert fakes["output"].published["kind"] == "report"
    assert fakes["output"].published["session_id"] == "ss"
    assert fakes["output"].published["content_type"] == "text/html"


def test_outputs_unknown_action(fakes):
    res = _run(tasks_api.internal_task_outputs({"action": "frob", "task_id": "t1"}, "tok"))
    assert res["success"] is False
    assert "list|publish" in res["error"]


# ----------------------------------------------- task output content / preview

def test_output_content_not_found(fakes):
    fakes["output"]._content_raises = FileNotFoundError()
    with pytest.raises(HTTPException) as exc:
        _run(tasks_api.internal_task_output_content("t1", "out1", "tok"))
    assert exc.value.status_code == 404


def test_output_content_success(fakes, tmp_path):
    f = tmp_path / "o.html"
    f.write_text("<i>hi</i>")
    fakes["output"]._content = (str(f), "text/html")
    from fastapi.responses import FileResponse
    resp = _run(tasks_api.internal_task_output_content("t1", "out1", "tok"))
    assert isinstance(resp, FileResponse)
    assert resp.media_type == "text/html"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in resp.headers["Content-Security-Policy"]


def test_preview_url_not_found(fakes):
    fakes["output"]._content_raises = FileNotFoundError()
    with pytest.raises(HTTPException) as exc:
        _run(tasks_api.task_output_preview_url("t1", "out1"))
    assert exc.value.status_code == 404


def test_preview_url_value_error(fakes):
    fakes["output"]._content_raises = ValueError("bad id")
    with pytest.raises(HTTPException) as exc:
        _run(tasks_api.task_output_preview_url("t1", "out1"))
    assert exc.value.status_code == 404


def test_preview_url_success(fakes, tmp_path):
    f = tmp_path / "o.html"
    f.write_text("x")
    fakes["output"]._content = (str(f), "text/html")
    # Valid ids: task [A-Za-z0-9_-]{1,64}, output [a-f0-9]{12}.
    res = _run(tasks_api.task_output_preview_url("task-abc_1", "abcdef012345"))
    assert res["url"].startswith("/api/task-output/preview/")
    assert "task-abc_1" in res["url"] and "abcdef012345" in res["url"]


def test_preview_invalid_token(fakes, tmp_path):
    f = tmp_path / "o.html"
    f.write_text("x")
    fakes["output"]._content = (str(f), "text/html")
    with pytest.raises(HTTPException) as exc:
        _run(tasks_api.task_output_preview("not-a-real-token", "task-abc_1", "abcdef012345"))
    assert exc.value.status_code == 403


def test_preview_valid_token_then_missing_content(fakes, tmp_path):
    f = tmp_path / "o.html"
    f.write_text("x")
    fakes["output"]._content = (str(f), "text/html")
    token = task_output_preview_urls.mint("task-abc_1", "abcdef012345")
    # After mint succeeds, content lookup fails.
    fakes["output"]._content_raises = FileNotFoundError()
    with pytest.raises(HTTPException) as exc:
        _run(tasks_api.task_output_preview(token, "task-abc_1", "abcdef012345"))
    assert exc.value.status_code == 404


def test_preview_success(fakes, tmp_path):
    f = tmp_path / "o.html"
    f.write_text("<i>hi</i>")
    fakes["output"]._content = (str(f), "text/html")
    token = task_output_preview_urls.mint("task-abc_1", "abcdef012345")
    from fastapi.responses import FileResponse
    resp = _run(tasks_api.task_output_preview(token, "task-abc_1", "abcdef012345"))
    assert isinstance(resp, FileResponse)
    assert resp.media_type == "text/html"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["Cache-Control"] == "private, no-store"
