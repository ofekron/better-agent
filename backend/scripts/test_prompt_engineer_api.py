"""Unit coverage for prompt_engineer_api.

The orchestration capabilities (`_submit_prompt` etc.) are injected through
`configure`; the heavy deps (`config_store`, `prompt_engineer`, `i18n.t`,
`require_role_internal`) are patched at the import seam so the FastAPI
routes can be exercised through an in-process TestClient with no real
orchestration, filesystem, or model work.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import prompt_engineer_api

TOK = {"X-Internal-Token": "internal-token"}


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(prompt_engineer_api.router)
    return app


@pytest.fixture
def deps():
    """Wire the module with seam-mocked deps and configured async callables."""
    submit = AsyncMock()
    cancel = AsyncMock()
    session_lite = AsyncMock()
    fanout = AsyncMock()
    prompt_engineer_api.configure(submit, cancel, session_lite, fanout)

    pe = MagicMock()
    pe.is_eng_session = MagicMock(return_value=True)
    pe.get_for_parent = MagicMock(return_value=None)
    pe.format_comment = MagicMock(return_value="FORMATTED")
    pe.start = AsyncMock()
    pe.finalize = AsyncMock()
    pe.cleanup = AsyncMock(return_value=True)

    cs = MagicMock()
    guard = MagicMock()
    t_func = MagicMock(side_effect=lambda *a, **k: "T:" + ":".join(map(str, a)))

    with patch.object(prompt_engineer_api, "prompt_engineer", pe), \
            patch.object(prompt_engineer_api, "config_store", cs), \
            patch.object(prompt_engineer_api, "require_role_internal", guard), \
            patch.object(prompt_engineer_api, "t", t_func):
        yield {
            "client": TestClient(_build_app()),
            "pe": pe,
            "cs": cs,
            "guard": guard,
            "submit": submit,
            "cancel": cancel,
            "session_lite": session_lite,
            "fanout": fanout,
        }


# --- configure ---------------------------------------------------------------

def test_configure_binds_callables_to_module_globals():
    s, c, l, f = object(), object(), object(), object()
    prompt_engineer_api.configure(s, c, l, f)  # type: ignore[arg-type]
    assert prompt_engineer_api._submit_prompt is s
    assert prompt_engineer_api._cancel_session is c
    assert prompt_engineer_api._session_lite is l
    assert prompt_engineer_api._publish_worker_fanout is f


# --- pure helpers ------------------------------------------------------------

def test_required_session_id_strips_and_returns_present():
    assert prompt_engineer_api._required_session_id({"session_id": " abc "}) == "abc"
    assert prompt_engineer_api._required_session_id({"session_id": "x"}) == "x"


@pytest.mark.parametrize("body", [None, {}, {"session_id": ""}, {"session_id": "   "}])
def test_required_session_id_rejects_missing_or_blank(body):
    with pytest.raises(Exception) as exc:
        prompt_engineer_api._required_session_id(body)
    assert exc.value.status_code == 400  # type: ignore[attr-defined]


def test_require_eng_session_passes_when_is_eng(monkeypatch):
    monkeypatch.setattr(prompt_engineer_api.prompt_engineer, "is_eng_session", lambda _sid: True)
    assert prompt_engineer_api._require_eng_session("s") is None


def test_require_eng_session_raises_404_when_not_eng(monkeypatch):
    monkeypatch.setattr(prompt_engineer_api.prompt_engineer, "is_eng_session", lambda _sid: False)
    with pytest.raises(Exception) as exc:
        prompt_engineer_api._require_eng_session("s")
    assert exc.value.status_code == 404  # type: ignore[attr-defined]


# --- start -------------------------------------------------------------------

def test_start_fork_submits_meta_prompt_and_returns_resumed(deps):
    deps["pe"].start.return_value = {
        "eng_session_id": "e1",
        "session": {"model": "m", "cwd": "/x", "orchestration_mode": "native"},
        "temp_file_path": "/t",
        "original_content": "orig",
        "meta_prompt": "MP",
        "resumed": True,
    }
    res = deps["client"].post(
        "/api/internal/prompt-engineer/start",
        headers=TOK,
        json={"session_id": "s1", "client_id": "c1"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["eng_session_id"] == "e1"
    assert body["temp_file_path"] == "/t"
    assert body["original_content"] == "orig"
    assert body["resumed"] is True
    # _submit sampled provider env then submitted the meta-prompt.
    assert deps["cs"].apply_env_vars.called
    deps["submit"].assert_awaited_once_with("e1", {
        "prompt": "MP",
        "app_session_id": "e1",
        "model": "m",
        "cwd": "/x",
        "ws_callback": None,
        "images": None,
        "orchestration_mode": "native",
        "client_id": "c1",
    })
    deps["guard"].assert_called_once_with("prompt-engineer")


def test_start_new_mode_without_meta_prompt_skips_submit(deps):
    deps["pe"].start.return_value = {
        "eng_session_id": "e2",
        "session": {},
        "temp_file_path": "/t2",
        "original_content": "",
        "meta_prompt": None,
    }
    res = deps["client"].post(
        "/api/internal/prompt-engineer/start",
        headers=TOK,
        json={"session_id": "s1", "mode": "new"},
    )
    assert res.status_code == 200
    assert res.json()["resumed"] is False
    deps["submit"].assert_not_awaited()
    # mode new is forwarded to start.
    deps["pe"].start.assert_awaited_once_with("s1", "", "new")


def test_start_keyerror_maps_to_404(deps):
    deps["pe"].start.side_effect = KeyError
    res = deps["client"].post(
        "/api/internal/prompt-engineer/start",
        headers=TOK,
        json={"session_id": "s1"},
    )
    assert res.status_code == 404


def test_start_valueerror_maps_to_400(deps):
    deps["pe"].start.side_effect = ValueError("boom")
    res = deps["client"].post(
        "/api/internal/prompt-engineer/start",
        headers=TOK,
        json={"session_id": "s1"},
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "boom"


def test_start_invalid_mode_maps_to_400(deps):
    res = deps["client"].post(
        "/api/internal/prompt-engineer/start",
        headers=TOK,
        json={"session_id": "s1", "mode": "bogus"},
    )
    assert res.status_code == 400
    deps["pe"].start.assert_not_awaited()


def test_start_missing_session_id_maps_to_400(deps):
    res = deps["client"].post(
        "/api/internal/prompt-engineer/start",
        headers=TOK,
        json={},
    )
    assert res.status_code == 400


def test_start_missing_token_header_maps_to_422(deps):
    res = deps["client"].post(
        "/api/internal/prompt-engineer/start",
        json={"session_id": "s1"},
    )
    assert res.status_code == 422


# --- get ---------------------------------------------------------------------

def test_get_returns_live_payload(deps):
    deps["pe"].get_for_parent.return_value = {
        "eng_session_id": "e",
        "temp_file_path": "/t",
        "original_content": "o",
        "session": {"model": "m"},
    }
    res = deps["client"].post(
        "/api/internal/prompt-engineer/get",
        headers=TOK,
        json={"session_id": "s1"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["eng_session_id"] == "e"
    assert body["temp_file_path"] == "/t"
    assert body["original_content"] == "o"
    assert body["session"] == {"model": "m"}
    assert body["resumed"] is True


def test_get_no_live_session_maps_to_404(deps):
    deps["pe"].get_for_parent.return_value = None
    res = deps["client"].post(
        "/api/internal/prompt-engineer/get",
        headers=TOK,
        json={"session_id": "s1"},
    )
    assert res.status_code == 404


def test_get_missing_session_id_maps_to_400(deps):
    res = deps["client"].post(
        "/api/internal/prompt-engineer/get",
        headers=TOK,
        json={},
    )
    assert res.status_code == 400


# --- comment -----------------------------------------------------------------

_FULL_COMMENT = {
    "session_id": "s1",
    "file_path": "/a.py",
    "start_line": 1, "end_line": 2,
    "start_col": 3, "end_col": 4,
    "comment": "fix me",
    "client_id": "c1",
}


def test_comment_formats_and_submits(deps):
    deps["pe"].format_comment.return_value = "MSG"
    deps["session_lite"].return_value = {"model": "m", "cwd": "/x", "orchestration_mode": "native"}
    res = deps["client"].post(
        "/api/internal/prompt-engineer/comment",
        headers=TOK,
        json=_FULL_COMMENT,
    )
    assert res.status_code == 200
    assert res.json() == {"submitted": True}
    deps["pe"].format_comment.assert_called_once_with("/a.py", 1, 2, 3, 4, "fix me")
    deps["submit"].assert_awaited_once_with("s1", {
        "prompt": "MSG",
        "app_session_id": "s1",
        "model": "m",
        "cwd": "/x",
        "ws_callback": None,
        "images": None,
        "orchestration_mode": "native",
        "client_id": "c1",
    })


def test_comment_not_eng_session_maps_to_404(deps):
    deps["pe"].is_eng_session.return_value = False
    res = deps["client"].post(
        "/api/internal/prompt-engineer/comment",
        headers=TOK,
        json=_FULL_COMMENT,
    )
    assert res.status_code == 404
    deps["submit"].assert_not_awaited()


def test_comment_missing_field_maps_to_400(deps):
    body = dict(_FULL_COMMENT)
    del body["file_path"]
    res = deps["client"].post(
        "/api/internal/prompt-engineer/comment",
        headers=TOK,
        json=body,
    )
    assert res.status_code == 400


def test_comment_typeerror_maps_to_400(deps):
    body = dict(_FULL_COMMENT)
    body["start_line"] = None  # int(None) -> TypeError
    res = deps["client"].post(
        "/api/internal/prompt-engineer/comment",
        headers=TOK,
        json=body,
    )
    assert res.status_code == 400


def test_comment_valueerror_maps_to_400(deps):
    body = dict(_FULL_COMMENT)
    body["end_col"] = "not-a-number"  # int(...) -> ValueError
    res = deps["client"].post(
        "/api/internal/prompt-engineer/comment",
        headers=TOK,
        json=body,
    )
    assert res.status_code == 400


def test_comment_missing_session_id_maps_to_400(deps):
    res = deps["client"].post(
        "/api/internal/prompt-engineer/comment",
        headers=TOK,
        json={"file_path": "/a"},
    )
    assert res.status_code == 400


# --- result ------------------------------------------------------------------

def test_result_returns_finalized_content(deps):
    deps["pe"].finalize.return_value = "FINAL"
    deps["session_lite"].return_value = {
        "working_mode_meta": {"parent_session_id": "p", "original_content": "oc"},
    }
    res = deps["client"].post(
        "/api/internal/prompt-engineer/result",
        headers=TOK,
        json={"session_id": "s1"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["content"] == "FINAL"
    assert body["parent_session_id"] == "p"
    assert body["original_content"] == "oc"


def test_result_missing_meta_defaults_original_content(deps):
    deps["pe"].finalize.return_value = "FINAL"
    deps["session_lite"].return_value = {}  # no working_mode_meta
    res = deps["client"].post(
        "/api/internal/prompt-engineer/result",
        headers=TOK,
        json={"session_id": "s1"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["parent_session_id"] is None
    assert body["original_content"] == ""


def test_result_keyerror_maps_to_404(deps):
    deps["pe"].finalize.side_effect = KeyError
    res = deps["client"].post(
        "/api/internal/prompt-engineer/result",
        headers=TOK,
        json={"session_id": "s1"},
    )
    assert res.status_code == 404


def test_result_filenotfound_maps_to_404(deps):
    deps["pe"].finalize.side_effect = FileNotFoundError
    res = deps["client"].post(
        "/api/internal/prompt-engineer/result",
        headers=TOK,
        json={"session_id": "s1"},
    )
    assert res.status_code == 404


def test_result_valueerror_maps_to_400(deps):
    deps["pe"].finalize.side_effect = ValueError("bad")
    res = deps["client"].post(
        "/api/internal/prompt-engineer/result",
        headers=TOK,
        json={"session_id": "s1"},
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "bad"


def test_result_not_eng_session_maps_to_404(deps):
    deps["pe"].is_eng_session.return_value = False
    res = deps["client"].post(
        "/api/internal/prompt-engineer/result",
        headers=TOK,
        json={"session_id": "s1"},
    )
    assert res.status_code == 404
    deps["pe"].finalize.assert_not_awaited()


# --- cleanup -----------------------------------------------------------------

def test_cleanup_cancels_and_publishes_and_returns_deleted(deps):
    deps["pe"].cleanup.return_value = True
    res = deps["client"].post(
        "/api/internal/prompt-engineer/cleanup",
        headers=TOK,
        json={"session_id": "s1"},
    )
    assert res.status_code == 200
    assert res.json() == {"deleted": True}
    deps["cancel"].assert_awaited_once_with("s1")
    deps["pe"].cleanup.assert_awaited_once_with("s1")
    deps["fanout"].assert_awaited_once_with(
        "s1",
        op_label="prompt-eng cleanup",
        caller_scope=True,
        remove_worker=True,
        outer_log_msg="worker fan-out failed during prompt-eng cleanup",
    )


def test_cleanup_returns_deleted_false(deps):
    deps["pe"].cleanup.return_value = False
    res = deps["client"].post(
        "/api/internal/prompt-engineer/cleanup",
        headers=TOK,
        json={"session_id": "s1"},
    )
    assert res.status_code == 200
    assert res.json() == {"deleted": False}


def test_cleanup_not_eng_session_maps_to_404(deps):
    deps["pe"].is_eng_session.return_value = False
    res = deps["client"].post(
        "/api/internal/prompt-engineer/cleanup",
        headers=TOK,
        json={"session_id": "s1"},
    )
    assert res.status_code == 404
    deps["cancel"].assert_not_awaited()


# --- authority guard (real) --------------------------------------------------

def test_routes_reject_without_authority_403():
    """Real require_role_internal with no principal provider => 403."""
    app = _build_app()
    client = TestClient(app)
    res = client.post(
        "/api/internal/prompt-engineer/get",
        headers=TOK,
        json={"session_id": "s1"},
    )
    assert res.status_code == 403
