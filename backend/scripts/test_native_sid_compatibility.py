from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from native_sid_compatibility import (
    derive_admitted_native_sid_compatibility,
    resolve_legacy_native_sid_compatibility,
)


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
