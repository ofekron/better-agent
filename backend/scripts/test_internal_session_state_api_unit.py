"""Unit-tier coverage of internal_session_state_api.py — the internal-loopback
router that extensions use to mutate sessions they own: the ownership /
permission gates, virtual-session CRUD, synthetic-message injection, managed
and headless runs, render-tree message writes, scoped session-field read/write,
and the session-activity snapshot.

Every route is security-relevant network surface (auth/approval/untrusted
input), so this file pins each gate and validation branch. Routes are called
directly as async functions (no HTTP client); collaborators are fakes swapped
onto the router's module namespace and restored on teardown so nothing bleeds
into other test files. Isolated BETTER_AGENT_HOME via _test_home.isolate.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-internal-session-state-")

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi import HTTPException  # noqa: E402
import pytest  # noqa: E402

import internal_session_state_api as api  # noqa: E402


# --- fakes -----------------------------------------------------------------


class _FakeTurnManager:
    def __init__(self, coord):
        self._coord = coord

    def is_running_cached(self, sid):
        return self._coord.running.get(sid, False)

    def monitoring_state_cached(self, sid):
        return self._coord.monitoring.get(sid, "idle")


class _FakeCoordinator:
    def __init__(self):
        self.broadcasts = []
        self.panels = []
        self.queued = {}
        self.running = {}
        self.monitoring = {}
        self.turn_manager = _FakeTurnManager(self)

    async def broadcast_global(self, event, payload):
        self.broadcasts.append((event, payload))

    async def emit_session_created_panel(self, **kw):
        self.panels.append(kw)

    def has_queued_prompts(self, sid):
        return self.queued.get(sid, 0) > 0

    def get_queued_count(self, sid):
        return self.queued.get(sid, 0)


class _FakeGuards:
    def __init__(self):
        self.valid = True
        self.extension_id = "ext1"

    def authority_is_valid(self):
        return self.valid

    def internal_authority_extension_id(self):
        return self.extension_id


class _FakeExtensionStore:
    def __init__(self):
        # extension_id -> {"active": bool, "perms": set, "manifest": {...}}
        self.records = {}

    def get_extension(self, eid):
        rec = self.records.get(eid)
        return rec if rec is None else dict(rec)

    def is_extension_active(self, eid):
        rec = self.records.get(eid)
        return bool(rec and rec.get("active"))

    def has_permission(self, record, permission):
        return bool(record and permission in record.get("perms", set()))

    def session_field_allowlist(self, eid):
        return self.records.get(eid, {}).get("mutates", [])

    def session_field_read_allowlist(self, eid):
        return self.records.get(eid, {}).get("reads", [])

    def grant(self, eid="ext1", perms=None, active=True, mutates=None,
              reads=None, managed_run_env=None):
        self.records[eid] = {
            "active": active,
            "perms": set(perms or []),
            "mutates": mutates or [],
            "reads": reads or [],
            "manifest": {"permissions": {"managed_run_env": managed_run_env or []}},
        }
        return self


class _FakeVirtualStore:
    def __init__(self):
        self.sessions = {}
        self.calls = []
        self.raise_on_upsert = None
        self.raise_on_delete = None
        self.raise_on_append = None
        self.append_result = {"id": "m1"}

    def get(self, sid):
        return self.sessions.get(sid)

    def upsert(self, eid, payload):
        if self.raise_on_upsert:
            exc, self.raise_on_upsert = self.raise_on_upsert, None
            raise exc
        sess = dict(payload)
        sess.setdefault("id", "vs1")
        self.sessions[sess["id"]] = sess
        self.calls.append(("upsert", eid, sess["id"]))
        return sess

    def delete(self, eid, sid):
        if self.raise_on_delete:
            exc, self.raise_on_delete = self.raise_on_delete, None
            raise exc
        existed = self.sessions.pop(sid, None) is not None
        self.calls.append(("delete", eid, sid))
        return existed

    def append_message(self, eid, sid, message):
        if self.raise_on_append:
            exc, self.raise_on_append = self.raise_on_append, None
            raise exc
        self.calls.append(("append", eid, sid, message))
        return self.append_result


class _FakeManager:
    def __init__(self):
        self.calls = []
        self.create_result = {"id": "s-new", "name": "n", "cwd": "/c",
                              "model": "m", "provider_id": None,
                              "runner": None, "node_id": "primary"}

    def create(self, **kw):
        self.calls.append(("create", kw))
        return dict(self.create_result)

    def append_user_msg(self, sid, msg):
        self.calls.append(("append_user", sid, msg))
        return msg

    def append_assistant_msg(self, sid, msg):
        self.calls.append(("append_assistant", sid, msg))
        return msg

    def update_running_content(self, sid, mid, content):
        self.calls.append(("update_content", sid, mid, content))
        return True

    def set_streaming(self, sid, mid, streaming):
        self.calls.append(("set_streaming", sid, mid, streaming))
        return True

    def apply_session_field(self, sid, field, value):
        self.calls.append(("apply_field", sid, field, value))
        return {"id": sid, field: value}


class _FakeConfigStore:
    def __init__(self):
        self.providers = {"prov1": {"id": "prov1"}}
        self.default_model = "default-model"

    def get_provider(self, pid):
        return self.providers.get(pid)

    def default_session_model(self):
        return self.default_model


class _Fakes:
    """Holds every swapped collaborator and exposes grant/deny helpers."""

    def __init__(self):
        self.coord = _FakeCoordinator()
        self.guards = _FakeGuards()
        self.estore = _FakeExtensionStore()
        self.vstore = _FakeVirtualStore()
        self.manager = _FakeManager()
        self.config = _FakeConfigStore()
        self.calls = []
        self.inject_result = {"ok": True}
        self.inject_raise = None
        self.run_result = {"run": True}
        self.run_raise = None
        self.headless_result = {"result": "x"}
        self.headless_raise = None
        self.ownership_owner = True
        self.providers_runner_result = "native-runner"
        self.resolved_provider = ""

    # permission / ownership helpers used across tests
    def grant(self, *perms, eid="ext1"):
        self.estore.grant(eid=eid, perms=perms)
        return eid


def _build_in_function_modules(fakes):
    """extension_session_ownership / extension_managed_runs / headless_admission
    are imported INSIDE route bodies, so patch sys.modules at call time."""
    ownership = types.ModuleType("extension_session_ownership")
    ownership.is_owner = lambda sid, eid: fakes.ownership_owner
    ownership.claim = lambda sid, eid: fakes.calls.append(("claim", sid, eid))

    managed = types.ModuleType("extension_managed_runs")

    async def _run(coord, **kw):
        fakes.calls.append(("managed_run", kw))
        if fakes.run_raise:
            exc, fakes.run_raise = fakes.run_raise, None
            raise exc
        return fakes.run_result
    managed.run = _run

    headless = types.ModuleType("headless_admission")

    async def _headless(sid, **kw):
        fakes.calls.append(("headless", sid, kw))
        if fakes.headless_raise:
            exc, fakes.headless_raise = fakes.headless_raise, None
            raise exc
        return fakes.headless_result
    headless.run_session_headless = _headless

    return ownership, managed, headless


def _build_synthetic(fakes):
    synth = types.ModuleType("synthetic_messages")

    async def _inject(coord, sid, **kw):
        fakes.calls.append(("inject", sid, kw))
        if fakes.inject_raise:
            exc, fakes.inject_raise = fakes.inject_raise, None
            raise exc
        return fakes.inject_result
    synth.inject = _inject
    return synth


def _build_projection(fakes):
    proj = types.ModuleType("ui_selection_projection")

    async def _close_tabs(session_ids):
        fakes.calls.append(("close_tabs", session_ids))
    proj.close_tabs_for_deleted = _close_tabs
    return proj


@pytest.fixture
def fakes():
    """Swap every collaborator on the router namespace; restore on teardown."""
    f = _Fakes()
    prior = {
        "guards": api.internal_guards,
        "estore": api.extension_store,
        "vstore": api.virtual_session_store,
        "manager": api.session_manager,
        "config": api.config_store,
        "synthetic": api.synthetic_messages,
        "projection": api.ui_selection_projection,
        "_provider_runner": api._provider_runner,
        "_resolve_provider_id_ref": api._resolve_provider_id_ref,
        "_session_exists": api._session_exists,
        "_session_lite": api._session_lite,
        "_coordinator_ref": api._coordinator_ref,
    }
    ownership, managed, headless = _build_in_function_modules(f)
    prior_sys = {
        "extension_session_ownership": sys.modules.get("extension_session_ownership"),
        "extension_managed_runs": sys.modules.get("extension_managed_runs"),
        "headless_admission": sys.modules.get("headless_admission"),
    }
    sys.modules["extension_session_ownership"] = ownership
    sys.modules["extension_managed_runs"] = managed
    sys.modules["headless_admission"] = headless

    async def _session_exists(sid):
        return f.manager.create_result["id"] == sid or sid in {"exists", "vs1"}

    async def _session_lite(sid):
        if sid in {"missing", ""}:
            return None
        if sid == "exists":
            return {"id": "exists", "queued_prompts": [], "status": "idle",
                    "field_a": "A", "secret": "S"}
        return None

    async def _resolve_provider(ref):
        return f.resolved_provider or None

    api.internal_guards = f.guards
    api.extension_store = f.estore
    api.virtual_session_store = f.vstore
    api.session_manager = f.manager
    api.config_store = f.config
    api.synthetic_messages = _build_synthetic(f)
    api.ui_selection_projection = _build_projection(f)
    api._provider_runner = lambda pid, runner: f.providers_runner_result
    api._resolve_provider_id_ref = _resolve_provider
    api._session_exists = _session_exists
    api._session_lite = _session_lite
    api.configure(coordinator=f.coord)

    yield f

    api.internal_guards = prior["guards"]
    api.extension_store = prior["estore"]
    api.virtual_session_store = prior["vstore"]
    api.session_manager = prior["manager"]
    api.config_store = prior["config"]
    api.synthetic_messages = prior["synthetic"]
    api.ui_selection_projection = prior["projection"]
    api._provider_runner = prior["_provider_runner"]
    api._resolve_provider_id_ref = prior["_resolve_provider_id_ref"]
    api._session_exists = prior["_session_exists"]
    api._session_lite = prior["_session_lite"]
    api._coordinator_ref = prior["_coordinator_ref"]
    for name, mod in prior_sys.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


def _err(coro):
    """Run an awaited route coroutine and capture its HTTPException."""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(coro)
    return exc.value


def _err_sync(fn):
    """Invoke a sync gate function and capture its HTTPException."""
    with pytest.raises(HTTPException) as exc:
        fn()
    return exc.value


# --- _coordinator / configure ---------------------------------------------


class TestCoordinatorBinding:
    def test_unconfigured_raises_503(self, fakes):
        api._coordinator_ref = None
        with pytest.raises(HTTPException) as exc:
            api._coordinator()
        assert exc.value.status_code == 503

    def test_configured_returns_coordinator(self, fakes):
        assert api._coordinator() is fakes.coord


# --- _require_extension_permission ----------------------------------------


class TestPermissionGate:
    def test_invalid_authority_rejected(self, fakes):
        fakes.guards.valid = False
        err = _err_sync(lambda: api._require_extension_permission("tok", "session_state"))
        assert err.status_code == 403

    def test_empty_extension_id_rejected(self, fakes):
        fakes.guards.extension_id = ""
        err = _err_sync(lambda: api._require_extension_permission("tok", "session_state"))
        assert err.status_code == 403

    def test_unknown_extension_rejected(self, fakes):
        err = _err_sync(lambda: api._require_extension_permission("tok", "session_state"))
        assert err.status_code == 403

    def test_inactive_extension_rejected(self, fakes):
        fakes.estore.grant(eid="ext1", perms=["session_state"], active=False)
        err = _err_sync(lambda: api._require_extension_permission("tok", "session_state"))
        assert err.status_code == 403

    def test_missing_permission_rejected(self, fakes):
        fakes.estore.grant(eid="ext1", perms=["other"])
        err = _err_sync(lambda: api._require_extension_permission("tok", "session_state"))
        assert err.status_code == 403

    def test_granted_returns_extension_id(self, fakes):
        fakes.grant("session_state")
        assert api._require_extension_permission("tok", "session_state") == "ext1"


# --- _require_extension_session_ownership ----------------------------------


class TestOwnershipGate:
    def test_invalid_authority_rejected(self, fakes):
        fakes.guards.valid = False
        err = _err_sync(lambda: api._require_extension_session_ownership("t", {"session_id": "s"}))
        assert err.status_code == 403

    def test_not_owner_rejected(self, fakes):
        fakes.ownership_owner = False
        err = _err_sync(lambda: api._require_extension_session_ownership("t", {"session_id": "s"}))
        assert err.status_code == 403

    def test_owner_returns_ids(self, fakes):
        fakes.ownership_owner = True
        ext, sid = api._require_extension_session_ownership("t", {"session_id": " s1 "})
        assert ext == "ext1"
        assert sid == "s1"  # stripped


# --- virtual sessions ------------------------------------------------------


class TestVirtualSessionsUpsert:
    def test_permission_denied(self, fakes):
        err = _err(api.internal_virtual_sessions_upsert({"id": "x"}, "tok"))
        assert err.status_code == 403

    def test_body_not_dict(self, fakes):
        fakes.grant("session_state")
        err = _err(api.internal_virtual_sessions_upsert("nope", "tok"))
        assert err.status_code == 400

    def test_value_error(self, fakes):
        fakes.grant("session_state")
        fakes.vstore.raise_on_upsert = ValueError("bad")
        err = _err(api.internal_virtual_sessions_upsert({"id": "x"}, "tok"))
        assert err.status_code == 400

    def test_permission_error(self, fakes):
        fakes.grant("session_state")
        fakes.vstore.raise_on_upsert = PermissionError("nope")
        err = _err(api.internal_virtual_sessions_upsert({"id": "x"}, "tok"))
        assert err.status_code == 403

    def test_create_new_session(self, fakes):
        fakes.grant("session_state")
        fakes.vstore.sessions.clear()  # get() -> None -> created
        res = asyncio.run(api.internal_virtual_sessions_upsert({"id": "vs1"}, "tok"))
        assert res["success"] is True
        assert fakes.coord.broadcasts[0][0] == "session_created"

    def test_update_existing_session(self, fakes):
        fakes.grant("session_state")
        fakes.vstore.sessions["vs1"] = {"id": "vs1"}  # get() truthy -> updated
        res = asyncio.run(api.internal_virtual_sessions_upsert({"id": "vs1"}, "tok"))
        assert res["success"] is True
        assert fakes.coord.broadcasts[0][0] == "session_metadata_updated"


class TestVirtualSessionsDelete:
    def test_permission_denied(self, fakes):
        err = _err(api.internal_virtual_sessions_delete({}, "tok"))
        assert err.status_code == 403

    def test_missing_session_id(self, fakes):
        fakes.grant("session_state")
        err = _err(api.internal_virtual_sessions_delete({}, "tok"))
        assert err.status_code == 400

    def test_permission_error(self, fakes):
        fakes.grant("session_state")
        fakes.vstore.raise_on_delete = PermissionError("no")
        err = _err(api.internal_virtual_sessions_delete({"session_id": "s"}, "tok"))
        assert err.status_code == 403

    def test_deleted_broadcasts_and_closes_tabs(self, fakes):
        fakes.grant("session_state")
        fakes.vstore.sessions["s"] = {"id": "s"}
        res = asyncio.run(api.internal_virtual_sessions_delete({"session_id": "s"}, "tok"))
        assert res == {"success": True, "deleted": True}
        assert fakes.coord.broadcasts[0][0] == "session_deleted"
        assert ("close_tabs", ["s"]) in fakes.calls

    def test_not_deleted_no_broadcast(self, fakes):
        fakes.grant("session_state")
        res = asyncio.run(api.internal_virtual_sessions_delete({"session_id": "ghost"}, "tok"))
        assert res == {"success": True, "deleted": False}
        assert fakes.coord.broadcasts == []


class TestVirtualSessionsAppendMessage:
    def test_permission_denied(self, fakes):
        err = _err(api.internal_virtual_sessions_append_message({}, "tok"))
        assert err.status_code == 403

    def test_missing_fields(self, fakes):
        fakes.grant("session_state")
        err = _err(api.internal_virtual_sessions_append_message({"session_id": "s"}, "tok"))
        assert err.status_code == 400

    def test_key_error(self, fakes):
        fakes.grant("session_state")
        fakes.vstore.raise_on_append = KeyError("nope")
        err = _err(api.internal_virtual_sessions_append_message(
            {"session_id": "s", "message": {"id": "m"}}, "tok"))
        assert err.status_code == 404

    def test_value_error(self, fakes):
        fakes.grant("session_state")
        fakes.vstore.raise_on_append = ValueError("bad")
        err = _err(api.internal_virtual_sessions_append_message(
            {"session_id": "s", "message": {"id": "m"}}, "tok"))
        assert err.status_code == 400

    def test_permission_error(self, fakes):
        fakes.grant("session_state")
        fakes.vstore.raise_on_append = PermissionError("no")
        err = _err(api.internal_virtual_sessions_append_message(
            {"session_id": "s", "message": {"id": "m"}}, "tok"))
        assert err.status_code == 403

    def test_happy_appends_and_broadcasts(self, fakes):
        fakes.grant("session_state")
        res = asyncio.run(api.internal_virtual_sessions_append_message(
            {"session_id": "s", "message": {"id": "m"}}, "tok"))
        assert res["success"] is True
        assert res["message"] == {"id": "m1"}
        assert fakes.coord.broadcasts[0][0] == "session_metadata_updated"


# --- synthetic messages ----------------------------------------------------


class TestSyntheticInject:
    def test_permission_denied(self, fakes):
        err = _err(api.internal_synthetic_messages_inject({}, "tok"))
        assert err.status_code == 403

    def test_body_not_dict(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_synthetic_messages_inject("nope", "tok"))
        assert err.status_code == 400

    def test_value_error(self, fakes):
        fakes.grant("spawn_runs")
        fakes.inject_raise = ValueError("bad")
        err = _err(api.internal_synthetic_messages_inject({"session_id": "s"}, "tok"))
        assert err.status_code == 400

    def test_capability_contexts_list_passthrough(self, fakes):
        fakes.grant("spawn_runs")
        asyncio.run(api.internal_synthetic_messages_inject(
            {"session_id": "s", "capability_contexts": ["c1"]}, "tok"))
        assert fakes.calls[0][2]["capability_contexts"] == ["c1"]

    def test_capability_contexts_non_list_becomes_none(self, fakes):
        fakes.grant("spawn_runs")
        asyncio.run(api.internal_synthetic_messages_inject(
            {"session_id": "s", "capability_contexts": "x"}, "tok"))
        assert fakes.calls[0][2]["capability_contexts"] is None

    def test_happy(self, fakes):
        fakes.grant("spawn_runs")
        res = asyncio.run(api.internal_synthetic_messages_inject(
            {"session_id": "s", "prompt": "p"}, "tok"))
        assert res == {"ok": True}


# --- managed run (env-var validation is the security core) -----------------


class TestManagedRun:
    def test_permission_denied(self, fakes):
        err = _err(api.internal_managed_run({}, "tok"))
        assert err.status_code == 403

    def test_body_not_dict(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_managed_run("nope", "tok"))
        assert err.status_code == 400

    def test_missing_managed_session_id(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_managed_run({}, "tok"))
        assert err.status_code == 400

    def test_not_owner(self, fakes):
        fakes.grant("spawn_runs")
        fakes.ownership_owner = False
        err = _err(api.internal_managed_run({"managed_session_id": "s"}, "tok"))
        assert err.status_code == 403

    def test_extra_env_not_dict(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_managed_run(
            {"managed_session_id": "s", "extra_env": "x"}, "tok"))
        assert err.status_code == 400

    def test_extra_env_non_str_key(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_managed_run(
            {"managed_session_id": "s", "extra_env": {1: "v"}}, "tok"))
        assert err.status_code == 400

    def test_extra_env_empty_or_nullbyte_key(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_managed_run(
            {"managed_session_id": "s", "extra_env": {"  ": "v"}}, "tok"))
        assert err.status_code == 400
        err = _err(api.internal_managed_run(
            {"managed_session_id": "s", "extra_env": {"a\x00b": "v"}}, "tok"))
        assert err.status_code == 400

    def test_extra_env_undeclared_key(self, fakes):
        fakes.grant("spawn_runs")
        fakes.estore.grant(eid="ext1", perms=["spawn_runs"], managed_run_env=["OK"])
        err = _err(api.internal_managed_run(
            {"managed_session_id": "s", "extra_env": {"NOPE": "v"}}, "tok"))
        assert err.status_code == 403

    def test_extra_env_nullbyte_or_too_long_value(self, fakes):
        fakes.grant("spawn_runs")
        fakes.estore.grant(eid="ext1", perms=["spawn_runs"], managed_run_env=["OK"])
        err = _err(api.internal_managed_run(
            {"managed_session_id": "s", "extra_env": {"OK": "v\x00x"}}, "tok"))
        assert err.status_code == 400
        err = _err(api.internal_managed_run(
            {"managed_session_id": "s", "extra_env": {"OK": "x" * 4097}}, "tok"))
        assert err.status_code == 400

    def test_value_error_from_run(self, fakes):
        fakes.grant("spawn_runs")
        fakes.run_raise = ValueError("bad")
        err = _err(api.internal_managed_run({"managed_session_id": "s"}, "tok"))
        assert err.status_code == 400

    def test_happy_only_declared_env_passed(self, fakes):
        fakes.grant("spawn_runs")
        fakes.estore.grant(eid="ext1", perms=["spawn_runs"], managed_run_env=["A", "B"])
        res = asyncio.run(api.internal_managed_run(
            {"managed_session_id": "s", "extra_env": {"A": "1", "B": "2"}}, "tok"))
        assert res == {"run": True}
        assert fakes.calls[0][0] == "managed_run"
        assert fakes.calls[0][1]["extra_env"] == {"A": "1", "B": "2"}


# --- managed-run create-session -------------------------------------------


class TestManagedRunCreateSession:
    def test_permission_denied(self, fakes):
        err = _err(api.internal_managed_run_create_session({}, "tok"))
        assert err.status_code == 403

    def test_body_not_dict(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_managed_run_create_session("nope", "tok"))
        assert err.status_code == 400

    def test_missing_parent(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_managed_run_create_session(
            {"parent_session_id": "missing"}, "tok"))
        assert err.status_code == 400

    def test_missing_name_or_cwd(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_managed_run_create_session({}, "tok"))
        assert err.status_code == 400

    def test_unknown_provider(self, fakes):
        fakes.grant("spawn_runs")
        fakes.resolved_provider = "prov-missing"
        err = _err(api.internal_managed_run_create_session(
            {"name": "n", "cwd": "/c", "provider_id": "prov-missing"}, "tok"))
        assert err.status_code == 400

    def test_happy_claims_and_returns(self, fakes):
        fakes.grant("spawn_runs")
        res = asyncio.run(api.internal_managed_run_create_session(
            {"name": "n", "cwd": "/c"}, "tok"))
        assert res["success"] is True
        assert res["session_id"] == "s-new"
        assert ("claim", "s-new", "ext1") in fakes.calls
        assert fakes.coord.panels == []  # no parent -> no panel

    def test_explicit_runner_skips_parent_default(self, fakes):
        fakes.grant("spawn_runs")
        seen = {}

        def _runner(pid, runner):
            seen["args"] = (pid, runner)
            return "resolved-runner"
        api._provider_runner = _runner
        res = asyncio.run(api.internal_managed_run_create_session(
            {"name": "n", "cwd": "/c", "runner": "explicit"}, "tok"))
        assert res["success"] is True
        assert seen["args"][1] == "explicit"

    def test_happy_with_parent_emits_panel(self, fakes):
        fakes.grant("spawn_runs")
        fakes.manager.create_result = {"id": "s-new", "name": "n", "cwd": "/c",
                                       "model": "m", "provider_id": None,
                                       "runner": None, "node_id": "primary"}
        # parent "exists" resolves a session_lite dict
        async def _lite_parent(sid):
            return {"cwd": "/p", "provider_id": None, "model": "pm", "node_id": "pn"}
        api._session_lite = _lite_parent
        res = asyncio.run(api.internal_managed_run_create_session(
            {"name": "n", "parent_session_id": "exists"}, "tok"))
        assert res["success"] is True
        assert fakes.coord.panels  # parent -> panel emitted


# --- headless run (heavy type-validation surface) -------------------------


class TestHeadlessRun:
    def test_permission_denied(self, fakes):
        err = _err(api.internal_headless_run({}, "tok"))
        assert err.status_code == 403

    def test_body_not_dict(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_headless_run("nope", "tok"))
        assert err.status_code == 400

    def test_invalid_fields(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_headless_run({"bogus": 1}, "tok"))
        assert err.status_code == 400

    def test_missing_app_session_id(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_headless_run({"prompt": "p"}, "tok"))
        assert err.status_code == 400

    def test_app_session_id_wrong_type(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_headless_run({"app_session_id": 5, "prompt": "p"}, "tok"))
        assert err.status_code == 400

    def test_missing_prompt(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_headless_run({"app_session_id": "s"}, "tok"))
        assert err.status_code == 400

    def test_bool_field_wrong_type(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_headless_run(
            {"app_session_id": "s", "prompt": "p", "fork": "yes"}, "tok"))
        assert err.status_code == 400

    def test_fork_and_resume_mutually_exclusive(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_headless_run(
            {"app_session_id": "s", "prompt": "p", "fork": True, "resume": True}, "tok"))
        assert err.status_code == 400

    def test_expected_field_wrong_type(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_headless_run(
            {"app_session_id": "s", "prompt": "p", "expected_model": 5}, "tok"))
        assert err.status_code == 400

    def test_timeout_wrong_type(self, fakes):
        fakes.grant("spawn_runs")
        err = _err(api.internal_headless_run(
            {"app_session_id": "s", "prompt": "p", "timeout": "x"}, "tok"))
        assert err.status_code == 400

    def test_value_error_maps_422(self, fakes):
        fakes.grant("spawn_runs")
        fakes.headless_raise = ValueError("bad")
        err = _err(api.internal_headless_run(
            {"app_session_id": "s", "prompt": "p"}, "tok"))
        assert err.status_code == 422

    def test_generic_exception_maps_502(self, fakes):
        fakes.grant("spawn_runs")
        fakes.headless_raise = RuntimeError("boom")
        err = _err(api.internal_headless_run(
            {"app_session_id": "s", "prompt": "p"}, "tok"))
        assert err.status_code == 502

    def test_happy_returns_result(self, fakes):
        fakes.grant("spawn_runs")
        res = asyncio.run(api.internal_headless_run(
            {"app_session_id": "s", "prompt": "p", "fork": True, "timeout": 5}, "tok"))
        assert res == {"result": "x"}


# --- session messages (ownership-gated) ------------------------------------


class TestSessionMessagesAppend:
    def test_ownership_denied(self, fakes):
        fakes.ownership_owner = False
        err = _err(api.internal_session_messages_append({"session_id": "s"}, "tok"))
        assert err.status_code == 403

    def test_bad_role(self, fakes):
        err = _err(api.internal_session_messages_append(
            {"session_id": "s", "role": "system"}, "tok"))
        assert err.status_code == 400

    def test_user_path(self, fakes):
        res = asyncio.run(api.internal_session_messages_append(
            {"session_id": "s", "role": "user", "content": "hi"}, "tok"))
        assert res["success"] is True
        assert res["message"]["role"] == "user"
        assert fakes.manager.calls[0][0] == "append_user"

    def test_assistant_path_sets_streaming_flag(self, fakes):
        res = asyncio.run(api.internal_session_messages_append(
            {"session_id": "s", "role": "assistant", "content": "hi",
             "is_streaming": True, "message_id": "m9"}, "tok"))
        assert res["message"]["id"] == "m9"
        assert res["message"]["isStreaming"] is True
        assert res["message"]["events"] == []
        assert fakes.manager.calls[0][0] == "append_assistant"

    def test_session_not_found(self, fakes):
        fakes.manager.append_user_msg = lambda sid, msg: None
        err = _err(api.internal_session_messages_append(
            {"session_id": "s", "role": "user", "content": "hi"}, "tok"))
        assert err.status_code == 404


class TestSessionMessagesUpdateContent:
    def test_ownership_denied(self, fakes):
        fakes.ownership_owner = False
        err = _err(api.internal_session_messages_update_content(
            {"session_id": "s"}, "tok"))
        assert err.status_code == 403

    def test_missing_message_id(self, fakes):
        err = _err(api.internal_session_messages_update_content(
            {"session_id": "s"}, "tok"))
        assert err.status_code == 400

    def test_not_found(self, fakes):
        fakes.manager.update_running_content = lambda *a: None
        err = _err(api.internal_session_messages_update_content(
            {"session_id": "s", "message_id": "m"}, "tok"))
        assert err.status_code == 404

    def test_happy(self, fakes):
        res = asyncio.run(api.internal_session_messages_update_content(
            {"session_id": "s", "message_id": "m", "content": "c"}, "tok"))
        assert res == {"success": True}


class TestSessionMessagesSetStreaming:
    def test_ownership_denied(self, fakes):
        fakes.ownership_owner = False
        err = _err(api.internal_session_messages_set_streaming(
            {"session_id": "s"}, "tok"))
        assert err.status_code == 403

    def test_missing_message_id(self, fakes):
        err = _err(api.internal_session_messages_set_streaming(
            {"session_id": "s"}, "tok"))
        assert err.status_code == 400

    def test_not_found(self, fakes):
        fakes.manager.set_streaming = lambda *a: None
        err = _err(api.internal_session_messages_set_streaming(
            {"session_id": "s", "message_id": "m", "streaming": True}, "tok"))
        assert err.status_code == 404

    def test_happy(self, fakes):
        res = asyncio.run(api.internal_session_messages_set_streaming(
            {"session_id": "s", "message_id": "m", "streaming": True}, "tok"))
        assert res == {"success": True}


# --- scoped session-field write / read -------------------------------------


class TestSessionFieldWrite:
    def test_invalid_authority(self, fakes):
        fakes.guards.valid = False
        err = _err(api.internal_session_field({"field": "f"}, "tok"))
        assert err.status_code == 403

    def test_no_allowlist(self, fakes):
        err = _err(api.internal_session_field({"field": "f"}, "tok"))
        assert err.status_code == 403

    def test_field_not_allowed(self, fakes):
        fakes.estore.grant(eid="ext1", perms=[], mutates=["allowed"])
        err = _err(api.internal_session_field({"field": "forbidden"}, "tok"))
        assert err.status_code == 403

    def test_session_not_found(self, fakes):
        fakes.estore.grant(eid="ext1", perms=[], mutates=["f"])
        err = _err(api.internal_session_field(
            {"field": "f", "session_id": "missing"}, "tok"))
        assert err.status_code == 404

    def test_value_error(self, fakes):
        fakes.estore.grant(eid="ext1", perms=[], mutates=["f"])
        def _raise(*a):
            raise ValueError("bad")
        fakes.manager.apply_session_field = _raise
        err = _err(api.internal_session_field(
            {"field": "f", "session_id": "exists"}, "tok"))
        assert err.status_code == 400

    def test_apply_returns_none(self, fakes):
        fakes.estore.grant(eid="ext1", perms=[], mutates=["f"])
        fakes.manager.apply_session_field = lambda *a: None
        err = _err(api.internal_session_field(
            {"field": "f", "session_id": "exists"}, "tok"))
        assert err.status_code == 404

    def test_happy(self, fakes):
        fakes.estore.grant(eid="ext1", perms=[], mutates=["f"])
        res = asyncio.run(api.internal_session_field(
            {"field": "f", "session_id": "exists", "value": "v"}, "tok"))
        assert res == {"success": True}


class TestSessionFieldsRead:
    def test_invalid_authority(self, fakes):
        fakes.guards.valid = False
        err = _err(api.internal_session_fields({"session_id": "s"}, "tok"))
        assert err.status_code == 403

    def test_extension_inactive(self, fakes):
        fakes.guards.extension_id = "ghost"
        err = _err(api.internal_session_fields({"session_id": "s"}, "tok"))
        assert err.status_code == 403

    def test_session_not_found(self, fakes):
        fakes.estore.grant(eid="ext1", perms=[], reads=["f"])
        err = _err(api.internal_session_fields(
            {"session_id": "missing", "fields": ["f"]}, "tok"))
        assert err.status_code == 404

    def test_fields_not_list(self, fakes):
        fakes.estore.grant(eid="ext1", perms=[], reads=["f"])
        err = _err(api.internal_session_fields(
            {"session_id": "exists", "fields": "f"}, "tok"))
        assert err.status_code == 400

    def test_fields_non_str_items(self, fakes):
        fakes.estore.grant(eid="ext1", perms=[], reads=["f"])
        err = _err(api.internal_session_fields(
            {"session_id": "exists", "fields": [1]}, "tok"))
        assert err.status_code == 400

    def test_filters_to_read_allowlist(self, fakes):
        fakes.estore.grant(eid="ext1", perms=[], reads=["field_a"])
        res = asyncio.run(api.internal_session_fields(
            {"session_id": "exists", "fields": ["field_a", "secret"]}, "tok"))
        assert res["fields"] == {"field_a": "A"}  # secret filtered out

    def test_session_lite_none(self, fakes):
        fakes.estore.grant(eid="ext1", perms=[], reads=["f"])
        async def _none(sid):
            return None
        api._session_lite = _none
        err = _err(api.internal_session_fields(
            {"session_id": "exists", "fields": ["f"]}, "tok"))
        assert err.status_code == 404


# --- session activity ------------------------------------------------------


class TestSessionActivity:
    def test_permission_denied(self, fakes):
        err = _err(api.internal_session_activity({}, "tok"))
        assert err.status_code == 403

    def test_missing_session_id(self, fakes):
        fakes.grant("session_state")
        err = _err(api.internal_session_activity({}, "tok"))
        assert err.status_code == 400

    def test_session_not_found(self, fakes):
        fakes.grant("session_state")
        err = _err(api.internal_session_activity({"session_id": "missing"}, "tok"))
        assert err.status_code == 404

    def test_happy_snapshot(self, fakes):
        fakes.grant("session_state")
        res = asyncio.run(api.internal_session_activity({"session_id": "exists"}, "tok"))
        assert res["success"] is True
        assert "is_running" in res
        assert res["idle"] is True


class TestActivitySnapshot:
    def test_non_dict_session(self, fakes):
        snap = api._session_activity_snapshot("s", "not-a-dict")
        assert snap["queued_prompts_count"] == 0
        assert snap["idle"] is True

    def test_queued_prompts_not_list(self, fakes):
        snap = api._session_activity_snapshot("s", {"queued_prompts": "nope"})
        assert snap["queued_prompts_count"] == 0

    def test_queued_from_session_when_coordinator_empty(self, fakes):
        snap = api._session_activity_snapshot("s", {"queued_prompts": [1, 2, 3]})
        assert snap["queued_prompts_count"] == 3

    def test_queued_takes_max_with_coordinator(self, fakes):
        fakes.coord.queued["s"] = 5
        fakes.coord.running["s"] = False
        snap = api._session_activity_snapshot("s", {"queued_prompts": [1]})
        assert snap["queued_prompts_count"] == 5
        assert snap["idle"] is False

    def test_running_flag_from_coordinator(self, fakes):
        fakes.coord.running["s"] = True
        snap = api._session_activity_snapshot("s", {})
        assert snap["is_running"] is True
        assert snap["idle"] is False

    def test_monitoring_state_from_coordinator(self, fakes):
        fakes.coord.monitoring["s"] = "watching"
        snap = api._session_activity_snapshot("s", {})
        assert snap["monitoring_state"] == "watching"
