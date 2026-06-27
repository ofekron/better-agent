from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-headless-generate-")

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi import HTTPException  # noqa: E402
import main  # noqa: E402
import provider_claude  # noqa: E402


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _call(body: dict, token: str | None = None):
    return asyncio.run(main.internal_headless_generate(
        body,
        x_internal_token=token if token is not None else main.coordinator.internal_token,
    ))


def _expect_http(body: dict, token: str | None, status: int) -> HTTPException:
    try:
        _call(body, token)
    except HTTPException as exc:
        assert exc.status_code == status, f"expected {status}, got {exc.status_code}: {exc.detail}"
        return exc
    raise AssertionError(f"expected HTTP {status}, no exception raised")


class _StubProvider:
    def __init__(self, *, fork=True, no_tools=True, result="suggestion"):
        self.supports_fork = fork
        self.supports_headless_no_tools = no_tools
        self._result = result
        self.calls: list[dict] = []

    async def run_headless(self, **kwargs):
        self.calls.append(kwargs)
        return {"result": self._result}


def _make_ran_session(name: str) -> str:
    sess = main.session_manager.create(
        name=name, cwd="/tmp", orchestration_mode="native", model="model", source="test",
    )
    sid = sess["id"]
    main.session_manager.set_agent_sid(sid, "native", "fake-agent-sid")
    return sid


# --------------------------------------------------------------------------
# Auth + validation (fail-closed)
# --------------------------------------------------------------------------
def test_rejects_non_internal_caller():
    _expect_http({"session_id": "x", "prompt": "y"}, "not-the-token", 403)


def test_missing_fields_400():
    _expect_http({"session_id": "", "prompt": ""}, None, 400)
    _expect_http({"session_id": "s", "prompt": ""}, None, 400)


def test_prompt_too_long_413():
    big = "a" * (main._HEADLESS_GENERATE_MAX_PROMPT + 1)
    _expect_http({"session_id": "s", "prompt": big}, None, 413)


def test_unknown_session_404():
    _expect_http({"session_id": "does-not-exist", "prompt": "hi"}, None, 404)


def test_session_without_provider_sid_409():
    sess = main.session_manager.create(
        name="no-sid", cwd="/tmp", orchestration_mode="native", model="model", source="test",
    )
    _expect_http({"session_id": sess["id"], "prompt": "hi"}, None, 409)


def test_provider_that_cannot_fork_or_disable_tools_422():
    sid = _make_ran_session("gate")
    original = main.coordinator.provider_for_session
    for stub in (_StubProvider(fork=False, no_tools=True), _StubProvider(fork=True, no_tools=False)):
        main.coordinator.provider_for_session = lambda _sid, _s=stub: _s
        try:
            _expect_http({"session_id": sid, "prompt": "hi"}, None, 422)
            assert not stub.calls, "must not invoke run_headless when the provider is gated out"
        finally:
            main.coordinator.provider_for_session = original


# --------------------------------------------------------------------------
# Happy path — security contract + zero render-tree footprint
# --------------------------------------------------------------------------
def test_happy_path_forks_disables_tools_and_leaves_tree_untouched():
    sid = _make_ran_session("happy")
    before = main.session_manager.get_lite(sid)
    before_msgs = [m.get("id") for m in (before or {}).get("messages", [])]

    stub = _StubProvider(result="hello world")
    original = main.coordinator.provider_for_session
    main.coordinator.provider_for_session = lambda _sid: stub
    try:
        result = _call({"session_id": sid, "prompt": "complete this"}, None)
    finally:
        main.coordinator.provider_for_session = original

    assert result == {"text": "hello world"}
    assert len(stub.calls) == 1
    kw = stub.calls[0]
    # Security contract: tools disabled + forked (never mutate the real session).
    assert kw["no_tools"] is True
    assert kw["fork"] is True
    assert kw["resume_sid"] == "fake-agent-sid"

    after = main.session_manager.get_lite(sid)
    after_msgs = [m.get("id") for m in (after or {}).get("messages", [])]
    assert after_msgs == before_msgs, "fill must not append anything to the session render tree"


def test_generation_failure_502():
    sid = _make_ran_session("fail")

    class _ErrProvider(_StubProvider):
        async def run_headless(self, **kwargs):
            return {"is_error": True, "result": "boom"}

    stub = _ErrProvider()
    original = main.coordinator.provider_for_session
    main.coordinator.provider_for_session = lambda _sid: stub
    try:
        _expect_http({"session_id": sid, "prompt": "hi"}, None, 502)
    finally:
        main.coordinator.provider_for_session = original


# --------------------------------------------------------------------------
# Provider-level security lock — `no_tools=True` MUST pass `--tools ""`.
# --------------------------------------------------------------------------
def test_claude_run_headless_disables_tools_flag():
    captured: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return (b'{"result": "ok"}', b"")

    async def _fake_exec(*cmd, **_kwargs):
        captured["cmd"] = list(cmd)
        return _FakeProc()

    prov = provider_claude.ClaudeProvider({
        "id": "claude-test", "kind": "claude", "name": "Claude Test",
        "default_model": "sonnet",
    })
    prov.build_env = lambda: {}  # type: ignore[assignment]

    orig_exec = provider_claude.asyncio.create_subprocess_exec
    provider_claude.asyncio.create_subprocess_exec = _fake_exec
    try:
        asyncio.run(prov.run_headless(prompt="x", resume_sid="s", fork=True, no_tools=True))
        cmd = captured["cmd"]
        assert "--tools" in cmd, cmd
        assert cmd[cmd.index("--tools") + 1] == "", "no_tools must pass an EMPTY --tools list"
        assert "--fork-session" in cmd, cmd

        captured.clear()
        asyncio.run(prov.run_headless(prompt="x", resume_sid="s", fork=True, no_tools=False))
        assert "--tools" not in captured["cmd"], "default run must NOT restrict tools"
    finally:
        provider_claude.asyncio.create_subprocess_exec = orig_exec


# --------------------------------------------------------------------------
# Extension parsing — plural suggestions + non-JSON fallback.
# --------------------------------------------------------------------------
def _load_routes_module():
    path = ROOT / "better-agent-private" / "extensions" / "composer-fill" / "backend" / "routes.py"
    spec = importlib.util.spec_from_file_location("composer_fill_routes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_coerce_suggestions_parses_json_array_and_falls_back():
    mod = _load_routes_module()
    assert mod._coerce_suggestions('["a", "b", "c"]', 3) == ["a", "b", "c"]
    # Honors the count cap.
    assert mod._coerce_suggestions('["a","b","c","d"]', 2) == ["a", "b"]
    # Fenced JSON.
    assert mod._coerce_suggestions('```json\n["x"]\n```', 3) == ["x"]
    # Non-JSON prose → single-suggestion fallback (not dropped).
    assert mod._coerce_suggestions("just write the tests", 3) == ["just write the tests"]
    # Empty → no suggestions.
    assert mod._coerce_suggestions("", 3) == []


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
