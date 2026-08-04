from __future__ import annotations

import ast
import asyncio
import inspect
import json
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

_test_home.isolate("bc-test-execution-template-")

from execution_template import (  # noqa: E402
    ExecutionArtifact,
    ExecutionAuthorityError,
    prepare_execution,
    validate_recovery_input,
    validate_recovery_sessions,
)
from execution_artifact_io import (  # noqa: E402
    bind_execution_input,
    load_execution_artifact,
)
from provider import Provider  # noqa: E402
import runs_dir  # noqa: E402


def _record(*, revision: int = 4, execution_revision: int = 3) -> dict:
    return {
        "id": "codex-work",
        "kind": "codex",
        "generation": "0a1f0f6c-f19f-4b9d-93d1-45d2db3af620",
        "revision": revision,
        "execution_revision": execution_revision,
    }


def _provider_record(*, revision: int = 4) -> dict:
    return {**_record(revision=revision), "kind": "claude"}


def _prepare_test_provider(self, **arguments):
    from provider_execution_contract import provider_family_contract

    return prepare_execution(
        self.record,
        provider_contract=provider_family_contract(
            self.record,
            payload={"test": "execution-template"},
        ),
        **arguments,
    )


def _arguments() -> dict:
    return {
        "run_id": "f5af980c-b88a-45c7-b1d2-07522350a379",
        "prompt": "finish the task",
        "cwd": "/workspace",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "session_id": "native-session",
        "mode": "native",
        "app_session_id": "app-session",
        "internal_token": "must-not-persist",
        "extra_env": {"API_TOKEN": "must-not-persist"},
        "provider_run_config": {
            "mcp_servers": {
                "private": {"command": "private-mcp", "args": ["serve"]},
            },
        },
        "capability_contexts": [
            {
                "name": "review",
                "provider_kind": "codex",
                "content_kind": "instructions",
                "content": "review carefully",
            },
        ],
        "resolved_harness_run_config": {
            "profile_id": "reviewer",
            "profile_revision": "revision-1",
        },
    }


def test_prepared_execution_is_detached_secret_free_and_authority_bound() -> None:
    arguments = _arguments()
    prepared = prepare_execution(_record(), **arguments)
    arguments["model"] = "mutated-after-prepare"
    arguments["provider_run_config"]["mcp_servers"].clear()

    assert prepared.start_arguments()["model"] == "gpt-5.6-sol"
    assert prepared.start_arguments()["provider_run_config"]["mcp_servers"]
    encoded = prepared.artifact.to_dict()
    serialized = json.dumps(encoded, sort_keys=True)
    assert "must-not-persist" not in serialized
    assert "internal_token" not in serialized
    assert "extra_env" not in serialized
    assert "backend_url" not in serialized
    assert "provider_run_config" in serialized
    assert "capability_contexts" in serialized
    assert "resolved_harness_run_config" in serialized
    assert "must-not-persist" not in repr(prepared)
    assert prepared.artifact.provider_revision == 4
    assert prepared.artifact.provider_execution_revision == 3
    prepared.artifact.require_authority(_record())
    prepared.artifact.require_authority(_record(revision=5))

    for changed in (
        _record(execution_revision=4),
        {**_record(), "generation": str(uuid.uuid4())},
        {**_record(), "id": "other"},
    ):
        try:
            prepared.artifact.require_authority(changed)
        except ExecutionAuthorityError:
            continue
        raise AssertionError(f"changed provider authority was accepted: {changed}")


def test_artifact_round_trip_is_strict_and_fingerprint_bound() -> None:
    artifact = prepare_execution(_record(), **_arguments()).artifact
    encoded = artifact.to_dict()
    assert ExecutionArtifact.from_dict(json.loads(json.dumps(encoded))) == artifact

    mutations = []
    unknown = json.loads(json.dumps(encoded))
    unknown["unexpected"] = True
    mutations.append(unknown)
    boolean_revision = json.loads(json.dumps(encoded))
    boolean_revision["provider_revision"] = True
    mutations.append(boolean_revision)
    boolean_execution_revision = json.loads(json.dumps(encoded))
    boolean_execution_revision["provider_execution_revision"] = True
    mutations.append(boolean_execution_revision)
    tampered_model = json.loads(json.dumps(encoded))
    tampered_model["template"]["arguments"]["model"] = "other"
    mutations.append(tampered_model)
    missing_fingerprint = json.loads(json.dumps(encoded))
    missing_fingerprint.pop("fingerprint")
    mutations.append(missing_fingerprint)

    for mutation in mutations:
        try:
            ExecutionArtifact.from_dict(mutation)
        except (ExecutionAuthorityError, ValueError):
            continue
        raise AssertionError(f"invalid execution artifact was accepted: {mutation}")

    unsafe = _arguments()
    unsafe["run_id"] = "../../escape"
    try:
        prepare_execution(_record(), **unsafe)
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe run_id reached execution persistence")

    invalid_arguments = (
        {"cwd": "relative/path"},
        {"backend_url": "http://user:secret@localhost:8000"},
        {"backend_url": 8000},
        {"images": ["not-an-object"]},
        {"files": [{"name": "missing-data"}]},
        {"disallowed_tools": ["Bash", 4]},
        {"continuation_chain": "not-a-list"},
        {"provider_run_config": []},
        {
            "provider_run_config": {
                "mcp_servers": {
                    "private": {"env": {"SERVICE_TOKEN": "must-not-persist"}},
                },
            },
        },
        {"capability_contexts": {}},
        {"resolved_harness_run_config": []},
        {"extra_env": {"TOKEN": 4}},
        {"source": 4},
        {"target_message_id": 4},
    )
    for override in invalid_arguments:
        candidate = {**_arguments(), **override}
        try:
            prepare_execution(_record(), **candidate)
        except ValueError:
            continue
        raise AssertionError(f"invalid execution arguments were accepted: {override}")


def test_recovery_input_is_diagnostic_only_and_must_match_artifact() -> None:
    prepared = prepare_execution(_record(), **_arguments())
    original = prepared.artifact.template.arguments()
    validate_recovery_input(prepared.artifact, dict(original))

    for key, value in (
        ("prompt", "mutated prompt"),
        ("cwd", "/different"),
        ("model", "different-model"),
        ("reasoning_effort", "low"),
        ("mode", "manager"),
        ("provider_id", "other-provider"),
    ):
        legacy = dict(original)
        legacy[key] = value
        try:
            validate_recovery_input(prepared.artifact, legacy)
        except ExecutionAuthorityError:
            continue
        raise AssertionError(f"recovery accepted mutated {key}")


def test_run_input_is_bound_to_exact_execution_authority() -> None:
    first = prepare_execution(_record(), **_arguments()).artifact
    second = prepare_execution(
        _record(revision=5),
        **{
            **_arguments(),
            "run_id": "504f29c0-8dd6-4cd9-8679-54a56943250c",
        },
    ).artifact
    payload = bind_execution_input(first, first.template.arguments())
    assert payload["execution_fingerprint"] == first.fingerprint
    assert "provider_generation" not in payload
    assert "provider_revision" not in payload

    with tempfile.TemporaryDirectory() as raw:
        run_dir = Path(raw)
        runs_dir.atomic_write_json(
            run_dir / "execution.json",
            first.to_dict(),
        )
        runs_dir.atomic_write_json(run_dir / "input.json", payload)
        assert load_execution_artifact(
            run_dir,
            validate_input=True,
        ) == first

        stale = bind_execution_input(second, second.template.arguments())
        runs_dir.atomic_write_json(run_dir / "input.json", stale)
        try:
            load_execution_artifact(run_dir, validate_input=True)
        except ExecutionAuthorityError:
            pass
        else:
            raise AssertionError("cross-run input authority was accepted")

        missing = dict(payload)
        missing.pop("execution_fingerprint")
        runs_dir.atomic_write_json(run_dir / "input.json", missing)
        try:
            load_execution_artifact(run_dir, validate_input=True)
        except ExecutionAuthorityError:
            pass
        else:
            raise AssertionError("unbound legacy input authority was accepted")


def test_retry_preserves_authority_and_original_frozen_inputs() -> None:
    prepared = prepare_execution(_record(), **_arguments())
    retry = prepared.retry(
        run_id="8a012cf5-b8a1-4407-92b3-59e6e027d148",
        session_id="resumed-session",
    )

    assert retry.artifact.provider_generation == prepared.artifact.provider_generation
    assert retry.artifact.provider_revision == prepared.artifact.provider_revision
    assert (
        retry.artifact.provider_execution_revision
        == prepared.artifact.provider_execution_revision
    )
    assert retry.start_arguments()["run_id"] != prepared.start_arguments()["run_id"]
    assert retry.start_arguments()["session_id"] == "resumed-session"
    assert retry.start_arguments()["model"] == "gpt-5.6-sol"
    assert retry.artifact.fingerprint != prepared.artifact.fingerprint
    local_url = prepared.retry(backend_url="http://node.local:8000")
    assert local_url.artifact == prepared.artifact
    assert local_url.start_arguments()["backend_url"] == "http://node.local:8000"

    worker_arguments = {**_arguments(), "app_session_id": "worker-session"}
    routed = prepare_execution(
        _record(),
        routing_session_id="manager-session",
        **worker_arguments,
    )
    assert routed.artifact.routing_session_id == "manager-session"
    assert routed.start_arguments()["app_session_id"] == "worker-session"
    assert (
        ExecutionArtifact.from_dict(routed.artifact.to_dict()).routing_session_id
        == "manager-session"
    )
    validate_recovery_sessions(
        routed.artifact,
        routing_session_id="manager-session",
        persist_session_id="worker-session",
    )
    for route, persist in (
        ("other-manager", "worker-session"),
        ("manager-session", "other-worker"),
    ):
        try:
            validate_recovery_sessions(
                routed.artifact,
                routing_session_id=route,
                persist_session_id=persist,
            )
        except ExecutionAuthorityError:
            continue
        raise AssertionError("recovery accepted conflicting routing authority")


def test_provider_start_run_is_shared_final_template() -> None:
    assert not getattr(Provider.start_run, "__isabstractmethod__", False)
    assert getattr(Provider._start_run, "__isabstractmethod__", False)
    signature = inspect.signature(Provider.start_run)
    assert tuple(signature.parameters) == (
        "self",
        "execution",
        "loop",
        "queue",
    )

    provider_files = [
        ROOT / name
        for name in (
            "provider_agy.py",
            "provider_amp.py",
            "provider_claude.py",
            "provider_codex.py",
            "provider_copilot.py",
            "provider_cursor.py",
            "provider_kimi.py",
            "provider_openai.py",
            "provider_opencode.py",
            "provider_pi.py",
            "provider_qwen.py",
            "provider_remote.py",
        )
    ]
    offenders = []
    for path in provider_files:
        source = path.read_text(encoding="utf-8")
        if "\n    def start_run(" in source:
            offenders.append(path.name)
            continue
        tree = ast.parse(source)
        start_nodes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_start_run"
        ]
        if not start_nodes:
            offenders.append(path.name)
            continue
        for node in start_nodes:
            keyword_names = [
                argument.arg for argument in node.args.kwonlyargs
            ]
            execution_index = keyword_names.index("_execution")
            if node.args.kw_defaults[execution_index] is not None:
                offenders.append(path.name)
    assert offenders == []


def test_provider_boundary_uses_frozen_execution_without_blocking_config_replace() -> None:
    class _Provider(Provider):
        KIND = "claude"
        prepare_run = _prepare_test_provider

        def __init__(self) -> None:
            super().__init__(_provider_record())
            self._runs = {}
            self.events: list[str] = []

        @contextmanager
        def _execution_authority_context(self, execution, start_arguments):
            del execution, start_arguments
            yield self.record

        def assert_not_suspended(self, *, action: str = "start runs") -> None:
            self.events.append(f"suspension:{action}")

        def require_runtime_credential(self) -> None:
            self.events.append("credential")

        def _admit_execution(self, execution) -> None:
            assert execution.artifact.provider_revision == 4
            self.events.append("admission")

        def build_env(self) -> dict[str, str]:
            return {}

        def _start_run(self, **kwargs) -> bool:
            assert kwargs["internal_token"] == "must-not-persist"
            replacement_done = threading.Event()
            replacement = threading.Thread(
                target=lambda: (
                    setattr(
                        self,
                        "record",
                        {**_provider_record(), "revision": 5},
                    ),
                    replacement_done.set(),
                ),
            )
            replacement.start()
            assert replacement_done.wait(0.2)
            assert self.record["revision"] == 5
            self.events.append("spawn")
            self.replacement = replacement
            return True

        def _write_backend_state(self, rs) -> None:
            del rs

        def recover_in_flight(self, loop=None, run_id_filter=None) -> list[dict]:
            del loop, run_id_filter
            return []

        def prune_old_runs(self, max_age_days: int = 7) -> int:
            del max_age_days
            return 0

        async def run_headless(self, *args, **kwargs) -> str:
            del args, kwargs
            return ""

        async def rewind(self, rewind_session_id: str, message_uuid: str) -> None:
            del rewind_session_id, message_uuid

    provider = _Provider()
    prepared = provider.prepare_run(**_arguments())
    with tempfile.TemporaryDirectory() as raw:
        original_runs_root = runs_dir.runs_root
        runs_dir.runs_root = lambda: Path(raw)
        loop = asyncio.new_event_loop()
        try:
            provider.start_run(
                execution=prepared,
                loop=loop,
                queue=asyncio.Queue(),
            )
            provider.replacement.join(timeout=1)
            assert not provider.replacement.is_alive()
        finally:
            loop.close()
            runs_dir.runs_root = original_runs_root

        persisted = json.loads(
            (Path(raw) / _arguments()["run_id"] / "execution.json").read_text(
                encoding="utf-8",
            ),
        )
        assert ExecutionArtifact.from_dict(persisted) == prepared.artifact
        assert provider.events == [
            "suspension:start new runs",
            "credential",
            "admission",
            "spawn",
        ]

    provider.events.clear()
    provider.record = {**_provider_record(), "execution_revision": 4}
    stale_loop = asyncio.new_event_loop()
    try:
        provider.start_run(
            execution=prepared,
            loop=stale_loop,
            queue=asyncio.Queue(),
        )
    except ExecutionAuthorityError:
        pass
    else:
        raise AssertionError("stale provider execution was spawned")
    finally:
        stale_loop.close()
    assert provider.events == []

def test_provider_starts_are_concurrent() -> None:
    class _Provider(Provider):
        KIND = "claude"
        prepare_run = _prepare_test_provider

        def __init__(self) -> None:
            super().__init__(_provider_record())
            self._runs = {}
            self.spawn_gate = threading.Event()
            self.spawn_started = threading.Event()
            self.spawned: list[str] = []

        @contextmanager
        def _execution_authority_context(self, execution, start_arguments):
            del execution, start_arguments
            yield self.record

        def assert_not_suspended(self, *, action: str = "start runs") -> None:
            del action

        def require_runtime_credential(self) -> None:
            return None

        def build_env(self) -> dict[str, str]:
            return {}

        def _start_run(self, **kwargs) -> bool:
            self.spawned.append(kwargs["run_id"])
            self.spawn_started.set()
            self.spawn_gate.wait(timeout=1)
            return True

        def _write_backend_state(self, rs) -> None:
            del rs

        def recover_in_flight(self, loop=None, run_id_filter=None) -> list[dict]:
            del loop, run_id_filter
            return []

        def prune_old_runs(self, max_age_days: int = 7) -> int:
            del max_age_days
            return 0

        async def run_headless(self, *args, **kwargs) -> str:
            del args, kwargs
            return ""

        async def rewind(self, rewind_session_id: str, message_uuid: str) -> None:
            del rewind_session_id, message_uuid

    provider = _Provider()
    loops = [asyncio.new_event_loop() for _ in range(2)]
    arguments = [
        {**_arguments(), "run_id": f"run-{index}", "turn_run_id": f"turn-{index}"}
        for index in range(2)
    ]
    executions = [provider.prepare_run(**item) for item in arguments]
    with tempfile.TemporaryDirectory() as raw:
        original_runs_root = runs_dir.runs_root
        runs_dir.runs_root = lambda: Path(raw)
        threads = [
            threading.Thread(
                target=provider.start_run,
                kwargs={
                    "execution": execution,
                    "loop": loop,
                    "queue": asyncio.Queue(),
                },
            )
            for execution, loop in zip(executions, loops)
        ]
        try:
            threads[0].start()
            threads[1].start()
            deadline = threading.Event()
            assert deadline.wait(0.2) is False
            assert sorted(provider.spawned) == ["run-0", "run-1"]

            provider.spawn_gate.set()
            for thread in threads:
                thread.join(timeout=1)
                assert not thread.is_alive()
        finally:
            provider.spawn_gate.set()
            for loop in loops:
                loop.close()
            runs_dir.runs_root = original_runs_root

    assert sorted(provider.spawned) == ["run-0", "run-1"]


def test_spawn_commit_linearizes_cancellation() -> None:
    class _Provider(Provider):
        KIND = "claude"
        prepare_run = _prepare_test_provider

        def __init__(self) -> None:
            super().__init__(_provider_record())
            self._runs = {}
            self.spawn_started = threading.Event()
            self.spawn_gate = threading.Event()
            self.spawned: list[str] = []

        @contextmanager
        def _execution_authority_context(self, execution, start_arguments):
            del execution, start_arguments
            yield self.record

        def assert_not_suspended(self, *, action: str = "start runs") -> None:
            del action

        def require_runtime_credential(self) -> None:
            return None

        def build_env(self) -> dict[str, str]:
            return {}

        def _start_run(self, **kwargs) -> bool:
            self.spawned.append(kwargs["run_id"])
            self.spawn_started.set()
            self.spawn_gate.wait(timeout=1)
            return True

        def _write_backend_state(self, rs) -> None:
            del rs

        def recover_in_flight(self, loop=None, run_id_filter=None) -> list[dict]:
            del loop, run_id_filter
            return []

        def prune_old_runs(self, max_age_days: int = 7) -> int:
            del max_age_days
            return 0

        async def run_headless(self, *args, **kwargs) -> str:
            del args, kwargs
            return ""

        async def rewind(self, rewind_session_id: str, message_uuid: str) -> None:
            del rewind_session_id, message_uuid

    provider = _Provider()
    loop = asyncio.new_event_loop()
    with tempfile.TemporaryDirectory() as raw:
        original_runs_root = runs_dir.runs_root
        runs_dir.runs_root = lambda: Path(raw)
        try:
            before = provider.prepare_run(
                **{**_arguments(), "run_id": "cancel-before"},
            )
            before._mark_cancelled()
            assert provider.start_run(
                execution=before,
                loop=loop,
                queue=asyncio.Queue(),
            ) is False
            assert "cancel-before" not in provider.spawned
            assert not (Path(raw) / "cancel-before").exists()

            after = provider.prepare_run(
                **{**_arguments(), "run_id": "cancel-after"},
            )
            thread = threading.Thread(
                target=provider.start_run,
                kwargs={
                    "execution": after,
                    "loop": loop,
                    "queue": asyncio.Queue(),
                },
            )
            thread.start()
            assert provider.spawn_started.wait(timeout=1)
            after._request_cancel_after_admission()
            provider.spawn_gate.set()
            thread.join(timeout=1)
            assert not thread.is_alive()
            assert (Path(raw) / "cancel-after" / "cancel").exists()
        finally:
            provider.spawn_gate.set()
            loop.close()
            runs_dir.runs_root = original_runs_root


def test_production_callers_use_prepared_execution_boundary() -> None:
    direct_callers = []
    for path in ROOT.rglob("*.py"):
        if (
            "scripts" in path.parts
            or path.name == "provider.py"
            or any(part.startswith(".") for part in path.relative_to(ROOT).parts)
        ):
            continue
        source = path.read_text(encoding="utf-8")
        if ".start_run," in source or ".start_run(" in source:
            direct_callers.append(str(path.relative_to(ROOT)))
    assert direct_callers == []


TESTS = (
    test_prepared_execution_is_detached_secret_free_and_authority_bound,
    test_artifact_round_trip_is_strict_and_fingerprint_bound,
    test_recovery_input_is_diagnostic_only_and_must_match_artifact,
    test_run_input_is_bound_to_exact_execution_authority,
    test_retry_preserves_authority_and_original_frozen_inputs,
    test_provider_start_run_is_shared_final_template,
    test_provider_boundary_uses_frozen_execution_without_blocking_config_replace,
    test_provider_starts_are_concurrent,
    test_spawn_commit_linearizes_cancellation,
    test_production_callers_use_prepared_execution_boundary,
)


if __name__ == "__main__":
    started = asyncio.get_event_loop_policy()
    del started
    for test in TESTS:
        test()
    print("PASS execution template")
