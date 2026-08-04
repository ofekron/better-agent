from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _test_home  # noqa: E402

TEST_HOME = _test_home.TestHome.acquire(
    "bc-test-claude-agy-execution-artifact-",
)

import config_store  # noqa: E402
from paths import encode_cwd  # noqa: E402
from provider_agy import AgyProvider  # noqa: E402
from provider_claude import ClaudeProvider  # noqa: E402
from provider_family_launch_attestation import (  # noqa: E402
    FamilyLaunchAttestation,
)
import session_manager  # noqa: E402


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _provider_record(kind: str, config_dir: Path) -> dict:
    return config_store.add_provider({
        "kind": kind,
        "name": kind,
        "mode": "subscription",
        "base_url": "",
        "config_dir": str(config_dir),
        "runner": "native",
    })


def _arguments(root: Path, session_id: str, kind: str) -> dict:
    return {
        "run_id": str(uuid.uuid4()),
        "prompt": "preserve the prepared contract",
        "images": None,
        "files": None,
        "cwd": str(root),
        "model": (
            "claude-opus-4-6"
            if kind == "claude"
            else "Gemini 3.5 Flash (Low)"
        ),
        "reasoning_effort": "high" if kind == "claude" else None,
        "session_id": None,
        "mode": "native",
        "app_session_id": session_id,
        "source": "web",
        "disallowed_tools": None,
        "setting_sources": None,
        "backend_url": "http://127.0.0.1:8000",
        "internal_token": "must-not-persist",
        "fork": False,
        "supervised": False,
        "supervisor_agent_session_id": None,
        "worker_agent_session_id": None,
        "mssg_sender_session_id": None,
        "is_worker": False,
        "browser_harness_enabled": False,
        "user_facing": True,
        "working_mode": None,
        "extra_env": {"BU_CDP_URL": "http://127.0.0.1:9222"},
        "continuation_chain": None,
        "provider_run_config": {
            "mcp_servers": {
                "frozen": {"command": "frozen-mcp", "args": ["serve"]},
            },
            "skills": {"frozen-skill": "Use the frozen skill."},
        },
        "capability_contexts": [{
            "name": "Frozen capability",
            "category": "system",
            "content": "Use the prepared capability.",
        }],
        "target_message_id": None,
        "resolved_harness_run_config": {
            "profile_id": "frozen-profile",
            "profile_revision": "revision-1",
            "provider_run_config": {},
        },
        "turn_run_id": None,
        "disabled_builtin_extensions": [],
        "provisioned_tool_profile": "frozen-tools",
    }


def _assert_family_preparation(
    provider,
    *,
    root: Path,
    session_id: str,
    kind: str,
) -> None:
    arguments = _arguments(root, session_id, kind)
    prepared = provider.prepare_run(**arguments)
    contract = prepared.artifact.provider_contract
    if not isinstance(contract, dict):
        raise AssertionError(f"{kind} preparation has no provider contract")
    assert contract["type"] == kind
    payload = contract["contract"]["payload"]
    attestation = FamilyLaunchAttestation.from_payload(payload)
    assert attestation.family == kind
    assert attestation.attest()

    runtime_policy = prepared.artifact.runtime_policy
    runner_input = runtime_policy.get("runner_input")
    if not isinstance(runner_input, dict):
        raise AssertionError(f"{kind} preparation has no frozen runner input")
    assert runner_input["prompt"] == arguments["prompt"]
    assert runner_input["model"] == arguments["model"]
    assert runner_input["provider_run_config"]["mcp_servers"]["frozen"]
    assert runner_input["resolved_harness_run_config"]["profile_id"] == (
        "frozen-profile"
    )
    assert runner_input["provisioned_tool_profile"] == "frozen-tools"
    compatibility = runtime_policy["native_sid_compatibility"]
    assert compatibility["engine"] == f"{kind}-native"
    assert compatibility["node_id"] == "primary"
    assert compatibility["thread_store_root"] == str(
        (
            Path(provider.record["config_dir"])
            / ("projects" if kind == "claude" else "conversations")
        ).resolve()
    )
    assert compatibility["claude_project_namespace"] == (
        encode_cwd(str(root)) if kind == "claude" else None
    )

    arguments["prompt"] = "mutated"
    arguments["provider_run_config"]["mcp_servers"].clear()
    persisted = json.dumps(prepared.artifact.to_dict(), sort_keys=True)
    assert "must-not-persist" not in persisted
    assert "BU_CDP_URL" not in persisted
    assert prepared.artifact.runtime_policy["runner_input"]["prompt"] == (
        "preserve the prepared contract"
    )
    assert prepared.retry(
        run_id=str(uuid.uuid4()),
    ).artifact.provider_contract == contract


def test_claude_and_agy_prepare_complete_immutable_family_artifacts() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        _write_executable(
            bin_dir / "claude",
            "#!/bin/sh\nexit 0\n",
        )
        _write_executable(
            bin_dir / "agy",
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = models ]; then\n"
                "  printf 'Gemini 3.5 Flash (Low)\\n'\n"
            "fi\n"
            "exit 0\n",
        )
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = os.pathsep.join((str(bin_dir), old_path))
        try:
            failures: list[str] = []
            for kind, provider_class in (
                ("claude", ClaudeProvider),
                ("agy", AgyProvider),
            ):
                config_dir = root / f"{kind}-config"
                config_dir.mkdir()
                record = _provider_record(kind, config_dir)
                session = session_manager.manager.create(
                    name=f"{kind} artifact",
                    cwd=str(root),
                    orchestration_mode="native",
                    source="test",
                    provider_id=record["id"],
                    model=None,
                    permission={"mode": "dontAsk"},
                    disabled_builtin_tools=["create_session"],
                    disabled_runtime_skills=["unused-skill"],
                )
                provider = provider_class(record)
                try:
                    _assert_family_preparation(
                        provider,
                        root=root,
                        session_id=session["id"],
                        kind=kind,
                    )
                except BaseException as exc:
                    failures.append(
                        f"{kind}: {type(exc).__name__}: {exc}",
                    )
            if failures:
                raise AssertionError("\n".join(failures))
        finally:
            os.environ["PATH"] = old_path


if __name__ == "__main__":
    try:
        test_claude_and_agy_prepare_complete_immutable_family_artifacts()
    finally:
        TEST_HOME.release()
    print("PASS Claude/AGY immutable execution artifacts")
