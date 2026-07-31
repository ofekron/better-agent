"""Unit-tier coverage of the internal-LLM settings endpoints in
internal_orchestration_api.py (get_internal_llm_endpoint /
set_internal_llm_endpoint).

These two endpoints are unguarded internal routes (no X-Internal-Token role
check) whose only collaborators are config_store + extension_store (disk-backed,
isolated home) plus a configured coordinator for the PUT's broadcast. That
makes them a clean, self-contained slice: called directly as async functions,
no HTTP client, no MCP wiring, no provider runtime.

Isolated BETTER_AGENT_HOME via _test_home.isolate; the router's configure()
collaborators are fakes, restored on teardown so nothing bleeds into other
test files in the same run.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import _test_home

_TMP_HOME = _test_home.isolate("bc-test-internal-llm-")

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi import HTTPException  # noqa: E402
import pytest  # noqa: E402

import config_store  # noqa: E402
import extension_store  # noqa: E402
import internal_orchestration_api  # noqa: E402


class _FakeCoordinator:
    def __init__(self):
        self.broadcasts: list[tuple] = []

    async def broadcast_global(self, event: str, payload):
        self.broadcasts.append((event, payload))


async def _noop_true(*args, **kwargs):
    return True


async def _noop_workers(*args, **kwargs):
    return {"workers": []}


@pytest.fixture
def coordinator():
    """Configure the router with a fake coordinator; restore prior bindings
    on teardown so this file does not disturb other test modules."""
    prior = (
        internal_orchestration_api._coordinator_ref,
        internal_orchestration_api._delete_session_tree,
        internal_orchestration_api._provision_workers,
    )
    coord = _FakeCoordinator()
    internal_orchestration_api.configure(
        coordinator=coord,
        delete_session_tree=_noop_true,
        provision_workers=_noop_workers,
    )
    yield coord
    internal_orchestration_api._coordinator_ref = prior[0]
    internal_orchestration_api._delete_session_tree = prior[1]
    internal_orchestration_api._provision_workers = prior[2]


async def _set(body):
    return await internal_orchestration_api.set_internal_llm_endpoint(body)


async def _get():
    return await internal_orchestration_api.get_internal_llm_endpoint()


# -------------------------------------------------------------------- GET


class TestGet:
    def test_returns_core_tasks_and_empty_assignments(self, coordinator):
        result = asyncio.run(_get())
        # default_session is the only user-editable core task here; the
        # delegation_* tasks are role-mapped (team-orchestration) and filtered
        # out of the public internal-llm settings view.
        assert result["tasks"] == ["default_session"]
        assert "delegation_task" not in result["tasks"]
        assert result["assignments"] == {}
        assert result["labels"] == {}

    def test_reflects_persisted_assignments(self, coordinator):
        asyncio.run(
            _set({"assignments": {"default_session": {"model": "m1"}}})
        )
        result = asyncio.run(_get())
        assert result["assignments"] == {"default_session": {"model": "m1"}}


# -------------------------------------------------------------------- PUT validation


class TestPutValidation:
    def test_rejects_non_dict_assignments(self, coordinator):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_set({"assignments": ["not", "a", "dict"]}))
        assert exc.value.status_code == 400

    def test_rejects_extension_owned_keys(self, coordinator, monkeypatch):
        # Extension-owned tasks must be edited in extension settings, not here.
        monkeypatch.setattr(
            extension_store,
            "extension_internal_llm_task_keys",
            lambda: {"ext.owned"},
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_set({"assignments": {"ext.owned": {"model": "x"}}}))
        assert exc.value.status_code == 403


# -------------------------------------------------------------------- PUT happy path


class TestPutHappyPath:
    def test_persists_normalizes_and_broadcasts(self, coordinator):
        # Unknown fields and blank strings are dropped by normalization;
        # only known tasks with non-empty string fields persist.
        result = asyncio.run(
            _set(
                {
                    "assignments": {
                        "default_session": {
                            "model": "  m1  ",
                            "reasoning_effort": "",
                            "bogus_field": "ignored",
                        },
                        "not_a_real_task": {"model": "z"},
                    }
                }
            )
        )
        assert result["assignments"] == {
            "default_session": {"model": "m1"}
        }
        # Broadcast fired exactly once.
        assert coordinator.broadcasts == [("internal_llm_changed", {})]
        # Persisted to the store.
        assert config_store.get_internal_llm_assignments() == {
            "default_session": {"model": "m1"}
        }

    def test_put_update_replaces_prior_value(self, coordinator):
        # default_session is the only non-role core task; the merge base keeps
        # ONLY extension-owned current entries (none here), so a second PUT
        # for the same task updates the stored value rather than accumulating.
        asyncio.run(
            _set({"assignments": {"default_session": {"model": "m1"}}})
        )
        assert config_store.get_internal_llm_assignments() == {
            "default_session": {"model": "m1"}
        }
        result = asyncio.run(
            _set({"assignments": {"default_session": {"model": "m2"}}})
        )
        assert result["assignments"] == {"default_session": {"model": "m2"}}
        assert config_store.get_internal_llm_assignments() == {
            "default_session": {"model": "m2"}
        }
