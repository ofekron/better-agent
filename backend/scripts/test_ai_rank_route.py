"""Security + contract tests for `POST /api/internal/ai-rank`.

Locks:
  - internal-token required (403 without/wrong)
  - calling extension must be active AND declare `spawn_runs` (403 otherwise,
    including a core token that has no extension identity)
  - the extension must declare the requested `kind` under
    `permissions.ai_rank_kinds` (403 for an undeclared kind)
  - bad body shapes -> 400, never 500
  - happy path forwards query/candidates/max_results to `ai_rank.rank` under
    the extension's declared task_key and returns its response verbatim

Run via: ./scripts/run-backend-tests.sh -- -k test_ai_rank_route
"""
from __future__ import annotations

import os
import sys

import _test_home
_TMP_HOME = _test_home.isolate_installed("bc-test-ai-rank-route-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"
os.environ["BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH"] = _TMP_HOME

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import ai_rank  # noqa: E402
import extension_store  # noqa: E402
import extension_token_registry  # noqa: E402
import main  # noqa: E402

CLIENT = TestClient(main.app, client=("127.0.0.1", 50002))
TOKEN = main.coordinator.internal_token

RANK_EXT = "test.ai-rank-ext"
NOSPAWN_EXT = "test.ai-rank-nospawn-ext"
UNDECLARED_EXT = "test.ai-rank-undeclared-ext"

_SENTINEL = object()


def _seed_extension(extension_id: str, *, spawn_runs: bool, ai_rank_kinds: dict | None = None) -> None:
    data = extension_store._load()
    permissions: dict = {}
    if spawn_runs:
        permissions["spawn_runs"] = True
    if ai_rank_kinds is not None:
        permissions["ai_rank_kinds"] = ai_rank_kinds
    data["extensions"][extension_id] = {
        "manifest": {"id": extension_id, "permissions": permissions},
        "enabled": True,
        "source": {"type": "git", "install_path": ""},
        "entitlement": {"status": "not_required"},
    }
    extension_store._save(data)


_seed_extension(RANK_EXT, spawn_runs=True, ai_rank_kinds={"test_route_kind": "test_route_task"})
_seed_extension(NOSPAWN_EXT, spawn_runs=False, ai_rank_kinds={"test_route_kind": "test_route_task"})
_seed_extension(UNDECLARED_EXT, spawn_runs=True, ai_rank_kinds={"other_kind": "other_task"})


def _post(body=None, *, token=_SENTINEL, extension_id=RANK_EXT):
    if token is _SENTINEL:
        token = TOKEN if extension_id is None else extension_token_registry.mint(extension_id)
    headers = {}
    if token is not None:
        headers["X-Internal-Token"] = token
    return CLIENT.post("/api/internal/ai-rank", json=body, headers=headers)


@pytest.fixture(autouse=True)
def _mock_provisioning_run(monkeypatch):
    """No real LLM turns: mock provisioning.run for every test in this
    module, defaulting to a plain successful worker reply."""
    from provisioning.manager import ProvisionedResult

    calls: list = []

    async def _fake_run(spec, query, ctx=None, *, model=None):
        calls.append({"spec": spec, "query": query, "ctx": ctx})
        candidates = (ctx or {}).get("candidates") or []
        ids = [c["id"] for c in candidates][: (ctx or {}).get("max_results") or len(candidates)]
        return ProvisionedResult(
            text="",
            value={"ids": ids, "reasoning": "matched"},
            config=None,
            base_session_id="base",
            caller_session_id="caller",
            dispatch_result={"events": []},
        )

    monkeypatch.setattr(ai_rank.provisioning, "run", _fake_run)
    return calls


def test_missing_or_wrong_token_rejected():
    r = _post({"kind": "test_route_kind", "query": "q", "candidates": [{"id": "a"}]}, token="wrong-token")
    assert r.status_code == 403
    r = _post({"kind": "test_route_kind", "query": "q", "candidates": [{"id": "a"}]}, token=None)
    assert r.status_code in (403, 422)


def test_core_token_without_extension_identity_rejected():
    r = _post({"kind": "test_route_kind", "query": "q", "candidates": [{"id": "a"}]}, extension_id=None)
    assert r.status_code == 403


def test_extension_without_spawn_runs_rejected():
    r = _post(
        {"kind": "test_route_kind", "query": "q", "candidates": [{"id": "a"}]},
        extension_id=NOSPAWN_EXT,
    )
    assert r.status_code == 403


def test_unknown_extension_rejected():
    r = _post(
        {"kind": "test_route_kind", "query": "q", "candidates": [{"id": "a"}]},
        extension_id="test.not-installed-at-all",
    )
    assert r.status_code == 403


def test_undeclared_kind_rejected():
    r = _post(
        {"kind": "test_route_kind", "query": "q", "candidates": [{"id": "a"}]},
        extension_id=UNDECLARED_EXT,
    )
    assert r.status_code == 403

    r = _post(
        {"kind": "some_kind_this_extension_never_declared", "query": "q", "candidates": [{"id": "a"}]},
        extension_id=RANK_EXT,
    )
    assert r.status_code == 403


def test_bad_body_shapes_return_400_not_500():
    r = _post({"query": "q", "candidates": [{"id": "a"}]})
    assert r.status_code == 400  # missing kind
    r = _post({"kind": "", "query": "q", "candidates": [{"id": "a"}]})
    assert r.status_code == 400  # empty kind
    r = _post({"kind": "test_route_kind", "query": 5, "candidates": [{"id": "a"}]})
    assert r.status_code == 400  # query not a string
    r = _post({"kind": "test_route_kind", "query": "q", "candidates": "not-a-list"})
    assert r.status_code == 400  # candidates not a list -> ai_rank.rank's ValueError
    r = _post({"kind": "test_route_kind", "query": "q", "candidates": [{"id": ""}]})
    assert r.status_code == 400  # candidate with empty id -> ai_rank.rank's ValueError


def test_happy_path_dispatches_under_declared_task_key(_mock_provisioning_run):
    r = _post({
        "kind": "test_route_kind",
        "query": "find it",
        "candidates": [{"id": "a"}, {"id": "b"}],
        "max_results": 1,
    })
    assert r.status_code == 200
    body = r.json()
    assert body == {"ids": ["a"], "reasoning": "matched", "error": None}

    calls = _mock_provisioning_run
    assert len(calls) == 1
    assert calls[0]["spec"].task_key == "test_route_task"
    assert calls[0]["spec"].key == "ai_rank:test_route_kind"
    assert calls[0]["query"] == "find it"
    assert calls[0]["ctx"]["candidates"] == [{"id": "a"}, {"id": "b"}]
    assert calls[0]["ctx"]["max_results"] == 1


def test_default_max_results_when_omitted(_mock_provisioning_run):
    r = _post({"kind": "test_route_kind", "query": "q", "candidates": [{"id": "a"}]})
    assert r.status_code == 200
    calls = _mock_provisioning_run
    assert calls[0]["ctx"]["max_results"] == 20


def test_error_taxonomy_passes_through_verbatim():
    r = _post({"kind": "test_route_kind", "query": "   ", "candidates": [{"id": "a"}]})
    assert r.status_code == 200
    assert r.json() == {"ids": [], "reasoning": "", "error": "empty_query"}
