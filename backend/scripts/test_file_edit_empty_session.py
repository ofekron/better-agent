from __future__ import annotations

import asyncio
import os
import shutil
import time
import threading
import sys
from pathlib import Path

import pytest

import _test_home
from _test_home import scoped_patches
_TMP_HOME = _test_home.isolate("bc-test-file-edit-empty-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import file_editor  # noqa: E402
import config_store  # noqa: E402
import main as main_api  # noqa: E402,F401  (import wires the app)
import session_detail_api  # noqa: E402
import models  # noqa: E402
import render_stub  # noqa: E402
import synthetic_messages  # noqa: E402
import working_mode  # noqa: E402
import extension_store  # noqa: E402
import harness_profile_resolver  # noqa: E402
import session_readiness  # noqa: E402
from event_journal import event_journal_reader  # noqa: E402
from session_manager import manager as session_manager  # noqa: E402


_FAKE_BASES: dict[tuple[str, str, str, str], str] = {}
_FAKE_BASES_LOCK = threading.Lock()


async def _fake_ensure_file_edit_base(cfg):
    key = (cfg.cwd, cfg.provider_id, cfg.model, cfg.node_id)
    with _FAKE_BASES_LOCK:
        sid = _FAKE_BASES.get(key)
        if sid and session_manager.get(sid):
            return sid
        base = session_manager.create(
            name="file-editing-base",
            model=cfg.model,
            cwd=cfg.cwd,
            orchestration_mode="native",
            source="internal",
            provider_id=cfg.provider_id,
            reasoning_effort=cfg.reasoning_effort or None,
            node_id=cfg.node_id,
            bare_config=False,
            worker_creation_policy="deny",
        )
        fake_agent_sid = f"fake-empty-base-sid-{len(_FAKE_BASES)}"
        session_manager._run(
            base["id"],
            lambda s: s.__setitem__("agent_session_id", fake_agent_sid),
            {"kind": "test_agent_sid_set"},
        )
        working_mode.mark_working_mode(
            base["id"],
            mode=file_editor.BASE_MODE,
            meta={
                "cwd": cfg.cwd,
                "provider_id": cfg.provider_id,
                "model": cfg.model,
                "machine_completion": False,
                "version": file_editor.FILE_EDIT_BASE_SPEC.version,
                "node_id": cfg.node_id,
                "provisioned_at": time.time(),
            },
        )
        _FAKE_BASES[key] = base["id"]
        return base["id"]


_FAKE_PROVIDER = {
    "id": "test-provider",
    "name": "Test Provider",
    "kind": "claude",
    "default_model": "test-model",
    "custom_models": ["test-model"],
    "runner": "native",
    "supports_reasoning_effort": False,
}


def _fake_provider_record(provider_id: str | None = None) -> dict:
    return dict(_FAKE_PROVIDER)


def _fake_available_models(provider_id=None):
    return ["test-model"] if provider_id == "test-provider" else []


def _fake_get_provider(provider_id):
    return dict(_FAKE_PROVIDER) if provider_id == "test-provider" else None


def _fake_resolve_profile(profile_id, *args, **kwargs):
    """create_session's file-edit gate requires the File Edit extension to be
    present and not disabled in the resolved harness profile. A fresh test home
    has no extensions installed, so simulate an enabled File Edit harness."""
    return {
        "extension_instances": {
            extension_store.BUILTIN_FILE_EDIT_EXTENSION_ID: {},
        },
        "disabled_builtin_extensions": {"resolved": []},
    }


# Patches scoped to this module's tests only. Applying these at module top
# level leaks them across the whole pytest session (collection imports every
# module before any test runs) and poisons later script-style modules that
# exercise the real config path at import time.
_MODULE_PATCHES = [
    (file_editor, "_ensure_file_edit_base", _fake_ensure_file_edit_base),
    (file_editor, "_provider_record", _fake_provider_record),
    (file_editor, "_require_fork_support", lambda provider_id: None),
    (config_store, "get_provider", _fake_get_provider),
    (models, "available_models", _fake_available_models),
    (models, "available_models_including_retired", _fake_available_models),
    (session_detail_api, "_provider_for_required_model", lambda provider_id: dict(_FAKE_PROVIDER)),
    (session_detail_api, "_provider_reasoning_effort", lambda *args, **kwargs: ""),
    (session_detail_api, "_provider_permission", lambda provider_id, requested, *args, **kwargs: requested or {}),
    (harness_profile_resolver, "resolve_profile", _fake_resolve_profile),
]


@pytest.fixture(autouse=True, scope="module")
def _scoped_file_edit_patches():
    with scoped_patches(_MODULE_PATCHES):
        yield


def _project(label: str) -> Path:
    d = Path(_TMP_HOME) / "proj" / label
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(d: Path, name: str, content: str) -> Path:
    p = d / name
    p.write_text(content)
    return p


async def _await_provisioning(sid: str, timeout: float = 5.0) -> None:
    """The fork binding (`forked_from_agent_sid` + `base_session_id`) is stamped
    by a background task scheduled by start_empty/start, not by create itself.
    Wait for it to land in the SAME event loop so assertions observe the bound
    state rather than the create-time pre-binding snapshot — otherwise a check
    like `base_session_id == base_session_id` passes vacuously (None == None)."""
    for _ in range(max(1, int(timeout / 0.01))):
        if not session_readiness.is_awaiting_fork_provisioning(sid):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"fork provisioning never completed for {sid}")


def test_empty_session_has_no_required_file() -> None:
    d = _project("empty")
    result = asyncio.run(file_editor.start_empty(cwd=str(d), persistent=True))
    session = session_manager.get(result["session_id"]) or {}
    meta = session.get("working_mode_meta") or {}
    assert meta.get("project_cwd") == str(d.resolve()), f"wrong project_cwd: {meta.get('project_cwd')!r}"
    assert meta.get("file_paths") == [], f"expected empty file_paths, got {meta.get('file_paths')!r}"
    assert meta.get("original_contents") == {}, f"expected empty original_contents, got {meta.get('original_contents')!r}"
    assert meta.get("persistent") is True, f"expected persistent=True, got {meta.get('persistent')!r}"
    assert result.get("meta_prompt") is None, f"empty session should not send a model prompt, got {result.get('meta_prompt')!r}"
    ask = result.get("user_ask") or ""
    assert "Which file or files do you want to edit?" in ask, f"missing user-facing ask: {ask!r}"


def test_empty_session_then_file_creates_new_session_same_base() -> None:
    d = _project("join")

    async def _run() -> None:
        empty = await file_editor.start_empty(cwd=str(d), persistent=True)
        await _await_provisioning(empty["session_id"])
        target = _write(d, "target.txt", "hello\n")
        joined = await file_editor.start(str(target), cwd=str(d))
        await _await_provisioning(joined["session_id"])
        assert joined["session_id"] != empty["session_id"], "selected file should create a fresh session, not join the empty one"
        resolved = str(target.resolve())
        assert joined["file_paths"] == [resolved], f"expected file_paths=[{resolved!r}], got {joined['file_paths']!r}"
        assert joined["original_contents"].get(resolved) == "hello\n", f"missing baseline: {joined['original_contents']!r}"
        assert resolved in (joined.get("meta_prompt") or ""), f"bootstrap prompt should mention {resolved}"
        empty_meta = (session_manager.get(empty["session_id"]) or {}).get("working_mode_meta") or {}
        joined_meta = (session_manager.get(joined["session_id"]) or {}).get("working_mode_meta") or {}
        assert empty_meta.get("base_session_id") == joined_meta.get("base_session_id"), "same cwd/model/provider should reuse the warm base"

    asyncio.run(_run())


def test_empty_ask_appends_visible_operator_message() -> None:
    d = _project("visible_ask")
    result = asyncio.run(file_editor.start_empty(cwd=str(d), persistent=True))
    ask = result.get("user_ask") or ""
    asyncio.run(
        synthetic_messages.append_operator_message(
            result["session_id"],
            content=ask,
            source="file_editor",
        )
    )
    session = session_manager.get(result["session_id"]) or {}
    messages = session.get("messages") or []
    assert not session.get("queued_prompts"), f"user-facing ask should not create queued prompts: {session.get('queued_prompts')!r}"
    assert len(messages) == 1, f"expected exactly one visible message, got {len(messages)}"
    msg = messages[0]
    assert msg.get("role") == "operator", f"expected operator message, got {msg.get('role')!r}"
    assert msg.get("content") == ask, f"wrong message content: {msg.get('content')!r}"
    assert render_stub.message_output_text(msg) == ask, "ask must be derivable from render events"
    assert msg.get("source") == "file_editor", f"wrong source: {msg.get('source')!r}"


def test_create_session_returns_visible_empty_ask() -> None:
    d = _project("create_session")
    session = asyncio.run(
        session_detail_api.create_session(
            {
                "name": "",
                "model": "test-model",
                "cwd": str(d),
                "orchestration_mode": "native",
                "provider_id": "test-provider",
                "file_edit_enabled": True,
                "node_id": "primary",
            }
        )
    )
    messages = session.get("messages") or []
    assert len(messages) == 1, f"expected returned session to include one ask message, got {len(messages)}"
    msg = messages[0]
    assert msg.get("role") == "operator", f"expected operator message, got {msg.get('role')!r}"
    assert msg.get("source") == "file_editor", f"wrong source: {msg.get('source')!r}"
    assert "Which file or files do you want to edit?" in (msg.get("content") or ""), f"missing ask content: {msg.get('content')!r}"
    journal_events = event_journal_reader.read_frontend_events(
        session["id"],
        message_id=msg["id"],
    )
    assert "Which file or files do you want to edit?" in render_stub.message_output_text({"events": journal_events}), "ask must be written to the event journal"
    session_manager._roots.clear()
    session_manager._node_root_id.clear()
    cold = session_manager.get(session["id"]) or {}
    cold_msg = ((cold.get("messages") or []) or [{}])[0]
    assert "Which file or files do you want to edit?" in (cold_msg.get("content") or ""), f"cold reload lost ask content: {cold_msg.get('content')!r}"


def test_empty_session_reopen_creates_fresh_prompt() -> None:
    d = _project("resume")

    async def _run() -> None:
        first = await file_editor.start_empty(cwd=str(d), persistent=True)
        await _await_provisioning(first["session_id"])
        second = await file_editor.start_empty(cwd=str(d), persistent=True)
        await _await_provisioning(second["session_id"])
        assert second["session_id"] != first["session_id"], "expected empty start to create a fresh session"
        assert second.get("meta_prompt") is None, f"empty session should not inject a model prompt, got {second.get('meta_prompt')!r}"
        assert second.get("user_ask") is not None, "fresh empty session should show the user ask"
        meta1 = (session_manager.get(first["session_id"]) or {}).get("working_mode_meta") or {}
        meta2 = (session_manager.get(second["session_id"]) or {}).get("working_mode_meta") or {}
        assert meta1.get("base_session_id") == meta2.get("base_session_id"), "empty sessions for same cwd should share the warm base"

    asyncio.run(_run())


def test_empty_session_requires_cwd() -> None:
    with pytest.raises(ValueError):
        asyncio.run(file_editor.start_empty(cwd=""))


def test_every_file_edit_session_is_a_provisioned_fork() -> None:
    """Every new file-editing session — with a file OR none — must be created
    as a provisioned fork (forked_from_agent_sid set), regardless of file."""

    d = _project("always_fork")

    async def _run() -> None:
        empty = await file_editor.start_empty(cwd=str(d), persistent=True)
        await _await_provisioning(empty["session_id"])
        empty_sess = session_manager.get(empty["session_id"]) or {}
        empty_fork = empty_sess.get("forked_from_agent_sid")
        assert isinstance(empty_fork, str) and empty_fork, f"no-file session must be a provisioned fork, got forked_from_agent_sid={empty_fork!r}"

        target = _write(d, "always.txt", "hi\n")
        filed = await file_editor.start(str(target), cwd=str(d))
        await _await_provisioning(filed["session_id"])
        filed_sess = session_manager.get(filed["session_id"]) or {}
        filed_fork = filed_sess.get("forked_from_agent_sid")
        assert isinstance(filed_fork, str) and filed_fork, f"file session must be a provisioned fork, got forked_from_agent_sid={filed_fork!r}"

    asyncio.run(_run())


def main() -> int:
    tests = [
        test_empty_session_has_no_required_file,
        test_empty_session_then_file_creates_new_session_same_base,
        test_empty_ask_appends_visible_operator_message,
        test_create_session_returns_visible_empty_ask,
        test_empty_session_reopen_creates_fresh_prompt,
        test_empty_session_requires_cwd,
        test_every_file_edit_session_is_a_provisioned_fork,
    ]
    try:
        for test in tests:
            test()
            print(f"PASS {test.__name__}")
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
