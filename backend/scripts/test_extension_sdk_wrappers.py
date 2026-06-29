"""Path + payload checks for the generic-core + delegation SDK wrappers and the
inter-extension ``call_extension`` primitive. Feature-specific capabilities
(requirements, scheduler, credentials, browser-harness, project-updates,
continuation-recall, provider-config-sync) are intentionally NOT in the shared
SDK — they live in per-extension SDKs reached via ``call_extension``.

Run standalone:  python scripts/test_extension_sdk_wrappers.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
_REPO = os.path.dirname(_BACKEND)
for _p in (_BACKEND, os.path.join(_REPO, "sdk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from better_agent_sdk import (  # noqa: E402
    Client,
    BetterAgentError,
    FrontendModule,
    Instruction,
    McpPredicate,
    McpServer,
    PermissionSet,
    TeamDefinition,
)

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


def main_test() -> int:
    captured: dict = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"success": true}'

    def _fake_urlopen(req, timeout=None):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        captured["data"] = json.loads(req.data.decode("utf-8")) if req.data else None
        captured["timeout"] = timeout
        return _FakeResp()

    original_urlopen = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen
    try:
        c = Client(
            internal_token="tok",
            extension_id="ext-1",
            app_session_id="caller-sid",
            cwd="/repo",
            model="m",
            backend_url="http://core",
        )

        env_keys = [
            "BETTER_AGENT_BACKEND_URL",
            "BETTER_AGENT_INTERNAL_TOKEN",
            "BETTER_AGENT_APP_SESSION_ID",
            "BETTER_AGENT_CWD",
            "BETTER_AGENT_EXTENSION_ID",
            "BETTER_AGENT_MODEL",
            "BETTER_AGENT_PROVIDER_ID",
            "BETTER_CLAUDE_BACKEND_URL",
            "BETTER_CLAUDE_INTERNAL_TOKEN",
            "BETTER_CLAUDE_APP_SESSION_ID",
            "BETTER_CLAUDE_CWD",
            "BETTER_CLAUDE_EXTENSION_ID",
            "BETTER_CLAUDE_MODEL",
            "BETTER_CLAUDE_PROVIDER_ID",
        ]
        old_env = {key: os.environ.get(key) for key in env_keys}
        try:
            for key in env_keys:
                os.environ.pop(key, None)
            os.environ["BETTER_AGENT_BACKEND_URL"] = "http://agent-core"
            os.environ["BETTER_AGENT_INTERNAL_TOKEN"] = "agent-token"
            os.environ["BETTER_AGENT_APP_SESSION_ID"] = "agent-session"
            os.environ["BETTER_AGENT_CWD"] = "/agent-repo"
            os.environ["BETTER_AGENT_EXTENSION_ID"] = "agent-extension"
            os.environ["BETTER_AGENT_MODEL"] = "agent-model"
            os.environ["BETTER_AGENT_PROVIDER_ID"] = "agent-provider"
            os.environ["BETTER_CLAUDE_BACKEND_URL"] = "http://legacy-core"
            os.environ["BETTER_CLAUDE_INTERNAL_TOKEN"] = "legacy-token"
            os.environ["BETTER_CLAUDE_APP_SESSION_ID"] = "legacy-session"
            os.environ["BETTER_CLAUDE_CWD"] = "/legacy-repo"
            os.environ["BETTER_CLAUDE_EXTENSION_ID"] = "legacy-extension"
            os.environ["BETTER_CLAUDE_MODEL"] = "legacy-model"
            os.environ["BETTER_CLAUDE_PROVIDER_ID"] = "legacy-provider"
            env_client = Client()
            check(
                env_client.backend_url == "http://agent-core"
                and env_client.internal_token == "agent-token"
                and env_client.app_session_id == "agent-session"
                and env_client.cwd == "/agent-repo"
                and env_client.extension_id == "agent-extension"
                and env_client.model == "agent-model"
                and env_client.provider_id == "agent-provider",
                "Client prefers BETTER_AGENT_* env over legacy BETTER_CLAUDE_*",
            )
            for key in env_keys:
                os.environ.pop(key, None)
            os.environ["BETTER_CLAUDE_INTERNAL_TOKEN"] = "legacy-token"
            check(Client().internal_token == "legacy-token", "Client still accepts legacy BETTER_CLAUDE_* env")
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        cases = [
            ("create_sub_session", lambda: c.create_sub_session("parent", description="d"),
             "POST", "/api/internal/create-sub-session",
             {"sender_session_id": "parent", "description": "d", "provider_id": "", "model": "m",
              "reasoning_effort": "", "cwd": "/repo", "node_id": ""}),
            ("delegate_task", lambda: c.delegate_task("do it", target_session_id="t1"),
             "POST", "/api/internal/delegate-task",
             {"sender_session_id": "caller-sid", "task": "do it", "target_session_id": "t1",
              "provider_id": "", "model": "m", "reasoning_effort": "", "sub_session": True, "cwd": "/repo",
              "run_mode": "direct"}),
            ("ask", lambda: c.ask("t1", "hi"),
             "POST", "/api/internal/ask",
             {"sender_session_id": "caller-sid", "target_session_id": "t1", "target_worker_id": "",
              "target_worker_pool": "", "message": "hi", "ask_id": ""}),
            ("mssg", lambda: c.mssg("t1", "hi"),
             "POST", "/api/internal/mssg",
             {"sender_session_id": "caller-sid", "target_session_id": "t1", "target_worker_id": "",
              "target_worker_pool": "", "message": "hi"}),
            ("async", lambda: c.async_(target_worker_pool="pool-a", message="hi"),
             "POST", "/api/internal/async-communicate",
             {"sender_session_id": "caller-sid", "target_session_id": "", "target_worker_id": "",
              "target_worker_pool": "pool-a", "message": "hi"}),
            ("ask_propose", lambda: c.ask_propose(["s1", "s2"], reasoning="r"),
             "POST", "/api/internal/ask-propose",
             {"caller_sid": "caller-sid", "session_ids": ["s1", "s2"], "reasoning": "r", "proposed_project_path": ""}),
            ("open_file_panel", lambda: c.open_file_panel("/a/b.py", mode="inline", start_line=5),
             "POST", "/api/internal/open-file-panel",
             None),
            ("request_user_input", lambda: c.request_user_input([{"id": "q", "header": "H", "question": "Q"}], timeout_seconds=30),
             "POST", "/api/internal/user-input/request",
             {"app_session_id": "caller-sid", "questions": [{"id": "q", "header": "H", "question": "Q"}], "timeout_seconds": 30}),
            ("call_extension", lambda: c.call_extension("other-ext", "/foo", {"a": 1}),
             "POST", "/api/internal/extension-call",
             {"target_extension_id": "other-ext", "path": "/foo", "method": "POST", "body": {"a": 1}}),
            ("lock_ops", lambda: c.lock_ops("git_ops", holder_token="tok", release=True),
             "POST", "/api/internal/coordination/lock-ops",
             {"key": "git_ops", "release": True, "holder_token": "tok"}),
            ("lock_ops_multi", lambda: c.lock_ops("", keys=["a", "b"], timeout_seconds=12),
             "POST", "/api/internal/coordination/lock-ops",
             {"key": "", "release": False, "holder_token": "", "keys": ["a", "b"], "timeout_seconds": 12}),
            ("get_session_fields", lambda: c.get_session_fields(["current_todos"]),
             "POST", "/api/internal/session-fields",
             {"session_id": "caller-sid", "fields": ["current_todos"]}),
            ("update_session_field", lambda: c.update_session_field("current_todos", []),
             "POST", "/api/internal/session-field",
             {"session_id": "caller-sid", "field": "current_todos", "value": []}),
        ]
        for name, call, method, path, expected in cases:
            captured.clear()
            call()
            ok = captured["method"] == method and captured["url"].endswith(path)
            if expected is not None:
                ok = ok and captured["data"] == expected
            check(ok, f"{name} -> {method} {path}" + ("" if expected is None else " + payload"))

        # GET endpoint
        captured.clear()
        c.get_internal_llm()
        check(captured["method"] == "GET" and captured["url"].endswith("/api/settings/internal-llm"),
              "get_internal_llm -> GET settings/internal-llm")

        # call_internal: cleaned loopback substrate — POST only, auto-injects
        # app_session_id (caller value wins), prefix-gates to /api/internal/.
        captured.clear()
        c.call_internal("/api/internal/schedules", {"action": "list"})
        check(captured["method"] == "POST"
              and captured["url"].endswith("/api/internal/schedules")
              and captured["data"] == {"action": "list", "app_session_id": "caller-sid"},
              "call_internal POSTs + auto-injects app_session_id")
        captured.clear()
        c.call_internal("/api/internal/x", {"app_session_id": "explicit", "k": 1})
        check(captured["data"] == {"app_session_id": "explicit", "k": 1},
              "call_internal preserves caller app_session_id")
        gate_ok = False
        try:
            c.call_internal("/api/evil", {})
        except BetterAgentError:
            gate_ok = True
        check(gate_ok, "call_internal rejects non-/api/internal/ path")
        captured.clear()
        c.call_internal("/api/internal/x", {}, timeout=120.0)
        check(captured["timeout"] == 120.0, "call_internal forwards timeout")

        # ask_fork payload shape (many fields — spot check the key ones)
        captured.clear()
        c.ask_fork("instr", "w1", "m", "/repo")
        d = captured["data"]
        check(d["app_session_id"] == "caller-sid" and d["worker_session_id"] == "w1"
              and d["instructions"] == "instr" and d["run_mode"] == "fork",
              "ask_fork payload shape")
        # open_file_panel payload shape
        captured.clear()
        c.open_file_panel("/a/b.py", mode="inline", start_line=5)
        check(captured["data"]["path"] == "/a/b.py" and captured["data"]["mode"] == "inline"
              and captured["data"]["start_line"] == 5, "open_file_panel payload shape")
        captured.clear()
        c.create_managed_session("agent", parent_session_id="root", cwd="/repo")
        check(
            captured["url"].endswith("/api/internal/managed-runs/create-session")
            and captured["data"]["name"] == "agent"
            and captured["data"]["parent_session_id"] == "root",
            "create_managed_session payload shape",
        )
        captured.clear()
        c.run_managed(
            "managed",
            "do it",
            parent_session_id="root",
            init_prompt="boot",
            agent_sid="sid",
            event_prefix="browser_harness",
            extra_env={"BU_CDP_URL": "http://127.0.0.1:1"},
        )
        check(
            captured["url"].endswith("/api/internal/managed-runs/run")
            and captured["data"]["managed_session_id"] == "managed"
            and captured["data"]["extra_env"] == {"BU_CDP_URL": "http://127.0.0.1:1"},
            "run_managed payload shape",
        )

        # Removed feature-specific methods must NOT exist on the generic Client.
        removed = [
            "get_requirements", "search_requirements", "capture_project_update",
            "list_project_updates", "mark_project_updates_seen",
            "credential_request", "credential_execute", "browser_harness",
            "create_schedule", "list_schedules", "delete_schedule",
            "recall_continuation", "broadcast_provider_config_change", "open_config_panel",
        ]
        leaked = [m for m in removed if hasattr(Client, m)]
        check(not leaked, f"feature-specific methods removed from generic SDK (leaked: {leaked})")

        permissions = PermissionSet(
            session_state=True,
            internal_loopback=True,
            storage=True,
        ).to_dict()
        check(
            permissions == {"session_state": True, "internal_loopback": True, "storage": True},
            "PermissionSet emits only enabled manifest permissions",
        )
        predicate = McpPredicate(equals={"orchestration_mode": "native"}, nonempty=("app_session_id",))
        mcp = McpServer(
            name="private-tool",
            python="mcp/server.py",
            args=("--stdio",),
            env={"PRIVATE_TOOL": "1"},
            predicate=predicate,
        ).to_dict()
        check(
            mcp["predicate"] == {
                "equals": {"orchestration_mode": "native"},
                "nonempty": ["app_session_id"],
            }
            and mcp["args"] == ["--stdio"]
            and mcp["env"] == {"PRIVATE_TOOL": "1"},
            "McpServer emits predicate/args/env manifest shape",
        )
        check(
            Instruction("rules", "instructions/rules.md", level="project").to_dict()
            == {"name": "rules", "path": "instructions/rules.md", "level": "project"},
            "Instruction emits manifest shape",
        )
        check(
            TeamDefinition("default", "teams/default.json").to_dict()
            == {"name": "default", "path": "teams/default.json"},
            "TeamDefinition emits manifest shape",
        )
        check(
            FrontendModule("session_panel", "Session panel", "ui/session-panel.js").to_dict()
            == {
                "slot": "session_panel",
                "id": "session_panel",
                "label": "Session panel",
                "kind": "module",
                "module": "ui/session-panel.js",
            },
            "FrontendModule emits manifest shape",
        )
    finally:
        urllib.request.urlopen = original_urlopen

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: extension sdk generic-core + delegation wrappers")
    return 0


if __name__ == "__main__":
    sys.exit(main_test())
