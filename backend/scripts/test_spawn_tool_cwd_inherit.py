"""Locks the cwd contract for the create-new spawn tools:

- When the agent omits cwd, the tool INHERITS the creating session's cwd
  (BETTER_CLAUDE_CWD for the communicate MCP surface, the bound session cwd
  for the runner-native surface).
- When the agent supplies cwd, it OVERRIDES the inherited value.
- ensure_named_worker no longer REQUIRES cwd (inherits when omitted).

ask / mssg are intentionally excluded — they target existing sessions and
never create a new one, so cwd is not settable there.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paths  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="cwd_inherit_home_")
paths.engage_test_home(_TMP)

import communicate_mcp  # noqa: E402
import runner  # noqa: E402
import orchestration_tool_schemas as schemas  # noqa: E402

INHERITED = "/inherited/session/root"
OVERRIDE = "/explicit/override/root"

_captured: dict = {}


def _fake_post(endpoint: str, payload: dict, timeout: float) -> dict:
    _captured["endpoint"] = endpoint
    _captured["payload"] = payload
    # ensure_named_worker/create_worker read a workers list back.
    return {"workers": [{"agent_session_id": "s1", "name": "w", "created": True,
                         "orchestration_mode": "native", "registry_cwd": payload.get("cwd")}]}


def _fake_env(name: str, default: str = "") -> str:
    if name == "BETTER_CLAUDE_CWD":
        return INHERITED
    return default


def _fake_env_required(name: str) -> str:
    return "sender-sid"


communicate_mcp._post_json = _fake_post
communicate_mcp._env = _fake_env
communicate_mcp._env_required = _fake_env_required


def _payload_cwd(fn, *args, **kwargs) -> str:
    _captured.clear()
    fn(*args, **kwargs)
    return _captured["payload"]["cwd"]


def _check(label: str, fn, base_kwargs: dict):
    inherited = _payload_cwd(fn, **base_kwargs)
    assert inherited == INHERITED, f"{label}: omitted cwd should inherit, got {inherited!r}"
    overridden = _payload_cwd(fn, **{**base_kwargs, "cwd": OVERRIDE})
    assert overridden == OVERRIDE, f"{label}: explicit cwd should override, got {overridden!r}"
    print(f"  ok: {label} (inherit={inherited!r} override={overridden!r})")


def test_communicate_surface():
    print("communicate MCP surface:")
    _check("create_session", communicate_mcp.create_session_response, {"name": "s"})
    _check("create_sub_session", communicate_mcp.create_sub_session_response, {"description": "d"})
    _check("create_worker", communicate_mcp.create_worker_response,
           {"worker_description": "w", "justification": "j", "orchestration_mode": "native"})
    _check("delegate_task", communicate_mcp.delegate_task_response, {"task": "t"})
    _check("ensure_named_worker", communicate_mcp.ensure_named_worker_response,
           {"name": "n", "orchestration_mode": "native"})


def test_ensure_named_worker_no_longer_requires_cwd():
    _captured.clear()
    res = communicate_mcp.ensure_named_worker_response(name="n", orchestration_mode="native")
    assert res.get("success") is True, f"ensure_named_worker should succeed without cwd: {res}"
    assert _captured["payload"]["cwd"] == INHERITED
    print("  ok: ensure_named_worker succeeds without an explicit cwd")


def test_runner_resolve_helper():
    print("runner-native surface:")
    assert runner._resolve_tool_cwd({}, INHERITED) == INHERITED
    assert runner._resolve_tool_cwd({"cwd": ""}, INHERITED) == INHERITED
    assert runner._resolve_tool_cwd({"cwd": "  "}, INHERITED) == INHERITED
    assert runner._resolve_tool_cwd({"cwd": OVERRIDE}, INHERITED) == OVERRIDE
    print("  ok: _resolve_tool_cwd inherit/override")


def test_schemas():
    print("tool schemas:")
    assert "cwd" in schemas.DELEGATE_TASK_INPUT_SCHEMA["properties"]
    assert "cwd" not in schemas.ENSURE_NAMED_WORKER_INPUT_SCHEMA["required"]
    assert "cwd" in schemas.ENSURE_NAMED_WORKER_INPUT_SCHEMA["properties"]
    for name in ("_CREATE_WORKER_INPUT_SCHEMA", "_CREATE_SESSION_INPUT_SCHEMA",
                 "_CREATE_SUB_SESSION_INPUT_SCHEMA"):
        sch = getattr(runner, name)
        assert "cwd" in sch["properties"], f"{name} missing cwd property"
        assert "cwd" not in sch.get("required", []), f"{name} must not require cwd"
    print("  ok: schemas expose optional cwd")


def main() -> int:
    try:
        test_communicate_surface()
        test_ensure_named_worker_no_longer_requires_cwd()
        test_runner_resolve_helper()
        test_schemas()
    finally:
        from shutil import rmtree
        rmtree(paths.bc_home(), ignore_errors=True)
    print("\nALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
