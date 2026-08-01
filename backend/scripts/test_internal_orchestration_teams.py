"""Unit-tier coverage of the team-orchestration endpoints and the team
activation engine in internal_orchestration_api.py:

- delegate-task-policy get/set
- team-definitions list / plan
- teams create / register-member
- team-definitions activate (endpoint) + activate GET
- team-definitions finalize
- _run_team_definition_activation / _rollback_team_activation (the engine)

The stores (config_store, team_store, team_activation_store, extension_store,
team_definitions) are real and disk-backed under an isolated home; only the
cross-subsystem collaborators that this module does not own are faked — the
internal-token authority guard (shared stub), request-principal session
existence (`_session_lite`), provider-id resolution (`_resolve_provider_id_ref`),
and the injected worker-provisioning / session-tree-deletion callbacks bound
by `configure()`. This keeps the tests focused on this router's validation,
branching, and orchestration logic without spinning a coordinator or a real
session/process.

Isolated BETTER_AGENT_HOME via _test_home.isolate; collaborators are fakes
restored on teardown so nothing bleeds into other test modules in the run.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-internal-teams-")

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi import HTTPException  # noqa: E402
import pytest  # noqa: E402

import config_store  # noqa: E402
import extension_store  # noqa: E402
import internal_orchestration_api  # noqa: E402
import team_activation_store  # noqa: E402
import team_store  # noqa: E402
import _test_authority  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _Recorder:
    """Async callable that records every invocation and returns a configurable
    result. Used for the injected provision_workers / delete_session_tree
    callbacks so tests can assert what the engine asked them to do."""

    def __init__(self, result):
        self.calls: list = []
        self.result = result
        self.raise_on: set[int] = set()  # 1-based call indices that should raise

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if len(self.calls) in self.raise_on:
            raise RuntimeError(f"injected callback raised on call {len(self.calls)}")
        return self.result


class _FakeCoordinator:
    def __init__(self):
        self.broadcasts: list[tuple] = []

    async def broadcast_global(self, event: str, payload):
        self.broadcasts.append((event, payload))


@pytest.fixture
def configured():
    """Bind fakes to the router and stub the authority guard; restore prior
    bindings on teardown so this file does not disturb other test modules.

    The team endpoints call `require_builtin_runtime_extension` directly (not
    only via `require_role_internal`), so that gate is stubbed here too — the
    guard has its own dedicated coverage; stubbing it keeps these handler
    tests focused on validation/orchestration logic."""
    import internal_guards

    prior = (
        internal_orchestration_api._coordinator_ref,
        internal_orchestration_api._delete_session_tree,
        internal_orchestration_api._provision_workers,
        internal_guards.require_builtin_runtime_extension,
    )
    provisioner = _Recorder({"workers": [{"created": True, "agent_session_id": "w-session"}]})
    deleter = _Recorder(True)
    coord = _FakeCoordinator()
    internal_orchestration_api.configure(
        coordinator=coord,
        delete_session_tree=deleter,
        provision_workers=provisioner,
    )
    internal_guards.require_builtin_runtime_extension = lambda *_a, **_k: None
    with _test_authority.stub_internal_authority():
        yield _Bindings(coord, provisioner, deleter)
    internal_guards.require_builtin_runtime_extension = prior[3]
    internal_orchestration_api._coordinator_ref = prior[0]
    internal_orchestration_api._delete_session_tree = prior[1]
    internal_orchestration_api._provision_workers = prior[2]


class _Bindings:
    def __init__(self, coord, provisioner, deleter):
        self.coord = coord
        self.provisioner = provisioner
        self.deleter = deleter


def _session_exists(monkeypatch, exists: bool):
    """Stub the (async) session-existence check imported into the router."""
    async def _stub(_sid):
        return {"id": _sid} if exists else None

    monkeypatch.setattr(internal_orchestration_api, "_session_lite", _stub)


def _resolve_provider(monkeypatch, value: str = ""):
    monkeypatch.setattr(
        internal_orchestration_api,
        "_resolve_provider_id_ref",
        lambda _ref: _async_identity(value),
    )


async def _async_identity(value):
    return value


# ============================================================ delegate policy


class TestDelegateTaskPolicy:
    def test_get_returns_default_then_persisted(self, configured):
        # Default policy normalizes to "auto".
        first = asyncio.run(
            internal_orchestration_api.internal_get_delegate_task_policy_endpoint()
        )
        assert first["policy"] == "auto"
        config_store.set_delegate_task_policy("manual")
        second = asyncio.run(
            internal_orchestration_api.internal_get_delegate_task_policy_endpoint()
        )
        assert second["policy"] == "manual"

    def test_set_normalizes_known_policy(self, configured):
        result = asyncio.run(
            internal_orchestration_api.internal_set_delegate_task_policy_endpoint(
                {"policy": "always_new"}
            )
        )
        assert result["policy"] == "always_new"
        assert config_store.get_delegate_task_policy() == "always_new"

    def test_set_unknown_policy_falls_back_to_auto(self, configured):
        # An unrecognized policy is normalized back to the "auto" default.
        result = asyncio.run(
            internal_orchestration_api.internal_set_delegate_task_policy_endpoint(
                {"policy": "balanced"}
            )
        )
        assert result["policy"] == "auto"
        assert config_store.get_delegate_task_policy() == "auto"

    def test_set_blank_falls_back_to_auto(self, configured):
        result = asyncio.run(
            internal_orchestration_api.internal_set_delegate_task_policy_endpoint({})
        )
        assert result["policy"] == "auto"


# ============================================================ team-definitions


class TestTeamDefinitionList:
    def test_list_returns_sources(self, configured):
        result = asyncio.run(
            internal_orchestration_api.internal_list_extension_team_definitions()
        )
        assert result == {"team_definitions": extension_store.team_definition_sources()}

    def test_list_maps_extension_error_to_400(self, configured, monkeypatch):
        def _boom():
            raise extension_store.ExtensionError("corpus unreadable")

        monkeypatch.setattr(extension_store, "team_definition_sources", _boom)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                internal_orchestration_api.internal_list_extension_team_definitions()
            )
        assert exc.value.status_code == 400
        assert "corpus unreadable" in exc.value.detail


class TestTeamDefinitionPlan:
    def test_plan_missing_source_is_400(self, configured):
        # build_plan rejects an unknown source_id with TeamDefinitionError.
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                internal_orchestration_api.internal_plan_team_definition(
                    {"source_id": "", "profile": ""}
                )
            )
        assert exc.value.status_code == 400

    def test_plan_maps_extension_error_to_400(self, configured, monkeypatch):
        import team_definitions

        def _boom(*_a, **_kw):
            raise extension_store.ExtensionError("source load failed")

        monkeypatch.setattr(team_definitions, "build_plan", _boom)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                internal_orchestration_api.internal_plan_team_definition(
                    {"source_id": "src", "profile": ""}
                )
            )
        assert exc.value.status_code == 400
        assert "source load failed" in exc.value.detail

    def test_plan_passes_body_fields_through(self, configured, monkeypatch):
        import team_definitions

        captured: dict = {}

        def _fake_build(*, source_id, profile, team_instance_id, variables):
            captured.update(
                source_id=source_id,
                profile=profile,
                team_instance_id=team_instance_id,
                variables=variables,
            )
            return {"team_instance_id": team_instance_id, "activate": []}

        monkeypatch.setattr(team_definitions, "build_plan", _fake_build)
        result = asyncio.run(
            internal_orchestration_api.internal_plan_team_definition(
                {
                    "source_id": "src-1",
                    "profile": "prof-1",
                    "team_instance_id": "team-7",
                    "variables": {"x": 1},
                }
            )
        )
        assert result["plan"]["team_instance_id"] == "team-7"
        assert captured == {
            "source_id": "src-1",
            "profile": "prof-1",
            "team_instance_id": "team-7",
            "variables": {"x": 1},
        }


# ============================================================ teams / members


class TestCreateTeam:
    def test_requires_root_session_id(self, configured):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(internal_orchestration_api.internal_create_team({}))
        assert exc.value.status_code == 400
        assert "root_session_id is required" in exc.value.detail

    def test_unknown_root_session_is_400(self, configured, monkeypatch):
        _session_exists(monkeypatch, exists=False)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                internal_orchestration_api.internal_create_team(
                    {"root_session_id": "ghost"}
                )
            )
        assert exc.value.status_code == 400
        assert "does not exist" in exc.value.detail

    def test_creates_team(self, configured, monkeypatch):
        _session_exists(monkeypatch, exists=True)
        result = asyncio.run(
            internal_orchestration_api.internal_create_team(
                {
                    "root_session_id": "root-1",
                    "definition_ref": "src",
                    "profile": "prof",
                    "team_id": "team-happy",
                }
            )
        )
        assert result["success"] is True
        assert result["team"]["id"] == "team-happy"
        assert team_store.get("team-happy") is not None

    def test_invalid_team_id_is_400(self, configured, monkeypatch):
        # _clean_id rejects path separators as team_id; the store raises
        # TeamStoreError, which the handler maps to 400.
        _session_exists(monkeypatch, exists=True)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                internal_orchestration_api.internal_create_team(
                    {"root_session_id": "root-1", "team_id": "bad/id"}
                )
            )
        assert exc.value.status_code == 400


class TestRegisterMember:
    def test_registers_member(self, configured, monkeypatch):
        _resolve_provider(monkeypatch, value="prov-1")
        team_store.create(root_session_id="root-1", team_id="team-m")
        result = asyncio.run(
            internal_orchestration_api.internal_register_team_member(
                {
                    "team_instance_id": "team-m",
                    "member_id": "worker-a",
                    "member_type": "worker",
                    "agent_session_id": "sess-a",
                    "role": "builder",
                    "cwd": "/repo",
                    "model": "m",
                }
            )
        )
        assert result["success"] is True
        assert result["member"]["id"] == "worker-a"
        assert result["member"]["provider_id"] == "prov-1"

    def test_invalid_member_type_is_400(self, configured, monkeypatch):
        _resolve_provider(monkeypatch)
        team_store.create(root_session_id="root-1", team_id="team-mt")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                internal_orchestration_api.internal_register_team_member(
                    {"team_instance_id": "team-mt", "member_type": "bogus"}
                )
            )
        assert exc.value.status_code == 400


# ============================================================ activation


def _eager_task(monkeypatch, captured: list):
    """Patch asyncio.create_task inside the router so the activation engine
    runs on the caller's loop and can be awaited deterministically from the
    test (instead of leaking as a fire-and-forget background task)."""
    real_create_task = asyncio.create_task

    def _capture(coro, **kwargs):
        task = real_create_task(coro)
        captured.append(task)
        return task

    monkeypatch.setattr(internal_orchestration_api.asyncio, "create_task", _capture)


class TestActivateTeamDefinition:
    def test_requires_root_session_id(self, configured):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(internal_orchestration_api.internal_activate_team_definition({}))
        assert exc.value.status_code == 400
        assert "root_session_id is required" in exc.value.detail

    def test_unknown_root_session_is_400(self, configured, monkeypatch):
        _session_exists(monkeypatch, exists=False)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                internal_orchestration_api.internal_activate_team_definition(
                    {"root_session_id": "ghost"}
                )
            )
        assert exc.value.status_code == 400

    def test_plan_must_be_object(self, configured, monkeypatch):
        _session_exists(monkeypatch, exists=True)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                internal_orchestration_api.internal_activate_team_definition(
                    {"root_session_id": "root-1", "plan": ["not", "an", "object"]}
                )
            )
        assert exc.value.status_code == 400
        assert "plan must be an object" in exc.value.detail

    def test_activates_and_runs_engine(self, configured, monkeypatch):
        _session_exists(monkeypatch, exists=True)
        captured: list = []
        _eager_task(monkeypatch, captured)
        plan = {
            "team_instance_id": "team-act",
            "profile": "prof",
            "manager": {"id": "mgr"},
            "activate": [{"member_id": "w1", "role_key": "worker"}],
        }

        async def drive():
            res = await internal_orchestration_api.internal_activate_team_definition(
                {"root_session_id": "root-1", "plan": plan}
            )
            # Let the eagerly-captured engine task finish before the loop ends.
            if captured:
                await asyncio.gather(*captured)
            return res

        result = asyncio.run(drive())
        assert result["success"] is True
        activation_id = result["activation"]["id"]
        # Engine completed: team + manager + worker all provisioned.
        finished = team_activation_store.get(activation_id)
        assert finished["status"] == "complete"
        assert team_store.get("team-act") is not None

    def test_builds_plan_when_none_given(self, configured, monkeypatch):
        # No explicit plan: the endpoint derives one from source_id/profile via
        # team_definitions.build_plan, and injects a team_instance_id when the
        # built plan omits one.
        import team_definitions

        _session_exists(monkeypatch, exists=True)
        captured: list = []
        _eager_task(monkeypatch, captured)
        monkeypatch.setattr(
            team_definitions,
            "build_plan",
            lambda **kw: {"profile": "prof", "activate": [{"member_id": "w1"}]},
        )

        async def drive():
            res = await internal_orchestration_api.internal_activate_team_definition(
                {"root_session_id": "root-1", "source_id": "src", "profile": "prof"}
            )
            if captured:
                await asyncio.gather(*captured)
            return res

        result = asyncio.run(drive())
        assert result["success"] is True
        # A team_instance_id was generated and threaded into the activation.
        assert result["activation"]["team_instance_id"].startswith("team-")

    def test_build_failure_when_no_plan_is_400(self, configured, monkeypatch):
        import team_definitions

        _session_exists(monkeypatch, exists=True)
        monkeypatch.setattr(
            team_definitions,
            "build_plan",
            lambda **kw: (_ for _ in ()).throw(
                team_definitions.TeamDefinitionError("no such source")
            ),
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                internal_orchestration_api.internal_activate_team_definition(
                    {"root_session_id": "root-1", "source_id": "missing"}
                )
            )
        assert exc.value.status_code == 400
        assert "no such source" in exc.value.detail

    def test_plan_without_team_instance_id_is_injected(self, configured, monkeypatch):
        # A provided plan that omits team_instance_id gets one generated.
        _session_exists(monkeypatch, exists=True)
        captured: list = []
        _eager_task(monkeypatch, captured)

        async def drive():
            res = await internal_orchestration_api.internal_activate_team_definition(
                {"root_session_id": "root-1", "plan": {"activate": []}}
            )
            if captured:
                await asyncio.gather(*captured)
            return res

        result = asyncio.run(drive())
        assert result["success"] is True
        assert result["activation"]["team_instance_id"].startswith("team-")


class TestGetActivation:
    def test_get_returns_activation(self, configured):
        created = team_activation_store.create(
            root_session_id="root-1",
            team_instance_id="team-g",
            source_id="src",
            profile="prof",
        )
        result = asyncio.run(
            internal_orchestration_api.internal_get_team_definition_activation(
                created["id"]
            )
        )
        assert result["success"] is True
        assert result["activation"]["id"] == created["id"]

    def test_unknown_activation_is_404(self, configured):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                internal_orchestration_api.internal_get_team_definition_activation(
                    "nope"
                )
            )
        assert exc.value.status_code == 404

    def test_corrupt_activation_is_400(self, configured, monkeypatch):
        def _corrupt(_aid):
            raise team_activation_store.TeamActivationError("schema wrecked")

        monkeypatch.setattr(team_activation_store, "get", _corrupt)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                internal_orchestration_api.internal_get_team_definition_activation(
                    "wrecked"
                )
            )
        assert exc.value.status_code == 400
        assert "schema wrecked" in exc.value.detail


class TestFinalize:
    def test_requires_team_instance_id(self, configured):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                internal_orchestration_api.internal_finalize_team_definition_member(
                    {"member_id": "m"}
                )
            )
        assert exc.value.status_code == 400

    def test_requires_member_id(self, configured):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                internal_orchestration_api.internal_finalize_team_definition_member(
                    {"team_instance_id": "t"}
                )
            )
        assert exc.value.status_code == 400

    def test_unknown_team_is_404(self, configured):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                internal_orchestration_api.internal_finalize_team_definition_member(
                    {"team_instance_id": "ghost", "member_id": "m"}
                )
            )
        assert exc.value.status_code == 404

    def test_no_pending_member_is_404(self, configured):
        team_store.create(root_session_id="root-1", team_id="team-f")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                internal_orchestration_api.internal_finalize_team_definition_member(
                    {"team_instance_id": "team-f", "member_id": "none"}
                )
            )
        assert exc.value.status_code == 404

    def test_finalizes_pending_worker(self, configured):
        team_store.create(root_session_id="root-1", team_id="team-fin")
        spec = {"member_id": "deferred", "role_key": "worker", "cwd": "/repo"}
        team_store.set_pending_members("team-fin", [spec])
        result = asyncio.run(
            internal_orchestration_api.internal_finalize_team_definition_member(
                {"team_instance_id": "team-fin", "member_id": "deferred"}
            )
        )
        assert result["success"] is True
        assert result["workers"] == [{"created": True, "agent_session_id": "w-session"}]
        # Provisioner was handed the popped spec + team context.
        body = configured.provisioner.calls[0][0][0]
        assert body["team_instance_id"] == "team-fin"
        assert body["workers"] == [spec]

    def test_unconfigured_provisioner_is_503(self, configured):
        team_store.create(root_session_id="root-1", team_id="team-503")
        team_store.set_pending_members(
            "team-503", [{"member_id": "deferred"}]
        )
        internal_orchestration_api._provision_workers = None
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                internal_orchestration_api.internal_finalize_team_definition_member(
                    {"team_instance_id": "team-503", "member_id": "deferred"}
                )
            )
        assert exc.value.status_code == 503


# ============================================================ activation engine


class TestRunTeamDefinitionActivation:
    def test_happy_path_completes(self, configured):
        act = team_activation_store.create(
            root_session_id="root-1",
            team_instance_id="team-run",
            source_id="src",
            profile="prof",
        )
        asyncio.run(
            _run_with_id(
                act["id"],
                root_session_id="root-1",
                plan={
                    "team_instance_id": "team-run",
                    "profile": "prof",
                    "manager": {"id": "mgr", "cwd": "/m"},
                    "activate": [{"member_id": "w1", "role_key": "worker"}],
                },
                default_cwd="/repo",
                bare_config=False,
            )
        )
        assert team_activation_store.get(act["id"])["status"] == "complete"
        assert team_store.get("team-run") is not None
        # Worker provisioning recorded the created session for potential rollback.
        assert configured.provisioner.calls

    def test_finalize_with_registers_deferred_workers(self, configured):
        act = team_activation_store.create(
            root_session_id="root-1",
            team_instance_id="team-fw",
            source_id="src",
            profile="prof",
        )
        asyncio.run(
            _run_with_id(
                act["id"],
                root_session_id="root-1",
                plan={
                    "team_instance_id": "team-fw",
                    "profile": "prof",
                    "manager": {"id": "mgr"},
                    "activate": [{"member_id": "w1"}],
                    "finalize_with": [{"member_id": "later", "role_key": "worker"}],
                },
                default_cwd="/repo",
                bare_config=False,
            )
        )
        team = team_store.get("team-fw")
        # Deferred worker staged as pending finalize.
        assert team_store.pop_pending_member("team-fw", "later") is not None

    def test_bad_activate_items_rolls_back_and_fails(self, configured):
        act = team_activation_store.create(
            root_session_id="root-1",
            team_instance_id="team-bad",
            source_id="src",
            profile="prof",
        )
        asyncio.run(
            _run_with_id(
                act["id"],
                root_session_id="root-1",
                plan={
                    "team_instance_id": "team-bad",
                    "profile": "prof",
                    "manager": {"id": "mgr"},
                    "activate": ["not-a-dict"],
                },
                default_cwd="/repo",
                bare_config=False,
            )
        )
        failed = team_activation_store.get(act["id"])
        assert failed["status"] == "failed"
        # Rollback deleted the partially-created team.
        assert team_store.get("team-bad") is None

    def test_activate_not_a_list_rolls_back_and_fails(self, configured):
        # plan.activate must be a list; a non-list value aborts after the team
        # and manager are created, triggering rollback.
        act = team_activation_store.create(
            root_session_id="root-1",
            team_instance_id="team-nl",
            source_id="src",
            profile="prof",
        )
        asyncio.run(
            _run_with_id(
                act["id"],
                root_session_id="root-1",
                plan={
                    "team_instance_id": "team-nl",
                    "profile": "prof",
                    "manager": {"id": "mgr"},
                    "activate": "not-a-list",
                },
                default_cwd="/repo",
                bare_config=False,
            )
        )
        assert team_activation_store.get(act["id"])["status"] == "failed"
        assert team_store.get("team-nl") is None

    def test_provision_failure_rolls_back_created_worker(self, configured):
        act = team_activation_store.create(
            root_session_id="root-1",
            team_instance_id="team-pf",
            source_id="src",
            profile="prof",
        )
        # First provision call raises; the engine should treat it as failure
        # and roll back. The created-worker list is empty (provision raised),
        # so rollback only deletes the team.
        configured.provisioner.raise_on = {1}
        asyncio.run(
            _run_with_id(
                act["id"],
                root_session_id="root-1",
                plan={
                    "team_instance_id": "team-pf",
                    "profile": "prof",
                    "manager": {"id": "mgr"},
                    "activate": [{"member_id": "w1"}],
                },
                default_cwd="/repo",
                bare_config=False,
            )
        )
        assert team_activation_store.get(act["id"])["status"] == "failed"
        assert team_store.get("team-pf") is None


async def _run_with_id(activation_id, **kwargs):
    """Thin wrapper so each engine test passes a real activation_id."""
    await internal_orchestration_api._run_team_definition_activation(
        activation_id, **kwargs
    )


class TestRollbackTeamActivation:
    def test_deletes_created_workers_and_team(self, configured):
        team_store.create(root_session_id="root-1", team_id="team-rb")
        rolled = asyncio.run(
            internal_orchestration_api._rollback_team_activation(
                "team-rb", ["sess-1", "sess-2"]
            )
        )
        assert rolled == ["sess-1", "sess-2"]
        assert configured.deleter.calls[0][0] == ("sess-1",)
        assert configured.deleter.calls[1][0] == ("sess-2",)
        assert team_store.get("team-rb") is None

    def test_continues_past_delete_failure(self, configured):
        team_store.create(root_session_id="root-1", team_id="team-rbf")
        # Second session delete raises; rollback must log + continue and still
        # delete the team + return the sessions that did roll back.
        configured.deleter.raise_on = {2}
        rolled = asyncio.run(
            internal_orchestration_api._rollback_team_activation(
                "team-rbf", ["sess-ok", "sess-bad", "sess-ok2"]
            )
        )
        assert rolled == ["sess-ok", "sess-ok2"]
        # Team cleanup still ran despite a per-session failure.
        assert team_store.get("team-rbf") is None

    def test_empty_team_id_skips_team_delete(self, configured):
        rolled = asyncio.run(
            internal_orchestration_api._rollback_team_activation("", ["sess-1"])
        )
        assert rolled == ["sess-1"]

    def test_team_delete_failure_is_logged_not_fatal(self, configured, monkeypatch):
        # Sessions rolled back fine, but team_store.delete raises — rollback
        # must log + continue and still report the rolled-back sessions.
        team_store.create(root_session_id="root-1", team_id="team-tdf")

        def _boom(_tid):
            raise RuntimeError("disk full")

        monkeypatch.setattr(team_store, "delete", _boom)
        rolled = asyncio.run(
            internal_orchestration_api._rollback_team_activation(
                "team-tdf", ["sess-1"]
            )
        )
        assert rolled == ["sess-1"]
