"""Security + contract tests for the Better Agent Integration SDK surface.

Locks the new ``POST /api/internal/provisioned-sessions`` endpoint (the SDK
primitive that lets extensions spawn provisioned runs) and the SDK client:

Endpoint:
  - internal-token required (403 without/wrong)
  - calling extension must be active AND declare ``spawn_runs`` (403 otherwise,
    including a core token that has no extension identity)
  - unknown spec_key -> 404; bad body shapes -> 400 without 500
  - happy path dispatches ``provisioning.run`` and returns text/value/base_session_id

SDK client:
  - propagates X-Internal-Token (identity is token-derived; no X-Extension-Id)
  - raises BetterAgentError without a token

Run standalone:  python scripts/test_extension_sdk_provisioning.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import _test_home
_TMP_HOME = _test_home.isolate("bc-test-sdkprov-")
os.environ["BETTER_CLAUDE_TEST_AUTH_BYPASS"] = "1"
# Point the required-marketplace lookup at the temp home so the marketplace
# package is simply absent (no network fetch during extension_store._load).
os.environ["BETTER_AGENT_MARKETPLACE_EXTENSION_REPO_PATH"] = _TMP_HOME

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
_REPO = os.path.dirname(_BACKEND)
for _p in (_BACKEND, os.path.join(_REPO, "sdk")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from starlette.testclient import TestClient  # noqa: E402
import main  # noqa: E402
import extension_store  # noqa: E402
import config_store  # noqa: E402
import provisioning  # noqa: E402
import provider  # noqa: E402
import extension_token_registry  # noqa: E402
from better_agent_sdk import BetterAgentError, Client  # noqa: E402

failures: list[str] = []


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    if not cond:
        failures.append(msg)


CLIENT = TestClient(main.app, client=("127.0.0.1", 50001))
TOKEN = main.coordinator.internal_token

SPAWN_EXT = "test.spawn-ext"
NOSPAWN_EXT = "test.nospawn-ext"
INLINE_TASK = "test_inline_task"


def _seed_extension(extension_id: str, *, spawn_runs: bool) -> None:
    data = extension_store._load()
    data["extensions"][extension_id] = {
        "manifest": {
            "id": extension_id,
            "permissions": {"spawn_runs": True} if spawn_runs else {},
        },
        "enabled": True,
        "source": {"type": "git", "install_path": ""},
        "entitlement": {"status": "not_required"},
    }
    extension_store._save(data)


def _install_team_orchestration_extension() -> None:
    extension_id = extension_store.BUILTIN_TEAM_ORCHESTRATION_EXTENSION_ID
    package = Path(_TMP_HOME) / "team-orchestration-fixture"
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    manifest = {
        "kind": extension_store.MANIFEST_KIND,
        "id": extension_id,
        "name": extension_id,
        "version": "1.0.0",
        "description": extension_id,
        "surfaces": ["backend_feature"],
        "entrypoints": {},
        "permissions": {},
        "protocol": {
            "version": 1,
            "smoke_test": {"required_paths": ["better-agent-extension.json"], "python_modules": []},
        },
        "marketplace": {},
    }
    (package / "better-agent-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    extension_store._install_from_package_dir(  # type: ignore[attr-defined]
        package_dir=package,
        source={
            "type": "better_agent_local",
            "repo_url": str(package.parent),
            "extension_path": package.name,
            "ref": "",
            "commit_sha": extension_id,
        },
        persist=True,
    )
    providers = config_store.list_providers()["providers"]
    provider = providers[0]
    assignments = config_store.get_internal_llm_assignments()
    assignments["default_session"] = {
        "provider_id": provider["id"],
        "model": provider["default_model"],
        "reasoning_effort": provider.get("default_reasoning_effort") or "",
    }
    config_store.set_internal_llm_assignments(assignments)


_SENTINEL = object()


def _post(body=None, *, token=_SENTINEL, extension_id=SPAWN_EXT, raw=None):
    # Identity is token-derived. Default: send the calling extension's minted
    # token (extension_id=None -> core token, which has no extension principal).
    # An explicit `token` override (e.g. "wrong-token"/None) exercises auth.
    if token is _SENTINEL:
        token = TOKEN if extension_id is None else extension_token_registry.mint(extension_id)
    headers = {}
    if token is not None:
        headers["X-Internal-Token"] = token
    if raw is not None:
        headers["Content-Type"] = "application/json"
        return CLIENT.post(
            "/api/internal/provisioned-sessions", content=raw, headers=headers
        )
    return CLIENT.post("/api/internal/provisioned-sessions", json=body, headers=headers)


def _inline_spec(**overrides) -> dict:
    spec = {
        "key": "inline-worker",
        "version": 2,
        "name": "Inline worker",
        "task_key": INLINE_TASK,
        "provision_prompt": "Prime inline worker",
        "instructions": "Answer inline request",
        "dirty_policy": {
            "max_base_bytes": 128_000,
            "max_user_turns": 1,
            "max_assistant_turns": 1,
            "leak_markers": ["Prime inline worker"],
        },
    }
    spec.update(overrides)
    return spec


class _FakeSpec(provisioning.ProvisionedSessionSpec):
    key = "test-fake-spec"


def main_test() -> int:
    _seed_extension(SPAWN_EXT, spawn_runs=True)
    _seed_extension(NOSPAWN_EXT, spawn_runs=False)
    extension_store._BUILTIN_INTERNAL_LLM_TASKS[SPAWN_EXT] = (INLINE_TASK,)
    provisioning.register(_FakeSpec())

    run_calls = []

    async def _fake_run(spec, query, ctx):
        run_calls.append((spec.key, query, ctx))
        return SimpleNamespace(text="fork reply", value={"ok": 1}, base_session_id="base-123")

    original_run = provisioning.run
    provisioning.run = _fake_run
    try:
        print("T1 auth: internal token required")
        r = _post({"spec_key": "test-fake-spec", "query": "q"}, token="wrong-token")
        check(r.status_code == 403, f"wrong token -> 403 (got {r.status_code})")
        r = _post({"spec_key": "test-fake-spec", "query": "q"}, token=None)
        check(r.status_code in (403, 422), f"missing token rejected (got {r.status_code})")

        print("T2 extension identity + spawn_runs permission")
        r = _post({"spec_key": "test-fake-spec", "query": "q"}, extension_id=None)
        check(r.status_code == 403, f"core token (no extension identity) -> 403 (got {r.status_code})")
        r = _post({"spec_key": "test-fake-spec", "query": "q"}, extension_id=NOSPAWN_EXT)
        check(r.status_code == 403, f"extension without spawn_runs -> 403 (got {r.status_code})")
        r = _post({"spec_key": "test-fake-spec", "query": "q"}, extension_id="test.not-installed")
        check(r.status_code == 403, f"unknown extension -> 403 (got {r.status_code})")

        print("T3 unknown spec + bad body -> 4xx, never 500")
        r = _post({"spec_key": "no-such-spec", "query": "q"})
        check(r.status_code == 404, f"unknown spec -> 404 (got {r.status_code})")
        r = _post({"query": "q"})
        check(r.status_code == 400, f"missing spec_key -> 400 (got {r.status_code})")
        r = _post({"spec_key": "test-fake-spec", "query": "q", "ctx": [1, 2]})
        check(r.status_code == 400, f"ctx not object -> 400 (got {r.status_code})")
        r = _post({"spec_key": "test-fake-spec", "inline_spec": _inline_spec(), "query": "q"})
        check(r.status_code == 400, f"spec_key + inline_spec rejected (got {r.status_code})")
        r = _post({"inline_spec": _inline_spec(task_key="unknown_task"), "query": "q"})
        check(r.status_code == 400, f"undeclared inline task_key rejected (got {r.status_code})")
        r = _post({"inline_spec": _inline_spec(default_cwd="/tmp"), "query": "q"})
        check(r.status_code == 400, f"unsupported inline field rejected (got {r.status_code})")
        r = _post({"inline_spec": _inline_spec(node_id="remote-node"), "query": "q"})
        check(r.status_code == 400, f"non-primary inline node rejected (got {r.status_code})")
        nan_spec = json.dumps(_inline_spec())[:-1] + ',"provision_timeout":NaN}'
        r = _post(raw=f'{{"inline_spec":{nan_spec},"query":"q"}}')
        check(r.status_code == 400, f"non-finite inline timeout rejected (got {r.status_code})")
        r = _post(raw='{"spec_key":"test-fake-spec","query":"q","ctx":{"a":1}}')
        check(r.status_code == 200, f"valid raw-json body accepted (got {r.status_code})")

        print("T4 happy path dispatches provisioning.run")
        run_calls.clear()
        r = _post({"spec_key": "test-fake-spec", "query": "do thing", "ctx": {"k": "v"}})
        body = r.json()
        check(r.status_code == 200 and body.get("success") is True, f"success (got {r.status_code} {body})")
        check(body.get("text") == "fork reply", "returns fork text")
        check(body.get("value") == {"ok": 1}, "returns parsed value")
        check(body.get("base_session_id") == "base-123", "returns base session id")
        check(run_calls == [("test-fake-spec", "do thing", {"k": "v"})],
              f"provisioning.run called once with forwarded args (got {run_calls})")
    finally:
        provisioning.run = original_run

    print("T4b inline spec dispatches namespaced runtime spec")
    inline_calls = []

    async def _fake_inline_run(spec, query, ctx):
        inline_calls.append({
            "key": spec.key,
            "version": spec.version,
            "name": spec.name,
            "task_key": spec.task_key,
            "query": query,
            "ctx": ctx,
            "provision_prompt": spec.build_provision_prompt(ctx),
            "instructions": spec.build_instructions(query, ctx),
            "value": spec.parse_result("inline reply", ctx),
            "run_mode": spec.run_mode,
            "dispatch": spec.dispatch,
            "worker_creation_policy": spec.worker_creation_policy,
            "machine_completion": spec.machine_completion,
            "bare_config": spec.bare_config,
        })
        return SimpleNamespace(text="inline reply", value="inline reply", base_session_id="base-inline")

    provisioning.run = _fake_inline_run
    try:
        r = _post({"inline_spec": _inline_spec(), "query": "inline query", "ctx": {"k": "v"}})
        body = r.json()
        check(r.status_code == 200 and body.get("success") is True, f"inline success (got {r.status_code} {body})")
        check(body.get("text") == "inline reply", "inline returns fork text")
        check(body.get("value") == "inline reply", "inline value is raw text")
        check(body.get("base_session_id") == "base-inline", "inline returns base session id")
        expected = [{
            "key": f"extension:{SPAWN_EXT}:inline-worker",
            "version": 2,
            "name": "Inline worker",
            "task_key": INLINE_TASK,
            "query": "inline query",
            "ctx": {"k": "v"},
            "provision_prompt": "Prime inline worker",
            "instructions": "Answer inline request",
            "value": "inline reply",
            "run_mode": "fork",
            "dispatch": "in_process",
            "worker_creation_policy": "deny",
            "machine_completion": True,
            "bare_config": True,
        }]
        check(inline_calls == expected, f"inline spec constrained + forwarded (got {inline_calls})")
    finally:
        provisioning.run = original_run

    print("T4c inline spec resolves config only through declared task")
    spec = provisioning.inline_spec_from_payload(
        _inline_spec(),
        extension_id=SPAWN_EXT,
        allowed_task_keys={INLINE_TASK},
    )
    resolved_task_keys = []
    original_resolve_internal_llm = config_store.resolve_internal_llm
    original_get_provider = provider.get_provider

    class _ForkProvider:
        supports_fork = True

    class _NoForkProvider:
        supports_fork = False

    def _resolve_internal_llm(task_key: str) -> dict:
        resolved_task_keys.append(task_key)
        return {
            "provider_id": "provider-inline",
            "model": "model-inline",
            "reasoning_effort": "high",
        }

    config_store.resolve_internal_llm = _resolve_internal_llm
    provider.get_provider = lambda _provider_id: _ForkProvider()
    try:
        cfg = spec.build_config()
        check(resolved_task_keys == [INLINE_TASK], f"inline config uses declared task only (got {resolved_task_keys})")
        check(cfg.provider_id == "provider-inline" and cfg.model == "model-inline",
              "inline config uses task provider/model")
        check(cfg.reasoning_effort == "high", "inline config forwards task reasoning effort")
        check(cfg.cwd == str(Path(os.getcwd()).expanduser().resolve()), "inline config pins cwd to backend process cwd")
        check(cfg.node_id == "primary" and cfg.run_mode == "fork" and cfg.dispatch == "in_process",
              "inline config pins node/run/dispatch")
        override_rejected = False
        try:
            spec.build_config(model="caller-model")
        except RuntimeError:
            override_rejected = True
        check(override_rejected, "inline config rejects per-call model override")
        provider.get_provider = lambda _provider_id: _NoForkProvider()
        no_fork_rejected = False
        try:
            spec.build_config()
        except RuntimeError:
            no_fork_rejected = True
        check(no_fork_rejected, "inline config rejects non-fork provider")
    finally:
        config_store.resolve_internal_llm = original_resolve_internal_llm
        provider.get_provider = original_get_provider

    print("T5 provisioning.run failure -> success:false, not 5xx")
    async def _boom(spec, query, ctx):
        raise RuntimeError("fork exploded")
    provisioning.run = _boom
    try:
        r = _post({"spec_key": "test-fake-spec", "query": "q"})
        check(r.status_code == 200 and r.json().get("success") is False
              and "fork exploded" in r.json().get("error", ""),
              "dispatch error surfaced as success:false")
    finally:
        provisioning.run = original_run

    print("T6 real team-definition route guards")
    missing_team = CLIENT.post(
        "/api/internal/team-definitions/list",
        headers={"X-Internal-Token": TOKEN},
        json={},
    )
    check(missing_team.status_code == 404, f"team extension missing -> 404 (got {missing_team.status_code})")
    _install_team_orchestration_extension()
    wrong_token = CLIENT.post(
        "/api/internal/team-definitions/list",
        headers={"X-Internal-Token": "wrong"},
        json={},
    )
    check(wrong_token.status_code == 403, f"wrong token -> 403 (got {wrong_token.status_code})")
    listed = CLIENT.post(
        "/api/internal/team-definitions/list",
        headers={"X-Internal-Token": TOKEN},
        json={},
    )
    check(
        listed.status_code == 200 and isinstance(listed.json().get("team_definitions"), list),
        f"team definitions list succeeds with runtime extension (got {listed.status_code} {listed.text})",
    )
    bad_plan = CLIENT.post(
        "/api/internal/team-definitions/plan",
        headers={"X-Internal-Token": TOKEN},
        json={"source_id": "missing", "profile": "web", "team_instance_id": "team-1"},
    )
    check(bad_plan.status_code == 400, f"team definition plan validates real route (got {bad_plan.status_code})")
    root = main.session_manager.create(
        name="team-root",
        cwd="/tmp/repo",
        orchestration_mode="native",
        model="test-model",
        source="cli",
    )
    activation_response = CLIENT.post(
        "/api/internal/team-definitions/activate",
        headers={"X-Internal-Token": TOKEN},
        json={
            "root_session_id": root["id"],
            "plan": {
                "source_id": "manual",
                "profile": "test",
                "team_instance_id": "team-real-route",
                "manager": {"id": "manager", "cwd": "/tmp/repo"},
                "activate": [],
            },
            "cwd": "/tmp/repo",
        },
    )
    activation_body = activation_response.json()
    activation_id = (activation_body.get("activation") or {}).get("id")
    check(
        activation_response.status_code == 200 and bool(activation_id),
        f"team definition activate starts real route (got {activation_response.status_code} {activation_response.text})",
    )
    if activation_id:
        final_status = ""
        for _ in range(20):
            poll = CLIENT.get(
                f"/api/internal/team-definitions/activate/{activation_id}",
                headers={"X-Internal-Token": TOKEN},
            )
            final_status = (poll.json().get("activation") or {}).get("status", "")
            if final_status == "complete":
                break
            time.sleep(0.05)
        check(final_status == "complete", f"team activation completes through real route (got {final_status!r})")

    print("S1 SDK client reads env")
    os.environ["BETTER_AGENT_BACKEND_URL"] = "http://env-core:9000"
    os.environ["BETTER_AGENT_INTERNAL_TOKEN"] = "env-tok"
    os.environ["BETTER_AGENT_EXTENSION_ID"] = "env-ext"
    try:
        c = Client()
        check(c.backend_url == "http://env-core:9000", "backend_url from env")
        check(c.internal_token == "env-tok", "internal_token from env")
        check(c.extension_id == "env-ext", "extension_id from env")
    finally:
        for k in ("BETTER_AGENT_BACKEND_URL", "BETTER_AGENT_INTERNAL_TOKEN", "BETTER_AGENT_EXTENSION_ID"):
            os.environ.pop(k, None)

    print("S2 SDK _post propagates headers + payload")
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"success": true}'

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["data"] = req.data.decode("utf-8")
        return _FakeResp()

    original_urlopen = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen
    try:
        client = Client(internal_token="tok", extension_id="ext-1", backend_url="http://core")
        out = client.create_provisioned_session(
            "spec", "query", {"a": 1}
        )
        registered_captured = dict(captured)
        inline_payload = _inline_spec()
        inline_out = client.create_inline_provisioned_session(inline_payload, query="inline", ctx={"b": 2})
        inline_captured = dict(captured)
    finally:
        urllib.request.urlopen = original_urlopen
    check(out == {"success": True}, "returns parsed body")
    check(
        registered_captured["url"].endswith("/api/internal/provisioned-sessions"),
        f"posts to right path (got {registered_captured['url']})",
    )
    check(registered_captured["headers"].get("x-internal-token") == "tok", "sends X-Internal-Token")
    check("x-extension-id" not in registered_captured["headers"], "does not send X-Extension-Id (identity is token-derived)")
    check(json.loads(registered_captured["data"]) == {"spec_key": "spec", "query": "query", "ctx": {"a": 1}},
          "sends correct payload")
    check(inline_out == {"success": True}, "inline SDK returns parsed body")
    check(json.loads(inline_captured["data"]) == {"inline_spec": inline_payload, "query": "inline", "ctx": {"b": 2}},
          "inline SDK sends correct payload")

    print("S2b SDK team methods use core integration endpoints")
    captured_calls = []
    activation_poll_count = {"value": 0}

    def _fake_urlopen_many(req, timeout=None):
        if req.full_url.endswith("/api/internal/team-definitions/activate/team-act-1"):
            activation_poll_count["value"] += 1
            status = "complete" if activation_poll_count["value"] >= 2 else "running"

            class _ActivationResp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps(
                        {"success": True, "activation": {"id": "team-act-1", "status": status}}
                    ).encode("utf-8")

            captured_calls.append({
                "url": req.full_url,
                "method": req.get_method(),
                "headers": {k.lower(): v for k, v in req.header_items()},
                "data": json.loads(req.data.decode("utf-8")) if req.data else None,
            })
            return _ActivationResp()
        captured_calls.append({
            "url": req.full_url,
            "method": req.get_method(),
            "headers": {k.lower(): v for k, v in req.header_items()},
            "data": json.loads(req.data.decode("utf-8")) if req.data else None,
        })
        return _FakeResp()

    urllib.request.urlopen = _fake_urlopen_many
    try:
        team_client = Client(internal_token="tok", extension_id="ext-1", backend_url="http://core")
        team_client.list_team_definitions()
        team_client.plan_team_definition("src", "web", "team-1", {"TARGET_REPO": "/repo"})
        team_client.create_team("root-1", definition_ref="src", profile="web", team_id="team-1")
        team_client.register_team_member(
            "team-1",
            "worker-a",
            "worker",
            agent_session_id="sid-1",
            role="role-a",
            cwd="/repo",
        )
        team_client.provision_workers("/repo", [{"role_key": "worker-a", "cwd": "/repo"}], team_instance_id="team-1")
        team_client.start_team_activation("root-1", plan={"team_instance_id": "team-1"}, cwd="/repo")
        team_client.get_team_activation("team-act-1")
        waited = team_client.wait_team_activation("team-act-1", poll_interval=0.05)
        team_client.create_session(
            "name",
            "/repo",
            orchestration_mode="native",
            provider_id="claude",
            bare_config=True,
            capability_contexts=[{"source_id": "ctx"}],
        )
    finally:
        urllib.request.urlopen = original_urlopen
    check(
        [call["method"] for call in captured_calls]
        == ["POST", "POST", "POST", "POST", "POST", "POST", "GET", "GET", "POST"],
          "team SDK methods use expected HTTP verbs")
    check(
        captured_calls[0]["url"].endswith("/api/internal/team-definitions/list"),
        "lists team definitions through core internal endpoint",
    )
    check(
        captured_calls[1]["url"].endswith("/api/internal/team-definitions/plan"),
        "plans team definition through core internal endpoint",
    )
    check(captured_calls[1]["data"]["variables"] == {"TARGET_REPO": "/repo"}, "forwards plan variables")
    check(captured_calls[2]["url"].endswith("/api/internal/teams/create"), "creates runtime team")
    check(captured_calls[3]["url"].endswith("/api/internal/teams/register-member"), "registers team member")
    check(captured_calls[3]["data"]["agent_session_id"] == "sid-1", "register member forwards agent_session_id")
    check(captured_calls[4]["url"].endswith("/api/internal/workers/provision"), "provisions workers")
    check(captured_calls[4]["data"]["team_instance_id"] == "team-1", "forwards team id to worker provisioning")
    check(captured_calls[5]["url"].endswith("/api/internal/team-definitions/activate"), "starts team activation")
    check(captured_calls[5]["data"]["plan"] == {"team_instance_id": "team-1"}, "forwards activation plan")
    check(captured_calls[6]["url"].endswith("/api/internal/team-definitions/activate/team-act-1"), "polls team activation")
    check(waited["activation"]["status"] == "complete", "wait_team_activation polls until complete")
    check(captured_calls[8]["url"].endswith("/api/internal/create-session"), "creates session")
    check(captured_calls[8]["data"]["bare_config"] is True, "create_session forwards bare_config")
    check(captured_calls[8]["data"]["capability_contexts"] == [{"source_id": "ctx"}],
          "create_session forwards capability contexts")

    failed_client = Client(internal_token="tok", extension_id="ext-1", backend_url="http://core")
    failed_polls = {"value": 0}

    def _fake_failed_activation(req, timeout=None):
        failed_polls["value"] += 1

        class _FailedActivationResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"success": true, "activation": {"id": "team-act-2", "status": "failed", "error": "worker failed"}}'

        return _FailedActivationResp()

    urllib.request.urlopen = _fake_failed_activation
    failed_raised = False
    try:
        failed_client.wait_team_activation("team-act-2", poll_interval=0.05)
    except BetterAgentError as exc:
        failed_raised = "worker failed" in str(exc)
    finally:
        urllib.request.urlopen = original_urlopen
    check(failed_raised and failed_polls["value"] == 1, "failed activation raises BetterAgentError")

    print("S3 SDK without token raises")
    no_tok = Client(backend_url="http://core")
    no_tok.internal_token = ""
    raised = False
    try:
        no_tok.create_provisioned_session("k", "q")
    except BetterAgentError:
        raised = True
    check(raised, "missing token -> BetterAgentError")

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("OK: integration sdk + provisioned-sessions endpoint")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main_test())
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
