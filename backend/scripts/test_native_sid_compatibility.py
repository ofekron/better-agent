from __future__ import annotations

import json
import sys
import types
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from native_sid_compatibility import (
    NativeSidCompatibility,
    admitted_native_routing_node_id,
    admitted_native_node,
    derive_better_agent_runner_native_sid_compatibility,
    derive_admitted_native_sid_compatibility,
    resolve_legacy_native_sid_compatibility_projection,
    resolve_legacy_native_sid_compatibility,
)
from lifecycle_command_model import SelectorAuthoritySnapshot, SelectorIdentity


def test_admitted_compatibility_is_canonical_immutable_and_secret_free(
    tmp_path: Path,
) -> None:
    root = tmp_path / "claude" / "projects"
    root.mkdir(parents=True)
    compatibility = derive_admitted_native_sid_compatibility(
        engine="claude-native",
        node_id="worker-1",
        thread_store_root=root / ".." / "projects",
        claude_project_namespace="-work-repo",
    )
    assert compatibility.to_dict() == {
        "schema": 1,
        "engine": "claude-native",
        "node_id": "worker-1",
        "thread_store_root": str(root.resolve()),
        "claude_project_namespace": "-work-repo",
    }
    assert not any(
        marker in key
        for key in compatibility.to_dict()
        for marker in ("token", "secret", "credential", "api_key")
    )
    with pytest.raises(FrozenInstanceError):
        compatibility.node_id = "other"  # type: ignore[misc]


def test_admitted_routing_requires_persisted_node_id(monkeypatch) -> None:
    manager = types.SimpleNamespace(get_fields=lambda *_args, **_kwargs: {})
    monkeypatch.setitem(
        sys.modules,
        "session_manager",
        types.SimpleNamespace(manager=manager),
    )

    with pytest.raises(ValueError, match="routing is invalid"):
        admitted_native_routing_node_id(
            app_session_id="session-without-node",
            worker_session_id=None,
        )


def test_better_agent_runner_second_turn_reuses_exact_native_sid() -> None:
    with admitted_native_node("worker-ba"):
        compatibility = derive_better_agent_runner_native_sid_compatibility(
            app_session_id="session-ba",
            worker_session_id=None,
        )
    assert compatibility == NativeSidCompatibility.from_dict(
        compatibility.to_dict()
    )
    assert compatibility.engine == "better-agent-runner"
    assert Path(compatibility.thread_store_root).name == "better_agent_sessions"

    selector = SelectorIdentity("provider-ba", "model-ba", "better_agent_runner")
    first, decision = SelectorAuthoritySnapshot().admit_attempt(
        selector,
        compatibility.to_dict(),
        primary_native_sid=None,
        supervisor_native_sid=None,
        primary_native_sid_compatibility=None,
        supervisor_native_sid_compatibility=None,
    )
    assert decision == "admitted"
    attached = SelectorAuthoritySnapshot(
        generation=first.generation,
        identity=first.identity,
        native_sid_compatibility=first.native_sid_compatibility,
        primary_native_sid="ba-thread-1",
        primary_native_sid_compatibility=compatibility.to_dict(),
    )
    second, decision = attached.admit_attempt(
        selector,
        compatibility.to_dict(),
        primary_native_sid="ba-thread-1",
        supervisor_native_sid=None,
        primary_native_sid_compatibility=compatibility.to_dict(),
        supervisor_native_sid_compatibility=None,
    )
    assert decision == "admitted"
    assert second.primary_native_sid == "ba-thread-1"


def test_remote_windows_projection_remains_node_owned_and_opaque() -> None:
    payload = {
        "schema": 1,
        "engine": "better-agent-runner",
        "node_id": "windows-node",
        "thread_store_root": r"C:\\Users\\agent\\better_agent_sessions",
        "claude_project_namespace": None,
    }
    assert NativeSidCompatibility.from_dict(payload).to_dict() == payload


def test_all_better_agent_runner_preparers_supply_compatibility() -> None:
    backend = Path(__file__).resolve().parents[1]
    for filename in (
        "provider_claude_better_agent_runner.py",
        "provider_session_events_execution.py",
    ):
        source = (backend / filename).read_text(encoding="utf-8")
        assert "derive_better_agent_runner_native_sid_compatibility" in source
        assert "native_sid_compatibility=" in source


def test_legacy_claude_and_agy_require_exact_native_artifacts(
    tmp_path: Path,
) -> None:
    sid = "sid-1"
    claude = tmp_path / "claude" / "projects" / "-repo" / f"{sid}.jsonl"
    claude.parent.mkdir(parents=True)
    claude.write_text("{}\n", encoding="utf-8")
    agy = tmp_path / "agy" / "conversations" / f"{sid}.db"
    agy.parent.mkdir(parents=True)
    agy.write_bytes(b"sqlite")

    claude_result = resolve_legacy_native_sid_compatibility(
        engine="claude-native",
        node_id="primary",
        native_sid=sid,
        artifact_paths=(claude,),
    )
    assert claude_result is not None
    assert claude_result.claude_project_namespace == "-repo"
    assert claude_result.thread_store_root == str(
        (tmp_path / "claude" / "projects").resolve()
    )

    agy_result = resolve_legacy_native_sid_compatibility(
        engine="agy-native",
        node_id="primary",
        native_sid=sid,
        artifact_paths=(agy,),
    )
    assert agy_result is not None
    assert agy_result.thread_store_root == str(
        (tmp_path / "agy" / "conversations").resolve()
    )


def test_codex_requires_rollout_session_meta_for_the_actual_sid(
    tmp_path: Path,
) -> None:
    sid = "01912345-1234-7123-8123-123456789abc"
    rollout = (
        tmp_path
        / "codex"
        / "sessions"
        / "2026"
        / "08"
        / "04"
        / f"rollout-2026-08-04T00-00-00-{sid}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n",
        encoding="utf-8",
    )
    result = resolve_legacy_native_sid_compatibility(
        engine="codex-native",
        node_id="primary",
        native_sid=sid,
        artifact_paths=(rollout,),
    )
    assert result is not None
    assert result.thread_store_root == str(
        (tmp_path / "codex" / "sessions").resolve()
    )

    assert resolve_legacy_native_sid_compatibility(
        engine="codex-native",
        node_id="primary",
        native_sid="different",
        artifact_paths=(rollout,),
    ) is None


def test_missing_or_conflicting_legacy_evidence_is_unresolved(
    tmp_path: Path,
) -> None:
    sid = "sid-1"
    first = tmp_path / "one" / "projects" / "-repo" / f"{sid}.jsonl"
    second = tmp_path / "two" / "projects" / "-repo" / f"{sid}.jsonl"
    for path in (first, second):
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")

    assert resolve_legacy_native_sid_compatibility(
        engine="claude-native",
        node_id="primary",
        native_sid=sid,
        artifact_paths=(),
    ) is None
    assert resolve_legacy_native_sid_compatibility(
        engine="claude-native",
        node_id="primary",
        native_sid=sid,
        artifact_paths=(first, second),
    ) is None


def test_legacy_projection_uses_only_unambiguous_artifact_evidence(
    tmp_path: Path,
) -> None:
    sid = "legacy-sid"
    artifact = tmp_path / "claude" / "projects" / "-repo" / f"{sid}.jsonl"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}\n", encoding="utf-8")

    projected = resolve_legacy_native_sid_compatibility_projection(
        node_id="primary",
        native_sid=sid,
        artifact_paths=(artifact,),
    )
    assert projected is not None
    assert projected.engine == "claude-native"

    agy = tmp_path / "agy" / "conversations" / f"{sid}.db"
    agy.parent.mkdir(parents=True)
    agy.write_bytes(b"sqlite")
    assert resolve_legacy_native_sid_compatibility_projection(
        node_id="primary",
        native_sid=sid,
        artifact_paths=(artifact, agy),
    ) is None
    assert resolve_legacy_native_sid_compatibility_projection(
        node_id="primary",
        native_sid=sid,
        artifact_paths=(),
    ) is None


def test_legacy_artifact_evidence_is_bound_to_each_native_sid_role(
    tmp_path: Path,
) -> None:
    primary_sid = "primary-sid"
    supervisor_sid = "supervisor-sid"
    primary = (
        tmp_path / "claude" / "projects" / "-repo" / f"{primary_sid}.jsonl"
    )
    supervisor = primary.with_name(f"{supervisor_sid}.jsonl")
    primary.parent.mkdir(parents=True)
    primary.write_text("{}\n", encoding="utf-8")
    supervisor.write_text("{}\n", encoding="utf-8")

    assert resolve_legacy_native_sid_compatibility_projection(
        node_id="primary",
        native_sid=primary_sid,
        artifact_paths=(primary,),
    ) is not None
    assert resolve_legacy_native_sid_compatibility_projection(
        node_id="primary",
        native_sid=supervisor_sid,
        artifact_paths=(primary,),
    ) is None
    assert resolve_legacy_native_sid_compatibility_projection(
        node_id="primary",
        native_sid=supervisor_sid,
        artifact_paths=(supervisor,),
    ) is not None
    assert resolve_legacy_native_sid_compatibility_projection(
        node_id="primary",
        native_sid=primary_sid,
        artifact_paths=(supervisor,),
    ) is None
