"""Unit tests for native_index_api.py — pure parsing helpers + API-router
delegation/gating, exercised in-process via TestClient (no spawned backend,
no live model). The manager singleton and the internal guard are mocked at
the import seam."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import native_index_api
from native_index_api import _parse_limit, _parse_project_paths, _parse_provider_ids


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(native_index_api.router)
    return app


# --------------------------------------------------------------------------- #
# _parse_provider_ids
# --------------------------------------------------------------------------- #
def test_provider_ids_non_dict_body_returns_none():
    assert _parse_provider_ids(None) is None


def test_provider_ids_omitted_returns_none():
    assert _parse_provider_ids({}) is None


def test_provider_ids_explicit_none_returns_none():
    assert _parse_provider_ids({"provider_ids": None}) is None


def test_provider_ids_list_passes_through():
    assert _parse_provider_ids({"provider_ids": ["claude", "codex"]}) == ["claude", "codex"]


def test_provider_ids_non_list_raises_400():
    with pytest.raises(HTTPException) as exc:
        _parse_provider_ids({"provider_ids": "claude"})
    assert exc.value.status_code == 400
    assert "provider_ids must be a list" in exc.value.detail


# --------------------------------------------------------------------------- #
# _parse_limit
# --------------------------------------------------------------------------- #
def test_limit_omitted_returns_none():
    assert _parse_limit({}) is None


def test_limit_none_returns_none():
    assert _parse_limit({"limit": None}) is None


def test_limit_non_dict_returns_none():
    assert _parse_limit(None) is None


def test_limit_int_passes_through():
    assert _parse_limit({"limit": 5}) == 5


def test_limit_zero_collapses_to_none():
    # `limit or None` — a zero cap is treated as "no cap".
    assert _parse_limit({"limit": 0}) is None


def test_limit_non_int_raises_400():
    with pytest.raises(HTTPException) as exc:
        _parse_limit({"limit": "five"})
    assert exc.value.status_code == 400
    assert exc.value.detail == "limit must be an integer"


def test_limit_negative_raises_400():
    with pytest.raises(HTTPException) as exc:
        _parse_limit({"limit": -1})
    assert exc.value.status_code == 400
    assert exc.value.detail == "limit must be >= 0"


# --------------------------------------------------------------------------- #
# _parse_project_paths (async)
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_project_paths_all_projects_returns_none():
    assert await _parse_project_paths({"all_projects": True}) is None


@pytest.mark.anyio
async def test_project_paths_omitted_loads_manager_defaults():
    loaded = ["/a", "/b"]
    with patch.object(
        native_index_api.manager, "loaded_project_paths", new=AsyncMock(return_value=loaded)
    ):
        assert await _parse_project_paths({}) == loaded


@pytest.mark.anyio
async def test_project_paths_non_list_raises_400():
    with pytest.raises(HTTPException) as exc:
        await _parse_project_paths({"project_paths": "/a"})
    assert exc.value.status_code == 400
    assert "project_paths must be a list" in exc.value.detail


@pytest.mark.anyio
async def test_project_paths_filters_non_string_and_empty_entries():
    raw = ["/keep", 123, "", None, "/also", "   "]
    # only non-empty strings survive; whitespace-only is truthy so it stays.
    result = await _parse_project_paths({"project_paths": raw})
    assert result == ["/keep", "/also", "   "]


# --------------------------------------------------------------------------- #
# Routes — public
# --------------------------------------------------------------------------- #
def test_start_native_import_passes_parsed_args_to_manager():
    fake_manager = MagicMock()
    fake_manager.start_import = AsyncMock(return_value={"job": "running"})
    fake_manager.loaded_project_paths = AsyncMock(return_value=["/cwd"])
    with patch.object(native_index_api, "manager", fake_manager):
        client = TestClient(_app())
        resp = client.post("/api/native-import", json={"provider_ids": ["claude"], "limit": 3})
    assert resp.status_code == 200
    assert resp.json() == {"job": "running"}
    fake_manager.start_import.assert_awaited_once_with(["claude"], 3, ["/cwd"])


def test_start_native_import_rejects_bad_provider_ids():
    with patch.object(native_index_api, "manager", MagicMock()):
        client = TestClient(_app())
        resp = client.post("/api/native-import", json={"provider_ids": "claude"})
    assert resp.status_code == 400


def test_start_native_import_rejects_bad_limit():
    with patch.object(native_index_api, "manager", MagicMock()):
        client = TestClient(_app())
        resp = client.post("/api/native-import", json={"limit": "x"})
    assert resp.status_code == 400


def test_native_import_status_delegates():
    fake_manager = MagicMock()
    fake_manager.import_status = AsyncMock(return_value={"state": "idle"})
    with patch.object(native_index_api, "manager", fake_manager):
        client = TestClient(_app())
        resp = client.get("/api/native-import/status")
    assert resp.status_code == 200
    assert resp.json() == {"state": "idle"}
    fake_manager.import_status.assert_awaited_once()


def test_native_import_summary_splits_provider_ids():
    fake_manager = MagicMock()
    fake_manager.import_summary = AsyncMock(return_value={"n": 2})
    with patch.object(native_index_api, "manager", fake_manager):
        client = TestClient(_app())
        resp = client.get("/api/native-import/summary", params={"provider_ids": "claude,codex"})
    assert resp.status_code == 200
    fake_manager.import_summary.assert_awaited_once_with(["claude", "codex"], False)


def test_native_import_summary_all_projects_true():
    fake_manager = MagicMock()
    fake_manager.import_summary = AsyncMock(return_value={"n": 9})
    with patch.object(native_index_api, "manager", fake_manager):
        client = TestClient(_app())
        resp = client.get("/api/native-import/summary", params={"all_projects": "true"})
    assert resp.status_code == 200
    fake_manager.import_summary.assert_awaited_once_with(None, True)


def test_native_import_summary_no_args_passes_none_false():
    fake_manager = MagicMock()
    fake_manager.import_summary = AsyncMock(return_value={"n": 0})
    with patch.object(native_index_api, "manager", fake_manager):
        client = TestClient(_app())
        resp = client.get("/api/native-import/summary")
    assert resp.status_code == 200
    fake_manager.import_summary.assert_awaited_once_with(None, False)


# --------------------------------------------------------------------------- #
# Routes — internal (X-Internal-Token gated)
# --------------------------------------------------------------------------- #
def test_internal_start_native_import_requires_valid_token():
    # require_internal() raises 403 when authority is invalid (no request principal).
    with patch.object(native_index_api, "manager", MagicMock()):
        client = TestClient(_app())
        resp = client.post(
            "/api/internal/native-import",
            json={},
            headers={"X-Internal-Token": "anything"},
        )
    assert resp.status_code == 403


def test_internal_start_native_import_passes_when_authorized():
    fake_manager = MagicMock()
    fake_manager.start_import = AsyncMock(return_value={"job": "ok"})
    fake_manager.loaded_project_paths = AsyncMock(return_value=[])
    with patch.object(native_index_api, "manager", fake_manager), patch.object(
        native_index_api, "require_internal", lambda: None
    ):
        client = TestClient(_app())
        resp = client.post(
            "/api/internal/native-import",
            json={"limit": 2},
            headers={"X-Internal-Token": "t"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"job": "ok"}
    fake_manager.start_import.assert_awaited_once_with(None, 2, [])


def test_internal_native_import_status_passes_when_authorized():
    fake_manager = MagicMock()
    fake_manager.import_status = AsyncMock(return_value={"state": "done"})
    with patch.object(native_index_api, "manager", fake_manager), patch.object(
        native_index_api, "require_internal", lambda: None
    ):
        client = TestClient(_app())
        resp = client.get(
            "/api/internal/native-import/status",
            headers={"X-Internal-Token": "t"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"state": "done"}


def test_internal_start_missing_token_header_is_unprocessable():
    # Header(...) is required by FastAPI -> 422 before the handler runs.
    with patch.object(native_index_api, "manager", MagicMock()):
        client = TestClient(_app())
        resp = client.post("/api/internal/native-import", json={})
    assert resp.status_code == 422
