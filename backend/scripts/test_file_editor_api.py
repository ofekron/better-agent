"""Unit coverage for file_editor_api.py.

The router is thin HTTP wiring around the file-editor coordinator and a handful
of collaborators. This file covers its unit-tier surface: input validation,
error-branch mapping (400 / 404 / 403 / 503), the internal-token authority gate,
and the meta_prompt / cleanup delegation branches. Collaborators are satisfied
by narrow fakes patched at the module's import bindings.

Run with:
    cd backend && .venv/bin/python scripts/test_file_editor_api.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-file-editor-api-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import file_editor_api  # noqa: E402
from file_editor_api import (  # noqa: E402
    add_file_editor_comment,
    cleanup_file_editor,
    configure,
    internal_start_file_discussion,
    patch_file_editor_discussion,
    send_file_editor_discussion_message,
    start_file_editor,
    start_file_editor_discussion,
)

PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


def _check(cond: bool, name: str, detail: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {name}{'' if cond else ' -- ' + detail}")
    return cond


# --------------------------------------------------------------------------- #
# Collaborator fakes + patch harness
# --------------------------------------------------------------------------- #
class FakeCoordinator:
    def __init__(self) -> None:
        self.prompts: list[tuple[str, dict]] = []
        self.cancelled: list[str] = []

    async def submit_prompt_async(self, session_id: str, payload: dict) -> None:
        self.prompts.append((session_id, payload))

    async def cancel_session(self, session_id: str) -> None:
        self.cancelled.append(session_id)


class FakeFileEditor:
    """Replaces the ``file_editor`` module surface used by the router."""

    def __init__(self) -> None:
        self.sessions: set[str] = {"sess-ok"}
        self.start_result: dict = {
            "session_id": "sess-ok",
            "file_paths": ["/repo/a.py"],
            "original_contents": {"a.py": "x"},
            "resumed": False,
            "meta_prompt": None,
        }
        self.start_calls: list[dict] = []
        self.cleanup_called: list[str] = []
        # start() dispatch mode: "ok" returns start_result; "nf"/"ve" raise.
        self.start_mode: str = "ok"

    def is_file_editor_session(self, session_id: str) -> bool:
        return session_id in self.sessions

    async def start(self, file_path, **kw) -> dict:
        self.start_calls.append({"file_path": file_path, **kw})
        if self.start_mode == "nf":
            raise FileNotFoundError("no such file")
        if self.start_mode == "ve":
            raise ValueError("bad value")
        result = dict(self.start_result)
        result.setdefault("session_id", "sess-ok")
        return result

    def start_discussion(self, session_id, *, file_path, line, title, opened_by, client_id=None):
        if not isinstance(line, int):
            raise TypeError("line must be int")
        return {"id": "d1", "session_id": session_id, "file_path": file_path,
                "line": line, "title": title, "opened_by": opened_by}

    def patch_discussion(self, session_id, discussion_id, body, *, client_id=None):
        if body.get("force_error"):
            raise ValueError("bad patch")
        return {"id": discussion_id, "patched": True}

    def get_discussion(self, session_id, discussion_id):
        if discussion_id == "missing":
            raise ValueError("no such discussion")
        return {"id": discussion_id}

    @staticmethod
    def format_discussion_prompt(discussion, prompt):
        return f"[{discussion['id']}] {prompt}"

    def cleanup(self, session_id) -> bool:
        self.cleanup_called.append(session_id)
        return True


class _FakeModule:
    """A stand-in module object whose attributes can be set freely."""


def _build_fakes() -> tuple[FakeCoordinator, FakeFileEditor, dict[str, Any]]:
    coord = FakeCoordinator()
    fe = FakeFileEditor()
    # config_store stand-in.
    config = _FakeModule()
    config.default_session_model = lambda: "default-model"
    config.apply_env_vars = lambda *a, **k: None
    # working_mode stand-in.
    working = _FakeModule()
    working.format_file_comment = lambda *a, **k: "FORMATTED"
    # internal_guards stand-in.
    guards = _FakeModule()
    guards.authority_is_valid = lambda: True
    # session_detail_api stand-in.
    detail = _FakeModule()

    async def _publish(*a, **k):
        return None
    detail._publish_worker_fanout_required = _publish
    fakes = {
        "coord": coord, "fe": fe, "config": config,
        "working": working, "guards": guards, "detail": detail,
    }
    return coord, fe, fakes


def _patch(fakes: dict[str, Any]) -> list[tuple[Any, str, Any]]:
    """Swap the router's collaborator bindings for fakes; return originals
    so the caller can restore them."""
    coord, fe = fakes["coord"], fakes["fe"]
    saved: list[tuple[Any, str, Any]] = []
    swaps = [
        (file_editor_api, "file_editor", fe),
        (file_editor_api, "config_store", fakes["config"]),
        (file_editor_api, "working_mode", fakes["working"]),
        (file_editor_api, "internal_guards", fakes["guards"]),
        (file_editor_api, "session_detail_api", fakes["detail"]),
        (file_editor_api, "_session_lite", _session_lite_fake),
        (file_editor_api, "_provider_runner", lambda *a, **k: "native"),
        (file_editor_api, "_provider_reasoning_effort", lambda *a, **k: "high"),
        (file_editor_api, "_api_reasoning_effort", lambda *a, **k: "high"),
    ]
    for mod, attr, val in swaps:
        saved.append((mod, attr, getattr(mod, attr)))
        setattr(mod, attr, val)
    configure(coordinator=coord)
    return saved


def _restore(saved: list[tuple[Any, str, Any]]) -> None:
    for mod, attr, val in saved:
        setattr(mod, attr, val)


async def _session_lite_fake(session_id: str) -> dict | None:
    if session_id == "missing":
        return None
    return {"model": "m", "cwd": "/repo"}


def _run(coro):
    return asyncio.run(coro)


def _status(exc) -> int:
    return exc.status_code if hasattr(exc, "status_code") else type(exc).__name__


# --------------------------------------------------------------------------- #
# _coordinator() not configured -> 503
# --------------------------------------------------------------------------- #
def test_coordinator_not_configured_503() -> bool:
    ok = True
    saved = file_editor_api._coordinator_ref
    try:
        file_editor_api._coordinator_ref = None
        raised = False
        try:
            file_editor_api._coordinator()
        except Exception as exc:  # HTTPException
            raised = _status(exc) == 503
        ok = _check(raised, "_coordinator() unconfigured -> 503") and ok
    finally:
        file_editor_api._coordinator_ref = saved
    return ok


# --------------------------------------------------------------------------- #
# start_file_editor
# --------------------------------------------------------------------------- #
def test_start_file_editor_branches() -> bool:
    ok = True
    coord, fe, fakes = _build_fakes()
    saved = _patch(fakes)
    try:
        # Missing file_path -> 400.
        raised = False
        try:
            _run(start_file_editor({}))
        except Exception as exc:
            raised = _status(exc) == 400
        ok = _check(raised, "missing file_path -> 400") and ok

        # start() raises FileNotFoundError -> 404.
        fe.start_mode = "nf"
        raised = False
        try:
            _run(start_file_editor({"file_path": "/x", "cwd": "/repo"}))
        except Exception as exc:
            raised = _status(exc) == 404
        ok = _check(raised, "FileNotFoundError -> 404") and ok

        # start() raises ValueError -> 400.
        fe.start_mode = "ve"
        raised = False
        try:
            _run(start_file_editor({"file_path": "/x"}))
        except Exception as exc:
            raised = _status(exc) == 400
        ok = _check(raised, "ValueError -> 400") and ok

        # Success WITHOUT meta_prompt -> coordinator not called.
        fe.start_mode = "ok"
        fe.start_result = {
            "session_id": "sess-ok", "file_paths": ["/repo/a.py"],
            "original_contents": {"a.py": "x"}, "resumed": False,
            "meta_prompt": None,
        }
        out = _run(start_file_editor({"file_path": "/repo/a.py", "cwd": "/repo"}))
        ok = _check(coord.prompts == [], "no meta_prompt -> coordinator skipped") and ok
        ok = _check(out["resumed"] is False and out["session_id"] == "sess-ok",
                    "success payload returned") and ok
        # default model path exercised (body has no model) -> default-session-model used.
        ok = _check(fe.start_calls[-1]["model"] == "default-model",
                    "default model used when body omits model") and ok

        # Success WITH meta_prompt -> coordinator.submit_prompt_async called once.
        fe.start_result = {
            "session_id": "sess-ok", "file_paths": [], "original_contents": {},
            "resumed": True, "meta_prompt": "META",
        }
        out = _run(start_file_editor({"file_path": "/repo/a.py", "model": "explicit"}))
        ok = _check(len(coord.prompts) == 1, "meta_prompt -> one coordinator submit") and ok
        ok = _check(coord.prompts[0][1]["prompt"] == "META",
                    "submitted prompt is the meta_prompt") and ok
        ok = _check(out["resumed"] is True, "resumed flag surfaced") and ok
    finally:
        _restore(saved)
    return ok


# --------------------------------------------------------------------------- #
# add_file_editor_comment
# --------------------------------------------------------------------------- #
def test_add_file_editor_comment_branches() -> bool:
    ok = True
    coord, fe, fakes = _build_fakes()
    saved = _patch(fakes)
    try:
        # Not a file-editor session -> 404.
        raised = False
        try:
            _run(add_file_editor_comment("nope", {}))
        except Exception as exc:
            raised = _status(exc) == 404
        ok = _check(raised, "comment on non-editor session -> 404") and ok

        # Missing field (file_path) -> 400.
        raised = False
        try:
            _run(add_file_editor_comment("sess-ok", {"start_line": 1}))
        except Exception as exc:
            raised = _status(exc) == 400
        ok = _check(raised, "missing field -> 400") and ok

        # Non-int line -> ValueError -> 400.
        raised = False
        try:
            _run(add_file_editor_comment("sess-ok", {
                "file_path": "a", "start_line": "x", "end_line": 2,
                "start_col": 1, "end_col": 2, "comment": "c",
            }))
        except Exception as exc:
            raised = _status(exc) == 400
        ok = _check(raised, "non-int line -> 400") and ok

        # Success -> coordinator receives the formatted comment.
        out = _run(add_file_editor_comment("sess-ok", {
            "file_path": "a.py", "start_line": 1, "end_line": 2,
            "start_col": 1, "end_col": 2, "comment": "fix this", "client_id": "c1",
        }))
        ok = _check(out == {"submitted": True}, "success returns submitted=True") and ok
        ok = _check(len(coord.prompts) == 1 and coord.prompts[0][1]["prompt"] == "FORMATTED",
                    "formatted comment submitted to coordinator") and ok
        ok = _check(coord.prompts[0][1]["client_id"] == "c1",
                    "client_id forwarded") and ok
    finally:
        _restore(saved)
    return ok


# --------------------------------------------------------------------------- #
# start_file_editor_discussion
# --------------------------------------------------------------------------- #
def test_start_file_editor_discussion_branches() -> bool:
    ok = True
    coord, fe, fakes = _build_fakes()
    saved = _patch(fakes)
    try:
        # Not editor session -> 404.
        raised = False
        try:
            _run(start_file_editor_discussion("nope", {"file_path": "a", "line": 1}))
        except Exception as exc:
            raised = _status(exc) == 404
        ok = _check(raised, "discussion on non-editor session -> 404") and ok

        # Bad input (non-int line -> TypeError) -> 400.
        raised = False
        try:
            _run(start_file_editor_discussion("sess-ok", {"file_path": "a", "line": "x"}))
        except Exception as exc:
            raised = _status(exc) == 400
        ok = _check(raised, "bad discussion input -> 400") and ok

        # Success.
        out = _run(start_file_editor_discussion("sess-ok", {
            "file_path": " a.py ", "line": 5, "title": "T", "client_id": "c",
        }))
        ok = _check(out["discussion"]["line"] == 5, "discussion returned") and ok
        ok = _check(out["discussion"]["file_path"] == "a.py",
                    "file_path stripped before forwarding") and ok
        ok = _check(out["discussion"]["opened_by"] == "user",
                    "user-opened discussion tagged opened_by=user") and ok
    finally:
        _restore(saved)
    return ok


# --------------------------------------------------------------------------- #
# internal_start_file_discussion (agent-opened, in-band errors)
# --------------------------------------------------------------------------- #
def test_internal_start_file_discussion_branches() -> bool:
    ok = True
    coord, fe, fakes = _build_fakes()
    saved = _patch(fakes)
    try:
        # Authority invalid -> 403.
        fakes["guards"].authority_is_valid = lambda: False
        raised = False
        try:
            _run(internal_start_file_discussion({"app_session_id": "sess-ok"}, "tok"))
        except Exception as exc:
            raised = _status(exc) == 403
        ok = _check(raised, "invalid internal authority -> 403") and ok
        fakes["guards"].authority_is_valid = lambda: True

        # Not an editor session -> in-band {success: False} (no raise).
        out = _run(internal_start_file_discussion({"app_session_id": "nope"}, "tok"))
        ok = _check(out.get("success") is False, "non-editor session -> in-band failure") and ok

        # Bad input -> in-band {success: False, error}.
        out = _run(internal_start_file_discussion(
            {"app_session_id": "sess-ok", "line": "x"}, "tok"))
        ok = _check(out.get("success") is False and "error" in out,
                    "bad input -> in-band error payload") and ok

        # Success.
        out = _run(internal_start_file_discussion(
            {"app_session_id": "sess-ok", "file_path": "a", "line": 3, "title": "T"}, "tok"))
        ok = _check(out.get("success") is True, "success -> in-band success") and ok
        ok = _check(out["discussion"]["opened_by"] == "agent",
                    "agent-opened discussion tagged opened_by=agent") and ok
    finally:
        _restore(saved)
    return ok


# --------------------------------------------------------------------------- #
# patch_file_editor_discussion
# --------------------------------------------------------------------------- #
def test_patch_file_editor_discussion_branches() -> bool:
    ok = True
    coord, fe, fakes = _build_fakes()
    saved = _patch(fakes)
    try:
        # Not editor session -> 404.
        raised = False
        try:
            _run(patch_file_editor_discussion("nope", "d1", {"client_id": "c"}))
        except Exception as exc:
            raised = _status(exc) == 404
        ok = _check(raised, "patch on non-editor session -> 404") and ok

        # ValueError -> 400.
        raised = False
        try:
            _run(patch_file_editor_discussion("sess-ok", "d1", {"force_error": True}))
        except Exception as exc:
            raised = _status(exc) == 400
        ok = _check(raised, "patch ValueError -> 400") and ok

        # Success.
        out = _run(patch_file_editor_discussion("sess-ok", "d1", {"client_id": "c"}))
        ok = _check(out["discussion"]["patched"] is True, "patch success") and ok
    finally:
        _restore(saved)
    return ok


# --------------------------------------------------------------------------- #
# send_file_editor_discussion_message
# --------------------------------------------------------------------------- #
def test_send_file_editor_discussion_message_branches() -> bool:
    ok = True
    coord, fe, fakes = _build_fakes()
    saved = _patch(fakes)
    try:
        # Not editor session -> 404.
        raised = False
        try:
            _run(send_file_editor_discussion_message("nope", "d1", {"prompt": "hi"}))
        except Exception as exc:
            raised = _status(exc) == 404
        ok = _check(raised, "message on non-editor session -> 404") and ok

        # Empty prompt -> 400.
        raised = False
        try:
            _run(send_file_editor_discussion_message("sess-ok", "d1", {"prompt": "  "}))
        except Exception as exc:
            raised = _status(exc) == 400
        ok = _check(raised, "empty prompt -> 400") and ok

        # get_discussion ValueError -> 400.
        raised = False
        try:
            _run(send_file_editor_discussion_message("sess-ok", "missing", {"prompt": "hi"}))
        except Exception as exc:
            raised = _status(exc) == 400
        ok = _check(raised, "unknown discussion -> 400") and ok

        # Success -> coordinator gets cli_prompt from format_discussion_prompt.
        out = _run(send_file_editor_discussion_message("sess-ok", "d1", {
            "prompt": "hello", "client_id": "c7"}))
        ok = _check(out["submitted"] is True and out["client_id"] == "c7",
                    "success returns submitted + client_id") and ok
        ok = _check(len(coord.prompts) == 1, "one coordinator submit") and ok
        ok = _check(coord.prompts[0][1]["cli_prompt"] == "[d1] hello",
                    "cli_prompt built via format_discussion_prompt") and ok
        ok = _check(coord.prompts[0][1]["file_discussion_id"] == "d1",
                    "file_discussion_id stamped on payload") and ok
    finally:
        _restore(saved)
    return ok


# --------------------------------------------------------------------------- #
# cleanup_file_editor
# --------------------------------------------------------------------------- #
def test_cleanup_file_editor_branches() -> bool:
    ok = True
    coord, fe, fakes = _build_fakes()
    saved = _patch(fakes)
    try:
        # Not editor session -> 404.
        raised = False
        try:
            _run(cleanup_file_editor("nope"))
        except Exception as exc:
            raised = _status(exc) == 404
        ok = _check(raised, "cleanup on non-editor session -> 404") and ok

        # Success -> cancel + cleanup + fanout, returns deleted.
        out = _run(cleanup_file_editor("sess-ok"))
        ok = _check(out == {"deleted": True}, "cleanup returns deleted=True") and ok
        ok = _check(coord.cancelled == ["sess-ok"], "coordinator.cancel_session called") and ok
        ok = _check(fe.cleanup_called == ["sess-ok"], "file_editor.cleanup called") and ok
    finally:
        _restore(saved)
    return ok


TESTS = [
    test_coordinator_not_configured_503,
    test_start_file_editor_branches,
    test_add_file_editor_comment_branches,
    test_start_file_editor_discussion_branches,
    test_internal_start_file_discussion_branches,
    test_patch_file_editor_discussion_branches,
    test_send_file_editor_discussion_message_branches,
    test_cleanup_file_editor_branches,
]


def main() -> int:
    results = []
    for test in TESTS:
        print(f"\n--- {test.__name__} ---")
        try:
            results.append(test())
        except Exception as exc:  # noqa: BLE001
            print(f"{FAIL} {test.__name__} raised: {exc!r}")
            results.append(False)
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} test groups passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
